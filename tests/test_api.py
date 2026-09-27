"""通过 FastAPI TestClient 验证 HTTP 层错误码区分与幂等。"""

from datetime import date

from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
TOTAL = sum(e.total_cents for e in main.account.schedule)


def setup_function(_):
    # 每个用例重建内存账户
    main.account = main.LoanAccount(main.LoanConfig(
        principal_cents=1_000_000_00, annual_rate=0.036, periods=12,
        method=main.RepaymentMethod.EQUAL_INSTALLMENT,
        start_date=date(2026, 1, 1)))


def test_api_normal_duplicate_and_distinguishable_errors():
    e1 = main.account.schedule[0].total_cents
    payload = {"request_id": "api-1", "amount_cents": e1,
               "pay_date": "2026-02-01"}
    r1 = client.post("/payments", json=payload)
    r2 = client.post("/payments", json=payload)
    print("\n[输入] 同一请求 POST 两次:", payload["request_id"])
    print("[中间状态] 状态码:", r1.status_code, r2.status_code,
          "第二次 duplicate:", r2.json()["duplicate"])
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["duplicate"] is True

    bad = client.post("/payments", json={
        "request_id": "bad", "amount_cents": -1, "pay_date": "2026-02-01"})
    over = client.post("/payments", json={
        "request_id": "over", "amount_cents": TOTAL + 1,
        "pay_date": "2026-02-01"})
    print("[判定依据] 非法金额 ->", bad.status_code, bad.json()["error"])
    print("[判定依据] 超额还款 ->", over.status_code, over.json()["error"])
    assert bad.status_code == 400
    assert over.status_code == 422

    # 结清后再入账 -> 409
    remain = sum(e.total_cents for e in main.account.schedule[1:])
    client.post("/payments", json={
        "request_id": "settle", "amount_cents": remain,
        "pay_date": "2026-02-01"})
    again = client.post("/payments", json={
        "request_id": "again", "amount_cents": 1, "pay_date": "2026-02-01"})
    print("[判定依据] 已结清 ->", again.status_code, again.json()["error"])
    assert again.status_code == 409
