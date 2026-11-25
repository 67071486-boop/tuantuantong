"""订单管理页「同步收款」按钮的后端网（计划
`docs/plans/2026-10-26-receipt-sync-button.md` §6 的 14 条）。

## 桩打在哪（E-0029 / E-0087）

出站 `order_detail` 在领域模块里是 `from apps.business.xgj import order_detail` 进来的名字
⇒ 桩打在**被查找的那个模块** `apps.business.xgj_receipt_sync.order_detail`，并先证它确实就是
`apps.business.xgj.order_detail`（桩接没接上本身要有判据）。

## 探针（计划 §1.1 / 「交付说明」）

计划要求「实现第一步先跑只读探针确认 `order/detail` 的 `order_status` 取值」。本文件用
**测试替身数据**证明那个**假设形态**（`ReceiptStatusContractTests`），⛔ **不打真网络**；
真机只读探针由父会话发版前在生产上跑——「未做真机探针」是如实登记的残差。

## 夹具值一律合成（E-0077）

AppKey / AppSecret / 来源单号 / 店铺标识全是仿形状的合成值。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest import mock

import psycopg
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import upstream_receipt_sync, xgj, xgj_push, xgj_receipt_sync
from apps.business.models import (
    Game,
    Order,
    OrderSource,
    OrderStatus,
    PaymentStatus,
    XgjShop,
    XgjShopBinding,
)
from apps.business.xgj import XgjError
from apps.core.error_codes import ErrorCode
from tests.hall_shadow_helpers import make_hidden_order, make_mirror
from tests.p5_auth_helpers import make_logged_in_client

DOMAIN = "tenant.test.com"
URL = "/api/business/orders/xgj-receipt-sync/"

#: 窗口日（显式给，⛔ 不依赖跑测试那天）。
DAY = date(2026, 10, 26)
#: 窗口内的固定时刻（本地时区正午）——避免「扫单日恰好跨午夜」的不确定性。
INSIDE = datetime(2026, 10, 26, 12, 0)

#: 合成来源单号（19 位纯数字，只仿形状）。
TB_NO = "22000000000000000001"
TB_NO_OTHER = "22000000000000000002"

#: 上游收款的判据常量（唯一出处 `xgj_push.RECEIPT_PUSH_STATUS`）。
RECEIPT_ACTION = xgj_push.RECEIPT_ACTION
PAYMENT_ACTION = "order.change_payment_status"


#: `_order(source=…)` 的「没传」哨兵（`None` 本身是**有语义**的取值：订单没填来源）。
_DEFAULT_SOURCE = object()


@contextmanager
def _other_db_connection():
    """**另一条**到同一测试库的连接：只为了让另一个会话真的持着 advisory lock。

    ⚠️ 同一连接上重复 `pg_try_advisory_lock` 是**可重入**的（总能拿到）⇒ 负方向必须用
    第二条连接（照 `tests/test_jidanpu_member_consume.py::_other_db_connection` 的形状；
    ⛔ 不把同一连接重入当成「别人占着」）。参数从当前连接取 ⇒ 自动指向 `test_<POSTGRES_DB>`。
    """
    params = connection.get_connection_params()
    kwargs = {
        key: params[key]
        for key in ("dbname", "user", "password", "host", "port")
        if key in params
    }
    conn = psycopg.connect(**kwargs, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


class _Base(TenantTestCase):
    """一条「来源 → 店 → 应用（凭据完整）」的解析链 + 一个持收款节点的登录账号。"""

    def setUp(self):
        self.game = Game.objects.create(name="收款同步测试游戏")
        self.owner = User.objects.create_user(username="receipt-owner", password="pw-receipt")
        self.source = OrderSource.objects.create(name="收款同步来源甲")
        self.binding = XgjShopBinding.objects.create(
            shop_name="收款同步应用甲", app_key="AK-SYNC-1", app_secret="SK-SYNC-1"
        )
        self.shop = XgjShop.objects.create(
            binding=self.binding,
            user_identity="ID-SYNC-1",
            shop_name="收款同步店铺甲",
            order_source=self.source,
        )
        self._seq = 0
        self.client = self._login("receipt-node-user", ("order.list", "order.change_payment_status"))

    # ── 夹具工具 ──────────────────────────────────────────────────────
    def _login(self, username, nodes):
        return make_logged_in_client(
            username=username, groups=(f"收款组{self._seq}",), nodes=nodes
        )

    def _next_no(self):
        self._seq += 1
        return f"202610260000000{self._seq:05d}"

    def _order(
        self,
        *,
        no=None,
        status=OrderStatus.UNASSIGNED,
        payment_status=PaymentStatus.UNSETTLED,
        source=_DEFAULT_SOURCE,
        source_order_no=TB_NO,
        created_at=None,
    ):
        order = Order.objects.create(
            game=self.game,
            order_no=no or self._next_no(),
            status=status,
            payment_status=payment_status,
            order_source=self.source if source is _DEFAULT_SOURCE else source,
            source_order_no=source_order_no,
        )
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.make_aware(created_at or INSIDE)
        )
        order.refresh_from_db()
        return order

    def _stub(self, **kwargs):
        return mock.patch.object(xgj_receipt_sync, "order_detail", **kwargs)

    def _run(self, **overrides):
        kwargs = {"date_from": DAY, "date_to": DAY, "schema_name": None}
        kwargs.update(overrides)
        return xgj_receipt_sync.run_xgj_receipt_sync(**kwargs)

    def _logs(self, order_no):
        return OperationLog.objects.filter(order_no=order_no)

    @staticmethod
    def _frozen_clock():
        """冻结成 0.0 的单调时钟 + 记录 sleep 的替身（限速判据用）。"""
        sleep = mock.Mock()
        clock = mock.Mock(return_value=0.0)
        return clock, sleep


# ═══════════ 探针 / 契约（计划 §1.1 的假设形态）═══════════════════════════


class ReceiptStatusContractTests(SimpleTestCase):
    """「`order/detail` 返回里的 `order_status` 取值 = `RECEIPT_PUSH_STATUS`」的**形态**证明。

    ⚠️ **用测试替身数据、⛔ 零真网络**：`order_detail` 的响应契约（`xgj.py::order_detail`
    docstring）是「`data` 就是订单对象本身」，本类只钉**我们这一侧怎么读它**：
    `22`（数字）/ `"22"`（字符串）都算成功，别的一律不算；判据常量只有一个出处。
    ⛔ **未做真机只读探针**（父会话发版前在生产上跑）——这是如实登记的残差。
    """

    #: 测试替身：按 `order/detail` 的契约（订单对象本身）最小化到我们读的那两个键。
    DUMMY_DETAIL = {"order_no": TB_NO, "order_status": 22}

    def test_the_success_status_constant_is_the_pinned_22(self):
        """钉**字面值**（E-0024）：拿常量跟它自己比改坏了不会红。"""
        self.assertEqual(xgj_push.RECEIPT_PUSH_STATUS, "22")

    def test_a_trade_success_detail_is_read_as_success_in_both_shapes(self):
        """上游可能给**数字**或**字符串** ⇒ `str(...).strip()` 归一后 `22` 两形状都成立。"""
        self.assertTrue(xgj_receipt_sync.receipt_success(self.DUMMY_DETAIL))
        self.assertTrue(
            xgj_receipt_sync.receipt_success({**self.DUMMY_DETAIL, "order_status": " 22 "})
        )

    def test_other_statuses_and_missing_keys_are_not_success(self):
        for raw in ("23", "24", 23, 24, 0, "", None):
            with self.subTest(order_status=raw):
                self.assertFalse(
                    xgj_receipt_sync.receipt_success(
                        {**self.DUMMY_DETAIL, "order_status": raw}
                    )
                )
        # 上游没给这个键（形状漂移）⇒ 不是成功，⛔ 不猜
        self.assertFalse(
            xgj_receipt_sync.receipt_success({"order_no": TB_NO})
        )

    def test_the_predicate_is_pinned_to_the_push_constant_not_a_second_literal(self):
        """**变异自证**：改掉 `xgj_push.RECEIPT_PUSH_STATUS` ⇒ 判据跟着变。

        如果这里另抄了一份 `"22"` 字面量，改上游常量它会**照样**判 22 成功 ⇒ 本条红。
        """
        with mock.patch.object(xgj_push, "RECEIPT_PUSH_STATUS", "99"):
            self.assertTrue(
                xgj_receipt_sync.receipt_success({**self.DUMMY_DETAIL, "order_status": "99"})
            )
            self.assertFalse(xgj_receipt_sync.receipt_success(self.DUMMY_DETAIL))


# ═══════════ ① 只查未收款 ═══════════════════════════════════════════════


class CandidateScopeTests(_Base):
    def test_only_unsettled_orders_are_queried(self):
        """UNSETTLED 才进候选；SETTLING / SETTLED 一律不发上游。"""
        unsettled = self._order(payment_status=PaymentStatus.UNSETTLED, source_order_no=TB_NO)
        self._order(payment_status=PaymentStatus.SETTLING, source_order_no=TB_NO_OTHER)
        self._order(payment_status=PaymentStatus.SETTLED, source_order_no=TB_NO_OTHER)

        with self._stub(return_value={"order_no": TB_NO, "order_status": "23"}) as stub:
            summary = self._run()

        self.assertEqual(stub.call_count, 1, "只有未收款那一张该被查")
        self.assertEqual(stub.call_args.args[0], TB_NO)
        self.assertEqual(summary["scanned"], 1)
        unsettled.refresh_from_db()
        self.assertEqual(unsettled.payment_status, PaymentStatus.UNSETTLED)


# ═══════════ ② 成功改已收款并留痕 ═══════════════════════════════════════


class ReceiptSuccessTests(_Base):
    def test_trade_success_marks_settled_and_writes_two_logs(self):
        """`22`（数字）与 `"22"`（字符串）两种上游形状都覆盖；一次成功 = 两条日志。"""
        for raw in (22, "22"):
            with self.subTest(order_status=raw):
                OperationLog.objects.all().delete()
                Order.objects.all().delete()
                order = self._order()
                with self._stub(
                    return_value={"order_no": TB_NO, "order_status": raw}
                ):
                    summary = self._run()

                order.refresh_from_db()
                self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
                self.assertEqual(summary["updated"], 1)
                self.assertEqual(summary["not_success"], 0)
                logs = self._logs(order.order_no)
                self.assertEqual(logs.count(), 2, "改了状态 + 那条带原因的收款留痕")
                self.assertCountEqual(
                    logs.values_list("action", flat=True),
                    [PAYMENT_ACTION, RECEIPT_ACTION],
                )
                self.assertEqual(
                    logs.get(action=RECEIPT_ACTION).reason, "交易成功"
                )


# ═══════════ ③ 非成功跳过 ═══════════════════════════════════════════════


class NotSuccessTests(_Base):
    def test_non_success_statuses_are_skipped_without_writes(self):
        for raw in ("23", "24", "0", None):
            with self.subTest(order_status=raw):
                OperationLog.objects.all().delete()
                Order.objects.all().delete()
                order = self._order()
                with self._stub(
                    return_value={"order_no": TB_NO, "order_status": raw}
                ):
                    summary = self._run()

                order.refresh_from_db()
                self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
                self.assertEqual(self._logs(order.order_no).count(), 0)
                self.assertEqual(summary["not_success"], 1)
                self.assertEqual(summary["updated"], 0)


# ═══════════ ④ 限速 ═════════════════════════════════════════════════════


class RateLimitTests(_Base):
    def test_two_orders_are_paced_by_one_over_qps(self):
        """qps=2（默认）⇒ 两次上游调用之间间隔 1/2 秒。"""
        self._order(source_order_no=TB_NO)
        self._order(source_order_no=TB_NO_OTHER)
        clock, sleep = self._frozen_clock()
        with self._stub(return_value={"order_status": "23"}), mock.patch.object(
            xgj_receipt_sync, "_monotonic", clock
        ), mock.patch.object(xgj_receipt_sync, "_sleep", sleep):
            self._run()

        self.assertEqual(sleep.call_count, 1, "2 笔之间恰好一次等待")
        self.assertAlmostEqual(sleep.call_args.args[0], 0.5)

    def test_the_qps_setting_changes_the_interval(self):
        """`XGJ_RECEIPT_SYNC_QPS=3` ⇒ 1/3 秒；判据必须真的读设置（⛔ 写死 2）。"""
        self._order(source_order_no=TB_NO)
        self._order(source_order_no=TB_NO_OTHER)
        clock, sleep = self._frozen_clock()
        with self.settings(XGJ_RECEIPT_SYNC_QPS=3), self._stub(
            return_value={"order_status": "23"}
        ), mock.patch.object(xgj_receipt_sync, "_monotonic", clock), mock.patch.object(
            xgj_receipt_sync, "_sleep", sleep
        ):
            self._run()

        self.assertEqual(sleep.call_count, 1)
        self.assertAlmostEqual(sleep.call_args.args[0], 1 / 3)


# ═══════════ ⑤ 单例（两个方向）═══════════════════════════════════════════


class SingletonLockTests(_Base):
    @staticmethod
    def _advisory_locks_of_this_session():
        """当前连接持有的 advisory lock 条数（锁泄漏判据：正常跑完必须 0）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
            return cursor.fetchone()[0]

    def test_the_lock_namespace_is_pinned(self):
        """命名空间是**锁的身份**（与 `jidanpu_member.OUTBOX_LOCK_NAMESPACE` 必须不同）。"""
        self.assertEqual(xgj_receipt_sync.SYNC_LOCK_NAMESPACE, 0x58474A52)
        self.assertNotEqual(
            xgj_receipt_sync.SYNC_LOCK_NAMESPACE,
            self._outbox_namespace(),
        )

    @staticmethod
    def _outbox_namespace():
        from apps.business.jidanpu_member import OUTBOX_LOCK_NAMESPACE

        return OUTBOX_LOCK_NAMESPACE

    def test_the_endpoint_returns_1037_when_another_session_holds_the_lock(self):
        """**另一条连接**持锁 ⇒ 端点回 1037（⛔ 不在同一连接上重入做负方向：可重入必假绿）。"""
        self._order()
        with _other_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [xgj_receipt_sync.SYNC_LOCK_NAMESPACE, xgj_receipt_sync.SYNC_LOCK_KEY],
                )
                self.assertTrue(cursor.fetchone()[0], "夹具没拿到锁 ⇒ 用例是假绿")

            response = self.client.post(
                URL, {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
                content_type="application/json",
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], ErrorCode.XGJ_RECEIPT_SYNC_BUSY)
        self.assertIn("正在运行", body["msg"])
        self.assertEqual(self._advisory_locks_of_this_session(), 0, "探测那条锁必须放掉")

    def test_the_body_gives_up_quietly_when_another_session_holds_the_lock(self):
        """线程体拿不到锁 ⇒ 直接退出（`ran=False`）、零上游调用、零写入。"""
        order = self._order()
        with _other_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [xgj_receipt_sync.SYNC_LOCK_NAMESPACE, xgj_receipt_sync.SYNC_LOCK_KEY],
                )
                self.assertTrue(cursor.fetchone()[0])

            with self._stub(return_value={"order_status": "22"}) as stub:
                summary = self._run()

        self.assertFalse(summary["ran"])
        stub.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(self._logs(order.order_no).count(), 0)

    def test_the_lock_is_released_after_a_run(self):
        """跑完锁必须放掉（否则下一次点永远 1037）；`pg_locks` 里本会话的 advisory 计数为 0。"""
        self._order()
        with self._stub(return_value={"order_status": "23"}):
            summary = self._run()

        self.assertTrue(summary["ran"])
        self.assertEqual(self._advisory_locks_of_this_session(), 0)

    def test_a_second_run_after_release_is_accepted(self):
        """释放后可再跑（端点正方向对照：⛔ 不是「永远 1037」）."""
        self._order()
        with self._stub(return_value={"order_status": "23"}), mock.patch.object(
            upstream_receipt_sync, "start_upstream_receipt_sync"
        ) as dispatch:
            response = self.client.post(
                URL, {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
                content_type="application/json",
            )

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], ErrorCode.OK)
        self.assertTrue(body["data"]["accepted"])
        dispatch.assert_called_once()


# ═══════════ ⑥ 范围 > 1 个月 ═════════════════════════════════════════════


class DateRangeValidationTests(_Base):
    def _post(self, date_from, date_to):
        return self.client.post(
            URL,
            {"date_from": date_from, "date_to": date_to},
            content_type="application/json",
        )

    def test_more_than_31_days_is_rejected_with_1000(self):
        """> 31 天 ⇒ 200 + 1000（后端 `validate()` 是判据）。"""
        response = self._post("2026-10-01", "2026-11-02")   # 33 天
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIn("31", body["msg"])

    def test_exactly_31_days_is_accepted(self):
        with mock.patch.object(upstream_receipt_sync, "start_upstream_receipt_sync") as dispatch:
            response = self._post("2026-10-01", "2026-10-31")   # 31 天
        self.assertEqual(response.json()["code"], ErrorCode.OK)
        dispatch.assert_called_once()

    def test_from_after_to_is_rejected_with_1000(self):
        body = self._post("2026-10-27", "2026-10-26").json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION)


# ═══════════ ⑦ 失败继续 ═════════════════════════════════════════════════


class UpstreamFailureContinuesTests(_Base):
    def test_one_upstream_failure_does_not_stop_the_batch(self):
        """第 1 单 `XgjError`、第 2 单正常 ⇒ 第 2 单被改、计 1 个 `upstream_error`。"""
        first = self._order(source_order_no=TB_NO)
        second = self._order(source_order_no=TB_NO_OTHER)
        with self._stub(
            side_effect=[XgjError(upstream_code=-1, upstream_msg="上游坏了"),
                         {"order_no": TB_NO_OTHER, "order_status": "22"}]
        ) as stub:
            summary = self._run()

        self.assertEqual(stub.call_count, 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(second.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(summary["upstream_error"], 1)
        self.assertEqual(summary["updated"], 1)

    def test_an_unexpected_exception_is_not_swallowed(self):
        """未预期异常 ⇒ 浮出（由线程体的 `logger.exception` 接），⛔ 不混进业务跳过。"""
        self._order()
        with self._stub(side_effect=RuntimeError("编程错误")):
            with self.assertRaises(RuntimeError):
                self._run()


# ═══════════ ⑧ 幂等（重读）═══════════════════════════════════════════════


class _FakeCandidateQs:
    """把候选集换成一份**逐行求值**的生成器（模拟「扫单期间被别的线改掉」）。

    ⚠️ 只替候选集的取数，不替被测循环：`run_xgj_receipt_sync` 照旧按 pk **重读**。
    """

    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_args):
        return self

    def iterator(self, chunk_size=200):
        return iter(self._rows)


class ReReadIdempotencyTests(_Base):
    def test_a_row_settled_mid_scan_is_skipped_without_an_upstream_call(self):
        """扫描期间被推送线改成已结款 ⇒ 重读后跳过：不调上游、不写日志、计 `settled_skip`。"""
        first = self._order(source_order_no=TB_NO)
        second = self._order(source_order_no=TB_NO_OTHER)

        def rows():
            yield first
            # 模拟推送线在这两张之间把第二张改成了已结款（真写库）。
            Order.objects.filter(pk=second.pk).update(
                payment_status=PaymentStatus.SETTLED
            )
            yield second

        with mock.patch.object(
            xgj_receipt_sync, "candidates_qs", return_value=_FakeCandidateQs(rows())
        ), self._stub(return_value={"order_status": "22"}) as stub:
            summary = self._run()

        self.assertEqual(stub.call_count, 1, "只有第一张该被查")
        self.assertEqual(summary["settled_skip"], 1)
        self.assertEqual(self._logs(second.order_no).count(), 0)


# ═══════════ ⑨ 上限 `MAX_ORDERS_PER_RUN`（起 5000；超限零上游）═══════


class OrderCapTests(_Base):
    def test_more_than_the_cap_is_rejected_without_any_upstream_call(self):
        rows = [
            Order(
                game=self.game,
                order_no=f"20261026{i:012d}",
                status=OrderStatus.UNASSIGNED,
                payment_status=PaymentStatus.UNSETTLED,
                order_source=self.source,
                source_order_no=f"TB{i:018d}",
            )
            for i in range(xgj_receipt_sync.MAX_ORDERS_PER_RUN + 1)
        ]
        Order.objects.bulk_create(rows)
        Order.objects.filter(order_no__startswith="20261026").update(
            created_at=timezone.make_aware(INSIDE)
        )
        self.assertEqual(
            xgj_receipt_sync.count_candidates(DAY, DAY),
            xgj_receipt_sync.MAX_ORDERS_PER_RUN + 1,
        )

        with self._stub(return_value={"order_status": "22"}) as stub:
            response = self.client.post(
                URL, {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
                content_type="application/json",
            )

        body = response.json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIn("缩小", body["msg"])
        stub.assert_not_called()
        self.assertEqual(
            Order.objects.filter(payment_status=PaymentStatus.SETTLED).count(), 0
        )


# ═══════════ ⑩ 鉴权 ═════════════════════════════════════════════════════


class AuthTests(_Base):
    def test_anonymous_is_401(self):
        client = Client(SERVER_NAME=DOMAIN)
        response = client.post(
            URL, {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_list_only_node_is_403(self):
        client = make_logged_in_client(
            username="receipt-list-only", groups=("只看组",), nodes=("order.list",)
        )
        response = client.post(
            URL, {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403, response.content[:300])


# ═══════════ ⑪ 凭据回落（四例）══════════════════════════════════════════


class CredentialResolutionTests(_Base):
    def test_a_source_with_a_complete_binding_uses_that_binding(self):
        self._order()
        with self._stub(return_value={"order_status": "23"}) as stub:
            self._run()
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(stub.call_args.args[0], TB_NO)
        self.assertEqual(stub.call_args.kwargs["app_key"], "AK-SYNC-1")
        self.assertEqual(stub.call_args.kwargs["app_secret"], "SK-SYNC-1")

    def test_a_source_whose_shop_has_no_binding_is_skipped(self):
        """店在、`binding=None` ⇒ 进不了候选（零上游）；逐行兜底也把它记成 `skipped_no_binding`。"""
        other_source = OrderSource.objects.create(name="收款同步来源乙")
        XgjShop.objects.create(
            binding=None, user_identity="ID-SYNC-2", shop_name="没应用的店",
            order_source=other_source,
        )
        order = self._order(source=other_source)

        self.assertEqual(xgj_receipt_sync.count_candidates(DAY, DAY), 0)
        with self._stub(return_value={"order_status": "22"}) as stub:
            summary = self._run()
        stub.assert_not_called()
        self.assertEqual(summary["scanned"], 0, "候选集已把它排除 ⇒ 一行都不扫")

        # 逐行兜底的判据：绕过候选集直接把这张单喂进循环。
        with mock.patch.object(
            xgj_receipt_sync, "candidates_qs",
            return_value=_FakeCandidateQs([order]),
        ), self._stub(return_value={"order_status": "22"}) as stub2:
            summary = self._run()
        stub2.assert_not_called()
        self.assertEqual(summary["skipped_no_binding"], 1)

    def test_a_source_whose_binding_has_blank_credentials_is_skipped(self):
        """有店有绑定但凭据不全（空 secret）⇒ 同样跳过、零上游。"""
        broken = XgjShopBinding.objects.create(
            shop_name="收款同步应用乙", app_key="AK-SYNC-2", app_secret=""
        )
        other_source = OrderSource.objects.create(name="收款同步来源丙")
        XgjShop.objects.create(
            binding=broken, user_identity="ID-SYNC-3", shop_name="半配置的店",
            order_source=other_source,
        )
        order = self._order(source=other_source)

        self.assertEqual(xgj_receipt_sync.count_candidates(DAY, DAY), 0)
        with mock.patch.object(
            xgj_receipt_sync, "candidates_qs",
            return_value=_FakeCandidateQs([order]),
        ), self._stub(return_value={"order_status": "22"}) as stub:
            summary = self._run()
        stub.assert_not_called()
        self.assertEqual(summary["skipped_no_binding"], 1)

    def test_null_source_uses_the_single_complete_binding(self):
        """`order_source=None` + 本租户**恰好一个**完整 binding ⇒ 用它兜底（并进候选）。"""
        order = self._order(source=None)
        self.assertIsNone(order.order_source_id)
        self.assertEqual(xgj_receipt_sync.count_candidates(DAY, DAY), 1)
        with self._stub(return_value={"order_status": "22"}) as stub:
            summary = self._run()
        self.assertEqual(stub.call_count, 1)
        self.assertEqual(stub.call_args.kwargs["app_key"], "AK-SYNC-1")
        self.assertEqual(summary["updated"], 1)

    def test_null_source_with_two_complete_bindings_is_skipped(self):
        """`order_source=None` + 两个完整 binding ⇒ 不猜，跳过、零上游。"""
        second = XgjShopBinding.objects.create(
            shop_name="收款同步应用丙", app_key="AK-SYNC-3", app_secret="SK-SYNC-3"
        )
        XgjShop.objects.create(
            binding=second, user_identity="ID-SYNC-4", shop_name="第二家店",
            order_source=OrderSource.objects.create(name="收款同步来源丁"),
        )
        order = self._order(source=None)

        self.assertEqual(xgj_receipt_sync.count_candidates(DAY, DAY), 0)
        with mock.patch.object(
            xgj_receipt_sync, "candidates_qs",
            return_value=_FakeCandidateQs([order]),
        ), self._stub(return_value={"order_status": "22"}) as stub:
            summary = self._run()
        stub.assert_not_called()
        self.assertEqual(summary["skipped_no_binding"], 1)


# ═══════════ ⑫ 时间边界 ═════════════════════════════════════════════════


class WindowBoundaryTests(_Base):
    def test_the_window_is_half_open_on_local_midnight(self):
        """当日 23:30 在范围内、次日 00:30 不在（`[00:00, 次日 00:00)`、`make_aware`）。"""
        late = self._order(
            source_order_no=TB_NO,
            created_at=datetime(2026, 10, 26, 23, 30),
        )
        next_day = self._order(
            source_order_no=TB_NO_OTHER,
            created_at=datetime(2026, 10, 27, 0, 30),
        )
        found = list(xgj_receipt_sync.candidates_qs(date_from=DAY, date_to=DAY).values_list("pk", flat=True))
        self.assertEqual(found, [late.pk])
        self.assertNotIn(next_day.pk, found)

    def test_the_day_before_is_outside_the_window(self):
        before = self._order(
            source_order_no=TB_NO,
            created_at=datetime(2026, 10, 25, 23, 59),
        )
        self.assertEqual(xgj_receipt_sync.count_candidates(DAY, DAY), 0)
        self.assertNotIn(
            before.pk,
            list(xgj_receipt_sync.candidates_qs(date_from=DAY, date_to=DAY).values_list("pk", flat=True)),
        )


# ═══════════ ⑬ 隐藏单与空白单号（零上游）═════════════════════════════════


class ShadowAndBlankSourceTests(_Base):
    def test_hidden_orders_and_blank_source_numbers_never_reach_upstream(self):
        card = make_mirror(game=self.game, bound_by=self.owner, no="SYN-RCPT-CARD-1")
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261026000000000901"
        )
        Order.objects.filter(pk=hidden.pk).update(
            payment_status=PaymentStatus.UNSETTLED,
            created_at=timezone.make_aware(INSIDE),
        )
        blank = self._order(source_order_no="   ")
        self.assertEqual(blank.source_order_no, "   ")

        self.assertEqual(xgj_receipt_sync.count_candidates(DAY, DAY), 0)
        with self._stub(return_value={"order_status": "22"}) as stub:
            summary = self._run()

        stub.assert_not_called()
        self.assertEqual(summary["scanned"], 0)
        hidden.refresh_from_db()
        self.assertEqual(hidden.payment_status, PaymentStatus.UNSETTLED)


# ═══════════ ⑭ 请求不阻塞 ═══════════════════════════════════════════════


class AsyncDispatchTests(_Base):
    def test_the_endpoint_starts_a_daemon_thread_and_does_not_wait(self):
        """请求先返回、线程后跑；请求期间零外呼（照 `test_xgj_auto_ship.AsyncDispatchTests`）。"""
        import threading

        self._order()
        entered, release, runner = threading.Event(), threading.Event(), []

        def blocked(*, date_from, date_to, schema_name=None):
            runner.append(threading.current_thread())
            entered.set()
            release.wait(timeout=10)

        with mock.patch.object(
            upstream_receipt_sync, "_sync_in_thread", side_effect=blocked
        ), self._stub(return_value={"order_status": "22"}) as stub:
            response = self.client.post(
                URL, {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
                content_type="application/json",
            )
            # 请求已经拿到响应，而线程体还堵在事件上 ⇒ 请求没等它。
            self.assertTrue(entered.wait(timeout=5), "线程体没被启动")
            self.assertEqual(response.json()["code"], ErrorCode.OK)
            stub.assert_not_called()   # 请求期间零外呼

        release.set()
        runner[0].join(timeout=5)
        self.assertFalse(runner[0].is_alive(), "线程没退出（测试自身别留悬线程）")
        self.assertTrue(runner[0].daemon, "必须是 daemon（进程重启即丢，不阻塞退出）")

    def test_the_body_seam_runs_synchronously_for_db_side_assertions(self):
        """可直调缝隙：`run_xgj_receipt_sync(schema_name=None)` 在测试线程里同步跑完。"""
        order = self._order()
        with self._stub(return_value={"order_status": "22"}):
            summary = self._run()
        self.assertTrue(summary["ran"])
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)


# ═══════════ 桩接线自证（E-0029）════════════════════════════════════════


class StubWiringTests(SimpleTestCase):
    def test_the_module_global_is_the_looked_up_name(self):
        """`xgj_receipt_sync.order_detail` 必须就是 `xgj.order_detail`（桩打在对的模块上）。"""
        self.assertIs(xgj_receipt_sync.order_detail, xgj.order_detail)
