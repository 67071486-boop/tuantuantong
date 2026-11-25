"""上下游订单推送：指派触发 + 真投递 / 下家侧落单。spec §7.1 / §7.2 / §8.3 / §10.4。判据分**四层**，缺一层就漏一种错法：① 入队内核本身（`enqueue_order_event` 与它的三个壳 `enqueue_order_assigned` / `enqueue_order_grabbed` / `enqueue_order_withdrawn`）：这个 taker 没有生效绑定 ⇒ **一行都不写**；有 ⇒ 正好一行 `pending`（⛔ 请求路径上不投递，见 spec §16.6）；② 挂钩真的接在 `/assign/` 的**成功路径**上（打真 HTTP + 断**数据库行**）——⛔ 没有这一层，「挂钩挂错地方 / 根本没挂上」是**静默**的（单元测试全绿）；③ `deliver_one` 真投递：成功落 `done`、失败落 `pending` + 退避（第 5 次才 `failed` 终态）+ 连续失败计数；④ `receive_events` / 接收**端点**：来源字典、销售归属、去重、认证与信封。⚠️ ②里「指派一次出站都不发」与「`on_commit` 回调一个都不排」两条判据走**真 HTTP**（`AssignEndpointHookTests`）——§16.6 把投递搬出请求路径后，判据是「**根本没投**」，只有打请求才看得见（`on_commit` 是请求提交时排的）。
⚠️ 入队**不排任何 `on_commit` 回调**（§16.6 删掉了投递回调）⇒ 只有一条用例需要 `captureOnCommitCallbacks()`，它钉的就是「回调数为 **0**」。⚠️ 该回调在 `TestCase` 里**不会**自动跑 ⇒ 要它执行得用 `captureOnCommitCallbacks(execute=True)`；实现读的是 Django 5.2 `TestCase.captureOnCommitCallbacks`（该文件里 `yield callbacks` 在 `finally` 分支才把回调填进列表）：列表在 `with` 块退出时才填满 ⇒ 断言必须写在 `with` 块**之后**。⛔ 别用 `assertLogs` 断言「发出去了」（`AGENTS.md` §5-G / E-0009）。⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里 ⇒ 普通 `TestCase` 的 search_path 里没有 `business_*` 表（照 `tests/test_peer_models.py` 的先例）。
⚠️ 与任务书/计划的偏差（如实登记）：回归套件 `tests.test_order_assign_api` **在本仓不存在**，`/assign/` 的既有回归实为 `tests.test_order_assign_ext`（外加 `tests/test_dispatch_api`）；投递内核住 `apps/business/peer_worker.py`、名 `deliver_one`（Ruling 6：⛔ 不在 `peer_views` 留 shim）⇒ 调用与 `mock.patch` 目标都在那里（`mock.patch` 必须打在**被查找的模块**上，E-0029）；`payload=order_payload(order)` 照抄会崩——`Order.total_price` 是 `DecimalField`，进 `PeerSyncEvent.payload`（JSONField）时 psycopg 抛 `TypeError: Object of type Decimal is not JSON serializable` ⇒ `peer_sync.order_payload` 整类做了 JSON 安全化（见那里的 docstring）；`receive_events` 的 `Order.game` 是**非空**外键（见 `ReceiveEventTests.setUp`）；`test_two_peers_with_the_same_first_ten_chars_share_one_source` 的任务书原稿两绑定名截断后并不相同（就地更正，见该 docstring）。"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone as django_timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.api.peer_views import receive_events
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderSource,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_client import PeerUnreachable
from apps.business.peer_dispatch import (
    WITHDRAW_REASON_TEXT,
    enqueue_hall_event,
    enqueue_order_assigned,
    enqueue_order_grabbed,
    enqueue_order_withdrawn,
    enqueue_screenshot_event,
)
from apps.business.peer_sync import (
    HALL_THROW_KIND,
    hash_token,
    new_token,
    order_payload,
    source_name_for,
)
from apps.business.peer_worker import deliver_one
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BASE = "/api/business/orders"

#: `/assign/` 的类级节点门 + `UNASSIGNED → IN_PROGRESS` 那条边的节点集 + 读订单
#: （与 `tests/test_order_assign_ext.py` 的 `NODES` 同源，别自己凭记忆缩）。
NODES = (
    "order.assign_taker",
    "order.submit_payout_price",
    "order.change_status",
    "order.list",
)


class _PeerPushFixture(TenantTestCase):
    """两层的共用夹具。
    ⚠️ `PeerDownstreamBinding` 的 `account` / `peer_base_url` / `token_in_hash` / `token_out` **全部非空**（见该模型的字段定义）⇒ 少给一个就 `IntegrityError`，夹具必须按形状给全。"""

    def setUp(self):
        # `Game.name` 在租户内**唯一**（模型 `Meta.constraints` 的 `uniq_game_name`）——
        # 接收侧按名字对齐游戏正是靠这条（裁决 R21）；这个夹具也顺带复用同一款游戏。
        self.game = Game.objects.create(name="上下游指派夹具游戏")

    def _plain_user(self, username="peer-push-plain"):
        """一个**没有任何上下游绑定**的普通账号。"""
        return get_user_model().objects.create_user(username=username, password="pw-peer-push")

    def _taker_with_profile(self, username):
        """能被 `/assign/` 指派的账号（带提成档案；缺档案会回落全额，但这更贴近真实）。"""
        user = self._plain_user(username)
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 能被 /assign/ 指派的夹具账号要持本类的游戏（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(user, self.game)
        return user

    def _binding_with_account(self, username="peer-push-bound", *, state=None):
        """一条**我方为上家**的绑定 + 它那个账号。
        ⚠️ `token_in_hash` 走真 `hash_token`（对手方给我的令牌只存哈希）、`token_out` 是明文（我发给下家那一个）——按 `PeerDownstreamBinding` 的字段注释口径给，⛔ 不要两列都塞明文。"""
        account = self._plain_user(username)
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        # ⇒ 绑定账号被 /assign/ 指派时也要持本类的游戏（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(account, self.game)
        binding = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            **({"state": state} if state is not None else {}),
        )
        return binding

    def _order(self, no="20260925000000000901"):
        """造一张能过 `/assign/` 的单（照 `tests/test_order_assign_ext.py::_order` 的形状）。
        ⛔ **不要** `Order.objects.create()` 只填一半字段：`game` 之外还有一堆默认值/可空列，但 `main_performance_of(order)` 在 `/assign/` 里**必须**取到 MAIN 绩效行（缺行它抛 `Performance.DoesNotExist`），所以这条行是夹具的一部分。"""
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.UNASSIGNED,
            # ⛔ 业务方裁定：客户价（= 接单价）**取发单价**（`hall_price`）⇒
            #    外发载荷只带 `hall_price`、不再带 `total_price`。两列同值，
            #    断言钉 `hall_price` 这一个非默认值即可。
            total_price=Decimal("100.00"),
            hall_price=Decimal("100.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("20.00")
        )
        return order


class EnqueueOrderAssignedTests(_PeerPushFixture):
    """第 ① 层：`enqueue_order_assigned` 的取值与落库。"""

    def test_no_binding_means_no_event(self):
        """这个 taker 不是任何绑定的账号 ⇒ 一行事件都不该写（⛔ 不许建「空投递」）。
        ⚠️ 这里传 `order=None` 之所以**安全**：函数**先查绑定**、没有就立刻返回，`order` 与 `order_payload()` 那一步根本不会被执行。这条用例同时钉住那个**顺序**——把绑定查询挪到 `order_payload(order)` 之后（或挪到 `order.order_no` 之后），本用例会以 `AttributeError: 'NoneType' object has no attribute ...` 响亮地红。
        ⚠️ 库里**故意先放一条别人的生效绑定**：这样「漏掉 `account=taker` 这条过滤」（`filter(state=ACTIVE).first()`）也会被本条抓住——一个绑定都不建时，漏掉账号过滤的实现与本实现**取值相同**，抓不到（AGENTS §5-G）。"""
        self._binding_with_account(username="peer-push-someone-else")
        taker = self._plain_user()
        self.assertIsNone(enqueue_order_assigned(order=None, taker=taker))
        # 判据打在**数据库行**上，不是返回值（E-0024 / AGENTS §5-G）。
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_a_revoked_binding_means_no_event(self):
        """已解除的绑定不得再收推送 ⇒ `state=ACTIVE` 这条过滤是**承重**的。
        ⚠️ 本条的 taker **有一条绑定、只是不在生效态**——这正是「有没有绑定」与「绑定生不生效」两种实现的**取值不同点**（去掉 `state=` 过滤，本用例红）。⚠️ **传的是真订单、不是 `order=None`**：传 `None` 时去掉 `state=` 过滤的实现会在 `order.order_no` 上抛 `AttributeError`（内部错误），而不是让**行数断言**红——那样这条用例守的就不是「不许写行」这件事了。
        ⚠️ 行数断言写在返回值断言**之前**：否则去掉过滤时先红的是 `assertIsNone`（返回了一个事件），行数断言根本没有机会失败。"""
        binding = self._binding_with_account(
            username="peer-push-revoked", state=PeerDownstreamBinding.State.REVOKED
        )
        event = enqueue_order_assigned(order=self._order(), taker=binding.account)
        # 判据打在**数据库行**上（E-0024 / AGENTS §5-G）。
        self.assertEqual(PeerSyncEvent.objects.count(), 0)
        self.assertIsNone(event)

    def test_binding_means_one_pending_event(self):
        """有生效绑定 ⇒ 正好一行 `pending`，字段逐个可辨（⛔ 请求路径上不投递，spec §16.6）。"""
        binding = self._binding_with_account()
        order = self._order()

        # ⚠️ 本用例只钉**落库形状**。「投递不在请求路径上」那两条判据（一个出站都不发、
        #    一个 `on_commit` 回调都不排）在 `AssignEndpointHookTests` 里，各有一条用例。
        event = enqueue_order_assigned(order=order, taker=binding.account)
        self.assertIsNotNone(event)

        # ⚠️ 重新取库里的那一行：断言打**真实出口**，不看内存里那个没落库的对象。
        row = PeerSyncEvent.objects.get()
        self.assertEqual(row.pk, event.pk)
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(row.kind, "order.assign")
        self.assertEqual(row.binding_role, "downstream")
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.target_type, "order")
        self.assertEqual(row.target_ref, order.order_no)
        # 载荷走白名单函数（⛔ 不是随便一个 dict）
        self.assertEqual(row.payload, order_payload(order))
        # ⚠️ 再钉**具体值**：上面那条是「同一个函数两边相等」，只在载荷被 JSONField
        #    改写时才会红（自指，E-0024）；下面两条钉的是**内容与 JSON 表示**本身。
        self.assertEqual(row.payload["order_no"], order.order_no)
        # `hall_price` 是 `DecimalField` ⇒ 入库前必须已成 JSON 安全的字符串，
        # ⛔ 原样塞 Decimal 会 `TypeError: Object of type Decimal is not JSON serializable`。
        # ⛔ 业务方裁定：客户价取**发单价** ⇒ 载荷带 `hall_price`、不带 `total_price`。
        self.assertEqual(row.payload["hall_price"], "100.00")
        self.assertNotIn("total_price", row.payload)
        self.assertEqual(row.payload["status"], OrderStatus.UNASSIGNED)
        # 幂等键由发送方生成（spec §6.4）⇒ 非空、且不是字面常量
        self.assertTrue(row.idempotency_key)
        self.assertNotEqual(row.idempotency_key, "order.assign")

    def test_two_assignments_get_distinct_idempotency_keys(self):
        """幂等键是**每次生成**的（同一个键发两次会被对端当重复丢掉）。"""
        binding = self._binding_with_account()
        first = enqueue_order_assigned(order=self._order(), taker=binding.account)
        second = enqueue_order_assigned(
            order=self._order(no="20260925000000000902"), taker=binding.account
        )
        self.assertNotEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(PeerSyncEvent.objects.count(), 2)


class EnqueueOrderEventTests(_PeerPushFixture):
    """第 ①′ 层：三类事件共用的入队内核（spec §7.1 / §16.2 / §16.3）。
    ⚠️ 三类事件（`order.assign` / `order.grab` / `order.withdraw`）走的是**同一个** `enqueue_order_event` ⇒ 绑定过滤 / 去重 / 升级 / `meta` 这些判据只在这里各钉一遍。"""

    def test_a_withdraw_for_an_unbound_taker_writes_nothing(self):
        """没有生效绑定 ⇒ 一行都不写（§16.2；三类事件共用这一条检查，⛔ 不写"空投递"）。
        ⚠️ 撤单的 `previous_taker` **绝大多数**都不是绑定账号（普通打手）⇒ 这是撤单的**主路径**。
        判别力：把绑定查询挪到 `order.order_no` / `order_payload(order)` 之后（或之前先碰 `order`），本用例传 `order=None` 会以 `AttributeError` 红，而不是让行数断言红（与 `EnqueueOrderAssignedTests::test_no_binding_means_no_event` 同款）。"""
        self._binding_with_account(username="peer-push-someone-else-3")
        taker = self._plain_user("peer-push-unbound-withdraw")
        self.assertIsNone(enqueue_order_withdrawn(order=None, previous_taker=taker,
                                                  reason="taker_removed"))
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_withdraw_event_carries_reason_and_occurred_at(self):
        """撤单原因走事件级 `meta`，订单载荷仍只有白名单字段（spec §16.2）。"""
        binding = self._binding_with_account(username="peer-push-withdraw")
        # ⭐ 昵称与登录名**取值不同**：这样「填的是显示名还是登录名」才分辨得出来（E-0024）。
        binding.account.first_name = "测试昵称甲"
        binding.account.save(update_fields=["first_name"])
        order = self._order(no="20260925000000000920")

        event = enqueue_order_withdrawn(order, binding.account, "taker_removed")

        self.assertIsNotNone(event)
        # ⚠️ 判据打在**库里的行**上（E-0009 / E-0024），不是内存里那个刚 create 出来的对象。
        row = PeerSyncEvent.objects.get(pk=event.pk)
        self.assertEqual(row.kind, "order.withdraw")
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(row.binding_role, "downstream")
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.target_ref, order.order_no)
        self.assertEqual(row.meta["withdraw_reason"], "taker_removed")
        # ⭐ `{昵称}` 的来源（下家算不出显示名 ⇒ 入队时由 `binding.account` 自己填）。
        self.assertEqual(row.meta["actor_name"], "测试昵称甲")
        self.assertNotEqual(row.meta["actor_name"], binding.account.get_username())
        self.assertEqual(row.meta["occurred_at"], order.updated_at.isoformat())
        # 事件级元数据 ⛔ 不进 `payload`（payload 只放 §10.1 白名单里的订单字段）。
        self.assertEqual(row.payload, order_payload(order))
        for key in ("withdraw_reason", "actor_name", "occurred_at"):
            self.assertNotIn(key, row.payload)

    def test_a_second_withdraw_upgrades_a_pending_taker_removed(self):
        """同一次请求里"先清接手人、再迁退单"只留一行，且原因升级成 `refunded`（§16.2）。"""
        binding = self._binding_with_account(username="peer-push-withdraw-upgrade")
        order = self._order(no="20260925000000000921")

        first = enqueue_order_withdrawn(order, binding.account, "taker_removed")
        upgraded = enqueue_order_withdrawn(order, binding.account, "refunded")

        self.assertEqual(first.pk, upgraded.pk, "升级应当改写那一行，⛔ 不是再写一行")
        rows = PeerSyncEvent.objects.filter(
            kind="order.withdraw", target_ref=order.order_no
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().meta["withdraw_reason"], "refunded")

    def test_deleted_also_upgrades_a_pending_taker_removed(self):
        """`deleted`（删单）与 `refunded` 一样能升级（§16.2 的枚举是两个，不是一个）。"""
        binding = self._binding_with_account(username="peer-push-withdraw-deleted")
        order = self._order(no="20260925000000000922")
        enqueue_order_withdrawn(order, binding.account, "taker_removed")
        enqueue_order_withdrawn(order, binding.account, "deleted")
        row = PeerSyncEvent.objects.get()
        self.assertEqual(row.meta["withdraw_reason"], "deleted")

    def test_a_taker_removed_does_not_downgrade_a_pending_refunded(self):
        """**反向不升级**（§16.2）：先到的 `refunded` 不得被后来的 `taker_removed` 覆盖。"""
        binding = self._binding_with_account(username="peer-push-withdraw-downgrade")
        order = self._order(no="20260925000000000923")
        enqueue_order_withdrawn(order, binding.account, "refunded")
        enqueue_order_withdrawn(order, binding.account, "taker_removed")
        rows = PeerSyncEvent.objects.filter(kind="order.withdraw")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().meta["withdraw_reason"], "refunded")

    def test_a_pending_refunded_is_not_rewritten_by_a_later_deleted(self):
        """可被升级的**只有** `taker_removed`：先到的 `refunded` 不被后来的 `deleted` 改写。
        spec §16.2 只说 `refunded` / `deleted` 能升级一条尚未投递的 `taker_removed`（⛔ 没说这两者之间谁覆盖谁）⇒ 实现取「**先到的那个原因留下**」，⛔ 不静默改写对客日志文案。
        判别力：把 `enqueue_order_event` 升级条件里的 `(pending.meta or {}).get("withdraw_reason") == "taker_removed"` 去掉，本用例会读到 `deleted` ⇒ 红。"""
        binding = self._binding_with_account(username="peer-push-withdraw-two-strong")
        order = self._order(no="20260925000000000926")
        enqueue_order_withdrawn(order, binding.account, "refunded")
        enqueue_order_withdrawn(order, binding.account, "deleted")
        rows = PeerSyncEvent.objects.filter(kind="order.withdraw")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().meta["withdraw_reason"], "refunded")

    def test_a_withdraw_is_a_separate_row_from_a_pending_assign(self):
        """去重键含 `kind`：撤单不得去顶替同一张单那条尚未投递的 `order.assign`（§16.2）。
        判别力：把 `enqueue_order_event` 里去重查询的 `kind=kind` 删掉，本用例的 `count() == 2` 会变成 1（撤单"复用"了指派那一行、而它永远是 `order.assign`）⇒ 红。"""
        binding = self._binding_with_account(username="peer-push-withdraw-vs-assign")
        order = self._order(no="20260925000000000924")
        enqueue_order_assigned(order, binding.account)
        withdrawn = enqueue_order_withdrawn(order, binding.account, "taker_removed")
        self.assertEqual(PeerSyncEvent.objects.count(), 2)
        self.assertEqual(withdrawn.kind, "order.withdraw")

    def test_a_failed_withdraw_does_not_block_a_new_event(self):
        """⭐ 去重只挡 `status=pending`，终态 `failed` **不挡**新事件。
        spec §16.2：那一笔已经**放弃投递**了（退避四档走完），下一次业务动作值得重新试一次 ⇒ 必须**新增一行** `pending`，⛔ 不许复用 / 复活那条 `failed` 行。
        判别力：把去重查询的 `status=PENDING` 过滤删掉 ⇒ 本用例的 `count() == 2` 变成 1 ⇒ 红。"""
        binding = self._binding_with_account(username="peer-push-failed-redo")
        order = self._order(no="20260925000000000927")
        terminal = enqueue_order_withdrawn(order, binding.account, "taker_removed")
        terminal.status = PeerSyncEvent.Status.FAILED
        terminal.attempts = 5
        terminal.save(update_fields=["status", "attempts"])

        again = enqueue_order_withdrawn(order, binding.account, "taker_removed")

        self.assertNotEqual(again.pk, terminal.pk)
        rows = PeerSyncEvent.objects.filter(
            kind="order.withdraw", target_ref=order.order_no
        ).order_by("pk")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.last().status, PeerSyncEvent.Status.PENDING)
        terminal.refresh_from_db()
        self.assertEqual(terminal.status, PeerSyncEvent.Status.FAILED)

    def test_a_grabbed_event_has_the_grab_kind(self):
        """抢单是**第三类**事件（spec §7.1 第 2 行）：`kind` 必须是 `order.grab`。"""
        binding = self._binding_with_account(username="peer-push-grab")
        order = self._order(no="20260925000000000925")
        event = enqueue_order_grabbed(order, binding.account)
        self.assertIsNotNone(event)
        row = PeerSyncEvent.objects.get(pk=event.pk)
        self.assertEqual(row.kind, "order.grab")
        self.assertEqual(row.target_ref, order.order_no)
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)
        # 事件级时刻对三类事件都落（spec §16.3），⛔ 不进 payload。
        self.assertEqual(row.meta["occurred_at"], order.updated_at.isoformat())
        self.assertNotIn("occurred_at", row.payload)

    def test_withdraw_reason_text_is_the_spec_wording(self):
        """撤单原因 → 下家订单日志的**逐字中文**（spec §7.3 / §16.2）。
        ⚠️ 这条钉的是**对客文案**：改一个字就是改给客户看的日志。取值集合一并钉住（多一个键 = 上家能发一个下家翻不出中文的原因）。⛔ 协议传枚举、中文只在本仓内部用。
        ⚠️ 从 `peer_dispatch` 取（而⛔ 不是直接从 `peer_sync`）：那条 re-export 是给调用方/下家侧用的契约面，本用例顺带钉住它。"""
        self.assertEqual(
            WITHDRAW_REASON_TEXT,
            {
                "refunded": "发单方在发单系统里已将该笔订单改成已退单",
                "taker_removed": "发单方将订单接手人移除",
                "deleted": "发单方在发单系统里已将该笔订单删除",
            },
        )


class AssignEndpointHookTests(_PeerPushFixture):
    """第 ② 层：挂钩真的接在 `/assign/` 上（打真 HTTP，断数据库行）。"""

    def _assign(self, order, user_id, username="peer-push-cs"):
        client = make_logged_in_client(
            username=username, groups=("客服",), nodes=NODES
        )
        response = client.post(
            f"{BASE}/{order.order_no}/assign/",
            {"user_id": user_id},
            content_type="application/json",
        )
        return response.json()

    def test_assigning_to_a_bound_account_writes_one_pending_event(self):
        """指派给「下家绑定账号」⇒ 库里多一行 `order.assign` 待投递事件。
        判别力：把 `order_views.assign` 里那一行挂钩删掉 ⇒ 本用例 `PeerSyncEvent.objects.get()` 抛 `DoesNotExist` 红。"""
        binding = self._binding_with_account(username="peer-push-bound-taker")
        order = self._order(no="20260925000000000910")

        body = self._assign(order, binding.account_id)

        self.assertEqual(body["code"], 0, body)
        row = PeerSyncEvent.objects.get()
        self.assertEqual(row.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(row.kind, "order.assign")
        self.assertEqual(row.binding_role, "downstream")
        self.assertEqual(row.binding_id, str(binding.pk))
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.target_ref, order.order_no)
        # ⚠️ 挂钩在 `apply_transition` **之后**跑 ⇒ 载荷读到的状态是**指派后**的
        #    `IN_PROGRESS`（下家要看到的就是这个），⛔ 不是我这只手里那份旧的 `UNASSIGNED`。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(row.payload, order_payload(order))
        # 钉具体值（⛔ 不是与同一个函数对拍的自指断言，E-0024）
        self.assertEqual(row.payload["order_no"], order.order_no)
        self.assertEqual(row.payload["status"], OrderStatus.IN_PROGRESS)
        self.assertEqual(row.payload["hall_price"], "100.00")
        self.assertNotIn("total_price", row.payload)

    def test_assigning_to_an_unbound_account_writes_no_event(self):
        """阴性对照：指派给**没有绑定**的账号 ⇒ 一行事件都不写。
        ⛔ 没有这一条，「挂钩写成了无条件写事件」也会全绿（两种实现在第一条用例上取值相同）——这正是「断言必须能失败」的那类钉子。
        ⚠️ 库里**故意先放一条别人的生效绑定**：这样「漏掉 `account=taker` 这条过滤」（`filter(state=ACTIVE).first()`）也会被本条抓住；一个绑定都不建时，漏掉账号过滤的实现与本实现**取值相同**，抓不到（AGENTS §5-G）。"""
        self._binding_with_account(username="peer-push-someone-else-2")
        taker = self._taker_with_profile("peer-push-unbound-taker")
        order = self._order(no="20260925000000000911")

        body = self._assign(order, taker.id, username="peer-push-cs-2")

        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：指派**真的成功了**（否则「没写事件」是因为单子压根没指派出去）
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, taker.id)
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_assign_only_enqueues_and_never_calls_out(self):
        """spec §16.6：指派写一行 `pending`，⛔ 请求路径上一次出站都不发。
        本条取代旧的「投递失败不影响指派」用例——那条的前提「投递发生在请求里」自 §16.6 起已不成立：投递搬到后台 worker ⇒「投递失败不影响指派」不再是一条请求路径上的判据，现在的判据是**根本不投递**。
        ⚠️ 投递内核住 `apps/business/peer_worker.py` ⇒ 桩改打在**那里**（`peer_views` 已不 import `push_events`；⛔ 打在一个已不存在的名字上会 `AttributeError`，而那不是本条要守的判据）。"""
        binding = self._binding_with_account(username="peer-push-down-taker")
        order = self._order(no="20260925000000000912")
        with mock.patch("apps.business.peer_worker.push_events") as push:
            body = self._assign(order, binding.account_id, username="peer-push-cs-3")

        # ① 业务动作**真的成功了**：HTTP 200 + 业务码 0 + 订单/绩效都落库。
        self.assertEqual(body["code"], 0, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id,
            binding.account_id,
        )
        # ② §16.6：一次出站都没有（⛔ 不是"发了但失败了"）。
        push.assert_not_called()
        # ③ 事件行留在 `pending` 等 worker（E-0009：判据打在数据库行上）。
        event = PeerSyncEvent.objects.get()
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.kind, "order.assign")
        self.assertEqual(event.attempts, 0)
        self.assertIsNone(event.processed_at)

    def test_assign_registers_no_delivery_callback(self):
        """§16.6 的另一半：入队**不排 `on_commit` 回调**（连"提交后试一次"都没有了）。
        本条取代旧的「投递异常不打断指派」用例——那条守的是排回调处的 `robust=True`；回调本身已随 §16.6 删除 ⇒ 改守「回调数为 0」。
        **判别力**：把 `transaction.on_commit(lambda: _deliver_later(event.pk), robust=True)` 放回 `peer_dispatch`，本用例的 `len(callbacks) == 0` 立刻红（这正是把投递塞回请求路径的形状）；而 `test_assign_only_enqueues_and_never_calls_out` **抓不到**这种回退——`TestCase` 里 `on_commit` 回调默认不执行、`push_events` 照样没被调用。"""
        binding = self._binding_with_account(username="peer-push-down-taker-2")
        order = self._order(no="20260925000000000913")
        with self.captureOnCommitCallbacks() as callbacks:
            body = self._assign(order, binding.account_id, username="peer-push-cs-4")

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(len(callbacks), 0, "投递已搬出请求路径，不该再排 on_commit 回调")
        self.assertEqual(
            PeerSyncEvent.objects.get().status, PeerSyncEvent.Status.PENDING
        )


# ── 上家侧**真投递** + 下家侧**接收落单**（spec §7.2 / §8.3 / §10.4）──────
#: 下家侧接收端点（spec §8.3）。⚠️ 带尾斜杠（`AGENTS.md` 的项目约定）。
INBOUND_URL = "/api/business/peer-sync/inbound/events/"
#: HTTP 层判据要带的租户域名（`TenantTestCase` 自带租户的域名，见 `test_peer_binding_api.py`）。
PEER_DOMAIN = "tenant.test.com"


class ReceiveEventTests(TenantTestCase):
    """下家侧落单（spec §10.4 / A10 / A11）。"""

    def setUp(self):
        # ⚠️ `Order.game` 是**非空** FK（实测 `Order._meta.get_field("game").null is False`），夹具必须给游戏、并把**游戏名**放进载荷——
        #    ⭐ 裁决 R21 起，接收侧按**名字**对齐（⛔ 不再传 `game_id`：每个租户的 id 都从 1 开始，传 id 会静默挂到错的游戏下）。
        self.game = Game.objects.create(name="上下游落单夹具游戏")
        self.kefu = get_user_model().objects.create_user(
            username="kefu-b", password="pw-b"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _event(self, order_no="A-1", **payload_over):
        payload = {
            "status": OrderStatus.IN_PROGRESS,
            # ⛔ 业务方裁定：客户价（= 接单价）取**发单价**；载荷不再带 total_price。
            "hall_price": "88.00",
            "remark": "上家备注",
            "customer_phone": "",
            "content": "客户原始需求",
            # ⭐ 裁决 R21：游戏按**名字**传（⛔ 不传 `game_id`）。
            "game_name": self.game.name,
        }
        payload.update(payload_over)
        return {
            "idempotency_key": uuid.uuid4().hex,
            "kind": "order.assign",
            "target_ref": order_no,
            "payload": payload,
        }

    def test_payload_creates_an_order_with_source_and_sales_owner(self):
        """A10：来源 = 上家工作室名（缺则建）；销售提成归属人 = **建绑定的那个客服**。
        ⚠️ 接手类落地即未分配（业务方口径）：本条收的是 `order.assign` ⇒ 落地的状态是 `UNASSIGNED`（⛔ 不照抄对端的 `IN_PROGRESS`）。"""
        result = receive_events(self.binding, [self._event()])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["skipped_reasons"], [])
        order = Order.objects.get()
        self.assertEqual(order.order_source.name, "测试工作室甲")
        # ⚠️ 来源字典 FK 与名字快照必须一致（D7）：走 `source_snapshot_overrides` 的理由。
        self.assertEqual(order.source, "测试工作室甲")
        self.assertEqual(order.sales_user_id, self.kefu.pk)
        self.assertEqual(order.sales_agent, "kefu-b")
        self.assertEqual(order.source_order_no, "A-1")
        # ⚠️ P4 起接手类落地即未分配（业务方）。
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # ⭐ 裁决 R21：载荷里的**游戏名**解析成了本租户那一款（`Game.name` 租户内唯一
        # ⇒ 解析确定），⛔ 不是照抄对端 id。
        self.assertEqual(order.game_id, self.game.pk)
        self.assertEqual(order.game.name, self.game.name)
        self.assertEqual(order.total_price, Decimal("88.00"))
        # 去重地基：链接行把两边单号对上。
        link = order.peer_links.get()
        self.assertEqual(link.upstream_binding_id, self.binding.pk)
        self.assertEqual(link.peer_order_no, "A-1")

    def test_sales_agent_uses_the_display_name_not_the_login_account(self):
        """`sales_agent` 与页面「绑定操作人」列**同名**（昵称优先、空串才回落登录账号）。
        ⚠️ 判别力来自「两个取值**不同**」：这里把 `first_name` 设成 `测试客服甲`，而登录账号是 `kefu-b` ⇒ 取错任何一个（例如仍写 `get_username()`）本用例立刻红。⛔ 别改成断言 `kefu-b`——那正是修复前那个 bug 的行为（同一个人两个名字：绑定页 `测试客服甲`、订单 `kefu-b`）。"""
        self.kefu.first_name = "测试客服甲"
        self.kefu.save(update_fields=["first_name"])

        receive_events(self.binding, [self._event()])
        order = Order.objects.get()
        self.assertEqual(order.sales_agent, "测试客服甲")
        # ⚠️ `created_by` **刻意仍是用户名**：模型那一列的注释与既有消费方（「被抢走」通知按
        # username 解析）都把 `created_by` 定义成**用户名**快照，与 `sales_agent` 这个展示用
        # 文本快照语义不同 ⇒ 本用例顺带钉住「没有被顺手一起改掉」。
        self.assertEqual(order.created_by, "kefu-b")
        # 归属仍是那个人（显示名只改快照文案，⛔ 不改 FK）。
        self.assertEqual(order.sales_user_id, self.kefu.pk)

    def test_receiving_again_reuses_the_existing_source_row(self):
        """来源字典幂等（spec §10.4-1）：两单同名 ⇒ `OrderSource` 只有一行。"""
        receive_events(self.binding, [self._event("A-1"), self._event("A-2")])
        self.assertEqual(OrderSource.objects.filter(name="测试工作室甲").count(), 1)

    def test_long_peer_name_is_truncated_to_ten_chars(self):
        """A11：11 字的名字 ⇒ 来源名**恰好 10 字**（断言长度与值，不是"没报错"）。"""
        self.binding.peer_name = "一二三四五六七八九十十一"
        self.binding.save(update_fields=["peer_name"])
        receive_events(self.binding, [self._event()])
        source = OrderSource.objects.get()
        self.assertEqual(len(source.name), 10)
        self.assertEqual(source.name, "一二三四五六七八九十")

    def test_two_peers_with_the_same_first_ten_chars_share_one_source(self):
        """⚠️ A11 的**有意**行为（spec §14-R10）：前 10 字相同 ⇒ 复用同一行来源；用例名写明了这一点，⛔ 别当 bug 修掉。
        ⚠️ 任务书原稿**自相矛盾**（执行期实测）：它的 `other.peer_name` 写成「一二三四五六七八九十乙」并注「前 10 字相同」，但 `setUp` 里的 `self.binding` 名字是「测试工作室甲」——两者截断后**并不相同**，所以 `OrderSource.objects.count()` 原样跑出来是 **2**，那条 `assertEqual(…, 1)` **恒假**（不是实现的问题）。
        这里把第一个绑定也改成前 10 字相同、第 11 字不同的名字，让这条判据真的能证明「前 10 字相同 ⇒ 复用同一行来源」。"""
        self.binding.peer_name = "一二三四五六七八九十甲"     # 11 字，前 10 字与 other 相同
        self.binding.save(update_fields=["peer_name"])
        other = PeerUpstreamBinding.objects.create(
            peer_name="一二三四五六七八九十乙",      # 前 10 字相同
            peer_base_url="https://peer-c.test.invalid",
            peer_account="peer-account-c",
            token_in_hash=hash_token(new_token()), token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )
        receive_events(self.binding, [self._event("A-1")])
        receive_events(other, [self._event("C-1")])
        # 自证两个名字真的共享前 10 字（否则下面那条断言可能靠巧合成立）。
        self.assertEqual(
            source_name_for(self.binding.peer_name),
            source_name_for(other.peer_name),
        )
        self.assertEqual(OrderSource.objects.count(), 1)
        self.assertEqual(Order.objects.count(), 2)

    def test_the_same_peer_order_is_not_applied_twice(self):
        """`peer_order_no` 去重（spec §6.3）：重投同一单 ⇒ 只有一张单。
        ⭐ **新契约**（spec §16.3）：去重不再是"见到已有单就一律跳过"——已有单的**后续事件按事件时刻裁决**（严格更新才应用）。本条打的是**不带 `occurred_at` 的旧形状事件**：已有单 + 时刻不可用 ⇒ 硬跳过 `code=bad_shape`，单子仍然只有一张、且**零写入**。
        ⚠️ 带真时刻的严格规则（相等 ⇒ `stale`、严格更新 ⇒ 应用）由**下一条**用例钉住；⛔ 别把本条读成"更新分支不存在"。"""
        event = self._event("A-1")
        receive_events(self.binding, [event])
        # ⚠️ **重投刻意换一个会改到已存值的载荷**（`remark` 与首投不同）：去重若失效、
        #    走了任何"更新已有单"的分支，下面那条 `remark` 断言立刻红。⛔ 别图省事复用上面那个
        #    `event`——那样第二次的载荷与首投**逐字相同**，断言就恒真（修复波前的缺陷，E-0011 同族）。
        replay = self._event("A-1", remark="重投改过的备注（去重若失效会顶掉首投的值）")
        result = receive_events(self.binding, [replay])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        # 跳过要有原因（`skipped` 与 `skipped_reasons` 一一对应）。
        self.assertEqual(
            [r["target_ref"] for r in result["skipped_reasons"]], ["A-1"]
        )
        self.assertTrue(result["skipped_reasons"][0]["reason"])
        # ⭐ P1 事件没有时刻 ⇒ 机器码是 `bad_shape`（不是 `stale`：没法裁决新旧）。
        self.assertEqual(result["skipped_details"][0]["code"], "bad_shape")
        self.assertEqual(Order.objects.count(), 1)
        # 零写入的自证：重投载荷里**变了**的那个字段没有落到已有单上（去重真的提前返回了）。
        self.assertEqual(Order.objects.get().remark, "上家备注")

    def test_a_replayed_event_is_stale_and_a_newer_one_updates(self):
        """⭐ Ruling 22 的严格口径：相等 ⇒ `stale`（重投），严格更新 ⇒ 应用（spec §16.3）。
        ⚠️ 接手类落地即未分配（业务方口径）：本条的两次事件都是 `order.assign` ⇒ 那张单的状态**一律是 `UNASSIGNED`**。因此本条的判别力落在 `applied` / `skipped` 计数上（下面两段各自钉住），状态的取值只钉「落地即未分配」这一条本期口径。
        判别力：把时刻比较从 `<=` 改成 `<`，第一段的同刻重投会变成"应用"（`applied=1`，该单被更新支再写一次 `UNASSIGNED`——**与首投同值**）⇒ 上面那两条 `applied` / `skipped_details` 断言红（⛔ 不靠状态断言：接手类下两支同值，分辨不出是否重投）；把 `<=` 改成"只看有没有时刻"，第二段不会应用 ⇒ 红。"""
        base = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
        first = self._event("A-1")
        first["occurred_at"] = base.isoformat()
        receive_events(self.binding, [first])

        # ① 同一时刻重投 ⇒ `stale`，⛔ 不是"应用"（相等 = 同一条事件重投）。
        replay = self._event("A-1", status=OrderStatus.COMPLETED)
        replay["occurred_at"] = base.isoformat()
        result = receive_events(self.binding, [replay])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped_details"][0]["code"], "stale")
        # ⚠️ P4 起接手类落地即未分配（业务方）。
        self.assertEqual(Order.objects.get().status, OrderStatus.UNASSIGNED)

        # ② 严格晚于已应用到的时刻 ⇒ 更新（spec §16.3）。
        newer = self._event("A-1", status=OrderStatus.COMPLETED)
        newer["occurred_at"] = (base + timedelta(minutes=1)).isoformat()
        result = receive_events(self.binding, [newer])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)
        # ⚠️ P4 起接手类落地即未分配（业务方）：这条更新事件也是 `order.assign`
        #    ⇒ 落地状态仍是 `UNASSIGNED`（判别力见方法 docstring 的说明）。
        self.assertEqual(Order.objects.get().status, OrderStatus.UNASSIGNED)

    def test_a_different_peer_order_no_is_a_second_order(self):
        """阴性对照：去重键是 `(绑定, 对端单号)` ⇒ 换个单号就是新单（防"一绑定只落一单"）。"""
        receive_events(self.binding, [self._event("A-1")])
        result = receive_events(self.binding, [self._event("A-2")])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["skipped_reasons"], [])
        self.assertEqual(Order.objects.count(), 2)

    def test_the_game_is_aligned_by_name_not_by_the_peers_id(self):
        """⭐ 裁决 R21 的**判别用例**：游戏按**名字**对齐，⛔ 不照抄对端 id。
        ⚠️ 载荷里**同时**放一个 `game_id`（指向本租户**另一款**游戏）与正确的 `game_name`：旧实现写 `game_id=payload.get("game_id")` ⇒ 这一单会静默挂到 `decoy` 下（而接口照样回 `applied: 1`——"错单被报成成功"）；本实现在 `game_name` 上解析 ⇒ 落 `self.game`。
        两个游戏都在本租户里存在，所以这条判据分辨的是**对齐口径**，不是"解析得到与否"。"""
        decoy = Game.objects.create(name="另一款游戏（对端 id 恰好指向它）")
        result = receive_events(
            self.binding, [self._event(game_id=decoy.pk)]
        )
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)
        order = Order.objects.get()
        self.assertEqual(order.game_id, self.game.pk)
        self.assertNotEqual(order.game_id, decoy.pk)

    def test_an_unknown_game_name_creates_the_game_grants_it_and_writes_the_order(self):
        """⭐ 未知名 ⇒ 自动建同名游戏、把单录进去、并把这款游戏授权给**建这条绑定的那个客服**（spec §16.4；⛔ 不是漏单、⛔ 不是猜一款）。
        ⚠️ 旧行为（未知名 = 硬跳过、一行都不写）已被推翻——⛔ 别把断言改回"跳过"。判别力：① 不建游戏 ⇒ `Game.objects.filter(name=…).exists()` 红；② 建了但没用在订单行上 ⇒ `order.game_id` 红；③ 不给操作人授权 ⇒ `profile.games` 红。
        ⛔ 缺失名 / 非字符串名**仍然是**硬跳过，由下面两条相邻用例各自钉住。"""
        # 建绑定的人有档案才谈得上授权（本夹具的 `self.kefu` 默认没有档案）。
        StaffProfile.objects.create(user=self.kefu)

        result = receive_events(
            self.binding, [self._event(game_name="本租户没有的游戏")]
        )

        self.assertEqual(result["applied"], 1, result)
        self.assertEqual(result["skipped"], 0, result)
        self.assertEqual(result["skipped_reasons"], [])
        game = Game.objects.get(name="本租户没有的游戏")
        self.assertTrue(game.is_active)
        order = Order.objects.get()
        self.assertEqual(order.game_id, game.pk)
        self.assertEqual(order.source_order_no, "A-1")
        self.assertTrue(PeerOrderLink.objects.filter(order=order).exists())
        # A15：授权落在**数据库出口**上（重新查询，⛔ 不读可能带缓存的 `profile.games`）。
        self.assertTrue(
            StaffProfile.objects.get(user=self.kefu).games.filter(pk=game.pk).exists()
        )

    def test_a_missing_game_name_is_skipped_loudly(self):
        """缺失游戏名同样不许"随便挑一款"（`Order.game` 非空不是猜一个的理由）。"""
        event = self._event()
        del event["payload"]["game_name"]
        result = receive_events(self.binding, [event])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(result["skipped_reasons"][0]["reason"])
        self.assertFalse(Order.objects.exists())

    def test_a_non_string_game_name_is_skipped_not_a_500(self):
        """畸形游戏名（对端发了个对象）⇒ 跳过，⛔ 不是 500（E-0010：先归一形状）。"""
        result = receive_events(
            self.binding, [self._event(game_name={"nope": 1})]
        )
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(Order.objects.exists())

    def test_project_text_rides_along_and_lands_verbatim(self):
        """⭐ `project_text` 随载荷过来并原样落（与 R21 一并裁定）。
        ⚠️ `project_id` / `server_id` 本期**不消费**（对齐另批）；`project_text` 是自由文本、是 D9 的**权威值** ⇒ 带上它同步过来的单才不至于在"项目"那一栏全空。"""
        receive_events(
            self.binding, [self._event(project_text="上家写的项目名")]
        )
        self.assertEqual(Order.objects.get().project_text, "上家写的项目名")

    def test_an_event_without_a_target_ref_is_skipped(self):
        """没有对端单号 ⇒ 跳过（去重键缺一半，落单无从谈起），⛔ 不建"无名单"。"""
        event = self._event()
        event["target_ref"] = ""
        result = receive_events(self.binding, [event])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue(result["skipped_reasons"][0]["reason"])
        self.assertFalse(Order.objects.exists())

    # ── 状态白名单（spec §10.2 / §12.3 #16；评审判定 Important）────────────────
    def test_a_known_status_is_applied_verbatim(self):
        """已知状态被接住并落库（⛔ 不被跳过、⛔ 不必回落到任何默认态）。
        ⚠️ 接手类落地即未分配（业务方口径）：本条走的是**建单**分支、事件是 `order.assign` ⇒ 落地状态是 `UNASSIGNED`（⛔ 不是"原样照抄对端状态」）；「已知状态被接受、响应形状不变」这两条判据一字未改，改的只是落地取值。`order.update` 上的「按接受集原样落库」由 `tests/test_peer_p3_inbound_down.py` 的用例继续钉住。
        ⚠️ 这里推的状态必须落在 `DOWNSTREAM_ACCEPTED_STATUSES` 里（业务方裁定 R1「下家只认那 4 种」）——原来推的 `COMPLETED` 在**集外**。这条用例走的是**建单**分支（本地没有 `PeerOrderLink`），而建单分支**有意不设接受集闸**（不漏单，A14），所以它当时**没有变红**；但留着集外的取值，会让本用例与 R1 的裁定读起来自相矛盾。"""
        result = receive_events(
            self.binding, [self._event(status=OrderStatus.IN_PROGRESS)]
        )
        # 响应形状钉死（多出的键是「跳过原因」通道，见 `receive_events` docstring）：
        # `skipped_details` 是**机器契约**（每条带 `code`，spec §16.5）。
        self.assertEqual(
            set(result),
            {"applied", "skipped", "skipped_reasons", "skipped_details"},
        )
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["skipped"], 0)
        # ⚠️ P4 起接手类落地即未分配（业务方）。
        self.assertEqual(Order.objects.get().status, OrderStatus.UNASSIGNED)

    def test_an_unknown_status_is_not_applied(self):
        """⛔ 未知状态**响亮失败**：不落单、不回落到默认态，且 skipped 里留原因。
        判别力：旧实现 `status=payload.get("status") or OrderStatus.UNASSIGNED` 把 `"NOT_A_STATUS"` **原样**写进 `Order.status`（没有数据库约束拦它）——本用例的「一单都没有」当场红；改回 `or UNASSIGNED` 也一样红。"""
        result = receive_events(
            self.binding, [self._event(status="NOT_A_STATUS")]
        )
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(result["skipped_reasons"]), 1)
        self.assertEqual(result["skipped_reasons"][0]["target_ref"], "A-1")
        # 原因里必须带上那个认不出的值（否则排障时只知道"跳过了"）。
        self.assertIn("NOT_A_STATUS", result["skipped_reasons"][0]["reason"])
        # ⛔ 不落单：既不是原样写入，也不是回落成 `UNASSIGNED`。
        self.assertFalse(Order.objects.exists())
        self.assertFalse(PeerOrderLink.objects.exists())

    def test_a_missing_status_is_not_applied(self):
        """缺失状态同样**不许回落** `UNASSIGNED`（spec §12.3 #16）。
        ⚠️ 这里是把键**删掉**（不是传 `None`）——「缺失」与「显式 null」在 `payload.get("status")` 上取值相同，但删键才是上家真正会发出的形状。"""
        event = self._event()
        del event["payload"]["status"]
        result = receive_events(self.binding, [event])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["skipped_reasons"][0]["target_ref"], "A-1")
        self.assertTrue(result["skipped_reasons"][0]["reason"])
        self.assertFalse(Order.objects.exists())
        # 被拒的事件 ⛔ 不得留下任何 `OrderSource` 脏行（判据放在 `atomic()` 之前的原因）。
        self.assertFalse(OrderSource.objects.exists())


class DeliverEventTests(TenantTestCase):
    """上家侧投递（spec §7.2 / §7.6 / §16.6）：成功落 `done`、失败**退避重排**、终态才 `failed`。
    ⚠️ **Ruling 10**：旧的「失败 ⇒ 立刻 `failed`」契约已被 spec §16.6 第 3 条**取代**（失败 ⇒ `pending` + 退避）⇒ 四条用例**就地改准**，⛔ 不是删掉重写。调用与 `mock.patch` 目标都在 `apps.business.peer_worker`（Ruling 6 / E-0029：桩打在**被查找的那个模块**上）。
    ⚠️ 终态不是"第 4 次"而是"**第 5 次**"失败——四档退避（`1m/5m/15m/1h`）必须**全部可达**。第 4 次 ⇒ 仍 `pending` + `now + 3600`；第 5 次 ⇒ `failed` 终态。"""

    def setUp(self):
        self.now = django_timezone.now()
        self.account = get_user_model().objects.create_user(
            username="peer-account-a", password="pw-a"
        )
        self.downstream = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url="https://peer-b.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        self.event = PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=str(self.downstream.pk),
            direction="out", kind="order.assign", target_type="order",
            target_ref="A-1", payload={"status": "IN_PROGRESS"},
            idempotency_key=uuid.uuid4().hex,
        )

    @mock.patch("apps.business.peer_worker.push_events")
    def test_success_marks_done_and_clears_failures(self, push_events):
        self.downstream.consecutive_failures = 3
        self.downstream.save(update_fields=["consecutive_failures"])
        self.assertEqual(deliver_one(self.event.pk, now=self.now), "done")
        self.event.refresh_from_db()
        self.downstream.refresh_from_db()
        self.assertEqual(self.event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(self.event.processed_at, self.now)
        self.assertEqual(self.downstream.consecutive_failures, 0)
        self.assertEqual(self.downstream.last_ok_at, self.now)
        # ⚠️ 桩必须**真的被调用**：不钉这一条，`deliver_one` 里把 `push_events` 整段删掉
        #    也全绿（`assertLogs` 同族的自指陷阱，E-0029 / E-0009）。
        push_events.assert_called_once()
        base_url, token, events = push_events.call_args.args
        self.assertEqual(base_url, "https://peer-b.test.invalid")
        self.assertEqual(token, self.downstream.token_out)
        # 载荷**原样**发（R14：`event.payload` 已是 JSON 安全的；⛔ 不二次转换）。
        self.assertEqual(events, [{
            "idempotency_key": self.event.idempotency_key,
            "kind": "order.assign",
            "target_ref": "A-1",
            "payload": {"status": "IN_PROGRESS"},
        }])

    @mock.patch("apps.business.peer_worker.push_events")
    def test_meta_rides_on_the_event_object_top_level(self, push_events):
        """⭐ 事件级元数据随事件对象**顶层**外发（spec §16.3），⛔ 不塞进 payload。
        ⚠️ 这条钉的是**跨进程契约**：接收侧从 `raw.get("occurred_at")` 读（⛔ 不读 `payload`）⇒ 发送侧少了这几个键，同单更新与撤单落库在真环境里**永远走不到**（首单照常落、后续事件全被判 `bad_shape`），而只直接构造事件对象的用例全绿——那种"两层各自绿、接线断了没人看得见"正是本条要防的（E-0029 同族）。"""
        self.event.meta = {
            "occurred_at": "2026-09-26T10:00:00+08:00",
            "withdraw_reason": "taker_removed",
            "actor_name": "测试客服甲",
        }
        self.event.save(update_fields=["meta"])

        deliver_one(self.event.pk, now=self.now)

        events = push_events.call_args.args[2]
        self.assertEqual(events[0]["occurred_at"], "2026-09-26T10:00:00+08:00")
        self.assertEqual(events[0]["withdraw_reason"], "taker_removed")
        self.assertEqual(events[0]["actor_name"], "测试客服甲")
        # 事件级字段 ⛔ 不进 `payload`（payload 只放 §10.1 白名单里的订单字段）。
        for key in ("occurred_at", "withdraw_reason", "actor_name"):
            self.assertNotIn(key, events[0]["payload"])
        # 载荷本身仍是那一条（展开 meta 没有顶掉它）。
        self.assertEqual(events[0]["payload"], {"status": "IN_PROGRESS"})

    @mock.patch("apps.business.peer_worker.push_events",
                side_effect=PeerUnreachable("connection refused"))
    def test_a_failed_attempt_is_rescheduled_with_the_first_backoff(self, _):
        """⭐ Ruling 10：失败**不再**直接 `failed`——落 `pending` + `now + 60`（spec §16.6 第 3 条）。
        判别力：把退避那段删掉、恢复成"失败即 `failed`" ⇒ 本条的 `status` / `next_attempt_at` 两条断言红（这正是旧的契约）。"""
        self.assertEqual(deliver_one(self.event.pk, now=self.now), "retry")  # ⛔ 不抛
        self.event.refresh_from_db()
        self.downstream.refresh_from_db()
        self.assertEqual(self.event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(self.event.next_attempt_at, self.now + timedelta(seconds=60))
        self.assertIsNone(self.event.processed_at)
        self.assertEqual(self.event.attempts, 1)
        self.assertIn("connection refused", self.event.reason)
        self.assertEqual(self.downstream.consecutive_failures, 1)
        self.assertIn("connection refused", self.downstream.last_error)
        self.assertEqual(self.downstream.last_error_at, self.now)

    @mock.patch("apps.business.peer_worker.push_events",
                side_effect=PeerUnreachable("connection refused"))
    def test_the_fourth_failure_uses_the_hour_and_the_fifth_is_terminal(self, _):
        """同一契约的另一半：四档退避全可达，**第 5 次**才终态。
        判别力：终态判据写成 `>=`（旧口径）⇒ 第一段会在 `status == FAILED` 上红，而 `1h` 那一档永远用不到。"""
        self.event.attempts = 3
        self.event.save(update_fields=["attempts"])

        # 第 4 次失败 ⇒ 仍可重投，退避取最后一档 1h。
        self.assertEqual(deliver_one(self.event.pk, now=self.now), "retry")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(self.event.attempts, 4)
        self.assertEqual(self.event.next_attempt_at, self.now + timedelta(seconds=3600))

        # 第 5 次失败 ⇒ 终态。
        self.assertEqual(deliver_one(self.event.pk, now=self.now), "failed")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(self.event.attempts, 5)
        self.assertEqual(self.event.reason, "connection refused")

    def test_a_non_pending_event_is_not_delivered_again(self):
        """只投 `pending`：已 `done` / `failed` 的行不再重投。"""
        self.event.status = PeerSyncEvent.Status.DONE
        self.event.save(update_fields=["status"])
        with mock.patch("apps.business.peer_worker.push_events") as push_events:
            self.assertEqual(deliver_one(self.event.pk, now=self.now), "noop")
        push_events.assert_not_called()
        self.event.refresh_from_db()
        self.assertEqual(self.event.attempts, 0)

    def test_a_revoked_binding_blocks_delivery(self):
        """已解除的绑定不得再收推送（spec §16.6 第 4 条末句）：`state=ACTIVE` 是承重的。
        ⭐ 拦下之后事件转**终态** `failed`（`dropped`），⛔ 不许留在 `pending` 被反复认领（那会把一个租户的名额吃光、饿死其它绑定）。"""
        self.downstream.state = PeerDownstreamBinding.State.REVOKED
        self.downstream.save(update_fields=["state"])
        with mock.patch("apps.business.peer_worker.push_events") as push_events:
            self.assertEqual(deliver_one(self.event.pk, now=self.now), "dropped")
        push_events.assert_not_called()
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(self.event.attempts, 0)
        self.assertEqual(self.event.reason, "绑定已解除/失效，不再投递")


class PeerInboundEventsEndpointTests(TenantTestCase):
    """下家侧接收**端点**（spec §8.3）：Bearer 认证 + 落单 + 拒会话 + 畸形入参。
    ⚠️ 只测函数层的 `receive_events` **不够**——那条路不经过认证、URL 与信封。端点本身是 spec §8.3 的对外契约（也是 `test_no_route_500` 的令牌豁免对象），没有这一层，「路由没挂上 / 认证装错 / 信封形状变了」全是静默的（本文件模块 docstring 的两层判据同一理由）。
    ⚠️ 本类的令牌按**裁决 R15** 的形状造：我是下家 ⇒ 验的是 `token_in_hash`（上家推单时带的 `token_a`）⇒ `raw` 哈希进 `token_in_hash`；`token_out` 是我方外发的 `token_b`（明文）。⚠️ 本类的绿**只**证明端点装配正确；「真实握手后两个方向的令牌都能验通」这条判据由 `tests/test_peer_binding_api.py::PeerHandshakeCredentialTests` 端到端钉住。"""

    def setUp(self):
        self.game = Game.objects.create(name="上下游接收端点夹具游戏")
        self.kefu = get_user_model().objects.create_user(
            username="kefu-b", password="pw-b"
        )
        self.raw = new_token()
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(self.raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _events(self):
        return [{
            "idempotency_key": uuid.uuid4().hex,
            "kind": "order.assign",
            "target_ref": "A-1",
            # ⭐ 裁决 R21：游戏按名字对齐（接收端点那一层也走同一条判据）。
            "payload": {"status": OrderStatus.IN_PROGRESS, "game_name": self.game.name},
        }]

    def _post(self, body, *, token=None):
        headers = (
            {"HTTP_AUTHORIZATION": "Bearer " + token} if token is not None else {}
        )
        return Client(SERVER_NAME=PEER_DOMAIN).post(
            INBOUND_URL, body, content_type="application/json", **headers
        )

    def test_bearer_token_applies_the_events(self):
        resp = self._post({"events": self._events()}, token=self.raw)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        data = resp.json()["data"]
        self.assertEqual(data["applied"], 1)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["skipped_reasons"], [])
        order = Order.objects.get()
        self.assertEqual(order.source_order_no, "A-1")
        self.assertEqual(order.order_source.name, "测试工作室甲")
        self.assertEqual(order.sales_user_id, self.kefu.pk)

    def test_a_display_name_longer_than_the_column_is_truncated(self):
        """R19：显示名长于 `sales_agent` 列宽 ⇒ **不 500、不整批回滚**，落库值**恰好列宽**。
        缺陷形状：`Order.sales_agent` 是 `CharField(max_length=64)`，而它装的是操作人**显示名**（`User.first_name` 的上限是 150）⇒ 长名落单时 PostgreSQL 抛 `value too long for type character varying(64)`；整批共用一个事务 ⇒ **一条长名的推送会把同一批里其它正常的单一起回滚**，上家侧只看到 500 / failed。
        ⚠️ 判据**同时钉值与长度**（⛔ 不是"没抛异常"）：只钉「请求 200」在 `value[:64]` 与 `value[:150]` 之间分辨不出来（E-0083 族：判据必须能失败）。⚠️ 宽度从**模型元数据**取（与实现同一出处，⛔ 不抄 64）；但**期望值**由夹具名自己切出来、且先自证夹具真的超过列宽 ⇒ 不是恒真的自指断言（把实现改回"不截断"本用例立刻红）。"""
        width = Order._meta.get_field("sales_agent").max_length
        long_name = "长" * (width + 20)
        self.assertGreater(len(long_name), width)      # 夹具自证：真的超过列宽
        self.kefu.first_name = long_name
        self.kefu.save(update_fields=["first_name"])

        resp = self._post({"events": self._events()}, token=self.raw)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        self.assertEqual(resp.json()["data"]["applied"], 1, resp.content[:200])
        order = Order.objects.get()
        self.assertEqual(len(order.sales_agent), width)
        self.assertEqual(order.sales_agent, long_name[:width])

    def test_no_token_is_401_and_writes_nothing(self):
        resp = self._post({"events": self._events()})
        self.assertEqual(resp.status_code, 401, resp.content[:200])
        self.assertEqual(resp.json()["code"], 401)
        self.assertFalse(Order.objects.exists())

    def test_session_cookie_is_rejected(self):
        """spec §9-S2：同步端点**不认会话**（⚠️ 用真登录，理由见 `test_peer_binding_api.py`）。"""
        client = make_logged_in_client(
            domain=PEER_DOMAIN, username="peer-inbound-session"
        )
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        resp = client.post(
            INBOUND_URL, {"events": self._events()}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401, resp.content[:200])
        self.assertFalse(Order.objects.exists())

    def test_events_must_be_a_list(self):
        """畸形入参 ⇒ 业务码 `1000`，⛔ 不是 500（E-0010：请求来源的值先过校验）。"""
        resp = self._post({"events": "nope"}, token=self.raw)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 1000)
        self.assertFalse(Order.objects.exists())

    def test_a_non_object_body_is_rejected_not_reported_as_success(self):
        """非 dict 顶层请求体 ⇒ 业务码 `1000`，⛔ 不是 `{"applied":0}` + 码 0。
        判别力（旧实现的**静默成功**）：裸数组被旧代码归一成 `{}` ⇒ `events=[]` ⇒ 回 `{"applied":0,"skipped":0}` + 码 0 ⇒ **上家把这次推送标成 done，实际一条都没落**。本用例钉 `code == 1000`；旧实现回 0 ⇒ 红。
        ⚠️ 三种形状都**直接发 JSON 文本**：Django 测试客户端的 `_encode_json` 只序列化 `dict/list/tuple`（读 `django.test.client.Client._encode_json` 源码实测），传裸 `"nope"` / `123` 会以**非法 JSON** 打出去、变成 400 解析错——那就测不到本 defect 了。"""
        for body_text in ("[1, 2]", '"nope"', "123"):
            with self.subTest(body=body_text):
                resp = Client(SERVER_NAME=PEER_DOMAIN).post(
                    INBOUND_URL, body_text, content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer " + self.raw,
                )
                self.assertEqual(resp.status_code, 200, resp.content[:200])
                self.assertEqual(resp.json()["code"], 1000, resp.content[:200])
                self.assertTrue(resp.json()["msg"])
                self.assertFalse(Order.objects.exists())

    def _downstream_binding(self):
        """给 `self.kefu` 建一条**我方为上家**的绑定，回它收到的那个令牌明文。
        ⚠️ 令牌按 `PeerTokenAuthentication` 的形状造：`token_in_hash` 是**对方向我带的那套**（存哈希），所以这里把明文交出来给请求头用、库里只留哈希。"""
        raw_down = new_token()
        PeerDownstreamBinding.objects.create(
            account=self.kefu,
            peer_base_url="https://peer-b.test.invalid",
            token_in_hash=hash_token(raw_down),
            token_out=new_token(),
        )
        return raw_down

    def test_a_downstream_binding_token_enters_the_upstream_echo_branch(self):
        """R4①：下家绑定的令牌**不再**被 1020 拒收。本端点现在**分方向**：`PeerUpstreamBinding` 令牌 ⇒ 下家收上家推来的单；`PeerDownstreamBinding` 令牌 ⇒ **上家收下家回传的状态**（上行支）。
        ⇒ 拿一条指向**本地不存在订单**的 `order.update` 打进来，应当回 `code=0` 且 `skipped_details` 里是 `unknown_order`（业务上一条都没落，但通道是通的）。
        ⚠️ 判据必须钉 `code == 0`（旧实现回 1020 ⇒ 红）+ 那个具体的跳过码：⛔ 只钉「不是 500」不够——1020 也满足它。同时钉「一单没落」。"""
        raw_down = self._downstream_binding()
        resp = self._post(
            {"events": [{
                "kind": "order.update",
                "target_ref": "下游回传的一张本地没有的单",
                "occurred_at": django_timezone.now().isoformat(),
                # 状态必须是**上行接受集**里的值，否则会先落到 unknown_status。
                "payload": {"status": "PENDING_REVIEW"},
            }]},
            token=raw_down,
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 0, body)
        self.assertTrue(
            any(
                detail["code"] == "unknown_order"
                for detail in body["data"]["skipped_details"]
            ),
            body,
        )
        self.assertFalse(Order.objects.exists())
        # 下家那条绑定**一行都没被动过**（上行支不断发送方的账）。
        self.assertEqual(PeerDownstreamBinding.objects.count(), 1)

    def test_a_binding_of_an_unknown_type_is_still_loudly_rejected(self):
        """R4②：既不是上游、也不是下游的绑定类型 ⇒ **仍响亮拒收**（业务码 `1020`）。
        ⛔ 这条判据**不许删**：它是 `PeerUnbindView` 那条防线的同族——分方向之后，视图的 `else` 支必须继续把"我认不出你这个绑定类型"翻成业务码，⛔ 不许退化成 500，⛔ 也不许把未知类型静默塞进某一条已有的支。
        ⚠️ 驱动方式：本项目当前只有两张绑定表 ⇒ 造不出第三种真实绑定，所以 `mock.patch` 掉 `PeerTokenAuthentication.authenticate`（**打在视图查找它的那个模块**上，E-0029），让它挂一个**两张表都不是**的绑定对象并放行认证。"""
        dummy_binding = object()          # 既不是 PeerUpstreamBinding 也不是 PeerDownstreamBinding

        def fake_authenticate(auth, request):
            # `auth` 是认证类实例（DRF 按位置传），与测试用例的 `self` 无关。
            request._peer_binding = dummy_binding
            return (self.kefu, None)

        with mock.patch(
            "apps.business.api.peer_views.PeerTokenAuthentication.authenticate",
            fake_authenticate,
        ):
            resp = self._post({"events": self._events()}, token="irrelevant-token")

        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 1020, resp.content[:200])
        self.assertTrue(resp.json()["msg"])
        self.assertFalse(Order.objects.exists())


# ══ peer 同步身份统一为「来源单号」（计划 §2 / §5-1 / §5-7）══════════
#: 出向夹具的来源单号（**与内置单号取值不同** ⇒ 「取错列」能分辨出来，E-0024）。
SYN_SOURCE_NO = "SYN-SRC-PUSH-1"


class PeerSourceIdentityOutboundTests(_PeerPushFixture):
    """出向**所有 kind** 的 `target_ref` = `effective_source_order_no(order)`（计划 §2 第 1 条）。
    ⚠️ 判据打在**库里的 `PeerSyncEvent` 行**上（E-0009 / E-0024），不是内存对象。
    ⚠️ 每条都要求 `source_order_no` 与 `order_no` **取值不同**——同值时两种实现结果一样，抓不到「还在用内置号」。"""

    def _order_with_source(self, no="20260925000000000950"):
        """一张**有来源单号**的本地单（来源号 ≠ 内置号）。"""
        order = self._order(no=no)
        order.source_order_no = SYN_SOURCE_NO
        order.save(update_fields=["source_order_no"])
        return order

    def test_the_assign_event_targets_the_source_order_no(self):
        binding = self._binding_with_account(username="peer-src-assign")
        order = self._order_with_source()
        event = enqueue_order_assigned(order, binding.account)
        row = PeerSyncEvent.objects.get(pk=event.pk)
        self.assertEqual(row.target_ref, SYN_SOURCE_NO)
        self.assertNotEqual(row.target_ref, order.order_no)
        # ⭐ 载荷仍带**内置号**：对端建单 / 兼容老链路（对账第二个键）都要用它，
        #    ⛔ 不因为身份键换了就把它从载荷里删掉。
        self.assertEqual(row.payload["order_no"], order.order_no)
        # 载荷里的来源单号仍是本单自己的那一列（白名单字段，未改语义）。
        self.assertEqual(row.payload["source_order_no"], SYN_SOURCE_NO)

    def test_an_empty_source_order_no_falls_back_to_the_builtin_no(self):
        """来源号为空 ⇒ 回落内置号（计划 §5-1 的后半；`_order` 夹具默认就是空来源号）。"""
        binding = self._binding_with_account(username="peer-src-fallback")
        order = self._order(no="20260925000000000951")
        self.assertEqual(order.source_order_no, "")
        enqueue_order_assigned(order, binding.account)
        row = PeerSyncEvent.objects.get()
        self.assertEqual(row.target_ref, order.order_no)

    def test_a_blank_source_order_no_falls_back_too(self):
        """**纯空白**也算「没有」（唯一出处的既有口径：⛔ 不把空白当单号发出去）。"""
        binding = self._binding_with_account(username="peer-src-blank")
        order = self._order(no="20260925000000000952")
        order.source_order_no = "   "
        order.save(update_fields=["source_order_no"])
        enqueue_order_assigned(order, binding.account)
        self.assertEqual(PeerSyncEvent.objects.get().target_ref, order.order_no)

    def test_grab_and_withdraw_target_the_source_order_no(self):
        """另两类订单事件（`order.grab` / `order.withdraw`）同一把尺子。"""
        binding = self._binding_with_account(username="peer-src-grab")
        order = self._order_with_source(no="20260925000000000953")

        grabbed = enqueue_order_grabbed(order, binding.account)
        self.assertEqual(PeerSyncEvent.objects.get(pk=grabbed.pk).target_ref, SYN_SOURCE_NO)

        PeerSyncEvent.objects.all().delete()
        withdrawn = enqueue_order_withdrawn(order, binding.account, "taker_removed")
        self.assertEqual(
            PeerSyncEvent.objects.get(pk=withdrawn.pk).target_ref, SYN_SOURCE_NO
        )

    def test_the_hall_throw_targets_the_source_order_no(self):
        """大厅镜像广播（`hall.throw`）也走来源号（计划 §2 第 1 条的 hall 类）。"""
        binding = self._binding_with_account(username="peer-src-hall")
        self.assertTrue(binding.hall_push_enabled)      # 夹具自证：推得出去
        order = self._order_with_source(no="20260925000000000954")
        made = enqueue_hall_event(order, kind=HALL_THROW_KIND)
        self.assertEqual(len(made), 1)
        row = PeerSyncEvent.objects.get(pk=made[0].pk)
        self.assertEqual(row.kind, HALL_THROW_KIND)
        self.assertEqual(row.target_ref, SYN_SOURCE_NO)
        # 载荷仍带本单内置号（下一跳 / 兼容对账都靠它）。
        self.assertEqual(row.payload["order_no"], order.order_no)

    def test_the_screenshot_event_targets_the_source_order_no_and_keeps_the_object_key(self):
        """截图事件：`target_ref` = 来源号，`object_key` **逐字不变**（计划 §5-5）。"""
        binding = self._binding_with_account(username="peer-src-shot")
        order = self._order_with_source(no="20260925000000000955")
        # 截图的下行支只发给「本单当前 MAIN 接手人」，而且要它有生效下游绑定。
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(
            user=binding.account
        )
        key = f"shared-orders/{SYN_SOURCE_NO}/abc.jpg"
        made = enqueue_screenshot_event(
            order, kind="order.screenshot.add", object_key=key
        )
        down = [event for event in made if event.binding_role == "downstream"]
        self.assertEqual(len(down), 1)
        row = PeerSyncEvent.objects.get(pk=down[0].pk)
        self.assertEqual(row.target_ref, SYN_SOURCE_NO)
        self.assertEqual(row.payload["object_key"], key)

    def test_the_upstream_status_reply_uses_the_peer_side_source_no(self):
        """回上行：`target_ref` = `link.peer_order_no`（**对端的来源单号**，计划 §2 第 1 条尾）。"""
        from apps.business.peer_dispatch import enqueue_upstream_status

        order = self._order_with_source(no="20260925000000000956")
        # 上行回传只认那四种状态（`UPSTREAM_TRIGGER_STATUSES`）⇒ 先把单放到其中一档。
        order.status = OrderStatus.ISSUE
        order.save(update_fields=["status"])
        upstream = PeerUpstreamBinding.objects.create(
            peer_name="测试上家来源号",
            peer_base_url="https://peer-src-up.test.invalid",
            peer_account="peer-src-up-account",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-src-down.test.invalid",
            bound_by=self._plain_user("peer-src-up-binder"),
        )
        # 新口径：链路上这一列存的就是**对端的来源单号**（收向建行时写 target_ref）。
        PeerOrderLink.objects.create(
            order=order, upstream_binding=upstream, peer_order_no="SYN-SRC-UPSTREAM-1"
        )
        event = enqueue_upstream_status(order)
        self.assertIsNotNone(event)
        self.assertEqual(
            PeerSyncEvent.objects.get(pk=event.pk).target_ref, "SYN-SRC-UPSTREAM-1"
        )


class ReceiveSourceIdentityTests(TenantTestCase):
    """收向：两列都落**对端的来源单号**；老链路兼容不建重复单（计划 §2 第 2 条 / §4 / §5-2/3）。
    ⚠️ 夹具照 `ReceiveEventTests`（同一套形状），但每个用例只关心**身份列**，所以刻意不复用那个类（复用它会把父类用例再跑一遍）。"""

    def setUp(self):
        self.game = Game.objects.create(name="来源号收向夹具游戏")
        self.kefu = get_user_model().objects.create_user(
            username="kefu-src-in", password="pw-src-in"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室来源号",
            peer_base_url="https://peer-src-a.test.invalid",
            peer_account="peer-src-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-src-b.test.invalid",
            bound_by=self.kefu,
        )

    def _event(self, target_ref, *, occurred_at=None, kind="order.assign",
               **payload_over):
        payload = {
            "status": OrderStatus.IN_PROGRESS,
            "hall_price": "88.00",
            "remark": "上家备注",
            "customer_phone": "",
            "content": "客户原始需求",
            "game_name": self.game.name,
        }
        payload.update(payload_over)
        event = {
            "idempotency_key": uuid.uuid4().hex,
            "kind": kind,
            "target_ref": target_ref,
            "payload": payload,
        }
        if occurred_at is not None:
            event["occurred_at"] = occurred_at
        return event

    def test_a_new_source_ref_lands_in_both_identity_columns(self):
        """`Order.source_order_no` 与 `PeerOrderLink.peer_order_no` 都 = `target_ref`。"""
        result = receive_events(
            self.binding,
            [self._event("SYN-SRC-IN-1", order_no="SYN-BUILTIN-IN-1")],
        )
        self.assertEqual(result["applied"], 1, result)
        order = Order.objects.get()
        self.assertEqual(order.source_order_no, "SYN-SRC-IN-1")
        link = order.peer_links.get()
        self.assertEqual(link.peer_order_no, "SYN-SRC-IN-1")
        # 内置号是**本地生成**的（⛔ 不是对端那个）——两者的分工在这里最清楚。
        self.assertNotEqual(order.order_no, "SYN-SRC-IN-1")
        self.assertNotEqual(order.order_no, "SYN-BUILTIN-IN-1")

    def test_a_legacy_builtin_link_is_still_hit_by_the_new_source_ref(self):
        """兼容（承重）：老链路按旧内置号建 ⇒ 新对端的来源号靠**载荷 `order_no`** 兜一次，
        仍落到同一张单、⛔ 不建重复单（计划 §4 的收向兼容）。"""
        receive_events(
            self.binding,
            [self._event("SYN-OLD-BUILTIN", occurred_at="2026-10-25T10:00:00+00:00")],
        )
        order = Order.objects.get()
        self.assertEqual(order.source_order_no, "SYN-OLD-BUILTIN")

        # 对端升级后：`target_ref` 变成它的**来源号**，载荷里还带着它自己的内置号。
        result = receive_events(
            self.binding,
            [
                self._event(
                    "SYN-NEW-SOURCE",
                    occurred_at="2026-10-25T10:05:00+00:00",
                    kind="order.update",
                    order_no="SYN-OLD-BUILTIN",
                    status=OrderStatus.ISSUE,
                )
            ],
        )
        self.assertEqual(result["applied"], 1, result)
        # 判别力：没有兜底那一次 ⇒ 这两条都会红（建了第二张单 / 第二行链路）。
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(PeerOrderLink.objects.count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.ISSUE)
        # ⛔ 不做存量反推：老链路那一列**保持旧值**（它记的是当年的键）。
        self.assertEqual(order.peer_links.get().peer_order_no, "SYN-OLD-BUILTIN")

    def test_a_legacy_builtin_ref_without_a_second_key_still_creates_one_order(self):
        """没有第二个键可兜（老对端只发内置号）⇒ 仍按 `target_ref` 建单，行为逐字不变。"""
        result = receive_events(self.binding, [self._event("SYN-ONLY-BUILTIN")])
        self.assertEqual(result["applied"], 1, result)
        order = Order.objects.get()
        self.assertEqual(order.source_order_no, "SYN-ONLY-BUILTIN")
        self.assertEqual(order.peer_links.get().peer_order_no, "SYN-ONLY-BUILTIN")
