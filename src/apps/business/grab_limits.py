"""抢单限制的**读取层**（P3c Task 1）：把「这个人能抢什么单」的口径收到一处。 **为什么需要这一层**：`apps/business/claim.py` 的限制值原先是**全局拟定常量**，而对标物是按**人**配这几项的（`StaffProfile.grab_limit_dl` / `grab_limit_pw` / `grab_price_min` / `grab_price_max`）。 **P3c Task 1**：这三个**常量**收口到本模块（当时 `claim.py` 改为从本模块 import、不再自己定义，但判据读的还是这三个 `GLOBAL_*` 全局值）。 **P3c Task 2 起**：`claim.check_grab_limits` 经 `limits_for_order(user_id, candidate)` 读 **按人**的生效值（价格区间 / 上限；`order_type` 选 `dl` / `pw`）。 **P3e Task 1 起**：再带回一项**开关**——`grab_forbid_self_service`（「禁自助接单」，只在**抢单**时被拒；大厅照常可见，见口径 2）。 ⚠️ 本模块的 `GLOBAL_*` **不是「当前生效值」，而是回落值**（缺档案时整组回落；价格两项在该列为 `NULL` 时也各自回落，见下面口径 1 / 3）——所以它没有被废弃，仍是 C2 裁定的回落来源。调用方从本模块取生效值，**不自己去读一次 `StaffProfile`**—— 两处判据必然漂移（P3a 的 E-0021 就是「同一事实两套判据，只用对了一套」）。 ⚠️ **本层只取值、不校验**（编排方裁定）： - 「接单价越界」（`< price_min` / `> price_max`）与「该单没有接单价」（`price is None`）两种判定都**留在 `claim.check_grab_limits`**（Task 2），本层不提前拒绝——提前拒绝会让
Task 2 拿不到值、也把两种失败原因混在一起； - 本层也**不读**订单的其它字段（价格 / 状态 / 归属都不参与），只回答「这个人的限制是多少」。口径（每条都是**有意**的，改动即改「谁能抢到什么单」）： 1. **缺档案 ⇒ 回落全局常量**（C2 推荐分支）：`/assign` 允许把单指派给任何活跃账号，档案并不保证存在，所以「没有档案」必须有一个明确行为而不是报错。 ⚠️ 回落的是**全局常量**（含 `GLOBAL_MAX_ACTIVE = 10`），**不是**「不限制」。 ⚠️ **例外是第 2 条**（禁自助接单）：那一项缺档案 = **不禁止**，不回落到任何拟定值。 2. **`grab_forbid_self_service` 缺档案 ⇒ `False`（不禁止）——与第 1 条语义相反**：这一项的「未配」不是「回落到某个拟定值」，而是「**没有施加这个限制**」。有档案时取 `bool(profile.grab_forbid_self_service)`——读取层保证这一项**类型恒为 `bool`** （不是 `None`、也不是裸的 `0` / `1`）⇒ 调用方（`claim.py` 的 `if limits.forbid_self_service:`） **直接按真值判断**即可，不必再写 `is True`；测试侧用 `assertIs(..., True)` 钉字面值（E-0024：钉值不钉常量）。 📌 **本句旧文写「调用方的判据用 `is True`，不用真值」——与实现不符**（`claim.py` 一直用真值），已按实现改准。**改措辞 ≠ 放宽判据**：「不禁止」这个结论的**语义来源**只有**一处**—— ① **缺档案那条字面量 `False`**（语义来源，**有用例**钉住：缺档案 ⇒ 不禁止）； ② 有档案时 `bool(...)` 是**防御性归一化**：当前列类型（非空 `BooleanField`）下与**直取**等价，只保证类型恒为 `bool`，**不是**承重逻辑——拆掉它**没有任何用例会红**（验收轮自证式变异 = survived）。
⚠️ **不要写成「两条路径缺一不可」**：那是把**防御性写法**当**承重逻辑**（写「缺一不可 / 必须 / 唯一」前先问「去掉它，哪条用例会红」，答不上来就不是不变量；同 E-0011 的形状）。依据：对标那列叫「禁**自助**接单」（tips 原文与列标题见 `js/userlist.js:305` / `:772`），而「被禁止」只能由一条**显式配置**施加——否则每个没建档案的人（`/assign` 允许）都被默认拦下。 3. **价格逐列各自回落，两列互不牵连**：`grab_price_min` 有值就用它的值、`None` 则**该列** 回落 `GLOBAL_PRICE_MIN`；`grab_price_max` 同理回落 `GLOBAL_PRICE_MAX`。所以「`grab_price_min = Decimal("5")` 且 `max` 未配」的生效区间就是 `(Decimal("5"), Decimal("99999"))`（不是「一端配了另一端跟着变」，也不是「整段作废」）。依据：模型的 CHECK `staffprofile_grab_price_range_ordered` 也**只在两端都有值**时才要求有序。返回的两个价格字段**永不为 `None`**（类型是 `Decimal`），调用方不需要判空。 4. **`grab_limit_dl` / `grab_limit_pw` 的 `0` = 不限制** ⇒ `max_active = None`；非 `0` ⇒ 该值本身。 ⚠️ 这两个字段是 `PositiveIntegerField` + `default=0` ⇒ **不可能是负数、也不可能是 `None`**，所以「未配」只发生在「没有档案」这一支（第 1 条）。于是 `0`（⇒ `None`）与「未配」（⇒ `GLOBAL_MAX_ACTIVE`）是**两种不同**的语义，返回值上必须可区分。 ⚠️ 判据用 `== 0` / `is None`，**不要**用真值（`if limit:`）——`bool(0) is False`，真值写法会把「不限制」当成「未配」（E-0021 的形状：正面用例全绿，只有 `0` 能抓到）。 5. **`order_type` 的判据是穷尽的**（C4 推荐分支）：`== OrderType.COMPANION` ⇒ `grab_limit_pw`； **其余一切取值 ⇒ `grab_limit_dl`**，包括 `BOOST`、**空串**、以及 `choices` 之外的未知值。这**是有意的**，不是漏考虑：`Order.order_type` 是 `blank=True` 的 `CharField`，存量里可能有空串（C4：空值按**代练**走）；而 `CharField + choices` 在 DB 层不拦第三值，未知值落进同一条 `else` 分支（Task 1 不为它单独报错——本层不校验）。 `limits_of` 不含订单维度，固定取 `dl`（给「只关心上限、不涉及具体订单」的调用方）。 ⚠️ 本模块**不碰 request**、不返回模型对象，只返回 `GrabLimits`（与 `commission.py` 同形，便于纯逻辑测试；区别：本层收 `user_id`，`commission.py` 收 `user` 对象）。查询只落在**当前连接所在的 schema** 上，**不接受任何请求参数传入的租户线索**。"""
from dataclasses import dataclass
from decimal import Decimal

from apps.accounts.models import StaffProfile
from apps.business.models import OrderType

#: 未配时的价格下界/上界与持单上限（含边界）。回落**触发条件两组不同**：价格两项是「缺档案 **或** 该列 `NULL`」；上限只有「缺档案」一种（该列非空，配成 `0` 走的是「不限制」，不是回落）。
#: ⚠️ `grab_forbid_self_service` **不在这两组里**：它缺档案时不回落（= 不禁止），见模块口径 2。此前它们定义在 `claim.py`（`MAX_ACTIVE_ORDERS` / `PRICE_MIN` / `PRICE_MAX`），
#: 取值逐字未变；现在这里是**唯一**的定义处（现在 `claim.py` 经由 `limits_for_order` 取生效值，不再直接 import 这三个符号）。
GLOBAL_PRICE_MIN: Decimal = Decimal("0")
GLOBAL_PRICE_MAX: Decimal = Decimal("99999")
GLOBAL_MAX_ACTIVE: int = 10


@dataclass(frozen=True)
class GrabLimits:
    """某个人在抢单时的**生效**限制（已把回落算完，调用方直接比较即可）。 - `price_min` / `price_max`：`Decimal`，**永不为 `None`**（未配时是全局常量）；
    - `max_active`：`int | None`——`None` = **不限制**（档案配了 `0`），正整数 = 同时持单上限。「未配」（缺档案）**不等于** `None`，它回落到 `GLOBAL_MAX_ACTIVE`。
    - `forbid_self_service`：`bool`——`True` = 「禁自助接单」（**只在抢单时被拒**，大厅照常可见）。缺档案 ⇒ `False`（**不禁止**），与上面两项的「回落」语义相反（见模块 docstring 口径 2）。"""

    price_min: Decimal
    price_max: Decimal
    max_active: int | None
    forbid_self_service: bool


def _limits(profile, limit_field: str) -> GrabLimits:
    """把一份档案（可能为 `None`）换算成生效限制。`limit_field` 只能是那两个上限字段名。
    ⚠️ `profile is None` ⇒ `forbid_self_service = False`（**不禁止**）——这一项缺档案
    不是「回落」，是「没有施加限制」（模块 docstring 口径 2）。"""
    if profile is None:
        return GrabLimits(GLOBAL_PRICE_MIN, GLOBAL_PRICE_MAX, GLOBAL_MAX_ACTIVE, False)

    price_min = profile.grab_price_min
    if price_min is None:
        # 「未配」用 `is None` 判，不用真值：`Decimal("0.00")` 也是配置过的值。
        price_min = GLOBAL_PRICE_MIN
    price_max = profile.grab_price_max
    if price_max is None:
        price_max = GLOBAL_PRICE_MAX

    raw_limit = getattr(profile, limit_field)
    # `0` = 不限制（不是「未配」）：该字段是非空的正整数列，取值只有 0 与正整数两种。
    max_active = None if raw_limit == 0 else raw_limit
    return GrabLimits(
        price_min, price_max, max_active, bool(profile.grab_forbid_self_service)
    )


def limits_of(user_id) -> GrabLimits:
    """取「只关心上限、不涉及具体订单」的调用方的限制：上限用 `grab_limit_dl`（代练）。
    缺档案 ⇒ `price_min/price_max/max_active` 回落全局常量（0 / 99999 / 10），
    而 `forbid_self_service` 是 `False`（**不禁止**，不是回落）。"""
    profile = StaffProfile.objects.filter(user_id=user_id).first()
    return _limits(profile, "grab_limit_dl")


def limits_for_order(user_id, candidate) -> GrabLimits:
    """按 `candidate.order_type` 选上限：`COMPANION` ⇒ `grab_limit_pw`，**其余一切取值** （`BOOST` / 空串 / `choices` 之外的未知值）⇒ `grab_limit_dl`（穷尽式，见模块 docstring 第 5 条）。
    只读 `candidate.order_type` 这一个字段——价格与状态与本层无关（见模块 docstring）。 ⚠️ **形参叫 `candidate` 而不是 `order`**（P2-B Task 1）：镜像单接单传的是
    `claim.GrabCandidate` 值对象，而它与本函数的旧入参 `Order` 只共享 `order_type` 这一个被读的字段 ⇒ 语义不变、调用点全部按位置传参，改名只为让两处读同一件事。"""
    limit_field = (
        "grab_limit_pw" if candidate.order_type == OrderType.COMPANION else "grab_limit_dl"
    )
    profile = StaffProfile.objects.filter(user_id=user_id).first()
    return _limits(profile, limit_field)
