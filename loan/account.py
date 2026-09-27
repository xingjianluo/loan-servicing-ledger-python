"""账户聚合：逾期判定、罚息确定性重算、状态迁移、还款入账分配、幂等控制。

逾期与罚息语义（自然日，金额量化到分）：
- 每期 due_date 为应还日；宽限期 grace_period_days 内未足额仍视为正常，
  自 ``overdue_start = due_date + grace + 1`` 当日起进入逾期；
- 罚息基数 = 该期「当日未还本金」，日率 = 条款 penalty_daily_rate；
  某段自然日的罚息 = 基数 * 日率 * 天数（跨还款分段，合计后一次性量化到分）；
  因本金偿还历史被逐期记录，penalty(as_of) 是 as_of 的纯函数，
  与 scan/balance 的调用次数、时机无关（路径无关、无扫描导致的尾差）；
- 逾期管理费 fee_rate：每期首次被判定逾期时一次性计提
  ``overdue_start 当日未还本金 * fee_rate``（同一期不重复，金额路径无关）；
- 状态 ACTIVE -> OVERDUE -> ACTIVE/SETTLED 的每次迁移均落事件可追溯。

还款分配（稳定、可解释）：
- 仅偿还「已到期」部分；按 allocation_order 桶顺序依次填满，
  默认 利息 -> 罚息 -> 费用 -> 本金；同桶内按期数从小到大；
- 允许部分还款；超过到期应还合计被拒（提前还款走 prepay）。

幂等：payment_id 为幂等键，同键同参返回原凭证、同键异参报冲突；
账户锁内完成「检查-扣减」，配合服务层账户锁串行化并发请求。
"""

import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Sequence

from .amortization import PrepayMode, build_schedule, rebuild_after_prepayment
from .errors import (
    AccountClosedError,
    AccountStateError,
    DuplicatePaymentError,
    InvalidAmountError,
    PaymentExceedsDueError,
    RateAdjustmentError,
)
from .models import (
    ALLOCATION_BUCKETS,
    ALLOCATION_DEFAULT,
    AccountEvent,
    AccountStatus,
    Allocation,
    BalanceView,
    Installment,
    LoanTerms,
    PaymentRecord,
    RateChange,
)
from .money import money, quantize, rate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _validate_order(order: Sequence[str]) -> tuple[str, ...]:
    if tuple(sorted(order)) != tuple(sorted(ALLOCATION_BUCKETS)):
        raise ValueError(
            f"分配顺序必须恰好包含 {ALLOCATION_BUCKETS} 四个桶各一次，得到 {order}"
        )
    return tuple(order)


@dataclass
class Account:
    """单个借款账户（一个账户一笔贷款）。"""

    account_id: str
    terms: LoanTerms
    installments: list[Installment]
    rate_changes: list[RateChange] = field(default_factory=list)
    status: AccountStatus = AccountStatus.ACTIVE
    payments: dict[str, PaymentRecord] = field(default_factory=dict)
    events: list[AccountEvent] = field(default_factory=list)
    _seq: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ---------------- 工厂 ----------------

    @classmethod
    def open(
        cls,
        account_id: str,
        terms: LoanTerms,
        rate_changes: Sequence[RateChange] = (),
    ) -> "Account":
        if not isinstance(terms, LoanTerms):
            raise InvalidAmountError("terms 必须是 LoanTerms")
        schedule = build_schedule(terms, rate_changes)
        acc = cls(
            account_id=account_id,
            terms=terms,
            installments=list(schedule.installments),
            rate_changes=list(schedule.rate_changes),
        )
        acc._record_event(
            "ACCOUNT_OPENED",
            {
                "principal": str(terms.principal),
                "annual_rate": str(terms.annual_rate),
                "term_months": terms.term_months,
                "amortization_type": terms.amortization_type.value,
                "grace_period_days": terms.grace_period_days,
                "penalty_daily_rate": str(terms.penalty_daily_rate),
                "fee_rate": str(terms.fee_rate),
                "rate_changes": [
                    {"effective_date": str(c.effective_date), "annual_rate": str(c.annual_rate)}
                    for c in schedule.rate_changes
                ],
            },
        )
        return acc

    # ---------------- 事件 ----------------

    def _record_event(self, event_type: str, detail: dict) -> AccountEvent:
        self._seq += 1
        ev = AccountEvent(seq=self._seq, at=_now(), event_type=event_type, detail=detail)
        self.events.append(ev)
        return ev

    # ---------------- 逾期判定与确定性罚息 ----------------

    def _overdue_start(self, inst: Installment) -> date:
        """宽限期届满次日（该日仍未足额即视为逾期）。"""
        return inst.due_date + timedelta(days=self.terms.grace_period_days + 1)

    def _unpaid_principal_at(self, inst: Installment, d: date) -> Decimal:
        """某期截至 d 日（含）的未还本金：计划本金 - 当日及以前已还本金。"""
        paid = sum(
            (amt for pd, amt in inst.principal_payments if pd <= d),
            Decimal("0"),
        )
        return inst.principal - paid

    def penalty_of(self, inst: Installment, as_of: date) -> Decimal:
        """确定性计算某期截至 as_of（含）应计罚息，路径无关。

        约定：还款日当天仍按「还款前基数」计息（与还款清偿含当日罚息自洽），
        次日起按新基数。以 (本金还款日 +1 天) 为分段点，对区间 [od, as_of]
        内每个自然日段，段内基数恒定为段首前一日的未还本金；各段
        基数*日率*天数 高精度求和后，最后一次性量化到分。
        """
        od = self._overdue_start(inst)
        if as_of < od or self.terms.penalty_daily_rate == 0:
            return Decimal("0")
        cuts = sorted(
            {pd + timedelta(days=1) for pd, _ in inst.principal_payments
             if od <= pd <= as_of}
        )
        total = Decimal("0")
        seg_start = od
        for cut in cuts + [as_of + timedelta(days=1)]:
            seg_end = min(cut, as_of + timedelta(days=1))
            days = (seg_end - seg_start).days
            if days > 0:
                base = self._unpaid_principal_at(
                    inst, seg_start - timedelta(days=1)
                )
                total += base * self.terms.penalty_daily_rate * Decimal(days)
            seg_start = cut
        return quantize(total)

    def unpaid_penalty_of(self, inst: Installment, as_of: date) -> Decimal:
        return self.penalty_of(inst, as_of) - inst.paid_penalty

    def unpaid_total_of(self, inst: Installment, as_of: date) -> Decimal:
        return (
            inst.unpaid_principal
            + inst.unpaid_interest
            + inst.unpaid_fee
            + self.unpaid_penalty_of(inst, as_of)
        )

    def scan(self, as_of: date) -> None:
        """扫描至 as_of：计提一次性逾期管理费并做状态迁移。

        罚息不在此累积——它由 :meth:`penalty_of` 按日确定性重算；
        本方法可任意重复调用，除首次计提费用外无副作用。
        """
        with self.lock:
            overdue = False
            for inst in self.installments:
                if inst.due_date > as_of:
                    continue
                od = self._overdue_start(inst)
                core_unpaid = inst.unpaid_principal + inst.unpaid_interest
                if core_unpaid > 0 and as_of >= od:
                    overdue = True
                    if not inst.fee_charged:
                        # 以逾期起始日的未还本金为基数，保证路径无关
                        base = self._unpaid_principal_at(inst, od)
                        fee = quantize(base * self.terms.fee_rate)
                        inst.accrued_fee += fee
                        inst.fee_charged = True
                        self._record_event(
                            "FEE_CHARGED",
                            {"period": inst.period, "base": str(base), "fee": str(fee)},
                        )
            total_unpaid = sum(
                (self.unpaid_total_of(i, as_of) for i in self.installments), Decimal("0")
            )
            new_status = (
                AccountStatus.SETTLED
                if total_unpaid == 0
                else (AccountStatus.OVERDUE if overdue else AccountStatus.ACTIVE)
            )
            if new_status != self.status:
                old = self.status
                self.status = new_status
                self._record_event(
                    "STATE_CHANGED",
                    {"from": old.value, "to": new_status.value, "as_of": str(as_of)},
                )

    def balance(self, as_of: date) -> BalanceView:
        """先完成逾期扫描再返回视图。"""
        with self.lock:
            self.scan(as_of)
            due_p = due_i = due_pen = due_fee = Decimal("0")
            outstanding = Decimal("0")
            for inst in self.installments:
                outstanding += inst.unpaid_principal
                if inst.due_date <= as_of:
                    due_p += inst.unpaid_principal
                    due_i += inst.unpaid_interest
                    due_pen += self.unpaid_penalty_of(inst, as_of)
                    due_fee += inst.unpaid_fee
            return BalanceView(
                as_of_date=as_of,
                outstanding_principal=outstanding,
                due_principal=due_p,
                due_interest=due_i,
                due_penalty=due_pen,
                due_fee=due_fee,
                due_total=due_p + due_i + due_pen + due_fee,
                status=self.status,
            )

    # ---------------- 还款入账 ----------------

    def _due_buckets(self, as_of: date) -> dict[str, Decimal]:
        """截至 as_of 各桶已到期待还余额（罚息按 as_of 确定性重算）。"""
        agg = {b: Decimal("0") for b in ALLOCATION_BUCKETS}
        for inst in sorted(self.installments, key=lambda i: i.period):
            if inst.due_date <= as_of:
                agg["interest"] += inst.unpaid_interest
                agg["penalty"] += self.unpaid_penalty_of(inst, as_of)
                agg["fee"] += inst.unpaid_fee
                agg["principal"] += inst.unpaid_principal
        return agg

    def pay(
        self,
        payment_id: str,
        amount: "int | str | Decimal",
        payment_date: date,
        allocation_order: Sequence[str] = ALLOCATION_DEFAULT,
    ) -> PaymentRecord:
        """还款入账（部分还款允许；以 payment_id 幂等）。"""
        with self.lock:
            if self.status == AccountStatus.SETTLED:
                raise AccountClosedError(
                    f"账户 {self.account_id} 已结清，拒绝再次入账 payment_id={payment_id}"
                )
            amt = money(amount, positive=True)
            order = _validate_order(allocation_order)

            existing = self.payments.get(payment_id)
            if existing is not None:
                if existing.amount != amt or existing.payment_date != payment_date:
                    raise DuplicatePaymentError(
                        f"payment_id={payment_id} 曾以不同内容提交："
                        f"原 {existing.amount}@{existing.payment_date}，"
                        f"现 {amt}@{payment_date}"
                    )
                return existing  # 幂等：原样返回，不重复扣减

            self.scan(payment_date)
            due_insts = sorted(
                (i for i in self.installments if i.due_date <= payment_date),
                key=lambda i: i.period,
            )
            # 各期当前四桶待还（罚息取 payment_date 当日值）
            per_inst_balance: dict[int, dict[str, Decimal]] = {}
            totals = {b: Decimal("0") for b in ALLOCATION_BUCKETS}
            for inst in due_insts:
                balances = {
                    "interest": inst.unpaid_interest,
                    "penalty": self.unpaid_penalty_of(inst, payment_date),
                    "fee": inst.unpaid_fee,
                    "principal": inst.unpaid_principal,
                }
                per_inst_balance[inst.period] = balances
                for b, v in balances.items():
                    totals[b] += v
            total_due = sum(totals.values(), Decimal("0"))
            if total_due <= 0:
                raise PaymentExceedsDueError(
                    f"截至 {payment_date} 无到期应还金额（未到期还款请走提前还款）",
                    allowed=Decimal("0"),
                )
            if amt > total_due:
                raise PaymentExceedsDueError(
                    f"还款金额 {amt} 超过截至 {payment_date} 到期应还合计 {total_due}",
                    allowed=total_due,
                )

            alloc = {b: Decimal("0") for b in ALLOCATION_BUCKETS}
            remaining = amt
            for bucket in order:
                for inst in due_insts:
                    if remaining <= 0:
                        break
                    available = per_inst_balance[inst.period][bucket]
                    take = min(remaining, available)
                    if take <= 0:
                        continue
                    if bucket == "interest":
                        inst.paid_interest += take
                    elif bucket == "penalty":
                        inst.paid_penalty += take
                    elif bucket == "fee":
                        inst.paid_fee += take
                    else:
                        inst.paid_principal += take
                        inst.principal_payments.append((payment_date, take))
                    per_inst_balance[inst.period][bucket] -= take
                    alloc[bucket] += take
                    remaining -= take
            assert remaining == 0, f"分配未耗尽：剩余 {remaining}"
            assert sum(alloc.values(), Decimal("0")) == amt, "分配守恒失败"

            allocation = Allocation(
                interest=alloc["interest"],
                penalty=alloc["penalty"],
                fee=alloc["fee"],
                principal=alloc["principal"],
            )
            record = PaymentRecord(
                payment_id=payment_id,
                amount=amt,
                payment_date=payment_date,
                allocation=allocation,
                created_at=_now(),
            )
            self.payments[payment_id] = record
            self._record_event(
                "PAYMENT_POSTED",
                {
                    "payment_id": payment_id,
                    "amount": str(amt),
                    "payment_date": str(payment_date),
                    "allocation": {k: str(v) for k, v in alloc.items()},
                },
            )
            self.scan(payment_date)  # 补齐后 ACTIVE/SETTLED 迁移
            return record

    # ---------------- 利率调整 ----------------

    def change_rate(
        self, effective_date: date, annual_rate: Decimal, as_of: date
    ) -> list[RateChange]:
        """登记利率调整，重算生效日起尚未到期的期（已发生期不受影响）。"""
        with self.lock:
            new_rate = rate(annual_rate)
            if effective_date < self.terms.origination_date:
                raise RateAdjustmentError(
                    f"生效日 {effective_date} 早于放款日 {self.terms.origination_date}"
                )
            if any(c.effective_date == effective_date for c in self.rate_changes):
                raise RateAdjustmentError(f"生效日 {effective_date} 已存在利率调整")
            trial = sorted(self.rate_changes + [RateChange(effective_date, new_rate)],
                           key=lambda c: c.effective_date)

            self.scan(as_of)
            affected = [
                i for i in self.installments
                if i.due_date >= effective_date and i.due_date > as_of
            ]
            if not affected:
                raise RateAdjustmentError(
                    f"利率调整 {effective_date} 生效时已无可受影响的未到期期"
                )

            from .amortization import _rate_fn, _roll_fixed_dates

            new_pieces = _roll_fixed_dates(
                sum((i.unpaid_principal for i in affected), Decimal("0")),
                affected[0].period,
                [i.due_date for i in affected],
                _rate_fn(self.terms, trial),
                self.terms.amortization_type,
            )
            by_period = {p.period: p for p in new_pieces}
            for inst in affected:
                p = by_period[inst.period]
                assert inst.paid_principal == 0 and inst.paid_interest == 0
                inst.principal = p.principal
                inst.interest = p.interest
            self.rate_changes = trial
            self._record_event(
                "RATE_CHANGED",
                {"effective_date": str(effective_date),
                 "annual_rate": str(new_rate), "as_of": str(as_of)},
            )
            return list(self.rate_changes)

    # ---------------- 提前还款 ----------------

    def prepay(
        self,
        payment_id: str,
        amount: "int | str | Decimal",
        payment_date: date,
        mode: PrepayMode = PrepayMode.KEEP_TERM,
    ) -> PaymentRecord:
        """提前还款：截至还款日无到期欠款时允许，全部冲未到期本金并重算。"""
        with self.lock:
            if self.status == AccountStatus.SETTLED:
                raise AccountClosedError(
                    f"账户 {self.account_id} 已结清，拒绝提前还款 payment_id={payment_id}"
                )
            amt = money(amount, positive=True)
            existing = self.payments.get(payment_id)
            if existing is not None:
                if existing.amount != amt or existing.payment_date != payment_date:
                    raise DuplicatePaymentError(
                        f"payment_id={payment_id} 曾以不同内容提交"
                    )
                return existing

            self.scan(payment_date)
            due_total = sum(self._due_buckets(payment_date).values(), Decimal("0"))
            if due_total > 0:
                raise AccountStateError(
                    f"截至 {payment_date} 尚有到期应还 {due_total}，"
                    "请先清偿到期款项后再申请提前还款"
                )
            future_unpaid = sum(
                (i.unpaid_principal for i in self.installments if i.due_date > payment_date),
                Decimal("0"),
            )
            if future_unpaid <= 0:
                raise PaymentExceedsDueError("已无未到期本金可供提前偿还")
            if amt > future_unpaid:
                raise PaymentExceedsDueError(
                    f"提前还款额 {amt} 超过未到期剩余本金 {future_unpaid}",
                    allowed=future_unpaid,
                )

            rebuilt = rebuild_after_prepayment(
                self.terms, self.installments, self.rate_changes,
                payment_date, amt, mode,
            )
            self.installments = list(rebuilt.installments)
            record = PaymentRecord(
                payment_id=payment_id,
                amount=amt,
                payment_date=payment_date,
                allocation=Allocation(principal=amt),
                created_at=_now(),
            )
            self.payments[payment_id] = record
            self._record_event(
                "PREPAYMENT_POSTED",
                {"payment_id": payment_id, "amount": str(amt),
                 "payment_date": str(payment_date), "mode": mode.value,
                 "new_term_periods": len(self.installments)},
            )
            self.scan(payment_date)
            return record

    def settle_check(self, as_of: date) -> None:
        """扫描并在清偿时迁移 SETTLED（pay/scan 已自动调用，此处对外暴露）。"""
        with self.lock:
            self.scan(as_of)
