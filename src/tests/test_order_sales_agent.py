"""换接单销售客服**归属**（业务方 2026-10-08 拍板「换销售客服 = 提成也换人」，本线 `feat/sales-agent`）。

## 本文件判什么

一个**专用端点** `POST /api/business/orders/<单号>/sales-agent/`（入参 = 目标用户**主键**）
把三件事在**同一个事务**里做完：

1. `Order.sales_user`（决定客服提成归谁的 `FK`）；
2. 该单 SALES 绩效行的 `user`（缺行则补建）；
3. `Order.sales_agent`（**文本快照**，值 = 目标用户的显示名）+ 金额按**新人**费率立刻重算。

外加**录单那一路**（`POST /orders/` 多带一个非订单列的键 `sales_user`）：缺键 = 归属 = 认证态
（既有行为一个字不改）；持节点才收，不持却带 ⇒ **HTTP 403**。

## 每条用例的判别力（⛔ 不写恒真断言）

- 「成功」那几条**三个终态都断**：`Order.sales_user` / SALES 行 `user` / `Order.sales_agent` 的
  **名字**（只看其中两个，会让「只改了归属没搬绩效行」或「名字还是旧的」这两种半成品逃过去）；
- 「已结账 ⇒ 拒绝」那条**额外断「库里一个字节没变」**：订单三列 + SALES 行 + 日志行数
  （只断 HTTP 码，会让「先写了一半再拒」的脏现场逃过去 —— E-0022 家族）；
- 「目标是本单已有绩效行的人 ⇒ 拒绝」那条**断 HTTP 不是 500**（那是撞
  `uniq_performance_order_user` 抛 `IntegrityError` 的形状）；
- 金额那两条**造两个费率不同的用户**并断 `bonus_amount` / `commission_rate` **真的变了**
  （⛔ 不断常量：那会在常量被改坏时照样绿，E-0024）；
- 留痕断言打在**数据库行**上（E-0009），并断 `from_value` / `to_value` 是**两个不同的名字**
  （只断「有一行日志」会让「一句『已更新』」也通过）。

⚠️ 夹具一律**合成值**（E-0077）；候选口径读的是 `Role.can_be_sales_agent`
（`groups__role__can_be_sales_agent=True`）⇒ 只建 `Group` 不给 `Role` 的人**不进候选**
（理由与 `tests/test_order_kefu_options.py::_role` 同源，那两处都写着）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.commission import CommissionError
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.performance import create_performance_for_order
from apps.core.error_codes import ErrorCode
from apps.rbac.models import Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 本端点（`url_path="sales-agent"`、`detail=True`）。
ENDPOINT = "/api/business/orders/{order_no}/sales-agent/"
#: 本线新增的节点（业务方原话：**只用这一个**）。
NODE = "order.allow_change_sales_agent"
#: 录单端点。
INTAKE_URL = "/api/business/orders/"

#: 三个合成账号（昵称与登录账号**不同形** ⇒ 断「名字」时才分得清口径对不对）。
KEFU_USERNAME = "kefu-jia"
KEFU_NICKNAME = "测试昵称甲"
NEW_KEFU_USERNAME = "kefu-yi"
NEW_KEFU_NICKNAME = "测试昵称乙"
TAKER_USERNAME = "dailian-bing"
TAKER_NICKNAME = "测试昵称丙"
OUTSIDER_USERNAME = "kefu-ding"
OUTSIDER_NICKNAME = "测试昵称丁"

#: 两个费率不同的客服（金额用例的判别力来源；⛔ 用**字面十进制值**当期望，不用常量，E-0024）。
RATE_A = Decimal("20.00")
RATE_B = Decimal("35.00")
#: 订单金额与打手绩效：利润 = 108.00 − 60.00 = **48.00**（与 `test_customer_commission.py` 同口径）。
TOTAL_PRICE = Decimal("108.00")
TAKER_BONUS = Decimal("60.00")
#: 期望的客服提成：48.00 × 20% = **9.60**；48.00 × 35% = **16.80**。
BONUS_AT_RATE_A = Decimal("9.60")
BONUS_AT_RATE_B = Decimal("16.80")


def _role(name, *, flag):
    """建 / 取职位 `name` 并把「能不能当销售客服」设成 `flag`（幂等）。

    候选口径读的是 `Role.can_be_sales_agent`（`groups__role__can_be_sales_agent`），
    只建 `Group` 的人**不进候选** ⇒ 本文件所有「该进候选」的账号都必须经这里。
    `flag` 必填关键字参数：忘了设标记与故意不设是两件相反的事。
    """
    group, _ = Group.objects.get_or_create(name=name)
    role, _was_created = Role.objects.get_or_create(name=name, defaults={"group": group})
    Role.objects.filter(pk=role.pk).update(can_be_sales_agent=flag)
    return role


def _staff(username, nickname, *, group, role_flag):
    """建一个带职位（并显式声明其标记）的账号。"""
    user = User.objects.create_user(username=username)
    user.first_name = nickname
    user.save(update_fields=["first_name"])
    _role(group, flag=role_flag)
    user.groups.add(Group.objects.get(name=group))
    return user


def _write_client(username, *, nodes, group="操作组"):
    """持指定节点的真实登录态（`games` 走夹具默认：当前租户全部游戏）。

    ⚠️ **`groups` 必须一起给**：`make_logged_in_client(nodes=…)` 只把节点挂到用户**已有的**
    Group 对应的 `Role` 上（`p5_auth_helpers._grant_nodes`）——不给组时 `groups` 是空集、
    循环一次都不进 ⇒ **一个节点都没挂上**、每个请求都以 403 回来（本文件第一次跑就是这么红的）。
    ⚠️ 组名用**非内置职位**的短名（`Role.name` 上限 5 字）：⛔ 不能图省事用「系统管理员」
    ——那个组在 `is_tenant_admin` 里是**管理员短路**，会把「有节点 / 没节点」的判据变成恒真。

    ⚠️ **`group` 参数是承重的，不是方便**：① `Role.permissions` 是**累积**的（`_grant_nodes`
    往同一个 Role 上 `add`）⇒ 同一个 `setUp` 里两次 `_write_client` 如果共用一个组名，
    第二次的节点会**顺带给第一个账号**（或反过来），「不持节点」那条用例于是**恒假**
    （本文件实测踩到：`test_without_the_node_…` 两次都以 200 而不是 403 回来）；
    ② `Role` 是 `name` 唯一 + `group` OneToOne ⇒ 不同组名 = 不同 Role = 互不污染。
    ⇒ 每个要判「有没有这个节点」的账号**各用一个组名**。
    """
    return make_logged_in_client(
        username=username, groups=(group,), nodes=nodes
    )


class _SalesAgentBase(TenantTestCase):
    """公共夹具：一个游戏、三个账号（当前归属人 / 候选新人 / 本单打手）+ 一张待验收的单。

    ⚠️ 订单用**生产同一入口**建绩效行（`create_performance_for_order`）⇒ 夹具与线上一致
    （尤其是 SALES 行的 `user` 就是 `order.sales_user` 这条不变量）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 当前归属人（客服，带标记 ⇒ 在候选里）+ **费率 20%**。
        self.owner = _staff(
            KEFU_USERNAME, KEFU_NICKNAME, group="客服", role_flag=True
        )
        StaffProfile.objects.create(
            user=self.owner,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=RATE_A,
        )
        # 目标新人（客服，带标记 ⇒ 在候选里）+ **费率 35%**（与上面不同 ⇒ 金额会真的变）。
        self.newcomer = _staff(
            NEW_KEFU_USERNAME, NEW_KEFU_NICKNAME, group="客服", role_flag=True
        )
        StaffProfile.objects.create(
            user=self.newcomer,
            commission_mode=CommissionMode.PROFIT_PERCENT,
            commission_value=RATE_B,
        )
        # 不在候选里的人（代练员职位**不带标记**）——反证半。
        self.outsider = _staff(
            OUTSIDER_USERNAME, OUTSIDER_NICKNAME, group="代练员", role_flag=False
        )
        # 本单的打手（**已在本单有一条绩效行**的人——判据 ③ 的判别对象）。
        self.taker = _staff(
            TAKER_USERNAME, TAKER_NICKNAME, group="代练员", role_flag=False
        )
        # 当前登录人（持该节点）；他也可能是这条单的销售客服（幂等那条要它）。
        self.client = _write_client(
            "cs-operator", nodes=("order.add_edit", NODE), group="有节点组"
        )
        self.operator = User.objects.get(username="cs-operator")

    def _make_order(self, *, status=OrderStatus.PENDING_REVIEW, sales_user=None):
        """建一张单 + 两行绩效（用生产同一入口），返回 `(order, customer_row)`。

        ⚠️ 单号用**递增字面量**（本文件每类用例各建一两张单，Testcase 之间会回滚 ⇒ 不会撞）。
        """
        self._seq = getattr(self, "_seq", 0) + 1
        order = Order.objects.create(
            game=self.game,
            order_no=f"2026100800000000{self._seq:04d}",
            status=status,
            hall_price=Decimal("88.00"),
            total_price=TOTAL_PRICE,
            received_price=Decimal("1.00"),   # 陷阱值：提成基数不看它（D-0142）
            sales_user=sales_user if sales_user is not None else self.owner,
            # ⚠️ **必须显式给**：录单兜底只活在 `order_intake.create_order` 里。本夹具直接
            # `Order.objects.create(...)` ⇒ 不给就是空串，「名字没被改坏」会因夹具而红。
            sales_agent=KEFU_NICKNAME,
        )
        create_performance_for_order(order)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        main.user = self.taker
        main.temp_amount = TAKER_BONUS
        main.bonus_amount = TAKER_BONUS
        main.review_status = ReviewStatus.APPROVED
        main.save(
            update_fields=[
                "user", "temp_amount", "bonus_amount", "review_status", "updated_at"
            ]
        )
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        return order, customer

    def _post(self, order, *, user_id, client=None):
        return (client or self.client).post(
            ENDPOINT.format(order_no=order.order_no),
            {"user": user_id},
            content_type="application/json",
        )

    def _assert_row(self, order, *, user_id, nickname):
        """三个终态**一起**断（`Order.sales_user` / SALES 行 `user` / 快照名字）。

        ⛔ 少断任何一个都会让半成品逃过去：只断订单列 ⇒ 「绩效行没搬」看不见；
        只断绩效行 ⇒ 「名字还是旧的」看不见（而名字正是用户在列表页上看到的那一列）。
        """
        order.refresh_from_db()
        self.assertEqual(order.sales_user_id, user_id)
        self.assertEqual(order.sales_agent, nickname)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.user_id, user_id)
        return customer


class ChangeSalesAgentTests(_SalesAgentBase):
    """端点的正路：换了归属、搬了绩效行、同步了名字。"""

    def test_the_order_row_is_locked_before_the_sales_performance_row(self):
        """⭐ I4（终审修复轮）：`/sales-agent/` 统一锁序 —— 先锁 Order、再锁 Performance。

        这条边原先在视图里先锁 SALES 绩效行（Z1 的 TOCTOU 修复），订单行锁要等
        `change_sales_agent` 才拿到 ⇒ 与 `/assign/` 方向相反。判据是**这次调用发出的
        SQL 顺序**。判别力：把视图里的 `lock_order_row(order)` 删掉 ⇒ 本用例红。
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from tests.lock_order_helpers import assert_order_locked_before_performance

        order, _customer = self._make_order()
        with CaptureQueriesContext(connection) as captured:
            body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertEqual(body["code"], 0, body)
        assert_order_locked_before_performance(self, captured)
        # 哨兵：归属真的换过去了（不是在一笔失败的调用上断锁序）。
        order.refresh_from_db()
        self.assertEqual(order.sales_user_id, self.newcomer.pk)

    def test_it_moves_the_owner_the_performance_row_and_the_display_name(self):
        order, customer = self._make_order()
        self.assertEqual(customer.user_id, self.owner.pk)   # 构造物：换之前确实是甲

        body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertEqual(body["code"], 0)
        # 三个终态一起断（见 `_assert_row`）。
        self._assert_row(order, user_id=self.newcomer.pk, nickname=NEW_KEFU_NICKNAME)
        # 响应体里也是新人（前端据此刷新那一格，不必再发一次详情）。
        self.assertEqual(body["data"]["sales_user"], self.newcomer.pk)
        self.assertEqual(body["data"]["sales_agent"], NEW_KEFU_NICKNAME)

    def test_it_records_who_moved_the_ownership_from_whom_to_whom(self):
        """留痕断在**数据库行**上（E-0009），并断前后值是**两个不同的名字**。

        只断「有一行 action=... 的日志」会让「一句『已更新』」也通过 —— 而业务要看的是
        「从谁改到谁」（本线需求原话就是这么说的）。
        """
        order, _customer = self._make_order()
        self._post(order, user_id=self.newcomer.pk)

        row = OperationLog.objects.get(
            order_no=order.order_no, action="order.change_sales_agent"
        )
        self.assertEqual(row.log_type, 10)                  # 订单相关，字面值（E-0024）
        self.assertEqual(row.from_value, KEFU_NICKNAME)
        self.assertEqual(row.to_value, NEW_KEFU_NICKNAME)
        self.assertEqual(row.field_name, "sales_agent")
        self.assertEqual(row.actor, "cs-operator")          # 操作人取认证态（R20）
        self.assertIn(KEFU_NICKNAME, row.summary)
        self.assertIn(NEW_KEFU_NICKNAME, row.summary)

    def test_it_records_a_readable_summary_when_nobody_owned_the_order(self):
        """⭐ 修复轮 F1：**原本没有归属人**时，审计正文必须可读（⛔ 不是病句「…从改为Y」）。

        ## 这条用例抓的是什么

        `ORDER_CHANGE_SALES_AGENT` 的模板是「…从{from_value}改为{to_value}」，而模板层
        **不做**「空值 ⇒ 未设置」这层兜底（模板的纪律：渲染输入只能来自行内列）。写入侧若
        照旧传空串，`from_value=""` 就会渲染成「**将订单 X 的接单销售客服从改为测试昵称乙**」
        ——一句**病句**，而它恰恰是「这单原本无主」这一档唯一的证据。

        ## 判据

        断**值**（⛔ 不断言模板常量本身——那种自指断言在常量被改坏时照样绿，E-0024）：
        `from_value` 是可读的「（无）」、正文里含「（无）」与**目标显示名**、且不含病句片段。
        """
        # 构造物：这一单**没有归属人**（`sales_user=None` ⇒ 也建不出 SALES 行），
        # 而那一列历史上可能留着别的东西 ⇒ 本条的判据只看「归属人是谁」。
        order = Order.objects.create(
            game=self.game,
            order_no="20261008000000009100",
            status=OrderStatus.PENDING_REVIEW,
            hall_price=Decimal("88.00"),
            total_price=TOTAL_PRICE,
            sales_user=None,
        )
        create_performance_for_order(order)

        body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertEqual(body["code"], 0)
        self._assert_row(order, user_id=self.newcomer.pk, nickname=NEW_KEFU_NICKNAME)

        row = OperationLog.objects.get(
            order_no=order.order_no, action="order.change_sales_agent"
        )
        self.assertEqual(row.from_value, "（无）")
        self.assertEqual(row.to_value, NEW_KEFU_NICKNAME)
        self.assertIn("（无）", row.summary)
        self.assertIn(NEW_KEFU_NICKNAME, row.summary)
        self.assertNotIn("从改为", row.summary)          # 病句片段（修复前的渲染结果）

    def test_the_log_names_are_clipped_like_the_column(self):
        """⭐ 修复轮 S3：超长显示名 ⇒ **日志里说的名字与库里存的名字逐字相同**（都裁到列宽）。

        判据形状：先造一个「显示名远长于 `sales_agent` 列宽」的候选人，改归属后
        断言 `to_value == Order.sales_agent`（**两处相等于值**，而不是各自与一个常量比——
        后者在列宽或裁剪口径被改坏时照样绿）。
        """
        long_name = "超长测试昵称" * 20                              # 120 字 > varchar(64)
        newcomer = _staff(
            "kefu-chang", long_name, group="客服", role_flag=True
        )
        order, _customer = self._make_order()

        body = self._post(order, user_id=newcomer.pk).json()
        self.assertEqual(body["code"], 0)

        order.refresh_from_db()
        row = OperationLog.objects.get(
            order_no=order.order_no, action="order.change_sales_agent"
        )
        self.assertEqual(row.to_value, order.sales_agent)          # 日志 == 库里那一列
        self.assertEqual(len(order.sales_agent), 64)              # 构造物真的超长了
        self.assertIn(row.to_value, row.summary)                  # 正文里也是这个值

    def test_a_missing_customer_row_is_created(self):
        """SALES 行**不存在**时按既有函数补建（`recompute_customer_commission_write` 的先例）。

        ⚠️ 构造物哨兵在前面：先断「真的没有 SALES 行」，否则这条用例在「行还在」时
        照样绿、等于什么都没测（`sales_user` 为空那一档建不出 SALES 行，
        正是 `performance.create_customer_performance` 的早退分支）。
        """
        order = Order.objects.create(
            game=self.game,
            order_no="20261008000000009001",
            status=OrderStatus.PENDING_REVIEW,
            hall_price=Decimal("88.00"),
            total_price=TOTAL_PRICE,
            sales_user=None,          # ⇒ 录单那次没有建 SALES 行
        )
        create_performance_for_order(order)
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.SALES).exists()
        )

        body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertEqual(body["code"], 0)
        customer = self._assert_row(
            order, user_id=self.newcomer.pk, nickname=NEW_KEFU_NICKNAME
        )
        # 补建出来的行是**初始态**（与录单同形）：钱由下面那次重算写。
        self.assertEqual(customer.temp_amount, Decimal("0.00"))

    def test_the_same_user_is_a_no_op_and_writes_no_fake_change_row(self):
        """目标是**现在的**归属人、且名字快照**已经一致** ⇒ 幂等成功，且**不留**一条
        「把甲改成甲」的假变更。

        判据两半：① 业务码 0；② `order.change_sales_agent` 的日志行数**恰好 0**
        （`order.update` 那种「同值不留痕」的同一条口径）。

        ⚠️ **2026-10-08 修复轮（F4.3）改准**：本条原先的标题是「什么都不做」——那一版
        在名字快照**已经被改脏**时会**留着脏值成功返回**（红队构造的现场：列表显示乙、
        提成归甲）。新口径下这一档会**先对齐名字**（见
        `test_the_idempotent_call_repairs_a_dirty_display_name`），所以本条只覆盖
        「**本来就一致** ⇒ 一行都不写」这半边；`updated_at` 不变那条断言在**这一档**仍然成立。
        """
        order, _customer = self._make_order()
        before = Order.objects.get(pk=order.pk).updated_at

        body = self._post(order, user_id=self.owner.pk).json()
        self.assertEqual(body["code"], 0)
        self._assert_row(order, user_id=self.owner.pk, nickname=KEFU_NICKNAME)
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=order.order_no, action="order.change_sales_agent"
            ).count(),
            0,
        )
        self.assertEqual(Order.objects.get(pk=order.pk).updated_at, before)  # 一行没写

    def test_the_idempotent_call_repairs_a_dirty_display_name(self):
        """⭐ 修复轮 F4.3（红队构造的现场）：名字快照被改脏之后，幂等调用必须把它**对齐回归属人**。

        ## 这条用例抓的是什么

        `Order.sales_agent`（**列表显示的那一列**）与 `Order.sales_user`（**提成归谁**）必须
        指向同一个人。修复轮之前，「目标 == 现归属人」这一档**什么都不做就成功返回**
        ⇒ 只要那一列被单独改脏过（旧版的通用 PATCH 就能做到，见
        `test_patch_with_a_different_sales_agent_is_rejected` 的同族），
        「**列表显示乙、提成归甲**」就会**永久留着**，而且没有任何信号。

        ## 判据（三个终态一起断）

        ① 名字列 == 归属人的显示名（值来自**服务端**，⛔ 不是断言常量）；② 归属人没被换；
        ③ 仍然**没有** `order.change_sales_agent` 留痕（「没有发生变更」这件事不变——
        对齐不是一次换人）。
        """
        order, customer = self._make_order()
        # 构造物：把那一列**改脏**（绕过模型 save，模拟旧版 PATCH 单独写这一列）。
        # ⚠️ 哨兵：先断「现在确实是脏的」，否则本条在实现没生效时也会绿。
        Order.objects.filter(pk=order.pk).update(sales_agent="被改脏的测试昵称")
        order.refresh_from_db()
        self.assertNotEqual(order.sales_agent, KEFU_NICKNAME)

        body = self._post(order, user_id=self.owner.pk).json()

        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.sales_agent, KEFU_NICKNAME)      # 已对齐（值 = 归属人显示名）
        self.assertEqual(order.sales_user_id, self.owner.pk)     # 归属人没被换
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.owner.pk)        # 绩效行也没被搬
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=order.order_no, action="order.change_sales_agent"
            ).count(),
            0,
        )


class ChangeSalesAgentRejectionTests(_SalesAgentBase):
    """四条拒绝判据（每条都要能分辨对错）。"""

    def test_a_settled_customer_row_is_rejected_and_changes_nothing(self):
        """① 已结账 ⇒ 业务码 `1014`，且**库里一个字节没变**。

        判据不止 HTTP 码：订单三列 + SALES 行归属 + `sales_agent` 名字 + 日志行数**全都断**。
        只看业务码会让「先改了 `sales_user` 再拒」这种半成品逃过去（E-0022 家族，
        而那正是任务书要求「这道闸必须在改任何东西之前」的原因）。
        """
        order, customer = self._make_order()
        customer.settle_status = SettleStatus.SETTLED
        customer.settled_amount = Decimal("9.60")
        customer.save(update_fields=["settle_status", "settled_amount", "updated_at"])
        before = {
            "sales_user_id": Order.objects.get(pk=order.pk).sales_user_id,
            "sales_agent": Order.objects.get(pk=order.pk).sales_agent,
            "updated_at": Order.objects.get(pk=order.pk).updated_at,
        }

        body = self._post(order, user_id=self.newcomer.pk).json()

        self.assertEqual(body["code"], 1014)
        self.assertIn("已结账", body["msg"])
        self.assertEqual(
            {
                "sales_user_id": Order.objects.get(pk=order.pk).sales_user_id,
                "sales_agent": Order.objects.get(pk=order.pk).sales_agent,
                "updated_at": Order.objects.get(pk=order.pk).updated_at,
            },
            before,
        )
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.owner.pk)     # 绩效行**没被搬**
        self.assertEqual(
            OperationLog.objects.filter(order_no=order.order_no).count(), 0
        )

    def test_a_user_outside_the_candidate_pool_is_rejected(self):
        """② 不在候选范围 ⇒ 业务码 `1000`。

        `outsider` 是「代练员」职位的人，而该职位**没有** `can_be_sales_agent` 标记；
        他也不等于当前登录人 ⇒ 两半都不命中。
        ⚠️ 阳性对照同批：把同一个请求发给 `newcomer`（带标记）⇒ 成功
        （否则「拒绝」可能只是端点坏了，而不是判据生效）。
        """
        order, _customer = self._make_order()
        body = self._post(order, user_id=self.outsider.pk).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("候选", body["msg"])
        self._assert_row(order, user_id=self.owner.pk, nickname=KEFU_NICKNAME)

        self.assertEqual(self._post(order, user_id=self.newcomer.pk).json()["code"], 0)

    def test_a_user_with_an_existing_performance_row_is_rejected_without_a_500(self):
        """③ 目标是**本单已有绩效行**的人 ⇒ 业务码 `1000`，**不是 500**。

        机制：`Performance` 的唯一约束 `uniq_performance_order_user` 是 `(order, user)`、
        **不含 `emp_type`**（`apps/business/models.py`）⇒ 直接改会撞约束抛
        `IntegrityError`。所以本用例**显式断状态码**：`500` 是这条闸缺失时的症状。

        ⚠️ **构造物是承重的**：这个打手**必须同时落在候选范围里**，否则请求会先撞②那道
        「不在候选」的闸、回的是另一句话 —— 那样本用例测的是②，③的判别力等于零
        （本文件实测踩到：第一版拿「代练员职位」的打手当目标，回的是
        「该用户不在销售客服候选范围内」）。所以这里**当场**把他的职位标成
        「可作为销售客服」（候选口径读的就是这个标记，`_role` 是它的唯一入口）。
        """
        _role("代练员", flag=True)
        order, _customer = self._make_order()
        response = self._post(order, user_id=self.taker.pk)

        self.assertEqual(response.status_code, 200)            # ⛔ 不是 500
        body = response.json()
        self.assertEqual(body["code"], 1000)
        self.assertIn(TAKER_NICKNAME, body["msg"])
        self.assertIn("接单人", body["msg"])
        self._assert_row(order, user_id=self.owner.pk, nickname=KEFU_NICKNAME)

    def test_without_the_node_the_endpoint_is_forbidden(self):
        """④ 不持 `order.allow_change_sales_agent` ⇒ **HTTP 403**（fail-closed 的节点门）。

        夹具只挂 `order.add_edit` ⇒ 「能编辑订单」**不等于**「能把提成搬给别人」。
        """
        order, _customer = self._make_order()
        other = _write_client("cs-no-node", nodes=("order.add_edit",), group="无节点组")

        response = self._post(order, user_id=self.newcomer.pk, client=other)

        self.assertEqual(response.status_code, 403)
        self._assert_row(order, user_id=self.owner.pk, nickname=KEFU_NICKNAME)

    def test_an_unknown_user_id_is_rejected(self):
        """目标主键不存在 ⇒ `1000`「指定的用户不存在」（与 `/assign/` 同款文案）。"""
        order, _customer = self._make_order()
        body = self._post(order, user_id=999999).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("不存在", body["msg"])
        self._assert_row(order, user_id=self.owner.pk, nickname=KEFU_NICKNAME)

    def test_the_body_must_carry_the_primary_key(self):
        """缺 `user` ⇒ DRF 字段校验回 `1000`、一行不写（⛔ 不是 HTML 500）。"""
        order, _customer = self._make_order()
        response = self.client.post(
            ENDPOINT.format(order_no=order.order_no), {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self._assert_row(order, user_id=self.owner.pk, nickname=KEFU_NICKNAME)


class ChangeSalesAgentAmountTests(_SalesAgentBase):
    """金额：未完成 ⇒ 不动；已完成 ⇒ 按**新人**的费率重算。

    ⚠️ 两个用户费率不同（20% / 35%）⇒ 「真的按新人重算」有判别力；
    ⛔ 不断言常量（E-0024：常量被改坏时那种断言照样绿）。
    """

    def _completed_order(self):
        order, customer = self._make_order(status=OrderStatus.COMPLETED)
        # 直接落「完成时已确定」的现场：甲（20%）⇒ 9.60。
        customer.bonus_amount = BONUS_AT_RATE_A
        customer.commission_rate = RATE_A
        customer.review_status = ReviewStatus.APPROVED
        customer.save(
            update_fields=[
                "bonus_amount", "commission_rate", "review_status", "updated_at"
            ]
        )
        return order, customer

    def test_a_completed_order_recomputes_the_amount_at_the_new_rate(self):
        order, customer = self._completed_order()
        body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertEqual(body["code"], 0)

        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.newcomer.pk)
        self.assertEqual(customer.bonus_amount, BONUS_AT_RATE_B)      # 16.80（不是 9.60）
        self.assertEqual(customer.commission_rate, RATE_B)
        self.assertEqual(customer.review_status, ReviewStatus.APPROVED)

    def test_an_unfinished_order_keeps_the_amount_and_still_has_the_row(self):
        """订单**不是**已完成 ⇒ 金额不动、行存在（与
        `settlement_hooks.recompute_customer_commission` 的既有口径同源）。

        ⚠️ 构造物哨兵：先把 SALES 行的金额摆成一组**非零且与两个费率都不同**的值，
        这样「没被写」与「被写成了 0」才分得开（都留 0.00 的话这条用例是恒真的）。
        """
        order, customer = self._make_order(status=OrderStatus.PENDING_REVIEW)
        customer.bonus_amount = Decimal("3.33")
        customer.commission_rate = Decimal("7.00")
        customer.save(update_fields=["bonus_amount", "commission_rate", "updated_at"])

        body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertEqual(body["code"], 0)

        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.newcomer.pk)          # 归属搬了
        self.assertEqual(customer.bonus_amount, Decimal("3.33"))      # 金额**没动**
        self.assertEqual(customer.commission_rate, Decimal("7.00"))


class GenericPatchStillCannotReassignTests(_SalesAgentBase):
    """通用 PATCH 仍然**不能**改 `sales_user`（既有的网，本线确认它还绿）。

    ⚠️ 与端点那几条**成对**才有意义：本线**新开了一条能改归属的路**，
    所以「通用 PATCH 那条旧路仍然关着」必须同批有判据（否则「钱只走专用端点」是空话）。
    """

    def test_patch_cannot_reassign_the_sales_user(self):
        order, customer = self._make_order()
        response = self.client.patch(
            f"/api/business/orders/{order.order_no}/",
            {
                # ⚠️ 值必须是**合法的目标**（在候选里、且不是本单已有绩效行的人）——
                # 否则这条用例会因为「新人用不了」而红，而不是因为「PATCH 不收归属」。
                "sales_user": self.newcomer.pk,
                # ⚠️ 名字**保持不变**（换成与现值不同的名字会撞上「值变了就要求权限」那道闸，
                # 那是另一条判据、与「PATCH 改不了归属」无关）。
                "sales_agent": KEFU_NICKNAME,
                "tag": "被改过的备注",
                # ⚠️ **必带**：`update_order_fields` 的必填判据（「代练/陪玩项目是必填项」）
                # 与录单同源 ⇒ 一张只有 `project_text` 的单也必须把它原样带回来，
                # 否则本用例会因为一个与「PATCH 收不收归属」无关的原因红（实测踩到）。
                "project_text": "满星",
            },
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)

        order.refresh_from_db()
        self.assertEqual(order.tag, "被改过的备注")            # 哨兵：PATCH 真的生效了
        self.assertEqual(order.sales_user_id, self.owner.pk)   # 归属**没变**
        self.assertEqual(order.sales_agent, KEFU_NICKNAME)     # 名字也没变
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.owner.pk)      # 绩效行与订单**两侧一致**

    def test_patch_with_a_different_sales_agent_is_rejected(self):
        """⭐ 修复轮 F4.1：请求体带一个**与本单现值不同**的 `sales_agent` ⇒ 明确拒绝、库里一个字不变。

        ## 为什么持节点也拒

        夹具的登录人（`cs-operator`）**持** `order.allow_change_sales_agent` ⇒ 这一档在修复轮
        之前是**放行**的：那一列被单独改掉，而 `Order.sales_user`（提成归属）一个字没动
        ⇒ 造出「列表显示乙、提成归甲」。收成一条路之后，换人**只有**
        `POST /orders/<单号>/sales-agent/`（同一事务里改归属 + 搬绩效行 + 同步名字 + 重算）。

        ## 判据

        ① 业务码 1000（**不是** 403：授权是够的，错的是**路径**）+ 一句指向专用端点的提示；
        ② 库里 `sales_agent` / `sales_user` / 绩效行**全都没变**；
        ③ **整笔请求被拒**（同一次带的 `tag` 也没写）——⛔ 不是「只丢这一个字段」的静默行为。
        """
        order, customer = self._make_order()
        response = self.client.patch(
            f"/api/business/orders/{order.order_no}/",
            {
                "sales_agent": "被单独改掉的测试昵称",
                "tag": "这一次不该落库",
                "project_text": "满星",      # 必填判据要它（见上一条用例的注释）
            },
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)              # 业务码走 200 信封
        self.assertEqual(body["code"], 1000)
        self.assertIn("换销售客服", body["msg"])                  # 提示改走专用端点

        order.refresh_from_db()
        self.assertEqual(order.sales_agent, KEFU_NICKNAME)        # 那一列**没变**
        self.assertEqual(order.sales_user_id, self.owner.pk)      # 归属没变
        self.assertEqual(order.tag, "")                           # 整笔被拒（别的字段也没写）
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.owner.pk)

    def test_patch_with_a_clearing_sales_agent_is_rejected_too(self):
        """同一条闸的另一档：`sales_agent: ""`（**清空**）也是一次「值变了」⇒ 同样拒绝。

        判别力：把判据写成真值判断（`if raw_sales_agent:`）会让空串被当成"没带"、
        静默通过并把这一列清掉（E-0021 的 JS/Python 两侧同型），本条立刻红。
        ⚠️ 它同时钉住上面那道闸用的是 `is not _ABSENT` 而不是真值。
        """
        order, _customer = self._make_order()
        body = self.client.patch(
            f"/api/business/orders/{order.order_no}/",
            {"sales_agent": "", "project_text": "满星"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(Order.objects.get(pk=order.pk).sales_agent, KEFU_NICKNAME)

    def test_patch_without_the_node_and_a_different_sales_agent_is_still_403(self):
        """无节点 + 值变了 ⇒ **仍然是 HTTP 403**（那一档的口径一个字没改）。

        判别力：F4.1 那道新闸若写成「先判业务码」，无节点的人会从 403 变成 1000
        ⇒ 越权与「路径错」在监控上再也分不开。
        """
        order, _customer = self._make_order()
        other = _write_client("cs-patch-no-node", nodes=("order.add_edit",), group="丙组")
        response = other.patch(
            f"/api/business/orders/{order.order_no}/",
            {"sales_agent": "被单独改掉的测试昵称", "project_text": "满星"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.get(pk=order.pk).sales_agent, KEFU_NICKNAME)


class SalesAgentCommissionFailureTests(_SalesAgentBase):
    """⭐ 修复轮 S1：提成率算不出（可预期失败）⇒ **业务码**，不是 HTTP 500，且库里不留半成品。

    ⚠️ **桩打在「被查找的那个模块」上**（E-0029）：`order_writes` 里 `from ... import
    recompute_customer_commission` ⇒ 打 `apps.business.order_writes.recompute_customer_commission`。
    为了证明「桩真的接上了」，本类**另有一条不接桩的对照**：同一个端点在不接桩时业务码 0
    （见 `ChangeSalesAgentTests` 的正路用例）。
    """

    def _patch_hook(self):
        error = CommissionError("测试：提成费率配置缺失", ErrorCode.VALIDATION)
        return mock.patch(
            "apps.business.order_writes.recompute_customer_commission", side_effect=error
        )

    def test_a_commission_error_is_a_business_code_and_leaves_no_half_written_row(self):
        order, customer = self._make_order()
        with self._patch_hook() as hook:
            body = self._post(order, user_id=self.newcomer.pk).json()
        self.assertTrue(hook.called)                    # 桩真的接上了（E-0029）

        self.assertEqual(body["code"], 1000)
        self.assertIn("提成费率配置缺失", body["msg"])
        # 三个终态**一个都没动**：`set_rollback(True)` 把块内的写入全部回滚（E-0022）。
        order.refresh_from_db()
        self.assertEqual(order.sales_user_id, self.owner.pk)
        self.assertEqual(order.sales_agent, KEFU_NICKNAME)
        customer.refresh_from_db()
        self.assertEqual(customer.user_id, self.owner.pk)
        self.assertEqual(
            OperationLog.objects.filter(order_no=order.order_no).count(), 0
        )


class IntakeSalesUserTests(_SalesAgentBase):
    """录单那一路：**缺键 = 维持现状**；持节点显式指定 = 用它；不持节点带键 = 拒绝。

    背景（为什么录单也要能指定）：录单页那一格现在**真的能选别人**了
    ⇒ 只按认证态写归属就会造出「名字是甲、提成归录单人」的**分叉**，
    而那正是业务方要消掉的东西。
    """

    def _intake(self, *, client=None, **extra):
        payload = {"game": self.game.pk, "project_text": "满星", "hall_price": "88.00"}
        payload.update(extra)
        return (client or self.client).post(
            INTAKE_URL, payload, content_type="application/json"
        )

    def _assert_intake_owner(self, body, user, *, nickname):
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.sales_user_id, user.pk)
        self.assertEqual(order.sales_agent, nickname)
        customer = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(customer.user_id, user.pk)
        return order

    def test_without_the_key_the_owner_is_the_authenticated_user(self):
        """**缺键 ⇒ 逐字维持既有行为**（归属 = 认证态、名字 = 认证态的显示名）。

        ⚠️ 这条是「一个字都不改」那一半的判据：本线加了显式指定那一支，
        但缺键的请求必须逐字回到旧口径。
        """
        self.operator.first_name = "测试昵称操作人"
        self.operator.save(update_fields=["first_name"])

        body = self._intake().json()

        self.assertEqual(body["code"], 0)
        self._assert_intake_owner(
            body, self.operator, nickname="测试昵称操作人"
        )

    def test_with_the_key_and_the_node_the_target_owns_the_order(self):
        """持节点显式指定 ⇒ 归属 = 目标用户、SALES 行 = 目标用户、名字 = **服务端的**显示名。

        ⚠️ **2026-10-08 修复轮（F4.2）改准**：本条原先**还**往同一个请求里塞一个
        `sales_agent="来路不明的测试昵称"`，并断言它「不作数」（code 0）。新口径下那种请求
        **被明确拒绝**（业务码 1000）——名字必须是服务端按归属人算出来的那一个，
        「带一个别的名字然后指望它被忽略」正是静默丢字段那一族。⇒ 拆成两条：
        本条只送 `sales_user`（正路），外带名字那一档见
        `test_with_a_foreign_sales_agent_name_nothing_is_created`。
        """
        body = self._intake(sales_user=self.newcomer.pk).json()

        self.assertEqual(body["code"], 0)
        self._assert_intake_owner(body, self.newcomer, nickname=NEW_KEFU_NICKNAME)

    def test_with_the_key_and_the_matching_name_the_target_owns_the_order(self):
        """阳性对照：`sales_user` + **与目标显示名逐字相同**的 `sales_agent` ⇒ 成功。

        判别力：把 F4.2 那道闸写成「带了 `sales_agent` 就拒」，本条立刻红。
        """
        body = self._intake(
            sales_user=self.newcomer.pk, sales_agent=NEW_KEFU_NICKNAME
        ).json()

        self.assertEqual(body["code"], 0)
        self._assert_intake_owner(body, self.newcomer, nickname=NEW_KEFU_NICKNAME)

    def test_with_a_foreign_sales_agent_name_nothing_is_created(self):
        """`sales_user` + 一个**别的**名字 ⇒ 业务码 1000、**一行都不写**（修复轮 F4.2）。

        为什么必须是「拒」而不是「忽略」：`Order.sales_agent` 是**展示列**（订单列表那一格），
        而提成归 `Order.sales_user`。忽略它 = 调用方以为「名字是甲」而钱归乙，
        且**页面上没有任何信号**（本线要消掉的分叉）。
        """
        before = Order.objects.count()
        body = self._intake(
            sales_user=self.newcomer.pk, sales_agent="来路不明的测试昵称"
        ).json()

        self.assertEqual(body["code"], 1000)
        self.assertIn("归属人", body["msg"])
        self.assertEqual(Order.objects.count(), before)

    def test_without_the_node_the_key_is_rejected_and_nothing_is_created(self):
        """不持节点却带 `sales_user` ⇒ **HTTP 403**、**一行都不写**（⛔ 不是静默忽略）。

        ⚠️ 判据两半：状态码 + **订单表一行没多**。只断状态码会让「先建了单再拒」
        （半成品）逃过去。
        """
        before = Order.objects.count()
        other = _write_client(
            "cs-intake-no-node", nodes=("order.add_edit",), group="甲组"
        )

        response = self._intake(client=other, sales_user=self.newcomer.pk)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), before)

    def test_with_the_node_but_a_user_outside_the_pool_nothing_is_created(self):
        """持节点但目标**不在候选**里 ⇒ `1000`，同样一行都不写（同一份判据、两处入口）。"""
        before = Order.objects.count()
        body = self._intake(sales_user=self.outsider.pk).json()
        self.assertEqual(body["code"], 1000)
        self.assertEqual(Order.objects.count(), before)
