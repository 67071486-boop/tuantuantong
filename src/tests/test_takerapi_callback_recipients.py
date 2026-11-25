"""T3：打手侧回调的**收件人**与**四类事件的入队点**（spec `2026-10-09-taker-callback-design.md`）。

本文件证什么（逐条都能单独弄红；原始变异读数见 T3 报告）：

| 组 | 钉什么 |
|---|---|
| `RecipientTests` | ⭐ spec §4.2：收件人只可能是「绑定账号 == 这张单的**当前接手人**」那个应用 —— 他自己的单 ⇒ 只给他名下的应用建行；**同租户另一个打手零行**；**孤儿单 / 没人接零行**；应用停用 / 回调停用 / 地址为空**各一条**零行 |
| `PayloadContractTests` | spec §4.1：四类事件各自建得出行，且 `payload` 的键集合**恰好**等于契约（键的字面量写在下面 `PAYLOAD_KEYS` 里，⛔ 不从被测模块派生 —— E-0024：钉值不钉常量） |
| `ReassignmentRecipientTests` | ⭐ spec §4.2 末段：`order.reassigned` 的收件人是**前任**（换人后**新**接手人的应用零行），以及反例：换人后**再发生别的事件**时前任零行 |
| `DedupTests` | spec §9-3：同一状态**连续**两次只留一条；同一 `object_key` 的图只推一次；`order.deleted` 的重复只看「在飞」——**并有一条反例**：「删 → 恢复 → 再删」必须产生**两条** |
| `CrossFamilyTests` | spec §9-7：打手侧事件**不写** `OpenApiDelivery`；反之，上家侧事件**不写** `TakerApiDelivery` |
| `OutboxTransactionTests` | ⭐ spec §7.4 的改准块：入队与业务写入**同一个事务** ⇒ **业务回滚不产生任何投递行**（⛔ 不是 `on_commit`） |
| `RealPathCoverageTests` | ⭐ **每一个新入队点都经真实业务路径触发**（`transition_order` / `soft_delete_order` / `assign_order` / `delete_order_write` / `claim_screenshot` / `store_remote_pics` / `apply_events` / `apply_upstream_events` / `_apply_screenshot`）——⛔ 不是直接调自己那几个包装（那只证明包装能跑，证明不了挂钩点接对了） |

## 夹具纪律

- 凭据 / 地址 / 单号 / 用户名一律**合成值**（`AGENTS.md` §5-C / E-0077）；回调地址用**公网地址字面量**
  （`93.184.216.34`，⛔ 不是任何真实第三方），且**一个 HTTP 都不发**（本文件只测**入队**，投递由
  `tests/test_openapi_dispatch.py` 那批用例负责）。
- 必须用 `TenantTestCase`：`apps.business` / `apps.openapi` / `apps.takerapi` 都在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import Client, SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import recycle
from apps.business.api.screenshot_views import claim_screenshot
from apps.business.jidanpu_upstream_pics import RemotePic, RemotePics, store_remote_pics
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.order_status import apply_transition
from apps.business.order_writes import (
    WriteContext,
    assign_order,
    delete_order_write,
    transition_order,
)
from apps.business.peer_inbound import _apply_screenshot, apply_events, apply_upstream_events
from apps.business.peer_sync import hash_token, new_token
from apps.core.storage import SHARED_ORDER_PREFIX, get_storage
from apps.openapi import dispatch as openapi_dispatch
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.models import ApiApp, OpenApiDelivery
from apps.openapi.models import CallbackState as OpenApiCallbackState
from apps.takerapi.api import callback_recipients
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.models import AppState, CallbackState, TakerApiDelivery, TakerApp
from apps.wecom.models import WecomAppConfig
from tests.p5_auth_helpers import grant_games, make_logged_in_client

User = get_user_model()

#: 一个**公网**地址字面量（只用于夹具；⛔ 不是任何真实第三方）。
PUBLIC_HOST = "93.184.216.34"
CALLBACK_URL = f"https://{PUBLIC_HOST}/taker/hook"

#: 四类事件的字面值（E-0024：⛔ 不从 `TakerApiDelivery.Event` 取 —— 那会让"事件名被改坏"这条不红）。
EVENT_STATUS = "order.status_changed"
EVENT_DELETED = "order.deleted"
EVENT_IMAGE = "order.image_added"
EVENT_REASSIGNED = "order.reassigned"

#: ⭐ 契约里每类事件在 outbox `payload` 列里的**恰好**键集合（spec §8-2）。
#: ⚠️ 五个通用键（`event` / `delivery_id` / `order_no` / `source_order_no` / `occurred_at`）
#: 由引擎给；多出来的那一格由**各事件**给（`status`+`note` / 无 / `images` / `reason`）。
#: ⛔ `timestamp` / `sign` **不在**库里（投递那一刻现算）—— 见
#: `tests/test_openapi_dispatch.py` 的 `SignatureTests`。
_COMMON_KEYS = {"event", "delivery_id", "order_no", "source_order_no", "occurred_at"}
PAYLOAD_KEYS = {
    EVENT_STATUS: _COMMON_KEYS | {"status", "note"},
    EVENT_DELETED: set(_COMMON_KEYS),
    EVENT_IMAGE: _COMMON_KEYS | {"images"},
    EVENT_REASSIGNED: _COMMON_KEYS | {"reason"},
}

#: 一个真的 JPEG 魔数开头的最小字节串（内部认领那条路要过魔数嗅探）。
JPEG = b"\xff\xd8\xff" + b"t3-fake-jpeg-body"

#: `IN_PROGRESS` 那道边的节点集（`order_status._TARGET_NODES`，⛔ 不 import 常量：钉值）。
NODES_IN_PROGRESS = frozenset({
    "order.change_status", "order.assign_taker", "order.submit_payout_price",
    "order.complete", "grab_hall.take_order",
})
#: 迁入 `UNASSIGNED` 的节点集。
NODES_UNASSIGNED = frozenset({
    "order.change_status", "order.clear_taker", "order.withdraw_from_grab_hall",
})
#: 迁入 `REFUNDED` 的节点集。
NODES_REFUNDED = frozenset({"order.change_status"})


def _no(n: int) -> str:
    """20 位合成订单号。"""
    return f"2026100900000000{n:04d}"


class _CallbackFixture(TenantTestCase):
    """一个租户、三个打手（各一套应用）、一款游戏、若干张单。

    ⚠️ `taker_other` 那条应用是**收件人判据的负向夹具**：它在同一个租户里、状态全开、
    地址也配了 —— 只有"绑定账号 == 当前接手人"那条判据拦得住它（"推给全租户"会放行）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="回调夹具游戏甲")
        self.taker_a = self._user("tkcb-taker-a")
        self.taker_b = self._user("tkcb-taker-b")
        self.taker_other = self._user("tkcb-taker-other")
        self.admin = self._user("tkcb-admin")
        self.app_a = self._app("回调夹具应用甲", "tkcbkey0000000001", self.taker_a)
        self.app_b = self._app("回调夹具应用乙", "tkcbkey0000000002", self.taker_b)
        self.app_other = self._app("回调夹具应用丙", "tkcbkey0000000003", self.taker_other)
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 走真改派路径的甲/乙要持本类游戏（`taker_other` 是收件人负向夹具、从不被指派，不授）。
        # 专钉：tests/test_order_assign_game_gate.py。
        grant_games(self.taker_a, self.game)
        grant_games(self.taker_b, self.game)
        #: 默认那张单：**当前接手人 = 甲**、代练中。
        self.order = self._order(1, taker=self.taker_a)

    # ── 造物 ─────────────────────────────────────────────────────────────
    def _user(self, username):
        return User.objects.create_user(username=username, password="pw-tkcb-0001")

    def _app(self, name, app_key, taker, **overrides) -> TakerApp:
        fields = {
            "name": name,
            "app_key": app_key,
            "app_secret": f"{app_key}-secret-synthetic",
            "taker": taker,
            "created_by": taker,
            "callback_url": CALLBACK_URL,
        }
        fields.update(overrides)
        return TakerApp.objects.create(**fields)

    def _order(
        self, n, *, taker=None, with_main=True, status=OrderStatus.IN_PROGRESS,
        source_order_no="T3-SRC-0001",
    ) -> Order:
        """一张单 + 一条 MAIN 绩效行。

        ⚠️ `with_main=True` + `taker=None` 是**合法形态**（抢单 / 指派之前 MAIN 行的 `user`
        就是空的）⇒ 那是"**没人接**"那一档夹具，与 `with_main=False`（**孤儿单**：
        一条 MAIN 行都没有）**分开测**（spec §4.2 的 `if taker_id is None` 覆盖两种成因）。
        """
        order = Order.objects.create(
            order_no=_no(n),
            game=self.game,
            status=status,
            total_price=Decimal("10.00"),
            hall_price=Decimal("8.00"),
            source_order_no=source_order_no,
            # `_apply_screenshot` 在 `binding=None` 时从 `order.sales_user` 取上传人。
            sales_user=self.admin,
        )
        if with_main:
            Performance.objects.create(
                order=order,
                user=taker,
                emp_type=EmpType.MAIN,
                claimed_at=timezone.now() if taker is not None else None,
            )
        return order

    def _ctx(self, *, nodes=NODES_IN_PROGRESS) -> WriteContext:
        return WriteContext(
            actor="回调夹具操作人",
            log_actor="回调夹具操作人",
            log_actor_id=self.admin.pk,
            nodes=frozenset(nodes),
            user=self.admin,
        )

    # ── 读数 ─────────────────────────────────────────────────────────────
    def rows(self) -> list[TakerApiDelivery]:
        return list(TakerApiDelivery.objects.order_by("id"))

    def pairs(self) -> list[tuple[int, str]]:
        """`[(app_id, event)]`（收件人判据的读数形状）。"""
        return [(row.app_id, row.event) for row in self.rows()]

    def only_row(self) -> TakerApiDelivery:
        rows = self.rows()
        self.assertEqual(len(rows), 1, [r.payload for r in rows])
        return rows[0]

    # ── 直接调包装的两条便捷口（**只**给判据 1/2/4 的矩阵用；入队点覆盖见最后一组）──
    def _notify_status(self, order=None, *, status=OrderStatus.IN_PROGRESS):
        return callback_recipients.notify_order_status_changed_for_taker(
            order if order is not None else self.order,
            status=status,
            occurred_at=timezone.now(),
        )

    def _notify_image(self, *, object_key=None, order=None):
        return callback_recipients.notify_order_image_added_for_taker(
            order if order is not None else self.order,
            object_key=object_key or f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg",
            img_type=0,
            remark="",
            occurred_at=timezone.now(),
        )


# ── ① 收件人（spec §4.2 / §9-1）─────────────────────────────────────────────


class RecipientTests(_CallbackFixture):
    """⭐ 收件人**只可能**是「绑定账号 == 这张单的当前接手人」那个应用。"""

    def test_only_the_current_takers_app_gets_a_row(self):
        """他自己接的单 ⇒ **只**给他名下的应用建行（另外两个打手的应用零行）。

        失败方向：把 `taker_apps_for` 的 `taker_id=` 去掉（退化成"本租户全部启用应用"）
        ⇒ 本用例红成三行（那正是上家侧的语义，spec §4.2 明写不许退化）。
        """
        self._notify_status()
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_STATUS)])

    def test_another_takers_app_in_the_same_tenant_gets_nothing(self):
        """同租户**另一个打手**的应用 ⇒ **零行**（"推给全租户"会放行它）。"""
        self._notify_status()
        for app in (self.app_b, self.app_other):
            with self.subTest(app=app.name):
                self.assertNotIn(
                    app.pk, [row.app_id for row in self.rows()],
                    "同一租户里**别人**的应用收到了这一单的推送（收件人判据退化了）",
                )

    def test_an_orphan_order_gets_nothing(self):
        """⭐ **孤儿单**（这张单一条 MAIN 绩效行都没有）⇒ **零行**（spec §9-1 点名的反例）。"""
        order = self._order(2, taker=None, with_main=False)
        self._notify_status(order)
        self.assertEqual(self.rows(), [], "孤儿单也推了（`taker_id is None` 那一支没兜住）")

    def test_an_unclaimed_order_gets_nothing(self):
        """⭐ **没人接**（MAIN 行在、`user` 为空）⇒ **零行**（与孤儿单**不同**的成因，同一出口）。"""
        order = self._order(3, taker=None, with_main=True)
        self._notify_status(order)
        self.assertEqual(self.rows(), [], "没人的单也推了")

    def test_a_disabled_app_gets_nothing(self):
        """应用 `state` 停用 ⇒ 零行（三条判据①：停用应用停整条对外通道）。"""
        self.app_a.state = AppState.DISABLED
        self.app_a.save(update_fields=["state"])
        self._notify_status()
        self.assertEqual(self.rows(), [])

    def test_an_app_with_callbacks_switched_off_gets_nothing(self):
        """回调开关停用 ⇒ 零行（判据②：停用回调只停推送）。⚠️ 应用本身仍启用。"""
        self.app_a.callback_state = CallbackState.DISABLED
        self.app_a.save(update_fields=["callback_state"])
        self.assertEqual(self.app_a.state, AppState.ENABLED)
        self._notify_status()
        self.assertEqual(self.rows(), [])

    def test_an_app_with_an_empty_callback_url_gets_nothing(self):
        """回调地址为空 ⇒ 零行（判据③）。⚠️ 两个开关都还开着。"""
        self.app_a.callback_url = ""
        self.app_a.save(update_fields=["callback_url"])
        self.assertEqual(self.app_a.state, AppState.ENABLED)
        self.assertEqual(self.app_a.callback_state, CallbackState.ENABLED)
        self._notify_status()
        self.assertEqual(self.rows(), [])

    def test_only_the_enabled_app_of_the_same_taker_is_picked(self):
        """同一个打手配了**两套**应用、只有一套可用 ⇒ 只给可用那套建行（收件人是"应用"不是"人"）。"""
        second = self._app(
            "回调夹具应用甲二", "tkcbkey0000000004", self.taker_a,
            callback_url="",
        )
        self._notify_status()
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_STATUS)])
        self.assertNotIn(second.pk, [row.app_id for row in self.rows()])


# ── ①b `TAKER_SPEC.callback_enabled_value` **按值**钉住（T3 评审挂账 ②）──────────


class SpecCallbackEnabledValueTests(SimpleTestCase):
    """⭐ 收件人那条判据的**取值来源**（`TAKER_SPEC.callback_enabled_value`）按**值**钉住。

    ## 为什么需要这一条（挂账的来由）

    收件人判据（`callback_recipients.taker_apps_for`）按 `TAKER_SPEC.callback_enabled_value`
    这个**字符串**去比 `TakerApp.callback_state` 那一列。而打手侧的两个枚举
    （`AppState` / `CallbackState`）**今天成员名与取值逐字相同**（都是 `enabled` / `disabled`）
    ⇒ 那一格若写成 `AppState.ENABLED`，`RecipientTests` 那八条用例**一条都不会红**
    （两个值都是 `"enabled"`），而语义已经错了：**回调开关那一列**的 choices 是
    `CallbackState`（`spec.callback_state_field = "callback_state"`），与 `AppState` 是
    **两个独立开关**（模型 docstring 逐字）。⇒ 挂账要求补一条**能在那一刻变红**的判据。

    ## 这条断言**为什么能红**（两条腿**都要**，缺一条就会被"值恰好相同"盖住）

    1. `== "enabled"`（**字面值**，⛔ 不从任何枚举取）：把这一格改成任意别的字符串 ⇒ 红；
    2. `is CallbackState.ENABLED`（**同一性**，⛔ 不用 `==`）：把这一格写回
       `AppState.ENABLED` ⇒ 红 —— ⚠️ **这一条正是"值相同也拦得住"的那条腿**：
       两个枚举今天同值，`==` 判不出差别，只有 `is`（枚举成员是**单例**）判得出。

    ⚠️ **弄红它（实测过）**：把 `apps/takerapi/credentials.py` 那一行临时改回
    `callback_enabled_value=AppState.ENABLED` ⇒ 腿 1 仍绿（值同为 `"enabled"`）、
    **腿 2 当场红**（`AssertionError: ... is not CallbackState.ENABLED`）——
    原始读数抄在 T5 报告里。还原后两条腿都绿。
    """

    def test_the_spec_value_is_the_callback_enums_enabled_member(self):
        # 腿 1：**字面值**（改坏了任何一端都会红；⛔ 不写 `CallbackState.ENABLED.value`，
        #        那样"两处一起改成 on"会自洽地全绿 —— E-0024 的自指形状）。
        self.assertEqual(TAKER_SPEC.callback_enabled_value, "enabled")
        # 腿 2：**同一性**（今天两枚举同值 ⇒ 只有它拦得住"写回 AppState.ENABLED"）。
        self.assertIs(
            TAKER_SPEC.callback_enabled_value,
            CallbackState.ENABLED,
            "`callback_enabled_value` 必须取**回调开关**那个枚举的成员"
            "（`CallbackState`），⛔ 不是 `AppState` —— 两个枚举今天取值逐字相同，"
            "写错了不会有任何用例红，而语义上是「用应用开关的字面值去比回调开关那一列」",
        )


# ── ② 四类事件各自的载荷契约（spec §4.1 / §8-2 / §9-2）──────────────────────

class PayloadContractTests(_CallbackFixture):
    """四类事件各自建得出行，且 `payload` 的键集合**恰好**等于契约。"""

    def test_order_status_changed(self):
        callback_recipients.notify_order_status_changed_for_taker(
            self.order, status=OrderStatus.PAUSED, occurred_at=timezone.now()
        )
        row = self.only_row()
        self.assertEqual(row.event, EVENT_STATUS)
        self.assertEqual(set(row.payload), PAYLOAD_KEYS[EVENT_STATUS])
        self.assertEqual(row.payload["status"], str(OrderStatus.PAUSED))
        self.assertEqual(row.payload["note"], "")
        self.assertEqual(row.order_no, self.order.order_no)
        self.assertEqual(row.source_order_no, "T3-SRC-0001")
        self.assertEqual(row.status, TakerApiDelivery.Status.PENDING)

    def test_order_deleted(self):
        callback_recipients.notify_order_deleted_for_taker(
            self.order, occurred_at=timezone.now()
        )
        row = self.only_row()
        self.assertEqual(row.event, EVENT_DELETED)
        self.assertEqual(set(row.payload), PAYLOAD_KEYS[EVENT_DELETED])

    def test_order_image_added(self):
        key = f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg"
        callback_recipients.notify_order_image_added_for_taker(
            self.order, object_key=key, img_type=0, remark="回调夹具备注", occurred_at=timezone.now()
        )
        row = self.only_row()
        self.assertEqual(row.event, EVENT_IMAGE)
        self.assertEqual(set(row.payload), PAYLOAD_KEYS[EVENT_IMAGE])
        # ⚠️ 库里存的是**稳定身份**（`object_key`），⛔ 不是 300 秒就过期的预签名 URL
        #    （引擎的 `_wire_payload` 在**投递那一刻**才把 key 换成 url）。
        self.assertEqual(len(row.payload["images"]), 1)
        entry = row.payload["images"][0]
        self.assertEqual(entry["object_key"], key)
        self.assertNotIn("url", entry)
        self.assertEqual(entry["img_type"], 0)
        self.assertEqual(entry["remark"], "回调夹具备注")

    def test_the_wrapper_does_not_invent_an_img_type(self):
        """⛔ 不是本组的目标（这里只钉「包装不会自己发明一个 `img_type`」：值原样落到载荷里）。

        群聊二维码（`img_type=1`）在**接收侧**被 `_apply_screenshot` 拒掉（入队点 6 那条），
        而**入队包装**本身不判 `img_type`——判据在写入点那一层，⛔ 不是在这里。
        """
        callback_recipients.notify_order_image_added_for_taker(
            self.order, object_key="shared/x.jpg", img_type=1, remark="", occurred_at=timezone.now()
        )
        self.assertEqual(self.only_row().payload["images"][0]["img_type"], 1)

    def test_order_reassigned(self):
        callback_recipients.notify_order_reassigned_for_taker(
            self.order,
            former_taker_id=self.taker_a.pk,
            reason="taker_removed",
            occurred_at=timezone.now(),
        )
        row = self.only_row()
        self.assertEqual(row.event, EVENT_REASSIGNED)
        self.assertEqual(set(row.payload), PAYLOAD_KEYS[EVENT_REASSIGNED])
        self.assertEqual(row.payload["reason"], "taker_removed")
        # 收件人是**前任**（= 甲）。
        self.assertEqual(row.app_id, self.app_a.pk)


# ── ③ `order.reassigned` 的收件人是"前任"（spec §4.2 末段）────────────────────


class ReassignmentRecipientTests(_CallbackFixture):
    """⭐ 换人 ⇒ **旧**接手人的应用有一行、**新**接手人的应用零行。"""

    def _swap_to_b(self):
        """走真实业务路径换人（`assign_order`）。单已是 `IN_PROGRESS` ⇒ 不走状态机那一支
        （R-3），因此这一次动作**只**产出 `order.reassigned` 一条事件。"""
        outcome = assign_order(
            self.order, taker=self.taker_b, ctx=self._ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        return outcome

    def test_the_former_taker_gets_it_and_the_new_taker_does_not(self):
        self._swap_to_b()
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_REASSIGNED)])
        self.assertNotIn(
            self.app_b.pk, [row.app_id for row in self.rows()],
            "换人后**新**接手人的应用收到了 order.reassigned（他以为自己刚被撤单）",
        )
        self.assertEqual(self.only_row().payload["reason"], "taker_removed")
        self.order.refresh_from_db()
        self.assertEqual(
            self.order.performances.get(emp_type=EmpType.MAIN).user_id, self.taker_b.pk
        )

    def test_after_the_swap_the_former_taker_gets_nothing_else(self):
        """⭐ **反例**：换人**之后**再发生别的事件 ⇒ 前任（甲）的应用**零新增行**。

        这时"当前接手人"已经是乙 ⇒ `order.deleted` 只该给乙。
        失败方向：把收件人判据写成"曾经接过单的人都要推" ⇒ 本用例红成两行给甲。
        """
        self._swap_to_b()
        before = len(self.rows())                       # 1 行（reassigned → 甲）
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        rows = self.rows()
        self.assertEqual(len(rows), before + 1, [r.payload for r in rows])
        self.assertEqual(rows[-1].event, EVENT_DELETED)
        self.assertEqual(
            rows[-1].app_id, self.app_b.pk,
            "删单事件推给了**前任**（甲）—— 那时接手人已经是乙",
        )
        self.assertEqual(
            [row.app_id for row in rows].count(self.app_a.pk), 1,
            "甲的应用在换人之后又收到了别的事件",
        )

    def test_a_no_op_assign_writes_nothing(self):
        """A→A（同一个人再保存）⇒ 一行都不写（与那条既有 peer 撤单事件同口径）。"""
        assign_order(self.order, taker=self.taker_a, ctx=self._ctx(), dry_run=False)
        self.assertEqual(self.rows(), [])

    def test_the_recipient_is_the_former_even_if_he_has_no_app_any_more(self):
        """前任把应用停用了 ⇒ 零行（收件人 = 前任**名下可用的**应用），而不是回落到现任。"""
        self.app_a.state = AppState.DISABLED
        self.app_a.save(update_fields=["state"])
        self._swap_to_b()
        self.assertEqual(self.rows(), [])


# ── ④ 去重（spec §9-3）─────────────────────────────────────────────────────


class DedupTests(_CallbackFixture):
    """⭐ 去重判据（引擎的 `dispatch._is_duplicate` + `_identity_of`）。"""

    def test_the_same_status_twice_in_a_row_is_enqueued_once(self):
        """同一状态**连续**推三次 ⇒ **一条**（at-least-once 的常态）。

        失败方向：去掉 `_is_duplicate`（或把身份值写错）⇒ 本用例红成三条。
        """
        for _ in range(3):
            self._notify_status(status=OrderStatus.IN_PROGRESS)
        self.assertEqual(len(self.rows()), 1, [r.payload for r in self.rows()])

    def test_a_status_returning_after_another_one_is_a_new_event(self):
        """⭐ `A → B → A`：第三个 `A` 是**新事件**（引擎 docstring 逐字：`X → Y → X` 的两条 `X`
        都发得出去）——判据是「与**最近一条**比身份」，不是为了某个状态只留一份。

        ⚠️ 与 `order.deleted` 那一档的差别**不是严不严**，而是**报文能不能区分**：
        两次删除在报文里长得一模一样，而 `A → B → A` 是两件不同的事（对方要看到"回到了 A"）。
        失败方向：把判据改成"这个状态历史上出现过就跳过" ⇒ 本用例红成 `[A, B]`。
        """
        for status in (OrderStatus.IN_PROGRESS, OrderStatus.PAUSED, OrderStatus.IN_PROGRESS):
            self._notify_status(status=status)
        self.assertEqual(
            [row.payload["status"] for row in self.rows()],
            [str(OrderStatus.IN_PROGRESS), str(OrderStatus.PAUSED), str(OrderStatus.IN_PROGRESS)],
        )

    def test_the_same_image_key_is_enqueued_once(self):
        key = f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg"
        for _ in range(2):
            self._notify_image(object_key=key)
        self.assertEqual(len(self.rows()), 1)

    def test_a_different_image_key_is_a_new_event(self):
        """正对照：不同的图是**不同的事**，⛔ 不许被去重吃掉。"""
        for _ in range(2):
            self._notify_image()
        self.assertEqual(len(self.rows()), 2)

    def test_a_second_delete_in_the_pending_window_is_enqueued_once(self):
        """在飞（`pending`）窗口里的第二次删除 ⇒ 一条。"""
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        self.assertEqual(len(self.rows()), 1)
        self.order.refresh_from_db()
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        self.assertEqual(len(self.rows()), 1)

    def test_a_delete_after_a_restore_is_enqueued_again(self):
        """⭐⭐ **反例**：「删 → 恢复 → 再删」的第二次删除**必须再排一条**。

        失败方向：把删除那一档的判据放宽成"最近一条行存在就跳过"（⛔ 不看它是不是**在飞**）
        ⇒ 本用例红——而症状是**对方永远不知道这单现在是删的**（一条静默丢事件）。
        """
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        first = self.only_row()
        # 模拟"第一行已经投出去了"（终态 `done`）。
        first.status = TakerApiDelivery.Status.DONE
        first.processed_at = timezone.now()
        first.save(update_fields=["status", "processed_at"])
        self.order.refresh_from_db()
        recycle.restore_order(order=self.order, actor=self.admin)
        self.order.refresh_from_db()
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        rows = self.rows()
        self.assertEqual(
            len(rows), 2,
            "删 → 恢复 → 再删 的第二次删除被'最近一条行'吃掉了（对方永远不知道这单是删的）",
        )
        self.assertTrue(all(row.event == EVENT_DELETED for row in rows))

    def test_the_deleted_row_is_kept_when_the_app_is_gone(self):
        """⛔ 不是去重那一档的判据（这里钉"零行"与"被别人吃了"要分得开）：
        应用全停用 ⇒ 删两次也是零行。"""
        self.app_a.state = AppState.DISABLED
        self.app_a.save(update_fields=["state"])
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        self.assertEqual(self.rows(), [])


# ── ⑤ 跨族不串台（spec §9-7）───────────────────────────────────────────────


class CrossFamilyTests(_CallbackFixture):
    """⭐ 打手侧事件**不写** `OpenApiDelivery`；上家侧事件**不写** `TakerApiDelivery`。"""

    def setUp(self):
        super().setUp()
        #: 一行**上家侧**应用（两个开关都可用）——它同时是"串台"的诱饵与反向对照的收件人。
        self.api_app = ApiApp.objects.create(
            name="回调夹具上家应用",
            app_key="tkcb-open-key-00000001",
            app_secret="tkcb-open-secret-synthetic-0001",
            callback_url=CALLBACK_URL,
            callback_state=OpenApiCallbackState.ENABLED,
            created_by=self.admin,
        )

    def test_a_taker_side_event_does_not_write_the_upstream_ledger(self):
        callback_recipients.notify_order_status_changed_for_taker(
            self.order, status=OrderStatus.IN_PROGRESS, occurred_at=timezone.now()
        )
        self.assertEqual(
            OpenApiDelivery.objects.count(), 0,
            "打手侧的事件写进了**上家侧**那张台账（spec §2.2 最贵的那个形状）",
        )
        self.assertEqual(TakerApiDelivery.objects.count(), 1)

    def test_an_upstream_side_event_does_not_write_the_taker_ledger(self):
        openapi_dispatch.notify_order_status_changed(
            self.order, spec=OPENAPI_SPEC, status=OrderStatus.IN_PROGRESS,
            occurred_at=timezone.now(),
        )
        self.assertEqual(TakerApiDelivery.objects.count(), 0)
        self.assertEqual(OpenApiDelivery.objects.count(), 1)

    def test_both_families_coexist_side_by_side(self):
        """两族**同时**各有一条时，两条落在**各自**那张表上（同一张单、两个当事人）。"""
        callback_recipients.notify_order_status_changed_for_taker(
            self.order, status=OrderStatus.IN_PROGRESS, occurred_at=timezone.now()
        )
        openapi_dispatch.notify_order_status_changed(
            self.order, spec=OPENAPI_SPEC, status=OrderStatus.IN_PROGRESS,
            occurred_at=timezone.now(),
        )
        self.assertEqual(TakerApiDelivery.objects.count(), 1)
        self.assertEqual(OpenApiDelivery.objects.count(), 1)
        self.assertEqual(
            TakerApiDelivery.objects.get().app_id, self.app_a.pk
        )
        self.assertEqual(OpenApiDelivery.objects.get().app_id, self.api_app.pk)


# ── ⑥ 入队与业务写入同一个事务（spec §7.4 的改准块）──────────────────────────


class OutboxTransactionTests(_CallbackFixture):
    """⭐ **业务回滚 ⇒ 一行投递行都不留**（⛔ 不是 `on_commit`）。"""

    def setUp(self):
        super().setUp()
        #: 这张单从「待抢单」开始，好让 `transition_order` 真的走一次状态机。
        self.hall_order = self._order(4, taker=self.taker_a, status=OrderStatus.WAITING_GRAB)

    def test_a_rolled_back_transition_leaves_no_taker_delivery_row(self):
        """驱动的是**真实**领域函数（`transition_order`），⛔ 不是直接调包装：
        后者证明不了"入队点在业务事务里"这件事。"""
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                outcome = transition_order(
                    self.hall_order, target=OrderStatus.IN_PROGRESS,
                    ctx=self._ctx(), dry_run=False,
                )
                self.assertTrue(outcome.ok, outcome)
                self.assertEqual(
                    TakerApiDelivery.objects.count(), 1,
                    "入队必须发生在**同一个**事务里（此刻应已看到那一行）",
                )
                raise RuntimeError("故意回滚")
        self.assertEqual(
            TakerApiDelivery.objects.count(), 0,
            "业务回滚了、投递行却留下了 ⇒ 入队不在同一个事务里（`on_commit` 的形态）",
        )
        self.hall_order.refresh_from_db()
        self.assertEqual(self.hall_order.status, OrderStatus.WAITING_GRAB)

    def test_a_committed_transition_leaves_exactly_one_row(self):
        """正对照：同一段代码提交之后**恰好一行**（否则上面那条会因为"压根不入队"而假绿）。"""
        with transaction.atomic():
            outcome = transition_order(
                self.hall_order, target=OrderStatus.IN_PROGRESS,
                ctx=self._ctx(), dry_run=False,
            )
            self.assertTrue(outcome.ok, outcome)
        row = self.only_row()
        self.assertEqual(row.event, EVENT_STATUS)
        self.assertEqual(row.app_id, self.app_a.pk)
        self.assertEqual(row.payload["status"], str(OrderStatus.IN_PROGRESS))

    def test_status_notice_and_both_callback_ledgers_roll_back_for_each_entry(self):
        order = self.order
        order.sales_agent = "回调夹具客服"
        order.save(update_fields=["sales_agent"])
        enabled_at = timezone.now()
        Order.objects.filter(pk=order.pk).update(updated_at=enabled_at - timedelta(minutes=2))
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name, enabled=True, corp_id="synthetic-status-corp",
            agent_id=1, secret="synthetic-status-secret", order_status_enabled=True,
            order_status_enabled_at=enabled_at,
        )
        api_app = ApiApp.objects.create(
            name="状态落库回调夹具", app_key="tkcb-open-status-0001",
            app_secret="tkcb-open-status-secret-synthetic", callback_url=CALLBACK_URL,
            callback_state=OpenApiCallbackState.ENABLED, created_by=self.admin,
        )
        staff = make_logged_in_client(username="tkcb-status-admin", is_superuser=True)
        token = new_token()
        PeerDownstreamBinding.objects.create(
            account=self.taker_a, peer_base_url="https://status-peer.test.invalid",
            token_in_hash=hash_token(token), token_out=new_token(),
        )
        peer = Client(SERVER_NAME="tenant.test.com")
        peer_url = "/api/business/peer-sync/inbound/events/"
        peer_headers = {"HTTP_AUTHORIZATION": "Bearer " + token}

        def peer_request(event_at):
            return peer, peer_url, {"events": [{
                "kind": "order.update",
                "target_ref": order.order_no,
                "occurred_at": event_at.isoformat(),
                "payload": {"status": "ISSUE"},
            }]}, peer_headers

        local_request = (
            staff, f"/api/business/orders/{order.order_no}/transition/", {"target": "ISSUE"}, {},
        )
        scenarios = (
            ("local", "order.transition", True, local_request),
            ("peer after enable", "peer.upstream_status", True,
             peer_request(enabled_at + timedelta(minutes=2))),
            ("peer before enable", "peer.upstream_status", False,
             peer_request(enabled_at - timedelta(minutes=1))),
        )
        for scenario, action, expect_notice, (client, url, body, headers) in scenarios:
            logs = OperationLog.objects.filter(order_no=order.order_no, action=action)
            with self.subTest(scenario=scenario):
                with transaction.atomic():
                    started = timezone.now()
                    response = client.post(url, body, content_type="application/json", **headers)
                    finished = timezone.now()
                    self.assertEqual(response.json()["code"], 0, response.json())
                    order.refresh_from_db()
                    self.assertEqual(order.status, OrderStatus.ISSUE)
                    self.assertEqual(order.wecom_notice_pending, expect_notice)
                    self.assertEqual(
                        [row["lookup_name"] for row in order.wecom_notice_journal],
                        ["回调夹具客服"] if expect_notice else [],
                    )
                    taker = TakerApiDelivery.objects.get()
                    upstream = OpenApiDelivery.objects.get()
                    self.assertEqual((taker.app_id, upstream.app_id), (self.app_a.pk, api_app.pk))
                    for row in (taker, upstream):
                        self.assertEqual(row.event, "order.status_changed")
                        self.assertEqual(row.payload["status"], "ISSUE")
                    self.assertEqual(taker.payload["occurred_at"], upstream.payload["occurred_at"])
                    callback_at = datetime.fromisoformat(taker.payload["occurred_at"])
                    self.assertLessEqual(started, callback_at)
                    self.assertLessEqual(callback_at, finished)
                    self.assertEqual(logs.count(), 1)
                    transaction.set_rollback(True)

                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
                self.assertFalse(order.wecom_notice_pending)
                self.assertEqual(order.wecom_notice_journal, [])
                self.assertFalse(TakerApiDelivery.objects.exists())
                self.assertFalse(OpenApiDelivery.objects.exists())
                self.assertFalse(logs.exists())


# ── ⑦ 每个新入队点的真实路径覆盖（spec §4.3 那张表逐行）──────────────────────


class RealPathCoverageTests(_CallbackFixture):
    """⭐ 逐行覆盖 spec §4.3 那张入队点全集 —— 全部走**真实业务路径**。"""

    # ── `order.status_changed` ───────────────────────────────────────────
    def test_the_state_machine_enqueues_a_status_event(self):
        """入队点 1：`order_status.apply_transition`（经真实的 `transition_order`）。"""
        order = self._order(5, taker=self.taker_a, status=OrderStatus.WAITING_GRAB)
        with transaction.atomic():
            outcome = transition_order(
                order, target=OrderStatus.IN_PROGRESS, ctx=self._ctx(), dry_run=False
            )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_STATUS)])
        self.assertEqual(self.only_row().payload["status"], str(OrderStatus.IN_PROGRESS))

    def _upstream_link(self, order):
        """给这张单挂一条**生效的上游绑定**（下游接收侧那三条入队点都要它）。"""
        binding = PeerUpstreamBinding.objects.create(
            peer_name="回调夹具工作室甲",
            peer_base_url="https://peer-t3.test.invalid",
            peer_account="peer-t3-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-t3-local.test.invalid",
            bound_by=self.admin,
        )
        PeerOrderLink.objects.create(
            order=order,
            upstream_binding=binding,
            peer_order_no="T3-PEER-1",
            last_applied_at=timezone.now() - timedelta(minutes=5),
        )
        return binding

    def _down_event(self, order, *, kind, status, occurred_at):
        return {
            "kind": kind,
            "target_ref": "T3-PEER-1",
            "occurred_at": occurred_at.isoformat(),
            "payload": {
                "status": status,
                "hall_price": "8.00",
                "total_price": "10.00",
                "game_name": self.game.name,
            },
        }

    def test_the_peer_update_path_enqueues_a_status_event(self):
        """入队点 2：`peer_inbound.apply_events` 的 `order.update` 支（下游同步接收侧）。"""
        order = self._order(6, taker=self.taker_a)
        binding = self._upstream_link(order)
        moment = timezone.now()
        outcome = apply_events(
            [self._down_event(order, kind="order.update", status=OrderStatus.ISSUE,
                              occurred_at=moment)],
            binding=binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_STATUS)])
        self.assertEqual(self.only_row().payload["status"], str(OrderStatus.ISSUE))

    def test_the_peer_assign_path_enqueues_reassigned_to_the_former_taker(self):
        """入队点 3：`peer_inbound.apply_events` 的 `order.assign` / `order.grab` /
        `order.withdraw` 支（同一处三 kind）。

        ⭐ D-0343改准：接手类事件把**有人的**单打回未分配时，同一事务
        清掉我方接手人（回池补 D-0032）⇒ 打手侧入队的是给**前任**的
        `order.reassigned`（`reason=taker_removed`，收件人=前任是本类事件唯一形状）；
        `order.status_changed` 的收件人取**当前**接手人——人已清空 ⇒ **零行**
        （本用例在 D-0343 之前钉的「一条 status_changed」已不是事实，合并树
        run204 s1 / run205 s13 两轮同点实测红后随线改准）。
        """
        order = self._order(7, taker=self.taker_a)
        binding = self._upstream_link(order)
        outcome = apply_events(
            [self._down_event(order, kind="order.assign", status=OrderStatus.IN_PROGRESS,
                              occurred_at=timezone.now())],
            binding=binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        order.refresh_from_db()
        # ⭐ P4 口径：接手类事件 ⇒ 我方这张单落成**未分配**（⛔ 不照抄对端状态）。
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # ⭐ D-0343：前任收到「不再是你的了」；当前接手人已清空 ⇒ 没有 status_changed 行。
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_REASSIGNED)])
        self.assertEqual(self.only_row().payload["reason"], "taker_removed")

    def test_the_upstream_feedback_path_enqueues_a_status_event(self):
        """入队点 4：`peer_inbound.apply_upstream_events`（上行回传，绕过状态机）。"""
        order = self._order(8, taker=self.taker_a)
        binding = PeerDownstreamBinding.objects.create(
            account=self.taker_a,
            peer_base_url="https://peer-up.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        outcome = apply_upstream_events(
            [{
                "kind": "order.update",
                "target_ref": order.order_no,
                "occurred_at": (timezone.now() + timedelta(seconds=30)).isoformat(),
                "payload": {"status": "PENDING_REVIEW"},
            }],
            binding=binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        order.refresh_from_db()
        self.assertEqual(order.status, "PENDING_REVIEW")
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_STATUS)])
        self.assertEqual(self.only_row().payload["status"], "PENDING_REVIEW")

    # ── `order.image_added` ─────────────────────────────────────────────
    def test_the_internal_claim_enqueues_an_image_event(self):
        """入队点 5：`business/api/screenshot_views.claim_screenshot`（内部认领，走真 HTTP 端点）。"""
        client = make_logged_in_client(
            username="tkcb-shot-admin",
            groups=("系统管理员",),
            nodes=("order.upload_screenshot", "order.view_screenshot"),
        )
        base = f"/api/business/orders/{self.order.order_no}/screenshots"
        ticket = client.post(f"{base}/ticket", {"ext": "jpg", "img_type": 0}, format="json")
        row_id = ticket.data["data"]["screenshot_id"]
        shot = OrderScreenshot.objects.get(pk=row_id)
        get_storage().put_bytes(shot.object_key, JPEG, content_type="image/jpeg")
        claimed = client.post(base, {"screenshot_id": row_id}, format="json")
        self.assertEqual(claimed.status_code, 200, claimed.content[:300])
        self.assertEqual(claimed.data["code"], 0, claimed.data)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_IMAGE)])
        self.assertEqual(
            self.only_row().payload["images"][0]["object_key"], shot.object_key
        )
        # 恰好一行（认领这一步只排一条图片事件）。
        self.assertEqual(len(self.rows()), 1)

    def test_the_claim_domain_function_is_the_same_site(self):
        """入队点 5 的**领域函数**那一半：`claim_screenshot` 直接调也走同一处入队。"""
        client = make_logged_in_client(
            username="tkcb-shot-admin-2",
            groups=("系统管理员",),
            nodes=("order.upload_screenshot", "order.view_screenshot"),
        )
        base = f"/api/business/orders/{self.order.order_no}/screenshots"
        ticket = client.post(f"{base}/ticket", {"ext": "jpg", "img_type": 0}, format="json")
        row_id = ticket.data["data"]["screenshot_id"]
        shot = OrderScreenshot.objects.get(pk=row_id)
        get_storage().put_bytes(shot.object_key, JPEG, content_type="image/jpeg")
        claimed = claim_screenshot(
            order_no=self.order.order_no,
            uploaded_by=shot.uploaded_by,
            screenshot_id=row_id,
        )
        self.assertEqual(claimed.code, 0, claimed)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_IMAGE)])

    def test_the_peer_screenshot_write_enqueues_an_image_event(self):
        """入队点 6：`peer_inbound._apply_screenshot`（同步接收侧的进度图写入）。"""
        key = f"{SHARED_ORDER_PREFIX}/{self.order.order_no}/{uuid.uuid4().hex}.jpg"
        applied = _apply_screenshot(
            self.order,
            kind="order.screenshot.add",
            payload={"object_key": key, "img_type": 0, "remark": "同步进来的图"},
            binding=None,
            target_ref="T3-PEER-1",
            note_skip=lambda *args, **kwargs: None,
            direction="down",
        )
        self.assertTrue(applied)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_IMAGE)])
        self.assertEqual(self.only_row().payload["images"][0]["object_key"], key)

    def test_the_remote_pull_path_enqueues_an_image_event(self):
        """入队点 7：`jidanpu_upstream_pics.store_remote_pics`（把远端那张单上的图拉回来入库）。"""
        outcome = store_remote_pics(
            RemotePics(items=(RemotePic(src="https://oss.test.invalid/a.jpg", alt=""),), rows=1),
            order=self.order,
            uploaded_by=self.admin,
            now=timezone.now(),
            download=lambda src: JPEG,
        )
        self.assertTrue(outcome.pulled, outcome)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_IMAGE)])
        self.assertEqual(
            len(self.only_row().payload["images"]), 1,
            "拉回来的那张图必须**只**排一条图片事件",
        )

    # ── `order.deleted` ─────────────────────────────────────────────────
    def test_the_soft_delete_enqueues_a_deleted_event(self):
        """入队点 8：`business/recycle.soft_delete_order`。"""
        recycle.soft_delete_order(order=self.order, actor=self.admin, is_del_emp="N")
        row = self.only_row()
        self.assertEqual(row.event, EVENT_DELETED)
        self.assertEqual(row.app_id, self.app_a.pk)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.deleted_at)
        # ⚠️ 时刻是**显式取**的删除时刻（软删不改 `Order.updated_at`）。
        self.assertEqual(row.payload["occurred_at"], self.order.deleted_at.isoformat())

    # ── `order.reassigned`（四处）────────────────────────────────────────
    def test_the_pool_entry_enqueues_a_reassignment(self):
        """入队点 9：`performance._on_pool_entry`（清接手人 / 撤单回池那条漏斗）。

        走真实入口 `apply_transition(..., allow_taker_release=True)` —— 那正是
        `/clear-taker/` 视图的调用形状（D2 例外①）。
        """
        with transaction.atomic():
            apply_transition(
                self.order, OrderStatus.UNASSIGNED,
                nodes=NODES_UNASSIGNED, actor="回调夹具操作人",
                actor_id=self.admin.pk, allow_taker_release=True,
            )
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_REASSIGNED)])
        self.assertEqual(self.only_row().payload["reason"], "taker_removed")
        self.order.refresh_from_db()
        self.assertIsNone(
            self.order.performances.get(emp_type=EmpType.MAIN).user_id,
            "回池之后库里那条 MAIN 行的 user 应当已被清空"
            "（收件人只能靠入队点手上那个『前任』局部变量）",
        )

    def test_the_real_reassignment_enqueues_a_reassignment(self):
        """入队点 10：`order_writes.assign_order` 的真换人支。"""
        outcome = assign_order(
            self.order, taker=self.taker_b, ctx=self._ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(self.pairs(), [(self.app_a.pk, EVENT_REASSIGNED)])

    def test_the_soft_delete_write_enqueues_both_deleted_and_reassignment(self):
        """入队点 11：`order_writes.delete_order_write` 的软删支。

        ⚠️ 同一张单被删时**两条都要有**（spec §4.1 末段：`order.deleted` 说"这单不存在了"、
        `order.reassigned(reason=deleted)` 说"你不该再干它"）——⛔ 这不是重复。
        """
        outcome = delete_order_write(
            self.order, is_del_emp="N", ctx=self._ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(
            self.pairs(),
            [(self.app_a.pk, EVENT_DELETED), (self.app_a.pk, EVENT_REASSIGNED)],
        )
        reasons = [row.payload.get("reason") for row in self.rows()]
        self.assertEqual(reasons, [None, "deleted"])

    def test_the_refunded_hook_enqueues_a_reassignment(self):
        """入队点 12：`settlement_hooks` 的 `REFUNDED` 钩子（改已退单）。"""
        with transaction.atomic():
            apply_transition(
                self.order, OrderStatus.REFUNDED,
                nodes=NODES_REFUNDED, actor="回调夹具操作人", actor_id=self.admin.pk,
            )
        rows = self.rows()
        reassigned = [row for row in rows if row.event == EVENT_REASSIGNED]
        self.assertEqual(len(reassigned), 1, [r.payload for r in rows])
        self.assertEqual(reassigned[0].app_id, self.app_a.pk)
        self.assertEqual(reassigned[0].payload["reason"], "refunded")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)

    # ── 零应用即零成本（贯穿所有入队点的那条纪律）────────────────────────
    def test_no_usable_app_means_no_row_on_the_hottest_path(self):
        """没有可用应用 ⇒ 一个入队点都**不写行**（退出早、不碰存储、不发 HTTP）。"""
        TakerApp.objects.update(callback_url="")
        with transaction.atomic():
            outcome = transition_order(
                self.order, target=OrderStatus.PAUSED, ctx=self._ctx(), dry_run=False
            )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(self.rows(), [])
