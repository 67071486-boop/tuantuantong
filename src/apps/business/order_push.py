"""订单消息推送（对标「自动发消息到微信群设置」；2026-10-17 实装）。 ## 一件事：把订单事件发到租户自己配的 Webhook 规则存在 `OrderPushSetting`（每个租户 schema 一份）。本模块只做三件事： 1. **匹配规则**：`queue_order_push()` 按事件类型 + 订单游戏挑出启用中的规则； 2. **拼消息**：`build_payload()` 是纯函数（可单测），按渠道与消息类型产出不同载荷； 3. **发出去**：`_deliver_later()` 在**业务事务提交之后**才发（`transaction.on_commit`），真正那次 HTTP 在一个 daemon 线程里跑 —— 订单状态变化**不等**一个外部 HTTP。 ## 渠道与载荷（一处约定，改这里等于改对外契约）
| `channel` | 载荷 | |---|---| | `wecom` | 企业微信群机器人原生格式：`{"msgtype":"text"|"markdown", "text"|"markdown":{"content":…}}`；文本模式 `call_all` 时带 `mentioned_list:["@all"]` | | `custom` | 通用 JSON：`{event, order_no, status, from_status, game, project, price, msg_type, content, text:{content}, markdown:{content}, timestamp}` | ⛔ 第四种投递渠道 —— **微信公众号模板消息** —— 不在本表：它是**租户级全局开关**（不挂
`OrderPushSetting` 规则行），实现在 `apps/wechatmp/push.py`，由 `queue_order_push()` 在规则循环之后分流调用（D-0268）。自定义渠道的 `content` 是**纯文本**（给不会解析 markdown 的接收方）；同时附 `text` / `markdown` 两套原生片段，接收方按自己的适配器取即可。 ## 有意与对标不同的一处对标只认企业微信群机器人（外加一个我们没有的内部抢单群聊 `groupId`）。本模块把渠道抽成 `Channel`：企微 / 自定义 Webhook。**没有** `groupId` —— 那是对标自家聊天系统的概念，本项目没有。 ## 安全（SSRF 的第一步） `validate_webhook_url()` 只放行 `http` / `https`，并挡掉回环、`localhost` 与云元数据地址（`169.254.0.0/16`）。**私网地址是放行的**（租户可能把自定义 Webhook 放在内网）——这是有意的口径，不是遗漏；真正的出口边界仍应由部署侧防火墙兜底。 ## 失败怎么办 **不重试、不落库、不阻塞业务**：发失败只写 logger（URL 只留 host，避免把密钥写进日志）。群消息是提示，不是凭据（与 `Notification` 同一口径）。页面上的「发送测试消息」是同步的，失败会把原因回给用户——那一次是配置动作，值得让用户当场知道。"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from apps.business.models import OrderPushSetting, OrderStatus

logger = logging.getLogger(__name__)

#: 事件 → 匹配的 `timing` 档（`0` 是对标的「大厅 + 交易平台」两件事合一）。
EVENT_HALL = "hall"
EVENT_IMPORTANT = "important"
EVENT_PLATFORM = "platform"

EVENT_TIMINGS = {
    EVENT_HALL: (
        OrderPushSetting.Timing.HALL_AND_PLATFORM,
        OrderPushSetting.Timing.HALL,
    ),
    # ⚠️ **只匹配 30**（「交易订单产生重要变化时」）。对标的 `0` 是「大厅 + 交易平台」，
    # 不是「大厅 + 重要变化」——把 0 也算进来会让「大厅+平台」规则多收一类它没配的事件。
    EVENT_IMPORTANT: (OrderPushSetting.Timing.IMPORTANT,),
    # 今天没有接交易平台（M6 未做）⇒ 这一档只留给将来的平台发单事件，本模块**没有**调用方。
    EVENT_PLATFORM: (
        OrderPushSetting.Timing.HALL_AND_PLATFORM,
        OrderPushSetting.Timing.PLATFORM,
    ),
}

#: 「交易订单产生重要变化」的落点：状态迁入这些目标态时发。
#: ⚠️ `WAITING_GRAB` **不在**这里：它是「大厅上架」那个事件（`EVENT_HALL`），
#: 两者分开才不会一条抛单消息发两遍。
IMPORTANT_TARGET_STATUSES = frozenset(
    {
        OrderStatus.IN_PROGRESS,
        OrderStatus.PENDING_REVIEW,
        OrderStatus.COMPLETED,
        OrderStatus.ISSUE,
        OrderStatus.PAUSED,
        OrderStatus.REFUNDED,
    }
)

#: 单次 HTTP 超时（秒）。群消息不值得让后台线程挂很久。
HTTP_TIMEOUT = 5.0
#: 后台线程最多试两次（首次 + 一次重试），第二次失败只记日志。
MAX_ATTEMPTS = 2

USER_AGENT = "tuantuantong-order-push/1.0"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """**不跟随 3xx**。跟随重定向会把「我们校验过的公网地址」变成「一个未校验的跳转目标」——SSRF 的经典绕过。
    Webhook 端点正常不会重定向；真返回 3xx 时按失败处理（日志里说清 HTTP 码）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


#: 无重定向的 opener（`_post_json` 的唯一 HTTP 出口）。
_OPENER = urllib.request.build_opener(_NoRedirectHandler())


class PushDeliveryError(Exception):
    """发送失败（URL 非法 / 网络错误 / 对端非 2xx / 响应不是 JSON 都算）。"""


# ── URL 校验 ────────────────────────────────────────────────────────────────


def validate_webhook_url(raw: str) -> str:
    """校验并原样返回 Webhook 地址；不合法时抛 `ValueError`（消息可直接给用户看）。
    只挡「明确不该由租户配置触发」的目标：非 http(s)、回环、云元数据地址。
    **私网放行**（见模块 docstring）。"""
    url = (raw or "").strip()
    if not url:
        raise ValueError("Webhook 地址不能为空")
    if len(url) > 500:
        raise ValueError("Webhook 地址不能超过 500 个字符")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Webhook 地址必须是 http:// 或 https:// 开头的完整地址")
    # `hostname` 可能带尾点（`127.0.0.1.` 也是回环），先归一。
    host = parsed.hostname.lower().strip("[]").rstrip(".")
    if host in {"localhost", "0.0.0.0", "::1"} or host.endswith(".localhost"):
        raise ValueError("Webhook 地址不能指向本机回环地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        # `::ffff:127.0.0.1` 这类 IPv4-mapped 地址：`.is_loopback` 对它为 False，要单独看。
        mapped = getattr(address, "ipv4_mapped", None)
        if address.is_loopback or (mapped is not None and mapped.is_loopback):
            raise ValueError("Webhook 地址不能指向本机回环地址")
        if address.is_unspecified:
            raise ValueError("Webhook 地址不能是未指定地址")
        # 云元数据地址（IPv4 `169.254.0.0/16` / IPv6 `fe80::/10` 等链路本地段）：
        # 挡掉，避免借租户配置打云厂商元数据。
        if address.is_link_local:
            raise ValueError("Webhook 地址不能指向链路本地地址")
    return url


# ── 消息拼装（纯函数）──────────────────────────────────────────────────────


def _money(value) -> str:
    if value is None:
        return "未设置"
    return f"¥{value:.2f}"


def _price_cell(value) -> str:
    """custom 渠道 JSON 里 `price` 的格（E-0263-F2）。有值 `str()` 原样（对外合同不变）；缺失给「未设置」——⛔ `str(None)` 的 Python
    字面量 `"None"` 不许混进对外 JSON（与正文 `_money` 的空值口径一致）。真实订单两个金额列 NOT NULL，缺失只出现在「发送测试消息」这类无订单载荷里。"""
    return "未设置" if value is None else str(value)


def _project_name(order) -> str:
    """`Order.project_text` 是权威值（D9）；为空才回落到 FK 的名字。"""
    text = (order.project_text or "").strip()
    if text:
        return text
    if order.project_id:
        return order.project.name
    return "—"


def _status_label(raw: str) -> str:
    try:
        return OrderStatus(raw).label
    except ValueError:
        return raw or "—"


def _header(event: str) -> str:
    return "新单上架" if event == EVENT_HALL else "订单动态"


def _display_order_no(order) -> str:
    """推送文本里「订单号」的取值（⭐ D-0301）。 **来源单号非空取它、空回退内置号**——与全端展示口径同一条。⚠️ 只换**人读**的
    消息文本；`build_payload` 里 custom 渠道 JSON 顶层的 `order_no` 是机器合同字段，
    ⛔ 不换（下游按它寻址）。"""
    return order.source_order_no or order.order_no


def _text_body(setting, order, event, from_status=None) -> str:
    lines = [
        f"【{_header(event)}】{order.game.name} / {_project_name(order)}",
        f"订单号：{_display_order_no(order)}",
    ]
    if event == EVENT_HALL:
        lines.append(f"发单价：{_money(order.hall_price)}")
        lines.append(f"状态：{_status_label(order.status)}")
    else:
        before = _status_label(from_status) if from_status else "—"
        lines.append(f"状态：{before} → {_status_label(order.status)}")
        lines.append(f"订单总价：{_money(order.total_price)}")
    if order.server_id:
        lines.append(f"区服：{order.server.name}")
    if setting.remark:
        lines.append(f"说明：{setting.remark}")
    lines.append(f"时间：{timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')}")
    return "\n".join(lines)


def _markdown_body(setting, order, event, from_status=None) -> str:
    lines = [
        f"## {_header(event)}",
        f"> 游戏：{order.game.name}",
        f"> 项目：{_project_name(order)}",
        f"> 订单号：{_display_order_no(order)}",
    ]
    if event == EVENT_HALL:
        lines.append(f'> 发单价：<font color="warning">{_money(order.hall_price)}</font>')
        lines.append(f"> 状态：{_status_label(order.status)}")
    else:
        before = _status_label(from_status) if from_status else "—"
        lines.append(f"> 状态：{before} → <font color=\"info\">{_status_label(order.status)}</font>")
        lines.append(f"> 订单总价：{_money(order.total_price)}")
    if order.server_id:
        lines.append(f"> 区服：{order.server.name}")
    if setting.remark:
        lines.append(f"> 说明：{setting.remark}")
    lines.append(f"> 时间：{timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')}")
    if setting.call_all:
        # ⚠️ 企微群机器人的 markdown **不支持** mentioned_list；这里只能把 @all 写成可见文字。
        # 真要让所有人都收到提醒，请把消息类型选成「文本模式」（页面有这句提示）。
        lines.append("")
        lines.append("@all")
    return "\n".join(lines)


def build_payload(setting, order, event: str, from_status: str | None = None) -> dict:
    """按渠道产出**最终发出去的那个 dict**（纯函数，测试直接钉它）。"""
    text = _text_body(setting, order, event, from_status=from_status)
    markdown = _markdown_body(setting, order, event, from_status=from_status)
    if setting.channel == OrderPushSetting.Channel.WECOM:
        if setting.msg_type == OrderPushSetting.MsgType.TEXT:
            body = {"msgtype": "text", "text": {"content": text}}
            if setting.call_all:
                body["text"]["mentioned_list"] = ["@all"]
            return body
        return {"msgtype": "markdown", "markdown": {"content": markdown}}
    # 自定义渠道：通用形状 + 两套原生片段（见模块 docstring 的表）。
    return {
        "event": f"order.{event}",
        "order_no": order.order_no,
        "status": order.status,
        "from_status": from_status or "",
        "game": order.game.name,
        "project": _project_name(order),
        "price": (
            str(order.hall_price)
            if event == EVENT_HALL and order.hall_price is not None
            else _price_cell(order.total_price)
        ),
        "msg_type": setting.msg_type,
        "content": text,
        "text": {"content": text},
        "markdown": {"content": markdown},
        "timestamp": timezone.now().astimezone(dt_timezone.utc).isoformat(),
    }


def build_test_payload(setting) -> dict:
    """「发送测试消息」用的载荷。不依赖任何订单（页面上没有选单的动作）。"""

    class _FakeOrder:
        order_no = "TEST-0000000000000000"
        # ⚠️ 与真 `Order` 同形（D-0301 起展示取值会读它）：测试消息没有来源号 ⇒ 空串。
        source_order_no = ""
        status = OrderStatus.WAITING_GRAB
        hall_price = None
        total_price = None
        project_text = "测试项目"
        project_id = None
        server_id = None

        class game:  # noqa: N801 - 这里只要一个带 name 的对象
            name = "测试游戏"

    return build_payload(setting, _FakeOrder(), EVENT_HALL)


# ── 发送 ────────────────────────────────────────────────────────────────────


def _post_json(
    url: str,
    payload: dict,
    *,
    timeout: float = HTTP_TIMEOUT,
    check_wecom_errcode: bool = False,
) -> dict | None:
    """发一个 JSON POST；非 2xx / 网络错误 / 非 JSON 响应都抛 `PushDeliveryError`。
    错误消息里**只留 host**：企微 webhook 的 `key=` 是凭据，不能进日志或用户可见文案。"""
    safe_host = urllib.parse.urlparse(url).hostname or "（未知地址）"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:  # noqa: S310
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise PushDeliveryError(f"Webhook 返回 HTTP {exc.code}（{safe_host}）") from exc
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        raise PushDeliveryError(f"连接 Webhook 失败（{safe_host}）：{exc.__class__.__name__}") from exc
    if status < 200 or status >= 300:
        raise PushDeliveryError(f"Webhook 返回 HTTP {status}（{safe_host}）")
    if not raw:
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    # 企微群机器人成功是 `{"errcode":0,"errmsg":"ok"}`；非 0 要当失败（HTTP 仍是 200）。
    # ⚠️ 这个判断**只对企微渠道**成立：自定义 Webhook 返回的 `errcode` 是它自己的业务字段，
    # 不能拿企微的语义去判（否则对方回一个 `{"errcode":1,"msg":"ok"}` 会被我们当失败）。
    if (
        check_wecom_errcode
        and isinstance(body, dict)
        and body.get("errcode") not in (None, 0)
    ):
        raise PushDeliveryError(f"Webhook 返回 errcode={body.get('errcode')}：{body.get('errmsg') or ''}")
    return body


def _send(*, setting_id: int, url: str, channel: str, payload: dict) -> None:
    """一次发送（最多两次）。**线程里跑**，失败只记日志。"""
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            validate_webhook_url(url)
            _post_json(
                url,
                payload,
                check_wecom_errcode=channel == OrderPushSetting.Channel.WECOM,
            )
            return
        except (PushDeliveryError, ValueError) as exc:
            last_error = exc
            if attempt >= MAX_ATTEMPTS:
                break
    logger.warning(
        "订单消息推送失败 setting=%s channel=%s：%s",
        setting_id,
        channel,
        last_error,
    )


def _send_in_thread(
    *, setting_id: int, url: str, channel: str, payload: dict
) -> None:
    """线程入口。**只收普通值、不碰数据库**。 ⚠️ 这条是**多租户下的硬约束**，不是洁癖：新线程的数据库连接是**另一条**，它没有
    请求线程设过的租户 schema（django-tenants 的 schema 是 connection 级状态）⇒
    在线程里 `OrderPushSetting.objects.filter(...)` 会去 `public` 找表、直接报「表不存在」。所以规则值在**入队时**就取好、随线程参数带过去。"""
    _send(setting_id=setting_id, url=url, channel=channel, payload=payload)


def _deliver_later(setting_id: int, *, url: str, channel: str, payload: dict) -> None:
    """提交之后再起线程（`transaction.on_commit` 由调用方保证在事务内）。"""

    def _start() -> None:
        threading.Thread(
            target=_send_in_thread,
            kwargs={
                "setting_id": setting_id,
                "url": url,
                "channel": channel,
                "payload": payload,
            },
            name=f"order-push-{setting_id}",
            daemon=True,
        ).start()

    transaction.on_commit(_start)


def send_test_message(setting) -> None:
    """同步发一条测试消息（页面按钮）。失败抛 `PushDeliveryError`，由视图翻成业务错误。"""
    payload = build_test_payload(setting)
    validate_webhook_url(setting.webhook_url)
    _post_json(
        setting.webhook_url,
        payload,
        check_wecom_errcode=setting.channel == OrderPushSetting.Channel.WECOM,
    )


# ── 事件入口 ────────────────────────────────────────────────────────────────


def queue_order_push(order, event: str, from_status: str | None = None) -> int:
    """给匹配的规则各排一次发送；返回排了几条。 ⚠️ 必须在**业务写入所在的事务里**调用（本函数用 `transaction.on_commit`）。
    ⚠️ `games` 必填：`games__id=order.game_id` 让「这个游戏没勾」的规则自然不匹配。"""
    timings = EVENT_TIMINGS.get(event)
    if not timings:
        return 0
    settings = (
        OrderPushSetting.objects.filter(
            is_active=True, timing__in=timings, games__id=order.game_id
        )
        .distinct()
    )
    sent = 0
    for setting in settings:
        payload = build_payload(setting, order, event, from_status=from_status)
        _deliver_later(
            setting.pk,
            url=setting.webhook_url,
            channel=setting.channel,
            payload=payload,
        )
        sent += 1
    # ⭐ 微信公众号模板消息渠道（D-0268）。⚠️ 它是**租户级全局开关** （`WechatMpConfig.enabled` + `template_id`），与上面的规则行**无关** ——
    # 即使一条规则都不匹配，只要开关开着、收件人绑定了公众号，模板消息照样发（计划 T5 / H2：按收件人逐个发的形态与"一行规则一个 webhook"不同，v1 不动 `OrderPushSetting`）。收件人 = `sales_user` + MAIN 接手打手中已绑定者。
    # ⚠️ **函数内懒导入**（与调用方 `order_status.py` 同款）：不让本模块顶层依赖 `apps.wechatmp`；线程禁查库的约束由那个模块自己扛（token/openid 在请求线程取好、随参数交线程）。
    from apps.wechatmp.push import queue_wechatmp_push

    sent += queue_wechatmp_push(order, event, from_status=from_status)
    return sent


def queue_status_change_push(order, *, from_status: str | None, hall_publisher) -> int:
    """状态迁移的**唯一**推送钩子（`apply_transition` 调它）。 - 目标态 `WAITING_GRAB` 且 `hall_publisher` 非空（= 抛单到大厅）⇒ 大厅事件；
    - 目标态在 `IMPORTANT_TARGET_STATUSES` 里 ⇒ 重要变化事件。两条互斥：`WAITING_GRAB` 不在重要变化集合里（见那个常量的注释）。"""
    target = str(order.status)
    if target == OrderStatus.WAITING_GRAB and hall_publisher is not None:
        sent = queue_order_push(order, EVENT_HALL)
        # QQ 机器人群推送——同样只吃**大厅**事件。 ⚠️ 不是漏了别的事件：本钩子整体在 `apply_transition` 的 `suppress_outbound_sync` 闸内（order_status.py:416 那个 `if`），QQ 侧与
        # wecom/custom 同一批触发；QQ 入队只写本租户库里的 pending 行，与业务 **同事务、同生共死**（没有外部 HTTP，不存在 on_commit 半途问题）。
        # 外部回拉路径 `hall_publisher=None` 不产生 EVENT_HALL ⇒ 不会漏推也不会多推。
        try:
            from apps.qqbot.push import queue_hall_push  # 延迟导入防环（同 order_status 的口径）

            queue_hall_push(order)
        except Exception:  # noqa: BLE001 - 通知面失败不得回滚业务（与 wecom 同一口径）
            logger.warning("qqbot hall push enqueue failed", exc_info=True)
        return sent
    if target in IMPORTANT_TARGET_STATUSES:
        return queue_order_push(order, EVENT_IMPORTANT, from_status=from_status)
    return 0
