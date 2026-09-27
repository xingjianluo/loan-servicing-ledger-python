"""金额工具：统一使用 ``Decimal`` 并按分（0.01）量化，杜绝浮点尾差。

系统内所有金额一律走 :func:`money` 入口，禁止 ``float`` 参与金额计算，
避免 0.1 + 0.2 之类的二进制尾差；分摊计算产生的零碎差额一律按分量化，
并在最后一期做找平，保证守恒（见 amortization 模块）。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .errors import InvalidAmountError

CENT = Decimal("0.01")


def D(value: "int | str | Decimal") -> Decimal:
    """把输入转为安全的 ``Decimal``。

    - 接受 ``int`` / ``str`` / ``Decimal``；
    - 拒绝 ``float``（强制调用方显式转字符串，避免二进制尾差）与 ``bool``；
    - 非法字符串抛 :class:`InvalidAmountError`。
    """
    if isinstance(value, bool):
        raise InvalidAmountError(f"金额不能是布尔值: {value!r}")
    if isinstance(value, float):
        raise InvalidAmountError(
            f"禁止用 float 表示金额: {value!r}；请改用字符串，例如 '100.05'"
        )
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidAmountError(f"非法金额: {value!r}") from exc


def quantize(value: Decimal) -> Decimal:
    """四舍五入量化到分（0.01）。"""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def is_nonneg(value: Decimal) -> bool:
    return value >= 0


def is_positive(value: Decimal) -> bool:
    return value > 0


def rate(value: "int | str | Decimal") -> Decimal:
    """解析利率/费率：允许高于分的精度（如日罚息率 0.0005），必须非负。

    利率不是金额，不做分位量化；但同样禁止 float 直传。
    """
    r = D(value)
    if not is_nonneg(r):
        raise InvalidAmountError(f"利率/费率不能为负: {value!r}")
    return r


def money(
    value: "int | str | Decimal",
    *,
    positive: bool = False,
    nonneg: bool = False,
) -> Decimal:
    """解析并校验一笔外部输入金额。

    - 必须是 0.01 的整数倍（不接受厘及以下精度）；
    - ``positive=True`` 时必须大于 0；``nonneg=True`` 时必须 >= 0；
    - 校验通过后量化到分返回。
    """
    amount = D(value)
    if amount.as_tuple().exponent < -2:
        raise InvalidAmountError(f"金额精度超过分（0.01）: {value!r}")
    if positive and not is_positive(amount):
        raise InvalidAmountError(f"金额必须大于 0: {value!r}")
    if nonneg and not is_nonneg(amount):
        raise InvalidAmountError(f"金额不能为负: {value!r}")
    return quantize(amount)
