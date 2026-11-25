"""**下家**鸡蛋铺②式（官方 Open Platform API）那条路的判据（回调消费 + 兜底对账 + 健康）——规格 `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §5/§8/§9.4＋§19–§28（总指挥补记，优先级最高）＋任务书「**方案 A：回调只当提醒**」。
本文件守的九条判据（每条都要**能红**，逐条钉在下面各用例的 docstring 里）：①端到端=真接收器落盘**真事件**＋**下家②式**真绑定（`account`/`password` **留空**，⛔ 假密码，E-0202）⇒ **订单行真的变了**（断言数据库行）；②不改下家线状态语义（远端非逐字「已完成」⇒ 一个字不写；我方 `REFUNDED` ⇒ 连权威都不查）；③判据与写面只有一处（`jidanpu_member.apply_remote_completion` / `_apply_member_status`）；④水位用我方 `received_at`（⛔ 报文 `timestamp` 偏 +8 小时）；⑤撞上限只推迟不丢；⑥配不上的每一档都留痕（⛔ 不静默）；⑦入口闸按 `mode` 分（E-0202）；⑧兜底对账低频/逐单/有上限/窗口轮转只推迟不丢；⑨健康列用下家**本表**默认 180 ⇒ 1800 秒（⛔ 上家 600/6000）、「回调在来、水位不动」判红、「低单量」⛔ 不判红。
三条纪律：⛔ 零真实网络（假上游**复用** `tests.test_jidanpu_openapi._Upstream`——两份桩必然漂；地址用 `.invalid` 顶级域，桩没接上就永远解析不了）；⛔ 夹具一律合成值（`AGENTS.md` §5-C / E-0077）；⛔ 断言打在数据库行上（E-0009）。"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.utils import timezone as dj_timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import (
    jidanpu_assignment_lock,
    jidanpu,
    jidanpu_common,
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_member_openapi_outbox,
    jidanpu_openapi,
    jidanpu_openapi_consume,
    jidanpu_openapi_health,
    jidanpu_openapi_reconcile,
    jidanpu_relaunch,
    jidanpu_upstream_pics,
)
from apps.business.api import jidanpu_member_views
#: ⭐ **进程内的间隔锚**（`(租户 schema 名, 绑定主键)` ⇒ 上次跑到点是什么时候；见 `_MEMBER_RECONCILE_AT`的注释）。⚠️ 它们**不随事务回滚**，而每个测试类都会重建 schema ⇒ 主键**各自从 1 开始**⇒ 上一个类留下的锚会命中这一个类的主键（那一轮于是判「没到点」、兜底对账静默不跑）。
#: 照 `tests/test_jidanpu_member_schedule.py` / `test_jidanpu_member_identity_heal.py` 的同一条做法，本模块在 `setUp` 里**逐用例清掉**（否则「某一轮跑没跑对账」取决于类的顺序与主键对齐——静默）。
from apps.business.management.commands.jidanpu_sync import (
    _MEMBER_FAST_AT,
    _MEMBER_RECONCILE_AT,
    _MEMBER_STATUS_PULL_AT,
)
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.callback import signature
from apps.callback import wsgi as callback_wsgi
from apps.core.storage import get_storage
#: ⭐ 复用上一轮那条线的假上游与合成凭据（⛔ 不另写一个假上游：两份桩必然漂）。
from tests.test_jidanpu_openapi import (
    BASE_URL,
    CLIENT_ID,
    MEMBER_BASE_URL,
    ORDER_PROCESS_IMGS_PATH,
    ORDER_STATUS_PATH,
    SENTINEL_SECRET,
    _Upstream,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值（E-0077）────────────────────────────────────────────────────────

#: 三串 22 位合成码（形状 = `[A-Za-z0-9_-]{22}`，照接收器的定长正则）。
CODE_MINE = "Ab3_-memberopenapi_A22"
CODE_UNCLAIMED = "Ef5_-memberopenapi_C22"
#: 第二个**下家**绑定自己那一串码（⚠️ `callback_code` 上那条「两行不许同码」的部分唯一索引
#: 已按业务方新口径**删除**——今天两行同码不但允许、而且是**常态**；
#: 这一串留给「另一条线 / 另一个域名」那类场景用）。
CODE_SECOND = "Ab3_-memberopenapi_B22"
assert len(CODE_MINE) == len(CODE_UNCLAIMED) == len(CODE_SECOND) == 22

SECRET_MINE = "tt-member-openapi-secret-0001"
SECRET_UNCLAIMED = "tt-member-openapi-secret-0003"

#: **另一家店**的下家地址（合成值）：镜像接手那条阳性对照要把「第二份推送证据」放到
#: 另一家店上——按现行口径「接手人是这家店的主人 ⇒ **整店不撤**」（同一家店的第二份
#: 绑定不再收退单，见 `tests/test_member_same_store_withdraw.py`）。
OTHER_MEMBER_BASE_URL = "https://jidanpu-member-second.test.invalid"

#: 密钥文件的**首行注释**（照接收器 spec §5.3 的固定文案；⛔ 实现里不拿它当判据）。
KEYS_HEADER = "# 团团通 · 鸡蛋铺官方 API 回调密钥（0600；由「保存绑定」自动维护；⛔ 人工改要重启接收器）"

#: 下家那侧的远端单号（= 链路行的 `jidanpu_order_id`）与我方单号（20 位）。
JID = "JDP-MEMBER-000000000000000901"
JID_B = "JDP-MEMBER-000000000000000902"
JID_C = "JDP-MEMBER-000000000000000903"
ORDER_NO = "20261008000000000901"
ORDER_NO_B = "20261008000000000902"
ORDER_NO_C = "20261008000000000903"

#: 合成来源 IP（TEST-NET-3 文档保留段，⛔ 不是真值）。
SOURCE_IP = "203.0.113.11"

#: 固定的墙钟（⛔ 不用 `timezone.now()`：水位与「最近收到」的读数要能对着字面值断言）。
FIXED_1 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt_timezone.utc)
FIXED_2 = datetime(2026, 10, 9, 12, 0, 3, tzinfo=dt_timezone.utc)
FIXED_3 = datetime(2026, 10, 9, 12, 0, 6, tzinfo=dt_timezone.utc)
ROUND_NOW = datetime(2026, 10, 9, 13, 0, 0, tzinfo=dt_timezone.utc)

#: 权威状态（`GetOrderStatus` 的 `data.status`）——下家线**只认逐字「已完成」**。
AUTHORITATIVE = "已完成"
#: 回调**报文里**那个 `status`：**刻意**取一个「如果拿它当真就会写错」的值（问题单 ⇒ ISSUE）。
CALLBACK_STATUS = "问题单"
#: 不进「处理集合」的两个我方终态之一（钱的承重那一格）。
TERMINAL_STATUS = str(OrderStatus.REFUNDED)
#: 「远端还没完成」那一档（⛔ 一个字都不许写）。
NOT_COMPLETED = "代练中"
#: 认不出来的状态（任何表里都不会有它）。
UNKNOWN_TOKEN = "外星状态"


class _FrozenClock:
    """接收器 `_now_iso()` 的时钟替身：`datetime.now(tz)` 回一个**固定**时刻。"""

    value: datetime = FIXED_1

    @classmethod
    def now(cls, tz=None):
        return cls.value


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ══ 夹具基类 ═════════════════════════════════════════════════════════════════


class _MemberOpenApiCase(TenantTestCase):
    """临时事件 / 密钥文件 + 假上游 + 真接收器写事件 + **下家②式**绑定 / 我方订单 / 链路。"""

    def setUp(self):
        super().setUp()
        jidanpu_openapi.reset_token_cache()
        jidanpu_openapi_consume.reset_process_state()
        jidanpu_openapi_reconcile.reset_process_state()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.addCleanup(jidanpu_openapi_consume.reset_process_state)
        self.addCleanup(jidanpu_openapi_reconcile.reset_process_state)
        #: ⚠️ 调度那两条间隔锚也是**进程级**状态（见文件头那个 import 的注释）⇒ 逐用例清掉，
        #: 否则「这一轮跑没跑兜底对账」由**上一个测试类的绑定主键**决定（那是**静默**的读数漂移）。
        _MEMBER_FAST_AT.clear()
        _MEMBER_RECONCILE_AT.clear()
        _MEMBER_STATUS_PULL_AT.clear()
        self.addCleanup(_MEMBER_FAST_AT.clear)
        self.addCleanup(_MEMBER_RECONCILE_AT.clear)
        self.addCleanup(_MEMBER_STATUS_PULL_AT.clear)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "shared" / "callback" / "events.jsonl"
        self.keys_file = self.tmp / "shared" / "callback-keys.env"
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        self.keys_file.write_text(
            KEYS_HEADER + "\n"
            + f"{CODE_MINE}\t{SECRET_MINE}\n"
            + f"{CODE_UNCLAIMED}\t{SECRET_UNCLAIMED}\n",
            encoding="utf-8",
        )
        self.keys_file.chmod(0o600)
        env = mock.patch.dict(
            os.environ,
            {"CALLBACK_EVENTS_PATH": str(self.events), "CALLBACK_KEYS_FILE": str(self.keys_file)},
        )
        env.start()
        self.addCleanup(env.stop)

        self.upstream = _Upstream()
        http = mock.patch.object(jidanpu_openapi.httpx, "Client", self.upstream.factory)
        http.start()
        self.addCleanup(http.stop)

        self.game = Game.objects.create(name="测试游戏乙")
        #: ⭐ 每个用例都有一行默认的**下家②式**绑定（`CODE_MINE`）。
        self.binding = self.make_binding()

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_binding(self, *, code=CODE_MINE, name="a", secret=SECRET_MINE, enabled=True,
                     account="", password=""):
        """一行**下家②式**绑定（`JidanpuMemberBinding`）。⭐ **`account` / `password` 默认留空**：那就是界面上②式的真实形态（「我的资料 → 功能设置」那块「下家鸡蛋铺绑定」按②式保存时不填账号密码）。
        ⛔ 不许为了「让它建得上」塞一个假密码 —— 上一轮那条线正是被假密码骗过去的（E-0202）。⚠️ 绑定人**持有 `order.change_status`**（写状态那一步要它）。"""
        username = f"kefu-member-openapi-{name}"
        make_logged_in_client(
            username=username, groups=("客服",), nodes=("order.change_status",),
        )
        return JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=MEMBER_BASE_URL,
            account=account, password=password, mode="openapi",
            client_id=f"{CLIENT_ID}-member-{name}", client_secret=secret,
            callback_code=code, enabled=enabled,
        )

    def make_order(self, *, order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS, **kwargs):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=status, total_price=Decimal("100.00"),
            **kwargs,
        )

    def make_link(self, binding, order, *, remote_id=JID):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id=remote_id,
        )

    def reply_status(self, status=AUTHORITATIVE):
        """假上游：`GetOrderStatus` 回一个**成功**信封（`code=1`，实测只读端点成功码）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None,
            "data": {
                "status": status, "creatTime": "2026-10-09 12:00:00", "operTime": None,
                "doneTime": None, "verifyTime": None,
            },
        })
        #: ⭐ ②式写「待验收」那一步会打 `GetOrderProcessImgs`（B 段：补拉一次全量进度图）⇒ 默认回**空列表** = 「这单没有进度图」（那是**正常**状态，不是失败）。
        #: ⚠️ 不给默认回包的话桩会回 `data=null` ⇒ 客户端判「形状畸形」⇒ 每条用例都会多一条失败台账，那些读数就不是这条用例要钉的东西了（夹具不该造与用例无关的噪声）。
        self.reply_process_imgs(())

    def reply_process_imgs(self, rows):
        """假上游：`GetOrderProcessImgs` 回一组进度图行（实测形状 `{url, uploadTime, name, note}`）。"""
        self.upstream.reply(ORDER_PROCESS_IMGS_PATH, payload={
            "code": 1, "msg": None, "data": list(rows),
        })

    # ── 让**真接收器**写一条事件 ─────────────────────────────────────────────

    def post_callback(self, *, code=CODE_MINE, secret=SECRET_MINE, params=None, path=None,
                      sign=None, received_at=FIXED_1) -> str:
        """走真接收器（`wsgi.application`）打一条 POST ⇒ 返回它的状态行。"""
        body = dict(params if params is not None else self.status_params())
        body[signature.SIGN_FIELD_NAME] = (
            sign if sign is not None else signature.compute_sign(params or self.status_params(), secret)
        )
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": path or f"{callback_wsgi.CALLBACK_PATH_PREFIX}{code}",
            "QUERY_STRING": "",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": BytesIO(raw),
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_X_REAL_IP": SOURCE_IP,
        }
        status: dict = {}

        def start_response(line, headers):
            status["line"] = line

        with mock.patch.object(callback_wsgi, "datetime", _FrozenClock):
            _FrozenClock.value = received_at
            callback_wsgi.application(environ, start_response)
        return status["line"]

    @staticmethod
    def status_params(*, order_no=JID, status=CALLBACK_STATUS, action="10",
                      timestamp="1791500000"):
        """一条 `action=10` 的**回调报文**（合成值；`timestamp` 是按实测 +8 小时的形状给的假值）。"""
        return {
            "orderNo": order_no,
            "sourceOrderNo": ORDER_NO,
            "status": status,
            "note": "",
            "action": action,
            "timestamp": timestamp,
        }

    # ── 跑一轮（回调消费 / 兜底对账）────────────────────────────────────────

    def consume(self, binding=None, *, now=ROUND_NOW, max_orders=None):
        """跑一轮下家②式的回调消费；先断言「记录 > 0」（免得整份用例落在空文件上空洞通过）。"""
        self.assertTrue(
            _read_lines(self.events), "事件文件是空的 ⇒ 这一轮什么都没得处理（夹具坏了）",
        )
        return jidanpu_member_openapi.consume_member_callbacks(
            binding or self.binding, now=now, max_orders=max_orders,
        )

    def reconcile(self, binding=None, *, now=ROUND_NOW, max_orders=None):
        return jidanpu_member_openapi.reconcile_member_statuses(
            binding or self.binding, now=now, max_orders=max_orders,
        )

    # ── 读数 ────────────────────────────────────────────────────────────────

    def order_status(self, order) -> str:
        """**重新查库**取状态（⛔ 不读内存对象：那会把「没写」看成本地那次改动）。"""
        return Order.objects.get(pk=order.pk).status

    def notes(self, *, kind=None):
        rows = JidanpuSyncEvent.objects.all()
        return rows.filter(kind=kind) if kind else rows

    def queried_order_ids(self) -> list[str]:
        """假上游这一轮**真的**收到过哪些 `GetOrderStatus` 的 `orderId`（按发生顺序）。
        ⚠️ 只认状态查询那一条路径：写「待验收」时还会打 `GetOrderProcessImgs`（同一个
        `orderId` 参数）⇒ 不过滤就会把「拉图」算成「又查了一单」。"""
        return [
            row["params"].get("orderId") for row in self.upstream.api_calls()
            if row["path"] == ORDER_STATUS_PATH
        ]

    def refresh(self, binding=None):
        return JidanpuMemberBinding.objects.get(pk=(binding or self.binding).pk)

    def run_a_schedule_round(self):
        """跑**一轮调度**（`jidanpu_sync --once`）⇒ 回刷新后的绑定那一行。
        ⚠️ 判据要打在**真出口**上（`last_error` 是调度那一层盖上去的，`AGENTS.md` §5-G）：
        只断言 `outcome.findings` 证明不了「页面上那一条在不在」。"""
        tenant = mock.Mock(schema_name="tenant-member-openapi")
        with _fake_tenants(tenant):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())
        return self.refresh()


# ══ ① 端到端（这条路唯一「看得见效果」的那一环）════════════════════════════════


class EndToEndTests(_MemberOpenApiCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_a_real_event_changes_the_order_row(self):
        """⭐ **端到端**：真接收器落盘的一条回调 ⇒ 跑一轮 ⇒ 我方订单**真的**变「待验收」。⛔ 弄红它：让消费者不写（去掉 `_apply_member_status` 里那次 `apply_remote_completion`）⇒ 第一条断言红（库里还是「代练中」）。
        ⚠️ 五条断言各自承重：① **数据库行**（E-0009：不看返回值、不看 logger）；② **回调里的 `status` 不作数**（报文说「问题单」，最终必须是「待验收」= 权威值「已完成」的结论）；
        ③ 审计 + 台账**各一行**（写状态在两个出口上都要留痕）；④ 水位 / 健康列 = **我们自己的接收时刻**（⛔ 不是报文里那个偏 +8 小时的 `timestamp`）；⑤ 打出去的那一发**打到这一行自己的地址**上、参数是 `orderId=<链路那一列>`。"""
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        # ① 订单状态**真的变了**（重新查库）
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")
        self.assertNotEqual(self.order_status(self.order), "ISSUE")  # 回调里那个值不许作数
        self.assertEqual(outcome.applied, 1)
        # ② 审计：既有动作码 `order.transition`，前后值 = 中文 label、actor = 下家线的机器名
        audit = OperationLog.objects.filter(order_no=ORDER_NO, action="order.transition")
        self.assertEqual(audit.count(), 1)
        row = audit.get()
        self.assertEqual((row.from_value, row.to_value), ("代练中", "待验收"))
        self.assertEqual(row.actor, jidanpu_member.MACHINE_ACTOR)
        self.assertIsNone(row.actor_id)  # 机器行（与慢路那条线逐字同形）
        # ③ 台账：一条「已写」的留痕（下家线**既有**的那个 kind）
        done = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO, status="done")
        self.assertEqual(done.count(), 1)
        self.assertIn("已完成", done.get().reason)
        # ④ 水位 / 健康列
        fresh = self.refresh()
        self.assertEqual(fresh.callback_watermark_at, FIXED_1)
        self.assertEqual(fresh.callback_last_at, FIXED_1)
        self.assertEqual(fresh.callback_count, 1)
        # ⑤ 真打了权威那一次 **＋ B 段那一次全量进度图**（起：写「待验收」之前
        #    用官方 API 补拉一次图，**替代**②式下被跳过的 `ViewPic`），两次都打到**这一行自己的地址**
        self.assertEqual(
            sorted(call["path"] for call in self.upstream.api_calls()),
            sorted([ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH]),
        )
        for call in self.upstream.api_calls():
            with self.subTest(path=call["path"]):
                self.assertEqual(call["params"], {"orderId": JID})
                self.assertEqual(call["method"], "GET")
                self.assertTrue(call["url"].startswith(MEMBER_BASE_URL), call["url"])
                self.assertNotIn(BASE_URL, call["url"])

    def test_the_consumer_uses_the_member_lines_own_judgment(self):
        """⭐ **判据与写面只有一处**：②式那条路走的是**慢路回拉那同一个**函数。⛔ 弄红它：在本模块里另写一段「判 + 写」（例如自己判 `status == "已完成"` 再自己调 `apply_transition`）⇒ 本用例的调用记录为空 ⇒ 红。
        ⚠️ 同时钉住两个**只在②式下成立**的入参：`trigger` 必须是 `callback`（人话才说得对：「推来一条回调」），`client` 必须是 `None`（②式**没有**①式的登录凭据 ⇒ 拉图那一段跳过，见那个函数的 docstring）。"""
        self.assertEqual(self.post_callback(), "200 OK")
        seen: list[dict] = []
        real = jidanpu_member.apply_remote_completion

        def _spy(**kwargs):
            seen.append(kwargs)
            return real(**kwargs)

        with mock.patch.object(jidanpu_member, "apply_remote_completion", side_effect=_spy):
            self.consume()

        self.assertEqual(len(seen), 1, "②式那条路没有走慢路那一段「判 → 写」")
        call = seen[0]
        self.assertEqual(call["trigger"], jidanpu_member.TRIGGER_CALLBACK)
        self.assertIsNone(call["client"])
        self.assertEqual(call["remote_status"], AUTHORITATIVE)
        self.assertEqual(call["link"].jidanpu_order_id, JID)

    def test_an_unexpected_exception_in_the_seam_is_not_swallowed(self):
        """未预期异常照旧**响亮地冒出去**（⛔ 不伪装成「本轮什么都没发生」）。
        ⚠️ 本用例是上面那条桩的**对照**：桩接没接上、异常有没有被吞，两者都要能分辨
        （`AGENTS.md` §5-E：桩接没接上本身要有判据）。"""
        self.assertEqual(self.post_callback(), "200 OK")
        with mock.patch.object(
            jidanpu_member, "apply_remote_completion", side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self.consume()


# ══ ② 不改变下家线的状态语义（⛔ 一个字都不许乱写）═════════════════════════════


class SemanticsTests(_MemberOpenApiCase):
    def test_a_remote_status_that_is_not_completed_writes_nothing(self):
        """⭐ **判据②**：远端**不是**逐字「已完成」⇒ 一个字都不写（订单、审计、台账都没有）。⛔ 弄红它：把 `apply_remote_completion` 里那句 `!= "已完成"` 去掉（或改成`in ("已完成", "代练中")`）⇒ 订单状态当场变「待验收」⇒ 第一条断言红。
        ⚠️ 也不留台账（照既有口径：「远端还没完成」是**常态**，逐条留痕会把台账淹掉）⇒ 判据打在**数据库行**上。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertFalse(OperationLog.objects.filter(order_no=ORDER_NO).exists())
        self.assertEqual(self.notes(kind="member.status_in").count(), 0)
        # 水位照推（这一条**有结论**：权威说还没完成）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)

    def test_an_unknown_authoritative_status_never_writes_and_is_registered(self):
        """权威值认不出来 ⇒ ⛔ 不写、⛔ 不猜成问题单，但要留一条待人工核对的台账。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status(UNKNOWN_TOKEN)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertNotEqual(self.order_status(order), "ISSUE")
        rows = self.notes(kind="member.openapi_unknown_status")
        self.assertEqual(rows.count(), 1)
        self.assertIn(UNKNOWN_TOKEN, rows.get().reason)
        self.assertEqual(rows.get().payload["member_binding_id"], self.binding.pk)
        self.assertEqual(outcome.failed, 1)

    def test_an_already_pending_review_order_is_left_alone(self):
        """⭐ **判据③**：我方**已经是**待验收 ⇒ 一个字都不写（幂等），且**不写台账**（不刷屏）。"""
        order = self.make_order(status=OrderStatus.PENDING_REVIEW)
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self.order_status(order), "PENDING_REVIEW")
        self.assertFalse(OperationLog.objects.filter(order_no=ORDER_NO).exists())
        self.assertEqual(self.notes(kind="member.status_in").count(), 0)

    def test_a_refunded_order_is_not_even_queried(self):
        """⭐ **判据①是钱的承重那一格**：我方**已退单**的单 ⇒ 连权威都**不查**（省一次上游读）。
        ⛔ 弄红它：把处理集合的判据（`jidanpu_member.pullable_links`）放宽成「不排除终态」⇒ 这一张退掉的单会被写回「待验收」= **复活一张退单**（退款那条路是裸 `update()`、跑不到退款结清 ⇒ 钱会算错），同时 `api_calls()` 不为空 ⇒ 两条断言同时红。"""
        order = self.make_order(status=OrderStatus.REFUNDED)
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        self.assertEqual(self.order_status(order), TERMINAL_STATUS)
        self.assertEqual(self.upstream.api_calls(), [], "已退单的单竟然去查了权威状态")
        self.assertEqual(outcome.skipped, 1)
        skipped = self.notes(kind="member.status_in").filter(status="skipped")
        self.assertEqual(skipped.count(), 1)
        self.assertIn("不进处理集合", skipped.get().reason)

    def test_the_b1_gate_blocks_an_order_that_slipped_into_the_set(self):
        """⭐ **判据④（写前第二道闸）**：用**库里最新那一行**再跑一次 B-1 的挡住集合。⚠️ 现实形态（为什么必须有这道闸）：处理集合是**找链路之前**筛的，而「筛 → 查权威 →写」之间隔着一次网络往返 ⇒ 那一单可能刚好被结账 / 退单。这里用**把取集合那一步放宽**来复现那个时差（`pullable_links` 换成不带判据①的查询），⛔ 不是改被测代码的判据。
        ⛔ 弄红它：删掉 `_b1_status_rollback_blocked` 那一跳 ⇒ 一张**已退单**的单被写成「待验收」⇒ 第一条断言红。"""
        order = self.make_order(status=OrderStatus.REFUNDED)
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        def _wide(_binding):
            return JidanpuMemberOrderLink.objects.filter(binding=_binding)

        with mock.patch.object(jidanpu_member, "pullable_links", _wide):
            self.consume()

        self.assertEqual(self.order_status(order), TERMINAL_STATUS)
        self.assertFalse(OperationLog.objects.filter(order_no=ORDER_NO).exists())
        blocked = self.notes(kind="member.status_in").filter(status="skipped")
        self.assertEqual(blocked.count(), 1)
        self.assertIn("挡住", blocked.get().reason)

    def test_a_soft_deleted_order_is_skipped_instead_of_crashing(self):
        """已软删的单：判据①把它挡在集合外 ⇒ 留一条 `skipped` 的痕，⛔ 不抛异常。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        Order.objects.filter(pk=order.pk).update(deleted_at=FIXED_1)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self.upstream.api_calls(), [])
        skipped = self.notes(kind="member.status_in").filter(status="skipped")
        self.assertEqual(skipped.count(), 1)
        self.assertIn("软删", skipped.get().reason)


# ══ ③ 水位（用我们自己的 `received_at`）════════════════════════════════════════


class WatermarkTests(_MemberOpenApiCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_the_same_file_is_consumed_exactly_once(self):
        """同一份事件跑两轮 ⇒ 只处理一次（第二轮一次上游都不打、审计只有一行）。"""
        self.assertEqual(self.post_callback(), "200 OK")
        self.consume()

        before = len(self.upstream.requests)
        second = self.consume()

        self.assertEqual(second.mine, 0)
        self.assertEqual(len(self.upstream.requests), before, "第二轮又打了上游")
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 1)
        self.assertEqual(self.refresh().callback_count, 1)

    def test_the_event_timestamp_never_steers_the_watermark(self):
        """⭐ **水位用我们自己的 `received_at`**（报文里那个 `timestamp` 偏 +8 小时，F19）。
        ⛔ 弄红它：把 `_event_time` 换成读报文里的 `timestamp` ⇒ 水位变成 2026-10-09 **20:00**
        那一档（+8 小时）⇒ 第一条断言红。"""
        self.assertEqual(
            self.post_callback(params=self.status_params(timestamp="1791600000")), "200 OK",
        )
        self.consume()
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)
        self.assertNotEqual(
            self.refresh().callback_watermark_at,
            datetime(2026, 10, 9, 20, 0, 0, tzinfo=dt_timezone.utc),
        )

    def test_a_failed_query_keeps_the_watermark_and_the_next_round_finishes_it(self):
        """⭐ **一条都没处理成 ⇒ 水位不动**（下一轮从同一条重试）。"""
        self.upstream.boom(ORDER_STATUS_PATH, RuntimeError("upstream down"))
        self.assertEqual(self.post_callback(), "200 OK")

        first = self.consume()

        self.assertEqual(first.failed, 1)
        self.assertIsNone(self.refresh().callback_watermark_at, "一条都没处理成，水位却动了")
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        self.assertEqual(self.notes(kind="member.openapi_query_failed").count(), 1)

        # 上游恢复 ⇒ 下一轮把同一条处理掉（⛔ 没有丢）
        self.upstream.errors.clear()
        self.reply_status()
        second = self.consume()

        self.assertEqual(second.applied, 1)
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)

    def test_a_partial_batch_advances_the_watermark_to_just_before_the_first_unprocessed(self):
        """⭐ **中途停下** ⇒ 水位推到「第一条没处理的前一微秒」（⛔ 不用「最大已处理」）。⚠️ 为什么用**前一微秒**：两条事件的 `received_at` 可以是**同一微秒**（接收器的钟分辨率）⇒ 拿「最大已处理」当水位，会让同一微秒里**排在后面**那一条**永远**轮不到（本仓最恨的静默丢）。本用例刻意把两条放在同一微秒上把这一格钉死。
        ⛔ 弄红它：把 `blocked_at - timedelta(microseconds=1)` 改成 `max(processed_times)`⇒ 水位 = FIXED_1 ⇒ 第二条（同一微秒）下一轮**仍然**进不了 pending ⇒ 红。"""
        order_b = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, order_b, remote_id=JID_B)
        self.reply_status()
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.assertEqual(
            self.post_callback(params=self.status_params(order_no=JID_B), received_at=FIXED_1),
            "200 OK",
        )

        #: 第 2 发查不动（第 1 发正常）⇒ 「第 1 条处理成、第 2 条停住」
        calls = {"n": 0}
        real = jidanpu_openapi.OpenApiClient.get_order_status

        def _flaky(client_self, *, order_id):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("down")
            return real(client_self, order_id=order_id)

        with mock.patch.object(jidanpu_openapi.OpenApiClient, "get_order_status", _flaky):
            outcome = self.consume()

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(outcome.failed, 1)
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")
        self.assertEqual(self.order_status(order_b), "IN_PROGRESS")
        self.assertEqual(
            self.refresh().callback_watermark_at, FIXED_1 - timedelta(microseconds=1),
        )

        # ⭐ 下一轮：同一微秒的那一条**真的**被处理到（水位那一格没把它吃掉）
        second = self.consume()
        self.assertEqual(second.applied, 1)
        self.assertEqual(self.order_status(order_b), "PENDING_REVIEW")
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)


# ══ ③″ ⭐⭐ 同族第 3 次：「对方明确说没有这张单」⇒ 终态收口 + **水位必须前进** ══════════
# 真机现场（kexing）：对方回 `code=0`「没有相关订单信息」那一档被算作**失败**⇒ 那一条回调**永远**失败 ⇒ 水位钉在它上面 ⇒ **它后面所有回调都不被处理**（堆了 13865 条）。⇒ 判据必须打在**水位前进**上（下面第一条）；反向控制打在「真失败 ⇒ 水位不动」（第三条）。
# ⚠️ 判据只有一处（`jidanpu_openapi.absent_order_error` = `find_order_status` 的同一个语义），⛔ 不解析 `msg`。

#: ⭐ 真机那一格的**逐字读数**（只读端点成功码是 `code>0` ⇒ `code=0` 是失败信封）。
ABSENT_ENVELOPE = {"code": 0, "msg": "没有相关订单信息", "data": ""}


class AbsentRemoteOrderTests(_MemberOpenApiCase):
    """对方明确说「没有这张单」= **终态**（⛔ 不重试）**且水位前进**；其余失败照旧钉住水位。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)

    def reply_absent(self):
        """假上游：`GetOrderStatus` 回**真机那一格**（`code=0` ＋ `msg` ＋ `data` 是**空串**）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload=dict(ABSENT_ENVELOPE))

    def test_a_callback_retries_after_the_status_query_parameter_error_clears(self):
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 0, "msg": "系统单号和来源单号不可同时为空", "data": "",
        })
        self.assertEqual(self.post_callback(), "200 OK")

        first = self.consume()
        self.assertEqual((first.failed, first.skipped), (1, 0))
        self.assertIsNone(self.refresh().callback_watermark_at)
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")

        self.reply_status()
        second = self.consume()
        self.assertEqual(second.applied, 1)
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")

    def test_a_contradictory_absence_envelope_keeps_the_reconcile_retryable(self):
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 0, "msg": "没有相关订单信息", "data": {"status": NOT_COMPLETED},
        })
        first = self.reconcile()
        self.assertTrue(first.stopped)
        self.assertEqual(first.skipped, 0)
        self.assertTrue(first.findings)
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")

        self.reply_status()
        second = self.reconcile()
        self.assertEqual(second.applied, 1)
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")

    def test_the_peer_saying_there_is_no_such_order_is_terminal_and_moves_the_watermark(self):
        """⭐⭐ **判据本体**：造一条「查不到单」的回调 ⇒ 消费一轮 ⇒ **水位推到它之后**。
        ⛔ 弄红它（两条各自能红）：把 `_apply_member_status` 里那一支删掉（落回「查不动」）⇒ 水位断言 + `skipped` 断言当场红（真机上就是水位卡在 2026-10-08、后面 13865 条全不处理）；把这一支并进「撞上限」/「远端不是已完成」⇒ 台账那条断言红（⛔ 不许静默）。"""
        self.reply_absent()
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        #: ① 订单**一个字都没写**（⛔ 不猜状态、⛔ 不留审计）
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 0)
        self.assertEqual(outcome.applied, 0)
        #: ② ⭐ **有结论**（终态收口）而不是「失败」：⛔ 不许再计进 `failed`
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.failed, 0)
        #: ③ ⭐⭐ **水位前进**（本轮唯一那条判据；真机上就是这一格不动 ⇒ 整条管道卡死）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)
        #: ④ ⛔ 不静默：一条 `skipped` 的人话台账，而且**不是**「查权威失败」那一档
        rows = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO)
        self.assertEqual(rows.count(), 1, "对方明确了结论，却一行痕都没有")
        self.assertIn("没有这张单", rows.get().reason)
        self.assertEqual(rows.get().status, "skipped")
        self.assertEqual(self.notes(kind="member.openapi_query_failed").count(), 0)
        #: ⑤ 这一档**不进错误列**（有结论的终态 ⇒ `last_error` 不许常年挂一句假话）
        self.assertEqual(outcome.findings, ())
        #: ⑥ 自证夹具：这一轮**真的**打了上游（否则「没有这张单」可能来自桩没接上）
        self.assertEqual(
            len([r for r in self.upstream.api_calls() if r["path"] == ORDER_STATUS_PATH]), 1,
        )

    def test_the_backlog_behind_a_stuck_watermark_flows_through_without_any_manual_action(self):
        """⭐⭐ 真机现场**逐件复现**：水位卡住（`2026-10-08 14:33:33`）＋ 台账里已经有**2 条** `member.openapi_query_failed` ＋ 它后面堆着一批没被处理过的回调。
        ⛔ 这条用例要回答的是**已有数据怎么办**（不需要任何一次性 SQL / 人工动作）：那 2 条历史台账**不参与**水位（水位只由 `callback_watermark_at` + 事件文件决定）⇒ 新逻辑下一轮就会：①把「对方说没有这张单」那一条收口成终态、②把水位推过去、
        ③把它**后面**那一条真的处理掉。⚠️ 历史那 2 行**一个字都不删**（台账是只增的历史）。夹具：第一条（`JID`）= 对方没有这张单；第二条（`JID_B`）的我方单**已终态**⇒ 按判据①它**根本不查权威**（零上游读）⇒ 「水位**越过了**第一条」这件事由 `skipped == 2` ＋ **上游只被打了 1 次**两件事一起钉住（⛔ 不是靠计数蒙的）。"""
        stuck_at = FIXED_1 - timedelta(seconds=1)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            callback_watermark_at=stuck_at
        )
        stale_reason = jidanpu_member_openapi.REASON_QUERY_FAILED.format(
            detail="OpenApiError（没有相关订单信息）",
        )
        for index in (1, 2):
            jidanpu_common.write_event(
                direction=JidanpuSyncEvent.Direction.IN, kind="member.openapi_query_failed",
                status="failed", target_type="callback", target_ref=str(self.binding.pk),
                reason=f"{stale_reason}（历史第 {index} 条）",
                payload={"member_binding_id": self.binding.pk, "order_no": JID,
                         "trigger": "callback"},
            )
        terminal = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.REFUNDED)
        self.make_link(self.binding, terminal, remote_id=JID_B)
        self.reply_absent()
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.assertEqual(
            self.post_callback(params=self.status_params(order_no=JID_B), received_at=FIXED_2),
            "200 OK",
        )

        outcome = self.consume()

        self.assertEqual(outcome.mine, 2)
        self.assertEqual(outcome.skipped, 2, "第一条之后那一条没有被处理（本轮被停住了）")
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(
            len([r for r in self.upstream.api_calls() if r["path"] == ORDER_STATUS_PATH]), 1,
            "已终态那条按判据①不该查权威 ⇒ 恰好多打了一次就说明分档错了",
        )
        #: ⭐ 水位**推到了这一批之后**（那一条卡住的记录真的越过去了）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_2)
        #: ⛔ 历史那 2 行一个字都没动（既不删也不重写），而且没收口成第 3 行
        self.assertEqual(self.notes(kind="member.openapi_query_failed").count(), 2)

    def test_the_reconcile_window_rotates_instead_of_stopping_on_such_an_order(self):
        """⭐ 兜底对账那一路**共用同一段**（`_apply_member_status`）⇒ 同样必须收口。
        ⛔ 弄红它：删掉那一支 ⇒ `status_of` 抛 / 回 `RETRY` ⇒ `stopped=True`，窗口**永远**停在这一单上 ⇒ 它后面那些在途单一条都查不到（与回调那条水位是同一个病）。⚠️ 人话按触发方式分：对账这一路**没有水位**，说「水位前移」就是一句假话。"""
        self.reply_absent()

        outcome = self.reconcile()

        self.assertFalse(outcome.stopped, "对账窗口被这一单钉住了")
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.failed, 0)
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        rows = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO)
        self.assertEqual(rows.count(), 1)
        self.assertIn("没有这张单", rows.get().reason)
        self.assertIn("对账窗口", rows.get().reason)
        self.assertNotIn("水位", rows.get().reason)

    def test_every_other_kind_of_failure_still_keeps_the_watermark(self):
        """⭐⭐ **反向控制**：网络 / 5xx / 没 `code` / 权限档 / 协议错 / **取 token** 那一步⇒ **必须照旧算失败、水位照旧不动**（⛔ 绝不当作「对方没有这张单」）。
        ⚠️ 每一条各自造一份**新的事件文件**：水位一直不动（这是**要钉住**的性质），旧事件会一直留在 pending 里 ⇒ 共用一份文件时，第二轮停住的永远是**第一条**（= 只有第一个 case 真的被跑到，其余全是假绿）。
        ⛔ 弄红它：把 `absent_order_error` 的任一条判据放宽（例如去掉 `step` 那一格）⇒ 「取 token」那一条当场红。"""
        token_business_failure = {"code": 0, "msg": "系统繁忙", "data": ""}
        not_json = "{这不是 JSON"
        cases = (
            ("传输层连不上", lambda: self.upstream.boom(
                ORDER_STATUS_PATH, httpx.ConnectError("合成连接失败"))),
            ("HTTP 5xx", lambda: self.upstream.reply(
                ORDER_STATUS_PATH, status=503, payload={"code": None, "msg": "系统维护"})),
            ("HTTP 200 但报文里没有 code", lambda: self.upstream.reply(
                ORDER_STATUS_PATH, payload={"msg": "上游改版了", "data": {}})),
            ("权限档", lambda: self.upstream.reply(
                ORDER_STATUS_PATH, payload={"code": 0, "msg": "没有权限", "data": ""})),
            ("data 里没有 status（协议形状错）", lambda: self.upstream.reply(
                ORDER_STATUS_PATH, payload={"code": 1, "msg": None, "data": {}})),
            ("响应不是 JSON", lambda: self.upstream.reply(
                ORDER_STATUS_PATH, payload=not_json)),
            ("取 token 那一步的业务失败", lambda: self.upstream.token_reply(
                status=200, payload=dict(token_business_failure))),
        )
        for index, (label, configure) in enumerate(cases):
            with self.subTest(label=label):
                self._reset_upstream()
                #: ⚠️ token 缓存是**进程级**的：不清掉的话第 7 条压根不会去打 token 那一步。
                jidanpu_openapi.reset_token_cache()
                #: ⚠️ 换一份**新的事件文件**（连 `self.events` 一起换：`consume()` 用它自证
                #:    「这一轮真的有东西可处理」——只改环境变量的话那句自证会打在旧那份空文件上）。
                self.events = self.tmp / f"fresh-{index}" / "events.jsonl"
                os.environ["CALLBACK_EVENTS_PATH"] = str(self.events)
                configure()
                #: ⚠️ 每条 case 用**不同的 `received_at`**：回调失败计次按「事件」算
                #: （`received_at` + `body_sha256` 是身份）——7 条 case 的报文完全相同，若都落同一微秒，它们会被正确地认成**同一条事件反复失败**（第 5 条就放弃）。这里要测的是「每一种失败都各自停住水位」，不是「同一条坏事件的重试上限」。
                self.assertEqual(
                    self.post_callback(received_at=FIXED_1 + timedelta(seconds=index)),
                    "200 OK",
                )

                outcome = self.consume()

                self.assertEqual(outcome.failed, 1, f"{label}：这一档必须算失败")
                self.assertEqual(outcome.skipped, 0, f"{label}：被当成终态收口了")
                self.assertIsNone(
                    self.refresh().callback_watermark_at, f"{label}：真失败却把水位推走了",
                )
                self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
                self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 0)
                self.assertGreaterEqual(
                    self.notes(kind="member.openapi_query_failed").count(), 1,
                    f"{label}：查不动必须留痕",
                )
                self.assertEqual(
                    self.notes(kind="member.status_in").count(), 0,
                    f"{label}：真失败被写成了终态台账",
                )

    def _reset_upstream(self):
        """把假上游清回出厂状态（每条 case 各自造自己的形态，⛔ 不许上一条的桩漏到这一条）。"""
        self.upstream.errors.clear()
        self.upstream.routes.clear()
        self.upstream.token_status = 200
        self.upstream.token_payload = None
        self.upstream.expires_in = 3600


# ══ ④ 只推迟、不丢（撞上限）════════════════════════════════════════════════════


class BatchCapTests(_MemberOpenApiCase):
    def test_the_per_round_cap_defers_but_never_drops(self):
        """⭐ **撞上限只推迟不丢**：三张单、本轮只查两张 ⇒ 水位停在那两张之后，下一轮查第三张。
        ⛔ 弄红它：撞上限时把水位推到**本批最大**（= 丢掉第三张）⇒ 「第二轮终于写了」那条红。"""
        orders = []
        for index, (order_no, remote_id) in enumerate(
            ((ORDER_NO, JID), (ORDER_NO_B, JID_B), (ORDER_NO_C, JID_C))
        ):
            order = self.make_order(order_no=order_no)
            self.make_link(self.binding, order, remote_id=remote_id)
            orders.append(order)
            self.assertEqual(
                self.post_callback(
                    params=self.status_params(order_no=remote_id),
                    received_at=(FIXED_1, FIXED_2, FIXED_3)[index],
                ),
                "200 OK",
            )
        self.reply_status()

        first = self.consume(max_orders=2)

        self.assertEqual(first.mine, 3)
        self.assertEqual(first.applied, 2)
        self.assertEqual(first.failed, 1)
        self.assertEqual(self.order_status(orders[0]), "PENDING_REVIEW")
        self.assertEqual(self.order_status(orders[1]), "PENDING_REVIEW")
        self.assertEqual(self.order_status(orders[2]), "IN_PROGRESS")
        # 水位停在「第一条没处理的那一条」的前一微秒（= 第 3 条的时刻 ⇒ 不影响已处理的两条）
        self.assertEqual(
            self.refresh().callback_watermark_at, FIXED_3 - timedelta(microseconds=1),
        )

        second = self.consume(max_orders=2)

        self.assertEqual(self.order_status(orders[2]), "PENDING_REVIEW", "第三条被悄悄丢了")
        self.assertEqual(second.applied, 1)
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_3)


# ══ ④′ 「值不可读」与「撞上限」是**两档**（处置相反，⛔ 不许并成一档）══════════════


class MissingValueTests(_MemberOpenApiCase):
    """⭐ 判据（整线评审点名的那一处）：**权威值不可读** ⇏ **撞上限**。
    两档的处置**相反**：值不可读 ⇒ 有结论（「不写」）⇒ 留台账 + **水位前移**；
    撞上限 ⇒ 没结论 ⇒ 只推迟 + **水位不动**。⛔ 把它们并成一档就是「说假话的留痕」。"""

    def test_a_blank_authoritative_status_is_noted_as_missing_and_the_watermark_moves(self):
        """⭐ 权威值**只含空白**（= 这一轮拿不到可读的值）⇒ 走「没有给出可读的状态」那条台账，⛔ 一个字都不写订单，而**水位照常前移**（⛔ 不是「下一轮重试」）。
        ⚠️ 这一档**到得了**（⛔ 不是死代码）：客户端的判据是「**非空**字符串」⇒ 只含空白的`status` 过得去（`jidanpu_openapi.py` 的判空那一支），到 `_apply_member_status` 里被 `strip()` 归一化成空。（「`data` 里**根本没有** `status`」是**另一档**：客户端抛⇒ 走「查不动」，水位不前移，见 `test_a_failed_query_keeps_the_watermark_...`。）
        ⛔ 弄红它：把这一档并进「撞上限」（`if remote_status is None or not str(remote_status).strip():`⇒ 回 `OUTCOME_RETRY`）⇒ 水位不动、台账也没有 ⇒ 本用例红。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status(status="   ")
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        #: ① 订单**一个字都没写**（⛔ 也不猜成问题单、⛔ 不留审计）
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 0)
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 1)
        #: ② 台账那一条**是这一档**（判据打在措辞上，⛔ 不是「有没有留痕」）
        rows = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO)
        self.assertEqual(rows.count(), 1, "「值不可读」那一档必须留痕（⛔ 不许静默）")
        self.assertIn("没有给出可读的状态", rows.get().reason)
        self.assertNotIn("上限", rows.get().reason)
        self.assertEqual(rows.get().status, "skipped")
        #: ③ 水位**正常前移**（这一轮**有**结论 ⇒ 这一条事件不许把水位钉住）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)
        #: ④ 这一档**不进**错误列（正常结论；留痕出口是台账那一行）
        self.assertEqual(outcome.findings, ())

    def test_the_cap_and_a_blank_value_leave_opposite_watermarks(self):
        """⭐ 两档**分别**弄红：撞上限 ⇒ 水位**不动** + 进错误列；值不可读 ⇒ 留台账 + 水位**前移**。
        ⚠️ 上限用 `max_orders=0` **真的**撞（⛔ 不接桩 `status_of`：那会把被测的分档逻辑
        一起换成桩的行为，等于什么都没证）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status(status="   ")
        self.assertEqual(self.post_callback(), "200 OK")

        capped = self.consume(max_orders=0)

        self.assertEqual(capped.applied, 0)
        self.assertIsNone(self.refresh().callback_watermark_at, "撞上限却把水位推走了")
        self.assertEqual(
            self.notes(kind="member.status_in").count(), 0, "撞上限那一档不该留下订单台账",
        )
        self.assertIn("上限", capped.findings[0])  # 要人看见（⇒ 绑定 `last_error`）
        self.assertEqual(self.order_status(order), "IN_PROGRESS")

        #: 下一轮上限放开 ⇒ **同一条**事件被处理成「值不可读」那一档（⛔ 没有丢）
        missing = self.consume()

        self.assertEqual(missing.skipped, 1)
        self.assertEqual(missing.findings, ())
        self.assertIn(
            "没有给出可读的状态",
            self.notes(kind="member.status_in").filter(target_ref=ORDER_NO).get().reason,
        )
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)


# ══ ⑤ 配不上的每一档都要留痕（⛔ 一律不许静默）════════════════════════════════


class UnmatchedTests(_MemberOpenApiCase):
    def test_an_event_that_failed_the_signature_never_touches_the_order(self):
        """验签没过 ⇒ **绝不改单**（连正文都不解析），但留一条台账 + 水位照推。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.assertEqual(self.post_callback(sign="deadbeef"), "401 Unauthorized")

        outcome = self.consume()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.notes(kind="member.openapi_unsigned").count(), 1)
        self.assertEqual(outcome.failed, 1)
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)

    def test_a_notification_that_is_not_a_status_change_is_only_registered(self):
        """不是 `action=10`（图片 / 删除 / 账单）⇒ 只登记、不改单（第一期只消费状态）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.assertEqual(
            self.post_callback(params=self.status_params(action="40")), "200 OK",
        )

        outcome = self.consume()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.upstream.api_calls(), [])
        rows = self.notes(kind="member.openapi_other_action")
        self.assertEqual(rows.count(), 1)
        self.assertIn("40", rows.get().reason)
        self.assertEqual(outcome.skipped, 1)

    def test_an_event_without_a_link_never_writes_and_leaves_a_trace(self):
        """本绑定名下没有这条链路 ⇒ ⛔ 不建单、不改任何订单，只留痕。"""
        other = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, other, remote_id=JID_B)
        self.assertEqual(self.post_callback(), "200 OK")  # orderNo = JID，没有链路

        outcome = self.consume()

        self.assertEqual(self.order_status(other), "IN_PROGRESS")
        self.assertEqual(self.upstream.api_calls(), [])
        rows = self.notes(kind="member.openapi_unknown_order")
        self.assertEqual(rows.count(), 1)
        self.assertIn(JID, rows.get().reason)
        self.assertEqual(rows.get().payload["member_binding_id"], self.binding.pk)
        self.assertEqual(outcome.failed, 1)

    def test_a_code_no_enabled_binding_claims_is_loudly_registered(self):
        """⭐ 事件里的码**没有任何启用中的②式绑定**认领 ⇒ 必须由**别的**启用绑定报出来。
        ⚠️ 本用例的码写的是**没有对应绑定**的那一串 ⇒ 报出来的是「没人认领」那一档。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.assertEqual(
            self.post_callback(code=CODE_UNCLAIMED, secret=SECRET_UNCLAIMED), "200 OK",
        )

        outcome = self.consume()

        self.assertEqual(outcome.unclaimed, 1)
        self.assertEqual(outcome.mine, 0)
        rows = self.notes(kind="member.openapi_unclaimed")
        self.assertEqual(rows.count(), 1)
        self.assertIn(CODE_UNCLAIMED, rows.get().reason)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")

    def test_a_code_claimed_by_the_other_line_is_not_reported_as_unclaimed(self):
        """⭐ **码是接收器那一侧的共享命名空间**：另一条线（上家）认领着的码 ⛔ 不许报「没人认领」。
        ⛔ 弄红它：把 `_claimed_codes` 改回「只看自己那张绑定表」⇒ 本用例红
        （下家这一把会把**上家**的码报成没人认领，而那是**假警报**）。"""
        from apps.business.models import JidanpuUpstreamBinding

        other_code = "Hk6_-sharednamespa_B22"
        assert len(other_code) == 22
        make_logged_in_client(username="kefu-shared-ns", groups=("客服",))
        JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username="kefu-shared-ns"), jidanpu_base_url=BASE_URL,
            account="acc-shared-ns", password="pw-synthetic", mode="openapi",
            client_id=f"{CLIENT_ID}-shared-ns", client_secret=SENTINEL_SECRET,
            callback_code=other_code, enabled=True,
        )
        self.keys_file.write_text(
            KEYS_HEADER + "\n" + f"{CODE_MINE}\t{SECRET_MINE}\n" + f"{other_code}\t{SENTINEL_SECRET}\n",
            encoding="utf-8",
        )
        self.assertEqual(self.post_callback(code=other_code, secret=SENTINEL_SECRET), "200 OK")

        outcome = self.consume()

        self.assertEqual(outcome.unclaimed, 0)
        self.assertEqual(self.notes(kind="member.openapi_unclaimed").count(), 0)

    def test_a_query_failure_writes_nothing_and_keeps_the_watermark(self):
        """⭐ **查权威失败 ⇒ 一个字都不写 + 水位不前移**（回调只当提醒，不当真相）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.assertEqual(self.post_callback(), "200 OK")

        self.upstream.boom(ORDER_STATUS_PATH, RuntimeError("down"))
        outcome = self.consume()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(outcome.failed, 1)
        self.assertIsNone(self.refresh().callback_watermark_at)
        rows = self.notes(kind="member.openapi_query_failed")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(self.notes(kind="member.status_in").count(), 0)


# ══ ⑤-2 ⭐ 同一个回调码下有**多个打手**（业务方裁定）════════════════════
# 新口径：**同一个租户 + 同一个 `jidanpu_base_url` 共用一个回调码**（同一个下家鸡蛋铺系统只有一处回调地址设置项）⇒「一个码下有多条绑定」是**常态**，而回调报文里只带一个 `orderNo`。⇒ 落地面必须**在一整组里按单号定位**（`jidanpu_member_openapi._link_of`），
# ⛔ 不是「只看自己那一行」——后者会把**别人家的单**报成「本绑定名下没有这条链路」（**假警报**）。⚠️ 夹具：基类的 `make_binding` 用的都是**同一个** `MEMBER_BASE_URL` ⇒ 传同一个 `code`造出来的就是新口径下的真实形态（两条绑定共用一个码）。


class SharedCodeGroupTests(_MemberOpenApiCase):
    """⭐ 一个码下两条绑定：回调靠**单号**落到**正确的那条**上。"""

    def _two_members_sharing_one_code(self):
        """两条②式绑定（**同一个域名 + 同一个码**）各带一张自己的单 + 链路。"""
        sibling = self.make_binding(name="b", code=CODE_MINE)
        self.assertEqual(sibling.callback_code, self.binding.callback_code,
                         "自证夹具：两条绑定在同一个码下")
        order_a = self.make_order(order_no=ORDER_NO)
        order_b = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, order_a, remote_id=JID)
        self.make_link(sibling, order_b, remote_id=JID_B)
        return sibling, order_a, order_b

    def test_a_callback_for_a_sibling_row_is_not_reported_as_unknown(self):
        """⭐ **判据本体**：B 那条单的回调进来 ⇒ 跑 **A** 那一轮 ⇒ ⛔ **不报「找不到链路」**。⛔ 弄红它：把 `_link_of` 的查找范围改回「只看自己那一行」（`group = [binding]`）⇒ A 那一轮会把 B 的单报成 `member.openapi_unknown_order`（台账 + `findings`⇒ 绑定 `last_error`）⇒ 本用例前两条断言红。**那正是业务方最恨的假警报形态。**
        ⚠️ 同时钉住「A 那一轮**一个字都不改** B 的单 / 不替 B 打上游」：A 的凭据只办 A 的事（⛔ 不拿兄弟行的凭据去查：那会让别人的凭据坏掉时把**本行**这一轮卡在 `_RETRY`）。"""
        sibling, order_a, order_b = self._two_members_sharing_one_code()
        self.assertEqual(self.post_callback(params=self.status_params(order_no=JID_B)), "200 OK")

        outcome = self.consume(self.binding)          # 跑 **A** 那一轮

        self.assertEqual(self.notes(kind="member.openapi_unknown_order").count(), 0,
                         "把同码兄弟行的单报成「本绑定名下没有这条链路」= 假警报")
        self.assertEqual(
            [text for text in outcome.findings if "没有这条链路" in text], [],
            "假警报进了 findings ⇒ 会盖进绑定 last_error（错误列说假话）",
        )
        self.assertEqual(self.order_status(order_b), "IN_PROGRESS",
                         "A 那一轮不许替 B 改单（谁的单归谁办）")
        self.assertEqual(self.order_status(order_a), "IN_PROGRESS")
        self.assertEqual(self.upstream.api_calls(), [], "A 为 B 的单打了上游")
        #: 这一条事件确实在 A 的 `pending` 里过了一遍（⛔ 不是「文件里压根没有它」而空洞通过）
        self.assertEqual(outcome.mine, 1)

    def test_the_sibling_row_itself_lands_the_event(self):
        """⭐ 那一条回调**真的**落到 B 上：跑 **B** 那一轮 ⇒ B 的单改了、A 的单不动。
        ⚠️ 与上一条**成对**：上一条证「A 不误报、不替 B 办」，本条证「B 自己办得掉」
        —— 只有两条一起，才说明这条回调**没有丢**、也**没有办错行**。"""
        sibling, order_a, order_b = self._two_members_sharing_one_code()
        self.assertEqual(self.post_callback(params=self.status_params(order_no=JID_B)), "200 OK")

        self.consume(self.binding)                    # A 先跑一轮（⛔ 不该动 B 的单）
        self.assertEqual(self.order_status(order_b), "IN_PROGRESS")
        self.reply_status()
        outcome = self.consume(sibling)               # 再跑 B 那一轮

        self.assertEqual(self.order_status(order_b), "PENDING_REVIEW",
                         "B 自己那一轮没把这条回调办掉 ⇒ 回调丢了")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order_a), "IN_PROGRESS", "B 那一轮改了 A 的单")
        audit = OperationLog.objects.filter(order_no=ORDER_NO_B, action="order.transition")
        self.assertEqual(audit.count(), 1)
        #: ⚠️ 打在**B** 那一行的水位上（水位是每行一份：A 的那一份不该被 B 的事件推走）
        self.assertEqual(self.refresh(sibling).callback_watermark_at, FIXED_1)

    def test_a_disabled_siblings_order_is_now_landed_by_the_consumer(self):
        """⭐⭐ **B 档改准（业务方 2026-10-25）**：兄弟行**停用**时，它的单由**消费方照常办**。
        ## 这条用例变的是什么（旧名字：`…is_named_not_silently_dropped`）旧口径（2026-10-09~2026-10-24）：停用那一行的单**没有任何人**会办它（它自己那一轮不跑、①式那条路也不读回调）⇒ 只留一条 `skipped` 的订单维度台账。业务方拍板改成「**官方回调是主路**」：停用的那一行同样**照常应用**，只是**应用身份 = 那条链路自己的 owner**（⛔ 不是消费方）。
        ## 判据（打在哪）① 单**真的落了地**（我方 `PENDING_REVIEW`；⚠️ 这条回调的权威状态是「已完成」）；② ⛔ **没有**那条「没有人办它」的 `skipped` 台账（那是旧口径的痕迹）；③ ⛔ 不进错误列（`findings` 空）；④ ⚠️ 台账 / 完成写入记在**链路自己的 owner** 名下（不是消费方）。"""
        sibling, _order_a, order_b = self._two_members_sharing_one_code()
        sibling.enabled = False
        sibling.save(update_fields=["enabled"])
        self.reply_status()          # 权威状态 = 「已完成」⇒ 写「待验收」
        self.assertEqual(self.post_callback(params=self.status_params(order_no=JID_B)), "200 OK")

        outcome = self.consume(self.binding)

        self.assertEqual(self.order_status(order_b), "PENDING_REVIEW", "停用的兄弟行那一单没人办")
        self.assertEqual(
            self.notes(kind="member.status_in").filter(
                target_ref=ORDER_NO_B, status="skipped",
            ).count(),
            0,
            "旧口径那条「本轮没有人消费这条回调」的痕还在 ⇒ 说明没有照常应用",
        )
        self.assertEqual(self.notes(kind="member.openapi_unknown_order").count(), 0)
        self.assertEqual(outcome.findings, ())
        done = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO_B, status="done")
        self.assertEqual(done.count(), 1, [n.reason for n in self.notes()])
        #: ⭐ 完成写入的台账记在**链路自己的 owner**（那条停用的②式行）名下
        self.assertEqual(done.get().payload["member_binding_id"], sibling.pk)


# ══ ⑤-2′ ⭐⭐ 跨绑定落地（业务方B 档）：回调主路 + **应用身份 = 链路自己的 owner** ══
# 现场（E-0290 第 ③ 条）：①式（账号 + 密码）录的单，回调从②式那条进来 ⇒ 旧实现「只认本绑定」/「兄弟行不消费 ⇒ 只留一条痕」⇒ 状态只能靠**轮询**，慢半拍。业务方拍板：**官方回调是主路**——同一家店的兄弟绑定（①式 / 已停用）的链路**照常应用**，但「把单接到谁名下 / 台账记给谁」一律用**那条链路自己的 owner**（⛔ 不是消费方；钱与节点门都在 owner 名下）。
# ⚠️ **跨店（地址不同）⇒ 一个字节都不写**：拿这家店的凭据去改另一家店的单 = 动别人的数据。⚠️ 第一档（兄弟行是**启用中**的②式）照旧**跳过**：它自己那一轮会办（⛔ 不双执行，用例见上面 `SharedCodeGroupTests` 那两条）。


class CrossBindingLandingTests(_MemberOpenApiCase):
    """①式那条录的单，回调从②式这条进来 ⇒ 照常应用、应用身份 = owner。"""

    def make_password_binding(self, *, name="password-owner", code=CODE_MINE,
                              enabled=True, base_url=MEMBER_BASE_URL):
        """一行**①式**绑定（账号 + 密码），带一个与本类②式消费方**共用**的回调码。
        ⚠️ ①式那一行**本该没有**回调码（`_ensure_callback_code` 对①式一个字节都不写）——这里显式塞同一串，是复刻**真机那个形态**：那一行原本是②式（码由我们发出去、早已填进鸡蛋铺后台），后来**切回①式**，码照旧留着（`_ensure_callback_code` 从不清理）。"""
        username = f"kefu-member-openapi-{name}"
        make_logged_in_client(
            username=username, groups=("客服",),
            nodes=("order.change_status", "grab_hall.take_order"),
        )
        return JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=base_url,
            account=f"acc-{name}", password="pw-synthetic", mode="password",
            callback_code=code, enabled=enabled,
        )

    def make_grab_order(self, *, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB):
        """一张（默认）停在「待抢单」的单 + 一条 MAIN 绩效行（接手条件更新的靶子）。"""
        order = self.make_order(
            order_no=order_no, status=status, hall_price=Decimal("12.34"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34"),
        )
        return order

    def test_a_password_rows_order_is_claimed_under_the_owners_identity(self):
        """⭐⭐ **主判据（B 档）**：①式那条的单 + 回调带「代练中」⇒ 单**照常**接到**owner** 名下（⛔ 不是消费方那个账号）。判别力（三条都能单独红）：① 退回旧口径（兄弟行不应用）⇒ 订单停在「待抢单」、正文第一条断言红；
        ② `_apply_member_status(binding=消费方)`（把应用身份写错）⇒ MAIN 行 = 消费方的 user⇒ 第二条断言红（本仓口径：钱要算在**链路自己的主人**名下）；
        ③ 把 `nodes_of` 也换成消费方的 ⇒ 第三条（审计的操作对象）红。"""
        owner = self.make_password_binding()
        self.assertNotEqual(owner.user_id, self.binding.user_id, "自证夹具：两个不同的人")
        order = self.make_grab_order()
        self.make_link(owner, order)
        self.reply_status(NOT_COMPLETED)          # 权威「代练中」⇒ 中间档自动接手
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.binding)      # ⚠️ 跑的是**②式消费方**那一轮

        # ① 单**真的**落了地（走状态机那条边 ⇒ 落在 IN_PROGRESS，不是「待验收」）
        self.assertEqual(self.order_status(order), "IN_PROGRESS", "兄弟行的单没有照常应用")
        self.assertEqual(outcome.applied, 1)
        # ② ⭐ 钱那一格：MAIN 绩效行 = **owner** 的代练员（⛔ 不是消费方）
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, owner.user_id, "接手接到了消费方名下（应用身份写错了）")
        self.assertNotEqual(perf.user_id, self.binding.user_id)
        # ③ 审计的操作对象也是 owner 的账号
        audit = OperationLog.objects.filter(
            order_no=ORDER_NO, action="order.claim",
        )
        self.assertEqual(audit.count(), 1)
        self.assertEqual(audit.get().target_id, str(owner.user_id))
        # ④ 台账记在 **owner** 名下（⭐ 不是消费方 —— 订单维度的留痕要能追到那条链路）
        taken = self.notes(kind="member.remote_taken").filter(target_ref=ORDER_NO)
        self.assertEqual(taken.count(), 1)
        self.assertEqual(taken.get().payload["member_binding_id"], owner.pk)
        # ⑤ 水位仍按既有引擎记**消费方**（引擎一个字没动）；owner 那一行的水位不前移
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_1)
        self.assertIsNone(self.refresh(owner).callback_watermark_at)
        # ⑥ ⛔ 不进错误列
        self.assertEqual(outcome.findings, ())

    def test_another_store_is_still_skipped_at_the_sibling_ledger(self):
        """⛔ **跨店不代写**：owner 在**另一家店**（同码，历史遗留形态）⇒ 仍跳过 + 留一条台账。
        判别力：把 `_sibling_lands_here` 里那句 `_same_member_store` 去掉 ⇒ 本用例会拿**这家店**的②式凭据去改**另一家店**的链路 ⇒ 第一条断言红（订单被写成待验收）。⚠️ 台账里那句人话要多一句「不是同一家店」（只留痕、⛔ 不进错误列）。"""
        owner = self.make_password_binding(
            name="password-other-store", base_url="https://jidanpu-other-store.test.invalid",
        )
        order = self.make_grab_order()
        self.make_link(owner, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.binding)

        self.assertEqual(self.order_status(order), "WAITING_GRAB", "跨店也照常应用了")
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
        )
        notes = self.notes(kind="member.status_in").filter(
            target_ref=ORDER_NO, status="skipped",
        )
        self.assertEqual(notes.count(), 1, [n.reason for n in self.notes()])
        self.assertIn(f"#{owner.pk}", notes.get().reason)
        self.assertIn("不是同一家店", notes.get().reason)
        self.assertEqual(outcome.findings, ())
        #: 这一轮也⛔ 不该为别人的店去打上游（没有「替它查权威」这一步）
        self.assertEqual(self.upstream.api_calls(), [])

    def test_an_enabled_openapi_sibling_is_still_not_double_executed(self):
        """对照（第一档没变）：兄弟行是**启用中的②式** ⇒ ⛔ 本行一个字都不写、也不留痕。
        判别力：把 `_sibling_lands_here` 的第一档删掉 ⇒ 同一条回调会被**两个执行者**各办一次
        （本用例的前两条断言红：单被动了 + 多一条台账）。"""
        sibling = self.make_binding(name="enabled-sibling", code=CODE_MINE)
        order = self.make_grab_order()
        self.make_link(sibling, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.binding)      # 消费方跑一轮（sibling 自己会办）

        self.assertEqual(self.order_status(order), "WAITING_GRAB", "第一档（不双执行）被破坏了")
        self.assertEqual(self.upstream.api_calls(), [], "替启用中的兄弟行打了上游")
        self.assertEqual(self.notes(kind="member.status_in").count(), 0)
        self.assertEqual(outcome.findings, ())


class CrossBindingScreenshotTests(_MemberOpenApiCase):
    """`action=30` 的兄弟档同口径：①式那条的单来新截图 ⇒ 图存进**那张单**（不是本行的单）。"""

    def test_an_action_30_for_a_password_rows_order_stores_the_picture_on_that_order(self):
        """⭐⭐ 判别力：退回旧口径（``owner.pk != binding.pk`` 一律 `skipped`）⇒ 图零行。
        而且 `uploaded_by` 必须是**链路自己的 owner**（⛔ 不是消费方那个人）：
        它是「这张图算谁传的」的判据（①式那条路同一个出处 `sales_user or binding.user`）。"""
        username = "kefu-member-openapi-shot-owner"
        make_logged_in_client(
            username=username, groups=("客服",), nodes=("order.change_status",),
        )
        owner = JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=MEMBER_BASE_URL,
            account="acc-shot-owner", password="pw-synthetic", mode="password",
            callback_code=CODE_MINE, enabled=True,
        )
        self.assertNotEqual(owner.user_id, self.binding.user_id, "自证夹具：两个不同的人")
        order = self.make_order()
        self.make_link(owner, order, remote_id=JID)
        self.assertEqual(self.post_callback(params={
            "action": "30", "orderNo": JID, "sourceOrderNo": ORDER_NO,
            "note": "订单截图", "imgUrl": SHOT_URL_DIRECT, "name": "对端上传者甲",
            "timestamp": "1791500000",
        }), "200 OK")

        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic", side_effect=lambda _src: JPEG_BODY,
        ):
            outcome = self.consume(self.binding)

        rows = list(OrderScreenshot.objects.filter(order=order))
        self.assertEqual(len(rows), 1, [n.reason for n in self.notes()])
        self.assertEqual(rows[0].status, OrderScreenshot.STATUS_READY)
        self.assertEqual(rows[0].uploaded_by_id, owner.user_id, "图记到了消费方名下")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.notes(kind="member.openapi_other_action").count(), 0)


# ══ ⑤-3 ⭐ 「不是本线路径」的两档**必须分开**（业务方报的假错误）══════════
# 业务方报的形态：错误列里**一直挂着**一条「事件文件里出现了不是本线路径的行（形状：legacy-notify（旧路径 `/jidanpu/v1/notify`）」—— 而那**不是错**：`/jidanpu/v1/notify` 是**①式登录线（旧回调线）自己的正常流量**，每 3 秒都会落行，跟这条下家绑定**毫无关系** ⇒ 盖进 `last_error` 就是**让错误列说假话**（本仓最恨的形态）。
# ⚠️ 两档**只差一个形状名**，⛔ 不许并成一档：另一档（新前缀下码的形状不合法 = `bad-code-shape`）可能意味着**接收器的路径规则变了** ⇒ 那一档**必须**留在错误列里。⛔ 台账两档都照留（这是**只摘错误列、不摘留痕**）。


class ForeignPathVisibilityTests(_MemberOpenApiCase):
    """⭐ 旧路径那一档**不进 `last_error`**；形状不对那一档**照进**（两条缺一不可）。"""

    def _post_a_real_callback_and_append_a_foreign_row(self, *, path: str) -> None:
        """一条**正常**回调 + 一条**异路径**的行（手写：真接收器不会落盘旧路径那种行）。
        ⚠️ 那条异路径的行**逐字照**真接收器写的那一条改 `path`（与上家线那份用例同一手法），
        ⛔ 不作弊造一个形状对不上的 dict（那样测的就不是接收器与消费者的口径了）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")
        raw = dict(_read_lines(self.events)[0])
        raw["path"] = path
        with self.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n")

    def _run_a_round_through_the_command(self):
        """跑**一轮调度**（`jidanpu_sync --once`）⇒ 回刷新后的绑定那一行（见基类那个 helper）。"""
        return self.run_a_schedule_round()

    def test_a_legacy_path_row_never_reaches_the_error_column(self):
        """⭐ 旧路径（另一条线的正常流量）⇒ **台账里有、错误列里没有**，且台账只有一条。
        ⛔ 弄红它：把 `_note_foreign_path` 里的 `if wrote and not known_foreign` 改回 `if wrote`
        ⇒ 这一句会盖进 `last_error` ⇒ 第一条断言红（这正是业务方报的那条「说假话的错」）。"""
        self._post_a_real_callback_and_append_a_foreign_row(path=callback_wsgi.NOTIFY_PATH)

        fresh = self._run_a_round_through_the_command()

        # ⭐ ① **错误列里没有它**（这一条是本用例的判据本体）
        self.assertEqual(fresh.last_error, "",
                         "旧路径的行是**另一条线**的正常流量，盖进错误列 = 让页面上一直挂着一条不是错的错")
        self.assertIsNone(fresh.last_error_at)
        # ⭐ ② 台账里**有**它（⛔ 不是「把这一档整个删掉」），而且**只有一条**（形状去重照旧）
        legacy = self.notes(kind="member.openapi_foreign_path").filter(
            payload__shape=jidanpu_openapi_consume.FOREIGN_SHAPE_LEGACY,
        )
        self.assertEqual(legacy.count(), 1, "台账里没有这一档 ⇒ 接收器路径一变就没人看得见了")
        self.assertEqual(legacy.get().status, "skipped")
        self.assertIn("不是本线路径", legacy.get().reason)

    def test_a_bad_code_shape_still_reaches_the_error_column(self):
        """⭐ 反向对照：**新前缀下码的形状不合法** ⇒ 照旧进错误列（那一档可能意味着规则变了）。⛔ 弄红它：把两档并成一档（例如一律 `if wrote:` 判成「正常流量」而不看形状）⇒ 本条红。
        ⚠️ 这一档是**要人看**的：接收器的路径规则一变（定长 / 前缀 / 中间多一段路由），本绑定的回调会**整批**从这里流过去 —— 那时错误列里必须有话。"""
        bad_code_path = f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_MINE}X"   # 码多一个字符
        self._post_a_real_callback_and_append_a_foreign_row(path=bad_code_path)

        fresh = self._run_a_round_through_the_command()

        self.assertIn("不是本线路径", fresh.last_error)
        self.assertIn("bad-code-shape", fresh.last_error)
        self.assertEqual(fresh.last_error_at, ROUND_NOW)
        rows = self.notes(kind="member.openapi_foreign_path").filter(status="failed")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().payload["sample_path"], bad_code_path)

    def test_the_two_shapes_are_recorded_separately(self):
        """⭐ 两档**各自一条**台账（形状是幂等键的一半）：一条旧路径 + 一条形状不对。
        ⛔ 弄红它：把形状名从 `target_ref` 里拿掉（一律拼 `binding.pk`）⇒ 两条并成一条 ⇒ 红。"""
        self._post_a_real_callback_and_append_a_foreign_row(
            path=f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_MINE}X",
        )
        raw = dict(_read_lines(self.events)[0])
        raw["path"] = callback_wsgi.NOTIFY_PATH
        with self.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n")

        fresh = self._run_a_round_through_the_command()

        rows = self.notes(kind="member.openapi_foreign_path")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            sorted(row.status for row in rows), ["failed", "skipped"],
            "两档的 status 必须分得开（要人看的/正常的）",
        )
        #: 错误列里只有「要人看」那一档的名字，没有旧路径那个形状名
        self.assertIn("bad-code-shape", fresh.last_error)
        self.assertNotIn(jidanpu_openapi_consume.FOREIGN_SHAPE_LEGACY, fresh.last_error)


# ══ ⑥ 兜底对账（§8：低频、逐单、有上限、窗口轮转、与回调同一段判→写）════════════


class ReconcileTests(_MemberOpenApiCase):
    def _three_in_flight(self):
        orders = []
        for order_no, remote_id in ((ORDER_NO, JID), (ORDER_NO_B, JID_B), (ORDER_NO_C, JID_C)):
            order = self.make_order(order_no=order_no)
            self.make_link(self.binding, order, remote_id=remote_id)
            orders.append(order)
        return orders

    def test_it_writes_the_order_and_rotates_the_window(self):
        """⭐ 逐单查在途单 ⇒ 写「待验收」；撞上限只推迟不丢 ⇒ **下一个间隔接着查没查过的**。
        ⚠️窗口起点是**钟摆**（G1）⇒ 不再假设「第一轮查前两张」：断言改成
        「一轮只查 2 张（有界）+ 第二轮把上一轮漏掉的那张补上（不丢）」。"""
        orders = self._three_in_flight()
        self.reply_status()

        first = self.reconcile(max_orders=2)

        self.assertEqual(first.candidates, 3)
        self.assertEqual(first.queried, 2)
        self.assertEqual(first.deferred, 1)
        written = [o for o in orders if self.order_status(o) == "PENDING_REVIEW"]
        self.assertEqual(len(written), 2, "一轮只该查 2 张（有界）")
        cap_rows = self.notes(kind="member.openapi_reconcile_cap")
        self.assertEqual(cap_rows.count(), 1)
        self.assertEqual(cap_rows.get().payload["member_binding_id"], self.binding.pk)

        # 过一个间隔（本表 `reconcile_seconds` 默认 180）：钟摆走一格 ⇒ 上一轮漏的那张必须被查。
        second = self.reconcile(max_orders=2, now=ROUND_NOW + timedelta(seconds=180))

        self.assertEqual(second.applied, 1)
        for order in orders:
            self.assertEqual(
                self.order_status(order), "PENDING_REVIEW", "窗口没有轮转 ⇒ 悄悄丢",
            )

    def test_its_candidates_follow_the_member_lines_processing_set(self):
        """⭐ 候选 = 下家线自己那条「处理集合」判据（终态 / 已软删**不进**候选）。
        ⛔ 弄红它：把候选换成「本绑定名下所有链路」⇒ 已退单那一张会被查、被写 ⇒ 红。"""
        order = self.make_order(order_no=ORDER_NO, status=OrderStatus.REFUNDED)
        self.make_link(self.binding, order, remote_id=JID)
        alive = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, alive, remote_id=JID_B)
        self.reply_status()

        outcome = self.reconcile()

        self.assertEqual(outcome.candidates, 1)
        self.assertEqual(self.order_status(order), TERMINAL_STATUS)
        self.assertEqual(self.order_status(alive), "PENDING_REVIEW")

    def test_it_uses_the_same_judgment_as_the_callback(self):
        """⭐ 对账与回调**共用同一段**「查 → 判 → 写」（`_apply_member_status`）。
        ⛔ 弄红它：给对账另写一段判据（例如直接 `plan_openapi_status` 那套上家判据）
        ⇒ 这条桩记录的 `trigger` 就不会是 `reconcile`。"""
        self._three_in_flight()
        self.reply_status()
        seen: list[str] = []
        real = jidanpu_member_openapi._apply_member_status

        def _spy(**kwargs):
            seen.append(kwargs["trigger"])
            return real(**kwargs)

        with mock.patch.object(jidanpu_member_openapi, "_apply_member_status", side_effect=_spy):
            self.reconcile()

        self.assertEqual(seen, [jidanpu_member.TRIGGER_RECONCILE] * 3)

    def test_another_lines_round_cannot_push_this_lines_window(self):
        """⭐ 同一时刻重放，本行的窗口**必须一模一样**（G1：起点只由时钟算，⛔ 没有共享游标）。
        ⚠️ 现实形态：一个租户里同时有上家②式与下家②式绑定（两张表的 pk 都可以是 `1`）。老实现把窗口位置放进一个**进程级**字典 ⇒ 上家那一轮会把下家那一格推着走（静默错位）。现在起点是 `_window(now, interval, width)` 的纯函数 ⇒ 中间插一轮别人的对账，本行在同一时刻仍然查**同一张**单。⛔ 弄红它：把起点换回进程内游标 ⇒ 第二轮从下一格开始 ⇒ 两次查到的单号不同。"""
        from apps.business.models import JidanpuUpstreamBinding, JidanpuUpstreamOrderLink

        orders = []
        for order_no, remote_id in ((ORDER_NO, JID), (ORDER_NO_B, JID_B), (ORDER_NO_C, JID_C)):
            order = self.make_order(order_no=order_no)
            self.make_link(self.binding, order, remote_id=remote_id)
            orders.append(order)

        make_logged_in_client(username="kefu-cursor-upstream", groups=("客服",))
        upstream = JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username="kefu-cursor-upstream"), jidanpu_base_url=BASE_URL,
            account="acc-cursor-upstream", password="pw-synthetic", mode="openapi",
            client_id=f"{CLIENT_ID}-cursor", client_secret=SENTINEL_SECRET,
        )
        for index, order in enumerate(orders):
            JidanpuUpstreamOrderLink.objects.create(
                binding=upstream, order=order, upstream_order_id=f"UP-{index}",
            )
        self.reply_status()

        self.reconcile(max_orders=1)
        member_ids = list(self.queried_order_ids())
        self.assertEqual(len(member_ids), 1)

        # 别人的一轮（上家线，同一时刻）：⛔ 不许影响本行下面的窗口。
        upstream_outcome = jidanpu_openapi_reconcile.reconcile_openapi_statuses(
            upstream, now=ROUND_NOW, max_orders=1,
        )
        self.assertEqual(upstream_outcome.candidates, 3)

        before = len(self.queried_order_ids())
        self.reconcile(max_orders=1, now=ROUND_NOW)

        self.assertEqual(
            self.queried_order_ids()[before:], member_ids,
            "同一时刻重放却查了另一张 ⇒ 窗口起点还带着进程内状态",
        )

    def test_a_not_completed_remote_status_writes_nothing(self):
        """兜底对账上同样成立：远端不是「已完成」⇒ 一个字都不写（同一条判据）。"""
        orders = self._three_in_flight()
        self.reply_status(NOT_COMPLETED)

        outcome = self.reconcile()

        self.assertEqual(outcome.applied, 0)
        for order in orders:
            self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertFalse(OperationLog.objects.filter(order_no=ORDER_NO).exists())


# ══ ⑥-补 ⭐ 停滞修复（线上事故）══════════════════════════════════════
# 现场：在途 4767 条里 4749 条**从未回拉**（`last_in_at IS NULL`），其中 526 张停在「待抢单」；根因之一是「窗口按 `pk` 升序轮转 ⇒ 刚建的单排在最后，要等一整圈」+「窗口起点住在进程内存」。
# 判据见计划 `docs/plans/2026-10-22-reconcile-stall-fix.md` 的 G1 / G3 / G4。


class StallFixTests(_MemberOpenApiCase):
    """G3：一轮 50 = 最新 15（`last_in_at IS NULL` 且「待抢单」）+ 轮转 35。"""

    #: 让钟摆落在第 0 格的时刻（`int(ts)//180*35 % 60 == 0`）：轮转那 35 张 = 最老的 35 张。
    NOW_LATEST = ROUND_NOW + timedelta(seconds=720)

    def test_the_round_always_includes_the_newest_waiting_grab(self):
        """⭐ 60 张候选 ⇒ 查 50 张，**最新那 15 张「待抢单」一定在里面**（老实现查不到它们）。
        ⛔ 弄红它：去掉「最新优先」那一段 ⇒ 钟摆窗口只覆盖第 0–34 张 ⇒ 最新那 15 张
        （第 45–59 张）一张都查不到 ⇒ 本用例红。"""
        self.reply_status("未分配")
        newest: list[str] = []
        for index in range(60):
            waiting = index >= 45
            order = self.make_order(
                order_no=f"2026100800000000{index:04d}",
                status=OrderStatus.WAITING_GRAB if waiting else OrderStatus.IN_PROGRESS,
            )
            remote_id = f"2026100800000001{index:04d}"
            self.make_link(self.binding, order, remote_id=remote_id)
            if waiting:
                newest.append(remote_id)

        outcome = self.reconcile(now=self.NOW_LATEST)

        self.assertEqual(outcome.candidates, 60)
        self.assertEqual(outcome.queried, 50, "单一预算 = 50，多一发都不许")
        for remote_id in newest:
            self.assertIn(remote_id, self.queried_order_ids(), "最新那批「待抢单」没排进窗口")
        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.stuck, 0)


class ReconcileMemberStatusesCommandTests(_MemberOpenApiCase):
    """G4：`reconcile_member_statuses` 管理命令（默认 dry-run、逐单 try、每批看凭据退避）。"""

    def _schema(self) -> str:
        from django.db import connection

        return connection.schema_name

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command("reconcile_member_statuses", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _waiting_order(self, *, order_no=ORDER_NO, remote_id=JID,
                       status=OrderStatus.WAITING_GRAB):
        """一张（默认）「待抢单」的单 + MAIN 绩效行（机器接手的条件更新靶子）+ 一条链路。"""
        order = self.make_order(order_no=order_no, status=status, hall_price=Decimal("12.34"))
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34"),
            user=None, claimed_at=None,
        )
        self.make_link(self.binding, order, remote_id=remote_id)
        return order

    def test_schema_is_required(self):
        """⭐ `--schema` 必填：绑定 pk 每个租户库都从 1 开始，不带 schema 会跨租户误伤。"""
        with self.assertRaises(CommandError):
            call_command("reconcile_member_statuses", stdout=StringIO(), stderr=StringIO())

    def test_default_dry_run_prints_the_takeover_target_and_writes_nothing(self):
        """⭐ 默认 dry-run：打印「单号 / 上游状态 / 会不会接手、接手给谁」，**一个字都不落库**。"""
        order = self._waiting_order()
        self.reply_status(NOT_COMPLETED)  # 「代练中」⇒ 映射 IN_PROGRESS ⇒ 会接手给绑定人

        out, _err = self._run(
            "--schema", self._schema(), "--binding", str(self.binding.pk), "--sleep", "0",
        )

        self.assertIn(ORDER_NO, out, "dry-run 必须逐单打印单号")
        self.assertIn(NOT_COMPLETED, out, "dry-run 必须打印上游状态")
        self.assertIn("机器接手=会", out)
        self.assertIn(self.binding.user.username, out, "必须说清接手给谁")
        # 一个字都不许落库
        self.assertEqual(self.order_status(order), "WAITING_GRAB")
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
        )
        self.assertFalse(OperationLog.objects.filter(order_no=ORDER_NO).exists())

    def test_execute_actually_claims(self):
        """`--execute` 走**与回调同一条**写入面 ⇒ 真的接手（我方进行中 + MAIN = 绑定人）。"""
        order = self._waiting_order()
        self.reply_status(NOT_COMPLETED)

        out, _err = self._run(
            "--schema", self._schema(), "--binding", str(self.binding.pk),
            "--sleep", "0", "--execute",
        )

        self.assertIn(ORDER_NO, out)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.binding.user_id,
        )

    def test_only_waiting_filters_the_candidates(self):
        """`--only-waiting` 只补「待抢单」那一档（2026-10-22 事故里那 526 张的形态）。"""
        waiting = self._waiting_order()
        other = self._waiting_order(
            order_no=ORDER_NO_B, remote_id=JID_B, status=OrderStatus.IN_PROGRESS,
        )
        self.reply_status(NOT_COMPLETED)

        _out, _err = self._run(
            "--schema", self._schema(), "--binding", str(self.binding.pk),
            "--sleep", "0", "--only-waiting", "--execute",
        )

        self.assertEqual(self.order_status(waiting), "IN_PROGRESS")
        self.assertEqual(self.order_status(other), "IN_PROGRESS")
        queried = [row["params"].get("orderId") for row in self.upstream.api_calls()]
        self.assertEqual(queried, [JID], "非「待抢单」的单不该被查")

    def test_a_credential_backoff_stops_before_any_request(self):
        """⭐ 每批之前看 `credential_backoff_reason`：刚被拒就停，⛔ 一次上游都不打。"""
        self._waiting_order()
        self.reply_status(NOT_COMPLETED)

        with mock.patch.object(
            jidanpu_openapi, "credential_backoff_reason", return_value="刚被拒过（合成原因）",
        ):
            out, _err = self._run(
                "--schema", self._schema(), "--binding", str(self.binding.pk), "--sleep", "0",
            )

        self.assertIn("停止", out)
        self.assertEqual(self.upstream.api_calls(), [])


# ══ ⑥′ ⭐⭐ 中间档自动接手（D-0226，spec 2026-10-11 §2.2 / §3）══════════════════
# 现场（kexing 真机）：②式绑定的单抛到下家后对方打手接了单、提交、乃至对方客户验收，我方**中间档永远不写**（接手人 / 绩效不建）。⚠️ 旧句「已完成从待抢单会被状态机拒 ⇒ 永不自愈」与实现不符——「待抢单 → 待验收」的边存在（D-0147 按 target 铺边）、节点门放行时旧代码会写成**没有接手人与绩效的假完成**；节点门不放行才 `IllegalTransition`。⇒ 现在：远端「有人在做」（待上号 / 代练中 / 待验收）或「已完成」、而我方还在「待抢单」⇒ **机器接手**给绑定的代练员（`jidanpu_member.machine_claim_order`，与①式日志快路**共用一份**钱路径；MAIN 绩效行 = `binding.user`，钱在我方算）。⚠️ 夹具：接手那条边（`WAITING_GRAB → IN_PROGRESS`）的节点门是 **any-of**
# （`order.change_status` / `grab_hall.take_order`… 之一即可过）⇒ 本类用自己的绑定工厂给绑定人挂 `grab_hall.take_order`（照①式快路夹具的口径）；订单带一条**无人**的 MAIN绩效行（`claim_order` 条件更新的靶子）。缺节点那一档（`nodes_of` 恒空）用`_powerless_binding`——⚠️ 基类那行（客服 + `order.change_status`）**过得了**接手边，当不了缺节点的靶子。


class MidStatusClaimTests(_MemberOpenApiCase):
    """⭐ 远端中间档 × 我方「待抢单」⇒ 自动接手；三档拒绝 ⇒ 台账 + findings + `NOTED`。"""

    #: 远端「待验收」档（⭐ 中间档里**只接手、不写待验收**的那一格）。
    PRE_REVIEW_STATUS = "待验收"
    #: 远端「未分配」档（对方还没人接 ⇒ 不动作）。
    UNASSIGNED_STATUS = "未分配"

    def setUp(self):
        super().setUp()
        self.claim_binding = self.make_claim_binding()

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_claim_binding(self, *, name="claimer"):
        """一行②式绑定，绑定人带 `grab_hall.take_order` 节点（照①式快路夹具的口径）。⚠️ 与基类那行共用一个回调码（新口径下的常态：一个域名共用一个码）⇒ 本类一律**跑这一行自己的那一轮**（`consume/reconcile(self.claim_binding)`）。
        ⚠️ 边的节点门是 **any-of**（`order.change_status` 也能过接手那条边 ⇒ 基类那行故意不够用）⇒ 缺节点那一档（`nodes_of` 为空）用 `_powerless_binding`。"""
        return self._make_binding_with_nodes(
            name=name, nodes=("order.change_status", "grab_hall.take_order"),
        )

    def _powerless_binding(self, *, name="powerless"):
        """一行②式绑定，绑定人**没有任何分组 / 节点**（`nodes_of` 恒空 ⇒ 接手边必被拒）。
        ⚠️ 不能用 `make_logged_in_client(..., nodes=())` 造这档：节点挂在**组**的 Role 上（`p5_auth_helpers._grant_nodes` 给组的 Role `add`）⇒ 本类 `setUp` 里 claim 那一行的授权会顺着**同一个「客服」组**漏给它。照①式快路缺节点用例的口径：裸 `create_user`。"""
        user = User.objects.create_user(
            username=f"member-openapi-{name}", password="pw-synthetic",
        )
        return JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url=MEMBER_BASE_URL,
            account="", password="", mode="openapi",
            client_id=f"{CLIENT_ID}-member-{name}", client_secret=SECRET_MINE,
            callback_code=CODE_MINE, enabled=True,
        )

    def _make_binding_with_nodes(self, *, name, nodes):
        username = f"kefu-member-openapi-{name}"
        make_logged_in_client(username=username, groups=("客服",), nodes=nodes)
        return JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=MEMBER_BASE_URL,
            account="", password="", mode="openapi",
            client_id=f"{CLIENT_ID}-member-{name}", client_secret=SECRET_MINE,
            callback_code=CODE_MINE, enabled=True,
        )

    def make_grab_order(self, *, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB,
                        taker=None):
        """一张（默认）停在「待抢单」的单 + 一条 MAIN 绩效行（条件更新的靶子）。"""
        order = self.make_order(
            order_no=order_no, status=status, hall_price=Decimal("12.34"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34"),
            user=taker, claimed_at=FIXED_1 if taker else None,
        )
        return order

    # ── spec §3-1 / §3-2：两条入口都真的接手 ────────────────────────────────

    def test_a_mid_status_callback_claims_the_waiting_grab_order(self):
        """⭐ **回调路径**：`action=10` 回调 + 权威「代练中」+ 我方「待抢单」⇒ 机器接手：我方变「进行中」、MAIN 绩效行 = 绑定的代练员、审计 = 机器操作人、`member.remote_taken` done、水位前进。
        ⛔ 弄红它：删掉 `_apply_member_status` 里中间档那一支 ⇒ 订单停在「待抢单」、MAIN 行无人 ⇒ 前两条断言红。"""
        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        # ① 我方「进行中」（接手走状态机那条边 ⇒ 落在 IN_PROGRESS，⛔ 不是「待验收」）
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(outcome.applied, 1)
        # ② 钱：MAIN 绩效行 = 绑定的代练员（钱在我方算）+ 接单时刻
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.claim_binding.user_id)
        self.assertIsNotNone(perf.claimed_at)
        # ③ 审计：机器操作人 + 接手出处的那句人话（②式拿不到接单人身份 ⇒ 传不了 uid）
        audit = OperationLog.objects.filter(
            order_no=ORDER_NO, action="order.claim",
        )
        self.assertEqual(audit.count(), 1)
        row = audit.get()
        self.assertEqual(row.actor, jidanpu_member.MACHINE_ACTOR)
        self.assertIsNone(row.actor_id)
        self.assertIn(f"已同步到下家 {self.claim_binding.user.username} 的系统", row.summary)
        self.assertNotIn("官方API权威状态=", row.summary)
        # ④ 台账：`member.remote_taken` done（⭐ ①式共享那一份写的，幂等标记同一形状）
        taken = self.notes(kind="member.remote_taken").filter(target_ref=ORDER_NO)
        self.assertEqual(taken.count(), 1)
        self.assertEqual(taken.get().status, "done")
        self.assertEqual(taken.get().payload["member_binding_id"], self.claim_binding.pk)
        self.assertIsNone(taken.get().payload["remote_uid"])
        self.assertIn("官方API权威状态=代练中", taken.get().payload["source_note"])
        self.assertEqual(self.notes(kind="member.status_in").count(), 0)
        # ⑤ 水位前进（本档**有结论**）且不进错误列
        self.assertEqual(self.refresh(self.claim_binding).callback_watermark_at, FIXED_1)
        self.assertEqual(outcome.findings, ())

    def test_machine_claim_does_not_touch_the_deposit_even_with_the_tenant_gate_on(self):
        """⭐ W2B-01（2026-10-20 审查复核）：机器接手**不碰押金**——总闸开着也照样接。
        `machine_claim_order` 给 `claim_order` 显式传 `enforce_deposit=False`（机器接手没有「接单人钱包」语义，钱在我方算）。删掉那一行 ⇒ 总闸回落生效，`ensure_deposit_for_claim` 会按游戏默认押金（缺规则行 = 50 元）去冻/拒（本夹具的接手人没有钱包）⇒ 本条红。这就是它存在的意义：那行豁免**没有**任何别的网钉着。
        判据：总闸 True 下接手照常成功（进行中 + MAIN = 绑定的代练员），且接手人名下**没有**钱包行（更没有冻结额）。"""
        with self.settings(DEPOSIT_ENFORCED=True):
            order = self.make_grab_order()
            self.make_link(self.claim_binding, order)
            self.reply_status(NOT_COMPLETED)
            self.post_callback()
            outcome = self.consume(self.claim_binding)

        from apps.accounts.models import TakerWallet  # 仅本判据用（文件顶部无此依赖）

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.claim_binding.user_id,
        )
        self.assertFalse(
            TakerWallet.objects.filter(user_id=self.claim_binding.user_id).exists(),
            "机器接手不该给接手人建钱包 / 冻押金",
        )

    def test_the_reconcile_round_claims_the_same_way(self):
        """⭐ **对账路径**：`reconcile_member_statuses` 同一场景 ⇒ 同样接手
        （两条入口**共用** `_apply_member_status`，候选含该链路）。"""
        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        self.reply_status(NOT_COMPLETED)

        outcome = self.reconcile(self.claim_binding)

        self.assertEqual(outcome.applied, 1)
        self.assertFalse(outcome.stopped)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.claim_binding.user_id,
        )
        self.assertTrue(
            self.notes(kind="member.remote_taken")
            .filter(target_ref=ORDER_NO, status="done").exists()
        )

    # ── spec §3-3 / §3-4：两档「接手到哪为止」的分界 ─────────────────────────

    def test_a_pending_review_remote_status_only_claims_to_in_progress(self):
        """⭐ 远端「待验收」⇒ **只接手到「进行中」**，⛔ 不直接写「待验收」
        （那一格保留给远端「已完成」的既有完成写入，spec §2.2）。"""
        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        self.reply_status(self.PRE_REVIEW_STATUS)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        # ⛔ 完成写入那一跳**没跑**：没有迁移审计、连 B 段拉图都不打（只查了那一次权威）
        self.assertFalse(
            OperationLog.objects.filter(order_no=ORDER_NO, action="order.transition").exists()
        )
        self.assertEqual(
            [call["path"] for call in self.upstream.api_calls()], [ORDER_STATUS_PATH],
        )

    def test_a_done_remote_status_claims_then_completes_in_one_observation(self):
        """⭐ **远端「已完成」+ 我方「待抢单」⇒ 先接手、再完成**，两步一气呵成 ⇒ 终态「待验收」。⭐ 完成那步审计的**前值 = 「进行中」**是「接手后重取订单行」的机械判据：拿陈旧的「待抢单」进去，前值就会是「待抢单」（而且整步会被边判拒掉）。
        ⛔ 弄红它（真实机制，评审更正后）：删掉「先接手」那一支 ⇒ 本夹具的节点集放行「待抢单 → 待验收」那条边 ⇒ 订单被写成「待验收」但**没有接手人、没有 MAIN 绩效行**、`auto_claim` 审计计数为 0（断言红在三处）；去掉 `refresh_from_db()` ⇒ 前值断言红。"""
        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        self.reply_status(AUTHORITATIVE)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "PENDING_REVIEW")
        # 接手与完成两步各一行审计；完成那行的前值 = 「进行中」（重取订单行钉住的）
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=ORDER_NO, action="order.claim",
            ).count(), 1,
        )
        transition = OperationLog.objects.filter(
            order_no=ORDER_NO, action="order.transition",
        )
        self.assertEqual(transition.count(), 1)
        self.assertEqual(
            (transition.get().from_value, transition.get().to_value),
            ("代练中", "待验收"),  # ⚠️ `IN_PROGRESS` 的中文 label 就是「代练中」
        )
        # MAIN 绩效行 = 绑定的代练员；接手台账与完成台账都在（既有出口）
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.claim_binding.user_id,
        )
        self.assertTrue(
            self.notes(kind="member.remote_taken")
            .filter(target_ref=ORDER_NO, status="done").exists()
        )
        self.assertTrue(
            self.notes(kind="member.status_in")
            .filter(target_ref=ORDER_NO, status="done").exists()
        )
        # 截图照旧走 `openapi_pics`（B 段那一发打了官方API 的拉图端点）
        self.assertIn(
            ORDER_PROCESS_IMGS_PATH, [call["path"] for call in self.upstream.api_calls()],
        )
        self.assertEqual(self.refresh(self.claim_binding).callback_watermark_at, FIXED_1)

    # ── spec §3-5 / §3-6：常态与不动作的两档（都不许留台账）──────────────────

    def test_an_already_in_progress_order_with_a_mid_status_writes_nothing(self):
        """⭐ **幂等 / 常态**：我方已「进行中」（他已接手）+ 远端「代练中」⇒ **不接手、
        不留台账、不排事件**，水位照常前进（远端在做、我方已接，是常态）。"""
        order = self.make_grab_order(status=OrderStatus.IN_PROGRESS, taker=self.claim_binding.user)
        self.make_link(self.claim_binding, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0, "常态却留了台账 / 排了事件")
        self.assertEqual(self.refresh(self.claim_binding).callback_watermark_at, FIXED_1)

    def test_an_unassigned_remote_status_leaves_the_order_alone(self):
        """⭐ **未分配**（对方还没人接）⇒ 不动作：不接手、不留台账，水位前进（今日行为）。"""
        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        self.reply_status(self.UNASSIGNED_STATUS)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self.order_status(order), "WAITING_GRAB")
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0)
        self.assertEqual(self.refresh(self.claim_binding).callback_watermark_at, FIXED_1)

    # ── spec §3-7 / §3-8：三档可预期拒绝（照既有完成 DENIED 的处置形状）───────

    def test_a_missing_node_denies_the_claim_and_is_noted(self):
        """⭐ **缺节点**：绑定人 `nodes_of` 为空 ⇒ `IllegalTransition` ⇒订单维度 `failed` 台账 + findings（要人看）+ `OUTCOME_NOTED`，且**水位前进**（这一档有结论，⛔ 不许把它后面的回调一起钉住）；那半截绩效行必须滚回去。
        ⚠️ 边的节点门是 any-of（`order.change_status` 也能过接手那条边）⇒ 基类那行（客服 + `order.change_status`）**不够用**，本用例用一行**零节点**的绑定。"""
        binding = self._powerless_binding()
        order = self.make_grab_order()
        self.make_link(binding, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(binding)

        self.assertEqual(outcome.failed, 1)  # `OUTCOME_NOTED` 计入 failed（要人看）
        self.assertTrue(outcome.findings)
        self.assertIn("状态机", outcome.findings[0])
        self.assertEqual(self.order_status(order), "WAITING_GRAB")
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            "那半截绩效行没有滚回去（E-0022 的「一半成功」形状）",
        )
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=ORDER_NO, action="order.claim",
            ).exists()
        )
        noted = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO)
        self.assertEqual(noted.count(), 1)
        self.assertEqual(noted.get().status, "failed")
        self.assertIn("自动接手被状态机拒绝", noted.get().reason)
        # ⭐ 水位**前进**（有结论；与「查不动」那一档相反）且没留下「接手成功」的假标记
        self.assertEqual(self.refresh(binding).callback_watermark_at, FIXED_1)
        self.assertEqual(self.notes(kind="member.remote_taken").count(), 0)

    def test_a_same_person_conflict_is_noted_with_its_own_words(self):
        """⭐ **same-person 冲突**：绑定人已在该单有绩效行（他既是客服又是代练员）⇒
        真撞 `uniq_performance_order_user` ⇒ **自己的**那句人话 + `OUTCOME_NOTED`
        （水位前进；⛔ 不许并进「不在抢单大厅」那句——错根因比没有根因更贵）。"""
        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        # 绑定人在这张单上**已经有一条**绩效行（客服那条「销售提成」）⇒ 条件更新把 MAIN 行
        # 写成他的时候撞唯一约束（真机的同一形态，⛔ 不用桩：真约束真撞）。
        Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=self.claim_binding.user,
            temp_amount=Decimal("1.00"),
        )
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.failed, 1)
        self.assertIn("绩效行", outcome.findings[0])
        self.assertEqual(self.order_status(order), "WAITING_GRAB")
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        noted = self.notes(kind="member.status_in").filter(target_ref=ORDER_NO)
        self.assertEqual(noted.count(), 1)
        self.assertEqual(noted.get().status, "failed")
        self.assertIn("绩效行", noted.get().reason)
        self.assertNotIn("不在我方抢单大厅", noted.get().reason)
        self.assertEqual(self.refresh(self.claim_binding).callback_watermark_at, FIXED_1)

    # ── spec §3-9：裁定 8 平价（按人的闸门挡不住机器接手）────────────────────

    def test_the_claim_ignores_the_per_person_gates(self):
        """⭐ **裁定 8 平价**：绑定人配了「会挡住人类」的接单价区间（不含本单 12.34）
        ⇒ 机器接手照样成功（`enforce_limits=False`；①式
        `test_the_machine_takeover_ignores_the_per_person_gates` 的②式版）。"""
        from apps.accounts.models import StaffProfile

        order = self.make_grab_order()
        self.make_link(self.claim_binding, order)
        profile, _ = StaffProfile.objects.get_or_create(user=self.claim_binding.user)
        profile.grab_price_min = Decimal("100.00")
        profile.grab_price_max = Decimal("200.00")
        profile.save(update_fields=["grab_price_min", "grab_price_max"])

        real = jidanpu_member.claim_order
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")
        with mock.patch.object(jidanpu_member, "claim_order", wraps=real) as spy:
            outcome = self.consume(self.claim_binding)

        spy.assert_called_once()
        self.assertIs(spy.call_args.kwargs["enforce_limits"], False)
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.claim_binding.user_id,
        )

    # ── spec §3-10 → ⭐ **D-0227 改写**（E-0236 根因③：钉相等的对拍有盲区）────────

    def test_the_fastpath_still_echoes_but_the_mirror_claim_does_not(self):
        """⭐⭐ **闸 1 反向对照（D-0227）**：①式快路（in_hall=True）仍排 assign+unlist+status_out，②式镜像接手**一条都不排**——钉**不等于**，⛔ 不是钉相等。
        ⚠️ **本用例是对 D-0226 版本的改写**（提交说明里登记）：旧版钉「②式与①式的 outbox事件形状**相等**」且两条腿都是 `in_hall=False` 夹具——生产里 in_hall=True 多出的那条`member.unlist` 正好落在对拍的盲区里（E-0236 根因③：11 次自动接手、每次同秒unlist-done）。新判据（spec §5 第 3 条）：①式行为**逐字不变**的显式钉子 +
        ②式**恰好为零**，两边各自**逐字**断言 + 显式 `assertNotEqual`。⛔ 弄红它：把②式调用点的 `suppress_outbound_sync=True` 去掉 ⇒ ②式那一列多出assign / unlist / status_out ⇒ 「恰好为空」与「不等于」两条一起红。"""
        order_fastpath = self.make_grab_order(order_no=ORDER_NO)
        JidanpuMemberOrderLink.objects.create(
            binding=self.claim_binding, order=order_fastpath,
            jidanpu_order_id=JID, in_hall=True,      # ⭐ E-0236 的生产形态（②式恒 True）
        )
        order_openapi = self.make_grab_order(order_no=ORDER_NO_B)
        JidanpuMemberOrderLink.objects.create(
            binding=self.claim_binding, order=order_openapi,
            jidanpu_order_id=JID_B, in_hall=True,
        )

        # ①式那一腿：真的日志快路把 A 单接手（并由此排出它那一列 outbox 事件）
        self.assertEqual(
            self._run_fastpath_claim_round(remote_id=JID, order_no=ORDER_NO), 1,
        )
        self.assertEqual(self.order_status(order_fastpath), "IN_PROGRESS")

        # ②式那一腿：一条「代练中」的回调把 B 单接手（镜像 ⇒ 零回声）
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(
            self.post_callback(params=self.status_params(order_no=JID_B)), "200 OK",
        )
        outcome = self.consume(self.claim_binding)
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order_openapi), "IN_PROGRESS")

        def shapes(order_no: str) -> list[tuple[str, str]]:
            return sorted(
                (row.kind, row.status)
                for row in JidanpuSyncEvent.objects.filter(
                    direction="out", target_ref=order_no,
                )
            )

        # ①式：三条都在（in_hall=True ⇒ 比 D-0226 那版多出的 unlist 是**要钉住**的那条）：
        self.assertEqual(
            shapes(ORDER_NO),
            [
                ("member.assign", "pending"),
                ("member.status_out", "pending"),
                ("member.unlist", "pending"),
            ],
        )
        # ②式：**恰好为零**（镜像接手不对对方系统产生任何写动作）：
        self.assertEqual(shapes(ORDER_NO_B), [])
        # ⭐ 显式钉**不等于**（E-0236 教训：钉相等的对拍看不见「多出来的那半」）：
        self.assertNotEqual(
            shapes(ORDER_NO_B), shapes(ORDER_NO),
            "②式镜像接手与①式快路的事件形状竟然相等 ⇒ 闸 1 没接上",
        )

    def _run_fastpath_claim_round(self, *, remote_id: str, order_no: str) -> int:
        """跑一轮**真的**①式日志快路（`poll_log`）：三个登录式上游出口换成脚本替身。
        替身形状照 `tests.test_jidanpu_member_fastpath`（`_row` / `_taken`）：一行命中本线
        链路的系统日志 + 远端单「已离开他家大厅、未退单」+ 一条有数值 `userId` 的接单人日志。"""
        moment = dj_timezone.localtime(dj_timezone.now()).strftime("%Y-%m-%d %H:%M:%S")
        rows = [{
            "createTime": moment, "joinOrderId": remote_id, "soureOrderId": order_no,
            "userName": "别人的操作人",
            "remark": "【打手甲】通过接单大厅(内)自助接手此单(系统网页)，接手价：1.00",
            "logType": 10,
        }]
        with mock.patch.object(jidanpu, "fetch_system_logs", return_value=rows), \
             mock.patch.object(
                 jidanpu, "fetch_order_by_key1",
                 return_value={"orderId": remote_id, "orderSta": "待上号", "isThrowOrder": "N"},
             ), \
             mock.patch.object(
                 jidanpu, "fetch_ass_logs",
                 return_value=[{"userId": 5001, "userName": "打手甲"}],
             ):
            return jidanpu_member.poll_log(
                self.claim_binding, now=dj_timezone.now(), client=object(),
            )


# ══ ⭐⭐ D-0227 / E-0236：镜像接手的三道闸（闸 1 抑制回声 / 闸 3 in_hall 刷新）═══════


class MirrorClaimGatesTests(_MemberOpenApiCase):
    """②式「因为对方接走而触发的自动接手」（**镜像接手**）不回声、`in_hall` 随权威刷新。
    夹具口径与 `MidStatusClaimTests` 同源（绑定人带接手那两个节点；单停在「待抢单」+MAIN 绩效行），差别只在链路那格 **`in_hall=True`**——那是 E-0236 的生产形态（②式没有大厅监视 ⇒ 那格恒 True、永不过期，本地判据失真）。（闸 2 的网在 `tests/test_jidanpu_member_openapi_outbox.py::UnlistPreReadGateTests`。）"""

    def setUp(self):
        super().setUp()
        self.claim_binding = self._make_binding_with_nodes(
            name="gate-claimer", nodes=("order.change_status", "grab_hall.take_order"),
        )

    # ── 夹具工厂（与 MidStatusClaimTests 同口径；⛔ 不继承那个类——会把它 10 条重跑一遍）──

    def _make_binding_with_nodes(self, *, name, nodes):
        username = f"kefu-member-openapi-{name}"
        make_logged_in_client(username=username, groups=("客服",), nodes=nodes)
        return JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username),
            jidanpu_base_url=MEMBER_BASE_URL,
            account="", password="", mode="openapi",
            client_id=f"{CLIENT_ID}-member-{name}", client_secret=SECRET_MINE,
            callback_code=CODE_MINE, enabled=True,
        )

    def make_grab_order(self, *, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB,
                        taker=None):
        """一张（默认）停在「待抢单」的单 + 一条 MAIN 绩效行（条件更新的靶子）。"""
        order = self.make_order(
            order_no=order_no, status=status, hall_price=Decimal("12.34"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("12.34"),
            user=taker, claimed_at=FIXED_1 if taker else None,
        )
        return order

    def _link_in_hall(self, binding, order, *, remote_id=JID):
        """一条**标着还在对方大厅**的链路（E-0236 的生产形态：②式没有大厅监视）。"""
        return JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id=remote_id, in_hall=True,
        )

    def _out_kinds(self, order_no=ORDER_NO) -> list[str]:
        return sorted(
            JidanpuSyncEvent.objects.filter(
                direction="out", target_ref=order_no,
            ).values_list("kind", flat=True)
        )

    # ── 闸 1 主用例：E-0236 复盘形态 ───────────────────────────────────────────

    def test_the_e236_replay_claims_without_any_peer_outbound_events(self):
        """⭐⭐ **闸 1 主用例（E-0236 复盘）**：②式回调 → 权威「代练中」→ 我方待抢单、`in_hall=True` ⇒ 接手成功，且 peer 向**一条事件都不排**（unlist / assign / status_out）、refund 原语**零调用**（spy 钉死）。
        ⛔ 弄红它：把②式调用点的 `suppress_outbound_sync=True` 去掉 ⇒ 本地 `in_hall=True`让 `queue_claimed` 排出 assign + unlist、`apply_transition` 排出 status_out⇒ 事件集合与 spy 两条断言红（那正是 E-0236 现场：11 次自动接手、每次同秒 unlist-done）。"""
        order = self.make_grab_order()
        self._link_in_hall(self.claim_binding, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        # ⭐ refund 原语零调用（spy 钉死）：②式的退单 / 撤下（`refund_remote`）最终都打
        #    `ChangeOrderStatus` 这一发 ⇒ 在客户端出口上 spy（`refund_remote` 本身被
        #    `_MEMBER_OPENAPI_LINE` 在导入期捕走了函数对象，模块属性补丁拦不到）。
        with mock.patch.object(
            jidanpu_openapi.OpenApiClient, "change_order_status", autospec=True,
        ) as refund_spy:
            outcome = self.consume(self.claim_binding)

        # 接手本身成立（我方记账那半不受闸影响：钱在我方算）：
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.claim_binding.user_id,
        )
        taken = self.notes(kind="member.remote_taken").filter(target_ref=ORDER_NO)
        self.assertEqual((taken.count(), taken.get().status), (1, "done"))
        # 闸 1：peer 向一条都不排（kind 用**字面值**，⛔ 不从被测常量派生，E-0024）：
        self.assertEqual(self._out_kinds(), [])
        # 闸 3（双保险）：本地 `in_hall` 已被权威观察收窄成 False：
        link = JidanpuMemberOrderLink.objects.get(
            binding=self.claim_binding, order=order,
        )
        self.assertFalse(link.in_hall)
        # refund 原语零调用 + 整轮只打了那一次权威查询（网络出口的读数）：
        refund_spy.assert_not_called()
        self.assertEqual(
            [call["path"] for call in self.upstream.api_calls()], [ORDER_STATUS_PATH],
        )

    def test_a_second_binding_still_gets_its_withdraw(self):
        """⭐⭐ **闸 1 阳性对照（抑制不许扩大化）**：这单另有一份「已录到**另一家店**的第二个绑定」的推送证据（链路行）⇒ 镜像接手之后第二个绑定**仍**排出 `member.withdraw`。
        ⚠️ 那一半不是回声、是冗余副本清理：**另一套**下家系统那份按地址去重语义是**没人接的挂单**，撤它是对的（业务方已确认这个语义，spec §3 闸 1）。
        ⚠️ 第二份证据**故意在另一家店**：接手人是这家店的主人时，**同一家店**的另一份绑定不再收退单（他在那边正在做这张单，见`tests/test_member_same_store_withdraw.py`）⇒ 用同店构造会把本条变成测那一档。⛔ 弄红它：把抑制错扩大到 withdraw 那一半 ⇒ 本用例红（withdraw 计数变 0）。"""
        second = self.make_binding(name="gate-second")
        # ⚠️ 挪到**另一家店**：接手人是这家店的主人 ⇒ **整店不撤**（同店的第二份绑定不再收退单，见 `tests/test_member_same_store_withdraw.py`）。
        # 本用例钉的是「抑制不许扩大化」——换一家店同样成立，而且那才是真正的「冗余副本清理」（两套下家系统各推了一份）。
        JidanpuMemberBinding.objects.filter(pk=second.pk).update(
            jidanpu_base_url=OTHER_MEMBER_BASE_URL
        )
        order = self.make_grab_order()
        self._link_in_hall(self.claim_binding, order)
        JidanpuMemberOrderLink.objects.create(
            binding=second, order=order, jidanpu_order_id=JID_B,
        )
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        withdraw_rows = JidanpuSyncEvent.objects.filter(
            direction="out", kind="member.withdraw", target_ref=ORDER_NO,
        )
        self.assertEqual(withdraw_rows.count(), 1, "第二个绑定的 withdraw 被抑制误伤")
        self.assertEqual(withdraw_rows.get().payload["member_binding_id"], second.pk)
        # 接手人自己那份的 assign / unlist 照旧一条没有（闸 1 那一半仍然在）：
        self.assertNotIn("member.assign", self._out_kinds())
        self.assertNotIn("member.unlist", self._out_kinds())

    # ── 闸 3：`in_hall` 随权威观察刷新 ─────────────────────────────────────────

    def test_a_mid_status_refreshes_the_stale_marker_when_we_already_have_a_taker(self):
        """⭐⭐ **闸 3（含「我方已是进行中、不接手」的那档）**：我方已有人接（常态、不接手、不留台账）+ 远端「代练中」⇒ `in_hall` **照旧收窄成 False**——进这一档就刷新，无论本次是否接手。
        ⛔ 弄红它：把闸 3 那两行收窄删掉 ⇒ `link.in_hall` 仍是 True（E-0236 根因①：②式没有大厅监视，那格永不过期）。"""
        order = self.make_grab_order(
            status=OrderStatus.IN_PROGRESS, taker=self.claim_binding.user,
        )
        link = self._link_in_hall(self.claim_binding, order)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.skipped, 1)                 # 常态：不接手
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(
            JidanpuSyncEvent.objects.count(), 0, "常态却留了台账 / 排了事件（既有口径被破坏）",
        )
        link.refresh_from_db()
        self.assertFalse(link.in_hall, "权威说对方已离开大厅，本地标记没刷新（闸 3 没接上）")

    def test_an_unassigned_remote_status_does_not_write_in_hall_true(self):
        """⭐ **闸 3 的「不扩大」那半**：远端「未分配」⇒ `in_hall` **不回写 True**
        （②式的 5 格回包分不清「厅里挂着的未分配」与「指派录进去的未指派单」⇒ 宁缺毋滥）。
        ⛔ 弄红它：给闸 3 加一个「未分配 ⇒ 置 True」的回写 ⇒ 本用例红。"""
        order = self.make_grab_order()
        link = JidanpuMemberOrderLink.objects.create(
            binding=self.claim_binding, order=order,
            jidanpu_order_id=JID, in_hall=False,
        )
        self.reply_status("未分配")
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.skipped, 1)                 # 未分配：不动作（今日行为）
        self.assertEqual(self.order_status(order), "WAITING_GRAB")
        link.refresh_from_db()
        self.assertFalse(link.in_hall, "未分配那档不许凭 5 格回包置 True（指派单会被错标成在厅）")

    # ── 已完成 × 待抢单：先接手（带抑制）再完成 ────────────────────────────────

    def test_a_done_remote_status_claims_quietly_then_completes(self):
        """⭐⭐ **spec §5 第 6 条**：远端「已完成」× 我方「待抢单」⇒ 先接手（带抑制）、再完成 ⇒ 终态「待验收」，全程**无 peer 向 unlist / assign**。
        ⛔ 弄红它：把②式调用点的抑制去掉 ⇒ in_hall=True 让接手那一步多排assign + unlist ⇒ 事件集合断言红。"""
        order = self.make_grab_order()
        self._link_in_hall(self.claim_binding, order)
        self.reply_status(AUTHORITATIVE)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.claim_binding)

        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self.order_status(order), "PENDING_REVIEW")
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.claim_binding.user_id,
        )
        out_kinds = self._out_kinds()
        self.assertNotIn("member.unlist", out_kinds)
        self.assertNotIn("member.assign", out_kinds)
        # 闸 3：链路标记同样收窄（已完成 ∈ 刷新档）：
        link = JidanpuMemberOrderLink.objects.get(
            binding=self.claim_binding, order=order,
        )
        self.assertFalse(link.in_hall)


# ══ ⑦ 接线：入口闸按 `mode` 分 + ②式那一轮跑哪几段 ══════════════════════════════


@contextmanager
def _fake_tenants(tenant):
    """把命令的租户遍历换成**只有这一个**租户（⛔ 不碰平台面）。⚠️ **①式那几条路全部换成会抛的替身**（②式那一轮跑了它们就是「跑了但必然失败」，任务书明禁）——判据是「这几条路一次都没被调到」。⭐ **`run_outbox` 这一格要分线**（步骤 5 接线后必须改，如实登记）：
    ②式那一轮的第三段（出向发单）**按设计**要经**同一个**共享引擎（`jidanpu_member.run_outbox(..., line=②式那一份)` —— 判据「过没过闸」只有引擎那一处）⇒ 把整格换成会抛的替身会把**合法**的那一跳也判成违规。⇒ 这一格改成：**①式那一份**（没给 `line`）才抛；②式那一份**转交真函数**（开关默认关着 ⇒ 它在那道闸处提前返回，**一次上游都不打、一个字节都不写**）。
    ⛔ 这不是放宽：①式那条路仍然「碰一下就红」，而②式那一段的「零上游、零写入」由`tests/test_jidanpu_member_openapi_outbox.py` 里走真命令的那两条用例守着。"""
    from apps.business.management.commands import jidanpu_sync as module

    @contextmanager
    def _noop_context(_tenant):
        yield

    objects = mock.MagicMock()
    objects.exclude.return_value.order_by.return_value = [tenant]
    patcher = mock.patch.object(module.Tenant, "objects", objects)

    def _boom(*args, **kwargs):
        raise AssertionError("②式那一轮竟然跑了①式的登录式读路")

    real_run_outbox = module.jidanpu_member.run_outbox

    def _no_password_outbox(binding, **kwargs):
        if kwargs.get("line") is None:
            raise AssertionError("②式那一轮竟然跑了①式那一份 outbox（登录式写面）")
        return real_run_outbox(binding, **kwargs)

    with patcher, mock.patch.object(module, "tenant_context", _noop_context), \
         mock.patch.object(module.jidanpu, "open_client_with_http", _boom), \
         mock.patch.object(module.jidanpu_member, "heal_binding_identity", _boom), \
         mock.patch.object(module.jidanpu_member, "run_outbox", _no_password_outbox), \
         mock.patch.object(module.jidanpu_member, "poll_log", _boom), \
         mock.patch.object(module.jidanpu_member, "pull_statuses", _boom), \
         mock.patch.object(module.jidanpu_member, "reconcile_and_queue", _boom), \
         mock.patch.object(module.timezone, "now", return_value=ROUND_NOW):
        yield


class WiringTests(_MemberOpenApiCase):
    def test_the_entry_gate_lets_an_account_password_empty_openapi_binding_run(self):
        """⭐ **入口闸按 `mode` 分**（E-0202 那个坑）：账号密码为空的下家②式绑定**真的跑**。
        ⛔ 弄红它：把 `_run_member_bindings` 的闸门改回 `jidanpu_member.credentials_missing`
        （只看地址 / 账号 / 密码）⇒ 这一行被**整行静默跳过** ⇒ 订单状态不变 ⇒ 红。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")
        self.assertEqual(self.binding.account, "")
        self.assertEqual(self.binding.password, "")

        tenant = mock.Mock(schema_name="tenant-member-openapi")
        with _fake_tenants(tenant):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        self.assertEqual(self.order_status(order), "PENDING_REVIEW", "这一行被整行静默跳过了")

    def test_the_openapi_round_runs_the_three_segments_in_the_fixed_order(self):
        """⭐⭐ ②式那一轮**恰好三段、顺序固定**：回调消费 → 兜底对账 → **出向发单**。⭐ **顺序判据（步骤 5 的任务书要求「钉住段序」）**：三段各自被调一次，且**有序**。⛔ 弄红它（三种改法都会红，逐条试过其中两种见报告）：
        ① 把出向发单挪到回调消费**之前** ⇒ 台账 `attempts` 会先被烧一次（见下一条用例的「缺系统单号」判据），这里先红在顺序上；② 把兜底对账与出向发单对调 ⇒ 顺序红；③ 干脆不接线（保留「⛔ 不跑」）⇒ 第三段一次都不被调 ⇒ 红。
        ⚠️ **为什么是「消费 → 对账 → 发单」而不是①式那种「outbox 在前」**：见`_run_member_openapi_binding` 的 docstring 那张表（spec §5.5 的承重口径 +缺号自愈那条硬依赖 + 与上家线那一支同形状）。⚠️ ①式那五段一段都没跑（`_fake_tenants` 里那几个会抛的替身守着）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")
        seen: list[str] = []
        real_consume = jidanpu_member_openapi.consume_member_callbacks
        real_reconcile = jidanpu_member_openapi.reconcile_member_statuses
        real_outbox = jidanpu_member_openapi_outbox.run_member_status_outbox

        def _consume(binding, **kwargs):
            seen.append("consume")
            return real_consume(binding, **kwargs)

        def _reconcile(binding, **kwargs):
            seen.append("reconcile")
            return real_reconcile(binding, **kwargs)

        def _outbox(binding, **kwargs):
            seen.append("outbox")
            return real_outbox(binding, **kwargs)

        tenant = mock.Mock(schema_name="tenant-member-openapi")
        with _fake_tenants(tenant), \
             mock.patch.object(
                 jidanpu_member_openapi, "consume_member_callbacks", side_effect=_consume,
             ), \
             mock.patch.object(
                 jidanpu_member_openapi, "reconcile_member_statuses", side_effect=_reconcile,
             ), \
             mock.patch.object(
                 jidanpu_member_openapi_outbox, "run_member_status_outbox", side_effect=_outbox,
             ):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        self.assertEqual(seen, ["consume", "reconcile", "outbox"])
        self.assertEqual(self.order_status(order), "PENDING_REVIEW")

    def test_a_password_mode_member_binding_never_reads_the_event_file(self):
        """①式（`mode="password"`）的下家绑定 ⇒ ⛔ **一次文件都不读**（零成本）。"""
        binding = self.make_binding(
            code=CODE_SECOND, name="password-mode", account="acc-pw", password="pw",
        )
        # ⚠️ 码由模型自己生成（`mode="openapi"` 且没有码 ⇒ 生成一串）⇒ 必须先建成②式再改回①式，
        #    ⛔ 不能靠 `callback_code=""` 造一行①式（那一个参数会被 `save()` 覆盖掉）。
        binding.mode = "password"
        binding.callback_code = ""
        binding.save(update_fields=["mode", "callback_code"])

        def _boom(*args, **kwargs):
            raise AssertionError("①式绑定竟然去读了回调事件文件")

        #: ⚠️读口是增量读（`_scan`）⇒ 替身要打在新口上（旧口 `iter_lines` 已不用）。
        with mock.patch.object(jidanpu_openapi_consume, "_scan", _boom):
            outcome = jidanpu_member_openapi.consume_member_callbacks(binding, now=ROUND_NOW)

        self.assertFalse(outcome.enabled)

    def test_the_health_columns_are_stamped_on_a_clean_openapi_round(self):
        """§9.4 第 3 问：②式那一轮**复用本表既有四列**（不新造一套），且**真的做了事**才盖成功。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        tenant = mock.Mock(schema_name="tenant-member-openapi")
        with _fake_tenants(tenant):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        fresh = self.refresh()
        self.assertEqual(fresh.last_ok_at, ROUND_NOW)
        self.assertEqual(fresh.last_run_at, ROUND_NOW)
        self.assertEqual(fresh.last_error, "")

    def test_nothing_done_does_not_claim_success(self):
        """⛔ **什么都没做不许盖成功**（裁定 1）：没有事件、对账也没到点 ⇒ `last_ok_at` 不动。"""
        tenant = mock.Mock(schema_name="tenant-member-openapi")
        with _fake_tenants(tenant):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        fresh = self.refresh()
        self.assertEqual(fresh.last_run_at, ROUND_NOW, "②式那一行会显示「从未运行」（说假话）")
        self.assertIsNone(fresh.last_ok_at)

    def test_the_alert_of_a_bad_code_reaches_the_binding_last_error(self):
        """⭐ 要人看见的那句话**真的落到绑定 `last_error`**（⛔ 不是只写日志 / 只写台账）。"""
        self.assertEqual(
            self.post_callback(code=CODE_UNCLAIMED, secret=SECRET_UNCLAIMED), "200 OK",
        )
        tenant = mock.Mock(schema_name="tenant-member-openapi")
        with _fake_tenants(tenant):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        fresh = self.refresh()
        self.assertIn(CODE_UNCLAIMED, fresh.last_error)
        self.assertEqual(fresh.last_error_at, ROUND_NOW)


# ══ ⑧ 健康列（§9.4 第 2 问）的判据与阈值 ═══════════════════════════════════════


class HealthTests(_MemberOpenApiCase):
    def test_the_member_gate_value_is_180_seconds(self):
        """⭐ 窗口 = `max(900, 10 × 本表 reconcile_seconds)`：下家 **180** ⇒ **1800** 秒。
        ⛔ 弄红它：`lag_window` 里写死上家那个模型的默认值（600）⇒ 下家行算出 6000 秒 ⇒ 红
        （一台真没人消费的机器要多等 3.3 倍时间才在页面上红起来，而且**静默**）。"""
        self.assertEqual(jidanpu_openapi_health.LAG_ROUNDS, 10)
        self.assertEqual(jidanpu_openapi_health.LAG_WINDOW_FLOOR, 900)
        self.assertEqual(jidanpu_openapi_health.lag_window(self.binding), timedelta(seconds=1800))

    def test_a_zeroed_gate_column_falls_back_to_this_tables_own_default(self):
        """闸门列存 `0`（配置写坏）⇒ 按**本表**这一列的默认值算（下家 180 ⇒ 1800 秒）。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(reconcile_seconds=0)
        fresh = self.refresh()
        self.assertEqual(jidanpu_openapi_health.lag_window(fresh), timedelta(seconds=1800))

    def test_the_upstream_line_keeps_its_own_window(self):
        """⭐ 上家那条线**一个字都没变**（600 ⇒ 6000 秒）：两条线各按自己的闸门值算。"""
        from apps.business.models import JidanpuUpstreamBinding

        make_logged_in_client(username="kefu-health-upstream", groups=("客服",))
        upstream = JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username="kefu-health-upstream"), jidanpu_base_url=BASE_URL,
            account="acc-health-upstream", password="pw-synthetic", mode="openapi",
            client_id=f"{CLIENT_ID}-health", client_secret=SENTINEL_SECRET,
        )
        self.assertEqual(jidanpu_openapi_health.lag_window(upstream), timedelta(seconds=6000))

    def test_a_stalled_consumer_is_flagged_but_low_volume_is_not(self):
        """⭐ 「回调在来、水位长期不动」⇒ 判红；「低单量两小时没单」⇒ ⛔ **不判红**。
        ⚠️ 阈值是 **1800** 秒（下家本表的 180 × 10 轮）——用例里的两个读数刻意跨过它。"""
        # ① 真没人消费：水位停在 3000 秒前，而回调刚刚还在来
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=ROUND_NOW, callback_watermark_at=ROUND_NOW - timedelta(seconds=3000),
        )
        stalled = jidanpu_member_openapi.member_consumer_state(self.refresh(), now=ROUND_NOW)
        self.assertTrue(stalled.bad)
        self.assertIn("没有任何东西在处理它", stalled.text)

        # ② 低单量：两小时没单（两个时刻**停在同一个点**）⇒ 差恒为 0 ⇒ **不报警**
        long_ago = ROUND_NOW - timedelta(hours=2)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=long_ago, callback_watermark_at=long_ago,
        )
        quiet = jidanpu_member_openapi.member_consumer_state(self.refresh(), now=ROUND_NOW)
        self.assertFalse(quiet.bad)

    def test_a_password_mode_binding_says_it_has_nothing_to_consume(self):
        binding = self.make_binding(
            code=CODE_SECOND, name="pw-health", account="acc-pw2", password="pw",
        )
        binding.mode = "password"
        binding.callback_code = ""
        binding.save(update_fields=["mode", "callback_code"])
        state = jidanpu_member_openapi.member_consumer_state(binding, now=ROUND_NOW)
        self.assertFalse(state.bad)
        self.assertEqual(state.text, jidanpu_openapi_health.PASSWORD_MODE_TEXT)


# ══ ⑨ 哨兵：密钥一个字节都不许进台账 / 事件文件 ════════════════════════════════


class SecretHygieneTests(_MemberOpenApiCase):
    def test_no_secret_reaches_the_journal_or_the_event_file(self):
        """⭐ 消费者**不读密钥**（B3）：跑完之后台账与事件文件里都搜不到那把哨兵密钥。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        self.consume()

        rows = list(JidanpuSyncEvent.objects.values_list("kind", "reason", "payload"))
        self.assertNotIn(SENTINEL_SECRET, json.dumps(rows, ensure_ascii=False, default=str))
        self.assertNotIn(SENTINEL_SECRET, self.events.read_text(encoding="utf-8"))


# ══ ⑩「立即同步」端点的②式那一支（官方 API 对接线；spec §9.3 / §27.1）══════════
# ⚠️ 这一组就是**上家线栽过的那个同形坑**（E-0202 / spec §27.1）的判据：`JidanpuMemberSyncNowView` 原来**无条件**调 `jidanpu_member.pull_statuses`，而那一跳会拿**空账号密码**去建①式的登录连接 ⇒ ②式用户点「立即同步」拿到的是「凭据不完整」，而②式真正的同步（回调消费 + 兜底对账）**一次都不会跑**。
# ⇒ 判据 = 这一跳**真的跑了那两段**（订单行真变了 / 台账里 `trigger` 分得开），并且⛔ **一次登录连接都不建**。

#: 「立即同步」的行主（本类自己那一行；⚠️ 码与夹具基类那两行**不同** —— 本类要一个自己能认出来的码）。
SYNC_NOW_USERNAME = "kefu-member-syncnow-openapi"
CODE_SYNC_NOW = "Gh7_-memberopenapi_D22"
SECRET_SYNC_NOW = "tt-member-openapi-secret-0004"
assert len(CODE_SYNC_NOW) == 22

SYNC_NOW_URL = "/api/business/member/jidanpu-sync-now/"

#: ⚠️ **本类那一行换一个地址**：这个夹具要的是一个**与基类那一行分得开**的站点（页面/接口上的「立即同步」只该动本人那一行）。
#: ⛔ `(jidanpu_base_url, account)` 的唯一约束（`uniq_jdp_member_account`）**只对①式生效**（`condition=Q(mode="password")`，见 `JidanpuMemberBinding.Meta.constraints`），**同一个域名上的第二条②式绑定完全合法**（还共用同一个回调码）。⇒ 这一行现在**不是**为了躲约束，只是为了「一个分得开的站点」这个夹具目的。
SYNC_NOW_BASE_URL = "https://jidanpu-member-syncnow.test.invalid"


class SyncNowOpenApiTests(_MemberOpenApiCase):
    """②式「立即同步」：**真的跑一轮**（回调消费 + 兜底对账），⛔ 不再回「凭据不完整」。
    ⚠️ 夹具刻意按**界面的形态**造：`account=""` / `password=""`（②式界面把这两格藏起来了）——旧实现无条件走①式的 `open_client_with_http` ⇒ 建连处抛`ConfigError("…账号 / 密码…")` ⇒ 用户拿到「凭据不完整」，而②式真正的同步**一次都不会跑**。"""

    def setUp(self):
        super().setUp()
        #: 频控状态是**进程内**的 ⇒ 每个用例换一个新的闸门（照本仓另一条线的同一取舍）。
        jidanpu_member_views._SYNC_NOW_GATE = jidanpu_member_views._SyncNowGate()
        self.client = make_logged_in_client(
            username=SYNC_NOW_USERNAME, groups=("客服",), nodes=("order.change_status",),
        )
        #: 本类那一行的「码 → 密钥」也要在那份 0600 文件里（接收器按码取密钥验签）。
        with self.keys_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{CODE_SYNC_NOW}\t{SECRET_SYNC_NOW}\n")
        self.binding = JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=SYNC_NOW_USERNAME),
            jidanpu_base_url=SYNC_NOW_BASE_URL, account="", password="",
            mode="openapi", client_id=f"{CLIENT_ID}-syncnow", client_secret=SECRET_SYNC_NOW,
            callback_code=CODE_SYNC_NOW, enabled=True,
        )

    def _post_my_callback(self, *, remote_id=JID, order_no=ORDER_NO) -> str:
        """打一条**属于本类那一行**的真回调（码 / 密钥 / 下家单号都是本类的合成值）。"""
        params = {
            "orderNo": remote_id, "sourceOrderNo": order_no, "status": CALLBACK_STATUS,
            "note": "", "action": "10", "timestamp": "1791500000",
        }
        return self.post_callback(code=CODE_SYNC_NOW, secret=SECRET_SYNC_NOW, params=params)

    # ── 正路：②式点一下 ⇒ 两段都真的跑了 ──────────────────────────────────────

    def test_the_button_really_runs_the_callback_and_the_reconcile_rounds(self):
        """⭐ **这一条就是本次要修的东西**：②式（账号 / 密码都是空的）点「立即同步」⇒两张单都被改对，而**它们的结论来自两条不同的路**（有回调那张走消费、没回调那张走对账）。判据（⛔ 全部打在**数据库行**上，E-0009 —— 不看 stdout、不看 logger）：
        ① 两张订单行都变成「待验收」（下家线只认逐字「已完成」⇒ 写的是待验收）；② 台账里那两条结论**分得出来**哪一条是回调消费写的、哪一条是兜底对账写的（下家线把「这一笔是谁做的」写进人话原因里）——⭐ 这一条就是「回调消费**且**兜底对账都真的跑了」的机械判据；
        ③ 回给页面的话里⛔ 没有「账号」那类话（②式压根不需要账号密码）；④ ⛔ **一次①式的登录连接都没建**（建了就是走错路）。⛔ 弄红它：把分流去掉（②式也走①式那一跳）⇒ 建连处抛 `ConfigError`⇒ 信封回业务失败码（①那条断言红）、订单一张都不变（两向都红）、台账零行。"""
        with_callback = self.make_order(order_no=ORDER_NO)
        self.make_link(self.binding, with_callback, remote_id=JID)
        without_callback = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, without_callback, remote_id=JID_B)
        self.reply_status()
        self.assertEqual(
            self._post_my_callback(), "200 OK", "夹具坏了：真接收器没落盘",
        )

        def _no_login(*args, **kwargs):
            raise AssertionError("②式这一跳⛔ 不许建①式的登录连接（分流判据只看 mode）")

        with mock.patch.object(jidanpu, "open_client_with_http", _no_login):
            res = self.client.post(SYNC_NOW_URL)

        body = res.json()
        # ① 这一跳成功了（不是「凭据不完整」那一档）
        self.assertEqual(body["code"], 0, res.content)
        self.assertNotIn("账号", body["msg"], body["msg"])
        self.assertNotIn("凭据", body["msg"], body["msg"])
        # ② 订单状态**真的变了**（重新查库）
        # ⚠️ 下家线**只认逐字「已完成」**⇒ 落的是「待验收」（⛔ 不是上家线那个映射态：
        #    两条线的状态语义各自一套，本任务明令不许动下家线那套）。
        self.assertEqual(self.order_status(with_callback), str(OrderStatus.PENDING_REVIEW))
        self.assertEqual(self.order_status(without_callback), str(OrderStatus.PENDING_REVIEW))
        # ③ 两条路的结论都在台账里，且**分得出来**是哪一段写的
        # ⚠️ 下家线**不往 payload 里放 `trigger`**（那是上家线那条路的形状）——它把「这一笔是谁做的」写进**人话原因**里（`_APPLIED_REASONS` 三条各一句）⇒ 判据打在 reason 上（⛔ 不是从被测模块的常量派生：那不会红，E-0024）。
        by_order = {
            row.target_ref: row.reason
            for row in JidanpuSyncEvent.objects.filter(
                kind="member.status_in", status="done",
            )
        }
        self.assertIn(
            "回调", by_order.get(ORDER_NO, ""),
            "有回调那一张不是回调消费改的 ⇒ 消费那一段没跑",
        )
        self.assertIn(
            "兜底对账", by_order.get(ORDER_NO_B, ""),
            "没回调那一张没被兜底对账兜住 ⇒ 对账那一段没跑",
        )
        self.assertEqual(body["data"]["applied"], 2, body)

    def test_the_round_really_hits_the_upstream_read_endpoint(self):
        """②式那一跳真的打的是**只读**端点，⛔ 一次写接口都不打。
        ⚠️ 与上一条各自承重：上一条钉「订单变了」，这一条钉「走的是官方 API 那条只读路」（②式的凭据只对开放平台有效，⛔ 不可能去打登录式那几条写接口）。
        ⚠️ **2026-10-09 起恰好是那两个端点**（`GetOrderStatus` + B 段的 `GetOrderProcessImgs`）：写「待验收」之前多了一发拉全量进度图的**只读**请求（业务方点名的截图同步）⇒ 判据从「恰好一个」改成「恰好这两个」——⛔ 仍然**不是**放宽成 `toContain`：多出任何第三个端点（尤其写接口）当场红。"""
        order = self.make_order(order_no=ORDER_NO)
        self.make_link(self.binding, order, remote_id=JID)
        self.reply_status()
        self.assertEqual(self._post_my_callback(), "200 OK", "夹具坏了：真接收器没落盘")

        res = self.client.post(SYNC_NOW_URL)

        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertTrue(self.upstream.token_calls(), "一次 token 都没取（②式那一跳没跑）")
        self.assertEqual(
            {row["path"] for row in self.upstream.api_calls()},
            {ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH},
            "②式那一跳打了别的端点（写接口？）",
        )
        for row in self.upstream.api_calls():
            with self.subTest(path=row["path"]):
                self.assertEqual(row["method"], "GET", "②式那一跳里出现了非只读请求")

    def test_a_failed_round_says_so_instead_of_claiming_success(self):
        """本线「立即同步」**不写健康列** ⇒ ②式那一支的失败只能看**本轮结果**：有失败行 ⇒ 业务失败码 + 一句人话（⛔ 不许回 `code 0` 一片空白 ＝「成功但什么都没发生」）。
        ⛔ 弄红它：把视图里 `if openapi_failed:` 那一段删掉（照旧只回 `{"applied": N}`）⇒ 本用例红（订单一个字没动，页面却报成功）。"""
        order = self.make_order(order_no=ORDER_NO)
        self.make_link(self.binding, order, remote_id=JID)
        #: 取 token 那一跳**网络失败** ⇒ 两段都查不动。
        self.upstream.boom(jidanpu_openapi.TOKEN_PATH, httpx.ConnectError("合成：连不上"))
        self.assertEqual(self._post_my_callback(), "200 OK", "夹具坏了：真接收器没落盘")

        res = self.client.post(SYNC_NOW_URL)

        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertTrue(body["msg"], body)
        self.assertEqual(self.order_status(order), "IN_PROGRESS", "什么都没查成却改了订单")

    # ── 反向对照：①式行上留着 `client_id` ⇒ 仍然走①式那一支（E-0021 家族）──────

    def test_a_password_row_with_a_leftover_client_id_still_goes_the_login_way(self):
        """⭐ 分流判据**只有 `mode`**（spec §2.1）：①式行上**留着** `client_id`（切来切去不重输的形态）⇒ 这一跳必须走①式那一段（`pull_statuses`），⛔ 不许因为 `client_id` 非空就去跑回调消费 / 兜底对账。
        ⛔ 弄红它：把分流判据写成 `if binding.client_id:`（或任何 `client_id` 真值判据）⇒ ②式那两段被调用 ⇒ 下面那两个「一调就抛」的桩让本用例**响亮地红**（⛔ 不是静默通过）。
        ⚠️ `pull_statuses` 在这里**打桩**（它内部要建①式的登录连接 ⇒ 真网络）；本用例要钉的只是「**哪一支**被走到」，①式那一跳自己的行为由`tests/test_jidanpu_member_status.py` 钉。"""
        self.binding.mode = "password"
        self.binding.account = "kefu-member-syncnow-login"
        self.binding.password = "pw-canary-syncnow-login"
        self.binding.client_id = f"{CLIENT_ID}-leftover"   # ← 切来切去留下来的那一格
        self.binding.save(update_fields=["mode", "account", "password", "client_id"])

        def _boom(*args, **kwargs):
            raise AssertionError("①式这一跳不许跑②式那两段（分流判据只看 mode）")

        with mock.patch.object(jidanpu_member, "pull_statuses", return_value=1) as slow, \
                mock.patch.object(
                    jidanpu_member_openapi, "consume_member_callbacks", _boom), \
                mock.patch.object(
                    jidanpu_member_openapi, "reconcile_member_statuses", _boom):
            res = self.client.post(SYNC_NOW_URL)

        body = res.json()
        self.assertEqual(body["code"], 0, res.content)
        self.assertEqual(body["data"]["applied"], 1, body)
        self.assertTrue(slow.called, "①式那一跳一次都没跑（它没走对支）")


# ══ ⑧ ⭐ 老地址那条例外（`/jidanpu/v1/notify` ⇒ 下家②式绑定）════════════════════════
# 业务方点名的「**一个例外**」：平台侧那条老回调线（`https://callback.suqing.chat/jidanpu/v1/notify`）已经收了 6000+ 条真回调、还在收，它**没有任何回调码** ⇒ 只能靠报文里的 `orderNo` 在**本租户**里找链路。
# ⚠️⚠️ **边界（承重）**：老地址所在的那台机器就是 **kexing**（`callback.suqing.chat`→ `111.229.154.5`）⇒ 「老地址的行归哪个租户」是**机器级**的事实，⛔ **不是**一条对谁都成立的通用做法。实现上是一个**默认关着**的闸（`JIDANPU_LEGACY_CALLBACK_TENANT` = 那一个租户的 schema 名）⇒ 别的机器照抄这一行也**不会**生效。本节的用例把闸打开（模拟 kexing 那台机），⛔ 不改任何生产默认值。

#: 老回调线那一把**单一** `clientSecret`（合成值；`shared/callback.env` 那个形状）。
LEGACY_SECRET = "tt-legacy-notify-secret-0001"
#: 老回调线**没有码**，只有这一条固定路径。
LEGACY_PATH = callback_wsgi.NOTIFY_PATH
#: 老地址那张单的远端单号 / 我方单号（与 ① 节那几串区分开）。
JID_LEGACY = "JDP-MEMBER-000000000000000911"
ORDER_NO_LEGACY = "20261008000000000911"
#: 一个**没有任何链路**的远端单号（老地址会收到**整台鸡蛋铺系统**的回调 ⇒ 这是常态）。
JID_NOT_OURS = "JDP-MEMBER-000000000000000999"


class LegacyNotifyTests(_MemberOpenApiCase):
    """老地址的回调按 `orderNo` 落到**正确的那条**②式绑定上（找不到 ⇒ 只台账、不进错误列）。"""

    def setUp(self):
        super().setUp()
        #: 老地址那把密钥在**另一个文件**（`shared/callback.env`，`K=V` 形状）——
        #: 接收器那两条路**各读自己的文件**，⛔ 不互相兜底。
        self.legacy_env = self.tmp / "shared" / "callback.env"
        self.legacy_env.write_text(
            f"# 合成：老回调线那一把单一密钥\n{callback_env_var()}={LEGACY_SECRET}\n",
            encoding="utf-8",
        )
        self.legacy_env.chmod(0o600)
        legacy = mock.patch.dict(os.environ, {"CALLBACK_ENV_FILE": str(self.legacy_env)})
        legacy.start()
        self.addCleanup(legacy.stop)

    # ── 夹具 ────────────────────────────────────────────────────────────────

    def enable_legacy_route(self):
        """把那条**默认关着**的闸打开（= kexing 那台机的 `.env` 里那一行）。"""
        patcher = mock.patch.dict(
            os.environ,
            {jidanpu_member_openapi.ENV_LEGACY_CALLBACK_TENANT: connection.schema_name},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def post_legacy(self, *, order_no=JID_LEGACY, status=CALLBACK_STATUS, action="10",
                    extra=None, received_at=FIXED_1) -> str:
        """走**真接收器**打一条**老路径**的 POST（老地址只有这一条路径、没有码）。"""
        params = self.status_params(order_no=order_no, status=status, action=action)
        params.update(extra or {})
        return self.post_callback(
            path=LEGACY_PATH, secret=LEGACY_SECRET, params=params, received_at=received_at,
        )

    def order_status_of(self, order) -> str:
        return Order.objects.get(pk=order.pk).status

    def _colliding_store_links(self):
        self.enable_legacy_route()
        other = self.make_binding(code=CODE_SECOND, name="other-store")
        other.jidanpu_base_url = "https://second-legacy-shop.test.invalid"
        other.save(update_fields=["jidanpu_base_url"])
        first = self.make_order(order_no=ORDER_NO)
        second = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        self.make_link(self.binding, first, remote_id=JID_LEGACY)
        self.make_link(other, second, remote_id=JID_LEGACY)
        return other, first, second

    def test_a_colliding_legacy_screenshot_only_lands_on_the_matching_source_order(self):
        other, first, second = self._colliding_store_links()
        self.assertEqual(self.post_legacy(action="30", extra={
            "sourceOrderNo": HEAL_SOURCE_ORDER_NO,
            "imgUrl": SHOT_URL_DIRECT, "name": "合成截图", "note": "订单截图",
        }), "200 OK")

        with mock.patch.object(jidanpu_upstream_pics, "download_pic", return_value=JPEG_BODY) as download:
            outcomes = self.consume(self.binding), self.consume(other)

        self.assertEqual([outcome.applied for outcome in outcomes], [0, 1])
        self.assertFalse(OrderScreenshot.objects.filter(order=first).exists())
        self.assertEqual(OrderScreenshot.objects.filter(order=second).count(), 1)
        download.assert_called_once_with(SHOT_URL_DIRECT)

    def test_a_colliding_legacy_status_can_land_before_the_other_store_polls(self):
        other, first, second = self._colliding_store_links()
        self.reply_status()
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK")

        self.assertEqual(self.consume(other).applied, 1)
        self.assertEqual(self.consume(self.binding).applied, 0)
        self.assertEqual(self.order_status_of(first), "IN_PROGRESS")
        self.assertEqual(self.order_status_of(second), "PENDING_REVIEW")

    def test_a_legacy_screenshot_for_a_retired_copy_never_lands_on_the_other_store(self):
        other, first, second = self._colliding_store_links()
        JidanpuMemberOrderLink.objects.filter(binding=other, order=second).update(retired_at=FIXED_1)
        self.assertEqual(self.post_legacy(action="30", extra={
            "sourceOrderNo": HEAL_SOURCE_ORDER_NO,
            "imgUrl": SHOT_URL_DIRECT, "name": "合成截图", "note": "订单截图",
        }), "200 OK")

        with mock.patch.object(jidanpu_upstream_pics, "download_pic", return_value=JPEG_BODY) as download:
            self.assertEqual(self.consume(other).applied, 0)
            self.assertEqual(self.consume(self.binding).applied, 0)

        download.assert_not_called()
        self.assertFalse(OrderScreenshot.objects.filter(order__in=(first, second)).exists())

    def _assert_legacy_screenshot_does_not_guess_a_blank_link(self, *, same_source):
        other, first, second = self._colliding_store_links()
        if same_source:
            first.source_order_no = HEAL_SOURCE_ORDER_NO
            first.save(update_fields=["source_order_no"])
        blank = JidanpuMemberOrderLink.objects.get(binding=other, order=second)
        blank.jidanpu_order_id = " \t"
        blank.save(update_fields=["jidanpu_order_id"])
        self.assertEqual(self.post_legacy(action="30", extra={
            "sourceOrderNo": HEAL_SOURCE_ORDER_NO,
            "imgUrl": SHOT_URL_DIRECT, "name": "合成截图", "note": "订单截图",
        }), "200 OK")

        with mock.patch.object(jidanpu_upstream_pics, "download_pic", return_value=JPEG_BODY) as download:
            self.assertEqual(self.consume(self.binding).applied, 0)
            self.assertEqual(self.consume(other).applied, 0)

        download.assert_not_called()
        self.assertFalse(OrderScreenshot.objects.filter(order__in=(first, second)).exists())
        blank.refresh_from_db()
        self.assertEqual(blank.jidanpu_order_id, " \t")
        return other, second, blank

    def test_a_legacy_screenshot_cannot_guess_when_its_source_has_a_blank_link(self):
        other, second, blank = self._assert_legacy_screenshot_does_not_guess_a_blank_link(same_source=False)

        self.reply_status()
        with self.keys_file.open("a", encoding="utf-8") as keys:
            keys.write(f"{CODE_SECOND}\t{SECRET_MINE}\n")
        params = self.status_params(order_no=JID_LEGACY)
        params["sourceOrderNo"] = HEAL_SOURCE_ORDER_NO
        self.assertEqual(self.post_callback(code=CODE_SECOND, params=params, received_at=FIXED_2), "200 OK")
        self.assertEqual(self.consume(other).applied, 1)
        blank.refresh_from_db()
        self.assertEqual(blank.jidanpu_order_id, JID_LEGACY)
        self.assertEqual(self.order_status_of(second), "PENDING_REVIEW")

    def test_a_legacy_screenshot_cannot_guess_when_a_blank_copy_shares_the_same_source(self):
        self._assert_legacy_screenshot_does_not_guess_a_blank_link(same_source=True)

    def test_a_blank_link_for_the_same_order_does_not_block_its_legacy_screenshot(self):
        self.enable_legacy_route()
        other = self.make_binding(code=CODE_SECOND, name="blank-same-order")
        order = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        self.make_link(self.binding, order, remote_id=JID_LEGACY)
        self.make_link(other, order, remote_id="")
        self.assertEqual(self.post_legacy(action="30", extra={
            "sourceOrderNo": HEAL_SOURCE_ORDER_NO,
            "imgUrl": SHOT_URL_DIRECT, "name": "合成截图", "note": "订单截图",
        }), "200 OK")

        with mock.patch.object(jidanpu_upstream_pics, "download_pic", return_value=JPEG_BODY) as download:
            self.assertEqual(self.consume(self.binding).applied, 1)
            self.assertEqual(self.consume(other).applied, 0)

        download.assert_called_once_with(SHOT_URL_DIRECT)
        self.assertEqual(OrderScreenshot.objects.filter(order=order).count(), 1)

    def test_a_reused_source_number_does_not_route_legacy_status_to_the_other_store(self):
        other, first, second = self._colliding_store_links()
        original = "SYNTH-LONG-" + "x" * 70
        first.source_order_no = original
        first.save(update_fields=["source_order_no"])
        api = make_logged_in_client(
            username="legacy-source-number-editor", groups=("客服",),
            nodes=("order.add_edit", "order.view_detail", "order.list"),
        )

        for order, source in ((first, "SYNTH-RENUMBERED"), (second, original)):
            response = api.patch(
                f"/api/business/orders/{order.order_no}/",
                {"source_order_no": source, "project_text": "合成项目"},
                content_type="application/json",
            )
            self.assertEqual(response.json()["code"], 0, response.json())

        self.reply_status()
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": original}), "200 OK")
        self.assertEqual(self.consume(other).applied, 0)
        self.assertEqual(self.consume(self.binding).applied, 0)
        self.assertEqual(self.order_status_of(first), "IN_PROGRESS")
        self.assertEqual(self.order_status_of(second), "IN_PROGRESS")
        self.assertEqual(self.upstream.api_calls(), [])

    def test_a_legacy_collision_with_no_unique_source_does_not_heal_an_unrelated_link(self):
        other, first, second = self._colliding_store_links()
        blank_order = self.make_order(order_no=ORDER_NO_B, source_order_no="合成其他来源")
        blank_link = self.make_link(self.binding, blank_order, remote_id="")
        self.reply_status()
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": "合成其他来源"}), "200 OK")

        for binding in (self.binding, other):
            self.assertEqual(self.consume(binding).applied, 0)

        blank_link.refresh_from_db()
        self.assertEqual(blank_link.jidanpu_order_id, "")
        self.assertEqual(self.order_status_of(first), "IN_PROGRESS")
        self.assertEqual(self.order_status_of(second), "IN_PROGRESS")
        self.assertEqual(self.upstream.api_calls(), [])

    # ── 判据 ────────────────────────────────────────────────────────────────

    def test_a_legacy_callback_is_processed_like_a_second_style_one(self):
        """⭐ **主判据**：老地址的一条回调 ⇒ 跑一轮 ⇒ **订单真的被处理了**（不是只留一条台账）。⛔ 弄红它：把引擎 ③ 里那个「老路径 ⇒ 进 `pending`」的分支去掉（或把`legacy_notify` 钩子摘掉）⇒ 这一条落到 `_note_foreign_path`（旧路径那一档）⇒订单还是「代练中」、台账里是 `foreign_path` ⇒ 前两条断言当场红。
        ⚠️ 状态按**②式那套判据**走：报文里的 `status`（「问题单」）**不作数**，权威值「已完成」⇒ 写「待验收」。"""
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY)
        self.make_link(self.binding, order, remote_id=JID_LEGACY)
        self.reply_status()
        self.assertEqual(self.post_legacy(), "200 OK")

        outcome = self.consume()

        # ① 订单**真的**被处理了（重新查库）
        self.assertEqual(self.order_status_of(order), "PENDING_REVIEW")
        self.assertEqual(outcome.applied, 1)
        # ② 审计一行（与②式那条路逐字同形）
        audit = OperationLog.objects.filter(order_no=ORDER_NO_LEGACY, action="order.transition")
        self.assertEqual(audit.count(), 1)
        self.assertEqual((audit.get().from_value, audit.get().to_value), ("代练中", "待验收"))
        # ③ 它是**被处理**的，⛔ 不是「不是本线路径」那一档（后者只是看得见的跳过）
        self.assertEqual(self.notes(kind="member.openapi_foreign_path").count(), 0)
        # ④ 水位照推（用我们自己的 `received_at`）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)

    def test_the_route_lands_on_the_binding_that_owns_the_link(self):
        """⭐ **两条绑定时按单号落到正确的那条**（要能红）。⛔ 弄红它：把 `_link_of(legacy=True)` 换回「本行自己那一组」（`member_group_bindings`）⇒ 跑 A 那一轮时找不到 B 的单 ⇒ 留一条 `member.openapi_legacy_unknown`⇒ 第三条断言红（那正是「**假警报**」：明明有链路，却说找不到）。
        ⚠️ 两行**刻意用不同的码**：这样「按码分组」这条路**根本不可能**找到别人家的单（同码那两条用例测不出这个缺陷）。"""
        self.enable_legacy_route()
        sibling = self.make_binding(code=CODE_SECOND, name="second")
        order_b = self.make_order(order_no=ORDER_NO_LEGACY)
        self.make_link(sibling, order_b, remote_id=JID_LEGACY)
        self.reply_status()
        self.assertEqual(self.post_legacy(), "200 OK")

        # ① 跑 A 那一轮：单是 B 的 ⇒ A ⛔ 一个字都不写、**也不留假警报**
        first = self.consume(self.binding)
        self.assertEqual(self.order_status_of(order_b), "IN_PROGRESS")
        self.assertEqual(
            self.notes(kind="member.openapi_legacy_unknown").count(), 0,
            "A 找不到别人家的单就报「找不到链路」= 假警报",
        )
        self.assertEqual(first.findings, ())
        self.assertEqual(self.upstream.api_calls(), [], "A 为 B 的单打了上游")

        # ② 跑 B 那一轮：它自己把这条回调办掉
        second = self.consume(sibling)
        self.assertEqual(self.order_status_of(order_b), "PENDING_REVIEW")
        self.assertEqual(second.applied, 1)

    def test_a_legacy_notification_that_is_not_ours_only_leaves_a_note(self):
        """⭐ 老地址送来一条**不属于本租户**的单 ⇒ 只留台账、⛔ **不进错误列**。（老地址收的是**整台鸡蛋铺系统**的回调 ⇒「不是我们的单」是**常态**，不是故障。）
        ⛔ 弄红它：把老地址那一档也 `findings.append(reason)`（照新路径那一档）⇒跑一轮调度之后 `last_error` 里就有它 ⇒ 第二条断言红（页面上常年挂一条不是错的错）。"""
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY)
        self.make_link(self.binding, order, remote_id=JID_LEGACY)
        self.reply_status()
        #: 报文里那个单号**没有**任何链路（本租户里一张单都对不上）
        self.assertEqual(self.post_legacy(order_no=JID_NOT_OURS), "200 OK")

        outcome = self.consume()
        # ① 台账里**有**它（⛔ 不是静默丢），而且措辞与「本绑定名下没有这条链路」**分得开**
        rows = self.notes(kind="member.openapi_legacy_unknown")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "skipped")
        self.assertIn(JID_NOT_OURS, rows.get().reason)
        # ② 本轮**没有任何**要人看的话
        self.assertEqual(outcome.findings, ())
        self.assertEqual(self.upstream.api_calls(), [], "不是我们的单，却去打了上游")
        self.assertEqual(self.order_status_of(order), "IN_PROGRESS")

        # ③ 真出口：跑一轮**调度** ⇒ 错误列（`last_error`）里一个字都没有
        fresh = self.run_a_schedule_round()
        self.assertEqual(
            fresh.last_error, "",
            "「不是我们的单」盖进了错误列 ⇒ 健康列常年说一句假话",
        )

    def test_a_legacy_callback_heals_the_missing_remote_id_too(self):
        """⭐ **自愈在老地址那条路上也成立**（自愈那一节的第 5 条判据）。⚠️ 为什么必须钉：**真机实测的那条报文就是从老地址来的**（`callback.suqing.chat`那个没有码的固定路径，kexing 那台机上唯一在跑的入口）⇒ 只在「有码」那条路上补，真机上等于没补。
        ⛔ 弄红它：把 `_member_landed` 里那次 `_heal_missing_remote_id` 调用去掉 ⇒ 第一条断言红（链路那一格还是空的）；自愈成功后**直接 return**（不往下走 ⑧）⇒ 第二条红。
        ⚠️ **本条证明力的边界**（⛔ 别当它证明了「查范围」）：这里只有一个绑定⇒「`member_all_bindings()`（老地址那一档）还是 `member_group_bindings(binding)`」这一下**分辨不出来**。「老地址按单号在**本租户**里找、落到链路所属那一行」那句判据由 `test_the_route_lands_on_the_binding_that_owns_the_link`（**不同的码**）守着。"""
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        link = self.make_link(self.binding, order, remote_id="")
        self.reply_status()
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK")

        outcome = self.consume()

        # ① 那一格被补上了（重新查库）
        self.assertEqual(
            JidanpuMemberOrderLink.objects.get(pk=link.pk).jidanpu_order_id, JID_LEGACY,
        )
        # ② 照常处理完毕：订单真的变「待验收」+ 审计一行
        self.assertEqual(self.order_status_of(order), "PENDING_REVIEW")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=ORDER_NO_LEGACY, action="order.transition",
            ).count(),
            1,
        )
        # ③ 人话台账一条（老地址那条例外**不改**自愈的处置：办成了 ⇒ `done`、⛔ 不进错误列）
        healed = self.notes(kind=jidanpu_member_openapi.KIND_HEALED)
        self.assertEqual(healed.count(), 1)
        self.assertEqual(healed.get().status, "done")
        self.assertEqual(outcome.findings, ())

    def test_a_legacy_callback_heals_a_siblings_link_and_that_binding_processes_it(self):
        """⭐ 自愈那一节的第 4 条判据（老地址那一档的**查范围**）：按来源单号在**本租户**里找，链路属**哪一行**就补**哪一行**（⛔ 不是「只看跑这一轮的那一行」）。
        ⛔ 弄红它：把 `_heal_missing_remote_id` 里那一格 `member_all_bindings()`（老地址那一档）换成 `member_group_bindings(binding)` ⇒ A 看不见 B 那条链路 ⇒ 一格都不补、落回 `member.openapi_legacy_unknown` ⇒ 第一条断言红。
        ⚠️ 两行**刻意用不同的码**（照 `test_the_route_lands_on_the_binding_that_owns_the_link`的理由）：同码时「按本组找」与「按本租户找」分辨不出来。"""
        self.enable_legacy_route()
        sibling = self.make_binding(code=CODE_SECOND, name="second-heal")
        order_b = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        link = self.make_link(sibling, order_b, remote_id="")
        self.reply_status()
        self.assertEqual(
            self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK",
        )

        # ① 跑 A 那一轮：补的是 **B** 那条链路（链路属谁就补谁），而 A ⛔ 一个字都不写订单
        first = self.consume(self.binding)
        self.assertEqual(
            JidanpuMemberOrderLink.objects.get(pk=link.pk).jidanpu_order_id, JID_LEGACY,
        )
        self.assertEqual(self.order_status_of(order_b), "IN_PROGRESS")
        self.assertEqual(first.findings, ())
        self.assertEqual(self.notes(kind="member.openapi_legacy_unknown").count(), 0)
        # ② 跑 B 那一轮：它自己按**补好的**链路把这一条办完
        second = self.consume(sibling)
        self.assertEqual(self.order_status_of(order_b), "PENDING_REVIEW")
        self.assertEqual(second.applied, 1)

    def test_an_ambiguous_legacy_callback_cannot_fill_another_stores_order(self):
        self.enable_legacy_route()
        owner = self.make_binding(name="other-store")
        owner.jidanpu_base_url = "https://jidanpu-other-store.test.invalid"
        owner.save(update_fields=["jidanpu_base_url"])
        order = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        link = self.make_link(owner, order, remote_id="")
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK")

        self.assertEqual(self.consume(owner).applied, 0)
        self.assertEqual(self.consume(self.binding).applied, 0)
        link.refresh_from_db()
        self.assertEqual(link.jidanpu_order_id, "")

        correct_id = "JDP-SYNTH-OTHER-STORE"
        params = self.status_params(order_no=correct_id)
        params["sourceOrderNo"] = HEAL_SOURCE_ORDER_NO
        self.assertEqual(self.post_callback(params=params, received_at=FIXED_2), "200 OK")
        self.consume(owner)
        link.refresh_from_db()
        self.assertEqual(link.jidanpu_order_id, correct_id)

    def test_a_legacy_callback_cannot_fill_a_replacement_link_in_a_single_current_store(self):
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        retired = self.make_link(self.binding, order, remote_id=JID_LEGACY)
        JidanpuMemberOrderLink.objects.filter(pk=retired.pk).update(retired_at=FIXED_1)
        self.binding.jidanpu_base_url = "https://jidanpu-replacement-store.test.invalid"
        self.binding.save(update_fields=["jidanpu_base_url"])
        replacement = self.make_link(self.binding, order, remote_id="")
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK")

        self.assertEqual(self.consume().applied, 0)
        replacement.refresh_from_db()
        self.assertEqual(replacement.jidanpu_order_id, "")

    def test_a_legacy_callback_leaves_a_withdrawn_links_empty_number_alone(self):
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        link = self.make_link(self.binding, order, remote_id="")
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(withdrawn_at=FIXED_1)
        self.reply_status(NOT_COMPLETED)
        self.assertEqual(self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK")

        self.assertEqual(self.consume().applied, 0)
        link.refresh_from_db()
        self.assertEqual(link.jidanpu_order_id, "")

    def test_a_legacy_blocked_heal_stays_out_of_the_error_column(self):
        """⭐ 边界那一档（老地址：**补不上**）**照旧不进错误列**（真出口 = `last_error`）。⚠️ 这是既有那两档的同一条纪律（老地址收的是**整台鸡蛋铺系统**的回调 ⇒ 「不是我们的单」/ 「号对不上」是常态）：⛔ 不静默（台账照写），但⛔ 不盖进绑定页面那个错误列。
        ⛔ 弄红它：让自愈「补不上」那一支**不看 `legacy`**、无条件 `findings.append(reason)`⇒ 调度那一轮的 `last_error` 里就有它 ⇒ 最后一条断言红（照 `AGENTS.md` §5-G：判据打在**真出口**上，⛔ 不只断言 `outcome.findings`）。"""
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY, source_order_no=HEAL_SOURCE_ORDER_NO)
        self.make_link(self.binding, order, remote_id=JID_NOT_OURS)
        self.reply_status()
        self.assertEqual(
            self.post_legacy(extra={"sourceOrderNo": HEAL_SOURCE_ORDER_NO}), "200 OK",
        )

        outcome = self.consume()

        # ① 已有的号一个字都不改
        self.assertEqual(
            JidanpuMemberOrderLink.objects.get(order=order).jidanpu_order_id, JID_NOT_OURS,
        )
        # ② 有一条痕（⛔ 不静默）、措辞是「补不上」那一档、状态是 `skipped`（不是 `failed`）
        blocked = self.notes(kind=jidanpu_member_openapi.KIND_HEAL_BLOCKED)
        self.assertEqual(blocked.count(), 1)
        self.assertEqual(blocked.get().status, "skipped")
        # ③ 本轮没有任何要人看的话 + 真出口（调度盖上去的那一列）里一个字都没有
        self.assertEqual(outcome.findings, ())
        fresh = self.run_a_schedule_round()
        self.assertEqual(
            fresh.last_error, "",
            "老地址那档「补不上」盖进了错误列 ⇒ 健康列常年说一句假话",
        )

    def test_a_legacy_action_30_callback_also_stores_the_picture(self):
        """⭐ 老地址上的 `action=30`（新截图）**也**入库（业务方要的就是「老地址喂给他」）。⚠️ 这一条钉的是「老地址那条例外**不分 action**」：状态与 `action=30` 都办，`action=40/50` 那些账单类照旧只留痕（见 `UnmatchedTests` 那一条）。
        ⛔ 弄红它：让 `_member_other_action` 只处理**新路径**（`if legacy: return None`）⇒ 这一条落到老口径 `member.openapi_other_action` ⇒ 图一行都没有 ⇒ 第一条断言红。"""
        self.enable_legacy_route()
        order = self.make_order(order_no=ORDER_NO_LEGACY)
        self.make_link(self.binding, order, remote_id=JID_LEGACY)
        self.assertEqual(
            self.post_legacy(
                action="30",
                extra={"imgUrl": f"https://oss-direct.invalid/{SHOT_OBJECT}.jpg",
                       "name": "对端上传者甲", "note": "订单截图"},
            ),
            "200 OK",
        )

        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic", side_effect=lambda _src: JPEG_BODY,
        ):
            self.consume()

        rows = list(OrderScreenshot.objects.filter(order=order))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(rows[0].status, OrderScreenshot.STATUS_READY)
        self.assertEqual(self.notes(kind="member.openapi_other_action").count(), 0)
        # 订单状态**一个字都没动**（图片事件不改状态）
        self.assertEqual(self.order_status_of(order), "IN_PROGRESS")


def callback_env_var() -> str:
    """老回调线密钥文件里那个变量名（⭐ 唯一出处 = `apps/callback/config.py`）。"""
    from apps.callback import config as callback_config

    return callback_config.SECRET_VAR_NAME


# ══ ⑨ ⭐ 截图同步 A（快路：`action=30` 带 `imgUrl` ⇒ 直接入库）══════════════════════

#: 合成图片字节（JPEG 魔数；`sniff_image_type` 认它）。
JPEG_BODY = b"\xff\xd8\xff" + b"\0" * 8
#: ⭐ 同一个**对象名**（两种域名指向的是**同一个对象** —— 这是判重的判据所在）。
SHOT_OBJECT = "9000-0000000000000000000-0000000001.png"
#: 那张图的**两个域名**（形状照真机：OSS 直连 vs `oss.jidanpu.com`）+ 各自的查询串
#: （签名 / 变换参数都在查询串里 ⇒ 判重**不许**看它）。
SHOT_URL_DIRECT = f"https://tt-synth.oss-cn-shenzhen.aliyuncs.com/{SHOT_OBJECT}?x-oss-process=style/geshi"
SHOT_URL_JIDANPU = f"https://oss.jidanpu.com/{SHOT_OBJECT}?accessid=CANARY-ACCESSID&signature=CANARY-SIG"
#: 第二张图（只出现在官方 API 那一发里 ⇒ 用来分辨「入库了几张」）。
SHOT_OBJECT_B = "9000-0000000000000000000-0000000002.png"
SHOT_URL_B = f"https://oss.jidanpu.com/{SHOT_OBJECT_B}"


class CallbackScreenshotTests(_MemberOpenApiCase):
    """`action=30` 那条快路：报文里带着直链 ⇒ **不另打上游**直接入库、按**对象 key** 去重。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)

    def post_shot(self, *, url=SHOT_URL_DIRECT, note="订单截图", received_at=FIXED_1,
                  object_name="对端上传者甲", order_no=JID, source_order_no=ORDER_NO):
        """真接收器打一条 `action=30`（订单有新截图）的回调。
        ⚠️ 两个单号可换（缺省就是本类 setUp 那一对）：自愈那条用例要打**另一张单**的截图回调
        （它的链路那一格是空的）⇒ ⛔ 不为了那一档另抄一份报文拼装（形状只此一处）。"""
        return self.post_callback(params={
            "action": "30",
            "orderNo": order_no,
            "sourceOrderNo": source_order_no,
            "note": note,
            "imgUrl": url,
            "name": object_name,
            "timestamp": "1791500000",
        }, received_at=received_at)

    def download_stub(self):
        return mock.patch.object(
            jidanpu_upstream_pics, "download_pic", side_effect=lambda _src: JPEG_BODY,
        )

    def rows(self):
        return list(OrderScreenshot.objects.filter(order=self.order))

    def test_an_action_30_callback_stores_the_picture_from_the_report(self):
        """⭐ 主判据：`action=30` + `imgUrl` ⇒ 跑一轮 ⇒ **那张图真的入库了**。⛔ 弄红它：把 `MEMBER_LINE` 的 `other_action` 钩子摘掉 ⇒ 这一条落到老口径（`member.openapi_other_action`，只留痕）⇒ 图一行都没有 ⇒ 第一条断言红。
        ⚠️ 三条一起钉：① 图真的落库（行 + 对象存储里真有字节）；② **⛔ 一次上游都没打**（快路不许为了这张图去调官方 API）；③ 图的归属与图种按既有口径。"""
        self.assertEqual(self.post_shot(), "200 OK")

        with self.download_stub():
            outcome = self.consume()

        rows = self.rows()
        self.assertEqual(len(rows), 1, [n.reason for n in self.notes()])
        self.assertEqual(rows[0].img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(rows[0].status, OrderScreenshot.STATUS_READY)
        self.assertEqual(rows[0].uploaded_by_id, self.binding.user_id)
        self.assertTrue(rows[0].object_key.startswith(f"shared-orders/{ORDER_NO}/"))
        with get_storage().open_stream(rows[0].object_key) as handle:
            self.assertEqual(handle.read(), JPEG_BODY)
        # ② 快路**不打上游**：一次 token / 一次只读端点都没有
        self.assertEqual(self.upstream.requests, [], self.upstream.requests)
        self.assertEqual(outcome.applied, 1)
        # ③ 老口径那条「不是 action=10 ⇒ 只登记」的痕**不在这里**（这一条被本线办掉了）
        self.assertEqual(self.notes(kind="member.openapi_other_action").count(), 0)

    def test_withdrawal_during_download_prevents_picture_storage(self):
        self.assertEqual(self.post_shot(), "200 OK")
        link = JidanpuMemberOrderLink.objects.get(order=self.order)
        storage = mock.Mock(wraps=get_storage())

        def download_then_withdraw(_url):
            JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(withdrawn_at=FIXED_1)
            return JPEG_BODY

        with (
            mock.patch.object(jidanpu_upstream_pics, "get_storage", return_value=storage),
            mock.patch.object(jidanpu_upstream_pics, "download_pic", side_effect=download_then_withdraw) as download,
        ):
            outcome = self.consume()

        download.assert_called_once()
        storage.put_bytes.assert_not_called()
        self.assertEqual(self.rows(), [])
        self.assertEqual(outcome.applied, 0)

    def test_a_busy_copy_is_retried_before_the_picture_callback_advances(self):
        self.assertEqual(self.post_shot(), "200 OK")
        separate = connection.copy(alias="test_member_callback_picture_lock")
        try:
            with separate.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    [jidanpu_assignment_lock._key("order", self.order.pk)],
                )
                self.assertTrue(cursor.fetchone()[0])
            rounds = jidanpu_openapi_consume.CALLBACK_MAX_ATTEMPTS + 1
            with self.download_stub() as download:
                for _ in range(rounds):
                    first = self.consume()
                    self.assertEqual(first.applied, 0)
                    self.assertEqual(first.failed, 0)
                    self.assertIsNone(first.watermark)
            self.assertEqual(download.call_count, rounds)
            self.assertEqual(self.rows(), [])
            self.assertFalse(self.notes(kind=jidanpu_openapi_consume.KIND_CALLBACK_RETRY).exists())
        finally:
            separate.close()

        with self.download_stub():
            second = self.consume()
        self.assertEqual(second.applied, 1)
        self.assertEqual(second.watermark, FIXED_1)
        self.assertEqual(len(self.rows()), 1)

    def _assert_withdraw_and_restore_rejects_picture(self, *, status, evidence):
        self.assertEqual(self.post_shot(), "200 OK")
        link = JidanpuMemberOrderLink.objects.get(order=self.order)

        def download_then_withdraw_and_restore(_url):
            JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(withdrawn_at=FIXED_2)
            JidanpuSyncEvent.objects.create(
                direction="out", kind=jidanpu_member.KIND_WITHDRAW, status=status,
                target_type="order", target_ref=self.order.order_no,
                payload={"member_binding_id": self.binding.pk, **evidence}, processed_at=FIXED_2,
            )
            JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(withdrawn_at=None, last_out_at=FIXED_3)
            return JPEG_BODY

        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic", side_effect=download_then_withdraw_and_restore,
        ):
            self.assertEqual(self.consume().applied, 0)
        self.assertEqual(self.rows(), [])

    def test_a_confirmed_withdrawal_and_restore_do_not_revive_an_old_picture(self):
        self._assert_withdraw_and_restore_rejects_picture(status="done", evidence={})

    def test_a_confirmed_absence_and_restore_do_not_revive_an_old_picture(self):
        self._assert_withdraw_and_restore_rejects_picture(
            status="skipped", evidence={jidanpu_relaunch.WITHDRAW_ABSENCE_CONFIRMED_KEY: True},
        )

    def test_an_ordinary_copy_update_does_not_block_a_picture(self):
        self.assertEqual(self.post_shot(), "200 OK")
        link = JidanpuMemberOrderLink.objects.get(order=self.order)

        def download_then_unlist(_url):
            JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(in_hall=False, last_out_at=FIXED_2)
            JidanpuSyncEvent.objects.create(
                direction="out", kind=jidanpu_member.KIND_WITHDRAW, status="skipped",
                target_type="order", target_ref=self.order.order_no,
                payload={"member_binding_id": self.binding.pk}, processed_at=FIXED_2,
            )
            return JPEG_BODY

        with mock.patch.object(jidanpu_upstream_pics, "download_pic", side_effect=download_then_unlist):
            self.assertEqual(self.consume().applied, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_an_action_30_group_qr_is_not_stored(self):
        """⭐ 2026-10-25 计划 Rev.2：快回调**只收订单截图**；群码（本线自己推上去的那类）
        不再入库，且⛔ 不下载（回环断点）。"""
        self.assertEqual(self.post_shot(note="群二维码"), "200 OK")

        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic",
            side_effect=AssertionError("群码不该被下载"),
        ):
            outcome = self.consume()

        self.assertEqual(self.rows(), [])
        self.assertEqual(outcome.applied, 1)

    def test_an_action_30_for_our_own_pushed_object_is_not_stored(self):
        """⭐⭐ 快回调也按**历史推送台账**判重：报文里的对象名是我们推上去的那一张 ⇒ 不入库。"""
        JidanpuSyncEvent.objects.create(
            direction="in", kind="member.assign", status="done",
            target_type="order", target_ref=ORDER_NO,
            reason="订单截图已传到下家（共 1 张：订单截图 1 张）",
            payload={
                "member_binding_id": self.binding.pk,
                "pushed_pic_keys": {
                    f"shared-orders/{ORDER_NO}/synth-pushed.jpg": SHOT_OBJECT,
                },
            },
        )
        self.assertEqual(self.post_shot(url=SHOT_URL_DIRECT), "200 OK")

        with mock.patch.object(
            jidanpu_upstream_pics, "download_pic",
            side_effect=AssertionError("我们推过的图不该被下载"),
        ):
            outcome = self.consume()

        self.assertEqual(self.rows(), [])
        self.assertEqual(outcome.applied, 1)

    def test_the_same_delivery_twice_stores_the_picture_once(self):
        """⭐ 幂等：同一条（重复送达实测 0.5%）再来一次 ⇒ **不重复入库**。
        ⛔ 弄红它：把 `store_remote_pics` 里「`object_key` 已存在 ⇒ `existing`」那一支去掉
        ⇒ 第二次会撞 `object_key` 唯一约束（或插出第二行）⇒ 第一条断言红。"""
        self.assertEqual(self.post_shot(received_at=FIXED_1), "200 OK")
        self.assertEqual(self.post_shot(received_at=FIXED_2), "200 OK")

        with self.download_stub():
            self.consume()

        self.assertEqual(len(self.rows()), 1, "同一张图被存了两次")
        self.assertEqual(self.order.screenshots.count(), 1)

    def test_an_action_30_callback_heals_the_missing_remote_id_and_stores_the_picture(self):
        """⭐⭐ **C 补丁（2026-10-10）**：缺系统单号那段时间里的**新截图回调**也要能补号 + 照常入库。**缺口**：自愈（`_heal_missing_remote_id`）原来只接在**状态那条路**（`_member_landed`）上，而 `action=30` 走的是**另一支**（`_member_other_action`）⇒ 那段时间里对方推来的新截图**会被丢掉**（只留一条「找不到链路」的痕，图永远不入库）。
        **补法**：同一支里**再调一次同一个 helper**（⛔ 不抄第二份逻辑），补上就照常往下走。⛔ 弄红它（两条各自能红）：① 把 `_member_other_action` 里那次 `_heal_missing_remote_id` 调用去掉⇒ 链路那一格还是空的（第一条断言红），且图一行都没有；
        ② 自愈成功后**直接 return**（不往下走存图）⇒ 图仍然零行 ⇒ 第二条断言红。⚠️ 四条一起钉：① **数据库行**（链路那一格真的补上了）；② ⭐ **截图真的入库**（行 + 对象存储里真有字节，⛔ 不是「补完就 return」）；③ 一条 `done` 的人话台账；④ ⛔ **没有**「找不到链路」那一档的痕（图没被丢掉）、不进错误列。"""
        order_b = self.make_order(order_no=ORDER_NO_B)
        link = self.make_link(self.binding, order_b, remote_id="")
        self.assertEqual(
            self.post_shot(order_no=JID_B, source_order_no=ORDER_NO_B), "200 OK",
        )

        with self.download_stub():
            outcome = self.consume()

        # ① 链路那一格**真的补上了**（重新查库，⛔ 不读内存对象）
        self.assertEqual(
            JidanpuMemberOrderLink.objects.get(pk=link.pk).jidanpu_order_id, JID_B,
        )
        # ② ⭐ 截图**照常入库**（⛔ 不是「补完就 return」）
        rows = list(OrderScreenshot.objects.filter(order=order_b))
        self.assertEqual(len(rows), 1, [n.reason for n in self.notes()])
        self.assertEqual(rows[0].status, OrderScreenshot.STATUS_READY)
        self.assertTrue(rows[0].object_key.startswith(f"shared-orders/{ORDER_NO_B}/"))
        with get_storage().open_stream(rows[0].object_key) as handle:
            self.assertEqual(handle.read(), JPEG_BODY)
        # ③ 人话台账一条（`done`：**办成了**的事，⛔ 不进错误列）
        healed = self.notes(kind=jidanpu_member_openapi.KIND_HEALED)
        self.assertEqual(healed.count(), 1)
        self.assertEqual(healed.get().status, "done")
        self.assertIn(JID_B, healed.get().reason)
        # ④ ⛔ 图**没有**被当成「找不到链路的单」丢掉：
        self.assertEqual(
            self.notes(kind=jidanpu_member_openapi.KIND_UNKNOWN_ORDER).count(), 0,
        )
        self.assertEqual(outcome.findings, ())
        self.assertEqual(outcome.applied, 1)

    def test_a_callback_without_a_usable_img_url_only_leaves_a_note(self):
        """`action=30` 却没有可用的直链 ⇒ 留一条痕**并进错误列**（⛔ 不静默、⛔ 不建空行）。⛔ 弄红它：把 `if not img_url:` 那一支去掉 ⇒ 会拿着空 URL 去下载（夹具的下载替身照旧回字节）⇒ 反而存出一行来 ⇒ 第一条断言红。
        ⚠️ 为什么「没有直链」要进错误列（与「下载失败」**分得开**）：那是**配不上**的一档—— 对端明明说有新截图、报文里却没有地址 ⇒ 截图会**静默停住**（业务方报的就是那个形态）；而「下载失败」是网络抖一下，照①式那条口径只留台账（见 `OpenApiProcessImgsTests`）。"""
        self.assertEqual(self.post_shot(url=""), "200 OK")

        with self.download_stub():
            outcome = self.consume()

        self.assertEqual(self.rows(), [])
        rows = self.notes(kind="member.openapi_unreadable")
        self.assertEqual(rows.count(), 1)
        self.assertIn("imgUrl", rows.get().reason)
        self.assertEqual(outcome.findings, (rows.get().reason,))


# ══ ⑩ ⭐ 截图同步 B（兜底：官方 API「取订单进度图列表」补拉一次**全量**）══════════════


class OpenApiProcessImgsTests(_MemberOpenApiCase):
    """②式写「待验收」那一步：用官方 API 拉一次**全量**进度图（**替代**被跳过的 `ViewPic`）。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)

    def imgs_calls(self):
        return [row for row in self.upstream.api_calls() if row["path"] == ORDER_PROCESS_IMGS_PATH]

    def store_stub(self, download=None):
        return mock.patch.object(
            jidanpu_upstream_pics, "download_pic",
            side_effect=download or (lambda _src: JPEG_BODY),
        )

    def rows(self):
        return list(OrderScreenshot.objects.filter(order=self.order))

    def test_the_completion_step_pulls_the_full_progress_images_exactly_once(self):
        """⭐ 主判据：②式 + 远端「已完成」 ⇒ 跑一轮 ⇒ **真的调了**那个接口、且**只调一次**、且拉回来的图**入库**。
        ⛔ 弄红它：把 `_apply_member_status` 里那个 `openapi_pics=queries` 去掉 ⇒一次都不打 `GetOrderProcessImgs` ⇒ 第一条（调用次数）与第二条（图行）同时红。⚠️ 三条承重：① **恰好一发**（钉「不是每轮重复拉」：同一条单的多条通知只该拉一次）；
        ② **参数是链路那一列的远端单号**（⛔ 不是本行的 `sourceOrderNo`）；③ **与查权威状态共用同一次取 token**（那一发只该有一次）—— ⛔ 不为拉图再取一次 token 是这条实现的承重口径（`_client_of`）。"""
        self.reply_status()
        self.reply_process_imgs([
            {"url": SHOT_URL_JIDANPU, "uploadTime": "2026-10-09 11:00:00",
             "name": "对端上传者甲", "note": "订单截图"},
            {"url": SHOT_URL_B, "uploadTime": "2026-10-09 11:30:00",
             "name": "对端上传者乙", "note": "群二维码"},
        ])
        self.assertEqual(self.post_callback(), "200 OK")

        with self.store_stub():
            outcome = self.consume()

        # ① 只调一次（且真的是那个只读端点）
        calls = self.imgs_calls()
        self.assertEqual(len(calls), 1, self.upstream.api_calls())
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["params"], {"orderId": JID})
        # ③ 取 token 只发生一次（拉图复用查状态那一个客户端）
        self.assertEqual(len(self.upstream.token_calls()), 1)
        # ② 两张图里**只收订单截图**（计划 Rev.2：群码是本线自己推给下家的，
        #    拉回侧按图种过滤掉，⛔ 不再入库 —— 回环断点）。
        rows = self.rows()
        self.assertEqual([row.img_type for row in rows], [OrderScreenshot.IMG_ORDER])
        self.assertEqual([row.status for row in rows], ["ready"])
        # 状态照写（图是附属动作）
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")
        self.assertEqual(outcome.applied, 1)

    def test_the_reconcile_round_also_pulls_them(self):
        """兜底对账那一轮也走**同一段**（②式那两条入口都必须拿到它，⛔ 不许只有回调那一条）。"""
        self.reply_status()
        self.reply_process_imgs([
            {"url": SHOT_URL_JIDANPU, "uploadTime": "2026-10-09 11:00:00",
             "name": "对端上传者甲", "note": "订单截图"},
        ])

        with self.store_stub():
            self.reconcile()

        self.assertEqual(len(self.imgs_calls()), 1, self.upstream.api_calls())
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")

    def test_a_failed_pull_does_not_block_pending_review(self):
        """⭐ 口径③：拉图**任何失败都不挡「待验收」**，只记一条台账人话。⛔ 弄红它：把 `_pull_openapi_screenshots` 的失败也冒出去（不接那个 `except`）⇒
        ①式状态写不进去（`apply_remote_completion` 抛）⇒ 第一条断言红。⚠️ 同时钉「⛔ 不进错误列」：`findings` 是空的（照①式那条同源口径）。"""
        self.reply_status()
        self.upstream.boom(ORDER_PROCESS_IMGS_PATH, httpx.ConnectError("合成：连不上"))
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume()

        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")
        self.assertEqual(self.rows(), [])
        failed = self.notes(kind="member.status_in").filter(
            target_ref=ORDER_NO, status="failed",
        )
        self.assertEqual(failed.count(), 1)
        self.assertIn("没拉回来", failed.get().reason)
        # ⛔ 不进错误列（`last_error` 由调度那一层按 `findings` 盖）
        self.assertEqual(outcome.findings, ())

    def test_a_denied_write_does_not_pull_the_images_twice_in_one_round(self):
        """⭐ 「只调一次」的**真形态**：一单的多条通知在同一轮里 ⇒ 全量图只拉**一发**。
        ⚠️ 为什么这一条非有不可（我实测过）：写成「一条回调 ⇒ 一次转换 ⇒ 一发图」那种用例**抓不到** `_imgs` 那道缓存被删掉——那种情况下转换只发生一次，缓存有没有都是 1。真正到得了第二发的形态是**写被拒**：状态机 / 节点门拒绝 ⇒ 订单**没**进「待验收」⇒ 同一条单的下一条通知**会再走一遍**「判 → 写」那一段（这一轮里）。
        ⛔ 弄红它：把 `_StatusQueries._imgs` 那道缓存去掉（`process_imgs_of` 不查/不写它）⇒ 第二条通知又拉一次全量图 ⇒ 第一条断言（调用次数 = 1）红。⚠️ 顺带钉住「写被拒**不挡**拉图」：那两条通知各留一条 `failed` 台账（状态机拒绝那一条），图仍然入库（图是附属动作）。"""
        from apps.business.order_status import IllegalTransition

        self.reply_status()
        self.reply_process_imgs([
            {"url": SHOT_URL_JIDANPU, "uploadTime": "2026-10-09 11:00:00",
             "name": "对端上传者甲", "note": "订单截图"},
        ])
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")

        with mock.patch.object(
            jidanpu_member, "apply_transition",
            side_effect=IllegalTransition("合成：这个绑定账号缺节点"),
        ), self.store_stub():
            outcome = self.consume()

        # ① 全量图**只拉了一发**（缓存那道闸）
        self.assertEqual(len(self.imgs_calls()), 1, self.upstream.api_calls())
        # ② 写被拒 ⇒ 订单没动，但图照旧入库（附属动作不跟着回滚）
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        self.assertEqual(len(self.rows()), 1)
        # ③ 两条通知各自留了一条「状态机拒绝」的痕（⛔ 不是静默）
        denied = self.notes(kind="member.status_in").filter(
            target_ref=ORDER_NO, status="failed",
        )
        self.assertEqual(denied.count(), 1)
        self.assertIn("缺节点", denied.get().reason)
        self.assertEqual(outcome.applied, 0)

    def test_the_two_domains_of_one_object_are_stored_once(self):
        """⭐⭐ **判重按对象 key**：同一张图在「回调给的 OSS 直连」与「查询接口给的`oss.jidanpu.com`」两个域名上 ⇒ **只存一行**。
        ⛔ 弄红它：把 `_pull_openapi_screenshots` / `_store_callback_picture` 里的`identity=remote_object_key` 换成默认的 `stable_pic_identity`（域名 + 路径）⇒ 这两个域名算出两个不同的键 ⇒ 同一个对象存两行 ⇒ 第一条断言红。
        ⚠️ 两发查询串**刻意不同**（真机那两种直链都带签名 / 变换参数）⇒ 按整条 URL 判重也一定会红。"""
        # ① 回调（快路）先存下那张图：走 **OSS 直连**域名
        self.assertEqual(
            self.post_callback(params={
                "action": "30", "orderNo": JID, "sourceOrderNo": ORDER_NO,
                "note": "订单截图", "imgUrl": SHOT_URL_DIRECT,
                "name": "对端上传者甲", "timestamp": "1791500000",
            }),
            "200 OK",
        )
        with self.store_stub():
            self.consume()
        first = self.rows()
        self.assertEqual(len(first), 1)
        key_from_callback = first[0].object_key

        # ② 再来一条状态回调 ⇒ 到达「待验收」那一步 ⇒ 官方 API 把**同一个对象**（另一个域名）
        #    连同另外一张新图一起给回来
        self.reply_status()
        self.reply_process_imgs([
            {"url": SHOT_URL_JIDANPU, "uploadTime": "2026-10-09 11:00:00",
             "name": "对端上传者甲", "note": "订单截图"},
            {"url": SHOT_URL_B, "uploadTime": "2026-10-09 11:30:00",
             "name": "对端上传者乙", "note": "订单截图"},
        ])
        self.assertEqual(
            self.post_callback(received_at=FIXED_2), "200 OK",
        )
        with self.store_stub():
            self.consume()

        rows = self.rows()
        self.assertEqual(len(rows), 2, [row.object_key for row in rows])
        self.assertEqual(
            sorted(row.object_key for row in rows).count(key_from_callback), 1,
            "同一张图在两种域名下被存了两行（判重看的是域名而不是对象 key）",
        )
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")


# ══ ⑪ ⭐ 自愈：②式「缺系统单号」那一档由回调补上═════════════════════════
# **缺口（真机实测，上一轮如实登记的残差）**：②式录单时若对方**已经有**这张单（按来源单号查得到），官方 API **不给**它的系统订单号（`GetOrderStatus` 回包恰好 5 格、没有 `orderId`）⇒ 认领那一支**落不下链路** ⇒ 那一单在我方**停在待抢单**直到人工处理。**补法**：真机回调报文里**两个号都有** —— `orderNo` = 对方的**系统订单号**、`sourceOrderNo` = 我方的**有效来源单号** ⇒ 回调到达时把链路里缺的那一格补上，且这一条回调**照常继续处理**（该查权威查权威、该写状态写状态）。本节逐条钉死模块「自愈」那一节的四条硬口径：① **只在那一格为空时补**（⛔ 绝不覆盖已有的号）；② **补完照常处理**（⛔ 不是「补完就 return」）；
# ③ **边界各留各的痕**（找不到单 ⇒ 落回既有那一档；多张 / 冲突 ⇒ `member.openapi_heal_blocked`）；④ **查范围与 `_link_of` 同源**（新路径 = 同一个回调码那一组 / 老地址 = 本租户全部绑定）。⚠️ 老地址那一条（第 5 条判据）长在 `LegacyNotifyTests` 里（那一节才有老地址的夹具），它的 docstring 指回本节。

#: 自愈用例用的**有效来源单号**（合成值，形状贴近真值：上游给的第三方单号；⛔ 不是真值）。
HEAL_SOURCE_ORDER_NO = "SYNTH-HEAL-2026-000001"


class SelfHealMissingRemoteIdTests(_MemberOpenApiCase):
    """缺系统单号那一档：回调把链路那一格补上，**并且照常把这一条回调办完**。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        #: 权威值「已完成」⇒ 照常处理那一半一定写得出「待验收」（判据要能分辨「没处理」）。
        self.reply_status()

    # ── 夹具与读数 ──────────────────────────────────────────────────────────

    def post_with_source(self, *, order_no=JID, source_order_no=ORDER_NO) -> str:
        """走**真接收器**打一条 `action=10` 的回调，报文里**两个号都给**（真机形状）。"""
        return self.post_callback(params={
            "orderNo": order_no,
            "sourceOrderNo": source_order_no,
            "status": CALLBACK_STATUS,
            "note": "",
            "action": "10",
            "timestamp": "1791500000",
        })

    def link_row(self, link):
        """**重新查库**读链路那一行（E-0009：⛔ 不读内存对象——那会把「没写」看成本地那次改动）。"""
        return JidanpuMemberOrderLink.objects.get(pk=link.pk)

    def blocked(self):
        return self.notes(kind=jidanpu_member_openapi.KIND_HEAL_BLOCKED)

    def healed(self):
        return self.notes(kind=jidanpu_member_openapi.KIND_HEALED)

    # ── ① 端到端：补上了 **且** 照常处理完毕 ────────────────────────────────

    def test_a_live_same_store_blank_is_healed_amid_withdrawn_and_other_store_links(self):
        other_store = self.make_binding(name="other-store")
        other_store.jidanpu_base_url = "https://jidanpu-other-store.test.invalid"
        other_store.save(update_fields=["jidanpu_base_url"])
        foreign = self.make_link(other_store, self.order, remote_id="JDP-SYNTH-FOREIGN")
        inactive = self.make_binding(name="withdrawn")
        withdrawn = self.make_link(inactive, self.order, remote_id="")
        JidanpuMemberOrderLink.objects.filter(pk=withdrawn.pk).update(withdrawn_at=FIXED_1)
        live = self.make_link(self.binding, self.order, remote_id="")
        self.assertEqual(self.post_with_source(), "200 OK")

        self.assertEqual(self.consume().applied, 1)
        self.assertEqual(self.link_row(live).jidanpu_order_id, JID)
        self.assertEqual(self.link_row(withdrawn).jidanpu_order_id, "")
        self.assertEqual(self.link_row(foreign).jidanpu_order_id, "JDP-SYNTH-FOREIGN")

    def test_a_busy_missing_id_is_healed_after_more_than_five_busy_rounds(self):
        link = self.make_link(self.binding, self.order, remote_id="")
        self.assertEqual(self.post_with_source(), "200 OK")
        separate = connection.copy(alias="test_member_callback_heal_lock")
        try:
            with separate.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s)",
                    [jidanpu_assignment_lock._key("order", self.order.pk)],
                )
                self.assertTrue(cursor.fetchone()[0])
            for _ in range(jidanpu_openapi_consume.CALLBACK_MAX_ATTEMPTS + 1):
                outcome = self.consume()
                self.assertEqual(outcome.failed, 0)
                self.assertIsNone(outcome.watermark)
            self.assertEqual(self.link_row(link).jidanpu_order_id, "")
        finally:
            separate.close()

        self.assertEqual(self.consume().applied, 1)
        self.assertEqual(self.link_row(link).jidanpu_order_id, JID)

    def test_a_callback_heals_the_missing_remote_id_and_is_still_processed(self):
        """⭐ **主判据**：空的那一格被补上 **且** 这一条回调照常办完（订单真的变「待验收」）。⛔ 弄红它（两条各自能红）：
        ① 把 `_member_landed` 里那次 `_heal_missing_remote_id` 调用去掉 ⇒ 第一条断言红（链路那一格还是空的）；② 自愈成功后**直接 return**（不往下走 ⑧）⇒ 订单还是「代练中」、审计零行 ⇒ 第二、三条红。
        ⚠️ 五条断言各自承重：① **数据库行**（链路那一格）；② 订单状态真的写了；③ 打出去的权威那一次用的是**补进来的那个号**（补号与处理是同一条链路）；④ 一条 `done` 的人话台账（⛔ 不进错误列）；⑤ 水位照推。"""
        link = self.make_link(self.binding, self.order, remote_id="")
        self.assertEqual(self.post_with_source(), "200 OK")

        outcome = self.consume()

        # ① ⭐ 链路那一格**真的补上了**（重新查库）
        self.assertEqual(self.link_row(link).jidanpu_order_id, JID)
        # ② ⭐ 这一条回调**照常处理完毕**（⛔ 不是「补完就 return」）
        self.assertEqual(self.order_status(self.order), "PENDING_REVIEW")
        self.assertEqual(outcome.applied, 1)
        audit = OperationLog.objects.filter(order_no=ORDER_NO, action="order.transition")
        self.assertEqual(audit.count(), 1)
        self.assertEqual((audit.get().from_value, audit.get().to_value), ("代练中", "待验收"))
        # ③ 权威那一次打的是**补进来的那个号**（＋B 段那一次全量进度图，与①节逐字同形）
        self.assertEqual(
            sorted(call["path"] for call in self.upstream.api_calls()),
            sorted([ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH]),
        )
        for call in self.upstream.api_calls():
            with self.subTest(path=call["path"]):
                self.assertEqual(call["params"], {"orderId": JID})
        # ④ 人话台账一条（`done`：**办成了**的事，⛔ 不进错误列）
        self.assertEqual(self.healed().count(), 1)
        note = self.healed().get()
        self.assertEqual(note.status, "done")
        self.assertEqual(note.target_ref, ORDER_NO)
        self.assertEqual(note.payload["member_binding_id"], self.binding.pk)
        self.assertIn(JID, note.reason)
        #: ⚠️ 本条走的是**回落形态**：我方来源单号为空 ⇒ 有效来源单号 = 内置单号（`ORDER_NO`）
        #: ⇒ 报文里的 `sourceOrderNo` 就是它（另一形态由老地址那条用例钉着：显式来源单号）。
        self.assertIn(ORDER_NO, note.reason)
        self.assertEqual(outcome.findings, ())
        # ⑤ 水位照推（这一条**处理过了**）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)
        self.assertEqual(self.refresh().callback_count, 1)

    # ── ② 不覆盖：那一格已有别的号 ⇒ ⛔ 一个字都不改 ─────────────────────────

    def test_an_existing_remote_id_is_never_overwritten(self):
        """⭐ **不覆盖**：链路已有号、回调给的号**不同** ⇒ ⛔ 不许改（一条痕 + 进错误列）。⛔ 弄红它：把 `_heal_missing_remote_id` 里那一段冲突判据去掉（「空才算空」改成「有链路就写」）⇒ 第一条断言当场红（`JID_B` 被改成了 `JID`）。
        ⚠️ 还钉住一条承重的：号对不上时⛔ **不拿 `JID` 的权威状态去写这张单**（那会把**别人那张远端单**的状态写到这张单上）⇒ 一次上游读都没有、订单一个字没动。"""
        link = self.make_link(self.binding, self.order, remote_id=JID_B)
        self.assertEqual(self.post_with_source(), "200 OK")

        outcome = self.consume()

        # ① ⛔ 已有的号一个字节都不许动（重新查库）
        self.assertEqual(self.link_row(link).jidanpu_order_id, JID_B)
        # ② 留了一条「补不上」的痕（要人看 ⇒ 进错误列）
        self.assertEqual(self.blocked().count(), 1)
        note = self.blocked().get()
        self.assertEqual(note.status, "failed")
        self.assertEqual(note.target_ref, ORDER_NO)
        self.assertIn(JID_B, note.reason)
        self.assertEqual(outcome.findings, (note.reason,))
        self.assertEqual(self.healed().count(), 0)
        # ③ ⛔ 不拿别人的号去写这张单：一次上游读都没有、订单一个字都没动
        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        self.assertEqual(
            OperationLog.objects.filter(order_no=ORDER_NO, action="order.transition").count(), 0,
        )
        # ④ 水位照推（这一条有结论：**要人看**那一档）
        self.assertEqual(self.refresh().callback_watermark_at, FIXED_1)

    # ── ③ 找不到单 ⇒ 落回**既有**那一档（⛔ 不新造第五档措辞）─────────────────

    def test_a_source_order_no_that_matches_no_order_uses_the_existing_tier(self):
        """⭐ **找不到单** ⇒ 照既有「找不到链路」那一档留痕（新路径：`failed` + 进 `findings`）。
        ⛔ 弄红它：让自愈在「找不到单」时**自己**写一条 `member.openapi_heal_blocked`
        （或干脆静默返回）⇒ 第二条断言红（既有的 `member.openapi_unknown_order` 那一行没了）。"""
        link = self.make_link(self.binding, self.order, remote_id="")
        #: 报文里的来源单号**对不上任何**我方单（本租户里那张单的有效来源单号是 `ORDER_NO`）
        self.assertEqual(
            self.post_with_source(source_order_no=HEAL_SOURCE_ORDER_NO), "200 OK",
        )

        outcome = self.consume()

        # ① 自愈**没有**凭空补任何东西
        self.assertEqual(self.link_row(link).jidanpu_order_id, "")
        self.assertEqual(self.healed().count(), 0)
        self.assertEqual(self.blocked().count(), 0)
        # ② 既有那一档照旧（措辞里**两个号都有**，人要能照着查）
        rows = self.notes(kind="member.openapi_unknown_order")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "failed")
        self.assertIn(JID, rows.get().reason)
        self.assertIn(HEAL_SOURCE_ORDER_NO, rows.get().reason)
        self.assertEqual(outcome.findings, (rows.get().reason,))
        # ③ 一次上游读都没有、订单一个字没动
        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")

    # ── ④ 多张单 ⇒ 认不出补哪一张（⛔ 不猜）─────────────────────────────────

    def test_two_orders_with_the_same_source_order_no_block_the_heal(self):
        """⭐ **找到多张** ⇒ 一格都不补 + 一条要人看的痕（⛔ 不落回「找不到链路」那一档措辞）。⛔ 弄红它：把「多张」那一支去掉（照 `matched[0]` 补第一张）⇒ 第一条断言红（补错一格 = 把两张单的链路接错，本仓最贵的形态之一）。
        ⚠️ 与日志快路那一档（`jidanpu_member.DUPLICATE_SOURCE_ORDER_NO_REASON`）是**同一类事实**—— 那边按链路 id 认最早一条，这边**连补都不补**。"""
        first = self.make_order(order_no=ORDER_NO_B, source_order_no=HEAL_SOURCE_ORDER_NO)
        second = self.make_order(order_no=ORDER_NO_C, source_order_no=HEAL_SOURCE_ORDER_NO)
        link = self.make_link(self.binding, first, remote_id="")
        self.assertEqual(
            self.post_with_source(source_order_no=HEAL_SOURCE_ORDER_NO), "200 OK",
        )

        outcome = self.consume()

        # ① ⛔ 一格都不补
        self.assertEqual(self.link_row(link).jidanpu_order_id, "")
        self.assertEqual(self.healed().count(), 0)
        # ② 一条「补不上」的痕：带上**张数**（人要能照着查来源单号）
        self.assertEqual(self.blocked().count(), 1)
        note = self.blocked().get()
        self.assertEqual(note.status, "failed")
        self.assertIn("我方 2 张单", note.reason)
        self.assertIn(HEAL_SOURCE_ORDER_NO, note.reason)
        self.assertEqual(outcome.findings, (note.reason,))
        # ③ ⛔ 不落回「找不到链路」那一档（各自留各自的痕：那一句在这里是**半句假话**）
        self.assertEqual(self.notes(kind="member.openapi_unknown_order").count(), 0)
        # ④ 一次上游读都没有、两张单一个字都没动
        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.order_status(first), "IN_PROGRESS")
        self.assertEqual(self.order_status(second), "IN_PROGRESS")


# ══ ⑬ 增量读：下家绑定也把「已消费到的字节偏移」落库（生产性能缺陷）════════


class IncrementalOffsetTests(_MemberOpenApiCase):
    """⭐ kexing 现网那条就是**下家②式**绑定 ⇒ 偏移必须落在 `JidanpuMemberBinding` 上、
    跨轮生效（引擎是两条线共用的一份，见 `jidanpu_openapi_consume.consume_callbacks`）。
    """

    def test_the_member_binding_persists_the_consumed_byte_offset(self):
        """⭐ 第一轮落库、第二轮只读新增那一行（⛔ 不重读 198MB 历史）。
        ⛔ 弄红它：① 只把偏移放在进程内记忆 ⇒ 第二轮 `scanned=2`；② 不写这张表的列
        （引擎给下家线写偏移时 AttributeError / 字段不存在）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")

        first = self.consume()

        self.assertEqual(first.scanned, 1, "首轮小文件应当整份读（默认行为不变）")
        self.assertEqual(
            self.refresh().callback_file_offset, self.events.stat().st_size,
            "下家绑定的已消费偏移没有落库 / 没停在最后一条完整行的末尾",
        )

        second_order = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, second_order, remote_id=JID_B)
        self.assertEqual(
            self.post_callback(params=self.status_params(order_no=JID_B), received_at=FIXED_2),
            "200 OK",
        )
        second = self.consume(now=ROUND_NOW + timedelta(minutes=1))

        self.assertEqual(second.scanned, 1, "第二轮把历史又读了一遍（偏移没有跨轮生效）")
        self.assertEqual(second.mine, 1)
        self.assertEqual(self.order_status(second_order), "PENDING_REVIEW")
        self.assertEqual(self.refresh().callback_file_offset, self.events.stat().st_size)
