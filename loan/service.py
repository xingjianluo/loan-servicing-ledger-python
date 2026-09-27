"""应用服务：开户、查询、还款、利率调整、提前还款。

并发与幂等编排：
- 每个账户一把独立锁（见 :class:`InMemoryStore.account_lock`），同一账户的
  写请求在服务层串行化，再进入账户聚合内的 RLock；不同账户互不阻塞；
- 幂等键为 payment_id，已存在则直接返回原凭证或报 DuplicatePaymentError，
  因此同一笔还款无论重复提交还是并发提交，账户余额都只扣减一次。
"""

from datetime import date
from decimal import Decimal
from typing import Sequence

from .account import Account
from .amortization import PrepayMode
from .errors import AccountStateError
from .models import (
    ALLOCATION_DEFAULT,
    Allocation,
    AmortizationType,
    BalanceView,
    Installment,
    LoanTerms,
    PaymentRecord,
    RateChange,
)
from .money import money, rate
from .storage import InMemoryStore

Number = "int | str | Decimal"


class LoanService:
    """面向单借款账户的贷款服务（本地内存/本地文件模拟数据）。"""

    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()

    # ---------------- 开户 ----------------

    def open_account(
        self,
        account_id: str,
        principal: Number,
        annual_rate: Number,
        term_months: int,
        origination_date: date,
        amortization_type: AmortizationType = AmortizationType.EQUAL_INSTALLMENT,
        grace_period_days: int = 0,
        penalty_daily_rate: Number = "0",
        fee_rate: Number = "0",
        rate_changes: Sequence[RateChange] = (),
    ) -> Account:
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("account_id 必须是非空字符串")
        if self.store.exists(account_id):
            raise AccountStateError(f"账户已存在: {account_id}")
        terms = LoanTerms(
            principal=money(principal, positive=True),
            annual_rate=rate(annual_rate),
            term_months=int(term_months),
            origination_date=origination_date,
            amortization_type=amortization_type,
            grace_period_days=int(grace_period_days),
            penalty_daily_rate=rate(penalty_daily_rate),
            fee_rate=rate(fee_rate),
        )
        account = Account.open(account_id, terms, rate_changes)
        self.store.save(account)
        return account

    def get_account(self, account_id: str) -> Account:
        return self.store.load(account_id)

    # ---------------- 查询 ----------------

    def schedule(self, account_id: str) -> list[Installment]:
        return list(self.store.load(account_id).installments)

    def events(self, account_id: str) -> list[dict]:
        acc = self.store.load(account_id)
        return [
            {"seq": e.seq, "at": e.at.isoformat(), "event_type": e.event_type, "detail": e.detail}
            for e in acc.events
        ]

    def balance(self, account_id: str, as_of: date) -> BalanceView:
        with self.store.account_lock(account_id):
            account = self.store.load(account_id)
            view = account.balance(as_of)
            self.store.save(account)
            return view

    # ---------------- 还款 ----------------

    def pay(
        self,
        account_id: str,
        payment_id: str,
        amount: Number,
        payment_date: date,
        allocation_order: Sequence[str] = ALLOCATION_DEFAULT,
    ) -> PaymentRecord:
        with self.store.account_lock(account_id):
            account = self.store.load(account_id)
            amt = money(amount, positive=True)  # 非法金额在锁外语义下提前拒绝
            record = account.pay(payment_id, amt, payment_date, allocation_order)
            self.store.save(account)
            return record

    # ---------------- 利率调整 ----------------

    def change_rate(
        self,
        account_id: str,
        effective_date: date,
        annual_rate: Number,
        as_of: date,
    ) -> list[RateChange]:
        with self.store.account_lock(account_id):
            account = self.store.load(account_id)
            new_rate = rate(annual_rate)
            changes = account.change_rate(effective_date, new_rate, as_of)
            self.store.save(account)
            return changes

    # ---------------- 提前还款 ----------------

    def prepay(
        self,
        account_id: str,
        payment_id: str,
        amount: Number,
        payment_date: date,
        mode: PrepayMode = PrepayMode.KEEP_TERM,
    ) -> PaymentRecord:
        with self.store.account_lock(account_id):
            account = self.store.load(account_id)
            amt = money(amount, positive=True)
            record = account.prepay(payment_id, amt, payment_date, mode)
            self.store.save(account)
            return record
