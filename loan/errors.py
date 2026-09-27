"""贷款服务领域错误类型。

所有业务错误继承 :class:`LoanError`，不同失败原因对应不同子类，
调用方可按异常类型稳定区分拒绝原因。
"""


class LoanError(Exception):
    """贷款领域错误基类。"""


class InvalidAmountError(LoanError):
    """非法金额（非数字、负数、零、精度超限等）。"""


class InvalidScheduleError(LoanError):
    """贷款条款非法，无法生成摊还计划。"""


class RateAdjustmentError(LoanError):
    """利率调整请求非法（日期/利率不合法或无生效期间）。"""


class PaymentExceedsDueError(LoanError):
    """还款金额超出当前允许偿还的范围。"""

    def __init__(self, message: str, *, allowed: "object | None" = None):
        super().__init__(message)
        self.allowed = allowed


class AccountClosedError(LoanError):
    """账户已结清，拒绝再次入账。"""


class DuplicatePaymentError(LoanError):
    """幂等冲突：同一幂等键曾以不同请求内容提交。"""


class AccountStateError(LoanError):
    """账户状态不允许该操作。"""
