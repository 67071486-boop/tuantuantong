"""QQ 机器人的租户侧模型。五张表、全部每租户 schema 一份（本 app 只进 TENANT_APPS，见 `config/settings.py`）：
`QQBotBinding`=凭据与连接健康（每租户单例，`get_solo()`）；`QQBotGroup`=所在 QQ 群清单（官方事件投影 + 本地策略列）；`QQBotGroupPushConfig`=哪个群接哪些游戏的大厅推送（设置页读写）；`QQBotOutboundMessage`=待发/已发消息的发件箱（worker sender 抽它）；`QQBotJoinRequestLog`=入群申请与处置留痕（审批策略的唯一证据面）。命名与写法照 `apps/business/models.py`（verbose_name 全中文、TextChoices 内嵌、`__str__` 给人看）。
⚠️ `app_secret` 只写不读：只在「保存设置」与「取 token」两个时刻被用，任何读接口/日志/异常文案都不得带出（`qqbot_api._redact` 是兜底不是借口）；序列化层必须显式排除在响应之外。"""
from django.conf import settings
from django.db import models

from apps.business.models import Game, Order


class QQBotBinding(models.Model):
    """机器人凭据 + 连接健康（每租户单例：一行 = 一个机器人）。
    ⚠️ 「单例」由 `get_solo()` 的 `get_or_create(pk=1)` 语义保证（主键唯一 ⇒ 并发下也不会出第二行）；⛔ 不要绕过它手工建多行。"""

    class ConnectionState(models.TextChoices):
        OFFLINE = "offline", "未连接"
        CONNECTING = "connecting", "连接中"
        ONLINE = "online", "在线"

    app_id = models.CharField("AppID", max_length=64, unique=True)
    # ⛔ **只写不读**（模块 docstring 的纪律）：读面（序列化器）显式排除本列。
    app_secret = models.CharField(
        "AppSecret", max_length=256, help_text="只写不读：保存后任何接口都不回显。"
    )
    enabled = models.BooleanField("是否启用", default=False)
    #: True = 官方后台完成了认证 ⇒ Bot 维度限频走 60 条/分钟（False = 30）。
    #: 出处见 `apps/qqbot/ratelimit.py` 的常量注释；发版前要拿真机校准一次。
    auth_verified = models.BooleanField("已认证（限频 60/分钟）", default=False)
    bot_name = models.CharField("机器人名称", max_length=100, blank=True, default="")
    #: False = 已经因为无 Markdown 权限降级过（sender 自动降级并写回，见 worker）。
    markdown_ok = models.BooleanField("可用 Markdown", default=True)
    connection_state = models.CharField(
        "连接状态", max_length=16, blank=True, default=""
    )
    last_alive_at = models.DateTimeField("最近存活心跳", null=True, blank=True)
    last_event_at = models.DateTimeField("最近收到事件", null=True, blank=True)
    last_error = models.TextField("最近错误", blank=True, default="")
    menu_version = models.IntegerField("群菜单版本", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    @classmethod
    def get_solo(cls) -> "QQBotBinding":
        """取本租户唯一一行（没有就建空行，幂等）。⛔ 单例的唯一入口。"""
        return cls.objects.get_or_create(pk=1)[0]

    class Meta:
        verbose_name = "QQ 机器人凭据"
        verbose_name_plural = "QQ 机器人凭据"

    def __str__(self):
        return f"QQBot({self.app_id or '未配置'})"


class QQBotGroup(models.Model):
    """机器人所在的 QQ 群。「官方投影」列（群名/人数/收消息设置…）以官方事件与读接口为准回填；
    「本地」列（`remark`/`join_policy_*`）是租户自己的数据、官方没有。"""

    class Status(models.TextChoices):
        ACTIVE = "active", "在群"
        GONE = "gone", "已移出"

    class JoinPolicyMode(models.TextChoices):
        OFF = "off", "不自动处理（留人工）"
        APPROVE_ALL = "approve_all", "全部自动通过"
        DECLINE_ALL = "decline_all", "全部自动拒绝"
        WHITELIST = "whitelist", "白名单自动通过，其余拒绝"

    group_openid = models.CharField("群 openid", max_length=64, unique=True)
    remark = models.CharField("本地备注名", max_length=100, blank=True, default="")
    group_name = models.CharField("群名称", max_length=200, blank=True, default="")
    member_num = models.IntegerField("成员数", null=True, blank=True)
    member_role = models.CharField(
        "机器人群身份", max_length=16, blank=True, default=""
    )
    #: None = 还没收到过任何一方的事件（不知道）。
    allow_proactive_msg = models.BooleanField(
        "允许主动消息", null=True, blank=True
    )
    recv_msg_setting = models.CharField(
        "收消息设置", max_length=32, blank=True, default=""
    )
    joined_at = models.DateTimeField("机器人进群时间", null=True, blank=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    first_seen_at = models.DateTimeField("首次发现", auto_now_add=True)
    last_seen_at = models.DateTimeField("最近活动", auto_now=True)
    join_policy_mode = models.CharField(
        "入群审批策略",
        max_length=16,
        choices=JoinPolicyMode.choices,
        default=JoinPolicyMode.OFF,
    )
    #: 存 `member_openid` / `union_openid`（`JSONField` list；语义见 events 的审批分支）。
    join_policy_whitelist = models.JSONField(
        "入群白名单", default=list, blank=True
    )

    class Meta:
        verbose_name = "QQ 群"
        verbose_name_plural = "QQ 群"
        ordering = ["-last_seen_at"]

    def __str__(self):
        return self.remark or self.group_name or self.group_openid


class QQBotGroupPushConfig(models.Model):
    """大厅推送配置：一个群 + 一组游戏（对标 `OrderPushSetting` 的「规则」形状，一对一简化）。
    ⚠️ `group` 是 OneToOne：一个群最多一条配置（对标允许多规则，本表有意先收窄；要放宽时先改 `push.queue_hall_push` 的去重语义）。"""

    class MsgType(models.TextChoices):
        MARKDOWN = "markdown", "Markdown"
        TEXT = "text", "纯文本"

    group = models.OneToOneField(
        QQBotGroup,
        on_delete=models.CASCADE,
        related_name="push_config",
        verbose_name="推送群",
    )
    games = models.ManyToManyField(Game, related_name="qqbot_push_configs", verbose_name="限定游戏")
    is_active = models.BooleanField("启用状态", default=True)
    msg_type = models.CharField(
        "消息类型", max_length=16, choices=MsgType.choices, default=MsgType.MARKDOWN
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "QQ 群推送配置"
        verbose_name_plural = "QQ 群推送配置"

    def __str__(self):
        return f"推送配置#{self.pk}→{self.group}"


class QQBotOutboundMessage(models.Model):
    """发件箱：要发给 QQ 群的一条消息（大厅推送 / 测试 / 被动回复都从这走）。
    写入点纪律见 `tests/test_qqbot_outbound_write_net.py`：本表只有 `push.py` 建行（`pending`）、`worker.py` 的 sender 改状态（`sent`/`failed`/退避）。"""

    class Kind(models.TextChoices):
        HALL_PUSH = "hall_push", "大厅推送"
        TEST_PUSH = "test_push", "测试消息"
        PASSIVE_REPLY = "passive_reply", "被动回复"

    class MsgType(models.TextChoices):
        MARKDOWN = "markdown", "Markdown"
        TEXT = "text", "纯文本"

    class Status(models.TextChoices):
        PENDING = "pending", "待发"
        SENT = "sent", "已发"
        FAILED = "failed", "失败"

    group = models.ForeignKey(
        QQBotGroup,
        on_delete=models.CASCADE,
        related_name="outbound_messages",
        verbose_name="目标群",
    )
    kind = models.CharField("类型", max_length=16, choices=Kind.choices)
    order = models.ForeignKey(
        Order,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qqbot_outbound_messages",
        verbose_name="关联订单",
    )
    #: **配置想要的**类型；真发哪一种由 binding.markdown_ok 与本行的 markdown 载荷
    #: 有没有值共同决定（worker.sender）——降级重发成功后会把本列改写成 `text`。
    msg_type_eff = models.CharField(
        "生效消息类型", max_length=16, choices=MsgType.choices, default=MsgType.MARKDOWN
    )
    #: 两套载荷**都存**：markdown 因权限被降级时，sender 直接拿 `payload_text` 补发，
    #: 不需要回到订单重新拼（订单金额可能已变）。
    payload_markdown = models.TextField("Markdown 载荷", blank=True, default="")
    payload_text = models.TextField("纯文本载荷", blank=True, default="")
    passive_msg_id = models.CharField("被动消息 msg_id", max_length=128, blank=True, default="")
    passive_msg_seq = models.IntegerField("被动消息序号", default=1)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.IntegerField("已尝试次数", default=0)
    qq_message_id = models.CharField("QQ 消息 id", max_length=128, blank=True, default="")
    error_code = models.CharField("错误码", max_length=32, blank=True, default="")
    error_text = models.TextField("错误详情", blank=True, default="")
    scheduled_at = models.DateTimeField("计划发送时间")
    sent_at = models.DateTimeField("发送时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "QQ 待发消息"
        verbose_name_plural = "QQ 待发消息"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "scheduled_at"], name="qqbot_out_status_sched"),
            models.Index(fields=["group", "created_at"], name="qqbot_out_group_created"),
        ]

    def __str__(self):
        return f"QQ消息#{self.pk}({self.kind}/{self.status})"


class QQBotJoinRequestLog(models.Model):
    """入群申请与处置留痕（一行 = 一次申请的最终处置）。⚠️ `group` 可空：申请到达时群里可能还没有这个群的投影行
    （`GROUP_ADD_ROBOT` 还没来过）——那一次也要留痕（`decision=manual_pending`），⛔ 不能因为没投影就把一条真人申请静默丢掉（`events.handle_join_request` 的口径）。"""

    class Decision(models.TextChoices):
        AUTO_APPROVED = "auto_approved", "自动通过"
        AUTO_DECLINED = "auto_declined", "自动拒绝"
        MANUAL_APPROVED = "manual_approved", "人工通过"
        MANUAL_DECLINED = "manual_declined", "人工拒绝"
        MANUAL_PENDING = "manual_pending", "待人工处理"
        FAILED = "failed", "处置失败"

    group = models.ForeignKey(
        QQBotGroup,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="join_request_logs",
        verbose_name="目标群",
    )
    join_request_id = models.CharField("申请 id", max_length=128)
    member_openid = models.CharField("申请人 openid", max_length=64)
    union_openid = models.CharField("union openid", max_length=64, blank=True, default="")
    username = models.CharField("申请人昵称", max_length=100, blank=True, default="")
    apply_at = models.DateTimeField("申请时间", null=True, blank=True)
    apply_source = models.CharField("申请来源", max_length=64, blank=True, default="")
    risk_tips = models.CharField("风险提示", max_length=200, blank=True, default="")
    verify_info = models.JSONField("验证信息", null=True, blank=True)
    decision = models.CharField(
        "处置结果", max_length=24, choices=Decision.choices
    )
    reject_reason = models.CharField("拒绝理由", max_length=200, blank=True, default="")
    error_code = models.CharField("错误码", max_length=32, blank=True, default="")
    acted_at = models.DateTimeField("处置时间", null=True, blank=True)
    #: ⭐ 评审 P2-3：**操作人**。人工审批（`api/views.py` 的 approve 端点）落
    # `request.user`；自动审批 / 机器路径（`events.handle_join_request`）为 `None`
    # ——前端留痕表空 = 「自动」。
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="qqbot_join_request_logs",
        verbose_name="操作人",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "QQ 入群申请留痕"
        verbose_name_plural = "QQ 入群申请留痕"
        ordering = ["-created_at"]

    def __str__(self):
        return f"入群申请#{self.pk}({self.decision})"
