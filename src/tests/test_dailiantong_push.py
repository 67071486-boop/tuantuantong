"""代练通抛单：前置闸 / 真发单落库 / 回读状态 / 部分唯一索引 / 接口 / 租户隔离 / 审计。

**全程不访问真实网络**：上游一律用 `httpx.MockTransport` 塞进 `DailiantongClient`
（服务层**唯一**客户端构造点是 `dailiantong_push._open_client`，本文件 patch 的正是它
——⛔ 不是 patch 定义 `DailiantongClient` 的那个模块，见 `AGENTS.md` §5-E）；每次抛单
新建客户端（服务层 `finally` 关闭），所以工厂函数每次返回新实例。

**判据口径**：写操作断言**数据库终态**（改完 `refresh_from_db` / 重新查库，§5-G）；
前置闸断言「没调写上游」（前四道闸连 `UserMoney` 都不该出现，余额是第 5 道）；
超时只调一次 `LevelOrderAdd`（**真钱红线**：写动作永不重试）；凭据不回显 =
把响应 JSON / 审计各列拼成一句搜 `app_secret` / 支付密码明文 / 其 md5；夹具一律**合成值**（PII 守卫扫 `src/**`）。
"""
import hashlib
import io
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import parse_qsl

import httpx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.accounts.models import StaffProfile
from apps.business import dailiantong_push as push_module
from apps.business.claim import claim_order
from apps.business.dailiantong_actions import clear_withdraw_defaults_cache
from apps.business.dailiantong_client import DailiantongClient, DailiantongConfig
from apps.business.dailiantong_push import (
    DailiantongPushError,
    build_actors,
    build_title,
    clear_zones_cache,
    display_status_for_remote,
    mark_unknown_push_failed,
    push_order,
    reclaim_stale_pending_pushes,
    refresh_push_status,
)
from apps.business.models import (
    DailiantongPush,
    DailiantongZoneMap,
    EmpType,
    Game,
    GameServer,
    Order,
    OrderStatus,
    Performance,
    PlatformIntegration,
    PlatformPushSettings,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

#: 合成凭据（⛔ 不是任何真实密钥）。
APP_ID = "synth-dlt-app-0001"
APP_SECRET = "synth-dlt-secret-9f3a"
PAY_PASSWORD = "synth-pay-pw-71c"
PAY_PASSWORD_MD5 = hashlib.md5(PAY_PASSWORD.encode("utf-8")).hexdigest()

#: 合成上游单号（实测是 20 位纯数字；这里用不会撞真实单的合成串）。
SERIAL_OK = "19000000000000000001"

#: 合成上游业务失败原文（用来把「业务失败」与「超时」两种失败区分开）。
UPSTREAM_BUSINESS_ERR = "可操作资金不足，不能发布."

ZONE_ID = "100100010000000"
ORDER_NO = "20261026000000000001"

#: **读**接口的 RBAC 节点（与视图里那个模块常量同值；测试里写死字面量）。
NODE = "order.list"
#: ⭐ M5（审计）：**写**接口的专属节点（抛单 = 真钱冻结）。
PLATFORM_WRITE_NODE = "order.platform_write"
HALL_PUBLISH_NODE = "order.send_to_grab_hall"

URL_PUSH = f"/api/business/orders/{ORDER_NO}/push-to-platform/"
URL_LIST = f"/api/business/orders/{ORDER_NO}/platform-push/"
URL_ZONES = "/api/business/platform-integrations/zones/"


def _push_kwargs(**overrides):
    kwargs = {
        "price": Decimal("5.00"),
        "ensure1": Decimal("0.00"),
        "ensure2": Decimal("0.00"),
        "time_limit_hours": 24,
        "zone_server_id": ZONE_ID,
    }
    kwargs.update(overrides)
    return kwargs


class FakeDailiantong:
    """一个假上游：记录每次调用，按 Action 回不同的信封。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.balance = "100.00"
        self.add_response: dict = {"Result": 1, "Err": SERIAL_OK}
        self.add_timeout = False
        #: 详情回读超时（读面兜底用例：上游挂了也不许 500）。
        self.detail_timeout = False
        self.detail: dict = {"Status": 12, "CancelStatus": 10, "AcceptUID": "USR-SYNTH-TAKER"}
        #: 写动作后的下一次详情回包（按顺序弹；空 ⇒ 一直用 `self.detail`）。
        self.next_details: list[dict] = []
        #: `LevelOrderDelSelf` 的回包与「删掉的单号」（删后详情回查不到）。
        self.delete_response: dict = {"Result": 1, "Err": "删除成功."}
        self.deleted: set[str] = set()
        self.zone_rows: list[dict] = [
            {"ZoneServerID": "1001", "ServerName": "英雄联盟/电信/艾欧尼亚"},
            {"ZoneServerID": "2002", "ServerName": "王者荣耀/安卓QQ区"},
        ]

    @property
    def actions(self) -> list[str]:
        return [action for action, _form in self.calls]

    def forms_for(self, action: str) -> list[dict]:
        return [form for called, form in self.calls if called == action]

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        action = form.get("Action", "")
        self.calls.append((action, form))
        if action == "UserMoney":
            return httpx.Response(
                200,
                json={"SumBal": self.balance, "FreezeBal": "0.00", "ActiveBal": self.balance},
            )
        if action == "LevelOrderAdd":
            if self.add_timeout:
                raise httpx.ReadTimeout("simulated timeout")
            return httpx.Response(200, json=self.add_response)
        if action == "LevelOrderDetail":
            if self.detail_timeout:
                raise httpx.ReadTimeout("simulated detail timeout")
            serial = form.get("ODSerialNo", "")
            if serial in self.deleted:
                return httpx.Response(
                    200, json={"Result": -1, "Err": "没有找到该订单！"}
                )
            if self.next_details:
                return httpx.Response(200, json=self.next_details.pop(0))
            return httpx.Response(200, json=self.detail)
        if action == "LevelOrderCancel":
            return httpx.Response(200, json={"Result": 1, "Err": "撤销成功."})
        if action == "LevelOrderDelSelf":
            serial = form.get("ODSerialNo", "")
            if self.delete_response.get("Result") == 1:
                self.deleted.add(serial)
            return httpx.Response(200, json=self.delete_response)
        if action == "gamezoneserver":
            return httpx.Response(200, json={"gamezoneserver": self.zone_rows})
        return httpx.Response(200, json={"Result": -101, "Err": "未知动作"})


class _PushTestBase(TenantTestCase):
    """租户 A = `test` schema；每例造一个游戏 / 区服 / 待抢单订单 / 启用默认配置。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        clear_zones_cache()
        # 撤销弹窗的默认金额有 30 秒模块级缓存（键 = schema + 上游单号）⇒ 用例之间清掉。
        clear_withdraw_defaults_cache()
        self.addCleanup(clear_withdraw_defaults_cache)
        self.user_model = get_user_model()
        self.operator = self.user_model.objects.create_user(
            username="push-operator", password="synth-pw-0001", first_name="推送员"
        )
        operator_role, _ = Group.objects.get_or_create(name="发单员")
        self.operator.groups.add(operator_role)
        grant_nodes(self.operator, HALL_PUBLISH_NODE)
        self.game = Game.objects.create(name="王者荣耀")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )
        self.integration = PlatformIntegration.objects.create(
            platform=10,
            account="USR-SYNTH-PUSH-01",
            app_id=APP_ID,
            app_secret=APP_SECRET,
            pay_password_md5=PAY_PASSWORD_MD5,
            is_enabled=True,
            is_default=True,
            contact_phone="",
            contact_qq="",
        )
        self.fake = FakeDailiantong()

    # ── 打桩（⛔ 不碰真实网络）────────────────────────────────────────
    def _client_factory(self, integration):
        return DailiantongClient(
            DailiantongConfig(
                app_id=integration.app_id,
                app_secret=integration.app_secret,
                user_id=integration.account,
                pay_password_md5=integration.pay_password_md5,
            ),
            transport=httpx.MockTransport(self.fake.handler),
            clock=lambda: 1_790_000_000,
        )

    def _patched_client(self):
        from unittest import mock

        return mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        )

    def _call_push(self, **overrides):
        with self._patched_client():
            return push_order(self.order, operator=self.operator, **_push_kwargs(**overrides))

    def _gate_message(self, **overrides):
        """跑一次抛单并返回中文文案（前置闸必须抛 `DailiantongPushError`）。"""
        with self.assertRaises(DailiantongPushError) as ctx:
            self._call_push(**overrides)
        return str(ctx.exception)

    # ── 小工具 ──────────────────────────────────────────────────────
    def _make_push(self, **overrides):
        payload = {
            "order": self.order,
            "platform": 10,
            "state": DailiantongPush.State.SENT,
            "price": Decimal("5.00"),
            "ensure1": Decimal("0.00"),
            "ensure2": Decimal("0.00"),
            "time_limit_hours": 24,
            "ex_order_no": self.order.order_no,
            "attempts": 1,
        }
        payload.update(overrides)
        return DailiantongPush.objects.create(**payload)

    def _audit_rows(self):
        return OperationLog.objects.filter(action="order.dailiantong_push")


class PushGateTests(_PushTestBase):
    """五条前置闸：各一条、断言中文文案、断言没调写上游。"""

    def test_only_dailiantong_is_supported(self):
        message = self._gate_message(platform=30)
        self.assertIn("代练通", message)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_gate_1_missing_integration_points_to_the_config_page(self):
        PlatformIntegration.objects.all().delete()
        message = self._gate_message()
        self.assertIn("交易平台对接设置", message)
        self.assertIn("启用且默认", message)
        self.assertEqual(self.fake.calls, [], "没有配置时不该调用任何上游动作")
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_gate_1b_incomplete_credentials_names_the_missing_fields_only(self):
        # ⚠️ 判据要**非恒真**：留一个真实存在的密钥值（sentinel），证明错误文案只报字段名、
        # ⛔ 不把已配置的值顺手打出来。
        sentinel = "synth-secret-must-not-leak-0002"
        self.integration.app_id = ""
        self.integration.app_secret = sentinel
        self.integration.save(update_fields=["app_id", "app_secret"])
        message = self._gate_message()
        self.assertIn("AppId", message)
        self.assertNotIn("AppSecret", message, "AppSecret 已配置，不该被列为缺失字段")
        self.assertNotIn(sentinel, message)
        self.assertNotIn(PAY_PASSWORD_MD5, message)
        self.assertEqual(self.fake.calls, [])

    def test_gate_2_order_status_must_be_waiting_grab(self):
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        message = self._gate_message()
        self.assertIn("待抢单", message)
        self.assertIn("代练中", message, "文案里要带上订单当前状态")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_gate_2b_refunded_order_without_active_push_can_be_rethrown(self):
        """⭐ 2026-10-28（业务方）：已退单 + 代练通无活跃订单 ⇒ 允许重抛，成功后变待抢单。"""
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])

        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 1)

    def test_gate_2c_refunded_order_with_an_active_push_is_still_a_duplicate(self):
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])
        self._make_push(serial_no="19000000000000000123")

        message = self._gate_message()

        self.assertIn("19000000000000000123", message)
        self.assertIn("不能重复抛单", message)
        self.assertEqual(self.fake.actions, [])

    def test_gate_2d_completed_order_is_still_rejected_with_a_chinese_reason(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status"])
        message = self._gate_message()
        self.assertIn("已完成", message)
        self.assertIn("才能抛到代练通", message)
        self.assertEqual(self.fake.actions, [])

    def test_gate_3_price_below_two_yuan(self):
        """⭐ 2026-10-28 复核：全局下限仍是 2 元（区服粒度门槛交给上游，见 MIN_PRICE 注释）。"""
        message = self._gate_message(price=Decimal("1.99"))
        self.assertIn("2.00", message)
        self.assertIn("1.99", message)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_gate_4_existing_active_push_is_rejected_with_its_serial(self):
        self._make_push(serial_no="19000000000000000099")
        message = self._gate_message()
        self.assertIn("19000000000000000099", message)
        self.assertIn("不能重复抛单", message)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 1)

    def test_gate_4b_unknown_push_says_check_manually(self):
        self._make_push(state=DailiantongPush.State.UNKNOWN, serial_no=None)
        message = self._gate_message()
        self.assertIn("状态未知", message)
        self.assertIn("人工核对", message)
        self.assertEqual(self.fake.calls, [])

    def test_gate_5_insufficient_balance_reports_the_gap(self):
        self.fake.balance = "3.00"
        message = self._gate_message(price=Decimal("5.00"))
        self.assertIn("余额不足", message)
        self.assertIn("需要 5.00 元", message)
        self.assertIn("当前可用 3.00 元", message)
        self.assertIn("还差 2.00 元", message)
        # 余额检查是唯一会打上游的前置闸，但**不允许**进到发单动作。
        self.assertEqual(self.fake.actions, ["UserMoney"])
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 0)
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_gate_5b_balance_message_never_uses_four_decimal_places(self):
        self.fake.balance = "1.23456"
        message = self._gate_message(price=Decimal("2.00"))
        self.assertNotIn("1.23456", message)
        self.assertIn("1.23", message)


class PushSuccessTests(_PushTestBase):
    """成功路径：载荷 / 落库 / 区服对照 / 审计。"""

    def test_success_stores_serial_and_zone_mapping(self):
        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertEqual(push.serial_no, SERIAL_OK)
        self.assertEqual(push.remote_status, 11, "实测发单成功后是 Status=11（未接手）")
        self.assertEqual(push.last_error, "")
        self.assertEqual(push.ex_order_no, self.order.order_no)
        self.assertEqual(push.attempts, 1)
        self.assertEqual(push.price, Decimal("5.00"))

        zone = DailiantongZoneMap.objects.get(game=self.game, server=self.server)
        self.assertEqual(zone.zone_server_id, ZONE_ID)

        self.assertEqual(self.fake.actions, ["UserMoney", "LevelOrderAdd"])
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 1)

    def test_payload_satisfies_the_upstream_length_rules(self):
        self._call_push()
        form = self.fake.forms_for("LevelOrderAdd")[0]

        title = form["Title"]
        self.assertTrue(
            2 <= len(title) <= 128, f"标题长度 {len(title)} 不在 2–128：{title!r}"
        )
        # ⭐ 标题不再自动加游戏名（游戏在「游戏 / 平台」里选过）。
        self.assertNotIn(self.game.name, title)
        self.assertIn("星耀上王者", title)

        actors = form["Actors"]
        parts = actors.split("|*|")
        self.assertEqual(len(parts), 5, actors)
        self.assertEqual(len(parts[0]), 8, "账号占位必须 8 位（实测长度校验）")
        self.assertEqual(
            parts[0],
            build_actors(self.order).split("|*|")[0],
            "账号必须来自订单派生（⛔ 固定账号会撞「同一游戏账号不能重复发」）",
        )
        self.assertEqual(len(parts[1]), 8, "密码占位必须 8 位（实测长度校验）")
        self.assertGreaterEqual(len(parts[2]), 2, "角色名至少 2 字")
        self.assertIn("接单后联系客服获取账号", parts[4])

        self.assertEqual(form["ExOrderNo"], self.order.order_no)
        self.assertEqual(form["ZoneServerID"], ZONE_ID)
        self.assertEqual(form["TimeLimit"], "24")
        self.assertEqual(form["Price"], "5.00")
        self.assertNotIn(APP_SECRET, json.dumps(form, ensure_ascii=False))
        self.assertNotIn(PAY_PASSWORD_MD5, json.dumps(form, ensure_ascii=False))

    def test_two_orders_get_different_actor_accounts(self):
        """真钱风险 I1：固定账号会被上游「同一游戏账号不能重复发」挡住第二单。"""
        other = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no="20261026000000000002",
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
        )
        account_a = build_actors(self.order).split("|*|")[0]
        account_b = build_actors(other).split("|*|")[0]
        self.assertEqual(len(account_a), 8)
        self.assertEqual(len(account_b), 8)
        self.assertNotEqual(account_a, account_b)

    def test_balance_needs_only_the_price_but_the_form_still_carries_both_guarantees(self):
        """⭐ 发单只冻**代练费**（实测）；两份保证金照上送、但不占我方余额。

        两侧都钉：**余额闸**（余额 5.00 + 价格 5.00 + 双金 3+2 ⇒ 必须**放行**，旧实现要
        10.00 会误拒）；**载荷**（`Ensure1` / `Ensure2` 原样上送——打手侧的要求，不是我方冻结额）。
        """
        self.fake.balance = "5.00"
        push = self._call_push(
            price=Decimal("5.00"),
            ensure1=Decimal("3.00"),
            ensure2=Decimal("2.00"),
        )
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertEqual(push.ensure1, Decimal("3.00"))
        self.assertEqual(push.ensure2, Decimal("2.00"))
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form["Price"], "5.00")
        self.assertEqual(form["Ensure1"], "3.00")
        self.assertEqual(form["Ensure2"], "2.00")

    def test_balance_short_of_the_price_by_one_cent_is_rejected_with_the_price_only(self):
        """⭐ A1：差额只按**代练费**算，文案里不再出现「保证金合计」（那是与实测相反的事实）。"""
        self.fake.balance = "4.99"
        message = self._gate_message(
            price=Decimal("5.00"),
            ensure1=Decimal("3.00"),
            ensure2=Decimal("2.00"),
        )
        self.assertIn("需要 5.00 元", message)
        self.assertIn("当前可用 4.99 元", message)
        self.assertIn("还差 0.01 元", message)
        # ⛔ 旧的错文案会写「保证金合计 5.00 元」并把 need 算成 10.00。
        self.assertNotIn("保证金合计", message)
        self.assertNotIn("需要 10.00", message)
        self.assertEqual(self.fake.actions, ["UserMoney"])
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_balance_equality_uses_decimal_math_not_float(self):
        """余额**恰好**等于价格必须放行（`Decimal` 比较，⛔ 不用 float）。"""
        self.fake.balance = "2.00"
        push = self._call_push(
            price=Decimal("2.00"),
            ensure1=Decimal("0.10"),
            ensure2=Decimal("0.20"),
        )
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_overlong_title_is_truncated_when_sent_through_push_order(self):
        """真钱风险 I3：只测 `build_title` 抓不住「服务层没调它」。"""
        self.order.project_text = "超长标题" * 20
        self.order.save(update_fields=["project_text"])
        self._call_push()
        form = self.fake.forms_for("LevelOrderAdd")[0]
        title = form["Title"]
        self.assertTrue(
            2 <= len(title) <= 128,
            f"经 push_order 上送的标题长度 {len(title)} 不在 2–128：{title!r}",
        )
        # ⭐ 标题不再自动加游戏名（游戏已在「游戏 / 平台」里选过）。
        self.assertNotIn(self.game.name, title)
        self.assertTrue(title.startswith("超长标题"))

    def test_success_writes_one_audit_row_with_target_and_no_credentials(self):
        push = self._call_push()
        rows = list(self._audit_rows())
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.target_id, str(push.pk))
        self.assertEqual(row.target_name, self.order.order_no)
        self.assertEqual(row.order_no, self.order.order_no)
        self.assertEqual(row.log_type, 10)
        self.assertIn(self.order.order_no, row.summary)
        self.assertIn("代练通", row.summary)
        self.assertIn(SERIAL_OK, row.reason)

        blob = " ".join(
            str(value)
            for value in (
                row.summary,
                row.reason,
                row.target_id,
                row.target_name,
                row.actor,
                push.last_error,
                repr(push),
            )
        )
        self.assertNotIn(APP_SECRET, blob)
        self.assertNotIn(PAY_PASSWORD, blob)
        self.assertNotIn(PAY_PASSWORD_MD5, blob)

    def test_second_push_after_failed_row_gets_a_new_attempt_number(self):
        self.fake.add_response = {"Result": -2, "Err": UPSTREAM_BUSINESS_ERR}
        first = self._call_push()
        self.assertEqual(first.attempts, 1)

        self.fake.add_response = {"Result": 1, "Err": SERIAL_OK}
        second = self._call_push()
        self.assertEqual(second.attempts, 2)
        self.assertEqual(second.state, DailiantongPush.State.SENT)
        # 历史失败行**保留**（链路可追溯）。
        self.assertEqual(DailiantongPush.objects.count(), 2)


class PushUpstreamFailureTests(_PushTestBase):
    """超时 / 业务失败：⛔ 永不自动重试，且必须落成可区分的状态。"""

    def test_timeout_is_unknown_and_calls_upstream_only_once(self):
        self.fake.add_timeout = True
        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.UNKNOWN)
        self.assertIsNone(push.serial_no)
        self.assertTrue(push.last_error)
        # 两种失败必须可区分：超时**没有上游回包** ⇒ 不可能是上游原文。
        self.assertNotEqual(push.last_error, UPSTREAM_BUSINESS_ERR)
        self.assertIn("请求失败", push.last_error)
        # `unknown`（待人工核对）仍算在途 ⇒ `is_active` 保持 True（口径）。
        self.assertTrue(push.is_active)
        # 真钱红线：写动作只允许一次尝试。
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 1)
        self.assertEqual(self.fake.actions, ["UserMoney", "LevelOrderAdd"])
        self.assertEqual(self._audit_rows().count(), 1)

    def test_business_failure_keeps_the_upstream_message_verbatim(self):
        self.fake.add_response = {"Result": -2, "Err": UPSTREAM_BUSINESS_ERR}
        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.FAILED)
        self.assertIsNone(push.serial_no)
        self.assertEqual(push.last_error, UPSTREAM_BUSINESS_ERR)
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 1)
        audit = self._audit_rows().get()
        self.assertIn("可操作资金不足", audit.reason)

    def test_business_failure_marks_the_push_inactive_and_allows_repush(self):
        """⭐ 2026-10-28 真机 bug：失败行不能算「活跃平台单」（单 20261028105707877853）。"""
        self.fake.add_response = {"Result": -2, "Err": UPSTREAM_BUSINESS_ERR}
        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.FAILED)
        self.assertFalse(push.is_active, "failed 必须同时关掉 is_active")
        self.assertFalse(
            DailiantongPush.objects.filter(order=self.order, is_active=True).exists(),
            "失败行不能让「无活跃推送」判据为假",
        )
        # 状态列回本地：本地状态不再被「已抛代练通」锁住。
        self.assertIsNone(push_module.local_status_locked_denial(self.order))
        # 允许重抛（闸与预览都放行）。
        self.assertIs(push_module._preflight_hard(self.order)[0], True)
        self.fake.add_response = {"Result": 1, "Err": SERIAL_OK}
        second = self._call_push()
        second.refresh_from_db()
        self.assertEqual(second.state, DailiantongPush.State.SENT)
        self.assertEqual(second.attempts, 2)

    def test_success_without_a_serial_is_unknown_not_sent(self):
        self.fake.add_response = {"Result": 1, "Err": ""}
        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.UNKNOWN)
        self.assertIsNone(push.serial_no)
        self.assertIn("没有返回订单号", push.last_error)


class RepairPushFlagsCommandTests(_PushTestBase):
    """一次性修两类历史坏标志；默认 dry-run、都是精确组合。"""

    def test_dry_run_then_apply_fixes_failed_active_and_finished_sent_rows(self):
        # ① failed + is_active=True
        failed = self._make_push(
            serial_no=None,
            state=DailiantongPush.State.FAILED,
            is_active=True,
        )
        # ② 已收口（16/12）但仍 sent + 活跃（换一张单：同单同平台只许一条活跃推送）
        other_order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no="20261027000000000099",
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )
        finished = self._make_push(
            order=other_order,
            serial_no="19000000000000000111",
            state=DailiantongPush.State.SENT,
            remote_status=16,
            remote_cancel_status=12,
            is_active=True,
        )
        # ③ 仍在跑的 16/10：⛔ 不许修
        running = self._make_push(
            serial_no="19000000000000000112",
            state=DailiantongPush.State.SENT,
            remote_status=16,
            remote_cancel_status=10,
            is_active=True,
        )
        schema = self.tenant.schema_name

        out = io.StringIO()
        call_command(
            "dailiantong_repair_push_flags", "--tenant", schema, stdout=out
        )
        failed.refresh_from_db()
        finished.refresh_from_db()
        self.assertTrue(failed.is_active, "dry-run 不许写库")
        self.assertEqual(finished.state, DailiantongPush.State.SENT)
        blob = out.getvalue()
        self.assertIn("failed+active=1", blob)
        self.assertIn("已收口仍活跃=1", blob)

        out = io.StringIO()
        call_command(
            "dailiantong_repair_push_flags",
            "--tenant",
            schema,
            "--apply",
            stdout=out,
        )
        failed.refresh_from_db()
        finished.refresh_from_db()
        running.refresh_from_db()
        self.assertFalse(failed.is_active)
        self.assertEqual(finished.state, DailiantongPush.State.CLOSED)
        self.assertFalse(finished.is_active)
        self.assertIn("已收口", finished.remote_note)
        self.assertEqual(running.state, DailiantongPush.State.SENT)
        self.assertTrue(running.is_active, "16/10 仍算在跑，⛔ 不许修")

        out = io.StringIO()
        call_command(
            "dailiantong_repair_push_flags",
            "--tenant",
            schema,
            "--apply",
            stdout=out,
        )
        self.assertIn("failed+active=0", out.getvalue(), "幂等")
        self.assertIn("已收口仍活跃=0", out.getvalue(), "幂等")


class RePushAfterFinishedPushTests(_PushTestBase):
    """⭐ 2026-10-28 真机：撤销完成的旧推送不算「还在跑」，收口后允许重发。"""

    def _old_push(self, *, status, cancel_status, serial="19000000000000000121"):
        return self._make_push(
            serial_no=serial,
            state=DailiantongPush.State.SENT,
            remote_status=status,
            remote_cancel_status=cancel_status,
            is_active=False,
        )

    def test_finished_cancel_16_12_is_closed_and_allows_repush(self):
        old = self._old_push(status=16, cancel_status=12)
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])

        push = self._call_push()

        old.refresh_from_db()
        self.assertEqual(old.state, DailiantongPush.State.CLOSED)
        self.assertFalse(old.is_active)
        self.assertIn("已撤销完成", old.remote_note)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_settled_status_17_is_closed_and_allows_repush(self):
        old = self._old_push(status=17, cancel_status=10, serial="19000000000000000122")
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])

        push = self._call_push()

        old.refresh_from_db()
        self.assertEqual(old.state, DailiantongPush.State.CLOSED)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_cancelling_16_10_still_blocks_repush(self):
        old = self._old_push(status=16, cancel_status=10)
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])

        message = self._gate_message()

        self.assertIn("撤销中", message)
        self.assertIn("取消撤销", message)
        old.refresh_from_db()
        self.assertEqual(old.state, DailiantongPush.State.SENT, "16/10 ⛔ 不收口")

    def test_unaccepted_and_accepted_without_finish_still_block_repush(self):
        for status, expected in ((11, "不能重复抛单"), (12, "已被代练通接手")):
            with self.subTest(status=status):
                DailiantongPush.objects.all().delete()
                self._old_push(status=status, cancel_status=10)
                self.order.status = OrderStatus.REFUNDED
                self.order.save(update_fields=["status"])
                message = self._gate_message()
                self.assertIn(expected, message)


class PushConstraintTests(_PushTestBase):
    """部分唯一索引：同一 `(order, platform)` 至多一条有效推送（库层，不是 Python 查一遍）。"""

    def test_second_active_push_is_rejected_by_the_database(self):
        self._make_push(serial_no="19000000000000000011")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._make_push(
                    serial_no=None, state=DailiantongPush.State.UNKNOWN
                )

    def test_failed_rows_do_not_block_a_new_push(self):
        self._make_push(serial_no=None, state=DailiantongPush.State.FAILED)
        # 正对照：`failed` 不在部分唯一索引的条件里 ⇒ 再插一条有效推送必须成功。
        push = self._make_push(serial_no="19000000000000000012")
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertEqual(DailiantongPush.objects.count(), 2)


class RefreshPushStatusTests(_PushTestBase):
    def test_remote_status_is_written_back(self):
        push = self._make_push(serial_no=SERIAL_OK)
        with self._patched_client():
            refreshed = refresh_push_status(push)

        refreshed.refresh_from_db()
        self.assertEqual(refreshed.remote_status, 12)
        self.assertEqual(refreshed.remote_cancel_status, 10)
        self.assertIsNotNone(refreshed.last_synced_at)
        self.assertEqual(refreshed.last_error, "")
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_detail_without_both_status_fields_is_an_error_and_records_it(self):
        """⭐ P2-7：`Status` / `CancelStatus` 都读不到 ⇒ 抛错、不清 last_error、不写同步时间。"""
        push = self._make_push(serial_no=SERIAL_OK)
        old_synced = timezone.now() - timedelta(hours=1)
        DailiantongPush.objects.filter(pk=push.pk).update(
            last_error="上一轮的错误",
            last_synced_at=old_synced,
        )
        self.fake.detail = {"Foo": "bar"}

        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                refresh_push_status(push)

        self.assertIn("Status", str(ctx.exception))
        push.refresh_from_db()
        self.assertEqual(push.last_error, str(ctx.exception))
        self.assertEqual(push.last_synced_at, old_synced)
        self.assertNotEqual(push.last_error, "")

    def test_missing_serial_cannot_be_refreshed(self):
        push = self._make_push(
            serial_no=None, state=DailiantongPush.State.UNKNOWN
        )
        with self.assertRaises(DailiantongPushError) as ctx:
            refresh_push_status(push)
        self.assertIn("没有上游订单号", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])


class PendingRecoveryTests(_PushTestBase):
    """⭐ 审计修复（2026-10-27，P1-4）：pending 超时回收。"""

    def test_stale_pending_becomes_unknown_with_a_reason(self):
        push = self._make_push(serial_no=None, state=DailiantongPush.State.PENDING)
        DailiantongPush.objects.filter(pk=push.pk).update(
            updated_at=timezone.now() - timedelta(minutes=6)
        )

        changed = reclaim_stale_pending_pushes()

        self.assertEqual(changed, 1)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.UNKNOWN)
        self.assertIn("pending 状态超过", push.last_error)

    def test_fresh_pending_is_not_reclaimed(self):
        push = self._make_push(serial_no=None, state=DailiantongPush.State.PENDING)

        changed = reclaim_stale_pending_pushes()

        self.assertEqual(changed, 0)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.PENDING)


class ManualUnknownFailureTests(_PushTestBase):
    """⭐ unknown 的人工出口（受审计，动作留痕）。"""

    def _unknown(self):
        return self._make_push(
            serial_no=None, state=DailiantongPush.State.UNKNOWN
        )

    def _run_command(self, push, *, evidence="已登录代练通后台按外部单号核对，没有这张单"):
        return call_command(
            "dailiantong_mark_failed",
            "--tenant",
            self.tenant.schema_name,
            "--push-id",
            str(push.pk),
            "--operator",
            self.operator.username,
            "--evidence",
            evidence,
        )

    def test_unknown_row_is_marked_failed_and_audited(self):
        push = self._unknown()
        DailiantongPush.objects.filter(pk=push.pk).update(last_error="超时")

        self._run_command(push)

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.FAILED)
        self.assertFalse(push.is_active)
        self.assertIn("人工核对后标记失败", push.last_error)
        self.assertIn("没有这张单", push.last_error)
        rows = list(
            OperationLog.objects.filter(action="order.dailiantong_push").order_by("id")
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("没有这张单", rows[0].summary)
        self.assertEqual(rows[0].actor, self.operator.username)

    def test_after_marking_failed_the_order_can_be_pushed_again(self):
        push = self._unknown()
        self._run_command(push)

        # 重新抛单不再被这张 unknown 行挡住（✅ 新链路行可以落库）。
        new_push = self._make_push(serial_no=None, state=DailiantongPush.State.PENDING)
        self.assertNotEqual(new_push.pk, push.pk)
        self.assertFalse(
            DailiantongPush.objects.filter(
                state__in=DailiantongPush.ACTIVE_STATES
            ).exclude(pk=new_push.pk).exists()
        )

    def test_evidence_is_required(self):
        push = self._unknown()

        with self.assertRaises(CommandError) as ctx:
            self._run_command(push, evidence="  ")

        self.assertIn("核对依据", str(ctx.exception))
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.UNKNOWN)

    def test_only_unknown_rows_can_be_marked(self):
        push = self._make_push(serial_no=SERIAL_OK, state=DailiantongPush.State.SENT)

        with self.assertRaises(CommandError):
            self._run_command(push)

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_mark_service_rejects_short_evidence(self):
        push = self._unknown()

        with self.assertRaises(DailiantongPushError):
            mark_unknown_push_failed(
                push, operator=self.operator, evidence="太短"
            )


class TitleTests(_PushTestBase):
    """标题：本地保证 2–128 字（⛔ 不交给上游静默截断），**且不再自动加游戏名**（2026-10-28）。"""

    def test_short_title_is_padded_to_at_least_two_characters(self):
        self.game.name = "单"
        self.order.project_text = ""
        title = build_title(self.order)
        self.assertGreaterEqual(len(title), 2)
        self.assertLessEqual(len(title), 128)

    def test_long_title_is_truncated_to_one_hundred_and_twenty_eight(self):
        self.order.project_text = "很长" * 100
        title = build_title(self.order)
        self.assertEqual(len(title), 128)
        self.assertTrue(title.startswith("很长"))
        self.assertNotIn(self.game.name, title, "30 天 2026-10-28：标题不许带游戏名")

    def test_title_includes_the_order_content_excerpt_and_no_game_name(self):
        """⭐ 业务方要求：标题 = 项目 + 订单内容摘要（内容主放 Memo），⛔ 不含游戏名。"""
        self.order.project_text = "星耀上王者"
        self.order.content = "客户原始需求：只用射手，不要打野，白天在线。"
        self.order.save(update_fields=["project_text", "content"])

        title = build_title(self.order)

        self.assertIn("星耀上王者", title)
        self.assertIn("客户原始需求", title)
        self.assertNotIn(self.game.name, title)
        self.assertLessEqual(len(title), 128)

    def test_title_uses_the_content_excerpt_when_project_text_is_empty(self):
        """项目为空 ⇒ 用内容摘要；仍然⛔ 不拿游戏名充数。"""
        self.order.project_text = ""
        self.order.content = "客户原始需求：只用射手"
        self.order.save(update_fields=["project_text", "content"])

        title = build_title(self.order)

        self.assertEqual(title, "客户原始需求：只用射手")
        self.assertNotIn(self.game.name, title)

    def test_title_falls_back_to_a_neutral_phrase_when_both_are_empty(self):
        self.order.project_text = ""
        self.order.content = ""
        self.order.save(update_fields=["project_text", "content"])

        title = build_title(self.order)

        self.assertEqual(title, "代练订单")
        self.assertNotIn(self.game.name, title)

    def test_whitespace_in_the_excerpt_is_collapsed_for_the_title(self):
        self.order.content = "第一行需求\n第二行需求   第三段"
        self.order.save(update_fields=["content"])
        title = build_title(self.order)
        self.assertNotIn("\n", title)
        self.assertIn("第一行需求 第二行需求 第三段", title)

    def test_long_title_cuts_at_a_punctuation_boundary_not_mid_word(self):
        """⭐ 业务方反馈：硬截会切出「…【群码在订单截」这种半个词。"""
        self.order.project_text = (
            "客户要求只用射手，不要打野，尽量白天上线，群码在订单里，"
            "打完截图发群，验收后结算，不许私下加价，"
        )
        title = build_title(self.order)

        self.assertLessEqual(len(title), 128)
        self.assertGreaterEqual(len(title), 2)
        # 超过 128 ⇒ 落刀在词界上：标题以标点 / 空格收尾（⛔ 不是半个词）。
        self.assertIn(title[-1], push_module.TITLE_BOUNDARY_CHARS)

    def test_long_title_without_any_boundary_falls_back_to_a_hard_cut(self):
        self.order.project_text = "无" * 200
        title = build_title(self.order)
        self.assertEqual(len(title), 128)


class PushApiTests(_PushTestBase):
    """接口层：成功 / 失败 / 403 / 读列表 / 审计。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="push-api-user", groups=("客服",), nodes=(NODE, PLATFORM_WRITE_NODE)
        )

    def _post(self, payload=None, *, client=None):
        return (client or self.client).post(
            URL_PUSH,
            payload if payload is not None else {"price": "5.00", "zone_server_id": ZONE_ID},
            content_type="application/json",
        )

    def test_api_success_returns_the_push_row_and_writes_audit(self):
        with self._patched_client():
            response = self._post()

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        row = response.data["data"]
        self.assertEqual(row["state"], DailiantongPush.State.SENT)
        self.assertEqual(row["serial_no"], SERIAL_OK)
        self.assertEqual(row["last_error"], "")
        self.assertEqual(DailiantongPush.objects.count(), 1)
        self.assertEqual(self._audit_rows().count(), 1)
        self.assertEqual(self._audit_rows().get().target_name, self.order.order_no)

        body = json.dumps(response.data, ensure_ascii=False, default=str)
        for secret in (APP_SECRET, PAY_PASSWORD, PAY_PASSWORD_MD5):
            self.assertNotIn(secret, body)

    def test_api_upstream_failure_returns_code_zero_with_failed_row(self):
        self.fake.add_response = {"Result": -2, "Err": UPSTREAM_BUSINESS_ERR}
        with self._patched_client():
            response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["state"], DailiantongPush.State.FAILED)
        self.assertEqual(
            response.data["data"]["last_error"], UPSTREAM_BUSINESS_ERR
        )
        self.assertEqual(DailiantongPush.objects.count(), 1)

    def test_api_gate_failure_is_a_chinese_business_error(self):
        with self._patched_client():
            response = self._post({"price": "1.00", "zone_server_id": ZONE_ID})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1002, response.data)
        self.assertIn("2.00", response.data["msg"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_api_requires_the_platform_write_node(self):
        """⭐ 抛单 = 真钱冻结 + 建平台单 ⇒ 要 `order.platform_write`。

        两向都钉：① 零节点 ⇒ 403；② **只持 `order.list`**（能看订单）⇒ 同样 403
        ——这正是修复过的洞（改前「能看订单的人就能抛真钱单」）。
        """
        # ⚠️ `groups=()`：`groups=("客服",)` 会让夹具给同名 Role 补节点，而本类 `setUp`
        # 已把 `order.list` 挂到那个 Role 上（同一事务 ⇒ 403 会变成 200）。要挂节点就得有
        # 自己的 Group（判据读 Group→Role→permissions）；`groups=()` + `nodes=…` 挂不上
        # （`_grant_nodes` 遍历已存在的组）。
        for label, groups, nodes in (
            ("no-node", (), ()),
            ("list-only", ("rdlst",), (NODE,)),
        ):
            with self.subTest(label=label):
                self.fake.calls.clear()
                client = make_logged_in_client(
                    username=f"push-api-{label}", groups=groups, nodes=nodes
                )
                with self._patched_client():
                    response = self._post(client=client)
                self.assertEqual(response.status_code, 403, response.content[:400])
                self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 0)

    def test_the_platform_write_node_alone_pushes_without_publishing_to_the_hall(self):
        client = make_logged_in_client(
            username="push-api-write-only", groups=("wonly",),
            nodes=(PLATFORM_WRITE_NODE,),
        )
        self.order.status = OrderStatus.UNASSIGNED
        self.order.hall_price = Decimal("12.00")
        self.order.save(update_fields=["status", "hall_price"])

        with self._patched_client():
            response = self._post(
                {"price": "5.00", "zone_server_id": ZONE_ID, "hall_price_same": "8.00"},
                client=client,
            )
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["state"], DailiantongPush.State.SENT)
        self.assertEqual(self.fake.forms_for("LevelOrderAdd")[0]["Price"], "5.00")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(self.order.hall_price, Decimal("12.00"))
        self.assertIsNone(self.order.hall_publisher_id)
        self.assertFalse(OperationLog.objects.filter(
            action="order.throw_to_hall", order_no=self.order.order_no,
        ).exists())

        push = DailiantongPush.objects.get(order=self.order, serial_no=SERIAL_OK)
        self.fake.detail["Status"] = 11
        with self._patched_client():
            refresh_push_status(push)
        self.order.refresh_from_db()
        self.assertTrue(self.fake.forms_for("LevelOrderDetail"))
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

        self.fake.detail["Status"] = 12
        with self._patched_client():
            refresh_push_status(push)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(
            Performance.objects.get(order=self.order, emp_type=EmpType.MAIN).user.username,
            "dailiantong_taker",
        )

    def test_api_read_endpoints_also_require_the_order_list_node(self):
        """三条接口都挂 `order.list`，读端点同样必须 403（⛔ 只测 POST 会留洞）。

        ⚠️ 必须套 `_patched_client()`：否则「权限门被删掉」的变异会**真打上游**（zones 会回
        真实区服数据），违反「全程不访问真实网络」红线。判据两步：① 先证桩真的装上了
        （否则「没调上游」可能只是假绿）；② 再证 403 的请求**一次都没构造客户端**
        （`_open_client` 零调用 + fake 零动作）。
        """
        client = make_logged_in_client(username="push-api-no-node-read", groups=())
        with self._patched_client() as patched:
            self.assertIs(
                push_module._open_client,
                patched,
                "打桩没装上——这条用例的『没调上游』就会退化成假绿",
            )
            for url in (URL_LIST, URL_ZONES):
                with self.subTest(url=url):
                    response = client.get(url)
                    self.assertEqual(
                        response.status_code, 403, response.content[:400]
                    )
            self.assertEqual(
                patched.call_count, 0, "403 请求不该构造任何上游客户端"
            )
        self.assertEqual(self.fake.calls, [], "无权限的请求不该打到上游")

    def test_api_reads_back_every_push_row_of_the_order(self):
        self._make_push(serial_no="19000000000000000021")
        self._make_push(serial_no=None, state=DailiantongPush.State.FAILED)
        self.fake.detail = {
            "Status": 11,
            "CancelStatus": 10,
            "PayLevelBal": "2.00",
            "RepEnsureBal": "0.50",
        }

        with self._patched_client():
            response = self.client.get(URL_LIST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 0)
        rows = response.data["data"]
        states = sorted(item["state"] for item in rows)
        self.assertEqual(states, ["failed", "sent"])
        # ⭐ 前端契约：最新一条有效推送要带撤销弹窗的两个默认金额。
        sent_row = next(item for item in rows if item["state"] == "sent")
        self.assertEqual(sent_row["pay_level_bal"], "2.00")
        self.assertEqual(sent_row["rep_ensure_bal"], "0.50")
        self.assertEqual(sent_row["withdraw_defaults_error"], "")
        # 历史 / 结束行不额外打上游，两格为空 + 说明。
        failed_row = next(item for item in rows if item["state"] == "failed")
        self.assertIsNone(failed_row["pay_level_bal"])
        self.assertIn("只能手填", failed_row["withdraw_defaults_error"])

    def test_the_read_endpoint_carries_the_display_status_of_the_cancel_codes(self):
        """⭐ 真机 bug：详情读面每行带 `display_status` / `display_status_label`。

        现场：`Status=16`（撤销中）+ `CancelStatus=12`（协商已处理）⇒ 页面「交易状态」
        必须显示**已撤销**，⛔ 不能再显示「撤销中」。前端优先用后端这两个键。
        """
        self._make_push(
            serial_no="19000000000000000023",
            state=DailiantongPush.State.FAILED,
            remote_status=16,
            remote_cancel_status=12,
        )

        response = self.client.get(URL_LIST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 0)
        row = response.data["data"][0]
        self.assertEqual(row["display_status"], "revoked")
        self.assertEqual(row["display_status_label"], "已撤销")
        self.assertEqual(row["remote_status"], 16)
        self.assertEqual(row["remote_cancel_status"], 12)

    def test_the_read_endpoint_is_not_500_when_the_detail_read_fails(self):
        self._make_push(serial_no="19000000000000000021")
        self.fake.detail_timeout = True
        with self._patched_client():
            response = self.client.get(URL_LIST)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 0)
        row = response.data["data"][0]
        self.assertIsNone(row["pay_level_bal"])
        self.assertIn("只能手填", row["withdraw_defaults_error"])

    def test_api_read_of_an_unknown_order_is_a_business_error(self):
        response = self.client.get("/api/business/orders/20261026000000009999/platform-push/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000)
        self.assertEqual(response.data["msg"], "订单不存在或不可见")

    def test_api_rejects_a_blank_zone_without_touching_the_database(self):
        with self._patched_client():
            response = self._post({"price": "5.00", "zone_server_id": ""})
        # 本仓的 DRF 异常处理器把序列化器错误包成 **HTTP 200 + code=1000** 的信封
        # （与 `orders/<no>/refunds` 的既有形状一致）。
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertIn("代练通区服", response.data["msg"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(DailiantongPush.objects.count(), 0)


class ZonesApiTests(_PushTestBase):
    """区服字典代理：关键字过滤 / 按游戏列全部 / 模块级 TTL 缓存。"""

    def setUp(self):
        super().setUp()
        # 模块级缓存跨用例共享 ⇒ 每条自己清一次（否则「按游戏列全部」会读到别的用例的字典）。
        clear_zones_cache()
        self.addCleanup(clear_zones_cache)
        self.client = make_logged_in_client(
            username="zones-api-user", groups=("客服",), nodes=(NODE,)
        )

    def test_zones_are_filtered_by_keyword(self):
        with self._patched_client():
            response = self.client.get(URL_ZONES, {"q": "王者"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 0)
        self.assertEqual(
            response.data["data"],
            [{"zone_server_id": "2002", "server_name": "王者荣耀/安卓QQ区"}],
        )

    def test_zones_are_cached_for_the_ttl_window(self):
        with self._patched_client():
            first = self.client.get(URL_ZONES, {"q": "艾欧尼亚"})
            second = self.client.get(URL_ZONES, {"q": "艾欧尼亚"})
        self.assertEqual(first.data["code"], 0)
        self.assertEqual(second.data["code"], 0)
        self.assertEqual(self.fake.actions.count("gamezoneserver"), 1)

    def test_game_id_returns_all_zones_of_that_game_in_dictionary_order(self):
        """⭐ 业务方反馈：发单页区服要「下拉选」，不只是只能填默认那一个。"""
        self.fake.zone_rows = [
            {"ZoneServerID": "2003", "ServerName": "王者荣耀/苹果QQ区"},
            {"ZoneServerID": "1001", "ServerName": "英雄联盟/电信/艾欧尼亚"},
            {"ZoneServerID": "2002", "ServerName": "王者荣耀/安卓QQ区"},
            {"ZoneServerID": "9", "ServerName": "王者荣耀世界/PC/默认服"},
        ]

        with self._patched_client():
            response = self.client.get(URL_ZONES, {"game_id": self.game.pk})

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.data["code"], 0, response.data)
        # ⭐ B3：**不再前缀匹配** ——
        # 「王者荣耀世界」是**另一个游戏**，不许混进「王者荣耀」的区服列表（更不许当默认）。
        # 判据只剩「游戏名第一段精确同名 / 归一化同名」，顺序仍是字典原序。
        self.assertEqual(
            response.data["data"],
            [
                {"zone_server_id": "2003", "server_name": "王者荣耀/苹果QQ区"},
                {"zone_server_id": "2002", "server_name": "王者荣耀/安卓QQ区"},
            ],
        )

    def test_q_is_ignored_when_game_id_is_given(self):
        """前端契约（2026-10-27）：给了 `game_id` 时返回该游戏**全部**区服，`q` 忽略。"""
        self.fake.zone_rows = [
            {"ZoneServerID": "2003", "ServerName": "王者荣耀/苹果QQ区"},
            {"ZoneServerID": "2002", "ServerName": "王者荣耀/安卓QQ区"},
            {"ZoneServerID": "1001", "ServerName": "英雄联盟/电信/艾欧尼亚"},
        ]

        with self._patched_client():
            response = self.client.get(
                URL_ZONES, {"game_id": self.game.pk, "q": "安卓"}
            )

        self.assertEqual(
            [row["zone_server_id"] for row in response.data["data"]],
            ["2003", "2002"],
        )

    def test_game_id_of_an_unknown_game_returns_an_empty_list(self):
        with self._patched_client():
            response = self.client.get(URL_ZONES, {"game_id": 987654})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"], [])

    def test_malformed_game_id_is_a_business_error_not_a_500(self):
        with self._patched_client():
            response = self.client.get(URL_ZONES, {"game_id": "abc"})

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.data["code"], 1000, response.data)


class PushIsolationTests(TenantTestCase):
    """以租户 A 的凭据访问 B 的推送行必须失败（`AGENTS.md` §4 的强制测试）。"""

    DOMAIN_A = "tenant.test.com"
    DOMAIN_B = "dlt-push-iso-b.test.local"
    SCHEMA_B = "dlt_push_iso_b"
    ORDER_A = "20261026000000000031"
    ORDER_B = "20261026000000000032"
    SERIAL_A = "19000000000000000031"
    SERIAL_B = "19000000000000000032"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="代练通抛单隔离租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        clear_zones_cache()

    def _seed_tenant(self, *, order_no, serial_no, game_name, domain, username):
        """在**当前** schema 里造游戏 / 区服 / 订单 / 推送行，并返回登录 client。"""
        game = Game.objects.create(name=game_name)
        server = GameServer.objects.create(game=game, name="安卓QQ区")
        order = Order.objects.create(
            game=game,
            server=server,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            project_text="上分",
        )
        push = DailiantongPush.objects.create(
            order=order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=serial_no,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=order_no,
            attempts=1,
        )
        client = make_logged_in_client(
            domain=domain, username=username, groups=("客服",),
            nodes=(NODE, PLATFORM_WRITE_NODE),
        )
        return order, push, client

    def _seed(self):
        with tenant_context(self.tenant):
            order_a, push_a, client_a = self._seed_tenant(
                order_no=self.ORDER_A,
                serial_no=self.SERIAL_A,
                game_name="王者荣耀-A",
                domain=self.DOMAIN_A,
                username="dlt-iso-a",
            )
        with tenant_context(self.tenant_b):
            order_b, push_b, client_b = self._seed_tenant(
                order_no=self.ORDER_B,
                serial_no=self.SERIAL_B,
                game_name="王者荣耀-B",
                domain=self.DOMAIN_B,
                username="dlt-iso-b",
            )
        # ⚠️ 两个 schema 的主键序列各自从 1 开始 ⇒ **不能用主键**指认「另一租户那一行」
        # （`AGENTS.md` §5-G：跨租户的否定断言用租户内唯一、且本例两租户取值不同的业务值）。
        self.assertNotEqual(push_a.serial_no, push_b.serial_no)
        return order_a, push_a, client_a, order_b, push_b, client_b

    def test_each_tenant_only_reads_its_own_push_rows(self):
        _order_a, push_a, client_a, _order_b, push_b, client_b = self._seed()

        # ⚠️ 读面会为「最新一条有效推送」回读详情取撤销弹窗的两个默认金额 ⇒
        # 在这里把它桩掉（测试**不访问真实网络**）；本用例只关心租户隔离。
        placeholder = {
            "pay_level_bal": None,
            "rep_ensure_bal": None,
            "withdraw_defaults_error": "测试桩：不打上游",
        }
        with mock.patch(
            "apps.business.api.dailiantong_push_views.withdraw_defaults_for_push",
            return_value=placeholder,
        ):
            response_a = client_a.get(
                f"/api/business/orders/{self.ORDER_A}/platform-push/"
            )
            self.assertEqual(response_a.data["code"], 0, response_a.data)
            serials_a = [item["serial_no"] for item in response_a.data["data"]]
            self.assertEqual(serials_a, [self.SERIAL_A])

            try:
                response_b = client_b.get(
                    f"/api/business/orders/{self.ORDER_B}/platform-push/",
                    SERVER_NAME=self.DOMAIN_B,
                )
            finally:
                connection.set_tenant(self.tenant)
            self.assertEqual(response_b.data["code"], 0, response_b.data)
            serials_b = [item["serial_no"] for item in response_b.data["data"]]
            self.assertEqual(serials_b, [self.SERIAL_B])

    def test_tenant_b_cannot_read_tenant_a_push_rows(self):
        _order_a, _push_a, _client_a, _order_b, _push_b, client_b = self._seed()

        try:
            response = client_b.get(
                f"/api/business/orders/{self.ORDER_A}/platform-push/",
                SERVER_NAME=self.DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertNotIn(self.SERIAL_A, json.dumps(response.data, default=str))

    def test_tenant_a_cannot_read_tenant_b_push_rows(self):
        _order_a, _push_a, client_a, _order_b, _push_b, _client_b = self._seed()

        response = client_a.get(f"/api/business/orders/{self.ORDER_B}/platform-push/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertNotIn(self.SERIAL_B, json.dumps(response.data, default=str))


class ActorsPlaceholderTests(_PushTestBase):
    """占位账号资料的形状（实测的长度校验是硬前提，别被「更顺眼的文案」改坏）。"""

    def test_placeholder_matches_the_measured_shape(self):
        parts = build_actors(self.order).split("|*|")
        self.assertEqual(len(parts), 5)
        self.assertEqual(len(parts[0]), 8)
        self.assertEqual(len(parts[1]), 8)
        self.assertGreaterEqual(len(parts[2]), 2)
        self.assertIn("接单后联系客服获取账号", parts[4])
        # ⭐ 第 4 段是订单说明，⛔ 不再是固定「其它:无」。
        self.assertNotEqual(parts[3], "其它:无")
        self.assertIn("星耀上王者", parts[3])

    def test_account_is_stable_for_the_same_order(self):
        self.assertEqual(build_actors(self.order), build_actors(self.order))


# ── ⭐ 一键抛单 B3：状态闸 / 稳定联系方式 / 门槛 / 代练要求模板 ──


class PushStatusGateTests(_PushTestBase):
    """计划 §2.5：`UNASSIGNED`（未分配）与 `WAITING_GRAB`（待抢单）都放行，其余拒绝。"""

    def test_unassigned_order_can_be_pushed(self):
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])

        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 1)

    def test_other_statuses_are_rejected_with_both_allowed_states_named(self):
        for status, label in (
            (OrderStatus.IN_PROGRESS, "代练中"),
            (OrderStatus.PENDING_REVIEW, "待验收"),
            (OrderStatus.COMPLETED, "已完成"),
        ):
            with self.subTest(status=status):
                self.order.status = status
                self.order.save(update_fields=["status"])
                message = self._gate_message()
                self.assertIn("未分配", message)
                self.assertIn("待抢单", message)
                self.assertIn(label, message, "文案里要带上订单当前状态")
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(DailiantongPush.objects.count(), 0)


class StableContactTests(_PushTestBase):
    """计划 §2.3：联系电话 / QQ 按订单号派生（稳定），⛔ 不再用配置里的联系字段。"""

    def test_contact_fields_are_derived_not_taken_from_the_integration(self):
        # 配置里放一对「人一眼能认出来」的号码：上送表单里出现它们 ⇒ 红。
        self.integration.contact_phone = "13000000001"
        self.integration.contact_qq = "10001"
        self.integration.save(update_fields=["contact_phone", "contact_qq"])

        self._call_push()

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertNotEqual(form["Mobile"], "13000000001")
        self.assertNotEqual(form["QQ"], "10001")
        # 形状：手机号 11 位 `1` 开头；QQ 9 位、首位非 0（上游若做格式校验也像个号）。
        self.assertRegex(form["Mobile"], r"^1\d{10}$")
        self.assertRegex(form["QQ"], r"^[1-9]\d{8}$")

    def test_the_same_order_gets_the_same_contact_values(self):
        """同一张单恒定（客服回查时不会每抛一次换一个号）；不同单不同。"""
        from apps.business.dailiantong_push import _contact_mobile, _contact_qq

        self.assertEqual(
            _contact_mobile(self.order.order_no), _contact_mobile(self.order.order_no)
        )
        self.assertEqual(
            _contact_qq(self.order.order_no), _contact_qq(self.order.order_no)
        )
        self.assertNotEqual(
            _contact_mobile(self.order.order_no),
            _contact_mobile("20261026000000000002"),
        )
        self.assertNotEqual(
            _contact_qq(self.order.order_no), _contact_qq("20261026000000000002")
        )


class ThresholdPayloadTests(_PushTestBase):
    """计划 §2.4：两个门槛参数是可选的，传了就上送。"""

    def test_thresholds_are_sent_when_given(self):
        self._call_push(power_level=5, limit_accept=15)

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form["PowerLevel"], "5")
        self.assertEqual(form["LimitAccept"], "15")

    def test_thresholds_are_absent_when_not_given(self):
        self._call_push()

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertNotIn("PowerLevel", form)
        self.assertNotIn("LimitAccept", form)

    def test_zero_power_level_is_sent_explicitly(self):
        """`0` 是「不限制」的显式值，不是「没传」（D-0015：⛔ 别用真值判边界）。"""
        self._call_push(power_level=0, limit_accept=0)

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form["PowerLevel"], "0")
        self.assertEqual(form["LimitAccept"], "0")

    def test_out_of_range_values_are_rejected_before_any_upstream_write(self):
        for kwargs, needle in (
            ({"power_level": -1}, "要求最低完单数"),
            ({"power_level": True}, "要求最低完单数"),
            ({"limit_accept": -1}, "拒绝新手和介入率"),
            ({"limit_accept": 101}, "拒绝新手和介入率"),
        ):
            with self.subTest(kwargs=kwargs):
                message = self._gate_message(**kwargs)
                self.assertIn(needle, message)
                self.assertEqual(self.fake.actions.count("LevelOrderAdd"), 0)
                self.assertEqual(DailiantongPush.objects.count(), 0)


class ThresholdReadbackTests(_PushTestBase):
    """计划 §2.4：发单后回读核对 `PowerLevel` / `LimitAccept`（⛔ 不假报成功）。"""

    def test_matching_readback_leaves_no_error(self):
        self.fake.detail = {
            "Status": 11,
            "CancelStatus": 10,
            "PowerLevel": 5,
            "LimitAccept": 15,
        }

        push = self._call_push(power_level=5, limit_accept=15)

        push.refresh_from_db()
        self.assertEqual(push.last_error, "")
        self.assertEqual(self.fake.actions.count("LevelOrderDetail"), 1)
        self.assertIn("发单成功", self._audit_rows().get().reason)
        self.assertNotIn("门槛回读不一致", self._audit_rows().get().reason)

    def test_mismatch_is_a_readable_reason_and_never_claims_success(self):
        self.fake.detail = {
            "Status": 11,
            "CancelStatus": 10,
            "PowerLevel": 3,
            "LimitAccept": 15,
        }

        push = self._call_push(power_level=5, limit_accept=15)

        push.refresh_from_db()
        # 单确实发出去了（真钱冻结）⇒ state 仍是 sent，⛔ 不改成 unknown（那会挡人工重抛）。
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertIn("门槛回读不一致", push.last_error)
        self.assertIn("提交 5", push.last_error)
        self.assertIn("回读 3", push.last_error)
        self.assertIn("门槛回读不一致", self._audit_rows().get().reason)

    def test_field_not_returned_by_detail_is_recorded_as_unverified_not_success(self):
        """厂商文档的详情回包没有这两个字段：审计里留「未能核对」，⛔ 不写成核对通过。"""
        self.fake.detail = {"Status": 11, "CancelStatus": 10}

        push = self._call_push(power_level=5)

        push.refresh_from_db()
        self.assertEqual(push.last_error, "", "常态不该把每次成功抛单都标成错误")
        reason = self._audit_rows().get().reason
        self.assertIn("没有回传", reason)
        self.assertIn("未能核对", reason)

    def test_readback_failure_is_a_visible_problem_but_keeps_the_sent_state(self):
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(parse_qsl(request.content.decode("utf-8")))
            self.fake.calls.append((form.get("Action", ""), form))
            action = form.get("Action")
            if action == "UserMoney":
                return httpx.Response(200, json={"ActiveBal": "100.00", "FreezeBal": "0"})
            if action == "LevelOrderAdd":
                return httpx.Response(200, json={"Result": 1, "Err": SERIAL_OK})
            return httpx.Response(200, json={"Result": -1, "Err": "订单异常"})

        from unittest import mock as _mock

        from apps.business.dailiantong_client import DailiantongClient, DailiantongConfig

        def client_factory(integration):
            return DailiantongClient(
                DailiantongConfig(
                    app_id=integration.app_id,
                    app_secret=integration.app_secret,
                    user_id=integration.account,
                    pay_password_md5=integration.pay_password_md5,
                ),
                transport=httpx.MockTransport(handler),
                clock=lambda: 1_790_000_000,
            )

        with _mock.patch(
            "apps.business.dailiantong_push._open_client", side_effect=client_factory
        ):
            push = push_order(
                self.order, operator=self.operator, **_push_kwargs(power_level=5)
            )

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertIn("门槛回读失败", push.last_error)
        self.assertIn("订单异常", push.last_error)


class RequireTemplateTests(_PushTestBase):
    """计划 §2.4：全局「代练/陪玩要求」模板写进 `Actors` 第五段；空则用既有占位。"""

    def _set_template(self, value: str) -> None:
        from apps.business.models import PlatformPushSettings

        row = PlatformPushSettings.get_solo()
        row.require_template = value
        row.save(update_fields=["require_template", "updated_at"])

    def test_template_is_written_into_the_fifth_actors_segment(self):
        self._set_template("合成模板：接单后先加微信，白天在线")

        self._call_push()

        form = self.fake.forms_for("LevelOrderAdd")[0]
        parts = form["Actors"].split("|*|")
        self.assertEqual(parts[4], "合成模板：接单后先加微信，白天在线")

    def test_empty_template_falls_back_to_the_existing_placeholder(self):
        self._set_template("")

        self._call_push()

        parts = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertEqual(parts[4], "接单后联系客服获取账号")

    def test_overlong_template_is_cut_to_the_documented_limit(self):
        """服务层也要兜（⛔ 靠接口层是唯一防线：直接调服务层的调用点存在）。"""
        from apps.business.dailiantong_push import REQUIRE_TEMPLATE_MAX_LENGTH

        self._set_template("超" * (REQUIRE_TEMPLATE_MAX_LENGTH + 50))

        self._call_push()

        parts = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertEqual(len(parts[4]), REQUIRE_TEMPLATE_MAX_LENGTH)
        # 整列仍在上游 1024 的列宽内（`Actors` 是 String(1024)）。
        self.assertLessEqual(len(self.fake.forms_for("LevelOrderAdd")[0]["Actors"]), 1024)


class PushApiThresholdTests(_PushTestBase):
    """接口层：没传门槛 ⇒ 用全局默认；显式 `null` ⇒ 本单不设置。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="push-api-threshold", groups=("客服",), nodes=(NODE, PLATFORM_WRITE_NODE)
        )

    def _post(self, payload=None):
        return self.client.post(
            URL_PUSH,
            payload if payload is not None else {"price": "5.00", "zone_server_id": ZONE_ID},
            content_type="application/json",
        )

    def _settings(self, **values):
        from apps.business.models import PlatformPushSettings

        row = PlatformPushSettings.get_solo()
        for field, value in values.items():
            setattr(row, field, value)
        row.save()

    def test_omitted_thresholds_come_from_the_global_settings(self):
        self._settings(default_power_level=6, default_limit_accept=25)
        self.fake.detail = {
            "Status": 11,
            "CancelStatus": 10,
            "PowerLevel": 6,
            "LimitAccept": 25,
        }

        with self._patched_client():
            response = self._post()

        self.assertEqual(response.data["code"], 0, response.data)
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form["PowerLevel"], "6")
        self.assertEqual(form["LimitAccept"], "25")
        self.assertEqual(response.data["data"]["last_error"], "")

    def test_explicit_values_win_over_the_global_settings(self):
        self._settings(default_power_level=6, default_limit_accept=25)

        with self._patched_client():
            self._post(
                {
                    "price": "5.00",
                    "zone_server_id": ZONE_ID,
                    "power_level": 2,
                    "limit_accept": None,
                }
            )

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form["PowerLevel"], "2")
        self.assertNotIn("LimitAccept", form)

    def test_out_of_range_limits_are_a_field_named_business_error(self):
        for payload, needle in (
            ({"limit_accept": 101}, "拒绝新手和介入率"),
            ({"limit_accept": -1}, "拒绝新手和介入率"),
            ({"power_level": -2}, "要求最低完单数"),
        ):
            with self.subTest(payload=payload):
                with self._patched_client():
                    response = self._post(
                        {"price": "5.00", "zone_server_id": ZONE_ID, **payload}
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["code"], 1000, response.data)
                self.assertIn(needle, response.data["msg"])
                self.assertEqual(self.fake.calls, [])
                self.assertEqual(DailiantongPush.objects.count(), 0)


# ── ⭐ 一键抛单 B8：发单预览 = 默认值的唯一来源 ────────────────

URL_PREVIEW = f"/api/business/orders/{ORDER_NO}/platform-push/preview/"
URL_SUMMARY = "/api/business/platform-integrations/summary/"


class PushPreviewTests(_PushTestBase):
    """`GET orders/<no>/platform-push/preview/`：默认值 / 可否抛 / 与真发单同源。

    ⭐ 核心判据（B8 的硬要求）：预览里的 actors 五段 / QQ / 电话 / 标题 / 价格，
    与 `LevelOrderAdd` **实际载荷逐字相等** —— 用同一批函数才可能一直成立。
    """

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="push-preview-user", groups=("客服",),
            nodes=(NODE, PLATFORM_WRITE_NODE),
        )
        # ⛔ 不访问真实网络：区服字典是**上游数据**，预览在「没有映射」时会去拉它
        # （生产有 10 分钟缓存）；测试里一律换成本地合成字典。
        push_module.clear_zones_cache()
        self.addCleanup(push_module.clear_zones_cache)
        self.zone_rows = (
            {"ZoneServerID": ZONE_ID, "ServerName": "王者荣耀/安卓QQ区"},
            {"ZoneServerID": "2002", "ServerName": "星穹铁道/仙舟"},
        )
        patcher = mock.patch.object(
            push_module, "_fetch_zone_rows", side_effect=lambda: self.zone_rows
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # 映射优先：这条单记住的区服就是合成字典里的第一条。
        DailiantongZoneMap.objects.create(
            game=self.game, server=self.server, zone_server_id=ZONE_ID
        )

    def _settings(self, **values):
        from apps.business.models import PlatformPushSettings

        row = PlatformPushSettings.get_solo()
        for field, value in values.items():
            setattr(row, field, value)
        row.save()

    def _preview(self, query=None, client=None):
        return (client or self.client).get(URL_PREVIEW, query or {})

    def _push_with_preview_defaults(self, preview):
        payload = {
            "price": preview["price"],
            "ensure1": preview["ensure1"],
            "ensure2": preview["ensure2"],
            "time_limit_hours": preview["time_limit_hours"],
            "zone_server_id": preview["zone_server_id"],
            "power_level": preview["power_level"],
            "limit_accept": preview["limit_accept"],
        }
        with self._patched_client():
            return self.client.post(URL_PUSH, payload, content_type="application/json")

    def test_price_prefers_hall_price_then_platform_price(self):
        """⭐ 业务方改准：先取**发单价**（`hall_price`），没有再取 `platform_price`。"""
        self.order.hall_price = Decimal("8.50")
        self.order.platform_price = Decimal("6.00")
        self.order.save(update_fields=["hall_price", "platform_price"])
        data = self._preview().data["data"]
        self.assertEqual(data["price"], "8.50")

        self.order.hall_price = None
        self.order.save(update_fields=["hall_price"])
        data = self._preview().data["data"]
        self.assertEqual(data["price"], "6.00")

    def test_zero_prices_are_treated_as_unpriced(self):
        """`platform_price` 默认是 0 ⇒ 0 视为没定价，⛔ 不把 0.00 塞进价格框。"""
        self.order.hall_price = Decimal("0.00")
        self.order.platform_price = Decimal("0.00")
        self.order.save(update_fields=["hall_price", "platform_price"])

        data = self._preview().data["data"]

        self.assertIsNone(data["price"])
        self.assertIs(data["can_push"], True)
        self.assertTrue(any("价格" in item for item in data["warnings"]), data["warnings"])

    def test_without_any_price_price_is_null_and_a_warning_but_not_a_blocker(self):
        """业务方口径：价格可以在弹窗里改 ⇒ `can_push` 仍为 true，只给 `warnings`。"""
        data = self._preview().data["data"]

        self.assertIsNone(data["price"])
        self.assertIs(data["can_push"], True)
        self.assertEqual(data["reason"], "")
        self.assertTrue(any("价格" in item for item in data["warnings"]), data["warnings"])

    def test_preview_values_are_the_exact_ones_the_push_actually_sends(self):
        from apps.business.dailiantong_push import (
            PUSH_DEFAULT_TIME_LIMIT_HOURS,
            REQUIRE_TEMPLATE_MAX_LENGTH,
        )

        self._settings(
            default_power_level=4,
            default_limit_accept=12,
            require_template="合成模板：接单后先加微信",
        )
        self.order.hall_price = Decimal("9.90")
        self.order.content = "客户原始需求：星耀上王者，只用射手。"
        self.order.remark = "重要备注：晚上 8 点后在线。"
        self.order.save(update_fields=["hall_price", "content", "remark"])

        preview = self._preview().data["data"]
        self.assertIs(preview["can_push"], True, preview)
        self.assertEqual(preview["game_id"], self.game.pk)
        self.assertEqual(preview["reason"], "")
        self.assertEqual(preview["ensure1"], "0.00")
        self.assertEqual(preview["ensure2"], "0.00")
        self.assertEqual(preview["time_limit_hours"], PUSH_DEFAULT_TIME_LIMIT_HOURS)
        self.assertEqual(preview["zone_source"], "map")
        self.assertEqual(preview["zone_server_id"], ZONE_ID)
        self.assertEqual(preview["power_level"], 4)
        self.assertEqual(preview["limit_accept"], 12)
        self.assertEqual(preview["requirement"], "合成模板：接单后先加微信")
        self.assertEqual(preview["warnings"], [])
        self.assertLessEqual(len(preview["requirement"]), REQUIRE_TEMPLATE_MAX_LENGTH)

        response = self._push_with_preview_defaults(preview)
        self.assertEqual(response.data["code"], 0, response.data)
        form = self.fake.forms_for("LevelOrderAdd")[0]

        # ⭐ 逐字相等：预览显示什么，真发就是什么。
        self.assertEqual(form["Price"], preview["price"])
        self.assertEqual(form["Ensure1"], preview["ensure1"])
        self.assertEqual(form["Ensure2"], preview["ensure2"])
        self.assertEqual(form["TimeLimit"], str(preview["time_limit_hours"]))
        self.assertEqual(form["ZoneServerID"], preview["zone_server_id"])
        self.assertEqual(form["Title"], preview["title"])
        # ⭐ 预览与真发同源 ⇒ 两边都不许带游戏名（游戏在「游戏 / 平台」里选）。
        self.assertNotIn(self.game.name, preview["title"])
        self.assertNotIn(self.game.name, form["Title"])
        self.assertEqual(form["Memo"], preview["memo"])
        self.assertIn("客户原始需求", form["Memo"])
        self.assertIn("重要备注", form["Memo"])
        self.assertEqual(form["Mobile"], preview["contact_phone"])
        self.assertEqual(form["QQ"], preview["contact_qq"])
        self.assertEqual(form["PowerLevel"], str(preview["power_level"]))
        self.assertEqual(form["LimitAccept"], str(preview["limit_accept"]))
        self.assertEqual(
            form["Actors"],
            "|*|".join(
                [
                    preview["game_account"],
                    preview["game_password"],
                    preview["game_role"],
                    preview["current_info"],
                    preview["requirement"],
                ]
            ),
        )

    def test_preview_never_queries_the_balance(self):
        """B8 明确要求：余额不进 preview（由 summary 负责，30 秒缓存）。

        ⚠️ 允许打的那一次上游只可能是**区服字典**（模块级 10 分钟缓存）；判据是
        `UserMoney` 这个动作**一次都不出现**（⛔ 不是「零上游」——字典是另一件事）。
        """
        self.order.platform_price = Decimal("5.00")
        self.order.save(update_fields=["platform_price"])

        with self._patched_client():
            self._preview()

        self.assertNotIn("UserMoney", self.fake.actions)

    def test_manual_zone_matching_the_remembered_map_reports_source_map(self):
        self.order.platform_price = Decimal("5.00")
        self.order.save(update_fields=["platform_price"])

        data = self._preview({"zone_server_id": ZONE_ID}).data["data"]

        self.assertEqual(data["zone_server_id"], ZONE_ID)
        self.assertEqual(data["zone_source"], "map")
        self.assertEqual(data["server_name"], "王者荣耀/安卓QQ区")
        self.assertEqual(data["warnings"], [])

    def test_manual_zone_not_matching_any_default_reports_source_none(self):
        """`zone_source` 只有 map / first / none 三个值（前端契约）——手动选的不算第四种。"""
        with mock.patch.object(
            push_module,
            "_cached_zone_rows",
            return_value=({"ZoneServerID": ZONE_ID, "ServerName": "王者荣耀/安卓QQ区"},),
        ):
            data = self._preview({"zone_server_id": "100100010000099"}).data["data"]

        self.assertEqual(data["zone_server_id"], "100100010000099")
        self.assertEqual(data["zone_source"], "none")

    def test_zone_falls_back_to_the_business_default_when_there_is_no_map(self):
        DailiantongZoneMap.objects.all().delete()
        push_module.clear_zones_cache()

        data = self._preview().data["data"]

        self.assertEqual(data["zone_source"], "first")
        self.assertEqual(data["zone_server_id"], ZONE_ID)
        self.assertEqual(data["server_name"], "王者荣耀/安卓QQ区")

    def test_refunded_order_without_active_push_can_push_in_preview(self):
        """⭐ 2026-10-28：preview 与真发同口径——已退单 + 无活跃推送 ⇒ `can_push=true`。"""
        self.order.platform_price = Decimal("5.00")
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["platform_price", "status"])

        data = self._preview().data["data"]

        self.assertIs(data["can_push"], True, data)
        self.assertEqual(data["reason"], "")

    def test_refunded_order_with_an_active_push_is_blocked_in_preview(self):
        self.order.platform_price = Decimal("5.00")
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["platform_price", "status"])
        self._make_push(serial_no="19000000000000000078")

        data = self._preview().data["data"]

        self.assertIs(data["can_push"], False)
        self.assertIn("19000000000000000078", data["reason"])

    def test_cannot_push_when_the_order_status_is_not_allowed(self):
        self.order.platform_price = Decimal("5.00")
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["platform_price", "status"])

        data = self._preview().data["data"]

        self.assertIs(data["can_push"], False)
        self.assertIn("未分配", data["reason"])
        self.assertIn("待抢单", data["reason"])
        self.assertIn("代练中", data["reason"])

    def test_cannot_push_when_a_valid_push_already_exists(self):
        self.order.platform_price = Decimal("5.00")
        self.order.save(update_fields=["platform_price"])
        self._make_push(serial_no="19000000000000000077")

        data = self._preview().data["data"]

        self.assertIs(data["can_push"], False)
        self.assertIn("19000000000000000077", data["reason"])

    def test_below_the_measured_minimum_price_is_a_warning_not_a_blocker(self):
        """业务方口径：改价就能解决 ⇒ `can_push` 仍 true，只在 `warnings` 里提示。"""
        self.order.platform_price = Decimal("1.50")
        self.order.save(update_fields=["platform_price"])

        data = self._preview().data["data"]

        self.assertIs(data["can_push"], True)
        self.assertEqual(data["reason"], "")
        blob = " ".join(data["warnings"])
        self.assertIn("2.00", blob)
        self.assertIn("1.50", blob)

    def test_cannot_push_without_an_integration(self):
        PlatformIntegration.objects.all().delete()
        self.order.platform_price = Decimal("5.00")
        self.order.save(update_fields=["platform_price"])

        data = self._preview().data["data"]

        self.assertIs(data["can_push"], False)
        self.assertIn("交易平台对接设置", data["reason"])

    def test_unknown_order_is_a_business_error(self):
        response = self.client.get(
            "/api/business/orders/20261026000000009999/platform-push/preview/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000)
        self.assertEqual(response.data["msg"], "订单不存在或不可见")

    def test_malformed_zone_parameter_is_a_field_named_business_error(self):
        response = self._preview({"zone_server_id": "x" * 40})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertIn("区服", response.data["msg"])

    def test_zone_not_matched_is_a_warning_not_a_blocker(self):
        DailiantongZoneMap.objects.all().delete()
        push_module.clear_zones_cache()
        with mock.patch.object(push_module, "_fetch_zone_rows", return_value=()):
            data = self._preview().data["data"]
        push_module.clear_zones_cache()

        self.assertEqual(data["zone_source"], "none")
        self.assertIs(data["can_push"], True)
        self.assertTrue(any("区服" in item for item in data["warnings"]), data["warnings"])

    def test_without_the_order_list_node_it_is_403(self):
        denied = make_logged_in_client(username="push-preview-no-node", groups=())
        response = self._preview(client=denied)
        self.assertEqual(response.status_code, 403, response.content[:300])


class MemoTests(_PushTestBase):
    """⭐ 真机反馈：**代练内容在 `project_text`**，旧实现只拼 content+remark
    ⇒ 那张单（content / remark 都空）发出去的 `Memo` 是空串，打手看不到要做什么。

    业务口径：`Memo` = `project_text`（代练内容）+ `content`（客户原始需求）+ `remark`
    （重要备注），各自非空才拼、去重、按**字符**截断 2048。**预览与真发同源**。
    """

    def _set(
        self,
        *,
        project_text: str = "",
        content: str = "",
        remark: str = "",
    ) -> None:
        self.order.project_text = project_text
        self.order.content = content
        self.order.remark = remark
        self.order.save(update_fields=["project_text", "content", "remark"])

    def test_memo_is_project_text_then_content_then_remark(self):
        self._set(
            project_text="代练内容：星耀上王者",
            content="客户原始需求：只用射手。",
            remark="重要备注：晚上在线。",
        )

        self._call_push()

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(
            form["Memo"],
            "代练内容：星耀上王者\n客户原始需求：只用射手。\n重要备注：晚上在线。",
        )
        # ⭐ 同源：预览回给页面的那份值 = 真发出去的那份（同一次 `build_memo`）。
        self.assertEqual(push_module.preview_push(self.order)["memo"], form["Memo"])

    def test_each_field_alone_reaches_the_memo(self):
        """三个字段**各自的取值**都要被包含（FX1 的现场就是只有 `project_text` 有值）。"""
        cases = (
            ("project_text", "代练内容：星耀上王者"),
            ("content", "客户原始需求：只用射手"),
            ("remark", "重要备注：晚上在线"),
        )
        for field, value in cases:
            with self.subTest(only=field):
                self._set(**{field: value})
                self.assertEqual(
                    push_module.build_memo(self.order),
                    value,
                    f"只有 {field} 有值时它必须在 Memo 里",
                )
                self.assertEqual(
                    push_module.preview_push(self.order)["memo"],
                    push_module.build_memo(self.order),
                    "预览值与真发的 Memo 必须同源（同一次拼装）",
                )

    def test_the_real_push_scenario_only_project_text_is_sent(self):
        """真机那张单的形态：`content=''`、`remark=''`、内容只在 `project_text`。"""
        self._set(project_text="测试订单 请勿接取")

        self._call_push()

        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form.get("Memo"), "测试订单 请勿接取")

    def test_duplicate_parts_are_joined_once(self):
        self._set(
            project_text="星耀上王者",
            content="星耀上王者",
            remark="星耀上王者",
        )
        self._call_push()
        self.assertEqual(self.fake.forms_for("LevelOrderAdd")[0]["Memo"], "星耀上王者")

    def test_empty_parts_are_not_joined_as_blank_lines(self):
        self._set(project_text="", content="只有客户需求", remark="")
        self._call_push()
        self.assertEqual(
            self.fake.forms_for("LevelOrderAdd")[0]["Memo"], "只有客户需求"
        )

    def test_no_memo_parameter_when_all_three_are_empty(self):
        self._set(project_text="", content="", remark="")
        self._call_push()
        self.assertNotIn("Memo", self.fake.forms_for("LevelOrderAdd")[0])

    def test_memo_is_truncated_to_the_upstream_column_width(self):
        from apps.business.dailiantong_push import MEMO_MAX_LENGTH

        # ⚠️ 用 `content`（`TextField`）造超长值：`project_text` 列宽只有 900。
        self._set(project_text="", content="甲" * (MEMO_MAX_LENGTH + 500))

        self._call_push()

        self.assertEqual(
            len(self.fake.forms_for("LevelOrderAdd")[0]["Memo"]), MEMO_MAX_LENGTH
        )


class RemoteFieldStorageTests(_PushTestBase):
    """⭐ A2（计划 Rev.9 §2）：回读要把**远端多字段**存进推送行。"""

    def test_accept_over_and_cust_cancel_counts_are_stored(self):
        from apps.business.dailiantong_push import store_remote_fields

        push = self._make_push(serial_no="19000000000000000778", remote_status=12)
        changed = store_remote_fields(
            push,
            {"Status": 12, "AcceptOverCount": "37", "CustCancelCount": "2"},
            status=12,
            cancel_status=10,
        )
        self.assertIn("accept_over_count", changed)
        self.assertIn("cust_cancel_count", changed)
        self.assertEqual(push.accept_over_count, 37)
        self.assertEqual(push.cust_cancel_count, 2)

        store_remote_fields(
            push,
            {"Status": 12, "AcceptOverCount": "", "CustCancelCount": "坏值"},
            status=12,
            cancel_status=10,
        )
        self.assertIsNone(push.accept_over_count, "空值 ⇒ null，⛔ 不写 0")
        self.assertIsNone(push.cust_cancel_count, "解析不出 ⇒ null，⛔ 不写 0")

    def test_verify_started_at_is_set_on_first_13_and_cleared_when_leaving(self):
        from apps.business.dailiantong_push import store_remote_fields

        push = self._make_push(serial_no="19000000000000000777", remote_status=12)
        self.assertIsNone(push.remote_verify_started_at)

        changed = store_remote_fields(
            push, {"Status": 13}, status=13, cancel_status=10
        )
        self.assertIn("remote_verify_started_at", changed)
        self.assertIsNotNone(push.remote_verify_started_at)

        first_started = push.remote_verify_started_at
        # 第二次仍是 13 ⇒ 不重置起点（「首次进入」口径）。
        store_remote_fields(push, {"Status": 13}, status=13, cancel_status=10)
        self.assertEqual(push.remote_verify_started_at, first_started)

        changed = store_remote_fields(
            push, {"Status": 12}, status=12, cancel_status=10
        )
        self.assertIn("remote_verify_started_at", changed)
        self.assertIsNone(push.remote_verify_started_at, "离开 13 ⇒ 清掉起点")

    def test_detail_fields_are_stored_verbatim(self):
        push = self._make_push(serial_no=SERIAL_OK)
        self.fake.detail = {
            "Status": 12,
            "CancelStatus": 10,
            "AcceptUID": "USR-SYNTH-TAKER-9",
            "AcceptUserName": "合成打手昵称",
            "StartDate": "2026-10-27 09:30:15",
            "CancelCmt": "客户临时不要了",
        }

        with self._patched_client():
            refresh_push_status(push)

        push.refresh_from_db()
        self.assertEqual(push.remote_taker_uid, "USR-SYNTH-TAKER-9")
        self.assertEqual(push.remote_taker_name, "合成打手昵称")
        self.assertIsNotNone(push.remote_accepted_at)
        # 接单时间按上游原文解析（不是「回读时刻」）。
        self.assertEqual(
            timezone.localtime(push.remote_accepted_at).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-10-27 09:30:15",
        )
        self.assertEqual(push.remote_cancel_cmt, "客户临时不要了")
        # ⭐ CancelReason 与 CancelCmt 分开存；这份详情没有 CancelReason。
        self.assertEqual(push.remote_cancel_reason, "")

    def test_cancel_reason_and_cancel_cmt_are_stored_separately(self):
        push = self._make_push(serial_no=SERIAL_OK)
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "CancelReason": "好的",
            "CancelCmt": (
                '接单者"新手USR-SYNTH-806325"申请撤销,需发单者支出:0.00, '
                "接单者支出:0.00. 请尽快协商解决.撤销原因：好的"
            ),
        }
        with self._patched_client():
            refresh_push_status(push)
        push.refresh_from_db()
        self.assertEqual(push.remote_cancel_reason, "好的")
        self.assertEqual(
            push.remote_cancel_cmt,
            '接单者"新手USR-SYNTH-806325"申请撤销,需发单者支出:0.00, '
            "接单者支出:0.00. 请尽快协商解决.撤销原因：好的",
        )

    def test_cancel_cmt_falls_back_to_cancel_reason_when_the_key_is_absent(self):
        push = self._make_push(serial_no=SERIAL_OK)
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "CancelReason": "只给了原因",
        }
        with self._patched_client():
            refresh_push_status(push)
        push.refresh_from_db()
        self.assertEqual(push.remote_cancel_reason, "只给了原因")
        self.assertEqual(push.remote_cancel_cmt, "只给了原因", "缺键才退回原因")

    def test_pay_level_and_rep_ensure_amounts_are_stored_from_detail(self):
        push = self._make_push(serial_no=SERIAL_OK)
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "PayLevelBal": "1.50",
            "RepEnsureBal": "0.00",
        }
        with self._patched_client():
            refresh_push_status(push)
        push.refresh_from_db()
        self.assertEqual(push.remote_pay_level_bal, Decimal("1.50"))
        self.assertEqual(push.remote_rep_ensure_bal, Decimal("0.00"))

    def test_empty_or_unparsable_settled_amounts_become_null(self):
        push = self._make_push(serial_no=SERIAL_OK)
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "PayLevelBal": "",
            "RepEnsureBal": "不是金额",
        }
        with self._patched_client():
            refresh_push_status(push)
        push.refresh_from_db()
        self.assertIsNone(push.remote_pay_level_bal)
        self.assertIsNone(push.remote_rep_ensure_bal)

    def test_cancel_cmt_and_reason_are_both_empty_when_the_detail_has_neither(self):
        push = self._make_push(serial_no=SERIAL_OK)
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_client():
            refresh_push_status(push)
        push.refresh_from_db()
        self.assertEqual(push.remote_cancel_reason, "")
        self.assertEqual(push.remote_cancel_cmt, "")

    def test_missing_detail_fields_keep_the_previous_values(self):
        """详情没回传时⛔ 不许用空值把已经记下的接单人擦掉。"""
        push = self._make_push(
            serial_no=SERIAL_OK,
            remote_taker_uid="USR-KEEP",
            remote_taker_name="留着的人",
        )
        self.fake.detail = {"Status": 13, "CancelStatus": 10}

        with self._patched_client():
            refresh_push_status(push)

        push.refresh_from_db()
        self.assertEqual(push.remote_taker_uid, "USR-KEEP")
        self.assertEqual(push.remote_taker_name, "留着的人")
        self.assertEqual(push.remote_status, 13)


class DisplayStatusForRemoteTests(SimpleTestCase):
    """⭐ 真机 bug：`(Status, CancelStatus)` ⇒ **读面综合状态**。

    现场：远端 `Status=16`（撤销中）+ `CancelStatus=12`（协商已处理 = 打手已同意撤销）、
    本地订单已是「已退单」，但第三方平台订单页的「交易状态」仍显示「撤销中」——状态没跟
    `CancelStatus` 走。口径（业务方）：`CancelStatus 12/15/16` ⇒ 已撤销 / 已强制撤销；
    `11` ⇒ 撤销中；`14` ⇒ 客服介入中；`13` ⇒ 回到正常流程、按 `Status` 显示。
    """

    def test_cancel_status_12_negotiated_is_revoked(self):
        """12 协商已处理 = 打手已同意撤销 ⇒ 已撤销（⛔ 不是「撤销中」）。"""
        self.assertEqual(display_status_for_remote(16, 12), ("revoked", "已撤销"))

    def test_cancel_status_15_customer_handled_is_revoked(self):
        """15 客服已处理 ⇒ 已撤销（业务方 2026-10-28 定案）。"""
        self.assertEqual(display_status_for_remote(16, 15), ("revoked", "已撤销"))

    def test_cancel_status_16_customer_forced_is_force_revoked(self):
        self.assertEqual(
            display_status_for_remote(16, 16), ("force_revoked", "已强制撤销")
        )

    def test_cancel_status_13_reverted_falls_back_to_the_remote_status(self):
        """13 撤销已取消 ⇒ 撤销流程作废、按 `Status` 显示（⛔ 不显示「已撤销」）。"""
        self.assertEqual(display_status_for_remote(16, 13), ("cancelling", "撤销中"))
        self.assertEqual(display_status_for_remote(12, 13), ("in_progress", "正在代练"))
        self.assertEqual(
            display_status_for_remote(13, 13), ("waiting_review", "等待验收")
        )

    def test_cancel_status_11_and_14_are_still_in_flight(self):
        """11 协商撤销中 / 14 客服介入中 ⇒ 都还没撤销完（⛔ 不许显示「已撤销」）。"""
        self.assertEqual(display_status_for_remote(16, 11), ("cancelling", "撤销中"))
        self.assertEqual(
            display_status_for_remote(16, 14), ("arbitrating", "客服介入中")
        )

    def test_plain_status_codes_keep_their_own_labels(self):
        """其余按 `Status`（10/11/12/13/14/15/16/17）。"""
        expected = {
            10: ("temp_order", "临时订单"),
            11: ("unaccepted", "未接手"),
            12: ("in_progress", "正在代练"),
            13: ("waiting_review", "等待验收"),
            14: ("abnormal", "订单异常"),
            # ⭐ 真机读数改准：15 = 被我们锁定（锁 12/13/14 ⇒ 15，解锁回原状态）。
            15: ("locked", "已锁定"),
            16: ("cancelling", "撤销中"),
            17: ("settled", "已结算"),
        }
        for status, pair in expected.items():
            with self.subTest(status=status):
                self.assertEqual(display_status_for_remote(status, 10), pair)
                self.assertEqual(display_status_for_remote(status, None), pair)

    def test_settled_wins_over_a_lagging_cancel_code(self):
        """`Status=17`（钱已结算）优先于撤销码 —— 与 `local_status_for_remote` 同一优先级。"""
        self.assertEqual(display_status_for_remote(17, 12), ("settled", "已结算"))

    def test_nothing_known_means_no_display_status(self):
        """两个码都还没有 / 未知码 ⇒ `(None, "")`（读面不给假状态，前端画「—」）。"""
        self.assertEqual(display_status_for_remote(None, None), (None, ""))
        self.assertEqual(display_status_for_remote(None, 13), (None, ""))
        self.assertEqual(display_status_for_remote(99, 99), (None, ""))


class RemoteStatusWritebackTests(_PushTestBase):
    """⭐ A2：远端状态 ⇒ **本地订单状态**回写（Rev.9 §2 的映射表）。"""

    def setUp(self):
        super().setUp()
        # 默认给一个有发单价的单（绩效金额可算）；「没有发单价」那一档单独有 FX2 用例。
        self.order.hall_price = Decimal("5.00")
        self.order.save(update_fields=["hall_price"])
        self.sales = self.user_model.objects.create_user(username="dlt-sales")
        self._serials: list[str] = []

    def _refresh_with(self, *, push=None, serial: str | None = None, **detail):
        # ⚠️ 两个唯一约束都在：`serial_no` 全局唯一、`(order, platform)` 只许一条有效推送
        #    ⇒ 同一个用例里多次回读要么**复用同一条**（`push=`），要么换一张单。
        #    下面这个自增号只服务「第一次造行」。
        if push is None:
            serial = serial or f"19000000000000{len(self._serials) + 1:06d}"
            self._serials.append(serial)
            push = self._make_push(serial_no=serial)
        self.fake.detail = {"CancelStatus": 10, **detail}
        with self._patched_client():
            refresh_push_status(push)
        push.refresh_from_db()
        self.order.refresh_from_db()
        return push

    def test_readback_cancelled_status_clears_the_local_cancel_marker(self):
        """⭐ C1：回读 CancelStatus=13（撤销已取消）⇒ 清本地标记 + 自动仲裁待办。"""
        push = self._make_push(serial_no="19000000000000000888")
        push.cancel_requested_at = timezone.now()
        push.auto_arbitrate = True
        push.auto_arbitrate_attempts = 2
        push.auto_arbitrate_next_at = timezone.now()
        push.auto_arbitrate_note = "等待自动仲裁"
        push.save(
            update_fields=[
                "cancel_requested_at",
                "auto_arbitrate",
                "auto_arbitrate_attempts",
                "auto_arbitrate_next_at",
                "auto_arbitrate_note",
                "updated_at",
            ]
        )

        push = self._refresh_with(push=push, Status=12, CancelStatus=13)

        self.assertIsNone(push.cancel_requested_at)
        self.assertFalse(push.auto_arbitrate)
        self.assertEqual(push.auto_arbitrate_attempts, 0)
        self.assertIsNone(push.auto_arbitrate_next_at)
        self.assertEqual(push.auto_arbitrate_note, "")
        self.assertIn("取消", push.remote_note)
        self.assertTrue(
            OperationLog.objects.filter(
                action="order.dailiantong_withdraw",
                order_no=self.order.order_no,
                reason__contains="取消",
            ).exists()
        )

    def test_status_11_maps_back_to_waiting_grab(self):
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])
        self._refresh_with(Status=11)
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_status_12_maps_to_in_progress_and_records_the_placeholder_taker(self):
        push = self._refresh_with(Status=12, AcceptUID="USR-X", AcceptUserName="平台打手")
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertIsNotNone(perf.user_id)
        self.assertEqual(perf.user.username, push_module.VIRTUAL_TAKER_USERNAME)
        # 占位账号不可登录（业务口径：虚拟账号）。
        self.assertFalse(perf.user.is_active)
        self.assertFalse(perf.user.has_usable_password())
        self.assertEqual(push.remote_taker_uid, "USR-X")
        self.assertIn("代练通", push.remote_note)

    def test_status_12_without_a_hall_price_still_records_the_placeholder_taker(self):
        """⭐ 真机反馈（业务方）：没有发单价的单也要记上「代练通」。

        现场：远端已接手（本地 `IN_PROGRESS`）但本地没有接手人——根因是 A2 原先在
        `hall_price is None` 时**直接跳过**指派。业务口径是「远端已接手 ⇒ 本地接手人必须是
        占位账号」：钱可以等补价后再算，人不能空着。
        """
        self.order.hall_price = None
        self.order.save(update_fields=["hall_price"])

        push = self._refresh_with(Status=12)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertIsNotNone(perf.user_id, "远端已接手，本地接手人不能还是空的")
        self.assertEqual(perf.user.username, push_module.VIRTUAL_TAKER_USERNAME)
        self.assertEqual(perf.user.first_name, push_module.VIRTUAL_TAKER_DISPLAY_NAME)
        self.assertIn(push_module.VIRTUAL_TAKER_DISPLAY_NAME, push.remote_note)

    def test_status_13_and_17_keep_the_placeholder_taker(self):
        """FX2：远端往后走（等待验收 / 已结算）⛔ 不许把已经记上的占位接手人清掉。"""
        push = self._refresh_with(Status=12)

        def _placeholder():
            perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
            return perf.user.username if perf.user_id else ""

        self.assertEqual(_placeholder(), push_module.VIRTUAL_TAKER_USERNAME)

        push = self._refresh_with(push=push, Status=13)
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(_placeholder(), push_module.VIRTUAL_TAKER_USERNAME)

        push = self._refresh_with(push=push, Status=17)
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(_placeholder(), push_module.VIRTUAL_TAKER_USERNAME)

    def test_last_push_terminal_releases_the_placeholder_taker(self):
        """⭐ 2026-10-28（业务方）：最后一条推送也终态 ⇒ 清掉占位接手人「代练通」。"""
        push = self._refresh_with(Status=12)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user.username, push_module.VIRTUAL_TAKER_USERNAME)

        push = self._refresh_with(push=push, Status=16, CancelStatus=12)

        self.order.refresh_from_db()
        # ⭐ I1：只清人，**状态保持 REFUNDED**（⛔ 不再改写成「未分配」）。
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id, "全部推送结束 ⇒ 占位接手人必须清掉")
        audits = OperationLog.objects.filter(action="order.dailiantong_clear_taker")
        self.assertEqual(audits.count(), 1)
        self.assertIn("已无活跃订单", audits.get().reason)
        self.assertEqual(
            push_module.release_placeholder_taker_if_no_active_pushes(self.order),
            "",
            "已经清过 ⇒ 幂等返回空串",
        )
        self.assertEqual(
            OperationLog.objects.filter(action="order.dailiantong_clear_taker").count(),
            1,
            "重复触发不重复写审计",
        )
        # 再回读一次同一条终态推送：`_assign_placeholder_taker` 可能又把占位人写回来，
        # 收口必须再次清掉，且⛔ 不再写第二条审计。
        self._refresh_with(push=push, Status=16, CancelStatus=12)
        perf.refresh_from_db()
        self.assertIsNone(perf.user_id)
        self.assertEqual(
            OperationLog.objects.filter(action="order.dailiantong_clear_taker").count(),
            1,
        )

    def test_one_still_active_push_keeps_the_placeholder_taker(self):
        push = self._refresh_with(Status=12)
        # 另一平台还有一条活跃推送（`is_active=True`）⇒ 不能当成「全部结束」。
        DailiantongPush.objects.create(
            order=self.order,
            platform=11,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000999001",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=self.order.order_no,
            attempts=1,
        )
        self._refresh_with(push=push, Status=16, CancelStatus=12)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user.username, push_module.VIRTUAL_TAKER_USERNAME)
        self.assertFalse(
            OperationLog.objects.filter(action="order.dailiantong_clear_taker").exists()
        )

    def test_a_real_taker_is_never_released(self):
        """接手人是真人 ⇒ ⛔ 不清（这只清理平台占位账号）。"""
        self._refresh_with(Status=12)
        real = get_user_model().objects.create_user(
            username="real-taker-keep", password="synth-pw-keep"
        )
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(
            user=real
        )
        DailiantongPush.objects.filter(order=self.order).update(is_active=False)

        note = push_module.release_placeholder_taker_if_no_active_pushes(self.order)

        self.assertEqual(note, "", "真人接手人不是清理对象")
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, real.pk)
        self.assertFalse(
            OperationLog.objects.filter(action="order.dailiantong_clear_taker").exists()
        )

    def test_status_13_maps_to_pending_review(self):
        self._refresh_with(Status=13)
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)

    def test_status_17_maps_to_completed(self):
        self._refresh_with(Status=17)
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertIsNotNone(self.order.completed_at)

    def test_customer_forced_cancel_maps_to_refunded(self):
        # 没有经历过「被接手」的占位人 ⇒ 没有东西要清，停在本地的「已退单」。
        self._refresh_with(Status=16, CancelStatus=16)
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)

    def test_negotiated_cancel_completed_maps_to_refunded(self):
        self._refresh_with(Status=16, CancelStatus=12)
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)

    def test_cancel_12_writes_refunded_even_when_the_remote_status_lags(self):
        """⭐ 真机口径：`CancelStatus=12` 已经是「打手同意撤销」的事实 ⇒
        即使远端 `Status` 还停在 12/13（读面滞后），本地也必须落已退单。

        ⛔ 不许再拿「远端 Status 看着还在做」当借口不写——那样本地会一直停在代练中。
        """
        push = self._refresh_with(Status=12, CancelStatus=12)
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        # 同一行再读一次（换一张单会撞「同单同平台一条有效推送」的部分唯一索引）。
        self._refresh_with(push=push, Status=13, CancelStatus=12)
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)

    def test_cancel_15_customer_handled_writes_refunded(self):
        """⭐ h1：`CancelStatus=15`（客服已处理）也按撤销完成落已退单（业务方 2026-10-28）。"""
        self._refresh_with(Status=13, CancelStatus=15)
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)

    def test_a_completed_order_is_not_flipped_back_to_refunded(self):
        """⭐ h1：本地已是**另一个终态**（已完成）⇒ 远端撤销完成只记原因、⛔ 不来回改。"""
        self.order.status = OrderStatus.COMPLETED
        self.order.save(update_fields=["status"])

        push = self._refresh_with(Status=16, CancelStatus=12)

        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertIn("终态", push.remote_note)
        self.assertIn("已完成", push.remote_note)
        self.assertIn("已撤销", push.remote_note)

    def test_pending_negotiation_does_not_change_the_local_status(self):
        self._refresh_with(Status=12, CancelStatus=11)
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_process_only_remote_statuses_are_recorded_but_not_applied(self):
        """`Status=14/15/16` 只记远端码与原因，⛔ 不改本地状态（Rev.9 §2 的表）。"""
        push = None
        for remote in (14, 15, 16):
            with self.subTest(remote=remote):
                self.order.status = OrderStatus.IN_PROGRESS
                self.order.save(update_fields=["status"])
                push = self._refresh_with(
                    push=push, Status=remote, CancelCmt="打手提交异常"
                )
                self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
                self.assertEqual(push.remote_status, remote)
                # ⭐ 只有 CancelCmt 时它落 `remote_cancel_cmt`，
                # `remote_cancel_reason` 只装 CancelReason（不混装）。
                self.assertEqual(push.remote_cancel_cmt, "打手提交异常")
                self.assertEqual(push.remote_cancel_reason, "")

    def test_a_second_round_does_not_write_the_same_status_again(self):
        """同一远端状态第二轮不产生第二条 `order.transition` 审计（幂等）。"""
        push = self._refresh_with(Status=13)
        audits = OperationLog.objects.filter(
            action="order.transition", order_no=self.order.order_no
        ).count()
        self.assertEqual(audits, 1)
        self.fake.detail = {"Status": 13, "CancelStatus": 10}
        with self._patched_client():
            refresh_push_status(push)
        self.assertEqual(
            OperationLog.objects.filter(
                action="order.transition", order_no=self.order.order_no
            ).count(),
            audits,
        )

    def test_writeback_failure_is_recorded_but_the_readback_still_succeeds(self):
        """本地回写失败（提成配置坏）⇒ `last_error` 说清楚，⛔ 不把回读算成失败、⛔ 不停轮询。"""
        self.order.hall_price = None
        self.order.save(update_fields=["hall_price"])
        self.order.sales_user = self.sales
        self.order.save(update_fields=["sales_user"])
        # 客服行 + 一个模式不匹配的档案（FIXED ≠ 本处需要的 PROFIT_PERCENT）⇒ 完成时
        # `commission_percent_of` 抛 `CommissionError`（**预期失败**的那一档）。
        Performance.objects.create(
            order=self.order, emp_type=EmpType.SALES, user=self.sales
        )
        StaffProfile.objects.create(
            user=self.sales, commission_mode=10, commission_value=Decimal("5.00")
        )

        push = self._refresh_with(Status=17)

        # 远端确实读到了（同步时间照写）——这是「回读成功」。
        self.assertIsNotNone(push.last_synced_at)
        # 本地写不成（销售提成模式不匹配）⇒ 原因留在 last_error，且**继续轮询**。
        self.assertIn("本地订单状态回写失败", push.last_error)
        self.assertTrue(push.is_active)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, OrderStatus.COMPLETED)

    def test_the_placeholder_account_is_reused_per_tenant(self):
        push = self._refresh_with(Status=12)
        push = self._refresh_with(push=push, Status=12)
        self.assertEqual(
            self.user_model.objects.filter(
                username=push_module.VIRTUAL_TAKER_USERNAME
            ).count(),
            1,
        )

    def test_a_legacy_placeholder_account_is_renamed_instead_of_duplicated(self):
        """⭐ 旧租户库里那个占位账号还叫旧名「代练通打手」⇒ **就地改名复用**。

        判据是**固定 username**（⛔ 不按显示名找）：改名后同一个 pk、账号数仍为 1、
        `first_name` 逐字 = 新显示名「代练通」。
        """
        legacy = self.user_model.objects.create_user(
            username=push_module.VIRTUAL_TAKER_USERNAME
        )
        legacy.first_name = "代练通打手"
        legacy.is_active = False
        legacy.set_unusable_password()
        legacy.save(update_fields=["first_name", "is_active", "password"])

        user = push_module.placeholder_taker_user()

        self.assertEqual(user.pk, legacy.pk, "必须复用旧行，⛔ 不新建第二个占位账号")
        user.refresh_from_db()
        self.assertEqual(user.first_name, "代练通")
        self.assertEqual(
            self.user_model.objects.filter(
                username=push_module.VIRTUAL_TAKER_USERNAME
            ).count(),
            1,
        )

    def test_a_real_local_taker_is_rolled_back_when_the_platform_took_it_first(self):
        """⭐ A4 第 3 条（两边同时接）：保代练通 ⇒ 本地打手撤销、绩效回退、留中文提示。"""
        internal = self.user_model.objects.create_user(username="internal-taker")
        # 池内单必须有那条 MAIN 绩效行（D-0032）——抢单的条件更新打在它上面。
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("5.00")
        )
        claim_order(
            user_id=internal.id, order_id=self.order.id, nodes={"grab_hall.take_order"}
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

        push = self._refresh_with(Status=12, AcceptUID="USR-WIN", AcceptUserName="平台赢家")

        self.order.refresh_from_db()
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user.username, push_module.VIRTUAL_TAKER_USERNAME)
        self.assertNotEqual(perf.user_id, internal.id)
        self.assertIn("已撤销", push.remote_note)
        self.assertIn("代练通", push.remote_note)
        # 本地状态仍按远端（12 = 代练中）。
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_an_order_without_a_hall_price_still_gets_the_taker_and_says_money_is_pending(self):
        """FX2：没有发单价 ⇒ **接手人照记**（业务口径），只把「绩效金额待补价」说清楚。"""
        self.order.hall_price = None
        self.order.save(update_fields=["hall_price"])

        push = self._refresh_with(Status=12)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user.username, push_module.VIRTUAL_TAKER_USERNAME)
        self.assertIn("没有「发单价」", push.remote_note)
        self.assertIn("代练通", push.remote_note)
        self.assertEqual(push.last_error, "")


class InternalHallPublishTests(_PushTestBase):
    """⭐ A3（Rev.9 §3）：抛代练通前若还没发内部大厅 ⇒ 同时发；大厅失败只提示。"""

    def test_unassigned_order_is_also_published_to_the_internal_hall(self):
        """⭐ 2026-10-28：大厅价 = **本次抛单的发平台价格**（不再沿用旧发单价/1 元兜底）。"""
        self.order.status = OrderStatus.UNASSIGNED
        self.order.hall_price = Decimal("1.00")  # 旧值（真机曾把它当大厅价发出去）
        self.order.save(update_fields=["status", "hall_price"])

        push = self._call_push(price=Decimal("6.00"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(
            self.order.hall_price,
            Decimal("6.00"),
            "大厅价必须等于本次发平台价格，⛔ 不是旧发单价",
        )
        self.assertEqual(self.order.hall_publisher, self.operator)
        hall_audit = OperationLog.objects.filter(
            action="order.throw_to_hall", order_no=self.order.order_no
        ).get()
        self.assertEqual(Decimal(str(hall_audit.to_value)), Decimal("6.00"))
        self.assertIn("内部抢单大厅", push.hall_note)
        # 代练通那一步照常发出（不是二选一）。
        self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_hall_price_same_wins_over_the_platform_price(self):
        """`hall_price_same` 传了就用它（页面另一个格子）；代练通仍用 `price`。"""
        self.order.status = OrderStatus.UNASSIGNED
        self.order.hall_price = Decimal("1.00")
        self.order.save(update_fields=["status", "hall_price"])

        self._call_push(price=Decimal("3.00"), hall_price_same=Decimal("2.50"))

        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("2.50"))
        self.assertEqual(self.fake.forms_for("LevelOrderAdd")[0]["Price"], "3.00")

    def test_order_already_in_the_hall_is_not_published_twice(self):
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        push = self._call_push()
        self.assertEqual(push.hall_note, "")
        self.assertFalse(
            OperationLog.objects.filter(
                action="order.throw_to_hall", order_no=self.order.order_no
            ).exists()
        )

    def test_hall_failure_is_only_a_note_and_the_push_still_happens(self):
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])
        from apps.business import order_status as order_status_module

        with mock.patch.object(
            order_status_module, "apply_transition", side_effect=RuntimeError("hall boom")
        ):
            push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertIn("补发内部大厅没成功", push.hall_note)
        self.assertNotIn("hall boom", push.hall_note, "提示里不带内部异常原文")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.UNASSIGNED)

    def test_hall_publish_happens_before_the_real_upstream_call(self):
        """顺序判据：大厅先发（本地），再打代练通；上游失败也不撤销大厅补发。"""
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])
        self.fake.add_response = {"Result": -2, "Err": UPSTREAM_BUSINESS_ERR}

        push = self._call_push()

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.FAILED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIn("内部抢单大厅", push.hall_note)


class InternalHallPublishApiTests(_PushTestBase):
    """A3 的**接口层**判据：大厅补发那一句必须随响应回给发单人（⛔ 只落内存属性等于没说）。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="push-hall-api", groups=("客服",),
            nodes=(NODE, PLATFORM_WRITE_NODE, HALL_PUBLISH_NODE),
        )

    def _post(self):
        return self.client.post(
            URL_PUSH,
            {"price": "5.00", "zone_server_id": ZONE_ID},
            content_type="application/json",
        )

    def test_the_response_message_carries_the_hall_note(self):
        self.order.status = OrderStatus.UNASSIGNED
        self.order.save(update_fields=["status"])

        with self._patched_client():
            response = self._post()

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertIn("内部抢单大厅", response.data["msg"])
        self.assertIn("发单成功", response.data["msg"])

    def test_an_order_already_in_the_hall_has_no_extra_message(self):
        with self._patched_client():
            response = self._post()
        self.assertEqual(response.data["msg"], "发单成功")


class RequirementAndOffSellDaysTests(_PushTestBase):
    """⭐ 页面「代练要求」与「自动下架天数」要真发出去。

    - 代练要求：页面填的值优先（写进 `Actors` 第五段），空 ⇒ 用全局模板；
    - 自动下架天数：上游 `OffSellDays`（不传 = 用上游默认 7 天，⛔ 不发 0）。
    """

    def test_the_page_requirement_goes_into_the_fifth_actors_segment(self):
        self._call_push(requirement="按单号核对，晚上在线")

        actors = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertEqual(actors[4], "按单号核对，晚上在线")

    def test_an_empty_page_requirement_falls_back_to_the_global_template(self):
        self._call_push(requirement="   ")

        actors = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertIn("接单后联系客服获取账号", actors[4])

    def test_the_page_requirement_beats_the_global_template(self):
        settings = PlatformPushSettings.get_solo()
        settings.require_template = "全局模板：禁止写账号信息"
        settings.save(update_fields=["require_template"])

        self._call_push(requirement="本单要求：只打排位")

        actors = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertEqual(actors[4], "本单要求：只打排位")

    def test_the_page_requirement_is_truncated_to_the_actors_limit(self):
        from apps.business.dailiantong_push import REQUIRE_TEMPLATE_MAX_LENGTH

        self._call_push(requirement="要" * (REQUIRE_TEMPLATE_MAX_LENGTH + 100))

        actors = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertEqual(len(actors[4]), REQUIRE_TEMPLATE_MAX_LENGTH)

    def test_off_sell_days_is_sent_when_given(self):
        self._call_push(off_sell_days=7)
        self.assertEqual(self.fake.forms_for("LevelOrderAdd")[0]["OffSellDays"], "7")

    def test_off_sell_days_is_absent_when_not_given(self):
        self._call_push()
        self.assertNotIn("OffSellDays", self.fake.forms_for("LevelOrderAdd")[0])


class RequirementAndOffSellDaysApiTests(_PushTestBase):
    """I3 的接口层：两个新键真进服务层；坏形状 400（⛔ 不 500）。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="push-i3-api", groups=("客服",), nodes=(NODE, PLATFORM_WRITE_NODE)
        )

    def _post(self, payload):
        return self.client.post(URL_PUSH, payload, content_type="application/json")

    def test_the_api_forwards_requirement_and_off_sell_days(self):
        with self._patched_client():
            response = self._post(
                {
                    "price": "5.00",
                    "zone_server_id": ZONE_ID,
                    "requirement": "本单要求：不写账号",
                    "off_sell_days": 7,
                }
            )
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form["Actors"].split("|*|")[4], "本单要求：不写账号")
        self.assertEqual(form["OffSellDays"], "7")

    def _assert_validation_error(self, response):
        # 本仓统一信封：入参形状错 ⇒ **HTTP 200 + code=1000**（同 `push-to-platform` 的既有口径）。
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.data["code"], 1000, response.data)

    def test_zero_off_sell_days_is_rejected_by_the_serializer(self):
        """「不设置」的正确形状是**不上送**；`0` 不是有效天数（⛔ 别发 0 过去）。"""
        self._assert_validation_error(
            self._post({"price": "5.00", "zone_server_id": ZONE_ID, "off_sell_days": 0})
        )

    def test_malformed_off_sell_days_is_a_business_error_not_a_500(self):
        for bad in ("abc", 31, -1):
            with self.subTest(bad=bad):
                self._assert_validation_error(
                    self._post(
                        {"price": "5.00", "zone_server_id": ZONE_ID, "off_sell_days": bad}
                    )
                )

    def test_an_overlong_requirement_is_a_business_error_not_a_500(self):
        from apps.business.dailiantong_push import REQUIRE_TEMPLATE_MAX_LENGTH

        self._assert_validation_error(
            self._post(
                {
                    "price": "5.00",
                    "zone_server_id": ZONE_ID,
                    "requirement": "要" * (REQUIRE_TEMPLATE_MAX_LENGTH + 1),
                }
            )
        )


# ── P4：重复抛单的文案要能被页面展示────────────────────────


class AlreadyPushedMessageTests(_PushTestBase):
    """业务方（2026-10-27）：抛单按钮**常驻**，已抛过就点进去被拒——
    所以拒绝文案必须让客服一眼看出「已有平台单号是哪一个」，⛔ 不能只说不许重复。
    """

    def test_the_message_carries_the_existing_serial_and_a_says_already_pushed(self):
        self._make_push(serial_no=SERIAL_OK)
        message = self._gate_message()
        self.assertIn("已抛过", message)
        self.assertIn(SERIAL_OK, message, "拒绝文案要带已有平台单号")
        self.assertNotIn("LevelOrderAdd", self.fake.actions)

    def test_the_unknown_row_message_also_says_already_pushed(self):
        self._make_push(state=DailiantongPush.State.UNKNOWN, serial_no=None)
        message = self._gate_message()
        self.assertIn("已抛过", message)
        self.assertIn("已有平台单号", message)

    def test_the_pending_row_message_also_says_already_pushed(self):
        self._make_push(state=DailiantongPush.State.PENDING)
        message = self._gate_message()
        self.assertIn("已抛过", message)


# ── P8 / P9：发单说明以页面填的内容为准──────────────────────


class ExplicitMemoTests(_PushTestBase):
    """业务方原话：在「当前订单说明」里改了内容，发出去**还是默认值** `其它:无`。"""

    def setUp(self):
        super().setUp()
        self.order.content = "客户原始需求：星耀上王者"
        self.order.remark = "重要备注：晚上八点后上号"
        self.order.save(update_fields=["content", "remark", "updated_at"])

    def test_the_page_memo_is_sent_verbatim(self):
        page_memo = "页面里改过的说明（保留空格）\n第二行"
        self._call_push(memo=page_memo)
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form.get("Memo"), page_memo, "传了就一字不改发给代练通")
        self.assertNotIn("客户原始需求", form.get("Memo"))

    def test_project_title_is_the_fourth_actors_segment(self):
        """⭐ 业务方改准：第 4 段 = **代练项目标题**，⛔ 不是页面 memo。"""
        page_memo = "页面里的当前订单说明（不该进 Actors 第 4 段）"
        self._call_push(memo=page_memo)

        form = self.fake.forms_for("LevelOrderAdd")[0]
        parts = form["Actors"].split("|*|")
        self.assertEqual(parts[3], "星耀上王者", "必须是 project_text")
        self.assertNotIn(page_memo, form["Actors"])
        self.assertNotIn("其它:无", form["Actors"])
        # 预览与真发同源：`current_info` 就是第 4 段的实际文本。
        data = push_module.preview_push(self.order, memo=page_memo)
        self.assertEqual(data["current_info"], parts[3])

    def test_without_a_project_title_the_fourth_segment_is_a_neutral_fallback(self):
        Order.objects.filter(pk=self.order.pk).update(project_text="", project=None)
        self.order.refresh_from_db(fields=["project_text", "project"])
        self._call_push()
        parts = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertEqual(parts[3], "代练订单")
        self.assertNotIn("其它:无", self.fake.forms_for("LevelOrderAdd")[0]["Actors"])

    def test_a_long_memo_never_enters_the_fourth_actors_segment(self):
        """业务方改准：memo 再长也只进 `Memo`，⛔ 不挤占第 4 段。"""
        memo_text = "客户要求只用射手，不要打野，尽量白天上线，" * 30
        self._call_push(memo=memo_text)
        form = self.fake.forms_for("LevelOrderAdd")[0]
        parts = form["Actors"].split("|*|")
        self.assertEqual(parts[3], "星耀上王者")
        self.assertLessEqual(len(parts[3]), push_module.ACTORS_CURRENT_INFO_MAX_LENGTH)
        self.assertLessEqual(len(form["Actors"]), 1024, "整列不能超上游 1024")
        self.assertEqual(form["Memo"], memo_text, "Memo 字段照旧原样发")

    def test_an_overlong_project_title_is_truncated_with_an_ellipsis(self):
        long_title = "客户要求只用射手，不要打野，尽量白天上线，" * 30
        Order.objects.filter(pk=self.order.pk).update(project_text=long_title)
        self.order.refresh_from_db(fields=["project_text"])
        self._call_push()
        parts = self.fake.forms_for("LevelOrderAdd")[0]["Actors"].split("|*|")
        self.assertLessEqual(
            len(parts[3]), push_module.ACTORS_CURRENT_INFO_MAX_LENGTH
        )
        self.assertTrue(parts[3].endswith("…"), parts[3][-10:])
        self.assertIn(parts[3][-2], push_module.TITLE_BOUNDARY_CHARS + "…")

    def test_without_memo_the_content_and_remark_are_used(self):
        self._call_push()
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertIn("星耀上王者", form["Memo"])
        self.assertIn("晚上八点后上号", form["Memo"])

    def test_an_empty_page_memo_means_no_memo_at_all(self):
        self._call_push(memo="")
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertNotIn("Memo", form, "空串 = 这次不带备忘（⛔ 回落成默认值）")

    def test_a_too_long_memo_is_a_chinese_error_and_no_write(self):
        message = self._gate_message(memo="x" * (push_module.MEMO_MAX_LENGTH + 1))
        self.assertIn(str(push_module.MEMO_MAX_LENGTH), message)
        self.assertNotIn("LevelOrderAdd", self.fake.actions)

    def test_preview_returns_the_page_memo(self):
        data = push_module.preview_push(self.order, memo="页面说明")
        self.assertEqual(data["memo"], "页面说明")

    def test_preview_without_memo_returns_the_composed_value(self):
        data = push_module.preview_push(self.order)
        self.assertIn("星耀上王者", data["memo"])

    def test_the_api_passes_the_page_memo_through(self):
        client = make_logged_in_client(
            username="push-memo-api", groups=("客服",), nodes=(NODE, PLATFORM_WRITE_NODE)
        )
        with self._patched_client():
            response = client.post(
                URL_PUSH,
                {
                    "price": "5.00",
                    "zone_server_id": ZONE_ID,
                    "memo": "页面说明（API）",
                },
                content_type="application/json",
            )
        self.assertEqual(response.data["code"], 0, response.data)
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form.get("Memo"), "页面说明（API）")


# ── 频道 / 标签 / 同发内部抢单价──────────────────────────────


class ChannelLabelAndHallPriceTests(_PushTestBase):
    def test_groups_and_labels_are_forwarded_to_upstream(self):
        self._call_push(
            groups="GOODCHANNEL", label1="加急", label2="老客户"
        )
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(form.get("Groups"), "GOODCHANNEL")
        self.assertEqual(form.get("Label1"), "加急")
        self.assertEqual(form.get("Label2"), "老客户")

    def test_groups_and_labels_are_optional(self):
        self._call_push()
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertNotIn("Groups", form)
        self.assertNotIn("Label1", form)

    def test_a_negative_hall_price_is_rejected_before_any_upstream_call(self):
        message = self._gate_message(hall_price_same=Decimal("-1.00"))
        self.assertIn("同发内部抢单价不能是负数", message)
        self.assertEqual(self.fake.calls, [])

    def test_hall_price_same_is_written_before_publishing_to_the_hall(self):
        """单是「未分配」⇒ 抛代练通前会补发内部大厅；页面填的抢单价要用上。"""
        self.order.status = OrderStatus.UNASSIGNED
        self.order.hall_price = None
        self.order.save(update_fields=["status", "hall_price", "updated_at"])

        with self._patched_client():
            push = push_module.push_order(
                self.order, operator=self.operator, hall_price_same=Decimal("8.00"),
                **_push_kwargs()
            )

        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("8.00"))
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertIn("同发内部抢单价", push.hall_note)

    def test_without_hall_price_same_the_platform_price_wins(self):
        """⭐ 2026-10-28：没传「同发内部抢单价」⇒ 大厅价 = **本次发平台价格**（不是旧发单价）。"""
        self.order.status = OrderStatus.UNASSIGNED
        self.order.hall_price = Decimal("6.00")
        self.order.save(update_fields=["status", "hall_price", "updated_at"])

        with self._patched_client():
            push = push_module.push_order(
                self.order, operator=self.operator, **_push_kwargs()
            )

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.hall_price, Decimal("5.00"), "大厅价必须等于本次发平台价格"
        )
        self.assertIn("大厅价 = 发平台价格 5.00", push.hall_note)


class InternalAssignWithdrawsPlatformTests(_PushTestBase):
    """⭐ 2026-10-28：指派 / 抢单成功后**立刻**撤掉代练通订单（同步返回结果）。"""

    def setUp(self):
        super().setUp()
        self.order.hall_price = Decimal("5.00")
        self.order.save(update_fields=["hall_price"])
        # 打手要过「游戏范围」闸 ⇒ 用统一夹具建（默认勾当前租户全部游戏）。
        make_logged_in_client(
            username="internal-assign-taker", groups=("代练员",)
        )
        self.taker = self.user_model.objects.get(username="internal-assign-taker")
        self.client = make_logged_in_client(
            username="internal-assign-cs",
            groups=("客服",),
            # `order.add_edit` 给下面 G1 那条「编辑订单不许动平台单」用。
            nodes=("order.assign_taker", "order.add_edit"),
        )

    def _post_assign(self):
        with self._patched_client():
            return self.client.post(
                f"/api/business/orders/{self.order.order_no}/assign/",
                {"user_id": self.taker.pk},
                content_type="application/json",
            )

    def _push(self, *, status, serial="19000000000000000151"):
        return self._make_push(
            serial_no=serial,
            remote_status=status,
            remote_cancel_status=10,
            is_active=True,
        )

    def test_without_a_push_there_is_no_upstream_call(self):
        response = self._post_assign()
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["msg"], "")
        self.assertEqual(self.fake.calls, [])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_unaccepted_remote_is_deleted_immediately(self):
        self._push(status=11)
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        response = self._post_assign()
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertIn("已撤回代练通订单", response.data["msg"])
        self.assertIn("LevelOrderDelSelf", self.fake.actions)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS, "本地指派必须已生效")

    def test_off_shelf_remote_is_deleted_immediately(self):
        """⭐ `Status=18 已下架` 与 11 / 10 / 空同一族 ⇒ 也走删除。

        即时路径当天已按业务方口径放宽到 18（真机实测通过）；本地指派之后必须把平台单删掉，
        否则「已下架」的行会一直挂着（页面显示「平台还有单」）。
        """
        self._push(status=18)
        self.fake.detail = {"Status": 18, "CancelStatus": 10}
        self.fake.next_details = []
        response = self._post_assign()
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertIn("已撤回代练通订单", response.data["msg"])
        self.assertIn("LevelOrderDelSelf", self.fake.actions)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_accepted_remote_is_rejected_and_never_touches_upstream(self):
        """⭐ 业务方 2026-10-28 新口径：远端 12/13/14/15 已接单 ⇒ **不许再指派**。

        ⛔ 不再「先指派、再自动协商撤销」：响应不能报成功、下一轮回读又把打手换掉，
        而且平台打手与内部打手会同时干同一张单。判据 = 中文拒 + 零上游调用 +
        本地状态与推送行一个字节都不动。
        """
        for status in (12, 13, 14, 15):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.order.status = OrderStatus.WAITING_GRAB
                self.order.save(update_fields=["status", "updated_at"])
                push = self._push(status=status, serial=f"1900000000000000016{status}")
                response = self._post_assign()
                self.assertEqual(response.status_code, 200, response.content[:400])
                self.assertEqual(response.data["code"], 1002, response.data)
                msg = response.data["msg"]
                self.assertIn("这张单已被代练通接单", msg)
                self.assertIn("不能再指派", msg)
                self.assertIn("协商撤销", msg)
                self.assertEqual(self.fake.calls, [], "拒绝必须在任何上游调用之前")
                self.assertEqual(self.fake.forms_for("LevelOrderCancel"), [])
                self.order.refresh_from_db()
                self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
                push.refresh_from_db()
                self.assertIsNone(push.cancel_requested_at, "⛔ 不许替客服发起协商撤销")
                # 收尾：让下一轮 subTest 不被「同单同平台已有有效推送」干扰。
                push.delete()

    def test_upstream_refusal_returns_a_clear_warning_and_keeps_the_assignment(self):
        self._push(status=11)
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        self.fake.delete_response = {"Result": -1, "Err": "删除失败."}
        response = self._post_assign()
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, "指派已生效 ⇒ 仍是成功码")
        msg = response.data["msg"]
        self.assertIn("代练通订单未能撤回", msg)
        self.assertIn("删除失败.", msg)
        self.assertIn("第三方平台订单管理", msg)
        self.assertIn("请先", msg)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, self.taker.pk, "本地指派不许被回滚")
        # ⭐ M3（审计）：失败要留痕（页面「最近同步结论」看得到），
        # ⛔ 不能只在弹窗 msg 里说一句就没了。
        push = DailiantongPush.objects.get(order=self.order)
        self.assertIn("撤回失败", push.remote_note)
        self.assertIn("删除失败.", push.remote_note)
        self.assertIsNone(
            push.claim_withdraw_at, "失败不记「已撤」⇒ 兜底下一轮还能接手重试"
        )

    def test_the_withdraw_gate_refuses_without_a_real_taker(self):
        """⭐ 没有**真人**接手人 ⇒ 零上游调用。

        编辑订单（`partial_update`）曾经也调这个函数（挂点已摘）；这道闸留在函数里，
        回答的是「这次动作的业务前提成不成立」——只有占位账号「代练通」不算真人。
        """
        from apps.business.dailiantong_actions import withdraw_for_internal_claim

        self._push(status=11)
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        self.fake.calls.clear()
        # 还没有任何 MAIN 接手人（夹具默认空行）⇒ 不撤。
        had_push, msg = withdraw_for_internal_claim(self.order)
        self.assertFalse(had_push)
        self.assertEqual(msg, "")
        self.assertEqual(self.fake.calls, [])
        self.assertNotIn("LevelOrderDelSelf", self.fake.actions)

    def test_a_terminal_remote_push_is_not_withdrawn(self):
        """⭐ 远端终态（17 已结算）⇒ 一句「无需撤回」，零上游调用。"""
        from apps.business.dailiantong_actions import withdraw_for_internal_claim

        push = self._push(status=17)
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("5.00"),
        )
        self.fake.calls.clear()

        had_push, msg = withdraw_for_internal_claim(self.order)

        self.assertTrue(had_push)
        self.assertIn("已经结束", msg)
        self.assertEqual(self.fake.calls, [])
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_an_off_shelf_remote_is_deleted_by_the_same_primitive(self):
        """⭐ G2：`Status=18 已下架` 走 `LevelOrderDelSelf`（与即时路径同一份原语）。"""
        from apps.business.dailiantong_actions import withdraw_for_internal_claim

        push = self._push(status=18)
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("5.00"),
        )
        self.fake.detail = {"Status": 18, "CancelStatus": 10}
        self.fake.next_details = []

        with self._patched_client():
            had_push, msg = withdraw_for_internal_claim(self.order)

        self.assertTrue(had_push)
        self.assertIn("已撤回代练通订单", msg)
        self.assertIn("LevelOrderDelSelf", self.fake.actions)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)

    def test_editing_the_order_never_touches_the_platform_push(self):
        """⭐ 编辑订单（PATCH）**不许**撤回 / 协商撤销。

        「指派成功后立刻撤回」曾被挂在 `partial_update` 尾部，而编辑接口根本不能指派 ⇒
        客服改个备注就会把一张真钱平台单删掉 / 发起协商撤销。修法是**摘掉挂点**，
        撤回只留在真正指派成功的地方。
        """
        push = self._push(status=11)
        before_note = push.remote_note
        self.fake.calls.clear()

        with self._patched_client():
            response = self.client.patch(
                f"/api/business/orders/{self.order.order_no}/",
                {"remark": "只是改个备注"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(self.fake.calls, [], "编辑订单⛔ 不许有任何上游调用")
        self.assertNotIn("LevelOrderDelSelf", self.fake.actions)
        self.assertNotIn("LevelOrderCancel", self.fake.actions)
        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertIsNone(push.cancel_requested_at)
        self.assertEqual(push.remote_note, before_note)

    def _post_batch_assign(self):
        return self.client.post(
            "/api/business/orders/batch/assign/",
            {
                "order_nos": [self.order.order_no],
                "user_id": self.taker.pk,
                "dry_run": False,
            },
            content_type="application/json",
        )

    def test_batch_assign_rejects_a_platform_taken_order(self):
        """⭐ 批量与单笔同一口径——远端已接单 ⇒ 拒，零上游。"""
        self._push(status=12)
        self.fake.calls.clear()

        with self._patched_client():
            response = self._post_batch_assign()

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 1002, response.data)
        self.assertIn("这张单已被代练通接单", response.data["msg"])
        self.assertEqual(self.fake.calls, [], "被拒的批量不许有任何上游调用")
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)
        self.assertFalse(
            Performance.objects.filter(
                order=self.order, emp_type=EmpType.MAIN, user__isnull=False
            ).exists(),
            "被拒的批量不许落任何接手人",
        )

    def test_batch_assign_withdraws_the_platform_order_after_commit(self):
        """⭐ M1：未接手的已抛单 ⇒ 批量指派成功，且**提交之后**逐单撤回（与单笔同原语）。"""
        self._push(status=11)
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        self.fake.next_details = []

        with self._patched_client():
            response = self._post_batch_assign()

        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertIn(self.order.order_no, response.data["data"]["applied"])
        self.assertIn("已撤回代练通订单", response.data["msg"])
        self.assertIn("LevelOrderDelSelf", self.fake.actions)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)


    def test_the_claim_path_also_withdraws_immediately(self):
        self._push(status=11)
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        # 抢单是**条件更新 MAIN 绩效行**（user IS NULL）；补齐录单时应有的 MAIN 行。
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN)
        claimer = make_logged_in_client(
            username="internal-claim-taker",
            groups=("代练员",),
            nodes=("grab_hall.take_order",),
        )
        with self._patched_client():
            response = claimer.post(
                f"/api/business/hall/{self.order.order_no}/claim/",
                {},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertIn("已撤回代练通订单", response.data["msg"])
        self.assertIn("LevelOrderDelSelf", self.fake.actions)


class PlatformRunningCriterionTests(_PushTestBase):
    """⭐ 前后端统一「什么算还在平台跑」。

    判据 = **`state=sent` 且 `is_active=True`**；终态回读（17 / CancelStatus 12/15/16）
    在本地状态写回成功后把行 **finalize**（closed + is_active=False），让 A2 闸与前端
    `hasActivePlatformPush = is_active` 同判据。
    """

    def _push(self, **overrides):
        payload = {
            "order": self.order,
            "platform": 10,
            "state": DailiantongPush.State.SENT,
            "serial_no": "19000000000000000201",
            "price": Decimal("5.00"),
            "ensure1": Decimal("0.00"),
            "ensure2": Decimal("0.00"),
            "time_limit_hours": 24,
            "ex_order_no": self.order.order_no,
            "attempts": 1,
        }
        payload.update(overrides)
        return DailiantongPush.objects.create(**payload)

    def test_an_inactive_push_no_longer_locks_local_status(self):
        """`state=sent` 但 `is_active=False`（终态 / 回读停轮询）⇒ 不再锁本地状态。"""
        push = self._push(is_active=False)
        self.assertIsNone(push_module.push_locks_local_status(self.order))
        self.assertIsNone(push_module.local_status_locked_denial(self.order))

    def test_an_active_push_still_locks_local_status(self):
        self._push(is_active=True)
        self.assertIsNotNone(push_module.push_locks_local_status(self.order))
        self.assertIn("已经抛到代练通", push_module.local_status_locked_denial(self.order))

    def test_a_terminal_readback_finalizes_the_push_row(self):
        """终态 ⇒ closed + is_active=False + 可读原因（把「撤销完成的旧行一直是 sent」收口）。"""
        from unittest import mock

        push = self._push()
        self.fake.detail = {"Status": 17, "CancelStatus": 10}

        with self._patched_client(), mock.patch.object(
            push_module, "apply_remote_status", return_value=""
        ):
            push_module.refresh_push_status(push)

        push.refresh_from_db()
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertFalse(push.is_active)
        self.assertIn("已收口", push.remote_note)

# ── 余额不足预警（运营配套）──────────────────────────────────


URL_BALANCE_ALERT = "/api/business/platform-integrations/balance-alert/"


class BalanceAlertTests(_PushTestBase):
    def setUp(self):
        super().setUp()
        push_module.clear_summary_cache()
        self.addCleanup(push_module.clear_summary_cache)

    def _pending_order(self, order_no, price):
        return Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=order_no,
            status=OrderStatus.WAITING_GRAB,
            project_text="待发单",
            total_price=Decimal("100.00"),
            hall_price=price,
        )

    def test_it_sums_the_prices_of_orders_waiting_to_be_pushed(self):
        self._pending_order("20261027000000000101", Decimal("5.00"))
        self._pending_order("20261027000000000102", Decimal("7.00"))
        # 这张单已经有有效推送 ⇒ 不该再计入（钱已经冻结过 / 已在平台上）。
        pushed = self._pending_order("20261027000000000103", Decimal("9.00"))
        self._make_push(order=pushed, serial_no="19000000000000000031")
        self.fake.balance = "3.00"

        with self._patched_client():
            data = push_module.balance_alert(force_refresh=True)

        self.assertEqual(data["available"], "3.00")
        self.assertEqual(data["needed"], "12.00")
        self.assertEqual(data["pending_orders"], 2)
        self.assertTrue(data["is_low"])
        self.assertEqual(data["shortfall"], "9.00")
        self.assertIn("低于", data["message"])

    def test_enough_balance_is_not_low(self):
        self._pending_order("20261027000000000104", Decimal("5.00"))
        self.fake.balance = "50.00"
        with self._patched_client():
            data = push_module.balance_alert(force_refresh=True)
        self.assertFalse(data["is_low"])
        self.assertEqual(data["shortfall"], "0.00")
        self.assertIn("覆盖", data["message"])

    def test_unpriced_orders_are_counted_but_not_summed(self):
        # 夹具那张单（`hall_price` / `platform_price` 都是空）本身也是「没定价」的一张。
        with self._patched_client():
            before = push_module.balance_alert(force_refresh=True)
        Order.objects.create(
            game=self.game,
            server=self.server,
            order_no="20261027000000000105",
            status=OrderStatus.WAITING_GRAB,
            project_text="没定价",
        )
        self.fake.balance = "50.00"
        with self._patched_client():
            data = push_module.balance_alert(force_refresh=True)
        self.assertEqual(data["unpriced_orders"], before["unpriced_orders"] + 1)
        self.assertEqual(data["needed"], "0.00")
        self.assertIn("还没定价", data["message"])

    def test_an_upstream_balance_failure_is_a_chinese_error_not_low(self):
        self._pending_order("20261027000000000106", Decimal("5.00"))
        # `UserMoney` 回一个解析不了的余额 ⇒ 摘要里是中文 error，且⛔ 不判断够不够。
        self.fake.balance = "not-a-number"
        with self._patched_client():
            data = push_module.balance_alert(force_refresh=True)
        self.assertIsNone(data["available"])
        self.assertFalse(data["is_low"], "查不到余额时⛔ 不许说「余额充足/不足」")
        self.assertIn("余额", data["error"])

    def test_the_api_returns_the_alert(self):
        self._pending_order("20261027000000000107", Decimal("5.00"))
        self.fake.balance = "1.00"
        client = make_logged_in_client(
            username="push-balance-api", groups=("客服",), nodes=(NODE,)
        )
        with self._patched_client():
            response = client.get(URL_BALANCE_ALERT)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertTrue(payload["data"]["is_low"])


# ── D8（fx-D）：撤销申请后允许重发 + 状态只认最新未结束推送 ──────


class RepushAfterCancelApplicationTests(_PushTestBase):
    """业务方口径：

    - 没申请撤销 ⇒ 拒（原话「当前订单已被代练通接手，如要重发请先申请撤销」）；
    - 已申请撤销 ⇒ 允许重发，新推送行记 `replaces_push`；旧行**照旧**轮询（不置 closed）；
    - 本地状态只认**最新一条未结束推送**：旧行撤销完成不许覆盖新行的「代练中」；
    - 所有未结束推送都结束后，旧的撤销结果才允许落本地。
    """

    OLD_SERIAL = "19000000000000000701"
    NEW_SERIAL = "19000000000000000702"

    def _old_push(
        self, *, cancel_requested, remote_status=12, cancel_status=10,
        cancel_initiator="",
    ):
        push = self._make_push(
            serial_no=self.OLD_SERIAL,
            remote_status=remote_status,
            remote_cancel_status=cancel_status,
            remote_taker_name="失联打手",
        )
        fields = []
        if cancel_requested:
            push.cancel_requested_at = timezone.now()
            fields.append("cancel_requested_at")
        if cancel_initiator:
            # ⭐ L2：回读判定出的撤销发起方（"us" / "taker"；空 = 判不了）。
            push.cancel_initiator = cancel_initiator
            fields.append("cancel_initiator")
        if fields:
            push.save(update_fields=[*fields, "updated_at"])
        return push

    def test_without_a_cancel_application_the_repush_is_rejected_with_the_wording(self):
        self._old_push(cancel_requested=False)
        message = self._gate_message()
        self.assertIn("当前订单已被代练通接手，如要重发请先申请撤销", message)
        self.assertEqual(DailiantongPush.objects.count(), 1)

    def test_after_the_cancel_application_the_repush_is_allowed_and_records_replaces(self):
        # ⭐ C1：本地标记 + 远端 11（在途）两个证据同向才放行。
        old = self._old_push(cancel_requested=True, cancel_status=11)
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])

        push = self._call_push()

        self.assertEqual(push.replaces_push_id, old.pk)
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        old.refresh_from_db()
        self.assertEqual(old.state, DailiantongPush.State.SENT, "旧行⛔ 不许被置 closed")
        self.assertTrue(old.is_active, "旧行还要继续轮询 ⇒ is_active 留 True")
        self.assertEqual(DailiantongPush.objects.filter(order=self.order).count(), 2)

    def test_a_legacy_us_initiated_cancel_11_is_backfilled_on_repush(self):
        """⭐ 只有**判定为我方发起**（`CancelUID == CreateUID`）才回填。

        历史行本地没有 `cancel_requested_at`、远端只有 `CancelStatus=11` ⇒ 真重发前要补记
        本地依据（D8 的一致性 / 审计）。`cancel_initiator` 就是回读时按 `CancelUID`
        判定出来的发起方（唯一出处 `cancel_initiator_from_detail`）。
        """
        old = self._old_push(
            cancel_requested=False, remote_status=12, cancel_status=11,
            cancel_initiator="us",
        )
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])

        push = self._call_push()

        old.refresh_from_db()
        self.assertIsNotNone(old.cancel_requested_at, "真重发前要把本地依据补记下来")
        self.assertEqual(push.replaces_push_id, old.pk)

    def test_a_taker_initiated_cancel_11_is_never_backfilled_as_ours(self):
        """⛔ 打手发起的撤销**不许**回填成「我方发起」。

        回填的后果不是显示问题：客服会以为这是自己申请的那次协商撤销，去点「取消撤销」
        （想同意的撤销被自己撤掉）。
        ⚠️ 不回填 ⇒ 旧行仍占着部分唯一索引 ⇒ **本次重发显式拒**（不能为了能重发写假事实），
        并告诉客服下一步（在代练通同意撤销 / 等它处理完）。
        """
        old = self._old_push(
            cancel_requested=False, remote_status=12, cancel_status=11,
            cancel_initiator="taker",
        )
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])

        message = self._gate_message()

        self.assertIn("打手发起", message)
        self.assertIn("同意撤销", message)
        old.refresh_from_db()
        self.assertIsNone(
            old.cancel_requested_at, "打手发起的撤销被误标成我方发起"
        )
        self.assertEqual(
            DailiantongPush.objects.filter(order=self.order).count(), 1,
            "被拒的重发不许落第二行（第二张真钱单）",
        )

    def test_an_unknown_initiator_is_not_backfilled(self):
        """⛔ `CancelUID` 拿不到 / 判不出（`cancel_initiator == ""`）⇒ 不回填。

        宁可不写一条无法证实的本地记录（保守：不把「想同意的撤销」标成我方发起的）；
        代价是这次重发被拒（旧行仍占索引），文案让人去代练通后台核对。
        """
        old = self._old_push(
            cancel_requested=False, remote_status=12, cancel_status=11,
            cancel_initiator="",
        )
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])

        message = self._gate_message()

        self.assertIn("判不出发起方", message)
        self.assertIn("CancelUID", message)
        old.refresh_from_db()
        self.assertIsNone(old.cancel_requested_at)
        self.assertEqual(DailiantongPush.objects.filter(order=self.order).count(), 1)

    def test_stale_local_marker_with_a_cancelled_remote_is_rejected_and_cleared(self):
        """⭐ C1：远端 12/10（撤销已取消）+ 本地旧标记 ⇒ 拒，并清掉本地标记。"""
        old = self._old_push(cancel_requested=True, remote_status=12, cancel_status=10)

        message = self._gate_message()

        self.assertIn("当前订单已被代练通接手", message)
        self.assertIn("先申请撤销", message)
        old.refresh_from_db()
        self.assertIsNone(old.cancel_requested_at, "陈旧本地标记必须被清掉")
        self.assertIn("取消", old.remote_note)
        self.assertTrue(
            OperationLog.objects.filter(
                action="order.dailiantong_withdraw",
                order_no=self.order.order_no,
                reason__contains="取消",
            ).exists()
        )

    def test_cancelling_16_10_gets_the_accurate_message(self):
        """⭐ M3：16/10 已在撤销中，⛔ 不能再叫客服「先申请撤销」。"""
        self._old_push(cancel_requested=False, remote_status=16, cancel_status=10)
        message = self._gate_message()
        self.assertIn("撤销中", message)
        self.assertIn("取消撤销", message)
        self.assertNotIn("先申请撤销", message)

    def _repush_pair(self):
        old = self._old_push(cancel_requested=True, remote_status=12, cancel_status=11)
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        new = self._call_push()
        # 模拟「新单已被代练通接手」：本地先到「代练中」，再看旧撤销能否把它盖掉。
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        return old, new

    def test_the_old_cancellation_does_not_override_the_new_in_progress(self):
        old, new = self._repush_pair()
        self.fake.detail = {"Status": 16, "CancelStatus": 12}

        with self._patched_client():
            refresh_push_status(old)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.status,
            OrderStatus.IN_PROGRESS,
            "旧推送撤销完成⛔ 不许覆盖新推送的「代练中」",
        )

    def test_the_new_push_still_drives_the_local_status(self):
        old, new = self._repush_pair()
        new.refresh_from_db()
        old.refresh_from_db()
        self.fake.detail = {"Status": 12, "CancelStatus": 10}

        with self._patched_client():
            refresh_push_status(new)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        old.refresh_from_db()
        self.assertEqual(old.remote_status, 12, "旧行没被这次刷新碰到")

    def test_the_new_settled_push_wins_while_the_old_cancel_is_pending(self):
        old, new = self._repush_pair()
        self.fake.detail = {"Status": 17, "CancelStatus": 10}

        with self._patched_client():
            refresh_push_status(new)

        self.order.refresh_from_db()
        new.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(new.state, DailiantongPush.State.CLOSED)
        self.assertEqual(old.state, DailiantongPush.State.SENT)
        self.assertTrue(old.is_active)

    def test_repush_cannot_close_the_latest_unapplied_settlement(self):
        old, new = self._repush_pair()
        DailiantongPush.objects.filter(pk=new.pk).update(
            remote_status=17, remote_cancel_status=10
        )
        dlt_calls = self.fake.actions.count("LevelOrderAdd")

        message = self._gate_message()

        self.assertIn("代练通已结算，本地正在同步完单", message)
        new.refresh_from_db()
        self.assertEqual(new.state, DailiantongPush.State.SENT)
        self.assertTrue(new.is_active)
        self.assertEqual(self.fake.actions.count("LevelOrderAdd"), dlt_calls)

    def test_old_settlement_retries_if_the_new_pending_push_fails(self):
        old, new = self._repush_pair()
        DailiantongPush.objects.filter(pk=new.pk).update(
            state=DailiantongPush.State.PENDING, serial_no=None
        )
        self.fake.detail = {"Status": 17, "CancelStatus": 10}

        with self._patched_client():
            refresh_push_status(old)

        self.order.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(old.state, DailiantongPush.State.SENT)
        self.assertTrue(old.is_active)
        self.assertIn("新推送结果尚未确认", old.last_error)

        DailiantongPush.objects.filter(pk=new.pk).update(
            state=DailiantongPush.State.FAILED, is_active=False
        )
        with self._patched_client():
            refresh_push_status(old)

        self.order.refresh_from_db()
        old.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertEqual(old.state, DailiantongPush.State.CLOSED)
        self.assertFalse(old.is_active)
        self.assertEqual(old.last_error, "")

    def test_an_old_settled_push_cannot_override_a_new_active_push(self):
        old, new = self._repush_pair()
        self.fake.detail = {"Status": 17, "CancelStatus": 10}

        with self._patched_client():
            refresh_push_status(old)

        self.order.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(new.state, DailiantongPush.State.SENT)
        self.assertTrue(new.is_active)

    def test_once_the_new_push_is_gone_the_old_cancellation_lands(self):
        old, new = self._repush_pair()
        new.state = DailiantongPush.State.FAILED
        new.save(update_fields=["state"])
        self.fake.detail = {"Status": 16, "CancelStatus": 12}

        with self._patched_client():
            refresh_push_status(old)

        self.order.refresh_from_db()
        self.assertEqual(
            self.order.status,
            OrderStatus.REFUNDED,
            "没有未结束的新推送时，旧的撤销结果应落到本地",
        )

    def test_when_every_push_is_finished_the_latest_sent_row_still_wins(self):
        """新行也结束时：按**最新**那条 `sent` 的终态落，⛔ 不让旧行盖掉它。"""
        old, new = self._repush_pair()
        DailiantongPush.objects.filter(pk=old.pk).update(
            remote_status=17, remote_cancel_status=10
        )
        DailiantongPush.objects.filter(pk=new.pk).update(
            remote_status=17, remote_cancel_status=10
        )
        old.refresh_from_db()
        new.refresh_from_db()
        self.fake.detail = {"Status": 17, "CancelStatus": 10}

        with self._patched_client():
            refresh_push_status(new)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)

