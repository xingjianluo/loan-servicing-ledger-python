"""本地 HTTP 接口（FastAPI）：仅做参数解析与错误码映射，便于手工验证。

启动：``uv run python main.py``（默认 127.0.0.1:8000，端口可改）。
进程内使用单例 LoanService（内存存储），无外部依赖、无真实凭据。

错误响应统一形如 ``{"error_code": "...", "message": "..."}``，
不同失败原因对应不同 error_code，调用方可稳定区分。
"""

from datetime import date
from decimal import Decimal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .amortization import PrepayMode
from .errors import (
    AccountClosedError,
    AccountStateError,
    DuplicatePaymentError,
    InvalidAmountError,
    InvalidScheduleError,
    LoanError,
    PaymentExceedsDueError,
    RateAdjustmentError,
)
from .models import ALLOCATION_DEFAULT, AmortizationType
from .service import LoanService

# (异常类, HTTP 状态码, 稳定错误码)
_ERROR_MAP: list[tuple[type[Exception], int, str]] = [
    (InvalidAmountError, 400, "INVALID_AMOUNT"),
    (InvalidScheduleError, 400, "INVALID_SCHEDULE"),
    (RateAdjustmentError, 400, "RATE_ADJUSTMENT_REJECTED"),
    (ValueError, 400, "INVALID_REQUEST"),
    (PaymentExceedsDueError, 422, "PAYMENT_EXCEEDS_DUE"),
    (AccountClosedError, 409, "ACCOUNT_CLOSED"),
    (DuplicatePaymentError, 409, "DUPLICATE_PAYMENT_CONFLICT"),
    (AccountStateError, 409, "ACCOUNT_STATE_CONFLICT"),
]


class OpenAccountRequest(BaseModel):
    account_id: str
    principal: str = Field(description="贷款本金，字符串金额，如 '100000.00'")
    annual_rate: str = Field(description="年利率，如 '0.072'")
    term_months: int
    origination_date: date
    amortization_type: AmortizationType = AmortizationType.EQUAL_INSTALLMENT
    grace_period_days: int = 0
    penalty_daily_rate: str = "0"
    fee_rate: str = "0"


class PaymentRequest(BaseModel):
    payment_id: str
    amount: str
    payment_date: date
    allocation_order: list[str] | None = None


class RateChangeRequest(BaseModel):
    effective_date: date
    annual_rate: str
    as_of: date


class PrepayRequest(BaseModel):
    payment_id: str
    amount: str
    payment_date: date
    mode: PrepayMode = PrepayMode.KEEP_TERM


def _allocation_dict(alloc) -> dict:
    return {
        "interest": str(alloc.interest),
        "penalty": str(alloc.penalty),
        "fee": str(alloc.fee),
        "principal": str(alloc.principal),
        "total": str(alloc.total),
    }


def _installment_dict(acc, inst, as_of) -> dict:
    penalty = acc.penalty_of(inst, as_of)
    unpaid_total = acc.unpaid_total_of(inst, as_of)
    return {
        "period": inst.period,
        "due_date": inst.due_date.isoformat(),
        "principal": str(inst.principal),
        "interest": str(inst.interest),
        "due_total": str(inst.due_total),
        "paid_principal": str(inst.paid_principal),
        "paid_interest": str(inst.paid_interest),
        "accrued_penalty": str(penalty),
        "paid_penalty": str(inst.paid_penalty),
        "accrued_fee": str(inst.accrued_fee),
        "paid_fee": str(inst.paid_fee),
        "unpaid_total": str(unpaid_total),
        "is_paid": unpaid_total == 0,
    }


def create_app(service: LoanService | None = None) -> FastAPI:
    """构造 FastAPI 应用；可注入自定义 service（如 JsonFileStore 版本）。"""
    svc = service or LoanService()
    app = FastAPI(title="loan-servicing-ledger（本地贷款服务）", version="0.1.0")

    @app.exception_handler(LoanError)
    async def _loan_error_handler(request, exc: LoanError):
        for exc_type, status, code in _ERROR_MAP:
            if isinstance(exc, exc_type):
                body = {"error_code": code, "message": str(exc)}
                if isinstance(exc, PaymentExceedsDueError) and exc.allowed is not None:
                    body["allowed"] = str(exc.allowed)
                return JSONResponse(status_code=status, content=body)
        return JSONResponse(
            status_code=400,
            content={"error_code": "LOAN_ERROR", "message": str(exc)},
        )

    @app.exception_handler(KeyError)
    async def _key_error_handler(request, exc: KeyError):
        return JSONResponse(
            status_code=404,
            content={"error_code": "ACCOUNT_NOT_FOUND", "message": str(exc.args[0])},
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(request, exc: ValueError):
        return JSONResponse(
            status_code=400,
            content={"error_code": "INVALID_REQUEST", "message": str(exc)},
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/accounts", status_code=201)
    def open_account(req: OpenAccountRequest):
        acc = svc.open_account(
            req.account_id,
            req.principal,
            req.annual_rate,
            req.term_months,
            req.origination_date,
            amortization_type=req.amortization_type,
            grace_period_days=req.grace_period_days,
            penalty_daily_rate=req.penalty_daily_rate,
            fee_rate=req.fee_rate,
        )
        return {"account_id": acc.account_id, "status": acc.status.value, "installments": len(acc.installments)}

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        acc = svc.get_account(account_id)
        return {
            "account_id": acc.account_id,
            "status": acc.status.value,
            "terms": {
                "principal": str(acc.terms.principal),
                "annual_rate": str(acc.terms.annual_rate),
                "term_months": acc.terms.term_months,
                "amortization_type": acc.terms.amortization_type.value,
                "grace_period_days": acc.terms.grace_period_days,
                "penalty_daily_rate": str(acc.terms.penalty_daily_rate),
                "fee_rate": str(acc.terms.fee_rate),
            },
            "rate_changes": [
                {"effective_date": c.effective_date.isoformat(), "annual_rate": str(c.annual_rate)}
                for c in acc.rate_changes
            ],
            "installments": [
                _installment_dict(acc, i, date.today()) for i in acc.installments
            ],
        }

    @app.get("/accounts/{account_id}/balance")
    def get_balance(account_id: str, as_of: date):
        view = svc.balance(account_id, as_of)
        return {
            "as_of_date": view.as_of_date.isoformat(),
            "status": view.status.value,
            "outstanding_principal": str(view.outstanding_principal),
            "due_principal": str(view.due_principal),
            "due_interest": str(view.due_interest),
            "due_penalty": str(view.due_penalty),
            "due_fee": str(view.due_fee),
            "due_total": str(view.due_total),
        }

    @app.post("/accounts/{account_id}/payments", status_code=201)
    def post_payment(account_id: str, req: PaymentRequest):
        order = tuple(req.allocation_order) if req.allocation_order else ALLOCATION_DEFAULT
        record = svc.pay(account_id, req.payment_id, req.amount, req.payment_date, order)
        return {
            "payment_id": record.payment_id,
            "amount": str(record.amount),
            "payment_date": record.payment_date.isoformat(),
            "allocation": _allocation_dict(record.allocation),
        }

    @app.post("/accounts/{account_id}/prepayments", status_code=201)
    def post_prepayment(account_id: str, req: PrepayRequest):
        record = svc.prepay(account_id, req.payment_id, req.amount, req.payment_date, req.mode)
        return {
            "payment_id": record.payment_id,
            "amount": str(record.amount),
            "payment_date": record.payment_date.isoformat(),
            "allocation": _allocation_dict(record.allocation),
        }

    @app.post("/accounts/{account_id}/rate-changes", status_code=200)
    def post_rate_change(account_id: str, req: RateChangeRequest):
        changes = svc.change_rate(account_id, req.effective_date, req.annual_rate, req.as_of)
        return {
            "rate_changes": [
                {"effective_date": c.effective_date.isoformat(), "annual_rate": str(c.annual_rate)}
                for c in changes
            ]
        }

    @app.get("/accounts/{account_id}/events")
    def get_events(account_id: str):
        return {"account_id": account_id, "events": svc.events(account_id)}

    return app


app = create_app()
