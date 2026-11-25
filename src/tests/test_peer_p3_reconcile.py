"""上下游 P3 Task 8：丢单自愈（对账端点 + `run_reconcile` 自动补）。

业务方原话：「你要是能查到丢单 那直接自动补不就行了」⇒ 对账是**上家问下家**
「我推给你的那些单，你应用到哪了」，把对端水位**落后于本地事件时刻**的那些事件
重新排成 `pending`，让正常的投递内核把它补过去。

本文件钉四层，缺一层就漏一种错法：

① **对账端点**（`POST /api/business/peer-sync/reconcile/`，Bearer）：服务对象是**被问的一方**。
   下家被问（令牌命中 `PeerUpstreamBinding`）⇒ 按 `(upstream_binding, peer_order_no)` 回
   `PeerOrderLink` 的**最后一次本地决定时刻** `max(last_applied_at, local_action_at)`；
   没见过的单号 / 两个时刻都为空回 `null`。上家被问
   （令牌命中 `PeerDownstreamBinding`）⇒ 本期**一律回 `null`**（只有「上家问下家」一个方向）。
   请求体非对象 / `orders` 非数组 ⇒ `1000`（⚠️ 含 `{}` / `0` / `false` / `""` 这类 **falsy 非数组**
   ——⛔ 不许被 `or []` 静默吞成空请求）；`orders` 缺失 / 显式 `null` ⇒ 空映射；
   请求的单号**按上限切片**（超出的不理）。
② **出站客户端** `fetch_applied_watermarks`：信封 + Bearer + 超时，路径 `peer-sync/reconcile/`。
③ **`run_reconcile`**：只碰**出站下行**里近窗口内的 `failed` 或（`done` **且 `reason` 为空**）；
   `done` + 非空 `reason` 是**有意作废**（撤单复核）⇒ ⛔ 不重投；`pending` 归退避管。
   对端不可达 / 拒收 ⇒ 计入 `errors`、⛔ 不让整轮炸掉。
④ **重排的字段**：`status=pending`、`next_attempt_at=None`、**`attempts=0`**、人话原因。
   ⚠️ `attempts` 的清零是**承重**的：`_attempt` 的终态判据是
   `attempts > len(RETRY_BACKOFF_SECONDS)`，一条已经 `failed`（attempts=5）的事件若不清零，
   **下一投立刻又终态** —— 对账补了等于没补。
⑤ **上行超龄重排**：下家→上家的状态回传走 81 分钟梯子后终态
   `failed` 的行原本**没人再碰**（唯一的重排写点在下行对账）⇒ `run_reconcile` 现在先跑一次
   **本地**兜底：创建超 24 小时的 `failed` 上行行重排**一次**——`attempts` **不清零**（它就是
   「最多重排 1 次」的封顶计数：重排后那一投再失败落 `attempts=6`，不再进判据）；
   台账（`last_error`）留人话、健康计数⛔ 不碰；失效绑定的行不救（重排也会再被 drop）。

⚠️ 判据一律钉**字面值**（`"对账发现缺口，重投"`、`200`、`24`），⛔ 不与实现常量对拍（E-0024）。
⚠️ `mock.patch` 打在**被查找的那个模块**上（`apps.business.peer_worker.fetch_applied_watermarks`）,
⛔ 不是定义它的 `peer_client`（E-0029）。
⚠️ 夹具值**全是合成值**（`*.test.invalid` / `SYN-RECON-*`），见 E-0077。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import APIClient

from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_client import (
    PeerRejected,
    PeerUnreachable,
    fetch_applied_watermarks,
)
from apps.business.peer_sync import RECONCILE_MAX_ORDERS, hash_token, new_token
from apps.business.peer_worker import (
    RECONCILE_REQUEUE_REASON,
    RECONCILE_WINDOW_HOURS,
    run_reconcile,
)

#: `TenantTestCase` 自带租户的域名（与 `tests/test_peer_worker.py` 同源）。
PEER_DOMAIN = "tenant.test.com"
RECONCILE_URL = "/api/business/peer-sync/reconcile/"


def _now_plus(seconds: int):
    """相对「现在」往后 `seconds` 秒的 aware 时刻（对端水位夹具）。"""
    return timezone.now() + timedelta(seconds=seconds)


class TestPeerBase(TenantTestCase):
    """下家视角的共用夹具：一条**上家绑定** + 一张已被应用过的单（带 link）。

    - `self.upstream_binding`：我方作为**下家**的那条绑定，`raw_upstream_token` 是
      上家推单时带的那套（命中 `token_in_hash`）⇒ 端点据此认出「我是被问的下家」；
    - `self.link`：`(upstream_binding, peer_order_no=order.order_no)` 的对应行，
      `last_applied_at` 就是端点要回报的水位。
    """

    def setUp(self):
        self.game = Game.objects.create(name="对账夹具游戏")
        self.binder = get_user_model().objects.create_user(
            username="peer-reconcile-binder", password="pw-reconcile"
        )
        self.raw_upstream_token = new_token()
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家甲",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="peer-reconcile-account",
            token_in_hash=hash_token(self.raw_upstream_token),
            token_out=new_token(),
            local_base_url="https://peer-down.test.invalid",
            bound_by=self.binder,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no="SYN-RECON-1",
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
        )
        self.applied_at = timezone.now() - timedelta(minutes=5)
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no=self.order.order_no,
            last_applied_at=self.applied_at,
        )

    def _post_reconcile_as_downstream(self, orders, *, token=None, order_nos=None):
        """以**被问的下家**身份问对账端点（带 Bearer 令牌，⛔ 不带会话 cookie）。"""
        body = {"orders": orders}
        if order_nos is not None:
            body["order_nos"] = order_nos
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        return client.post(
            RECONCILE_URL,
            body,
            format="json",
            HTTP_AUTHORIZATION="Bearer " + (token or self.raw_upstream_token),
        )


class ReconcileEndpointTests(TestPeerBase):
    """① 端点：下家按 `(upstream_binding, peer_order_no)` 回报本地已应用水位。"""

    def test_it_reports_the_applied_watermark_per_order(self):
        resp = self._post_reconcile_as_downstream([self.order.order_no])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 0)
        self.assertEqual(
            resp.data["data"]["applied"][self.order.order_no],
            self.link.last_applied_at.isoformat(),
        )

    # ── 对账双键（计划 §4 的兼容 / §5-6）────────────────────────
    def test_the_legacy_builtin_key_still_hits_the_link(self):
        """老链路按旧内置号建 ⇒ 新对端问的是**来源号**，靠第二个键（`order_nos`）命中。

        判别力：收方只按 `orders` 里的字符串查 `peer_order_no` ⇒ 这个来源号永远回 `None`
        （发送方据此把每条事件都当缺口重投，而重投永远追不上水位）。
        """
        resp = self._post_reconcile_as_downstream(
            ["SYN-SRC-RECON-1"],
            order_nos={"SYN-SRC-RECON-1": self.order.order_no},
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 0)
        self.assertEqual(
            resp.data["data"]["applied"]["SYN-SRC-RECON-1"],
            self.link.last_applied_at.isoformat(),
        )

    def test_the_primary_key_wins_when_both_keys_would_match(self):
        """两把键都能命中时**任一对上即命中**（先按 `orders` 里的原样键，再兜第二把）。"""
        resp = self._post_reconcile_as_downstream(
            [self.order.order_no],
            order_nos={self.order.order_no: "SYN-OTHER-BUILTIN"},
        )
        self.assertEqual(
            resp.data["data"]["applied"][self.order.order_no],
            self.link.last_applied_at.isoformat(),
        )

    def test_a_non_dict_order_nos_is_a_validation_error(self):
        """`order_nos` 是可选的**第二个键**；给了但不是对象 ⇒ `1000`（⛔ 不静默忽略）。"""
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        resp = client.post(
            RECONCILE_URL,
            {"orders": [self.order.order_no], "order_nos": ["nope"]},
            format="json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 1000)

    def test_a_non_string_value_in_order_nos_is_a_validation_error(self):
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        resp = client.post(
            RECONCILE_URL,
            {"orders": [self.order.order_no], "order_nos": {self.order.order_no: 7}},
            format="json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 1000)

    def test_an_order_it_never_applied_comes_back_none(self):
        resp = self._post_reconcile_as_downstream(["NEVER-SEEN"])
        self.assertEqual(resp.data["code"], 0)
        self.assertIsNone(resp.data["data"]["applied"]["NEVER-SEEN"])

    def test_every_requested_ref_gets_an_entry(self):
        """要了三个单号（含一个从没应用过的）⇒ 回三个键，未知的为 `None`。"""
        resp = self._post_reconcile_as_downstream(
            [self.order.order_no, "NEVER-SEEN", "ALSO-NEVER-SEEN"]
        )
        applied = resp.data["data"]["applied"]
        self.assertEqual(
            set(applied),
            {self.order.order_no, "NEVER-SEEN", "ALSO-NEVER-SEEN"},
        )
        self.assertEqual(
            applied[self.order.order_no], self.link.last_applied_at.isoformat()
        )
        self.assertIsNone(applied["NEVER-SEEN"])
        self.assertIsNone(applied["ALSO-NEVER-SEEN"])

    def test_a_link_without_an_applied_time_comes_back_none(self):
        """`last_applied_at` 为空（P1 建的行 / 首单丢了时刻）⇒ 回 `null`，⛔ 不 500。"""
        self.link.last_applied_at = None
        self.link.save(update_fields=["last_applied_at"])
        resp = self._post_reconcile_as_downstream([self.order.order_no])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertIsNone(resp.data["data"]["applied"][self.order.order_no])

    def test_it_reports_local_action_at_when_it_is_newer(self):
        """水位取 `max(last_applied_at, local_action_at)`：本地有意保留了更新的改动 ⇒ 报本地时刻。

        场景（Task 7 的 `local_action_at` 判据）：下家本地改过这张单，上家推来的那条事件被
        下家**有意跳过**（不算落后），可跳过**不推进** `last_applied_at` ⇒ 只报 `last_applied_at`
        就等于永远回一个更旧的时刻，上家每轮（`--loop` 10 秒）都把它当成缺口重投。
        判别力：只报 `last_applied_at` ⇒ 本用例期望的 `local_action_at` 变成旧时刻 ⇒ 红。
        """
        newer = self.applied_at + timedelta(minutes=1)
        self.link.local_action_at = newer
        self.link.save(update_fields=["local_action_at"])
        resp = self._post_reconcile_as_downstream([self.order.order_no])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(
            resp.data["data"]["applied"][self.order.order_no], newer.isoformat()
        )

    def test_it_reports_last_applied_at_when_that_is_newer(self):
        """反向：`last_applied_at` 更晚时**仍报它**（取 max，⛔ 不是无条件改报本地时刻）。"""
        self.link.local_action_at = self.applied_at - timedelta(minutes=1)
        self.link.save(update_fields=["local_action_at"])
        resp = self._post_reconcile_as_downstream([self.order.order_no])
        self.assertEqual(
            resp.data["data"]["applied"][self.order.order_no],
            self.applied_at.isoformat(),
        )

    def test_a_link_with_only_a_local_action_time_reports_that(self):
        """只有 `local_action_at` 有值（`last_applied_at` 为空）⇒ 报本地时刻，⛔ 不回 `null`。"""
        self.link.last_applied_at = None
        self.link.local_action_at = self.applied_at
        self.link.save(update_fields=["last_applied_at", "local_action_at"])
        resp = self._post_reconcile_as_downstream([self.order.order_no])
        self.assertEqual(
            resp.data["data"]["applied"][self.order.order_no],
            self.applied_at.isoformat(),
        )

    def test_a_link_with_neither_time_comes_back_none(self):
        """两个时刻都为空（P1 建的行）⇒ 回 `null`（⛔ 不许拿"当前时间"凑一个水位）。"""
        self.link.last_applied_at = None
        self.link.local_action_at = None
        self.link.save(update_fields=["last_applied_at", "local_action_at"])
        resp = self._post_reconcile_as_downstream([self.order.order_no])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertIsNone(resp.data["data"]["applied"][self.order.order_no])

    def test_a_missing_orders_key_answers_with_an_empty_mapping(self):
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        resp = client.post(
            RECONCILE_URL,
            {},
            format="json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 0)
        self.assertEqual(resp.data["data"]["applied"], {})

    def test_a_non_object_body_is_a_validation_error(self):
        """裸 JSON 数组不是信封要求的对象 ⇒ `1000`（⛔ 不许静默当成空请求）。"""
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        resp = client.post(
            RECONCILE_URL,
            ["SYN-RECON-1"],
            format="json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 1000)

    def test_a_non_list_orders_value_is_a_validation_error(self):
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        resp = client.post(
            RECONCILE_URL,
            {"orders": "SYN-RECON-1"},
            format="json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 1000)

    def test_a_falsy_non_list_orders_value_is_a_validation_error(self):
        """**falsy 非数组**（`{}` / `0` / `false` / `""`）⇒ `1000`，⛔ 不许被静默吞成空请求。

        判别力：`request.data.get("orders") or []` 会把这些 falsy 值归一成 `[]` ⇒ 回
        `{"applied": {}}` + 业务码 0；而按对账语义「对端回 `null`/没这个号 = 我没有」，
        发起方会据此把**整个窗口**的候选单当成落后而全部重投（一个旧版对端发 `{"orders": {}}`
        就会触发）。所以这类形状必须是业务失败，⛔ 不是"空请求"。
        """
        for falsy in ({}, 0, False, ""):
            with self.subTest(orders=falsy):
                client = APIClient(SERVER_NAME=PEER_DOMAIN)
                resp = client.post(
                    RECONCILE_URL,
                    {"orders": falsy},
                    format="json",
                    HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
                )
                self.assertEqual(resp.status_code, 200, resp.content[:200])
                self.assertEqual(resp.data["code"], 1000)

    def test_an_explicit_null_orders_answers_with_an_empty_mapping(self):
        """`orders` 显式 `null` 是**钉住的**空请求（与缺键同义）⇒ `{"applied": {}}`。

        ⚠️ 与上一条是**不同**的形状：这里 `null` 是"没问任何单号"，⛔ 不是"问了一堆单号但形状错"。
        """
        client = APIClient(SERVER_NAME=PEER_DOMAIN)
        resp = client.post(
            RECONCILE_URL,
            {"orders": None},
            format="json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_upstream_token,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 0)
        self.assertEqual(resp.data["data"]["applied"], {})

    def test_a_non_string_ref_is_a_validation_error(self):
        """单号必须是字符串：列表 / 字典当 JSON 对象的键会抛 `TypeError` ⇒ 500 ⇒ 过闸门。"""
        resp = self._post_reconcile_as_downstream([1, {"nope": 1}])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["code"], 1000)

    def test_the_requested_refs_are_capped(self):
        """请求量有上限（防对端拿超长清单让我们跑无界查询）：超出的**不理**。

        判别力：把切片去掉 ⇒ `applied` 里会多出第三个键 ⇒ 红。
        """
        from apps.business.api import peer_views

        with mock.patch.object(peer_views, "RECONCILE_MAX_ORDERS", 2):
            resp = self._post_reconcile_as_downstream(["A-1", "A-2", "A-3"])
        self.assertEqual(set(resp.data["data"]["applied"]), {"A-1", "A-2"})

    def test_the_cap_and_the_window_are_the_pinned_literals(self):
        self.assertEqual(RECONCILE_MAX_ORDERS, 200)
        self.assertEqual(RECONCILE_WINDOW_HOURS, 24)

    def test_the_upstream_side_answers_null_for_every_ref(self):
        """上家被问（令牌命中 `PeerDownstreamBinding`）⇒ 本期**一律回 `null`**。

        本期对账只覆盖「上家问下家」一个方向（上家侧没有 `PeerOrderLink`）⇒ 服务端如实
        回「我没记」，让上家侧不要把 `null` 误读成缺口（登记为 Task 9 残差）。
        """
        account = get_user_model().objects.create_user(
            username="peer-reconcile-downstream", password="pw-reconcile"
        )
        raw = new_token()
        PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-down2.test.invalid",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
        )
        resp = self._post_reconcile_as_downstream(["A-1", self.order.order_no], token=raw)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.data["data"]["applied"], {"A-1": None, self.order.order_no: None})

    def test_a_session_cookie_is_not_accepted(self):
        """只认 Bearer 令牌（spec §9-S2）：无令牌 ⇒ 401。"""
        resp = APIClient(SERVER_NAME=PEER_DOMAIN).post(
            RECONCILE_URL, {"orders": []}, format="json"
        )
        self.assertEqual(resp.status_code, 401, resp.content[:200])


class _ReconcileWorkerFixture(TenantTestCase):
    """上家视角的共用夹具：一条**生效的下游绑定** + 一个固定时刻。"""

    def setUp(self):
        self.account = get_user_model().objects.create_user(
            username="peer-reconcile-sender", password="pw-reconcile"
        )
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url="https://peer-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        self.now = timezone.now()

    def _event(self, *, kind="order.assign", target_ref="SYN-A-1",
               status=PeerSyncEvent.Status.FAILED,
               attempts=5, occurred_at=None, direction="out",
               binding_role="downstream", binding_id=None, **over):
        meta = dict(over.pop("meta", {}))
        # ⚠️ `payload` 也走 `**over`（加）：对账的第二个键取自载荷里的内置号
        #     ⇒ 用例要能指定载荷；同名参数不能既在下面写死又被 `**over` 传一次。
        payload = over.pop("payload", {"status": OrderStatus.IN_PROGRESS})
        if occurred_at is not None:
            meta["occurred_at"] = occurred_at.isoformat()
        return PeerSyncEvent.objects.create(
            binding_role=binding_role,
            binding_id=binding_id or str(self.binding.pk),
            direction=direction,
            kind=kind,
            target_type="order",
            target_ref=target_ref,
            status=status,
            attempts=attempts,
            payload=payload,
            meta=meta,
            idempotency_key=uuid.uuid4().hex,
            **over,
        )


class ReconcileRepairTests(_ReconcileWorkerFixture):
    """③ / ④：对账重排（只碰 done/failed、清 attempts、对端不可达只要计错误）。"""

    def test_orders_only_downstream_reconciliation_leaves_both_chat_protocols_unchanged(self):
        rows = [self._event(target_ref=f"SYN-{target}", occurred_at=self.now) for target in (
            "chat", "chat_core", "order",
        )]
        for row, target in zip(rows, ("chat", "chat_core", "order"), strict=True):
            PeerSyncEvent.objects.filter(pk=row.pk).update(target_type=target)
        before = [PeerSyncEvent.objects.values().get(pk=row.pk) for row in rows[:2]]
        with mock.patch("apps.business.peer_worker.fetch_applied_watermarks", return_value={}) as fetch:
            summary = run_reconcile(now=self.now, orders_only=True)
        self.assertEqual(fetch.call_args.args[2], ["SYN-order"])
        self.assertEqual(summary["requeued"], 1)
        self.assertEqual([PeerSyncEvent.objects.values().get(pk=row.pk) for row in rows[:2]], before)

    def test_a_failed_event_is_requeued_when_the_peer_is_behind(self):
        event = self._event(occurred_at=self.now - timedelta(minutes=1))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ) as fetch:
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertIsNone(event.next_attempt_at)
        self.assertEqual(event.reason, "对账发现缺口，重投")
        self.assertEqual(summary, {"requeued": 1, "errors": 0})
        # 只问这一条候选单号（⛔ 不为没有候选的绑定发 HTTP）。
        self.assertEqual(fetch.call_args.args[0], self.binding.peer_base_url)
        self.assertEqual(fetch.call_args.args[1], self.binding.token_out)
        self.assertEqual(fetch.call_args.args[2], [event.target_ref])

    # ── 发送方**同时带**内置号那一把键（计划 §4 的兼容 / §5-6）──────
    def test_the_builtin_order_no_is_sent_as_the_second_key(self):
        """每条候选把载荷里的**内置号**当第二个键发出去（收方任一对上即命中）。"""
        event = self._event(
            target_ref="SYN-SRC-RECON-1",
            occurred_at=self.now - timedelta(minutes=1),
            payload={"status": OrderStatus.IN_PROGRESS, "order_no": "SYN-BUILTIN-RECON-1"},
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={"SYN-SRC-RECON-1": None},
        ) as fetch:
            run_reconcile(now=self.now)
        self.assertEqual(
            fetch.call_args.kwargs.get("order_nos"),
            {"SYN-SRC-RECON-1": "SYN-BUILTIN-RECON-1"},
        )

    def test_an_event_without_a_builtin_order_no_sends_an_empty_second_key(self):
        """载荷里没有内置号（截图等）⇒ 第二把键是空映射，⛔ 不塞 `None` / 不猜。"""
        event = self._event(
            occurred_at=self.now - timedelta(minutes=1),
            payload={"status": OrderStatus.IN_PROGRESS},
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ) as fetch:
            run_reconcile(now=self.now)
        self.assertEqual(fetch.call_args.kwargs.get("order_nos"), {})

    def test_a_done_event_is_also_a_candidate(self):
        """`done` 也要查：事件"投成功"与"下家真应用了"是两回事（这正是丢单的成因）。"""
        event = self._event(
            status=PeerSyncEvent.Status.DONE,
            attempts=1,
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(summary["requeued"], 1)

    def test_a_done_event_with_a_reason_is_not_requeued(self):
        """`done` 但 `reason` 非空 ⇒ **有意作废**的行，⛔ 永不重投。

        两类来源：撤单投递前复核判定"仍是当前接手人"（本地作废，压根没发出去）、
        以及别的"有意跳过"。这类行对端**永远不会**应用它、也就不会推进水位 ⇒ 不排除的话
        对账每轮（`--loop` 10 秒）都把它重新排成 `pending`，持续整个 24 小时窗口。
        判别力：候选仍是 `status IN (done, failed)` ⇒ 本行会被重排成 `pending`、且会发出 HTTP ⇒ 红。
        """
        event = self._event(
            status=PeerSyncEvent.Status.DONE,
            attempts=1,
            reason="撤单已作废：该账号仍是这张单的当前接手人",
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ) as fetch:
            summary = run_reconcile(now=self.now)
        # 唯一一条候选被排除 ⇒ 一个 HTTP 都不发（空的绑定不该产生出站流量）。
        fetch.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, "撤单已作废：该账号仍是这张单的当前接手人")
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_a_done_event_without_a_reason_is_requeued_when_behind(self):
        """正面控制：`done` + `reason` 为空 + 水位落后 ⇒ **仍要**重投（丢单的典型形态）。

        与上一条成对：排除的是"有意作废"（`reason` 非空），⛔ 不是把 `done` 整个排除掉。
        """
        event = self._event(
            status=PeerSyncEvent.Status.DONE,
            attempts=1,
            reason="",
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.attempts, 0)
        self.assertEqual(summary, {"requeued": 1, "errors": 0})

    def test_a_done_hall_throw_is_not_requeued_even_when_watermark_is_null(self):
        """`hall.*` 镜像事件**不参与对账重排**（D-0303 生产实锤 2026-10-25）。

        接收侧对镜像事件只落镜像行、**不建订单与水位** ⇒ 水位查询必回 `null` ⇒ 按
        「回 null ⇒ 重排」的既有判据会**永久循环**：铺底 655 条投完后每轮
        「投递 50 · 对账补 50」无限空转。镜像事件天然 at-least-once（幂等 upsert），
        漏的 throw 由铺底命令 / 后续 `hall.take` 收口。
        判别力：候选含 `hall.throw` ⇒ 本行会被重排成 `pending`、且发出 HTTP ⇒ 红。
        """
        event = self._event(
            kind="hall.throw",
            status=PeerSyncEvent.Status.DONE,
            attempts=1,
            reason="",
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ) as fetch:
            summary = run_reconcile(now=self.now)
        fetch.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_a_failed_event_with_a_reason_is_still_requeued(self):
        """`failed` + 非空 `reason` **仍是**候选：那是真投递失败（`reason` 是对端给的原因）。

        判别力：把判据误写成"`reason` 非空一律排除" ⇒ 本行不再被补投 ⇒ 红。
        """
        event = self._event(
            status=PeerSyncEvent.Status.FAILED,
            attempts=5,
            reason="对端拒收：单号不存在",
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(summary, {"requeued": 1, "errors": 0})

    def test_an_event_the_peer_already_applied_is_left_alone(self):
        event = self._event(occurred_at=self.now - timedelta(minutes=5))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: _now_plus(60).isoformat()},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 5)
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_a_watermark_exactly_at_the_event_moment_counts_as_applied(self):
        """判据是 `>=`（相等 = 对端已有这条）：把 `>=` 写成 `>` ⇒ 本用例红。"""
        moment = self.now - timedelta(minutes=1)
        event = self._event(occurred_at=moment)
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: moment.isoformat()},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(summary["requeued"], 0)

    def test_a_watermark_behind_the_event_requeues_it(self):
        moment = self.now - timedelta(minutes=1)
        event = self._event(occurred_at=moment)
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: (moment - timedelta(seconds=30)).isoformat()},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(summary["requeued"], 1)

    def test_requeueing_resets_attempts_so_the_event_gets_a_fresh_ladder(self):
        """⭐ 承重：重排必须把 `attempts` **清零**。

        判别力：不清零 ⇒ 一条 `attempts == 5` 的 `failed` 事件重排后，下一投
        `attempts` 变 6 > `len(RETRY_BACKOFF_SECONDS)` ⇒ **立刻又终态**（补了等于没补）。
        """
        event = self._event(attempts=5, occurred_at=self.now - timedelta(minutes=1))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ):
            run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.reason, RECONCILE_REQUEUE_REASON)
        self.assertEqual(event.reason, "对账发现缺口，重投")
        self.assertIsNone(event.next_attempt_at)

    def test_a_pending_event_is_not_touched_and_does_not_trigger_any_http(self):
        """`pending` 归正常退避管，对账**不抢**；没有候选 ⇒ 一个 HTTP 都不发。"""
        pending = self._event(
            status=PeerSyncEvent.Status.PENDING,
            attempts=2,
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks"
        ) as fetch:
            summary = run_reconcile(now=self.now)
        fetch.assert_not_called()
        pending.refresh_from_db()
        self.assertEqual(pending.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(pending.attempts, 2)
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_an_event_outside_the_window_is_not_a_candidate(self):
        event = self._event(occurred_at=self.now - timedelta(minutes=1))
        PeerSyncEvent.objects.filter(pk=event.pk).update(
            created_at=self.now - timedelta(hours=RECONCILE_WINDOW_HOURS + 1)
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks"
        ) as fetch:
            summary = run_reconcile(now=self.now)
        fetch.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_inbound_and_upstream_events_are_not_candidates(self):
        self._event(
            target_ref="SYN-IN-1", direction="in",
            occurred_at=self.now - timedelta(minutes=1),
        )
        other_binder = get_user_model().objects.create_user(
            username="peer-reconcile-up-binder", password="pw-reconcile"
        )
        upstream = PeerUpstreamBinding.objects.create(
            peer_name="测试上家乙",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="peer-reconcile-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-down.test.invalid",
            bound_by=other_binder,
        )
        self._event(
            target_ref="SYN-UP-OUT-1", binding_role="upstream",
            binding_id=str(upstream.pk),
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks"
        ) as fetch:
            summary = run_reconcile(now=self.now)
        fetch.assert_not_called()
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_a_revoked_binding_is_not_reconciled(self):
        self.binding.state = PeerDownstreamBinding.State.REVOKED
        self.binding.save(update_fields=["state"])
        self._event(occurred_at=self.now - timedelta(minutes=1))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks"
        ) as fetch:
            summary = run_reconcile(now=self.now)
        fetch.assert_not_called()
        self.assertEqual(summary, {"requeued": 0, "errors": 0})

    def test_an_event_without_a_parseable_occurred_at_is_skipped(self):
        event = self._event(meta={"occurred_at": "not-a-time"})
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 5)
        self.assertEqual(summary["requeued"], 0)

    def test_an_event_without_any_occurred_at_is_skipped(self):
        event = self._event(meta={})
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: None},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(summary["requeued"], 0)

    def test_an_unparseable_watermark_counts_as_behind(self):
        """对端回了个解析不出的水位 ⇒ 当作"没有"重投（⛔ 不静默当成"已有"）。"""
        event = self._event(occurred_at=self.now - timedelta(minutes=1))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={event.target_ref: "garbage"},
        ):
            summary = run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(summary["requeued"], 1)

    def test_reconcile_failure_never_raises(self):
        """对账是后台动作：对端不可达 ⇒ 记台账 / 返回计数，⛔ 不让整轮炸掉。"""
        self._event(occurred_at=self.now - timedelta(minutes=1))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            side_effect=PeerUnreachable("boom"),
        ):
            summary = run_reconcile(now=self.now)
        self.assertEqual(summary, {"requeued": 0, "errors": 1})

    def test_a_rejection_is_an_error_too(self):
        self._event(occurred_at=self.now - timedelta(minutes=1))
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            side_effect=PeerRejected("nope"),
        ):
            summary = run_reconcile(now=self.now)
        self.assertEqual(summary, {"requeued": 0, "errors": 1})

    def test_one_unreachable_binding_does_not_stop_the_others(self):
        """逐绑定异常边界：一个对端不可达 ⇒ 别的绑定照常对账。"""
        reachable_account = get_user_model().objects.create_user(
            username="peer-reconcile-sender-2", password="pw-reconcile"
        )
        reachable = PeerDownstreamBinding.objects.create(
            account=reachable_account,
            peer_base_url="https://peer-down2.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        good = self._event(
            target_ref="SYN-GOOD", binding_id=str(reachable.pk),
            occurred_at=self.now - timedelta(minutes=1),
        )
        self._event(target_ref="SYN-BAD", occurred_at=self.now - timedelta(minutes=1))

        def _fetch(base_url, token, refs, **kwargs):
            # ⚠️ `**kwargs`：发送方多带第二个键 `order_nos`（计划 §4）
            #    ⇒ 桩必须吃下它，否则这条用例会以 `TypeError` 红在桩的签名上（E-0029 同族）。
            if base_url == self.binding.peer_base_url:
                raise PeerUnreachable("boom")
            return {good.target_ref: None}

        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks", side_effect=_fetch
        ):
            summary = run_reconcile(now=self.now)

        good.refresh_from_db()
        self.assertEqual(good.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(summary, {"requeued": 1, "errors": 1})
        self.assertEqual(reachable.state, PeerDownstreamBinding.State.ACTIVE)

    def test_the_ref_list_for_one_binding_is_deduplicated(self):
        """同一单的多条事件只问一次单号（去重后发）。"""
        first = self._event(
            target_ref="SYN-DUP", attempts=1,
            status=PeerSyncEvent.Status.DONE,
            occurred_at=self.now - timedelta(minutes=2),
        )
        self._event(
            target_ref="SYN-DUP", attempts=5,
            occurred_at=self.now - timedelta(minutes=1),
        )
        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks",
            return_value={"SYN-DUP": None},
        ) as fetch:
            summary = run_reconcile(now=self.now)
        self.assertEqual(fetch.call_args.args[2], ["SYN-DUP"])
        self.assertEqual(summary["requeued"], 2)
        first.refresh_from_db()
        self.assertEqual(first.attempts, 0)


class _UpstreamRescueFixture(TenantTestCase):
    """PW-02 的夹具：一条**启用且未解绑**的上游绑定 + 一个固定时刻。

    ⚠️ 字段形状照 `test_peer_p3_deliver_upstream.py` 的 `_UpstreamDeliveryFixture`
    （`PeerUpstreamBinding` 的非空列必须给全，少一个就 `IntegrityError`）。
    ⚠️ `created_at` 是 `auto_now_add` ⇒ 夹具走 `.update()` 改（与既有窗口用例同一手法）。
    """

    def setUp(self):
        self.operator = get_user_model().objects.create_user(
            username="peer-reconcile-up-rescue", password="pw-reconcile"
        )
        self.upstream = PeerUpstreamBinding.objects.create(
            peer_name="测试上家丙",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="peer-rescue-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.operator,
        )
        self.now = timezone.now()

    def _event(self, *, target_ref="UP-RESCUE-1",
               status=PeerSyncEvent.Status.FAILED, attempts=5, created_at=None,
               binding_id=None, binding_role="upstream", **over):
        meta = dict(over.pop("meta", {}))
        meta.setdefault("occurred_at", self.now.isoformat())
        event = PeerSyncEvent.objects.create(
            binding_role=binding_role,
            binding_id=binding_id or str(self.upstream.pk),
            direction="out",
            kind="order.update",
            target_type="order",
            target_ref=target_ref,
            status=status,
            attempts=attempts,
            payload={"status": "PENDING_REVIEW"},
            meta=meta,
            idempotency_key=uuid.uuid4().hex,
            **over,
        )
        if created_at is not None:
            PeerSyncEvent.objects.filter(pk=event.pk).update(created_at=created_at)
        return event


class UpstreamRescueTests(_UpstreamRescueFixture):
    """⑤ 上行超龄重排：恰好一次 / 封顶 / 新鲜的归梯子 / pending 不抢 / 死行不救。"""

    def test_orders_only_upstream_rescue_leaves_both_chat_protocols_unchanged(self):
        rows = [self._event(target_ref=f"SYN-{target}", created_at=self.now - timedelta(hours=25))
                for target in ("chat", "chat_core", "order")]
        for row, target in zip(rows, ("chat", "chat_core", "order"), strict=True):
            PeerSyncEvent.objects.filter(pk=row.pk).update(target_type=target)
        before = [PeerSyncEvent.objects.values().get(pk=row.pk) for row in rows[:2]]
        summary = run_reconcile(now=self.now, orders_only=True)
        self.assertEqual(summary["requeued"], 1)
        self.assertEqual([PeerSyncEvent.objects.values().get(pk=row.pk) for row in rows[:2]], before)

    def test_a_stale_failed_upstream_event_is_requeued_exactly_once(self):
        """创建超 24 小时的 `failed` 上行行 ⇒ 重排回 `pending` + 台账留痕 + 计数并入 `requeued`。

        判别力：删掉上行兜底 ⇒ `requeued == 0` / 状态还是 `failed` ⇒ 红。
        ⚠️ 纯本地重排：全程**一个 HTTP 都不发**（`fetch_applied_watermarks` 是下行对账的桩）。
        """
        event = self._event(created_at=self.now - timedelta(hours=25))

        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks"
        ) as fetch:
            summary = run_reconcile(now=self.now)

        fetch.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.reason, "上行回传超龄，自动重排一次")
        self.assertIsNone(event.next_attempt_at)
        self.assertIsNone(event.processed_at)
        self.assertEqual(summary, {"requeued": 1, "errors": 0})
        # 台账留**人话**（`last_error` 类），健康计数⛔ 不碰（重排不是失败也不是成功）。
        self.upstream.refresh_from_db()
        self.assertEqual(self.upstream.last_error, "上行回传超龄未送达，已自动重排一次")
        self.assertEqual(self.upstream.last_error_at, self.now)
        self.assertEqual(self.upstream.consecutive_failures, 0)

    def test_the_rescue_does_not_reset_attempts_so_it_is_capped_at_one(self):
        """⭐ 封顶承重：重排**不清零** `attempts`——重排后那一投再失败（attempts=6）⇒ 不再重排。

        判别力：把重排写成「清零 `attempts`」（照抄下行对账的写法）或「不按 `attempts` 封顶」
        ⇒ 第二次 `run_reconcile` 又把它排回 `pending` ⇒ 本用例红。
        ⚠️ 与下行对账**刻意相反**：下行清零换一整副新梯子（有水位对账把关），
        上行只兜底**再试一次**（没有对账，⛔ 不能变成无限重试）。
        """
        event = self._event(created_at=self.now - timedelta(hours=25))

        run_reconcile(now=self.now)
        event.refresh_from_db()
        self.assertEqual(event.attempts, 5)     # ⭐ 不清零：它就是封顶的计数器
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)

        # 模拟「重排后的那一投又失败了」：`_attempt` 对 `attempts=5` 的行 +1 → 6 → 终态。
        PeerSyncEvent.objects.filter(pk=event.pk).update(
            status=PeerSyncEvent.Status.FAILED, attempts=6,
            reason="connection refused",
        )
        summary = run_reconcile(now=self.now)

        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 6)
        self.assertEqual(summary["requeued"], 0)

    def test_a_requeued_event_is_not_requeued_again_while_still_pending(self):
        """重排后还是 `pending`（尚未到投递轮）⇒ 下一轮 `run_reconcile` 不再碰它。"""
        event = self._event(created_at=self.now - timedelta(hours=25))
        run_reconcile(now=self.now)

        summary = run_reconcile(now=self.now)

        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(summary["requeued"], 0)

    def test_a_fresh_failed_upstream_event_is_left_to_the_ladder(self):
        """创建不满 24 小时的终态行**不救**：正常梯子全程约 81 分钟，刚终态的行让时间说话。

        判别力：把年龄阈值删掉 ⇒ 本行会被重排 ⇒ 红。
        """
        event = self._event(created_at=self.now - timedelta(hours=1))

        summary = run_reconcile(now=self.now)

        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 5)
        self.assertEqual(summary["requeued"], 0)

    def test_a_pending_upstream_event_is_never_rescued(self):
        """`pending` 归正常退避管（租约到期下一轮再认领，不存在卡死的 pending）⇒ ⛔ 不抢。"""
        event = self._event(
            status=PeerSyncEvent.Status.PENDING, attempts=2,
            created_at=self.now - timedelta(hours=25),
        )

        summary = run_reconcile(now=self.now)

        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.attempts, 2)
        self.assertEqual(summary["requeued"], 0)

    def test_events_of_a_disabled_binding_are_not_rescued(self):
        """**停用**绑定的行不救：投了也会被 `_attempt` 按「绑定失效」再终态化
        （且 `dropped` 不动 `attempts`）⇒ 救它就是开「重排 → drop → 再重排」的死循环。"""
        self.upstream.enabled = False
        self.upstream.save(update_fields=["enabled"])
        event = self._event(created_at=self.now - timedelta(hours=25))

        first = run_reconcile(now=self.now)
        second = run_reconcile(now=self.now)

        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(first["requeued"], 0)
        self.assertEqual(second["requeued"], 0)
        # ⛔ 台账一个字都不写（没救成任何东西）。
        self.upstream.refresh_from_db()
        self.assertEqual(self.upstream.last_error, "")
        self.assertIsNone(self.upstream.last_error_at)

    def test_events_of_a_soft_deleted_binding_are_not_rescued(self):
        """**解绑**（`deleted_at` 非空）同上：行随绑定一起变死信，⛔ 不进重排。"""
        self.upstream.deleted_at = self.now
        self.upstream.save(update_fields=["deleted_at"])
        event = self._event(created_at=self.now - timedelta(hours=25))

        summary = run_reconcile(now=self.now)

        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(summary["requeued"], 0)

    def test_a_stale_failed_downstream_event_is_not_rescued_here(self):
        """⛔ 上行兜底**不碰下行行**——下行归水位对账管（窗口、候选、清零口径全不同）。

        同一条 25 小时前的 `failed` 下行行，两套机制都**不**动它：
        水位对账的窗口是 24 小时（`test_an_event_outside_the_window_is_not_a_candidate`），
        上行兜底只认 `binding_role="upstream"`。
        """
        account = get_user_model().objects.create_user(
            username="peer-reconcile-down-rescue", password="pw-reconcile"
        )
        downstream = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-down9.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        event = self._event(
            target_ref="DOWN-NOT-RESCUED",
            binding_id=str(downstream.pk),
            # 夹具默认造上行行，这里显式造成**下行**行。
            binding_role="downstream",
            created_at=self.now - timedelta(hours=25),
        )

        with mock.patch(
            "apps.business.peer_worker.fetch_applied_watermarks"
        ) as fetch:
            summary = run_reconcile(now=self.now)

        fetch.assert_not_called()     # 下行对账没有候选（绑定无 `downstream` 候选行）
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 5)
        self.assertEqual(summary["requeued"], 0)


class FetchAppliedWatermarksTests(SimpleTestCase):
    """② 出站客户端：信封 + Bearer + 超时，形状漂了回 `{}`（⛔ 不崩）。"""

    BASE_URL = "https://peer-up.test.invalid"

    def _response(self, envelope):
        # ⭐ C5：出站改走流式读 ⇒ 假件换共用那一份（`tests/_peer_stream_fakes`）。
        from tests._peer_stream_fakes import stream_response

        return stream_response(envelope)

    def test_the_orders_are_posted_with_the_bearer_token(self):
        with mock.patch(
            "apps.business.peer_client.httpx.stream",
            return_value=self._response({"code": 0, "data": {"applied": {"A-1": None}}}),
        ) as post:
            applied = fetch_applied_watermarks(self.BASE_URL, "TOKEN-IN", ["A-1"])
        self.assertEqual(applied, {"A-1": None})
        self.assertEqual(
            post.call_args.args[1],
            "https://peer-up.test.invalid/api/business/peer-sync/reconcile/",
        )
        self.assertEqual(post.call_args.kwargs["json"], {"orders": ["A-1"]})
        self.assertEqual(
            post.call_args.kwargs["headers"], {"Authorization": "Bearer TOKEN-IN"}
        )

    def test_the_second_key_is_posted_when_given(self):
        """第二个键（内置号）**有才发**：老对端不认识它、多一个键对它是噪声（计划 §4）。

        判别力：把 `order_nos` 无条件塞进请求体 ⇒ 上一条用例（只给 `orders` 时请求体
        逐字等于 `{"orders": ["A-1"]}`）红。
        """
        with mock.patch(
            "apps.business.peer_client.httpx.stream",
            return_value=self._response({"code": 0, "data": {"applied": {}}}),
        ) as post:
            fetch_applied_watermarks(
                self.BASE_URL,
                "TOKEN-IN",
                ["SYN-SRC-1"],
                order_nos={"SYN-SRC-1": "SYN-BUILTIN-1"},
            )
        self.assertEqual(
            post.call_args.kwargs["json"],
            {
                "orders": ["SYN-SRC-1"],
                "order_nos": {"SYN-SRC-1": "SYN-BUILTIN-1"},
            },
        )

    def test_a_missing_or_non_object_applied_returns_an_empty_mapping(self):
        for envelope in (
            {"code": 0},
            {"code": 0, "data": None},
            {"code": 0, "data": []},
            {"code": 0, "data": {"applied": "nope"}},
            {"code": 0, "data": {"applied": ["A-1"]}},
        ):
            with self.subTest(envelope=envelope):
                with mock.patch(
                    "apps.business.peer_client.httpx.stream",
                    return_value=self._response(envelope),
                ):
                    self.assertEqual(
                        fetch_applied_watermarks(self.BASE_URL, "TOKEN-IN", ["A-1"]),
                        {},
                    )

    def test_a_non_zero_code_is_a_rejection(self):
        with mock.patch(
            "apps.business.peer_client.httpx.stream",
            return_value=self._response({"code": 1020, "msg": "令牌无效"}),
        ):
            with self.assertRaises(PeerRejected):
                fetch_applied_watermarks(self.BASE_URL, "TOKEN-IN", ["A-1"])
