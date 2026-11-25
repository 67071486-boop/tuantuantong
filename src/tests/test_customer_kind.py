"""新老客标签（2026-11-25）的判据：自动判定 / 手动覆盖 / 筛选 / 回填 / 写入面。

口径（已与业务方确认）：
- **当前店铺** = `Order.order_source`（一家店 = 一个来源字典项），⛔ 不看 `source` 文本；
- 同来源、同客户、未删除的历史单**只要有一单** ⇒ 老客；否则新客。退款/取消/已完成都算；
- 标签是**录单时快照**：后来的订单不会回改历史标签；手动覆盖优先于自动；
- 没有 `customer`（无 buyer_eid 手工单）或没有 `order_source` ⇒ 自动判定为空，不按昵称猜；
- 回填只碰有 customer + order_source 的未删除单，保留手动标签，分批、幂等、可续跑。

全部夹具合成（E-0077）。
"""
from datetime import datetime
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.db.models.query import QuerySet
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    Customer,
    CustomerKind,
    Game,
    Order,
    OrderSource,
    OrderStatus,
)
from tests.p5_auth_helpers import make_logged_in_client
from tests.xlsx_helpers import workbook_of

BASE = "/api/business/orders"
LIST = "/api/business/orders/"
EXPORT = "/api/business/orders/export"
EID_A = "kind-eid-a-0001"
EID_B = "kind-eid-b-0001"
EID_C = "kind-eid-c-0001"


def _at(hour, minute=0):
    """合成历史时刻（固定日期 ⇒ 回填排序稳定，不受用例运行时刻影响）。"""
    return timezone.make_aware(datetime(2026, 6, 1, hour, minute))


class _KindFixture(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(username="kind-admin", is_superuser=True)
        self.game = Game.objects.create(name="新老客游戏")
        self.source_a = OrderSource.objects.create(name="新老客店铺甲")
        self.source_b = OrderSource.objects.create(name="新老客店铺乙")

    def _create(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "新老客项目",
            "total_price": "10.00",
        }
        payload.update(overrides)
        body = self.client.post(BASE + "/", payload, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _row(self, order_no):
        return Order.objects.get(order_no=order_no)

    def _history(self, no, *, customer=None, order_source=None, created_at=None, **overrides):
        order = Order.objects.create(
            order_no=no,
            game=self.game,
            project_text="历史项目",
            total_price=Decimal("10.00"),
            customer=customer,
            order_source=order_source,
            **overrides,
        )
        if created_at is not None:
            Order.objects.filter(pk=order.pk).update(created_at=created_at)
            order.refresh_from_db()
        return order

    def _list(self, query=""):
        body = self.client.get(LIST + query).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]


class IntakeCustomerKindTests(_KindFixture):
    """录单时的自动判定、快照语义与手动覆盖。"""

    def test_first_order_at_source_is_new_and_second_is_old(self):
        first = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        self.assertEqual(first["customer_kind"], "new")
        self.assertIs(first["customer_kind_is_manual"], False)
        first_order = self._row(first["order_no"])

        second = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        self.assertEqual(second["customer_kind"], "old")
        self.assertIs(second["customer_kind_is_manual"], False)

        # 快照：第二单不会回改第一单。
        first_order.refresh_from_db()
        self.assertEqual(first_order.customer_kind, "new")

    def test_cross_source_order_is_new_for_that_source(self):
        first = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        self.assertEqual(first["customer_kind"], "new")

        cross = self._create(buyer_eid=EID_A, order_source=self.source_b.pk)
        self.assertEqual(cross["customer_kind"], "new", "跨来源不算老客")

        # 回到来源 A：A 已经有一单 ⇒ 老客（同客户的来源维度各自独立）。
        back = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        self.assertEqual(back["customer_kind"], "old")

    def test_refunded_and_completed_history_still_count_as_ordered(self):
        for index, status in enumerate((OrderStatus.REFUNDED, OrderStatus.COMPLETED)):
            with self.subTest(status=status):
                eid = f"kind-eid-status-{index}"
                first = self._create(buyer_eid=eid, order_source=self.source_a.pk)
                Order.objects.filter(order_no=first["order_no"]).update(status=status)

                second = self._create(buyer_eid=eid, order_source=self.source_a.pk)
                self.assertEqual(second["customer_kind"], "old")

    def test_soft_deleted_history_does_not_count(self):
        first = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        Order.objects.filter(order_no=first["order_no"]).update(deleted_at=timezone.now())

        second = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        self.assertEqual(second["customer_kind"], "new", "已删除的历史单不算下过单")

    def test_manual_override_wins_over_auto_and_is_marked_manual(self):
        self._create(buyer_eid=EID_A, order_source=self.source_a.pk)

        overridden = self._create(
            buyer_eid=EID_A,
            order_source=self.source_a.pk,
            customer_kind_override="new",
        )
        self.assertEqual(overridden["customer_kind"], "new", "手动覆盖优先于自动判老客")
        self.assertIs(overridden["customer_kind_is_manual"], True)

        another = self._create(
            buyer_eid=EID_B,
            order_source=self.source_a.pk,
            customer_kind_override="old",
        )
        self.assertEqual(another["customer_kind"], "old", "首单也可手动指定老客")
        self.assertIs(another["customer_kind_is_manual"], True)

    def test_manual_order_without_buyer_id_has_no_auto_tag_but_can_be_overridden(self):
        plain = self._create(order_source=self.source_a.pk)
        self.assertEqual(plain["customer_kind"], "")
        self.assertIs(plain["customer_kind_is_manual"], False)

        overridden = self._create(
            order_source=self.source_a.pk,
            customer_kind_override="old",
        )
        self.assertEqual(overridden["customer_kind"], "old")
        self.assertIs(overridden["customer_kind_is_manual"], True)
        self.assertIsNone(self._row(overridden["order_no"]).customer_id)

    def test_phone_only_order_is_new_then_old_in_the_same_source(self):
        first = self._create(
            order_source=self.source_a.pk, customer_phone="19922729946",
        )
        self.assertEqual(first["customer_kind"], "new")
        second = self._create(
            order_source=self.source_a.pk, customer_phone="+86 199-2272-9946",
        )
        self.assertEqual(second["customer_kind"], "old", "电话相同即同一客户")
        self.assertIsNone(self._row(second["order_no"]).customer_id)

    def test_phone_identity_does_not_cross_sources(self):
        self._create(order_source=self.source_a.pk, customer_phone="15001198511")
        cross = self._create(order_source=self.source_b.pk, customer_phone="15001198511")
        self.assertEqual(cross["customer_kind"], "new")
        back = self._create(order_source=self.source_a.pk, customer_phone="15001198511")
        self.assertEqual(back["customer_kind"], "old")

    def test_phone_or_buyer_id_either_one_marks_old_within_source(self):
        self._create(
            buyer_eid=EID_A, order_source=self.source_a.pk, customer_phone="19922729946",
        )
        # 同一电话，但没有买家 ID ⇒ 电话命中，老客。
        by_phone = self._create(
            order_source=self.source_a.pk, customer_phone="19922729946",
        )
        self.assertEqual(by_phone["customer_kind"], "old")
        # 同一买家 ID，但换了电话 ⇒ ID 命中，仍是老客。
        by_id = self._create(
            buyer_eid=EID_A, order_source=self.source_a.pk, customer_phone="15001198511",
        )
        self.assertEqual(by_id["customer_kind"], "old")

    def test_order_without_source_has_no_auto_tag(self):
        first = self._create(buyer_eid=EID_A)
        self.assertEqual(first["customer_kind"], "")
        self.assertIs(first["customer_kind_is_manual"], False)

        second = self._create(buyer_eid=EID_A)
        self.assertEqual(second["customer_kind"], "", "没有当前店铺 ⇒ 不判新老客")

    def test_detail_split_copies_the_group_tag_and_customer_to_children(self):
        created = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        response = self.client.post(
            f"{BASE}/{created['order_no']}/split/",
            {"projects": ["段一", "段二", "段三"]},
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order = self._row(created["order_no"])
        group = list(Order.objects.filter(split_id=order.split_id).order_by("id"))
        self.assertEqual(len(group), 3)
        self.assertEqual({member.customer_kind for member in group}, {"new"})
        self.assertEqual({member.customer_id for member in group}, {order.customer_id})

    def test_split_group_members_share_the_same_tag(self):
        first = self._create(
            buyer_eid=EID_A,
            order_source=self.source_a.pk,
            project_text="段一/段二/段三",
            split=True,
        )
        group = list(Order.objects.filter(split_id=first["split_id"]).order_by("id"))
        self.assertEqual(len(group), 3)
        self.assertEqual({order.customer_kind for order in group}, {"new"})
        self.assertEqual({order.customer_kind_is_manual for order in group}, {False})

        second = self._create(
            buyer_eid=EID_A,
            order_source=self.source_a.pk,
            project_text="段一/段二",
            split=True,
        )
        group = list(Order.objects.filter(split_id=second["split_id"]).order_by("id"))
        self.assertEqual(len(group), 2)
        self.assertEqual({order.customer_kind for order in group}, {"old"})


class CustomerKindReadAndFilterTests(_KindFixture):
    """列表 / 详情读标签；筛选按**存储的**标签。"""

    def test_list_and_detail_payload_carry_the_stored_tag(self):
        created = self._create(
            buyer_eid=EID_A,
            order_source=self.source_a.pk,
            customer_kind_override="old",
        )
        row = next(item for item in self._list() if item["order_no"] == created["order_no"])
        self.assertEqual(row["customer_kind"], "old")
        self.assertIs(row["customer_kind_is_manual"], True)

        detail = self.client.get(f"{LIST}{created['order_no']}/").json()
        self.assertEqual(detail["code"], 0, detail)
        self.assertEqual(detail["data"]["customer_kind"], "old")
        self.assertIs(detail["data"]["customer_kind_is_manual"], True)

    def test_filter_uses_the_stored_snapshot_not_a_recomputed_label(self):
        first = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        second = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        manual_old = self._create(
            buyer_eid=EID_B,
            order_source=self.source_a.pk,
            customer_kind_override="old",
        )
        blank = self._create(order_source=self.source_a.pk)

        self.assertEqual(first["customer_kind"], "new")
        self.assertEqual(second["customer_kind"], "old", "自动档第二单应判老客")

        new_rows = self._list("?customer_kind=new")
        self.assertEqual({row["order_no"] for row in new_rows}, {first["order_no"]})
        old_rows = self._list("?customer_kind=old")
        self.assertEqual(
            {row["order_no"] for row in old_rows},
            {second["order_no"], manual_old["order_no"]},
        )
        self.assertNotIn(blank["order_no"], {row["order_no"] for row in new_rows + old_rows})

        invalid = self.client.get(LIST + "?customer_kind=bogus").json()
        self.assertEqual(invalid["code"], 1000, invalid)

    def test_export_forwards_the_customer_kind_filter(self):
        first = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        second = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        self.assertNotEqual(first["customer_kind"], second["customer_kind"])

        response = self.client.get(EXPORT + "?customer_kind=old")
        self.assertEqual(response.status_code, 200, response.content[:200])
        exported = [
            row[1].value
            for row in workbook_of(response)["Sheet1"].iter_rows(min_row=2)
        ]
        self.assertIn(second["order_no"], exported)
        self.assertNotIn(first["order_no"], exported)


class CustomerKindWriteGuardTests(_KindFixture):
    """请求体不是标签的权威来源：标签由服务端算（自动或专用覆盖键）。"""

    def test_intake_payload_cannot_write_the_columns_directly(self):
        created = self._create(
            buyer_eid=EID_A,
            order_source=self.source_a.pk,
            customer_kind="old",
            customer_kind_is_manual=True,
        )
        self.assertEqual(created["customer_kind"], "new", "首单自动判新客，请求体那两列被忽略")
        self.assertIs(created["customer_kind_is_manual"], False)

        plain = self._create(
            order_source=self.source_a.pk,
            customer_kind="old",
            customer_kind_is_manual=True,
        )
        self.assertEqual(plain["customer_kind"], "")
        self.assertIs(plain["customer_kind_is_manual"], False)

    def test_patch_cannot_write_the_columns(self):
        created = self._create(buyer_eid=EID_A, order_source=self.source_a.pk)
        response = self.client.patch(
            f"{BASE}/{created['order_no']}/",
            {
                "customer_kind": "old",
                "customer_kind_is_manual": True,
                "remark": "阳性对照",
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        row = self._row(created["order_no"])
        self.assertEqual(row.customer_kind, "new")
        self.assertIs(row.customer_kind_is_manual, False)
        self.assertEqual(row.remark, "阳性对照")

    def test_the_override_key_is_not_an_order_column(self):
        created = self._create(
            buyer_eid=EID_A,
            order_source=self.source_a.pk,
            customer_kind_override="old",
        )
        row = self._row(created["order_no"])
        self.assertEqual(row.customer_kind, "old")
        self.assertIs(row.customer_kind_is_manual, True)
        # 覆盖键只影响标签，不会变成订单上的第二列。
        self.assertFalse(hasattr(row, "customer_kind_override"))


class BackfillCustomerKindCommandTests(_KindFixture):
    """回填命令：分组序 / 手动保留 / 软删不计 / 无客户或无来源不碰 / 分批 / 幂等 / 无 PII。"""

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(eid=EID_A, buyer_nick="买家昵称甲")
        self.customer_2 = Customer.objects.create(eid=EID_B, buyer_nick="买家昵称乙")
        self.customer_3 = Customer.objects.create(eid=EID_C, buyer_nick="买家昵称丙")

        # C1 / 店铺甲：首单 new、次单 old、已删单不参与计数。
        self.c1_a1 = self._history(
            "20260601000000000001", customer=self.customer,
            order_source=self.source_a, created_at=_at(8),
        )
        self.c1_a2 = self._history(
            "20260601000000000002", customer=self.customer,
            order_source=self.source_a, created_at=_at(9),
        )
        self.c1_a3 = self._history(
            "20260601000000000003", customer=self.customer,
            order_source=self.source_a, created_at=_at(10),
            deleted_at=timezone.now(),
        )
        # C1 / 店铺乙：只有一单 ⇒ new（跨来源不继承店铺甲的老客）。
        self.c1_b1 = self._history(
            "20260601000000000004", customer=self.customer,
            order_source=self.source_b, created_at=_at(11),
        )
        # C2 / 店铺甲：一单 new；同组第二单已有手动 old ⇒ 保留、且算作「下过单」。
        self.c2_a1 = self._history(
            "20260601000000000005", customer=self.customer_2,
            order_source=self.source_a, created_at=_at(12),
        )
        self.c2_a2 = self._history(
            "20260601000000000006", customer=self.customer_2,
            order_source=self.source_a, created_at=_at(13),
            customer_kind=CustomerKind.OLD, customer_kind_is_manual=True,
        )
        # C3 / 店铺甲：最早一单已软删 ⇒ 剩余第一单仍是 new。
        self._history(
            "20260601000000000007", customer=self.customer_3,
            order_source=self.source_a, created_at=_at(7),
            deleted_at=timezone.now(),
        )
        self.c3_a1 = self._history(
            "20260601000000000008", customer=self.customer_3,
            order_source=self.source_a, created_at=_at(8),
        )
        # 无客户 / 无来源：保持空，不按昵称猜、也不报错。
        self.no_customer = self._history(
            "20260601000000000009", order_source=self.source_a, created_at=_at(9),
        )
        self.no_source = self._history(
            "20260601000000000010", customer=self.customer_3, created_at=_at(10),
        )
        # 手动标签在最前面：后面的空标签要按「前面已有一单」判 old。
        self.c4_a1 = self._history(
            "20260601000000000011", customer=Customer.objects.create(eid="kind-eid-d-0001"),
            order_source=self.source_a, created_at=_at(14),
            customer_kind=CustomerKind.NEW, customer_kind_is_manual=True,
        )
        self.c4_a2 = self._history(
            "20260601000000000012", customer=self.c4_a1.customer,
            order_source=self.source_a, created_at=_at(15),
        )

    def _run(self, **kwargs):
        out = StringIO()
        call_command(
            "backfill_customer_kind",
            schema=connection.schema_name,
            stdout=out,
            **kwargs,
        )
        return out.getvalue()

    def test_backfill_labels_existing_orders_idempotently_and_keeps_manual(self):
        output = self._run(batch_size=1)
        for order in (
            self.c1_a1, self.c1_a2, self.c1_b1, self.c2_a1, self.c3_a1,
            self.c4_a1, self.c4_a2,
        ):
            order.refresh_from_db()
        self.assertEqual(self.c1_a1.customer_kind, "new")
        self.assertEqual(self.c1_a2.customer_kind, "old")
        self.assertEqual(self.c1_a3.customer_kind, "", "已软删的单不碰")
        self.assertEqual(self.c1_b1.customer_kind, "new")
        self.assertEqual(self.c2_a1.customer_kind, "new")
        self.assertEqual(self.c2_a2.customer_kind, "old")
        self.assertIs(self.c2_a2.customer_kind_is_manual, True, "手动标签必须保留")
        self.assertEqual(self.c3_a1.customer_kind, "new", "软删的历史单不参与计数")
        self.assertEqual(self.no_customer.customer_kind, "")
        self.assertEqual(self.no_source.customer_kind, "")
        self.assertEqual(self.c4_a1.customer_kind, "new")
        self.assertIs(self.c4_a1.customer_kind_is_manual, True)
        self.assertEqual(self.c4_a2.customer_kind, "old", "手动单也算下过单")

        # 输出只有计数，不含 eid / 昵称。
        self.assertNotIn(EID_A, output)
        self.assertNotIn("买家昵称甲", output)

        # 第二次跑：一条都不写（幂等）。
        with mock.patch.object(QuerySet, "bulk_update") as bulk_update:
            second_output = self._run(batch_size=1)
        self.assertFalse(bulk_update.called, "第二次回填不应发生批量写入")
        self.assertIn("本次写入 0", second_output)
        # 所有「有客户 + 有来源」的在库单都已带上标签（含原本手动的两单）。
        self.assertEqual(
            Order.objects.filter(
                customer__isnull=False, order_source__isnull=False
            ).exclude(customer_kind="").count(),
            8,
        )

    def test_backfill_keeps_a_split_group_on_one_tag(self):
        customer = Customer.objects.create(eid="kind-eid-split-0001", buyer_nick="拆单买家")
        first = self._history(
            "20260601000000000101", customer=customer, order_source=self.source_a,
            created_at=_at(16), split_id="kind-split-group-1",
        )
        second = self._history(
            "20260601000000000102", customer=customer, order_source=self.source_a,
            created_at=_at(16), split_id="kind-split-group-1",
        )
        next_order = self._history(
            "20260601000000000103", customer=customer, order_source=self.source_a,
            created_at=_at(17),
        )
        self._run(batch_size=10)
        for order in (first, second, next_order):
            order.refresh_from_db()
        self.assertEqual(first.customer_kind, "new")
        self.assertEqual(second.customer_kind, "new", "拆单组整组同标签")
        self.assertEqual(next_order.customer_kind, "old", "下一笔购买才是老客")

    def test_backfill_uses_phone_when_customer_is_missing(self):
        first = self._history(
            "20260601000000000201", order_source=self.source_a,
            customer_phone="19922729946", created_at=_at(18),
        )
        second = self._history(
            "20260601000000000202", order_source=self.source_a,
            customer_phone="+86 199-2272-9946", created_at=_at(19),
        )
        self._run(batch_size=10)
        for order in (first, second):
            order.refresh_from_db()
        self.assertEqual(first.customer_kind, "new")
        self.assertEqual(second.customer_kind, "old", "电话命中同来源历史单")

    def test_backfill_matches_customer_or_phone_as_one_identity(self):
        customer = Customer.objects.create(eid="kind-eid-mixed-0001")
        first = self._history(
            "20260601000000000203", customer=customer,
            order_source=self.source_a, customer_phone="15001198511", created_at=_at(20),
        )
        second = self._history(
            "20260601000000000204", order_source=self.source_a,
            customer_phone="15001198511", created_at=_at(21),
        )
        self._run(batch_size=10)
        for order in (first, second):
            order.refresh_from_db()
        self.assertEqual(first.customer_kind, "new")
        self.assertEqual(second.customer_kind, "old", "有 ID 或有电话都算同一客户")

    def test_backfill_batches_writes_by_batch_size(self):
        sizes = []
        original = QuerySet.bulk_update

        def spy(queryset, objs, fields, batch_size=None):
            sizes.append(len(objs))
            return original(queryset, objs, fields, batch_size=batch_size)

        with mock.patch.object(QuerySet, "bulk_update", spy):
            self._run(batch_size=2)
        self.assertTrue(sizes, "应有批量写入")
        self.assertLessEqual(max(sizes), 2)

    def test_unknown_schema_is_a_command_error(self):
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            call_command("backfill_customer_kind", schema="no-such-schema")
