"""闲管家推送 **22 分支（交易成功 → 收款）** 的行为网
（spec `2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §5 / §9 的新网清单）。

逐条落点（spec §9 → 本文件的用例）：

| 判据 | 用例 |
|---|---|
| 匹配（与退款同一套 BTRIM 归一、命中全部行逐张改） | `ReceiptMatchTests` |
| 幂等（已 SETTLED / 已 REFUNDED 跳过；重复推送不双算） | `ReceiptMatchTests` / `ReceiptDedupTests` |
| 去重（唯一键 app_key+order_no；**删绑定重建后同单重推不双算**，评审 H-2） | `ReceiptDedupTests` |
| 金额回填（回查线程体同步直调；金额 = 买家实付减已退 §12 F1-D；`shop_identity` 回写 F2-D） | `ReceiptAmountBackfillTests` |
| 响应契约不变（`{"result","msg"}` 两键、恒 success） | `ReceiptResponseContractTests` |
| 23/24 回归一字不动 | `ReceiptResponseContractTests.test_refund_paths_still_work_after_the_branch` |
| 没匹配到也照样记账（§3 默认 B：暴露漏录单） | `ReceiptMatchTests.test_an_unmatched_push_still_records` |
| 交易成功时间（modify_time 秒级整数 → aware UTC；认不出留空） | `ReceiptTimeTests` |
| 补查命令（重试空金额行；已有金额不动；`--refetch-all` 重算脏行 §12 F3-D） | `BackfillCommandTests` |

## 三条纪律（沿 `test_xgj_push_inbound.py` 的同款）

- **副作用判据打在数据库行上**（E-0009）：改没改状态、记没记账、写没写日志，一律断行。
- **夹具值一律合成**（E-0077）：AppKey / AppSecret / 单号 / 时间戳全仿形状；⛔ 不跑
  `tests.test_xgj_live`（那要真凭据）。
- **`mock.patch` 打在「被查找的那个模块」上**（E-0029）：出站 `order_detail` 在
  `xgj_push` 里是 `from … import` 进来的名字 ⇒ 桩打在 `apps.business.xgj_push.order_detail`，
  并**先证**它确实就是被查找的那个对象。

⚠️ **线程在测试里从不真起**：`TenantTestCase` 把每条用例包在一层事务里，
`transaction.on_commit` 的回调默认不执行（先例见 `xgj_ship.schedule_auto_ship` 的说明）
⇒ 回查一律**同步直调** `fetch_receipt_amount`（`run_receipt_amount_fetch(schema_name=None)`
的既有可直调缝隙）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from urllib.parse import urlencode
from unittest import mock

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.models import OperationLog
from apps.business import xgj as xgj_api
from apps.business import xgj_push
from apps.business.models import (
    Game,
    Order,
    OrderSource,
    OrderStatus,
    PaymentStatus,
    XgjReceiptRecord,
    XgjShop,
    XgjShopBinding,
)
from apps.business.xgj import XgjError
from django_tenants.test.cases import TenantTestCase

ORDER_URL = "/api/business/xgj/push/order/"
DOMAIN_A = "tenant.test.com"

#: 合成凭据 / 单号 / 时间戳（⛔ 不是任何真实应用或订单）。
APP_KEY = "AK-RECEIPT-TEST-1"
APP_SECRET = "SK-RECEIPT-TEST-1"
SOURCE_NO = "33000000000000000001"
SOURCE_NO_OTHER = "33000000000000000002"
#: 秒级时间戳（文档：`modify_time` 是 int32 秒级；形状仿 2026 年）。
MODIFY_TS = 1780000000
MODIFY_TS_LATER = 1780000100
TS = "1780000000"

_ORDER_SEQ = iter(range(1, 1000))


def _order_no() -> str:
    return "2027" + f"{next(_ORDER_SEQ):016d}"


def _md5(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def sign_of(body: bytes) -> str:
    """用例侧独立实现签名（⛔ 不调 `xgj_push.sign_for`，防自指断言；同 inbound 网的口径）。"""
    return _md5(f"{APP_KEY},{_md5(body)},{TS},{APP_SECRET}")


class _PushFixture(TenantTestCase):
    """「有应用凭据」的租户夹具 + 推送请求的小工具（照 inbound 网的同款）。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="收款测试游戏甲")
        self.source = OrderSource.objects.create(name="收款测试来源甲")
        self.binding = XgjShopBinding.objects.create(
            shop_name="收款测试应用甲", app_key=APP_KEY, app_secret=APP_SECRET,
        )

    def make_order(self, *, source_order_no: str = SOURCE_NO,
                   status: str = OrderStatus.UNASSIGNED,
                   payment_status: str = PaymentStatus.UNSETTLED, **extra) -> Order:
        return Order.objects.create(
            game=self.game, order_no=_order_no(), status=status,
            payment_status=payment_status,
            order_source=self.source, source_order_no=source_order_no, **extra,
        )

    @staticmethod
    def receipt_body(*, order_no: str = SOURCE_NO, seller_id="660001",
                     user_name="合成卖家甲", modify_time=MODIFY_TS) -> bytes:
        """一条 22 推送：**modify_time 按文档用秒级整数**（评审 H-3 点名的新形状）。"""
        return json.dumps(
            {
                "seller_id": seller_id,
                "user_name": user_name,
                "order_no": order_no,
                "order_type": "1",
                "order_status": 22,
                "refund_status": "0",
                "modify_time": modify_time,
                "product_id": "219530767978565",
                "item_id": "1",
            },
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")

    def push_receipt(self, body: bytes):
        query = urlencode({"appid": APP_KEY, "timestamp": TS, "sign": sign_of(body)})
        return self.client.post(
            f"{ORDER_URL}?{query}", body, content_type="application/json",
            SERVER_NAME=DOMAIN_A,
        )

    # ── 判据小工具 ────────────────────────────────────────────────────
    @staticmethod
    def actions_of(order_no: str) -> list[str]:
        return list(
            OperationLog.objects.filter(order_no=order_no)
            .order_by("id").values_list("action", flat=True)
        )

    @staticmethod
    def the_record() -> XgjReceiptRecord:
        """本租户唯一那条记账行（本网每条用例只推一个单号）。"""
        return XgjReceiptRecord.objects.get()


# ── 匹配与改状态 ───────────────────────────────────────────────────────────

class ReceiptMatchTests(_PushFixture):
    def test_a_receipt_push_marks_the_order_settled_and_records_it(self):
        order = self.make_order()
        response = self.push_receipt(self.receipt_body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        # 判据打在数据库行上（E-0009）：收款状态改了、**订单状态**一个字没动。
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # 两行日志：① `change_payment_status`（领域函数自写）② 新动作码的人话（§4-D3）。
        self.assertEqual(
            self.actions_of(order.order_no),
            ["order.change_payment_status", "order.xgj_push_receipt"],
        )
        receipt_log = OperationLog.objects.get(
            order_no=order.order_no, action="order.xgj_push_receipt"
        )
        self.assertIn("交易成功", receipt_log.summary)
        self.assertEqual(receipt_log.actor, xgj_push.SYSTEM_ACTOR)
        payment_log = OperationLog.objects.get(
            order_no=order.order_no, action="order.change_payment_status"
        )
        self.assertEqual(payment_log.to_value, "已结款")

        # 记账行：三元组 + 原文 + 匹配结果。
        record = self.the_record()
        self.assertEqual(record.app_key, APP_KEY)
        self.assertEqual(record.order_no, SOURCE_NO)
        self.assertEqual(record.seller_id, "660001")
        self.assertEqual(record.user_name, "合成卖家甲")
        self.assertEqual(record.binding_id, self.binding.pk)
        self.assertEqual(record.matched_count, 1)
        self.assertEqual(record.match_result, [
            {"order_no": order.order_no, "result": "updated"},
        ])
        self.assertEqual(record.raw_push["order_no"], SOURCE_NO)

    def test_matching_reuses_the_refund_trim_rules(self):
        """库列带首尾空白也命中（与退款同一份 BTRIM 判据，spec §4-D5）。"""
        order = self.make_order(source_order_no=f"\u3000{SOURCE_NO}\u3000")
        self.push_receipt(self.receipt_body())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)

    def test_multiple_matches_all_updated(self):
        first = self.make_order(source_order_no=SOURCE_NO)
        second = self.make_order(source_order_no=SOURCE_NO)
        self.push_receipt(self.receipt_body())
        for order in (first, second):
            order.refresh_from_db()
            self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(self.the_record().matched_count, 2)

    def test_an_already_settled_order_is_skipped_without_new_logs(self):
        """幂等（§3-D）：收款状态已是「已结款」⇒ 跳过（连 save 都免）。"""
        order = self.make_order(payment_status=PaymentStatus.SETTLED)
        logs_before = OperationLog.objects.count()
        self.push_receipt(self.receipt_body())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(OperationLog.objects.count(), logs_before)  # 零日志
        self.assertEqual(self.the_record().match_result, [
            {"order_no": order.order_no, "result": "settled_skip"},
        ])

    def test_a_refunded_order_is_skipped(self):
        """已退单 ⇒ 跳过（§3-D：退款跟退由 23/24 线负责，两条线互不掺和）。"""
        order = self.make_order(status=OrderStatus.REFUNDED)
        logs_before = OperationLog.objects.count()
        self.push_receipt(self.receipt_body())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(OperationLog.objects.count(), logs_before)
        self.assertEqual(self.the_record().match_result, [
            {"order_no": order.order_no, "result": "refunded_skip"},
        ])

    def test_an_unmatched_push_still_records(self):
        """§3 默认 B：没匹配到系统订单 ⇒ **照样记账**（钱确实到店、顺带暴露漏录单）。"""
        logs_before = OperationLog.objects.count()
        orders_before = Order.objects.count()
        self.push_receipt(self.receipt_body(order_no=SOURCE_NO_OTHER))
        record = self.the_record()
        self.assertEqual(record.order_no, SOURCE_NO_OTHER)
        self.assertEqual(record.matched_count, 0)
        self.assertEqual(record.match_result, [])
        self.assertEqual(OperationLog.objects.count(), logs_before)
        self.assertEqual(Order.objects.count(), orders_before)

    def test_an_empty_order_no_writes_nothing(self):
        """空单号 ⇒ 照旧 ack、零写入（与 23/24 的空值闸同形）。"""
        self.push_receipt(self.receipt_body(order_no=""))
        self.push_receipt(self.receipt_body(order_no="   "))
        self.assertEqual(XgjReceiptRecord.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_a_settling_order_is_also_marked_settled(self):
        """§3-D：未收款 / 结算中 → 都改成「已结款」（这条测「结算中」）。"""
        order = self.make_order(payment_status=PaymentStatus.SETTLING)
        self.push_receipt(self.receipt_body())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)


# ── 去重（唯一约束是钱面统计的承重闸）─────────────────────────────────────

class ReceiptDedupTests(_PushFixture):
    def test_a_repeat_push_updates_the_same_row(self):
        """厂商「任一变化都推」⇒ 同一单的 22 推多次：**更新旧行**，⛔ 不重复计数（§4-D2）。"""
        self.push_receipt(self.receipt_body())
        self.push_receipt(self.receipt_body(modify_time=MODIFY_TS_LATER))
        self.assertEqual(XgjReceiptRecord.objects.count(), 1)
        record = self.the_record()
        self.assertEqual(
            record.success_time,
            datetime.fromtimestamp(MODIFY_TS_LATER, tz=dt_timezone.utc),
        )

    def test_rebuilding_the_binding_does_not_double_count(self):
        """评审 H-2：删绑定再重建（同 app_key）后同单重推 ⇒ 仍是**同一行**、订单不重复处理。

        ⚠️ 判据打在数据库行上：行数 = 1；订单收款状态不变（已 SETTLED ⇒ settled_skip）；
        行的 `binding` 换成**新**绑定（update 侧刷新归属）。
        """
        order = self.make_order()
        response = self.push_receipt(self.receipt_body())
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        self.assertEqual(XgjReceiptRecord.objects.count(), 1)
        old_binding_id = self.the_record().binding_id

        # 删绑定（SET_NULL 生效）→ 重建（同 app_key、新 id）。
        self.binding.delete()
        self.the_record().refresh_from_db()
        self.assertIsNone(self.the_record().binding_id)
        new_binding = XgjShopBinding.objects.create(
            shop_name="收款测试应用甲-重建", app_key=APP_KEY, app_secret=APP_SECRET,
        )
        self.assertNotEqual(new_binding.pk, old_binding_id)

        response = self.push_receipt(self.receipt_body())
        self.assertEqual(
            response.json(), {"result": "success", "msg": "接收成功"},
            f"重建绑定后的重推没有 success：{response.content!r}",
        )
        self.assertEqual(XgjReceiptRecord.objects.count(), 1)  # ⭐ 不双算
        record = self.the_record()
        self.assertEqual(record.binding_id, new_binding.pk)
        # 订单侧：唯一那张单已 SETTLED ⇒ 本轮 settled_skip，没有新日志。
        self.assertEqual(record.match_result, [
            {"order_no": order.order_no, "result": "settled_skip"},
        ])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        # 两次推送只产生**一次**改款的两行日志（重推没有第二次改动）。
        self.assertEqual(
            self.actions_of(order.order_no),
            ["order.change_payment_status", "order.xgj_push_receipt"],
        )


# ── 响应契约 / 23-24 回归 ─────────────────────────────────────────────────

class ReceiptResponseContractTests(_PushFixture):
    def test_the_response_contract_is_unchanged(self):
        """响应是**第三方形状**：键集恰好 `{"result","msg"}`、22 照旧 HTTP 200 + success。"""
        response = self.push_receipt(self.receipt_body())
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body, {"result": "success", "msg": "接收成功"})
        self.assertEqual(set(body), {"result", "msg"})
        self.assertNotIn("code", body)

    def test_numeric_fields_are_accepted(self):
        """`seller_id` / `modify_time` 用 JSON 数字推送也要认（DRF CharField 收口成字符串）。"""
        body = json.dumps(
            {"seller_id": 660002, "user_name": "合成卖家乙", "order_no": SOURCE_NO,
             "order_status": 22, "modify_time": MODIFY_TS},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        self.assertIn(b'"modify_time":1780000000', body)  # 自证：发出去的确实是数字
        self.push_receipt(body)
        record = self.the_record()
        self.assertEqual(record.seller_id, "660002")
        self.assertEqual(
            record.success_time,
            datetime.fromtimestamp(MODIFY_TS, tz=dt_timezone.utc),
        )

    def test_refund_paths_still_work_after_the_branch(self):
        """23/24 回归（spec §4-D1：退款路径一字未动）：22 推过之后 23 照常自动退单。

        ⚠️ 顺序有意义：先推 22（改收款状态）再推 23（改订单状态）——两分支各写各的列，
        22 的记账行不受影响；退款两行日志的形状与 inbound 网一致。
        """
        order = self.make_order()
        self.push_receipt(self.receipt_body())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)

        refund_body = json.dumps(
            {"order_no": SOURCE_NO, "order_status": 23},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        response = self.push_receipt(refund_body)
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)  # 退款不碰收款状态
        self.assertEqual(XgjReceiptRecord.objects.count(), 1)  # 23 不写记账行
        # 22 的两行 + 退款的两行。
        self.assertEqual(
            self.actions_of(order.order_no),
            [
                "order.change_payment_status", "order.xgj_push_receipt",
                "order.transition", "order.xgj_push_refund",
            ],
        )


# ── 交易成功时间 ───────────────────────────────────────────────────────────

class ReceiptTimeTests(_PushFixture):
    def test_modify_time_seconds_become_aware_utc(self):
        """秒级整数 → **aware UTC** 存库（期望值用标准库独立算，⛔ 不调被测函数）。"""
        self.push_receipt(self.receipt_body())
        record = self.the_record()
        self.assertEqual(
            record.success_time,
            datetime.fromtimestamp(MODIFY_TS, tz=dt_timezone.utc),
        )
        self.assertIsNotNone(record.success_time.utcoffset())

    def test_an_unparsable_modify_time_leaves_null(self):
        """认不出（非数字 / 越界）⇒ `None`（不猜）；行照记。"""
        self.push_receipt(self.receipt_body(modify_time="not-a-number"))
        self.assertIsNone(self.the_record().success_time)
        self.assertEqual(XgjReceiptRecord.objects.count(), 1)

    def test_an_unparsable_modify_time_on_a_repeat_push_keeps_the_stored_time(self):
        """评审 L-2：重推的 `modify_time` 认不出 ⇒ **保留**已存的交易成功时间。

        ⚠️ 把 `None` 灌进 defaults 会把已存的 `success_time` 覆盖掉——那一行从此
        掉出**所有**统计窗口（钱记了账却查不到）⇒ 解析失败时不写这个键、留旧值。
        """
        self.push_receipt(self.receipt_body())
        record = self.the_record()
        self.assertEqual(
            record.success_time,
            datetime.fromtimestamp(MODIFY_TS, tz=dt_timezone.utc),
        )
        self.push_receipt(self.receipt_body(modify_time="not-a-number"))
        self.assertEqual(XgjReceiptRecord.objects.count(), 1)  # 仍是同一行
        record.refresh_from_db()
        self.assertEqual(
            record.success_time,
            datetime.fromtimestamp(MODIFY_TS, tz=dt_timezone.utc),
        )

    def test_a_newer_parsable_modify_time_still_refreshes_the_stored_time(self):
        """对照：解析**得出来**的新时间照旧刷新（L-2 不收紧正常路径）。"""
        self.push_receipt(self.receipt_body())
        self.push_receipt(self.receipt_body(modify_time=MODIFY_TS_LATER))
        self.assertEqual(
            self.the_record().success_time,
            datetime.fromtimestamp(MODIFY_TS_LATER, tz=dt_timezone.utc),
        )

    def test_a_negative_modify_time_leaves_null(self):
        self.push_receipt(self.receipt_body(modify_time=-5))
        self.assertIsNone(self.the_record().success_time)


# ── 唯一约束的承重钉子（评审 M-1；先例 = test_game_config / test_home_qr_codes）────

class UniqueConstraintPinTests(_PushFixture):
    def test_duplicate_app_key_and_order_no_is_rejected_by_the_database(self):
        """`unique_xgj_receipt_appkey_orderno` 的**钉子**：删掉约束这条必须当场红。

        ⚠️ 上一层的「去重」行为判据（`ReceiptDedupTests`）走的是 `update_or_create`
        ⇒ 它对「约束还在不在」**不敏感**（约束被删它照样绿，靠的是 upsert 语义而不是
        约束本身）。这一条**绕开应用层**直接 `create` 两次，把判据打在**数据库约束**上——
        钱面统计不双算的最后一道闸，必须有自己能红的用例（照 `test_game_config.py:19`
        的同款形状：`assertRaises(IntegrityError)` + `atomic()` 包住第二次 create）。
        """
        XgjReceiptRecord.objects.create(app_key=APP_KEY, order_no=SOURCE_NO)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                XgjReceiptRecord.objects.create(app_key=APP_KEY, order_no=SOURCE_NO)


# ── 推送 → on_commit 排程回查这根线的钉子────────────────────────

class ReceiptScheduleTests(_PushFixture):
    """「22 推送把金额回查**排到事务提交后**」自己的判据。

    ⚠️ 既有回查用例全是**同步直调** `fetch_receipt_amount`——把 `xgj_push.py` 里那行
    `transaction.on_commit(...)` 删掉它们**照样全绿**（那根线没有判据）。本类照
    `test_xgj_auto_ship.py` 的手法补上：`dispatch_receipt_amount_fetch` 替成
    **同步直调替身**（⛔ 不替成 `_receipt_amount_in_thread`：它 finally 里的
    `connections.close_all()` 会把测试自己的事务连接关掉），再用
    `captureOnCommitCallbacks(execute=True)` 真正执行排下的回调。
    """

    def test_the_push_schedules_and_runs_the_amount_fetch_on_commit(self):
        """推一笔 22 ⇒ 回查**真的被排上**（stub 恰好一次）**并被执行**（金额落库）。"""
        recorded: list[int] = []

        def inline_fetch(record_id: int) -> bool:
            recorded.append(record_id)
            return xgj_push.fetch_receipt_amount(record_id)

        detail = {"order_no": SOURCE_NO, "pay_amount": 321}
        with mock.patch.object(xgj_push, "order_detail", return_value=detail):
            with mock.patch.object(
                xgj_push, "dispatch_receipt_amount_fetch", side_effect=inline_fetch
            ) as stub:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.push_receipt(self.receipt_body())
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        # ① 排程线被接上：删掉 `transaction.on_commit(...)` 那行，这条当场红。
        stub.assert_called_once()
        record = self.the_record()
        self.assertEqual(recorded, [record.pk])
        # ② 排下的回调真的执行了取数（金额落库，不是只排了没跑）。
        record.refresh_from_db()
        self.assertEqual(record.amount, Decimal("3.21"))
        self.assertIsNotNone(record.amount_fetched_at)

    def test_without_executing_the_callbacks_nothing_fetches(self):
        """对照（证 capture 是承重的）：不执行回调 ⇒ 排程函数与出站**一次都不跑**。

        `TenantTestCase` 把用例包在一层事务里 ⇒ on_commit 回调默认不执行——
        这条把「默认不执行」本身钉住，让上一条的 `captureOnCommitCallbacks` 显出判据力。
        """
        with mock.patch.object(xgj_push, "order_detail") as stub_outbound:
            with mock.patch.object(xgj_push, "dispatch_receipt_amount_fetch") as stub_sched:
                self.push_receipt(self.receipt_body())
        stub_sched.assert_not_called()
        stub_outbound.assert_not_called()
        self.assertIsNone(self.the_record().amount)


# ── 金额回填（线程体同步直调；先例 = test_xgj_auto_ship 的替身口径）──────────

class ReceiptAmountBackfillTests(_PushFixture):
    def setUp(self):
        super().setUp()
        self.shop = XgjShop.objects.create(
            binding=self.binding, user_identity="EID-SHOP-1",
            shop_name="合成店铺·壹号", user_name="合成会员甲",
        )

    def _record(self) -> XgjReceiptRecord:
        self.push_receipt(self.receipt_body())
        return self.the_record()

    def test_the_stub_is_planted_on_the_module_that_looks_the_name_up(self):
        """桩打在**被查找的那个模块**上（E-0029）——先证「回查查的就是这个名字」。"""
        self.assertIs(xgj_push.order_detail, xgj_api.order_detail)

    def test_amount_is_pay_minus_refund_and_identity_is_written(self):
        """回查成功（F1-D / F2-D）：金额 = (买家实付 − 已退) / 100；`seller_eid` 写进
        `shop_identity`（分组键）；店名主键 = `XgjShop.user_identity` 命中。"""
        record = self._record()
        detail = {
            "order_no": SOURCE_NO,
            "pay_amount": 1234,       # 买家实付 12.34 元（分；数字型）
            "refund_amount": "0",     # 已退 0（字符串型——上游两型都收）
            "seller_eid": "EID-SHOP-1",
            "order_status": 22,
        }
        with mock.patch.object(
            xgj_push, "order_detail", return_value=detail
        ) as stub:
            ok = xgj_push.fetch_receipt_amount(record.pk)
        self.assertTrue(stub.called, "桩没被调到——这条用例没测到回查路径")
        self.assertTrue(ok)
        record.refresh_from_db()
        self.assertEqual(record.amount, Decimal("12.34"))
        self.assertIsNotNone(record.amount_fetched_at)
        self.assertEqual(record.shop_identity, "EID-SHOP-1")
        self.assertEqual(record.shop_name_snapshot, "合成店铺·壹号")

    def test_the_probe_case_is_ten_yuan_not_zero(self):
        """F-1 的实弹探针单形状：实付 1000 分、已退 0 分 ⇒ **10.00**（不再恒 0.00）。"""
        detail = {"order_no": SOURCE_NO, "pay_amount": 1000, "refund_amount": 0}
        self.assertEqual(xgj_push._detail_amount(detail), Decimal("10.00"))

    def test_partial_refund_is_deducted_and_negative_diff_is_kept(self):
        """部分退款照减；已退 > 实付的负差**照存**（本仓金额可为负，D-0015）。"""
        self.assertEqual(
            xgj_push._detail_amount({"pay_amount": 1000, "refund_amount": 250}),
            Decimal("7.50"),
        )
        self.assertEqual(
            xgj_push._detail_amount({"pay_amount": 0, "refund_amount": 500}),
            Decimal("-5.00"),
        )

    def test_refund_missing_or_garbage_counts_as_zero(self):
        """`refund_amount` 缺失 / 认不出 ⇒ 按 **0**（没退就是 0，不是缺失）。"""
        self.assertEqual(xgj_push._detail_amount({"pay_amount": 1000}), Decimal("10.00"))
        self.assertEqual(
            xgj_push._detail_amount({"pay_amount": 1000, "refund_amount": "abc"}),
            Decimal("10.00"),
        )
        self.assertEqual(
            xgj_push._detail_amount({"pay_amount": 1000, "refund_amount": None}),
            Decimal("10.00"),
        )

    def test_pay_amount_garbage_means_missing_not_zero(self):
        """`pay_amount` 认不出 ⇒ `None`（金额**缺失**，⛔ 不猜 0）。"""
        for raw in (None, True, "", "abc", "1.2.3", "NaN", "Infinity", [], {}):
            with self.subTest(raw=raw):
                self.assertIsNone(xgj_push._detail_amount({"pay_amount": raw}))
        # 分→元的进位：`ROUND_HALF_UP`（12.345 元 → 12.35；banker's 会给 12.34）。
        self.assertEqual(
            xgj_push._detail_amount({"pay_amount": "1234.5"}), Decimal("12.35")
        )

    def test_identity_is_written_even_when_amount_is_missing(self):
        """金额认不出 ≠ 店铺认不出：`seller_eid` 非空就写 `shop_identity`（两件独立的事）；
        详情没给 `seller_eid` ⇒ `shop_identity` 留空（NULL，未识别桶）。"""
        record = self._record()
        with mock.patch.object(
            xgj_push, "order_detail",
            return_value={"order_no": SOURCE_NO, "seller_eid": " EID-SHOP-1 "},
        ):
            ok = xgj_push.fetch_receipt_amount(record.pk)
        self.assertFalse(ok)  # 金额缺失
        record.refresh_from_db()
        self.assertIsNone(record.amount)
        self.assertIsNone(record.amount_fetched_at)
        self.assertEqual(record.shop_identity, "EID-SHOP-1")  # strip 后非空才写
        self.assertEqual(record.shop_name_snapshot, "合成店铺·壹号")

        other = self.push_receipt(self.receipt_body(order_no=SOURCE_NO_OTHER))
        self.assertEqual(other.json(), {"result": "success", "msg": "接收成功"})
        second = XgjReceiptRecord.objects.exclude(pk=record.pk).get()
        with mock.patch.object(
            xgj_push, "order_detail",
            return_value={"order_no": SOURCE_NO_OTHER, "pay_amount": 500},
        ):
            xgj_push.fetch_receipt_amount(second.pk)
        second.refresh_from_db()
        self.assertIsNone(second.shop_identity)

    def test_a_detail_without_the_amount_leaves_it_empty(self):
        """上游没给金额键 ⇒ 留空（缺失**可见**，不虚增也不丢单）。"""
        record = self._record()
        with mock.patch.object(
            xgj_push, "order_detail",
            return_value={"order_no": SOURCE_NO, "seller_eid": "EID-SHOP-1"},
        ):
            ok = xgj_push.fetch_receipt_amount(record.pk)
        self.assertFalse(ok)
        record.refresh_from_db()
        self.assertIsNone(record.amount)
        self.assertIsNone(record.amount_fetched_at)

    def test_an_upstream_failure_is_swallowed_with_empty_amount(self):
        """上游业务失败（`XgjError`）⇒ 不抛、留空金额（补查命令兜底）。"""
        record = self._record()
        with mock.patch.object(
            xgj_push, "order_detail",
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            ok = xgj_push.fetch_receipt_amount(record.pk)
        self.assertFalse(ok)
        record.refresh_from_db()
        self.assertIsNone(record.amount)

    def test_a_deleted_binding_cannot_fetch(self):
        """绑定已删（SET_NULL）⇒ 取不到密钥，不打出站（桩没被调），留空。"""
        record = self._record()
        record.binding = None
        record.save(update_fields=["binding", "updated_at"])
        with mock.patch.object(xgj_push, "order_detail") as stub:
            ok = xgj_push.fetch_receipt_amount(record.pk)
        self.assertFalse(ok)
        self.assertFalse(stub.called, "没有密钥还打出站=拿什么签的名？这条不许发生")

    def test_the_run_entry_falls_the_schema_when_given(self):
        """`run_receipt_amount_fetch(schema_name=…)` 落 schema 后复用同一本体
        （线程体的可直调缝隙；本条在测试线程里同步直调）。"""
        record = self._record()
        detail = {"order_no": SOURCE_NO, "pay_amount": 780}
        with mock.patch.object(xgj_push, "order_detail", return_value=detail):
            xgj_push.run_receipt_amount_fetch(
                record_id=record.pk, schema_name=self.tenant.schema_name,
            )
        record.refresh_from_db()
        self.assertEqual(record.amount, Decimal("7.80"))


# ── 补查命令 ───────────────────────────────────────────────────────────────

class BackfillCommandTests(_PushFixture):
    def _push_two(self) -> tuple[XgjReceiptRecord, XgjReceiptRecord]:
        """两行：一笔（甲）给回查得到金额；一笔（乙）留在空金额。"""
        self.push_receipt(self.receipt_body())
        first = self.the_record()
        self.push_receipt(self.receipt_body(order_no=SOURCE_NO_OTHER))
        second = XgjReceiptRecord.objects.exclude(pk=first.pk).get()
        return first, second

    def test_it_refills_only_rows_with_empty_amount(self):
        """重试 amount 为空的行；**已有金额的行一律不重查**（桩只对空行被调）。"""
        first, second = self._push_two()
        calls: list[str] = []

        def fake_detail(order_no, *, app_key, app_secret, **kwargs):
            calls.append(order_no)
            return {"order_no": order_no, "pay_amount": 990}

        with mock.patch.object(xgj_push, "order_detail", side_effect=fake_detail):
            out = StringIO()
            call_command(
                "backfill_xgj_receipt_amounts",
                schema=self.tenant.schema_name,
                stdout=out,
            )
        # 两行都是空的 ⇒ 都被查（第一行在推送线程之外没有金额，命令是它的第一次机会）。
        self.assertEqual(sorted(calls), sorted([SOURCE_NO, SOURCE_NO_OTHER]))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.amount, Decimal("9.90"))
        self.assertEqual(second.amount, Decimal("9.90"))
        self.assertIn("回填金额 2 行", out.getvalue())

    def test_it_skips_rows_that_already_have_amounts(self):
        """`amount IS NOT NULL` 的行**不重查**（判据：桩只为空行被调、金额原样）。

        ⚠️ 甲的既有金额故意用 **0.00**（F-1 实弹首日的脏值形状）：不加 `--refetch-all`
        时它同样是「已回查过的事实」⇒ 照样跳过；重算脏值是 `--refetch-all` 那条路的事。
        """
        first, second = self._push_two()
        first.amount = Decimal("0.00")
        first.amount_fetched_at = timezone.now()
        first.save(update_fields=["amount", "amount_fetched_at", "updated_at"])

        calls: list[str] = []

        def fake_detail(order_no, **kwargs):
            calls.append(order_no)
            return {"order_no": order_no, "pay_amount": 990}

        with mock.patch.object(xgj_push, "order_detail", side_effect=fake_detail) as stub:
            out = StringIO()
            call_command(
                "backfill_xgj_receipt_amounts",
                schema=self.tenant.schema_name,
                stdout=out,
            )
        # 桩**恰好**为乙那一行被调；甲（已有金额，哪怕是脏的 0.00）一次都没查。
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(calls, [SOURCE_NO_OTHER])
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("0.00"))
        self.assertIn("回填金额 1 行", out.getvalue())

    def test_refetch_all_clears_then_rewrites_dirty_rows(self):
        """`--refetch-all`（F3-D）：先清空选中行的金额/回查时间/店铺标识，再逐行回查重写。

        - 能查到的行（甲）：0.00 脏值 → 正确值，`shop_identity` 回写；
        - 回查失败的行（乙）：清空后取不到 ⇒ 金额**缺失可见**（不猜数），店铺标识留空。
        """
        first, second = self._push_two()
        # 店铺列表有这一家（`user_identity` = 详情 `seller_eid` 同源值）⇒ 回查后店名真命中。
        XgjShop.objects.create(
            binding=self.binding, user_identity="EID-SHOP-1",
            shop_name="合成店铺·壹号", user_name="合成会员甲",
        )
        dirty_time = timezone.now()
        for row in (first, second):
            row.amount = Decimal("0.00")
            row.amount_fetched_at = dirty_time
            row.shop_identity = "EID-STALE-DIRTY"
            row.save(update_fields=["amount", "amount_fetched_at", "shop_identity", "updated_at"])

        def fake_detail(order_no, **kwargs):
            if order_no == SOURCE_NO:
                return {
                    "order_no": order_no,
                    "pay_amount": 1000,
                    "refund_amount": 0,
                    "seller_eid": "EID-SHOP-1",
                }
            raise XgjError(upstream_code=100001, upstream_msg="签名错误")

        with mock.patch.object(xgj_push, "order_detail", side_effect=fake_detail):
            out = StringIO()
            call_command(
                "backfill_xgj_receipt_amounts",
                schema=self.tenant.schema_name,
                refetch_all=True,
                stdout=out,
            )
        first.refresh_from_db()
        self.assertEqual(first.amount, Decimal("10.00"))  # 脏 0.00 → 探针单的正确值
        self.assertIsNotNone(first.amount_fetched_at)
        self.assertNotEqual(first.amount_fetched_at, dirty_time)
        self.assertEqual(first.shop_identity, "EID-SHOP-1")
        self.assertEqual(first.shop_name_snapshot, "合成店铺·壹号")
        second.refresh_from_db()
        self.assertIsNone(second.amount)              # 回查失败 = 缺失可见
        self.assertIsNone(second.amount_fetched_at)
        self.assertIsNone(second.shop_identity)       # 脏标识也被清掉，不再误导分组
        self.assertIn("清空待重查 2 行", out.getvalue())
        self.assertIn("回填金额 1 行", out.getvalue())

    def test_an_upstream_failure_does_not_crash_the_command(self):
        """上游失败被回查本体吞掉 ⇒ 命令跑完、该行留空（下次再试）。"""
        _, second = self._push_two()
        with mock.patch.object(
            xgj_push, "order_detail",
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            out = StringIO()
            call_command(
                "backfill_xgj_receipt_amounts",
                schema=self.tenant.schema_name,
                stdout=out,
            )
        second.refresh_from_db()
        self.assertIsNone(second.amount)
        self.assertIn("回填金额 0 行", out.getvalue())
