"""错误类型：不同失败原因可区分。"""


class LoanLedgerError(Exception):
    """所有贷款账本错误的基类。"""


class InvalidAmountError(LoanLedgerError):
    """非法金额（非正数、非整数分等）。"""


class ExcessPaymentError(LoanLedgerError):
    """还款金额超出当前应还范围。"""


class ExceedsDueAmountError(LoanLedgerError):
    """普通还款金额超过当前到期应还（提前还款须走单独申请）。"""


class AccountSettledError(LoanLedgerError):
    """账户已结清，拒绝再次入账。"""


class InvalidRequestError(LoanLedgerError):
    """其他非法请求（如非法利率、期数等）。"""
