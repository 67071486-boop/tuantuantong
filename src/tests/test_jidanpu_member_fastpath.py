"""下游鸡蛋铺线的**日志快路 + 自动接手**（Task 7；spec §2.2 / §6.1 / §6.3 / §8.2 / §8.3）。

被钉住的契约：

- `jidanpu.fetch_system_logs`：`GET /SystemAjax/GetSysmteLogList`，`stime` / `etime` 是
  `yyyy-MM-dd HH:mm:ss`（**带时分秒**）、`logType=10` 订单相关；载荷形状防御走 `_as_rows`；
- `poll_log`：用 `binding.log_cursor` 当游标增量拉；去重键 `(createTime, joinOrderId, remark)`
  （**同一秒可以有多行** ⇒ 只按时间当游标会漏）；只处理「我方单号能对上本线链路」的行
  （`soureOrderId` = 我方 `order_no`，或 `joinOrderId` = 链路的远端单号）；处理完把游标推到
  本次窗口里最大的 `createTime`（空窗口/没有更新的行 ⇒ 推到 `now − 落库延迟护栏`，缺省 60 秒、
  env `JIDANPU_MEMBER_LOG_CURSOR_LAG_SECONDS` 可调、`0` = 推到 `now` 的旧行为）；
- **回声抑制一道**（spec §6.1）：操作人 = 本绑定的 `account_name` ⇒ 跳过；
  E-0292）删掉了时间窗：窗口内对方的接单也会被吞；现在只按操作人跳过。
- **日志只当闹钟**（A9）：命中一行 ⇒ `fetch_ass_logs` 回查「远端谁接了」（数值 `userId` 是
  唯一稳定来源），⛔ **不解析 `remark`**（同一件事有多种渠道文案 ⇒ 换文案行为必须不变）；
- **自动接手**（A6）：走 `claim_order`（唯一状态机入口 `apply_transition`，带 `nodes_of` 的节点门），
  成功后写**订单审计**（`OperationLog`，操作人 = 机器、人话写明「由下家鸡蛋铺接单自动接手」）
  与台账 `member.remote_taken`（**同一个事务**，照 `jidanpu_hall.discover_remote_taken` 的形状）；
- **钱在我方算**（spec §8.2，与旧线**不同**）：断言**绩效行**（MAIN 行的 `user` = 该代练员）
  与**订单状态**，⛔ 不只断言台账行；
- **竞态（A10）**：`claim_order` 抛「已被别人接走」且接手人**不是**本绑定的主人 ⇒ 排
  `member.withdraw`（原因沿用既有那句 `jidanpu_hall.WITHDRAW_REMARK`）；接手人**就是**他 ⇒ 不排；
- **失败一律响亮**：读不到日志 / 读不到远端接单人 / 缺节点 ⇒ 台账 + `last_error`
  （收窄写入，⛔ 不整行 `save()`）+ **不推进游标**（下一轮重试）。

⚠️ 断言一律打**数据库行**与**桩收到的载荷**（⛔ 不断言 logger）；桩打在**被查找的模块**
`apps.business.jidanpu` 上（E-0029）；⛔ 任何用例都**不**断言 `remark` 文本（A9），
只有「换文案 ⇒ 行为不变」这一条**故意**用两种文案做对照。
⚠️ 夹具一律**合成值**（E-0077）、零真实网络。
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.audit.models import LogType, OperationLog
from apps.business import claim, hall_shadow, jidanpu, jidanpu_common, jidanpu_member, peer_inbound
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderSource,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token, source_name_for
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

BASE_URL = "https://jidanpu-a.test.invalid"
BINDING_USER = "member-a"
#: ⭐ 订单日志人名线 spec §3-A2：绑定代练员账号的**昵称**。
#: ⚠️ 与 `BINDING_USER` **不同形**（spec §5 的夹具纪律）：两者相同时
#: 「自动接手的 `target_name` 走的是昵称还是账号」这条判据会**假绿**。
BINDING_USER_NICKNAME = "测试昵称打手庚"
BINDING_DISPLAY_NAME = "传令兵甲"
OTHER_OPERATOR = "别人的操作人"

ORDER_NO = "20260926000000003001"
ORDER_NO_2 = "20260926000000003002"
JID = "JDP-00000000000000003001"
JID_2 = "JDP-00000000000000003002"
FOREIGN_ORDER_NO = "20260926999999999999"
FOREIGN_JID = "JDP-00000000000000999999"

REMOTE_TAKER_UID = 5001
REMOTE_TAKER_NAME = "打手甲"

# ── 契约字面值（E-0024：钉值不钉常量）───────────────────────────────────────────

LOG_PATH_LITERAL = "/SystemAjax/GetSysmteLogList"
KIND_REMOTE_TAKEN_LITERAL = "member.remote_taken"
KIND_WITHDRAW_LITERAL = "member.withdraw"
AUDIT_ACTION_LITERAL = "order.claim"
AUDIT_ACTOR_LITERAL = "下家鸡蛋铺同步"
WITHDRAW_REMARK_LITERAL = "订单已被发单方打手接取"

#: spec §8.3 要求审计里必须有的那句人话（机器动作必须一眼看出来）。

#: A9：同一件事在对方日志里有**多种渠道写法**（实测三种）⇒ 换文案行为必须不变。
REMARK_WEB = "【打手甲】通过接单大厅(内)自助接手此单(系统网页)，接手价：1.00"
REMARK_DOWNSTREAM = "【打手甲】通过接单大厅(内)自助接手此单(下家系统)，接手价：1.00"


def _log_time(moment) -> str:
    """测试侧独立的时间写法（⛔ 不从被测模块的格式常量派生）。"""
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M:%S")


def _row(
    *,
    create_time: str,
    join: str = JID,
    source: str = ORDER_NO,
    user: str = OTHER_OPERATOR,
    remark: str = REMARK_WEB,
) -> dict:
    """一行系统操作日志（形状照 spec §2.2 的**实测**语料；全是合成值）。"""
    return {
        "createTime": create_time,
        "joinOrderId": join,
        "soureOrderId": source,
        "userName": user,
        "remark": remark,
        "logType": 10,
        "gameName": "合成游戏甲",
        "gameRoleName": "合成角色甲",
    }


def _taken(remote_id: str = JID) -> list[dict]:
    """`GetOrderAssLogs` 的「远端有人接了」回包（数值 `userId` 是唯一稳定来源）。"""
    return [{"userId": REMOTE_TAKER_UID, "userName": REMOTE_TAKER_NAME}]


class _Upstream:
    """`apps.business.jidanpu` 那三个被快路查找的出口的脚本替身。

    ⚠️ 只替**出口**（`fetch_system_logs` / `fetch_order_by_key1` / `fetch_ass_logs`）；
    `poll_log` 自己的逻辑一行不替。
    ⚠️ `remote`（远端单查）**缺省**回一行「已离开他家大厅、未退单」的单（`isThrowOrder="N"`）
    ——那是「可能真被接走」的形态 ⇒ 既有用例照旧走到接单人那一步；要造远端真实状态就显式给。
    """

    def __init__(self, *, logs=None, ass=None, log_error=None, remote=None):
        self.logs = list(logs or [])
        self.ass = dict(ass or {})
        self.log_error = log_error
        self.remote = dict(remote or {})
        self.calls: list[tuple[str, dict]] = []

    def names(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def fetch_system_logs(self, client, *, stime, log_type=10, etime=None):
        self.calls.append((
            "fetch_system_logs",
            {"stime": stime, "log_type": log_type, "etime": etime},
        ))
        if isinstance(self.log_error, Exception):
            raise self.log_error
        return tuple(self.logs)

    def fetch_order_by_key1(self, client, *, key, expect_field, expect_value):
        self.calls.append(("fetch_order_by_key1", {
            "key": key, "expect_field": expect_field, "expect_value": expect_value,
        }))
        if key in self.remote:
            return self.remote[key]
        return {"orderId": key, "orderSta": "待上号", "isThrowOrder": "N"}

    def fetch_ass_logs(self, client, *, remote_order_id):
        self.calls.append(("fetch_ass_logs", {"remote_order_id": remote_order_id}))
        got = self.ass.get(remote_order_id, [])
        if isinstance(got, Exception):
            raise got
        return tuple(got)


@contextmanager
def _stub(up: _Upstream):
    """把替身接到**被查找的模块** `apps.business.jidanpu` 上（E-0029）。"""
    with (
        mock.patch.object(jidanpu, "fetch_system_logs", side_effect=up.fetch_system_logs),
        mock.patch.object(
            jidanpu, "fetch_order_by_key1", side_effect=up.fetch_order_by_key1
        ),
        mock.patch.object(jidanpu, "fetch_ass_logs", side_effect=up.fetch_ass_logs),
    ):
        yield up


#: 注入的假客户端：三个桩都忽略它（真实出口拿到它会响亮地炸，正是要的）。
CLIENT = object()


class _Base(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.game = Game.objects.create(name="合成游戏甲")
        # ⚠️ 用一个**带 `grab_hall.take_order` 节点**的代练员：自动接手走的是状态机的
        #    `(WAITING_GRAB, IN_PROGRESS)` 边，缺这个节点会被拒（另有一条缺节点的用例）。
        #    夹具把 client 留下来：反向用例要走**人类入口**（真 HTTP `/claim/`）。
        self.user_client = make_logged_in_client(
            username=BINDING_USER, groups=("代练员",), nodes=("grab_hall.take_order",)
        )
        self.user = User.objects.get(username=BINDING_USER)
        # ⭐ spec §3-A2）：给代练员一个**与登录账号不同形**的昵称
        #    ⇒ 「自动接手的 `target_name` 是账号名称」这条判据才有判别力。
        self.user.first_name = BINDING_USER_NICKNAME
        self.user.save(update_fields=["first_name"])
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-a",
            account_name=BINDING_DISPLAY_NAME,
            password="pw-a",
            source_name="团团通系统",
            enabled=True,
        )
        self.order = self._order()

    # ── 夹具工具 ─────────────────────────────────────────────────────────
    def _order(self, *, order_no: str = ORDER_NO, status: str = OrderStatus.WAITING_GRAB):
        order = Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=status,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目甲",
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34")
        )
        return order

    def _link(self, *, order=None, remote_id: str = JID, in_hall: bool = True,
              last_out_at=None):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding,
            order=order or self.order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
            last_out_at=last_out_at,
        )

    def _main_user_id(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id

    def _blocking_limit(self, *, user=None):
        """给这个人配一个**会挡住人类**的按人限制：接单价区间不含本单（12.34）。

        ⚠️ 这是「人类入口必拒」的靶子；机器接手（裁定 8）必须**无视**它。
        """
        from apps.accounts.models import StaffProfile

        profile, _ = StaffProfile.objects.get_or_create(user=user or self.user)
        profile.grab_price_min = Decimal("100.00")
        profile.grab_price_max = Decimal("200.00")
        profile.save(update_fields=["grab_price_min", "grab_price_max"])
        return profile

    def _ledger(self, order_no=ORDER_NO, kind: str = KIND_REMOTE_TAKEN_LITERAL):
        return JidanpuSyncEvent.objects.filter(kind=kind, target_ref=order_no)

    def _audit(self, order_no=ORDER_NO):
        return OperationLog.objects.filter(order_no=order_no, action=AUDIT_ACTION_LITERAL)


# ══ ① fetch_system_logs（原语契约）═══════════════════════════════════════════


class FetchSystemLogsTests(SimpleTestCase):
    """日志端点原语：路径 / 参数格式 / 形状防御（spec §2.2 实测）。"""

    def test_the_path_and_the_order_log_type_are_the_measured_values(self):
        # 端点拼写照实测（`GetSysmteLogList` 的拼写是**对标自己的**，⛔ 不许"修正"）。
        self.assertEqual(jidanpu.SYSTEM_LOG_PATH, LOG_PATH_LITERAL)
        self.assertEqual(jidanpu.SYSTEM_LOG_ORDER_TYPE, 10)

    def test_it_calls_get_with_the_time_format_with_seconds(self):
        client = mock.Mock()
        client.call.return_value = []
        out = jidanpu.fetch_system_logs(
            client, stime="2026-09-26 10:00:00", etime="2026-09-26 10:00:03"
        )
        client.call.assert_called_once_with(
            "GET",
            LOG_PATH_LITERAL,
            query={
                "stime": "2026-09-26 10:00:00",
                "etime": "2026-09-26 10:00:03",
                "logType": 10,
            },
        )
        self.assertEqual(out, ())

    def test_it_fills_etime_from_the_clock_when_the_caller_does_not_give_one(self):
        client = mock.Mock()
        client.call.return_value = []
        jidanpu.fetch_system_logs(client, stime="2026-09-26 10:00:00")
        etime = client.call.call_args.kwargs["query"]["etime"]
        # 形状：`yyyy-MM-dd HH:mm:ss`（**带时分秒**）——⛔ 别写成只吃日期的那种
        # （同产品的另一个日志端点才是纯日期，混用会静默取不到）。
        self.assertRegex(etime, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_a_non_list_payload_is_loud(self):
        client = mock.Mock()
        client.call.return_value = {"rows": []}
        with self.assertRaises(ApiError):
            jidanpu.fetch_system_logs(client, stime="2026-09-26 10:00:00")


# ══ ② 游标推进 + 去重 + 只留本线单 ═══════════════════════════════════════════


class CursorAndDedupeTests(_Base):
    def test_the_dedupe_keys_are_the_log_row_fields(self):
        self.assertEqual(
            jidanpu_member.LOG_DEDUPE_KEYS, ("createTime", "joinOrderId", "remark")
        )

    def test_the_first_poll_starts_at_the_binding_creation_time(self):
        """`log_cursor` 空 ⇒ `stime` = 绑定时刻（⛔ 不去扫绑定之前的历史）。"""
        up = _Upstream(logs=[])
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)
        stime = dict(up.calls)["fetch_system_logs"]["stime"]
        self.assertEqual(stime, _log_time(self.binding.created_at))
        self.assertRegex(stime, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_the_cursor_advances_to_the_largest_create_time_of_the_window(self):
        self._link()
        early = _log_time(self.now - timedelta(seconds=6))
        late = _log_time(self.now - timedelta(seconds=4))
        up = _Upstream(
            logs=[
                _row(create_time=early),
                _row(create_time=late, remark=REMARK_DOWNSTREAM),
            ]
        )
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, late)

    def test_the_cursor_advances_to_now_on_an_empty_window(self):
        """空窗 ⇒ 游标推到 `now − 落库延迟护栏`（缺省 60 秒）：上游把行放进日志流有延迟
        （实测接单行晚几秒才回），推到 `now` 会把还没出现的行永远甩在游标后面。
        （首轮游标=绑定时刻、比护栏窗还新 ⇒ 钳住不倒退；跑到护栏窗之外才推到 `now−60`。）"""
        self._link()
        poll_now = self.now + timedelta(seconds=120)  # 绑定时刻已在护栏窗外
        up = _Upstream(logs=[])
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=poll_now)
        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.log_cursor, _log_time(poll_now - timedelta(seconds=60))
        )

    def test_the_guardrail_env_zero_restores_the_old_push_to_now(self):
        """护栏 env=0 ⇒ 旧行为（空窗推到 `now`）：发版风险的逃生口必须真的通。"""
        self._link()
        poll_now = self.now + timedelta(seconds=120)
        up = _Upstream(logs=[])
        with mock.patch.dict(
            os.environ, {jidanpu_member.LOG_CURSOR_LAG_SECONDS_ENV: "0"}
        ):
            with _stub(up):
                jidanpu_member.poll_log(self.binding, now=poll_now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, _log_time(poll_now))

    def test_a_max_equal_to_the_cursor_round_pushes_to_now_minus_the_guardrail(self):
        """「这一轮没有比游标更新的行」（最大 `createTime` == 游标）与空窗同理 ⇒ 推到
        `now − 护栏`；护栏结果早于旧游标时**钳住不倒退**（窗口不发散、游标恒 ≤ now）。"""
        self._link()
        boundary = _log_time(self.now - timedelta(seconds=2))
        row = _row(create_time=boundary)
        first = _Upstream(logs=[row], ass={JID: []})
        with _stub(first):
            jidanpu_member.poll_log(self.binding, now=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, boundary)

        # 第二轮同一行回来（`stime` 含界）：没有更新的行 ⇒ 候选 `now−60` 早于旧游标 ⇒
        # 钳在旧游标上（⛔ 不倒退）；游标仍 ≤ `now`（窗口有界：≤ 护栏 + 轮询间隔）。
        second_now = self.now + timedelta(seconds=3)
        second = _Upstream(logs=[row], ass={JID: []})
        with _stub(second):
            jidanpu_member.poll_log(self.binding, now=second_now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, boundary)
        self.assertLess(self.binding.log_cursor, _log_time(second_now))

        # 护栏窗滑过之后（`now ≥ boundary+60`）游标照常前进到 `now−60`：重拉是**有界**的，
        # ⛔ 不是把游标永久钉在那一秒上。
        late_now = self.now + timedelta(seconds=61)
        third = _Upstream(logs=[row], ass={JID: []})
        with _stub(third):
            jidanpu_member.poll_log(self.binding, now=late_now)
        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.log_cursor, _log_time(late_now - timedelta(seconds=60))
        )

    def test_a_claim_row_inside_the_guardrail_window_is_caught_on_the_next_round(self):
        """⭐ E-0317 现场形状：第 1 轮空窗（游标推到 `poll_now−60`）→ 上游把接单行**晚几秒**才
        放进日志流、`createTime` 落在护栏窗内 ⇒ 第 2 轮被拉回、匹配并**自动接手**
        （旧行为把游标推到 `now` ⇒ 该行永不再回、永远停在待抢单）。"""
        self._link()
        poll_now = self.now + timedelta(seconds=120)  # 绑定时刻已在护栏窗外
        with _stub(_Upstream(logs=[])):
            jidanpu_member.poll_log(self.binding, now=poll_now)
        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.log_cursor, _log_time(poll_now - timedelta(seconds=60))
        )

        # 接单行的 `createTime` 在第 1 轮拉取**之后**才落库，但仍落在护栏窗内。
        late_row_time = poll_now - timedelta(seconds=4)
        up = _Upstream(
            logs=[_row(create_time=_log_time(late_row_time))],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(
                self.binding, now=poll_now + timedelta(seconds=6)
            )
        self.assertEqual(processed, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), self.user.pk)

    def test_a_malformed_create_time_skips_only_that_row_and_advances_the_cursor(self):
        """⭐ 行级隔离：一行 `createTime` 畸形 ⇒ **只跳过这一行**，后续行照常处理、游标照推。

        判别力（把 `_poll_log` 那一支改回 `return _fail_poll(...)`）：游标停在原值、
        后续行那张单永远不会被接手 ⇒ ①②③ 三条断言同时红。
        """
        self._link()
        broken = _row(create_time="2026-09-26T12:00:00")  # ⛔ 不是 yyyy-MM-dd HH:mm:ss
        later = _log_time(self.now - timedelta(seconds=4))
        up = _Upstream(
            logs=[broken, _row(create_time=later)],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)

        # ① 后续行**仍被处理**：那张单真的被接手了（坏行没有停摆这一轮）
        self.assertEqual(processed, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        # ② 游标推进到**本批可解析的最大** createTime（不是停在坏行、也不是退到 now）
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, later)
        # ③ 坏行**恰一条**人话台账（点名哪个键坏了、是哪一行）
        notes = JidanpuSyncEvent.objects.filter(
            direction="in", target_type="binding", target_ref=str(self.binding.pk)
        )
        self.assertEqual(notes.count(), 1)
        note = notes.get()
        self.assertEqual(note.status, "skipped")
        self.assertIn("createTime", note.reason)
        self.assertIn(f"joinOrderId={JID}", note.reason)

    def test_a_malformed_row_is_not_recorded_twice_on_the_next_round(self):
        """⭐ 坏行的去重：下一轮又拿到同一行 ⇒ **不再重复记**（键 = 那一行的人话原因）。"""
        self._link()
        broken = _row(create_time="not-a-time-at-all")
        notes = JidanpuSyncEvent.objects.filter(
            direction="in", target_type="binding", target_ref=str(self.binding.pk)
        )
        with _stub(_Upstream(logs=[broken])):
            jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(notes.count(), 1)
        # 本批**一行都解析不了** ⇒ 游标按护栏分支推进（首轮候选 `now−60` 早于旧游标
        # （绑定时刻）⇒ 钳住不倒退：停在旧游标上，下一轮从它重拉）
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, _log_time(self.binding.created_at))
        with _stub(_Upstream(logs=[broken])):
            jidanpu_member.poll_log(self.binding, now=self.now + timedelta(seconds=3))
        self.assertEqual(notes.count(), 1)

    def test_two_bindings_each_get_their_own_note_for_the_same_order(self):
        """⭐ 台账归属：同址、范围重叠的两个绑定各跑一轮 ⇒ **各自**一行，且各指自己。

        判别力：`_note_order_once` 的去重键漏掉 `payload__member_binding_id` ⇒ 第二条被
        第一条挡住 ⇒ 本用例只看到 1 行，而且它记在**别的绑定**名下（留痕认错人）。
        """
        self._link()
        other_user = User.objects.create_user(
            username="member-fastpath-b", password="pw-a"
        )
        other = JidanpuMemberBinding.objects.create(
            user=other_user,
            jidanpu_base_url=BASE_URL,   # 同址
            account="acc-b",
            account_name=BINDING_DISPLAY_NAME,
            password="pw-a",
            source_name="团团通系统",
            enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=other, order=self.order, jidanpu_order_id=JID, in_hall=True
        )
        remote = {JID: {"orderId": JID, "orderSta": "待上号", "isThrowOrder": "Y"}}
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            remote=remote,
        )
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)
            jidanpu_member.poll_log(other, now=self.now)

        rows = self._ledger()
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {row.payload["member_binding_id"] for row in rows},
            {self.binding.pk, other.pk},
        )

    def test_the_next_poll_uses_the_advanced_cursor(self):
        self._link()
        later = self.now + timedelta(seconds=3)
        first = _Upstream(logs=[_row(create_time=_log_time(later))])
        with _stub(first):
            jidanpu_member.poll_log(self.binding, now=later)
        second = _Upstream(logs=[])
        with _stub(second):
            jidanpu_member.poll_log(self.binding, now=later + timedelta(seconds=3))
        self.assertEqual(
            dict(second.calls)["fetch_system_logs"]["stime"], _log_time(later)
        )

    def test_a_boundary_second_that_never_advances_does_not_hold_the_cursor_forever(self):
        """护栏 env=0 的旧行为：游标停在某一秒时，**下一轮必须推过它**。

        否则 `stime`（含界）每轮都把那一秒整段拉回来 ⇒ 窗口一轮比一轮长。
        （护栏开着时该分支推到 `now−60`、被钳制钉在旧游标上，见
        `test_a_max_equal_to_the_cursor_round_pushes_to_now_minus_the_guardrail`。）
        """
        self._link()
        boundary = _log_time(self.now - timedelta(seconds=2))
        row = _row(create_time=boundary)
        first = _Upstream(logs=[row], ass={JID: []})
        with mock.patch.dict(
            os.environ, {jidanpu_member.LOG_CURSOR_LAG_SECONDS_ENV: "0"}
        ):
            with _stub(first):
                jidanpu_member.poll_log(self.binding, now=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, boundary)

        second_now = self.now + timedelta(seconds=3)
        # 第二轮上游把**同一行**又回了一次（`stime` 含界，真实形态就是这样）
        second = _Upstream(logs=[row], ass={JID: []})
        with mock.patch.dict(
            os.environ, {jidanpu_member.LOG_CURSOR_LAG_SECONDS_ENV: "0"}
        ):
            with _stub(second):
                jidanpu_member.poll_log(self.binding, now=second_now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, _log_time(second_now))

    def test_rows_in_the_same_second_are_all_processed(self):
        """⭐ 同一秒可以有多行 ⇒ 只按时间当游标会漏（spec §2.2）。"""
        self._link()
        order_2 = self._order(order_no=ORDER_NO_2)
        self._link(order=order_2, remote_id=JID_2)
        same_second = _log_time(self.now - timedelta(seconds=2))
        up = _Upstream(
            logs=[
                _row(create_time=same_second),
                _row(create_time=same_second, join=JID_2, source=ORDER_NO_2),
            ],
            ass={JID: _taken(JID), JID_2: _taken(JID_2)},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 2)
        self.assertEqual(
            up.names().count("fetch_ass_logs"), 2
        )
        for order in (self.order, order_2):
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, same_second)

    def test_an_identical_row_twice_is_processed_once(self):
        """去重键命中 ⇒ **同一行只算一次**（这里的远端还没接，去重是唯一的挡板：

        没有它就会回查两次上游、把「处理了几行」算成 2）。
        """
        self._link()
        t = _log_time(self.now - timedelta(seconds=2))
        row = _row(create_time=t)
        up = _Upstream(logs=[row, dict(row)], ass={JID: []})
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        self.assertEqual(up.names().count("fetch_ass_logs"), 1)
        self.assertEqual(self._ledger().count(), 1)

    def test_a_row_that_does_not_match_this_lines_chain_is_left_alone(self):
        """⛔ 不是本线的单 ⇒ 一次上游都不打、一行台账都不写。"""
        self._link()
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join=FOREIGN_JID,
                    source=FOREIGN_ORDER_NO,
                )
            ]
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        self.assertEqual(up.names(), ["fetch_system_logs"])
        self.assertFalse(JidanpuSyncEvent.objects.exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)


# ══ ②b 日志行按**有效来源单号**匹配（业务方）══════════════════════
#
# 业务方要求「来源单号要尽量一致」⇒ 我方写进对方 `souresOrderID` 的是**有效来源单号**
# （`source_order_no` 优先、空白才回落 `order_no`），所以日志行里的 `soureOrderId`
# 也必须按**同一个值**去认——⛔ 不许这里再用内置单号当第二把尺子。

#: 合成来源单号（形状贴近真实：上游给的第三方单号）。
SOURCE_ORDER_NO = "SYNTH-SRC-2026-000031"


class SourceOrderNoMatchingTests(_Base):
    def test_a_row_matches_by_our_source_order_no(self):
        """`soureOrderId` = 我方**来源单号**（不是内置单号）⇒ 命中本线链路并处理。"""
        Order.objects.filter(pk=self.order.pk).update(source_order_no=SOURCE_ORDER_NO)
        self.order.refresh_from_db()
        self._link()
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join="",
                    source=SOURCE_ORDER_NO,
                )
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        self.assertEqual(up.names().count("fetch_ass_logs"), 1)

    def test_the_builtin_order_no_no_longer_matches_once_a_source_order_no_exists(self):
        """来源单号非空 ⇒ 日志行里的**内置单号**不再当匹配键（⛔ 不许两把尺子）。

        判别力：`_link_index` 仍按 `order.order_no` 建索引 ⇒ 本用例红（那一行会被当成
        本线单去回查，processed 变成 1）。
        """
        Order.objects.filter(pk=self.order.pk).update(source_order_no=SOURCE_ORDER_NO)
        self.order.refresh_from_db()
        self._link()
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join="",
                    source=ORDER_NO,
                )
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        self.assertEqual(up.names(), ["fetch_system_logs"])

    def test_two_orders_with_the_same_source_order_no_are_recorded_as_a_residual(self):
        """两张我方单来源单号相同 ⇒ **如实登记成残差**（⛔ 不自己拍板复杂策略）。

        判据两条：
        ① 日志快路仍**确定性**地认**最早那条**链路（按 `id`，不随查询顺序漂移）；
        ② 绑定上留一条**人话台账**（⛔ 不静默吞掉这个形态）。
        """
        other = self._order(order_no=ORDER_NO_2)
        for row in (self.order, other):
            Order.objects.filter(pk=row.pk).update(source_order_no=SOURCE_ORDER_NO)
            row.refresh_from_db()
        first = self._link(remote_id=JID)
        second = self._link(order=other, remote_id=JID_2)
        self.assertLess(first.pk, second.pk)

        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join="",
                    source=SOURCE_ORDER_NO,
                )
            ],
            ass={JID: _taken(), JID_2: _taken(JID_2)},
        )
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)

        # ① 认的是 `id` 更小的那条链路（两个远端单号不同 ⇒ 从调用参数上分得出来）
        self.assertEqual(
            [kw for name, kw in up.calls if name == "fetch_ass_logs"][0][
                "remote_order_id"
            ],
            JID,
        )
        # ② 残差留痕：绑定级台账，人话里点明「来源单号」这件事
        notes = JidanpuSyncEvent.objects.filter(
            direction="in", target_type="binding", target_ref=str(self.binding.pk)
        )
        self.assertEqual(notes.count(), 1, list(notes.values_list("reason", flat=True)))
        self.assertIn("来源单号", notes.get().reason)
        self.assertEqual(notes.get().status, "failed")


# ══ ②c 链路索引改「按本轮键点查」（性能修复）══════════════════════
#
# 实测缺陷：快路每一轮（空轮也算）先全量拉整条绑定的链路+订单建两张映射表，而映射表每轮
# 只被点查几下 ⇒ 生产实测链路表 22 次全表扫/分、订单表连带 20 次/分（EXPLAIN cost 11069）。
# 改后：先收集本轮日志行的单号键，键集都空 ⇒ 零查询；忙轮远端臂走唯一索引点查。
# ⛔ 语义不许变：撞号取最早链路 + 残差台账照旧（本节用例各钉一面，含软删单那一面）。

#: 20 字以内的合成来源单号（`Order.order_no` 列宽 20 ⇒ 回落臂造行要塞得进去）。
SHORT_SOURCE_NO = "SYNTH-SRC-31"


class LinkIndexPointQueryTests(_Base):
    def _select_sql(self, captured, fragment: str) -> list[str]:
        """本轮对含 `fragment` 的表发过的 SELECT（`captured` = CaptureQueriesContext）。"""
        return [
            entry["sql"]
            for entry in captured.captured_queries
            if fragment in entry["sql"] and entry["sql"].lstrip().startswith("SELECT")
        ]

    def test_an_empty_round_queries_neither_links_nor_orders(self):
        """空轮（没有日志行）⇒ 链路表与订单表**一条 SQL 都不发**（旧行为：每 3 秒全量拉两张表建索引）。"""
        up = _Upstream(logs=[])
        with CaptureQueriesContext(connection) as captured:
            with _stub(up):
                processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        self.assertEqual(
            self._select_sql(captured, "business_jidanpumemberorderlink"),
            [],
            "空轮不该查链路表",
        )
        self.assertEqual(
            self._select_sql(captured, 'FROM "business_order"'),
            [],
            "空轮不该查订单表",
        )

    def test_a_round_of_keyless_rows_still_skips_the_link_queries(self):
        """行在、但两个单号字段都空 ⇒ 键集都空 ⇒ 照样零链路查询（短路打在键集上，不是「有没有行」）。"""
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join="",
                    source="",
                )
            ]
        )
        with CaptureQueriesContext(connection) as captured:
            with _stub(up):
                processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        self.assertEqual(up.names(), ["fetch_system_logs"])
        self.assertEqual(
            self._select_sql(captured, "business_jidanpumemberorderlink"),
            [],
            "键集为空的轮不该查链路表",
        )

    def test_a_busy_round_point_queries_links_by_the_unique_remote_key(self):
        """忙轮（远端单号命中）⇒ 链路 SELECT 恰好一条、带 `binding_id` 过滤 + `jidanpu_order_id` IN（唯一索引 `uniq_jdp_member_link_remote` 点查）——不再是「按 binding 全量拉取 + ORDER BY id」的旧形状。"""
        self._link()
        up = _Upstream(
            logs=[
                _row(create_time=_log_time(self.now - timedelta(seconds=2)), source="")
            ],
            ass={JID: _taken()},
        )
        with CaptureQueriesContext(connection) as captured:
            with _stub(up):
                processed = jidanpu_member.poll_log(self.binding, now=self.now)
        # 功能半边：点查真的找到了链路、这一行真的被处理了。
        self.assertEqual(processed, 1)
        selects = self._select_sql(captured, "business_jidanpumemberorderlink")
        # 正面：远端臂的**唯一索引点查**真的在这一轮出现过（含 binding 过滤 + IN 键）。
        self.assertTrue(
            [s for s in selects if '"jidanpu_order_id" IN (' in s and "binding_id" in s],
            f"没有远端臂唯一索引点查：{selects}",
        )
        # 反面：旧「按 binding 全量拉取建索引」的形状（WHERE 只有 binding、无任何单号/订单键）
        # 必须消失。其余按 `order_id` 的点查是接手路径既有的，不在此判据内。
        self.assertEqual(
            [
                s
                for s in selects
                if '"binding_id" = ' in s and "IN (" not in s and "order_id" not in s
            ],
            [],
            f"仍在按 binding 全量拉链路建索引：{selects}",
        )

    def test_duplicate_effective_source_no_still_takes_the_earliest_link(self):
        """两张我方单的**有效来源单号**相同（一张靠来源单号列、一张靠空白回落内置单号）⇒ 仍认 `id` 最早的链路 + 残差台账恰好一条（点查不改语义）。"""
        other = self._order(order_no=SHORT_SOURCE_NO)  # 内置单号=来源单号 ⇒ 回落臂命中
        Order.objects.filter(pk=self.order.pk).update(source_order_no=SHORT_SOURCE_NO)
        self.order.refresh_from_db()
        first = self._link(remote_id=JID)  # 来源单号臂命中
        second = self._link(order=other, remote_id=JID_2)
        self.assertLess(first.pk, second.pk)
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join="",
                    source=SHORT_SOURCE_NO,
                )
            ],
            ass={JID: _taken(), JID_2: _taken(JID_2)},
        )
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)
        # ① 认的是 `id` 更早那条链路：远端回查打在 JID 上，且只有 first 的单被接手（断数据库行）。
        self.assertEqual(
            [kw for name, kw in up.calls if name == "fetch_order_by_key1"][0]["key"],
            JID,
        )
        self.order.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(other.status, OrderStatus.WAITING_GRAB)
        # ② 残差留痕照旧：绑定级台账恰好一条，人话点明「来源单号」。
        notes = JidanpuSyncEvent.objects.filter(
            direction="in", target_type="binding", target_ref=str(self.binding.pk)
        )
        self.assertEqual(notes.count(), 1, list(notes.values_list("reason", flat=True)))
        self.assertIn("来源单号", notes.get().reason)
        self.assertEqual(notes.get().status, "failed")

    def test_a_source_no_matching_a_soft_deleted_order_still_writes_the_missing_note(self):
        """来源单号命中**软删单**的链路 ⇒ 行为与旧全量索引一致：行被认领、写 ORDER_MISSING 台账（⛔ 不是静默跳过——预筛若走默认 manager 会把那一路吞掉，判别力：换回 `Order.objects` 本用例红）。"""
        self._link()  # order_no=ORDER_NO、来源单号空 ⇒ 有效来源单号=内置单号
        Order.objects.filter(pk=self.order.pk).update(deleted_at=self.now)
        up = _Upstream(
            logs=[
                _row(create_time=_log_time(self.now - timedelta(seconds=2)), join="")
            ]
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        notes = self._ledger().filter(status="skipped")
        self.assertEqual(notes.count(), 1, list(notes.values_list("reason", flat=True)))
        self.assertIn("订单已删除或已无法定位", notes.get().reason)


# ══ ③ 回声抑制（一道：只按操作人；E-0292 删时间窗）══════════════════
class EchoSuppressionTests(_Base):
    def test_staff_assignment_by_the_binding_account_claims_the_order_once(self):
        listed_at = self.now - timedelta(minutes=2)
        link = self._link(last_out_at=listed_at)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(listed_at=listed_at)
        assigned_at = self.now - timedelta(seconds=2)
        peer = PeerDownstreamBinding.objects.create(
            account=User.objects.create_user(username="staff-assign-peer"),
            peer_base_url="https://staff-assign-peer.test.invalid",
            token_in_hash="syn-in",
            token_out="syn-out",
        )
        up = _Upstream(
            logs=[_row(create_time=_log_time(assigned_at), user=BINDING_DISPLAY_NAME)],
            remote={JID: {
                "orderId": JID,
                "orderSta": "代练中",
                "isThrowOrder": "N",
                "recManName": REMOTE_TAKER_NAME,
                "assUserName": BINDING_DISPLAY_NAME,
                "assTime": _log_time(assigned_at),
                "recOrderTime": None,
            }},
            ass={JID: _taken()},
        )

        with _stub(up):
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 1)
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), self.binding.user_id)
        self.assertEqual(self._ledger().get(status="done").payload["remote_uid"], REMOTE_TAKER_UID)
        self.assertEqual(self._audit().count(), 1)
        take = PeerSyncEvent.objects.get(kind="hall.take", binding_id=str(peer.pk))
        self.assertEqual(take.target_ref, self.order.order_no)
        self.assertEqual(take.payload["status"], OrderStatus.IN_PROGRESS)

    def test_matching_operator_checks_hall_without_acting_on_unassigned_or_stale_rows(self):
        listed_at = self.now - timedelta(minutes=2)
        link = self._link(last_out_at=listed_at)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(listed_at=listed_at)
        assigned_at = self.now - timedelta(seconds=2)
        remote = {
            "orderId": JID, "orderSta": "代练中", "isThrowOrder": "N",
            "recManName": REMOTE_TAKER_NAME, "assTime": _log_time(assigned_at),
        }
        for change in (
            {"isThrowOrder": "Y"},
            {"orderSta": "未分配"},
            {"assTime": _log_time(listed_at - timedelta(seconds=2))},
            {"recManName": ""},
        ):
            with self.subTest(change=change):
                up = _Upstream(
                    logs=[_row(create_time=_log_time(assigned_at), user=BINDING_DISPLAY_NAME)],
                    remote={JID: {**remote, **change}},
                    ass={JID: _taken()},
                )
                with _stub(up):
                    self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 0)
                self.assertEqual(up.names(), ["fetch_system_logs", "fetch_order_by_key1"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))

    def test_staff_assignment_in_a_peer_mirror_sends_one_upstream_claim(self):
        other_taker_name = "合成鸡蛋铺协作打手"
        peer = PeerUpstreamBinding.objects.create(
            peer_name="合成订单源头", peer_base_url="https://staff-assign-up.test.invalid",
            peer_account="synthetic", token_in_hash=hash_token(new_token()), token_out=new_token(),
            local_base_url="https://staff-assign-local.test.invalid",
            bound_by=User.objects.create_user(username="staff-assign-peer-operator"),
            hall_sync_enabled=True,
        )
        OrderSource.objects.create(name=source_name_for(peer.peer_name))
        payload = {
            "order_no": ORDER_NO_2, "source_order_no": ORDER_NO_2, "game_name": self.game.name,
            "status": OrderStatus.WAITING_GRAB, "hall_price": "50.00",
        }
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=peer, peer_base_url=peer.peer_base_url, peer_order_no=ORDER_NO_2,
            game=self.game, remote_hall_price=Decimal("50.00"), first_seen_at=self.now, payload=payload,
        )
        shadow = hall_shadow.ensure_for_card(card)
        listed_at = self.now - timedelta(minutes=5)
        link = self._link(order=shadow, last_out_at=listed_at)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(listed_at=listed_at)
        assigned_at = self.now - timedelta(seconds=3)
        up = _Upstream(
            logs=[_row(create_time=_log_time(assigned_at), user=BINDING_DISPLAY_NAME)],
            remote={JID: {
                "orderId": JID, "orderSta": "代练中", "isThrowOrder": "N",
                "recManName": f"{REMOTE_TAKER_NAME}/{other_taker_name}",
                "assTime": _log_time(assigned_at),
            }},
            ass={JID: [{"userId": 8101, "userName": other_taker_name}, *_taken()]},
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 1)
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 0)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(card.claiming_by_id, self.binding.user_id)
        claim_event = PeerSyncEvent.objects.get(
            binding_role="upstream", binding_id=str(peer.pk), kind="hall.claim",
        )
        self.assertEqual(claim_event.target_ref, ORDER_NO_2)
        event = {
            "kind": "order.assign", "idempotency_key": "syn-staff-assign-upstream-reply",
            "target_type": "order", "target_ref": ORDER_NO_2,
            "occurred_at": timezone.now().isoformat(),
            "payload": {**payload, "status": OrderStatus.IN_PROGRESS},
        }
        self.assertEqual(peer_inbound.apply_events([event], binding=peer).applied, 1)
        card.refresh_from_db()
        shadow.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(card.promoted_order_id, shadow.pk)
        self.assertEqual(self._main_user_id(shadow), self.binding.user_id)
        self.assertEqual(PeerOrderLink.objects.get(upstream_binding=peer).order_id, shadow.pk)

    def test_a_row_from_our_own_binding_account_is_suppressed(self):
        """第一道：操作人 = 本绑定的 `account_name` ⇒ 跳过（我方自己录单 / 抛单 / 改状态的行）。"""
        self._link()
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    user=BINDING_DISPLAY_NAME,
                )
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        # ⚠️ 连回查都不做：这是回声抑制的**代价方向**（⛔ 不白打一次上游）。
        self.assertEqual(up.names(), ["fetch_system_logs"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))

    def test_the_same_row_from_another_operator_is_not_suppressed(self):
        """对照：只换操作人 ⇒ 必须真的去回查并接手（否则上一条是「表不可达」的假绿）。"""
        self._link()
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    user=OTHER_OPERATOR,
                )
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        self.assertIn("fetch_ass_logs", up.names())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_a_take_row_inside_the_push_window_is_processed_and_claims(self):
        """E-0292 回归：对方在我们推送后 5 分钟内自助接单，快路必须处理（原时间窗会吞掉）。

        现场（订单 3316546598085013195）：09:34:03 推单、09:34:20 对方自助接单，
        这一行落在 `[last_out_at, +300s]` 里被**时间窗**跳过（E-0292 删掉的那道）⇒ 没有自动接手。
        """
        self._link(last_out_at=self.now - timedelta(seconds=10))
        up = _Upstream(
            logs=[
                _row(create_time=_log_time(self.now - timedelta(seconds=5)))
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        self.assertIn("fetch_ass_logs", up.names())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), self.user.pk)

    def test_our_own_row_is_suppressed_even_inside_the_push_window(self):
        """按操作人那道仍然生效：窗口内、且操作人 = 本绑定 account_name ⇒ 跳过、⛔ 不回查。"""
        self._link(last_out_at=self.now - timedelta(seconds=10))
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=5)),
                    user=BINDING_DISPLAY_NAME,
                )
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        self.assertEqual(up.names(), ["fetch_system_logs"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))

    def test_our_own_assignment_echo_with_unknown_operator_does_not_reassign_or_withdraw(self):
        """E-0292 补充（审阅方）：我方**自己的指派回显**被处理时，必须幂等、⛔ 不改派 / 不退单。

        现网该绑定的 `account_name` 是**空的**（身份自愈没落名，计划 §5 残差）⇒ 我方自己的
        行过不了「操作人」那道闸、会被快路当成普通闹钟处理。本用例钉住「处理它无害」：
        场景 = 本地单**已指派给 taker X**、链路刚推过（`last_out_at` 在窗口内），日志行是
        **同一次指派**的回显（`userName` 就是本该被认出来的我方操作人名，但 `account_name`
        为空 ⇒ 认不出来），远端只是「已录单、还没人接」（本线 ⛔ 不在对方系统里指派任何人，
        spec A15）。

        判据只打数据库行与桩的调用序列（⛔ 不解析 `remark` 文本，A9）：
        ① 那一行**真的被处理**（去回查了远端）——删窗之前它被静默跳过；
        ② 我方单**一个字没动**（状态 / 接手人）；
        ③ ⛔ 没排退单、⛔ 没写「自动接手」的审计；
        ④ 同一句话的台账只有一条，且**再跑一轮**（上游把同一行又回一次）仍然只有一条。
        """
        # 现网形态：绑定的 `account_name` 为空 ⇒ 操作人那道闸什么都挡不住（fail-open）。
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(account_name="")
        self.binding.refresh_from_db()
        other = User.objects.create_user(username="other-taker", password="pw-a")
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(
            user=other
        )
        self._link(last_out_at=self.now - timedelta(seconds=10))
        # `userName` 是**我方操作人自己的名字**——它没被认出来是因为 `account_name` 为空，
        # 不是因为名字不同；`remark` 用默认那条也无所谓：判据一个字都不读它（A9）。
        row = _row(
            create_time=_log_time(self.now - timedelta(seconds=5)),
            user=BINDING_DISPLAY_NAME,
        )
        up = _Upstream(logs=[row], ass={JID: []})
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        # ① 真的被处理（两条上游回查都在，不是被「窗口」静默跳过）
        self.assertEqual(processed, 1)
        self.assertEqual(
            up.names(),
            ["fetch_system_logs", "fetch_order_by_key1", "fetch_ass_logs"],
        )
        # ② 我方单一个字没动：状态与接手人都还是 X
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), other.pk)
        # ③ ⛔ 没排退单、⛔ 没自动接手
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind=KIND_WITHDRAW_LITERAL, target_ref=ORDER_NO
            ).exists()
        )
        self.assertFalse(self._audit().exists())
        # ④ 幂等：台账按「同一句话只一条」去重；下一轮把同一行又拉回来也不加行
        self.assertEqual(self._ledger().count(), 1)
        second = _Upstream(logs=[row], ass={JID: []})
        with _stub(second):
            self.assertEqual(
                jidanpu_member.poll_log(
                    self.binding, now=self.now + timedelta(seconds=3)
                ),
                1,
            )
        self.assertEqual(self._ledger().count(), 1)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind=KIND_WITHDRAW_LITERAL, target_ref=ORDER_NO
            ).exists()
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), other.pk)

    def test_a_row_outside_the_push_window_is_not_suppressed(self):
        """删窗后：离上次推送很远 ⇒ 对方行照常被处理（与窗口内同一条路）。"""
        self._link(last_out_at=self.now - timedelta(seconds=1000))
        up = _Upstream(
            logs=[
                _row(create_time=_log_time(self.now - timedelta(seconds=5)))
            ],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)


# ══ ④ 日志只当闹钟：换文案行为不变（A9）═══════════════════════════════════════


class RemarkVariantTests(_Base):
    def test_free_text_variants_do_not_change_behaviour(self):
        """A9：把 `remark` 换成另一种渠道写法 ⇒ 接手结果必须一样（⛔ 不解析文案）。"""
        variants = (
            (REMARK_WEB, ORDER_NO, JID),
            (REMARK_DOWNSTREAM, ORDER_NO_2, JID_2),
        )
        for remark, order_no, jid in variants:
            with self.subTest(remark=remark):
                order = (
                    self.order if order_no == ORDER_NO else self._order(order_no=order_no)
                )
                self._link(order=order, remote_id=jid)
                up = _Upstream(
                    logs=[
                        _row(
                            create_time=_log_time(self.now - timedelta(seconds=2)),
                            join=jid,
                            source=order_no,
                            remark=remark,
                        )
                    ],
                    ass={jid: _taken(jid)},
                )
                with _stub(up):
                    processed = jidanpu_member.poll_log(self.binding, now=self.now)
                self.assertEqual(processed, 1)
                # 判据落在**真实状态查询**上（⛔ 不是落在日志文本上）
                self.assertIn("fetch_ass_logs", up.names())
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
                self.assertEqual(self._main_user_id(order), self.user.pk)


# ══ ⑤ 自动接手（A6：钱在我方算）═══════════════════════════════════════════════


class AutoClaimTests(_Base):
    def _poll(self, up: _Upstream, *, now=None):
        with _stub(up):
            return jidanpu_member.poll_log(self.binding, now=now or self.now)

    def _taken_upstream(self):
        return _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    user=OTHER_OPERATOR,
                )
            ],
            ass={JID: _taken()},
        )

    def test_the_remote_takeover_claims_the_order_and_moves_the_money_row(self):
        """⭐ spec §8.2：本线**钱在我方算**——接手成功要断言**订单 + 绩效行**，⛔ 不只台账。"""
        self._link()
        processed = self._poll(self._taken_upstream())
        self.assertEqual(processed, 1)

        # ① 订单状态（走的是状态机 ⇒ `代练中`）
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        # ② 绩效行（MAIN 行的 user = 该代练员）——钱落在**我方**这一行上
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.user.pk)
        self.assertIsNotNone(perf.claimed_at)

        # ③ 台账（in 方向；幂等标记）
        event = self._ledger().get()
        self.assertEqual(event.direction, "in")
        self.assertEqual(event.status, "done")
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)
        self.assertEqual(event.payload["remote_uid"], REMOTE_TAKER_UID)

    def test_a_busy_earlier_order_is_retried_without_blocking_a_later_claim(self):
        self._link()
        later_order = self._order(order_no=ORDER_NO_2)
        self._link(order=later_order, remote_id=JID_2)
        first_at = _log_time(self.now - timedelta(seconds=2))
        second_at = _log_time(self.now - timedelta(seconds=1))
        self.binding.log_cursor = _log_time(self.now - timedelta(minutes=1))
        self.binding.save(update_fields=["log_cursor"])

        class BoundedLog(_Upstream):
            def fetch_system_logs(self, client, *, stime, **kwargs):
                rows = super().fetch_system_logs(client, stime=stime, **kwargs)
                return tuple(row for row in rows if row["createTime"] >= stime)

        upstream = BoundedLog(
            logs=[
                _row(create_time=first_at),
                _row(create_time=second_at, join=JID_2, source=ORDER_NO_2),
            ],
            ass={JID: _taken(), JID_2: _taken(JID_2)},
        )

        from apps.business import jidanpu_assignment_lock

        separate = connection.copy(alias="test_member_claim_lock")
        try:
            with separate.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    [jidanpu_assignment_lock._key("order", self.order.pk)],
                )
                self.assertTrue(cursor.fetchone()[0])
            self.assertEqual(self._poll(upstream), 1)
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.log_cursor, first_at)
            self.assertEqual(self._poll(upstream, now=self.now + timedelta(seconds=90)), 0)
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.log_cursor, first_at)
        finally:
            separate.close()

        later_order.refresh_from_db()
        self.assertEqual(later_order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._poll(upstream, now=self.now + timedelta(seconds=91)), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._audit().count(), 1)

    def test_a_unique_constraint_on_the_claim_is_a_loud_refusal_not_a_retry_loop(self):
        """⭐ 2026-09-27 真机：接手撞了 `uniq_performance_order_user`（同单同人只有一条绩效行）。

        真机现象（运维侧日志原文）：`13:12:10` 起每 3~4 秒一条
        「快路出现未预期失败（IntegrityError），继续下一个」，而绑定的 `last_error` 一个字都没有
        —— `IntegrityError` 裸逃会**跳过游标推进** ⇒ 同一行日志每轮重来一次，页面上却看不出来。

        判据：① 本轮**正常收尾**（`processed == 1` ⇒ 游标照推、不再重来）；
        ② 台账一条 `failed` 且**点名成因**（同一张单同一人只有一条绩效行）；
        ③ 那句话进绑定的 `last_error`（页面看得见）；④ ⛔ **不排退单**（对方那张单不能被动）。
        """
        self._link()
        with mock.patch.object(
            jidanpu_member, "claim_order",
            side_effect=IntegrityError("uniq_performance_order_user"),
        ):
            processed = self._poll(self._taken_upstream())

        self.assertEqual(processed, 1)
        note = self._ledger().filter(status="failed").get()
        self.assertIn("绩效行", note.reason)
        binding = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
        self.assertIn("绩效行", binding.last_error)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="member.withdraw", target_ref=self.order.order_no
            ).exists()
        )

    def test_the_audit_row_says_it_was_a_machine_action(self):
        """spec §8.3：机器动作 + 钱的动作 ⇒ 审计必须能看出「不是人点的」。"""
        self._link()
        self._poll(self._taken_upstream())

        row = self._audit().get()
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.actor, AUDIT_ACTOR_LITERAL)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.order_no, ORDER_NO)
        # ⭐ spec §3-A2）：`target_name` 是**账号名称**（昵称），⛔ 不再是登录账号。
        self.assertEqual(row.target_name, BINDING_USER_NICKNAME)
        self.assertNotEqual(row.target_name, BINDING_USER)
        self.assertEqual(row.to_value, "12.34")
        self.assertEqual(
            row.summary,
            f"【{AUDIT_ACTOR_LITERAL}】{BINDING_USER_NICKNAME}接手了订单 {ORDER_NO}，"
            f"接手价 12.34 元，已同步到下家 {BINDING_USER_NICKNAME} 的系统",
        )
        # 正文里**不许**再出现登录账号（那是本线要修的症状）
        self.assertNotIn(BINDING_USER, row.summary)
        # 来源**恰好出现一次**（`【` 只有开头一个）
        self.assertEqual(row.summary.count("【"), 1, row.summary)

    def test_remote_uid_and_name_share_the_same_raw_evidence(self):
        """原始同步记录保留配对后的远端 UID 和名字，业务日志只显示本地账号。"""
        self._link()
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: [
                {"userId": "", "userName": "只有名字的甲"},
                {"userId": 5002, "userName": "有uid的乙"},
            ]},
        )
        self._poll(up)

        row = self._audit().get()
        payload = self._ledger().get(status="done").payload
        self.assertEqual((payload["remote_uid"], payload["remote_name"]), (5002, "有uid的乙"))
        self.assertNotIn("uid=", row.summary)
        self.assertNotIn("有uid的乙", row.summary)

    def test_the_takeover_goes_through_the_only_state_machine_entry(self):
        """spec §8.3：⛔ 不裸 `update()`；接手必须经过唯一漏斗 `apply_transition`。"""
        link = self._link()
        seen = []
        real = claim.apply_transition

        def _spy(order, target, *, nodes, **kwargs):
            seen.append((order.order_no, str(target), sorted(nodes)))
            return real(order, target, nodes=nodes, **kwargs)

        with mock.patch.object(claim, "apply_transition", side_effect=_spy):
            self._poll(self._taken_upstream())

        self.assertEqual(
            seen, [(ORDER_NO, OrderStatus.IN_PROGRESS, ["grab_hall.take_order"])]
        )
        # 唯一的漏斗还会给已有链路排一条 `member.status_out` —— 它同时是
        # 「真实现（不是桩）跑过」的独立证据。
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="member.status_out",
                target_ref=ORDER_NO,
                payload__member_binding_id=self.binding.pk,
            ).exists()
        )
        self.assertEqual(link.pk, JidanpuMemberOrderLink.objects.get(pk=link.pk).pk)

    def test_a_second_poll_does_not_claim_or_audit_again(self):
        """幂等：同一批日志行再来一轮 ⇒ **不再回查远端、不再接手**（`done` 标记挡住）。

        ⚠️ 拉日志那一跳**照常**（那是闹钟本身）；被挡掉的是 `fetch_ass_logs` 与后续所有写。
        """
        self._link()
        first = self._taken_upstream()
        self.assertEqual(self._poll(first), 1)
        second = self._taken_upstream()
        self.assertEqual(self._poll(second), 0)
        self.assertEqual(second.names(), ["fetch_system_logs"])
        self.assertEqual(self._audit().count(), 1)
        self.assertEqual(self._ledger().count(), 1)

    def test_a_missing_node_denies_the_takeover_and_rolls_the_money_row_back(self):
        """缺节点 ⇒ `IllegalTransition`：绩效行那半截写入必须**回滚**，且台账 + `last_error` 响亮。"""
        powerless = User.objects.create_user(username="member-nonodes", password="pw-a")
        self.binding.user = powerless
        self.binding.save(update_fields=["user"])
        self._link()

        processed = self._poll(self._taken_upstream())
        self.assertEqual(processed, 1)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        # ⚠️ 条件更新在 `apply_transition` **之前**写过 MAIN 行 ⇒ 不去回滚的话这里就是接单人
        #    （E-0022 的「一半成功」形状）。
        self.assertIsNone(self._main_user_id(self.order))
        self.assertFalse(self._audit().exists())
        note = self._ledger().get()
        self.assertEqual(note.status, "failed")
        self.binding.refresh_from_db()
        self.assertIn("状态机", self.binding.last_error)
        self.assertIsNotNone(self.binding.last_error_at)

    def test_the_machine_takeover_ignores_the_per_person_gates(self):
        """⭐ **裁定 8**：会挡住**人**的按人闸门（接单价区间不含该单）挡不住**机器接手**。

        A6 的「对方打手接了 ⇒ 我方自动接手」是**无条件**的（那时单已经在对方手里），
        被闸门挡住会造出「我方没接手、对方在做」⇒ 绩效 / 结算漏发。
        """
        self._link()
        self._blocking_limit()

        real = jidanpu_member.claim_order
        with mock.patch.object(
            jidanpu_member, "claim_order", wraps=real
        ) as spy:
            processed = self._poll(self._taken_upstream())
        self.assertEqual(processed, 1)

        # ① 显式钉住这次调用传的是 `enforce_limits=False`（⛔ 不是靠「碰巧过了」）
        spy.assert_called_once()
        self.assertIs(spy.call_args.kwargs["enforce_limits"], False)
        # ② 机器接手真的成了：订单状态 + MAIN 绩效行 + 审计行（钱在我方算）
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), self.user.pk)
        self.assertTrue(self._audit().exists())
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")

    def test_a_human_claim_is_still_blocked_by_the_same_limit(self):
        """反：**默认 `enforce_limits=True`** ⇒ 同一个人走人类入口（真 HTTP `/claim/`）仍被挡。

        这条证明上面那条没有把默认路径改宽（⛔ 不是「放宽了闸门」而是「机器这一路显式跳过」）。
        """
        self._link()
        self._blocking_limit()

        body = self.user_client.post(
            f"/api/business/hall/{ORDER_NO}/claim/", {},
            content_type="application/json",
        ).json()
        # `GRAB_LIMIT_EXCEEDED`（`apps/core/error_codes.py`）——写**字面值** 1013（E-0024）
        self.assertEqual(body["code"], 1013, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))
        self.assertFalse(self._audit().exists())

    def test_an_order_that_left_our_hall_is_refused_loudly(self):
        """裁定 8 之后**只剩这一种**机器拒绝：该单此刻不在我方抢单大厅 ⇒ 台账 + `last_error`。"""
        order = self._order(order_no=ORDER_NO_2, status=OrderStatus.UNASSIGNED)
        self._link(order=order, remote_id=JID_2)
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join=JID_2,
                    source=ORDER_NO_2,
                )
            ],
            ass={JID_2: _taken(JID_2)},
        )
        self._poll(up)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self._main_user_id(order))
        note = self._ledger(ORDER_NO_2).get()
        self.assertEqual(note.status, "failed")
        self.assertIn("不在我方抢单大厅", note.reason)
        self.binding.refresh_from_db()
        self.assertIn("不在我方抢单大厅", self.binding.last_error)
        self.assertFalse(self._audit(ORDER_NO_2).exists())
        # 第二轮上游又把**同一行**回了一次：台账**不会**多出一行（同 `(单, 原因)` 只记一条）
        # —— 「⛔ 不无限重试」落在这里。
        second = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join=JID_2,
                    source=ORDER_NO_2,
                )
            ],
            ass={JID_2: _taken(JID_2)},
        )
        self._poll(second)
        self.assertEqual(self._ledger(ORDER_NO_2).count(), 1)
        self.assertFalse(self._audit(ORDER_NO_2).exists())

    def test_a_missing_main_row_is_refused_without_touching_the_remote(self):
        """⭐ 评审 Important：**缺 MAIN 绩效行 ≠ 我方别的打手赢了**。

        `claim_order` 抛 `ORDER_ALREADY_TAKEN` 的第二种成因是「条件更新 0 行，因为这条单
        **一条 MAIN 行都没有**」（E-0148：「无主的 MAIN 行被整条删掉」是**正常数据形态**）。
        误判成 A10 竞态就会**在谁都没接手的情况下**把**对方那张单**退掉，而台账还写着
        「我方已有别的打手接手」（一句假话）。
        """
        order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO_2,
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目甲",
        )  # ⛔ 故意**不建** MAIN 绩效行
        self._link(order=order, remote_id=JID_2)
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join=JID_2,
                    source=ORDER_NO_2,
                )
            ],
            ass={JID_2: _taken(JID_2)},
        )
        self._poll(up)

        # ⛔ 一条 `member.withdraw` 都不许排（那会真的把对方那张单退掉）
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind=KIND_WITHDRAW_LITERAL).exists()
        )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        note = self._ledger(ORDER_NO_2).get()
        self.assertEqual(note.status, "failed")
        self.assertIn("MAIN 绩效行", note.reason)
        self.binding.refresh_from_db()
        self.assertIn("MAIN 绩效行", self.binding.last_error)
        self.assertFalse(self._audit(ORDER_NO_2).exists())

    def test_a_remote_refunded_order_is_not_claimed(self):
        """spec §6.1 ③：回查到「远端**已退单**」⇒ 只更新台账，⛔ 不接手、⛔ 不排退单。"""
        self._link()
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            remote={JID: {"orderId": JID, "orderSta": "已退单", "isThrowOrder": "N"}},
            # 就算接单人日志还留着旧接单人，也**不许**接手（那正是本缺口要防的）
            ass={JID: _taken()},
        )
        self._poll(up)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))
        self.assertFalse(self._audit().exists())
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind=KIND_WITHDRAW_LITERAL).exists()
        )
        note = self._ledger().get()
        self.assertEqual(note.status, "skipped")
        self.assertIn("已退单", note.reason)
        # 远端真实状态一读出来就短路 ⇒ 连接单人日志那一步都不打
        self.assertNotIn("fetch_ass_logs", up.names())

    def test_a_remote_order_still_in_the_hall_is_not_claimed(self):
        """spec §6.1 ③：回查到「**还挂在他家大厅**」⇒ 没人接走（旧接单人日志不算数）。"""
        self._link()
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            remote={JID: {"orderId": JID, "orderSta": "待上号", "isThrowOrder": "Y"}},
            ass={JID: _taken()},
        )
        self._poll(up)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))
        self.assertFalse(self._audit().exists())
        note = self._ledger().get()
        self.assertEqual(note.status, "skipped")
        self.assertIn("大厅", note.reason)
        self.assertNotIn("fetch_ass_logs", up.names())

    def test_a_remote_order_that_cannot_be_looked_up_is_loud(self):
        """⚠️ `fetch_order_by_key1` 回 `None` ⇒ **读不到**（⛔ 不是「远端没有这张单」）。"""
        self._link()
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            remote={JID: None},
            ass={JID: _taken()},
        )
        self.assertEqual(self._poll(up), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))
        note = self._ledger().get()
        self.assertEqual(note.status, "failed")
        self.assertIn("读不到", note.reason)
        self.binding.refresh_from_db()
        self.assertIn("读不到", self.binding.last_error)
        self.assertNotIn("fetch_ass_logs", up.names())
        # ⚠️ 游标照推：一行「读不到」不会把整条快路**永远卡死**（响亮已记在台账 + last_error）
        self.assertEqual(
            self.binding.log_cursor, _log_time(self.now - timedelta(seconds=2))
        )

    def test_a_taker_row_without_a_numeric_uid_is_treated_as_unreadable(self):
        """⚠️ 接单人日志**非空、却没有可用的数值 `userId`** ⇒ 当**读不到**（需求 6）。

        ⛔ 不是「健康地没被接走」：那种误判会**把上一次的旧错清掉**，看起来一切正常。
        """
        self._link()
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            last_error="上一次的旧错", last_error_at=self.now - timedelta(minutes=5)
        )
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: [{"userId": "", "userName": REMOTE_TAKER_NAME}]},
        )
        self._poll(up)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))
        note = self._ledger().get()
        self.assertEqual(note.status, "failed")
        self.assertIn("userId", note.reason)
        self.binding.refresh_from_db()
        # 旧错被**换成**新的读不到原因（⛔ 不是被清成空串）
        self.assertIn("userId", self.binding.last_error)
        self.assertIsNotNone(self.binding.last_error_at)


    def test_a_soft_deleted_order_is_not_claimed_upstream(self):
        order = self._order(order_no=ORDER_NO_2)
        link = self._link(order=order, remote_id=JID_2)
        Order.all_objects.filter(pk=order.pk).update(deleted_at=timezone.now())
        up = _Upstream(
            logs=[
                _row(
                    create_time=_log_time(self.now - timedelta(seconds=2)),
                    join=JID_2,
                    source=ORDER_NO_2,
                )
            ],
            ass={JID_2: _taken(JID_2)},
        )
        self._poll(up)
        self.assertFalse(self._audit(ORDER_NO_2).exists())
        self.assertEqual(link.pk, JidanpuMemberOrderLink.objects.get(pk=link.pk).pk)


# ══ ⑤b `done` 标记的时间窗（N1 / R3-M1：⛔ 不许跨生命周期粘连）════════════════════


class RemoteTakenDoneMarkerLifecycleTests(_Base):
    """`member.remote_taken` 的 `done` 标记只对**当前这一轮上大厅**有效。

    现场（R3-M1）：回池重抛只把链路更新为 `in_hall=True, listed_at=now`，
    ⛔ 不清理上一轮留下的 `done` 行 ⇒ 下一轮对方接单时旧标记把整条闹钟吞掉，
    单永停 `WAITING_GRAB`、可被二次抢。

    口径（业务方拍板）：**不改 `done` 行 status**（那是「已自动接手」的台账事实），
    而是给「命中」加一条时间判据——标记时刻**早于**链路最近一次 `listed_at` ⇒ 不算命中；
    `listed_at IS NULL`（legacy / 从未进过厅）⇒ 退回旧判据。
    """

    def _done_marker(self, *, created_at=None):
        """写一条上一轮生命周期留下的 `done` 标记（reason 用可辨认的合成人话）。"""
        event = JidanpuSyncEvent.objects.create(
            direction="in",
            kind=KIND_REMOTE_TAKEN_LITERAL,
            status="done",
            target_type="order",
            target_ref=ORDER_NO,
            reason="已自动接手（钱按绩效算）",
            payload={"member_binding_id": self.binding.pk},
        )
        if created_at is not None:
            # `created_at` 是 `auto_now_add` ⇒ 要造「上一轮」的时刻只能落库后改。
            JidanpuSyncEvent.objects.filter(pk=event.pk).update(created_at=created_at)
            event.refresh_from_db()
        return event

    def _taken_upstream(self):
        return _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: _taken()},
        )

    def test_rethrow_ignores_prior_claim_and_processes_current_claim_only_once(self):
        link = self._link()
        self._done_marker(created_at=self.now - timedelta(minutes=2))
        previous_assigned = self.now - timedelta(minutes=1)
        OperationLog.objects.create(
            log_type=LogType.ORDER, action="order.clear_taker", target_kind="user",
            target_id=str(self.user.pk), target_name=BINDING_USER_NICKNAME, order_no=self.order.order_no,
        )
        withdraw = jidanpu_member.queue_member_event(
            self.order, self.binding, kind="member.withdraw", rearm=True,
        )
        launch = jidanpu_member.queue_member_event(
            self.order, self.binding, kind="member.launch", rearm=True,
        )
        prior = {"orderId": JID, "orderSta": "代练中", "isThrowOrder": "N",
                 "recManName": "上一轮接手人", "assTime": _log_time(previous_assigned)}
        remote = prior.copy()
        old_assignment = {"userId": 9009, "userName": "上一轮接手人"}
        logs = [old_assignment]
        upstream = _Upstream(remote={JID: remote}, ass={JID: logs})

        def set_remote_status(client, *, remote_order_id, status_label, **ignored):
            remote.update(orderSta=status_label, isThrowOrder="N")

        def clear_remote_taker(client, *, remote_order_id):
            self.assertEqual(remote_order_id, JID)
            self.assertEqual(remote["orderSta"], "已退单")
            remote["recManName"] = ""
            logs.clear()

        def publish(client, **ignored):
            self.assertEqual(remote["orderSta"], "未分配")
            self.assertEqual(logs, [])
            remote["isThrowOrder"] = "Y"

        with (
            _stub(upstream),
            mock.patch.object(jidanpu, "set_order_status", side_effect=set_remote_status),
            mock.patch.object(jidanpu, "clear_order_taker", side_effect=clear_remote_taker) as clear,
            mock.patch.object(jidanpu, "throw_order", side_effect=publish) as throw,
        ):
            row = _row(create_time=_log_time(self.now))
            self.assertEqual(jidanpu_member._handle_alarm(
                self.binding, link, row=row, client=CLIENT, now=self.now,
            )[0], "done_marker")
            self.assertEqual(upstream.calls, [])
            self.assertEqual(jidanpu_member.consume_event(withdraw, client=CLIENT), "done")
            self.assertEqual(jidanpu_member.consume_event(launch, client=CLIENT), "done")
            clear.assert_called_once_with(CLIENT, remote_order_id=JID)
            throw.assert_called_once()
            link.refresh_from_db()

            remote.update(prior)
            logs[:] = [old_assignment]
            self.assertEqual(jidanpu_member._handle_alarm(
                self.binding, link, row=row, client=CLIENT, now=self.now,
            )[0], "no_claim")
            self.assertIsNone(self._main_user_id(self.order))

            remote.update(
                recManName=REMOTE_TAKER_NAME,
                assTime=_log_time(link.listed_at + timedelta(seconds=2)),
            )
            logs[:] = [old_assignment, *_taken()]
            self.assertEqual(jidanpu_member._handle_alarm(
                self.binding, link, row=row, client=CLIENT, now=self.now,
            )[0], "claimed")
            self.assertEqual(jidanpu_member._handle_alarm(
                self.binding, link, row=row, client=CLIENT, now=self.now,
            )[0], "done_marker")

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), self.user.pk)
        marker = self._ledger().filter(status="done").latest("id")
        self.assertEqual(marker.payload["remote_uid"], REMOTE_TAKER_UID)
        self.assertEqual(self._ledger().filter(status="done").count(), 2)
        self.assertEqual(self._audit().count(), 1)

    def test_rethrow_recovers_a_new_claim_before_the_listing_is_observed(self):
        link = self._link(in_hall=False)
        self._done_marker(created_at=self.now - timedelta(minutes=2))
        withdraw = jidanpu_member.queue_member_event(
            self.order, self.binding, kind="member.withdraw", rearm=True,
        )
        launch = jidanpu_member.queue_member_event(
            self.order, self.binding, kind="member.launch", rearm=True,
        )
        confirmed_at = self.now - timedelta(seconds=10)
        JidanpuSyncEvent.objects.filter(pk=withdraw.pk).update(
            status="done", processed_at=confirmed_at,
        )
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(withdrawn_at=confirmed_at)
        current = {
            "orderId": JID, "orderSta": "代练中", "isThrowOrder": "N",
            "assTime": _log_time(self.now - timedelta(seconds=3)), "recManName": REMOTE_TAKER_NAME,
        }
        upstream = _Upstream(remote={JID: current}, ass={
            JID: [{"userId": 9009, "userName": "上一轮接手人"}, *_taken()],
        })
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(launch, client=CLIENT), "done")
            self.assertEqual(jidanpu_member.consume_event(launch, client=CLIENT), "done")
        link.refresh_from_db()
        self.order.refresh_from_db()
        self.assertFalse(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        marker = self._ledger().filter(status="done").latest("id")
        self.assertEqual(marker.payload["remote_uid"], REMOTE_TAKER_UID)
        self.assertEqual(self._audit().count(), 1)

    def test_a_second_lifecycle_is_not_blocked_by_the_previous_done_marker(self):
        """判据①：回池重抛后对方第二轮接单 ⇒ **真的再接手一次**（现状被旧 marker 吞掉）。

        ⛔ 弄红它：把 `_remote_taken_done` 的时间判据去掉（只看 `done` 行存在）⇒
        订单停在 `WAITING_GRAB`、`processed == 0`、没有第二条审计。
        """
        link = self._link()
        marker = self._done_marker(created_at=self.now - timedelta(minutes=30))
        #: 回池重抛：链路最近一次上大厅发生在那个旧标记**之后**。
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            in_hall=True, listed_at=self.now - timedelta(minutes=1),
        )

        with _stub(self._taken_upstream()):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)

        self.assertEqual(processed, 1, "旧 done 标记把第二轮接手吞掉了")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), self.user.pk)
        #: 判据④：旧标记是**台账事实**，一个字都不许改（⛔ 不许改成 `skipped`）。
        marker.refresh_from_db()
        self.assertEqual(marker.status, "done")
        self.assertEqual(marker.reason, "已自动接手（钱按绩效算）")

    def test_the_marker_window_is_inclusive_and_legacy_links_keep_the_old_rule(self):
        """判据①＋②：`created_at >= listed_at` 才算命中；`listed_at IS NULL` 退回旧判据。

        ⛔ 弄红它：把「早于 listed_at」也当命中（现状）⇒ 第二条断言红；
        把 `listed_at is None` 当「永不命中」⇒ 第三条断言红（老数据失去去重）。
        """
        marker = self._done_marker(created_at=self.now - timedelta(minutes=10))
        link = self._link()

        #: 边界含等号：标记时刻 == 最近上大厅时刻 ⇒ 仍算命中。
        self.assertTrue(
            jidanpu_member._remote_taken_done(
                self.binding, ORDER_NO, listed_at=marker.created_at,
            )
        )
        #: 标记早于最近一次上大厅 ⇒ 上一轮的结论，不算命中。
        self.assertFalse(
            jidanpu_member._remote_taken_done(
                self.binding, ORDER_NO,
                listed_at=marker.created_at + timedelta(seconds=1),
            )
        )
        #: legacy：链路从未进过厅（`listed_at IS NULL`）⇒ 退回旧判据（仍算命中）。
        self.assertIsNone(JidanpuMemberOrderLink.objects.get(pk=link.pk).listed_at)
        self.assertTrue(jidanpu_member._remote_taken_done(self.binding, ORDER_NO))

    def test_a_legacy_link_without_listed_at_still_dedupes_the_alarm(self):
        """判据②（集成面）：`listed_at IS NULL` 的链路仍被旧口径挡住（⛔ 不白打上游）。"""
        self._done_marker()
        self._link()
        up = self._taken_upstream()

        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)

        self.assertEqual(processed, 0)
        #: 只拉了日志（闹钟本身），⛔ 没有回查远端 / 没有接手。
        self.assertEqual(up.names(), ["fetch_system_logs"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))


# ══ ⑥ 竞态（A10：先接先得）═══════════════════════════════════════════════════


class RaceTests(_Base):
    def _poll(self, up: _Upstream):
        with _stub(up):
            return jidanpu_member.poll_log(self.binding, now=self.now)

    def _taken_upstream(self):
        return _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: _taken()},
        )

    def test_when_one_of_our_other_takers_already_took_it_we_withdraw_the_remote_order(self):
        """我方先接 ⇒ 去对方把那张单退掉（原因沿用既有那句，⛔ 不在本线另写一份）。"""
        other = User.objects.create_user(username="other-taker", password="pw-a")
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(
            user=other
        )
        self._link()

        processed = self._poll(self._taken_upstream())
        self.assertEqual(processed, 1)

        queued = JidanpuSyncEvent.objects.get(
            kind=KIND_WITHDRAW_LITERAL,
            target_ref=ORDER_NO,
            payload__member_binding_id=self.binding.pk,
        )
        self.assertEqual(queued.direction, "out")
        self.assertEqual(queued.status, "pending")
        # ⛔ 我方**没有**接手：别人的单不动
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._main_user_id(self.order), other.pk)
        self.assertFalse(self._audit().exists())
        # 退单原因逐字沿用既有那条线的那句（spec §13-T6 默认沿用）
        self.assertEqual(jidanpu_common.WITHDRAW_REMARK, WITHDRAW_REMARK_LITERAL)

    def test_when_the_order_is_already_ours_we_do_not_withdraw(self):
        """接单人**就是**本绑定的主人（他自己在我方大厅接的）⇒ 不排退单。"""
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(
            user=self.user
        )
        self._link()

        self._poll(self._taken_upstream())
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind=KIND_WITHDRAW_LITERAL, target_ref=ORDER_NO
            ).exists()
        )
        self.assertEqual(self._main_user_id(self.order), self.user.pk)


# ══ ⑦ 失败一律响亮 + 收窄写入 ═════════════════════════════════════════════════


class FailureTests(_Base):
    def test_a_disabled_binding_is_not_polled(self):
        """spec §8.4：停用 ⇒ 快路停跑（一次上游都不打）。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.binding.refresh_from_db()
        up = _Upstream(logs=[])
        with _stub(up):
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 0)
        self.assertEqual(up.names(), [])

    def test_a_failed_log_fetch_is_loud_and_does_not_advance_the_cursor(self):
        self._link()
        up = _Upstream(log_error=ApiError("上游 500", code=None))
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, "")
        self.assertIn("读不到", self.binding.last_error)
        self.assertIsNotNone(self.binding.last_error_at)
        self.assertIsNotNone(self.binding.last_run_at)
        self.assertIsNone(self.binding.last_ok_at)

    def test_an_unreadable_remote_state_is_loud_and_does_not_claim(self):
        """⚠️ 「读不到」≠「远端没接」：⛔ 不猜、不许静默（spec §2.2 的取证纪律）。"""
        self._link()
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: ApiError("上游 500", code=None)},
        )
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self._main_user_id(self.order))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, "")
        self.assertIn("接单人日志", self.binding.last_error)
        self.assertEqual(self._ledger().get().status, "failed")

    def test_an_empty_ass_log_is_a_note_not_a_claim(self):
        """远端日志里没有可用的接单人 ⇒ 只记一条人话台账、下一轮再判（照 B-1 的形状）。"""
        self._link()
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: []},
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=self.now), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        note = self._ledger().get()
        self.assertEqual(note.status, "skipped")
        self.assertFalse(self._audit().exists())

    def test_a_successful_poll_clears_the_previous_error(self):
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            last_error="上一次的旧错", last_error_at=self.now - timedelta(minutes=5)
        )
        self._link()
        up = _Upstream(logs=[])
        with _stub(up):
            jidanpu_member.poll_log(self.binding, now=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertIsNone(self.binding.last_error_at)
        self.assertEqual(self.binding.last_ok_at, self.now)

    def test_poll_never_saves_the_whole_binding_row(self):
        """健康列与游标一律**收窄写入**（`.update()`）；整行 `save()` 会覆盖并发写进去的列。"""
        self._link()
        saved = []
        original = JidanpuMemberBinding.save

        def _spy(instance, *args, **kwargs):
            saved.append(instance.pk)
            return original(instance, *args, **kwargs)

        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)))],
            ass={JID: _taken()},
        )
        with mock.patch.object(JidanpuMemberBinding, "save", _spy):
            with _stub(up):
                jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(saved, [])
