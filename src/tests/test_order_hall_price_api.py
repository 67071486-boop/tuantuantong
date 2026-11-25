"""改发单价端点 `POST /api/business/orders/<单号>/hall-price/`（spec §5.2 / §8 第 6–13、18、19 条）。

    判据一律写**字面值**（E-0024）；写调用断言**数据库行**；跨租户的否定断言用**订单号**指认
    （`AGENTS.md` §5：两个 schema 的主键各自从 1 开始，用主键会命中「自己的那一行」）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.performance_amount import effective_taker_price
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BASE = "/api/business/orders"
NODE = "order.set_offer_price"


class HallPriceEndpointTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="offer-price-cs",
            groups=("客服",),
            nodes=(
                NODE,
                "order.list",
                "order.add_edit",
                "order.assign_taker",
                "order.submit_payout_price",
                # ⚠️ 计划里的 `nodes` 少了这一个（`/withdraw/` 的类级门是
                # `order.withdraw_from_grab_hall`）⇒ 撤回那一腿会**以 403 静默通过**
                # 「价没变」的断言（非判别性）。这里补上它，并断 `code == 0` 的哨兵。
                "order.withdraw_from_grab_hall",
                "order.clear_taker",
                # ⭐ 计划 ④：本文件有一条用例打 `/throw-to-hall/`
                # （「写入点与清理路径」），而该门已改判**独占** 36 ⇒ 夹具补它。
                "order.send_to_grab_hall",
            ),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。本类的每一次请求都是对本账号 `self.game` 那张单的操作：
        # 录单写侧过 `assert_game_allowed`（⇒ 业务码 1000），改发单价 / 改收款状态 / 撤回 /
        # 清除接单人都要先经 `get_object()`（被 `game_scope` 收窄的 queryset）。
        # `grant_games` **刻意不接进** `make_logged_in_client` 的默认路径（那会让
        # 「没有档案 ⇒ 什么都看不到」这条新品钉变成恒真）⇒ 这里显式授权。
        grant_games(User.objects.get(username="offer-price-cs"), self.game)

    def _create(self, **overrides):
        payload = {"game": self.game.id, "project_text": "满星", "total_price": "88.00"}
        payload.update(overrides)
        body = self.client.post(BASE + "/", payload, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["order_no"]

    def _post(self, no, payload):
        return self.client.post(
            f"{BASE}/{no}/hall-price/", payload, content_type="application/json"
        ).json()

    def _seed_old_price(self, no, value="1.00"):
        """把发单价设成一个**确定不同于目标值**的旧值，让留痕的 from/to 有判别力。"""
        Order.objects.filter(order_no=no).update(hall_price=Decimal(value))

    def _row(self, no):
        return Order.objects.get(order_no=no)

    # ── 金额档（spec §8 第 6 条）───────────────────────────────────────────
    def test_amount_inputs_land_as_two_decimal_values(self):
        for index, (raw, expected) in enumerate(
            (("88.5", Decimal("88.50")), ("0", Decimal("0.00")), ("-12.34", Decimal("-12.34")))
        ):
            no = self._create(project_text=f"金额档{index}")
            body = self._post(no, {"price": raw})
            self.assertEqual(body["code"], 0, (raw, body))
            self.assertEqual(self._row(no).hall_price, expected, raw)

    # ── 比例档（spec §8 第 7 条）──────────────────────────────────────────
    def test_percent_inputs_convert_from_the_order_total(self):
        for index, (raw, expected) in enumerate(
            (("50%", Decimal("44.00")), ("0%", Decimal("0.00")), ("1000%", Decimal("880.00")))
        ):
            no = self._create(project_text=f"比例档{index}")
            body = self._post(no, {"price": raw})
            self.assertEqual(body["code"], 0, (raw, body))
            self.assertEqual(self._row(no).hall_price, expected, raw)

    def test_percent_above_the_ceiling_is_rejected_and_leaves_the_row_unchanged(self):
        no = self._create()
        self._seed_old_price(no)
        body = self._post(no, {"price": "1000.01%"})
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(self._row(no).hall_price, Decimal("1.00"))

    def test_percent_conversion_rounds_half_up(self):
        # 0.01 × 50 ÷ 100 = 0.005 ⇒ ROUND_HALF_UP ⇒ 0.01（HALF_EVEN 会给 0.00）
        no = self._create(total_price="0.01")
        self._post(no, {"price": "50%"})
        self.assertEqual(self._row(no).hall_price, Decimal("0.01"))
        # 0.05 × 50 ÷ 100 = 0.025 ⇒ ROUND_HALF_UP ⇒ 0.03（HALF_EVEN 会给 0.02）
        no2 = self._create(total_price="0.05")
        self._post(no2, {"price": "50%"})
        self.assertEqual(self._row(no2).hall_price, Decimal("0.03"))

    def test_the_ceiling_constant_is_a_thousand_not_a_hundred(self):
        """判别力：把 `HALL_PRICE_PERCENT_MAX` 误接成 `performance_amount.PERCENT_MAX`（100）⇒
    `1000%` 会被截成 `100%`、本用例落 `88.00` 而不是 `880.00`，红。"""
        no = self._create()
        self._post(no, {"price": "1000%"})
        self.assertEqual(self._row(no).hall_price, Decimal("880.00"))

    # ── 金额闸与语法（spec §8 第 8 条）────────────────────────────────────
    def test_bad_syntax_blank_and_missing_are_validation_errors(self):
        no = self._create()
        self._seed_old_price(no)
        for raw in ("50%x", "%50", "50%%", "-50%", "+", "", "   ", "abc"):
            body = self._post(no, {"price": raw})
            self.assertEqual(body["code"], 1000, (raw, body))
            self.assertEqual(self._row(no).hall_price, Decimal("1.00"), raw)
        self.assertEqual(self._post(no, {})["code"], 1000)

    def test_direct_amount_beyond_twelve_digits_is_rejected_not_a_500(self):
        no = self._create()
        self._seed_old_price(no)
        body = self._post(no, {"price": "99999999999.99"})
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(self._row(no).hall_price, Decimal("1.00"))

    def test_percent_conversion_overflow_is_rejected_not_a_500(self):
        no = self._create(total_price="9999999999.99")
        self._seed_old_price(no)
        body = self._post(no, {"price": "1000%"})
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(self._row(no).hall_price, Decimal("1.00"))

    def test_bad_input_is_http_200_with_code_1000_not_a_500(self):
        """铁律：入参非法一律 **HTTP 200 + `code=1000`**（不是 500）。

    ⚠️ 上面几条只断信封里的 `code`——信封恒 200 这件事**没被钉住**（E-0010 的同族：
    只测「翻译对了」不测「没变成 500」）。这里直接断 `resp.status_code == 200`。
"""
        no = self._create()
        for raw in ("50%x", "1000.01%", "99999999999.99"):
            resp = self.client.post(
                f"{BASE}/{no}/hall-price/", {"price": raw}, content_type="application/json"
            )
            self.assertEqual(resp.status_code, 200, raw)
            self.assertEqual(resp.json()["code"], 1000, raw)

    def test_the_write_rolls_back_when_the_audit_row_fails(self):
        """写动作与留痕**同事务**（spec §10 第 7 条）：留痕抛错 ⇒ `hall_price` 也回滚。

    桩打在 `order_writes.record`（**被查找的那个模块**，不是定义它的 `apps.audit.log`）
    ——动作体已搬进领域核心 `apps/business/order_writes.py`，`record` 在**那里**被查找
    （E-0029：桩打在定义模块上接不上）。与
    `test_dispatch_api.py::test_throw_to_hall_rolls_back_status_and_price_when_audit_fails`
    同一手法。判别力：把 `with transaction.atomic():` 去掉 ⇒ 价格已提交、本用例红。
"""
        no = self._create()
        self._seed_old_price(no, "1.00")

        def _boom(**kwargs):
            raise RuntimeError("留痕写不进去")

        with mock.patch("apps.business.order_writes.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"{BASE}/{no}/hall-price/", {"price": "66.00"}, content_type="application/json"
                )
        self.assertEqual(self._row(no).hall_price, Decimal("1.00"))
        self.assertFalse(
            OperationLog.objects.filter(action="order.change_offer_price", order_no=no).exists()
        )

    # ── 状态、审计列与留痕（spec §8 第 10、11 条）────────────────────────
    def test_it_does_not_touch_the_status(self):
        no = self._create()
        before = self._row(no)
        # ⚠️ 计划里这条断言 `updated_by == "offer-price-cs"`（认证态）——**实测为假**：
        # `Order.updated_by` 走 `self._actor(request)`，而它取的是**请求体的 `actor`**
        # （缺省空串）。认证态只进留痕（见 `test_change_is_recorded_...` 的 `log.actor`）。
        # 与 T3 的 `test_the_two_actor_sources_are_kept_apart` 同一口径。
        body = self._post(no, {"price": "66.00", "actor": "改价客服"})
        self.assertEqual(body["code"], 0, body)
        after = self._row(no)
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.status, "UNASSIGNED")
        self.assertEqual(after.hall_price, Decimal("66.00"))
        self.assertEqual(after.updated_by, "改价客服")
        # spec §8 第 10 条还要求 `updated_at` 变化（原先只断了两列中的一列）。
        self.assertGreater(after.updated_at, before.updated_at)

    def test_change_is_recorded_with_old_new_and_the_percent_reason(self):
        no = self._create()
        self._seed_old_price(no)
        self._post(no, {"price": "50%"})
        log = OperationLog.objects.get(action="order.change_offer_price", order_no=no)
        self.assertEqual(log.field_name, "hall_price")
        self.assertEqual(log.from_value, "1.00")
        self.assertEqual(log.to_value, "44.00")
        self.assertEqual(log.actor, "offer-price-cs")
        self.assertEqual(log.log_type, 10)
        self.assertIn("50%", log.reason)

    def test_amount_tier_records_an_empty_reason(self):
        no = self._create()
        self._seed_old_price(no)
        self._post(no, {"price": "66.00"})
        log = OperationLog.objects.get(action="order.change_offer_price", order_no=no)
        self.assertEqual(log.from_value, "1.00")
        self.assertEqual(log.to_value, "66.00")
        self.assertEqual(log.reason, "")

    def test_same_value_writes_no_new_log_row_but_still_saves(self):
        no = self._create()
        self._seed_old_price(no, "44.00")
        # ⚠️ 同 `test_it_does_not_touch_the_status`：`updated_by` 取**请求体** `actor`
        # （不是认证态），所以这里显式送一个与登录名不同的值再断它。
        body = self._post(no, {"price": "44.00", "actor": "最后确认的人"})
        self.assertEqual(body["code"], 0, body)
        self.assertFalse(
            OperationLog.objects.filter(action="order.change_offer_price", order_no=no).exists()
        )
        self.assertEqual(self._row(no).updated_by, "最后确认的人")

    # ── 写入点与清理路径（spec §8 第 12 条）──────────────────────────────
    def test_price_and_withdraw_and_clear_taker_leave_it_alone(self):
        no = self._create(total_price="88.00")
        self._post(no, {"price": "66.00"})
        # `/price/` 只写 `total_price`
        body = self.client.post(
            f"{BASE}/{no}/price/", {"total_price": "99.00"}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row(no).hall_price, Decimal("66.00"))
        # 撤回大厅：先抛单再撤回
        body = self.client.post(
            f"{BASE}/{no}/throw-to-hall/", {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        body = self.client.post(f"{BASE}/{no}/withdraw/", {}, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row(no).hall_price, Decimal("66.00"))

    def test_clear_taker_after_an_assign_with_a_price_leaves_it_alone(self):
        """`/clear-taker/` 不回退 `hall_price`（spec §8 第 12 条逐条枚举的那一条）。

    ⚠️ 前置哨兵：`/assign/` 带价时**真的把 55.00 写进去**——否则「清完之后仍是 66」
    这种断言在「从头到尾没被改过」时也会通过（非判别性）。
"""
        taker = User.objects.create_user(username="offer-price-clear-taker")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        no = self._create()
        # ⚠️ 计划里的夹具没给这笔单一个已有的 `hall_price` ⇒ `/assign/` 在 `set_taker`
        # 重算 `temp_amount` 时读 `effective_taker_price`（覆盖价为空 ⇒ 回落到
        # `order.hall_price`）会抛 `AmountError`「该单没有接单价，请联系客服」⇒
        # 业务码 **1000**（实测）。这不属于本任务要覆盖的形状，先垫一个旧值。
        self._seed_old_price(no, "88.00")
        body = self.client.post(
            f"{BASE}/{no}/assign/",
            {"user_id": taker.pk, "price": "55.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))
        body = self.client.post(
            f"{BASE}/{no}/clear-taker/", {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))

    def test_assign_with_a_price_is_still_a_write_point(self):
        """既有行为（`/assign/` 带价时写 `hall_price`）：防顺手改坏。

    夹具形状照 `tests/test_dispatch_api.py::test_assign_sets_taker_and_status`（`:242-256`）：
    接单人要是本租户里**已存在**的账号。带价的闸门（实测 `order_views.py:1048`）是
    **RBAC 节点** `order.submit_payout_price` 的 `has_node_now(...)`，不是 legacy 的
    Django codename `business.override_taker_price`（`order_views.py:856` 的注释明写
    「**不是** legacy 的 `business.override_taker_price`」）——所以这里靠夹具的 `nodes=`
    过闸，不给端点开口子、也不补挂那个 legacy 权限。
"""
        taker = User.objects.create_user(username="offer-price-taker")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        no = self._create()
        # ⚠️ 同上：`/assign/` 带价这条路要订单**已经有** `hall_price`
        # （`set_taker` 重算 `temp_amount` 时读它；为空 ⇒ 1000「该单没有接单价」）。
        self._seed_old_price(no, "88.00")
        body = self.client.post(
            f"{BASE}/{no}/assign/",
            {"user_id": taker.pk, "price": "55.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))

    # ── 连带后果（spec §8 第 13 条）─────────────────────────────────────
    def test_assigning_with_a_price_recomputes_the_temp_amount_from_the_new_hall_price(self):
        """新语义（spec §4.2 / §4.3，D-0147）：带 `price` 的 `/assign/` **写价后**再重算 `temp_amount`。

    ⚠️ **本条在 2026-09-24 被反转**（D-0147，提交信息里也说明了这次语义反转）。
    原名 `test_a_main_coverage_price_keeps_the_hall_effective_price_and_keeps_temp_amount`，
    断言「改价**不**重算 `temp_amount`」——那是本批之前的现状，与 spec §4.2
    「写价后必须再重算一次」直接冲突。改后的判据是**重算确实发生**：
    `temp == 有效基价 × 提成率`，而旧接单人的覆盖价被 `reset_taker_performance` 清掉
    ⇒ 有效基价就是**新的发单价**（不是旧覆盖价 55.00、也不是旧发单价 88.00）。

    ⚠️ 原用例驱动的是 `/hall-price/`；spec §6.1 的 T5 把这条判据挂在 `/assign/` 上
    （本批唯一改动的写价路径，`/hall-price/` 的语义本批不动），所以这里改为驱动 `/assign/`。
    ⚠️ **上句括号里的「`/hall-price/` 的语义本批不动」在 2026-09-24 被裁定 R-1 作废**
    （spec §4.2 的 ⛔ 更正块）：两个写价路径现在**都要**重算；`/hall-price/` 那侧的判据
    见下面新增的四条用例。本条仍按 T5 驱动 `/assign/`。
    夹具形状照 `test_assign_with_a_price_is_still_a_write_point`：带价这条路要订单
    **已经有** `hall_price`（`set_taker` 的第一次重算会读它）。
"""
        old_taker = User.objects.create_user(username="offer-price-old-taker")
        taker = User.objects.create_user(username="offer-price-recalc-taker")
        StaffProfile.objects.create(
            user=taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(old_taker, self.game)
        grant_games(taker, self.game)
        no = self._create(total_price="88.00")
        self._seed_old_price(no, "88.00")
        perf = Performance.objects.get(order=self._row(no), emp_type=EmpType.MAIN)
        perf.user = old_taker
        perf.temp_amount = Decimal("80.00")
        perf.taker_price = Decimal("70.00")
        perf.save(update_fields=["user", "temp_amount", "taker_price"])

        body = self.client.post(
            f"{BASE}/{no}/assign/",
            {"user_id": taker.pk, "price": "55.00"},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))
        # 旧覆盖价被**先回退** ⇒ 有效基价 = 新发单价（55.00），不是 70.00 / 88.00
        self.assertIsNone(perf.taker_price)
        self.assertEqual(perf.user_id, taker.id)
        self.assertEqual(effective_taker_price(perf), Decimal("55.00"))
        self.assertEqual(perf.temp_amount, Decimal("11.00"))     # 55.00 × 20%

    # ── `/hall-price/` 自己也要重算（spec §4.2 的 ⛔ 更正块，裁定 R-1）──────
    # 「预计绩效 = 发单价 × 提成率」是全局不变式；只要还剩一条能改 `hall_price` 而不同步
    # `temp_amount` 的路径，它就会被证伪 ⇒ 本动作写价后同样要重算。下面四条逐档钉住那张表
    # （其中第二条是**恢复**本批之前被删掉的那条
    # `…keeps_the_hall_effective_price_and_keeps_temp_amount` 的场景，按裁定改用新名字：
    # 「覆盖价优先 ⇒ 重算幂等」这条判据在新行为下**仍然成立**）。
    def _seed_main_row(self, no, *, user, temp_amount, taker_price=None):
        """给该单的 MAIN 绩效行写一个**确定的**接单人与金额（合成值），供重算判据对拍。"""
        perf = Performance.objects.get(order=self._row(no), emp_type=EmpType.MAIN)
        perf.user = user
        perf.temp_amount = temp_amount
        perf.taker_price = taker_price
        fields = ["user", "temp_amount"]
        if taker_price is not None:
            fields.append("taker_price")
        perf.save(update_fields=fields)
        return perf

    def test_hall_price_change_recomputes_temp_amount_for_the_current_taker(self):
        """(a) 有接单人、**无**覆盖价 ⇒ 改 `/hall-price/` 后按新发单价重算。

    判别力：把动作体里那段重算删掉 ⇒ `temp_amount` 停在按旧价 88.00 算出的 17.60，红。
"""
        taker = User.objects.create_user(username="hall-price-recalc-taker")
        StaffProfile.objects.create(
            user=taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        no = self._create(total_price="88.00")
        perf = self._seed_main_row(
            no, user=taker, temp_amount=Decimal("17.60")     # 88.00 × 20% = 旧价下的值
        )
        self._seed_old_price(no, "88.00")
        body = self._post(no, {"price": "55.00"})
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))
        self.assertIsNone(perf.taker_price)
        self.assertEqual(effective_taker_price(perf), Decimal("55.00"))
        self.assertEqual(perf.temp_amount, Decimal("11.00"))     # 55.00 × 20%

    def test_hall_price_change_keeps_temp_amount_when_a_taker_price_override_is_set(self):
        """(b) 有接单人、**有** `taker_price` 覆盖 ⇒ 预计绩效与有效价都**不变**。

    这是本批之前那条
    `test_a_main_coverage_price_keeps_the_hall_effective_price_and_keeps_temp_amount`
    的**同一场景**（按裁定 R-1 用新名字恢复，⛔ 不是删掉不测）：`taker_price` 覆盖时有效基价
    仍是覆盖价 ⇒ 写价后的重算是**幂等**的，所以「覆盖价优先」这条判据在新行为下仍然成立。
    判别力：若重算错误地改成只读订单 `hall_price`（绕过覆盖价）⇒ `temp_amount` 会落 11.00，红。
"""
        taker = User.objects.create_user(username="hall-price-override-taker")
        StaffProfile.objects.create(
            user=taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        no = self._create(total_price="88.00")
        perf = self._seed_main_row(
            no,
            user=taker,
            temp_amount=Decimal("14.00"),                    # 70.00 × 20% = 覆盖价下的值
            taker_price=Decimal("70.00"),
        )
        self._seed_old_price(no, "88.00")
        body = self._post(no, {"price": "55.00"})
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))
        self.assertEqual(effective_taker_price(perf), Decimal("70.00"))
        self.assertEqual(perf.temp_amount, Decimal("14.00"))

    def test_hall_price_change_keeps_temp_amount_while_the_order_is_in_the_pool(self):
        """(c) **无**接单人（`user` 为空，池内那一档）⇒ `temp_amount` 不变。

    池内那张行上的 `temp_amount` 是「录单即建」的估值，不由发单价导出 ⇒ 不动它。
    哨兵：先断 `user_id is None` 与一个**非 0** 的初值——否则「0 改成 0」会让这条断言
    在「重算被误加到这里」时也通过（非判别性）。
"""
        no = self._create(total_price="88.00")
        perf = self._seed_main_row(no, user=None, temp_amount=Decimal("88.00"))
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)
        self._seed_old_price(no, "88.00")
        body = self._post(no, {"price": "55.00"})
        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))
        self.assertEqual(perf.temp_amount, Decimal("88.00"))

    def test_a_broken_commission_config_rejects_the_whole_price_change(self):
        """(d) 有接单人但提成**算不出**（固定金额档）⇒ **整笔拒绝改价**（D-0254，2026-10-18）。

    ⛔ **本用例的期望按 D-0254 改过**（口径钉，不是自洽判据）：原名
    `test_the_recompute_is_skipped_when_the_commission_cannot_be_computed`，
    钉的是旧口径（spec 差异 V-H：价照写、跳过重算、响应仍成功）——业务方
    裁定改为**不吞**：整笔回滚 + 业务错误信封。四条判据：
    ① 失败信封：code `1000`（同模块 `assign_order` 的 `set_taker` / 重算两支与
    `transition_order` 的钩子支对这两个异常用的同一档 `exc.code`=1000）+ 指定文案；
    ② `hall_price` 未变；③ 绩效 `temp_amount` 未变；
    ④ 无「改价成功」留痕（`order.change_offer_price` 不落行）。
    判别力：改回 `except: pass`（价照写）⇒ ①④ 红；去掉 `set_rollback`（块内 return
    提交半笔）⇒ ②③ 红。
"""
        taker = User.objects.create_user(username="hall-price-fixed-taker")
        StaffProfile.objects.create(
            user=taker,
            commission_mode=CommissionMode.FIXED,
            commission_value=Decimal("5.00"),
        )
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        no = self._create(total_price="88.00")
        perf = self._seed_main_row(no, user=taker, temp_amount=Decimal("66.00"))
        self._seed_old_price(no, "88.00")
        body = self._post(no, {"price": "55.00"})
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "提成算不出，改价未生效（请先检查提成配置）", body)
        self.assertEqual(self._row(no).hall_price, Decimal("88.00"))
        perf.refresh_from_db()
        self.assertEqual(perf.temp_amount, Decimal("66.00"))
        self.assertFalse(
            OperationLog.objects.filter(action="order.change_offer_price", order_no=no).exists()
        )

    def test_the_order_row_is_locked_before_the_main_performance_row(self):
        """⭐ I4（终审修复轮）：`/hall-price/` 统一锁序 —— 先锁 Order、再锁 Performance。

    判据是**这次调用发出的 SQL 顺序**（不是「跑两线程看死不死」：死锁不保证复现）。
    判别力：把 `change_hall_price` 里的 `lock_order_row(order)` 删掉（或挪到绩效行锁之后）
    ⇒ 本用例以「没有锁订单行 / 锁序反了」当场红。
"""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.lock_order_helpers import assert_order_locked_before_performance

        taker = User.objects.create_user(username="hall-price-lock-taker")
        StaffProfile.objects.create(
            user=taker,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        grant_games(taker, self.game)
        no = self._create(total_price="88.00")
        self._seed_main_row(no, user=taker, temp_amount=Decimal("17.60"))
        self._seed_old_price(no, "88.00")
        with CaptureQueriesContext(connection) as captured:
            body = self._post(no, {"price": "55.00"})
        self.assertEqual(body["code"], 0, body)
        assert_order_locked_before_performance(self, captured)
        # 哨兵：改价真的成功了（锁序断言不是在一笔失败的调用上做的）。
        self.assertEqual(self._row(no).hall_price, Decimal("55.00"))


class HallPriceIsolationTests(TenantTestCase):
    """spec §8 第 18 条：跨租户 ⇒ 404 且对方那一行两列都未变（判据用**订单号**）。

    ⚠️ 第二个租户在 `setUpClass` 里建（在一个用例内部建 schema 会与外层事务相冲，
    与 `tests/test_account_api.py:683-692` 同一手法）；`provision_tenant` 的形参是
    **keyword-only**（`schema_name` / `name` / `domain`），返回 `(tenant, domain)`。
"""

    OTHER_NO = "20260923000000000901"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _domain_b = provision_tenant(
            schema_name="order_price_b", name="改发单价租户 B", domain="order-price-b.test.local"
        )

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # ⚠️ 计划里的 `nodes` 只有 `NODE` ⇒ 跨租户那条用例的第一腿（`payment-status`）
        # 会先被**类级门**拦成 403（权限装配先于 `get_object()`），跑不到「404」那一层。
        # 两个节点都给上，才真的在测「跨租户取不到行」。
        self.client = make_logged_in_client(
            username="offer-price-a",
            groups=("客服",),
            nodes=(NODE, "order.list", "order.change_payment_status"),
        )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="原神")
            Order.objects.create(
                game=game_b,
                order_no=self.OTHER_NO,
                status=OrderStatus.UNASSIGNED,
                payment_status="SETTLING",
                hall_price=Decimal("7.00"),
            )

    def test_cross_tenant_calls_are_rejected_and_leave_the_other_row_alone(self):
        """⚠️ **实测与 spec §5.4 / 计划不符**：spec 写「订单号不属于当前租户 ⇒ 404」，
    而本仓的既有契约是 `get_object()` 抛 `NotFound` 经
    `apps/core/exceptions.py:35` 翻成 **HTTP 200 + `code=1002`**
    （既有钉子：`tests/test_order_api.py::test_missing_order_returns_chinese_message`，
    原文就是「404 分支……」+ 断 `code == 1002`）。这里按**实测**断 1002——
    它仍然满足本条要点（跨租户取不到对方那行、且那行两列未变），
    只是不把 HTTP 状态码当契约。
"""
        for path, payload in (
            ("payment-status", {"payment_status": "SETTLED"}),
            ("hall-price", {"price": "99.00"}),
        ):
            body = self.client.post(
                f"{BASE}/{self.OTHER_NO}/{path}/",
                payload,
                content_type="application/json",
            ).json()
            self.assertEqual(body["code"], 1002, (path, body))
            self.assertEqual(body["msg"], "记录不存在或已被删除", (path, body))
        with tenant_context(self.tenant_b):
            other = Order.objects.get(order_no=self.OTHER_NO)
            self.assertEqual(other.payment_status, "SETTLING")
            self.assertEqual(other.hall_price, Decimal("7.00"))

    def test_the_other_tenant_row_is_not_in_our_list(self):
        body = self.client.get(BASE + "/").json()
        self.assertEqual(body["code"], 0, body)
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(self.OTHER_NO, nos)


class HallPricePermissionTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="offer-price-holder", groups=("客服",), nodes=(NODE, "order.add_edit")
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改。`setUp` 里这位 holder 要先录出一张单（录单写侧过
        # `assert_game_allowed`），后面两条用例才谈得上「403 的账号点不到这张单」——
        # 缺档案时 `_create` 拿到的是 `data=None`，两条用例**都在 setUp 里**以
        # `TypeError` 炸掉（不是它们想测的那个 403 / 401）。
        # ⛔ 补的是**这个 holder**（发请求造单的人），不是那两个反向账号：
        # `offer-price-nobody` / 匿名本来就是「没有该节点」的构造物。
        grant_games(User.objects.get(username="offer-price-holder"), self.game)
        self.no = self._create()

    def _create(self):
        body = self.client.post(
            BASE + "/",
            {"game": self.game.id, "project_text": "满星", "total_price": "88.00"},
            content_type="application/json",
        ).json()
        return body["data"]["order_no"]

    def test_a_user_without_the_node_gets_403(self):
        # 内置组「客服」的 Role 是租户级共享的，setUp 已经把 NODE 挂上去，同组的人都会放行。
        # 反向账号用非内置组名，持 `order.list`（同一次 GET /orders/ 200 是阳性对照）、
        # 不持 `order.set_offer_price`。把 `nodes` 改回 `()` ⇒ 阳性对照当场红（403 != 200）。
        nobody = make_logged_in_client(
            username="offer-price-nobody", groups=("旁观组",), nodes=("order.list",)
        )
        # 阳性对照：同一账号**读得动列表**（它确实持有 `order.list`）⇒ 这个账号不是空节点集，
        # 下面拿到的 403 只可能来自「缺 `order.set_offer_price`」。
        self.assertEqual(nobody.get(BASE + "/").status_code, 200)
        resp = nobody.post(
            f"{BASE}/{self.no}/hall-price/", {"price": "1.00"}, content_type="application/json"
        )
        body = resp.json()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(body["code"], 403, body)

    def test_anonymous_gets_401(self):
        anon = Client(SERVER_NAME="tenant.test.com")
        resp = anon.post(
            f"{BASE}/{self.no}/hall-price/", {"price": "1.00"}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401)
