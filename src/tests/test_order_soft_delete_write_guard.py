"""`Order.deleted_at` / `deleted_by` 只由软删 / 恢复端点写（spec §6.1）。

两条通用写路径（`POST /orders/` 录单、`PATCH /orders/<单号>/` 编辑）都**不接受**
请求体指定这两列：它们是软删动作的落点，允许通用写就等于凭空多出第二个写入点，
绕过 `order.delete` 节点（软删端点的类级节点门）与 `order.delete` 审计行。

⚠️ 每条否定断言都带**阳性对照**（同一次请求里合法可写列真的写进去了），
否则「两列没被写」可能由「整个请求压根没生效」凑出来（`test_order_payment_status_field.py`
同形）。判据按 `read_only_fields` 的既有先例走 `OrderSerializer` /
`OrderUpdateSerializer`（`SERVER_WRITTEN_ORDER_FIELDS` / `DEDICATED_ENDPOINT_ONLY_ORDER_FIELDS`）。

⚠️ 留痕断言打**数据库行**（E-0009），不断言 logger；期望值写字面量（E-0024）。
⚠️ 夹具值一律合成（E-0077）；`nodes=` 必须与 `groups=` 成对给（节点挂在 Group → Role）。
"""
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BASE = "/api/business/orders"

#: 伪造的删除人（合成值，**不是**任何真实姓名）与伪造的删除时间。
FORGED_DELETER = "伪造的操作人甲"
FORGED_DELETED_AT = "2020-01-01T00:00:00+08:00"


class SoftDeleteWriteGuardTests(TenantTestCase):
    """两条通用写路径都写不进这两列，且都不产生 `order.delete` 审计行。"""

    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.client = make_logged_in_client(
            username="softdel-guard-cs",
            groups=("客服",),
            nodes=("order.add_edit", "order.submit_edit"),
        )
        grant_games(User.objects.get(username="softdel-guard-cs"), self.game)

    def _create(self, **overrides):
        payload = {"game": self.game.id, "project_text": "满星"}
        payload.update(overrides)
        body = self.client.post(
            BASE + "/", payload, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["order_no"]

    def test_patch_cannot_write_the_two_delete_columns(self):
        no = self._create()
        body = self.client.patch(
            f"{BASE}/{no}/",
            {"deleted_at": FORGED_DELETED_AT, "deleted_by": FORGED_DELETER},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        row = Order.all_objects.get(order_no=no)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")

    def test_patch_still_writes_the_columns_it_is_allowed_to_write(self):
        """阳性对照：同一次 PATCH 里的合法列真的写进去了。"""
        no = self._create(remark="旧备注")
        body = self.client.patch(
            f"{BASE}/{no}/",
            {"deleted_at": FORGED_DELETED_AT, "deleted_by": FORGED_DELETER,
             "remark": "新备注"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        row = Order.all_objects.get(order_no=no)
        self.assertEqual(row.remark, "新备注")
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")

    def test_create_cannot_birth_an_already_deleted_order(self):
        no = self._create(deleted_at=FORGED_DELETED_AT, deleted_by=FORGED_DELETER)
        row = Order.all_objects.get(order_no=no)
        # 阳性对照：同一次 create 里的 `project_text` 真的写进去了。
        self.assertEqual(row.project_text, "满星")
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")

    def test_neither_generic_path_leaves_an_order_delete_audit_row(self):
        """两条通用路径都不得留下 `order.delete` 行。

        阳性对照：那次 PATCH 改动了 `remark` ⇒ 必须留下 `order.update` 行，
        证明审计链路本身是通的（不是「审计压根没写」凑出来的空集）。
        """
        no = self._create(deleted_at=FORGED_DELETED_AT, deleted_by=FORGED_DELETER)
        body = self.client.patch(
            f"{BASE}/{no}/",
            {"deleted_at": FORGED_DELETED_AT, "deleted_by": FORGED_DELETER,
             "remark": "新备注"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            OperationLog.objects.filter(action="order.delete").count(), 0
        )
        self.assertEqual(
            OperationLog.objects.filter(action="order.update").count(), 1
        )
