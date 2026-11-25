"""上家鸡蛋铺线（客服个人绑定）：**我方 → 上家的两个写动作**（计划 Task 8；spec §6.1 / §6.2）。

本文件检七件事（每条都**钉值**，⛔ 不是「有就行」）：

① **两个写端点的载荷逐字**（spec §2.3 的**实测**）：`RecOrderByInternal` 只带 `orderId`、
   `SetOrderSta` 只带 `orderId` + `sta=待验收`（⛔ 不带 `changeSoureType` / `remark` /
   `isSyncTaoBao`——那是管理端退单端点的形状）；
② **写端点判据**（spec §11-9 / E-0087）：失败信封（`code=0` + 非空 `msg` + 空 `data`）**必须**
   被判失败；`code=2`（提交了 2 笔）**必须**判成功——判据是 `code > 0`，⛔ 不是「等于 1」；
③ ⭐ **A16「接单不额外推状态」**：大厅接单成功后，桩的**调用集合**里**只有**
   `RecOrderByInternal`（⛔ 没有任何 `SetOrderSta` / 状态写）——依据：**接单动作本身**就让上家
   那张单变「代练中」（spec §2.3 的实测 / §16.2）；
④ ⭐ **A5「传图失败 ⇒ 零提交」**：上家该单无图 + 传图那一步失败 ⇒ **一次 `SetOrderSta` 都不发**
   （判据同样是桩的调用集合）；
⑤ **只有我方订单 = 「已完成」才提交**（spec §6.2）：中间态一个上游调用都不发；
⑥ **G4 的重复提交兜底**：**先回读**——上家已是「待验收 / 已完成」⇒ 幂等短路（⛔ 不再提交），
   提交后**回读不确认** ⇒ 不算成功（`failed` + 退避）；
⑦ **台账 / `last_error`**：两个动作都进台账（`upstream.hall_claim` / `upstream.complete`，
   `target_ref` 带**绑定归属**），失败原因**不带凭据**并进绑定 `last_error`；
   ⭐ **A13 / §11-24**：转正出的单 `sales_user` = **owner 绑定**的客服（⛔ 不是接单打手、
   ⛔ 不是同地址第二份绑定的客服），且 `SALES` 绩效行的 `user` 就是它。

⚠️ 判据打**数据库行**与**桩的调用集合**（`AGENTS.md` §5-G），⛔ 不打 logger。
⚠️ **零真实网络**：`_StubClient` 打在 SDK 边界、`_StubHttp` 打在上传页那一跳、OSS 那一跳注入替身。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-up-*`、
    密码 `pw-canary-consume`、单号 `20260928000000000901` 起（全零段，一眼是合成值）。

⭐ **Task 8 修复轮**：F1 事件归属 = 链路 / 提成归属（`…rotated_row_owner…` /
`…owner_binding_is_gone…`）+ **F1 收口·停用点名**（`…disabled_service…`；对照 `…active_service…`）、
F2 只吞「两边都没价」那一档（`…different_amount_error…`）、
F3 烧完额度的行遇**显式动作**重新发额度（`…explicit_re_queue…` / `…inside_its_budget…` /
   ⭐ M-1 收口：`…explicit_hall_re_queue…` 直调入口造现场 + 可达性钉子 `…cannot_re_enter…`）、
F5 `COMPLETE_LINK_MISSING_REASON` 的两支（`test_complete_does_nothing_…`）、
F6 认不出「已被接走」时人话点明需人工核对（`…unclear_refusal…` ×2）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_common, jidanpu_upstream
from apps.business.claim import ClaimError
from apps.business.models import (
    EmpType,
    Game,
    HallExitSnapshot,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderSource,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.business.order_no import generate_order_no
from apps.business.performance_amount import AmountError
from apps.business.recycle import soft_delete_order
from apps.core.error_codes import ErrorCode
from apps.core.storage import get_storage
from jidanpu.errors import TransportError

#: 端点路径**逐字**抄 spec §2.1 / §2.3（⛔ 不从模块常量派生）。
REC_ORDER_PATH = "/OrderAjax/RecOrderByInternal"
SET_STATUS_PATH = "/OrderAjax/SetOrderSta"
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"
VIEW_PIC_PATH = "/OrderAjax/ViewPic"
UPLOAD_PIC_PATH = "/OrderAjax/UploadOrderPic"
UPLOAD_PAGE_PATH = "/Order/UploadPic"

BASE_URL = "https://jidanpu-upstream-consume.test.invalid"
ACCOUNT = "kefu-up-owner"
ACCOUNT_OTHER = "kefu-up-other"
PASSWORD = "pw-canary-consume"

#: 合成上家单号（全零段，一眼是夹具）。
REMOTE_ID = "20260928000000000901"

#: 提交完成的 `sta`**逐字**（spec §2.3 实测：中文标签）。
COMPLETION_LABEL = "待验收"

#: 「已被接走」的合成上游文案（**推断**文本，⛔ 不是实测原文；实现按 token 保守分类）。
TAKEN_MESSAGE = "该订单已被接走"

#: 合成凭据 canary（spec §11-18：**搜字符串**，不是「看起来没有」）。
CANARY_PASSWORD = PASSWORD


def _upload_page_html(*, order_id=REMOTE_ID, sid="10001", game_acc_id="7", task_id="9"):
    """合成上传页（隐藏域齐全；spec §2.4 的形状）。"""
    parts = ["<html><body><form method=\"post\">"]
    if order_id is not None:
        parts.append(f'<input type="hidden" name="orderId" value="{order_id}" />')
    if game_acc_id is not None:
        parts.append(f"<input name='gameAccId' type='hidden' value='{game_acc_id}'>")
    if task_id is not None:
        parts.append(f'<input type="hidden" value="{task_id}" name="taskId"/>')
    if sid is not None:
        parts.append(f"<input value='{sid}' name='sid' type='hidden'>")
    parts.append("</form></body></html>")
    return "".join(parts)


class _StubClient:
    """SDK 边界替身：按端点回包 / 抛错，并记录**每一次调用**（⛔ 不接真网络）。

    - `claim_*`：`RecOrderByInternal` 的回包；
    - `submit_*`：`SetOrderSta` 的回包；
    - `remote_statuses`：`GetMyOrderList` 逐次回的状态（读一份少一份，最后一份会重复用）；
    - `pic_envelope`：`ViewPic` 的**原始业务壳**（判据只看 `msg.data`）。
    """

    def __init__(self, *, claim_code=1, claim_msg="接单成功，请到「我的订单」里查看订单资料",
                 claim_data="", submit_code=1, submit_msg="已成功提交1笔订单",
                 submit_data=None, remote_statuses=("代练中",), pic_envelope=None):
        self.claim_code = claim_code
        self.claim_msg = claim_msg
        self.claim_data = claim_data
        self.submit_code = submit_code
        self.submit_msg = submit_msg
        self.submit_data = [] if submit_data is None else submit_data
        self._statuses = list(remote_statuses)
        self.pic_envelope = (
            pic_envelope
            if pic_envelope is not None
            else {"code": 0, "msg": {"title": "", "data": [{"pic": "合成图"}]}}
        )
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        if path == MY_ORDERS_PATH:
            status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
            row = {"orderId": REMOTE_ID, "orderSta": status}
            self.last_envelope = {"code": 0, "msg": "success", "data": [row], "count": 1}
            return [row]
        if path == REC_ORDER_PATH:
            self.last_envelope = {
                "code": self.claim_code, "msg": self.claim_msg, "data": self.claim_data,
            }
            return self.claim_data
        if path == SET_STATUS_PATH:
            self.last_envelope = {
                "code": self.submit_code, "msg": self.submit_msg, "data": self.submit_data,
            }
            return self.submit_data
        if path == VIEW_PIC_PATH:
            self.last_envelope = dict(self.pic_envelope)
            return self.pic_envelope.get("data", self.pic_envelope.get("msg"))
        if path == UPLOAD_PIC_PATH:
            self.last_envelope = {"code": 1, "msg": "成功保存1张图片", "data": []}
            return []
        raise AssertionError(f"未预期的端点：{method} {path}")

    #: 便于断言「调用集合」。
    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _kwargs in self.calls]


class _StubHttp:
    """HTTP 边界替身（只服务上传页那一跳）。"""

    def __init__(self, *, html=None, status=200):
        self.html = _upload_page_html() if html is None else html
        self.status = status
        self.gets: list[tuple[str, dict]] = []

    def get(self, path, **kwargs):
        self.gets.append((path, kwargs))
        return httpx.Response(self.status, text=self.html)


class _OssRecorder:
    """`oss_post` 的替身：记录调用（ODD 那一跳**不是**鸡蛋铺端点，spec §6.3）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, host, fields, key, data, content_type, *, filename="file"):
        self.calls.append((host, dict(fields), key, data, content_type))


class _ClientCtx:
    """`jidanpu.open_client(binding)` 的替身（上下文管理器里就是桩客户端）。"""

    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False


# ══ 常量与契约（⛔ 钉值，不钉常量名）════════════════════════════════════════════


class ConsumeContractTests(SimpleTestCase):
    def test_the_two_kinds_and_the_endpoints_are_the_verbatim_ones(self):
        """kind 与端点路径逐字钉住（spec §4.4 / §2.3）：⛔ 拼错一个就是一次静默失败。"""
        self.assertEqual(jidanpu_upstream.KIND_HALL_CLAIM, "upstream.hall_claim")
        self.assertEqual(jidanpu_upstream.KIND_COMPLETE, "upstream.complete")
        self.assertEqual(
            tuple(jidanpu_upstream.OUTBOX_KINDS),
            ("upstream.hall_claim", "upstream.complete"),
        )
        self.assertEqual(jidanpu_upstream.REC_ORDER_PATH, REC_ORDER_PATH)
        # ⚠️ 提交完成**复用**租户级那条线的常量（⛔ 不复制字面量）。
        self.assertEqual(jidanpu.SET_STATUS_PATH, SET_STATUS_PATH)
        self.assertEqual(jidanpu_upstream.COMPLETION_STATUS_LABEL, COMPLETION_LABEL)


# ══ 夹具基类 ═══════════════════════════════════════════════════════════════════


class _UpstreamConsumeBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        self.owner_user = User.objects.create_user(
            username=ACCOUNT, password=PASSWORD, first_name="测试客服甲",
        )
        self.other_user = User.objects.create_user(
            username=ACCOUNT_OTHER, password=PASSWORD, first_name="测试客服乙",
        )
        self.taker = User.objects.create_user(username="taker-up-a", password=PASSWORD)
        #: 同地址两份绑定 ⇒ owner = **最早**那份（A2 的地址级去重）；A13 的对照也靠它。
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.owner_user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )
        self.other_binding = JidanpuUpstreamBinding.objects.create(
            user=self.other_user, jidanpu_base_url=BASE_URL, account=ACCOUNT_OTHER,
            password=PASSWORD, enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_mirror(self, *, status=JidanpuUpstreamHallOrder.Status.LISTED,
                    claiming_by=None, rec_fee=Decimal("88.00"), owner=None):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=REMOTE_ID,
            owner_binding=owner if owner is not None else self.binding,
            status=status, game=self.game,
            project_text="合成项目", remote_remark="合成备注",
            remote_role_name="合成角色", remote_rec_fee=rec_fee,
            claiming_by=claiming_by, claiming_at=self.now if claiming_by else None,
            upstream_payload={
                "orderId": REMOTE_ID, "orderIdView": f"VIEW-{REMOTE_ID}",
                "orderContent": "合成需求", "orderRemark": "合成备注",
                "roleName": "合成角色", "gameAcc": "acc-synth", "zgameAcc": "sub-synth",
                "recFee": "88.00",
            },
            first_seen_at=self.now,
        )

    def claim_event(self, mirror):
        event = jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.taker.pk)
        self.assertIsNotNone(event)
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker,
            claiming_at=self.now,
        )
        mirror.refresh_from_db()
        return event

    def make_order(self, *, status=OrderStatus.COMPLETED):
        return Order.objects.create(
            order_no=generate_order_no(), game=self.game, status=status,
            order_type="BOOST", total_price=Decimal("88.00"),
        )

    #: ⭐ D-0367 起完成链默认带截图上传 ⇒ 走到 OSS policy 那一跳的用例用它接桩。
    EMPTY_PIC_ENVELOPE = {"code": 0, "msg": {"title": "", "data": []}}

    def stub_oss_policy(self, client):
        """给桩接上「取 OSS 签名」那一跳（形状照 `missing-picture` 用例，集中一处）。"""
        real_call = client.call

        def call(method, path, **kwargs):
            if path == jidanpu_upstream.OSS_POLICY_PATH:
                client.calls.append((method, path, dict(kwargs)))
                client.last_envelope = {
                    "code": 0, "msg": "success",
                    "data": {"accessid": "CANARY-ACCESSID", "host": "https://oss-synth.invalid",
                             "policy": "CANARY-POLICY", "signature": "CANARY-SIGNATURE",
                             "expire": 1790784240},
                }
                return client.last_envelope["data"]
            return real_call(method, path, **kwargs)

        client.call = call
        return client

    def make_link(self, order, *, binding=None, terminated=False,
                  completed_submitted_at=None):
        return JidanpuUpstreamOrderLink.objects.create(
            binding=binding if binding is not None else self.binding, order=order,
            upstream_order_id=REMOTE_ID, in_hall=False,
            completed_submitted_at=completed_submitted_at,
            terminated_at=self.now if terminated else None,
        )

    def complete_event(self, order, link):
        queued = jidanpu_upstream.queue_complete(order)
        self.assertEqual(queued, 1)
        return JidanpuSyncEvent.objects.get(kind="upstream.complete", payload__link_id=link.pk)

    def make_screenshot(self, order):
        key = f"shared-orders/{order.order_no}/shot-{uuid.uuid4().hex[:8]}.jpg"
        body = b"\xff\xd8\xff" + b"\0" * 32
        get_storage().put_bytes(key, body, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=order, img_type=OrderScreenshot.IMG_ORDER,
            object_key=key, status=OrderScreenshot.STATUS_READY,
            size_bytes=len(body), uploaded_by=self.owner_user, remark="合成备注",
        )


# ══ ①–③⑦ 大厅接单（A4 / A16 / A13）═════════════════════════════════════════════


class HallClaimConsumeTests(_UpstreamConsumeBase):
    def test_the_claim_payload_is_the_verbatim_shape_and_promotes(self):
        """⭐ spec §2.3 / §6.1：载荷**只有** `orderId`、判 `code > 0`，成功才转正成我方订单。"""
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient()
        outcome = jidanpu_upstream.consume_event(event, client=client)
        self.assertEqual(outcome, "done")
        # 载荷**逐字**：`data` 恰好只有 orderId，而且要显式收手让我们的判据可达（GC 7）。
        self.assertEqual(client.calls, [
            ("POST", REC_ORDER_PATH,
             {"data": {"orderId": REMOTE_ID}, "success_rule": "lenient"}),
        ])
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        order = Order.objects.get()
        self.assertEqual(mirror.promoted_order_id, order.pk)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(order.game_id, self.game.pk)
        # §5.2 的投影：内容来自镜像行留存的**上家原始行**。
        self.assertEqual(order.content, "合成需求")
        self.assertEqual(order.remark, "合成备注")
        self.assertEqual(order.game_role_name, "合成角色")
        self.assertEqual(order.game_account, "acc-synth")
        self.assertEqual(order.game_sub_account, "sub-synth")
        # 钱：客户价 ← 上家 `recFee`（**不受比例影响**）；
        # 发单价 = 勾兑后的价 —— 本夹具走**模型默认**百分比 100 ⇒ 原价接手（= `recFee`）。
        self.assertEqual(order.total_price, Decimal("88.00"))
        self.assertEqual(order.hall_price, Decimal("88.00"))
        # 有价 ⇒ 打手绩效按「发单价 × 提成比例」正常算（本夹具的打手没有档案 ⇒ 全额 100%）。
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.temp_amount, Decimal("88.00"))
        self.assertIsNone(main.taker_price)     # ⛔ 不用 `taker_price=0` 把这条绩效钉死
        # 链路行（in_hall=False）+ 台账：这一单往后由本线跟踪。
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.order_id, order.pk)
        self.assertEqual(link.upstream_order_id, REMOTE_ID)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertTrue(event.reason)          # 人话，⛔ 不静默
        self.assertEqual(event.target_ref, f"{self.binding.pk}|{BASE_URL}/{REMOTE_ID}")

    def test_the_promoted_order_takes_the_blended_offer_price_and_keeps_the_customer_price(self):
        """⭐ 判据 ②（**事件消费那一跳**）：转正单的发单价 = 上家价 × 绑定百分比；客户价不受影响。

        走 `consume_event`（后台轮那条路）而不是请求路径 —— 两条路的转正都进
        `promote_upstream_mirror`，本用例钉的是「**同一个算法**在这一跳也被用上」。
        失败方向：`hall_price` 写回 `None`（旧的 T18 口径）⇒ 本用例红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            hall_price_percent=50
        )
        mirror = self.make_mirror(rec_fee=Decimal("99.99"))
        event = self.claim_event(mirror)
        self.assertEqual(jidanpu_upstream.consume_event(event, client=_StubClient()), "done")
        order = Order.objects.get()
        self.assertEqual(order.total_price, Decimal("99.99"))   # ⛔ 比例不许影响客户价
        self.assertEqual(order.hall_price, Decimal("50.00"))    # 99.99 × 50% = 49.995 ⇒ 50.00
        # ⭐ 口径（业务方口径，orderIdView 计划）：转正单的来源单号 = 镜像报文里
        #    的上家**展示号**（缺才回落 `upstream_order_id`——回落档由录池路那条用例钉）。
        self.assertEqual(order.source_order_no, f"VIEW-{REMOTE_ID}")

    def test_a_promoted_order_without_an_upstream_price_takes_a_zero_offer_price(self):
        """⭐ 判据 ④（**口径修订，业务方 2026-09-29**）：上家**没给价** ⇒ 勾兑输入按 `0.00` 算
        ⇒ 转正出来的单 `hall_price = 0.00`（⛔ 不再是 `None`、「请填发单价」那套提示已下线）、
        绩效预计 `0.00`、`taker_price` 仍保持 `NULL`（⛔ 不用 `taker_price=0` 把它钉死）。

        业务原话：「如果是空的，默认按照 0 算就行了」「不需要提示」。
        失败方向：把 `upstream_hall_price` 的 `None` 分支改回 `return None` ⇒ 这里读到 `None`
        ⇒ 本用例红（大厅投影 / 指派单录单各有一条同族用例）。
        """
        mirror = self.make_mirror(rec_fee=None)
        event = self.claim_event(mirror)
        self.assertEqual(jidanpu_upstream.consume_event(event, client=_StubClient()), "done")
        order = Order.objects.get()
        self.assertEqual(order.hall_price, Decimal("0.00"))
        self.assertEqual(order.total_price, Decimal("0.00"))    # 客户价照既有口径兜 0.00
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.temp_amount, Decimal("0.00"))
        self.assertIsNone(main.taker_price)

    def test_claiming_never_pushes_a_status_to_the_upstream(self):
        """⭐ **A16 / §16.2（本任务的核心减法）**：接单那一次的调用集合里**只有** `RecOrderByInternal`。

        依据：**接单动作本身**就让上家那张单变「代练中」（spec §2.3 实测）⇒ ⛔ 不许再推一次
        `SetOrderSta`（判据 = 桩的调用集合，⛔ 不是「没报错」）。
        失败方向：在接单成功后顺手补一次 `submit_completion(...)` ⇒ 本用例红。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(client.paths, [REC_ORDER_PATH])
        self.assertNotIn(SET_STATUS_PATH, client.paths)

    def test_the_promoted_order_credits_the_hall_owner_service(self):
        """⭐ spec §11-24 / A13 / §15.5：提成归 **owner 绑定**的客服。

        同地址两份绑定、owner = 最早那份 ⇒ 转正出的单 `sales_user` = owner 的客服
        （⛔ **不是**接单的我方打手、⛔ 不是第二份绑定的客服），且 `SALES` 行的 `user` 就是它。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        jidanpu_upstream.consume_event(event, client=_StubClient())
        order = Order.objects.get()
        self.assertEqual(order.sales_user_id, self.owner_user.pk)
        self.assertNotEqual(order.sales_user_id, self.taker.pk)
        self.assertNotEqual(order.sales_user_id, self.other_user.pk)
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(sales.user_id, self.owner_user.pk)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, self.taker.pk)
        self.assertIsNotNone(main.claimed_at)

    def test_the_ordered_mirror_keeps_the_owner_binding_on_the_link(self):
        """链路挂在**owner 绑定**上（⛔ 不是第二份）：`run_outbox` 的取活键也按它。"""
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        jidanpu_upstream.consume_event(event, client=_StubClient())
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.binding_id, self.binding.pk)
        self.assertNotEqual(link.binding_id, self.other_binding.pk)

    def test_a_refused_claim_converges_the_mirror_to_gone(self):
        """spec §6.1 的 ⑥：上家说「已被接走」⇒ ⛔ 不转正 + 镜像行 gone + 人话台账。

        ⚠️ 终局 ⇒ 事件收成 `done`（⛔ 不记 `failed`——那会被反复取活重试）。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient(claim_code=0, claim_msg=TAKEN_MESSAGE, claim_data="")
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(Order.objects.count(), 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNotNone(mirror.gone_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertIn("不在大厅", event.reason)

    def test_the_production_wording_taken_or_not_in_hall_is_recognised_as_gone(self):
        """⭐ 2026-10-23 真机文案（某租户线上 90 分钟 13 次）必须判成「不在大厅」。

        上家原话：`该订单已被接或不在抢单中（上游码 0）`。它不含旧词表里的任何一条长词
        （「已被接走 / 已被接取 / 不在大厅」）⇒ 修之前这一档被当成「认不出的拒绝」：
        打手看到「暂时无法接单」、镜像行永久留在 `claiming`。
        判据：事件收成 `done`、镜像行 `gone`、我方零订单；失败方向 = 把新词删掉 ⇒ 本用例红
        （会退回 `failed` + `claiming`）。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient(
            claim_code=0,
            claim_msg="该订单已被接或不在抢单中（上游码 0）",
            claim_data="",
        )
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(Order.objects.count(), 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_an_unrecognised_refusal_stays_retryable(self):
        """认不出的上游拒绝 ⇒ **可重试**：镜像行**留在 claiming**、事件 `failed` + `attempts=1`。

        失败方向：把 `_looks_taken` 改成「一律 True」⇒ 本用例红（一张真挂在大厅的单被丢弃）。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient(claim_code=0, claim_msg="系统繁忙，请稍后重试", claim_data=[])
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "failed")
        self.assertEqual(Order.objects.count(), 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertIn("系统繁忙", event.reason)   # 上游那句 msg 接进人话（spec §9.1 末段）

    def test_a_failure_envelope_on_the_claim_is_judged_a_failure(self):
        """⭐ spec §11-9：`code=0` + 非空 `msg` + 空 `data` ⇒ **必须**判失败（⛔ 不靠 SDK 宽松解包）。"""
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient(claim_code=0, claim_msg="接单失败", claim_data=[])
        outcome = jidanpu_upstream.consume_event(event, client=client)
        self.assertEqual(outcome, "failed")
        self.assertEqual(Order.objects.count(), 0)
        event.refresh_from_db()
        self.assertNotEqual(event.status, "done")

    def test_an_already_linked_mirror_is_never_claimed_again(self):
        """brief 的幂等：「链路里已有标记」⇒ ⛔ 一次上游都不打、⛔ 不建第二张单。"""
        mirror = self.make_mirror()
        existing_order = self.make_order(status=OrderStatus.IN_PROGRESS)
        self.make_link(existing_order)
        event = self.claim_event(mirror)
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(client.calls, [])                    # ⛔ 零上游调用
        self.assertEqual(Order.objects.count(), 1)            # ⛔ 不建第二张单
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(mirror.promoted_order_id, existing_order.pk)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_consuming_the_claim_event_twice_claims_only_once(self):
        """重复消费同一条事件 ⇒ 第二次一个上游调用都没有（第二条是终态守卫 / 链路标记）。"""
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        self.assertEqual(jidanpu_upstream.consume_event(event, client=_StubClient()), "done")
        event.refresh_from_db()
        second = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=second), "done")
        self.assertEqual(second.calls, [])
        self.assertEqual(Order.objects.count(), 1)

    def test_a_claim_already_marked_claimed_promotes_without_a_second_call(self):
        """`payload["claimed"]` 是「这一次上游调用已经成功过」的断点（崩在转正之前的重试）。

        失败方向：删掉 `if not payload.get("claimed"):` 那一支 ⇒ 重试会**再接一次**，
        第二次大概率被上家以「已被接取」拒掉 ⇒ 那张单被判 gone、**白丢**。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            payload={**(event.payload or {}), "claimed": True}
        )
        event.refresh_from_db()
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(client.calls, [])            # ⛔ 不再接一次
        self.assertEqual(Order.objects.count(), 1)

    def test_acknowledged_claim_reuses_a_pool_order_imported_during_its_reservation(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        order = jidanpu_upstream.record_assigned_order(
            self.binding, mirror.upstream_payload, game=self.game, now=self.now,
        )
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        event = self.claim_event(mirror)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload={**event.payload, "claimed": True})
        client = _StubClient()

        jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        self.assertEqual(client.calls, [])
        self.assertEqual(Order.objects.count(), 1)
        order.refresh_from_db()
        event.refresh_from_db()
        mirror.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, self.taker.pk)
        self.assertEqual(mirror.promoted_order_id, order.pk)
        self.assertEqual(event.status, "done")

    def test_acknowledged_claim_does_not_take_an_order_already_assigned_locally(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        order = jidanpu_upstream.record_assigned_order(
            self.binding, mirror.upstream_payload, game=self.game, now=self.now,
        )
        Order.objects.filter(pk=order.pk).update(status=OrderStatus.IN_PROGRESS)
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(user=self.other_user)
        event = self.claim_event(mirror)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload={**event.payload, "claimed": True})
        client = _StubClient()

        jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        self.assertEqual(client.calls, [])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, self.other_user.pk)
        mirror.refresh_from_db()
        event.refresh_from_db()
        self.assertNotEqual(mirror.claiming_by_id, self.taker.pk)
        self.assertEqual(event.status, "skipped")

    def test_acknowledged_claim_preserves_a_pending_peer_assignment(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        order = jidanpu_upstream.record_assigned_order(
            self.binding, mirror.upstream_payload, game=self.game, now=self.now,
        )
        marker = {"request_id": "synthetic-direct-assignment"}
        Order.objects.filter(pk=order.pk).update(peer_direct_pending=marker)
        event = self.claim_event(mirror)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload={**event.payload, "claimed": True})
        client = _StubClient()

        jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        order.refresh_from_db()
        event.refresh_from_db()
        mirror.refresh_from_db()
        self.assertEqual(client.calls, [])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.peer_direct_pending, marker)
        self.assertIsNone(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id)
        self.assertIsNone(mirror.claiming_by_id)
        self.assertEqual(event.status, "skipped")

    def test_outbox_reads_a_receipt_saved_just_before_lease_takeover(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        order = jidanpu_upstream.record_assigned_order(
            self.binding, mirror.upstream_payload, game=self.game, now=self.now,
        )
        event = self.claim_event(mirror)
        acquire = jidanpu_common.claim_event_for_execution
        client = _StubClient()

        def acquire_after_receipt(*args, **kwargs):
            JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload={**event.payload, "claimed": True})
            return acquire(*args, **kwargs)

        with mock.patch.object(jidanpu_common, "claim_event_for_execution", side_effect=acquire_after_receipt):
            jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        self.assertEqual(client.calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, self.taker.pk)

    def test_old_refusal_cannot_remove_a_new_reservation(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        event = self.claim_event(mirror)
        client = _StubClient(claim_code=0, claim_msg="订单已被接取")
        call = client.call
        renewed_at = self.now + timedelta(seconds=4)

        def take_over_before_refusal(*args, **kwargs):
            JidanpuSyncEvent.objects.filter(pk=event.pk).update(lease_owner="new-executor")
            JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(claiming_at=renewed_at)
            return call(*args, **kwargs)

        with mock.patch.object(client, "call", side_effect=take_over_before_refusal):
            result = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        self.assertEqual(result.skipped, 1)
        mirror.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertEqual(mirror.claiming_at, renewed_at)
        self.assertEqual((event.status, event.lease_owner), ("pending", "new-executor"))

    def test_old_retryable_refusal_cannot_change_the_new_executor_event(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        event = self.claim_event(mirror)
        client = _StubClient(claim_code=0, claim_msg="暂时不能接单")
        call = client.call

        def take_over_before_refusal(*args, **kwargs):
            JidanpuSyncEvent.objects.filter(pk=event.pk).update(lease_owner="new-executor")
            return call(*args, **kwargs)

        with mock.patch.object(client, "call", side_effect=take_over_before_refusal):
            result = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        event.refresh_from_db()
        self.assertEqual((result.skipped, result.failed), (1, 0))
        self.assertEqual((event.status, event.attempts, event.lease_owner), ("pending", 0, "new-executor"))

    def test_unacknowledged_claim_with_expired_lease_can_enter_the_pool_without_resending(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        event = self.claim_event(mirror)
        expiry = self.now - timedelta(seconds=1)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            payload={**event.payload, "send_started": True},
            lease_owner="old-executor", lease_expires_at=expiry,
        )

        order = jidanpu_upstream.record_assigned_order(
            self.binding, mirror.upstream_payload, game=self.game, now=self.now,
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        client = _StubClient()
        jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        self.assertEqual(client.calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id)
        self.assertEqual(Order.objects.count(), 1)

    def test_acknowledged_claim_without_a_lease_defers_pool_import_until_recovery(self):
        mirror = self.make_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING, claiming_by=self.taker)
        event = self.claim_event(mirror)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            payload={**event.payload, "send_started": True, "claimed": True},
        )

        jidanpu_upstream.record_assigned_order(
            self.binding, mirror.upstream_payload, game=self.game, now=self.now,
        )
        self.assertFalse(Order.objects.exists())
        client = _StubClient()
        jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)

        self.assertEqual(client.calls, [])
        order = Order.objects.get()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(Performance.objects.get(order=order, emp_type=EmpType.MAIN).user_id, self.taker.pk)

    def test_a_rotated_row_owner_keeps_the_link_and_the_credit_on_the_event_binding(self):
        """⭐ **F1（Task 8 修复轮）**：**事件归属 = 链路 / 提成归属**，⛔ 不许分叉。

        可达路径（评审实测的那条）：事件按 A 排下 → A 停用、同地址 B 接管（B 的 `sync_hall`
        会把 `claiming` 行的 `owner_binding` 改写成 B）→ A 再启用后消费这条旧事件。
        修前 `promote_upstream_mirror` 按**消费时刻**的 `row.owner_binding`（= B）建链路与
        `sales_user` ⇒ 提成错误归 B，且后续 `upstream.complete` 排在 B 上、而 **B 的账号里没有
        这张单** ⇒ 一直重试到 `abandoned`、**上家那张单永远停在「代练中」**。

        判据：**上游那一跳只有一次**（事件那份绑定）+ 链路在 **A** + `sales_user` = **A 的客服**
        + 一条人话台账（⛔ 不静默）。失败方向：把 `binding=binding` 改回读 `row.owner_binding`
        ⇒ 链路 / 提成落 B、台账也不写 ⇒ 本用例红。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)                  # 事件归 A（`self.binding`）
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            owner_binding=self.other_binding               # owner 轮换成 B（同地址第二份）
        )
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(client.paths, [REC_ORDER_PATH])
        order = Order.objects.get()
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.binding_id, self.binding.pk)          # ⛔ 不是 B
        self.assertEqual(link.order_id, order.pk)
        self.assertEqual(order.sales_user_id, self.owner_user.pk)   # 提成归 A 的客服
        self.assertNotEqual(order.sales_user_id, self.other_user.pk)
        sales = Performance.objects.get(order=order, emp_type=EmpType.SALES)
        self.assertEqual(sales.user_id, self.owner_user.pk)
        # ⛔ 不静默：留一条人话台账（`direction="in"`，⛔ 不进 outbox 待办）。
        note = JidanpuSyncEvent.objects.get(
            kind=jidanpu_upstream.KIND_HALL_OWNER_MISMATCH
        )
        self.assertEqual(note.direction, "in")
        self.assertTrue(note.reason)
        self.assertIn("归属", note.reason)
        self.assertEqual(note.payload["upstream_binding_id"], self.binding.pk)
        self.assertNotIn(jidanpu_upstream.KIND_HALL_OWNER_MISMATCH,
                         jidanpu_upstream.OUTBOX_KINDS)

    def test_a_mirror_whose_owner_binding_is_gone_promotes_under_the_event_binding(self):
        """⭐ F1 同一条口径的另一半：行没有 owner（负责拉大厅那份绑定**已被删**、FK `SET_NULL`）。

        转正需要的是「这一单的上游那一跳是谁打的」，⛔ 不是行上那个会轮换 / 会被置空的指针
        ⇒ 照样按**事件那份**转正 + 一条人话台账。修前那一支是 `HALL_CLAIM_NO_OWNER_REASON`
        的 `skipped`（镜像行**永久卡在 `claiming`**、没有任何恢复入口）。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(owner_binding=None)
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(client.paths, [REC_ORDER_PATH])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(
            JidanpuUpstreamOrderLink.objects.get().binding_id, self.binding.pk
        )
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind=jidanpu_upstream.KIND_HALL_OWNER_MISMATCH
            ).exists()
        )
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)

    def test_a_disabled_service_on_the_event_binding_is_named_in_the_mismatch_reason(self):
        """⭐ **F1 收口（§15.5 第 3 条 / §13.1-R24，业务方 2026-09-29 裁定）**：停用要点名。

        归属仍取**事件那份绑定**（⛔ 不改派、⛔ 不回 `skipped`），但那份绑定的客服已停用
        （`user.is_active=False`）时，台账人话必须**点名**「客服已停用」——否则运维看不出钱会
        落在一个停用账号名下、不知道该找谁对账。失败方向：把新加的停用分支改成恒假（或删掉）
        ⇒ 本用例红。
        """
        self.owner_user.is_active = False
        self.owner_user.save(update_fields=["is_active"])
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            owner_binding=self.other_binding            # owner 轮换成 B ⇒ 归属不一致
        )
        self.assertEqual(
            jidanpu_upstream.consume_event(event, client=_StubClient()), "done"
        )
        # 归属**不变**：仍按事件那份（A）的客服记，⛔ 不因为客服停用就改派 / 跳过。
        self.assertEqual(Order.objects.get().sales_user_id, self.owner_user.pk)
        note = JidanpuSyncEvent.objects.get(
            kind=jidanpu_upstream.KIND_HALL_OWNER_MISMATCH
        )
        self.assertIn("客服已停用", note.reason)        # 钉值：点名那位客服已停用
        self.assertIn("停用账号", note.reason)          # 钉值：钱会落在停用账号名下

    def test_an_active_service_on_the_event_binding_keeps_the_mismatch_reason_plain(self):
        """**对照**（⛔ 不能省）：客服在职（`is_active=True`）+ 归属不一致 ⇒ 人话**不含**停用点名。

        没有这一条，上一条的字面断言可能因为「任何归属不一致都拼这句」而**恒真**（等于没测）。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            owner_binding=self.other_binding
        )
        self.assertEqual(
            jidanpu_upstream.consume_event(event, client=_StubClient()), "done"
        )
        self.assertTrue(self.owner_user.is_active)      # 前提交互可测：客服确实在职
        self.assertEqual(Order.objects.get().sales_user_id, self.owner_user.pk)
        note = JidanpuSyncEvent.objects.get(
            kind=jidanpu_upstream.KIND_HALL_OWNER_MISMATCH
        )
        self.assertNotIn("客服已停用", note.reason)
        self.assertNotIn("停用账号", note.reason)

    def test_a_different_amount_error_on_promotion_is_not_swallowed(self):
        """⭐ **F2（Task 8 修复轮）**：转正只吞「两边都没价」那一档（`MissingPriceError`）。

        别的 `AmountError`（以后有人往 `set_taker` 的重算链上加别的金额校验、或数据库里那条
        绩效的价坏了）必须**响亮上抛**，⛔ 不因为是「转正」就被吞掉。失败方向：把
        `except MissingPriceError` 放宽成 `except Exception`（吞掉一切；即评审那条
        `if order.hall_price is not None` → `if False:` 的等价变异）⇒ 本用例红。
        """
        mirror = self.make_mirror()
        self.claim_event(mirror)
        with mock.patch.object(
            jidanpu_upstream, "set_taker",
            side_effect=AmountError("别的金额问题", ErrorCode.VALIDATION),
        ):
            with self.assertRaises(AmountError) as caught:
                jidanpu_upstream.promote_upstream_mirror(
                    mirror, binding=self.binding, detail=mirror.upstream_payload,
                    user_id=self.taker.pk, now=self.now,
                )
        self.assertEqual(caught.exception.message, "别的金额问题")
        # 整个事务回滚：⛔ 不留半截（订单 / 链路 / 绩效都不该有）。
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)

    def test_an_unclear_refusal_on_the_outbox_stays_retryable_and_names_the_burnout_exit(self):
        """⭐ **F6（Task 8 修复轮）+ D-0335 出口改准**：认不出「已被接走」⇒ 保守按可重试
        （⛔ 不误判 `gone`）。这一档**只属 outbox 消费路**（请求路已改为当场放回大厅）。

        事件 `failed` + 退避；人话点明它会停在哪一档、以及烧完额度后的出口
        （D-0335 清道夫自动放回大厅——旧句「需人工核对」已不成立）。
        """
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        client = _StubClient(claim_code=0, claim_msg="系统繁忙，请稍后重试", claim_data=[])
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "failed")
        self.assertEqual(Order.objects.count(), 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertIn("退避重试", event.reason)      # ⛔ 不静默：点明它会停在哪一档
        self.assertIn("放回大厅", event.reason)      # 烧完额度后的出口（D-0335 清道夫）

    def test_an_unclear_refusal_on_the_request_path_releases_the_row_back_to_the_hall(self):
        """⭐ D-0335（「消掉抢单锁定」）：**请求路**认不出的拒绝 ⇒ **当场放回大厅**，不再
        留 `claiming` 按退避重试（旧口径把卡锁成「所有人接不了、打手拿临时失败」，烧完额度
        后永久卡死——L-5 的上游形态）。

        依据：`ApiError` = 上游失败信封（E-0087）⇒ 这次接单没被上家收下，单还在上家大厅
        ⇒ 回 `listed` 是事实不是赌。判据钉**五件事**：行回 `listed` 且两接单列清空、
        离厅灰卡被删（防一单双卡）、事件收敛成 `done`（⛔ 不是 `failed`——它不再重试）、
        绑定 `last_error` 带上游原话（运维仍能查根因）、打手拿到「已放回大厅」那句人话。
        """
        mirror = self.make_mirror()
        client = _StubClient(claim_code=0, claim_msg="系统繁忙，请稍后重试", claim_data=[])

        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
            with self.assertRaises(ClaimError) as caught:
                jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                )
        # 打手那句人话（D-0335 新常量；⛔ 不是旧的「正在抢单，请稍后刷新查看」一族）。
        self.assertEqual(caught.exception.code, ErrorCode.PLATFORM_QUERY_FAILED)
        self.assertEqual(
            str(caught.exception), jidanpu_upstream.HALL_CLAIM_RELEASED_MESSAGE
        )
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(mirror.claiming_by_id)     # 解冻闸（wallet._has_active_work）读它
        self.assertIsNone(mirror.claiming_at)
        self.assertIn("放回大厅", mirror.last_error)
        self.assertIn("系统繁忙", mirror.last_error)  # 上游原话仍接进人话（E-0087 口径不变）
        # 离厅灰卡被删：卡回大厅了，留着就是「可接区一张 + 灰区一张」的一单双卡。
        self.assertEqual(HallExitSnapshot.objects.filter(mirror=mirror).count(), 0)
        # 事件收敛成 done（终局、不再重试），reason 仍带上游原话与「放回大厅」。
        event = JidanpuSyncEvent.objects.get(kind="upstream.hall_claim")
        self.assertEqual(event.status, "done")
        self.assertIn("放回大厅", event.reason)
        self.assertIn("系统繁忙", event.reason)
        self.binding.refresh_from_db()
        self.assertIn("放回大厅", self.binding.last_error)
        # ⛔ 失败原因不带凭据（spec §11-18）。
        self.assertNotIn(CANARY_PASSWORD, self.binding.last_error)
        self.assertNotIn(CANARY_PASSWORD, event.reason)

    def test_the_request_path_reserves_then_calls_the_upstream_once(self):
        """请求路径（spec §6.1 的 ①–⑤）：本地预占与事件同事务 → 赢下租约才打上游 → 转正 + 审计。"""
        mirror = self.make_mirror()
        client = _StubClient()

        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
            order = jidanpu_upstream.claim_upstream_mirror(
                user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                actor=self.taker.username, actor_id=self.taker.pk, channel="网页端大厅",
            )
        self.assertEqual(client.paths, [REC_ORDER_PATH])
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(mirror.claiming_by_id, self.taker.pk)
        # 正式成交后只有一条普通接单日志，内部订单身份仍是系统单号。
        log = OperationLog.objects.get(action="order.claim")
        self.assertEqual(log.actor, self.taker.username)
        self.assertEqual(log.actor_id, self.taker.pk)
        self.assertEqual(log.order_no, order.order_no)
        self.assertEqual(log.target_id, str(self.taker.pk))
        self.assertEqual(log.reason, "网页端大厅")
        self.assertIn(f"接手了订单 {order.source_order_no}", log.summary)
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_remote_claim").exists())

    def test_the_request_path_yields_when_another_executor_holds_the_lease(self):
        """⭐ 执行租约：别人持着这条事件的租约 ⇒ 请求路径**一次上游都不打**。

        ⭐ D-0335 改准：输家那档从 `1002`「正在抢单，请稍后刷新查看」改回 **`1012`**
        （App 只对 1012 自动刷新列表；持约方秒级必收口，刷新看到的已是终局——打手不再
        对着旧快照手动刷新）。行状态断言仍钉 `claiming`：持约方在干活，输家不动行。

        ⚠️ 起手必须是 `listed`（否则第一道守卫先把它按「已被接走 / 处理中」拒掉）：
        事件由 `queue_hall_claim` 预先排下（行仍 `listed`），再让**别人**持住它的租约。
        """
        mirror = self.make_mirror()
        event = jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.taker.pk)
        self.assertIsNotNone(event)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            lease_owner="other-executor",
            lease_expires_at=timezone.now() + timedelta(days=365),
        )
        client = _StubClient()

        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
            with self.assertRaises(ClaimError) as caught:
                jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                )
        self.assertEqual(client.calls, [])
        self.assertEqual(caught.exception.code, ErrorCode.ORDER_ALREADY_TAKEN)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)

    def test_a_transport_failure_on_the_request_path_releases_the_row_back_to_the_hall(self):
        """⭐ D-0335：请求路**传输层失败**（连接 / 超时 / 会话建不起）⇒ 当场放回大厅 +
        事件收敛，对打手回业务码（旧口径原样上抛 500、行留 `claiming` 等后台慢慢收口）。

        残差（如实登记，方案 §2.1-2）：若上游其实已收下而响应丢失，`sync_my_orders` 的
        `record_assigned_order` 下一轮把那张单录回未分配池；期间他人再点判「已被接走」——
        与旧重试路同后果、不劣化。捕获面：`ApiError`（业务）已被 `_claim_refused` 接住、
        `AuthError` 显式重抛，都不落这一支。
        """
        mirror = self.make_mirror()
        client = _StubClient()

        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)), \
                mock.patch.object(
                    jidanpu_upstream, "rec_order_by_internal",
                    side_effect=TransportError("连接上游超时"),
                ):
            with self.assertRaises(ClaimError) as caught:
                jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                )
        self.assertEqual(caught.exception.code, ErrorCode.PLATFORM_QUERY_FAILED)
        self.assertEqual(
            str(caught.exception), jidanpu_upstream.HALL_CLAIM_RELEASED_MESSAGE
        )
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(mirror.claiming_by_id)
        self.assertIn("放回大厅", mirror.last_error)
        self.assertEqual(HallExitSnapshot.objects.filter(mirror=mirror).count(), 0)
        event = JidanpuSyncEvent.objects.get(kind="upstream.hall_claim")
        self.assertEqual(event.status, "done")     # 终局：不再占着退避队列
        self.assertIn("放回大厅", event.reason)

    def test_the_release_and_its_converge_share_one_transaction(self):
        """⭐ 评审 S-1（2026-10-30 修复轮）：release（行 UPDATE）与 converge（事件 UPDATE）
        **同一个事务**——拆开 atomic ⇒ 本用例红。

        竞态形状（评审报告读码：`_queue_out_event` 的 pending 去重）：两条语句分处两个
        自动提交时，微秒级窗口里第二个打手可把刚放回 `listed` 的行再接走，其
        `_queue_out_event` 会**复用**本路还没收口的 `pending` 事件，随后被本路
        `_converge` 标 `done` ⇒「行=claiming(新打手，实拿 1012 未执行) + 事件=done + 无链路」。

        判据打**真实请求路发出的 SQL 结构**（`CaptureQueriesContext`）：TestCase 外层
        原子里，`transaction.atomic()` 的直读证据就是一对 `SAVEPOINT…RELEASE` ——放回那条
        UPDATE 执行时所在的保存点，必须到收敛那条 UPDATE **之后**才释放（= 两条 UPDATE
        同事务提交，行锁压住并发再接）。变异（拆开 atomic）下两条 UPDATE 裸露、放回那条
        执行时保存点栈为空 ⇒ 红。表名带引号 + 目标状态字面量定位（Django debug cursor
        已把参数内插进 SQL；表名是 Django 默认的 `business_<模型名小写>`）；租约 UPDATE
        只写租约列、⛔ 不动 `status`（`jidanpu_common.claim_event_for_execution` 的硬判据）
        ⇒ 两条锚点在本流程里各自唯一。
        """
        mirror = self.make_mirror()
        client = _StubClient(claim_code=0, claim_msg="系统繁忙，请稍后重试", claim_data=[])

        with CaptureQueriesContext(connection) as ctx:
            with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
                with self.assertRaises(ClaimError):
                    jidanpu_upstream.claim_upstream_mirror(
                        user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                    )
        sqls = [" ".join(q["sql"].split()) for q in ctx.captured_queries]

        def locate(table: str, target: str) -> int:
            # ⚠️ 锚必须钉 **SET 子句**：预占那条 UPDATE 的 WHERE 守卫里也有
            # `"status" = 'listed'`，只看子串会把它误算进来。
            set_clause = f'SET "status" = \'{target}\''
            hits = [
                i for i, sql in enumerate(sqls)
                if sql.upper().startswith("UPDATE")
                and f'"{table}"' in sql
                and set_clause in sql
            ]
            self.assertEqual(
                len(hits), 1,
                f"预期恰好一条 UPDATE {table} → {target}，实得 {hits} 条；全量 SQL：\n"
                + "\n".join(sqls),
            )
            return hits[0]

        release_i = locate("business_jidanpuupstreamhallorder", "listed")
        converge_i = locate("business_jidanpusyncevent", "done")
        self.assertLess(release_i, converge_i)          # 顺序：先放回、后收敛

        stack: list[str] = []
        at_release: str | None = None
        for i, sql in enumerate(sqls):
            upper = sql.upper()
            if upper.startswith("SAVEPOINT"):
                stack.append(sql.split()[-1].strip('"'))
            elif upper.startswith(("RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")):
                if stack:
                    stack.pop()
            if i == release_i:
                at_release = stack[-1] if stack else None
        self.assertIsNotNone(
            at_release, "放回大厅的 UPDATE 没包在 atomic 里（评审 S-1 的竞态窗口敞开）"
        )
        closed_after_converge = [
            i for i, sql in enumerate(sqls)
            if i > converge_i
            and sql.upper().startswith("RELEASE SAVEPOINT")
            and sql.split()[-1].strip('"') == at_release
        ]
        self.assertTrue(
            closed_after_converge,
            f"保存点 {at_release} 在收敛 UPDATE 之前就释放了——release 与 converge "
            "不在同一事务，重接窗口复用 pending 事件的竞态成立",
        )
        # 终态锚（完整契约由上面两条既有用例钉着，这里只留定位锚防 SQL 解析落空）。
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        event = JidanpuSyncEvent.objects.get(kind="upstream.hall_claim")
        self.assertEqual(event.status, "done")


class UpstreamPromotedOrderSourceTests(_UpstreamConsumeBase):
    """⭐ D-0261：镜像**转正**建出的我方订单自动标注「订单来源」（计划钉 ② + ③ 的转正一半）。

    ⚠️ 直打**枚举出的建单写点**（E-0232）经真实消费路径（`queue_hall_claim` → `consume_event`）
    到 `promote_upstream_mirror`；判据打**数据库行**。录池那一半在 `test_jidanpu_upstream_record.py`。
    """

    def promote_one(self) -> Order:
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        self.assertEqual(jidanpu_upstream.consume_event(event, client=_StubClient()), "done")
        return Order.objects.get()

    def test_pin2_the_promoted_order_carries_the_bindings_source_and_name(self):
        """⭐ 钉②：配了来源的绑定转正 ⇒ 本地单 `order_source` + `source` 名字快照都带上。

        修前红：绑定表还没有 `order_source` 列（`save(update_fields=…)` 抛 `FieldError`）。
        """
        source = OrderSource.objects.create(name="星辰")
        self.binding.order_source = source
        self.binding.save(update_fields=["order_source"])
        order = self.promote_one()
        self.assertEqual(order.order_source_id, source.pk)
        self.assertEqual(order.source, "星辰")

    def test_a_binding_without_a_source_promotes_orders_with_no_source(self):
        """⭐ 钉③（转正一半）：未配来源 ⇒ 两列照旧全空（fail-soft，⛔ 不回落）。"""
        self.assertIsNone(self.binding.order_source)
        order = self.promote_one()
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "")


# ══ ①–⑥ 提交完成（A5 / G4）══════════════════════════════════════════════════════


class CompleteConsumeTests(_UpstreamConsumeBase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order(status=OrderStatus.COMPLETED)
        self.link = self.make_link(self.order)

    def _completion_client(self, readback="待验收"):
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = self.stub_oss_policy(_StubClient(
            remote_statuses=("代练中", readback),
            pic_envelope=dict(self.EMPTY_PIC_ENVELOPE),
        ))
        return event, client

    def _consume_completion(self, event, client):
        return jidanpu_upstream.consume_event(
            event, client=client, http=_StubHttp(), oss_post=_OssRecorder(),
        )

    def test_an_order_deleted_after_queueing_is_not_submitted(self):
        event, client = self._completion_client()
        soft_delete_order(order=self.order, actor=self.owner_user, is_del_emp="N")

        self.assertEqual(self._consume_completion(event, client), "skipped")

        self.assertEqual(client.paths, [])
        self.link.refresh_from_db()
        self.assertIsNone(self.link.completed_submitted_at)

    def test_reopening_during_picture_upload_stops_completion(self):
        event, client = self._completion_client()
        send = client.call

        def reopen_during_upload(method, path, **kwargs):
            response = send(method, path, **kwargs)
            if path == UPLOAD_PIC_PATH:
                Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
            return response

        client.call = reopen_during_upload
        self.assertEqual(self._consume_completion(event, client), "skipped")

        self.assertIn(UPLOAD_PIC_PATH, client.paths)
        self.assertNotIn(SET_STATUS_PATH, client.paths)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.completed_submitted_at)

    def test_a_new_completion_during_upload_stays_pending(self):
        self.order.completed_at = self.now
        self.order.save(update_fields=["completed_at"])
        event, client = self._completion_client()
        send = client.call

        def complete_again(method, path, **kwargs):
            response = send(method, path, **kwargs)
            if path == UPLOAD_PIC_PATH:
                self.order.completed_at = self.now + timedelta(minutes=2)
                self.order.save(update_fields=["completed_at"])
                self.assertEqual(jidanpu_upstream.queue_complete(self.order), 1)
            return response

        client.call = complete_again
        self.assertEqual(self._consume_completion(event, client), "skipped")

        self.assertNotIn(SET_STATUS_PATH, client.paths)
        event.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(event.status, "pending")
        self.assertIsNone(self.link.completed_submitted_at)

    def test_already_accepted_after_submit_is_a_successful_readback(self):
        event, client = self._completion_client(readback="已完成")

        self.assertEqual(self._consume_completion(event, client), "done")

        self.assertEqual(client.paths.count(SET_STATUS_PATH), 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.completed_submitted_at)

    def test_the_submit_payload_is_the_verbatim_shape(self):
        """⭐ spec §2.3：`SetOrderSta` 的 `data` **恰好**两个键（⛔ 不带退单端点那三件）。

        ⭐ D-0367：完成链默认带截图上传（补截图 + 接 OSS 桩，与后两条同款）。"""
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = self.stub_oss_policy(_StubClient(
            remote_statuses=("代练中", "待验收"),
            pic_envelope=dict(self.EMPTY_PIC_ENVELOPE),
        ))
        self.assertEqual(
            jidanpu_upstream.consume_event(
                event, client=client, http=_StubHttp(), oss_post=_OssRecorder(),
            ),
            "done",
        )
        submits = [kw for method, path, kw in client.calls if path == SET_STATUS_PATH]
        self.assertEqual(submits, [{
            "data": {"orderId": REMOTE_ID, "sta": COMPLETION_LABEL},
            "success_rule": "lenient",
        }])
        for forbidden in ("changeSoureType", "remark", "isSyncTaoBao"):
            self.assertNotIn(forbidden, submits[0]["data"])

    def test_the_success_writes_the_submitted_marker_and_the_ledger(self):
        """成功 ⇒ 链路补 `completed_submitted_at` + 台账 `done` + 人话。

        ⭐ D-0367：完成链默认带截图上传（本用例补一张 ready 截图 + 接 OSS 桩）。"""
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = self.stub_oss_policy(_StubClient(
            remote_statuses=("代练中", "待验收"),
            pic_envelope=dict(self.EMPTY_PIC_ENVELOPE),
        ))
        self.assertEqual(
            jidanpu_upstream.consume_event(
                event, client=client, http=_StubHttp(), oss_post=_OssRecorder(),
            ),
            "done",
        )
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.completed_submitted_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertTrue(event.reason)
        self.assertEqual(event.target_ref, f"{self.binding.pk}|{self.order.order_no}")
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")   # 成功不写 last_error

    def test_a_failure_envelope_is_judged_a_failure(self):
        """⭐ spec §11-9：`code=0` + 非空 `msg` + 空 `data` ⇒ 判失败（⛔ 不当成功）。"""
        event = self.complete_event(self.order, self.link)
        client = _StubClient(
            submit_code=0, submit_msg="还没上传过截图，不能提交完成", submit_data=[],
            remote_statuses=("代练中",),
        )
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "failed")
        self.link.refresh_from_db()
        self.assertIsNone(self.link.completed_submitted_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertIn("截图", event.reason)

    def test_a_second_submission_is_judged_a_success(self):
        """spec §11-9：成功码 = **提交笔数** ⇒ `code=2` 必须判成功（⛔ 不是「等于 1」）。

        ⭐ D-0367：完成链默认带截图上传（补截图 + 接 OSS 桩，同上一条）。"""
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = self.stub_oss_policy(_StubClient(
            submit_code=2, submit_msg="已成功提交2笔订单",
            remote_statuses=("代练中", "待验收"),
            pic_envelope=dict(self.EMPTY_PIC_ENVELOPE),
        ))
        self.assertEqual(
            jidanpu_upstream.consume_event(
                event, client=client, http=_StubHttp(), oss_post=_OssRecorder(),
            ),
            "done",
        )
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.completed_submitted_at)

    def test_a_failed_picture_push_sends_zero_submissions(self):
        """⭐ **A5 / spec §11-10（本任务的核心闸门）**：传图失败 ⇒ **一次 `SetOrderSta` 都不发**。

        判据 = 桩的**调用集合**（⛔ 不是「没报错」）。
        失败方向：把 `if outcome.blocks_completion:` 那一支删掉（照常提交）⇒ 本用例红。
        """
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = _StubClient(
            remote_statuses=("代练中",),
            # 上家**没有**图 ⇒ 必须走上传那三步。
            pic_envelope={"code": 0, "msg": {"title": "", "data": []}},
        )
        # 上传页缺隐藏域 ⇒ 传图第②步之前就失败（`step="fields"`）。
        http = _StubHttp(html=_upload_page_html(sid=None, game_acc_id=None, task_id=None))
        stats = jidanpu_upstream.run_outbox(
            self.binding, now=self.now, client=client, http=http,
        )
        self.assertEqual(stats.failed, 1)
        self.assertNotIn(SET_STATUS_PATH, client.paths)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.completed_submitted_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertIn("店铺标识", event.reason)     # 点名是哪一步（⛔ 不静默）
        self.binding.refresh_from_db()
        self.assertIn("店铺标识", self.binding.last_error)

    def test_a_missing_picture_is_uploaded_before_the_submission(self):
        """Task 7 的接口真的接上了：policy → OSS 直传 → `UploadOrderPic` → 才 `SetOrderSta`。"""
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = _StubClient(
            remote_statuses=("代练中", "待验收"),
            pic_envelope={"code": 0, "msg": {"title": "", "data": []}},
        )
        client.last_envelope = {}
        oss = _OssRecorder()
        http = _StubHttp()

        # 上传页取策略那一步要能回签名（`fetch_oss_policy`）。
        real_call = client.call

        def call(method, path, **kwargs):
            if path == jidanpu_upstream.OSS_POLICY_PATH:
                client.calls.append((method, path, dict(kwargs)))
                client.last_envelope = {
                    "code": 0, "msg": "success",
                    "data": {"accessid": "CANARY-ACCESSID", "host": "https://oss-synth.invalid",
                             "policy": "CANARY-POLICY", "signature": "CANARY-SIGNATURE",
                             "expire": 1790784240},
                }
                return client.last_envelope["data"]
            return real_call(method, path, **kwargs)

        client.call = call
        outcome = jidanpu_upstream.consume_event(
            event, client=client, http=http, oss_post=oss,
        )
        self.assertEqual(outcome, "done")
        self.assertEqual(client.paths, [
            MY_ORDERS_PATH, VIEW_PIC_PATH, jidanpu_upstream.OSS_POLICY_PATH,
            UPLOAD_PIC_PATH, SET_STATUS_PATH, MY_ORDERS_PATH,
        ])
        self.assertEqual(len(oss.calls), 1)
        self.assertEqual(http.gets[0][0], UPLOAD_PAGE_PATH)

    def test_a_remote_with_existing_pics_still_gets_ours(self):
        """⭐ **D-0367（2026-11-03 业务方拍板「上家有图也需要传」）**：对面已有图
        （哪怕是它家自己挂的参考图）⇒ 我们的验收截图**照传**，然后才提交完成。

        现场（E-0314 同族实测）：竹屿/团团工作室**每单创建即自带一张参考图**，
        旧口径（整档短路）会让验收截图一张都上不去——竹屿 3/3、团团工作室 4/4 样本，
        上家单上只有它自家那张图。
        失败方向：把调用点改回 `skip_if_remote_has_pic=True` ⇒ 本用例红
        （`UploadOrderPic` 一次都不发、`SetOrderSta` 照发）。
        判据 = 桩的**调用集合**（⛔ 不是「没报错」）。
        """
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = _StubClient(
            remote_statuses=("代练中", "待验收"),
            # 上家**已经有**一张图（它家自己挂的参考图）⇒ D-0367 起这不再是「不传」的理由。
            pic_envelope={"code": 0, "msg": {"title": "", "data": [
                {"pid": "0", "IsOpenView": "None",
                 "src": "https://oss-synth.invalid/theirs-ref.jpg",
                 "alt": "【合成客服】上传于11-2 09:00，说明：群二维码"},
            ]}},
        )
        client.last_envelope = {}
        oss = _OssRecorder()
        http = _StubHttp()

        real_call = client.call

        def call(method, path, **kwargs):
            if path == jidanpu_upstream.OSS_POLICY_PATH:
                client.calls.append((method, path, dict(kwargs)))
                client.last_envelope = {
                    "code": 0, "msg": "success",
                    "data": {"accessid": "CANARY-ACCESSID", "host": "https://oss-synth.invalid",
                             "policy": "CANARY-POLICY", "signature": "CANARY-SIGNATURE",
                             "expire": 1790784240},
                }
                return client.last_envelope["data"]
            return real_call(method, path, **kwargs)

        client.call = call
        outcome = jidanpu_upstream.consume_event(
            event, client=client, http=http, oss_post=oss,
        )
        self.assertEqual(outcome, "done")
        # 我们的图**真的上去了**：ViewPic（逐张判缺读一次）→ 取策略 → 直传 → 登记 → 才提交。
        self.assertEqual(client.paths, [
            MY_ORDERS_PATH, VIEW_PIC_PATH, jidanpu_upstream.OSS_POLICY_PATH,
            UPLOAD_PIC_PATH, SET_STATUS_PATH, MY_ORDERS_PATH,
        ])
        self.assertEqual(len(oss.calls), 1)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.completed_submitted_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_an_already_submitted_order_is_idempotent_by_read_back(self):
        """⭐ **G4**：上家已是「待验收」⇒ **先回读**判出来 ⇒ 只补 `completed_submitted_at`、⛔ 不再提交。

        失败方向：把「先回读再提交」的顺序倒过来（先提交）⇒ 本用例红（多发一次 `SetOrderSta`）。
        """
        event = self.complete_event(self.order, self.link)
        client = _StubClient(remote_statuses=("待验收",))
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        self.assertEqual(client.paths, [MY_ORDERS_PATH])          # ⛔ 一次提交都没发
        self.assertNotIn(SET_STATUS_PATH, client.paths)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.completed_submitted_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertIn("幂等", event.reason)

    def test_an_order_that_is_not_completed_is_never_submitted(self):
        """spec §6.2：我方订单**不是「已完成」**（中间任何状态）⇒ 一个上游调用都不发。"""
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        event = self.complete_event(self.order, self.link)
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "skipped")
        self.assertEqual(client.calls, [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("已完成", event.reason)

    def test_a_readback_that_does_not_confirm_is_not_a_success(self):
        """spec §6.2 第 ⑥ 步：提交后回读**没确认**已变「待验收」⇒ `failed` + 退避（⛔ 不当成功）。

        ⭐ D-0367：完成链默认带截图上传（补截图 + 接 OSS 桩 + 空远端）。"""
        self.make_screenshot(self.order)
        event = self.complete_event(self.order, self.link)
        client = self.stub_oss_policy(_StubClient(
            remote_statuses=("代练中", "代练中"),
            pic_envelope=dict(self.EMPTY_PIC_ENVELOPE),
        ))
        self.assertEqual(
            jidanpu_upstream.consume_event(
                event, client=client, http=_StubHttp(), oss_post=_OssRecorder(),
            ),
            "failed",
        )
        self.assertIn(SET_STATUS_PATH, client.paths)              # 提交真的发了
        self.link.refresh_from_db()
        self.assertIsNone(self.link.completed_submitted_at)       # 但没被当成成功
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)

    def test_complete_does_nothing_without_a_link(self):
        """⭐ **F5（Task 8 修复轮）**：`COMPLETE_LINK_MISSING_REASON` 那一支。

        台账里的链路已不存在（换绑 / 删号）⇒ 提交完成**一次上游都不打**、收敛成 `skipped`
        + 人话。⚠️ 这一支此前**没有用例**（它恰好是「静默跳过提交」的那条分支）。
        """
        event = self.complete_event(self.order, self.link)
        JidanpuUpstreamOrderLink.objects.filter(pk=self.link.pk).delete()
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "skipped")
        self.assertEqual(client.calls, [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("链路", event.reason)

    def test_complete_does_nothing_when_the_link_belongs_to_another_binding(self):
        """F5 的另一半：链路还在、但**不是这条事件的绑定**（换绑过）⇒ 同一分支，同样零上游调用。

        ⚠️ 判据是 `link.binding_id != binding.pk`（与 F1 同一条「事件归属 = 链路归属」口径）。
        """
        link = self.make_link(self.order, binding=self.other_binding)
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", status="pending",
            target_type="order", target_ref=f"{self.binding.pk}|{self.order.order_no}",
            payload={"upstream_binding_id": self.binding.pk, "link_id": link.pk,
                     "upstream_order_id": REMOTE_ID},
        )
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "skipped")
        self.assertEqual(client.calls, [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("链路", event.reason)

    def test_a_terminated_link_is_never_submitted(self):
        """spec §5.4：已停止跟踪的链路 ⇒ ⛔ 一个上游调用都不打（`queue_complete` 也不排它）。"""
        JidanpuUpstreamOrderLink.objects.filter(pk=self.link.pk).update(
            terminated_at=self.now
        )
        self.assertEqual(jidanpu_upstream.queue_complete(self.order), 0)
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", status="pending",
            target_type="order", target_ref=f"{self.binding.pk}|{self.order.order_no}",
            payload={"upstream_binding_id": self.binding.pk, "link_id": self.link.pk,
                     "upstream_order_id": REMOTE_ID},
        )
        client = _StubClient()
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "skipped")
        self.assertEqual(client.calls, [])

    def test_the_ledger_reason_never_carries_a_credential(self):
        """spec §11-18：失败原因**搜不到**密码（**搜字符串**，⛔ 不是「看起来没有」）。"""
        event = self.complete_event(self.order, self.link)
        client = _StubClient(
            submit_code=0, submit_msg="没权限", submit_data=[],
            remote_statuses=("代练中",),
        )
        jidanpu_upstream.consume_event(event, client=client)
        event.refresh_from_db()
        self.assertNotIn(CANARY_PASSWORD, event.reason)
        self.binding.refresh_from_db()
        self.assertNotIn(CANARY_PASSWORD, self.binding.last_error)


# ══ 排事件（零绑定即零成本）+ 一轮 outbox ═══════════════════════════════════════


class QueueAndRoundTests(_UpstreamConsumeBase):
    def test_queue_complete_costs_one_query_without_a_link(self):
        """⭐ 零绑定即零成本（Task 9 会把它挂在最热的写入口上）：没有链路 ⇒ **恰好一次**查询。

        ⚠️ 只数 `SELECT`：`assertNumQueries` 会把 django-tenants 的 `SET search_path` 也算进去
        （那是**租户上下文**，不是本函数的取数）。
        """
        order = self.make_order(status=OrderStatus.COMPLETED)
        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(jidanpu_upstream.queue_complete(order), 0)
        selects = [
            query for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith("SELECT")
        ]
        self.assertEqual(len(selects), 1)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0)

    def test_queue_complete_queues_exactly_one_event_carrying_the_binding(self):
        order = self.make_order(status=OrderStatus.COMPLETED)
        link = self.make_link(order)
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)
        event = JidanpuSyncEvent.objects.get()
        self.assertEqual(event.kind, "upstream.complete")
        self.assertEqual(event.direction, "out")
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.payload["upstream_binding_id"], self.binding.pk)
        self.assertEqual(event.payload["link_id"], link.pk)
        # 在飞去重：同一张单再排一次不新增行。
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 1)

    def test_an_explicit_re_queue_gives_a_burned_out_event_a_new_budget(self):
        """⭐ **F3（Task 8 修复轮）**：烧完重试额度的行 + 一次**新的显式触发** ⇒ 重新可跑。

        修前：`attempts=5`（`run_outbox` 归 `abandoned`、此后一次上游都不打）的行会被
        `_queue_out_event` 的去重**永远**命中（它只排除 `done` / `skipped`）⇒ `queue_complete`
        照样回 1、台账不新增一行、下一次 `run_outbox` 只记 `abandoned` ——**没有任何恢复入口**。

        判据钉**行上的四个值**（额度 / 状态 / 原因 / 退避锚都复位），再钉「**下一次真的打上游**」
        （⛔ 不是「返回了同一行」——那正是旧行为）。口径与反方向线
        `jidanpu_member.queue_member_event` 的 `rearm` 同一处取舍。
        """
        order = self.make_order(status=OrderStatus.COMPLETED)
        link = self.make_link(order)
        self.make_screenshot(order)      # ⭐ D-0367：完成链默认带截图上传
        self.complete_event(order, link)
        JidanpuSyncEvent.objects.filter(kind="upstream.complete").update(
            status="failed", attempts=jidanpu_upstream.OUTBOX_MAX_ATTEMPTS,
            reason="上一轮打上游失败", processed_at=self.now,
        )
        burned = JidanpuSyncEvent.objects.get(kind="upstream.complete")
        # 再点一次「这一单完成了」（显式动作）⇒ 同一行被重新发额度。
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)
        fresh = JidanpuSyncEvent.objects.get(pk=burned.pk)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 1)     # ⛔ 不新建第二行
        self.assertEqual(fresh.attempts, 0)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.reason, "")
        self.assertIsNone(fresh.processed_at)
        # 真的会打上游（这一句才是「恢复入口」）：提交完成那一跳的完整序列。
        client = self.stub_oss_policy(_StubClient(
            remote_statuses=("代练中", "待验收"),
            pic_envelope=dict(self.EMPTY_PIC_ENVELOPE),
        ))
        stats = jidanpu_upstream.run_outbox(
            self.binding, now=self.now, client=client,
            http=_StubHttp(), oss_post=_OssRecorder(),
        )
        self.assertEqual(stats.done, 1)
        self.assertEqual(stats.abandoned, 0)
        self.assertIn(SET_STATUS_PATH, client.paths)

    def test_a_row_still_inside_its_budget_is_not_re_armed(self):
        """⚠️ F3 的边界：只复位**烧完额度**的行——还在额度内的行照旧按退避等（连点几下不能绕过退避）。"""
        order = self.make_order(status=OrderStatus.COMPLETED)
        link = self.make_link(order)
        self.complete_event(order, link)
        JidanpuSyncEvent.objects.filter(kind="upstream.complete").update(
            status="failed", attempts=1, reason="上一轮打上游失败", processed_at=self.now,
        )
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)
        row = JidanpuSyncEvent.objects.get(kind="upstream.complete")
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.reason, "上一轮打上游失败")

    def test_an_explicit_hall_re_queue_gives_a_burned_out_event_a_new_budget(self):
        """⭐ **M-1（Task 8 修复轮收口，2026-09-29 评审实测）**：`queue_hall_claim` 的 `rearm=True`
        也要有**能失败**的判据（改前：把那一处改 `False` ⇒ 整文件全绿 SURVIVED）。

        ⚠️ **现场为什么直调本函数造**：真实请求路径今天排不出这一幕——`claim_upstream_mirror`
        进入前必须 `row.status == listed`，而 `listed → claiming` 与排事件**同事务**、本表又没有
        `claiming → listed` 的回退边 ⇒ 一个镜像行一生只排一次（那一半由
        `test_the_request_path_cannot_re_enter_an_already_claimed_row` 钉住）。
        `queue_hall_claim` 是模块的公开触发点，将来任何新增的直接 / 重试入口都会命中这一格 ⇒
        按「显式动作」的契约直接造现场：先排下一条烧完额度的旧行，再走一次显式动作。

        判据钉**行上的四个值**（额度 / 状态 / 原因 / 退避锚都复位），再钉「**下一次真的打上游**」
        （⛔ 不是「返回了同一行」——那正是旧行为）。口径与 `queue_complete` 那一处（F3）同一取舍。
        """
        mirror = self.make_mirror()
        burned = self.claim_event(mirror)          # 行 listed→claiming + 事件排下（显式动作）
        JidanpuSyncEvent.objects.filter(pk=burned.pk).update(
            status="failed", attempts=jidanpu_upstream.OUTBOX_MAX_ATTEMPTS,
            reason="上一轮接单失败", processed_at=self.now,
        )
        # 再来一次显式接单动作（本函数的公开入口）⇒ 同一行被重新发额度。
        again = jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.taker.pk)
        self.assertEqual(again.pk, burned.pk)
        fresh = JidanpuSyncEvent.objects.get(pk=burned.pk)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 1)     # ⛔ 不新建第二行
        self.assertEqual(fresh.attempts, 0)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.reason, "")
        self.assertIsNone(fresh.processed_at)
        # 真的会打上游（这一句才是「恢复入口」）：大厅接单那一跳。
        client = _StubClient()
        stats = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)
        self.assertEqual(stats.done, 1)
        self.assertEqual(stats.abandoned, 0)
        self.assertIn(REC_ORDER_PATH, client.paths)

    def test_the_request_path_cannot_re_enter_an_already_claimed_row(self):
        """M-1 的另一半（**可达性证据**）：真实请求路径对**同一个镜像行**只可能排一次事件。

        `claim_upstream_mirror` 的第二跳在 `row.status != listed` 那道守卫上就红了
        （⛔ 不会再排一条、⛔ 不会再有第二次显式动作）⇒ 上面那条 `rearm` 判据只能由**直接入口**
        造现场。⚠️ 本用例钉的是「请求路径只有一次」这个事实，⛔ 不是 M-1 的判据本身。
        """
        mirror = self.make_mirror()
        client = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
            jidanpu_upstream.claim_upstream_mirror(
                user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
            )
        self.assertEqual(JidanpuSyncEvent.objects.count(), 1)
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
            with self.assertRaises(ClaimError):
                jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                )
        self.assertEqual(JidanpuSyncEvent.objects.count(), 1)     # ⛔ 不排第二条

    def test_a_released_row_can_be_claimed_again(self):
        """⭐ D-0335：「消掉抢单锁定」的**可达面**——失败放回大厅的行**可以再点**。

        旧口径：认不出的拒绝把行锁死在 `claiming`、本表没有回退边 ⇒ 一个镜像行一生只排
        一次事件（M-1）。新口径：请求路当场放回 `listed` ⇒ 第二次接单完整重走闸门、排
        **第二条**事件（旧那条已随失败收敛成 `done`，不在去重面里）、**第二次真打上游**。
        成功路仍一生一次（上一条用例钉着）——那是终态，不是锁。
        """
        mirror = self.make_mirror()
        client = _StubClient(claim_code=0, claim_msg="系统繁忙，请稍后重试", claim_data=[])
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client)):
            with self.assertRaises(ClaimError):
                jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
                )
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        first = JidanpuSyncEvent.objects.get(kind="upstream.hall_claim")
        self.assertEqual(first.status, "done")
        # 第二次接单：这次上游成功 ⇒ 成交。
        client2 = _StubClient()
        with mock.patch.object(jidanpu, "claim_session", lambda _b: _ClientCtx(client2)):
            order = jidanpu_upstream.claim_upstream_mirror(
                user_id=self.taker.pk, mirror_id=mirror.pk, now=self.now,
            )
        self.assertEqual(JidanpuSyncEvent.objects.count(), 2)     # 第二条事件（新的一条）
        self.assertIn(REC_ORDER_PATH, client2.paths)
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)

    def test_a_paused_binding_touches_nothing(self):
        """spec §8.4：停用的绑定 ⇒ 一条事件都不动（`paused=True`），⛔ 也不建连。"""
        mirror = self.make_mirror()
        event = self.claim_event(mirror)
        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        client = _StubClient()
        stats = jidanpu_upstream.run_outbox(
            self.binding, now=self.now, client=client,
        )
        self.assertTrue(stats.paused)
        self.assertEqual(client.calls, [])
        event.refresh_from_db()
        self.assertEqual(event.status, "pending")

    def test_a_leased_event_is_deferred_by_the_round(self):
        """⭐ 执行租约（后台那一侧）：别人持着这条事件的租约 ⇒ 本轮**一次上游都不打**。"""
        mirror = self.make_mirror()
        event = jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.taker.pk)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            lease_owner="other-executor",
            lease_expires_at=timezone.now() + timedelta(days=365),
        )
        client = _StubClient()
        stats = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)
        self.assertEqual(stats.deferred, 1)
        self.assertEqual((stats.done, stats.failed), (0, 0))
        self.assertEqual(client.calls, [])

    def test_a_freshly_failed_event_waits_out_its_backoff_window(self):
        """退避阶梯：刚失败过的事件在退避窗内 ⇒ 本轮不动它（⛔ 不打上游）。"""
        mirror = self.make_mirror()
        event = jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.taker.pk)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=1, processed_at=self.now,
        )
        client = _StubClient()
        stats = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)
        self.assertEqual(stats.deferred, 1)
        self.assertEqual(client.calls, [])

    def test_an_event_past_the_retry_cap_is_abandoned(self):
        """到尝试上限 ⇒ 不再自动重试（`abandoned`）、⛔ 不打上游，留台账待人看。"""
        mirror = self.make_mirror()
        event = jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.taker.pk)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=jidanpu_upstream.OUTBOX_MAX_ATTEMPTS,
        )
        client = _StubClient()
        stats = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)
        self.assertEqual(stats.abandoned, 1)
        self.assertEqual(client.calls, [])

    def test_run_outbox_stamps_the_binding_error_on_failure(self):
        """失败 ⇒ 绑定 `last_error` 有人话（Task 7 刻意留给本任务落库），事件仍可重试。"""
        order = self.make_order(status=OrderStatus.COMPLETED)
        link = self.make_link(order)
        self.complete_event(order, link)
        client = _StubClient(
            submit_code=0, submit_msg="没权限", submit_data=[],
            remote_statuses=("代练中",),
        )
        stats = jidanpu_upstream.run_outbox(self.binding, now=self.now, client=client)
        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.done, 0)
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.last_error)
        self.assertNotIn(CANARY_PASSWORD, self.binding.last_error)
        event = JidanpuSyncEvent.objects.get(kind="upstream.complete")
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)

    def test_run_outbox_without_events_never_builds_a_client(self):
        """空转：没有在飞事件 ⇒ 一次上游都不打（⛔ 不白登录一遍），只打「上次运行时刻」。"""
        with mock.patch.object(jidanpu, "open_client_with_http") as opener:
            stats = jidanpu_upstream.run_outbox(self.binding, now=self.now)
        self.assertFalse(opener.called)
        self.assertEqual((stats.done, stats.failed, stats.skipped), (0, 0, 0))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_run_at, self.now)


# ══ ⑧ ⭐ 提交完成的**回读**改 `key1` 单查（计划 §1-④ / §2-5）═══════


class _Key1ProbeStub:
    """只服务**回读**那一发：逐字记下 `query`，并按给定行回包（⛔ 不判 `count`，让实现自己回校验）。"""

    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs.get("query") or {})))
        assert path == MY_ORDERS_PATH, f"未预期的端点：{method} {path}"
        self.last_envelope = {
            "code": 0, "msg": "success", "data": [dict(r) for r in self.rows],
            "count": len(self.rows),
        }
        return [dict(r) for r in self.rows]


class RemoteRowKey1Tests(SimpleTestCase):
    """⭐ `_remote_row`：**按 `orderId` 单查** + **逐字回校验**（⛔ 不拿 `count == 1` 当判据）。"""

    def test_the_readback_asks_by_key1(self):
        """请求形状逐字钉住：只带 `page` 与 `key1`（⛔ 不带时间窗、⛔ 不拉整表）。"""
        stub = _Key1ProbeStub([{"orderId": REMOTE_ID, "orderSta": "代练中"}])
        row = jidanpu_upstream._remote_row(stub, REMOTE_ID)
        self.assertEqual(row["orderId"], REMOTE_ID)
        self.assertEqual(stub.calls, [("GET", MY_ORDERS_PATH, {"page": 1, "key1": REMOTE_ID})])

    def test_a_substring_hit_on_another_order_is_not_accepted(self):
        """⭐ `key1` 实测会**子串**命中（`orderIdView` LIKE `%key%`）⇒ 只认 `orderId` **逐字相等**。

        失败方向：拿「命中数 == 1」或「第一行」当判据 ⇒ 回读到**别的单**（本用例红）。
        """
        stub = _Key1ProbeStub([
            {"orderId": REMOTE_ID + "9", "orderSta": "代练中"},   # 子串命中，但不是这一张单
            {"orderId": REMOTE_ID, "orderSta": "待验收"},          # 逐字相等那一行才是它
        ])
        row = jidanpu_upstream._remote_row(stub, REMOTE_ID)
        self.assertEqual(row["orderId"], REMOTE_ID)
        self.assertEqual(row["orderSta"], "待验收")

    def test_no_row_matching_verbatim_is_none(self):
        """没有**逐字相等**那一行 ⇒ `None`（调用方按「读不到」处理，⛔ 不判远端没有这张单）。"""
        stub = _Key1ProbeStub([{"orderId": "别的单", "orderSta": "代练中"}])
        self.assertIsNone(jidanpu_upstream._remote_row(stub, REMOTE_ID))
