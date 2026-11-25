"""QQ 机器人的常驻 worker：每个启用的 binding 一对线程（reader 连 WS 网关 + sender 抽发件箱）。
⭐⭐ 线程入口模板（全仓首例「多线程写库」，判据钉在 `tests/test_qqbot_ws_events.py`）：django-tenants 的租户 schema 是 connection 级状态、线程不继承请求线程的上下文 ⇒ 必须自己切，两步：① `django.db.close_old_connections()`（新线程从零拿连接，先清遗留状态）；② `tenant_context(self.tenant)` 包住线程的全部 ORM 面。（django-tenants 后端每次建 cursor 都重发 `SET search_path`，已读源确认 ⇒ 连接中途重建也不会静默落回 `public`，不需要线程里反复重申。）
sender 四步判定（对每行到期 pending）：① 单群日限（DB 计数今天 `sent` ≥ `GROUP_DAILY_LIMIT` ⇒ `failed`）；② 群滑动窗口满 ⇒ 该行 `scheduled_at` 顺延到窗口空位、换下一行（不消耗名额——先 `wait_time()` 后 `acquire()`，见 `ratelimit.SlidingWindow`）；③ bot 窗口满 ⇒ sleep 小片、本轮不动这行；④ 发送：`msg_type=2` 仅当 `binding.markdown_ok` 且行有 markdown 载荷；错误码 `304036`/`40034127`（无 Markdown 权限）⇒ `markdown_ok=False` + 同内容 `msg_type=0` 重发一次；可重试（`40034100`/`50055001`/`50055006`/网络错）⇒ `attempts+1`、退避重排（`MAX_ATTEMPTS` 次后 `failed`）；成功 ⇒ `sent` + `sent_at` + `qq_message_id`。"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import timedelta

import websocket
from django.db import close_old_connections, connection
from django.utils import timezone
from django_tenants.utils import tenant_context

from apps.qqbot import events, qqbot_api, ws_protocol
from apps.qqbot.models import QQBotBinding, QQBotOutboundMessage
from apps.qqbot.ratelimit import (
    BOT_QPM_UNVERIFIED,
    BOT_QPM_VERIFIED,
    GROUP_DAILY_LIMIT,
    GROUP_QPM,
    SlidingWindow,
)

logger = logging.getLogger(__name__)

#: 官方 hello 帧给的 `heartbeat_interval`（毫秒）标称 45000；心跳判定按它走。
HEARTBEAT_PERIOD_MS_DEFAULT = 45000
#: ⭐ recv 的小片超时（秒，评审 P2-1）：recv 每这么久醒来一次，按「距上次心跳 ≥
#: hello 下发间隔」判发心跳——⛔ 不能拿整个心跳周期当 recv 超时，否则**持续来帧时
#: 永远醒不来、心跳停发**，官方会把连接踢掉。
RECV_SLICE_SECONDS = 5.0
#: 断线退避（秒）：先 resume；失败按这串退避重新 identify（封顶最后一个）。
RECONNECT_BACKOFF_SECONDS = (5, 10, 30, 60)
#: 心跳 ack 之后盖 binding 健康列的节流（秒）——⛔ 不每拍都写。
HEALTH_STAMP_THROTTLE_SECONDS = 300.0
#: sender 空轮询间隔（秒）。
SENDER_POLL_SECONDS = 1.0
#: 单轮最多抽多少行（防一次抽全表；被动行优先、各自限额）。
SEND_DRAIN_BATCH = 50
#: 「首次 + 一次重试」＝总共两次（照 `order_push.MAX_ATTEMPTS` 同一口径）。
MAX_ATTEMPTS = 2
#: 可重试错误的退避（秒）。大厅推送是时效消息，30 秒量级足够；日限/权限类不退避直接判死。
RETRY_BACKOFF_SECONDS = 30.0
#: bot 窗口满时一次睡多少秒（睡整个 wait 会卡住别的群）。
BOT_SLEEP_SLICE_SECONDS = 5.0
#: 这些业务码重试（官方口径：主动消息超频控 / 发送异常稍后重试）。
RETRYABLE_CODES = frozenset({"40034100", "50055001", "50055006"})
NETWORK_CODE = "NETWORK"
#: 无 Markdown 权限（⇒ 触发 binding 级降级 + 同内容文本重发一次）。
MARKDOWN_PRIV_CODES = frozenset({"304036", "40034127"})
DAILY_LIMIT_CODE = "DAILY_LIMIT"


def bot_qpm(binding) -> int:
    """Bot 维度限频按 `auth_verified` 切档（出处见 `ratelimit` 模块 docstring）。"""
    return BOT_QPM_VERIFIED if binding.auth_verified else BOT_QPM_UNVERIFIED


def _local_day_bounds(now):
    """`now` 所在**本地日**的 `[0 点, 明天 0 点)` 边界（评审 P3-6：给 `sent_at` 的范围条件——
    `__date` 查找要对列做类型转换、吃不到索引，范围条件可以）。"""
    local_now = now.astimezone(timezone.get_default_timezone())
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day_start, day_start + timedelta(days=1)


class GroupSender:
    """一个启用绑定的发送面：抽队、限流判定、发送与状态落库（都在租户上下文内跑）。
    ⚠️ 滑动窗口挂在实例上：常驻 loop 里要复用同一个实例，否则窗口每轮清零、限频形同虚设。"""

    def __init__(self, binding, *, token_manager=None):
        self.binding = binding
        self.token_manager = token_manager or qqbot_api.TokenManager(
            binding.app_id, binding.app_secret
        )
        self._bot_window = SlidingWindow(bot_qpm(binding))
        self._group_windows: dict[int, SlidingWindow] = {}

    def drain(self, *, now=None, batch=SEND_DRAIN_BATCH) -> int:
        """处理一轮到期行；返回「了结」数（`sent`/`failed`；顺延与暂睡的不算）。
        抽队口径：被动回复优先、其余按 `created_at`——同一轮里被动行先走（它们有 5 分钟时效，大厅推送没有）。"""
        now = now or timezone.now()
        due = QQBotOutboundMessage.objects.filter(
            status=QQBotOutboundMessage.Status.PENDING,
            scheduled_at__lte=now,
        ).select_related("group")
        rows = list(
            due.filter(kind=QQBotOutboundMessage.Kind.PASSIVE_REPLY).order_by(
                "created_at"
            )[:batch]
        )
        rows += list(
            due.exclude(kind=QQBotOutboundMessage.Kind.PASSIVE_REPLY).order_by(
                "created_at"
            )[:batch]
        )
        # ⭐ 日限计数按群聚合一次（本批候选行涉及的群各 COUNT 一次、时间用范围条件吃索引），
        # ⛔ 不再逐行查；本轮内发送成功就地 +1，保持「逐行判定」语义。
        # ⚠️ 已知未改：events 的群投影双查询、`last_event_at` 每事件必写（无节流）——见 events.py 就地注释。
        daily_counts = self._daily_sent_counts({row.group_id for row in rows}, now)
        resolved = 0
        for row in rows:
            try:
                if self._handle_row(row, now, daily_counts):
                    resolved += 1
            except Exception:  # noqa: BLE001 - 单行失败不断整轮
                logger.exception(
                    "qqbot 发送行处理失败 row=%s group=%s", row.pk, row.group_id
                )
        return resolved

    # ── 单行的四步 ─────────────────────────────────────────────────────────

    def _handle_row(self, row, now, daily_counts) -> bool:
        if self._daily_limit_reached(row.group_id, daily_counts):
            row.status = QQBotOutboundMessage.Status.FAILED
            row.error_code = DAILY_LIMIT_CODE
            row.error_text = "该群今日发送已达官方日限（1000 条）"
            row.save(update_fields=["status", "error_code", "error_text"])
            return True
        group_window = self._group_windows.setdefault(
            row.group_id, SlidingWindow(GROUP_QPM)
        )
        wait = group_window.wait_time()
        if wait > 0:
            row.scheduled_at = now + timedelta(seconds=wait)
            row.save(update_fields=["scheduled_at"])
            return False
        bot_wait = self._bot_window.wait_time()
        if bot_wait > 0:
            time.sleep(min(bot_wait, BOT_SLEEP_SLICE_SECONDS))
            return False
        group_window.acquire()
        self._bot_window.acquire()
        self._send(row, daily_counts)
        return True

    @staticmethod
    def _daily_sent_counts(group_ids, now) -> dict[int, int]:
        """每个候选群的「今日已发」计数（一群一次 COUNT；时间用**范围条件**——
        `sent_at__date` 要对列做类型转换、吃不到索引，`>= 今天0点 且 < 明天0点` 可以）。
        ⚠️ 只在 `drain()` 开头算一次；本轮内发送成功由 `_send` 就地 +1（见下）。"""
        day_start, day_end = _local_day_bounds(now)
        return {
            group_id: QQBotOutboundMessage.objects.filter(
                group_id=group_id,
                status=QQBotOutboundMessage.Status.SENT,
                sent_at__gte=day_start,
                sent_at__lt=day_end,
            ).count()
            for group_id in group_ids
        }

    @staticmethod
    def _daily_limit_reached(group_id: int, daily_counts) -> bool:
        return daily_counts.get(group_id, 0) >= GROUP_DAILY_LIMIT

    def _send(self, row, daily_counts) -> None:
        use_markdown = (
            row.msg_type_eff == QQBotOutboundMessage.MsgType.MARKDOWN
            and self.binding.markdown_ok
            and bool(row.payload_markdown)
        )
        ok, data, code, message = self._attempt_send(row, use_markdown)
        if not ok and code in MARKDOWN_PRIV_CODES and use_markdown:
            # 降级：binding 关 markdown（写回 DB），同内容用文本**重发一次**。
            self.binding.markdown_ok = False
            self.binding.save(update_fields=["markdown_ok", "updated_at"])
            ok, data, code, message = self._attempt_send(row, False)
            if ok:
                row.msg_type_eff = QQBotOutboundMessage.MsgType.TEXT
        if ok:
            row.status = QQBotOutboundMessage.Status.SENT
            row.sent_at = timezone.now()
            row.qq_message_id = str((data or {}).get("id") or "")
            row.error_code = ""
            row.error_text = ""
            row.save(
                update_fields=[
                    "status",
                    "msg_type_eff",
                    "sent_at",
                    "qq_message_id",
                    "error_code",
                    "error_text",
                ]
            )
            # 本批的计数缓存同步 +1：同一轮里后续行照旧能被日限拦住（语义同逐行查库）。
            daily_counts[row.group_id] = daily_counts.get(row.group_id, 0) + 1
            return
        if code in RETRYABLE_CODES or code == NETWORK_CODE:
            row.attempts += 1
            if row.attempts >= MAX_ATTEMPTS:
                self._mark_failed(row, code, message)
                return
            row.scheduled_at = timezone.now() + timedelta(seconds=RETRY_BACKOFF_SECONDS)
            row.save(update_fields=["attempts", "scheduled_at"])
            return
        self._mark_failed(row, code, message)

    def _attempt_send(self, row, use_markdown: bool):
        try:
            return qqbot_api.send_group_message(
                self.token_manager,
                row.group.group_openid,
                msg_type=2 if use_markdown else 0,
                content=None if use_markdown else row.payload_text,
                markdown=row.payload_markdown if use_markdown else None,
                msg_id=row.passive_msg_id or None,
                msg_seq=row.passive_msg_seq or None,
            )
        except Exception as exc:  # noqa: BLE001 - api 层已兜网络错；这里防意外炸穿
            return False, None, NETWORK_CODE, type(exc).__name__

    def _mark_failed(self, row, code: str, message: str) -> None:
        row.status = QQBotOutboundMessage.Status.FAILED
        row.error_code = code
        row.error_text = message
        # attempts 一起存：终态时的已尝试次数也要落库（判定过程的一部分）。
        row.save(update_fields=["status", "attempts", "error_code", "error_text"])


class _InvalidSession(Exception):
    """op9：服务端不认这个会话（resume 被拒）⇒ 丢弃 session、退避后重新 identify。"""


class _ReconnectRequested(Exception):
    """op7：服务端要求重连（session 大概率还有效）⇒ 重连后先试 resume。"""


class BotWorker:
    """一个启用 binding 的常驻线程对。生命周期由命令层的重扫循环管（起 / 停）。"""

    def __init__(self, tenant, binding_id: int, *, stop_event=None):
        self.tenant = tenant
        self.binding_id = binding_id
        self.stop_event = stop_event or threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_alive_stamp = 0.0
        self._backoff_index = 0
        self._heartbeat_seconds = HEARTBEAT_PERIOD_MS_DEFAULT / 1000
        # ⭐ 断线 resume 的会话状态挂实例：`_connect_and_read` 在 READY 与每条带 `s` 的帧上就地更新——连接怎么断的（op7/网络断）都不丢。
        # ⛔ 不能只在「正常返回」时才把会话带出去：op7/断线走的是异常路径，那样外层永远拿不到 session
        # ⇒ 永远重新 identify、从不 resume，断线窗口的事件全丢。
        self._session_id: str | None = None
        self._last_seq: int | None = None
        #: 心跳节拍的时钟（可注入；`SlidingWindow(clock=…)` / `TokenManager(clock=…)` 同款）。
        self._clock = time.monotonic

    # ── 生命周期 ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self._threads = [
            threading.Thread(
                target=self._thread_main(self._reader_loop),
                name=f"qqbot-reader-{self.binding_id}",
                daemon=True,
            ),
            threading.Thread(
                target=self._thread_main(self._sender_loop),
                name=f"qqbot-sender-{self.binding_id}",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    # ─⭐⭐ 线程入口模板（见模块 docstring；判据用例钉在 test_qqbot_ws_events.py）
    def _thread_main(self, fn):
        def _run():
            close_old_connections()
            try:
                with tenant_context(self.tenant):
                    fn()
            finally:
                # 线程收尾把连接还掉：常驻线程只在退出时走到这里（重扫把停用的
                # binding 线程停掉 ⇒ 不还连接就漏）；测试的探针线程跑完即死，
                # 不还连接会让测试库拆除被「1 个在用会话」卡住（ObjectInUse）。
                connection.close()

        return _run

    # ── reader ─────────────────────────────────────────────────────────────

    def _reader_loop(self) -> None:
        """断线重连的外层：resume 材料**每轮都从实例属性现取**（评审 P1-1）——
        op7 / 网络断走的异常路径不经过任何「带回会话」的出口，挂实例是唯一不丢的挂法。"""
        while not self.stop_event.is_set():
            resume = (self._session_id, self._last_seq) if self._session_id else None
            try:
                self._connect_and_read(resume)
                self._backoff_index = 0
            except _InvalidSession:
                # op9 分支已在原地清掉 `_session_id` ⇒ 下一轮走 identify（不再 resume）。
                self._wait_backoff()
            except Exception as exc:  # noqa: BLE001 - 断线是常态，退避重连
                logger.warning(
                    "qqbot reader 断线 binding=%s：%s", self.binding_id, type(exc).__name__
                )
                self._wait_backoff()

    def _connect_and_read(self, resume) -> None:
        """连一次网关并读到断线为止；会话状态**就地**更新在实例属性上（评审 P1-1）。"""
        binding = QQBotBinding.objects.get(pk=self.binding_id)
        token_manager = events._token_manager(binding)
        ok, data, code, message = qqbot_api.fetch_gateway(token_manager)
        if not ok:
            raise RuntimeError(f"取网关失败 {code}: {message}")
        # ⭐ recv 超时是**小片**（评审 P2-1），不是整个心跳周期——否则持续来帧时
        # recv 永远不超时、心跳永远发不出，官方把连接踢掉。
        ws = websocket.create_connection(data["url"], timeout=RECV_SLICE_SECONDS)
        try:
            op, _s, _t, d = ws_protocol.parse_frame(ws.recv())
            if op != ws_protocol.OP_HELLO:
                raise RuntimeError("握手第一帧不是 hello")
            self._heartbeat_seconds = (
                (d or {}).get("heartbeat_interval") or HEARTBEAT_PERIOD_MS_DEFAULT
            ) / 1000
            if resume:
                ws.send(json.dumps(ws_protocol.build_resume(token_manager.get_token(), *resume)))
            else:
                ws.send(json.dumps(ws_protocol.build_identify(token_manager.get_token())))
            last_heartbeat_at = self._clock()
            while not self.stop_event.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    raw = None
                if raw is not None:
                    op, s, t, d = ws_protocol.parse_frame(raw)
                    if s is not None:
                        self._last_seq = s
                    if op == ws_protocol.OP_HEARTBEAT_ACK:
                        self._stamp_alive()
                    elif op == ws_protocol.OP_DISPATCH:
                        if t == ws_protocol.EVENT_READY:
                            self._session_id = (d or {}).get("session_id") or self._session_id
                            self._stamp_alive()
                        else:
                            self._dispatch_safely(t, d)
                    elif op == ws_protocol.OP_RECONNECT:
                        raise _ReconnectRequested()
                    elif op == ws_protocol.OP_INVALID_SESSION:
                        # op9：服务端不认这个会话 ⇒ **就地**丢弃（外层下一轮走 identify）。
                        self._session_id = None
                        self._last_seq = None
                        raise _InvalidSession()
                # ⭐ 心跳按「距上次 ≥ hello 下发间隔」判发（评审 P2-1）：每一轮——
                # 收到帧、超时醒来——都检查，连续来帧也停不了心跳。
                if self._clock() - last_heartbeat_at >= self._heartbeat_seconds:
                    ws.send(json.dumps(ws_protocol.build_heartbeat(self._last_seq)))
                    last_heartbeat_at = self._clock()
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass

    def _dispatch_safely(self, t, d) -> None:
        try:
            events.handle_dispatch(t, d)
        except Exception:  # noqa: BLE001 - 单事件失败不断连（模块 docstring 的原则）
            logger.exception("qqbot 事件处理失败 t=%s", t)

    def _wait_backoff(self) -> None:
        delay = RECONNECT_BACKOFF_SECONDS[
            min(self._backoff_index, len(RECONNECT_BACKOFF_SECONDS) - 1)
        ]
        self._backoff_index += 1
        self.stop_event.wait(delay)

    def _stamp_alive(self) -> None:
        """盖健康列（节流 ≥ `HEALTH_STAMP_THROTTLE_SECONDS`；只写那两列，不动别的）。"""
        now = time.monotonic()
        if now - self._last_alive_stamp < HEALTH_STAMP_THROTTLE_SECONDS:
            return
        self._last_alive_stamp = now
        try:
            binding = QQBotBinding.objects.get(pk=self.binding_id)
            binding.last_alive_at = timezone.now()
            binding.connection_state = QQBotBinding.ConnectionState.ONLINE
            binding.save(update_fields=["last_alive_at", "connection_state", "updated_at"])
        except Exception:  # noqa: BLE001 - 健康列失败不该断连
            logger.debug("qqbot 盖健康列失败", exc_info=True)

    # ── sender ─────────────────────────────────────────────────────────────

    def _sender_loop(self) -> None:
        try:
            binding = QQBotBinding.objects.get(pk=self.binding_id)
        except QQBotBinding.DoesNotExist:
            return
        sender = GroupSender(binding)
        while not self.stop_event.is_set():
            try:
                sender.drain()
            except Exception:  # noqa: BLE001 - 一轮失败下一轮再来
                logger.exception("qqbot sender 轮失败 binding=%s", self.binding_id)
            self.stop_event.wait(SENDER_POLL_SECONDS)
