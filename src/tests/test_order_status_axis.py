"""单轴化的承重测试：**50 条边**的节点判据 + 图论 + 新旧包含关系。

⚠️ **D-0147 / R8）**：边表从 42 条整体放开到 56 条（`src != target` 的
每一对），节点集按 **target** 定 ⇒ 本文件的期望表与图论判据随之更新。
⚠️ **本批 F3，spec §2.3）**：入 `COMPLETED` 的边只剩
`PENDING_REVIEW → COMPLETED` 一条 ⇒ 从 56 条收到 **50 条**；③ 的包含关系对那五条
旧表里的「→ 已完成」边**显式豁免**（业务方裁定，见 `_REMOVED_BY_COMPLETE_GATE`）。
判据形态（刻意的，见 spec §10）：
① 每条边两向：持该边**某一个**节点 ⇒ 放行；持**与该边无关**的节点 ⇒ 拒绝 + 消息可指认门；
② 图论：`can_transition(from, to)`（不传 nodes）对 50 条边全真、对非边全假；
③ 包含关系（**可观察形态**）：每个内置职位 × **旧表 42 条边**，
   「旧判据放行 ∧ 该边可被该职位实际走到」⇒ 新判据必须放行。
   只比状态机、不加「可被实际走到」这一层会**必红**（见下面 `_ENDPOINT_GATES` 的说明）；
④ 结构：9 个端点的节点门 ⊆ 它可写边的节点集。

期望值一律写**字面量**（不从生产模块派生——E-0024 的自指陷阱）。
"""
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business.models import OrderStatus
from apps.business.order_status import (
    SELF_TRANSITION_MESSAGE,
    TRANSITIONS,
    IllegalTransition,
    apply_transition,
    can_transition,
    transition_denial,
)
from apps.rbac.models import Role
from apps.rbac.seed import seed_builtin_roles, seed_catalog


# 期望表按 target 分组写字面量，再展开成边。不要从生产模块 import（自指）。
_TARGET_NODES = {
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
    # ⭐ 业务方裁定：`ISSUE` 加了 `_SUBMIT`——新端点 `/report-issue/` 的目标
    #    就是它（目标/来源态写死，理由见生产模块 `order_status.py` 的注释块）。
    OrderStatus.ISSUE: frozenset({"order.change_status", "order.submit_work"}),
    OrderStatus.PAUSED: frozenset({"order.change_status"}),
    OrderStatus.REFUNDED: frozenset({"order.change_status"}),
}

#: 唯一一条带 `grab_hall.take_order` 的边（T2 的边界：代练员自助接单只在大厅那条路上）。
#: ⚠️ spec §2.3：入 `COMPLETED` 的边只剩
#: `PENDING_REVIEW → COMPLETED` 一条 ⇒ 本表从 56 条收到 **50 条**（六个来源态 × 一条）。
_EDGES = {
    (src, target): (
        nodes | {"grab_hall.take_order"}
        if target == OrderStatus.IN_PROGRESS and src == OrderStatus.WAITING_GRAB
        else nodes
    )
    for src in OrderStatus
    for target, nodes in _TARGET_NODES.items()
    if src != target
    and (target != OrderStatus.COMPLETED or src == OrderStatus.PENDING_REVIEW)
}
assert len(_EDGES) == 50

#: 一个**存在**于目录里、但**不在这 56 条边任何一条的节点集里**的 code（逐条核过）。
_UNRELATED_NODE = "finance.payroll"


# ── ③ 的旧角色侧：测试内硬编码，**不** import 生产常量（生产符号已删除）──────────
#: 旧角色 → 持有它的内置职位。依据（读码）：`apps/business/roles.py` 的
#: `GROUP_TO_ROLES`（本线已删除，故此处**必须**硬编码，不能 import）：
#:   客服 → {CS}；客服主管 → {SUPERVISOR, REVIEWER}；代练员 → {TAKER}；
#:   系统开发员 → {TAKER}；系统管理员 → 全集。
_OLD_ROLE_OWNERS = {
    "CS": {"客服"},
    "REVIEWER": {"客服主管"},
    "SUPERVISOR": {"客服主管"},
    "TAKER": {"代练员", "系统开发员"},
}
#: `_OLD_ROLES` 里 4 个常量 → 它们各自展开出的角色集合。
_OLD_ROLE_SETS = {
    "CS": {"CS"},
    "TAKER": {"TAKER"},
    "REVIEWER": {"REVIEWER"},
    "ANY_STAFF": {"CS", "REVIEWER"},
}
#: 42 条边 → 旧角色集（把上表按边展开；旧表与节点表逐条对照 spec §4.1 / §4.2）。
_OLD_ROLES = {
    (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB): _OLD_ROLE_SETS["CS"],
    (OrderStatus.UNASSIGNED, OrderStatus.IN_PROGRESS): _OLD_ROLE_SETS["CS"],
    (OrderStatus.WAITING_GRAB, OrderStatus.IN_PROGRESS): _OLD_ROLE_SETS["TAKER"],
    (OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW): _OLD_ROLE_SETS["TAKER"],
    (OrderStatus.UNASSIGNED, OrderStatus.COMPLETED): _OLD_ROLE_SETS["ANY_STAFF"],
    (OrderStatus.WAITING_GRAB, OrderStatus.COMPLETED): _OLD_ROLE_SETS["ANY_STAFF"],
    (OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED): _OLD_ROLE_SETS["ANY_STAFF"],
    (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED): _OLD_ROLE_SETS["ANY_STAFF"],
    (OrderStatus.PENDING_REVIEW, OrderStatus.IN_PROGRESS): _OLD_ROLE_SETS["REVIEWER"],
    (OrderStatus.ISSUE, OrderStatus.IN_PROGRESS): (
        _OLD_ROLE_SETS["TAKER"] | _OLD_ROLE_SETS["CS"]
    ),
    (OrderStatus.PAUSED, OrderStatus.IN_PROGRESS): (
        _OLD_ROLE_SETS["TAKER"] | _OLD_ROLE_SETS["CS"]
    ),
    (OrderStatus.ISSUE, OrderStatus.UNASSIGNED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.ISSUE, OrderStatus.WAITING_GRAB): _OLD_ROLE_SETS["CS"],
    (OrderStatus.ISSUE, OrderStatus.PENDING_REVIEW): _OLD_ROLE_SETS["CS"],
    (OrderStatus.ISSUE, OrderStatus.COMPLETED): _OLD_ROLE_SETS["ANY_STAFF"],
    (OrderStatus.PAUSED, OrderStatus.UNASSIGNED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PAUSED, OrderStatus.WAITING_GRAB): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PAUSED, OrderStatus.PENDING_REVIEW): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PAUSED, OrderStatus.COMPLETED): _OLD_ROLE_SETS["ANY_STAFF"],
    (OrderStatus.WAITING_GRAB, OrderStatus.UNASSIGNED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.IN_PROGRESS, OrderStatus.UNASSIGNED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.REFUNDED, OrderStatus.UNASSIGNED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.REFUNDED, OrderStatus.WAITING_GRAB): _OLD_ROLE_SETS["CS"],
    (OrderStatus.UNASSIGNED, OrderStatus.ISSUE): _OLD_ROLE_SETS["CS"],
    (OrderStatus.UNASSIGNED, OrderStatus.PAUSED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.UNASSIGNED, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.WAITING_GRAB, OrderStatus.ISSUE): _OLD_ROLE_SETS["CS"],
    (OrderStatus.WAITING_GRAB, OrderStatus.PAUSED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.WAITING_GRAB, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.IN_PROGRESS, OrderStatus.ISSUE): _OLD_ROLE_SETS["CS"],
    (OrderStatus.IN_PROGRESS, OrderStatus.PAUSED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.IN_PROGRESS, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PENDING_REVIEW, OrderStatus.ISSUE): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PENDING_REVIEW, OrderStatus.PAUSED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PENDING_REVIEW, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.COMPLETED, OrderStatus.ISSUE): _OLD_ROLE_SETS["CS"],
    (OrderStatus.COMPLETED, OrderStatus.PAUSED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.COMPLETED, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.ISSUE, OrderStatus.PAUSED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.ISSUE, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PAUSED, OrderStatus.ISSUE): _OLD_ROLE_SETS["CS"],
    (OrderStatus.PAUSED, OrderStatus.REFUNDED): _OLD_ROLE_SETS["CS"],
}
assert len(_OLD_ROLES) == 42

#: ⭐ **本批（F3，spec §2.3）显式豁免的旧边**：旧表里「→ 已完成」那五条里，本批只留
#: `PENDING_REVIEW → COMPLETED`，其余**逐条枚举**在这里 —— ③ 的包含关系对它们
#: 不再成立，这是**业务方的裁定**，不是实现收窄。
#: ⛔ 不要把它们从 `_OLD_ROLES` 里删掉：留着 + 显式豁免，才能对「顺手又放回来一条」
#: 保持可见（删掉就是「不留半新半旧」的反面：判据的证明力被悄悄削掉）。
_REMOVED_BY_COMPLETE_GATE = {
    (OrderStatus.UNASSIGNED, OrderStatus.COMPLETED),
    (OrderStatus.WAITING_GRAB, OrderStatus.COMPLETED),
    (OrderStatus.IN_PROGRESS, OrderStatus.COMPLETED),
    (OrderStatus.ISSUE, OrderStatus.COMPLETED),
    (OrderStatus.PAUSED, OrderStatus.COMPLETED),
}
assert _REMOVED_BY_COMPLETE_GATE <= set(_OLD_ROLES)
assert not (_REMOVED_BY_COMPLETE_GATE & set(_EDGES))


# ── ③ 的「可被实际走到」那一层（spec §10.3 第 2 条的机械形态）──────────────────
#: 该边**唯一**的写入口 → 端点的类级节点门（字面量，来自 spec §1.1 / §4.3 的实测表）。
#: 一个真实用户要「走到」某条边，必须①状态机放行 **且** ②能过某个写入口的节点门。
_ENDPOINT_GATES = {
    "E1": frozenset({"order.change_status"}),            # 非 COMPLETED / 非 WAITING_GRAB 目标
    # ⭐ 计划 ④：`/throw-to-hall/` 改判**独占** 36（原来是三选一）。
    "E2": frozenset({"order.send_to_grab_hall"}),
    "E3": frozenset({"order.withdraw_from_grab_hall"}),
    "E4": frozenset({"order.assign_taker", "order.submit_payout_price"}),
    "E5": frozenset({"grab_hall.take_order"}),
    "E6": frozenset({"order.clear_taker"}),
    "E7": frozenset({"order.submit_work"}),
    "E8": frozenset({"order.change_status_completed"}),
    #: ⭐ 业务方裁定：`/report-issue/`（报问题单）——与 E7 同一个权限点，
    #: 但**目标写死 `ISSUE`**（见 `order_views.OrderViewSet.report_issue`）。
    "E9": frozenset({"order.submit_work"}),
}
_E1_DONE = frozenset({"order.change_status_completed"})   # E1 在目标为 COMPLETED 时的门
#: ⭐ 计划 ④：E1 在目标为 `WAITING_GRAB` 时的门（业务方拍板 F）——
#: 与 `OrderViewSet.TRANSITION_TARGET_PERMISSIONS` 逐条对拍（见 ④ 的锚定用例）。
_E1_WAITING_GRAB = frozenset({"order.send_to_grab_hall"})

#: 边 → 能写它的端点（spec §4.3 的表）。E1 是通用流转 ⇒ 每条边都在里面。
_EDGE_ENDPOINTS = {
    (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB): ("E1", "E2"),
    (OrderStatus.UNASSIGNED, OrderStatus.IN_PROGRESS): ("E1", "E4"),
    (OrderStatus.WAITING_GRAB, OrderStatus.IN_PROGRESS): ("E1", "E5"),
    (OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW): ("E1", "E7"),
    (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED): ("E1", "E8"),
    (OrderStatus.PENDING_REVIEW, OrderStatus.IN_PROGRESS): ("E1", "E8"),
    (OrderStatus.ISSUE, OrderStatus.IN_PROGRESS): ("E1",),
    (OrderStatus.PAUSED, OrderStatus.IN_PROGRESS): ("E1",),
    (OrderStatus.ISSUE, OrderStatus.UNASSIGNED): ("E1", "E6"),
    (OrderStatus.ISSUE, OrderStatus.WAITING_GRAB): ("E1", "E2"),
    (OrderStatus.ISSUE, OrderStatus.PENDING_REVIEW): ("E1",),
    (OrderStatus.PAUSED, OrderStatus.UNASSIGNED): ("E1", "E6"),
    (OrderStatus.PAUSED, OrderStatus.WAITING_GRAB): ("E1", "E2"),
    (OrderStatus.PAUSED, OrderStatus.PENDING_REVIEW): ("E1",),
    (OrderStatus.WAITING_GRAB, OrderStatus.UNASSIGNED): ("E1", "E3", "E6"),
    (OrderStatus.IN_PROGRESS, OrderStatus.UNASSIGNED): ("E1", "E6"),
    (OrderStatus.REFUNDED, OrderStatus.UNASSIGNED): ("E1", "E6"),
    (OrderStatus.REFUNDED, OrderStatus.WAITING_GRAB): ("E1", "E2"),
    (OrderStatus.UNASSIGNED, OrderStatus.ISSUE): ("E1",),
    (OrderStatus.UNASSIGNED, OrderStatus.PAUSED): ("E1",),
    (OrderStatus.UNASSIGNED, OrderStatus.REFUNDED): ("E1",),
    (OrderStatus.WAITING_GRAB, OrderStatus.ISSUE): ("E1",),
    (OrderStatus.WAITING_GRAB, OrderStatus.PAUSED): ("E1",),
    (OrderStatus.WAITING_GRAB, OrderStatus.REFUNDED): ("E1",),
    #: ⭐ `/report-issue/`（E9）也写这条边——与 E1 并列（E1 走通用门，
    #: E9 走受限门；两者的节点门都 ⊆ 本边的节点集，由 ④ 检查）。
    (OrderStatus.IN_PROGRESS, OrderStatus.ISSUE): ("E1", "E9"),
    (OrderStatus.IN_PROGRESS, OrderStatus.PAUSED): ("E1",),
    (OrderStatus.IN_PROGRESS, OrderStatus.REFUNDED): ("E1",),
    (OrderStatus.PENDING_REVIEW, OrderStatus.ISSUE): ("E1",),
    (OrderStatus.PENDING_REVIEW, OrderStatus.PAUSED): ("E1",),
    (OrderStatus.PENDING_REVIEW, OrderStatus.REFUNDED): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.ISSUE): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.PAUSED): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.REFUNDED): ("E1",),
    (OrderStatus.ISSUE, OrderStatus.PAUSED): ("E1",),
    (OrderStatus.ISSUE, OrderStatus.REFUNDED): ("E1",),
    (OrderStatus.PAUSED, OrderStatus.ISSUE): ("E1",),
    (OrderStatus.PAUSED, OrderStatus.REFUNDED): ("E1",),
    # ── R8 新铺的 14 条（D-0147）：来源态之间原本没有边的那些对 ──────────────────
    # ⚠️ 这些边今天**只有**通用 `/transition/`（E1）这一个写入口——原先的专属端点
    #    （`/throw-to-hall/`、`/claim/`、`/clear-taker/` 等）各自有自己的来源态前置判据，
    #    本表只说「端点的类级节点门 ⊆ 边的节点集」这一层（④）。
    (OrderStatus.PENDING_REVIEW, OrderStatus.UNASSIGNED): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.UNASSIGNED): ("E1",),
    (OrderStatus.IN_PROGRESS, OrderStatus.WAITING_GRAB): ("E1",),
    (OrderStatus.PENDING_REVIEW, OrderStatus.WAITING_GRAB): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.WAITING_GRAB): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.IN_PROGRESS): ("E1",),
    (OrderStatus.REFUNDED, OrderStatus.IN_PROGRESS): ("E1",),
    (OrderStatus.UNASSIGNED, OrderStatus.PENDING_REVIEW): ("E1",),
    (OrderStatus.WAITING_GRAB, OrderStatus.PENDING_REVIEW): ("E1",),
    (OrderStatus.COMPLETED, OrderStatus.PENDING_REVIEW): ("E1",),
    (OrderStatus.REFUNDED, OrderStatus.PENDING_REVIEW): ("E1",),
    (OrderStatus.REFUNDED, OrderStatus.ISSUE): ("E1",),
    (OrderStatus.REFUNDED, OrderStatus.PAUSED): ("E1",),
}
assert set(_EDGE_ENDPOINTS) == set(_EDGES)


def _gate_for(endpoint, edge):
    """该端点写该边时要过的节点门（E1 按目标态分：COMPLETED ⇒ 17、WAITING_GRAB ⇒ 36）。"""
    if endpoint == "E1" and edge[1] == OrderStatus.COMPLETED:
        return _E1_DONE
    if endpoint == "E1" and edge[1] == OrderStatus.WAITING_GRAB:
        return _E1_WAITING_GRAB
    return _ENDPOINT_GATES[endpoint]


def _titles_old_roles(title):
    """该职位在旧角色词表下持有的角色集（`系统管理员` = 四个角色的全集）。"""
    if title == "系统管理员":
        return set(_OLD_ROLE_OWNERS)
    return {role for role, owners in _OLD_ROLE_OWNERS.items() if title in owners}


def _held_nodes(title):
    """该职位**库里种子数据**里的节点 code（不 import 生产常量推导）。"""
    return set(Role.objects.get(name=title).permissions.values_list("code", flat=True))


def _reachable(title, edge):
    """该职位**能不能真的走到**这条边：端点门 ∩ 职位持有的节点 ≠ ∅。"""
    held = _held_nodes(title)
    for endpoint in _EDGE_ENDPOINTS[edge]:
        if _gate_for(endpoint, edge) & held:
            return True
    return False


class EdgeAxisTests(SimpleTestCase):
    """① 每条边两向 + ② 图论（纯逻辑，不碰数据库）。"""

    def test_production_table_keys_are_exactly_the_literal_edges(self):
        self.assertEqual(set(TRANSITIONS), set(_EDGES))

    def test_c1_every_edge_has_its_literal_node_set_and_allows_its_own_nodes(self):
        """① 放行侧：持该边**某一个**节点 ⇒ 放行；顺手把节点集与字面量表逐条比对。"""
        for edge, nodes in _EDGES.items():
            with self.subTest(edge=edge):
                self.assertEqual(TRANSITIONS.get(edge), nodes)
            self.assertTrue(nodes, f"{edge} 的节点集是空的——空集对任何人都拒绝")
            for node in sorted(nodes):
                with self.subTest(edge=edge, node=node):
                    self.assertTrue(can_transition(edge[0], edge[1], nodes={node}))
                    self.assertIsNone(transition_denial(edge[0], edge[1], {node}))

    def test_c1_unrelated_node_is_denied_and_the_message_points_at_the_gate(self):
        """① 拒绝侧：持与之无关的节点 ⇒ 拒绝，且消息能指认「节点不够」这扇门。"""
        for edge in _EDGES:
            with self.subTest(edge=edge):
                self.assertFalse(
                    can_transition(edge[0], edge[1], nodes={_UNRELATED_NODE})
                )
                message = transition_denial(edge[0], edge[1], {_UNRELATED_NODE})
                self.assertIsNotNone(message)
                self.assertIn("非法状态迁移", message)
                self.assertTrue(
                    message.endswith("（缺少所需权限节点）"),
                    f"{edge} 的拒绝消息没有指认节点门：{message!r}",
                )

    def test_missing_edge_denial_is_distinguishable_from_a_node_shortage(self):
        """边不存在的那一支**不带**「缺少所需权限节点」后缀（A10 的两种拒绝可分辨）。

        ⚠️ 靶子换过两次：D-0147 / R8）原用 `COMPLETED → IN_PROGRESS`，那条边
        现在**合法**，于是改用**自环**；⭐ **本线 T4）** 自环改回**一句人话**
        （`SELF_TRANSITION_MESSAGE`），不再是通用那句 ⇒ 通用那句的样本换成
        「**当前状态不是一个已知的 `OrderStatus` 码**」（存量脏行）那一档。
        """
        self.assertEqual(
            transition_denial("ARCHIVED", OrderStatus.UNASSIGNED, {_UNRELATED_NODE}),
            "非法状态迁移：ARCHIVED → UNASSIGNED",
        )
        # ⭐ T4：自环**不再**落进这一支（值钉在 `tests/test_order_status.py`；这里只钉形状）。
        self.assertEqual(
            transition_denial(OrderStatus.COMPLETED, OrderStatus.COMPLETED, {_UNRELATED_NODE}),
            SELF_TRANSITION_MESSAGE.format(status_label=OrderStatus.COMPLETED.label),
        )
        # 节点不够那一支仍带后缀 ⇒ 两种拒绝仍可分辨。
        self.assertEqual(
            transition_denial(
                OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB, {_UNRELATED_NODE}
            ),
            f"非法状态迁移：{OrderStatus.UNASSIGNED} → {OrderStatus.WAITING_GRAB}"
            "（缺少所需权限节点）",
        )

    def test_c2_every_edge_is_true_without_nodes(self):
        """② 图论：不传 nodes 时，56 条边全真。"""
        for edge in _EDGES:
            with self.subTest(edge=edge):
                self.assertTrue(can_transition(edge[0], edge[1]))

    def test_c2_every_non_edge_is_false_without_nodes(self):
        """② 图论：不传 nodes 时，非边全假（逐对枚举，不是「抽查几条」）。"""
        probed = 0
        for source in OrderStatus:
            for target in OrderStatus:
                if (source, target) in _EDGES:
                    continue
                probed += 1
                with self.subTest(source=source, target=target):
                    self.assertFalse(can_transition(source, target))
        self.assertEqual(probed, len(OrderStatus) ** 2 - len(_EDGES))

    def test_c4_every_endpoint_gate_is_a_subset_of_every_edge_it_can_write(self):
        """④ 结构：9 个端点的节点门 ⊆ 它能写的每条边的节点集。"""
        checked = 0
        for edge, endpoints in _EDGE_ENDPOINTS.items():
            for endpoint in endpoints:
                checked += 1
                with self.subTest(edge=edge, endpoint=endpoint):
                    gate = _gate_for(endpoint, edge)
                    self.assertTrue(
                        gate <= _EDGES[edge],
                        f"{endpoint} 的门 {sorted(gate)} 不是 {edge} 的节点集 "
                        f"{sorted(_EDGES[edge])} 的子集——端点上会放行、状态机再拒一次",
                    )
        self.assertGreater(checked, 0)


class LiveEndpointGateAnchoringTests(SimpleTestCase):
    """④ 的字面量表必须与**线上表**一致——否则「端点门 ⊆ 边节点集」锚在空气上。

    ⚠️ **修复轮 1（复审）**：`_ENDPOINT_GATES` / `_EDGE_ENDPOINTS` 是手写字面量，
    它们与 `OrderViewSet.ORDER_PERMISSIONS` / `OrderViewSet.TRANSITION_TARGET_PERMISSIONS`
    / `PerformanceViewSet.PERFORMANCE_PERMISSIONS` **今天是逐条相同**的（复审当场核过），
    但原先没有任何用例把两者绑在一起 ⇒ 生产表漂移时 ④（以及 ③ 的 `_reachable`）
    会**照样绿**。本条读**线上表**逐条对拍，把这层锚定补上：生产表一改，本条立刻红。
    """

    def test_the_literal_endpoint_gates_agree_with_the_live_permission_tables(self):
        # 延迟 import：保持上面那些纯逻辑用例的 import 面不被视图层拖大。
        from apps.business.api.order_views import OrderViewSet
        from apps.business.api.performance_views import PerformanceViewSet

        live = {
            "E1": set(OrderViewSet.ORDER_PERMISSIONS["transition"]),
            "E2": set(OrderViewSet.ORDER_PERMISSIONS["throw_to_hall"]),
            "E3": set(OrderViewSet.ORDER_PERMISSIONS["withdraw"]),
            "E4": set(OrderViewSet.ORDER_PERMISSIONS["assign"]),
            "E5": set(OrderViewSet.ORDER_PERMISSIONS["claim"]),
            "E6": set(OrderViewSet.ORDER_PERMISSIONS["clear_taker"]),
            "E7": set(OrderViewSet.ORDER_PERMISSIONS["submit"]),
            "E8": set(PerformanceViewSet.PERFORMANCE_PERMISSIONS["review"]),
            #: ⭐ 新的受限通道 `/report-issue/`。它与 E7 **同门不同目标**
            #: ⇒ 必须单独锚在这里，否则 E9 的字面量会漂在线上表之外（本轮加它正是为了
            #: 不让「新端点的门」成为 T4/④ 的盲区）。
            "E9": set(OrderViewSet.ORDER_PERMISSIONS["report_issue"]),
        }
        for endpoint, codes in live.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    codes,
                    set(_ENDPOINT_GATES[endpoint]),
                    f"{endpoint} 的线上门与测试里的字面量不一致——两处必须同源",
                )
        with self.subTest(endpoint="E1", target="COMPLETED"):
            self.assertEqual(
                set(
                    OrderViewSet.TRANSITION_TARGET_PERMISSIONS[
                        str(OrderStatus.COMPLETED)
                    ]
                ),
                set(_E1_DONE),
                "E1 在「已完成」目标上的线上门与 `_E1_DONE` 不一致",
            )
        # ⭐ 计划 ④：新增的「待抢单」目标那一格也要与线上表逐条对拍——
        # 否则 ④（以及 ③ 的 `_reachable`）会锚在空气上。
        with self.subTest(endpoint="E1", target="WAITING_GRAB"):
            self.assertEqual(
                set(
                    OrderViewSet.TRANSITION_TARGET_PERMISSIONS[
                        str(OrderStatus.WAITING_GRAB)
                    ]
                ),
                set(_E1_WAITING_GRAB),
                "E1 在「待抢单」目标上的线上门与 `_E1_WAITING_GRAB` 不一致",
            )


class ApplyTransitionDenialTests(SimpleTestCase):
    """`apply_transition` 的两种拒绝在**唯一写入口**上都可分辨（A10）。"""

    class _StubOrder:
        def __init__(self, status):
            self.status = status
            self.pk = 1

        def save(self, **kwargs):  # pragma: no cover - 拒绝路径不该写库
            raise AssertionError("被拒的迁移不该写库")

    def test_node_shortage_raises_with_the_gate_suffix(self):
        order = self._StubOrder(OrderStatus.IN_PROGRESS)
        with self.assertRaises(IllegalTransition) as ctx:
            apply_transition(order, OrderStatus.PENDING_REVIEW, nodes={_UNRELATED_NODE})
        self.assertTrue(str(ctx.exception).endswith("（缺少所需权限节点）"))
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_missing_edge_raises_without_the_gate_suffix(self):
        # ⚠️ 靶子是**自环**（D-0147 / R8）：`COMPLETED → IN_PROGRESS`
        #    现在是合法边，不再是「边不存在」的样本。
        order = self._StubOrder(OrderStatus.COMPLETED)
        with self.assertRaises(IllegalTransition) as ctx:
            apply_transition(order, OrderStatus.COMPLETED, nodes={_UNRELATED_NODE})
        self.assertNotIn("缺少所需权限节点", str(ctx.exception))
        self.assertEqual(order.status, OrderStatus.COMPLETED)


class ContainmentTests(TenantTestCase):
    """③ 新旧判据的包含关系（**可观察形态**）。"""

    def setUp(self):
        seed_catalog()
        seed_builtin_roles()

    def test_c3_no_builtin_title_loses_an_observable_transition(self):
        """每个内置职位 × 每条边：旧判据放行 ∧ 今天真的走得到 ⇒ 新判据必须放行。

        ⚠️ 「真的走得到」这一层不可省：`ISSUE/PAUSED → IN_PROGRESS` 的旧角色集含 `TAKER`，
        代练员在**状态机**上确实被放行；但它唯一的写入口是通用 `/transition/`，节点门是 16，
        而代练员没有 `order` 栏目 ⇒ 今天在端点上就已经 403（不是本次收窄）。

        ⚠️ **本批 F3，spec §2.3）**：`_REMOVED_BY_COMPLETE_GATE` 那五条边
        是**显式豁免**（业务方裁定「必须从待验收改已完成」）——它们在豁免计数里留痕，
        不是从 `_OLD_ROLES` 里消失。哨兵要求 `compared` 与 `exempted` **都**大于 0：
        前者防「什么都没比」的假绿，后者防「豁免集被写坏成空/不匹配」的假绿。
        """
        compared = 0
        exempted = 0
        for title in ("客服", "客服主管", "代练员", "系统开发员", "系统管理员"):
            held = _held_nodes(title)
            for edge, old_set in _OLD_ROLES.items():
                if edge in _REMOVED_BY_COMPLETE_GATE:
                    exempted += 1
                    continue
                with self.subTest(title=title, edge=edge):
                    old_allowed = bool(old_set & _titles_old_roles(title))
                    if not (old_allowed and _reachable(title, edge)):
                        continue          # 旧判据不放行、或今天就走不到 ⇒ 不在比较域内
                    compared += 1
                    self.assertTrue(
                        _EDGES[edge] & held,
                        f"{title} 在 {edge} 上从「可观察地放行」变成拒绝——这是本线不允许的收窄",
                    )
        # 哨兵：比较域为空时上面一条都不会执行，本条防止「什么都没比」的假绿。
        self.assertGreater(compared, 0, "比较域是空的——包含关系实际上一条都没比")
        # 豁免也要留痕（并且恰好遍历到每一个内置职位 × 每一条被删的旧边）
        self.assertEqual(exempted, len(_REMOVED_BY_COMPLETE_GATE) * 5)
