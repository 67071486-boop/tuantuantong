from django.apps import apps
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import AuditLog, Game, Order, Project, ProjectCategory
from apps.platform.provisioning import provision_tenant


class P2IsolationTests(TenantTestCase):
    """P2 新增的模型同样必须租户隔离。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="p2_a", name="租户 A", domain="p2a.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="p2_b", name="租户 B", domain="p2b.test.local"
        )

    def test_order_written_in_one_tenant_is_invisible_in_another(self):
        with tenant_context(self.tenant_a):
            game = Game.objects.create(name="仅 A 的游戏")
            Order.objects.create(
                game=game, order_no="20260920000000000001", project_text="仅 A 可见"
            )

        with tenant_context(self.tenant_b):
            # 先证明 B 的表可达且有数据（非空断言，避免空洞通过）
            game_b = Game.objects.create(name="仅 B 的游戏")
            Order.objects.create(
                game=game_b, order_no="20260920000000000002", project_text="仅 B 可见"
            )
            self.assertTrue(Order.objects.filter(project_text="仅 B 可见").exists())
            # 再证明看不到 A 的
            self.assertFalse(Order.objects.filter(project_text="仅 A 可见").exists())

        with tenant_context(self.tenant_a):
            self.assertTrue(Order.objects.filter(project_text="仅 A 可见").exists())

    def test_order_no_can_repeat_across_tenants(self):
        """订单号只有租户内唯一——两个租户可以有同一个号（spec §3.2.2）。"""
        same_no = "20260920000000000009"
        with tenant_context(self.tenant_a):
            game = Game.objects.create(name="游戏")
            Order.objects.create(game=game, order_no=same_no, project_text="A 的单")
        with tenant_context(self.tenant_b):
            game = Game.objects.create(name="游戏")
            Order.objects.create(game=game, order_no=same_no, project_text="B 的单")
            # 按**具体行**断言，不用 `count()`——Global Constraints 明确禁止用 count() 做否定断言，
            # 而且它在唯一约束被放宽后会**静默失去判别力**（数字仍是 1，隔离却已经破了）。
            rows = list(
                Order.objects.filter(order_no=same_no).values_list("project_text", flat=True)
            )
            self.assertEqual(rows, ["B 的单"])

    def test_game_config_is_isolated_too(self):
        with tenant_context(self.tenant_a):
            game = Game.objects.create(name="原神")
            cat = ProjectCategory.objects.create(game=game, name="深境螺旋")
            Project.objects.create(category=cat, name="满星-A")
        with tenant_context(self.tenant_b):
            # **阳性对照**：先证明 B 自己能看见自己的配置。
            # 否则「查不到 A 的」也可能只是因为 `Project.objects` 什么都查不到——
            # 那条否定断言会空洞通过（本项目踩过这个坑）。
            game_b = Game.objects.create(name="星穹铁道")
            cat_b = ProjectCategory.objects.create(game=game_b, name="忘却之庭")
            Project.objects.create(category=cat_b, name="满星-B")
            self.assertTrue(Project.objects.filter(name="满星-B").exists())
            # 再证明看不到 A 的（用 A 独有的名字，避免与 B 自己的数据撞名）
            self.assertFalse(Game.objects.filter(name="原神").exists())
            self.assertFalse(ProjectCategory.objects.filter(name="深境螺旋").exists())
            self.assertFalse(Project.objects.filter(name="满星-A").exists())

    def test_audit_log_is_isolated_too(self):
        """最新加的表也得有隔离测试——`Order` / `Game` / `Project` 都有，`AuditLog` 没有。

        留痕是租户数据（见 `AuditLog` 的 docstring）：A 的留痕不能在 B 里被查到。
        """
        with tenant_context(self.tenant_a):
            AuditLog.objects.create(
                action=AuditLog.Action.STATUS_CHANGE,
                order_no="A-ONLY-0000000000001",
                actor="A 的客服",
            )

        with tenant_context(self.tenant_b):
            # **阳性对照**：先证明 B 自己能看见自己的留痕，
            # 否则下面的否定断言可能只是因为这张表什么都查不到（空洞通过）。
            AuditLog.objects.create(
                action=AuditLog.Action.PRICE_CHANGE,
                order_no="B-ONLY-0000000000001",
                actor="B 的客服",
            )
            self.assertTrue(
                AuditLog.objects.filter(order_no="B-ONLY-0000000000001").exists()
            )
            # 再证明看不到 A 的（用 A 独有的 order_no）
            self.assertFalse(
                AuditLog.objects.filter(order_no="A-ONLY-0000000000001").exists()
            )

        with tenant_context(self.tenant_a):
            self.assertTrue(
                AuditLog.objects.filter(order_no="A-ONLY-0000000000001").exists()
            )

    def test_public_schema_still_has_no_tenant_only_tables(self):
        """P1 的铁律在后续新增模型后依然成立——判据是查数据库，不是查模型元数据。

        ⚠️ 本用例原先只枚举 `business`（方法名也写着 `..._no_business_tables`），
        于是**账号线新增的 `apps.accounts` 被漏在兜底之外**：把它误挪进 `SHARED_APPS`
        时，`accounts_staffprofile` 会静默留在 public 而这里一声不响。现在把
        只属于租户平面的 app **全部**枚举进来，并显式自证每个 app 的表确实在清单里——
        否则「某个 app 被挪走」这条兜底还是一句空话。
        ⚠️ **开放平台线（T1）追加 `openapi`**：这是**第三个**租户专属 app，漏掉它就是把
        上面那条教训再犯一次（而且**今天不会红** —— 正是它危险的地方）。
        ⚠️ **打手侧线（T1）追加 `takerapi`**：这是**第四个**（spec
        `docs/specs/2026-10-08-taker-open-api-design.md` §8 第一行）——同一条教训第三次适用：
        漏掉它今天照样不会红。
        """
        tenant_only_tables = set()
        labels = ("business", "accounts", "openapi", "takerapi")
        for label in labels:
            models_of_app = list(apps.get_app_config(label).get_models())
            self.assertTrue(models_of_app, f"{label} app 下没有任何模型？")
            for model in models_of_app:
                tenant_only_tables.add(model._meta.db_table)
                # 多对多的**自动中间表**与主表同属租户平面（由同一个 app 的迁移建出来）
                # ⇒ ⛔ 不能只收 `get_models()` 的主表：中间表被误建进 public 时那条空洞
                # 会与主表那半一起静默（本用例要防的正是静默空洞）。
                for field in model._meta.local_many_to_many:
                    through = field.remote_field.through
                    if through._meta.auto_created:
                        tenant_only_tables.add(through._meta.db_table)
        # 自证枚举到了**每个** label 的表：少枚举一个，这里就对不上（不是"清单非空"那种恒真判据）。
        for label in labels:
            prefix = f"{apps.get_app_config(label).label}_"
            self.assertTrue(
                any(table.startswith(prefix) for table in tenant_only_tables),
                f"租户专属表清单里没有 {label} 的表：{sorted(tenant_only_tables)}",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            public_tables = {row[0] for row in cursor.fetchall()}
        # 先证明 public 的表列表**真的查到了东西**，否则下面的空集断言会空洞通过
        # （P1 的同族测试就是这么守的：assertIn("platform_tenant", public_tables)）
        self.assertIn("platform_tenant", public_tables)
        self.assertEqual(
            public_tables & tenant_only_tables,
            set(),
            f"租户专属表不得出现在 public：{sorted(public_tables & tenant_only_tables)}",
        )
