"""本地端到端演示：不依赖任何外部服务。

运行：``uv run python demo.py``
依次演示：等额本息开户 -> 跨宽限期逾期与罚息 -> 部分还款与分配 ->
重复/并发幂等 -> 利率调整重算 -> 提前还款 -> 状态事件流。
"""

import threading
from datetime import date
from decimal import Decimal

from loan.amortization import PrepayMode
from loan.models import AmortizationType
from loan.service import LoanService


def hr(title):
    print(f"\n{'='*70}\n{title}\n{'='*70}")


def main():
    svc = LoanService()

    hr("1. 开户：等额本息 120,000 / 年利率 7.2% / 12期 / 宽限3天 / 日罚息0.05% / 管理费1%")
    acc = svc.open_account(
        "DEMO-001",
        "120000.00", "0.072", 12,
        date(2026, 1, 1),
        amortization_type=AmortizationType.EQUAL_INSTALLMENT,
        grace_period_days=3,
        penalty_daily_rate="0.0005",
        fee_rate="0.01",
    )
    print(f"账户 {acc.account_id} 状态 {acc.status.value}")
    for i in acc.installments[:3]:
        print(f"  第{i.period}期 {i.due_date} 本金={i.principal} 利息={i.interest} 合计={i.due_total}")
    print("  ...（共12期）")
    total_p = sum(i.principal for i in acc.installments)
    print(f"[守恒] 本金合计 = {total_p}")

    hr("2. 跨宽限期：2026-02-04（宽限内）vs 2026-02-08（逾期4天）")
    b1 = svc.balance("DEMO-001", date(2026, 2, 4))
    print(f"02-04: status={b1.status.value} due_total={b1.due_total} 罚息={b1.due_penalty} 费用={b1.due_fee}")
    b2 = svc.balance("DEMO-001", date(2026, 2, 8))
    print(f"02-08: status={b2.status.value} due_total={b2.due_total} 罚息={b2.due_penalty} 费用={b2.due_fee}")

    hr("3. 部分还款 3,000：默认顺序 利息->罚息->费用->本金")
    rec = svc.pay("DEMO-001", "P-0001", "3000.00", date(2026, 2, 8))
    print(f"分配: 利息={rec.allocation.interest} 罚息={rec.allocation.penalty} "
          f"费用={rec.allocation.fee} 本金={rec.allocation.principal} 合计={rec.allocation.total}")

    hr("4. 同一 payment_id 并发提交 8 次（只入账一次）")
    errs = []
    def worker():
        try:
            svc.pay("DEMO-001", "P-0001", "3000.00", date(2026, 2, 8))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    n = len(svc.get_account("DEMO-001").payments)
    print(f"异常数={len(errs)}，账户内凭证数={n}（重复提交幂等返回原凭证）")

    hr("5. 逐期正常还款：补齐第1期逾期欠款，并在第2~5期到期日当日足额还款")
    due = svc.balance("DEMO-001", date(2026, 2, 8)).due_total
    svc.pay("DEMO-001", "P-0002", due, date(2026, 2, 8))
    print(f"02-08 再还 {due} 后状态 = {svc.balance('DEMO-001', date(2026,2,8)).status.value}")
    for k in range(1, 5):  # 第2~5期（索引1..4）
        d = svc.get_account("DEMO-001").installments[k].due_date
        amt = svc.balance("DEMO-001", d).due_total
        svc.pay("DEMO-001", f"P-{k+2:04d}", amt, d)
        print(f"  第{k+1}期 {d} 正常还款 {amt}")

    hr("6. 利率调整：2026-07-01 起 7.2% -> 4.0%（as_of=2026-06-30）")
    before = [i.due_total for i in svc.schedule("DEMO-001")]
    svc.change_rate("DEMO-001", date(2026, 7, 1), "0.04", date(2026, 6, 30))
    after = svc.schedule("DEMO-001")
    print(f"第6期月供 {before[5]} -> 第7期起月供 {after[6].due_total}")
    print(f"[守恒] 全部本金段合计 = {sum(i.principal for i in after)}（= 剩余本金）")

    hr("7. 提前还款 20,000（KEEP_TERM 期数不变、月供下降）")
    rec = svc.prepay("DEMO-001", "PP-1", "20000.00", date(2026, 6, 30),
                     mode=PrepayMode.KEEP_TERM)
    print(f"提前还本 {rec.allocation.principal}，期数仍为 {len(svc.schedule('DEMO-001'))}")

    hr("8. 状态事件流（可追溯）")
    for e in svc.events("DEMO-001"):
        if e["event_type"] in ("STATE_CHANGED", "RATE_CHANGED", "PREPAYMENT_POSTED"):
            print(f"  seq={e['seq']} {e['event_type']} {e['detail']}")

    print("\n演示完成。HTTP 方式可运行: uv run python main.py 后访问 /docs")


if __name__ == "__main__":
    main()
