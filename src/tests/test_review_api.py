"""B1-6：`/review` 审核绩效（spec §5 / §6.2 / §8.1 / §8.3；P4 的 B4、B4b）。

规则（每条都有会红的测试）：
- **谁**：持 `/review` 的节点 `order.change_status_completed`（spec §4.3 / §13 Q1）。
  ⚠️ Task 4（单轴化收口）删掉了 `{REVIEWER}` 职位门 ⇒「谁能审核」**只剩节点**：
  持 17 的客服现在也能过审（业务方裁定的放宽，D-0071 的「对标没有审核」同向）；
  两个方向都要测（有节点过 / 缺节点被拒），403 的 msg 要**可辨来源**；
- **来源态**只收「订单在 `PENDING_REVIEW`**且**绩效在 `待审核`」——只看订单状态不够：
  P2 遗留的两条边（`ISSUE`/`PAUSED → PENDING_REVIEW`，节点集都含 `order.change_status`）能把**没交过单**的单送进待验收；
  ⭐ **修复轮 1 的唯一例外**：订单在 `PENDING_REVIEW` 而该列**已是 `已通过`**、本次请求又是**通过**
  ⇒ 幂等回执成功（不报错、不动钱、不留痕）；`已驳回` / 空值 / 「已是 `已通过` 但本次驳回」**照旧拒绝**；
- **通过** → `已通过` + 订单 → `COMPLETED`；**驳回** → `已驳回` + 订单 → `IN_PROGRESS`（可再提交再通过）；
- **同事务**：状态、绩效、留痕三样一起；中途失败两处都不能留痕；
- 留痕要有**审核动作**行（含前后值）与**订单状态变更**行，并记 `reviewed_by` / `reviewed_at`；
- ⛔ **§4.1.1 的排他性已作废**（D-0071，业务方）：原口径是「订单完成**只能**由本端点
  触发 —— 通用 `/transition/` 打 `target=COMPLETED` 必须被拒」（P3a 只收紧了边的**角色**，
  没堵住**路径**）。业务方裁定「对标没有审核、订单状态变成完成后绩效即生效，有权限的职位都可以用」
  ⇒ 那道**路径守卫已删除**，通用端点按**节点 17**授权即可完成订单（失败形态是 **403**）。
  本文件把这条改成**正向**判据：通用端点能完成，但**不会顺手替你做审核**。

⚠️ 金额/状态断言一律钉**字面值**（E-0024 的教训：期望值不得引用被测模块导出的常量——
`assertEqual(f(x), SOME_CONST)` 在 `SOME_CONST` 被改坏时不会红，是**自指断言**）。
⚠️ 本行原引 `E-0050`：那个号是「跨租户隔离测试的两个反向陷阱」（`docs/ERRORS.md`），
**与本条教训不是一回事**——同一个号两个含义，由 P4-12 修复轮 F-4 更正。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.core.error_codes import ErrorCode as EC
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本文件走到的节点门（三条）。`/performances/{id}/review/` 的类级门在
#: `PerformanceViewSet.PERFORMANCE_PERMISSIONS`（= `order.change_status_completed`，
#: 取代 legacy 的 `business.review_performance`）；三条订单门在 `OrderViewSet.ORDER_PERMISSIONS`。
REVIEW_ORDER_NODES = (
    # （RBAC 目录裁剪线）：对标 105 的 code 由 `order.confirm_link_group`
    # 改造为 `order.submit_work`（提交完单）⇒ 夹具同步改名（判据的字面量同批改于
    # `order_views.py::ORDER_PERMISSIONS["submit"]`）。本文件不在 T7 的「允许红」清单里
    # （那份清单只列了 4 个文件，见执行记录 §6 第 3 条）⇒ 按同一口径改名收口。
    "order.submit_work",              # `/submit/`
    "order.change_status",            # `/transition/`（D-0071 后它只承担「非已完成」的目标）
    "order.view_detail",
    "order.change_status_completed",  # `/review/` + `/transition/ {target: COMPLETED}`（D-0071 起两者共用）
)

#: `/review` 那一条单独的 code（拒绝侧要从 `REVIEW_ORDER_NODES` 里摘掉它）。
REVIEW_NODE = "order.change_status_completed"

#: 节点门（类级 `HasNode`）没有自定义 `message` ⇒ DRF 默认文案经统一信封后的固定串。
#: 用它做**源判别**：403 也可能是 CSRF / 别的门的产物，只断状态码分不出来（E-0063 的同族）。
NODE_DENIED_MSG = "您没有执行该操作的权限"


class ReviewApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            status=OrderStatus.PENDING_REVIEW, hall_price=Decimal("88.00"),
        )
        # ⚠️ 接单人用真实登录夹具建（`create_user` 建的号没有可用口令，登不进去），
        #    下面「驳回后可再提交」的回路要用这个 client。
        self.taker_client = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=REVIEW_ORDER_NODES
        )
        self.taker = User.objects.get(username="taker1")
        # ⚠️ 接单人来源**保持** `make_logged_in_client` 建出来的 `console`（网页端/后台新增）：
                #    本文件测的就是「客服在**网页端**审核」这条路，而 D-0279（业务方）规定
        #    自动结账**只对 App 注册打手生效** ⇒ console 打手在这里**不该**产生
        #    `performance.settle` 留痕。⛔ 不要为了保住旧计数把来源改成 `app`：那会造出
        #    「App 账号带网页会话」这个生产不可达的组合（D-0277 禁止 App 账号登网页），
        #    还把 D-0279 的核心场景从测试里删掉。
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。该用例的期望值一个字没改。
        # 代练员要 POST `/submit/`（该路径取订单过 `game_scope`）⇒ 必须显式持有本用例那款游戏。
        grant_games(self.taker, self.game)
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("88.00"), bonus_amount=Decimal("17.60"),
            review_status=ReviewStatus.PENDING,
        )
        self.client, self.reviewer = self._login_reviewer("reviewer1")
        # 客服主管要 POST 通用 `/transition/`（同样取订单过 `game_scope`）⇒ 同样显式授权。
        grant_games(self.reviewer, self.game)
        self.url = f"/api/business/performances/{self.perf.id}/review/"

    def _login_reviewer(self, username, *, with_node=True, groups=("客服主管",)):
        """`with_node` 控制是否挂 `/review` 的节点 `order.change_status_completed`。

        ⚠️ 不再是 `with_perm`：③线 Task 3（D1 / Ruling 12）起 `/review` 判的是**节点 code**，
        legacy 的 `business.review_performance` 已冻结（D-0028，实现保留但不再消费）。
        ⚠️ **拒绝侧不能留在这个职位组里**：节点挂在 `Role` 上而 `Role.name` 唯一，
        `setUp` 的 `reviewer1` 已经把 `REVIEW_ORDER_NODES` 挂到「客服主管」的 `Role` 上 ⇒
        同组的下一个 client 会**继承**那些节点（`with_node=False` 也照样有）。
        Task 4 起节点门是本端点**唯一**的判据，拒绝侧换个组名即可（职位认不认不影响这条判据）。
        """
        nodes = (
            REVIEW_ORDER_NODES
            if with_node
            else tuple(c for c in REVIEW_ORDER_NODES if c != REVIEW_NODE)
        )
        client = make_logged_in_client(username=username, groups=groups, nodes=nodes)
        return client, User.objects.get(username=username)

    def _post(self, payload):
        return self.client.post(self.url, payload, content_type="application/json")

    #: ⭐ **D4b**（R4 逐字，spec §4.4.3）：⛔ 不许改写、⛔ 不许补
    #: `非法状态迁移：<当前> → <目标>` 前缀。
    D4B_MSG = "该订单的绩效已结账，不能改状态；请先撤销结账后再操作"

    def _mark_settled(self):
        """把 `setUp` 那条 MAIN 绩效行置成**已结账**（只落 `settle_status` 一列）。

        ⚠️ 与 `test_dispatch_api.PoolEntrySettledGuardTests.SETTLED_SHAPES` 同形：
        `Performance.is_settled` 是**三字段并集** ⇒ 只落一列就足以命中，
        也正好检验判据没有偷看别的列。
        """
        self.perf.settle_status = SettleStatus.SETTLED
        self.perf.save(update_fields=["settle_status", "updated_at"])

    # ── 通过 ────────────────────────────────────────────────────
    def test_reviewer_approves_and_completes_the_order(self):
        body = self._post({"approved": True, "comment": "金额无误"}).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.perf.reviewed_by, "reviewer1")
        self.assertIsNotNone(self.perf.reviewed_at)
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        row = OperationLog.objects.get(
            order_no=self.order.order_no, action="performance.review"
        )
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.actor, "reviewer1")
        self.assertEqual(row.field_name, "review_status")
        self.assertEqual(row.from_value, "待审核")
        self.assertEqual(row.to_value, "已通过")
        self.assertEqual(row.reason, "金额无误")
        # `/review` 是**一个**业务动作 ⇒ 审核留痕**恰好一行**（旧的 `REVIEW` + `STATUS_CHANGE`
        # 两行由 `performance.review` 取代；订单真的变成 `COMPLETED` 已在上面钉住）。
        # ⭐ D-0279：接单人是 `console`（网页端/后台建）⇒ **不再自动结账**，
        #    这一跳**不会**写 `performance.settle` ⇒ 本单日志总数是 **1**（只有审核那一行）。
        #    ⚠️ 这正是 D-0279 要钉的场景：客服在网页端点审核，console 打手不会被自动发钱。
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="performance.review"
            ).count(),
            1,
        )
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="performance.settle"
            ).count(),
            0,
        )
        self.assertEqual(
            OperationLog.objects.filter(order_no=self.order.order_no).count(), 1
        )
        # 钱与结账列一个字节都不动（D-0279 的承重判据）。
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.settle_status, SettleStatus.UNSETTLED)
        self.assertIsNone(self.perf.settled_at)

    # ── 驳回（含「可再提交再通过」的完整回路）────────────────────
    def test_reject_returns_the_order_to_in_progress_and_the_taker_can_resubmit(self):
        body = self._post({"approved": False, "comment": "金额不对"}).json()
        self.assertEqual(body["code"], 0)
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.REJECTED)
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

        # 完整回路：代练员再交一次单 → 又回到待审核 → 审核通过 → 已完成
        resubmit = self.taker_client.post(
            f"/api/business/orders/{self.order.order_no}/submit/", {},
            content_type="application/json",
        )
        self.assertEqual(resubmit.json()["code"], 0)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        # ⭐ R2：`/submit/` **不写任何金额** ⇒ 重交后 `bonus_amount` 停在交单前的
        #    值（夹具 `setUp` 是 `17.60`），⛔ 不再是旧的「没配提成 ⇒ 回落全额 88.00」。
        self.assertEqual(self.perf.bonus_amount, Decimal("17.60"))
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))

        self.assertEqual(self._post({"approved": True}).json()["code"], 0)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        # ⭐ R5：完成时按 `temp_amount` 生成生效绩效（`88.00`，⛔ 不是 `17.60`）
        self.assertEqual(self.perf.bonus_amount, Decimal("88.00"))

    # ── D4a：驳回方向（目标 `IN_PROGRESS`）也重算 `temp_amount` ─────
    def test_reject_recomputes_the_temp_amount_on_the_way_back(self):
        """⭐ spec §7.2 #21：`/review/` **驳回**方向与 `/transition/` 共用同一份钩子
        ⇒ 驳回后 `temp_amount == 有效基价 × 当前接单人率` 的**字面值**。

        夹具的接单人是 `make_logged_in_client` 建的档案（`ORDER_PERCENT` / `0.00`
        ⇒ 按 `commission.py` 的口径回落 100%）⇒ `88.00 × 100% == 88.00`。
        ⚠️ 先把 `temp` 预置成**一个明显不同的陈旧值**（`1.00`）——否则「重算过」与
        「原样没动」在值上分不开，这条判据会退化成恒真。

        **杀法**：删掉 `IN_PROGRESS` 那条注册（或把钩子只挂在 `/transition/` 的视图里）
        ⇒ `temp` 停在 `1.00` ⇒ 本用例红。
        """
        self.perf.temp_amount = Decimal("1.00")
        self.perf.save(update_fields=["temp_amount"])
        body = self._post({"approved": False, "comment": "金额不对"}).json()
        self.assertEqual(body["code"], 0, body)
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertEqual(self.perf.bonus_amount, Decimal("17.60"))    # ⛔ 不碰

    def test_a_fixed_mode_rejection_is_rolled_back_whole(self):
        """⭐ R7（spec §7.2 #22 的 `/review/` 那一条）：驳回方向撞 `CommissionError`
        ⇒ 业务码 + `msg` **等于 D5 那句原文** + **整笔回滚**（订单状态与 `review_status`
        三列一个都不留）+ 无留痕。

        本端点是「先写 `review_status`、再问状态机」的形态 ⇒ 回滚只靠
        `performance_views.review` 的 `transaction.set_rollback(True)` 成立（E-0022）。

        **杀法（「抛错被吞」向）**：把钩子包成 `try/except CommissionError: pass` ⇒
        订单被改到 `IN_PROGRESS`、`code == 0` ⇒ 本用例红。
        """
        StaffProfile.objects.filter(user=self.taker).update(
            commission_mode=CommissionMode.FIXED, commission_value=Decimal("30.00"),
        )
        body = self._post({"approved": False, "comment": "驳回试试"}).json()
        self.assertEqual(body["code"], EC.VALIDATION, body)
        self.assertEqual(
            body["msg"],
            "该单的接手人（用户名 taker1）的提成模式是「固定金额」，"
            "本处需要「订单价百分比」；请到人员管理把 TA 的提成模式改成"
            "「订单价百分比」后再操作",
            body,
        )
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        self.assertEqual(self.perf.reviewed_by, "")
        self.assertIsNone(self.perf.reviewed_at)
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    def test_after_rejection_the_performance_can_be_reviewed_again(self):
        """驳回之后状态回到 `IN_PROGRESS`，此时**不**在待审核 ⇒ 再审核应被来源态判据拒绝。"""
        self.assertEqual(self._post({"approved": False}).json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        again = self._post({"approved": True}).json()
        self.assertEqual(again["code"], EC.BUSINESS)
        self.assertIn("待验收", again["msg"])
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.REJECTED)   # 没被改成已通过

    # ── 已通过 ⇒ 幂等（业务方裁定）────────────
    def test_approving_an_already_approved_row_is_idempotent(self):
        """⭐ **修复轮 1**：`已完成 → 待验收 → 再点「审核通过」` 必须**幂等不报错、钱不变、
        不留第二条痕**（修复前实测返回 `1002`「该绩效不在待审核状态（当前：已通过）」）。

        本用例走**真实路径**构造那一档（⛔ 不是直接把列改成 `已通过`）：
        `/review/` 过审（订单 → `COMPLETED`、`review_status` → `已通过`、一条留痕）
        ⇒ 通用 `/transition/` 重开回 `待验收`（R5 起那一列**不会**被复位）
        ⇒ 再点 `/review/` 通过。

        判别力（三条一起断，少一条就有假通过面）：
        ① **不报错**（`code == 0`，修复前这里是 `1002`）；
        ② **钱不变**——重开之后 `temp_amount` 被改成 `50.00`（夹具构造的漂移；真实入口
           也可以：`/adjust-amount/` 的 `amount_kind="temp"` 不受 R3 闸限制，只有 `"bonus"`
           那一档才判订单状态）⇒ 若本档改走 `apply_transition(COMPLETED)`，完成钩子会把
           `bonus_amount` 重写成 `50.00` ⇒ ② 红；
        ③ **不留第二条痕**——留痕数仍等于第一次过审留下的那一条 ⇒ 若本档照常 `record(...)`
           ⇒ ③ 红。
        ⚠️ 本档是**幂等**（不再做完成动作）⇒ 订单停在进入本档时的状态（`待验收`），
        要重新完成走 `/transition/`；这一条也在这里钉住，免得实现「顺手代做」。
        """
        first = self._post({"approved": True, "comment": "第一次"}).json()
        self.assertEqual(first["code"], 0, first)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.perf.bonus_amount, Decimal("88.00"))   # 完成钩子：bonus := temp
        self.assertEqual(self.perf.reviewed_by, "reviewer1")
        review_logs_before = OperationLog.objects.filter(
            order_no=self.order.order_no, action="performance.review"
        ).count()
        self.assertEqual(review_logs_before, 1)

        reopened = self.client.post(
            f"/api/business/orders/{self.order.order_no}/transition/",
            {"target": OrderStatus.PENDING_REVIEW}, content_type="application/json",
        ).json()
        self.assertEqual(reopened["code"], 0, reopened)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        # 前置哨兵：重开之后订单在待验收，而绩效那一列**仍是**「已通过」（= 被点名的那一档）
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        # ⚠️ 重开**自己**会留一条 `order.transition` 痕（不是本用例的被测对象）⇒ 第二次点审核
        #    前后的判据都以「重开之后」为基线。
        logs_after_reopen = OperationLog.objects.filter(order_no=self.order.order_no).count()
        # 基线 = ① 第一次审核通过 ② 这次重开（`order.transition`）。
        # （D-0279）之后**没有**「完成时自动入账」那一行（console 打手不自动结账）。
        self.assertEqual(logs_after_reopen, 2)
        # 夹具构造的漂移（真实入口见 docstring）：重开之后预计绩效被改
        self.perf.temp_amount = Decimal("50.00")
        self.perf.save(update_fields=["temp_amount"])

        body = self._post({"approved": True, "comment": "第二次"}).json()
        self.assertEqual(body["code"], 0, body)                       # ① 不报错
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("88.00"))    # ② 钱不变（⛔ 不是 50.00）
        self.assertEqual(self.perf.temp_amount, Decimal("50.00"))     # 漂移值原样留着
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.perf.reviewed_by, "reviewer1")          # 三列没被重写
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="performance.review"
            ).count(),
            review_logs_before,
        )                                                             # ③ 不留第二条审核痕
        self.assertEqual(
            OperationLog.objects.filter(order_no=self.order.order_no).count(),
            logs_after_reopen,
        )                                                             # ③ 总行数也没涨
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)   # 幂等：不代做完成

    def test_a_rejected_row_on_a_pending_review_order_is_still_refused(self):
        """「只放开已是已通过这一档」的边界：**已驳回** + 订单在待验收 ⇒ 照旧 `1002`。

        构造物是可达的（驳回 ⇒ 订单回 `IN_PROGRESS`、该列 `已驳回`；此后把订单改回
        `待验收` 时那一列**不会**被复位）。
        判别力：把 `review_status` 那道活闸门整条删掉（= 任何状态都当通过）⇒ 本用例红。
        """
        self.perf.review_status = ReviewStatus.REJECTED
        self.perf.save(update_fields=["review_status"])
        body = self._post({"approved": True}).json()
        self.assertEqual(body["code"], EC.BUSINESS, body)
        self.assertIn("待审核", body["msg"])
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)   # 订单没被推进
        self.assertEqual(self.perf.review_status, ReviewStatus.REJECTED)  # 也没被改成已通过
        self.assertEqual(self.perf.reviewed_by, "")
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    def test_rejecting_an_already_approved_row_is_still_refused(self):
        """⭐ 裁定只覆盖**再点「审核通过」**：「已是 `已通过` 但本次请求是**驳回**」照旧拒绝。

        判别力：把幂等档写成「只看 `review_status == APPROVED`」而**不看本次方向** ⇒ 本用例红。
        """
        self.perf.review_status = ReviewStatus.APPROVED
        self.perf.save(update_fields=["review_status"])
        body = self._post({"approved": False, "comment": "又驳回"}).json()
        self.assertEqual(body["code"], EC.BUSINESS, body)
        self.assertIn("待审核", body["msg"])
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    # ── D4b：已结账 ⇒ 拒绝一切状态变更（R9：驳回方向自动覆盖）──────
    def test_a_settled_row_cannot_be_rejected_and_is_rolled_back_whole(self):
        """⭐ **R9**（spec §4.4.5(A) / §6.3 #6）：闸加在 `apply_transition` ⇒
        `/review/` 的**驳回方向**（目标 `IN_PROGRESS`）**自动覆盖**。

        ⚠️ **本端点是「先写 `review_status` 三列、再问状态机」的形态**（`performance_views.py`
        的 `with transaction.atomic():` 里先 `perf.save(...)` 再 `apply_transition`）
        ⇒ 「被拒时一行不写」**只靠 `set_rollback(True)` 成立**，
        ⛔ 别照抄 `/transition/` 的「守卫在 `save()` 之前所以不用回滚」。

        **杀法**：把闸只加在「目标 == `COMPLETED`」那格 ⇒ 本用例红；
        去掉 `performance_views.review` 里的 `transaction.set_rollback(True)` ⇒ 三列会留下 ⇒ 红。
        """
        self._mark_settled()
        body = self._post({"approved": False, "comment": "驳回试试"}).json()
        self.assertEqual(body["code"], EC.BUSINESS, body)
        self.assertEqual(body["msg"], self.D4B_MSG, body)
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)   # 三列一个都不许留
        # ⚠️ `reviewed_by` 是 `CharField(max_length=150, blank=True)`（**非空**、库里存空串）
        # ⇒ 「没留痕」= 保持 `setUp` 时的初始空串。⛔ 不写 `assertIsNone`（那会恒假：
        # 计划片段那句话与模型定义不符，按实际列型改准；这条仍有判别力——
        # 回滚没生效时这里会是 `"reviewer1"`）。
        self.assertEqual(self.perf.reviewed_by, "")
        self.assertIsNone(self.perf.reviewed_at)
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    def test_after_the_revoke_the_same_rejection_succeeds(self):
        """⭐ **反向对照**：撤销结账后同一次驳回 ⇒ `code == 0`
        （杀「有结账痕迹就全拦」那种恒真实现）。

        撤销走三字段一起复位（与 `test_dispatch_api` 那条反向对照同形）；
        生产入口是 `POST /api/business/settlements/revoke`。
        """
        self._mark_settled()
        self.perf.settle_status = SettleStatus.UNSETTLED
        self.perf.settled_at = None
        self.perf.settled_amount = None
        self.perf.save(
            update_fields=["settle_status", "settled_at", "settled_amount", "updated_at"]
        )
        body = self._post({"approved": False}).json()
        self.assertEqual(body["code"], 0, body)
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.REJECTED)
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    # ── 权限（两个方向）────────────────────────────────────────
    def test_cs_with_the_node_can_now_review(self):
        """Task 4：`/review` 只剩**节点**判据 ⇒ 持 17 的客服**可以**过审。

        ⚠️ **本条替换了旧的 `test_cs_with_the_node_still_cannot_review`**：旧用例断言
        「职位不对 ⇒ 403」（`{REVIEWER}` 职位门 + 节点门是 `and`）。单轴化（spec §6 第 13 行）
        删掉了那道职位门，业务方裁定（§13 Q1）**接受**「客服也能走 `/review` 过审」
        ——理由是对标没有审核点（D-0071）、钱挂在 `COMPLETED` 状态上而不是审核状态上，
        放宽的只是「谁写那条审核留痕」。

        ⚠️ 本用例用**客服 + 节点 17**：这正是旧用例的同一套夹具，只是期望值从 403 变成功
        （判据换了轴，不是断言被放松）。
        """
        client = make_logged_in_client(
            username="cs_reviewer", groups=("客服",), nodes=REVIEW_ORDER_NODES
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。`/review` 经
        # `get_object()`（被 `game_scope` 收窄的 queryset）取这张绩效行 ⇒ 不补本夹具的
        # 游戏就会先撞上读侧的 **1002**，而本用例要断的是「持 17 ⇒ 能过审」。期望值一个字没改。
        grant_games(User.objects.get(username="cs_reviewer"), self.game)
        response = client.post(self.url, {"approved": True}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0)
        self.perf.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)

    def test_reviewer_without_the_node_cannot_review(self):
        """持有内置职位（客服主管）但**缺** `/review` 的节点 ⇒ 403。

        ⚠️ 拒绝侧换组名（`客服管无`）：节点挂在 `Role` 上，同组会继承 `setUp` 的授予
        （见 `_login_reviewer` 的说明）。Task 4 起只剩节点门，所以这里的 403 **就是**节点门。
        """
        client, _ = self._login_reviewer(
            "reviewer_no_node", with_node=False, groups=("客服管无",)
        )
        response = client.post(self.url, {"approved": True}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        # 可辨来源：节点门（类级 `HasNode` 的 DRF 默认文案），不是 CSRF / 别的 403 来源
        msg = response.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("CSRF", msg)
        self.assertTrue(msg)
        self._assert_nothing_changed()

    def test_the_adjust_node_is_not_enough_for_review(self):
        """两个节点不能互相顶替（`/adjust` 的 `order.submit_payout_price` 对 `/review` 无效）。

        ⚠️ 拒绝侧必须是**过得了类级节点门、但因缺本节点而被拒**的构造物，
        否则 403 会来自别的来源，测不到本用例想测的东西。

        ⚠️ **2026-09-23 判据统一后不再能用「系统管理员」当这个构造物**（原文用的就是它）：
        那个职位**现在就是管理员判据本身**，`has_node_now` 对它无条件短路 ⇒ "缺节点"这个
        前提根本构不出来（旧写法实测返回 200 ⇒ 本用例红）。改用「客服主管」，并在函数体里
        **显式摘掉** `setUp` 挂上去的那条 `/review` 节点，只留错的节点
        （`order.submit_payout_price`）。
        ⛔ `main` 原文在这里写「(`roles_of` 含 `REVIEWER`，过角色闸门)」——`roles_of` 已被
        本线删除，那句随单轴化作废，不取。
        """
        Role.objects.get(name="客服主管").permissions.remove(
            PermissionNode.objects.get(code=REVIEW_NODE)
        )
        client = make_logged_in_client(
            username="reviewer_wrong_node", groups=("客服主管",),
            nodes=("order.submit_payout_price",),
        )
        response = client.post(self.url, {"approved": True}, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        # 可辨来源：节点门（类级 `HasNode` 的 DRF 默认文案），不是 CSRF / 别的 403 来源
        msg = response.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("CSRF", msg)
        self._assert_nothing_changed()

    def test_anonymous_is_rejected(self):
        response = Client(SERVER_NAME="tenant.test.com").post(
            self.url, {"approved": True}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)

    # ── 来源态（两层）──────────────────────────────────────────
    def test_review_requires_the_order_to_be_pending_review(self):
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        body = self._post({"approved": True}).json()
        self.assertEqual(body["code"], EC.BUSINESS)
        self.assertIn("待验收", body["msg"])
        self._assert_nothing_changed(order_status=OrderStatus.IN_PROGRESS)

    def test_review_refuses_a_performance_that_was_never_submitted(self):
        """⚠️ 只看订单状态不够：`ISSUE → PENDING_REVIEW` 是**客服**的边，
        客服能把一张没交过单的单送进待验收（P2 遗留边）——那时绩效上没有 `待审核`。"""
        self.perf.review_status = None
        self.perf.save(update_fields=["review_status"])
        body = self._post({"approved": True}).json()
        self.assertEqual(body["code"], EC.BUSINESS)
        self.assertIn("待审核", body["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)   # 订单没被推进

    def test_malformed_payloads_are_business_codes_not_500(self):
        for payload in ({"approved": "maybe"}, {"comment": "x" * 300}, {"approved": None}):
            with self.subTest(payload=payload):
                body = self._post(payload).json()
                self.assertEqual(body["code"], EC.VALIDATION)

    # ── 事务原子性 ──────────────────────────────────────────────
    def test_rolls_back_both_sides_when_the_status_audit_fails(self):
        seen = {}

        def _boom(**kwargs):
            seen["status"] = Order.objects.get(pk=self.order.pk).status
            seen["review_status"] = Performance.objects.get(pk=self.perf.pk).review_status
            raise RuntimeError("boom")

        # ⚠️ **patch 要打在「被查找的地方」**：`/review` 在 `performance_views` 里调用
        #    `record`，所以打的是 `performance_views` 的那个名字。
        #    打在 `order_views` 上**不会生效**（那是另一个模块的全局名）——本用例第一版就栽在这里：
        #    没有异常抛出，`assertRaises` 直接失败，而不是"回滚没生效"。
        with mock.patch(
            "apps.business.api.performance_views.record", side_effect=_boom
        ):
            with self.assertRaises(RuntimeError):
                self._post({"approved": True})
        # 哨兵：抛错前订单与绩效**都已经落库**了（否则「回滚」对「没写」不加区分）
        self.assertEqual(seen["status"], OrderStatus.COMPLETED)
        self.assertEqual(seen["review_status"], ReviewStatus.APPROVED)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )

    # ── D-0071：通用迁移端点**也能**完成订单（并留下状态留痕）────────
    def test_generic_transition_can_complete_an_order_without_reviewing(self):
        """**D-0071（2026-09-22）**：订单完成**不再**专属于绩效审核——通用 `/transition/`
        在持有节点 `order.change_status_completed`（17）时**接受** `target=COMPLETED`。

        ⚠️ **本条替换了旧的 `test_generic_transition_cannot_complete_an_order`**：
        旧用例钉的是 spec §4.1.1 的**路径守卫**（`code=BUSINESS` + `msg` 含 "review"），
        即「订单完成只能由绩效审核触发」。业务方裁定取消这道闸门
        （对标没有审核、订单状态变成完成后绩效即生效，原话「**有权限的职位都可以用**」）——
        授权判据改成**节点 17**，失败形态是 **403**（不是 200 + 业务码）。
        本夹具的客服主管持 17（`REVIEW_ORDER_NODES`），所以这条路径现在**成功**。

        判别力（三件一起断，少一件就有假通过面）：
        ① 订单真的到 `COMPLETED`；② 留了一条 `order.transition` 痕（迁移必须留痕）；
        ③ **审核动作没有被顺手做掉**——没有 `performance.review` 留痕，`reviewed_by` /
        `reviewed_at` 仍是空。少了 ③，一个「把审核也捎带做了」的实现会照样绿，
        而那正是 D-0071 要把两件事分开的地方。
        ⭐ **R5 改准**：`review_status` **不再**是这条判据的承重项——
        订单进 `COMPLETED` 时完成钩子会把它写成「已通过」（这一列表达「**钱已确定生效**」，
        与「谁审的」无关）⇒ ③ 的承重项换成 `reviewed_by` / `reviewed_at` 两列 + 留痕。
        ⭐ Task L（合并 `main`）：②③ 的留痕断言改打在**新表 `OperationLog`** 上
        （本线的唯一写入出口 `record()`）；期望值写**中文标签字面值**（spec §5.1 / E-0024）。
        """
        response = self.client.post(
            f"/api/business/orders/{self.order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED}, content_type="application/json",
        )
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        # R5：完成钩子把这一列写成「已通过」（钱生效标记）；但**审核动作**没有发生：
        self.assertEqual(self.perf.review_status, ReviewStatus.APPROVED)
        self.assertEqual(self.perf.reviewed_by, "")
        self.assertIsNone(self.perf.reviewed_at)
        status_row = OperationLog.objects.get(
            order_no=self.order.order_no, action="order.transition"
        )
        self.assertEqual(status_row.from_value, "待验收")
        self.assertEqual(status_row.to_value, "已完成")
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=self.order.order_no, action="performance.review"
            ).exists()
        )

    # ── 辅助 ────────────────────────────────────────────────────
    def _assert_nothing_changed(self, order_status=OrderStatus.PENDING_REVIEW):
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, order_status)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        self.assertEqual(self.perf.reviewed_by, "")   # 列是 blank=True 的 CharField，空值是 "" 不是 None
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order.order_no).exists()
        )
