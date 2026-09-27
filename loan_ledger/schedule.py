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
    annual_rate: float,
    periods: int,
    method: RepaymentMethod,
    first_due_date: date,
    start_period: int,
) -> list[ScheduleEntry]:
    if principal_cents <= 0:
        raise InvalidRequestError("本金必须为正整数分")
    if periods <= 0:
        raise InvalidRequestError("期数必须为正整数")
    if annual_rate < 0:
        raise InvalidRequestError("利率不能为负")

    r = Decimal(str(annual_rate)) / 12  # 月利率
    balance = principal_cents
    entries: list[ScheduleEntry] = []

    if method is RepaymentMethod.EQUAL_INSTALLMENT:
        if r == 0:
            payment = Decimal(principal_cents) / periods
        else:
            factor = (1 + r) ** periods
            payment = Decimal(principal_cents) * r * factor / (factor - 1)
        payment_cents = _round_cents(payment)
        for i in range(periods):
            interest = _round_cents(Decimal(balance) * r)
            if i < periods - 1:
                principal = payment_cents - interest
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
                rate=annual_rate,
            ))
            balance -= principal
    else:  # EQUAL_PRINCIPAL
        base = principal_cents // periods
        for i in range(periods):
            interest = _round_cents(Decimal(balance) * r)
            if i < periods - 1:
                principal = base
            else:
                principal = balance  # 尾差归入最后一期
            entries.append(ScheduleEntry(
                period=start_period + i,
                due_date=_add_months(first_due_date, i),
                principal_cents=principal,
                interest_cents=interest,
                rate=annual_rate,
            ))
            balance -= principal

    assert balance == 0, f"累计本金必须等于贷款本金，剩余 {balance}"
    return entries


def generate_schedule(
    principal_cents: int,
    annual_rate: float,
    periods: int,
    method: RepaymentMethod,
    start_date: date,
) -> list[ScheduleEntry]:
    """生成完整摊还计划，首期到期日为起借日后一个月。"""
    return _build(principal_cents, annual_rate, periods, method,
                  first_due_date=_add_months(start_date, 1), start_period=1)


def recalc_schedule(
    remaining_principal_cents: int,
    annual_rate: float,
    remaining_periods: int,
    method: RepaymentMethod,
    next_due_date: date,
    start_period: int,
) -> list[ScheduleEntry]:
    """利率调整或提前还款后，对剩余本金按剩余期数重算。"""
    return _build(remaining_principal_cents, annual_rate, remaining_periods,
                  method, first_due_date=next_due_date,
                  start_period=start_period)
