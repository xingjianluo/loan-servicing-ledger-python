"""摊还计划生成与重算。

金额一律为整数“分”。守恒约束：
- 每期 本金+利息 = 本期应还总额（由构造保证）；
- 累计本金 = 贷款本金（尾差归入最后一期本金）；
- 利息按剩余本金 * 月利率 四舍五入（ROUND_HALF_UP）到分。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .errors import InvalidRequestError
from .models import RepaymentMethod, ScheduleEntry

_CENT = Decimal("1")


def _round_cents(value: Decimal) -> int:
    return int(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    # 月末对齐：如 1/31 + 1 月 -> 2/28(29)
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _build(
    principal_cents: int,
    rates: list[float],
    periods: int,
    method: RepaymentMethod,
    first_due_date: date,
    start_period: int,
) -> list[ScheduleEntry]:
    """按逐期年利率构建摊还计划。len(rates) 必须 >= periods。

    逐期利率用于在提前还款重算时保住已生效的调息结果：
    每一期的适用利率由 rates[i] 指定，不会被单一利率覆盖。
    """
    if principal_cents <= 0:
        raise InvalidRequestError("本金必须为正整数分")
    if periods <= 0:
        raise InvalidRequestError("期数必须为正整数")
    if len(rates) < periods:
        raise InvalidRequestError("逐期利率数量不足")
    if any(rate < 0 for rate in rates):
        raise InvalidRequestError("利率不能为负")

    monthly = [Decimal(str(rates[i])) / 12 for i in range(periods)]
    balance = principal_cents
    entries: list[ScheduleEntry] = []

    if method is RepaymentMethod.EQUAL_INSTALLMENT:
        # 等额本息：逐期利率可能不同，月供无法一次算清，
        # 从最后一期往前递推“每期月供相同”的精确解再取整。
        payment = _solve_installment(principal_cents, monthly)
        for i in range(periods):
            interest = _round_cents(Decimal(balance) * monthly[i])
            if i < periods - 1:
                principal = payment - interest
                if principal <= 0:
                    raise InvalidRequestError("参数导致月供无法覆盖利息")
            else:
                # 最后一期：本金兜底，吸收全部累计尾差
                principal = balance
            entries.append(ScheduleEntry(
                period=start_period + i,
                due_date=_add_months(first_due_date, i),
                principal_cents=principal,
                interest_cents=interest,
                rate=rates[i],
            ))
            balance -= principal
    else:  # EQUAL_PRINCIPAL
        base = principal_cents // periods
        for i in range(periods):
            interest = _round_cents(Decimal(balance) * monthly[i])
            if i < periods - 1:
                principal = base
            else:
                principal = balance  # 尾差归入最后一期
            entries.append(ScheduleEntry(
                period=start_period + i,
                due_date=_add_months(first_due_date, i),
                principal_cents=principal,
                interest_cents=interest,
                rate=rates[i],
            ))
            balance -= principal

    assert balance == 0, f"累计本金必须等于贷款本金，剩余 {balance}"
    return entries


def _solve_installment(principal_cents: int, monthly: list[Decimal]) -> int:
    """逐期月利率下求等额月供（分，ROUND_HALF_UP）。

    等额本息满足：balance_n = 0，其中
    balance_k = balance_{k-1} * (1 + r_k) - payment。
    展开后 payment = P * A / B，A、B 为由各期利率决定的系数。
    """
    n = len(monthly)
    # balance_n = P * prod(1+r_i) - payment * sum_k prod_{i>k}(1+r_i)
    a = Decimal(1)  # prod(1+r_i)
    for r in monthly:
        a *= 1 + r
    # suffix product after period k: prod_{i>k}(1+r_i)
    suffixes = [Decimal(1)] * n
    for k in range(n - 2, -1, -1):
        suffixes[k] = suffixes[k + 1] * (1 + monthly[k + 1])
    b = sum(suffixes, Decimal(0))
    if b == 0:
        return 0
    payment = Decimal(principal_cents) * a / b
    return _round_cents(payment)


def generate_schedule(
    principal_cents: int,
    annual_rate: float,
    periods: int,
    method: RepaymentMethod,
    start_date: date,
) -> list[ScheduleEntry]:
    """生成完整摊还计划，首期到期日为起借日后一个月。"""
    return _build(principal_cents, [annual_rate] * periods, periods, method,
                  first_due_date=_add_months(start_date, 1), start_period=1)


def recalc_schedule(
    remaining_principal_cents: int,
    annual_rate: float,
    remaining_periods: int,
    method: RepaymentMethod,
    next_due_date: date,
    start_period: int,
) -> list[ScheduleEntry]:
    """利率调整后，对剩余本金按剩余期数以单一利率重算。"""
    return _build(remaining_principal_cents, [annual_rate] * remaining_periods,
                  remaining_periods, method, first_due_date=next_due_date,
                  start_period=start_period)


def rebuild_with_rates(
    principal_cents: int,
    rates: list[float],
    method: RepaymentMethod,
    first_due_date: date,
    start_period: int,
) -> list[ScheduleEntry]:
    """提前还款（降低月供）：期数与到期日不变，逐期保住原适用利率重摊。"""
    return _build(principal_cents, rates, len(rates), method,
                  first_due_date=first_due_date, start_period=start_period)


def shortened_schedule(
    principal_cents: int,
    rates: list[float],
    method: RepaymentMethod,
    first_due_date: date,
    start_period: int,
    keep_cents: int,
) -> list[ScheduleEntry]:
    """提前还款（缩短期限）：月供/每期本金基数不变，期数缩短。

    keep_cents：等额本息为要保持的月供（分），等额本金为每期本金基数（分）。
    逐期利率沿用被替换期次的原适用利率（期数只会减少不会增加，
    因此 rates 必然够用；防御性地复用最后一个利率）。
    到期日从 first_due_date 起逐月连续推进，不产生空期或重复期。
    """
    if principal_cents <= 0:
        raise InvalidRequestError("本金必须为正整数分")
    if keep_cents <= 0:
        raise InvalidRequestError("保持金额必须为正整数分")

    def rate_at(i: int) -> float:
        return rates[min(i, len(rates) - 1)]

    entries: list[ScheduleEntry] = []
    balance = principal_cents
    i = 0
    while balance > 0:
        r = Decimal(str(rate_at(i))) / 12
        interest = _round_cents(Decimal(balance) * r)
        if method is RepaymentMethod.EQUAL_INSTALLMENT:
            if balance + interest <= keep_cents:
                principal = balance  # 末期：应还不超过月供，兜底结清
            else:
                principal = keep_cents - interest
                if principal <= 0:
                    raise InvalidRequestError("月供不足以覆盖利息，无法缩短期限")
        else:  # EQUAL_PRINCIPAL：每期本金基数不变
            principal = min(keep_cents, balance)
        entries.append(ScheduleEntry(
            period=start_period + i,
            due_date=_add_months(first_due_date, i),
            principal_cents=principal,
            interest_cents=interest,
            rate=rate_at(i),
        ))
        balance -= principal
        i += 1
    return entries
