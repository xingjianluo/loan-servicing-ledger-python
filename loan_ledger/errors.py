"""错误类型：不同失败原因可区分。"""


class LoanLedgerError(Exception):
    """所有贷款账本错误的基类。"""


class InvalidAmountError(LoanLedgerError):
    """非法金额（非正数、非整数分等）。"""


class ExcessPaymentError(LoanLedgerError):
    """还款金额超出当前应还范围（基类，便于一并拦截超额类拒绝）。"""


class PaymentExceedsDueError(ExcessPaymentError):
    """普通还款超过“当前已到期应还”金额；不会默认转成提前还款。"""


class PaymentExceedsTotalError(ExcessPaymentError):
    """金额超过整笔贷款尚未还完的总额（含提前还款超过剩余本金）。"""


class AccountSettledError(LoanLedgerError):
    """账户已结清，拒绝再次入账。"""


class InvalidRequestError(LoanLedgerError):
    """其他非法请求（如非法利率、期数等）。"""
