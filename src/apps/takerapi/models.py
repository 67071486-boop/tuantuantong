"""打手侧开放接口的**数据面**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §3）。

本 app 在 `TENANT_APPS` 里 ⇒ **每个租户 schema 各一份**（与 `apps.business` / `apps.openapi` 同口径）：
表里**不带 `tenant_id` 列**，租户上下文只来自认证态 / schema 本身。

本模块**只有数据**：认证、令牌、幂等、限流、端点分别在后续任务落地（T2–T7）。
⚠️ 三张表的形状**刻意与 `apps.openapi` 的三张对应表同形**（`TakerApp` ↔ `ApiApp`、
`TakerApiIdempotency` ↔ `OpenApiIdempotency`、`TakerApiRateBucket` ↔ `OpenApiRateBucket`），
⛔ 但不是"复制一份业务逻辑"：共用的验签 / 幂等 / 限流实现由 T2 抽基类参数化（spec §4），
本模块只声明**列**。
"""
from django.conf import settings
from django.db import models


class AppState(models.TextChoices):
    """应用开关（spec §3.1）。

    ⚠️ **停用是唯一的关闭动作**（spec §3.1 末列：⛔ 没有 `DELETE`，只能停用）——
    与 `apps.openapi` 的 `ApiAppState` 同口径。停用 / 轮换**秒级生效、无宽限期**（spec §6 末行）。
    """
    ENABLED = "enabled", "启用"
    DISABLED = "disabled", "停用"


class CallbackState(models.TextChoices):
    """打手侧的**回调开关**（第二期 spec `docs/specs/2026-10-09-taker-callback-design.md` §3.1）。

    ⚠️ **默认 `ENABLED` 是有意选的**：地址为空本来就不投递（三条判据缺一不可：应用停用 /
    回调停用 / 地址为空）⇒ 默认值**不改变安全面**（逐条理由写在 `TakerApp.callback_state`
    的列注释里）。⛔ 它是一个**独立于 `AppState` 的开关**：停用应用 ⇒ 整条对外通道都不通；
    停用回调 ⇒ 只停推送。

    ## ⛔ 为什么本族**自己**定义一份，⛔ 不从 `apps.openapi.models` import

    两个成员名与取值（`enabled` / `disabled`）与上家侧的 `CallbackState` **逐字相同**——
    那**不是**"可以共用一个类"的理由：`apps.openapi` 是**另一个 app**，共用会把
    「打手侧的模型层」变成依赖「上家侧的模型层」（模型层不许跨 app 依赖）。
    真正的共享面在上游：两族的**投递引擎**只按 `CredentialSpec.callback_enabled_value`
    这个**字符串**比较（spec §2.2 第 4 项）⇒ 引擎那份实现只有一份，而枚举各归各家。
    ⚠️ 这个形状与 `AppState` ↔ `ApiAppState` 同款（那对也是各定义一份、取值相同）。
    """
    ENABLED = "enabled", "启用"
    DISABLED = "disabled", "停用"


# 内部绑定只用于 SDK 短期凭据，不可由旧版打手应用后台启用。
SDK_BINDING_APP_KEY_PREFIX = "sdk_"
SDK_BINDING_APP_STATE = "sdk_binding"


class TakerApp(models.Model):
    """打手侧接入应用 = 一套 `app_key` / `app_secret` + **一条必填的绑定打手账号**（spec §3.1）。

    ## ① 与 `apps.openapi.ApiApp` 的关系：**分开**，而且分开的好处是结构性的

    两张表都是"一套对外凭据"，但**服务的是两个相反方向的人**：`ApiApp` 是**上家**（把单派给我们）
    的通道，`TakerApp` 是**打手**（在我们这儿接单的外部工作室）的通道。

    分开的**承重理由不是"整洁"，而是"打手那把令牌在结构上打不开上家侧的端点"**（spec §2 末段）：
    两个 app 各有自己的认证类（T2 的 `TakerAppBearerAuthentication` / `ApiAppBearerAuthentication`，
    同属一个 `BearerCredentialAuthentication` 基类），而各自的认证类**只查自己那张表** ⇒
    拿 `TakerApp` 的令牌打 `/api/open/v1/…` 时，`ApiApp` 那张表里根本查不到这个 `app_key`，
    **必然 401**（反之亦然）。

    ⛔ **别把它改成"一张表加一列 `kind` 区分"**：那样两条通道会共用一次查表，
    "打手令牌打不到上家端点"就退化成**每条端点都要记得过滤**——漏一处就是静默全租户
    （spec §2 的红队 Blocking 项；同族：spec §3.2 末尾否掉"两张计数表合成一张带 `kind` 的多态表"）。

    ## ② `app_secret` **存库可读**是设计，⛔ 不是"忘了哈希"

    验签要用它算 HMAC（`apps.openapi.tokens` 的 `issue` / `verify` 按**字符串**参数化，
    spec §4-2：那份实现一个字都不用改）⇒ 哈希了就验不了。它与 `ApiApp.app_secret` 同口径：
    **存库可读**、**明文随行载荷出现在"返回应用"的响应里**（`D-0201` 的既有口径，spec §3.1）。

    ⛔ **仍然没有放松的那一条**：`app_secret` **不进**任何**日志 / 留痕 / 错误消息**
    （留痕文案里唯一允许出现的凭据是 `app_key`，它按设计**不是**机密：它要进
    `TakerApiRateBucket.ident` 之类的排障面）。

    ## ③ 范围**从 `taker` 那一行算**，⛔ 不存第二份授权

    这个应用能看什么、能做什么，全部由 `taker` 指向的那个账号**当场**决定（spec §3.1 / §5.3）：
    大厅按**他名下的游戏权限**过滤、抢单用**他的节点**（`nodes_of(taker)`）、
    "我的单"按 `Performance.filter(order=…, emp_type=MAIN).user_id == taker.id` 判。
    ⇒ 本表**没有**权限节点 M2M、**没有**游戏 M2M（`ApiApp` 那三列已在 T10 删除，本表从一开始就没有）。

    ⚠️ **代价（如实登记，spec §10-4）**：那个账号的职位 / 节点一改，外部系统的能力
    **静默跟着变**（管理页上必须把这句话写给租户看）。好处是**不会有两份授权互相漂移**
    （同族：`E-0021`"不建第二套授权存储"）。

    ## ⛔ 本表**没有**的东西（别顺手加回来）

    - ⛔ 不加 `callback_url` / `callback_state`：第一版**没有主动回调推送**（spec §3.1 / §10-1）。
      收了不生效是**静默**——比不收更糟，所以连列都不放。
      ⚠️⭐ **现行口径（T2）**：上面那句是第一版 spec（`2026-10-08-taker-open-api-design.md`）的口径，**已过期**；**第二期已经把这两列加上了**，出处 =
      `docs/specs/2026-10-09-taker-callback-design.md` **§3.1**（照 `ApiApp:117/123` 的同款口径）
      ⇒ ⛔ 别照本行旧句理解成"本表没有回调面"。
      **改准的依据**：旧句的承重点是"收了不生效是**静默**"——第二期把**入队 / 投递 / 台账 /
      页面**一并接上（本线 T3–T6），"收了不生效"这个前提**不再成立**；而"静默"那条**没有放松**
      （三条判据缺一不可：应用停用 / 回调停用 / 地址为空）。
      判据 = `tests/test_takerapi_model.py` 的两条**恰好集合**（`TakerApp` 列清单 + 本 app 模型名清单）。
    - ⛔ 不加 `tenant_id` / `tenant`：每个租户 schema 各一份（同 app 其它表的口径）。
    - ⛔ 不加 `service_account` 之类第二份账号指向：有且只有 `taker` 一条**必填**的绑定
      （`created_by` 记的是"谁建的"，与范围无关）。
    """

    name = models.CharField("应用名", max_length=50, unique=True)
    app_key = models.CharField("AppKey", max_length=64, unique=True)
    # ⚠️ 存库可读是**设计**（验签需要），⛔ 不是"忘了哈希"；见类 docstring 的 ②。
    app_secret = models.CharField("AppSecret", max_length=64)
    # ⭐ 绑定的打手账号（**必填**）：范围判据的唯一来源（spec §3.1 / §5.3）。
    # ⚠️ `PROTECT`：**不许**删掉一个还有接入应用的账号——那会让"这个应用能看什么"
    # 失去依据（应用行还在、指向的账号没了 ⇒ 范围变成"查不到那一行"，行为不可推理）。
    # ⚠️ `related_name="+"`：**故意不建反向访问器**——从 User 侧摸不到"这个人有几套凭据"
    # （没有消费方；建了就会变成一个没人维护、却会被后来者当接口用的入口）。
    taker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="绑定打手账号",
        on_delete=models.PROTECT,
        related_name="+",
    )
    # ⭐ 主动回调推送的接收地址（第二期 spec §3.1；由打手侧自己填）。
    # ⚠️ `max_length=200` 的取值理由（照 `apps/openapi/models.py` 的 `ApiApp.callback_url`
    # 那一段**同款口径**）：这是**我们自己**的出站投递地址（不是浏览器里的长链接：
    # 没有查询串、没有锚），200 已覆盖现实取值；且与 `TakerApp` 其它入参列
    # （`app_key` / `app_secret` 都是 64）同一量级 ⇒ **页面输入框的限长与库列宽同源**，
    # ⛔ 不会出现"页面让填、库里静默截断"。真需要更长要**改列宽 + 配迁移**，那是显式动作。
    # ⚠️⛔ **仅接受 https**、且必须**公网地址**（私网 / 回环 / 链路本地一律拒）、URL 内不带凭据
    # —— 校验发生在「保存时」与「每次投递前」两个时点（spec §8.4；DNS 会变，只校验一次是假的）。
    # ⛔ 本列在这里**只是列与注释**：校验逻辑是 T5 的事，本任务**有意不实现**
    # ——所以库里今天可以存在非 https / 私网地址的行。
    callback_url = models.URLField("回调地址", max_length=200, blank=True, default="")
    # ⭐ 回调开关（第二期 spec §3.1）：停用 ⇒ 不产生投递（不排队、不重试、不报错）。
    # ⚠️ **默认 `enabled` 是「有意选的默认值」**，理由是：`callback_url` 为空时本来就**不投递**
    # （三条判据缺一不可：应用停用 / 回调停用 / 地址为空）⇒ 新建应用这两列都是「没配」状态，
    # 默认 `enabled` **不改变安全面**：它既不会凭空产生投递，也不会绕过 https / 公网那两道校验
    # （校验与开关无关）。⛔ 别把这里读成"默认打开就是放行"——放行的前提是打手侧**自己填了合法地址**。
    # ⚠️ 它与 `state` 是**两个独立开关**：停用应用停整条对外通道、停用回调只停推送。
    callback_state = models.CharField(
        "回调开关",
        max_length=16,
        choices=CallbackState.choices,
        default=CallbackState.ENABLED,
    )
    # 唯一开关（停用 = 秒级失效）；⛔ 没有 `DELETE`。
    state = models.CharField(
        "状态", max_length=16,
        choices=[*AppState.choices, (SDK_BINDING_APP_STATE, "业务 SDK 内部绑定")],
        default=AppState.ENABLED,
    )
    # 每行 / 逗号一个 IP 或网段；**空 = 不限制**。
    ip_allowlist = models.TextField("IP 白名单", blank=True, default="")
    # 最近一次**验签成功**（用于"这个应用还用不用"）。⛔ 不是"最近一次调用"。
    last_used_at = models.DateTimeField("最近使用时间", null=True, blank=True)
    # 轮换时刻（停用 / 轮换秒级生效，无宽限期；spec §6）。
    secret_rotated_at = models.DateTimeField("密钥轮换时间", null=True, blank=True)
    # 谁建的（打手自己建的 = 他自己）。⛔ 它**不是**范围判据（那是 `taker`）。
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "打手侧接入应用"
        verbose_name_plural = verbose_name
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(app_key__startswith=SDK_BINDING_APP_KEY_PREFIX, state=SDK_BINDING_APP_STATE)
                    | (~models.Q(app_key__startswith=SDK_BINDING_APP_KEY_PREFIX)
                       & ~models.Q(state=SDK_BINDING_APP_STATE))
                ),
                name="takerapp_sdk_binding_state",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}({self.app_key})"


class TakerApiIdempotency(models.Model):
    """打手侧写端点的**幂等键台账**（列与 `apps.openapi.OpenApiIdempotency` 逐一对应，spec §3.2）。

    写端点必须带 `Idempotency-Key`（8–128 字符；缺失 / 超长 ⇒ `1000`）：命中同一键 ⇒
    **回放**上一次的响应快照（⛔ 不重复执行副作用）；同一个键配**不同**请求体 ⇒ 业务码 `1000`
    （⛔ 不静默返回上一个响应）。

    ⚠️ `request_hash` 存的是**请求体的 sha256 hex**（64 字符）——用它分辨"同键同体（回放）"
    与"同键异体（拒绝）"，⛔ 我们**不存原始请求体**（那会把对方报文里的内容原样留库，
    而留库的东西会进备份 / 排障会话）。

    ⚠️ **写入方是本 app 自己的 `CredentialSpec`**：读 / 写这张表的三个函数
    （`apps/openapi/idempotency.py` 的 `_run` / `_replay` / `_existing_outcome`，以及
    `_find_existing` / `_prune` / `seen_source_response` / `remember_source`）
    **一律收一份 `CredentialSpec`**（`apps/openapi/credentials.py::CredentialSpec`），
    taker 侧显式传 `apps/takerapi/credentials.py::TAKER_SPEC` ⇒ ⛔ 不存在第二份幂等实现
    （spec §4-3）。
    ⛔⭐ **现行口径（T2b）**：本行不由 T2 收一个 `model` 参数——
    实际收的是**整个 `CredentialSpec`**（不只是台账模型：`spec` 还带列名
    `idem_path_field` 与超长路径那条闸的文案模板）。⛔ 别照旧句理解为"传一个模型类就行"：
    照那个理解去写第二族，`_prune` / `MAX_PATH_CHARS` / `source_key` 前缀那三处会**静默**
    落到上家侧（spec §4 那张表的第 2–4 项）。

    ⛔ 本表**没有** `tenant_id`（同 app 其它表的口径）。
    """

    app = models.ForeignKey(
        "takerapi.TakerApp",
        verbose_name="打手侧应用",
        on_delete=models.CASCADE,
        # ⚠️ `related_name="+"`：幂等台账**没有反向消费方**（从应用侧数"它有多少幂等键"没人要）。
        related_name="+",
    )
    # 调用方给的键（契约长度 8–128；列宽按上限 191 留余量以兼容既有口径）。
    key = models.CharField("幂等键", max_length=191)
    method = models.CharField("请求方法", max_length=8)
    path = models.CharField("请求路径", max_length=191)
    # 请求体 sha256 的 hex（64 字符）。
    request_hash = models.CharField("请求体摘要", max_length=64)
    # 上一次的响应快照（回放时逐字返回它）。⚠️ 快照里**不许**有 `app_secret`
    # （`D-0201` 不放松的那一格：密钥不进任何留痕）。
    response_snapshot = models.JSONField("响应快照", default=dict, blank=True)
    status_code = models.PositiveSmallIntegerField("响应状态码")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "打手侧幂等台账"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        # ⚠️ 唯一性**只由这条具名约束**保证（⛔ 不给 `key` 加 `unique=True`：
        # 那会变成第二棵唯一索引，而真正的判据是「**同一个应用内**唯一」——
        # 不同应用撞同一个键是**合法**的，加 `unique=True` 会把它们一起拒掉）。
        constraints = [
            models.UniqueConstraint(
                fields=["app", "key"], name="uniq_taker_idem_app_key"
            ),
        ]
        # ⚠️ **单列** `created_at` 索引，专供机会式清理那条
        # `DELETE … WHERE created_at < <明确算出的时刻> LIMIT 500`。⛔ 它不是"顺手加的"：
        # 上面那条唯一约束的前缀是 `app`，而那条 `DELETE` 的取数面只有 `created_at`
        # ⇒ 用不上它，没有这条索引就是每次全表扫（本表**只增不减**，会一直长）。
        # （与 `OpenApiIdempotency` / `OpenApiRateBucket` 的两条同族索引**同一个理由**。）
        indexes = [
            models.Index(fields=["created_at"], name="taker_idem_created_idx"),
        ]

    def __str__(self) -> str:
        return f"打手侧幂等台账:{self.app_id}/{self.method} {self.path}/{self.key}"


class TakerApiRateBucket(models.Model):
    """打手侧限流计数桶（列与 `apps.openapi.OpenApiRateBucket` 逐一对应，spec §3.3 / §4-4）。

    本表把**应用档**与**来源 IP 档**两个计数维度放进**同一张表**，用 `scope` 分成**两种不同的行**
    ——`scope="app"` 的行装 `app_key`、`scope="ip"` 的行装来源 IP。
    ⛔ 这**不等于**"把两个桶的计数合并"：两个档的**阈值各自独立**，`(scope, ident)` 不同 ⇒ 行不同
    ⇒ 计数天然隔离（表的形状与阈值的语义是两件事）。

    ## 为什么换 DB（⛔ 不用进程内的限流存储）

    生产是 `gunicorn --workers 2`（实测）⇒ 进程内缓存的桶**每个 worker 各一份**，
    实际额度 ≈ 设定值 × worker 数。固定窗口口径不变（跨边界最坏两倍放行），
    一次 `INSERT … ON CONFLICT DO UPDATE` 完成「判 + 记」。

    ⚠️ **本任务（T1）不实现阈值逻辑**（那是 T2 参数化 + T3/T5 的调用点）：这里只有列与唯一键。

    ⛔ 本表**没有** `tenant_id`（同 app 其它表的口径）。
    """

    class Scope(models.TextChoices):
        """计数维度。⚠️ 三个成员与 `OpenApiRateBucket.Scope` **逐字一致**（spec §3.3）。
        """
        APP = "app", "按应用"
        IP = "ip", "按来源 IP"
        #: 取令牌端点的**兜底额度**那一行（值与 `ip` 同维，差的是**用途 / 阈值**）。
        #: ⚠️ 8 个字符**恰好**占满 `max_length=8`（⛔ 别加长这个名字）。
        IP_TOKEN = "ip_token", "按来源 IP（取令牌端点）"

    scope = models.CharField("维度", max_length=8, choices=Scope.choices)
    # ⚠️ `ident` 的语义**由 `scope` 决定**：`app` 档存 `app_key`（明文，按设计不是机密），
    # `ip` 档存来源 IP。⛔ 不要拿它当"通用标识"跨档比较。
    ident = models.CharField("维度取值", max_length=64)
    #: 固定窗口的左端（口径沿用现实现：**固定窗口**，⛔ 不是滑动窗口）。
    window_start = models.DateTimeField("窗口开始")
    count = models.PositiveIntegerField("计数", default=0)

    class Meta:
        verbose_name = "打手侧限流桶"
        verbose_name_plural = verbose_name
        ordering = ["-window_start", "-id"]
        # ⚠️ 唯一性**只由这条具名约束**保证，⛔ 不给三列各自加 `unique=True`。
        # ⚠️ `scope` **必须**进这个唯一键：它是"几种行互不干扰"的载体。
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "ident", "window_start"],
                name="uniq_taker_rate_bucket",
            ),
        ]
        # ⚠️ **单列** `window_start` 索引，专供机会式清理那条
        # `DELETE … WHERE window_start < <时刻>`。⛔ 它不是"顺手加的"：
        # 上面那条唯一索引的前缀是 `scope`，这个 `DELETE` 用不上它。
        indexes = [
            models.Index(fields=["window_start"], name="taker_rate_win_idx"),
        ]

    def __str__(self) -> str:
        return f"打手侧限流桶:{self.scope}/{self.ident}/{self.count}"


class TakerApiDelivery(models.Model):
    """打手侧主动回调推送的 **outbox + 台账**（**一张表两种角色**，第二期 spec §3.2）。

    ## ⛔ 这是**打手侧**的那一张，与上家侧的 `OpenApiDelivery` **各一张、⛔ 不共用**

    两族的列**逐一对应**（同一个投递引擎按 `CredentialSpec.delivery_model` 现取），但表是**两张**：

    - 上家侧那张（`apps.openapi.OpenApiDelivery`）的收件人是**上家**（把单派给我们的第三方）；
    - 本表的收件人是**打手**（在我们这儿接单的外部工作室，`TakerApp`）。

    ⚠️⭐ **两族"共用一张表"是最贵的那个形状**（spec §2.2 第 10 项）：引擎照常跑、一行异常都没有，
    只有"一族的回调写进了另一族的台账、投递给错的人"这一件事**没有任何症状**。
    ⇒ 判据不是"注释里写了分开"，而是 `TAKER_SPEC.delivery_model is TakerApiDelivery`
    这个**身份**（`tests/test_takerapi_auth.py` / 第二期 T3 的跨族不串台用例）。

    ## 为什么不拆两张（outbox / 台账）

    与上家侧同款理由：两者的生命周期同形（建 → 投递 → 落状态），拆开就会造出一份必须同步的
    重复事实（形状先例 = `apps.business.models.PeerSyncEvent`）。

    ## 为什么表里**没有**签名，⛔ 也别顺手加一个 `sign` 列

    `payload` 是**已定稿、待投递**的报文主体，里面**不含签名**：签名在**投递那一刻**用**当时的**
    `TakerApp.app_secret` 现算。理由 = 密钥可以轮换 —— 把签名存进 outbox 等于把签名**钉死在
    写行那一刻的密钥**上 ⇒ 轮换之后尚未投递的行会带着一把**已作废的密钥**算出的签名送出去，
    对端一律验不过，而失败原因看起来像"对方不认我们的签名"（极难排查）。

    ## ⭐ 四类事件（`Event`）与它们的语义

    | 事件 | 什么时候入队 | 报文里的额外字段 |
    |---|---|---|
    | `order.status_changed` | 他接的单**状态变化**（走状态机的那条路） | `status` / `note` |
    | `order.deleted` | 单被**软删** | 无（只有 8 个通用键 + `timestamp`/`sign`） |
    | `order.image_added` | **进度图认领成功**（`img_type=0` 那一档） | `images` |
    | ⭐ `order.reassigned` | **该单不再属于原来那位接手人**（换人 / 清接手人 / 撤单回池 / 退单 / 删单） | `reason`（机器可读：`taker_removed` / `refunded` / `deleted`） |

    ⚠️⭐ **`order.deleted` 与 `order.reassigned(reason=deleted)` 可能同时推**（同一张单被删时
    既"没了"也"不再是你的"）—— 这是**有意**的，⛔ 不是重复：对外语义不同（前者说"这单不存在了"，
    后者说"你不该再干它"）。契约里逐字写明（spec §4.1 / §10-5）。
    ⚠️ 事件名的**取值**与上家侧那三个**逐字相同**（引擎只按字符串比较，spec §2.2 第 4 项），
    但枚举各归各家（理由同 `CallbackState`）⇒ ⛔ 别从 `apps.openapi.models` import。

    ## 状态与重试

    `status` + `attempts` + `next_attempt_at` 三个列驱动重试（退避表 `10s/60s/5m/15m/1h/6h`，
    第 7 次失败才终态），⛔ 不用 `sleep` 阻塞循环。终态标记 `failed` 但**保留行**
    （页面可见、可手工重投）。

    ## ⛔ 本表**没有**的东西（别顺手加回来）

    - ⛔ 不加 `tenant_id`：本 app 在 `TENANT_APPS` 里 ⇒ 每个租户 schema 各一份。
    - ⛔ 不加"租户标识"列：租户上下文只来自认证态 / schema 本身（`AGENTS.md` §4 的红线）。
    """

    class Event(models.TextChoices):
        """四类推送事件（第二期 spec §3.2 / §4.1；⛔ 不含"大厅来了新单"那一类）。

        ⚠️ 前三类的**取值**与上家侧的 `OpenApiDelivery.Event` **逐字相同**（引擎按字符串比较）；
        第四类 `order.reassigned` 是**本线独有**的（spec §1 末段裁定）。
        """
        ORDER_STATUS_CHANGED = "order.status_changed", "订单状态变化"
        ORDER_DELETED = "order.deleted", "订单删除"
        ORDER_IMAGE_ADDED = "order.image_added", "订单新增进度图"
        ORDER_REASSIGNED = "order.reassigned", "订单不再属于该接手人"

    class Status(models.TextChoices):
        """三个投递状态。⚠️ **取值逐字同于上家侧**（引擎按 `spec` 里的字符串比较）。"""
        PENDING = "pending", "待投递"
        DONE = "done", "已投递"
        FAILED = "failed", "投递失败"

    app = models.ForeignKey(
        "takerapi.TakerApp",
        verbose_name="打手侧应用",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    event = models.CharField("事件类型", max_length=32, choices=Event.choices)
    order_no = models.CharField("订单号", max_length=64)
    # 对端的来源单号：我们自己的单可能没有来源单号 ⇒ 允许空串（⛔ 不让它挡住写行）。
    source_order_no = models.CharField("来源单号", max_length=64, blank=True, default="")
    # 已定稿、待投递的**报文主体**。⚠️ **不含签名**——签名在投递时用当时的密钥现算（见类 docstring）。
    payload = models.JSONField("报文主体", default=dict, blank=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("尝试次数", default=0)
    # 最近一次失败原因（人看的诊断文本）。
    # ⚠️ 这是 `TextField` ⇒ **没有列级长度**，写入方必须**自己截断**（约定上限 2000 字符，
    # 照 `apps.business` 里失败原因类列的既有口径）；⛔ 别把整段对方响应体直接塞进来
    # ——那会让一行台账变成几 MB，且页面上也读不了。
    reason = models.TextField("最近一次失败原因", blank=True, default="")
    # 下一次可投递时刻（退避表驱动它）：为空 = 立刻可投。
    next_attempt_at = models.DateTimeField("下次可投递时刻", null=True, blank=True)
    # 对方回执的 HTTP 状态码：3xx / 5xx 都是失败，但**码要看得到**（排障靠它）。
    response_status = models.PositiveSmallIntegerField(
        "回执状态码", null=True, blank=True
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    processed_at = models.DateTimeField("处理时刻", null=True, blank=True)

    class Meta:
        verbose_name = "打手侧推送台账"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        # ⚠️ 索引的取舍理由（照上家侧那张的**取数路径**重新取舍，⛔ 不是照抄三条）：
        # ① `(status, next_attempt_at)` **必须**：常驻投递进程每 10 秒取一次「待发 / 到点重试」
        # 的行，这是它**唯一**的取数条件，没有它就是每次全表扫；
        # ② `app`：管理页的「最近推送记录」按应用取最近 N 条（`app_id` 排序取数）
        # ⇒ 单列索引就是它的取数面（`source_order_no` / `event` 没有这类取数路径，⛔ 不建）。
        # ⛔ 不建 `(app, event, order_no)` 那条：本表的"同一件事"由 *(app, event, order_no)* 表达，
        # 而它**不是**唯一键（同一订单的状态可以变多次）⇒ 建唯一索引会**丢事件**，
        # 建普通索引又没有取数路径。
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="taker_deliv_due_idx"),
            models.Index(fields=["app"], name="taker_deliv_app_idx"),
            # ⚠️ **单列** `created_at` 索引，专供机会式清理那条
            # `DELETE … WHERE status='done' AND created_at < <明确算出的时刻> LIMIT 500`
            # （`apps/openapi/dispatch.py::prune_done_deliveries`，两族共用同一份实现）。
            # ⛔ 它不是"顺手加的"：上面那条 `(status, next_attempt_at)` 的**第二列**是
            # `next_attempt_at`（终态行那一列是 NULL，`created_at` 用不上它）、`app` 那条前缀是
            # `app_id` ⇒ 那条清理的取数面只有 `created_at` 一列，没有本索引就是每次全表扫
            # （本表**只增不减**，会一直长）。
            # （与 `OpenApiDelivery` / `TakerApiIdempotency` 的同族索引**同一个理由**。）
            models.Index(fields=["created_at"], name="taker_deliv_created_idx"),
        ]

    def __str__(self) -> str:
        return f"打手侧推送台账:{self.event}/{self.order_no}/{self.status}"
