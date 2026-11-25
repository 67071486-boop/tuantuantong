"""上下游 P3 Task 2：`order.update` 入队内核与状态漏斗（spec §7.1 / §16.2）。

本文件钉三件事：

① 下行 `order.update`（状态 / 发单价共用一行）在**同一个 pending 窗口**里被刷成最新那一份，
   ⛔ 不新开一行；
② 上行 `order.update` 只回传状态、`target_ref` 用**上家的单号**；没有生效上游绑定时一行都不写；
③ `notify_order_status_changed` 按当前状态决定发哪个方向（`UNASSIGNED` ⇒ 一个方向都不发）。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里，普通 `TestCase` 的
search_path 里没有 `business_*` 表（照既有上下游用例的先例）。
⚠️ 夹具值**全部是合成值**（`SYN-P3-*` / `UP-1` / `*.test.invalid`），⛔ 不含真实租户数据。

⚠️ **与任务书的偏差（执行期更正）**：任务书 Step 1 的
`test_notify_only_enqueues_for_the_ruled_statuses` 用 `up.assert_not_called()` 钉「非触发态时
漏斗**连上游入口都不调**」，而任务书 Step 3 的漏斗实现**无条件调** `enqueue_upstream_status`
（把状态判定留给那个函数自己做）——两者不能同时成立。处理：把状态判定提到**漏斗里**
（两个方向各自按状态集决定调不调），漏斗 docstring 的「下行 / 上行 / 都不发」才真的是它自己的
判断；`enqueue_upstream_status` 自身的状态守卫**保留**（它是独立入口，直接调用方仍受它保护）。
两种写法的**可观测行为完全相同**（非触发态两条路都不写行），差别只是「谁做这个决定」。

⚠️ 本文件另有一条**任务书之外**的用例：`test_notify_fans_out_to_both_directions_for_a_middle_tenant`
——任务书的四条用例只钉了漏斗的**阴性**一支（不发），A→B→C 中间租户「两个方向都发」这条
承诺在任务书里没有任何判据，所以补一条打**数据库行**的正向用例。
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_dispatch import (
    enqueue_order_update,
    enqueue_upstream_status,
    notify_order_status_changed,
)
from apps.business.peer_sync import hash_token, new_token

#: 本文件的合成单号（`Order.order_no` 列宽 20，⛔ 不是真实的 20 位流水号）。
LOCAL_ORDER_NO = "SYN-P3-1"
UNLINKED_ORDER_NO = "SYN-P3-2"
#: 上家那一侧的单号（`PeerOrderLink.peer_order_no`，上行事件的 `target_ref`）。
PEER_ORDER_NO = "UP-1"


class _P3EnqueueFixture(TenantTestCase):
    """状态漏斗这一层的共用夹具（两租户建法照既有上下游用例，值全合成）。

    四个 `self.*` 各自对应漏斗的一支：
    - `self.bound_account` + `self.binding`：一条**生效**的下游绑定 ⇒ 下行事件写得出来；
    - `self.order` 的 **MAIN 绩效行 `user` 就是 `bound_account`** ⇒ 漏斗的下行分支取得到接手人；
    - `self.link`：把 `self.order` 链到一条**启用且未软删**的上游绑定 ⇒ 上行事件写得出来；
    - `self.unlinked_order`：**没有**上游链的单（状态取一个**在触发集里**的值，好让
      「一行都不写」真的是*没有链*造成的，而不是被状态集提前挡掉）。

    ⚠️ `PeerDownstreamBinding` 的 `account` / `peer_base_url` / `token_in_hash` / `token_out`
    与 `PeerUpstreamBinding` 的 `peer_name` / `peer_base_url` / `peer_account` /
    `token_in_hash` / `token_out` / `local_base_url` / `bound_by` **全部非空**
    ⇒ 少给一个就 `IntegrityError`，夹具按字段形状给全。
    ⚠️ 令牌两列按字段口径给：对手方给我的只存哈希、我发给对手方的存明文（⛔ 不两列都塞明文）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="上下游P3入队夹具游戏")

        # 下游绑定 + 它的账号（这个账号就是漏斗要投递的人）。
        self.bound_account = get_user_model().objects.create_user(
            username="peer-p3-bound", password="pw-peer-p3"
        )
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.bound_account,
            peer_base_url="https://peer-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

        # 上游绑定 + 它的绑定操作人。
        self.operator = get_user_model().objects.create_user(
            username="peer-p3-operator", password="pw-peer-p3"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家甲",
            peer_base_url="https://peer-up.test.invalid",
            peer_account="peer-account-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-local.test.invalid",
            bound_by=self.operator,
        )

        # 本地下家单：MAIN 绩效行归 `bound_account`（漏斗下行分支的取人依据）。
        self.order = Order.objects.create(
            game=self.game,
            order_no=LOCAL_ORDER_NO,
            status=OrderStatus.UNASSIGNED,
            # 发单价给**非默认值**：`order_payload` 取错列时断言才分辨得出来。
            total_price=Decimal("100.00"),
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=self.bound_account,
            temp_amount=Decimal("20.00"),
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.upstream_binding,
            peer_order_no=PEER_ORDER_NO,
        )

        # 没有上游链的那张单：状态**刻意取触发集里的值**，否则「没写行」会因为状态被挡
        # 而不是因为没链（那样这条用例就证明不了链接查询）。
        self.unlinked_order = Order.objects.create(
            game=self.game,
            order_no=UNLINKED_ORDER_NO,
            status=OrderStatus.COMPLETED,
            total_price=Decimal("50.00"),
            hall_price=Decimal("40.00"),
        )


class OrderUpdateEnqueueTests(_P3EnqueueFixture):
    """`order.update` 的刷新语义 + 上行载荷 + 漏斗的方向选择。"""

    def test_a_status_change_refreshes_the_pending_update_row(self):
        """同一个 pending 窗口里先改价、再改状态 ⇒ 只留一行，且载荷是**后一次**的。"""
        first = enqueue_order_update(self.order, self.bound_account)
        self.order.status = "ISSUE"
        self.order.save(update_fields=["status"])
        second = enqueue_order_update(self.order, self.bound_account)
        self.assertEqual(first.pk, second.pk)
        rows = PeerSyncEvent.objects.filter(kind="order.update", target_ref=self.order.order_no)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().payload["status"], "ISSUE")
        self.assertEqual(rows.get().payload["hall_price"], str(self.order.hall_price))

    def test_upstream_status_is_mapped_and_only_carries_status(self):
        self.order.status = "COMPLETED"
        self.order.save(update_fields=["status"])
        event = enqueue_upstream_status(self.order)
        self.assertEqual(event.binding_role, "upstream")
        self.assertEqual(event.kind, "order.update")
        self.assertEqual(event.target_ref, self.link.peer_order_no)   # 上家的单号
        self.assertEqual(event.payload, {"status": "PENDING_REVIEW"})
        self.assertEqual(event.meta["occurred_at"], self.order.updated_at.isoformat())

    def test_upstream_without_a_link_writes_nothing(self):
        self.assertIsNone(enqueue_upstream_status(self.unlinked_order))

    def test_notify_only_enqueues_for_the_ruled_statuses(self):
        """`UNASSIGNED` 既不在下行集也不在上行集 ⇒ 一行都不写。"""
        self.order.status = "UNASSIGNED"
        self.order.save(update_fields=["status"])
        with mock.patch("apps.business.peer_dispatch.enqueue_order_update") as down, \
             mock.patch("apps.business.peer_dispatch.enqueue_upstream_status") as up:
            notify_order_status_changed(self.order)
        down.assert_not_called()
        up.assert_not_called()

    def test_notify_fans_out_to_both_directions_for_a_middle_tenant(self):
        """⭐ `ISSUE` 同时在下行集与上行集 ⇒ 两个方向**各写一行**（A→B→C 的中间租户）。

        判别力：漏斗只做下行或只做上行 ⇒ 下面 `count() == 2` 或其中一条 `get()` 立刻红。
        ⚠️ 判据打在**数据库行**上（E-0009 / E-0024），⛔ 不是断言调用了哪个函数。
        """
        self.order.status = "ISSUE"
        self.order.save(update_fields=["status"])

        notify_order_status_changed(self.order)

        self.assertEqual(PeerSyncEvent.objects.count(), 2)
        down = PeerSyncEvent.objects.get(binding_role="downstream")
        self.assertEqual(down.kind, "order.update")
        self.assertEqual(down.binding_id, str(self.binding.pk))
        self.assertEqual(down.target_ref, self.order.order_no)
        self.assertEqual(down.payload["status"], "ISSUE")
        up = PeerSyncEvent.objects.get(binding_role="upstream")
        self.assertEqual(up.kind, "order.update")
        self.assertEqual(up.binding_id, str(self.upstream_binding.pk))
        # 上行那一行的对象标识是**上家的单号**（⛔ 不是本地下家单号）。
        self.assertEqual(up.target_ref, PEER_ORDER_NO)
        self.assertEqual(up.payload, {"status": "ISSUE"})
