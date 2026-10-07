"""摊还计划生成与重算。

金额一律为整数“分”。守恒约束：
- 每期 本金+利息 = 本期应还总额（由构造保证）；
- 累计本金 = 贷款本金（尾差归入最后一期本金）；
- 利息按剩余本金 * 月利率 四舍五入（ROUND_HALF_UP）到分。

提前还款重算的两种方式：
- ``build_schedule``（期数不变、降低月供）：按期次给定的固定利率序列摊还，
  每个“同利率段”以该段年金月供摊还，末期吸收全部累计尾差；
- ``shorten_schedule``（月供不变、缩短期数）：沿用原每期应还总额（等额本息）
  或原每期本金份额（等额本金）逐期冲减，求能够覆盖剩余本金的最短期数，
  到期日序列为原序列的前缀（不产生空期/重复期）。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from .errors import InvalidRequestError
from .models import RepaymentMethod, ScheduleEntry

_CENT = Decimal(1)


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
    due_dates = [_add_months(first_due_date, i) for i in range(periods)]
    return build_schedule(
        principal_cents, [annual_rate] * periods, method,
        due_dates, start_period)


def _interest(balance_cents: int, annual_rate: float) -> int:
    r = Decimal(str(annual_rate)) / 12
    return int((Decimal(balance_cents) * r)
               .quantize(_CENT, rounding=ROUND_HALF_UP))


def _annuity_payment(balance_cents: int, annual_rate: float,
                     periods: int) -> Decimal:
    """等额本息年金月供（未舍入的 Decimal）。"""
    r = Decimal(str(annual_rate)) / 12
    if r == 0:
        return Decimal(balance_cents) / periods
    factor = (1 + r) ** periods
    return Decimal(balance_cents) * r * factor / (factor - 1)


def _rate_segments(rates: Sequence[float]) -> list[tuple[int, int, float]]:
    """把逐期利率序列切成同利率段 [(起始下标, 结束下标, 利率)]。"""
    segments: list[tuple[int, int, float]] = []
    start = 0
    for i in range(1, len(rates) + 1):
        if i == len(rates) or rates[i] != rates[start]:
            segments.append((start, i, rates[start]))
            start = i
    return segments


def _trial_payment(principal_cents: int, payment: int,
                   rates: Sequence[float]) -> bool:
    """试算固定月供是否恰好在全部期次内摊平。

    要求：
    - 每个非末期（前 n-1 期）偿还正本金，且走完后余额仍为正（不能提前结清）；
    - 非末期走完后的剩余余额不超过一期月供（末期兜底才能结清，
      末期总额不会相对月供产生巨额跳变）。
    可行月供构成一个连续区间，取其上界（=最大可行月供）即可得到
    与理论年金最接近、末期最小的摊还。
    """
    balance = principal_cents
    for j in range(len(rates) - 1):
        interest = _interest(balance, rates[j])
        principal = payment - interest
        if principal <= 0:
            return False
        balance -= principal
        if balance <= 0:
            return False
    return balance <= payment


def _level_payment(principal_cents: int,
                    rates: Sequence[float]) -> Decimal:
    """跨利率段恒定月供的理论值（未舍入 Decimal）。

    逐期按当期月利率计息（单利），令本金 = 各期月供折现之和：
    P = A * Σ_{j=1..n} 1 / Π_{k=1..j}(1 + r_k)，解出 A。
    """
    discount_factor = Decimal(1)
    total = Decimal(0)
    for rate in rates:
        discount_factor /= (1 + Decimal(str(rate)) / 12)
        total += discount_factor
    return Decimal(principal_cents) / total


def _solve_payment(principal_cents: int,
                   rates: Sequence[float]) -> int:
    """求跨利率段恒定的等额本息月供（整数分）。

    末期兜底吸收尾差。可行月供构成连续区间：低于下限时连利息都覆盖不住，
    高于上限时会在非末期提前结清。先由折现公式得到理论月供，再在其±2 分
    邻域内取可行的整数月供；理论值本身即严格可行（舍入误差 <= 0.5 分/期）。
    """
    def feasible(pay: int) -> bool:
        return pay > 0 and _trial_payment(principal_cents, pay, rates)

    raw = _level_payment(principal_cents, rates)
    anchor = int(raw.quantize(_CENT, rounding=ROUND_HALF_UP))
    for delta in range(5):
        for cand in (anchor - delta, anchor + delta):
            if feasible(cand):
                return cand
    raise InvalidRequestError("参数导致月供无法覆盖利息")


def build_schedule(
    principal_cents: int,
    rates: Sequence[float],
    method: RepaymentMethod,
    due_dates: Sequence[date],
    start_period: int,
) -> list[ScheduleEntry]:
    """按逐期固定利率序列摊还（期数 = 利率/到期日序列长度），降低月供方式。

    等额本息：整个序列内**月供恒定**（即便期间利率发生变化，已生效的未来
    期次按其固定利率计息，月供跨段保持一致），最后一期本金兜底吸收全部
    累计尾差。等额本金按期均分本金（末期兜底），利息逐期计算。
    """
    n = len(rates)
    if principal_cents <= 0:
        raise InvalidRequestError("本金必须为正整数分")
    if n <= 0:
        raise InvalidRequestError("期数必须为正整数")
    if n != len(due_dates):
        raise InvalidRequestError("利率序列与到期日序列长度必须一致")
    if any(r is None or r < 0 for r in rates):
        raise InvalidRequestError("利率不能为负或缺失")

    balance = principal_cents
    entries: list[ScheduleEntry] = []

    if method is RepaymentMethod.EQUAL_INSTALLMENT:
        payment = _solve_payment(balance, rates)
        for j, rate in enumerate(rates):
            interest = _interest(balance, rate)
            if j < n - 1:
                principal = payment - interest
            else:
                principal = balance  # 末期吸收全部累计尾差
            entries.append(ScheduleEntry(
                period=start_period + j,
                due_date=due_dates[j],
                principal_cents=principal,
                interest_cents=interest,
                rate=rate,
            ))
            balance -= principal
    else:  # EQUAL_PRINCIPAL
        base = principal_cents // n
        for j, rate in enumerate(rates):
            interest = _interest(balance, rate)
            if j < n - 1:
                principal = base
            else:
                principal = balance  # 尾差归入最后一期
            entries.append(ScheduleEntry(
                period=start_period + j,
                due_date=due_dates[j],
                principal_cents=principal,
                interest_cents=interest,
                rate=rate,
            ))
            balance -= principal

    assert balance == 0, f"累计本金必须等于贷款本金，剩余 {balance}"
    return entries


def shorten_schedule(
    remaining_principal_cents: int,
    rates: Sequence[float],
    method: RepaymentMethod,
    old_due_dates: Sequence[date],
    start_period: int,
    old_payment_cents: Sequence[int],
) -> list[ScheduleEntry]:
    """月供（等额本金：每期本金份额）不变，求覆盖剩余本金所需的最短期数。

    新到期日取 ``old_due_dates`` 的前缀，因此缩短后各期与原计划逐期衔接，
    不会产生空期或重复期。``old_payment_cents`` 为原计划各期应还总额
    （等额本息）或各期本金（等额本金），与利率序列等长。
    """
    n = len(rates)
    if remaining_principal_cents <= 0:
        raise InvalidRequestError("剩余本金必须为正整数分")
    if not (0 < n <= len(old_due_dates)) or n != len(old_payment_cents):
        raise InvalidRequestError("期数/到期日/月供序列长度非法")
    if any(r is None or r < 0 for r in rates):
        raise InvalidRequestError("利率不能为负或缺失")

    entries: list[ScheduleEntry] = []
    balance = remaining_principal_cents
    j = 0
    while j < n:
        interest = _interest(balance, rates[j])
        if method is RepaymentMethod.EQUAL_INSTALLMENT:
            payment = old_payment_cents[j]
            principal = payment - interest
            if principal <= 0:
                raise InvalidRequestError("参数导致月供无法覆盖利息")
            if balance - principal <= 0:
                principal = balance  # 末期兜底：本期固定月供足以结清
        else:  # EQUAL_PRINCIPAL
            principal = min(old_payment_cents[j], balance)
        entries.append(ScheduleEntry(
            period=start_period + j,
            due_date=old_due_dates[j],
            principal_cents=principal,
            interest_cents=interest,
            rate=rates[j],
        ))
        balance -= principal
        j += 1
        if balance <= 0:
            break

    new_count = len(entries)
    if new_count >= n:
        # 期数未严格变少（含“末期做小、期数不变”），不算缩短期限
        raise InvalidRequestError(
            "提前还款金额不足以缩短期数，请改用降低月供方式或增加金额")
    assert sum(e.principal_cents for e in entries) \
        == remaining_principal_cents, "缩短后本金合计必须等于剩余本金"
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
