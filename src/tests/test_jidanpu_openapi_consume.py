"""消费者（`apps/business/jidanpu_openapi_consume.py`）的判据。spec：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §5.4 / §5.5 / §6 / §13.4 的 **E1 / E2 / E3 / E8**。这份文件守什么：⭐ **端到端**——造一条**真的** JSONL 事件（**由真接收器 `apps/callback/wsgi.application` 写出来**，⛔ 不是手搓一个 dict 形状）+ 一个②式绑定 + 一张我方订单 ⇒ 跑一轮消费者 ⇒ **订单状态真的变了**（断言**数据库行**，⛔ 不打 logger、⛔ 不看返回值）；同一条用例里顺带钉死 **B4**：回调报文里的 `status` **不决定写成什么**（报文说「问题单」、权威说「已暂停」⇒ 最终必须是「已暂停」）；**E1**：只处理**本绑定**那串码的事件（本码 / 别的码 / 前缀相同的更长的码）；**E3**：**写之前一定先查**——查权威失败 ⇒ **一个字节都不写订单**（⛔ 不许拿回调里的值顶上）；**E2**：水位（同一份事件跑两轮只处理一次）+ 幂等（清空水位后会重读、但**不产生第二次写入**）；
**E8**：事件文件变短 / mtime 倒退 ⇒ **水位清零 + 响亮留痕**（⛔ 不静默跳过）；**F19**：回调报文里的 `timestamp` **一个字都不参与**（我们只认自己的 `received_at`）；**未识别状态**（spec §6.4）：⛔ 不改单、⛔ 不猜成问题单、要留一条待人工核对的台账；**E-0022**：`atomic()` 里的补偿路径不许用裸 `return` ⇒ 造「写到一半失败」与「可预期拒绝」两条路，断言**没有半态**；**接线**：消费者跑在 **outbox 之前**，且它留下的「要人看见的事」**真的落到绑定 `last_error`**（⛔ 不是只写日志）。
三条纪律：⛔ **零真实网络**——假上游复用 `tests.test_jidanpu_openapi._Upstream`（MockTransport；两个假上游必然漂，所以**复用同一个**），地址用 `.invalid` 顶级域（桩没接上就永远解析不了）；⛔ **夹具一律合成值**（`AGENTS.md` §5-C / E-0077）——码是 22 位合成串、密钥 / 客户端 id 都是 `synthetic` / `canary`、来源 IP 取 **TEST-NET-3**（`203.0.113.0/24`，文档保留段）；⛔ **断言打在数据库行上**（E-0009）——订单状态、审计行、台账行、绑定上的水位 / 计数。"""
from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import time
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
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import jidanpu_openapi, jidanpu_openapi_consume, jidanpu_upstream
from apps.business.jidanpu_upstream import HallOutcome, MyOrdersOutcome, OutboxStats
from apps.core.datetime_display import local_iso
from apps.business.models import (
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from apps.callback import signature
from apps.callback import store as callback_store
from apps.callback import wsgi as callback_wsgi
#: ⭐ 复用 T2 那一条线的假上游与合成凭据（⛔ 不另写一个假上游：两份桩必然漂）。
from tests.test_jidanpu_openapi import (
    BASE_URL,
    CLIENT_ID,
    ORDER_STATUS_PATH,
    SENTINEL_SECRET,
    _Upstream,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值 ────────────────────────────────────────────────────────────────

#: 三串 22 位合成码（形状 = `[A-Za-z0-9_-]{22}`，照接收器的定长正则）。
CODE_MINE = "Ab3_-consume_tests_A22"
CODE_OTHER = "Cd4_-consume_tests_B22"
CODE_UNCLAIMED = "Ef5_-consume_tests_C22"
assert len(CODE_MINE) == len(CODE_OTHER) == len(CODE_UNCLAIMED) == 22

SECRET_MINE = "tt-consume-secret-0001"
SECRET_OTHER = "tt-consume-secret-0002"
SECRET_UNCLAIMED = "tt-consume-secret-0003"

#: 密钥文件的**首行注释**（照接收器 spec §5.3 的固定文案；⛔ 实现里不拿它当判据）。
KEYS_HEADER = "# 团团通 · 鸡蛋铺官方 API 回调密钥（0600；由「保存绑定」自动维护；⛔ 人工改要重启接收器）"

#: 上家单号（远端）与我方单号（20 位，`Order.order_no` 的列宽）。
REMOTE_ID = "20261008000000000901"
OTHER_REMOTE_ID = "20261008000000000902"
ORDER_NO = "20261008000000000801"
ORDER_NO_B = "20261008000000000802"

#: 合成来源 IP（TEST-NET-3 文档保留段，⛔ 不是真值）。
SOURCE_IP = "203.0.113.9"

#: 固定的墙钟（⛔ 不用 `timezone.now()`：水位与「最近收到」的读数要能对着字面值断言）。
FIXED_1 = datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt_timezone.utc)
FIXED_2 = datetime(2026, 10, 8, 12, 0, 3, tzinfo=dt_timezone.utc)
FIXED_3 = datetime(2026, 10, 8, 12, 0, 6, tzinfo=dt_timezone.utc)
ROUND_NOW = datetime(2026, 10, 8, 13, 0, 0, tzinfo=dt_timezone.utc)

#: 权威状态（`GetOrderStatus` 的 `data.status`）——「已暂停」是**会写**的那三格之一
#: （`IN_PROGRESS → PAUSED`），另两格是「问题单 → ISSUE」与「未分配 → UNASSIGNED」。
AUTHORITATIVE = "已暂停"
#: 回调**报文里**那个 `status`：**刻意**取一个「如果拿它当真就会写错」的值（问题单 ⇒ ISSUE）。
CALLBACK_STATUS = "问题单"
#: 认不出来的状态（任何表里都不会有它）。
UNKNOWN_TOKEN = "外星状态"

#: ⭐ 回调失败计次台账的 kind 与上限（**字面值**：⛔ 不从生产常量派生，E-0024）。
#: 计次台账复用 `JidanpuSyncEvent.attempts`（零迁移），一行一条事件。
CALLBACK_RETRY_KIND_LITERAL = "openapi.callback_retry"
CALLBACK_MAX_ATTEMPTS_LITERAL = 5


class _FrozenClock:
    """接收器 `_now_iso()` 的时钟替身：`datetime.now(tz)` 回一个**固定**时刻。
    ⚠️ 为什么需要它：水位是「我们自己的接收时刻」，用例要能拿**确定的值**去断言库里的列（`AGENTS.md` §5-G：断言要钉「值」，不要钉「跑的时刻碰巧是什么」）。"""

    value: datetime = FIXED_1

    @classmethod
    def now(cls, tz=None):
        return cls.value


def _read_lines(path: Path) -> list[dict]:
    """事件文件 → 解析后的记录列表（⛔ 不 `exists()` 当零条：文件不在就是 0 条）。"""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _row_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ══ 夹具基类 ═════════════════════════════════════════════════════════════════


class _ConsumeCase(TenantTestCase):
    """临时事件 / 密钥文件 + 假上游 + 真接收器写事件 + ②式绑定 / 我方订单 / 链路。"""

    def setUp(self):
        super().setUp()
        jidanpu_openapi.reset_token_cache()
        jidanpu_openapi_consume.reset_process_state()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.addCleanup(jidanpu_openapi_consume.reset_process_state)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "shared" / "callback" / "events.jsonl"
        self.keys_file = self.tmp / "shared" / "callback-keys.env"
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        self.keys_file.write_text(
            KEYS_HEADER + "\n"
            + f"{CODE_MINE}\t{SECRET_MINE}\n"
            + f"{CODE_OTHER}\t{SECRET_OTHER}\n"
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

        self.game = Game.objects.create(name="测试游戏甲")
        #: ⭐ 每个用例都有一行默认的②式绑定（`CODE_MINE`）——子类要别的形状就自己再造一行。
        self.binding = self.make_binding()

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_binding(self, *, code=CODE_MINE, name="a", secret=SECRET_MINE, enabled=True):
        """一行**②式**（`openapi`）绑定；绑定人**持有 `order.change_status`**（写状态要它）。"""
        username = f"kefu-consume-{name}"
        make_logged_in_client(
            username=username, groups=("客服",), nodes=("order.change_status",),
        )
        return JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=BASE_URL,
            account=f"acc-consume-{name}", password="pw-canary-consume", mode="openapi",
            client_id=f"{CLIENT_ID}-{name}", client_secret=secret, callback_code=code,
            enabled=enabled,
        )

    def make_order(self, *, order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=status, total_price=Decimal("100.00"),
        )

    def make_link(self, binding, order, *, remote_id=REMOTE_ID, **kwargs):
        return JidanpuUpstreamOrderLink.objects.create(
            binding=binding, order=order, upstream_order_id=remote_id, **kwargs,
        )

    def reply_status(self, status=AUTHORITATIVE):
        """假上游：`GetOrderStatus` 回一个**成功**信封（`code=1`，实测只读端点成功码）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None,
            "data": {
                "status": status, "creatTime": "2026-10-08 12:00:00", "operTime": None,
                "doneTime": None, "verifyTime": None,
            },
        })

    # ── 让**真接收器**写一条事件 ─────────────────────────────────────────────

    def post_callback(self, *, code=CODE_MINE, secret=SECRET_MINE, params=None, path=None,
                      sign=None, received_at=FIXED_1) -> str:
        """走真接收器（`wsgi.application`）打一条 POST ⇒ 返回它的状态行。
        ⭐ **这是「形状照接收器」的落地方式**：记录由接收器自己落盘，⛔ 不是用例手搓一个 dict（手搓的形状会漂，而漂了之后用例照样绿——那正是本仓最防的假绿）。"""
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
    def status_params(*, order_no=REMOTE_ID, status=CALLBACK_STATUS, timestamp="1791500000"):
        """一条 `action=10` 的**回调报文**（合成值；`timestamp` 是按实测 +8 小时的形状给的假值）。"""
        return {
            "orderNo": order_no,
            "sourceOrderNo": ORDER_NO,
            "status": status,
            "note": "",
            "action": "10",
            "timestamp": timestamp,
        }

    # ── 跑一轮消费者 ────────────────────────────────────────────────────────

    def consume(self, binding, *, now=ROUND_NOW):
        """跑一轮消费者；同时断言「记录 > 0」这条前置（免得整份用例落在空文件上空洞通过）。"""
        self.assertTrue(
            _read_lines(self.events), "事件文件是空的 ⇒ 消费者这一轮什么都没得处理（夹具坏了）",
        )
        return jidanpu_openapi_consume.consume_callbacks(binding, now=now)

    # ── 读数 ────────────────────────────────────────────────────────────────

    def order_status(self, order) -> str:
        """**重新查库**取状态（⛔ 不读内存对象：那会把「没写」看成本地那次改动）。"""
        return Order.objects.get(pk=order.pk).status

    def notes(self, *, kind=None):
        rows = JidanpuSyncEvent.objects.all()
        return rows.filter(kind=kind) if kind else rows

    def refresh(self, binding):
        return JidanpuUpstreamBinding.objects.get(pk=binding.pk)


# ══ ① 端到端（本线唯一「看得见效果」的那一环）══════════════════════════════════


class EndToEndTests(_ConsumeCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_a_real_event_changes_the_order_row(self):
        """⭐ **端到端**：真接收器落盘的一条回调 ⇒ 跑一轮 ⇒ 我方订单状态**真的变了**。⛔ 弄红它：让消费者不写（去掉 `_write_status` 的调用 / 把 `plan.write` 当假）⇒ 第一条断言红（库里还是「代练中」）。
        ⚠️ 三条断言各自承重：① **数据库行**（E-0009：不看返回值、不看 logger）；② **回调里的 `status` 不作数**（报文说「问题单」，最终必须是「已暂停」= 权威值——这是 B4「回调只当提醒不当真相」在数据上的判据）；
        ③ 审计 + 台账**各一行**（写状态这件事在两个出口上都要留痕）。"""
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.binding)

        # ① 订单状态**真的变了**（重新查库）
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertNotEqual(self.order_status(self.order), "ISSUE")  # 回调里那个值不许作数
        self.assertEqual(outcome.applied, 1)
        # ② 审计：既有动作码 `order.transition`，前后值 = 中文 label、actor = 本线的机器名
        audit = OperationLog.objects.filter(order_no=ORDER_NO, action="order.transition")
        self.assertEqual(audit.count(), 1)
        row = audit.get()
        self.assertEqual((row.from_value, row.to_value), ("代练中", "已暂停"))
        self.assertEqual(row.actor, jidanpu_openapi_consume.MACHINE_ACTOR)
        # 机器行：`actor_id` 留空（与登录式上家线的 `_pull_row` 逐字同形）
        self.assertIsNone(row.actor_id)
        # ③ 台账：一条「已写」的留痕（认得出的 kind）
        done = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO, status="done")
        self.assertEqual(done.count(), 1)
        self.assertIn(AUTHORITATIVE, done.get().reason)
        # ④ 水位 / 健康列：水位 = **我们自己的接收时刻**（⛔ 不是报文里那个偏 +8 小时的 timestamp）
        fresh = self.refresh(self.binding)
        self.assertEqual(fresh.callback_watermark_at, FIXED_1)
        self.assertEqual(fresh.callback_last_at, FIXED_1)
        self.assertEqual(fresh.callback_count, 1)
        # ⑤ 真打了权威那一次（一条事件 ⇒ 一次查单）
        self.assertEqual(len(self.upstream.api_calls()), 1)
        self.assertEqual(self.upstream.last_api_call()["params"], {"orderId": REMOTE_ID})

    def test_the_write_goes_through_the_same_exit_as_the_login_line(self):
        """⭐ 写面与登录式上家线**同一个出口**：`apply_transition` + `nodes_of(binding.user)`。
        ⛔ 弄红它：在本模块里另写一条写路径（直接 `order.status = …; order.save()`）⇒ 本用例红（桩上的调用数为 0，而且上面那条端到端用例的审计行也不会出现）。"""
        self.assertEqual(self.post_callback(), "200 OK")
        real = jidanpu_upstream.apply_transition
        seen: list[dict] = []

        def _spy(order, target, **kwargs):
            seen.append({"target": str(target), **kwargs})
            return real(order, target, **kwargs)

        with mock.patch.object(
            jidanpu_openapi_consume.jidanpu_upstream, "apply_transition", side_effect=_spy,
        ):
            self.consume(self.binding)

        self.assertEqual(len(seen), 1)
        call = seen[0]
        self.assertEqual(call["target"], "PAUSED")
        self.assertEqual(call["actor"], jidanpu_openapi_consume.MACHINE_ACTOR)
        # ⚠️ ⛔ 消费方**不传** `actor_id`（照 `_pull_row` 的形状：机器动作只有 `actor` 名字，
        #    `actor_id` 的 `None` 是在审计那一笔上显式给的）——传了会与那条线漂。
        self.assertNotIn("actor_id", call)
        self.assertIs(call["suppress_outbound_sync"], True)
        self.assertEqual(
            call["nodes"],
            frozenset({"order.change_status"}),
            "写面必须用绑定人自己的节点集（⛔ 不许无条件放行）",
        )


# ══ ①' 跟退那一档（⭐ 审计批 D3，L-13）══════════════════════════════


class RefundFollowTests(_ConsumeCase):
    """「已退单」那一档**真的跟退**（不再「只登记 + 人工核对」）。病根：②式上游绑定遇「已退单」原先只记一条 `failed` 台账 ⇒ 上家退了单、我方状态与钱都停在原地（打手的钱留在钱包里 / 绩效照旧），与①式自动跟退不对称。
    ⚠️ 钱面判据打在**钱包余额 / 绩效行 / 回冲行**上（`AGENTS.md` §5-G；与 `tests/test_settle_p1_peer_follow_refund.py` 同一条纪律），⛔ 不只看返回值。
    ⚠️ 端到端照 `EndToEndTests` 的同一条路（真接收器落盘 → 跑一轮）。"""

    #: 钱包里那笔自动入账的金额：`100.00 × 60% = 60.00`（字面值，⛔ 不从被测模块派生）。
    PAID = Decimal("60.00")
    TAKER_RATE = Decimal("60.00")

    def setUp(self):
        super().setUp()
        from apps.accounts.choices import AccountSource, CommissionMode
        from apps.accounts.models import StaffProfile

        self.taker = User.objects.create_user(username="refund-taker", password="pw-canary-x")
        StaffProfile.objects.create(
            user=self.taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=self.TAKER_RATE,
            account_source=AccountSource.APP,
        )

    def _completed_order_with_wallet(self, *, order_no):
        """一张**已完成 + 系统自动发薪**的单（钱在打手钱包里），回 `(order, perf)`。
        ⚠️ 迁移链照 `tests/test_settle_p1_peer_follow_refund.py` 的同一条：`待验收 → 已完成`（入 `COMPLETED` 只剩这一条入边，`order_status._COMPLETED_SOURCE`）⇒ 夹具单从 `PENDING_REVIEW` 起、显式走领域函数推进，
        ⛔ 不是直接 `create(status=COMPLETED)` 再自环（那是非法迁移，会响亮抛）。"""
        from apps.accounts.payout import payout_completed_order
        from apps.business.models import EmpType, Performance

        order = self.make_order(order_no=order_no, status=OrderStatus.PENDING_REVIEW)
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=self.PAID, bonus_amount=self.PAID,
        )
        jidanpu_upstream.apply_transition(
            order, OrderStatus.COMPLETED, nodes=frozenset({"order.change_status_completed"}),
        )
        payout_completed_order(order)
        perf.refresh_from_db()
        return order, perf

    def _wallet_balance(self):
        from apps.accounts.models import TakerWallet

        return TakerWallet.objects.get(user=self.taker).balance

    def _clawbacks(self):
        from apps.business.models import PerformanceAdjustment

        return list(
            PerformanceAdjustment.objects.filter(
                request_id__startswith="PAYOUT-CLAWBACK-"
            ).order_by("id")
        )

    def _post_and_consume(self, *, status="已退单", code=CODE_MINE, secret=SECRET_MINE):
        self.assertEqual(
            self.post_callback(
                code=code, secret=secret, params=self.status_params(status=status),
            ),
            "200 OK",
        )
        return self.consume(self.binding)

    def test_an_upstream_refund_is_followed_and_the_money_moves_back(self):
        """⭐ 自动跟退成功：系统自动发薪的完成单 ⇒ 跟退后**钱包回 0**、订单 `REFUNDED`、台账 `done`。
        ⛔ 弄红它：把 `_follow_refund` 的调用摘掉（改回「只登记」）⇒ 订单与钱包两条断言红。"""
        order_no = ORDER_NO
        order, perf = self._completed_order_with_wallet(order_no=order_no)
        self.make_link(self.binding, order)
        self.reply_status("已退单")
        # 夹具自证：钱真的在钱包里（60.00），行真的被系统标了「已结账」。
        self.assertEqual(self._wallet_balance(), self.PAID)
        self.assertEqual(perf.settled_by, "system")

        outcome = self._post_and_consume()

        # ① 钱面（承重判据）：钱包回 0 + 回冲恰一条 −60.00 + 绩效两列清零 + 结账痕迹复位。
        self.assertEqual(self._wallet_balance(), Decimal("0.00"))
        clawbacks = self._clawbacks()
        self.assertEqual(len(clawbacks), 1)
        self.assertEqual(clawbacks[0].amount, Decimal("-60.00"))
        perf.refresh_from_db()
        self.assertEqual((perf.temp_amount, perf.bonus_amount), (Decimal("0.00"), Decimal("0.00")))
        self.assertFalse(perf.is_settled)
        # ② 订单状态真的到了「已退单」。
        self.assertEqual(self.order_status(order), "REFUNDED")
        self.assertEqual(outcome.applied, 1)
        # ③ 台账 `done`，人话点明「自动跟退」；写成功 ⛔ 不进 `findings`（错误列）。
        done = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO, status="done")
        self.assertEqual(done.count(), 1)
        self.assertIn("自动跟退", done.get().reason)
        self.assertEqual(outcome.findings, ())

    def test_a_manually_settled_completion_is_kept_and_logged_for_humans(self):
        """⭐ 已结账被闸拦：**财务手工结账**的完成单 ⇒ 状态与钱一个字不动，台账 `failed` 要人看。
        （那些钱从来没进过钱包，跟着扣就是凭空多扣——D-0287 的手工行仍挡。）"""
        from apps.business.models import EmpType, Performance, SettleStatus
        from django.utils import timezone as dj_tz

        order = self.make_order(status=OrderStatus.COMPLETED)
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=self.PAID, bonus_amount=self.PAID,
        )
        Performance.objects.filter(pk=perf.pk).update(
            settle_status=SettleStatus.SETTLED, settled_at=dj_tz.now(),
            settled_by="finance-manual", settled_amount=self.PAID,
        )
        self.make_link(self.binding, order)
        self.reply_status("已退单")

        outcome = self._post_and_consume()

        # 钱面与状态面都**一个字不动**。
        self.assertEqual(self.order_status(order), "COMPLETED")
        self.assertEqual(len(self._clawbacks()), 0)
        perf.refresh_from_db()
        self.assertEqual((perf.temp_amount, perf.bonus_amount), (self.PAID, self.PAID))
        self.assertEqual(perf.settled_by, "finance-manual")
        # 结论 = 要人看：`failed` 计数 + 台账 `failed` 行 + `findings`（进绑定 `last_error`）。
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.failed, 1)
        row = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO, status="failed").get()
        self.assertIn("挡住", row.reason)
        self.assertEqual(len(outcome.findings), 1)

    def test_a_denied_refund_only_leaves_a_failed_ledger_row(self):
        """⭐ 跟退失败：绑定账号缺 `order.change_status` 节点 ⇒ 状态机拒绝 ⇒ `failed` 台账，不动钱。"""
        from apps.accounts.models import TakerWallet

        order = self.make_order()
        # 一个**没有节点**的绑定（自己的回调码）：`nodes_of(user)` 是空集 ⇒ `apply_transition`
        # 响亮拒绝（缺节点），`_follow_refund` 要把它收成 `failed` 台账 + `_NOTED`。
        no_nodes = self.make_binding_no_nodes()
        self.make_link(no_nodes, order)
        self.reply_status("已退单")
        self.assertEqual(
            self.post_callback(
                code=CODE_OTHER, secret=SECRET_OTHER,
                params=self.status_params(status="已退单"),
            ),
            "200 OK",
        )
        outcome = jidanpu_openapi_consume.consume_callbacks(no_nodes, now=ROUND_NOW)

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.failed, 1)
        row = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO, status="failed").get()
        self.assertIn("跟退被状态机拒绝", row.reason)
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())

    def test_an_already_refunded_order_is_an_idempotent_skip(self):
        """幂等：我方**已经是**「已退单」⇒ 正常结论 `skipped`（⛔ 不进错误列），钱包不再动。"""
        from apps.accounts.models import TakerWallet

        order = self.make_order(status=OrderStatus.REFUNDED)
        self.make_link(self.binding, order)
        self.reply_status("已退单")

        outcome = self._post_and_consume()

        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.failed, 0)
        row = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO, status="skipped").get()
        self.assertIn("幂等", row.reason)
        self.assertEqual(outcome.findings, ())
        self.assertFalse(TakerWallet.objects.filter(user=self.taker).exists())

    def make_binding_no_nodes(self, *, name="nonodes"):
        """一行**没有 `order.change_status`** 的②式绑定（制造「缺节点」拒绝那一档）。
        ⚠️ 回调码用**另一串**（`CODE_OTHER`，密钥文件的第二行）：`callback_code` 上有唯一约束 ⇒ 不能与 `self.binding` 共码；事件也打到**那个码**上（见用例内）。
        ⚠️ **groups 也空的**：`nodes_of` 会并上**组**带的节点（「客服」组本身带 `order.change_status`——夹具自证见上面那条的失败方向），要造「真没节点」就得连组都不给。"""
        username = f"kefu-consume-{name}"
        make_logged_in_client(username=username)
        return JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=BASE_URL,
            account=f"acc-consume-{name}", password="pw-canary-consume", mode="openapi",
            client_id=f"{CLIENT_ID}-{name}", client_secret=SENTINEL_SECRET,
            callback_code=CODE_OTHER, enabled=True,
        )


# ══ ② E1：只认本绑定的那串码 ═════════════════════════════════════════════════


class CodeRoutingTests(_ConsumeCase):
    def test_only_this_bindings_code_is_processed(self):
        """⭐ **E1**：本码 / 别的码 / 前缀相同的更长的码 ⇒ 只处理本码那一条。
        ⛔ 弄红它：把码的比对写成 `path.startswith(prefix)`（前缀相同就全收）⇒ 三条都被处理。
        ⚠️ 第三条（23 位码）**接收器根本不会落盘**（形状不合法 ⇒ 404 且不落盘）——它是**手写**的一行，用来钉「消费者自己的比对也必须是全等」这一半。"""
        other = self.make_binding(code=CODE_OTHER, name="other", secret=SECRET_OTHER)
        mine_order = self.make_order(order_no=ORDER_NO)
        other_order = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, mine_order)
        self.make_link(other, other_order, remote_id=OTHER_REMOTE_ID)
        self.reply_status()

        self.assertEqual(self.post_callback(), "200 OK")
        self.assertEqual(
            self.post_callback(code=CODE_OTHER, secret=SECRET_OTHER,
                               params=self.status_params(order_no=OTHER_REMOTE_ID)),
            "200 OK",
        )
        # ⚠️ 手写一行：23 位码（前缀相同的更长的码）。接收器不会写这种记录（404 不落盘）
        #    ⇒ 这一行的形状照 `wsgi._new_record` 抄，只为把「全等比对」这一半钉死。
        longer = CODE_MINE + "X"
        raw = dict(_read_lines(self.events)[0])
        raw["path"] = f"{callback_wsgi.CALLBACK_PATH_PREFIX}{longer}"
        with self.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n")

        self.consume(self.binding)

        self.assertEqual(self.order_status(mine_order), "PAUSED")
        self.assertEqual(
            self.order_status(other_order), "IN_PROGRESS", "别的绑定那一单被本绑定改了",
        )

    def test_a_code_no_enabled_binding_claims_is_loudly_registered(self):
        """⭐ 码没有**启用中的②式绑定**认领 ⇒ 留一条台账（⛔ 不静默）。
        ⛔ 弄红它：把这一档改成 `continue`（静默跳过）⇒ 台账为 0 行。
        ⚠️ 两种情况都要报：**压根没有这个码的绑定**，以及**有绑定但停用了**（那一条回调同样没有任何人在消费——`enabled_bindings()` 遍历不到停用的行）。"""
        disabled = self.make_binding(code=CODE_OTHER, name="disabled", secret=SECRET_OTHER,
                                     enabled=False)
        self.assertFalse(disabled.enabled)
        order = self.make_order()
        self.reply_status()
        self.assertEqual(self.post_callback(code=CODE_UNCLAIMED, secret=SECRET_UNCLAIMED,
                                            params=self.status_params()), "200 OK")
        self.assertEqual(
            self.post_callback(code=CODE_OTHER, secret=SECRET_OTHER,
                               params=self.status_params(order_no=OTHER_REMOTE_ID)),
            "200 OK",
        )

        outcome = self.consume(self.binding)
        # 再跑一轮：**同一个码只登记一次**（幂等键是四元组，⛔ 不是每轮灌一条）
        self.consume(self.binding)

        self.assertEqual(
            self.notes(kind="openapi.callback_unclaimed_code").count(), 2,
            "两个没人认领的码各应留下恰好一行台账（跑两轮不许翻倍）",
        )
        refs = set(
            self.notes(kind="openapi.callback_unclaimed_code").values_list("target_ref", flat=True)
        )
        self.assertEqual(refs, {f"code:{CODE_UNCLAIMED}", f"code:{CODE_OTHER}"})
        self.assertEqual(outcome.unclaimed, 2)
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.upstream.requests, [], "没人认领的码竟然打了上游")


# ══ ③ 验签没过 / 报文读不出来 ═════════════════════════════════════════════════


class UntrustedEventTests(_ConsumeCase):
    def test_an_event_that_failed_the_signature_never_touches_the_order(self):
        """⭐ **验签没过 ⇒ ⛔ 一个字都不改订单**（而且**一次上游都不打**）。
        ⛔ 弄红它：去掉 `sign_ok` 那一道判据 ⇒ 订单被改、上游被打、台账 kind 也不对。
        ⚠️ 报文本身是**形状完美**的一条状态回调（有 `orderNo`、有 `status`）：这正是要防的形态——伪造者能把正文写得漂漂亮亮，能挡住的只有签名。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        self.assertEqual(
            self.post_callback(sign="0" * 32), "401 Unauthorized",
        )
        record = _read_lines(self.events)[-1]
        self.assertFalse(record["sign_ok"])
        self.assertEqual(record["sign_reason"], "sign_mismatch")

        outcome = self.consume(self.binding)

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.upstream.requests, [], "验签没过的报文竟然拿去查了上游")
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 0)
        rows = self.notes(kind="openapi.callback_unsigned")
        self.assertEqual(rows.count(), 1)
        self.assertIn("sign_mismatch", rows.get().reason)
        self.assertEqual(outcome.failed, 1)

    def test_a_body_that_is_not_json_is_registered_not_guessed(self):
        """报文不是 JSON 对象（真接收器回 400 并留痕）⇒ 只留一条台账，⛔ 不写、⛔ 不猜。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        raw = b"<html>not json</html>"
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_MINE}",
            "QUERY_STRING": "", "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": BytesIO(raw),
            "REMOTE_ADDR": "127.0.0.1", "HTTP_X_REAL_IP": SOURCE_IP,
        }
        with mock.patch.object(callback_wsgi, "datetime", _FrozenClock):
            _FrozenClock.value = FIXED_1
            callback_wsgi.application(environ, lambda *a: None)
        self.assertEqual(_read_lines(self.events)[-1]["sign_reason"], "body_not_json")

        self.consume(self.binding)

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.upstream.requests, [])
        # 没验签成功 ⇒ 归「验签没过」那一档（fail-closed：💡 不解析没验过签的正文）
        self.assertEqual(
            self.notes().filter(
                kind__in=["openapi.callback_unsigned", "openapi.callback_unreadable"]
            ).count(),
            1,
        )


# ══ ④ 找不到我方订单 / 不是 action=10 ═════════════════════════════════════════


class UnmatchedEventTests(_ConsumeCase):
    def test_an_event_without_a_link_never_writes_and_leaves_a_trace(self):
        """⭐ 找不到我方订单 ⇒ 明确出口 + 留痕（⛔ 不建单、⛔ 不改任何订单）。
        ⛔ 弄红它：把「找不到链路」那一档改成静默 `continue` ⇒ 台账 0 行。
        ⚠️ 第二半承重：**别的绑定**名下有一条同号的链路 ⇒ 本绑定照样不许动它（B5）。"""
        mine = self.make_order(order_no=ORDER_NO)
        other = self.make_binding(code=CODE_OTHER, name="owned", secret=SECRET_OTHER)
        other_order = self.make_order(order_no=ORDER_NO_B)
        self.make_link(other, other_order, remote_id=REMOTE_ID)  # 同号，但归**别的绑定**
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")

        self.consume(self.binding)

        self.assertEqual(self.order_status(mine), "IN_PROGRESS")
        self.assertEqual(self.order_status(other_order), "IN_PROGRESS")
        rows = self.notes(kind="openapi.callback_unknown_order")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().target_ref, f"{self.binding.pk}:{REMOTE_ID}")
        self.assertEqual(rows.get().payload["order_no"], REMOTE_ID)
        self.assertEqual(self.upstream.requests, [], "找不到我方订单竟然打了上游")

    def test_a_notification_that_is_not_a_status_change_is_only_registered(self):
        """不是 `action=10` ⇒ 只登记、不改订单（第一期只消费状态；⛔ 也不为它打上游）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        params = self.status_params()
        params["action"] = "30"
        params["imgUrl"] = "https://oss.example.invalid/synthetic-1.png"
        params["name"] = "合成截图"
        self.assertEqual(self.post_callback(params=params), "200 OK")

        self.consume(self.binding)

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.upstream.requests, [])
        rows = self.notes(kind="openapi.callback_other_action")
        self.assertEqual(rows.count(), 1)
        self.assertIn("action=30", rows.get().reason)

    def test_a_soft_deleted_order_is_skipped_instead_of_crashing(self):
        """我方订单已软删 ⇒ 跳过 + 留痕（⛔ 不为它打上游，⛔ 不许抛异常）。
        ⚠️ 这条**不是防御性写法**：`link.order` 走 forward FK ⇒ 软删单取得到，而唯一写入口加锁读的是**默认 manager**（读不到软删行）⇒ 放它往下走会抛 `Order.DoesNotExist`（未预期）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.reply_status()
        Order.objects.filter(pk=order.pk).update(deleted_at=FIXED_1)
        self.assertEqual(self.post_callback(), "200 OK")

        self.consume(self.binding)

        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 0)
        self.assertEqual(self.notes(kind="openapi.callback_not_tracked").count(), 1)


# ══ ⑤ E3：写之前一定先查（查不动 ⇒ 一个字节都不写）════════════════════════════


class QueryBeforeWriteTests(_ConsumeCase):
    def test_a_transport_failure_writes_nothing_and_keeps_the_watermark(self):
        """⭐ **E3**：查权威失败 ⇒ **一个字节都不写订单**，且水位**不前移**（下一轮重试）。
        ⛔ 弄红它：把 `_RETRY` 改成「用回调里的值写」⇒ 订单会变成「问题单」（ISSUE），而正确行为是**保持代练中**。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("synthetic transport failure"))
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.binding)

        self.assertEqual(self.order_status(order), "IN_PROGRESS", "查不到权威竟然写了订单")
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 0)
        rows = self.notes(kind="openapi.callback_query_failed")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "failed")
        self.assertIn("ConnectError", rows.get().reason)
        self.assertEqual(outcome.failed, 1)
        # 水位**不前移** ⇒ 下一轮还会重试这一条（库里的水位仍是空的）
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at)

    def test_a_business_rejection_from_the_status_endpoint_also_writes_nothing(self):
        """上游**业务级**拒绝（HTTP 200 + `code=0` + 「没有权限」）同样一个字都不写。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.upstream.reply(
            ORDER_STATUS_PATH, payload={"code": 0, "msg": "没有权限", "data": None},
        )
        self.assertEqual(self.post_callback(), "200 OK")

        self.consume(self.binding)

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.notes(kind="openapi.callback_query_failed").count(), 1)
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at)

    def test_two_failures_with_different_root_causes_both_land_in_the_journal(self):
        """⭐ ④ 幂等键不许把「**不同根因**」的两句话吞成一句：同类型、不同根因 ⇒ **两条**台账。⛔ 弄红它：把 `_failure_detail` 的兜底那一支改回「只留类型名」⇒ 第二句结论被四元组吞掉（台账仍旧 1 行 ⇒ 第一条断言红）。
        ⚠️ 两个方向一起钉（口径是「**同一句原因只记一条**，换了原因就要新记一条」）：换根因 ⇒ 落**第二条**；**同一根因再失败一次** ⇒ 仍然只有那一条（⛔ 不是每轮灌一条）。⚠️ `reason` 里⛔ 不许出现异常原文（它可能带请求信息 / 凭据）——只留类型名 + 一条**根因指纹**（`str(exc)` 的短摘要，推不回原文）。
        ⚠️ 这一支**只对非 `OpenApiError`** 生效：`OpenApiError` 的人话本来就带 HTTP 状态 / 上游 `code` / `transport` 类型 / 脱敏后的对方原文 ⇒ 不同根因本来就落不同的账（见 `_failure_detail` 注释）。"""
        order = self.make_order()
        self.make_link(self.binding, order)
        self.assertEqual(self.post_callback(), "200 OK")

        def _fail_with(root_cause):
            with mock.patch.object(
                jidanpu_openapi_consume._StatusQueries, "status_of",
                side_effect=RuntimeError(root_cause),
            ):
                self.consume(self.binding)

        _fail_with("合成根因甲")
        _fail_with("合成根因乙")

        rows = self.notes(kind=jidanpu_openapi_consume.KIND_QUERY_FAILED)
        self.assertEqual(rows.count(), 2, "同类型不同根因的第二句结论被四元组吞掉了")
        for row in rows:
            self.assertIn("RuntimeError", row.reason)
            self.assertIn("根因指纹", row.reason)
            self.assertNotIn("合成根因甲", row.reason, "异常原文进了台账（可能带请求信息 / 凭据）")
            self.assertNotIn("合成根因乙", row.reason, "异常原文进了台账（可能带请求信息 / 凭据）")

        # ⛔ 同一根因**再**失败一次 ⇒ 仍然只有那一条（幂等键的口径没变）
        _fail_with("合成根因甲")
        self.assertEqual(
            self.notes(kind=jidanpu_openapi_consume.KIND_QUERY_FAILED).count(), 2,
            "同一句原因被灌了第二条（那一档是「下一轮重试」的档，会每轮都失败）",
        )
        # 这一档照旧：一个字都不写订单、水位不前移（下一轮重试）
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at)

    def test_a_retryable_failure_stops_the_batch_but_the_next_round_finishes_it(self):
        """⭐ 查失败那一档「**本轮停在这里**」：后面的那一单还留在水位之后（⛔ 不丢）。
        造两条事件（两条单）：第一条查失败时**停批**；下一轮上游恢复正常 ⇒ 两条都处理掉。"""
        first = self.make_order(order_no=ORDER_NO)
        second = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, first, remote_id=REMOTE_ID)
        self.make_link(self.binding, second, remote_id=OTHER_REMOTE_ID)
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("synthetic transport failure"))
        self.assertEqual(self.post_callback(), "200 OK")
        self.assertEqual(
            self.post_callback(params=self.status_params(order_no=OTHER_REMOTE_ID),
                               received_at=FIXED_2),
            "200 OK",
        )

        self.consume(self.binding)
        self.assertEqual(self.order_status(first), "IN_PROGRESS")
        self.assertEqual(self.order_status(second), "IN_PROGRESS")

        # 上游恢复 ⇒ 第二轮把两条都处理掉
        self.upstream.errors.clear()
        self.reply_status()
        self.consume(self.binding, now=ROUND_NOW + timedelta(minutes=1))

        self.assertEqual(self.order_status(first), "PAUSED")
        self.assertEqual(self.order_status(second), "PAUSED")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)


# ══ ④″ 回调失败重试上限（按事件计次；撞上限**不算**失败）══════════════════════════


class CallbackAttemptCapTests(_ConsumeCase):
    """反复失败的**同一条事件** ⇒ 计满 `CALLBACK_MAX_ATTEMPTS` 后放弃并让水位越过它。
    ⛔ **只对确认失败计次**：本轮撞「最多查几条」上限的那条事件一次都不许算失败（它下一轮就能查，不是坏事件）。判据见 `_StatusQueries.cap_hit` 与 `consume_callbacks`。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)

    def _retry_rows(self):
        return self.notes(kind=CALLBACK_RETRY_KIND_LITERAL)

    def test_an_event_that_keeps_failing_is_abandoned_and_the_watermark_moves(self):
        """⭐ 同一条事件连续失败 5 次 ⇒ 计次台账 `failed` + 人话，且水位**越过它**。
        ⛔ 弄红它：每轮都只是照旧把水位钉住（不记次、不放弃）⇒ 第 5 轮之后水位仍是空，且计次台账为 0 行 ⇒ 下面两条断言红。"""
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("synthetic transport failure"))
        self.assertEqual(self.post_callback(), "200 OK")

        for attempt in range(1, CALLBACK_MAX_ATTEMPTS_LITERAL):
            outcome = self.consume(
                self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=attempt),
            )
            self.assertEqual(outcome.failed, 1, f"第 {attempt} 次失败没有落到本轮计数")
            self.assertIsNone(
                self.refresh(self.binding).callback_watermark_at,
                f"第 {attempt} 次失败就把水位推走了",
            )
            rows = self._retry_rows()
            self.assertEqual(rows.count(), 1, "计次台账应当**一条事件一行**")
            row = rows.get()
            self.assertEqual(row.attempts, attempt)
            self.assertEqual(row.status, "failed")
            self.assertNotIn("已放弃", row.reason)

        final = self.consume(
            self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=5),
        )

        self.assertEqual(final.failed, 1)
        row = self._retry_rows().get()
        self.assertEqual(row.attempts, CALLBACK_MAX_ATTEMPTS_LITERAL)
        self.assertIn("重试 5 次仍失败，已放弃并推进水位", row.reason)
        self.assertEqual(
            self.refresh(self.binding).callback_watermark_at, FIXED_1,
            "放弃之后水位必须越过这条事件（否则下一轮还卡在它）",
        )
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")

        # 再跑一轮：那条事件已经在水位之前 ⇒ 不该再处理、计次也不该再涨
        after = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=6))
        self.assertEqual(after.mine, 0)
        row.refresh_from_db()
        self.assertEqual(row.attempts, CALLBACK_MAX_ATTEMPTS_LITERAL)

    def test_the_per_round_cap_is_not_counted_as_a_failure(self):
        """⭐⭐ **撞上限 ≠ 失败**：先造一次确认失败（计次 = 1），再连续撞上限 7 轮 ⇒ 仍 = 1。
        ⛔ 弄红它：把 `_RETRY` 一律计次（不区分撞上限）⇒ 撞上限那几轮把计数推到上限 ⇒ 事件被误放弃；下面「计数仍是 1」「上限放开后照旧处理成」两条会红。
        ⚠️ `ConsumeOutcome.failed` 在撞上限时仍计 1——那是**既有契约**（「本轮没成功的条数」，由既有用例钉住）；本用例钉的是**另一件事**：计次台账不涨、事件不被放弃。"""
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("synthetic transport failure"))
        self.assertEqual(self.post_callback(), "200 OK")

        first = self.consume(self.binding)
        self.assertEqual(first.failed, 1)
        row = self._retry_rows().get()
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at)

        with mock.patch.object(jidanpu_openapi_consume, "MAX_ORDERS_PER_ROUND", 0):
            for offset in range(CALLBACK_MAX_ATTEMPTS_LITERAL + 2):
                capped = self.consume(
                    self.refresh(self.binding),
                    now=ROUND_NOW + timedelta(minutes=offset + 1),
                )
                self.assertEqual(capped.failed, 1)  # 既有「本轮没成功」那一格
                self.assertIsNone(self.refresh(self.binding).callback_watermark_at)

        row.refresh_from_db()
        self.assertEqual(row.attempts, 1, "撞上限被算成了处理失败（计次涨了）")
        self.assertNotIn("已放弃", row.reason)

        # 上限放开 + 上游恢复 ⇒ 同一条事件照旧处理成（⛔ 没有被放弃）
        self.upstream.errors.clear()
        self.reply_status()
        final = self.consume(
            self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=10),
        )
        self.assertEqual(final.applied, 1)
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_1)

    def test_a_successful_event_writes_no_attempt_row(self):
        """默认路径（不触发失败）⇒ 计次台账一行都没有（行为逐字不变）。"""
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")
        outcome = self.consume(self.binding)
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(self._retry_rows().count(), 0)


# ══ ⑥ E2：水位与幂等 ══════════════════════════════════════════════════════════


class WatermarkTests(_ConsumeCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_the_same_file_is_consumed_exactly_once(self):
        """⭐ **E2**：同一份事件跑两轮 ⇒ **只处理一次**（第二轮连上游都不打）。
        ⛔ 弄红它：去掉水位那道判据（每轮都重读整份文件）⇒ 第二轮会再打一次上游 ⇒「第二轮零请求」那条断言红。"""
        self.assertEqual(self.post_callback(), "200 OK")
        self.consume(self.binding)
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertEqual(len(self.upstream.api_calls()), 1)

        second = self.consume(self.binding, now=ROUND_NOW + timedelta(minutes=1))

        self.assertEqual(second.mine, 0)
        self.assertEqual(second.applied, 0)
        self.assertEqual(len(self.upstream.api_calls()), 1, "第二轮又把同一件事查了一遍")
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertEqual(self.notes(kind="openapi.status_in").count(), 1)
        fresh = self.refresh(self.binding)
        self.assertEqual(fresh.callback_watermark_at, FIXED_1)
        self.assertEqual(fresh.callback_count, 1, "累计条数被重复计了一次")

    def test_clearing_the_watermark_rereads_without_a_second_write(self):
        """⭐ **E2 的另一半**：把水位清空 ⇒ **会重读**（查一次上游），但**不产生第二次写入**。
        ⛔ 弄红它：去掉 `should_write_status` 的「我方 == 目标 ⇒ 不写」那一格（改成无条件写）⇒ 审计行会变成 2 行、台账也会多一条。"""
        self.assertEqual(self.post_callback(), "200 OK")
        self.consume(self.binding)
        applied_note = self.notes(kind="openapi.status_in").get()

        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_watermark_at=None,
        )
        # ⚠️ 必须拿**重新查库**的那一行：内存对象上还留着上一轮写进去的水位（收窄写入会同步它）
        again = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        self.assertEqual(again.mine, 1, "清空水位之后应当整份重读")
        self.assertEqual(len(self.upstream.api_calls()), 2, "重读时应当再查一次权威状态")
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 1,
                         "重读竟然产生了第二次状态写入")
        # 台账：**同一句结论**不许被灌第二条（幂等键 = 四元组）；重读这一轮会**另**留下一条
        # 「我方 == 目标 ⇒ 不写（幂等）」的结论 —— 那是**另一句事实**，不是重复留痕。
        self.assertEqual(
            self.notes(kind="openapi.status_in").filter(reason=applied_note.reason).count(), 1,
            "重读竟然把同一句结论灌了第二条",
        )
        self.assertEqual(
            self.notes(kind="openapi.status_in").filter(status="skipped").count(), 1,
            "重读应当留下恰好一条「幂等 ⇒ 不写」的结论",
        )

    def test_a_shrunk_or_moved_file_resets_the_watermark_loudly(self):
        """⭐ **E8**：事件文件变短 / mtime 倒退 ⇒ **水位清零 + 响亮留痕**（⛔ 不静默跳过）。
        ⛔ 弄红它：去掉「比上次小」那一道判据 ⇒ 库里水位仍是旧值（新文件里那些**更旧**的事件从此永远轮不到——正是「搬机器」那一幕的静默丢）。
        ⚠️ 两条判据一起钉：`size` 变小（本用例）与 `mtime` 倒退（下面那一步）。"""
        self.assertEqual(self.post_callback(), "200 OK")
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        self.consume(self.binding)
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)
        self.assertEqual(len(_read_lines(self.events)), 2)

        # 文件被换成一份**更短的**（模拟「整块搬走 / 换了一台机器上的旧副本」）
        self.events.write_text(
            json.dumps(_read_lines(self.events)[0], ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        self.assertEqual(len(_read_lines(self.events)), 1)

        before_reset = self.notes().count()
        outcome = self.consume(self.refresh(self.binding))

        self.assertTrue(outcome.reset, "文件变短了却没有判出「被搬动过」")
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at, "水位没有清零")
        self.assertTrue(outcome.alert, "水位清零这一档必须留一句要人看见的话")
        self.assertIn("水位", outcome.alert)
        # 本轮**不消费**（清零那一轮只留痕）；下一轮才整份重读
        self.assertEqual(outcome.mine, 0)
        self.assertEqual(self.notes().count(), before_reset, "清零那一轮竟然也消费了")

        # 下一轮：整份重读 ⇒ 幂等键兜住（不产生第二次写入，也不多灌台账）
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 1)
        self.assertEqual(self.notes().count(), before_reset, "重读把台账灌多了")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_1)

    def test_a_shorter_file_whose_events_are_all_newer_is_still_treated_as_moved(self):
        """⭐ **「比上次小」那一道判据自己也能抓住**（⛔ 不是被「比水位还旧」那一道掩盖的）。场景：文件被截短 / 换成一份新的（**只有一条比水位更新**的事件）⇒「最新一条 < 水位」不成立，唯一能发现「文件被搬动过」的就是 **size 比上一次小**。
        ⛔ 弄红它：去掉 size 那一道判据 ⇒ 水位留在旧值上，本用例第一条断言红。
        ⚠️ 这一条专门补上「两条判据互为冗余」时的靶子：只有它红了，才说明 size 那一半**不是**没有用例的防御性写法（`AGENTS.md` §5-E / E-0021）。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.consume(self.binding)
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)

        # 文件被换成一份**更短的**、而且里面那条事件**比水位更新**（⇒ 另一条判据不成立）
        bigger = self.events.stat().st_size
        self.events.write_text("", encoding="utf-8")
        self.assertEqual(self.post_callback(received_at=FIXED_3), "200 OK")
        self.assertEqual(len(_read_lines(self.events)), 1)
        self.assertLess(self.events.stat().st_size, bigger, "自证夹具：这一份确实比上一份小")
        outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))

        self.assertTrue(outcome.reset, "文件变短了却没有判出「被搬动过」")
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at, "水位没有清零")
        self.assertEqual(outcome.mine, 0)

        # 下一轮：整份重读 ⇒ 那条**更新**的事件照常消费掉
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=3))
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_3)

    def test_a_mtime_going_backwards_alone_is_enough(self):
        """**mtime 倒退**（大小没变）也算「被搬动过」：判据是两条的**或**，⛔ 不是只认 size。⛔ 弄红它：① 去掉 mtime 那一道判据 ⇒ `outcome.reset` 为假；② 把下面 `os.utime` 拿掉（mtime 不倒退）⇒ 第一句自证红；③ 让改写多出一个字节 ⇒ 第二句自证红。
        ⚠️ 第②③句是**自证夹具**（⛔ 不是 `assertEqual(size, size)` 那种自己比自己的恒真断言）：它们钉的是「**只有 mtime 倒退、大小一个字节没动**」这个前置 ⇒ 上面那个 `reset` 只可能由 mtime 那一道判据给出（size 那一半在本用例里不成立）。
        恒真的断言等于没有断言（`AGENTS.md` §5-E / E-0063），所以这两句都拿**改动前先取的真值**去比。"""
        self.assertEqual(self.post_callback(), "200 OK")
        self.consume(self.binding)
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_1)

        before = self.events.stat()
        os.utime(self.events, (before.st_mtime - 600, before.st_mtime - 600))
        after = self.events.stat()
        self.assertLess(
            after.st_mtime, before.st_mtime,
            "自证夹具：mtime 没有倒退 ⇒ 本用例测不到 mtime 那一道判据",
        )
        self.assertEqual(
            after.st_size, before.st_size,
            "自证夹具：文件大小跟着变了 ⇒ size 那一半也能抓住它，本用例就不专一了",
        )
        outcome = self.consume(self.refresh(self.binding))

        self.assertTrue(outcome.reset)
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at)

    def test_the_per_round_cap_defers_but_never_drops(self):
        """⭐ 撞上「一轮最多查几张单」的上限 ⇒ **只推迟、不丢**（水位停在没处理的那一条之前）。
        ⛔ 弄红它：撞上限时把水位推到整批之后（「只查前 N 条、剩下的当没看见」）⇒ 第二条永远轮不到，下面的第二轮断言会红（订单仍是代练中）。
        ⚠️ 上限的**契约值**单独用字面量钉一次（E-0024：不从生产常量派生），行为那一半把上限临时改成 2 来跑（⛔ 不建 21 张单去凑那个数字）。"""
        self.assertEqual(jidanpu_openapi_consume.MAX_ORDERS_PER_ROUND, 20)
        first = self.order  # 基类已经造好并挂好链路的（ORDER_NO ↔ REMOTE_ID / 代练中）
        second = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, second, remote_id=OTHER_REMOTE_ID)
        self.reply_status()
        self.assertEqual(self.post_callback(), "200 OK")
        self.assertEqual(
            self.post_callback(params=self.status_params(order_no=OTHER_REMOTE_ID),
                               received_at=FIXED_2),
            "200 OK",
        )

        with mock.patch.object(jidanpu_openapi_consume, "MAX_ORDERS_PER_ROUND", 1):
            capped = self.consume(self.binding)

        self.assertEqual(capped.applied, 1)
        self.assertEqual(capped.failed, 1)
        self.assertEqual(self.order_status(first), "PAUSED")
        self.assertEqual(self.order_status(second), "IN_PROGRESS", "撞上限那一单被静默丢掉了")
        self.assertEqual(
            self.refresh(self.binding).callback_watermark_at,
            FIXED_2 - timedelta(microseconds=1),
            "水位应当停在「第一条没处理的」前一微秒（下一轮接着查）",
        )

        # 下一轮：上限之内 ⇒ 把剩下的那一单查完
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertEqual(self.order_status(second), "PAUSED")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)

    def test_a_file_that_is_bigger_but_all_older_is_still_treated_as_moved(self):
        """⭐ **「文件里最新一条比水位还旧」那一道判据单独也能抓住**（进程重启后 size 记忆为空时就只剩它）。场景：file 被换成**另一台机器上那份更长、但全部更旧**的事件文件（搬机器的真实现场）——`size` 变**大**了、`mtime` 也没倒退 ⇒ 前面那一道判据**不成立**，只有「最新一条比水位还旧」能发现它；若不发现，水位以下那些旧事件永远轮不到（静默丢）。
        ⛔ 弄红它：去掉 `rolled_back` 那一道判据 ⇒ 本用例第一条断言红。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.consume(self.binding)
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)

        # 换成「更长、但全部更旧」的一份：把第一条（真接收器写的）那一行复制成 21 行
        raw_lines = self.events.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(raw_lines), 2)
        before_size = self.events.stat().st_size
        self.events.write_text("\n".join([raw_lines[0]] * 21) + "\n", encoding="utf-8")
        os.utime(self.events, None)  # mtime = 现在（⛔ 不倒退）
        self.assertGreater(self.events.stat().st_size, before_size, "自证夹具：这一份更大")
        self.assertTrue(all(row["received_at"] <= FIXED_1.isoformat() for row in _read_lines(self.events)))

        outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))

        self.assertTrue(outcome.reset, "文件换成了更旧的一份，却没有判出「被搬动过」")
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at, "水位没有清零")
        self.assertEqual(outcome.mine, 0)

    def test_the_event_timestamp_never_steers_the_watermark(self):
        """⭐ **F19**：报文里的 `timestamp` **偏 +8 小时** ⇒ 一个字都不参与（水位用我们自己的钟）。
        ⛔ 弄红它：拿报文 `timestamp` 当水位 / 当「未来就丢弃」的判据 ⇒ 下面两条都会红（水位不等于 `FIXED_1`，或者这条事件被丢掉、订单没变）。"""
        # 报文时间戳 = 我们收到时刻 + 8 小时（正是实测那个偏移量），而且它是「未来」
        skewed = int(FIXED_1.timestamp()) + 8 * 3600
        self.assertEqual(
            self.post_callback(params=self.status_params(timestamp=str(skewed))), "200 OK",
        )

        self.consume(self.binding)

        self.assertEqual(self.order_status(self.order), "PAUSED", "偏 8 小时的时间戳把事件弄丢了")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_1)


# ══ ⑥′ 保留期清理（已消费 + 超 24h 的归档段由**消费侧**删掉）════════════


class SegmentRetentionConsumerTests(_ConsumeCase):
    """⭐ 承重：删段**不能让消费侧误判「文件被搬动」**（那会水位清零 + 重读一遍）。
    判据打在**绑定行**上（水位 / 已消费偏移），并用 `snapshot().high_water_bytes` / `total_bytes` 的**一平一降**自证「这一段真的被删掉了、而高水位没动」。"""

    #: 轮转阈值压到这么小 ⇒ 每条回调几乎各自成段（夹具要的是「有多段」，不是真实阈值）。
    ROTATE_AT = 600

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def _seed_rotated_and_consume(self):
        """造出「多个归档段 + 现役文件」，跑一轮消费，再把第一段变超期。返回删段前的读数。"""
        with mock.patch.object(callback_store, "ROTATE_BYTES", self.ROTATE_AT):
            for received_at in (FIXED_1, FIXED_1):
                self.assertEqual(self.post_callback(received_at=received_at), "200 OK")
        #: 最后一条**不轮转**地落在现役文件里（夹具的既有前置要求现役文件非空）。
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        segments = callback_store.segment_files(self.events)
        self.assertGreaterEqual(len(segments), 2, "自证失败：没轮转出归档段")
        self.assertGreater(segments[0][1].stat().st_size, 0, "自证失败：第一段是空的")
        first_round = self.consume(self.binding)
        self.assertFalse(first_round.reset, "第一轮就不该判「文件被搬动」")
        fresh = self.refresh(self.binding)
        stored = fresh.callback_file_offset
        self.assertGreaterEqual(
            stored, segments[1][0], "自证失败：第一段还没被消费完（这一轮不该删它）",
        )
        high_water = callback_store.snapshot(self.events).high_water_bytes
        #: 第一段「最后一笔写入」改成 25 小时前 ⇒ 已过保留期。
        stamp = time.time() - callback_store.SEGMENT_RETENTION_SECONDS - 3600
        os.utime(segments[0][1], (stamp, stamp))
        return segments, stored, high_water

    def test_a_pruned_segment_does_not_reset_the_watermark(self):
        """删段 ⇒ 现存字节变小，但**高水位不变** ⇒ 消费侧不触发「文件被搬动」清零。"""
        segments, stored, high_water = self._seed_rotated_and_consume()
        first_segment = segments[0][1]

        outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        self.assertFalse(first_segment.exists(), "已消费 + 超保留期的归档段没有被删掉")
        self.assertFalse(
            outcome.reset, "删段被误判成「文件被搬动 / 变短」⇒ 水位被清零、现存段重读一遍",
        )
        fresh = self.refresh(self.binding)
        self.assertEqual(fresh.callback_watermark_at, FIXED_2, "删段把水位弄丢了")
        self.assertEqual(fresh.callback_file_offset, stored, "删段把已消费偏移弄丢了")
        snap = callback_store.snapshot(self.events)
        self.assertEqual(snap.high_water_bytes, high_water, "高水位被清理弄小了")
        self.assertLess(
            snap.total_bytes, high_water, "自证失败：现存字节没变小（说明其实没删掉）",
        )

    def test_rows_appended_after_a_prune_are_consumed_exactly_once(self):
        """不丢行：删段之后新追加的行照常被读到，且**旧行不重复**（本轮只读到新那一条）。"""
        segments, _stored, _high_water = self._seed_rotated_and_consume()
        first_segment = segments[0][1]
        # 先跑一轮触发清理（没有新事件）。
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertFalse(first_segment.exists(), "自证失败：这一轮没触发清理（后面的判据测不到东西）")

        self.assertEqual(self.post_callback(received_at=FIXED_3), "200 OK")
        outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))

        self.assertFalse(outcome.reset, "删段之后追加新行，竟被判成「文件被搬动」")
        self.assertEqual(outcome.scanned, 1, "本轮应当只读到刚追加的那一条（旧行不许重复）")
        self.assertEqual(outcome.mine, 1)
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_3)

    def test_an_offset_inside_a_pruned_segment_is_a_loud_error_not_a_silent_reread(self):
        """偏移落在已被删掉的段里（另一份绑定落后 / 人工改错）⇒ **响亮报错**，⛔ 不静默从头读。"""
        self._seed_rotated_and_consume()
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        segments = callback_store.segment_files(self.events)
        #: 现存最早段之前的位置 = 已被删掉的范围。
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_file_offset=segments[0][0] - 1,
        )

        with self.assertRaises(callback_store.OffsetOutOfRetention):
            self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))

    def test_the_first_run_tail_read_never_drops_a_row_when_the_prefix_is_gone(self):
        """首轮大文件「只读末尾 N 字节」那一条：起点够到/早于现存最早段 ⇒ 从**段首**读，⛔ 不丢第一行。
        ⛔ 弄红它：把那一支的 `skip_partial` 一直设成 `True` ⇒ 段首那一条（完整行）会被当半行丢掉 ⇒ `scanned` 比现存行数少 1。"""
        self._seed_rotated_and_consume()
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        retained = len(list(callback_store.iter_lines(self.events)))
        self.assertGreaterEqual(retained, 1, "自证失败：删完之后一条都不剩，用例测不到东西")
        #: 模拟「列刚加上」：偏移空、水位还在。再让「末尾 N 字节」的窗口大于整个留存区
        #: ⇒ 起点会被夹到现存最早段的段首（这是要钉的那一支）。
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(callback_file_offset=0)
        with mock.patch.object(
            jidanpu_openapi_consume, "FIRST_RUN_TAIL_THRESHOLD", 1,
        ), mock.patch.object(
            jidanpu_openapi_consume, "FIRST_RUN_TAIL_BYTES", 8 * 1024 * 1024,
        ):
            outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))

        self.assertFalse(outcome.reset)
        self.assertEqual(outcome.scanned, retained, "从段首读起时丢掉了一条完整的行")

    # ── ⭐ N2 / R3-M2：安全下限 + 停删告警 + 兜底开关 ─────────────────────────

    def _lagging_upstream_binding(self, *, offset):
        """另一份**启用②式**上家绑定，已消费偏移停在 `offset`（落后）。"""
        binding = self.make_binding(name="lagging", code=CODE_OTHER)
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(
            callback_file_offset=offset,
        )
        return JidanpuUpstreamBinding.objects.get(pk=binding.pk)

    def _lagging_member_binding(self, *, offset):
        """另一份**启用②式**下家绑定（另一张表），已消费偏移停在 `offset`。"""
        #: ⚠️ 局部导入：本文件既有行号（尤其那条「偏移落在被删段仍响亮报错」的判据）不因
        #: 新增下家线的夹具而整体下移。
        from apps.business.models import JidanpuMemberBinding

        user = User.objects.create_user(
            username="member-lagging", password="pw-canary-lagging",
        )
        binding = JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=BASE_URL,
            account="acc-member-lagging",
            password="pw-canary-lagging",
            mode=jidanpu_openapi.OPENAPI_MODE,
            client_id=f"{CLIENT_ID}-member",
            client_secret=SECRET_OTHER,
            callback_code=CODE_OTHER,
            enabled=True,
        )
        JidanpuMemberBinding.objects.filter(pk=binding.pk).update(
            callback_file_offset=offset,
        )
        return JidanpuMemberBinding.objects.get(pk=binding.pk)

    def test_a_lagging_upstream_binding_holds_the_prune_floor(self):
        """判据③（上家表）：A 已消费到段尾、B 落后 ⇒ A 触发 prune 时**不删 B 还要读的段**。
        ⛔ 弄红它：把下限退回「只拿触发清理这一份的偏移」⇒ 第一段被删，B 下一轮直接撞 `OffsetOutOfRetention`（两条断言同时红）。"""
        segments, _stored, _high_water = self._seed_rotated_and_consume()
        lagging = self._lagging_upstream_binding(offset=segments[1][0] - 1)

        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        self.assertTrue(segments[0][1].exists(), "A 把 B 还要读的归档段删掉了")
        #: B 下一轮**正常读**它要读的范围（不再撞 OffsetOutOfRetention）。
        outcome = jidanpu_openapi_consume.consume_callbacks(
            self.refresh(lagging), now=ROUND_NOW + timedelta(minutes=2),
        )
        self.assertTrue(outcome.enabled)

    def test_a_lagging_member_line_binding_holds_the_prune_floor_too(self):
        """判据③（下家表）：落后的是**另一条线**的绑定 ⇒ 同样进下限（两张表都算）。"""
        #: ⚠️ 局部导入：保持本文件既有行号不整体下移（同 `_lagging_member_binding`）。
        from apps.business import jidanpu_member_openapi
        from apps.business.models import JidanpuMemberBinding

        segments, _stored, _high_water = self._seed_rotated_and_consume()
        lagging = self._lagging_member_binding(offset=segments[1][0] - 1)

        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        self.assertTrue(segments[0][1].exists(), "上家线把下家线绑定还要读的归档段删掉了")
        outcome = jidanpu_member_openapi.consume_member_callbacks(
            JidanpuMemberBinding.objects.get(pk=lagging.pk),
            now=ROUND_NOW + timedelta(minutes=2),
        )
        self.assertTrue(outcome.enabled)

    def test_the_auto_skip_switch_is_off_by_default_and_records_the_lost_range_when_on(self):
        """判据⑤：默认**关**（仍响亮抛）；显式打开后跳过，并把丢失区间写进 findings。
        ⛔ 弄红它：把开关做成默认开 ⇒ 前半段 `assertRaises` 红；打开后不留痕 ⇒ 后半段的「区间 / 字节数」断言红。"""
        self._seed_rotated_and_consume()
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        remaining = callback_store.segment_files(self.events)
        self.assertTrue(remaining, "自证失败：一条归档段都不剩，测不到「起点早于现存最早段」")
        earliest = remaining[0][0]
        lost_from = earliest - 7
        #: 模拟「偏移落在已删范围」：水位也退到所有留存事件之前（落后的绑定没见过它们）。
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_file_offset=lost_from,
            callback_watermark_at=FIXED_1 - timedelta(seconds=1),
        )

        #: ① 默认关 ⇒ 照旧响亮报错（与既有那条判据同一形状）。
        with mock.patch.dict(
            os.environ, {jidanpu_openapi_consume.ENV_AUTO_SKIP_OUT_OF_RETENTION: ""},
        ):
            with self.assertRaises(callback_store.OffsetOutOfRetention):
                self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))

        #: ② 打开 ⇒ 跳到现存最早段，丢失区间进 `findings`（⇒ 绑定 `last_error`）。
        with mock.patch.dict(
            os.environ, {jidanpu_openapi_consume.ENV_AUTO_SKIP_OUT_OF_RETENTION: "1"},
        ):
            outcome = self.consume(
                self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=3),
            )

        alert = outcome.alert
        self.assertIn(str(lost_from), alert, alert)
        self.assertIn(str(earliest), alert, alert)
        self.assertIn(str(earliest - lost_from), alert, alert)
        self.assertIn("字节", alert, alert)
        self.assertGreaterEqual(
            self.refresh(self.binding).callback_file_offset, earliest,
            "跳过之后偏移没有推进到现存最早段（下一轮还会撞同一处）",
        )

    def test_holding_the_floor_warns_once_per_bucket_and_names_the_lagging_binding(self):
        """判据⑥：停删要有人知道 ⇒ warning + finding 点名 B；**同一档 1 小时一次**。
        ⛔ 弄红它：去掉限频 ⇒ 第二轮又追加一条 finding / 又告警；去掉点名 ⇒ 断言里找不到 `#pk`。"""
        segments, stored, _high_water = self._seed_rotated_and_consume()
        lagging = self._lagging_upstream_binding(offset=segments[1][0] - 1)
        floor = lagging.callback_file_offset

        with mock.patch.object(jidanpu_openapi_consume.logger, "warning") as warning:
            first = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertTrue(segments[0][1].exists(), "自证失败：落后绑定没拖住清理，测不到告警")
        floor_findings = [text for text in first.findings if f"#{lagging.pk}" in text]
        self.assertEqual(len(floor_findings), 1, first.findings)
        message = floor_findings[0]
        self.assertIn(str(floor), message)
        self.assertIn(str(stored), message)
        #: warning 与 finding 是**同一句话**（出口两条，内容一处）。
        self.assertIn(message, [call.args[0] for call in warning.call_args_list])

        #: 同一进程、1 小时内、同一档 ⇒ 不再告警、也不再追加 finding。
        with mock.patch.object(jidanpu_openapi_consume.logger, "warning") as warning_again:
            second = self.consume(
                self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=30),
            )
        self.assertFalse(warning_again.called, "限频失效：同一档 30 分钟内又告警了")
        self.assertFalse([text for text in second.findings if f"#{lagging.pk}" in text])

    def test_after_the_lagging_binding_catches_up_the_expired_segment_is_really_deleted(self):
        """判据⑦：落后绑定**追上之后**真的删（⛔ 不是变成永久停删）。"""
        segments, stored, _high_water = self._seed_rotated_and_consume()
        lagging = self._lagging_upstream_binding(offset=segments[1][0] - 1)
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))
        self.assertTrue(segments[0][1].exists(), "自证失败：第一轮就没停删，测不到「追上后删」")

        #: 模拟 B 追上 A（偏移推到同一个已消费位置）。
        JidanpuUpstreamBinding.objects.filter(pk=lagging.pk).update(
            callback_file_offset=stored,
        )
        with mock.patch.object(jidanpu_openapi_consume.logger, "warning") as warning:
            outcome = self.consume(
                self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2),
            )

        self.assertFalse(segments[0][1].exists(), "落后绑定追上之后，超期段仍没被删（永久停删）")
        self.assertFalse([text for text in outcome.findings if f"#{lagging.pk}" in text])
        self.assertFalse(warning.called, "已经没人拖住下限了，却还在告警")


# ══ ⑦ 认不出来的状态（spec §6.4：⛔ 不改单、⛔ 不猜、要留痕）════════════════════


class UnknownStatusTests(_ConsumeCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)

    def test_an_unknown_authoritative_status_never_writes_and_is_registered(self):
        """⭐ 权威值是认不出来的 ⇒ ⛔ 不改单（尤其⛔ 不许变成问题单）+ 一条待人工核对的台账。
        ⛔ 弄红它：照登录式底表把认不出来的值兜底成 `ISSUE` ⇒「订单没变」那条断言红。"""
        self.reply_status(status=UNKNOWN_TOKEN)
        self.assertEqual(self.post_callback(), "200 OK")

        outcome = self.consume(self.binding)

        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        self.assertNotEqual(self.order_status(self.order), "ISSUE")
        rows = self.notes(kind="openapi.unmapped_status")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.status, "failed")
        self.assertIn(UNKNOWN_TOKEN, row.reason)
        # 四件事实（T7 的 payload 形状）：远端原值 / 我方单号 / 绑定 id / 查单时刻
        self.assertEqual(row.payload["remote_status"], UNKNOWN_TOKEN)
        self.assertEqual(row.payload["order_no"], ORDER_NO)
        self.assertEqual(row.payload["upstream_binding_id"], self.binding.pk)
        self.assertEqual(row.payload["queried_at"], ROUND_NOW.isoformat())
        # 要人看见的那一句话（调度层把它盖进绑定 `last_error`；见下面接线那一组用例）
        self.assertIn(UNKNOWN_TOKEN, outcome.alert)
        self.assertEqual(outcome.failed, 1)

    def test_the_command_leaves_the_consumer_alert_on_the_binding(self):
        """⭐ 留痕要**能被人看见**：跑一轮命令 ⇒ 台账一行 **且** 绑定 `last_error` 有那句话。
        ⛔ 弄红它：把命令里那次 `_stamp_binding_error` 删掉 ⇒ `last_error` 是空串（页面上看不见）。⚠️ 顺序也是判据：健康列那一段会清 `last_error`，所以那一句必须留在它**之后**。"""
        self.reply_status(status=UNKNOWN_TOKEN)
        self.assertEqual(self.post_callback(), "200 OK")

        out, err = self.run_command()

        fresh = self.refresh(self.binding)
        self.assertIn(UNKNOWN_TOKEN, fresh.last_error, "要人看见的那句话没有落到绑定上")
        self.assertIsNotNone(fresh.last_error_at)
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        self.assertIn("回调", out)

    # ── 命令级跑一轮（照 `test_jidanpu_upstream_schedule` 的桩法：只换租户遍历与三段读路）──

    def run_command(self):
        from apps.business.management.commands import jidanpu_sync as module

        tenant = mock.Mock(schema_name="tenant-consume")
        out, err = StringIO(), StringIO()

        @contextmanager
        def _noop_context(_tenant):
            yield

        patcher = mock.patch.object(module.Tenant, "objects")
        objects = patcher.start()
        objects.exclude.return_value.order_by.return_value = [tenant]
        try:
            with mock.patch.object(module, "tenant_context", _noop_context), \
                 mock.patch.object(module.timezone, "now", return_value=ROUND_NOW), \
                 mock.patch.object(
                     jidanpu_openapi_consume.jidanpu_upstream, "run_outbox",
                     return_value=simple_outbox(),
                 ), \
                 mock.patch.object(
                     jidanpu_openapi_consume.jidanpu_upstream, "sync_hall",
                     return_value=simple_hall(),
                 ), \
                 mock.patch.object(
                     jidanpu_openapi_consume.jidanpu_upstream, "sync_my_orders",
                     return_value=simple_orders(),
                 ), \
                 mock.patch.object(module.jidanpu, "open_client_with_http", _stub_connection):
                call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
        finally:
            patcher.stop()
        return out.getvalue(), err.getvalue()


#: 三段读路的**空结论**（接线用例只关心「这一段被调到了」，不关心它读到了什么）。
def simple_outbox():
    return OutboxStats()


def simple_hall():
    return HallOutcome()


def simple_orders():
    return MyOrdersOutcome()


@contextmanager
def _stub_connection(_binding):
    """`open_client_with_http` 的替身：产出 `(client, http)`，⛔ 一条真网络都不建。"""
    yield (mock.Mock(), mock.Mock())


# ══ ⑧ E-0022：atomic 里的补偿路径 ════════════════════════════════════════════


class AtomicityTests(_ConsumeCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_a_failure_after_the_state_write_rolls_the_whole_block_back(self):
        """⭐ **E-0022**：块内**后面的**写入失败 ⇒ 状态那一笔也必须回滚（⛔ 不留半态）。
        ⛔ 弄红它：把 `_write_status` 的 `transaction.atomic()` 去掉（或把块内的失败**接住**）⇒ 订单会留在「已暂停」而审计没写成——那就是「前面几次写入被提交」的脏行。
        ⚠️ 同时钉「不可预期的失败要**响亮**」：异常必须**冒出去**（⛔ 不伪装成业务结论），而且水位不前移（下一轮重试这一条）。"""
        self.assertEqual(self.post_callback(), "200 OK")

        def _boom(**kwargs):
            raise RuntimeError("synthetic audit failure mid-block")

        with mock.patch.object(jidanpu_openapi_consume, "record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.consume(self.binding)

        self.assertEqual(self.order_status(self.order), "IN_PROGRESS", "留下了一笔半截账")
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 0)
        self.assertEqual(self.notes().count(), 0, "写没成功却先记了台账")
        self.assertIsNone(self.refresh(self.binding).callback_watermark_at, "失败却前移了水位")

    def test_a_predictable_denial_rolls_back_and_keeps_the_batch_going(self):
        """⭐ **E-0022 的另一半**：可预期的拒绝 ⇒ `set_rollback(True)` **再**退出。
        ⛔ 弄红它：把 `transaction.set_rollback(True)` 删掉 ⇒ 桩在块内写下的那一行会**活下来**（`marker` 断言红）——那正是「块内正常 return 会把前面的写入提交」的现场。
        ⚠️ 桩模拟的是「块内已经写了一笔、随后被状态机拒绝」这个形状（用真的 `IllegalTransition`）。"""
        from apps.business.order_status import IllegalTransition

        self.assertEqual(self.post_callback(), "200 OK")

        def _writing_but_denied(order, target, **kwargs):
            JidanpuSyncEvent.objects.create(
                direction="in", kind="test.marker_inside_atomic", target_type="order",
                target_ref=order.order_no, status="done", reason="块内已经写了一笔",
            )
            raise IllegalTransition("非法状态迁移：合成拒绝")

        with mock.patch.object(
            jidanpu_openapi_consume.jidanpu_upstream, "apply_transition",
            side_effect=_writing_but_denied,
        ):
            outcome = self.consume(self.binding)

        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="test.marker_inside_atomic").count(), 0,
            "块内那一笔活下来了 ⇒ 半态（set_rollback 没生效）",
        )
        self.assertEqual(self.order_status(self.order), "IN_PROGRESS")
        denied = self.notes(kind="openapi.status_in").filter(status="failed")
        self.assertEqual(denied.count(), 1)
        self.assertIn("合成拒绝", denied.get().reason)
        self.assertEqual(outcome.failed, 1)


# ══ ⑨ 接线：跑在 outbox **之前** ═════════════════════════════════════════════


class WiringTests(_ConsumeCase):
    def test_the_consumer_runs_before_the_outbox(self):
        """⭐ 顺序是**承重**的（spec §5.5）：先把对方推来的事实收敛掉，再决定要不要往外推。⛔ 弄红它：把消费那一段挪到 outbox **之后** ⇒ 调用顺序反转，本用例红。
        ⚠️ **入口闸改成按方式分之后**的夹具改法（如实登记）：②式那一轮**不跑 outbox**（写接口第一期不做，spec §15-N3）⇒「消费在 outbox 之前」这一对段序**今天在任何一行上都不同时成立**（①式不跑消费、②式不跑 outbox）；这条判据是给**第二期**（②式有写接口那天）留的契约 ⇒ 这里**把这一行当成①式来驱动段序**（`is_openapi_binding` 换成 `False`），而不是把断言删掉/放宽——⛔ 删掉它等于「第二期一接写接口就没人守这条顺序了」。
        ⚠️ 夹具那一行的 `password` 非空（`_ConsumeCase.make_binding` 的既有形状）⇒ 当成①式时**过得了①式的凭据闸**（⛔ 不是靠放宽闸让它进来）。"""
        trace: list[str] = []

        def _consume(binding, **kwargs):
            trace.append("consume")
            return jidanpu_openapi_consume.ConsumeOutcome(enabled=True)

        def _outbox(binding, **kwargs):
            trace.append("outbox")
            return simple_outbox()

        from apps.business.management.commands import jidanpu_sync as module

        tenant = mock.Mock(schema_name="tenant-consume-order")

        @contextmanager
        def _noop_context(_tenant):
            yield

        patcher = mock.patch.object(module.Tenant, "objects")
        objects = patcher.start()
        objects.exclude.return_value.order_by.return_value = [tenant]
        try:
            with mock.patch.object(module, "tenant_context", _noop_context), \
                 mock.patch.object(module.timezone, "now", return_value=ROUND_NOW), \
                 mock.patch.object(
                     jidanpu_openapi_consume, "consume_callbacks", side_effect=_consume,
                 ), \
                 mock.patch.object(
                     jidanpu_openapi_consume.jidanpu_upstream, "run_outbox", side_effect=_outbox,
                 ), \
                 mock.patch.object(
                     jidanpu_openapi_consume.jidanpu_upstream, "sync_hall",
                     return_value=simple_hall(),
                 ), \
                 mock.patch.object(
                     jidanpu_openapi_consume.jidanpu_upstream, "sync_my_orders",
                     return_value=simple_orders(),
                 ), \
                 mock.patch.object(
                     module.jidanpu_openapi, "is_openapi_binding", return_value=False,
                 ), \
                 mock.patch.object(module.jidanpu, "open_client_with_http", _stub_connection):
                call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())
        finally:
            patcher.stop()

        self.assertEqual(trace, ["consume", "outbox"])

    def test_a_password_mode_binding_never_reads_the_event_file(self):
        """①式绑定（`mode="password"`）⇒ 消费者**一次文件都不读**（零成本，spec §5.5）。
        ⚠️ 判据打在「文件有没有被打开过」上（把读口换成一个会抛的替身），⛔ 不是打在返回值上。
        ⚠️ 读口是**增量读**（`_scan` → `store.read_records_from`，`open("rb")+seek`）⇒ 替身必须打在新的那个口上（打在 `iter_lines` 上已经拦不住任何东西 = 恒真断言）。"""
        binding = self.make_binding(code="", name="password-mode")
        binding.mode = "password"
        binding.callback_code = ""
        binding.save(update_fields=["mode", "callback_code"])

        def _boom(*args, **kwargs):
            raise AssertionError("①式绑定竟然去读了回调事件文件")

        with mock.patch.object(jidanpu_openapi_consume, "_scan", _boom):
            outcome = jidanpu_openapi_consume.consume_callbacks(binding, now=ROUND_NOW)

        self.assertFalse(outcome.enabled)
        self.assertEqual(outcome.text, "")


# ══ ⑩ 哨兵：事件文件与命令输出里不许出现密钥 ══════════════════════════════════


class SecretHygieneTests(_ConsumeCase):
    def test_no_secret_ever_reaches_the_journal_or_the_output(self):
        """⭐ 消费者**不读密钥**：它跑完之后，事件文件与台账里都搜不到那把哨兵密钥。
        ⚠️ 用**哨兵**（`tests.test_jidanpu_openapi.SENTINEL_SECRET`）当判据：它是合成值，只允许出现在内存里（接收器 spec 的同款用例钉的是接收器那一侧，这里钉消费者这一侧）。"""
        # 让这个绑定的 `client_secret` **就是**哨兵：接收器那一侧也要用同一把（否则验签不过）
        self.keys_file.write_text(
            KEYS_HEADER + "\n" + f"{CODE_MINE}\t{SENTINEL_SECRET}\n", encoding="utf-8",
        )
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            client_secret=SENTINEL_SECRET,
        )
        binding = self.refresh(self.binding)
        order = self.make_order()
        self.make_link(binding, order)
        self.reply_status()
        self.assertEqual(self.post_callback(secret=SENTINEL_SECRET), "200 OK")

        outcome = self.consume(binding)

        self.assertNotIn(SENTINEL_SECRET, _row_text(self.events))
        for row in self.notes():
            self.assertNotIn(SENTINEL_SECRET, row.reason)
            self.assertNotIn(SENTINEL_SECRET, json.dumps(row.payload, ensure_ascii=False))
        self.assertNotIn(SENTINEL_SECRET, outcome.alert)
        self.assertNotIn(SENTINEL_SECRET, outcome.text)
        # 阳性对照：这一轮真的写成功了（否则上面的断言可能只是「什么都没跑」）
        self.assertEqual(self.order_status(order), "PAUSED")


# ══ ⑪ 两条「看得见的跳过」═════════════════════════════════
# 两条都是**非阻塞项**的修复：水位那条口径的已知边界（乱序的一条）与「不是本线路径的行」。
# 口径一句话：**⛔ 都不改水位算法、⛔ 都不改「写状态」那一段**，只把两种静默形态变成台账上一行字。


class VisibleSkipTests(_ConsumeCase):
    """乱序那一条（②）与不是本线路径的行（③）各自要留痕，而且**⛔ 都不许刷屏**。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_an_out_of_order_event_is_a_visible_skip_not_a_silent_drop(self):
        """⭐ ② 一条**没排在水位之后**、却是**上一轮之后**才到的回调 ⇒ 留一条「看得见的跳过」。⛔ 弄红它：把 `consume_callbacks` 里 `_note_stale_skip` 那一次调用（含它上面那个 `elif`）去掉 ⇒ 台账 0 行（本用例第一条断言红）。
        ⚠️ 场景：接收器的钟被 NTP **回拨**（或将来的**双写进程**）⇒ 后到的那一条带着**比水位还旧**的 `received_at`；它在现有口径下**永远进不了 `pending`**、一个字都不改单，而「整份文件最新一条比水位还旧」那条兜底也**不成立**（文件最新一条仍是 FIXED_2）。
        ⚠️ 判据三条各自承重：① 台账**真的有一行**（⛔ 不是静默丢）；② 人话里**逐字**带着那一条的 `received_at` 与当时的水位；③ ⛔ **不改水位算法**：它照旧不进 `pending`、水位照旧不动。"""
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        self.consume(self.binding)
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)
        self.assertEqual(self.order_status(self.order), "PAUSED")
        before_notes = self.notes().count()

        # 乱序：这一条**后到**，却带着一个比水位还旧的 received_at（钟被回拨）
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")

        outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        rows = self.notes(kind=jidanpu_openapi_consume.KIND_STALE_SKIPPED)
        self.assertEqual(rows.count(), 1, "乱序的那一条被静默丢了：台账一行都没有")
        row = rows.get()
        self.assertEqual(row.status, "failed")
        self.assertEqual((row.target_type, row.direction), ("callback", "in"))
        # D-0302：台账人话里的两个时刻走 `local_iso`（**项目时区 +08:00**，
        # 旧写法主动 `astimezone(utc)` 出 UTC 墙钟、页面上差 8 小时）。
        self.assertIn(local_iso(FIXED_1), row.reason, "人话里没有那一条的 received_at（逐字）")
        self.assertIn(local_iso(FIXED_2), row.reason, "人话里没有当时的水位（逐字）")
        self.assertEqual(row.payload["received_at"], local_iso(FIXED_1))
        self.assertEqual(row.payload["watermark"], local_iso(FIXED_2))
        self.assertEqual(row.payload["upstream_binding_id"], self.binding.pk)
        # 「看得见」= 这句话也进这一轮的 findings（⇒ 绑定 `last_error`，页面上看得见）
        self.assertIn("看得见的跳过", outcome.alert)
        # ⛔ 水位算法一个字都没改：不进 pending、不为它打上游、水位与订单都不动
        self.assertEqual(outcome.mine, 0)
        self.assertEqual(len(self.upstream.api_calls()), 1, "为一条被水位挡住的事件打了上游")
        self.assertEqual(self.refresh(self.binding).callback_watermark_at, FIXED_2)
        self.assertEqual(self.order_status(self.order), "PAUSED")

        # ⛔ 不刷屏：再跑一轮还是那一条（⛔ 不是每轮灌一条）
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2))
        self.assertEqual(self.notes(kind=jidanpu_openapi_consume.KIND_STALE_SKIPPED).count(), 1)
        self.assertEqual(self.notes().count(), before_notes + 1, "第二轮又灌了台账")

    def test_a_row_that_is_not_this_lines_path_is_visible_and_never_spams(self):
        """⭐ ③ 事件文件里**不是本线路径**的行 ⇒ 留一条台账（⛔ 不静默），但**按形状只记一条**。⛔ 弄红它：把 `_path_code(row) is None` 那一支改回裸 `continue` ⇒ 台账 0 行（本用例红）。
        ⚠️ **为什么必须留**：接收器那边的路径规则一变（前缀改了 / 定长改了 / 中间多一段路由），本绑定的回调会**整批**从这一支流过去 ⇒ 现场是「回调一条都没来、台账一行都没有」（本仓最恨的静默形态，比报错贵得多）；⚠️ **为什么不刷屏**：旧路径（①式登录线）的行**每一条**都会落在这里 ⇒ 判据按**形状**去重，⛔ 不是逐行、也不是每轮；本用例连跑两轮，钉住「跑两轮不许翻倍」。
        ⚠️ 那两行是**手写**的（真接收器不会落盘它们：`/jidanpu/v1/notify` 走旧路、形状不合法回 404 且不落盘），形状照 `wsgi._new_record` 抄——与本文件既有那条「23 位码」用例同一个手法。"""
        self.assertEqual(self.post_callback(), "200 OK")
        raw = dict(_read_lines(self.events)[0])
        foreign_paths = (
            callback_wsgi.NOTIFY_PATH,  # 旧路径：①式登录线的行（**每一条**都落在这里）
            f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_MINE}X",  # 形状不对：码多一个字符
        )
        for foreign_path in foreign_paths:
            row = dict(raw)
            row["path"] = foreign_path
            with self.events.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.assertEqual(len(_read_lines(self.events)), 3, "自证夹具：两行异路径确实进了文件")

        outcome = self.consume(self.binding)
        self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        rows = self.notes(kind=jidanpu_openapi_consume.KIND_FOREIGN_PATH)
        self.assertEqual(rows.count(), 2, "两个形状各应恰好一条台账（跑两轮不许翻倍）")
        by_shape = {row.payload["shape"]: row for row in rows}
        legacy = by_shape[jidanpu_openapi_consume.FOREIGN_SHAPE_LEGACY]
        # 旧路径那一档是**正常**的（它是①式登录线自己的行）；形状不对那一档**要人看**（可能意味着规则变了）
        self.assertEqual(legacy.status, "skipped")
        self.assertEqual(legacy.payload["sample_path"], callback_wsgi.NOTIFY_PATH)
        bad_shape = next(
            row for shape, row in by_shape.items()
            if shape != jidanpu_openapi_consume.FOREIGN_SHAPE_LEGACY
        )
        self.assertEqual(bad_shape.status, "failed")
        self.assertTrue(bad_shape.payload["sample_path"].startswith(callback_wsgi.CALLBACK_PATH_PREFIX))
        for row in rows:
            self.assertIn("不是本线路径", row.reason)
            self.assertEqual(row.target_type, "callback")
            self.assertEqual(row.payload["upstream_binding_id"], self.binding.pk)
        # ⛔ 异路径的行不许进 pending（本线不处理别人的事）：本绑定那一条照旧被处理，上游只打一次
        self.assertEqual(outcome.mine, 1)
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertEqual(len(self.upstream.api_calls()), 1)
        # ⭐⭐⭐ **两档分得开**（业务方裁定；引擎是两条线共用的一份）：
        # 「要人看」的那一档（新前缀下码的形状不合法 ⇒ 可能是接收器的路径规则变了）**进**错误列；
        # 旧路径那一档是**另一条线的正常流量**（每 3 秒都在落）⇒ ⛔ **不进**（进了就是错误列说假话）。
        self.assertIn("不是本线路径", outcome.alert)
        self.assertNotIn(jidanpu_openapi_consume.FOREIGN_SHAPE_LEGACY, outcome.alert)


# ══ ⑦″ 增量读：按**字节偏移**只读新增的行（生产性能缺陷）══════════════════


class IncrementalOffsetTests(_ConsumeCase):
    """⭐ 每轮只从「已消费到的字节偏移」往后读（⛔ 不再把整份留档从头扫一遍、全部解析进内存）。
    真机现场（kexing）：事件文件 198MB / 十几万行 ⇒ 旧写法**每轮**全扫＋全部解析 ⇒ 一轮跑很久、水位几乎不前进（进程有 CPU、没日志）。
    这一组用例的判据**全部打在读数上**（`outcome.scanned` = 本轮真的解析了几条 ＋ 库里 `callback_file_offset` 的值），⛔ 不打 logger、⛔ 不看耗时（耗时在用例机上没有意义）。"""

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.make_link(self.binding, self.order)
        self.reply_status()

    def test_the_second_round_only_reads_the_appended_tail(self):
        """⭐ 第二轮**只读新增那一行**（⛔ 不重读上一轮那份历史）。
        ⛔ 弄红它：让引擎每轮都 `_scan` 整份文件（旧写法）⇒ 第二轮 `scanned=2`，第一条断言红。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        first = self.consume(self.binding)
        self.assertEqual(first.scanned, 1, "首轮小文件应当整份读（默认行为不变）")

        second_order = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, second_order, remote_id=OTHER_REMOTE_ID)
        self.assertEqual(
            self.post_callback(
                params=self.status_params(order_no=OTHER_REMOTE_ID), received_at=FIXED_2,
            ),
            "200 OK",
        )

        second = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        self.assertEqual(
            second.scanned, 1,
            "第二轮把上一轮那份历史又读了一遍（应当只读已消费偏移之后新增的那一条）",
        )
        self.assertEqual(second.mine, 1)
        self.assertEqual(self.order_status(second_order), "PAUSED")
        self.assertEqual(
            self.refresh(self.binding).callback_file_offset,
            self.events.stat().st_size,
            "这一轮跑完后已消费偏移没有停在文件末尾",
        )

    def test_the_offset_is_persisted_and_survives_a_process_restart(self):
        """⭐ 偏移**落库**、跨轮（含进程重启）生效：清空进程内记忆后仍只读新增行。
        ⛔ 弄红它：把偏移只放在 `_FILE_STATE`（进程内）⇒ 重启后从 0 重读整份文件，第二轮 `scanned` 变成 2。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.consume(self.binding)
        persisted = self.refresh(self.binding).callback_file_offset
        self.assertEqual(
            persisted, self.events.stat().st_size,
            "已消费偏移没有落库（或没停在最后一条完整行的末尾）",
        )

        # 进程重启：`_FILE_STATE` 是进程内记忆 ⇒ 清空；偏移必须还在库里
        jidanpu_openapi_consume.reset_process_state()

        second_order = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, second_order, remote_id=OTHER_REMOTE_ID)
        self.assertEqual(
            self.post_callback(
                params=self.status_params(order_no=OTHER_REMOTE_ID), received_at=FIXED_2,
            ),
            "200 OK",
        )
        outcome = self.consume(self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1))

        self.assertEqual(outcome.scanned, 1, "重启后把历史整份重读了一遍（偏移没有跨轮生效）")
        self.assertEqual(outcome.mine, 1)
        self.assertEqual(self.order_status(second_order), "PAUSED")
        self.assertEqual(self.refresh(self.binding).callback_file_offset, self.events.stat().st_size)

    def test_a_shrunk_file_resets_the_offset_and_the_watermark(self):
        """⭐ 文件变短 / 被搬动过 ⇒ **水位和已消费偏移一起清零** + 响亮留痕（⛔ 不静默）。
        ⛔ 弄红它：只清水位、不清偏移 ⇒ 下一轮从「旧文件的偏移」起读一份更短的新文件，那一行永远轮不到（`callback_file_offset` 不是 0 ⇒ 第一条断言红）。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        self.consume(self.binding)
        before = self.refresh(self.binding)
        self.assertGreater(before.callback_file_offset, 0, "自证夹具：第一轮之后偏移应当 > 0")
        self.assertEqual(before.callback_watermark_at, FIXED_2)

        # 换一份**更短的**（模拟「整块搬走 / 换了一台机器上的旧副本」）
        self.events.write_text(
            json.dumps(_read_lines(self.events)[0], ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        outcome = self.consume(before)

        self.assertTrue(outcome.reset, "文件变短了却没有判出「被搬动过」")
        fresh = self.refresh(self.binding)
        self.assertIsNone(fresh.callback_watermark_at, "水位没有清零")
        self.assertEqual(fresh.callback_file_offset, 0, "文件变短了，已消费偏移没有清零")
        self.assertIn("水位", outcome.alert)

    def test_the_first_round_on_a_large_file_reads_only_the_tail(self):
        """⭐ 首轮大文件（偏移 = 0 但水位非空 且 > 32MB）⇒ 只读末尾一段 + 留一条人话。
        ⛔ 弄红它：去掉「大文件只读末尾」这一支 ⇒ 首轮把 101 条全解析一遍（`scanned=101`），第二条断言红；而且现场那一幕（198MB 每轮全扫）照旧。
        ⚠️ 文件用 **100 条合法但比水位还旧的记录**撑到 40MB（每条 ~400KB）：整份解析是 101 条、只读末尾约 16MB 是 40 几条 ⇒ `scanned` 的大小就是判据。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_watermark_at=FIXED_1,
        )
        self.events.parent.mkdir(parents=True, exist_ok=True)
        pad = json.dumps(
            {
                "received_at": FIXED_1.isoformat(),
                "path": f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_MINE}",
                "pad": "x" * 400_000,
            },
            ensure_ascii=False, separators=(",", ":"),
        ) + "\n"
        with self.events.open("wb") as handle:
            for _ in range(100):
                handle.write(pad.encode("utf-8"))
        self.assertGreater(
            self.events.stat().st_size, 32 * 1024 * 1024, "自证夹具：文件没有超过 32MB",
        )
        self.assertEqual(self.post_callback(received_at=FIXED_2), "200 OK")
        self.assertEqual(len(_read_lines(self.events)), 101, "自证夹具：文件里确实有 101 条合法记录")

        outcome = self.consume(self.refresh(self.binding))

        self.assertEqual(outcome.mine, 1)
        self.assertEqual(self.order_status(self.order), "PAUSED")
        self.assertLess(
            outcome.scanned, 101,
            "首轮大文件仍然整份全扫（应当只读末尾 16MB 那一段）",
        )
        self.assertIn("历史回调按偏移跳过（文件过大）", outcome.alert, "跳过历史这件事没有留一句人话")
        self.assertEqual(
            self.refresh(self.binding).callback_file_offset,
            self.events.stat().st_size,
        )

    def test_a_broken_line_keeps_the_offset_before_it_and_the_next_round_rereads(self):
        """⭐ 坏行 ⇒ 偏移**停在坏行之前**、下一轮从同一位置重读（⛔ 不许跳过去）。
        ⛔ 弄红它：遇到坏行照样把偏移推到文件末尾 ⇒ 第三轮不会重读那一行，坏行后面那条真事件永远轮不到（订单仍是「代练中」）。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.consume(self.binding)
        offset_before = self.refresh(self.binding).callback_file_offset
        self.assertGreater(offset_before, 0, "自证夹具：第一轮之后偏移应当 > 0")

        # 坏行 + 它后面的一条**真**事件（两条都排在已消费偏移之后）
        with self.events.open("ab") as handle:
            handle.write(b"this-is-not-json\n")
        second_order = self.make_order(order_no=ORDER_NO_B)
        self.make_link(self.binding, second_order, remote_id=OTHER_REMOTE_ID)
        self.assertEqual(
            self.post_callback(
                params=self.status_params(order_no=OTHER_REMOTE_ID), received_at=FIXED_2,
            ),
            "200 OK",
        )

        outcome = jidanpu_openapi_consume.consume_callbacks(
            self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1),
        )

        self.assertEqual(outcome.scanned, 0, "坏行之前的记录早就消费过了，本轮不该解析出任何一条")
        self.assertIn("读不出来", outcome.alert, "坏行没有留痕（⛔ 不许静默）")
        self.assertEqual(
            self.refresh(self.binding).callback_file_offset, offset_before,
            "坏行的偏移被写进去了（下一轮就不会重读这一行）",
        )
        self.assertEqual(self.order_status(second_order), "IN_PROGRESS", "坏行后面那条被静默处理了")

        # 下一轮：把那一行**补成一条合法记录**（长度变了也没关系）⇒ 从同一偏移重读
        fixed = json.dumps(
            {
                "received_at": FIXED_2.isoformat(),
                "path": callback_wsgi.NOTIFY_PATH,
                "sign_ok": False,
                "sign_reason": "synthetic",
            },
            ensure_ascii=False, separators=(",", ":"),
        ) + "\n"
        raw = self.events.read_bytes()
        self.events.write_bytes(raw.replace(b"this-is-not-json\n", fixed.encode("utf-8")))

        after = jidanpu_openapi_consume.consume_callbacks(
            self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2),
        )

        self.assertEqual(after.scanned, 2, "下一轮没有从坏行那个位置重读（修好的那一行 + 后面那条）")
        self.assertEqual(after.mine, 1)
        self.assertEqual(self.order_status(second_order), "PAUSED")
        self.assertEqual(self.refresh(self.binding).callback_file_offset, self.events.stat().st_size)

    def test_a_half_written_last_line_keeps_the_offset_before_it(self):
        """⭐ 半行（接收器正在写、没有换行结尾）⇒ 偏移停在它之前，写完之后下一轮读得到。
        ⛔ 弄红它：把「没有换行结尾的半行」也当成一条完整记录 / 把偏移推到 EOF ⇒ 第二条断言（偏移没动）与第三轮（读到那一条）都会红。"""
        self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
        self.consume(self.binding)
        offset_before = self.refresh(self.binding).callback_file_offset

        whole = json.dumps(
            {
                "received_at": FIXED_2.isoformat(),
                "path": callback_wsgi.NOTIFY_PATH,
                "sign_ok": False,
                "sign_reason": "synthetic-half-line",
            },
            ensure_ascii=False, separators=(",", ":"),
        ) + "\n"
        split = 40
        with self.events.open("ab") as handle:
            handle.write(whole[:split].encode("utf-8"))

        outcome = jidanpu_openapi_consume.consume_callbacks(
            self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1),
        )

        self.assertEqual(outcome.scanned, 0)
        self.assertIn("读不出来", outcome.alert, "半行没有留痕（⛔ 不许静默）")
        self.assertEqual(
            self.refresh(self.binding).callback_file_offset, offset_before,
            "半行的偏移被写进去了（下一轮读不到这一条）",
        )

        # 接收器把这一行写完了 ⇒ 下一轮从同一偏移重读，把它完整读进来
        with self.events.open("ab") as handle:
            handle.write(whole[split:].encode("utf-8"))
        after = jidanpu_openapi_consume.consume_callbacks(
            self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=2),
        )

        self.assertEqual(after.scanned, 1, "写完之后下一轮没有读到那一条")
        self.assertEqual(self.refresh(self.binding).callback_file_offset, self.events.stat().st_size)

    def test_rotation_never_looks_like_a_shrunk_file_and_reads_across_segments(self):
        """⭐ 轮转（H-1）**不会被误判成「文件被搬动过」**：全局尺寸单调不减 + 跨段续读。⛔ 弄红它：把消费侧第 ① 步的 `snap = callback_store.snapshot(path)` 换回 `self.events.stat().st_size`（只看现役文件，旧写法）⇒ 第一次轮转之后现役文件变空，第二轮按「变小」判成换过文件 ⇒ `reset=True`、水位与偏移一起清零 ⇒ 下面三条全红（本用例的 `offset == snapshot().total_bytes` 与 `segment_files(...) == 2` 自证「轮转真的发生了」，⛔ 不是在一个没轮转的文件上空过）。
        三条判据各自承重：① 偏移是**全局坐标**（`snapshot().total_bytes`），⛔ 不是现役文件的 `st_size`；
        ② `reset=False`（轮转不是「整块搬走 / 换了一份旧副本」）；③ 新事件落在**新的归档段**里，消费侧跨段把它读到并真的写了单。"""
        with mock.patch.object(callback_store, "ROTATE_BYTES", 1):
            self.assertEqual(self.post_callback(received_at=FIXED_1), "200 OK")
            # 自证夹具：第一条一写完就轮转了（现役文件空、归档段在）。
            self.assertEqual(
                self.events.stat().st_size, 0,
                "自证夹具：写完没有轮转（现役文件不为空）⇒ 这条用例测不到轮转",
            )
            self.assertEqual(len(callback_store.segment_files(self.events)), 1)

            first = jidanpu_openapi_consume.consume_callbacks(self.binding, now=ROUND_NOW)
            self.assertEqual(first.mine, 1)
            self.assertEqual(self.order_status(self.order), "PAUSED")
            # ① 第一轮跑完：偏移 = **全局**总字节（段在、现役文件空）。
            self.assertEqual(
                self.refresh(self.binding).callback_file_offset,
                callback_store.snapshot(self.events).total_bytes,
                "轮转之后偏移没有停在逻辑流的全局末尾",
            )

            second_order = self.make_order(order_no=ORDER_NO_B)
            self.make_link(self.binding, second_order, remote_id=OTHER_REMOTE_ID)
            self.assertEqual(
                self.post_callback(
                    params=self.status_params(order_no=OTHER_REMOTE_ID), received_at=FIXED_2,
                ),
                "200 OK",
            )
            # 自证夹具：第二次写把新现役文件也轮转了 ⇒ 两个归档段。
            self.assertEqual(len(callback_store.segment_files(self.events)), 2)

            outcome = jidanpu_openapi_consume.consume_callbacks(
                self.refresh(self.binding), now=ROUND_NOW + timedelta(minutes=1),
            )

        # ② 轮转没有被误判成「文件被搬动过」。
        self.assertFalse(outcome.reset, "轮转被误判成「文件被搬动过」（水位与偏移被清零）")
        # ③ 跨段读到了新段里的那一条，并真的写了单。
        self.assertEqual(outcome.scanned, 1, "跨段之后没有从新段读起")
        self.assertEqual(outcome.mine, 1)
        self.assertEqual(self.order_status(second_order), "PAUSED")
        self.assertEqual(
            self.refresh(self.binding).callback_file_offset,
            callback_store.snapshot(self.events).total_bytes,
            "跨段消费之后偏移没有停在逻辑流的全局末尾",
        )
