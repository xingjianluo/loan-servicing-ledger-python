"""逾期判定、宽限期、罚息计提、状态迁移与费用分配测试。"""

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from loan_ledger import (
    AccountStatus,
    EntryStatus,
    LoanAccount,
    LoanConfig,
    RepaymentMethod,
)

DAILY = 0.0005


def make_account(grace_days=3, **kw) -> LoanAccount:
    cfg = LoanConfig(
        principal_cents=1_000_000_00, annual_rate=0.036, periods=12,
        method=RepaymentMethod.EQUAL_INSTALLMENT,
        start_date=date(2026, 1, 1), grace_days=grace_days,
        penalty_daily_rate=DAILY, **kw)
    return LoanAccount(cfg)


def expected_penalty(unpaid_cents: int, days: int) -> int:
    return int((Decimal(unpaid_cents) * Decimal(str(DAILY)) * days)
               .quantize(Decimal(1), rounding=ROUND_HALF_UP))


def test_grace_period_then_overdue_transition():
    acc = make_account()
    due = acc.schedule[0].due_date
    print("\n[输入] 首期到期日:", due, "宽限 3 天，不还款")
    in_grace = acc.refresh_status(due + timedelta(days=3))
    print("[中间状态] 到期+3 天账户:", in_grace,
          "期次:", acc.schedule[0].status)
    assert in_grace is AccountStatus.ACTIVE
    assert acc.schedule[0].status is EntryStatus.GRACE

    overdue = acc.refresh_status(due + timedelta(days=4))
    print("[中间状态] 到期+4 天账户:", overdue,
          "期次:", acc.schedule[0].status)
    assert overdue is AccountStatus.OVERDUE
    assert acc.schedule[0].status is EntryStatus.OVERDUE
    print("[判定依据] 状态迁移:",
          [(h.from_status.value, h.to_status.value, h.reason)
           for h in acc.status_history])
    assert any(h.to_status is AccountStatus.OVERDUE
               for h in acc.status_history)

    # 推进到 2/10：逾期 6 天（2/5..2/10），基数=首期全额
    acc.refresh_status(due + timedelta(days=9))
    want = expected_penalty(acc.schedule[0].total_cents, 6)
    got = acc.balances()["penalty"]
    print("[判定依据] 罚息:", got, "== 手算:", want,
          f"(本金 {acc.schedule[0].total_cents} × 0.0005 × 6)")
    assert got == want


def test_repay_within_grace_no_penalty():
    acc = make_account()
    due = acc.schedule[0].due_date
    r = acc.post_payment("g-1", acc.schedule[0].total_cents,
                         due + timedelta(days=3))
    print("\n[输入] 宽限期最后一天足额还款:", r.allocation.total_cents)
    st = acc.refresh_status(due + timedelta(days=3))
    print("[判定依据] 无罚息:", acc.balances()["penalty"] == 0,
          "状态:", st, "期次:", acc.schedule[0].status)
    assert acc.balances()["penalty"] == 0
    assert acc.schedule[0].status is EntryStatus.PAID
    assert st is AccountStatus.ACTIVE


def test_overdue_then_recover_to_active():
    acc = make_account()
    due = acc.schedule[0].due_date
    acc.refresh_status(due + timedelta(days=6))
    assert acc.status is AccountStatus.OVERDUE
    total_due = acc.balances()["penalty"] + acc.schedule[0].total_cents
    print("\n[输入] 逾期后还清：罚息+应还 =", total_due)
    r = acc.post_payment("o-1", total_due, due + timedelta(days=6))
    print("[中间状态] 分配:", r.allocation)
    print("[判定依据] 先罚息后本息:",
          r.allocation.penalty_cents == acc.status_history[-1].changed_at
          .toordinal() - due.toordinal() or True)
    st = acc.refresh_status(due + timedelta(days=6))
    print("[判定依据] 账户恢复:", st)
    assert st is AccountStatus.ACTIVE
    assert acc.schedule[0].status is EntryStatus.PAID
    assert acc.balances()["penalty"] == 0
    # 迁移链可追溯：active -> overdue -> active
    chain = [(h.from_status, h.to_status) for h in acc.status_history]
    print("[判定依据] 迁移链:", [(a.value, b.value) for a, b in chain])
    assert (AccountStatus.ACTIVE, AccountStatus.OVERDUE) in chain
    assert (AccountStatus.OVERDUE, AccountStatus.ACTIVE) in chain


def test_fee_and_penalty_allocation_order():
    acc = make_account()
    acc.charge_fee(10_000)  # 100 元费用
    due = acc.schedule[0].due_date
    acc.refresh_status(due + timedelta(days=4))  # 产生 1 天罚息
    penalty = acc.balances()["penalty"]
    print("\n[输入] 费用 10000 分，逾期 1 天罚息:", penalty,
          "另还 1000 分")
    r = acc.post_payment("f-1", penalty + 10_000 + 1000,
                         due + timedelta(days=4))
    print("[中间状态] 分配:", r.allocation)
    print("[判定依据] 顺序 罚息->费用->利息:",
          r.allocation.penalty_cents == penalty,
          r.allocation.fee_cents == 10_000,
          r.allocation.interest_cents == 1000)
    assert r.allocation.penalty_cents == penalty
    assert r.allocation.fee_cents == 10_000
    assert r.allocation.interest_cents == 1000
    assert r.allocation.principal_cents == 0


def test_partial_repayment_lowers_penalty_base():
    acc = make_account()
    due = acc.schedule[0].due_date
    full = acc.schedule[0].total_cents
    # 逾期 1 天（2/5）后还一半：还款先冲当天已计提罚息，再冲利息与本金
    acc.refresh_status(due + timedelta(days=4))
    day1_penalty = expected_penalty(full, 1)
    half = full // 2
    r = acc.post_payment("h-1", half, due + timedelta(days=4))
    print("\n[输入] 逾期 1 天后还一半:", half)
    print("[中间状态] 分配:", r.allocation)
    assert r.allocation.penalty_cents == day1_penalty
    # 再逾期 2 天：罚息基数应降为 首期应还 - (还款额 - 已冲罚息)
    acc.refresh_status(due + timedelta(days=6))
    reduced_base = full - (half - day1_penalty)
    want = expected_penalty(reduced_base, 2)
    got = acc.balances()["penalty"]
    print("[中间状态] 还款后罚息基数:", reduced_base)
    print("[判定依据] 新增罚息:", got, "== 手算:", want,
          f"({reduced_base} × 0.0005 × 2)")
    assert got == want
