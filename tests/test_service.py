"""服务层测试：并发/重复提交幂等、罚息路径无关、调息重算、提前还款、持久化。

用例打印输入、关键中间状态（余额、事件）与判定依据。
"""

import threading
from datetime import date
from decimal import Decimal

import pytest

from loan.account import Account
from loan.errors import (
    AccountStateError,
    PaymentExceedsDueError,
    RateAdjustmentError,
)
from loan.models import AmortizationType, LoanTerms
from loan.service import LoanService
from loan.storage import JsonFileStore
from loan.amortization import PrepayMode
from loan.money import quantize

CENT = Decimal("0.01")


def open_service(path=None):
    store = JsonFileStore(path) if path else None
    svc = LoanService(store)
    svc.open_account(
        "A1", "3000.00", "0.12", 3, date(2026, 1, 1),
        grace_period_days=3, penalty_daily_rate="0.0005", fee_rate="0.01",
    )
    return svc


def test_concurrent_duplicate_submission_idempotent():
    svc = open_service()
    due = svc.balance("A1", date(2026, 2, 8)).due_total
    print(f"[输入] 8 个线程并发提交同一 payment_id=C1，金额 {due}")
    results: list = []
    errors: list[Exception] = []

    def worker():
        try:
            r = svc.pay("A1", "C1", due, date(2026, 2, 8))
            results.append(r)
        except Exception as exc:  # noqa: BLE001 - 并发下理论上不应出现任何异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, [repr(e) for e in errors]
    assert len(results) == 8
    acc = svc.get_account("A1")
    assert len(acc.payments) == 1, "同一 payment_id 只能有一张凭证"
    # 余额只被扣一次：到期合计应为 0，剩余本金 = 3000 - 已还本金
    view = svc.balance("A1", date(2026, 2, 8))
    print(f"[中间状态] 凭证数={len(acc.payments)} status={view.status.value} "
          f"到期合计={view.due_total} 剩余本金={view.outstanding_principal}")
    assert view.due_total == 0
    assert view.outstanding_principal == Decimal("3000.00") - acc.payments["C1"].allocation.principal
    print("[判定] 8 次并发提交只入账一次，账户余额未被重复扣减（线程安全+幂等）")


def test_concurrent_distinct_ids_all_post():
    svc = open_service()
    print("[输入] 10 个线程用不同 payment_id 各还 50.00")
    errors: list[Exception] = []

    def worker(idx):
        try:
            svc.pay("A1", f"D{idx}", "50.00", date(2026, 2, 8))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    acc = svc.get_account("A1")
    assert len(acc.payments) == 10
    posted = sum((p.amount for p in acc.payments.values()), Decimal("0"))
    print(f"[判定] 10 笔并发独立还款全部入账，金额合计 {posted}")
    assert posted == Decimal("500.00")


def test_penalty_path_independent_of_scan_timing():
    """先天天扫描 vs 直接跳到 02-08，罚息必须相同。"""
    svc_a = open_service()
    svc_b = open_service()
    for d in [date(2026, 2, 5), date(2026, 2, 6), date(2026, 2, 7), date(2026, 2, 8)]:
        svc_a.balance("A1", d)
    va = svc_a.balance("A1", date(2026, 2, 8))
    vb = svc_b.balance("A1", date(2026, 2, 8))
    print(f"[中间状态] 逐日扫描罚息={va.due_penalty}；一次跳查罚息={vb.due_penalty}")
    assert va.due_penalty == vb.due_penalty
    base = svc_a.get_account("A1").installments[0].principal
    assert va.due_penalty == (base * Decimal("0.0005") * 4).quantize(CENT)
    print(f"[判定] 罚息只取决于日期与还款历史：{va.due_penalty} == "
          f"基数{base}*0.0005*4，与扫描次数无关")


def test_penalty_recomputes_after_partial_principal_payment():
    """部分还本后，后续罚息基数下降（分段计息），已计部分不回退。"""
    svc = open_service()
    # 02-05 还 500（默认顺序：息30+罚0.50+费9.90，剩余冲本金 459.60）
    r1 = svc.pay("A1", "PR1", "500.00", date(2026, 2, 5))
    print(f"[输入] 02-05 还500，分配={r1.allocation}")
    v1 = svc.balance("A1", date(2026, 2, 5))
    inst = svc.get_account("A1").installments[0]
    base1 = inst.principal
    base_after = inst.unpaid_principal
    print(f"[中间状态] 罚息基数 {base1} -> {base_after}")
    # 到 02-08：02-05 当天按旧基数1天，02-06~08 按新基数3天
    v2 = svc.balance("A1", date(2026, 2, 8))
    expect = quantize(base1 * Decimal("0.0005") * 1
                      + base_after * Decimal("0.0005") * 3)
    print(f"[判定依据] 分段罚息 = {base1}*0.0005*1 + {base_after}*0.0005*3 = {expect}")
    assert v2.due_penalty + r1.allocation.penalty == expect
    print(f"[判定] 已还罚息 {r1.allocation.penalty} + 未还 {v2.due_penalty} = {expect}")


def test_change_rate_recalc_via_service_conservation():
    svc = LoanService()
    svc.open_account("A1", "500000.00", "0.05", 24, date(2026, 3, 31))
    before_due_totals = [i.due_total for i in svc.schedule("A1")]
    print("[输入] 50万/5%/24期，2026-09-01 起调息至 3.5%（as_of=2026-06-30）")
    svc.change_rate("A1", date(2026, 9, 1), "0.035", date(2026, 6, 30))
    after = svc.schedule("A1")
    total_principal = sum((i.principal for i in after), Decimal("0"))
    print(f"[中间状态] 调息前6期月供={before_due_totals[0]}；调息后第7期月供={after[6].due_total}")
    assert total_principal == Decimal("500000.00")
    assert after[6].due_total < before_due_totals[6]
    print(f"[判定] 本金合计仍为 {total_principal}；后续月供下降至 {after[6].due_total}")
    # 生效日当天所有期都已发生 -> 拒绝
    with pytest.raises(RateAdjustmentError):
        svc.change_rate("A1", date(2028, 4, 30), "0.02", date(2028, 4, 30))
    print("[判定] 无受影响未到期期时调息被 RateAdjustmentError 拒绝")
    evs = [e["event_type"] for e in svc.events("A1")]
    assert "RATE_CHANGED" in evs


def test_prepay_full_flow_and_rejection_when_due_exists():
    svc = LoanService()
    svc.open_account("A1", "100000.00", "0.072", 12, date(2026, 1, 1))
    # 07-01 当日无历史欠款（前6期当天到期，需要先正常还款 -> 直接在 06-30 申请）
    print("[输入] 2026-06-30 提前还款 30,000（第1期 07... 实际首期02-01，故6-30有到期欠款）")
    with pytest.raises(AccountStateError):
        svc.prepay("A1", "PP0", "30000", date(2026, 6, 30))
    print("[判定] 有到期欠款时提前还款被 AccountStateError 拒绝（与普通还款失败原因可区分）")

    # 先结清前5期（最后一期当天前一天申请）
    for k in range(5):
        d = svc.get_account("A1").installments[k].due_date
        due = svc.balance("A1", d).due_total
        svc.pay("A1", f"N{k}", due, d)
    # 06-30 时第6期 07-01 未到期 -> 可以提前还
    rec = svc.prepay("A1", "PP1", "30000.00", date(2026, 6, 30),
                     mode=PrepayMode.KEEP_TERM)
    assert rec.allocation.principal == Decimal("30000.00")
    sched = svc.schedule("A1")
    total = sum((i.principal for i in sched), Decimal("0"))
    paid_core = Decimal("100000.00") - Decimal("30000.00")
    print(f"[判定] 提前还款后计划本金合计 {total} == 原本金-提前还本 {paid_core}")
    assert total == paid_core
    assert len(sched) == 12  # KEEP_TERM 期数不变

    # 超额提前还款拒绝
    with pytest.raises(PaymentExceedsDueError):
        svc.prepay("A1", "PP2", "999999", date(2026, 6, 30))
    print("[判定] 提前还款额超过未到期本金 -> PaymentExceedsDueError")


def test_persistence_roundtrip_preserves_idempotency(tmp_path):
    path = tmp_path / "ledger.json"
    svc = open_service(path)
    due = svc.balance("A1", date(2026, 2, 8)).due_total
    svc.pay("A1", "K1", due, date(2026, 2, 8))
    print(f"[中间状态] 已落盘还款 K1={due}，重新加载进程后重复提交")

    svc2 = LoanService(JsonFileStore(path))
    view_before = svc2.balance("A1", date(2026, 2, 8))
    rec = svc2.pay("A1", "K1", due, date(2026, 2, 8))  # 应幂等返回
    view_after = svc2.balance("A1", date(2026, 2, 8))
    assert rec.amount == due
    assert view_before.due_total == view_after.due_total == 0
    assert len(svc2.get_account("A1").payments) == 1
    print("[判定] JSON 持久化重载后幂等键仍生效，余额无变化，状态=", view_after.status.value)


def test_closed_account_rejects_all_further_posts(tmp_path):
    path = tmp_path / "ledger.json"
    svc = open_service(path)
    # 逐期全额还清
    for k in range(3):
        d = svc.get_account("A1").installments[k].due_date
        due = svc.balance("A1", d).due_total
        svc.pay("A1", f"F{k}", due, d)
    last = date(2026, 4, 1)
    assert svc.balance("A1", last).status.value == "settled"
    from loan.errors import AccountClosedError
    with pytest.raises(AccountClosedError):
        svc.pay("A1", "X1", "1.00", last)
    with pytest.raises(AccountClosedError):
        svc.prepay("A1", "X2", "1.00", last)
    print("[判定] SETTLED 账户普通还款与提前还款均被 AccountClosedError 拒绝")
