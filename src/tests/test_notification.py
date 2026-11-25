"""B2-1：站内通知（spec §3.5 / §3.6 / §5 / §7.4 / §8.2）。

口径（来源：brief §3 的 R1/R2，已由业务方裁定）：

- 三类通知：**被指派 / 被抢走 / 待审核**；**通知不保证送达**，是提示而非凭据。
- 收件人：
  - `被指派` = 该单的**接单人**（`Performance` 的 MAIN 行 `user`）；
  - `被抢走` = **抛单人**（`order.created_by` 按 `username` 解析，解析不到就**不写行**）；
  - `待审核` = 该租户内**全体客服主管**（`客服主管` Group 且 `is_active=True`），一个都没有就不写行。
- 副作用出口：`被抢走` / `待审核` 挂 `apply_transition` 的 target hook；
  `被指派` 挂 `Performance` 的 `post_save`（因为 `/assign` 的 `set_taker` 在
  `apply_transition` **之后**，钩子跑时 `Performance.user` 还是空的）。

断言纪律（AGENTS.md §5）：
- 期望值一律写**字面值**，不引用被测模块导出的常量（E-0024 自指断言）；
- 副作用断言打**数据库行**，不打 `logger`（E-0009）；
- 否定断言**不用 `count()` 单判**，断言具体行/字段。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Notification,
    Order,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
#: （只打 `/api/business/notifications/` 的两个类不需要它，故不挂。）
NOTIFICATION_ORDER_NODES = (
    "order.add_edit",             # 录单
    "order.list",                 # 列表
    "order.view_detail",          # 详情
    "order.change_status",        # `/transition/`
    "order.assign_taker",         # `/assign/`
    "order.submit_payout_price",
    "grab_hall.take_order",       # `/claim/`
    "order.submit_work",          # `/submit/`
)


def _game(name="原神"):
    return Game.objects.create(name=name)


def _order(game, order_no, *, status, created_by="", hall_price=Decimal("88.00")):
    return Order.objects.create(
        game=game,
        order_no=order_no,
        status=status,
        created_by=created_by,
        hall_price=hall_price,
    )


def _perf(order, user=None, price=Decimal("88.00")):
    return Performance.objects.create(
        order=order, emp_type=EmpType.MAIN, user=user, temp_amount=price
    )


# ── 触发点 1：`被指派`（Performance 信号）────────────────────────────
class AssignedNotificationTests(TenantTestCase):
    def test_assign_creates_one_assigned_row_for_the_taker(self):
        """`/assign` 之后库里**有**一条 `被指派`、收件人是接单人（断行 + 断字段值）。"""
        game = _game()
        order = _order(game, "20260920000000000101", status=OrderStatus.UNASSIGNED)
        _perf(order)
        cs = make_logged_in_client(username="cs-assign", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——发请求的客服要显式持有该游戏，
        # 否则读侧空结果 / 写侧业务码 1000。本用例期望值一个字没改，不是放宽判据。
        grant_games(User.objects.get(username="cs-assign"), game)
        taker = User.objects.create_user(username="taker-assign")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（见 assign_game_gate 专钉）。
        grant_games(taker, game)

        body = cs.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        rows = Notification.objects.filter(kind="被指派")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.user_id, taker.id)
        self.assertEqual(row.order_id, order.id)
        self.assertEqual(row.payload["order_no"], order.order_no)
        self.assertIsNone(row.read_at)

    def test_assign_does_not_send_a_grabbed_row(self):
        """判别力（`from_status == WAITING_GRAB` 守卫）：`UNASSIGNED → IN_PROGRESS` **不是**抢单。"""
        game = _game()
        order = _order(game, "20260920000000000102", status=OrderStatus.UNASSIGNED)
        _perf(order)
        cs = make_logged_in_client(username="cs-assign-2", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——见上一条同型说明。
        grant_games(User.objects.get(username="cs-assign-2"), game)
        taker = User.objects.create_user(username="taker-assign-2")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（见 assign_game_gate 专钉）。
        grant_games(taker, game)

        body = cs.post(
            f"/api/business/orders/{order.order_no}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(
            Notification.objects.filter(kind="被抢走").exists(),
            "指派（UNASSIGNED → IN_PROGRESS）不得被当成抢单",
        )


# ── spec §7.4「通知与状态/绩效**同事务**」的端到端判据（FIX 7）────────
class NotificationAtomicityTests(TenantTestCase):
    """通知必须**随事务回滚**——两个方向都要断（spec §7.4 的「同事务」判据）。

    ⚠️ **桩打在「被查找的那个模块」上**（AGENTS §5 / E-0029）：A4把 `/assign/`
    的动作体搬进了 `apps.business.order_writes`（唯一判据出处，spec §D5）⇒ 现在打的是
    `apps.business.order_writes.record`。`/assign` 在 `transaction.atomic()` 内**最后**才调它，
    此时 `set_taker` 已经写过 `被指派` 通知（`Performance.post_save` → `notify_assigned`）
    ⇒ 让它抛一个**未预期**异常，正好检验「通知与状态是不是真的同事务」。
    （期望值一个字未改；搬迁前那一版打在 `apps.business.api.order_views.record` 上。）
    """

    def test_assign_notification_and_status_roll_back_together(self):
        game = _game()
        order = _order(game, "20260920000000000701", status=OrderStatus.UNASSIGNED)
        _perf(order)
        cs = make_logged_in_client(username="cs-atomic", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——发请求的客服要显式持有该游戏。
        grant_games(User.objects.get(username="cs-atomic"), game)
        taker = User.objects.create_user(username="taker-atomic")
        # ⭐ D-0252：指派闸判「目标打手游戏范围」⇒ 目标也要授权（见 assign_game_gate 专钉）。
        grant_games(taker, game)

        with mock.patch(
            "apps.business.order_writes.record",
            side_effect=RuntimeError("桩：模拟事务块内的未预期异常"),
        ):
            with self.assertRaises(RuntimeError):
                cs.post(
                    f"/api/business/orders/{order.order_no}/assign/",
                    {"user_id": taker.id},
                    content_type="application/json",
                )

        # 方向一：通知**没有**留下（`set_taker` 那笔同事务写入被回滚）
        self.assertFalse(Notification.objects.filter(kind="被指派").exists())
        # 方向二：订单状态回到 `UNASSIGNED`（`apply_transition` 那笔写入被回滚）
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # 顺带钉住第三笔同事务写入也回滚了：绩效归属人回到 NULL
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )


# ── 触发点 2：`被抢走`（IN_PROGRESS 的 target hook）──────────────────
class GrabbedNotificationTests(TenantTestCase):
    def _order_with_thrower(self, order_no, thrower_username):
        game = _game()
        order = _order(
            game, order_no, status=OrderStatus.WAITING_GRAB, created_by=thrower_username
        )
        _perf(order)
        return order

    def test_claim_notifies_the_thrower(self):
        """代练员 `/claim` 之后，抛单人**收到**一条 `被抢走`（断行 + 断字段值）。"""
        make_logged_in_client(username="cs-thrower", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        thrower = User.objects.get(username="cs-thrower")
        order = self._order_with_thrower("20260920000000000201", "cs-thrower")
        # 旁观者：没有抛这单，必须一条通知都收不到（判别力负向）
        bystander = User.objects.create_user(username="bystander")

        taker_client = make_logged_in_client(username="taker-claim", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        taker = User.objects.get(username="taker-claim")
        # （按游戏权限收窄）：**夹具补齐**——抢单方要显式持有该单的游戏；
        # 抛单人只是收件人（不发请求、不被断言可见），故不补。
        grant_games(taker, order.game)
        body = taker_client.post(
            f"/api/business/hall/{order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        rows = Notification.objects.filter(kind="被抢走")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.user_id, thrower.id)
        self.assertEqual(row.order_id, order.id)
        self.assertEqual(row.payload["order_no"], order.order_no)
        self.assertEqual(row.payload["from_status"], "WAITING_GRAB")
        # 负向：旁观者没有任何通知；抢单者自己也不是收件人
        self.assertFalse(Notification.objects.filter(user=bystander).exists())
        self.assertFalse(Notification.objects.filter(user=taker).exists())

    def test_claim_with_empty_created_by_writes_no_row(self):
        """`created_by` 是空串 ⇒ 解析不到抛单人 ⇒ **不写行**（通知不保证送达）。"""
        order = self._order_with_thrower("20260920000000000202", "")
        taker_client = make_logged_in_client(username="taker-empty", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——抢单方要显式持有该单的游戏。
        grant_games(User.objects.get(username="taker-empty"), order.game)
        body = taker_client.post(
            f"/api/business/hall/{order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(Notification.objects.filter(kind="被抢走").exists())

    def test_claim_with_unknown_username_writes_no_row(self):
        """`created_by` 指向一个**不存在的**用户名 ⇒ 不写行。"""
        order = self._order_with_thrower("20260920000000000203", "no-such-user")
        taker_client = make_logged_in_client(username="taker-ghost", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——抢单方要显式持有该单的游戏。
        grant_games(User.objects.get(username="taker-ghost"), order.game)
        body = taker_client.post(
            f"/api/business/hall/{order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(Notification.objects.filter(kind="被抢走").exists())

    def test_claim_with_inactive_thrower_writes_no_row(self):
        """抛单人**已停用** ⇒ 不写行（brief §3 R1 的「已停用」口径）。"""
        make_logged_in_client(username="cs-off", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        thrower = User.objects.get(username="cs-off")
        thrower.is_active = False
        thrower.save(update_fields=["is_active"])
        order = self._order_with_thrower("20260920000000000204", "cs-off")
        taker_client = make_logged_in_client(username="taker-off", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——抢单方要显式持有该单的游戏；
        # 已停用的抛单人只是被断言「收不到」，不补。
        grant_games(User.objects.get(username="taker-off"), order.game)
        body = taker_client.post(
            f"/api/business/hall/{order.order_no}/claim/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(Notification.objects.filter(kind="被抢走").exists())

    def test_generic_transition_from_hall_does_not_fake_a_grab(self):
        """通用 `/transition/` 的 `WAITING_GRAB → IN_PROGRESS` 没有接单人 ⇒ **不发** `被抢走`。

        机制（B2 整体评审 FIX 2）：`order_views.transition` 只挡
        `target == COMPLETED`，**没挡大厅边**，所以 `TAKER` 能把它调通；但那条路径
        **没有人写 `Performance.user`**（写**非空** `user` 的调用点只有 `/assign` 的
        `set_taker` 与 `/claim` 的条件更新；`reset_taker_performance` 写的是 `None`，
        故不在其列——**措辞更正见** `notifications.py::_on_in_progress` 的 C1 更正块）
        ⇒ 订单进 `IN_PROGRESS` 却无接单人。
        判据「该单 MAIN 绩效此刻**真的有归属人**」把这条路径与 `/claim` 区分开。

        判别力（变异自证）：去掉 `_on_in_progress` 的归属人判据 ⇒ 本用例变红
        （会给 `created_by` 发一条**假的** `被抢走`）。原始输出见修复报告 FIX 2。
        """
        make_logged_in_client(username="cs-fake-thrower", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        thrower = User.objects.get(username="cs-fake-thrower")
        order = self._order_with_thrower("20260920000000000210", "cs-fake-thrower")
        taker_client = make_logged_in_client(username="taker-fake-grab", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——发请求的代练员要显式持有该单的游戏。
        grant_games(User.objects.get(username="taker-fake-grab"), order.game)

        body = taker_client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": "IN_PROGRESS"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        # 如实断出这条路径留下的**P3a 遗留状态**：进了 IN_PROGRESS，接单人仍是空的
        # （本组不修，登记在 `notifications.py` 的 `_on_in_progress` docstring 与计划 B2 收尾段）。
        self.assertIsNone(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id
        )
        # 两个方向都为负：`被抢走` 这一 kind 没有行，抛单人本人也没有任何通知
        self.assertFalse(Notification.objects.filter(kind="被抢走").exists())
        self.assertFalse(Notification.objects.filter(user=thrower).exists())


# ── 触发点 3：`待审核`（PENDING_REVIEW 的 target hook）───────────────
class PendingReviewNotificationTests(TenantTestCase):
    def _in_progress_order(self, order_no, taker):
        game = _game()
        order = _order(game, order_no, status=OrderStatus.IN_PROGRESS)
        _perf(order, user=taker)
        return order

    def test_submit_notifies_every_active_reviewer_once(self):
        """`/submit` 后每个 `is_active` 的客服主管**各一条**；代练员自己**没有**。"""
        taker_client = make_logged_in_client(username="taker-submit", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        taker = User.objects.get(username="taker-submit")
        make_logged_in_client(username="reviewer-1", groups=("客服主管",), nodes=NOTIFICATION_ORDER_NODES)
        reviewer_1 = User.objects.get(username="reviewer-1")
        make_logged_in_client(username="reviewer-2", groups=("客服主管",), nodes=NOTIFICATION_ORDER_NODES)
        reviewer_2 = User.objects.get(username="reviewer-2")
        # 停用的客服主管不出现在收件人里
        off = User.objects.create_user(username="reviewer-off")
        off.groups.add(Group.objects.get(name="客服主管"))
        off.is_active = False
        off.save(update_fields=["is_active"])

        order = self._in_progress_order("20260920000000000301", taker)
        # （按游戏权限收窄）：**夹具补齐**——发请求的代练员要显式持有该单的游戏；
        # 客服主管只是收件人（不发请求），故不补。
        grant_games(taker, order.game)
        body = taker_client.post(
            f"/api/business/orders/{order.order_no}/submit/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)

        rows = Notification.objects.filter(kind="待审核")
        # ⚠️ **行数断言是必须的**：只断 `user_id` 的**集合**
        #    时，「同一个客服主管被写了两行」也会绿——集合去重把它吞掉了，与用例名
        #    「每个客服主管**各一条**」不符。这一条让重复行真的会红。
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list("user_id", flat=True)), {reviewer_1.id, reviewer_2.id}
        )
        for row in rows:
            self.assertEqual(row.order_id, order.id)
            self.assertEqual(row.payload["order_no"], order.order_no)
        # 负向：代练员自己不是客服主管，不应收到 `待审核`
        self.assertFalse(Notification.objects.filter(kind="待审核", user=taker).exists())

    def test_submit_without_reviewers_writes_no_row(self):
        """一个客服主管都没有 ⇒ **不写行**（不是写一条收件人为空的）。"""
        taker_client = make_logged_in_client(username="taker-noreview", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        taker = User.objects.get(username="taker-noreview")
        order = self._in_progress_order("20260920000000000302", taker)
        # （按游戏权限收窄）：**夹具补齐**——发请求的代练员要显式持有该单的游戏。
        grant_games(taker, order.game)
        body = taker_client.post(
            f"/api/business/orders/{order.order_no}/submit/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(Notification.objects.filter(kind="待审核").exists())


# ── 「指派」只在 `UNASSIGNED → IN_PROGRESS` 发生 ──────────────────────
class AssignmentIsNotRecoveryTests(TenantTestCase):
    def test_recovering_an_issue_order_sends_neither_kind(self):
        """`ISSUE → IN_PROGRESS`（恢复）**不发**任何一类通知。

        判别力：去掉 IN_PROGRESS 钩子的 `from_status == WAITING_GRAB` 守卫 ⇒ `被抢走` 冒出来。
        """
        make_logged_in_client(username="cs-owner", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        cs = User.objects.get(username="cs-owner")
        taker = User.objects.create_user(username="taker-recover")
        game = _game()
        order = _order(
            game, "20260920000000000401", status=OrderStatus.ISSUE, created_by="cs-owner"
        )
        _perf(order, user=taker)

        cs_client = make_logged_in_client(username="cs-transition", groups=("客服",), nodes=NOTIFICATION_ORDER_NODES)
        # （按游戏权限收窄）：**夹具补齐**——发请求的客服要显式持有该游戏；
        # `cs-owner`（抛单人）与 `taker-recover` 都不发请求，不补。
        grant_games(User.objects.get(username="cs-transition"), game)
        body = cs_client.post(
            f"/api/business/orders/{order.order_no}/transition/",
            {"target": "IN_PROGRESS"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertFalse(Notification.objects.filter(kind="被抢走").exists())
        self.assertFalse(Notification.objects.filter(kind="被指派").exists())

    def test_submit_does_not_resend_assigned(self):
        """`/submit` 会 `perf.save()`，但接单人**不是新指派** ⇒ 不得再发一条 `被指派`。

        判别力：去掉 post_save 的 `_previous_user_id is None` 守卫 ⇒ 本用例变红。
        """
        taker_client = make_logged_in_client(username="taker-resend", groups=("代练员",), nodes=NOTIFICATION_ORDER_NODES)
        taker = User.objects.get(username="taker-resend")
        game = _game()
        order = _order(game, "20260920000000000402", status=OrderStatus.IN_PROGRESS)
        _perf(order, user=taker)
        # （按游戏权限收窄）：**夹具补齐**——发请求的代练员要显式持有该单的游戏。
        grant_games(taker, game)

        body = taker_client.post(
            f"/api/business/orders/{order.order_no}/submit/",
            {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(Notification.objects.filter(kind="被指派").exists())


# ── 三个接口 ────────────────────────────────────────────────────────
LIST_URL = "/api/business/notifications/"
UNREAD_COUNT_URL = "/api/business/notifications/unread-count/"


class NotificationListTests(TenantTestCase):
    def setUp(self):
        self.a_client = make_logged_in_client(username="list-a", groups=("代练员",))
        self.a = User.objects.get(username="list-a")
        self.b_client = make_logged_in_client(username="list-b", groups=("代练员",))
        self.b = User.objects.get(username="list-b")
        game = _game()
        order = _order(game, "20260920000000000501", status=OrderStatus.UNASSIGNED)
        self.mine = Notification.objects.create(
            user=self.a, kind="被指派", order=order, payload={"order_no": order.order_no}
        )
        self.theirs = Notification.objects.create(
            user=self.b, kind="被指派", order=order, payload={"order_no": order.order_no}
        )

    def _ids(self, client):
        body = client.get(LIST_URL).json()
        self.assertEqual(body["code"], 0)
        return [row["id"] for row in body["data"]["items"]]

    def test_list_contains_only_my_rows(self):
        """明确断言 B 的那条**不在**里面（不要只断 `count()`）。"""
        body = self.a_client.get(LIST_URL).json()
        self.assertEqual(body["code"], 0)
        items = body["data"]["items"]
        ids = [row["id"] for row in items]
        self.assertIn(self.mine.id, ids)
        self.assertNotIn(self.theirs.id, ids)
        self.assertEqual([row["id"] for row in items], [self.mine.id])

    def test_list_serializes_the_documented_fields(self):
        row = self.a_client.get(LIST_URL).json()["data"]["items"][0]
        self.assertEqual(row["id"], self.mine.id)
        self.assertEqual(row["kind"], "被指派")
        self.assertEqual(row["order_no"], self.mine.order.order_no)
        # ⚠️ 本夹具的 payload 是**直接按旧形状**写的（没有 source 键）——「建⾏时带上
        #    `source_order_no`」由 `tests.test_source_first_display` 钉；这里钉的是读面
        #    **逐字回出存量 payload**（快照语义，⛔ 不改写）。
        self.assertEqual(row["payload"], {"order_no": self.mine.order.order_no})
        # ⭐ D-0301：读面行**实时读 `order` FK** 的来源单号——
        #    存量 payload 缺键也回得出（该夹具单的来源号为空 ⇒ 空串）。
        self.assertEqual(row["source_order_no"], self.mine.order.source_order_no)
        self.assertIsNone(row["read_at"])
        self.assertIn("created_at", row)

    def test_list_pagination_shape_is_the_p2_envelope(self):
        Notification.objects.create(user=self.a, kind="待审核", payload={"order_no": "X"})
        body = self.a_client.get(f"{LIST_URL}?page=1&page_size=1").json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(set(data.keys()), {"items", "total", "page", "page_size"})
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 1)

    def test_anonymous_gets_401(self):
        response = Client(SERVER_NAME="tenant.test.com").get(LIST_URL)
        self.assertEqual(response.status_code, 401)

    def test_malformed_page_param_is_not_500(self):
        response = self.a_client.get(f"{LIST_URL}?page=abc")
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200)
        self.assertIn("code", response.json())

    def test_list_uses_the_scoping_result(self):
        """接线判别力（照 `test_p3a_isolation.ScopingWiringTests`）：桩打在**返回值**上。"""
        self.assertIn(self.mine.id, self._ids(self.a_client))          # 对照：本来看得见
        with mock.patch(
            "apps.business.api.notification_views.scope_queryset",
            side_effect=lambda user, qs: qs.none(),
        ) as seam:
            self.assertEqual(self._ids(self.a_client), [])
        self.assertTrue(seam.called, "列表没有经过 scope_queryset 卡点")


class NotificationReadTests(TenantTestCase):
    def setUp(self):
        self.a_client = make_logged_in_client(username="read-a", groups=("代练员",))
        self.a = User.objects.get(username="read-a")
        self.b_client = make_logged_in_client(username="read-b", groups=("代练员",))
        self.b = User.objects.get(username="read-b")

    def _url(self, notification):
        return f"/api/business/notifications/{notification.id}/read/"

    def test_read_is_idempotent_and_keeps_the_first_timestamp(self):
        row = Notification.objects.create(user=self.a, kind="待审核", payload={"order_no": "N1"})
        first = self.a_client.post(self._url(row), {}, content_type="application/json").json()
        self.assertEqual(first["code"], 0)
        row.refresh_from_db()
        first_read_at = row.read_at
        self.assertIsNotNone(first_read_at)

        second = self.a_client.post(self._url(row), {}, content_type="application/json").json()
        self.assertEqual(second["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.read_at, first_read_at)

    def test_read_returns_the_row_with_read_at_set(self):
        row = Notification.objects.create(user=self.a, kind="被抢走", payload={"order_no": "N2"})
        body = self.a_client.post(self._url(row), {}, content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["id"], row.id)
        self.assertIsNotNone(body["data"]["read_at"])

    def test_reading_someone_elses_row_fails_and_leaves_it_unread(self):
        """别人的通知读不到。

        ⚠️ **brief 与 spec 的冲突（已在报告登记）**：brief §4.3 写「别人的通知 → 404」，
        但 spec §5 首句定的是「**业务结果一律 200 + 业务 code**」，且
        `apps/core/exceptions.py` 明确把 `Http404` 归一化成 `200 + BUSINESS(1002)`
        （它的一条设计说明就是「不这样做会出现两套客户端契约」）。
        按「**冲突以 spec 为准**」，这里取实测的 `200 + 1002`。
        """
        row = Notification.objects.create(user=self.b, kind="待审核", payload={"order_no": "N3"})
        response = self.a_client.post(self._url(row), {}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)
        row.refresh_from_db()
        self.assertIsNone(row.read_at)

    def test_anonymous_gets_401(self):
        row = Notification.objects.create(user=self.a, kind="待审核", payload={"order_no": "N4"})
        response = Client(SERVER_NAME="tenant.test.com").post(
            self._url(row), {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)


# ── 删除语义（spec §3.6 写死）───────────────────────────────────────
class NotificationDeleteSemanticsTests(TenantTestCase):
    def test_deleting_the_order_keeps_the_row_and_nulls_the_fk(self):
        game = _game()
        order = _order(game, "20260920000000000601", status=OrderStatus.UNASSIGNED)
        user = User.objects.create_user(username="del-order-user")
        row = Notification.objects.create(user=user, kind="被指派", order=order)

        order.delete()

        row.refresh_from_db()
        self.assertIsNone(row.order_id)
        self.assertEqual(row.user_id, user.id)

    def test_deleted_order_shows_order_no_none_in_the_list(self):
        """订单删除（`SET_NULL`）后走**列表接口**：那一行的 `order_no` 是 `None`。

        覆盖 `notification_views.py::NotificationSerializer.get_order_no` 里
        `obj.order_id` 为空的**分支**（FIX 9）——删除语义不止「FK 被置空」，
        还要看在真实出口（列表接口）上的形状。

        ⚠️ 造一张**没有绩效行**的订单：`Performance.order` 是 `PROTECT`，
        有绩效行的订单**删不掉**（会被 `ProtectedError` 挡住）。
        """
        client = make_logged_in_client(username="del-list-user", groups=("代练员",))
        user = User.objects.get(username="del-list-user")
        game = _game()
        order = _order(game, "20260920000000000603", status=OrderStatus.UNASSIGNED)
        row = Notification.objects.create(
            user=user, kind="被指派", order=order, payload={"order_no": order.order_no}
        )

        order.delete()          # 无绩效行 ⇒ 不会被 `PROTECT` 挡住

        body = client.get(LIST_URL).json()
        self.assertEqual(body["code"], 0)
        items = body["data"]["items"]
        self.assertEqual([r["id"] for r in items], [row.id])
        self.assertIsNone(items[0]["order_no"])

    def test_deleting_the_user_removes_the_row(self):
        game = _game()
        order = _order(game, "20260920000000000602", status=OrderStatus.UNASSIGNED)
        user = User.objects.create_user(username="del-user")
        row = Notification.objects.create(user=user, kind="被指派", order=order)

        user.delete()

        self.assertFalse(Notification.objects.filter(pk=row.pk).exists())


# ── 未读计数（打手 App 底部「消息」角标，业务方）──────────
class NotificationUnreadCountTests(TenantTestCase):
    """`GET /api/business/notifications/unread-count/` 的判别力用例。

    口径：`read_at IS NULL` 的**本人**通知；别人的通知**不算进来**、全读回 0。
    """

    def setUp(self):
        self.a_client = make_logged_in_client(username="count-a", groups=("代练员",))
        self.a = User.objects.get(username="count-a")
        self.b_client = make_logged_in_client(username="count-b", groups=("代练员",))
        self.b = User.objects.get(username="count-b")
        game = _game()
        order = _order(game, "20260920000000000701", status=OrderStatus.UNASSIGNED)
        self.unread_one = Notification.objects.create(
            user=self.a, kind="被指派", order=order, payload={"order_no": order.order_no}
        )
        self.unread_two = Notification.objects.create(
            user=self.a, kind="待审核", order=order, payload={"order_no": order.order_no}
        )
        self.read = Notification.objects.create(
            user=self.a, kind="被抢走", order=order, read_at=timezone.now()
        )
        self.theirs = Notification.objects.create(user=self.b, kind="被指派", order=order)

    def test_counts_only_my_unread_rows(self):
        """自己的 2 条未读算进来；自己的已读、B 的未读都**不算**。"""
        body = self.a_client.get(UNREAD_COUNT_URL).json()
        self.assertEqual(body["code"], 0)
        # 出参形状照统一信封，`data` 里只有这一个键（不是列表）。
        self.assertEqual(body["data"], {"notifications": 2})

    def test_all_read_returns_zero(self):
        Notification.objects.filter(user=self.a, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        body = self.a_client.get(UNREAD_COUNT_URL).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"], {"notifications": 0})

    def test_query_params_do_not_change_the_scope(self):
        """⛔ 不接受任何租户 / 用户参数：带上来也只按认证态算。"""
        body = self.a_client.get(
            f"{UNREAD_COUNT_URL}?user={self.b.id}&tenant=other&tenant_id={self.b.id}"
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"], {"notifications": 2})

    def test_notifications_of_a_soft_deleted_order_are_not_counted(self):
        """已软删单的通知在列表里看不到、也读不掉 ⇒ 不能算进角标（否则永远清不掉）。

        与列表共用同一份可见性口径，所以这里同时断列表也是空的。
        """
        Order.all_objects.filter(pk=self.unread_one.order_id).update(deleted_at=timezone.now())
        body = self.a_client.get(UNREAD_COUNT_URL).json()
        self.assertEqual(body["data"], {"notifications": 0})
        listed = self.a_client.get(LIST_URL).json()
        self.assertEqual(listed["data"]["items"], [])

    def test_a_notification_with_no_order_is_still_counted(self):
        """`order` 为 NULL 的行（订单被物理删掉后 `SET_NULL`）必须留住并计入。"""
        Notification.objects.create(user=self.a, kind="被抢走", payload={"order_no": "X"})
        body = self.a_client.get(UNREAD_COUNT_URL).json()
        self.assertEqual(body["data"], {"notifications": 3})

    def test_count_uses_the_scoping_result(self):
        """接线判别力：桩打在 `scope_queryset` 的**返回值**上，与列表用例同形。"""
        with mock.patch(
            "apps.business.api.notification_views.scope_queryset",
            side_effect=lambda user, qs: qs.none(),
        ) as seam:
            body = self.a_client.get(UNREAD_COUNT_URL).json()
        self.assertEqual(body["data"], {"notifications": 0})
        self.assertTrue(seam.called, "未读计数没有经过 scope_queryset 卡点")

    def test_anonymous_gets_401(self):
        response = Client(SERVER_NAME="tenant.test.com").get(UNREAD_COUNT_URL)
        self.assertEqual(response.status_code, 401)
