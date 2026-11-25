"""金额纯函数（spec §6.1 / §10 #4；P4 的 B2 与 B3）。 **为什么单独成模块**：金额算错**不会报错，只会算错钱**（P4 已经因为「一处乘、一处除」的量纲不一致出过一次真错）。所以算式只允许有**一处**实现，且必须能被不经 HTTP 直接测试。三个函数、三条纪律： 1. `effective_taker_price(perf)` —— 绩效覆盖价优先，为空才用订单默认价（spec §6.3）。
⚠️ 大厅列表侧的同一事实是 **SQL 注解**（`selectors.effective_taker_price_expression()`）。两个表示法**允许并存**（一个给列表、一个给单行），但必须对得上——有交叉核对测试钉住。 2. `commission_amount(base, percent)` —— **一律 `× percent / 100`**（P4 的 B2：量纲写死 0–100）。
量化到分、`ROUND_HALF_UP`（= 远离零，与 PostgreSQL `numeric` 的舍入一致）。 3. `deduct_amount(*, mode, value, base)` —— P4 的 B3 要求「抽成可复用函数」，因为 P4 的应付净额是 `bonus_amount − 扣点额 + Σ调整记录`，两侧必须**同一口径**。 ⚠️ **不引入任何 `≥ 0` 校验**（D-0015：金额可以为负 = 退款 / 冲抵 / 调整）。扣点额只设**上界**，而且这正是对标物自校验原文的形状（单边）：「扣点比例不能超过100%」「扣点金额不能超过预计绩效总计」（spec §6.1，**读码**）。 ⚠️ 本模块**不碰 request**、**不写库**——只做算术与校验，便于纯逻辑测试。"""
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator

from apps.core.error_codes import ErrorCode

#: 金额量化到分。
CENT = Decimal("0.01")
#: 扣点按比例模式的百分比上界（含）——对标物原文。
PERCENT_MAX = Decimal("100")

#: 扣点模式（`Performance.deduct_mode` 是 `CharField(max_length=1)`，空串 = 未设置）
DEDUCT_PERCENT = "0"    # 按比例扣除
DEDUCT_FIXED = "1"      # 扣除固定金额


class AmountError(Exception):
    """金额**不可计算**或参数越界。`code` 直接给 API 层当业务码用。 ⚠️ 与 `ClaimError` / `CommissionError` 同形（各模块自己的领域错误），
    属性名用 `message` 而不是靠 `str(self)`：调用方要原样把中文理由放进信封的 `msg`。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


class MissingPriceError(AmountError):
    """**「两边都没有价」**这一档：绩效覆盖价与订单默认价都为空 ⇒ 金额**无从算起**。 ⭐（**F2**）：单独成类，是因为**有一个调用方需要只吞这一档** ——`apps/business/jidanpu_upstream.py::promote_upstream_mirror` 转正大厅镜像单时， ⚠️ **上家压根没给价**那一档 `hall_price` 曾会留空 ⇒ `set_taker` 里的重算那一步必然抛。 ⚠️ **口径修订（业务方）**：上家没给价 ⇒ 发单价按 `0.00` 算
    （`upstream_hall_price` 恒回 `Decimal`，⛔ 不再回 `None`）⇒ **转正那一路不再走到这一档**，那个 `except` 已成防御性分支。本类仍然有效：**原生单**等路径上 `Order.hall_price` 可以是 `NULL`，`effective_taker_price` 对「两边都没价」照旧**拒绝**而不是算 0。判据仍必须落在 **异常类型**上： ⚠️ 它原来写成「当时 `order.hall_price is None`」——那个判据**恒假**（这条路径上的单
    **就是没价建的**），既分辨不了别的 `AmountError`，把它改成 `if False:` 也一条用例都不红（评审实测 **SURVIVED**；`AGENTS.md` §5-E / **E-0063** 同族：恒真的判据在生产代码里是**说假话**）。改成按类型判之后，别的 `AmountError` 会**响亮上抛**，而这是**能被用例弄红**的（见 `tests/test_jidanpu_upstream_consume.py` 的 `test_a_different_amount_error_on_promotion_is_not_swallowed`）。 ⚠️ 它是 `AmountError` 的子类 ⇒ 既有的 `except AmountError`（`/claim` 的 NULL 价分支、批量指派的 `1002` 等）**一个字都不用改**。"""


def ensure_amount_fits(value, field):
    """`value` 能否落进 `field` 这一列；不能 ⇒ `AmountError`（业务码，**不是** 500）。 **为什么需要它**（R-10c）：`/adjust-amount/` 写库的是**反推出来的**基价 `base = V ÷ rate × 100`，而入口序列化器只保证 **V** 自己 ≤ 它的 `max_digits`
    ——低提成率会把 V 放大若干个量级（25% 档下 base = V × 4，1% 档下 = V × 100） ⇒ 超出列宽时 PostgreSQL 抛 `DataError`（HTTP 500）。超界是**可预期**的输入问题，按本项目纪律必须翻成业务码（E-0010：预期失败不许伪装成 500）。
    ⚠️ **不硬编码 12/2**：位宽从**目标列自己**读（`field.max_digits` / `field.decimal_places`）——列宽改了这里跟着改，同一事实只有一处（E-0021 同族）。 ⚠️ 只做**位宽**校验；`0` 与负数都合法（D-0015），本函数不设下界。"""
    max_digits = getattr(field, "max_digits", None)
    if max_digits is None:                       # 非 DecimalField ⇒ 没有位宽可判
        return value
    decimal_places = getattr(field, "decimal_places", None) or 0
    try:
        DecimalValidator(max_digits, decimal_places)(value)
    except ValidationError:
        raise AmountError(
            f"金额超出可记录范围（本列最多 {max_digits - decimal_places} 位整数、"
            f"{decimal_places} 位小数）",
            ErrorCode.VALIDATION,
        )
    return value


def effective_taker_price(performance, *, order=None):
    """生效接单价：绩效覆盖价优先，为空才用订单的默认价（spec §6.3 / §6.3.1）。 ⚠️ 判据是 **`is not None`**，不是真值——覆盖价 `0` 是**合法金额**（D-0015），用真值判断会把它当「没传」而回落到默认价（E-0021 的同族形状）。 ⚠️ 两边都没有价 ⇒ **拒绝**，不是算成 0：算成 0 会**静默**把代练员的钱算没。
    ⭐ 这一档抛的是 `MissingPriceError`（`AmountError` 的子类）⇒ 需要「只吞没价、别的金额错照抛」的调用方按**类型**判（⛔ 不要按「当时的价是不是 None」判，那是恒假判据——见那个类的 docstring）。 ⚠️ **返回值一律是 `Decimal`**：调用方手里的 `Order` 可能是**未从库里刷新的实例**——
    实测 `Order.objects.create(hall_price="88.00")` 之后，内存里的 `order.hall_price` **仍然是字符串**（Django 不会把 serializer 的输入改写回来），直接把 `str` 交给 `commission_amount` 会 `TypeError`（`str * Decimal`）。这里统一收口，让「返回 `Decimal`」这条契约不依赖调用方怎么拿到实例。"""
    if performance.taker_price is not None:
        return Decimal(performance.taker_price)
    hall_price = (performance.order if order is None else order).hall_price
    if hall_price is None:
        # ⚠️ `MissingPriceError`（`AmountError` 的子类）：这一档**语义明确**——两边都没价，
        #    调用方（镜像单转正）可以只吞它；别的 `AmountError` 必须原样上抛（F2）。
        raise MissingPriceError("该单没有接单价，请联系客服", ErrorCode.VALIDATION)
    return Decimal(hall_price)


def commission_amount(base, percent):
    """佣金 = `base × percent / 100`，量化到分（P4 的 B2：量纲 0–100）。量化必须显式做：不量化就会把 `29.3304` 这类中间值带进库，
    而 Django 的 `DecimalField` 与 PostgreSQL `numeric` 各自还有一层舍入——**两处舍入 = 两套口径**。
    `ROUND_HALF_UP` 在 Django/Python 侧等价于「远离零」，与 PostgreSQL `numeric` 的取整一致。"""
    return (base * percent / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)


def deduct_amount(*, mode, value, base):
    """扣点额（**独立一项**，不折进 `bonus_amount`——P4 的 B3）。 | `mode` | 方式 | 上界（对标物原文） | |---|---|---| | `""` | 未设置 | —— ⇒ `0` | | `"0"` | 按比例 | 「扣点比例不能超过100%」 |
    | `"1"` | 固定金额 | 「扣点金额不能超过预计绩效总计」 | ⚠️ **只设上界，不设下界**：负数 = 冲抵 / 加钱，是 D-0015 明确允许的业务形态；对标物的自校验本身也只写了上界。**谁加 `≥ 0`，测试会红**。 ⚠️ `base` 是「预计绩效总计」的口径（`temp_amount`），不是 `bonus_amount`——见 spec §6.1 的原文。
    ⭐ **D-0049 方案 A（业务方裁定）之后**：`/submit` 交单后 `temp_amount` **跟着实际佣金一起写**⇒ 交过单的行上两者**相等**，未交单的行仍是认领 / 指派时刻的预计值。本函数**不改**：它拿到的 `base` 永远是那个「该行此刻的绩效值」。"""
    if mode in ("", None) or value is None:
        return Decimal("0.00")
    value = Decimal(value)
    if value == 0:
        return Decimal("0.00")

    if mode == DEDUCT_PERCENT:
        if value > PERCENT_MAX:
            raise AmountError(
                f"扣点比例不能超过{PERCENT_MAX}%（当前 {value}）", ErrorCode.VALIDATION
            )
        return commission_amount(base, value)      # 同一个 `/100` 口径，不另写一处
    if mode == DEDUCT_FIXED:
        if value > base:
            raise AmountError(
                f"扣点金额不能超过预计绩效总计（{base}），当前 {value}", ErrorCode.VALIDATION
            )
        return value.quantize(CENT, rounding=ROUND_HALF_UP)

    raise AmountError(f"未知的扣点模式：{mode!r}", ErrorCode.VALIDATION)
