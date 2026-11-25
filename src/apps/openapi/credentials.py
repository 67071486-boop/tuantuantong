"""共享地基的**凭据规格**（`CredentialSpec`）—— 一处定义、各引擎（含投递）都收它当参数（spec §4）。

## 这里是什么

`apps/openapi` 的四个引擎 —— 认证（`authentication`）/ 令牌（`tokens` 是纯函数，收的是
**字符串**参数，⛔ 本任务一个字没改）/ 幂等（`idempotency`）/ 限流（`rate_limit` + 兜底闸
`throttling`）—— 原先各自**硬编码**了「凭据模型 `ApiApp` / 幂等台账模型 / 限流桶模型
（含表名与唯一约束名，因为限流那条路拼的是裸 SQL）/ 失败计数落哪 / 各项文案 / 各项额度」。
本模块把这一束东西集中成**数据**（`CredentialSpec`），引擎一律收它当参数
⇒ 第二个凭据族（打手侧 `TakerApp*`）只需**再构造一份 spec**，
⛔ 不必把任一引擎抄成第二份（`AGENTS.md` §5-E：同一个事实两套判据）。
⚠️ **本线（打手侧回调）追加第五个引擎面**：投递（`apps/openapi/dispatch.py` 的
outbox / 取活 / 记账 / 退避 / 清理）原先硬编码「投递台账模型 `OpenApiDelivery` /
应用模型 `ApiApp` / 两个 `CallbackState` 判据 / 三个 `Status` 值 / 三句"不投递"人话」
⇒ 同一个 spec 现在也带那一面（`delivery_model` / `callback_*` / `delivery_*_value`），
引擎一行具体模型都不再 import。

## ⛔ 为什么必须是"一束"、⛔ 不是"每处加个 `model=` 参数"

零散地一处一处加参数**必然漏**；而漏掉任何一处的症状都是「**既有网全绿、线上跨系统串台**」
（spec §4 那张表逐条列了后果，最贵的一条是两侧共用失败计数桶 ⇒ 一边穷举凭据把另一边
同一来源一起拉黑）。⇒ 「漏没漏」这条判据做成了**搜模型名**：

    grep -n "OpenApiIdempotency\\|OpenApiRateBucket\\|ApiApp\\b" \\
      apps/openapi/{idempotency,rate_limit,authentication,throttling}.py

命中**只应出现在**：本模块（= spec 定义处）/ 类型注解 / docstring / **再导出行**
（见下面「再导出」那一节）。⛔ 不应出现在任何"决定行为"的位置。

⚠️ **投递台账那一面**（打手侧回调线 T1 给本 dataclass 追加的 `delivery_model` / `callback_*`）
有**同款**一条判据，但⛔ 不扫本模块，扫的是 `apps/openapi/dispatch.py`：**剥掉注释与
docstring 之后**，那里不许再出现 `OpenApiDelivery` / `ApiApp` / `CallbackState` 的**行为位**引用
（判据原文与做法见那个模块 docstring 的「引擎 / 族的切分」一节）。
理由同族：那一面原先也硬编码在**引擎**里（`dispatch.py` 的 `from apps.openapi.models import …`），
两族共用同一份投递实现 ⇒ 漏一处就是"打手侧的回调写进上家侧的台账"。

## ⚠️ 引擎里的模块级别名为什么还在原处（⛔ 不是漏改）

`authentication.IP_FAILURE_MAX` / `throttling.OPENAPI_FALLBACK_MAX_REQUESTS` /
`token_views.TOKEN_FALLBACK_MAX_REQUESTS` 这些名字**留在原模块**，值是本 spec 的字段
（⛔ 数字不在那边写第二遍）。两个理由：

1. 既有用例按这些**模块名** import（`from apps.openapi.throttling import
   OPENAPI_FALLBACK_MAX_REQUESTS`）⇒ 挪走会 ImportError；
2. 既有用例按这些**模块名**打桩（`mock.patch.object(throttling,
   "OPENAPI_FALLBACK_MAX_REQUESTS", 2)`）⇒ 决策点上必须在**调用时**读那个模块名，
   从 spec 现取会让补丁**静默失效**（E-0029 同族）。⚠️ 这两处**如实登记**在各自的
   注释里（`throttling.allow_request` 与 `token_views.TokenView.post`）。

## ⚠️ 「再导出」那一类命中（判读 grep 输出时要知道）

`authentication` 与 `token_views` 各保留一行 `from apps.openapi.models import ApiApp`
（⛔ 它不参与任何判定，代码一律走 `spec.app_model`）。理由：既有用例把桩打在
**那两个模块的 `ApiApp` 名字**上（`mock.patch.object(auth_module.ApiApp.objects,
"filter", …)` / `mock.patch.object(token_views.ApiApp.objects, "filter", …)`），
⚠️ 而那两条"桩接没接上"的判据（对照用例）**是承重的**（E-0029：桩没接上时
"不查库"那条断言会恒真）⇒ ⛔ 不许为了"名字干净"把它们去掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured

from apps.openapi.models import (
    BUSINESS_INTEGRATION_ENABLED,
    ApiApp,
    ApiAppKind,
    ApiAppState,
    CallbackState,
    OpenApiDelivery,
    OpenApiIdempotency,
    OpenApiRateBucket,
)


@dataclass(frozen=True)
class CredentialSpec:
    """一个**凭据族**的全部"可参数化面"：模型 / 列名 / 文案 / 额度。

    ## ⛔ 字段**没有默认值**（几张表与那几个列名 / 状态值除外，见下）

    每一份 spec 都把自己那一族的东西**显式**写全 —— 漏填一个 ⇒ **构造期**就响亮地报错，
    ⛔ 不是"默默继承上一族的文案 / 额度"（那正是跨系统串台最贵的形状）。
    ⚠️ 列名（`app_key_field` / `secret_field` / `idem_path_field` / `bucket_unique_fields`）
    今天两族**逐字相同**（spec §3 的两张表与既有表逐列对应），故给了默认值；
    它们仍然是**数据**：真出现写法不同的那一族时，改的是构造处、⛔ 不是引擎。
    ⚠️ 同理，投递台账那一面的 `callback_url_field` / `callback_state_field` 与三个状态值
    （`delivery_pending_value` / `delivery_done_value` / `delivery_failed_value`）也给了默认值
    —— 两族的台账列与 `Status` 三值是**逐列对应**来的（spec §3.2 点明"逐字同值"）
    ⇒ 与上面那几个列名同一档：**默认值不是"可以省略"**，而是"今天两族一样"。

    ## ⚠️⭐ 投递台账那一面（`delivery_model` / `callback_*`）：本族**必须自己表态**

    `delivery_model` 与 `callback_enabled_value` / `callback_disabled_reasons` **没有默认值**：
    共用投递引擎（`apps/openapi/dispatch.py`）的**每一次**取数、建行、状态判据都经它们
    ⇒ 漏一格的症状与上面那条同族、且更贵：**打手侧的回调静默写进上家侧的台账**（spec §2.2）。
    ⚠️ 如实说破一个**过渡形态**：打手侧那份 spec 的 `delivery_model` 今天**显式写着 `None`**
    （它那一族的投递台账表要到本线 T2 才建）⇒ 那是"这一族**还没有**投递台账"的**显式表态**、
    ⛔ 不是漏填，⛔ 更不许拿别族的模型当占位（那正是上面那句话说的形状）。
    ✅ 已在 T2 回填计划里登记（`apps/takerapi/credentials.py` 那一行旁 + 计划文件
    `docs/plans/2026-10-09-taker-callback.md` 的 **T2** 行：T2 建表后必须回填成
    `TakerApiDelivery`）。

    ⚠️⭐ **现行口径（评审收口）**：`locked_app_kind` **也是必填**——它原先带一个
    默认值 `None`，而那是一条 **fail-open**（漏写 ⇒ **整族凭据的端点白名单那道闸静默不存在**，
    且现有测试全绿）。逐条理由见那一格自己的注释。

    ## ⚠️ 取自模型枚举的那四个值（`enabled_state_value` / 三个 `*_scope`）

    它们**不从字面量来**，构造时取自模型自己的枚举（`ApiAppState.ENABLED` /
    `OpenApiRateBucket.Scope.*`）⇒ 枚举改名 / 改值时**编译期（构造期）**就是同一个值，
    ⛔ 不会出现"引擎里写着 `"enabled"`、模型已经换成别的成员"那种静默错位。
    ⚠️ 把它们放进 spec（而不是让引擎去 import 那两个枚举）还有一个**结构**理由：
    打手侧那两个枚举是**它自己的**（`TakerApp` 的 `state`、`TakerApiRateBucket.Scope`），
    引擎 import 哪一份都错。
    """

    # ── 表（**唯一**出处：限流那条裸 SQL 的表名与唯一约束名都由它现取）──
    #: 凭据模型（`app_key` / `app_secret` / `state` 住在它上面）。
    app_model: type
    #: 幂等台账模型（写端点的幂等键台账；`path` 那一列宽是超长路径闸的同源量）。
    idem_model: type
    #: 限流 / 失败计数桶模型（`(scope, ident, window_start)` 唯一）。
    bucket_model: type

    # ── 取自模型枚举的行为值 ──
    #: 凭据模型上"这个应用可用"那一个 `state` 值。
    enabled_state_value: str
    #: 按来源 IP 的**失败计数**落在哪个 `scope` 上（与下面两个额度各自成行）。
    failure_scope: str
    #: 兜底闸（按应用分桶）的 `scope`。
    app_fallback_scope: str
    #: 取令牌端点自己的兜底额度的 `scope`（与失败闸**同维不同用途** ⇒ 靠 `scope` 分行）。
    token_fallback_scope: str

    #: ⭐ 这一族里**哪一类行算「锁定应用」**（`ApiApp.kind` 的某个值）；`None` = 本族**没有**
    #: 这个概念（⇒ 端点白名单那道闸在**第一行**就返回）。
    #:
    #: 闸在两族共用的认证里。打手凭据表没有 kind 列，读 app.kind 会让打手侧每个请求 500。
    #: 本字段必填、没有默认值（所以排在有默认值的字段前面）。漏写是 fail-open：
    #: 唯一的消费者 `apps/openapi/permissions.py::enforce_app_endpoint_scope` 的**第一行**
    #: 就是 `if spec.locked_app_kind is None: return` ⇒ 谁新写一份 `CredentialSpec` 而
    #: **漏写**这一格，那**整族凭据的那道闸会静默不存在**（没异常、没日志、没人会收到信号），
    #: 而**现有测试全绿**（没有任何用例会因为"少了一道闸"而红）。
    #: ⇒ 必填之后，漏写 = **构造期**就 `TypeError`（与其余每一格**同一条**口径：
    #: "漏填一个 ⇒ 构造期响亮报错"）。
    #: ⚠️ **语义一字未变**：两族今天显式写的仍然是 `WECOM_GROUP_QR`（上家侧）与 `None`
    #: （打手侧）—— ⛔ 本次只关掉"漏写"那条路，⛔ 不改任何行为。
    #: ⚠️ 判据是一条**结构网**（⛔ 不是值断言）：
    #: `src/tests/test_credential_spec_locked_kind_required.py`。
    locked_app_kind: str | None

    #: ⭐ **绑定账号的那一列**（「令牌对应的打手账号必须可用」那道闸按它读人；D-0333）。
    #:
    #: ## 这道闸是什么（M3 半边收口，业务方拍板「需要的」）
    #:
    #: 停用 / 软删的打手（`is_active=False`——本仓软删账号的两笔写入
    #: `staff_profile.deleted_at` + `is_active=False` 在**同一个事务**里
    #: （`apps/accounts/views.py::AccountViewSet.delete`），恢复动作**碰都不碰**
    #: `is_active`（「恢复 ≠ 启用」，代码即口径）⇒ **`is_active` 一格就同时盖住
    #: 「停用」与「软删」两档**，判据与 App 令牌链既有那半边
    #: （`apps/core/authentication.py::AppTokenAuthentication` 的
    #: `if not token.user.is_active`）**同一口径**）⇒ 其已签发的对外令牌必须**立即失效**。
    #: 消费者有两处，都在**两族共用**的那份实现里：
    #: Bearer 认证引擎（`apps/openapi/authentication.py::ApiAppBearerAuthentication.authenticate`
    #: 的第 5.5 步）与取令牌共用流程
    #: （`apps/openapi/api/token_views.py::token_exchange_response` 的凭据判）。
    #: 失败一律回 `invalid_credentials_msg`（与「凭据无效 / 应用停用」**逐字同一句**）
    #: —— ⛔ 单独说破「账号被停用」等于给凭据持有者一条可探测「这个账号还活着吗」的信道
    #: （同族先例：`apps/takerapi/api/hall_views.py::NO_TAKER_NODE_MSG`）。
    #:
    #: ## 为什么这一格**必填**（`None` = 本族**没有**绑定账号的概念，⭐ 显式表态）
    #:
    #: 与上面 `locked_app_kind` **同一条 fail-open**： consumers 的第一行都是
    #: `if spec.bound_user_field is None: …（早退）` ⇒ 谁新写一份 `CredentialSpec`
    #: （第三族）而那一族**有**绑定账号却**漏写**这一格 ⇒ 那道闸**静默不存在**，
    #: 且现有测试全绿。⇒ 必填之后，漏写 = **构造期** `TypeError`。
    #: ⚠️ 两族今天显式写的值：上家侧 `None`（`ApiApp` **没有**「绑定账号」这一列——
    #: 它是上家工作室的通道，人不在本仓）、打手侧 `"taker"`（`TakerApp.taker`，
    #: **必填** FK、`PROTECT` ⇒ 取属性不会炸，详见 `apps/takerapi/models.py` ③）。
    #: ⚠️ 判据与 `locked_app_kind` 那条**同一家**的结构网：
    #: `src/tests/test_credential_spec_locked_kind_required.py`（两类并排）。
    bound_user_field: str | None

    # ── 文案（⛔ 全部与改造前**逐字相同**；改它 = 改对外契约）──
    #: 令牌不对 / 应用不存在 / 应用停用**三档共用**的那一句（防应用枚举面）。
    invalid_credentials_msg: str
    #: 令牌验签通过但已过期那一句（单独说破的理由见 `authentication` 的常量注释）。
    expired_msg: str
    #: 来源 IP 不在白名单内那一句。
    ip_not_allowed_msg: str
    #: 请求路径超过幂等台账列宽那一句的**模板**（`{}` 处填 `max_path_chars`）。
    #: ⚠️ 之所以是模板而不是成品句：数字必须**拼自列宽**（⛔ 不在文案里再写一次字面量，
    #: 两处数字会漂）—— 见 `path_too_long_msg`。
    path_too_long_template: str
    #: 「同一应用 + 同一来源单号」那颗**合成幂等键**的前缀。
    source_key_prefix: str
    #: `grant_type` 不是 `client_credentials` 时那句 `error_description`。
    unsupported_grant_type_description: str
    #: 取令牌端点被两道闸挡住时那句 `error_description`（**与调用内容无关**的固定一句）。
    rate_limited_description: str

    # ── 额度（⛔ 不是配额：口径见 `throttling` / `token_views` 的模块 docstring）──
    #: 按来源 IP 的失败闸：每 IP 每窗口允许失败几次。
    failure_max: int
    #: 失败闸的窗口长度（秒）。
    failure_window_seconds: int
    #: 兜底闸：每应用每窗口放行几次。
    app_fallback_max_requests: int
    #: 兜底闸的窗口长度（秒）。
    app_fallback_window_seconds: int
    #: 取令牌端点的兜底额度：每来源 IP 每窗口几次（**含成功与失败**）。
    token_fallback_max_requests: int
    #: 取令牌端点兜底额度的窗口长度（秒）。
    token_fallback_window_seconds: int

    # ── 投递台账（回调 **outbox + 台账** 那一面；spec §2.3）──
    #: 投递台账模型（outbox 与台账是**一张表两种角色**）。共用投递引擎的取活 / 建行 /
    #: 顺序丢弃 / 终态 / 清理**全部**经它现取 ⇒ ⛔ 引擎里不许出现任何具体模型名。
    #: ⚠️ 注解按 spec §2.3 是 `type`；打手侧那一份在本线 T2 建表之前**显式**写 `None`
    #: （见类 docstring 的「投递台账那一面」一节——那是表态，不是漏填）。
    #: ✅ 已在 T2 回填计划里登记（T2 建表后必须回填成 `TakerApiDelivery`）。
    delivery_model: type
    #: 这一族应用上"**回调开着**"那一个值（字面量，由各族构造时取自自己的枚举）。
    #: ⚠️ 与 `enabled_state_value`（应用本身开着）是**两个独立的开关**：
    #: 停用应用停整条对外通道、停用回调只停推送（spec §2.2 / §5）。
    callback_enabled_value: str
    #: 投递时发现应用不再可用 ⇒ 写进台账 `reason` 的**三句人话**。
    #: ⚠️ **顺序是契约**（spec §2.3）：`[0]` 应用停用 / `[1]` 回调停用 / `[2]` 地址为空。
    #: ⚠️ 两族**可以不同**（打手侧那三句说"打手侧…"）⇒ 必须由 spec 给，⛔ 不许在引擎里写死。
    callback_disabled_reasons: tuple[str, str, str]
    #: 手工重投时"那一行**不是**投递失败"那一句人话（`dispatch.prepare_retry` 抛出的消息）。
    #: ⚠️ 与 `callback_disabled_reasons` 同一条纪律：**两族各自的数据**，⛔ 不许在视图里
    #: 再写一份字面量（同一条闸两个文案 = 必然漂；收口前那两份逐字副本就是现场，
    #: 见 `dispatch.prepare_retry` 的 docstring）。
    #: ⚠️ 两族今天**逐字相同**是**有意的**：两条通道各自的对外契约恰好用了同一句话，
    #: 改一族不动另一族（判据按**值**写 ⇒ 谁改了一族，那一族的用例当场红、另一族照样绿）。
    delivery_retry_only_failed_msg: str

    # ── 列名（今天两族逐字相同 ⇒ 给了默认值；⚠️ dataclass 的字段序要求"有默认值的排最后"）──
    #: 凭据模型上那一列**公开**的应用标识（按设计不是机密，可以进日志）。
    app_key_field: str = "app_key"
    #: 凭据模型上那一列密钥（**存库可读**：验签要用；⛔ 不许进任何长期留痕）。
    secret_field: str = "app_secret"
    #: 幂等台账上装"请求路径"的那一列（超长路径闸按它的列宽判）。
    idem_path_field: str = "path"
    #: 桶模型上那条**唯一约束**的字段元组（限流 UPSERT 的 `ON CONFLICT` 按它认约束）。
    #: ⚠️ 按**字段元组**认，⛔ 不按"`constraints` 里的第一个"认（理由见 `rate_limit`
    #: 的 `_bucket_table_and_constraint`）。
    bucket_unique_fields: tuple[str, ...] = ("scope", "ident", "window_start")
    #: 这一族应用上装**回调地址**的那一列（投递前的三条判据之一按它判"地址为空"）。
    callback_url_field: str = "callback_url"
    #: 这一族应用上装**回调开关**的那一列（取值与 `callback_enabled_value` 比）。
    callback_state_field: str = "callback_state"
    # ── 投递台账的三个状态值（spec §2.3；两族**逐字同值**，理由见类 docstring）──
    #: 待投递（= 在飞：投递引擎的取活条件与"删除类事件"的去重判据都按它判）。
    delivery_pending_value: str = "pending"
    #: 已投递（投递成功 = 终态；只有它会被保留期清理删掉）。
    delivery_done_value: str = "done"
    #: 投递失败（终态失败：**保留行**、页面可见、可手工重投）。
    delivery_failed_value: str = "failed"

    @property
    def max_path_chars(self) -> int:
        """幂等台账 `path` 那一列的**容量**（字符数）—— 超长路径闸与列宽**同源**。

        ⛔ **不硬编码 191**：列宽改了（或这一族换了列 / 换了模型）这条闸**跟着改**，
        不会留下"注释说 191、列已经 255"那种静默错位
        （先例：`_is_shared_screenshot_key` 的列宽也是从 `_meta` 取的）。
        """
        return self.idem_model._meta.get_field(self.idem_path_field).max_length

    @property
    def path_too_long_msg(self) -> str:
        """路径超过列宽时的那句话（**对外契约的一部分**）。

        ⚠️ 数字**拼自** `max_path_chars`：模板 + 列宽 ⇒ 只有一个数字出处。
        """
        return self.path_too_long_template.format(self.max_path_chars)


def spec_of(request) -> CredentialSpec:
    """这一次请求该用**哪一份**凭据规格：从**认证态**取（⛔ 不从路径 / 参数猜）。

    ## 为什么这么取

    `@idempotent`（幂等入口）是**两个凭据族共用**的装饰器。⚠️ **现行口径（T2）**：
    它现在**自己收** `spec`（写端点显式声明自己哪一族，见 `idempotency.idempotent`），
    ⛔ 不再由本函数在运行期替它猜。本函数**仍然在职**：`order_create_views` 那条
    "同一应用 + 同一来源单号"的去重（`seen_source_response` / `remember_source`）按请求取。
    取法：DRF 会把"这一次是哪个认证器认得它"记在 `request.successful_authenticator` 上，
    而每个认证器自带它那一族的 `spec`（`ApiAppBearerAuthentication.spec`）⇒ 打手侧的
    认证子类只要 `spec = TAKER_SPEC`，共用引擎就自动落到**它自己**的台账 / 桶 / 文案上。

    ## 边界（三条；⛔ 第一条是"响亮"，⛔ 不是"回默认"）

    - `request` **根本没有** `successful_authenticator` 这个属性（= 直接调用 / 单元用法）
      ⇒ 回 `OPENAPI_SPEC`（改造前那份**硬编码**所指的东西 ⇒ 那些用法行为逐字不变）；
    - `successful_authenticator` 存在但**是 `None`**（DRF 在"这次请求没有任何认证器
      认得它"时就是这么记的）⇒ 同上，回 `OPENAPI_SPEC`；
    - ⭐⭐ **`successful_authenticator` 存在、却不是 `None`，但缺 `spec` 属性 / `spec`
      不是 `CredentialSpec`** ⇒ **抛 `ImproperlyConfigured`**（**T2**改的就这一条）。

    ## ⛔ 为什么第三条必须响亮（⛔ 不许回退成"回默认"）

    改之前第三条与第二条合在一起"回默认"，于是**认证器忘了声明 `spec`** 这件事
    **完全静默**：请求照常成功，只是落到**上家侧**的台账 / 桶 / 文案上 ——
    那是评审点名的"静默串台面"（同一种错在 `_find_existing` 那边是必填 keyword-only、
    当场 `TypeError`，在这里却按上家侧处理）。
    现在它在**第一次请求**就炸，排障信息里也不用去猜（消息写清"哪一路 / 缺什么"）。

    ⚠️ **消息里不带任何凭据值**：只说认证器那个**类名**（`spec` 缺失是"哪一类认证器"
    这件事，与凭据本身无关）⇒ ⛔ 不回显 `app_secret` / `app_key` / 令牌
    （`apps/openapi/models.py` 的纪律：密钥不进任何长期留存 / 错误消息）。
    """
    if not hasattr(request, "successful_authenticator"):
        # 直接调用 / 单元用法：请求对象上压根没有这一格（DRF 之外的形状）。
        return OPENAPI_SPEC
    authenticator = request.successful_authenticator
    if authenticator is None:
        # DRF 在"没有任何认证器认得这次请求"时记的就是 `None`（= 与"没有这一格"同义）。
        return OPENAPI_SPEC
    spec = getattr(authenticator, "spec", None)
    if not isinstance(spec, CredentialSpec):
        raise ImproperlyConfigured(
            f"认证器 {type(authenticator).__name__} 没有声明一份有效的 CredentialSpec："
            f"它的 spec 属性是 {type(spec).__name__ if spec is not None else '缺失'}。"
            "每一族认证器都必须显式声明自己那一份 spec"
            "（apps.openapi.credentials.OPENAPI_SPEC / 打手侧自己那一份），"
            "否则共用引擎会按上家侧的台账 / 桶 / 文案静默处理"
            "（见 apps/openapi/credentials.py 的模块 docstring）。"
        )
    return spec


#: ⭐ **开放平台（上家侧）那一族的规格** —— 值全部与改造前那四处硬编码**逐字相同**
#: （模型 / 文案 / 额度 / 窗口），所以 T2 是一次**行为逐字节不变**的重构。
#: ⚠️ 打手侧那一份（`TAKER_SPEC`）**不在这里、也不在本任务**：按 spec §4 的落地顺序，
#: 先把引擎全部改成"经 spec"，**再**由 T2b 在 `apps/takerapi/credentials.py` 里加第二份。
OPENAPI_SPEC = CredentialSpec(
    app_model=ApiApp,
    idem_model=OpenApiIdempotency,
    bucket_model=OpenApiRateBucket,
    enabled_state_value=ApiAppState.ENABLED,
    failure_scope=OpenApiRateBucket.Scope.IP,
    app_fallback_scope=OpenApiRateBucket.Scope.APP,
    token_fallback_scope=OpenApiRateBucket.Scope.IP_TOKEN,
    invalid_credentials_msg="应用凭据无效",
    expired_msg="令牌已过期",
    ip_not_allowed_msg="来源 IP 不在白名单内",
    path_too_long_template="请求路径过长（上限 {} 个字符）",
    source_key_prefix="openapi:source:",
    unsupported_grant_type_description="仅支持 client_credentials",
    rate_limited_description="请求过于频繁，请稍后再试",
    failure_max=20,
    failure_window_seconds=60,
    app_fallback_max_requests=6000,
    app_fallback_window_seconds=60,
    token_fallback_max_requests=600,
    token_fallback_window_seconds=60,
    # ⭐ 上家侧**有**"锁定应用"这一类行（企微群码上传专用，D1：同一张 `ApiApp` 表的另一类行）
    # ⇒ 端点白名单那道闸在本族**真的会判**（`apps/openapi/permissions.py`）。
    locked_app_kind=ApiAppKind.WECOM_GROUP_QR,
    # ⭐ 上家侧**没有**"绑定账号"这个概念：`ApiApp` 是**上家工作室**（把单派给我们的第三方）
    # 的通道，人不在本仓的账号表里 ⇒ 「绑定账号必须可用」那道闸在本族**第一行就返回**
    # （两个消费者的 `is None` 早退）。⛔ 必填字段不许靠默认值，**显式写 `None`**。
    bound_user_field=None,
    # ── 投递台账那一面（值全部与改造前 `apps/openapi/dispatch.py` 里的硬编码**逐字相同**）──
    delivery_model=OpenApiDelivery,
    callback_enabled_value=CallbackState.ENABLED,
    # ⚠️ 三句的**顺序是契约**：应用停用 / 回调停用 / 地址为空。
    # ⚠️ 下面这三句的字面量**改造前住在** `apps/openapi/dispatch.py`（`APP_DISABLED_REASON` 等
    # 三个模块名）——既有用例按那三个**模块名**取它们，故那边留着同名的再导出名（值取自这里）。
    callback_disabled_reasons=(
        "应用已停用，不再推送",
        "回调已停用，不再推送",
        "没有配置回调地址，不再推送",
    ),
    # ⚠️ 值**逐字取自**收口前那句模块常量（`apps/openapi/api/app_views.py` 的
    # `DELIVERY_RETRY_ONLY_FAILED_MSG`，T5b 收口时它已删除）⇒ 本族行为逐字不变。
    delivery_retry_only_failed_msg=(
        "只有投递失败的推送记录可以重投（已投递成功的记录重投会让对方收到两次）"
    ),
)


def app_is_enabled(app, spec: CredentialSpec) -> bool:
    if spec is OPENAPI_SPEC and getattr(app, "kind", None) == ApiAppKind.BUSINESS_INTEGRATION:
        return app.state == BUSINESS_INTEGRATION_ENABLED
    return app.state == spec.enabled_state_value
