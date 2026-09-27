"""领域模型：贷款条款、摊还计划分期、还款单、账户事件、余额视图。

罚息设计说明：罚息不在 Installment 上累积存储，而是由 Account 依据
``principal_payments``（本金偿还历史）对任意 as_of 日期确定性重算，
因此余额只是「日期 + 还款历史」的纯函数，与扫描/查询的时机与次数无关。
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from .money import D

# 还款资金分配桶的稳定优先级（默认：利息 -> 罚息 -> 费用 -> 本金）。
# 可在还款时通过 allocation_order 覆盖，但必须是四个桶的一个排列。
ALLOCATION_INTEREST = "interest"
ALLOCATION_PENALTY = "penalty"
ALLOCATION_FEE = "fee"
ALLOCATION_PRINCIPAL = "principal"
ALLOCATION_BUCKETS = (
    ALLOCATION_INTEREST,
    ALLOCATION_PENALTY,
    ALLOCATION_FEE,
    ALLOCATION_PRINCIPAL,
)
ALLOCATION_DEFAULT = ALLOCATION_BUCKETS


class AmortizationType(str, Enum):
    """摊还口径。"""

    EQUAL_INSTALLMENT = "equal_installment"  # 等额本息
    EQUAL_PRINCIPAL = "equal_principal"      # 等额本金


class AccountStatus(str, Enum):
    """账户状态。"""

    ACTIVE = "active"    # 正常（含宽限期内）
    OVERDUE = "overdue"  # 逾期（超出宽限期仍未足额还款）
    SETTLED = "settled"  # 已结清（全部本息/罚息/费用为零）


@dataclass(frozen=True)
class LoanTerms:
    """贷款条款（全部为可配置项）。"""

    principal: Decimal
    annual_rate: Decimal                   # 年利率，如 Decimal('0.072') 表示 7.2%
    term_months: int                       # 期数
    origination_date: date                 # 放款日（首期到期日 = 放款日 +1 月）
    amortization_type: AmortizationType
    grace_period_days: int = 0             # 宽限期（自然日）
    penalty_daily_rate: Decimal = D("0")   # 日罚息率，对每期逾期未还本金按日计收
    fee_rate: Decimal = D("0")             # 逾期管理费率：每期首次进入逾期时按
                                           # 逾期起始日的未还本金一次性计提

    def monthly_rate(self) -> Decimal:
        """月利率 = 年利率 / 12（保留高精度，不提前量化）。"""
        return self.annual_rate / Decimal(12)


@dataclass
class Installment:
    """计划中的一期。本金/利息为合同口径应还，其余为动态执行口径。"""

    period: int
    due_date: date
    principal: Decimal                  # 计划本金
    interest: Decimal                   # 计划利息
    paid_principal: Decimal = D("0")    # 已还本金（累计）
    paid_interest: Decimal = D("0")     # 已还利息（累计）
    paid_penalty: Decimal = D("0")      # 已还罚息（累计）
    accrued_fee: Decimal = D("0")       # 已计提逾期管理费（每期最多一次）
    paid_fee: Decimal = D("0")          # 已还费用（累计）
    fee_charged: bool = False           # 本期是否已计过逾期管理费
    # 本金偿还历史 (还款日, 本金金额)，用于确定性重算罚息
    principal_payments: list = field(default_factory=list)

    @property
    def due_total(self) -> Decimal:
        """计划应还总额（本金 + 利息，不含后续产生的罚息/费用）。"""
        return self.principal + self.interest

    @property
    def unpaid_principal(self) -> Decimal:
        return self.principal - self.paid_principal

    @property
    def unpaid_interest(self) -> Decimal:
        return self.interest - self.paid_interest

    @property
    def unpaid_fee(self) -> Decimal:
        return self.accrued_fee - self.paid_fee


@dataclass(frozen=True)
class RateChange:
    """期间利率调整：自 effective_date（含）起到期的各期按新年利率计息。"""

    effective_date: date
    annual_rate: Decimal


@dataclass(frozen=True)
class Allocation:
    """一笔还款在四个桶中的分配结果。"""

    interest: Decimal = D("0")
    penalty: Decimal = D("0")
    fee: Decimal = D("0")
    principal: Decimal = D("0")

    @property
    def total(self) -> Decimal:
        return self.interest + self.penalty + self.fee + self.principal


@dataclass(frozen=True)
class PaymentRecord:
    """已入账还款凭证（同一 payment_id 永远只入账一次）。"""

    payment_id: str
    amount: Decimal
    payment_date: date
    allocation: Allocation
    created_at: datetime


@dataclass
class AccountEvent:
    """账户状态/余额变化的可追溯事件（按 seq 严格递增）。"""

    seq: int
    at: datetime
    event_type: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BalanceView:
    """某一时点的账户余额视图（as_of_date 已完成逾期扫描与罚息计提）。"""

    as_of_date: date
    outstanding_principal: Decimal   # 剩余本金（含未到期本金）
    due_principal: Decimal           # 已到期未还本金
    due_interest: Decimal            # 已到期未还利息
    due_penalty: Decimal             # 截至该日应计未还罚息
    due_fee: Decimal                 # 已计提未还费用
    due_total: Decimal               # 已到期应还合计
    status: AccountStatus
