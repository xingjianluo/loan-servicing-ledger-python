"""领域模型与类型定义。金额一律使用整数“分”，避免浮点尾差。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class RepaymentMethod(str, Enum):
    EQUAL_INSTALLMENT = "equal_installment"  # 等额本息
    EQUAL_PRINCIPAL = "equal_principal"      # 等额本金


class AccountStatus(str, Enum):
    ACTIVE = "active"        # 正常
    OVERDUE = "overdue"      # 逾期
    SETTLED = "settled"      # 已结清


class EntryStatus(str, Enum):
    PENDING = "pending"      # 未到期
    DUE = "due"              # 已到期未足额
    GRACE = "grace"          # 宽限期内
    OVERDUE = "overdue"      # 已过宽限期
    PAID = "paid"            # 已足额结清


class PrepaymentMethod(str, Enum):
    SHORTEN_TERM = "shorten_term"    # 月供不变，缩短剩余期数
    REDUCE_PAYMENT = "reduce_payment"  # 期数不变，降低月供


@dataclass
class RateChange:
    """利率调整记录：自 effective_date 起（含当日生效的未来期次）适用新利率。"""

    effective_date: date
    annual_rate: float


@dataclass
class ScheduleEntry:
    """摊还计划中的一期。金额单位：分。"""

    period: int              # 期号，从 1 开始
    due_date: date
    principal_cents: int     # 本期应还本金
    interest_cents: int      # 本期应还利息
    rate: float              # 本期适用年利率（如 0.036）
    status: EntryStatus = EntryStatus.PENDING
    paid_principal_cents: int = 0
    paid_interest_cents: int = 0

    @property
    def total_cents(self) -> int:
        return self.principal_cents + self.interest_cents


@dataclass
class Allocation:
    """一笔还款在各科目上的分配结果（分）。"""

    penalty_cents: int = 0
    fee_cents: int = 0
    interest_cents: int = 0
    principal_cents: int = 0

    @property
    def total_cents(self) -> int:
        return (self.penalty_cents + self.fee_cents
                + self.interest_cents + self.principal_cents)


@dataclass
class PaymentReceipt:
    """还款入账回执。"""

    request_id: str
    amount_cents: int
    allocation: Allocation
    status_after: AccountStatus
    duplicate: bool = False  # 是否为幂等重放


@dataclass
class PrepaymentReceipt:
    """提前还款申请回执（独立入口，与普通还款分开）。"""

    request_id: str
    amount_cents: int                 # 本次提前偿还的本金（分）
    method: PrepaymentMethod
    remaining_principal_cents: int    # 申请后剩余本金（分）
    remaining_periods: int            # 申请后完全未还的剩余期数
    next_due_date: date | None        # 重算后首个未到期期次的到期日
    status_after: AccountStatus
    duplicate: bool = False          # 是否为幂等重放


@dataclass
class StatusChange:
    """状态迁移记录，保证可追溯。"""

    changed_at: date
    from_status: AccountStatus
    to_status: AccountStatus
    reason: str


@dataclass
class LoanConfig:
    """可配置项。"""

    principal_cents: int
    annual_rate: float
    periods: int
    method: RepaymentMethod
    start_date: date
    grace_days: int = 3                 # 宽限期（天）
    penalty_daily_rate: float = 0.0005  # 逾期罚息日利率
