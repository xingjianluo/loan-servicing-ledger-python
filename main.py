"""本地可运行的贷款服务 API：单账户、内存态、无外部依赖。

启动：uv run python main.py  （随后访问 http://127.0.0.1:8000/docs）
"""

from datetime import date

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from loan_ledger import (
    AccountSettledError,
    InvalidAmountError,
    InvalidRequestError,
    LoanAccount,
    LoanConfig,
    PaymentExceedsDueError,
    PaymentExceedsTotalError,
    PrepaymentMethod,
    RepaymentMethod,
)

app = FastAPI(title="Loan Servicing Ledger (local)")

account = LoanAccount(LoanConfig(
    principal_cents=1_000_000_00,   # 100 万元
    annual_rate=0.036,
    periods=12,
    method=RepaymentMethod.EQUAL_INSTALLMENT,
    start_date=date(2026, 1, 1),
    grace_days=3,
    penalty_daily_rate=0.0005,
))


class PaymentIn(BaseModel):
    request_id: str = Field(min_length=1)
    amount_cents: int
    pay_date: date


class PrepaymentIn(BaseModel):
    request_id: str = Field(min_length=1)
    amount_cents: int
    method: PrepaymentMethod
    apply_date: date


class RateAdjustIn(BaseModel):
    new_annual_rate: float = Field(ge=0)
    effective_date: date


class FeeIn(BaseModel):
    amount_cents: int


@app.exception_handler(InvalidAmountError)
async def _invalid_amount(_, exc):
    return _err(400, exc)


@app.exception_handler(PaymentExceedsDueError)
async def _exceeds_due(_, exc):
    # 普通还款超过当期到期应还（不会自动转提前还款）
    return _err(422, exc)


@app.exception_handler(PaymentExceedsTotalError)
async def _exceeds_total(_, exc):
    # 超过整笔未还总额（含提前还款超过未到期剩余本金）
    return _err(422, exc)


@app.exception_handler(AccountSettledError)
async def _settled(_, exc):
    return _err(409, exc)


@app.exception_handler(InvalidRequestError)
async def _invalid_req(_, exc):
    return _err(400, exc)


def _err(code: int, exc: Exception):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code,
                        content={"error": type(exc).__name__,
                                 "detail": str(exc)})


@app.get("/schedule")
def get_schedule():
    return account.schedule


@app.get("/balances")
def get_balances():
    return account.balances()


@app.get("/status")
def get_status(as_of: date):
    return {"status": account.refresh_status(as_of),
            "history": account.status_history}


@app.post("/payments")
def post_payment(body: PaymentIn):
    r = account.post_payment(body.request_id, body.amount_cents,
                             body.pay_date)
    return r


@app.post("/prepayments")
def apply_prepayment(body: PrepaymentIn):
    r = account.apply_prepayment(
        body.request_id, body.amount_cents, body.method, body.apply_date)
    return r


@app.post("/rate-adjustments")
def adjust_rate(body: RateAdjustIn):
    account.adjust_rate(body.new_annual_rate, body.effective_date)
    return {"schedule": account.schedule}


@app.post("/fees")
def charge_fee(body: FeeIn):
    account.charge_fee(body.amount_cents)
    return {"balances": account.balances()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
