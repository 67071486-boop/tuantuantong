"""上下游 P4：**链身份 `origin_order_no`**（Task 2）+ **三租户链端到端**（Task 3）。

本文件放三段：

- `OriginOrderNoTests` / `OriginOrderNoShapeTests`：P4 Task 2 的**逐跳传播**与**形状闸**；
- `ChainFixture` + `ThreeTenantChainTests`：P4 Task 3 的**四个真租户**（A→B→C→D）端到端，
  含「回传只一级」的否定用例。截图按它分段是 Tasks 4/5 的事。

口径（控制器裁决 R-P4-6 / R-P4-8 / R-P4-9 / R-P4-10 / R-P4-11）：

- `origin_order_no` 记的是**这张单在链上最初那一跳的单号**：
  首跳（本地还没有这张单）⇒ 就是**对端的单号本身**（`peer_order_no` 兜底）；
  其后的跳 ⇒ 取载荷里的 `source_order_no`。
- ⚠️ **2026-10-25 改准**（计划 `2026-10-25-peer-source-identity`）：原先「第 4 跳起退成第 k-2 跳
  那个租户的单号」的残差**在新链路上已消失**——发送侧的身份键改成了
  `effective_source_order_no`（上家那一列，逐跳不变）⇒ 每一跳存到的都是**最初那一跳**的号。
  ⛔ **老链路（升级前建的）仍漂**且不反推（RUNBOOK §3.2f）；本文件建的是新链路
  ⇒ `ThreeTenantChainTests` 现在钉「第 4 跳仍是 A 的单号」。
- ⚠️ **接手即转发**（业务方裁定）：接手类事件（`order.assign` / `order.grab`）
  落到对方那张单上一律是**未分配**；回传（状态）**只走一级**——接收侧不走状态漏斗，
  C 改状态会让 B 跟着变、**不会**再传到 A（`test_cs_change_does_not_reach_a`）。
- ⚠️ 与 `peer_order_no` 的分工是**语义**上的（⛔ 不是在数值上恒不相等）：本列记**链的起点**，
  `peer_order_no` 记**直接上家的来源单号**——首跳与兜底时两者恰好同值。
- ⭐ 评审必修 1：链身份的值是**对端 JSON** 给的 ⇒ 写列前必须过形状闸（见
  `OriginOrderNoShapeTests`）：非空字符串才用，其余一律退回对端单号。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
⚠️ 夹具一律**合成值**（`https://peer-a.test.invalid` / `测试工作室甲` / `2026…` 单号）。
⚠️ `ThreeTenantChainTests` 里那张单的本地单号**必须 20 位数字**（`/assign/`、`/transition/`
走 router 的详情路由，`lookup_value_regex = r"\\d{20}"`），见 `CHAIN_A_ORDER_NO` 的注释。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone as django_timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.api.peer_views import receive_events
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_client import PeerRejected
from apps.business.peer_sync import hash_token, new_token
from apps.business.peer_worker import deliver_one
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

User = get_user_model()

#: 两端对齐用的游戏名（裁决 R21：按名字对齐，⛔ 不传 id）。
GAME = "测试游戏"
#: 事件时刻的基准（合成值；`plus_seconds` 相对它推移）。
BASE_TIME = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


class TestPeerBase(TenantTestCase):
    """共用夹具：一款游戏、一条启用的上游绑定（下家侧）。"""

    def setUp(self):
        self.game = Game.objects.create(name=GAME)
        self.kefu = get_user_model().objects.create_user(
            username="syn-p4-chain-kefu", password="pw-syn-p4"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            # 令牌两列按模型口径给全（`token_in_hash` 只存哈希、`token_out` 存明文）。
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _event(self, kind, order_no, *, status="IN_PROGRESS", game_name=GAME,
               plus_seconds=0, source_order_no=None):
        """造一条事件：`kind` / `target_ref` / `occurred_at` 在**顶层**，订单字段进 `payload`。

        `source_order_no` 走 `payload`（上家 `order_payload` 的白名单字段）：**不给**
        （`None`）时**不进载荷**，给空串时进载荷的空串——两种情况都测（Step 1 已覆盖）。
        """
        payload = {
            "status": status,
            "total_price": "88.00",
            "remark": "上家备注",
            "content": "客户原始需求",
            "game_name": game_name,
        }
        if source_order_no is not None:
            payload["source_order_no"] = source_order_no
        return {
            "kind": kind,
            "target_ref": order_no,
            "occurred_at": (BASE_TIME + timedelta(seconds=plus_seconds)).isoformat(),
            "payload": payload,
        }


class OriginOrderNoTests(TestPeerBase):
    """建行时写 `origin_order_no`：首跳用对端单号，其后取载荷的 `source_order_no`。"""

    def test_the_first_hop_stores_the_peers_order_no(self):
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-ORIG-1", status="IN_PROGRESS", game_name=GAME)])
        link = PeerOrderLink.objects.get(peer_order_no="SYN-P4-ORIG-1")
        self.assertEqual(link.origin_order_no, "SYN-P4-ORIG-1")

    def test_the_second_hop_keeps_the_origin_not_the_middle_order_no(self):
        """载荷里的 `source_order_no` 带着最初那一跳的单号 ⇒ 第二跳存的必须是它。"""
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-MID-1", status="IN_PROGRESS", game_name=GAME,
            source_order_no="SYN-P4-ORIG-1",
        )])
        link = PeerOrderLink.objects.get(peer_order_no="SYN-P4-MID-1")
        self.assertEqual(link.origin_order_no, "SYN-P4-ORIG-1")

    def test_an_empty_source_order_no_falls_back_to_the_target_ref(self):
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-FB-1", status="IN_PROGRESS", game_name=GAME,
            source_order_no="",
        )])
        self.assertEqual(
            PeerOrderLink.objects.get(peer_order_no="SYN-P4-FB-1").origin_order_no, "SYN-P4-FB-1"
        )


class OriginOrderNoShapeTests(TestPeerBase):
    """⭐ 评审必修 1：链身份是**外部 JSON** 给的值 ⇒ 写进类型化的列之前必须过形状闸。

    判据按 `AGENTS.md` §5-E / E-0010 的「**这类请求**还会不会 500」：⛔ 不是只修被点名的那一个
    字段。`payload` 与 `target_ref` 都来自对端请求体（接收端点直接读 `request.data`、本批共用
    一个事务）⇒ 一个畸形值不许把同一批里其它正常的单一起回滚，也不许把 JSON 容器
    `str()` 成 `"[1, 2]"` 这种**静默错身份**。
    """

    def test_a_truthy_non_string_source_order_no_falls_back(self):
        """`123` 之前会 `[:64]` 抛 `TypeError` ⇒ 整批 500；现在必须退回对端单号。"""
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-NS-1", status="IN_PROGRESS", game_name=GAME,
            source_order_no=123,
        )])
        self.assertEqual(
            PeerOrderLink.objects.get(peer_order_no="SYN-P4-NS-1").origin_order_no,
            "SYN-P4-NS-1",
        )

    def test_a_json_list_source_order_no_does_not_become_a_stringified_identity(self):
        """列表能切片、`CharField` 会把它 `str()` 成 `"[1, 2]"` ⇒ ⛔ 静默错身份，必须退回。"""
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-NS-2", status="IN_PROGRESS", game_name=GAME,
            source_order_no=[1, 2],
        )])
        link = PeerOrderLink.objects.get(peer_order_no="SYN-P4-NS-2")
        self.assertEqual(link.origin_order_no, "SYN-P4-NS-2")

    def test_an_over_long_source_order_no_is_clipped_to_the_column_width(self):
        """超长字符串必须按**列宽**截断（宽度从模型元数据取，⛔ 不抄数字）——否则整批回滚。"""
        long_origin = "SYN-P4-LONG-" + "x" * 80
        receive_events(self.upstream_binding, [self._event(
            "order.assign", "SYN-P4-NS-3", status="IN_PROGRESS", game_name=GAME,
            source_order_no=long_origin,
        )])
        link = PeerOrderLink.objects.get(peer_order_no="SYN-P4-NS-3")
        width = PeerOrderLink._meta.get_field("origin_order_no").max_length
        self.assertEqual(link.origin_order_no, long_origin[:width])
        self.assertEqual(len(link.origin_order_no), width)

    def test_a_non_string_target_ref_is_a_bad_shape_skip_not_a_500(self):
        """P4 挂账⑨：`target_ref` 既是**精确查找键**又落**有界列** ⇒ 非字符串硬跳过、零写入。

        ⚠️ P4 首轮这条钉的是"非字符串照样落单"（旧口径把它 `str()` 成 `"12345"` 收下）——
        ⛔ 那正是挂账⑨ 要关掉的**静默错身份**（对端给的是 JSON 数字，却成了一个"单号"），
        所以本用例改成新契约。整类闸的正面网见 `tests/test_peer_p4_residual_gates.py`。
        """
        result = receive_events(self.upstream_binding, [self._event(
            "order.assign", 12345, status="IN_PROGRESS", game_name=GAME)])
        self.assertEqual(result["applied"], 0, result)
        self.assertEqual(result["skipped"], 1, result)
        self.assertEqual(result["skipped_details"][0]["code"], "bad_shape", result)
        self.assertFalse(PeerOrderLink.objects.exists())
        self.assertFalse(Order.objects.exists())


# ── P4 三租户链端到端（A → B → C）+「回传只一级」的否定用例 ────────────
#: 链路上三个**新建**租户的域名 / schema。
#: ⚠️ 取值与其它测试类**刻意不同**：`Domain.domain` 上有唯一约束，撞了 `provision_tenant` 会报错。
#: ⚠️ 后缀 `.test.local` 落在本机 `.env` 的 `DJANGO_ALLOWED_HOSTS` 里（A 那个域名由
#: `TenantTestCase` 自己加白名单）。
CHAIN_DOMAIN_B = "peer-chain-b.test.local"
CHAIN_DOMAIN_C = "peer-chain-c.test.local"
CHAIN_DOMAIN_D = "peer-chain-d.test.local"
CHAIN_SCHEMA_B = "peer_chain_b"
CHAIN_SCHEMA_C = "peer_chain_c"
CHAIN_SCHEMA_D = "peer_chain_d"

#: 同步端点与页面端点的路径（与 `apps/business/urls.py` 的字面量一致）。
CHAIN_BIND_URL = "/api/business/peer-sync/bind/"
CHAIN_INBOUND_URL = "/api/business/peer-sync/inbound/events/"
CHAIN_PAGE_URL = "/api/business/peer-bindings/"
CHAIN_ORDERS_URL = "/api/business/orders"
#: 页面绑定端点的节点门（裁决 R22）；⛔ 逐字写死、不从视图 import（E-0024 的自指断言）。
CHAIN_PAGE_NODE = "system.bind_upstream_system"
#: `/assign/` + `/transition/` 要过的四道门（与 `tests/test_peer_p3_event_ordering.py` 同源）。
CHAIN_NODES = (
    "order.assign_taker",
    "order.submit_payout_price",
    "order.change_status",
    "order.list",
)
#: 两端按**名字**对齐的游戏（裁决 R21：⛔ 不传对端 id）。
CHAIN_GAME = "上下游链夹具游戏"
#: A 那一跳的**本地单号**（合成值）。
#: ⚠️ **必须 20 位数字**：`/assign/` 与 `/transition/` 走 router 生成的详情路由，
#: 而 `OrderViewSet.lookup_value_regex = r"\d{20}"` ⇒ 计划 Task 3 正文里的 `SYN-P4-A-1`
#: 这类单号**根本匹配不到路由**（实测 404）。本文件改用 20 位合成号，
#: 并把「链身份」的判据打在 `CHAIN_A_ORDER_NO` 这个字面量上（判别力不变）。
CHAIN_A_ORDER_NO = "20260101000000000301"


@override_settings(PEER_SYNC_LOCAL_BASE_URL="")
class ChainFixture(TenantTestCase):
    """四个真租户（A = `TenantTestCase` 自带那个 / B / C / D）与 A→B→C→D 那条链的夹具。

    ## 为什么 schema 一律在 `setUpClass` 里建

    用例内建 schema 会与外层事务相冲（Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）
    ——照 `test_peer_binding_api.py::PeerCrossTenantTokenTests` 的先例。
    ⚠️ D（`c_downstream`）只有第三个用例用得到，但 schema 必须与另外两个一起建
    ⇒ 本夹具一次建满四个。

    ## 连接钉法（⛔ 别删）

    跨域名的请求会把连接留在那个租户的 schema 上（`TenantMainMiddleware` 只在请求进来时
    `set_tenant`、**不在响应结束还原**）⇒ 每一跳都用 `try/finally` 把连接钉回来，
    `setUp` 第一行也显式 `set_tenant(self.tenant)`（挡住上一条用例的残留）。

    ## 绑定怎么来的：三条**真握手**（⛔ 不是手写两列令牌）

    B 去绑 A、C 去绑 B、D 去绑 C——每条都驱动下家页面端点（`POST /peer-bindings/`），
    它的 `bind_upstream` 在**传输层**被打桩成一次进程内的真请求（照
    `PeerHandshakeCredentialTests._real_handshake`）。这样两侧的绑定行与两套令牌都按
    裁决 R15 的真实方向落库，下面 `deliver_peer_events` 的每一次投递才真的验得过
    （手写夹具会让"存错了列"这类缺陷从所有断言底下溜过去）。

    ⚠️ `PEER_SYNC_LOCAL_BASE_URL` 必须清空（类级 `override_settings`）：否则本机 `.env`
    里那个值会顶掉 `_local_base_url` 的请求推导，上家存下的"下家域名"就不是本次夹具的域名，
    `deliver_peer_events` 会投到一个不存在的地址。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.b, _ = provision_tenant(
            schema_name=CHAIN_SCHEMA_B, name="上下游链租户 B", domain=CHAIN_DOMAIN_B
        )
        cls.c, _ = provision_tenant(
            schema_name=CHAIN_SCHEMA_C, name="上下游链租户 C", domain=CHAIN_DOMAIN_C
        )
        cls.c_downstream, _ = provision_tenant(
            schema_name=CHAIN_SCHEMA_D, name="上下游链租户 D", domain=CHAIN_DOMAIN_D
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据；不变量（默认 Group / RBAC 目录）
        # 不能依赖 schema 恰好是本次运行刚建的（`--keepdb` 会复用）⇒ 显式再灌一次。
        for tenant in (cls.b, cls.c, cls.c_downstream):
            seed_tenant(tenant)

    def setUp(self):
        # ⚠️ 握手失败限流桶按 schema 记在 cache 里、**跨测试类不清**
        # ⇒ 不清的话本类那三条真握手会被别处灌满的桶拦掉（看起来像握手坏了）。
        cache.clear()
        connection.set_tenant(self.tenant)          # ⚠️ 第一行，别删
        self.a = self.tenant
        self._cs_clients: dict[str, Client] = {}
        # A 的域名由 `TenantTestCase` 自己建（`tenant.test.com`），从它那一行取，⛔ 不抄字面量。
        self._domains = {
            self.a.schema_name: self.domain.domain,
            CHAIN_SCHEMA_B: CHAIN_DOMAIN_B,
            CHAIN_SCHEMA_C: CHAIN_DOMAIN_C,
            CHAIN_SCHEMA_D: CHAIN_DOMAIN_D,
        }
        # A（链的起点）：B 拿它来绑的那个账号；B / C 各自也要一个"被自己下家绑"的账号。
        self.a_bound_account, a_password = self._make_taker_account(
            self.a, "chain-account-a", "pw-chain-a"
        )
        self.b_bound_account, b_password = self._make_taker_account(
            self.b, "chain-account-b", "pw-chain-b"
        )
        self.c_bound_account, c_password = self._make_taker_account(
            self.c, "chain-account-c", "pw-chain-c"
        )
        # 三条真握手：B→A、C→B、D→C（绑定行与令牌按 R15 的方向落库）。
        self._handshake(
            upstream=self.a, downstream=self.b, upstream_account=self.a_bound_account,
            upstream_password=a_password, kefu="chain-page-kefu-b",
        )
        self._handshake(
            upstream=self.b, downstream=self.c, upstream_account=self.b_bound_account,
            upstream_password=b_password, kefu="chain-page-kefu-c",
        )
        self._handshake(
            upstream=self.c, downstream=self.c_downstream,
            upstream_account=self.c_bound_account, upstream_password=c_password,
            kefu="chain-page-kefu-d",
        )

    # ── 夹具内部 ───────────────────────────────────────────────────────
    def _domain_of(self, ctx) -> str:
        return self._domains[ctx.schema_name]

    def _make_taker_account(self, ctx, username, password):
        """在 `ctx` 里建一个**可被指派**的账号 + 一款游戏（合成值）。回 `(user, password)`。"""
        connection.set_tenant(ctx)
        try:
            game, _ = Game.objects.get_or_create(name=CHAIN_GAME)
            user = User.objects.create_user(username=username, password=password)
            # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
            # ⇒ 被指派的账号在**它自己的租户连接内**补授权（专钉：tests/test_order_assign_game_gate.py）。
            grant_games(user, game)
        finally:
            connection.set_tenant(self.tenant)
        return user, password

    def _handshake(self, *, upstream, downstream, upstream_account, upstream_password, kefu):
        """驱动下家的页面端点跑一次**真握手**（照 `_real_handshake` 的手法）。

        `bind_upstream` 被打桩成"进程内真请求"：它打**上家**的 `bind/`，并在 `finally` 里把
        连接钉回**下家**——否则页面端点随后的 `PeerUpstreamBinding` 写入会落进上家的库。
        """
        upstream_domain = self._domain_of(upstream)
        downstream_domain = self._domain_of(downstream)
        # ⭐ 审计批 C2（Z22）：握手声明的本方地址必须 https。⚠️ 类级 `@override_settings`
        # 在 TenantTestCase 上是 no-op（`setUpClass` 不调 super 那一层，见
        # `test_peer_binding_api.PeerHandshakeCredentialTests` 的注释）⇒ 这里用
        # **上下文管理器**包住整次握手。
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
                # 页面绑定端点要节点门（裁决 R22）；`groups` 是 `_grant_nodes` 的落点。
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

    def _cs_client(self, ctx):
        """`ctx` 里一个持 `/assign/` + `/transition/` 所需节点的客服 client（按租户缓存）。"""
        if ctx.schema_name not in self._cs_clients:
            connection.set_tenant(ctx)
            try:
                self._cs_clients[ctx.schema_name] = make_logged_in_client(
                    domain=self._domain_of(ctx),
                    username=f"chain-cs-{ctx.schema_name}",
                    password=f"pw-chain-cs-{ctx.schema_name}",
                    groups=("客服",), nodes=CHAIN_NODES,
                )
            finally:
                connection.set_tenant(self.tenant)
        return self._cs_clients[ctx.schema_name]

    # ── 契约方法──────────────────────────────────────
    def make_order(self, ctx, order_no):
        """在 `ctx` 建一张单（合成夹具值）。⛔ 不建 MAIN 绩效行：`/assign/` 自己会补。"""
        connection.set_tenant(ctx)
        try:
            game, _ = Game.objects.get_or_create(name=CHAIN_GAME)
            return Order.objects.create(
                game=game,
                order_no=order_no,
                status=OrderStatus.UNASSIGNED,
                total_price=Decimal("88.00"),
                hall_price=Decimal("88.00"),
            )
        finally:
            connection.set_tenant(self.tenant)

    def assign(self, ctx, order, user):
        """打真端点 `POST .../assign/`，把单指派给 `user`（业务码必须为 0）。"""
        client = self._cs_client(ctx)
        try:
            resp = client.post(
                f"{CHAIN_ORDERS_URL}/{order.order_no}/assign/",
                json.dumps({"user_id": user.pk}),
                content_type="application/json",
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])

    def transition(self, ctx, order, target):
        """打真端点 `POST .../transition/`（业务码必须为 0）。"""
        client = self._cs_client(ctx)
        try:
            resp = client.post(
                f"{CHAIN_ORDERS_URL}/{order.order_no}/transition/",
                json.dumps({"target": target}),
                content_type="application/json",
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])

    def link(self, ctx, peer_ref):
        """取本租户收到的那条链路行；⛔ 取不到就 `self.fail`（不许回 `None` 让后面静默通过）。"""
        connection.set_tenant(ctx)
        try:
            link = PeerOrderLink.objects.filter(peer_order_no=peer_ref).first()
        finally:
            connection.set_tenant(self.tenant)
        if link is None:
            self.fail(f"{ctx.schema_name} 里没有 peer_order_no={peer_ref!r} 的链路行")
        return link

    def received_order(self, ctx, peer_ref):
        """按链路行取本租户收到的那张单；判据同上（取不到就 `self.fail`）。"""
        link = self.link(ctx, peer_ref)
        connection.set_tenant(ctx)
        try:
            order = Order.objects.filter(pk=link.order_id).first()
        finally:
            connection.set_tenant(self.tenant)
        if order is None:
            self.fail(f"{ctx.schema_name} 里 {peer_ref!r} 那条链路没有对应的订单行")
        return order

    def peer_ref(self, ctx, order):
        """本租户这张单在**对端**那侧的身份键 = 本地下游推送用的 `target_ref`。

        ⭐ 计划 `2026-10-25-peer-source-identity`）起：`enqueue_order_event` 的
        `target_ref = effective_source_order_no(order)`（**来源单号**优先，空才回落内置号）；
        接收侧建单时把对端给的 `target_ref` 抄进 `source_order_no` **与**
        `PeerOrderLink.peer_order_no` ⇒ 本方法就是同一个唯一出处，⛔ 不是无条件的
        `order.order_no`（那正是本线要消灭的「用内置号当跨系统身份」）。
        """
        from apps.business import jidanpu_member

        return jidanpu_member.effective_source_order_no(order)

    def build_three_tier(self):
        """把 A→B→C 跑到「**C 已收到**」为止，回 `(a_order, b_order, c_order)`。

        ⚠️ 两个队列都必须在这里跑（R-P4-10）：投递不在请求路径上（spec §16.6），
        少跑一次，"C 已收到"就是假的——后面所有断言都会变成"没收到"的假红。
        """
        a_order = self.make_order(self.a, CHAIN_A_ORDER_NO)
        self.assign(self.a, a_order, self.a_bound_account)
        self.deliver_peer_events(self.a)                 # A 的待投递队列 → B
        b_order = self.received_order(self.b, self.peer_ref(self.a, a_order))
        self.assign(self.b, b_order, self.b_bound_account)
        self.deliver_peer_events(self.b)                 # B 的待投递队列 → C
        c_order = self.received_order(self.c, self.peer_ref(self.b, b_order))
        return a_order, b_order, c_order

    def reload(self, ctx, order):
        """回**投递完成后**的真实状态（`refresh_from_db`，且连接钉在 `ctx` 上）。"""
        connection.set_tenant(ctx)
        try:
            order.refresh_from_db()
        finally:
            connection.set_tenant(self.tenant)
        return order

    def deliver_peer_events(self, ctx):
        """把 `ctx` **自己**入队的出站事件逐个真投给对端（不启常驻进程）。

        ⚠️ 语义是「跑 `ctx` 这个租户的队列」（R-P4-10）：回传事件入在**发送方**的库里，
        接收侧不走状态漏斗、不会替对端再排一条 ⇒ 传错租户 = 一条都投不出去。

        ⚠️ 投递**真的走 HTTP**：桩打在 `apps.business.peer_worker.push_events`（**被查找的
        那个模块**，E-0029），桩里用 `Client(SERVER_NAME=<对端域名>)` 打对端的接收端点；
        基址与令牌都取库里那些绑定行（与生产同源，⛔ 不是手写的）。
        """
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
            # ⭐ 自证夹具：投递基址必须是本次夹具的四个租户域名之一（否则这条用例在
            #    `.env` 配了 `PEER_SYNC_LOCAL_BASE_URL` 的机器上会"投递成功"到不存在的地址）。
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
            # 与真 `push_events` 的失败姿势同形（业务码非 0 ⇒ `PeerRejected`）。
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
                    # ⛔ 投递失败不许静默：`_attempt` 把预期失败记成 retry/failed 而**不冒泡**
                    #    ⇒ 不在这里钉住 `done` 的话，后面只会说"没收到"，看不出是投递坏了。
                    self.assertEqual(
                        outcome, "done",
                        f"{ctx.schema_name} 的事件 {event_pk} 投递结果是 {outcome!r}（应为 done）",
                    )
            finally:
                connection.set_tenant(self.tenant)


class ThreeTenantChainTests(ChainFixture):
    """三个真租户：A 上家 → B 中间 → C 下家（+ C 自己的下家 D，只验"再推一级"）。
    ⚠️ 建 schema 的夹具照 `test_peer_binding_api.py::PeerCrossTenantTokenTests` 的手法扩一套
    （见 `ChainFixture` 的 docstring）。"""

    def test_the_chain_reaches_c_with_unassigned_and_the_original_identity(self):
        a_order = self.make_order(self.a, CHAIN_A_ORDER_NO)
        self.assign(self.a, a_order, self.a_bound_account)                 # ① A 指派给 B 的绑定账号
        # ⚠️ 投递**不在** `assign` 里（spec §16.6：请求路径只入队）⇒ 这一步必须显式驱动，
        #    否则 B 一条都收不到（计划正文漏写了它）。
        self.deliver_peer_events(self.a)
        b_order = self.received_order(self.b, CHAIN_A_ORDER_NO)            # B 收到
        self.assertEqual(b_order.status, "UNASSIGNED")
        self.assertEqual(
            self.link(self.b, CHAIN_A_ORDER_NO).origin_order_no, CHAIN_A_ORDER_NO
        )

        self.assign(self.b, b_order, self.b_bound_account)                 # ② B 指派给 C 的绑定账号
        self.deliver_peer_events(self.b)
        c_peer_ref = self.peer_ref(self.b, b_order)                        # = B 这张单的单号
        c_order = self.received_order(self.c, c_peer_ref)                  # C 收到
        self.assertEqual(c_order.status, "UNASSIGNED")                     # ③
        self.assertEqual(
            self.link(self.c, c_peer_ref).origin_order_no, CHAIN_A_ORDER_NO
        )

    def test_cs_change_does_not_reach_a(self):
        """回传只一级（业务方 2026-09-27 裁定）：C 改状态 ⇒ B 跟着变，A **一动不动**。"""
        a_order, b_order, c_order = self.build_three_tier()
        self.transition(self.c, c_order, "ISSUE")
        # ⚠️ 回传事件入在**C 自己**的库里（接收侧不走状态漏斗、不替对端再排一条）⇒
        #    这里必须投 **C** 的队列，投 B 的队列一条都投不出去（R-P4-10）。
        self.deliver_peer_events(self.c)
        self.assertEqual(self.reload(self.b, b_order).status, "ISSUE")
        # ⭐ 把 **B 的**队列也抽干，再断言 A 一动不动——否则"B 压根没排回传"与
        #    "B 排了回传但没人投"在只投 C 队列时**取值相同**（接收侧不走状态漏斗是本条的
        #    承重事实；这里让它真的可判）。B 的队列此刻应当是**空的** ⇒ 这一步是 no-op。
        self.deliver_peer_events(self.b)
        # ⭐ A 这边**必须**还停在它自己最后那次动作的结果上（⛔ 不许写成「只要不是 ISSUE 就行」）：
        #    本地 `/assign/` 把 A 那张单改成了 `IN_PROGRESS`（挂钩在 `apply_transition` 之后跑，
        #    同一事实钉在 `tests/test_peer_order_push.py` 的指派挂钩用例上），
        #    而 B 收到回传后一条都不往外排 ⇒ 这条回传到 B 为止。
        self.assertEqual(self.reload(self.a, a_order).status, "IN_PROGRESS")

    def test_the_chain_continues_one_more_hop(self):
        """再接手一次就再推一级：B 那一跳已经是第二棒，本用例对 C 再做一次同样的动作
        （C 把它指派给**自己的**下家绑定账号）⇒ 收到方同样是「未分配」。"""
        a_order, b_order, c_order = self.build_three_tier()
        self.assign(self.c, c_order, self.c_bound_account)
        # ⚠️ 同理：这一步之前一条事件都还没到 D（R-P4-10）。
        self.deliver_peer_events(self.c)
        d_order = self.received_order(self.c_downstream, self.peer_ref(self.c, c_order))
        self.assertEqual(d_order.status, "UNASSIGNED")
        # ⭐ **来源身份口径（计划 `2026-10-25-peer-source-identity`）**：`origin_order_no`
        #    在**新链路**上**不再漂**——每一跳都把自己的 `source_order_no` 写成
        #    `target_ref`（= 上家的来源号），而发送侧送的就是那一列 ⇒ 链上每一跳存到的
        #    都是**最初那一跳**的号，第 4 跳 D 拿到的也是 A 的单号。
        #    ⚠️ **老链路仍漂**（存量不反推，RUNBOOK §3.2f）：升级前建的 `PeerOrderLink`
        #    保持旧值（第 4 跳起是祖辈号）。本跳是**新链路**（本次运行内建的）⇒ 钉不漂。
        #    判别力：把发送侧的 `effective_peer_ref` 改回 `order.order_no`，D 拿到的是
        #    **B 的单号** ⇒ 下面两条一起红。
        d_link = self.link(self.c_downstream, self.peer_ref(self.c, c_order))
        self.assertEqual(d_link.origin_order_no, a_order.order_no)
        self.assertNotEqual(d_link.origin_order_no, b_order.order_no)

