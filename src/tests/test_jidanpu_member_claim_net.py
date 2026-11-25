"""下游鸡蛋铺线的**接单兜底网**（`jidanpu_member.claim_net`；快路游标护栏的兜底面）。

被钉住的契约：

- **选取集**：本绑定名下、链路还在对方大厅（`in_hall=True`）、我方仍 `WAITING_GRAB`、
  没软删的单；新上厅优先、旧单轮转，每轮不超过 `CLAIM_NET_BUDGET`；
- **接手零复制**：逐条调既有 `_handle_alarm` → `machine_claim_order` ⇒ 绩效 / 台账 / 审计 /
  竞态分流与快路**同形状**（钱路径唯一实现）；
- **结局聚合照 `_poll_log`**：`unreadable` ⇒ 中止本轮 + 健康列红；
  `refused`/`denied`/`unreadable_row` ⇒ loud 进 `last_error`、继续；
  台账幂等（`_note_order_once`）让同一结论连跑两轮不灌水；
- **闸门在调度层**（`_MEMBER_CLAIM_NET_AT`，节奏 180 秒），本文件不测闸门
  （在 `test_jidanpu_member_schedule.py` / `test_jidanpu_sync_outbox_switch.py`）。

⚠️ 夹具一律**合成值**（E-0077）、零真实网络：三个上游出口全被桩掉
（桩打在**被查找的那个模块** `apps.business.jidanpu` 上，E-0029）。
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_member
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

BASE_URL = "https://jidanpu-claim-net.test.invalid"
BINDING_USER = "member-net"
BINDING_USER_NICKNAME = "测试昵称打手网"
REMOTE_TAKER_UID = 5301
REMOTE_TAKER_NAME = "打手丙"

ORDER_NO = "20261104000000005301"
ORDER_NO_2 = "20261104000000005302"
ORDER_NO_3 = "20261104000000005303"
JID = "JDP-00000000000000005301"
JID_2 = "JDP-00000000000000005302"
JID_3 = "JDP-00000000000000005303"

KIND_REMOTE_TAKEN_LITERAL = "member.remote_taken"
KIND_WITHDRAW_LITERAL = "member.withdraw"
AUDIT_ACTION_LITERAL = "order.claim"


def _row_key_of(remote_id: str) -> str:
    return remote_id


def _taken(remote_id: str = JID) -> list[dict]:
    """`GetOrderAssLogs` 的「远端有人接了」回包（数值 `userId` 是唯一稳定来源）。"""
    return [{"userId": REMOTE_TAKER_UID, "userName": REMOTE_TAKER_NAME}]


class _Upstream:
    """`apps.business.jidanpu` 三个被兜底网查找的出口的脚本替身（照 fastpath 的形状）。

    ⚠️ `remote` 缺省回「已离开他家大厅、未退单」（远端 `orderSta=代练中`）——
    那是「可能真被接走」的形态；要造别的远端事实就显式给。
    """

    def __init__(self, *, remote=None, ass=None, remote_error=None):
        self.remote = dict(remote or {})
        self.ass = dict(ass or {})
        self.remote_error = remote_error
        self.calls: list[tuple[str, dict]] = []

    def names(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def fetch_order_by_key1(self, client, *, key, expect_field, expect_value):
        self.calls.append(("fetch_order_by_key1", {"key": key}))
        if isinstance(self.remote_error, Exception):
            raise self.remote_error
        if key in self.remote:
            value = self.remote[key]
            if isinstance(value, Exception):
                raise value
            return value
        return {"orderId": key, "orderSta": "代练中", "isThrowOrder": "N"}

    def fetch_ass_logs(self, client, *, remote_order_id):
        self.calls.append(("fetch_ass_logs", {"remote_order_id": remote_order_id}))
        got = self.ass.get(remote_order_id, [])
        if isinstance(got, Exception):
            raise got
        return tuple(got)


def _stub(up: _Upstream):
    """把替身接到**被查找的那个模块** `apps.business.jidanpu` 上（E-0029）。"""
    return (
        mock.patch.object(
            jidanpu, "fetch_system_logs",
            side_effect=AssertionError("兜底网不打日志快路那一跳"),
        ),
        mock.patch.object(
            jidanpu, "fetch_order_by_key1", side_effect=up.fetch_order_by_key1
        ),
        mock.patch.object(jidanpu, "fetch_ass_logs", side_effect=up.fetch_ass_logs),
    )


#: 注入的假客户端：桩都忽略它（真实出口拿到它会响亮地炸，正是要的）。
CLIENT = object()


class ClaimNetTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        positions = mock.patch.object(jidanpu_member, "_claim_net_positions", {})
        positions.start()
        self.addCleanup(positions.stop)
        self.now = timezone.now()
        self.game = Game.objects.create(name="合成游戏网")
        # ⭐ 带接单节点的代练员：自动接手走状态机 `(WAITING_GRAB, IN_PROGRESS)` 边。
        self.user_client = make_logged_in_client(
            username=BINDING_USER, groups=("代练员",), nodes=("grab_hall.take_order",)
        )
        self.user = User.objects.get(username=BINDING_USER)
        self.user.first_name = BINDING_USER_NICKNAME
        self.user.save(update_fields=["first_name"])
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-net",
            account_name="传令兵丙",
            password="pw-net",
            source_name="团团通系统",
            enabled=True,
        )
        jidanpu_member.LAST_CALL.reset()

    # ── 夹具工具 ─────────────────────────────────────────────────────────
    def _order(self, order_no: str, status: str = OrderStatus.WAITING_GRAB):
        order = Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=status,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目网",
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34")
        )
        return order

    def _link(self, order, remote_id: str, *, in_hall: bool = True,
              listed_at=None):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding,
            order=order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
            listed_at=listed_at,
        )

    def _run_net(self, up: _Upstream, *, now=None):
        stubs = _stub(up)
        with stubs[0], stubs[1], stubs[2]:
            return jidanpu_member.claim_net(self.binding, client=CLIENT, now=now or self.now)

    def _ledger(self, order_no, kind: str = KIND_REMOTE_TAKEN_LITERAL):
        return JidanpuSyncEvent.objects.filter(kind=kind, target_ref=order_no)

    def _audit(self, order_no):
        return OperationLog.objects.filter(order_no=order_no, action=AUDIT_ACTION_LITERAL)

    def _main_user_id(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id

    # ── 正面：远端已被接走 ⇒ 与快路同形状的自动接手 ─────────────────────

    def test_a_remote_taken_order_is_claimed_like_the_fast_path_does(self):
        """⭐ 远端「代练中」（不在厅、未退单）+ 我方 `WAITING_GRAB` ⇒ 走既有
        `machine_claim_order` 自动接手：订单 + MAIN 绩效行 + `member.remote_taken`
        `done` 台账 + 机器审计，一项不少（⛔ 不另抄第二份接手逻辑）。"""
        self._link(self._order(ORDER_NO), JID)
        up = _Upstream(ass={JID: _taken()})
        processed = self._run_net(up)
        self.assertEqual(processed, 1)
        # 逐条链路回了「远端单查 + 接单人日志」两跳
        self.assertEqual(up.names(), ["fetch_order_by_key1", "fetch_ass_logs"])

        self.order_refresh(ORDER_NO)
        perf = Performance.objects.get(order__order_no=ORDER_NO, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.user.pk)
        self.assertIsNotNone(perf.claimed_at)
        event = self._ledger(ORDER_NO).get()
        self.assertEqual(event.direction, "in")
        self.assertEqual(event.status, "done")
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)
        self.assertEqual(event.payload["remote_uid"], REMOTE_TAKER_UID)
        self.assertEqual(event.payload["remote_order_id"], JID)
        row = self._audit(ORDER_NO).get()
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.target_name, BINDING_USER_NICKNAME)
        # 健康面：干净的一轮 ⇒ `last_ok_at` 盖上、旧错清掉（与快路同一健康面）
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertEqual(self.binding.last_ok_at, self.now)
        # 「真跑过」自报（调度层出口池复位判据用）
        upstream_called, _blocked = jidanpu_member.LAST_CALL.take()
        self.assertTrue(upstream_called)

    def order_refresh(self, order_no):
        Order.objects.get(order_no=order_no).refresh_from_db()

    # ── 选取集判据 ───────────────────────────────────────────────────────

    def test_links_outside_the_selection_set_are_not_queried(self):
        """我方已不是 `WAITING_GRAB` / 链路已不在厅 / 单已软删 ⇒ 都不进选取集
        （⛔ 一次上游都不打：兜底网只兜「在途单」）。"""
        self._link(self._order(ORDER_NO, OrderStatus.IN_PROGRESS), JID)
        self._link(self._order(ORDER_NO_2), JID_2, in_hall=False)
        removed = self._order(ORDER_NO_3)
        self._link(removed, JID_3)
        Order.all_objects.filter(order_no=ORDER_NO_3).update(
            deleted_at=timezone.now()
        )
        up = _Upstream()
        processed = self._run_net(up)
        self.assertEqual(processed, 0)
        self.assertEqual(up.names(), [])

    def test_an_empty_selection_set_is_a_zero_upstream_early_return(self):
        """选取集为空 ⇒ 零上游、回 0，且**不动健康列**（一次空转不许清掉别路刚写的真错）。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            last_error="上一次的旧错", last_error_at=self.now - timedelta(minutes=5)
        )
        up = _Upstream()
        self.assertEqual(self._run_net(up), 0)
        self.assertEqual(up.names(), [])
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "上一次的旧错")
        self.assertIsNone(self.binding.last_ok_at)

    # ── 远端真实状态的三档「不接手」─────────────────────────────────────

    def test_a_remote_order_still_in_the_hall_is_a_silent_no_claim(self):
        """远端 `isThrowOrder=Y`（还在厅）⇒ 静默 `no_claim`：台账记一条、不接手；
        连跑两轮 ⇒ `_note_order_once` 去重，第二条台账不新增。"""
        self._link(self._order(ORDER_NO), JID)
        up = _Upstream(remote={JID: {"orderId": JID, "orderSta": "代练中", "isThrowOrder": "Y"}})
        self.assertEqual(self._run_net(up), 1)
        self.assertEqual(self._run_net(_Upstream(remote=up.remote)), 1)
        notes = self._ledger(ORDER_NO)
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.get().status, "skipped")
        self.assertIn("大厅", notes.get().reason)
        self.assertFalse(self._audit(ORDER_NO).exists())
        self.order_refresh(ORDER_NO)
        self.assertEqual(
            Order.objects.get(order_no=ORDER_NO).status, OrderStatus.WAITING_GRAB
        )

    def test_a_remote_refunded_order_is_noted_once_and_never_claimed(self):
        """远端已退单 ⇒ note 一次：⛔ 不接手、⛔ 不排退单（谁都没接手的单退掉对方副本
        = 事故形状）、连接单人日志那一跳都不打。"""
        self._link(self._order(ORDER_NO), JID)
        up = _Upstream(remote={JID: {"orderId": JID, "orderSta": "已退单", "isThrowOrder": "N"}},
                       ass={JID: _taken()})
        self.assertEqual(self._run_net(up), 1)
        note = self._ledger(ORDER_NO).get()
        self.assertEqual(note.status, "skipped")
        self.assertIn("已退单", note.reason)
        self.assertFalse(self._audit(ORDER_NO).exists())
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind=KIND_WITHDRAW_LITERAL).exists()
        )
        self.assertNotIn("fetch_ass_logs", up.names())

    # ── 响亮失败 ────────────────────────────────────────────────────────

    def test_an_unreadable_remote_state_aborts_the_round_and_turns_health_red(self):
        """回查抛 `JidanpuError` ⇒ **中止本轮**（后面的单本轮不再查）+ 健康列红，
        ⛔ 不把「读不到」当成「没被接走」。"""
        self._link(self._order(ORDER_NO), JID)
        self._link(self._order(ORDER_NO_2), JID_2)
        up = _Upstream(remote_error=jidanpu.ApiError("合成：上游 500", code=None))
        processed = self._run_net(up)
        self.assertEqual(processed, 0)
        # 第一张就读不到 ⇒ 第二张本轮不再查（中止）
        self.assertEqual(up.names().count("fetch_order_by_key1"), 1)
        self.assertEqual(up.calls[0][1]["key"], JID)
        self.assertFalse(self._ledger(ORDER_NO_2).exists())
        self.order_refresh(ORDER_NO_2)
        self.assertEqual(
            Order.objects.get(order_no=ORDER_NO_2).status, OrderStatus.WAITING_GRAB
        )
        self.binding.refresh_from_db()
        self.assertIn("读不到", self.binding.last_error)
        self.assertIsNotNone(self.binding.last_error_at)
        self.assertIsNone(self.binding.last_ok_at)

    # ── 预算 ────────────────────────────────────────────────────────────

    def test_older_links_rotate_to_the_tail_without_exceeding_the_budget(self):
        rows = []
        for index in range(55):
            order_no = f"20261104000000006{index:03d}"
            remote_id = f"JDP-00000000000000006{index:03d}"
            order = self._order(order_no)
            # 越新越靠后：`listed_at` 递增 ⇒ 最老优先
            listed = self.now - timedelta(minutes=100 - index)
            self._link(order, remote_id, listed_at=listed)
            rows.append((order_no, remote_id))
        up = _Upstream()
        processed = self._run_net(up)
        self.assertEqual(processed, jidanpu_member.CLAIM_NET_BUDGET)
        self.assertEqual(
            up.names().count("fetch_order_by_key1"), jidanpu_member.CLAIM_NET_BUDGET
        )
        # 查过的键 = `listed_at` 最老的 50 张；最新那张（index=54）**不在**其中
        queried = {
            kwargs["key"] for name, kwargs in up.calls if name == "fetch_order_by_key1"
        }
        self.assertNotIn(rows[54][1], queried)
        self.assertIn(rows[0][1], queried)
        self.assertEqual(len(queried), jidanpu_member.CLAIM_NET_BUDGET)

        later = _Upstream(ass={rows[54][1]: _taken()})
        self.assertEqual(self._run_net(later, now=self.now + timedelta(minutes=3)), 5)
        self.assertEqual(
            [kwargs["key"] for name, kwargs in later.calls if name == "fetch_order_by_key1"],
            [remote_id for _, remote_id in rows[50:]],
        )
        order = Order.objects.get(order_no=rows[54][0])
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(order), self.binding.user_id)

    def test_recent_admin_assignment_is_found_without_a_system_log(self):
        recent_order = self._order(ORDER_NO)
        self._link(recent_order, JID, listed_at=self.now - timedelta(minutes=17))
        for order_no, remote_id in ((ORDER_NO_2, JID_2), (ORDER_NO_3, JID_3)):
            self._link(
                self._order(order_no), remote_id, listed_at=self.now - timedelta(days=1),
            )
        up = _Upstream(
            remote={JID: {
                "orderId": JID, "orderSta": "代练中", "isThrowOrder": "N",
                "recManName": REMOTE_TAKER_NAME, "assTime": jidanpu.format_log_time(self.now),
            }},
            ass={JID: _taken()},
        )

        with mock.patch.object(jidanpu_member, "CLAIM_NET_BUDGET", 2):
            self.assertEqual(self._run_net(up), 2)
            next_round = _Upstream()
            self.assertEqual(self._run_net(next_round), 1)
        self.assertEqual(
            [kwargs["key"] for name, kwargs in up.calls if name == "fetch_order_by_key1"],
            [JID, JID_2],
        )
        self.assertEqual(
            [kwargs["key"] for name, kwargs in next_round.calls if name == "fetch_order_by_key1"],
            [JID_3],
        )
        recent_order.refresh_from_db()
        self.assertEqual(recent_order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(recent_order), self.binding.user_id)
        self.assertEqual(self._ledger(ORDER_NO).filter(status="done").count(), 1)

    def test_a_failing_recent_lookup_does_not_block_other_recent_or_old_links(self):
        newest = self._order(ORDER_NO)
        self._link(newest, JID, listed_at=self.now - timedelta(minutes=1))
        self._link(self._order(ORDER_NO_2), JID_2, listed_at=self.now - timedelta(minutes=2))
        self._link(self._order(ORDER_NO_3), JID_3, listed_at=self.now - timedelta(days=1))
        remote = {JID: jidanpu.ApiError("合成：单查失败", code=None)}
        with mock.patch.object(jidanpu_member, "CLAIM_NET_BUDGET", 4):
            self.assertEqual(self._run_net(_Upstream(remote=remote)), 0)
            later = _Upstream(remote=remote, ass={JID_2: _taken(), JID_3: _taken()})
            self.assertEqual(self._run_net(later), 2)
        queried = [kwargs["key"] for name, kwargs in later.calls if name == "fetch_order_by_key1"]
        self.assertEqual(queried, [JID_3, JID_2, JID])
        self.assertEqual(self._ledger(ORDER_NO_2).filter(status="done").count(), 1)
        self.assertEqual(self._ledger(ORDER_NO_3).filter(status="done").count(), 1)

    def test_off_hall_unassigned_and_stale_assignment_logs_do_not_claim(self):
        listed_at = self.now - timedelta(days=1)
        orders = [self._order(ORDER_NO), self._order(ORDER_NO_2)]
        self._link(orders[0], JID, listed_at=listed_at)
        self._link(orders[1], JID_2, listed_at=listed_at)
        up = _Upstream(
            remote={
                JID: {"orderId": JID, "orderSta": "未分配", "isThrowOrder": "N"},
                JID_2: {
                    "orderId": JID_2, "orderSta": "代练中", "isThrowOrder": "N",
                    "recManName": REMOTE_TAKER_NAME,
                    "assTime": jidanpu.format_log_time(listed_at - timedelta(minutes=1)),
                },
            },
            ass={JID: _taken(), JID_2: _taken()},
        )
        self.assertEqual(self._run_net(up), 2)
        self.assertNotIn("fetch_ass_logs", up.names())
        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
            self.assertIsNone(self._main_user_id(order))

    # ── 停用绑定 ────────────────────────────────────────────────────────

    def test_a_disabled_binding_makes_zero_upstream_calls(self):
        """spec §8.4：停用 ⇒ 一次上游都不打、回 0（连选取集之外的任何读都不做）。"""
        self._link(self._order(ORDER_NO), JID)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.binding.refresh_from_db()
        up = _Upstream(ass={JID: _taken()})
        self.assertEqual(self._run_net(up), 0)
        self.assertEqual(up.names(), [])
