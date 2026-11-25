"""上下游 P4 Task 4：**订单截图共享前缀 + 护栏窄例外**（本期安全核心）。

业务裁定：截图按订单共享——一个订单不管被链上几个租户接过，截图都是**同一套**。
做法：订单截图的对象名落在**共享的、按「链上最初单号」分段**的前缀下（`shared-orders/<origin>/…`），
各租户各持一行 `OrderScreenshot` 指着同一个对象（**字节只存一份**）。护栏
（`apps.core.storage.key_belongs_to_tenant`）为此开**一条窄例外**：共享前缀**只有在
「本租户确实持有这张单」时**才放行，**不给判据一律拒**（fail-closed）。

本文件覆盖四层：

1. `SharedOrderKeyBuilderTests`：新 builder 的形状；
2. `SharedKeyGuardTests`：护栏的**正 / 反 / 无判据 / 老规则不变**四面（含关键词参数的形状钉）；
3. `SharedKeyCrossTenantTests`：⭐ **真·第二个租户**持有不了这张单 ⇒ 共享前缀对它必须拒
   （跨租户否定断言**用订单号 / 对象名指认**，⛔ 不用主键——两个 schema 的主键序列各自从 1 开始）；
4. `SharedOriginResolutionTests` / `SharedKeyTicketWiringTests` / `SharedKeyBlobEndpointTests`：
   「origin 怎么解析」与**接线**（真端点签发的 key 必须被**真的**持有判据认下、本机直传 PUT/GET 也要放行）。

口径出处（控制器裁决，覆盖 brief）：R-P4-2（关键词参数 + 默认拒）、R-P4-12（**每一张**订单截图
都走共享前缀，⛔ 不按「有没有链身份」分叉；
origin 三级兜底 + ⛔ 不许出现空段）、R-P4-13（`media_blob_views` 也要传判据、持有判据在平台面
fail-closed、`profile_views` 的用户媒体**不传**判据）。
⚠️ **R-P4-7 已废除**（D-0316）：群聊二维码也走共享前缀并随订单同步（后到覆盖）。
本文件里与群码相关的用例自 T2/T3/T4/T5 起按新口径断言。

⚠️ 夹具一律**合成值**（`SYN-…` 单号 / `测试…` 名称 / `*.test.invalid` 域名）。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import pathlib
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone as django_timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.media_access import order_screenshot_key_is_held, order_shared_origin
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderScreenshot,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_dispatch import (
    enqueue_order_assigned,
    enqueue_order_grabbed,
    enqueue_order_update,
    enqueue_order_withdrawn,
    enqueue_screenshot_event,
)
from apps.business.peer_inbound import (
    SKIP_BAD_SHAPE,
    SKIP_NOT_YOUR_ORDER,
    SKIP_STALE,
    SKIP_UNKNOWN_ORDER,
    apply_events,
    apply_upstream_events,
)
from apps.business.peer_sync import PEER_ORDER_FIELDS, hash_token, new_token
from apps.core.storage import (
    SHARED_ORDER_PREFIX,
    build_order_key,
    build_shared_order_key,
    get_storage,
    key_belongs_to_tenant,
    key_media_kind,
)
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: `TenantTestCase` 自带租户的域名（与 `make_logged_in_client` 的默认值同源）。
#: 请求必须显式打在这个 host 上，否则 `TenantMainMiddleware` 解析不到 `Domain` ⇒ 404。
TENANT_DOMAIN = "tenant.test.com"
#: 第二个租户（跨租户否定面）——合成值，刻意与别的测试文件的 schema 不同名。
OTHER_SCHEMA = "peer_p4_media_b"
OTHER_DOMAIN = "peer-p4-media-b.test.local"

#: 截图上传 / 查看 / 管理三个 RBAC 节点（与 `test_order_screenshots_api.py` 同口径）。
UPLOAD_NODE = "order.upload_screenshot"
VIEW_NODE = "order.view_screenshot"
MANAGE_NODE = "order.screenshot_manage"


def _holder_yes(_key: str) -> bool:
    """「本租户持有」的**假**判据：一律点头（只为测护栏的形状，⛔ 不代表任何真实持有关系）。"""
    return True


def _holder_no(_key: str) -> bool:
    return False


class SharedOrderKeyBuilderTests(SimpleTestCase):
    """新 builder 的形状（brief Step 1 的期待值逐字成立）。"""

    def test_the_builder_shape_with_kind(self):
        self.assertEqual(
            build_shared_order_key("SYN-P4-ORIG-1", "jpg", random_part="abc", kind="qr"),
            "shared-orders/SYN-P4-ORIG-1/qr.abc.jpg",
        )

    def test_the_builder_shape_without_kind(self):
        self.assertEqual(
            build_shared_order_key("SYN-P4-ORIG-1", "jpg", random_part="abc"),
            "shared-orders/SYN-P4-ORIG-1/abc.jpg",
        )

    def test_the_extension_is_lowercased(self):
        self.assertEqual(
            build_shared_order_key("SYN-P4-ORIG-1", "PNG", random_part="abc"),
            "shared-orders/SYN-P4-ORIG-1/abc.png",
        )

    def test_the_builder_generates_a_fresh_random_part_by_default(self):
        first = build_shared_order_key("SYN-P4-ORIG-1", "jpg")
        second = build_shared_order_key("SYN-P4-ORIG-1", "jpg")
        self.assertNotEqual(first, second)
        # 形状钉住：`uuid.uuid4().hex[:12]` ⇒ 12 位小写十六进制（与 `build_order_key` 同口径）
        for key in (first, second):
            self.assertRegex(key, r"^shared-orders/SYN-P4-ORIG-1/[0-9a-f]{12}\.jpg$")

    def test_the_shared_prefix_constant_is_the_first_segment(self):
        """前缀常量的**外部契约值**是 `shared-orders`（⛔ 不是从函数派生，避免自指断言）。"""
        self.assertEqual(SHARED_ORDER_PREFIX, "shared-orders")
        self.assertEqual(
            build_shared_order_key("SYN-P4-ORIG-1", "jpg", random_part="abc").split("/")[0],
            "shared-orders",
        )

    def test_a_shared_key_is_still_classified_as_an_order_key(self):
        """共享 key 仍走**订单截图**那条 18 MiB 上限（`key_media_kind` 的第 2 段不是 `users`）。"""
        self.assertEqual(
            key_media_kind(build_shared_order_key("SYN-P4-ORIG-1", "jpg")), "order"
        )

    def test_a_hostile_shared_origin_still_classifies_as_an_order_key(self):
        """⭐ Minor 1：共享前缀的**第 2 段是对端可控的 origin** ⇒ 仍必须判成订单截图（18 MiB）。

        判别力：守卫只看第 2 段时，对端把 `source_order_no` 填成 `users`（⇒ key 形如
        `shared-orders/users/abc.jpg`）就能把**本租户**这张单的本机直传上限从 18 MiB
        静默压到 10 MB。判据必须是「归一化首段 = 共享前缀」这个**类**，⛔ 不是去限制 origin。
        """
        for key in (
            "shared-orders/users/abc.jpg",
            "shared-orders/users/1/abc.jpg",
            build_shared_order_key("users", "jpg", random_part="abc"),
        ):
            self.assertEqual(key_media_kind(key), "order", key)
        # benign 形状不受这条短路影响：第 2 段真的是 `users` 时仍是用户媒体。
        for key in ("demo/users/1/avatar.abc.jpg", "demo/users/1/wechat-qr.abc.png"):
            self.assertEqual(key_media_kind(key), "user", key)


class SharedKeyGuardTests(SimpleTestCase):
    """护栏的四面：持有时放行 / 无判据拒 / 判据说不拒 / 老规则一字不变。"""

    def test_a_shared_key_passes_only_with_a_holding_checker(self):
        key = build_shared_order_key("SYN-P4-SHARED-1", "jpg", random_part="abc")
        self.assertTrue(
            key_belongs_to_tenant(key, "any-tenant-schema", shared_holder=_holder_yes)
        )

    def test_without_a_holder_checker_the_shared_prefix_is_denied(self):
        """⭐ **fail-closed**：调用方不给「持有关系」判据时，共享前缀**一律拒**。"""
        self.assertFalse(
            key_belongs_to_tenant("shared-orders/SYN-P4-SHARED-1/abc.jpg", "demo")
        )

    def test_a_holder_that_says_no_is_denied(self):
        key = build_shared_order_key("SYN-P4-SHARED-1", "jpg", random_part="abc")
        self.assertFalse(key_belongs_to_tenant(key, "demo", shared_holder=_holder_no))

    def test_the_holder_receives_the_exact_key(self):
        """判据必须拿到**被问的那个 key**（⛔ 不是被忽略的参数）。"""
        seen: list[str] = []
        key = build_shared_order_key("SYN-P4-SHARED-1", "jpg", random_part="abc")

        def holder(k: str) -> bool:
            seen.append(k)
            return True

        self.assertTrue(key_belongs_to_tenant(key, "demo", shared_holder=holder))
        self.assertEqual(seen, [key])

    def test_the_holder_parameter_is_keyword_only(self):
        """裁决 R-P4-2：第三参数是**关键词参数** ⇒ 位置传参必须 TypeError（防误用）。"""
        with self.assertRaises(TypeError):
            key_belongs_to_tenant("shared-orders/x/a.jpg", "demo", _holder_yes)

    def test_dotdot_shaped_shared_keys_are_denied_even_with_a_yes_holder(self):
        """`..` 判据**先于**例外：持有判据点头也不能把逃逸形状救回来。"""
        for bad in (
            "shared-orders/../victim/abc.jpg",
            "shared-orders/SYN-P4-SHARED-1/../../victim/abc.jpg",
            "shared-orders/SYN-P4-SHARED-1/./../victim/abc.jpg",
        ):
            with self.subTest(key=bad):
                self.assertFalse(
                    key_belongs_to_tenant(bad, "demo", shared_holder=_holder_yes), bad
                )

    def test_the_old_prefix_rule_still_holds_for_normal_keys(self):
        """回归：非共享前缀的对象名**仍**要求首段等于本租户，且**不受**持有判据影响。"""
        self.assertFalse(key_belongs_to_tenant("other-tenant/orders/O/1.jpg", "demo"))
        self.assertTrue(key_belongs_to_tenant("demo/orders/O/1.jpg", "demo"))
        # 给了判据也**不**改变普通前缀的判据（两个方向都钉）
        self.assertFalse(
            key_belongs_to_tenant(
                "other-tenant/orders/O/1.jpg", "demo", shared_holder=_holder_yes
            )
        )
        self.assertTrue(
            key_belongs_to_tenant("demo/orders/O/1.jpg", "demo", shared_holder=_holder_no)
        )

    def test_empty_and_malformed_keys_are_still_denied(self):
        for bad in ("", ".", "/"):
            with self.subTest(key=bad):
                self.assertFalse(
                    key_belongs_to_tenant(bad, "demo", shared_holder=_holder_yes), bad
                )


class _OrderFixtureMixin:
    """建一张本租户的订单 + 一个上传人（夹具一律合成值）。"""

    ORDER_NO = "20260927000000000401"

    def _make_order_and_user(self, *, order_no: str | None = None) -> None:
        # ⚠️ `self.tenant` 是 Tenant **模型实例**（`TenantTestCase` 的），不是 schema 名
        #    ⇒ 需要 schema 名的地方一律取 `connection.schema_name`。
        self.schema = connection.schema_name
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no=order_no or self.ORDER_NO,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        self.kefu = User.objects.create_user(
            username="syn-p4-shared-media-kefu", password="pw-syn-p4"
        )


class SharedKeyCrossTenantTests(_OrderFixtureMixin, TenantTestCase):
    """⭐ 真正的安全判据：**第二个租户**没有这张单 ⇒ 共享前缀对它必须拒。

    ⚠️ **第二个租户必须在 `setUpClass` 里建**（用例内建 schema 会与外层事务相冲）。
    ⚠️ 跨 schema 调用一律 `try/finally` 把连接钉回本租户：`connection.set_tenant` 不会自己还原。
    ⚠️ 断言用**对象名 K**指认那一行，⛔ 不用主键——两个 schema 的主键序列各自从 1 开始。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.other_tenant, _ = provision_tenant(
            schema_name=OTHER_SCHEMA, name="截图共享隔离租户乙", domain=OTHER_DOMAIN
        )
        # `provision_tenant` 只在**新建** schema 时灌基线；不变量不能依赖「恰好本次刚建」。
        seed_tenant(cls.other_tenant)

    def setUp(self):
        connection.set_tenant(self.tenant)          # ⚠️ 第一行：挡住上一条用例的残留
        self._make_order_and_user()
        self.key = build_shared_order_key(self.order.order_no, "jpg", random_part="abc")
        OrderScreenshot.objects.create(
            order=self.order,
            object_key=self.key,
            uploaded_by=self.kefu,
            status=OrderScreenshot.STATUS_READY,
        )

    def _holds(self, *, tenant) -> bool:
        """在指定租户的上下文里跑**真的**持有判据 + 护栏。"""
        try:
            connection.set_tenant(tenant)
            return key_belongs_to_tenant(
                self.key, tenant.schema_name, shared_holder=order_screenshot_key_is_held
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_this_tenant_holds_the_order(self):
        """⚠️ **正向对照**：没有它，「对面为 False」可能由「这条路径对谁都不工作」凑出来。"""
        self.assertTrue(self._holds(tenant=self.tenant))

    def test_a_tenant_that_does_not_hold_the_order_is_rejected(self):
        """⭐ 否定面（本期安全核心的判据）：乙租户库里没有这一行 ⇒ 共享前缀对它**拒**。"""
        self.assertFalse(self._holds(tenant=self.other_tenant))

    def test_the_real_holder_says_false_for_a_bare_shared_prefix_key(self):
        """M-d：`shared-orders` 这个**光秃秃的前缀本身**不是任何一行的对象名 ⇒ 真判据回 False。

        判别力：把持有判据写成「key 以共享前缀开头就回 True」⇒ 本条立刻红。
        """
        self.assertFalse(order_screenshot_key_is_held(SHARED_ORDER_PREFIX))
        self.assertFalse(order_screenshot_key_is_held(f"{SHARED_ORDER_PREFIX}/"))


class HolderFailClosedOnThePublicPlaneTests(_OrderFixtureMixin, TenantTestCase):
    """裁决 R-P4-13：「持有判据」在**平台面**必须 fail-closed。

    `apps.business` 只在 `TENANT_APPS` 里，而两个平面共用同一份 urlconf ⇒ 平台面上查它的表会
    `relation does not exist` ⇒ 500。所以持有判据必须**在碰任何业务模型之前**回 False。
    """

    def setUp(self):
        connection.set_tenant(self.tenant)
        self._make_order_and_user()

    def test_the_holder_returns_false_on_the_public_plane(self):
        from django_tenants.utils import get_public_schema_name, schema_context

        with schema_context(get_public_schema_name()):
            self.assertEqual(connection.schema_name, get_public_schema_name())
            # 没有那条守卫时，这里的查询会抛 ProgrammingError（本行以 ERROR 红）
            self.assertFalse(
                order_screenshot_key_is_held(
                    build_shared_order_key("SYN-P4-ORIG-1", "jpg", random_part="abc")
                )
            )


class SharedOriginResolutionTests(_OrderFixtureMixin, TenantTestCase):
    """origin 的三级兜底（裁决 R-P4-12）与「⛔ 不许出现空段」。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        self._make_order_and_user()
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _link(self, *, peer_order_no: str, origin_order_no: str | None = None) -> PeerOrderLink:
        kwargs = {"order": self.order, "upstream_binding": self.upstream_binding,
                  "peer_order_no": peer_order_no}
        if origin_order_no is not None:
            kwargs["origin_order_no"] = origin_order_no
        return PeerOrderLink.objects.create(**kwargs)

    def test_an_order_with_no_link_uses_its_own_order_no(self):
        """链头（或不上链的本地单）没有 `PeerOrderLink` 行 ⇒ 用**自己的单号**当 origin。"""
        self.assertEqual(order_shared_origin(self.order), self.order.order_no)
        key = build_shared_order_key(
            order_shared_origin(self.order), "jpg", random_part="abc"
        )
        self.assertEqual(key, f"shared-orders/{self.order.order_no}/abc.jpg")

    # ── 链头的 origin 也改「来源单号」（计划 §2 第 3 条）──────────────
    def test_a_chain_head_with_a_source_order_no_uses_it_as_the_origin(self):
        """链头单**有来源单号** ⇒ origin 段用来源号（⛔ 不是内置号）。

        为什么必须一致：同一个链路的下游那一行 `origin_order_no` 取的正是**上家的来源号**
        （收向建行时写 `target_ref`）⇒ 链头自己仍用内置号的话，同一条链的图会分在两个段下。
        """
        self.order.source_order_no = "SYN-P4-SRC-ORIG"
        self.order.save(update_fields=["source_order_no"])
        self.assertEqual(order_shared_origin(self.order), "SYN-P4-SRC-ORIG")
        self.assertEqual(
            build_shared_order_key(order_shared_origin(self.order), "jpg", random_part="abc"),
            "shared-orders/SYN-P4-SRC-ORIG/abc.jpg",
        )

    def test_an_unsafe_source_order_no_falls_back_to_the_orders_own_number(self):
        """来源号含 `/`（对端可控）⇒ ⛔ 不许当 origin，回落到本单号（原护栏一字不放宽）。"""
        self.order.source_order_no = "a/b"
        self.order.save(update_fields=["source_order_no"])
        self.assertEqual(order_shared_origin(self.order), self.order.order_no)

    def test_a_link_uses_its_origin_order_no(self):
        self._link(peer_order_no="SYN-P4-PEER-9", origin_order_no="SYN-P4-ORIG-9")
        self.assertEqual(order_shared_origin(self.order), "SYN-P4-ORIG-9")

    def test_a_legacy_link_with_a_blank_origin_falls_back_to_the_peer_order_no(self):
        """P1–P3 老行（迁移前建的、`origin_order_no` 为空）⇒ `peer_order_no` **就是** origin。"""
        self._link(peer_order_no="SYN-P4-PEER-LEGACY", origin_order_no="")
        self.assertEqual(order_shared_origin(self.order), "SYN-P4-PEER-LEGACY")

    def test_the_column_default_is_also_the_legacy_branch(self):
        """不显式传 `origin_order_no`（走列默认 `""`）也走同一条兜底。"""
        self._link(peer_order_no="SYN-P4-PEER-DEFAULT")
        self.assertEqual(order_shared_origin(self.order), "SYN-P4-PEER-DEFAULT")

    def test_the_worst_case_shape_never_yields_an_empty_segment(self):
        """两个单号都空的最坏形状 ⇒ 回落到本单号；⛔ 绝不产出 `shared-orders//x.jpg` 那种空段。"""
        self._link(peer_order_no="", origin_order_no="")
        origin = order_shared_origin(self.order)
        self.assertEqual(origin, self.order.order_no)
        key = build_shared_order_key(origin, "jpg", random_part="abc")
        self.assertNotIn("//", key)
        self.assertEqual([p for p in key.split("/") if p == ""], [])

    def test_a_dotdot_origin_is_skipped_for_the_next_candidate(self):
        """M-c：对端可控的 `source_order_no` 含 `..` ⇒ ⛔ 不许当 origin。

        判别力：`..` 段会让对象名**永远**过不了护栏（`key_belongs_to_tenant` 对 `..` 一字不让）
        ⇒ 整条链的截图从此再也传不上去（跨租户 DoS）。
        """
        self._link(peer_order_no="SYN-P4-PEER-SAFE", origin_order_no="../victim")
        origin = order_shared_origin(self.order)
        self.assertEqual(origin, "SYN-P4-PEER-SAFE")
        self.assertNotIn("..", origin)

    def test_a_slash_origin_is_skipped_for_the_next_candidate(self):
        """含 `/` 的候选会凭空多出一段（把图放进别的命名空间）⇒ 同样跳过。"""
        self._link(peer_order_no="SYN-P4-PEER-SAFE-2", origin_order_no="a/b")
        self.assertEqual(order_shared_origin(self.order), "SYN-P4-PEER-SAFE-2")

    def test_blank_candidates_all_fall_through_to_the_orders_own_number(self):
        """空白 / 空串候选一律跳过；最坏情况落回本单号（系统生成、非空、无空段）。"""
        self._link(peer_order_no="   ", origin_order_no="  ")
        origin = order_shared_origin(self.order)
        self.assertEqual(origin, self.order.order_no)
        key = build_shared_order_key(origin, "jpg", random_part="abc")
        self.assertNotIn("//", key)
        self.assertEqual([p for p in key.split("/") if p == ""], [])

    def test_a_preferred_link_wins_over_a_legacy_blank_link(self):
        """一张单同时有上家 / 下家两条链行时（spec §14-R4），**有 origin 的那条**优先。"""
        self._link(peer_order_no="SYN-P4-PEER-LEGACY", origin_order_no="")
        downstream_user = User.objects.create_user(
            username="syn-p4-shared-media-down", password="pw-syn-p4"
        )
        downstream_binding = PeerDownstreamBinding.objects.create(
            account=downstream_user,
            peer_base_url="https://peer-c.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        PeerOrderLink.objects.create(
            order=self.order,
            downstream_binding=downstream_binding,
            peer_order_no="SYN-P4-PEER-DOWN",
            origin_order_no="SYN-P4-ORIG-DOWN",
        )
        self.assertEqual(order_shared_origin(self.order), "SYN-P4-ORIG-DOWN")


class SharedKeyTicketWiringTests(_OrderFixtureMixin, TenantTestCase):
    """⭐ 接线：真端点签发的 key 必须落在共享前缀下、且被**真的**持有判据认下。"""

    ORDER_NO = "20260927000000000402"

    def setUp(self):
        connection.set_tenant(self.tenant)
        self._make_order_and_user()
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )
        self.client = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="syn-p4-shared-media-admin",
            groups=("系统管理员",),
            nodes=(UPLOAD_NODE, VIEW_NODE, MANAGE_NODE),
        )
        self.url_ticket = f"/api/business/orders/{self.order.order_no}/screenshots/ticket"

    def _ticket(self, img_type: int = 0, ext: str = "jpg"):
        return self.client.post(
            self.url_ticket, {"ext": ext, "img_type": img_type}, format="json"
        )

    def test_an_order_screenshot_ticket_uses_the_shared_prefix_and_is_held(self):
        resp = self._ticket()
        self.assertEqual(resp.data["code"], 0, resp.data)
        key = resp.data["data"]["key"]
        # ⛔ 钉住**前缀段与 origin 段**，不许弱化成 startswith("shared-orders/")：
        #    这里是「没有链行 ⇒ origin = 本单号」那一支的端到端判据。
        self.assertTrue(
            key.startswith(f"shared-orders/{self.order.order_no}/"), key
        )
        self.assertTrue(key.endswith(".jpg"), key)
        row = OrderScreenshot.objects.get(pk=resp.data["data"]["screenshot_id"])
        self.assertEqual(row.object_key, key)
        # ⭐ **真的**持有判据认它（不是 fake）——这条把护栏与真正的 checker 绑在一起
        self.assertTrue(order_screenshot_key_is_held(key))

    def test_a_ticket_for_a_peer_order_uses_the_chain_identity(self):
        """有一条带 origin 的链行 ⇒ 签发直接落在那条链身份下（链上各租户共用一段）。"""
        PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no="SYN-P4-PEER-9",
            origin_order_no="SYN-P4-ORIG-9",
        )
        resp = self._ticket()
        self.assertEqual(resp.data["code"], 0, resp.data)
        key = resp.data["data"]["key"]
        self.assertTrue(key.startswith("shared-orders/SYN-P4-ORIG-9/"), key)
        self.assertTrue(order_screenshot_key_is_held(key))

    def test_a_legacy_peer_order_ticket_uses_the_peer_order_no_as_origin(self):
        """老链行（origin 为空）⇒ origin 用 `peer_order_no`，且**没有空段**。"""
        PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no="SYN-P4-PEER-LEGACY",
            origin_order_no="",
        )
        resp = self._ticket()
        self.assertEqual(resp.data["code"], 0, resp.data)
        key = resp.data["data"]["key"]
        self.assertTrue(key.startswith("shared-orders/SYN-P4-PEER-LEGACY/"), key)
        self.assertNotIn("//", key)

    def test_a_group_qr_ticket_uses_the_shared_prefix(self):
        """⭐ D-0316：群聊二维码与订单截图一样落**共享前缀**（它随订单同步），按链身份分段。

        ⚠️ 旧口径（R-P4-7「群码留在本租户前缀」）**已废除**——业务方：
        「之间同步就行，覆盖很正常，一个订单一般只有一个群二维码」。
        """
        resp = self._ticket(img_type=1)
        self.assertEqual(resp.data["code"], 0, resp.data)
        key = resp.data["data"]["key"]
        self.assertTrue(
            key.startswith(f"shared-orders/{self.order.order_no}/qr."), key
        )
        self.assertTrue(key.endswith(".jpg"), key)
        # 共享前缀**必须**配持有判据才放行（护栏 fail-closed 那一条不变）
        self.assertTrue(
            key_belongs_to_tenant(
                key, self.schema, shared_holder=order_screenshot_key_is_held
            )
        )
        self.assertFalse(key_belongs_to_tenant(key, self.schema))
        self.assertTrue(order_screenshot_key_is_held(key))


class SharedKeyBlobEndpointTests(_OrderFixtureMixin, TenantTestCase):
    """本机直传 PUT / GET 也要放行共享 key（裁决 R-P4-13）。

    本机 / CI 下浏览器拿到的是**后端自己的** `/api/media/blob/<key>`（没有签名、没有时效），
    读写两侧共用 `MediaBlobView._belongs_to_current_tenant` ⇒ 它不传持有判据的话，
    本机的订单截图**传不上去也显示不出来**（403）。
    """

    ORDER_NO = "20260927000000000403"

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)
        self._make_order_and_user()
        self.client = make_logged_in_client(
            domain=TENANT_DOMAIN, username="syn-p4-shared-media-blob"
        )
        self.held_key = build_shared_order_key(
            self.order.order_no, "jpg", random_part="held"
        )
        OrderScreenshot.objects.create(
            order=self.order,
            object_key=self.held_key,
            uploaded_by=self.kefu,
            status=OrderScreenshot.STATUS_PENDING,
        )

    def _url(self, key: str) -> str:
        return f"/api/media/blob/{key}"

    def test_put_and_get_a_shared_key_this_tenant_holds(self):
        body = b"\xff\xd8\xffshared-media"
        put = self.client.put(self._url(self.held_key), data=body, content_type="image/jpeg")
        self.assertEqual(put.status_code, 200)
        self.assertEqual(get_storage().head(self.held_key), (len(body), "image/jpeg"))

        got = self.client.get(self._url(self.held_key))
        self.assertEqual(got.status_code, 200)
        self.assertEqual(b"".join(got.streaming_content), body)

    def test_put_a_shared_key_this_tenant_does_not_hold_is_403(self):
        """否定面：本租户没有这一行 ⇒ 共享前缀对本租户**也**拒（不是「凡共享前缀一律放行」）。"""
        unheld = build_shared_order_key(
            "SYN-P4-SOMEBODY-ELSE", "jpg", random_part="nope"
        )
        put = self.client.put(self._url(unheld), data=b"hello", content_type="image/jpeg")
        self.assertEqual(put.status_code, 403)
        self.assertIsNone(get_storage().head(unheld))

    def test_get_a_shared_key_this_tenant_does_not_hold_is_403(self):
        unheld = build_shared_order_key(
            "SYN-P4-SOMEBODY-ELSE", "jpg", random_part="nope"
        )
        get_storage().put_bytes(unheld, b"secret", content_type="image/jpeg")
        self.assertIsNotNone(get_storage().head(unheld))     # 自证受害对象**真的**在
        got = self.client.get(self._url(unheld))
        self.assertEqual(got.status_code, 403)

    def test_a_tenant_prefixed_key_still_works_without_any_row(self):
        """老形状的普通上传**不受**这次改动影响（它走前缀那一支、不需要任何行）。"""
        key = build_order_key(self.schema, self.order.order_no, "jpg", random_part="plain")
        put = self.client.put(self._url(key), data=b"\xff\xd8\xffplain", content_type="image/jpeg")
        self.assertEqual(put.status_code, 200)


# ══════════════════════════════════════════════════════════════════════════════
# P4 截图增 / 删在链上广播（各家建 / 删自己的行）
# ══════════════════════════════════════════════════════════════════════════════
# 口径出处（控制器裁决，覆盖 brief）：
# - R-P4-15：**两个接收支**（`apply_events` 下行 / `apply_upstream_events` 上行）都要有截图分支，
#   且必须**绕过状态单调性闸**（`last_applied_at` / `local_action_at` 的陈旧裁决），也⛔ **不许推进**
#   那两列；幂等靠 key（add 不建第二行、delete 对不存在的 key 是**良性 no-op**）；
#   本地没有那张单 ⇒ `unknown_order` 硬跳过。
# - R-P4-16：接收侧形状闸——`object_key` 必须是非空 `str`、归一化后首段 ==
#   `SHARED_ORDER_PREFIX`；`img_type` 必须是 0。其余一律 `bad_shape`、⛔ 一行都不建。
# - R-P4-17：`add` 在**认领成功之后**入队（⛔ 不在签发里、⛔ 任何拒绝路径都不入队）；
#   `delete` 在删行**之前**入队、且只对 `img_type=0`；接收侧⛔ **永不转发**（无 relay）；
#   `uploaded_by` 取本单 `sales_user`，为空时回落绑定上的本地用户（⛔ 绝不写 None）。
# - R-P4-21：入队接口形状是 `enqueue_screenshot_event(order, *, kind, object_key, img_type=0,
#   remark="")`（brief 的 Interfaces 行写的是另一个形状，以 Step 3 为准）。
# ⚠️ 夹具一律**合成值**（`SYN-P4-*` 单号 / `测试*` 名称 / `*.test.invalid` 域名）。

#: 广播夹具的合成单号（20 位，与真单号形状一致）。
SHOT_ORDER_NO = "20260927000000000411"
#: 本地这张单在**对端**那侧的单号（发送下游时当 `target_ref`）。
SHOT_PEER_REF = "SYN-P4-SHOT-PEER-1"
#: 链身份（共享前缀的分段依据）。
SHOT_ORIGIN = "SYN-P4-SHOT-ORIG-1"
#: 事件时刻基准（合成值）。
SHOT_BASE_TIME = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)


def _shot_event(kind, object_key, *, target_ref=None, img_type=0, remark="", occurred_at=None):
    """Step 1b 的 `_shot_event` 契约：事件级字段在顶层，截图字段进 `payload`。

    ⚠️ `object_key` / `img_type` / `remark` 走 **`payload`**（⛔ 不进 `PEER_ORDER_FIELDS`）；
    `occurred_at` **必须存在**（对端总带），但接收侧**不用它**做新旧裁决（R-P4-15.3）。
    """
    return {
        "kind": kind,
        "target_ref": target_ref or SHOT_PEER_REF,
        "occurred_at": (occurred_at or SHOT_BASE_TIME).isoformat(),
        "payload": {"object_key": object_key, "img_type": img_type, "remark": remark},
    }


class ScreenshotBroadcastFixture(TenantTestCase):
    """链上「两边都在」的一张本地单：上游绑定 + 当前 MAIN 接手人有生效的下游绑定。

    ⚠️ 两个方向**都要有**才测得出「两边都发」；只有一边时另一方向的入队是 no-op，
    断言 `{upstream, downstream}` 会假红/假绿分不清。
    """

    #: 共享前缀下的一张图（合成对象名）；下游 / 上游两个接收支都用它。
    KEY = f"shared-orders/{SHOT_ORIGIN}/abc.jpg"

    def setUp(self):
        connection.set_tenant(self.tenant)          # ⚠️ 第一行，挡住上一条用例的残留
        self.schema = connection.schema_name
        self.game = Game.objects.create(name="测试截图广播游戏")
        self.kefu = User.objects.create_user(
            username="syn-p4-shot-kefu", password="pw-syn-p4"
        )
        self.taker = User.objects.create_user(
            username="syn-p4-shot-taker", password="pw-syn-p4"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室丙",
            peer_base_url="https://peer-shot-up.test.invalid",
            peer_account="peer-account-shot",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-shot-local.test.invalid",
            bound_by=self.kefu,
        )
        self.downstream_binding = PeerDownstreamBinding.objects.create(
            account=self.taker,
            peer_base_url="https://peer-shot-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=SHOT_ORDER_NO,
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
            sales_user=self.kefu,
        )
        Performance.objects.create(
            order=self.order, user=self.taker, emp_type=EmpType.MAIN
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no=SHOT_PEER_REF,
            origin_order_no=SHOT_ORIGIN,
        )


class ScreenshotBroadcastTests(ScreenshotBroadcastFixture):
    """入队两个方向 / 下行接收支 / 形状闸 / 幂等 / 良性 no-op / 无转发。"""

    # ── 入队：两个方向都发（载荷走 payload）─────────────────────────────
    def test_adding_a_screenshot_broadcasts_to_both_sides(self):
        made = enqueue_screenshot_event(
            self.order, kind="order.screenshot.add", object_key=self.KEY, remark="第一张"
        )
        self.assertEqual(len(made), 2, made)
        rows = PeerSyncEvent.objects.filter(kind="order.screenshot.add")
        self.assertEqual(rows.count(), 2)
        self.assertEqual({r.binding_role for r in rows}, {"upstream", "downstream"})
        by_role = {r.binding_role: r for r in rows}
        self.assertEqual(by_role["downstream"].target_ref, self.order.order_no)
        self.assertEqual(by_role["upstream"].target_ref, SHOT_PEER_REF)
        for row in rows:
            self.assertEqual(row.direction, "out")
            self.assertEqual(row.payload["object_key"], self.KEY)
            self.assertEqual(row.payload["img_type"], 0)
            self.assertEqual(row.payload["remark"], "第一张")
            self.assertIn("occurred_at", row.meta)

    def test_the_new_screenshot_fields_are_not_in_the_order_field_whitelist(self):
        """`object_key` / `img_type` 是**截图事件的载荷**，⛔ 不进订单字段契约。

        ⚠️ `remark` 不在此列：它本来就是订单白名单里的字段（订单备注）；截图备注走的是
        同一个键名、由截图分支自己读——两条支各读各的。
        """
        for field in ("object_key", "img_type"):
            self.assertNotIn(field, PEER_ORDER_FIELDS)

    def test_without_a_downstream_binding_only_the_upstream_leg_is_enqueued(self):
        """链尾（没有生效下游绑定）⇒ 只发上行；⛔ 不许因为少一边就整条不发。"""
        self.downstream_binding.state = PeerDownstreamBinding.State.REVOKED
        self.downstream_binding.save(update_fields=["state"])
        made = enqueue_screenshot_event(
            self.order, kind="order.screenshot.delete", object_key=self.KEY
        )
        self.assertEqual([e.binding_role for e in made], ["upstream"])

    def test_without_an_upstream_link_only_the_downstream_leg_is_enqueued(self):
        self.link.delete()
        made = enqueue_screenshot_event(
            self.order, kind="order.screenshot.delete", object_key=self.KEY
        )
        self.assertEqual([e.binding_role for e in made], ["downstream"])

    # ── 下行接收支：建行 / 删行 / 幂等 / 良性 no-op ─────────────────────
    def test_the_receiver_creates_a_row_pointing_at_the_shared_key(self):
        outcome = apply_events(
            [_shot_event("order.screenshot.add", self.KEY, remark="对端备注")],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped_details, [])
        row = OrderScreenshot.objects.get(object_key=self.KEY)
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(row.order.order_no, self.order.order_no)
        self.assertEqual(row.uploaded_by, self.order.sales_user)
        self.assertEqual(row.remark, "对端备注")

    def test_a_delete_removes_everyones_row(self):
        apply_events(
            [_shot_event("order.screenshot.add", self.KEY)], binding=self.upstream_binding
        )
        self.assertTrue(OrderScreenshot.objects.filter(object_key=self.KEY).exists())
        apply_events(
            [_shot_event("order.screenshot.delete", self.KEY)], binding=self.upstream_binding
        )
        self.assertFalse(OrderScreenshot.objects.filter(object_key=self.KEY).exists())

    def test_a_replayed_add_is_idempotent(self):
        for _ in range(2):
            apply_events(
                [_shot_event("order.screenshot.add", self.KEY)],
                binding=self.upstream_binding,
            )
        self.assertEqual(OrderScreenshot.objects.filter(object_key=self.KEY).count(), 1)

    def test_a_replayed_add_is_applied_not_skipped(self):
        """幂等那一跳也算 `applied`（⛔ 不进 `skipped_details`）：否则发送侧会退避重投。"""
        apply_events(
            [_shot_event("order.screenshot.add", self.KEY)], binding=self.upstream_binding
        )
        outcome = apply_events(
            [_shot_event("order.screenshot.add", self.KEY)], binding=self.upstream_binding
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped_details, [])

    def test_a_delete_for_an_unknown_key_is_a_benign_noop(self):
        outcome = apply_events(
            [_shot_event("order.screenshot.delete", "shared-orders/SYN-P4-NOPE/x.jpg")],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.skipped_details, [])
        self.assertEqual(outcome.applied, 1, outcome)

    # ── 无转发：接收侧⛔ 永不 re-enqueue（钉死"没有 relay"）────────────
    def test_applying_a_screenshot_event_never_enqueues_another_event(self):
        before = PeerSyncEvent.objects.count()
        apply_events(
            [_shot_event("order.screenshot.add", self.KEY)], binding=self.upstream_binding
        )
        apply_events(
            [_shot_event("order.screenshot.delete", self.KEY)], binding=self.upstream_binding
        )
        self.assertEqual(PeerSyncEvent.objects.count(), before)

    # ── 形状闸（R-P4-16）──────────────────────────────────────────────
    def test_a_malformed_object_key_is_a_bad_shape_skip_and_creates_nothing(self):
        cases = (
            ("", "空串"),
            ("   ", "纯空白"),
            (SHARED_ORDER_PREFIX, "光秃秃的前缀"),
            (f"{SHARED_ORDER_PREFIX}/", "只有前缀与斜杠"),
            ("demo/orders/SYN-P4-SHOT/x.jpg", "本租户前缀不是共享前缀"),
            ("orders/SYN-P4-SHOT/x.jpg", "别的形状"),
            (f"{SHARED_ORDER_PREFIX}/../victim/x.jpg", ".. 逃逸"),
            (123, "非字符串"),
            (None, "缺失"),
            (["shared-orders/a/x.jpg"], "列表"),
        )
        for key, label in cases:
            with self.subTest(label=label):
                outcome = apply_events(
                    [_shot_event("order.screenshot.add", key)],
                    binding=self.upstream_binding,
                )
                self.assertEqual(outcome.applied, 0, outcome)
                self.assertTrue(
                    any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details),
                    outcome,
                )
                self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_a_missing_object_key_is_a_bad_shape_skip(self):
        event = _shot_event("order.screenshot.add", self.KEY)
        del event["payload"]["object_key"]
        outcome = apply_events([event], binding=self.upstream_binding)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_an_over_long_object_key_is_a_bad_shape_skip_not_a_500(self):
        """列宽装不下的 key 会让整批 `value too long` ⇒ 必须挡在形状闸上（E-0010 同族）。"""
        width = OrderScreenshot._meta.get_field("object_key").max_length
        key = f"{SHARED_ORDER_PREFIX}/{SHOT_ORIGIN}/" + "x" * width + ".jpg"
        self.assertGreater(len(key), width)
        outcome = apply_events(
            [_shot_event("order.screenshot.add", key)], binding=self.upstream_binding
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_img_type_one_is_accepted_and_evicts_the_previous_group_qr(self):
        """⭐ D-0316：`img_type=1`（群码）**收**——建行、`img_type=1`，并淘汰本单旧群码。

        ⚠️ 旧口径（"1 ⇒ `bad_shape`，且本租户自己的群码一行不动"）**已废除**：业务方
        2026-10-27 明确接受"对端 add 会覆盖本租户自己那张"（一个订单一般只有一个群二维码）。
        """
        qr_key = build_order_key(
            self.schema, self.order.order_no, "jpg", random_part="qr", kind="qr"
        )
        local_qr = OrderScreenshot.objects.create(
            order=self.order, object_key=qr_key, uploaded_by=self.kefu,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )
        outcome = apply_events(
            [_shot_event("order.screenshot.add", self.KEY, img_type=1)],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped_details, [])
        row = OrderScreenshot.objects.get(object_key=self.KEY)
        self.assertEqual(row.img_type, OrderScreenshot.IMG_GROUP_QR)
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        # 覆盖语义：本租户原来那张 ready 群码被淘汰（行没了；字节：本租户前缀仍删）。
        self.assertFalse(OrderScreenshot.objects.filter(pk=local_qr.pk).exists())
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
                status=OrderScreenshot.STATUS_READY,
            ).count(),
            1,
        )

    def test_an_img_type_outside_zero_and_one_is_rejected(self):
        """`img_type ∉ {0,1}` ⇒ `bad_shape`、⛔ 不建行（含 JSON 的 `false`/`true`/字符串 `"0"`）。"""
        qr_key = build_order_key(
            self.schema, self.order.order_no, "jpg", random_part="qr", kind="qr"
        )
        qr = OrderScreenshot.objects.create(
            order=self.order, object_key=qr_key, uploaded_by=self.kefu,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )
        for img_type in (2, -1, "1", "0", None, True, False):
            with self.subTest(img_type=img_type):
                outcome = apply_events(
                    [_shot_event("order.screenshot.add", self.KEY, img_type=img_type)],
                    binding=self.upstream_binding,
                )
                self.assertEqual(outcome.applied, 0, outcome)
                self.assertTrue(
                    any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details),
                    outcome,
                )
                self.assertFalse(
                    OrderScreenshot.objects.filter(object_key=self.KEY).exists()
                )
                # 形状不对 ⇒ 一行都不建，也⛔ 不动本租户自己那张群码。
                self.assertTrue(OrderScreenshot.objects.filter(pk=qr.pk).exists())

    def test_a_group_qr_key_outside_the_shared_prefix_is_loudly_rejected(self):
        """⭐ 老版本对端发来的**本租户前缀**群码 key ⇒ `bad_shape` **并留痕**、零建行。

        ⛔ 不给非共享前缀单开允许规则（那等于让对端随便插 key 伪造持有关系）。
        "明确拒绝并留痕"= 在 `skipped_details` 里能看到这一条，⛔ 不是静默丢弃。
        """
        legacy_qr_key = (
            f"{self.schema}/orders/{self.order.order_no}/qr.aaaaaaaaaaaa.jpg"
        )
        outcome = apply_events(
            [_shot_event("order.screenshot.add", legacy_qr_key, img_type=1)],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            [(d["code"], d["target_ref"]) for d in outcome.skipped_details],
            [(SKIP_BAD_SHAPE, SHOT_PEER_REF)],
            outcome,
        )
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_a_screenshot_event_for_an_unknown_order_is_a_hard_skip(self):
        for kind in ("order.screenshot.add", "order.screenshot.delete"):
            with self.subTest(kind=kind):
                outcome = apply_events(
                    [_shot_event(kind, self.KEY, target_ref="SYN-P4-NO-SUCH-ORDER")],
                    binding=self.upstream_binding,
                )
                self.assertEqual(outcome.applied, 0, outcome)
                self.assertTrue(
                    any(d["code"] == SKIP_UNKNOWN_ORDER for d in outcome.skipped_details),
                    outcome,
                )
                self.assertEqual(OrderScreenshot.objects.count(), 0)

    # ── 两条独立时间线（R-P4-15.3）────────────────────────────────────
    def test_a_screenshot_event_ignores_the_status_clock_and_does_not_advance_it(self):
        future = django_timezone.now() + timedelta(days=1)   # 比任何事件都新
        self.link.last_applied_at = future
        self.link.local_action_at = future
        self.link.save(update_fields=["last_applied_at", "local_action_at"])
        old = django_timezone.now() - timedelta(days=10)

        outcome = apply_events(
            [_shot_event("order.screenshot.add", self.KEY, occurred_at=old)],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertTrue(OrderScreenshot.objects.filter(object_key=self.KEY).exists())
        self.link.refresh_from_db()
        self.assertEqual(self.link.last_applied_at, future)
        self.assertEqual(self.link.local_action_at, future)

        # 对照：同样「更旧」的**状态**事件仍被诚实判 stale（两条时间线各自独立）。
        stale = apply_events(
            [{
                "kind": "order.update",
                "target_ref": SHOT_PEER_REF,
                "occurred_at": old.isoformat(),
                "payload": {"status": "ISSUE"},
            }],
            binding=self.upstream_binding,
        )
        self.assertTrue(
            any(d["code"] == SKIP_STALE for d in stale.skipped_details), stale
        )

    # ── uploaded_by 回落（⛔ 绝不写 None）─────────────────────────────
    def test_the_created_row_falls_back_to_the_binding_when_the_order_has_no_sales_user(self):
        self.order.sales_user = None
        self.order.save(update_fields=["sales_user"])
        outcome = apply_events(
            [_shot_event("order.screenshot.add", self.KEY)], binding=self.upstream_binding
        )
        self.assertEqual(outcome.applied, 1, outcome)
        row = OrderScreenshot.objects.get(object_key=self.KEY)
        self.assertEqual(row.uploaded_by_id, self.upstream_binding.bound_by_id)
        self.assertIsNotNone(row.uploaded_by_id)


class GroupQrInboundTests(ScreenshotBroadcastFixture):
    """⭐ D-0316：同步进来的群码（`img_type=1`）落库与**覆盖语义**（按到达顺序，后到覆盖先到）。

    业务方原话：「之间同步就行，覆盖很正常，一个订单一般只有一个群二维码」
    ⇒ 同一张单**只留最新一张** ready 群码；旧事件晚到会盖掉新图（**接受**，如实钉住）；
    本端删掉同步来的群码后**不广播删除**（T6 那条另有用例），对端补发会把它建回来（回声复活）。
    """

    def _qr_key(self, name):
        return f"shared-orders/{SHOT_ORIGIN}/qr.{name}.jpg"

    def _local_qr(self, name):
        """本租户**自己**那张 ready 群码（共享前缀：D-0316 之后新群码都落这一层）。"""
        return OrderScreenshot.objects.create(
            order=self.order,
            object_key=self._qr_key(name),
            uploaded_by=self.kefu,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )

    def _add(self, key):
        return apply_events(
            [_shot_event("order.screenshot.add", key, img_type=1)],
            binding=self.upstream_binding,
        )

    def _delete(self, key):
        return apply_events(
            [_shot_event("order.screenshot.delete", key, img_type=1)],
            binding=self.upstream_binding,
        )

    def _ready_qr_rows(self):
        return OrderScreenshot.objects.filter(
            order=self.order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )

    def test_a_new_group_qr_replaces_the_local_one(self):
        """① 本地已有一张 ready 群码 + 收到新的 ⇒ 恰一行、key = 新，旧行没了。"""
        old = self._local_qr("old")
        outcome = self._add(self._qr_key("new"))
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped_details, [])
        rows = self._ready_qr_rows()
        self.assertEqual(rows.count(), 1, list(rows))
        self.assertEqual(rows.get().object_key, self._qr_key("new"))
        self.assertFalse(OrderScreenshot.objects.filter(pk=old.pk).exists())

    def test_a_replayed_group_qr_add_is_idempotent(self):
        """② 同 key 重投 ⇒ 仍一行、算 applied（不建第二行）。"""
        key = self._qr_key("same")
        for _ in range(2):
            outcome = self._add(key)
            self.assertEqual(outcome.applied, 1, outcome)
            self.assertEqual(outcome.skipped_details, [])
        self.assertEqual(self._ready_qr_rows().count(), 1)

    def test_a_late_old_add_overwrites_the_newer_one(self):
        """③ 迟到旧 `add` ⇒ **覆盖成旧图**（钉住"到达顺序"这一裁决，引 D-0316）。

        ⛔ 这不是 bug 而是业务方明确接受的代价（发送侧 `meta.occurred_at` 是**订单**时刻，
        拿它裁决会误杀更新的图 / 还要加列）。
        """
        self.assertEqual(self._add(self._qr_key("new")).applied, 1)
        self.assertEqual(self._add(self._qr_key("old")).applied, 1)
        rows = self._ready_qr_rows()
        self.assertEqual(rows.count(), 1, list(rows))
        self.assertEqual(rows.get().object_key, self._qr_key("old"))

    def test_a_late_delete_for_the_old_key_keeps_the_new_row(self):
        """④ 迟到 `delete(旧 key)` ⇒ 新行仍在（删除支只匹配同 key）。"""
        self.assertEqual(self._add(self._qr_key("old")).applied, 1)
        self.assertEqual(self._add(self._qr_key("new")).applied, 1)
        self.assertEqual(self._delete(self._qr_key("old")).applied, 1)
        rows = self._ready_qr_rows()
        self.assertEqual(rows.count(), 1, list(rows))
        self.assertEqual(rows.get().object_key, self._qr_key("new"))

    def test_the_first_group_qr_add_creates_exactly_one_row(self):
        """⑤ 首次 `add` ⇒ 恰一行（不空、不多）。"""
        outcome = self._add(self._qr_key("first"))
        self.assertEqual(outcome.applied, 1, outcome)
        rows = self._ready_qr_rows()
        self.assertEqual(rows.count(), 1, list(rows))
        self.assertEqual(rows.get().img_type, OrderScreenshot.IMG_GROUP_QR)

    def test_a_failed_create_rolls_the_eviction_back(self):
        """⑥ 建行失败（`IntegrityError`）⇒ **旧行仍在**（淘汰与建行同处外层事务）。

        判据形态：外面套一个 `transaction.atomic()`（= `peer_views` 的批事务），让
        `create` 抛 `IntegrityError` —— 淘汰必须先于建行、且与它同生共死。
        """
        old = self._local_qr("kept")

        with mock.patch.object(
            OrderScreenshot.objects, "create", side_effect=IntegrityError("boom")
        ):
            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    self._add(self._qr_key("new"))

        self.assertTrue(
            OrderScreenshot.objects.filter(pk=old.pk).exists(),
            "建行失败时淘汰必须一起回滚（旧行仍在）",
        )

    def test_an_echoed_add_resurrects_a_locally_deleted_group_qr(self):
        """⑦ 回声复活：本端删掉该行后对端再补发同一 key ⇒ 本端重建一行（业务方已接受）。"""
        key = self._qr_key("echo")
        self.assertEqual(self._add(key).applied, 1)
        OrderScreenshot.objects.filter(object_key=key).delete()
        self.assertEqual(self._ready_qr_rows().count(), 0)
        outcome = self._add(key)
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(self._ready_qr_rows().count(), 1)


class UpstreamScreenshotBranchTests(ScreenshotBroadcastFixture):
    """R-P4-15.1：**上行接收支**（上家收下家传来的截图）也要处理这两个 kind。

    ⚠️ 上行支没有 `PeerOrderLink`（它不建单）⇒ 单号按 `Order.order_no` 找；
    它那条**承重安全判据**（当前 MAIN 接手人 == 这条下游绑定的账号）照旧适用——
    截图行是挂在这张单上的数据，令牌不能拿来往**别人的单**上挂图。
    """

    def _apply(self, kind, key, *, target_ref=None, **kwargs):
        return apply_upstream_events(
            [_shot_event(kind, key, target_ref=target_ref or self.order.order_no, **kwargs)],
            binding=self.downstream_binding,
        )

    def test_the_upstream_branch_adds_and_deletes_the_shared_row(self):
        before_updated = self.order.updated_at
        outcome = self._apply("order.screenshot.add", self.KEY)
        self.assertEqual(outcome.applied, 1, outcome)
        row = OrderScreenshot.objects.get(object_key=self.KEY)
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(row.order.order_no, self.order.order_no)
        self.assertEqual(row.uploaded_by, self.order.sales_user)

        outcome = self._apply("order.screenshot.delete", self.KEY)
        self.assertEqual(outcome.skipped_details, [], outcome)
        self.assertFalse(OrderScreenshot.objects.filter(object_key=self.KEY).exists())
        # ⛔ 不碰订单的状态与「本地动作时刻」代理（截图与状态是两条独立时间线）。
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.order.updated_at, before_updated)

    def test_the_upstream_branch_never_enqueues_another_event(self):
        before = PeerSyncEvent.objects.count()
        self._apply("order.screenshot.add", self.KEY)
        self._apply("order.screenshot.delete", self.KEY)
        self.assertEqual(PeerSyncEvent.objects.count(), before)

    def test_the_upstream_branch_applies_the_same_shape_gate(self):
        outcome = self._apply("order.screenshot.add", "demo/orders/SYN/x.jpg")
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_a_screenshot_event_for_an_unknown_order_is_a_hard_skip(self):
        outcome = self._apply("order.screenshot.add", self.KEY, target_ref="SYN-P4-UP-NOPE")
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_UNKNOWN_ORDER for d in outcome.skipped_details), outcome
        )
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_a_screenshot_event_for_someone_elses_order_is_rejected(self):
        """承重安全判据照旧：令牌只能证明"你是这个下家"，不能证明"这张单是你的"。"""
        other_taker = User.objects.create_user(
            username="syn-p4-shot-other", password="pw-syn-p4"
        )
        other = Order.objects.create(
            game=self.game, order_no="20260927000000000412",
            status=OrderStatus.IN_PROGRESS, sales_user=self.kefu,
        )
        Performance.objects.create(order=other, user=other_taker, emp_type=EmpType.MAIN)
        outcome = self._apply("order.screenshot.add", self.KEY, target_ref=other.order_no)
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_NOT_YOUR_ORDER for d in outcome.skipped_details), outcome
        )
        self.assertFalse(OrderScreenshot.objects.filter(object_key=self.KEY).exists())

    def test_the_upstream_branch_ignores_the_local_action_clock(self):
        future = django_timezone.now() + timedelta(days=1)
        Order.objects.filter(pk=self.order.pk).update(updated_at=future)
        self.order.refresh_from_db()
        self.assertEqual(self.order.updated_at, future)

        outcome = self._apply(
            "order.screenshot.add", self.KEY,
            occurred_at=django_timezone.now() - timedelta(days=10),
        )
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertTrue(OrderScreenshot.objects.filter(object_key=self.KEY).exists())
        self.order.refresh_from_db()
        self.assertEqual(self.order.updated_at, future)

        # 对照：同样更旧的**状态回传**仍被判 stale、订单状态一动不动。
        stale = apply_upstream_events(
            [{
                "kind": "order.update",
                "target_ref": self.order.order_no,
                "occurred_at": (
                    django_timezone.now() - timedelta(days=10)
                ).isoformat(),
                "payload": {"status": "ISSUE"},
            }],
            binding=self.downstream_binding,
        )
        self.assertTrue(any(d["code"] == SKIP_STALE for d in stale.skipped_details), stale)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_the_upstream_branch_falls_back_to_the_binding_account(self):
        """`PeerDownstreamBinding` 上**没有** `bound_by` ⇒ 回落列只能是 `account`（⛔ 不写 None）。"""
        self.order.sales_user = None
        self.order.save(update_fields=["sales_user"])
        self._apply("order.screenshot.add", self.KEY)
        row = OrderScreenshot.objects.get(object_key=self.KEY)
        self.assertEqual(row.uploaded_by_id, self.downstream_binding.account_id)
        self.assertIsNotNone(row.uploaded_by_id)


class ScreenshotTriggerWiringTests(ScreenshotBroadcastFixture):
    """R-P4-17：`add` 只在**认领成功之后**入队；`delete` 在删行前入队、只对 `img_type=0`。"""

    JPEG = b"\xff\xd8\xff" + b"fake-jpeg-body"

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.client = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="syn-p4-shot-wiring",
            groups=("系统管理员",),
            nodes=(UPLOAD_NODE, VIEW_NODE, MANAGE_NODE),
        )
        self.url_ticket = f"/api/business/orders/{self.order.order_no}/screenshots/ticket"
        self.url_collection = f"/api/business/orders/{self.order.order_no}/screenshots"

    def _ticket(self, img_type=0):
        return self.client.post(
            self.url_ticket, {"ext": "jpg", "img_type": img_type}, format="json"
        )

    def _uploaded_row(self, img_type=0, body=None):
        resp = self._ticket(img_type=img_type)
        self.assertEqual(resp.data["code"], 0, resp.data)
        row = OrderScreenshot.objects.get(pk=resp.data["data"]["screenshot_id"])
        get_storage().put_bytes(
            row.object_key, body or self.JPEG, content_type="image/jpeg"
        )
        return row

    def _claim(self, row, **extra):
        payload = {"screenshot_id": row.pk}
        payload.update(extra)
        return self.client.post(self.url_collection, payload, format="json")

    def test_issuing_a_ticket_does_not_enqueue_anything(self):
        self._uploaded_row()
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_a_successful_claim_enqueues_one_add_event_per_direction(self):
        row = self._uploaded_row()
        resp = self._claim(row, original_name="a.jpg", remark="第一张")
        self.assertEqual(resp.data["code"], 0, resp.data)
        rows = PeerSyncEvent.objects.filter(kind="order.screenshot.add")
        self.assertEqual(rows.count(), 2, list(rows))
        self.assertEqual({r.binding_role for r in rows}, {"upstream", "downstream"})
        for event in rows:
            self.assertEqual(event.payload["object_key"], row.object_key)
            self.assertEqual(event.payload["img_type"], 0)
            self.assertEqual(event.payload["remark"], "第一张")

    def test_a_rejected_claim_enqueues_no_event(self):
        """魔数不符（不是图片）⇒ 认领被拒、行与对象都清掉 ⇒ ⛔ 一条都不入队。"""
        row = self._uploaded_row(body=b"this-is-not-an-image")
        resp = self._claim(row)
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertEqual(
            PeerSyncEvent.objects.filter(
                kind__in=("order.screenshot.add", "order.screenshot.delete")
            ).count(),
            0,
        )

    def test_a_group_qr_claim_enqueues_one_add_event_per_direction(self):
        """⭐ D-0316：群码认领**也广播** peer（两个方向各一条、`img_type=1`）。

        ⚠️ 旧口径（"群码不广播"）已废除；⛔ 开放平台 / 打手回调那两句仍只对 `img_type=0`
        ——那条判据在 `tests/test_openapi_order_images.py` 的群码认领用例里（推送仍 0 条）。
        """
        row = self._uploaded_row(img_type=1)
        resp = self._claim(row, remark="第一张群码")
        self.assertEqual(resp.data["code"], 0, resp.data)
        row.refresh_from_db()
        self.assertEqual(row.img_type, OrderScreenshot.IMG_GROUP_QR)
        rows = PeerSyncEvent.objects.filter(kind="order.screenshot.add")
        self.assertEqual(rows.count(), 2, list(rows))
        self.assertEqual({r.binding_role for r in rows}, {"upstream", "downstream"})
        for event in rows:
            self.assertEqual(event.payload["object_key"], row.object_key)
            self.assertEqual(event.payload["img_type"], OrderScreenshot.IMG_GROUP_QR)
            self.assertEqual(event.payload["remark"], "第一张群码")

    def test_the_delete_endpoint_enqueues_a_delete_event_for_an_order_screenshot(self):
        row = self._uploaded_row()
        self.assertEqual(self._claim(row).data["code"], 0)
        key = row.object_key
        resp = self.client.delete(f"/api/business/orders/screenshots/{row.pk}")
        self.assertEqual(resp.data["code"], 0, resp.data)
        rows = PeerSyncEvent.objects.filter(kind="order.screenshot.delete")
        self.assertEqual(rows.count(), 2, list(rows))
        self.assertEqual({r.binding_role for r in rows}, {"upstream", "downstream"})
        for event in rows:
            self.assertEqual(event.payload["object_key"], key)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())

    def test_the_delete_endpoint_drops_the_group_qr_row_but_keeps_shared_bytes(self):
        """⭐ D-0316：下家删掉同步来的群码 ⇒ **本行没了、共享字节仍在、不广播删除**。

        判据三半（缺一条就会漏掉"删了不该删的字节"或"广播了不该广播的删除"）：
        ① 那一行真的没了；② `head()` 仍非 None（共享前缀群码的字节**不删**——B1）；
        ③ `order.screenshot.delete` 事件**仍 0 条**（A 那侧不受影响：覆盖语义已足够）。
        """
        row = self._uploaded_row(img_type=1)
        self.assertEqual(self._claim(row).data["code"], 0)
        key = row.object_key
        self.assertTrue(key.startswith("shared-orders/"), key)
        self.assertIsNotNone(get_storage().head(key), "认领后的群码字节应当在存储里")

        resp = self.client.delete(f"/api/business/orders/screenshots/{row.pk}")

        self.assertEqual(resp.data["code"], 0, resp.data)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNotNone(
            get_storage().head(key), "共享前缀的群码字节不许被删除端点删掉（B1）"
        )
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind="order.screenshot.delete").count(), 0
        )

    def test_a_failing_broadcast_rolls_the_claim_back(self):
        """Minor 1：入队与认领**同一个事务** ⇒ 广播写不进去时不许留下"行已 ready、对端永远不知道"。"""
        row = self._uploaded_row()
        with mock.patch(
            "apps.business.api.screenshot_views.enqueue_screenshot_event",
            side_effect=RuntimeError("广播写不进去"),
        ):
            with self.assertRaises(RuntimeError):
                self._claim(row)
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_PENDING)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_a_failing_delete_broadcast_keeps_the_row(self):
        """Minor 1：`row.delete()` 与入队同一事务 ⇒ ⛔ 不许"行没删掉、删除却已经广播出去"。"""
        row = self._uploaded_row()
        self.assertEqual(self._claim(row).data["code"], 0)
        with mock.patch(
            "apps.business.api.screenshot_views.enqueue_screenshot_event",
            side_effect=RuntimeError("广播写不进去"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.delete(f"/api/business/orders/screenshots/{row.pk}")
        self.assertTrue(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind="order.screenshot.delete").count(), 0
        )

    def test_a_failing_row_delete_rolls_the_broadcast_back(self):
        """Minor 1 的另一半：删行失败 ⇒ 那条**已经写进去的**删除事件也必须一起回滚。

        ⚠️ 判别力：把 `destroy` 里的 `transaction.atomic()` 去掉，事件会留在库里（count=1）
        ⇒ 对端把图删了、本家还留着——正是评审点名的那种半截状态。
        """
        row = self._uploaded_row()
        self.assertEqual(self._claim(row).data["code"], 0)
        with mock.patch.object(
            OrderScreenshot, "delete", side_effect=RuntimeError("删行失败")
        ):
            with self.assertRaises(RuntimeError):
                self.client.delete(f"/api/business/orders/screenshots/{row.pk}")
        self.assertTrue(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind="order.screenshot.delete").count(), 0
        )


class AssignmentBackfillTests(ScreenshotBroadcastFixture):
    """⭐ R-P4-28：接手类事件（派单 / 抢单）把**接手之前就已经认领**的订单截图补发。

    背景（整体评审 Important ①）：`enqueue_screenshot_event` 只在**认领**与**删除**两处被调 ⇒
    上家先传图、后把单派给那个绑了下游的账号时，认领那一刻这张单的接手人还不是它
    ⇒ 下家接手后看到的是**空列表**，与「一个订单不管被链上几个租户接过，截图都是同一套」
    相反，真机验收的截图那一步也会按操作次序假绿 / 假红。

    ⚠️ 本类**删掉上游链行**（`self.link`）：夹具两个方向都在时，一张图会同时产生上行 + 下行
    两条事件，「恰好两条」按 key 数的断言就分不清方向。这里只保留下游那一条。

    ⚠️ 其余口径**一字未动**：`order.update` / `order.withdraw` ⛔ 不补发（另有用例钉住）。
    """

    def setUp(self):
        super().setUp()
        self.link.delete()          # 只留下游方向

    def _row(self, name, *, img_type=OrderScreenshot.IMG_ORDER,
             status=OrderScreenshot.STATUS_READY) -> OrderScreenshot:
        return OrderScreenshot.objects.create(
            order=self.order,
            img_type=img_type,
            object_key=f"shared-orders/{SHOT_ORIGIN}/{name}.jpg",
            uploaded_by=self.kefu,
            status=status,
            remark=f"备注-{name}",
        )

    def _legacy_row(self, name, *, img_type=OrderScreenshot.IMG_ORDER,
                    status=OrderScreenshot.STATUS_READY) -> OrderScreenshot:
        """P4 **之前**上传的老截图：键在**本租户**前缀下（`<租户>/orders/…`），⛔ 不在共享前缀下。

        ⚠️ 形状照 `build_order_key` 的返回行（`<tenant>/orders/<order_no>/<随机>.<ext>`），
        `tenant` 取当前 schema（`self.schema`）——这正是真机验收时那两条被拒事件的形状
        （`demo/orders/<单号>/…`）。
        """
        return OrderScreenshot.objects.create(
            order=self.order,
            img_type=img_type,
            object_key=f"{self.schema}/orders/{self.order.order_no}/legacy-{name}.jpg",
            uploaded_by=self.kefu,
            status=status,
            remark=f"老图备注-{name}",
        )

    def test_assign_backfills_exactly_the_ready_screenshots_and_group_qr(self):
        """⭐ D-0316：恰**三条** = 两张订单截图 + 一张群码；`pending` 一律不发。

        ⚠️ 旧口径（"群码不补发"）已废除：群码随订单同步，"先传群码后派单"与订单截图
        是同一个坑（接手人看不到图）。`pending`（签了票没认领）仍然不发。
        """
        first = self._row("first")
        second = self._row("second")
        pending = self._row("still-pending", status=OrderScreenshot.STATUS_PENDING)
        qr = self._row("group-qr", img_type=OrderScreenshot.IMG_GROUP_QR)
        pending_qr = self._row(
            "pending-qr", img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_PENDING,
        )

        made = enqueue_order_assigned(self.order, self.taker)

        self.assertIsNotNone(made)
        self.assertEqual(made.kind, "order.assign")
        adds = PeerSyncEvent.objects.filter(kind="order.screenshot.add").order_by("pk")
        self.assertEqual(adds.count(), 3, list(adds))
        self.assertEqual(
            [(event.payload["object_key"], event.payload["remark"]) for event in adds],
            [
                (first.object_key, "备注-first"),
                (second.object_key, "备注-second"),
                (qr.object_key, "备注-group-qr"),
            ],
        )
        self.assertNotIn(pending.object_key, [e.payload["object_key"] for e in adds])
        self.assertNotIn(pending_qr.object_key, [e.payload["object_key"] for e in adds])
        self.assertEqual(
            [e.payload["img_type"] for e in adds],
            [OrderScreenshot.IMG_ORDER, OrderScreenshot.IMG_ORDER,
             OrderScreenshot.IMG_GROUP_QR],
        )
        for event in adds:
            self.assertEqual(event.binding_role, "downstream")
            self.assertEqual(event.target_ref, self.order.order_no)

    def test_assign_backfills_only_the_rows_under_the_shared_prefix(self):
        """⭐ 真机验收缺陷（2026-09-27）：补发只发**共享前缀下**的 ready 行（两类图同一套）。

        同一张单上六行齐备：(a) 共享前缀、ready、`img_type=0` ⇒ **发**；
        (b) 老前缀（`<租户>/orders/…`）、ready、`img_type=0` ⇒ **⛔ 不发**——接收侧的形状闸
        （R-P4-16）只认共享前缀，发了必被 `bad_shape` 拒 ⇒ 事件永远重试、涨到自动停绑；
        (c) 共享前缀但 `pending` ⇒ 不发；(d) 共享前缀、ready、`img_type=1`（群码）⇒ **发**
        （⭐ D-0316）。⚠️ **老前缀的 ready 群码**那条在下一个用例里（同单只能有一张 ready
        群码，部分唯一索引 `uniq_order_ready_group_qr` ⇒ 不能与本条的共享群码并存）。

        真机读数（tenant `demo`，派单 `20260924122401729001`）：旧实现发 1 条 `order.assign`
        + **2** 条 `order.screenshot.add`（两条老前缀 key），对端两条都 `bad_shape`。
        """
        shared = self._row("shared-ready")
        legacy = self._legacy_row("legacy-ready-1")
        self._legacy_row("legacy-ready-2")
        self._row("shared-still-pending", status=OrderScreenshot.STATUS_PENDING)
        shared_qr = self._row("shared-group-qr", img_type=OrderScreenshot.IMG_GROUP_QR)

        made = enqueue_order_assigned(self.order, self.taker)

        self.assertIsNotNone(made)
        adds = PeerSyncEvent.objects.filter(kind="order.screenshot.add").order_by("pk")
        self.assertEqual(adds.count(), 2, list(adds))
        self.assertEqual(
            [e.payload["object_key"] for e in adds],
            [shared.object_key, shared_qr.object_key],
        )
        self.assertNotIn(legacy.object_key, [e.payload["object_key"] for e in adds])

    def test_assign_with_only_legacy_screenshots_writes_no_add_event(self):
        """⭐ 负向对照：一张单的 ready 订单截图**全是老前缀** ⇒ **零** `order.screenshot.add`。

        `order.assign` 本身照旧发出（补发被跳过**不影响**订单事件）——真机上那「3 条出站」应当
        收敛成「1 条」。判别力：把前缀筛去掉，本条立刻变成 2 条 `order.screenshot.add`。
        """
        self._legacy_row("legacy-only-1")
        self._legacy_row("legacy-only-2")
        # ⭐ D-0316：老前缀的 ready **群码**同样补发不出去（历史群码不迁移：业务方已确认
        # 要重传一次才会同步）。本单没有共享前缀群码 ⇒ 不撞那部分唯一索引。
        self._legacy_row("legacy-only-qr", img_type=OrderScreenshot.IMG_GROUP_QR)

        made = enqueue_order_assigned(self.order, self.taker)

        self.assertIsNotNone(made)
        self.assertEqual(
            [e.kind for e in PeerSyncEvent.objects.order_by("pk")], ["order.assign"]
        )

    def test_grab_backfills_the_same_way(self):
        """抢单那一档走同一个内核 ⇒ 同样补发（判据与派单是同一条代码路径）。"""
        row = self._row("grabbed")

        enqueue_order_grabbed(self.order, self.taker)

        adds = PeerSyncEvent.objects.filter(kind="order.screenshot.add")
        self.assertEqual([e.payload["object_key"] for e in adds], [row.object_key])

    def test_assign_without_any_screenshot_writes_only_the_order_event(self):
        """⚠️ 没有截图时必须**与改动前逐字相同**：只有那一条 `order.assign`。"""
        enqueue_order_assigned(self.order, self.taker)

        self.assertEqual(
            [(e.kind, e.binding_role) for e in PeerSyncEvent.objects.order_by("pk")],
            [("order.assign", "downstream")],
        )

    def test_an_update_event_never_backfills_screenshots(self):
        """⛔ 只有接手类补发：`order.update` 那一档一行截图事件都不许多出来。"""
        self._row("not-for-update")

        enqueue_order_update(self.order, self.taker)

        self.assertEqual(
            [e.kind for e in PeerSyncEvent.objects.order_by("pk")], ["order.update"]
        )

    def test_a_withdraw_event_never_backfills_screenshots(self):
        """⛔ 撤单那一档同样不补发（与 `order.update` 并列：只有接手类补）。"""
        self._row("not-for-withdraw")

        enqueue_order_withdrawn(self.order, self.taker, "taker_removed")

        self.assertEqual(
            [e.kind for e in PeerSyncEvent.objects.order_by("pk")], ["order.withdraw"]
        )


class AssignmentBackfillReceiverTests(ScreenshotBroadcastFixture):
    """R-P4-28 的另一半：补发出来的那条 `order.screenshot.add` 载荷，接收侧真的吃得下。

    对拍口径照 `ScreenshotContractAndIsolationTests::test_the_payload_the_sender_writes_is_accepted_by_the_receiver`
    （发送侧真载荷 → 接收侧真解析），⛔ 不另造一份形状。
    """

    def test_a_backfilled_add_creates_exactly_one_row(self):
        key = f"shared-orders/{SHOT_ORIGIN}/backfilled.jpg"
        OrderScreenshot.objects.create(
            order=self.order,
            object_key=key,
            uploaded_by=self.kefu,
            status=OrderScreenshot.STATUS_READY,
            remark="接手前就有的图",
        )

        enqueue_order_assigned(self.order, self.taker)

        event = PeerSyncEvent.objects.get(
            kind="order.screenshot.add", binding_role="upstream"
        )
        incoming = {
            "kind": event.kind,
            "target_ref": event.target_ref,
            "occurred_at": event.meta["occurred_at"],
            "payload": event.payload,
        }
        # 发送侧那一行不是接收侧的行（真实链上是两个库）⇒ 先清掉，才测得出「建行」。
        OrderScreenshot.objects.filter(object_key=key).delete()

        first = apply_events([incoming], binding=self.upstream_binding)
        self.assertEqual(first.applied, 1, first)
        created = OrderScreenshot.objects.get(object_key=key)
        self.assertEqual(created.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(created.img_type, OrderScreenshot.IMG_ORDER)
        self.assertEqual(created.remark, "接手前就有的图")
        self.assertEqual(created.order.order_no, self.order.order_no)

        second = apply_events([incoming], binding=self.upstream_binding)
        self.assertEqual(second.applied, 1, second)
        self.assertEqual(OrderScreenshot.objects.filter(object_key=key).count(), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 评审修复（C1 + Minor 1–5）：存储形状闸 / 事务边界 / 单号一致 / 并发 / 契约
# ══════════════════════════════════════════════════════════════════════════════
#: 同步接收端点（与 `apps/business/urls.py` 的字面量一致）。
INBOUND_URL = "/api/business/peer-sync/inbound/events/"


class ScreenshotUnstorableTextTests(ScreenshotBroadcastFixture):
    """⭐ C1 / 裁决 R-P4-23：对端可控的字符串里只要有 **PostgreSQL 存不下**的字节
    （NUL 或孤立代理项），就在碰任何 DB 之前整条跳过 ⇒ `bad_shape`、零写入、**不 500**。

    判别力：没有这条闸时，`OrderScreenshot.objects.filter(object_key=…)` 就会抛
    `DataError`（NUL）/ `UnicodeEncodeError`（代理项）⇒ 端点那个整批事务回滚 + HTTP 500，
    发送侧会永远重投这条毒事件并把绑定推向自动停绑。
    """

    #: 一个 NUL 与一个**孤立代理项**（`json.loads('"\\ud800"')` 的产物）。
    NUL = "\x00"
    SURROGATE = "\ud800"

    def _downstream(self, event):
        return apply_events([event], binding=self.upstream_binding)

    def _upstream(self, event):
        # 上行支按 `Order.order_no` 找单，且要求当前 MAIN 接手人 == 绑定账号（夹具已满足）。
        event["target_ref"] = event.get("target_ref") or self.order.order_no
        return apply_upstream_events([event], binding=self.downstream_binding)

    def _assert_skipped(self, outcome):
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def _toxic_events(self, *, target_ref):
        """每一种"存不下"的形状各造一条（覆盖 key / 备注 / 旧字段 / 嵌套 / target_ref 自己）。"""
        key_with_nul = f"shared-orders/{SHOT_ORIGIN}/a{self.NUL}.jpg"
        key_with_surrogate = f"shared-orders/{SHOT_ORIGIN}/a{self.SURROGATE}.jpg"
        events = [
            ("object_key 里的 NUL", _shot_event(
                "order.screenshot.add", key_with_nul, target_ref=target_ref)),
            ("object_key 里的孤立代理项", _shot_event(
                "order.screenshot.add", key_with_surrogate, target_ref=target_ref)),
            ("备注里的 NUL", _shot_event(
                "order.screenshot.add", self.KEY, target_ref=target_ref,
                remark=f"备注{self.NUL}")),
        ]
        # P1–P3 就有的字段：以前同样能 500（这就是"整类判据"而不是逐字段补丁的理由）。
        old_field = _shot_event("order.screenshot.add", self.KEY, target_ref=target_ref)
        old_field["payload"]["game_name"] = f"游戏{self.NUL}名"
        events.append(("旧字段 game_name 里的 NUL", old_field))
        # 递归：嵌在 dict / list 里也必须被抓到。
        nested = _shot_event("order.screenshot.add", self.KEY, target_ref=target_ref)
        nested["payload"]["content"] = {"deep": [f"x{self.SURROGATE}"]}
        events.append(("嵌套 list 里的孤立代理项", nested))
        # target_ref **自己**中招（响应体也不能回它）。
        events.append(("target_ref 里的 NUL", _shot_event(
            "order.screenshot.add", self.KEY, target_ref=f"{target_ref}{self.NUL}")))
        events.append(("target_ref 里的孤立代理项", _shot_event(
            "order.screenshot.add", self.KEY, target_ref=f"{target_ref}{self.SURROGATE}")))
        return events

    def test_the_downstream_branch_skips_every_unstorable_shape(self):
        for label, event in self._toxic_events(target_ref=SHOT_PEER_REF):
            with self.subTest(label=label):
                OrderScreenshot.objects.all().delete()
                self._assert_skipped(self._downstream(event))

    def test_the_upstream_branch_skips_every_unstorable_shape(self):
        for label, event in self._toxic_events(target_ref=self.order.order_no):
            with self.subTest(label=label):
                OrderScreenshot.objects.all().delete()
                self._assert_skipped(self._upstream(event))

    def test_a_clean_event_still_applies_after_a_toxic_one(self):
        """正向对照：闸门只挡坏串，同批里正常的那条照常落地。"""
        toxic = _shot_event(
            "order.screenshot.add", f"shared-orders/{SHOT_ORIGIN}/a{self.NUL}.jpg"
        )
        good = _shot_event("order.screenshot.add", self.KEY)
        outcome = apply_events([toxic, good], binding=self.upstream_binding)
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 1, outcome)
        self.assertTrue(OrderScreenshot.objects.filter(object_key=self.KEY).exists())


class ScreenshotUnstorableTextEndpointTests(ScreenshotBroadcastFixture):
    """端点面：坏字符串被跳过，而不是把整批打成 HTTP 500（C1 的真实出口）。"""

    def setUp(self):
        super().setUp()
        raw = new_token()
        self.upstream_binding.token_in_hash = hash_token(raw)
        self.upstream_binding.save(update_fields=["token_in_hash"])
        self.raw_token = raw

    def _post(self, events):
        # ⚠️ 测试客户端用 `json.dumps`（`ensure_ascii=True`）编码 ⇒ NUL 变 `\u0000`、
        #    孤立代理项变 `\ud800`，服务端解出来的就是那两个**真的**存不下的字符。
        return Client(SERVER_NAME=TENANT_DOMAIN).post(
            INBOUND_URL, {"events": events}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw_token,
        )

    def test_a_nul_in_target_ref_does_not_500_the_batch(self):
        toxic = _shot_event("order.screenshot.add", self.KEY)
        toxic["target_ref"] = f"{SHOT_PEER_REF}\x00"
        good = _shot_event("order.screenshot.add", self.KEY)

        resp = self._post([toxic, good])

        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1, body)          # 正常那条照常落地
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in body["data"]["skipped_details"]),
            body,
        )
        self.assertEqual(OrderScreenshot.objects.filter(object_key=self.KEY).count(), 1)

    def test_a_lone_surrogate_in_object_key_does_not_500_the_batch(self):
        resp = self._post([_shot_event(
            "order.screenshot.add", f"shared-orders/{SHOT_ORIGIN}/a\ud800.jpg"
        )])
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 0, body)
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_a_lone_surrogate_in_target_ref_does_not_break_response_rendering(self):
        """⭐ 响应体本身也不能带代理项：本仓 DRF 是 `ensure_ascii=False`，回显它会在编码那一步 500。"""
        toxic = _shot_event("order.screenshot.add", self.KEY)
        toxic["target_ref"] = f"{SHOT_PEER_REF}\ud800"

        resp = self._post([toxic])

        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        details = body["data"]["skipped_details"]
        self.assertTrue(any(d["code"] == SKIP_BAD_SHAPE for d in details), body)
        # ⛔ 存不下的串不回显（回空串），否则渲染那一步就炸。
        self.assertEqual([d["target_ref"] for d in details], [""], details)


class ScreenshotOrderScopingTests(ScreenshotBroadcastFixture):
    """Minor 2：截图事件必须按 `target_ref` 指认的那张单作用，⛔ 不静默 no-op、⛔ 不误删别人的行。"""

    OTHER_ORDER_NO = "20260927000000000413"

    def setUp(self):
        super().setUp()
        self.other_order = Order.objects.create(
            game=self.game, order_no=self.OTHER_ORDER_NO,
            status=OrderStatus.IN_PROGRESS, sales_user=self.kefu,
        )
        # 同一个共享 key 被**本租户另一张单**持有（同链段的边角情形）。
        self.other_row = OrderScreenshot.objects.create(
            order=self.other_order, object_key=self.KEY, uploaded_by=self.kefu,
            img_type=OrderScreenshot.IMG_ORDER, status=OrderScreenshot.STATUS_READY,
        )

    def test_an_add_pointing_at_another_orders_key_is_a_loud_skip(self):
        outcome = apply_events(
            [_shot_event("order.screenshot.add", self.KEY)], binding=self.upstream_binding
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        # ⛔ 另一张单那一行原样不动，也⛔ 不新建第二行。
        self.assertTrue(OrderScreenshot.objects.filter(pk=self.other_row.pk).exists())
        self.assertEqual(OrderScreenshot.objects.filter(object_key=self.KEY).count(), 1)

    def test_a_delete_pointing_at_another_orders_key_is_a_loud_skip(self):
        outcome = apply_events(
            [_shot_event("order.screenshot.delete", self.KEY)],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertTrue(
            any(d["code"] == SKIP_BAD_SHAPE for d in outcome.skipped_details), outcome
        )
        self.assertTrue(OrderScreenshot.objects.filter(pk=self.other_row.pk).exists())


class ScreenshotConcurrentAddTests(ScreenshotBroadcastFixture):
    """Minor 3：同 key 的**并发**双投递不许把整批打成 500（savepoint + 良性 already-applied）。"""

    def test_a_racing_duplicate_add_is_benign(self):
        # 「另一条同 key 投递已经落地」——那一行在**本条**的 savepoint 之外，回滚撤不掉它，
        # 而本条的预检跑在对方提交**之前**（所以没看见它）⇒ create 撞唯一约束。
        OrderScreenshot.objects.create(
            order=self.order, object_key=self.KEY, uploaded_by=self.kefu,
            img_type=OrderScreenshot.IMG_ORDER, status=OrderScreenshot.STATUS_READY,
        )
        real_filter = OrderScreenshot.objects.filter
        seen = {"count": 0}

        def _filter(*args, **kwargs):
            seen["count"] += 1
            if seen["count"] == 1:          # 只有 add 的预检看空
                return OrderScreenshot.objects.none()
            return real_filter(*args, **kwargs)

        with mock.patch.object(OrderScreenshot.objects, "filter", side_effect=_filter):
            outcome = apply_events(
                [_shot_event("order.screenshot.add", self.KEY)],
                binding=self.upstream_binding,
            )

        # ⛔ 不抛异常（没有 savepoint 就是 IntegrityError ⇒ 整批 500）、⛔ 不记跳过。
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped_details, [])
        self.assertEqual(OrderScreenshot.objects.filter(object_key=self.KEY).count(), 1)


class ScreenshotContractAndIsolationTests(ScreenshotBroadcastFixture):
    """Minor 5：(a) 下行支不碰订单；(b) 发送侧写出来的载荷真的能被接收侧吃下（对拍）。"""

    def test_the_downstream_branch_leaves_the_order_untouched(self):
        """⭐ 截图事件只写 `OrderScreenshot`：状态 / 时间戳 / 金额 / **订单备注**一个字不动。

        ⚠️ `remark` 是重点：截图载荷里**也有**一个 `remark` 键（截图备注），而订单白名单里
        也有 `remark`（订单备注）⇒ 写成"顺手把 payload 的 remark 写到订单上"就会静默改单。
        """
        before = (
            self.order.status,
            self.order.updated_at,
            self.order.total_price,
            self.order.hall_price,
            self.order.remark,
        )
        apply_events(
            [_shot_event("order.screenshot.add", self.KEY, remark="截图备注")],
            binding=self.upstream_binding,
        )
        apply_events(
            [_shot_event("order.screenshot.delete", self.KEY)],
            binding=self.upstream_binding,
        )
        self.order.refresh_from_db()
        self.assertEqual(
            (
                self.order.status,
                self.order.updated_at,
                self.order.total_price,
                self.order.hall_price,
                self.order.remark,
            ),
            before,
        )

    def test_the_payload_the_sender_writes_is_accepted_by_the_receiver(self):
        """⭐ **同一份载荷**对拍：把 `enqueue_screenshot_event` 真的写出来的事件反喂接收侧。

        判别力：以前发送侧的形状与接收侧的形状各钉各的（两套断言），任何一侧改了键名
        （例如 `object_key` → `key`）两边都还是绿的。本用例只有一条事实来源。
        """
        self.assertEqual(PeerSyncEvent.objects.count(), 0)
        made = enqueue_screenshot_event(
            self.order, kind="order.screenshot.add", object_key=self.KEY, remark="契约"
        )
        upstream_event = {r.binding_role: r for r in made}["upstream"]
        incoming = {
            "kind": upstream_event.kind,
            "target_ref": upstream_event.target_ref,
            "occurred_at": upstream_event.meta["occurred_at"],
            "payload": upstream_event.payload,
        }

        outcome = apply_events([incoming], binding=self.upstream_binding)

        self.assertEqual(outcome.applied, 1, outcome)
        row = OrderScreenshot.objects.get(object_key=self.KEY)
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.remark, "契约")
        self.assertEqual(row.order.order_no, self.order.order_no)

        # 删除那一半同样对拍（同一条链路，方向相反）。
        made = enqueue_screenshot_event(
            self.order, kind="order.screenshot.delete", object_key=self.KEY
        )
        upstream_event = {r.binding_role: r for r in made}["upstream"]
        delete_event = {
            "kind": upstream_event.kind,
            "target_ref": upstream_event.target_ref,
            "occurred_at": upstream_event.meta["occurred_at"],
            "payload": upstream_event.payload,
        }
        outcome = apply_events([delete_event], binding=self.upstream_binding)
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertFalse(OrderScreenshot.objects.filter(object_key=self.KEY).exists())
