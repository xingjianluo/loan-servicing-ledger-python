"""提前还款申请测试：两种方式、调息叠加顺序、幂等并发、拒绝原因区分。"""

import threading
from datetime import date, timedelta

import pytest

from loan_ledger import (
    AccountSettledError,
    ExceedsDueAmountError,
    ExcessPaymentError,
    InvalidAmountError,
    LoanAccount,
    LoanConfig,
    PrepaymentMode,
    RepaymentMethod,
)

START = date(2026, 1, 1)


def make_account(**kw) -> LoanAccount:
    cfg = {
        "principal_cents": 1_000_000_00, "annual_rate": 0.036,
        "periods": 12, "method": RepaymentMethod.EQUAL_INSTALLMENT,
        "start_date": START, "grace_days": 3,
        "penalty_daily_rate": 0.0005,
    }
    cfg.update(kw)
    return LoanAccount(LoanConfig(**cfg))


def assert_conservation(acc, label=""):
    """守恒校验：每期 本金+利息=应还；各期未还本金合计=账户剩余本金。"""
    for e in acc.schedule:
        assert e.total_cents == e.principal_cents + e.interest_cents
        assert e.principal_cents >= 0 and e.interest_cents >= 0
    unpaid = sum(e.principal_cents - e.paid_principal_cents
                 for e in acc.schedule)
    assert unpaid == acc.balances()["principal"], label
    # 期号连续、到期日逐月连续（无空期、无重复期）
    for prev, cur in zip(acc.schedule, acc.schedule[1:]):
        assert cur.period == prev.period + 1, label
        assert cur.due_date > prev.due_date, label
        assert cur.due_date <= prev.due_date + timedelta(days=31), label
    assert len({e.period for e in acc.schedule}) == len(acc.schedule), label


def test_reduce_term_shortens_periods_keeps_installment():
    acc = make_account()
    keep = acc.schedule[1].total_cents  # 第 2 期月供（重算应保持不变）
    n_before = len(acc.schedule)
    prepay = 30_000_000
    r = acc.apply_prepayment("t1", prepay, date(2026, 1, 5),
                             PrepaymentMode.REDUCE_TERM)
    print("\n[输入] 等额本息缩短期限，提前还(分):", prepay,
          "保持月供:", keep)
    print("[中间状态] 期数:", n_before, "->", r.periods_after,
          "剩余本金:", r.principal_before_cents, "->",
          r.principal_after_cents)
    new_entries = acc.schedule[1:]  # 第 1 期未到期但未到还款日，仍在
    print("[中间状态] 重算后各期:",
          [(e.period, str(e.due_date), e.total_cents) for e in new_entries])
    print("[判定依据] 月供保持不变:",
          all(e.total_cents == keep for e in new_entries[:-1]))
    print("[判定依据] 末期应还 <= 月供:", new_entries[-1].total_cents <= keep)
    assert r.periods_after < n_before
    assert all(e.total_cents == keep for e in new_entries[:-1])
    assert new_entries[-1].total_cents <= keep
    assert r.principal_after_cents == r.principal_before_cents - prepay
    assert_conservation(acc, "缩短期限守恒")


def test_reduce_installment_keeps_periods_lowers_payment():
    acc = make_account()
    n_before = len(acc.schedule)
    before = acc.schedule[1].total_cents
    prepay = 30_000_000
    r = acc.apply_prepayment("i1", prepay, date(2026, 1, 5),
                             PrepaymentMode.REDUCE_INSTALLMENT)
    after = acc.schedule[1].total_cents
    print("\n[输入] 等额本息降低月供，提前还(分):", prepay)
    print("[中间状态] 期数:", n_before, "->", r.periods_after,
          "月供:", before, "->", after)
    print("[判定依据] 期数不变:", r.periods_after == n_before,
          "月供下降:", after < before)
    assert r.periods_after == n_before
    assert after < before
    assert_conservation(acc, "降低月供守恒")


def test_reduce_term_equal_principal_keeps_base():
    acc = make_account(method=RepaymentMethod.EQUAL_PRINCIPAL)
    base = acc.schedule[1].principal_cents  # 每期本金基数
    n_before = len(acc.schedule)
    prepay = 25_000_000
    r = acc.apply_prepayment("ep1", prepay, date(2026, 1, 5),
                             PrepaymentMode.REDUCE_TERM)
    new_entries = acc.schedule[1:]
    print("\n[输入] 等额本金缩短期限，提前还(分):", prepay,
          "保持每期本金基数:", base)
    print("[中间状态] 期数:", n_before, "->", r.periods_after,
          "各期本金:", [e.principal_cents for e in new_entries])
    print("[判定依据] 非末期本金基数不变:",
          all(e.principal_cents == base for e in new_entries[:-1]))
    assert r.periods_after < n_before
    assert all(e.principal_cents == base for e in new_entries[:-1])
    assert new_entries[-1].principal_cents <= base
    assert_conservation(acc, "等额本金缩短期限守恒")


def test_repeated_prepayments_recalc_from_actual_balance():
    acc = make_account()
    r1 = acc.apply_prepayment("r1", 10_000_000, date(2026, 1, 5),
                              PrepaymentMode.REDUCE_INSTALLMENT)
    mid = acc.schedule[1].total_cents
    r2 = acc.apply_prepayment("r2", 10_000_000, date(2026, 1, 10),
                              PrepaymentMode.REDUCE_INSTALLMENT)
    r3 = acc.apply_prepayment("r3", 5_000_000, date(2026, 1, 15),
                              PrepaymentMode.REDUCE_TERM)
    print("\n[输入] 连续三次提前还款: 1000万(降月供) + 1000万(降月供)"
          " + 500万(缩期)")
    print("[中间状态] 剩余本金:", r1.principal_after_cents, "->",
          r2.principal_after_cents, "->", r3.principal_after_cents)
    print("[中间状态] 第二次后月供:", mid,
          "第三次后期数:", r3.periods_after)
    print("[判定依据] 每次都按当时真实剩余本金重算:",
          r2.principal_before_cents == r1.principal_after_cents
          and r3.principal_before_cents == r2.principal_after_cents)
    assert r2.principal_before_cents == r1.principal_after_cents
    assert r3.principal_before_cents == r2.principal_after_cents
    assert acc.balances()["principal"] == 1_000_000_00 - 25_000_000
    assert_conservation(acc, "反复提前还款守恒")


def test_rate_adjust_then_prepayment_preserves_rate():
    """先调息再提前还：已生效利率在提前还款重算后必须保住。"""
    acc = make_account()
    acc.adjust_rate(0.072, date(2026, 1, 10))
    rates_before = [e.rate for e in acc.schedule[1:]]
    interest_before = sum(e.interest_cents for e in acc.schedule[1:])
    r = acc.apply_prepayment("pp1", 20_000_000, date(2026, 1, 15),
                             PrepaymentMode.REDUCE_INSTALLMENT)
    rates_after = [e.rate for e in acc.schedule[1:]]
    interest_after = sum(e.interest_cents for e in acc.schedule[1:])
    print("\n[输入] 先调息 0.036->0.072，再提前还 2000万(降月供)")
    print("[中间状态] 调息后未来利率:", set(rates_before),
          "提前还后未来利率:", set(rates_after))
    print("[中间状态] 未来利息合计:", interest_before, "->", interest_after)
    print("[判定依据] 调息结果未回退:",
          all(rate == 0.072 for rate in rates_after))
    assert all(rate == 0.072 for rate in rates_after)
    # 对照：若错误地按老利率 0.036 重算，利息会显著更低
    from loan_ledger.schedule import recalc_schedule
    wrong = recalc_schedule(r.principal_after_cents, 0.036,
                            len(acc.schedule) - 1, acc.config.method,
                            acc.schedule[1].due_date, 2)
    wrong_interest = sum(e.interest_cents for e in wrong)
    print("[判定依据] 若按老利率重算利息:", wrong_interest,
          "应明显低于实际:", interest_after)
    assert interest_after > wrong_interest
    assert_conservation(acc, "先调息再提前还守恒")


def test_prepayment_then_rate_adjust_then_prepayment():
    """先提前还再调息、再提前还：任意穿插后账目仍守恒、利率正确。"""
    acc = make_account()
    acc.apply_prepayment("s1", 10_000_000, date(2026, 1, 5),
                         PrepaymentMode.REDUCE_TERM)
    n_after_first = len(acc.schedule)
    acc.adjust_rate(0.06, date(2026, 1, 10))
    assert all(e.rate == 0.06 for e in acc.schedule[1:])
    r = acc.apply_prepayment("s2", 10_000_000, date(2026, 1, 15),
                             PrepaymentMode.REDUCE_INSTALLMENT)
    print("\n[输入] 提前还1000万(缩期) -> 调息0.06 -> 再提前还1000万(降月供)")
    print("[中间状态] 第一次后期数:", n_after_first,
          "第二次后剩余本金:", r.principal_after_cents)
    print("[中间状态] 未来各期利率:",
          sorted({e.rate for e in acc.schedule[1:]}))
    print("[判定依据] 调息后利率在第二次提前还中保住:",
          all(e.rate == 0.06 for e in acc.schedule[1:]))
    assert all(e.rate == 0.06 for e in acc.schedule[1:])
    assert acc.balances()["principal"] == 1_000_000_00 - 20_000_000
    assert_conservation(acc, "先提前还再调息守恒")


def test_normal_payment_over_due_rejected_not_auto_prepaid():
    acc = make_account()
    e1 = acc.schedule[0]
    due = e1.total_cents
    snapshot = acc.balances()
    with pytest.raises(ExceedsDueAmountError) as exc:
        acc.post_payment("over-due", due + 1, date(2026, 2, 1))
    print("\n[输入] 普通还款金额 = 当期到期应还 + 1 =", due + 1)
    print("[判定依据] 拒绝原因:", exc.value)
    assert "提前还款" in str(exc.value)
    assert acc.balances() == snapshot  # 账户未变动
    # 恰好等于到期应还则可以入账
    r = acc.post_payment("exact", due, date(2026, 2, 1))
    assert r.allocation.total_cents == due
    print("[中间状态] 等额到期应还入账成功:", r.allocation)


def test_rejection_reasons_are_distinguishable():
    acc = make_account()
    e1_due = acc.schedule[0].total_cents
    outstanding = sum(e.total_cents for e in acc.schedule)
    remaining_principal = acc.balances()["principal"]
    print("\n[输入] 到期应还:", e1_due, "全部应还:", outstanding,
          "剩余本金:", remaining_principal)
    # 1. 金额本身不合法
    with pytest.raises(InvalidAmountError):
        acc.post_payment("bad1", 0, date(2026, 2, 1))
    with pytest.raises(InvalidAmountError):
        acc.apply_prepayment("bad2", -5, date(2026, 2, 1),
                             PrepaymentMode.REDUCE_TERM)
    # 2. 普通还款：超过当期到期应还（但未超全部应还）
    with pytest.raises(ExceedsDueAmountError):
        acc.post_payment("bad3", e1_due + 1, date(2026, 2, 1))
    # 3. 普通还款：超过整笔未还总额
    with pytest.raises(ExcessPaymentError):
        acc.post_payment("bad4", outstanding + 1, date(2026, 2, 1))
    # 4. 提前还款：超过剩余本金
    with pytest.raises(ExcessPaymentError):
        acc.apply_prepayment("bad5", remaining_principal + 1,
                             date(2026, 2, 1), PrepaymentMode.REDUCE_TERM)
    # 5. 账户已结清
    acc.post_payment("ok", e1_due, date(2026, 2, 1))
    acc.apply_prepayment("settle", acc.balances()["principal"],
                         date(2026, 2, 1), PrepaymentMode.REDUCE_TERM)
    with pytest.raises(AccountSettledError):
        acc.post_payment("bad6", 1, date(2026, 2, 2))
    with pytest.raises(AccountSettledError):
        acc.apply_prepayment("bad7", 1, date(2026, 2, 2),
                             PrepaymentMode.REDUCE_TERM)
    print("[判定依据] InvalidAmountError / ExceedsDueAmountError /"
          " ExcessPaymentError / AccountSettledError 均可区分，"
          "且失败请求未改变账户")


def test_prepayment_idempotent_replay_and_concurrency():
    acc = make_account()
    r1 = acc.apply_prepayment("dup", 5_000_000, date(2026, 1, 5),
                              PrepaymentMode.REDUCE_TERM)
    snapshot = acc.balances()
    n = len(acc.schedule)
    r2 = acc.apply_prepayment("dup", 5_000_000, date(2026, 1, 5),
                              PrepaymentMode.REDUCE_TERM)
    print("\n[输入] 同一提前还款单号 dup 重复提交")
    print("[中间状态] 重放标记:", r2.duplicate,
          "余额一致:", acc.balances() == snapshot,
          "期数一致:", len(acc.schedule) == n)
    assert r2.duplicate
    assert acc.balances() == snapshot
    assert len(acc.schedule) == n
    assert r2.principal_after_cents == r1.principal_after_cents

    # 并发：同一单号 8 线程 + 不同单号 4 线程 + 普通还款 4 线程混合
    acc2 = make_account()
    results, prepays, errors = [], [], []

    def pay(i):
        try:
            results.append(acc2.post_payment(f"p-{i}", 100_00,
                                             date(2026, 2, 1)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def prepay_same():
        try:
            prepays.append(acc2.apply_prepayment(
                "shared", 5_000_000, date(2026, 2, 1),
                PrepaymentMode.REDUCE_TERM))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def prepay_distinct(i):
        try:
            prepays.append(acc2.apply_prepayment(
                f"pp-{i}", 5_000_000, date(2026, 2, 1),
                PrepaymentMode.REDUCE_INSTALLMENT))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = ([threading.Thread(target=pay, args=(i,)) for i in range(4)]
               + [threading.Thread(target=prepay_same) for _ in range(8)]
               + [threading.Thread(target=prepay_distinct, args=(i,))
                  for i in range(4)])
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    unique_prepay = sum(p.amount_cents for p in prepays if not p.duplicate)
    paid = sum(r.allocation.total_cents for r in results if not r.duplicate)
    print("\n[输入] 并发混合: 4 笔普通还款(各10万分) + 8 线程同一提前单号"
          " + 4 笔不同提前单号(各500万)")
    print("[中间状态] 异常数:", len(errors),
          "提前还款去重后入账:", unique_prepay,
          "普通还款入账:", paid)
    expect = 1_000_000_00 - 5 * 5_000_000  # 同一单号只算一次
    print("[判定依据] 剩余本金:", acc2.balances()["principal"],
          "应等于:", expect)
    assert not errors
    assert unique_prepay == 5 * 5_000_000
    assert sum(1 for p in prepays if p.duplicate) == 7  # 8 线程只算 1 次
    assert acc2.balances()["principal"] == expect
    assert_conservation(acc2, "并发混合守恒")
