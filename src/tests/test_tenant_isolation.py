from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business import jidanpu_member, jidanpu_member_openapi, jidanpu_relaunch
from apps.business.jidanpu_member import _old_assignment_clear_cutoff
from apps.business.models import (
    EmpType, Game, HallExitSnapshot, JidanpuMemberBinding, JidanpuMemberOrderLink, JidanpuSyncEvent,
    JidanpuUpstreamBinding, JidanpuUpstreamHallOrder, JidanpuUpstreamOrderLink, Order, OrderStatus, Performance, SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client


class TenantIsolationTests(TenantTestCase):
    """租户 A 写入的数据，租户 B 必须完全不可见。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="tenant_a", name="租户 A", domain="a.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="tenant_b", name="租户 B", domain="b.test.local"
        )

    def tearDown(self):
        # 说明：TenantTestCase 继承 Django 的 TestCase，测试体与这里的 DELETE 都在
        # 同一个事务里、结束时统一回滚，因此本方法在当前基类下实际是惰性的。
        # 保留它只是为了测试类若换成非事务基类时仍不串数据。
        with tenant_context(self.tenant_a):
            Order.objects.all().delete()
        with tenant_context(self.tenant_b):
            Order.objects.all().delete()
        super().tearDown()

    def test_rows_written_in_one_tenant_are_invisible_in_another(self):
        with tenant_context(self.tenant_a):
            game_a = Game.objects.create(name="仅 A 的游戏")
            Order.objects.create(
                game=game_a, order_no="20260920000000000001", project_text="仅 A 可见"
            )

        # 断言必须**针对本条数据**，不能用 Order.objects.count()：
        # count() 的通过与否取决于两个租户里各有多少行，而不是「A 的行在 B 是否可见」。
        # 这里在 B 里先造一行并断言它**可见**，红线就不再依赖 provision_tenant 灌的
        # 基线数据来保证 B 非空，单独运行也成立。
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="仅 B 的游戏")
            Order.objects.create(
                game=game_b, order_no="20260920000000000002", project_text="仅 B 可见"
            )
            self.assertTrue(Order.objects.filter(project_text="仅 B 可见").exists())
            self.assertFalse(Order.objects.filter(project_text="仅 A 可见").exists())

        with tenant_context(self.tenant_a):
            self.assertTrue(Order.objects.filter(project_text="仅 A 可见").exists())

    def test_hall_page_details_cannot_be_read_with_another_tenants_credentials(self):
        endpoint = "/api/business/hall/"

        def create_card(tenant, suffix, number):
            with tenant_context(tenant):
                game = Game.objects.create(name=f"大厅隔离游戏 {suffix}")
                order = Order.objects.create(
                    game=game, order_no=number, status=OrderStatus.WAITING_GRAB,
                )
                Performance.objects.create(order=order, emp_type=EmpType.MAIN)
                client = make_logged_in_client(
                    domain=f"{suffix}.test.local", username=f"hall-isolation-{suffix}",
                    groups=("打手",), nodes=("grab_hall.take_order",), games=[game],
                )
                payload = client.get(endpoint, {"page_size": 1}).json()
                self.assertEqual(payload["code"], 0, payload)
                self.assertEqual([row["order_no"] for row in payload["data"]["items"]], [number])
                return client

        with transaction.atomic():
            own_no = "20261121000000000101"
            foreign_no = "20261121000000000102"
            client_a = create_card(self.tenant_a, "a", own_no)
            create_card(self.tenant_b, "b", foreign_no)

            with tenant_context(self.tenant_a):
                body = client_a.get(endpoint, {"keyword": foreign_no, "page_size": 1}).json()
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(body["data"]["total"], 0)
                self.assertEqual(body["data"]["items"], [])
            with tenant_context(self.tenant_b):
                response = client_a.get(endpoint, HTTP_HOST="b.test.local")
                self.assertEqual(response.status_code, 401)
            transaction.set_rollback(True)

    def test_same_name_can_exist_in_two_tenants_without_conflict(self):
        """唯一约束的作用域是单个租户 schema，不是全库。"""
        same_no = "20260920000000000003"
        with tenant_context(self.tenant_a):
            game_a = Game.objects.create(name="同名游戏")
            Order.objects.create(
                game=game_a, order_no=same_no, project_text="同名项目"
            )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="同名游戏")
            Order.objects.create(
                game=game_b, order_no=same_no, project_text="同名项目"
            )
            rows = list(
                Order.objects.filter(order_no=same_no).values_list("project_text", flat=True)
            )
            self.assertEqual(rows, ["同名项目"])

    def test_split_number_can_be_used_independently_of_another_tenant(self):
        with tenant_context(self.tenant_b):
            game = Game.objects.create(name="租户 B 拆单游戏")
            foreign = Order.objects.create(
                game=game, order_no="20260920000000000006", project_text="仅 B 的子单",
                source_order_no="SHARED-SPLIT-1",
            )
            self.assertTrue(Order.objects.filter(order_no=foreign.order_no).exists())
        with tenant_context(self.tenant_a), transaction.atomic():
            game = Game.objects.create(name="租户 A 拆单游戏")
            client = make_logged_in_client(
                domain="a.test.local", username="split-a", groups=("客服",),
                nodes=("order.add_edit", "order.view_detail", "order.list"),
            )
            body = client.post(
                "/api/business/orders/",
                {"game": game.pk, "project_text": "A 原单/A 子单", "split": True,
                 "source_order_no": "SHARED-SPLIT"},
                content_type="application/json",
            ).json()
            self.assertEqual(body["code"], 0, body)
            child = Order.objects.get(split_id=body["data"]["split_id"], project_text="A 子单")
            self.assertEqual(child.source_order_no, "SHARED-SPLIT-1")
            foreign_response = client.get(f"/api/business/orders/{foreign.order_no}/").json()
            self.assertEqual(foreign_response["code"], 1002, foreign_response)
            transaction.set_rollback(True)

    def test_completed_split_payment_check_uses_only_its_tenants_orders(self):
        with transaction.atomic():
            with tenant_context(self.tenant_b):
                game = Game.objects.create(name="租户 B 完成拆单游戏")
                foreign = Order.objects.create(game=game, order_no="20260920000000000007",
                                               status=OrderStatus.COMPLETED, project_text="租户 B 已结账")
                Performance.objects.create(order=foreign, emp_type=EmpType.MAIN, settle_status=SettleStatus.SETTLED)
                self.assertTrue(Performance.objects.get(order=foreign).is_settled)
            with tenant_context(self.tenant_a):
                game = Game.objects.create(name="租户 A 完成拆单游戏")
                own = Order.objects.create(game=game, order_no="20260920000000000008",
                                           status=OrderStatus.COMPLETED, project_text="租户 A 未结账", total_price=20)
                client = make_logged_in_client(
                    domain="a.test.local", username="split-completed-a", groups=("客服",),
                    nodes=("order.add_edit", "order.view_detail"),
                )
                own_url = f"/api/business/orders/{own.order_no}/"
                foreign_url = f"/api/business/orders/{foreign.order_no}/"
                draft = {"remaining_project_text": "剩余", "new_orders": [{"project_text": "子单", "total_price": "5.00"}]}
                self.assertIs(client.get(own_url).json()["data"]["can_continue_split"], True)
                self.assertEqual(client.get(foreign_url).json()["code"], 1002)
                self.assertEqual(client.post(foreign_url + "split-peer/", draft, content_type="application/json").json()["code"], 1002)
                self.assertEqual(client.post(own_url + "split-peer/", draft, content_type="application/json").json()["code"], 0)
            transaction.set_rollback(True)

    def test_relaunch_clear_eligibility_does_not_use_another_tenants_audit(self):
        same_no = "20260920000000000004"
        link = SimpleNamespace(listed_at=None)
        fields = dict(log_type=1, action="order.clear_taker", target_kind="user")
        own_no = "20260920000000000005"

        with tenant_context(self.tenant_a):
            marker_a = OperationLog.objects.create(order_no=same_no, target_id="11001", **fields)
            event = SimpleNamespace(target_ref=same_no, created_at=timezone.now(), payload={})
            self.assertEqual(_old_assignment_clear_cutoff(event, link), marker_a.created_at)
        with tenant_context(self.tenant_b):
            marker_b = OperationLog.objects.create(order_no=own_no, target_id="22001", **fields)
            own = SimpleNamespace(target_ref=own_no, created_at=timezone.now(), payload={})
            self.assertEqual(_old_assignment_clear_cutoff(own, link), marker_b.created_at)
            self.assertIsNone(_old_assignment_clear_cutoff(event, link))

    def test_member_remote_copy_ownership_does_not_cross_tenants(self):
        store = "https://synthetic-shared-store.test.invalid"
        remote_id = "SYNTH-TENANT-INDEPENDENT-COPY"

        def local_order_and_binding(suffix, order_no):
            binding = JidanpuMemberBinding.objects.create(
                user=User.objects.create(username=f"member-store-{suffix}"), jidanpu_base_url=store,
            )
            game = Game.objects.create(name=f"合成下家隔离游戏 {suffix}")
            return Order.objects.create(game=game, order_no=order_no), binding

        with tenant_context(self.tenant_b):
            order_b, binding_b = local_order_and_binding("b", "20261120000000000021")
            JidanpuMemberOrderLink.objects.create(
                binding=binding_b, order=order_b, jidanpu_order_id=remote_id,
            )
        with tenant_context(self.tenant_a):
            order_a, binding_a = local_order_and_binding("a", "20261120000000000022")
            link, created = jidanpu_member._ensure_link(
                binding_a, order_a, remote_id=remote_id, in_hall=False, now=timezone.now(),
            )
            self.assertTrue(created)
            self.assertEqual(link.order_id, order_a.pk)
            self.assertFalse(JidanpuMemberOrderLink.objects.filter(order__order_no=order_b.order_no).exists())

    def test_member_callback_reads_only_its_tenants_store_and_withdrawal_history(self):
        same_no = "20261120000000000001"
        received_at = timezone.now()

        def seed_copy(suffix):
            game = Game.objects.create(name=f"隔离回调游戏 {suffix}")
            order = Order.objects.create(game=game, order_no=same_no)
            binding = JidanpuMemberBinding.objects.create(
                pk=200, user=User.objects.create(username=f"member-callback-{suffix}"),
                enabled=True, jidanpu_base_url=f"https://audit-store-{suffix}.test.invalid",
            )
            return order, binding

        with tenant_context(self.tenant_b):
            order, binding = seed_copy("b")
            JidanpuMemberOrderLink.all_objects.create(
                order=order, binding=binding, jidanpu_order_id="SYNTH-RETIRED-B",
                retired_at=received_at,
            )
            JidanpuSyncEvent.objects.create(
                direction="out", kind=jidanpu_relaunch.KIND_WITHDRAW, status="done",
                target_type="order", target_ref=same_no, processed_at=received_at,
                payload={"member_binding_id": binding.pk},
            )
            self.assertFalse(jidanpu_member_openapi._legacy_can_fill_id(binding, order))
            self.assertTrue(jidanpu_relaunch.confirmed_withdrawal_since(same_no, binding, received_at))

        with tenant_context(self.tenant_a):
            order, binding = seed_copy("a")
            JidanpuMemberOrderLink.objects.create(order=order, binding=binding, jidanpu_order_id="")
            self.assertTrue(jidanpu_member_openapi._legacy_can_fill_id(binding, order))
            self.assertFalse(jidanpu_relaunch.confirmed_withdrawal_since(same_no, binding, received_at))

    def test_member_legacy_callback_does_not_resolve_another_tenants_copy(self):
        remote_id = "SYNTH-LEGACY-B"
        source_order_no = "合成租户B来源单"

        def seed_binding(suffix):
            return JidanpuMemberBinding.objects.create(
                user=User.objects.create(username=f"legacy-callback-{suffix}"),
                enabled=True, jidanpu_base_url=f"https://legacy-store-{suffix}.test.invalid",
            )

        with tenant_context(self.tenant_b):
            binding = seed_binding("b")
            game = Game.objects.create(name="合成租户B游戏")
            order = Order.objects.create(
                game=game, order_no="20261120000000000002", source_order_no=source_order_no,
            )
            JidanpuMemberOrderLink.objects.create(
                order=order, binding=binding, jidanpu_order_id=remote_id,
            )
            link, _, _, _ = jidanpu_member_openapi._link_of(
                binding, remote_id, source_order_no, legacy=True,
            )
            self.assertEqual(link.order.source_order_no, source_order_no)

        with tenant_context(self.tenant_a):
            binding = seed_binding("a")
            self.assertEqual(
                jidanpu_member_openapi._link_of(binding, remote_id, source_order_no, legacy=True),
                (None, None, None, ""),
            )

    def test_upstream_binding_cleanup_cannot_reach_another_tenant(self):
        upstream_url = "https://shared-upstream.test.invalid"
        password = "pw-tenant-isolation"

        def seed_hall(tenant, code):
            with tenant_context(tenant):
                game = Game.objects.create(name=f"隔离大厅 {code}")
                upstream = JidanpuUpstreamBinding.objects.create(
                    name=f"上家 {code}", jidanpu_base_url=upstream_url, account=f"up-{code}",
                    password=password, enabled=True, user=User.objects.create(username=f"up-{code}"),
                )
                downstream = JidanpuMemberBinding.objects.create(
                    jidanpu_base_url=f"https://down-{code}.test.invalid", account=f"down-{code}",
                    password=password, enabled=True, user=User.objects.create(username=f"down-{code}"),
                )
                for index in range(2):
                    order_id = f"card-{code}-{index}"
                    card = JidanpuUpstreamHallOrder.objects.create(
                        upstream_base_url=upstream_url, upstream_order_id=order_id,
                        owner_binding=upstream, game=game, status="listed", first_seen_at=timezone.now(),
                    )
                    shadow = Order.objects.create(
                        order_no=f"20261116{code * 10 + index:012d}", game=game,
                        status=OrderStatus.WAITING_GRAB, upstream_hall_card=card,
                    )
                    JidanpuMemberOrderLink.objects.create(
                        order=shadow, binding=downstream, jidanpu_order_id=f"remote-{order_id}", in_hall=True,
                    )
                tracked = Order.objects.create(
                    order_no=f"20261120{code:012d}", game=game, status=OrderStatus.IN_PROGRESS,
                )
                JidanpuUpstreamOrderLink.objects.create(
                    binding=upstream, order=tracked, upstream_order_id="synthetic-shared-remote",
                )
                return upstream

        binding_a = seed_hall(self.tenant_a, 1)
        binding_b = seed_hall(self.tenant_b, 2)
        with tenant_context(self.tenant_a):
            client = make_logged_in_client(
                domain="a.test.local", username="upstream-ops-a", groups=("绑定维护",),
                nodes=("system.bind_upstream_system",),
            )
            body = {"jidanpu_base_url": upstream_url, "account": "replacement-up-a", "enabled": False}
            own = client.put(
                f"/api/business/upstream-bindings/jidanpu/{binding_a.pk}/",
                body, content_type="application/json",
            ).json()
            self.assertEqual(own["code"], 0, own)
            self.assertFalse(JidanpuUpstreamBinding.objects.get(name="上家 1").enabled)
            self.assertEqual(JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id__startswith="card-1-", status="gone",
            ).count(), 2)
            self.assertEqual(JidanpuSyncEvent.objects.filter(kind="member.withdraw").count(), 2)
            self.assertIsNotNone(JidanpuUpstreamOrderLink.all_objects.get(
                order__order_no="20261120000000000001",
            ).retired_at)
            self.assertFalse(JidanpuUpstreamOrderLink.objects.filter(
                order__order_no="20261120000000000002",
            ).exists())
            body["account"] = "replacement-up-b"
            self.assertTrue(client.cookies.get("sessionid"))
            forbidden = client.put(
                f"/api/business/upstream-bindings/jidanpu/{binding_b.pk}/",
                body, content_type="application/json", HTTP_HOST="b.test.local",
            )
            self.assertEqual(forbidden.status_code, 401, forbidden.content)

        with tenant_context(self.tenant_b):
            untouched = JidanpuUpstreamBinding.objects.get(name="上家 2")
            self.assertTrue(untouched.enabled)
            self.assertEqual(untouched.account, "up-2")
            self.assertTrue(JidanpuUpstreamOrderLink.objects.filter(
                order__order_no="20261120000000000002", upstream_order_id="synthetic-shared-remote",
            ).exists())
            foreign_cards = JidanpuUpstreamHallOrder.objects.filter(upstream_order_id__startswith="card-2-")
            self.assertEqual(foreign_cards.filter(status="listed").count(), 2)
            self.assertEqual(Order.objects.filter(
                upstream_hall_card__in=foreign_cards, status=OrderStatus.WAITING_GRAB,
            ).count(), 2)
            self.assertFalse(HallExitSnapshot.objects.filter(mirror__in=foreign_cards).exists())
            self.assertFalse(JidanpuSyncEvent.objects.filter(kind="member.withdraw").exists())

    def test_order_table_has_no_tenant_id_column(self):
        """schema 是隔离边界，不允许用 tenant_id 伪装隔离。

        必须查**数据库的列**，不能查模型字段名：一个
        `ForeignKey(..., db_column="tenant_id")` 的**字段名**不是 `tenant_id`，
        但库里确实会多出 `tenant_id` 列——查 `_meta.get_fields()` 会漏掉它。
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'business_order'"
            )
            columns = {row[0] for row in cursor.fetchall()}

        # 先自证查询没空手而归，否则下面的否定断言是空的
        self.assertTrue(columns, "没取到 business_order 的列，检查 search_path")
        self.assertNotIn("tenant_id", columns)
        self.assertEqual(
            [c for c in columns if c.startswith("tenant")],
            [],
            f"租户表不应含租户判别列，实际列：{sorted(columns)}",
        )

    def test_public_schema_has_no_business_tables(self):
        """`public` 只放平台数据。业务表出现在 public 就是隔离破了。

        这条对应完成标准里的 psql 手检，改成自动化后才能在 CI 里守住。
        被禁集合**从 app 配置推导**，不硬编码表名——否则将来新增业务模型时，
        新表进了 public 这条测试依然全绿。
        只枚举一个 app 时，后加的租户专属 app 会静默落在兜底之外。
        现在四个都列上：`business` / `accounts` / `openapi` / `takerapi`。
        漏掉一个，它的表被挪进 `SHARED_APPS` 时这条兜底一声不响。
        """
        labels = ("business", "accounts", "openapi", "takerapi")
        tenant_only_tables = set()
        for label in labels:
            models_of_app = list(apps.get_app_config(label).get_models())
            self.assertTrue(models_of_app, f"{label} app 下没有任何模型？检查 app label")
            for model in models_of_app:
                tenant_only_tables.add(model._meta.db_table)
                # M2M 的**自动中间表**同属租户平面（同一个 app 的迁移里建出来）⇒ 一起收，
                # 否则「中间表被误建进 public」这条会静默（与主表那半一起）。
                for field in model._meta.local_many_to_many:
                    through = field.remote_field.through
                    if through._meta.auto_created:
                        tenant_only_tables.add(through._meta.db_table)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            public_tables = {row[0] for row in cursor.fetchall()}

        # 先自证查询有效（空集合会让下面的不相交断言恒真）
        self.assertIn("platform_tenant", public_tables)
        self.assertIn("platform_domain", public_tables)
        # 再自证**每个** label 都进了被禁集合：少枚举一个，这里就对不上
        # （不是"集合非空"那种恒真判据——`business` 一个 app 也非空）。
        for label in labels:
            prefix = f"{apps.get_app_config(label).label}_"
            self.assertTrue(
                any(table.startswith(prefix) for table in tenant_only_tables),
                f"被禁表清单里没有 {label} 的表：{sorted(tenant_only_tables)}",
            )
        self.assertEqual(
            public_tables & tenant_only_tables,
            set(),
            f"租户专属表不得出现在 public：{sorted(public_tables & tenant_only_tables)}",
        )
