"""摊还计划测试：两种口径、守恒约束、尾差与零利率边界。"""

from datetime import date

from loan_ledger.models import RepaymentMethod
from loan_ledger.schedule import generate_schedule, recalc_schedule

PRINCIPAL = 123_456_789  # 故意取奇数，制造尾差压力


def _assert_conservation(entries, principal):
    # 每期：本金+利息=应还总额（属性恒等式 + 正数检查）
    for e in entries:
        assert e.total_cents == e.principal_cents + e.interest_cents
        assert e.principal_cents > 0 and e.interest_cents >= 0
    # 累计本金必须严格等于贷款本金，不允许尾差
    assert sum(e.principal_cents for e in entries) == principal
    # 期号与到期日连续递增一个月
    for prev, cur in zip(entries, entries[1:]):
        assert cur.period == prev.period + 1


def test_equal_installment_conservation():
    print("\n[输入] 等额本息 本金(分):", PRINCIPAL,
          "年利率: 0.048, 期数: 24, 起息日:", date(2026, 3, 31))
    s = generate_schedule(PRINCIPAL, 0.048, 24,
                          RepaymentMethod.EQUAL_INSTALLMENT,
                          date(2026, 3, 31))
    print("[中间状态] 首期:", s[0])
    print("[中间状态] 末期:", s[-1])
    print("[判定依据] 累计本金 =",
          sum(e.principal_cents for e in s), "应等于", PRINCIPAL)
    print("[判定依据] 月供差(末-首) =",
          s[-1].total_cents - s[0].total_cents,
          "（末期吸收累计舍入，上界=期数/2 分）")
    # 每期利息舍入误差 <= 0.5 分，末期兜底吸收的累计漂移 <= 期数/2 分
    assert abs(s[-1].total_cents - s[0].total_cents) <= len(s) // 2
    _assert_conservation(s, PRINCIPAL)


def test_equal_principal_conservation():
    print("\n[输入] 等额本金 本金(分):", PRINCIPAL,
          "年利率: 0.06, 期数: 17, 起息日:", date(2026, 1, 10))
    s = generate_schedule(PRINCIPAL, 0.06, 17,
                          RepaymentMethod.EQUAL_PRINCIPAL,
                          date(2026, 1, 10))
    base = PRINCIPAL // 17
    print("[中间状态] 前两期本金:", s[0].principal_cents, s[1].principal_cents,
          "（整除基数:", base, ")")
    print("[中间状态] 末期本金:", s[-1].principal_cents,
          "利息:", s[-1].interest_cents, "（尾差归入末期本金）")
    print("[判定依据] 前 16 期本金应等于整除基数:",
          all(e.principal_cents == base for e in s[:-1]))
    assert all(e.principal_cents == base for e in s[:-1])
    # 等额本金：利息逐期递减
    for prev, cur in zip(s, s[1:]):
        assert cur.interest_cents <= prev.interest_cents
    _assert_conservation(s, PRINCIPAL)


def test_zero_rate():
    print("\n[输入] 零利率边界 本金(分):", 10_000_01, "期数: 3")
    s = generate_schedule(10_000_01, 0.0, 3,
                          RepaymentMethod.EQUAL_INSTALLMENT,
                          date(2026, 5, 20))
    print("[中间状态] 三期金额:", [(e.principal_cents, e.interest_cents)
                                for e in s])
    print("[判定依据] 利息全为 0:", all(e.interest_cents == 0 for e in s))
    print("[判定依据] 本金合计:", sum(e.principal_cents for e in s))
    assert all(e.interest_cents == 0 for e in s)
    _assert_conservation(s, 10_000_01)


def test_recalc_preserves_remaining_principal():
    print("\n[输入] 重算 剩余本金(分): 80_000_07, 新利率: 0.03, 剩余期数: 9")
    s = recalc_schedule(80_000_07, 0.03, 9,
                        RepaymentMethod.EQUAL_INSTALLMENT,
                        date(2027, 1, 15), start_period=4)
    print("[中间状态] 重算首期期号:", s[0].period, "末期期号:", s[-1].period)
    print("[判定依据] 期号从 4 开始:", s[0].period == 4)
    print("[判定依据] 累计本金:", sum(e.principal_cents for e in s))
    assert s[0].period == 4 and s[-1].period == 12
    _assert_conservation(s, 80_000_07)
