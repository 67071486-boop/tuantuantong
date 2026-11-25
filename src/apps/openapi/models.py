"""开放平台：第三方应用的凭据与授权范围（spec `docs/specs/2026-10-07-open-platform-api-design.md` §5）。

本 app 在 `TENANT_APPS` 里（D1）⇒ **每个租户 schema 各一份**，与 `apps.business` 同口径：
表里**不带 `tenant_id` 列**，租户上下文只来自认证态 / schema 本身。

本模块**只有数据**：签名、认证、权限、限流、端点分别在后续任务落地（T2–T6）。

⚠️ **v2 追加（`docs/specs/2026-10-08-open-platform-v2-parity-design.md` §7 / §6.3 / §10）**：
本模块加了四张表的定义 —— `ApiApp` **加两列**（`callback_url` / `callback_state`，§7.1），
**新建三张表** `OpenApiDelivery` / `OpenApiIdempotency` / `OpenApiRateBucket`。
⚠️ 三张表**必须**留在这个 app 里（它已在 `TENANT_APPS`）⇒ 表建在**租户 schema**、⛔ 不进 `public`；
这是 `tests/test_tenant_isolation.py` 与 `tests/test_p2_isolation.py` 的判据（那两张网按 app 配置推导表名）。
⛔ 本任务（T1）**只加不删**：校验 / 限流阈值 / 投递 / 幂等回放这些**逻辑**分属 T3–T6，此处只有列与注释。
"""
from django.conf import settings
from django.db import models


class ApiAppState(models.TextChoices):
    """应用开关（§5.1）。⚠️ **停用是唯一的关闭动作**（业务方 选定「只能停用不能删」，§7.3）。"""
    ENABLED = "enabled", "启用"
    DISABLED = "disabled", "停用"


# 旧版只接受 enabled，回滚代码时要继续拒绝业务 SDK 凭据。
BUSINESS_INTEGRATION_ENABLED = "sdk_enabled"


class CallbackState(models.TextChoices):
    """回调开关（v2 设计文档 §7.1）。

    ⚠️ **默认 `ENABLED` 是有意选的**：地址为空本来就不投递（§7.1）⇒ 默认值不改变安全面
    （理由逐条写在 `ApiApp.callback_state` 的列注释里）。⛔ 它是一个**独立于 `ApiAppState` 的开关**：
    停用应用 ⇒ 整条对外通道都不通；停用回调 ⇒ 只停推送、查单照旧。
    """
    ENABLED = "enabled", "启用"
    DISABLED = "disabled", "停用"


class ApiAppKind(models.TextChoices):
    """应用用途（`docs/specs/2026-10-09-wecom-group-qr-auto-upload-design.md` §5.1 / D1）。

    ⚠️ 它**不是**另一张表：`ApiApp` 里「企微群码上传专用」与「通用」是**同一张表的两类行**
    （D1）⇒ 令牌 / 验签 / 推送 / 台账 / 轮换这些机制**全部复用**，本线不新造另一套。

    ⚠️ `GENERAL` 是**既有行与新建行的默认值**：下面 `ApiApp.kind` 的列注释解释了为什么
    这个默认值让**既有数据零变化**。
    """
    GENERAL = "general", "通用"
    WECOM_GROUP_QR = "wecom_group_qr", "企微群码上传专用"
    BUSINESS_INTEGRATION = "business_integration", "业务 SDK 专用"


class ApiApp(models.Model):
    """一个开放平台应用 = 第三方拿到的一套 AppKey / AppSecret + 它的授权范围（spec §5.1）。

    ## 为什么 `app_key` 有唯一索引，而 peer 那套是逐行扫哈希（§5.2 / §5.4）

    内网那条 peer 通道（`PeerTokenAuthentication`）是**逐行比对哈希**的：
    `for candidate in <绑定表>.objects.all(): if token_matches(raw, candidate.…hash)` ——
    每个请求 O(绑定数) 次摘要。对内只有几条绑定，这个代价可以接受。

    **对外会有几十上百个应用，而且每次请求都要付这个代价** ⇒ 本线**有意不照抄**：
    按 `app_key` **唯一索引直接查一行**，只对命中的那一行做一次摘要比对。
    ⛔ **这张唯一索引是承重的，不要为了「跟 peer 统一风格」把它去掉** ——
    去掉之后每请求都会退化成全表扫描 + 逐行算摘要，而**功能上看不出任何异常**（只有量级变化）。
    ⚠️ 也正因为它是明文入库的唯一键，`app_key` 本就不是机密（它可以出现在日志与排障会话里，
    §9-15）；机密只有 `app_secret`。

    ## `app_secret` 的纪律

    **存库可读**（验签要算 HMAC，哈希了就验不了）。明文出现在所有「返回应用」的响应里
    （列表 / 创建 / `PATCH` / 轮换；`app_row_payload` 是唯一装配出口），管理页上也明文展示。
    ⛔ 别改回 `write_only` 或「只能看一次」。
    ⛔ **不进**日志 / 留痕 / 错误消息（留痕里出现的是 `app_key`，它不是机密）。
    写进留痕是缺陷。

    ## ⛔ 授权范围的三列已于 T10 删除（设计文档 §13.1）

    本表原有三列表达「这个应用被授权了什么」：一个权限节点 M2M、一个游戏 M2M，以及一个
    「全部游戏（含以后新增的）」布尔开关。v2 的收口任务把它们**整体删掉**
    （迁移 `0007_remove_apiapp_all_games_remove_apiapp_games_and_more`，`RemoveField` × 3）。

    「一个应用凭据 = 本租户全部业务数据」只对 `kind=general` 成立：对外端点不再有节点门，
    也不再按游戏范围过滤。
    锁定应用（`ApiAppKind.WECOM_GROUP_QR`，每租户一把）另有范围：只能读订单、只能写群聊二维码，
    白名单外的对外端点回 403（判据在 `apps/openapi/permissions.py`）。
    那张白名单是认证层的 `(HTTP 方法, url_name)`，不是已经删掉的三列。

    ⚠️ **代价（如实登记，⛔ 不许只写好处）**：授权面从"可收窄"变成"整租户"
    ⇒ 泄漏一把 `app_secret` 的爆炸半径 = 该租户**全部业务数据**（能读能写、能改价、能读账单、
    能读成本与提成）。重新评估触发点见设计文档 §14。

    ## 本表**没有**的东西（别顺手加回来）

    - ⛔ 不加 `tenant_id`：每个租户 schema 各一份（与 `apps.business` 同口径）。
    - ⛔ 不加 `service_account` / 任何指向账号的外键：业务方 第二轮**取消专用账号**（D10），
      应用的身份就是它自己的 `app_key` / `app_secret`（⛔ 不挂任何账号）。
      ⚠️ 原句后半截「应用的权限与游戏范围**直接挂在应用自己身上**」已随那三列在 T10 删除（见上一节）。
    - 本 app 另有三张表：`OpenApiDelivery`（推送 outbox）、`OpenApiIdempotency`（写端点幂等）、
      `OpenApiRateBucket`（限流计数，多 worker 下不能用进程内 cache）。
      ⛔ `ApiApp` 自己不加计数字段。
    - ⛔ 不加「上一把密钥 + 失效时刻」：本版轮换是**单密钥、立即失效、没有宽限期**（§5.3）。
    """

    name = models.CharField("应用名", max_length=50, unique=True)
    app_key = models.CharField("AppKey", max_length=64, unique=True)
    # ⭐ 用途（企微群码线 §5.1 / D1）：两类行住同一张表（见 `ApiAppKind` 的 docstring）。
    # ⚠️ **`default=GENERAL` 让既有行零变化**：本列是 `AddField` 加上去的，迁移把**既有每一行**
    # 都写成长度 32 的 `general`（没有数据回填语句，§5.1 明令不写回填）⇒ 今天所有应用
    # 与新建的普通应用取值**逐字相同**，既有查询 / 序列化器 / 页面读到的还是原来那套语义。
    # ⚠️ 约束**只对 `wecom_group_qr` 这一档生效**（见 `Meta.constraints` 那条条件唯一约束）：
    # `general` 行**可以**有很多条，本列对它们只是一个恒为 `general` 的取值。
    kind = models.CharField(
        "用途", max_length=32, choices=ApiAppKind.choices, default=ApiAppKind.GENERAL
    )
    # ⚠️ 存库可读是**设计**（验签需要），⛔ 不是"忘了哈希"；见类 docstring 的纪律。
    app_secret = models.CharField("AppSecret", max_length=64)
    # ⛔⭐ T10：本处原有三列表达授权范围（权限节点 M2M / 游戏 M2M /
    # 「全部游戏（含以后新增的）」布尔）。它们随 v1 面一起删除（迁移 `0007_*`：
    # `RemoveField` × 3），理由与代价见类 docstring 的「授权范围的三列已于 T10 删除」一节。
    # ⭐ 主动回调推送的接收地址（v2 设计文档 §7.1；由租户自己填）。
    # ⚠️ `max_length=200` 的取值理由：这是**我们自己**的出站投递地址（不是浏览器里的长链接：
    # 没有查询串、没有锚），200 已覆盖现实取值；且与 `ApiApp` 其它入参列（`app_key` /
    # `app_secret` 都是 64）同一量级 ⇒ 页面输入框的限长与库列宽同源，
    # ⛔ 不会出现"页面让填、库里静默截断"。真需要更长要**改列宽 + 配迁移**，那是显式动作。
    # ⚠️⛔ **仅接受 https**：http 与其它协议一律不收（§4-V6）。
    # ⚠️⛔ **还要做 SSRF 校验**：解析后的 IP 不得落在**私网 / 回环 / 链路本地 / 保留段**
    # （`127.0.0.1` 打的是本机 gunicorn，`169.254.169.254` 是云元数据地址）。
    # ⚠️ **校验发生在「保存时」与「每次投递前」两个时点**（DNS 会变，只在保存时校验一次是假的）。
    # ⛔ 本列在这里**只是列与注释**：校验逻辑是后续任务（T6，`outbound.py`）的事，
    # 本任务（T1）**有意不实现**——所以库里今天可以存在非 https / 私网地址的行。
    callback_url = models.URLField("回调地址", max_length=200, blank=True, default="")
    # ⭐ 回调开关（§7.1）：停用 ⇒ 不产生投递（不排队、不重试、不报错）。
    # ⚠️ **默认 `enabled` 是「有意选的默认值」**，理由是：`callback_url` 为空时本来就**不投递**
    # （§7.1 第一句）⇒ 新建应用两列都是「没配」状态，默认 `enabled` **不改变安全面**：
    # 它既不会凭空产生投递，也不会绕过 https / SSRF 校验（那两道判据与开关无关）。
    # ⛔ 别把这里读成"默认打开就是放行"——放行的前提是租户**自己填了合法地址**。
    callback_state = models.CharField(
        "回调开关", max_length=16, choices=CallbackState.choices, default=CallbackState.ENABLED
    )
    state = models.CharField(
        "状态", max_length=16,
        choices=[*ApiAppState.choices, (BUSINESS_INTEGRATION_ENABLED, "业务 SDK 启用")],
        default=ApiAppState.ENABLED,
    )
    # 每行 / 逗号一个 IP 或网段；**空 = 不限制**（默认空，§14-Q13 建议第一版不做白名单）。
    ip_allowlist = models.TextField("IP 白名单", blank=True, default="")
    # 最近一次**验签成功**（用于"这个应用还用不用"）。⛔ 不是"最近一次调用"。
    last_used_at = models.DateTimeField("最近使用时间", null=True, blank=True)
    secret_rotated_at = models.DateTimeField("密钥轮换时间", null=True, blank=True)
    # `related_name="+"`：**故意不建反向访问器** —— 从 User 侧摸不到"这个人建过哪些应用"
    # （没有消费方；建了就会变成一个没人维护、却会被后来者当接口用的入口）。
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "开放平台应用"
        verbose_name_plural = "开放平台应用"
        ordering = ["id"]
        # ⭐ 条件唯一约束（企微群码线 §5.1 / D2）：**只**约束 `kind=wecom_group_qr`
        # 那一档（`condition=` 只把这一档的行纳入索引）⇒ 「锁定应用一个租户一把」这条口径
        # 落成**库级判据**，而不是应用层那种「先 `exists()` 再建」的检查
        # ⚠️ **为什么必须是库级**：先查后建的检查**在并发下会被穿过** —— 两个创建请求
        # 各自查到「还没有」⇒ 两行都写进去，而两边都返回成功、不报错
        # （症状是"页面上出现了两把锁定应用"，且看不出是哪一步漏的）。
        # ⚠️ 这正是 §5.2 要求「并发撞约束必须翻译成业务码 1000 + 一句人话、⛔ 不是 500」的
        # 前提：本条约束是那个 `IntegrityError` 的**唯一来源**。
        # ⚠️ **「每租户一把」是怎么来的**：本 app 在 `TENANT_APPS` 里（见模块 docstring）
        # ⇒ 本表建在**每个租户 schema 各一份**、表名也各一份 ⇒ 索引只在**那个 schema 内**生效。
        # 所以「一个租户只能有一把锁定应用」不需要额外的租户列来承担（本表按设计**没有**
        # `tenant_id`，见类 docstring）。⚠️ 这是**实测过**的形态（`tests/test_openapi_wecom_group_qr.py`
        # 的第三条用例：同一时刻 A / B 两个租户各建一行、都成功）。
        # ⚠️ **代价（如实登记）**：「通用」那一档完全不受本条约束（`general` 行想建多少条都行）
        # ⇒ 将来若还要「第二种锁定用途」，那是**再写一条**约束，⛔ 不是把这条的 `condition` 放宽。
        constraints = [
            models.UniqueConstraint(
                fields=["kind"],
                condition=models.Q(kind=ApiAppKind.WECOM_GROUP_QR),
                name="uniq_apiapp_wecom_qr",
            ),
            models.CheckConstraint(
                condition=(
                    (~models.Q(kind=ApiAppKind.BUSINESS_INTEGRATION)
                     | models.Q(state__in=[BUSINESS_INTEGRATION_ENABLED, ApiAppState.DISABLED]))
                    & (models.Q(kind=ApiAppKind.BUSINESS_INTEGRATION)
                       | ~models.Q(state=BUSINESS_INTEGRATION_ENABLED))
                ),
                name="apiapp_business_sdk_state",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name}({self.app_key})"


class OpenApiDelivery(models.Model):
    """主动回调推送的 **outbox + 台账**（**一张表两种角色**，v2 设计文档 §7.4 / §4-V5）。

    ## 为什么不拆两张

    outbox 与台账的生命周期同形（建 → 投递 → 落状态），拆开就会造出一份必须同步的重复事实。
    ⚠️ **这个形状是照抄既有先例**、⛔ 不是本线发明的：`apps/business/models.py` 的 `PeerSyncEvent`
    （它的 docstring 原文就写「outbox + 台账（**一张表**）」）。

    ## 为什么表里**没有**签名，⛔ 也别顺手加一个 `sign` 列

    `payload` 是**已定稿、待投递**的报文主体，里面**不含签名**。签名在**投递那一刻**用**当时的**
    `ApiApp.app_secret` 现算（§7.3 的 `sign = HMAC-SHA256(app_secret, …)`）。
    **理由 = 密钥可以轮换**：把签名存进 outbox 就等于把签名**钉死在写行那一刻的密钥**上
    ⇒ 轮换密钥之后，那些尚未投递的行会带着一把**已作废的密钥**算出的签名送出去，
    对端一律验不过，而**失败原因看起来像"对方不认我们的签名"**（极难排查）。
    现算则天然跟着密钥走 ⇒ 轮换后待投递的行用新密钥重新签，语义正确。

    ## 状态与重试

    `status` + `attempts` + `next_attempt_at` 三个列驱动重试（§7.4 的退避表），
    ⛔ 不用 `sleep` 阻塞循环。超限标记 `failed` 但**保留行**（页面可见、可手工重投）。

    ## ⛔ 本表**没有**的东西（别顺手加回来）

    - ⛔ 不加 `tenant_id`：本 app 在 `TENANT_APPS` 里 ⇒ 每个租户 schema 各一份（与 `apps.business`
      同口径，`ApiApp` 的类 docstring 也写着同一条）。
    - ⛔ 不加"租户标识"列：租户上下文只来自认证态 / schema 本身（`AGENTS.md` §4 的红线）。
    """

    class Event(models.TextChoices):
        """三类推送事件（§7.2，⛔ 不含对标 `action=40/50` 那两类，§3.2-3）。"""
        ORDER_STATUS_CHANGED = "order.status_changed", "订单状态变化"
        ORDER_DELETED = "order.deleted", "订单删除"
        ORDER_IMAGE_ADDED = "order.image_added", "订单新增进度图"
        INTEGRATION_CHANGE = "integration.change", "业务应用变更"

    class Status(models.TextChoices):
        PENDING = "pending", "待投递"
        DONE = "done", "已投递"
        FAILED = "failed", "投递失败"

    app = models.ForeignKey(
        "openapi.ApiApp",
        verbose_name="开放平台应用",
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
    # 下一次可投递时刻（§7.4 的退避表驱动它）：为空 = 立刻可投。
    next_attempt_at = models.DateTimeField("下次可投递时刻", null=True, blank=True)
    # 对方回执的 HTTP 状态码：3xx / 5xx 都是失败，但**码要看得到**（排障靠它）。
    response_status = models.PositiveSmallIntegerField(
        "回执状态码", null=True, blank=True
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    processed_at = models.DateTimeField("处理时刻", null=True, blank=True)

    class Meta:
        verbose_name = "开放平台推送台账"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        # ⚠️ 索引的取舍理由（照 `PeerSyncEvent` 的三条索引**按本表的取数路径**重新取舍，
        # ⛔ 不是照抄三条）：
        # ① `(status, next_attempt_at)` **必须**：常驻投递进程每 10 秒取一次「待发 / 到点重试」
        # 的行，这是它**唯一**的取数条件，没有它就是每次全表扫；
        # ② `app`：管理页的「最近推送记录」按应用取最近 N 条（`app_id` 排序取数）
        # ⇒ 单列索引就是它的取数面（`source_order_no` / `event` 没有这类取数路径，⛔ 不建）。
        # ⛔ 不照抄 `PeerSyncEvent` 的 `binding_role/binding_id/idempotency_key` 那条：
        # 本表的"同一件事"由 *(app, event, order_no)* 表达，而它**不是**唯一键
        # （同一订单的状态可以变多次）⇒ 建唯一索引会**丢事件**，建普通索引又没有取数路径。
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="openapi_deliv_due_idx"),
            models.Index(fields=["app"], name="openapi_deliv_app_idx"),
            # ⚠️ ⭐ T8 追加：**单列** `created_at` 索引，专供机会式清理那条
            # `DELETE … WHERE status='done' AND created_at < <明确算出的时刻> LIMIT 500`
            # （`apps/openapi/dispatch.py::prune_done_deliveries`）。⛔ 它不是"顺手加的"：
            # 上面那条 `(status, next_attempt_at)` 的**第二列**是 `next_attempt_at`
            # （终态行那一列是 NULL，`created_at` 用不上它）、`app` 那条前缀是 `app_id`
            # ⇒ 那条清理的取数面只有 `created_at` 一列，没有本索引就是每次全表扫
            # （本表**只增不减**，会一直长）。
            # ⚠️ 与 `OpenApiIdempotency` / `OpenApiRateBucket` 的两条同族索引**同一个理由**。
            models.Index(fields=["created_at"], name="openapi_deliv_created_idx"),
        ]

    def __str__(self) -> str:
        return f"开放平台推送台账:{self.event}/{self.order_no}/{self.status}"


class OpenApiIdempotency(models.Model):
    """写端点的**幂等键台账**（v2 设计文档 §6.3）。

    写端点必须带 `Idempotency-Key`：命中同一键 ⇒ **回放**上一次的响应快照（⛔ 不重复执行副作用）；
    同一个键配**不同**请求体 ⇒ 业务码 `1000`（⛔ 不静默返回上一个响应）。

    ⚠️ `request_hash` 存的是**请求体的 sha256 hex**（64 字符）——用它分辨"同键同体（回放）"
    与"同键异体（拒绝）"，⛔ 我们**不存原始请求体**（那会把对方报文里的内容原样留库，
    而留库的东西会进备份 / 排障会话）。

    ⛔ 本表**没有** `tenant_id`（同 app 其它表的口径）。
    """

    app = models.ForeignKey(
        "openapi.ApiApp",
        verbose_name="开放平台应用",
        on_delete=models.CASCADE,
        # ⚠️ `related_name="+"`：幂等台账**没有反向消费方**（从应用侧数"它有多少幂等键"没人要）
        # ⇒ 建一个反向访问器只会变成一个没人维护、却被后来者当接口用的入口。
        related_name="+",
    )
    # 调用方给的键（契约长度 8–128，见 §6.3；列宽按上限 191 留余量以兼容既有口径）。
    key = models.CharField("幂等键", max_length=191)
    method = models.CharField("请求方法", max_length=8)
    path = models.CharField("请求路径", max_length=191)
    # 请求体 sha256 的 hex（64 字符）。
    request_hash = models.CharField("请求体摘要", max_length=64)
    # 上一次的响应快照（回放时逐字返回它）。⚠️ 快照里**不许**有 `app_secret` 之外的凭据类字段，
    # ⛔ 尤其不许把 `app_secret` 存进来（D-0201 不放松的那一格：密钥不进任何留痕）。
    response_snapshot = models.JSONField("响应快照", default=dict, blank=True)
    status_code = models.PositiveSmallIntegerField("响应状态码")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "开放平台幂等台账"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        # ⚠️ 唯一性**只由这条具名约束**保证（⛔ 不给 `key` 加 `unique=True`：
        # 那会变成第二棵唯一索引，而真正的判据是「**同一个应用内**唯一」——
        # 不同应用撞同一个键是**合法**的，加 `unique=True` 会把它们一起拒掉）。
        # 写法照 `apps.business.models.OrderRefund` 的 `uniq_order_refund_request_id` 先例。
        constraints = [
            models.UniqueConstraint(
                fields=["app", "key"], name="uniq_openapi_idem_app_key"
            ),
        ]
        # ⚠️ ⭐ T5 追加：**单列** `created_at` 索引，专供机会式清理那条
        # `DELETE … WHERE created_at < <明确算出的时刻> LIMIT 500`
        # （`apps/openapi/idempotency.py::_prune`）。⛔ 它不是"顺手加的"：
        # 上面那条唯一约束的前缀是 `app`，而那条 `DELETE` 的取数面只有 `created_at`
        # ⇒ 用不上它，没有这条索引就是每次全表扫（本表**只增不减**，会一直长）。
        indexes = [
            models.Index(fields=["created_at"], name="openapi_idem_created_idx"),
        ]

    def __str__(self) -> str:
        return f"开放平台幂等台账:{self.app_id}/{self.method} {self.path}/{self.key}"


class OpenApiRateBucket(models.Model):
    """限流计数桶（**替掉进程内 `LocMemCache`**，v2 设计文档 §10 / §4-V4）。

    ## ⚠️ 为什么两个维度住**同一张表**（这一条必须写清楚，否则会被读成"合并了两个桶"）

    本表把**应用档**与**来源 IP 档**两个计数维度放进**同一张表**，但用 `scope` 分成**两种不同的行**
    ——`scope="app"` 的行装 `app_key`、`scope="ip"` 的行装来源 IP。

    ⛔ **这不等于「把两个桶的计数合并」**：旧 spec 明令不许合并的是**计数与阈值**
    （`app` 档的额度与 `ip` 档的额度是**两个独立阈值**，各自判各自的额度），
    ⛔ **不是**"不许住同一张表"。表的形状与阈值的语义是两件事：
    同一张表只是共享了列结构与一条 UPSERT 语句；`(scope, ident)` 不同 ⇒ 行不同 ⇒ 计数天然隔离。

    第三个 `scope` 是 `ip_token`（`IP_TOKEN`），装取令牌端点的兜底额度。
    ⚠️ 它与 `ip` **不是两个"维度"**（两者都是"按来源 IP"），而是**同一个维度的两种用途**
    —— 之所以不给它们同一行，是因为同一张表里按来源 IP 有**两个独立阈值**
    （失败闸 20 次 / 60 秒，兜底额度 600 次 / 60 秒且**含成功与失败**）：
    共用一行会让两个阈值互相污染（兜底额度每来一次调用就加一，失败闸读到的是**总调用数**
    ⇒ 第 20 次**成功**调用就会被当成"猜了 20 次凭据"挡掉）。
    ⚠️ **为什么用一个新的 `scope` 值而不是在 `ident` 上拼前缀**（这是有意的取舍）：
    `ident` 的语义是「由 `scope` 决定的一维取值」（纯 `app_key` 或纯 IP）；拼前缀会把它变成
    **两段拼接的复合键**，而复合键的"拼"与"解"规则迟早会在第二个地方被复制
    （`AGENTS.md` §5-E）。多一个 `scope` 成员是**显式**的：读代码的人一眼看得出这一行是另一档。
    ⚠️ 代价（如实登记）：`scope` 的取值从 2 个变成 3 个 ⇒ **多一次 `AlterField`**（迁移 `0004`，
    与那条 `window_start` 索引在**同一个**迁移文件里）；`choices` 只在应用层校验，
    库里那一列仍是 `varchar(8)`（`ip_token` 是 8 字符，**恰好**占满 ⇒ 不要给它起更长的名字）。

    ⚠️ **本任务（T1）不实现阈值逻辑**（那是 T3）：这里只有列与唯一键。

    ## 为什么换 DB

    生产是 `gunicorn --workers 2`（实测）⇒ `LocMemCache` 的桶**每个 worker 各一份**，
    实际额度 ≈ 设定值 × worker 数（`throttling.py` 的 docstring 自己写着这条）。
    固定窗口口径不变（跨边界最坏两倍放行），一次 `INSERT … ON CONFLICT DO UPDATE` 完成「判 + 记」。

    ⛔ 本表**没有** `tenant_id`（同 app 其它表的口径）。

    ## ⭐ T3 追加：计数入口、清理、以及**一处** `ident` 前缀

    - 唯一的计数出口是 `apps/openapi/rate_limit.py::consume()`（一次原子 UPSERT「记 + 判」），
      只读那一半是同一个模块的 `current_count()`；⛔ 别在别处再写一份读-改-写。
    - 本表**没有 TTL**（DB 行不会自己过期）⇒ 过期行由 `consume()` 做**机会式清理**
      （「本窗口第一次命中」时删掉早于「当前窗口 − 3 个窗口」的行）。⚠️ 那条 `DELETE` 的取数面
      只有 `window_start` 一列，所以本表**另加一条 `window_start` 单列索引**（迁移 `0004`）
      —— ⛔ 唯一索引 `(scope, ident, window_start)` 的前缀是 `scope`，替不了它。
    - ⚠️ **`ident` 的一处例外**（append-only，原口径「`ip` 档存来源 IP」仍适用于其余各处）：
      取令牌端点的**兜底额度**那一行用 `token-quota:<来源 IP>`
      （`rate_limit.TOKEN_QUOTA_IDENT_PREFIX`）。理由：同一张表里按来源 IP 有**两个独立阈值**
      （失败闸 20/60 与兜底额度 600/60），而 `scope` 只有两个取值 ⇒ 两行必须靠 `ident` 分开，
      否则两个阈值会互相污染（详细推导见 `rate_limit.py` 的常量注释）。
      ⛔ **失败闸那一行不带前缀**（纯来源 IP，与签名 / Bearer 两条路共用同一行）。
    """

    class Scope(models.TextChoices):
        APP = "app", "按应用"
        IP = "ip", "按来源 IP"
        #: ⭐ T3 新增（v2 §13.11-1 ②）：**取令牌端点的兜底额度**那一行。
        #: 与 `ip` 同维（都按来源 IP），差的是用途和阈值（见类 docstring）。
        #: ⚠️ 8 个字符**恰好**占满 `max_length=8`（⛔ 别加长这个名字）。
        IP_TOKEN = "ip_token", "按来源 IP（取令牌端点）"
        #: ⚠️ 失败闸那一行仍用 `IP`（**纯来源 IP 的 `ident`**，与签名 / Bearer 两条路共用同一行）。

    scope = models.CharField("维度", max_length=8, choices=Scope.choices)
    # ⚠️ `ident` 的语义**由 `scope` 决定**：`app` 档存 `app_key`（明文，按设计不是机密），
    # `ip` 档存来源 IP（`IPv4` / `IPv6` 文本形态）。⛔ 不要拿它当"通用标识"跨档比较。
    ident = models.CharField("维度取值", max_length=64)
    #: 固定窗口的左端（口径沿用现实现：**固定窗口**，⛔ 不是滑动窗口）。
    window_start = models.DateTimeField("窗口开始")
    count = models.PositiveIntegerField("计数", default=0)

    class Meta:
        verbose_name = "开放平台限流桶"
        verbose_name_plural = verbose_name
        ordering = ["-window_start", "-id"]
        # ⚠️ 唯一性**只由这条具名约束**保证，⛔ 不给三列各自加 `unique=True`。
        # ⚠️ `scope` **必须**进这个唯一键：它是"两种行互不干扰"的载体
        # （少了它，同一个 `ident` 文本在两个档之间会互相撞，而两档额度是独立的）。
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "ident", "window_start"],
                name="uniq_openapi_rate_bucket",
            ),
        ]
        # ⚠️ T3 追加：**单列** `window_start` 索引，专供机会式清理那条
        # `DELETE … WHERE window_start < <时刻>`。⛔ 它不是"顺手加的"：
        # 上面那条唯一索引的前缀是 `scope`，这个 `DELETE` 用不上它（见类 docstring）。
        indexes = [
            models.Index(fields=["window_start"], name="openapi_rate_win_idx"),
        ]

    def __str__(self) -> str:
        return f"开放平台限流桶:{self.scope}/{self.ident}/{self.count}"


class IntegrationTakerBinding(models.Model):
    app = models.ForeignKey(ApiApp, on_delete=models.CASCADE, related_name="taker_bindings")
    external_user_id = models.CharField(max_length=64)
    taker_app = models.OneToOneField(
        "takerapi.TakerApp", on_delete=models.CASCADE, related_name="integration_binding",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("app", "external_user_id"), name="unique_integration_taker_subject"),
        ]
