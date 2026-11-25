"""两个权限点翻真 + 两条审计模板（spec §8-T6 第 30–33 条 / §8-T7 第 34–37 条 / §8-T8 第 38–41 条）。

⚠️ 留痕断言打**数据库行**（E-0009）且**钉值不钉常量**（E-0024）：
`summary` 一律写成**字面值**，⛔ 不许写 `ACTION_TEMPLATES["order.delete"].format(...)`。

⚠️ 夹具值一律合成（E-0077）：单号仿 20 位形状，人名用「测试…甲」。
⚠️ `nodes=` 必须与 `groups=` 成对给：节点挂在 **Group → Role** 上（`_grant_nodes` 遍历
   用户已有的组），没有组 ⇒ 一个节点都挂不上 ⇒ 全 403（T3 实施者实测）。
"""
from decimal import Decimal

from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.rbac.catalog import NODES
from tests.p5_auth_helpers import make_logged_in_client

NO = "20260924000000000301"
#: 操作人的合成用户名（认证态；`make_logged_in_client` 返回的 Django 测试 client
#: 没有 `.user` 属性 ⇒ 操作人按夹具**自己声明的那个**取）。
ACTOR_USERNAME = "测试操作人甲"


class RbacNodesAreLiveTests(TenantTestCase):
    def test_the_two_nodes_are_available_and_keep_their_benchmark_ids(self):
        # `catalog.NODES` 的元素是 7 元组：`(code, benchmark_id, name, note, url, kind, available)`
        # （本仓没有 namedtuple；`test_rbac_catalog.py` 同样按下标解包）。
        by_code = {row[0]: row for row in NODES}
        self.assertTrue(by_code["order.delete"][6])
        self.assertEqual(by_code["order.delete"][1], 23)
        self.assertTrue(by_code["order.recycle"][6])
        self.assertEqual(by_code["order.recycle"][1], 1219)


class AuditRowsTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        Order.objects.create(game=self.game, order_no=NO, total_price=Decimal("100.00"))
        self.client = make_logged_in_client(
            username=ACTOR_USERNAME,
            groups=("审计组",),
            nodes=("order.delete", "order.recycle"),
        )
        self.actor = ACTOR_USERNAME

    def test_delete_log_reports_the_removed_performance(self):
        self.client.post(
            f"/api/business/orders/{NO}/delete/",
            {"isDelEmp": "Y"},
            content_type="application/json",
        )
        rows = OperationLog.objects.filter(action="order.delete")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(
            row.summary,
            # ⭐ 审计日志线（spec §3.3）：人为操作的正文前面带 `【显示名】`。该夹具用户没有
            # `first_name` ⇒ 显示名就是登录账号（与 `ACTOR_USERNAME` 同值）。
            f"【{ACTOR_USERNAME}】已将订单 {NO} 移入回收站，并删除关联绩效记录",
        )
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.order_no, NO)
        self.assertEqual(row.actor, self.actor)

    def test_restore_log_identifies_the_restored_order(self):
        self.client.post(
            f"/api/business/orders/{NO}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        )
        self.client.post(f"/api/business/orders/{NO}/restore/")
        rows = OperationLog.objects.filter(action="order.restore")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(
            rows.get().summary, f"【{ACTOR_USERNAME}】已从回收站恢复订单 {NO}"
        )
        self.assertEqual(rows.get().log_type, 10)

    def test_delete_log_reports_the_retained_performance(self):
        self.client.post(
            f"/api/business/orders/{NO}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        )
        self.assertEqual(
            OperationLog.objects.get(action="order.delete").summary,
            f"【{ACTOR_USERNAME}】已将订单 {NO} 移入回收站，保留绩效记录",
        )
