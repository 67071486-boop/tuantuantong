from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event

from django.contrib.auth.models import User
from django.db import OperationalError, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType, Game, GameServer, Order, OrderSource, OrderStatus, Performance, ReviewStatus, SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


DICTIONARIES = "/api/business/orders/dictionary-options/"


class FinanceDictionaryOptionsTests(TenantTestCase):
    def test_order_list_actor_receives_only_sorted_lookup_fields(self):
        game = Game.objects.create(name="字典游戏", sort_order=10)
        Game.objects.create(name="隐藏游戏", deleted_at="2026-01-01T00:00:00Z")
        server = GameServer.objects.create(name="字典区服", game=game)
        source = OrderSource.objects.create(name="字典来源")
        actor = make_logged_in_client(is_superuser=True)
        response = actor.get(DICTIONARIES)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {
            "games": [{"id": game.pk, "name": "字典游戏"}],
            "servers": [{"id": server.pk, "name": "字典区服", "game": game.pk}],
            "order_sources": [{"id": source.pk, "name": "字典来源"}],
        })
        forbidden = make_logged_in_client(username="dictionary-denied")
        self.assertEqual(forbidden.get(DICTIONARIES).status_code, 403)
        self.assertEqual(Client().get(DICTIONARIES, SERVER_NAME="tenant.test.com").status_code, 401)


DAILY = "/api/business/settlements/daily-summary"
SOURCES = DAILY + "/sources"
MONTH = {"date_from": "2026-01-01", "date_to": "2026-01-31"}
DAY = {"date": "2026-01-10"}
DOMAINS = ("finance-catalog-a.test.local", "finance-catalog-b.test.local")


@override_settings(ALLOWED_HOSTS=DOMAINS)
class FinanceCommittedReadCacheTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.tenants = []
        for index, domain in enumerate(DOMAINS):
            tenant, _ = provision_tenant(schema_name=f"finance_catalog_{index}", name=domain, domain=domain)
            self.tenants.append(tenant)

    def tearDown(self):
        connection.set_schema_to_public()
        for tenant in self.tenants:
            tenant.delete(force_drop=True)
        super().tearDown()

    def seed(self, index, visible, amount):
        actor = make_logged_in_client(
            domain=DOMAINS[index], username="same-finance-user", groups=("缓存读取",), nodes=("finance.payroll", "order.list"),
            games=[visible],
        )
        payment = self.create_settled_payment(visible, amount, suffix=1)
        return actor, payment

    def create_settled_payment(self, game, amount, suffix):
        source = OrderSource.objects.create(name=f"来源{suffix}", order_by=suffix)
        order = Order.objects.create(
            game=game,
            order_source=source,
            order_no=f"20260110000000000{suffix:03d}",
            status=OrderStatus.COMPLETED,
        )
        return Performance.objects.create(
            order=order,
            user=User.objects.create_user(username=f"打手{suffix}"),
            emp_type=EmpType.MAIN,
            temp_amount=Decimal(amount),
            bonus_amount=Decimal(amount),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.SETTLED,
            settled_at=datetime(2026, 1, 10, 2, tzinfo=UTC),
            settled_amount=Decimal(amount),
        )

    def body(self, actor, route, params=None):
        response = actor.get(route, params or {})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        return payload["data"]

    def test_live_revision_is_authoritative_for_cached_catalogs_and_history(self):
        self.assertTrue(connection.get_autocommit())
        first, second = self.tenants
        with tenant_context(first):
            visible = Game.objects.create(name="甲租户可见游戏", sort_order=5)
            private = Game.objects.create(name="甲租户其他权限游戏")
            GameServer.objects.create(game=visible, name="甲区服")
            a, ledger = self.seed(0, visible, "12.00")
            self.create_settled_payment(private, "99.00", suffix=2)
            other = make_logged_in_client(
                domain=DOMAINS[0], username="other-scope", groups=("另组财务",), nodes=("finance.payroll",), games=[private]
            )

            self.assertEqual(self.body(a, DICTIONARIES)["games"][0]["name"], "甲租户可见游戏")
            self.assertEqual(self.body(a, DAILY, MONTH)["items"][0]["total_amount"], "12.00")
            self.assertEqual(self.body(a, SOURCES, DAY)["items"][0]["source_name"], "来源1")
            self.assertEqual(self.body(other, DAILY, MONTH)["items"][0]["total_amount"], "99.00")

            Performance.objects.filter(pk=ledger.pk).update(settled_amount=Decimal("12.00"))
            Performance.objects.filter(pk=ledger.pk).update(temp_amount=Decimal("45.00"))
            with CaptureQueriesContext(connection) as hit:
                same_catalog = self.body(a, DICTIONARIES)
                month_rows = self.body(a, DAILY, MONTH)
                sources_rows = self.body(a, SOURCES, DAY)
            self.assertEqual(same_catalog["games"][0]["name"], "甲租户可见游戏")
            self.assertEqual(month_rows["items"][0]["total_amount"], "12.00")
            self.assertEqual(sources_rows["items"][0]["total_amount"], "12.00")
            self.assertFalse(
                [query["sql"] for query in hit if '"business_performance"' in query["sql"].lower()],
                "Warm closed reports must not scan or aggregate the ledger",
            )
            self.assertFalse(
                [query["sql"] for query in hit if '"business_gameserver"' in query["sql"].lower()],
                "Warm dictionaries must not reload servers",
            )

            with transaction.atomic():
                Performance.objects.filter(pk=ledger.pk).update(settled_amount=Decimal("15.00"))
                OrderSource.objects.filter(pk=ledger.order.order_source_id).update(name="整改来源")
                Game.objects.filter(pk=visible.pk).update(name="甲更名")
            self.assertEqual(self.body(a, DICTIONARIES)["games"][0]["name"], "甲更名")
            self.assertEqual(self.body(a, DAILY, MONTH)["items"][0]["total_amount"], "15.00")
            sources = self.body(a, SOURCES, DAY)["items"]
            self.assertEqual([(row["source_name"], row["total_amount"]) for row in sources], [
                ("整改来源", "15.00"),
            ])

            lookups = User.objects.get(username="same-finance-user")
            lookups.staff_profile.games.set([private])
            self.assertEqual(self.body(a, DAILY, MONTH)["items"][0]["total_amount"], "99.00")
            lookups.staff_profile.games.set([visible])

            Order.all_objects.filter(pk=ledger.order_id).update(deleted_at=datetime(2026, 1, 11, tzinfo=UTC))
            self.assertEqual(self.body(a, DAILY, MONTH)["items"], [])
            Order.all_objects.filter(pk=ledger.order_id).update(deleted_at=None)
            self.assertEqual(self.body(a, DAILY, MONTH)["items"][0]["total_amount"], "15.00")
            Performance.objects.filter(pk=ledger.pk).update(settle_status=SettleStatus.UNSETTLED)
            self.assertEqual(self.body(a, DAILY, MONTH)["items"], [])
            Performance.objects.filter(pk=ledger.pk).update(settle_status=SettleStatus.SETTLED)
            self.assertEqual(self.body(a, DAILY, MONTH)["items"][0]["total_amount"], "15.00")

        with tenant_context(second):
            visible_b = Game.objects.create(name="乙租户游戏")
            b, _ = self.seed(1, visible_b, "27.00")
            self.assertEqual(self.body(b, DICTIONARIES)["games"][0]["name"], "乙租户游戏")
            self.assertEqual(self.body(b, DAILY, MONTH)["items"][0]["total_amount"], "27.00")
            self.assertEqual(self.body(b, SOURCES, DAY)["items"][0]["total_amount"], "27.00")
            self.assertIn(a.get(DAILY, MONTH, SERVER_NAME=DOMAINS[1]).status_code, (401, 403))

    def test_order_change_and_late_payment_cannot_publish_a_stale_sources_cache(self):
        tenant = self.tenants[0]
        with tenant_context(tenant):
            game = Game.objects.create(name="并发归属游戏")
            actor = make_logged_in_client(
                domain=DOMAINS[0], username="finance-race-user",
                groups=("并发汇总",), nodes=("finance.payroll",), games=[game],
            )
            ledger = self.create_settled_payment(game, "12.00", suffix=3)
            Performance.objects.filter(pk=ledger.pk).update(settle_status=SettleStatus.UNSETTLED)
            new_source = OrderSource.objects.create(name="并发更正来源")
            changed = Event()
            publish = Event()

            def move_order():
                try:
                    with tenant_context(tenant), transaction.atomic():
                        Order.objects.filter(pk=ledger.order_id).update(order_source=new_source)
                        with connection.cursor() as cursor:
                            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                        changed.set()
                        if not publish.wait(15):
                            raise TimeoutError("test order transaction was not released")
                finally:
                    connection.close()

            def settle():
                Performance.objects.filter(pk=ledger.pk).update(settle_status=SettleStatus.SETTLED)

            settled_before_order = False
            with ThreadPoolExecutor(max_workers=1) as pool:
                mover = pool.submit(move_order)
                try:
                    self.assertTrue(changed.wait(10))
                    try:
                        with transaction.atomic():
                            with connection.cursor() as cursor:
                                cursor.execute("SET LOCAL lock_timeout = '500ms'")
                            settle()
                        settled_before_order = True
                    except OperationalError as exc:
                        self.assertEqual(getattr(exc.__cause__, "sqlstate", None), "55P03")
                    if settled_before_order:
                        before = self.body(actor, SOURCES, DAY)["items"]
                        self.assertEqual(before[0]["source_name"], "来源3")
                finally:
                    publish.set()
                    mover.result(timeout=10)

            if not settled_before_order:
                settle()
            after = self.body(actor, SOURCES, DAY)["items"]
            self.assertEqual(after[0]["source_name"], "并发更正来源")
