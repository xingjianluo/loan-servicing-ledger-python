"""还款入账测试：分配优先级、部分还款、幂等（重复/并发）、结清拒绝。"""

import threading
from datetime import date

import pytest

from loan_ledger import (
    AccountSettledError,
    AccountStatus,
    ExcessPaymentError,
    InvalidAmountError,
    LoanAccount,
    LoanConfig,
    PrepaymentMode,
    RepaymentMethod,
)


def make_account(**kw) -> LoanAccount:
    cfg = LoanConfig(
        principal_cents=1_000_000_00, annual_rate=0.036, periods=12,
        method=RepaymentMethod.EQUAL_INSTALLMENT,
        start_date=date(2026, 1, 1), grace_days=3,
        penalty_daily_rate=0.0005, **kw)
    return LoanAccount(cfg)


def test_partial_payment_allocates_interest_first():
    acc = make_account()
    e1 = acc.schedule[0]
    print("\n[输入] 首期应还(分):", e1.total_cents,
          "其中利息:", e1.interest_cents, "本金:", e1.principal_cents)
    r = acc.post_payment("p1", 1000_00, date(2026, 2, 1))
    print("[中间状态] 分配结果:", r.allocation)
    print("[判定依据] 部分还款应先全部冲利息:",
          r.allocation.interest_cents == 1000_00
          and r.allocation.principal_cents == 0)
    assert r.allocation.interest_cents == 1000_00
    assert r.allocation.principal_cents == 0
    assert r.allocation.total_cents == 1000_00  # 分配守恒
    b = acc.balances()
    print("[中间状态] 入账后余额:", b)
    assert b["interest"] == sum(
        e.interest_cents for e in acc.schedule) - 1000_00


def test_full_first_period_then_prepayment_recalc():
    acc = make_account()
    e1 = acc.schedule[0]
    acc.post_payment("p1", e1.total_cents, date(2026, 2, 1))
    before = [e.total_cents for e in acc.schedule[1:]]
    prepay = 20_000_000  # 提前还 20 万元本金
    r = acc.apply_prepayment("pp-1", prepay, date(2026, 2, 5),
                             PrepaymentMode.REDUCE_INSTALLMENT)
    print("\n[输入] 提前还款(分):", prepay, "方式: 期数不变降低月供")
    print("[中间状态] 回执:", r)
    assert r.principal_before_cents - r.principal_after_cents == prepay
    assert r.periods_after == r.periods_before  # 期数不变
    after = [e.total_cents for e in acc.schedule[1:]]
    print("[中间状态] 重算前第2期月供:", before[0], "重算后:", after[0])
    print("[判定依据] 重算后月供应下降:", after[0] < before[0])
    assert after[0] < before[0]
    # 守恒：所有期次未还本金合计 == 账户剩余本金
    b = acc.balances()
    unpaid = sum(e.principal_cents - e.paid_principal_cents
                 for e in acc.schedule)
    print("[判定依据] 各期未还本金合计:", unpaid, "== 账户剩余本金:",
          b["principal"])
    assert unpaid == b["principal"]


def test_duplicate_request_id_is_idempotent():
    acc = make_account()
    r1 = acc.post_payment("dup-1", 500_00, date(2026, 2, 1))
    snapshot = acc.balances()
    r2 = acc.post_payment("dup-1", 500_00, date(2026, 2, 1))
    r3 = acc.post_payment("dup-1", 500_00, date(2026, 2, 1))
    print("\n[输入] 同一 request_id=dup-1 重复提交 3 次，金额 500_00")
    print("[中间状态] 首次分配:", r1.allocation,
          "重放标记:", r2.duplicate, r3.duplicate)
    print("[判定依据] 余额未被重复扣减:", acc.balances() == snapshot)
    assert r2.duplicate and r3.duplicate
    assert acc.balances() == snapshot
    assert r2.allocation == r1.allocation


def test_concurrent_same_request_id_charged_once():
    acc = make_account()
    results, errors = [], []

    def pay():
        try:
            results.append(acc.post_payment("c-1", 500_00, date(2026, 2, 1)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=pay) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n[输入] 16 线程并发提交同一 request_id=c-1，金额 500_00")
    print("[中间状态] 成功回执数:", len(results), "异常数:", len(errors),
          "其中重复回执:", sum(1 for r in results if r.duplicate))
    total_charged = sum(
        r.allocation.total_cents for r in results if not r.duplicate)
    print("[判定依据] 非重复入账总额:", total_charged, "应恰为 500_00")
    assert not errors
    assert len(results) == 16
    assert sum(1 for r in results if not r.duplicate) == 1
    assert total_charged == 500_00
    assert acc.balances()["interest"] == sum(
        e.interest_cents for e in acc.schedule) - 500_00


def test_concurrent_distinct_requests_no_overcharge():
    acc = make_account()
    e1_total = acc.schedule[0].total_cents
    n = 8
    amount = e1_total // n
    results, errors = [], []

    def pay(i):
        try:
            results.append(acc.post_payment(f"t-{i}", amount,
                                            date(2026, 2, 1)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=pay, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n[输入] 8 线程并发不同 request_id，各还", amount)
    print("[中间状态] 成功:", len(results), "失败:", len(errors))
    charged = sum(r.allocation.total_cents for r in results)
    print("[判定依据] 入账总额:", charged, "== 请求总额:", amount * n)
    assert not errors
    assert charged == amount * n  # 无重复扣减、无丢失


def test_settled_account_rejects_payment():
    acc = make_account()
    # 先还首期到期应还，再申请提前还完全部剩余本金（提前结清）
    e1 = acc.schedule[0]
    acc.post_payment("p1", e1.total_cents, date(2026, 2, 1))
    remaining = acc.balances()["principal"]
    r = acc.apply_prepayment("settle", remaining, date(2026, 2, 1),
                             PrepaymentMode.REDUCE_TERM)
    print("\n[输入] 还首期后提前还完全部剩余本金:", remaining,
          "随后再还 1 分")
    print("[中间状态] 结清后状态:", acc.status, "剩余期数:", r.periods_after)
    assert acc.status is AccountStatus.SETTLED
    with pytest.raises(AccountSettledError):
        acc.post_payment("again", 1, date(2026, 2, 2))
    with pytest.raises(AccountSettledError):
        acc.apply_prepayment("again-pp", 1, date(2026, 2, 2),
                             PrepaymentMode.REDUCE_TERM)
    print("[判定依据] 已结清账户普通还款/提前还款均抛出 AccountSettledError")


def test_invalid_amount_and_excess_payment():
    acc = make_account()
    print("\n[输入] 非法金额: 0, -100, 1.5；超额: 应还总额+1")
    for bad in (0, -100, 1.5):
        with pytest.raises(InvalidAmountError):
            acc.post_payment(f"bad-{bad}", bad, date(2026, 2, 1))
    total = sum(e.total_cents for e in acc.schedule)
    with pytest.raises(ExcessPaymentError):
        acc.post_payment("over", total + 1, date(2026, 2, 1))
    print("[判定依据] 非法金额与超额还款分别抛出"
          " InvalidAmountError / ExcessPaymentError，且账户未变动")
    assert acc.balances()["principal"] == 1_000_000_00


def test_rate_adjustment_recalc_and_conservation():
    acc = make_account()
    # 部分偿还首期（利息还一半），再加息：当期保持原金额，未来重算
    e1 = acc.schedule[0]
    acc.post_payment("r-1", e1.interest_cents // 2, date(2026, 2, 1))
    old_future_interest = sum(e.interest_cents for e in acc.schedule[1:])
    old_future_principal = sum(e.principal_cents for e in acc.schedule[1:])
    acc.adjust_rate(0.072, date(2026, 2, 1))
    new_future_interest = sum(e.interest_cents for e in acc.schedule[1:])
    new_future_principal = sum(e.principal_cents for e in acc.schedule[1:])
    print("\n[输入] 利率 0.036 -> 0.072，生效 2026-02-01")
    print("[中间状态] 未来利息合计:", old_future_interest,
          "->", new_future_interest)
    print("[判定依据] 加息后利息上升:",
          new_future_interest > old_future_interest)
    print("[判定依据] 本金合计不变:",
          old_future_principal == new_future_principal, "=",
          new_future_principal)
    print("[判定依据] 首期利率仍为 0.036:", acc.schedule[0].rate == 0.036,
          "第二期为 0.072:", acc.schedule[1].rate == 0.072)
    assert new_future_interest > old_future_interest
    assert old_future_principal == new_future_principal
    assert acc.schedule[0].rate == 0.036
    assert acc.schedule[1].rate == 0.072
    # 全局守恒：所有期次本金合计 == 原始贷款本金
    assert sum(e.principal_cents for e in acc.schedule) == 1_000_000_00
