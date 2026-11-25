"""P4-7：结账端点的六步、批量部分失败、`1011`、幂等、留痕与事务原子性。

依据：spec §4.3（六步，顺序不可改）· §7 / §7.1（入参校验）· §7.2（留痕落库）·
§8（事务边界）· §9 #3/#4/#9/#14/#15/#16/#17 · §10 #18（分组求和，**拟定解读**）。

七条纪律（每条都有会红的用例）：

1. **六步同事务**：第 3 步 fail-closed 逐 id 判定（**不是**总数比对）、第 4 步 `1011`
   **必须早于**第 5 步标记结账——见 `apps/business/settlement.py` 的文档串。
2. **绝不做静默部分成功**：一条不合格 ⇒ 整批回滚（§9 #4 ②③）。
   ⚠️ **「未审核」不再是不合格理由**（D-0071）：结账不再按 `review_status` 过滤，
   §9 #4 ③ 那条「混一条未审核 ⇒ 整批回滚」已按新口径改成「**照样结掉**」。
   ⚠️ **但「绩效必须已生效」由两条真实判据承担**：
   `order__status=已完成` 与 `user__isnull=False`（订单未完成 / 还没有接单人 ⇒ `1006`）。
   这四条用例在 `SettleSixStepsTests` 里（两条拒绝侧 + 一条正向 + 一条「重开」边界）。
3. **可结账集合 = 绩效行 ∪ 其未结账调整记录**：已结账的绩效被追加的新调整记录
   **要能在下一次结掉**（§9 #9，spec §17.1 #3）。
4. **空列表 ⇒ `1000`**：第一版用 `updated == len(set(ids))`，而 `0 == len(set([]))`
   恒成立 ⇒ 空列表会被判成「成功结账 0 笔」（spec §17.1 #9）。
5. **重复提交 ⇒ `1006`**，不是 200（财务双击第二次要明确看到「这批已经结过了」）。
6. **留痕断言打在新表 `OperationLog` 的数据库行上**（E-0009；本项目**没有** `LOGGING` 配置）：
   ⛔ 本文件**不写**任何日志断言（判据：全文件搜「assert」紧跟「Logs」的串为 0）。
   ⭐ M3a：P4 的结账写入点已从旧 `AuditLog` 迁到
   `apps/audit/log.py::record(...)`——本文件的留痕断言随之搬到新表，并额外断言
   **旧表零新增行**（旧出口退役的判据，见 spec §16.1）。
7. **期望值一律写字面值**（E-0024）：业务码写 `1006` / `1011` / `1000`，状态写 `"已结账"`——
   不引 `ErrorCode.*` / `SettleStatus.*` 这类被测模块导出的常量（自指断言不会红）。
8. ⭐ **第 5 步 `update()` 的受影响行数必须校验**（乐观并发）：行数与「将要结的行数」不符
   ⇒ 整批回滚 + `1006` + **零审计行**。用确定性桩（`QuerySet.update` 谎报 0 行）复现
   并发下 T2 的那次 UPDATE，**不依赖真并发**（见 `_rowcount_stub`）。

⚠️ **越权用例必须用非管理员账号**：`has_node_now` 对 `is_superuser` 有短路（spec §6 末条）；
业务逻辑用例用管理员只是为了**跳过建角色目录**，闸门本身由 `SettlePermissionTests`
用普通账号双向钉住（Ruling T：本端点是全仓 `has_node` 的第一个业务消费方）。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import Group, User
from django.db.models.query import QuerySet
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    AuditLog,
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    PerformanceSettleRequest,
    ReviewStatus,
    SettleStatus,
)
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: spec §4.3 的最终路径（**没有**尾斜杠；客户端 POST 的正是这一串）。
SETTLE_URL = "/api/business/settlements/settle"
#: 结账闸门的节点 code（实测 `apps/rbac/catalog.py` 对标 `84 更改结算状态`）。
NODE_CODE = "finance.change_settlement_status"


def _rowcount_stub(*, mismatching_model, calls):
    """确定性桩：把 `QuerySet.update` 换成「对 `mismatching_model` **谎报 0 行**」。

    模拟并发下 T2 的那次 UPDATE——谓词在库里重判、命中 0 行（评审给出的机制，
    **不需要真起两个线程**）。其它模型的 `update` 照常走真实实现，所以请求里
    的会话写入等无关调用不受影响。

    ⚠️ `mock.patch` 打在**被查找的位置**（E-0029）：`rows.update(...)` 是在实例上查找、
    命中 `QuerySet` 这个类的方法，所以补丁目标是 `django.db.models.query.QuerySet.update`。

    `calls` 由调用方传入并断言（桩**自证接上**：至少有一次调用命中了目标模型）。
    """
    real_update = QuerySet.update

    def fake_update(queryset, **kwargs):
        calls.append(queryset.model)
        if queryset.model is mismatching_model:
            return 0
        return real_update(queryset, **kwargs)

    return fake_update


class _SettleFixture(TenantTestCase):
    """建单 / 建绩效 / 建调整记录的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # 管理员登录：`has_node_now` 短路为真 ⇒ 业务逻辑用例不必先搭角色目录。
        self.finance = make_logged_in_client(username="finance-p4-7", is_superuser=True)
        self.taker_a = User.objects.create_user(username="taker-a")
        self.taker_b = User.objects.create_user(username="taker-b")
        self._order_seq = 0

    def _order(self, *, status=OrderStatus.COMPLETED, hall_price="100.00"):
        self._order_seq += 1
        return Order.objects.create(
            game=self.game,
            order_no=f"2026092000000{self._order_seq:07d}",
            status=status,
            hall_price=Decimal(hall_price),
        )

    def _perf(self, order, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            "review_status": ReviewStatus.APPROVED,
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _adj(
        self,
        performance,
        amount,
        *,
        settle=SettleStatus.UNSETTLED,
        kind=PerformanceAdjustment.Kind.MANUAL,
    ):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=kind,
            amount=Decimal(amount),
            reason="测试调整",
            operator="fin",
            settle_status=settle,
        )

    def _post(self, payload, client=None):
        return (client or self.finance).post(
            SETTLE_URL, payload, content_type="application/json"
        )


class SettleSixStepsTests(_SettleFixture):
    """六步的主路径与三条回滚路径（§9 #4 ①②③ / #9 / #14 / #15 / #17）。"""

    def test_fully_qualified_batch_settles_every_row(self):
        """§9 #4 ①：一批全合格 → 全变「已结账」，响应报出被结的 id。"""
        first = self._perf(self._order(), self.taker_a)
        second = self._perf(self._order(), self.taker_b)

        body = self._post({"performance_ids": [first.id, second.id]}).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            sorted(body["data"]["settled_performances"]), sorted([first.id, second.id])
        )
        self.assertEqual(body["data"]["settled_adjustments"], [])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.settle_status, "已结账")
        self.assertEqual(second.settle_status, "已结账")

    def test_a_settled_row_mixed_in_is_skipped_and_the_rest_still_settles(self):
        """§9 #4 ② **按 2026-10-03 的 Q3=A 改写**：混一条已结账 ⇒ 只结另一条，
        已结账那条进 `skipped_performances`（**不是**整批报错）。

        ⚠️ **改前 → 改后**：原用例叫 `test_a_settled_row_mixed_in_rolls_back_the_whole_batch`，
        断言 `code == 1006` + 整批没变 + 零审计行——那编码的是「已是目标状态 = 不合格」。
        业务方裁定 Q3=A（原话：「勾选未结算或者已结算的时候，如果勾选的订单已经是
        要更改的状态 直接跳过即可 不用报错」）⇒ 同一条输入现在**成功**且**如实报数**。
        判据一个字没放宽：好行真的被结（库里 `settle_status` + 审计行都断），
        被跳过那行的库内状态与 `updated_at` 都**不动**。
        ⚠️ 与它配对的是**真不合格**那一组（不存在 / 订单未完成 / 无归属人）：
        那些 id ⛔ 不进 `skipped_performances`，而是照旧让整批 `1006`（本类下面几条 + `SettleInputValidationTests`）。
        """
        good = self._perf(self._order(), self.taker_a)
        already = self._perf(
            self._order(), self.taker_b, settle_status=SettleStatus.SETTLED
        )
        already.refresh_from_db()
        untouched_at = already.updated_at

        body = self._post({"performance_ids": [good.id, already.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [good.id])
        self.assertEqual(body["data"]["settled_adjustments"], [])
        self.assertEqual(body["data"]["skipped_performances"], [already.id])
        good.refresh_from_db()
        already.refresh_from_db()
        self.assertEqual(good.settle_status, "已结账")
        self.assertEqual(already.settle_status, "已结账")
        # 跳过的行**一个字节都不动**：`updated_at` 是 `auto_now`，任何一次 `save()` 都会推走它。
        self.assertEqual(already.updated_at, untouched_at)
        # 审计只给**真被结**的那一条写（E-0009：打数据库行，不打断言 logger）。
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(), 1
        )

    def test_a_batch_of_settled_rows_is_skipped_whole_and_writes_nothing(self):
        """§3 T2.2 判据 ①：**全批都是已结账** ⇒ `code=0`、`skipped_performances` 含全部、
        库内一行未动、**零审计行**。

        ⚠️ 这条与上面那条（混选）分工不同：混选断的是「只结该结的」，这条断的是
        「一条都不该结时，**依然是成功**而不是报错」——业务方 Q3=A 的原话
        「全部已是该状态 ⇒『所选记录已是该状态，无需修改』」（前端的文案由它驱动）。
        """
        first = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        second = self._perf(
            self._order(), self.taker_b, settle_status=SettleStatus.SETTLED
        )
        first.refresh_from_db()
        second.refresh_from_db()
        first_at, second_at = first.updated_at, second.updated_at

        body = self._post({"performance_ids": [first.id, second.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [])
        self.assertEqual(body["data"]["settled_adjustments"], [])
        self.assertEqual(
            sorted(body["data"]["skipped_performances"]), sorted([first.id, second.id])
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.settle_status, "已结账")
        self.assertEqual(second.settle_status, "已结账")
        self.assertIsNone(first.settled_amount)     # 跳过的行连快照都不写
        self.assertEqual(first.updated_at, first_at)
        self.assertEqual(second.updated_at, second_at)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_already_settled_row_of_a_frozen_owner_is_skipped_not_rejected(self):
        """口径的**边界**（有意如此，⛔ 别当成漏判）：一条**已经结清**的行**不**看
        「归属人被冻结 / 归属人为空 / 订单未完成」这三项——本批对它是**空操作**（没有钱要发），
        那三项闸门是为「有钱要发」设的。

        判据逐字取自计划 §3 T2.2 的跳过定义（**可见 ∧ 已结账 ∧ 名下无未结账调整记录**），
        ⛔ 不额外加那三项。没有这条用例时，「顺手把冻结/归属人/订单状态加进跳过判据」
        这种改动**不会红**：本类上面几条都不覆盖这个组合（冻结用例是**未结账**的行）。
        """
        frozen = User.objects.create_user(username="taker-frozen-settled")
        profile, _ = StaffProfile.objects.get_or_create(user=frozen)
        profile.settle_forbidden = True
        profile.save(update_fields=["settle_forbidden"])
        perf = self._perf(
            self._order(), frozen, settle_status=SettleStatus.SETTLED
        )

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["skipped_performances"], [perf.id])
        self.assertEqual(body["data"]["settled_performances"], [])
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_truly_unqualified_id_mixed_in_still_rejects_the_whole_batch(self):
        """§3 T2.2 判据 ⑤：**真不合格**（这里是「不存在」）混在可结的行里 ⇒ 整批 `1006`、
        一行不写、**没有**「部分成功」。

        ⚠️ 这条是「跳过」这条新路径的**反面守门**：把跳过判据写成「凡是结不了的都跳过」
        （例如只看 `id not in row_ids`）时，本用例会红——那会把真失败伪装成成功。
        失败信封 `data` 是 `None`（⛔ 没有 `skipped_performances` 可以让人误读成「跳过了一部分」）。
        """
        good = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [good.id, 999999]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("999999", body["msg"])
        self.assertIsNone(body["data"], body)
        good.refresh_from_db()
        self.assertEqual(good.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_unapproved_row_is_settled_too(self):
        """⭐ §9 #4 ③ **按 D-0071 改写**（2026-09-22）：`review_status = 待审核` 的行
        **能**被结账，不再拖累整批。

        ⚠️ **改前 → 改后**：原用例叫 `test_an_unapproved_row_mixed_in_rolls_back_the_whole_batch`，
        断言 `code == 1006`、好行 `未结账`、零审计行——那编码的是「结账要求
        `review_status = 已通过`」这条**我们自加的、对标没有的**内控步骤（D-0071）。
        业务方裁定「对标没有审核，订单状态变成完成后绩效即生效」⇒
        `settlement.settle_performances` 第 2 步的 `review_status` 过滤已删除，
        所以**同一条输入现在必须成功结掉**。断言换成新口径，判据（「整批都结 / 都有审计行」）
        一个字没有放宽——它仍然逐行断两行的 `settle_status` 与审计行数。

        ⚠️ 与 `test_a_settled_row_mixed_in_is_skipped_and_the_rest_still_settles` 配对：
        那条钉的「已结账 ⇒ **跳过**（成功路径）」是 2026-10-03 的新口径
        （旧口径「已结账 ⇒ `1006` 整批回滚」已被 Q3=A 取代）；`settle_status` 仍是
        「真不合格 ⇒ 整批回滚」那条判据的状态维度（订单未完成 / 无归属人在本类下面两条）。
        """
        good = self._perf(self._order(), self.taker_a)
        pending = self._perf(
            self._order(), self.taker_b, review_status=ReviewStatus.PENDING
        )

        body = self._post({"performance_ids": [good.id, pending.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            sorted(body["data"]["settled_performances"]), sorted([good.id, pending.id])
        )
        good.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(good.settle_status, "已结账")
        self.assertEqual(pending.settle_status, "已结账")
        self.assertEqual(OperationLog.objects.count(), 2)

    # ── `review_status` 移除后的两条**真实判据** ──────────
    def test_a_row_of_an_unfinished_order_cannot_be_settled(self):
        """⭐ **C-1 判据 ①**：订单**还没完成** ⇒ 它的绩效结不了（`1006`、零审计、行没动）。

        ⚠️ **改前 → 改后**：删掉 `review_status=已通过` 之后、还没补这两条判据时，
        一张 `IN_PROGRESS`、有接单人、`review_status=None`、`bonus_amount=0.00` 的行会被
        **结掉**；随后 `/submit` 回写 `bonus_amount=30.00`（它**不看** `settle_status`）⇒
        再结回 `1006`「已完全结清」⇒ **那 30 元永久结不了**（评审实测的失效路径）。
        现在第 2 步加了 `order__status=OrderStatus.COMPLETED`，本用例必须红→绿。

        依据：D-0071「订单完成 ⇒ 绩效生效」+ `settlement.py` docstring 第一句
        「**已完成订单产生的**绩效」。
        """
        perf = self._perf(self._order(status=OrderStatus.IN_PROGRESS), self.taker_a)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("订单未完成", body["msg"])
        self.assertIn(OrderStatus.IN_PROGRESS, body["msg"])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_row_without_an_owner_cannot_be_settled(self):
        """⭐ **C-1 判据 ②**：**没有归属人**的绩效结不了（D-0069）。

        ⚠️ 前端那道闸门只是**体验层**；后端必须自己守住（「判据留在后端一处」）。
        判据是 `user__isnull=False`（第 2 步）⇒ 期望 `1006`、msg 说清是「还没有接单人」、
        行没动、零审计行。
        """
        perf = self._perf(self._order(), None, review_status=None)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("接单人", body["msg"])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_completed_order_row_with_an_owner_is_settled(self):
        """⭐ **C-1 判据 ③（正向）**：已完成订单 + 有归属人 ⇒ **结得掉**。

        防「把判据收得过头」：只钉两条拒绝侧的话，把过滤写成恒假（`id__in=[]` 之类的
        「什么都结不了」）也会全绿。
        """
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [perf.id])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_a_row_of_a_reopened_completed_order_cannot_be_settled(self):
        """⚠️ **C-1 的已知边界（如实登记，不放宽）**：订单先完成、之后又被**重开**
        （`COMPLETED → 问题单/已暂停/已退单`）⇒ 它的绩效按新判据**结不了**了。

        改前不按订单状态过滤 ⇒ 这种行**能**结。现在的口径是「重开 = 这单这轮不成立」，
        所以它的绩效回到「未生效」、不该结。**代价**（写在报告 §7）：若这单一直不再完成，
        这行会**永久停在未结账**（钱不发）——要发就得先把订单再改成已完成。
        ⚠️ 这是**有意的收紧**，不是漏判；⛔ 不要为了让它可结而删掉 `order__status` 条件
        （那会把 C-1 的 30 元路径放回来）。
        """
        perf = self._perf(self._order(), self.taker_a)
        Order.objects.filter(pk=perf.order_id).update(status=OrderStatus.ISSUE)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("订单未完成", body["msg"])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_new_adjustment_on_a_settled_performance_is_settled_next_time(self):
        """§9 #9：绩效已结账 → 再产生新调整记录 → 下一次结账把它结掉。

        断言**绩效行 `settle_status` 未变**（仍 `已结账`）而**调整记录变了**——
        这正是 spec §17.1 #3 修掉的坏账场景（第一版的 fail-closed 判据表达不了它）。

        ⭐ Q3=A）补的**反面对照**：这一支**不算「已是目标状态」**
        （它名下还有账没结）⇒ `skipped_performances` 必须是空的。
        没有这条，把「已结账且无未结账调整」的判据写成「只看已结账」也能让本用例绿，
        而那条坏账出口会被**静默跳过**（那条 `-5.00` 永不进任何实结金额）。
        """
        settled = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        adjustment = self._adj(settled, "-5.00")

        body = self._post({"performance_ids": [settled.id]}).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["settled_performances"], [])
        self.assertEqual(body["data"]["settled_adjustments"], [adjustment.id])
        self.assertEqual(body["data"]["skipped_performances"], [])
        settled.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(settled.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)

    def test_a_loss_making_batch_is_rejected_with_1011(self):
        """§9 #15：亏损单（退款额 × 率 > 提成额）→ `1011`，两处都不落库。"""
        # 应付净额 = 12.00 − 0.00 + (−20.00) = −8.00
        perf = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("12.00"),
            bonus_amount=Decimal("12.00"),
        )
        adjustment = self._adj(perf, "-20.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1011)
        # spec §9 前言：一律断言**具体十进制值**（E-0024：写字面值，不引被测常量）。
        self.assertIn("-8.00", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_profitable_batch_settles_normally(self):
        """§9 #15 正向对照：净额为正的**同一批**正常结账（防止「一律拒绝」假通过）。"""
        # 应付净额 = 12.00 − 0.00 + (−5.00) = 7.00
        perf = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("12.00"),
            bonus_amount=Decimal("12.00"),
        )
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "已结账")

    def test_an_empty_id_list_is_a_validation_failure_and_changes_nothing(self):
        """spec §17.1 #9：空列表 ⇒ `1000`，且一行都没被改。

        第一版用 `updated == len(set(ids))` 做判据，而 `0 == len(set([]))` **恒成立**
        ⇒ 空列表会被判成「成功结账 0 笔」并返回 OK。
        """
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": []}).json()

        self.assertEqual(body["code"], 1000)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_resubmitting_the_same_batch_is_a_skipped_success(self):
        """幂等：重复提交 ⇒ **成功**，全部进 `skipped_performances`（2026-10-03 Q3=A 改写）。

        ⚠️ **改前 → 改后**：原用例叫 `test_resubmitting_the_same_batch_is_a_business_failure`，
        断言第二次 `code == 1006` 且 `msg` 里有那个 id——那是「双击第二次要明确看到这批已经结过了」
        的旧口径。业务方裁定 Q3=A（「勾选未结算或者已结算的时候，如果勾选的订单
        已经是要更改的状态 直接跳过即可 不用报错」）⇒ 现在断的是「成功 + `skipped_performances`
        恰好是那一条 + 库里一行未动 + 零新审计」——判据比旧的那条**更严**（旧的那条只看业务码）。
        """
        perf = self._perf(self._order(), self.taker_a)

        first = self._post({"performance_ids": [perf.id]}).json()
        perf.refresh_from_db()
        after_first = perf.updated_at
        audit_after_first = OperationLog.objects.count()

        second = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], 0, second)
        self.assertEqual(second["data"]["settled_performances"], [])
        self.assertEqual(second["data"]["settled_adjustments"], [])
        self.assertEqual(second["data"]["skipped_performances"], [perf.id])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        # 第二次**一行都没写**：`updated_at`（`auto_now`）与审计行数都不变。
        self.assertEqual(perf.updated_at, after_first)
        self.assertEqual(OperationLog.objects.count(), audit_after_first)

    def test_one_negative_row_is_allowed_when_the_same_owner_offsets_it(self):
        """§10 #18：一条负数可与**同一个人的**其他正数一起结（该人组和 ≥ 0）。"""
        # taker-a：−5.00 + 20.00 = 15.00 ≥ 0
        negative = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("10.00"),
        )
        self._adj(negative, "-15.00")
        positive = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
        )

        body = self._post(
            {"performance_ids": [negative.id, positive.id]}
        ).json()

        self.assertEqual(body["code"], 0)
        negative.refresh_from_db()
        positive.refresh_from_db()
        self.assertEqual(negative.settle_status, "已结账")
        self.assertEqual(positive.settle_status, "已结账")

    def test_different_owners_do_not_offset_each_other(self):
        """§10 #18 的拟定解读：**不同人**混批时不许互相抵（A 的亏空不能用 B 的盈余平）。"""
        # taker-a：−5.00；taker-b：+20.00；整批 +15.00，但分组后 A 组为负 ⇒ 拒绝。
        negative = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("10.00"),
            bonus_amount=Decimal("10.00"),
        )
        self._adj(negative, "-15.00")
        positive = self._perf(
            self._order(),
            self.taker_b,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
        )

        body = self._post(
            {"performance_ids": [negative.id, positive.id]}
        ).json()

        self.assertEqual(body["code"], 1011)
        # taker-a 的组和 = 10.00 − 15.00 = −5.00（具体十进制值，E-0024）。
        self.assertIn("-5.00", body["msg"])
        negative.refresh_from_db()
        positive.refresh_from_db()
        self.assertEqual(negative.settle_status, "未结账")
        self.assertEqual(positive.settle_status, "未结账")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_audit_rows_carry_the_settle_action_order_and_values(self):
        """§9 #17：留痕落在**新表 `OperationLog`** 上——断言具体行数与字段值。

        两个订单：A 有一条绩效 + 一条未结账调整（2 行留痕），B 只有绩效（1 行）⇒ 共 3 行。
        ⛔ **不得用日志断言**（本项目没有 `LOGGING` 配置，`logger.info()` 是空操作）。
        ⭐ M3a：动作是 `performance.settle`、分类 20；`performance_id` 的承重语义
        （「结的是哪一条绩效」）改由 R11 的 `target_kind="user"` + `target_id`/`target_name`
        承载（`OperationLog` 没有 `performance_id` 列）。期望值全是**字面值**（E-0024）。
        """
        order_a = self._order()
        perf_a = self._perf(order_a, self.taker_a)
        self._adj(perf_a, "-5.00")
        order_b = self._order()
        perf_b = self._perf(order_b, self.taker_b)
        legacy_before = AuditLog.objects.count()

        body = self._post(
            {
                "performance_ids": [perf_a.id, perf_b.id],
                "reason": "9 月结账",
            }
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(OperationLog.objects.count(), 3)
        # 旧出口退役的判据：这些端点**一行都不许**再写旧表（spec §16.1）。
        self.assertEqual(AuditLog.objects.count(), legacy_before)

        rows_a = list(OperationLog.objects.filter(order_no=order_a.order_no))
        self.assertEqual(len(rows_a), 2)
        for row in rows_a:
            self.assertEqual(row.action, "performance.settle")
            self.assertEqual(row.log_type, 20)
            self.assertEqual(row.order_no, order_a.order_no)
            self.assertEqual(row.target_kind, "user")
            self.assertEqual(row.target_id, str(self.taker_a.pk))
            self.assertEqual(row.target_name, "taker-a")
            self.assertEqual(row.actor, "finance-p4-7")
            self.assertEqual(row.from_value, "未结账")
            self.assertEqual(row.to_value, "已结账")
            self.assertEqual(row.reason, "9 月结账")
            self.assertEqual(
                row.summary, f"【finance-p4-7】已将taker-a的绩效结账状态标记为已结账（仅标记状态）（订单 {order_a.order_no}）"
            )

        rows_b = list(OperationLog.objects.filter(order_no=order_b.order_no))
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0].action, "performance.settle")
        self.assertEqual(rows_b[0].target_id, str(self.taker_b.pk))
        self.assertEqual(rows_b[0].target_name, "taker-b")
        self.assertEqual(rows_b[0].reason, "9 月结账")

    def test_the_audit_row_names_the_owner_by_account_name_not_login_account(self):
        """⭐ 2026-10-17（订单日志人名线 spec §3-A2 / 判据 5）：结账留痕里的人名是**账号名称**。

        ⚠️ 本用例**自建**一个带昵称的归属人（不去改共享夹具 `taker-a`）：
        那两条共享夹具被同一文件里多处方言引用（拒绝文案等），改它会把判据焊到一片别的东西上。
        ⚠️ 两向断言（昵称在 + 账号不在）——只断前者时「两个都写进去」也能绿。
        """
        owner = User.objects.create_user(
            username="taker-nick", first_name="测试昵称打手子"
        )
        order = self._order()
        perf = self._perf(order, owner)

        body = self._post(
            {"performance_ids": [perf.id], "reason": "昵称结账"}
        ).json()
        self.assertEqual(body["code"], 0, body)

        row = OperationLog.objects.get(
            order_no=order.order_no, action="performance.settle"
        )
        self.assertEqual(row.target_name, "测试昵称打手子")
        self.assertNotEqual(row.target_name, "taker-nick")
        self.assertEqual(
            row.summary,
            f"【finance-p4-7】已将测试昵称打手子的绩效结账状态标记为已结账（仅标记状态）（订单 {order.order_no}）",
        )
        self.assertIn("测试昵称打手子", row.summary)
        self.assertNotIn("taker-nick", row.summary)

    def test_a_settle_batch_with_a_taker_row_and_a_sales_row_keeps_them_distinct(self):
        """⭐ 承重语义：**一条订单同时有打手行与客服行**时，两行留痕仍能分辨。

        旧 `log_settle` 带 `performance_id` 正是为了这件事（spec §3.6：只用 `order_no`
        分不出结的是哪条）。`OperationLog` 没有该列 ⇒ 身份必须由 R11 的
        `target_kind="user"` + `target_id`/`target_name`（绩效归属人）承载；
        `uniq_performance_order_user` 保证「(订单, 人)」唯一，所以按 (order_no, target_id)
        就能还原是哪条绩效。本用例**不用主键**指认（AGENTS.md §5 的跨租户教训同族）。
        """
        order = self._order()
        taker = self._perf(order, self.taker_a, emp_type=EmpType.MAIN)
        sales = self._perf(order, self.taker_b, emp_type=EmpType.SALES)

        body = self._post({"performance_ids": [taker.id, sales.id]}).json()

        self.assertEqual(body["code"], 0)
        rows = list(OperationLog.objects.filter(order_no=order.order_no))
        self.assertEqual(len(rows), 2)
        # 两行**不是**同一行：target_id / target_name 各自指向自己的归属人。
        self.assertEqual(
            sorted(row.target_name for row in rows), ["taker-a", "taker-b"]
        )
        self.assertEqual(
            {row.target_id for row in rows}, {str(self.taker_a.pk), str(self.taker_b.pk)}
        )
        # 两行都能各自反查到不同的绩效 id（按「订单 × 人」这一唯一键）。
        by_owner = {
            row.target_id: Performance.objects.get(
                order=order, user_id=int(row.target_id)
            ).id
            for row in rows
        }
        self.assertEqual(
            sorted(by_owner.values()), sorted([taker.id, sales.id]),
            "两行留痕无法各自指认一条绩效 ⇒ 旧 performance_id 的承重语义丢了",
        )

    def test_audit_failure_rolls_back_performances_and_adjustments(self):
        """审计失败时，绩效、调整记录和提前结算申请随本次结账一起回滚。

        ⚠️ **桩打在「被查找的那个模块」**（E-0029）：`settlement` 里 `record` 是模块全局名
        （`from apps.audit.log import record`），所以补丁目标是
        `apps.business.settlement.record`，**不是** `apps.audit.log.record`。
        ⭐ M3a：E-0029 的原目标 `apps.business.audit.log_settle` 随 `audit.py` 一起退役，
        桩目标按同一规则换成 `record`（被查找的位置）。
        ⚠️ **桩要自证接上**：`stub.called` 必须为真（不接桩时同一批会正常结账——见上面 #4 ①）。
        """
        perf = self._perf(self._order(), self.taker_a)
        adjustment = self._adj(perf, "-5.00")
        request = PerformanceSettleRequest.objects.create(performance=perf, user=self.taker_a)
        stub = mock.Mock(side_effect=RuntimeError("审计写入失败"))
        self.finance.raise_request_exception = False

        with mock.patch("apps.business.settlement.record", stub):
            response = self._post({"performance_ids": [perf.id]})

        self.assertEqual(response.status_code, 500)
        self.assertTrue(stub.called, "桩没接上：record 一次都没被调用")
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        self.assertEqual(perf.settled_by, "")
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
        self.assertEqual(request.state, "pending")
        self.assertIsNone(request.settled_at)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_the_audit_stub_target_is_looked_up_in_the_settlement_module(self):
        """E-0029 的**对照**：桩打在「**定义** `record` 的模块」上 ⇒ 不生效、请求照常成功。

        与上一条合起来，「桩接上了」才有判据：
        打 `apps.business.settlement.record`（**被查找**的模块）⇒ 500 + 两张表未变；
        打 `apps.audit.log.record`（定义它的模块）⇒ 200 + 已结账 + 那个桩没被调用。
        少了这条控制项，上一条的 `stub.called` 也可能只是巧合（E-0029 的原形状：
        桩打错模块时 `assertRaises` 直接失败，看起来像「回滚没生效」）。
        ⭐ M3a：对照目标从 `apps.business.audit.log_settle`（随旧出口退役）换成
        `apps.audit.log.record`——同一条 E-0029 规则（找被查找的位置，不是定义的位置）。
        """
        perf = self._perf(self._order(), self.taker_a)
        wrong_target = mock.Mock(side_effect=RuntimeError("打在定义模块上，不该生效"))

        with mock.patch("apps.audit.log.record", wrong_target):
            response = self._post({"performance_ids": [perf.id]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertFalse(wrong_target.called, "桩打在定义模块上，却真的被调用了")
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")

    def test_a_stale_performance_rowcount_rolls_back_and_writes_no_audit(self):
        """⭐ 乐观并发校验（E-0022 同族）：`Performance` 的 `update()` 报 **0 行** ⇒
        整批回滚 + `1006` + **一行审计都不许写**。

        机制（评审给出的形状，**不依赖真并发**）：两个请求都先读到「未结账」，
        T1 先更新并提交，T2 的 `UPDATE` 在库里重判谓词、命中 **0 行**——而第 6 步的审计
        仍按第 3/4 步缓存的行写 ⇒ 「没真结却留了痕」。这里用确定性桩把
        `Performance` 的 `QuerySet.update` 返回值改成 0，**精确复现 T2 的那次 UPDATE**。

        ⚠️ 桩打在**被查找的** `QuerySet.update` 上（E-0029）；`calls` 断言桩确实接上。
        """
        perf = self._perf(self._order(), self.taker_a)
        adjustment = self._adj(perf, "-5.00")
        calls = []

        with mock.patch.object(
            QuerySet,
            "update",
            _rowcount_stub(mismatching_model=Performance, calls=calls),
        ):
            body = self._post({"performance_ids": [perf.id]}).json()

        self.assertIn(
            Performance, calls, "桩没接上：Performance 的 update 一次都没被调用"
        )
        self.assertEqual(body["code"], 1006)
        self.assertIn("本批在你读取之后已被改动", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
        # 核心：不许留下「没真结却留了痕」的脏留痕。
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_stale_adjustment_rowcount_rolls_back_and_writes_no_audit(self):
        """判据覆盖**另一类**：`PerformanceAdjustment` 的 `update()` 报 0 行 ⇒ 同样回滚 + `1006`。

        这一次绩效行**真的被更新了**（只有调整记录那一类被桩改坏）——它必须跟着回滚一起
        退回「未结账」，否则就成了「一半成功」。
        """
        perf = self._perf(self._order(), self.taker_a)
        adjustment = self._adj(perf, "-5.00")
        calls = []

        with mock.patch.object(
            QuerySet,
            "update",
            _rowcount_stub(mismatching_model=PerformanceAdjustment, calls=calls),
        ):
            body = self._post({"performance_ids": [perf.id]}).json()

        self.assertIn(
            PerformanceAdjustment,
            calls,
            "桩没接上：PerformanceAdjustment 的 update 一次都没被调用",
        )
        self.assertEqual(body["code"], 1006)
        self.assertIn("本批在你读取之后已被改动", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_the_same_batch_settles_when_the_rowcount_stub_is_not_installed(self):
        """对照组（E-0029 的纪律）：**不接桩**时同一批照常成功。

        没有这条，上面两条的 `1006` 无法区分「rowcount 判据真的命中」与
        「实现一律返回 `1006`」的假通过。
        """
        perf = self._perf(self._order(), self.taker_a)
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)
        self.assertEqual(OperationLog.objects.count(), 2)


class SettleAdjustmentsLegTests(_SettleFixture):
    """钱② 残留（2026-09-23 业务方裁定）：**调整记录那条腿**要求「订单**不在池内**」
    （`UNASSIGNED` / `WAITING_GRAB`）。

    被修的那条路径（钱② 探针的残留）：订单已回池、绩效行**已结账**、它又被追加了一条
    未结账调整 ⇒ 第 2 步的 `rows` 天然不含这行（它已结账），而 `adjustments` 那条腿过去
    **不看订单状态** ⇒ 整批不报错、调整记录照样被结掉。探针实测那两条调整净 `0.00`
    所以无害，但**非 0 的人工修正**会被结掉，而那种场景没有实测过。

    ⚠️ **口径的往返（如实留痕）**：初版落的是「要求订单 `COMPLETED`」（业务方第一轮选 A），
    被实测推翻——它把 `tests/test_refund_full.py` 的
    `FullRefundSettledPriorDeductionTests::test_the_appended_correction_can_still_be_settled`
    （spec §3.3 的坏账出口：全额退款后订单落 `REFUNDED`，那条把客服提成冲回 `0` 的
    `FULL_REFUND` 冲减**结不掉** = **少扣钱 / 多发钱**、且**永久**不是窗口）打红 ⇒
    业务方裁定 **B「订单不在池里（排除未指派 / 待抢单）」**。
    钱② 的窗口本来就只在池内两态上，两种写法在**窗口上等价**；差别只在退单 / 问题单 /
    已暂停这些**非池内**的未完成状态——**这些仍然要能结**（坏账出口），见 ②。

    ⚠️ 每组都是**一正一反**：只钉拒绝侧的话，「把所有调整记录都拦掉」也能全绿。
    ⚠️ **两个池内状态各一条**（`UNASSIGNED` 与 `WAITING_GRAB`）：只钉一个的话，
    「池内集合漏掉另一个」的变异不会被任何用例杀死。
    """

    def _assert_rejected_in_pool(self, status):
        perf = self._perf(
            self._order(status=status),
            self.taker_a,
            settle_status=SettleStatus.SETTLED,
        )
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("订单未完成", body["msg"])
        self.assertIn(status, body["msg"])
        self.assertNotIn("已完全结清", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_adjustment_on_a_settled_row_of_an_unassigned_order_is_rejected(self):
        """①-a 池内（**未分配**）：已结账绩效 + 一条未结账调整 ⇒ `1006`、
        原因指向订单未完成（真话：`UNASSIGNED` 确实不是完成态）、
        且**库里那条调整记录仍是「未结账」**（E-0009：断言数据库行）。

        `1006` 与「整批拒绝」只是出口；真正的判据是数据库那一行的 `settle_status`
        与 `settled_at`——响应说没结、库里真的没结，才是财务对账的依据。
        """
        self._assert_rejected_in_pool(OrderStatus.UNASSIGNED)

    def test_an_adjustment_on_a_settled_row_of_a_waiting_grab_order_is_rejected(self):
        """①-b 池内（**待抢单**）：与 ①-a 同形，专门钉「池内」是**两个**状态。

        少了这条，把池内集合写成只排 `UNASSIGNED`（漏掉 `WAITING_GRAB`）的变异
        不会被任何用例杀死——而 `WAITING_GRAB` 同样是「订单还在池里」。
        """
        self._assert_rejected_in_pool(OrderStatus.WAITING_GRAB)

    # ── 评审 B-1：调整记录**不是独立的一笔钱** ──────────────────

    def _assert_rejected_when_the_row_is_unsettled(self, status):
        """行**未结账** + 订单**非池内的未完成态** + 一条未结账调整 ⇒ 整批 `1006`。

        这是评审 B-1 实测的那条漏：该 id 既不在 `rows`（订单没完成），过去却能从
        `adjustments` 那条腿结掉 ⇒ 第 5 步**只把调整记录标成已结账**、绩效行留到以后，
        而 `payable_net` 只算未结账调整 ⇒ 那笔金额**永不进入任何实结金额**（多发钱）。
        """
        perf = self._perf(self._order(status=status), self.taker_a)  # 行**未结账**
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("订单未完成", body["msg"])
        self.assertIn(status, body["msg"])
        self.assertNotIn("已完全结清", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_amount)
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_adjustment_on_an_unsettled_row_of_an_in_progress_order_is_rejected(self):
        """①-a 评审 B-1：`IN_PROGRESS` + **行未结账** + 一条未结账调整 ⇒ `1006`，
        且库里那条调整记录仍是「未结账」（E-0009：断言数据库行）。

        可达性（评审给的两条）：结算列表不按订单状态过滤、绩效页能勾这种行。
        """
        self._assert_rejected_when_the_row_is_unsettled(OrderStatus.IN_PROGRESS)

    def test_an_adjustment_on_an_unsettled_row_of_a_pending_review_order_is_rejected(self):
        """①-b 评审 B-1：`PENDING_REVIEW`（待验收）同形 ⇒ 同样 `1006`、同样不落库。

        与 ①-a 配对：只钉一个状态的话，「只挡 `IN_PROGRESS`」这类漏判不会被杀死。
        """
        self._assert_rejected_when_the_row_is_unsettled(OrderStatus.PENDING_REVIEW)

    def test_the_adjustment_does_not_drop_out_of_the_settled_amount(self):
        """⭐ ② 钱不再掉出实结金额（评审 B-1 的**回归用例**，钉字面金额）。

        同一条链走两步：窗口内（`IN_PROGRESS` + 行未结账）结 ⇒ **被拒**；订单改成
        `COMPLETED` 再结 ⇒ 那条调整与行**同一批**结掉，`settled_amount` 是**含它的净额**
        `20.00 − 0.00 + (−5.00) = 15.00`。

        修前（只排池内两态）：第一步**成功**、只有调整记录变「已结账」⇒ 第二步再结时
        `payable_net` 只算未结账调整 ⇒ `settled_amount` 会是 `20.00`，**多发 `5.00`**。
        评审探针的实测对照正是 `20.00` vs `15.00`。
        """
        perf = self._perf(self._order(status=OrderStatus.IN_PROGRESS), self.taker_a)
        adjustment = self._adj(perf, "-5.00")

        first = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(first["code"], 1006, first)
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_amount)
        self.assertEqual(adjustment.settle_status, "未结账")

        Order.objects.filter(pk=perf.order_id).update(status=OrderStatus.COMPLETED)

        second = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(second["code"], 0, second)
        self.assertEqual(second["data"]["settled_performances"], [perf.id])
        self.assertEqual(second["data"]["settled_adjustments"], [adjustment.id])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(perf.settled_amount, Decimal("15.00"))   # 字面值（E-0024）
        self.assertEqual(adjustment.settle_status, "已结账")

    def test_an_unsettled_row_of_a_completed_order_settles_with_its_adjustment(self):
        """③ 正常路径（**承重**）：订单 `COMPLETED` + **行未结账** + 一条未结账调整
        ⇒ 行与调整**同一批**结掉。

        它钉住新增判据的第一支（`performance__order__status=COMPLETED`）：删掉那一支、
        只留「行已结账」时，行仍会从 `rows` 那条腿结掉（`code 0`），但**调整被漏掉**
        （`settled_adjustments` 为空）⇒ 本用例红。
        """
        perf = self._perf(self._order(status=OrderStatus.COMPLETED), self.taker_a)
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [perf.id])
        self.assertEqual(body["data"]["settled_adjustments"], [adjustment.id])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)

    def test_the_same_adjustment_settles_once_the_order_is_completed(self):
        """② 反例（坏账出口的正身）：**同一批**在订单为 `COMPLETED` 时 ⇒ 能结掉。

        它把「订单在池内真的挡住了调整记录」与「实现把所有调整记录都拦掉了」区分开：
        删掉池内排除时①-a/①-b 会红、本用例不受影响；反过来，「把所有调整记录都拦掉」
        的实现会让本用例红、①-a/①-b**假绿**。
        """
        perf = self._perf(
            self._order(status=OrderStatus.COMPLETED),
            self.taker_a,
            settle_status=SettleStatus.SETTLED,
        )
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [])
        self.assertEqual(body["data"]["settled_adjustments"], [adjustment.id])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)

    def test_a_refund_correction_on_a_settled_row_of_a_refunded_order_settles(self):
        """⭐ ②-b **承重反例（裁定 B 的由来）**：订单 `REFUNDED`（**非池内**的未完成态）
        上「已结账绩效 + 追加的**退款冲减**」必须**结得掉**——这就是 spec §3.3 的坏账出口。

        初版口径（要求订单 `COMPLETED`）会把这条路径**永久**堵死：全额退款后订单落
        `REFUNDED`，那条把客服提成冲回 `0` 的冲减再也结不掉 ⇒ 该扣的 `8.00` 扣不回来
        （**多发钱**）。所以业务方裁定 B「只排除池内两态」。

        期望值钉**字面金额**与**字面状态**（E-0024）：金额 `-8.00`、状态 `"已结账"`。
        """
        perf = self._perf(
            self._order(status=OrderStatus.REFUNDED),
            self.taker_a,
            settle_status=SettleStatus.SETTLED,
        )
        adjustment = self._adj(
            perf, "-8.00", kind=PerformanceAdjustment.Kind.FULL_REFUND
        )
        # 夹具前提：这条冲减真的是「未结账」，且金额是字面值（否则下面的断言是空的）。
        self.assertEqual(adjustment.amount, Decimal("-8.00"))
        self.assertEqual(adjustment.settle_status, "未结账")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_performances"], [])
        self.assertEqual(body["data"]["settled_adjustments"], [adjustment.id])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")     # 原行不动（D-0014）
        self.assertEqual(adjustment.amount, Decimal("-8.00"))
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)

    def test_a_truly_settled_row_is_skipped_instead_of_reported_as_a_failure(self):
        """③ 「已完全结清」（已结账 + 一条未结账调整都没有）⇒ **跳过 + 成功**，不是 `1006`。

        ⚠️ **改前 → 改后**：原用例叫 `test_a_truly_settled_row_still_reports_fully_settled`，
        断的是 `code == 1006` + `msg` 含「已完全结清」。业务方裁定 Q3=A 之后，
        这一支在 fail-closed 判定**之前**就被摘走了 ⇒ 端点不再触达 `_reject_message` 里
        「已完全结清」那句话（那条判据仍是「库里还有没有账没结」的出处，见本类上面两条）。

        **它保留的对照价值与原来一样**：把「真的结清了」（成功 + 跳过）与「已结账但还有账没结」
        （本类上面两条：`1006` 且指认**真原因**）区分开——两边各断一侧，任何一侧写错都会红。
        """
        perf = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["skipped_performances"], [perf.id])
        self.assertEqual(body["data"]["settled_performances"], [])
        self.assertEqual(body["data"]["settled_adjustments"], [])
        # 跳过不写任何留痕（E-0009：打数据库行）。
        self.assertEqual(OperationLog.objects.count(), 0)


class SettleInputValidationTests(_SettleFixture):
    """§7.1 / §9 #16：请求来源的值必须过序列化器 ⇒ 200 + 业务码，**不是** 500。"""

    def test_null_ids_is_a_validation_failure(self):
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": None}).json()

        self.assertEqual(body["code"], 1000)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")

    def test_non_integer_element_is_a_validation_failure(self):
        body = self._post({"performance_ids": ["abc"]}).json()

        self.assertEqual(body["code"], 1000)

    def test_over_long_id_list_is_a_validation_failure(self):
        """超长列表：**必须在入口拒绝**——否则它直进 `filter(id__in=...)`，
        撞 PostgreSQL 的绑定参数上限，变成 HTML 500（E-0010 的「超长列表」那一格）。"""
        body = self._post({"performance_ids": list(range(1, 1002))}).json()

        self.assertEqual(body["code"], 1000)

    def test_missing_ids_key_is_a_validation_failure(self):
        body = self._post({"reason": "只给了原因"}).json()

        self.assertEqual(body["code"], 1000)

    def test_over_long_reason_is_a_validation_failure_and_changes_nothing(self):
        """`reason` 列宽 255：不校验就会在写审计行时 500——而那时绩效**已经落库**
        （P2 `PriceChangeSerializer` 注释记录过同一个坑：变成「只改字段、没有留痕」）。"""
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [perf.id], "reason": "长" * 300}).json()

        self.assertEqual(body["code"], 1000)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")

    def test_a_nonexistent_id_is_a_business_failure_not_a_500(self):
        body = self._post({"performance_ids": [999999]}).json()

        self.assertEqual(body["code"], 1006)
        self.assertIn("999999", body["msg"])


class SettlePermissionTests(TenantTestCase):
    """§9 #3（Ruling T）：`has_node("finance.change_settlement_status")` 真的生效。

    ⚠️ **必须用非管理员账号**：`has_node_now` 对 `is_superuser` 有**短路**（平面守卫排在它前面），
    用管理员构造会恒放行、测不出闸门。这里三个方向：持有 → 200；不持有 → 403 + 权限 msg；
    **持有另一个节点** → 仍 403（否则「有任意权限就放行」也能绿）。
    """

    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000001",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
        )
        self.taker = User.objects.create_user(username="taker-perm")

    def _perf(self):
        return Performance.objects.create(
            order=self.order,
            user=self.taker,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            review_status=ReviewStatus.APPROVED,
            settle_status=SettleStatus.UNSETTLED,
        )

    def _grant(self, username, *codes):
        nodes = [
            PermissionNode.objects.get_or_create(
                code=code,
                defaults={
                    "name": code,
                    "note": f"测试节点 {code}",
                    "kind": NodeKind.API,
                    "level": 1,
                },
            )[0]
            for code in codes
        ]
        role = Role.objects.create(
            name=f"角{username}"[:5], group=Group.objects.create(name=f"g-{username}"[:150])
        )
        role.permissions.set(nodes)
        User.objects.get(username=username).groups.add(role.group)

    def _post(self, client, performance_id):
        return client.post(
            SETTLE_URL,
            {"performance_ids": [performance_id]},
            content_type="application/json",
        )

    def test_user_holding_the_node_can_settle(self):
        client = make_logged_in_client(username="finance-with-node")
        self._grant("finance-with-node", NODE_CODE)
        # ⭐ 按游戏权限收窄：**请求方**要显式持有本夹具的游戏，
        #    否则结账取行时被收窄掉、拿 `1006`「不存在或不可见」。期望值一个字没改。
        grant_games(User.objects.get(username="finance-with-node"), self.game)
        perf = self._perf()

        body = self._post(client, perf.id).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")

    def test_user_without_the_node_is_denied_with_a_permission_message(self):
        client = make_logged_in_client(username="finance-without-node")
        perf = self._perf()

        response = self._post(client, perf.id)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "你没有结账权限")
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")

    def test_user_holding_a_different_node_is_still_denied(self):
        client = make_logged_in_client(username="finance-wrong-node")
        self._grant("finance-wrong-node", "order.list")
        perf = self._perf()

        response = self._post(client, perf.id)

        self.assertEqual(response.status_code, 403)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")

    def test_anonymous_request_is_401_not_403(self):
        """未认证 ⇒ **401**（spec §7 首段的分层：401 与 403 不能混）。

        这条守的是 `SettleView.permission_denied` 的**转调写法**：它给 403 换了一句中文
        `msg`，而实现必须转调 `super()`——DRF 在 `permission_denied` 里先判「有没有认证成功」，
        自己直接 `raise PermissionDenied` 会把匿名请求也变成 403。
        匿名 client 不带会话，租户守卫**不会**短路它（守卫只拦「带 `_auth_user_id` 但平面不符」），
        所以这条确实走到 DRF 的权限层。
        """
        anonymous = Client(SERVER_NAME="tenant.test.com")

        response = anonymous.post(
            SETTLE_URL, {"performance_ids": [1]}, content_type="application/json"
        )

        self.assertEqual(response.status_code, 401)


class SettleForbiddenTests(_SettleFixture):
    """Task 9：「禁止结算」（`StaffProfile.settle_forbidden`）在**结账**侧的拦截。

    业务方裁定「两处都拦」——结账（发钱）与扣点（从工资扣钱）都不许。
    本类是结账侧的四条：拒绝、解冻放行、混合批整批回滚、**没有档案行的人不被误挡**。

    ⚠️ **拒绝形态沿用 `_reject_message` 的既有形状**（整批拒 + 逐 id 说明原因），
    业务码因此是 `1006`（`SettleError(..., SETTLE_STATE_INVALID)`）——
    ⚠️ 计划 `2026-09-22-system-user-list-completion.md` §4.2 写的是 `ErrorCode.BUSINESS`；
    本实现按**任务书 brief** 的「不许另写一段 if / 不许整批一个笼统 msg」取既有形状，
    差异已写进 `task-9-report.md` 的疑虑一节。
    """

    def _freeze(self, user, *, value=True):
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.settle_forbidden = value
        profile.save(update_fields=["settle_forbidden"])
        return profile

    @staticmethod
    def _perf_state(performance):
        """绩效行上「结账会改动」的四个列（整批回滚的判据）。"""
        return (
            performance.settle_status,
            performance.settled_at,
            performance.settled_amount,
            performance.settled_by,
        )

    @staticmethod
    def _adjustment_state(adjustment):
        return (adjustment.settle_status, adjustment.settled_at)

    def test_a_frozen_owner_rejects_the_batch_and_settles_nothing(self):
        """冻结的人 ⇒ 整批拒；**msg 指认得是这个人**；库里那行一个状态位都没动。"""
        self._freeze(self.taker_a)
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006)
        self.assertIn("禁止结算", body["msg"])
        self.assertIn("taker-a", body["msg"])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        self.assertFalse(OperationLog.objects.filter(order_no=perf.order.order_no).exists())

    def test_unfreezing_lets_the_same_row_settle(self):
        """同一个人解冻后再结 ⇒ 放行。**前置哨兵**：解冻前那一次必须已经被拒，
        否则「解冻后绿」可能由「这条判据根本没生效」凑出来。"""
        profile = self._freeze(self.taker_a)
        perf = self._perf(self._order(), self.taker_a)
        self.assertEqual(self._post({"performance_ids": [perf.id]}).json()["code"], 1006)

        profile.settle_forbidden = False
        profile.save(update_fields=["settle_forbidden"])
        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertIsNotNone(perf.settled_at)

    def test_a_mixed_batch_is_rejected_whole(self):
        """混合批（一个冻结 + 一个正常）⇒ 整批拒；**正常那行也没被结**（事务里一行都没写）。"""
        self._freeze(self.taker_a)
        frozen = self._perf(self._order(), self.taker_a)
        normal = self._perf(self._order(), self.taker_b)

        body = self._post({"performance_ids": [frozen.id, normal.id]}).json()

        self.assertEqual(body["code"], 1006)
        self.assertIn("禁止结算", body["msg"])
        frozen.refresh_from_db()
        normal.refresh_from_db()
        self.assertEqual(frozen.settle_status, "未结账")
        self.assertEqual(normal.settle_status, "未结账")
        self.assertIsNone(normal.settled_at)
        self.assertIsNone(normal.settled_amount)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_an_owner_without_a_profile_row_is_not_blocked(self):
        """**边界**：归属人**没有档案行** ⇒ 这条判据**不挡**他（`~Q(...=True)` 的语义）。

        判别力：把判据写成 `Q(user__staff_profile__settle_forbidden=False)`（INNER JOIN）
        ⇒ 没有档案行的行被 join 丢掉 ⇒ 本用例变红（`1006` 而不是 `0`）。
        这也是为什么不能用「查不到档案 = 禁止」的真值判据——`seed_tenant` 灌的 `admin`
        这类账号按设计就没有 `StaffProfile`。
        """
        self.assertFalse(StaffProfile.objects.filter(user=self.taker_a).exists())  # 夹具自证
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")

    # ── 调整记录那条腿也必须被冻结挡住 ──────────
    def test_a_frozen_owner_with_an_unsettled_adjustment_rejects_the_batch(self):
        """① **绩效行 + 未结账调整记录**：整批拒，且**调整记录也没被结**。

        判别力（评审 I-1 实测出的漏钱路径）：`adjustments` 那条查询若只按
        `performance_id__in` + `settle_status=未结账` 过滤、**不排被冻归属人**，
        这个 id 会从「调整腿」被算成合格 ⇒ 第 3 步 fail-closed 不报 ⇒ 调整记录被结掉、
        写出一条 `performance.settle` 审计行，而绩效行留在**未结账的半态**。
        本用例因此同时钉三样：响应码、调整记录态、审计行数。
        """
        self._freeze(self.taker_a)
        perf = self._perf(self._order(), self.taker_a)
        adjustment = self._adj(perf, "-5.00")
        before_perf = self._perf_state(perf)
        before_adjustment = self._adjustment_state(adjustment)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006)
        self.assertIn("禁止结算", body["msg"])
        self.assertIn("taker-a", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(self._perf_state(perf), before_perf)
        self.assertEqual(self._adjustment_state(adjustment), before_adjustment)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_frozen_owner_with_only_a_new_adjustment_rejects_the_batch(self):
        """② **绩效已结账 + 后追加的未结账调整记录**（第 2 步自述的「坏账出口」）。

        这是「坏账出口」被冻结挡住的那一半：这一批**只有调整记录**可结（绩效行早已结账）,
        所以它是最容易被漏掉的一条腿——绩效行不在 `rows` 里是**正常**的，
        判据全落在调整腿上。整批拒 ⇒ 调整记录一行也不许动。
        """
        self._freeze(self.taker_a)
        perf = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        adjustment = self._adj(perf, "-5.00")
        before_perf = self._perf_state(perf)
        before_adjustment = self._adjustment_state(adjustment)

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006)
        self.assertIn("禁止结算", body["msg"])
        self.assertIn("taker-a", body["msg"])          # 不是「已完全结清」——见 _reject_message 的顺序
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(self._perf_state(perf), before_perf)
        self.assertEqual(self._adjustment_state(adjustment), before_adjustment)
        self.assertEqual(OperationLog.objects.count(), 0)


class SettleForbiddenGateTests(_SettleFixture):
    """钱⑤（2026-09-23 业务方裁定）：账户级「禁止结算」是结账的闸门。

    对标 `isProhibitSettStatus`（`.tools/recon/sysuser/js/userlist.js:299-300`、`:763` 的
    `lay-tips` 整句「如果你临时不想给TA结算…**此人在冻结期间的所有账单都将无法结算**」），
    以及扣点前置条件整句（`.tools/recon/suqing1/assets/JS2019/Emp/emplist.js:659` 的
    「【生效绩效大于0】并且【未被禁止结算】」）。落地形态：`StaffProfile.settle_forbidden`
    被标成 `True` 的人，其**绩效行**与**调整记录**两条腿都被第 2 步排除 ⇒ 第 3 步
    fail-closed 拒绝整批（业务码 **1006**，与「订单未完成」「还没有接单人」同一条出口）。

    ⚠️ **② 是必须有的反例**：只有 ① 的话，「把所有行都拦掉」也能让 ① 绿——它不区分
    「只拦被标记的人」与「拦了所有人」。

    ⚠️ **落点（执行期修订 2 第 1 条）**：本类原先在
    `tests/test_settlement_api.py`（一个**只读**结算接口的文件）里，而
    `POST /api/business/settlements/settle` 的写面用例本就在本文件
    （`SETTLE_URL` 见 `:61`）⇒ 同一端点被两个文件测着、常量重复。现在只留本文件一处。
    """

    def _profile(self, user, **fields):
        return StaffProfile.objects.create(user=user, **fields)

    def test_a_forbidden_owner_is_rejected_and_the_row_stays_unsettled(self):
        """① 闸门：`settle_forbidden=True` 的归属人 ⇒ 整批 `1006` 且库里那行仍未结账。

        E-0009：判据打在**数据库行**上（不是日志、也不是响应里的 `settled_performances`）
        ——「响应说没结」与「库里真的没结」是两件事，后者才是财务对账的依据。
        """
        self._profile(self.taker_a, settle_forbidden=True)
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [perf.pk]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("禁止结算", body["msg"])
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_amount)

    def test_the_same_batch_settles_when_the_owner_is_not_forbidden(self):
        """② 反例：同一个人 `settle_forbidden=False` ⇒ 同一批**能结掉**。

        它把「闸门只对被标记的人生效」与「闸门把所有行都拦了」区分开。
        """
        self._profile(self.taker_a, settle_forbidden=False)
        perf = self._perf(self._order(), self.taker_a)

        body = self._post({"performance_ids": [perf.pk]}).json()

        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")

    def test_an_owner_without_a_profile_still_settles(self):
        """③ 钉住 `.exclude(...=True)` 这个选择：**没有 `StaffProfile`** 的归属人仍能结账。

        `StaffProfile` 是 `auth.User` 的**一对一扩展**，反向过滤写成
        `.filter(user__staff_profile__settle_forbidden=False)` 时 SQL 是**内连接** ⇒
        没有档案的用户（`seed_tenant` 灌的 `admin` 就是这种）**整批从 `rows` 里消失**、
        被 `1006` 拒掉。那不是「禁止结算」，是「没有档案就不能结账」——
        与 P3e `gameName` 同型的语义事故。
        """
        # 夹具前提：这个归属人**确实**没有档案行（不是「有档案但开关为假」）。
        self.assertFalse(StaffProfile.objects.filter(user=self.taker_b).exists())
        perf = self._perf(self._order(), self.taker_b)

        body = self._post({"performance_ids": [perf.pk]}).json()

        self.assertEqual(body["code"], 0, body)
        perf.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")

    # ── 评审 A-1：闸门必须**两条腿对称** ──────────────────────────

    def test_a_forbidden_owners_unsettled_adjustment_is_rejected_too(self):
        """⭐ 评审 A-1：被冻结的人的**未结账调整记录**也结不掉（不再「只拦绩效行」）。

        组合：绩效行**已结账**（`rows` 那条腿天然不含它）+ 一条新调整记录 + 订单
        `COMPLETED` + 归属人 `settle_forbidden=True`。修前只有绩效行那条腿有闸门 ⇒
        调整记录照样被结掉（实测 `code 0`、调整记录变「已结账」、绩效行仍未结账）；
        随后解冻再结那行时 `payable_net` 只算**未结账**调整 ⇒ 那笔钱**进了「已结账」**
        却从未进入任何实结金额 = **少发钱**。这与 tips 原文「**所有账单**都将无法结算」
        及 DoD #2 都不一致。

        ⚠️ 本条同时钉 `_reject_message` 的**原因优先级**：这个组合在旧文案下先命中
        「已完全结清」——那是**假话**（库里明明还躺着一条未结账的调整）。所以断言
        **同时**要求出现「禁止结算」且**不出现**「已完全结清」。
        """
        self._profile(self.taker_a, settle_forbidden=True)
        perf = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("禁止结算", body["msg"])
        self.assertNotIn("已完全结清", body["msg"])
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "已结账")
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)

    def test_the_same_adjustment_settles_when_the_owner_is_not_forbidden(self):
        """反例：同一个人 `settle_forbidden=False` ⇒ 同一条调整记录**能结掉**。

        与上一条配对，把「闸门只对被标记的人生效」与「闸门把所有调整记录都拦了」区分开。
        """
        self._profile(self.taker_a, settle_forbidden=False)
        perf = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_adjustments"], [adjustment.id])
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.settle_status, "已结账")
        self.assertIsNotNone(adjustment.settled_at)

    def test_an_owner_without_a_profile_can_still_settle_an_adjustment(self):
        """③-b：**没有 `StaffProfile`** 的归属人，其已结账绩效上的新调整记录也仍能结账。

        这是③在**调整记录那条腿**上的孪生用例：两条腿的过滤都必须写成
        `.exclude(...=True)`（LEFT JOIN），换成 `.filter(...=False)`（内连接）时
        **本用例**会红——③ 只覆盖绩效行那条腿，杀不掉调整记录那条腿的变异。
        """
        self.assertFalse(StaffProfile.objects.filter(user=self.taker_b).exists())
        perf = self._perf(
            self._order(), self.taker_b, settle_status=SettleStatus.SETTLED
        )
        adjustment = self._adj(perf, "-5.00")

        body = self._post({"performance_ids": [perf.id]}).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["settled_adjustments"], [adjustment.id])
        adjustment.refresh_from_db()
        self.assertEqual(adjustment.settle_status, "已结账")


class SettleSkipScopeGateTests(_SettleFixture):
    """⭐ M-2：`settlement.settled_ids_without_pending_adjustments` 的**按游戏收窄**是承重的。

    **变异实测**：把那个函数里的
    `scope_filter=game_scope("order__game_id")` 删掉，`tests.test_settle_api` +
    `tests.test_p4fu_performance_writes` **88 条全绿**（`SURVIVED`）——「已是目标状态
    ⇒ 跳过」那道判据一旦丢了收窄，**别人游戏的已结账行**会被念成「本来就已结账、
    跳过吧」而让整批成功：既是**一句假话**（那一行并不在请求方的可见范围内），
    也是一次**越权被静默吞掉**（本该 `1006`「不存在或不可见」）。

    判据形态照 `tests/test_settlement_preview_api.py::PreviewScopeTests`：
    `games=[]` 是**显式 opt-out**（夹具默认给当前租户的**全部**游戏 ⇒ 区分不出范围），
    权限的唯一来源是 `grant_games(..., game_a)`。
    """

    def setUp(self):
        super().setUp()
        self.game_b = Game.objects.create(name="另一个游戏")

    def _a_only(self):
        """只勾 `game_a`（= 夹具自带的 `self.game`）的账号：持结账节点、零游戏起步。"""
        client = make_logged_in_client(
            username="settle-a-only", groups=("清算甲",), nodes=(NODE_CODE,), games=[]
        )
        grant_games(User.objects.get(username="settle-a-only"), self.game)
        return client

    def _order_b(self):
        return Order.objects.create(
            game=self.game_b,
            order_no="20260920000000099001",
            status=OrderStatus.COMPLETED,
            hall_price=Decimal("100.00"),
        )

    def test_another_games_settled_row_is_rejected_not_silently_skipped(self):
        """B 游戏的**已结账**行在 A 账号下 ⇒ 仍 `1006`，⛔ 不进「已跳过」。

        前置哨兵两条（否则「没跳过」与「端点坏了」不可区分）：
        ① 同一次调用里 A 游戏的**同样形态**那一行**确实**被跳过（`code=0` + `skipped` 含它）；
        ② B 游戏那一行**真的在库里**且确实是 `已结账`。
        ⚠️ 失败信封的 `data` 是 `None` —— 「`skipped_performances` 里没有它」在这里
        等价于「**根本没有成功信封**」，本用例把这一点也断住。
        """
        client = self._a_only()
        mine = self._perf(
            self._order(), self.taker_a, settle_status=SettleStatus.SETTLED
        )
        theirs = self._perf(
            self._order_b(), self.taker_b, settle_status=SettleStatus.SETTLED
        )
        # 哨兵②：两行都在、且都是「已结账」。
        self.assertEqual(
            Performance.objects.filter(
                id__in=[mine.id, theirs.id], settle_status="已结账"
            ).count(),
            2,
        )

        # 哨兵①：A 游戏那一行走成功路径（否则下面那条 `1006` 可能只是端点在闹脾气）。
        ok = self._post({"performance_ids": [mine.id]}, client=client).json()
        self.assertEqual(ok["code"], 0, ok)
        self.assertEqual(ok["data"]["skipped_performances"], [mine.id])

        # 正题：B 游戏那一行 ⇒ 整批 `1006`、没有成功信封、那一行一个字节没动。
        theirs.refresh_from_db()
        untouched_at = theirs.updated_at
        body = self._post({"performance_ids": [theirs.id]}, client=client).json()
        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertIsNone(body["data"])
        theirs.refresh_from_db()
        self.assertEqual(theirs.settle_status, "已结账")
        self.assertEqual(theirs.updated_at, untouched_at)


class SettleHallShadowTests(_SettleFixture):
    """⭐ 二轮评审 Important 2：镜像卡派生的**隐藏单**上的绩效行不许结账。

    「隐藏」= 它不是本租户的一笔生意（没有客户价、也没有本租户的收入）；结了就是把
    「把卡同步给下家大厅的载体」当成真单发钱。两处判据**各有一条会红的用例**：

    * `settlement._resolve_batch` 的 `rows` 腿（第 2 步）——隐藏单的绩效行取不到
      ⇒ 第 3 步 fail-closed ⇒ 整批 `1006`（⛔ 不是静默部分成功）；
    * `settlement._reject_reasons` 的 `visible`（只产文案）——同一批行与「不存在或不可见」
      同一个出口，⛔ 不念它的订单状态（否则是一句与真实闸门不符的假话）。

    ⚠️ 生产上隐藏单没有 MAIN 行，这里造一条带行的：排除是**显式**判据，不靠「没有行」。
    """

    def _hidden_perf(self, *, status, no, user):
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        card = make_mirror(
            game=self.game, bound_by=user, no=f"SYN-SHADOW-{no}",
        )
        hidden = make_hidden_order(
            game=self.game, card=card, order_no=no, status=status, with_main=True,
            main_user=user,
        )
        return hidden, Performance.objects.get(order=hidden, emp_type=EmpType.MAIN)

    def test_a_hall_shadow_performance_is_not_settled(self):
        """✅ 完成态的隐藏单 ⇒ 结账取不到它 ⇒ 整批 `1006`，一行都没被结。

        没有 `rows` 腿那条排除时：它是「订单已完成 + 行未结账 + 有归属人」⇒ **会被结掉**
        （`code=0`）⇒ 本用例红。
        """
        good = self._perf(self._order(), self.taker_a)
        hidden, hidden_perf = self._hidden_perf(
            status=OrderStatus.COMPLETED, no="20261026000000000901", user=self.taker_b,
        )

        body = self._post(
            {"performance_ids": [good.id, hidden_perf.id]}
        ).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIsNone(body["data"])
        hidden_perf.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(hidden_perf.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(good.settle_status, SettleStatus.UNSETTLED)
        self.assertEqual(
            OperationLog.objects.filter(action="performance.settle").count(), 0,
            "整批回滚 ⇒ 零审计行",
        )
        hidden.refresh_from_db()
        self.assertIsNotNone(hidden.hall_card_id, "夹具自证：它确实是一张隐藏单")

    def test_a_hall_shadow_order_is_invisible_in_the_reject_message(self):
        """✅ 拒绝文案那一处也要排除：说「不存在或不可见」，⛔ 不说「订单未完成」。

        只去掉 `_reject_reasons` 的 `visible` 排除时：`rows` 腿取不到它、但 `visible`
        能取到 ⇒ 文案变成「订单未完成，当前：WAITING_GRAB」——一句**假话**
        （真正挡住它的是「隐藏单不参与结账」，不是订单状态）。
        """
        hidden, hidden_perf = self._hidden_perf(
            status=OrderStatus.WAITING_GRAB, no="20261026000000000902",
            user=self.taker_a,
        )

        body = self._post({"performance_ids": [hidden_perf.id]}).json()

        self.assertEqual(body["code"], 1006, body)
        self.assertIn("不存在或不可见", body["msg"])
        self.assertNotIn("订单未完成", body["msg"])
        hidden_perf.refresh_from_db()
        self.assertEqual(hidden_perf.settle_status, SettleStatus.UNSETTLED)
        hidden.refresh_from_db()
        self.assertIsNotNone(hidden.hall_card_id, "夹具自证：它确实是一张隐藏单")
