"""QQ 待发消息（`QQBotOutboundMessage`）的**唯一建行入口**。

三件事，全部只建 `pending` 行、不碰网络（真正发在 `worker.GroupSender`）：

1. `queue_hall_push(order)`：大厅事件 → 命中的推送配置各排一条；
2. `queue_test_push(group)`：页面「发送测试」→ 一条占位载荷；
3. `queue_passive_reply(group, text, msg_id)`：被动回复（官方口径：每 `msg_id`
   最多回 **5** 条、`msg_id` **5 分钟**有效——超了不建行、回 `False`）。

写入点纪律：本表没有别的建行入口 ⇒ 结构性网
`tests/test_qqbot_outbound_write_net.py` 按「一行一个写入点」钉着，新增写入点必须回来登记。

⚠️ 同一 `order + group` **不去重不更新**：一条大厅事件 = 一批新行（与 wecom 推送的
「每条规则各排一次」同一口径——事件本身可重发，去重是接收方的事）。
"""
from __future__ import annotations

import logging

from django.db import connection
from django.utils import timezone

from apps.qqbot.models import (
    QQBotGroup,
    QQBotGroupPushConfig,
    QQBotOutboundMessage,
)
from apps.qqbot.payloads import build_hall_texts

logger = logging.getLogger(__name__)

#: 官方口径：被动回复每 `msg_id` 最多 5 条、`msg_id` 有效期 5 分钟。
PASSIVE_REPLY_MAX_PER_MSG_ID = 5
PASSIVE_REPLY_WINDOW_SECONDS = 300.0


def _system_name() -> str:
    """租户名（消息第三行「系统：…」）。调用点都在租户上下文里（django-tenants 已设）。"""
    return connection.tenant.name


def queue_hall_push(order) -> int:
    """大厅事件入队：命中配置的每个群各一条 `pending` 行；返回排了几条。

    匹配口径照 `order_push.queue_order_push`：`is_active=True` 且
    `games__id=order.game_id`（「这个游戏没勾」的配置自然不匹配）。
    两个载荷**都存**（`payload_markdown` / `payload_text`）：markdown 因权限降级时
    sender 直接拿 text 补发，不回订单重拼（金额可能已变）。
    """
    now = timezone.now()
    text, markdown = build_hall_texts(order, _system_name(), now=now)
    configs = (
        QQBotGroupPushConfig.objects.filter(
            is_active=True, games__id=order.game_id
        )
        .select_related("group")
        .distinct()
    )
    queued = 0
    for config in configs:
        QQBotOutboundMessage.objects.create(
            group=config.group,
            kind=QQBotOutboundMessage.Kind.HALL_PUSH,
            order=order,
            msg_type_eff=config.msg_type,
            payload_markdown=markdown,
            payload_text=text,
            scheduled_at=now,
        )
        queued += 1
    return queued


def queue_test_push(group: QQBotGroup) -> QQBotOutboundMessage:
    """页面「发送测试消息」：不依赖订单的占位载荷（照 `order_push.build_test_payload`
    的思路——价格 / 订单号行用「未设置」占位，格式与大厅推送同版）。"""
    now = timezone.now()

    class _FakeOrder:
        """占位订单：只要 `build_hall_texts` 读的那几个属性。"""

        order_no = "未设置"
        # ⚠️ 与真 `Order` 同形（D-0301 起展示取值会读它）：测试推送没有来源号 ⇒ 空串。
        source_order_no = ""
        hall_price = None
        project_text = "推送测试"
        project_id = None

        class game:  # noqa: N801 - 同 order_push.build_test_payload：只要一个带 name 的对象
            name = "测试游戏"

    text, markdown = build_hall_texts(_FakeOrder(), _system_name(), now=now)
    return QQBotOutboundMessage.objects.create(
        group=group,
        kind=QQBotOutboundMessage.Kind.TEST_PUSH,
        order=None,
        msg_type_eff=QQBotOutboundMessage.MsgType.MARKDOWN,
        payload_markdown=markdown,
        payload_text=text,
        scheduled_at=now,
    )


def queue_passive_reply(
    group: QQBotGroup, text: str, msg_id: str, *, now=None
) -> bool:
    """被动回复入队；超过官方口径（每 `msg_id` ≤5 条、5 分钟有效）时**不建行**回 `False`。

    ⚠️ 5 分钟窗口的**如实口径**（评审 P2-2 改准，原「只会判短不会判长」说反了）：
    `msg_id` 的真实签发时刻（对方发消息那一刻）本表不存，这里用「同一 `msg_id`
    最早一条回复的 `created_at`」当代理。代理起点（首条回复 `created_at`）**晚于**
    原消息到达 ⇒ 测得的年龄**偏小** ⇒ 窗口被**判长**：最坏多放一条进队列、发出去被
    QQ 拒掉留一行 `failed`——错的方向是**多放**，不是错杀（错杀才伤产品：入群回复
    发不出去）。⚠️ 事件 payload 里**没有**可靠的原消息时刻字段可用（官方各事件的时间
    格式未实测钉死，见 `events._parse_moment` 的注），想存用得等真机验证核对后再接。
    一个 `msg_id` 一条记录都没有时无从判龄 ⇒ 放行（调用方刚收到这条消息才会走到这）。
    """
    now = now or timezone.now()
    prior = QQBotOutboundMessage.objects.filter(
        group=group,
        kind=QQBotOutboundMessage.Kind.PASSIVE_REPLY,
        passive_msg_id=msg_id,
    )
    count = prior.count()
    if count >= PASSIVE_REPLY_MAX_PER_MSG_ID:
        return False
    oldest = prior.order_by("created_at").first()
    if oldest is not None and (
        (now - oldest.created_at).total_seconds() > PASSIVE_REPLY_WINDOW_SECONDS
    ):
        return False
    QQBotOutboundMessage.objects.create(
        group=group,
        kind=QQBotOutboundMessage.Kind.PASSIVE_REPLY,
        msg_type_eff=QQBotOutboundMessage.MsgType.TEXT,
        payload_markdown="",
        payload_text=text,
        passive_msg_id=msg_id,
        passive_msg_seq=count + 1,
        scheduled_at=now,
    )
    return True
