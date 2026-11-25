import json
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import unquote

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_push, jidanpu, jidanpu_assignment_lock, jidanpu_direct_taker, jidanpu_member
from apps.business.api.jidanpu_member_views import _converge_in_flight_events
from apps.business.models import (
    EmpType, Game, JidanpuMemberBinding, JidanpuMemberOrderLink, JidanpuSyncEvent, Order, OrderScreenshot, OrderStatus, Performance,
)
from apps.platform.provisioning import provision_tenant
from jidanpu.errors import ApiError, AuthError, RateLimitError, SessionExpiredError, TransportError, UpstreamBlockedError
from tests.p5_auth_helpers import make_logged_in_client


DOMAIN = "tenant.test.com"
OTHER_DOMAIN = "jidanpu-direct-other.test.local"
ORDER_NO = "20261118000000000001"
REMOTE_NO = "20261118000000000081"
OLD_NUMBER = "9010405060708090101"
NEW_NUMBER = "9010405060708090102"


class Downstream:
    def __init__(self):
        self.last_envelope = None
        self.current = {"userId": OLD_NUMBER, "userName": "打手旧", "fee": "3.50"}
        self.default_fee = "16.50"
        self.default_fee_after_assign = None
        self.remote_no = REMOTE_NO
        self.remote_oid = 123
        self.remote_recorded = True
        self.price = "23.45"
        self.in_hall = False
        self.remote_status = None
        self.game_name = "测试鸡蛋铺游戏"
        self.before_release = None
        self.before_assign = None
        self.lose_recording_response = False
        self.login_uid = "671"
        self.login_name = "合成录单客服"
        self.recorded_by = self.login_uid
        self.ignore_recorder_filter = False
        self.calls = []
        self.result = "success"
        self.rejection_message = "鸡蛋铺拒绝了这次指派"
        self.change_on_order_read = None
        self.savepoint_depth = None

    def call(self, method, path, **kwargs):
        if self.savepoint_depth != len(connection.savepoint_ids):
            raise AssertionError("鸡蛋铺 HTTP 调用不能持有指派事务的行锁")
        self.calls.append((method, path, kwargs))
        if path == jidanpu.LOGIN_USER_INFO_PATH:
            self.last_envelope = {"code": 0}
            return {"Id": self.login_uid, "Name": self.login_name}
        if path == jidanpu.SOURE_LIST_PATH:
            self.last_envelope = {"code": 0}
            return [{"name": "S-团团通系统", "value": "团团通系统"}]
        if path == jidanpu.ADD_ORDER_PATH:
            if self.remote_recorded:
                raise AssertionError("不应重复录单")
            order = json.loads(unquote(kwargs["data"]["strParameter"]))
            self.price = order["price"]
            self.remote_recorded = True
            if self.lose_recording_response:
                raise ApiError("合成录单回包异常", code=500)
            self.last_envelope = {"code": 123}
            return ""
        if path == jidanpu.ORDER_LIST_PATH:
            if self.change_on_order_read:
                self.current = self.change_on_order_read
            if not self.remote_recorded:
                self.last_envelope = {"code": 0, "count": 0, "msg": ""}
                return []
            if kwargs["data"].get("kefuType") == 5 and not self.ignore_recorder_filter:
                if kwargs["data"]["customerServiceUserId"] != self.recorded_by:
                    self.last_envelope = {"code": 0, "count": 0}
                    return []
            self.last_envelope = {"code": 0, "count": 1, "msg": ""}
            return [{
                "orderId": self.remote_no, "orderIdView": "SOURCE-1", "oid": self.remote_oid,
                "recManName": self.current["userName"] if self.current else "",
                "orderSta": self.remote_status or ("代练中" if self.current else "未分配"),
                "isThrowOrder": "Y" if self.in_hall else "N",
                "gameName": self.game_name,
                "recFee": self.default_fee, "price": self.price, "hours": "72", "project": "测试项目",
            }]
        if path == jidanpu.ASS_LOGS_PATH:
            self.last_envelope = {"code": 0}
            return [self.current] if self.current else []
        if path == jidanpu.ASSIGNABLE_USERS_PATH:
            self.last_envelope = {"code": 0, "count": 3}
            rows = [
                {"id": OLD_NUMBER, "userName": "打手旧", "isValid": "Y"},
                {"id": "111", "userName": "打手新停用", "isValid": "N"},
                {"id": int(NEW_NUMBER), "userName": "打手新", "isValid": "Y"},
            ]
            if key := kwargs["query"]["key"]:
                rows = [item for item in rows if key in item["userName"]]
                self.last_envelope["count"] = len(rows)
            page = kwargs["query"]["page"]
            return rows[(page - 1) * 2:page * 2]
        if path == jidanpu.RELEASE_TO_HOME_PATH:
            if self.before_release:
                self.before_release()
            if self.in_hall:
                self.in_hall = False
                self.default_fee = "0.00"
            self.last_envelope = {"code": 1}
            return ""
        if path == jidanpu.ASSIGN_PATH:
            if self.in_hall:
                raise AssertionError("指派前须确认原单已离开鸡蛋铺大厅")
            if self.before_assign:
                self.before_assign()
            if self.result == "lost":
                raise TransportError("合成写响应缺失")
            if self.result == "rejected":
                self.last_envelope = {"code": 0, "msg": self.rejection_message}
                return ""
            if self.result == "unexpected":
                self.last_envelope = {"code": 2, "msg": "合成未知回包"}
                return ""
            row = json.loads(kwargs["data"]["strParameter"])[0]
            self.current = {"userId": row["userId"], "userName": row["userName"], "fee": row["fee"]}
            if self.default_fee_after_assign is not None:
                self.default_fee = self.default_fee_after_assign
            if self.result == "lost_after_apply":
                raise TransportError("合成已指派但回执缺失")
            self.last_envelope = {"code": 1}
            return ""
        raise AssertionError(f"未预期鸡蛋铺调用 {method} {path}")


class JidanpuDirectTakerTests(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        provision_tenant(schema_name="jidanpu_direct_other", name="对照工作室", domain=OTHER_DOMAIN)

    def setUp(self):
        game = Game.objects.create(name="测试游戏")
        self.client = make_logged_in_client(
            username="direct-customer-service", groups=("测试客服",), nodes=("order.assign_taker", "order.clear_taker"),
        )
        self.order = Order.objects.create(
            order_no=ORDER_NO, source_order_no="SOURCE-1", game=game, status=OrderStatus.IN_PROGRESS,
            project_text="测试项目", total_price=Decimal("999"), hall_price=Decimal("23.45"),
        )
        self.owner_client = make_logged_in_client(username="direct-jidanpu-studio")
        studio = get_user_model().objects.get(username="direct-jidanpu-studio")
        self.performance = Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=studio)
        self.binding = JidanpuMemberBinding.objects.create(
            user=studio, jidanpu_base_url="https://downstream.test.invalid", account="test", password="placeholder", enabled=True,
        )
        self.link = JidanpuMemberOrderLink.objects.create(binding=self.binding, order=self.order, jidanpu_order_id=REMOTE_NO)
        self.path = f"/api/business/orders/{ORDER_NO}/jidanpu-direct-assignment/"
        self.downstream = Downstream()
        self.bound_sessions = {self.binding.pk: self.downstream}

        @contextmanager
        def session(binding):
            downstream = self.bound_sessions[binding.pk]
            downstream.savepoint_depth = len(connection.savepoint_ids)
            yield downstream, None

        for replacement in (patch.object(jidanpu, "member_session", side_effect=session),
                            patch.object(jidanpu, "ASSIGNABLE_USERS_PAGE_SIZE", 2)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_unassigned_order_can_choose_a_jidanpu_studio(self):
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])
        self.performance.user = None
        self.performance.save(update_fields=["user"])

        data = self.client.get(self.path).json()["data"]
        self.assertEqual(data["studios"], [{"id": self.binding.pk, "name": "direct-jidanpu-studio"}])
        self.assertEqual(data["price"], "23.45")
        self.assertTrue(data["assignable"])
        self.assertEqual(self.downstream.calls, [])

    def test_first_search_uses_only_the_selected_eligible_studio(self):
        self.prepare_first()
        for username, games in (("second-jidanpu", [self.order.game]), ("outside-game", [])):
            make_logged_in_client(username=username, games=games)
            JidanpuMemberBinding.objects.create(
                user=get_user_model().objects.get(username=username),
                jidanpu_base_url=f"https://{username}.test.invalid", account=username,
                password="placeholder", enabled=True,
            )
        second = JidanpuMemberBinding.objects.get(user__username="second-jidanpu")
        outside_game = JidanpuMemberBinding.objects.get(user__username="outside-game")
        self.bound_sessions[second.pk] = second_client = Downstream()

        options = self.client.get(self.path).json()["data"]
        self.assertEqual([studio["id"] for studio in options["studios"]], [self.binding.pk, second.pk])
        self.assertNotEqual(self.post(self.path + "search/", query=NEW_NUMBER, studio_id=outside_game.pk)["code"], 0)
        found = self.post(self.path + "search/", query=NEW_NUMBER, studio_id=second.pk)["data"]
        self.assertEqual(found["items"], [{"number": NEW_NUMBER, "name": "打手新"}])
        self.assertTrue(second_client.calls)
        self.assertEqual(self.downstream.calls, [])
        request = self.first_request()
        request["studio_token"] = found["studio_token"]
        self.assertIn("绑定已变化", self.post(**request)["msg"])
        self.assertFalse(self.writes())

    def prepare_first(self, *, status=OrderStatus.UNASSIGNED, no_link=True):
        self.order.status = status
        self.order.save(update_fields=["status"])
        self.performance.user = None
        self.performance.save(update_fields=["user"])
        self.binding.input_order_kefu = "671"
        self.binding.save(update_fields=["input_order_kefu"])
        self.client = make_logged_in_client(
            username="first-dispatch-editor", groups=("测试客服",),
            nodes=("order.assign_taker", "order.submit_payout_price", "order.clear_taker"),
        )
        self.downstream.current = None
        self.downstream.default_fee = None
        if no_link:
            self.link.delete()
            self.downstream.remote_recorded = False

    def first_request(self, **overrides):
        response = self.post(self.path + "search/", query="打手新", studio_id=self.binding.pk)
        self.assertEqual(response["code"], 0, response)
        search = response["data"]
        self.assertEqual(search["items"], [{"number": NEW_NUMBER, "name": "打手新"}])
        return {
            "studio_id": self.binding.pk, "studio_token": search["studio_token"], "number": NEW_NUMBER, "name": "打手新",
            "expected_number": "", "expected_fee": None, "expected_price": "23.45",
            "fee": "12.34", **overrides,
        }

    def prepare_listed_first(self):
        self.prepare_first(status=OrderStatus.WAITING_GRAB, no_link=False)
        self.link.in_hall = True
        self.link.save(update_fields=["in_hall"])
        self.downstream.in_hall = True
        self.downstream.default_fee = "16.50"

    def test_queued_first_assignment_does_not_wait_for_unrelated_outbox(self):
        self.prepare_first()
        operation_id = "b7c43f68-7385-4a70-92be-5f066ad72c67"
        request = self.first_request(operation_id=operation_id)
        self.downstream.calls.clear()

        @contextmanager
        def busy_outbox(_binding):
            yield False

        with patch.object(jidanpu_member, "_outbox_guard", side_effect=busy_outbox):
            accepted = self.post(**request)
            self.assertEqual(accepted["code"], 0, accepted)
            self.assertEqual(accepted["data"]["operation_status"], "working")
            self.assertEqual(self.downstream.calls, [])
            self.assertEqual(self.post(**request)["data"]["pending"], accepted["data"]["pending"])
            conflict = self.post(**{**request, "number": OLD_NUMBER})
            self.assertEqual(conflict["data"], {"operation_conflict": True})
            self.assertIn("原指派不同", conflict["msg"])
            from apps.business import jidanpu_direct_queue

            jidanpu_direct_queue.run_pending()
        completed = self.client.get(self.path, {"operation_id": operation_id}).json()["data"]
        self.assertEqual(completed["operation_status"], "done", completed)
        self.assertEqual(completed["current"]["number"], NEW_NUMBER)
        self.assertEqual(len(self.writes()), 1)

    def test_queued_first_assignment_completes_from_success_receipt_without_remote_reads(self):
        from apps.business import jidanpu_direct_queue

        self.prepare_first()
        request = self.first_request(operation_id="707aab61-2e05-4d2f-9f0e-657b0f86ad93")
        self.downstream.login_name = ""
        self.downstream.calls.clear()
        accepted = self.post(**request)["data"]
        self.assertEqual(accepted["operation_status"], "working")

        jidanpu_direct_queue.run_pending()

        paths = [path for _, path, _ in self.downstream.calls]
        self.assertEqual(paths[paths.index(jidanpu.ASSIGN_PATH):], [jidanpu.ASSIGN_PATH])
        self.assertEqual(paths[paths.index(jidanpu.ADD_ORDER_PATH):].count(jidanpu.ORDER_LIST_PATH), 1)
        self.assertNotIn(jidanpu.ASSIGNABLE_USERS_PATH, paths)
        self.assert_first_saved("23.45")
        result = self.client.get(self.path, {"operation_id": request["operation_id"]}).json()["data"]
        self.assertEqual(result["operation_status"], "done")
        self.assertEqual(result["current"], {"number": NEW_NUMBER, "name": "打手新"})

    def test_queue_finishes_saved_success_receipt_without_a_remote_session(self):
        from apps.business import jidanpu_direct_queue

        self.prepare_first()
        operation_id = "7f8455c2-95dd-43ea-830e-28b2be296be1"
        self.assertEqual(self.post(**self.first_request(operation_id=operation_id))["code"], 0)
        with patch.object(jidanpu_direct_taker, "_record_assignment_outcome",
                          side_effect=jidanpu_direct_taker.AssignmentError("模拟本地落账中断")):
            jidanpu_direct_queue.run_pending()
        event = self.events().get()
        self.assertEqual(event.status, JidanpuSyncEvent.Status.PENDING)
        self.assertEqual(event.payload["phase"], "confirmed")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.events().update(processed_at=timezone.now() - timedelta(seconds=10))

        with patch.object(jidanpu, "member_session", side_effect=AssertionError("已有成功回执，不可访问鸡蛋铺")):
            jidanpu_direct_queue.run_pending()

        self.assert_first_saved("23.45")
        self.assertEqual(len(self.writes()), 1)

    def test_queue_finishes_saved_refusal_without_a_remote_session(self):
        from apps.business import jidanpu_direct_queue, jidanpu_first_assignment

        self.prepare_first()
        self.downstream.result = "rejected"
        self.downstream.rejection_message = "鸡蛋铺拒绝。" + "验收原因" * 70
        operation_id = "ea64e13e-6e4b-4906-84e9-cb8876f33d50"
        self.assertEqual(self.post(**self.first_request(operation_id=operation_id))["code"], 0)
        with patch.object(jidanpu_direct_taker, "_record_assignment_outcome",
                          side_effect=jidanpu_direct_taker.AssignmentError("模拟本地落账中断")):
            jidanpu_direct_queue.run_pending()
        event = self.events().get()
        self.assertEqual(event.status, JidanpuSyncEvent.Status.PENDING)
        self.assertEqual(event.payload["phase"], "rejected")
        self.assertEqual(event.payload["rejection_message"], self.downstream.rejection_message)
        self.events().update(processed_at=timezone.now() - timedelta(seconds=10))

        with patch.object(jidanpu, "member_session", side_effect=AssertionError("已有拒绝回执，不可访问鸡蛋铺")):
            jidanpu_direct_queue.run_pending()

        result = self.client.get(self.path, {"operation_id": operation_id}).json()["data"]
        self.assertEqual((result["operation_status"], result["reason"]), ("failed", self.downstream.rejection_message))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(list(jidanpu_first_assignment.retained_drafts(self.binding)), [ORDER_NO])
        self.assertEqual(len(self.writes()), 1)

    def test_linked_manual_check_cannot_clear_saved_success_receipt(self):
        with patch.object(jidanpu_direct_taker, "_record_assignment_outcome",
                          side_effect=jidanpu_direct_taker.AssignmentError("模拟本地落账中断")):
            self.assertNotEqual(self.assign()["code"], 0)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.PENDING)

        with patch.object(jidanpu, "member_session", side_effect=AssertionError("已有成功回执，不可访问鸡蛋铺")):
            result = self.verify(close=True)

        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["data"]["current"]["number"], NEW_NUMBER)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(len(self.writes()), 1)

    def test_linked_manual_close_rechecks_a_receipt_saved_during_readback(self):
        self.downstream.result = "lost"
        self.assertNotEqual(self.assign()["code"], 0)
        original = self.downstream.call

        def publish_receipt(method, path, **kwargs):
            if path == jidanpu.ORDER_LIST_PATH:
                event = self.events().get()
                event.payload["phase"] = "confirmed"
                event.save(update_fields=["payload"])
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=publish_receipt):
            result = self.verify(close=True)

        self.assertEqual(result["code"], 0, result)
        self.assertEqual(result["data"]["current"]["number"], NEW_NUMBER)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(len(self.writes()), 1)

    def test_queued_linked_rejection_shows_original_reason_without_readback(self):
        from apps.business import jidanpu_direct_queue

        operation_id = "f2640507-7fc7-44d9-a853-a89ec05c9add"
        self.downstream.result = "rejected"
        self.downstream.rejection_message = "鸡蛋铺拒绝。" + "验收原因" * 70
        self.assertEqual(self.post(operation_id=operation_id, number=NEW_NUMBER, expected_number=OLD_NUMBER,
                                   expected_fee="16.50")["code"], 0)

        jidanpu_direct_queue.run_pending()

        response = self.client.get(self.path, {"operation_id": operation_id}).json()["data"]
        self.assertEqual(response["operation_status"], "failed")
        self.assertEqual(response["reason"], self.downstream.rejection_message)
        paths = [path for _, path, _ in self.downstream.calls]
        self.assertEqual(paths[paths.index(jidanpu.ASSIGN_PATH):], [jidanpu.ASSIGN_PATH])

    def test_explicit_http_rejections_end_assignment_and_display_upstream_reason(self):
        original = self.downstream.call
        refusals = (
            (AuthError("认证失败", upstream_message="对接账号没有指派权限"), "对接账号没有指派权限"),
            (RateLimitError("请求频率受限", upstream_message="超过鸡蛋铺请求限制"), "超过鸡蛋铺请求限制"),
            (ApiError("提交失败", status_code=422, body={"message": "打手接单资料不合格"}), "打手接单资料不合格"),
            (SessionExpiredError("登录状态已失效", upstream_message="请先登录"), "请先登录"),
        )
        for refusal, message in refusals:
            with self.subTest(error=type(refusal).__name__):
                self.downstream.calls.clear()

                def reject_assign(method, path, **kwargs):
                    if path == jidanpu.ASSIGN_PATH:
                        self.downstream.calls.append((method, path, kwargs))
                        raise refusal
                    return original(method, path, **kwargs)

                with patch.object(self.downstream, "call", side_effect=reject_assign):
                    result = self.assign()

                self.assertNotEqual(result["code"], 0)
                self.assertEqual(result["msg"], message)
                self.assertEqual(self.events().first().status, JidanpuSyncEvent.Status.FAILED)
                paths = [path for _, path, _ in self.downstream.calls]
                self.assertEqual(paths[paths.index(jidanpu.ASSIGN_PATH):], [jidanpu.ASSIGN_PATH])

    def test_session_redirect_or_unstructured_error_only_recovers_the_exact_order(self):
        original = self.downstream.call
        unknown = (
            SessionExpiredError("会话重定向"),
            ApiError("请求超时", status_code=408, body={"msg": "请求超时"}),
            ApiError("响应缺少业务码", status_code=200, body={"unrecognized": True}),
        )
        for error in unknown:
            with self.subTest(error=type(error).__name__, status=getattr(error, "status_code", None)):
                self.downstream.calls.clear()

                def lose_receipt(method, path, **kwargs):
                    if path == jidanpu.ASSIGN_PATH:
                        self.downstream.calls.append((method, path, kwargs))
                        raise error
                    return original(method, path, **kwargs)

                with patch.object(self.downstream, "call", side_effect=lose_receipt):
                    result = self.assign()
                    self.assertIn("尚未确认", result["msg"])
                    self.assertIn("尚未确认", self.assign()["msg"])

                paths = [path for _, path, _ in self.downstream.calls]
                self.assertEqual(paths[paths.index(jidanpu.ASSIGN_PATH):], [jidanpu.ASSIGN_PATH, jidanpu.ORDER_LIST_PATH])
                self.assertEqual(self.downstream.calls[-1][2]["data"]["key1"], REMOTE_NO)
                self.assertEqual(self.events().first().status, JidanpuSyncEvent.Status.PENDING)
                self.assertEqual(len(self.writes()), 1)
                self.assertEqual(self.verify(close=True)["code"], 0)

    def test_queue_clears_its_own_pending_marker_if_linked_order_is_archived(self):
        from apps.business import jidanpu_direct_queue

        operation_id = "ebafc403-c34c-4010-9081-df9832a333e8"
        self.assertEqual(self.post(operation_id=operation_id, number=NEW_NUMBER, expected_number=OLD_NUMBER,
                                   expected_fee="16.50")["code"], 0)
        self.order.deleted_at = timezone.now()
        self.order.save(update_fields=["deleted_at"])

        jidanpu_direct_queue.run_pending()

        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.deleted_at)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(self.downstream.calls, [])

    def test_queued_reassignment_recovers_late_remote_result_without_resending(self):
        from apps.business import jidanpu_direct_queue

        operation_id = "0f050bc5-b764-4908-9b74-af81f291b49e"
        submission = dict(operation_id=operation_id, number=NEW_NUMBER, expected_number=OLD_NUMBER, expected_fee="16.50")
        accepted = self.post(**submission)["data"]
        self.assertEqual(accepted["operation_status"], "working")
        self.assertEqual(self.downstream.calls, [])
        self.assertEqual(self.client.get(self.path).json()["data"], accepted)
        self.assertEqual(self.verify()["data"], accepted)
        self.assertNotEqual(self.verify(pending_ref="99999")["code"], 0)

        self.downstream.result = "lost"
        jidanpu_direct_queue.run_pending()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.events().first().payload["phase"], "sending")
        self.assertEqual(self.post(**submission)["data"]["operation_status"], "working")
        self.assertEqual(self.client.get(self.path, {"operation_id": operation_id}).json()["data"]["operation_status"], "working")

        self.downstream.current = {"userId": NEW_NUMBER, "userName": "打手新", "fee": "16.50"}
        jidanpu_direct_queue.run_pending()
        self.assertEqual(self.events().first().status, JidanpuSyncEvent.Status.PENDING)
        self.events().update(processed_at=timezone.now() - timedelta(seconds=10))
        jidanpu_direct_queue.run_pending()

        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.post(**submission)["data"]["operation_status"], "done")
        self.assertEqual(self.client.get(self.path, {"operation_id": operation_id}).json()["data"]["current"]["number"], NEW_NUMBER)

    def test_queued_first_assignment_confirms_recording_after_a_lost_response(self):
        from apps.business import jidanpu_direct_queue

        self.prepare_first()
        operation_id = "1b52ee35-4e29-4890-8c26-e78f1bdaaa13"
        submission = self.first_request(operation_id=operation_id)
        self.downstream.lose_recording_response = True
        self.assertEqual(self.post(**submission)["data"]["operation_status"], "working")

        jidanpu_direct_queue.run_pending()
        self.assertFalse(self.writes())
        self.assertEqual(self.events().get().payload["phase"], "recording")
        self.events().update(processed_at=timezone.now() - timedelta(seconds=10))

        with self.assertLogs(jidanpu_direct_taker.__name__, level="INFO") as timings:
            jidanpu_direct_queue.run_pending()
        self.assertTrue(any("stage=readback elapsed_ms=" in line for line in timings.output))
        self.assert_first_saved("23.45")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)
        self.assertEqual(self.client.get(self.path, {"operation_id": operation_id}).json()["data"]["operation_status"], "done")

    def test_queued_recording_refusal_reports_actual_reason_and_releases_wait(self):
        from apps.business import jidanpu_direct_queue

        self.prepare_first()
        operation_id = "85ffe9ab-f143-4d29-97b9-1df017a47b04"
        submission = self.first_request(operation_id=operation_id)
        call = self.downstream.call

        def reject_recording(method, path, **kwargs):
            if path == jidanpu.ADD_ORDER_PATH:
                raise ApiError("合成录单拒绝", endpoint=f"POST {path}", status_code=200, code=0,
                               body={"code": 0, "msg": "合成录单字段错误"})
            return call(method, path, **kwargs)

        self.assertEqual(self.post(**submission)["data"]["operation_status"], "working")
        with patch.object(self.downstream, "call", side_effect=reject_recording):
            jidanpu_direct_queue.run_pending()

        status = self.client.get(self.path, {"operation_id": operation_id}).json()["data"]
        self.assertEqual((status["operation_status"], status["reason"]), ("failed", "合成录单字段错误"))
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.writes())

    def test_proxy_block_after_send_intent_reaches_session_and_recovers_read_only(self):
        from apps.business import jidanpu_direct_queue

        operation_id = "01127669-d2b9-4736-97f3-5ddae4bf01ff"
        submission = dict(operation_id=operation_id, number=NEW_NUMBER, expected_number=OLD_NUMBER, expected_fee="16.50")
        self.assertEqual(self.post(**submission)["data"]["operation_status"], "working")
        blocked = []

        @contextmanager
        def observed_session(binding):
            try:
                with jidanpu.member_session(binding) as pair:
                    yield pair
            except UpstreamBlockedError as exc:
                blocked.append(exc.status_code)
                raise

        def block_write():
            raise UpstreamBlockedError(status_code=468)

        self.downstream.before_assign = block_write
        jidanpu_direct_queue.run_pending(session=observed_session)
        self.assertEqual(blocked, [468])
        self.assertEqual(self.events().get().payload["phase"], "sending")

        self.downstream.current = {"userId": NEW_NUMBER, "userName": "打手新", "fee": "16.50"}
        self.events().update(processed_at=timezone.now() - timedelta(seconds=10))
        jidanpu_direct_queue.run_pending(session=observed_session)

        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.client.get(self.path, {"operation_id": operation_id}).json()["data"]["operation_status"], "done")

    def test_queued_refusal_reuses_remote_draft_without_reopening_previous_operation_id(self):
        from apps.business import jidanpu_direct_queue

        self.prepare_first()
        first_operation = "055c3462-70a2-417b-a82f-4f2950f54de2"
        later_operation = "a4a1659f-e6f3-4cda-8db9-e2a386e40070"
        submission = self.first_request(operation_id=first_operation)
        self.downstream.result = "rejected"
        self.downstream.rejection_message = "合成指派拒绝原因"
        self.assertEqual(self.post(**submission)["code"], 0)
        jidanpu_direct_queue.run_pending()
        refused = self.client.get(self.path, {"operation_id": first_operation}).json()["data"]
        self.assertEqual((refused["operation_status"], refused["reason"]), ("failed", "合成指派拒绝原因"))

        self.downstream.result = "ok"
        self.assertEqual(self.post(**{**submission, "operation_id": later_operation})["data"]["operation_status"], "working")
        self.assertEqual(self.post(**submission)["data"], refused)
        jidanpu_direct_queue.run_pending()

        recorded = [path for _, path, _ in self.downstream.calls if path == jidanpu.ADD_ORDER_PATH]
        self.assertEqual(len(recorded), 1)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.client.get(self.path, {"operation_id": later_operation}).json()["data"]["operation_status"], "done")
        self.assertEqual(self.client.get(self.path, {"operation_id": first_operation}).json()["data"], refused)
        order_path = f"/api/business/orders/{ORDER_NO}/"
        self.assertEqual(self.client.post(order_path + "clear-taker/").json()["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        from apps.business import jidanpu_first_assignment

        self.assertFalse(jidanpu_first_assignment.retained_drafts(self.binding))

    def test_first_dispatch_reuses_the_original_order_while_it_is_in_the_downstream_hall(self):
        self.prepare_listed_first()

        options = self.client.get(self.path, {"studio_id": self.binding.pk}).json()["data"]
        self.assertTrue(options["assignable"], options)
        self.assertIsNone(options["fee"])
        self.assertEqual(self.downstream.calls, [])
        request = self.first_request(expected_fee="16.50")
        del request["fee"]
        result = self.post(**request)

        self.assertEqual(result.get("data", {}).get("current"), {"number": NEW_NUMBER, "name": "打手新"}, result)
        self.assert_first_saved("23.45")
        self.link.refresh_from_db()
        self.assertFalse(self.link.in_hall)
        mutations = [(path, kwargs["data"]) for method, path, kwargs in self.downstream.calls
                     if method == "POST" and path in (jidanpu.RELEASE_TO_HOME_PATH, jidanpu.ASSIGN_PATH)]
        self.assertEqual([path for path, _ in mutations], [jidanpu.RELEASE_TO_HOME_PATH, jidanpu.ASSIGN_PATH])
        self.assertEqual(mutations[0][1], {"orderId": REMOTE_NO})
        self.assertEqual(mutations[1][1]["orderId"], REMOTE_NO)
        self.assertEqual(json.loads(mutations[1][1]["strParameter"])[0]["fee"], 16.5)
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind=jidanpu_member.KIND_WITHDRAW, target_ref=ORDER_NO).exists())

    def test_waiting_order_can_choose_a_worker_if_downstream_order_read_times_out(self):
        self.prepare_listed_first()
        downstream_call = self.downstream.call

        def timeout_on_order_reads(method, path, **kwargs):
            if path == jidanpu.ORDER_LIST_PATH:
                raise TransportError("合成订单读取超时")
            return downstream_call(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=timeout_on_order_reads):
            options = self.client.get(self.path, {"studio_id": self.binding.pk}).json()["data"]
            self.assertTrue(options["assignable"], options)
            self.assertIsNone(options["fee"])
            request = self.first_request()
            search_call = next(kwargs for _, path, kwargs in self.downstream.calls if path == jidanpu.ASSIGNABLE_USERS_PATH)
            self.assertEqual(search_call["query"]["gameName"], "测试游戏")
            self.assertIn("暂时无法核对", self.post(**request)["msg"])

        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertTrue(self.downstream.in_hall)
        self.assertFalse(self.writes())
        self.assertEqual(self.post(**request)["code"], 0)

    def test_waiting_order_picker_opens_without_downstream_reads_and_search_preserves_zero_fee(self):
        self.prepare_listed_first()
        self.downstream.default_fee = "0.00"
        options = self.client.get(self.path, {"studio_id": self.binding.pk}).json()["data"]
        self.assertTrue(options["assignable"], options)
        self.assertIsNone(options["fee"])
        self.assertEqual(self.downstream.calls, [])

        found = self.post(self.path + "search/", query="打手新", studio_id=self.binding.pk)["data"]
        self.assertEqual(found["items"], [{"number": NEW_NUMBER, "name": "打手新"}])
        self.assertEqual(found["fee"], "0.00")
        self.assertEqual([path for _, path, _ in self.downstream.calls], [
            jidanpu.ORDER_LIST_PATH, jidanpu.ASSIGNABLE_USERS_PATH,
        ])

    def test_waiting_order_checks_recording_owner_only_when_confirming_assignment(self):
        self.prepare_listed_first()
        self.downstream.recorded_by = "672"
        request = self.first_request()

        self.assertIn("录单人无法核实", self.post(**request)["msg"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertTrue(self.downstream.in_hall)
        self.assertFalse(self.writes())

    def test_waiting_order_picker_can_search_when_downstream_game_is_blank(self):
        self.prepare_listed_first()
        self.downstream.game_name = " "

        request = self.first_request(expected_fee="16.50")
        picker_query = next(kwargs["query"] for _, path, kwargs in self.downstream.calls if path == jidanpu.ASSIGNABLE_USERS_PATH)
        self.assertEqual(picker_query["gameName"], "测试游戏")
        reply = self.post(**request)
        self.assertEqual(reply["code"], 0, reply)
        self.assertEqual(reply["data"]["current"]["number"], NEW_NUMBER)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.downstream.in_hall)
        self.assertEqual(len(self.writes()), 1)

    def test_waiting_order_does_not_preview_fee_from_wrong_source_order(self):
        self.prepare_listed_first()
        self.order.source_order_no = "ANOTHER-SOURCE"
        self.order.save(update_fields=["source_order_no"])

        options = self.client.get(self.path, {"studio_id": self.binding.pk}).json()["data"]
        self.assertTrue(options["assignable"], options)
        self.assertIsNone(options["fee"])
        search = self.post(self.path + "search/", query="打手新", studio_id=self.binding.pk)["data"]
        self.assertEqual(search["items"][0]["number"], NEW_NUMBER)
        self.assertNotIn("fee", search)
        self.assertIn("录单人无法核实", self.post(**self.first_request())["msg"])
        self.assertTrue(self.downstream.in_hall)
        self.assertFalse(self.writes())

    def test_worker_that_takes_waiting_order_before_submission_is_preserved(self):
        self.prepare_listed_first()
        self.downstream.in_hall = False
        request = self.first_request()
        self.downstream.change_on_order_read = {"userId": OLD_NUMBER, "userName": "打手旧", "fee": "16.50"}

        self.assertIn("已有打手接手", self.post(**request)["msg"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.downstream.current["userId"], OLD_NUMBER)
        self.assertFalse(self.writes())

    def test_first_dispatch_does_not_replace_a_worker_who_grabs_the_downstream_order_first(self):
        self.prepare_listed_first()
        self.link.in_hall = False
        self.link.save(update_fields=["in_hall"])

        def another_worker_grabs_it():
            self.downstream.current = {"userId": OLD_NUMBER, "userName": "打手旧", "fee": "16.50"}
            self.downstream.in_hall = False

        self.downstream.before_release = another_worker_grabs_it
        result = self.post(**self.first_request(expected_fee="16.50"))

        self.assertNotEqual(result["code"], 0)
        self.assertIn("已有打手接手", result["msg"])
        self.assertFalse(self.writes())
        self.assertEqual(self.downstream.current["userId"], OLD_NUMBER)
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertTrue(self.link.in_hall)
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind=jidanpu_member.KIND_WITHDRAW, target_ref=ORDER_NO).exists())
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)

    def test_first_dispatch_verifies_a_lost_unlist_response_before_retrying_without_repeating_it(self):
        self.prepare_listed_first()
        request = self.first_request(expected_fee="16.50")
        del request["fee"]
        original = self.downstream.call

        def drop_unlist_confirmation(method, path, **kwargs):
            if path == jidanpu.ORDER_LIST_PATH and not self.downstream.in_hall:
                raise TransportError("撤下后的合成读取失败")
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=drop_unlist_confirmation):
            self.assertIn("尚未确认撤下大厅", self.post(**request)["msg"])
            pending = self.client.get(self.path).json()["data"]["pending"]
            self.assertIsNotNone(pending)
            self.assertNotEqual(self.post(**request)["code"], 0)
            self.assertNotEqual(self.verify(pending_ref=pending)["code"], 0)
        self.assertFalse(self.writes())
        self.assertFalse(self.downstream.in_hall)

        checked = self.verify(pending_ref=pending)["data"]
        self.assertEqual((checked["reservation"], checked["fee"]), (pending, "16.50"))
        selection = self.post(self.path + "search/", query="打手新", studio_id=self.binding.pk)["data"]
        self.assertNotIn("fee", selection)
        result = self.post(**request)
        self.assertEqual(result["data"]["current"]["number"], NEW_NUMBER)
        self.assert_first_saved("23.45")
        self.assertEqual(sum(path == jidanpu.RELEASE_TO_HOME_PATH for _, path, _ in self.downstream.calls), 1)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.downstream.current["fee"], 16.5)

    def test_first_dispatch_can_end_waiting_after_confirmed_unlist_and_resume_normal_sync(self):
        self.prepare_listed_first()

        def unlist_without_reply():
            self.downstream.in_hall = False
            self.downstream.default_fee = "0.00"
            raise TransportError("合成撤下回包丢失")

        self.downstream.before_release = unlist_without_reply
        request = self.first_request(expected_fee="16.50")
        original = self.downstream.call

        def disconnect_until_verified(method, path, **kwargs):
            if path == jidanpu.ORDER_LIST_PATH and not self.downstream.in_hall:
                raise TransportError("合成回读丢失")
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=disconnect_until_verified):
            self.assertIn("尚未确认撤下大厅", self.post(**request)["msg"])
        pending = self.client.get(self.path).json()["data"]["pending"]
        result = self.verify(pending_ref=pending, close=True)

        self.assertEqual(result["code"], 0, result)
        self.link.refresh_from_db()
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.link.in_hall)
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertFalse(self.writes())
        reconcile = jidanpu_member.reconcile(self.binding, now=timezone.now())
        self.assertEqual([item.order.order_no for item in reconcile.to_launch], [ORDER_NO])
        self.assertFalse(reconcile.to_withdraw)

    def test_first_dispatch_waits_for_pending_sync_and_rechecks_a_changed_fee_before_unlist(self):
        self.prepare_listed_first()
        request = self.first_request(expected_fee="16.50")
        sync = jidanpu_member.queue_member_event(self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH)
        self.assertIn("正在同步", self.post(**request)["msg"])
        sync.status = JidanpuSyncEvent.Status.DONE
        sync.save(update_fields=["status"])
        self.downstream.default_fee = "17.25"

        self.assertIn("金额已变化", self.post(**request)["msg"])
        self.assertTrue(self.downstream.in_hall)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.writes())
        self.assertFalse(any(path == jidanpu.RELEASE_TO_HOME_PATH for _, path, _ in self.downstream.calls))

    def test_pending_unlist_cannot_be_closed_while_still_listed_but_can_close_after_remote_cancellation(self):
        self.prepare_listed_first()

        def unknown_unlist():
            raise TransportError("合成撤下超时")

        self.downstream.before_release = unknown_unlist
        self.assertIn("尚未确认撤下大厅", self.post(**self.first_request(expected_fee="16.50"))["msg"])
        pending = self.client.get(self.path).json()["data"]["pending"]
        result = self.verify(pending_ref=pending, close=True)
        self.assertNotEqual(result["code"], 0)
        self.assertIn("尚未确认撤下大厅", result["msg"])
        self.assertIn("确认已撤下后再回来核对", result["msg"])
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)

        self.downstream.in_hall = False
        self.downstream.remote_status = "已撤销"
        result = self.verify(pending_ref=pending, close=True)
        self.assertEqual(result["code"], 0, result)
        self.assertIn("已撤销", result["data"]["reason"])
        self.order.refresh_from_db()
        self.link.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertTrue(self.link.in_hall)
        self.assertIsNone(self.link.withdrawn_at)
        self.assertFalse(self.writes())
        self.assertEqual(sum(path == jidanpu.RELEASE_TO_HOME_PATH for _, path, _ in self.downstream.calls), 1)
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_launch)

    def test_first_dispatch_ends_without_assignment_if_original_order_has_been_refunded(self):
        self.prepare_listed_first()

        def another_operator_refunds_it():
            self.downstream.in_hall = False
            self.downstream.remote_status = "已退单"

        self.downstream.before_release = another_operator_refunds_it
        result = self.post(**self.first_request(expected_fee="16.50"))
        self.assertNotEqual(result["code"], 0)
        self.assertIn("已退单", result["msg"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.writes())
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_launch)

    def test_a_definite_unlist_rejection_leaves_the_original_order_available_for_grab(self):
        self.prepare_listed_first()

        def reject_unlist():
            raise ApiError("合成撤下不允许", code=0)

        self.downstream.before_release = reject_unlist
        result = self.post(**self.first_request(expected_fee="16.50"))
        self.assertNotEqual(result["code"], 0)
        self.assertIn("拒绝撤下大厅", result["msg"])
        self.assertTrue(self.downstream.in_hall)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.writes())
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)

    def test_hall_assignment_wait_can_close_if_the_exact_order_was_canceled_after_sending(self):
        self.prepare_listed_first()
        self.downstream.result = "lost"
        result = self.post(**self.first_request(expected_fee="16.50"))
        self.assertIn("首次指派尚未确认", result["msg"])
        self.assertEqual(len(self.writes()), 1)
        pending = self.client.get(self.path).json()["data"]["pending"]

        self.downstream.remote_status = "已撤销"
        result = self.verify(pending_ref=pending, close=True)
        self.assertEqual(result["code"], 0, result)
        self.assertIn("已撤销", result["data"]["reason"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(len(self.writes()), 1)
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_launch)

    def assert_first_saved(self, price):
        self.order.refresh_from_db()
        self.performance.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.hall_price, Decimal(price))
        self.assertEqual(self.order.total_price, Decimal("999"))
        self.assertEqual(self.performance.user_id, self.binding.user_id)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(JidanpuMemberOrderLink.objects.get(order=self.order).jidanpu_order_id, REMOTE_NO)
        audit = OperationLog.objects.get(action=jidanpu_direct_taker.AUDIT_ACTION, order_no=ORDER_NO, reason="")
        self.assertEqual((audit.actor, audit.reason), ("first-dispatch-editor", ""))

    def test_first_dispatch_records_then_assigns_with_distinct_amounts(self):
        self.prepare_first()
        OrderScreenshot.objects.create(
            order=self.order, status=OrderScreenshot.STATUS_READY, img_type=OrderScreenshot.IMG_ORDER,
            uploaded_by=self.binding.user, object_key="test-first-dispatch/order.jpg",
        )

        def enter_remote_write():
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
            self.assertEqual(self.order.peer_direct_pending["studio_id"], 0)
            self.assertEqual(self.order.peer_direct_pending["account_name"], "")
        self.downstream.before_assign = enter_remote_write

        request = self.first_request(price="23.40")
        result = self.post(**request)
        self.assertEqual(result.get("data", {}).get("current"), {"number": NEW_NUMBER, "name": "打手新"}, result)
        self.assert_first_saved("23.40")
        operations = [(path, kwargs) for method, path, kwargs in self.downstream.calls
                      if method == "POST" and path in (jidanpu.ADD_ORDER_PATH, jidanpu.ASSIGN_PATH)]
        self.assertEqual([path for path, _ in operations], [jidanpu.ADD_ORDER_PATH, jidanpu.ASSIGN_PATH])
        [record, assignment] = [kwargs["data"] for _, kwargs in operations]
        self.assertEqual(json.loads(unquote(record["strParameter"]))["price"], "23.40")
        self.assertEqual(json.loads(assignment["strParameter"])[0]["fee"], 12.34)
        repush = JidanpuSyncEvent.objects.get(kind=jidanpu_member.KIND_ASSIGN, target_ref=ORDER_NO)
        self.assertEqual(repush.payload["repush"], True)
        self.assertEqual(repush.payload["member_binding_id"], self.binding.pk)

    def test_first_dispatch_does_not_scan_a_large_roster_after_selection(self):
        self.prepare_first()
        request = {**self.first_request(), "name": "打手新"}
        self.downstream.calls.clear()
        original = self.downstream.call

        def large_directory(method, path, **kwargs):
            if path == jidanpu.ASSIGNABLE_USERS_PATH:
                self.downstream.calls.append((method, path, kwargs))
                self.downstream.last_envelope = {"code": 0, "count": 3523}
                offset = (kwargs["query"]["page"] - 1) * 200
                return [{"id": 10000 + n, "userName": "合成其他打手", "isValid": "Y"}
                        for n in range(offset, min(offset + 200, 3523))]
            return original(method, path, **kwargs)

        with patch.object(jidanpu, "ASSIGNABLE_USERS_PAGE_SIZE", 200), patch.object(self.downstream, "call", side_effect=large_directory):
            reply = self.post(**request)
        self.assertEqual(reply["code"], 0, reply)
        self.assertEqual(reply["data"]["current"]["number"], NEW_NUMBER)
        self.assertFalse(any(path == jidanpu.ASSIGNABLE_USERS_PATH for _, path, _ in self.downstream.calls))
        self.assert_first_saved("23.45")

    def test_reassignment_does_not_read_roster_after_selection(self):
        original = self.downstream.call

        def directory_unavailable(method, path, **kwargs):
            if path == jidanpu.ASSIGNABLE_USERS_PATH:
                raise AssertionError("提交指派不能重新读取人员名单")
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=directory_unavailable):
            reply = self.assign()
        self.assertEqual(reply["code"], 0, reply)
        self.assertEqual(reply["data"]["current"]["number"], NEW_NUMBER)
        self.assertEqual(len(self.writes()), 1)

    def test_missing_name_from_an_old_client_requires_reselection_without_writing(self):
        reply = self.client.post(self.path, {
            "number": NEW_NUMBER, "expected_number": OLD_NUMBER, "expected_fee": "16.50",
        }, content_type="application/json").json()
        self.assertEqual(reply["code"], 1000)
        self.assertIn("重新搜索", reply["msg"])
        self.assertFalse(self.downstream.calls)
        self.assertFalse(self.events().exists())

    def test_first_explicit_rejection_releases_waiting_without_needing_readback(self):
        self.prepare_first()
        request = self.first_request()
        self.downstream.result = "rejected"
        self.downstream.rejection_message = "该打手没有该游戏接单权限，请更换打手" * 12
        sent = False
        original = self.downstream.call

        def lose_readback_after_rejection(method, path, **kwargs):
            nonlocal sent
            if path == jidanpu.ORDER_LIST_PATH and sent:
                raise TransportError("合成拒绝后的回读失败")
            result = original(method, path, **kwargs)
            if path == jidanpu.ASSIGN_PATH:
                sent = True
            return result

        with patch.object(self.downstream, "call", side_effect=lose_readback_after_rejection):
            reply = self.post(**request)
        self.assertEqual(reply["code"], 1000)
        self.assertEqual(reply["msg"], self.downstream.rejection_message)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.FAILED)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)

    def test_rejected_first_dispatch_allows_local_assignment_and_releases_draft_cleanup(self):
        self.prepare_first()
        self.downstream.result = "rejected"
        self.post(**self.first_request())
        make_logged_in_client(username="local-replacement")
        target = get_user_model().objects.get(username="local-replacement")
        reply = self.post(f"/api/business/orders/{ORDER_NO}/assign/", user_id=target.pk)
        self.assertEqual(reply["code"], 0, reply)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        result = jidanpu_member.reconcile(self.binding, now=timezone.now())
        self.assertEqual([item.order.pk for item in result.to_withdraw], [self.order.pk])

    def test_rejected_first_dispatch_reuses_the_frozen_recorded_price(self):
        self.prepare_first()
        self.downstream.result = "rejected"
        request = self.first_request(price="23.40")
        self.assertNotEqual(self.post(**request)["code"], 0)
        options = self.client.get(self.path, {"studio_id": self.binding.pk}).json()["data"]
        self.assertEqual(options["price"], "23.40")
        self.downstream.result = "success"
        request.pop("price")
        request["expected_price"] = options["price"]
        reply = self.post(**request)
        self.assertEqual(reply["code"], 0, reply)
        self.assert_first_saved("23.40")
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)

    def test_rejected_first_dispatch_can_be_deleted_and_no_longer_retains_draft(self):
        self.prepare_first()
        self.downstream.result = "rejected"
        self.post(**self.first_request())
        deleter = make_logged_in_client(
            username="draft-cleanup-admin", groups=("订单管理员",), nodes=("order.delete",),
        )
        reply = deleter.post(f"/api/business/orders/{ORDER_NO}/delete/", {
            "isDelEmp": "Y",
        }, content_type="application/json").json()
        self.assertEqual(reply["code"], 0, reply)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.deleted_at)
        result = jidanpu_member.reconcile(self.binding, now=timezone.now())
        self.assertEqual([item.order.pk for item in result.to_withdraw], [self.order.pk])

    def test_rejected_draft_retry_read_failure_does_not_lock_local_order_again(self):
        self.prepare_first()
        request = self.first_request()
        self.downstream.result = "rejected"
        self.assertNotEqual(self.post(**request)["code"], 0)
        self.downstream.result = "success"
        original = self.downstream.call

        def lose_order_read(method, path, **kwargs):
            if path == jidanpu.ORDER_LIST_PATH:
                raise TransportError("合成草稿重试读取失败")
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=lose_order_read):
            reply = self.post(**request)
        self.assertIn("暂时无法核对", reply["msg"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.FAILED)
        self.assertEqual(len(self.writes()), 1)
        queued = jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        self.assertEqual((queued.launch, queued.assign, queued.withdraw, queued.unlist), (0, 0, 0, 0))
        self.assertEqual(self.post(**request)["code"], 0)
        self.assert_first_saved("23.45")
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)

    def test_rejected_draft_retry_preflight_changes_preserve_proof_and_do_not_write(self):
        self.prepare_first()
        request = self.first_request()
        self.downstream.result = "rejected"
        self.assertNotEqual(self.post(**request)["code"], 0)
        initial = self.events().get().payload
        self.downstream.result = "success"
        original = self.downstream.call
        cases = (
            ("price", "接单价与本次发单价"),
            ("owner", "录单人无法核实"),
            ("late_read", "暂时无法核对"),
            ("late_price", "订单或当前接手人已变化"),
            ("late_taker", "已有打手接手"),
        )
        for mode, expected_message in cases:
            with self.subTest(mode=mode):
                self.downstream.price = "99.99" if mode == "price" else "23.45"
                self.downstream.login_uid = "672" if mode == "owner" else "671"
                self.downstream.current = None
                reads = 0

                def change_last_read(method, path, **kwargs):
                    nonlocal reads
                    if path == jidanpu.ORDER_LIST_PATH:
                        reads += 1
                        # The first read also verifies ownership. The second is the pre-write recheck.
                        if reads == 2:
                            if mode == "late_read":
                                raise TransportError("合成发送前最终读取失败")
                            if mode == "late_price":
                                self.downstream.price = "99.99"
                            if mode == "late_taker":
                                self.downstream.current = {"userId": OLD_NUMBER, "userName": "打手旧", "fee": "12.34"}
                    return original(method, path, **kwargs)

                with patch.object(self.downstream, "call", side_effect=change_last_read):
                    reply = self.post(**request)
                self.assertIn(expected_message, reply["msg"])
                self.order.refresh_from_db()
                event = self.events().get()
                self.assertIsNone(self.order.peer_direct_pending)
                self.assertEqual(event.status, JidanpuSyncEvent.Status.FAILED)
                self.assertEqual(self.order.hall_price, Decimal("23.45"))
                self.assertEqual(len(self.writes()), 1)
                for key in ("recording_remote_oid", "direct_recording_owner_id", "link_id", "remote_order_id", "price"):
                    self.assertEqual(event.payload[key], initial[key])
                reconciled = jidanpu_member.reconcile(self.binding, now=timezone.now())
                self.assertFalse(reconciled.to_launch or reconciled.to_record or reconciled.to_withdraw or reconciled.to_unlist)
        self.downstream.price = "23.45"
        self.downstream.login_uid = "671"
        self.downstream.current = None
        self.assertEqual(self.post(**request)["code"], 0)
        self.assert_first_saved("23.45")
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)

    def test_retry_failure_does_not_release_an_earlier_unknown_assignment(self):
        self.prepare_first()
        request = self.first_request()
        self.downstream.result = "rejected"
        self.assertNotEqual(self.post(**request)["code"], 0)
        self.downstream.result = "lost"
        self.assertNotEqual(self.post(**request)["code"], 0)
        pending = str(self.events().get().pk)
        self.assertEqual(self.verify(pending_ref=pending, close=True)["code"], 0)
        original = self.downstream.call

        def lose_order_read(method, path, **kwargs):
            if path == jidanpu.ORDER_LIST_PATH:
                raise TransportError("合成已知待确认流程的读取失败")
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=lose_order_read):
            reply = self.post(**request)
        self.assertIn("尚未确认", reply["msg"])
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.PENDING)
        self.assertEqual(len(self.writes()), 2)

    def test_unexpected_assignment_code_keeps_waiting_without_repeating_send(self):
        self.prepare_first()
        request = self.first_request()
        self.downstream.result = "unexpected"
        reply = self.post(**request)
        self.assertNotEqual(reply["code"], 0)
        self.assertIn("尚未确认", reply["msg"])
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.PENDING)
        self.assertNotEqual(self.post(**request)["code"], 0)
        self.assertEqual(len(self.writes()), 1)

    def test_rejected_first_dispatch_keeps_one_record_for_retry(self):
        self.prepare_first()
        self.downstream.result = "rejected"
        request = self.first_request()
        self.assertIn("拒绝", self.post(**request)["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.FAILED)
        data = self.client.get(self.path).json()["data"]
        self.assertTrue(data["assignable"])
        self.assertNotIn("reservation", data)
        self.assertIsNone(data["pending"])
        recorded = JidanpuMemberOrderLink.objects.get(order=self.order)
        queue = jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        self.assertEqual((queue.launch, queue.assign, queue.withdraw, queue.unlist), (0, 0, 0, 0))
        late = jidanpu_member.queue_member_event(self.order, self.binding, kind=jidanpu_member.KIND_WITHDRAW)
        touched = len(self.downstream.calls)
        self.assertEqual(jidanpu_member.consume_event(late, client=self.downstream), "deferred")
        self.assertEqual(len(self.downstream.calls), touched)
        self.assertTrue(self.client.get(self.path).json()["data"]["assignable"])
        self.downstream.result = "success"
        found = self.post(self.path + "search/", query=OLD_NUMBER, studio_id=self.binding.pk)["data"]
        result = self.post(**{**request, "studio_token": found["studio_token"], "number": OLD_NUMBER})
        self.assertEqual(result["data"]["current"]["number"], OLD_NUMBER)
        self.assert_first_saved("23.45")
        self.assertEqual(JidanpuMemberOrderLink.objects.get(order=self.order).pk, recorded.pk)
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)
        self.assertEqual(len(self.writes()), 2)
        late.refresh_from_db()
        self.assertEqual(late.status, JidanpuSyncEvent.Status.PENDING)

    def test_unconfirmed_remote_recording_is_recovered_before_retry(self):
        self.prepare_first()
        request = self.first_request()
        self.downstream.lose_recording_response = True

        self.assertIn("尚未确认", self.post(**request)["msg"])
        self.assertNotEqual(self.post(**request)["code"], 0)
        self.assertFalse(self.writes())
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        still_pending = self.client.get(self.path).json()["data"]["pending"]
        self.assertIsNotNone(still_pending)
        result = self.verify(pending_ref=still_pending)["data"]
        self.assertTrue(result["assignable"])
        self.assertIsNotNone(result["reservation"])
        self.assertEqual(len(self.writes()), 0)
        self.assertEqual(self.post(**self.first_request())["data"]["current"]["number"], NEW_NUMBER)
        self.assert_first_saved("23.45")
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)

    def test_first_dispatch_requires_unchanged_remote_price_before_saving(self):
        self.prepare_first()
        self.downstream.result = "lost_after_apply"
        self.downstream.before_assign = lambda: setattr(self.downstream, "price", "29.75")
        reply = self.post(**self.first_request())
        self.assertNotEqual(reply["code"], 0)
        pending = self.client.get(self.path).json()["data"]["pending"]
        self.assertIsNotNone(pending)
        self.assertNotEqual(self.verify(pending_ref=pending, close=True)["code"], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.downstream.price = "23.45"
        self.assertEqual(self.verify(pending_ref=pending)["code"], 0)
        self.assert_first_saved("23.45")

    def test_first_dispatch_read_failure_cannot_release_an_assigned_order(self):
        self.prepare_first()
        self.downstream.result = "lost_after_apply"
        request = self.first_request()
        sent = False
        original = self.downstream.call

        def go_offline_after_assign(method, path, **kwargs):
            nonlocal sent
            if sent and path == jidanpu.ORDER_LIST_PATH:
                raise TransportError("合成读取失败")
            if path == jidanpu.ASSIGN_PATH:
                sent = True
            return original(method, path, **kwargs)

        with patch.object(self.downstream, "call", side_effect=go_offline_after_assign):
            self.assertNotEqual(self.post(**request)["code"], 0)
            pending = self.client.get(self.path).json()["data"]["pending"]
            self.assertNotEqual(self.verify(pending_ref=pending, close=True)["code"], 0)
        self.assertEqual(self.downstream.current["userId"], NEW_NUMBER)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_withdraw)

    def test_first_dispatch_manual_verification_can_retry_a_confirmed_unassigned_order(self):
        self.prepare_first()
        self.downstream.result = "lost"
        self.assertIn("尚未确认", self.post(**self.first_request())["msg"])
        pending = self.client.get(self.path).json()["data"]["pending"]
        link = JidanpuMemberOrderLink.objects.get(order=self.order)
        self.assertNotEqual(self.verify(pending_ref=pending)["code"], 0)

        checked = self.verify(pending_ref=pending, close=True)["data"]
        self.assertEqual(checked["reservation"], pending)
        self.assertTrue(checked["assignable"])
        self.assertEqual(len(self.writes()), 1)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_withdraw)

        self.downstream.result = "success"
        dispatched = self.post(**self.first_request())
        self.assertEqual(dispatched["data"]["current"]["number"], NEW_NUMBER)
        self.assertEqual(JidanpuMemberOrderLink.objects.get(order=self.order).pk, link.pk)
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)
        self.assertEqual(len(self.writes()), 2)

    def test_first_dispatch_can_explicitly_end_after_the_draft_is_confirmed_unassigned(self):
        self.prepare_first()
        self.downstream.result = "lost"
        self.assertIn("尚未确认", self.post(**self.first_request())["msg"])
        pending = self.client.get(self.path).json()["data"]["pending"]
        self.assertEqual(self.verify(pending_ref=pending, close=True)["data"]["reservation"], pending)
        self.assertEqual(self.verify(pending_ref=pending, close=True)["code"], 0)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(len(self.writes()), 1)
        audit = OperationLog.objects.get(action=jidanpu_direct_taker.AUDIT_ACTION, order_no=ORDER_NO)
        self.assertEqual(audit.reason, "已结束等待，未确认鸡蛋铺指派成功。")

    def test_first_dispatch_cannot_confirm_if_order_link_changes_after_sending(self):
        self.prepare_first()

        def replace_link():
            JidanpuMemberOrderLink.objects.filter(order=self.order).update(jidanpu_order_id="20261118000000000127")

        self.downstream.before_assign = replace_link
        self.assertNotEqual(self.post(**self.first_request())["code"], 0)
        pending = self.client.get(self.path).json()["data"]["pending"]
        self.assertNotEqual(self.verify(pending_ref=pending, close=True)["code"], 0)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_first_dispatch_cannot_release_after_binding_changes(self):
        self.prepare_first()
        self.downstream.result = "lost"
        self.assertIn("尚未确认", self.post(**self.first_request())["msg"])
        pending = self.client.get(self.path).json()["data"]["pending"]
        self.binding.account = "changed"
        self.binding.save(update_fields=["account"])
        self.assertNotEqual(self.verify(pending_ref=pending, close=True)["code"], 0)
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_withdraw)

    def assert_unverified_recording_never_linked(self):
        pending = self.client.get(self.path).json()["data"]["pending"]
        self.assertIsNotNone(pending)
        self.assertIn("录单人无法核实", self.verify(pending_ref=pending, close=True)["msg"])
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        self.assertFalse(self.writes())
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_withdraw)

    def test_first_dispatch_does_not_adopt_another_employee_s_order(self):
        self.prepare_first()
        self.downstream.recorded_by = "672"
        self.downstream.remote_recorded = True
        self.assertIn("录单人无法核实", self.post(**self.first_request())["msg"])
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        self.assertFalse(self.writes())
        self.assertFalse(any(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls))

    def test_first_dispatch_does_not_link_an_unverified_recording_readback(self):
        self.prepare_first()
        self.downstream.recorded_by = "672"
        self.downstream.remote_oid = 124
        self.assertIn("录单人无法核实", self.post(**self.first_request())["msg"])
        self.assertTrue(self.downstream.remote_recorded)
        self.assert_unverified_recording_never_linked()

    def test_new_record_uses_its_acknowledged_identity_when_recorder_filter_is_ignored(self):
        self.prepare_first()
        self.downstream.ignore_recorder_filter = True
        result = self.post(**self.first_request())
        self.assertEqual(result["code"], 0, result)
        self.assert_first_saved("23.45")
        event = self.events().get()
        self.assertEqual(event.payload["recording_remote_oid"], 123)
        self.assertEqual(event.payload["direct_recording_owner_id"], "671")
        self.assertEqual(sum(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls), 1)

    def test_failed_identity_read_before_recording_leaves_order_available(self):
        self.prepare_first()
        request = self.first_request()
        original = self.downstream.call
        for failure in (TransportError("合成身份读取超时"), ApiError("合成身份读取失败", code=0)):
            with self.subTest(failure=type(failure).__name__):
                def fail_identity(method, path, **kwargs):
                    if path == jidanpu.LOGIN_USER_INFO_PATH:
                        raise failure
                    return original(method, path, **kwargs)

                with patch.object(self.downstream, "call", side_effect=fail_identity):
                    self.assertNotEqual(self.post(**request)["code"], 0)
                self.order.refresh_from_db()
                self.assertIsNone(self.order.peer_direct_pending)
                self.assertEqual(self.events().first().status, JidanpuSyncEvent.Status.SKIPPED)
                self.assertFalse(any(path == jidanpu.ADD_ORDER_PATH for _, path, _ in self.downstream.calls))
                self.assertFalse(self.writes())

    def test_adopted_order_keeps_waiting_when_fee_changes_after_link_is_created(self):
        self.prepare_first()
        self.downstream.remote_recorded = True
        self.downstream.default_fee = "17.25"
        result = self.post(**self.first_request(expected_fee="16.50"))
        self.assertIn("金额已变化", result["msg"])
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.assertEqual(self.events().get().payload["phase"], "ready")
        self.assertTrue(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        self.assertFalse(self.writes())
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_withdraw)

    def prepare_legacy_recorded_draft(self):
        self.prepare_first()
        self.downstream.ignore_recorder_filter = True
        self.downstream.remote_oid = 124
        self.assertIn("录单人无法核实", self.post(**self.first_request())["msg"])
        event = self.events().get()
        event.payload.pop("recording_remote_oid", None)
        event.payload.pop("direct_recording_owner_id", None)
        event.save(update_fields=["payload"])
        return str(event.pk)

    def test_recorded_draft_can_close_after_exact_remote_refund_without_recorder_filter(self):
        pending = self.prepare_legacy_recorded_draft()
        self.assertEqual(self.events().get().payload["phase"], "recording")
        self.downstream.remote_status = "已退单"
        count = len(self.downstream.calls)

        result = self.verify(pending_ref=pending, close=True)

        self.assertEqual(result["code"], 0, result)
        self.order.refresh_from_db()
        self.performance.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(self.performance.user_id)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(self.writes(), [])
        self.assertEqual([path for _, path, _ in self.downstream.calls[count:]], [jidanpu.ORDER_LIST_PATH])

    def test_unverified_recorded_draft_keeps_waiting_until_exact_terminal_order_is_confirmed(self):
        pending = self.prepare_legacy_recorded_draft()
        for label, state, listed, remote_no in (
            ("live", "未分配", False, REMOTE_NO),
            ("listed", "已退单", True, REMOTE_NO),
            ("wrong_original", "已退单", False, "20261118000000000999"),
        ):
            with self.subTest(label=label):
                self.downstream.remote_status = state
                self.downstream.in_hall = listed
                self.downstream.remote_no = remote_no
                self.assertNotEqual(self.verify(pending_ref=pending)["code"], 0)
                self.order.refresh_from_db()
                self.assertIsNotNone(self.order.peer_direct_pending)
                self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
                self.assertEqual(self.writes(), [])

    def test_explicitly_closing_legacy_live_draft_keeps_exact_original_without_assigning_it(self):
        pending = self.prepare_legacy_recorded_draft()
        before = len(self.downstream.calls)
        result = self.verify(pending_ref=pending, close=True)
        self.assertEqual(result["code"], 0, result)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(self.writes(), [])
        self.assertEqual([path for _, path, _ in self.downstream.calls[before:]], [jidanpu.ORDER_LIST_PATH, jidanpu.ASS_LOGS_PATH])
        self.assertFalse(jidanpu_member.reconcile(self.binding, now=timezone.now()).to_withdraw)

    def test_first_dispatch_does_not_recover_another_employee_s_order(self):
        self.prepare_first()
        self.downstream.lose_recording_response = True
        self.assertIn("尚未确认", self.post(**self.first_request())["msg"])
        self.downstream.recorded_by = "672"
        self.assert_unverified_recording_never_linked()

    def test_first_dispatch_before_recording_failure_can_retry_without_waiting(self):
        self.prepare_first()
        request = self.first_request()
        with patch.object(self.downstream, "call", side_effect=TransportError("合成请求在录单前失败")):
            self.assertIn("暂时无法核对", self.post(**request)["msg"])
            pending = self.client.get(self.path).json()["data"]["pending"]
            self.assertIsNone(pending)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.peer_direct_pending)
        self.assertFalse(self.downstream.remote_recorded)
        self.assertFalse(self.writes())
        self.assertEqual(self.post(**request)["code"], 0)

    def test_first_dispatch_requires_the_studio_already_linked_to_the_order(self):
        self.prepare_first()
        self.downstream.current = {"userId": OLD_NUMBER, "userName": "打手旧", "fee": "12.34"}
        make_logged_in_client(username="other-eligible-studio")
        second = JidanpuMemberBinding.objects.create(
            user=get_user_model().objects.get(username="other-eligible-studio"),
            jidanpu_base_url="https://other-eligible.test.invalid", account="other-test",
            password="placeholder", enabled=True, input_order_kefu="671",
        )
        self.bound_sessions[second.pk] = second_client = Downstream()
        second_client.current = None
        second_client.remote_recorded = False

        token = self.post(self.path + "search/", query=NEW_NUMBER, studio_id=second.pk)["data"]["studio_token"]
        second_client.calls.clear()
        JidanpuMemberOrderLink.objects.create(binding=self.binding, order=self.order, jidanpu_order_id=REMOTE_NO)
        catalog = self.client.get(self.path, {"studio_id": second.pk}).json()["data"]
        self.assertIn("已关联", catalog["reason"])
        self.assertIn("已关联", self.post(self.path + "search/", query=NEW_NUMBER, studio_id=second.pk)["msg"])
        reply = self.post(
            studio_id=second.pk, studio_token=token, number=NEW_NUMBER, expected_number="",
            expected_fee=None, expected_price="23.45", fee="12.34",
        )
        self.assertIn("已关联", reply["msg"])
        self.assertFalse(second_client.calls)

    def test_unknown_first_dispatch_keeps_local_and_sync_writes_waiting(self):
        self.prepare_first(status=OrderStatus.WAITING_GRAB)
        self.downstream.result = "lost"
        request = self.first_request()
        self.assertIn("尚未确认", self.post(**request)["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        pending = self.client.get(self.path).json()["data"]["pending"]
        self.assertEqual(self.events().get().payload["phase"], "sending")

        peer = self.client.get(f"/api/business/orders/{ORDER_NO}/peer-direct-assignment/").json()
        self.assertEqual((peer["data"]["studios"], peer["data"]["pending"]), ([], None))
        local = self.post(f"/api/business/orders/{ORDER_NO}/assign/", user_id=self.binding.user_id)
        self.assertIn("正在确认", local["msg"])
        taker = make_logged_in_client(
            username="first-dispatch-hall-taker", groups=("代练员",), nodes=("grab_hall.take_order",),
        )
        claim = taker.post(
            f"/api/business/hall/{ORDER_NO}/claim/", {}, content_type="application/json",
        ).json()
        self.assertIn("正在确认", claim["msg"])
        self.performance.refresh_from_db()
        self.assertIsNone(self.performance.user_id)
        self.assertNotEqual(self.post(**request)["code"], 0)
        self.assertEqual(len(self.writes()), 1)

        reconciled = jidanpu_member.reconcile(self.binding, now=timezone.now())
        self.assertFalse(reconciled.to_launch or reconciled.to_record or reconciled.to_withdraw or reconciled.to_unlist)
        late = jidanpu_member.queue_member_event(self.order, self.binding, kind=jidanpu_member.KIND_WITHDRAW)
        touched = len(self.downstream.calls)
        self.assertEqual(jidanpu_member.consume_event(late, client=self.downstream), "deferred")
        self.assertEqual(len(self.downstream.calls), touched)

        self.downstream.current = {"userId": OLD_NUMBER, "userName": "打手旧", "fee": "12.34"}
        self.assertIn("已有接手人", self.verify(pending_ref=pending, close=True)["msg"])
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.peer_direct_pending)
        self.downstream.current = {"userId": NEW_NUMBER, "userName": "打手新", "fee": "12.34"}
        result = self.verify(pending_ref=pending)
        self.assertEqual(result["data"]["current"]["number"], NEW_NUMBER)
        self.assert_first_saved("23.45")
        self.assertEqual(len(self.writes()), 1)

    def test_first_dispatch_does_not_take_over_external_hall_or_changed_binding(self):
        self.prepare_first(no_link=False)
        request = self.first_request()
        self.link.in_hall = True
        self.link.save(update_fields=["in_hall"])
        self.assertIn("鸡蛋铺大厅", self.post(**request)["msg"])
        self.link.in_hall = False
        self.link.save(update_fields=["in_hall"])
        self.binding.account = "changed"
        self.binding.save(update_fields=["account"])
        self.assertIn("绑定已变化", self.post(**request)["msg"])
        self.assertFalse(self.writes())

    def post(self, path=None, **body):
        if (path is None or path == self.path) and isinstance(body.get("number"), str):
            body.setdefault("name", {OLD_NUMBER: "打手旧", NEW_NUMBER: "打手新"}.get(body["number"], "合成选择打手"))
        return self.client.post(path or self.path, body, content_type="application/json").json()

    def assign(self, number=NEW_NUMBER, expected=OLD_NUMBER, expected_fee="16.50"):
        return self.post(number=number, expected_number=expected, expected_fee=expected_fee)

    def verify(self, client=None, pending_ref=None, *, close=False):
        if pending_ref is None:
            pending_ref = str(self.events().first().pk)
        return (client or self.client).post(
            self.path + "verify/", {"pending_ref": pending_ref, "close_unconfirmed": close}, content_type="application/json",
        ).json()

    def writes(self):
        return [kwargs for _, path, kwargs in self.downstream.calls if path == jidanpu.ASSIGN_PATH]

    def events(self):
        return JidanpuSyncEvent.objects.filter(kind=jidanpu_direct_taker.KIND, target_ref=ORDER_NO)

    def test_reopening_taker_dialog_reuses_verified_lookup_but_refresh_reads_live(self):
        first = self.client.get(self.path).json()["data"]
        self.assertEqual(first["current"]["number"], OLD_NUMBER)
        self.assertEqual([path for _, path, _ in self.downstream.calls], [jidanpu.ORDER_LIST_PATH, jidanpu.ASS_LOGS_PATH])

        self.downstream.calls.clear()
        self.assertEqual(self.client.get(self.path).json()["data"], first)
        self.assertEqual(self.downstream.calls, [])

        self.downstream.current = {"userId": NEW_NUMBER, "userName": "合成新打手", "fee": "16.50"}
        refreshed = self.client.get(self.path, {"refresh": "true"}).json()["data"]
        self.assertEqual(refreshed["current"]["number"], NEW_NUMBER)
        self.assertEqual([path for _, path, _ in self.downstream.calls], [jidanpu.ORDER_LIST_PATH, jidanpu.ASS_LOGS_PATH])
        self.assertEqual(self.client.get(self.path).json()["data"], refreshed)

    def test_reopening_after_reassignment_shows_new_taker(self):
        self.assertEqual(self.client.get(self.path).json()["data"]["current"]["number"], OLD_NUMBER)
        self.assertEqual(self.assign()["code"], 0)
        self.assertEqual(self.client.get(self.path).json()["data"]["current"]["number"], NEW_NUMBER)

    def test_taker_lookup_and_operation_do_not_cross_tenants_with_matching_links(self):
        from apps.platform.models import Tenant

        self.assertEqual(self.client.get(self.path).json()["data"]["current"]["number"], OLD_NUMBER)
        operation_id = "f57793ef-6ebc-4535-9078-456dbc8795e8"
        submission = dict(operation_id=operation_id, number=NEW_NUMBER, expected_number=OLD_NUMBER, expected_fee="16.50")
        self.assertEqual(self.post(**submission)["data"]["operation_status"], "working")
        try:
            connection.set_tenant(Tenant.objects.get(schema_name="jidanpu_direct_other"))
            game = Game.objects.create(name="对照工作室测试游戏")
            client = make_logged_in_client(
                domain=OTHER_DOMAIN, username="other-cache-test-service",
                groups=("测试客服",), nodes=("order.assign_taker",),
            )
            studio = get_user_model().objects.create_user(username="other-cache-test-studio")
            order = Order.objects.create(
                pk=self.order.pk, order_no=ORDER_NO, source_order_no=self.order.source_order_no,
                game=game, status=OrderStatus.IN_PROGRESS, project_text="测试项目", total_price=Decimal("999"),
            )
            Order.objects.filter(pk=order.pk).update(updated_at=self.order.updated_at)
            binding = JidanpuMemberBinding.objects.create(
                pk=self.binding.pk, user=studio, jidanpu_base_url=self.binding.jidanpu_base_url,
                account=self.binding.account, password="placeholder", enabled=True,
            )
            JidanpuMemberOrderLink.objects.create(pk=self.link.pk, binding=binding, order=order, jidanpu_order_id=REMOTE_NO)
            Performance.objects.create(order=order, emp_type=EmpType.MAIN, user=studio)
            downstream = Downstream()
            downstream.current = {"userId": NEW_NUMBER, "userName": "对照工作室打手", "fee": "3.50"}
            with patch.dict(self.bound_sessions, {binding.pk: downstream}):
                result = client.get(self.path).json()["data"]
                operation = client.get(self.path, {"operation_id": operation_id}).json()
            self.assertEqual(result["current"], {"number": NEW_NUMBER, "name": "对照工作室打手"})
            self.assertEqual(operation["code"], 0)
            self.assertIsNone(operation["data"])
        finally:
            connection.set_tenant(self.tenant)

    def test_reads_current_number_and_searches_across_pages(self):
        data = self.client.get(self.path).json()["data"]
        self.assertEqual(data["current"], {"number": OLD_NUMBER, "name": "打手旧"})
        self.assertTrue(data["assignable"])
        self.assertEqual(data["fee"], "16.50")
        for query in ("打手新", NEW_NUMBER):
            self.assertEqual(self.post(self.path + "search/", query=query)["data"]["items"],
                             [{"number": NEW_NUMBER, "name": "打手新"}])
        self.assertEqual(self.post(self.path + "search/", query=OLD_NUMBER)["data"]["items"],
                         [{"number": OLD_NUMBER, "name": "打手旧"}])
        queries = [kwargs["query"] for _, path, kwargs in self.downstream.calls if path == jidanpu.ASSIGNABLE_USERS_PATH]
        self.assertEqual([query["page"] for query in queries], [1, 1, 2, 1])
        self.assertEqual([query["key"] for query in queries], ["打手新", "", "", ""])
        self.assertTrue(all(query["gameName"] == "测试鸡蛋铺游戏" for query in queries))

    def test_assignment_uses_server_link_and_remote_payout_without_modifying_local_order(self):
        self.downstream.default_fee_after_assign = "31.56"
        request = dict(number=NEW_NUMBER, expected_number=OLD_NUMBER, remote_order_id="unrelated", expected_fee="16.50")
        reply = self.post(**request)["data"]
        self.assertEqual(reply["current"], {"number": NEW_NUMBER, "name": "打手新"})
        self.assertIsNone(reply["fee"])
        self.assertFalse(reply["assignable"])
        paths = [path for _, path, _ in self.downstream.calls]
        self.assertEqual(paths[paths.index(jidanpu.ASSIGN_PATH):], [jidanpu.ASSIGN_PATH])
        self.assertEqual(self.client.get(self.path).json()["data"]["fee"], "31.56")
        self.assertEqual(self.post(**request)["data"]["fee"], "31.56")
        [write] = self.writes()
        self.assertEqual(write["data"]["orderId"], REMOTE_NO)
        self.assertFalse(write["retry_on_sent"])
        [target] = json.loads(write["data"]["strParameter"])
        self.assertEqual(target["userId"], NEW_NUMBER)
        self.assertEqual(target["fee"], 16.50)
        self.assertEqual(target["hours"], "72")
        self.assertEqual(target["project"], "测试项目")
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(Performance.objects.get(pk=self.performance.pk).user_id, self.binding.user_id)
        self.assertEqual(Order.objects.get(pk=self.order.pk).hall_price, Decimal("23.45"))
        audit = OperationLog.objects.get(action="order.jidanpu_direct_assign", order_no=ORDER_NO)
        self.assertEqual(audit.actor, "direct-customer-service")
        self.assertEqual(audit.target_id, ORDER_NO)
        self.assertFalse(audit.target_name or audit.reason or audit.from_value or audit.to_value)
        self.assertNotIn(NEW_NUMBER, audit.summary)

    def test_payout_override_requires_permission_and_checks_default_was_not_changed(self):
        body = {"number": NEW_NUMBER, "name": "打手新", "expected_number": OLD_NUMBER, "expected_fee": "16.50", "fee": "13.50"}
        self.assertEqual(self.client.post(self.path, body, content_type="application/json").status_code, 403)
        editor = make_logged_in_client(
            username="direct-payout-editor", groups=("出款客服",),
            nodes=("order.assign_taker", "order.submit_payout_price"),
        )
        self.downstream.default_fee = "16.51"
        self.assertIn("金额已变化", editor.post(self.path, body, content_type="application/json").json()["msg"])
        self.assertEqual(self.writes(), [])
        body["expected_fee"] = "16.51"
        self.assertEqual(editor.post(self.path, body, content_type="application/json").json()["code"], 0)
        [write] = self.writes()
        self.assertEqual(json.loads(write["data"]["strParameter"])[0]["fee"], 13.50)

    def test_missing_payout_does_not_fall_back_to_either_order_price(self):
        self.downstream.default_fee = None
        self.assertIsNone(self.client.get(self.path).json()["data"]["fee"])
        self.assertIn("没有可用的默认金额", self.assign(expected_fee=None)["msg"])
        self.assertEqual(self.writes(), [])

    def test_missing_link_and_official_key_only_never_write(self):
        self.link.delete()
        status = self.client.get(self.path).json()["data"]
        self.assertTrue(status["bound"])
        self.assertIn("尚未同步", status["reason"])
        self.assertIn("尚未同步", self.assign()["msg"])
        self.binding.mode = JidanpuMemberBinding.Mode.OPENAPI
        self.binding.save(update_fields=["mode"])
        self.assertIn("网页账号", self.assign()["msg"])
        self.assertEqual(self.downstream.calls, [])
        self.assertFalse(self.events().exists())

    def test_wrong_previous_number_blocks_and_disabled_candidate_uses_remote_rejection(self):
        self.assertIn("已变化", self.assign(expected="123")["msg"])
        self.assertEqual(self.writes(), [])
        self.downstream.result = "rejected"
        self.downstream.rejection_message = "该打手已停用，不能接单"
        self.assertEqual(self.assign(number="111")["msg"], "该打手已停用，不能接单")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.FAILED)

    def test_changed_remote_assignee_before_submission_is_not_overwritten(self):
        self.downstream.change_on_order_read = {"userId": "123", "userName": "已换别人", "fee": "13.50"}
        self.assertIn("已变化", self.assign()["msg"])
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.events().exists())

    def test_posted_identifier_must_be_a_string(self):
        reply = self.assign(number=int(NEW_NUMBER))
        self.assertNotEqual(reply["code"], 0)
        self.assertEqual(self.downstream.calls, [])

    def test_rejection_reports_failure_and_keeps_existing_assignee(self):
        self.downstream.result = "rejected"
        self.assertIn("拒绝", self.assign()["msg"])
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.FAILED)
        self.assertEqual(self.client.get(self.path).json()["data"]["current"]["number"], OLD_NUMBER)

    def test_unknown_response_blocks_repeats_until_positive_readback(self):
        self.downstream.result = "lost"
        for _ in range(2):
            self.assertIn("尚未确认", self.assign()["msg"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.PENDING)
        paths = [path for _, path, _ in self.downstream.calls]
        self.assertEqual(paths[paths.index(jidanpu.ASSIGN_PATH):], [jidanpu.ASSIGN_PATH, jidanpu.ORDER_LIST_PATH])
        self.downstream.current = {"userId": OLD_NUMBER, "userName": "打手新", "fee": "16.50"}
        check_start = len(self.downstream.calls)
        self.assertIn("尚未与原指派一致", self.verify()["msg"])
        checked_paths = [path for _, path, _ in self.downstream.calls[check_start:]]
        self.assertEqual(checked_paths, [jidanpu.ORDER_LIST_PATH, jidanpu.ASS_LOGS_PATH])
        self.downstream.current = {"userId": NEW_NUMBER, "userName": "打手新", "fee": "16.51"}
        self.assertTrue(self.client.get(self.path).json()["data"]["pending"])
        self.assertIn("尚未与原指派一致", self.verify()["msg"])
        self.downstream.current["fee"] = "16.50"
        self.downstream.default_fee = "21.20"
        verifier = make_logged_in_client(
            username="different-verifier", groups=("核对客服",), nodes=("order.assign_taker",),
        )
        verified = self.verify(verifier)["data"]
        self.assertEqual(verified["current"]["number"], NEW_NUMBER)
        self.assertEqual(verified["fee"], "21.20")
        self.assertFalse(verified["pending"])
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(OperationLog.objects.get(action=jidanpu_direct_taker.AUDIT_ACTION).actor, "direct-customer-service")
        self.downstream.result = "success"
        self.assertEqual(self.assign(number=OLD_NUMBER, expected=NEW_NUMBER, expected_fee="21.20")["code"], 0)
        self.assertEqual(len(self.writes()), 2)

    def test_old_downstream_pending_cannot_use_new_takers_binding_or_block_their_link(self):
        self.downstream.result = "lost"
        self.assertIn("尚未确认", self.assign()["msg"])
        original_event = self.events().get()
        replacement = get_user_model().objects.create_user(username="alternative-downstream-studio")
        binding = JidanpuMemberBinding.objects.create(
            user=replacement, jidanpu_base_url="https://replacement.test.invalid",
            account="second", password="placeholder", enabled=True,
        )
        remote = Downstream()
        remote.remote_no = "20261118000000000082"
        self.bound_sessions[binding.pk] = remote
        JidanpuMemberOrderLink.objects.create(binding=binding, order=self.order, jidanpu_order_id=remote.remote_no)
        self.performance.user = replacement
        self.performance.save(update_fields=["user"])
        self.assertIn("没有待核对", self.verify()["msg"])
        self.assertEqual(remote.calls, [])
        self.assertFalse(self.client.get(self.path).json()["data"]["pending"])
        self.assertEqual(self.assign()["code"], 0)
        original_event.refresh_from_db()
        self.assertEqual(original_event.status, JidanpuSyncEvent.Status.PENDING)
        for _, path, args in remote.calls:
            if path == jidanpu.ASSIGN_PATH:
                self.assertEqual(args["data"]["orderId"], remote.remote_no)

        remote.result = "lost"
        self.assertIn("尚未确认", self.assign(number=OLD_NUMBER, expected=NEW_NUMBER)["msg"])
        before = len(remote.calls)
        self.assertIn("没有待核对", self.verify(pending_ref=str(original_event.pk))["msg"])
        self.assertEqual(len(remote.calls), before)

    def test_rebinding_converges_old_event_and_verify_does_not_use_new_identity(self):
        self.downstream.result = "lost"
        self.assertIn("尚未确认", self.assign()["msg"])
        self.binding.account = "changed-account"
        self.binding.save(update_fields=["account"])
        call_count = len(self.downstream.calls)
        self.assertIn("没有待核对", self.verify()["msg"])
        self.assertEqual(len(self.downstream.calls), call_count)
        self.assertEqual(_converge_in_flight_events(self.binding), 1)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)

    def test_new_login_can_read_old_link_but_cannot_assign_another_recorders_order(self):
        self.binding.account = "changed-account"
        self.binding.save(update_fields=["account"])
        self.downstream.login_uid = "672"
        self.assertEqual(self.client.get(self.path).json()["data"]["current"]["number"], OLD_NUMBER)
        self.assertIn("录单人无法核实", self.assign()["msg"])
        self.assertIn("录单人无法核实", self.post(self.path + "search/", query="打手新")["msg"])
        self.assertFalse(self.events().exists())
        self.assertEqual(self.writes(), [])
        # Read access can grow after rebind without conferring write rights.
        self.downstream.ignore_recorder_filter = True
        self.assertIn("录单人无法核实", self.assign()["msg"])
        self.assertEqual(self.writes(), [])

    def test_crash_before_send_can_be_cleared_without_repeating_remote_write(self):
        with patch.object(jidanpu_direct_taker, "_start_sending", side_effect=jidanpu_direct_taker.AssignmentError("合成中断")):
            self.assertIn("合成中断", self.assign()["msg"])
        self.assertEqual(self.events().get().payload["phase"], "reserved")
        self.assertEqual(self.writes(), [])
        self.assertIsNone(self.verify()["data"]["pending"])
        self.assertIn("未发出", self.events().get().reason)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.assign()["code"], 0)
        self.assertEqual(len(self.writes()), 1)

    def test_operator_can_close_unconfirmed_request_without_repeating_remote_write(self):
        self.downstream.result = "lost"
        self.assertIn("尚未确认", self.assign()["msg"])
        self.assertIn("尚未与原指派一致", self.verify()["msg"])
        self.assertIsNone(self.verify(close=True)["data"]["pending"])
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(OperationLog.objects.get(action=jidanpu_direct_taker.AUDIT_ACTION).reason,
                         "人工核对后结束等待，未确认远端成功。")
        self.assertEqual(len(self.writes()), 1)

    def test_binding_and_order_changes_refused_while_send_lock_is_held(self):
        secondary = connection.copy()
        try:
            with secondary.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock_shared(%s)", [jidanpu_assignment_lock._key("binding", self.binding.pk)])
                cursor.execute("SELECT pg_advisory_lock(%s)", [jidanpu_assignment_lock._key("order", self.order.pk)])
            configure = {"jidanpu_base_url": self.binding.jidanpu_base_url, "account": "changed-account", "enabled": False}
            result = self.owner_client.put("/api/business/member/jidanpu-binding/", configure, content_type="application/json").json()
            self.assertIn("指派正在发送", result["msg"])
            replacement = "direct-replacement"
            make_logged_in_client(username=replacement)
            replacement_id = get_user_model().objects.get(username=replacement).pk
            order_path = f"/api/business/orders/{ORDER_NO}/"
            for state in (OrderStatus.IN_PROGRESS, OrderStatus.PAUSED, OrderStatus.ISSUE):
                with self.subTest(state=state):
                    self.order.status = state
                    self.order.save(update_fields=["status"])
                    result = self.client.post(order_path + "assign/", {"user_id": replacement_id}, content_type="application/json").json()
                    self.assertIn("正在向鸡蛋铺发送", result["msg"])
            self.order.status = OrderStatus.IN_PROGRESS
            self.order.save(update_fields=["status"])
            self.assertIn("正在向鸡蛋铺发送", self.client.post(order_path + "clear-taker/").json()["msg"])
            self.performance.refresh_from_db()
            self.binding.refresh_from_db()
            self.assertTrue(self.binding.enabled)
            self.assertEqual(self.performance.user_id, self.binding.user_id)
            with jidanpu_assignment_lock.sending(binding_id=self.binding.pk, order_id=self.order.pk + 1) as busy_reason:
                self.assertEqual(busy_reason, "")
        finally:
            secondary.close()

    def test_binding_and_order_are_protected_after_reserve_before_remote_post(self):
        reserve = jidanpu_direct_taker._reserve
        secondary = connection.copy()
        self.addCleanup(secondary.close)

        def reserve_then_compete(*args, **kwargs):
            event = reserve(*args, **kwargs)
            self.assertEqual(self.writes(), [])
            with secondary.cursor() as cursor:
                for scope, pk in (("binding", self.binding.pk), ("order", self.order.pk)):
                    cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [jidanpu_assignment_lock._key(scope, pk)])
                    self.assertIs(cursor.fetchone()[0], False)
            return event

        with patch.object(jidanpu_direct_taker, "_reserve", side_effect=reserve_then_compete):
            self.assertEqual(self.assign()["code"], 0)
        self.assertEqual(len(self.writes()), 1)

    def test_real_edit_and_price_endpoints_share_the_send_lock(self):
        new_game = Game.objects.create(name="另一测试游戏")
        editor = make_logged_in_client(
            username="direct-order-editor", groups=("改单客服",),
            nodes=("order.add_edit", "order.set_offer_price", "order.submit_payout_price"),
        )
        order_path = f"/api/business/orders/{ORDER_NO}/"
        price_data = {"price": "35.00"}
        secondary = connection.copy()
        try:
            with secondary.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [jidanpu_assignment_lock._key("order", self.order.pk)])
            for data in (
                {"source_order_no": "SOURCE-CHANGED"},
                {"game": new_game.pk},
                {"total_price": "999.00", "source_order_no": "SOURCE-WITH-PRICE"},
            ):
                response = editor.patch(order_path, data, content_type="application/json").json()
                self.assertIn("正在向鸡蛋铺发送", response["msg"], response)
            price = editor.post(order_path + "hall-price/", price_data, content_type="application/json").json()
            self.assertIn("正在向鸡蛋铺发送", price["msg"], price)
            batch = editor.post(
                "/api/business/orders/batch/hall-price/",
                {**price_data, "order_nos": [ORDER_NO], "dry_run": False},
                content_type="application/json",
            ).json()
            self.assertIn("正在向鸡蛋铺发送", json.dumps(batch, ensure_ascii=False))
            adjusted = editor.post(
                f"/api/business/performances/{self.performance.pk}/adjust-amount/",
                {"amount_kind": "temp", "amount": "35.00"}, content_type="application/json",
            ).json()
            self.assertIn("正在向鸡蛋铺发送", adjusted["msg"], adjusted)
            self.assertIn("正在向鸡蛋铺发送", dailiantong_push.ensure_internal_hall(
                self.order, operator=None, hall_price=Decimal("35.00"),
            ))
            unrelated = editor.patch(order_path, {"remark": "无关字段可改"}, content_type="application/json").json()
            self.assertEqual(unrelated["code"], 0, unrelated)
        finally:
            secondary.close()
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.game_id, new_game.pk)
        self.assertEqual((self.order.source_order_no, self.order.hall_price, self.order.remark),
                         ("SOURCE-1", Decimal("23.45"), "无关字段可改"))
        unlocked = editor.patch(order_path, {"source_order_no": "SOURCE-CHANGED"}, content_type="application/json").json()
        self.assertEqual(unlocked["code"], 0, unlocked)

    def test_context_change_before_send_is_closed_without_remote_write(self):
        reserve = jidanpu_direct_taker._reserve

        def reserve_then_change_link(*args, **kwargs):
            event = reserve(*args, **kwargs)
            JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(jidanpu_order_id="20261118000000000090")
            return event

        with patch.object(jidanpu_direct_taker, "_reserve", side_effect=reserve_then_change_link):
            self.assertIn("已变化", self.assign()["msg"])
        self.assertIn("未发出", self.events().get().reason)
        self.assertEqual(self.events().get().status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(self.writes(), [])

    def test_anonymous_and_staff_without_assignment_permission_cannot_read_or_write(self):
        without_node = make_logged_in_client(username="direct-viewer", nodes=("order.list",))
        for client, expected in ((Client(SERVER_NAME=DOMAIN), 401), (without_node, 403)):
            self.assertEqual(client.get(self.path).status_code, expected)
            self.assertEqual(client.post(self.path + "verify/").status_code, expected)
            self.assertEqual(client.post(self.path, {"number": NEW_NUMBER, "name": "打手新", "expected_number": OLD_NUMBER, "expected_fee": "16.50"},
                                         content_type="application/json").status_code, expected)
        self.assertEqual(self.downstream.calls, [])

    def test_other_tenant_credentials_cannot_access_this_link(self):
        self.prepare_first()
        first_request = self.first_request()
        self.downstream.calls.clear()
        try:
            with connection.cursor():
                from apps.platform.models import Tenant
                connection.set_tenant(Tenant.objects.get(schema_name="jidanpu_direct_other"))
                Game.objects.create(name="其他工作室游戏")
                other = make_logged_in_client(
                    domain=OTHER_DOMAIN, username="another-tenant-service",
                    groups=("测试客服",), nodes=("order.assign_taker", "order.submit_payout_price"),
                )
            for response in (
                other.get(self.path),
                other.post(self.path + "verify/"),
                other.post(self.path + "search/", {"query": "打手新", "studio_id": self.binding.pk},
                           content_type="application/json"),
                other.post(self.path, first_request, content_type="application/json"),
                other.post(self.path, {"number": NEW_NUMBER, "name": "打手新", "expected_number": OLD_NUMBER, "expected_fee": "16.50"},
                           content_type="application/json"),
            ):
                self.assertEqual(response.json()["code"], 1002)
                self.assertNotIn(OLD_NUMBER, response.content.decode())
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(self.downstream.calls, [])
