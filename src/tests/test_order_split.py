"""批次 4 线 B：**拆单**（录单入口 + 详情页入口 + 列表读面）。

业务方原话：「加拆单功能 并且在订单列表可以展开查看拆单 **而不是排序位置不一致**，
也就是说拆出来的订单要有个拆的标记这个可以抄对标，但是**拆出来的订单必须和原订单在一块**
也就是说可以展开 但是尽量少动原有结构」。

## 本文件的判据分四组

1. `SplitIntakeTests` —— **录单**（`POST /orders/` 带 `split: true`）：N 笔同组、价格不变式、
   空段 / N ≤ 1 的兜底、**任一笔失败 ⇒ 一笔都不落**。
2. `SplitEndpointTests` —— **详情页**（`POST /orders/<单号>/split/`，**推断级设计**）：
   原单成组内第一笔、连拆两次仍是同一个组、四条拒绝路径（都不是 500）、节点门、留痕。
3. `SplitListReadTests` —— **列表读面**：折叠规则（在**当次筛选结果集**内做）、
   `split_members` / `split_id` 两个键、`?split_id=` 展开不折叠、`summary` 与折叠**同一份 queryset**、
   子单**仍然可达**（详情端点既有入口不动）。
4. `SplitModelShapeTests` —— 模型形状（**只加一列**，⛔ 没有 `is_split` 布尔列、⛔ 没有父子外键）。
5. 两条**结构网**（各自独立成类，⛔ 不是行为判据）：
   - `SplitRestrictToSelfCreatedTests`：折叠必须在 `scope_by_creator` **之后**（P0 回归）；
   - `MoneyFieldEnumerationTests`：`Order` 的实际金额列 ↔ `MONEY_FIELDS` ↔ `SPLIT_MEMBER_ZERO_PRICES`
     ↔ 本文件的 `PRICE_INPUT` 四处**恰好一致**（E-0027 的形状：将来加价格列而漏进清单 ⇒ 当场红）。

## 纪律

- 断言打在**数据库行**与**真实出口**上（E-0009）：值一律从库里的行 / 端点的载荷里读，
  ⛔ 不拿内存对象当证据。
- 期望值写**字面量**（E-0024）：价格不变式按**列清单**逐条比对，⛔ 不在测试里重算「算式」。
- 边界一律 `is not None` / `== 0`（E-0021）：本线大量涉及 **0**（子单价格全 0），
  真值判断会把 0 当成「没值」。
- 夹具 `make_logged_in_client` 默认给「当前租户全部游戏」（D-0136）⇒ 本文件**不额外声明游戏权限**；
  要测「没有权限」的那一条**显式 opt-out**（不挂节点）。
"""
from datetime import datetime, timedelta
from datetime import timezone as datetime_utc
from decimal import Decimal
from unittest import mock
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db.models import DecimalField
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business import xgj_ship
from apps.business.commission import CommissionError
from apps.business.models import (
    MONEY_FIELDS,
    AlipaySettlementAuthorization,
    EmpType,
    Game,
    OnlinePayrollBatch,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SendResult,
    SettleStatus,
    TaobaoOrderSendLog,
)
from apps.business.order_intake import SPLIT_MEMBER_ZERO_PRICES
from apps.business.settlement_hooks import customer_commission_amount
from tests.p5_auth_helpers import make_logged_in_client

CREATE_URL = "/api/business/orders/"
LIST_URL = "/api/business/orders/"

#: 录单 / 详情页两个入口都要的节点门（`ORDER_PERMISSIONS` 的 `create` 与 `split`）。
ADD_EDIT = "order.add_edit"

#: `/price/` 与 `/batch/price/` 的节点门（G2 那组用例要走这两个端点）。
PRICE_NODES = ("order.submit_payout_price", "order.edit_payout_price")

#: 绩效行上「钱」的列（ 的价格不变式也覆盖它们——录单时一并写入）：
#: `temp_amount`（录单时 = `Order.hall_price` 的估值）/ `bonus_amount`（录单恒 0）/
#: `taker_price`（录单不写，恒空）。
PERFORMANCE_PRICE_FIELDS = ("temp_amount", "bonus_amount", "taker_price")


class _SplitFixture(TenantTestCase):
    """共用夹具：一个真实登录的客服 + 录单工具。"""

    def setUp(self):
        # ⚠️ 顺序承重：游戏先建、`make_logged_in_client` 后建 —— 后者的默认值是
        # 「把**当前存在的**全部游戏」勾给这个账号（D-0136）。
        self.game = Game.objects.create(name="原神")
        self.client = make_logged_in_client(
            username="split-cs",
            groups=("客服",),
            # 本文件走到的门：录单 / 拆分（同一个 `order.add_edit`）、列表、详情，
            # 写入点回查要的软删（`order.delete`）与恢复（`order.recycle`），
            # 以及 G2 那组要的改价两节点（单笔 / 批量同一个 code）。
            nodes=(
                ADD_EDIT, "order.list", "order.view_detail", "order.delete",
                "order.recycle", *PRICE_NODES,
            ),
        )
        self.base = "/api/business/orders"

    def _create(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "满星",
            "total_price": "199.50",
        }
        payload.update(overrides)
        return self.client.post(self.base + "/", payload, content_type="application/json")

    def _post_split(self, order_no, projects):
        return self.client.post(
            f"{self.base}/{order_no}/split/",
            {"projects": projects},
            content_type="application/json",
        )

    def _post_peer_split(self, order_no, remaining, new_orders, *, existing_orders=None):
        payload = {"remaining_project_text": remaining, "new_orders": new_orders}
        if existing_orders is not None:
            payload["existing_orders"] = existing_orders
        return self.client.post(
            f"{self.base}/{order_no}/split-peer/",
            payload,
            content_type="application/json",
        )


class PeerSplitTests(_SplitFixture):
    def test_completed_unsettled_split_preserves_completion_shipping_and_group_net(self):
        source = self._create(project_text="原项目", total_price="80.00").json()["data"]
        order = Order.objects.get(order_no=source["order_no"])
        seller = get_user_model().objects.create_user(username="split-completed-sales")
        StaffProfile.objects.create(user=seller, commission_mode=CommissionMode.PROFIT_PERCENT,
                                    commission_value=Decimal("40.00"))
        completion = timezone.now() - timedelta(hours=1)
        Order.objects.filter(pk=order.pk).update(
            sales_user=seller, status=OrderStatus.COMPLETED, completed_at=completion,
            refund_fee=Decimal("5.00"),
        )
        sales_row = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        Performance.objects.filter(pk=sales_row.pk).update(user=seller, bonus_amount=Decimal("30.00"))
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(deleted_at=completion)
        TaobaoOrderSendLog.objects.create(order_no=order.order_no, tb_order_no="22000000000000000991",
                                         result=SendResult.SUCCESS, sent_at=completion)
        detail = self.client.get(f"{self.base}/{order.order_no}/").json()["data"]
        self.assertIs(detail.get("can_continue_split"), True)

        body = self._post_peer_split(
            order.order_no, "剩余项目", [{"project_text": "拆出的项目", "total_price": "20.00"}],
        ).json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        sales_row.refresh_from_db()
        child = Order.objects.get(order_no=body["data"]["created"][0]["order_no"])
        self.assertEqual((order.status, order.completed_at, order.total_price, order.refund_fee),
                         (OrderStatus.COMPLETED, completion, Decimal("60.00"), Decimal("5.00")))
        self.assertEqual((child.status, child.total_price, child.refund_fee, child.split_ship_main_order_no),
                         (OrderStatus.UNASSIGNED, Decimal("20.00"), Decimal("0.00"), order.order_no))
        self.assertEqual(sales_row.bonus_amount, Decimal("22.00"))

        child.status = OrderStatus.COMPLETED
        child.save(update_fields=["status"])
        with mock.patch.object(xgj_ship, "schedule_auto_ship") as schedule:
            xgj_ship.schedule_auto_ship_gated(child)
        schedule.assert_not_called()
        self.assertEqual(TaobaoOrderSendLog.objects.filter(result=SendResult.SUCCESS).count(), 1)

    def test_completed_split_refuses_settlement_evidence_and_reserved_payroll_for_both_roles(self):
        payer = AlipaySettlementAuthorization.objects.create(request_id=uuid4())
        actor = get_user_model().objects.get(username="split-cs")
        batch = OnlinePayrollBatch.objects.create(
            request_id=uuid4(), category=OnlinePayrollBatch.Category.TAKER, authorization=payer, created_by=actor,
        )
        for staff_type, evidence in (
            (EmpType.MAIN, {"settle_status": SettleStatus.SETTLED}),
            (EmpType.SALES, {"settled_at": timezone.now()}),
            (EmpType.SALES, {"settled_amount": Decimal("0.00")}),
            (EmpType.MAIN, {"online_payroll_batch": batch}),
            (EmpType.MAIN, {"deleted_at": timezone.now(), "settled_amount": Decimal("0.00")}),
        ):
            with self.subTest(staff_type=staff_type, evidence=evidence):
                source = self._create(project_text="原项目", total_price="80.00").json()["data"]
                order = Order.objects.get(order_no=source["order_no"])
                order.status = OrderStatus.COMPLETED
                order.save(update_fields=["status"])
                Performance.objects.filter(order=order, emp_type=staff_type).update(**evidence)
                detail = self.client.get(f"{self.base}/{order.order_no}/").json()["data"]
                self.assertIs(detail.get("can_continue_split"), False)
                count_before = Order.objects.count()
                body = self._post_peer_split(
                    order.order_no, "不得修改", [{"project_text": "不得创建", "total_price": "20.00"}],
                ).json()
                self.assertEqual(body["code"], 1000, body)
                order.refresh_from_db()
                self.assertEqual((order.project_text, order.total_price, order.split_id),
                                 ("原项目", Decimal("80.00"), ""))
                self.assertEqual(Order.objects.count(), count_before)

    def test_completed_member_split_keeps_settled_anchor_intact(self):
        source = self._create(project_text="原项目", total_price="80.00").json()["data"]
        group = self._post_peer_split(source["order_no"], "组首剩余", [
            {"project_text": "待继续拆单", "total_price": "20.00"},
            {"project_text": "另一组员", "total_price": "10.00"},
        ]).json()["data"]
        anchor, order, sibling = Order.objects.filter(split_id=group["split_id"]).order_by("pk")
        Order.objects.filter(pk__in=[anchor.pk, order.pk]).update(status=OrderStatus.COMPLETED)
        Performance.objects.filter(order=anchor, emp_type=EmpType.SALES).update(settled_amount=Decimal("0.00"))
        draft = [{"project_text": "新子单", "total_price": "5.00"}]
        blocked = self._post_peer_split(order.order_no, "剩余", draft, existing_orders=[
            {"order_no": sibling.order_no, "total_price": "15.00"},
        ]).json()
        self.assertEqual(blocked["code"], 1000, blocked)
        self.assertEqual(list(Order.objects.filter(split_id=group["split_id"]).order_by("pk").values_list(
            "total_price", flat=True,
        )), [Decimal("50.00"), Decimal("20.00"), Decimal("10.00")])

        split = self._post_peer_split(order.order_no, "剩余", draft).json()
        self.assertEqual(split["code"], 0, split)
        self.assertEqual(list(Order.objects.filter(split_id=group["split_id"]).order_by("pk").values_list(
            "total_price", flat=True,
        )), [Decimal("50.00"), Decimal("15.00"), Decimal("10.00"), Decimal("5.00")])

    def test_late_legacy_split_defers_unsettled_sales_and_adjustments_until_all_members_complete(self):
        source = self._create(project_text="原项目", total_price="80.00").json()["data"]
        legacy = self._post_split(source["order_no"], ["原单", "旧子单"]).json()
        self.assertEqual(legacy["code"], 0, legacy)
        members = Order.objects.filter(split_id=legacy["data"]["split_id"])
        members.update(status=OrderStatus.COMPLETED)
        order = members.get(order_no=source["order_no"])
        seller = get_user_model().objects.create_user(username="split-legacy-sales")
        StaffProfile.objects.create(user=seller, commission_mode=CommissionMode.PROFIT_PERCENT,
                                    commission_value=Decimal("40.00"))
        order.sales_user = seller
        order.save(update_fields=["sales_user"])
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        sales.user = seller
        sales.bonus_amount = Decimal("32.00")
        sales.save(update_fields=["user", "bonus_amount"])
        adjustment = PerformanceAdjustment.objects.create(
            performance=sales, kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("3.00"), reason="历史调整",
        )
        finance = make_logged_in_client(username="split-finance", is_superuser=True)
        split = self._post_peer_split(order.order_no, "剩余", [
            {"project_text": "末单", "total_price": "10.00"},
        ]).json()
        self.assertEqual(split["code"], 0, split)
        self.assertEqual(split["data"]["split_kind"], "LEGACY")

        def settle():
            return finance.post(
                "/api/business/settlements/settle", {"performance_ids": [sales.pk]}, content_type="application/json",
            ).json()

        self.assertEqual(settle()["code"], 1006)
        payroll = finance.post("/api/business/online-payroll/preview/", {
            "performance_ids": [sales.pk], "category": "customer_service",
        }, content_type="application/json").json()
        self.assertIn("尚未结账", payroll["msg"])
        self.assertNotEqual(payroll["code"], 0)
        sales.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual((sales.bonus_amount, sales.settle_status, adjustment.settle_status), (
            Decimal("32.00"), SettleStatus.UNSETTLED, SettleStatus.UNSETTLED,
        ))

        child_no = split["data"]["created"][0]["order_no"]
        child = members.get(order_no=child_no)
        child.status = OrderStatus.PENDING_REVIEW
        child.save(update_fields=["status"])
        taker = get_user_model().objects.create_user(username="split-legacy-taker")
        Performance.objects.filter(order=child, emp_type=EmpType.MAIN).update(
            user=taker, temp_amount=Decimal("12.50"),
        )
        completed = finance.post(f"{self.base}/{child_no}/transition/", {
            "target": OrderStatus.COMPLETED,
        }, content_type="application/json").json()
        self.assertEqual(completed["code"], 0, completed)
        sales.refresh_from_db()
        self.assertEqual(sales.bonus_amount, Decimal("27.00"))
        settled = settle()
        self.assertEqual(settled["code"], 0, settled)
        self.assertEqual(settled["data"]["settled_performances"], [sales.pk])
        sales.refresh_from_db()
        self.assertEqual(sales.settled_amount, Decimal("30.00"))
        members.filter(order_no=child_no).update(status=OrderStatus.IN_PROGRESS)
        correction = PerformanceAdjustment.objects.create(
            performance=sales, kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal("2.00"), reason="结账后的调整",
        )
        self.assertEqual(settle()["data"]["settled_adjustments"], [correction.pk])

    def test_completed_legacy_child_can_split_with_unchanged_settled_anchor_taker(self):
        source = self._create(project_text="原项目", total_price="80.00").json()["data"]
        legacy = self._post_split(source["order_no"], ["原单", "旧子单"]).json()["data"]
        priced = self._post_peer_split(source["order_no"], "原单", [
            {"project_text": "待拆已完成子单", "total_price": "20.00"},
        ]).json()["data"]
        anchor = Order.objects.get(order_no=source["order_no"])
        order = Order.objects.get(order_no=priced["created"][0]["order_no"])
        Order.objects.filter(split_id=legacy["split_id"]).update(status=OrderStatus.COMPLETED)
        Performance.objects.filter(order=anchor, emp_type=EmpType.MAIN).update(settled_amount=Decimal("0.00"))
        detail_url = f"{self.base}/{order.order_no}/"
        self.assertIs(self.client.get(detail_url).json()["data"]["can_continue_split"], True)
        draft = [{"project_text": "末单", "total_price": "5.00"}]
        Performance.objects.filter(order=anchor, emp_type=EmpType.SALES).update(settled_amount=Decimal("0.00"))
        self.assertIs(self.client.get(detail_url).json()["data"]["can_continue_split"], False)
        self.assertEqual(self._post_peer_split(order.order_no, "剩余", draft).json()["code"], 1000)
        Performance.objects.filter(order=anchor, emp_type=EmpType.SALES).update(settled_amount=None)

        original_game = anchor.game
        anchor.game = Game.objects.create(name="限权的原单游戏")
        anchor.save(update_fields=["game"])
        restricted = make_logged_in_client(
            username="split-child-game-only", groups=("限拆",),
            nodes=(ADD_EDIT, "order.view_detail"), games=[self.game],
        )
        self.assertIs(restricted.get(detail_url).json()["data"]["can_continue_split"], False)
        denied = restricted.post(f"{self.base}/{order.order_no}/split-peer/", {
            "remaining_project_text": "剩余", "new_orders": draft,
        }, content_type="application/json").json()
        self.assertEqual(denied["code"], 1000, denied)
        self.assertEqual(Order.objects.filter(split_id=legacy["split_id"]).count(), 3)

        anchor.game = original_game
        anchor.save(update_fields=["game"])
        siblings = Order.objects.filter(split_id=legacy["split_id"]).exclude(pk__in=[anchor.pk, order.pk])
        siblings.update(game=Game.objects.get(name="限权的原单游戏"))
        self.assertIs(restricted.get(detail_url).json()["data"]["can_continue_split"], False)
        self.assertEqual(restricted.post(f"{self.base}/{order.order_no}/split-peer/", {
            "remaining_project_text": "剩余", "new_orders": draft,
        }, content_type="application/json").json()["code"], 1000)
        siblings.update(game=original_game)
        self.assertEqual(self._post_peer_split(order.order_no, "剩余", draft).json()["code"], 0)
        anchor.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((anchor.total_price, order.total_price), (Decimal("60.00"), Decimal("15.00")))

    def test_split_transfers_real_price_and_keeps_group_flat(self):
        source = self._create(
            project_text="满星/深渊", total_price="120.00", hall_price="45.00"
        ).json()["data"]
        sales_user = get_user_model().objects.create_user(username="split-sales")
        StaffProfile.objects.create(
            user=sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("40.00"),
        )
        source_order = Order.objects.get(order_no=source["order_no"])
        source_order.sales_user = sales_user
        source_order.save(update_fields=["sales_user"])
        sales_row = Performance.objects.filter(
            order=source_order, emp_type=EmpType.SALES
        ).first()
        sales_row.user = sales_user
        sales_row.save(update_fields=["user"])
        main_row = Performance.objects.get(order=source_order, emp_type=EmpType.MAIN)
        main_row.bonus_amount = Decimal("6.66")
        main_row.save(update_fields=["bonus_amount"])
        response = self._post_peer_split(
            source["order_no"],
            "满星剩余",
            [
                {"project_text": "深渊代打", "total_price": "30.25"},
                {"project_text": "每日任务", "total_price": "10.00"},
            ],
        ).json()
        self.assertEqual(response["code"], 0, response)
        self.assertEqual(response["data"]["split_kind"], "PEER")
        self.assertEqual(response["data"]["source_total_price"], "79.75")
        members = list(Order.objects.filter(split_id=response["data"]["split_id"]).order_by("id"))
        self.assertEqual(len(members), 3)
        self.assertEqual({member.split_kind for member in members}, {"PEER"})
        self.assertEqual([member.project_text for member in members], ["满星剩余", "深渊代打", "每日任务"])
        self.assertEqual([member.total_price for member in members], [
            Decimal("79.75"), Decimal("30.25"), Decimal("10.00")
        ])
        self.assertEqual(sum((member.total_price for member in members), Decimal("0.00")), Decimal("120.00"))
        # MAIN 按 hall_price 算绩效：价格转移不改它；SALES 预计按源单新 total_price 刷新。
        main = Performance.objects.get(order=members[0], emp_type=EmpType.MAIN)
        self.assertEqual(main.temp_amount, Decimal("45.00"))
        sales = Performance.objects.get(
            order=members[0], emp_type=EmpType.SALES, user=sales_user
        )
        self.assertEqual(sales.temp_amount, Decimal("31.90"))
        self.assertEqual(sales.bonus_amount, Decimal("0.00"))
        self.assertEqual(
            Performance.objects.get(order=members[0], emp_type=EmpType.MAIN).bonus_amount,
            Decimal("6.66"),
        )
        self.assertEqual(
            list(Performance.objects.filter(order__in=members[1:]).order_by("order_id", "emp_type").values_list(
                "emp_type", "temp_amount", "bonus_amount"
            )),
            [
                (EmpType.MAIN, Decimal("0.00"), Decimal("0.00")),
                (EmpType.SALES, Decimal("0.00"), Decimal("0.00")),
                (EmpType.MAIN, Decimal("0.00"), Decimal("0.00")),
                (EmpType.SALES, Decimal("0.00"), Decimal("0.00")),
            ],
        )

    def test_peer_member_can_continue_split_without_making_a_main_order(self):
        source = self._create(project_text="原项目", total_price="80.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "剩余一", [{"project_text": "拆出一", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        second = self._post_peer_split(
            child_no, "剩余二", [{"project_text": "拆出二", "total_price": "5.00"}]
        ).json()
        self.assertEqual(second["code"], 0, second)
        self.assertEqual(second["data"]["split_id"], first["data"]["split_id"])
        self.assertEqual(
            set(Order.objects.filter(split_id=first["data"]["split_id"]).values_list("split_kind", flat=True)),
            {"PEER"},
        )
        self.assertEqual(
            sorted(
                Order.objects.filter(split_id=first["data"]["split_id"]).values_list("total_price", flat=True)
            ),
            sorted([Decimal("60.00"), Decimal("15.00"), Decimal("5.00")]),
        )

    def test_existing_peer_member_content_can_be_saved_without_price_changes(self):
        source = self._create(project_text="原项目", total_price="80.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源单剩余", [{"project_text": "已有子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        child = Order.objects.get(order_no=child_no)
        old_price = child.total_price
        updated = self._post_peer_split(
            source["order_no"],
            "源单内容同步修改",
            [],
            existing_orders=[{"order_no": child_no, "project_text": "编辑后的已有子单"}],
        ).json()
        self.assertEqual(updated["code"], 0, updated)
        self.assertEqual(updated["data"]["created"], [])
        self.assertEqual(updated["data"]["updated"], [child_no])
        child.refresh_from_db()
        source_order = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(child.project_text, "编辑后的已有子单")
        self.assertEqual(child.total_price, old_price)
        self.assertEqual(source_order.project_text, "源单内容同步修改")
        self.assertEqual(source_order.total_price, Decimal("60.00"))

    def test_split_peer_adjusts_existing_member_price_and_conserves_group_total(self):
        source = self._create(project_text="源", total_price="120.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        saved = self._post_peer_split(
            source["order_no"],
            "源剩余",
            [],
            existing_orders=[{
                "order_no": child_no,
                "total_price": "30.00",
            }],
        ).json()
        self.assertEqual(saved["code"], 0, saved)
        rows = list(Order.objects.filter(split_id=first["data"]["split_id"]).order_by("id"))
        self.assertEqual([row.total_price for row in rows], [
            Decimal("90.00"), Decimal("30.00")
        ])
        self.assertEqual(rows[1].project_text, "子单")
        self.assertEqual(sum((row.total_price for row in rows), Decimal("0.00")), Decimal("120.00"))

    def test_split_peer_cannot_directly_edit_the_balance_anchor_price(self):
        source = self._create(project_text="源", total_price="120.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        direct_source = self.client.post(
            f"{self.base}/{source['order_no']}/split-peer/",
            {
                "remaining_project_text": "源剩余",
                "source_total_price": "95.00",
                "new_orders": [],
            },
            content_type="application/json",
        ).json()
        direct_existing = self._post_peer_split(
            child_no,
            "子单",
            [],
            existing_orders=[{
                "order_no": source["order_no"],
                "project_text": "源剩余",
                "total_price": "95.00",
            }],
        ).json()
        self.assertEqual(direct_source["code"], 1000, direct_source)
        self.assertEqual(direct_existing["code"], 1000, direct_existing)
        rows = list(Order.objects.filter(split_id=first["data"]["split_id"]).order_by("id"))
        self.assertEqual([row.total_price for row in rows], [
            Decimal("100.00"), Decimal("20.00")
        ])

    def test_split_peer_child_source_price_and_new_order_are_applied_once(self):
        source = self._create(project_text="源", total_price="120.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        saved = self.client.post(
            f"{self.base}/{child_no}/split-peer/",
            {
                "remaining_project_text": "子单剩余",
                "source_total_price": "25.00",
                "new_orders": [{"project_text": "新拆单", "total_price": "10.00"}],
            },
            content_type="application/json",
        ).json()
        self.assertEqual(saved["code"], 0, saved)
        rows = list(Order.objects.filter(split_id=first["data"]["split_id"]).order_by("id"))
        self.assertEqual([row.total_price for row in rows], [
            Decimal("95.00"), Decimal("15.00"), Decimal("10.00")
        ])
        self.assertEqual(sum((row.total_price for row in rows), Decimal("0.00")), Decimal("120.00"))

    def test_split_child_patch_updates_text_and_price_atomically_with_anchor_balance(self):
        source = self._create(project_text="源", total_price="120.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        response = self.client.patch(
            f"{self.base}/{child_no}/",
            {"project_text": "改过的子单", "total_price": "30.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)
        child = Order.objects.get(order_no=child_no)
        anchor = Order.objects.get(order_no=source["order_no"])
        self.assertEqual((child.project_text, child.total_price), ("改过的子单", Decimal("30.00")))
        self.assertEqual(anchor.total_price, Decimal("90.00"))
        self.assertEqual(anchor.total_price + child.total_price, Decimal("120.00"))

    def test_plain_order_patch_cannot_change_total_price(self):
        source = self._create(project_text="普通", total_price="120.00").json()["data"]
        response = self.client.patch(
            f"{self.base}/{source['order_no']}/",
            {"project_text": "不应半保存", "total_price": "30.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 1000, response)
        order = Order.objects.get(order_no=source["order_no"])
        self.assertEqual((order.project_text, order.total_price), ("普通", Decimal("120.00")))

    def test_price_changes_require_existing_payout_permission_without_partial_content_save(self):
        source = self._create(project_text="源", total_price="120.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        limited = make_logged_in_client(
            username="split-no-price-node",
            groups=("限价",),
            nodes=(ADD_EDIT, "order.submit_edit", "order.list", "order.view_detail"),
        )
        split_response = limited.post(
            f"{self.base}/{source['order_no']}/split-peer/",
            {
                "remaining_project_text": "源剩余",
                "new_orders": [],
                "existing_orders": [{
                    "order_no": child_no,
                    "project_text": "不应保存",
                    "total_price": "30.00",
                }],
            },
            content_type="application/json",
        )
        self.assertEqual(split_response.status_code, 403)
        patch_response = limited.patch(
            f"{self.base}/{child_no}/",
            {"project_text": "不应半保存", "total_price": "30.00"},
            content_type="application/json",
        )
        self.assertEqual(patch_response.status_code, 403)
        child = Order.objects.get(order_no=child_no)
        anchor = Order.objects.get(order_no=source["order_no"])
        self.assertEqual((child.project_text, child.total_price), ("子单", Decimal("20.00")))
        self.assertEqual(anchor.total_price, Decimal("100.00"))

    def test_price_rebalancing_rejects_an_anchor_outside_visible_game_scope(self):
        source = self._create(project_text="源", total_price="120.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        anchor = Order.objects.get(order_no=source["order_no"])
        anchor.game = Game.objects.create(name="锚点不可见游戏")
        anchor.save(update_fields=["game"])
        restricted = make_logged_in_client(
            username="split-child-game-only",
            groups=("限价",),
            nodes=(ADD_EDIT, "order.submit_edit", "order.list", "order.view_detail", *PRICE_NODES),
            games=[self.game],
        )
        response = restricted.patch(
            f"{self.base}/{child_no}/",
            {"project_text": "不应保存", "total_price": "30.00"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], 0, response.json())
        child = Order.objects.get(order_no=child_no)
        anchor.refresh_from_db()
        self.assertEqual((child.project_text, child.total_price), ("子单", Decimal("20.00")))
        self.assertEqual(anchor.total_price, Decimal("100.00"))

    def test_split_group_source_content_can_be_saved_without_new_or_existing_rows(self):
        source = self._create(project_text="源单原内容", total_price="80.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源单原内容", [{"project_text": "子单", "total_price": "20.00"}]
        ).json()
        self.assertEqual(first["code"], 0, first)
        saved = self._post_peer_split(source["order_no"], "独立修改源单内容", []).json()
        self.assertEqual(saved["code"], 0, saved)
        source_after = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(source_after.project_text, "独立修改源单内容")
        self.assertEqual(source_after.total_price, Decimal("60.00"))
        self.assertEqual(saved["data"]["created"], [])
        self.assertEqual(saved["data"]["updated"], [])

    def test_existing_member_updates_reject_source_and_non_group_members(self):
        source = self._create(project_text="普通订单").json()["data"]
        rejected = self._post_peer_split(
            source["order_no"],
            "内容",
            [],
            existing_orders=[{"order_no": source["order_no"], "project_text": "伪装组员"}],
        ).json()
        self.assertEqual(rejected["code"], 1000, rejected)
        unchanged = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(unchanged.project_text, "普通订单")
        self.assertEqual(unchanged.split_id, "")

    def test_existing_group_content_only_updates_allow_completed_orders_and_noop_is_rejected(self):
        source = self._create(project_text="源单原内容", total_price="80.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源单原内容", [{"project_text": "已有成员", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        Order.objects.filter(split_id=first["data"]["split_id"]).update(status=OrderStatus.COMPLETED)
        source_sales = Performance.objects.filter(
            order__order_no=source["order_no"], emp_type=EmpType.SALES
        ).first()
        self.assertIsNotNone(source_sales)
        source_sales.temp_amount = Decimal("12.34")
        source_sales.bonus_amount = Decimal("56.78")
        source_sales.settled_at = timezone.now()
        source_sales.save(update_fields=["temp_amount", "bonus_amount", "settled_at"])
        before_source_price = Order.objects.get(order_no=source["order_no"]).total_price
        before_child_price = Order.objects.get(order_no=child_no).total_price
        response = self._post_peer_split(
            source["order_no"],
            "源单新内容",
            [],
            existing_orders=[{"order_no": child_no, "project_text": "已完成成员新内容"}],
        ).json()
        self.assertEqual(response["code"], 0, response)
        source_after = Order.objects.get(order_no=source["order_no"])
        child_after = Order.objects.get(order_no=child_no)
        self.assertEqual(source_after.project_text, "源单新内容")
        self.assertEqual(child_after.project_text, "已完成成员新内容")
        self.assertEqual(source_after.status, OrderStatus.COMPLETED)
        self.assertEqual(child_after.status, OrderStatus.COMPLETED)
        self.assertEqual(source_after.total_price, before_source_price)
        self.assertEqual(child_after.total_price, before_child_price)
        source_sales.refresh_from_db()
        self.assertEqual(source_sales.temp_amount, Decimal("12.34"))
        self.assertEqual(source_sales.bonus_amount, Decimal("56.78"))
        log_count = OperationLog.objects.filter(
            order_no__in=[source["order_no"], child_no], action="order.update"
        ).count()
        source_updated_at = source_after.updated_at
        child_updated_at = child_after.updated_at
        no_op = self._post_peer_split(
            source["order_no"],
            "源单新内容",
            [],
            existing_orders=[{"order_no": child_no, "project_text": "已完成成员新内容"}],
        ).json()
        self.assertEqual(no_op["code"], 1000, no_op)
        self.assertEqual(
            OperationLog.objects.filter(
                order_no__in=[source["order_no"], child_no], action="order.update"
            ).count(),
            log_count,
        )
        source_after.refresh_from_db()
        child_after.refresh_from_db()
        self.assertEqual(source_after.updated_at, source_updated_at)
        self.assertEqual(child_after.updated_at, child_updated_at)

    def test_existing_member_edit_checks_each_gamescope(self):
        source = self._create(project_text="源项目", total_price="80.00").json()["data"]
        first = self._post_peer_split(
            source["order_no"], "源剩余", [{"project_text": "已有成员", "total_price": "20.00"}]
        ).json()
        child_no = first["data"]["created"][0]["order_no"]
        child = Order.objects.get(order_no=child_no)
        child.game = Game.objects.create(name="无权限的游戏")
        child.save(update_fields=["game"])
        response = self._post_peer_split(
            source["order_no"],
            "源剩余",
            [],
            existing_orders=[{"order_no": child_no, "project_text": "越权编辑"}],
        ).json()
        self.assertNotEqual(response["code"], 0, response)
        child.refresh_from_db()
        self.assertEqual(child.project_text, "已有成员")

    def test_order_detail_exposes_split_kind_as_server_owned_data(self):
        source = self._create(project_text="原项目").json()["data"]
        response = self._post_peer_split(
            source["order_no"], "剩余项目", [{"project_text": "拆出项目", "total_price": "1.00"}]
        ).json()
        self.assertEqual(response["code"], 0, response)
        detail = self.client.get(f"{self.base}/{source['order_no']}/").json()
        self.assertEqual(detail["code"], 0, detail)
        self.assertEqual(detail["data"]["split_kind"], "PEER")
        patched = self.client.patch(
            f"{self.base}/{source['order_no']}/",
            {"split_kind": "LEGACY"},
            content_type="application/json",
        ).json()
        self.assertEqual(patched["code"], 0, patched)
        source_row = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(source_row.split_kind, "PEER")

    def test_peer_split_rejects_project_text_longer_than_order_column(self):
        source = self._create(project_text="原项目").json()["data"]
        response = self._post_peer_split(
            source["order_no"],
            "剩余项目",
            [{"project_text": "拆" * 901, "total_price": "1.00"}],
        ).json()
        self.assertEqual(response["code"], 1000, response)
        self.assertEqual(Order.objects.count(), 1)
        source_row = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(source_row.project_text, "原项目")
        self.assertEqual(source_row.total_price, Decimal("199.50"))
        self.assertEqual((source_row.split_id, source_row.split_kind), ("", ""))

    def test_peer_split_rejects_computed_source_price_outside_order_column_range(self):
        source = self._create(
            project_text="原项目", total_price="9999999999.99"
        ).json()["data"]
        log_count = OperationLog.objects.count()
        response = self._post_peer_split(
            source["order_no"],
            "剩余项目",
            [{"project_text": "拆出负价项目", "total_price": "-0.01"}],
        ).json()
        self.assertEqual(response["code"], 1000, response)
        self.assertEqual(response["msg"], "拆分后原订单接单价超出有效金额范围")
        self.assertEqual(Order.objects.count(), 1)
        source_row = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(source_row.total_price, Decimal("9999999999.99"))
        self.assertEqual(source_row.project_text, "原项目")
        self.assertEqual((source_row.split_id, source_row.split_kind), ("", ""))
        self.assertEqual(OperationLog.objects.count(), log_count)

    def test_legacy_group_accepts_transfer_without_changing_legacy_kind_or_existing_member_price(self):
        source = self._create(project_text="原项目").json()["data"]
        legacy = self._post_split(source["order_no"], ["原项目", "新增项目"]).json()
        self.assertEqual(legacy["code"], 0, legacy)
        group = list(Order.objects.filter(split_id=legacy["data"]["split_id"]))
        self.assertEqual({row.split_kind for row in group}, {"LEGACY"})
        existing_child = next(row for row in group if row.order_no != source["order_no"])
        old_child_price = existing_child.total_price
        source_order = Order.objects.get(order_no=source["order_no"])
        source_sales = Performance.objects.filter(
            order=source_order, emp_type=EmpType.SALES
        ).first()
        source_sales.temp_amount = Decimal("12.34")
        source_sales.bonus_amount = Decimal("56.78")
        source_sales.save(update_fields=["temp_amount", "bonus_amount"])
        transferred = self._post_peer_split(
            source["order_no"], "原项目剩余", [{"project_text": "新增价项目", "total_price": "10.00"}]
        ).json()
        self.assertEqual(transferred["code"], 0, transferred)
        self.assertEqual(transferred["data"]["split_kind"], "LEGACY")
        source_order.refresh_from_db()
        new_member_no = transferred["data"]["created"][0]["order_no"]
        self.assertEqual(source_order.total_price, Decimal("189.50"))
        self.assertEqual(Order.objects.get(pk=existing_child.pk).total_price, old_child_price)
        second_transfer = self._post_peer_split(
            new_member_no,
            "新成员的剩余内容",
            [{"project_text": "从已有新成员再拆出", "total_price": "5.00"}],
        ).json()
        self.assertEqual(second_transfer["code"], 0, second_transfer)
        members = list(Order.objects.filter(split_id=legacy["data"]["split_id"]))
        self.assertEqual(len(members), 4)
        self.assertEqual({member.split_kind for member in members}, {"LEGACY"})
        self.assertEqual(sum((member.total_price for member in members), Decimal("0.00")), Decimal("199.50"))
        source_sales.refresh_from_db()
        self.assertEqual(source_sales.temp_amount, Decimal("12.34"))
        self.assertEqual(source_sales.bonus_amount, Decimal("56.78"))

    def test_legacy_customer_commission_keeps_using_group_total_after_transfer(self):
        source = self._create(project_text="原项目", total_price="120.00").json()["data"]
        legacy = self._post_split(source["order_no"], ["原项目", "既有子单"]).json()
        self.assertEqual(legacy["code"], 0, legacy)
        order = Order.objects.get(order_no=source["order_no"])
        sales_user = get_user_model().objects.create_user(username="legacy-split-sales")
        StaffProfile.objects.create(
            user=sales_user,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=Decimal("40.00"),
        )
        order.sales_user = sales_user
        order.save(update_fields=["sales_user"])
        sales_row = Performance.objects.filter(order=order, emp_type=EmpType.SALES).first()
        sales_row.user = sales_user
        sales_row.save(update_fields=["user"])
        order.refund_fee = Decimal("5.00")
        order.save(update_fields=["refund_fee"])
        old_child = Order.objects.exclude(order_no=order.order_no).get(
            split_id=legacy["data"]["split_id"]
        )
        old_child.refund_fee = Decimal("7.00")
        old_child.save(update_fields=["refund_fee"])
        before = customer_commission_amount(order, Decimal("60.00"))
        self.assertEqual(before, Decimal("22.00"))
        transferred = self._post_peer_split(
            order.order_no, "原项目剩余", [{"project_text": "新子单", "total_price": "20.00"}]
        ).json()
        self.assertEqual(transferred["code"], 0, transferred)
        order.refresh_from_db()
        after = customer_commission_amount(order, Decimal("60.00"))
        self.assertEqual(after, Decimal("22.00"))
        group_id = legacy["data"]["split_id"]
        Order.objects.filter(split_id=group_id).update(status=OrderStatus.COMPLETED)
        Performance.objects.filter(
            order__split_id=group_id, emp_type=EmpType.MAIN
        ).update(bonus_amount=Decimal("20.00"))
        from apps.business.settlement_hooks import recompute_customer_commission

        order.refresh_from_db()
        recompute_customer_commission(order, dry_run=False)
        sales_row.refresh_from_db()
        self.assertEqual(sales_row.bonus_amount, Decimal("22.00"))


class PeerSplitDeleteTests(_SplitFixture):
    def _peer_group(self, *, count=2):
        source = self._create(project_text="原项目", total_price="90.00").json()["data"]
        new_orders = [
            {"project_text": f"拆出项目{i}", "total_price": "10.00"}
            for i in range(count)
        ]
        response = self._post_peer_split(source["order_no"], "剩余项目", new_orders).json()
        self.assertEqual(response["code"], 0, response)
        members = list(Order.objects.filter(split_id=response["data"]["split_id"]).order_by("id"))
        return members

    def _delete(self, member):
        return self.client.post(
            f"{self.base}/{member.order_no}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        ).json()

    def test_delete_child_credits_main_once_and_restores_without_transferred_price(self):
        from apps.business.recycle import soft_delete_order

        source, child, sibling = self._peer_group()
        stale_child = Order.objects.get(pk=child.pk)
        actor = get_user_model().objects.get(username="split-cs")
        first = self._delete(child)
        self.assertEqual(first["code"], 0, first)
        soft_delete_order(order=stale_child, actor=actor, is_del_emp="N")
        source.refresh_from_db()
        child.refresh_from_db()
        sibling.refresh_from_db()
        self.assertEqual(source.total_price, Decimal("80.00"))
        self.assertEqual(child.total_price, Decimal("0.00"))
        self.assertEqual(sibling.total_price, Decimal("10.00"))
        self.assertEqual(source.split_id, sibling.split_id)
        self.assertEqual(child.split_id, "")
        price_logs = OperationLog.objects.filter(action="order.change_taker_price")
        self.assertEqual(
            list(price_logs.filter(
                order_no=source.order_no, reason="删除子单返还接单价"
            ).values_list("from_value", "to_value")),
            [("70.00", "80.00")],
        )
        self.assertEqual(
            list(price_logs.filter(order_no=child.order_no).values_list("from_value", "to_value")),
            [("10.00", "0.00")],
        )
        restored = self.client.post(
            f"{self.base}/{child.order_no}/restore/", {}, content_type="application/json"
        ).json()
        self.assertEqual(restored["code"], 0, restored)
        child.refresh_from_db()
        self.assertEqual(child.total_price, Decimal("0.00"))
        self.assertEqual(child.split_id, "")

    def test_delete_credits_negative_legacy_child_price_and_prunes_group(self):
        source = self._create(total_price="100.00").json()["data"]
        split = self._post_split(source["order_no"], ["主单", "子单"]).json()
        self.assertEqual(split["code"], 0, split)
        main, child = Order.objects.filter(split_id=split["data"]["split_id"]).order_by("id")
        child.total_price = Decimal("-5.00")
        child.save(update_fields=["total_price"])

        body = self._delete(child)
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(main.total_price, Decimal("95.00"))
        self.assertEqual(child.total_price, Decimal("0.00"))
        self.assertEqual(main.split_id, "")
        self.assertIsNotNone(child.deleted_at)

    def test_delete_price_overflow_rejected_in_preflight_and_does_not_delete(self):
        source, child = self._peer_group(count=1)
        source.total_price = Decimal("9999999999.99")
        source.save(update_fields=["total_price"])
        dry = self.client.post(
            f"{self.base}/batch/delete/",
            {"order_nos": [child.order_no], "dry_run": True, "isDelEmp": "N"},
            content_type="application/json",
        ).json()
        self.assertEqual(dry["data"]["eligible"], [])
        self.assertIn("超出有效金额范围", dry["data"]["ineligible"][0]["msg"])
        response = self._delete(child)
        self.assertEqual(response["code"], 1000, response)
        source.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(source.total_price, Decimal("9999999999.99"))
        self.assertEqual(child.total_price, Decimal("10.00"))
        self.assertIsNone(child.deleted_at)

    def test_peer_delete_of_earliest_member_is_single_order_and_closes_ready_group_once(self):
        from apps.business import xgj_ship

        source, child_a, child_b = self._peer_group(count=2)
        Order.objects.filter(pk__in=[source.pk, child_a.pk, child_b.pk]).update(
            status=OrderStatus.COMPLETED
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self._delete(source)
        self.assertEqual(body["code"], 0, body)
        source.refresh_from_db()
        child_a.refresh_from_db()
        child_b.refresh_from_db()
        self.assertIsNotNone(source.deleted_at)
        self.assertEqual((source.split_id, source.split_kind), ("", ""))
        self.assertIsNone(child_a.deleted_at)
        self.assertIsNone(child_b.deleted_at)
        self.assertEqual({child_a.split_kind, child_b.split_kind}, {"PEER"})
        self.assertNotEqual(child_a.split_id, "")
        self.assertEqual(child_a.split_id, child_b.split_id)
        dispatch.assert_called_once_with(child_a.order_no)

    def test_peer_prune_to_one_clears_survivor_and_uses_plain_order_ship_path(self):
        from apps.business import xgj_ship

        source, child = self._peer_group(count=1)
        Order.objects.filter(pk__in=[source.pk, child.pk]).update(
            status=OrderStatus.COMPLETED
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self._delete(child)
        self.assertEqual(body["code"], 0, body)
        source.refresh_from_db()
        self.assertEqual((source.split_id, source.split_kind), ("", ""))
        dispatch.assert_called_once_with(source.order_no)

    def test_peer_delete_does_not_ship_until_remaining_members_are_complete(self):
        from apps.business import xgj_ship

        source, child_a, child_b = self._peer_group(count=2)
        Order.objects.filter(pk__in=[source.pk, child_a.pk]).update(
            status=OrderStatus.COMPLETED
        )
        Order.objects.filter(pk=child_b.pk).update(status=OrderStatus.IN_PROGRESS)
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self._delete(child_a)
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_not_called()

    def test_peer_delete_closes_shipping_with_refunded_member_still_in_group(self):
        source, child_a, child_b = self._peer_group(count=2)
        source.status = OrderStatus.COMPLETED
        source.save(update_fields=["status"])
        child_b.status = OrderStatus.REFUNDED
        child_b.save(update_fields=["status"])
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self._delete(child_a)
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_called_once_with(source.order_no)

    def test_peer_delete_does_not_ship_when_only_refunded_members_remain(self):
        source, child_a, child_b = self._peer_group(count=2)
        Order.objects.filter(pk__in=[source.pk, child_b.pk]).update(status=OrderStatus.REFUNDED)
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self._delete(child_a)
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_not_called()

    def test_peer_delete_does_not_duplicate_a_group_already_shipped(self):
        from apps.business import xgj_ship
        from apps.business.models import SendResult, TaobaoOrderSendLog

        source, child_a, child_b = self._peer_group(count=2)
        Order.objects.filter(pk__in=[source.pk, child_a.pk, child_b.pk]).update(
            status=OrderStatus.COMPLETED
        )
        TaobaoOrderSendLog.objects.create(
            order_no=child_a.order_no,
            tb_order_no="22000000000000000991",
            result=SendResult.SUCCESS,
            sent_at=timezone.now(),
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self._delete(child_a)
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_not_called()
        self.assertEqual(TaobaoOrderSendLog.objects.count(), 1)


class SplitIntakeTests(_SplitFixture):
    """**录单**那条入口（对标同款：项目之间用 `/` 分隔）。"""

    #: 一次录单要带的**全部价格列**（`MONEY_FIELDS` 6 个 + 绩效行那 3 个）。
    #: ⚠️ 这份清单**手写**：它是「组内价格之和 == 拆单前原值」这条不变式的**列枚举**，
    #: 从被测代码导出就等于自指（新增价格列时它不会红）。
    PRICE_INPUT = {
        "total_price": "300.00",
        "received_price": "250.00",
        "platform_price": "280.00",
        "hall_price": "88.00",
        "refund_fee": "5.00",
        "fee": "12.00",
    }

    def _group(self, split_id):
        return list(Order.objects.filter(split_id=split_id).order_by("id"))

    def test_legacy_intake_allocates_a_distinct_source_number_for_each_child(self):
        body = self._create(
            project_text="保留/深渊/每日/周常", split=True, source_order_no="LEGACY-SPLIT",
        ).json()
        self.assertEqual(body["code"], 0, body)
        orders = self._group(body["data"]["split_id"])
        self.assertEqual([order.source_order_no for order in orders], [
            "LEGACY-SPLIT", "LEGACY-SPLIT-1", "LEGACY-SPLIT-2", "LEGACY-SPLIT-3",
        ])
        self.assertEqual({order.split_ship_main_order_no for order in orders}, {orders[0].order_no})

    def test_split_intake_skips_numbers_allocated_by_another_group(self):
        first = self._create(
            project_text="原项目/子项目", split=True, source_order_no="SHARED-EXTERNAL",
        ).json()
        self.assertEqual(first["code"], 0, first)
        second = self._create(
            project_text="另一原项目", source_order_no="SHARED-EXTERNAL", split=True,
            split_orders=[{"project_text": "另一子项目", "total_price": "10.00"}],
        ).json()
        self.assertEqual(second["code"], 0, second)
        self.assertNotEqual(first["data"]["split_id"], second["data"]["split_id"])
        self.assertEqual(
            [order.source_order_no for order in self._group(second["data"]["split_id"])],
            ["SHARED-EXTERNAL", "SHARED-EXTERNAL-2"],
        )

    def test_legacy_intake_keeps_child_numbers_within_the_peer_identity_width(self):
        original_source_no = "S" * 64
        body = self._create(
            project_text="保留/子单甲/子单乙", split=True, source_order_no=original_source_no,
        ).json()
        self.assertEqual(body["code"], 0, body)
        orders = self._group(body["data"]["split_id"])
        self.assertEqual(
            [order.source_order_no for order in orders],
            [original_source_no, "S" * 62 + "-1", "S" * 62 + "-2"],
        )

    def test_explicit_split_balances_the_original_and_assigns_only_it(self):
        self.client = make_logged_in_client(
            username="split-intake-assignment",
            groups=("录单拆分",),
            nodes=(ADD_EDIT, "order.assign_taker"),
        )
        make_logged_in_client(username="split-intake-taker", groups=("代练员",), nodes=())
        taker = get_user_model().objects.get(username="split-intake-taker")
        body = self._create(
            project_text="保留 / 原内容",
            split=True,
            split_orders=[
                {"project_text": "子单　甲 / 乙", "total_price": "32.25"},
                {"project_text": "折抵", "total_price": "-2.25"},
                {"project_text": "补做", "total_price": "0.00"},
            ],
            source_order_no="SYNTHETIC-SPLIT",
            order_label="录单标签",
            qr_fetch_suppress_until="2027-01-01T00:00:00+08:00",
            buyer_eid="synthetic-buyer",
            assignee=taker.pk,
            **self.PRICE_INPUT,
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertTrue(body["data"]["split_id"], body)
        orders = self._group(body["data"]["split_id"])
        original, *children = orders
        self.assertEqual(body["data"]["order_no"], original.order_no)
        self.assertEqual([order.project_text for order in orders], [
            "保留 / 原内容", "子单 甲 / 乙", "折抵", "补做",
        ])
        self.assertEqual([order.total_price for order in orders], [
            Decimal("270.00"), Decimal("32.25"), Decimal("-2.25"), Decimal("0.00"),
        ])
        self.assertEqual({order.split_kind for order in orders}, {"PEER"})
        self.assertEqual({order.split_ship_main_order_no for order in orders}, {original.order_no})
        self.assertEqual([order.source_order_no for order in orders], [
            "SYNTHETIC-SPLIT", "SYNTHETIC-SPLIT-1", "SYNTHETIC-SPLIT-2", "SYNTHETIC-SPLIT-3",
        ])
        self.assertIsNotNone(original.customer_id)
        self.assertEqual({order.customer_id for order in orders}, {original.customer_id})
        self.assertEqual([order.order_label for order in orders], ["录单标签", "", "", ""])
        self.assertIsNotNone(original.qr_fetch_suppress_until)
        self.assertTrue(all(order.qr_fetch_suppress_until is None for order in children))
        self.assertEqual(original.status, OrderStatus.IN_PROGRESS)
        self.assertTrue(all(order.status == OrderStatus.UNASSIGNED for order in children))
        for field, raw in self.PRICE_INPUT.items():
            if field != "total_price":
                self.assertEqual(getattr(original, field), Decimal(raw), field)
                self.assertEqual([getattr(child, field) for child in children], [Decimal("0.00")] * 3, field)
        self.assertEqual(list(
            Performance.objects.filter(order__in=orders, emp_type=EmpType.MAIN)
            .order_by("order_id").values_list("user_id", "temp_amount")
        ), [(taker.pk, Decimal("88.00"))] + [(None, Decimal("0.00"))] * 3)
        self.assertEqual(list(
            Performance.objects.filter(order__in=orders, emp_type=EmpType.SALES)
            .order_by("order_id").values_list("user_id", flat=True)
        ), [original.sales_user_id] * 4)

    def test_explicit_split_uses_zero_when_original_total_is_omitted(self):
        payload = {
            "game": self.game.id,
            "project_text": "原单",
            "split": True,
            "split_orders": [{"project_text": "子单", "total_price": "12.50"}],
        }
        body = self.client.post(CREATE_URL, payload, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        orders = self._group(body["data"]["split_id"])
        self.assertEqual([order.total_price for order in orders], [Decimal("-12.50"), Decimal("12.50")])
        self.assertEqual(
            [order.source_order_no for order in orders], ["", f"{orders[0].order_no}-1"],
        )

    def test_invalid_explicit_split_rejects_before_creating_any_rows(self):
        child = {"project_text": "子单", "total_price": "25.00"}
        malformed_requests = [
            {"split": False, "split_orders": [child]},
            {"split": True, "split_orders": []},
            {"split": True, "split_orders": [child, {**child, "project_text": " "}]},
            {"split": True, "split_orders": [child, {**child, "total_price": "1.001"}]},
            {
                "split": True,
                "total_price": "9999999999.99",
                "split_orders": [child, {**child, "total_price": "-25.01"}],
            },
        ]
        for data in malformed_requests:
            with self.subTest(data=data):
                body = self._create(**data).json()
                self.assertEqual(body["code"], 1000, body)
                self.assertIsNone(body["data"])
                self.assertFalse(Order.objects.exists())
                self.assertFalse(Performance.objects.exists())
                self.assertFalse(OperationLog.objects.filter(action="order.create").exists())

    def test_three_projects_make_three_orders_in_one_group(self):
        body = self._create(
            project_text="满星/深渊/每日", split=True, **self.PRICE_INPUT
        ).json()
        self.assertEqual(body["code"], 0, body)
        split_id = body["data"]["split_id"]
        self.assertNotEqual(split_id, "")  # 非空哨兵：真的拆了
        orders = self._group(split_id)
        self.assertEqual(len(orders), 3, [o.order_no for o in orders])
        # 每笔一段、顺序与入参一致（`id` 升序 = 落库顺序）
        self.assertEqual([o.project_text for o in orders], ["满星", "深渊", "每日"])
        # 组 ID 是**组标识**、不是订单号（形状同为 20 位数字，但值必须不同）
        self.assertEqual({o.split_id for o in orders}, {split_id})
        self.assertNotIn(split_id, [o.order_no for o in orders])

    def test_the_group_price_sum_equals_the_single_order_total(self):
        """**G3（D-0357 重写）：主单 `total_price` == 拆分前原值、子单 `total_price` 恒 0**。

        ⚠️ **旧不变式（组内 `MONEY_FIELDS` 之和 == 原值）已作废**（业务方
        口径 2/4：总价不能变 = **客户总价**不变，子单价格不逐笔分摊、接单价统一 0、
        子单发单价（打手价）由客服发单时后填合法值）⇒ 本用例改为逐字钉新口径：
        - 主单 6 个价格列 = 录单入参原值（客户账只在主单上）；
        - 子单 6 个价格列**每列恰好是 `Decimal("0.00")`**（不是 `None`，E-0021）；
        - 子单后填发单价（打手价）合法 ⇒ 主单 `total_price` 一个字节不动。
        判别力：给子单加任何「分摊」逻辑（旧口径的形状）或给 `/price/` 开子单口
        （G2 的反面），本用例红。
        """
        body = self._create(
            project_text="满星/深渊/每日", split=True, **self.PRICE_INPUT
        ).json()
        self.assertEqual(body["code"], 0, body)
        orders = self._group(body["data"]["split_id"])
        self.assertEqual(len(orders), 3)  # 非空哨兵

        # 主单 = 组内 id 最小者：6 列全是入参原值
        main = orders[0]
        for field, raw in self.PRICE_INPUT.items():
            self.assertEqual(getattr(main, field), Decimal(raw), f"主单 {field} 变了")
        # 子单：每列恰好 0
        for child in orders[1:]:
            for field in self.PRICE_INPUT:
                self.assertEqual(
                    getattr(child, field), Decimal("0.00"), f"子单 {field} 不是 0"
                )

        # 绩效行价格：录单时 MAIN 行 temp = 该笔 hall_price ⇒ 主单行 88.00、子单行 0
        self.assertEqual(
            Performance.objects.filter(order=main, emp_type=EmpType.MAIN).get().temp_amount,
            Decimal("88.00"),
        )
        for child in orders[1:]:
            for perf in Performance.objects.filter(order=child):
                self.assertEqual(perf.temp_amount, Decimal("0.00"))
                self.assertEqual(perf.bonus_amount, Decimal("0.00"))
                self.assertIsNone(perf.taker_price)

        # 子单后填发单价（打手价，G4：发单逻辑与普通单一致）⇒ 客户总价不动
        child = orders[1]
        child.hall_price = Decimal("66.00")
        child.save(update_fields=["hall_price"])
        main.refresh_from_db()
        self.assertEqual(main.total_price, Decimal("300.00"), "主单接单价被子单带跑")

    def test_each_split_order_gets_its_own_performance_rows(self):
        """每笔子单都要有自己的绩效行（否则就是「没有绩效的孤儿订单」，spec §7.4）。"""
        body = self._create(
            project_text="满星/深渊", split=True, **self.PRICE_INPUT
        ).json()
        orders = self._group(body["data"]["split_id"])
        self.assertEqual(len(orders), 2)
        for order in orders:
            main = Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
            self.assertEqual(main.count(), 1, order.order_no)
            # 客服行也建（`sales_user` = 认证态录单人 ⇒ 非空）
            self.assertEqual(
                Performance.objects.filter(order=order, emp_type=EmpType.SALES).count(),
                1,
                order.order_no,
            )

    def test_a_single_project_falls_back_to_one_plain_order(self):
        """勾了拆单**但没有 `/`** ⇒ 按普通录单落一笔、`split_id` 空串、**不报错**。"""
        body = self._create(project_text="满星", split=True, **self.PRICE_INPUT).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["split_id"], "")
        self.assertEqual(Order.objects.count(), 1)
        order = Order.objects.get()
        self.assertEqual(order.project_text, "满星")
        self.assertEqual(order.total_price, Decimal("300.00"))

    def test_blank_segments_are_dropped(self):
        """`/` 产生的空段被丢弃（` 满星 / /深渊 ` ⇒ 两段，且各自去掉首尾空白）。"""
        body = self._create(
            project_text=" 满星 / /深渊 ", split=True, **self.PRICE_INPUT
        ).json()
        self.assertEqual(body["code"], 0, body)
        orders = self._group(body["data"]["split_id"])
        self.assertEqual([o.project_text for o in orders], ["满星", "深渊"])

    def test_all_segments_blank_falls_back_to_one_plain_order(self):
        """全是空段（`" / "`）⇒ **不拆**、按普通录单落一笔、**不报错**（N ≤ 1 那一档）。

        ⚠️ 归一化后的 `" / "` 不是空串（`normalize_project_text` 只压空白）⇒ 它过得了
        录单的必填闸，落库的 `project_text` 就是那一串 —— 与「没勾拆单」**逐字同一条路**。
        """
        body = self._create(project_text=" / ", split=True).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["split_id"], "")
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().project_text, "/")

    def test_the_model_column_cannot_be_forced_from_the_intake_payload(self):
        """⛔ 请求体给不了 `split_id`（服务端生成；见 `SERVER_WRITTEN_ORDER_FIELDS`）。

        判别力：把它从 `OrderSerializer.Meta.read_only_fields` 拿掉 ⇒ 这里会是 `"FORGED"`。
        """
        body = self._create(project_text="满星", split_id="FORGED").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["split_id"], "")
        self.assertEqual(Order.objects.get().split_id, "")

    def test_absent_split_key_means_no_split(self):
        """⛔ 缺席（与显式 `false`）**都不是拆单**：行为与今天逐字相同。"""
        body = self._create(project_text="满星/深渊").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["split_id"], "")
        self.assertEqual(Order.objects.count(), 1)

    def test_one_failed_child_rolls_the_whole_batch_back(self):
        """**任一笔失败 ⇒ 一笔都不落**（`transaction.atomic()` + `set_rollback(True)`）。

        判别力：把建单那一段的 `atomic()` 去掉 ⇒ 第 1 笔会留在库里 ⇒ 本用例红。
        ⚠️ 桩打在**被查找的那个模块**上（`apps.business.order_intake`，E-0029）；
        第 2 次调用抛**领域层可预期**的 `CommissionError`（那条翻译路径既有）。
        """
        real = __import__(
            "apps.business.order_intake", fromlist=["create_performance_for_order"]
        ).create_performance_for_order
        calls = {"n": 0}

        def flaky(order):
            calls["n"] += 1
            if calls["n"] == 2:
                raise CommissionError("提成配置不可用", 1000)
            return real(order)

        split_requests = [
            {"project_text": "满星/深渊/每日"},
            {"split_orders": [{"project_text": "深渊", "total_price": "30.00"}]},
        ]
        for payload in split_requests:
            with self.subTest(payload=payload), mock.patch(
                "apps.business.order_intake.create_performance_for_order", side_effect=flaky
            ):
                calls["n"] = 0
                body = self._create(split=True, **self.PRICE_INPUT, **payload).json()
                self.assertEqual(calls["n"], 2, "桩没接上（第 2 笔都没轮到）")
                self.assertEqual(body["code"], 1000, body)
                self.assertEqual(body["msg"], "提成配置不可用")
                self.assertEqual(Order.objects.count(), 0)
                self.assertEqual(Performance.objects.count(), 0)
                self.assertEqual(OperationLog.objects.filter(action="order.create").count(), 0)


class SplitEndpointTests(_SplitFixture):
    """**详情页**那条入口（`POST /orders/<单号>/split/`；**推断级设计**，对标零抓件）。"""

    def _order(self, project_text="原项目", **kw):
        kw.setdefault("project_text", project_text)
        kw.setdefault("status", OrderStatus.UNASSIGNED)
        return Order.objects.create(
            game=self.game,
            order_no="20261012000000000001",
            total_price=Decimal("300.00"),
            hall_price=Decimal("88.00"),
            **kw,
        )

    def test_the_current_order_becomes_the_first_member_and_keeps_its_prices(self):
        order = self._order()
        body = self._post_split(order.order_no, ["甲项目", "乙项目", "丙项目"]).json()
        self.assertEqual(body["code"], 0, body)
        group_id = body["data"]["split_id"]
        self.assertNotEqual(group_id, "")
        self.assertEqual(body["data"]["order_no"], order.order_no)

        order.refresh_from_db()
        self.assertEqual(order.project_text, "甲项目")
        self.assertEqual(order.split_id, group_id)
        # ⚠️ 价格**原样保留**（一个都不动）
        self.assertEqual(order.total_price, Decimal("300.00"))
        self.assertEqual(order.hall_price, Decimal("88.00"))

        members = list(Order.objects.filter(split_id=group_id).order_by("id"))
        self.assertEqual(members[0].order_no, order.order_no)  # 组内第一笔 = 原单
        children = members[1:]
        self.assertEqual([c.project_text for c in children], ["乙项目", "丙项目"])
        # ⭐ D-0301：`created` 从内置号字符串列表改成**行列表**
        #    （`order_no` + `source_order_no`，前端展示回退需要两个原值）。
        self.assertEqual(
            body["data"]["created"],
            [{"order_no": c.order_no, "source_order_no": c.source_order_no}
             for c in children],
        )
        for child in children:
            self.assertEqual(child.status, OrderStatus.UNASSIGNED)
            for field in MONEY_FIELDS:
                self.assertEqual(getattr(child, field), Decimal("0.00"), field)
            # ⚠️ 客服行只在当前单**有 `sales_user`** 时建（`create_customer_performance` 的
            #    既有口径）；本夹具那两笔子单照抄当前单 ⇒ 这里的期望值取决于夹具。
            expected_rows = 2 if order.sales_user_id is not None else 1
            self.assertEqual(
                Performance.objects.filter(order=child).count(), expected_rows
            )
            self.assertEqual(
                Performance.objects.filter(
                    order=child, emp_type=EmpType.MAIN
                ).count(),
                1,
            )

    def test_splitting_twice_stays_in_the_same_group(self):
        """连拆两次 ⇒ **仍是同一个组**（⛔ 不产生第二个组）。"""
        order = self._order()
        first = self._post_split(order.order_no, ["甲", "乙"]).json()
        self.assertEqual(first["code"], 0, first)
        group_id = first["data"]["split_id"]

        second = self._post_split(order.order_no, ["甲", "丙", "丁"]).json()
        self.assertEqual(second["code"], 0, second)
        self.assertEqual(second["data"]["split_id"], group_id)
        members = Order.objects.filter(split_id=group_id).order_by("id")
        self.assertEqual(members.count(), 4, [o.project_text for o in members])
        self.assertEqual(
            sorted(members.values_list("project_text", flat=True)),
            ["丁", "丙", "乙", "甲"],
        )
        # 全仓只有一个组（⛔ 没有第二个组 ID）
        self.assertEqual(
            set(Order.objects.exclude(split_id="").values_list("split_id", flat=True)),
            {group_id},
        )

    def test_a_deleted_order_is_rejected_with_a_business_code(self):
        order = self._order()
        Order.objects.filter(pk=order.pk).update(
            deleted_at=order.created_at, deleted_by="tester"
        )
        body = self._post_split(order.order_no, ["甲", "乙"]).json()
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "记录不存在或已被删除")
        self.assertEqual(Order.objects.filter(split_id__gt="").count(), 0)

    def test_a_completed_order_is_rejected_without_writing_anything(self):
        order = self._order(status=OrderStatus.COMPLETED)
        body = self._post_split(order.order_no, ["甲", "乙"]).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "已完成的订单不能再拆分")
        order.refresh_from_db()
        self.assertEqual(order.project_text, "原项目")
        self.assertEqual(order.split_id, "")
        self.assertEqual(Order.objects.count(), 1)

    def test_a_single_project_is_rejected_with_a_business_code(self):
        order = self._order()
        body = self._post_split(order.order_no, ["只有一个"]).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "拆分订单至少需要 2 个代练项目")
        self.assertEqual(Order.objects.count(), 1)

    def test_a_blank_project_is_rejected_with_a_business_code(self):
        order = self._order()
        for blank in ("", "   "):
            body = self._post_split(order.order_no, ["甲", blank]).json()
            self.assertEqual(body["code"], 1000, body)
            self.assertEqual(body["msg"], "拆分的代练项目不能有空项")
        self.assertEqual(Order.objects.count(), 1)

    def test_a_missing_projects_key_is_not_a_500(self):
        order = self._order()
        body = self.client.post(
            f"{self.base}/{order.order_no}/split/", {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(Order.objects.count(), 1)

    def test_without_the_add_edit_node_the_endpoint_is_403(self):
        """节点门与录单**同一条**（`order.add_edit`）：⛔ 不新增权限节点。

        ⚠️ 第二个账号**不给任何组 / 节点**（`groups=()`）：给「客服」组挂节点会**连带**
        放行第一个夹具账号（`_grant_nodes` 挂的是**共享的 Role**，两个账号同组时节点是并集）
        ⇒ 那样这条用例会假绿（实测踩到过）。
        """
        order = self._order()
        plain = make_logged_in_client(username="split-no-node", groups=(), nodes=())
        response = plain.post(
            f"{self.base}/{order.order_no}/split/",
            {"projects": ["甲", "乙"]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), 1)

    def test_patch_cannot_move_an_order_into_or_out_of_a_group(self):
        """⛔ 通用 `PATCH` 改不了 `split_id`（服务端列；判据见 `SERVER_WRITTEN_ORDER_FIELDS`）。

        判别力：把它从 `OrderUpdateSerializer.Meta.read_only_fields` 拿掉 ⇒ 这里会是 `"X"`。
        """
        order = self._order()
        body = self.client.patch(
            f"{self.base}/{order.order_no}/",
            {"split_id": "X", "remark": "改得动别的"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.split_id, "")
        self.assertEqual(order.remark, "改得动别的")  # 阳性对照：别的列照旧可写

    def test_the_operation_log_records_the_rename_and_every_new_order(self):
        """留痕打在**数据库行**上：主单一条 `order.update`（项目名前后值）+ 每个子单一条 `order.create`。"""
        order = self._order()
        body = self._post_split(order.order_no, ["甲", "乙", "丙"]).json()
        self.assertEqual(body["code"], 0, body)
        renamed = list(
            OperationLog.objects.filter(
                order_no=order.order_no, action="order.update"
            )
        )
        self.assertEqual(len(renamed), 1, renamed)
        row = renamed[0]
        self.assertEqual(row.field_name, "project_text")
        self.assertEqual(row.from_value, "原项目")
        self.assertEqual(row.to_value, "甲")
        self.assertEqual(row.actor, "split-cs")

        created = list(OperationLog.objects.filter(action="order.create"))
        # ⭐ D-0301：`created` 是行列表 ⇒ 取 `order_no` 键再比（集合逐字）。
        self.assertEqual(
            sorted(r.order_no for r in created),
            sorted(row["order_no"] for row in body["data"]["created"]),
        )
        self.assertEqual({r.actor for r in created}, {"split-cs"})


class SplitListReadTests(_SplitFixture):
    """**列表读面**：折叠（当次筛选结果集内做）+ 两个新键 + 展开 + 子单仍可达。"""

    def _order(self, no, **kw):
        kw.setdefault("project_text", "整单项目")
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.UNASSIGNED,
            **kw,
        )

    def _group_of_three(self):
        """一组三笔（主单 + 两个子单），返回 `(主单, [子单…])`。"""
        main = self._order("20261012000000000101", total_price=Decimal("300.00"))
        children = [
            self._order(
                "20261012000000000102",
                project_text="子项目乙",
                split_id="90000000000000000001",
            ),
            self._order(
                "20261012000000000103",
                project_text="子项目丙",
                split_id="90000000000000000001",
            ),
        ]
        main.split_id = "90000000000000000001"
        main.save(update_fields=["split_id"])
        return main, children

    def _items(self, query=""):
        """取信封里的 `items`（先断端点成功，避免把归因引到错误的层）。"""
        body = self.client.get(LIST_URL + query).json()
        self.assertEqual(body["code"], 0, body)
        return body

    def test_only_the_smallest_id_of_a_group_appears(self):
        main, children = self._group_of_three()
        body = self._items()
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn(main.order_no, rows)  # 非空哨兵
        for child in children:
            self.assertNotIn(child.order_no, rows, "子单不该单独占一行")
        self.assertEqual(body["data"]["total"], 1)

    def test_the_two_new_keys_are_on_the_payload_and_at_the_end(self):
        main, _ = self._group_of_three()
        body = self._items()
        row = body["data"]["items"][0]
        self.assertEqual(row["split_id"], main.split_id)
        self.assertEqual(row["split_members"], 3)
        # **追加在末尾**（既有前端与「键集合」网的纪律）。
        # ⭐ 拆单读面：末尾追加组内计数、兼容旧主单标志与新价格锚点标志；
        # 非拆单行两键恒 `0` / `False`（`test_a_non_split_row_is_untouched` 同类）。
        self.assertEqual(
            list(row)[-6:],
            [
                "split_kind", "split_id", "split_members", "split_completed_members",
                "split_is_main", "split_is_balance_anchor",
            ],
        )
        # 组内三笔都未完成 ⇒ `X=0`；主单行（折叠后留在列表上的代表）`split_is_main=True`
        self.assertEqual(row["split_completed_members"], 0)
        self.assertIs(row["split_is_main"], True)
        self.assertIs(row["split_is_balance_anchor"], True)

    def test_a_non_split_row_is_untouched(self):
        self._order("20261012000000000201")
        self._group_of_three()
        body = self._items()
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertIn("20261012000000000201", rows)
        plain = rows["20261012000000000201"]
        self.assertEqual(plain["split_id"], "")
        self.assertEqual(plain["split_members"], 0)
        self.assertEqual(body["data"]["total"], 2)  # 非拆单一行 + 拆单组一行

    def test_a_filtered_out_main_lets_a_child_stand_for_the_group(self):
        """**折叠在当次筛选结果集内做**：主单被筛掉时，命中的子单自己站出来。

        判别力：把折叠写成「按全表最小 id 折」（例如拿 `Order.objects` 而不是**当次
        queryset** 去建 `Exists`）⇒ 这里主单不匹配筛选、子单会被折掉 ⇒ 整组蒸发、本用例红。
        """
        main, children = self._group_of_three()
        body = self._items("?project_text=子项目丙")
        rows = [row["order_no"] for row in body["data"]["items"]]
        self.assertEqual(rows, [children[1].order_no])
        row = body["data"]["items"][0]
        self.assertEqual(row["split_members"], 3)  # 组内总数照旧 = 3
        self.assertEqual(body["data"]["total"], 1)
        self.assertNotIn(main.order_no, rows)

    def test_the_summary_counts_the_same_folded_queryset(self):
        """`summary` 与列表**同一份 queryset**（折叠一起变）。"""
        self._group_of_three()
        body = self._items()
        summary = body["data"]["summary"]
        self.assertIsNotNone(summary)  # 非空哨兵：没被 view_total_stats 挡掉
        self.assertEqual(summary["count"], body["data"]["total"])
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["total_price"], "300.00")  # 子单的 0 没被算成另一笔
        self.assertEqual(summary["status_counts"].get(OrderStatus.UNASSIGNED), 1)

    def test_an_explicit_split_id_returns_every_member_without_folding(self):
        main, children = self._group_of_three()
        body = self._items(f"?split_id={main.split_id}&page_size=50")
        rows = {row["order_no"]: row for row in body["data"]["items"]}
        self.assertEqual(
            set(rows), {main.order_no, children[0].order_no, children[1].order_no}
        )
        self.assertEqual(body["data"]["total"], 3)
        # 展开那一档里每个成员都带自己的「拆」标记口径（成员总数）
        self.assertEqual({row["split_members"] for row in rows.values()}, {3})

    def test_a_child_order_is_still_reachable_by_the_detail_endpoint(self):
        """子单是**真实订单行**：详情端点既有入口不动（折叠只是读面的呈现）。"""
        _, children = self._group_of_three()
        body = self.client.get(f"{self.base}/{children[0].order_no}/").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["order_no"], children[0].order_no)
        self.assertEqual(body["data"]["split_id"], "90000000000000000001")

    def test_an_over_long_split_id_param_is_a_business_code_not_a_500(self):
        """非法长度 ⇒ 200 + 业务码 `1000`（⛔ 不是 HTML 500：请求来源的值过了序列化器）。"""
        self._group_of_three()
        body = self.client.get(LIST_URL + "?split_id=" + "9" * 33).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"])


class SplitModelShapeTests(TenantTestCase):
    """既有 split_id 关联键 + 本次新增的组类型列；不建主子外键。"""

    def test_only_one_new_column_and_no_boolean_or_parent_foreign_key(self):
        fields = {field.name for field in Order._meta.get_fields()}
        self.assertIn("split_id", fields)
        self.assertIn("split_kind", fields)
        # ⛔ 没有 `is_split` 之类的第二个判据列
        self.assertFalse({"is_split", "is_split_order", "split"} & fields)
        # ⛔ 没有父子外键（本线的「主单」是**显示层**概念）
        self.assertFalse({"parent", "parent_order", "main_order", "from_order"} & fields)
        # 排序一个字不改（同组靠折叠 + 显式展开聚在一起，⛔ 不靠排序位置）
        self.assertEqual(Order._meta.ordering, ["-created_at", "-id"])


class SplitCopyAndRecycleTests(_SplitFixture):
    """**写入点回查**（`AGENTS.md` §5-E / E-0027）：新增 `split_id` 之后，它的
    「复制成新单 / 软删」两条既有路径各是什么行为 —— 逐条钉住，⛔ 不留未定义。

    - **复制**（`POST /orders/<单号>/copy`）：新单**不进任何组**（`split_id` 走模型默认的空串）。
      复制出来的是一张独立的新单，不是某个拆单组的新成员。
    - **软删**：⛔ **G9（D-0357）改准**——删除即出组：被删成员的 `split_id` 同事务清空；
      活跃成员**剩 1** ⇒ 幸存那笔的也清（业务方拍板 6「组内只剩一笔时自动变回独立单」）。
      ⚠️ 旧判据「组 ID 原样保留」随本轮**作废**（拆单 v2 的「删除 = 出组 / 删组」口径），
      重写见 `SplitDeleteGroupTests`。
    """

    GROUP = "71000000000000000001"

    def _group(self):
        main = Order.objects.create(
            game=self.game,
            order_no="20261012000000000901",
            project_text="主项目",
            split_id=self.GROUP,
        )
        child = Order.objects.create(
            game=self.game,
            order_no="20261012000000000902",
            project_text="子项目",
            split_id=self.GROUP,
        )
        return main, child

    def test_copying_a_member_creates_a_standalone_order(self):
        main, _ = self._group()
        body = self.client.post(
            f"{self.base}/{main.order_no}/copy", {}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        new = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(new.split_id, "")
        # 阳性对照：老单的组 ID 一个字没动
        main.refresh_from_db()
        self.assertEqual(main.split_id, self.GROUP)

    def test_soft_deleting_one_of_two_members_prunes_the_survivor_to_standalone(self):
        """**G9 prune**：两成员组删一个 ⇒ 被删的出组（`split_id` 清）、幸存的**变独立单**。

        ⚠️ 判别力：G9 的两半各有一半——被删成员 `split_id` 不清 ⇒ 前半个断言红；
        幸存者不清（残留一人「组」）⇒ 后半个断言红。列表载荷随之归零
        （`split_members == 0`，非拆单行）。
        """
        main, child = self._group()
        body = self.client.get(LIST_URL).json()
        self.assertEqual(body["data"]["items"][0]["split_members"], 2)  # 前置哨兵

        deleted = self.client.post(
            f"{self.base}/{child.order_no}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        ).json()
        self.assertEqual(deleted["code"], 0, deleted)
        child.refresh_from_db()
        main.refresh_from_db()
        self.assertIsNotNone(child.deleted_at)  # 副产品：真的软删了
        self.assertEqual(child.split_id, "")    # G9 前半：删除即出组
        self.assertEqual(main.split_id, "")     # G9 后半：剩 1 ⇒ 清尾变独立单

        row = self.client.get(LIST_URL).json()["data"]["items"][0]
        self.assertEqual(row["order_no"], main.order_no)
        self.assertEqual(row["split_members"], 0)  # 幸存者是普通单，不再是「组」

    def test_soft_deleting_one_of_three_keeps_the_group_intact(self):
        """三成员组删一个 ⇒ 组**仍在**（剩 2 不 prune）、被删的出组、其余组 ID 不动。"""
        main = Order.objects.create(
            game=self.game, order_no="20261012000000000911",
            project_text="主项目", split_id=self.GROUP,
        )
        child_a = Order.objects.create(
            game=self.game, order_no="20261012000000000912",
            project_text="子项目甲", split_id=self.GROUP,
        )
        child_b = Order.objects.create(
            game=self.game, order_no="20261012000000000913",
            project_text="子项目乙", split_id=self.GROUP,
        )
        deleted = self.client.post(
            f"{self.base}/{child_a.order_no}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        ).json()
        self.assertEqual(deleted["code"], 0, deleted)
        child_a.refresh_from_db()
        main.refresh_from_db()
        child_b.refresh_from_db()
        self.assertEqual(child_a.split_id, "")   # 出组
        self.assertEqual(main.split_id, self.GROUP)     # 组保留
        self.assertEqual(child_b.split_id, self.GROUP)  # 组保留


class SplitRestrictToSelfCreatedTests(_SplitFixture):
    """⭐ **P0 回归（独立评审抓到的顺序缺陷）**：折叠必须在 `scope_by_creator` **之后**。

    路径：同一组里**主单是别人录的**（`created_by` = 另一个账号）、**子单是我录的**，而我的角色
    带 `order.restrict_to_self_created`（只看得见自己录的单）：

    - **错序**（先前实现：先折叠、后收窄）⇒ 折叠先按**未收窄**的集合保下「我看不见的主单」、
      折掉「我的子单」，再按 `created_by` 收掉主单 ⇒ **行数为 0**（我该看见的子单也一起没了，
      整组凭空消失）；
    - **现序**（先收窄、后折叠）⇒ 收窄后的集合里只剩我的子单 ⇒ 它自己站出来当那一组的代表。

    ⚠️ 两个 client 必须**不同组**：节点挂在共享的 `Group → Role` 上（`p5_auth_helpers._grant_nodes`
    给该用户**所有**组都挂同一批节点）⇒ 同组会让「只给第二个账号施加 flag」变成两个账号一起被
    限制，本文件其它用例会连带红。
    """

    GROUP = "73000000000000000001"
    #: 「别人」的账号名——`scope_by_creator` 比的是 `Order.created_by`（用户名快照列）。
    OUTSIDER = "另外一位客服的账号"

    def _group(self):
        main = Order.objects.create(
            game=self.game,
            order_no="20261012000000000801",
            project_text="别人的主项目",
            split_id=self.GROUP,
            created_by=self.OUTSIDER,
        )
        child = Order.objects.create(
            game=self.game,
            order_no="20261012000000000802",
            project_text="我录的子项目",
            split_id=self.GROUP,
            created_by="split-self-only",
        )
        return main, child

    def _restricted_client(self):
        """带 `order.restrict_to_self_created` 的账号（**另起一个组**，见类 docstring）。"""
        return make_logged_in_client(
            username="split-self-only",
            groups=("代练员",),
            nodes=(ADD_EDIT, "order.list", "order.restrict_to_self_created"),
        )

    def test_my_child_stands_for_the_group_when_the_main_is_someone_elses(self):
        main, child = self._group()
        body = self._restricted_client().get(LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = [row["order_no"] for row in body["data"]["items"]]
        # ⚠️ 判据是「**真的出现**」，⛔ 不是「不是 500」：我的子单自己当那一组的代表。
        self.assertEqual(rows, [child.order_no], body["data"]["items"])
        self.assertEqual(body["data"]["total"], 1)
        self.assertNotIn(main.order_no, rows)  # 别人的主单看不见（flag 的既有语义，未放宽）
        # 组内总数照旧 = 2（`split_members` 走默认 manager、不受这条 flag 影响）
        self.assertEqual(body["data"]["items"][0]["split_members"], 2)

    def test_without_the_flag_the_group_still_folds_to_the_main(self):
        """对照：**不带**该 flag 时行为不变（本文件的既有折叠判据一个字没动）。"""
        main, child = self._group()
        body = self.client.get(LIST_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = [row["order_no"] for row in body["data"]["items"]]
        self.assertEqual(rows, [main.order_no])
        self.assertEqual(body["data"]["total"], 1)
        self.assertNotIn(child.order_no, rows)


class MoneyFieldEnumerationTests(SimpleTestCase):
    """E-0027 的形状：**四处**价格列清单必须**恰好一致**（⛔ 不是包含式）。

    为什么需要这张网：子单的价格全 0 靠的是 `SPLIT_MEMBER_ZERO_PRICES`（从 `MONEY_FIELDS` 导出），
    而不变式的判据用的是本文件手写的 `PRICE_INPUT`。今天两边都完整，但**将来给 `Order` 加一个金额列
    而漏进 `MONEY_FIELDS`** ⇒ 子单会**继承原价**（真不变式破了），而所有行为用例**照样全绿**
    （它们只比对两张清单里都有的那些列）。这四条断言让那种改动**当场红**，逼人同一次改齐。

    ⚠️ **`MONEY_FIELDS` 的定义域（读源码核实，⛔ 不是照抄一句推断）**：`apps/business/models.py`
    的注释说它是「金额字段：只有 `/price/` 能改」的登记表，**没有**任何「有意排除某一列」的说明；
    实测 `Order._meta` 上的 `DecimalField` 恰好就是这 6 个（`python -c` 列过：
    fee / hall_price / platform_price / received_price / refund_fee / total_price）。
    所以第 ① 条写成**恰好相等**是成立的。⚠️ 若将来**有意**排除某一列（例如加了「非金额的
    Decimal 比率」），必须**同一次**改这张网并把「为什么不含它」写在这里 —— ⛔ 不许把它放宽成
    `issubset`（那正是本条要挡的失败方向）。
    """

    #: `MONEY_FIELDS` 之外**允许**存在的 Decimal 列（今天为空）。
    #: 加列时**要么**进 `MONEY_FIELDS`、**要么**写进这里并给出理由；两边都不在 ⇒ 红。
    NON_MONEY_DECIMAL_COLUMNS: frozenset[str] = frozenset()

    def test_the_money_fields_are_exactly_the_orders_decimal_columns(self):
        columns = {
            field.name
            for field in Order._meta.get_fields()
            if isinstance(field, DecimalField)
        }
        self.assertEqual(
            columns - set(MONEY_FIELDS),
            set(self.NON_MONEY_DECIMAL_COLUMNS),
            "Order 上出现了既不在 MONEY_FIELDS、也没登记进 NON_MONEY_DECIMAL_COLUMNS 的金额列"
            "（拆单的子单价格全 0 靠 MONEY_FIELDS ⇒ 漏进去就是静默继承原价）",
        )
        self.assertEqual(
            set(MONEY_FIELDS) - columns,
            set(),
            "MONEY_FIELDS 里有 Order 上不存在的列（写错名字 / 列被删了）",
        )

    def test_the_split_zero_price_list_is_exactly_the_money_fields(self):
        self.assertEqual(set(SPLIT_MEMBER_ZERO_PRICES), set(MONEY_FIELDS))
        # 值也钉住：这一格要的是「0」而不是「空」
        self.assertEqual(set(SPLIT_MEMBER_ZERO_PRICES.values()), {Decimal("0.00")})

    def test_the_invariant_fixture_covers_exactly_the_money_fields(self):
        """本文件那份**手写**的 `PRICE_INPUT` 与 `MONEY_FIELDS` 也必须恰好一致。

        判别力：往 `MONEY_FIELDS` 加一列、却忘了在 `SplitIntakeTests.PRICE_INPUT` 里补上 ⇒
        「价格不变式」那条用例会**静默漏测新列**（它只遍历 `PRICE_INPUT`）⇒ 本条当场红。
        """
        self.assertEqual(set(SplitIntakeTests.PRICE_INPUT), set(MONEY_FIELDS))


class SplitSourceDisplayTests(_SplitFixture):
    """⭐ D-0301（2026-10-24）：拆单响应 `created` 行 + 留痕 reason 的**展示单号**。

    - `created` 从内置号字符串列表改成 `{order_no, source_order_no}` 行列表
      （前端展示回退需要两个原值）；
    - 留痕 `reason`（「由订单 X 拆分生成」）用**展示单号**；`order_no` / `target_id`
      等寻址键照旧内置号。
    """

    SRC = "SRC-TEST-SPLIT"

    def _split_with_source(self, source_value):
        response = self._create()
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order_no = body["data"]["order_no"]
        Order.objects.filter(order_no=order_no).update(source_order_no=source_value)
        split = self._post_split(order_no, ["甲项目", "乙项目"])
        self.assertEqual(split.json()["code"], 0, split.json())
        data = split.json()["data"]
        # ⚠️ 录单那一步自己也写一条 `order.create` 留痕（reason 为空）——
        #    断言**只认拆单落下的那两笔**（按 target_id 指认，不靠行序）。
        child_nos = [row["order_no"] for row in data["created"]]
        return order_no, data, child_nos

    def test_created_rows_suffix_the_source_and_keep_the_unsuffixed_shipping_number(self):
        order_no, data, _ = self._split_with_source(self.SRC)
        children = list(Order.objects.filter(split_id=data["split_id"]).order_by("id"))[1:]
        self.assertEqual(
            data["created"],
            [{"order_no": c.order_no, "source_order_no": f"{self.SRC}-1"} for c in children],
        )
        self.assertEqual([child.split_ship_main_order_no for child in children], [order_no])

    def test_continuing_from_a_child_uses_original_prefix_and_next_free_suffix(self):
        _, data, (child_no,) = self._split_with_source(self.SRC)
        result = self._post_peer_split(
            child_no, "乙项目", [{"project_text": "丙项目", "total_price": "0.00"}]
        ).json()
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["data"]["split_id"], data["split_id"])
        self.assertEqual(result["data"]["created"][0]["source_order_no"], f"{self.SRC}-2")

    def test_continued_split_skips_occupied_and_soft_deleted_numbers(self):
        root_no, _, _ = self._split_with_source("OCCUPIED")
        unrelated = self._create(source_order_no="OCCUPIED-2").json()
        self.assertEqual(unrelated["code"], 0, unrelated)
        deleted = self._create(source_order_no="OCCUPIED-3").json()
        self.assertEqual(deleted["code"], 0, deleted)
        Order.objects.filter(order_no=deleted["data"]["order_no"]).update(deleted_at=timezone.now())
        padded = self._create(source_order_no="OCCUPIED-4").json()
        self.assertEqual(padded["code"], 0, padded)
        Order.objects.filter(order_no=padded["data"]["order_no"]).update(source_order_no="\u00a0OCCUPIED-4\t")
        result = self._post_split(root_no, ["甲项目", "丙项目", "丁项目"]).json()
        self.assertEqual(result["code"], 0, result)
        self.assertEqual(
            [row["source_order_no"] for row in result["data"]["created"]],
            ["OCCUPIED-5", "OCCUPIED-6"],
        )
        self.assertEqual(Order.objects.get(order_no=root_no).source_order_no, "OCCUPIED")

    def test_created_rows_without_original_source_use_original_system_number(self):
        order_no, data, _ = self._split_with_source("")
        children = list(Order.objects.filter(split_id=data["split_id"]).order_by("id"))[1:]
        self.assertEqual(
            data["created"],
            [{"order_no": c.order_no, "source_order_no": f"{order_no}-1"} for c in children],
        )
        self.assertEqual(Order.objects.get(order_no=order_no).source_order_no, "")

    def test_split_reason_shows_the_source(self):
        order_no, data, child_nos = self._split_with_source(self.SRC)
        rows = OperationLog.objects.filter(action="order.create", target_id__in=child_nos)
        self.assertEqual(rows.count(), len(child_nos))  # 非空哨兵
        for row in rows:
            self.assertEqual(row.reason, f"由订单 {self.SRC} 拆分生成")

    def test_split_reason_blank_source_falls_back_to_system_no(self):
        order_no, data, child_nos = self._split_with_source("")
        rows = OperationLog.objects.filter(action="order.create", target_id__in=child_nos)
        self.assertEqual(rows.count(), len(child_nos))  # 非空哨兵
        for row in rows:
            self.assertEqual(row.reason, f"由订单 {order_no} 拆分生成")


class SplitPriceChangeGateTests(_SplitFixture):
    """拆单组价格经任意非锚点成员变动时，由最早活跃成员自动调平。"""

    GROUP_NO = "20261101000000000101"

    def _group(self):
        body = self._create(
            project_text="主项目/子项目甲/子项目乙", split=True
        ).json()
        self.assertEqual(body["code"], 0, body)
        orders = list(
            Order.objects.filter(split_id=body["data"]["split_id"]).order_by("id")
        )
        self.assertEqual(len(orders), 3)  # 非空哨兵
        return orders[0], orders[1], orders[2]

    def test_legacy_child_price_change_balances_the_anchor(self):
        main, child, _ = self._group()
        body = self.client.post(
            f"{self.base}/{child.order_no}/price/",
            {"total_price": "88.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        child.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(child.total_price, Decimal("88.00"))
        self.assertEqual(main.total_price, Decimal("111.50"))
        self.assertEqual(main.total_price + child.total_price, Decimal("199.50"))

    def test_the_balance_anchor_cannot_be_edited_directly(self):
        main, _, _ = self._group()
        body = self.client.post(
            f"{self.base}/{main.order_no}/price/",
            {"total_price": "288.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("原单接单价由系统调平", body["msg"])
        main.refresh_from_db()
        self.assertEqual(main.total_price, Decimal("199.50"))

    def test_peer_members_can_change_price_as_ordinary_orders(self):
        source = self._create(project_text="原项目").json()["data"]
        split = self._post_peer_split(
            source["order_no"], "剩余项目", [{"project_text": "拆出项目", "total_price": "10.00"}]
        ).json()
        self.assertEqual(split["code"], 0, split)
        child_no = split["data"]["created"][0]["order_no"]
        response = self.client.post(
            f"{self.base}/{child_no}/price/",
            {"total_price": "12.50"},
            content_type="application/json",
        ).json()
        self.assertEqual(response["code"], 0, response)
        child = Order.objects.get(order_no=child_no)
        source_order = Order.objects.get(order_no=source["order_no"])
        self.assertEqual(child.total_price, Decimal("12.50"))
        self.assertEqual(source_order.total_price, Decimal("187.00"))
        self.assertEqual(source_order.total_price + child.total_price, Decimal("199.50"))

    def test_batch_price_rejects_anchor_direct_edit(self):
        """批量不能绕过锚点只读规则；整批失败时不写任何成员。"""
        main, child, _ = self._group()
        body = self.client.post(
            f"{self.base}/batch/price/",
            {
                "order_nos": [main.order_no],
                "total_price": "66.00",
                "dry_run": False,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn("原单接单价由系统调平", body["msg"])
        self.assertEqual(body["data"]["conflict"]["order_no"], main.order_no)
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(main.total_price, Decimal("199.50"))   # 主单也没写成（整批回滚）
        self.assertEqual(child.total_price, Decimal("0.00"))

    def test_batch_price_dry_run_and_apply_balance_non_anchor_member(self):
        """试算与写入都允许非锚点改价，并给锚点反向调平。"""
        main, child, _ = self._group()
        body = self.client.post(
            f"{self.base}/batch/price/",
            {
                "order_nos": [child.order_no],
                "total_price": "66.00",
                "dry_run": True,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["eligible"], [child.order_no])
        applied = self.client.post(
            f"{self.base}/batch/price/",
            {"order_nos": [child.order_no], "total_price": "66.00", "dry_run": False},
            content_type="application/json",
        ).json()
        self.assertEqual(applied["code"], 0, applied)
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(main.total_price, Decimal("133.50"))
        self.assertEqual(child.total_price, Decimal("66.00"))
        self.assertEqual(main.total_price + child.total_price, Decimal("199.50"))

    def test_batch_dry_run_rejects_member_price_that_overflows_anchor(self):
        body = self._create(
            project_text="原项目/子项目甲/子项目乙",
            total_price="9999999999.99",
            split=True,
        ).json()
        self.assertEqual(body["code"], 0, body)
        main, child, _ = list(
            Order.objects.filter(split_id=body["data"]["split_id"]).order_by("id")
        )
        payload = {
            "order_nos": [child.order_no],
            "total_price": "-0.01",
        }
        dry_run = self.client.post(
            f"{self.base}/batch/price/",
            {**payload, "dry_run": True},
            content_type="application/json",
        ).json()
        self.assertEqual(dry_run["code"], 0, dry_run)
        self.assertEqual(dry_run["data"]["eligible"], [])
        self.assertEqual(
            [row["order_no"] for row in dry_run["data"]["ineligible"]],
            [child.order_no],
        )
        self.assertIn(
            "调平后的原单接单价超出有效金额范围",
            dry_run["data"]["ineligible"][0]["msg"],
        )
        applied = self.client.post(
            f"{self.base}/batch/price/",
            {**payload, "dry_run": False},
            content_type="application/json",
        ).json()
        self.assertEqual(applied["code"], 1002, applied)
        self.assertEqual(applied["data"]["conflict"]["order_no"], child.order_no)
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(main.total_price, Decimal("9999999999.99"))
        self.assertEqual(child.total_price, Decimal("0.00"))


class SplitDeleteGroupTests(_SplitFixture):
    """**G9 / G10 / G8 / M3（D-0357）**：删除的组语义——子单删除出组、主单删除整组
    同删、剩余组收口补发货与主单提成、prune 到 1 时幸存单按正常单收口（修复轮 M3，
    业务方：「如果订单被删到只剩一个 那就是变成正常订单了 不是拆单了啊」）。
    全部走 `POST /orders/<单号>/delete/`（单笔 / 批量 / 上游钩子共用
    `recycle.soft_delete_order` 这一个实现）。
    """

    GROUP = "71000000000000000002"

    #: 组内打手价（MAIN 行 bonus），给 G8 的「减项 == 全组和」算字面期望值。
    TAKER_BONUS_MAIN_ORDER = Decimal("0.00")
    TAKER_BONUS_A = Decimal("100.00")
    TAKER_BONUS_B = Decimal("50.00")
    MAIN_TOTAL = Decimal("300.00")
    SALES_RATE = Decimal("40.00")  # 40% ⇒ 期望提成 = (300 − 150) × 40% = 60.00（字面钉）

    def _member(self, order_no, project_text, **kw):
        return Order.objects.create(
            game=self.game, order_no=order_no, project_text=project_text,
            split_id=self.GROUP, total_price=kw.pop("total_price", Decimal("0.00")),
            **kw,
        )

    def _performance(self, order, emp_type, *, user=None, bonus=Decimal("0.00"),
                     review_status=None):
        return Performance.objects.create(
            order=order, emp_type=emp_type, user=user,
            bonus_amount=bonus, temp_amount=bonus,
            review_status=review_status,
        )

    def _sales_context(self):
        """给主单配归属客服 + 利润提成档案（G8 收口要算真钱）。"""
        from apps.accounts.choices import CommissionMode
        from apps.accounts.models import StaffProfile

        sales = get_user_model().objects.get(username="split-cs")
        # ⚠️ `make_logged_in_client` 已经给这个账号建过档案（默认 `ORDER_PERCENT` 模式）
        # ⇒ 不能 `get_or_create` 就完事，必须**改成**利润百分比档，否则
        # `commission_percent_of` 抛 `CommissionError`（模式不匹配）。
        profile, _ = StaffProfile.objects.get_or_create(user=sales)
        profile.commission_mode = CommissionMode.PROFIT_PERCENT
        profile.commission_value = self.SALES_RATE
        profile.save()
        return sales

    def _group_of_three(self, *, statuses=(None, None, None), sales=False):
        """主单 + 两个子单（id 升序 = 落库顺序）；`statuses` 逐位指定（None = 默认）。"""
        sales_user = self._sales_context() if sales else None
        main = self._member(
            "20261101000000000201", "主项目", total_price=self.MAIN_TOTAL,
            sales_user=sales_user,
        )
        sub_a = self._member("20261101000000000202", "子项目甲")
        sub_b = self._member("20261101000000000203", "子项目乙")
        self._performance(main, EmpType.MAIN)
        perf_a = self._performance(sub_a, EmpType.MAIN, bonus=self.TAKER_BONUS_A)
        perf_b = self._performance(sub_b, EmpType.MAIN, bonus=self.TAKER_BONUS_B)
        if sales:
            self._performance(main, EmpType.SALES, user=sales_user)
        for order, status in zip((main, sub_a, sub_b), statuses):
            if status is not None:
                order.status = status
                order.save(update_fields=["status"])
        return main, sub_a, sub_b, sales_user

    # ── G10：删主单 = 整组同删 ────────────────────────────────────────────

    def test_deleting_the_main_cascades_to_every_active_member(self):
        """删主单 ⇒ 其余活跃成员**同事务**全软删、各自留痕、`split_id` 全清（R7）。"""
        main, sub_a, sub_b, _ = self._group_of_three()
        body = self.client.post(
            f"{self.base}/{main.order_no}/delete/",
            {"isDelEmp": "Y"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        for order in (main, sub_a, sub_b):
            order.refresh_from_db()
            self.assertIsNotNone(order.deleted_at, order.order_no)
            self.assertEqual(order.split_id, "", order.order_no)  # 恢复各自为独立单
        # 各自绩效标记（isDelEmp="Y" 的单笔口径对每个成员都成立）
        for order in (main, sub_a, sub_b):
            perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
            self.assertIsNotNone(perf.deleted_at, order.order_no)
        # 各自 `order.delete` 留痕：主单不带 reason；子单注明「随拆单组主单删除」
        main_log = OperationLog.objects.get(action="order.delete", order_no=main.order_no)
        self.assertEqual(main_log.reason, "")
        for child in (sub_a, sub_b):
            log = OperationLog.objects.get(action="order.delete", order_no=child.order_no)
            self.assertEqual(log.reason, "随拆单组主单删除")
        # 恰好三条（不多不少——幂等语义的另一半）
        self.assertEqual(OperationLog.objects.filter(action="order.delete").count(), 3)

    def test_cascade_skips_already_deleted_members_without_touching_their_history(self):
        """已软删成员**幂等跳过**：它的 `deleted_at` / 删单留痕是自己的历史，不被刷新。"""
        main, sub_a, sub_b, _ = self._group_of_three()
        earlier = timezone.now() - timedelta(days=1)
        sub_b.deleted_at = earlier
        sub_b.deleted_by = "更早的那次删除"
        sub_b.save(update_fields=["deleted_at", "deleted_by"])
        body = self.client.post(
            f"{self.base}/{main.order_no}/delete/",
            {"isDelEmp": "Y"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        sub_b.refresh_from_db()
        self.assertEqual(sub_b.deleted_at, earlier)          # 历史不动
        self.assertEqual(sub_b.deleted_by, "更早的那次删除")
        self.assertEqual(sub_b.split_id, "")                 # 收尾仍清（R7）
        # 子单 b 没有第二条删单留痕
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.delete", order_no=sub_b.order_no
            ).count(),
            0,
        )
        # 主单与子单 a 照常级联
        main.refresh_from_db()
        sub_a.refresh_from_db()
        self.assertIsNotNone(main.deleted_at)
        self.assertIsNotNone(sub_a.deleted_at)

    # ── G9：恢复不回组 ────────────────────────────────────────────────────

    def test_restoring_a_deleted_member_does_not_rejoin_the_group(self):
        """两成员组删一个（prune）再恢复 ⇒ 各自仍是独立单（「删组即终局」R7）。"""
        main = self._member(
            "20261101000000000211", "主项目", total_price=self.MAIN_TOTAL
        )
        sub = self._member("20261101000000000212", "唯一子单")
        first = self.client.post(
            f"{self.base}/{sub.order_no}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        ).json()
        self.assertEqual(first["code"], 0, first)
        main.refresh_from_db()
        self.assertEqual(main.split_id, "")  # 前置哨兵：prune 已发生（G9 剩 1 清尾）
        second = self.client.post(
            f"{self.base}/{sub.order_no}/restore/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(second["code"], 0, second)
        main.refresh_from_db()
        sub.refresh_from_db()
        self.assertEqual(main.split_id, "")  # 恢复不回组
        self.assertEqual(sub.split_id, "")

    # ── G8：出组收口（补发货 + 主单提成）──────────────────────────────────

    def test_deleting_the_blocking_refunded_member_ships_and_pays_the_main(self):
        """全组只剩一个退单子单挡路 ⇒ 删它 ⇒ 同事务补调度发货 + 主单 SALES 生效。

        期望提成钉**字面量** `80.00`：主单 300 − **在组**打手 (主单 0 + 甲 100) = 200
        利润 × 40%。乙（退单挡路的那张）删除即出组、`split_id` 已清 ⇒ 它的 50 **不在**
        减项里（spec G13 成员集合定义「已软删成员自然不在」+ R8 的字面口径）。
        """
        main, sub_a, sub_b, _ = self._group_of_three(
            statuses=(OrderStatus.COMPLETED, OrderStatus.COMPLETED, OrderStatus.REFUNDED),
            sales=True,
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub_b.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        # 收口补发货：排的正是**主单**那一跳（走同一个 G6 闸；组内无人发过 ⇒ 放行）
        self.assertEqual(dispatch.call_count, 1, dispatch.call_args_list)
        self.assertEqual(dispatch.call_args.args[0], main.order_no)
        # 主单 SALES 生效：减项 = 在组成员的组和（乙已出组不计，R8）
        customer = Performance.objects.get(order=main, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("80.00"))
        self.assertEqual(customer.review_status, "已通过")
        sub_b.refresh_from_db()
        self.assertEqual(sub_b.split_id, "")

    def test_deleting_a_member_ships_with_refunded_sibling_but_waits_to_settle_sales(self):
        main, sub_a, _, _ = self._group_of_three(
            statuses=(OrderStatus.COMPLETED, OrderStatus.IN_PROGRESS, OrderStatus.REFUNDED),
            sales=True,
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub_a.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_called_once_with(main.order_no)
        customer = Performance.objects.get(order=main, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(customer.review_status)

    def test_closure_does_not_reship_when_a_member_already_shipped(self):
        """组内已有 SUCCESS 发货行（G7 组幂等）⇒ 收口只走闸、闸只补日志不重发。"""
        from apps.business.models import SendResult, TaobaoOrderSendLog

        main, sub_a, sub_b, _ = self._group_of_three(
            statuses=(OrderStatus.COMPLETED, OrderStatus.COMPLETED, OrderStatus.REFUNDED),
            sales=True,
        )
        TaobaoOrderSendLog.objects.create(
            order_no=sub_a.order_no, tb_order_no="22000000000000000901",
            result=SendResult.SUCCESS, sent_at=timezone.now(),
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub_b.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_not_called()  # 闸拦下（幂等），不排队列
        self.assertEqual(
            TaobaoOrderSendLog.objects.count(), 1  # 没有第二行发货记录
        )
        # 幂等补的是「成功」订单日志（与 D7 同形）
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.ship_result", order_no=main.order_no
            ).count(),
            1,
        )

    def test_no_closure_when_the_remaining_group_is_not_fully_completed(self):
        """剩余组还有未完成成员 ⇒ 不补发、主单提成不动（闸的否定面）。"""
        main, sub_a, sub_b, _ = self._group_of_three(
            statuses=(OrderStatus.COMPLETED, OrderStatus.IN_PROGRESS, OrderStatus.REFUNDED),
            sales=True,
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub_b.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        dispatch.assert_not_called()
        customer = Performance.objects.get(order=main, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))  # 组未齐 ⇒ 保持 0

    # ── M3：prune 到 1 的收口（业务方裁定「剩 1 = 变回正常单」）─────

    def _group_of_two(self, *, main_status, sub_status, order_no_main, order_no_sub,
                      sales=False, sales_bonus=Decimal("0.00")):
        """主单 + 一个子单（直建，不经完成钩子 ⇒ 完成了也没有任何发货调度）。"""
        sales_user = self._sales_context() if sales else None
        main = self._member(
            order_no_main, "主项目", total_price=self.MAIN_TOTAL,
            status=main_status, sales_user=sales_user,
        )
        sub = self._member(order_no_sub, "唯一子单", status=sub_status)
        self._performance(main, EmpType.MAIN)
        self._performance(sub, EmpType.MAIN, bonus=self.TAKER_BONUS_A)
        if sales:
            self._performance(main, EmpType.SALES, user=sales_user, bonus=sales_bonus)
        return main, sub

    def test_pruning_to_one_ships_the_completed_survivor_as_a_plain_order(self):
        """**M3 正面钉**：两成员组、幸存者已完成且无发货行 ⇒ 删挡路子单后
        dispatch 被调度**恰好一次** + 客服提成按**正常单**口径重算。

        期望提成钉**字面量** `120.00`：幸存主单 `split_id` 已清 ⇒ 减项走
        `taker_bonus_base` **非组支** = 本单 MAIN 行 bonus `0.00`，
        (300 − 0) × 40%。对照 G8 组口径那条的 `80.00`——同样的主单价钱，
        减项不同，正是两条收口的分界（幸存单已不是组，不吃组和减项）。

        弄红证明：把 `recycle.py` prune 分支里的 M3 块删掉（或把它的
        `COMPLETED` 判据改成恒 `False`）⇒ dispatch 断言先红（零调度），
        SALES 断言随后红（`bonus_amount` 仍 `0.00`、`review_status` 仍 `None`）。
        """
        from apps.business.models import SendResult, TaobaoOrderSendLog

        main, sub = self._group_of_two(
            main_status=OrderStatus.COMPLETED,
            sub_status=OrderStatus.COMPLETED,
            order_no_main="20261101000000000411",
            order_no_sub="20261101000000000412",
            sales=True,
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.split_id, "")     # 前置哨兵：prune 已发生（G9 剩 1 清尾）
        # 调度恰好一次：排的正是幸存单那一跳（`split_id` 已清 ⇒ 闸属性短路直放）
        self.assertEqual(dispatch.call_count, 1, dispatch.call_args_list)
        self.assertEqual(dispatch.call_args.args[0], main.order_no)
        # 直放而不是被闸拦下：幸存单名下没有「暂不发货」日志
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.split_ship_hold", order_no=main.order_no
            ).count(),
            0,
        )
        # 提成按正常单重算：非组支减项 = 本单 MAIN bonus 0.00 ⇒ (300−0)×40%
        customer = Performance.objects.get(order=main, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("120.00"))
        self.assertEqual(customer.review_status, "已通过")
        # 调度只是入队（on_commit 桩接住）⇒ 没有真发货记录行
        self.assertEqual(TaobaoOrderSendLog.objects.count(), 0)

    def test_pruning_to_one_is_silent_when_the_survivor_already_shipped(self):
        """**M3 幂等钉**：幸存者已有 SUCCESS 发货行 ⇒ 零调度、提成一个字节不动。

        幸存主单的 SALES 行预置一个**哨兵值** `7.77`（不是任何算式会算出的数），
        删除后它必须原样——重算发生就会把它冲成算式值，一测便知。

        弄红证明：把 M3 块里的 `TaobaoOrderSendLog SUCCESS` 判据去掉 ⇒
        `dispatch.assert_not_called()` 先红（闸虽直放但货已发过，重排是重复发货），
        哨兵断言随后红（SALES 被 (300−0)×40% = 120 冲掉）。
        """
        from apps.business.models import SendResult, TaobaoOrderSendLog

        main, sub = self._group_of_two(
            main_status=OrderStatus.COMPLETED,
            sub_status=OrderStatus.COMPLETED,
            order_no_main="20261101000000000421",
            order_no_sub="20261101000000000422",
            sales=True,
            sales_bonus=Decimal("7.77"),
        )
        TaobaoOrderSendLog.objects.create(
            order_no=main.order_no, tb_order_no="22000000000000000911",
            result=SendResult.SUCCESS, sent_at=timezone.now(),
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.split_id, "")     # prune 本身照常发生（G9 与 M3 各管各的）
        dispatch.assert_not_called()            # 零调度
        self.assertEqual(
            TaobaoOrderSendLog.objects.count(), 1  # 没有第二行发货记录
        )
        customer = Performance.objects.get(order=main, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("7.77"))  # 哨兵原样 = 零重算
        self.assertIsNone(customer.review_status)

    def test_pruning_to_one_does_nothing_while_the_survivor_is_incomplete(self):
        """**M3 否定面**：幸存者未完成 ⇒ 零动作（它自己完成那一跳走正常路）。

        幸存主单停在待验收：删除挡路的已完成子单后既不调度也不重算——
        「完成后自动发货 + 完成即生效」归它**自己的完成事件**管，删除路径不代办。

        弄红证明：把 M3 块里的 `COMPLETED` 判据去掉 ⇒ `dispatch.assert_not_called()`
        先红（未完成单被排了发货），SALES 断言随后红（未完成单提成被写数）。
        """
        main, sub = self._group_of_two(
            main_status=OrderStatus.PENDING_REVIEW,
            sub_status=OrderStatus.COMPLETED,
            order_no_main="20261101000000000431",
            order_no_sub="20261101000000000432",
            sales=True,
        )
        with mock.patch.object(xgj_ship, "dispatch_auto_ship") as dispatch, \
                self.captureOnCommitCallbacks(execute=True):
            body = self.client.post(
                f"{self.base}/{sub.order_no}/delete/",
                {"isDelEmp": "N"},
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        main.refresh_from_db()
        self.assertEqual(main.split_id, "")     # prune 照常（清尾不依赖收口）
        self.assertEqual(main.status, OrderStatus.PENDING_REVIEW)  # 状态没人动
        dispatch.assert_not_called()
        customer = Performance.objects.get(order=main, emp_type=EmpType.SALES)
        self.assertEqual(customer.bonus_amount, Decimal("0.00"))
        self.assertIsNone(customer.review_status)


class SplitPeerInboundNegativeTests(TenantTestCase):
    """**G2 例外钉子**：peer 入向 `order.update` 白名单可写 `total_price`
    （`peer_inbound.py` 的 `_UPDATE_FIELDS`），但子单**没有 `PeerOrderLink`**
    （拆分建单不建链）⇒ 该路对子单**不可达**——事件按链解析，解析不到就跳过
    （`unknown_order`），⛔ 不按 `order_no` 全表兜底。
    """

    PEER_NO = "90000000000000000002"

    def setUp(self):
        from apps.business.models import PeerUpstreamBinding
        from apps.business.peer_sync import hash_token, new_token

        self.game = Game.objects.create(name="拆单负向夹具游戏")
        kefu = get_user_model().objects.create_user(
            username="peer-negative-kefu", password="pw-kefu"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室乙",
            peer_base_url="https://peer-x.test.invalid",
            peer_account="peer-account-x",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-y.test.invalid",
            bound_by=kefu,
        )
        self.main = Order.objects.create(
            game=self.game, order_no="20261101000000000301",
            total_price=Decimal("300.00"), split_id="71000000000000000003",
        )
        self.child = Order.objects.create(
            game=self.game, order_no="20261101000000000302",
            total_price=Decimal("0.00"), split_id="71000000000000000003",
        )

    def test_an_update_event_cannot_reach_a_child_order_without_a_link(self):
        """子单没有 `PeerOrderLink` ⇒ 入向事件**够不着它**：对端拿子单单号点名时，
        入向按「未建链 ⇒ 首条事件建一张**新单**」处理（`peer_inbound` 的既有口径），
        新单是独立行（`split_id` 空串）——⛔ 不是改到子单头上。子单接单价一个字节不动。
        """
        from apps.business.models import PeerOrderLink
        from apps.business.peer_inbound import apply_events

        outcome = apply_events(
            [
                {
                    "kind": "order.update",
                    "target_ref": self.child.order_no,  # 对端点名子单
                    "occurred_at": datetime(
                        2026, 11, 1, 12, 0, tzinfo=datetime_utc.utc
                    ).isoformat(),
                    # 载荷形状照 `tests/test_peer_inbound_update.py` 的既有事件
                    # （白名单键齐全；`total_price` 在更新白名单里 ⇒ 若能达就会改掉它）。
                    "payload": {
                        "order_no": self.PEER_NO,
                        "status": OrderStatus.IN_PROGRESS,
                        "total_price": "88.00",  # 想改子单接单价
                        "remark": "新备注",
                        "project_text": "新项目名",
                        "customer_phone": "新电话",
                        "customer_qq": "新QQ",
                        "content": "新需求",
                        "game_name": self.game.name,
                    },
                }
            ],
            binding=self.binding,
        )
        self.assertEqual(outcome.applied, 1)  # 非空哨兵：事件真被处理了（不是静默丢）
        # 事件落到一张**新的独立单**上（建单支），不是子单（那张新单的价格列怎么写
        # 是入向自己的口径，不在本线判据内）
        created = Order.objects.exclude(
            pk__in=[self.main.pk, self.child.pk]
        ).filter(split_id="")
        self.assertEqual(created.count(), 1)
        # 子单：接单价 / 组身份一个字节没动；也没有链指到它
        self.child.refresh_from_db()
        self.assertEqual(self.child.total_price, Decimal("0.00"))
        self.assertEqual(self.child.split_id, "71000000000000000003")
        self.assertFalse(
            PeerOrderLink.objects.filter(order=self.child).exists()
        )
