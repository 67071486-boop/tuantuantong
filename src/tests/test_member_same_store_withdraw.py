"""下家**同店兄弟绑定**不再误退单（2026-10-25 生产事故，真实单号见 E-0290）。

## 事故（kexing 租户）

同一家下家鸡蛋铺有**两条绑定**：

| 绑定 | 对接方式 | 属于谁 | 当时状态 | 真的在推单？ |
|---|---|---|---|---|
| ①式（账号 + 密码） | `password` | 管理员（user 1） | **启用** | ✅ 是 |
| ②式（官方 API） | `openapi` | 下家那边的系统账号（user 2） | **停用** | ❌ 否 |

客服把单指派给「该店②式那条绑定的账号」（user 2）之后，旧判据只认**同一个绑定**
（`_enabled_binding_of(taker)`）：接手人名下那一份**停用** ⇒ 判成「我方**别的**打手接走了」
⇒ 给①式那条排了 `member.withdraw` ⇒ 消费后把对方**正在做**的活单退了
（对方系统日志：该订单被改成「已退单」，原因是「订单已被发单方打手接取」）。

## 修法的口径（spec §5.1 / §5.3：指派给他 ⇒ **只录单、对方大厅那张保持原样**）

1. 判据从「**同一个绑定**」放宽成「**同一家店**」：
   `jidanpu_member.downstream_addresses_of_taker(taker_id)` = 该用户名下**所有**绑定
   （⛔ 不筛 `enabled`）的下家地址集合；空 / 查不到 ⇒ 空集；
2. 排队侧三处（`queue_assign` / `queue_claimed` / `queue_withdraw` + 对账的
   `to_withdraw`）都按「同地址」判；
3. **消费侧兜底（承重）**：`_consume_withdraw` 在真正发上游**之前**再判一次 ⇒
   **已经排好的旧事件**（本事故那条就是）也挡得住，终态 `skipped` + 人话，
   `link.withdrawn_at` 不动（保持可再推）。

⚠️ 判据**不许放宽成「永不退单」**：接手人是个**没有任何绑定**的外人、
或者他的绑定在**另一家店**时，退单照排、消费照发（本文件的 `OutsiderControlTests`、
`SameStoreQueueWithdrawTests`、`SameStoreReconcileTests` 三处对照钉住这一点）。

## 同一个文件的第二件事：**推单归属拆开**（业务方B 档）

同一家店同时有①式（账号 + 密码）与②式（官方 API）两条**都启用**时，**推单只排给①式那条**，
②式那条**永不**成为推单目标（它只收回调）；只有该地址**没有启用中的**①式时它才按原样推
（「纯②式店」照旧可用）。判据唯一出处 = `jidanpu_member._push_targets_for_shop`，
四个落点（`owners_of` / `_assign_target_of` / `_push_evidence_ids` / 对账的候选）
都只过它那一处 —— 用例在文件末尾的 `SameStorePushOwnershipTests` 与
`SameStorePushReconcileTests`。

夹具一律**合成值**（E-0077）：`same-store-*` / `https://jidanpu-same.test.invalid` /
`pw-canary`。⛔ 零真实网络：上游出口用桩（打在**被查找的模块** `apps.business.jidanpu`
上，E-0029），并断言**一次都没被调用**。
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu, jidanpu_member
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
)

#: 三个 kind 的**字面值**（写方契约；⛔ 不从被测模块派生，E-0024）。
KIND_LAUNCH = "member.launch"
KIND_ASSIGN = "member.assign"
KIND_WITHDRAW = "member.withdraw"

#: 同一家下家店（两条绑定共用它）与**另一家**店（对照支）。
STORE_URL = "https://jidanpu-same.test.invalid"
OTHER_STORE_URL = "https://jidanpu-other.test.invalid"

ORDER_NO = "20261025000000009001"
JID = "JDP-000000000000009001"

#: 退单回读要看到的标签（`docs/JIDANPU-API.md` 的状态表；字面值）。
LABEL_REFUNDED = "已退单"

#: 消费侧兜底那句人话（**逐字**；⛔ 不写常量名——自指断言在被断言的常量改坏时不会红，E-0024）。
SAME_STORE_SKIP_REASON = "接手人是这家下家的账号，撤回作废（单还在下家做）"


class _SameStoreFixture(TenantTestCase):
    """同一家店两条绑定（①式启用 / ②式停用）＋ 一张已推出去的单。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="同店退单夹具游戏")
        # ①式：管理员，**启用**，真正在推单的那一份。
        self.owner = self._user("same-store-owner")
        self.binding = self._binding(self.owner, enabled=True)
        # ②式：下家那边的系统账号，**当时停用**（事故现场）。
        self.store_account = self._user("same-store-account")
        self.sibling = self._binding(self.store_account, enabled=False)

        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("50.00"),
            hall_price=Decimal("12.34"),
            project_text="合成项目甲",
        )
        # 前置哨兵：这张单**真的**推到过那家店（链路 = 已录到对方系统）。
        self.link = JidanpuMemberOrderLink.objects.create(
            binding=self.binding,
            order=self.order,
            jidanpu_order_id=JID,
            in_hall=False,
        )

    # ── 夹具工具 ─────────────────────────────────────────────────────────
    def _user(self, username):
        return get_user_model().objects.create_user(username=username, password="pw-canary")

    def _binding(self, user, *, enabled, base_url=STORE_URL):
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=base_url,
            account=f"acc-{user.username}",
            password="pw-canary",
            source_name="团团通系统",
            enabled=enabled,
        )

    def _take(self, user):
        """把这张单的 MAIN 接单人写成 `user`（我方「谁接了」= MAIN 绩效行的 `user`）。"""
        return Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=user,
            temp_amount=Decimal("20.00"),
        )

    def _events(self, kind):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=kind, target_ref=self.order.order_no
        )

    def _binding_ids(self, kind):
        return [
            row.payload["member_binding_id"] for row in self._events(kind).order_by("id")
        ]


# ══ ① 本事故复现（排队侧）：接手人是本店账号 ⇒ 零 withdraw ═════════════════════


class SameStoreClaimTests(_SameStoreFixture):
    def test_claiming_by_the_store_account_queues_no_withdraw_at_all(self):
        """⭐⭐ **本事故的复现**：接手人 user 2（②式停用）在我方接了单 ⇒ **零 withdraw**。

        旧实现：`taken = {taker_binding.pk}`，而 user 2 名下那份**停用**
        ⇒ `_enabled_binding_of` 回 `None` ⇒ `taken` 是空集 ⇒ ①式那条收到
        `member.withdraw` ⇒ 消费后把对方正在做的活单退了（事故本体）。

        判别力：把 `taken` 退回「只排除接手人自己那一份」⇒ 本用例红（退单清单里有 1 条）；
        反过来把规则放宽成「永不退单」⇒ 本文件 `OutsiderControlTests` 红。
        """
        self._take(self.store_account)

        self.assertEqual(
            jidanpu_member.queue_claimed(self.order, taker_id=self.store_account.pk), 0
        )

        self.assertEqual(self._binding_ids(KIND_WITHDRAW), [])
        # ⛔ 更不许把 assign 排到那条**停用**的兄弟绑定上（spec §8.4：停用的不跑对账、
        #    消费者也不该替它发上游）。⚠️ 不钉「一条 assign 都没有」：将来若把
        #    `queue_assign` 的「排到启用兄弟绑定」也接到这一档，那是**改进**、不是回归。
        self.assertNotIn(self.sibling.pk, self._binding_ids(KIND_ASSIGN))


# ══ ② 指派（排队侧）：录单要排到**启用**的那条兄弟绑定上 ════════════════════════


class SameStoreAssignTests(_SameStoreFixture):
    def test_assigning_to_the_store_account_queues_on_the_enabled_sibling(self):
        """客服指派给 user 2（②式停用）⇒ `member.assign` 排在**①式启用**那条上。

        ⛔ 不许排到停用那条：停用的绑定不跑对账、消费者也不该替它发上游
        （spec §8.4「停用停止快路与对账」）。

        判别力：把这条排除掉（退回只认「接手人自己那一份」）⇒ 一条 assign 都没有，红；
        排到停用那条 ⇒ 绑定 id 断言红。
        """
        self.assertEqual(
            jidanpu_member.queue_assign(self.order, taker_id=self.store_account.pk), 1
        )

        self.assertEqual(self._binding_ids(KIND_ASSIGN), [self.binding.pk])
        self.assertEqual(self._binding_ids(KIND_WITHDRAW), [])


# ══ ③ 消费侧兜底（承重）：**已经排好**的旧事件也不许发上游 ═════════════════════


class SameStoreWithdrawConsumeTests(_SameStoreFixture):
    def test_a_pending_withdraw_against_the_store_account_is_a_terminal_skip(self):
        """⭐⭐ **承重的那一道**：本事故那条台账**已经排好了**（pending），消费时也必须挡住。

        事故现场：事件是旧代码排的（`payload.member_binding_id` = ①式那条），
        排好之后才发版 ⇒ 只在排队侧修**挡不住它**。

        判据三条一起钉：
        ① 终态 `skipped` + 那句人话（⛔ 不是 failed、也不重试）；
        ② **上游一次都没打**（两个出口都 `assert_not_called`）；
        ③ `link.withdrawn_at` **保持空**（这张单还能被重新推 / 继续对账，⛔ 不被记成「退成过」）。
        """
        self._take(self.store_account)
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=KIND_WITHDRAW, rearm=True
        )

        with (
            mock.patch.object(jidanpu, "set_order_status") as refund,
            mock.patch.object(
                jidanpu,
                "fetch_order_by_key1",
                return_value={"orderId": JID, "orderSta": LABEL_REFUNDED},
            ) as readback,
        ):
            outcome = jidanpu_member.consume_event(event, client=object())

        event.refresh_from_db()
        self.assertEqual(outcome, "skipped", f"台账原因是「{event.reason}」")
        refund.assert_not_called()
        readback.assert_not_called()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, SAME_STORE_SKIP_REASON)
        self.assertEqual(event.attempts, 0)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.withdrawn_at)
        self.assertFalse(self.link.in_hall)  # 链路原样（构造物：推送时就已不在大厅）


# ══ ④ 对照：接手人是个**没有任何绑定**的外人 ⇒ 照旧退单、照旧真发 ═══════════════


class OutsiderControlTests(_SameStoreFixture):
    def test_an_unbound_outsider_still_gets_the_withdraw_and_it_really_ships(self):
        """⛔ **别把规则放宽成「永不退单」**：外人接了 ⇒ 排 withdraw **且**消费真的发上游。

        这条同时覆盖两半（排队 + 消费），⛔ 不拆成两条会被漏跑的形态：
        - 排队侧：`queue_claimed(taker=外人)` ⇒ ①式那条收到 `member.withdraw`；
        - 消费侧：回读确认「已退单」⇒ `done`，上游写接口真的被调用（带逐字原因）。
        """
        outsider = self._user("same-store-outsider")  # 一个绑定都没有
        self._take(outsider)

        self.assertEqual(jidanpu_member.queue_claimed(self.order, taker_id=outsider.pk), 1)
        self.assertEqual(self._binding_ids(KIND_WITHDRAW), [self.binding.pk])
        event = self._events(KIND_WITHDRAW).get()

        with (
            mock.patch.object(jidanpu, "set_order_status") as refund,
            mock.patch.object(
                jidanpu,
                "fetch_order_by_key1",
                return_value={"orderId": JID, "orderSta": LABEL_REFUNDED},
            ) as readback,
        ):
            outcome = jidanpu_member.consume_event(event, client=object())

        self.assertEqual(outcome, "done")
        refund.assert_called_once()
        self.assertEqual(
            refund.call_args.kwargs["status_label"], LABEL_REFUNDED,
            "退单必须写「已退单」这一档",
        )
        self.assertEqual(
            refund.call_args.kwargs["remark"], "订单已被发单方打手接取",
            "退单原因必须逐字沿用既有那句（spec §13-T6）",
        )
        readback.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.withdrawn_at)

    def test_a_bound_outsider_on_another_store_still_gets_the_withdraw(self):
        """对照的第二档：**另一家店**的打手接了 ⇒ 这家店那张单照样退（⛔ 只见「同店」才放过）。"""
        other_store_taker = self._user("same-store-other-taker")
        self._binding(other_store_taker, enabled=True, base_url=OTHER_STORE_URL)
        self._take(other_store_taker)

        jidanpu_member.queue_claimed(self.order, taker_id=other_store_taker.pk)

        self.assertEqual(self._binding_ids(KIND_WITHDRAW), [self.binding.pk])


# ══ ⑤ 触发点那一半：撤回 / 删除 ⇒ 也不许退「自己人正在做」的单 ═════════════════


class SameStoreQueueWithdrawTests(_SameStoreFixture):
    def test_queue_withdraw_skips_the_store_the_taker_belongs_to(self):
        """`queue_withdraw`（撤回 / 删除 / 回池那一档）同样按「同店」放过。

        判别力：只在 `queue_claimed` 修、这一处不修 ⇒ 本用例红（删除一张**本店账号在做**的单
        会去退对方正在做的那张）。
        """
        self._take(self.store_account)

        self.assertEqual(jidanpu_member.queue_withdraw(self.order), 0)

        self.assertEqual(self._binding_ids(KIND_WITHDRAW), [])

    def test_queue_withdraw_still_fires_when_the_taker_is_unbound(self):
        """对照：**没有接手人**（地址集为空）⇒ 照旧排（这条防「顺手把 queue_withdraw 一刀切」）。"""
        order_without_taker = Order.objects.create(
            game=self.game,
            order_no="20261025000000009002",
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("50.00"),
            hall_price=Decimal("12.34"),
            project_text="合成项目乙",
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=order_without_taker,
            jidanpu_order_id="JDP-000000000000009002", in_hall=False,
        )

        self.assertEqual(jidanpu_member.queue_withdraw(order_without_taker), 1)
        row = JidanpuSyncEvent.objects.get(
            direction="out", kind=KIND_WITHDRAW, target_ref=order_without_taker.order_no
        )
        self.assertEqual(row.payload["member_binding_id"], self.binding.pk)


# ══ ⑥ 对账那一半：`to_withdraw` 也按「同店」放过 ═════════════════════════════


class SameStoreReconcileTests(_SameStoreFixture):
    def test_reconcile_does_not_queue_a_withdraw_for_the_store_the_taker_belongs_to(self):
        """对账的 `to_withdraw` 与 `queue_withdraw` 同一条口径。

        构造：接单人 = 本店账号、我方这张单已退回池内（`absent` 形态）、本地还有链路
        ⇒ 旧实现每 180 秒排一条退单；新口径**一条都不排**。

        判别力：只在消费侧兜底、对账不修 ⇒ 每轮又排一条（终态 `skipped` 挡得住重试、
        ⛔ 挡不住重排，spec §5.2 的收口同族）。
        """
        self._take(self.store_account)
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])

        result = jidanpu_member.reconcile(self.binding, now=timezone.now())

        self.assertEqual(result.to_withdraw, ())

    def test_reconcile_still_queues_a_withdraw_for_an_unbound_taker(self):
        """对照：外人接了 ⇒ 对账照旧排退单。"""
        outsider = self._user("same-store-outsider-2")
        self._take(outsider)
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])

        result = jidanpu_member.reconcile(self.binding, now=timezone.now())

        self.assertEqual([item.order.pk for item in result.to_withdraw], [self.order.pk])


# ══ ⑦ 推单归属拆开（业务方B 档）：②式那条**永不**成为推单目标 ═════════════
#
# 现场（E-0290 第 ② 条）：同一家店两条**都启用**，地址级去重取 `id` 最小 ⇒ 启用②式
# （官方 API）那条之后，新单的 `member.launch` 排给了它，而它的发单开关是关的 ⇒ 单卡住不推。
# 口径（业务方原话「推单只排给账号密码那条」）：**②式那条只收回调、永不推单**；
# 只有该地址**没有启用中的①式**时，②式那条才按原样推（「纯②式店」照旧可用）。
# 判据唯一出处 = `jidanpu_member._push_targets_for_shop`：`queue_hall_launch`（经 `owners_of`）、
# `_assign_target_of`、`_push_evidence_ids`（退单清单）与对账的候选**都只过它那一处**。


class _TwoChannelFixture(TenantTestCase):
    """同一家店两条**都启用**的绑定：②式（官方 API，**先建**）+ ①式（账号 + 密码）。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="双通道推单夹具游戏")
        # ⚠️ ②式**先建**：事故现场正是「地址级去重取 id 最小 ⇒ 给了②式那条」。
        self.api_owner = self._user("two-channel-api")
        self.api = self._api_binding(self.api_owner)
        self.password_owner = self._user("two-channel-password")
        self.password = self._binding(self.password_owner)
        self.assertLess(self.api.pk, self.password.pk, "自证夹具：②式那条 id 更小")

    # ── 夹具工具 ─────────────────────────────────────────────────────────
    def _user(self, username):
        user = get_user_model().objects.create_user(username=username, password="pw-canary")
        profile = StaffProfile.objects.create(user=user)
        profile.games.add(self.game)
        return user

    def _binding(self, user, *, enabled=True, base_url=STORE_URL):
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=base_url,
            account=f"acc-{user.username}",
            password="pw-canary",
            source_name="团团通系统",
            enabled=enabled,
        )

    def _api_binding(self, user, *, enabled=True, base_url=STORE_URL):
        """一行**②式**绑定（账号密码留空 —— 界面上②式的真实形态，E-0202）。"""
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=base_url,
            account="",
            password="",
            mode="openapi",
            client_id="cid-synthetic",
            client_secret="secret-synthetic",
            enabled=enabled,
        )

    def _hall_order(self, no="20261025000000009101"):
        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("50.00"),
            hall_price=Decimal("12.34"),
            project_text="合成项目甲",
        )

    def _events(self, order, kind):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=kind, target_ref=order.order_no
        )

    def _binding_ids(self, order, kind):
        return [
            row.payload["member_binding_id"] for row in self._events(order, kind).order_by("id")
        ]


class SameStorePushOwnershipTests(_TwoChannelFixture):
    """三个选择点（抛大厅 / 指派 / 撤回）都只排给①式那条。"""

    def test_the_hall_launch_goes_to_the_password_binding_only(self):
        """⭐ **选择点①**（`queue_hall_launch` → `owners_of`）：两条都启用 ⇒ 只排①式那条。

        判别力：去掉 `owners_of` 里那句 `_only_push_targets` ⇒ `dedupe_owner` 取 id 最小的
        ②式那条 ⇒ 本用例红（那正是 2026-10-25 的事故形态）。
        """
        order = self._hall_order()

        self.assertEqual(jidanpu_member.queue_hall_launch(order), 1)

        self.assertEqual(self._binding_ids(order, KIND_LAUNCH), [self.password.pk])

    def test_disabling_the_password_binding_hands_the_launch_back_to_the_api_row(self):
        """对照：**该地址没有启用中的①式** ⇒ ②式那条按原样推（「纯②式店」不能被打死）。

        判别力：把判据写成「②式一律不推」⇒ 本用例红。
        """
        self.password.enabled = False
        self.password.save(update_fields=["enabled"])
        order = self._hall_order("20261025000000009102")

        self.assertEqual(jidanpu_member.queue_hall_launch(order), 1)

        self.assertEqual(self._binding_ids(order, KIND_LAUNCH), [self.api.pk])

    def test_the_assign_goes_to_the_password_binding_even_when_the_taker_is_the_api_account(self):
        """⭐ **选择点②**（`_assign_target_of`）：指派给②式那条的主人 ⇒ 录单排给①式那条。

        判别力：去掉 `_assign_target_of` 里那道判据 ⇒ 排给②式那条 ⇒ 本用例红。
        """
        order = self._hall_order("20261025000000009103")

        self.assertEqual(jidanpu_member.queue_assign(order, taker_id=self.api_owner.pk), 1)

        self.assertEqual(self._binding_ids(order, KIND_ASSIGN), [self.password.pk])

    def test_the_withdraw_never_targets_the_api_row(self):
        """⭐ **选择点③**（`_push_evidence_ids`）：撤回**只**清①式那条，②式那条不算「推过」。

        构造：**两条**绑定都有这张单的链路（②式那条曾经也推过 ⇒ 若按旧口径，它会被排退单）。
        判别力：去掉 `_push_evidence_ids` 里那句过滤 ⇒ 本用例看到 2 条 withdraw（含②式）。
        """
        order = self._hall_order("20261025000000009104")
        JidanpuMemberOrderLink.objects.create(
            binding=self.password, order=order,
            jidanpu_order_id="JDP-000000000000009104", in_hall=False,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.api, order=order,
            jidanpu_order_id="JDP-000000000000009114", in_hall=False,
        )

        self.assertEqual(jidanpu_member.queue_withdraw(order), 1)

        self.assertEqual(self._binding_ids(order, KIND_WITHDRAW), [self.password.pk])

    def test_the_api_row_is_back_in_the_withdraw_list_when_no_password_row_is_enabled(self):
        """对照（同上那条判据的另一面）：①式停用 ⇒ ②式那条回到「推过」的清单里。

        判别力：把判据写成「②式一律剔除」⇒ 本用例红（那会让②式那条推出去的单永远退不掉）。
        """
        self.password.enabled = False
        self.password.save(update_fields=["enabled"])
        order = self._hall_order("20261025000000009105")
        JidanpuMemberOrderLink.objects.create(
            binding=self.api, order=order,
            jidanpu_order_id="JDP-000000000000009115", in_hall=False,
        )

        jidanpu_member.queue_withdraw(order)

        self.assertIn(self.api.pk, self._binding_ids(order, KIND_WITHDRAW))

    def test_the_claimed_path_only_withdraws_the_password_row(self):
        """同一条判据的另一个调用口：**别的打手接了** ⇒ 只给①式那条排退单。

        ⚠️ 与 `queue_withdraw` 是**两个**调用点（`queue_claimed` / `queue_withdraw`），
        判据都在 `_push_evidence_ids` 里 ⇒ 这一条钉的是「没有哪个调用点漏过去」。
        """
        order = self._hall_order("20261025000000009106")
        JidanpuMemberOrderLink.objects.create(
            binding=self.password, order=order,
            jidanpu_order_id="JDP-000000000000009106", in_hall=False,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.api, order=order,
            jidanpu_order_id="JDP-000000000000009116", in_hall=False,
        )
        outsider = self._user("two-channel-outsider")
        # 接手人**不是**这家店的人（否则整店不撤 —— 那是另一条修法，见本文件前面几节）。
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, user=outsider,
            temp_amount=Decimal("20.00"),
        )

        jidanpu_member.queue_claimed(order, taker_id=outsider.pk)

        self.assertEqual(self._binding_ids(order, KIND_WITHDRAW), [self.password.pk])


class SameStorePushReconcileTests(_TwoChannelFixture):
    """对账那一侧的「地址级去重」同样过判据（⛔ 每 180 秒又把单排给②式那条）。"""

    def test_the_api_row_never_reconciles_into_a_launch(self):
        """⭐ ②式那条跑自己那一轮 ⇒ `to_launch` 一张都不排；**①式那条照排**（对照）。

        判别力：去掉 `reconcile` 里 `_only_push_targets` + `push_ok` 那两处 ⇒ ②式那条
        每一轮都排一条 `member.launch`（单永远卡住）。
        """
        order = self._hall_order("20261025000000009107")

        api_round = jidanpu_member.reconcile(self.api, now=timezone.now())
        self.assertEqual(api_round.to_launch, ())

        password_round = jidanpu_member.reconcile(self.password, now=timezone.now())
        self.assertEqual([item.order.pk for item in password_round.to_launch], [order.pk])
