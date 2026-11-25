"""配置三表（游戏 / 项目分类 / 代练项目）的**软删除**（业务方 2026-09-22 裁定，D-0085）。

    这一份守的是四件事，每一件都对应一个「静默出错」的形状：

    1. **不真删**：行留在库里、`deleted_at` 有时间戳、**历史订单/区服上的引用一个字不动**
    ——这是「允许删除」与「对得上账」能同时成立的唯一理由；
    2. **列表隐藏 + 可恢复**：默认列表里消失、`?deleted=1` 只看已删除、`PATCH {deleted_at: null}` 恢复；
    详情路径必须看得见已删除的行（否则恢复会 404）；
    3. **引用计数**（删除前那句警告的数据源）：⚠️ 同一条查询里对**两个及以上**多值关联做聚合会
    互相相乘——「2 笔订单 + 3 个分类」就是专门为这个笛卡尔积造的用例（不 `distinct` 会得 6/6）；
    4. **留痕仍是数据库行**（E-0009）：软删除照样写 `*.destroy` 那一行。

    ⚠️ 断言全部打在**数据库行 / 响应体**这些真实出口上，不打 logger、不看 `response.data` 是否为 None
    （后者在 DRF 里恒真，见 E-0063）。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import Game, GameServer, Order, OrderStatus, Project, ProjectCategory
from tests.p5_auth_helpers import make_logged_in_client


class ConfigSoftDeleteTests(TenantTestCase):
    def setUp(self):
        # ⚠️ `GameViewSet` 的写动作现在还要节点
        # （create / update / partial_update → `game.add_edit`；destroy → `game.delete`）。
        # 本文件的游戏删除 / 恢复 / 创建走真实端点 ⇒ 夹具显式声明这两条。
        # ⚠️ 本文件还经端点删 / 恢复**项目**与**分类**
        # （`DELETE|PATCH /projects/<id>/`、`DELETE /categories/<id>/`）⇒ 另需这两条节点。
        # ⚠️ `nodes=` **必须配 `groups=`**（`_grant_nodes` 遍历 `user.groups` 挂节点）；
        # 组名 ≤5 字（`Role.name` 的 `max_length=5`）。
        self.client = make_logged_in_client(
            groups=("配置组",),
            nodes=(
                "game.add_edit",
                "game.delete",
                "game.order_type_categories",
                "game.common_projects",
            ),
        )
        self.base = "/api/business"

    # ── 小助手 ──────────────────────────────────────────────
    def _items(self, path: str) -> list[dict]:
        body = self.client.get(path).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _delete(self, path: str) -> dict:
        response = self.client.delete(path)
        self.assertEqual(response.status_code, 200, response.content[:200])
        return response.json()

    def _restore(self, path: str) -> dict:
        response = self.client.patch(
            path, {"deleted_at": None}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        return response.json()

    def _tree(self):
        """一棵最小的配置树：游戏 → 分类 → 项目（＋一笔引用该分类的订单）。"""
        game = Game.objects.create(name="原神")
        category = ProjectCategory.objects.create(game=game, name="日常")
        project = Project.objects.create(category=category, name="委托")
        order = Order.objects.create(
            game=game,
            category=category,
            project=project,
            order_no="20260922000000000001",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        return game, category, project, order

    # ── 1. 不真删 + 历史引用不动 ─────────────────────────────
    def test_project_delete_is_soft_and_order_reference_survives(self):
        _, _, project, order = self._tree()

        body = self._delete(f"{self.base}/projects/{project.id}/")

        self.assertEqual(body["code"], 0)
        project.refresh_from_db()
        self.assertIsNotNone(project.deleted_at)
        # ⚠️ 这条是「软删除」的全部意义：订单上那条引用还在、指向的还是同一行。
        order.refresh_from_db()
        self.assertEqual(order.project_id, project.id)

    def test_category_delete_cascades_its_projects_but_not_orders(self):
        """删分类 ⇒ 它下面**还活着**的项目一起下架；订单上的引用原样保留。

    为什么级联：父级下架后各页下拉里再也选不到它 ⇒ 留着活着的项目会得到
    「项目页按游戏过滤仍然显示、『所属分类』列却空着」那种半死不活的状态。
"""
        _, category, project, order = self._tree()

        body = self._delete(f"{self.base}/categories/{category.id}/")

        self.assertEqual(body["code"], 0)
        category.refresh_from_db()
        project.refresh_from_db()
        self.assertIsNotNone(category.deleted_at)
        self.assertIsNotNone(project.deleted_at, "分类下的项目必须一起下架")
        order.refresh_from_db()
        self.assertEqual(order.category_id, category.id)
        self.assertEqual(order.project_id, project.id)

    def test_game_delete_cascades_categories_and_projects(self):
        game, category, project, order = self._tree()

        body = self._delete(f"{self.base}/games/{game.id}/")

        self.assertEqual(body["code"], 0)
        for row in (game, category, project):
            row.refresh_from_db()
            self.assertIsNotNone(row.deleted_at, f"{row!r} 没有被级联下架")
        order.refresh_from_db()
        self.assertEqual((order.game_id, order.category_id, order.project_id),
                         (game.id, category.id, project.id))

    # ── 2. 列表隐藏 / 已删除视图 / 恢复 ──────────────────────
    def test_deleted_rows_hide_from_list_and_show_in_deleted_view(self):
        game, category, project, _ = self._tree()

        self._delete(f"{self.base}/projects/{project.id}/")

        alive = [r["name"] for r in self._items(f"{self.base}/projects/?game={game.id}")]
        self.assertNotIn("委托", alive)
        deleted = [r["name"] for r in self._items(
            f"{self.base}/projects/?game={game.id}&deleted=1"
        )]
        self.assertEqual(deleted, ["委托"])
        # 恢复之后回到正常列表。
        self.assertEqual(self._restore(f"{self.base}/projects/{project.id}/")["code"], 0)
        project.refresh_from_db()
        self.assertIsNone(project.deleted_at)
        alive = [r["name"] for r in self._items(f"{self.base}/projects/?game={game.id}")]
        self.assertIn("委托", alive)

    def test_detail_of_a_deleted_row_is_reachable(self):
        """恢复路径的前提：详情 / PATCH 必须看得见已删除的行（列表才过滤）。"""
        game, _, _, _ = self._tree()
        self._delete(f"{self.base}/games/{game.id}/")

        body = self.client.get(f"{self.base}/games/{game.id}/").json()

        self.assertEqual(body["code"], 0, "已删除的行在详情路径上必须是可达的，否则恢复会 404")
        self.assertEqual(body["data"]["name"], "原神")
        self.assertIsNotNone(body["data"]["deleted_at"])

    def test_deleted_param_is_whitelisted(self):
        """`?deleted=abc` 回业务码 1000——**不静默当成 0**（同 E-0021 的口径）。"""
        self._tree()
        body = self.client.get(f"{self.base}/games/?deleted=abc").json()
        self.assertEqual(body["code"], 1000, body)
        # 白名单内的取值正常放行（正面哨兵，防「一律拒绝」也算通过）。
        for value in ("0", "1", "true", "false", ""):
            self.assertEqual(self.client.get(f"{self.base}/games/?deleted={value}").json()["code"], 0)

    # ── 3. 引用计数（删除前的警告） ──────────────────────────
    def test_reference_counts_survive_multiple_joined_relations(self):
        """⚠️ 笛卡尔积守卫：2 笔订单 + 3 个分类时，两个计数必须还是 2 和 3。

    朴素写法（`Count("orders")` + `Count("categories")` 不加 `distinct`）会让两个 JOIN
    相乘 ⇒ 这里会读到 6 / 6：数字看着像「有数据」，其实全错。加 `distinct=True` 才对。
"""
        game = Game.objects.create(name="原神")
        for i in range(3):
            ProjectCategory.objects.create(game=game, name=f"分类{i}")
        for i in range(2):
            Order.objects.create(
                game=game,
                order_no=f"2026092200000000000{i + 1}",
                status=OrderStatus.IN_PROGRESS,
                hall_price=Decimal("1.00"),
            )

        row = self._items(f"{self.base}/games/")[0]

        self.assertEqual(row["order_count"], 2)
        self.assertEqual(row["category_count"], 3)
        self.assertEqual(row["project_count"], 0)

    def test_reference_counts_ignore_already_deleted_children(self):
        """警告里说「还有 N 个项目」得是**页面上还看得到的**那 N 个，不含已下架的。"""
        game, category, project, _ = self._tree()
        ProjectCategory.objects.create(game=game, name="活动")
        self._delete(f"{self.base}/projects/{project.id}/")

        category_row = self._items(f"{self.base}/categories/?game={game.id}")[0]
        game_row = self._items(f"{self.base}/games/")[0]

        self.assertEqual(category_row["project_count"], 0, "别把已下架的项目算进警告")
        self.assertEqual(game_row["project_count"], 0)
        self.assertEqual(game_row["category_count"], 2)
        self.assertEqual(game_row["order_count"], 1)

    def test_counts_are_present_on_the_create_response(self):
        """创建/更新响应走的是**没有注解**的回落路径（`COUNT` 查询）——形状必须一致。"""
        body = self.client.post(
            f"{self.base}/games/", {"name": "崩坏"}, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["order_count"], 0)
        self.assertEqual(body["data"]["category_count"], 0)
        self.assertEqual(body["data"]["project_count"], 0)

    # ── 4. 留痕仍是数据库行 ─────────────────────────────────
    def test_soft_delete_still_writes_a_destroy_audit_row(self):
        game, _, _, _ = self._tree()
        before = OperationLog.objects.count()

        self._delete(f"{self.base}/games/{game.id}/")

        self.assertEqual(OperationLog.objects.count(), before + 1)
        row = OperationLog.objects.order_by("-id").first()
        self.assertEqual(row.action, "system.game_destroy")
        self.assertEqual(row.target_kind, "game")
        self.assertEqual(str(row.target_id), str(game.id))
        self.assertEqual(row.target_name, "原神")

    def test_restore_writes_an_update_audit_row(self):
        """恢复是一次普通 `PATCH` ⇒ 走既有的 `*.update` 埋点（**没有**为恢复新增审计动作）。"""
        game, _, _, _ = self._tree()
        self._delete(f"{self.base}/games/{game.id}/")
        before = OperationLog.objects.count()

        self._restore(f"{self.base}/games/{game.id}/")

        self.assertEqual(OperationLog.objects.count(), before + 1)
        self.assertEqual(OperationLog.objects.order_by("-id").first().action, "system.game_update")



    def test_reference_count_ignores_hall_shadow_orders(self):
        """⭐ Minor：游戏删除计数不把「镜像卡派生的隐藏单」算成真订单。"""
        from django.contrib.auth import get_user_model

        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        owner = get_user_model().objects.create_user(username="syn-shadow-count")
        game = Game.objects.create(name="原神-隐藏单")
        Order.objects.create(
            game=game, order_no="20260922000000000031",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("1.00"),
        )
        card = make_mirror(game=game, bound_by=owner, no="SYN-SHADOW-COUNT")
        make_hidden_order(game=game, card=card, order_no="20260922000000000032")

        rows = self._items(f"{self.base}/games/")
        row = next(r for r in rows if r["name"] == "原神-隐藏单")
        self.assertEqual(row["order_count"], 1)

    def test_reference_count_ignores_upstream_hall_shadow_orders(self):
        """⭐ 评审 R-4（2026-10-29）：游戏删除计数也不把**上家镜像**隐藏单（D-0325）算成真单。

    它同样没有客户 / 绩效，与 peer 族隐藏单同一口径；两条取值路都要钉——
    列表走 ViewSet 注解（`GameViewSet.count_annotations`），详情 / 创建响应走
    序列化器回落（`GameSerializer.order_count` 的 `COUNT`），漏一路数字就会说假话。
    失败方向 = 摘掉任一路的 `upstream_hall_card` 排除 ⇒ 对应断言红。
"""
        from datetime import datetime, timezone

        from apps.business.models import JidanpuUpstreamHallOrder

        game = Game.objects.create(name="原神-上家隐藏单")
        Order.objects.create(
            game=game, order_no="20260922000000000041",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("1.00"),
        )
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://jidanpu-up-r4.test.invalid",
            upstream_order_id="33160000000000003001",
            game=game,
            first_seen_at=datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc),
        )
        Order.objects.create(
            game=game, order_no="20260922000000000042",
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("1.00"),
            source_order_no="33160000000000003001",
            upstream_hall_card=mirror,
        )

        # 路 1：列表（注解路）。
        row = self._items(f"{self.base}/games/?deleted=0")
        row = next(r for r in row if r["name"] == "原神-上家隐藏单")
        self.assertEqual(row["order_count"], 1)
        # 路 2：详情（无注解 ⇒ 序列化器回落路）。
        body = self.client.get(f"{self.base}/games/{game.id}/").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["order_count"], 1)


class ConfigSoftDeleteNullabilityTests(TenantTestCase):
    """新列是**可空**的，且既有数据不受影响（升级是纯增量）。"""

    def test_new_rows_start_alive(self):
        game = Game.objects.create(name="原神")
        category = ProjectCategory.objects.create(game=game, name="日常")
        project = Project.objects.create(category=category, name="委托")
        for row in (game, category, project):
            self.assertIsNone(row.deleted_at, f"{row!r} 新建时不该是已删除状态")

    def test_game_server_is_not_soft_deleted(self):
        """区服**没有**软删除列（三页 UI 里没有区服维护页）——删游戏不会动它，仅登记在 spec。"""
        game = Game.objects.create(name="原神")
        server = GameServer.objects.create(game=game, name="国服")
        self.assertFalse(hasattr(server, "deleted_at"))
