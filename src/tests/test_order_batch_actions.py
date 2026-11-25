"""批量端点主干网（spec §7-T1）。基线见「测试夹具基线」一节。"""
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from apps.audit.models import OperationLog
from apps.business.api.order_batch import run_batch
from apps.business.models import (
    EmpType, Game, Order, OrderRefund, OrderStatus, Performance, PerformanceAdjustment,
)
from apps.business.order_status import (
    COMPLETE_REQUIRES_REVIEW_MESSAGE,
    SELF_TRANSITION_MESSAGE,
)
from apps.business.order_writes import WriteOutcome
from apps.business.validation import ValidationProblem
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.test_order_batch_base import BATCH, CTX, OrderBatchTestBase

BATCH_PRICE_URL = f"{BATCH}/price/"
BATCH_PAYMENT_STATUS_URL = f"{BATCH}/payment-status/"
BATCH_TYPES_URL = f"{BATCH}/types/"
BATCH_HALL_PRICE_URL = f"{BATCH}/hall-price/"
BATCH_DELETE_URL = f"{BATCH}/delete/"
V2_BATCH_DELETE_URL = f"{BATCH}/delete-v2/"
BATCH_TRANSITION_URL = f"{BATCH}/transition/"
BATCH_ASSIGN_URL = f"{BATCH}/assign/"
BATCH_REFUNDS_URL = f"{BATCH}/refunds/"
BATCH_RECOMPUTE_URL = f"{BATCH}/recompute-commission/"


class BatchSplitConfirmationTests(OrderBatchTestBase):
    split_id = "20260926000000009999"

    def _effect(self, order, operation):
        return {
            "order_no": order.order_no,
            "source_order_no": order.source_order_no,
            "operation": operation,
        }

    def test_legacy_parent_delete_requires_confirming_all_cascaded_children(self):
        main = self._order(split_id=self.split_id, split_kind="LEGACY")
        child = self._order(split_id=self.split_id, split_kind="LEGACY", source_order_no="TEST-CHILD")
        args = {"order_nos": [main.order_no], "isDelEmp": "N", "confirmed_effects": []}
        preview = self._post(BATCH_DELETE_URL, {**args, "dry_run": True})
        effects = [self._effect(child, "随拆单主单删除")]
        self.assertEqual(preview["data"]["effects"], effects)
        self.assertEqual(preview["data"]["eligible"], [main.order_no])

        late_child = self._order(split_id=self.split_id, split_kind="LEGACY")
        stale = self._post(BATCH_DELETE_URL, {**args, "dry_run": False, "confirmed_effects": effects})
        self.assertEqual(stale["code"], 1002, stale)
        self.assertEqual(stale["data"]["applied"], [])
        self.assertFalse(OperationLog.objects.filter(action="order.delete").exists())
        for member in (main, child, late_child):
            member.refresh_from_db()
            self.assertIsNone(member.deleted_at)

        preview = self._post(BATCH_DELETE_URL, {**args, "dry_run": True})
        effects.append(self._effect(late_child, "随拆单主单删除"))
        self.assertEqual(preview["data"]["effects"], effects)
        result = self._post(BATCH_DELETE_URL, {**args, "dry_run": False, "confirmed_effects": effects})
        self.assertEqual(result["data"]["applied"], [main.order_no])
        for member in (main, child, late_child):
            member.refresh_from_db()
            self.assertIsNotNone(member.deleted_at)

    def test_child_price_previews_anchor_and_requires_that_exact_effect(self):
        main = self._order(split_id=self.split_id, split_kind="PEER", total_price=Decimal("100.00"))
        child = self._order(split_id=self.split_id, split_kind="PEER", total_price=Decimal("10.00"))
        args = {"order_nos": [child.order_no], "total_price": "25.00", "confirmed_effects": []}
        preview = self._post(BATCH_PRICE_URL, {**args, "dry_run": True})
        effects = [self._effect(main, "自动调平接单价")]
        self.assertEqual(preview["data"]["effects"], effects)
        refused = self._post(BATCH_PRICE_URL, {**args, "dry_run": False})
        self.assertEqual(refused["code"], 1002, refused)
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual((main.total_price, child.total_price), (Decimal("100.00"), Decimal("10.00")))

        result = self._post(BATCH_PRICE_URL, {**args, "dry_run": False, "confirmed_effects": effects})
        self.assertEqual(result["code"], 0, result)
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual((main.total_price, child.total_price), (Decimal("85.00"), Decimal("25.00")))

    def test_delete_child_previews_price_return_and_group_dissolution_together(self):
        main = self._order(split_id=self.split_id, split_kind="LEGACY", total_price=Decimal("100.00"))
        child = self._order(split_id=self.split_id, split_kind="LEGACY", total_price=Decimal("10.00"))
        args = {"order_nos": [child.order_no], "isDelEmp": "N", "confirmed_effects": []}
        preview = self._post(BATCH_DELETE_URL, {**args, "dry_run": True})
        effects = [self._effect(main, "返还被删子单的接单价、解除拆单归组")]
        self.assertEqual(preview["data"]["effects"], effects)
        result = self._post(BATCH_DELETE_URL, {**args, "dry_run": False, "confirmed_effects": effects})
        self.assertEqual(result["code"], 0, result)
        main.refresh_from_db()
        self.assertEqual((main.total_price, main.split_id), (Decimal("110.00"), ""))

    def test_delete_does_not_send_out_of_scope_linked_order_details(self):
        main = self._order(split_id=self.split_id, split_kind="LEGACY")
        game = Game.objects.create(name="测试不可见游戏")
        child = self._order(split_id=self.split_id, split_kind="LEGACY", game=game)
        args = {"order_nos": [main.order_no], "isDelEmp": "N", "confirmed_effects": []}
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = self._post(BATCH_DELETE_URL, {**args, "dry_run": dry_run})
                self.assertNotEqual(result["code"], 0)
                self.assertIn("无权查看", result["msg"])
                self.assertNotIn(child.order_no, str(result))
        main.refresh_from_db()
        child.refresh_from_db()
        self.assertIsNone(main.deleted_at)
        self.assertIsNone(child.deleted_at)

    def test_delete_does_not_disclose_recycle_bin_members_without_read_permission(self):
        main = self._order(split_id=self.split_id, split_kind="LEGACY")
        self._order(split_id=self.split_id, split_kind="LEGACY")
        historical = self._order(split_id=self.split_id, split_kind="LEGACY", deleted_at=timezone.now())
        self.client = make_logged_in_client(
            username="obatch-delete-only", groups=("删单测试组",),
            nodes=("order.list", "order.delete"),
        )
        preview = self._post(BATCH_DELETE_URL, {
            "order_nos": [main.order_no], "isDelEmp": "N", "confirmed_effects": [], "dry_run": True,
        })
        self.assertIn("无权查看", preview["msg"])
        self.assertNotIn(historical.order_no, str(preview))


class BatchPriceTests(OrderBatchTestBase):
    def test_dry_run_lists_every_order_as_eligible_and_writes_nothing(self):
        a = self._order(total_price=Decimal("100.00"))
        b = self._order(total_price=Decimal("200.00"))
        # spec §4.3 判据 1：`dry_run` 时该动作涉及的**业务列一行未改**。
        # 先按数据库行取一次基线（E-0009：断言打在库里的行上，不打在内存对象 / logger 上）。
        before = {
            row.order_no: (row.total_price, row.updated_by)
            for row in Order.objects.filter(order_no__in=[a.order_no, b.order_no])
        }
        self.assertEqual(before[a.order_no], (Decimal("100.00"), ""))
        self.assertEqual(before[b.order_no], (Decimal("200.00"), ""))
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": True, "total_price": "88.00",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [a.order_no, b.order_no])
        self.assertEqual(body["data"]["ineligible"], [])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        # ⚠️ 少了这一段，「dry_run 分支顺手 save 一下但不写日志」的改法能穿过本文件全部用例。
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(
                    (order.total_price, order.updated_by), before[order.order_no]
                )

    def test_stale_client_preview_works_but_cannot_apply_unconfirmed_multi_order_price(self):
        first = self._order(total_price=Decimal("100.00"))
        second = self._order(total_price=Decimal("200.00"))
        payload = {"order_nos": [first.order_no, second.order_no], "total_price": "0.00"}

        preview = self._post(BATCH_PRICE_URL, {**payload, "dry_run": True})
        self.assertEqual(preview["code"], ErrorCode.OK, preview)
        self.assertEqual(preview["data"]["eligible"], payload["order_nos"])
        refused = self._post(BATCH_PRICE_URL, {**payload, "dry_run": False})
        self.assertEqual(refused["code"], ErrorCode.BUSINESS, refused)
        self.assertIn("请刷新管理订单页面", refused["msg"])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.total_price, second.total_price), (Decimal("100.00"), Decimal("200.00")))
        self.assertFalse(OperationLog.objects.filter(action="order.change_taker_price").exists())

    def test_apply_writes_every_order_and_one_log_each(self):
        a = self._order(total_price=Decimal("100.00"))
        b = self._order(total_price=Decimal("200.00"))
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "total_price": "88.00",
            "confirmed_effects": [],
        })
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.total_price, Decimal("88.00"))
        self.assertEqual(b.total_price, Decimal("88.00"))
        self.assertEqual(
            OperationLog.objects.filter(action="order.change_taker_price").count(), 2
        )

    def test_empty_order_nos_is_rejected(self):
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [], "dry_run": True, "total_price": "88.00",
        })
        self.assertEqual(body["code"], 1000)
        # ⚠️ **文案必须钉死**：只断 `code == 1000` 时，把 `BatchPriceSerializer.order_nos`
        # 的 `allow_empty` 改成 `False`（空数组改由 DRF 回它自己的内置文案——本仓
        # `LANGUAGE_CODE=zh-hans` 下实测为「列表不能为空。」）也能全绿，而 `serializers.py`
        # 的注释明写那条文案归 `parse_order_nos` 给。
        self.assertEqual(body["msg"], "请至少选择一笔订单")

    def test_duplicate_order_nos_is_rejected(self):
        a = self._order()
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [a.order_no, a.order_no], "dry_run": True, "total_price": "88.00",
        })
        self.assertEqual(body["code"], 1000)

    def test_over_limit_is_rejected(self):
        # ⚠️ 501 个**互不相同**的 20 位数字串——用同一串会先撞「重复」那条闸，
        #    那样这条用例就测不到上限（两条判据必须分开考）。
        order_nos = [f"{i:020d}" for i in range(501)]
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": order_nos, "dry_run": True, "total_price": "88.00",
        })
        self.assertEqual(body["code"], 1000)
        # ⚠️ 原断言是 `assertIn("500", body["msg"])`——DRF 自己的
        # `ListField(max_length=500)` 文案经统一异常处理后**同样**是 1000 且**也含 "500"**
        # （本仓 `LANGUAGE_CODE=zh-hans` 下实测为「该字段不能超过 500 个元素。」）⇒
        # 给 `ListField` 加 `max_length=500` 能全绿，而 `serializers.py` 的注释明写不许加。
        # 整句相等把文案出处钉死在这儿。
        self.assertEqual(body["msg"], "一次最多处理 500 笔，本次 501 笔")

    def test_unknown_order_no_is_ineligible_and_others_still_eligible(self):
        """取不到的单号是**逐笔**不合格（spec §D9）——不把整批判成 1000，也不牵连其余单。

        ⚠️ **本用例由编排方 brief 的 `test_unknown_order_no_is_ineligible_and_others_still_apply`
        改写**：原版送 `dry_run=False` 并断言 `applied == [a.order_no]`，与
        Ambiguity resolution #2 / §D4（`apply` 里出现 `resolve_orders` 的 issue ⇒
        `set_rollback(True)` + 1002 + `data.conflict`，**整批拒**）直接冲突
        （那一支的 `data` 里没有 `ineligible` 键）。逐笔语义在 `dry_run` 分支上考，
        `apply` 分支的整批拒由下一条用例考。
        """
        a = self._order(total_price=Decimal("100.00"))
        missing = "1" * 20
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [a.order_no, missing], "dry_run": True, "total_price": "88.00",
        })
        data = body["data"]
        self.assertEqual(body["code"], 0)
        self.assertEqual(data["eligible"], [a.order_no])
        self.assertEqual([i["order_no"] for i in data["ineligible"]], [missing])
        self.assertEqual(data["ineligible"][0]["code"], 1002)
        self.assertEqual(data["applied"], [])

    def test_unknown_order_no_in_apply_rejects_the_whole_batch(self):
        """`apply` 收到「上次校验就说不合格」的单号 ⇒ 整批拒、**一笔不写**（§D4 / Ambiguity #2）。"""
        a = self._order(total_price=Decimal("100.00"))
        missing = "1" * 20
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [a.order_no, missing], "dry_run": False, "total_price": "88.00",
            "confirmed_effects": [],
        })
        self.assertEqual(body["code"], 1002)
        self.assertIn(missing, body["msg"])
        self.assertIn("本批未做任何修改", body["msg"])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(body["data"]["conflict"]["order_no"], missing)
        a.refresh_from_db()
        self.assertEqual(a.total_price, Decimal("100.00"))
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_missing_dry_run_is_rejected(self):
        a = self._order()
        body = self._post(BATCH_PRICE_URL, {"order_nos": [a.order_no], "total_price": "88.00"})
        self.assertEqual(body["code"], 1000)

    def test_repeat_order_no_in_the_batch_is_its_own_case(self):
        # 与 test_duplicate_order_nos_is_rejected 是**同一条判据**的两半：
        # 上面考「同一串两次」，这条考「两串不同但都重复」——分开写是为了让
        # 「先去重还是先判重」这类改法在任一半上都能红。
        a = self._order()
        b = self._order()
        body = self._post(BATCH_PRICE_URL, {
            "order_nos": [a.order_no, b.order_no, a.order_no],
            "dry_run": True, "total_price": "88.00",
        })
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], "同一笔订单只能出现一次")


class RunBatchIneligibleOrderTests(OrderBatchTestBase):
    """`ineligible` 的顺序 = 请求里 `order_nos` 的顺序（spec §D2 / §D9）。

    ⚠️ **这条判据在 `batch_price` 上观察不到**（`change_price` 的逐笔判据今天恒 `_ok`）⇒
    直接调脚手架 `run_batch`，用一个 `per_order` 替身造出「第 1、3 笔逐笔不合格 +
    第 2 笔是取不到行的 issue」的混合现场。⛔ 本用例**不依赖 `batch_price` 的具体判据**。
    """

    def test_ineligible_keeps_request_order_when_parse_issue_mixes_with_per_order_failure(self):
        a = self._order()
        c = self._order()
        missing = "1" * 20  # 20 位数字、库里没有 ⇒ `resolve_orders` 报 issue
        request = SimpleNamespace(user=User.objects.get(username="obatch-user"))

        def per_order(order, ctx, dry_run):
            return WriteOutcome(
                ok=False, code=int(ErrorCode.BUSINESS), msg=f"第 {order.order_no} 笔不合格"
            )

        response = run_batch(
            request,
            order_nos=[a.order_no, missing, c.order_no],
            dry_run=True,
            per_order=per_order,
            ctx=CTX,
        )

        data = response.data["data"]
        self.assertEqual(data["eligible"], [])
        self.assertEqual(
            [issue["order_no"] for issue in data["ineligible"]],
            [a.order_no, missing, c.order_no],
        )
        self.assertEqual(
            [issue["msg"] for issue in data["ineligible"]],
            [f"第 {a.order_no} 笔不合格", "记录不存在或已被删除", f"第 {c.order_no} 笔不合格"],
        )
        self.assertEqual([issue["code"] for issue in data["ineligible"]], [1002, 1002, 1002])
        self.assertEqual(data["applied"], [])


class BatchPriceMalformedRequestProbeTests(OrderBatchTestBase):
    """E-0010 的形状：`POST orders/batch/price/` 收到**这一类**畸形请求不许 500。

    ⚠️ 这条 POST 动作落在 `tests/test_no_route_500.py` 两条腿之间：registry 腿只枚举
    collection 前缀（`@action` 不进 `router.registry`），显式路由腿又把 `router.urls`
    整体减掉 ⇒ 它**没有任何结构性兜底**。判据与那条网一致：**HTTP 200 + 统一信封里的
    业务码**（1000 / 1002），⛔ 不是 HTML 500。
    """

    def _assert_envelope_not_500(self, label, payload):
        response = self.client.post(
            BATCH_PRICE_URL, payload, content_type="application/json"
        )
        self.assertEqual(
            response.status_code, 200,
            msg=f"{label}: HTTP {response.status_code}，body={response.content[:300]!r}",
        )
        body = response.json()
        self.assertIn(
            body.get("code"), (1000, 1002),
            msg=f"{label}: 业务码 {body.get('code')!r} / msg={body.get('msg')!r}",
        )

    def test_malformed_bodies_are_business_errors_not_500(self):
        a = self._order()
        cases = {
            "order_nos 是字符串（不是数组）": {
                "order_nos": a.order_no, "dry_run": True, "total_price": "88.00",
            },
            "order_nos 里塞非字符串": {
                "order_nos": [a.order_no, {"x": 1}], "dry_run": True, "total_price": "88.00",
            },
            "order_nos 里塞 null": {
                "order_nos": [a.order_no, None], "dry_run": True, "total_price": "88.00",
            },
            "dry_run 不传": {
                "order_nos": [a.order_no], "total_price": "88.00",
            },
            "total_price 超长": {
                "order_nos": [a.order_no], "dry_run": True, "total_price": "1" * 40,
            },
            "total_price 非法": {
                "order_nos": [a.order_no], "dry_run": True, "total_price": "abc",
            },
            "空 body": {},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                self._assert_envelope_not_500(label, payload)


class BatchPaymentStatusTests(OrderBatchTestBase):
    """`POST /orders/batch/payment-status/`（spec §4.1 第 6 行 / §4.2 / §4.3）。

    ⚠️ 形状照 `BatchPriceTests`：`dry_run` 零写**先取数据库基线、再 `refresh_from_db()`
    逐列比**（E-0009；只断 `applied == []` 抓不到「顺手 save 一下但不写日志」的改法）。
    """

    def test_dry_run_lists_and_writes_nothing(self):
        a = self._order(payment_status="UNSETTLED")
        b = self._order(payment_status="SETTLING")
        before = {
            row.order_no: (row.payment_status, row.updated_by)
            for row in Order.objects.filter(order_no__in=[a.order_no, b.order_no])
        }
        self.assertEqual(before[a.order_no], ("UNSETTLED", ""))
        self.assertEqual(before[b.order_no], ("SETTLING", ""))
        body = self._post(BATCH_PAYMENT_STATUS_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": True,
            "payment_status": "SETTLED",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [a.order_no, b.order_no])
        self.assertEqual(body["data"]["ineligible"], [])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(
                    (order.payment_status, order.updated_by), before[order.order_no]
                )

    def test_apply_writes_the_same_tier_to_every_order(self):
        a = self._order(payment_status="UNSETTLED")
        b = self._order(payment_status="SETTLING")
        body = self._post(BATCH_PAYMENT_STATUS_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False,
            "payment_status": "SETTLED",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.payment_status, b.payment_status), ("SETTLED", "SETTLED"))
        # 逐单一行（不是一条汇总日志）：两笔 ⇒ 两行，且各自挂在自己单号上。
        self.assertEqual(
            OperationLog.objects.filter(action="order.change_payment_status").count(), 2
        )

    def test_one_ineligible_order_leaves_the_whole_batch_untouched(self):
        """一笔不合格 ⇒ 整批不动（spec §D4）。

        ⚠️ 本端点的核心函数**没有逐笔判据**（三档人工标记，任何单都合格）⇒ 造不出
        「同一份请求对 A 合格、对 B 不合格」的现场：`per_order` 的替身属于脚手架用例
        （`RunBatchIneligibleOrderTests`），端点级用**取不到行**那一支（`resolve_orders`
        的 issue ⇒ §D4 的整批拒）代替。判据仍是「一笔不合格 ⇒ 一行不写」。
        """
        a = self._order(payment_status="UNSETTLED")
        missing = "1" * 20
        body = self._post(BATCH_PAYMENT_STATUS_URL, {
            "order_nos": [a.order_no, missing], "dry_run": False,
            "payment_status": "SETTLED",
        })
        self.assertEqual(body["code"], 1002)
        self.assertIn(missing, body["msg"])
        self.assertIn("本批未做任何修改", body["msg"])
        self.assertEqual(body["data"]["applied"], [])
        a.refresh_from_db()
        self.assertEqual(a.payment_status, "UNSETTLED")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_bad_tier_is_a_validation_error(self):
        order = self._order()
        body = self._post(BATCH_PAYMENT_STATUS_URL, {
            "order_nos": [order.order_no], "dry_run": True, "payment_status": "未收款",
        })
        self.assertEqual(body["code"], 1000)
        self.assertEqual(OperationLog.objects.count(), 0)


class BatchTypesTests(OrderBatchTestBase):
    """`POST /orders/batch/types/`（spec §4.1 第 3 行 / §9-R7）。"""

    def test_dry_run_lists_and_writes_nothing(self):
        order = self._order(total_price=Decimal("100.00"), order_type="COMPANION")
        before = (order.order_type, order.total_price, order.status, order.updated_by)
        body = self._post(BATCH_TYPES_URL, {
            "order_nos": [order.order_no], "dry_run": True, "order_type": "BOOST",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [order.order_no])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(
            (order.order_type, order.total_price, order.status, order.updated_by), before
        )

    def test_apply_writes_order_type_to_every_order(self):
        # ⚠️ 两笔都建成 COMPANION：`update_order_fields` 对**同值**不写留痕
        #    （spec §5.1「值没变的字段不写行」）⇒ 若第二笔本来就是 BOOST，
        #    日志只会有一行，而本条要钉的是「逐单一行」（两笔各一行）。
        a = self._order(order_type="COMPANION")
        b = self._order(order_type="COMPANION")
        body = self._post(BATCH_TYPES_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "order_type": "BOOST",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.order_type, b.order_type), ("BOOST", "BOOST"))
        self.assertEqual(OperationLog.objects.filter(action="order.update").count(), 2)

    def test_only_order_type_reaches_the_database(self):
        """⛔ 批量**不得**成为「改订单别的字段」的第二条写通道（spec §9-R7）。

        请求体里捎带 `total_price` / `status`：`BatchOrderTypeSerializer` 只有三个键，
        多余键被 DRF 忽略，且 `per_order` 传的 `validated` 只含 `order_type`
        ⇒ 两列都必须一字不动。
        """
        # `Order.order_type` 的默认是空串 ⇒ 先显式建成 COMPANION，改动才是可判的
        order = self._order(total_price=Decimal("100.00"), order_type="COMPANION")
        body = self._post(BATCH_TYPES_URL, {
            "order_nos": [order.order_no], "dry_run": False, "order_type": "BOOST",
            "total_price": "1.00", "status": "COMPLETED",
        })
        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.order_type, "BOOST")
        self.assertEqual(order.total_price, Decimal("100.00"))   # ⛔ 批量不是第二条写金额的通道
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_missing_order_type_is_a_validation_error(self):
        order = self._order(order_type="COMPANION")
        body = self._post(BATCH_TYPES_URL, {
            "order_nos": [order.order_no], "dry_run": False,
        })
        self.assertEqual(body["code"], 1000)
        order.refresh_from_db()
        self.assertEqual(order.order_type, "COMPANION")

    def test_unknown_order_type_is_a_validation_error(self):
        order = self._order(order_type="COMPANION")
        body = self._post(BATCH_TYPES_URL, {
            "order_nos": [order.order_no], "dry_run": False, "order_type": "BOOSTED",
        })
        self.assertEqual(body["code"], 1000)
        order.refresh_from_db()
        self.assertEqual(order.order_type, "COMPANION")

    def test_game_outside_the_requesters_scope_is_ineligible(self):
        """`assert_game_allowed` 的 `ValidationProblem` 由**视图**翻成 `_fail`（不是 500）。

        见 `docs/specs/…-order-batch-operations-design.md` §4.2 的 types 行：
        `validate_required` / `validate_order_chain` / `assert_game_allowed` 不通过 ⇒ 逐笔 `1000`。
        ⚠️ 造现场的办法是**换游戏**（把单挂到当前账号没有权限的第二个游戏上），
        ⛔ 不是放宽本基线的节点声明。
        """
        other_game = Game.objects.create(name="测试游戏乙")
        order = self._order(game=other_game, order_type="COMPANION")
        body = self._post(BATCH_TYPES_URL, {
            "order_nos": [order.order_no], "dry_run": True, "order_type": "BOOST",
        })
        # ⚠️ 取不到行（`game_scope` 读不到）⇒ 走 `resolve_orders` 的 issue 支，
        #    这一条钉的是「逐笔不合格、不是 500」；视图里 `ValidationProblem` 那一支
        #    另由 `test_validation_problem_from_the_core_lands_as_ineligible` 用桩考。
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual([i["order_no"] for i in body["data"]["ineligible"]],
                         [order.order_no])
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_validation_problem_from_the_core_lands_as_ineligible(self):
        """核心抛的 `ValidationProblem` 在批量端点里必须落成 `ineligible`，⛔ 不是 500。"""
        order = self._order(order_type="COMPANION")
        with mock.patch(
            "apps.business.api.order_views.update_order_fields",
            side_effect=ValidationProblem("测试用校验失败"),
        ):
            response = self.client.post(
                BATCH_TYPES_URL,
                {"order_nos": [order.order_no], "dry_run": True, "order_type": "BOOST"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual(body["data"]["ineligible"][0]["code"], 1000)
        self.assertEqual(body["data"]["ineligible"][0]["msg"], "测试用校验失败")
        order.refresh_from_db()
        self.assertEqual(order.order_type, "COMPANION")


class BatchHallPriceTests(OrderBatchTestBase):
    """`POST /orders/batch/hall-price/`（spec §4.1 第 5 行 / §4.2 / §4.3）。"""

    def test_dry_run_lists_and_writes_nothing(self):
        a = self._order(total_price=Decimal("100.00"))
        b = self._order(total_price=Decimal("200.00"))
        Performance.objects.create(order=a, emp_type=EmpType.MAIN)
        Performance.objects.create(order=b, emp_type=EmpType.MAIN)
        before = {
            row.order_no: (row.hall_price, row.updated_by)
            for row in Order.objects.filter(order_no__in=[a.order_no, b.order_no])
        }
        self.assertEqual(before[a.order_no], (None, ""))
        body = self._post(BATCH_HALL_PRICE_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": True, "price": "50.00",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [a.order_no, b.order_no])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(
                    (order.hall_price, order.updated_by), before[order.order_no]
                )

    def test_apply_succeeds_when_every_order_is_eligible(self):
        a = self._order(total_price=Decimal("100.00"))
        b = self._order(total_price=Decimal("200.00"))
        body = self._post(BATCH_HALL_PRICE_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "price": "50.00",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.hall_price, b.hall_price),
                         (Decimal("50.00"), Decimal("50.00")))
        self.assertEqual(
            OperationLog.objects.filter(action="order.change_offer_price").count(), 2
        )

    def test_one_ineligible_order_rolls_back_the_whole_batch(self):
        """已结账的 MAIN 行 ⇒ `1014` 逐笔不合格 ⇒ 提交时整批回滚、**一张单都不写**（§D4）。"""
        ok_order = self._order(total_price=Decimal("100.00"))
        settled = self._order(total_price=Decimal("100.00"))
        Performance.objects.create(order=ok_order, emp_type=EmpType.MAIN)
        settled_perf = Performance.objects.create(order=settled, emp_type=EmpType.MAIN)
        self._settle(settled_perf)
        body = self._post(BATCH_HALL_PRICE_URL, {
            "order_nos": [ok_order.order_no, settled.order_no],
            "dry_run": False, "price": "50.00",
        })
        self.assertEqual(body["code"], 1002)
        self.assertIn(settled.order_no, body["msg"])
        self.assertIn("本批未做任何修改", body["msg"])
        ok_order.refresh_from_db()
        self.assertIsNone(ok_order.hall_price)                     # ⛔ 整批不动
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_percent_tier_uses_each_orders_own_total_price(self):
        """`%` 档按**每笔自己的接单价**折算（spec §7-T4 第 2 条）。"""
        a = self._order(total_price=Decimal("100.00"))
        b = self._order(total_price=Decimal("200.00"))
        body = self._post(BATCH_HALL_PRICE_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "price": "50%",
        })
        self.assertEqual(body["code"], 0)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.hall_price, b.hall_price),
                         (Decimal("50.00"), Decimal("100.00")))
        # `%` 档的 `reason` 由**服务端**填（金额档是空串）——两笔各写进自己的日志行。
        self.assertEqual(
            sorted(OperationLog.objects.filter(action="order.change_offer_price")
                   .values_list("reason", flat=True)),
            ["按接单价 50% 折算", "按接单价 50% 折算"],
        )

    def test_bad_price_is_a_request_level_validation_error(self):
        order = self._order(total_price=Decimal("100.00"))
        for label, price in (("非法金额", "abc"), ("比例超上限", "1001%")):
            with self.subTest(label=label):
                body = self._post(BATCH_HALL_PRICE_URL, {
                    "order_nos": [order.order_no], "dry_run": True, "price": price,
                })
                self.assertEqual(body["code"], 1000)
        order.refresh_from_db()
        self.assertIsNone(order.hall_price)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_per_order_failure_carries_the_flattened_message(self):
        """逐笔序列化失败时 `ineligible[].msg` 是**摊平后的人话**，不是 DRF 的 repr。

        ⚠️ 修前这里是 `str(payload.errors)` ⇒ 前端确认清单（它把 `msg` **原样**显示）
        会看到 `{'price': [ErrorDetail(string='发单价格式不正确', …)]}` 这种机器味文案，
        而单笔 `/hall-price/` 经 `apps/core/exceptions._first_message` 摊平成
        「发单价格式不正确」⇒ 批量必须复用**同一个**摊平函数给出同一条文案。

        ⚠️ 可达路径：`total_price=9999999999.99`（12 位、过得了模型列）+ `1000%`
        ⇒ `commission_amount` 得 `99999999999.90`（13 位）⇒ 折算后金额闸（12 位）不合格。
        ⚠️ 请求级那一次用的是**空分母**替身 ⇒ 它照样放行，走到逐笔才失败。
        """
        order = self._order(total_price=Decimal("9999999999.99"))
        body = self._post(BATCH_HALL_PRICE_URL, {
            "order_nos": [order.order_no], "dry_run": True, "price": "1000%",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual(len(body["data"]["ineligible"]), 1)
        issue = body["data"]["ineligible"][0]
        self.assertEqual(issue["code"], 1000)
        # 整句相等把文案出处钉死：与单笔 `/hall-price/` 的 msg 逐字相同。
        self.assertEqual(issue["msg"], "发单价格式不正确")
        order.refresh_from_db()
        self.assertIsNone(order.hall_price)
        self.assertEqual(OperationLog.objects.count(), 0)


class BatchActorConsistencyTests(OrderBatchTestBase):
    """批量端点的 `validate_actor` 口径必须与**各自的单笔端点**一致（编排方对照表）。

    判据是**可观测的行为**，不是读源码：单笔 `/payment-status/` 走 `_actor(request)`
    ⇒ 超长 `actor` 是 1000；`batch/payment-status/` 若漏了这一个口径就会**静默放行**。
    ⚠️ **校验侧**（超长 `actor` ⇒ 1000）与**忽略侧**（超长 `actor` 被忽略 ⇒ 业务码 0）
    分开判，⛔ 不用一条代表全部端点；各端点自己的断言见本类各方法
    （⛔ 端点清单不在这里写计数——E-0011）。
    """

    def _assert_actor_gate(self, url, payload):
        # 151 字符 > `ActorSerializer` 的 150 ⇒ 请求级 1000（HTTP 200 + 统一信封）。
        response = self.client.post(
            url, {**payload, "actor": "x" * 151}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_batch_payment_status_validates_actor(self):
        order = self._order()
        self._assert_actor_gate(BATCH_PAYMENT_STATUS_URL, {
            "order_nos": [order.order_no], "dry_run": True, "payment_status": "SETTLED",
        })

    def test_batch_types_and_hall_price_validate_actor(self):
        order = self._order(order_type="COMPANION", total_price=Decimal("100.00"))
        with self.subTest(endpoint="types"):
            self._assert_actor_gate(BATCH_TYPES_URL, {
                "order_nos": [order.order_no], "dry_run": True, "order_type": "BOOST",
            })
        with self.subTest(endpoint="hall-price"):
            self._assert_actor_gate(BATCH_HALL_PRICE_URL, {
                "order_nos": [order.order_no], "dry_run": True, "price": "50.00",
            })
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_batch_delete_and_transition_ignore_the_request_body_actor(self):
        """`/delete/` 与 `/transition/` 的单笔端点**不读也不校验**请求体 `actor`
        ⇒ 批量这两条必须用 `_write_context(request, validate_actor=False)`：超长 `actor`
        被**忽略**（业务码 0），⛔ 不是凭空多出来的 1000。

        ⚠️ 判据是**可观测行为**（与上面三条反着判）：把它们改成
        `validate_actor=True` 会让本条红，而今天没有任何别的用例会红。
        """
        deleted = self._order()
        body = self._post(BATCH_DELETE_URL, {
            "order_nos": [deleted.order_no], "dry_run": True, "isDelEmp": "N",
            "actor": "x" * 151,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [deleted.order_no])

        moved = self._order()
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [moved.order_no], "dry_run": True,
            "target": OrderStatus.IN_PROGRESS, "actor": "x" * 151,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [moved.order_no])

    def test_batch_assign_and_refunds_ignore_the_request_body_actor(self):
        """`/assign/` 与 `/refunds` 的单笔端点**不读也不校验**请求体 `actor`
        ⇒ 批量这两条必须用 `_write_context(request, validate_actor=False)`：超长 `actor`
        被**忽略**（业务码 0），⛔ 不是凭空多出来的一个 1000。

        ⚠️ 与 `test_batch_delete_and_transition_ignore_the_request_body_actor` **同形状、
        同判据**，分两处写是刻意的：那一条钉 `/delete/` + `/transition/`，这条钉 A9 的两条
        ——⛔ 不要让「已经有两条代表」吞掉新端点的口径（两者改错的方向也不同：
        改成 `validate_actor=True` 会让本条红）。
        """
        taker = User.objects.create_user(username="测试打手-actor", password="测试口令-a")
        # ⭐ D-0252：`assign_order` 入口多了「目标打手游戏范围」闸（无档案=拒）
        #    ⇒ 本条考的是「请求体 actor 被忽略」，给打手授游戏让那一闸放行
        #    （「无该游戏 ⇒ 拒」的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        order = self._order(hall_price=Decimal("100.00"))
        Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        body = self._post(BATCH_ASSIGN_URL, {
            "order_nos": [order.order_no], "dry_run": True, "user_id": taker.pk,
            "actor": "x" * 151,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [order.order_no])

        refundable = self._order(
            status=OrderStatus.COMPLETED, total_price=Decimal("100.00"),
            sales_user=self.sales,
        )
        Performance.objects.create(
            order=refundable, emp_type=EmpType.SALES, user=self.sales,
            commission_rate=Decimal("30.00"),
        )
        body = self._post(BATCH_REFUNDS_URL, {
            "order_nos": [refundable.order_no], "dry_run": True, "refund_total": "5.00",
            "reason": "测试原因", "request_ids": ["测试键-actor"], "actor": "x" * 151,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [refundable.order_no])
        self.assertEqual(OperationLog.objects.count(), 0)


class BatchDeleteTests(OrderBatchTestBase):
    """`POST /orders/batch/delete/`（spec §4.1 第 8 行 / §4.2 的 delete 行 / §D9 的例外）。

    ⚠️ 与单笔 `/delete/` **同源**：取行口是 `Order.all_objects`（`include_deleted=True`）
    ⇒ **已软删单合格**（幂等成功），而不是 `1002`。
    """

    def test_already_deleted_order_is_eligible_and_idempotent(self):
        order = self._order()
        stamp = timezone.now()
        # 与单笔 `_order_including_deleted` 同源的现场：库里已经有一张软删单。
        order.deleted_at = stamp
        order.deleted_by = "测试删除人"
        order.save(update_fields=["deleted_at", "deleted_by"])

        body = self._post(BATCH_DELETE_URL, {
            "order_nos": [order.order_no], "dry_run": True, "isDelEmp": "N",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [order.order_no])
        self.assertEqual(body["data"]["ineligible"], [])

        # 幂等：apply 已删单**合格**，且不覆盖第一次的删除时间 / 删除人、不补写留痕。
        applied = self._post(BATCH_DELETE_URL, {
            "order_nos": [order.order_no], "dry_run": False, "isDelEmp": "N",
        })
        self.assertEqual(applied["code"], 0)
        self.assertEqual(applied["data"]["applied"], [order.order_no])
        order.refresh_from_db()
        self.assertEqual(order.deleted_at, stamp)
        self.assertEqual(order.deleted_by, "测试删除人")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_does_not_soft_delete(self):
        order = self._order()
        body = self._post(BATCH_DELETE_URL, {
            "order_nos": [order.order_no], "dry_run": True, "isDelEmp": "N",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [order.order_no])
        self.assertEqual(body["data"]["applied"], [])
        order.refresh_from_db()
        self.assertIsNone(order.deleted_at)
        self.assertEqual(order.deleted_by, "")
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_apply_soft_deletes_every_order(self):
        a = self._order()
        b = self._order()
        body = self._post(BATCH_DELETE_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "isDelEmp": "N",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertIsNotNone(order.deleted_at)
                # 删除人取**认证态**（与单笔同源）——`validate_actor=False` 让两者同源。
                self.assertEqual(order.deleted_by, "obatch-user")
        # 逐单一行（不是一条汇总日志）。
        self.assertEqual(OperationLog.objects.filter(action="order.delete").count(), 2)


class V2BatchDeleteReasonTests(OrderBatchTestBase):
    def test_four_orders_require_nonblank_reason_without_writing(self):
        order_nos = [self._order().order_no for _ in range(4)]
        payload = {"order_nos": order_nos, "dry_run": False, "isDelEmp": "Y"}
        for extra in ({}, {"reason": " \t "}, {"reason": "删" * 201}):
            with self.subTest(extra=extra):
                body = self._post(V2_BATCH_DELETE_URL, {**payload, **extra})
                self.assertEqual(body["code"], 1000)
        self.assertEqual(Order.objects.filter(order_no__in=order_nos).count(), 4)
        self.assertFalse(OperationLog.objects.filter(action="order.delete").exists())

        preview = self._post(V2_BATCH_DELETE_URL, {**payload, "dry_run": True})
        self.assertEqual(preview["data"]["eligible"], order_nos)
        self.assertEqual(preview["data"]["applied"], [])

    def test_reason_is_trimmed_and_recorded_on_each_deleted_order(self):
        anchor = self._order(split_id="batch-reason-test")
        child = self._order(split_id=anchor.split_id)
        order_nos = [anchor.order_no, *(self._order().order_no for _ in range(3))]
        body = self._post(V2_BATCH_DELETE_URL, {
            "order_nos": order_nos, "dry_run": False,
            "isDelEmp": "Y", "reason": "  清理测试订单  ",
        })
        self.assertEqual(body["data"]["applied"], order_nos)
        self.assertFalse(Order.objects.filter(order_no__in=[*order_nos, child.order_no]).exists())
        logs = OperationLog.objects.filter(action="order.delete")
        self.assertEqual(
            dict(logs.values_list("order_no", "reason")),
            {**dict.fromkeys(order_nos, "清理测试订单"), child.order_no: "随拆单组主单删除（清理测试订单）"},
        )
        self.assertIn("（原因：清理测试订单）", logs.get(order_no=anchor.order_no).summary)
        self.assertIn("随拆单组主单删除（清理测试订单）", logs.get(order_no=child.order_no).summary)

    def test_operations_log_returns_complete_200_character_delete_reason(self):
        order = self._order()
        performance = Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        order_nos = [order.order_no, *(self._order().order_no for _ in range(3))]
        reason = "测试删除原因" * 33 + "末尾"
        self.assertEqual(len(reason), 200)
        body = self._post(V2_BATCH_DELETE_URL, {
            "order_nos": order_nos, "dry_run": False, "isDelEmp": "N", "reason": reason,
        })
        self.assertEqual(body["data"]["applied"], order_nos)
        order.refresh_from_db()
        performance.refresh_from_db()
        self.assertIsNotNone(order.deleted_at)
        self.assertIsNone(performance.deleted_at)
        reader = make_logged_in_client(
            username="obatch-log-reader", groups=("日志组",), nodes=("system.logs",)
        )
        response = reader.get("/api/audit/logs/", {
            "order_no": order_nos[0], "include_delete_reason": "true",
        }).json()
        self.assertEqual(response["code"], 0)
        row, = response["data"]["items"]
        self.assertEqual(row["action"], "order.delete")
        self.assertIn(
            f"【obatch-user】已将订单 {order.order_no} 移入回收站，保留绩效记录（原因：{reason}",
            row["summary"],
        )
        self.assertEqual(row["delete_reason"], reason)

    def test_three_v2_orders_and_four_legacy_orders_can_delete_without_reason(self):
        for url, count in ((V2_BATCH_DELETE_URL, 3), (BATCH_DELETE_URL, 4)):
            with self.subTest(url=url):
                order_nos = [self._order().order_no for _ in range(count)]
                body = self._post(url, {"order_nos": order_nos, "dry_run": False})
                self.assertEqual(body["data"]["applied"], order_nos)
                self.assertFalse(Order.objects.filter(order_no__in=order_nos).exists())


class BatchDeleteMethodGuardTests(OrderBatchTestBase):
    """`DELETE /orders/batch/delete/` 必须是 405。

    ⚠️ 形状照单笔的
    `tests/test_order_recycle.py::SoftDeleteLandingTests::test_delete_is_post_only`。
    这条同时钉住两件事：类级 `http_method_names` 不含 `delete`，**且**批量动作的方法名是
    `batch_delete` 而不是 `delete`——`APIView.dispatch` 按
    `getattr(self, request.method.lower())` 取处理器（E-0072），名叫 `delete` 的方法会
    **同时**成为每条本 ViewSet 路由上 HTTP DELETE 的处理器（`test_drf_view_method_names`
    的结构网会红，但那条网钉的是「名字不许撞动词」，不是「这条路由的 405」）。
    """

    def test_delete_route_is_post_only(self):
        resp = self.client.delete(BATCH_DELETE_URL)
        self.assertEqual(resp.status_code, 405)


class BatchTransitionTests(OrderBatchTestBase):
    """`POST /orders/batch/transition/` 的两种形态（spec §D10 / §4.2 的 transition 行）。"""

    def test_without_target_returns_the_union_and_writes_nothing(self):
        unassigned = self._order(status=OrderStatus.UNASSIGNED)
        working = self._order(status=OrderStatus.IN_PROGRESS)
        # ⚠️ 已完成那一笔**排在最前**且它的可选集比另两笔**少一个** `COMPLETED`
        #    （「已完成」提示项只对**当前不是**已完成的单列出）⇒ 这条请求顺序让
        #    「`targets` 只取第一笔」那类改法能被下面那条并集断言判红。
        done = self._order(status=OrderStatus.COMPLETED)
        order_nos = [done.order_no, unassigned.order_no, working.order_no]
        before = {
            row.order_no: (row.status, row.updated_by)
            for row in Order.objects.filter(order_no__in=order_nos)
        }

        body = self._post(BATCH_TRANSITION_URL, {"order_nos": order_nos, "dry_run": True})
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertIn(OrderStatus.IN_PROGRESS, data["targets"])
        self.assertEqual(
            [row["order_no"] for row in data["per_order"]],
            order_nos,
            "`per_order` 必须与请求里的 `order_nos` 同序（spec §D9）",
        )
        # `targets` = 各笔 `allowed` 的**并集**（不是某一笔的），且按声明顺序排序（渲染稳定）。
        union = set()
        for row in data["per_order"]:
            union.update(row["allowed"])
        self.assertEqual(set(data["targets"]), union)
        self.assertEqual(
            data["targets"],
            [str(value) for value, _ in OrderStatus.choices if str(value) in union],
        )
        # 已完成那一笔的可选集里没有「已完成」提示项，另两笔有 ⇒ 并集里必须有它。
        done_row = next(r for r in data["per_order"] if r["order_no"] == done.order_no)
        self.assertNotIn(OrderStatus.COMPLETED, done_row["allowed"])
        self.assertIn(OrderStatus.COMPLETED, data["targets"])
        self.assertEqual(data["eligible"], [])
        self.assertEqual(data["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        for order in (done, unassigned, working):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual((order.status, order.updated_by), before[order.order_no])

    def test_read_form_ignores_dry_run_and_reports_missing_rows(self):
        """`target` 缺省 = 读取形态 ⇒ **那条请求的 `dry_run` 不参与判断**（spec §D10）。

        ⚠️ 送 `dry_run=False` 也必须一个字节都不写、`data.dry_run` 恒为 `True`。
        """
        order = self._order(status=OrderStatus.UNASSIGNED)
        missing = "1" * 20
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no, missing], "dry_run": False,
        })
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertIs(data["dry_run"], True)
        self.assertEqual(
            [row["order_no"] for row in data["per_order"]], [order.order_no]
        )
        self.assertEqual([i["order_no"] for i in data["ineligible"]], [missing])
        self.assertEqual(data["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

    def test_target_is_validated_per_order(self):
        """同一份请求对 A 笔合格、对 B 笔不合格（spec §4.2 的 transition 行）。"""
        review = self._order(status=OrderStatus.PENDING_REVIEW)   # 只有它能到 COMPLETED
        working = self._order(status=OrderStatus.IN_PROGRESS)
        before = {
            row.order_no: row.status
            for row in Order.objects.filter(order_no__in=[review.order_no, working.order_no])
        }
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [review.order_no, working.order_no],
            "dry_run": True, "target": OrderStatus.COMPLETED,
        })
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(data["eligible"], [review.order_no])
        self.assertEqual([i["order_no"] for i in data["ineligible"]], [working.order_no])
        # 文案逐字来自 `order_status.COMPLETE_REQUIRES_REVIEW_MESSAGE`（业务方给的原文）。
        self.assertIn("待验收", data["ineligible"][0]["msg"])
        self.assertEqual(data["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        for order in (review, working):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(order.status, before[order.order_no])

    def test_self_transition_reports_the_human_copy_in_the_ineligible_list(self):
        """⭐ 本线 T4（判据 j）：批量预检里，**目标 == 当前**那笔带的是那句人话。

        这就是业务方现场那条路径：一批单已经全是「已完成」，再点一次「已完成」的批量预检
        ⇒ 从前回 `非法状态迁移：COMPLETED → COMPLETED`（用户以为操作失败，其实是第一次成功了），
        现在回 `该订单已经是「已完成」，无需修改`。
        ⚠️ 字面量的**唯一值钉**在 `tests/test_order_status.py::SelfTransitionMessageTests`
        （⛔ 这里引常量、不抄第二份字面量）；本条只证明**它确实流到了批量清单里**。
        """
        done = self._order(status=OrderStatus.COMPLETED)
        working = self._order(status=OrderStatus.IN_PROGRESS)
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [done.order_no, working.order_no],
            "dry_run": True, "target": OrderStatus.COMPLETED,
        })
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(data["eligible"], [])
        self.assertEqual(
            [i["order_no"] for i in data["ineligible"]],
            [done.order_no, working.order_no],
            "清单必须与请求同序（spec §D9）",
        )
        self.assertEqual(
            data["ineligible"][0]["msg"],
            SELF_TRANSITION_MESSAGE.format(status_label=OrderStatus.COMPLETED.label),
        )
        self.assertNotIn("非法状态迁移", data["ineligible"][0]["msg"])
        # 另一档（代练中 → 已完成）仍走业务方给的**逐字**原文（⛔ 一个字不动）。
        self.assertEqual(data["ineligible"][1]["msg"], COMPLETE_REQUIRES_REVIEW_MESSAGE)
        self.assertEqual(data["applied"], [])
        self.assertEqual(OperationLog.objects.count(), 0)
        # `dry_run` 一个字节都不写（判据打在**数据库行**上，E-0009 同族）。
        done.refresh_from_db()
        working.refresh_from_db()
        self.assertEqual(
            (done.status, working.status),
            (OrderStatus.COMPLETED, OrderStatus.IN_PROGRESS),
        )

    def test_apply_transitions_every_order(self):
        a = self._order(status=OrderStatus.UNASSIGNED)
        b = self._order(status=OrderStatus.UNASSIGNED)
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False,
            "target": OrderStatus.IN_PROGRESS,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status),
                         (OrderStatus.IN_PROGRESS, OrderStatus.IN_PROGRESS))
        self.assertEqual(
            OperationLog.objects.filter(action="order.transition").count(), 2
        )

    def test_one_ineligible_order_rolls_back_the_whole_batch(self):
        """`apply` 收到「上次校验就说不合格」的单号 ⇒ 整批拒、**一笔不写**（§D4）。"""
        review = self._order(status=OrderStatus.PENDING_REVIEW)
        working = self._order(status=OrderStatus.IN_PROGRESS)
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [review.order_no, working.order_no], "dry_run": False,
            "target": OrderStatus.COMPLETED,
        })
        self.assertEqual(body["code"], 1002)
        self.assertIn(working.order_no, body["msg"])
        self.assertIn("本批未做任何修改", body["msg"])
        self.assertEqual(body["data"]["applied"], [])
        review.refresh_from_db()
        self.assertEqual(review.status, OrderStatus.PENDING_REVIEW)   # ⛔ 整批不动
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_batch_transition_reason_lands_in_every_order_log(self):
        """⭐ D-0337（2026-10-30）：批量「改状态」的说明原因**逐笔**进留痕行。

        弹窗的批量支与单笔支共用同一份表单 ⇒ 勾 N 笔填一次原因，每笔的日志都要带。
        判据打在 summary（前端可见的唯一形状）与 `reason` 列两处。
        """
        a = self._order(status=OrderStatus.PAUSED)
        b = self._order(status=OrderStatus.PAUSED)
        order_nos = [a.order_no, b.order_no]
        # 两段式：预检不写（dry_run 的零副作用照旧）⇒ 预检请求带 reason 也不写一行。
        probe = self._post(BATCH_TRANSITION_URL, {
            "order_nos": order_nos, "dry_run": True,
            "target": OrderStatus.UNASSIGNED, "reason": "客户整批退款",
        })
        self.assertEqual(probe["code"], 0)
        self.assertEqual(probe["data"]["eligible"], order_nos)
        self.assertEqual(OperationLog.objects.filter(action="order.transition").count(), 0)
        # 执行段：applied 两笔，每笔日志的 summary 带同一条原因。
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": order_nos, "dry_run": False,
            "target": OrderStatus.UNASSIGNED, "reason": "客户整批退款",
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], order_nos)
        rows = OperationLog.objects.filter(action="order.transition").order_by("order_no")
        self.assertEqual(rows.count(), 2)
        for row in rows:
            with self.subTest(order_no=row.order_no):
                self.assertEqual(row.reason, "客户整批退款")
                self.assertTrue(
                    row.summary.endswith("（原因：客户整批退款）"),
                    f"summary 应带原因尾段，实际：{row.summary}",
                )

    def test_batch_transition_reason_over_200_chars_is_rejected(self):
        """批量侧同一个上限（同一份字段声明）：201 字 ⇒ 请求级 1000、一笔不写。"""
        order = self._order(status=OrderStatus.PAUSED)
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no], "dry_run": False,
            "target": OrderStatus.UNASSIGNED, "reason": "退" * 201,
        })
        self.assertEqual(body["code"], 1000)
        self.assertIn("200", body["msg"])
        self.assertEqual(OperationLog.objects.count(), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED)


class BatchTransitionNodeGateTests(OrderBatchTestBase):
    """`batch_transition` 的节点门三种形态（spec §D6 / §D10）。

    ⚠️ 这是本任务**新增的授权分支**（`get_permissions()` 里那条 `batch_transition`）：
    读取形态取 16/17 的 **any** 元组、写形态按 `target` 分流。核心判据是
    「读选项不是写 ⇒ 读取形态的放宽**不许**传染给写」——下面三条各钉一面。
    ⚠️ 换账号用 `make_logged_in_client` 另建（⛔ 不放宽基线的账号、⛔ 不改基线的节点声明）。
    """

    def _login_only_with(self, username, *nodes):
        """换一个**只挂这几个节点**的账号（⛔ 不放宽基线的账号）。

        ⚠️ 组名必须 **≤5 字**：`Role.name` 的 `max_length=5`
        （`p5_auth_helpers._grant_nodes` 的 docstring 记了这条），超长会在建 Role 时
        `DataError`（症状看起来像夹具坏了）。
        ⚠️ 每个账号**各用一个组名**：`_grant_nodes` 按组名取同一个 `Role` 再 `add` 节点
        ⇒ 两个账号共用一个组名会让先建的账号**静默拿到后挂的节点**（判据变恒真）。
        """
        self._gate_seq = getattr(self, "_gate_seq", 0) + 1
        self.client = make_logged_in_client(
            username=username, groups=(f"节点组{self._gate_seq}",), nodes=nodes
        )

    def test_read_form_is_same_source_as_the_order_detail(self):
        """读取形态的 `per_order[].allowed` 与订单详情 `allowed_transitions` **逐字相同**。

        ⚠️ 两侧**同源**（都出自同一份状态机判据）⇒ 这条只是**跨端点一致性**的对拍：
        将来有人另写一套**一致的第二份实现**，它照样全绿。真正的**独立 oracle** 是
        `tests/test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint`
        （那边拿独立来源对拍）。
        """
        order = self._order(status=OrderStatus.UNASSIGNED)
        self._login_only_with(
            "obatch-samesource", "order.view_detail",
            "order.change_status", "order.change_status_completed",
        )
        detail = self.client.get(f"/api/business/orders/{order.order_no}/").json()
        self.assertEqual(detail["code"], 0)
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no], "dry_run": True,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            body["data"]["per_order"][0]["allowed"],
            detail["data"]["allowed_transitions"],
        )
        # 哨兵：「两边都空」也能让上面那条相等成立 ⇒ 钉住非空 + 一个具体目标。
        self.assertIn(OrderStatus.IN_PROGRESS, detail["data"]["allowed_transitions"])

    def test_read_form_accepts_either_of_the_two_nodes(self):
        """读取形态是 **any** 元组：只持 16 **或**只持 17 都能读（spec §D6）。"""
        order = self._order(status=OrderStatus.PENDING_REVIEW)
        for username, node in (
            ("obatch-read16", "order.change_status"),
            ("obatch-read17", "order.change_status_completed"),
        ):
            with self.subTest(node=node):
                self._login_only_with(username, node)
                body = self._post(BATCH_TRANSITION_URL, {
                    "order_nos": [order.order_no], "dry_run": True,
                })
                self.assertEqual(body["code"], 0)
                self.assertEqual(
                    [row["order_no"] for row in body["data"]["per_order"]],
                    [order.order_no],
                )

    def test_the_read_relaxation_does_not_leak_into_the_write_halves(self):
        """读取形态的 any 元组**只**放宽读：写 `COMPLETED` 仍要 17、写其余仍要 16。"""
        order = self._order(status=OrderStatus.PENDING_REVIEW)
        # 只持 17：读放行（上面那条），但写非已完成目标要 16 ⇒ **403**。
        self._login_only_with("obatch-write17", "order.change_status_completed")
        response = self.client.post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no], "dry_run": True,
            "target": OrderStatus.IN_PROGRESS,
        }, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        # 只持 16：写 `COMPLETED` 要 17 ⇒ **403**。
        self._login_only_with("obatch-write16", "order.change_status")
        response = self.client.post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no], "dry_run": True,
            "target": OrderStatus.COMPLETED,
        }, content_type="application/json")
        self.assertEqual(response.status_code, 403)
        # 对照：同一个 16-only 账号写**非已完成**目标是放行的（否则上面两条 403 可能是
        # 「这个账号什么都做不了」凑出来的假绿）。
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no], "dry_run": True,
            "target": OrderStatus.IN_PROGRESS,
        })
        self.assertEqual(body["code"], 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)   # dry_run 一行不写


class BatchMalformedRequestProbeTests(OrderBatchTestBase):
    """E-0010 的形状：`batch/price/` 之外的批量 POST 端点收到**这一类**畸形请求也不许 500。

    ⚠️ 与 `BatchPriceMalformedRequestProbeTests` **同一形状、同一判据**（HTTP 200 +
    业务码 1000/1002）。分两处写是刻意的：那一类钉 `batch/price/`，这一类钉其后各任务
    新落的端点（端点**全集**就是下面的 `_VALID_TAIL`，⛔ 不在这里写计数——E-0011）；
    ⛔ 不要让「已经有一条代表」吞掉新端点的兜底：这些 `@action` 落在
    `tests/test_no_route_500.py` 两条腿之间（registry 腿只枚举 collection 前缀、
    显式路由腿又把 `router.urls` 整体减掉）⇒ 它们**没有任何结构性兜底**。
    """

    #: 每个端点的「合法底包」：只把 `order_nos` / `dry_run` 弄坏，其余键保持合法。
    #: ⚠️ `batch/assign/` 的 `user_id` 用一个**存在的**用户不可行（这里没有 `self`）⇒ 用字面
    #: 主键：畸形用例在序列化器那一层就被拒，走不到「用户不存在」那条请求级闸。
    #: ⚠️ `batch/refunds/` 的 `request_ids` 长度必须与「合法底包」里的 `order_nos`（1 个）一致，
    #: 否则每条畸形用例都会先撞「两串不等长」，那就不再是在考畸形响应了。
    _VALID_TAIL = {
        BATCH_PAYMENT_STATUS_URL: {"payment_status": "SETTLED"},
        BATCH_TYPES_URL: {"order_type": "BOOST"},
        BATCH_HALL_PRICE_URL: {"price": "50.00"},
        BATCH_DELETE_URL: {},
        BATCH_TRANSITION_URL: {"target": OrderStatus.IN_PROGRESS},
        BATCH_ASSIGN_URL: {"user_id": 1},
        BATCH_REFUNDS_URL: {"refund_total": "5.00", "reason": "测试原因",
                            "request_ids": ["测试键-畸形"]},
        # ⭐ A10：重算客服提成（body 只有 `order_nos` + `dry_run` ⇒ 底包是空字典）。
        BATCH_RECOMPUTE_URL: {},
    }

    def _assert_envelope_not_500(self, label, url, payload):
        response = self.client.post(url, payload, content_type="application/json")
        self.assertEqual(
            response.status_code, 200,
            msg=f"{label}: HTTP {response.status_code}，body={response.content[:300]!r}",
        )
        body = response.json()
        self.assertIn(
            body.get("code"), (1000, 1002),
            msg=f"{label}: 业务码 {body.get('code')!r} / msg={body.get('msg')!r}",
        )

    def test_malformed_bodies_are_business_errors_not_500(self):
        order = self._order(total_price=Decimal("100.00"), order_type="COMPANION")
        for url, tail in self._VALID_TAIL.items():
            cases = {
                "order_nos 是字符串（不是数组）": {
                    "order_nos": order.order_no, "dry_run": True, **tail,
                },
                "order_nos 里塞非字符串": {
                    "order_nos": [order.order_no, {"x": 1}], "dry_run": True, **tail,
                },
                "order_nos 里塞 null": {
                    "order_nos": [order.order_no, None], "dry_run": True, **tail,
                },
                "dry_run 不传": {"order_nos": [order.order_no], **tail},
                "空 body": {},
            }
            for label, payload in cases.items():
                with self.subTest(endpoint=url, label=label):
                    self._assert_envelope_not_500(
                        f"{url} / {label}", url, payload
                    )


class BatchAssignTests(OrderBatchTestBase):
    """`POST /orders/batch/assign/`（spec §4.1 第 1 行 / §4.2 的 assign 行 / §4.3）。

    ⚠️ **请求级与逐单的分工**（spec §D5 + 编排方 Ambiguity #1/#2）：
    `taker` 不存在 / 已停用、带 `price` / `fee` 时的两道节点闸都是**请求级**
    （同一份节点集对所有单结论一样）⇒ 在视图里、`run_batch` 之前做**一次**，
    失败是整批 1000 / **403**；而「`taker` 就是本单的销售归属人」对不同单结论可能不同
    ⇒ 留在 `order_writes.assign_order` 里，落成 `ineligible`(1002)。
    """

    def _assignable_order(self, **kw):
        """一笔**真的能被指派**的单：有发单价 + 一条 MAIN 绩效行。

        ⚠️ 缺发单价时 `assign_order` 的 `set_taker` → `effective_taker_price` 会抛
        `AmountError("该单没有接单价…")` ⇒ `apply` 整批回 1002（而 `dry_run` 在 `set_taker`
        **之前**就返回 ⇒ 它报 eligible——那是 §9-R8 已登记的「判据不在内存闸里」形态）。
        夹具照 `tests/test_order_assign_ext.py::_AssignFixture._order` 的既有写法
        （⛔ 不要假设「建单自带绩效行」，也⛔ 不要用没有发单价的单考 apply）。
        """
        kw.setdefault("hall_price", Decimal("100.00"))
        order = self._order(**kw)
        Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        return order

    def test_request_level_checks_fail_the_whole_batch_once(self):
        """`user_id` 指向不存在的用户 ⇒ 整批一次 1000，且**零写入**。

        ⚠️ 「零写入」是承重断言：这道闸必须在 `run_batch` **之前**——
        挪进逐单循环的话，本用例会退化成「每笔各一条 ineligible、码 1002」。
        """
        a = self._order()
        b = self._order()
        before = {
            row.order_no: (row.status, row.updated_by)
            for row in Order.objects.filter(order_no__in=[a.order_no, b.order_no])
        }
        body = self._post(BATCH_ASSIGN_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "user_id": 999999,
        })
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], "指定的用户不存在")
        self.assertEqual(Performance.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual((order.status, order.updated_by), before[order.order_no])

    def test_inactive_taker_is_rejected_for_the_whole_batch(self):
        """停用号不可被指派（与单笔 `/assign/` 同口径）⇒ 整批一次 1000，零写入。"""
        taker = User.objects.create_user(username="测试打手停用", password="测试口令-停")
        taker.is_active = False
        taker.save(update_fields=["is_active"])
        order = self._order()
        body = self._post(BATCH_ASSIGN_URL, {
            "order_nos": [order.order_no], "dry_run": False, "user_id": taker.pk,
        })
        self.assertEqual(body["code"], 1000)
        self.assertEqual(body["msg"], "指定的账号已停用")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(Performance.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_per_order_sales_user_conflict_is_ineligible(self):
        """`taker == order.sales_user` 是**逐单**判据 ⇒ `ineligible`(1002)，⛔ 不是整批 403/1000。"""
        taker = User.objects.create_user(username="测试打手丙", password="测试口令-3")
        # ⭐ D-0252：给打手授游戏让游戏范围闸放行，本条考的才是 ① 那道闸。
        grant_games(taker, self.game)
        order = self._order(sales_user=taker)
        body = self._post(BATCH_ASSIGN_URL, {
            "order_nos": [order.order_no], "dry_run": True, "user_id": taker.pk,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [])
        self.assertEqual(body["data"]["ineligible"][0]["order_no"], order.order_no)
        self.assertEqual(body["data"]["ineligible"][0]["code"], 1002)
        # 文案来自唯一判据出处 `order_writes.assign_order`（⛔ 视图里没有第二份）。
        self.assertIn("销售提成归属人", body["data"]["ineligible"][0]["msg"])
        self.assertEqual(body["data"]["applied"], [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_apply_assigns_every_order_to_the_same_taker(self):
        taker = User.objects.create_user(username="测试打手丁", password="测试口令-4")
        # ⭐ D-0252：给打手授游戏让游戏范围闸放行（成功路径的夹具前提）。
        grant_games(taker, self.game)
        a = self._assignable_order()
        b = self._assignable_order()
        body = self._post(BATCH_ASSIGN_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "user_id": taker.pk,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        # 逐单一行（不是一条汇总日志）。
        self.assertEqual(
            OperationLog.objects.filter(action="performance.assign").count(), 2
        )
        # 两笔的 MAIN 绩效行都落到**同一个** taker 上（不是各指一半）。
        self.assertEqual(
            sorted(
                Performance.objects.filter(emp_type=EmpType.MAIN, user=taker)
                .values_list("order__order_no", flat=True)
            ),
            [a.order_no, b.order_no],
        )

    def test_assign_with_price_when_order_has_no_existing_hall_price(self):
        taker = User.objects.create_user(username="测试带价指派打手", password="测试口令-6")
        grant_games(taker, self.game)
        for price in ("25.50", "0.00"):
            with self.subTest(price=price):
                order = self._order(hall_price=None)
                body = self._post(BATCH_ASSIGN_URL, {
                    "order_nos": [order.order_no],
                    "dry_run": False,
                    "user_id": taker.pk,
                    "price": price,
                })
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(body["data"]["applied"], [order.order_no])
                order.refresh_from_db()
                self.assertEqual(order.hall_price, Decimal(price))
                self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
                perf = self._main_perf(order)
                self.assertEqual(perf.user_id, taker.pk)
                self.assertEqual(perf.temp_amount, Decimal(price))

    def test_dry_run_writes_nothing(self):
        """§4.3 第 1/2/5 条：`dry_run` 时状态列与绩效行**逐列不动**、零日志、`applied == []`。

        ⚠️ 先建一条 MAIN 绩效行再断（E-0009 的形状）：不建的话「预检里顺手
        `set_taker` 一下」的改法在绩效侧观察不到（`_ok` 之后照样 `applied == []`）。
        """
        taker = User.objects.create_user(username="测试打手戊", password="测试口令-5")
        # ⭐ D-0252：给打手授游戏让游戏范围闸放行（本条考 `dry_run` 零写入）。
        grant_games(taker, self.game)
        order = self._order(hall_price=Decimal("100.00"))
        Performance.objects.create(order=order, emp_type=EmpType.MAIN)
        before = (order.status, order.updated_by)
        body = self._post(BATCH_ASSIGN_URL, {
            "order_nos": [order.order_no], "dry_run": True, "user_id": taker.pk,
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [order.order_no])
        self.assertEqual(body["data"]["ineligible"], [])
        self.assertEqual(body["data"]["applied"], [])
        order.refresh_from_db()
        self.assertEqual((order.status, order.updated_by), before)
        perf = self._main_perf(order)
        self.assertIsNone(perf.user_id)
        self.assertEqual(OperationLog.objects.count(), 0)


class BatchAssignMoneyNodeGateTests(OrderBatchTestBase):
    """带 `price` / `fee` 时的两道**请求级**节点闸（spec §4.2 的 assign 行 / §D5）。

    ⚠️ 判据必须用 `is not None`（E-0021）：`price=0` / `fee=0` 是**合法边界**，
    不能被当成「没传」而静默关掉闸门。故这里用 `"0.00"` 当探针值。
    ⚠️ 造现场用**另建账号**（形状照 `BatchTransitionNodeGateTests._login_only_with`）：
    基线的账号持 `order.submit_payout_price` ⇒ 在它身上测不出 403。⛔ 不放宽基线节点声明。
    ⚠️ 两道闸失败是 **403**（授权语义），⛔ 不是逐笔 `ineligible`——见 `batch_assign` 的说明。
    """

    def _client_without_the_money_node(self):
        """已认证、够进端点（持 `order.assign_taker`）、但**不持** `order.submit_payout_price`。

        ⚠️ 组名必须 ≤5 字（`Role.name` 的 `max_length=5`），且**不能为空**
        （节点挂在 `Group → Role` 上，空组 ⇒ 类级门 403，测出来的不是本闸）。
        """
        return make_logged_in_client(
            username="obatch-nomoney",
            groups=("无钱组",),
            nodes=("order.assign_taker", "order.change_status", "order.list"),
        )

    def _post_assign(self, client, payload):
        return client.post(BATCH_ASSIGN_URL, payload, content_type="application/json")

    def test_price_zero_still_requires_the_money_node(self):
        client = self._client_without_the_money_node()
        order = self._order()
        response = self._post_assign(client, {
            "order_nos": [order.order_no], "dry_run": True,
            "user_id": self.sales.pk, "price": "0.00",
        })
        self.assertEqual(response.status_code, 403, response.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)   # 403 ⇒ 一行不写
        self.assertIsNone(order.hall_price)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_fee_zero_still_requires_the_money_node(self):
        client = self._client_without_the_money_node()
        order = self._order()
        response = self._post_assign(client, {
            "order_nos": [order.order_no], "dry_run": True,
            "user_id": self.sales.pk, "fee": "0.00",
        })
        self.assertEqual(response.status_code, 403, response.content[:300])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_without_the_two_money_keys_the_same_account_is_allowed(self):
        """阳性对照：同一个账号**不带** `price` / `fee` ⇒ 业务码 0。

        ⛔ 没有这条，上面两条 403 可能只是「这个账号什么都做不了」凑出来的假绿。
        """
        client = self._client_without_the_money_node()
        order = self._order()
        # ⭐ D-0252：`obatch-sales` 在基线里没有档案（=无游戏）——这里它
        #    **被指派**为目标打手 ⇒ 先授游戏让游戏范围闸放行（本条考的是节点 403 的阳性对照）。
        grant_games(self.sales, self.game)
        response = self._post_assign(client, {
            "order_nos": [order.order_no], "dry_run": True, "user_id": self.sales.pk,
        })
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["eligible"], [order.order_no])


class BatchRefundTests(OrderBatchTestBase):
    """`POST /orders/batch/refunds/`（spec §4.1 第 7 行 / §4.2 的 refunds 行 / §D13）。

    ⚠️ 逐笔判据（`status != COMPLETED`(1007) / `amount <= 0`(1008) /
    `amount > 剩余可退`(1008) / SALES 行提成率为空(1009)）全部来自
    `refunds.refund_total_denial` / `assert_sales_commissions_configured`（Task 5 抽的同一批函数）
    ——本端点的 `per_order` 只负责把**该笔自己的** `request_id` 递进去，⛔ 不重写任何判据。
    """

    def _refundable_order(self, amount="10.00"):
        """建一笔「可退款」的单：已完成 + 有 SALES 行 + 已配提成率。

        ⚠️ 这三个条件缺一不可（spec §4.2 的退款项）：缺 SALES 行会 `DoesNotExist`、
        提成率为空会回 1009 —— 那都不是本类要测的判据。
        形态照 `src/tests/test_refund_partial.py` 里既有的建单 / 建绩效写法。
        """
        order = self._order(
            status=OrderStatus.COMPLETED, total_price=Decimal(amount),
            sales_user=self.sales,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.SALES, user=self.sales,
            commission_rate=Decimal("30.00"),
        )
        return order

    def test_request_ids_length_must_match(self):
        """`len(request_ids) != len(order_nos)` ⇒ 请求级 1000（两个方向都判）。"""
        order = self._refundable_order()
        for label, request_ids in (("少一个", []), ("多一个", ["测试键-1", "测试键-2"])):
            with self.subTest(label=label):
                body = self._post(BATCH_REFUNDS_URL, {
                    "order_nos": [order.order_no], "dry_run": True, "refund_total": "5.00",
                    "reason": "测试原因", "request_ids": request_ids,
                })
                self.assertEqual(body["code"], 1000)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_over_long_request_id_is_a_business_code_not_500(self):
        """单个键超列宽（64）在**批量**路径上也是 1000、不是 500。

        ⚠️ `request_ids` 的 `max_length=64` 此前只在**单笔**路径有钉子
        （`tests/test_refund_partial.py::test_malformed_amount_reason_and_request_id_are_business_codes_not_500`）
        ——批量那份是 `ListField(child=CharField(max_length=64))`，字段层若被换成裸
        `ListField()`（例如为了「空数组文案由 `parse_order_nos` 给」而顺手放宽），
        这条路会静默变成 `DataError` 500。判据打业务码 + 不落行（E-0010 的形状）。
        """
        order = self._refundable_order()
        body = self._post(BATCH_REFUNDS_URL, {
            "order_nos": [order.order_no], "dry_run": False, "refund_total": "5.00",
            "reason": "测试原因", "request_ids": ["键" * 65],   # 65 字 > 列宽 64
        })
        self.assertEqual(body["code"], 1000)
        self.assertEqual(OrderRefund.objects.count(), 0)

    def test_each_order_uses_its_own_request_id(self):
        """幂等键**按下标**与 `order_nos` 对齐（spec §D9/§D13）——每笔用属于自己的那个键。"""
        a = self._refundable_order()
        b = self._refundable_order()
        body = self._post(BATCH_REFUNDS_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": False, "refund_total": "5.00",
            "reason": "测试原因", "request_ids": ["测试键-a", "测试键-b"],
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["applied"], [a.order_no, b.order_no])
        self.assertEqual(
            sorted(OrderRefund.objects.values_list("request_id", flat=True)),
            ["测试键-a", "测试键-b"],
        )
        # ⚠️ 上面那条 `sorted` 相等**判不出「两个键被对调」**（两笔都退款成功时集合一样）
        # ⇒ 再钉一次**配对**：这一条才是「每笔用自己的键」的真正判据。
        self.assertEqual(
            set(OrderRefund.objects.values_list("order__order_no", "request_id")),
            {(a.order_no, "测试键-a"), (b.order_no, "测试键-b")},
        )

    def test_amount_overrun_is_per_order(self):
        """`amount` 超限是**逐笔**结果：只拒小的那笔（1008），大的那笔仍 `eligible`。"""
        small = self._refundable_order("3.00")
        big = self._refundable_order("100.00")
        body = self._post(BATCH_REFUNDS_URL, {
            "order_nos": [small.order_no, big.order_no], "dry_run": True, "refund_total": "5.00",
            "reason": "测试原因", "request_ids": ["测试键-c", "测试键-d"],
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [big.order_no])
        self.assertEqual(body["data"]["ineligible"][0]["order_no"], small.order_no)
        self.assertEqual(body["data"]["ineligible"][0]["code"], 1008)
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(OrderRefund.objects.count(), 0)
        for order in (small, big):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(order.refund_fee, Decimal("0.00"))
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_dry_run_writes_nothing(self):
        """§4.3 第 1/2/3/5 条：不建 `OrderRefund`、不建调整行、`refund_fee` 逐列不动、零日志。"""
        a = self._refundable_order("100.00")
        b = self._refundable_order("100.00")
        before = {
            row.order_no: (row.refund_fee, row.updated_by)
            for row in Order.objects.filter(order_no__in=[a.order_no, b.order_no])
        }
        body = self._post(BATCH_REFUNDS_URL, {
            "order_nos": [a.order_no, b.order_no], "dry_run": True, "refund_total": "5.00",
            "reason": "测试原因", "request_ids": ["测试键-e", "测试键-f"],
        })
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["eligible"], [a.order_no, b.order_no])
        self.assertEqual(body["data"]["ineligible"], [])
        self.assertEqual(body["data"]["applied"], [])
        self.assertEqual(OrderRefund.objects.count(), 0)
        self.assertEqual(PerformanceAdjustment.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)
        for order in (a, b):
            order.refresh_from_db()
            with self.subTest(order_no=order.order_no):
                self.assertEqual(
                    (order.refund_fee, order.updated_by), before[order.order_no]
                )



class BatchEchoRowsCarrySourceTests(OrderBatchTestBase):
    """⭐ D-0301（2026-10-24）：批量出入参的**回显行**补 `source_order_no`
    （结果弹窗「来源号优先、空回退内置号」的取数前提）。"""

    SRC = "SRC-TEST-BATCH"

    def test_transition_per_order_rows_carry_the_source(self):
        order = self._order()
        Order.objects.filter(pk=order.pk).update(source_order_no=self.SRC)
        order.refresh_from_db()
        # ⚠️ **不带 `target`** ⇒ 走 `batch/transition` 的**读取形态**
        # （`transition_targets`，spec §D10：`targets` + 每笔 `allowed`）——
        # 带了 `target` 就是执行形态（`run_batch`，回的是 eligible / ineligible）。
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [order.order_no], "dry_run": True,
        })
        self.assertEqual(body["code"], 0, body)
        per_order = body["data"]["per_order"]
        self.assertEqual(len(per_order), 1)
        self.assertEqual(per_order[0]["source_order_no"], self.SRC)
        self.assertEqual(per_order[0]["order_no"], order.order_no)

    def test_ineligible_rows_carry_the_source_and_misses_carry_empty(self):
        """逐笔不合格的行带**那一单自己的**来源号；取不到行的 issue 是**空串**
        （没有行可读，前端回退 `order_no`）。"""
        # 一笔会被判不合格的单：已完成的单不能再「改为已完成」；给它带上来源号，
        # 让「不合格行带**那一单自己的**来源号」的断言有判别力（非空 ⇒ 不是凑巧的空串）。
        done = self._order(status=OrderStatus.COMPLETED)
        Order.objects.filter(pk=done.pk).update(source_order_no=self.SRC)
        body = self._post(BATCH_TRANSITION_URL, {
            "order_nos": [done.order_no, "20261024000000000999"],
            "dry_run": True, "target": OrderStatus.COMPLETED,
        })
        self.assertEqual(body["code"], 0)
        by_no = {row["order_no"]: row for row in body["data"]["ineligible"]}
        self.assertIn(done.order_no, by_no)
        self.assertEqual(by_no[done.order_no]["source_order_no"], self.SRC)
        missing = by_no["20261024000000000999"]
        self.assertEqual(missing["source_order_no"], "")
