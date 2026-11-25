from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import CommissionMode, MemberType
from apps.accounts.models import StaffProfile
from apps.business.api import order_views
from apps.business.models import (
    Approval, ApprovalKind, ApprovalSettings, ApprovalStatus, EmpType, Game, Order, OrderRefund,
    OrderStatus, Performance,
)
from apps.internal_chat.models import Conversation, OrderGroup
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


URL = "/api/business/orders/"


def create_penalty_child(parent, order_no, **fields):
    approval = Approval.objects.create(
        kind=ApprovalKind.PENALTY, status=ApprovalStatus.APPROVED, original_order=parent, reason="合成罚款",
    )
    return Order.objects.create(
        game=fields.pop("game", parent.game), order_no=order_no, penalty_approval=approval,
        status=OrderStatus.COMPLETED, hall_price=-12, **fields,
    )


class PenaltyOrderListTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="合成游戏")
        self.parent = Order.objects.create(
            game=self.game, order_no="20261116000000003001", source_order_no="源单-03001",
        )
        self.child = create_penalty_child(
            self.parent, "20261116000000003002", source_order_no="罚款-03002", created_by="系统",
        )
        self.admin = make_logged_in_client(username="penalty-list-admin", is_superuser=True)

    def fetch(self, *, client=None, **params):
        body = (client or self.admin).get(URL, params).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_grouping_keeps_legacy_list_and_summary_and_paginates_parents(self):
        legacy = self.fetch()
        self.assertEqual(legacy["total"], 2)
        self.assertNotIn("penalty_child_count", legacy["items"][0])
        unrelated = Order.objects.create(game=self.game, order_no="20261116000000003003")
        first = self.fetch(group_penalty_children=True, page_size=1)
        second = self.fetch(group_penalty_children=True, page_size=1, page=2)
        self.assertEqual(first["total"], 2)
        self.assertEqual(second["total"], 2)
        items = {row["order_no"]: row for row in first["items"] + second["items"]}
        self.assertEqual(set(items), {self.parent.order_no, unrelated.order_no})
        self.assertEqual(items[self.parent.order_no]["penalty_child_count"], 1)
        self.assertEqual(items[unrelated.order_no]["penalty_child_count"], 0)
        self.assertEqual(first["summary"], self.fetch()["summary"])
        self.assertEqual(first["summary"]["total_hall_price"], "-12.00")

    def test_grouped_and_plain_list_compute_totals_with_expected_queries(self):
        for params, total, reads in (({}, 2, 3), ({"group_penalty_children": True}, 1, 3)):
            with self.subTest(params=params), CaptureQueriesContext(connection) as captured:
                data = self.fetch(**params)
                self.assertEqual((data["total"], data["summary"]["count"]), (total, 2))
                self.assertEqual(data["summary"]["total_hall_price"], "-12.00")
            order_reads = [
                entry for entry in captured
                if entry["sql"].lstrip().upper().startswith("SELECT")
                and 'FROM "business_order"' in entry["sql"]
            ]
            # 命中金额 + 关联罚款金额 + 分页读（折叠/普通列表都要并入关联金额）。
            self.assertEqual(len(order_reads), reads)

    def test_searching_either_side_of_a_penalty_pair_nets_the_related_amounts(self):
        self.parent.total_price = Decimal("0.00")
        self.parent.hall_price = Decimal("85.00")
        self.parent.status = OrderStatus.COMPLETED
        self.parent.save(update_fields=["total_price", "hall_price", "status"])
        self.child.total_price = Decimal("0.00")
        self.child.hall_price = Decimal("-85.00")
        self.child.status = OrderStatus.IN_PROGRESS
        self.child.save(update_fields=["total_price", "hall_price", "status"])

        for keyword in (self.parent.order_no, self.child.order_no):
            with self.subTest(keyword=keyword):
                data = self.fetch(keyword=keyword)
                self.assertEqual(data["summary"]["count"], 1)
                self.assertEqual(data["summary"]["total_price"], "0.00")
                self.assertEqual(data["summary"]["total_hall_price"], "0.00")
                self.assertEqual(data["summary"]["diff"], "0.00")

    def test_multiple_penalty_children_use_the_same_family_amounts_from_any_side(self):
        self.parent.total_price = Decimal("0.00")
        self.parent.hall_price = Decimal("85.00")
        self.parent.save(update_fields=["total_price", "hall_price"])
        self.child.total_price = Decimal("0.00")
        self.child.hall_price = Decimal("-12.00")
        self.child.save(update_fields=["total_price", "hall_price"])
        sibling = create_penalty_child(self.parent, "20261116000000003004")
        sibling.total_price = Decimal("0.00")
        sibling.hall_price = Decimal("-20.00")
        sibling.save(update_fields=["total_price", "hall_price"])

        for keyword in (self.parent.order_no, self.child.order_no, sibling.order_no):
            with self.subTest(keyword=keyword):
                data = self.fetch(keyword=keyword)
                self.assertEqual(data["summary"]["total_hall_price"], "53.00")
                self.assertEqual(data["summary"]["diff"], "-53.00")

    def test_later_pages_load_display_fields_only_for_the_selected_orders(self):
        Order.objects.create(game=self.game, order_no="20261116000000003003")
        for grouped, expected in ((False, self.child), (True, self.parent)):
            with self.subTest(grouped=grouped):
                with CaptureQueriesContext(connection) as captured:
                    data = self.fetch(group_penalty_children=grouped, page=2, page_size=1)
                self.assertEqual([row["order_no"] for row in data["items"]], [expected.order_no])
                self.assertEqual((data["total"], data["summary"]["count"]), (2 if grouped else 3, 3))
                statements = [entry["sql"] for entry in captured]
                page_sql = next(
                    sql for sql in statements if 'FROM "business_order"' in sql and "LIMIT 1 OFFSET 1" in sql
                )
                self.assertNotIn('"taker_name"', page_sql)
                ids = f'"business_order"."id" IN ({expected.pk})'
                self.assertTrue(any(ids in sql and '"taker_name"' in sql for sql in statements))

    def test_penalty_expansion_picks_children_before_loading_display_fields(self):
        with CaptureQueriesContext(connection) as captured:
            data = self.fetch(penalty_parent_order_no=self.parent.order_no, page_size=1)
        self.assertEqual([row["order_no"] for row in data["items"]], [self.child.order_no])
        statements = [entry["sql"] for entry in captured]
        ids = f'"business_order"."id" IN ({self.child.pk})'
        self.assertTrue(any(ids in sql and '"penalty_parent_order_no"' in sql for sql in statements))

    def test_taker_filter_pages_ids_before_loading_display_fields(self):
        operator = get_user_model().objects.get(username="penalty-list-admin")
        taker = get_user_model().objects.create(username="page-taker")
        self.parent.total_price = Decimal("50.00")
        self.parent.split_id = "20261116000000003100"
        self.parent.save(update_fields=["total_price", "split_id"])
        other = Order.objects.create(game=self.game, order_no="20261116000000003003", total_price=20)
        sibling = Order.objects.create(
            game=self.game, order_no="20261116000000003004", split_id=self.parent.split_id,
        )
        later = [
            Order.objects.create(game=self.game, order_no=f"2026111600000000300{n}")
            for n in (5, 6)
        ]
        for order in (self.parent, self.child, other, sibling, *later):
            Performance.objects.create(order=order, user=taker, emp_type=EmpType.MAIN)
            for index in range(2):
                OrderRefund.objects.create(
                    kind=OrderRefund.Kind.PARTIAL, order=order, amount=2,
                    operator=operator.username, request_id=f"page-{order.order_no}-{index}",
                )

        for grouped in (False, True):
            with self.subTest(grouped=grouped):
                params = {
                    "group_penalty_children": grouped, "include_page_relations": grouped,
                    "kefu_type": 10, "sales_user": operator.pk, "page_size": 2, "page": 2,
                }
                expected = self.fetch(**params)
                self.assertEqual(expected["summary"]["count"], 5)
                self.assertEqual(expected["summary"]["total_price"], "70.00")
                self.assertEqual(expected["total"], 4 if grouped else 5)
                final_row = self.parent if grouped else self.child
                self.assertEqual(
                    [row["order_no"] for row in expected["items"]], [other.order_no, final_row.order_no],
                )
                if grouped:
                    self.assertEqual(
                        [row["order_no"] for row in expected["page_relations"]["split_members_by_id"][self.parent.split_id]],
                        [sibling.order_no, self.parent.order_no],
                    )

                with CaptureQueriesContext(connection) as captured:
                    result = self.fetch(**params, taker_user=taker.pk)
                self.assertEqual(result, expected)
                sql = [entry["sql"] for entry in captured]
                page_sql = next(statement for statement in sql if "LIMIT 2 OFFSET 2" in statement)
                self.assertRegex(page_sql, r'^SELECT DISTINCT "business_order"."id"')
                self.assertNotIn('"taker_name"', page_sql)
                self.assertNotIn('"penalty_child_count"', page_sql)
                self.assertNotIn('COUNT(', page_sql)
                page_ids_filter = f'"business_order"."id" IN ({other.pk}, {final_row.pk})'
                page_details = [
                    statement for statement in sql
                    if '"taker_name"' in statement and page_ids_filter in statement
                ]
                self.assertEqual(len(page_details), 1)

    def test_page_detail_read_excludes_order_that_becomes_hidden_between_queries(self):
        taker = get_user_model().objects.create(username="visibility-taker")
        Performance.objects.create(order=self.parent, user=taker, emp_type=EmpType.MAIN)
        visible_game = self.game
        hidden_game = Game.objects.create(name="无权查看的游戏")
        viewer = make_logged_in_client(
            username="visibility-viewer", groups=("可见订单",), games=[visible_game], nodes=("order.list",),
        )
        filters = {"client": viewer, "taker_user": taker.pk, "page_size": 1}
        self.assertEqual([row["order_no"] for row in self.fetch(**filters)["items"]], [self.parent.order_no])

        def move_order_after_page_ids(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if sql.startswith('SELECT "business_order"."id" AS "pk"') and sql.endswith("LIMIT 1"):
                Order.objects.filter(pk=self.parent.pk).update(game=hidden_game)
            return result

        with connection.execute_wrapper(move_order_after_page_ids):
            page = self.fetch(**filters)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"], [])
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.game_id, hidden_game.pk)

    def test_order_list_returns_retryable_error_after_postgres_cancels_a_query(self):
        def slow_summary(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(0.35)")
            return None, None

        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
        with (
            patch.object(order_views, "_ORDER_LIST_STATEMENT_TIMEOUT_MS", 100, create=True),
            patch.object(order_views, "order_list_summary", side_effect=slow_summary),
        ):
            response = self.admin.get(URL)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            "code": 503, "msg": "订单查询耗时过长，请缩小筛选范围或稍后重试。", "data": None,
        })
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            self.assertEqual(cursor.fetchone(), ("5s",))
        self.assertEqual(self.fetch()["total"], 2)

    def test_order_list_preserves_existing_timeout_and_restores_it_on_success(self):
        original_summary = order_views.order_list_summary
        for before, during in (("9s", "4s"), ("2s", "2s"), ("0", "4s")):
            with self.subTest(before=before):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)", [before])

                def inspect_timeout(*args, **kwargs):
                    with connection.cursor() as cursor:
                        cursor.execute("SHOW statement_timeout")
                        self.assertEqual(cursor.fetchone(), (during,))
                    return original_summary(*args, **kwargs)

                with patch.object(order_views, "order_list_summary", side_effect=inspect_timeout):
                    self.assertEqual(self.fetch(group_penalty_children=True)["total"], 1)
                with connection.cursor() as cursor:
                    cursor.execute("SHOW statement_timeout")
                    self.assertEqual(cursor.fetchone(), (before,))

    def test_grouped_refund_operator_counts_each_order_and_its_money_once(self):
        operator = get_user_model().objects.get(username="penalty-list-admin")
        self.parent.total_price = Decimal("50.00")
        self.parent.hall_price = Decimal("40.00")
        self.parent.refund_fee = Decimal("5.00")
        self.parent.status = OrderStatus.REFUNDED
        self.parent.save(update_fields=["total_price", "hall_price", "refund_fee", "status"])
        for order in (self.parent, self.child):
            for index in range(2):
                OrderRefund.objects.create(
                    kind=OrderRefund.Kind.PARTIAL, order=order, amount=Decimal("2.50"),
                    operator=operator.username, request_id=f"summary-{order.order_no}-{index}",
                )

        data = self.fetch(group_penalty_children=True, kefu_type=10, sales_user=operator.pk)
        self.assertEqual([row["order_no"] for row in data["items"]], [self.parent.order_no])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["summary"]["count"], 2)
        self.assertEqual(data["summary"]["total_price"], "50.00")
        self.assertEqual(data["summary"]["total_hall_price"], "28.00")
        self.assertEqual(data["summary"]["total_refund_fee"], "5.00")
        self.assertEqual(data["summary"]["diff"], "22.00")
        self.assertEqual(data["summary"]["refunded_diff"], "10.00")
        self.assertEqual(data["summary"]["status_counts"]["REFUNDED"], 1)
        self.assertEqual(data["summary"]["status_counts"]["COMPLETED"], 1)

    @override_settings(TT_CHAT_ORDER_GROUPS_ENABLED=True)
    def test_page_relations_match_legacy_order_and_include_only_page_groups(self):
        self.parent.split_id = "20261116000000003100"
        self.parent.save(update_fields=["split_id"])
        member_creator = get_user_model().objects.create(username="split-creator", first_name="合成拆单客服")
        leaf_creator = get_user_model().objects.create(username="nested-creator", first_name="合成罚款客服")
        member = Order.objects.create(
            game=self.game, order_no="20261116000000003003", split_id=self.parent.split_id,
            created_by=member_creator.username,
        )
        direct = create_penalty_child(member, "20261116000000003004")
        leaf = create_penalty_child(direct, "20261116000000003005", created_by=leaf_creator.username)
        second_group = Order.objects.create(
            game=self.game, order_no="20261116000000003006", split_id="20261116000000003101",
        )
        Order.objects.create(
            game=self.game, order_no="20261116000000003007", split_id=second_group.split_id,
        )
        params = {"group_penalty_children": True, "include_page_relations": True, "page_size": 1}
        first = self.fetch(**params)
        self.assertEqual([row["order_no"] for row in first["items"]], [second_group.order_no])
        self.assertEqual(set(first["page_relations"]["split_members_by_id"]), {second_group.split_id})
        self.assertEqual(first["page_relations"]["penalty_children_by_parent"], {})

        for order in (member, direct):
            OrderGroup.objects.create(
                conversation=Conversation.objects.create(), order=order, order_no=order.order_no, wire_id=uuid4(),
            )
        with CaptureQueriesContext(connection) as captured:
            second = self.fetch(**params, page=2)
        eligibility_queries = [entry for entry in captured if 'FROM "internal_chat_ordergroup"' in entry["sql"]]
        self.assertEqual(len(eligibility_queries), 1)
        self.assertEqual([row["order_no"] for row in second["items"]], [self.parent.order_no])
        self.assertFalse(second["items"][0]["chat_follow_up_available"])
        relations = second["page_relations"]
        split_rows = relations["split_members_by_id"][self.parent.split_id]
        child_rows = [row for rows in relations["penalty_children_by_parent"].values() for row in rows]
        self.assertCountEqual([
            row["order_no"] for row in [*split_rows, *child_rows] if row["chat_follow_up_available"]
        ], [member.order_no, direct.order_no])
        legacy_members = self.fetch(group_penalty_children=True, split_id=self.parent.split_id)["items"]
        self.assertEqual(legacy_members[0]["created_by_name"], member_creator.first_name)
        self.assertEqual(relations["split_members_by_id"], {self.parent.split_id: legacy_members})
        expected_children = {
            parent.order_no: self.fetch(penalty_parent_order_no=parent.order_no)["items"]
            for parent in (self.parent, member, direct)
        }
        self.assertEqual(relations["penalty_children_by_parent"], expected_children)
        self.assertEqual([row["order_no"] for row in expected_children[direct.order_no]], [leaf.order_no])
        self.assertEqual(expected_children[direct.order_no][0]["created_by_name"], leaf_creator.first_name)
        self.assertNotIn("page_relations", self.fetch(group_penalty_children=True))

    def test_large_page_relations_leave_primary_page_available_with_legacy_expansion(self):
        leaf = create_penalty_child(self.child, "20261116000000003003")
        params = {"group_penalty_children": True}
        primary = self.fetch(**params)
        path = "apps.business.api.order_page_relations"
        for limit in ("MAX_RELATED_ORDERS", "MAX_PENALTY_LEVELS"):
            with self.subTest(limit=limit), patch(f"{path}.{limit}", 1):
                self.assertEqual(self.fetch(**params, include_page_relations=True), primary)
        expanded = self.fetch(penalty_parent_order_no=self.child.order_no)["items"]
        self.assertEqual([row["order_no"] for row in expanded], [leaf.order_no])

    def test_page_relations_order_reads_depend_on_depth_not_the_number_of_groups(self):
        self.parent.split_id = "20261116000000003100"
        self.parent.save(update_fields=["split_id"])
        Order.objects.create(game=self.game, order_no="20261116000000003003", split_id=self.parent.split_id)
        params = {"group_penalty_children": True, "include_page_relations": True}

        def read_order_queries(expected_groups):
            with CaptureQueriesContext(connection) as captured:
                relations = self.fetch(**params)["page_relations"]
            self.assertEqual(len(relations["split_members_by_id"]), expected_groups)
            self.assertEqual(len(relations["penalty_children_by_parent"]), expected_groups)
            return sum(
                entry["sql"].lstrip().upper().startswith("SELECT")
                and 'FROM "business_order"' in entry["sql"]
                for entry in captured
            )

        one_group = read_order_queries(1)
        for i in range(3):
            split_id = str(20261116000000003101 + i)
            root, member, child = (str(20261116000000003010 + 3 * i + j) for j in range(3))
            parent = Order.objects.create(game=self.game, order_no=root, split_id=split_id)
            Order.objects.create(game=self.game, order_no=member, split_id=split_id)
            create_penalty_child(parent, child)
        self.assertEqual(read_order_queries(4), one_group)

    def test_page_relations_preserve_filters_and_each_permission(self):
        actor = "page-relations-viewer"
        other_game = Game.objects.create(name="不可见合成游戏")
        split_id = "20261116000000003100"
        self.parent.created_by = actor
        self.parent.split_id = split_id
        self.parent.save(update_fields=["created_by", "split_id"])
        member = Order.objects.create(
            order_no="20261116000000003003", game=self.game, created_by=actor, split_id=split_id,
        )
        self.child.created_by = actor
        self.child.save(update_fields=["created_by"])
        outside_game = create_penalty_child(
            self.parent, "20261116000000003004", game=other_game, created_by=actor, split_id=split_id,
        )
        outside_actor = create_penalty_child(self.parent, "20261116000000003005")
        viewer = make_logged_in_client(
            username=actor, groups=("关联订单",), games=[self.game],
            nodes=("order.list", "order.restrict_to_self_created", "order.restrict_view_total_stats",
                   "order.restrict_view_taker_price", "order.restrict_view_offer_price"),
        )
        opts = {"client": viewer, "group_penalty_children": True, "include_page_relations": True,
                "keyword": self.parent.order_no}
        for allow_unmatched, expected_members in ((False, [self.parent]), (True, [member, self.parent])):
            with self.subTest(allow_unmatched=allow_unmatched):
                data = self.fetch(**opts, include_unmatched_split_members=allow_unmatched)
                self.assertEqual(data["total"], 1)
                self.assertIsNone(data["summary"])
                relations = data["page_relations"]
                split_rows = relations["split_members_by_id"][split_id]
                self.assertEqual([row["order_no"] for row in split_rows], [row.order_no for row in expected_members])
                child_rows = relations["penalty_children_by_parent"][self.parent.order_no]
                self.assertEqual([row["order_no"] for row in child_rows], [self.child.order_no])
                self.assertEqual(child_rows, self.fetch(client=viewer, penalty_parent_order_no=self.parent.order_no)["items"])
                self.assertIsNone(child_rows[0]["hall_price"])
                self.assertNotIn(outside_game.order_no, str(data))
                self.assertNotIn(outside_actor.order_no, str(data))

    def test_original_number_search_keeps_expandable_child_and_child_search_keeps_own_row(self):
        for keyword in (self.parent.order_no, self.parent.source_order_no):
            params = {"group_penalty_children": True, "keyword": keyword}
            row, = self.fetch(**params)["items"]
            self.assertEqual(row["order_no"], self.parent.order_no)
            self.assertEqual(row["penalty_child_count"], 1)
            child, = self.fetch(**params, penalty_parent_order_no=self.parent.order_no)["items"]
            self.assertEqual(child["order_no"], self.child.order_no)
            self.assertTrue(child["is_penalty_child"])

        for keyword in (self.child.order_no, self.child.source_order_no):
            row, = self.fetch(group_penalty_children=True, keyword=keyword)["items"]
            self.assertEqual(row["order_no"], self.child.order_no)
            self.assertTrue(row["is_penalty_child"])
            self.assertEqual(row["penalty_parent_order_no"], self.parent.order_no)

        row, = self.fetch(group_penalty_children=True, status=OrderStatus.COMPLETED)["items"]
        self.assertEqual(row["order_no"], self.child.order_no)
        self.assertEqual(row["penalty_parent_order_no"], self.parent.order_no)

    def test_expansion_rejects_malformed_parent_order_number(self):
        body = self.admin.get(URL, {"penalty_parent_order_no": f"invalid-{self.parent.order_no}"}).json()
        self.assertEqual(body["code"], 1000, body)

    def test_nested_penalties_are_reachable_from_one_top_level_row(self):
        users = get_user_model().objects
        admin = users.get(username="penalty-list-admin")
        StaffProfile.objects.filter(user=admin).update(commission_mode=CommissionMode.PROFIT_PERCENT)
        ApprovalSettings.objects.create().approvers.add(admin)
        target = users.create_user(username="penalty-list-target")
        StaffProfile.objects.create(user=target, member_type=MemberType.PARTNER).games.add(self.game)
        approvals = "/api/business/approvals/"
        submitted = self.admin.post(approvals, {
            "original_order_no": self.child.order_no,
            "target_id": target.pk,
            "approver_id": admin.pk,
            "amount": "12",
            "reason": "再次罚款",
            "order": {"project_text": "合成罚款项目"},
        }, content_type="application/json").json()
        self.assertEqual(submitted["code"], 0, submitted)
        decision = self.admin.post(
            f"{approvals}{submitted['data']['id']}/decision/", {"decision": "approved"},
            content_type="application/json",
        ).json()
        self.assertEqual(decision["code"], 0, decision)
        grandchild_no = decision["data"]["generated_order_no"]
        original, = self.fetch(group_penalty_children=True, page_size=1)["items"]
        self.assertEqual(original["order_no"], self.parent.order_no)
        child, = self.fetch(penalty_parent_order_no=self.parent.order_no)["items"]
        self.assertEqual(child["order_no"], self.child.order_no)
        self.assertEqual(child["penalty_child_count"], 1)
        leaf, = self.fetch(penalty_parent_order_no=self.child.order_no)["items"]
        self.assertEqual(leaf["order_no"], grandchild_no)
        self.assertEqual(leaf["penalty_child_count"], 0)
        batch = {"batch_lookup_nos": f"{self.parent.order_no},{grandchild_no}"}
        rows = self.fetch(group_penalty_children=True, **batch)["items"]
        self.assertEqual({r["order_no"] for r in rows}, {self.parent.order_no, grandchild_no})
        leaf, = self.fetch(penalty_parent_order_no=self.child.order_no, **batch)["items"]
        self.assertEqual(leaf["order_no"], grandchild_no)

    def test_penalty_order_stays_in_its_split_group_and_in_original_expansion(self):
        self.child.split_id = "20261116000000003099"
        self.child.save(update_fields=["split_id"])
        Order.objects.create(
            game=self.game, order_no="20261116000000003003", split_id=self.child.split_id,
        )
        rows = self.fetch(group_penalty_children=True)["items"]
        self.assertEqual({r["order_no"] for r in rows}, {self.parent.order_no, self.child.order_no})
        child = next(r for r in rows if r["order_no"] == self.child.order_no)
        self.assertTrue(child["is_penalty_child"])
        self.assertEqual(child["split_members"], 2)
        parent = next(r for r in rows if r["order_no"] == self.parent.order_no)
        self.assertEqual(parent["penalty_child_count"], 1)
        preview, = self.fetch(penalty_parent_order_no=self.parent.order_no)["items"]
        self.assertEqual(preview["order_no"], self.child.order_no)
        self.assertEqual(preview["split_id"], self.child.split_id)
        search, = self.fetch(group_penalty_children=True, keyword=self.parent.order_no)["items"]
        self.assertEqual(search["penalty_child_count"], 1)

    def test_original_as_non_anchor_split_member_keeps_reachable_penalties(self):
        split_id = "20261116000000003099"
        # The normal group's representative is its oldest member.
        anchor = self.parent
        anchor.split_id = split_id
        anchor.save(update_fields=["split_id"])
        original = Order.objects.create(game=self.game, order_no="20261116000000003003", split_id=split_id)
        self.child.penalty_approval.original_order = original
        self.child.penalty_approval.save(update_fields=["original_order"])

        group = {"group_penalty_children": True}
        root = self.fetch(**group, page_size=1)
        self.assertEqual(root["total"], 1)
        representative, = root["items"]
        self.assertEqual(representative["order_no"], anchor.order_no)
        self.assertEqual(representative["penalty_child_count"], 0)
        split = {"split_id": split_id, "include_unmatched_split_members": True}
        members = {row["order_no"]: row for row in self.fetch(**group, **split)["items"]}
        self.assertEqual(members[original.order_no]["penalty_child_count"], 1)
        child, = self.fetch(**group, penalty_parent_order_no=original.order_no)["items"]
        self.assertEqual(child["order_no"], self.child.order_no)

        # Searching for both the group anchor and the penalty excludes the original from exact matches.
        keyword = {"batch_lookup_nos": f"{anchor.order_no},{self.child.order_no}"}
        rows = self.fetch(**group, **keyword)["items"]
        self.assertEqual({row["order_no"] for row in rows}, {anchor.order_no, self.child.order_no})
        members = {row["order_no"]: row for row in self.fetch(**group, **split, **keyword)["items"]}
        self.assertEqual(members[original.order_no]["penalty_child_count"], 0)

    def test_expansion_honors_status_and_data_permissions(self):
        other_game = Game.objects.create(name="另一游戏")
        foreign_parent = Order.objects.create(game=other_game, order_no="20261116000000003004")
        visible_child = create_penalty_child(foreign_parent, "20261116000000003005", game=self.game)
        create_penalty_child(self.parent, "20261116000000003006", game=other_game)
        viewer = make_logged_in_client(
            username="penalty-list-viewer", groups=("罚款列表",), nodes=("order.list",), games=[self.game],
        )
        params = {"group_penalty_children": True, "client": viewer}
        rows = self.fetch(**params)["items"]
        self.assertEqual({r["order_no"] for r in rows}, {self.parent.order_no, visible_child.order_no})
        parent = next(r for r in rows if r["order_no"] == self.parent.order_no)
        self.assertEqual(parent["penalty_child_count"], 1)
        standalone = next(r for r in rows if r["order_no"] == visible_child.order_no)
        self.assertTrue(standalone["is_penalty_child"])
        self.assertIsNone(standalone["penalty_parent_order_no"])
        self.assertEqual(self.fetch(**params, penalty_parent_order_no=foreign_parent.order_no)["items"], [])
        child, = self.fetch(**params, penalty_parent_order_no=self.parent.order_no)["items"]
        self.assertEqual(child["order_no"], self.child.order_no)
        self.assertEqual(
            self.fetch(**params, penalty_parent_order_no=self.parent.order_no, status=OrderStatus.UNASSIGNED)["items"],
            [],
        )

    def test_creator_restriction_applies_to_child_count_and_expansion(self):
        username = "penalty-list-created"
        self.parent.created_by = username
        self.parent.save(update_fields=["created_by"])
        viewer = make_logged_in_client(
            username=username, groups=("罚款本人",),
            nodes=("order.list", "order.restrict_to_self_created"), games=[self.game],
        )
        row, = self.fetch(client=viewer, group_penalty_children=True)["items"]
        self.assertEqual(row["order_no"], self.parent.order_no)
        self.assertEqual(row["penalty_child_count"], 0)
        self.assertEqual(self.fetch(client=viewer, penalty_parent_order_no=self.parent.order_no)["items"], [])

        self.parent.created_by = "其他客服"
        self.parent.save(update_fields=["created_by"])
        self.child.created_by = username
        self.child.save(update_fields=["created_by"])
        row, = self.fetch(client=viewer, group_penalty_children=True)["items"]
        self.assertEqual(row["order_no"], self.child.order_no)
        self.assertIsNone(row["penalty_parent_order_no"])


class PenaltyOrderListTenantIsolationTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other, _ = provision_tenant(
            schema_name="penalty_list_other", name="另一罚款列表租户", domain="penalty-list-other.test.local",
        )

    def test_taker_page_only_loads_detail_rows_from_current_tenant(self):
        def taken_order(order_no, order_game):
            order = Order.objects.create(order_no=order_no, game=order_game)
            taker = get_user_model().objects.create(username="tenant-page-taker")
            Performance.objects.create(order=order, user=taker, emp_type=EmpType.MAIN)
            return taker

        a_no, b_no = "20261116000000003007", "20261116000000003008"
        a_taker = taken_order(a_no, Game.objects.create(name="甲游戏"))
        a_client = make_logged_in_client(username="tenant-page-viewer", is_superuser=True)
        with tenant_context(self.other):
            b_game = Game.objects.create(name="乙游戏")
            b_taker = taken_order(b_no, b_game)
            b_client = make_logged_in_client(
                "penalty-list-other.test.local", username="tenant-page-viewer", is_superuser=True,
            )
            b_response = b_client.get(URL, {"taker_user": b_taker.pk}).json()
            self.assertEqual(b_response["code"], 0, b_response)
            self.assertEqual([row["order_no"] for row in b_response["data"]["items"]], [b_no])

        params = {"taker_user": a_taker.pk, "group_penalty_children": True}
        a_response = a_client.get(URL, params).json()
        self.assertEqual(a_response["code"], 0, a_response)
        self.assertEqual([row["order_no"] for row in a_response["data"]["items"]], [a_no])
        foreign = a_client.get(URL, {**params, "batch_lookup_nos": b_no}).json()
        self.assertEqual(foreign["code"], 0, foreign)
        self.assertEqual(foreign["data"]["items"], [])

    def test_cannot_expand_order_in_another_tenant(self):
        game = Game.objects.create(name="甲游戏")
        parent = Order.objects.create(game=game, order_no="20261116000000003001")
        child = create_penalty_child(parent, "20261116000000003002")
        viewer = make_logged_in_client(username="penalty-list-owner", is_superuser=True)
        params = {"penalty_parent_order_no": parent.order_no}
        rows = viewer.get(URL, params).json()["data"]["items"]
        self.assertEqual([r["order_no"] for r in rows], [child.order_no])
        grouped = {"group_penalty_children": True, "include_page_relations": True}
        self.assertEqual(
            viewer.get(URL, grouped).json()["data"]["page_relations"]["penalty_children_by_parent"],
            {parent.order_no: rows},
        )
        with tenant_context(self.other):
            viewer = make_logged_in_client(
                "penalty-list-other.test.local", username="penalty-list-owner", is_superuser=True,
            )
            body = viewer.get(URL, params).json()
            self.assertEqual(body["code"], 0, body)
            self.assertEqual(body["data"]["items"], [])
            isolated = viewer.get(URL, grouped).json()["data"]["page_relations"]
            self.assertEqual(isolated, {"split_members_by_id": {}, "penalty_children_by_parent": {}})
