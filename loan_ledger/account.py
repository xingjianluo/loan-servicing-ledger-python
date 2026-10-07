"""贷款账户：还款入账、提前还款申请、幂等、逾期判定与状态迁移。

普通还款分配优先级（稳定且明确）：
罚息 -> 费用 -> 已到期利息 -> 已到期本金；同一科目内部按期号 FIFO。
普通还款金额超过“当前到期应还”即拒绝（ExceedsDueAmountError），
不会自动转成提前还款；提前还款必须走独立申请入口 apply_prepayment。

提前还款申请：直接冲减剩余本金，并按用户选择重摊未来期次——
REDUCE_TERM（月供不变、缩短期数）或 REDUCE_INSTALLMENT（期数不变、
降低月供）。重算逐期保住已生效的调息结果（适用利率不回退）。
利率调整：仅作用于完全未还的未来期次，当期已部分还款的期次保持原金额。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from .errors import (
    AccountSettledError,
    ExceedsDueAmountError,
    ExcessPaymentError,
    InvalidAmountError,
    InvalidRequestError,
)
from .models import (
    AccountStatus,
    Allocation,
    EntryStatus,
    LoanConfig,
    PaymentReceipt,
    PrepaymentMode,
    PrepaymentReceipt,
    RepaymentMethod,
    ScheduleEntry,
    StatusChange,
)
from .schedule import (
    generate_schedule,
    rebuild_with_rates,
    recalc_schedule,
    shortened_schedule,
)


class LoanAccount:
    """单借款账户账本，线程安全（内存锁）。"""

    def __init__(self, config: LoanConfig) -> None:
        if config.principal_cents <= 0:
            raise InvalidRequestError("本金必须为正整数分")
        self.config = config
        self.status = AccountStatus.ACTIVE
        self.schedule: list[ScheduleEntry] = generate_schedule(
            config.principal_cents, config.annual_rate, config.periods,
            config.method, config.start_date,
        )
        self.status_history: list[StatusChange] = []
        self._receipts: dict[str, PaymentReceipt] = {}
        self._prepay_receipts: dict[str, PrepaymentReceipt] = {}
        self._lock = threading.Lock()
        # 罚息与费用科目（分）
        self._penalty_accrued = 0
        self._penalty_paid = 0
        self._fee_charged = 0
        self._fee_paid = 0
        self._last_accrual: date = config.start_date

    # ---------- 内部：逾期与罚息 ----------

    def _entry_unpaid_total(self, e: ScheduleEntry) -> int:
        return (e.principal_cents - e.paid_principal_cents
                + e.interest_cents - e.paid_interest_cents)

    def _accrue_penalty(self, as_of: date) -> None:
        """逐天计提罚息：每天对当日“已过宽限期未还金额”按日利率计息。

        逐天推进可正确处理宽限期跨越：期次在到期日+宽限期次日才进入基数；
        部分还款后基数随之下降，已计提部分不受影响。
        """
        if as_of <= self._last_accrual:
            return
        from decimal import Decimal, ROUND_HALF_UP
        grace = timedelta(days=self.config.grace_days)
        rate = Decimal(str(self.config.penalty_daily_rate))
        accrued = Decimal(0)
        d = self._last_accrual + timedelta(days=1)
        while d <= as_of:
            base = sum(
                unpaid for e in self.schedule
                if (unpaid := self._entry_unpaid_total(e)) > 0
                and d > e.due_date + grace
            )
            if base > 0:
                accrued += Decimal(base) * rate
            d += timedelta(days=1)
        self._penalty_accrued += int(
            accrued.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        self._last_accrual = as_of

    def _refresh_entries(self, as_of: date) -> None:
        grace = timedelta(days=self.config.grace_days)
        for e in self.schedule:
            if self._entry_unpaid_total(e) == 0:
                e.status = EntryStatus.PAID
            elif as_of < e.due_date:
                e.status = EntryStatus.PENDING
            elif as_of <= e.due_date + grace:
                e.status = EntryStatus.GRACE
            else:
                e.status = EntryStatus.OVERDUE

    def _set_status(self, new: AccountStatus, as_of: date, reason: str) -> None:
        if new is not self.status:
            self.status_history.append(StatusChange(
                changed_at=as_of, from_status=self.status,
                to_status=new, reason=reason))
            self.status = new

    def refresh_status(self, as_of: date) -> AccountStatus:
        """按给定日期计提罚息并迁移账户状态，迁移记录可追溯。"""
        with self._lock:
            self._accrue_penalty(as_of)
            self._refresh_entries(as_of)
            if self._is_settled():
                self._set_status(AccountStatus.SETTLED, as_of, "全部应还已结清")
            elif any(e.status is EntryStatus.OVERDUE for e in self.schedule):
                self._set_status(AccountStatus.OVERDUE, as_of,
                                 "存在超过宽限期未足额还款的期次")
            else:
                self._set_status(AccountStatus.ACTIVE, as_of, "无逾期期次")
            return self.status

    def _is_settled(self) -> bool:
        return (all(self._entry_unpaid_total(e) == 0 for e in self.schedule)
                and self._penalty_accrued == self._penalty_paid
                and self._fee_charged == self._fee_paid)

    # ---------- 内部：应还范围 ----------

    def _outstanding(self) -> int:
        """当前全部未还金额（罚息+费用+利息+本金）。"""
        return (
            (self._penalty_accrued - self._penalty_paid)
            + (self._fee_charged - self._fee_paid)
            + sum(self._entry_unpaid_total(e) for e in self.schedule)
        )

    def _due_amount(self, as_of: date) -> int:
        """当前到期应还：罚息 + 费用 + 到期日 <= as_of 期次的未还金额。"""
        return (
            (self._penalty_accrued - self._penalty_paid)
            + (self._fee_charged - self._fee_paid)
            + sum(self._entry_unpaid_total(e) for e in self.schedule
                  if e.due_date <= as_of)
        )

    def _remaining_principal(self) -> int:
        """真实剩余本金：各期未还本金合计。"""
        return sum(e.principal_cents - e.paid_principal_cents
                   for e in self.schedule)

    # ---------- 对外：费用 ----------

    def charge_fee(self, amount_cents: int) -> None:
        """计收一笔费用（如服务费），进入费用科目等待还款分配。"""
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise InvalidAmountError("费用金额必须为正整数分")
        with self._lock:
            self._fee_charged += amount_cents

    # ---------- 对外：还款入账 ----------

    def post_payment(self, request_id: str, amount_cents: int,
                     pay_date: date) -> PaymentReceipt:
        """还款入账。同一 request_id 重复/并发提交幂等，返回首次回执。"""
        if not request_id:
            raise InvalidRequestError("request_id 不能为空")
        with self._lock:
            # 幂等：重复或并发提交直接重放首次结果，不重复扣减
            if request_id in self._receipts:
                first = self._receipts[request_id]
                return PaymentReceipt(
                    request_id=first.request_id,
                    amount_cents=first.amount_cents,
                    allocation=first.allocation,
                    status_after=first.status_after,
                    duplicate=True,
                )
            if not isinstance(amount_cents, int) or amount_cents <= 0:
                raise InvalidAmountError("还款金额必须为正整数分")

            self._accrue_penalty(pay_date)
            self._refresh_entries(pay_date)
            if self._is_settled():
                self._set_status(AccountStatus.SETTLED, pay_date,
                                 "全部应还已结清")
                raise AccountSettledError("账户已结清，拒绝再次入账")

            outstanding = self._outstanding()
            if amount_cents > outstanding:
                raise ExcessPaymentError(
                    f"还款金额 {amount_cents} 超出当前全部应还 {outstanding}")
            due = self._due_amount(pay_date)
            if amount_cents > due:
                raise ExceedsDueAmountError(
                    f"还款金额 {amount_cents} 超过当前到期应还 {due}，"
                    f"提前还款请使用提前还款申请入口")

            allocation = self._allocate(amount_cents, pay_date)
            receipt = PaymentReceipt(
                request_id=request_id,
                amount_cents=amount_cents,
                allocation=allocation,
                status_after=self.status,
            )
            self._receipts[request_id] = receipt

            self._refresh_entries(pay_date)
            if self._is_settled():
                self._set_status(AccountStatus.SETTLED, pay_date,
                                 "全部应还已结清")
            receipt.status_after = self.status
            return receipt

    def _allocate(self, amount_cents: int, pay_date: date) -> Allocation:
        """分配顺序：罚息 -> 费用 -> 已到期利息 -> 已到期本金。

        普通还款只允许覆盖“当前到期应还”（入口已校验），
        因此分配结束时剩余金额必为 0；提前还款不走这里。
        """
        alloc = Allocation()
        remaining = amount_cents

        # 1. 罚息
        due = self._penalty_accrued - self._penalty_paid
        pay = min(due, remaining)
        self._penalty_paid += pay
        alloc.penalty_cents = pay
        remaining -= pay

        # 2. 费用
        due = self._fee_charged - self._fee_paid
        pay = min(due, remaining)
        self._fee_paid += pay
        alloc.fee_cents = pay
        remaining -= pay

        due_entries = [e for e in self.schedule if e.due_date <= pay_date]

        # 3. 已到期利息（期号升序）
        for e in due_entries:
            if remaining <= 0:
                break
            due = e.interest_cents - e.paid_interest_cents
            pay = min(due, remaining)
            e.paid_interest_cents += pay
            alloc.interest_cents += pay
            remaining -= pay

        # 4. 已到期本金（期号升序）
        for e in due_entries:
            if remaining <= 0:
                break
            due = e.principal_cents - e.paid_principal_cents
            pay = min(due, remaining)
            e.paid_principal_cents += pay
            alloc.principal_cents += pay
            remaining -= pay

        assert remaining == 0, \
            f"普通还款不应有超出到期应还的余额: {remaining}"
        assert alloc.total_cents == amount_cents, \
            f"分配守恒被破坏: {alloc.total_cents} != {amount_cents}"
        return alloc

    def _target_indices(self, ref_date: date) -> list[int]:
        """未到期、完全未还的期次下标（重算作用域）。"""
        return [i for i, e in enumerate(self.schedule)
                if e.due_date > ref_date
                and self._entry_unpaid_total(e) > 0
                and e.paid_principal_cents == 0
                and e.paid_interest_cents == 0]

    # ---------- 对外：重算 ----------

    def _recalc(self, target_idx: list[int], base_principal: int,
                new_rate: float | None = None) -> None:
        """把指定期次按给定剩余本金重摊（期号、到期日不变）。"""
        if base_principal <= 0:
            return
        first = self.schedule[target_idx[0]]
        rate = new_rate if new_rate is not None else first.rate
        rebuilt = recalc_schedule(
            base_principal, rate, len(target_idx), self.config.method,
            next_due_date=first.due_date, start_period=first.period)
        for i, new_entry in zip(target_idx, rebuilt):
            self.schedule[i] = new_entry

    def _recalc_future(self, ref_date: date,
                       new_rate: float | None = None) -> None:
        """利率调整/重算：用账户实际剩余本金重摊未到期、完全未还的期次。"""
        target_idx = self._target_indices(ref_date)
        if not target_idx:
            return
        total_unpaid = sum(e.principal_cents - e.paid_principal_cents
                           for e in self.schedule)
        excluded_unpaid = sum(
            e.principal_cents - e.paid_principal_cents
            for i, e in enumerate(self.schedule) if i not in target_idx)
        self._recalc(target_idx, total_unpaid - excluded_unpaid, new_rate)

    def adjust_rate(self, new_annual_rate: float,
                    effective_date: date) -> None:
        """期间利率调整：仅重算完全未还的未来期次。"""
        if new_annual_rate < 0:
            raise InvalidRequestError("利率不能为负")
        with self._lock:
            self._accrue_penalty(effective_date)
            self._recalc_future(effective_date, new_rate=new_annual_rate)

    # ---------- 对外：提前还款申请 ----------

    def apply_prepayment(self, request_id: str, amount_cents: int,
                         apply_date: date,
                         mode: PrepaymentMode) -> PrepaymentReceipt:
        """提前还款申请（独立入口，幂等）。

        直接冲减当前真实剩余本金，并按 mode 重摊“未到期且完全未还”的期次：
        - REDUCE_TERM：月供不变、缩短期数，到期日逐月连续、不留空期；
        - REDUCE_INSTALLMENT：期数与到期日不变、降低月供。
        重算逐期沿用各期原适用利率，已生效的调息结果不会回退。
        同一 request_id 重复/并发提交只入账一次，重放首次回执。
        """
        if not request_id:
            raise InvalidRequestError("request_id 不能为空")
        try:
            mode = PrepaymentMode(mode)
        except ValueError:
            raise InvalidRequestError(f"非法的提前还款方式: {mode}") from None
        with self._lock:
            # 幂等：重复或并发提交直接重放首次结果，余额与期次不重复扣减
            if request_id in self._prepay_receipts:
                first = self._prepay_receipts[request_id]
                return PrepaymentReceipt(
                    request_id=first.request_id,
                    amount_cents=first.amount_cents,
                    mode=first.mode,
                    principal_before_cents=first.principal_before_cents,
                    principal_after_cents=first.principal_after_cents,
                    periods_before=first.periods_before,
                    periods_after=first.periods_after,
                    duplicate=True,
                )
            if not isinstance(amount_cents, int) or amount_cents <= 0:
                raise InvalidAmountError("提前还款金额必须为正整数分")

            self._accrue_penalty(apply_date)
            self._refresh_entries(apply_date)
            if self._is_settled():
                self._set_status(AccountStatus.SETTLED, apply_date,
                                 "全部应还已结清")
                raise AccountSettledError("账户已结清，拒绝提前还款申请")

            principal_before = self._remaining_principal()
            if amount_cents > principal_before:
                raise ExcessPaymentError(
                    f"提前还款金额 {amount_cents} 超出剩余本金 "
                    f"{principal_before}")
            periods_before = sum(
                1 for e in self.schedule if self._entry_unpaid_total(e) > 0)

            self._apply_prepay_principal(amount_cents, apply_date, mode)

            self._refresh_entries(apply_date)
            if self._is_settled():
                self._set_status(AccountStatus.SETTLED, apply_date,
                                 "全部应还已结清")
            receipt = PrepaymentReceipt(
                request_id=request_id,
                amount_cents=amount_cents,
                mode=mode,
                principal_before_cents=principal_before,
                principal_after_cents=self._remaining_principal(),
                periods_before=periods_before,
                periods_after=sum(1 for e in self.schedule
                                  if self._entry_unpaid_total(e) > 0),
            )
            self._prepay_receipts[request_id] = receipt
            return receipt

    def _apply_prepay_principal(self, amount_cents: int, apply_date: date,
                                mode: PrepaymentMode) -> None:
        """冲减本金并重摊未来期次（逐期保住原适用利率）。"""
        target_idx = self._target_indices(apply_date)
        target_principal = sum(
            self.schedule[i].principal_cents for i in target_idx)
        rest = amount_cents
        if target_idx:
            # 重算作用域必为计划表后缀（未来期次不可能被部分还款）
            assert target_idx == list(range(target_idx[0],
                                            len(self.schedule)))
            first = self.schedule[target_idx[0]]
            if rest >= target_principal:
                # 未来期次本金被全部冲掉：整段删除
                del self.schedule[target_idx[0]:]
                rest -= target_principal
            else:
                rates = [self.schedule[i].rate for i in target_idx]
                new_base = target_principal - rest
                if mode is PrepaymentMode.REDUCE_INSTALLMENT:
                    rebuilt = rebuild_with_rates(
                        new_base, rates, self.config.method,
                        first_due_date=first.due_date,
                        start_period=first.period)
                else:  # REDUCE_TERM：月供/每期本金基数不变，期数缩短
                    keep = (first.total_cents if self.config.method
                            is RepaymentMethod.EQUAL_INSTALLMENT
                            else first.principal_cents)
                    rebuilt = shortened_schedule(
                        new_base, rates, self.config.method,
                        first_due_date=first.due_date,
                        start_period=first.period,
                        keep_cents=keep)
                self.schedule[target_idx[0]:] = rebuilt
                rest = 0
        # 兜底：剩余部分冲减已到期/逾期期次的未还本金（期号从大到小）
        if rest > 0:
            for e in reversed(self.schedule):
                if rest <= 0:
                    break
                unpaid = e.principal_cents - e.paid_principal_cents
                cut = min(unpaid, rest)
                e.principal_cents -= cut
                rest -= cut
        assert rest == 0, "提前还款本金冲减后不应有剩余"

    # ---------- 对外：查询 ----------

    def balances(self) -> dict[str, int]:
        """当前各科目余额（分），用于测试与对账。"""
        return {
            "penalty": self._penalty_accrued - self._penalty_paid,
            "fee": self._fee_charged - self._fee_paid,
            "interest": sum(e.interest_cents - e.paid_interest_cents
                            for e in self.schedule),
            "principal": sum(e.principal_cents - e.paid_principal_cents
                             for e in self.schedule),
        }
