"""账户层测试：逾期判定、宽限期、罚息/费用计提、状态迁移、还款分配、结清拒绝。

用例打印输入、关键中间状态（余额视图、事件流）与判定依据。
"""

from datetime import date
from decimal import Decimal

import pytest

from loan.account import Account
from loan.errors import (
    AccountClosedError,
    DuplicatePaymentError,
    InvalidAmountError,
    PaymentExceedsDueError,
)
from loan.models import (
    ALLOCATION_PRINCIPAL,
    AmortizationType,
    LoanTerms,
)

CENT = Decimal("0.01")


def make_account(**over):
    params = dict(
        principal=Decimal("3000.00"),
        annual_rate=Decimal("0.12"),
        term_months=3,
        origination_date=date(2026, 1, 1),
        amortization_type=AmortizationType.EQUAL_INSTALLMENT,
        grace_period_days=3,
        penalty_daily_rate=Decimal("0.0005"),
        fee_rate=Decimal("0.01"),
    )
    params.update(over)
    return Account.open("A-001", LoanTerms(**params))


def print_view(acc, as_of, tag=""):
    b = acc.balance(as_of)
    print(
        f"[余额视图 {tag} @ {as_of}] status={b.status.value} "
        f"到期本金={b.due_principal} 利息={b.due_interest} "
        f"罚息={b.due_penalty} 费用={b.due_fee} 到期合计={b.due_total} "
        f"剩余总本金={b.outstanding_principal}"
    )
    return b


def test_grace_period_then_overdue_and_penalty_accrual():
    acc = make_account()
    inst1 = acc.installments[0]
    print("[输入] 第1期到期日", inst1.due_date, "宽限3天；应还",
          inst1.due_total, "其中本金", inst1.unpaid_principal)

    # 宽限期内（due+3 = 02-04）：仍 ACTIVE，无罚息
    b = print_view(acc, date(2026, 2, 4), "宽限届满日当天")
    assert b.status.value == "active" and b.due_penalty == 0 and b.due_fee == 0

    # 02-05 起逾期，当天计提1天罚息 + 一次性管理费
    b = print_view(acc, date(2026, 2, 5), "逾期首日")
    assert b.status.value == "overdue"
    base = inst1.unpaid_principal
    assert b.due_penalty == (base * Decimal("0.0005")).quantize(CENT)
    assert b.due_fee == (base * Decimal("0.01")).quantize(CENT)
    print(f"[判定] 罚息=未还本金{base} * 日率0.0005 * 1天 = {b.due_penalty}；"
          f"管理费=本金 * 1% = {b.due_fee}，仅计提一次")

    # 跨4天到 02-08：罚息增量到4天（不是重新算）
    b = print_view(acc, date(2026, 2, 8), "逾期第4天")
    assert b.due_penalty == (base * Decimal("0.0005") * 4).quantize(CENT)
    assert b.due_fee == (base * Decimal("0.01")).quantize(CENT), "管理费不重复计提"
    print("[判定] 罚息增量计提到4天，管理费仍只一次")

    # 重复扫描同一天：幂等，不翻倍
    acc.scan(date(2026, 2, 8))
    b2 = acc.balance(date(2026, 2, 8))
    assert b2.due_penalty == b.due_penalty
    print("[判定] 同日重复扫描幂等")

    types = [e.event_type for e in acc.events]
    assert types.count("FEE_CHARGED") == 1
    assert types.count("STATE_CHANGED") == 1
    print("[事件]", types)


def test_partial_payment_allocation_order_and_conservation():
    acc = make_account()
    b = print_view(acc, date(2026, 2, 8), "还款前")
    before_p = acc.installments[0].unpaid_principal
    before_i = acc.installments[0].unpaid_interest
    before_pen = b.due_penalty
    before_fee = b.due_fee

    rec = acc.pay("P-100", "100.00", date(2026, 2, 8))
    print(f"[输入] 部分还款 100.00（默认顺序 息->罚息->费->本）")
    print(f"[分配] 利息={rec.allocation.interest} 罚息={rec.allocation.penalty} "
          f"费用={rec.allocation.fee} 本金={rec.allocation.principal}")
    assert rec.allocation.total == Decimal("100.00")
    assert rec.allocation.interest == before_i
    assert rec.allocation.penalty == before_pen
    assert rec.allocation.fee == before_fee
    assert rec.allocation.principal == Decimal("100.00") - before_i - before_pen - before_fee

    b2 = print_view(acc, date(2026, 2, 8), "还款后")
    # 守恒：扣减的本金 = 分配本金；到期合计下降恰为 100
    assert b2.due_total == b.due_total - Decimal("100.00")
    assert acc.installments[0].unpaid_principal == before_p - rec.allocation.principal
    print("[判定] 分配合计==还款额；各桶余额下降额与分配一致（守恒）")


def test_partial_payment_principal_first_custom_order():
    acc = make_account()
    b = print_view(acc, date(2026, 2, 8), "还款前(自定义顺序)")
    order = (
        ALLOCATION_PRINCIPAL,
        "interest",
        "penalty",
        "fee",
    )
    rec = acc.pay("P-PRIM", "100.00", date(2026, 2, 8), allocation_order=order)
    print(f"[分配(本金优先)] {rec.allocation}")
    assert rec.allocation.principal == Decimal("100.00")
    assert rec.allocation.interest == 0
    print("[判定] 自定义顺序 本->息->罚息->费 生效：100 元全部冲本金")


def test_idempotent_duplicate_and_conflicting_payment():
    acc = make_account()
    r1 = acc.pay("DUP", "100.00", date(2026, 2, 8))
    r2 = acc.pay("DUP", "100.00", date(2026, 2, 8))
    assert r1.allocation == r2.allocation and len(acc.payments) == 1
    print("[判定] 同 payment_id 同参数重复提交 -> 返回原凭证，仅入账一次")
    with pytest.raises(DuplicatePaymentError):
        acc.pay("DUP", "101.00", date(2026, 2, 8))
    print("[判定] 同 payment_id 不同参数 -> DuplicatePaymentError 拒绝")


def test_invalid_amounts_rejected():
    acc = make_account()
    for bad, why in [
        ("0", "零金额"), ("-5.00", "负数"), ("abc", "非数字"),
        ("1.999", "超过分精度"), (True, "布尔"),
    ]:
        with pytest.raises(InvalidAmountError):
            acc.pay("X-" + str(bad), bad, date(2026, 2, 8))
        print(f"[判定] 非法金额 {bad!r}（{why}）-> InvalidAmountError")


def test_overpayment_rejected_with_allowed_hint():
    acc = make_account()
    b = print_view(acc, date(2026, 2, 8), "超额还款前")
    with pytest.raises(PaymentExceedsDueError) as ei:
        acc.pay("BIG", b.due_total + Decimal("0.01"), date(2026, 2, 8))
    assert ei.value.allowed == b.due_total
    print(f"[判定] 超过到期应还 -> PaymentExceedsDueError，allowed={b.due_total}")


def test_repay_overdue_recovers_to_active():
    acc = make_account()
    b = print_view(acc, date(2026, 2, 8), "补齐前")
    acc.pay("CURE", b.due_total, date(2026, 2, 8))
    b2 = print_view(acc, date(2026, 2, 8), "补齐后（第2期03-01未到期）")
    assert b2.status.value == "active" and b2.due_total == 0
    states = [e.detail for e in acc.events if e.event_type == "STATE_CHANGED"]
    assert states[0]["from"] == "active" and states[0]["to"] == "overdue"
    assert states[1]["from"] == "overdue" and states[1]["to"] == "active"
    print("[判定] 状态迁移 overdue -> active，事件可追溯：", states)


def test_full_settlement_then_repost_rejected():
    acc = make_account(grace_period_days=0, penalty_daily_rate=Decimal("0"),
                       fee_rate=Decimal("0"))
    schedule = [(i.period, i.due_total) for i in acc.installments]
    print("[输入] 各期应还：", schedule)
    last_due = acc.installments[-1].due_date
    totals = []
    for pid, (period, _) in zip(["S1", "S2", "S3"], schedule):
        d = acc.installments[period - 1].due_date
        due = acc.balance(d).due_total
        totals.append(due)
        rec = acc.pay(pid, due, d)
        print(f"[入账] {pid} {d} 金额={due} 分配={rec.allocation}")
    b = acc.balance(last_due)
    print_view(acc, last_due, "末期还清后")
    assert b.status.value == "settled" and b.outstanding_principal == 0
    with pytest.raises(AccountClosedError):
        acc.pay("S4", "1.00", last_due)
    print("[判定] 全部清偿 -> SETTLED；结清后再次入账 -> AccountClosedError")
    # 资金守恒：三次还款的本金合计 == 贷款本金
    principal_back = sum((p.allocation.principal for p in acc.payments.values()), Decimal("0"))
    assert principal_back == Decimal("3000.00")
    print(f"[判定] 已还本金合计 {principal_back} == 贷款本金 3000.00")
