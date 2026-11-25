"""企微配置、回调、客户群与群码均按租户隔离。"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class ExternalPayState(models.Model):
    """每个凭据身份独立记录扫描位置，避免切换企业后串账。"""
    identity_revision = models.PositiveBigIntegerField(unique=True)
    corp_id = models.CharField(max_length=64)
    watermark = models.DateTimeField(null=True)
    window_start = models.DateTimeField(null=True)
    window_end = models.DateTimeField(null=True)
    cursor = models.TextField(blank=True, default="")
    cursor_seen = models.JSONField(default=list)
    last_run_at = models.DateTimeField(null=True)
    last_success_at = models.DateTimeField(null=True)
    last_error = models.CharField(max_length=300, blank=True, default="")


class ExternalPayBill(models.Model):
    """账单身份和匹配结果，仅保存同步所需字段。"""
    state = models.ForeignKey(ExternalPayState, on_delete=models.PROTECT)
    bill_type = models.PositiveSmallIntegerField()
    transaction_id = models.CharField(max_length=128)
    mch_id = models.CharField(max_length=128)
    out_trade_no = models.CharField(max_length=128)
    out_refund_no = models.CharField(max_length=128, blank=True, default="")
    payee_userid = models.CharField(max_length=128, blank=True, default="")
    mismatch_active = models.BooleanField(default=False)
    mismatch_notification_pending = models.BooleanField(default=False)
    mismatch_notice_corp_id = models.CharField(max_length=64, blank=True, default='')
    mismatch_notice_agent_id = models.IntegerField(default=0)
    mismatch_notice_enabled_at = models.DateTimeField(null=True, blank=True)
    total_fee = models.BigIntegerField()
    total_refund_fee = models.BigIntegerField(null=True)
    pay_time = models.DateTimeField()
    trade_state = models.IntegerField(null=True)
    order = models.ForeignKey("business.Order", null=True, on_delete=models.PROTECT)
    issue = models.CharField(max_length=300, blank=True, default="")
    next_retry_at = models.DateTimeField(null=True, db_index=True)
    matched_at = models.DateTimeField(null=True)
    search_end = models.DateTimeField(null=True)
    search_start = models.DateTimeField(null=True)
    search_cursor = models.TextField(blank=True, default="")
    search_seen = models.JSONField(default=list)
    search_candidate = models.JSONField(null=True)
    refund_origin = models.BooleanField(default=False)
    conflicted = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["state", "bill_type", "transaction_id", "mch_id"], name="wecom_external_bill_identity")]
        indexes = [models.Index(fields=["state", "mch_id", "out_trade_no", "bill_type"], name="wecom_external_bill_link")]


class ExternalPayRecheck(models.Model):
    order = models.ForeignKey("business.Order", on_delete=models.CASCADE)
    pending = models.BooleanField(default=True, db_index=True)


class ExternalPayInvalidBill(models.Model):
    state = models.ForeignKey(ExternalPayState, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    payload = models.JSONField()
    issue = models.CharField(max_length=300)
    resolved = models.BooleanField(default=False)
    last_retry_at = models.DateTimeField(null=True)
    search_start = models.DateTimeField(null=True)
    search_end = models.DateTimeField(null=True)
    search_cursor = models.TextField(blank=True, default="")
    search_seen = models.JSONField(default=list)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["state", "fingerprint"], name="wecom_external_invalid_identity")]


class WecomAppConfig(models.Model):
    """本租户的企微自建应用配置（**每租户一行**）。

    **单行唯一靠 DB 约束**：照 `WechatMpConfig.tenant_schema` 的列级 `unique=True`
    （没有约束时两个并发的首保存会各建一行，此后「读哪一行」看运气）。

    ⚠️ `enabled` 默认 **False**（fail-closed）：回调端点对企微侧回 403，
    代理端点回「未配置」业务码。
    """

    #: 租户标识 = 租户 schema 名。⚠️ **只来自 `connection.schema_name`**，
    #: ⛔ 绝不接受请求参数传入的租户标识（`AGENTS.md` §4 铁律）。
    tenant_schema = models.CharField("租户标识", max_length=64, unique=True)

    #: 企业 ID（企微后台 → 我的企业 → 企业信息）。留空 = 未配置。
    corp_id = models.CharField("CorpID", max_length=64, blank=True, default="")
    #: 应用 agentId（企微后台 → 应用管理 → 该应用的 AgentId）；0 = 未配置。
    agent_id = models.IntegerField("AgentId", default=0)
    identity_revision = models.PositiveBigIntegerField(default=0)
    #: 应用 secret。⚠️ **只写不读**：响应只回掩码，留空 = 保持不变（`views.py`）。
    secret = models.CharField("应用 Secret", max_length=256, blank=True, default="")
    #: 回调配置的 Token（企微后台「接收消息」页自起的任意串）。
    callback_token = models.CharField("回调 Token", max_length=64, blank=True, default="")
    #: 回调配置的 EncodingAESKey（43 位字符；企微回调固定加密 ⇒ 配置必填）。
    encoding_aes_key = models.CharField("EncodingAESKey", max_length=43, blank=True, default="")
    #: 客户联系 secret（**选填**：留空 = 直接用应用 secret 调客户联系接口，前提是本应用
    #: 已在企微后台「客户联系 → 可调用接口的应用」授权）。⚠️ 同样**只写不读**。
    contacts_secret = models.CharField("客户联系 Secret", max_length=256, blank=True, default="")
    #: 总开关：回调 / 代理端点全部看它。**默认关**（fail-closed）。
    enabled = models.BooleanField("启用企微集成", default=False)
    group_sync_enabled = models.BooleanField(default=True)
    group_qr_enabled = models.BooleanField(default=True)
    external_pay_enabled = models.BooleanField(default=True)
    payment_mismatch_enabled = models.BooleanField(default=False)
    payment_mismatch_enabled_at = models.DateTimeField(null=True, blank=True)
    order_status_enabled = models.BooleanField(default=False)
    order_status_enabled_at = models.DateTimeField(null=True, blank=True)

    # ── 应用 token 缓存（库表持久化，照 `WechatMpConfig` 口径）────────────────
    #: 由 `sdk.DBSessionStorage` 落库；空 = 还没取到过。
    access_token = models.CharField("access_token", max_length=512, blank=True, default="")
    #: ⚠️ 判据用 `is not None`（E-0021：None = 从没取过，不是「已过期」的同义词）。
    access_token_expires_at = models.DateTimeField("token过期时刻", null=True, blank=True)

    # ── 客户联系 token（**另一个** token，独立 secret ⇒ 分列存，⛔ 不与上面共用）──
    contacts_token = models.CharField("contacts_token", max_length=512, blank=True, default="")
    contacts_token_expires_at = models.DateTimeField(
        "contacts_token过期时刻", null=True, blank=True
    )

    #: 最近一次回调 GET 验证成功的时刻（自检页的「回调通了没有」读数）。
    verified_at = models.DateTimeField("回调验证成功时刻", null=True, blank=True)

    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "企微自建应用配置"
        verbose_name_plural = "企微自建应用配置"

    def __str__(self) -> str:
        state = "启用" if self.enabled else "未启用"
        # ⛔ `__str__` 会出现在日志 / 报错文本里：secret / token 一个字都不进来。
        return f"{self.corp_id}:{state}"

    def is_ready(self) -> bool:
        """回调 / 代理端点的**最低配置判据**：启用 + corp_id/secret/agent_id 三件齐。

        这些列全部 `blank=True, default=""` / `default=0`（不存在 None 形态），
        常规真值判断安全（E-0021 例外：读层已归一）。
        """
        return bool(self.enabled and self.corp_id and self.secret and self.agent_id)

    def contacts_ready(self) -> bool:
        """客户联系面的判据：启用 + corp_id + (contacts_secret 或应用 secret)。
        contacts_secret 选填（空 = sdk 层回退应用 secret，见 `sdk.get_contacts_client`）。
        """
        return bool(self.enabled and self.corp_id and (self.contacts_secret or self.secret))


class WecomNotification(models.Model):
    event_key = models.CharField(max_length=32, null=True, blank=True, unique=True)
    identity_revision = models.PositiveBigIntegerField()
    corp_id = models.CharField(max_length=64, blank=True)
    agent_id = models.IntegerField(default=0)
    enabled_at = models.DateTimeField(null=True, blank=True)
    kind = models.CharField(max_length=32)
    order_no = models.CharField(max_length=128, blank=True)
    detail = models.TextField()
    lookup_name = models.CharField(max_length=128, blank=True)
    bill = models.ForeignKey(ExternalPayBill, null=True, blank=True, on_delete=models.PROTECT)
    lookup_cursor = models.TextField(blank=True, default='')
    lookup_seen = models.JSONField(default=list)
    lookup_found = models.JSONField(default=list)
    recipients = models.JSONField(default=dict)
    recipient_attempts = models.JSONField(default=dict)
    resolved = models.BooleanField(default=False)
    status = models.CharField(max_length=16, default="pending", db_index=True)
    reason = models.CharField(max_length=300, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)


class WecomInboundMessage(models.Model):
    """企微回调消息台账（初版只收 + 留痕，业务触发二期）。

    ⚠️ **唯一写入点 = 回调视图**（`views.WecomCallbackView.post`；计划钉死——
    未来加第二个写点必须回查 `msg_id` 去重的覆盖面）。保留策略挂账给业务方，初版不删。
    """

    #: 企微消息 id（**事件类消息没有 MsgId** ⇒ 可空）。非空时**库级唯一**
    #: （企微回调重试最多 3 次，这道 partial unique 挡重试行）；事件类不去重，如实允许重复行。
    msg_id = models.CharField("MsgId", max_length=64, null=True, blank=True)
    #: 发送方 userid（通讯录成员）；系统消息可能没有 ⇒ 空串。
    from_user_name = models.CharField("发送方", max_length=64, blank=True, default="")
    #: `text` / `image` / `event` …（企微 `MsgType` 原值）。
    msg_type = models.CharField("消息类型", max_length=32, blank=True, default="")
    #: 事件名（仅 `msg_type=event` 有；其余为 None——「没有事件」与「空事件名」不要混）。
    event = models.CharField("事件名", max_length=32, null=True, blank=True)
    #: 推送到达的应用 agentId（透传存一份；无该字段的包为 None）。
    agent_id = models.IntegerField("AgentId", null=True, blank=True)
    #: 文本消息正文（非文本类为空串；二进制内容不落库）。
    content = models.TextField("内容", blank=True, default="")
    #: 解密后的原文 XML（排障用；密钥与 token 不在里面）。
    raw_xml = models.TextField("解密后原文", blank=True, default="")
    received_at = models.DateTimeField("接收时刻", auto_now_add=True)

    class Meta:
        verbose_name = "企微回调消息"
        verbose_name_plural = "企微回调消息"
        constraints = [
            models.UniqueConstraint(
                fields=["msg_id"],
                condition=Q(msg_id__isnull=False),
                name="uniq_wecom_inbound_msg_id",
            ),
        ]

    def __str__(self) -> str:
        # ⛔ 台账正文（聊天内容）不进日志友好文本：留对账线索即可。
        return f"{self.msg_type}:{(self.msg_id or 'event')}@{self.received_at:%Y-%m-%d %H:%M}"


class WecomGroupChat(models.Model):
    """租户客户群快照及持久刷新任务。"""

    chat_id = models.CharField(max_length=256, unique=True)
    identity_revision = models.PositiveBigIntegerField(default=0, db_index=True)
    name = models.CharField(max_length=512, blank=True, default="")
    details = models.JSONField(default=dict)
    dismissed = models.BooleanField(default=False)
    pending_dismissal = models.BooleanField(default=False)
    event_time = models.BigIntegerField(default=0)
    revision = models.PositiveBigIntegerField(default=0)
    pending = models.BooleanField(default=False, db_index=True)
    confirming = models.BooleanField(default=False)
    retry_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class WecomGroupQr(models.Model):
    """每个群的进群配置及跨进程操作状态。"""

    group = models.OneToOneField(WecomGroupChat, on_delete=models.PROTECT, related_name="qr_config")
    config_id = models.CharField(max_length=256, blank=True, default="")
    corp_id = models.CharField(max_length=64, blank=True, default="")
    agent_id = models.IntegerField(default=0)
    state = models.CharField(max_length=16, default="idle")
    operation_id = models.CharField(max_length=32, blank=True, default="")
    operation_at = models.DateTimeField(null=True, blank=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)


class WecomArchiveState(models.Model):
    identity_revision = models.PositiveBigIntegerField()
    program_id = models.CharField(max_length=128)
    cursor = models.CharField(max_length=128, blank=True)
    next_poll_at = models.DateTimeField(null=True)
    retry_at = models.DateTimeField(null=True)
    lease_until = models.DateTimeField(null=True)
    lease_id = models.CharField(max_length=32, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["identity_revision", "program_id"], name="uniq_wecom_archive_state")
        ]


class WecomArchiveNotification(models.Model):
    identity_revision = models.PositiveBigIntegerField()
    program_id = models.CharField(max_length=128)
    notify_id = models.CharField(max_length=128)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["identity_revision", "program_id", "notify_id"], name="uniq_wecom_archive_notification"
            )
        ]


class WecomZoneNotification(models.Model):
    identity_revision = models.PositiveBigIntegerField()
    program_id = models.CharField(max_length=128)
    notify_id = models.CharField(max_length=128)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    retry_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    expired_at = models.DateTimeField(null=True)
    event_type = models.CharField(max_length=64, blank=True)
    details = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["identity_revision", "program_id", "notify_id"], name="uniq_wecom_zone_notice",
        )]


class WecomZoneKeywordState(models.Model):
    identity_revision = models.PositiveBigIntegerField()
    program_id = models.CharField(max_length=128)
    cursor = models.CharField(max_length=64, blank=True)
    needs_pull = models.BooleanField(default=False)
    retry_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["identity_revision", "program_id"], name="uniq_wecom_zone_keyword_state",
        )]


class WecomZoneKeywordHit(models.Model):
    identity_revision = models.PositiveBigIntegerField()
    program_id = models.CharField(max_length=128)
    msg_id = models.CharField(max_length=256)
    rule_id = models.CharField(max_length=128)
    has_hit_keyword = models.BooleanField()
    semantics = models.JSONField(default=list)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["identity_revision", "program_id", "msg_id", "rule_id"], name="uniq_wecom_zone_rule_hit",
        )]


class WecomRefundNotice(models.Model):
    hit = models.OneToOneField(WecomZoneKeywordHit, on_delete=models.CASCADE, related_name="refund_notice")
    order = models.ForeignKey("business.Order", on_delete=models.SET_NULL, null=True, related_name="+")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    delivered_recipient_ids = models.JSONField(default=list, db_default=[])
    chat_id = models.CharField(max_length=256)
    message_sent_at = models.DateTimeField()
    summary_job_id = models.CharField(max_length=256, blank=True)
    summary = models.TextField(blank=True)
    next_attempt_at = models.DateTimeField(db_index=True)
    failures = models.PositiveSmallIntegerField(default=0)
    problem = models.CharField(max_length=32, blank=True)
    closed_at = models.DateTimeField(null=True)
    sent_at = models.DateTimeField(null=True)


class WecomAftersalesSettings(models.Model):
    class Status(models.TextChoices):
        DISABLED = "disabled"
        PENDING = "pending"
        SYNCED = "synced"
        ERROR = "error"

    class OrderRecipient(models.TextChoices):
        CREATOR = "creator", "录单人"
        PUBLISHER = "publisher", "发单人"
        SALES = "sales", "销售客服"

    config = models.OneToOneField(WecomAppConfig, on_delete=models.CASCADE, related_name="+")
    enabled = models.BooleanField(default=False)
    keywords = models.JSONField(default=list)
    group_ids = models.JSONField(default=list)
    order_recipient = models.CharField(max_length=16, choices=OrderRecipient, default=OrderRecipient.SALES)
    fixed_recipient_ids = models.JSONField(default=list)
    status = models.CharField(max_length=16, choices=Status, default=Status.DISABLED)
    rule_name = models.CharField(max_length=20, blank=True)
    rule_id = models.CharField(max_length=128, blank=True)
    rule_revision = models.PositiveBigIntegerField(null=True)
    rule_program = models.CharField(max_length=128, blank=True)
    next_sync_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)


class WecomArchiveOrderGroup(models.Model):
    """An order/group association tied to the app and number that established it."""

    order = models.ForeignKey("business.Order", on_delete=models.CASCADE, related_name="+")
    config = models.ForeignKey(WecomAppConfig, on_delete=models.CASCADE, related_name="+")
    identity_revision = models.PositiveBigIntegerField()
    program_id = models.CharField(max_length=128)
    matched_order_no = models.CharField(max_length=128)
    chat_id = models.CharField(max_length=256)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "config", "identity_revision", "program_id", "matched_order_no", "chat_id"],
                name="uniq_wecom_archive_order_group",
            ),
        ]


class WecomArchiveMessage(models.Model):
    class SenderType(models.IntegerChoices):
        EMPLOYEE = 1, "员工"
        EXTERNAL = 2, "外部联系人"
        ROBOT = 3, "机器人"

    identity_revision = models.PositiveBigIntegerField()
    msg_id = models.CharField(max_length=256)
    chat_id = models.CharField(max_length=256)
    sent_at = models.DateTimeField(db_index=True)
    encrypted_secret_key = models.CharField(max_length=512)
    public_key_version = models.PositiveIntegerField()
    # 旧版同步进程在部署或回滚时不会写入此列。
    sender_id = models.CharField(max_length=256, blank=True, default="", db_default="")
    sender_type = models.PositiveSmallIntegerField(choices=SenderType.choices, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["identity_revision", "msg_id"], name="uniq_wecom_archive_message")
        ]
        indexes = [
            models.Index(fields=["identity_revision", "chat_id", "-sent_at", "-id"], name="wecom_archive_chat_time"),
        ]
