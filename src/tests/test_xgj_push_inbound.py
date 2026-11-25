"""闲管家推送接收端的行为网（spec `2026-10-04-xgj-push-inbound-design.md` §8 的 12 条判据）。

逐条落点（spec §8 的编号 → 本文件的用例）：

| §8 | 判据 | 用例 |
|---|---|---|
| 1 | 验签（含**厂商公开文档的样例向量**） | `SignVectorTests` + `SignatureTests` |
| 2 | 只处理 `order_status ∈ {23, 24}` | `StatusFilterTests` |
| 3 | 已退款 / 已关闭 ⇒ 已退单（两档各一条） | `AutoRefundTests`（另加一条：`order_status` 用 JSON **数字**推送） |
| 4 | 同一来源单号命中多张 ⇒ 全部退 | `MultipleMatchesTests` |
| 5 | 来源单号带首尾空白也能命中 | `WhitespaceKeyTests`（＋`TrimCharacterSetTests`：削白**字符集**两侧同一套，F1 追加） |
| 6 | 幂等：已是 `REFUNDED` ⇒ 跳过、不新增日志 | `IdempotenceTests` |
| 7 | 已结账 ⇒ 单独那条日志 + 状态不变 | `SettledTests`（＋`IllegalTransitionTests`：`IllegalTransition` ⇒ 跳过，F7 追加） |
| 8 | 找不到单 ⇒ `success` 且零写入 | `NoMatchTests`（＋空 `order_no` 那道承重闸门，F4 追加） |
| 9 | 商品推送 ⇒ `success` 且零写入 | `ProductPushTests` |
| 10 | 跨租户隔离 | `XgjPushIsolationTests` |
| 11 | 未预期异常 ⇒ `fail` + 不是 500 | `UnexpectedErrorTests` |
| 12 | 非信封响应形状（键集**恰好**两个键） | `ResponseShapeTests` |

## 三条纪律（本文件怎么被写出来的）

- **副作用判据打在数据库行上**（E-0009）：「零写入」一律断 `OperationLog` / `Order` 的行数
  与**行里的值**，⛔ 不用 `assertLogs` 断 `logger.*`（`assertLogs` 会把目标 logger 的级别抬到
  INFO ⇒ 「生产一行都没写」的用例全绿）。
- **跨租户的否定断言不用主键指认另一个租户的行**（两个 schema 的主键序列各自从 1 开始）：
  判据用**租户内唯一、且两租户取值不同**的 `source_order_no`。
- **`mock.patch` 打在「被查找的那个模块」上**（E-0029）：`handle_order_push` 是视图模块里
  `from ... import` 进来的名字 ⇒ 桩打在 `apps.business.api.xgj_push_views`，
  并**先证**它确实就是被查找的那个对象。

## 夹具值一律合成（E-0077）

`APP_KEY` / `APP_SECRET` / `SOURCE_NO` / 时间戳都是**合成值**（只仿形状）。
⚠️ **唯一的例外**是 `SignVectorTests` 里那组签名向量——见那个类的 docstring。
"""
from __future__ import annotations

import hashlib
import itertools
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business import dailiantong_actions, hall_shadow, upstream_hall_shadow, xgj_push
from apps.business.api import xgj_push_views
from apps.business.dailiantong_client import DailiantongError
from apps.business.models import (
    Approval,
    DailiantongPush,
    EmpType,
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderSource,
    OrderStatus,
    PaymentStatus,
    Performance,
    SettleStatus,
    SystemSwitch,
    XgjReceiptRecord,
    XgjShopBinding,
)
from apps.business.order_status import IllegalTransition
from apps.platform.provisioning import provision_tenant

ORDER_URL = "/api/business/xgj/push/order/"
PRODUCT_URL = "/api/business/xgj/push/product/"

#: 租户 A 的域名 = `TenantTestCase` 那个 test schema 的主域名（与 `p5_auth_helpers` 同口径）。
#: ⚠️ 每条请求都必须显式带上它（`SERVER_NAME`）：两个推送端点**免登录**，所以本文件不用
#: `make_logged_in_client`，但**租户上下文仍然只来自域名**（`AGENTS.md` §4）——
#: 不带域名的请求会被 `TenantMainMiddleware` 判成「没有这个租户」而 404，
#: 且它在那之前已经把连接切到 public schema（后续用例会在错的 schema 上建夹具）。
DOMAIN_A = "tenant.test.com"

#: 本租户那个闲管家应用的凭据（**合成值**，⛔ 不是任何真实应用）。
APP_KEY = "AK-PUSH-TEST-1"
APP_SECRET = "SK-PUSH-TEST-1"
#: 合成时间戳（仿 10 位形状）。本线**不做**新鲜度窗口（spec §D10）⇒ 值本身不影响判据。
TS = "1780000000"
#: 合成来源单号（19 位纯数字，仿闲鱼订单号的形状；⛔ 不是真实订单号）。
SOURCE_NO = "22000000000000000001"
SOURCE_NO_OTHER = "22000000000000000002"

_ORDER_SEQ = itertools.count(1)


def _order_no() -> str:
    """本仓订单号（20 位；`Order.order_no` 列宽 20）。合成值、用例之间不重复。"""
    return "2026" + f"{next(_ORDER_SEQ):016d}"


def _md5(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def sign_of(body: bytes, *, app_key: str = APP_KEY, app_secret: str = APP_SECRET,
            timestamp: str = TS) -> str:
    """按 spec §2.2 给一个 body 算签名。

    ⚠️ **用例侧独立实现**（`hashlib` 直接算），⛔ 不调 `xgj_push.sign_for`：
    否则「签名算错」这种缺陷会让所有用例自洽地一起通过（自指断言，E-0024）。
    公式本身对不对由 `SignVectorTests` 的厂商样例向量钉住。
    """
    return _md5(f"{app_key},{_md5(body)},{timestamp},{app_secret}")


def _query(*, appid: str = APP_KEY, timestamp: str = TS, sign: str = "0" * 32,
           omit: str | None = None) -> str:
    params = {"appid": appid, "timestamp": timestamp, "sign": sign}
    if omit is not None:
        params.pop(omit)
    return urlencode(params)


class SignVectorTests(TenantTestCase):
    """§8-1 的第一半：**厂商公开文档里的样例签名向量**（逐字，⛔ 不许删）。

    出处 = 闲管家开放平台共享文档《接入说明》（<https://s.apifox.cn/3ac13d69-5a38-4536-ae9b-a54001854ef8/doc-2686716>，
    2026-10-04 取；spec §2.2 的公式与 spec §8-1 的这条夹具都指向它）。

    ⚠️ **这是公开发布文档里的示例值，不是任何真实租户的凭据**（`appKey` / `appSecret` /
    `timestamp` / `body` / `sign` 五个值逐字照抄）。它与本文件其它夹具的纪律（一律合成值，
    E-0077）**不冲突**：这条用例是「我们抄的公式没错」的**唯一机械证据**——
    ⛔ 不许删、不许换成合成值、不许把它「顺手简化」成一个自指的断言。
    """

    #: 文档给的那五个值（⛔ 一个字符都不要改）。
    DOC_APP_KEY = "203413189371893"
    DOC_APP_SECRET = "o9wl81dncmvby3ijpq7eur456zhgtaxs"
    DOC_TIMESTAMP = "1636087298"
    DOC_BODY = b'{"product_id":"219530767978565"}'
    DOC_BODY_MD5 = "2608f2139cca8755cabf25209251e549"
    DOC_SIGN = "c26c8a48809141f3dd80bd9b9ddb41ea"

    def test_the_documented_sample_vector(self):
        """我们的 `body_md5` / `sign_for` 必须逐字复现文档给的那两个摘要。"""
        self.assertEqual(xgj_push.body_md5(self.DOC_BODY), self.DOC_BODY_MD5)
        self.assertEqual(
            xgj_push.sign_for(
                app_key=self.DOC_APP_KEY,
                body_md5_hex=self.DOC_BODY_MD5,
                timestamp=self.DOC_TIMESTAMP,
                app_secret=self.DOC_APP_SECRET,
            ),
            self.DOC_SIGN,
        )

    def test_the_test_side_reimplementation_agrees_with_the_vector(self):
        """用例侧那份独立实现也要复现同一个向量（否则 §8-1 其余用例白测）。

        ⚠️ 这不是同义反复：`sign_of` 与 `xgj_push.sign_for` 是**两份**独立代码
        （一份在 `src/tests/`、一份在 `src/apps/`）；两边都对上文档，才说明
        「夹具的签名」与「被测的验收」是同一套公式。
        """
        self.assertEqual(
            sign_of(
                self.DOC_BODY, app_key=self.DOC_APP_KEY,
                app_secret=self.DOC_APP_SECRET, timestamp=self.DOC_TIMESTAMP,
            ),
            self.DOC_SIGN,
        )


class _PushFixture(TenantTestCase):
    """一条「有应用凭据」的租户夹具 + 推送请求的三个小工具。"""

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回本租户。跨域名请求**不会**在响应结束时还原 schema
        #    （`django_tenants` 的中间件不还原；404 那条路还会先把连接切到 public）
        #    ⇒ 上一条用例可能把连接留在别的 schema 上。同 `test_xgj_shop_binding.py` 的先例。
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="测试游戏甲")
        self.source = OrderSource.objects.create(name="测试来源甲")
        self.binding = XgjShopBinding.objects.create(
            shop_name="测试应用甲", app_key=APP_KEY, app_secret=APP_SECRET,
        )

    # ── 夹具 ──────────────────────────────────────────────────────────
    def make_order(self, *, source_order_no: str = SOURCE_NO,
                   status: str = OrderStatus.UNASSIGNED, **extra) -> Order:
        return Order.objects.create(
            game=self.game, order_no=_order_no(), status=status,
            order_source=self.source, source_order_no=source_order_no, **extra,
        )

    @staticmethod
    def push_body(*, order_no: str = SOURCE_NO, order_status: str = "23") -> bytes:
        """一条订单推送的 body（键名照文档：匹配键在报文里叫 `order_no`）。

        ⚠️ 只列本线会读的两个键**有意义**，其余键照文档形状一起发（证明未知键会被忽略、
        也不会 500）。
        """
        return json.dumps(
            {
                "seller_id": "0",
                "user_name": "测试卖家甲",
                "order_no": order_no,
                "order_type": "1",
                "order_status": order_status,
                "refund_status": "1",
                "modify_time": "2026-10-04 12:00:00",
                "product_id": "219530767978565",
                "item_id": "1",
                "address_status": "1",
            },
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")

    # ── 请求 ──────────────────────────────────────────────────────────
    def post_body(self, url: str, body: bytes, *, query: str, **extra):
        """按给定 query 发一条原始 body（`request.body` 必须逐字节等于 `body`）。"""
        extra.setdefault("SERVER_NAME", DOMAIN_A)
        return self.client.post(
            f"{url}?{query}", body, content_type="application/json", **extra
        )

    def push_order(self, body: bytes, *, sign: str | None = None, **extra):
        return self.post_body(
            ORDER_URL, body,
            query=_query(sign=sign_of(body) if sign is None else sign), **extra,
        )

    def push_product(self, body: bytes, *, sign: str | None = None, **extra):
        return self.post_body(
            PRODUCT_URL, body,
            query=_query(sign=sign_of(body) if sign is None else sign), **extra,
        )

    # ── 判据小工具 ────────────────────────────────────────────────────
    @staticmethod
    def actions_of(order_no: str) -> list[str]:
        """该订单的操作日志动作码，按写入顺序。"""
        return list(
            OperationLog.objects.filter(order_no=order_no)
            .order_by("id").values_list("action", flat=True)
        )


# ── §8-1 验签 ─────────────────────────────────────────────────────────────

class SignatureTests(_PushFixture):
    def test_a_correct_signature_is_accepted(self):
        response = self.push_order(self.push_body(order_status="12"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

    def test_a_signature_with_one_character_changed_is_rejected(self):
        body = self.push_body(order_status="12")
        good = sign_of(body)
        tampered = ("0" if good[0] != "0" else "1") + good[1:]
        self.assertNotEqual(tampered, good)  # 自证：变异确实应用了
        response = self.push_order(body, sign=tampered)
        self.assertEqual(response.json(), {"result": "fail", "msg": "签名失败"})

    def test_an_extra_space_in_the_body_breaks_the_signature(self):
        """**原文变了、sign 没跟着变** ⇒ 不匹配。

        这一条钉的是「验签用的是收到的**原始字节**」：body 尾部多一个空格后 JSON 仍能解析、
        业务字段一个都没变，若实现拿「解析回来再 dumps」的字符串去验签，这里会**假通过**。
        """
        body = self.push_body(order_status="12")
        response = self.push_order(body + b" ", sign=sign_of(body))
        self.assertEqual(response.json(), {"result": "fail", "msg": "签名失败"})

    def test_an_unknown_appid_is_rejected(self):
        body = self.push_body(order_status="12")
        response = self.post_body(
            ORDER_URL, body,
            query=_query(appid="AK-NOT-IN-THIS-TENANT", sign=sign_of(body)),
        )
        self.assertEqual(response.json(), {"result": "fail", "msg": "签名失败"})

    def test_missing_signature_params_are_rejected(self):
        body = self.push_body(order_status="12")
        sign = sign_of(body)
        for missing in ("appid", "timestamp", "sign"):
            with self.subTest(missing=missing):
                response = self.post_body(
                    ORDER_URL, body, query=_query(sign=sign, omit=missing)
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json(), {"result": "fail", "msg": "缺少签名参数"}
                )

    def test_a_signature_from_another_binding_is_rejected(self):
        """另一条绑定的密钥算出来的签名不算通过（凭据是按 `appid` 找到的那一行）。"""
        other_key, other_secret = "AK-PUSH-TEST-2", "SK-PUSH-TEST-2"
        XgjShopBinding.objects.create(
            shop_name="测试应用乙", app_key=other_key, app_secret=other_secret,
        )
        body = self.push_body(order_status="12")
        response = self.push_order(
            body, sign=sign_of(body, app_key=other_key, app_secret=other_secret)
        )
        self.assertEqual(response.json(), {"result": "fail", "msg": "签名失败"})

    def test_a_binding_whose_app_key_has_surrounding_whitespace_still_verifies(self):
        """F5：库里 `app_key` 带首尾空白（复制粘贴 / 历史数据 / 直接写库）⇒ 仍要验签通过并退单。

        ⚠️ 出站那条 `credentials_of` 一直 `strip()`（它的 docstring 点名是为「历史数据 / 直接从
        shell 写库」兜底）；入站按字面 `filter(app_key=appid)` 比，同一份绑定就会「出站能用、
        入站持续 `签名失败`」，而那个症状与「验签公式抄错」**一模一样**（spec §6-1）
        ⇒ 这条把**两侧归一化钉成同一套**。
        """
        # 模拟「绕过序列化器」那条路（用 queryset.update 直接改库，不经 validate）。
        XgjShopBinding.objects.filter(pk=self.binding.pk).update(app_key=f"  {APP_KEY}\n")
        stored = XgjShopBinding.objects.get(pk=self.binding.pk)
        # 自证：库里那一位确实带空白（否则这条用例什么都没测）。
        self.assertEqual(stored.app_key, f"  {APP_KEY}\n")

        order = self.make_order()
        response = self.push_order(self.push_body(order_status="23"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)


# ── §8-2 只处理两个状态 ───────────────────────────────────────────────────

class StatusFilterTests(_PushFixture):
    def test_statuses_other_than_refund_or_closed_change_nothing(self):
        """11 待付款 / 12 待发货 / 21 已发货 ⇒ `success` 且**零写入**。

        ⚠️ **22 已完成已移出本循环**（收款线，评审 H-3）：22 从「ack 零痕迹」
        变成「记账 + 改收款状态」，它的行为网在 `tests/test_xgj_receipt_push.py`
        （含 `modify_time` 按文档用**秒级整数**的新用例——本夹具发的还是字符串形状）。
        """
        order = self.make_order()
        logs_before = OperationLog.objects.count()
        orders_before = Order.objects.count()
        records_before = XgjReceiptRecord.objects.count()
        for status in ("11", "12", "21"):
            with self.subTest(order_status=status):
                response = self.push_order(self.push_body(order_status=status))
                self.assertEqual(
                    response.json(), {"result": "success", "msg": "接收成功"}
                )
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.UNASSIGNED)
                self.assertEqual(OperationLog.objects.count(), logs_before)
                self.assertEqual(Order.objects.count(), orders_before)
                self.assertEqual(XgjReceiptRecord.objects.count(), records_before)


# ── §8-3 已退款 / 已关闭 ⇒ 已退单 ─────────────────────────────────────────

class AutoRefundTests(_PushFixture):
    def test_a_refund_push_turns_the_order_refunded(self):
        order = self.make_order()
        response = self.push_order(self.push_body(order_status="23"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        # 判据打在**数据库行**上（E-0009），不是响应体。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        # 两条日志：①既有动作码的状态事实 ②本线新增的带原因人话（spec §D5）。
        self.assertEqual(
            self.actions_of(order.order_no),
            ["order.transition", "order.xgj_push_refund"],
        )
        refund_log = OperationLog.objects.get(
            order_no=order.order_no, action="order.xgj_push_refund"
        )
        self.assertIn("退款", refund_log.summary)

    def test_a_numeric_order_status_in_the_json_body_is_accepted(self):
        """`order_status` 用 **JSON 数字**推送也要认（文档列的是 `11` / `12` / `23` 这种值）。

        ⚠️ 这条钉的是视图那个 `CharField` 的**实际行为**（DRF 把 `int` 转成 `"23"`），
        不是我在注释里的一句声明——`json.dumps` 在这里写出的是真正的 JSON 数字。
        """
        order = self.make_order()
        body = json.dumps(
            {"order_no": SOURCE_NO, "order_status": 23},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        self.assertIn(b'"order_status":23', body)  # 自证：发出去的确实是数字
        response = self.push_order(body)
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)

    def test_a_closed_push_turns_the_order_refunded(self):
        order = self.make_order()
        response = self.push_order(self.push_body(order_status="24"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        refund_log = OperationLog.objects.get(
            order_no=order.order_no, action="order.xgj_push_refund"
        )
        self.assertIn("关闭", refund_log.summary)

    def test_the_order_log_records_the_state_fact_with_chinese_labels(self):
        """①那一行的前后值是**中文标签**（与 `/transition/` 同口径）。"""
        order = self.make_order(status=OrderStatus.IN_PROGRESS)
        self.push_order(self.push_body(order_status="23"))
        transition_log = OperationLog.objects.get(
            order_no=order.order_no, action="order.transition"
        )
        self.assertEqual(transition_log.from_value, "代练中")
        self.assertEqual(transition_log.to_value, "已退单")


# ── §8-4 命中多张 ⇒ 全部退 ────────────────────────────────────────────────

class PlatformRefundRoutingTests(_PushFixture):
    def make_platform_push(
        self, order, *, state=DailiantongPush.State.SENT, remote_status=11,
        serial_no="20000000000000000004", **extra,
    ):
        return DailiantongPush.objects.create(
            order=order, platform=10, state=state,
            serial_no=serial_no, remote_status=remote_status, price=Decimal("30.00"), **extra,
        )

    def use_platform_client(self):
        client = mock.Mock()
        self.enterContext(mock.patch.object(dailiantong_actions, "_active_integration"))
        self.enterContext(mock.patch.object(dailiantong_actions, "_open_client", return_value=client))
        return client

    def test_the_disabled_switch_leaves_the_order_untouched(self):
        order = self.make_order()
        self.make_platform_push(order)
        client = self.use_platform_client()
        SystemSwitch.objects.create(key="auto_refund_platform_order", value="N")

        result = self.push_order(self.push_body()).json()

        self.assertEqual(result, {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self.actions_of(order.order_no), [])
        self.assertEqual(client.mock_calls, [])
        self.assertFalse(Approval.objects.filter(original_order=order).exists())

    def test_unaccepted_platform_order_is_deleted_and_verified_before_local_refund(self):
        order = self.make_order(status=OrderStatus.WAITING_GRAB)
        push = self.make_platform_push(order)
        client = self.use_platform_client()

        def read_detail(_serial):
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
            if client.delete_order.called:
                return {}
            return {"Status": 11, "CancelStatus": 10}

        client.order_detail.side_effect = read_detail
        result = self.push_order(self.push_body()).json()
        retry = self.push_order(self.push_body(order_status="24")).json()

        self.assertEqual(result, {"result": "success", "msg": "接收成功"})
        self.assertEqual(retry, result)
        self.assertEqual(client.order_detail.call_args_list, [mock.call(push.serial_no)] * 2)
        client.delete_order.assert_called_once_with(push.serial_no)
        client.cancel_order.assert_not_called()
        order.refresh_from_db()
        push.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertEqual(self.actions_of(order.order_no).count("order.xgj_push_refund"), 1)
        self.assertFalse(Approval.objects.filter(original_order=order).exists())

    def assert_one_refund_task(self, order, *, reason_fragment):
        approval = Approval.objects.get(original_order=order)
        self.assertEqual(approval.kind, "refund")
        self.assertEqual(approval.status, "pending")
        self.assertIn(reason_fragment, approval.reason)
        self.assertFalse(Order.objects.filter(penalty_approval=approval).exists())

    def test_fresh_accepted_status_keeps_original_order_and_queues_one_refund_task(self):
        order = self.make_order(status=OrderStatus.IN_PROGRESS)
        push = self.make_platform_push(order, is_active=False)
        client = self.use_platform_client()
        client.order_detail.return_value = {"Status": 12, "CancelStatus": 10, "AcceptUID": "123456"}

        for status in ("23", "24", "23"):
            result = self.push_order(self.push_body(order_status=status)).json()
            self.assertEqual(result, {"result": "success", "msg": "接收成功"})

        client.order_detail.assert_called_once_with(push.serial_no)
        client.delete_order.assert_not_called()
        client.cancel_order.assert_not_called()
        order.refresh_from_db()
        push.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assert_one_refund_task(order, reason_fragment="正在代练")

    def test_acceptance_during_delete_routes_to_manual_without_cancel(self):
        order = self.make_order(status=OrderStatus.WAITING_GRAB)
        push = self.make_platform_push(order)
        client = self.use_platform_client()
        client.order_detail.return_value = {"Status": 11, "CancelStatus": 10}
        client.delete_order.side_effect = DailiantongError(
            code=-4, message="订单已被他人接手或已删除", action="LevelOrderDelSelf", raw={},
        )

        result = self.push_order(self.push_body()).json()

        self.assertEqual(result, {"result": "success", "msg": "接收成功"})
        client.delete_order.assert_called_once_with(push.serial_no)
        client.cancel_order.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assert_one_refund_task(order, reason_fragment="没有接受这次删除")

    def test_ambiguous_readback_requires_review_and_does_not_retry_delete(self):
        order = self.make_order(status=OrderStatus.WAITING_GRAB)
        push = self.make_platform_push(order)
        client = self.use_platform_client()
        client.order_detail.side_effect = [
            {"Status": 11, "CancelStatus": 10},
            DailiantongError(
                code=-4, message="订单已被他人接手或已删除", action="LevelOrderDetail", raw={},
            ),
        ]

        for status in ("23", "24"):
            result = self.push_order(self.push_body(order_status=status)).json()
            self.assertEqual(result, {"result": "success", "msg": "接收成功"})

        client.delete_order.assert_called_once_with(push.serial_no)
        client.cancel_order.assert_not_called()
        order.refresh_from_db()
        push.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assert_one_refund_task(order, reason_fragment="没能确认")

    def test_older_accepted_push_cannot_be_ignored_after_repush(self):
        order = self.make_order(status=OrderStatus.WAITING_GRAB)
        old_push = self.make_platform_push(
            order, remote_status=16, remote_cancel_status=11, cancel_requested_at=timezone.now(),
        )
        self.make_platform_push(order, serial_no="20000000000000000005", replaces_push=old_push)
        client = self.use_platform_client()

        result = self.push_order(self.push_body()).json()

        self.assertEqual(result, {"result": "success", "msg": "接收成功"})
        self.assertEqual(client.mock_calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assert_one_refund_task(order, reason_fragment="多条仍需核对")

    def test_locally_closed_push_with_unconfirmed_cancellation_blocks_auto_refund(self):
        order = self.make_order(status=OrderStatus.WAITING_GRAB)
        older = self.make_platform_push(
            order, state=DailiantongPush.State.CLOSED, remote_status=12,
            remote_cancel_status=10, cancel_requested_at=timezone.now(), is_active=False,
        )
        self.make_platform_push(order, serial_no="20000000000000000005", replaces_push=older)
        client = self.use_platform_client()
        client.order_detail.side_effect = [{"Status": 11, "CancelStatus": 10}, {}]

        result = self.push_order(self.push_body()).json()

        self.assertEqual(result, {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(client.mock_calls, [])
        self.assert_one_refund_task(order, reason_fragment="多条仍需核对")

    def test_already_settled_platform_order_routes_to_manual(self):
        order = self.make_order(status=OrderStatus.COMPLETED)
        self.make_platform_push(
            order, state=DailiantongPush.State.CLOSED, remote_status=17, is_active=False,
        )
        client = self.use_platform_client()

        result = self.push_order(self.push_body()).json()

        self.assertEqual(result, {"result": "success", "msg": "接收成功"})
        self.assertEqual(client.mock_calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assert_one_refund_task(order, reason_fragment="代练通订单已结算")

    def test_local_refund_denial_preserves_verified_withdrawal_and_routes_to_manual(self):
        order = self.make_order(status=OrderStatus.WAITING_GRAB)
        push = self.make_platform_push(order)
        client = self.use_platform_client()
        client.order_detail.side_effect = [{"Status": 11, "CancelStatus": 10}, {}]

        with mock.patch.object(xgj_push, "apply_transition", side_effect=IllegalTransition("状态已锁定")):
            first = self.push_order(self.push_body()).json()
        retry = self.push_order(self.push_body(order_status="24")).json()

        self.assertEqual(first, {"result": "success", "msg": "接收成功"})
        self.assertEqual(retry, first)
        client.delete_order.assert_called_once_with(push.serial_no)
        push.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assert_one_refund_task(order, reason_fragment="代练通已撤回，团团通退单受阻")


class MultipleMatchesTests(_PushFixture):
    def test_every_order_with_that_source_number_is_refunded(self):
        first = self.make_order(source_order_no=SOURCE_NO)
        second = self.make_order(source_order_no=SOURCE_NO)
        # 哨兵：两张单确实都建出来了（否则「都退了」可能只是空集上的断言）。
        self.assertEqual(
            Order.objects.filter(source_order_no=SOURCE_NO).count(), 2
        )
        response = self.push_order(self.push_body(order_status="23"))
        self.assertEqual(response.json()["result"], "success")

        for order in (first, second):
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.REFUNDED)


# ── §8-4·附 隐藏单不进取单口（M-01）────────────────

#: 合成「撞号」单号：形状仿**上家鸡蛋铺内部单号**（`upstream_hall_shadow`
#: 派生隐藏单时写进 `source_order_no` 的那族值；⛔ 合成值，不是任何真实号码）。
#: 闲管家推送携带的是**闲管家单号**——两个 ID 空间本不相干，本组用例造的正是
#: 「真撞号」这一档（修复前 23/24 会把隐藏单翻成 `REFUNDED`、22 会记一笔假收款）。
COLLIDE_NO = "33160000000000009977"


class HiddenOrderExclusionTests(_PushFixture):
    """推送取单口（`_matching_orders`，22 与 23/24 两支共用）**不匹配隐藏单**。

    ⭐ 隐藏单按**真派生路径**造（⛔ 不手搓 `hall_card` / `upstream_hall_card` 列）：
    peer 镜像卡走 `hall_shadow.ensure_for_card`、上家镜像卡走
    `upstream_hall_shadow.ensure_for_card`——两族各一张，再配一张**同号真单**做对照
    （证明排除没有过度：真单照常退 / 照常记收款）。
    """

    def setUp(self):
        super().setUp()
        #: 真派生·peer 卡族：一张 listed 镜像卡 ⇒ 隐藏单（`source_order_no` = 卡单号）。
        from tests.hall_shadow_helpers import make_mirror

        self.shadow_user = get_user_model().objects.create_user(
            username="push-shadow-fixture", password="pw-shadow-fixture",
        )
        peer_card = make_mirror(
            game=self.game, bound_by=self.shadow_user, no=COLLIDE_NO,
        )
        self.peer_shadow = hall_shadow.ensure_for_card(peer_card)
        # 派生哨兵：卡真的派生出了隐藏单、且单号就是撞号那个（⛔ 不然下面的断言是空集）。
        self.assertIsNotNone(self.peer_shadow)
        self.assertEqual(self.peer_shadow.source_order_no, COLLIDE_NO)

        #: 真派生·上家镜像卡族：水位之后的 listed 卡 ⇒ 隐藏单（`source_order_no` =
        #: 上家内部单号）。价闸要展示价 > 0 ⇒ rec_fee 100 × 85% ＝ 85。
        up_binding = JidanpuUpstreamBinding.objects.create(
            user=self.shadow_user,
            jidanpu_base_url="https://jidanpu-up-push.test.invalid",
            account="acc-push-shadow", password="pw-shadow-fixture",
            enabled=True, hall_price_percent=85,
        )
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://jidanpu-up-push.test.invalid",
            upstream_order_id=COLLIDE_NO,
            owner_binding=up_binding,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            remote_rec_fee=Decimal("100.00"),
            project_text="合成项目-推送撞号",
            remote_remark="合成备注-推送撞号",
            remote_role_name="合成角色",
            first_seen_at=upstream_hall_shadow.UPSTREAM_THROW_SINCE + timedelta(minutes=1),
        )
        self.upstream_shadow = upstream_hall_shadow.ensure_for_card(mirror)
        self.assertIsNotNone(self.upstream_shadow)
        self.assertEqual(self.upstream_shadow.source_order_no, COLLIDE_NO)

        #: 撞号对照面：一张**真单**的来源单号恰好同号（＝闲管家单号撞上内部号的那笔生意）。
        self.real_order = self.make_order(source_order_no=COLLIDE_NO)

    def _assert_shadows_untouched(self):
        """两族隐藏单：状态 / 收款状态一个字不改、操作日志一行没有。"""
        for shadow in (self.peer_shadow, self.upstream_shadow):
            shadow.refresh_from_db()
            self.assertEqual(shadow.status, OrderStatus.WAITING_GRAB)
            self.assertEqual(shadow.payment_status, PaymentStatus.UNSETTLED)
            # 判据打在数据库行上（E-0009）：不匹配 = 连「白跑一趟」的日志都不许有。
            self.assertEqual(self.actions_of(shadow.order_no), [])

    def test_a_refund_push_with_a_colliding_number_refunds_the_real_order_only(self):
        """23 退款推送撞上隐藏单的内部号 ⇒ 真单照退、两族隐藏单原样（修复前它们会被翻成退单）。"""
        response = self.push_order(self.push_body(order_no=COLLIDE_NO, order_status="23"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        # 正对照：真单照常退（⭐ 排除没有过度——同号真单仍要进命中集合）。
        self.real_order.refresh_from_db()
        self.assertEqual(self.real_order.status, OrderStatus.REFUNDED)
        self.assertEqual(
            self.actions_of(self.real_order.order_no),
            ["order.transition", "order.xgj_push_refund"],
        )
        self._assert_shadows_untouched()

    def test_a_receipt_push_with_a_colliding_number_settles_the_real_order_only(self):
        """22 收款推送撞上隐藏单的内部号 ⇒ 真单记收款、两族隐藏单不记假收款。"""
        response = self.push_order(self.push_body(order_no=COLLIDE_NO, order_status="22"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        # 正对照：真单的收款状态被推成「已结款」。
        self.real_order.refresh_from_db()
        self.assertEqual(self.real_order.payment_status, PaymentStatus.SETTLED)
        self.assertIn("order.xgj_push_receipt", self.actions_of(self.real_order.order_no))
        # 台账行也只数到真单那一张（修复前 matched_count 会数到 3）。
        record = XgjReceiptRecord.objects.get(order_no=COLLIDE_NO)
        self.assertEqual(record.matched_count, 1)
        self._assert_shadows_untouched()


# ── §8-5 来源单号带首尾空白也能命中 ───────────────────────────────────────

class WhitespaceKeyTests(_PushFixture):
    def test_matching_ignores_surrounding_whitespace_on_both_sides(self):
        """两侧各一行：**库里那一列**带空白、以及**推送来的值**带空白（spec §D3）。

        `Order.source_order_no` 是客服录单时手填的自由文本（先例 `xgj_ship._snapshot_number`
        就是先把这一列 strip 再发给上游）⇒ 这一列的空白不能算差异。
        """
        stored_clean = self.make_order(source_order_no=SOURCE_NO)
        stored_padded = self.make_order(source_order_no=f" {SOURCE_NO}\n")
        response = self.push_order(
            self.push_body(order_no=f"  {SOURCE_NO}  ", order_status="23")
        )
        self.assertEqual(response.json()["result"], "success")
        for order in (stored_clean, stored_padded):
            order.refresh_from_db()
            self.assertEqual(order.status, OrderStatus.REFUNDED)


# ── §8-5 追加：削白字符集与 Python `str.strip()` 必须**同一套** ─────────────

class TrimCharacterSetTests(_PushFixture):
    """F1：库侧 `BTRIM` 的字符集（`xgj_push._TRIM_CHARS`）与推送侧 `str.strip()` 必须同集合。

    ⚠️ **两侧归一化必须同一套**（`AGENTS.md` §5-G 的同族纪律，E-0083）：推送来的值由
    `(order_no or "").strip()` 削（Python 全集），而库里那一列由 PostgreSQL `BTRIM(列, 字符集)` 削。
    只要 `_TRIM_CHARS` 比 `str.strip()` **小**，库里那一列带全角空格（中文界面复制粘贴很常见）就
    **匹配不到**——接口照回 `success`、状态不变、连一条 warning 都没有（**静默少退单**）。

    ⚠️ 机械判据的期望值是**当场算出来的**（`chr(cp).isspace()`），⛔ 不是抄一份常量：
    抄常量就成了自指断言（E-0024）——常量本身被改小时它照样绿。
    """

    def test_the_trim_character_set_is_exactly_python_str_strip(self):
        """`set(_TRIM_CHARS)` 必须**恰好**等于 `str.strip()` 认的那一组码位（含全角空格）。"""
        self.assertEqual(
            set(xgj_push._TRIM_CHARS),
            {chr(cp) for cp in range(0x11000) if chr(cp).isspace()},
        )

    def test_a_full_width_space_around_the_stored_value_still_matches(self):
        """库里那一列的**首尾**带 `\\u3000`（全角空格）⇒ 推送仍要命中、状态真的变 `REFUNDED`。

        ⚠️ 这一条是**行为**判据（不是集合判据）：`\u3000` 落在 `str.strip()` 里、但**不在**
        改之前的 `_TRIM_CHARS`（那 6 个 ASCII 空白）里 ⇒ 改之前这一笔会**静默不命中**。
        """
        order = self.make_order(source_order_no=f"\u3000{SOURCE_NO}\u3000")
        # 自证：库里那一列确实带的是全角空格（否则「没命中」可能只是夹具写错了）。
        self.assertTrue(order.source_order_no.startswith("\u3000"))
        response = self.push_order(self.push_body(order_status="23"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)


# ── §8-6 幂等 ─────────────────────────────────────────────────────────────

class IdempotenceTests(_PushFixture):
    def test_an_already_refunded_order_is_skipped_without_new_logs(self):
        order = self.make_order(status=OrderStatus.REFUNDED)
        logs_before = OperationLog.objects.count()
        response = self.push_order(self.push_body(order_status="23"))
        self.assertEqual(response.json()["result"], "success")
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(OperationLog.objects.count(), logs_before)


# ── §8-7 已结账 ⇒ 单独那条日志 + 状态不变 ─────────────────────────────────

class SettledTests(_PushFixture):
    def test_a_settled_order_gets_its_own_log_and_keeps_its_status(self):
        order = self.make_order(status=OrderStatus.IN_PROGRESS)
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, settle_status=SettleStatus.SETTLED,
        )
        response = self.push_order(self.push_body(order_status="23"))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)  # 状态一个字不改
        actions = self.actions_of(order.order_no)
        self.assertIn("order.xgj_push_refund_settled", actions)
        self.assertNotIn("order.xgj_push_refund", actions)
        self.assertNotIn("order.transition", actions)

    def test_the_settled_log_names_the_reason_and_the_next_step(self):
        order = self.make_order(status=OrderStatus.IN_PROGRESS)
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, settle_status=SettleStatus.SETTLED,
        )
        self.push_order(self.push_body(order_status="24"))
        settled_log = OperationLog.objects.get(
            order_no=order.order_no, action="order.xgj_push_refund_settled"
        )
        self.assertIn("关闭", settled_log.summary)
        self.assertIn("绩效已结账", settled_log.summary)


# ── §8-7 追加：`IllegalTransition` ⇒ 跳过（F7）──────────────────────────────

class IllegalTransitionTests(_PushFixture):
    """§D7「其余改不动的情形 ⇒ 跳过」这一档今天**零用例**（F7 补上）。

    ⚠️ **可达性已改准**：今天**每个**来源态都有一条入 `REFUNDED` 的边（实测：`TRANSITIONS` 里
    只差 `REFUNDED` 自己，而它在 `_refund_one` 之前就被幂等分支 `continue` 掉了）
    ⇒ spec §D7 里「**边不存在**」那一支**今天不可达**，真正能触发这一支的是
    「**陈旧状态 / 并发复读不一致**」——`IllegalTransition` 自 P3c 起同时表示这两种语义
    （见 `order_status.py` 的类 docstring）。⛔ 只追加改准，不改 spec 原句。

    ⚠️ 打桩打在**被查找的那个模块**上（E-0029）：`xgj_push` 里 `apply_transition` 是
    `from … import` 进来的**模块级名字** ⇒ 桩打在 `apps.business.xgj_push.apply_transition`。
    并且照纪律先跑一条「不接桩时必须真的退单」的对照，再自证桩被调到了。
    """

    def test_an_illegal_transition_is_skipped_without_any_write(self):
        # ① 对照：不接桩时同一条路**必须**真的把单退掉（否则下面那条断言在「端点压根不工作」时也会绿）。
        control_order = self.make_order(source_order_no=SOURCE_NO)
        control = self.push_order(self.push_body(order_no=SOURCE_NO, order_status="23"))
        self.assertEqual(control.json(), {"result": "success", "msg": "接收成功"})
        control_order.refresh_from_db()
        self.assertEqual(control_order.status, OrderStatus.REFUNDED)

        # ② 被测：把唯一写入口打桩成「这条迁移现在不合法」（陈旧状态 / 并发复读不一致的语义）。
        order = self.make_order(source_order_no=SOURCE_NO_OTHER)
        logs_before = OperationLog.objects.count()
        body = self.push_body(order_no=SOURCE_NO_OTHER, order_status="23")
        with mock.patch.object(
            xgj_push, "apply_transition", side_effect=IllegalTransition("陈旧状态")
        ) as stub:
            response = self.push_order(body)
        self.assertTrue(stub.called, "桩没被调到——这条用例没测到 D7 的跳过分支")
        self.assertEqual(stub.call_count, 1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)  # 状态不变
        self.assertEqual(OperationLog.objects.count(), logs_before)  # 零写入（E-0009：打在数据库行上）


# ── §8-8 找不到单 ⇒ success 且零写入 ──────────────────────────────────────

class NoMatchTests(_PushFixture):
    def test_an_unknown_source_number_changes_nothing(self):
        logs_before = OperationLog.objects.count()
        orders_before = Order.objects.count()
        response = self.push_order(
            self.push_body(order_no=SOURCE_NO_OTHER, order_status="23")
        )
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        self.assertEqual(OperationLog.objects.count(), logs_before)
        self.assertEqual(Order.objects.count(), orders_before)

    def test_an_empty_source_number_refunds_nothing(self):
        """F4：推送来的 `order_no` 为空 / 纯空白 ⇒ 直接 ack，⛔ 一张「来源单号为空」的单都不许退。

        ⚠️ 空键会命中**所有**「`source_order_no` 为空 / 纯空白」的行（`BTRIM` 之后都是 `''`）
        ⇒ 少了这道闸门，一条 `{"order_no":"","order_status":"23"}` 的推送会把它们**一次性全部
        退单**（**批量错退钱**，还会连带清绩效）。spec §5 原本没写这一支：实现**有意**加了一道
        承重闸门，登记在 spec 文末追加块与实现报告里。
        """
        first = self.make_order(source_order_no="")
        second = self.make_order(source_order_no="")
        # 哨兵：两张「空来源单号」的单确实建出来了（否则「一张没退」可能只是空集上的断言）。
        self.assertEqual(Order.objects.filter(source_order_no="").count(), 2)
        logs_before = OperationLog.objects.count()

        for pushed in ("", "   "):
            with self.subTest(order_no=pushed):
                response = self.push_order(
                    self.push_body(order_no=pushed, order_status="23")
                )
                self.assertEqual(
                    response.json(), {"result": "success", "msg": "接收成功"}
                )
                for order in (first, second):
                    order.refresh_from_db()
                    self.assertEqual(order.status, OrderStatus.UNASSIGNED)  # 状态一个都不许变
                self.assertEqual(OperationLog.objects.count(), logs_before)  # 零新增


# ── §8-9 商品推送 ⇒ success 且零写入 ──────────────────────────────────────

class ProductPushTests(_PushFixture):
    def test_a_product_push_is_acknowledged_without_any_write(self):
        """body 故意**不是 JSON**：本端点只验签 + ack，⛔ 不解析（spec §D9）。"""
        body = "商品推送的原文（不解析）".encode("utf-8")
        logs_before = OperationLog.objects.count()
        orders_before = Order.objects.count()
        response = self.push_product(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})
        self.assertEqual(OperationLog.objects.count(), logs_before)
        self.assertEqual(Order.objects.count(), orders_before)

    def test_a_product_push_with_a_bad_signature_is_rejected(self):
        body = b'{"product_id":"1"}'
        response = self.push_product(body, sign="f" * 32)
        self.assertEqual(response.json(), {"result": "fail", "msg": "签名失败"})

    def test_a_product_push_with_missing_params_is_rejected(self):
        response = self.post_body(PRODUCT_URL, b"{}", query=_query(omit="appid"))
        self.assertEqual(response.json(), {"result": "fail", "msg": "缺少签名参数"})


# ── §8-10 跨租户隔离 ─────────────────────────────────────────────────────

DOMAIN_B = "xgj-push-iso-b.test.local"
SCHEMA_B = "xgj_push_iso_b"
#: 租户 B 的来源单号：**租户内唯一、且与 A 取值不同**（判据用它，⛔ 不用主键）。
SOURCE_NO_B = "22000000000000000099"


class XgjPushIsolationTests(TenantTestCase):
    """A 租户的推送**碰不到** B 租户的单（`AGENTS.md` §4 的红线）。

    ⚠️ 第二个租户在 `setUpClass` 里建（在一个用例内部建 schema 会与外层事务相冲）。
    ⚠️ 跨域名的请求会在响应结束后把连接留在那个租户的 schema 上（`django_tenants`
    的中间件不还原）⇒ 每次跨租户请求都用 `try/finally` 把连接钉回 A。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="闲管家推送隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.game_a = Game.objects.create(name="A测试游戏")
        self.source_a = OrderSource.objects.create(name="A测试来源")
        XgjShopBinding.objects.create(
            shop_name="A测试应用", app_key=APP_KEY, app_secret=APP_SECRET,
        )
        self.order_a = Order.objects.create(
            game=self.game_a, order_no=_order_no(), status=OrderStatus.UNASSIGNED,
            order_source=self.source_a, source_order_no=SOURCE_NO,
        )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="B测试游戏")
            source_b = OrderSource.objects.create(name="B测试来源")
            XgjShopBinding.objects.create(
                shop_name="B测试应用", app_key=APP_KEY, app_secret=APP_SECRET,
            )
            self.order_b = Order.objects.create(
                game=game_b, order_no=_order_no(), status=OrderStatus.UNASSIGNED,
                order_source=source_b, source_order_no=SOURCE_NO_B,
            )

    def _push_from(self, domain: str, body: bytes, *, sign=None):
        """从 `domain` 打一条推送；连接用 `finally` 钉回租户 A。"""
        try:
            return self.client.post(
                f"{ORDER_URL}?{_query(sign=sign_of(body) if sign is None else sign)}", body,
                content_type="application/json", SERVER_NAME=domain,
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_a_push_in_tenant_a_cannot_touch_a_tenant_b_order(self):
        # A 里没有 SOURCE_NO_B 这个值 ⇒ 这一笔在 A 里找不到单。
        response = self._push_from(DOMAIN_A, self.push_body_for(SOURCE_NO_B))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.status, OrderStatus.UNASSIGNED)
        with tenant_context(self.tenant_b):
            # 判据用**租户内唯一、且两租户取值不同**的字段（⛔ 不用主键）。
            b_order = Order.objects.get(source_order_no=SOURCE_NO_B)
            self.assertEqual(b_order.status, OrderStatus.UNASSIGNED)
            self.assertFalse(
                OperationLog.objects.filter(order_no=b_order.order_no).exists()
            )

    def test_a_push_in_tenant_b_cannot_touch_a_tenant_a_order(self):
        response = self._push_from(DOMAIN_B, self.push_body_for(SOURCE_NO))
        self.assertEqual(response.json(), {"result": "success", "msg": "接收成功"})

        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.status, OrderStatus.UNASSIGNED)
        self.assertFalse(
            OperationLog.objects.filter(order_no=self.order_a.order_no).exists()
        )
        with tenant_context(self.tenant_b):
            b_order = Order.objects.get(source_order_no=SOURCE_NO_B)
            self.assertEqual(b_order.status, OrderStatus.UNASSIGNED)

    def test_each_tenant_refunds_only_its_own_order(self):
        """正向对照：两边各推**自己的**号 ⇒ 各自退单，另一边不动。

        ⚠️ 少了这条，上面两条否定断言在「本端点压根不工作」时也会绿。
        """
        response_a = self._push_from(DOMAIN_A, self.push_body_for(SOURCE_NO))
        response_b = self._push_from(DOMAIN_B, self.push_body_for(SOURCE_NO_B))
        self.assertEqual(response_a.json()["result"], "success")
        self.assertEqual(response_b.json()["result"], "success")

        self.order_a.refresh_from_db()
        self.assertEqual(self.order_a.status, OrderStatus.REFUNDED)
        with tenant_context(self.tenant_b):
            b_order = Order.objects.get(source_order_no=SOURCE_NO_B)
            self.assertEqual(b_order.status, OrderStatus.REFUNDED)

    def test_a_signature_cannot_create_a_tenant_b_refund_task(self):
        secret_b = "SK-PUSH-TEST-B"
        with tenant_context(self.tenant_b):
            binding = XgjShopBinding.objects.get(app_key=APP_KEY)
            binding.app_secret = secret_b
            binding.save(update_fields=["app_secret"])
            DailiantongPush.objects.create(
                order=self.order_b, state=DailiantongPush.State.PENDING, price=Decimal("20.00"),
            )

        body = self.push_body_for(SOURCE_NO_B)
        unauthorized = self._push_from(DOMAIN_B, body)
        self.assertEqual(unauthorized.json(), {"result": "fail", "msg": "签名失败"})
        with tenant_context(self.tenant_b):
            self.assertFalse(Approval.objects.filter(original_order__source_order_no=SOURCE_NO_B).exists())

        authorized = self._push_from(DOMAIN_B, body, sign=sign_of(body, app_secret=secret_b))
        self.assertEqual(authorized.json(), {"result": "success", "msg": "接收成功"})
        self.assertFalse(Approval.objects.exists())
        with tenant_context(self.tenant_b):
            task = Approval.objects.get(original_order__source_order_no=SOURCE_NO_B)
            self.assertEqual(task.kind, "refund")
            self.assertEqual(task.status, "pending")

    def push_body_for(self, source_order_no: str) -> bytes:
        return json.dumps(
            {"order_no": source_order_no, "order_status": "23"},
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")


# ── §8-11 未预期异常 ⇒ fail + 不是 500 ────────────────────────────────────

class UnexpectedErrorTests(_PushFixture):
    def test_the_stub_is_planted_on_the_module_that_looks_the_name_up(self):
        """桩打在**被查找的那个模块**上（E-0029）——先证「视图查的就是这个名字」。"""
        self.assertIs(xgj_push_views.handle_order_push, xgj_push.handle_order_push)

    def test_an_unexpected_error_is_reported_as_fail_and_not_500(self):
        body = self.push_body(order_status="23")
        self.make_order()  # 有单可改 ⇒ 不接桩时这一笔会真的成功

        # 对照（桩接没接上的判据）：不打桩时同一条请求**必须**是 success。
        control = self.push_order(body)
        self.assertEqual(control.json(), {"result": "success", "msg": "接收成功"})

        with mock.patch.object(
            xgj_push_views, "logger"
        ) as fake_logger, mock.patch.object(
            xgj_push_views, "handle_order_push", side_effect=RuntimeError("boom")
        ) as stub:
            response = self.push_order(body)
        self.assertTrue(stub.called, "桩没被调到——这条用例没测到视图的异常分支")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "fail", "msg": "处理失败"})
        # 辅助判据（spec §4 第 4 行的「同时 logger.exception 留全栈」）；
        # ⚠️ 承重的是上面两行（200 + fail），⛔ 不用 assertLogs 断 logger（E-0009）。
        fake_logger.exception.assert_called_once()


# ── §8-12 非信封响应形状 ─────────────────────────────────────────────────

class ResponseShapeTests(_PushFixture):
    def test_the_response_has_exactly_the_two_vendor_keys(self):
        response = self.push_order(self.push_body(order_status="12"))
        body = response.json()
        self.assertEqual(set(body), {"result", "msg"})
        # 反向：本仓统一信封的键一个都不许在——「含这两个键」放不进信封的回归。
        self.assertNotIn("code", body)
        self.assertNotIn("data", body)

    def test_the_failure_response_has_the_same_shape(self):
        response = self.post_body(
            ORDER_URL, self.push_body(order_status="12"), query=_query(omit="sign")
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"result", "msg"})
        self.assertEqual(body["result"], "fail")
