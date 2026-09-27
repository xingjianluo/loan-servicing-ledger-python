"""存储抽象、本地内存实现与单文件 JSON 持久化。

不依赖任何外部服务或真实凭据。同一账户的所有写操作经过一把账户锁串行化，
配合账户自身的 payment_id 幂等表，保证并发/重复提交不重复扣减。
"""

import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .account import Account


class AccountStore(Protocol):
    """账户存储接口。"""

    def save(self, account: "Account") -> None: ...

    def load(self, account_id: str) -> "Account": ...

    def exists(self, account_id: str) -> bool: ...

    def account_lock(self, account_id: str) -> threading.RLock:
        """返回该账户专用的串行化锁（同一账户的写请求互斥）。"""
        ...


class InMemoryStore:
    """进程内字典存储；按账户分配独立锁，不同账户可并行。"""

    def __init__(self) -> None:
        self._accounts: dict[str, "Account"] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def account_lock(self, account_id: str) -> threading.RLock:
        with self._guard:
            lk = self._locks.get(account_id)
            if lk is None:
                lk = threading.RLock()
                self._locks[account_id] = lk
            return lk

    def save(self, account: "Account") -> None:
        self._accounts[account.account_id] = account

    def load(self, account_id: str) -> "Account":
        try:
            return self._accounts[account_id]
        except KeyError as exc:
            raise KeyError(f"账户不存在: {account_id}") from exc

    def exists(self, account_id: str) -> bool:
        return account_id in self._accounts


# ---------------- 本地 JSON 文件持久化 ----------------

def _account_to_dict(account: "Account") -> dict:
    from .models import AccountStatus, Allocation, PaymentRecord

    return {
        "account_id": account.account_id,
        "status": account.status.value,
        "terms": {
            "principal": str(account.terms.principal),
            "annual_rate": str(account.terms.annual_rate),
            "term_months": account.terms.term_months,
            "origination_date": account.terms.origination_date.isoformat(),
            "amortization_type": account.terms.amortization_type.value,
            "grace_period_days": account.terms.grace_period_days,
            "penalty_daily_rate": str(account.terms.penalty_daily_rate),
            "fee_rate": str(account.terms.fee_rate),
        },
        "rate_changes": [
            {"effective_date": c.effective_date.isoformat(), "annual_rate": str(c.annual_rate)}
            for c in account.rate_changes
        ],
        "installments": [
            {
                "period": i.period,
                "due_date": i.due_date.isoformat(),
                "principal": str(i.principal),
                "interest": str(i.interest),
                "paid_principal": str(i.paid_principal),
                "paid_interest": str(i.paid_interest),
                "paid_penalty": str(i.paid_penalty),
                "accrued_fee": str(i.accrued_fee),
                "paid_fee": str(i.paid_fee),
                "fee_charged": i.fee_charged,
                "principal_payments": [
                    [pd.isoformat(), str(amt)] for pd, amt in i.principal_payments
                ],
            }
            for i in account.installments
        ],
        "payments": [
            {
                "payment_id": p.payment_id,
                "amount": str(p.amount),
                "payment_date": p.payment_date.isoformat(),
                "created_at": p.created_at.isoformat(),
                "allocation": {
                    "interest": str(p.allocation.interest),
                    "penalty": str(p.allocation.penalty),
                    "fee": str(p.allocation.fee),
                    "principal": str(p.allocation.principal),
                },
            }
            for p in account.payments.values()
        ],
        "events": [
            {"seq": e.seq, "at": e.at.isoformat(), "event_type": e.event_type, "detail": e.detail}
            for e in account.events
        ],
        "_seq": account._seq,
    }


def _account_from_dict(data: dict) -> "Account":
    from datetime import date, datetime
    from decimal import Decimal

    from .account import Account
    from .models import (
        AccountStatus,
        Allocation,
        AmortizationType,
        Installment,
        LoanTerms,
        PaymentRecord,
        RateChange,
    )

    td = data["terms"]
    terms = LoanTerms(
        principal=Decimal(td["principal"]),
        annual_rate=Decimal(td["annual_rate"]),
        term_months=td["term_months"],
        origination_date=date.fromisoformat(td["origination_date"]),
        amortization_type=AmortizationType(td["amortization_type"]),
        grace_period_days=td["grace_period_days"],
        penalty_daily_rate=Decimal(td["penalty_daily_rate"]),
        fee_rate=Decimal(td["fee_rate"]),
    )
    installments = [
        Installment(
            period=row["period"],
            due_date=date.fromisoformat(row["due_date"]),
            principal=Decimal(row["principal"]),
            interest=Decimal(row["interest"]),
            paid_principal=Decimal(row["paid_principal"]),
            paid_interest=Decimal(row["paid_interest"]),
            paid_penalty=Decimal(row["paid_penalty"]),
            accrued_fee=Decimal(row["accrued_fee"]),
            paid_fee=Decimal(row["paid_fee"]),
            fee_charged=row["fee_charged"],
            principal_payments=[
                (date.fromisoformat(pd), Decimal(amt))
                for pd, amt in row.get("principal_payments", [])
            ],
        )
        for row in data["installments"]
    ]
    payments = {
        p["payment_id"]: PaymentRecord(
            payment_id=p["payment_id"],
            amount=Decimal(p["amount"]),
            payment_date=date.fromisoformat(p["payment_date"]),
            created_at=datetime.fromisoformat(p["created_at"]),
            allocation=Allocation(**{k: Decimal(v) for k, v in p["allocation"].items()}),
        )
        for p in data["payments"]
    }
    from .models import AccountEvent

    events = [
        AccountEvent(
            seq=e["seq"],
            at=datetime.fromisoformat(e["at"]),
            event_type=e["event_type"],
            detail=e["detail"],
        )
        for e in data["events"]
    ]
    return Account(
        account_id=data["account_id"],
        terms=terms,
        installments=installments,
        rate_changes=[
            RateChange(
                effective_date=date.fromisoformat(c["effective_date"]),
                annual_rate=Decimal(c["annual_rate"]),
            )
            for c in data.get("rate_changes", [])
        ],
        status=AccountStatus(data["status"]),
        payments=payments,
        events=events,
        _seq=data.get("_seq", len(events)),
    )


class JsonFileStore(InMemoryStore):
    """单文件 JSON 持久化：构造时加载，每次 save 整体落盘。

    仅用于本地演示/验证——无数据库、无网络、无真实凭据。
    写文件用临时文件 + 原子替换，避免半写损坏。
    """

    def __init__(self, path: "str | object") -> None:
        super().__init__()
        from pathlib import Path

        self.path = Path(path)
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        import json

        with self.path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        for row in payload.get("accounts", []):
            account = _account_from_dict(row)
            self._accounts[account.account_id] = account

    def _flush(self) -> None:
        import json
        import os

        payload = {
            "accounts": [
                _account_to_dict(a) for a in self._accounts.values()
            ]
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def save(self, account: "Account") -> None:
        with self.account_lock(account.account_id):
            super().save(account)
            self._flush()
