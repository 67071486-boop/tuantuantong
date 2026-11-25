"""目录树接口（编辑权限弹层的数据源，spec §7）。"""
from django_tenants.test.cases import TenantTestCase

from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client


class NodeTreeApiTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        # ⚠️ **不要用 `self.client.force_login(...)`**：它产出的会话里没有 `identity_plane`，
        # 会被 `TenantSessionGuardMiddleware` 与 `TenantBoundAuthentication` 按 fail-closed 不变式
        # 拒成 **401**（`src/tests/p5_auth_helpers.py` 的 docstring 有实测，spec §5.1-3 有红线）。
        # 一律走真实登录夹具 `make_logged_in_client`；它**当场创建**用户（`seed_tenant` 的 admin
        # 是 `set_unusable_password()`，登不进去）。
        # ⚠️ 判据统一后不能用"登录后手工置 `is_superuser`"：管理员判据是
        # 「持『系统管理员』职位」，裸超管过不了节点门；夹具的 `is_superuser=True` 会顺手给组。
        self.client = make_logged_in_client(
            username="tree-admin", password="pw-123456", is_superuser=True
        )

    def test_tree_has_thirteen_modules_and_all_children(self):
        tree = self.client.get("/api/permission-nodes/").json()["data"]
        self.assertEqual(len(tree), 13)
        # ⭐ T4：150 → **151**（本仓第一条自有节点
        # `system.allow_taker_open_api`——它是 `system` 栏目下的子节点，所以也进这棵树）。
        # ⭐ 系统开关线（D-0223）：151 → **152**（第二条自有节点 `system.edit_settings`）。
        # ⭐ 首页二维码横幅线（D-0262）：152 → **153**（第三条自有节点
        # `home.qr_manage`——`home` 栏目下的子节点，所以也进这棵树）。
        # ⭐ QQ 机器人线：153 → **154**（第四条自有节点
        # `system.qqbot_settings`，同为 `system` 栏目下的子节点）。
        # ⭐ 打手钱包 / 提现审核线：154 → **155**（第五条自有节点
        # `finance.taker_withdraw_review`，`finance` 栏目下的子节点）。
        #    （第六条 `finance.taker_wallet_adjust` 的 155 → 156 只改了字面量、未补注记。）
        # ⭐ 闲管家收款明细线：156 → **157**（第七条自有节点
        # `finance.xgj_receipt_detail`，同为 `finance` 栏目下的子节点）。
        # ⭐ 频道文章作品线：157 → **158**（第八条自有节点
        # `system.article_manage`，同为 `system` 栏目下的子节点）。
        # ⭐ 交易平台涉钱写动作线（审计 M5）：158 → **159**（第九条自有节点
        # `order.platform_write`，`order` 栏目下的子节点）。
        # ⭐ 客户维护组收敛线（2026-11-11）：159 → **152**（客户维护组裁 8 个
        # 无功能 / 已删功能点 + 新增自有限制节点 `customer.member_list_self_only`；
        # 与 `catalog.EXPECTED_NODE_COUNT` 同源）。
        # ⭐ 企微自建应用线合并（2026-11-11）：152 → **153**（第十一条自有节点
        # `system.wecom_settings`；合并树实数，与 `catalog.EXPECTED_NODE_COUNT` 同源）。
        self.assertEqual(sum(len(m["children"]) for m in tree), 157)

    def test_node_carries_code_name_note_and_available(self):
        tree = self.client.get("/api/permission-nodes/").json()["data"]
        order = next(m for m in tree if m["code"] == "order")
        row = next(c for c in order["children"] if c["code"] == "order.list")
        self.assertEqual(row["name"], "订单管理列表")
        self.assertEqual(row["note"], "订单管理列表")
        self.assertIn("available", row)

    def test_modules_are_ordered_like_the_reference(self):
        tree = self.client.get("/api/permission-nodes/").json()["data"]
        self.assertEqual([m["code"] for m in tree][:3], ["my_orders", "my_performance", "my_profile"])
        self.assertEqual([m["code"] for m in tree][-1], "customer")
