"""大厅镜像**全量拉取兜底**（计划 `2026-10-25-peer-hall-pull` v4）。

覆盖三块，每个判据都能被单独弄红：

① **上家端点** `POST /api/business/peer-sync/hall/list/`：类型闸 / 可见性三闸三态 /
   keyset 分页 + count / 载荷按来源同源 / **Z20 拉取证据与响应同事务**；
② **下家 worker** `peer_worker.run_hall_pull`：开关标记、游标跨轮续传、单轮预算 /
   页数上限、5 分钟节拍 / 失败退避 / 老对端 404 TTL、拉全才判消失、复活闸；
③ **串行化**：last_seen 节流、hop 取最大值、只在新建/复活时转发下游。

⚠️ 夹具值全部合成值（`*.test.invalid` / `2026…`）；断言打数据库行与响应载荷（E-0024）。
⚠️ 必须用 `TenantTestCase`（`apps.business` 只在 `TENANT_APPS` 里）。
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_model, schema_context

from apps.business import peer_worker
from apps.business.api import peer_views
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_client import (
    PeerHallBodyIncomplete, PeerUnsupported, PeerUnreachable, enforce_total_peer_timeout,
    new_peer_deadline_loop,
)
from apps.business.peer_dispatch import mirror_hall_payload
from apps.business.peer_hall import display_price
from apps.business.peer_inbound import (
    PEER_HALL_HOP_LIMIT,
    _hall_throw_delivered,
    apply_hall_pull_rows,
)
from apps.business.peer_sync import (
    HALL_THROW_KIND,
    hash_token,
    new_token,
)
from apps.business.peer_worker import (
    HALL_PULL_BACKOFF_SECONDS,
    HALL_PULL_MAX_PAGES,
    HALL_PULL_PAGE_LIMIT,
    HALL_PULL_PAGE_TIMEOUT_SECONDS,
    HALL_PULL_PERIOD_SECONDS,
    HALL_PULL_ROUND_BUDGET_SECONDS,
    HALL_PULL_UNSUPPORTED_TTL_SECONDS,
    fetch_hall_page,
    run_hall_pull,
)

DOMAIN = "tenant.test.com"
HALL_LIST_URL = "/api/business/peer-sync/hall/list/"
UP_BASE = "https://peer-pull-up.test.invalid"
DOWN_BASE = "https://peer-pull-down.test.invalid"
GAME_NAME = "拉取夹具游戏"


def payload(no, *, price="50.00", game_name=GAME_NAME, source_order_no=""):
    """一条 `peer_sync.order_payload` 形状的合成载荷。"""
    return {
        "order_no": no,
        "status": "WAITING_GRAB",
        "hall_price": price,
        "remark": "syn-拉取备注",
        "game_name": game_name,
        "server_id": None,
        "project_id": None,
        "project_text": "syn-拉取项目",
        "customer_phone": "",
        "customer_qq": "",
        "content": "syn-拉取需求",
        "source_order_no": source_order_no,
    }


def pull_page(*refs, next_cursor=None, count=None, shared=True, complete=None):
    """一页拉取响应（worker 桩的返回形状）。"""
    rows = [
        {"target_ref": ref, "payload": payload(ref, source_order_no=ref)}
        for ref in refs
    ]
    return {
        "shared": shared,
        "count": len(rows) if count is None else count,
        "rows": rows,
        "next_cursor": next_cursor,
        "complete": (next_cursor is None) if complete is None else complete,
    }


class PullFixture(TenantTestCase):
    """一条「我方为下家」的绑定（拉取方）+ 一条「我方为上家」的绑定（被拉方）。"""

    def setUp(self):
        # ⚠️ 模块级游标表是进程内存：用例之间必须清空，否则上一条的游标会串进来。
        peer_worker.reset_hall_pull_state()
        self.now = timezone.now()
        self.game = Game.objects.create(name=GAME_NAME)
        self.operator = get_user_model().objects.create_user(
            username="peer-pull-operator", password="pw-pull"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="拉取夹具上家",
            peer_base_url=UP_BASE,
            peer_account="syn-pull-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url=DOWN_BASE,
            bound_by=self.operator,
            hall_sync_enabled=True,
            enabled=True,
        )
        # 被拉的一方：下家拿着这枚令牌来拉我们的厅。
        self.down_account = get_user_model().objects.create_user(
            username="peer-pull-down-account", password="pw-down"
        )
        self.down_token = new_token()
        self.down_binding = PeerDownstreamBinding.objects.create(
            account=self.down_account,
            peer_base_url=DOWN_BASE,
            token_in_hash=hash_token(self.down_token),
            token_out=new_token(),
            state=PeerDownstreamBinding.State.ACTIVE,
            hall_push_enabled=True,
        )
        # 转发目标：本租户的下家（拉取落地「新建 / 复活」时才应该收到 hall.throw）。
        self.next_account = get_user_model().objects.create_user(
            username="peer-pull-next-account", password="pw-next"
        )
        self.next_binding = PeerDownstreamBinding.objects.create(
            account=self.next_account,
            peer_base_url="https://peer-pull-next.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            state=PeerDownstreamBinding.State.ACTIVE,
            hall_push_enabled=True,
        )

    # ── 夹具 ───────────────────────────────────────────────────────────
    def _order(self, no, *, created_at=None, status=OrderStatus.WAITING_GRAB,
               source_order_no=""):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal("88.00"),
            source_order_no=source_order_no,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        if created_at is not None:
            Order.objects.filter(pk=order.pk).update(created_at=created_at)
            order.refresh_from_db()
        return order

    def _mirror(self, no, *, status=PeerHallMirror.Status.LISTED,
                first_seen_at=None, last_seen_at=None, gone_at=None, hop=0,
                binding=None):
        return PeerHallMirror.objects.create(
            peer_upstream_binding=binding or self.binding,
            peer_base_url=(binding or self.binding).peer_base_url,
            peer_order_no=no,
            status=status,
            game=self.game,
            payload=payload(no),
            remote_hall_price=Decimal("50.00"),
            hop=hop,
            first_seen_at=first_seen_at or self.now,
            last_seen_at=last_seen_at,
            gone_at=gone_at,
        )

    # ── HTTP 夹具 ──────────────────────────────────────────────────────
    _DEFAULT_TOKEN = object()

    def _post_list(self, body=None, *, token=_DEFAULT_TOKEN, client=None):
        client = client or Client(SERVER_NAME=DOMAIN)
        headers = {}
        if token is self._DEFAULT_TOKEN:
            token = self.down_token
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return client.post(
            HALL_LIST_URL,
            json.dumps({} if body is None else body),
            content_type="application/json",
            **headers,
        )


# ══ ① 端点 ═══════════════════════════════════════════════════════════════
class HallListEndpointTests(PullFixture):
    def test_logs_only_safe_correlated_request_ids_for_authenticated_peers(self):
        trace_logger = "apps.business.api.peer_views.hall_pull_trace"
        request_id = "a1" * 16
        client = Client(SERVER_NAME=DOMAIN, HTTP_X_PEER_HALL_REQUEST_ID=request_id)
        self.assertTrue(logging.getLogger(trace_logger).isEnabledFor(logging.INFO))
        with self.assertLogs(trace_logger, level="INFO") as traces:
            body = self._post_list(client=client).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(len(traces.output), 1)
        self.assertIn(f"request_id={request_id} phase=response_prepared", traces.output[0])
        self.assertNotIn(self.down_token, traces.output[0])

        with self.assertNoLogs(trace_logger, level="INFO"):
            self.assertEqual(self._post_list(client=client, token=None).status_code, 401)
            invalid = Client(SERVER_NAME=DOMAIN, HTTP_X_PEER_HALL_REQUEST_ID="invalid trace")
            self.assertEqual(self._post_list(client=invalid).json()["code"], 0)

    def test_no_token_or_a_wrong_token_is_401(self):
        for token in (None, new_token()):
            with self.subTest(token=token):
                resp = self._post_list(token=token)
                self.assertEqual(resp.status_code, 401, resp.content[:300])
                self.assertEqual(resp.json()["code"], 401)

    def test_an_upstream_token_is_a_loud_business_error_not_500(self):
        """两张绑定表共用一套校验：上游令牌也能过认证 ⇒ 类型闸必须响亮回绝。"""
        raw = new_token()
        PeerUpstreamBinding.objects.create(
            peer_name="另一条下家绑定",
            peer_base_url="https://peer-pull-self.test.invalid",
            peer_account="syn-pull-self",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
            local_base_url=DOWN_BASE,
            bound_by=self.operator,
            hall_sync_enabled=True,
        )
        resp = self._post_list(token=raw)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 1020, body)
        self.assertIn("绑定类型", body["msg"])

    def test_shared_false_when_push_is_off(self):
        """闸 2（hall_push_enabled）关着 ⇒ shared=false，⛔ 不是「空厅」。"""
        self._order("20261025000000000001")
        self.down_binding.hall_push_enabled = False
        self.down_binding.save(update_fields=["hall_push_enabled"])
        body = self._post_list().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"],
            {"shared": False, "rows": [], "next_cursor": None,
             "count": 0, "complete": False},
        )

    def test_shared_true_with_an_empty_hall(self):
        body = self._post_list().json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["shared"], True)
        self.assertEqual(body["data"]["rows"], [])
        self.assertEqual(body["data"]["count"], 0)
        self.assertIs(body["data"]["complete"], True)

    def test_only_this_tenants_claimable_hall_cards_are_returned(self):
        native = self._order("20261025000000000002")
        taken = self._order("20261025000000000003", status=OrderStatus.IN_PROGRESS)
        mirror = self._mirror("20261025000000000004")
        # 归属绑定关掉接收开关的镜像行不出现（闸 3，照 peer_hall_queryset）。
        off_binding = PeerUpstreamBinding.objects.create(
            peer_name="关掉接收的上家",
            peer_base_url="https://peer-pull-off.test.invalid",
            peer_account="syn-pull-off",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url=DOWN_BASE,
            bound_by=self.operator,
            hall_sync_enabled=False,
        )
        self._mirror("20261025000000000005", binding=off_binding)
        body = self._post_list().json()
        refs = {row["target_ref"] for row in body["data"]["rows"]}
        self.assertEqual(
            refs, {"20261025000000000002", "20261025000000000004"}, body
        )
        self.assertNotIn(taken.order_no, refs)
        self.assertEqual(mirror.peer_order_no, "20261025000000000004")

    def test_rows_use_the_same_payload_shape_as_push(self):
        """原生走 order_payload（⛔ 无 total_price）；镜像走 mirror_hall_payload（展示价）。"""
        native = self._order("20261025000000000006", source_order_no="SRC-6")
        mirror = self._mirror("20261025000000000007")
        mirror.remote_hall_price = Decimal("50.00")
        mirror.save(update_fields=["remote_hall_price"])
        self.binding.hall_price_percent = 90
        self.binding.save(update_fields=["hall_price_percent"])
        body = self._post_list().json()
        rows = {row["target_ref"]: row["payload"] for row in body["data"]["rows"]}
        native_row = rows["SRC-6"]           # 原生卡的同步身份 = 来源单号
        self.assertNotIn("total_price", native_row)
        self.assertEqual(native_row["order_no"], native.order_no)
        mirror_row = rows["20261025000000000007"]
        self.assertEqual(mirror_row["hall_price"], "45.00")   # 90% 展示价
        self.assertEqual(mirror_row["hop"], 1)                # 行 hop + 1
        self.assertNotIn("total_price", mirror_row)

    def test_keyset_pagination_walks_every_row_once_and_count_is_total(self):
        for index in range(3):
            self._order(
                f"2026102500000000010{index}",
                created_at=self.now + timedelta(seconds=index),
            )
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            body = self._post_list(
                {"cursor": cursor, "limit": 2} if cursor else {"limit": 2}
            ).json()
            data = body["data"]
            self.assertEqual(body["code"], 0, body)
            self.assertEqual(data["count"], 3)
            seen.extend(row["target_ref"] for row in data["rows"])
            pages += 1
            cursor = data["next_cursor"]
            if cursor is None:
                self.assertIs(data["complete"], True)
                break
            self.assertLessEqual(pages, 5)
        self.assertEqual(seen, [
            "20261025000000000100",
            "20261025000000000101",
            "20261025000000000102",
        ])
        self.assertEqual(pages, 2)

    def test_limit_shape_and_cap(self):
        self._order("20261025000000000200")
        for bad in ("abc", -1, True):
            with self.subTest(bad=bad):
                body = self._post_list({"limit": bad}).json()
                self.assertEqual(body["code"], 1000, body)
        body = self._post_list({"limit": 100000}).json()
        self.assertEqual(body["code"], 0, body)   # 超上限只夹紧，不报错

    def test_cursor_is_opaque_and_shape_checked_not_parsed_as_an_integer(self):
        self._order("20261025000000000300")
        self._order("20261025000000000301", created_at=self.now + timedelta(seconds=1))
        for bad in (123, {"x": 1}, "not a cursor!!", "x" * 600):
            with self.subTest(bad=bad):
                body = self._post_list({"cursor": bad}).json()
                self.assertEqual(body["code"], 1000, body)
        # 第一页回的游标是**不透明串**（⛔ 不是整数偏移）。
        data = self._post_list({"limit": 1}).json()["data"]
        self.assertIsInstance(data["next_cursor"], str)
        self.assertFalse(data["next_cursor"].isdigit())

    def test_query_count_is_constant_across_page_sizes(self):
        for index in range(8):
            self._order(
                f"2026102500000000040{index}",
                created_at=self.now + timedelta(seconds=index),
            )
        with CaptureQueriesContext(connection) as small:
            self._post_list({"limit": 1})
        with CaptureQueriesContext(connection) as large:
            self._post_list({"limit": 8})
        self.assertEqual(len(small.captured_queries), len(large.captured_queries))



    def test_hidden_orders_are_not_shared_as_native_hall_rows(self):
        """⭐ 评审 Important 7：`native_qs` 显式排除隐藏单（防御性：即使有 MAIN 行也不露）。"""
        from apps.business.models import PeerHallMirror

        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        native = self._order("20261025000000000009")
        card = make_mirror(game=self.game, bound_by=self.operator, no="SYN-SHADOW-PULL")
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261025000000000010",
            with_main=True,
        )
        body = self._post_list().json()
        self.assertEqual(body["code"], 0, body)
        # 大厅列表的行是**投递事件形状**（`target_ref` + `payload`）：原生行的 `payload.order_no`
        # 才是订单号（与 `test_rows_use_the_same_payload_shape` 同一口径）。
        nos = [row["payload"]["order_no"] for row in body["data"]["rows"]]
        self.assertIn(native.order_no, nos)                # 正对照
        self.assertNotIn(hidden.order_no, nos)
        # `count` = 原生可抢单 + 镜像卡（本夹具里那张 `UP-READ-1` 也是 listed）
        self.assertEqual(body["data"]["count"], 2)


class Z20PullEvidenceTests(PullFixture):
    def _pull_events(self, *, ref, binding=None):
        return PeerSyncEvent.objects.filter(
            direction="in",
            kind="hall.pull",
            binding_role="downstream",
            binding_id=str((binding or self.down_binding).pk),
            target_ref=ref,
            status=PeerSyncEvent.Status.DONE,
            reason="",
        )

    def test_every_returned_card_gets_one_pull_evidence_row(self):
        self._order("20261025000000000500")
        body = self._post_list().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._pull_events(ref="20261025000000000500").count(), 1)
        # 再拉一次：不是每页写一条，按卡去重（部分唯一 + ignore_conflicts）。
        self._post_list()
        self.assertEqual(self._pull_events(ref="20261025000000000500").count(), 1)
        row = self._pull_events(ref="20261025000000000500").get()
        self.assertEqual(row.target_type, "hall")
        self.assertTrue(row.idempotency_key)

    def test_cards_not_returned_get_no_evidence(self):
        """先过闸、只对真正返回的行写（⛔ 先写后过滤 = 把没返回的卡写成可接）。"""
        for index in range(3):
            self._order(
                f"2026102500000000060{index}",
                created_at=self.now + timedelta(seconds=index),
            )
        body = self._post_list({"limit": 1}).json()
        self.assertEqual(len(body["data"]["rows"]), 1)
        returned = body["data"]["rows"][0]["target_ref"]
        self.assertEqual(self._pull_events(ref=returned).count(), 1)
        for ref in ("20261025000000000601", "20261025000000000602"):
            self.assertEqual(self._pull_events(ref=ref).count(), 0)

    def test_claim_gate_accepts_pull_evidence_but_never_a_fake_or_aggregate(self):
        order = self._order("20261025000000000700")
        self.assertFalse(_hall_throw_delivered(
            self.down_binding, target_ref="20261025000000000700"
        ))
        self._post_list()
        self.assertTrue(_hall_throw_delivered(
            self.down_binding, target_ref="20261025000000000700"
        ))
        # 近似形状不算：direction / binding / status / reason 任一不对都仍是 False。
        PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=str(self.next_binding.pk),
            direction="in", kind="hall.pull", target_type="hall",
            target_ref="20261025000000000700",
            status=PeerSyncEvent.Status.DONE, reason="",
            idempotency_key=new_token(),
        )
        self.assertTrue(_hall_throw_delivered(
            self.down_binding, target_ref="20261025000000000700"
        ))   # 真证据还在
        # 另一张**从未出现在任何拉取响应里**的卡仍然拒（防安全倒退的负向对照）。
        self.assertFalse(_hall_throw_delivered(
            self.down_binding, target_ref="20261025000000000799"
        ))
        self.assertEqual(order.order_no, "20261025000000000700")

    def test_evidence_write_failure_fails_the_whole_request(self):
        """证据与响应同一事务：写失败 ⇒ 端点失败（⛔ 不能「看见了却没证据」）。"""
        self._order("20261025000000000800")
        client = Client(SERVER_NAME=DOMAIN, raise_request_exception=False)
        with mock.patch(
            "apps.business.api.peer_views._write_pull_evidence",
            side_effect=RuntimeError("evidence write boom"),
        ):
            resp = self._post_list(client=client)
        self.assertGreaterEqual(resp.status_code, 500, resp.content[:300])


# ══ ② 客户端 ═══════════════════════════════════════════════════════════
class FetchHallPageTests(TenantTestCase):
    def _response(self, envelope, *, status_code=200):
        # ⭐ C5：出站改走流式读 ⇒ 假件换共用那一份（`tests/_peer_stream_fakes`）。
        from tests._peer_stream_fakes import stream_response

        return stream_response(envelope, status_code=status_code)

    def test_posts_cursor_and_limit_with_the_bearer_token_and_short_timeout(self):
        with mock.patch(
            "apps.business.peer_client.httpx.stream",
            return_value=self._response(
                {"code": 0, "data": pull_page("A-1", next_cursor=None)}
            ),
        ) as post:
            data = fetch_hall_page(
                UP_BASE, "TOKEN-PULL", cursor="CURSOR-1", limit=7
            )
        _method, url = post.call_args.args      # ⭐ C5：stream 的首个位置参数是方法名
        self.assertEqual(_method, "POST")
        self.assertEqual(url, f"{UP_BASE}/api/business/peer-sync/hall/list/")
        self.assertEqual(
            post.call_args.kwargs["json"], {"cursor": "CURSOR-1", "limit": 7}
        )
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer TOKEN-PULL")
        self.assertRegex(headers["X-Peer-Hall-Request-ID"], r"^[0-9a-f]{32}$")
        self.assertEqual(post.call_args.kwargs["timeout"], 1.5)
        self.assertEqual(HALL_PULL_PAGE_TIMEOUT_SECONDS, 1.5)
        self.assertLess(HALL_PULL_PAGE_TIMEOUT_SECONDS, HALL_PULL_ROUND_BUDGET_SECONDS)
        self.assertEqual(data["rows"][0]["target_ref"], "A-1")

    def test_404_and_405_are_a_distinct_unsupported_exception(self):
        for status in (404, 405):
            with self.subTest(status=status):
                with mock.patch(
                    "apps.business.peer_client.httpx.stream",
                    return_value=self._response({"detail": "not found"},
                                                status_code=status),
                ):
                    with self.assertRaises(PeerUnsupported) as ctx:
                        fetch_hall_page(UP_BASE, "TOKEN-PULL")
                self.assertIsInstance(ctx.exception, PeerUnreachable)

    def test_a_transport_error_stays_unreachable(self):
        import httpx

        with mock.patch(
            "apps.business.peer_client.httpx.stream",
            side_effect=httpx.ConnectError("boom"),
        ):
            with self.assertRaises(PeerUnreachable) as ctx:
                fetch_hall_page(UP_BASE, "TOKEN-PULL")
        self.assertNotIsInstance(ctx.exception, PeerUnsupported)

    def test_a_bad_envelope_shape_is_unreachable(self):
        for data in ({}, {"shared": True, "rows": "nope"}, {"rows": [{"payload": 1}]}):
            with self.subTest(data=data):
                with mock.patch(
                    "apps.business.peer_client.httpx.stream",
                    return_value=self._response({"code": 0, "data": data}),
                ):
                    with self.assertRaises(PeerUnreachable):
                        fetch_hall_page(UP_BASE, "TOKEN-PULL")


# ══ ③ worker ═══════════════════════════════════════════════════════════
class RunHallPullTests(PullFixture):
    def _fetch(self, *pages):
        return mock.patch.object(
            peer_worker, "fetch_hall_page", side_effect=list(pages)
        )

    def test_the_worker_treats_a_landing_crash_as_a_round_failure(self):
        """消费处兜底（⭐ A4 的 worker 半边）：落地抛异常 ⇒ 本轮按有失败收场、标记不清。

        变异对照：没有这层 try 时异常裸逃，`run_hall_pull` 直接 500（拉取循环被打死）。
        """
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        with self._fetch(pull_page("20261025000000003000")):
            with mock.patch.object(
                peer_worker,
                "apply_hall_pull_rows",
                side_effect=RuntimeError("落地崩了（测试注入）"),
            ):
                summary = run_hall_pull(now=self.now)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["errors"], 1)
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.hall_pull_requested_at)   # 标记不清 ⇒ 下轮续传
        self.assertLessEqual(self.binding.hall_pull_next_at, timezone.now())
        self.assertEqual(
            self.binding.consecutive_failures, 0
        )   # 拉取失败不进投递的停绑计数


    def test_constants_are_the_plan_numbers(self):
        self.assertEqual(HALL_PULL_PERIOD_SECONDS, 300)
        self.assertEqual(HALL_PULL_BACKOFF_SECONDS, 600)
        self.assertEqual(HALL_PULL_MAX_PAGES, 50)
        self.assertEqual(HALL_PULL_PAGE_LIMIT, 200)
        self.assertEqual(HALL_PULL_UNSUPPORTED_TTL_SECONDS, 45 * 60)
        self.assertEqual(HALL_PULL_ROUND_BUDGET_SECONDS, 2.0)

    def test_switching_the_switch_on_marks_an_immediate_full_pass(self):
        """PUT False→True ⇒ requested_at 置位 + next_at 到期（立刻拉一次全量）。"""
        self.binding.hall_sync_enabled = False
        self.binding.save(update_fields=["hall_sync_enabled"])
        client = self._staff_client()
        resp = client.put(
            f"/api/business/peer-bindings/{self.binding.pk}/",
            json.dumps({"hall_sync_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.hall_pull_requested_at)
        self.assertIsNotNone(self.binding.hall_pull_next_at)
        self.assertLessEqual(self.binding.hall_pull_next_at, timezone.now())
        # 连续再 PUT 一次 True→True：标记不刷新（同一枚标记，幂等）。
        first = self.binding.hall_pull_requested_at
        resp = client.put(
            f"/api/business/peer-bindings/{self.binding.pk}/",
            json.dumps({"hall_sync_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.json()["code"], 0)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_requested_at, first)

    def _staff_client(self):
        from tests.p5_auth_helpers import make_logged_in_client

        return make_logged_in_client(
            username="peer-pull-page-operator",
            groups=("客服",),
            games=[self.game],
            nodes=["system.bind_upstream_system"],
        )

    def test_a_complete_pass_clears_the_marker_and_schedules_five_minutes(self):
        self._order("20261025000000001000")
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        with self._fetch(pull_page("20261025000000001000")):
            summary = run_hall_pull(now=self.now)
        self.assertEqual(summary["pages"], 1)
        self.assertTrue(summary["complete"])
        self.binding.refresh_from_db()
        self.assertIsNone(self.binding.hall_pull_requested_at)
        self.assertEqual(
            self.binding.hall_pull_next_at,
            self.now + timedelta(seconds=HALL_PULL_PERIOD_SECONDS),
        )

    def test_a_budget_stop_keeps_the_marker_and_resumes_with_the_cursor(self):
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        clock = iter([0.0, 0.0, HALL_PULL_ROUND_BUDGET_SECONDS + 0.1])
        pages = [
            pull_page("20261025000000001100", next_cursor="CURSOR-2"),
            pull_page("20261025000000001101"),
        ]
        with self._fetch(*pages) as fetch:
            first = run_hall_pull(now=self.now, monotonic=lambda: next(clock))
        self.assertFalse(first["complete"])
        self.assertEqual(fetch.call_count, 1)
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.hall_pull_requested_at)
        self.assertLessEqual(self.binding.hall_pull_next_at, timezone.now())
        # 下一轮从上一轮的游标续传，而不是从头再来。
        with self._fetch(pages[1]) as fetch:
            second = run_hall_pull(now=self.now, monotonic=lambda: 0.0)
        self.assertEqual(fetch.call_args.kwargs["cursor"], "CURSOR-2")
        self.assertTrue(second["complete"])
        self.assertEqual(
            PeerHallMirror.objects.filter(peer_order_no__startswith="2026102500000000110").count(),
            2,
        )

    def test_the_page_cap_is_fifty_and_does_not_clear_the_marker(self):
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        with self._fetch(*[
            pull_page(f"202610250000000020{i:02d}", next_cursor=f"C{i}")
            for i in range(HALL_PULL_MAX_PAGES + 5)
        ]) as fetch:
            summary = run_hall_pull(now=self.now, monotonic=lambda: 0.0)
        self.assertEqual(fetch.call_count, HALL_PULL_MAX_PAGES)
        self.assertFalse(summary["complete"])
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.hall_pull_requested_at)

    def test_a_network_failure_backs_off_ten_minutes_and_keeps_the_marker(self):
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        with self._fetch(PeerUnreachable("timeout")):
            summary = run_hall_pull(now=self.now)
        self.assertEqual(summary["errors"], 1)
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.hall_pull_requested_at)
        self.assertEqual(
            self.binding.hall_pull_next_at,
            self.now + timedelta(seconds=HALL_PULL_BACKOFF_SECONDS),
        )
        self.assertEqual(self.binding.consecutive_failures, 0)

    def test_an_old_peer_404_gets_a_long_ttl_and_never_auto_stops_the_binding(self):
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        with self._fetch(PeerUnsupported("no endpoint")):
            summary = run_hall_pull(now=self.now)
        self.assertTrue(summary["unsupported"])
        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.hall_pull_next_at,
            self.now + timedelta(seconds=HALL_PULL_UNSUPPORTED_TTL_SECONDS),
        )
        self.assertIsNotNone(self.binding.hall_pull_requested_at)
        self.assertEqual(self.binding.consecutive_failures, 0)
        self.assertTrue(self.binding.enabled)
        self.assertIsNone(self.binding.deleted_at)

    def test_shared_false_never_marks_anything_gone(self):
        stale = self._mirror(
            "20261025000000003000",
            last_seen_at=self.now - timedelta(minutes=30),
        )
        with self._fetch(pull_page(shared=False)):
            summary = run_hall_pull(now=self.now)
        self.assertFalse(summary["complete"])
        stale.refresh_from_db()
        self.assertEqual(stale.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(stale.gone_at)
        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.hall_pull_next_at,
            self.now + timedelta(seconds=HALL_PULL_PERIOD_SECONDS),
        )

    def test_only_a_complete_pass_marks_missing_cards_gone_and_takes_them_downstream(self):
        stale = self._mirror(
            "20261025000000003100",
            last_seen_at=self.now - timedelta(minutes=11),
        )
        fresh = self._mirror(
            "20261025000000003101",
            last_seen_at=self.now - timedelta(minutes=11),
        )
        with self._fetch(
            pull_page("20261025000000003101", next_cursor="MORE"),
            PeerUnreachable("network blip"),
        ):
            first = run_hall_pull(now=self.now)
        self.assertFalse(first["complete"])
        stale.refresh_from_db()
        self.assertEqual(stale.status, PeerHallMirror.Status.LISTED)
        later = self.now + timedelta(seconds=HALL_PULL_BACKOFF_SECONDS)
        with self._fetch(pull_page()):
            second = run_hall_pull(now=later)
        self.assertTrue(second["complete"])
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(stale.gone_at)
        self.assertEqual(fresh.status, PeerHallMirror.Status.LISTED)
        self.assertTrue(PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(self.next_binding.pk),
            kind="hall.take",
            target_ref=stale.peer_order_no,
        ).exists())

    def test_claiming_and_promoted_rows_only_get_their_last_seen_touched(self):
        claiming = self._mirror(
            "20261025000000003210", status=PeerHallMirror.Status.CLAIMING,
            last_seen_at=self.now - timedelta(seconds=200),
        )
        promoted = self._mirror(
            "20261025000000003211", status=PeerHallMirror.Status.PROMOTED,
            last_seen_at=self.now - timedelta(seconds=200),
        )
        with self._fetch(
            pull_page("20261025000000003210", "20261025000000003211")
        ):
            run_hall_pull(now=self.now)
        claiming.refresh_from_db()
        promoted.refresh_from_db()
        self.assertEqual(claiming.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(promoted.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(claiming.last_seen_at, self.now)
        self.assertEqual(promoted.last_seen_at, self.now)

    def test_the_sync_switch_off_means_the_binding_is_not_pulled_at_all(self):
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_sync_enabled=False
        )
        with mock.patch.object(peer_worker, "fetch_hall_page") as fetch:
            summary = run_hall_pull(now=self.now)
        self.assertFalse(fetch.called)
        self.assertEqual(summary["bindings"], 0)

    def test_a_round_with_nothing_due_writes_nothing(self):
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_pull_next_at=self.now + timedelta(seconds=HALL_PULL_PERIOD_SECONDS)
        )
        before = PeerSyncEvent.objects.count()
        with mock.patch.object(peer_worker, "fetch_hall_page") as fetch:
            summary = run_hall_pull(now=self.now)
        self.assertFalse(fetch.called)
        self.assertEqual(PeerSyncEvent.objects.count(), before)
        self.assertEqual(summary["pages"], 0)

    def test_an_old_peer_404_does_not_block_normal_delivery(self):
        self.binding.hall_pull_requested_at = self.now
        self.binding.hall_pull_next_at = self.now
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        with self._fetch(PeerUnsupported("404")):
            run_hall_pull(now=self.now)
        # 普通投递照样跑：一条 pending 的 order.update 真的投出去并 done。
        PeerSyncEvent.objects.create(
            binding_role="downstream",
            binding_id=str(self.next_binding.pk),
            direction="out",
            kind="order.update",
            target_type="order",
            target_ref="20261025000000009901",
            payload=payload("20261025000000009901"),
            idempotency_key=new_token(),
        )
        with mock.patch.object(
            peer_worker, "push_events", return_value={}
        ) as push:
            summary = peer_worker.run_round(now=self.now)
        self.assertEqual(summary["delivered"], 1, summary)
        push.assert_called_once()
        self.assertTrue(PeerSyncEvent.objects.filter(
            binding_id=str(self.next_binding.pk),
            kind="order.update",
            target_ref="20261025000000009901",
            status=PeerSyncEvent.Status.DONE,
        ).exists())

    def test_a_card_younger_than_ten_minutes_is_never_marked_gone(self):
        young = self._mirror(
            "20261025000000003220",
            last_seen_at=self.now - timedelta(minutes=5),
        )
        with self._fetch(pull_page()):
            run_hall_pull(now=self.now)
        young.refresh_from_db()
        self.assertEqual(young.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(young.gone_at)

    def test_last_seen_refresh_is_throttled_to_about_ninety_seconds(self):
        row = self._mirror(
            "20261025000000003200",
            last_seen_at=self.now - timedelta(seconds=30),
        )
        with self._fetch(pull_page("20261025000000003200")):
            run_hall_pull(now=self.now)
        row.refresh_from_db()
        self.assertEqual(
            row.last_seen_at, self.now - timedelta(seconds=30),
            "90 秒内的重复拉取不该刷新 last_seen_at（写放大）",
        )
        later = self.now + timedelta(seconds=HALL_PULL_PERIOD_SECONDS)
        with self._fetch(pull_page("20261025000000003200")):
            run_hall_pull(now=later)
        row.refresh_from_db()
        self.assertEqual(row.last_seen_at, later)

    def test_partial_passes_do_not_advance_last_seen_beyond_the_throttle(self):
        """即使不 complete，见到就是见到：last_seen 的刷新与消失判定分开。"""
        row = self._mirror(
            "20261025000000003201",
            last_seen_at=self.now - timedelta(minutes=30),
        )
        with self._fetch(
            pull_page("20261025000000003201", next_cursor="NEXT"),
            pull_page(),
        ):
            run_hall_pull(now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.last_seen_at, self.now)   # 已见到 ⇒ 刷新
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)


class SharedHallPullRetryTests(PullFixture):
    @contextmanager
    def serve_pages(self, responses):
        requests = []
        stop = Event()
        steps = iter(responses)

        class HallPeer(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((payload["cursor"], self.headers.get("X-Peer-Hall-Request-ID")))
                page, condition = next(steps)
                if condition == "headers":
                    stop.wait(5)
                    return
                envelope = {"code": 0, "data": page}
                if condition == "rejected":
                    envelope = {"code": 1020, "msg": "合成业务拒绝", "data": None}
                body = json.dumps(envelope).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body if condition in ("complete", "rejected") else body[:len(body) // 2])
                if condition == "stall":
                    stop.wait(5)

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), HallPeer) as server:
            self.binding.peer_base_url = f"http://127.0.0.1:{server.server_port}"
            self.binding.save(update_fields=["peer_base_url"])
            thread = Thread(target=server.serve_forever, args=(0.025,), daemon=True)
            thread.start()
            try:
                yield requests
            finally:
                stop.set()
                server.shutdown()
                thread.join()

    @contextmanager
    def shared_deadline(self):
        loop = new_peer_deadline_loop()
        try:
            with enforce_total_peer_timeout(loop):
                yield
        finally:
            loop.close()

    def test_truncated_second_page_is_retried_without_replaying_first_page(self):
        first = "20261025000000003701"
        second = "20261025000000003702"
        marker = self.now - timedelta(seconds=1)
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(hall_pull_requested_at=marker)
        stale = self._mirror("20261025000000003703", last_seen_at=self.now - timedelta(minutes=15))
        responses = (
            (pull_page(first, next_cursor="CURSOR-2"), "complete"),
            (pull_page(second), "close"),
            (pull_page(second), "complete"),
        )
        with self.serve_pages(responses) as requests, self.shared_deadline():
            result = run_hall_pull(now=self.now)
            self.assertFalse(result["complete"])
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_fail_count, 0)
            self.assertEqual(self.binding.hall_pull_next_at, self.now + timedelta(seconds=5))
            self.assertEqual(self.binding.hall_pull_requested_at, marker)
            self.assertIsNone(self.binding.hall_pull_last_ok_at)
            stale.refresh_from_db()
            self.assertEqual(stale.status, PeerHallMirror.Status.LISTED)
            self.assertFalse(PeerHallMirror.objects.filter(peer_order_no=second).exists())
            self.assertEqual(run_hall_pull(now=self.now + timedelta(seconds=4))["bindings"], 0)
            self.assertTrue(run_hall_pull(now=self.now + timedelta(seconds=5))["complete"])
        self.assertEqual([cursor for cursor, _ in requests], [None, "CURSOR-2", "CURSOR-2"])
        self.assertEqual(len({request_id for _, request_id in requests}), 3)
        for ref in (first, second):
            self.assertEqual(PeerHallMirror.objects.filter(peer_order_no=ref).count(), 1)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 0)
        self.assertIsNone(self.binding.hall_pull_requested_at)
        stale.refresh_from_db()
        self.assertEqual(stale.status, PeerHallMirror.Status.GONE)

    def test_stalled_body_and_failed_retry_count_once_and_back_off(self):
        ref = "20261025000000003704"
        page = pull_page(ref)
        with self.serve_pages(((page, "stall"), (page, "close"))) as requests, self.shared_deadline():
            with self.assertLogs("apps.business.peer_client", level="WARNING") as traces:
                first = run_hall_pull(now=self.now)
            retry_at = self.now + timedelta(seconds=5)
            self.assertEqual(first["retry_at"], retry_at)
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_fail_count, 0)
            request_id = requests[0][1]
            self.assertIn(f"request_id={request_id} phase=response_body error=ReadTimeout", traces.output[0])
            length = len(json.dumps({"code": 0, "data": page}).encode())
            self.assertIn(f"expected_bytes={length} received_bytes={length // 2}", traces.output[0])
            self.assertNotIn(ref, traces.output[0])
            self.assertNotIn(self.binding.token_out, traces.output[0])
            self.assertNotIn("retry_at", run_hall_pull(now=retry_at))
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_fail_count, 1)
            self.assertEqual(self.binding.hall_pull_next_at, retry_at + timedelta(minutes=10))
            self.assertEqual(run_hall_pull(now=retry_at + timedelta(seconds=5))["bindings"], 0)
        self.assertEqual([cursor for cursor, _ in requests], [None, None])
        self.assertFalse(PeerHallMirror.objects.filter(peer_order_no=ref).exists())

    def test_stalled_headers_and_legacy_truncated_body_keep_long_backoff(self):
        cases = (("headers", self.shared_deadline()), ("close", nullcontext()))
        with self.serve_pages(tuple((pull_page(), condition) for condition, _ in cases)):
            for index, (condition, deadline) in enumerate(cases, start=1):
                with self.subTest(condition=condition), deadline:
                    PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(hall_pull_next_at=None)
                    with self.assertLogs("apps.business.peer_client", level="WARNING") as traces:
                        result = run_hall_pull(now=self.now)
                    if condition == "headers":
                        self.assertIn("phase=response_headers error=ReadTimeout", traces.output[0])
                    self.assertNotIn("retry_at", result)
                    self.binding.refresh_from_db()
                    self.assertEqual(self.binding.hall_pull_fail_count, index)
                    self.assertEqual(self.binding.hall_pull_next_at, self.now + timedelta(minutes=10))

    def test_stale_short_retry_cannot_overwrite_new_hall_request(self):
        replacement = self.now + timedelta(seconds=1)
        actual_fetch = fetch_hall_page

        def replace_marker(*args, **kwargs):
            PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
                hall_pull_requested_at=replacement, hall_pull_next_at=replacement,
            )
            return actual_fetch(*args, **kwargs)

        with self.serve_pages(((pull_page(), "close"),)), self.shared_deadline():
            with mock.patch.object(peer_worker, "fetch_hall_page", side_effect=replace_marker):
                result = run_hall_pull(now=self.now)
        self.assertNotIn("retry_at", result)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_next_at, replacement)
        self.assertEqual(self.binding.hall_pull_requested_at, replacement)
        self.assertEqual(self.binding.hall_pull_fail_count, 0)

    def test_business_rejection_settles_short_retry_without_connectivity_failure(self):
        ref = "20261025000000003705"
        page = pull_page(ref)
        with self.serve_pages(((page, "close"), (page, "rejected"))) as requests, self.shared_deadline():
            retry_at = run_hall_pull(now=self.now)["retry_at"]
            self.assertNotIn("retry_at", run_hall_pull(now=retry_at))
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_fail_count, 0)
            self.assertEqual(self.binding.hall_pull_next_at, retry_at + timedelta(minutes=10))
            self.assertEqual(run_hall_pull(now=retry_at + timedelta(seconds=10))["bindings"], 0)
        self.assertEqual(len(requests), 2)
        self.assertFalse(PeerHallMirror.objects.filter(peer_order_no=ref).exists())

    def test_retry_cannot_complete_or_remove_cards_after_an_earlier_page_failed(self):
        first = "20261025000000003706"
        second = "20261025000000003707"
        marker = self.now - timedelta(seconds=1)
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(hall_pull_requested_at=marker)
        stale = self._mirror(first, last_seen_at=self.now - timedelta(minutes=15))
        bad_page = pull_page(first, next_cursor="SECOND-PAGE")
        bad_page["rows"][0]["payload"]["hall_price"] = "invalid"
        responses = (
            (bad_page, "complete"),
            (pull_page(second), "close"),
            (pull_page(second), "complete"),
            (pull_page(first, next_cursor="SECOND-PAGE"), "complete"),
            (pull_page(second), "complete"),
        )
        with self.serve_pages(responses) as requests, self.shared_deadline():
            retry_at = run_hall_pull(now=self.now)["retry_at"]
            self.assertFalse(run_hall_pull(now=retry_at)["complete"])
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_requested_at, marker)
            self.assertIsNone(self.binding.hall_pull_last_ok_at)
            stale.refresh_from_db()
            self.assertEqual(stale.status, PeerHallMirror.Status.LISTED)
            self.assertTrue(run_hall_pull(now=retry_at + timedelta(seconds=10))["complete"])
        self.assertEqual(
            [cursor for cursor, _ in requests], [None, "SECOND-PAGE", "SECOND-PAGE", None, "SECOND-PAGE"],
        )

    def test_failed_short_retry_cannot_count_after_binding_is_disabled(self):
        actual_fetch = fetch_hall_page

        def disable(*args, **kwargs):
            PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(hall_sync_enabled=False)
            return actual_fetch(*args, **kwargs)

        with self.serve_pages(((pull_page(), "close"),) * 2), self.shared_deadline():
            retry_at = run_hall_pull(now=self.now)["retry_at"]
            with mock.patch.object(peer_worker, "fetch_hall_page", side_effect=disable):
                self.assertNotIn("retry_at", run_hall_pull(now=retry_at))
        self.binding.refresh_from_db()
        self.assertFalse(self.binding.hall_sync_enabled)
        self.assertEqual(self.binding.hall_pull_next_at, retry_at)
        self.assertEqual(self.binding.hall_pull_fail_count, 0)


class RetryWriteRaceTests(TransactionTestCase):
    def setUp(self):
        self.tenant = get_tenant_model()(schema_name="peer_hall_retry_race")
        self.tenant.save(verbosity=0)
        self.addCleanup(self.tenant.delete, force_drop=True)
        peer_worker.reset_hall_pull_state()
        self.addCleanup(peer_worker.reset_hall_pull_state)
        self.now = timezone.now()
        self.reopened_at = self.now + timedelta(seconds=6)
        with schema_context(self.tenant.schema_name):
            owner = get_user_model().objects.create_user(username="syn-hall-retry-race")
            self.binding = PeerUpstreamBinding.objects.create(
                peer_name="合成上家", peer_base_url=UP_BASE, peer_account="syn-race",
                token_in_hash=hash_token(new_token()), token_out=new_token(),
                local_base_url=DOWN_BASE, bound_by=owner, enabled=True, hall_sync_enabled=True,
                hall_pull_requested_at=self.now,
            )

    def try_reopen_in_other_connection(self):
        def reopen():
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    row = PeerUpstreamBinding.objects.select_for_update(nowait=True).get(pk=self.binding.pk)
                    row.hall_pull_requested_at = row.hall_pull_next_at = self.reopened_at
                    row.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
                return "reopened"
            except OperationalError as exc:
                if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                    raise
                return "locked"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(reopen).result(timeout=5)

    def test_retry_failure_serializes_threshold_side_effects_with_a_new_request(self):
        locking = []
        ref = "20261025000000003711"
        with schema_context(self.tenant.schema_name):
            PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(hall_pull_fail_count=29)
            stale = PeerHallMirror.objects.create(
                peer_upstream_binding=self.binding, peer_base_url=UP_BASE, peer_order_no=ref,
                game=Game.objects.create(name=GAME_NAME), remote_hall_price=50,
                payload=payload(ref), status=PeerHallMirror.Status.LISTED,
                first_seen_at=self.now - timedelta(minutes=20),
            )
            actual_unlist = peer_worker._maybe_pull_dead

            def check_threshold(*args, **kwargs):
                locking.append(self.try_reopen_in_other_connection())
                return actual_unlist(*args, **kwargs)

            with mock.patch.object(peer_worker, "fetch_hall_page", side_effect=PeerHallBodyIncomplete):
                retry_at = run_hall_pull(now=self.now)["retry_at"]
                with mock.patch.object(peer_worker, "_maybe_pull_dead", side_effect=check_threshold):
                    run_hall_pull(now=retry_at)
            self.assertEqual(locking, ["locked"])
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_fail_count, 30)
            self.assertEqual(self.binding.hall_pull_next_at, retry_at + timedelta(hours=6))
            stale.refresh_from_db()
            self.assertEqual(stale.status, PeerHallMirror.Status.GONE)
            self.assertEqual(self.try_reopen_in_other_connection(), "reopened")
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_next_at, self.reopened_at)

    def test_retry_landing_and_completion_each_serialize_with_a_new_request(self):
        locking = []
        ref = "20261025000000003712"
        with schema_context(self.tenant.schema_name):
            Game.objects.create(name=GAME_NAME)
            actual_apply = peer_worker.apply_hall_pull_rows
            actual_mark_missing = peer_worker.mark_missing_hall_rows_gone

            def attempt_reopen_before_apply(*args, **kwargs):
                locking.append(self.try_reopen_in_other_connection())
                return actual_apply(*args, **kwargs)

            def attempt_reopen_before_completion(*args, **kwargs):
                locking.append(self.try_reopen_in_other_connection())
                return actual_mark_missing(*args, **kwargs)

            with mock.patch.object(
                peer_worker, "fetch_hall_page", side_effect=[PeerHallBodyIncomplete(), pull_page(ref)],
            ):
                retry_at = run_hall_pull(now=self.now)["retry_at"]
                with (
                    mock.patch.object(peer_worker, "apply_hall_pull_rows", side_effect=attempt_reopen_before_apply),
                    mock.patch.object(peer_worker, "mark_missing_hall_rows_gone", side_effect=attempt_reopen_before_completion),
                ):
                    result = run_hall_pull(now=retry_at)
            self.assertEqual(locking, ["locked", "locked"])
            self.assertTrue(result["complete"])
            self.assertEqual(PeerHallMirror.objects.filter(peer_order_no=ref).count(), 1)
            self.binding.refresh_from_db()
            self.assertIsNone(self.binding.hall_pull_requested_at)
            self.assertEqual(self.try_reopen_in_other_connection(), "reopened")
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.hall_pull_requested_at, self.reopened_at)

    def test_retry_landing_and_completion_yield_to_inbound_hall_take(self):
        phases = ("apply_hall_pull_rows", "mark_missing_hall_rows_gone")
        with schema_context(self.tenant.schema_name):
            game = Game.objects.create(name=GAME_NAME)
            for index, phase in enumerate(phases):
                with self.subTest(phase=phase):
                    self.check_retry_yields_to_take(phase, game, f"2026102500000000372{index}")

    def check_retry_yields_to_take(self, phase, game, ref):
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_pull_next_at=None, hall_pull_requested_at=self.now, hall_pull_last_ok_at=None,
        )
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=self.binding, peer_base_url=UP_BASE, peer_order_no=ref,
            game=game, remote_hall_price=50, payload=payload(ref),
            status=PeerHallMirror.Status.LISTED, first_seen_at=self.now - timedelta(minutes=20),
        )
        pull_has_binding_lock = Event()
        push_has_card_lock = Event()
        dispatch = getattr(peer_worker, phase)
        record_health = peer_views._record_inbound_health

        def write_pull_under_guard(*args, **kwargs):
            pull_has_binding_lock.set()
            self.assertTrue(push_has_card_lock.wait(5))
            return dispatch(*args, **kwargs)

        def record_health_after_card_update(binding, outcome):
            self.assertEqual(PeerHallMirror.objects.get(pk=card.pk).status, PeerHallMirror.Status.GONE)
            push_has_card_lock.set()
            return record_health(binding, outcome)

        def receive_in_another_connection():
            try:
                self.assertTrue(pull_has_binding_lock.wait(5))
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    binding = PeerUpstreamBinding.objects.get(pk=self.binding.pk)
                    return peer_views.receive_events(binding, [
                        {"kind": "hall.take", "target_ref": ref, "payload": {}},
                    ])["applied"]
            finally:
                connections.close_all()

        page = pull_page(ref) if phase == "apply_hall_pull_rows" else pull_page()
        with mock.patch.object(
            peer_worker, "fetch_hall_page", side_effect=[PeerHallBodyIncomplete(), page],
        ):
            retry_at = run_hall_pull(now=self.now)["retry_at"]
            with (
                ThreadPoolExecutor(max_workers=1) as executor,
                mock.patch.object(peer_worker, phase, side_effect=write_pull_under_guard),
                mock.patch.object(peer_views, "_record_inbound_health", side_effect=record_health_after_card_update),
            ):
                incoming = executor.submit(receive_in_another_connection)
                retry = run_hall_pull(now=retry_at)
                self.assertEqual(incoming.result(timeout=5), 1)
        self.assertFalse(retry["complete"])
        self.assertEqual(retry["errors"], 1)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_next_at, retry_at)
        self.assertEqual(self.binding.hall_pull_requested_at, self.now)
        self.assertIsNone(self.binding.hall_pull_last_ok_at)
        self.assertEqual(self.binding.hall_pull_fail_count, 0)


class HallPullHealthTests(PullFixture):
    """⭐ B2 / M-7+L-11（2026-10-25 审计批）：拉取的健康两列与「连败判死」。

    判据字面值全部钉死（E-0024）：阈值 **30**（≈5 小时）、下架文案
    **"上家大厅连续拉取失败已自动下架（pull_dead）；上家恢复后会自动重新上架"**。
    """

    #: 字面值（⛔ 不 import 实现常量当期望）。
    DEAD_THRESHOLD = 30
    DEAD_NOTE_TEXT = (
        "上家大厅连续拉取失败已自动下架（pull_dead）；上家恢复后会自动重新上架"
    )

    def _fetch(self, *pages):
        """与 `RunHallPullTests._fetch` 同形（那一个定义在子类里，本类自己带一份）。"""
        return mock.patch.object(
            peer_worker, "fetch_hall_page", side_effect=list(pages)
        )

    def _fail_round(self, *, now, seq):
        """跑一轮注定失联的拉取（并把下一轮重新变成「到期」）。"""
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_pull_next_at=None
        )
        with self._fetch(PeerUnreachable(f"down-{seq}")):
            summary = run_hall_pull(now=now)
        self.assertEqual(summary["errors"], 1)
        return summary

    def test_a_complete_pass_stamps_last_ok_and_resets_the_fail_count(self):
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_pull_fail_count=7, hall_pull_last_ok_at=None,
        )
        with self._fetch(pull_page("20261025000000004100")):
            summary = run_hall_pull(now=self.now)
        self.assertTrue(summary["complete"])
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 0)
        self.assertEqual(self.binding.hall_pull_last_ok_at, self.now)

    def test_a_network_failure_bumps_the_count_and_never_stamps(self):
        self._fail_round(now=self.now, seq=1)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 1)
        self.assertIsNone(self.binding.hall_pull_last_ok_at)
        # 投递的健康列一个字不动（那是另一本账）。
        self.assertEqual(self.binding.consecutive_failures, 0)

    def test_an_unsupported_old_peer_never_counts_toward_dead(self):
        """老对端 404 = 活着、只是没端点 ⇒ ⛔ 不进判死计数。"""
        with self._fetch(PeerUnsupported("no endpoint")):
            run_hall_pull(now=self.now)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 0)

    def test_twenty_nine_failures_do_not_mark_anything_gone(self):
        dead = self._mirror(
            "20261025000000004200", last_seen_at=self.now - timedelta(minutes=30)
        )
        for i in range(29):
            self._fail_round(now=self.now + timedelta(minutes=10 * i), seq=i)
        dead.refresh_from_db()
        self.assertEqual(dead.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(dead.gone_at)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 29)
        self.assertIsNone(self.binding.deleted_at)   # 绑定保留

    def test_thirty_consecutive_failures_mark_listed_rows_gone_and_keep_the_binding(self):
        dead = self._mirror(
            "20261025000000004201", last_seen_at=self.now - timedelta(minutes=30)
        )
        fresh = self._mirror(
            "20261025000000004202", last_seen_at=self.now - timedelta(minutes=30)
        )
        for i in range(30):
            self._fail_round(now=self.now + timedelta(minutes=10 * i), seq=i)
        dead.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(dead.status, PeerHallMirror.Status.GONE)
        self.assertEqual(fresh.status, PeerHallMirror.Status.GONE)
        self.assertEqual(dead.last_error, self.DEAD_NOTE_TEXT)
        self.assertIsNotNone(dead.gone_at)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 30)
        # ⛔ 判死只下架卡：绑定不解（停绑是双向 kill，恢复要重走握手）。
        self.assertIsNone(self.binding.deleted_at)
        self.assertTrue(self.binding.enabled)
        # 下家手里同款卡照旧补发 take（复用 unlist_binding_mirrors 的转发）。
        self.assertTrue(
            PeerSyncEvent.objects.filter(
                binding_role="downstream",
                binding_id=str(self.next_binding.pk),
                direction="out",
                kind="hall.take",
                target_ref="20261025000000004201",
                status=PeerSyncEvent.Status.PENDING,
            ).exists()
        )

    def test_judged_dead_pushes_the_next_pull_out_to_six_hours(self):
        """⭐ L-3（2026-10-26）：判死收口当轮把 `hall_pull_next_at` 推远（6 小时）。

        不推远的话：拉取判 gone（补发 take）→ 对端推送又把卡挂回来 → 10 分钟后拉取再判 gone
        ⇒ **每 10 分钟拍一次**。判死 = 拉取这条路已坏 ⇒ 以推送为准，拉取先停手。
        """
        self._mirror(
            "20261025000000004209", last_seen_at=self.now - timedelta(minutes=30)
        )
        last_round_at = self.now + timedelta(minutes=10 * 29)
        for i in range(30):
            self._fail_round(now=self.now + timedelta(minutes=10 * i), seq=i)

        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.hall_pull_next_at, last_round_at + timedelta(hours=6)
        )
        # 判死后 10 分钟（旧节奏）**不再**开拉 ⇒ 不会与推送来的「复活」拍动。
        summary = run_hall_pull(now=last_round_at + timedelta(minutes=10))
        self.assertEqual(summary["errors"], 0, summary)
        self.assertEqual(summary["bindings"], 0, summary)

    def test_a_pull_dead_card_revives_when_the_upstream_recovers(self):
        """判死 ⇒ 上家恢复 ⇒ 完整 pass 重见同一张卡 ⇒ 复活 + 计数清零 + 盖章。"""
        dead = self._mirror(
            "20261025000000004203", last_seen_at=self.now - timedelta(minutes=30)
        )
        for i in range(30):
            self._fail_round(now=self.now + timedelta(minutes=10 * i), seq=i)
        dead.refresh_from_db()
        self.assertEqual(dead.status, PeerHallMirror.Status.GONE)

        recovery_at = self.now + timedelta(hours=6)
        PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_pull_next_at=None
        )
        peer_worker.reset_hall_pull_state()
        with self._fetch(pull_page("20261025000000004203")):
            summary = run_hall_pull(now=recovery_at)
        self.assertTrue(summary["complete"])
        dead.refresh_from_db()
        self.assertEqual(dead.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(dead.gone_at)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_fail_count, 0)
        self.assertEqual(self.binding.hall_pull_last_ok_at, recovery_at)


class PullRowShapeGateTests(PullFixture):
    """⭐ A4 / M-5（2026-10-25 审计批）：拉取行的 `hall_price` 形状 / 值域闸。"""

    def test_a_row_with_a_malformed_price_counts_as_failure_and_the_rest_lands(self):
        """畸形价（dict）⇒ 该行计 `failures` 跳过，**其余行照常落地**、不抛异常。"""
        rows = [
            {"target_ref": "PULL-BAD-1", "payload": payload("PULL-BAD-1", price={"bad": 1})},
            {"target_ref": "PULL-OK-1", "payload": payload("PULL-OK-1", price="66.00")},
        ]
        outcome = apply_hall_pull_rows(
            self.binding, rows, now=self.now, round_started_at=self.now
        )
        self.assertEqual(outcome["applied"], 1, outcome)
        self.assertEqual(outcome["failures"], 1, outcome)
        self.assertIn("PULL-OK-1", outcome["seen"])
        self.assertNotIn("PULL-BAD-1", outcome["seen"])
        self.assertTrue(
            PeerHallMirror.objects.filter(peer_order_no="PULL-OK-1").exists()
        )
        self.assertFalse(
            PeerHallMirror.objects.filter(peer_order_no="PULL-BAD-1").exists()
        )

    def test_a_negative_price_row_counts_as_failure(self):
        """⭐ A7 同闸：负价行计 `failures`，不进镜像行。"""
        rows = [
            {"target_ref": "PULL-NEG-1", "payload": payload("PULL-NEG-1", price="-99999")},
        ]
        outcome = apply_hall_pull_rows(
            self.binding, rows, now=self.now, round_started_at=self.now
        )
        self.assertEqual(outcome["applied"], 0, outcome)
        self.assertEqual(outcome["failures"], 1, outcome)
        self.assertEqual(PeerHallMirror.objects.count(), 0)


class PullResurrectionGateTests(PullFixture):
    def test_a_take_that_landed_after_the_round_started_never_resurrects(self):
        row = self._mirror(
            "20261025000000004000",
            status=PeerHallMirror.Status.GONE,
            gone_at=self.now + timedelta(seconds=1),
        )
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("20261025000000004000"),
        ):
            run_hall_pull(now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.GONE)
        self.assertEqual(row.gone_at, self.now + timedelta(seconds=1))

    def test_a_gone_row_seen_again_resurrects_and_forwards_once(self):
        row = self._mirror(
            "20261025000000004001",
            status=PeerHallMirror.Status.GONE,
            gone_at=self.now - timedelta(minutes=1),
        )
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("20261025000000004001"),
        ):
            run_hall_pull(now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(row.gone_at)
        self.assertTrue(PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(self.next_binding.pk),
            kind=HALL_THROW_KIND,
            target_ref=row.peer_order_no,
        ).exists())

    def test_a_legacy_gone_row_without_gone_at_can_resurrect(self):
        row = self._mirror(
            "20261025000000004002",
            status=PeerHallMirror.Status.GONE,
            gone_at=None,
        )
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("20261025000000004002"),
        ):
            run_hall_pull(now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)


class PullForwardingAndHopTests(PullFixture):
    def _throw_events(self, ref):
        return PeerSyncEvent.objects.filter(
            kind=HALL_THROW_KIND, target_ref=ref,
            binding_role="downstream", binding_id=str(self.next_binding.pk),
        )

    def test_new_cards_are_forwarded_but_refreshes_are_not(self):
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("20261025000000005000"),
        ):
            run_hall_pull(now=self.now)
        self.assertEqual(self._throw_events("20261025000000005000").count(), 1)
        # 把这条待投事件标成已投，再拉一轮同卡：refreshed 不转发。
        self._throw_events("20261025000000005000").update(
            status=PeerSyncEvent.Status.DONE, reason=""
        )
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("20261025000000005000"),
        ):
            run_hall_pull(now=self.now + timedelta(seconds=300))
        self.assertEqual(self._throw_events("20261025000000005000").count(), 1)

    def test_push_path_still_forwards_a_refresh(self):
        """推送侧行为逐字不变：刷新也转发（⛔ 被拉取侧的新口径带偏）。"""
        row = self._mirror("20261025000000005001")
        with mock.patch(
            "apps.business.peer_dispatch.enqueue_hall_mirror_event"
        ) as enqueue:
            from apps.business.peer_inbound import _apply_hall_event

            _apply_hall_event(
                kind=HALL_THROW_KIND, payload=payload("20261025000000005001"),
                binding=self.binding, target_ref=row.peer_order_no,
                note_skip=lambda *a: None, now=self.now,
            )
        enqueue.assert_called_once()

    def test_hop_is_written_as_the_max_of_existing_and_incoming(self):
        row = self._mirror("20261025000000005002", hop=4)
        mirror_payload = mirror_hall_payload(row)   # 行 hop 4 ⇒ 载荷 hop 5
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value={
                "shared": True, "count": 1, "next_cursor": None, "complete": True,
                "rows": [{"target_ref": row.peer_order_no,
                          "payload": mirror_payload}],
            },
        ):
            run_hall_pull(now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.hop, 5)
        # 原生载荷（无 hop）不会把已有 hop 降成 0。
        native = self._order("20261025000000005003", source_order_no="SRC-5003")
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("SRC-5003"),
        ):
            run_hall_pull(now=self.now + timedelta(seconds=300))
        self.assertFalse(PeerHallMirror.objects.filter(
            peer_order_no="SRC-5003", hop__gt=0
        ).exists())

    def test_hop_over_the_limit_creates_nothing_and_records_a_ledger(self):
        over = payload("20261025000000005004")
        over["hop"] = PEER_HALL_HOP_LIMIT + 1
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value={
                "shared": True, "count": 1, "next_cursor": None, "complete": True,
                "rows": [{"target_ref": over["order_no"], "payload": over}],
            },
        ):
            summary = run_hall_pull(now=self.now)
        self.assertFalse(PeerHallMirror.objects.filter(
            peer_order_no=over["order_no"]
        ).exists())
        self.assertTrue(PeerSyncEvent.objects.filter(
            kind="peer.hall_hop_limit", target_ref=over["order_no"]
        ).exists())
        self.assertTrue(summary["complete"], "跳数丢弃是处理完，不是本页失败")

    def test_pull_then_push_on_the_same_card_keeps_one_row(self):
        with mock.patch.object(
            peer_worker, "fetch_hall_page",
            return_value=pull_page("20261025000000005005"),
        ):
            run_hall_pull(now=self.now)
        from apps.business.peer_inbound import _apply_hall_event

        _apply_hall_event(
            kind=HALL_THROW_KIND, payload=payload("20261025000000005005"),
            binding=self.binding, target_ref="20261025000000005005",
            note_skip=lambda *a: None, now=self.now,
        )
        self.assertEqual(PeerHallMirror.objects.filter(
            peer_order_no="20261025000000005005"
        ).count(), 1)

    def test_switching_off_mid_pass_stops_before_the_next_page(self):
        from apps.business.models import PeerUpstreamBinding

        def _fetch(*args, **kwargs):
            PeerUpstreamBinding.objects.filter(pk=self.binding.pk).update(
                hall_sync_enabled=False
            )
            return pull_page("20261025000000005006", next_cursor="C2")

        with mock.patch.object(peer_worker, "fetch_hall_page", side_effect=_fetch) as fetch:
            summary = run_hall_pull(now=self.now)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(summary["complete"])
        self.assertFalse(PeerHallMirror.objects.filter(
            peer_order_no="20261025000000005007"
        ).exists())

    def test_aba_conditional_clear_never_clears_a_newer_marker(self):
        """读到落写之间开关又翻了一次：只清「本次读到的那个值」。"""
        old = self.now
        self.binding.hall_pull_requested_at = old
        self.binding.hall_pull_next_at = old
        self.binding.save(update_fields=["hall_pull_requested_at", "hall_pull_next_at"])
        newer = old + timedelta(seconds=1)
        written = peer_worker.clear_hall_pull_marker(self.binding, expected=old, now=self.now)
        self.assertTrue(written)
        self.binding.refresh_from_db()
        self.assertIsNone(self.binding.hall_pull_requested_at)
        # 现在模拟「标记已被别的进程换成更新的值」后再清旧值 ⇒ 不许清。
        self.binding.hall_pull_requested_at = newer
        self.binding.save(update_fields=["hall_pull_requested_at"])
        written = peer_worker.clear_hall_pull_marker(self.binding, expected=old, now=self.now)
        self.assertFalse(written)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.hall_pull_requested_at, newer)


class PullShadowOrderTests(PullFixture):
    """⭐ 2026-10-26：拉取支落地 `listed` 卡时也要建隐藏单（与推送支同一个落点）。

    判据：新建卡 ⇒ 一张 `WAITING_GRAB` 隐藏单 + 一条 `member.launch`；下一轮刷新同一张卡
    ⇒ **不建第二张**（隐藏单与卡 1:1）。
    """

    def _jidanpu_binding(self):
        from apps.business.models import JidanpuMemberBinding
        from tests.p5_auth_helpers import grant_games

        user = get_user_model().objects.create_user(
            username="syn-pull-shadow-member", password="pw-syn"
        )
        grant_games(user, self.game)
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url="https://syn-pull-shadow-shop.test.invalid",
            account="syn-pull-shadow-member",
            password="pw-syn",
            enabled=True,
        )

    def test_pull_creates_one_hidden_order_and_launches_it_once(self):
        from apps.business.jidanpu_member import KIND_LAUNCH
        from apps.business.models import JidanpuSyncEvent

        binding = self._jidanpu_binding()
        card_no = "20261025000000006000"
        with mock.patch.object(
            peer_worker, "fetch_hall_page", return_value=pull_page(card_no)
        ):
            run_hall_pull(now=self.now)
        row = PeerHallMirror.objects.get(peer_order_no=card_no)
        order = Order.objects.get(hall_card=row)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(order.source_order_no, card_no)
        self.assertEqual(order.hall_price, Decimal("50.00"))
        launches = JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH, target_ref=order.order_no,
            payload__member_binding_id=binding.pk,
        )
        self.assertEqual(launches.count(), 1)

        # 第二轮（同一张卡刷新）⇒ 不建第二张、不重复排队。
        with mock.patch.object(
            peer_worker, "fetch_hall_page", return_value=pull_page(card_no)
        ):
            run_hall_pull(now=self.now + timedelta(seconds=300))
        self.assertEqual(Order.objects.filter(hall_card=row).count(), 1)
        self.assertEqual(launches.count(), 1)
