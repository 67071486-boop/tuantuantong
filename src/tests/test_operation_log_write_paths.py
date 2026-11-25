"""Task 8：订单域 + 绩效域的写路径埋点（spec §5.1 / §5.2 / §5.9 / §10.2）。

**本文件是 Task 9–11 的追加点**（由 Task 8 创建，见计划预检 Ruling R2）。

每一条用例的形状（spec §10.2）：调一个真实端点 → 断言**新表** `OperationLog` 里
**恰好一行**（`/adjust`、`order.update` 是「一个被改字段一行」）→ 逐项断言**字面值**：
`action` / `log_type` / 渲染出来的 `summary` / 前后值 / `actor`。

三条纪律（本文件刻意都做到）：
1. **断言打在数据库行上**（E-0009）：不碰 `logger`、不用 `assertLogs`。
2. **期望值写字面值**（E-0024）：`summary` / `log_type` / `action` 全部手写字面串与数字，
   **不**用 `ACTION_TEMPLATES[action].format(...)` 之类的自指断言——那样模板被改坏时测试不会红。
3. **反向控制**：被拒的流转 / 被拒的改价 / 失败的抢单，新表必须**零行**；
   **订单域与绩效域的**正向用例还顺带断言**旧表 `AuditLog` 没有被写**（证明是「替换」而不是
   「双写」）——这类断言只出现在 `_WritePathBase._only` / `_assert_zero_new_rows` 与订单/绩效的
   两处用例里；账号 / RBAC / 配置三个域各有自己的断言助手，**不查**旧表。不要把这句话读成
   「本文件每条正向用例都查了旧表」。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, Permission, User
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import AccountSource, CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.log import record as real_record
from apps.audit.models import OperationLog
from apps.business.api.config_views import (
    GameServerViewSet,
    GameViewSet,
    ProjectCategoryViewSet,
    ProjectViewSet,
)
from apps.business.api.shop_binding_views import ShopBindingViewSet
from apps.business.api.source_group_views import OrderSourceGroupViewSet
from apps.business.api.source_views import OrderSourceViewSet
from apps.business.api.xgj_views import XgjShopBindingViewSet
from apps.platform.provisioning import seed_tenant
from apps.rbac.models import Role
from apps.rbac.seed import seed_catalog
from apps.business.models import (
    AuditLog,
    EmpType,
    Game,
    GameServer,
    Order,
    OrderSource,
    OrderStatus,
    Performance,
    Project,
    ProjectCategory,
    ReviewStatus,
    ShopBinding,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client

# 本文件里订单号统一用它，便于 `_rows()` 一次性收口。
ORDER_NO = "20260921000000000001"

# ⚠️ **为什么这里要显式声明节点**：订单与绩效两组端点**逐 action** 要节点 code
# （`OrderViewSet.ORDER_PERMISSIONS` / `PerformanceViewSet.PERFORMANCE_PERMISSIONS`），
# 而 `make_logged_in_client(groups=...)` 只建 `Group`、**不放行**
# （`tests/p5_auth_helpers.py`：`nodes` 默认空集 ⇒ 打受节点门保护的端点必然 403，
# fail-closed 是刻意的）——缺了这里，本文件全体用例都会因 403 变红
# （对照 `test_order_api.py` 的 `nodes=(...)`、`test_audit_coverage.py` 的 `AUDIT_ORDER_NODES`）。
# ⚠️ **只补前提、不放宽判据**：「这个端点要哪个 code」由
# `test_rbac_endpoint_enforcement.py` 与各端点自己的用例钉着；本文件问的是
# 「这个动作有没有写日志行」，授权维度不该混进来（混进来只会让判据退化）。
ORDER_WRITE_NODES = (
    "order.add_edit",                 # 录单（create）
    "order.submit_edit",              # 编辑（partial_update；与 add_edit 是 any）
    "order.view_detail",              # 详情（retrieve）
    "order.view_content",
    "order.change_status",            # 状态流转（transition）
    "order.set_offer_price",          # `/hall-price/`（改发单价；2026-10-26 起不再开抛单独占门）
    "order.send_to_grab_hall",        # `/throw-to-hall/`（独占闸）+ `/transition/` 的待抢单目标
    "order.submit_payout_price",      # 改价 / 指派 / 绩效调价（adjust）
    "order.edit_payout_price",
    "order.withdraw_from_grab_hall",  # 撤回
    "order.assign_taker",             # 指派
    "order.clear_taker",              # 清除接单人
)

#: 代练员：交单（`order.submit_work`，对标 105 改造后的 code）+ 抢单（`grab_hall.take_order`）。
TAKER_WRITE_NODES = ("order.submit_work", "grab_hall.take_order")

#: 客服主管：审核绩效（`order.change_status_completed`）。
REVIEWER_WRITE_NODES = ("order.change_status_completed",)


class _WritePathBase(TenantTestCase):
    """公共夹具 + 「恰好一行」/「新表零行」/「旧表零行」三个断言助手。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.cat = ProjectCategory.objects.create(game=self.game, name="深境螺旋")
        self.project = Project.objects.create(category=self.cat, name="满星")
        # 客服：派单 / 改价 / 编辑那一组端点的调用方。
        # `nodes` 见文件头：不声明节点就拿 403（fail-closed，刻意）。
        self.cs = make_logged_in_client(
            username="cs1", groups=("客服",), nodes=ORDER_WRITE_NODES
        )
        self.cs_user = User.objects.get(username="cs1")
        # **夹具补齐**——本类所有订单 / 绩效写路径都由
        # `cs1` 发起，读侧过 `scope_queryset(game_scope)`、写侧过 `assert_game_allowed`，
        # 缺 `StaffProfile` 会读侧空结果 / 写侧业务码 1000。**不是放宽判据**：
        # 本文件所有用例的期望值一个字没改。
        grant_games(self.cs_user, self.game)

    # ── 夹具 ─────────────────────────────────────────────────────
    def _order(self, **overrides):
        fields = {
            "game": self.game,
            "order_no": ORDER_NO,
            # 编辑路径会重跑 `validate_required`，所以订单必须有项目名（否则 1000）
            "project_text": "满星",
            "hall_price": Decimal("88.00"),
        }
        fields.update(overrides)
        return Order.objects.create(**fields)

    def _perf(self, order, **overrides):
        fields = {
            "order": order,
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("88.00"),
        }
        fields.update(overrides)
        return Performance.objects.create(**fields)

    def _grant(self, user, codename):
        user.user_permissions.add(Permission.objects.get(codename=codename))
        return user

    # ── 断言助手 ─────────────────────────────────────────────────
    def _rows(self, order_no, action):
        return OperationLog.objects.filter(order_no=order_no, action=action)

    def _only(self, order_no, action, *, log_type, summary):
        """钉住「这个动作恰好一行」，并逐项断言字面值。返回那一行。"""
        rows = list(self._rows(order_no, action))
        self.assertEqual(
            len(rows), 1,
            f"期望 {action} 恰好一行，实际 {len(rows)} 行："
            f"{[(r.action, r.summary) for r in OperationLog.objects.filter(order_no=order_no)]}",
        )
        row = rows[0]
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, log_type)
        self.assertEqual(row.summary, summary)
        # 旧表不得有同一订单的行（替换，不是双写）
        self.assertFalse(
            AuditLog.objects.filter(order_no=order_no).exists(),
            "转换后的写路径仍往旧 AuditLog 表写了一行 —— 这是双写",
        )
        return row

    def _assert_zero_new_rows(self, order_no):
        rows = list(OperationLog.objects.filter(order_no=order_no))
        self.assertEqual(
            rows, [], f"{order_no} 的失败路径不得在新表留下任何行，实际："
                      f"{[(r.action, r.summary) for r in rows]}",
        )
        self.assertFalse(AuditLog.objects.filter(order_no=order_no).exists())


# ══════════════════════════════════════════════════════════════════
# 订单域（spec §5.1）
# ══════════════════════════════════════════════════════════════════
class OrderWritePathLogTests(_WritePathBase):
    def test_create_writes_order_create_with_source_and_dispatch_price(self):
        body = self.cs.post(
            "/api/business/orders/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "source_order_no": "TB-1",
                "hall_price": "88.00",
                "total_price": "199.50",
                "actor": "客服小张",
                # 伪造的 actor_id：请求体里的身份没有入口，必须被忽略（Fix 2）
                "actor_id": 999999,
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        no = body["data"]["order_no"]

        row = self._only(
            no, "order.create", log_type=10,
            summary=f"cs1录入了订单 {no}，来源单号：TB-1，发单价 88.00 元",
        )
        self.assertEqual(row.source_order_no, "TB-1")
        self.assertEqual(row.to_value, "88.00")
        # ⚠️ R20：审计行的操作人取**认证态**；请求体里那个 "客服小张" 是**可伪造**的，
        # 必须**不出现**在留痕里（改价同理）。业务字段 `created_by` 另说（P2 契约）。
        self.assertEqual(row.actor, "cs1")
        self.assertNotIn("客服小张", row.actor)
        # R20 的另一半（Fix 2）：`actor_id` 也是**认证态的主键**，请求体伪造的 999999 无效
        self.assertEqual(row.actor_id, self.cs_user.pk)
        self.assertNotEqual(row.actor_id, 999999)

    def test_update_writes_one_row_per_changed_field(self):
        # ⚠️ `Order.total_price` 是 **NOT NULL + default 0.00**（迁移
        # `0010_p4_price_revision`），「NULL 总价的单」已不可表示。
        # `None/空 → 值` 的渲染这条腿仍在：`remark` 的 `from_value` 就是空串
        # （见下面的备注断言）。
        order = self._order(customer_phone="13800138000")
        response = self.cs.patch(
            f"/api/business/orders/{ORDER_NO}/",
            {
                "customer_phone": "13900139000",
                "remark": "客户要求静音",
                "is_urgent": True,
                "actor": "客服小张",
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)

        rows = list(self._rows(ORDER_NO, "order.update"))
        self.assertEqual(len(rows), 3, [r.summary for r in rows])
        self.assertEqual([r.log_type for r in rows], [10, 10, 10])
        # 按 `field_name` 取行（`validated_data` 的迭代顺序是**序列化器字段序**，
        # 不是请求体顺序 —— 不要断言行的先后）
        by_field = {row.field_name: row for row in rows}
        self.assertEqual(
            sorted(by_field), ["customer_phone", "is_urgent", "remark"]
        )
        self.assertEqual(
            by_field["customer_phone"].summary,
            f"cs1编辑了订单 {ORDER_NO}，联系电话由13800138000改为13900139000",
        )
        self.assertEqual(
            by_field["remark"].summary,
            f"cs1编辑了订单 {ORDER_NO}，重要备注由未填写改为客户要求静音",
        )
        self.assertEqual(by_field["remark"].from_value, "")
        self.assertEqual(
            by_field["is_urgent"].summary,
            f"cs1编辑了订单 {ORDER_NO}，是否加急由否改为是",
        )
        # `field_name` 存**真实列名**、中文只由静态标签表派生（R8）
        for row in rows:
            self.assertNotIn(row.field_name, row.summary)
            # R20：每行的操作人都取认证态，请求体里的 "客服小张" 不得出现
            self.assertEqual(row.actor, "cs1")
            self.assertNotIn("客服小张", row.actor)
        self.assertFalse(AuditLog.objects.filter(order_no=ORDER_NO).exists())

    def test_update_pays_one_actor_lookup_per_logged_field(self):
        """⭐ 审计日志线（spec §3.2 的 ⚠️ **代价**）：每行日志各查一次用户 = **真的 N+1 形状**。

        `record()` 为了派生 `actor_name` 要按主键查一次 `auth_user`，而 `order.update` 是
        「一个被改字段一行」⇒ 一次改 k 个字段 = k 行日志 = **k 次额外 SELECT**
        （⛔ 不要写成「不是 N+1」；spec 与业务方都要求把这条读数如实登记）。

        判据打在**真实端点**上（`PATCH /api/business/orders/<单号>/`，k = 3），
        并且用**同端点的零行基线**（同值 PATCH ⇒ 不写日志）扣掉「认证态 / 权限」本来就会有的
        那次 `auth_user` 查询 ⇒ 增量才是 `record()` 的代价。
        ⚠️ **实测读数（本用例现场）**：基线 `1 次 / 52 条 SQL`；
        改 3 个字段后 `4 次 / 64 条 SQL` ⇒ `record()` 的 3 次主键查询占该次请求总 SQL 的
        **4/64 ≈ 6%**、占增量（12 条）的 **3/12**。代价可接受 ⇒ 按「每行查一次」实现，
        ⛔ 不加进程级缓存（显示名随改名变，缓存会撒谎；spec §3.2）。
        """
        self._order(customer_phone="13800138000")

        def patch(payload):
            with CaptureQueriesContext(connection) as ctx:
                response = self.cs.patch(
                    f"/api/business/orders/{ORDER_NO}/", payload,
                    content_type="application/json",
                )
            self.assertEqual(response.json()["code"], 0, response.content)
            lookups = [
                q["sql"] for q in ctx.captured_queries
                if 'FROM "auth_user"' in q["sql"] and '"auth_user"."id" = ' in q["sql"]
            ]
            return lookups, len(ctx.captured_queries)

        # ① 基线：同一个端点、**零行日志**（同值 PATCH ⇒ 没有真变化）。
        base_lookups, base_total = patch({"customer_phone": "13800138000"})
        self.assertEqual(self._rows(ORDER_NO, "order.update").count(), 0)

        # ② 真写 3 行（一个被改字段一行）。
        lookups, total = patch({
            "customer_phone": "13900139000", "remark": "客户要求静音", "is_urgent": True,
        })
        self.assertEqual(self._rows(ORDER_NO, "order.update").count(), 3)

        extra = len(lookups) - len(base_lookups)
        self.assertEqual(
            extra, 3,
            f"3 行日志应当各多查一次用户：基线 {len(base_lookups)}/{base_total} 条 SQL，"
            f"写 3 行后 {len(lookups)}/{total} 条 ⇒ 多 {extra} 次主键查询",
        )
        # 这 3 次是**额外**的（总 SQL 条数也确实跟着涨），不是被别的查询顺带覆盖。
        self.assertGreater(total, base_total)

    def test_update_does_not_write_a_row_for_an_unchanged_field(self):
        """`{旧} → {新}` 只对**真的变了**的字段成立；同值 PATCH 不留噪音行。"""
        self._order(customer_phone="13800138000")
        self.cs.patch(
            f"/api/business/orders/{ORDER_NO}/",
            {"customer_phone": "13800138000", "actor": "客服小张"},
            content_type="application/json",
        )
        self.assertEqual(self._rows(ORDER_NO, "order.update").count(), 0)
        # 阳性对照：换成另一个值就**会**写一行 ⇒ 上面的 0 不是「表根本不可达」
        self.cs.patch(
            f"/api/business/orders/{ORDER_NO}/",
            {"customer_phone": "13700137000", "actor": "客服小张"},
            content_type="application/json",
        )
        self.assertEqual(self._rows(ORDER_NO, "order.update").count(), 1)

    def test_update_rolls_back_the_order_and_all_rows_when_a_record_fails(self):
        """R21：`partial_update` 现在有事务边界——一次编辑写**多行**留痕，
        中途失败必须让业务写入与**已写的留痕**一起回滚（E-0022 家族的反面）。

        判别力：去掉 `partial_update` 的 `atomic()` ⇒ `save()` 与第一行 `record`
        已各自提交 ⇒ 异常之后订单仍是新值、新表有 1 行 ⇒ 两条断言都变红。

        ⚠️ 桩打在 `order_writes.record`：动作体已搬进领域核心
        `apps/business/order_writes.py`，`record` 在**那里**被查找（E-0029）。
        """
        self._order(customer_phone="13800138000", remark="旧备注")
        seen = {}
        calls = {"n": 0}

        def _boom(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_record(**kwargs)
            # 哨兵：第一行留痕与业务新值都已落在同一事务里（读得到自己未提交的写入）
            seen["rows"] = OperationLog.objects.filter(order_no=ORDER_NO).count()
            seen["phone"] = Order.objects.get(order_no=ORDER_NO).customer_phone
            raise RuntimeError("留痕写不进去")

        with mock.patch(
            "apps.business.order_writes.record", side_effect=_boom
        ):
            with self.assertRaises(RuntimeError):
                self.cs.patch(
                    f"/api/business/orders/{ORDER_NO}/",
                    {
                        "customer_phone": "13900139000",
                        "remark": "新备注",
                        "actor": "客服小张",
                    },
                    content_type="application/json",
                )
        self.assertEqual(seen["rows"], 1)                 # 第一行真的写过
        self.assertEqual(seen["phone"], "13900139000")    # 业务写入也真的发生过
        order = Order.objects.get(order_no=ORDER_NO)
        self.assertEqual(order.customer_phone, "13800138000")   # ① 回滚
        self.assertEqual(order.remark, "旧备注")                 # ① 回滚
        self.assertEqual(                              # ② 已写的留痕也回滚
            OperationLog.objects.filter(order_no=ORDER_NO).count(), 0
        )

    def test_transition_writes_chinese_labels_and_field_name_status(self):
        self._order(status=OrderStatus.UNASSIGNED)
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "order.transition", log_type=10,
            summary=f"cs1将订单 {ORDER_NO} 的状态从未分配改为待抢单",
        )
        self.assertEqual(row.field_name, "status")
        # 前后值是**中文标签**（spec §5.1 / R15 的迁移口径一致），不是状态码
        self.assertEqual(row.from_value, "未分配")
        self.assertEqual(row.to_value, "待抢单")
        self.assertEqual(row.actor, "cs1")
        # Fix 2：`actor_id` 也是认证态主键（不是 null、不来自请求体）
        self.assertEqual(row.actor_id, self.cs_user.pk)

    def test_throw_to_hall_records_the_dispatch_price(self):
        self._order(status=OrderStatus.UNASSIGNED, hall_price=Decimal("88.00"))
        self._grant(self.cs_user, "override_taker_price")
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/throw-to-hall/",
            {"price": "66.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "order.throw_to_hall", log_type=10,
            summary=f"【cs1】将订单 {ORDER_NO} 抛入接单大厅，发单价：66.00",
        )
        self.assertEqual(row.to_value, "66.00")

    def test_withdraw_records_the_order(self):
        self._order(status=OrderStatus.WAITING_GRAB)
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/withdraw/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "order.withdraw", log_type=10,
            summary=f"【cs1】将订单 {ORDER_NO} 从接单大厅撤回",
        )
        # ⚠️ `待抢单 → 未分配` 的 `actor` 腿自旧表行（`test_dispatch_api`）搬来：
        # 旧行退役、结论不变，不留静默的覆盖缺口。
        self.assertEqual(row.actor, "cs1")

    def test_assign_first_time_uses_performance_assign(self):
        order = self._order(status=OrderStatus.UNASSIGNED, hall_price=Decimal("88.00"))
        self._perf(order)
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        # ⚠️ 分类照抄对标的不自洽边界：`performance.assign` → **20**（spec A2/§11-1）
        row = self._only(
            ORDER_NO, "performance.assign", log_type=20,
            summary=f"cs1将订单指派给taker1，发单价：88.00（订单 {ORDER_NO}）",
        )
        self.assertEqual(row.target_name, "taker1")
        self.assertEqual(row.target_id, str(taker.id))
        self.assertEqual(row.to_value, "88.00")
        # ⚠️ `未分配 → 代练中` 的 `actor` 腿自旧表行（`test_dispatch_api`）搬来：
        # 旧行退役、结论不变，不留静默的覆盖缺口。
        self.assertEqual(row.actor, "cs1")

    def test_assign_with_a_changed_price_uses_performance_reassign(self):
        self._order(status=OrderStatus.UNASSIGNED, hall_price=Decimal("88.00"))
        self._perf(Order.objects.get(order_no=ORDER_NO))
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(taker, self.game)
        self._grant(self.cs_user, "override_taker_price")
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/assign/",
            {"user_id": taker.id, "price": "66.00"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "performance.reassign", log_type=20,
            summary=(
                "cs1将订单重新指派给taker1，发单价有变化，"
                f"原来发单价：88.00，新发单价：66.00（订单 {ORDER_NO}）"
            ),
        )
        self.assertEqual(row.from_value, "88.00")
        self.assertEqual(row.to_value, "66.00")

    def test_assign_to_a_different_person_at_the_same_price_uses_assign(self):
        """换人**但发单价没变** ⇒ `performance.assign`（评审 Fix 1）。

        `performance.reassign` 的模板自己断言「**发单价有变化**」；同价换人若走那条模板，
        渲染出来就是「原来发单价：88.00，新发单价：88.00」——一句**写在审计日志里的假话**。
        `performance.assign` 的句子在「首次指派」与「同价换人」两种情况下都为真，
        换人这一事实由 `target_name` 如实写出。
        """
        order = self._order(status=OrderStatus.UNASSIGNED, hall_price=Decimal("88.00"))
        other = User.objects.create_user(username="other1")
        self._perf(order, user=other)
        taker = User.objects.create_user(username="taker1")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标（新打手）也要授权。
        grant_games(taker, self.game)
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "performance.assign", log_type=20,
            summary=f"cs1将订单指派给taker1，发单价：88.00（订单 {ORDER_NO}）",
        )
        self.assertEqual(row.target_name, "taker1")          # 换人这件事仍然如实写出
        self.assertEqual(row.to_value, "88.00")
        # 不得出现「原来发单价」，且价格只出现一次（没有 88.00 → 88.00 的假变化）
        self.assertNotIn("原来发单价", row.summary)
        self.assertEqual(row.summary.count("88.00"), 1)
        self.assertFalse(self._rows(ORDER_NO, "performance.reassign").exists())

    def test_claim_writes_order_claim_with_the_portal_channel(self):
        order = self._order(status=OrderStatus.WAITING_GRAB, hall_price=Decimal("88.00"))
        self._perf(order)
        taker = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=TAKER_WRITE_NODES
        )
        # **夹具补齐**——抢单方要显式持有该单的游戏。
        grant_games(User.objects.get(username="taker1"), order.game)
        body = taker.post(
            f"/api/business/hall/{ORDER_NO}/claim/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        # `order.claim` 归 **10 订单相关**（照抄对标的不自洽边界，不要「修正」）
        # 网页接单正文只显示一次接单账号名称。
        row = self._only(
            ORDER_NO, "order.claim", log_type=10,
            summary=f"taker1从网页端大厅接手了订单 {ORDER_NO}，接手价 88.00 元",
        )
        self.assertEqual(row.target_name, "taker1")
        self.assertEqual(row.reason, "网页端大厅")
        self.assertEqual(row.to_value, "88.00")
        # ⚠️ 这条 `actor` 腿自旧表的 `STATUS_CHANGE` 行（`test_claim.py`）搬来：
        # 旧行退役、结论不变，不留静默的覆盖缺口。
        self.assertEqual(row.actor, "taker1")

    def test_clear_taker_names_the_former_taker(self):
        order = self._order(status=OrderStatus.UNASSIGNED)
        taker = User.objects.create_user(username="taker1")
        self._perf(order, user=taker)
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status"])

        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/clear-taker/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "order.clear_taker", log_type=10,
            summary=f"【cs1】清除了订单 {ORDER_NO} 的接单人（原接单人：taker1）",
        )
        self.assertEqual(row.target_name, "taker1")
        # ⚠️ `代练中 → 未分配` 的 `actor` 腿自旧表行（`test_dispatch_api`）搬来：
        # 旧行退役、结论不变，不留静默的覆盖缺口。
        self.assertEqual(row.actor, "cs1")

    def test_submit_names_the_taker_and_records_no_amount(self):
        order = self._order(status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"))
        taker_client = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=TAKER_WRITE_NODES,
            # ⚠️ 显式 opt-out（夹具默认给当前租户全部游戏）：下面自己建带提成率的档案 ⇒
            # 夹具必须**不建**，否则撞唯一约束；游戏由下面那句 `grant_games` 逐款补。
            games=None,
        )
        taker = User.objects.get(username="taker1")
        StaffProfile.objects.create(
            user=taker, commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self._perf(order, user=taker)
        # **夹具补齐**——交单方要显式持有该单的游戏。
        # ⚠️ 必须放在上面 `StaffProfile.objects.create` **之后**：本函数是
        # `get_or_create`，不会撞唯一约束，也不会抹掉这里设的提成率。
        grant_games(taker, order.game)

        body = taker_client.post(
            f"/api/business/orders/{ORDER_NO}/submit/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        # ⭐ 逐字期望是 `taker1提交了订单…`（单名：模板用 `{actor_name}`，不叠前缀）。
        row = self._only(
            ORDER_NO, "performance.submit", log_type=20,
            summary=f"taker1提交了订单 {ORDER_NO} 的完单，绩效金额：0.00",
        )
        self.assertEqual(row.target_name, "taker1")
        # ⭐ R2：交单不写任何金额 ⇒ 留痕额是显式的 `0.00`（「本次未写金额」）
        self.assertEqual(row.to_value, "0.00")

    def test_price_writes_change_taker_price_with_total_price_column(self):
        self._order(total_price=Decimal("199.50"))
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/price/",
            {"total_price": "88.00", "reason": "客户砍价", "actor": "客服小张"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        # ⚠️ M1：落库列名是 `Order.total_price`，不是前端显示名「接单价」
        row = self._only(
            ORDER_NO, "order.change_taker_price", log_type=10,
            summary=(
                "【cs1】修改了订单接单价格（原订单价格：199.50元，修改后价格：88.00元）"
                f"（订单 {ORDER_NO}）"
            ),
        )
        self.assertEqual(row.field_name, "total_price")
        self.assertEqual(row.from_value, "199.50")
        self.assertEqual(row.to_value, "88.00")
        self.assertEqual(row.reason, "客户砍价")
        # R20：留痕操作人取认证态；请求体里的 "客服小张" 是可伪造的，不得出现在留痕里。
        # （业务字段 `Order.updated_by` 仍取请求体值 —— 见 test_order_api 的对应断言。）
        self.assertEqual(row.actor, "cs1")
        self.assertNotIn("客服小张", row.actor)

    def test_same_price_writes_zero_rows_with_positive_control(self):
        """R27.2 同口径（第四处）：**新价 == 旧价** ⇒ 零行，不得写「88.00 → 88.00」的假话。

        判别力：去掉 `price` 里的 `new_price != old_price` 判据（无条件 `record`）⇒ 第一条
        零行断言变红。**方向配对**：先证「同价 ⇒ 零行」，再在**同一个端点**上真改一次价
        （阳性对照）证「不同价 ⇒ 恰好一行且 before/after 正确」——零行因此不是「端点坏了」。
        """
        self._order(total_price=Decimal("88.00"))

        # 方向 ①：同价（88.00 → 88.00）。端点仍回 0（`save` 照做，只跳过留痕）。
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/price/",
            {"total_price": "88.00", "actor": "客服小张"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self._assert_zero_new_rows(ORDER_NO)

        # 方向 ②（阳性对照）：同一个端点真改一次 ⇒ 恰好一行，前后值写字面值。
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/price/",
            {"total_price": "99.00", "actor": "客服小张"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        row = self._only(
            ORDER_NO, "order.change_taker_price", log_type=10,
            summary=(
                "【cs1】修改了订单接单价格（原订单价格：88.00元，修改后价格：99.00元）"
                f"（订单 {ORDER_NO}）"
            ),
        )
        self.assertEqual(row.from_value, "88.00")
        self.assertEqual(row.to_value, "99.00")

    def test_retrieve_writes_order_view_credentials(self):
        self._order()
        body = self.cs.get(f"/api/business/orders/{ORDER_NO}/").json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "order.view_credentials", log_type=10,
            summary=f"【cs1】查看订单详情：{ORDER_NO}",
        )
        self.assertEqual(row.actor, "cs1")

    # ── 反向控制（spec §10.2 的「没留痕」断言）──────────────────
    def test_rejected_transition_leaves_zero_rows(self):
        # 阳性对照：同一端点对另一张单成功时**会**写行 ⇒ 下面的 0 不是「表不可达」
        other = self._order(
            order_no="20260921000000000002", status=OrderStatus.UNASSIGNED
        )
        self.cs.post(
            f"/api/business/orders/{other.order_no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        self.assertEqual(self._rows(other.order_no, "order.transition").count(), 1)

        self._order(status=OrderStatus.UNASSIGNED)
        # ⚠️ 靶子（D-0147 / R8）：`PENDING_REVIEW` 出边在状态机放开后**合法**，
                #    R8 之下唯一仍非法的形状是**自环**——所以造拒绝用自环。
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/transition/",
            {"target": OrderStatus.UNASSIGNED},
            content_type="application/json",
        ).json()
        self.assertNotEqual(body["code"], 0)
        self._assert_zero_new_rows(ORDER_NO)

    def test_rejected_price_change_leaves_zero_rows(self):
        # 阳性对照：合法的改价**会**写行
        other = self._order(
            order_no="20260921000000000002", total_price=Decimal("199.50")
        )
        self.cs.post(
            f"/api/business/orders/{other.order_no}/price/",
            {"total_price": "88.00", "actor": "客服小张"},
            content_type="application/json",
        )
        self.assertEqual(self._rows(other.order_no, "order.change_taker_price").count(), 1)

        self._order(total_price=Decimal("199.50"))
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/price/",
            {"total_price": "abc", "actor": "客服小张"},
            content_type="application/json",
        ).json()
        self.assertNotEqual(body["code"], 0)
        self._assert_zero_new_rows(ORDER_NO)

    def test_failed_claim_leaves_zero_rows(self):
        # 阳性对照：可抢的那张单真的会写行
        ok_order = self._order(
            order_no="20260921000000000002", status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        self._perf(ok_order)
        ok_taker = make_logged_in_client(
            username="taker_ok", groups=("代练员",), nodes=TAKER_WRITE_NODES
        )
        # **夹具补齐**——两个抢单方都要显式持有该单的游戏，
        # 否则阳性对照会以业务码 1000 假红、负向那条也不是被「不在大厅」拒的。
        grant_games(User.objects.get(username="taker_ok"), ok_order.game)
        ok_body = ok_taker.post(
            f"/api/business/hall/{ok_order.order_no}/claim/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(ok_body["code"], 0, ok_body)
        self.assertEqual(self._rows(ok_order.order_no, "order.claim").count(), 1)

        # 不在大厅的单 ⇒ 抢单失败 ⇒ 零行
        order = self._order(status=OrderStatus.UNASSIGNED, hall_price=Decimal("88.00"))
        self._perf(order)
        taker = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=TAKER_WRITE_NODES
        )
        # **夹具补齐**——该请求方也要显式持有游戏，
        # 这样它被拒的原因是「不在大厅」而不是「没有游戏权限」。
        grant_games(User.objects.get(username="taker1"), order.game)
        body = taker.post(
            f"/api/business/hall/{ORDER_NO}/claim/", {},
            content_type="application/json",
        ).json()
        self.assertNotEqual(body["code"], 0)
        self._assert_zero_new_rows(ORDER_NO)


# ══════════════════════════════════════════════════════════════════
# 绩效域（spec §5.2）
# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
# ⭐ 订单日志人名线（spec §3-A2 / 判据 4–5）
# ══════════════════════════════════════════════════════════════════
class PersonNameDisplayTests(_WritePathBase):
    """人名类 `target_name` 落**账号名称**（昵称优先、空串回落登录账号），⛔ 不再是登录账号。

    ⚠️ **夹具纪律**（spec §5）：昵称与登录账号必须**不同形**——两者相同时
    「正文里的人名走的是昵称还是账号」这条判据会**假绿**（本类为此专门写死两个常量）。
    ⚠️ 判据是两向的：① 昵称**在**正文里 ② 登录账号**不在**正文里。
    只断①的实现（比如把两个名字都写进去）在②上红。
    """

    #: 夹具：登录账号与昵称**不同形**（⛔ 别改成同一个值）。
    TAKER_ACCOUNT = "taker-001"
    TAKER_NICK = "甲昵称"

    def _nicknamed_taker(self):
        user = User.objects.create_user(
            username=self.TAKER_ACCOUNT, first_name=self.TAKER_NICK
        )
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权。
        grant_games(user, self.game)
        return user

    def test_assign_order_stores_and_renders_the_account_name(self):
        """判据 4：`assign_order` 落库 `target_name == 昵称`，正文含昵称、**不含账号**。"""
        order = self._order(status=OrderStatus.UNASSIGNED, hall_price=Decimal("88.00"))
        self._perf(order)
        taker = self._nicknamed_taker()
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._rows(ORDER_NO, "performance.assign").get()
        self.assertEqual(row.target_name, self.TAKER_NICK)
        self.assertNotEqual(row.target_name, self.TAKER_ACCOUNT)
        # `target_id` 仍是**主键**（身份维度不受显示口径影响）
        self.assertEqual(row.target_id, str(taker.id))
        self.assertEqual(
            row.summary, f"cs1将订单指派给{self.TAKER_NICK}，发单价：88.00（订单 {ORDER_NO}）"
        )
        self.assertIn(self.TAKER_NICK, row.summary)
        self.assertNotIn(self.TAKER_ACCOUNT, row.summary)
        # 操作人（`performance.assign` 的模板自带 `{actor_name}`）仍**恰好一次**
        self.assertEqual(row.summary.count("cs1"), 1, row.summary)

    def test_clear_taker_stores_and_renders_the_account_name(self):
        """判据 5（`clear_taker`）：正文里的**前接单人**是昵称，⛔ 不是登录账号。"""
        order = self._order(status=OrderStatus.UNASSIGNED)
        taker = self._nicknamed_taker()
        self._perf(order, user=taker)
        order.status = OrderStatus.IN_PROGRESS
        order.save(update_fields=["status"])

        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/clear-taker/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._rows(ORDER_NO, "order.clear_taker").get()
        self.assertEqual(row.target_name, self.TAKER_NICK)
        self.assertEqual(
            row.summary,
            f"【cs1】清除了订单 {ORDER_NO} 的接单人（原接单人：{self.TAKER_NICK}）",
        )
        self.assertNotIn(self.TAKER_ACCOUNT, row.summary)
        # ⚠️ 两个「人」不许混：操作人（cs1）在前缀里、被清掉的前接单人在正文里，各一次。
        self.assertEqual(row.summary.count("cs1"), 1, row.summary)
        self.assertEqual(row.summary.count(self.TAKER_NICK), 1, row.summary)


class NoBackfillTests(_WritePathBase):
    """⭐ 判据 6（spec §1 的裁定 1 / §3-A1 末尾）：**历史日志不回填**。

    只改渲染规则与写入点 ⇒ 旧机器行的 `summary` 一个字都不许动。
    判别力：把「回填」实现在 `record()` 里（或任何写入路径上顺手重渲染同单历史行）⇒ 本用例红；
    写成数据迁移 ⇒ 由 `makemigrations --check`（本线不应有迁移）与这条用例双面兜住。
    """

    #: 旧机器行的 `summary`：**修前**的渲染形状（没有 `【来源】` 前缀）。
    OLD_SUMMARY = f"将订单 {ORDER_NO} 的状态从未分配改为待抢单"

    def test_an_old_machine_row_summary_is_never_rewritten_by_new_writes(self):
        self._order(status=OrderStatus.UNASSIGNED)
        # 预置一条「旧」机器行：直接建行（⛔ 不走 `record()`，因为它现在会加前缀）。
        old = OperationLog.objects.create(
            log_type=10,
            action="order.transition",
            actor="系统同步",
            actor_id=None,
            order_no=ORDER_NO,
            summary=self.OLD_SUMMARY,
            field_name="status",
            from_value="未分配",
            to_value="待抢单",
        )
        # 跑一段**新**写入：同一张单、同一个动作（最容易触发「顺手重渲染」的那条路径）。
        body = self.cs.post(
            f"/api/business/orders/{ORDER_NO}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        old.refresh_from_db()
        self.assertEqual(old.summary, self.OLD_SUMMARY, "旧机器行被回填了")
        self.assertNotIn("【", old.summary)

        # 阳性对照：新写入的行带操作人，且没有回填旧行。
        new_rows = list(
            OperationLog.objects.filter(order_no=ORDER_NO, action="order.transition")
            .exclude(pk=old.pk)
        )
        self.assertEqual(len(new_rows), 1, [r.summary for r in new_rows])
        self.assertEqual(
            new_rows[0].summary,
            f"cs1将订单 {ORDER_NO} 的状态从未分配改为待抢单",
        )


#: ⭐ 订单日志人名线（spec §3-A2）：绩效归属人的**昵称**（与登录账号**不同形**）。
#: ⚠️ 两者相同时「`target_name` 写的是账号名称还是登录账号」这条判据会**假绿**（spec §5 的纪律）。
TAKER_NICKNAME = "测试昵称打手壬"


class PerformanceWritePathLogTests(_WritePathBase):
    def setUp(self):
        super().setUp()
        # ⭐ 给归属人一个**与登录账号不同形**的昵称（见 `TAKER_NICKNAME` 的理由）。
        self.taker = User.objects.create_user(
            username="taker1", first_name=TAKER_NICKNAME
        )
        # ⭐ 打手来源钉成「打手 App 注册」（业务方口径，D-0279）：自动结账
        #    **只对 App 注册打手生效**。下面的审核通过用例依赖「完成即自动入账」留下的
                #    那一条 `performance.settle` 留痕 ⇒ 来源必须是 `app`，
        #    否则那条留痕不再产生、本文件的两处计数断言会失真。
        StaffProfile.objects.create(
            user=self.taker, commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
            account_source=AccountSource.APP,
        )

    def _perf_with_taker(self, **overrides):
        order = self._order(status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"))
        return self._perf(order, user=self.taker, **overrides)

    def test_adjust_writes_one_row_per_changed_field(self):
        """**R9 的回归**：两个被改字段 ⇒ **两行**，各自只命名自己的字段。

        判别力：把 `changes` 合成一行四值 ⇒ 本用例的 `len(rows) == 2` 与两条
        `assertIn(field_label, row.summary)` 双双变红。
        """
        perf = self._perf_with_taker()
        self._grant(self.cs_user, "override_taker_price")
        body = self.cs.post(
            f"/api/business/performances/{perf.id}/adjust/",
            {"taker_price": "66.00", "deduct_mode": "1", "reason": "议价"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        rows = list(self._rows(ORDER_NO, "performance.adjust").order_by("id"))
        self.assertEqual(len(rows), 2, [r.summary for r in rows])
        self.assertEqual([r.field_name for r in rows], ["taker_price", "deduct_mode"])
        self.assertEqual([r.log_type for r in rows], [20, 20])
        self.assertEqual(rows[1].from_value, "")
        self.assertEqual(rows[1].to_value, "1")
        # ⭐ 订单日志人名线（spec §3-A2）：正文里的人名是**账号名称**（昵称），⛔ 不是登录账号。
        self.assertEqual(
            [r.summary for r in rows],
            [
                f"【cs1】修改了{TAKER_NICKNAME}的接单价覆盖值（原，新66.00）（订单 {ORDER_NO}）",
                f"【cs1】修改了{TAKER_NICKNAME}的扣点模式（原未设置，新扣除固定金额）（订单 {ORDER_NO}）",
            ],
        )
        # 反向：登录账号**不许**出现在正文里（否则上面两条在「同名夹具」下是假绿）
        for row in rows:
            self.assertNotIn("taker1", row.summary)
        # 每行只出现自己那个字段的中文名（R9：一行放不下两对值）
        self.assertIn("接单价覆盖值", rows[0].summary)
        self.assertNotIn("扣点模式", rows[0].summary)
        self.assertIn("扣点模式", rows[1].summary)
        self.assertNotIn("接单价覆盖值", rows[1].summary)
        for row in rows:
            self.assertEqual(row.actor, "cs1")
            # Fix 2：绩效域这一侧也必须把 `actor_id` 落成认证态主键
            self.assertEqual(row.actor_id, self.cs_user.pk)
            self.assertEqual(row.reason, "议价")
            # ⭐ 订单日志人名线（spec §3-A2）：`target_name` 落**账号名称**（昵称），不是登录账号。
            self.assertEqual(row.target_name, TAKER_NICKNAME)
            self.assertNotEqual(row.target_name, "taker1")
        self.assertFalse(AuditLog.objects.filter(order_no=ORDER_NO).exists())

    def test_adjust_does_not_write_rows_when_nothing_changed(self):
        perf = self._perf_with_taker()
        self._grant(self.cs_user, "override_taker_price")
        url = f"/api/business/performances/{perf.id}/adjust/"
        first = self.cs.post(
            url, {"taker_price": "66.00"}, content_type="application/json"
        ).json()
        self.assertEqual(first["code"], 0)
        # 阳性对照：真的改了值 ⇒ 一行
        self.assertEqual(self._rows(ORDER_NO, "performance.adjust").count(), 1)
        # 同值再来一次 ⇒ 不新增
        second = self.cs.post(
            url, {"taker_price": "66.00"}, content_type="application/json"
        ).json()
        self.assertEqual(second["code"], 0)
        self.assertEqual(self._rows(ORDER_NO, "performance.adjust").count(), 1)

    def test_review_writes_a_single_performance_review_row(self):
        """`/review` 是**一个**业务动作 ⇒ 新表**一行**（旧的 `REVIEW` + `STATUS_CHANGE` 两行
        由 `performance.review` 这一行取代；审核结果进 `to_value`）。"""
        order = self._order(status=OrderStatus.PENDING_REVIEW, hall_price=Decimal("88.00"))
        perf = self._perf(
            order, user=self.taker, bonus_amount=Decimal("17.60"),
            review_status=ReviewStatus.PENDING,
        )
        reviewer = make_logged_in_client(
            username="reviewer1", groups=("客服主管",),
            nodes=REVIEWER_WRITE_NODES,
        )
        self._grant(User.objects.get(username="reviewer1"), "review_performance")
        # ⭐ `/review` 走 `_base_queryset` ⇒ 审核人
        # 必须显式持有这张单的游戏，否则 `get_object()` 找不到绩效（200 + 1002）。
        grant_games(User.objects.get(username="reviewer1"), order.game)

        body = reviewer.post(
            f"/api/business/performances/{perf.id}/review/",
            {"approved": True, "comment": "OK"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._only(
            ORDER_NO, "performance.review", log_type=20,
            summary=f"reviewer1审核了订单 {ORDER_NO} 的绩效：已通过，备注：OK",
        )
        self.assertEqual(row.field_name, "review_status")
        self.assertEqual(row.from_value, "待审核")
        self.assertEqual(row.to_value, "已通过")
        self.assertEqual(row.reason, "OK")
        self.assertEqual(row.actor, "reviewer1")
        # ⭐ 绩效出钱包：验收**不再**自动入账 ⇒ 这一跳不写 `performance.settle`；
        #    审核动作仍恰好 1 行，本单总数是 1（`D-0294` / spec
        #    `2026-10-23-taker-income-and-early-settlement.md` §3.1）。
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=ORDER_NO, action="performance.settle"
            ).count(),
            0,
        )
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO).count(), 1)

    def test_rejected_review_records_the_rejection(self):
        order = self._order(status=OrderStatus.PENDING_REVIEW, hall_price=Decimal("88.00"))
        perf = self._perf(
            order, user=self.taker, review_status=ReviewStatus.PENDING,
        )
        reviewer = make_logged_in_client(
            username="reviewer1", groups=("客服主管",),
            nodes=REVIEWER_WRITE_NODES,
        )
        self._grant(User.objects.get(username="reviewer1"), "review_performance")
        grant_games(User.objects.get(username="reviewer1"), order.game)
        body = reviewer.post(
            f"/api/business/performances/{perf.id}/review/",
            {"approved": False, "comment": "金额不对"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        row = self._only(
            ORDER_NO, "performance.review", log_type=20,
            summary=f"reviewer1审核了订单 {ORDER_NO} 的绩效：已驳回，备注：金额不对",
        )
        self.assertEqual(row.to_value, "已驳回")


# ══════════════════════════════════════════════════════════════════
# 账号域（spec §5.3）
# ══════════════════════════════════════════════════════════════════
#: 新增载荷必须带齐的六个「必填，照抄」字段（R13）。与 `test_account_api.py` 的
#: 同名常量同源，但**刻意各写一份**：跨测试模块 import 会把两个文件的夹具耦合成
#: 「改一处、另一处静默错位」。
ACCOUNT_CREATE_PROFILE = {
    "display_name": "小王",
    "commission_value": "10.00",
    "grab_limit_dl": 0,
    "grab_limit_pw": 0,
    "hired_on": "2026-01-02",
    "deposit_amount": "0.00",
}


class AccountWritePathLogTests(TenantTestCase):
    """5 条写路径 + 3 类派生行，全部断言**数据库行**上的字面值。

    ⚠️ `TenantTestCase` **不会**自动 `seed_tenant`（`Group` / `admin` 都不存在），
    而账号线的职位是 `Group` 行 ⇒ `setUpClass` 里显式灌一次（同 `test_account_api.py`）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.main_game = Game.objects.create(name="王者荣耀")

    def setUp(self):
        self.admin = make_logged_in_client(username="log-admin")
        self.admin_user = User.objects.get(username="log-admin")
        self.admin_user.groups.add(Group.objects.get(name="系统管理员"))
        self.target = self._make_account(
            "log-target", role="客服", games=[self.main_game]
        )

    # ── 夹具 ─────────────────────────────────────────────────────
    def _make_account(self, username, *, role="客服", games=()):
        user = User.objects.create_user(username=username, password="pw-123456")
        if role:
            user.groups.add(Group.objects.get(name=role))
        profile = StaffProfile.objects.create(user=user)
        if games:
            profile.games.set([game.pk for game in games])
        return user

    def _create_payload(self, username, *, role="客服", games=None, **extra):
        return {
            **ACCOUNT_CREATE_PROFILE,
            "username": username,
            "role": role,
            "games": [g.pk for g in (games if games is not None else [self.main_game])],
            **extra,
        }

    # ── 断言助手 ─────────────────────────────────────────────────
    def _rows(self, action):
        return OperationLog.objects.filter(action=action)

    def _summaries(self, action):
        return sorted(self._rows(action).values_list("summary", flat=True))

    def _only(self, action, *, log_type, summary):
        """钉住「这个动作恰好一行」，并逐项断言字面值。返回那一行。"""
        rows = list(self._rows(action))
        self.assertEqual(
            len(rows), 1,
            f"期望 {action} 恰好一行，实际 {len(rows)} 行："
            f"{[(r.action, r.summary) for r in OperationLog.objects.all()]}",
        )
        row = rows[0]
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, log_type)
        self.assertEqual(row.summary, summary)
        return row

    # ── #2 新增 ──────────────────────────────────────────────────
    def test_create_writes_user_create_and_one_row_per_game(self):
        body = self.admin.post(
            "/api/accounts/", data=self._create_payload("fresh-1"),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        new_id = body["data"]["account"]["id"]

        row = self._only(
            "user.create", log_type=30,
            summary=f"【log-admin】新增用户(ID：{new_id}，用户名：fresh-1)的资料",
        )
        # 模板插值的名字必须真的在句子里（缺 `target_name` 会渲染成空名字，
        # 而「行存在」的断言看不出来）。
        self.assertIn("fresh-1", row.summary)
        self.assertEqual(row.target_id, str(new_id))
        self.assertEqual(row.target_name, "fresh-1")
        self.assertEqual(row.actor, "log-admin")
        self.assertEqual(row.actor_id, self.admin_user.pk)

        # M4：新增账号也按选中的游戏**逐条**写（`_persist_account` 用同一个 `games.set()`）。
        game_rows = list(self._rows("user.game_permission_add"))
        self.assertEqual([r.summary for r in game_rows], ["【log-admin】新增用户游戏权限：王者荣耀"])
        self.assertEqual(game_rows[0].log_type, 30)
        self.assertEqual(game_rows[0].game_name, "王者荣耀")
        self.assertEqual(OperationLog.objects.count(), 2)

    def test_create_with_two_games_writes_two_game_permission_rows(self):
        other = Game.objects.create(name="英雄联盟")
        body = self.admin.post(
            "/api/accounts/",
            data=self._create_payload("fresh-2", games=[self.main_game, other]),
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        self._only(
            "user.create", log_type=30,
            summary=(
                f"【log-admin】新增用户(ID：{body['data']['account']['id']}，用户名：fresh-2)的资料"
            ),
        )
        self.assertCountEqual(
            self._summaries("user.game_permission_add"),
            ["【log-admin】新增用户游戏权限：王者荣耀", "【log-admin】新增用户游戏权限：英雄联盟"],
        )
        self.assertEqual(OperationLog.objects.count(), 3)

    # ── #4 编辑（`partial_update` 只是转调，**不得**写两行）──────
    def test_update_writes_user_update_and_a_patch_is_one_row(self):
        body = self.admin.patch(
            f"/api/accounts/{self.target.pk}/",
            data={"display_name": "改名了"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._only(
            "user.update", log_type=30,
            summary=f"【log-admin】编辑用户(ID：{self.target.pk}，用户名：log-target)的资料",
        )
        self.assertIn("log-target", row.summary)
        self.assertEqual(row.actor_id, self.admin_user.pk)
        # ⚠️ I6：`.partial_update` 转调 `.update`，一次 `PATCH` **只能一行**。
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_update_rename_writes_the_extra_user_rename_row(self):
        body = self.admin.patch(
            f"/api/accounts/{self.target.pk}/",
            data={"username": "log-target-2"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        self._only(
            "user.update", log_type=30,
            summary=f"【log-admin】编辑用户(ID：{self.target.pk}，用户名：log-target-2)的资料",
        )
        rename = self._only(
            "user.rename", log_type=30,
            summary="【log-admin】已将登录账号由 log-target 改为 log-target-2",
        )
        self.assertEqual(rename.from_value, "log-target")
        self.assertEqual(rename.to_value, "log-target-2")
        self.assertEqual(OperationLog.objects.count(), 2)

    def test_update_role_change_writes_the_extra_role_change_row(self):
        body = self.admin.patch(
            f"/api/accounts/{self.target.pk}/",
            data={"role": "代练员"}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        self._only(
            "user.update", log_type=30,
            summary=f"【log-admin】编辑用户(ID：{self.target.pk}，用户名：log-target)的资料",
        )
        role_row = self._only(
            "user.role_change", log_type=30,
            summary="【log-admin】更改了log-target的职位为：代练员",
        )
        self.assertEqual(role_row.target_name, "log-target")
        self.assertEqual(role_row.to_value, "代练员")

    def test_update_game_diff_writes_one_add_and_one_remove_row(self):
        other = Game.objects.create(name="英雄联盟")
        body = self.admin.patch(
            f"/api/accounts/{self.target.pk}/",
            data={"games": [other.pk]}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        self._only(
            "user.update", log_type=30,
            summary=f"【log-admin】编辑用户(ID：{self.target.pk}，用户名：log-target)的资料",
        )
        added = self._only(
            "user.game_permission_add", log_type=30,
            summary="【log-admin】新增用户游戏权限：英雄联盟",
        )
        self.assertEqual(added.game_name, "英雄联盟")
        removed = self._only(
            "user.game_permission_remove", log_type=30,
            summary="【log-admin】删除用户游戏权限：王者荣耀",
        )
        self.assertEqual(removed.game_name, "王者荣耀")
        self.assertEqual(OperationLog.objects.count(), 3)

    def test_update_without_a_real_change_writes_no_derived_rows(self):
        """派生行按**真实差集**写，不按请求载荷。

        判别力：实现若照请求载荷写，这里会多出 1 条 `user.rename`、1 条 `user.role_change`
        与 1 条 `user.game_permission_add`（游戏集合没变）⇒ 四条 `assertFalse` 全红。
        """
        body = self.admin.patch(
            f"/api/accounts/{self.target.pk}/",
            data={
                "username": "log-target",
                "role": "客服",
                "games": [self.main_game.pk],
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        self.assertEqual(
            self._summaries("user.update"),
            [f"【log-admin】编辑用户(ID：{self.target.pk}，用户名：log-target)的资料"],
        )
        self.assertFalse(self._rows("user.rename").exists())
        self.assertFalse(self._rows("user.role_change").exists())
        self.assertFalse(self._rows("user.game_permission_add").exists())
        self.assertFalse(self._rows("user.game_permission_remove").exists())

    def test_empty_patch_writes_zero_rows(self):
        """空载荷 PATCH `{}` 是**纯 no-op**（`_persist_account` 的早返回）⇒ 留痕不点火。"""
        body = self.admin.patch(
            f"/api/accounts/{self.target.pk}/", data={},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OperationLog.objects.count(), 0)

        # 阳性对照：同一目标、同一 verb，非空 PATCH 会写行 ⇒ 上面的 0 不是「表不可达」。
        self.assertEqual(
            self.admin.patch(
                f"/api/accounts/{self.target.pk}/", data={"display_name": "有事发生"},
                content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertEqual(self._rows("user.update").count(), 1)

    # ── #5 单个启停 ─────────────────────────────────────────────
    def test_status_writes_user_status_with_both_labels(self):
        body = self.admin.post(
            f"/api/accounts/{self.target.pk}/status/", {"is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._only(
            "user.status", log_type=30,
            summary="【log-admin】更改了用户状态，将log-target的子账户状态从有效变成禁用",
        )
        self.assertIn("log-target", row.summary)
        self.assertEqual(row.from_value, "有效")
        self.assertEqual(row.to_value, "禁用")
        self.assertEqual(row.target_name, "log-target")
        self.assertEqual(row.target_id, str(self.target.pk))
        self.assertEqual(row.actor, "log-admin")
        self.assertEqual(row.actor_id, self.admin_user.pk)
        self.assertFalse(User.objects.get(pk=self.target.pk).is_active)

    def test_status_to_the_same_value_writes_zero_rows(self):
        """幂等 `set`：目标已是该状态时**没有状态变化** ⇒ 不留「从有效变成有效」的假行。

        `_apply_status` 的口径本来就是「重复发同一条请求是 no-op」（spec §3.3 注，
        `test_account_api.py::AccountStatusTests.test_disable_single_is_idempotent_set`）——
        它仍会执行一次幂等 `save()`（响应里的 `updated` 照旧列出该账号），但审计行只在
        **状态真的变了**时写，与 `order.update` 的「同值不留噪音行」同一纪律。
        """
        body = self.admin.post(
            f"/api/accounts/{self.target.pk}/status/", {"is_active": True},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OperationLog.objects.count(), 0)

        # 阳性对照：改成**另一个**状态就会写一行。
        self.assertEqual(
            self.admin.post(
                f"/api/accounts/{self.target.pk}/status/", {"is_active": False},
                content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertEqual(self._rows("user.status").count(), 1)

    def test_rejected_self_lockout_writes_zero_rows(self):
        """被拒的启停请求不得留痕（评审 Critical 的反向控制）。

        停用**自己**走 `check_admin_invariant` 的 ③，返回 **1004**，账号不落库。
        判别力：成功判据写成 `response.data is not None` 时它恒为真——`api_response()` 交出去的
        是**整个信封 dict**，`.data` 永远非 None ⇒ 这条请求会留下一条「从有效变成禁用」的假行，
        下面 `OperationLog.objects.count() == 1` 变红。
        """
        # 阳性对照先跑：同一端点对**别人**成功时确实写一行 ⇒ 下面的 0 不是「表不可达」。
        ok = self.admin.post(
            f"/api/accounts/{self.target.pk}/status/", {"is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0, ok)
        self.assertEqual(self._rows("user.status").count(), 1)

        rejected = self.admin.post(
            f"/api/accounts/{self.admin_user.pk}/status/", {"is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(rejected["code"], 1004, rejected)
        self.assertTrue(User.objects.get(pk=self.admin_user.pk).is_active)
        # 仍然只有阳性对照那一行——被拒的请求一行都不许写。
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_status_rolls_back_the_state_and_the_row_when_record_fails(self):
        """R24：`.status` 原先**没有**事务边界 ⇒ 补一层，把状态写与 `record(...)` 收在一起。

        判别力：去掉 `status()` 里那层 `with transaction.atomic():` ⇒ `_apply_status` 的
        `atomic()` 出块即释放 SAVEPOINT（测试库的外层事务仍在），`record` 再抛异常时
        状态已是新值、留痕也已落一行 ⇒ 末尾两条断言双双变红。
        """
        seen = {}

        def _boom(**kwargs):
            real_record(**kwargs)   # 哨兵：先真的写进去，再证明它随事务回滚
            seen["active"] = User.objects.get(pk=self.target.pk).is_active
            seen["rows"] = OperationLog.objects.filter(action="user.status").count()
            raise RuntimeError("留痕写不进去")

        with mock.patch("apps.accounts.views.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.admin.post(
                    f"/api/accounts/{self.target.pk}/status/", {"is_active": False},
                    content_type="application/json",
                )

        self.assertFalse(seen["active"])           # 状态真的被改过（同一事务内读得到）
        self.assertEqual(seen["rows"], 1)          # 留痕也真的写过一行
        self.assertTrue(User.objects.get(pk=self.target.pk).is_active)   # ① 回滚
        self.assertEqual(OperationLog.objects.count(), 0)                # ② 留痕一并回滚

    # ── #6 批量启停 ─────────────────────────────────────────────
    def test_status_batch_writes_one_row_with_the_changed_count(self):
        other = self._make_account("log-target-2", role="客服", games=[self.main_game])
        body = self.admin.post(
            "/api/accounts/status/",
            {"ids": [self.target.pk, other.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._only(
            "user.status_batch", log_type=30,
            summary="【log-admin】更改了用户状态，将2个子账户批量改为禁用",
        )
        self.assertEqual(row.from_value, "2")
        self.assertEqual(row.to_value, "禁用")
        self.assertEqual(row.actor, "log-admin")
        self.assertEqual(row.actor_id, self.admin_user.pk)

    def test_status_batch_counts_only_the_accounts_it_actually_changed(self):
        """`{n}` 取**真的变了**的账号数：批量里混着已停用的行时不得把它算进去。"""
        User.objects.filter(pk=self.target.pk).update(is_active=False)
        other = self._make_account("log-target-2", role="客服", games=[self.main_game])
        body = self.admin.post(
            "/api/accounts/status/",
            {"ids": [self.target.pk, other.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self._only(
            "user.status_batch", log_type=30,
            summary="【log-admin】更改了用户状态，将1个子账户批量改为禁用",
        )

    def test_status_batch_without_a_real_change_writes_zero_rows(self):
        User.objects.filter(pk=self.target.pk).update(is_active=False)
        body = self.admin.post(
            "/api/accounts/status/",
            {"ids": [self.target.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OperationLog.objects.count(), 0)

        # 阳性对照：同一批量入口改成启用 ⇒ 写一行。
        self.assertEqual(
            self.admin.post(
                "/api/accounts/status/", {"ids": [self.target.pk], "is_active": True},
                content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertEqual(self._rows("user.status_batch").count(), 1)

    def test_rejected_status_batch_writes_zero_rows(self):
        """整批被拒（1003）时一行都不许写（评审 Critical 的反向控制）。

        把租户里**全部**活跃管理员（含请求者自己）一起停用 ⇒ `_apply_status` 整批拒绝，
        一行都不落库（`test_account_admin_guard.py::
        test_batch_disabling_every_active_admin_is_rejected_as_a_whole` 钉住落库事实）。
        判别力：成功判据写成 `response.data is not None` 时它恒为真 ⇒ 这里会留下一条
        「将 2 个子账户批量改为禁用」的假行（`changed` 非空），`count() == 1` 变红。
        """
        # 阳性对照先跑：批量停用两个**非管理员** ⇒ 一行。
        other_a = self._make_account("log-target-2", role="客服", games=[self.main_game])
        other_b = self._make_account("log-target-3", role="客服", games=[self.main_game])
        ok = self.admin.post(
            "/api/accounts/status/",
            {"ids": [other_a.pk, other_b.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0, ok)
        self.assertEqual(self._rows("user.status_batch").count(), 1)

        seeded_admin = User.objects.get(username="admin")   # seed_tenant 的活跃超管
        rejected = self.admin.post(
            "/api/accounts/status/",
            {"ids": [seeded_admin.pk, self.admin_user.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(rejected["code"], 1003, rejected)
        self.assertTrue(User.objects.get(pk=seeded_admin.pk).is_active)
        self.assertTrue(User.objects.get(pk=self.admin_user.pk).is_active)
        # 仍然只有阳性对照那一行。
        self.assertEqual(OperationLog.objects.count(), 1)

    # ── #7 重置密码 ─────────────────────────────────────────────
    def test_reset_password_writes_user_reset_password(self):
        body = self.admin.post(
            f"/api/accounts/{self.target.pk}/reset-password/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = self._only(
            "user.reset_password", log_type=30,
            summary="【log-admin】已将log-target的登录密码重置为初始密码",
        )
        self.assertEqual(row.target_id, str(self.target.pk))
        self.assertEqual(row.target_name, "log-target")
        self.assertEqual(row.actor, "log-admin")
        self.assertEqual(row.actor_id, self.admin_user.pk)
        # 正向对照：口令真的被重置了（留痕不是这条路径唯一的副作用）。
        self.assertTrue(User.objects.get(pk=self.target.pk).check_password("123456"))


# ══════════════════════════════════════════════════════════════════
# 权限域（spec §5.4）
# ══════════════════════════════════════════════════════════════════
RBAC_BASE = "/api/role-authorities/"


class RbacRoleWritePathLogTests(TenantTestCase):
    """`RoleAuthorityViewSet` 的 4 条写路径 × 留痕列（spec §5.4 / §5.9 / §10.2）。

    夹具与 `test_rbac_api.py` 同口径：**超管**做调用方（`has_node` 对超管短路为真），
    本类测的是留痕列，不该被权限拒绝干扰；权限门本身由 `test_rbac_api.py` 的
    `RoleAuthorityPermissionGateTests` 逐动作钉住。

    三条纪律同本文件开头：断言打在**数据库行**上（E-0009）；期望值手写字面值——
    `log_type` 写 `40`（**不引用 `LogType.SYSTEM`**）、`action` 写 code、`summary` 写
    渲染后的整句（E-0024）；失败路径断言**零行**。
    """

    def setUp(self):
        # `TenantTestCase` **不跑** `seed_tenant`（R5）⇒ 权限目录要自己灌，
        # 否则 `_resolve_nodes` 认不出 `order.list` 这类 code。
        seed_catalog()
        # ⚠️ 判据统一后不能"登录后手工置 `is_superuser`"：管理员判据是
        # 「持『系统管理员』职位」，裸超管过不了节点门；夹具的 `is_superuser=True` 会顺手给组。
        self.client = make_logged_in_client(
            username="log-rbac-admin", password="pw-123456", is_superuser=True
        )
        # 留痕的 `actor_id` 断言要拿这个对象（与夹具创建的是同一行）。
        self.admin = User.objects.get(username="log-rbac-admin")

    # ── 夹具 ─────────────────────────────────────────────────────
    def _create(self, name="日志角色", codes=("order.list",), remark="原备注"):
        return self.client.post(
            RBAC_BASE,
            {"name": name, "remark": remark, "permission_codes": list(codes)},
            content_type="application/json",
        )

    def _role(self, name="日志角色", **kwargs):
        response = self._create(name=name, **kwargs)
        self.assertEqual(response.json()["code"], 0, response.content)
        return Role.objects.get(name=name)

    def _member(self, role, username):
        """往角色同名 Group 里塞一个**活跃非超管**成员（改名 / 删除守卫的扣减项）。"""
        user = User.objects.create_user(username=username, password="pw-123456")
        user.groups.add(role.group)
        return user

    # ── 断言助手 ─────────────────────────────────────────────────
    def _rows(self, action):
        return OperationLog.objects.filter(action=action)

    def _only(self, action, *, log_type, summary):
        """钉住「这个动作恰好一行」，并逐项断言字面值。返回那一行。"""
        rows = list(self._rows(action))
        self.assertEqual(
            len(rows), 1,
            f"期望 {action} 恰好一行，实际 {len(rows)} 行："
            f"{[(r.action, r.summary) for r in OperationLog.objects.all()]}",
        )
        row = rows[0]
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, log_type)
        self.assertEqual(row.summary, summary)
        return row

    # ── #1 新增 ──────────────────────────────────────────────────
    def test_create_writes_system_role_create(self):
        body = self._create("日志角色").json()
        self.assertEqual(body["code"], 0, body)
        role = Role.objects.get(name="日志角色")

        row = self._only("system.role_create", log_type=40, summary="【log-rbac-admin】新增角色职位：日志角色")
        # 模板插值的名字必须真的在句子里（缺 `target_name` 会渲染成空名字，
        # 而「行存在」的断言看不出来）。
        self.assertIn("日志角色", row.summary)
        self.assertEqual(row.target_kind, "role")
        self.assertEqual(row.target_id, str(role.pk))
        self.assertEqual(row.target_name, "日志角色")
        self.assertEqual(row.actor, "log-rbac-admin")
        self.assertEqual(row.actor_id, self.admin.pk)
        self.assertEqual(OperationLog.objects.count(), 1)

    # ── #2 编辑（`partial_update` 只是转调，**不得**写两行）──────
    def test_update_records_the_previous_name_when_renamed_in_the_same_request(self):
        """改名 + 改权限在一次请求里 ⇒ **恰好一行**，且旧名来自**写前状态**。

        判别力：把 `from_value` 改成请求载荷里的名字（载荷里根本没有旧名）或 `role.name`
        （写完之后 = 新名）⇒ 这一行的 `summary` 变成「…原职位名称：客服乙」⇒ 本用例变红。
        """
        role = self._role("客服甲")
        response = self.client.patch(
            f"{RBAC_BASE}{role.pk}/",
            {"name": "客服乙", "permission_codes": ["order.list", "order.delete"]},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertEqual(Role.objects.get(pk=role.pk).name, "客服乙")

        row = self._only(
            "system.role_update", log_type=40,
            summary="【log-rbac-admin】编辑角色权限：客服乙，原职位名称：客服甲",
        )
        self.assertEqual(row.target_kind, "role")
        self.assertEqual(row.target_id, str(role.pk))
        self.assertEqual(row.target_name, "客服乙")
        self.assertEqual(row.from_value, "客服甲")
        self.assertEqual(row.actor_id, self.admin.pk)

    def test_patch_writes_exactly_one_row(self):
        """⚠️ I6：`.partial_update` 只转调 `.update`——一次 `PATCH` **恰好一行**。

        判别力：在 `.partial_update` 上也埋一次 ⇒ 下面 `_only`（恰好一行）与总数断言双双变红。
        """
        role = self._role("客服甲")
        response = self.client.patch(
            f"{RBAC_BASE}{role.pk}/", {"remark": "新备注"}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.content)

        row = self._only(
            "system.role_update", log_type=40,
            summary="【log-rbac-admin】编辑角色权限：客服甲，原职位名称：客服甲",
        )
        self.assertEqual(row.from_value, "客服甲", "没改名时原职位名称就是它自己")
        self.assertEqual(row.target_name, "客服甲")
        # 1 行新增 + 1 行编辑，**不是** 2 行编辑。
        self.assertEqual(OperationLog.objects.count(), 2)

    # ── R26：纯 no-op 不写留痕；真的变了才写 ─────────────────────
    def test_noop_patch_writes_zero_rows(self):
        """R26：**没发生任何事**的 `PATCH` 不写留痕（与账号域「同值零行」同口径）。

        `system.role_update` 的模板断言了一次改名（「原职位名称：{旧名}」），对
        `PATCH {}` 或"重发当前值"写出「原职位名称：客服甲」是假话。

        判别力：去掉 `update` 里的 `changed` 判据（无条件 `record`）⇒ 六条 no-op 各多一行
        ⇒ 六条行数断言变红。末尾的**阳性对照**证明零行不是"端点坏了"。
        """
        role = self._role("客服甲", codes=("order.list", "order.add_edit"))
        created = OperationLog.objects.count()
        self.assertEqual(created, 1, "夹具：新增那一行")

        noops = [
            {},                                            # ① 空载荷（既有口径：纯 no-op，code 0）
            {"name": "客服甲"},                             # ② 重发当前名字
            {"remark": "原备注"},                           # ③ 重发当前备注
            # ④ 重发同一权限集合 **＋重复一项** ⇒ `set()` 去重后集合相同
            {"permission_codes": ["order.list", "order.add_edit", "order.list"]},
            # ⑤ 重发同一权限集合但**换顺序** ⇒ 集合语义下相同（比的是值/集合，不是列表顺序）
            {"permission_codes": ["order.add_edit", "order.list"]},
            # ⑥ 本线新增的第 4 个可写字段（`can_be_sales_agent`）：**重发当前值**（这个角色是 `False`）
            #    ⇒ 与「没传」是两件事（E-0021），但**结果**一样什么都没变 ⇒ 零行。
            {"can_be_sales_agent": False},
        ]
        for payload in noops:
            with self.subTest(payload=payload):
                response = self.client.patch(
                    f"{RBAC_BASE}{role.pk}/", payload, content_type="application/json"
                )
                self.assertEqual(response.json()["code"], 0, response.content)
                self.assertEqual(OperationLog.objects.count(), created)

        # **阳性对照**：同一个端点上真的改一次（备注）⇒ 恰好一行。
        response = self.client.patch(
            f"{RBAC_BASE}{role.pk}/", {"remark": "真改了"}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        row = self._only(
            "system.role_update", log_type=40,
            summary="【log-rbac-admin】编辑角色权限：客服甲，原职位名称：客服甲",
        )
        self.assertEqual(row.from_value, "客服甲")
        self.assertEqual(row.target_name, "客服甲")
        self.assertEqual(OperationLog.objects.count(), created + 1)

    def test_rename_only_patch_writes_one_row(self):
        """**只改名**（权限 / 备注都没动）⇒ 仍写一行：no-op 守卫不得把它吞掉。"""
        role = self._role("客服甲")
        before = OperationLog.objects.count()

        response = self.client.patch(
            f"{RBAC_BASE}{role.pk}/", {"name": "客服乙"}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        row = self._only(
            "system.role_update", log_type=40,
            summary="【log-rbac-admin】编辑角色权限：客服乙，原职位名称：客服甲",
        )
        self.assertEqual(row.target_name, "客服乙")
        self.assertEqual(row.from_value, "客服甲")
        self.assertEqual(OperationLog.objects.count(), before + 1)

    def test_permission_only_patch_writes_one_row(self):
        """**只改权限**（名字 / 备注都没动）⇒ 仍写一行：守卫的判据必须覆盖权限集合。"""
        role = self._role("客服甲")
        before = OperationLog.objects.count()

        response = self.client.patch(
            f"{RBAC_BASE}{role.pk}/",
            {"permission_codes": ["order.list", "order.delete"]},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        row = self._only(
            "system.role_update", log_type=40,
            summary="【log-rbac-admin】编辑角色权限：客服甲，原职位名称：客服甲",
        )
        self.assertEqual(row.from_value, "客服甲")
        self.assertEqual(
            set(Role.objects.get(pk=role.pk).permissions.values_list("code", flat=True)),
            {"order.list", "order.delete"},
        )
        self.assertEqual(OperationLog.objects.count(), before + 1)

    def test_sales_agent_flag_only_patch_writes_one_row(self):
        """**只勾/取消「可作为销售客服」**（其余字段一个没动）⇒ 仍写一行。

        ⚠️ 本线新增这个可写字段时**必须同时**回查它的留痕路径（E-0027 那一族：加写入点要
        连带看"清了没有 / 记了没有"）：`views.update` 的 `changed` 若不把标记算进去，
        勾一下保存是**真的改了数据却一行日志都没有**（`_only` 的「恰好一行」会红）。
        """
        role = self._role("客服甲")
        before = OperationLog.objects.count()

        response = self.client.patch(
            f"{RBAC_BASE}{role.pk}/",
            {"can_be_sales_agent": True},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        row = self._only(
            "system.role_update", log_type=40,
            summary="【log-rbac-admin】编辑角色权限：客服甲，原职位名称：客服甲",
        )
        self.assertEqual(row.from_value, "客服甲")
        self.assertIs(Role.objects.get(pk=role.pk).can_be_sales_agent, True)
        self.assertEqual(OperationLog.objects.count(), before + 1)

    # ── #3 删除 ──────────────────────────────────────────────────
    def test_destroy_writes_system_role_destroy(self):
        role = self._role("日志删除")
        role_id = role.pk
        self._only("system.role_create", log_type=40, summary="【log-rbac-admin】新增角色职位：日志删除")

        response = self.client.delete(f"{RBAC_BASE}{role_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertFalse(Role.objects.filter(pk=role_id).exists())

        row = self._only(
            "system.role_destroy", log_type=40, summary="【log-rbac-admin】删除角色职位：日志删除"
        )
        self.assertEqual(row.target_kind, "role")
        # ⚠️ `role.delete()` 会把实例的 pk 置 None ⇒ 必须在写前取到它。
        self.assertEqual(row.target_id, str(role_id))
        self.assertEqual(row.target_name, "日志删除")
        self.assertEqual(row.actor, "log-rbac-admin")
        self.assertEqual(row.actor_id, self.admin.pk)

    # ── #4 复制 ──────────────────────────────────────────────────
    def test_copy_names_both_the_source_and_the_new_role(self):
        source = self._role("日志源角", codes=("order.list",))
        response = self.client.post(
            f"{RBAC_BASE}{source.pk}/copy/", {"name": "日志副本", "remark": ""},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        clone = Role.objects.get(name="日志副本")

        row = self._only(
            "system.role_copy", log_type=40,
            summary="【log-rbac-admin】复制角色职位：日志源角 → 日志副本",
        )
        # 两个名字都必须真的在句子里（漏一个就渲染成空）。
        self.assertIn("日志源角", row.summary)
        self.assertIn("日志副本", row.summary)
        self.assertEqual(row.target_kind, "role")
        self.assertEqual(row.target_id, str(clone.pk))
        self.assertEqual(row.target_name, "日志副本")
        self.assertEqual(row.from_value, "日志源角")
        self.assertEqual(row.to_value, "日志副本")
        self.assertEqual(row.actor_id, self.admin.pk)
        # 复制出来的角色真的带上了源角色的权限（留痕不是这条路径唯一的副作用）。
        self.assertEqual(list(clone.permissions.values_list("code", flat=True)), ["order.list"])

    # ── 反向控制：被拒的写路径**零行** ───────────────────────────
    def test_duplicate_create_is_rejected_and_writes_zero_rows(self):
        self.assertEqual(self._create("重复角色").json()["code"], 0)
        before = OperationLog.objects.count()

        rejected = self._create("重复角色").json()
        self.assertEqual(rejected["code"], 1000, rejected)
        self.assertEqual(Role.objects.filter(name="重复角色").count(), 1)
        self.assertEqual(OperationLog.objects.count(), before)

    def test_duplicate_copy_is_rejected_and_writes_zero_rows(self):
        source = self._role("日志源角")
        copied = self.client.post(
            f"{RBAC_BASE}{source.pk}/copy/", {"name": "日志副本", "remark": ""},
            content_type="application/json",
        ).json()
        self.assertEqual(copied["code"], 0, copied)
        before = OperationLog.objects.count()

        rejected = self.client.post(
            f"{RBAC_BASE}{source.pk}/copy/", {"name": "日志副本", "remark": ""},
            content_type="application/json",
        ).json()
        self.assertEqual(rejected["code"], 1000, rejected)
        self.assertEqual(Role.objects.filter(name="日志副本").count(), 1)
        self.assertEqual(OperationLog.objects.count(), before)

    def test_rejected_rename_into_the_admin_role_writes_zero_rows(self):
        """改名守卫（`ensure_not_last_admin_role` 的 entering 方向）拒掉时**零行**。

        该角色有活跃非超管成员，改名为「系统管理员」会让这批人一次性成为管理员 ⇒
        `RoleServiceError` ⇒ 200 + 业务码，且角色 / Group 都不动。
        """
        role = self._role("日志角色")
        self._member(role, "log-role-member")
        before = OperationLog.objects.count()

        rejected = self.client.patch(
            f"{RBAC_BASE}{role.pk}/", {"name": "系统管理员"}, content_type="application/json"
        ).json()
        self.assertEqual(rejected["code"], 1000, rejected)
        self.assertEqual(Role.objects.get(pk=role.pk).name, "日志角色")
        self.assertEqual(Group.objects.get(pk=role.group_id).name, "日志角色")
        self.assertEqual(OperationLog.objects.count(), before)

    def test_rejected_delete_of_the_last_admin_role_writes_zero_rows(self):
        """删除保护（D9）：删**最后一个管理员角色**被拒时零行，角色与 Group 都还在。

        构造：让租户里唯一的管理员就是这个「系统管理员」角色的一位活跃非超管成员
        （超管全部停用）⇒ `active_admin_count() - dependent == 0`。
        """
        role = self._role(
            "系统管理员", codes=("system.role_authority", "system.role_authority_delete")
        )
        deleter = make_logged_in_client(username="log-role-deleter", password="pw-123456")
        deleter_user = User.objects.get(username="log-role-deleter")
        self.assertFalse(deleter_user.is_superuser, "夹具必须是非超管，否则保护被超管计入而放行")
        deleter_user.groups.add(role.group)
        # 夹具超管退场，否则它自己就是一个管理员，删除会被放行。
        User.objects.filter(username="log-rbac-admin").update(is_active=False)
        before = OperationLog.objects.count()

        response = deleter.delete(f"{RBAC_BASE}{role.pk}/")
        # 守卫抛的 `RoleServiceError` 由 `_run` 翻成业务码（E-0010），不是 403 / 500。
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000, response.content)
        self.assertTrue(Role.objects.filter(pk=role.pk).exists(), "被拒的删除把角色删掉了")
        self.assertTrue(Group.objects.filter(name="系统管理员").exists())
        self.assertEqual(OperationLog.objects.count(), before)

    # ── R25：`create` / `copy` 原先没有事务边界 ──────────────────
    def test_create_rolls_back_the_role_and_group_when_record_fails(self):
        """R25：`create` 原先**没有** `atomic()` ⇒ 补一层，把角色写与 `record(...)` 收在一起。

        判别力：去掉 `create()` 里那层 `with transaction.atomic():` ⇒ `create_role` 的
        `@atomic` 出块即释放 SAVEPOINT（测试库的外层事务仍在），`record` 再抛异常时
        角色与 Group 都已可见、留痕也已写一行 ⇒ 末尾三条断言变红。
        """
        seen = {}

        def _boom(**kwargs):
            real_record(**kwargs)   # 哨兵：先真的写进去，再证明它随事务回滚
            seen["role"] = Role.objects.filter(name="回滚角色").exists()
            seen["group"] = Group.objects.filter(name="回滚角色").exists()
            seen["rows"] = OperationLog.objects.filter(action="system.role_create").count()
            raise RuntimeError("留痕写不进去")

        # E-0029：桩打在**被查找的模块**（`rbac/views.py` 里的 `record` 名字）上。
        with mock.patch("apps.rbac.views.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self._create("回滚角色")

        self.assertTrue(seen["role"], "角色在事务里真的建过——否则回滚断言是假通过")
        self.assertTrue(seen["group"], "同名 Group 也在同一事务里")
        self.assertEqual(seen["rows"], 1, "留痕真的写过一行")
        self.assertFalse(Role.objects.filter(name="回滚角色").exists())   # ① 回滚
        self.assertFalse(Group.objects.filter(name="回滚角色").exists())  # ① 回滚
        self.assertEqual(OperationLog.objects.count(), 0)                 # ② 留痕一并回滚

    def test_copy_rolls_back_the_new_role_when_record_fails(self):
        """R25：`copy` 原先**没有** `atomic()` ⇒ 同 `create` 补一层。

        判别力同 `test_create_rolls_back_the_role_and_group_when_record_fails`：
        去掉那层 `atomic()` ⇒ 复制出来的角色还在、`system.role_copy` 留有一行 ⇒ 变红。
        """
        source = self._role("日志源角")
        seen = {}

        def _boom(**kwargs):
            real_record(**kwargs)
            seen["role"] = Role.objects.filter(name="回滚副本").exists()
            seen["rows"] = OperationLog.objects.filter(action="system.role_copy").count()
            raise RuntimeError("留痕写不进去")

        with mock.patch("apps.rbac.views.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"{RBAC_BASE}{source.pk}/copy/", {"name": "回滚副本", "remark": ""},
                    content_type="application/json",
                )

        self.assertTrue(seen["role"])
        self.assertEqual(seen["rows"], 1)
        self.assertFalse(Role.objects.filter(name="回滚副本").exists())    # ① 回滚
        self.assertFalse(Group.objects.filter(name="回滚副本").exists())   # ① 回滚
        self.assertEqual(self._rows("system.role_copy").count(), 0)        # ② 留痕一并回滚


# ══════════════════════════════════════════════════════════════════
# 配置域（spec §5.4 / §5.5）
# ══════════════════════════════════════════════════════════════════
CONFIG_BASE = "/api/business"
GAMES = f"{CONFIG_BASE}/games/"
SERVERS = f"{CONFIG_BASE}/servers/"
CATEGORIES = f"{CONFIG_BASE}/categories/"
PROJECTS = f"{CONFIG_BASE}/projects/"


class ConfigWritePathLogTests(TenantTestCase):
    """四个配置 ViewSet 的写路径 × 留痕列（spec §5.4 / §5.5 / §5.9 / §10.2）。

    这四个 ViewSet **自己没有** `create` / `update` / `destroy`（都继承自
    `EnvelopeModelViewSet`），埋点因此落在共享 mixin 的 DRF 钩子
    （`perform_create` / `perform_update` / `perform_destroy`）上。

    三条纪律同本文件开头：断言打在**数据库行**上（E-0009）；期望值手写字面值——
    `log_type` 写 `40` / `70`（**不引用 `LogType`**）、`action` 写 code、
    `summary` 写渲染后的整句（E-0024）；被拒 / 纯 no-op 的写路径断言**零行**。
    """

    def setUp(self):
        # ⚠️ 游戏配置写端点要节点
        # （create / update / partial_update → `game.add_edit`；destroy → `game.delete`）；
        # 本类还经真实端点写**区服 / 分类 / 项目**
        # （`SERVERS` / `CATEGORIES` / `PROJECTS`）⇒ 另需这三条节点。
        # `nodes=` **必须配 `groups=`**（`_grant_nodes` 遍历 `user.groups` 挂节点）。
        self.client = make_logged_in_client(
            username="log-config", groups=("配置组",),
            nodes=(
                "game.add_edit",
                "game.delete",
                "game.server",
                "game.order_type_categories",
                "game.common_projects",
            ),
        )
        self.user = User.objects.get(username="log-config")
        # 夹具直接走 ORM（不过端点）⇒ 不产生任何留痕行。
        self.game = Game.objects.create(name="原神", sort_order=5)
        self.cat = ProjectCategory.objects.create(game=self.game, name="深境螺旋")

    # ── 断言助手 ─────────────────────────────────────────────────
    def _only(self, action, *, log_type, summary):
        """钉住「这个动作恰好一行」，并逐项断言字面值。返回那一行。"""
        rows = list(OperationLog.objects.filter(action=action))
        self.assertEqual(
            len(rows), 1,
            f"期望 {action} 恰好一行，实际 {len(rows)} 行："
            f"{[(r.action, r.summary) for r in OperationLog.objects.all()]}",
        )
        row = rows[0]
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, log_type)
        self.assertEqual(row.summary, summary)
        return row

    def _json(self, response, expected=0):
        body = response.json()
        self.assertEqual(body["code"], expected, response.content)
        return body

    # ── 游戏：40 系统相关 ────────────────────────────────────────
    def test_game_create_update_destroy(self):
        created = self.client.post(
            GAMES, {"name": "崩坏", "actor": "冒名者"}, content_type="application/json"
        )
        game = Game.objects.get(name="崩坏")
        body = self._json(created)
        self.assertEqual(body["data"]["id"], game.pk)

        row = self._only("system.game_create", log_type=40, summary="【log-config】新增游戏类：崩坏")
        self.assertEqual(row.target_kind, "game")
        self.assertEqual(row.target_id, str(game.pk))
        self.assertEqual(row.target_name, "崩坏")
        # R20：操作人取**认证态**；请求体里那个同名 `actor` 字段不得进入留痕。
        self.assertEqual(row.actor, "log-config")
        self.assertEqual(row.actor_id, self.user.pk)

        patched = self.client.patch(
            f"{GAMES}{game.pk}/", {"name": "崩坏3"}, content_type="application/json"
        )
        self._json(patched)
        row = self._only("system.game_update", log_type=40, summary="【log-config】编辑游戏类：崩坏3")
        self.assertEqual(row.target_kind, "game")
        self.assertEqual(row.target_id, str(game.pk))
        self.assertEqual(row.target_name, "崩坏3")
        self.assertEqual(row.actor_id, self.user.pk)

        deleted = self.client.delete(f"{GAMES}{game.pk}/")
        self._json(deleted)
        # ⚠️ 删除是**软删除**（D-0085）：行还在，只是打上了 `deleted_at`。
        game.refresh_from_db()
        self.assertIsNotNone(game.deleted_at, "软删除必须打上 deleted_at")
        row = self._only("system.game_destroy", log_type=40, summary="【log-config】删除游戏：崩坏3")
        self.assertEqual(row.target_kind, "game")
        self.assertEqual(row.target_id, str(game.pk))
        self.assertEqual(row.target_name, "崩坏3")
        self.assertEqual(OperationLog.objects.count(), 3)

    # ── 区服：40 系统相关 ────────────────────────────────────────
    def test_server_create_update_destroy(self):
        created = self.client.post(
            SERVERS, {"game": self.game.pk, "name": "国服"}, content_type="application/json"
        )
        server = GameServer.objects.get(name="国服")
        self._json(created)

        row = self._only("system.server_create", log_type=40, summary="【log-config】新增区服：国服")
        # R28：`target_kind` 取 spec §4.2 词表里的字面值（不是模型名 `game_server`）。
        self.assertEqual(row.target_kind, "server")
        self.assertEqual(row.target_id, str(server.pk))
        self.assertEqual(row.target_name, "国服")
        self.assertEqual(row.actor, "log-config")
        self.assertEqual(row.actor_id, self.user.pk)

        patched = self.client.patch(
            f"{SERVERS}{server.pk}/", {"name": "官服"}, content_type="application/json"
        )
        self._json(patched)
        row = self._only("system.server_update", log_type=40, summary="【log-config】编辑区服：官服")
        self.assertEqual(row.target_id, str(server.pk))
        self.assertEqual(row.target_name, "官服")

        deleted = self.client.delete(f"{SERVERS}{server.pk}/")
        self._json(deleted)
        self.assertFalse(GameServer.objects.filter(pk=server.pk).exists())
        row = self._only("system.server_destroy", log_type=40, summary="【log-config】删除区服：官服")
        self.assertEqual(row.target_id, str(server.pk))
        self.assertEqual(row.target_name, "官服")
        self.assertEqual(OperationLog.objects.count(), 3)

    # ── 项目分类：70 其它 ────────────────────────────────────────
    def test_category_create_update_destroy(self):
        created = self.client.post(
            CATEGORIES,
            {"game": self.game.pk, "name": "深渊"},
            content_type="application/json",
        )
        cat = ProjectCategory.objects.get(name="深渊")
        self._json(created)

        row = self._only("category.create", log_type=70, summary="【log-config】新增项目分类：深渊")
        # R28：词表里是 `category`，不是模型名 `project_category`。
        self.assertEqual(row.target_kind, "category")
        self.assertEqual(row.target_id, str(cat.pk))
        self.assertEqual(row.target_name, "深渊")
        self.assertEqual(row.actor_id, self.user.pk)

        patched = self.client.patch(
            f"{CATEGORIES}{cat.pk}/", {"name": "深境"}, content_type="application/json"
        )
        self._json(patched)
        row = self._only("category.update", log_type=70, summary="【log-config】修改项目分类：深境")
        self.assertEqual(row.target_name, "深境")

        deleted = self.client.delete(f"{CATEGORIES}{cat.pk}/")
        self._json(deleted)
        # ⚠️ 删除是**软删除**（D-0085）：行还在，只是打上了 `deleted_at`。
        cat.refresh_from_db()
        self.assertIsNotNone(cat.deleted_at, "软删除必须打上 deleted_at")
        row = self._only("category.destroy", log_type=70, summary="【log-config】删除项目分类：深境")
        self.assertEqual(row.target_id, str(cat.pk))
        self.assertEqual(row.target_name, "深境")
        self.assertEqual(OperationLog.objects.count(), 3)

    # ── 代练项目：70 其它 ────────────────────────────────────────
    def test_project_create_update_destroy(self):
        created = self.client.post(
            PROJECTS,
            {"category": self.cat.pk, "name": "满星"},
            content_type="application/json",
        )
        project = Project.objects.get(name="满星")
        self._json(created)

        row = self._only("project.create", log_type=70, summary="【log-config】新增代练项目：满星")
        self.assertEqual(row.target_kind, "project")
        self.assertEqual(row.target_id, str(project.pk))
        self.assertEqual(row.target_name, "满星")
        self.assertEqual(row.actor, "log-config")
        self.assertEqual(row.actor_id, self.user.pk)

        patched = self.client.patch(
            f"{PROJECTS}{project.pk}/", {"name": "满星满命"}, content_type="application/json"
        )
        self._json(patched)
        row = self._only("project.update", log_type=70, summary="【log-config】修改代练项目：满星满命")
        self.assertEqual(row.target_name, "满星满命")

        deleted = self.client.delete(f"{PROJECTS}{project.pk}/")
        self._json(deleted)
        # ⚠️ 删除是**软删除**（D-0085）：行还在，只是打上了 `deleted_at`。
        project.refresh_from_db()
        self.assertIsNotNone(project.deleted_at, "软删除必须打上 deleted_at")
        row = self._only("project.destroy", log_type=70, summary="【log-config】删除代练项目：满星满命")
        self.assertEqual(row.target_id, str(project.pk))
        self.assertEqual(row.target_name, "满星满命")
        self.assertEqual(OperationLog.objects.count(), 3)

    # ── `partial_update` 只是转调 ⇒ 一次 PATCH 恰好一行 ──────────
    def test_patch_writes_exactly_one_row(self):
        """⚠️ `PATCH` 走 `partial_update`，它只转调 `update`：
        `rest_framework/mixins.py:80-82` 设 `kwargs['partial']=True` 后 `return self.update(...)`，
        而 `update` 调**一次** `perform_update`（同文件 `:63-68`）⇒ 一次 `PATCH` 恰好一行。

        判别力：若在 `partial_update` / 按请求方法各埋一次 ⇒ 下面「恰好一行」与总数断言变红。
        """
        project = Project.objects.create(category=self.cat, name="满星")
        response = self.client.patch(
            f"{PROJECTS}{project.pk}/", {"sort_order": 7}, content_type="application/json"
        )
        self._json(response)
        self.assertEqual(Project.objects.get(pk=project.pk).sort_order, 7)

        row = self._only("project.update", log_type=70, summary="【log-config】修改代练项目：满星")
        self.assertEqual(row.target_name, "满星")
        self.assertEqual(OperationLog.objects.count(), 1)

    # ── R27.2：纯 no-op 不写留痕；真变了才写（含布尔 / 0 边界）────
    def test_noop_put_and_patch_write_zero_rows_with_positive_controls(self):
        """R27.2：把当前值原样重发 = 什么都没发生 ⇒ **零行**；真改一个字段 ⇒ 恰好一行。

        判别力（三条）：
        ① 去掉 no-op 守卫 ⇒ 四条 no-op 各多一行 ⇒ 行数断言变红；
        ② 守卫若按**真值**判「有没有变」（`if value:`）⇒ 末尾两条把 `is_active` 改成
           `False`、`sort_order` 改成 `0` 的**真实变更**会被当成"没变"而吞掉（E-0021 同族）
           ⇒ 那两条 `_only` 变红；
        ③ 守卫若按**身份**比 ⇒ 正向对照里的 FK / 值比较行为见下一条用例。
        """
        before = OperationLog.objects.count()
        self.assertEqual(before, 0, "夹具：直接 ORM 建的对象不写留痕")

        noops = [
            # PUT 全量重发当前值
            ("put", {
                "name": "原神", "sort_order": 5, "is_active": True,
                "is_default": False, "is_mobile": False, "is_companion": False,
                "disable_sms": False,
            }),
            ("patch", {"name": "原神"}),                 # PATCH 重发名字
            ("patch", {"sort_order": 5, "is_active": True}),
            ("patch", {}),                              # 空载荷（既有口径：纯 no-op，code 0）
        ]
        for method, payload in noops:
            with self.subTest(method=method, payload=payload):
                response = getattr(self.client, method)(
                    f"{GAMES}{self.game.pk}/", payload, content_type="application/json"
                )
                self._json(response)
                self.assertEqual(
                    OperationLog.objects.count(), before,
                    f"{method} {payload} 是纯 no-op，不得写留痕",
                )

        # 阳性对照 ①：改布尔字段 True → False（falsy 值，真值判断会漏记）
        response = self.client.patch(
            f"{GAMES}{self.game.pk}/", {"is_active": False}, content_type="application/json"
        )
        self._json(response)
        row = self._only("system.game_update", log_type=40, summary="【log-config】编辑游戏类：原神")
        self.assertEqual(row.target_name, "原神")
        self.assertEqual(row.target_id, str(self.game.pk))
        self.assertEqual(OperationLog.objects.count(), before + 1)

        # 阳性对照 ②：改整数字段 5 → 0（同样是 falsy 值）
        response = self.client.patch(
            f"{GAMES}{self.game.pk}/", {"sort_order": 0}, content_type="application/json"
        )
        self._json(response)
        self.assertEqual(Game.objects.get(pk=self.game.pk).sort_order, 0)
        self.assertEqual(OperationLog.objects.count(), before + 2)

    def test_noop_patch_resending_the_same_foreign_key_writes_zero_rows(self):
        """FK 的比较必须是**值语义**（比主键），不是身份语义。

        判别力：按身份比（`validated_data["game"] is not instance.game`）⇒ 重发同一个
        `game` 也会被判成"变了"、多写一行 ⇒ 第一条零行断言变红（RBAC 的权限 code 集合
        踩的正是这一类坑）。
        """
        server = GameServer.objects.create(game=self.game, name="国服")
        before = OperationLog.objects.count()

        response = self.client.patch(
            f"{SERVERS}{server.pk}/", {"game": self.game.pk}, content_type="application/json"
        )
        self._json(response)
        self.assertEqual(
            OperationLog.objects.count(), before,
            "重发同一个 game 是纯 no-op，不得写留痕",
        )

        # 阳性对照：真的换一个游戏 ⇒ 恰好一行（守卫不得把真实变更吞掉）。
        other = Game.objects.create(name="崩坏")
        response = self.client.patch(
            f"{SERVERS}{server.pk}/", {"game": other.pk}, content_type="application/json"
        )
        self._json(response)
        row = self._only("system.server_update", log_type=40, summary="【log-config】编辑区服：国服")
        self.assertEqual(row.target_name, "国服")
        self.assertEqual(
            GameServer.objects.get(pk=server.pk).game_id, other.pk
        )
        self.assertEqual(OperationLog.objects.count(), before + 1)

    # ── R27.1：写 + 留痕同事务（反向控制）────────────────────────
    def test_create_rolls_back_the_game_when_record_fails(self):
        """R27.1：四个 ViewSet 此前**没有** `atomic()` ⇒ mixin 的钩子补一层。

        判别力：去掉 `perform_create` 里那层 `with transaction.atomic():` ⇒ 游戏已提交
        （`serializer.save()` 自己那层出块即释放 SAVEPOINT）、留痕也已写一行
        ⇒ 末尾两条断言变红。
        """
        seen = {}

        def _boom(**kwargs):
            real_record(**kwargs)   # 哨兵：先真的写进去，再证明它随事务回滚
            seen["game"] = Game.objects.filter(name="回滚游戏").exists()
            seen["rows"] = OperationLog.objects.filter(action="system.game_create").count()
            raise RuntimeError("留痕写不进去")

        # E-0029：桩打在**被查找的那个模块**（`config_views` 里的 `record` 名字）上。
        with mock.patch("apps.business.api.config_views.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    GAMES, {"name": "回滚游戏"}, content_type="application/json"
                )

        self.assertTrue(seen["game"], "游戏在事务里真的建过——否则回滚断言是假通过")
        self.assertEqual(seen["rows"], 1, "留痕真的写过一行")
        self.assertFalse(Game.objects.filter(name="回滚游戏").exists())  # ① 业务行回滚
        self.assertEqual(OperationLog.objects.count(), 0)                # ② 留痕一并回滚

    def test_destroy_rolls_back_the_server_when_record_fails(self):
        """R27.1（删除方向）：区服真的删掉了，但留痕写不进去 ⇒ 整块回滚，区服还在。

        判别力同 create：去掉 `perform_destroy` 里的 `atomic()` ⇒ 区服已删、留痕留一行。
        """
        server = GameServer.objects.create(game=self.game, name="回滚区服")
        seen = {}

        def _boom(**kwargs):
            real_record(**kwargs)
            seen["server"] = GameServer.objects.filter(pk=server.pk).exists()
            seen["rows"] = OperationLog.objects.filter(action="system.server_destroy").count()
            raise RuntimeError("留痕写不进去")

        with mock.patch("apps.business.api.config_views.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.client.delete(f"{SERVERS}{server.pk}/")

        self.assertFalse(seen["server"], "区服在事务里真的删过——否则回滚断言是假通过")
        self.assertEqual(seen["rows"], 1, "留痕真的写过一行")
        self.assertTrue(GameServer.objects.filter(pk=server.pk).exists())  # ① 业务行回滚
        self.assertEqual(OperationLog.objects.count(), 0)                  # ② 留痕一并回滚

    def test_update_rolls_back_the_field_change_when_record_fails(self):
        """R27.1（更新方向）：字段真的改了，但留痕写不进去 ⇒ 整块回滚，字段还原。

        这是 create / destroy 那对（R27.1 三个钩子）缺的第三条：删掉
        `apps/business/api/config_views.py::perform_update` 里的 `with transaction.atomic():`
        时，本用例**必须**变红（`PATCH` 的字段改动会提交、留痕也留一行）。

        判别力：去掉那层 `atomic()` ⇒ ① 字段已提交、② 留痕已留一行 ⇒ 末尾两条断言变红。
        哨兵 `seen` 证明「字段改动」与「留痕行」在失败**之前**都已真实写进事务——
        否则「回滚了」可能只是「压根没写」的假通过。
        """
        server = GameServer.objects.create(game=self.game, name="回滚区服")
        other = Game.objects.create(name="崩坏")
        seen = {}

        def _boom(**kwargs):
            # 哨兵：先真的把留痕写进事务，再证明它随事务回滚。
            real_record(**kwargs)
            seen["field"] = GameServer.objects.filter(pk=server.pk, game=other).exists()
            seen["rows"] = OperationLog.objects.filter(action="system.server_update").count()
            raise RuntimeError("留痕写不进去")

        # E-0029：桩打在**被查找的那个模块**（`config_views` 里的 `record` 名字）上。
        with mock.patch("apps.business.api.config_views.record", side_effect=_boom):
            with self.assertRaises(RuntimeError):
                self.client.patch(
                    f"{SERVERS}{server.pk}/",
                    {"game": other.pk},
                    content_type="application/json",
                )

        self.assertTrue(seen["field"], "字段在事务里真的改过——否则回滚断言是假通过")
        self.assertEqual(seen["rows"], 1, "留痕真的写过一行")
        # ① 字段改动回滚（`game` 仍是原来那个）
        self.assertEqual(GameServer.objects.get(pk=server.pk).game_id, self.game.pk)
        # ② 留痕一并回滚
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── 反向控制：`PROTECT` 挡住删除时**零行** ───────────────────
    def test_delete_referenced_game_is_soft_and_writes_exactly_one_row(self):
        """删**被引用**的游戏（业务方 2026-09-22 裁定改成软删除，D-0085）⇒ 成功 + 恰好一行。

        ⚠️ **本用例取代了原来那条「被引用 ⇒ 1001 + 零行」**（原文按本仓惯例保留在提交历史里）：
        那条凭的是 `GameServer.game` 的 `on_delete=PROTECT`（真删会抛 `ProtectedError`，
        由 `EnvelopeModelViewSet.destroy` 翻成 `ErrorCode.CONFLICT`=1001）。现在删除**不真删**
        ⇒ `PROTECT` 根本不会被触发、也不再回 1001；**而「被引用的数据不许被带走」这条一点没松**：
        区服行还在、还指着同一个游戏、游戏行也还在（只是 `deleted_at` 有值）。
        留痕仍然是**成功才写**、且恰好一行。
        """
        server = GameServer.objects.create(game=self.game, name="国服")
        before = OperationLog.objects.count()

        response = self.client.delete(f"{GAMES}{self.game.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.content)
        self.game.refresh_from_db()
        self.assertIsNotNone(self.game.deleted_at, "软删除必须打上 deleted_at")
        server.refresh_from_db()
        self.assertEqual(server.game_id, self.game.pk, "被引用的关联行不许被带走")
        self.assertEqual(OperationLog.objects.count(), before + 1)
        row = OperationLog.objects.order_by("-id").first()
        self.assertEqual(row.action, "system.game_destroy")
        self.assertEqual(row.target_kind, "game")
        self.assertEqual(row.target_id, str(self.game.pk))
        self.assertEqual(row.target_name, "原神")


# ══════════════════════════════════════════════════════════════════
# 订单来源 / 店铺对接
# ══════════════════════════════════════════════════════════════════
ORDER_SOURCES = "/api/business/order-sources/"
SHOP_BINDINGS = "/api/business/shop-bindings/"


class OrderSourceWritePathLogTests(TenantTestCase):
    """订单来源 CRUD 的埋点（spec §5.4）。

    文案**逐字照抄**对标（`.tools/recon/oplog/templates.json` 的 `systemlog:40`）⇒
    断言写**整句字面值**（含全角 `：` U+FF1A / `，` U+FF0C），**不**用模板 `.format(...)`
    自指（E-0024）——把标点改坏，这些用例必须红。
    四个写端点 = POST / PUT / PATCH / DELETE；PUT 与 PATCH 同落 `perform_update`，
    各有**正向**行级断言（E-0065：只覆写 `perform_create` 不能证明 PUT/PATCH 写了行）。
    """

    def setUp(self):
        # ⚠️ 订单来源写端点要节点
        # （create / update / partial_update → `system.add_edit_source`；
        # destroy → `system.delete_source`）。`nodes=` **必须配 `groups=`**。
        self.client = make_logged_in_client(
            username="log-source", groups=("来源组",),
            nodes=("system.add_edit_source", "system.delete_source"),
        )

    def _json(self, response, expected=0):
        body = response.json()
        self.assertEqual(body["code"], expected, response.content)
        return body

    def _only(self, action, *, log_type, summary):
        """钉住「这个动作恰好一行」并逐项断言字面值；顺带断言旧表零行。"""
        rows = list(OperationLog.objects.filter(action=action))
        self.assertEqual(
            len(rows), 1,
            f"期望 {action} 恰好一行，实际 {len(rows)} 行："
            f"{[(r.action, r.summary) for r in OperationLog.objects.all()]}",
        )
        row = rows[0]
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, log_type)
        self.assertEqual(row.summary, summary)
        self.assertFalse(AuditLog.objects.exists(), "订单来源写路径往旧 AuditLog 写了一行")
        return row

    def test_create_writes_the_benchmark_sentence_verbatim(self):
        created = self.client.post(
            ORDER_SOURCES,
            {"name": "中野三玖", "remark": "中野三玖", "order_by": 0, "shop_type": 20},
            content_type="application/json",
        )
        source = OrderSource.objects.get(name="中野三玖")
        self._json(created)

        row = self._only(
            "system.order_source_create", log_type=40,
            summary="【log-source】新增订单来源：中野三玖，排序：0，备注：中野三玖",
        )
        self.assertEqual(row.target_kind, "order_source")
        self.assertEqual(row.target_id, str(source.pk))
        self.assertEqual(row.target_name, "中野三玖")
        self.assertEqual(row.to_value, "0")
        self.assertEqual(row.reason, "中野三玖")
        # 操作人取认证态（R20）
        self.assertEqual(row.actor, "log-source")
        self.assertEqual(row.actor_id, User.objects.get(username="log-source").pk)

    def test_patch_update_writes_the_benchmark_sentence(self):
        source = OrderSource.objects.create(
            name="中野三玖", order_by=0, remark="中野三玖"
        )
        patched = self.client.patch(
            f"{ORDER_SOURCES}{source.pk}/", {"order_by": 1}, content_type="application/json"
        )
        self._json(patched)

        row = self._only(
            "system.order_source_update", log_type=40,
            summary="【log-source】编辑订单来源：中野三玖，排序：1，备注：中野三玖",
        )
        self.assertEqual(row.target_kind, "order_source")
        self.assertEqual(row.target_id, str(source.pk))
        self.assertEqual(row.target_name, "中野三玖")
        self.assertEqual(row.to_value, "1")

    def test_put_update_writes_a_row_too(self):
        """PUT 与 PATCH 是同一个落点（`perform_update`），**两者都必须真写**（E-0065）。"""
        source = OrderSource.objects.create(
            name="京东", order_by=3, remark="来自京东店铺的订单"
        )
        put = self.client.put(
            f"{ORDER_SOURCES}{source.pk}/",
            {"name": "京东", "order_by": 7, "remark": "来自京东店铺的订单", "shop_type": 20},
            content_type="application/json",
        )
        self._json(put)

        row = self._only(
            "system.order_source_update", log_type=40,
            summary="【log-source】编辑订单来源：京东，排序：7，备注：来自京东店铺的订单",
        )
        self.assertEqual(row.to_value, "7")

    def test_noop_update_writes_zero_rows(self):
        source = OrderSource.objects.create(name="闲鱼", order_by=0, remark="来自闲鱼的订单")

        self.client.patch(
            f"{ORDER_SOURCES}{source.pk}/",
            {"order_by": 0, "remark": "来自闲鱼的订单"},
            content_type="application/json",
        )
        self.client.put(
            f"{ORDER_SOURCES}{source.pk}/",
            # `shop_type` 必须写**实例当前值**（ORM 建行时取模型默认 `0` 淘宝/天猫），
            # 否则这一笔 PUT 就不是 no-op 了。
            {"name": "闲鱼", "order_by": 0, "remark": "来自闲鱼的订单", "shop_type": 0},
            content_type="application/json",
        )
        self.assertEqual(OperationLog.objects.count(), 0, "纯 no-op 更新写了留痕行")
        # 正向对照：真改一处就写行（否则上面可能只是整条链路没跑）
        self.client.patch(
            f"{ORDER_SOURCES}{source.pk}/", {"order_by": 5}, content_type="application/json"
        )
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_destroy_writes_the_benchmark_sentence(self):
        source = OrderSource.objects.create(
            name="京东", order_by=0, remark="来自京东店铺的订单"
        )
        pk = source.pk
        deleted = self.client.delete(f"{ORDER_SOURCES}{pk}/")
        self._json(deleted)
        self.assertFalse(OrderSource.objects.filter(pk=pk).exists())

        row = self._only(
            "system.order_source_destroy", log_type=40,
            summary="【log-source】删除订单来源：京东，备注：来自京东店铺的订单",
        )
        self.assertEqual(row.target_id, str(pk))
        self.assertEqual(row.target_name, "京东")
        self.assertEqual(row.reason, "来自京东店铺的订单")

    def test_rejected_create_leaves_zero_rows(self):
        OrderSource.objects.create(name="淘宝")
        rejected = self.client.post(
            ORDER_SOURCES, {"name": "淘宝"}, content_type="application/json"
        )
        # 字面值 1000，不引常量（E-0024）
        self.assertEqual(rejected.json()["code"], 1000, rejected.content)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertFalse(AuditLog.objects.exists())


class ShopBindingWritePathLogTests(TenantTestCase):
    """店铺对接 CRUD 的埋点（spec §5.4；M3b）。**来源 = 新拟**（对标无此动作）。

    句式照 `system.order_source_*`（名 + 属性 + 备注），`平台` 顶替其 `排序` 槽位
    （店铺对接没有排序字段）。三个写端点 = POST / PATCH / DELETE（PUT 被
    `http_method_names` 关成 405，有 `test_shop_binding_api.py` 的用例钉住）。

    ⚠️ **凭据纪律**是本类的一条独立判据（`test_no_credential_reaches_any_log_column`）：
    `access_token` 一个字都不得进日志列。
    """

    def setUp(self):
        # ⚠️ E-0253 起 shop-bindings 端点有节点门（`system.pinduoduo_settings`）⇒ 夹具持该节点；
        #    本类断言（留痕句式）一个字没改。
        self.client = make_logged_in_client(
            username="log-binding", groups=("客服",), nodes=("system.pinduoduo_settings",),
        )

    def _json(self, response, expected=0):
        body = response.json()
        self.assertEqual(body["code"], expected, response.content)
        return body

    def _only(self, action, *, log_type, summary):
        rows = list(OperationLog.objects.filter(action=action))
        self.assertEqual(
            len(rows), 1,
            f"期望 {action} 恰好一行，实际 {len(rows)} 行："
            f"{[(r.action, r.summary) for r in OperationLog.objects.all()]}",
        )
        row = rows[0]
        self.assertEqual(row.action, action)
        self.assertEqual(row.log_type, log_type)
        self.assertEqual(row.summary, summary)
        self.assertFalse(AuditLog.objects.exists(), "店铺对接写路径往旧 AuditLog 写了一行")
        return row

    def test_create_writes_the_new_sentence(self):
        created = self.client.post(
            SHOP_BINDINGS,
            {
                "platform": 20,
                "shop_name": "我的闲鱼店",
                "access_token": "tok-1",
                "remark": "测试店",
            },
            content_type="application/json",
        )
        binding = ShopBinding.objects.get(shop_name="我的闲鱼店")
        self._json(created)

        row = self._only(
            "system.shop_binding_create", log_type=40,
            summary="【log-binding】新增店铺对接：我的闲鱼店，平台：闲鱼，备注：测试店",
        )
        self.assertEqual(row.target_kind, "shop_binding")
        self.assertEqual(row.target_id, str(binding.pk))
        self.assertEqual(row.target_name, "我的闲鱼店")
        self.assertEqual(row.to_value, "闲鱼")
        self.assertEqual(row.reason, "测试店")
        self.assertEqual(row.actor, "log-binding")

    def test_patch_update_writes_the_new_sentence(self):
        binding = ShopBinding.objects.create(platform=20, shop_name="旧店", remark="旧备注")
        patched = self.client.patch(
            f"{SHOP_BINDINGS}{binding.pk}/",
            {"shop_name": "新店", "remark": "新备注"},
            content_type="application/json",
        )
        self._json(patched)

        row = self._only(
            "system.shop_binding_update", log_type=40,
            summary="【log-binding】编辑店铺对接：新店，平台：闲鱼，备注：新备注",
        )
        self.assertEqual(row.target_kind, "shop_binding")
        self.assertEqual(row.target_id, str(binding.pk))
        self.assertEqual(row.to_value, "闲鱼")

    def test_noop_update_writes_zero_rows(self):
        binding = ShopBinding.objects.create(platform=20, shop_name="闲鱼店", remark="备注")

        self.client.patch(
            f"{SHOP_BINDINGS}{binding.pk}/",
            {"shop_name": "闲鱼店", "remark": "备注"},
            content_type="application/json",
        )
        self.assertEqual(OperationLog.objects.count(), 0, "纯 no-op 更新写了留痕行")
        # 正向对照
        self.client.patch(
            f"{SHOP_BINDINGS}{binding.pk}/", {"remark": "改一下"}, content_type="application/json"
        )
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_destroy_writes_the_new_sentence(self):
        binding = ShopBinding.objects.create(
            platform=20, shop_name="闲鱼店", remark="来自闲鱼的订单"
        )
        pk = binding.pk
        deleted = self.client.delete(f"{SHOP_BINDINGS}{pk}/")
        self._json(deleted)
        self.assertFalse(ShopBinding.objects.filter(pk=pk).exists())

        row = self._only(
            "system.shop_binding_destroy", log_type=40,
            # 删除句与对标的 `删除订单来源：{名}，备注：{备注}` 同形：**不含**那个属性槽位。
            summary="【log-binding】删除店铺对接：闲鱼店，备注：来自闲鱼的订单",
        )
        self.assertEqual(row.target_id, str(pk))
        self.assertEqual(row.target_name, "闲鱼店")
        self.assertEqual(row.reason, "来自闲鱼的订单")

    def test_blank_shop_name_falls_back_to_the_platform_label(self):
        """`shop_name` 可空（允许先建行）：句子不能缺主词 ⇒ 退回平台文案。"""
        created = self.client.post(
            SHOP_BINDINGS, {"platform": 20}, content_type="application/json"
        )
        self._json(created)
        row = self._only(
            "system.shop_binding_create", log_type=40,
            summary="【log-binding】新增店铺对接：闲鱼，平台：闲鱼，备注：",
        )
        self.assertEqual(row.target_name, "闲鱼")

    def test_no_credential_reaches_any_log_column(self):
        """授权码是租户凭据：**任何日志列都不得出现它**（spec §5.4 的 M3b 凭据纪律）。

        两路构造：创建时带一个 canary、更新时换另一个 canary —— 正向对照是
        「两个 canary 都确实进了 `ShopBinding.access_token`」，然后要求它们在
        `OperationLog` 的**全部文本列**里都不出现。
        """
        canary_create = "CANARY-CREATE-TOKEN-9f3a"
        canary_update = "CANARY-UPDATE-TOKEN-1c7b"

        created = self.client.post(
            SHOP_BINDINGS,
            {
                "platform": 20,
                "shop_name": "凭据店",
                "access_token": canary_create,
                "remark": "备注",
            },
            content_type="application/json",
        )
        self._json(created)
        binding = ShopBinding.objects.get(shop_name="凭据店")
        self.assertEqual(binding.access_token, canary_create)

        patched = self.client.patch(
            f"{SHOP_BINDINGS}{binding.pk}/",
            {"access_token": canary_update},
            content_type="application/json",
        )
        self._json(patched)
        binding.refresh_from_db()
        self.assertEqual(binding.access_token, canary_update)

        text_columns = (
            "actor", "actor_name", "target_kind", "target_id", "target_name", "order_no",
            "source_order_no", "game_role_name", "game_name", "field_name",
            "from_value", "to_value", "reason", "summary",
        )
        dumped = "\n".join(
            getattr(row, column) or ""
            for row in OperationLog.objects.all()
            for column in text_columns
        )
        for canary in (canary_create, canary_update):
            self.assertNotIn(canary, dumped, "租户授权码进了操作日志列")
        # 两次写路径各一行（换 token 是真实变化 ⇒ 有行；但行里没有 token）
        self.assertEqual(OperationLog.objects.count(), 2)


class ConfigTargetKindVocabularyTests(SimpleTestCase):
    """R28：`target_kind` 只能是 spec §4.2 的枚举词表里的那一个。

    `OperationLog.target_kind` 是**自由 `CharField(16)`**（没有 `choices`、没有 DB 级枚举），
    所以「四个配置 ViewSet 各声明哪个 kind」这件事**只有测试能钉住**。自造词（如
    `game_server` / `project_category`）不在词表里 ⇒ 按「相关类型 = server」检索的人
    会漏掉每一行；而 `project_category` 又**恰好 16 字符**（= 列宽），一旦变长会被
    `record()` 按列宽**静默截断**成一个貌似合理、实则谁也不匹配的 kind。

    ⚠️ 期望值一律写字面串（E-0024）：不引 `viewset.audit_target_kind` 自指，也不引常量。
    """

    #: spec §4.2 行内原文的词表，**手抄**（不是从生产代码派生）。
    #: ⭐ M3b 追加 `order_source` / `shop_binding` 两个 kind（spec §4.2 的 M3b 补记）：
    #: C 端写路径落地了订单来源字典与店铺对接两张表，词表里原本没有它们的 kind。
    #: ⭐ `xgj_binding`（闲管家店铺对接，见 spec §4.2 的 Task L 补记）与
    #: `performance`（`performance.delete` 的 `target_id` 装的是**绩效主键**，
    #: 对标那句里就有「对应绩效ID」——与 settle / adjust_manual 落在 `user` 上不同，
    #: 那两条的对标句里没有绩效ID，见 spec §4.2 的 Task L 补记）。
    #: ⭐ `recognition_item`：改一行的关键词挂在
    #: **识别项**（`item_code`）上，不是游戏 / 区服之类的配置行（spec §3-D5）。
    #: ⚠️ 这个值**恰好 16 字符**（= `target_kind` 的列宽）⇒ 再多一个字符就会被
    #: `record()` 静默截断成谁也不匹配的 kind，所以下面那条字面值用例同时钉长度。
    SPEC_KINDS = frozenset(
        {"order", "user", "role", "game", "server", "project", "category", "operator"}
        | {"order_source", "shop_binding"}
        | {"xgj_binding", "performance"}
        | {"recognition_item"}
        # ⭐ 交易平台对接设置线：账号行 / 全局设置单行两个 kind。
        | {"platform_account", "platform_setting"}
        # ⭐ 公司店铺分组：`source_group`（12 字符，未撞 16 的列宽）。
        | {"source_group"}
    )

    def test_the_four_config_viewsets_declare_the_spec_kinds(self):
        declared = {
            GameViewSet: "game",
            GameServerViewSet: "server",
            ProjectCategoryViewSet: "category",
            ProjectViewSet: "project",
        }
        for viewset, literal in declared.items():
            with self.subTest(viewset=viewset.__name__):
                self.assertEqual(viewset.audit_target_kind, literal)
                self.assertIn(
                    viewset.audit_target_kind, self.SPEC_KINDS,
                    f"{viewset.__name__} 的 target_kind 不在 spec §4.2 词表里",
                )
                self.assertLessEqual(len(viewset.audit_target_kind), 16)

    def test_the_two_m3b_viewsets_declare_the_spec_kinds(self):
        """M3b 新增的两个域：字面值 + 词表成员 + ≤16 字符（同样写字面串，E-0024）。"""
        declared = {
            OrderSourceViewSet: "order_source",
            ShopBindingViewSet: "shop_binding",
        }
        for viewset, literal in declared.items():
            with self.subTest(viewset=viewset.__name__):
                self.assertEqual(viewset.audit_target_kind, literal)
                self.assertIn(
                    viewset.audit_target_kind, self.SPEC_KINDS,
                    f"{viewset.__name__} 的 target_kind 不在 spec §4.2 词表里",
                )
                self.assertLessEqual(
                    len(viewset.audit_target_kind), 16,
                    "超过 target_kind 的列宽（16）会被 record() 静默截断",
                )

    def test_the_source_group_viewset_declares_the_spec_kind(self):
        """公司店铺分组（2026-10-26）：字面值 + 词表成员 + ≤16 字符。

        ⚠️ `order_source_group` 是 18 字符、会被 `record()` 静默截断 ⇒ 这里钉 12 字符的
        `source_group`（计划 §3.2 写死的值）。
        """
        self.assertEqual(OrderSourceGroupViewSet.audit_target_kind, "source_group")
        self.assertIn(OrderSourceGroupViewSet.audit_target_kind, self.SPEC_KINDS)
        self.assertLessEqual(len("source_group"), 16)

    def test_the_task_l_xgj_viewset_declares_the_spec_kind(self):
        """Task L 新增的域（闲管家店铺对接）：字面值 + 词表成员 + ≤16 字符。

        ⚠️ 期望值写字面串（E-0024）：不引 `viewset.audit_target_kind` 自指。
        """
        self.assertEqual(XgjShopBindingViewSet.audit_target_kind, "xgj_binding")
        self.assertIn(XgjShopBindingViewSet.audit_target_kind, self.SPEC_KINDS)
        self.assertLessEqual(len("xgj_binding"), 16)

    def test_the_task_l_performance_kind_is_in_the_spec_vocabulary(self):
        """`performance.delete` 的 `target_kind` = `performance`（spec §4.2 的 Task L 补记）。

        ⚠️ 与 settle / adjust_manual 落在 `user` 上是**刻意的不同**：那两条的对标句里
        没有「绩效ID」，而删除那句里有 ⇒ 本动作的 `target_id` 装的是**绩效主键**。
        真正的行为断言（那一行的 `target_kind` / `target_id` 字面值）在
        「Task L：绩效删除 / 导出」用例组里（需要建租户夹具）。
        """
        self.assertIn("performance", self.SPEC_KINDS)
        self.assertIn("xgj_binding", self.SPEC_KINDS)

    def test_the_recognition_item_kind_is_in_the_spec_vocabulary(self):
        """录单识别设置线新增的 kind `recognition_item`：字面值 + 词表成员 + 长度。

        ⚠️ 期望值写字面串（E-0024）。真正的行为断言（那一行的 `target_kind` /
        `target_id` / `target_name` 字面值）在 `tests/test_recognition_settings_api.py` 里。
        ⚠️ 它**恰好 16 字符**（= `OperationLog.target_kind` 的列宽）：超一个字符就会被
        `record()` 静默截断。所以这里把长度也钉上，别让"刚好装下"变成"刚好溢出"。
        """
        self.assertIn("recognition_item", self.SPEC_KINDS)
        self.assertEqual(len("recognition_item"), 16)
        self.assertLessEqual(len("recognition_item"), 16)

    def test_the_platform_integration_kinds_fit_the_column_width(self):
        """交易平台对接设置线的两个 kind：字面值 + 词表成员 + **恰好 16 字符**。

        ⚠️ `OperationLog.target_kind` 是 `CharField(16)`，`record()` 超长会**静默截断**
        ⇒ 按类型检索的人会漏掉每一行。这里把长度钉死，别让"刚好装下"变成"刚好溢出"。
        ⚠️ 期望值写字面串（E-0024）：不从生产代码的常量派生。
        """
        for kind in ("platform_account", "platform_setting"):
            with self.subTest(kind=kind):
                self.assertIn(kind, self.SPEC_KINDS)
                self.assertEqual(len(kind), 16)

# ⛔ 词表里**没有** `jidanpu_binding`：租户级鸡蛋铺线的
# `apps/business/api/jidanpu_views.py` 整份删除 ⇒ 生产常量不存在、
# 也没有任何写路径会产出这个 kind（实测：`grep '"jidanpu_binding"' apps/` **零命中**）。
# ⚠️ 词表是**恰好集合**（下面那条 `SPEC_KINDS` 断言），这一项退场是**同步改准**，
# ⛔ 不是放宽。


# ── ⭐ 审计日志线：`record()` 的「操作人从哪来」源扫描网 ──────
#: 扫描根（相对 `src/`）与排除目录：与 `tests/test_doc_line_references.py` 同一口径。
_RECORD_SCAN_ROOTS = ("apps", "tests")
_RECORD_SCAN_EXCLUDED = frozenset({".venv", "__pycache__", "node_modules"})

#: 生产代码里 `record(` 的字面条数。写死是故意的：多一个或少一个埋点，本网先红。
#: 下面多次赋值，最后一个生效。不要按两侧读数相加。
_RECORD_PRODUCTION_CALL_SITES = 125
_RECORD_PRODUCTION_CALL_SITES = 126
_RECORD_PRODUCTION_CALL_SITES = 127
_RECORD_PRODUCTION_CALL_SITES = 128
# ⭐ 企微自建应用线（D-0379）：+1（PUT config 的留痕，`apps/wecom/views.py::WecomConfigView.put`）。
# ⭐ 同组金额拆分接口：+4（源单内容、组员内容、价格扣减、拆出新单）。
_RECORD_PRODUCTION_CALL_SITES = 136  # 含企微功能开关审计
_RECORD_PRODUCTION_CALL_SITES = 137  # 含结算明细导出审计
_RECORD_PRODUCTION_CALL_SITES = 139  # 含在线结算制单、取消和到账记账
_RECORD_PRODUCTION_CALL_SITES = 137  # 查看写入口和接单日志各自归并，加入撤单汇总
_RECORD_PRODUCTION_CALL_SITES = 138  # 审批中心配置审计
_RECORD_PRODUCTION_CALL_SITES = 140  # 含罚款订单审批和企微群聊关联留痕
_RECORD_PRODUCTION_CALL_SITES = 143  # SDK 应用配置、订单写入与打手审计
_RECORD_PRODUCTION_CALL_SITES = 144  # 工作室消息推送设置审计
_RECORD_PRODUCTION_CALL_SITES = 145  # 内部沟通通讯录设置审计
_RECORD_PRODUCTION_CALL_SITES = 146  # 鸡蛋铺下家直派结果审计
_RECORD_PRODUCTION_CALL_SITES = 147  # 企微售后纠纷设置保存审计
_RECORD_PRODUCTION_CALL_SITES = 148  # 同工作室 WxPusher 通知换绑审计
_RECORD_PRODUCTION_CALL_SITES = 149  # 内部沟通访客接待设置审计
_RECORD_PRODUCTION_CALL_SITES = 150  # 订单财务后台导出就绪审计
_RECORD_PRODUCTION_CALL_SITES = 153  # 访客客服接待状态、转接与结束审计
_RECORD_PRODUCTION_CALL_SITES = 154  # 内部沟通系统通知规则设置审计
#: 显式豁免只登记「判据自己的用例」这种必须漏传 actor_id 的地方。
#: 每一条必须恰好命中一个未声明的 record(；写错函数名或豁免过期，本网同样红。
#: 不用「理由文本里含某个词」当后门。
RECORD_SYSTEM_EXEMPTIONS = frozenset({
    # `record()` 的 fail-closed 判据自己的用例：它**必须**不传 `actor_id`、不声明 `system`
    # 才能证明「漏传会响亮失败」。
    ("tests/test_operation_log_model.py", "test_record_rejects_a_row_without_an_actor"),
})


class RecordActorSourceNet(SimpleTestCase):
    """⭐ `record(` 调用点的**源扫描**网：没有操作人 ⇒ 必须**显式**声明机器来源。

    ## 它守的是什么（本线那个洞的成因）

    业务口径是「**所有人为操作必须带上操作人**」，而这次的洞是「真人端点漏传
    `actor_id` ⇒ **静默**退化成机器行 ⇒ 正文里一个人名都没有，没有任何信号」。
    `record()` 的运行时 `ValueError`（`apps/audit/log.py`）是**最后**一道闸；
    本网是**写代码时**就看得见的那一道：**凡是不传 `actor_id`、或字面传
    `actor_id=None` 的调用，必须显式给 `system=True`**。

    ## 判据的边界（如实，⛔ 不要把「绿」读成「所有调用点都对」）

    - 判的是**关键字在不在 / 字面是不是 `None`**。`actor_id=user.pk if user is not None
      else None`、`actor_id=getattr(actor, "pk", None)` 这类**值级**可能是 `None` 的写法
      **不在本网判据内**——它们由 `record()` 的运行时 `ValueError` 兜底（那才是真正的
      fail-closed）；本网负责另一半：**写成代码时就能看见**的那一半。那些值级的点逐条
      自己传了 `system=<同一条件>`（本网看不见，靠用例与代码注释守）。
    - 它**不判**「人为的调用点传的 `actor_id` 是不是**对的那个**人」（那要业务语义），
      也不判 `system=True` 用得对不对（那是评审的事）。

    ## 判别力自证

    把任意一个 `system=True` 拿掉 ⇒ 本网红（变异自证）。
    """

    @staticmethod
    def _collect():
        """扫 `src/apps/**` + `src/tests/**` 的 `record(` 调用点，返回 (生产, 全部) 两个列表。

        每项 = `(相对 `src/` 的路径, 行号, 所在函数名, 是否「没有 pk」, 是否字面 `system=True`)`。
        """
        import ast
        from pathlib import Path

        src = Path(__file__).resolve().parents[1]
        found = []
        for root in _RECORD_SCAN_ROOTS:
            for path in sorted((src / root).rglob("*.py")):
                if _RECORD_SCAN_EXCLUDED & set(path.parts):
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                parents = {}
                for node in ast.walk(tree):
                    for child in ast.iter_child_nodes(node):
                        parents[child] = node
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    is_record = (
                        (isinstance(func, ast.Name) and func.id == "record")
                        or (isinstance(func, ast.Attribute) and func.attr == "record")
                    )
                    if not is_record:
                        continue
                    kwargs = {k.arg: k.value for k in node.keywords if k.arg}
                    # 「没有 pk」= 不传 `actor_id`，或字面传 `None`。
                    if "actor_id" not in kwargs:
                        no_pk = True
                    else:
                        value = kwargs["actor_id"]
                        no_pk = (
                            isinstance(value, ast.Constant) and value.value is None
                        )
                    system = kwargs.get("system")
                    declared = (
                        "system" in kwargs
                        and isinstance(system, ast.Constant)
                        and system.value is True
                    )
                    # 所在函数名（用例名 / 生产函数名）——豁免按它登记，⛔ 不用行号。
                    holder = node
                    name = "<module>"
                    while holder in parents:
                        holder = parents[holder]
                        if isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            name = holder.name
                            break
                    found.append(
                        (
                            path.relative_to(src).as_posix(),
                            node.lineno,
                            name,
                            no_pk,
                            declared,
                        )
                    )
        production = [row for row in found if row[0].startswith("apps/")]
        return production, found

    def test_every_call_without_an_actor_id_declares_a_system_source(self):
        production, found = self._collect()
        # ── 前置断言（防「扫了个寂寞」）────────────────────────────────────
        # ⚠️ 生产调用点是**字面条数**：新增/删除埋点时必须当场重数（见常量注释）。
        self.assertEqual(
            len(production), _RECORD_PRODUCTION_CALL_SITES,
            "`record(` 的生产调用点数变了——重新数一遍并把常量改准",
        )
        self.assertGreaterEqual(len(found), _RECORD_PRODUCTION_CALL_SITES)

        violations = []
        exempt_hits = {}
        for path, lineno, func, no_pk, declared in found:
            if not no_pk or declared:
                continue
            key = (path, func)
            if key in RECORD_SYSTEM_EXEMPTIONS:
                exempt_hits[key] = exempt_hits.get(key, 0) + 1
                continue
            violations.append(f"{path}:{lineno}（{func}）")
        self.assertEqual(
            violations, [],
            "这些 record() 调用点没有操作人、也没有显式声明 system=True："
            + "、".join(violations),
        )
        # ── 豁免必须**逐条命中且只命中一次**（豁免过期 / 写错函数名 ⇒ 红）──────
        self.assertEqual(
            exempt_hits,
            {key: 1 for key in RECORD_SYSTEM_EXEMPTIONS},
            "豁免清单与实测的未声明调用点对不上（豁免要么过期、要么一个调用点被算了多次）",
        )

    def test_the_net_sees_the_known_shapes(self):
        """⭐ **网自己的阳性对照**（E-0065：结构性判据必须证明它抓得到目标形状）。

        ⚠️ 本类用 `SimpleTestCase`（不许查库）⇒ 这里只证明**扫描器**认得两种形状：
        ① 「不传 `actor_id`」被算成「没有 pk」；② 同一个调用点加了 `system=True` 就不算。
        「不传 `actor_id` 的调用**真的**会被 `record()` 抛 `ValueError`」那一条打在真实出口上、
        由 `tests/test_operation_log_model.py::RecordTests::test_record_rejects_a_row_without_an_actor`
        钉着（那里有数据库、能断言「一行都没写」）。
        """
        import ast

        samples = {
            "no_pk_no_system": 'record(log_type=10, action="a", actor="甲")',
            "no_pk_with_system": 'record(log_type=10, action="a", actor="甲", system=True)',
            "pk_no_system": 'record(log_type=10, action="a", actor="甲", actor_id=1)',
            "none_pk_with_system": (
                'record(log_type=10, action="a", actor="甲", actor_id=None, system=True)'
            ),
        }
        got = {}
        for name, source in samples.items():
            tree = ast.parse(source)
            call = tree.body[0].value
            kwargs = {k.arg: k.value for k in call.keywords if k.arg}
            no_pk = "actor_id" not in kwargs or (
                isinstance(kwargs["actor_id"], ast.Constant)
                and kwargs["actor_id"].value is None
            )
            system = kwargs.get("system")
            declared = isinstance(system, ast.Constant) and system.value is True
            got[name] = (no_pk, declared)
        self.assertEqual(
            got,
            {
                "no_pk_no_system": (True, False),
                "no_pk_with_system": (True, True),
                "pk_no_system": (False, False),
                "none_pk_with_system": (True, True),
            },
        )
