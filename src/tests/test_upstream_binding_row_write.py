"""「绑定上家系统设置」页的**行级写端点**（E-0255 / D-0256：鸡蛋铺上家绑定改**多行**资源）。

被测对象 = `apps/business/api/upstream_binding_views.py` 的行级三支 + 既有兼容端点：

| 端点 | 判据 |
|---|---|
| `POST /api/business/upstream-bindings/jidanpu/` | 行级**创建**：`user` = 操作者（录入人语义）；节点门与列表/删除同门；`enabled=True` 走与既有 PUT 同一份**检测闸**（`detect_binding` 不过 ⇒ 一行都不落） |
| `PUT /api/business/upstream-bindings/jidanpu/<pk>/` | 行级**编辑**：只动目标行；检测闸照 D-0261 审阅 B（改凭据 / 翻开关才跑；仅改来源免检测、免盖 `checked_at`）；收窄写入照 `_PUT_WRITE_FIELDS` 口径；换凭据清 `checked_at` + 收敛在飞事件 |
| `GET /api/business/upstream-bindings/jidanpu/<pk>/` | 行级**读**（编辑弹窗的回填出口；节点门同门） |
| `PUT /api/business/member/upstream-jidanpu-binding/`（既有，兼容保留） | 语义**一字不改**：仍作用「本人那一行」（E-0255 的旧机制按兼容口径原样保留） |

⚠️ **钉① 的修前红**（E-0255 的机制存证）：同账号走既有 member PUT 连存两条不同地址 ⇒
第二笔**静默覆盖**第一笔（一行都不剩）——那是被修掉的旧世界，但 member PUT 本身按 D-0256
**兼容保留**，这条覆盖语义也随之钉住（它是「本人那一行」单行资源的定义，不是回归）。

⚠️ 判据打**数据库行**与**桩的调用集合**（`AGENTS.md` §5-G）；检测一律 mock
（`jidanpu_upstream.detect_binding`，E-0029：打在**被查找的那个模块**上），零真实网络。
⚠️ 夹具一律**合成值**（E-0077）：域名 `…test.invalid`、账号 `kefu-up-*`、密码 `pw-canary-*`。
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_common, jidanpu_upstream, jidanpu_upstream_pics
from apps.business.models import (
    Game, JidanpuSyncEvent, JidanpuUpstreamBinding, JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink, Order, OrderScreenshot, OrderSource, OrderStatus,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

#: 行级三支（**逐字**，⛔ 不反向解析——判据要钉住真实 URL 拼写）。
ROW_CREATE_URL = "/api/business/upstream-bindings/jidanpu/"
ROW_DETAIL_URL = "/api/business/upstream-bindings/jidanpu/{pk}/"
#: 既有的按用户 member PUT（兼容保留；D-0256）。
MEMBER_PUT_URL = "/api/business/member/upstream-jidanpu-binding/"

#: 本页节点门（逐字写死，⛔ 不从视图 import `UPSTREAM_BINDING_NODE`——E-0024 自指断言）。
PAGE_BINDING_NODE = "system.bind_upstream_system"

CREATE_ACTION = "system.jidanpu_upstream_binding_create"
UPDATE_ACTION = "system.jidanpu_upstream_binding_update"

#: 检测失败的人话（字面量，⛔ 不从被测模块常量派生）。
LOGIN_FAILURE = "登录失败：账号或密码错误"

BASE_URL_A = "https://row-a.test.invalid"
BASE_URL_B = "https://row-b.test.invalid"
ACCOUNT = "kefu-up-row"
ACCOUNT_B = "kefu-up-row-b"
PASSWORD = "pw-canary-row"
PASSWORD_B = "pw-canary-row-b"

#: 换绑时收敛在飞事件的人话原因要点名「人工核对」。
REBIND_REASON_HINT = "人工核对"


def _detect(ok, problems=()):
    """把 `jidanpu_upstream.detect_binding` 换成固定结论的桩（零真实网络）。"""
    return mock.patch.object(
        jidanpu_upstream,
        "detect_binding",
        return_value=jidanpu_upstream.DetectionReport(ok=ok, problems=tuple(problems)),
    )


def _payload(**overrides):
    body = {
        "jidanpu_base_url": BASE_URL_A,
        "account": ACCOUNT,
        "password": PASSWORD,
        "hall_price_percent": 100,
        "enabled": False,
    }
    body.update(overrides)
    return body


class RowWriteFixtureMixin:
    """行级端点的合成夹具（持节点操作人 + 另一个无节点人）。

    ⚠️ 两个客户端**必须各用各的组**：`make_logged_in_client` 的 `_grant_nodes`
    只往**已有 Group** 上挂节点 ⇒ 同一组名下第一个客户端拿到的节点会被第二个客户端
    **继承**，无节点那位的 403 判据就废了（E-0029 同族：判据要能失败）。
    """

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="kefu-row", groups=("客服行级甲",), nodes=(PAGE_BINDING_NODE,),
        )
        self.user = User.objects.get(username="kefu-row")
        self.other_client = make_logged_in_client(
            username="kefu-row-nonode", groups=("客服行级乙",), nodes=(),
        )
        self.other = User.objects.get(username="kefu-row-nonode")

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _post_row(self, *, client=None, **overrides):
        body = _payload(**overrides)
        return (client or self.client).post(
            ROW_CREATE_URL, json.dumps(body), content_type="application/json"
        )

    def _put_row(self, pk, *, client=None, **overrides):
        body = _payload(**overrides)
        return (client or self.client).put(
            ROW_DETAIL_URL.format(pk=pk), json.dumps(body),
            content_type="application/json",
        )

    def _get_row(self, pk, *, client=None):
        return (client or self.client).get(ROW_DETAIL_URL.format(pk=pk))

    def _member_put(self, *, client=None, **overrides):
        body = {
            "jidanpu_base_url": BASE_URL_A,
            "account": ACCOUNT,
            "password": PASSWORD,
            "enabled": False,
        }
        body.update(overrides)
        return (client or self.client).put(
            MEMBER_PUT_URL, json.dumps(body), content_type="application/json"
        )


class RowCreateTests(RowWriteFixtureMixin, TenantTestCase):
    """`POST /api/business/upstream-bindings/jidanpu/`（行级创建；钉①/③ 的落点）。"""

    # ── ⭐ 人填的「上家系统名称」（`name`）────────────────────────

    def test_create_with_name_persists_it_and_the_response_carries_it(self):
        """⭐ 创建带 `name` ⇒ 落库 + 响应带出（编辑弹窗回填要靠响应里的这一键）。"""
        res = self._post_row(name="初音网游", enabled=False)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuUpstreamBinding.objects.get(jidanpu_base_url=BASE_URL_A)
        self.assertEqual(row.name, "初音网游")
        self.assertEqual(res.json()["data"]["name"], "初音网游")

    def test_create_without_name_defaults_to_empty_and_account_name_stays_read_only(self):
        """不带 `name` ⇒ 落库默认空串；`account_name`（同步引擎缓存）照旧**只读**——
        请求体塞它 ⇒ 静默忽略、库里不落（只读语义与加 `name` 列之前一字不差）。
        """
        res = self._post_row(account_name="塞进来的显示名", enabled=False)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuUpstreamBinding.objects.get(jidanpu_base_url=BASE_URL_A)
        self.assertEqual(row.name, "")
        self.assertEqual(row.account_name, "", "account_name 必须仍是只读（同步引擎的写入方）")

    def test_pin1_same_account_creates_two_rows_that_do_not_overwrite_each_other(self):
        """⭐ 钉①（E-0255 / D-0256 的核心）：同账号连建两条**不同地址** ⇒ 两行都在、互不覆盖。

        修前红：行级端点不存在（404/405）；旧世界只有 member PUT，它的第二次保存会
        **静默覆盖**第一笔（见 `RowMemberPutCompatTests` 那条存证）。
        """
        with _detect(True):
            first = self._post_row(jidanpu_base_url=BASE_URL_A, enabled=True)
            second = self._post_row(jidanpu_base_url=BASE_URL_B, enabled=True)
        self.assertEqual(first.json()["code"], 0, first.content)
        self.assertEqual(second.json()["code"], 0, second.content)
        rows = JidanpuUpstreamBinding.objects.filter(user=self.user).order_by("pk")
        self.assertEqual(rows.count(), 2, "第二笔创建把第一笔覆盖掉了（E-0255 的旧机制还在）")
        self.assertEqual(
            [row.jidanpu_base_url for row in rows], [BASE_URL_A, BASE_URL_B]
        )
        self.assertEqual([row.account for row in rows], [ACCOUNT, ACCOUNT])

    def test_create_sets_user_to_the_operator_and_logs_the_create_action(self):
        """`user` = 操作者（D-0256 的「录入人」语义）；留痕动作码沿用既有 `_create`。"""
        with _detect(True):
            res = self._post_row(enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuUpstreamBinding.objects.get(jidanpu_base_url=BASE_URL_A)
        self.assertEqual(row.user_id, self.user.pk)
        self.assertIs(row.enabled, True)
        self.assertIsNotNone(row.checked_at)
        log = OperationLog.objects.get()
        self.assertEqual(log.action, CREATE_ACTION)
        self.assertEqual(log.actor, "kefu-row")
        self.assertEqual(log.actor_id, self.user.pk)
        self.assertEqual(log.target_id, str(row.pk))

    def test_create_with_detection_failure_writes_nothing(self):
        """⭐ 钉③：检测不过 ⇒ 一行都不落、一条留痕都没有（文案点名缺项）。"""
        with _detect(False, problems=(LOGIN_FAILURE,)):
            res = self._post_row(enabled=True)
        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn(LOGIN_FAILURE, body["msg"])
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_create_disabled_row_never_calls_the_detector(self):
        """`enabled=False` 的创建不走检测（与既有 PUT 的停用路径同一口径）。"""
        with _detect(True) as detect:
            res = self._post_row(enabled=False)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(detect.called)
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), 1)
        self.assertEqual(OperationLog.objects.count(), 1)

    def test_the_detector_receives_the_pending_row_not_a_persisted_one(self):
        """检测读的是「落库后会是什么」的未落库实例（与既有 PUT 的同一份闸）。"""
        seen = []

        def spy(binding):
            seen.append((binding.pk, binding.jidanpu_base_url, binding._state.adding))
            return jidanpu_upstream.DetectionReport(ok=True)

        with mock.patch.object(jidanpu_upstream, "detect_binding", side_effect=spy):
            res = self._post_row(enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertEqual(len(seen), 1)
        pk, base_url, adding = seen[0]
        self.assertIsNone(pk, "检测那一下行还没落库（失败时不留半行，E-0022 边界）")
        self.assertEqual(base_url, BASE_URL_A)
        self.assertIs(adding, True)


class RowEditTests(RowWriteFixtureMixin, TenantTestCase):
    """`PUT /api/business/upstream-bindings/jidanpu/<pk>/`（行级编辑；钉② 的落点）。"""

    def setUp(self):
        super().setUp()
        with _detect(True):
            res = self._post_row(jidanpu_base_url=BASE_URL_A, enabled=True)
            self.assertEqual(res.json()["code"], 0, res.content)
            res = self._post_row(
                jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B, enabled=True
            )
            self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a = JidanpuUpstreamBinding.objects.get(jidanpu_base_url=BASE_URL_A)
        self.row_b = JidanpuUpstreamBinding.objects.get(jidanpu_base_url=BASE_URL_B)

    def test_pin2_edit_touches_only_the_target_row(self):
        """⭐ 钉②：行级编辑只动目标行，另一行一个字节都不变。"""
        with _detect(True):
            res = self._put_row(
                self.row_a.pk, account="kefu-up-row-edited", enabled=True
            )
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.row_b.refresh_from_db()
        self.assertEqual(self.row_a.account, "kefu-up-row-edited")
        # 另一行原样（地址 / 账号 / 开关全没动）。
        self.assertEqual(
            (self.row_b.jidanpu_base_url, self.row_b.account),
            (BASE_URL_B, ACCOUNT_B),
        )
        self.assertIs(self.row_b.enabled, True)
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), 2)

    def test_switching_store_allows_the_same_remote_number_to_belong_to_a_new_order(self):
        from tests.test_jidanpu_upstream_api import _MyOrdersStub

        game = Game.objects.create(name="合成换绑游戏")
        old = Order.objects.create(game=game, order_no="20261120000000009951", status=OrderStatus.IN_PROGRESS)
        remote_id = "synthetic-shared-store-number"
        old_link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.row_a, order=old, upstream_order_id=remote_id,
        )
        with _detect(True):
            response = self._put_row(
                self.row_a.pk, jidanpu_base_url="https://replacement.test.invalid",
                account="synthetic-new-account", enabled=True,
            )
        self.assertEqual(response.json()["code"], 0, response.content)
        self.row_a.refresh_from_db()
        stub = _MyOrdersStub([{
            "orderId": remote_id, "orderIdView": "synthetic-new-source-number",
            "orderSta": "代练中", "gameName": game.name, "recFee": "75.00",
            "orderRemark": "新店备注",
        }])

        outcome = jidanpu_upstream.sync_my_orders(binding=self.row_a, client=stub, now=timezone.now())

        old.refresh_from_db()
        self.assertEqual((outcome.recorded, old.status, old.remark), (1, OrderStatus.IN_PROGRESS, ""))
        self.assertIsNotNone(JidanpuUpstreamOrderLink.all_objects.get(pk=old_link.pk).retired_at)
        new = JidanpuUpstreamOrderLink.objects.select_related("order").get(binding=self.row_a).order
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual((new.source_order_no, new.remark), ("synthetic-new-source-number", "新店备注"))

    def test_remaining_old_poll_rows_cannot_fill_the_new_stores_order(self):
        from tests.test_jidanpu_upstream_api import _MyOrdersStub

        game = Game.objects.create(name="合成批次换绑游戏")
        first = Order.objects.create(game=game, status=OrderStatus.IN_PROGRESS)
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.row_a, order=first, upstream_order_id="old-row-first",
        )
        now = timezone.now()
        new = None

        def rebind_during_first_rows_pictures(**kwargs):
            nonlocal new
            if kwargs["order"].pk != first.pk:
                return
            with _detect(True):
                response = self._put_row(
                    self.row_a.pk, account="synthetic-new-account", enabled=True,
                )
            self.assertEqual(response.json()["code"], 0, response.content)
            binding = JidanpuUpstreamBinding.objects.get(pk=self.row_a.pk)
            new = jidanpu_upstream.record_assigned_order(
                binding, {"orderId": "shared-row-second", "orderSta": "代练中"},
                game=game, now=now - timedelta(seconds=1),
            )

        with mock.patch.object(
            jidanpu_upstream, "_pull_claimed_order_pics", side_effect=rebind_during_first_rows_pictures,
        ):
            jidanpu_upstream.sync_my_orders(
                binding=self.row_a, now=now, client=_MyOrdersStub([
                    {"orderId": "old-row-first", "orderSta": "代练中"},
                    {"orderId": "shared-row-second", "orderSta": "已退单", "orderRemark": "旧店备注"},
                ]),
            )

        self.assertIsNotNone(new)
        new.refresh_from_db()
        self.assertEqual((new.status, new.remark), (OrderStatus.UNASSIGNED, ""))
        self.assertEqual(JidanpuUpstreamOrderLink.objects.get(order=new).last_in_at, now - timedelta(seconds=1))

    def test_rebind_during_qr_download_discards_old_picture_and_skips_only_request(self):
        game = Game.objects.create(name="合成群码换绑游戏")
        order = Order.objects.create(game=game, status=OrderStatus.IN_PROGRESS)
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=self.row_a, order=order, upstream_order_id="old-qr-order",
        )
        request, _ = jidanpu_upstream.queue_pull_pics_request(
            order=order, link=link, requested_by=self.user,
        )
        audit = JidanpuSyncEvent.objects.create(
            direction="in", kind=jidanpu_upstream.KIND_PULL_PICS_IN,
            status=JidanpuSyncEvent.Status.FAILED, target_type="order",
            target_ref=order.order_no, payload={"upstream_binding_id": self.row_a.pk},
        )

        def rebind_during_download(_url):
            with _detect(True):
                response = self._put_row(
                    self.row_a.pk, account="synthetic-new-account", enabled=True,
                )
            self.assertEqual(response.json()["code"], 0, response.content)
            return b"\x89PNG\r\n\x1a\n" + b"\0" * 8

        pictures = jidanpu_upstream_pics.RemotePics(
            rows=1, items=(jidanpu_upstream_pics.RemotePic(
                src="https://pics.test.invalid/synthetic-late-old-qr.png", alt="群二维码",
            ),),
        )
        with (
            mock.patch.object(jidanpu_upstream_pics, "fetch_remote_pics", return_value=pictures),
            mock.patch.object(jidanpu_upstream_pics, "download_pic", side_effect=rebind_during_download) as download,
        ):
            handled = jidanpu_upstream.run_pull_pics_requests(
                self.row_a, now=timezone.now(), client=object(),
            )

        self.assertEqual(handled, 1)
        download.assert_called_once()
        request.refresh_from_db()
        audit.refresh_from_db()
        self.assertFalse(OrderScreenshot.objects.filter(order=order).exists())
        self.assertEqual(request.status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertIn(REBIND_REASON_HINT, request.reason)
        self.assertEqual(audit.status, JidanpuSyncEvent.Status.FAILED)

    def test_rebind_before_the_first_claim_request_releases_the_old_reservation(self):
        now = timezone.now()
        mirror = JidanpuUpstreamHallOrder.objects.create(
            owner_binding=self.row_a, upstream_base_url=BASE_URL_A,
            upstream_order_id="old-store-unstarted-claim", first_seen_at=now,
            game=Game.objects.create(name="合成换绑抢单游戏"),
            status=JidanpuUpstreamHallOrder.Status.LISTED,
        )
        sdk = mock.Mock()

        def rebind_while_claim_waits_for_session(_binding):
            with _detect(True):
                response = self._put_row(
                    self.row_a.pk, jidanpu_base_url="https://new-claim-store.test.invalid",
                    account="synthetic-new-claim-account", enabled=True,
                )
            self.assertEqual(response.json()["code"], 0, response.content)
            return nullcontext(sdk)

        with mock.patch.object(jidanpu, "claim_session", side_effect=rebind_while_claim_waits_for_session):
            with self.assertRaises(jidanpu_upstream.ClaimError):
                jidanpu_upstream.claim_upstream_mirror(
                    user_id=self.user.pk, mirror_id=mirror.pk, now=now,
                    enforce_deposit=False, enforce_limits=False,
                )

        sdk.call.assert_not_called()
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNone(mirror.claiming_by_id)
        self.assertIsNone(mirror.claiming_at)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(kind=jidanpu_upstream.KIND_HALL_CLAIM).status, "skipped",
        )
        self.assertFalse(Order.objects.exists())

    def test_rebind_before_the_first_lease_releases_only_an_unsent_old_reservation(self):
        now = timezone.now()
        game = Game.objects.create(name="合成换绑抢单租约游戏")
        with _detect(True):
            successor = self._put_row(
                self.row_b.pk, jidanpu_base_url=BASE_URL_A, account=ACCOUNT_B, enabled=True,
            )
        self.assertEqual(successor.json()["code"], 0, successor.content)
        mirror = JidanpuUpstreamHallOrder.objects.create(
            owner_binding=self.row_a, upstream_base_url=BASE_URL_A,
            upstream_order_id="old-store-unleased-claim", first_seen_at=now, game=game,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
        )
        lease = jidanpu_common.execution_lease

        def rebind_before_acquiring_lease(event, **kwargs):
            self.assertFalse(event.payload["send_started"])
            with _detect(True):
                response = self._put_row(
                    self.row_a.pk, jidanpu_base_url="https://new-claim-store.test.invalid",
                    account="synthetic-new-claim-account", enabled=True,
                )
            self.assertEqual(response.json()["code"], 0, response.content)
            return lease(event, **kwargs)

        with (
            mock.patch.object(jidanpu_common, "execution_lease", side_effect=rebind_before_acquiring_lease),
            mock.patch.object(jidanpu, "claim_session") as session,
            self.assertRaises(jidanpu_upstream.ClaimError),
        ):
            jidanpu_upstream.claim_upstream_mirror(
                user_id=self.user.pk, mirror_id=mirror.pk, now=now,
                enforce_deposit=False, enforce_limits=False,
            )

        session.assert_not_called()
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(mirror.claiming_by_id)
        self.assertIsNone(mirror.claiming_at)
        self.assertEqual(
            JidanpuSyncEvent.objects.get(kind=jidanpu_upstream.KIND_HALL_CLAIM).status, "skipped",
        )

    def test_worker_releases_only_the_unsent_reservation_from_an_old_binding(self):
        game = Game.objects.create(name="合成换绑后台抢单游戏")
        reserved_at = timezone.now()
        mirrors = [
            JidanpuUpstreamHallOrder.objects.create(
                owner_binding=self.row_a, upstream_base_url=BASE_URL_A,
                upstream_order_id=f"old-store-worker-claim-{index}", first_seen_at=reserved_at,
                game=game, status=JidanpuUpstreamHallOrder.Status.CLAIMING,
                claiming_by=self.user, claiming_at=reserved_at,
            )
            for index in range(3)
        ]
        unsent, started, replaced = mirrors
        events = [
            jidanpu_upstream.queue_hall_claim(mirror, taker_id=self.user.pk)
            for mirror in mirrors
        ]
        JidanpuSyncEvent.objects.filter(pk=events[1].pk).update(
            payload={**events[1].payload, "send_started": True},
        )
        JidanpuUpstreamHallOrder.objects.filter(pk=replaced.pk).update(
            claiming_at=reserved_at + timedelta(seconds=1),
        )
        with _detect(True):
            response = self._put_row(
                self.row_a.pk, jidanpu_base_url="https://new-worker-claim-store.test.invalid",
                account="synthetic-new-worker-claim-account", enabled=True,
            )
        self.assertEqual(response.json()["code"], 0, response.content)
        self.row_a.refresh_from_db()
        # The rebind scan can miss an old reservation that commits after its search.
        JidanpuSyncEvent.objects.filter(pk__in=[event.pk for event in events]).update(
            status="pending", reason="", processed_at=None,
        )
        sdk = mock.Mock()

        result = jidanpu_upstream.run_outbox(
            self.row_a, now=reserved_at + timedelta(seconds=2), client=sdk,
        )

        self.assertEqual(result.skipped, 3)
        sdk.call.assert_not_called()
        unsent.refresh_from_db()
        self.assertEqual(unsent.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNone(unsent.claiming_by_id)
        self.assertIsNone(unsent.claiming_at)
        for mirror in (started, replaced):
            mirror.refresh_from_db()
            self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
            self.assertEqual(mirror.claiming_by_id, self.user.pk)

    def test_an_edit_validated_before_another_rebind_cannot_overwrite_it(self):
        game = Game.objects.create(name="合成设置并发游戏")
        winner_order = Order.objects.create(game=game, status=OrderStatus.IN_PROGRESS)

        def detect_after_new_account_is_saved(_candidate):
            with _detect(True):
                winner = self._put_row(
                    self.row_a.pk, account="synthetic-winning-account", enabled=True,
                )
            self.assertEqual(winner.json()["code"], 0, winner.content)
            JidanpuUpstreamOrderLink.objects.create(
                binding_id=self.row_a.pk, order=winner_order, upstream_order_id="winner-order",
            )
            return jidanpu_upstream.DetectionReport(ok=True)

        with mock.patch.object(
            jidanpu_upstream, "detect_binding", side_effect=detect_after_new_account_is_saved,
        ):
            stale = self._put_row(
                self.row_a.pk, account="synthetic-stale-edit-account", enabled=True,
            )

        self.assertEqual(stale.json()["code"], ErrorCode.VALIDATION, stale.content)
        self.assertIn("刷新", stale.json()["msg"])
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.account, "synthetic-winning-account")
        self.assertTrue(JidanpuUpstreamOrderLink.objects.filter(order=winner_order).exists())

    def test_edit_with_detection_failure_leaves_the_row_untouched(self):
        """⭐ 钉③（编辑支）：检测不过 ⇒ 目标行原样（账号、开关、`checked_at` 全是旧值）。"""
        self.row_a.checked_at = None
        self.row_a.save(update_fields=["checked_at"])
        logs_before = OperationLog.objects.count()
        with _detect(False, problems=(LOGIN_FAILURE,)):
            res = self._put_row(self.row_a.pk, account="kefu-should-not-land", enabled=True)
        self.assertNotEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.account, ACCOUNT)
        self.assertIs(self.row_a.enabled, True)
        self.assertIsNone(self.row_a.checked_at)
        self.assertEqual(OperationLog.objects.count(), logs_before)

    def test_edit_keeps_the_row_owner_and_blank_password_keeps_the_old_one(self):
        """编辑不改「录入人」（D-0256：`user` 列 = 录入人）；密码留空 = 不改。"""
        with _detect(True):
            res = self._put_row(self.row_a.pk, password="", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.user_id, self.user.pk)
        self.assertEqual(self.row_a.password, PASSWORD)

    def test_edit_does_not_clobber_health_columns_written_concurrently(self):
        """收窄写入照 `_PUT_WRITE_FIELDS` 口径：检测那次网络往返期间写的健康列不被打回旧值。"""
        stamped = timezone.now()

        def detect_and_stamp(*args, **kwargs):
            JidanpuUpstreamBinding.objects.filter(pk=self.row_a.pk).update(
                last_ok_at=stamped
            )
            return jidanpu_upstream.DetectionReport(ok=True)

        with mock.patch.object(jidanpu_upstream, "detect_binding",
                               side_effect=detect_and_stamp):
            res = self._put_row(self.row_a.pk, enabled=True, account="kefu-up-row-stamped")
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.last_ok_at, stamped)
        self.assertEqual(self.row_a.account, "kefu-up-row-stamped")

    def test_a_credential_change_on_edit_converges_only_that_rows_in_flight_events(self):
        """换凭据（改地址）⇒ 只收敛**这一行**的在飞 outbox 事件（`skipped` + 人话），并清 `checked_at`。

        ⚠️ 换绑判据与既有 member PUT **同一份**：停用下的换绑才把 `checked_at` 清成
        `None`（启用的换绑会立刻写成刚才那次检测的时刻——既有语义，不在这条里重判）。
        """
        pending = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", target_type="order",
            target_ref="20261018000000000001",
            status=JidanpuSyncEvent.Status.PENDING,
            payload={"upstream_binding_id": self.row_a.pk},
        )
        other_event = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", target_type="order",
            target_ref="20261018000000000001",
            status=JidanpuSyncEvent.Status.PENDING,
            payload={"upstream_binding_id": self.row_b.pk},
        )
        res = self._put_row(self.row_a.pk, jidanpu_base_url=BASE_URL_B)
        self.assertEqual(res.json()["code"], 0, res.content)
        pending.refresh_from_db()
        other_event.refresh_from_db()
        self.assertEqual(pending.status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertIn(REBIND_REASON_HINT, pending.reason)
        self.assertEqual(other_event.status, JidanpuSyncEvent.Status.PENDING)
        self.row_a.refresh_from_db()
        self.assertIsNone(self.row_a.checked_at, "换了凭据，旧检测结论不再成立")
        # 夹具那两笔「创建」也留痕 ⇒ 取**最后一次**（这次编辑的 update）来钉。
        log = OperationLog.objects.order_by("id").last()
        self.assertEqual(log.action, UPDATE_ACTION)
        self.assertEqual(log.target_id, str(self.row_a.pk))

    def test_editing_an_unknown_pk_is_a_named_business_error(self):
        logs_before = OperationLog.objects.count()
        res = self._put_row(99999999)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["code"], ErrorCode.BUSINESS)
        self.assertEqual(res.json()["msg"], "这个绑定不存在")
        self.assertEqual(OperationLog.objects.count(), logs_before)

    # ── ⭐ 人填的「上家系统名称」（`name`）────────────────────────

    def test_put_name_persists_to_the_db_row(self):
        """⭐ 行级 PUT 改 `name` ⇒ 那一行的 `name` **真变**（判据打数据库行，
        照 `test_pin7` 的口径——`_PUT_WRITE_FIELDS` 漏了 `name` 只有这条网能红，
        E-0027 家族：校验通过、内存有值、落库被静默丢弃）。"""
        self.row_a.name = "旧名称甲"
        self.row_a.save(update_fields=["name"])
        # ⚠️ 载荷带非空密码（`_payload` 的形状）⇒ 照旧跑检测（凭据域闸），桩放行。
        with _detect(True):
            res = self._put_row(self.row_a.pk, name="初音网游", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.name, "初音网游")
        self.assertEqual(res.json()["data"]["name"], "初音网游")

    def test_put_empty_name_clears_it(self):
        """`name` 是**可回显**的可见格 ⇒ 空串 = **清除**（与 `password` 的「空串 = 不改」
        不是一套语义——那一格不回显，本格回显，整表单重发时空串就是用户清空了名称）。"""
        self.row_a.name = "旧名称甲"
        self.row_a.save(update_fields=["name"])
        with _detect(True):
            res = self._put_row(self.row_a.pk, password="", name="", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.name, "")

    def test_name_only_edit_skips_detection_and_is_not_a_rebind(self):
        """仅改 `name`（凭据原值重发 + 密码空串 = 不改）⇒ **免检测**、不盖新 `checked_at`
        ——`name` 不在凭据域（改名字不是换绑，在飞事件不收敛），照 D-0261「仅改来源」
        的同一判据。"""
        stamped = timezone.now()
        self.row_a.checked_at = stamped
        self.row_a.save(update_fields=["checked_at"])
        pending = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", target_type="order",
            target_ref="20261029000000000001",
            status=JidanpuSyncEvent.Status.PENDING,
            payload={"upstream_binding_id": self.row_a.pk},
        )
        with _detect(True) as detect:
            res = self.client.put(
                ROW_DETAIL_URL.format(pk=self.row_a.pk),
                json.dumps({
                    "jidanpu_base_url": BASE_URL_A,
                    "account": ACCOUNT,
                    "password": "",
                    "name": "初音网游",
                }),
                content_type="application/json",
            )
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(detect.called, "只改名称不该跑登录检测")
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.name, "初音网游")
        self.assertEqual(self.row_a.checked_at, stamped, "没跑检测就不许盖新的检测时刻")
        pending.refresh_from_db()
        self.assertEqual(pending.status, JidanpuSyncEvent.Status.PENDING,
                         "改名不是换绑，在飞事件不许被收敛")

    def test_put_account_name_is_still_read_only(self):
        """`account_name`（同步引擎缓存）在行级 PUT 上照旧**只读**：塞进请求体 ⇒ 静默忽略。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.row_a.pk).update(
            account_name="同步引擎写的显示名"
        )
        with _detect(True):
            res = self._put_row(self.row_a.pk, account_name="塞进来的显示名", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.account_name, "同步引擎写的显示名")

    # ── ⭐ D-0261：行级 PUT 暴露「订单来源」（计划钉 ⑦ + 审阅 A / B / F）──────────

    def test_pin7_put_order_source_id_persists_to_the_db_row(self):
        """⭐ 钉⑦（审阅 C）：行级 PUT 带 `order_source_id` ⇒ 那一行的 `order_source_id` **真变**。

        ⚠️ 判据打**数据库行**（`refresh_from_db()`）——审阅 A 点名的「校验通过、内存有值、
        落库被 `_PUT_WRITE_FIELDS` 静默丢弃」（E-0027 家族）只有这条网能红。
        ⚠️ 夹具载荷带凭据域（`_payload` 的形状）⇒ 照旧跑检测（审阅 B 的闸条件），桩放行。
        """
        source = OrderSource.objects.create(name="竹屿")
        with _detect(True):
            res = self._put_row(self.row_a.pk, order_source_id=source.pk, enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.order_source_id, source.pk)

    def test_put_order_source_null_clears_it(self):
        """PUT `order_source_id: null` ⇒ **清除**（计划 §4 的口径）；省略键 = 不动。"""
        source = OrderSource.objects.create(name="竹屿")
        self.row_a.order_source = source
        self.row_a.save(update_fields=["order_source"])
        with _detect(True):
            res = self._put_row(self.row_a.pk, order_source_id=None, enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertIsNone(self.row_a.order_source_id)

    def test_put_with_an_unknown_source_id_is_a_field_error_not_a_500(self):
        """不存在的来源主键 ⇒ 序列化器点名拒绝（业务码），⛔ 不落库、⛔ 不 500（E-0010）。"""
        with _detect(True):
            res = self._put_row(self.row_a.pk, order_source_id=99999999, enabled=True)
        self.assertNotEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertIsNone(self.row_a.order_source_id)

    def test_source_only_edit_on_an_enabled_row_skips_detection_and_keeps_checked_at(self):
        """⭐ 审阅 B：仅改来源 ⇒ **免检测**、⛔ 不盖 `checked_at` 假健康戳。

        ⚠️ 载荷照页面弹窗的**真实形状**（整表单重发：地址 / 账号是**原值**、密码空串 = 不改）
        ——⛔ 不能只发 `{order_source_id}`：地址 / 账号是序列化器必填格，最小载荷连校验都过不了
        （执行期先红实测：`1000 该字段是必填项`）。「仅改来源」的判据因此落在**凭据域没被
        真改写**（值与库里一致 / 密码留空）上，不是「键不在载荷里」。
        ⚠️ 盖了新 `checked_at` 就是**假健康信号**（那次保存根本没跑过检测）——闸与盖戳同开同关。
        """
        stamped = timezone.now()
        self.row_a.checked_at = stamped
        self.row_a.save(update_fields=["checked_at"])
        source = OrderSource.objects.create(name="竹屿")
        with _detect(True) as detect:
            res = self.client.put(
                ROW_DETAIL_URL.format(pk=self.row_a.pk),
                json.dumps({
                    "jidanpu_base_url": BASE_URL_A,
                    "account": ACCOUNT,
                    "password": "",
                    "order_source_id": source.pk,
                }),
                content_type="application/json",
            )
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(detect.called, "凭据原值重发 + 只改来源，不该跑登录检测")
        self.row_a.refresh_from_db()
        self.assertEqual(self.row_a.order_source_id, source.pk)
        self.assertEqual(self.row_a.checked_at, stamped, "没跑检测就不许盖新的检测时刻")

    def test_enabling_with_a_source_change_still_runs_detection(self):
        """⭐ 审阅 B 的漏放组合：「停用行 + 改来源 + enabled=true（凭据原值重发）」⇒ 检测**必须**跑。

        只判「凭据被改写」的闸会漏放这一支——启用必须过检测（spec §9.2）；
        检测不过 ⇒ **一行都不落**（来源也不许落）。
        """
        source = OrderSource.objects.create(name="竹屿")
        JidanpuUpstreamBinding.objects.filter(pk=self.row_a.pk).update(enabled=False)
        with _detect(False, problems=(LOGIN_FAILURE,)) as detect:
            res = self.client.put(
                ROW_DETAIL_URL.format(pk=self.row_a.pk),
                json.dumps({
                    "jidanpu_base_url": BASE_URL_A,
                    "account": ACCOUNT,
                    "password": "",
                    "order_source_id": source.pk,
                    "enabled": True,
                }),
                content_type="application/json",
            )
        self.assertTrue(detect.called, "「启用＋改来源」必须照跑检测")
        self.assertNotEqual(res.json()["code"], 0, res.content)
        self.row_a.refresh_from_db()
        self.assertIs(self.row_a.enabled, False)
        self.assertIsNone(self.row_a.order_source_id)


class RowReadTests(RowWriteFixtureMixin, TenantTestCase):
    """`GET /api/business/upstream-bindings/jidanpu/<pk>/`（行级读：编辑弹窗的回填出口）。"""

    def test_get_returns_the_row_with_its_configuration_columns(self):
        with _detect(True):
            created = self._post_row(
                jidanpu_base_url=BASE_URL_A, hall_price_percent=50, enabled=True
            )
        pk = created.json()["data"]["id"]
        res = self._get_row(pk)
        self.assertEqual(res.json()["code"], 0, res.content)
        data = res.json()["data"]
        self.assertEqual(data["jidanpu_base_url"], BASE_URL_A)
        self.assertEqual(data["account"], ACCOUNT)
        self.assertEqual(data["hall_price_percent"], 50)
        self.assertIs(data["enabled"], True)
        # 凭据红线：密码只回「配没配」。
        self.assertIs(data["password_set"], True)
        self.assertNotIn("password", data)

    def test_get_returns_order_source_as_id_and_name(self):
        """⭐ D-0261（审阅 F）：行级 GET 回 `order_source` = `{id, 名字}`；未配 ⇒ `null`；
        写入键 `order_source_id` 是 `write_only`，响应里**不**回（形状恰好一组）。"""
        source = OrderSource.objects.create(name="叶枫")
        with _detect(True):
            created = self._post_row(order_source_id=source.pk, enabled=True)
        self.assertEqual(created.json()["code"], 0, created.content)
        data = self._get_row(created.json()["data"]["id"]).json()["data"]
        self.assertEqual(data["order_source"], {"id": source.pk, "name": "叶枫"})
        self.assertNotIn("order_source_id", data)

    def test_get_returns_order_source_null_when_unset(self):
        with _detect(True):
            created = self._post_row(enabled=True)
        data = self._get_row(created.json()["data"]["id"]).json()["data"]
        self.assertIsNone(data["order_source"])

    def test_get_of_an_unknown_pk_is_a_named_business_error(self):
        res = self._get_row(99999999)
        self.assertEqual(res.json()["code"], ErrorCode.BUSINESS)
        self.assertEqual(res.json()["msg"], "这个绑定不存在")


class RowRbacTests(RowWriteFixtureMixin, TenantTestCase):
    """⭐ 钉④：行级**四个动作**的节点门（fail-closed）：没有 `system.bind_upstream_system` 一律 403。"""

    def _enabled_row(self) -> JidanpuUpstreamBinding:
        with _detect(True):
            res = self._post_row(enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        return JidanpuUpstreamBinding.objects.get(jidanpu_base_url=BASE_URL_A)

    def test_all_four_row_actions_are_403_without_the_node_and_write_nothing(self):
        row = self._enabled_row()
        # 换成无节点的人（自证登进来了，403 不是「没登录」凑的）。
        self.assertEqual(self.other_client.get("/api/auth/me").status_code, 200)
        before_rows = JidanpuUpstreamBinding.objects.count()
        # 建夹具那笔创建留痕不算在被拒动作头上：判据是「被拒的四个动作**不再新增**」。
        logs_before = OperationLog.objects.count()
        cases = (
            ("post", ROW_CREATE_URL, _payload()),
            ("get", ROW_DETAIL_URL.format(pk=row.pk), None),
            ("put", ROW_DETAIL_URL.format(pk=row.pk), _payload()),
            ("delete", ROW_DETAIL_URL.format(pk=row.pk), None),
        )
        for method, url, body in cases:
            with self.subTest(method=method):
                if body is None:
                    resp = getattr(self.other_client, method)(url)
                else:
                    resp = getattr(self.other_client, method)(
                        url, json.dumps(body), content_type="application/json"
                    )
                self.assertEqual(resp.status_code, 403, (method, resp.content[:200]))
                self.assertEqual(resp.json()["code"], 403)
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), before_rows)
        self.assertEqual(OperationLog.objects.count(), logs_before)


class RowListIntegrationTests(RowWriteFixtureMixin, TenantTestCase):
    """⭐ 钉⑤：行级建出的两条都在**统一列表**里（多行真的能在页面上看到）。"""

    def test_the_unified_list_contains_every_created_row(self):
        LIST_URL = "/api/business/upstream-bindings/"
        with _detect(True):
            first = self._post_row(jidanpu_base_url=BASE_URL_A, enabled=True)
            second = self._post_row(jidanpu_base_url=BASE_URL_B, enabled=True)
        resp = self.client.get(LIST_URL)
        items = resp.json()["data"]["items"]
        ids = [item["id"] for item in items]
        self.assertIn(first.json()["data"]["id"], ids)
        self.assertIn(second.json()["data"]["id"], ids)
        self.assertEqual(resp.json()["data"]["total"], 2)


class RowMemberPutCompatTests(RowWriteFixtureMixin, TenantTestCase):
    """⭐ 钉⑥：既有 member PUT **兼容保留**（D-0256），语义一字不改。"""

    def test_pin6_member_put_still_creates_and_updates_the_users_single_row(self):
        """member PUT 仍是「本人那一行」的单行资源：没有行就建、有就改。"""
        with _detect(True):
            created = self._member_put(enabled=True)
        self.assertEqual(created.json()["code"], 0, created.content)
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertEqual(row.jidanpu_base_url, BASE_URL_A)
        with _detect(True):
            updated = self._member_put(account="kefu-up-row-next", enabled=True)
        self.assertEqual(updated.json()["code"], 0, updated.content)
        # 仍是同一行（覆盖），没有多出新行——这是「兼容保留」的定义。
        self.assertEqual(JidanpuUpstreamBinding.objects.filter(user=self.user).count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.account, "kefu-up-row-next")

    def test_the_legacy_member_put_overwrite_is_the_bug_e0255_recorded(self):
        """⭐ 钉① 的**修前红存证**（E-0255 的机制）：同账号走 member PUT 连存两条不同地址
        ⇒ 第二笔**静默覆盖**第一笔（生产 suqing 2026-10-18 的现场）。

        ⚠️ 这条钉的是**兼容端点自己的单行语义**（D-0256 保留它），⛔ 不是新世界的行为；
        页面已改走行级端点，这条覆盖语义不再有页面入口。
        """
        with _detect(True):
            first = self._member_put(jidanpu_base_url=BASE_URL_A, enabled=True)
            second = self._member_put(jidanpu_base_url=BASE_URL_B, enabled=True)
        self.assertEqual(first.json()["code"], 0, first.content)
        self.assertEqual(second.json()["code"], 0, second.content)
        rows = JidanpuUpstreamBinding.objects.filter(user=self.user)
        self.assertEqual(rows.count(), 1, "member PUT 的单行覆盖语义变了（兼容口径被回归）")
        self.assertEqual(rows.get().jidanpu_base_url, BASE_URL_B)

    def test_member_put_detection_failure_still_writes_nothing(self):
        """兼容端点的检测闸照旧（复用同一份闸，失败不留半行）。"""
        with _detect(False, problems=(LOGIN_FAILURE,)):
            res = self._member_put(enabled=True)
        self.assertNotEqual(res.json()["code"], 0, res.content)
        self.assertIn(LOGIN_FAILURE, res.json()["msg"])
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), 0)

    def test_pin5_member_put_silently_ignores_order_source(self):
        """⭐ 钉⑤（审阅 D）：member PUT（兼容路径）塞 `order_source` / `order_source_id`
        ⇒ **被静默忽略**：`code 0`、库里不改、响应与 member GET 都**没有**这两个键。

        ⚠️ member 与行级三支**共用**一份序列化器 ⇒ 新字段加进去后，兼容路径「不带此字段」
        必须是**显式动作**（D-0261 的收窄子类），这条钉就是那个动作的反向证明。
        """
        with _detect(True):
            res = self._member_put(enabled=True, order_source="竹屿", order_source_id=999999)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertIsNone(row.order_source_id, "兼容路径不许写来源")
        self.assertNotIn("order_source", res.json()["data"])
        self.assertNotIn("order_source_id", res.json()["data"])
        member_get = self.client.get(MEMBER_PUT_URL).json()["data"]
        self.assertNotIn("order_source", member_get)
        self.assertNotIn("order_source_id", member_get)

    def test_member_put_silently_ignores_name(self):
        """⭐ 2026-10-29（照钉⑤ 的同一处置）：member PUT（兼容路径）塞 `name`
        ⇒ **被静默忽略**：`code 0`、库里不落、响应与 member GET 都**没有**这个键。

        ⚠️ `name`（人填的上家系统名称）是行级世界的新格——兼容路径的口径照 D-0256
        「语义一字不改」：收窄子类显式剔除了它，这条钉就是那个动作的反向证明。
        """
        with _detect(True):
            res = self._member_put(enabled=True, name="初音网游")
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertEqual(row.name, "", "兼容路径不许写人填名称")
        self.assertNotIn("name", res.json()["data"])
        member_get = self.client.get(MEMBER_PUT_URL).json()["data"]
        self.assertNotIn("name", member_get)
