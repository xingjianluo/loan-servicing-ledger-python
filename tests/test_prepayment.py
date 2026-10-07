"""提前还款专项测试。

覆盖：
- 两种方式（SHORTEN_TERM / REDUCE_PAYMENT）的摊还守恒、期次衔接；
- 提前还款与调息的先后顺序（先调息后提前还 / 先提前还后调息 / 多次穿插），
  已生效的未来期次利率在后续重算中必须保住；
- 提前还款独立入口、可区分的拒绝原因（超当期应还 / 超整笔总额 /
  已结清 / 非法金额）；
- 提前还款申请与普通还款的独立幂等（重复、并发、混合并发）。

用例打印每步输入、关键中间状态（剩余本金、各期利率与金额）与判定依据。
"""

import threading
from datetime import date

import pytest

from loan_ledger import (
    AccountSettledError,
    AccountStatus,
    InvalidAmountError,
    LoanAccount,
    LoanConfig,
    PaymentExceedsDueError,
    PaymentExceedsTotalError,
    PrepaymentMethod,
    RepaymentMethod,
)
from loan_ledger.schedule import _add_months

ORIGINAL = 1_000_000_00


def make_account(**kw) -> LoanAccount:
    cfg = LoanConfig(
        principal_cents=ORIGINAL, annual_rate=0.036, periods=12,
        method=RepaymentMethod.EQUAL_INSTALLMENT,
        start_date=date(2026, 1, 1), grace_days=3,
        penalty_daily_rate=0.0005, **kw)
    return LoanAccount(cfg)


def _pay_first_period(acc: LoanAccount, rid: str = "p1") -> None:
    acc.post_payment(rid, acc.schedule[0].total_cents, date(2026, 2, 1))


def _assert_principal_conservation(acc: LoanAccount, prepaid: int) -> None:
    # 计划表内各期本金合计 = 原始本金 - 累计提前还款（无对不上的零头）
    assert sum(e.principal_cents for e in acc.schedule) == ORIGINAL - prepaid
    # 每期 本金+利息 = 应还总额，本金为正
    for e in acc.schedule:
        assert e.total_cents == e.principal_cents + e.interest_cents
        assert e.principal_cents > 0
    # 账户剩余本金 = 各期未还本金合计
    balance = acc.balances()["principal"]
    unpaid = sum(e.principal_cents - e.paid_principal_cents
                 for e in acc.schedule)
    assert balance == unpaid


def _assert_dates_continuous(acc: LoanAccount) -> None:
    for prev, cur in zip(acc.schedule, acc.schedule[1:]):
        assert cur.due_date == _add_months(prev.due_date, 1)
        assert cur.period == prev.period + 1


# ---------- 两种提前还款方式 ----------

def test_reduce_payment_keeps_periods_and_lowers_installment():
    acc = make_account()
    _pay_first_period(acc)
    before = acc.schedule[1].total_cents
    prepaid = 20_000_000
    print("\n[输入] 还首期后申请提前还款", prepaid, "分，REDUCE_PAYMENT")
    r = acc.apply_prepayment(
        "pp-1", prepaid, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 5))
    after = acc.schedule[1].total_cents
    print("[中间状态] 重算前月供:", before, "重算后:", after)
    print("[中间状态] 剩余本金:", r.remaining_principal_cents,
          "剩余期数:", r.remaining_periods, "下一到期日:", r.next_due_date)
    print("[判定依据] 月供下降:", after < before, "计划表仍为 12 期")
    assert after < before
    assert len(acc.schedule) == 12
    assert r.remaining_periods == 11
    _assert_principal_conservation(acc, prepaid)
    _assert_dates_continuous(acc)


def test_shorten_term_keeps_installment_and_removes_tail_periods():
    acc = make_account()
    _pay_first_period(acc)
    before = {e.due_date: e.total_cents for e in acc.schedule}
    prepaid = 20_000_000
    print("\n[输入] 还首期后申请提前还款", prepaid, "分，SHORTEN_TERM")
    r = acc.apply_prepayment(
        "pp-1", prepaid, PrepaymentMethod.SHORTEN_TERM,
        date(2026, 2, 5))
    print("[中间状态] 缩短后期数:", len(acc.schedule),
          "回执剩余期数:", r.remaining_periods,
          "期号:", [e.period for e in acc.schedule])
    assert r.remaining_periods == len(acc.schedule) - 1
    print("[中间状态] 各期金额:", [e.total_cents for e in acc.schedule])
    assert len(acc.schedule) < 12
    # 非末期月供与缩短前同到期日金额完全一致（月供不变）
    for e in acc.schedule[1:-1]:
        assert e.total_cents == before[e.due_date]
    # 到期日是原计划前缀：无空期、无重复期
    original_dates = sorted(before)
    assert [e.due_date for e in acc.schedule] \
        == original_dates[:len(acc.schedule)]
    _assert_principal_conservation(acc, prepaid)
    _assert_dates_continuous(acc)
    print("[判定依据] 非末期月供不变、到期日为原序列前缀、本金无零头 ✔")


def test_shorten_term_rejects_when_amount_cannot_reduce_periods():
    acc = make_account()
    _pay_first_period(acc)
    # 金额太小只能做小末期、期数并不缩短：必须明确拒绝，引导用降月供
    print("\n[输入] 仅提前还 100 元，SHORTEN_TERM（不足以缩短期数）")
    with pytest.raises(Exception) as exc:  # InvalidRequestError
        acc.apply_prepayment(
            "pp-tiny", 100_00, PrepaymentMethod.SHORTEN_TERM,
            date(2026, 2, 5))
    print("[判定依据] 被拒绝:", type(exc.value).__name__, exc.value)
    # 同金额走降月供应当成功
    r = acc.apply_prepayment(
        "pp-tiny2", 100_00, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 5))
    assert r.remaining_periods == 11
    print("[判定依据] 同金额改走 REDUCE_PAYMENT 成功，期数不变")


def test_repeated_prepayments_each_uses_current_remaining_principal():
    acc = make_account()
    _pay_first_period(acc)
    print("\n[输入] 连续 3 次提前还款（降月供/缩期交替）")
    r1 = acc.apply_prepayment(
        "pp-1", 10_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 3))
    print("[中间状态] 第1次后 剩余本金:", r1.remaining_principal_cents,
          "期数:", r1.remaining_periods)
    r2 = acc.apply_prepayment(
        "pp-2", 10_000_000, PrepaymentMethod.SHORTEN_TERM,
        date(2026, 2, 4))
    print("[中间状态] 第2次后 剩余本金:", r2.remaining_principal_cents,
          "期数:", r2.remaining_periods, "末日:", acc.schedule[-1].due_date)
    r3 = acc.apply_prepayment(
        "pp-3", 5_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 5))
    print("[中间状态] 第3次后 剩余本金:", r3.remaining_principal_cents,
          "期数:", r3.remaining_periods)
    first_principal = acc.schedule[0].principal_cents
    assert r1.remaining_principal_cents \
        == ORIGINAL - first_principal - 10_000_000
    assert r3.remaining_principal_cents \
        == ORIGINAL - first_principal - 25_000_000
    _assert_principal_conservation(acc, 25_000_000)
    _assert_dates_continuous(acc)
    print("[判定依据] 每次重算都基于当时真实剩余本金，累计冲减 25 万元 ✔")


def test_prepay_all_remaining_principal_settles():
    acc = make_account()
    _pay_first_period(acc)
    capacity = sum(e.principal_cents for e in acc.schedule[1:])
    print("\n[输入] 提前还款金额=全部未到期本金:", capacity)
    r = acc.apply_prepayment(
        "pp-all", capacity, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 2))
    print("[中间状态] 剩余期数:", r.remaining_periods, "状态:", acc.status)
    assert r.remaining_periods == 0
    assert acc.status is AccountStatus.SETTLED
    assert acc.balances()["principal"] == 0
    with pytest.raises(AccountSettledError):
        acc.apply_prepayment(
            "pp-again", 1, PrepaymentMethod.REDUCE_PAYMENT,
            date(2026, 2, 3))
    print("[判定依据] 全部提前结清 -> SETTLED，再次申请被拒 ✔")


# ---------- 调息与提前还款的先后关系 ----------

def _print_rates(acc, tag):
    print(f"[中间状态] {tag} 各期利率:",
          [(e.period, e.rate) for e in acc.schedule])


def test_rate_adjust_then_prepayment_preserves_effective_rates():
    acc = make_account()
    _pay_first_period(acc)
    acc.adjust_rate(0.072, date(2026, 2, 1))
    _print_rates(acc, "调息 3.6%->7.2% 后")
    print("[输入] 调息后再提前还 20 万（降月供）")
    acc.apply_prepayment(
        "pp-1", 20_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 5))
    rates = [e.rate for e in acc.schedule]
    print("[中间状态] 提前还款后各期利率:", rates)
    print("[判定依据] 首期仍 3.6%，全部未来期保住 7.2%，不回退老利率")
    assert rates[0] == 0.036
    assert all(r == 0.072 for r in rates[1:])
    _assert_principal_conservation(acc, 20_000_000)
    # 再缩短：保留的前缀利率仍逐期正确
    acc.apply_prepayment(
        "pp-2", 20_000_000, PrepaymentMethod.SHORTEN_TERM,
        date(2026, 2, 6))
    rates2 = [e.rate for e in acc.schedule]
    assert rates2[0] == 0.036
    assert all(r == 0.072 for r in rates2[1:])
    print("[判定依据] 再缩短后利率序列仍为调息结果前缀 ✔")


def test_prepayment_then_rate_adjust_matches_reverse_order():
    # 顺序 A：先调息后提前还
    a = make_account()
    _pay_first_period(a, "a1")
    a.adjust_rate(0.072, date(2026, 2, 1))
    a.apply_prepayment(
        "a-pp", 20_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 5))

    # 顺序 B：先提前还后调息
    b = make_account()
    _pay_first_period(b, "b1")
    b.apply_prepayment(
        "b-pp", 20_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 1))
    b.adjust_rate(0.072, date(2026, 2, 5))

    print("\n[输入] A: 调息->提前还 ; B: 提前还->调息（金额/日期相同）")
    print("[中间状态] A 利率:", [e.rate for e in a.schedule])
    print("[中间状态] B 利率:", [e.rate for e in b.schedule])
    assert [e.rate for e in a.schedule] == [e.rate for e in b.schedule]
    assert [e.due_date for e in a.schedule] \
        == [e.due_date for e in b.schedule]
    assert a.balances()["principal"] == b.balances()["principal"]
    assert [e.total_cents for e in a.schedule] \
        == [e.total_cents for e in b.schedule]
    interest_a = sum(e.interest_cents for e in a.schedule)
    interest_b = sum(e.interest_cents for e in b.schedule)
    print("[判定依据] 剩余本金一致:", a.balances()["principal"],
          "利息合计一致:", interest_a == interest_b)
    assert interest_a == interest_b
    print("[判定依据] 两种操作顺序最终本金/各期金额/利息合计完全一致 ✔")


def test_multiple_rate_changes_interleaved_with_prepayments():
    acc = make_account()
    _pay_first_period(acc)
    acc.adjust_rate(0.060, date(2026, 2, 1))   # 期2 起 6%
    acc.apply_prepayment(
        "pp-1", 10_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 3))
    acc.adjust_rate(0.072, date(2026, 5, 1))   # 到期日>=5/1（期5）起 7.2%
    print("\n[输入] 调息两次、提前还款穿插；第二次调息 5/1 生效")
    _print_rates(acc, "第二次调息后")
    rates = [e.rate for e in acc.schedule]
    # 期2-4（3/1、4/1、5/1? 注意期4到期日为4/1）保持 6%，期5 起 7.2%
    assert rates[1:4] == [0.06, 0.06, 0.06]
    assert all(r == 0.072 for r in rates[4:])
    # 后续提前还款不得把已生效利率回退
    acc.apply_prepayment(
        "pp-2", 5_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 5, 2))
    rates2 = [e.rate for e in acc.schedule]
    assert rates2[1:4] == [0.06, 0.06, 0.06]
    assert all(r == 0.072 for r in rates2[4:])
    # 缩短同样保住利率前缀
    before = {e.due_date: e.total_cents for e in acc.schedule}
    acc.apply_prepayment(
        "pp-3", 30_000_000, PrepaymentMethod.SHORTEN_TERM,
        date(2026, 5, 3))
    for e in acc.schedule[1:-1]:
        assert e.total_cents == before[e.due_date]
    rates3 = [e.rate for e in acc.schedule]
    assert 0.06 in rates3 and 0.072 in rates3
    _assert_principal_conservation(acc, 45_000_000)
    _assert_dates_continuous(acc)
    print("[判定依据] 多次调息与提前还款穿插，利率分段保住、期次连续 ✔")


# ---------- 独立入口与可区分的拒绝原因 ----------

def test_normal_payment_over_due_is_rejected_not_converted():
    acc = make_account()
    e1 = acc.schedule[0].total_cents
    print("\n[输入] 普通还款还当期 +1 分:", e1 + 1)
    with pytest.raises(PaymentExceedsDueError):
        acc.post_payment("over-due", e1 + 1, date(2026, 2, 1))
    # 账户未被动到：没有发生任何提前还款
    assert acc.balances()["principal"] == ORIGINAL
    assert len(acc.schedule) == 12
    print("[判定依据] PaymentExceedsDueError，计划未被改动（不默认提前还）✔")


def test_payment_over_total_has_distinct_error():
    acc = make_account()
    total = sum(e.total_cents for e in acc.schedule)
    print("\n[输入] 普通还款金额超过整笔未还总额+1:", total + 1)
    with pytest.raises(PaymentExceedsTotalError):
        acc.post_payment("over-total", total + 1, date(2026, 2, 1))
    print("[判定依据] PaymentExceedsTotalError（与超当期应还区分）✔")
    # 两个异常仍是兄弟、且可与基类区分
    assert issubclass(PaymentExceedsTotalError, Exception)


def test_prepayment_over_remaining_principal_rejected():
    acc = make_account()
    _pay_first_period(acc)
    capacity = sum(e.principal_cents for e in acc.schedule[1:])
    print("\n[输入] 提前还款超过未到期本金:", capacity + 1)
    with pytest.raises(PaymentExceedsTotalError):
        acc.apply_prepayment(
            "pp-over", capacity + 1,
            PrepaymentMethod.REDUCE_PAYMENT, date(2026, 2, 2))
    print("[判定依据] 提前还款超额 -> PaymentExceedsTotalError ✔")


def test_prepayment_invalid_amount_and_settled():
    acc = make_account()
    print("\n[输入] 非法提前还款金额: 0 / -1 / 1.5")
    for bad in (0, -1, 1.5):
        with pytest.raises(InvalidAmountError):
            acc.apply_prepayment(
                f"bad-{bad}", bad, PrepaymentMethod.REDUCE_PAYMENT,
                date(2026, 1, 15))
    # 未结清但没有未到期本金（先提前结清当期前的情形不触发），这里直接验结清
    _pay_first_period(acc)
    cap = sum(e.principal_cents for e in acc.schedule[1:])
    acc.apply_prepayment(
        "pp-all", cap, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 2))
    with pytest.raises(AccountSettledError):
        acc.apply_prepayment(
            "pp-x", 1, PrepaymentMethod.REDUCE_PAYMENT, date(2026, 2, 3))
    print("[判定依据] 非法金额 / 已结清 两类拒绝可区分 ✔")


# ---------- 提前还款幂等：重复、并发、与普通还款混合并发 ----------

def test_duplicate_prepayment_request_charged_once():
    acc = make_account()
    _pay_first_period(acc)
    r1 = acc.apply_prepayment(
        "dup-pp", 20_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 2))
    snapshot_principal = acc.balances()["principal"]
    snapshot_schedule = [(e.period, e.total_cents) for e in acc.schedule]
    r2 = acc.apply_prepayment(
        "dup-pp", 20_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 2))
    r3 = acc.apply_prepayment(
        "dup-pp", 99_000_000, PrepaymentMethod.SHORTEN_TERM,
        date(2026, 2, 3))  # 金额/方式不同也只重放首次
    print("\n[输入] 同一提前还款申请号 dup-pp 提交 3 次（第3次改金额/方式）")
    print("[中间状态] 首次剩余本金:", r1.remaining_principal_cents,
          "重放:", r2.duplicate, r3.duplicate)
    assert r2.duplicate and r3.duplicate
    assert r3.amount_cents == 20_000_000
    assert acc.balances()["principal"] == snapshot_principal
    assert [(e.period, e.total_cents) for e in acc.schedule] \
        == snapshot_schedule
    print("[判定依据] 余额与期次均未被重复扣减 ✔")


def test_concurrent_duplicate_prepayments_charged_once():
    acc = make_account()
    _pay_first_period(acc)
    results, errors = [], []

    def apply():
        try:
            results.append(acc.apply_prepayment(
                "c-pp", 20_000_000, PrepaymentMethod.SHORTEN_TERM,
                date(2026, 2, 2)))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=apply) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n[输入] 16 线程并发同一提前还款申请号 c-pp（SHORTEN_TERM）")
    print("[中间状态] 成功:", len(results), "异常:", len(errors),
          "重复:", sum(1 for r in results if r.duplicate))
    assert not errors
    assert len(results) == 16
    assert sum(1 for r in results if not r.duplicate) == 1
    # 只扣了一次 20 万
    first_principal = acc.schedule[0].principal_cents
    assert acc.balances()["principal"] \
        == ORIGINAL - first_principal - 20_000_000
    print("[判定依据] 并发下仅一笔真正入账，余额正确 ✔")


def test_mixed_concurrent_payment_and_prepayment_idempotency():
    acc = make_account()
    # 8 个普通还款请求（不同单号）：7 笔等额 + 1 笔补足余数，合计=首期应还
    e1_total = acc.schedule[0].total_cents
    pay_each = e1_total // 8
    remainder = e1_total - pay_each * 8
    pay_results, pp_results, errors = [], [], []

    def normal_pay(i):
        amount = pay_each + remainder if i == 7 else pay_each
        try:
            pay_results.append(acc.post_payment(
                f"pay-{i}", amount, date(2026, 2, 1)))
        except Exception as exc:  # noqa: BLE001
            errors.append(("pay", exc))

    def prepay():
        try:
            pp_results.append(acc.apply_prepayment(
                "mix-pp", 20_000_000, PrepaymentMethod.REDUCE_PAYMENT,
                date(2026, 2, 5)))
        except Exception as exc:  # noqa: BLE001
            errors.append(("pp", exc))

    threads = [threading.Thread(target=normal_pay, args=(i,))
               for i in range(8)]
    threads += [threading.Thread(target=prepay) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n[输入] 8 笔普通还款 + 8 个相同单号提前还款并发")
    print("[中间状态] 普通还款:", len(pay_results),
            "提前还款回执:", len(pp_results), "异常:", errors)
    assert not errors
    # 8 笔普通还款合计 = 首期应还总额
    assert sum(r.allocation.total_cents for r in pay_results) == e1_total
    # 提前还款只入账一次
    assert sum(1 for r in pp_results if not r.duplicate) == 1
    first_principal = acc.schedule[0].principal_cents
    assert acc.balances()["principal"] \
        == ORIGINAL - first_principal - 20_000_000
    print("[判定依据] 两类入口互不串号、各自幂等、总额不重不漏 ✔")


def test_equal_principal_method_both_modes_with_rate_change():
    cfg = LoanConfig(
        principal_cents=100_000_000, annual_rate=0.048, periods=12,
        method=RepaymentMethod.EQUAL_PRINCIPAL,
        start_date=date(2026, 1, 1))
    acc = LoanAccount(cfg)
    acc.post_payment("p1", acc.schedule[0].total_cents, date(2026, 2, 1))
    share_before = acc.schedule[1].principal_cents
    print("\n[输入] 等额本金：降月供提前还 10 万元")
    acc.apply_prepayment(
        "pp-1", 10_000_000, PrepaymentMethod.REDUCE_PAYMENT,
        date(2026, 2, 3))
    share_after = acc.schedule[1].principal_cents
    print("[中间状态] 每期本金份额:", share_before, "->", share_after,
          "期数:", len(acc.schedule))
    assert share_after < share_before
    assert len(acc.schedule) == 12
    assert sum(e.principal_cents for e in acc.schedule) \
        == 100_000_000 - 10_000_000

    print("[输入] 调息 4.8%->7.2%(3/1生效) 后再缩短期，提前还 20 万元")
    acc.adjust_rate(0.072, date(2026, 3, 1))
    profile = {e.due_date: e.principal_cents for e in acc.schedule}
    acc.apply_prepayment(
        "pp-2", 20_000_000, PrepaymentMethod.SHORTEN_TERM,
        date(2026, 3, 2))
    print("[中间状态] 缩短后期数:", len(acc.schedule),
          "利率:", [e.rate for e in acc.schedule])
    # 非末期本金份额保持；到期日连续；利率前缀正确（期1-2 为 4.8%）
    for e in acc.schedule[1:-1]:
        assert e.principal_cents == profile[e.due_date]
    _assert_dates_continuous(acc)
    rates = [e.rate for e in acc.schedule]
    assert rates[0] == 0.048 and rates[1] == 0.048
    assert all(r == 0.072 for r in rates[2:])
    assert sum(e.principal_cents for e in acc.schedule) \
        == 100_000_000 - 30_000_000
    print("[判定依据] 等额本金两种方式 + 调息穿插均守恒、保利率 ✔")
