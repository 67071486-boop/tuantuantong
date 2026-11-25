"""**优选闸**的判据网（D-0296，2026-10-24）。

## 这张网钉什么

`hall_preferred.check_preferred_claim` 接在**两条 claim 链**（原生 / 镜像）的
`check_grab_limits` 之后；`/assign` 指派路径**不受限**；机器接手（`enforce_limits=False`）
天然不进。

| 判据 | 期望 |
|---|---|
| 优选单（250）× 完成 0 | 拒 `1013`，文案 =「优选订单需要先完成过订单才能接：你还没有已完成的订单」 |
| 优选单（250）× 完成 3 | 过 |
| 优选单（250）× 完成 11 | **2026-10-28 起：放行**（业务方取消上限；此前是拒 `1013`） |
| 非优选（100）× 完成 0 | 过（闸只看优选单） |
| 阈值边界 | `200` 不是优选（严格大于）、`200.01` 是、`None` 不是 |
| 优选 × 押金不够 | 拒（**租户总闸关着也冻**——优选强制押金，文案冒泡为业务码） |
| 非优选 × 总闸关 | 不冻押金（0 余额也能接） |
| 优选 × 押金不够（**HTTP**：web 大厅 / 内部镜像 / takerapi 镜像，总闸关） | 拒 `1013` +「押金不足：」（H-1 回归：覆盖按 `enforce_limits` 判人类路径） |
| 非优选 × 总闸关（**HTTP**：web 大厅） | 接单成功（强制不误伤） |
| 工作室 / 管理员 / 非代练员 | 豁免 |
| 镜像优选（展示价 250）× 完成 0 | 同款拒 |
| `/assign` | 不受限 |

⚠️ 完成数口径（`completed_count_for`）：MAIN 行挂在该人名下 + 订单已完成 + 订单未软删 +
**绩效行未软删**——两条软删各有一条否定用例。
每条钉子的变异方向写在 docstring 里。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile, TakerWallet
from apps.business import jidanpu, jidanpu_upstream
from apps.business.claim import ClaimError, claim_order
from apps.business.hall_preferred import (
    PREFERRED_PRICE_THRESHOLD,
    check_preferred_claim,
    completed_count_for,
    is_personal_booster,
    is_preferred_order,
)
from apps.business.models import (
    EmpType,
    Game,
    GameDepositRule,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    Performance,
)
from apps.core.error_codes import ErrorCode
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

User = get_user_model()

PREFERRED_NO_MSG = "优选订单需要先完成过订单才能接：你还没有已完成的订单"
SUSPECT_MSG = "此订单可能已被接手，请刷新重试"
DEPOSIT_MSG_HEAD = "押金不足："

BASE_URL = "https://jidanpu-preferred.test.invalid"
PASSWORD = "pw-canary-preferred"
CLAIM_NODES = ("grab_hall.take_order", "order.change_status")
ASSIGN_URL = "/api/business/orders/{order_no}/assign/"

#: 文档保留段（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.61"


def _no(n: int) -> str:
    """20 位订单号（`lookup_value_regex = r"\\d{20}"`，19 位会 404）。"""
    return f"2026102400000000{n:04d}"


class PreferredPredicateTests(TenantTestCase):
    """判价与完成数的**纯判据**（阈值边界 + 软删两路的否定面）。"""

    def setUp(self):
        self.game = Game.objects.create(name="优选判据游戏")
        self.taker = User.objects.create_user(username="preferred-pred-taker")

    def test_threshold_is_strictly_greater_than_200(self):
        self.assertFalse(is_preferred_order(type("O", (), {"hall_price": None})()))
        self.assertFalse(
            is_preferred_order(type("O", (), {"hall_price": PREFERRED_PRICE_THRESHOLD})())
        )
        self.assertTrue(
            is_preferred_order(
                type("O", (), {"hall_price": PREFERRED_PRICE_THRESHOLD + Decimal("0.01")})()
            )
        )

    def test_completed_count_needs_a_live_main_row_on_a_live_completed_order(self):
        """完成数 = MAIN 行 × 该人 × 订单已完成 × 订单未软删 × 绩效未软删（缺一不算）。"""
        order = Order.objects.create(
            game=self.game, order_no=_no(1), status=OrderStatus.COMPLETED,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("88.00"),
        )
        self.assertEqual(completed_count_for(self.taker.pk), 1)
        # 订单软删 ⇒ 不算（D-0144 同款口径）。
        Order.objects.filter(pk=order.pk).update(deleted_at=timezone_now())
        self.assertEqual(completed_count_for(self.taker.pk), 0)
        Order.objects.filter(pk=order.pk).update(deleted_at=None)
        # 绩效行软删 ⇒ 不算。
        Performance.objects.filter(order=order).update(
            deleted_at=timezone_now(), deleted_by="canary"
        )
        self.assertEqual(completed_count_for(self.taker.pk), 0)

    def test_other_people_rows_and_other_statuses_do_not_count(self):
        """别人的行 / 未完成单都不算（否定面配对照：换成自己的完成单就数得到）。"""
        other = User.objects.create_user(username="preferred-pred-other")
        order = Order.objects.create(
            game=self.game, order_no=_no(2), status=OrderStatus.IN_PROGRESS,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("88.00"),
        )
        self.assertEqual(completed_count_for(self.taker.pk), 0)
        done = Order.objects.create(
            game=self.game, order_no=_no(3), status=OrderStatus.COMPLETED,
        )
        Performance.objects.create(
            order=done, emp_type=EmpType.MAIN, user=other,
            temp_amount=Decimal("88.00"),
        )
        self.assertEqual(completed_count_for(self.taker.pk), 0)
        self.assertEqual(completed_count_for(other.pk), 1)


def timezone_now():
    from django.utils import timezone

    return timezone.now()


class _GateFixture(TenantTestCase):
    """优选闸的公共夹具：一个代练员 + 可控的完成单数。"""

    def setUp(self):
        self.game = Game.objects.create(name="优选闸游戏")
        # 优选单**强制押金**（本线特性）⇒ 非押金用例统一给「免押规则」当基线，
        # 押金用例再各自把金额改掉（OneToOne，改行不改 create）。
        GameDepositRule.objects.create(game=self.game, amount=Decimal("0"))
        self.taker = self._make_taker("preferred-gate-taker")

    def _make_taker(self, username, *, is_studio=False, group="代练员"):
        user = User.objects.create_user(username=username)
        from django.contrib.auth.models import Group

        if group:
            name = group if isinstance(group, str) else group
            grp, _ = Group.objects.get_or_create(name=name)
            user.groups.add(grp)
            grant_nodes(user, "grab_hall.take_order", "order.change_status")
        StaffProfile.objects.create(user=user, is_studio=is_studio).games.set([self.game])
        return user

    def _add_completed(self, user, n, *, start=0):
        for i in range(n):
            order = Order.objects.create(
                game=self.game, order_no=_no(100 + start + i),
                status=OrderStatus.COMPLETED, hall_price=Decimal("88.00"),
            )
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, user=user,
                temp_amount=Decimal("88.00"),
            )

    def _hall_order(self, no, *, price="250.00", status=OrderStatus.WAITING_GRAB):
        order = Order.objects.create(
            game=self.game, order_no=no, status=status, hall_price=Decimal(price),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price),
        )
        return order

    def _claim(self, order, *, user, **kwargs):
        return claim_order(user_id=user.pk, order_id=order.pk, nodes=CLAIM_NODES, **kwargs)


class NativeGateTests(_GateFixture):
    """原生单的三档 + 押金强制 + 豁免。"""

    def test_zero_completed_is_rejected_with_the_verbatim_message(self):
        """完成 0 ⇒ 拒 `1013`，文案逐字（与同码其它支可区分）。"""
        order = self._hall_order(_no(1), price="250.00")
        with self.assertRaises(ClaimError) as caught:
            self._claim(order, user=self.taker)
        self.assertEqual(caught.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertEqual(str(caught.exception), PREFERRED_NO_MSG)
        # 零写入：MAIN 行没人、状态还在大厅。
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        self.assertEqual(Order.objects.get(pk=order.pk).status, OrderStatus.WAITING_GRAB)

    def test_three_completed_passes(self):
        """完成 3 ⇒ 过（闸放行，接单真的成功）。"""
        self._add_completed(self.taker, 3)
        order = self._hall_order(_no(2), price="250.00")
        claimed = self._claim(order, user=self.taker)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    def test_eleven_completed_passes(self):
        """⭐ **2026-10-28 业务方拍板的原点**：完成 11 单⇒ 照样能接。

        原来这条是「完成 > 10 ⇒ 拒（此订单可能已被接手）」，业务方要求**取消上限**
        ⇒ 「只要完成过订单就能接」。⛔ 把它加回来这条用例必红。
        """
        self._add_completed(self.taker, 11)
        order = self._hall_order(_no(3), price="250.00")
        claimed = self._claim(order, user=self.taker)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    def test_exactly_ten_completed_passes(self):
        """边界：完成数恰好 10 ⇒ 过（上限已取消，10 更该过）。"""
        self._add_completed(self.taker, 10)
        order = self._hall_order(_no(4), price="250.00")
        self._claim(order, user=self.taker)

    def test_a_non_preferred_order_needs_no_history(self):
        """非优选（100）× 完成 0 ⇒ 过（闸只看优选单；`100 < 200` 不是优选）。"""
        order = self._hall_order(_no(5), price="100.00")
        claimed = self._claim(order, user=self.taker)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_a_preferred_order_forces_the_deposit_even_when_the_tenant_gate_is_off(self):
        """⭐ 优选单强制押金：租户总闸**关着**也要冻；余额不够 ⇒ 人话冒泡为业务码。"""
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        self._add_completed(self.taker, 3)
        order = self._hall_order(_no(6), price="250.00")
        # ⚠️ 钱包闸要求已在事务里（`select_for_update`）；预期失败经异常退出即回滚。
        with transaction.atomic():
            with self.assertRaises(ClaimError) as caught:
                self._claim(order, user=self.taker)
        self.assertEqual(caught.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn(DEPOSIT_MSG_HEAD, str(caught.exception))
        # 零写入：绩效行没人。
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_a_preferred_order_with_enough_balance_freezes_and_passes(self):
        """余额够 ⇒ 强制押金**照冻**（闸的阳性对照：不是"强制"只是"报错"）。"""
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("50.00"))
        TakerWallet.objects.create(user=self.taker, balance=Decimal("60.00"))
        self._add_completed(self.taker, 3)
        order = self._hall_order(_no(7), price="250.00")
        self._claim(order, user=self.taker)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(wallet.frozen, Decimal("50.00"))
        self.assertEqual(wallet.balance, Decimal("10.00"))

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_a_non_preferred_order_with_an_empty_wallet_is_not_blocked(self):
        """非优选 × 总闸关 ⇒ 不冻押金（0 余额也能接）——对照证明「强制」只作用优选。"""
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        self._add_completed(self.taker, 3)
        order = self._hall_order(_no(8), price="100.00")
        claimed = self._claim(order, user=self.taker)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    def test_machine_claim_bypasses_the_gate(self):
        """机器接手（`enforce_limits=False`）不进优选闸（完成 0 也接）。"""
        order = self._hall_order(_no(9), price="250.00")
        claimed = self._claim(order, user=self.taker, enforce_limits=False)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)


class ExemptionTests(_GateFixture):
    """豁免面：工作室 / 管理员 / 非代练员（非个人打手一律放行）。"""

    def test_a_studio_account_is_exempt(self):
        studio = self._make_taker("preferred-studio", is_studio=True)
        order = self._hall_order(_no(10), price="250.00")
        claimed = self._claim(order, user=studio)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    def test_a_tenant_admin_is_exempt(self):
        """租户管理员（「系统管理员」职位）豁免——闸的判据收口在 `is_tenant_admin`。"""
        admin = self._make_admin()
        order = self._hall_order(_no(11), price="250.00")
        claimed = self._claim(order, user=admin)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    def _make_admin(self):
        from django.contrib.auth.models import Group

        user = User.objects.create_user(username="preferred-admin-user")
        grp, _ = Group.objects.get_or_create(name="系统管理员")
        user.groups.add(grp)
        grant_nodes(user, "grab_hall.take_order", "order.change_status")
        StaffProfile.objects.create(user=user).games.set([self.game])
        return user

    def test_an_account_without_the_worker_group_is_exempt(self):
        """不持「代练员」Group ⇒ 不是个人打手 ⇒ 放行（闸的覆盖面就是个人打手）。"""
        other = self._make_taker("preferred-non-worker", group="客服")
        order = self._hall_order(_no(12), price="250.00")
        claimed = self._claim(order, user=other)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)

    def test_is_personal_booster_shape(self):
        """判定函数自身的形状：三条同时成立才是个人打手。"""
        self.assertTrue(is_personal_booster(self.taker))
        studio = self._make_taker("preferred-shape-studio", is_studio=True)
        self.assertFalse(is_personal_booster(studio))
        admin = self._make_admin()
        self.assertFalse(is_personal_booster(admin))
        outsider = self._make_taker("preferred-shape-out", group="客服")
        self.assertFalse(is_personal_booster(outsider))
        self.assertFalse(is_personal_booster(None))


class AssignUnrestrictedTests(_GateFixture):
    """`/assign` 指派路径不受优选闸限制（闸只在 claim 链）。"""

    def test_assigning_a_preferred_order_to_a_zero_completed_taker_passes(self):
        taker_user = self.taker
        order = self._hall_order(_no(13), price="250.00")
        staff = make_logged_in_client(
            username="preferred-assigner", groups=("客服",), games=[self.game],
            nodes=("order.assign_taker", "order.view_detail"),
        )
        response = staff.post(
            ASSIGN_URL.format(order_no=order.order_no),
            {"user_id": taker_user.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            taker_user.pk,
        )


class MirrorGateTests(TenantTestCase):
    """镜像链的优选闸（同一份判据、价源 = 勾兑展示价）。"""

    def setUp(self):
        self.game = Game.objects.create(name="镜像优选游戏")
        self.owner_user = User.objects.create_user(
            username="preferred-up-owner", password=PASSWORD
        )
        self.taker = User.objects.create_user(
            username="preferred-up-taker", password=PASSWORD
        )
        from django.contrib.auth.models import Group

        grp, _ = Group.objects.get_or_create(name="代练员")
        self.taker.groups.add(grp)
        StaffProfile.objects.create(user=self.taker).games.set([self.game])
        grant_nodes(self.taker, "grab_hall.take_order", "order.change_status")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account="acc-preferred",
            password=PASSWORD, enabled=True,
        )
        # 优选镜像**强制押金** ⇒ 非押金用例给「免押规则」当基线。
        GameDepositRule.objects.create(game=self.game, amount=Decimal("0"))
        self.mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261024000000092222",
            owner_binding=self.binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game, project_text="镜像优选项目",
            remote_rec_fee=Decimal("250.00"),
            upstream_payload={"orderId": "20261024000000092222", "recFee": "250.00"},
            first_seen_at=timezone_now(),
        )

    def _add_completed(self, n):
        for i in range(n):
            order = Order.objects.create(
                game=self.game, order_no=_no(200 + i),
                status=OrderStatus.COMPLETED, hall_price=Decimal("88.00"),
            )
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, user=self.taker,
                temp_amount=Decimal("88.00"),
            )

    def test_a_preferred_mirror_rejects_a_zero_completed_booster(self):
        """镜像展示价 250（recFee 250 × 比例 100）× 完成 0 ⇒ 同款拒（价源 = 勾兑价）。"""
        with self.assertRaises(ClaimError) as caught:
            jidanpu_upstream.claim_upstream_mirror(
                user_id=self.taker.pk, mirror_id=self.mirror.pk,
                actor="preferred-up-taker", actor_id=self.taker.pk,
            )
        self.assertEqual(caught.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertEqual(str(caught.exception), PREFERRED_NO_MSG)
        # 零写入：行还在大厅、没有订单行。
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_preferred_mirror_with_history_goes_through(self):
        """完成 3 ⇒ 过（阳性对照；打桩挡住上游那一段，接单收敛到转正）。"""
        self._add_completed(3)

        class _Stub:
            def ensure_login(self):
                pass

            def call(self, method, path, **kwargs):
                if path == "/OrderAjax/RecOrderByInternal":
                    self.last_envelope = {"code": 1, "msg": "接单成功", "data": ""}
                    return ""
                raise AssertionError(f"未预期的端点：{method} {path}")

        stub = _Stub()

        class _Ctx:
            def __init__(self, c):
                self.c = c

            def __enter__(self):
                return self.c

            def __exit__(self, *exc):
                return False

        with mock.patch.object(jidanpu, "claim_session", lambda _b: _Ctx(stub)):
            order = jidanpu_upstream.claim_upstream_mirror(
                user_id=self.taker.pk, mirror_id=self.mirror.pk,
                actor="preferred-up-taker", actor_id=self.taker.pk,
            )
        self.assertIsNotNone(order)
        self.assertEqual(order.hall_price, Decimal("250.00"))


class DirectGateUnitTests(TenantTestCase):
    """`check_preferred_claim` 的直接单测：mirror 缺价源 / 空参放行。"""

    def test_no_target_means_pass(self):
        user = User.objects.create_user(username="preferred-unit-user")
        # 都不传 ⇒ 放行（不抛）。
        check_preferred_claim(user=user)

    def test_mirror_preferred_price_uses_the_blended_display_price(self):
        """镜像的「优选」按 `upstream_hall_price`（recFee × 比例）判，⛔ 不按 recFee 原值。

        变异方向：把判定改成读 `remote_rec_fee`（客户价）⇒ 比例 50、recFee 500
        （展示 250）那张卡会被当成 500 判——构造 recFee=500、比例 50：
        展示价 250 > 200 ⇒ 优选；若按 recFee 判也是优选 ⇒ 判不出差别？
        ⛔ 反向才有判别力：recFee=220、比例 50 ⇒ 展示 110（非优选）而 recFee 220 > 200
        ⇒ 按 recFee 判会误拒。本用例钉这个方向。
        """
        game = Game.objects.create(name="镜像判价游戏")
        owner = User.objects.create_user(username="preferred-price-owner")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url=BASE_URL, account="acc-price",
            password=PASSWORD, enabled=True,
        )
        binding.hall_price_percent = 50
        binding.save(update_fields=["hall_price_percent"])
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261024000000093333",
            owner_binding=binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=game, project_text="判价项目",
            remote_rec_fee=Decimal("220.00"),
            upstream_payload={"orderId": "20261024000000093333", "recFee": "220.00"},
            first_seen_at=timezone_now(),
        )
        zero_history = User.objects.create_user(username="preferred-price-taker")
        StaffProfile.objects.create(user=zero_history).games.set([game])
        # 展示价 110 ⇒ 非优选 ⇒ 放行（完成 0 也不拒）。
        check_preferred_claim(user=zero_history, mirror=mirror)


class TakerapiPreferredGateTests(_GateFixture):
    """takerapi 的对外 claim 链同样被闸拦（复用 claim_order ⇒ 自动生效）。"""

    def test_the_takerapi_claim_is_blocked_for_a_zero_completed_booster(self):
        order = self._hall_order(_no(14), price="250.00")
        app = TakerApp.objects.create(
            name="优选闸测试应用", app_key="preferredtakerkey1",
            app_secret="preferred-taker-secret-00000001",
            taker=self.taker, created_by=self.taker,
        )
        anon = Client(SERVER_NAME="tenant.test.com")
        token = anon.post(
            "/api/taker/oauth/token",
            {
                "grant_type": "client_credentials",
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        ).json()["access_token"]
        response = anon.post(
            f"/api/taker/v1/hall/{order.order_no}/claim/",
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY="preferred-gate-claim-0001",
        )
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED, body)
        self.assertEqual(body["msg"], PREFERRED_NO_MSG)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )


WEB_CLAIM_URL = "/api/business/hall/{order_no}/claim/"
INTERNAL_MIRROR_CLAIM_URL = "/api/business/hall/upstream/{pk}/claim/"
TAKER_MIRROR_CLAIM_URL = "/api/taker/v1/hall/upstream/{pk}/claim/"


class HallClaimDepositHttpTests(_GateFixture):
    """⭐ H-1 回归（**HTTP 级**）：优选强制押金在网页 / 打手两条主路径上真的生效。

    现场（评审 H-1）：旧覆盖条件写成「`enforce_deposit is None` 才强制」，
    而 web 大厅两条人类路径（原生 `order_views.claim`、内部镜像接单视图）传的是
    `deposit_enforced_for(request)`——**具体的 bool**，租户总闸关时 = `False`
    ⇒ 覆盖永不命中，优选单在网页大厅**不冻押金**。修复后判「人类路径」用的是
    `enforce_limits`。本类每条都走**真 HTTP**——既有押金用例全是直调 `claim_order`
    （None 形状），HTTP 形状的缺口正是这次漏网的原因，⛔ 别再改回直调。

    每条的变异方向写在 docstring 里。
    """

    def _web_client(self, username, *, nodes=CLAIM_NODES):
        return make_logged_in_client(
            username=username, groups=("代练员",), games=[self.game], nodes=nodes,
        )

    def _mirror(self, order_id, *, rec_fee="250.00"):
        """一张在挂镜像行（绑定默认比例 100 ⇒ 勾兑展示价 = recFee）。"""
        owner = User.objects.create_user(username=f"preferred-http-owner-{order_id}")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url=BASE_URL,
            account=f"acc-http-{order_id}", password=PASSWORD, enabled=True,
        )
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=order_id,
            owner_binding=binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game, project_text="HTTP 押金回归镜像",
            remote_rec_fee=Decimal(rec_fee),
            upstream_payload={"orderId": order_id, "recFee": rec_fee},
            first_seen_at=timezone_now(),
        )

    def _taker_token(self, app_key, app_secret):
        app = TakerApp.objects.create(
            name="优选押金回归应用", app_key=app_key, app_secret=app_secret,
            taker=self.taker, created_by=self.taker,
        )
        return Client(SERVER_NAME="tenant.test.com").post(
            "/api/taker/oauth/token",
            {
                "grant_type": "client_credentials",
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        ).json()["access_token"]

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_web_hall_claim_of_a_preferred_order_rejects_on_deposit_when_the_gate_is_off(self):
        """① web 大厅接优选单：总闸**关** + 钱包不够 ⇒ 仍被押金拒（人话冒泡为业务码）。

        变异方向：把 `claim_order` 的覆盖条件改回「`enforce_deposit is None`」⇒ 本条红
        （本路径传的是具体 `False` ⇒ 旧条件永不命中 ⇒ 接单会成功）。
        """
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        client = self._web_client("preferred-web-deposit")
        taker = User.objects.get(username="preferred-web-deposit")
        self._add_completed(taker, 3)  # 先过优选完成数档，让拒绝落在押金上
        order = self._hall_order(_no(20), price="250.00")
        response = client.post(WEB_CLAIM_URL.format(order_no=order.order_no))
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED, body)
        self.assertTrue(body["msg"].startswith(DEPOSIT_MSG_HEAD), body)
        # 零写入：MAIN 行没人、单还在大厅。
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        self.assertEqual(
            Order.objects.get(pk=order.pk).status, OrderStatus.WAITING_GRAB
        )

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_web_hall_claim_of_a_non_preferred_order_still_succeeds(self):
        """② 同配置接**非优选**单（100）⇒ 成功（强制押金不误伤总闸关的租户）。

        变异方向：把覆盖改成「优选与否不管、人类一律冻」⇒ 本条红（0 余额被拒）。
        """
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        client = self._web_client("preferred-web-nondeposit")
        taker = User.objects.get(username="preferred-web-nondeposit")
        self._add_completed(taker, 3)
        order = self._hall_order(_no(21), price="100.00")
        response = client.post(WEB_CLAIM_URL.format(order_no=order.order_no))
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            taker.pk,
        )

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_internal_mirror_claim_of_a_preferred_order_rejects_on_deposit(self):
        """③ 内部镜像接单视图（传 `deposit_enforced_for(request)` 的那条人类路）同样强制。

        变异方向：把 `claim_upstream_mirror` 的覆盖改回「`enforce_deposit is None`」档
        ⇒ 本条红（视图传具体 `False` ⇒ 旧条件永不命中）。
        """
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        client = make_logged_in_client(
            username="preferred-mirror-web", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order",),
        )
        taker = User.objects.get(username="preferred-mirror-web")
        self._add_completed(taker, 3)
        mirror = self._mirror("20261024000000003001")
        before = Order.objects.count()
        response = client.post(INTERNAL_MIRROR_CLAIM_URL.format(pk=mirror.pk))
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED, body)
        self.assertTrue(body["msg"].startswith(DEPOSIT_MSG_HEAD), body)
        # 零写入：行还在大厅、没有转正订单。
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(Order.objects.count(), before)

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_takerapi_mirror_claim_of_a_preferred_order_rejects_on_deposit(self):
        """④ takerapi 镜像接单端点：总闸关 + 钱包不够 ⇒ 优选镜像仍被押金拒。

        变异方向：把 `claim_upstream_mirror` 的优选强制押金整段删掉 ⇒ 本条红
        （该路径不传 `enforce_deposit` ⇒ 回落总闸 False ⇒ 会放行转正）。
        """
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        self._add_completed(self.taker, 3)  # 先过优选完成数档，让拒绝落在押金上
        mirror = self._mirror("20261024000000003002")
        token = self._taker_token(
            "preferreddepositkey1", "preferred-deposit-secret-0000001"
        )
        before = Order.objects.count()
        response = Client(SERVER_NAME="tenant.test.com").post(
            TAKER_MIRROR_CLAIM_URL.format(pk=mirror.pk),
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY="preferred-deposit-mirror-0001",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], ErrorCode.GRAB_LIMIT_EXCEEDED, body)
        self.assertTrue(body["msg"].startswith(DEPOSIT_MSG_HEAD), body)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(Order.objects.count(), before)


class PreferredSwitchOffTests(_GateFixture):
    """优选**租户开关** `hall_preferred_orders`（D-0328，2026-10-29）——**接单**侧。

    关（N）⇒ 优选完成数闸与「优选强制押金」都跳过：高价单回归普通接单规则
    （普通押金闸开着时照样拦）。开（显式 `Y` 行 / 无覆盖行默认 Y）⇒ 行为逐字
    不变——本文件其它用例全部在「无覆盖行 = 默认开」下跑，天然是回归网；这里
    再补一条显式 Y 行的逐字钉。
    每条的变异方向写在 docstring 里。
    """

    SWITCH_KEY = "hall_preferred_orders"

    def _turn(self, value: str) -> None:
        from apps.business.models import SystemSwitch

        SystemSwitch.objects.create(key=self.SWITCH_KEY, value=value)

    def test_switch_off_a_zero_completed_booster_can_claim_a_high_price_order(self):
        """关 ⇒ 高价单（250）× 完成 0 ⇒ 接得了（优选完成数闸跳过）。

        变异方向：把 `check_preferred_claim` 函数口上的 `preferred_enabled()` 闸摘掉
        ⇒ 本条红（仍抛 `1013`「优选订单需要先完成过订单才能接…」）。⚠️ 该闸收口在
        函数内 ⇒ 三条 claim 链（原生 / 鸡蛋铺镜像 / 团团通镜像）同一次变异全红面。
        """
        self._turn("N")
        order = self._hall_order(_no(30), price="250.00")
        claimed = self._claim(order, user=self.taker)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)
        # 真的接上了：MAIN 行归了人。
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            self.taker.pk,
        )

    @override_settings(DEPOSIT_ENFORCED=False)
    def test_switch_off_skips_the_preferred_forced_deposit(self):
        """关 ⇒ 优选强制押金跳过：总闸关 + 押金规则 300 + 0 余额 ⇒ 照样接、一分不冻。

        变异方向：把 `claim_order` 覆盖点上的 `preferred_enabled()` 摘掉 ⇒ 本条红
        （覆盖复活 ⇒ `1013`「押金不足：…」，接不了单）。
        """
        self._turn("N")
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        TakerWallet.objects.create(user=self.taker, balance=Decimal("0.00"))
        order = self._hall_order(_no(31), price="250.00")
        claimed = self._claim(order, user=self.taker)
        self.assertEqual(claimed.status, OrderStatus.IN_PROGRESS)
        wallet = TakerWallet.objects.get(user=self.taker)
        self.assertEqual(
            (wallet.frozen, wallet.balance), (Decimal("0.00"), Decimal("0.00")),
            "开关关着还冻了押金 = 优选强制押金没被跳过",
        )

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_switch_off_keeps_the_normal_deposit_gate(self):
        """关 ⇒ **普通押金闸照旧**：总闸开 + 0 余额 + 高价单 ⇒ 仍被押金拒（同码 + 押金文案）。

        「关 = 回归普通接单规则」的另一半：不是「关 = 免押」。变异方向：谁把
        开关关做成「连普通押金闸也跳」⇒ 本条红（接成了）。
        """
        self._turn("N")
        GameDepositRule.objects.filter(game=self.game).update(amount=Decimal("300.00"))
        order = self._hall_order(_no(32), price="250.00")
        with transaction.atomic():
            with self.assertRaises(ClaimError) as caught:
                self._claim(order, user=self.taker)
        self.assertEqual(caught.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertIn(DEPOSIT_MSG_HEAD, str(caught.exception))
        # 零写入：MAIN 行没人。
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )

    def test_explicit_on_row_still_rejects_with_the_verbatim_message(self):
        """显式登记 `Y` 行 ⇒ 与无覆盖行逐字同行为（回归钉；1013 + 逐字文案 + 零写入）。"""
        self._turn("Y")
        order = self._hall_order(_no(33), price="250.00")
        with self.assertRaises(ClaimError) as caught:
            self._claim(order, user=self.taker)
        self.assertEqual(caught.exception.code, ErrorCode.GRAB_LIMIT_EXCEEDED)
        self.assertEqual(str(caught.exception), PREFERRED_NO_MSG)
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )

    def test_switch_off_passes_the_mirror_and_price_targets_too(self):
        """关 ⇒ `mirror=` / `price=` 两条镜像链的同一道闸也放行（直调单元：
        展示价 250 的镜像 × 完成 0 不抛；开关开（对照）同参必抛）。
        """
        game = self.game
        owner = User.objects.create_user(username="pref-switch-off-up-owner")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url=BASE_URL,
            account="acc-pref-switch-off", password=PASSWORD, enabled=True,
        )
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261024000000094001",
            owner_binding=binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=game, project_text="开关关镜像",
            remote_rec_fee=Decimal("250.00"),
            upstream_payload={"orderId": "20261024000000094001", "recFee": "250.00"},
            first_seen_at=timezone_now(),
        )
        self._turn("N")
        # mirror 链（勾兑展示价 250 > 200）与 price 链（团团通镜像同款）都放行。
        check_preferred_claim(user=self.taker, mirror=mirror)
        check_preferred_claim(user=self.taker, price=Decimal("250.00"))
        # 对照：同一参、开关开着（删掉覆盖行回落默认 Y）⇒ 必抛（证明上面是闸在放行，
        # 不是参数本身不构成优选）。
        from apps.business.models import SystemSwitch

        SystemSwitch.objects.filter(key=self.SWITCH_KEY).delete()
        with self.assertRaises(ClaimError):
            check_preferred_claim(user=self.taker, mirror=mirror)


class UpstreamMirrorSwitchOffTests(TenantTestCase):
    """鸡蛋铺镜像全链 × 开关关：优选强制押金一并跳过（转正照走、押金不冻）。

    变异方向：把 `claim_upstream_mirror` 覆盖点上的 `preferred_enabled()` 摘掉
    ⇒ 本条红（总闸关 + 0 余额 + 押金规则 300 时 `1013`「押金不足」，转正不成）。
    """

    def test_switch_off_mirror_claim_promotes_without_freezing(self):
        from django.contrib.auth.models import Group

        from apps.accounts.models import TakerWallet
        from apps.business import jidanpu
        from apps.business.models import (
            GameDepositRule,
            SystemSwitch,
        )

        game = Game.objects.create(name="镜像开关关游戏")
        GameDepositRule.objects.create(game=game, amount=Decimal("300.00"))
        owner = User.objects.create_user(username="pref-sw-up-owner", password=PASSWORD)
        taker = User.objects.create_user(username="pref-sw-up-taker", password=PASSWORD)
        grp, _ = Group.objects.get_or_create(name="代练员")
        taker.groups.add(grp)
        from apps.accounts.models import StaffProfile

        StaffProfile.objects.create(user=taker).games.set([game])
        grant_nodes(taker, "grab_hall.take_order", "order.change_status")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url=BASE_URL,
            account="acc-pref-sw-off", password=PASSWORD, enabled=True,
        )
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id="20261024000000095001",
            owner_binding=binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=game, project_text="镜像开关关项目",
            remote_rec_fee=Decimal("250.00"),
            upstream_payload={"orderId": "20261024000000095001", "recFee": "250.00"},
            first_seen_at=timezone_now(),
        )
        TakerWallet.objects.create(user=taker, balance=Decimal("0.00"))
        SystemSwitch.objects.create(key="hall_preferred_orders", value="N")

        class _Stub:
            def ensure_login(self):
                pass

            def call(self, method, path, **kwargs):
                if path == "/OrderAjax/RecOrderByInternal":
                    self.last_envelope = {"code": 1, "msg": "接单成功", "data": ""}
                    return ""
                raise AssertionError(f"未预期的端点：{method} {path}")

        stub = _Stub()

        class _Ctx:
            def __init__(self, c):
                self.c = c

            def __enter__(self):
                return self.c

            def __exit__(self, *exc):
                return False

        with override_settings(DEPOSIT_ENFORCED=False):
            with mock.patch.object(jidanpu, "claim_session", lambda _b: _Ctx(stub)):
                order = jidanpu_upstream.claim_upstream_mirror(
                    user_id=taker.pk, mirror_id=mirror.pk,
                    actor="pref-sw-up-taker", actor_id=taker.pk,
                )
        # 转正真的成了（高价镜像在开关关时回归普通接单规则）。
        self.assertIsNotNone(order)
        self.assertEqual(order.hall_price, Decimal("250.00"))
        # 一分没冻（强制押金随开关一起跳过）。
        wallet = TakerWallet.objects.get(user=taker)
        self.assertEqual((wallet.frozen, wallet.balance), (Decimal("0.00"), Decimal("0.00")))
