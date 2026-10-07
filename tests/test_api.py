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
    print("[判定依据] 非法金额 ->", bad.status_code, bad.json()["error"])
    assert bad.status_code == 400

    # 普通还款超过当期到期应还 -> PaymentExceedsDueError（不自动转提前还款）
    over_due = client.post("/payments", json={
        "request_id": "over-due", "amount_cents": e1 + 1,
        "pay_date": "2026-02-02"})
    print("[判定依据] 超当期到期应还 ->",
          over_due.status_code, over_due.json()["error"])
    assert over_due.status_code == 422
    assert over_due.json()["error"] == "PaymentExceedsDueError"

    # 超过整笔未还总额 -> PaymentExceedsTotalError
    total = sum(x.total_cents for x in main.account.schedule)
    over_total = client.post("/payments", json={
        "request_id": "over-total", "amount_cents": total + 1,
        "pay_date": "2026-02-02"})
    print("[判定依据] 超整笔未还总额 ->",
          over_total.status_code, over_total.json()["error"])
    assert over_total.status_code == 422
    assert over_total.json()["error"] == "PaymentExceedsTotalError"

    # 提前还款独立入口：先结清当期，再提前结清全部未来本金
    prepay_total = sum(
        x.principal_cents for x in main.account.schedule[1:])
    pr = client.post("/prepayments", json={
        "request_id": "pp-settle", "amount_cents": prepay_total,
        "method": "reduce_payment", "apply_date": "2026-02-03"})
    print("[中间状态] 提前还款申请:", pr.status_code, pr.json())
    assert pr.status_code == 200
    again = client.post("/payments", json={
        "request_id": "again", "amount_cents": 1, "pay_date": "2026-02-04"})
    print("[判定依据] 已结清 ->", again.status_code, again.json()["error"])
    assert again.status_code == 409


def test_api_prepayment_endpoint_idempotent():
    # 新账户（12 期未到期），20 万元足以缩短期数
    body = {"request_id": "pp-api-1", "amount_cents": 20_000_000,
            "method": "shorten_term", "apply_date": "2026-01-15"}
    r1 = client.post("/prepayments", json=body)
    r2 = client.post("/prepayments", json=body)
    print("\n[输入] 提前还款申请重复提交:", body["request_id"])
    print("[中间状态]", r1.json())
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["duplicate"] is True
    assert r1.json()["remaining_principal_cents"] == \
        r2.json()["remaining_principal_cents"]
