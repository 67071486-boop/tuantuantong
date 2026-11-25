"""打手侧的**凭据规格**（`TAKER_SPEC`）—— 与 `apps/openapi/credentials.py::OPENAPI_SPEC` 并列的第二份。

## 这里是什么

`CredentialSpec` 的定义在 `apps/openapi/credentials.py`（那是**共享地基**，T2 落的），
本模块**只构造一份新的数据**：把打手侧那一族的
「凭据模型 / 幂等台账模型 / 限流桶表 / 三个 `scope` / 各项文案 / 各项额度」
指到 T1 建好的三张表（`TakerApp` / `TakerApiIdempotency` / `TakerApiRateBucket`）上。

⚠️ **本线（打手侧回调推送）给同一份 spec 追加了投递台账那一面**（`delivery_model` /
`callback_url_field` / `callback_state_field` / `callback_enabled_value` / 三个
`delivery_*_value` / `callback_disabled_reasons`，见 spec §2.3）——那是"打手侧的回调"这一族
自己的数据，取法与上面那几项同一条纪律（**显式写全、⛔ 不靠"跟上家侧一样"当默认**）。
⚠️ 其中 `delivery_model` 今天**显式写 `None`**（打手侧那张台账表要到本线 **T2** 才建）：
⛔ 不许拿 `OpenApiDelivery` 当占位（理由逐字写在那一行上面）。

⛔ **本模块一行引擎代码都没有**（认证 / 令牌 / 幂等 / 限流 / **回调投递**五件全部复用
`apps/openapi/` 那几份实现，它们都是收 `spec` 当参数的）⇒ spec §4 那条「⛔ 不抄第二份判据」
的落点就在这：第二族要**新写**的东西只有这份数据 + 两个子类（`TakerAppBearerAuthentication` /
`TakerAppFallbackThrottle`）+ 一条取令牌端点。

## ⛔ 为什么 `spec` 里的每一项都必须显式写全（⛔ 不靠"跟 openapi 一样"当默认）

`CredentialSpec` 的字段**没有默认值**（三个列名除外，它们两族今天逐字相同）：
漏填一个 ⇒ **构造期**就响亮报错。理由见那个 dataclass 的 docstring——最贵的一条是
「静默按上家侧处理」：闸照常工作、请求照常放行，只有"量级 / 表 / 文案是别人的"这一件事
没有任何症状。本模块的每一项都对应 spec §2 那张"分开什么"的表里的一行。

## ⚠️ 与 `OPENAPI_SPEC` 的**文案**：逐字相同是**有意的**

`invalid_credentials_msg` / `expired_msg` / `ip_not_allowed_msg` / 两句 OAuth2 描述
在这两份 spec 里**逐字相同**。⛔ 这**不是**"复制粘贴没改"，而是**两条通道各自独立的对外契约
恰好用了同一句话**（两条通道互不影响：改打手侧那一句不会动到上家侧）。
⚠️ 判据按**值**写（`E-0024`：钉值不钉常量）⇒ 谁把某一族改成别的话，那一族的用例当场红，
而**另一族照样绿**——这正是要的形状（两族的文案是**两份**数据，不是一份共享的）。

## ⚠️⭐ 两族的**额度**必须不同（spec §4.1-4 点名的那颗地雷）

`app_fallback_max_requests` / `token_fallback_max_requests` 在打手侧**故意取不同的数**
（见下面那两条常量注释里的取值理由）。spec §4.1-4 的原文是：taker 侧若照抄 openapi
那两个"读模块名"的薄壳，会**静默用到上家侧的额度**（6000/分钟）——行为看起来一切正常，
只有"量级是别人的"这一件事没有任何症状。⇒ 本任务做了两件（都要有）：

1. 兜底额度**按族可给**：`throttling.SpecDrivenFallbackThrottle` 的两个钩子在子类里实现，
   打手侧那一对读**本 app 自己**的常量（`apps/takerapi/throttling.py`）；
2. 一条**两族额度不同**的用例钉住（`tests/test_takerapi_auth.py::PerFamilyQuotaTests`）
   —— 它拿两族的**实际生效额度**对比，⛔ 不看注释、⛔ 不看在 spec 里写的数。

⚠️ **取值本身是本任务的实现选择**（spec §4.1-4 只要求"两族不同"、⛔ 没给数字）：
口径与上家侧那条**逐字同款**——**兜底、不是配额**（业务方 定的：不设每应用配额，
只想挡住失控）。打手侧取**上家侧的一半**（3000 / 300）：打手是**外部工作室自己的脚本**、
调用形状是"轮询大厅 + 抢单"，同一台机器上通常还跑着别的东西 ⇒ 给它的失控兜底比上家侧更紧一点。
⚠️ 这个数是**可调的**（`docs/plans` 的 T2b 行只钉"两族不同"）：真要调，改的是**这里**
（⛔ 不是引擎、⛔ 不是视图）。
"""
from __future__ import annotations

from apps.openapi.credentials import CredentialSpec
from apps.takerapi.models import (
    AppState,
    CallbackState,
    TakerApiDelivery,
    TakerApiIdempotency,
    TakerApiRateBucket,
    TakerApp,
)

#: 打手侧兜底闸的额度：**每应用每窗口**放行多少次。⛔ **不是配额**（口径见
#: `apps/openapi/throttling.py` 的模块 docstring：它只挡失控，正常业务碰不到）。
#: ⚠️⭐ **与上家侧的 6000 刻意不同**（spec §4.1-4 的地雷）：它比上家侧**紧一半** ——
#: 打手侧是外部工作室自己的脚本，形状是"轮询大厅 + 抢单"，同一台机器上通常还跑着别的东西。
#: ⛔ **别把它写成"读 openapi 那个常量"**：那样两族额度会**静默绑死**
#: （spec §4.1-4 的原句：行为看起来一切正常，只有"量级是别人的"没有任何症状）。
TAKER_FALLBACK_MAX_REQUESTS = 3000

#: 打手侧兜底闸的窗口长度（秒）。与上面的数字一起构成「3000 次 / 分钟 / 应用」。
TAKER_FALLBACK_WINDOW_SECONDS = 60

#: 打手侧**取令牌端点**的兜底额度：**每来源 IP 每窗口**放行多少次（**含成功与失败**）。
#: ⚠️ 同上：与上家侧的 600（`apps/openapi/api/token_views.py`）**刻意不同**。
#: ⚠️ **⛔ 不是配额**：令牌有效期 7200 秒（`apps/openapi/tokens.TOKEN_TTL_SECONDS`）
#: ⇒ 规规矩矩的调用方两小时换一次，而这里是每分钟 300 次。
TAKER_TOKEN_FALLBACK_MAX_REQUESTS = 300

#: 取令牌端点兜底额度的窗口长度（秒）。
TAKER_TOKEN_FALLBACK_WINDOW_SECONDS = 60


#: ⭐ **打手侧那一族的规格** —— 三张表全指向 `apps/takerapi/models.py` 里的那一份（spec §3）。
#:
#: ⚠️ 四个"取自模型枚举"的值（`enabled_state_value` / 三个 `*_scope`）**不写字面量**：
#: 它们取自**打手侧自己的**枚举（`AppState.ENABLED` / `TakerApiRateBucket.Scope.*`），
#: ⛔ 不是上家侧那一套 —— 两族的 `Scope` **成员名与取值今天逐字相同**（`app` / `ip` / `ip_token`），
#: 而它们住在**两个不同的模型类**里 ⇒ 引擎 import 哪一份都错，只有 spec 这一处能选对。
TAKER_SPEC = CredentialSpec(
    # ── 表：三张都是打手侧自己的（spec §3.1 / §3.2 / §3.3）──
    app_model=TakerApp,
    idem_model=TakerApiIdempotency,
    bucket_model=TakerApiRateBucket,
    # ── 取自**打手侧**模型枚举的行为值 ──
    enabled_state_value=AppState.ENABLED,
    failure_scope=TakerApiRateBucket.Scope.IP,
    app_fallback_scope=TakerApiRateBucket.Scope.APP,
    token_fallback_scope=TakerApiRateBucket.Scope.IP_TOKEN,
    # ⭐ **打手侧没有"锁定应用"这个概念** ⇒ 显式写 `None`（= 端点白名单那道闸在这一族
    # **第一行就返回**，`apps/openapi/permissions.py::enforce_app_endpoint_scope`）。
    # `locked_app_kind` 必填：漏写会让整族端点闸静默不存在。这里显式写 None。
    locked_app_kind=None,
    # ── 文案（⚠️ 与上家侧**逐字相同是有意的**，理由见模块 docstring）──
    invalid_credentials_msg="应用凭据无效",
    expired_msg="令牌已过期",
    ip_not_allowed_msg="来源 IP 不在白名单内",
    path_too_long_template="请求路径过长（上限 {} 个字符）",
    # ⚠️⭐ 前缀**必须与上家侧的 `openapi:source:` 不同**：两族共用一颗前缀的话，
    # 一族的来源单号会在另一族那边**命中回放**（spec §4 第 4 条）。
    # ⛔ 打手侧今天**不用**来源单号去重（`seen_source_response` / `remember_source`
    # 只有上家侧录单那条路调），但**这一格仍要显式给**：它是 spec 的必填字段，
    # 而且"将来某条打手侧端点要复用它"时，值已经是对的。
    source_key_prefix="takerapi:source:",
    unsupported_grant_type_description="仅支持 client_credentials",
    rate_limited_description="请求过于频繁，请稍后再试",
    # ── 额度（⚠️ 全部与上家侧**不同**，理由见上面那四个常量的注释）──
    # ⚠️ 按来源 IP 的**失败闸**（20 / 60 秒）两族**取同一个数**：那不是"兜底额度"，
    # 而是「同一个来源在这里猜凭据猜了多少次」的判决线 —— 两族各有一张桶表
    # ⇒ 各算各的（spec §9-16 要的正是"打满一族不影响另一族"），阈值没必要也不该分家。
    failure_max=20,
    failure_window_seconds=60,
    app_fallback_max_requests=TAKER_FALLBACK_MAX_REQUESTS,
    app_fallback_window_seconds=TAKER_FALLBACK_WINDOW_SECONDS,
    token_fallback_max_requests=TAKER_TOKEN_FALLBACK_MAX_REQUESTS,
    token_fallback_window_seconds=TAKER_TOKEN_FALLBACK_WINDOW_SECONDS,
    # ── 投递台账（spec §2.3）──
    # 打手侧的台账表是 `TakerApiDelivery`（迁移 `takerapi/0002_*`），不是上家侧的 `OpenApiDelivery`。
    # 不许写回 None：两族台账会静默合成一张。
    # ⛔⛔ **绝不许**拿上家侧的 `OpenApiDelivery` 当占位：那正是 spec §2.2 最贵的那个形状
    # ——「打手侧的回调静默写进上家侧的台账」（引擎照常跑、一行异常都没有）。
    delivery_model=TakerApiDelivery,
    # ⚠️ 与 `enabled_state_value` 是**两个独立开关**：停用打手侧应用停整条对外通道、
    # 停用回调只停推送；这一格取**打手侧自己的**枚举（⛔ 不是上家侧的 `CallbackState`）。
    # 这一格是回调开关列的值，取 `CallbackState`，不要取 `AppState`。
    # 两个枚举今天都是 `"enabled"`，只是巧合。取错枚举后，有人只改 `CallbackState` 时
    # 回调面会按另一个字面值去比，一条都选不中、一行都不推，而且不报错。
    callback_enabled_value=CallbackState.ENABLED,
    # ⭐ **绑定账号的那一列 = `taker`**（D-0333，M3 半边收口）：`TakerApp.taker` 是
    # **必填** FK（范围判据的唯一来源，`apps/takerapi/models.py` ③）⇒ 「令牌对应的
    # 打手账号必须可用（`is_active`）」这道闸在本族**真的会判**——消费点在**共用**的
    # Bearer 认证引擎（第 5.5 步）与取令牌共用流程里，失败回
    # `invalid_credentials_msg`（与「凭据无效」逐字同一句，⛔ 不说破「账号被停用」，
    # 防探测）。停用 ⇒ **已签发的对外令牌立即失效**、也**换不出新令牌**（业务方
    # 拍板「需要的」）。列名是**本族自己的数据**：⛔ 别在这里读上家侧的形状。
    bound_user_field="taker",
    # ⚠️⭐ 三句**必须说"打手侧…"**（spec §2.3 逐字要求；两族的台账页并排看时要分得清是谁的回调）。
    # ⚠️ 顺序是契约：应用停用 / 回调停用 / 地址为空。
    # ⚠️ 今天**没有任何行为**读这三句（T1 不建打手侧的入队与投递）⇒ 值与上家侧不同**不影响**
    # 任何既有读数；它是给 T3 的"投递时发现应用不可用"那一档准备的。
    callback_disabled_reasons=(
        "打手侧应用已停用，不再推送",
        "打手侧回调已停用，不再推送",
        "打手侧没有配置回调地址，不再推送",
    ),
    # ⚠️ 手工重投那道闸的人话（`dispatch.prepare_retry` 用；T5b）。
    # ⚠️ 与上家侧那一格**今天逐字相同**是**有意的**：两条通道各自的对外契约恰好用了同一句话
    # （理由与 `invalid_credentials_msg` 那几项同一条，见模块 docstring 的
    # 「与 OPENAPI_SPEC 的文案」一节）；⛔ 别把它写成"读上家侧那个常量"
    # ——那样两族的文案会静默绑死（改一族会连带动另一族）。
    delivery_retry_only_failed_msg=(
        "只有投递失败的推送记录可以重投（已投递成功的记录重投会让对方收到两次）"
    ),
)
