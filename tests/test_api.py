"""HTTP 接口测试：验证不同失败原因返回可区分的稳定错误码。"""

from fastapi.testclient import TestClient

from loan.api import create_app


def _client() -> TestClient:
    c = TestClient(create_app())
    r = c.post(
        "/accounts",
        json={
            "account_id": "A1",
            "principal": "3000.00",
            "annual_rate": "0.12",
            "term_months": 3,
            "origination_date": "2026-01-01",
            "grace_period_days": 3,
            "penalty_daily_rate": "0.0005",
            "fee_rate": "0.01",
        },
    )
    assert r.status_code == 201
    return c


def test_api_happy_path_and_distinct_error_codes():
    c = _client()
    print("[输入] 打开账户后，对同一资源发起合法与各类非法请求")

    # 合法余额查询：跨宽限期后逾期
    r = c.get("/accounts/A1/balance?as_of=2026-02-08")
    assert r.status_code == 200 and r.json()["status"] == "overdue"
    print("[判定] 逾期状态通过 balance 接口可见：", r.json())

    payload = {"payment_id": "P1", "amount": "100.00", "payment_date": "2026-02-08"}
    r1 = c.post("/accounts/A1/payments", json=payload)
    r2 = c.post("/accounts/A1/payments", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json() == r2.json()
    print("[判定] 重复入账幂等，两次响应完全一致")

    cases = [
        (
            "非法金额",
            {"payment_id": "X1", "amount": "-1", "payment_date": "2026-02-08"},
            400,
            "INVALID_AMOUNT",
        ),
        (
            "超出到期应还",
            {"payment_id": "X2", "amount": "999999.99", "payment_date": "2026-02-08"},
            422,
            "PAYMENT_EXCEEDS_DUE",
        ),
        (
            "同键异参冲突",
            {"payment_id": "P1", "amount": "101.00", "payment_date": "2026-02-08"},
            409,
            "DUPLICATE_PAYMENT_CONFLICT",
        ),
    ]
    for label, body, want_status, want_code in cases:
        r = c.post("/accounts/A1/payments", json=body)
        print(f"[判定] {label}: HTTP {r.status_code} error_code={r.json()['error_code']} "
              f"message={r.json()['message']}")
        assert r.status_code == want_status
        assert r.json()["error_code"] == want_code

    # 账户不存在
    r = c.get("/accounts/NOPE/balance?as_of=2026-02-08")
    assert r.status_code == 404 and r.json()["error_code"] == "ACCOUNT_NOT_FOUND"
    print("[判定] 未知账户 -> 404 ACCOUNT_NOT_FOUND")

    # 事件流可追溯
    r = c.get("/accounts/A1/events")
    types = [e["event_type"] for e in r.json()["events"]]
    print("[判定] 事件流:", types)
    assert "STATE_CHANGED" in types and "PAYMENT_POSTED" in types


def test_api_settled_account_rejects_repost():
    c = _client()
    # 无宽限无罚息的干净路径：逐期全额还清
    c2 = TestClient(create_app())
    c2.post(
        "/accounts",
        json={
            "account_id": "B1",
            "principal": "1200.00",
            "annual_rate": "0",
            "term_months": 3,
            "origination_date": "2026-01-01",
        },
    )
    for k, due in enumerate(["2026-02-01", "2026-03-01", "2026-04-01"]):
        bal = c2.get(f"/accounts/B1/balance?as_of={due}").json()
        r = c2.post(
            "/accounts/B1/payments",
            json={"payment_id": f"Q{k}", "amount": bal["due_total"], "payment_date": due},
        )
        assert r.status_code == 201
    final = c2.get("/accounts/B1/balance?as_of=2026-04-01").json()
    print("[判定] 末期视图:", final)
    assert final["status"] == "settled"
    r = c2.post(
        "/accounts/B1/payments",
        json={"payment_id": "QX", "amount": "1.00", "payment_date": "2026-04-01"},
    )
    print("[判定] 结清后入账:", r.status_code, r.json()["error_code"])
    assert r.status_code == 409 and r.json()["error_code"] == "ACCOUNT_CLOSED"
