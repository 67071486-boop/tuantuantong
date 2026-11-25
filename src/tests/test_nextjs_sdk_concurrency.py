from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TransactionTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model

from apps.business.models import EmpType, Game, Order, Performance
from apps.openapi.models import ApiApp
from apps.rbac.seed import seed_rbac


class NextjsSdkConcurrencyTests(TransactionTestCase):
    prefix = "/api/integration/v1/"
    host = "sdk-concurrency.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="sdk_concurrency")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model().objects.create(tenant=cls.tenant, domain=cls.host)
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def post(self, route, token, key, data, *, method="post"):
        client = Client(HTTP_HOST=self.host, REMOTE_ADDR="203.0.113.74")
        return getattr(client, method)(
            self.prefix + route, data=data, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_IDEMPOTENCY_KEY=key,
        )

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_concurrent_operator_assign_and_personal_claim_leave_one_taker(self):
        seed_rbac()
        owner = get_user_model().objects.create_user(username="sdk-race-owner")
        app = ApiApp.objects.create(
            name="SDK 派抢并发验收", kind="business_integration", state="sdk_enabled",
            app_key="sdk-race-synthetic-key", app_secret="sdk-race-synthetic-secret", created_by=owner,
        )
        token_response = Client(HTTP_HOST=self.host, REMOTE_ADDR="203.0.113.74").post(
            "/api/open/oauth/token", {
                "grant_type": "client_credentials", "client_id": app.app_key, "client_secret": app.app_secret,
            },
        )
        self.assertEqual(token_response.status_code, 200, token_response.content)
        app_token = token_response.json()["access_token"]
        game = Game.objects.create(name="SDK 并发验证游戏")
        claim_taker, assign_taker = [
            self.ok(self.post("takers/", app_token, f"new-race-taker-{tag}", {
                "username": f"sdk-race-{tag}", "display_name": f"并发打手 {tag}", "games": [game.pk],
            }))
            for tag in ("grab", "assign")
        ]
        self.ok(self.post("taker-bindings/sdk-race-grab/", app_token, "bind-race-taker", {
            "taker_id": claim_taker["id"],
        }, method="put"))
        claim_token = self.ok(self.post("taker-token/", app_token, "request-race-token", {
            "external_user_id": "sdk-race-grab",
        }))["access_token"]
        order_no = self.ok(self.post("orders/", app_token, "new-race-order", {
            "game": game.pk, "project_text": "SDK 并发订单", "source_order_no": "sdk-race-order",
            "total_price": "100.00", "hall_price": "88.00",
        }))["order_no"]
        self.ok(self.post(f"orders/{order_no}/publish/", app_token, "publish-race-order", {}))

        barrier = Barrier(2)

        def compete(route, token, key, body):
            aligned = False

            def at_first_order_lock(execute, sql, params, many, context):
                nonlocal aligned
                if not aligned and "FOR UPDATE" in sql and '"business_order"' in sql:
                    aligned = True
                    barrier.wait(timeout=15)
                return execute(sql, params, many, context)

            try:
                with connection.execute_wrapper(at_first_order_lock):
                    response = self.post(route, token, key, body)
                return response.status_code, response.json()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claim = pool.submit(compete, f"taker/hall/{order_no}/claim/", claim_token, "race-claim-order", {})
            assign = pool.submit(compete, f"orders/{order_no}/assign/", app_token, "race-assign-order", {
                "taker_id": assign_taker["id"],
            })
            results = [claim.result(timeout=25), assign.result(timeout=25)]

        self.assertEqual([status for status, _ in results], [200, 200], results)
        winners = [index for index, (_, body) in enumerate(results) if body["code"] == 0]
        self.assertEqual(len(winners), 1, results)
        order = Order.objects.get(order_no=order_no)
        taker_ids = (claim_taker["id"], assign_taker["id"])
        self.assertEqual(order.status, "IN_PROGRESS")
        self.assertEqual(list(Performance.objects.filter(
            order=order, emp_type=EmpType.MAIN, deleted_at__isnull=True,
        ).values_list("user_id", flat=True)), [taker_ids[winners[0]]])
