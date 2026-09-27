"""摊还计划：等额本息 / 等额本金生成、期间利率调整、提前还款后重算。

守恒约定（本模块严格满足，测试逐条断言）：
1. 每期 ``principal + interest`` 即该期应还总额（结构恒等）；
2. ``sum(installment.principal) == 贷款本金``；
3. 所有金额逐期量化到分（ROUND_HALF_UP），尾差一律并入最后一期本金
   （等额本息）或最后一个本金段（等额本金），不存在无法解释的尾差；
4. 提前还款重算后：已发生期原样保留（含已还金额），未来期本金之和
   恰好等于提前还款后的剩余本金。

利率口径：
- 月利率 = 年利率 / 12（计算过程保留 Decimal 高精度，仅利息结果量化到分）；
- 期间利率调整按到期日归属：某期 due_date >= 生效日即适用新利率；
- 等额本息在每个利率段的首期，以"剩余本金/剩余期数/新月利率"重算年金月供；
- 等额本金每期本金只取决于本金与期数，利率调整仅改变各期利息。
"""

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Callable, Sequence

from .errors import InvalidScheduleError, RateAdjustmentError
from .models import AmortizationType, Installment, LoanTerms, RateChange
from .money import quantize


class PrepayMode(str, Enum):
    """提前还款后重算方式。"""

    KEEP_TERM = "keep_term"        # 期数不变，减少每期月供
    KEEP_PAYMENT = "keep_payment"  # 月供（或每期本金段）不变，缩短期数


@dataclass(frozen=True)
class Schedule:
    """一份摊还计划。"""

    terms: LoanTerms
    installments: tuple[Installment, ...]
    rate_changes: tuple[RateChange, ...] = ()

    @property
    def total_principal(self) -> Decimal:
        return sum((i.principal for i in self.installments), Decimal("0"))

    @property
    def total_interest(self) -> Decimal:
        return sum((i.interest for i in self.installments), Decimal("0"))


def add_months(d: date, months: int) -> date:
    """日期加若干整月（目标月无对应日时取月末，如 3/31 +1 月 = 4/30）。"""
    total_index = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total_index, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


# ---------------- 校验 ----------------

def _validate_terms(terms: LoanTerms) -> None:
    if terms.principal <= 0:
        raise InvalidScheduleError(f"贷款本金必须大于 0: {terms.principal}")
    if terms.term_months <= 0:
        raise InvalidScheduleError(f"贷款期数必须大于 0: {terms.term_months}")
    if terms.annual_rate < 0:
        raise InvalidScheduleError(f"年利率不能为负: {terms.annual_rate}")
    if terms.grace_period_days < 0:
        raise InvalidScheduleError(f"宽限期不能为负: {terms.grace_period_days}")
    if terms.penalty_daily_rate < 0:
        raise InvalidScheduleError("罚息日利率不能为负")
    if terms.fee_rate < 0:
        raise InvalidScheduleError("逾期管理费率不能为负")


def _normalize_changes(
    terms: LoanTerms,
    rate_changes: Sequence[RateChange],
    last_due: date,
) -> list[RateChange]:
    changes = sorted(rate_changes, key=lambda c: c.effective_date)
    prev_date: date | None = None
    for c in changes:
        if c.annual_rate < 0:
            raise RateAdjustmentError(f"利率调整后利率不能为负: {c.annual_rate}")
        if c.effective_date < terms.origination_date:
            raise RateAdjustmentError(
                f"利率调整生效日 {c.effective_date} 早于放款日 "
                f"{terms.origination_date}"
            )
        if c.effective_date > last_due:
            raise RateAdjustmentError(
                f"利率调整生效日 {c.effective_date} 晚于末期到期日 {last_due}，"
                "不影响任何一期"
            )
        if prev_date is not None and c.effective_date == prev_date:
            raise RateAdjustmentError(f"同一天存在多笔利率调整: {c.effective_date}")
        prev_date = c.effective_date
    return changes


# ---------------- 核心滚动计算 ----------------

def _annuity(balance: Decimal, monthly_rate: Decimal, n: int) -> Decimal:
    """等额本息年金月供（高精度，未量化）；r=0 时退化为本金/期数。"""
    if monthly_rate == 0:
        return balance / Decimal(n)
    growth = (Decimal(1) + monthly_rate) ** n
    return balance * monthly_rate * growth / (growth - Decimal(1))


def _rate_fn(
    terms: LoanTerms, changes: Sequence[RateChange]
) -> Callable[[date], Decimal]:
    """返回 due_date -> 适用年利率 的查找函数。"""

    def rate_on(due: date) -> Decimal:
        rate = terms.annual_rate
        for c in changes:
            if c.effective_date <= due:
                rate = c.annual_rate
            else:
                break
        return rate

    return rate_on


def _split_principal(balance: Decimal, n: int) -> list[Decimal]:
    """等额本金本金段：每期量化到分，差额并入最后一段，保证合计 == balance。"""
    each = quantize(balance / Decimal(n))
    plan = [each] * n
    plan[-1] = balance - each * Decimal(n - 1)
    return plan


def _roll_fixed_dates(
    balance0: Decimal,
    first_period_no: int,
    due_dates: Sequence[date],
    rate_on: Callable[[date], Decimal],
    amortization_type: AmortizationType,
) -> list[Installment]:
    """按固定到期日列表逐期滚动（build 与 KEEP_TERM 重算共用）。"""
    n = len(due_dates)
    principal_plan = (
        _split_principal(balance0, n)
        if amortization_type == AmortizationType.EQUAL_PRINCIPAL
        else None
    )
    out: list[Installment] = []
    balance = balance0
    payment = Decimal("0")
    prev_rate: Decimal | None = None
    for idx, due in enumerate(due_dates):
        monthly_rate = rate_on(due) / Decimal(12)
        interest = quantize(balance * monthly_rate)
        is_last = idx == n - 1
        if amortization_type == AmortizationType.EQUAL_PRINCIPAL:
            principal = principal_plan[idx]
        else:
            # 利率段切换（含首期）时按剩余本金/剩余期数重算年金月供
            if prev_rate is None or rate_on(due) != prev_rate:
                payment = quantize(
                    _annuity(balance, monthly_rate, n - idx)
                )
            principal = balance if is_last else payment - interest
        out.append(
            Installment(
                period=first_period_no + idx,
                due_date=due,
                principal=principal,
                interest=interest,
            )
        )
        balance -= principal
        prev_rate = rate_on(due)
    return out


def _roll_until_zero(
    balance0: Decimal,
    first_period_no: int,
    first_due: date,
    rate_on: Callable[[date], Decimal],
    amortization_type: AmortizationType,
    base_installment: Installment,
) -> list[Installment]:
    """KEEP_PAYMENT：沿用原月供（等额本息）或原本金段（等额本金）滚动至清偿。

    期数自然缩短，末期以剩余本金找平。利率在期间发生变化时利息随之变化，
    但偿还节奏保持不变（等额本息月供不变、等额本金每期本金段不变）。
    """
    out: list[Installment] = []
    balance = balance0
    fixed_payment = base_installment.principal + base_installment.interest
    fixed_segment = base_installment.principal
    idx = 0
    while balance > 0:
        due = add_months(first_due, idx)
        monthly_rate = rate_on(due) / Decimal(12)
        interest = quantize(balance * monthly_rate)
        is_last = False
        if amortization_type == AmortizationType.EQUAL_PRINCIPAL:
            principal = fixed_segment
        else:
            principal = fixed_payment - interest
        if principal <= 0:
            raise InvalidScheduleError(
                "KEEP_PAYMENT 重算失败：固定月供已不足以覆盖当期利息，"
                "请改用 KEEP_TERM（期数不变、重算月供）"
            )
        if principal >= balance:
            principal = balance
            is_last = True
        out.append(
            Installment(
                period=first_period_no + idx,
                due_date=due,
                principal=principal,
                interest=interest if not is_last else quantize(balance * monthly_rate),
            )
        )
        balance -= principal
        idx += 1
        if idx > 100000:  # 防御性安全阀，正常业务不可能触及
            raise InvalidScheduleError("提前还款重算期数异常发散")
    return out


# ---------------- 对外入口 ----------------

def build_schedule(
    terms: LoanTerms,
    rate_changes: Sequence[RateChange] = (),
) -> Schedule:
    """按条款生成摊还计划（支持期间利率调整）。"""
    _validate_terms(terms)
    due_dates = [
        add_months(terms.origination_date, i) for i in range(1, terms.term_months + 1)
    ]
    changes = tuple(_normalize_changes(terms, rate_changes, due_dates[-1]))
    installments = _roll_fixed_dates(
        terms.principal,
        1,
        due_dates,
        _rate_fn(terms, changes),
        terms.amortization_type,
    )
    return Schedule(terms=terms, installments=tuple(installments), rate_changes=changes)


def rebuild_after_prepayment(
    terms: LoanTerms,
    current: Sequence[Installment],
    rate_changes: Sequence[RateChange],
    prepay_date: date,
    prepay_amount: Decimal,
    mode: PrepayMode,
) -> Schedule:
    """提前还款后基于剩余本金重算后续计划。

    - due_date <= prepay_date 的期视为已发生，原样保留（含已还/计提数据）；
    - 提前还款额只冲减未到期本金，必须满足 0 < amount <= 未到期剩余本金；
    - KEEP_TERM：未来期数与到期日不变，逐段重算月供；
    - KEEP_PAYMENT：维持偿还节奏，期数缩短。
    """
    if prepay_amount <= 0:
        raise InvalidScheduleError(f"提前还款金额必须大于 0: {prepay_amount}")
    ordered = sorted(current, key=lambda i: i.period)
    past = [i for i in ordered if i.due_date <= prepay_date]
    future = [i for i in ordered if i.due_date > prepay_date]
    if not future:
        raise InvalidScheduleError(
            f"提前还款日 {prepay_date} 之后已无未到期期数，无需重算"
        )
    unpaid_future = sum((i.unpaid_principal for i in future), Decimal("0"))
    if prepay_amount > unpaid_future:
        raise InvalidScheduleError(
            f"提前还款额 {prepay_amount} 超过未到期剩余本金 {unpaid_future}"
        )
    remaining_balance = unpaid_future - prepay_amount
    changes = tuple(sorted(rate_changes, key=lambda c: c.effective_date))
    rate_on = _rate_fn(terms, changes)

    if remaining_balance == 0:
        new_installments: list[Installment] = []
    elif mode == PrepayMode.KEEP_TERM:
        new_installments = _roll_fixed_dates(
            remaining_balance,
            future[0].period,
            [i.due_date for i in future],
            rate_on,
            terms.amortization_type,
        )
    else:
        new_installments = _roll_until_zero(
            remaining_balance,
            future[0].period,
            future[0].due_date,
            rate_on,
            terms.amortization_type,
            base_installment=future[0],
        )
    return Schedule(
        terms=terms,
        installments=tuple(past + new_installments),
        rate_changes=changes,
    )
