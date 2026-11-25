"""P4 二次转发（多跳）：**三层真租户**的大厅镜像整链（A → B → C）。

权威口径 = `docs/specs/2026-10-25-peer-hall-share-design.md` §5.3 / §5.3.1
（多跳机制；本文件按它写，按实现钉）；现役 v1 口径 = `2026-10-25-peer-hall-mirror.md`
（只有一跳）。落地映射见 `peer_hall.py` / `peer_inbound.py` 的 P4 段落。

覆盖（每条都能被单独弄红）：

① **逐跳相乘**：100 →（B 90%）→ 90 →（C 80%）→ 72，**读口**（`GET /api/business/hall/`
   的 `hall_price`）与**落单**（B / C 各自那张正式单的 `hall_price`）都验；
② **整链抢单**：C 打手点接单 ⇒ B 收到 ⇒ 逐跳转发给 A ⇒ A 判给 B 的绑定账号 ⇒
   B 转正并按**本层展示价**交给 C 的账号（排 `order.assign`）⇒ C 收到并转正 72；
③ **链身份**：C 收到的 `order.assign` 载荷带**源头 A 的单号**（`origin_order_no`），
   C 靠它认回自己大厅里那张卡（卡的号沿链不变 = A 的单号）；
④ **拒绝沿链回传**：A 拒 ⇒ B 收回 `listed` 并清四列 ⇒ 转给 C ⇒ C 也收回 `listed`；
⑤ **多跳行 5 分钟回收**：中间层卡住的 `claiming` 行同样被放回大厅，并清
   `claim_downstream` / `claim_id`；
⑥ **单跳回归**：A→B 一跳时载荷里**没有** `origin_order_no`（行为逐字不变）、
   B 的镜像行永远不会写多跳那两列；
⑦ ⭐ **B3下游撤卡**：B 在绑定页**真打**「关开关 / 解绑」⇒ 本地卡 gone
   **＋** 补发的 `hall.take` 真的投到 C ⇒ C 的卡也 gone、C 的大厅读口里不再出现。

⚠️ 夹具照 `test_peer_p4_chain.py::ChainFixture` 的手法：**三个真租户**（A = `TenantTestCase`
自带的那个 / B / C 由 `provision_tenant` 建），**真握手、真令牌、真域名**；投递真的走
HTTP（桩只打在 `apps.business.peer_worker.push_events` 上，用进程内 `Client` 打对端的
接收端点——与 worker 同一条投递链）。夹具值全部合成值。
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone as django_timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_client import PeerRejected
from apps.business.peer_hall import (
    RECLAIMED_NOTE,
    claim_peer_mirror,
    reclaim_stuck_claims,
)
from apps.business.peer_worker import deliver_one
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

# ── 三个租户的域名 / schema（⚠️ 与其它测试类**刻意不同**：`Domain.domain` 有唯一约束）──
HALL_DOMAIN_B = "peer-hall-chain-b.test.local"
HALL_DOMAIN_C = "peer-hall-chain-c.test.local"
HALL_SCHEMA_B = "peer_hall_chain_b"
HALL_SCHEMA_C = "peer_hall_chain_c"

#: 同步端点 / 页面端点 / 大厅端点（与 `apps/business/urls.py` 的字面量一致）。
CHAIN_BIND_URL = "/api/business/peer-sync/bind/"
CHAIN_INBOUND_URL = "/api/business/peer-sync/inbound/events/"
CHAIN_PAGE_URL = "/api/business/peer-bindings/"
CHAIN_ORDERS_URL = "/api/business/orders"
CHAIN_HALL_URL = "/api/business/hall/"

#: 页面绑定端点的节点门（裁决 R22）；⛔ 逐字写死、不从视图 import（E-0024 的自指断言）。
CHAIN_PAGE_NODE = "system.bind_upstream_system"
#: `/throw-to-hall/` 带价要过的两道理：类级门（起**独占** `order.send_to_grab_hall`）
#: + 带价那道 `order.submit_payout_price`。
CHAIN_THROW_NODES = (
    "order.set_offer_price",
    "order.send_to_grab_hall",
    "order.submit_payout_price",
)
#: 大厅读口的节点门（`ORDER_PERMISSIONS["hall"]`）。
CHAIN_HALL_NODE = "grab_hall.take_order"

#: 两端按**名字**对齐的游戏（裁决 R21：⛔ 不传对端 id）。
CHAIN_GAME = "大厅多跳夹具游戏"

#: A 那张单的本地单号（合成值，20 位数字——`/throw-to-hall/` 走 router 的详情路由，
#: `lookup_value_regex = r"\d{20}"`）。⚠️ 它同时是**沿链不变的卡号**。
CHAIN_A_ORDER_NO = "20260202000000000901"

#: 逐跳价格（业务方口径：100 → B 90% → 90 → C 80% → 72）。
A_PRICE = "100.00"
B_PERCENT = 90
C_PERCENT = 80
B_PRICE = Decimal("90.00")
C_PRICE = Decimal("72.00")


@override_settings(PEER_SYNC_LOCAL_BASE_URL="")
class HallChainFixture(TenantTestCase):
    """A（上家）→ B（中间层）→ C（最下家）三个真租户 + 一条真握手链。

    ## 为什么 schema 一律在 `setUpClass` 里建

    用例内建 schema 会与外层事务相冲（Django 的 schema DDL 不能在 `TestCase` 的事务里
    回滚）——照 `test_peer_p4_chain.py::ChainFixture` 的先例。

    ## 连接钉法（⛔ 别删）

    跨域名的请求会把连接留在那个租户的 schema 上（`TenantMainMiddleware` 只在请求进来时
    `set_tenant`、**不在响应结束还原**）⇒ 每一跳都用 `try/finally` 把连接钉回来，
    `setUp` 第一行也显式 `set_tenant(self.tenant)`（挡住上一条用例的残留）。

    ## 绑定怎么来的：两条**真握手**（⛔ 不是手写两列令牌）

    B 去绑 A、C 去绑 B——每条都驱动下家页面端点（`POST /peer-bindings/`），它的
    `bind_upstream` 在**传输层**被打桩成一次进程内的真请求 ⇒ 两侧的绑定行与两套令牌都按
    裁决 R15 的真实方向落库，下面 `deliver_peer_events` 的每一次投递才真的验得过。

    ## 接收开关 / 百分比

    握手只建绑定行；**开关（默认关）与百分比（默认 100）是管理员在绑定页上设的**。
    这里直接写列（那个 PUT 端点自己已有用例：`tests/test_peer_binding_api.py`），
    避免把「大厅链路」的用例绑在另一个端点的实现细节上。
    """

    extra_test_tenants = (
        {"schema_name": HALL_SCHEMA_B, "name": "大厅多跳租户 B", "domain": HALL_DOMAIN_B},
        {"schema_name": HALL_SCHEMA_C, "name": "大厅多跳租户 C", "domain": HALL_DOMAIN_C},
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b, _ = provision_tenant(
            schema_name=HALL_SCHEMA_B, name="大厅多跳租户 B", domain=HALL_DOMAIN_B
        )
        cls.c, _ = provision_tenant(
            schema_name=HALL_SCHEMA_C, name="大厅多跳租户 C", domain=HALL_DOMAIN_C
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据；不变量（默认 Group / RBAC
        # 目录）不能依赖 schema 恰好是本次运行刚建的（`--keepdb` 会复用）⇒ 显式再灌一次。
        for tenant in (cls.b, cls.c):
            seed_tenant(tenant)

    def setUp(self):
        # ⚠️ 握手失败限流桶按 schema 记在 cache 里、**跨测试类不清**
        # ⇒ 不清的话本类那两条真握手会被别处灌满的桶拦掉（看起来像握手坏了）。
        cache.clear()
        connection.set_tenant(self.tenant)          # ⚠️ 第一行，别删
        self.a = self.tenant
        # A 的域名由 `TenantTestCase` 自己建（`tenant.test.com`），从它那一行取，⛔ 不抄字面量。
        self._domains = {
            self.a.schema_name: self.domain.domain,
            HALL_SCHEMA_B: HALL_DOMAIN_B,
            HALL_SCHEMA_C: HALL_DOMAIN_C,
        }
        # 被自己下家绑的那个账号：A 里给 B 用、B 里给 C 用（要能被**机器接手**）。
        self.a_bound_account, a_password = self._make_taker_account(
            self.a, "chain-hall-account-a", "pw-chain-hall-a"
        )
        self.b_bound_account, b_password = self._make_taker_account(
            self.b, "chain-hall-account-b", "pw-chain-hall-b"
        )
        # C 那边**本地点单的打手**（链尾：真人在 C 的大厅里点接单）。
        self.c_taker, _ = self._make_taker_account(
            self.c, "chain-hall-taker-c", "pw-chain-hall-taker-c"
        )
        # 两条真握手：B→A、C→B。
        self._handshake(
            upstream=self.a, downstream=self.b, upstream_account=self.a_bound_account,
            upstream_password=a_password, kefu="chain-hall-kefu-b",
        )
        self._handshake(
            upstream=self.b, downstream=self.c, upstream_account=self.b_bound_account,
            upstream_password=b_password, kefu="chain-hall-kefu-c",
        )
        # 接收开关（默认关）+ 逐跳百分比：B 收 A 90%、C 收 B 80%。
        self._configure_upstream(self.b, percent=B_PERCENT)
        self._configure_upstream(self.c, percent=C_PERCENT)

    # ── 夹具内部 ───────────────────────────────────────────────────────
    def _domain_of(self, ctx) -> str:
        return self._domains[ctx.schema_name]

    def _make_taker_account(self, ctx, username, password):
        """在 `ctx` 里建一个**可被指派 / 可抢单**的账号 + 一款游戏（合成值）。回 `(user, pw)`。"""
        connection.set_tenant(ctx)
        try:
            game, _ = Game.objects.get_or_create(name=CHAIN_GAME)
            user = get_user_model().objects.create_user(
                username=username, password=password
            )
            # 机器接手（`claim_order(enforce_limits=False)`）仍要过状态机的节点门
            # （`WAITING_GRAB → IN_PROGRESS` 那条边带 `_TAKE_ORDER`）⇒ 必须持这个节点。
            group, _ = Group.objects.get_or_create(name="代练员")
            user.groups.add(group)
            grant_nodes(user, CHAIN_HALL_NODE)
            # D-0252：`assign_order` / 抢单闸都判「目标打手游戏范围」（无档案 = 无游戏 = 拒）。
            grant_games(user, game)
        finally:
            connection.set_tenant(self.tenant)
        return user, password

    def _handshake(self, *, upstream, downstream, upstream_account, upstream_password, kefu):
        """驱动下家的页面端点跑一次**真握手**（照 `_real_handshake` 的手法）。"""
        upstream_domain = self._domain_of(upstream)
        downstream_domain = self._domain_of(downstream)
        # ⭐ 审计批 C2（Z22）：握手声明的本方地址必须 https（同 p4 链那条的理由：
        # TenantTestCase 上类级 override 是 no-op ⇒ 用上下文管理器）。
        local_override = override_settings(
            PEER_SYNC_LOCAL_BASE_URL=f"https://{downstream_domain}"
        )

        def _fake_bind(base_url, *, account, password, token_out, local_base_url, **kwargs):
            try:
                resp = Client(SERVER_NAME=upstream_domain).post(
                    CHAIN_BIND_URL,
                    json.dumps({
                        "account": account,
                        "password": password,
                        "peer_base_url": local_base_url,
                        "token_out": token_out,
                    }),
                    content_type="application/json",
                )
            finally:
                connection.set_tenant(downstream)
            self.assertEqual(resp.status_code, 200, resp.content[:200])
            self.assertEqual(resp.json()["code"], 0, resp.content[:200])
            return resp.json()["data"]["token_out"]

        connection.set_tenant(downstream)
        try:
            client = make_logged_in_client(
                domain=downstream_domain, username=kefu, password=f"pw-{kefu}",
                groups=("客服",), nodes=(CHAIN_PAGE_NODE,),
            )
            with local_override:
                with mock.patch(
                    "apps.business.api.peer_views.bind_upstream", side_effect=_fake_bind
                ):
                    resp = client.post(
                        CHAIN_PAGE_URL,
                        json.dumps({
                            "peer_name": "测试上家甲",
                            # ⭐ C2：新绑定拒 `http://` ⇒ 夹具同步改 https。
                            "peer_base_url": f"https://{upstream_domain}",
                            "peer_account": upstream_account.get_username(),
                            "password": upstream_password,
                        }),
                        content_type="application/json",
                    )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])

    def _configure_upstream(self, ctx, *, percent: int) -> None:
        """把 `ctx` 那条上游绑定的大厅接收开关打开、并设逐跳百分比。"""
        connection.set_tenant(ctx)
        try:
            PeerUpstreamBinding.objects.update(
                hall_sync_enabled=True, hall_price_percent=percent
            )
        finally:
            connection.set_tenant(self.tenant)

    # ── 三端各自的 client ─────────────────────────────────────────────
    def cs_client(self, ctx):
        """`ctx` 里一个持 `/throw-to-hall/` 所需节点的客服 client（按租户缓存）。"""
        cache_key = f"_cs_{ctx.schema_name}"
        if not hasattr(self, cache_key):
            connection.set_tenant(ctx)
            try:
                setattr(self, cache_key, make_logged_in_client(
                    domain=self._domain_of(ctx),
                    username=f"chain-hall-cs-{ctx.schema_name}",
                    password=f"pw-chain-hall-cs-{ctx.schema_name}",
                    groups=("客服",), nodes=CHAIN_THROW_NODES,
                ))
            finally:
                connection.set_tenant(self.tenant)
        return getattr(self, cache_key)

    def hall_client(self, ctx):
        """`ctx` 里一个持大厅读口节点的 client（按租户缓存）。"""
        cache_key = f"_hall_{ctx.schema_name}"
        if not hasattr(self, cache_key):
            connection.set_tenant(ctx)
            try:
                setattr(self, cache_key, make_logged_in_client(
                    domain=self._domain_of(ctx),
                    username=f"chain-hall-reader-{ctx.schema_name}",
                    password=f"pw-chain-hall-reader-{ctx.schema_name}",
                    groups=("打手",), nodes=(CHAIN_HALL_NODE,),
                ))
            finally:
                connection.set_tenant(self.tenant)
        return getattr(self, cache_key)

    def page_client(self, ctx):
        """`ctx` 里一个持**绑定页节点门**的客服 client（关开关 / 解绑走真端点用）。"""
        cache_key = f"_page_{ctx.schema_name}"
        if not hasattr(self, cache_key):
            connection.set_tenant(ctx)
            try:
                setattr(self, cache_key, make_logged_in_client(
                    domain=self._domain_of(ctx),
                    username=f"chain-hall-page-{ctx.schema_name}",
                    password=f"pw-chain-hall-page-{ctx.schema_name}",
                    groups=("客服",), nodes=(CHAIN_PAGE_NODE,),
                ))
            finally:
                connection.set_tenant(self.tenant)
        return getattr(self, cache_key)

    def switch_hall_sync_off(self, ctx, binding):
        """在 `ctx` 的绑定页上真打一次 PUT（`hall_sync_enabled=True → False`）。"""
        client = self.page_client(ctx)
        try:
            resp = client.put(
                f"{CHAIN_PAGE_URL}{binding.pk}/",
                json.dumps({"hall_sync_enabled": False}),
                content_type="application/json",
            )
        finally:
            connection.set_tenant(self.tenant)
        return resp

    def unbind_page(self, ctx, binding):
        """在 `ctx` 的绑定页上真打一次 DELETE（软删上家绑定；通知上家那一步打桩）。"""
        client = self.page_client(ctx)
        try:
            with mock.patch("apps.business.api.peer_views.unbind_upstream"):
                resp = client.delete(f"{CHAIN_PAGE_URL}{binding.pk}/")
        finally:
            connection.set_tenant(self.tenant)
        return resp

    # ── 动作 ─────────────────────────────────────────────────────────
    def make_hall_order(self, ctx, order_no):
        """在 `ctx` 建一张待抛单（UNASSIGNED + 发单价；⛔ 不建 MAIN 行 ⇒ 下面自己补）。"""
        connection.set_tenant(ctx)
        try:
            game, _ = Game.objects.get_or_create(name=CHAIN_GAME)
            order = Order.objects.create(
                game=game,
                order_no=order_no,
                status=OrderStatus.UNASSIGNED,
                total_price=Decimal("120.00"),
                hall_price=Decimal(A_PRICE),
            )
            # `notify_order_status_changed` 要 MAIN 行才排 `order.update`（转正顺序用例依赖它）
            # ⇒ 与 `test_peer_hall_claim` 的夹具同款补一行（归属人留空 = 还没人接）。
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("30.00")
            )
            return order
        finally:
            connection.set_tenant(self.tenant)

    def throw_to_hall(self, ctx, order, price=A_PRICE):
        """打真端点 `POST .../throw-to-hall/`（带价 ⇒ 写发单价 + 排 `hall.throw`）。"""
        client = self.cs_client(ctx)
        try:
            resp = client.post(
                f"{CHAIN_ORDERS_URL}/{order.order_no}/throw-to-hall/",
                json.dumps({"price": str(price)}),
                content_type="application/json",
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])

    def deliver_peer_events(self, ctx):
        """把 `ctx` **自己**入队的出站事件逐个真投给对端（不启常驻进程）。⚠️ 见 `ChainFixture`。"""
        connection.set_tenant(ctx)
        try:
            event_pks = list(
                PeerSyncEvent.objects.filter(
                    direction="out", status=PeerSyncEvent.Status.PENDING
                )
                .order_by("created_at", "pk")
                .values_list("pk", flat=True)
            )
        finally:
            connection.set_tenant(self.tenant)

        def _fake_push(base_url, token, events, **kwargs):
            domain = urlsplit(base_url).hostname
            # ⭐ 自证夹具：投递基址必须是本次夹具的租户域名之一。
            self.assertIn(
                domain, set(self._domains.values()),
                f"投递基址不是本次夹具的租户域名：{base_url!r}",
            )
            try:
                resp = Client(SERVER_NAME=domain).post(
                    CHAIN_INBOUND_URL,
                    json.dumps({"events": events}),
                    content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer " + token,
                )
            finally:
                # ⚠️ 内层请求把连接切到了对端 ⇒ 必须切回 `ctx`，否则 worker 随后的记账
                #    （事件 `done` / 绑定健康列）会写进**对端的库**。
                connection.set_tenant(ctx)
            envelope = resp.json()
            if envelope.get("code") != 0:
                raise PeerRejected(envelope.get("msg") or "对端拒收了事件")
            data = envelope.get("data")
            return data if isinstance(data, dict) else {}

        now = django_timezone.now()
        with mock.patch("apps.business.peer_worker.push_events", side_effect=_fake_push):
            connection.set_tenant(ctx)
            try:
                for event_pk in event_pks:
                    outcome = deliver_one(event_pk, now=now)
                    self.assertEqual(
                        outcome, "done",
                        f"{ctx.schema_name} 的事件 {event_pk} 投递结果是 {outcome!r}（应为 done）",
                    )
            finally:
                connection.set_tenant(self.tenant)

    # ── 读面 / 断言助手 ───────────────────────────────────────────────
    def hall_rows(self, ctx):
        """`GET /api/business/hall/` 的全部行（业务码必须为 0）。"""
        client = self.hall_client(ctx)
        try:
            resp = client.get(CHAIN_HALL_URL)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def hall_card(self, ctx, card_no):
        """大厅里那张**团团通镜像卡**（`origin="peer"`，按卡上的展示单号找）。"""
        rows = [
            row for row in self.hall_rows(ctx)
            if row.get("origin") == "peer" and row.get("remote_order_view") == card_no
        ]
        if not rows:
            self.fail(f"{ctx.schema_name} 的大厅里没有卡 {card_no!r}")
        return rows[0]

    def mirror(self, ctx, card_no):
        """`ctx` 里那张镜像行（按卡号）；取不到就 `self.fail`（⛔ 不许回 `None` 让后面静默通过）。"""
        connection.set_tenant(ctx)
        try:
            row = PeerHallMirror.objects.filter(peer_order_no=card_no).first()
        finally:
            connection.set_tenant(self.tenant)
        if row is None:
            self.fail(f"{ctx.schema_name} 里没有卡号 {card_no!r} 的镜像行")
        return row

    def downstream_binding(self, ctx, domain):
        """`ctx`（作为上家）给 `domain` 那家下家的绑定行。"""
        connection.set_tenant(ctx)
        try:
            # `peer_base_url` 是**带 scheme** 的值（下家握手时按请求推导出来）⇒ 按域名子串匹配。
            binding = PeerDownstreamBinding.objects.filter(
                peer_base_url__icontains=domain
            ).first()
        finally:
            connection.set_tenant(self.tenant)
        if binding is None:
            self.fail(f"{ctx.schema_name} 里没有指向 {domain!r} 的下游绑定")
        return binding

    def upstream_binding(self, ctx):
        """`ctx` 那条上游绑定（本链上每家只有一个上家）。"""
        connection.set_tenant(ctx)
        try:
            binding = PeerUpstreamBinding.objects.first()
        finally:
            connection.set_tenant(self.tenant)
        if binding is None:
            self.fail(f"{ctx.schema_name} 里没有上游绑定")
        return binding

    def events_of(self, ctx, *, kind, binding_role, binding_id, target_ref=None):
        """`ctx` 库里某个 kind 的事件行（按绑定与 ref 收窄）。"""
        connection.set_tenant(ctx)
        try:
            qs = PeerSyncEvent.objects.filter(
                kind=kind, binding_role=binding_role, binding_id=str(binding_id)
            )
            if target_ref is not None:
                qs = qs.filter(target_ref=target_ref)
            return list(qs.order_by("pk"))
        finally:
            connection.set_tenant(self.tenant)

    def promoted_order(self, ctx, card_no):
        """转正单（**在 `ctx` 的 schema 里**取出来再返回）。

        ⚠️ 承重：`PeerHallMirror.promoted_order` 是**延迟外键**，回到本租户连接之后再读它
        会以本租户的 `Order` 表去解析那个 pk —— 两侧订单表的 pk 都从 1 起，于是拿到**另一个
        租户的单**（本夹具实测踩到：B 的单 90.00 被读成 A 的 100.00）。
        """
        connection.set_tenant(ctx)
        try:
            row = PeerHallMirror.objects.filter(peer_order_no=card_no).first()
            order = (
                None
                if row is None or row.promoted_order_id is None
                else Order.objects.filter(pk=row.promoted_order_id).first()
            )
        finally:
            connection.set_tenant(self.tenant)
        if order is None:
            self.fail(f"{ctx.schema_name} 里卡 {card_no!r} 没有转正单")
        return order

    def read_mirror(self, ctx, card_no):
        """重新读一遍镜像行（避免拿到夹具里过期的内存对象）。"""
        connection.set_tenant(ctx)
        try:
            return PeerHallMirror.objects.filter(peer_order_no=card_no).first()
        finally:
            connection.set_tenant(self.tenant)

    # ── 链的搭法 ─────────────────────────────────────────────────────
    def build_two_hop_chain(self):
        """把 A→B→C 跑到「C 的大厅里能看到那张卡」为止。

        回 `(a_order, b_card, c_card)`。⚠️ 每一次投递都必须显式驱动（spec §16.6：请求路径
        只入队）：A 抛单后的 `hall.throw` / `hall.reprice` 投给 B，B 转发的 `hall.throw` /
        `hall.reprice` 再投给 C。
        """
        a_order = self.make_hall_order(self.a, CHAIN_A_ORDER_NO)
        self.throw_to_hall(self.a, a_order)
        self.deliver_peer_events(self.a)                 # A → B
        b_card = self.mirror(self.b, CHAIN_A_ORDER_NO)
        self.deliver_peer_events(self.b)                 # B → C（二次转发）
        c_card = self.mirror(self.c, CHAIN_A_ORDER_NO)
        return a_order, b_card, c_card


class PricePerHopTests(HallChainFixture):
    """① 逐跳相乘：100 → 90 → 72（读口 + 落单都验）。"""

    def test_the_read_side_shows_100_90_72(self):
        """三端大厅读口：A 原生 100.00 / B 90.00 / C 72.00。

        ⚠️ 判别力：B 那一格必须**不是** 100（少乘 B 的 90%），C 那一格必须**不是** 80
        （少乘 C 的 80% —— 那正是「转发时送的是上家原价」这个缺陷的形状）。
        """
        self.build_two_hop_chain()
        self.assertEqual(self.hall_card(self.b, CHAIN_A_ORDER_NO)["hall_price"], "90.00")
        self.assertEqual(self.hall_card(self.c, CHAIN_A_ORDER_NO)["hall_price"], "72.00")
        # A 那边是**原生行**（不是镜像卡）：单号直接就是它自己的号，价 100。
        native = [
            row for row in self.hall_rows(self.a)
            if row.get("order_no") == CHAIN_A_ORDER_NO
        ]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0]["hall_price"], "100.00")

    def test_the_card_number_is_the_source_number_all_the_way_down(self):
        """⭐ 二次转发**不换卡上的单号**：B / C 的镜像行都记 A 的单号（认单键的地基）。"""
        _a_order, b_card, c_card = self.build_two_hop_chain()
        self.assertEqual(b_card.peer_order_no, CHAIN_A_ORDER_NO)
        self.assertEqual(c_card.peer_order_no, CHAIN_A_ORDER_NO)
        # 上家发单价（`remote_hall_price`）逐跳存的是**上一层的展示价**：B=100、C=90。
        self.assertEqual(b_card.remote_hall_price, Decimal(A_PRICE))
        self.assertEqual(c_card.remote_hall_price, B_PRICE)


class RepricePercentPropagationTests(HallChainFixture):
    """⭐ N5（R3-M6）I3 / 阻塞 1：绑定页改百分比要沿链传导，且**转发跳数从卡自己的深度起算**。"""

    def _set_percent(self, ctx, percent):
        """在 `ctx` 的绑定页真打一次 PUT（只改百分比）。"""
        binding = self.upstream_binding(ctx)
        client = self.page_client(ctx)
        try:
            resp = client.put(
                f"{CHAIN_PAGE_URL}{binding.pk}/",
                json.dumps({"hall_price_percent": percent}),
                content_type="application/json",
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        return binding

    def _hidden_order(self, ctx, card_no):
        connection.set_tenant(ctx)
        try:
            card = PeerHallMirror.objects.get(peer_order_no=card_no)
            return Order.objects.get(hall_card=card)
        finally:
            connection.set_tenant(self.tenant)

    def _set_card_hop(self, ctx, card_no, hop):
        connection.set_tenant(ctx)
        try:
            PeerHallMirror.objects.filter(peer_order_no=card_no).update(hop=hop)
        finally:
            connection.set_tenant(self.tenant)

    def test_changing_b_percent_updates_b_hidden_order_and_forwards_to_c(self):
        """A→B→C：改 B 对 A 的百分比 ⇒ B 的卡 / 隐藏单 / C 的镜像卡都换到新价。"""
        self.build_two_hop_chain()
        b_hidden = self._hidden_order(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_hidden.hall_price, B_PRICE)
        self._set_percent(self.b, 80)

        self.assertEqual(
            self.hall_card(self.b, CHAIN_A_ORDER_NO)["hall_price"], "80.00"
        )
        b_hidden = self._hidden_order(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_hidden.hall_price, Decimal("80.00"), "B 的隐藏单没跟上百分比")

        # B 把 reprice 转发给 C；C 的镜像行上家价 = B 的**新展示价**（不是 A 的原价）。
        self.deliver_peer_events(self.b)
        c_card = self.read_mirror(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_card.remote_hall_price, Decimal("80.00"))
        self.assertEqual(
            self.hall_card(self.c, CHAIN_A_ORDER_NO)["hall_price"], "64.00"
        )
        c_hidden = self._hidden_order(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_hidden.hall_price, Decimal("64.00"))

    def test_percent_entry_uses_the_cards_own_hop_and_stops_at_the_limit(self):
        """⭐ 阻塞 1：百分比入口的起点 = **那张卡自己的深度**（⛔ 不是 0 / None）。

        `hop=5` ⇒ 再转发就是第 6 跳 ⇒ 只留一条 `peer.hall_hop_limit` 台账（人话含「价」）、
        ⛔ 不外发；`hop=4` ⇒ 转发出去的 `hop=5`。
        """
        from apps.business.peer_inbound import HOP_LIMIT_LEDGER_KIND
        from apps.business.peer_sync import HALL_REPRICE_KIND

        _a_order, b_card, _c_card = self.build_two_hop_chain()
        self._set_card_hop(self.b, CHAIN_A_ORDER_NO, 5)
        b_down = self.downstream_binding(self.b, HALL_DOMAIN_C)
        b_up = self.upstream_binding(self.b)
        # 建链时 A 的第一次带价抛单本身会排一条 reprice（B 已转发给 C）⇒ 以它为基线，
        # 只判「改百分比有没有**新增**出站事件」。
        baseline = self.events_of(
            self.b, kind=HALL_REPRICE_KIND, binding_role="downstream",
            binding_id=b_down.pk, target_ref=CHAIN_A_ORDER_NO,
        )

        self._set_percent(self.b, 70)
        self.assertEqual(
            len(self.events_of(
                self.b, kind=HALL_REPRICE_KIND, binding_role="downstream",
                binding_id=b_down.pk, target_ref=CHAIN_A_ORDER_NO,
            )),
            len(baseline),
            "hop=5 的卡改百分比仍往外转发（跳数上限没生效）",
        )
        ledger = self.events_of(
            self.b, kind=HOP_LIMIT_LEDGER_KIND, binding_role="upstream",
            binding_id=b_up.pk, target_ref=CHAIN_A_ORDER_NO,
        )
        self.assertEqual(len(ledger), 1, ledger)
        self.assertEqual(ledger[0].payload["hop"], 5)
        self.assertIn("价", ledger[0].reason)

        # hop=4 ⇒ 转发出去的是第 5 跳（百分比入口真的把卡的深度带进了转发器）。
        self._set_card_hop(self.b, CHAIN_A_ORDER_NO, 4)
        self._set_percent(self.b, 60)
        forwarded = self.events_of(
            self.b, kind=HALL_REPRICE_KIND, binding_role="downstream",
            binding_id=b_down.pk, target_ref=CHAIN_A_ORDER_NO,
        )
        self.assertEqual(len(forwarded), len(baseline) + 1, forwarded)
        self.assertEqual(forwarded[-1].payload["hop"], 5)


class ChainClaimTests(HallChainFixture):
    """②③ 整链抢单 + 交单 + 链身份。"""

    def _claim_from_c(self):
        """C 的打手抢下那张卡（走**领域函数**：端点层自己已有用例 `test_peer_hall_claim`）。"""
        connection.set_tenant(self.c)
        try:
            c_card = PeerHallMirror.objects.get(peer_order_no=CHAIN_A_ORDER_NO)
            return claim_peer_mirror(
                user_id=self.c_taker.pk, mirror_id=c_card.pk
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_claim_walks_up_the_chain_and_hands_the_order_down_priced_per_hop(self):
        a_order, _b_card, _c_card = self.build_two_hop_chain()
        self._claim_from_c()

        # ── ②-a C 的 claim 到 B：B 占住自己那张卡 + 记下「是谁在要」+ 向上家转发 ──
        self.deliver_peer_events(self.c)
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_card.status, PeerHallMirror.Status.CLAIMING)
        c_binding_in_b = self.downstream_binding(self.b, HALL_DOMAIN_C)
        self.assertEqual(b_card.claim_downstream_id, c_binding_in_b.pk)
        self.assertNotEqual(b_card.claim_id, "")
        self.assertIsNotNone(b_card.claiming_at)
        # ⛔ 多跳这一档**不写** `claiming_by`（点单的人不在本租户里）。
        self.assertIsNone(b_card.claiming_by_id)
        # B 用**自己那条上家绑定**把 claim 转给 A（同一条 outbox、同一个事件形状）。
        b_upstream = self.upstream_binding(self.b)
        forwarded = self.events_of(
            self.b, kind="hall.claim", binding_role="upstream",
            binding_id=b_upstream.pk, target_ref=CHAIN_A_ORDER_NO,
        )
        self.assertEqual(len(forwarded), 1)

        # ── ②-b A 收到转发来的 claim：原生支判决（机器接手 + order.assign + claim_ok）──
        self.deliver_peer_events(self.b)
        connection.set_tenant(self.a)
        try:
            a_order.refresh_from_db()
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(a_order.status, OrderStatus.IN_PROGRESS)
        connection.set_tenant(self.a)
        try:
            main = Performance.objects.get(order=a_order, emp_type=EmpType.MAIN)
        finally:
            connection.set_tenant(self.tenant)
        # A 把单交给**B 在 A 的账号**（`PeerDownstreamBinding.account`）。
        self.assertEqual(main.user_id, self.a_bound_account.pk)

        # ── ②-c A 的 order.assign 回到 B：B 转正、按 90 交给 C 的账号、并排 order.assign ──
        self.deliver_peer_events(self.a)
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_card.status, PeerHallMirror.Status.PROMOTED)
        b_order = self.promoted_order(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_order.hall_price, B_PRICE)          # ⭐ 落单价 = 本层展示价
        self.assertEqual(b_order.source_order_no, CHAIN_A_ORDER_NO)
        connection.set_tenant(self.b)
        try:
            b_main = Performance.objects.get(order=b_order, emp_type=EmpType.MAIN)
        finally:
            connection.set_tenant(self.tenant)
        # 过账单的接单人 = **下一跳的绑定账号**（本租户没有本地打手参与）。
        self.assertEqual(b_main.user_id, self.b_bound_account.pk)
        handed = self.events_of(
            self.b, kind="order.assign", binding_role="downstream",
            binding_id=c_binding_in_b.pk,
        )
        self.assertEqual(len(handed), 1)
        handed_payload = handed[0].payload
        self.assertEqual(handed_payload["hall_price"], "90.00")
        # ⭐ ③ 链身份：C 靠它认回自己那张卡（卡的号是 A 的号，单的号是 B 生成的）。
        self.assertEqual(handed_payload["origin_order_no"], CHAIN_A_ORDER_NO)
        self.assertNotEqual(handed_payload["order_no"], CHAIN_A_ORDER_NO)

        # ── ②-d C 收到 order.assign：转正 + 落到 72 + 挂上 C 的打手 ──
        self.deliver_peer_events(self.b)
        c_card = self.read_mirror(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_card.status, PeerHallMirror.Status.PROMOTED)
        c_order = self.promoted_order(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_order.hall_price, C_PRICE)          # ⭐ 最下家落单价 72
        self.assertEqual(c_order.status, OrderStatus.IN_PROGRESS)
        connection.set_tenant(self.c)
        try:
            c_main = Performance.objects.get(order=c_order, emp_type=EmpType.MAIN)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(c_main.user_id, self.c_taker.pk)

    def test_the_assign_event_for_c_carries_the_source_order_no(self):
        """③ 判据只看**载荷**（C 真正收到的那一份）：`origin_order_no` = 源头 A 的单号。

        ⚠️ 判别力：把这一键去掉，C 的 `_hall_card_no` 会退回 `target_ref`（= B 的单号）
        ⇒ 认不回自己那张卡、转正落空（上一条用例的 C 就不是 `promoted` 了）。
        """
        a_order, _b, _c = self.build_two_hop_chain()
        self._claim_from_c()
        self.deliver_peer_events(self.c)
        self.deliver_peer_events(self.b)
        self.deliver_peer_events(self.a)
        c_binding_in_b = self.downstream_binding(self.b, HALL_DOMAIN_C)
        handed = self.events_of(
            self.b, kind="order.assign", binding_role="downstream",
            binding_id=c_binding_in_b.pk,
        )
        self.assertEqual(len(handed), 1)
        self.assertEqual(handed[0].payload["origin_order_no"], a_order.order_no)

    def test_the_passing_tier_does_not_queue_member_assign_for_this_hop(self):
        """⭐ 2026-10-26：多跳那一档（`claim_downstream` 非空）⛔ **不排** `member.assign`。

        B 的转正单在本层只是**过账单**（最终做单在 C 的租户里，接单人 = C 在 B 的绑定账号）
        ⇒ 给本层这个账号录到「他自己绑定的下家鸡蛋铺」是错的。

        ⚠️ 判别力：先给这个账号建一条 member 绑定 —— 否则「排不排」两版代码都是 0 条，
        用例抓不住错接线；把 `queue_assign` 错接在多跳档上这条会红。
        """
        connection.set_tenant(self.b)
        try:
            JidanpuMemberBinding.objects.create(
                user=self.b_bound_account,
                jidanpu_base_url="https://member-chain-b.test.invalid",
                account="syn-member-chain-b",
                password="pw-syn",
                enabled=True,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.build_two_hop_chain()
        self._claim_from_c()
        self.deliver_peer_events(self.c)              # C 的 claim 到 B：B 占卡 + 向上游转发
        # 自证：这一档确实是**多跳**那一档（`claim_downstream` 有值、`claiming_by` 为空）。
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertIsNotNone(b_card.claim_downstream_id)
        self.assertIsNone(b_card.claiming_by_id)
        self.deliver_peer_events(self.b)              # B 的 claim 到 A：A 机器接手并派回
        self.deliver_peer_events(self.a)              # A 的 order.assign 到 B：B 转正
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_card.status, PeerHallMirror.Status.PROMOTED)
        connection.set_tenant(self.b)
        try:
            self.assertFalse(
                JidanpuSyncEvent.objects.filter(
                    direction="out", kind="member.assign"
                ).exists()
            )
        finally:
            connection.set_tenant(self.tenant)


class ChainRefusedTests(HallChainFixture):
    """④ 拒绝沿链回传：**订单已不存在**这一类 ⇒ 逐跳**彻底下架**（gone），不再回大厅。

    ⭐ 业务方：`hall.claim_refused` 按原因分因——「订单已不存在 / 没共享给这家」
    ⇒ `gone`；「网络超时 / 上家没响应」⇒ `listed` 等重试。本用例走的是**前者**：
    A 那张单已经不在大厅（源头上就被别人接走 / 撤下）⇒ B、C 两张卡都该下架。
    """

    def test_a_refusal_walks_back_down_and_every_hop_goes_off_the_hall(self):
        self.build_two_hop_chain()
        connection.set_tenant(self.c)
        try:
            c_card = PeerHallMirror.objects.get(peer_order_no=CHAIN_A_ORDER_NO)
            claim_peer_mirror(user_id=self.c_taker.pk, mirror_id=c_card.pk)
        finally:
            connection.set_tenant(self.tenant)
        self.deliver_peer_events(self.c)                 # C 的 claim → B
        c_binding_in_b = self.downstream_binding(self.b, HALL_DOMAIN_C)
        self.assertEqual(
            self.read_mirror(self.b, CHAIN_A_ORDER_NO).claim_downstream_id,
            c_binding_in_b.pk,
        )

        # A 那张单**已经不在大厅**了（这里直接改状态：⛔ 不走状态机 ⇒ 不排 hall.take，
        # 这样 refused 的**原因**就是「不在大厅」那一类 ⇒ 逐跳 gone；
        # 「上家一直没回话」那一档（回 listed）由 `peer_hall.reclaim_stuck_claims` 兜）。
        connection.set_tenant(self.a)
        try:
            order = Order.objects.get(order_no=CHAIN_A_ORDER_NO)
            order.status = OrderStatus.ISSUE
            order.save(update_fields=["status", "updated_at"])
        finally:
            connection.set_tenant(self.tenant)

        self.deliver_peer_events(self.b)                 # B 把 claim 转给 A
        a_downstream = self.downstream_binding(
            self.a, f"https://{self._domain_of(self.b)}"
        )
        refused_to_b = self.events_of(
            self.a, kind="hall.claim_refused", binding_role="downstream",
            binding_id=a_downstream.pk, target_ref=CHAIN_A_ORDER_NO,
        )
        self.assertEqual(len(refused_to_b), 1)
        self.assertIn("上家拒绝", (refused_to_b[0].payload or {}).get("reason", ""))

        # ── B 这一跳：行 gone + 四列清空 + 把同一条 refused 原因转给 C ──
        self.deliver_peer_events(self.a)
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_card.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(b_card.gone_at)
        self.assertIsNone(b_card.claim_downstream_id)
        self.assertEqual(b_card.claim_id, "")
        self.assertIsNone(b_card.claiming_at)
        self.assertIsNone(b_card.claiming_by_id)
        self.assertIn("上家拒绝", b_card.last_error)
        forwarded = self.events_of(
            self.b, kind="hall.claim_refused", binding_role="downstream",
            binding_id=c_binding_in_b.pk, target_ref=CHAIN_A_ORDER_NO,
        )
        self.assertEqual(len(forwarded), 1)
        self.assertIn("上家拒绝", (forwarded[0].payload or {}).get("reason", ""))

        # ── C 这一跳：自己也 gone（**清掉 claiming_by**：那才是它那张卡的收口）──
        self.deliver_peer_events(self.b)
        c_card = self.read_mirror(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_card.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(c_card.gone_at)
        self.assertIsNone(c_card.claiming_by_id)
        self.assertIsNone(c_card.claiming_at)
        self.assertEqual(c_card.claim_id, "")
        self.assertIn("上家拒绝", c_card.last_error)


class MultiHopReclaimTests(HallChainFixture):
    """⑤ 多跳行超时回收（D-0335 起 60 秒；中间层卡住的那一行同样被放回大厅，并清两列）。"""

    def test_a_stuck_middle_hop_card_is_reclaimed_with_its_claim_columns(self):
        self.build_two_hop_chain()
        connection.set_tenant(self.c)
        try:
            c_card = PeerHallMirror.objects.get(peer_order_no=CHAIN_A_ORDER_NO)
            claim_peer_mirror(user_id=self.c_taker.pk, mirror_id=c_card.pk)
        finally:
            connection.set_tenant(self.tenant)
        self.deliver_peer_events(self.c)                 # → B 收到，行进 claiming（多跳）
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_card.status, PeerHallMirror.Status.CLAIMING)
        self.assertNotEqual(b_card.claim_id, "")

        now = django_timezone.now()
        connection.set_tenant(self.b)
        try:
            # 判据是 `claiming_at < now - RECLAIM_STUCK_CLAIMING_SECONDS`（严格小于；
            # ⭐ D-0335 起阈值 60 秒）⇒ 放到 61 秒前。
            PeerHallMirror.objects.filter(pk=b_card.pk).update(
                claiming_at=now - timedelta(seconds=61)
            )
            reclaimed = reclaim_stuck_claims(now=now)
            row = PeerHallMirror.objects.get(pk=b_card.pk)
            ledger = PeerSyncEvent.objects.get(
                kind="hall.claim_recycle", target_ref=CHAIN_A_ORDER_NO
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(reclaimed, 1)
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(row.last_error, RECLAIMED_NOTE)
        # ⭐ 四个接单列一起清（多跳那两列也在内）。
        self.assertIsNone(row.claiming_by_id)
        self.assertIsNone(row.claiming_at)
        self.assertIsNone(row.claim_downstream_id)
        self.assertEqual(row.claim_id, "")
        self.assertEqual(ledger.payload["stuck_seconds"], 60)


class SingleHopRegressionTests(HallChainFixture):
    """⑥ 单跳回归：载荷里**没有**链身份键、镜像行**不写**多跳那两列。"""

    def test_a_single_hop_assign_has_no_chain_identity_key(self):
        """A 的原生单推给 B ⇒ `order.assign` 载荷里**没有** `origin_order_no`（行为逐字不变）。

        ⚠️ 判别力：这一键是 P4 的加法，**单跳 / 原生单必须不出现**——出现了说明
        `_origin_order_no` 把「没有上游链路」也写成了一个值，接收侧的回落口径就失效了。
        """
        a_order = self.make_hall_order(self.a, CHAIN_A_ORDER_NO)
        self.throw_to_hall(self.a, a_order)
        self.deliver_peer_events(self.a)                 # A → B：卡在 B 的大厅里
        # B 的打手抢这张卡（本地大厅点单：`claiming_by` 有值、多跳两列为空）。
        connection.set_tenant(self.b)
        try:
            b_card = PeerHallMirror.objects.get(peer_order_no=CHAIN_A_ORDER_NO)
            claim_peer_mirror(
                user_id=self.b_bound_account.pk, mirror_id=b_card.pk
            )
            b_card.refresh_from_db()
            self.assertEqual(b_card.status, PeerHallMirror.Status.CLAIMING)
            self.assertEqual(b_card.claiming_by_id, self.b_bound_account.pk)
            self.assertIsNone(b_card.claim_downstream_id)
            self.assertEqual(b_card.claim_id, "")
            # 上行的 claim 事件：形状与 v1 逐字同形（载荷只有 `claim_by`）。
            claim_event = PeerSyncEvent.objects.get(
                kind="hall.claim", binding_role="upstream"
            )
            self.assertEqual(claim_event.target_ref, CHAIN_A_ORDER_NO)
            self.assertEqual(set(claim_event.payload), {"claim_by"})
        finally:
            connection.set_tenant(self.tenant)

        self.deliver_peer_events(self.b)                 # → A 判决
        a_downstream = self.downstream_binding(
            self.a, f"https://{self._domain_of(self.b)}"
        )
        assigns = self.events_of(
            self.a, kind="order.assign", binding_role="downstream",
            binding_id=a_downstream.pk,
        )
        self.assertEqual(len(assigns), 1)
        self.assertNotIn("origin_order_no", assigns[0].payload)

        self.deliver_peer_events(self.a)                 # → B 转正
        b_card = self.read_mirror(self.b, CHAIN_A_ORDER_NO)
        self.assertEqual(b_card.status, PeerHallMirror.Status.PROMOTED)
        self.assertEqual(
            self.promoted_order(self.b, CHAIN_A_ORDER_NO).hall_price, B_PRICE
        )
        # 单跳的转正**不排** order.assign 给下一跳（C 那张卡此时是 B 转发过的同款卡，
        # 但 B 没有「下一跳在要」这回事 ⇒ 只有 hall.take 把它撤下来，没有新单推过去）。
        c_binding_in_b = self.downstream_binding(self.b, HALL_DOMAIN_C)
        self.assertEqual(
            self.events_of(
                self.b, kind="order.assign", binding_role="downstream",
                binding_id=c_binding_in_b.pk,
            ),
            [],
        )


class DownstreamCardWithdrawTests(HallChainFixture):
    """⭐ B3（终审修复轮）：B 关开关 / 解绑 ⇒ **C 手里那张卡也要 gone**（不是只 B 本地 gone）。

    判据打在**真实的出口**上：C 的镜像行状态 + C 的大厅读口（`GET /api/business/hall/`），
    ⛔ 不是「B 有没有入队一条事件」——多跳下游会不会真的跟着消失才是这条修复要回答的问题。
    """

    def test_switching_the_switch_off_takes_the_card_from_the_next_hop(self):
        self.build_two_hop_chain()
        # 正对照：关之前 C 大厅里看得见这张卡。
        self.assertEqual(
            self.hall_card(self.c, CHAIN_A_ORDER_NO)["remote_order_view"],
            CHAIN_A_ORDER_NO,
        )
        b_binding = self.upstream_binding(self.b)
        with self.assertLogs("apps.business.api.peer_views", level="INFO"):
            resp = self.switch_hall_sync_off(self.b, b_binding)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        # B 本地先 gone（既有 P5 口径）。
        self.assertEqual(
            self.read_mirror(self.b, CHAIN_A_ORDER_NO).status,
            PeerHallMirror.Status.GONE,
        )
        # ⭐ B 的 take 真的投给 C ⇒ C 那张卡 gone、大厅里不再出现。
        self.deliver_peer_events(self.b)
        c_row = self.read_mirror(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_row.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(c_row.gone_at)
        self.assertEqual(
            [
                row for row in self.hall_rows(self.c)
                if row.get("origin") == "peer"
                and row.get("remote_order_view") == CHAIN_A_ORDER_NO
            ],
            [],
        )

    def test_unbinding_takes_the_card_from_the_next_hop(self):
        self.build_two_hop_chain()
        b_binding = self.upstream_binding(self.b)
        with self.assertLogs("apps.business.api.peer_views", level="INFO"):
            resp = self.unbind_page(self.b, b_binding)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        self.deliver_peer_events(self.b)
        c_row = self.read_mirror(self.c, CHAIN_A_ORDER_NO)
        self.assertEqual(c_row.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(c_row.gone_at)
