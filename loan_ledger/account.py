"""贷款账户：普通还款、提前还款申请、幂等、逾期判定与状态迁移。

两类资金入口严格分开：
- **普通还款**（``post_payment``）：只能偿还“当前已到期应还”
  （罚息、费用、已到期利息/本金）。超过该金额直接拒绝，不会默认转提前还款；
  超过整笔未还总额是另一类拒绝原因。
- **提前还款申请**（``apply_prepayment``）：单独申请单号、单独幂等表，
  申请人选择处理方式：月供不变缩短期数（SHORTEN_TERM）或期数不变降低
  月供（REDUCE_PAYMENT）。提前还款只冲减未到期、完全未还期次的本金，
  可反复申请，每次都按当时真实剩余本金重算。

还款分配优先级（普通还款，稳定且明确）：
罚息 -> 费用 -> 已到期利息 -> 已到期本金；
同一科目内部按期号从小到大（FIFO）分配。

利率调整只作用于完全未还的未来期次，并把生效利率记入调息历史；
之后无论再做多少次提前还款、按什么顺序与调息穿插，每个未来期次都按其
到期日对应的“已生效利率”计息，重算不会把未来利息退回老利率。
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

from .errors import (
    AccountSettledError,
    InvalidAmountError,
    InvalidRequestError,
    PaymentExceedsDueError,
    PaymentExceedsTotalError,
)
from .models import (
    AccountStatus,
    Allocation,
    EntryStatus,
    LoanConfig,
    PaymentReceipt,
    PrepaymentMethod,
    PrepaymentReceipt,
    RateChange,
    RepaymentMethod,
    ScheduleEntry,
    StatusChange,
)
from .schedule import (
    build_schedule,
    generate_schedule,
    shorten_schedule,
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
        self._prepayment_receipts: dict[str, PrepaymentReceipt] = {}
        # 调息历史：首条为起借日的初始利率；后续按期次到期日匹配适用利率
        self.rate_history: list[RateChange] = [
            RateChange(effective_date=config.start_date,
                       annual_rate=config.annual_rate)]
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
        from decimal import ROUND_HALF_UP, Decimal
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
            accrued.quantize(Decimal(1), rounding=ROUND_HALF_UP))
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

    def _due_outstanding(self, pay_date: date) -> int:
        """当前“已到期应还”：罚息+费用+到期日 <= 还款日的期次未还额。

        普通还款的上限。未到期期次不包含在内——提前还款必须走
        独立的 ``apply_prepayment`` 入口。
        """
        return (
            (self._penalty_accrued - self._penalty_paid)
            + (self._fee_charged - self._fee_paid)
            + sum(self._entry_unpaid_total(e) for e in self.schedule
                  if e.due_date <= pay_date)
        )

    def _future_unpaid_principal(self, ref_date: date) -> int:
        """未到期、完全未还期次的本金合计（提前还款可用额度）。"""
        return sum(e.principal_cents for e in self.schedule
                   if e.due_date > ref_date
                   and e.paid_principal_cents == 0
                   and e.paid_interest_cents == 0)

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
                # 超过整笔贷款尚未还完的总额：与“超当期到期应还”区分
                raise PaymentExceedsTotalError(
                    f"金额 {amount_cents} 超出整笔未还总额 {outstanding}")
            due = self._due_outstanding(pay_date)
            if amount_cents > due:
                # 普通还款不允许跨期；多余的钱不会默认转提前还款
                raise PaymentExceedsDueError(
                    f"普通还款金额 {amount_cents} 超过当期到期应还 {due}；"
                    "提前还款请使用独立的提前还款申请入口")

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
        """普通还款分配顺序：罚息 -> 费用 -> 已到期利息 -> 已到期本金。

        利息/本金只分配到“已到期”（到期日 <= 还款日）的期次，期号升序；
        未到期期次不在普通还款中处理（提前还款走独立入口）。
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

        assert remaining == 0 and alloc.total_cents == amount_cents, \
            f"普通还款分配守恒被破坏: 剩余 {remaining}"
        return alloc

    def _target_indices(self, ref_date: date) -> list[int]:
        """未到期、完全未还的期次下标（重算/提前还款作用域）。"""
        return [i for i, e in enumerate(self.schedule)
                if e.due_date > ref_date
                and self._entry_unpaid_total(e) > 0
                and e.paid_principal_cents == 0
                and e.paid_interest_cents == 0]

    def _rate_for_due_date(self, due_date: date) -> float:
        """按期次到期日匹配已生效的调息利率（调息结果不会被重算回退）。"""
        applicable = self.config.annual_rate
        for change in self.rate_history:
            if change.effective_date <= due_date:
                applicable = change.annual_rate
            else:
                break
        return applicable

    # ---------- 对外：提前还款申请 ----------

    def apply_prepayment(
        self,
        request_id: str,
        amount_cents: int,
        method: PrepaymentMethod,
        apply_date: date,
    ) -> PrepaymentReceipt:
        """提前还款申请（独立入口、独立幂等）。

        ``amount_cents`` 为提前偿还的本金（分），必须为正整数且不超过
        未到期、完全未还期次的本金合计；申请按当时真实剩余本金立即重摊。
        - SHORTEN_TERM：月供/每期本金份额不变，缩短剩余期数，新到期日取
          原计划前缀（无空期/重复期）；
        - REDUCE_PAYMENT：期数与到期日不变，重新摊还降低月供。
        """
        if not request_id:
            raise InvalidRequestError("request_id 不能为空")
        with self._lock:
            if request_id in self._prepayment_receipts:
                first = self._prepayment_receipts[request_id]
                return PrepaymentReceipt(
                    request_id=first.request_id,
                    amount_cents=first.amount_cents,
                    method=first.method,
                    remaining_principal_cents=first.remaining_principal_cents,
                    remaining_periods=first.remaining_periods,
                    next_due_date=first.next_due_date,
                    status_after=first.status_after,
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

            capacity = self._future_unpaid_principal(apply_date)
            if capacity == 0:
                raise PaymentExceedsTotalError(
                    "没有可提前偿还的未到期本金（请先通过普通还款结清当期）")
            if amount_cents > capacity:
                raise PaymentExceedsTotalError(
                    f"提前还款金额 {amount_cents} 超出未到期剩余本金 "
                    f"{capacity}")

            target_idx = self._target_indices(apply_date)
            if amount_cents == capacity:
                self._apply_prepayment_full(target_idx, apply_date)
            elif method is PrepaymentMethod.SHORTEN_TERM:
                self._apply_prepayment_shorten(target_idx, amount_cents)
            else:
                self._apply_prepayment_reduce(target_idx, amount_cents)

            self._refresh_entries(apply_date)
            if self._is_settled():
                self._set_status(AccountStatus.SETTLED, apply_date,
                                 "提前还款后全部应还已结清")
            remaining_indices = self._target_indices(apply_date)
            receipt = PrepaymentReceipt(
                request_id=request_id,
                amount_cents=amount_cents,
                method=method,
                remaining_principal_cents=sum(
                    e.principal_cents - e.paid_principal_cents
                    for e in self.schedule),
                remaining_periods=len(remaining_indices),
                next_due_date=(self.schedule[remaining_indices[0]].due_date
                               if remaining_indices else None),
                status_after=self.status,
            )
            self._prepayment_receipts[request_id] = receipt
            return receipt

    def _apply_prepayment_full(self, target_idx: list[int],
                               apply_date: date) -> None:
        """提前还清：作用域期次全部移除，其未来利息随本金结清一并注销。

        已到期/部分还款期次不在作用域，仍保留在计划中由普通还款结清。
        """
        removed = set(target_idx)
        self.schedule = [e for i, e in enumerate(self.schedule)
                         if i not in removed]
        self._refresh_entries(apply_date)
        if self._is_settled():
            self._set_status(AccountStatus.SETTLED, apply_date,
                             "提前还款结清全部剩余本金")

    def _apply_prepayment_reduce(self, target_idx: list[int],
                                 pool: int) -> None:
        """期数不变、降低月供（余额法）。

        pool 即时冲减剩余本金；作用域内所有完全未还期次按原期数、原到期日
        与各期固定利率重摊，重摊后本金合计 = 原目标本金 - pool。
        """
        base = sum(self.schedule[i].principal_cents
                   for i in target_idx) - pool
        self._rebuild_targets(target_idx, base)

    def _apply_prepayment_shorten(self, target_idx: list[int],
                                  pool: int) -> None:
        """月供不变、缩短期数（余额法重排出最短期数）。

        作用域按减少后的本金、当前每期应还总额（等额本息）或本金份额
        （等额本金）重排，新到期日为原序列前缀；尾部不再需要的期次移除，
        不产生空期或重复期。
        """
        entries = [self.schedule[i] for i in target_idx]
        rates = [e.rate for e in entries]
        dates = [e.due_date for e in entries]
        if self.config.method is RepaymentMethod.EQUAL_INSTALLMENT:
            old_profile = [e.total_cents for e in entries]
        else:
            old_profile = [e.principal_cents for e in entries]
        base = sum(e.principal_cents for e in entries) - pool
        rebuilt = shorten_schedule(
            base, rates, self.config.method, dates,
            start_period=entries[0].period,
            old_payment_cents=old_profile)
        # 前 len(rebuilt) 个槽位替换为新期次，其余尾部期次移除
        keep = [e for i, e in enumerate(self.schedule)
                if i < target_idx[0] or i > target_idx[-1]]
        self.schedule = (keep[:target_idx[0]] + rebuilt
                         + keep[target_idx[0]:])

    def _rebuild_targets(self, target_idx: list[int],
                         base_principal: int) -> None:
        """对作用域期次按真实剩余本金与各期已生效利率重摊。

        期号与到期日保持不变（缩短期限由调用方裁剪列表）。每个期次的
        利率取自调息历史，保证已生效的未来期次利率不被提前还款重算回退。
        """
        old = [self.schedule[i] for i in target_idx]
        rates = [self._rate_for_due_date(e.due_date) for e in old]
        dates = [e.due_date for e in old]
        rebuilt = build_schedule(
            base_principal, rates, self.config.method,
            dates, start_period=old[0].period)
        for i, new_entry in zip(target_idx, rebuilt):
            self.schedule[i] = new_entry

    # ---------- 对外：利率调整 ----------

    def adjust_rate(self, new_annual_rate: float,
                    effective_date: date) -> None:
        """期间利率调整：记录调息历史，并重算完全未还的未来期次。

        重算时各期次保留其到期日已生效的利率，因此多次调息与提前还款
        无论怎么穿插，已生效的未来期次利率都不会被后续操作回退。
        """
        if new_annual_rate < 0:
            raise InvalidRequestError("利率不能为负")
        with self._lock:
            self._accrue_penalty(effective_date)
            self.rate_history.append(
                RateChange(effective_date=effective_date,
                           annual_rate=new_annual_rate))
            self.rate_history.sort(key=lambda c: c.effective_date)
            target_idx = self._target_indices(effective_date)
            if target_idx:
                base = sum(self.schedule[i].principal_cents
                           for i in target_idx)
                self._rebuild_targets(target_idx, base)


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
