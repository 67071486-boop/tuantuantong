"""peer 同步落库的**订单日志**（⭐ 2026-10-26，业务方要求补的缺失）。

## 补的是什么（业务现场）

补之前，团团通↔团团通（peer）同步落库**只有撤单**写了订单日志；录单 / 指派 / 接单 /
更新 / 截图落地后订单详情里什么都看不到。业务方明确要求：订单详情要能看出
「这单是什么时候从哪家同步来的、下家什么时候接的单」。

## 本文件钉的七件事（与任务书 ①–⑦ 一一对应）

① 下行 `order.assign` 建单 ⇒ 一条「从上家「X」同步：已录单」；
② 下行 `order.grab` / `order.update`（已建链）各一条；
③ 下行 `order.withdraw` **恰好一条**（沿用既有 `order.peer_withdraw`）；
④ 被跳过的事件（`unknown_order` / `stale`）**不写**；
⑤ 同一事件重投**不新增**（含截图 add 的幂等支与 delete 的 no-op 支）；
⑥ 上行（我方是上家、收下家的回传）也有日志：状态回传 + 大厅接单；
⑦ **整链**：事件从接收端点（Bearer）进来、日志从订单详情 `GET /orders/<单号>/audit/`
   的**响应体**里读出来（⛔ 不是直接查表——那样断不出「页面上真的看得到」）。

## 断言纪律

- 判据钉**字面值**（E-0024）：`action` / `summary` 整句 / `log_type` / `to_value` 全部手写，
  ⛔ 不用 `ACTION_TEMPLATES[action].format(...)` 这类自指断言（模板改坏时不会红）。
- 夹具值**全部是合成值**（`SYN-PEER-LOG-*` / `*.test.invalid` / `测试*`），⛔ 不用真实
  租户 / 客户名（E-0077）。
- 机器行：`actor_id is None` + `actor` = 对方可读名；`peer.*` 一族 `log_type=40`。
- ⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.api.peer_views import receive_events
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_inbound import apply_upstream_events
from apps.business.peer_sync import (
    HALL_THROW_KIND,
    hash_token,
    new_token,
)
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

#: 两端对齐用的游戏名（裁决 R21：按名字对齐，⛔ 不传 id）。
GAME_NAME = "peer 日志夹具游戏"
#: 已建链的那张本地单的对端单号（夹具刻意让本地单号 = 对端单号）。
LINKED_REF = "SYN-PEER-LOG-1"
#: 建单用例用的新对端单号（本地还没有 link ⇒ 走建单支）。
NEW_REF = "SYN-PEER-LOG-NEW"
#: 接收端点（Bearer）与租户域名（与 `test_peer_p3_inbound_up` 同源）。
INBOUND_URL = "/api/business/peer-sync/inbound/events/"
PEER_DOMAIN = "tenant.test.com"
#: 订单详情读日志的端点（`OrderViewSet.audit`）。
ORDERS_URL = "/api/business/orders"
#: 对方可读名（合成值）。上家绑定 = `peer_name`；下家绑定 = 绑定账号的登录账号。
UP_NAME = "测试上家甲"
DOWN_USERNAME = "syn-peer-log-down"


def _now_plus(seconds):
    return timezone.now() + timedelta(seconds=seconds)


def _order_event(kind, ref, *, occurred_at, status="IN_PROGRESS", hall_price="50.00"):
    return {
        "kind": kind,
        "target_ref": ref,
        "occurred_at": occurred_at.isoformat(),
        "payload": {
            "status": status,
            "hall_price": hall_price,
            "game_name": GAME_NAME,
            "project_text": "syn-项目",
            "remark": "syn-备注",
            "content": "syn-需求",
        },
    }


class _PeerLogFixture(TenantTestCase):
    """上下两条绑定 + 一张已建链的本地单（同时给「下行建单 / 下行更新 / 上行回传」用）。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)
        # ── 下家侧（我方是下家、收上家的单）──
        self.kefu = get_user_model().objects.create_user(
            username="syn-peer-log-kefu", password="pw-syn-peer-log"
        )
        self.raw_upstream_token = new_token()
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name=UP_NAME,
            peer_base_url="https://peer-log-up.test.invalid",
            peer_account="syn-peer-log-up-account",
            token_in_hash=hash_token(self.raw_upstream_token),
            token_out=new_token(),
            local_base_url="https://peer-log-local.test.invalid",
            bound_by=self.kefu,
        )
        # ── 上家侧（我方是上家、收下家的回传）──
        self.downstream_account = get_user_model().objects.create_user(
            username=DOWN_USERNAME, password="pw-syn-peer-log"
        )
        self.downstream_binding = PeerDownstreamBinding.objects.create(
            account=self.downstream_account,
            peer_base_url="https://peer-log-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        # ── 一张已建链的本地单（走「已建链的后续事件」那一支）──
        self.order = Order.objects.create(
            order_no=LINKED_REF,
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
            source_order_no=LINKED_REF,
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no=LINKED_REF,
            # 留空 = 比任何带时刻的事件都旧 ⇒ 事件会被应用。
        )

    # ── 断言助手 ─────────────────────────────────────────────────────
    def _peer_rows(self, order_no):
        return OperationLog.objects.filter(
            order_no=order_no, action__startswith="peer."
        )

    def _assert_machine_order_row(self, row, *, order, action, summary, to_value=""):
        """一行 peer 日志的字面值（机器来源 + 订单目标的共同形状）。"""
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, 40)                  # peer.* 一族 = 系统相关
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.order_no, order.order_no)
        self.assertEqual(row.source_order_no, order.source_order_no)
        self.assertEqual(row.game_name, GAME_NAME)
        self.assertIsNone(row.actor_id)                     # 机器来源（本地没有人在操作）
        self.assertEqual(row.summary, summary)
        self.assertEqual(row.to_value, to_value)

    def _push_inbound(self, events):
        return receive_events(self.upstream_binding, events)


class InboundOrderLogTests(_PeerLogFixture):
    """① ② ③ 下行（我方是下家）的录单 / 指派 / 接单 / 更新 / 撤单。"""

    def test_order_assign_on_a_new_order_writes_the_sync_log(self):
        """① 下家收 `order.assign`、本地建单 ⇒ 一条「从上家「X」同步：已录单」。"""
        outcome = self._push_inbound([
            _order_event("order.assign", NEW_REF, occurred_at=_now_plus(5))
        ])
        self.assertEqual(outcome["applied"], 1, outcome)
        order = Order.objects.get(source_order_no=NEW_REF)
        row = self._peer_rows(order.order_no).get()
        self._assert_machine_order_row(
            row,
            order=order,
            action="peer.inbound_order",
            summary=f"从上家「{UP_NAME}」同步：已录单（{NEW_REF}）",
            to_value="未分配",                              # P4：接手类落地即未分配
        )
        self.assertEqual(row.actor, UP_NAME)
        # 副产物别漏：这一条不是「凑一条日志」，订单真落了。
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_order_grab_on_an_existing_order_writes_one_log(self):
        """② 已建链的单收到 `order.grab` ⇒ 一条 `peer.inbound_grab`。"""
        outcome = self._push_inbound([
            _order_event("order.grab", LINKED_REF, occurred_at=_now_plus(5))
        ])
        self.assertEqual(outcome["applied"], 1, outcome)
        self.order.refresh_from_db()
        row = self._peer_rows(self.order.order_no).get()
        self._assert_machine_order_row(
            row,
            order=self.order,
            action="peer.inbound_grab",
            summary=f"上家「{UP_NAME}」同步接单：{self.order.order_no}",
            to_value="未分配",
        )
        self.assertEqual(row.actor, UP_NAME)

    def test_order_update_on_an_existing_order_writes_one_log(self):
        """② 已建链的单收到 `order.update` ⇒ 一条 `peer.inbound_update`。

        ⭐ D-0364（案 B1）：模板带状态段——`from_value` = 本地旧状态 label（夹具是
        `WAITING_GRAB` ⇒「待抢单」），`to_value` = 入站 label；ISSUE 穿透（真实业务
        事件，池内豁免只拦 `IN_PROGRESS`）⇒ 状态段照常出现。文案手写字面值（E-0024）。
        """
        outcome = self._push_inbound([
            _order_event(
                "order.update", LINKED_REF,
                occurred_at=_now_plus(5), status="ISSUE",
            )
        ])
        self.assertEqual(outcome["applied"], 1, outcome)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)
        row = self._peer_rows(self.order.order_no).get()
        self._assert_machine_order_row(
            row,
            order=self.order,
            action="peer.inbound_update",
            summary=(
                f"上家「{UP_NAME}」更新了订单：{self.order.order_no}"
                "（状态：待抢单 → 问题单）"
            ),
            to_value="问题单",
        )
        self.assertEqual(row.actor, UP_NAME)
        # 结构化列同源（⛔ 只看 summary 会漏「列没跟上」的半截）：
        self.assertEqual(row.from_value, "待抢单")

    def test_order_withdraw_writes_exactly_one_log(self):
        """③ 撤单**恰好一条**：沿用既有 `order.peer_withdraw`（⛔ 不新开一条重复动作）。"""
        outcome = self._push_inbound([{
            "kind": "order.withdraw",
            "target_ref": LINKED_REF,
            "occurred_at": _now_plus(5).isoformat(),
            "withdraw_reason": "taker_removed",
            "actor_name": "测试客服甲",
            "payload": {"status": "REFUNDED", "hall_price": "50.00",
                        "game_name": GAME_NAME},
        }])
        self.assertEqual(outcome["applied"], 1, outcome)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        rows = OperationLog.objects.filter(order_no=self.order.order_no)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.action, "order.peer_withdraw")
        self.assertEqual(row.to_value, "已退单")
        self.assertEqual(row.reason, "发单方将订单接手人移除")
        self.assertIsNone(row.actor_id)

    def test_a_screenshot_add_and_delete_each_write_one_log(self):
        """②（延伸）截图真增删各一条；`{to_value}` 区分新增 / 删除。"""
        key = "shared-orders/SYN-PEER-LOG-1/syn-shot.jpg"
        add = self._push_inbound([{
            "kind": "order.screenshot.add",
            "target_ref": LINKED_REF,
            "occurred_at": _now_plus(5).isoformat(),
            "payload": {"object_key": key, "img_type": 0, "remark": "syn-截图"},
        }])
        self.assertEqual(add["applied"], 1, add)
        rows = self._peer_rows(self.order.order_no)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().action, "peer.inbound_screenshot")
        self.assertEqual(rows.get().to_value, "新增")
        self.assertEqual(
            rows.get().summary,
            f"上家「{UP_NAME}」新增了订单截图：{self.order.order_no}",
        )

        delete = self._push_inbound([{
            "kind": "order.screenshot.delete",
            "target_ref": LINKED_REF,
            "occurred_at": _now_plus(6).isoformat(),
            "payload": {"object_key": key, "img_type": 0},
        }])
        self.assertEqual(delete["applied"], 1, delete)
        rows = self._peer_rows(self.order.order_no)
        self.assertEqual(rows.count(), 2)
        self.assertTrue(rows.filter(to_value="删除").exists())


class SkippedAndReplayTests(_PeerLogFixture):
    """④ 跳过不写；⑤ 重投不新增。"""

    def test_a_skipped_event_writes_no_log(self):
        """④ 撤单指向本地没有的单 ⇒ `unknown_order`，零日志（也不建单）。"""
        outcome = self._push_inbound([{
            "kind": "order.withdraw",
            "target_ref": "SYN-PEER-LOG-MISSING",
            "occurred_at": _now_plus(5).isoformat(),
            "withdraw_reason": "refunded",
            "payload": {"status": "REFUNDED", "game_name": GAME_NAME},
        }])
        self.assertEqual(outcome["applied"], 0, outcome)
        self.assertEqual(
            outcome["skipped_details"][0]["code"], "unknown_order", outcome
        )
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_stale_event_writes_no_log(self):
        """④ 比本地水位旧的事件 ⇒ `stale`，零日志。"""
        self._push_inbound([
            _order_event("order.update", LINKED_REF, occurred_at=_now_plus(5),
                         status="ISSUE")
        ])
        self.assertEqual(self._peer_rows(self.order.order_no).count(), 1)
        # 更旧的一条（时刻早于已应用水位）⇒ stale。
        outcome = self._push_inbound([
            _order_event("order.update", LINKED_REF, occurred_at=_now_plus(1),
                         status="PAUSED")
        ])
        self.assertEqual(outcome["applied"], 0, outcome)
        self.assertEqual(outcome["skipped_details"][0]["code"], "stale", outcome)
        self.assertEqual(self._peer_rows(self.order.order_no).count(), 1)

    def test_replaying_the_same_event_does_not_write_a_second_log(self):
        """⑤ 同一事件重投 ⇒ 本地没变化（stale）⇒ ⛔ 不写第二行。"""
        event = _order_event(
            "order.assign", NEW_REF, occurred_at=_now_plus(5)
        )
        first = self._push_inbound([event])
        self.assertEqual(first["applied"], 1, first)
        order = Order.objects.get(source_order_no=NEW_REF)
        self.assertEqual(self._peer_rows(order.order_no).count(), 1)

        second = self._push_inbound([event])
        self.assertEqual(second["applied"], 0, second)
        self.assertEqual(second["skipped_details"][0]["code"], "stale", second)
        self.assertEqual(self._peer_rows(order.order_no).count(), 1)

    def test_replaying_a_screenshot_and_a_noop_delete_do_not_add_logs(self):
        """⑤ 截图的幂等支（同 key 重投）与 no-op 支（删不存在的行）都⛔ 不写。"""
        key = "shared-orders/SYN-PEER-LOG-1/syn-shot-replay.jpg"
        add = {
            "kind": "order.screenshot.add",
            "target_ref": LINKED_REF,
            "occurred_at": _now_plus(5).isoformat(),
            "payload": {"object_key": key, "img_type": 0},
        }
        self._push_inbound([add])
        self.assertEqual(self._peer_rows(self.order.order_no).count(), 1)
        # 同 key 重投：不建第二行（幂等 applied）⇒ 也不写第二行日志。
        replay = self._push_inbound([add])
        self.assertEqual(replay["applied"], 1, replay)
        self.assertEqual(self._peer_rows(self.order.order_no).count(), 1)
        # 删一个**不存在**的 key：良性 no-op ⇒ 也不写。
        noop = self._push_inbound([{
            "kind": "order.screenshot.delete",
            "target_ref": LINKED_REF,
            "occurred_at": _now_plus(6).isoformat(),
            "payload": {"object_key": key, "img_type": 0},
        }])
        self.assertEqual(noop["applied"], 1, noop)
        # 上面这次 delete 是真的删掉了刚建的那一行 ⇒ 有 1 行日志（删除）。
        self.assertEqual(self._peer_rows(self.order.order_no).count(), 2)
        # **再一次** delete（那一行已经不在了）⇒ no-op，不再写。
        again = self._push_inbound([{
            "kind": "order.screenshot.delete",
            "target_ref": LINKED_REF,
            "occurred_at": _now_plus(7).isoformat(),
            "payload": {"object_key": key, "img_type": 0},
        }])
        self.assertEqual(again["applied"], 1, again)
        self.assertEqual(self._peer_rows(self.order.order_no).count(), 2)


class UpstreamLogTests(_PeerLogFixture):
    """⑥ 上行（我方是上家、收下家的回传）：状态回传 + 大厅接单。"""

    def _local_order_with_taker(self):
        order = Order.objects.create(
            order_no="SYN-PEER-LOG-UP-1",
            game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
            source_order_no="SYN-PEER-LOG-UP-1",
        )
        Performance.objects.create(
            order=order, user=self.downstream_account, emp_type=EmpType.MAIN
        )
        return order

    def test_a_downstream_status_echo_writes_the_log(self):
        """⑥ 下家回传状态 ⇒ 一条 `peer.upstream_status`（机器行、带下家可读名）。"""
        order = self._local_order_with_taker()
        # ⚠️ `occurred_at` 取「现在」而**不是**未来：上行的新鲜度判据是
        #    `occurred_at <= order.updated_at`（本地动作时刻作代理），未来时刻会让
        #    重投仍被判「更新」⇒ 本用例失去判别力。
        event = {
            "kind": "order.update",
            "target_ref": order.order_no,
            "occurred_at": timezone.now().isoformat(),
            "payload": {"status": "ISSUE"},
        }
        outcome = apply_upstream_events([event], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 1, outcome)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        row = OperationLog.objects.get(order_no=order.order_no)
        self._assert_machine_order_row(
            row,
            order=order,
            action="peer.upstream_status",
            summary=f"下家「{DOWN_USERNAME}」回传订单状态：{order.order_no}（问题单）",
            to_value="问题单",
        )
        self.assertEqual(row.actor, DOWN_USERNAME)

        # ⑤ 同一回传（`occurred_at` 逐字相同）重投 ⇒ 本地没变化（stale）⇒ ⛔ 不写第二行。
        replay = apply_upstream_events([event], binding=self.downstream_binding)
        self.assertEqual(replay.applied, 0, replay)
        self.assertEqual(replay.skipped_details[0]["code"], "stale", replay)
        self.assertEqual(OperationLog.objects.filter(order_no=order.order_no).count(), 1)

    def test_a_downstream_hall_claim_writes_the_take_order_log(self):
        """⑥ 下家在我方大厅接单（`hall.claim` 受理）⇒ 一条 `peer.hall_claim`。"""
        order = Order.objects.create(
            order_no="SYN-PEER-LOG-CLAIM-1",
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("10.00")
        )
        # 绑定账号要能机器接手：代练员组 + 游戏权限 + 节点。
        worker_group, _ = Group.objects.get_or_create(name="代练员")
        self.downstream_account.groups.add(worker_group)
        grant_games(self.downstream_account, self.game)
        grant_nodes(self.downstream_account, "grab_hall.take_order")
        # Z20 的共享证据：这张单**真的**通过 `hall.throw` 投给过这个下家绑定。
        from apps.business import peer_dispatch

        made = peer_dispatch.enqueue_hall_event(order, kind=HALL_THROW_KIND)
        self.assertTrue(made, "夹具自证：这条单至少要有一个下家绑定可投")
        PeerSyncEvent.objects.filter(pk__in=[event.pk for event in made]).update(
            status=PeerSyncEvent.Status.DONE,
            reason="",
            processed_at=timezone.now(),
            next_attempt_at=None,
        )

        outcome = apply_upstream_events([{
            "kind": "hall.claim",
            "target_ref": order.order_no,
            "occurred_at": _now_plus(5).isoformat(),
            "payload": {},
        }], binding=self.downstream_binding)
        self.assertEqual(outcome.applied, 1, outcome)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        row = OperationLog.objects.get(order_no=order.order_no, action="peer.hall_claim")
        self._assert_machine_order_row(
            row,
            order=order,
            action="peer.hall_claim",
            summary=f"下家「{DOWN_USERNAME}」在我方大厅接单：{order.order_no}",
            to_value="代练中",
        )
        self.assertEqual(row.actor, DOWN_USERNAME)


class OrderDetailReadsTheLogsTests(_PeerLogFixture):
    """⑦ 整链：事件从**接收端点**进来，日志从**订单详情接口的响应**里读出来。"""

    def test_the_order_detail_endpoint_returns_the_sync_logs(self):
        # ① 事件走真实接收端点（Bearer 令牌 = 这条上家绑定的 `token_in_hash`）。
        response = Client(SERVER_NAME=PEER_DOMAIN).post(
            INBOUND_URL,
            {"events": [_order_event("order.assign", NEW_REF, occurred_at=_now_plus(5))]},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1, body)
        order = Order.objects.get(source_order_no=NEW_REF)

        # ② 订单详情读日志（认证态 + 只声明读详情那颗节点）。
        reader = make_logged_in_client(
            username="syn-peer-log-reader",
            groups=("客服",),
            nodes=("order.view_detail", "order.view_content"),
        )
        detail = reader.get(f"{ORDERS_URL}/{order.order_no}/audit/")
        self.assertEqual(detail.status_code, 200, detail.content[:300])
        payload = detail.json()
        self.assertEqual(payload["code"], 0, payload)

        rows = [row for row in payload["data"] if row["action"] == "peer.inbound_order"]
        self.assertEqual(len(rows), 1, payload["data"])
        row = rows[0]
        # ⛔ 判据取**响应体**（页面真正拿到的形状），不是数据库行。
        self.assertEqual(
            row["summary"],
            f"从上家「{UP_NAME}」同步：已录单（{NEW_REF}）",
        )
        self.assertEqual(row["log_type"], 40)
        self.assertEqual(row["to"], "未分配")
        self.assertEqual(row["actor_display"], UP_NAME)
