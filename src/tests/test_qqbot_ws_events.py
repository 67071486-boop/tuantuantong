"""WS 协议纯函数 + 网关事件处理器 + ⭐线程租户上下文（批次A）。

三块判据：
1. **协议帧**（纯函数）：identify 的 intents / token 前缀、心跳 `d`=最新 `s`、resume 帧；
2. **事件投影与审批**：`handle_dispatch` 各事件对 `QQBotGroup` / 留痕行的落库口径，
   以及「未知事件不抛」「审批 API 异常吞掉留痕」；
3. ⭐ **线程入口模板**：worker 的子线程必须自己进 `tenant_context`（django-tenants 的
   schema 是 connection 级状态，线程不继承）——这是全仓首例多线程写库，判据必须钉死。

修复轮（评审 P1-1 / P2-1 / P3-4 / P2-3）追加两块：
4. **断线 resume 与心跳切片**：假 ws 注入跑 `_reader_loop`——op7 重连发 op6 resume
   （会话挂实例）、op9 丢弃会话重新 identify、连续来帧停不了心跳；
5. `_TM_CACHE` 缓存键 = `app_id`（多租户防互踩）；自动审批留痕行 `acted_by` 为 `None`。
"""
import json
import threading
from uuid import uuid4

from django.db import connection
from django.test import TestCase
from django_tenants.test.cases import TenantTestCase
from unittest import mock

from apps.qqbot import events, ws_protocol, worker
from apps.qqbot.models import (
    QQBotBinding,
    QQBotGroup,
    QQBotJoinRequestLog,
)


# ── ① 协议帧（纯函数，不碰 DB）─────────────────────────────────────────────


class ProtocolFrameTests(TestCase):
    def test_identify_carries_both_intents_and_token_scheme(self):
        frame = ws_protocol.build_identify("tok-1")
        self.assertEqual(frame["op"], ws_protocol.OP_IDENTIFY)
        self.assertEqual(
            frame["d"]["intents"],
            (1 << 25) | (1 << 24),
            "GROUP_AND_C2C(1<<25) 与 GROUP_MEMBER(1<<24) 两个位都要订阅",
        )
        self.assertTrue(frame["d"]["token"].startswith("QQBot "))
        self.assertEqual(frame["d"]["shard"], [0, 1])

    def test_heartbeat_carries_latest_seq_and_null_first(self):
        self.assertEqual(
            ws_protocol.build_heartbeat(42),
            {"op": ws_protocol.OP_HEARTBEAT, "d": 42},
        )
        self.assertIsNone(ws_protocol.build_heartbeat(None)["d"], "首跳 d=null")

    def test_resume_frame_shape(self):
        frame = ws_protocol.build_resume("tok-1", "SESSION-9", 7)
        self.assertEqual(frame["op"], ws_protocol.OP_RESUME)
        self.assertEqual(
            frame["d"],
            {"token": "QQBot tok-1", "session_id": "SESSION-9", "seq": 7},
        )

    def test_parse_frame_and_malformed_fallback(self):
        self.assertEqual(
            ws_protocol.parse_frame('{"op":0,"s":5,"t":"READY","d":{"a":1}}'),
            (0, 5, "READY", {"a": 1}),
        )
        self.assertEqual(
            ws_protocol.parse_frame('{"op":11,"d":null}'), (11, None, None, None)
        )
        self.assertEqual(ws_protocol.parse_frame("not-json"), (None, None, None, None))
        self.assertEqual(ws_protocol.parse_frame("[1,2]"), (None, None, None, None))


# ── ② 事件投影与审批 ────────────────────────────────────────────────────────


def _join_payload(**overrides):
    payload = {
        "group_openid": "GROUP-1",
        "join_request_id": "REQ-1",
        "member_openid": "MEM-1",
        "union_openid": "UNION-1",
        "username": "申请人甲",
        "apply_at": "2026-10-19T10:00:00+08:00",
        "apply_source": "search",
        "risk_tips": "",
        "verify_info": {"question": "谁介绍的"},
    }
    payload.update(overrides)
    return payload


class EventDispatchTests(TenantTestCase):
    def setUp(self):
        self.binding = QQBotBinding.get_solo()
        self.group = QQBotGroup.objects.create(group_openid="GROUP-1")

    def _logs(self):
        return list(QQBotJoinRequestLog.objects.order_by("pk"))

    def test_add_robot_upserts_active_group_with_joined_at(self):
        events.handle_dispatch(
            "GROUP_ADD_ROBOT",
            {
                "group_openid": "GROUP-NEW",
                "op_member_openid": "ADMIN-1",
                "timestamp": "2026-10-19T10:00:00+08:00",
            },
        )
        row = QQBotGroup.objects.get(group_openid="GROUP-NEW")
        self.assertEqual(row.status, QQBotGroup.Status.ACTIVE)
        self.assertIsNotNone(row.joined_at)
        self.assertIsNotNone(row.first_seen_at)
        self.assertIsNotNone(QQBotBinding.get_solo().last_event_at)

    def test_add_robot_brings_a_gone_group_back_to_active(self):
        self.group.status = QQBotGroup.Status.GONE
        self.group.save(update_fields=["status"])
        events.handle_dispatch(
            "GROUP_ADD_ROBOT", {"group_openid": "GROUP-1", "timestamp": None}
        )
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, QQBotGroup.Status.ACTIVE)
        # 回迁不新建行（还是一个群一行）。
        self.assertEqual(QQBotGroup.objects.count(), 1)

    def test_del_robot_marks_gone_and_never_deletes(self):
        events.handle_dispatch("GROUP_DEL_ROBOT", {"group_openid": "GROUP-1"})
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, QQBotGroup.Status.GONE)
        # 没投影过的群被移出：也留一行 gone（推送配置/留痕挂着 FK，行不能凭空造）。
        events.handle_dispatch("GROUP_DEL_ROBOT", {"group_openid": "GROUP-404"})
        self.assertTrue(
            QQBotGroup.objects.filter(
                group_openid="GROUP-404", status=QQBotGroup.Status.GONE
            ).exists()
        )

    def test_msg_reject_and_receive_flip_the_proactive_flag(self):
        events.handle_dispatch("GROUP_MSG_REJECT", {"group_openid": "GROUP-1"})
        self.group.refresh_from_db()
        self.assertFalse(self.group.allow_proactive_msg)
        events.handle_dispatch("GROUP_MSG_RECEIVE", {"group_openid": "GROUP-1"})
        self.group.refresh_from_db()
        self.assertTrue(self.group.allow_proactive_msg)

    def test_at_message_creates_projection_and_touches_last_seen(self):
        events.handle_dispatch(
            "GROUP_AT_MESSAGE_CREATE",
            {"group_openid": "GROUP-AT", "content": "在吗"},
        )
        self.assertTrue(QQBotGroup.objects.filter(group_openid="GROUP-AT").exists())

    def test_join_request_with_approve_all_calls_api_and_logs(self):
        self.group.join_policy_mode = QQBotGroup.JoinPolicyMode.APPROVE_ALL
        self.group.save(update_fields=["join_policy_mode"])
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            return_value=(True, {}, "0", ""),
        ) as approve:
            events.handle_dispatch("GROUP_JOIN_REQUEST", _join_payload())
        approve.assert_called_once()
        self.assertEqual(approve.call_args.kwargs["op"], "approve")
        (log,) = self._logs()
        self.assertEqual(log.decision, QQBotJoinRequestLog.Decision.AUTO_APPROVED)
        self.assertEqual(log.join_request_id, "REQ-1")
        self.assertEqual(log.member_openid, "MEM-1")
        self.assertEqual(log.username, "申请人甲")
        self.assertIsNotNone(log.apply_at)
        self.assertIsNotNone(log.acted_at)
        # ⭐ 评审 P2-3：自动审批是**机器路径** ⇒ 没有操作人（人工审批才有，见 views 测试）。
        self.assertIsNone(log.acted_by)

    def test_join_request_whitelist_hit_and_miss(self):
        self.group.join_policy_mode = QQBotGroup.JoinPolicyMode.WHITELIST
        self.group.join_policy_whitelist = ["MEM-OK"]
        self.group.save(update_fields=["join_policy_mode", "join_policy_whitelist"])
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            return_value=(True, {}, "0", ""),
        ) as approve:
            events.handle_dispatch(
                "GROUP_JOIN_REQUEST", _join_payload(member_openid="MEM-OK")
            )
            self.assertEqual(approve.call_args.kwargs["op"], "approve")
            events.handle_dispatch(
                "GROUP_JOIN_REQUEST", _join_payload(member_openid="MEM-BAD")
            )
            self.assertEqual(approve.call_args.kwargs["op"], "decline")
        decisions = [log.decision for log in self._logs()]
        self.assertEqual(
            decisions,
            [
                QQBotJoinRequestLog.Decision.AUTO_APPROVED,
                QQBotJoinRequestLog.Decision.AUTO_DECLINED,
            ],
        )

    def test_join_request_whitelist_also_matches_union_openid(self):
        self.group.join_policy_mode = QQBotGroup.JoinPolicyMode.WHITELIST
        self.group.join_policy_whitelist = ["UNION-1"]  # 命中的是 union_openid
        self.group.save(update_fields=["join_policy_mode", "join_policy_whitelist"])
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            return_value=(True, {}, "0", ""),
        ) as approve:
            events.handle_dispatch("GROUP_JOIN_REQUEST", _join_payload())
        self.assertEqual(approve.call_args.kwargs["op"], "approve")

    def test_join_request_policy_off_only_leaves_manual_pending(self):
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request"
        ) as approve:
            events.handle_dispatch("GROUP_JOIN_REQUEST", _join_payload())
        approve.assert_not_called()
        (log,) = self._logs()
        self.assertEqual(log.decision, QQBotJoinRequestLog.Decision.MANUAL_PENDING)
        self.assertIsNone(log.acted_at)

    def test_join_request_without_group_projection_leaves_manual_pending(self):
        events.handle_dispatch(
            "GROUP_JOIN_REQUEST", _join_payload(group_openid="GROUP-404")
        )
        (log,) = self._logs()
        self.assertEqual(log.decision, QQBotJoinRequestLog.Decision.MANUAL_PENDING)
        self.assertIsNone(log.group)

    def test_join_request_api_failure_is_logged_as_failed(self):
        self.group.join_policy_mode = QQBotGroup.JoinPolicyMode.APPROVE_ALL
        self.group.save(update_fields=["join_policy_mode"])
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            return_value=(False, {}, "500001", "上游炸了"),
        ):
            events.handle_dispatch("GROUP_JOIN_REQUEST", _join_payload())
        (log,) = self._logs()
        self.assertEqual(log.decision, QQBotJoinRequestLog.Decision.FAILED)
        self.assertEqual(log.error_code, "500001")

    def test_join_request_api_exception_is_swallowed_and_logged(self):
        """⭐ 审批接口抛异常不能让事件循环死（吞掉 + failed 留痕）。"""
        self.group.join_policy_mode = QQBotGroup.JoinPolicyMode.APPROVE_ALL
        self.group.save(update_fields=["join_policy_mode"])
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            side_effect=RuntimeError("boom"),
        ):
            events.handle_dispatch("GROUP_JOIN_REQUEST", _join_payload())
        (log,) = self._logs()
        self.assertEqual(log.decision, QQBotJoinRequestLog.Decision.FAILED)
        self.assertEqual(log.error_code, "RuntimeError")

    def test_unknown_event_is_ignored_without_raising(self):
        events.handle_dispatch("GROUP_FUTURE_EVENT", {"whatever": 1})
        events.handle_dispatch(None, {})
        self.assertEqual(QQBotGroup.objects.count(), 1)


# ── ③ ⭐ 线程租户上下文 ─────────────────────────────────────────────────────


class WorkerThreadTenantContextTests(TenantTestCase):
    def test_thread_main_lands_in_the_tenant_schema(self):
        """⭐ worker 的线程入口模板：子线程里 `connection.schema_name` 是租户 schema，
        且它建的行落在租户 schema（qqbot 是 TENANT_APPS ⇒ public 里**连表都没有**）。

        ⚠️ 判别力：把模板里的 `tenant_context(self.tenant)` 删掉 ⇒ 子线程落在
        `public`，第一句 `connection.schema_name` 断言当场红；单独删
        `close_old_connections()` 则由线程复用主线程遗留连接的路径背锅（这条用
        本断言问不到——它的判据在「连接是新的」，见 `_thread_main` docstring）。
        """
        tenant = connection.tenant
        openid = f"GROUP-THREAD-{uuid4().hex[:12]}"
        seen = {}

        def probe():
            seen["schema"] = connection.schema_name
            QQBotGroup.objects.create(group_openid=openid)

        worker_obj = worker.BotWorker(tenant, binding_id=0)
        runner = worker_obj._thread_main(probe)
        thread = threading.Thread(target=runner, name="qqbot-probe")
        thread.start()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "探针线程 30 秒没跑完")
        # ⚠️ 线程里的写入是**独立连接、独立事务**（已提交），不会随本用例的
        #    事务回滚消失 ⇒ 先登记清理、再做断言（断言红了也不能留下脏行）。
        self.addCleanup(lambda: QQBotGroup.objects.filter(group_openid=openid).delete())
        self.assertEqual(seen["schema"], tenant.schema_name)
        # 行落在租户 schema：本线程（租户上下文）查得到、恰好一行。
        self.assertEqual(QQBotGroup.objects.filter(group_openid=openid).count(), 1)
        # 跨 schema 查不到的**硬判据**：qqbot 表只存在于租户 schema，public 没有。
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                ["public", QQBotGroup._meta.db_table],
            )
            (public_tables,) = cursor.fetchone()
        self.assertEqual(public_tables, 0)


# ── ④ ⭐ 断线 resume 与心跳切片（评审 P1-1 / P2-1）─────────────────────

#: `_ScriptedWs.recv` 的「超时」哨兵：抛真的 `WebSocketTimeoutException`
#: （worker 的 `except` 靠类型接，假异常类接不上）。
_TIMEOUT = object()


class _ScriptedWs:
    """可编程的假 ws（评审 P1-1 / P2-1 的注入物）。

    `steps` 逐个消费：帧字符串原样回、`_TIMEOUT` 抛超时、callable 先执行副作用
    （拨时钟 / 置 stop）再继续；步骤耗尽后恒超时。`sent` 收**所有发出的帧**
    （json 解码后）——「第二次连接发出的是 resume」的判据就打在它上面。
    """

    def __init__(self, steps):
        self.steps = list(steps)
        self.sent = []

    def recv(self):
        while self.steps:
            step = self.steps.pop(0)
            if callable(step):
                step()
                continue
            if step is _TIMEOUT:
                break
            return step
        raise worker.websocket.WebSocketTimeoutException()

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        pass


def _hello(heartbeat_interval_ms: int) -> str:
    return json.dumps({"op": ws_protocol.OP_HELLO, "d": {"heartbeat_interval": heartbeat_interval_ms}})


def _frame(op, *, s=None, t=None, d=None) -> str:
    frame: dict = {"op": op}
    if s is not None:
        frame["s"] = s
    if t is not None:
        frame["t"] = t
    if d is not None:
        frame["d"] = d
    return json.dumps(frame)


def _stop_step(holder) -> callable:
    """一个 callable step：执行时把 worker 的 stop_event 置位（测试收口用）。"""

    def _stop():
        holder["worker"].stop_event.set()

    return _stop


class WorkerReaderLoopTests(TenantTestCase):
    """假 ws 注入跑 `_reader_loop`（评审 P1-1 / P2-1 的判定物）。

    ⚠️ mock 落点（E-0029）：`fetch_gateway` 与 `_token_manager` 打在**被查找的**
    `apps.qqbot.qqbot_api` / `apps.qqbot.events` 模块属性上；`create_connection`
    打在 worker 里那个 `websocket` 模块的属性上。
    """

    def setUp(self):
        self.binding = QQBotBinding.get_solo()
        self.binding.app_id = "APP-1"
        self.binding.app_secret = "SECRET-1"
        self.binding.save()
        # ⭐ 模块级 TokenManager 缓存跨用例残留 ⇒ 进出都清（P3-4 之后键是 app_id）。
        events._TM_CACHE.clear()
        self.addCleanup(events._TM_CACHE.clear)

    def run_reader(self, scripts, clock=None):
        """按顺序把 `_ScriptedWs` 注入 `create_connection`，同步跑 `_reader_loop`
        直到某个 step 把 stop_event 置位；返回用过的假 ws 列表。

        `scripts` 是「第 N 次连接的 step 清单」工厂列表（收 `holder`，让 step 能摸到
        尚未创建的 worker）。顺带钉死：每次建连的 recv 超时必须是**小片**
        （评审 P2-1），⛔ 不是整个心跳周期。
        """
        fakes = []
        holder = {}

        def fake_create(url, timeout=None):
            self.assertEqual(
                timeout,
                worker.RECV_SLICE_SECONDS,
                "recv 超时必须是小片（评审 P2-1）：整周期超时会在持续来帧时饿死心跳",
            )
            fake = _ScriptedWs(scripts[len(fakes)](holder))
            fakes.append(fake)
            return fake

        with mock.patch(
            "apps.qqbot.qqbot_api.fetch_gateway",
            return_value=(True, {"url": "wss://gate.example/"}, "0", ""),
        ), mock.patch(
            "apps.qqbot.events._token_manager",
            return_value=mock.Mock(get_token=mock.Mock(return_value="tok-1")),
        ), mock.patch(
            "apps.qqbot.worker.websocket.create_connection", side_effect=fake_create
        ), mock.patch(
            "apps.qqbot.worker.RECONNECT_BACKOFF_SECONDS", (0,)
        ):
            worker_obj = worker.BotWorker(connection.tenant, self.binding.pk)
            if clock is not None:
                worker_obj._clock = lambda: clock[0]
            holder["worker"] = worker_obj
            worker_obj._reader_loop()
        return fakes

    def test_op7_reconnect_sends_resume_with_session_id_and_seq(self):
        """⭐ 评审 P1-1（核心）：op7 断开后第二跳发 op6 **resume** 且带 session_id / seq。

        变异方向：把会话改回「只在 `_connect_and_read` 正常返回时带出」⇒ op7 走异常
        路径、外层拿不到 session ⇒ 本用例红（第二跳变 identify）。
        """

        def script1(holder):
            return [
                _hello(3_600_000),  # 心跳间隔 1 小时 ⇒ 测试窗口内不发心跳，帧面干净
                _frame(ws_protocol.OP_DISPATCH, s=1, t=ws_protocol.EVENT_READY,
                       d={"session_id": "SESSION-1"}),
                # 未知事件：只为一帧带 `s=2` 的普通流量（推进 seq，不碰事件处理器）。
                _frame(ws_protocol.OP_DISPATCH, s=2, t="GROUP_FUTURE_EVENT", d={}),
                _frame(ws_protocol.OP_RECONNECT),
            ]

        def script2(holder):
            return [_hello(3_600_000), _stop_step(holder)]

        fakes = self.run_reader([script1, script2])
        self.assertEqual([f["op"] for f in fakes[0].sent], [2], "首跳 identify")
        (resume,) = fakes[1].sent
        self.assertEqual(resume["op"], ws_protocol.OP_RESUME)
        self.assertEqual(resume["d"]["session_id"], "SESSION-1")
        self.assertEqual(resume["d"]["seq"], 2, "seq = 断线前最后一条帧的 s")
        self.assertTrue(resume["d"]["token"].startswith("QQBot "))

    def test_op9_drops_the_session_and_next_connection_reidentifies(self):
        """op9：服务端不认会话 ⇒ **就地**清空 ⇒ 下一轮重新 identify（不拿废 session resume）。"""

        def script1(holder):
            return [
                _hello(3_600_000),
                _frame(ws_protocol.OP_DISPATCH, s=1, t=ws_protocol.EVENT_READY,
                       d={"session_id": "SESSION-9"}),
                _frame(ws_protocol.OP_INVALID_SESSION),
            ]

        def script2(holder):
            return [_hello(3_600_000), _stop_step(holder)]

        fakes = self.run_reader([script1, script2])
        self.assertEqual([f["op"] for f in fakes[0].sent], [2])
        (identify,) = fakes[1].sent
        self.assertEqual(
            identify["op"], ws_protocol.OP_IDENTIFY, "op9 丢弃会话 ⇒ 下一轮 identify"
        )

    def test_heartbeat_is_sent_while_frames_keep_arriving(self):
        """⭐ 评审 P2-1：recv 切小片 + 按间隔判发——**帧照来的同时**心跳照发。

        脚本把时钟拨过心跳间隔后**再**来一帧：处理完这一帧的同一轮循环就发出心跳
        （旧实现拿整周期当 recv 超时，持续来帧时永远走不到心跳分支）。
        """
        clock = [1000.0]

        def script(holder):
            return [
                _hello(45_000),
                lambda: clock.__setitem__(0, 1046),  # 距「上次心跳」46s ≥ 45s
                _frame(ws_protocol.OP_DISPATCH, s=6, t="GROUP_FUTURE_EVENT", d={}),
                _stop_step(holder),
            ]

        fakes = self.run_reader([script], clock=clock)
        self.assertEqual([f["op"] for f in fakes[0].sent], [2, 1])
        self.assertEqual(fakes[0].sent[1]["d"], 6, "心跳 d = 最新 seq")


class TokenManagerCacheKeyTests(TenantTestCase):
    """⭐ 评审 P3-4：`_TM_CACHE` 的键是 `app_id`（全局唯一），不是 `binding.pk`。"""

    def test_cache_is_keyed_by_app_id_not_pk(self):
        """binding 是**每租户单例**（所有租户 pk 恒 1）⇒ 按 pk 缓存在多租户进程里
        跨租户互踩、每次租户切换判 app_id 不等、每事件重建 TokenManager。"""
        events._TM_CACHE.clear()
        self.addCleanup(events._TM_CACHE.clear)
        binding = QQBotBinding.get_solo()
        binding.app_id = "APP-SHARED"
        binding.app_secret = "SECRET-1"
        binding.save()
        manager = events._token_manager(binding)
        # 「另一个租户」的同名机器人（pk 不同、app_id 相同）⇒ **同一份**缓存命中。
        other_tenant_row = QQBotBinding(app_id="APP-SHARED", app_secret="SECRET-1")
        self.assertIs(events._token_manager(other_tenant_row), manager)
        # app_id 不同 ⇒ 换一个 manager（旧条目不被串用）。
        binding.app_id = "APP-OTHER"
        self.assertIsNot(events._token_manager(binding), manager)
