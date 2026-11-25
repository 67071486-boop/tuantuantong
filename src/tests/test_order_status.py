from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase  # noqa: F401  (既有的 SimpleTestCase 保持不动)
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_member
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.order_status import (
    SELF_TRANSITION_MESSAGE,
    TRANSITIONS,
    IllegalTransition,
    _TARGET_HOOKS,
    apply_transition,
    can_transition,
    clear_target_hooks,
    register_target_hook,
    transition_denial,
)


class OrderStatusTests(SimpleTestCase):
    """纯逻辑，不碰数据库——所以用 SimpleTestCase。"""

    def test_legal_transitions_from_unassigned(self):
        self.assertTrue(can_transition(OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB))
        self.assertTrue(can_transition(OrderStatus.UNASSIGNED, OrderStatus.IN_PROGRESS))

    def test_only_self_loops_are_absent_from_the_whitelist(self):
        """任意状态互转都有边；**只有自环**（`src == target`）不在白名单里。

        ⚠️ **本条在 2026-09-24 被反转**（D-0147 / spec §4.4 的 R8：状态机整体放开）。
        改之前它叫 `test_illegal_transition_is_rejected`，靶子是 `COMPLETED → IN_PROGRESS`
        （P3 期「已完成不可回退」的收紧）——那条边现在**合法**。R8 之后唯一「边不存在」
        的形状就是自环 ⇒ 靶子换成 `COMPLETED → COMPLETED`。
        全表比对由下面的 `AnyStatusPairTests` 承担；本条只留一个可读的定点判据。

        ⚠️ 开头那条**正向**断言是**哨兵**：纯否定断言在「白名单为空」时也会通过，
        于是它区分不了「白名单正确」与「白名单为空」。哨兵让本条对空表变红。
        ⚠️ 名称里的「只有自环」是本批（F3）**之前**的事实：入 `COMPLETED` 的六条边
        现在也不在表里（见 `AnyStatusPairTests`）。定点判据本身仍成立，名称保留以存查。
        """
        self.assertTrue(can_transition(OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB))
        self.assertFalse(can_transition(OrderStatus.COMPLETED, OrderStatus.COMPLETED))

    def test_completed_can_go_back_to_in_progress(self):
        """已完成**可以**回到代练中（D-0147 / R8 取消了终态；代价见 spec §4.4 的 D-A2）。

        ⚠️ **本条在 2026-09-24 被反转**：原名 `test_completed_cannot_go_back_to_in_progress`，
        钉的是 P3 期「已完成不可回退」的收紧——那条收紧随 R8 的整体放开作废。
        同样加哨兵（见上）：`IN_PROGRESS → PENDING_REVIEW` 是既有边。
        """
        self.assertTrue(can_transition(OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW))
        self.assertTrue(can_transition(OrderStatus.COMPLETED, OrderStatus.IN_PROGRESS))

    def test_any_status_can_enter_the_three_exception_states(self):
        for src in OrderStatus:
            if src == OrderStatus.REFUNDED:
                continue
            for target in (OrderStatus.ISSUE, OrderStatus.PAUSED, OrderStatus.REFUNDED):
                if src == target:
                    continue
                self.assertTrue(can_transition(src, target), f"{src} -> {target}")

    def test_refunded_can_reach_every_other_status(self):
        """已退单不再是任何意义上的终态：除自环外每个目标态都有出边（D-0147 / R8）。

        ⚠️ **本条在 2026-09-24 被再次反转**：它此前叫
        `test_refunded_recovers_only_to_the_pool_states`，钉的是 2026-09-21 裁定的
        「只能回池内两态」。R8 把状态机整体放开后，「只回池内」不再成立；那次裁定的
        依据（P2 spec D8：恢复去向由人指定）仍然有效，只是可选项不再受限。

        判别力：逐条枚举每个非自环目标（阳性侧），并单独断自环不在——只断前者的话，
        一个把自环也铺上的表会漏过。

        ⚠️ **本批 F3，spec §2.3）**：`COMPLETED` 是**显式枚举的例外**
        （入 `COMPLETED` 的边只剩「待验收 → 已完成」一条）⇒ 它必须在否定侧。
        """
        self.assertTrue(can_transition(OrderStatus.IN_PROGRESS, OrderStatus.REFUNDED))
        for target in OrderStatus:
            with self.subTest(target=target):
                if target in (OrderStatus.REFUNDED, OrderStatus.COMPLETED):
                    self.assertFalse(can_transition(OrderStatus.REFUNDED, target))
                    continue
                self.assertTrue(can_transition(OrderStatus.REFUNDED, target))
        # 阳性哨兵：那两条回池边仍在（它们不再是「仅有的」两条）
        self.assertTrue(can_transition(OrderStatus.REFUNDED, OrderStatus.UNASSIGNED))
        self.assertTrue(can_transition(OrderStatus.REFUNDED, OrderStatus.WAITING_GRAB))

    def test_recovery_edges_have_target_determined_node_sets(self):
        """退单出边的节点集**逐条按 target 定**（不是同一个集合套所有目标）。

        - `REFUNDED → UNASSIGNED` = `{order.change_status, order.clear_taker,
          order.withdraw_from_grab_hall}`（target 为 `UNASSIGNED` 的三扇门）；
        - `REFUNDED → WAITING_GRAB` = `{order.send_to_grab_hall}`。

        ⚠️ D-0147 / R8）：`UNASSIGNED` 那条的目标集在旧口径下是
        `{16, 19}` 两条，现在按 **target** 定、补上了 `order.withdraw_from_grab_hall`
        （`WAITING_GRAB → UNASSIGNED` 那一支「撤回抛单」也在同一个目标态上）。

        判别力：持**无关**节点（如抢单节点）仍被拒。
        """
        expected = {
            OrderStatus.UNASSIGNED: frozenset({
                "order.change_status",
                "order.clear_taker",
                "order.withdraw_from_grab_hall",
            }),
            OrderStatus.WAITING_GRAB: frozenset({"order.send_to_grab_hall"}),
        }
        for target, nodes in expected.items():
            self.assertEqual(TRANSITIONS[(OrderStatus.REFUNDED, target)], nodes)
            for node in nodes:
                self.assertTrue(can_transition(OrderStatus.REFUNDED, target, {node}))
            self.assertFalse(can_transition(OrderStatus.REFUNDED, target, {"grab_hall.take_order"}))
            self.assertFalse(
                can_transition(OrderStatus.REFUNDED, target, {"order.change_status_completed"})
            )

    def test_issue_or_paused_to_in_progress_needs_one_of_the_in_progress_gates(self):
        """`ISSUE`/`PAUSED → IN_PROGRESS` 的节点集 = target `IN_PROGRESS` 的那一组。

        ⚠️ **本条在 2026-09-24 被更新**（D-0147 / spec §4.4）：旧断言写「节点集 =
        `{order.change_status}`（D8 的形态）」——那是旧手写表的形状；R8 之后节点集按
        **target** 定，`IN_PROGRESS` 这一组 = `{16, 18, 1162, 17}`
        （通用流转 / `/assign/` / 改价 / 完成，见 spec §4.4 的表）。
        判别力侧的重点没变：**抢单节点（50）不在这一组里**（它只在
        `(WAITING_GRAB, IN_PROGRESS)` 那一条边上有），所以持 50 的人仍推不动这两条边。
        """
        expected = frozenset({
            "order.change_status",
            "order.assign_taker",
            "order.submit_payout_price",
            "order.change_status_completed",
        })
        self.assertEqual(TRANSITIONS[(OrderStatus.ISSUE, OrderStatus.IN_PROGRESS)], expected)
        self.assertEqual(TRANSITIONS[(OrderStatus.PAUSED, OrderStatus.IN_PROGRESS)], expected)
        for node in expected:
            self.assertTrue(can_transition(OrderStatus.ISSUE, OrderStatus.IN_PROGRESS, {node}))
        # 抢单节点只在「大厅 → 代练中」那一条边上（T2 的边界；matrix 由 T2 的用例全表比对）
        self.assertFalse(
            can_transition(OrderStatus.ISSUE, OrderStatus.IN_PROGRESS, {"grab_hall.take_order"})
        )
        self.assertFalse(can_transition(OrderStatus.ISSUE, OrderStatus.COMPLETED, {"order.change_status"}))
        self.assertFalse(
            can_transition(OrderStatus.PAUSED, OrderStatus.WAITING_GRAB, {"grab_hall.take_order"})
        )

    def test_change_status_moves_issue_to_other_recovery_targets(self):
        """D8：持节点 16 的调用方可把问题单/暂停单迁到白名单内其它状态。

        ⚠️ **计划 ④）改准**：`ISSUE/PAUSED → WAITING_GRAB` 已**不在**这一档
        ——该目标改判独占 36（业务方拍板 F），持 16 的人不再推得动它。本条据此把那条断言
        翻成否定面（正面的「持 36 可以」在 `TRANSITIONS` 的按 target 表里另断）。
        """
        self.assertFalse(
            can_transition(OrderStatus.ISSUE, OrderStatus.WAITING_GRAB, {"order.change_status"})
        )
        self.assertTrue(
            can_transition(OrderStatus.ISSUE, OrderStatus.WAITING_GRAB, {"order.send_to_grab_hall"})
        )
        self.assertTrue(can_transition(OrderStatus.PAUSED, OrderStatus.UNASSIGNED, {"order.change_status"}))
        self.assertFalse(
            can_transition(OrderStatus.ISSUE, OrderStatus.PENDING_REVIEW, {"grab_hall.take_order"})
        )

    def test_nodes_none_skips_the_node_check_this_is_the_known_p2_gap(self):
        """P2 无认证，执行者身份不可知——传 nodes=None 时只校验「从→到」。

        这不是设计意图，是**已知安全缺口**（spec D8）。P5 接上认证后由
        `apply_transition` 的必填 `nodes` 收口（显式 `None` 也被翻成空集 ⇒ 拒绝）。
        ⚠️ 2026-09-23 换轴：本条只把 `roles=None` 的语义原样搬到 `nodes=None`
        （P2 的图论用例靠它回答「这条边存在吗」）。
        ⚠️ **本批 F3）**：入 `COMPLETED` 的边只剩「待验收 → 已完成」一条
        ⇒ 靶子从 `ISSUE → COMPLETED` 换成仍在表里的一条（`ISSUE → WAITING_GRAB`），
        并单独断「被删的入边在宽松模式下也回 False」。
        """
        self.assertTrue(can_transition(OrderStatus.ISSUE, OrderStatus.WAITING_GRAB, None))
        self.assertTrue(can_transition(OrderStatus.PAUSED, OrderStatus.REFUNDED, None))
        self.assertFalse(can_transition(OrderStatus.ISSUE, OrderStatus.COMPLETED, None))

    def test_string_values_work_too(self):
        """状态可能来自请求体，是字符串。

        ⚠️ 靶子在 2026-09-24 换过一次（D-0147 / R8）：`COMPLETED → IN_PROGRESS` 现在
        也是**合法**边，「字符串也走白名单」的**否定**侧改用自环（唯一仍非法的形状）。
        ⚠️ **本批 F3）**：否定侧换成 `"UNASSIGNED" → "COMPLETED"`
        （被删的六条入边之一，spec §2.3），自环仍单独断。
        """
        self.assertTrue(can_transition("UNASSIGNED", "WAITING_GRAB"))
        self.assertFalse(can_transition("UNASSIGNED", "COMPLETED"))
        self.assertFalse(can_transition("COMPLETED", "COMPLETED"))


class SelfTransitionMessageTests(SimpleTestCase):
    """⭐ 本线 T4（2026-10-05，`fix/batch-feedback-affordance`）：自环的原因句是一句人话。

    业务方现场：一批单**已经全是「已完成」**，再点一次「批量验收」⇒ 预检从前回的是
    `非法状态迁移：COMPLETED → COMPLETED`（用户当成「操作失败」，其实是「第一次已经成功了」）。
    判据 h = **逐字**钉住那句人话；**本文件是唯一写死字面量的地方**，端到端那一条
    （`tests/test_order_batch_actions.py`）引本模块的常量、⛔ 不抄第二份字面量。
    判据 i 的三档（边不存在 / 节点不够 / `COMPLETE_REQUIRES_REVIEW_MESSAGE`）分别钉在
    `test_order_status_axis.py` 与 `test_complete_requires_review.py`。
    """

    def test_self_transition_returns_the_verbatim_human_copy(self):
        self.assertEqual(
            transition_denial(
                OrderStatus.COMPLETED, OrderStatus.COMPLETED, {"order.change_status_completed"}
            ),
            "该订单已经是「已完成」，无需修改",
        )

    def test_the_status_label_comes_from_order_status_choices(self):
        """状态中文名**复用既有出处** `OrderStatus.choices`（⛔ 不新抄一张中文表）。"""
        for status in OrderStatus:
            with self.subTest(status=status):
                self.assertEqual(
                    transition_denial(status, status, {"order.change_status"}),
                    SELF_TRANSITION_MESSAGE.format(status_label=status.label),
                )

    def test_an_unknown_code_is_not_the_self_transition_branch(self):
        """存量 / 未知码不进自环那一档 ⇒ 仍回通用那句（「边不存在」因此仍有可达样本）。"""
        self.assertEqual(
            transition_denial("ARCHIVED", "ARCHIVED", {"order.change_status"}),
            "非法状态迁移：ARCHIVED → ARCHIVED",
        )


class ApplyTransitionTests(SimpleTestCase):
    """`apply_transition` 的行为。

    上面那些测试没碰这个函数。这里用不碰数据库的替身把它钉住。
    """

    class _StubOrder:
        """替身：只记录 `save()` 收到什么，不碰数据库。"""

        def __init__(self, status, pk=1):
            self.status = status
            self.pk = pk
            self.peer_direct_pending = None
            self.updated_by = ""
            # ⭐：`apply_transition` 现在会读这两列的**旧值**判
            #    「是不是第一次」（`is None` 才写）⇒ 替身必须像真模型一样把它们初始化成
            #    `None`。少了它们，`AttributeError` 会被替身吞成「属性存在但不等于 None」
            #    （裸替身对象上任何属性都不是 `None`）⇒ 这条用例会以假象绿/红。
            self.published_at = None
            self.submitted_at = None
            # `apply_transition` 会调 `queue_status_change_push`，
            #    它读 `order.game_id`（规则按游戏过滤），失败分支还读 `order.order_no`。
            #    替身原先是裸对象 ⇒ 主仓新增的推送钩子在这条用例上 `AttributeError`。
            #    这里补两个真模型上一定存在的属性（`game_id=None` ⇒ 没有规则匹配，行为等价于
            #    「这个租户没配推送」）。
            self.game_id = None
            self.order_no = "stub-order"
            self.saved = []

        def save(self, **kwargs):
            self.saved.append(kwargs)

    def test_illegal_transition_raises_and_does_not_mutate(self):
        """非法迁移必须**在改动之前**失败——否则会把订单改成错的状态再报错。

        ⚠️ **靶子在 2026-09-24 换过**（D-0147 / R8）：原用 `COMPLETED → IN_PROGRESS`，
        而 R8 之后那条边**合法**。R8 之下唯一仍非法的形状是**自环**，所以换用它。

        ⚠️ **2026-09-23 换轴**：`roles=ALL_ROLES` 改成 `nodes=None`——本用例问的是
        「**这条边存在吗**」，而 `nodes=None` 正是那个语义（`can_transition` 的宽松分支）。
        ⚠️ **修复轮 1（复审 Important 1）**：只断「抛了」**没有判别力**——`apply_transition`
        把显式 `None` 翻成空集，于是**已存在**的边也会抛（只是带「缺少所需权限节点」后缀）。
        所以必须额外断**消息里没有那个后缀**：后缀出现 ⇔ 边存在但节点不够；
        边不存在的那一支**只有状态对**。这才把「边表被误加一条」变成可判红的回归。
        """
        order = self._StubOrder(OrderStatus.COMPLETED)
        with self.assertRaises(ValueError) as ctx:
            apply_transition(order, OrderStatus.COMPLETED, nodes=None)
        self.assertNotIn(
            "缺少所需权限节点",
            str(ctx.exception),
            "拒绝原因带了「缺少所需权限节点」⇒ 边其实存在（(COMPLETED, COMPLETED) 被误加）",
        )
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.saved, [])

    def test_node_mismatch_raises_when_nodes_are_supplied(self):
        """传了 nodes 时节点不匹配必须拒绝——P5 会把这条变成常态。"""
        order = self._StubOrder(OrderStatus.ISSUE)
        with self.assertRaises(ValueError):
            apply_transition(order, OrderStatus.COMPLETED, nodes={"grab_hall.take_order"})
        self.assertEqual(order.status, OrderStatus.ISSUE)
        self.assertEqual(order.saved, [])

    def test_explicit_nodes_none_is_rejected(self):
        """显式传 `nodes=None` = **没有身份** ⇒ 拒绝，不当成"不校验"（Ruling 21）。

        判别力：`UNASSIGNED → WAITING_GRAB` 是表里**存在**的边，而
        `can_transition(..., nodes=None)` 的宽松语义对它返回 True——所以
        没有这层翻译时，本用例会**真的把订单改成 `WAITING_GRAB`** 再通过断言。
        """
        order = self._StubOrder(OrderStatus.UNASSIGNED)
        with self.assertRaises(IllegalTransition):
            apply_transition(order, OrderStatus.WAITING_GRAB, nodes=None)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.saved, [])

    def test_legal_transition_mutates_and_saves_only_changed_fields(self):
        """合法迁移改状态 + 记操作人，且 `update_fields` 只含那四列。

        目标态挂 `PAUSED`：池内态和待验收都有生产钩子，会在这只替身上真的查库。
        每个目标态还有 `member.status_out`，函数体里把那条钩子打桩掉。
        本用例问的是怎么写库。节点只传 `order.change_status`，传全集会放过「谁都能改」。
        """
        order = self._StubOrder(OrderStatus.IN_PROGRESS)
        # 不能传全集：会放过「谁都能改」。
        # 替身不落库。下面每个桩去掉都会在 SimpleTestCase 里真的查库而红；
        # 那道闸自己的判别力在对应的真库用例里，不在这里。
        # Order 复读、绩效入队、PeerOrderLink 盖时刻、已结账闸都要读库。
        # Performance.first() 必须是 None：MagicMock 默认是真值，已结账闸会误命中。
        # 桩打在被查找的那个模块上。
        with mock.patch("apps.business.order_status.Order.objects") as objects, \
             mock.patch("apps.business.order_status.jidanpu_assignment_lock.lock_order_change", return_value=True) as order_lock, \
             mock.patch("apps.business.order_status.Performance") as performance, \
             mock.patch("apps.business.dailiantong_push.DailiantongPush") as pushes, \
             mock.patch("apps.business.peer_dispatch.notify_order_status_changed"), \
             mock.patch("apps.business.order_push.queue_status_change_push"), \
             mock.patch("apps.openapi.dispatch.notify_order_status_changed"), \
             mock.patch("apps.wecom.notifications.prepare_order_status", return_value=False) as notice, \
             mock.patch("apps.business.order_status_persistence.queue_order_status_notice") as worker_notice, \
             mock.patch("apps.business.order_status.PeerOrderLink"):
            objects.select_for_update.return_value.only.return_value.get.return_value = (
                self._StubOrder(OrderStatus.IN_PROGRESS)
            )
            performance.objects.filter.return_value.first.return_value = None
            performance.objects.filter.return_value.exists.return_value = False
            # 代练通闸、开放平台入队、下家状态钩子、打手侧回调都要读库，同样打桩。
            # 桩打在被查找的模块上（函数内懒导入）。去掉桩这条用例会查库而红。
            pushes.objects.filter.return_value.order_by.return_value.first.return_value = None
            with mock.patch.object(jidanpu_member, "queue_status_out", return_value=0), \
                 mock.patch(
                     "apps.takerapi.api.callback_recipients"
                     ".notify_order_status_changed_for_taker"
                 ):
                apply_transition(
                    order, OrderStatus.PAUSED, nodes={"order.change_status"},
                    actor="kefu01",
                )
        order_lock.assert_called_once_with(order.pk)
        notice.assert_called_once_with(order, OrderStatus.IN_PROGRESS, event_at=None)
        worker_notice.assert_called_once_with(
            order, OrderStatus.IN_PROGRESS, event_at=None, actor_id=None, reason="",
        )
        self.assertEqual(order.status, OrderStatus.PAUSED)
        self.assertEqual(order.updated_by, "kefu01")
        self.assertEqual(len(order.saved), 1)
        # `completed_at` 在进出「已完成」时要写；断言是整列相等，不是 assertIn，
        # 漏列或多列都会红。这条向量两列时刻都没变，所以期望仍是这四列。
        # 那两列按需追加：无条件列进去会把刚落库的值擦成 NULL。
        self.assertEqual(
            order.saved[0]["update_fields"],
            ["status", "updated_by", "completed_at", "updated_at"],
        )


class P3TransitionTests(TenantTestCase):
    """P3 对迁移表的三处改动（spec §4.1 / §4.1.1）。

    判别力要求：断言**节点集合整体相等**，不是「有人能过」——否则把
    `order.change_status` 写成抢单节点这种越权（代练员能自己把单退回未分配）不会被抓到。
    """

    def test_withdraw_edge_needs_one_of_its_three_gates(self):
        self.assertEqual(
            TRANSITIONS[(OrderStatus.WAITING_GRAB, OrderStatus.UNASSIGNED)],
            frozenset({
                "order.change_status",
                "order.clear_taker",
                "order.withdraw_from_grab_hall",
            }),
        )

    def test_edge_to_unassigned_needs_one_of_its_three_gates(self):
        """指向 `UNASSIGNED` 的边节点集 = `{16, 19, 117}`（按 **target** 定，D-0147 / R8）。

        ⚠️ **本条在 2026-09-24 被更新**：原名
        `test_clear_taker_edge_needs_change_status_or_clear_taker`，钉的是
        `(IN_PROGRESS, UNASSIGNED)` 的两节点集 `{16, 19}`。R8 之后节点集按目标态定，
        目标 `UNASSIGNED` 上还有「撤回抛单」这一支（`order.withdraw_from_grab_hall`，117）
        ⇒ 三节点集。
        判别力：三扇门各自单独放行、抢单节点仍被拒。
        """
        expected = frozenset({
            "order.change_status",
            "order.clear_taker",
            "order.withdraw_from_grab_hall",
        })
        self.assertEqual(TRANSITIONS[(OrderStatus.IN_PROGRESS, OrderStatus.UNASSIGNED)], expected)
        for node in expected:
            self.assertTrue(can_transition(OrderStatus.IN_PROGRESS, OrderStatus.UNASSIGNED, {node}))
        self.assertFalse(
            can_transition(OrderStatus.IN_PROGRESS, OrderStatus.UNASSIGNED, {"grab_hall.take_order"})
        )

    def test_taker_node_cannot_withdraw(self):
        """抢单节点不得撤回抛单（上一条的**行为**侧对照）。"""
        self.assertFalse(
            can_transition(
                OrderStatus.WAITING_GRAB, OrderStatus.UNASSIGNED, {"grab_hall.take_order"}
            )
        )

    def test_review_edge_needs_the_completed_node(self):
        """`PENDING_REVIEW → COMPLETED` 的节点集 = `{order.change_status_completed}`（17）。

        ⚠️ **本条在 2026-09-22 被更新**（D-0071，业务方裁定「取消审核闸门、订单完成即绩效生效」），
        并在 **2026-09-23 换轴**：改之前它叫 `test_review_edge_is_reviewer_only`，
        钉的是 `frozenset({Reviewer 旧角色})`——那是 P3a spec §4.1.1 收紧的产物（有审核闸门时
        「订单完成只能由客服主管的审核触发」）。业务方取消了那道闸门之后，原话是
        「**有权限的职位都可以用**」，而「有权限」在本项目 = 持有节点
        **`order.change_status_completed`（17）**（RBAC 的分工：角色是数据、节点是权限）
        ⇒ 边的节点集就是 17 本身（通用 `/transition/` 对已完成目标的门就是它）。
        ⚠️ 判别力**没有削弱**，只是换了方向：本条同时断「17 在集合里」与
        「16 **不在**里」（只断前者会漏掉「顺手写成全集」那种放宽；这里是逐字比对，不是包含）。
        """
        self.assertEqual(
            TRANSITIONS[(OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED)],
            frozenset({"order.change_status_completed"}),
        )
        self.assertFalse(
            can_transition(
                OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED, {"order.change_status"}
            )
        )

    def test_completed_node_can_complete_order(self):
        """持节点 17 可以把待验收的单改成已完成。

        ⚠️ **本条是 `test_cs_cannot_complete_order` 的**反向**更新**（/ D-0071），
        2026-09-23 换轴：旧断言钉的是 P3a 的收紧（`Role.CS` 不能完成）——那条口径随审核
        闸门一起被业务方取消。依据与详细理由见上一条与 `order_status.TRANSITIONS` 的注释；
        **授权**要节点 17（由 `order_views.get_permissions()` 与
        `OrderSerializer.get_allowed_transitions` 两处同源判据保证）。
        """
        self.assertTrue(
            can_transition(
                OrderStatus.PENDING_REVIEW,
                OrderStatus.COMPLETED,
                {"order.change_status_completed"},
            )
        )

    def test_the_whole_edge_table_matches_the_target_node_sets(self):
        """**全表比对**：每条的节点集都由它的 **target** 决定 + 一条抢单例外。

        期望表逐字取自 spec §4.4（D-0147 / R8）：

        | target | 节点集 |
        |---|---|
        | `UNASSIGNED` | 16 \\| 19 \\| 117 |
        | `WAITING_GRAB` | 36 |
        | `IN_PROGRESS` | 16 \\| 18/1162 \\| 17（**只有**来源态为 `WAITING_GRAB` 时再加 50） |
        | `PENDING_REVIEW` | 16 \\| 105 |
        | `COMPLETED` | 17（**只有**来源态为 `PENDING_REVIEW` 时才有这条边） |
        | `ISSUE` / `PAUSED` / `REFUNDED` | 16 |

        ⚠️ **本条在 2026-09-24 被整表替换**（原名 `test_explicit_edges_match_expected_table`）：
        旧表是**手写枚举**的 24 条边；R8 之后边由 `(src, target)` 循环生成，
        「显式枚举」不再存在 ⇒ 改成按 target 分组的全表比对。判别力**只增不减**。
        ⚠️ `WAITING_GRAB → IN_PROGRESS` 是唯一带 `grab_hall.take_order` 的边（T2 的边界）。
        ⚠️ **本批 F3，spec §2.3）**：入 `COMPLETED` 的边只剩一条 ⇒ 被删的
        六个来源态在这里**显式枚举**、逐个断「没有这条边」（不是跳过——跳过会让
        「误加回一条」静默通过）。
        """
        by_target = {
            OrderStatus.UNASSIGNED: frozenset({
                "order.change_status", "order.clear_taker", "order.withdraw_from_grab_hall",
            }),
            OrderStatus.WAITING_GRAB: frozenset({"order.send_to_grab_hall"}),
            OrderStatus.IN_PROGRESS: frozenset({
                "order.change_status", "order.assign_taker",
                "order.submit_payout_price", "order.change_status_completed",
            }),
            OrderStatus.PENDING_REVIEW: frozenset({
                "order.change_status", "order.submit_work",
            }),
            OrderStatus.COMPLETED: frozenset({"order.change_status_completed"}),
            # ⭐ 业务方裁定：`ISSUE` 加了 `_SUBMIT`——新端点 `/report-issue/`
            #    （门 = `order.submit_work`）的目标就是它（理由见 `order_status.py` 的注释块）。
            OrderStatus.ISSUE: frozenset({"order.change_status", "order.submit_work"}),
            OrderStatus.PAUSED: frozenset({"order.change_status"}),
            OrderStatus.REFUNDED: frozenset({"order.change_status"}),
        }
        #: 本批删掉的六个来源态（唯一保留的是 `PENDING_REVIEW`）。
        deleted_sources = {
            src for src in OrderStatus
            if src not in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED)
        }
        seen = set()
        for src in OrderStatus:
            for target in OrderStatus:
                if src == target:
                    continue
                if target == OrderStatus.COMPLETED and src in deleted_sources:
                    with self.subTest(src=src, target=target):
                        self.assertIsNone(TRANSITIONS.get((src, target)))
                    continue
                expected = by_target[target]
                if src == OrderStatus.WAITING_GRAB and target == OrderStatus.IN_PROGRESS:
                    expected = expected | {"grab_hall.take_order"}
                with self.subTest(src=src, target=target):
                    self.assertEqual(TRANSITIONS.get((src, target)), expected)
                seen.add((src, target))
        # 自证：循环真的盖满了**剩下的**每一条非自环的边（防「期望表为空 / 循环被写坏」的恒真）
        self.assertEqual(
            len(seen),
            len(OrderStatus) * (len(OrderStatus) - 1) - len(deleted_sources),
        )

    def test_completed_is_reachable_only_from_pending_review(self):
        """F3（2026-09-25，spec §2.3）：入 `COMPLETED` 的边只剩 `PENDING_REVIEW →` 一条。

        ⚠️ **本条替换了 R8/D-0071 的**
        `test_completed_is_reachable_from_every_other_status`（那条钉的是「除自环外
        每个来源态都能到」）——业务方的裁定把那条能力**收掉**了，
        判别力：逐条枚举六个被删的来源态
        （不是「有几个不行就行」）。
        """
        self.assertIn((OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED), TRANSITIONS)
        for source in OrderStatus:
            if source == OrderStatus.PENDING_REVIEW:
                continue
            with self.subTest(source=source):
                self.assertNotIn((source, OrderStatus.COMPLETED), TRANSITIONS)

    def test_exception_edges_are_change_status_only(self):
        """任意来源 → `ISSUE` 是 `{order.change_status, order.submit_work}`；
        `PAUSED`/`REFUNDED` 仍是 `{order.change_status}`（按 target 定）。

        ⚠️ **D-0147 / R8）**：旧版跳过 `REFUNDED` 作为来源态（那时它只有
        两条显式回池边）。R8 之后 `REFUNDED` 也有指向异常态的边，所以枚举面扩到
        **全部来源态**（自环除外）——判别力只增不减。

        ⚠️ **业务方裁定）**：`ISSUE` 这一档**不再**是「只有 16」——新增的
        `/report-issue/`（门 = `order.submit_work`，目标/来源态写死）的目标就是它
        ⇒ 期望集加 `order.submit_work`（理由与「不构成提权」见 `order_status.py` 里
        `ISSUE 为什么要有 _SUBMIT` 那段注释）。⛔ `PAUSED` / `REFUNDED` 两档**一个字都没动**
        ——它们仍只认 16，本用例按 target **逐档**给期望值，正是为了让这一改动可见。
        """
        by_target = {
            OrderStatus.ISSUE: frozenset({
                "order.change_status",
                # 新端点 `/report-issue/` 的门（与 `/submit/` 同一个权限点）
                "order.submit_work",
            }),
            OrderStatus.PAUSED: frozenset({"order.change_status"}),
            OrderStatus.REFUNDED: frozenset({"order.change_status"}),
        }
        for src in OrderStatus:
            for target, nodes in by_target.items():
                if src == target:
                    continue
                with self.subTest(src=src, target=target):
                    self.assertEqual(TRANSITIONS[(src, target)], nodes)


class TargetHookTests(TenantTestCase):
    """迁移副作用机制：`apply_transition` 是**唯一**的副作用出口。

    这组测试守的是「**机制**」，不是某一条具体副作用——所以它注册的是**探针钩子**，
    这样将来新增副作用时它仍然是绿的。

    探针必须挂在当前没有生产钩子的目标态上。注册表是模块级的，
    `clear_target_hooks` 会把那个键整条 pop 掉，连带清掉生产钩子，后跑的测试就看执行顺序。
    生产钩子在 `UNASSIGNED` / `WAITING_GRAB`（`performance.py`）、
    `IN_PROGRESS` / `PENDING_REVIEW`（`notifications.py`）、
    `COMPLETED` / `REFUNDED`（`settlement_hooks.py`）。
    本类挂 `PAUSED`。`setUp` 把「探针目标态没有生产钩子」钉住，谁往上加钩子会当场红。
    """

    #: 探针挂的目标态：必须是**没有生产钩子**的那一个（见类 docstring）
    PROBE_TARGET = OrderStatus.PAUSED

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            status=OrderStatus.IN_PROGRESS,
        )
        # ⚠️ **防回归判据（FIX 1 的重点）**：注册探针**之前**先确认这个目标态上没有生产钩子。
        #    `clear_target_hooks(target)` 是整条 pop（`order_status.py`），所以探针挂到有生产钩子的
        #    目标态上，`tearDown` 会把生产钩子一起清掉——同进程里后跑的测试就**取决于执行顺序**，
        #    默认按字母序时表现为「恰好全绿」的假保证。
        self.assertEqual(
            _TARGET_HOOKS.get(str(self.PROBE_TARGET)),
            None,
            msg=(
                f"探针目标态 {self.PROBE_TARGET} 上已有生产钩子——`clear_target_hooks` 会把它们"
                "一起 pop 掉，后续测试会随执行顺序静默变红。请给 PROBE_TARGET 换一个"
                "没有生产钩子的目标态（当前生产钩子：UNASSIGNED / WAITING_GRAB / "
                "IN_PROGRESS / PENDING_REVIEW / COMPLETED / REFUNDED）。"
            ),
        )
        self.calls = []
        register_target_hook(
            self.PROBE_TARGET,
            lambda order, from_status: self.calls.append((order.order_no, from_status)),
        )

    def tearDown(self):
        # ⚠️ 注册表是**模块级**的：不清理就会污染同进程里的其它测试。
        #    只清自己挂的那个目标态——**它上面没有生产钩子**（由 `setUp` 的断言机械保证；
        #    若将来有人往 `PAUSED` 加生产钩子，`setUp` 会先红，而不是这里静默 pop 掉它）。
        clear_target_hooks(self.PROBE_TARGET)

    def test_hook_fires_with_from_status(self):
        # `IN_PROGRESS → PAUSED` 的节点集是 `{order.change_status}`，所以取它。
        apply_transition(
            self.order, self.PROBE_TARGET, nodes={"order.change_status"}, actor="cs01"
        )
        self.assertEqual(
            self.calls, [(self.order.order_no, OrderStatus.IN_PROGRESS)]
        )

    def test_hook_does_not_fire_on_illegal_transition(self):
        """非法迁移**没有发生**，副作用就不该跑——否则会改到一个没迁成功的单。

        ⚠️ **靶子在 2026-09-24 换过**（D-0147 / R8）：原用 `COMPLETED → IN_PROGRESS`，
        R8 之后那条边**合法**。R8 之下唯一仍非法的形状是**自环**，所以换用它。
        起点状态直接置成 `COMPLETED`——**不经过 `apply_transition`**，所以不会触发任何钩子。
        """
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status"])
        with self.assertRaises(IllegalTransition):
            apply_transition(
                self.order, OrderStatus.COMPLETED, nodes={"order.change_status_completed"}
            )
        self.assertEqual(self.calls, [])

    def test_hook_does_not_fire_for_other_targets(self):
        apply_transition(
            self.order, OrderStatus.ISSUE, nodes={"order.change_status"}
        )
        self.assertEqual(self.calls, [])


class AnyStatusPairTests(SimpleTestCase):
    """T1（D-0147 / spec §4.4 的 R8）的**当前形态**：除「入 `COMPLETED`」那一组外，
    任意状态互转都有边，且没有自环。

    ⚠️ **本批 F3，spec §2.3）收紧了 R8 的一处**：入 `COMPLETED` 的边只剩
    「待验收 → 已完成」一条 ⇒ 六个来源态到 `COMPLETED` 的边**不存在**。这是**显式枚举
    的例外**（不是把断言放宽成「大部分有边就行」）：下面把它与自环一起逐个断。

    节点集那半由 `P3TransitionTests.test_the_whole_edge_table_matches_the_target_node_sets`
    做全表比对；`apply_transition` 的行为侧由下面的 `ApplyTransitionAnyPairTests` 承担。
    """

    #: 入 `COMPLETED` 的边只留这一个来源态（spec §2.3）。
    COMPLETED_SOURCE = OrderStatus.PENDING_REVIEW

    def test_every_ordered_pair_has_an_edge_except_the_removed_completed_edges(self):
        for src in OrderStatus:
            for target in OrderStatus:
                with self.subTest(src=src, target=target):
                    if src == target:
                        self.assertNotIn(
                            (src, target), TRANSITIONS, f"{src} 不该有指向自己的自环"
                        )
                        continue
                    if target == OrderStatus.COMPLETED and src != self.COMPLETED_SOURCE:
                        self.assertNotIn(
                            (src, target),
                            TRANSITIONS,
                            f"{src} -> COMPLETED 的入边本批已被删除（spec §2.3）",
                        )
                        continue
                    self.assertIn(
                        (src, target), TRANSITIONS, f"{src} -> {target} 缺边"
                    )


class ApplyTransitionAnyPairTests(TenantTestCase):
    """T1 的行为侧：`apply_transition` 对**除入 `COMPLETED` 那一组外**的每一对状态放行，
    被删的六条入边则**抛 `IllegalTransition` 且不改状态**。

    ⚠️ **矩阵在「目标态无钩子」的口径下跑**：本用例只问状态机这一层「边在不在、
    节点够不够」，所以进入时把模块级的目标态钩子（回池 / 通知 / 结算）暂存并清空，
    退出时**原样还原**（`finally` + 还原后的自证断言）。否则每个 `REFUNDED` /
    `COMPLETED` 目标都会真的跑退款 / 结算钩子——那是另一个维度的行为，已有各自的用例
    （`TargetHookTests`、`tests/test_refund_full.py` 等）。
    """

    #: 全节点集合：每一对状态都有至少一个节点在它的边集里（含抢单节点）。
    ALL_NODES = frozenset({
        "order.change_status",
        "order.change_status_completed",
        "order.set_offer_price",
        "order.send_to_grab_hall",
        "order.submit_payout_price",
        "order.clear_taker",
        "order.withdraw_from_grab_hall",
        "order.assign_taker",
        "order.submit_work",
        "grab_hall.take_order",
    })

    def test_apply_transition_allows_every_ordered_pair(self):
        game = Game.objects.create(name="测试游戏甲")
        saved_hooks = {target: list(funcs) for target, funcs in _TARGET_HOOKS.items()}
        _TARGET_HOOKS.clear()
        try:
            index = 0
            for src in OrderStatus:
                for target in OrderStatus:
                    if src == target:
                        continue
                    index += 1
                    order = Order.objects.create(
                        game=game,
                        order_no=f"2026092400{index:010d}",
                        status=src,
                    )
                    removed = (
                        target == OrderStatus.COMPLETED
                        and src != OrderStatus.PENDING_REVIEW
                    )
                    with self.subTest(src=src, target=target):
                        if removed:
                            with self.assertRaises(IllegalTransition) as ctx:
                                apply_transition(
                                    order, target, nodes=self.ALL_NODES, actor="测试操作人甲"
                                )
                            self.assertEqual(
                                str(ctx.exception),
                                "必须先把订单状态改成待验收才能改成已完成",
                            )
                            order.refresh_from_db()
                            self.assertEqual(order.status, src)
                            continue
                        apply_transition(
                            order, target, nodes=self.ALL_NODES, actor="测试操作人甲"
                        )
                        order.refresh_from_db()
                        self.assertEqual(order.status, target)
        finally:
            _TARGET_HOOKS.clear()
            _TARGET_HOOKS.update(saved_hooks)
        # 还原自证：注册表回到进入前的形状（否则同进程后续用例会静默丢副作用）
        self.assertEqual(
            {target: list(funcs) for target, funcs in _TARGET_HOOKS.items()},
            saved_hooks,
        )


class TakeOrderBoundaryTests(SimpleTestCase):
    """T2（D-0147 / spec §4.4 边界 1）：`grab_hall.take_order` **只**开在大厅那条边上。

    `grab_hall.take_order` 是代练员自助接单（`/claim/`）的节点。按 target 铺边时若把它
    也铺到「任意状态 → 代练中」，持该节点的账号就能把任意单（含已完成 / 已退单）拉进
    代练中——那是提权。变异自证见 task-1 报告。
    """

    def test_take_order_appears_only_on_the_hall_edge(self):
        offenders = []
        for src in OrderStatus:
            for target in OrderStatus:
                if src == target:
                    continue
                edge = TRANSITIONS.get((src, target))
                if edge is None:
                    # 本批（F3）删掉的入 `COMPLETED` 那六条：没有边可谈
                    continue
                is_hall_edge = (
                    src == OrderStatus.WAITING_GRAB and target == OrderStatus.IN_PROGRESS
                )
                if is_hall_edge:
                    self.assertIn("grab_hall.take_order", edge)
                elif "grab_hall.take_order" in edge:
                    offenders.append((str(src), str(target)))
        self.assertEqual(offenders, [], "这些边不该带 grab_hall.take_order")

    def test_the_take_order_node_cannot_move_a_non_hall_order_into_progress(self):
        for src in OrderStatus:
            if src == OrderStatus.WAITING_GRAB:
                continue
            with self.subTest(src=src):
                self.assertFalse(
                    can_transition(src, OrderStatus.IN_PROGRESS, {"grab_hall.take_order"})
                )
        # 阳性哨兵：大厅那条边本身仍放行（否则「全拒绝」也能让上面通过）
        self.assertTrue(
            can_transition(
                OrderStatus.WAITING_GRAB, OrderStatus.IN_PROGRESS, {"grab_hall.take_order"}
            )
        )


class TakeOrderBoundaryBehaviorTests(TenantTestCase):
    """T2 的行为侧：只持抢单节点的调用方**推不动**非大厅单（变异必杀的靶子）。"""

    def test_apply_transition_rejects_a_take_order_only_caller_from_completed(self):
        game = Game.objects.create(name="测试游戏甲")
        order = Order.objects.create(
            game=game,
            order_no="20260924000000000901",
            status=OrderStatus.COMPLETED,
        )
        with self.assertRaises(IllegalTransition):
            apply_transition(
                order, OrderStatus.IN_PROGRESS, nodes={"grab_hall.take_order"}
            )
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)


class SettledStatusChangeGuardTests(TenantTestCase):
    """⭐ D4b 的「已结账」判据必须是**三字段并集**（spec §4.4.3 / §7.2 #11）。

    本用例钉「已结账」是三字段并集。只看 `settle_status` 一列时，
    状态仍是未结账、只把 `settled_at` 或 `settled_amount` 置上的行会漏过去，这条用例会红。

    目标态必须取非池内：池内两态会先撞另一道本来就看三字段的闸，单看状态列的退化照样绿。
    所以取 `COMPLETED → PAUSED`，让这一道成为唯一能拦住它的闸。
    必须真库：打桩的 Performance 替身分不出读了哪几列。
    """

    #: 只置**非 `settle_status`** 的那两列**之一**：状态列保持「未结账」。
    NON_STATUS_SETTLE_SHAPES = (
        ("settled_at", {"settled_at": timezone.now()}),
        ("settled_amount", {"settled_amount": Decimal("123.00")}),
    )

    def test_a_row_settled_only_by_the_other_columns_still_blocks_a_non_pool_target(self):
        game = Game.objects.create(name="结账判据测试游戏")
        for index, (field_name, shape) in enumerate(self.NON_STATUS_SETTLE_SHAPES):
            order = Order.objects.create(
                game=game,
                order_no=f"2026092800{index + 1:010d}",
                status=OrderStatus.COMPLETED,
                hall_price=Decimal("100.00"),
            )
            perf = Performance.objects.create(
                order=order,
                user=User.objects.create_user(username=f"结账判据打手{index}"),
                emp_type=EmpType.MAIN,
                temp_amount=Decimal("20.00"),
                bonus_amount=Decimal("20.00"),
                taker_price=Decimal("55.00"),
                # ⚠️ 状态列**不是**「已结账」——本用例问的就是「另外两列算不算数」。
                settle_status="未结账",
                settled_at=None,
                settled_amount=None,
            )
            for key, value in shape.items():
                setattr(perf, key, value)
            perf.save(update_fields=[*shape, "updated_at"])
            with self.subTest(settled_field=field_name):
                # 前置哨兵（防「夹具根本没置上」那种静默空转）：状态列仍是未结账、
                # 而模型属性判它**已结账**（= 另一列真的置上了）。
                perf.refresh_from_db()
                self.assertEqual(perf.settle_status, "未结账")
                self.assertTrue(perf.is_settled)
                with self.assertRaises(IllegalTransition) as ctx:
                    apply_transition(
                        order,
                        OrderStatus.PAUSED,
                        nodes={"order.change_status"},
                        actor="测试操作人甲",
                    )
                # 字面值（E-0024：不从被测模块 import 那句常量，否则改坏常量不会红）
                self.assertEqual(
                    str(ctx.exception),
                    "该订单的绩效已结账，不能改状态；请先撤销结账后再操作",
                )
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.COMPLETED)
                self.assertEqual(perf.settle_status, "未结账")


class DailiantongStatusLockTests(TenantTestCase):
    """⭐ A2（计划 Rev.9 §2，2026-10-27）：**已抛到代练通的单禁止手工改状态**。

    业务方口径：「状态必须与代练通一致（绝不能不一致）」⇒ 本地状态只能由代练通同步回写。
    闸落在**唯一写入口** `apply_transition`（服务层也走它），所以 `/transition/`、批量迁移、
    `/submit/`、`/review/`、退款全部被它拦住；机器路径用显式开关放行。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20261027000000000901",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN)
        DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000301",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            remote_status=11,
            attempts=1,
        )

    def test_a_manual_status_change_is_rejected_with_a_chinese_reason(self):
        """用**状态机本来允许**的那条边（待验收 → 已完成）证明拦它的是代练通那道闸。

        ⚠️ 若用「待抢单 → 已完成」，先在状态机那一层就被
        `COMPLETE_REQUIRES_REVIEW_MESSAGE` 拒了 ⇒ 测不到本闸。
        """
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        with self.assertRaises(IllegalTransition) as ctx:
            apply_transition(
                self.order,
                OrderStatus.COMPLETED,
                nodes={"order.change_status_completed"},
                actor="客服甲",
            )
        message = str(ctx.exception)
        self.assertIn("代练通", message)
        self.assertIn("不能手工修改", message)
        self.assertIn("19000000000000000301", message)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)

    def test_a_plain_status_change_is_also_rejected(self):
        with self.assertRaises(IllegalTransition) as ctx:
            apply_transition(
                self.order,
                OrderStatus.PAUSED,
                nodes={"order.change_status"},
                actor="客服甲",
            )
        self.assertIn("代练通", str(ctx.exception))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_the_sync_path_can_still_write_the_status(self):
        apply_transition(
            self.order,
            OrderStatus.IN_PROGRESS,
            nodes={"order.change_status"},
            actor="代练通同步",
            allow_status_change_while_pushed=True,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_a_failed_or_closed_push_does_not_lock_the_order(self):
        for state in (DailiantongPush.State.FAILED, DailiantongPush.State.CLOSED):
            with self.subTest(state=state):
                push = DailiantongPush.objects.get(order=self.order)
                push.state = state
                push.save(update_fields=["state"])
                self.order.status = OrderStatus.WAITING_GRAB
                self.order.save(update_fields=["status"])

                apply_transition(
                    self.order,
                    OrderStatus.PAUSED,
                    nodes={"order.change_status"},
                    actor="客服甲",
                )
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, OrderStatus.PAUSED)

    def test_an_unknown_push_without_a_serial_does_not_lock_the_order(self):
        """`unknown` 没有上游单号（超时）⇒ 不锁：它可能根本没发出去，人工核对是出口。"""
        push = DailiantongPush.objects.get(order=self.order)
        push.state = DailiantongPush.State.UNKNOWN
        push.serial_no = None
        push.save(update_fields=["state", "serial_no"])

        apply_transition(
            self.order,
            OrderStatus.PAUSED,
            nodes={"order.change_status"},
            actor="客服甲",
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PAUSED)
