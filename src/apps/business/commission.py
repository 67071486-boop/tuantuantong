"""提成读取适配层（P3b 计划 §1 的 Q1/Q3/Q4 裁定；P4-5 扩展 mode 30）。 **为什么需要这一层**：spec §3.3 原计划新建 `StaffCommission`，但账号线（M3，已合并进 `main`）已经把「人员提成」落到 **`apps/accounts/StaffProfile`**（`commission_mode` / `commission_value`），而且它的 choices **已含 P4 的 B1 要的 `profit_percent`**。所以本模块**不建表、不写库**，只做**读取与口径收口**——调用方（打手路径的 `/submit`、`/adjust`、`temp_amount` 重算，以及 P4 的客服提成）都从这里取比例，**判定只有一处**（`AGENTS.md` §4「不重复同一事实」）。
口径（逐条都有出处，改动即改发钱结果）： 1. **量纲**：`commission_value` 是 **0–100 的百分数**，算式一律 `× value / 100`（P4 的 B2）。 ⚠️ 这对 `ORDER_PERCENT`(20) 与 `PROFIT_PERCENT`(30) **同一条**：两种模式的差别只在**基数** （订单价 vs 利润），**不在比例本身**——基数由调用方按模式选定，本层只负责给比例。 2. **请求的模式必须与档案的模式一致**：`commission_percent_of(user, mode=...)` 的 `mode` 是 **调用方声明的口径**，档案的 `commission_mode` 必须等于它；不等就拒绝（`message` 说清两边）。
- `mode=ORDER_PERCENT`（默认，打手路径）——行为与 P3b 时一致； - `mode=PROFIT_PERCENT`（P4 客服路径）——按同一量纲给出比例； - `FIXED`(10) 的 `value` 是**金额（元）**，不是百分数 ⇒ 在**两个** `mode` 下都拒绝。一切不匹配都**明确报错**而不是"猜一个"，因为**金额错了不会报错，只会算错钱**。 3. **未配置 ⇒ 回落全额（100%）**：无档案，**或 `value == 0`**（Q3 裁定原文； P4-5 的 Ruling M 裁定**对两个 mode 一致**）。 ⚠️ 把 `0` 当「未配置」的代价是**业务上无法把某人设成不付提成**；要改就是改发钱结果，必须重新裁定（登记在 spec §6.1 的更正块里）。 4. **越界拒绝**：`value < 0` 或 `> 100` ⇒ `CommissionError(VALIDATION)`，**两个 mode 同一道上界**。账号线的**写入侧没有上界**（可以存 150），所以这道判定必须在计算侧有（跨线项见 spec §16.4 #19）。 ⚠️ 本模块**不碰 request**，也不返回模型对象——只返回 `Decimal` 比例，便于纯逻辑测试。"""
from decimal import Decimal

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.core.error_codes import ErrorCode

#: 百分数上界（含）。P4 的 B2 要求「量纲写死 0–100」。
PERCENT_MAX = Decimal("100")
#: 未配置时的回落值（Q3 裁定：全额）。
FULL_PERCENT = Decimal("100")
#: 本层支持按百分比读取的模式（它们的 `value` 量纲都是 0–100 的百分数）。 ⚠️ P3b 时这里是一个单值常量 `PERCENT_MODE = ORDER_PERCENT`，`mode != PERCENT_MODE`
#: 那条兜底分支靠它**对未知取值响亮拒绝**。P4-5 之后合法模式有两个，单值常量不再够用，已删除并换成这份白名单：兜底改由「请求 mode 必须在本白名单内」+「档案 mode 必须等于请求 mode」
#: 两条共同承担——未知取值仍然**拒绝**，不会被静默按百分比算。
PERCENT_MODES = (CommissionMode.ORDER_PERCENT, CommissionMode.PROFIT_PERCENT)

#: R4/D5（编排方裁定）：**客服方向**报错模板——必须含三要素 ① 指明**该单的销售客服**（⛔ 不是「当前登录账号」）② 回显**两边**模式 ③ 给出**怎么改**。 ⚠️ 本函数的 `user` 形参在客服方向**就是** `Order.sales_user`
#: （枚举命令与读数：`grep -rn "mode=CommissionMode.PROFIT_PERCENT" src/apps --include=*.py` ⇒ 复跑是 **2 行**，其中 **1 行是这条注释自己**（自命中，改注释前是 1 行 ⇒ 当时那条
#: 「只有一处调用点」的写法会让人以为复跑应当只有 1 行）；**排除本文件的真实调用点** `… | grep -v "commission.py"` ⇒ 恰好 `apps/business/settlement_hooks.py` 的 `recompute_customer_commission` **一处**）。
_CUSTOMER_MODE_HINT = (
    "该单的销售客服（用户名 {username}）的提成模式是「{profile_mode}」，"
    "本处需要「{mode}」；请到人员管理把 TA 的提成模式改成「{mode}」后再操作"
)
#: **打手方向**（默认 `ORDER_PERCENT`）照同形写一句：指明**该单的接手人** + 需要**订单价百分比**。
_TAKER_MODE_HINT = (
    "该单的接手人（用户名 {username}）的提成模式是「{profile_mode}」，"
    "本处需要「{mode}」；请到人员管理把 TA 的提成模式改成「{mode}」后再操作"
)


def _mode_label(mode):
    """把提成模式取值翻成可辨的中文标签；未知取值如实显示，**不抛异常**。为什么单独一个函数：`message` 要同时说清「档案是哪种」与「本处期望哪种」，
    而 `choices` 之外的取值（DB 层不校验）会让 `CommissionMode(mode)` 抛 `ValueError`——
    在报错路径上再抛一个错会把「配置不对」伪装成崩溃。"""
    try:
        return CommissionMode(mode).label
    except ValueError:
        return f"未知取值 {mode!r}"


class CommissionError(Exception):
    """提成配置**不可用于计算**。`code` 直接给 API 层当业务码用（与 `ClaimError` 同形）。
    ⚠️ 属性名是 `message` 而不是 `str(self)`：调用方需要原样把中文理由放进 `msg`，
    而 `Exception` 的 `str()` 在多参数时会带上 `args` 的括号与引号。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def commission_percent_of(user, *, mode=CommissionMode.ORDER_PERCENT) -> Decimal:
    """取该用户的提成百分比（0–100 的 `Decimal`）。这是**唯一**的提成判据入口：调用方都走它，不要在别处再读一次 `StaffProfile` （两处判据必然漂移——P3a 的 E-0021 就是「同一事实两套判据，只用对了一套」）。
    `mode` 是**调用方声明的口径**，keyword-only（P4-5 新增；默认 `ORDER_PERCENT` ⇒ 打手路径的三个既有调用点零改动）。档案的 `commission_mode` 必须等于它，否则拒绝。
    退出形态： - 可算 ⇒ `Decimal`（含 `FULL_PERCENT` 的回落情形）； - 不可算 ⇒ 抛 `CommissionError`，`code` 为 `VALIDATION`，`message` **可辨是哪一种原因** （请求模式本层不支持 / 档案模式与请求模式不匹配 / 数值越界），前端与测试都靠它区分。"""
    if mode not in PERCENT_MODES:
        # 兜底：请求了本层不认识的模式（`choices` 将来新增取值时也不会被静默按百分比算）。
        # 必须先于读档案判定——否则「档案恰好也是这个未知值」会骗过下面的相等判定。
        raise CommissionError(
            f"本层不支持按百分比读取的提成模式：{_mode_label(mode)}", ErrorCode.VALIDATION
        )

    profile = StaffProfile.objects.filter(user=user).first()
    if profile is None:
        # 回落：没有档案（`/assign` 允许指派给任何活跃账号，见 spec §16.4 #18 的裁定）。
        # 与请求哪种 mode 无关（Ruling M）。
        return FULL_PERCENT

    profile_mode = profile.commission_mode
    if profile_mode != mode:
        # ⚠️ 同时说清**档案是哪种**与**本处期望哪种**，否则调用方分不清是「配错了档案」还是「调用点传错了 mode」。 `FIXED`(10) 的 `value` 是金额元而不是百分数，它落在这一支（两个请求模式下都不相等）。
        # ⭐ R4/D5（编排方裁定）：**必须按 `mode` 分支**——同一支 `raise` 同时服务客服方向（`mode=PROFIT_PERCENT`）与打手方向（`mode=ORDER_PERCENT`），
        # 整支替换会对一个「已经是利润百分比」的人说「请改为利润的百分比」（语义反了）。 ⛔ 不许写成「当前登录账号」：批量重算客服提成时，财务可以一次对**许多别人录的单** 重算（`orders/batch/recompute-commission/`），那句话会变成事实错误。
        username = getattr(user, "username", "") or "（未命名）"
        template = (
            _CUSTOMER_MODE_HINT
            if mode == CommissionMode.PROFIT_PERCENT
            else _TAKER_MODE_HINT
        )
        raise CommissionError(
            template.format(
                username=username,
                profile_mode=_mode_label(profile_mode),
                mode=_mode_label(mode),
            ),
            ErrorCode.VALIDATION,
        )

    value = profile.commission_value
    if value is None:
        return FULL_PERCENT
    if value == 0:
        # ⚠️ 裁定原文（Q3）：`0` 当「未配置」⇒ 回落全额；P4-5 的 Ruling M 确认
        # 这对两个 mode 一致。代价见模块 docstring 第 3 条。
        return FULL_PERCENT
    if value < 0 or value > PERCENT_MAX:
        raise CommissionError(
            f"提成百分比必须在 0~{PERCENT_MAX} 之间，当前是 {value}", ErrorCode.VALIDATION
        )
    return value
