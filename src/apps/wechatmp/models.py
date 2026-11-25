"""微信公众号（自开发，D-0268）的租户侧两张表：**租户配置单行** + **用户绑定行**。

    ## 落位（⛔ 别与平台表混）

    本模块两张表住在**每个租户自己的 schema**（`TENANT_APPS`，见 `apps.py` 的 docstring）；
    没有平台侧对应物（appsecret 是**租户自己的**公众号凭据，不是平台托管的）。

    ## 对 malailai 四反例的规避（D-0268 逐条）

    1. 不存明文第三方密码 —— 本表只存**租户自己公众号**的 `appsecret`（本来就是要配进
    微信公众平台的凭据，无「第三方账号密码」这种东西）；响应里一律掩码（`views.py`）。
    2. openid 不裸当身份 —— `WechatMpBinding.openid` 只在**已认证会话**里经 OAuth +
    state 校验后落库（`bind_views.py`），端点不以 openid 作唯一凭据（malailai 的
    `auto-login` 用裸 openid 换账号密码，是反例）。
    3. token 库表持久化 —— `access_token` + `token_expires_at` 落在本表（行锁刷新，
    `services.py`），⛔ 不用进程内缓存（gunicorn 双 worker 各自缓存会互踢烧配额）。
    4. 模板 id 配置化 —— `template_id` 在设置页配，⛔ 不写死在代码里。
"""
from django.conf import settings
from django.db import models


class EncryptionMode(models.TextChoices):
    """公众号服务器配置的三种消息加密模式（微信公众平台「服务器配置」页的同名三项）。

    - `plain` 明文：body 即 XML，URL 上的 `signature` 只覆盖 query 三参；
    - `compatible` 兼容：body 里既有明文也有 `Encrypt`（微信会发**加密**包，取包内明文）；
    - `safe` 安全：body 只有 `Encrypt`，必须先验 `msg_signature` 再 AES 解密。

    ⚠️ `compatible` 模式下微信实际推送的是**加密包**（官方文档「兼容模式：消息数据包
    同时存在明文与密文，**优先取加密包**」）⇒ 本仓照 safe 的同一条路处理它
    （先验 `msg_signature` 再解密），回包一律空串（无需加密应答）。
"""

    PLAIN = "plain", "明文模式"
    COMPATIBLE = "compatible", "兼容模式"
    SAFE = "safe", "安全模式"


class WechatMpConfig(models.Model):
    """本租户的公众号服务器配置（**每租户一行**）。

    **单行唯一靠 DB 约束**（计划 S7）：照 `RealNameSetting` 先例 —— `tenant_schema`
    列级 `unique=True`。没有约束时两个并发的首保存会各建一行，此后「读哪一行」看运气
    （回调读到的 token 与设置页保存的可能不是同一行）。⛔ 别把它删掉"简化一下"。

    ⚠️ `enabled` 默认 **False**（fail-closed，与 `RealNameSetting.enabled` 同一纪律）：
    配置没配好之前回调端点对微信侧回 403（计划 S8），推送与菜单动作也全部拒绝。
"""

    #: 租户标识 = 租户 schema 名。⚠️ **只来自 `connection.schema_name`**，
    #: ⛔ 绝不接受请求参数传入的租户标识（`AGENTS.md` §4 铁律）。
    tenant_schema = models.CharField("租户标识", max_length=64, unique=True)

    #: 公众号 AppID（微信公众平台 → 设置与开发 → 帐号详情）。租户内唯一（一行配置
    #: 自然只有一个 appid；DB 级约束防的是「两行配置各配一个 appid」的并发双行）。
    appid = models.CharField("AppID", max_length=64, unique=True)
    #: 公众号 AppSecret。⚠️ **响应里一律掩码**（`views.py` 的载荷构造），留空 = 保持不变
    #: （计划 S4）。
    appsecret = models.CharField("AppSecret", max_length=128, blank=True, default="")
    #: 服务器配置的 Token（验签用，自己起的任意串）。
    token = models.CharField("服务器 Token", max_length=128, blank=True, default="")
    #: 服务器配置的 EncodingAESKey（43 位字符；空 = 没配）。⚠️ 留空 = 保持不变（计划 S4）。
    aes_key = models.CharField("EncodingAESKey", max_length=64, blank=True, default="")
    #: 消息加密模式（服务器配置页同名三项；默认明文）。
    mode = models.CharField(
        "消息加密模式",
        max_length=16,
        choices=EncryptionMode.choices,
        default=EncryptionMode.PLAIN,
    )
    #: 总开关：回调 / 菜单动作 / 推送全部看它。**默认关**（fail-closed）。
    enabled = models.BooleanField("启用公众号集成", default=False)
    #: 订单推送模板 id（公众平台 → 模板消息里申请的那条）。空 = 推送不发（计划 T5）。
    template_id = models.CharField("订单推送模板ID", max_length=64, blank=True, default="")

    # ── access_token 持久化（D-0268 反例 ③：库表持久化，⛔ 不用进程内缓存）────────
    #: 由 `services.acquire_access_token()` 行锁刷新后落库；空 = 还没取到过。
    access_token = models.CharField("access_token", max_length=512, blank=True, default="")
    #: 上述 token 的过期时刻（已含安全余量，见 `services.TOKEN_EXPIRY_MARGIN`）。
    #: ⚠️ 判据用 `is not None` / `<= now`（E-0021：None = 从没取过，不是「已过期」的同义词）。
    token_expires_at = models.DateTimeField("token过期时刻", null=True, blank=True)

    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "微信公众号配置"
        verbose_name_plural = "微信公众号配置"

    def __str__(self) -> str:
        state = "启用" if self.enabled else "未启用"
        # ⛔ `__str__` 会出现在日志 / 报错文本里：appsecret / token 一个字都不进来。
        return f"{self.appid}:{state}"

    def is_ready(self) -> bool:
        """回调与菜单动作的**最低配置判据**：启用 + appid/appsecret/token 三件都非空。

    ⚠️ 用 `is not None` 之外的常规真值判断是安全的：这些列全部 `blank=True,
    default=""`（永远是非空字符串），不存在 `None` 形态（E-0021 的例外：读层已归一）。
"""
        return bool(self.enabled and self.appid and self.appsecret and self.token)


class WechatMpBinding(models.Model):
    """一个团团通账号 ↔ 一个公众号 openid 的绑定（v1 一人一号，重绑 = 换绑覆盖）。

    ⚠️ `user` 唯一（计划 T1）：同一账号只绑一个 openid；换绑 = 覆盖同一行
    （`bind_views.py` 落库时 `update_or_create`），并写操作日志。
    ⚠️ `(appid, openid)` 唯一：同一个微信号的 openid 只能挂在**一个**团团通账号上
    （防止「一个微信号绑两个账号、推送发两份、登录认不出人」）。要换账号先解绑。
"""

    #: 一人一号，数据库级唯一。没有这道约束时并发换绑能落两行，随后按 user 更新会抛多对象。
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="wechatmp_bindings",
        verbose_name="系统用户",
    )
    #: 公众号 AppID（来自配置行；冗余存一份让 `(appid, openid)` 约束可建）。
    appid = models.CharField("AppID", max_length=64)
    #: 公众号对本租户公众号的用户唯一标识（OAuth `snsapi_base` 换来的那个）。
    openid = models.CharField("OpenID", max_length=64)
    #: 开放平台_unionid（租户公众号没绑开放平台时为空；v1 只存不用）。
    unionid = models.CharField("UnionID", max_length=64, blank=True, default="")
    #: 是否仍在关注（回调 `subscribe` / `unsubscribe` 事件翻这一位；推送只发给 True 的）。
    #: ⚠️ 默认 **True** 是**推断**（依据：菜单只有关注者可见，「绑定账号」入口只能从
    #: 菜单进 ⇒ 能走到绑定这步的人必然在关注），未与业务方逐字确认；
    #: 回调事件到达时以事件为准覆盖。
    subscribed = models.BooleanField("仍在关注", default=True)
    #: 绑定时刻（换绑**不**重置 —— 首次绑定时刻保留；「谁在什么时候换绑的」看操作日志）。
    bound_at = models.DateTimeField("绑定时间", auto_now_add=True)

    class Meta:
        verbose_name = "公众号绑定"
        verbose_name_plural = "公众号绑定"
        constraints = [
            models.UniqueConstraint(
                fields=["appid", "openid"],
                name="uniq_wechatmp_binding_appid_openid",
            ),
        ]

    def __str__(self) -> str:
        # ⛔ openid 虽不是密钥，也不进日志友好文本：截短留对账线索即可。
        return f"{self.user_id}:{self.appid}:{self.openid[:8]}"
