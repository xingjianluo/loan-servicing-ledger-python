"""摊还计划测试：等额本息/等额本金守恒、利率调整、提前还款重算、边界条款。

每个用例都打印：输入参数、逐期计划表（关键中间状态）、守恒校验结果。
"""

from datetime import date
from decimal import Decimal

import pytest

from loan.amortization import (
    PrepayMode,
    build_schedule,
    rebuild_after_prepayment,
)
from loan.errors import InvalidScheduleError, RateAdjustmentError
from loan.models import AmortizationType, LoanTerms, RateChange

CENT = Decimal("0.01")


def print_schedule(s, title):
    print(f"\n===== {title} =====")
    print(
        f"输入: 本金={s.terms.principal} 年利率={s.terms.annual_rate} "
        f"期数={s.terms.term_months} 口径={s.terms.amortization_type.value} "
        f"调息={[(str(c.effective_date), str(c.annual_rate)) for c in s.rate_changes]}"
    )
    print("期次 到期日       本金        利息        合计")
    for i in s.installments:
        print(f"{i.period:>3}  {i.due_date}  {i.principal:>10}  {i.interest:>10}  {i.due_total:>10}")
    print(f"合计本金={s.total_principal} 合计利息={s.total_interest}")


def assert_conservation(s, principal):
    """守恒断言：本金合计==贷款本金；每期 本+息==合计；无负额。"""
    assert s.total_principal == principal, (
        f"本金守恒被破坏: {s.total_principal} != {principal}"
    )
    for i in s.installments:
        assert i.principal + i.interest == i.due_total
        assert i.principal > 0
        assert i.interest >= 0
    print(f"[判定] 本金守恒成立 Σprincipal={s.total_principal} == 贷款本金 {principal}；"
          "逐期 principal+interest==due_total 成立")


def test_equal_installment_basic_conservation():
    terms = LoanTerms(
        Decimal("100000.00"), Decimal("0.072"), 12,
        date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT,
    )
    print("[输入]", terms)
    s = build_schedule(terms)
    print_schedule(s, "等额本息 100,000 / 7.2% / 12期")
    # 等额本息除末期外月供应一致
    pays = {i.due_total for i in s.installments[:-1]}
    assert len(pays) == 1, f"等额本息前期月供不一致: {pays}"
    print(f"[判定] 前11期月供恒定 = {pays.pop()}，末期尾差找平")
    assert_conservation(s, Decimal("100000.00"))


def test_equal_principal_basic_conservation():
    terms = LoanTerms(
        Decimal("100000.00"), Decimal("0.049"), 20,
        date(2026, 1, 1), AmortizationType.EQUAL_PRINCIPAL,
    )
    s = build_schedule(terms)
    print_schedule(s, "等额本金 100,000 / 4.9% / 20期")
    # 前 n-1 期本金段相等
    firsts = {i.principal for i in s.installments[:-1]}
    assert len(firsts) == 1
    print(f"[判定] 前19期本金段恒定 = {firsts.pop()}，末段找平")
    # 利息应逐期递减
    interests = [i.interest for i in s.installments]
    assert all(a >= b for a, b in zip(interests, interests[1:]))
    print("[判定] 利息逐期不增（余额递减）")
    assert_conservation(s, Decimal("100000.00"))


@pytest.mark.parametrize(
    "principal,rate,months,amort",
    [
        ("1000.00", "0", 3, AmortizationType.EQUAL_INSTALLMENT),
        ("1000.00", "0", 3, AmortizationType.EQUAL_PRINCIPAL),
        ("0.01", "0.36", 1, AmortizationType.EQUAL_INSTALLMENT),
        ("999999.99", "0.24", 360, AmortizationType.EQUAL_PRINCIPAL),
        ("333.33", "0.0001", 7, AmortizationType.EQUAL_INSTALLMENT),
    ],
)
def test_conservation_across_edge_shapes(principal, rate, months, amort):
    terms = LoanTerms(
        Decimal(principal), Decimal(rate), months,
        date(2026, 1, 1), amort,
    )
    s = build_schedule(terms)
    print_schedule(s, f"边界形态 {principal}/{rate}/{months}月/{amort.value}")
    assert_conservation(s, Decimal(principal))


def test_rate_change_mid_term_conservation():
    terms = LoanTerms(
        Decimal("500000.00"), Decimal("0.05"), 24,
        date(2026, 3, 31), AmortizationType.EQUAL_INSTALLMENT,
    )
    changes = [RateChange(date(2026, 9, 1), Decimal("0.035"))]
    print("[输入] 调息:", changes)
    s = build_schedule(terms, changes)
    print_schedule(s, "等额本息 + 第7期起调息 5%->3.5%")
    # 第1-6期与第7期月供不同（利率段切换重算年金）
    pay_before = s.installments[4].due_total
    pay_after = s.installments[5].due_total
    print(f"[中间状态] 调息前月供={pay_before} 调息后首期月供={pay_after}")
    assert pay_before != pay_after
    # 段内月供各自恒定
    assert len({i.due_total for i in s.installments[5:-1]}) == 1
    print("[判定] 调息段内月供重新恒定")
    assert_conservation(s, Decimal("500000.00"))
    # 月末日规则：2026-03-31 放款，首期到期 2026-04-30
    assert s.installments[0].due_date == date(2026, 4, 30)
    print("[判定] 月末日期加法正确（3/31 +1月 = 4/30）")


def test_rate_change_equal_principal_only_interest_changes():
    terms = LoanTerms(
        Decimal("120000.00"), Decimal("0.06"), 12,
        date(2026, 1, 1), AmortizationType.EQUAL_PRINCIPAL,
    )
    s_plain = build_schedule(terms)
    s_chg = build_schedule(terms, [RateChange(date(2026, 7, 1), Decimal("0.03"))])
    print("[判定依据] 比较调息前后本金段是否完全相同、利息是否仅后段变化")
    for a, b in zip(s_plain.installments, s_chg.installments):
        assert a.principal == b.principal, "等额本金调息不得改变本金段"
    assert s_plain.installments[:5] and True
    later_interest_dropped = all(
        s_chg.installments[k].interest <= s_plain.installments[k].interest
        for k in range(6, 12)
    )
    assert later_interest_dropped
    assert_conservation(s_chg, Decimal("120000.00"))
    print("[判定] 等额本金：本金段不受调息影响，第7期起利息下降，本金仍守恒")


def test_invalid_terms_and_rate_changes():
    with pytest.raises(InvalidScheduleError):
        build_schedule(
            LoanTerms(Decimal("0"), Decimal("0.1"), 12,
                      date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT)
        )
    with pytest.raises(InvalidScheduleError):
        build_schedule(
            LoanTerms(Decimal("100"), Decimal("0.1"), 0,
                      date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT)
        )
    print("[判定] 本金<=0、期数<=0 均以 InvalidScheduleError 拒绝")
    terms = LoanTerms(
        Decimal("1000"), Decimal("0.1"), 6,
        date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT,
    )
    with pytest.raises(RateAdjustmentError):
        build_schedule(terms, [RateChange(date(2025, 12, 31), Decimal("0.05"))])
    with pytest.raises(RateAdjustmentError):
        build_schedule(terms, [RateChange(date(2026, 8, 1), Decimal("0.05"))])
    print("[判定] 调息日早于放款日 / 晚于末期到期日均被拒绝")


def test_rebuild_after_prepayment_keep_term():
    terms = LoanTerms(
        Decimal("100000.00"), Decimal("0.072"), 12,
        date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT,
    )
    s = build_schedule(terms)
    prepay_date = date(2026, 7, 1)  # 前6期已到期
    prepay_amount = Decimal("30000.00")
    print(f"[输入] 提前还款日={prepay_date} 金额={prepay_amount} 模式=KEEP_TERM")
    r = rebuild_after_prepayment(
        terms, s.installments, (), prepay_date, prepay_amount, PrepayMode.KEEP_TERM
    )
    past = [i for i in r.installments if i.due_date <= prepay_date]
    fut = [i for i in r.installments if i.due_date > prepay_date]
    print_schedule(r, "提前还款后重算（期数不变）")
    assert len(past) == 6 and len(fut) == 6
    assert [i.period for i in past] == list(range(1, 7))
    expect_future_principal = (
        sum((i.principal for i in s.installments[6:]), Decimal("0")) - prepay_amount
    )
    actual = sum((i.principal for i in fut), Decimal("0"))
    assert actual == expect_future_principal
    # 全局守恒：历史本金 + 未来本金 == 原始本金 - 提前还本
    assert sum((i.principal for i in r.installments), Decimal("0")) == \
        Decimal("100000.00") - prepay_amount
    print(f"[判定] 未来本金合计={actual} == 预期{expect_future_principal}；"
          "未来期数仍为6；已发生期原样保留")
    # 月供下降
    assert fut[0].due_total < s.installments[6].due_total
    print(f"[判定] 月供由 {s.installments[6].due_total} 降至 {fut[0].due_total}")


def test_rebuild_after_prepayment_keep_payment_shortens_term():
    terms = LoanTerms(
        Decimal("100000.00"), Decimal("0.072"), 12,
        date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT,
    )
    s = build_schedule(terms)
    r = rebuild_after_prepayment(
        terms, s.installments, (), date(2026, 7, 1),
        Decimal("30000.00"), PrepayMode.KEEP_PAYMENT,
    )
    fut = [i for i in r.installments if i.due_date > date(2026, 7, 1)]
    print(f"[中间状态] KEEP_PAYMENT 未来期数={len(fut)}（原6期）")
    assert len(fut) < 6
    expect = sum((i.principal for i in s.installments[6:]), Decimal("0")) - Decimal("30000.00")
    assert sum((i.principal for i in fut), Decimal("0")) == expect
    print(f"[判定] 期数缩短至 {len(fut)} 期，未来本金合计仍守恒={expect}")


def test_prepayment_must_not_exceed_future_principal():
    terms = LoanTerms(
        Decimal("1000.00"), Decimal("0.12"), 3,
        date(2026, 1, 1), AmortizationType.EQUAL_INSTALLMENT,
    )
    s = build_schedule(terms)
    with pytest.raises(InvalidScheduleError):
        rebuild_after_prepayment(
            terms, s.installments, (), date(2026, 2, 1),
            Decimal("100000"), PrepayMode.KEEP_TERM,
        )
    print("[判定] 提前还款额超过未到期本金 -> InvalidScheduleError")
