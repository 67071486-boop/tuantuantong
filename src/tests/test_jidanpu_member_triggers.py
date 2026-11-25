"""下游鸡蛋铺线 **触发点接线**（spec §5.2 / §5.3 / §5.4 / §8.4）。本文件回答的不是「写台账的那个函数对不对」（那是 `test_jidanpu_member_consume.py` 的 `QueueMemberEventTests`），而是**每个业务动作到底有没有把那一行接上**——挂钩挂错地方 / 根本没挂上在这类改动里是**静默**的（内核单元测试全绿）。九处触发（spec §5.3 六行 + §5.2「绑定那一刻的补推」+ 上家接手事件改项目 + 绑定百分比变更）逐行落点与期望台账：抛单到抢单大厅 `POST /orders/<单号>/throw-to-hall/` ⇒ `member.launch`（**同地址只发最早那份绑定**）；客服指派 `POST /orders/<单号>/assign/` ⇒ `member.assign`（只录单）+ **已抛过 ⇒ `member.unlist`（撤下）**；**他自己**在我方大厅接了 `POST /hall/<单号>/claim/` ⇒ `member.assign` + 已抛过 ⇒ `member.unlist`；我方**别的**打手接了（同上端点）⇒ `member.withdraw`（发给**已经推过**的绑定）；撤回 `/withdraw/` / 删除 `/delete/` / 回池 `/clear-taker/` ⇒ `member.withdraw`；我方状态变了 `POST /orders/<单号>/transition/`（唯一写入口 `apply_transition`）⇒ `member.status_out`；**绑定那一刻的补推** `PUT /api/business/member/jidanpu-binding/`（`enabled` 翻真）⇒ `to_launch` ⇒ `member.launch`、`to_record` ⇒ `member.assign`（只录单）、`to_unlist` ⇒ `member.unlist`；⭐ 上家推来的接手事件改了「接手项目」（`peer_inbound.apply_events` 的 `order.assign` / `order.grab` / `order.withdraw`，不是 HTTP 端点，是令牌口那条路的落库层）⇒ `member.project`（判据在 `PeerInboundProjectTests`）；⭐ 绑定「抛单百分比」真的变了（同一行 `PUT …/jidanpu-binding/`；旧值快照判「真的变了」、换凭据的 PUT 不触发）⇒ `member.price`（只排**这一份绑定**在厅 + 本地待抢单的链路卡；判据在 `PercentRepriceTests`，消费端联动在 `test_jidanpu_member_price_sync.py::PercentRepriceConsumerLinkTests`）。
⚠️ 「上家接手事件」那一支**直接**改 `Order.project_text`（绕过 `order_writes.update_order_fields` 的 `changed` 清单）⇒ 不接这一句时，上家推来的项目名会**停在我在下家那一格**；判据口径与本地编辑那条**同一个**：真的变了才排。⚠️ 「绑定百分比变更」（D-0326 / E-0301 生产实锤）：绑定**创建那一刻**的启用补齐把存量单按**当时的**百分比抛出、之后才改百分比 ⇒ 首批已抛出的卡停在旧价、永不同步（现场读数见 E-0301）；「值变了没有」的判据住在 PUT 的**旧值快照**里（⭐ 同一条家族纪律：触发点函数自己不比对）。
判据形状：全部走**真 HTTP**（真端点、真登录），断言一律打**数据库行**（`JidanpuSyncEvent` 的 kind / `target_ref` / `payload["member_binding_id"]`），⛔ 不断言 logger（`AGENTS.md` §5-G）；`payload["member_binding_id"]` **是 int**（换绑收敛按它命中，裁定 2）；⭐ **「撤下」这个动作回来了**（业务方明确要求；`member.unlist`）：我方接手（指派 / 在我方大厅自接）之后，对方那张单要**停在「未分配」**——不在他们的抢单大厅、也不指派给他们任何人 ⇒ 给「已经抛过（链路 `in_hall=True`）」的绑定排一条撤下；⛔ 与 `member.withdraw`（退单）分开：撤下只把人家的单从大厅拿回来，不作废它；⚠️ **只有本地已经有链路**才排撤下（`throw-to-hall` + `assign` 连着点、而 `member.launch` 还没被消费时**没有链路**，那一档由 **180 秒的对账**（`to_unlist`）补上）；**零绑定即零成本**：一条查询计数用例钉住全仓最热的写入口 `apply_transition` 在没有本线链路时**最多只多一次查询**（去掉短路就会红）；夹具一律**合成值**（E-0077）：`djsync-*` / `https://jidanpu-a.test.invalid` / `pw-canary`；⛔ 零真实网络：`detect_binding` 用桩（**打在 `apps.business.jidanpu_member` 上**，E-0029）。"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db import transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu, jidanpu_member, jidanpu_member_openapi_outbox, jidanpu_openapi
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.order_status import apply_transition
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import make_logged_in_client

ORDERS = "/api/business/orders"
HALL = "/api/business/hall"
BINDING_URL = "/api/business/member/jidanpu-binding/"

BASE_URL = "https://jidanpu-a.test.invalid"
BASE_URL_B = "https://jidanpu-b.test.invalid"

#: 四个 kind 的**字面值**就是写台账的契约（⛔ 改名会静默换掉语义；E-0024 钉值不钉常量）。
KIND_LAUNCH = "member.launch"
KIND_ASSIGN = "member.assign"
KIND_WITHDRAW = "member.withdraw"
KIND_STATUS_OUT = "member.status_out"

#: **「撤下」的 kind**（`member.unlist`）：从对方**内部接单大厅**撤下（⛔ 不退单）。
#: ⭐ 业务方明确要求这个行为 ⇒ 用的是 SDK 登记里的 `RelesOrderToHomeByOrderID`（依据见 `test_jidanpu_member_consume` 的 `ReleaseOrderToHomePrimitiveTests`）；
#: ⚠️ 与 `member.withdraw`（退单）**不是一回事**：只断后者会**静默放行**一个把两者混为一谈的实现。
KIND_UNLIST = "member.unlist"

#: **「改价」的 kind**（`member.price`，触发点⑨用它）：绑定「抛单百分比」变了 ⇒
#: 已抛出去的卡按新百分比重抛钉值。字面值就是写台账的契约（E-0024 同一族）。
KIND_PRICE = "member.price"

#: 本文件走到的**端点门**（`OrderViewSet.ORDER_PERMISSIONS` + 大厅抢单门）。
#: `groups` 只建 Group、**不放行**，所以每个真登录的夹具都要显式声明它要过的门。
NODES = (
    "order.list",
    # ⭐ 计划 ④：抛大厅改判**独占** 36（原夹具的 20 不再开这道门）。
    "order.send_to_grab_hall",          # /throw-to-hall/
    "order.assign_taker",               # /assign/
    "grab_hall.take_order",             # /hall/<单号>/claim/
    "order.withdraw_from_grab_hall",    # /withdraw/
    "order.clear_taker",                # /clear-taker/
    "order.delete",                     # /delete/
    "order.change_status",              # /transition/（非 COMPLETED 目标）
)

#: 本线的三张表（查询计数用例用它们判「多没多查」）。
MEMBER_TABLES = (
    JidanpuMemberOrderLink._meta.db_table,
    JidanpuMemberBinding._meta.db_table,
    JidanpuSyncEvent._meta.db_table,
)

#: 检测桩的人话（**字面量**，⛔ 不从被测模块派生）。
DETECT_OK = jidanpu_member.DetectionReport(ok=True)


def _no(n: int) -> str:
    """20 位订单号（`OrderViewSet.lookup_value_regex = r"\\d{20}"`）。"""
    return f"2026092600000000{n:04d}"


def _detect_ok():
    """把 `jidanpu_member.detect_binding_with_login_identity` 换成固定「检测通过」的桩
    （PUT 启用那一跳要它；第二个返回值 `None` = 登录显示名那一跳没解析出来）。"""
    return mock.patch.object(
        jidanpu_member,
        "detect_binding_with_login_identity",
        return_value=(DETECT_OK, None),
    )


class _TriggerFixture(TenantTestCase):
    """共用夹具：一款游戏、若干账号与绑定、一张带 MAIN 绩效行的订单。
    ⚠️ 必须 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里，普通 `TestCase` 的 search_path 里没有 `business_*` 表。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="下游触发点夹具游戏")

    # ── 账号 / 绑定 ─────────────────────────────────────────────────
    def _user(self, username, *, with_game=True, profile=True):
        """建一个账号；默认给它「本文件那款游戏」的权限（大厅那一档要它）。"""
        user = get_user_model().objects.create_user(
            username=username, password="pw-canary"
        )
        if profile:
            row = StaffProfile.objects.create(user=user)
            if with_game:
                row.games.add(self.game)
        return user

    def _binding(self, user, *, base_url=BASE_URL, enabled=True):
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=base_url,
            account=f"acc-{user.username}",
            password="pw-canary",
            source_name="团团通系统",
            enabled=enabled,
        )

    def _link(self, binding, order, *, remote_id="JDP-00000000000000000001",
              in_hall=True):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding,
            order=order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    # ── 订单 ───────────────────────────────────────────────────────
    def _order(self, no, *, status=OrderStatus.UNASSIGNED, taker=None,
               hall_price="12.34"):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal(hall_price),
            total_price=Decimal("50.00"),
            project_text="合成项目甲",
        )
        Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("20.00"),
        )
        return order

    # ── 请求 ───────────────────────────────────────────────────────
    def _client(self, username="djsync-actor"):
        """真登录（fail-closed）的客服客户端；默认给当前租户全部游戏。"""
        return make_logged_in_client(username=username, groups=("客服",), nodes=NODES)

    def _bound_client(self, username, *, enabled=False, base_url=BASE_URL):
        """真登录**同时**是这条绑定的主人（PUT 启用补推那几条要用）。"""
        client = self._client(username)
        user = get_user_model().objects.get(username=username)
        binding = self._binding(user, enabled=enabled, base_url=base_url)
        return client, user, binding

    def _post(self, client, url, payload=None):
        return client.post(url, payload or {}, content_type="application/json").json()

    # ── 断言工具 ───────────────────────────────────────────────────
    def _events(self, order, kind):
        return JidanpuSyncEvent.objects.filter(kind=kind, target_ref=order.order_no)

    def _binding_id_of(self, order, kind):
        rows = self._events(order, kind)
        self.assertEqual(rows.count(), 1, f"{kind} 台账行数不对：{rows.count()}")
        row = rows.get()
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.target_type, "order")
        self.assertEqual(row.status, "pending")
        value = row.payload["member_binding_id"]
        self.assertIs(type(value), int)
        return value

    def _payload_of(self, order, kind):
        """该单该 kind **恰好一行**台账的 payload（`_binding_id_of` 的 payload 版）。"""
        rows = self._events(order, kind)
        self.assertEqual(rows.count(), 1, f"{kind} 台账行数不对：{rows.count()}")
        return dict(rows.get().payload or {})


# ══ ① 抛大厅 ⇒ member.launch ═════════════════════════════════════════════════


class ThrowToHallTests(_TriggerFixture):
    def test_throwing_to_the_hall_queues_member_launch(self):
        """抛单进大厅 ⇒ 给**归属该单**的绑定排一条 `member.launch`。
        判别力：把 `throw_to_hall` 里那次调用删掉 ⇒ 本用例的行数断言红。"""
        owner = self._user("djsync-launch-owner")
        binding = self._binding(owner)
        order = self._order(_no(1))

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._binding_id_of(order, KIND_LAUNCH), binding.pk)

    def test_a_binding_outside_the_game_scope_gets_nothing(self):
        """游戏范围外的绑定**一行都不写**（范围判据真的接上了，不是摆设）。"""
        other_game = Game.objects.create(name="下游触发点范围外游戏")
        outsider = self._user("djsync-launch-outsider")
        StaffProfile.objects.get(user=outsider).games.set([other_game])
        self._binding(outsider)
        order = self._order(_no(2))

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)

    def test_a_disabled_binding_gets_nothing(self):
        """**停用**的绑定不推（与既有那条线 `queue_launch` 的闸门口径一致）。"""
        owner = self._user("djsync-launch-disabled")
        self._binding(owner, enabled=False)
        order = self._order(_no(3))

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)

    def test_two_bindings_at_the_same_address_only_the_earlier_one_pushes(self):
        """同地址重叠 ⇒ **只推最早那份**（spec §5.4 第 2 行；`id` 小的是最早建的）。
        判别力：`owners_of` 不按地址分组 / 去掉 `dedupe_owner` ⇒ 本用例看到 2 行。"""
        earlier = self._user("djsync-launch-earlier")
        first = self._binding(earlier)
        later = self._user("djsync-launch-later")
        second = self._binding(later, base_url=BASE_URL)
        order = self._order(_no(4))

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_LAUNCH), first.pk)
        self.assertNotEqual(first.pk, second.pk)

    def test_two_addresses_each_get_their_own_launch(self):
        """不同地址是**两套下家系统**，各推一份（去重只管同地址）。"""
        first = self._binding(self._user("djsync-launch-addr-a"))
        second = self._binding(self._user("djsync-launch-addr-b"), base_url=BASE_URL_B)
        order = self._order(_no(5))

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        rows = self._events(order, KIND_LAUNCH)
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {row.payload["member_binding_id"] for row in rows},
            {first.pk, second.pk},
        )


# ══ ①-乙 ⭐「同步大厅订单」开关（D-0299）══════════════════════════════
# 业务方原话「有些工作室可能不想同步大厅的订单」。口径（D-0299 第 ② 条）：开关**只闸「抛大厅」那两条路**（触发点① + 对账 to_launch）；
# 指派录单 / 抢单录单 / 状态回传 / 退单 / 改价 / 截图等**其他一切功能照旧**（下面各一条对照）；⚠️ 夹具值**显式写** `hall_sync_enabled=False`（合成值纪律；对照支用模型默认 True）。


class HallSyncToggleTriggerTests(_TriggerFixture):
    def _hall_off(self, binding):
        """把这条绑定掰到「关」并落库（⛔ 不靠 ORM 默认：判据要的是**库里那一格**）。"""
        binding.hall_sync_enabled = False
        binding.save(update_fields=["hall_sync_enabled"])
        return binding

    def test_a_hall_off_binding_gets_no_launch_while_a_true_one_still_does(self):
        """关 ⇒ 不排 launch；**同一时刻**开着的那条照排（对照在同一张单上）。
        判别力：`queue_hall_launch` 里那行过滤删掉 ⇒ 两条都排（行数 2）红；过滤写成「一律不排」⇒ 对照支 0 行红；
        写成 `enabled` 之类别的列 ⇒ 两条都排也红。"""
        off_binding = self._hall_off(self._binding(self._user("djsync-halloff-a")))
        on_binding = self._binding(self._user("djsync-halloff-b"), base_url=BASE_URL_B)
        order = self._order(_no(60))

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/")

        self.assertEqual(body["code"], 0, body)
        rows = self._events(order, KIND_LAUNCH)
        # ⚠️ **恰好一行**、且归属**开着**的那条：1 行 = 关的那条没排、开的照排，两半一起钉。
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().payload["member_binding_id"], on_binding.pk)
        self.assertNotEqual(on_binding.pk, off_binding.pk)

    def test_hall_off_does_not_stop_member_assign_on_assignment(self):
        """**业务方点名的承诺**：关掉开关的绑定，指派录单照旧排 `member.assign`。
        判别力：把闸门误写进 `_enabled_binding_of`（或 assign 那一路也读开关）⇒ 本用例红。"""
        taker = self._user("djsync-halloff-taker")
        binding = self._hall_off(self._binding(taker))
        order = self._order(_no(61))

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        # 指派那一档**依旧**不产生 launch（开关闸的不是这一档）。
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)

    def test_hall_off_owner_claiming_in_our_hall_still_queues_member_assign(self):
        """关掉开关的主人**自己**在我方大厅接单 ⇒ `member.assign` 照排（queue_claimed 那一支）。
        判别力：queue_claimed 里的 assign 腿也被闸 ⇒ 本用例红。"""
        client = self._client("djsync-halloff-claimer")
        claimer = get_user_model().objects.get(username="djsync-halloff-claimer")
        binding = self._hall_off(self._binding(claimer))
        order = self._order(_no(62), status=OrderStatus.WAITING_GRAB)

        body = self._post(client, f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        self.assertEqual(self._events(order, KIND_WITHDRAW).count(), 0)


# ══ ② 客服指派 ⇒ member.assign（只录单）═════════════════════════════════════


class AssignTests(_TriggerFixture):
    def test_assigning_to_a_bound_taker_queues_member_assign(self):
        """指派给某个代练员 ⇒ 给**他那一份**绑定排 `member.assign`。
        判别力：删掉 `assign` 里那次调用 ⇒ 行数断言红。"""
        taker = self._user("djsync-taker")
        binding = self._binding(taker)
        order = self._order(_no(6))

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        # 指派**不是**抛单：本线不该产生 launch 行（这一档只录单）。
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)

    def test_assigning_a_hall_order_to_a_peer_withdraws_the_jidanpu_listing(self):
        taker = self._user("djsync-peer-taker")
        peer = PeerDownstreamBinding.objects.create(
            account=taker, peer_base_url="https://peer.test.invalid",
            token_in_hash=hash_token(new_token()), token_out=new_token(),
        )
        binding = self._binding(self._user("djsync-other-shop"))
        order = self._order(_no(95), status=OrderStatus.WAITING_GRAB)
        link = self._link(binding, order)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertTrue(PeerSyncEvent.objects.filter(
            kind="order.assign", target_ref=order.order_no, binding_id=str(peer.pk),
        ).exists())
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)
        self.assertFalse(self._events(order, KIND_ASSIGN).exists())

        event = self._events(order, KIND_WITHDRAW).get()
        with (
            mock.patch.object(jidanpu, "set_order_status") as refund,
            mock.patch.object(
                jidanpu, "fetch_order_by_key1",
                side_effect=[
                    {"orderId": link.jidanpu_order_id, "orderSta": "未分配", "isThrowOrder": "Y"},
                    {"orderId": link.jidanpu_order_id, "orderSta": "已退单", "isThrowOrder": "N"},
                ],
            ),
        ):
            self.assertEqual(jidanpu_member.consume_event(event, client=object()), "done")
        self.assertEqual(refund.call_args.kwargs["remote_order_id"], link.jidanpu_order_id)
        self.assertEqual(refund.call_args.kwargs["status_label"], "已退单")
        link.refresh_from_db()
        self.assertFalse(link.in_hall)
        self.assertIsNotNone(link.withdrawn_at)

    def test_assigning_a_hall_order_preserves_the_takers_store(self):
        taker = self._user("djsync-same-store-taker")
        self._binding(taker, enabled=False)
        same_store = self._binding(self._user("djsync-same-store-publisher"))
        other_store = self._binding(self._user("djsync-other-store-publisher"), base_url=BASE_URL_B)
        order = self._order(_no(96), status=OrderStatus.WAITING_GRAB)
        self._link(same_store, order)
        self._link(other_store, order, remote_id="JDP-00000000000000000002")

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), other_store.pk)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), same_store.pk)

    def test_assigning_a_hall_order_queues_a_withdraw_for_an_inflight_launch(self):
        taker = self._user("djsync-taker-before-remote-launch")
        binding = self._binding(self._user("djsync-pending-launch-publisher"))
        order = self._order(_no(97), status=OrderStatus.WAITING_GRAB)
        jidanpu_member.queue_member_event(order, binding, kind=KIND_LAUNCH)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)

    def test_assigning_a_hall_order_ignores_unlisted_and_withdrawn_copies(self):
        taker = self._user("djsync-taker-with-old-copies")
        unlisted = self._binding(self._user("djsync-unlisted-copy-publisher"))
        withdrawn = self._binding(self._user("djsync-withdrawn-copy-publisher"), base_url=BASE_URL_B)
        order = self._order(_no(98), status=OrderStatus.WAITING_GRAB)
        self._link(unlisted, order, in_hall=False)
        jidanpu_member.queue_member_event(order, unlisted, kind=KIND_LAUNCH, status="done")
        link = self._link(withdrawn, order, remote_id="JDP-00000000000000000002")
        link.withdrawn_at = timezone.now()
        link.save(update_fields=["withdrawn_at"])

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertFalse(self._events(order, KIND_WITHDRAW).exists())

    def test_assigning_withdraws_a_launch_whose_readback_ran_out_of_retries(self):
        taker = self._user("djsync-taker-after-launch-failure")
        binding = self._binding(self._user("djsync-failed-launch-owner"))
        order = self._order(_no(99), status=OrderStatus.WAITING_GRAB)
        link = self._link(binding, order, in_hall=False)
        launch = jidanpu_member.queue_member_event(
            order, binding, kind=KIND_LAUNCH,
            payload={"jidanpu_order_id": link.jidanpu_order_id},
        )
        launch.status = "failed"
        launch.attempts = jidanpu_member.OUTBOX_MAX_ATTEMPTS
        launch.save(update_fields=["status", "attempts"])

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)

    def test_assignment_withdraw_leaves_a_remote_copy_already_being_worked_on(self):
        taker = self._user("djsync-taker-after-remote-claim")
        binding = self._binding(self._user("djsync-remote-claim-owner"))
        order = self._order(_no(100), status=OrderStatus.WAITING_GRAB)
        link = self._link(binding, order)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        event = self._events(order, KIND_WITHDRAW).get()
        with (
            mock.patch.object(jidanpu, "set_order_status") as refund,
            mock.patch.object(
                jidanpu, "fetch_order_by_key1",
                return_value={
                    "orderId": link.jidanpu_order_id,
                    "orderSta": "代练中",
                    "isThrowOrder": "N",
                },
            ),
        ):
            outcome = jidanpu_member.consume_event(event, client=object())
        self.assertEqual(outcome, "skipped")
        refund.assert_not_called()
        link.refresh_from_db()
        self.assertIsNone(link.withdrawn_at)

    def test_reconcile_cannot_queue_a_stale_hall_snapshot_after_assignment(self):
        taker = self._user("djsync-taker-before-reconcile-queues")
        binding = self._binding(self._user("djsync-reconcile-owner"))
        order = self._order(_no(101), status=OrderStatus.WAITING_GRAB)
        snapshot = jidanpu_member.reconcile(binding, now=timezone.now())
        self.assertEqual([x.order.pk for x in snapshot.to_launch], [order.pk])

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )
        self.assertEqual(body["code"], 0, body)
        with mock.patch.object(jidanpu_member, "reconcile", return_value=snapshot):
            jidanpu_member.reconcile_and_queue(binding, now=timezone.now())

        self.assertFalse(self._events(order, KIND_LAUNCH).exists())

    def test_consumer_skips_an_automatic_launch_queued_after_the_order_left_the_hall(self):
        taker = self._user("djsync-taker-before-old-queue")
        binding = self._binding(self._user("djsync-old-queue-owner"))
        order = self._order(_no(105), status=OrderStatus.WAITING_GRAB)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )
        self.assertEqual(body["code"], 0, body)
        event = jidanpu_member.queue_member_event(order, binding, kind=KIND_LAUNCH)
        with mock.patch.object(jidanpu, "fetch_sources") as fetch_sources:
            self.assertEqual(jidanpu_member.consume_event(event, client=object()), "skipped")
        fetch_sources.assert_not_called()

    def test_explicit_withdraw_reuses_an_existing_unrestricted_task(self):
        taker = self._user("djsync-taker-explicit-reuse")
        binding = self._binding(self._user("djsync-explicit-reuse-owner"))
        order = self._order(_no(102), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order)
        jidanpu_member.queue_withdraw(order)
        original = self._events(order, KIND_WITHDRAW).get()
        client = self._client()

        body = self._post(client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id})
        self.assertEqual(body["code"], 0, body)
        body = self._post(client, f"{ORDERS}/{order.order_no}/clear-taker/")
        self.assertEqual(body["code"], 0, body)

        self.assertEqual(self._events(order, KIND_WITHDRAW).get().pk, original.pk)

    def test_clearing_taker_after_assignment_can_withdraw_a_non_hall_copy(self):
        taker = self._user("djsync-taker-explicit-scope")
        binding = self._binding(self._user("djsync-explicit-scope-owner"))
        order = self._order(_no(103), status=OrderStatus.WAITING_GRAB)
        link = self._link(binding, order)
        client = self._client()

        body = self._post(client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id})
        self.assertEqual(body["code"], 0, body)
        original = self._events(order, KIND_WITHDRAW).get()
        body = self._post(client, f"{ORDERS}/{order.order_no}/clear-taker/")
        self.assertEqual(body["code"], 0, body)
        latest = self._events(order, KIND_WITHDRAW).latest("id")
        self.assertNotEqual(latest.pk, original.pk)

        with (
            mock.patch.object(jidanpu, "set_order_status") as refund,
            mock.patch.object(
                jidanpu, "fetch_order_by_key1",
                return_value={"orderId": link.jidanpu_order_id, "orderSta": "已退单"},
            ),
        ):
            self.assertEqual(jidanpu_member.consume_event(original, client=object()), "skipped")
            self.assertEqual(jidanpu_member.consume_event(latest, client=object()), "done")
        self.assertEqual(refund.call_count, 1)
        self.assertEqual(refund.call_args.kwargs["remote_order_id"], link.jidanpu_order_id)

    def test_historical_openapi_listing_can_be_withdrawn_after_password_binding_exists(self):
        taker = self._user("djsync-taker-historical-openapi")
        old_binding = self._binding(self._user("djsync-historical-api-owner"))
        old_binding.mode = "openapi"
        old_binding.save(update_fields=["mode"])
        self._binding(self._user("djsync-current-password-owner"))
        order = self._order(_no(104), status=OrderStatus.WAITING_GRAB)
        link = self._link(old_binding, order)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), old_binding.pk)

        upstream = mock.create_autospec(jidanpu_openapi.OpenApiClient, instance=True)
        upstream.find_order_status_with_absence.side_effect = [
            (jidanpu_openapi.OrderStatus(status="未分配"), False),
            (jidanpu_openapi.OrderStatus(status="已退单"), False),
        ]
        with mock.patch.dict(os.environ, {
            jidanpu_member_openapi_outbox.ENV_MEMBER_OPENAPI_OUTBOX: "1",
        }):
            jidanpu_member_openapi_outbox.run_member_status_outbox(
                old_binding, now=timezone.now(), client=upstream,
            )
        self.assertEqual(self._events(order, KIND_WITHDRAW).get().status, "done")
        self.assertEqual(upstream.find_order_status_with_absence.call_args_list, [
            mock.call(order_id=link.jidanpu_order_id, require_confirmation=True),
            mock.call(order_id=link.jidanpu_order_id, require_confirmation=True),
        ])
        upstream.change_order_status.assert_called_once_with(
            order_id=link.jidanpu_order_id, status="已退单", remark=None,
        )

    def test_inflight_openapi_launch_is_cancelled_after_password_binding_exists(self):
        taker = self._user("djsync-taker-stale-openapi-launch")
        old_binding = self._binding(self._user("djsync-stale-openapi-owner"))
        old_binding.mode = "openapi"
        old_binding.save(update_fields=["mode"])
        self._binding(self._user("djsync-new-password-owner"))
        order = self._order(_no(106), status=OrderStatus.WAITING_GRAB)
        jidanpu_member.queue_member_event(order, old_binding, kind=KIND_LAUNCH)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), old_binding.pk)

    def test_assigning_to_an_unbound_taker_queues_nothing(self):
        """接手人**没有**（或停用）本线绑定时一行都不写——那类单由启用补推兜（`to_record`）。"""
        taker = self._user("djsync-taker-unbound")
        order = self._order(_no(7))

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=order.order_no).count(), 0
        )

    def test_the_disabled_binding_of_the_taker_gets_nothing(self):
        taker = self._user("djsync-taker-disabled")
        self._binding(taker, enabled=False)
        order = self._order(_no(8))

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": taker.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_ASSIGN).count(), 0)


# ══ ③④ 抢单：他自己接 ⇒ assign；别的打手接 ⇒ withdraw ══════════════════════


class ClaimTests(_TriggerFixture):
    def test_the_binding_owner_claiming_his_own_hall_order_queues_member_assign(self):
        """代练员**自己**在我方大厅接了 ⇒ `member.assign`（只录单），⛔ 不是退单。
        判别力：`claim_order` 尾上那次分流删掉 ⇒ 行数断言红；
        把两档判反（自己接也发 withdraw）⇒ `member.withdraw` 那半条断言红。"""
        client = self._client("djsync-claimer-owner")
        claimer = get_user_model().objects.get(username="djsync-claimer-owner")
        binding = self._binding(claimer)
        order = self._order(_no(9), status=OrderStatus.WAITING_GRAB)

        body = self._post(client, f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        self.assertEqual(self._events(order, KIND_WITHDRAW).count(), 0)

    def test_a_different_taker_claiming_queues_member_withdraw_for_the_pushed_binding(self):
        """我方**别的**打手接了 ⇒ 给已经推到对方的绑定退单。⚠️ **两个绑定各归不同的人**：接单人自己那条绑定收 `member.assign`，
        原推单那条收 `member.withdraw`——取值必须不同，否则「发给了谁」不可辨。
        判别力：删掉 `claim_order` 尾上的分流 ⇒ 两条断言都红；把「别的打手」那半删掉 ⇒ withdraw 那条红。"""
        owner = self._user("djsync-hall-owner")
        owner_binding = self._binding(owner)
        order = self._order(_no(10), status=OrderStatus.WAITING_GRAB)
        # 前置哨兵：这张单**真的**推到过 owner 那边（链路行 = 已录到对方系统）。
        self._link(owner_binding, order)

        client = self._client("djsync-claimer-other")
        claimer = get_user_model().objects.get(username="djsync-claimer-other")
        claimer_binding = self._binding(claimer, base_url=BASE_URL_B)

        body = self._post(client, f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), owner_binding.pk)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), claimer_binding.pk)
        self.assertNotEqual(owner_binding.pk, claimer_binding.pk)

    def test_a_different_unbound_taker_does_not_touch_a_never_pushed_order(self):
        """反向控制：这张单**从没推出去过**（没有链路、没有在飞 launch）⇒ 退单一行都不写。
        「退单」是**已推出去的东西的清理**；从没录过就无从退起。
        判别力：`_push_evidence_ids` 改成「所有启用中的绑定」⇒ 本用例红。"""
        order = self._order(_no(11), status=OrderStatus.WAITING_GRAB)

        body = self._post(self._client("djsync-claimer-nobody"), f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=order.order_no).count(), 0
        )

    def test_a_pending_launch_also_gets_a_withdraw(self):
        """**在飞**的 launch 也算「推过」：消费者按写入顺序先录单 + 抛单、紧接着退单。
        判别力：`_push_evidence_ids` 只认链路行（不认在飞的 launch 台账）⇒ 本用例红。"""
        owner = self._user("djsync-hall-owner-inflight")
        binding = self._binding(owner)
        order = self._order(_no(12), status=OrderStatus.WAITING_GRAB)
        # 抛单那一刻写下的在飞台账（消费者还没跑到）。
        jidanpu_member.queue_member_event(order, binding, kind=KIND_LAUNCH)

        body = self._post(self._client("djsync-claimer-inflight"), f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)


# ══ ⑤ 撤回 / 删除 / 回池 ⇒ member.withdraw ═══════════════════════════════════


class WithdrawFromHallTests(_TriggerFixture):
    def test_withdrawing_the_hall_order_queues_member_withdraw(self):
        """`/withdraw/`（撤回抛单）⇒ 给已推过的绑定退单。
        判别力：`withdraw` 视图里那次调用删掉 ⇒ 行数断言红。"""
        owner = self._user("djsync-withdraw-owner")
        binding = self._binding(owner)
        order = self._order(_no(13), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order)

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/withdraw/")

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)

    def test_withdrawing_a_never_pushed_order_queues_nothing(self):
        """从没推出去过的单撤回 ⇒ 无退单可排（反向控制）。"""
        self._binding(self._user("djsync-withdraw-owner-unpushed"))
        order = self._order(_no(14), status=OrderStatus.WAITING_GRAB)

        body = self._post(self._client(), f"{ORDERS}/{order.order_no}/withdraw/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_WITHDRAW).count(), 0)


class SoftDeleteTests(_TriggerFixture):
    def test_soft_deleting_a_pushed_order_queues_member_withdraw(self):
        """软删 ⇒ 给已推过的绑定退单（`soft_delete_order` 的**首次删除**分支）。
        ⚠️ 接单人**故意用外人**（不是绑定主人）：「接手人是这家下家自己的账号」那一档**整店不撤**（他在对方那边正在做这张单，退掉就是毁活单）——见 `tests/test_member_same_store_withdraw.py`；
        本用例钉的是**删除这个触发点有没有接上**，与那一档无关。判别力：`recycle.soft_delete_order` 里那次调用删掉 ⇒ 行数断言红。"""
        owner = self._user("djsync-delete-owner")
        binding = self._binding(owner)
        other_taker = self._user("djsync-delete-taker")
        order = self._order(_no(15), status=OrderStatus.IN_PROGRESS, taker=other_taker)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertIsNotNone(order.deleted_at)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)

    def test_deleting_a_never_pushed_order_queues_nothing(self):
        """反向控制：从没推出去过的单删掉 ⇒ 一行都不写。"""
        self._binding(self._user("djsync-delete-owner-unpushed"))
        order = self._order(_no(16), status=OrderStatus.IN_PROGRESS)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/delete/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=order.order_no).count(), 0
        )


class ClearTakerTests(_TriggerFixture):
    def test_clearing_the_taker_queues_member_withdraw(self):
        """回池（`/clear-taker/` ⇒ 未分配）⇒ 这张单不再属于他 ⇒ 退单。
        判别力：`clear_taker` 视图里那次调用删掉 ⇒ 行数断言红。"""
        owner = self._user("djsync-clear-owner")
        binding = self._binding(owner)
        order = self._order(_no(17), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/clear-taker/", {"isDelEmp": "N"}
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)


# ══ ⑤b 回池到「待抢单大厅」⇒ 先退掉下家那张，再重新录单 / 抛厅 ══════════════════


class PoolReturnTests(_TriggerFixture):
    """⭐ 业务方要求「回池后恢复同步」——⛔ 不能只退不建。这张单从打手手里退回「待抢单大厅」（`WAITING_GRAB`）之后，它**又成了一张大厅单**
    ⇒ 下家那张旧副本先退掉（`member.withdraw`；它是「已推出去的东西」），再按大厅状态重新同步过去（`member.launch` = 重新录单 + 抛厅）。
    判据走**唯一写入口** `apply_transition` 的漏斗（`/transition/`）——⛔ 不靠 180 秒对账兜底。"""

    def test_returning_a_taken_order_to_the_hall_withdraws_then_relaunches(self):
        """代练中 ⇒ （清人）⇒ 待抢单大厅：**先退后建**，两条台账都发给那张旧副本的绑定。
        判别力：只排 `member.withdraw` ⇒ `member.launch` 那条断言红；
        只排 `member.launch`（不先退）⇒ `member.withdraw` 那条红。"""
        owner = self._user("djsync-pool-owner")
        binding = self._binding(owner)
        order = self._order(_no(41), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, in_hall=False)

        # 「从打手手里退回大厅」是**两步**（D2 闸：池内目标态不许带接手人 ⇒ 先清人）：
        client = self._client()
        cleared = self._post(
            client, f"{ORDERS}/{order.order_no}/clear-taker/", {"isDelEmp": "N"}
        )
        self.assertEqual(cleared["code"], 0, cleared)
        body = self._post(client, f"{ORDERS}/{order.order_no}/throw-to-hall/", {})

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)
        self.assertEqual(self._binding_id_of(order, KIND_LAUNCH), binding.pk)
        # 消费按 `(created_at, id)` ⇒ 先退后建靠**主键顺序**表达（同一时刻写入）。
        withdraw = self._events(order, KIND_WITHDRAW).get()
        launch = self._events(order, KIND_LAUNCH).get()
        self.assertLess(withdraw.pk, launch.pk)
        self.assertEqual(launch.payload["after_withdraw"], {
            "id": withdraw.pk, "rearmed_at": withdraw.payload.get("rearmed_at"),
        })

    def test_entering_the_hall_withdraws_the_pushed_copy_before_relaunching(self):
        """⭐ 回池后**重新**进大厅（未分配 ⇒ 待抢单）：旧副本先退、再重新录单 / 抛厅。
        这是「不能只退不建」的另一半：单在**未分配**（回池后还没抛）但下家手里还有一张录过的副本 ⇒ 抛进大厅那一刻必须先把那张退掉，
        再按大厅状态抛一张新的。判别力：漏斗里只排 `member.launch` ⇒ `member.withdraw` 那条红。"""
        owner = self._user("djsync-pool-reenter")
        binding = self._binding(owner)
        order = self._order(_no(44), status=OrderStatus.UNASSIGNED)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/", {}
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)
        self.assertEqual(self._binding_id_of(order, KIND_LAUNCH), binding.pk)
        self.assertLess(
            self._events(order, KIND_WITHDRAW).get().pk,
            self._events(order, KIND_LAUNCH).get().pk,
        )

    def test_returning_an_unpushed_order_to_the_hall_only_relaunches(self):
        """从没推出去过的单没有可退的副本 ⇒ 只排重新录单 / 抛厅（反向控制）。"""
        self._binding(self._user("djsync-pool-unpushed"))
        order = self._order(_no(42), status=OrderStatus.UNASSIGNED)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/throw-to-hall/", {}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_WITHDRAW).count(), 0)
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 1)

    def test_leaving_the_hall_does_not_relaunch(self):
        """反向控制：**离开**大厅（撤回 / 接单 / 清人）⛔ 不排 launch（否则又把单抛回去）。"""
        self._binding(self._user("djsync-pool-leave"))
        order = self._order(_no(43), status=OrderStatus.WAITING_GRAB)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/withdraw/", {}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)


# ══ ⑦ 抛过的单被我方接手 ⇒ **撤下**（`member.unlist`；业务方）════════


class UnlistOnOurTakeTests(_TriggerFixture):
    """我方指派 / 在我方大厅自接 ⇒ 对方那张单要**停在「未分配」**（拉回他们的抢单大厅）。
    ⚠️ 与 `member.withdraw` 分得清清楚楚：撤下**不作废**对方那张单（退单才会）。"""

    def test_assigning_a_pushed_order_queues_member_unlist(self):
        """客服指派给该代练员、而这张单**已经抛过**（链路 `in_hall=True`）⇒ 排一条撤下。"""
        owner = self._user("djsync-unlist-assign")
        binding = self._binding(owner)
        order = self._order(_no(31), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order, in_hall=True)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": owner.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        self.assertEqual(self._binding_id_of(order, KIND_UNLIST), binding.pk)
        # ⛔ 撤下不是退单：一条退单都不许排
        self.assertEqual(self._events(order, KIND_WITHDRAW).count(), 0)

    def test_claiming_in_our_hall_after_a_push_queues_member_unlist(self):
        """他自己在**我方**大厅接了、而这张单已经抛过 ⇒ 也排撤下（同一条口径）。"""
        # ⚠️ 登录的**就是**绑定主人（`queue_claimed` 只给「接单人自己那一份绑定」排 assign）
        client = self._client("djsync-unlist-claimer")
        claimer = get_user_model().objects.get(username="djsync-unlist-claimer")
        binding = self._binding(claimer)
        order = self._order(_no(32), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order, in_hall=True)

        body = self._post(client, f"{HALL}/{order.order_no}/claim/")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        self.assertEqual(self._binding_id_of(order, KIND_UNLIST), binding.pk)
        self.assertEqual(self._events(order, KIND_WITHDRAW).count(), 0)

    def test_assigning_an_order_never_pushed_queues_no_unlist(self):
        """反向控制：**从没抛出去过**（没有链路）⇒ 没有可撤下的东西，⛔ 不排撤下。"""
        owner = self._user("djsync-unlist-unpushed")
        binding = self._binding(owner)
        order = self._order(_no(33), status=OrderStatus.WAITING_GRAB)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": owner.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        self.assertEqual(self._events(order, KIND_UNLIST).count(), 0)

    def test_assigning_a_link_already_out_of_the_hall_queues_no_unlist(self):
        """链路已经标着「不在对方大厅」⇒ 没有可撤下的东西（⛔ 不白排一条上游调用）。"""
        owner = self._user("djsync-unlist-not-thrown")
        binding = self._binding(owner)
        order = self._order(_no(34), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(), f"{ORDERS}/{order.order_no}/assign/", {"user_id": owner.id}
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_UNLIST).count(), 0)

# ══ ⑥ 我方状态变了 ⇒ member.status_out ══════════════════════════════════════


class StatusOutTests(_TriggerFixture):
    def test_a_status_change_on_a_linked_order_queues_member_status_out(self):
        """状态变了 ⇒ 给**已有链路**的绑定排一条 `member.status_out`。
        判别力：`apply_transition` 尾上那次调用删掉 ⇒ 行数断言红。"""
        owner = self._user("djsync-status-owner")
        binding = self._binding(owner)
        order = self._order(_no(18), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": OrderStatus.PAUSED},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(self._binding_id_of(order, KIND_STATUS_OUT), binding.pk)

    # ── 来源态快照（业务方；plan §1.2-7 / §8.2：写方在**排队那一刻**写死）────────

    def test_the_queued_payload_carries_the_source_state_snapshot(self):
        """⭐ 两条都是「→ 代练中」，但 `payload["from_status"]` 各记各的来源态：已完成 → 代练中 ⇒ `COMPLETED`；待验收 → 代练中 ⇒ `PENDING_REVIEW`。
        ⚠️ 来源态写**字面值**（⛔ 不从被测模块的常量派生：那样断言会跟着实现漂）。
        判别力：`queue_status_out` 不把 `from_status` 写进 payload（或写一个固定值）⇒ 本用例红。"""
        owner = self._user("djsync-status-payload")
        binding = self._binding(owner)
        done_order = self._order(_no(36), status=OrderStatus.IN_PROGRESS, taker=owner)
        review_order = self._order(_no(37), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, done_order, remote_id="JDP-00000000000000000036")
        self._link(binding, review_order, remote_id="JDP-00000000000000000037")

        jidanpu_member.queue_status_out(done_order, from_status=OrderStatus.COMPLETED)
        jidanpu_member.queue_status_out(
            review_order, from_status=OrderStatus.PENDING_REVIEW
        )

        self.assertEqual(
            self._payload_of(done_order, KIND_STATUS_OUT).get("from_status"),
            "COMPLETED",
        )
        self.assertEqual(
            self._payload_of(review_order, KIND_STATUS_OUT).get("from_status"),
            "PENDING_REVIEW",
        )

    def test_without_a_source_state_the_payload_has_no_from_status_key(self):
        """来源态快照的第二半：默认 `from_status=None` ⇒ payload 里**干脆不写**这个键。
        「写方没提供」与「提供了空值」在台账里分得开；消费者对两者都 fail-closed（判据见 `test_jidanpu_member_consume.py::StatusOutConsumeTests` 的池内那一档）。
        判别力：把 `queue_status_out` 里 `if from_status is not None` 那半条去掉（无条件写、缺省写空串）⇒ 本用例红。"""
        owner = self._user("djsync-status-payload-default")
        binding = self._binding(owner)
        order = self._order(_no(38), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, remote_id="JDP-00000000000000000038")

        jidanpu_member.queue_status_out(order)

        self.assertNotIn("from_status", self._payload_of(order, KIND_STATUS_OUT))

    # ── 打**真端点**：确认 `apply_transition` 真把来源态交下去了 ──────────────────

    def test_the_transition_endpoint_passes_the_completed_source_state(self):
        """⭐ 打**真端点**：`COMPLETED → IN_PROGRESS`（「回改」那两条边之一）交下去的来源态必须是「已完成」。
        判别力：`apply_transition` 尾上那句丢掉 `from_status=` 实参 ⇒ 本用例红（payload 里没有这个键）；
        「来源态**恒写 COMPLETED**」那种假话由下面那条抓。"""
        owner = self._user("djsync-status-endpoint-done")
        binding = self._binding(owner)
        order = self._order(_no(39), status=OrderStatus.COMPLETED, taker=owner)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            self._payload_of(order, KIND_STATUS_OUT).get("from_status"), "COMPLETED"
        )

    def test_the_transition_endpoint_passes_the_review_source_state(self):
        """⭐ 第二条：`PENDING_REVIEW → IN_PROGRESS` 交下去的是**另一个**值（待验收）。
        ⛔ 与上一条**必须分开**：只钉「已完成」的话，一个「来源态恒写 COMPLETED」的实现照样能全绿。"""
        owner = self._user("djsync-status-endpoint-review")
        binding = self._binding(owner)
        order = self._order(_no(40), status=OrderStatus.PENDING_REVIEW, taker=owner)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            self._payload_of(order, KIND_STATUS_OUT).get("from_status"),
            "PENDING_REVIEW",
        )

    def test_a_status_change_without_any_link_queues_nothing(self):
        """**零链路 ⇒ 零动作**：状态是发给「已经录到对方系统的那张单」的。
        判别力：短路去掉后照旧不写行（因为消费者要链路），所以真正接住这条的是
        下面 `ZeroBindingCostTests` 的查询计数——两条一起才构成「零绑定即零成本」。"""
        self._binding(self._user("djsync-status-owner-unlinked"))
        order = self._order(_no(19), status=OrderStatus.IN_PROGRESS)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": OrderStatus.PAUSED},
        )

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._events(order, KIND_STATUS_OUT).count(), 0)


    def test_a_plain_transition_still_queues_a_status_out(self):
        """反面对照：**不传**来源闸（默认值）⇒ 照排 `member.status_out`。
        防的是「把钩子整个关掉」也能让上一条用例绿：本用例走的是 `apply_transition` 的**默认签名**（既有调用方的形态），
        它必须与接本线之前一样排事件。"""
        owner = self._user("djsync-status-default-owner")
        binding = self._binding(owner)
        order = self._order(_no(23), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, in_hall=False)

        with transaction.atomic():
            apply_transition(
                order, OrderStatus.PAUSED,
                nodes=frozenset({"order.change_status"}), actor="djsync-actor",
            )

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(self._binding_id_of(order, KIND_STATUS_OUT), binding.pk)

    # ── A3（spec §4.1）：排出的 payload 带**排队那一刻的来源态快照** ──────────────

    def test_the_queued_payload_carries_the_source_state_snapshot(self):
        """⭐（D-0180）两条都是「→ 代练中」，但 `payload["from_status"]` 各记各的来源：池内（待抢单）→ 代练中 ⇒ `WAITING_GRAB`（消费者据此**跳过**，⛔ 不推）；
        已退单 → 代练中 ⇒ `REFUNDED`（消费者据此**照推**「代练中」）。⚠️ 来源态写**字面值**（⛔ 不从被测模块的常量派生：那样断言会跟着实现漂）。
        判别力：`queue_status_out` 不把 `from_status` 写进 payload（或写一个固定值）⇒ 本用例红。"""
        owner = self._user("djsync-status-payload")
        binding = self._binding(owner)
        pool_order = self._order(_no(36), status=OrderStatus.IN_PROGRESS, taker=owner)
        refunded_order = self._order(_no(37), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, pool_order, remote_id="JDP-00000000000000000036")
        self._link(binding, refunded_order, remote_id="JDP-00000000000000000037")

        jidanpu_member.queue_status_out(pool_order, from_status=OrderStatus.WAITING_GRAB)
        jidanpu_member.queue_status_out(refunded_order, from_status=OrderStatus.REFUNDED)

        self.assertEqual(
            self._payload_of(pool_order, KIND_STATUS_OUT).get("from_status"),
            "WAITING_GRAB",
        )
        self.assertEqual(
            self._payload_of(refunded_order, KIND_STATUS_OUT).get("from_status"),
            "REFUNDED",
        )

    def test_without_a_source_state_the_payload_has_no_from_status_key(self):
        """第二半：默认 `from_status=None` ⇒ payload 里**干脆不写**这个键（⛔ 不写空串）。
        「写方没提供」与「提供了空值」在台账里分得开；消费者对两者都 fail-closed（判据见 `test_our_take_is_never_pushed_as_the_downstreams_in_progress`）。
        判别力：把 `queue_status_out` 里 `if from_status is not None` 那半条去掉（无条件写、缺省写空串）⇒ 本用例红。"""
        owner = self._user("djsync-status-payload-default")
        binding = self._binding(owner)
        order = self._order(_no(38), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, remote_id="JDP-00000000000000000038")

        jidanpu_member.queue_status_out(order)

        self.assertNotIn("from_status", self._payload_of(order, KIND_STATUS_OUT))

    # ── A4（spec §4.1）：打**真端点**，确认 `apply_transition` 真把来源态交下去了 ──

    def test_the_transition_endpoint_passes_the_refunded_source_state(self):
        """⭐（D-0180）`REFUNDED → IN_PROGRESS`（业务方点名要同步的那条）交下去的来源态必须是「已退单」。
        判别力：`apply_transition` 尾上那句丢掉 `from_status=` 实参 ⇒ 本用例红（payload 里没有这个键）；「来源态**恒写 `REFUNDED`**」那种假话则由下面那条池内的抓；
        两条一起才覆盖两个方向（少了任一条都会放行一种错实现）。"""
        owner = self._user("djsync-status-endpoint-refunded")
        binding = self._binding(owner)
        order = self._order(_no(39), status=OrderStatus.REFUNDED)
        self._link(binding, order, in_hall=False)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            self._payload_of(order, KIND_STATUS_OUT).get("from_status"), "REFUNDED"
        )

    def test_the_transition_endpoint_passes_the_pool_source_state(self):
        """⭐ 第二条：`WAITING_GRAB → IN_PROGRESS` 交下去的是**池内值**（待抢单）。
        ⛔ 与上一条**必须分开**：只钉「已退单」那一条的话，一个「来源态恒写 REFUNDED」的实现
        （那是**假话**，会让池内来源也去推代练中）照样能全绿。"""
        owner = self._user("djsync-status-endpoint-pool")
        binding = self._binding(owner)
        order = self._order(_no(40), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order, in_hall=True)

        body = self._post(
            self._client(),
            f"{ORDERS}/{order.order_no}/transition/",
            {"target": OrderStatus.IN_PROGRESS},
        )

        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            self._payload_of(order, KIND_STATUS_OUT).get("from_status"), "WAITING_GRAB"
        )


class ZeroBindingCostTests(_TriggerFixture):
    """零绑定即零成本。`apply_transition` 是全仓最热的写入口（3700+ 条测试都走它）⇒ 本线的状态钩子必须**先短路**：
    一次查询取链路、空就立刻返回。⛔ 不许为每次状态迁移加一堆查询。"""

    def _member_queries(self, context, table=None):
        tables = (table,) if table is not None else MEMBER_TABLES
        return [
            q["sql"]
            for q in context.captured_queries
            if any(name in q["sql"] for name in tables)
        ]

    def test_a_transition_without_member_links_costs_exactly_one_query(self):
        """没有本线链路时，一次状态迁移**恰好**多一次查询（那一次就是短路用的取链路）。
        断言 `== 0`：说明钩子整个没了（那一半由 `StatusOutTests` 接住）；断言 `> 1`：说明短路的实现变成了「先 exists 再 filter」这类多查。
        判别力：把 `queue_status_out` 的短路拆成两次查询 ⇒ 本用例红。"""
        order = self._order(_no(20), status=OrderStatus.UNASSIGNED)

        with CaptureQueriesContext(connection) as ctx:
            with transaction.atomic():
                apply_transition(
                    order, OrderStatus.IN_PROGRESS,
                    nodes=frozenset({"order.change_status"}), actor="djsync-actor",
                )

        self.assertEqual(len(self._member_queries(ctx)), 1)
        self.assertEqual(self._events(order, KIND_STATUS_OUT).count(), 0)

    def test_a_transition_with_a_link_reads_the_links_exactly_once(self):
        """有链路时，**读链路只有一次**（`select_related` 带上绑定，⛔ 不查第二次）。
        ⚠️ 这一档的总查询数必然多于 1——写台账本身就要「查在飞去重 + 插入」两次（那是 `queue_member_event` 的既有契约）；所以这里钉的是**读取侧**只查一次。
        判别力：把 `select_related("binding")` 去掉（改成逐条 `link.binding` 懒加载）⇒ 本用例红。"""
        owner = self._user("djsync-cost-owner")
        binding = self._binding(owner)
        order = self._order(_no(21), status=OrderStatus.UNASSIGNED)
        self._link(binding, order)

        with CaptureQueriesContext(connection) as ctx:
            with transaction.atomic():
                apply_transition(
                    order, OrderStatus.IN_PROGRESS,
                    nodes=frozenset({"order.change_status"}), actor="djsync-actor",
                )

        self.assertEqual(
            len(self._member_queries(ctx, JidanpuMemberOrderLink._meta.db_table)), 1
        )
        self.assertEqual(self._binding_id_of(order, KIND_STATUS_OUT), binding.pk)


# ══ ⑦ 绑定那一刻的补推（`to_launch` ⇒ launch；`to_record` ⇒ assign）═════════


class BackfillTests(_TriggerFixture):
    def _enable(self, client, binding):
        payload = {
            "jidanpu_base_url": binding.jidanpu_base_url,
            "account": binding.account,
            "enabled": True,
        }
        with _detect_ok():
            body = client.put(
                BINDING_URL, json.dumps(payload), content_type="application/json"
            ).json()
        self.assertEqual(body["code"], 0, body)
        binding.refresh_from_db()
        self.assertIs(binding.enabled, True)

    def test_enabling_pushes_a_hall_order_as_member_launch(self):
        """① 绑定前就在他范围内大厅里的单 ⇒ 启用时落 `member.launch`（录单 + 抛单）。
        判别力：PUT 里那次 `reconcile_and_queue` 删掉 ⇒ 行数断言红。"""
        client, _, binding = self._bound_client("djsync-backfill-launch")
        order = self._order(_no(22), status=OrderStatus.WAITING_GRAB)

        self._enable(client, binding)

        self.assertEqual(self._binding_id_of(order, KIND_LAUNCH), binding.pk)

    def test_enabling_records_an_order_already_assigned_to_him(self):
        """② **已指派给他、本地却没有链路的单** ⇒ 启用时落 `member.assign`（只录单）。⭐ 只做 `to_launch` 的话这类单**静默永不进场**。
        ⛔ 必须与 ① 独立成例（两张不同的单、两个不同的 kind）。
        判别力：`reconcile_and_queue` 里**只**路由 `to_launch`（丢掉 `to_record`）⇒ 本用例红（而 ① 照样绿）。"""
        client, owner, binding = self._bound_client("djsync-backfill-record")
        order = self._order(_no(23), status=OrderStatus.IN_PROGRESS, taker=owner)

        self._enable(client, binding)

        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)
        # ⛔ 只录单：这一档**不抛单**（不许出现 launch 行）。
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)

    def test_enabling_withdraws_an_order_that_should_no_longer_exist(self):
        """③ 停用期间那张单已不该存在（这里：已软删）⇒ 启用时补一次退单。"""
        client, _, binding = self._bound_client("djsync-backfill-withdraw")
        order = self._order(_no(24), status=OrderStatus.IN_PROGRESS)
        self._link(binding, order, in_hall=False)
        # 软删（不经端点，直接改列）：`reconcile` 的期望形态变成「对方不该再有这张单」。
        Order.all_objects.filter(pk=order.pk).update(
            deleted_at=order.created_at, deleted_by="djsync"
        )

        self._enable(client, binding)

        self.assertEqual(self._binding_id_of(order, KIND_WITHDRAW), binding.pk)

    def test_enabling_unlists_a_pushed_order_that_was_assigned_while_disabled(self):
        """停用期间被指派、链路又标着「还在大厅」⇒ 启用那一刻的补推要排撤下。
        ⚠️ 与上面那条「补录单」**要分开判**：这一档已经录过（有链路），
        `to_record` 那一支排不出来，撤下只能来自 `to_unlist`（业务方）。"""
        client, owner, binding = self._bound_client("djsync-unlist-backfill")
        order = self._order(_no(35), status=OrderStatus.IN_PROGRESS, taker=owner)
        self._link(binding, order, in_hall=True)

        self._enable(client, binding)

        self.assertEqual(self._binding_id_of(order, KIND_UNLIST), binding.pk)
        # 已经录过（有链路）⇒ 不该再排录单
        self.assertEqual(self._events(order, KIND_ASSIGN).count(), 0)
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 0)

    def test_a_second_save_with_enabled_already_true_does_not_backfill_again(self):
        """已经启用中的再保存 ⇒ **没有「翻真」**，不重复补推（去重之外的第二道闸）。"""
        client, _, binding = self._bound_client("djsync-backfill-second")
        order = self._order(_no(25), status=OrderStatus.WAITING_GRAB)
        self._enable(client, binding)
        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 1)

        # 把第一条置成终态（否则 `queue_member_event` 的在飞去重会把它挡住，
        # 这里就分不清「没翻真」与「被去重挡住」了）。
        JidanpuSyncEvent.objects.filter(target_ref=order.order_no).update(status="done")
        self._enable(client, binding)

        self.assertEqual(self._events(order, KIND_LAUNCH).count(), 1)


# ══ Step 6：先抛大厅、后被指派 ⇒ 对方那边不动 ════════════════════════════════


class ThrownThenAssignedTests(_TriggerFixture):
    def test_throwing_then_assigning_to_the_same_member_writes_no_withdraw(self):
        """先抛大厅、后指派给**同一个人**，且**中间一次都没消费过台账** ⇒ ⛔ 不重复录单、⛔ 不整张退掉、⛔ 也没有撤下事件。
        ⚠️ 这里没有撤下，是因为**本地还没有链路**（`member.launch` 那一条还在飞、没人消费）——「撤下」的前提是「已经录到过对方」（链路 `in_hall=True`），这一档由 180 秒的对账（`to_unlist`）与 `_consume_assign` 的补排兜住（判据见 `UnlistOnOurTakeTests.test_assigning_a_pushed_order_queues_member_unlist`）。
        ⚠️ `member.status_out`（我方状态真的变了）不在禁止之列。"""
        owner = self._user("djsync-thrown-assigned")
        binding = self._binding(owner)
        order = self._order(_no(26))
        client = self._client()

        body = self._post(client, f"{ORDERS}/{order.order_no}/throw-to-hall/")
        self.assertEqual(body["code"], 0, body)
        body = self._post(
            client, f"{ORDERS}/{order.order_no}/assign/", {"user_id": owner.id}
        )
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

        # ① 不重复录单：抛单只有一条 launch 台账（指回同一个人那份绑定）。
        self.assertEqual(self._binding_id_of(order, KIND_LAUNCH), binding.pk)
        # ② 这一档既不退单也不撤下（没有链路 ⇒ 无从撤起）。⚠️ **两个 kind 必须显式枚举**：撤下（`member.unlist`）与退单（`member.withdraw`）
        #    是两件事，只断一个会**静默放行**把两者混为一谈的实现（本仓最忌的假判据形状）。
        #    判别力：往这条流程里塞一条 `member.unlist` / `member.withdraw` 的行 ⇒ 本行断言红。
        for kind in (KIND_WITHDRAW, KIND_UNLIST):
            self.assertEqual(
                self._events(order, kind).count(), 0, f"不该有 {kind} 事件"
            )
        # ③ 指派那一档照常落一条「只录单」。
        self.assertEqual(self._binding_id_of(order, KIND_ASSIGN), binding.pk)


# ══ Step 7：编辑保存里改了「接手项目」⇒ `member.project` ═══════════════
# ⚠️ 「内容」只在**建单那一刻**发一次 ⇒ 建单之后再改「接手项目」（`Order.project_text`）必须靠这个触发点跟上；本节的判据 = 触发点**挂对了地方**、且**不是「每次保存都推」**。
# ⚠️ 消费那一侧（真的打到对方 / 失败退避 / `gameName` 同源）的判据在 `tests/test_jidanpu_member_openapi_outbox.py::ProjectTests`。

#: 新 kind 的**字面值**（⛔ 不从被测常量派生：自指断言在被断言的常量改坏时**不会红**）。
KIND_PROJECT = "member.project"

#: 「接手项目」那一格的**合成值**（E-0077）。
PROJECT_OLD = "合成项目甲"
PROJECT_NEW = "合成项目乙（改过）"

#: 编辑保存那道门的节点（`OrderViewSet.ORDER_PERMISSIONS["partial_update"]`，**any** 语义）。
EDIT_NODES = NODES + ("order.add_edit", "order.submit_edit")


class ProjectEditTests(_TriggerFixture):
    """编辑订单（`PATCH /orders/<单号>/`）里改了「接手项目」⇒ 排一条 `member.project`。
    ⚠️ 走**真 HTTP**（真端点、真登录、真序列化器），断言打**数据库行**（`JidanpuSyncEvent` 的 kind / `target_ref` / `payload["member_binding_id"]`），
    ⛔ 不断言 logger（`AGENTS.md` §5-G）。"""

    def _client(self, username="djsync-actor"):
        """本节的客户端要多带「编辑保存」那道门的节点。
        ⚠️ `OrderViewSet.ORDER_PERMISSIONS["partial_update"]` 是 `("order.add_edit", "order.submit_edit")`（**any** 语义）⇒ 基类那份 `NODES` 里没有它，
        直接拿基类的客户端会**每一条都 403**（读数看起来像「触发点坏了」，实际是门没过）。"""
        return make_logged_in_client(username=username, groups=("客服",), nodes=EDIT_NODES)

    def _patch_order(self, order, payload):
        #: ⚠️ 客户端**每个用例只建一次**（`make_logged_in_client` 会**当场建**那个账号 ⇒
        #: 同一用例里建第二次会撞 `auth_user_username_key`，读数看起来像"接口坏了"）。
        client = getattr(self, "_editor_client", None)
        if client is None:
            client = self._editor_client = self._client(username="djsync-project-editor")
        return client.patch(
            f"{ORDERS}/{order.order_no}/", payload, content_type="application/json"
        )

    def test_editing_the_project_field_queues_member_project(self):
        """⭐ 判据：改了「接手项目」⇒ **同事务**排一条 `member.project`，指回那张单的那份绑定。
        ⚠️ 断言的是**台账行**（不是"函数被调过"）：这一档最常见的坏法是触发点挂错地方 / 根本没挂上，而那在单测里是**静默**的（内核那几套网全绿）。"""
        owner = self._user("djsync-project-owner")
        binding = self._binding(owner)
        order = self._order(_no(31))
        self._link(binding, order)

        response = self._patch_order(order, {"project_text": PROJECT_NEW})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        #: ① 库里真的改了（阳性对照：证明这次 PATCH 真的走到了落库那一步）。
        order.refresh_from_db()
        self.assertEqual(order.project_text, PROJECT_NEW)
        #: ② 台账**恰好一行**，且指回那份绑定（裁定 2：`member_binding_id` 是 int）。
        self.assertEqual(self._binding_id_of(order, KIND_PROJECT), binding.pk)

    def test_editing_the_remark_field_queues_member_project(self):
        """⭐ D-0377：改**备注**也排 `member.project`——消费口 ModifyOrder 的 `orderRemark`
        格取的就是 `Order.remark`（实测可写，出处
        `jidanpu_member_openapi_outbox.MODIFY_ORDER_PAYLOAD_CELLS`）⇒ remark 是真被消费的字段。
        判别力：把触发判据里的 `"remark"` 从 `("project_text", "remark")` 里删掉 ⇒ 本用例红（0 ≠ 1）。"""
        owner = self._user("djsync-remark-owner")
        binding = self._binding(owner)
        order = self._order(_no(36))
        self._link(binding, order)

        response = self._patch_order(order, {"remark": "编辑改过的备注"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], 0, response.content)
        #: ① 库里真的改了（阳性对照）。
        order.refresh_from_db()
        self.assertEqual(order.remark, "编辑改过的备注")
        #: ② 台账**恰好一行**，且指回那份绑定。
        self.assertEqual(self._events(order, KIND_PROJECT).count(), 1)
        self.assertEqual(self._binding_id_of(order, KIND_PROJECT), binding.pk)

    def test_saving_the_same_remark_a_second_time_queues_nothing(self):
        """⭐ D-0377 判据二（remark 档的**反向控制**）：备注**没改**（整表单带着原值再存一次）⇒ **零**新增事件——与 `project_text` 那条同一条「真的变了才排」纪律，⛔ 不是「带了 remark 键就排」。
        ⚠️ 与 `test_saving_the_same_value_a_second_time` 同一个坑：**第一次那一条必须先收口成 `done`**，否则在飞行去重会把坏实现也遮成一行（那条的 docstring 有现场）。
        判别力：把触发判据从「changed 含 project_text 或 remark **且值真变了**」退化成「请求带了 remark 键」⇒ 第二次保存会新增一行 ⇒ 本用例红。"""
        owner = self._user("djsync-remark-same")
        binding = self._binding(owner)
        order = self._order(_no(37))
        self._link(binding, order)

        first = self._patch_order(order, {"remark": "同值备注"})
        self.assertEqual(first.json()["code"], 0, first.content)
        events = self._events(order, KIND_PROJECT)
        self.assertEqual(events.count(), 1)
        #: ⭐ 把第一次那一条**收口**（＝worker 已经消费过它）⇒ 第二次保存若还排就是新增一行。
        jidanpu_member._finish(events.get(), status="done", reason="", now=timezone.now())
        self.assertEqual(self._events(order, KIND_PROJECT).count(), 1)

        # ── 第二次：**同一个值**（整表单提交的真实形态）⇒ 一条都不许新增 ──────────────
        second = self._patch_order(order, {"remark": "同值备注"})

        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["code"], 0, second.content)
        order.refresh_from_db()
        self.assertEqual(order.remark, "同值备注")
        self.assertEqual(
            self._events(order, KIND_PROJECT).count(), 1,
            "没改『备注』却又排了一条事件（＝每存一次单就打一次上游）",
        )

    def test_saving_the_same_value_a_second_time_queues_nothing(self):
        """⭐⭐ 判据二（**这条最容易写坏**）：**没改 ⇒ 一条事件都不排**。真实形态：前端编辑态是**整表单提交**，没改的那一格也会带上原值 ⇒ 若触发点按「带了这个键」判，客服**每存一次单就打一次上游**（业务方明确要求不要）。
        判别力（三条都能红）：① 把落点那句判据从 `any(field_name == "project_text" for …)` 改成 `"project_text" in data`（只判"带了键"）⇒ 第二次保存也会排一行 ⇒ 本用例红；② 把触发点从 `update_order_fields` 挪到 `partial_update` 的入口（每次请求都调）⇒ 同样红；③ 把触发点整个删掉 ⇒ 第一条用例红。
        ⚠️⚠️ **第一次那一条必须先收口成 `done`**（在飞之外），否则本用例**恒绿**：`queue_member_event` 的去重会命中「同 kind + 同单 + 同绑定」的**在飞行** ⇒ 一个"带了键就推"的坏实现照样只能看见**一行**（实测过：不先收口时把判据换成 `"project_text" in data` 本用例仍然绿）；真实世界里那一条本来就会在几秒内被 worker 消费掉（`jidanpu_sync` 每 3 秒一拍）⇒「先收口」不是造出来的形态，它就是**客服再点一次保存时**的现场。"""
        owner = self._user("djsync-project-same")
        binding = self._binding(owner)
        order = self._order(_no(32))
        self._link(binding, order)

        first = self._patch_order(order, {"project_text": PROJECT_NEW})
        self.assertEqual(first.json()["code"], 0, first.content)
        events = self._events(order, KIND_PROJECT)
        self.assertEqual(events.count(), 1)
        #: ⭐ 把第一次那一条**收口**（＝worker 已经消费过它）⇒ 下面第二次保存若还排，
        #: 就是**新增一行**（去重挡不住 `done`，见 `queue_member_event` 的注释）。
        jidanpu_member._finish(events.get(), status="done", reason="", now=timezone.now())
        self.assertEqual(self._events(order, KIND_PROJECT).count(), 1)

        # ── 第二次：**同一个值**（整表单提交的真实形态）⇒ 一条都不许新增 ──────────────
        second = self._patch_order(order, {"project_text": PROJECT_NEW})

        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.json()["code"], 0, second.content)
        order.refresh_from_db()
        self.assertEqual(order.project_text, PROJECT_NEW)
        self.assertEqual(
            self._events(order, KIND_PROJECT).count(), 1,
            "没改『接手项目』却又排了一条事件（＝每存一次单就打一次上游）",
        )

    def test_a_save_that_changes_another_field_queues_nothing_either(self):
        """⭐ 同一条判据的**另一半**：改**别的**字段（带上了 `project_text` 原值）⇒ 零事件。
        ⚠️ 与上一条**不是重复**：上一条改的是「同一个值」，这一条改的是**另一个字段**（`customer_phone`）而 `project_text` 只是被整表单**带着原值**提交
        ⇒ 判据必须是「这一列**真的变了**」，⛔ 不是「这次请求里有没有这一列」。"""
        owner = self._user("djsync-project-other")
        binding = self._binding(owner)
        order = self._order(_no(33))
        self._link(binding, order)

        response = self._patch_order(
            order, {"project_text": PROJECT_OLD, "customer_phone": "13900139001"}
        )

        self.assertEqual(response.json()["code"], 0, response.content)
        order.refresh_from_db()
        #: 阳性对照：这次 PATCH 真的生效了（改到了另一个字段）。
        self.assertEqual(order.customer_phone, "13900139001")
        self.assertEqual(order.project_text, PROJECT_OLD)
        self.assertEqual(self._events(order, KIND_PROJECT).count(), 0)

    def test_an_order_without_a_link_queues_nothing(self):
        """⭐ 零链路即零成本：没有链路 ⇒ 一条都不排（⛔ 不排空转行）。
        ⚠️ 与改价那一档同一条口径（`queue_price_update`）：还没录进去的单，之后的录单自然带**当前**项目名 ⇒ 为它排一条事件只会得到一条永远 `skipped` 的噪声行、把台账灌满。
        判别力：把 `queue_project_update` 里那道「有链路才排」的闸删掉 ⇒ 本用例红。"""
        owner = self._user("djsync-project-nolink")
        self._binding(owner)
        order = self._order(_no(34))

        response = self._patch_order(order, {"project_text": PROJECT_NEW})

        self.assertEqual(response.json()["code"], 0, response.content)
        order.refresh_from_db()
        self.assertEqual(order.project_text, PROJECT_NEW)
        self.assertEqual(self._events(order, KIND_PROJECT).count(), 0)

    def test_a_disabled_binding_queues_nothing(self):
        """⭐ 停用中的绑定 ⇒ 一条都不排（与 `queue_hall_launch` / `queue_price_update` 同口径）。
        ⚠️ 如实登记：这一档**没有「启用那一刻补齐」**（对账不比对远端项目名）⇒ 停用期间错过的这一次要靠人再保存一次（那一次走 `rearm`）——与改价那一档的已知残差同形。
        判别力：把 `if not link.binding.enabled: continue` 删掉 ⇒ 本用例红。"""
        owner = self._user("djsync-project-disabled")
        binding = self._binding(owner, enabled=False)
        order = self._order(_no(35))
        self._link(binding, order)

        response = self._patch_order(order, {"project_text": PROJECT_NEW})

        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertEqual(self._events(order, KIND_PROJECT).count(), 0)


# ══ **上家推来的事件**改了「接手项目 / 备注」⇒ 同一个触发点 ═════════
# ⚠️ `peer_inbound.apply_events` 的**已建单两支**（接手支 `order.assign` / `order.grab` / `order.withdraw`，与 D-0377 起的 `order.update` 支——它现在也落内容面）**直接**改 `Order.project_text` / `Order.remark` 并 `save(update_fields=…)`——**绕过** `order_writes.update_order_fields` 那份 `changed` 清单 ⇒ 不接的话，双挂链路的单上上家改的内容会**停在鸡蛋铺下家那一格**。
# ⚠️ 裁定：接上——同一条路上价格已经这么接了（`queue_price_update`），语义一致：内容面变了 ⇒ 下家该跟；判据必须与本地编辑那条**同一个口径**：**真的变了才排**（⛔ 不是"带了键就排"——对端载荷 `PEER_ORDER_FIELDS` **恒带**这两列，两条判据**都要能红**）。

#: 上家那条链路上的对端单号（合成值）。
PEER_NO = "PEER-B1-0001"
#: 事件时刻（`PeerOrderLink.last_applied_at` 之前的那个 T1 / 之后那个 T2）。
PEER_T1 = "2026-10-10T09:00:00+00:00"
PEER_T2 = "2026-10-10T10:00:00+00:00"


class PeerInboundProjectTests(_TriggerFixture):
    """**双挂链路**的单：上家推来的事件（接手类 / order.update）改了项目名或备注 ⇒ 排 `member.project`。
    ⚠️ 「双挂」是承重的：一张单**同时**挂 `PeerOrderLink`（上家推给我）与 `JidanpuMemberOrderLink`（我推给我下家的代练员）——
    不接那一句时，上家推来的内容会**停在我下家那一格**（与改价那一档的注释同一条形态）。"""

    def setUp(self):
        super().setUp()
        self.kefu = self._user("djsync-peer-project-kefu")
        #: 我方订单（`project_text` 先给一个**旧值** ⇒ 「真的变了」才有读数可看）。
        self.order = self._order(_no(41))
        Order.objects.filter(pk=self.order.pk).update(project_text=PROJECT_OLD)
        #: 下家那条链：启用中的绑定 + 一条**已有链路**（没有链路 ⇒ 触发点照旧零事件）。
        self.member_binding = self._binding(self.kefu)
        self._link(self.member_binding, self.order)
        #: 上家那条链：绑定 + 链路（`last_applied_at` 早于事件 ⇒ 事件会被**应用**）。
        self.peer_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室乙",
            peer_base_url="https://peer-b1.test.invalid",
            peer_account="peer-b1-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b1-local.test.invalid",
            bound_by=self.kefu,
        )
        self.peer_link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.peer_binding,
            peer_order_no=PEER_NO,
            last_applied_at=PEER_T1,
        )

    def _event(self, *, project_text, kind="order.assign"):
        """造一条上家的**接手类**事件（`occurred_at` 晚于 `last_applied_at` ⇒ 会被应用）。
        ⚠️ 载荷里**不带 `hall_price`**：这一条用例只量项目名那一档
        （带了价 ⇒ 同一支还会排一条 `member.price`，读数会多出与本题无关的行）。"""
        return {
            "kind": kind,
            "target_ref": PEER_NO,
            "occurred_at": PEER_T2,
            "payload": {
                "status": OrderStatus.IN_PROGRESS,
                "total_price": "88.00",
                "remark": "上家改过的备注",
                "project_text": project_text,
                "customer_phone": "13900139002",
                "customer_qq": "22334455",
                "content": "上家改过的需求",
                "game_name": self.game.name,
            },
        }

    def test_a_peer_project_change_queues_one_member_project(self):
        """⭐⭐ 判据（正面）：上家推来的接手事件带了**新的**项目名 ⇒ **恰好一条** `member.project`。断言全部打在**数据库行**上（台账 kind / `target_ref` / `payload["member_binding_id"]`），⛔ 不断言 logger（`AGENTS.md` §5-G）。
        判别力（三条都能红）：① 把 `peer_inbound` 接手支里那次 `queue_project_update` 删掉 ⇒ 台账行数断言红（0 ≠ 1）；② 把它排到 `save()` **之前**（拿到的是旧值）⇒ 判据恒不成立 ⇒ 同样红；
        ③ 把它挪到**建单支**（`link is None`，本用例走不到那里）⇒ 本用例红。⚠️ D-0377 起 `order.update` 支也挂着**同口径的一份**（本支与更新支各挂一次）——两份任何一份退化都会被本类其余用例抓到。"""
        result = apply_events([self._event(project_text=PROJECT_NEW)], binding=self.peer_binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        #: ① 库里真的改了（阳性对照：证明这次事件真的走到了落库那一步）。
        self.order.refresh_from_db()
        self.assertEqual(self.order.project_text, PROJECT_NEW)
        #: ② 台账**恰好一行**，且指回那份**下家**绑定（⛔ 不是上家绑定）。
        self.assertEqual(self._events(self.order, KIND_PROJECT).count(), 1)
        self.assertEqual(self._binding_id_of(self.order, KIND_PROJECT), self.member_binding.pk)

    def test_a_peer_update_event_changing_the_remark_queues_member_project(self):
        """⭐⭐ 判据（D-0377 修复轮）：上家推来的 `order.update`（已建单的普通更新支，
        该支现在也落内容面）只改了**备注** ⇒ 同样排**恰好一条** `member.project`——
        不接这一份，双挂租户上家改的内容会停在我与鸡蛋铺下家之间。
        判别力：把更新支里那次 `queue_project_update` 删掉（或判据漏掉 remark 半句）
        ⇒ 本用例红（0 ≠ 1）。"""
        changed = self._event(project_text=PROJECT_OLD, kind="order.update")
        changed["payload"]["remark"] = "更新支改过的备注"

        result = apply_events([changed], binding=self.peer_binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.order.refresh_from_db()
        #: ① 库里真的改了（阳性对照）。
        self.assertEqual(self.order.remark, "更新支改过的备注")
        self.assertEqual(self.order.project_text, PROJECT_OLD)
        #: ② 台账**恰好一行**，指回那份**下家**绑定。
        self.assertEqual(self._events(self.order, KIND_PROJECT).count(), 1)
        self.assertEqual(self._binding_id_of(self.order, KIND_PROJECT), self.member_binding.pk)

    def test_a_peer_event_carrying_the_same_project_and_remark_queues_nothing(self):
        """⭐⭐ 判据（**反向控制**）：项目名与备注都和旧值**逐字相同** ⇒ **零** `member.project`。这条比正面那条更容易被写坏：`PEER_ORDER_FIELDS`（`peer_sync.py`）**恒带** `project_text` / `remark`——上家每一条状态类 / 接手类事件都带着它们 ⇒ 拿「带没带键」当判据 ⇒ **每来一条上家事件就打一次下家上游**（正是本地编辑那条判据明写不许的形态）。
        ⚠️ 阳性对照必须有：`applied == 1` ＋ 订单那一行**真的被这次事件改过**（电话那一格）——否则「0 条」也可能只是"这条事件压根没被应用"（那种绿是假的）。
        判别力：把那一句判据从「`project_text` / `remark` 真的变了」改成无条件调用（或改成 `"project_text" in payload`）⇒ 本用例红（台账里会多出一行）。"""
        #: 本地先把两列摆成事件将要带来的**同值**（夹具单的备注本来是空串）。
        Order.objects.filter(pk=self.order.pk).update(
            remark="上家旧备注原样",
        )
        same = self._event(project_text=PROJECT_OLD)
        same["payload"]["remark"] = "上家旧备注原样"
        same["payload"]["customer_phone"] = "13911111111"

        result = apply_events([same], binding=self.peer_binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.order.refresh_from_db()
        #: 阳性对照：这一条事件**真的被应用了**（电话那一格变了），只是项目名 / 备注没变。
        self.assertEqual(self.order.customer_phone, "13911111111")
        self.assertEqual(self.order.project_text, PROJECT_OLD)
        self.assertEqual(self.order.remark, "上家旧备注原样")
        self.assertEqual(
            self._events(self.order, KIND_PROJECT).count(), 0,
            "项目名 / 备注没变却又排了一条 member.project（＝每来一条上家事件就打一次下家上游）",
        )

    def test_a_peer_event_changing_only_the_remark_queues_member_project(self):
        """⭐⭐ 判据（D-0377 的正面）：上家推来的事件只改了**备注** ⇒ 也排**恰好一条** `member.project`（消费口 ModifyOrder 的 `orderRemark` 格取的就是 `Order.remark`）。
        判别力：把接手支那句判据里的 `or order.remark != previous_remark` 删掉 ⇒ 本用例红（0 ≠ 1）。"""
        changed = self._event(project_text=PROJECT_OLD)
        changed["payload"]["remark"] = "只改备注的那条事件"

        result = apply_events([changed], binding=self.peer_binding)

        self.assertEqual(result.applied, 1, result)
        self.assertEqual(result.skipped, 0, result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.remark, "只改备注的那条事件")
        self.assertEqual(self.order.project_text, PROJECT_OLD)
        self.assertEqual(self._events(self.order, KIND_PROJECT).count(), 1)
        self.assertEqual(
            self._binding_id_of(self.order, KIND_PROJECT), self.member_binding.pk
        )


# ══ 触发点⑨（D-0326 / E-0301）：绑定「抛单百分比」变了 ⇒ 在厅卡自动补价 ═══
# ⚠️ 现场（E-0301）：绑定建立那一刻按默认 100% 启用补齐抛出 35 张、40 分钟后才改 85 ⇒ 22 张在厅单的 `recFee` 停在原价。本节钉「**真的变了**才排」与三道闸（这一份绑定 / 在厅 / 本地待抢单）；
# 消费端那一半在 `test_jidanpu_member_price_sync.py::PercentRepriceConsumerLinkTests`。


class PercentRepriceTests(_TriggerFixture):
    """`PUT /api/business/member/jidanpu-binding/` 改了「抛单百分比」⇒ 排 `member.price`。
    ⚠️ 走**真 HTTP**（真端点、真登录、真序列化器），断言打**数据库行**，⛔ 不断言 logger。
    ⚠️ 夹具的绑定一律**已启用**（生产现场：百分比是在启用之后才改的）；PUT 保持 `enabled=True` ⇒ 「启用那一刻的补推」那道闸（`was_enabled`）**不会**跑 ⇒ 台账里出现的 `member.price` 只能来自触发点⑨（归属分得清）。"""

    def _change_percent(self, client, binding, percent, **overrides):
        """对**本人那一行** PUT 一次（默认只改「抛单百分比」，其余原样带回——整表单提交形态）。"""
        payload = {
            "jidanpu_base_url": binding.jidanpu_base_url,
            "account": binding.account,
            "enabled": True,
            "throw_price_percent": percent,
        }
        payload.update(overrides)
        with _detect_ok():
            body = client.put(
                BINDING_URL, json.dumps(payload), content_type="application/json"
            ).json()
        self.assertEqual(body["code"], 0, body)
        binding.refresh_from_db()
        return body

    def test_changing_the_percent_queues_one_price_for_this_binding_only(self):
        """① 100→85 ⇒ 在厅 + 待抢单的链路卡**恰一条** `member.price`，且只指**这一份**绑定。⚠️ 「别的绑定不排」是承重断言：百分比是**每个代练员自己**那一行上的列，
        同一张单若还挂着**别的**绑定（不同下家）的链路，那张卡跟的是**别人**的百分比。
        判别力：把视图里那次 `queue_percent_reprice` 调用删掉 ⇒ 行数断言红（0 ≠ 1）；把 `queue_percent_reprice` 里的 `filter(binding=binding)` 摘掉 ⇒ 「恰好一条 / 指回这一份」两处断言红。"""
        client, _, binding = self._bound_client("djsync-pct-owner", enabled=True)
        other = self._binding(self._user("djsync-pct-other"), base_url=BASE_URL_B)
        order = self._order(_no(70), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order)
        self._link(other, order, remote_id="JDP-PCT-0000000000000000B")

        self._change_percent(client, binding, 85)

        #: 阳性对照：库里真的改了（证明这次 PUT 真的走到了落库那一步）。
        self.assertEqual(binding.throw_price_percent, 85)
        #: 台账**恰好一条**，且指回**这一份**绑定（裁定 2：`member_binding_id` 是 int）。
        self.assertEqual(self._events(order, KIND_PRICE).count(), 1)
        self.assertEqual(self._binding_id_of(order, KIND_PRICE), binding.pk)

    def test_a_put_that_keeps_the_same_percent_queues_nothing(self):
        """②⭐⭐ 百分比**没变**的 PUT ⇒ 一条事件都不排。真实形态：页面是**整表单提交**，没改的那格也会带原值 ⇒ 若触发点按「这次请求带了这一格」判，客服**每存一次设置就打一遍上游**（触发点⑧明写不许的形态）。
        ⚠️⚠️ **第一条必须先收口成 `done`**（worker 已消费过的现场）：`queue_member_event` 的在飞去重会命中「同 kind + 同单 + 同绑定」的在飞行 ⇒ 一个「带了键就排」的坏实现照样只能看见**一行**（读数看起来像「没变就不排」的假判据；实测教训见 `ProjectEditTests.test_saving_the_same_value_a_second_time_queues_nothing`）。
        判别力：把视图判据 `previous_throw_percent != binding.throw_price_percent` 摘掉（无条件调用）⇒ 第二次 PUT 会**新增**一行 ⇒ 本用例红。"""
        client, _, binding = self._bound_client("djsync-pct-same", enabled=True)
        order = self._order(_no(71), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order)

        self._change_percent(client, binding, 85)
        self.assertEqual(self._events(order, KIND_PRICE).count(), 1)
        #: ⭐ 把第一条收口（＝已被 worker 消费）⇒ 之后若还排，就是**新增**一行。
        jidanpu_member._finish(
            self._events(order, KIND_PRICE).get(), status="done", reason="", now=timezone.now()
        )

        # ── 第二次：**同一个值**（整表单提交的真实形态）⇒ 一条都不许新增 ─────────────
        self._change_percent(client, binding, 85)

        self.assertEqual(binding.throw_price_percent, 85)
        self.assertEqual(
            self._events(order, KIND_PRICE).count(), 1,
            "百分比没变却又排了一条 member.price（＝每存一次设置就打一遍上游）",
        )

    def test_not_in_hall_or_locally_refunded_queues_nothing(self):
        """③ 不在厅 / 本地已退单 ⇒ 不排（两个闸各占一半，缺一不可）。
        **不在厅**：要补的是「已抛出去、还在他家大厅」那批卡的**抛单价 `recFee`**，没抛出去的卡没有这一格可改（之后的录单/抛单自然用**当前**百分比）；**本地已退单**：那张卡归 `member.withdraw` 那条线管，⛔ 不越界替它改价。
        判别力（各自能红）：把 `queue_percent_reprice` 里的 `in_hall=True` 摘掉 ⇒ 第一半红；把 `order__status=OrderStatus.WAITING_GRAB` 摘掉 ⇒ 第二半红。"""
        client, _, binding = self._bound_client("djsync-pct-gates", enabled=True)
        not_in_hall = self._order(_no(72), status=OrderStatus.WAITING_GRAB)
        self._link(binding, not_in_hall, in_hall=False)
        refunded = self._order(_no(73), status=OrderStatus.REFUNDED)
        self._link(binding, refunded, remote_id="JDP-PCT-0000000000000000C", in_hall=True)

        self._change_percent(client, binding, 85)

        self.assertEqual(binding.throw_price_percent, 85)
        self.assertEqual(self._events(not_in_hall, KIND_PRICE).count(), 0)
        self.assertEqual(self._events(refunded, KIND_PRICE).count(), 0)

    def test_a_binding_with_no_links_queues_nothing(self):
        """④ 零链路即零成本：该绑定还没有任何链路 ⇒ 一条都不排（⛔ 不排空转行）。
        与 `queue_price_update` 的口径同一条：还没录进去的单，之后的录单自然带**当前**百分比，
        为它排一条事件只会得到一条永远 `skipped` 的噪声行、把台账灌满。"""
        client, _, binding = self._bound_client("djsync-pct-nolink", enabled=True)
        self._order(_no(74), status=OrderStatus.WAITING_GRAB)   # 有单、但与该绑定无链路

        self._change_percent(client, binding, 85)

        self.assertEqual(binding.throw_price_percent, 85)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=KIND_PRICE).count(), 0)

    def test_a_rebinding_put_queues_nothing_even_if_the_percent_changes(self):
        """⑤⭐ 换凭据的 PUT **不触发**（哪怕同一次把百分比也改了）。
        ⚠️ 换绑 = 「换了一整套凭据」：旧凭据名下的在飞事件已在同一事务里被收敛成 `skipped`、存量卡按 spec §8.4 归**人工核对**——⛔ 不许拿新凭据去改旧单，也⛔ 给它排一条新事件（那等于用新凭据重开「改旧单」的路）。
        判别力：把视图判据里的 `not rebinding` 摘掉 ⇒ 本用例红（会多出一行）。"""
        client, _, binding = self._bound_client("djsync-pct-rebind", enabled=True)
        order = self._order(_no(75), status=OrderStatus.WAITING_GRAB)
        self._link(binding, order)

        #: 换账号（凭据身份变了）与改百分比**同一次**叠加。
        self._change_percent(client, binding, 85, account="acc-pct-changed")

        #: 阳性对照：两处**都真的落了库**（证明这一支真的走到了换绑 + 改百分比）。
        self.assertEqual(binding.account, "acc-pct-changed")
        self.assertEqual(binding.throw_price_percent, 85)
        self.assertEqual(
            self._events(order, KIND_PRICE).count(), 0,
            "换凭据的 PUT 不许给旧单排改价事件（spec §8.4：不拿新凭据改旧单）",
        )
