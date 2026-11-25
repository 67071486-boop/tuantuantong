"""兜底对账（`apps/business/jidanpu_openapi_reconcile.py`，本线 T8）的判据。

> spec：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §8（全节）/
> §12.1-D6 / §13.4 的 **E9 / E10**；任务书 T8。

## 这份文件守什么

1. ⭐ **端到端（本任务存在的理由）**：**没有任何回调**（事件文件不存在 —— 这一点显式构造并断言）
   的情况下，造一张「远端已经变了、我方还是旧的」订单 ⇒ 跑一轮对账 ⇒
   **订单行真的被改对了**（断言**数据库行**，⛔ 不打 logger、⛔ 不看返回值）。
2. **查什么**：只查「在途」的单 —— 已停跟踪的链路、我方已闭合的单、已软删的单**都不查**。
3. **闸门**：没到 `reconcile_seconds`（默认 600）不跑（判据打在「上游请求数」上）。
4. **上限（E10）**：撞上限 ⇒ **只推迟不丢**（下一轮**必然**还能看到那些没查的）+ 响亮留痕。
5. **不知道要不要写的不写**：远端值与**我方一致** ⇒ ⛔ 不写（⛔ 不产生多余的审计行）。
6. ⛔ **打的是只读端点**：一轮里只出现 `GetOrderStatus`（行为断言：真发出去的请求路径集合）。
7. **失败要响亮留痕**（照 T6 的 `note_once` 台账口径），⛔ 不静默。
8. **E9：与回调走同一个写入口** —— 两条路各跑一遍同形状的场景，断言
   ①`apply_transition` 的**整份入参**逐字相同、②订单行逐列相同、③审计行逐列相同。

## 三条纪律（照 T6 那一份）

- ⛔ **零真实网络**：假上游复用 T2/T6 那一份（`tests.test_jidanpu_openapi._Upstream`，MockTransport）
  —— 假上游必然漂，所以**复用同一个**；地址用 `.invalid` 顶级域。
- ⛔ **夹具一律合成值**（`AGENTS.md` §5-C / E-0077）：码是 22 位合成串、密钥 / client id 都是
  `synthetic` / `canary`。
- ⛔ **断言打在数据库行上**（E-0009）：订单状态、审计行、台账行、绑定上的列。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.management import call_command
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import (
    jidanpu_openapi,
    jidanpu_openapi_consume,
    jidanpu_openapi_reconcile,
    jidanpu_upstream,
)
from apps.business.jidanpu_upstream import HallOutcome, MyOrdersOutcome, OutboxStats
from apps.business.models import (
    UPSTREAM_OPENAPI_RECONCILE_SECONDS,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from apps.callback import signature
from apps.callback import wsgi as callback_wsgi
#: ⭐ 复用 T2/T6 那一条线的假上游与合成凭据（⛔ 不另写一个假上游：两份桩必然漂）。
from tests.test_jidanpu_openapi import (
    BASE_URL,
    CLIENT_ID,
    ORDER_STATUS_PATH,
    _Upstream,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值（⛔ 一个真值都没有）──────────────────────────────────────────────

#: 三串 22 位合成码（形状 = `[A-Za-z0-9_-]{22}`，照接收器的定长正则；只有 E9 那两条用例
#: 会真的走接收器，其余用例的前提是「一个回调都没有」）。
CODE_A = "Rt8_-reconcile_test_1a"
CODE_NODES = "Rt8_-reconcile_test_2a"
CODE_PASSWORD = "Rt8_-reconcile_test_3a"
for _code in (CODE_A, CODE_NODES, CODE_PASSWORD):
    assert len(_code) == 22, _code

SECRET_A = "tt-reconcile-secret-0001"
SECRET_NODES = "tt-reconcile-secret-0002"
SECRET_PASSWORD = "tt-reconcile-secret-0003"

#: 密钥文件的**首行注释**（唯一出处 = `jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER`）。
KEYS_HEADER = jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER

#: 上家单号（远端）与我方单号（20 位，`Order.order_no` 的列宽）。
REMOTE_ID = "20261008000000000701"
REMOTE_ID_B = "20261008000000000702"
REMOTE_ID_C = "20261008000000000703"
ORDER_NO = "20261008000000000601"
ORDER_NO_B = "20261008000000000602"
ORDER_NO_C = "20261008000000000603"

#: 固定的墙钟（⛔ 不用 `timezone.now()`：闸门与台账的读数要能对着字面值断言）。
NOW = datetime(2026, 10, 8, 13, 0, 0, tzinfo=dt_timezone.utc)

#: 权威状态：「已暂停」是**会写**的那三格之一（`IN_PROGRESS → PAUSED`）。
AUTHORITATIVE = "已暂停"
#: 与我方 `IN_PROGRESS` 同一个目标态（⇒ 不该写）。
SAME_AS_OURS = "代练中"

#: 上限 / 间隔的**契约字面值**（spec §8.2 / §8.3）。⛔ 不从生产常量派生（E-0024）：
#: 有人把常量改坏时，下面那两条断言要**会红**。
CAP_CONTRACT = 50
RECONCILE_SECONDS_CONTRACT = 600


def _normalize_order_no(row: dict, *, skip: set[str]) -> dict:
    """把一行字典里**这一行自己的单号**换成占位符（两侧用同一套归一化，E-0083）。

    ⚠️ 只在「本来就是两张不同的单」这一层用（E9 要断言的是**写面**一致，
    ⛔ 不是「两张单的主键相同」）；行里的单号出现在不止一列，逐列排除会漏掉新列。
    """
    order_no = row.get("order_no") or ""
    out: dict = {}
    for key, value in row.items():
        if key in skip:
            continue
        if order_no and isinstance(value, str) and order_no in value:
            value = value.replace(order_no, "<单号>")
        out[key] = value
    return out


class _ReconcileCase(TenantTestCase):
    """一行②式绑定 + 假的密钥 / 事件文件路径 + 假上游。

    ⚠️ **事件文件放在一个不存在的目录下**：本文件绝大多数用例的前提是「**没有任何回调**」，
    那一点由 `test_no_callback_at_all_still_fixes_the_order_row` 显式断言。
    """

    def setUp(self):
        super().setUp()
        jidanpu_openapi.reset_token_cache()
        jidanpu_openapi_consume.reset_process_state()
        jidanpu_openapi_reconcile.reset_process_state()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.addCleanup(jidanpu_openapi_consume.reset_process_state)
        self.addCleanup(jidanpu_openapi_reconcile.reset_process_state)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "no-callbacks" / "events.jsonl"
        self.keys_file = self.tmp / "callback-keys.env"
        self.keys_file.write_text(
            KEYS_HEADER + "\n"
            + f"{CODE_A}\t{SECRET_A}\n"
            + f"{CODE_NODES}\t{SECRET_NODES}\n"
            + f"{CODE_PASSWORD}\t{SECRET_PASSWORD}\n",
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
        self.binding = self.make_binding()

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_binding(self, *, name="a", enabled=True):
        """一行**②式**绑定；绑定人持有 `order.change_status`（写状态要它）。"""
        code = {"a": CODE_A, "nodes": CODE_NODES, "password": CODE_PASSWORD}[name]
        secret = {
            "a": SECRET_A, "nodes": SECRET_NODES, "password": SECRET_PASSWORD,
        }[name]
        username = f"kefu-reconcile-{name}"
        # ⚠️ 节点是挂在**组同名 Role** 上的（`grant_nodes` 的口径）⇒ 「没有节点的那个账号」必须
        #    用一个**自己的组**，否则它会从同组别的账号那里继承到节点（`Role.name` 列宽 5）。
        groups = ("无权限",) if name == "nodes" else ("客服",)
        nodes = () if name == "nodes" else ("order.change_status",)
        make_logged_in_client(username=username, groups=groups, nodes=nodes)
        return JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=BASE_URL,
            account=f"acc-reconcile-{name}", password="pw-canary-reconcile",
            mode="openapi", client_id=f"{CLIENT_ID}-{name}", client_secret=secret,
            callback_code=code, enabled=enabled,
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

    def one_order(self, *, order_no=ORDER_NO, remote_id=REMOTE_ID, reply=True):
        """最常见的夹具：一张**在途**单 + 一条链路（`reply=True` 时顺手把假上游的回包设好）。"""
        order = self.make_order(order_no=order_no, status=OrderStatus.IN_PROGRESS)
        self.make_link(self.binding, order, remote_id=remote_id)
        if reply:
            self.reply_status()
        return order

    # ── 跑一轮对账 ──────────────────────────────────────────────────────────

    def reconcile(self, binding=None, *, now=NOW):
        return jidanpu_openapi_reconcile.reconcile_openapi_statuses(
            binding if binding is not None else self.binding, now=now,
        )

    # ── 读数（⛔ 一律重新查库）──────────────────────────────────────────────

    def order_status(self, order) -> str:
        return Order.objects.get(pk=order.pk).status

    def notes(self, *, kind=None):
        rows = JidanpuSyncEvent.objects.all()
        return rows.filter(kind=kind) if kind else rows

    def transitions(self):
        return OperationLog.objects.filter(action="order.transition")

    def queried_order_ids(self) -> list[str]:
        """假上游这一轮**真的**收到过哪些 `orderId`（按发生顺序）。"""
        return [row["params"].get("orderId") for row in self.upstream.api_calls()]

    def api_paths(self) -> set[str]:
        return {row["path"] for row in self.upstream.api_calls()}

    # ── 让**真接收器**落一条回调（只有 E9 那两条用例用）──────────────────────

    def post_callback(self, *, order_no) -> str:
        """走真接收器（`wsgi.application`）打一条通过验签的 `action=10` 回调。

        ⭐ 「形状照接收器」的落地方式：记录由接收器自己落盘，⛔ 不是用例手搓一个 dict。
        """
        params = {
            "orderNo": order_no, "sourceOrderNo": ORDER_NO, "status": "问题单",
            "note": "", "action": "10", "timestamp": "1791500000",
        }
        body = dict(params)
        body[signature.SIGN_FIELD_NAME] = signature.compute_sign(params, SECRET_A)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_A}",
            "QUERY_STRING": "", "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": BytesIO(raw),
            "REMOTE_ADDR": "127.0.0.1",
        }
        status: dict = {}
        callback_wsgi.application(environ, lambda line, headers: status.update(line=line))
        return status["line"]


# ══ ① 端到端：**没有回调**也要把订单改对（本任务存在的理由）═════════════════════
# spec §8.1 的三条理由：回调重试策略文档没写、实测 0.5% 重复、**发版重启那段确定会漏**。


class EndToEndTests(_ReconcileCase):
    def test_no_callback_at_all_still_fixes_the_order_row(self):
        """⭐ **端到端**：**一个回调都没有** ⇒ 跑一轮对账 ⇒ 远端已经变了而我们还旧着的订单
        **真的被改对了**。

        ⛔ 弄红它：把 `apply_authoritative_status` 那一步去掉（或让 `plan.write` 不生效）
        ⇒ 第一条断言红（库里还是「代练中」）。

        ⚠️ 四条断言各自承重：
        ① **数据库行**（E-0009：不看返回值、不看 logger）；
        ② 「**没有任何回调**」是显式构造的（文件不存在 ⇒ 这一轮的信息只能来自主动查）；
        ③ 审计 + 台账各一行，且台账**逐字**看得出这一笔是「兜底对账」做的；
        ④ 真打了权威那一次（一条在途单 ⇒ 一次查单），查的是**这张单的上家单号**。
        """
        order = self.one_order()
        # ② 前提：事件文件压根不存在 ⇒ 这一轮没有任何回调可消费
        self.assertFalse(
            self.events.exists(), "夹具坏了：这一条用例的前提是「一个回调都没有」",
        )
        self.assertEqual(
            self.upstream.api_calls(), [], "夹具坏了：这一轮之前不该有上游请求",
        )

        outcome = self.reconcile()

        # ① 订单状态**真的变了**（重新查库）
        self.assertEqual(self.order_status(order), "PAUSED")
        self.assertEqual(outcome.applied, 1)
        self.assertEqual(outcome.candidates, 1)
        # ③ 审计：既有动作码 `order.transition`，前后值 = 中文 label、actor = 本线的机器名
        audit = self.transitions().filter(order_no=ORDER_NO)
        self.assertEqual(audit.count(), 1)
        row = audit.get()
        self.assertEqual((row.from_value, row.to_value), ("代练中", "已暂停"))
        self.assertEqual(row.actor, jidanpu_openapi_consume.MACHINE_ACTOR)
        self.assertIsNone(row.actor_id)
        # ③ 台账：一条「已写」的留痕，且**逐字**看得出这一笔是兜底对账做的
        done = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO, status="done")
        self.assertEqual(done.count(), 1)
        self.assertIn(AUTHORITATIVE, done.get().reason)
        self.assertIn("兜底对账", done.get().reason)
        self.assertEqual(done.get().payload["trigger"], "reconcile")
        # ④ 真打了权威那一次，而且查的是**这张单的上家单号**
        self.assertEqual(self.queried_order_ids(), [REMOTE_ID])

    def test_only_the_read_only_endpoint_is_called(self):
        """⛔ **只打只读端点**：一轮里出现的路径**恰好**是 `GetOrderStatus`。

        ⛔ 弄红它：在对账那条路上顺手打任何写接口（录单 / 改单 / 改状态 / 传图）
        ⇒ 路径集合多一个成员，本用例红。

        ⚠️ 判据是**行为**（真发出去的请求），⛔ 不是「模块里没有那个字符串」那种读码断言。
        """
        self.one_order()

        self.reconcile()

        self.assertEqual(self.api_paths(), {ORDER_STATUS_PATH})

    def test_a_closed_order_is_not_a_candidate(self):
        """「**还没到终态**」那一半口径（spec §8.2）：我方**已闭合**的单不进候选。

        ⛔ 弄红它：去掉 `CLOSED_STATUSES` 那一条判据 ⇒ 候选数变 2、上游被多打一次。
        """
        self.reply_status()
        done = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.COMPLETED)
        self.make_link(self.binding, done, remote_id=REMOTE_ID_B)
        live = self.make_order()
        self.make_link(self.binding, live, remote_id=REMOTE_ID)

        outcome = self.reconcile()

        self.assertEqual(outcome.candidates, 1)
        self.assertEqual(self.queried_order_ids(), [REMOTE_ID])
        self.assertEqual(self.order_status(live), "PAUSED")
        self.assertEqual(self.order_status(done), "COMPLETED")

    def test_a_terminated_link_is_not_a_candidate(self):
        """已停跟踪的链路（`terminated_at` 非空 = 两边都到过终态）⇒ 不进候选。

        ⛔ 弄红它：去掉 `terminated_at__isnull=True` 那一条判据 ⇒ 候选数变 1、多打一次上游。
        """
        self.reply_status()
        stopped = self.make_order(order_no=ORDER_NO_C)
        self.make_link(
            self.binding, stopped, remote_id=REMOTE_ID_C, terminated_at=NOW,
        )

        outcome = self.reconcile()

        self.assertEqual(outcome.candidates, 0)
        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.order_status(stopped), "IN_PROGRESS")

    def test_a_soft_deleted_order_is_not_queried_and_never_crashes(self):
        """我方订单**已软删** ⇒ 不进候选（⛔ 不为它打上游、⛔ 也不抛异常）。

        ⚠️ **这条判据是承重的**（不是防御性写法）：`link.order` 走 forward FK
        ⇒ 软删单照样取得到，而唯一写入口 `apply_transition` 加锁读的是**默认 manager**
        ⇒ 放它往下走会**抛异常**（T6 第 ⑤ 步同一条判据）。
        ⛔ 弄红它：去掉软删那一条判据 ⇒ 本用例在 `reconcile()` 上抛异常（红）。
        """
        order = self.one_order()
        Order.all_objects.filter(pk=order.pk).update(deleted_at=NOW)
        self.assertIsNotNone(
            Order.all_objects.get(pk=order.pk).deleted_at, "夹具自证：这一行真的被软删了"
        )

        outcome = self.reconcile()

        self.assertEqual(outcome.candidates, 0)
        self.assertEqual(self.upstream.api_calls(), [])


# ══ ② E9：与回调走**同一个**写入口 / 同一份写面 ════════════════════════════════


class SameWriteExitTests(_ReconcileCase):
    """spec §8.3 第 ③ 条：对账的写入面必须与回调**完全一致**（⛔ 不许「多写一列」）。"""

    def _both_paths(self, *, order_no_b=ORDER_NO_B, remote_id_b=REMOTE_ID_B, spy=None):
        """同一个绑定上跑**两条路**：① 真回调（接收器 + 消费者）② 兜底对账。"""
        patcher = None
        if spy is not None:
            patcher = mock.patch.object(
                jidanpu_openapi_consume.jidanpu_upstream, "apply_transition", side_effect=spy,
            )
            patcher.start()
        try:
            first = self.one_order()
            second = self.make_order(order_no=order_no_b)
            self.make_link(self.binding, second, remote_id=remote_id_b)
            self.reply_status()
            self.assertEqual(self.post_callback(order_no=REMOTE_ID), "200 OK")
            jidanpu_openapi_consume.consume_callbacks(self.binding, now=NOW)
            jidanpu_openapi_reconcile.reconcile_openapi_statuses(self.binding, now=NOW)
        finally:
            if patcher is not None:
                patcher.stop()
        return first, second

    def test_both_paths_write_with_exactly_the_same_arguments(self):
        """⭐ **E9**：两条路各跑一遍同形状的场景 ⇒ `apply_transition` 的**整份入参**逐字相同。

        ⛔ 弄红它：让对账那条路自己写一遍（多传一个参数 / 换一套 `nodes` / 不传
        `suppress_outbound_sync`）⇒ 两次调用的键集合或值不同，本用例红。

        ⚠️ 断言钉的是**值**（字面量），⛔ 不是「两次调用相等」那种自洽判据（E-0083：
        同时改行为与期望就自检不出）——下面把每一个值都单独钉一次。
        """
        seen: list[dict] = []
        real = jidanpu_upstream.apply_transition

        def _spy(order, target, **kwargs):
            seen.append({"order": order.order_no, "target": str(target), **kwargs})
            return real(order, target, **kwargs)

        self._both_paths(spy=_spy)

        by_order = {call["order"]: call for call in seen}
        self.assertEqual(set(by_order), {ORDER_NO, ORDER_NO_B}, "两条路各要写一次")
        callback_call, reconcile_call = by_order[ORDER_NO], by_order[ORDER_NO_B]
        # ① 整份入参**逐字**相同（键集合 + 每一个值）
        self.assertEqual(
            set(callback_call), set(reconcile_call),
            "对账那条路多传 / 少传了参数（spec §8.3 ③：写入面必须完全一致）",
        )
        for key in callback_call:
            if key == "order":
                continue
            self.assertEqual(
                callback_call[key], reconcile_call[key],
                f"`apply_transition` 的 {key} 在两条路上不一致",
            )
        # ② 每一个值单独钉一次（⛔ 不只靠「两边相等」）
        self.assertEqual(reconcile_call["target"], "PAUSED")
        self.assertEqual(reconcile_call["actor"], jidanpu_openapi_consume.MACHINE_ACTOR)
        self.assertNotIn("actor_id", reconcile_call)
        self.assertIs(reconcile_call["suppress_outbound_sync"], True)
        self.assertEqual(
            reconcile_call["nodes"],
            frozenset({"order.change_status"}),
            "写面必须用绑定人自己的节点集（⛔ 不许无条件放行）",
        )

    def test_both_paths_produce_the_same_order_row_and_audit_row(self):
        """⭐ **E9（数据面）**：两条路写出来的**订单行逐列相同** + 审计行逐列相同。

        ⛔ 弄红它：给对账那条路多写一列（例如顺手把 `order.hall_price` 改了 / 多记一笔审计）
        ⇒ 两条路的订单行不再逐列相同，本用例红。

        ⚠️ 排除的三列都是**每次写入必然不同**的主键 / 时间戳（⛔ 不是「挑好看的列」）：
        `id` / `created_at` / `updated_at`；`updated_by` **留在断言里**
        （它写的就是 `actor` ⇒ 「对账换个 actor 名字」这种漂法当场就红）。
        """
        self._both_paths()

        # ⚠️ 排除的列：主键 / 时间戳（每次写入必然不同）+ `order_no`（两张单本来就不同号）。
        #    ⛔ `status` / `updated_by` / `completed_at` 这些**写面**上的列一个都不排除。
        excluded = {"id", "created_at", "updated_at", "order_no"}
        rows = {
            row["order_no"]: {k: v for k, v in row.items() if k not in excluded}
            for row in Order.objects.filter(
                order_no__in=[ORDER_NO, ORDER_NO_B]
            ).values()
        }
        self.assertEqual(set(rows), {ORDER_NO, ORDER_NO_B})
        self.assertEqual(
            rows[ORDER_NO], rows[ORDER_NO_B],
            "两条路写出来的订单行不一致（对账那条路「多写一列」了？）",
        )
        self.assertEqual(rows[ORDER_NO]["status"], "PAUSED")
        self.assertNotEqual(ORDER_NO, ORDER_NO_B, "夹具自证：两张单是两个单号")
        self.assertEqual(rows[ORDER_NO]["updated_by"], jidanpu_openapi_consume.MACHINE_ACTOR)

        # ⚠️ 审计行里的单号出现在**三列**（`order_no` / `target_id` / `summary` 那句人话里）
        #    ⇒ 对这三处**两侧用同一套归一化**把单号换成占位符（E-0083：只归一化一侧会让判据
        #    恒真），其余列逐字比。⛔ 归一化**不动** `from_value` / `to_value` / `action` /
        #    `actor` 这些**写面**上的列。
        audits = {
            row["order_no"]: _normalize_order_no(row, skip={"id", "created_at", "order_no", "target_id"})
            for row in self.transitions().values()
        }
        self.assertEqual(set(audits), {ORDER_NO, ORDER_NO_B})
        self.assertEqual(
            audits[ORDER_NO], audits[ORDER_NO_B],
            "两条路的审计行不一致（对账那条路多记了一笔 / 少记了一列？）",
        )
        for row in self.transitions().values():
            self.assertEqual(row["target_id"], row["order_no"], "审计指向了别的单")
        self.assertIn(
            "将订单 <单号> 的状态从代练中改为已暂停", audits[ORDER_NO]["summary"],
        )


# ══ ③ 不知道要不要写的不写（spec §6.3 的幂等那一格）════════════════════════════


class NothingToWriteTests(_ReconcileCase):
    def test_a_remote_value_equal_to_ours_writes_nothing(self):
        """⭐ 远端值与我方**一致** ⇒ ⛔ 不写（⛔ 不产生多余的审计行）。

        ⛔ 弄红它：绕开 `plan_openapi_status` 直接写（或把「我方 == 目标 ⇒ 不写」那一格去掉）
        ⇒ 审计行数变 1 ⇒ 本用例红。

        ⚠️ 台账**仍然**留一条（`status="skipped"`）——那是「这一轮查过了、不用写」的结论，
        与「审计行」是两件事（审计是订单域的证据面，台账是同步域的证据面）。
        """
        order = self.one_order(reply=False)
        self.reply_status(status=SAME_AS_OURS)

        outcome = self.reconcile()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.transitions().count(), 0, "不该产生审计行")
        self.assertEqual(outcome.applied, 0)
        self.assertEqual(outcome.skipped, 1)
        rows = self.notes(kind="openapi.status_in")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "skipped")
        # 阳性对照：这一轮**真的查了**（否则上面那些都可能只是「什么都没跑」）
        self.assertEqual(self.queried_order_ids(), [REMOTE_ID])

    def test_a_remote_value_that_this_line_never_maps_writes_nothing(self):
        """⛔ 本线**明确不映射**的那一档（`待验收`）⇒ 不写 + 留一条台账（⛔ 不猜）。

        ⛔ 弄红它：照登录式底表把它兜底成某个状态 ⇒ 订单行被改（红）。
        """
        order = self.one_order()
        self.reply_status(status="待验收")

        outcome = self.reconcile()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.transitions().count(), 0)
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(self.notes(kind="openapi.status_in").count(), 1)

    def test_an_unknown_remote_value_is_registered_not_guessed(self):
        """⛔ 认不出来的状态（spec §6.4）：不改单、⛔ 不猜成问题单、要留一条待人工核对的台账。"""
        order = self.one_order()
        self.reply_status(status="外星状态")

        outcome = self.reconcile()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertNotEqual(self.order_status(order), "ISSUE")
        rows = self.notes(kind="openapi.unmapped_status")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "failed")
        self.assertEqual(rows.get().payload["trigger"], "reconcile")
        self.assertEqual(outcome.failed, 1)
        self.assertIn("外星状态", outcome.alert)


# ══ ④ E10：上限 —— 只推迟、不丢 ════════════════════════════════════════════════


class RoundCapTests(_ReconcileCase):
    def test_the_cap_defers_but_never_drops(self):
        """⭐ **E10**：一轮最多查 `OPENAPI_RECONCILE_MAX_PER_ROUND` 张，**撞上限要留痕**且
        **只推迟不丢**（下一轮**必然**还能看到那些没查的）。

        做法（⛔ 不建 51 张单去凑那个数字）：把上限临时压到 2 跑，三张候选 ⇒
        第一轮查 2 张、第三张**原样不动** + 一条响亮的 `openapi.reconcile_cap` 台账；
        第二轮（窗口轮转）**必然**从第三张接着查 ⇒ 它被改对。

        ⛔ 弄红它（两个方向都要能红）：
        ① **去掉上限**（候选全查）⇒ 第一轮就把第三张写了 ⇒ 「第三张原样不动」那条红；
        ② **去掉窗口轮转**（每轮都从第一张开始）⇒ 第二轮又查前两张，第三张**永远**轮不到
           ⇒ 第二轮那条断言红（这正是「只推迟**且不丢**」承重的地方）。
        """
        # 契约值单独用字面量钉一次（E-0024：不从生产常量派生）
        self.assertEqual(
            jidanpu_openapi_reconcile.OPENAPI_RECONCILE_MAX_PER_ROUND, CAP_CONTRACT
        )

        self.reply_status()
        first = self.one_order(reply=False)
        second = self.one_order(
            order_no=ORDER_NO_B, remote_id=REMOTE_ID_B, reply=False
        )
        third = self.one_order(
            order_no=ORDER_NO_C, remote_id=REMOTE_ID_C, reply=False
        )

        with mock.patch.object(jidanpu_openapi_reconcile, "OPENAPI_RECONCILE_MAX_PER_ROUND", 2):
            capped = self.reconcile()

        self.assertEqual(capped.candidates, 3)
        self.assertEqual(capped.queried, 2, "一轮的请求数必须有界")
        self.assertEqual(self.queried_order_ids(), [REMOTE_ID, REMOTE_ID_B])
        self.assertEqual(self.order_status(third), "IN_PROGRESS", "没查的那一张被静默丢掉了？")
        self.assertEqual(capped.deferred, 1)
        # 响亮留痕（台账）：一条 `openapi.reconcile_cap`，reason 与 payload 都说清是**上限**
        cap_notes = self.notes(kind=jidanpu_openapi_reconcile.KIND_RECONCILE_CAP)
        self.assertEqual(cap_notes.count(), 1)
        cap_note = cap_notes.get()
        self.assertEqual(cap_note.status, "failed")
        self.assertEqual(cap_note.target_type, "reconcile")
        self.assertIn("超过一轮的上限", cap_note.reason)
        self.assertEqual(cap_note.payload["cap"], 2)
        self.assertEqual(cap_note.payload["deferred"], 1)
        self.assertEqual(cap_note.payload["upstream_binding_id"], self.binding.pk)
        # 那句话要能被人看见（调度层把它盖进绑定 `last_error`；见接线那一组）
        self.assertIn("超过一轮的上限", capped.alert)
        self.assertEqual(self.order_status(first), "PAUSED")
        self.assertEqual(self.order_status(second), "PAUSED")

        # 第二轮：窗口**轮转** ⇒ 上一轮没查的那一张必然被查到（只推迟、不丢）
        with mock.patch.object(
            jidanpu_openapi_reconcile, "OPENAPI_RECONCILE_MAX_PER_ROUND", 2
        ):
            second_round = self.reconcile(now=NOW + timedelta(seconds=600))

        self.assertIn(REMOTE_ID_C, self.queried_order_ids(), "上一轮没查的那一张永远轮不到")
        self.assertEqual(self.order_status(third), "PAUSED")
        self.assertEqual(second_round.applied, 1)
        # ⛔ 台账不刷屏：同一句原因只记一条（`note_once` 的四元组）
        self.assertEqual(
            self.notes(kind=jidanpu_openapi_reconcile.KIND_RECONCILE_CAP).count(), 1,
        )

    def test_an_empty_candidate_set_costs_nothing(self):
        """没有在途单 ⇒ 一次上游都不打、一行台账都不写（兜底对账的低频语义）。"""
        self.reply_status()

        outcome = self.reconcile()

        self.assertEqual(outcome.candidates, 0)
        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.notes().count(), 0)
        self.assertEqual(outcome.text, "对账 无在途单")


# ══ ⑤ 失败要**响亮留痕**（照 T6 的台账口径），⛔ 不静默 ═════════════════════════


class LoudFailureTests(_ReconcileCase):
    def test_a_transport_failure_writes_nothing_and_leaves_a_trace(self):
        """⭐ 查权威失败 ⇒ **一个字都不写订单** + 一条台账 + 进本轮 `alert`（⛔ 不静默）。

        ⚠️ 顺带钉住「**本轮就此停住**」：窗口不前移 ⇒ 下一轮从同一张重试。
        ⛔ 弄红它：查不到就拿旧值 / 猜一个状态写下去 ⇒ 订单行与审计行都会出现（红）。
        """
        order = self.one_order()
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("synthetic connect failure"))

        outcome = self.reconcile()

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.transitions().count(), 0)
        self.assertTrue(outcome.stopped)
        rows = self.notes(kind="openapi.callback_query_failed")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "failed")
        self.assertIn("ConnectError", rows.get().reason)
        self.assertTrue(outcome.alert, "这一轮必须留下要人看见的一句话")
        self.assertIn("中途停住", outcome.text)

    def test_a_denied_transition_is_registered_and_never_writes(self):
        """状态机拒绝（绑定人缺 `order.change_status` 节点）⇒ 只登记、不改状态。

        ⚠️ 这一条同时守「对账那条路不许自己判一套」：那句人话与回调那条路**逐字相同**
        （它来自同一个 `REASON_DENIED`）。

        ⛔ 弄红它：把 `nodes` 传成 `None`（宽松语义 ⇒ 只要边存在就放行）⇒ 状态被改写（红）。
        """
        binding = self.make_binding(name="nodes")
        order = self.make_order()
        self.make_link(binding, order)
        self.reply_status()

        outcome = self.reconcile(binding)

        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.transitions().count(), 0)
        self.assertEqual(outcome.failed, 1)
        rows = self.notes(kind="openapi.status_in").filter(target_ref=ORDER_NO)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().status, "failed")
        self.assertIn("状态机拒绝", rows.get().reason)
        self.assertIn("状态机拒绝", outcome.alert)

    def test_a_binding_without_credentials_never_touches_the_database_or_the_upstream(self):
        """凭据不全（`client_secret` 为空）⇒ 一次库都不查、一次上游都不打（⛔ 不静默建连）。"""
        order = self.one_order()
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(client_secret="")
        binding = JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)

        outcome = self.reconcile(binding)

        self.assertFalse(outcome.enabled)
        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.order_status(order), "IN_PROGRESS")


# ══ ⑥ 接线：闸门（`reconcile_seconds`）+ 顺序 + 留痕出口 ═══════════════════════


@contextmanager
def _noop_context(tenant):
    yield


@contextmanager
def _stub_connection(_binding):
    yield (mock.Mock(), mock.Mock())


class WiringTests(_ReconcileCase):
    """闸门 / 顺序 / `last_error` —— 都打在**真跑一遍 `jidanpu_sync --once`** 上。"""

    def setUp(self):
        super().setUp()
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        for name in (
            "_UPSTREAM_OPENAPI_RECONCILE_AT", "_UPSTREAM_HALL_AT",
            "_UPSTREAM_MY_ORDERS_AT",
        ):
            getattr(module, name).clear()
        self.tenant = mock.Mock(schema_name="tenant-t8")
        #: ⭐ 另外三段换成桩：本组只测对账那一段的接线 ⇒ 上游请求数**只**来自对账。
        #: ⚠️ 回调消费也换成桩：本文件的前提是「没有任何回调」（那一段自己有 T6 的用例）。
        self.patches = [
            mock.patch.object(jidanpu_upstream, "run_outbox", return_value=OutboxStats()),
            mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()),
            mock.patch.object(jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()),
            mock.patch.object(module.jidanpu, "open_client_with_http", _stub_connection),
            mock.patch.object(
                jidanpu_openapi_consume, "consume_callbacks",
                return_value=jidanpu_openapi_consume.ConsumeOutcome(enabled=True),
            ),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, *, now=NOW):
        """跑一趟 `--once`（租户遍历被替身接管），回 `(out, err)`。"""
        out, err = io.StringIO(), io.StringIO()
        patcher = mock.patch.object(self.module.Tenant, "objects")
        objects = patcher.start()
        objects.exclude.return_value.order_by.return_value = [self.tenant]
        try:
            with mock.patch.object(self.module, "tenant_context", _noop_context), \
                 mock.patch.object(self.module.timezone, "now", return_value=now):
                call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
        finally:
            patcher.stop()
        return out.getvalue(), err.getvalue()

    def test_the_interval_gate_keeps_the_upstream_quiet(self):
        """⭐ **闸门**：没到 `reconcile_seconds`（默认 600）就不跑 —— 判据打在**上游请求数**上。

        ⛔ 弄红它：去掉 `reconcile_due` 那道闸 ⇒ 第二趟（同一时刻）又查一次，
        `queried_order_ids()` 变两条 ⇒ 本用例红。

        ⚠️ 三个读数各自承重：① 第一趟**真查了**（阳性对照，否则「第二趟没查」可能只是
        「一趟都没跑」）；② 第二趟（+599 秒）**一条都不查**；
        ③ 到点（+600 秒）**又查**（闸门是「到点就跑」，⛔ 不是「只跑一次」）。
        """
        self.assertEqual(UPSTREAM_OPENAPI_RECONCILE_SECONDS, RECONCILE_SECONDS_CONTRACT)
        order = self.one_order()

        out, _err = self._run()
        self.assertEqual(self.queried_order_ids(), [REMOTE_ID], "第一趟就该到点（锚为空 = 到点）")
        self.assertEqual(self.order_status(order), "PAUSED")
        self.assertIn("对账 候选 1", out)

        self._run(now=NOW + timedelta(seconds=599))
        self.assertEqual(self.queried_order_ids(), [REMOTE_ID], "没到间隔却又打了一次上游")

        self._run(now=NOW + timedelta(seconds=600))
        self.assertEqual(
            self.queried_order_ids(), [REMOTE_ID, REMOTE_ID], "到点了却没跑（闸门把路堵死了）",
        )

    def test_a_stored_zero_falls_back_to_this_lines_own_default(self):
        """间隔列存 `0`（配置写坏）⇒ 按**本线这一列自己的默认值**（600 秒）兜底。

        ⛔ 弄红它：把 0 当成「每轮都跑」⇒ 第二趟就会多打一次上游
        （`or 600` 与「0 = 每轮」是两件事，E-0021 家族）。
        """
        self.one_order()
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(reconcile_seconds=0)

        self._run()
        self._run(now=NOW + timedelta(seconds=3))  # 常驻循环的一拍

        self.assertEqual(
            self.queried_order_ids(), [REMOTE_ID], "存 0 被当成了「每一拍都跑」",
        )

    def test_the_reconcile_segment_runs_after_the_callback_segment(self):
        """⭐ **顺序是承重的**（spec §8.3 第 ② 条）：一轮里**先**消费回调、**后**对账。

        ⛔ 弄红它：把对账那一段挪到回调消费之前 ⇒ 调用顺序反转，本用例红。

        ⚠️ 顺带钉住**按方式分闸**的另一半：这一行是②式 ⇒ `run_outbox` **一次都不该被调**
        （写接口第一期不做，spec §15-N3：它跑了只会在建连处抛 `ConfigError`）。
        弄红它：让②式照跑 outbox ⇒ `trace` 里多一个 `outbox`。
        """
        trace: list[str] = []

        def _consume(binding, **kwargs):
            trace.append("consume")
            return jidanpu_openapi_consume.ConsumeOutcome(enabled=True)

        def _reconcile(binding, **kwargs):
            trace.append("reconcile")
            return jidanpu_openapi_reconcile.ReconcileOutcome(enabled=True)

        def _outbox(binding, **kwargs):
            trace.append("outbox")
            return OutboxStats()

        with mock.patch.object(
            jidanpu_openapi_consume, "consume_callbacks", side_effect=_consume
        ), mock.patch.object(
            jidanpu_openapi_reconcile, "reconcile_openapi_statuses", side_effect=_reconcile
        ), mock.patch.object(jidanpu_upstream, "run_outbox", side_effect=_outbox):
            self._run()

        self.assertEqual(trace, ["consume", "reconcile"])

    def test_the_cap_warning_lands_on_the_binding_as_last_error(self):
        """⭐ **响亮**的第二半：撞上限那句话要**落到绑定 `last_error`**（页面 / 告警看的是它）。

        ⛔ 弄红它：只写台账、不在调度层盖 `last_error` ⇒ 本用例红
        （那正是「留痕了但没人看得见」）。
        """
        self.reply_status()
        for order_no, remote_id in (
            (ORDER_NO, REMOTE_ID), (ORDER_NO_B, REMOTE_ID_B), (ORDER_NO_C, REMOTE_ID_C),
        ):
            self.one_order(order_no=order_no, remote_id=remote_id, reply=False)

        with mock.patch.object(jidanpu_openapi_reconcile, "OPENAPI_RECONCILE_MAX_PER_ROUND", 2):
            self._run()

        fresh = JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)
        self.assertIn("超过一轮的上限", fresh.last_error)
        self.assertIsNotNone(fresh.last_error_at)
        self.assertEqual(
            self.notes(kind=jidanpu_openapi_reconcile.KIND_RECONCILE_CAP).count(), 1
        )
        # 阳性对照：这一轮真的写了前两张（否则「有 last_error」可能来自别处）
        self.assertEqual(self.order_status(Order.objects.get(order_no=ORDER_NO)), "PAUSED")

    def test_a_password_mode_binding_is_skipped_without_noise(self):
        """①式绑定 ⇒ 对账那一段**一次库都不查、一次上游都不打**，汇总里也不打噪音。

        ⚠️ 判据两条：上游零请求（候选那一行在库里没被碰）+ stdout 里**没有**对账那一段。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        binding = self.make_binding(name="password")
        binding.mode = "password"
        binding.save(update_fields=["mode"])
        order = self.make_order()
        self.make_link(binding, order)
        self.reply_status()

        out, _err = self._run()

        self.assertEqual(self.upstream.api_calls(), [])
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertNotIn("对账", out)


# ══ ⑦ 停滞修复（线上事故）═══════════════════════════════════════════
#
# 现场：单号 `20261022141632291086` 上游 14:35 已接单，我方「待抢单」到 14:50 人工指派；
# 在途 4767 条里 4749 条**从未回拉**。三个机械原因（计划 `docs/plans/2026-10-22-reconcile-stall-fix.md`）：
#   ① 窗口起点住在**进程内存** ⇒ 每次发版重启都从最前面 50 张重来；
#   ② 一遇 `OUTCOME_RETRY` 就 `break` ⇒ 窗口里有一张「查不动」的单，这一格永不再前进；
#   ③ 候选**全量材料化**，且最新那批单可能永远排不进窗口。
# ⚠️ 原 F1（把「没有相关订单信息」认成 absent）已被实测证伪 ⇒ ⛔ 本组**不碰** `absent_order_error`。


class StallFixTests(_ReconcileCase):
    """G1（时钟窗口）/ G2（失败分类）/ G3（单一预算 50）的判据。"""

    #: ⭐ 让**钟摆窗口**落在第 0 格的那个时刻（`int(ts)//600*35 % 60 == 0`）：
    #: 老实现（进程游标从 0 起）于是只查 0–49，最新那 10 张永远排不进来 —— 这正是要抓的红。
    NOW_LATEST = NOW + timedelta(seconds=3600)

    def reply_status_by_order(self, replies):
        """按 `orderId` 给**不同**回包（默认假上游只按路径给，而「单坏」那一档必须按单给）。

        ⚠️ 拦下来的那几张也照形状记进 `upstream.requests`（判「打了几发」的读数不能少）。
        """
        upstream = self.upstream
        original = upstream.handler

        def handler(request):
            params = dict(request.url.params)
            if request.url.path == ORDER_STATUS_PATH and params.get("orderId") in replies:
                upstream.requests.append({
                    "method": request.method, "path": request.url.path,
                    "url": str(request.url), "params": params,
                    "authorization": request.headers.get("authorization"),
                    "content_type": request.headers.get("content-type"),
                    "form": {}, "json": None,
                })
                reply = replies[params["orderId"]]
                if isinstance(reply, Exception):
                    raise reply
                return httpx.Response(200, json=reply)
            return original(request)

        upstream.handler = handler

    @staticmethod
    def _status_envelope(status):
        return {
            "code": 1, "msg": None,
            "data": {
                "status": status, "creatTime": "2026-10-08 12:00:00", "operTime": None,
                "doneTime": None, "verifyTime": None,
            },
        }

    # ── G2：只有「这一张单」类失败才让过 ─────────────────────────────────────

    def test_a_broken_order_does_not_stop_the_round(self):
        """⭐ 一张单的**形状坏**（`data.status` 空串）⇒ 记一条 `reconcile_stuck` + **继续下一张**。

        ⛔ 弄红它：把「单坏」也当全局失败 ⇒ `break` ⇒ 第二、三张停在「代练中」（第一张的
        `data.status` 空串会抛 `OpenApiError`），本轮 `stuck` 也不会是 1。
        """
        self.one_order(reply=False)
        second = self.one_order(order_no=ORDER_NO_B, remote_id=REMOTE_ID_B, reply=False)
        third = self.one_order(order_no=ORDER_NO_C, remote_id=REMOTE_ID_C, reply=False)
        self.reply_status()
        #: 只有第一张坏：`data.status` 不是非空字符串（= 计划里说的「形状坏」那一档）。
        self.reply_status_by_order({REMOTE_ID: self._status_envelope("")})

        outcome = self.reconcile()

        self.assertFalse(outcome.stopped, "一张单坏了就把整轮停住 ⇒ 后面所有单永远轮不到")
        self.assertEqual(outcome.stuck, 1)
        self.assertEqual(outcome.queried, 3, "坏单之后的两张必须接着查")
        self.assertEqual(self.order_status(second), "PAUSED")
        self.assertEqual(self.order_status(third), "PAUSED")
        stuck = self.notes(kind=jidanpu_openapi_reconcile.KIND_RECONCILE_STUCK)
        self.assertEqual(stuck.count(), 1)
        self.assertEqual(stuck.get().status, "failed")
        self.assertEqual(stuck.get().target_ref, ORDER_NO, "台账要点得名单号")

    def test_a_permission_failure_still_stops_the_round(self):
        """⭐ **反向控制**：权限档（①②）不是「这张单」的事 ⇒ 照旧**停住**、⛔ 不记 `reconcile_stuck`。

        ⚠️ 这两条缺一不可：只放宽不收紧 = 把「凭据坏了」当成「单坏了」，那会每轮白打一整窗。
        """
        self.one_order(reply=False)
        second = self.one_order(order_no=ORDER_NO_B, remote_id=REMOTE_ID_B, reply=False)
        third = self.one_order(order_no=ORDER_NO_C, remote_id=REMOTE_ID_C, reply=False)
        self.reply_status()
        self.reply_status_by_order({REMOTE_ID: {
            "code": 0, "msg": "您没有开放平台接入权限", "data": "",
        }})

        outcome = self.reconcile()

        self.assertTrue(outcome.stopped)
        self.assertEqual(outcome.stuck, 0)
        self.assertEqual(self.upstream.api_calls().__len__(), 1, "权限档之后不许再打第二发")
        self.assertEqual(self.order_status(second), "IN_PROGRESS")
        self.assertEqual(self.order_status(third), "IN_PROGRESS")
        self.assertEqual(
            self.notes(kind=jidanpu_openapi_reconcile.KIND_RECONCILE_STUCK).count(), 0,
        )

    # ── G3：单一预算 50 = 最新 15 + 轮转 35 ─────────────────────────────────

    def test_the_round_caps_at_fifty_and_always_includes_the_newest_waiting_grab(self):
        """⭐ 60 张候选 ⇒ 本轮**恰好查 50 张**，其中**一定有最新那 15 张「待抢单」**。

        夹具让钟摆窗口落在第 0 格：轮转那 35 张 = 第 0–34 张（最老的），
        最新 15 张（第 45–59 张）**只在**「最新优先」那一格里 ⇒ 少了它这一条就红。
        ⛔ 也不许出现「撞上限」的 RETRY（窗口 100 而 cap 50 那种形态）。
        """
        self.reply_status(status="代练中")
        newest: list[str] = []
        for index in range(60):
            order_no = f"2026100800000000{index:04d}"
            remote_id = f"2026100800000001{index:04d}"
            waiting = index >= 45
            order = self.make_order(
                order_no=order_no,
                status=OrderStatus.WAITING_GRAB if waiting else OrderStatus.IN_PROGRESS,
            )
            self.make_link(self.binding, order, remote_id=remote_id)
            if waiting:
                newest.append(remote_id)

        outcome = self.reconcile(now=self.NOW_LATEST)

        self.assertEqual(outcome.candidates, 60)
        self.assertEqual(outcome.queried, 50, "单一预算 = 50，多一发都不许")
        self.assertEqual(len(self.queried_order_ids()), 50)
        for remote_id in newest:
            self.assertIn(remote_id, self.queried_order_ids(), "最新那批单没排进窗口")
        self.assertFalse(outcome.stopped)
        self.assertEqual(outcome.stuck, 0)
        self.assertEqual(
            self.notes(kind="openapi.callback_query_failed").count(), 0,
            "出现了「撞上限」的 RETRY（窗口开得比 cap 大）",
        )
        self.assertEqual(
            self.notes(kind=jidanpu_openapi_reconcile.KIND_RECONCILE_CAP).count(), 1,
        )

    # ── G1：窗口起点是钟摆的纯函数 ──────────────────────────────────────────

    def test_the_window_start_is_a_pure_function_of_the_clock(self):
        """⭐ **重启不回零**：窗口起点只由「此刻的钟 + 间隔 + 宽度」决定，没有进程内状态。

        ⛔ 弄红它：把起点换回 `_RECONCILE_CURSOR`（进程字典）⇒ 清一次就回 0、而且这里
        `_window` 的签名也对不上（老签名是 `(total, *, key, cap)`）。
        """
        first = jidanpu_openapi_reconcile._window(
            total=1000, now=NOW, interval=180, width=35,
        )
        start, end = first
        self.assertEqual(end - start, 35)
        self.assertNotEqual(start, 0, "窗口起点又回头部了（发版重启后从第一张重来）")
        self.assertEqual(
            first,
            jidanpu_openapi_reconcile._window(total=1000, now=NOW, interval=180, width=35),
            "同一个时刻算出了两个起点 ⇒ 还有隐藏状态",
        )
        jidanpu_openapi_reconcile.reset_process_state()
        self.assertEqual(
            first,
            jidanpu_openapi_reconcile._window(total=1000, now=NOW, interval=180, width=35),
            "清一次进程内状态，窗口就换位置了 ⇒ 状态还在",
        )
        later = jidanpu_openapi_reconcile._window(
            total=1000, now=NOW + timedelta(seconds=180), interval=180, width=35,
        )
        self.assertEqual(
            later[0], (start + 35) % 1000, "过了一个间隔，窗口必须沿着候选表走一格",
        )


__all__ = [
    "EndToEndTests",
    "LoudFailureTests",
    "NothingToWriteTests",
    "RoundCapTests",
    "SameWriteExitTests",
    "StallFixTests",
    "WiringTests",
]
