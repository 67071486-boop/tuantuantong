"""代练通平台动作：撤回 / 改价 / 补时 / 留言。

**全程不访问真实网络**：上游一律用 `httpx.MockTransport` 收进 `DailiantongClient`；
服务层**唯一**客户端构造点是 `apps.business.dailiantong_actions._open_client`（委托抛单
模块的同一个构造点），本文件 patch 的正是**它被查找的那个模块**（`AGENTS.md` §5-E / E-0029）。

**判据口径**：写动作成败看**回读**（正常路径断言「上游写动作调一次 + 详情回读两次
（写前/写后）」+ **数据库终态**）；前置闸 / 状态不允许 ⇒ 零上游写调用（没有推送行的场景
连 `LevelOrderDetail` 都不该出现）；上游业务失败 ⇒ 中文文案带上游 `Err` **原文**、接口回
`code=1002`（⛔ 不是 500）；回读失败/不符 ⇒ 文案必须说「没能核验到」，⛔ 不报成功、
⛔ 不改本地行；凭据不回显 = 响应 JSON + 审计各列拼一句搜 `app_secret` / 支付密码 md5，
上游详情里的 `GamePass` 同样不许外泄；夹具一律**合成值**。
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl

import httpx
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_actions as actions_module
from apps.business import dailiantong_extra_actions as extra_module
from apps.business import dailiantong_push as dailiantong_push_module
from apps.business.dailiantong_client import DailiantongClient, DailiantongConfig
from apps.business.dailiantong_push import DailiantongPushError
from apps.business.models import (
    DailiantongPush,
    Game,
    GameServer,
    Order,
    OrderStatus,
    PlatformIntegration,
)
from tests.p5_auth_helpers import make_logged_in_client

#: 合成凭据（⛔ 不是任何真实密钥）。
APP_ID = "synth-dlt-action-app-01"
APP_SECRET = "synth-dlt-action-secret-c4e1"
PAY_PASSWORD_MD5 = "0123456789abcdef0123456789abcdef"

#: 合成上游单号 / 本地单号。
SERIAL = "19000000000000000061"
ORDER_NO = "20261027000000000001"

#: 四条动作共用的 RBAC 节点（与视图里的模块常量同值；测试里写死字面量）。
NODE = "order.list"
#: ⭐ M5（审计）：交易平台写动作的专属节点（原先这批写端点全挂 `order.list`）。
PLATFORM_WRITE_NODE = "order.platform_write"

URL_WITHDRAW = f"/api/business/orders/{ORDER_NO}/platform-push/withdraw/"
URL_PRICE = f"/api/business/orders/{ORDER_NO}/platform-push/price/"
URL_TIME = f"/api/business/orders/{ORDER_NO}/platform-push/time/"
URL_MESSAGE = f"/api/business/orders/{ORDER_NO}/platform-push/message/"
ALL_URLS = (URL_WITHDRAW, URL_PRICE, URL_TIME, URL_MESSAGE)

#: 上游业务失败的合成原文（用来钉「文案 = 上游原文」）。
UPSTREAM_ERR_TEXT = "只有未接手状态才能修改价格"

#: 详情里的占位明文（真实上游会把游戏密码明文回吐；本线绝不允许它进响应 / 审计）。
LEAKY_GAME_PASS = "SHOULD-NOT-LEAK-9f2c"


class FakeDailiantong:
    """假上游：记录调用；详情可以按顺序给出多份（写前 / 写后）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        #: 写前默认详情：已接手（走协商撤销分支）。
        self.detail = {
            "Status": 12,
            "CancelStatus": 10,
            "Price": "5.00",
            "TimeLimit": 24,
            "PayLevelBal": "2.00",
            "RepEnsureBal": "0.00",
            "GamePass": LEAKY_GAME_PASS,
        }
        #: 下一次 / 后续详情的**覆盖队列**（元素是 dict 或 `httpx` 异常实例）。
        self.next_details: list = []
        #: 写动作的默认回包（成功）。
        self.responses: dict[str, dict] = {}
        #: 这些动作抛传输超时（模拟「没拿到上游答复」）。
        self.timeout_actions: set[str] = set()
        self.detail_calls = 0
        #: `UserMoney` 回的可用余额（改价前的余额闸用）。
        self.balance = "100.00"
        #: 代练通侧订单要求模板的列表回包（`OrderRequireTempList`）。
        self.templates: list[dict] = []
        #: 列表回包的**覆盖队列**（删除 / 写后回读要给出与写前不同的列表）。
        self.template_pages: list[list[dict]] = []
        #: 新增模板时上游 `Err` 里的模板 ID。
        self.next_template_id = 7001
        #: P1 之后的读面（详情页）：留言 / 进度图的行（按上游形态的键）。
        self.message_rows: list[dict] = []
        self.progress_rows: list[dict] = []
        #: 进度回读里**是否**出现这次上传的对象名（False ⇒ 模拟「假成功」）。
        self.progress_readback_includes_upload = True
        #: 按日统计的裸对象回包（`LevelOrderStat`）。
        self.stat_payload: dict = {
            "LevelOrderStatList": [
                {"QDate": "2026-10-26", "PubCount": "3", "SettleCount": "2"}
            ],
            "LevelOrderStatTotal": {"PubCount": "3", "SettleCount": "2"},
        }

    @property
    def actions(self) -> list[str]:
        return [action for action, _form in self.calls]

    def forms_for(self, action: str) -> list[dict]:
        return [form for called, form in self.calls if called == action]

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8"), keep_blank_values=True))
        action = form.get("Action", "")
        self.calls.append((action, form))
        if action in self.timeout_actions:
            raise httpx.ReadTimeout("simulated timeout")
        if action == "UserMoney":
            return httpx.Response(
                200,
                json={"SumBal": self.balance, "FreezeBal": "0.00", "ActiveBal": self.balance},
            )
        if action == "LevelOrderDetail":
            self.detail_calls += 1
            # 第 1 次是**写前**回读（用 `self.detail`）；之后的回读依次用队列覆盖
            # （正常路径写前 / 写后两次；队列为空时退回 `self.detail`）。
            if self.detail_calls > 1 and self.next_details:
                item = self.next_details.pop(0)
                if isinstance(item, Exception):
                    raise item
                return httpx.Response(200, json=item)
            return httpx.Response(200, json=self.detail)
        if action == "GetSTS":
            return httpx.Response(
                200,
                json={
                    "SecurityToken": "synth-sts-token",
                    "AccessKeyId": "synth-sts-ak",
                    "AccessKeySecret": "synth-sts-sk",
                },
            )
        if action == "LevelOrderMessageList":
            return httpx.Response(
                200,
                json={
                    "LevelOrderMessageList": self.message_rows,
                    "RecordCount": len(self.message_rows),
                },
            )
        if action == "LevelOrderMessageAdd":
            # ⭐ FX3：上游收下留言后，列表回读应能看到它（页面「发送 → 历史」同源）。
            # ⚠️ 用例显式给了回包（例如上游拒收 `{"Result": -1, ...}`）⇒ 原样返回、
            #    不追加历史行——否则「上游拒绝」会被这个假上游悄悄改成成功。
            override = self.responses.get(action)
            if override is not None:
                return httpx.Response(200, json=override)
            self.message_rows.append(
                {
                    "Msg": form.get("Msg", ""),
                    "MsgType": 11,
                    "OperationID": "发单者",
                    "CreateDate": "2026-10-27 11:00:00",
                }
            )
            return httpx.Response(200, json={"Result": 1, "Err": "添加成功."})
        if action == "LevelOrderProgressList":
            return httpx.Response(
                200,
                json={
                    "LevelOrderProgressList": self.progress_rows,
                    "RecordCount": len(self.progress_rows),
                },
            )
        if action == "LevelOrderProgressAdd":
            img = form.get("Img") or ""
            if self.progress_readback_includes_upload and img:
                self.progress_rows.append(
                    {"Msg": form.get("Msg", ""), "Img": f"https://oss.synth/{img}"}
                )
            return httpx.Response(200, json={"Result": 1, "Err": "添加完成."})
        if action == "OrderRequireTempList":
            rows = self.template_pages.pop(0) if self.template_pages else self.templates
            return httpx.Response(
                200,
                json={
                    "OrderRequireTempList": rows,
                    "RecordCount": len(rows),
                },
            )
        if action == "OrderRequireTempAdd":
            return httpx.Response(
                200,
                json=self.responses.get(
                    action, {"Result": 1, "Err": str(self.next_template_id)}
                ),
            )
        if action == "LevelOrderStat":
            return httpx.Response(200, json=self.stat_payload)
        return httpx.Response(
            200, json=self.responses.get(action, {"Result": 1, "Err": "操作成功."})
        )


def _deleted_detail() -> dict:
    """删除后详情「查不到」的实测形态：上游回业务失败（不是 2xx 以外的东西）。"""
    return {"Result": -1, "Err": "没有找到该订单！"}


class _ActionTestBase(TenantTestCase):
    """租户 A：游戏 / 区服 / 订单 / 启用默认代练通配置 / 一条已发出的推送行。"""

    def setUp(self):
        from django.db import connection

        connection.set_tenant(self.tenant)
        # 撤销弹窗的默认金额有 30 秒模块级缓存（键 = schema + 上游单号）⇒ 用例之间清掉。
        actions_module.clear_withdraw_defaults_cache()
        self.addCleanup(actions_module.clear_withdraw_defaults_cache)
        self.user_model = get_user_model()
        self.operator = self.user_model.objects.create_user(
            username="dlt-action-operator",
            password="synth-pw-0002",
            first_name="平台操作员",
        )
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
            account="USR-SYNTH-ACTION-01",
            app_id=APP_ID,
            app_secret=APP_SECRET,
            pay_password_md5=PAY_PASSWORD_MD5,
            is_enabled=True,
            is_default=True,
            contact_phone="",
            contact_qq="",
        )
        self.fake = FakeDailiantong()
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=SERIAL,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=ORDER_NO,
            attempts=1,
        )

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
            "apps.business.dailiantong_actions._open_client",
            side_effect=self._client_factory,
        )

    def _api_client(self):
        # ⭐ M5：读端点要 `order.list`、写端点要 `order.platform_write` ⇒ 夹具两个都给
        # （各用例另有「少一个就 403」的专门判据）。
        return make_logged_in_client(
            username="dlt-action-api", groups=("客服",),
            nodes=(NODE, PLATFORM_WRITE_NODE),
        )

    def _audit_rows(self, action):
        return OperationLog.objects.filter(action=action)

    def _refresh(self):
        self.push.refresh_from_db()
        return self.push


class WithdrawDeleteBranchTests(_ActionTestBase):
    """撤回分支 ①：`Status=11` 未接手 ⇒ 删除（即时退款）→ 回读查不到。"""

    def setUp(self):
        super().setUp()
        self.fake.detail["Status"] = 11

    def test_unaccepted_order_is_deleted_and_marked_closed(self):
        # 第一次详情 = 未接手；删除后再查 ⇒ 上游回「查不到」。
        self.fake.next_details = [_deleted_detail()]
        with self._patched_client():
            push = actions_module.withdraw_push(
                self.order, operator=self.operator
            )

        self.assertEqual(self.fake.actions.count("LevelOrderDelSelf"), 1)
        self.assertEqual(self.fake.forms_for("LevelOrderDelSelf")[0]["ODSerialNo"], SERIAL)
        # 回读函数被调用：写前一次 + 写后一次。
        self.assertEqual(self.fake.detail_calls, 2)

        push = self._refresh()
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertFalse(push.is_active)
        self.assertEqual(push.serial_no, SERIAL)
        self.assertTrue(
            self._audit_rows("order.dailiantong_withdraw").exists(),
            "撤回必须留审计",
        )

    def test_delete_refused_by_upstream_is_a_chinese_error_with_original_text(self):
        self.fake.responses["LevelOrderDelSelf"] = {
            "Result": -1,
            "Err": "订单已被接手，不能删除",
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("订单已被接手，不能删除", str(ctx.exception))
        self.assertEqual(self.fake.actions.count("LevelOrderDelSelf"), 1)
        self.assertEqual(self._refresh().state, DailiantongPush.State.SENT)

    def test_delete_readback_failure_never_reports_success(self):
        # 删除成功，但回读详情超时 ⇒ 「已提交…没能核验到」，本地行不动。
        self.fake.next_details = [httpx.ReadTimeout("simulated readback timeout")]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        message = str(ctx.exception)
        self.assertIn("已提交删除", message)
        self.assertIn("没能核验到", message)
        self.assertEqual(self._refresh().state, DailiantongPush.State.SENT)

    def test_delete_but_detail_still_exists_is_not_success(self):
        self.fake.next_details = [
            {"Status": 11, "CancelStatus": 10, "GamePass": LEAKY_GAME_PASS}
        ]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("没能核验到删除", str(ctx.exception))
        self.assertEqual(self._refresh().state, DailiantongPush.State.SENT)

    def test_ambiguous_business_error_after_delete_is_not_success(self):
        """回读回的是**与「查不到」无关**的业务错误 ⇒ 不能当成删除成功（⛔ 假绿）。"""
        self.fake.next_details = [{"Result": -106, "Err": "处理数据异常"}]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        message = str(ctx.exception)
        self.assertIn("已提交删除", message)
        self.assertIn("没能确认", message)
        self.assertIn("处理数据异常", message)
        self.assertEqual(self._refresh().state, DailiantongPush.State.SENT)


    def test_delete_timeout_is_unknown_and_calls_upstream_only_once(self):
        """写动作超时 ⇒ 按「未知」处理，⛔ 不自动重试（真钱红线）。"""
        self.fake.timeout_actions = {"LevelOrderDelSelf"}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("结果未知", str(ctx.exception))
        self.assertIn("代练通后台核对", str(ctx.exception))
        self.assertEqual(self.fake.actions.count("LevelOrderDelSelf"), 1)
        self.assertEqual(self._refresh().state, DailiantongPush.State.SENT)


class WithdrawCancelBranchTests(_ActionTestBase):
    """撤回分支 ②：`Status=12/13/16` 已接手 ⇒ 协商撤销（`flag=0`）→ 回读 `CancelStatus`。"""

    def test_accepted_order_applies_cancel_with_detail_amounts_and_paypass(self):
        # 写前详情 = 正在代练；写后 CancelStatus=11（协商撤销中）。
        self.fake.next_details = [
            {
                "Status": 12,
                "CancelStatus": 11,
                "Price": "5.00",
                "TimeLimit": 24,
                "PayLevelBal": "2.00",
                "RepEnsureBal": "0.00",
            }
        ]
        with self._patched_client():
            actions_module.withdraw_push(
                self.order, operator=self.operator, comment="客户改主意了"
            )

        forms = self.fake.forms_for("LevelOrderCancel")
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form["ODSerialNo"], SERIAL)
        self.assertEqual(form["Flag"], "0")
        # 两个金额取详情原值（不是本地价 / 不是 0）。
        self.assertEqual(form["PayLevelBal"], "2.00")
        self.assertEqual(form["RepEnsureBal"], "0.00")
        self.assertEqual(form["Comment"], "客户改主意了")
        # 申请撤销要支付密码（值=派生串，这里只钉「带了」）。
        self.assertTrue(form.get("PayPass"))

        self.assertEqual(self.fake.detail_calls, 2, "写后必须回读")
        push = self._refresh()
        self.assertEqual(push.remote_cancel_status, 11)
        self.assertEqual(push.state, DailiantongPush.State.SENT)
        self.assertTrue(push.is_active, "协商中的单要继续轮询")

    def test_cancel_uses_default_comment_when_not_given(self):
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertEqual(
            self.fake.forms_for("LevelOrderCancel")[0]["Comment"], "已在本系统撤回"
        )

    def test_cancel_readback_still_not_cancelled_is_not_success(self):
        # 提交成功但回读仍是「未撤销」⇒ 不能报成功。
        self.fake.next_details = [{"Status": 12, "CancelStatus": 10}]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().remote_cancel_status, None)

    def test_cancel_refused_by_upstream_is_a_chinese_error_with_original_text(self):
        self.fake.responses["LevelOrderCancel"] = {
            "Result": -2,
            "Err": "支付密码错误.",
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("支付密码错误.", str(ctx.exception))

    # ── ⭐ 真机读数：详情两个金额为空 ⇒ 按 0 提交（原来上送空串 ⇒ 参数错误）──
    def test_empty_detail_amounts_are_sent_as_zero_and_written_in_the_audit(self):
        self.fake.detail = {
            "Status": 12,
            "CancelStatus": 10,
            "PayLevelBal": "",
            "RepEnsureBal": None,
        }
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            actions_module.withdraw_push(self.order, operator=self.operator)

        form = self.fake.forms_for("LevelOrderCancel")[0]
        self.assertEqual(Decimal(form["PayLevelBal"]), Decimal("0"))
        self.assertEqual(Decimal(form["RepEnsureBal"]), Decimal("0"))
        self.assertTrue(form["Comment"].strip(), "Comment 不能为空（上游必填）")
        reason = self._audit_rows("order.dailiantong_withdraw").get().reason
        self.assertIn("详情未给值", reason)
        self.assertIn("按 0 提交", reason)

    def test_unparsable_detail_amount_is_rejected_without_any_upstream_write(self):
        """⭐ I2：脏金额（不是空）⛔ 不许按 0 提交——那是替客服决定「0 元撤销」。"""
        self.fake.detail = {
            "Status": 12,
            "CancelStatus": 10,
            "PayLevelBal": "1,5",
            "RepEnsureBal": "1.5元",
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)

        message = str(ctx.exception)
        self.assertIn("金额无法识别", message)
        self.assertIn("1,5", message)
        self.assertIn("到代练通后台处理", message)
        self.assertEqual(self.fake.forms_for("LevelOrderCancel"), [], "脏金额 ⇒ 零写调用")

    def test_upstream_invalid_param_is_translated_to_state_not_allowed(self):
        """真机原话 `参数错误.` ⇒ 客服能看懂的中文 + 保留原文（不丢证据）。"""
        self.fake.responses["LevelOrderCancel"] = {
            "Result": -1,
            "Err": "参数错误.",
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        message = str(ctx.exception)
        self.assertIn("不允许撤回", message)
        self.assertIn("参数错误.", message)
        self.assertIn(
            "参数错误.",
            self._audit_rows("order.dailiantong_withdraw").get().reason,
            "失败原因要留痕（客服 / 事后追查都靠它）",
        )

    def test_already_negotiating_is_refused_with_agree_or_cancel_hint(self):
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        message = str(ctx.exception)
        self.assertIn("已在协商撤销中", message)
        self.assertIn("同意撤销", message)
        self.assertIn("取消撤销", message)
        self.assertEqual(
            self.fake.actions, ["LevelOrderDetail"], "⛔ 不重复申请撤销"
        )

    def test_abnormal_and_locked_orders_can_still_apply_for_cancel(self):
        """⭐ 2026-10-28：14 订单异常 / 15 帐号锁定 也走协商撤销（钱还在平台）。"""
        for status in (14, 15):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.fake.detail_calls = 0
                self.fake.detail = {
                    "Status": status,
                    "CancelStatus": 10,
                    "PayLevelBal": "1.00",
                    "RepEnsureBal": "0.00",
                }
                self.fake.next_details = [{"Status": status, "CancelStatus": 11}]
                with self._patched_client():
                    actions_module.withdraw_push(
                        self.order, operator=self.operator
                    )
                self.assertEqual(len(self.fake.forms_for("LevelOrderCancel")), 1)

    def test_cancelling_status_without_a_cancel_code_is_refused(self):
        """`Status=16`（撤销中）但 `CancelStatus=10` ⇒ 不是可重复申请的档，给准确中文。"""
        self.fake.detail = {"Status": 16, "CancelStatus": 10}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("撤销中", str(ctx.exception))
        self.assertIn("不接受新的撤销申请", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    # ── ⭐ 撤销时可选「自动申请仲裁」────────────────────────
    def test_a_successful_cancel_records_the_request_time_and_the_auto_flag(self):
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        before = timezone.now()
        with self._patched_client():
            actions_module.withdraw_push(
                self.order, operator=self.operator, auto_arbitrate=True
            )
        push = self._refresh()
        self.assertIsNotNone(push.cancel_requested_at)
        self.assertGreaterEqual(push.cancel_requested_at, before)
        self.assertTrue(push.auto_arbitrate, "客服主动勾选 ⇒ 这条推送 24h 后自动仲裁")

    def test_the_auto_flag_defaults_to_off(self):
        """⭐ 业务方口径：**不勾选就不代发**（默认 False）。

        业务原话：「若不勾选，代练通 24 小时自动客服介入」⇒ 默认不勾选时上游自己会转，
        我们再自动申请就是重复申请。
        """
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertFalse(
            self._refresh().auto_arbitrate, "业务默认：不自动申请仲裁（上游 24h 自己转）"
        )

    def test_the_api_defaults_the_flag_to_off_when_it_is_not_sent(self):
        """API 不传 `auto_arbitrate` ⇒ 落库 `False`（默认不勾选）。"""
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        client = self._api_client()
        with self._patched_client():
            response = client.post(
                URL_WITHDRAW,
                data=json.dumps({}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertFalse(self._refresh().auto_arbitrate)

    def test_the_api_passes_the_auto_arbitrate_choice(self):
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        client = self._api_client()
        with self._patched_client():
            response = client.post(
                URL_WITHDRAW,
                data=json.dumps({"auto_arbitrate": False}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0, response.json())
        push = self._refresh()
        self.assertFalse(push.auto_arbitrate)
        self.assertIsNotNone(push.cancel_requested_at)


class WithdrawBlockedByCancelInFlightTests(_ActionTestBase):
    """⭐ 口径：协商撤销已在流程里时，「撤回」**零写调用**并指出正确动作。

    `CancelStatus=11` 有两种发起方：
    - 我方发起（本地 `cancel_requested_at` 非空）⇒ 只能「取消撤销」（`Flag=1`，无金额无密码）；
    - 打手发起 ⇒ 只能「同意撤销」（h13 那条线）。
    两者都⛔ 不该再提交一次 `LevelOrderCancel(Flag=0)`（重复协商撤销 = 真钱链路）。
    """

    def test_our_cancel_in_flight_blocks_a_second_withdraw(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 11}
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            cancel_requested_at=timezone.now()
        )
        self.push.refresh_from_db()

        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)

        self.assertIn("取消撤销", str(ctx.exception))
        self.assertNotIn("LevelOrderCancel", self.fake.actions, "⛔ 不许重复提交协商撤销")
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_taker_initiated_cancel_in_flight_blocks_a_second_withdraw(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 11}
        self.assertIsNone(self.push.cancel_requested_at, "本地没有我方发起的记录")

        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)

        self.assertIn("同意撤销", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])
        self.assertNotIn("LevelOrderCancel", self.fake.actions)


class AutoArbitrationTests(_ActionTestBase):
    """⭐ 协商撤销满 24 小时 ⇒ 自动申请客服介入（`LevelOrderRequestArbitration`）。

    口径：
    - `cancel_requested_at + 1h` 之前**不调**（上游会拒，白打）；
    - `cancel_requested_at + 24h` 之前**不调**（先等打手处理）；
    - 满 24h ⇒ 调一次并记「已自动申请」；成功之后**不再调**；
    - 失败给 6 小时退避、最多 N 次；`auto_arbitrate=False` 一律不调。
    """

    def _set_cancel(
        self,
        *,
        age: timedelta,
        auto: bool = True,
        attempts: int = 0,
        next_at=None,
    ):
        now = timezone.now()
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_status=12,
            remote_cancel_status=11,
            cancel_requested_at=now - age,
            auto_arbitrate=auto,
            auto_arbitrate_attempts=attempts,
            auto_arbitrate_next_at=next_at,
            auto_arbitrated_at=None,
            auto_arbitrate_note="",
        )
        self.push.refresh_from_db()
        return self.push

    def _patched_push_client(self):
        from unittest import mock

        # ⚠️ 自动仲裁走的是 `dailiantong_push._open_client`（模块属性查找，
        # 与 `dailiantong_sync` 其它段同一个测试替换点）。
        return mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=self._client_factory,
        )

    def test_under_one_hour_does_not_call_upstream(self):
        push = self._set_cancel(age=timedelta(minutes=30))
        with self._patched_push_client():
            outcome = extra_module.auto_arbitrate_one(push)
        self.assertEqual(outcome.status, "skipped")
        self.assertNotIn("LevelOrderRequestArbitration", self.fake.actions)
        self.assertIsNone(push.auto_arbitrated_at)

    def test_between_one_and_twenty_four_hours_still_waits(self):
        push = self._set_cancel(age=timedelta(hours=2))
        with self._patched_push_client():
            outcome = extra_module.auto_arbitrate_one(push)
        self.assertEqual(outcome.status, "skipped")
        self.assertNotIn("LevelOrderRequestArbitration", self.fake.actions)
        self.assertNotIn("LevelOrderDetail", self.fake.actions, "闸没过就⛔ 不打上游")

    def test_after_twenty_four_hours_requests_once_only(self):
        push = self._set_cancel(age=timedelta(hours=25))
        self.fake.detail = {"Status": 12, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 12, "CancelStatus": 14}]
        with self._patched_push_client():
            outcome = extra_module.auto_arbitrate_one(push)
        self.assertEqual(outcome.status, "done", outcome.message)
        push.refresh_from_db()
        self.assertIsNotNone(push.auto_arbitrated_at)
        self.assertIn("自动申请客服介入", push.auto_arbitrate_note)
        self.assertIn("自动申请客服介入", outcome.message)
        self.assertEqual(self.fake.actions.count("LevelOrderRequestArbitration"), 1)
        self.assertEqual(
            self.fake.forms_for("LevelOrderRequestArbitration")[0]["ODSerialNo"], SERIAL
        )
        self.assertTrue(
            self._audit_rows("order.dailiantong_arbitration").exists(),
            "成功必须留一条审计",
        )
        # 第二轮（同一行）⇒ 不再调上游。
        calls_before = len(self.fake.calls)
        with self._patched_push_client():
            again = extra_module.auto_arbitrate_one(push)
        self.assertEqual(again.status, "skipped")
        self.assertEqual(len(self.fake.calls), calls_before)

    def test_a_failure_backs_off_six_hours_and_keeps_the_upstream_text(self):
        push = self._set_cancel(age=timedelta(hours=25))
        self.fake.detail = {"Status": 12, "CancelStatus": 11}
        self.fake.responses["LevelOrderRequestArbitration"] = {
            "Result": -1,
            "Err": "操作过于频繁",
        }
        now = timezone.now()
        with self._patched_push_client():
            outcome = extra_module.auto_arbitrate_one(push, now=now)
        self.assertEqual(outcome.status, "failed", outcome.message)
        push.refresh_from_db()
        self.assertEqual(push.auto_arbitrate_attempts, 1)
        self.assertIsNone(push.auto_arbitrated_at)
        self.assertIsNotNone(push.auto_arbitrate_next_at)
        self.assertGreaterEqual(
            push.auto_arbitrate_next_at, now + extra_module.AUTO_ARBITRATE_RETRY
        )
        self.assertIn("操作过于频繁", push.auto_arbitrate_note)
        self.assertIn("操作过于频繁", push.last_error)
        # 退避窗口内再跑 ⇒ 不打上游。
        calls_before = len(self.fake.calls)
        with self._patched_push_client():
            again = extra_module.auto_arbitrate_one(push)
        self.assertEqual(again.status, "skipped")
        self.assertEqual(len(self.fake.calls), calls_before)

    def test_after_the_attempt_cap_it_stops_with_a_readable_reason(self):
        push = self._set_cancel(
            age=timedelta(hours=25),
            attempts=extra_module.AUTO_ARBITRATE_MAX_ATTEMPTS - 1,
        )
        self.fake.detail = {"Status": 12, "CancelStatus": 11}
        self.fake.responses["LevelOrderRequestArbitration"] = {
            "Result": -1,
            "Err": "操作过于频繁",
        }
        with self._patched_push_client():
            outcome = extra_module.auto_arbitrate_one(push)
        self.assertEqual(outcome.status, "failed", outcome.message)
        push.refresh_from_db()
        self.assertEqual(push.auto_arbitrate_attempts, extra_module.AUTO_ARBITRATE_MAX_ATTEMPTS)
        self.assertIsNone(push.auto_arbitrate_next_at, "到上限 ⇒ 不再排下一次")
        self.assertIn("停止自动重试", push.auto_arbitrate_note)
        # 已达上限 ⇒ 之后一行都不打上游。
        self.fake.calls.clear()
        with self._patched_push_client():
            again = extra_module.auto_arbitrate_one(push)
        self.assertEqual(again.status, "skipped")
        self.assertEqual(self.fake.calls, [])

    def test_auto_arbitrate_false_never_calls_upstream(self):
        push = self._set_cancel(age=timedelta(hours=25), auto=False)
        with self._patched_push_client():
            outcome = extra_module.auto_arbitrate_one(push)
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(self.fake.calls, [])

    def test_the_scan_only_picks_rows_that_are_due(self):
        now = timezone.now()

        def _extra(index: int, age: timedelta):
            order = Order.objects.create(
                game=self.game,
                server=self.server,
                order_no=f"20261027000000{index:04d}",
                status=OrderStatus.IN_PROGRESS,
                project_text="星耀上王者",
                total_price=Decimal("120.00"),
            )
            return DailiantongPush.objects.create(
                order=order,
                platform=10,
                state=DailiantongPush.State.SENT,
                serial_no=f"1900000000000000{index:04d}",
                price=Decimal("5.00"),
                ensure1=Decimal("0.00"),
                ensure2=Decimal("0.00"),
                time_limit_hours=24,
                ex_order_no=order.order_no,
                attempts=1,
                remote_status=12,
                remote_cancel_status=11,
                cancel_requested_at=now - age,
                # ⚠️ 开关**默认关**（改准）⇒ 本用例只筛「时间到没到」，
                # 必须显式勾选，否则一行都不会是候选（开关那一档另有用例）。
                auto_arbitrate=True,
            )

        too_young = _extra(1, timedelta(minutes=30))
        waiting = _extra(2, timedelta(hours=2))
        due = _extra(3, timedelta(hours=25))
        self.fake.detail = {"Status": 12, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 12, "CancelStatus": 14}]
        with self._patched_push_client():
            done, failed = extra_module.auto_arbitrate_pending_pushes(now=now)
        self.assertEqual((done, failed), (1, 0))
        due.refresh_from_db()
        too_young.refresh_from_db()
        waiting.refresh_from_db()
        self.assertIsNotNone(due.auto_arbitrated_at)
        self.assertIsNone(too_young.auto_arbitrated_at)
        self.assertIsNone(waiting.auto_arbitrated_at)
        self.assertEqual(self.fake.actions.count("LevelOrderRequestArbitration"), 1)


class WithdrawGateTests(_ActionTestBase):
    """撤回的前置闸：终态 / 不支持的状态 / 没有推送行 ⇒ 零上游写调用。"""

    def test_settled_or_forced_cancel_order_cannot_be_withdrawn(self):
        for detail in ({"Status": 17}, {"Status": 12, "CancelStatus": 16}):
            with self.subTest(detail=detail):
                self.fake.calls.clear()
                self.fake.detail_calls = 0
                self.fake.detail = dict(detail)
                with self._patched_client():
                    with self.assertRaises(DailiantongPushError) as ctx:
                        actions_module.withdraw_push(
                            self.order, operator=self.operator
                        )
                self.assertIn("不能再撤回", str(ctx.exception))
                self.assertEqual(
                    [a for a in self.fake.actions if a != "LevelOrderDetail"],
                    [],
                    "终态单不该有任何上游写调用",
                )

    def test_unsupported_remote_status_is_a_chinese_error(self):
        """`10` 临时订单仍在本地下挡：既不是「未接手可删」，也不是「已接手可撤销」。"""
        self.fake.detail = {"Status": 10, "CancelStatus": 10}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("临时订单", str(ctx.exception))
        self.assertIn("Status=10", str(ctx.exception))
        self.assertIn("本系统不代发撤回", str(ctx.exception))
        self.assertIn("请以代练通后台为准", str(ctx.exception))
        self.assertNotIn("不支持撤回", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_abnormal_and_locked_statuses_are_forwarded_to_upstream(self):
        """⭐ 14 订单异常 / 15 帐号锁定**不再本地挡死**，交上游裁决。

        这两档要接单方在代练通客户端「提交异常」才会出现（我们造不出样本）⇒
        本地没有资格断言「不支持撤回」；放行后上游允许就是业务方要的能撤，
        上游拒绝就把原文透出去（下一条用例钉文案）。
        """
        for status in (14, 15):
            with self.subTest(status=status):
                self.fake.calls.clear()
                self.fake.detail_calls = 0
                self.fake.detail = {
                    "Status": status,
                    "CancelStatus": 10,
                    "PayLevelBal": "1.00",
                    "RepEnsureBal": "0.00",
                }
                self.fake.next_details = [
                    {
                        "Status": 16,
                        "CancelStatus": 11,
                        "PayLevelBal": "1.00",
                        "RepEnsureBal": "0.00",
                    }
                ]
                with self._patched_client():
                    actions_module.withdraw_push(
                        self.order, operator=self.operator, comment="异常单也撤"
                    )

                forms = self.fake.forms_for("LevelOrderCancel")
                self.assertEqual(len(forms), 1, "必须真的发一次协商撤销")
                self.assertEqual(forms[0]["ODSerialNo"], SERIAL)
                self.assertEqual(forms[0]["Flag"], "0")
                self.assertEqual(forms[0]["PayLevelBal"], "1.00")
                self.assertEqual(forms[0]["RepEnsureBal"], "0.00")
                self.assertEqual(forms[0]["Comment"], "异常单也撤")
                self.assertTrue(forms[0].get("PayPass"))

                push = self._refresh()
                self.assertEqual(push.remote_cancel_status, 11)
                self.assertEqual(push.state, DailiantongPush.State.SENT)

    def test_abnormal_status_upstream_refusal_keeps_its_exact_words(self):
        """上游拒 14/15 时，文案必须是「代练通回复：<原文>」，⛔ 不许只说「不支持撤回」。

        ⚠️ 这里的上游回包是**合成值**（取厂商文档 `LevelOrderCancel` 的 `-1` 文案），
        用来钉「原文透传」这条实现口径；⛔ 不是 Status=14 的真机实测（真机没样本）。
        """
        self.fake.detail = {
            "Status": 14,
            "CancelStatus": 10,
            "PayLevelBal": "1.00",
            "RepEnsureBal": "0.00",
        }
        synth_err = "订单异常，无法修改."
        self.fake.responses["LevelOrderCancel"] = {"Result": -1, "Err": synth_err}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)

        message = str(ctx.exception)
        self.assertIn(f"代练通回复：{synth_err}", message)
        self.assertNotIn("不支持撤回", message)
        self.assertEqual(self.fake.actions.count("LevelOrderCancel"), 1)
        self.assertIsNone(self._refresh().remote_cancel_status)

    def test_order_without_any_push_row_is_a_chinese_error(self):
        self.push.delete()
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("还没有抛到代练通", str(ctx.exception))
        self.assertEqual(self.fake.calls, [], "没有推送行时连详情都不该查")

    def test_push_row_without_serial_is_a_chinese_error(self):
        DailiantongPush.objects.filter(pk=self.push.pk).update(serial_no=None)
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.withdraw_push(self.order, operator=self.operator)
        self.assertIn("还没有上游订单号", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])


class PriceActionTests(_ActionTestBase):
    """改价：只有未接手可改；回读 `Price` 必须等于新价。"""

    def setUp(self):
        super().setUp()
        self.fake.detail["Status"] = 11

    def test_unaccepted_order_price_is_changed_and_read_back(self):
        self.fake.next_details = [
            {"Status": 11, "Price": "8.00", "TimeLimit": 24}
        ]
        with self._patched_client():
            push = actions_module.modify_push_price(
                self.order, operator=self.operator, price=Decimal("8.00")
            )

        forms = self.fake.forms_for("LevelOrderModify_Price")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["ODSerialNo"], SERIAL)
        self.assertEqual(Decimal(forms[0]["Price"]), Decimal("8.00"))
        self.assertTrue(forms[0].get("PayPass"), "改价要支付密码")
        self.assertEqual(self.fake.detail_calls, 2, "写完必须回读 Price")

        push = self._refresh()
        self.assertEqual(push.price, Decimal("8.00"))
        self.assertTrue(self._audit_rows("order.dailiantong_price").exists())

    def test_accepted_order_cannot_be_repriced_and_nothing_is_written(self):
        self.fake.detail["Status"] = 12
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price=Decimal("8.00")
                )
        self.assertIn("只有未接手的代练通订单才能改价", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_upstream_refusal_keeps_the_original_error_text(self):
        self.fake.detail["Status"] = 11
        self.fake.responses["LevelOrderModify_Price"] = {
            "Result": -1,
            "Err": UPSTREAM_ERR_TEXT,
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price=Decimal("8.00")
                )
        self.assertIn(UPSTREAM_ERR_TEXT, str(ctx.exception))
        self.assertEqual(self._refresh().price, Decimal("5.00"))

    def test_price_readback_mismatch_is_not_success(self):
        self.fake.next_details = [{"Status": 11, "Price": "7.00"}]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price=Decimal("8.00")
                )
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().price, Decimal("5.00"))

    def test_price_readback_failure_is_not_success(self):
        self.fake.next_details = [httpx.ReadTimeout("simulated readback timeout")]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price=Decimal("8.00")
                )
        self.assertIn("已提交改价", str(ctx.exception))
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().price, Decimal("5.00"))

    def test_price_write_timeout_is_unknown_and_calls_upstream_only_once(self):
        self.fake.timeout_actions = {"LevelOrderModify_Price"}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price=Decimal("8.00")
                )
        self.assertIn("改价结果未知", str(ctx.exception))
        self.assertEqual(self.fake.actions.count("LevelOrderModify_Price"), 1)
        self.assertEqual(self._refresh().price, Decimal("5.00"))


class TimeActionTests(_ActionTestBase):
    """补时：增量；`modify_special(kind=1)` **不带支付密码**；回读 `TimeLimit`。"""

    def test_time_is_added_without_paypass_and_read_back(self):
        self.fake.next_details = [{"Status": 12, "TimeLimit": 26}]
        with self._patched_client():
            actions_module.adjust_push_time(
                self.order, operator=self.operator, hours=2
            )

        forms = self.fake.forms_for("LevelOrderModify_Special")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["ODSerialNo"], SERIAL)
        self.assertEqual(forms[0]["Kind"], "1")
        self.assertEqual(forms[0]["Val"], "2")
        self.assertNotIn(
            "PayPass", forms[0], "补时不要支付密码（只有补款 kind=0 才要）"
        )
        self.assertEqual(self.fake.detail_calls, 2, "写完必须回读 TimeLimit")

        push = self._refresh()
        self.assertEqual(push.time_limit_hours, 26)
        self.assertTrue(self._audit_rows("order.dailiantong_time").exists())

    def test_hours_outside_the_allowed_range_is_rejected_locally(self):
        with self._patched_client():
            for hours in (0, 721):
                with self.subTest(hours=hours):
                    with self.assertRaises(DailiantongPushError):
                        actions_module.adjust_push_time(
                            self.order, operator=self.operator, hours=hours
                        )
        self.assertEqual(self.fake.calls, [])

    def test_upstream_refusal_keeps_the_original_error_text(self):
        self.fake.responses["LevelOrderModify_Special"] = {
            "Result": -1,
            "Err": "订单异常，无法修改",
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.adjust_push_time(
                    self.order, operator=self.operator, hours=2
                )
        self.assertIn("订单异常，无法修改", str(ctx.exception))
        self.assertEqual(self._refresh().time_limit_hours, 24)

    def test_time_readback_mismatch_is_not_success(self):
        self.fake.next_details = [{"Status": 12, "TimeLimit": 24}]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.adjust_push_time(
                    self.order, operator=self.operator, hours=2
                )
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().time_limit_hours, 24)

    def test_time_readback_failure_is_not_success(self):
        self.fake.next_details = [httpx.ReadTimeout("simulated readback timeout")]
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.adjust_push_time(
                    self.order, operator=self.operator, hours=2
                )
        self.assertIn("已提交补时", str(ctx.exception))
        self.assertIn("没能核验到", str(ctx.exception))

    def test_time_write_timeout_is_unknown_and_calls_upstream_only_once(self):
        self.fake.timeout_actions = {"LevelOrderModify_Special"}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.adjust_push_time(
                    self.order, operator=self.operator, hours=2
                )
        self.assertIn("补时结果未知", str(ctx.exception))
        self.assertEqual(self.fake.actions.count("LevelOrderModify_Special"), 1)
        self.assertEqual(self._refresh().time_limit_hours, 24)


class MessageActionTests(_ActionTestBase):
    """留言：成功码 + 审计；有延迟 ⇒ ⛔ 不强求回读。"""

    def test_message_is_sent_and_audited(self):
        with self._patched_client():
            actions_module.add_push_message(
                self.order, operator=self.operator, msg="账号已私聊发你"
            )

        forms = self.fake.forms_for("LevelOrderMessageAdd")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["ODSerialNo"], SERIAL)
        self.assertEqual(forms[0]["Msg"], "账号已私聊发你")
        # 写前必须回读一次详情判终态（P2-8）；留言本身有延迟 ⇒ 写后不回读。
        self.assertEqual(self.fake.detail_calls, 1)
        rows = self._audit_rows("order.dailiantong_message")
        self.assertEqual(rows.count(), 1)
        self.assertIn("留言", rows.first().summary)

    def test_blank_message_is_rejected_locally(self):
        with self._patched_client():
            for msg in ("", "   "):
                with self.subTest(msg=msg):
                    with self.assertRaises(DailiantongPushError):
                        actions_module.add_push_message(
                            self.order, operator=self.operator, msg=msg
                        )
        self.assertEqual(self.fake.calls, [])

    def test_too_long_message_is_rejected_locally(self):
        with self._patched_client():
            with self.assertRaises(DailiantongPushError):
                actions_module.add_push_message(
                    self.order, operator=self.operator, msg="刷" * 513
                )
        self.assertEqual(self.fake.calls, [])

    def test_upstream_refusal_keeps_the_original_error_text(self):
        self.fake.responses["LevelOrderMessageAdd"] = {
            "Result": -1,
            "Err": "订单不存在",
        }
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.add_push_message(
                    self.order, operator=self.operator, msg="你好"
                )
        self.assertIn("订单不存在", str(ctx.exception))

    def test_message_write_timeout_is_unknown_and_calls_upstream_only_once(self):
        self.fake.timeout_actions = {"LevelOrderMessageAdd"}
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.add_push_message(
                    self.order, operator=self.operator, msg="你好"
                )
        self.assertIn("结果未知", str(ctx.exception))
        self.assertEqual(self.fake.actions.count("LevelOrderMessageAdd"), 1)


class TerminalGateTests(_ActionTestBase):
    """⭐ 补时 / 留言写前统一判终态（已结算 / 撤销已完成 ⇒ 零写调用）。

    ⭐ h1：终态从「只认 `CancelStatus=16`」放宽到 `12/15/16`
    （协商已处理 / 客服已处理 / 客服强制撤销 都是撤销完成）。
    """

    def _assert_terminal_gate(self, action, *, status=12, cancel_status=10, **kwargs):
        # 一次只设一个终态：改坏一半（只认 Status / 只认 CancelStatus）也必须红。
        self.fake.detail["Status"] = status
        self.fake.detail["CancelStatus"] = cancel_status

        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                action(self.order, operator=self.operator, **kwargs)

        self.assertIn("已结束", str(ctx.exception))
        self.assertNotIn("LevelOrderModify_Special", self.fake.actions)
        self.assertNotIn("LevelOrderMessageAdd", self.fake.actions)

    def test_time_is_not_sent_to_a_settled_order(self):
        self._assert_terminal_gate(
            actions_module.adjust_push_time, status=17, hours=2
        )

    def test_time_is_not_sent_to_a_forced_cancel_order(self):
        self._assert_terminal_gate(
            actions_module.adjust_push_time, cancel_status=16, hours=2
        )

    def test_message_is_not_sent_to_a_settled_order(self):
        self._assert_terminal_gate(
            actions_module.add_push_message, status=17, msg="在吗"
        )

    def test_message_is_not_sent_to_a_forced_cancel_order(self):
        self._assert_terminal_gate(
            actions_module.add_push_message, cancel_status=16, msg="在吗"
        )

    def test_price_change_is_still_blocked_by_its_own_status_gate(self):
        self.fake.detail["Status"] = 17
        with self._patched_client():
            with self.assertRaises(DailiantongPushError):
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price="8.00"
                )
        self.assertNotIn("LevelOrderModify_Price", self.fake.actions)


class PriceSafetyTests(_ActionTestBase):
    """⭐ P2-9：改价的下限 + 余额闸（复用发单那一套，提示带差额）。"""

    def test_price_below_two_yuan_is_rejected_locally(self):
        """⭐ 2026-10-28 复核：全局下限仍是 2 元（球球大作战 / 无限暖暖 2 元可发；
        通用区要 5 元是**区服粒度**的门槛，⛔ 不要全局抬到 5）。"""
        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price="1.99"
                )

        self.assertIn("低于 2.00 元", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_insufficient_balance_reports_the_gap_and_never_writes(self):
        self.fake.balance = "3.00"
        self.fake.detail["Status"] = 11

        with self._patched_client():
            with self.assertRaises(DailiantongPushError) as ctx:
                actions_module.modify_push_price(
                    self.order, operator=self.operator, price="10.00"
                )

        message = str(ctx.exception)
        self.assertIn("还差 7.00 元", message)
        self.assertNotIn("LevelOrderModify_Price", self.fake.actions)


class PlatformActionApiTests(_ActionTestBase):
    """接口层：正常信封 / 上游失败也回 200（⛔ 不 500）/ 入参坏形状 / 订单不可见 / 403。"""

    def setUp(self):
        super().setUp()
        self.client = self._api_client()

    def test_withdraw_api_returns_the_updated_push_row(self):
        self.fake.detail["Status"] = 11
        self.fake.next_details = [_deleted_detail()]
        with self._patched_client():
            response = self.client.post(URL_WITHDRAW, {})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["state"], DailiantongPush.State.CLOSED)
        self.assertEqual(response.data["data"]["serial_no"], SERIAL)
        self.assertIn("删除并即时退款", response.data["msg"])

    def test_withdraw_api_says_cancel_is_only_submitted_for_an_accepted_order(self):
        self.fake.detail["Status"] = 12
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            response = self.client.post(URL_WITHDRAW, {})

        self.assertEqual(response.data["code"], 0, response.data)
        self.assertIn("已提交协商撤销", response.data["msg"])
        self.assertIn("等待", response.data["msg"])
        self.assertNotIn("删除", response.data["msg"])

    def test_price_api_returns_the_new_price(self):
        self.fake.detail["Status"] = 11
        self.fake.next_details = [{"Status": 11, "Price": "8.00"}]
        with self._patched_client():
            response = self.client.post(URL_PRICE, {"price": "8.00"})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(Decimal(response.data["data"]["price"]), Decimal("8.00"))

    def test_time_api_returns_the_new_time_limit(self):
        self.fake.next_details = [{"Status": 12, "TimeLimit": 30}]
        with self._patched_client():
            response = self.client.post(URL_TIME, {"hours": 6})
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["time_limit_hours"], 30)

    def test_message_api_returns_success_envelope(self):
        with self._patched_client():
            response = self.client.post(URL_MESSAGE, {"msg": "在吗"})
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["serial_no"], SERIAL)

    def test_upstream_business_failure_is_200_with_a_chinese_business_code(self):
        self.fake.detail["Status"] = 11
        self.fake.responses["LevelOrderModify_Price"] = {
            "Result": -1,
            "Err": UPSTREAM_ERR_TEXT,
        }
        with self._patched_client():
            response = self.client.post(URL_PRICE, {"price": "8.00"})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 1002, response.data)
        self.assertIn(UPSTREAM_ERR_TEXT, response.data["msg"])

    def test_bad_input_shapes_are_1000_and_never_touch_upstream(self):
        cases = (
            (URL_PRICE, {}),
            (URL_PRICE, {"price": "abc"}),
            (URL_TIME, {"hours": 0}),
            (URL_TIME, {"hours": 721}),
            (URL_MESSAGE, {}),
            (URL_MESSAGE, {"msg": ""}),
            (URL_MESSAGE, {"msg": "刷" * 513}),
        )
        with self._patched_client():
            for url, body in cases:
                with self.subTest(url=url, body=body):
                    response = self.client.post(url, body)
                    # 本仓 DRF 异常处理器把序列化器错误包成 HTTP 200 + code=1000。
                    self.assertEqual(response.status_code, 200, response.content[:400])
                    self.assertEqual(response.data["code"], 1000, response.data)
        self.assertEqual(self.fake.calls, [], "形状不对不该碰上游")

    def test_unknown_order_is_a_business_error(self):
        response = self.client.post(
            "/api/business/orders/20261027000000009999/platform-push/price/",
            {"price": "8.00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertEqual(response.data["msg"], "订单不存在或不可见")
        self.assertEqual(self.fake.calls, [])

    def test_all_four_write_actions_require_the_platform_write_node(self):
        """⭐ 四个写动作要 `order.platform_write`。

        两向都钉：① 零节点 ⇒ 403；② **只持 `order.list`**（能看订单）⇒ 也 403
        ——修复前这批端点只挂 `order.list`，「能看订单」就能对平台单做真钱写动作。
        """
        from unittest import mock

        cases = (
            (URL_WITHDRAW, {}),
            (URL_PRICE, {"price": "8.00"}),
            (URL_TIME, {"hours": 1}),
            (URL_MESSAGE, {"msg": "x"}),
        )
        # ⚠️ 要挂节点就得**有一个自己的 Group**（判据读 Group→Role→permissions）；
        # 用独立组名，别蹭 `客服`（那个 Role 在别的用例夹具里已被挂了两个节点）。
        for label, groups, nodes in (
            ("no-node", (), ()),
            ("list-only", ("rdlst",), (NODE,)),
        ):
            client = make_logged_in_client(
                username=f"dlt-action-denied-{label}", groups=groups, nodes=nodes
            )
            with mock.patch(
                "apps.business.dailiantong_actions._open_client",
                side_effect=self._client_factory,
            ) as patched:
                for url, body in cases:
                    with self.subTest(nodes=label, url=url):
                        response = client.post(url, body)
                        self.assertEqual(response.status_code, 403, response.content[:400])
                self.assertEqual(patched.call_count, 0, "403 请求不该构造上游客户端")
        self.assertEqual(self.fake.calls, [], "无权限的请求不该打到上游")


class CredentialRedLineTests(_ActionTestBase):
    """凭据 / 明文的红线：响应与审计里不许出现 secret、支付密码 md5、上游 `GamePass`。"""

    def setUp(self):
        super().setUp()
        self.client = self._api_client()

    def test_no_credentials_or_leaky_fields_leak_into_any_platform_action(self):
        responses = []
        self.fake.detail["Status"] = 11
        self.fake.next_details = [_deleted_detail()]
        with self._patched_client():
            responses.append(self.client.post(URL_WITHDRAW, {}))
        self.fake.detail["Status"] = 11
        self.fake.next_details = [{"Status": 11, "Price": "8.00"}]
        with self._patched_client():
            responses.append(self.client.post(URL_PRICE, {"price": "8.00"}))
        self.fake.next_details = [{"Status": 11, "TimeLimit": 26}]
        with self._patched_client():
            responses.append(self.client.post(URL_TIME, {"hours": 2}))
        with self._patched_client():
            responses.append(self.client.post(URL_MESSAGE, {"msg": "在吗"}))

        blob = json.dumps([r.data for r in responses], default=str)
        blob += json.dumps(
            list(
                OperationLog.objects.values(
                    "action", "summary", "reason", "from_value", "to_value", "actor"
                )
            ),
            default=str,
        )
        for secret in (APP_SECRET, PAY_PASSWORD_MD5, LEAKY_GAME_PASS):
            self.assertNotIn(secret, blob)


class CredentialRedLineStructuralTests(SimpleTestCase):
    """结构判据：服务模块里**代码**不读取支付密码字段 / 不出现派生串。"""

    def test_action_module_never_reads_the_pay_password_columns(self):
        import ast
        import inspect

        source = inspect.getsource(actions_module)
        tree = ast.parse(source)
        identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.Name):
                identifiers.add(node.id)
        # 只允许通过抛单模块的构造点拿客户端；⛔ 这里不该出现凭据列名。
        # ⚠️ 判据打在 **AST 标识符**上，不是整段源码文本：模块 docstring 里写这些
        #    字段名是为了解释红线（字符串常量不算「读取」）。
        for forbidden in ("pay_password_md5", "app_secret", "pay_pass"):
            self.assertNotIn(forbidden, identifiers)


# ── 第三方平台订单管理页的 11 颗工具栏按钮 ──────────────────
#
# 一个端点 `POST /api/business/platform-pushes/actions/` 按 `action` 分派。
# 判据口径：
# - **逐笔回原因**（部分成功要如实报），所以断言打在 `data.items` 上；
# - 平台动作仍按「写前回读 + 写后回读」判成败（假上游见 `FakeDailiantong`）；
# - 本地动作（改状态 / 退款 / 改价 / 标签 / 标记 / 加急）断言**数据库终态**与
#   **审计行**，⛔ 不拿「接口没报错」当成功；
# - 权限逐动作另判（⛔ 不因为类级 `order.list` 就把退款 / 改价 / 改状态放行）。
ACTION_URL = "/api/business/platform-pushes/actions/"

#: 一类测试客户端要的节点（页面可见 + 11 个动作里需要额外判的那几个）。
ALL_ACTION_NODES = (
    NODE,                       # order.list（类级门）
    PLATFORM_WRITE_NODE,        # 批量撤回 / 确认完单
    "order.change_status",      # 改系统状态（非「已完成」目标）
    "order.change_status_completed",  # 目标 = COMPLETED
    "order.submit_payout_price",      # 改接单价
    "order.add_edit",                 # 标签 / 标记 / 加急
    "order.online_refund",            # 记退款
)


class _BatchActionFixture(_ActionTestBase):
    """在既有夹具上加：一个持全套节点的客户端、一个不受推送行牵连的订单。"""

    def setUp(self):
        super().setUp()
        self.client_all = make_logged_in_client(
            username="c1-batch-all",
            groups=("客服",),
            nodes=ALL_ACTION_NODES,
        )

    def _post(self, payload, client=None):
        return (client or self.client_all).post(
            ACTION_URL,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _body(self, response):
        self.assertEqual(response.status_code, 200, response.content[:400])
        return json.loads(response.content)

    def _new_order(self, order_no, *, status=OrderStatus.COMPLETED, push=True,
                   remote_status=None, serial="19000000000000000099"):
        order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=order_no,
            status=status,
            project_text="星耀上王者",
            total_price=Decimal("50.00"),
        )
        if push:
            DailiantongPush.objects.create(
                order=order,
                platform=10,
                state=DailiantongPush.State.SENT,
                serial_no=serial,
                price=Decimal("5.00"),
                ensure1=Decimal("0.00"),
                ensure2=Decimal("0.00"),
                time_limit_hours=24,
                ex_order_no=order_no,
                remote_status=remote_status,
                attempts=1,
            )
        return order


class BatchWithdrawActionTests(_BatchActionFixture):
    """批量撤回：逐笔走既有 `withdraw_push`，部分成功如实回显。"""

    def test_order_list_permission_does_not_allow_batch_withdraw_or_accept(self):
        list_only = make_logged_in_client(
            username="c1-batch-list-only", groups=("仅列表",), nodes=(NODE,)
        )
        for action, status in (("withdraw", 11), ("accept", 13)):
            with self.subTest(action=action), self._patched_client():
                self.fake.detail["Status"] = status
                response = self._post({
                    "action": action,
                    "order_nos": [ORDER_NO],
                }, client=list_only)
                self.assertEqual(response.status_code, 403, response.content[:400])
                self.assertEqual(self.fake.actions, [])

    def test_unaccepted_order_is_deleted_and_reported_per_item(self):
        self.fake.detail["Status"] = 11
        self.fake.next_details = [_deleted_detail()]
        with self._patched_client():
            body = self._body(self._post({
                "action": "withdraw",
                "order_nos": [ORDER_NO],
            }))
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.assertEqual(body["data"]["fail_count"], 0, body)
        self.assertIn("删除并即时退款", body["data"]["items"][0]["msg"])
        self.assertEqual(self._refresh().state, DailiantongPush.State.CLOSED)
        self.assertTrue(self._audit_rows("order.dailiantong_withdraw").exists())

    def test_partial_success_reports_the_first_failure(self):
        """两笔里只有一笔有推送行 ⇒ 一笔成功、一笔失败，失败原因必须回显。"""
        order_b = self._new_order(
            "20261027000000000002", push=False
        )
        self.fake.detail["Status"] = 12
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            body = self._body(self._post({
                "action": "withdraw",
                "order_nos": [ORDER_NO, order_b.order_no],
            }))
        data = body["data"]
        self.assertEqual((data["ok_count"], data["fail_count"]), (1, 1), data)
        failed = [item for item in data["items"] if not item["ok"]][0]
        self.assertEqual(failed["order_no"], order_b.order_no)
        self.assertIn("还没有抛到代练通", failed["msg"])
        self.assertIn("失败 1 笔", body["msg"])


class BatchAcceptActionTests(_BatchActionFixture):
    """批量验收：只有远端 `Status=13` 能确认完单，成败看回读 `Status=17`。"""

    def test_waiting_acceptance_is_confirmed_and_read_back(self):
        self.fake.detail["Status"] = 13
        self.fake.next_details = [{"Status": 17, "CancelStatus": 10}]
        with self._patched_client():
            body = self._body(self._post({
                "action": "accept",
                "order_nos": [ORDER_NO],
            }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.assertIn("已确认完单", body["data"]["items"][0]["msg"])
        self.assertEqual(self.fake.actions.count("LevelOrderOver"), 1)
        self.assertEqual(self.fake.forms_for("LevelOrderOver")[0]["ODSerialNo"], SERIAL)
        push = self._refresh()
        self.assertEqual(push.remote_status, 17)
        self.assertEqual(push.state, DailiantongPush.State.CLOSED)
        self.assertFalse(push.is_active)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.COMPLETED)
        self.assertTrue(self._audit_rows("order.dailiantong_accept").exists())

    def test_other_remote_status_is_refused_without_calling_upstream_write(self):
        self.fake.detail["Status"] = 12
        with self._patched_client():
            body = self._body(self._post({
                "action": "accept",
                "order_nos": [ORDER_NO],
            }))
        data = body["data"]
        self.assertEqual(data["fail_count"], 1, data)
        self.assertIn("等待验收", data["items"][0]["msg"])
        self.assertNotIn("LevelOrderOver", self.fake.actions)

    def test_readback_not_settled_is_not_success(self):
        self.fake.detail["Status"] = 13
        self.fake.next_details = [{"Status": 13, "CancelStatus": 10}]
        with self._patched_client():
            body = self._body(self._post({
                "action": "accept",
                "order_nos": [ORDER_NO],
            }))
        failed = body["data"]["items"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("没能核验到", failed["msg"])
        self.assertEqual(self._refresh().remote_status, None)


class BatchSyncActionTests(_BatchActionFixture):
    """同步交易状态：复用 `refresh_push_status`，写回远端状态并留审计。"""

    def test_sync_writes_remote_status_and_audits(self):
        self.fake.detail = {"Status": 13, "CancelStatus": 10}
        with self._patched_client():
            body = self._body(self._post({
                "action": "sync",
                "order_nos": [ORDER_NO],
            }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.assertIn("等待验收", body["data"]["items"][0]["msg"])
        push = self._refresh()
        self.assertEqual(push.remote_status, 13)
        self.assertIsNotNone(push.last_synced_at)
        self.assertTrue(self._audit_rows("order.dailiantong_sync").exists())

    def test_order_without_push_row_reports_chinese_reason(self):
        order = self._new_order("20261027000000000003", push=False)
        with self._patched_client():
            body = self._body(self._post({
                "action": "sync",
                "order_nos": [order.order_no],
            }))
        failed = body["data"]["items"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("还没有抛到代练通", failed["msg"])
        self.assertEqual(self.fake.calls, [], "没有推送行不该打上游")


class BatchArbitrateActionTests(_BatchActionFixture):
    """批量仲裁：D 线未就绪 ⇒ 本地只记「待仲裁」，**零上游调用**。"""

    def test_local_note_says_it_is_not_submitted_to_platform(self):
        with self._patched_client():
            body = self._body(self._post({
                "action": "arbitrate",
                "order_nos": [ORDER_NO],
                "comment": "打手一直不上号",
            }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.assertIn("尚未提交代练通", body["data"]["items"][0]["msg"])
        self.assertEqual(self.fake.calls, [], "仲裁兜底不该碰上游")
        row = OperationLog.objects.get(action="order.note", order_no=ORDER_NO)
        self.assertIn("待仲裁", row.reason)
        self.assertIn("尚未提交代练通", row.reason)
        self.assertIn("打手一直不上号", row.reason)


class BatchChangeStatusTests(_BatchActionFixture):
    """改系统状态：已抛单且远端未结束 ⇒ 拒绝；远端已终态 / 没抛过 ⇒ 允许。"""

    def test_pushed_order_with_live_remote_is_refused(self):
        self.push.remote_status = 12
        self.push.save(update_fields=["remote_status"])
        body = self._body(self._post({
            "action": "change_status",
            "order_nos": [ORDER_NO],
            "target": OrderStatus.IN_PROGRESS,
        }))
        failed = body["data"]["items"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("不能手工改系统状态", failed["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_pushed_order_with_unknown_remote_is_refused(self):
        """还没回读到远端状态（`remote_status is None`）⇒ 拿不出「已结束」的证据 ⇒ 拒。"""
        body = self._body(self._post({
            "action": "change_status",
            "order_nos": [ORDER_NO],
            "target": OrderStatus.IN_PROGRESS,
        }))
        failed = body["data"]["items"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("不能手工改系统状态", failed["msg"])

    def test_a_running_push_still_refuses_a_manual_status_change(self):
        """A2 闸的判据 = **`state=sent` 且 `is_active=True`**。

        只要这条推送还在跑（哪怕远端已经读到终态、本地还来得及 finalize），本地状态就不许
        手工改——状态必须由同步回写。
        """
        self.push.remote_status = 17
        self.push.is_active = True
        self.push.save(update_fields=["remote_status", "is_active"])
        body = self._body(self._post({
            "action": "change_status",
            "order_nos": [ORDER_NO],
            "target": OrderStatus.IN_PROGRESS,
        }))
        self.assertEqual(body["data"]["ok_count"], 0, body)
        self.assertIn("不能手工修改", body["data"]["items"][0]["msg"])
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertFalse(
            self._audit_rows("order.transition").exists(), "被拒的请求不许留下状态流转"
        )

    def test_a_finalized_terminal_push_no_longer_blocks_manual_changes(self):
        """⭐ 另一向：**已收口**（`is_active=False`）的终态行不再锁本地状态。

        前端 `hasActivePlatformPush = is_active` 早就是这个判据（弹窗能打开）；后端原先只看
        `state=sent` ⇒ 前端放行、提交被 1002 拒（前后端劈叉）。`refresh_push_status` 在终态
        把行 finalize 成 `closed`，两把尺子一致。
        """
        self.push.remote_status = 17
        self.push.is_active = False
        self.push.state = DailiantongPush.State.CLOSED
        self.push.save(update_fields=["remote_status", "is_active", "state"])
        body = self._body(self._post({
            "action": "change_status",
            "order_nos": [ORDER_NO],
            "target": OrderStatus.IN_PROGRESS,
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_unpushed_order_can_be_changed(self):
        order = self._new_order(
            "20261027000000000004", status=OrderStatus.WAITING_GRAB, push=False
        )
        body = self._body(self._post({
            "action": "change_status",
            "order_nos": [order.order_no],
            "target": OrderStatus.IN_PROGRESS,
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_target_completed_needs_its_own_node(self):
        restricted = make_logged_in_client(
            username="c1-batch-no-complete",
            # ⚠️ **独立组**：`_grant_nodes` 把节点挂到「该组对应的 Role」上，而组是**租户内共享**的
            #    ⇒ 复用「客服」组会让受限账号继承 `client_all` 挂上去的全部节点（403 永远测不出来）。
            groups=("受限",),
            nodes=(NODE, "order.change_status"),
        )
        response = self._post({
            "action": "change_status",
            "order_nos": [ORDER_NO],
            "target": OrderStatus.COMPLETED,
        }, client=restricted)
        self.assertEqual(response.status_code, 403, response.content[:400])


class BatchRefundActionTests(_BatchActionFixture):
    """记退款：复用既有退款核心（累计总额语义），节点门与单笔端点同源。"""

    def test_completed_order_sets_the_cumulative_refund_total(self):
        order = self._new_order(
            "20261027000000000005", status=OrderStatus.COMPLETED, push=False
        )
        body = self._body(self._post({
            "action": "refund",
            "order_nos": [order.order_no],
            "refund_total": "20.00",
            "reason": "退差价",
            "request_id": "c1-refund-key-1",
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal("20.00"))
        self.assertTrue(self._audit_rows("order.refund").exists())

    def test_refund_needs_its_own_node(self):
        restricted = make_logged_in_client(
            username="c1-batch-no-refund",
            # ⚠️ **独立组**：`_grant_nodes` 把节点挂到「该组对应的 Role」上，而组是**租户内共享**的
            #    ⇒ 复用「客服」组会让受限账号继承 `client_all` 挂上去的全部节点（403 永远测不出来）。
            groups=("受限",),
            nodes=(NODE,),
        )
        response = self._post({
            "action": "refund",
            "order_nos": [ORDER_NO],
            "refund_total": "20.00",
            "reason": "退差价",
            "request_id": "c1-refund-key-2",
        }, client=restricted)
        self.assertEqual(response.status_code, 403, response.content[:400])

    def test_non_completed_order_is_refused_with_the_domain_message(self):
        body = self._body(self._post({
            "action": "refund",
            "order_nos": [ORDER_NO],
            "refund_total": "20.00",
            "reason": "退差价",
            "request_id": "c1-refund-key-3",
        }))
        failed = body["data"]["items"][0]
        self.assertFalse(failed["ok"])
        self.assertIn("只有「已完成」", failed["msg"])


class BatchTakerPriceTests(_BatchActionFixture):
    """改接单价：写 `Order.total_price`（既有 `change_price` 核心），节点门与单笔同源。"""

    def test_price_is_changed_and_audited(self):
        body = self._body(self._post({
            "action": "change_taker_price",
            "order_nos": [ORDER_NO],
            "taker_price": "88.00",
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("88.00"))
        self.assertTrue(
            self._audit_rows("order.change_taker_price").exists()
        )

    def test_price_needs_its_own_node(self):
        restricted = make_logged_in_client(
            username="c1-batch-no-price",
            # ⚠️ **独立组**：`_grant_nodes` 把节点挂到「该组对应的 Role」上，而组是**租户内共享**的
            #    ⇒ 复用「客服」组会让受限账号继承 `client_all` 挂上去的全部节点（403 永远测不出来）。
            groups=("受限",),
            nodes=(NODE,),
        )
        response = self._post({
            "action": "change_taker_price",
            "order_nos": [ORDER_NO],
            "taker_price": "88.00",
        }, client=restricted)
        self.assertEqual(response.status_code, 403, response.content[:400])


class BatchLocalFieldsTests(_BatchActionFixture):
    """标签 / 标记 / 删标记 / 加急：写本地列 + 逐字段留痕（与 PATCH 同一个核心）。"""

    def test_label_tag_and_urgent_are_written(self):
        body = self._body(self._post({
            "action": "set_label",
            "order_nos": [ORDER_NO],
            "label": "微",
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_label, "微")

        body = self._body(self._post({
            "action": "set_tag",
            "order_nos": [ORDER_NO],
            "tag": "客户催了",
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.tag, "客户催了")

        body = self._body(self._post({
            "action": "urgent",
            "order_nos": [ORDER_NO],
            "urgent": True,
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertIs(self.order.is_urgent, True)

        body = self._body(self._post({
            "action": "clear_tag",
            "order_nos": [ORDER_NO],
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.tag, "")
        # 四次写入 = 四条 `order.update` 留痕（逐字段一行）。
        fields = set(
            self._audit_rows("order.update").values_list("field_name", flat=True)
        )
        self.assertEqual(fields, {"order_label", "tag", "is_urgent"})

    def test_label_can_be_cleared_with_an_empty_value(self):
        self.order.order_label = "微"
        self.order.save(update_fields=["order_label"])
        body = self._body(self._post({
            "action": "set_label",
            "order_nos": [ORDER_NO],
            "label": "",
        }))
        self.assertEqual(body["data"]["ok_count"], 1, body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_label, "")

    def test_local_fields_need_the_edit_node(self):
        restricted = make_logged_in_client(
            username="c1-batch-no-edit",
            # ⚠️ **独立组**：`_grant_nodes` 把节点挂到「该组对应的 Role」上，而组是**租户内共享**的
            #    ⇒ 复用「客服」组会让受限账号继承 `client_all` 挂上去的全部节点（403 永远测不出来）。
            groups=("受限",),
            nodes=(NODE,),
        )
        response = self._post({
            "action": "urgent",
            "order_nos": [ORDER_NO],
            "urgent": True,
        }, client=restricted)
        self.assertEqual(response.status_code, 403, response.content[:400])


class BatchActionShapeTests(_BatchActionFixture):
    """请求形状坏 ⇒ 1000、零业务写入；未知单号逐笔回原因（⛔ 不整批 1000）。"""

    def test_bad_shapes_are_1000_and_never_touch_upstream(self):
        """坏形状 ⇒ `code=1000` + **中文原因**（信封只带第一句，`data` 恒空）。

        ⚠️ 判据是「那句中文出现在 `msg` 里」，⛔ 不是「某个 JSON 键存在」：
        本仓的 `envelope_exception_handler` 把 DRF 的字段错误收敛成**一句可读中文**，
        `data` 是空的（信封形状只此一处）。断言键名会变成对实现细节的断言。
        """
        cases = (
            ({}, "请指定要执行的动作"),
            ({"action": "no_such_action", "order_nos": [ORDER_NO]}, "不支持的操作"),
            ({"action": "withdraw", "order_nos": []}, "请至少选择一笔订单"),
            ({"action": "withdraw"}, "请至少选择一笔订单"),
            ({"action": "change_status", "order_nos": [ORDER_NO]}, "请选择要改成的系统状态"),
            (
                {"action": "change_status", "order_nos": [ORDER_NO], "target": "NOPE"},
                "目标系统状态不合法",
            ),
            ({"action": "refund", "order_nos": [ORDER_NO]}, "请填写累计退款总额"),
            (
                {"action": "refund", "order_nos": [ORDER_NO], "refund_total": "5.00"},
                "请填写退款原因",
            ),
            ({"action": "set_tag", "order_nos": [ORDER_NO]}, "请填写标记内容"),
            ({"action": "urgent", "order_nos": [ORDER_NO]}, "请指定加急标记"),
            (
                {"action": "change_taker_price", "order_nos": [ORDER_NO]},
                "请填写新的接单价",
            ),
        )
        with self._patched_client():
            for payload, expected in cases:
                with self.subTest(payload=payload):
                    body = self._body(self._post(payload))
                    self.assertEqual(body["code"], 1000, body)
                    self.assertIn(expected, body["msg"], body)
        self.assertEqual(self.fake.calls, [], "形状不对不该碰上游")

    def test_unknown_order_is_reported_per_item_not_as_whole_batch(self):
        body = self._body(self._post({
            "action": "withdraw",
            "order_nos": ["20261027000000009999", ORDER_NO],
        }))
        data = body["data"]
        self.assertEqual(
            [item["ok"] for item in data["items"]], [False, False]
        )
        self.assertIn("记录不存在", data["items"][0]["msg"])

    def test_page_node_is_required(self):
        restricted = make_logged_in_client(
            username="c1-batch-no-view",
            # ⚠️ **独立组**：`_grant_nodes` 把节点挂到「该组对应的 Role」上，而组是**租户内共享**的
            #    ⇒ 复用「客服」组会让受限账号继承 `client_all` 挂上去的全部节点（403 永远测不出来）。
            groups=("受限",),
            nodes=(),
        )
        response = self._post({"action": "sync", "order_nos": [ORDER_NO]}, client=restricted)
        self.assertEqual(response.status_code, 403, response.content[:400])

    def test_credentials_never_leak_into_the_batch_response(self):
        self.fake.detail["Status"] = 13
        self.fake.next_details = [{"Status": 17, "CancelStatus": 10}]
        with self._patched_client():
            response = self._post({"action": "accept", "order_nos": [ORDER_NO]})
        blob = response.content.decode("utf-8", "replace")
        for secret in (APP_SECRET, PAY_PASSWORD_MD5, LEAKY_GAME_PASS):
            self.assertNotIn(secret, blob)


class ManualStatusGateTests(_BatchActionFixture):
    """`manual_status_denial` 的三种取值（直测服务层，⛔ 不只靠 HTTP 那条腿）。"""

    def test_no_push_row_allows_manual_change(self):
        order = self._new_order("20261027000000000006", push=False)
        self.assertIsNone(actions_module.manual_status_denial(order))

    def test_terminal_remote_allows_manual_change(self):
        self.push.remote_status = 17
        self.push.save(update_fields=["remote_status"])
        self.assertIsNone(actions_module.manual_status_denial(self.order))

    def test_forced_cancel_allows_manual_change(self):
        self.push.remote_cancel_status = 16
        self.push.save(update_fields=["remote_cancel_status"])
        self.assertIsNone(actions_module.manual_status_denial(self.order))

    def test_live_remote_is_refused_with_the_current_status_in_the_message(self):
        self.push.remote_status = 13
        self.push.save(update_fields=["remote_status"])
        denial = actions_module.manual_status_denial(self.order)
        self.assertIsNotNone(denial)
        self.assertIn("等待验收", denial)
        self.assertIn("不能手工改系统状态", denial)

    def test_closed_push_row_does_not_block_manual_change(self):
        self.push.state = DailiantongPush.State.CLOSED
        self.push.save(update_fields=["state"])
        self.assertIsNone(actions_module.manual_status_denial(self.order))
# ── 代练通「有、我们没用上」的动作（dlt-d）─────────────────────
#
# 判据口径与上面四个动作完全一致：**写动作成败看回读**、**前置闸 ⇒ 零写调用**、
# 上游失败带原文、回读不到 / 不符 ⛔ 不报成功。唯一的两条例外（评价 / 拉黑）是
# **上游确实没有读面**，用例里显式钉「只认成功码」，⛔ 不假装回读。


class _ExtraActionTestBase(_ActionTestBase):
    """把客户端桩打在**新模块**的查找点上（AGENTS.md §5-E / E-0029）。"""

    def _patched_extra(self):
        from unittest import mock

        return mock.patch(
            "apps.business.dailiantong_extra_actions._open_client",
            side_effect=self._client_factory,
        )

    def _set_accept_uid(self, uid: str) -> None:
        DailiantongPush.objects.filter(pk=self.push.pk).update(accept_uid=uid)
        self.push.refresh_from_db()


class UpPriceActionTests(_ExtraActionTestBase):
    """未接手加价：只有 `Status=11`；回读 `Price` == 原价 + 加价额。"""

    def test_up_price_is_applied_and_read_back(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 11, "Price": "7.50"}]
        with self._patched_extra():
            push = extra_module.up_price_push(
                self.order, operator=self.operator, up_price=Decimal("2.50")
            )

        forms = self.fake.forms_for("LevelOrderUpPrice")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["ODSerialNo"], SERIAL)
        self.assertEqual(Decimal(forms[0]["UpPrice"]), Decimal("2.50"))
        self.assertTrue(forms[0].get("PayPass"), "加价要支付密码")
        self.assertEqual(self.fake.detail_calls, 2, "写完必须回读 Price")

        self.assertEqual(push.price, Decimal("7.50"))
        self.assertTrue(
            self._audit_rows("order.dailiantong_up_price").exists(), "加价必须留审计"
        )

    def test_accepted_order_cannot_be_up_priced_and_nothing_is_written(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "Price": "5.00"}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.up_price_push(
                    self.order, operator=self.operator, up_price="2.50"
                )
        self.assertIn("只有未接手的代练通订单才能加价", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_up_price_readback_mismatch_is_not_success(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 11, "Price": "6.00"}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.up_price_push(
                    self.order, operator=self.operator, up_price="2.50"
                )
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().price, Decimal("5.00"))

    def test_up_price_write_timeout_is_unknown_and_calls_upstream_once(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        self.fake.timeout_actions = {"LevelOrderUpPrice"}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.up_price_push(
                    self.order, operator=self.operator, up_price="2.50"
                )
        self.assertIn("加价结果未知", str(ctx.exception))
        self.assertEqual(self.fake.actions.count("LevelOrderUpPrice"), 1)


class ArbitrationActionTests(_ExtraActionTestBase):
    """申请客服介入：**协商撤销中（`CancelStatus=11`）且转入满 1 小时**（真机定案）。

    判据四档：未满 1 小时 ⇒ 拒（文案含剩余分钟数）；满 1 小时 ⇒ 放行（假上游成功）；
    非 11 ⇒ 拒（带上游原话）；本地没有 `cancel_requested_at`（打手侧发起）⇒ 放行给上游判。
    回读证据仍是 `CancelStatus ∈ {14,15}` 或 `Status=14`（核验不到 ⛔ 不报成功）。
    """

    def _requested_since(self, delta):
        """把 `cancel_requested_at` 钉到 `now - delta`（真实时钟 + 区间，⛔ 不冻时间）。"""
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            cancel_requested_at=timezone.now() - delta
        )
        self.push.refresh_from_db()

    def test_arbitration_is_requested_and_read_back(self):
        self._requested_since(timedelta(hours=2))
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 16, "CancelStatus": 14}]
        with self._patched_extra():
            push = extra_module.request_push_arbitration(
                self.order, operator=self.operator
            )

        self.assertEqual(len(self.fake.forms_for("LevelOrderRequestArbitration")), 1)
        self.assertEqual(self.fake.detail_calls, 2)
        self.assertEqual(push.remote_cancel_status, 14)
        self.assertTrue(
            self._audit_rows("order.dailiantong_arbitration").exists()
        )

    def test_before_one_hour_is_rejected_with_the_remaining_minutes(self):
        """未满 1 小时 ⇒ 中文错误 + **还需 X 分钟**，且一个写调用都不发。"""
        self._requested_since(timedelta(minutes=30))
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )

        message = str(ctx.exception)
        self.assertIn("不满 1 小时", message)
        self.assertIn("还需", message)
        self.assertIn("分钟", message)
        self.assertEqual(self.fake.forms_for("LevelOrderRequestArbitration"), [])
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_after_one_hour_is_forwarded(self):
        """满 1 小时（边界之上）⇒ 真的发给上游（这里给假上游成功 + 回读证据）。"""
        self._requested_since(timedelta(hours=1, minutes=1))
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 16, "CancelStatus": 14}]
        with self._patched_extra():
            extra_module.request_push_arbitration(
                self.order, operator=self.operator
            )
        self.assertEqual(len(self.fake.forms_for("LevelOrderRequestArbitration")), 1)

    def test_a_non_negotiating_status_is_rejected_with_the_upstream_wording(self):
        self.fake.detail = {"Status": 13, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )
        message = str(ctx.exception)
        self.assertIn("当前状态不能申请客服介入", message)
        self.assertIn("订单异常，无法申请仲裁介入", message)
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_without_a_local_timestamp_it_is_left_to_upstream(self):
        """`cancel_requested_at` 空（打手侧发起 / 历史行）⇒ 放行给上游判。"""
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 16, "CancelStatus": 14}]
        with self._patched_extra():
            extra_module.request_push_arbitration(
                self.order, operator=self.operator
            )
        self.assertEqual(len(self.fake.forms_for("LevelOrderRequestArbitration")), 1)

    def test_without_a_local_timestamp_the_upstream_refusal_is_kept_verbatim(self):
        """本地没时刻 ⇒ 交给上游；上游拒了就把原文带回来（⛔ 不本地永久禁用）。"""
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        self.fake.responses["LevelOrderRequestArbitration"] = {
            "Result": -3,
            "Err": "转入协商撤销状态1小时后才能申请仲裁介入.",
        }
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )
        self.assertIn("转入协商撤销状态1小时后才能申请仲裁介入.", str(ctx.exception))

    def test_unaccepted_order_cannot_request_arbitration(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )
        self.assertIn("不能申请客服介入", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_already_in_arbitration_is_rejected_without_another_call(self):
        self.fake.detail = {"Status": 16, "CancelStatus": 14}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )
        self.assertIn("已经在客服介入流程里", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_arbitration_without_readback_evidence_is_not_success(self):
        self._requested_since(timedelta(hours=2))
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 16, "CancelStatus": 11}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )
        self.assertIn("没能核验到", str(ctx.exception))

    def test_a_detail_without_cancel_status_is_not_guessed(self):
        """详情缺撤销状态 ⇒ 中文错误（⛔ 不猜「大概可以申请」）。"""
        self.fake.detail = {"Status": 16}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.request_push_arbitration(
                    self.order, operator=self.operator
                )
        self.assertIn("没有撤销状态", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])



class AgreeCancelTests(_ExtraActionTestBase):
    """⭐ 同意撤销（`LevelOrderCancel(Flag=2)`）。

    判据与其它平台动作同一口径：**写动作成败看回读**、前置闸零写调用、上游失败带原文、
    回读不到 / 不符 ⛔ 不报成功；金额取详情现值，详情为空**按 0 传且写清**。
    """

    URL = f"/api/business/orders/{ORDER_NO}/platform-push/agree-cancel/"

    def setUp(self):
        super().setUp()
        # 前置：推送行 = 协商撤销中（打手发起）。
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_cancel_status=11, cancel_initiator="taker"
        )
        self.push.refresh_from_db()
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "PayLevelBal": "1.50",
            "RepEnsureBal": "0.00",
            "CreateUID": "USR-SYNTH-CREATOR-01",
            "AcceptUID": "USR-SYNTH-TAKER-01",
            "CancelUID": "USR-SYNTH-TAKER-01",
        }
        self.fake.next_details = [
            {
                "Status": 16,
                "CancelStatus": 12,
                "CreateUID": "USR-SYNTH-CREATOR-01",
                "AcceptUID": "USR-SYNTH-TAKER-01",
                "CancelUID": "USR-SYNTH-TAKER-01",
            }
        ]

    def test_agree_cancel_calls_flag_two_with_detail_amounts_and_reads_back(self):
        with self._patched_extra():
            push = extra_module.agree_cancel(self.order, operator=self.operator)

        forms = self.fake.forms_for("LevelOrderCancel")
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form["ODSerialNo"], SERIAL)
        self.assertEqual(form["Flag"], "2", "同意撤销必须是 Flag=2")
        self.assertEqual(Decimal(form["PayLevelBal"]), Decimal("1.50"))
        self.assertEqual(Decimal(form["RepEnsureBal"]), Decimal("0.00"))
        self.assertEqual(form["Comment"], "同意撤销")
        self.assertTrue(form.get("PayPass"), "Flag=2 实测也要支付密码")
        self.assertEqual(self.fake.detail_calls, 2, "写前 + 写后各回读一次")

        push = self._refresh()
        self.assertEqual(push.remote_cancel_status, 12)
        self.assertEqual(push.cancel_initiator, "taker")
        self.assertFalse(push.is_active, "撤销完成 ⇒ 不再轮询")
        # 这张单没有经历过「被接手」的占位人 ⇒ 本地落「已退单」（没有东西要清）。
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        self.assertTrue(
            self._audit_rows("order.dailiantong_agree_cancel").exists(),
            "同意撤销必须留审计",
        )

    def test_agree_cancel_releases_the_placeholder_taker_assigned_before(self):
        """⭐ 2026-10-28：同意撤销后「全部推送结束」⇒ 只清占位接手人，状态保持已退单。"""
        from apps.business.models import EmpType, Performance

        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_status=13, remote_cancel_status=10
        )
        placeholder = dailiantong_push_module.placeholder_taker_user()
        self.order.status = OrderStatus.PENDING_REVIEW
        self.order.save(update_fields=["status"])
        Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            user=placeholder,
            temp_amount=Decimal("5.00"),
        )
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_status=16, remote_cancel_status=11, cancel_initiator="taker"
        )
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "PayLevelBal": "1.50",
            "RepEnsureBal": "0.00",
            "CreateUID": "USR-SYNTH-CREATOR-01",
            "AcceptUID": "USR-SYNTH-TAKER-01",
            "CancelUID": "USR-SYNTH-TAKER-01",
        }
        self.fake.next_details = [
            {
                "Status": 16,
                "CancelStatus": 12,
                "CreateUID": "USR-SYNTH-CREATOR-01",
                "AcceptUID": "USR-SYNTH-TAKER-01",
                "CancelUID": "USR-SYNTH-TAKER-01",
            }
        ]
        with self._patched_extra():
            extra_module.agree_cancel(self.order, operator=self.operator)

        self.order.refresh_from_db()
        # ⭐ I1：只清人，状态保持「已退单」（⛔ 不再改写成「未分配」）。
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        perf = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)
        self.assertTrue(
            OperationLog.objects.filter(
                action="order.dailiantong_clear_taker",
                order_no=self.order.order_no,
            ).exists()
        )

    def test_empty_amounts_are_sent_as_zero_and_written_clearly(self):
        self.fake.detail["PayLevelBal"] = ""
        self.fake.detail["RepEnsureBal"] = None
        with self._patched_extra():
            extra_module.agree_cancel(self.order, operator=self.operator)

        form = self.fake.forms_for("LevelOrderCancel")[0]
        self.assertEqual(Decimal(form["PayLevelBal"]), Decimal("0"))
        self.assertEqual(Decimal(form["RepEnsureBal"]), Decimal("0"))
        reason = self._audit_rows("order.dailiantong_agree_cancel").get().reason
        self.assertIn("为空", reason)
        self.assertIn("按 0", reason)

    def test_a_push_row_not_negotiating_is_rejected_with_zero_upstream_calls(self):
        DailiantongPush.objects.filter(pk=self.push.pk).update(remote_cancel_status=10)
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.agree_cancel(self.order, operator=self.operator)
        self.assertIn("协商撤销中", str(ctx.exception))
        self.assertEqual(self.fake.actions, [], "前置闸不许打上游（连详情都不查）")

    def test_detail_saying_not_negotiating_is_rejected_before_the_write(self):
        self.fake.detail["CancelStatus"] = 12
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.agree_cancel(self.order, operator=self.operator)
        self.assertIn("不能同意撤销", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"], "只读了详情，没写")

    def test_upstream_business_failure_keeps_the_original_text(self):
        self.fake.responses["LevelOrderCancel"] = {"Result": -1, "Err": "撤销状态已变更"}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.agree_cancel(self.order, operator=self.operator)
        self.assertIn("撤销状态已变更", str(ctx.exception))
        self.assertTrue(
            self._audit_rows("order.dailiantong_agree_cancel")
            .filter(reason__contains="撤销状态已变更")
            .exists(),
            "失败也要留痕",
        )

    def test_write_without_readback_evidence_is_not_success(self):
        self.fake.next_details = [{"Status": 16, "CancelStatus": 11}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.agree_cancel(self.order, operator=self.operator)
        self.assertIn("没能核验到", str(ctx.exception))
        push = self._refresh()
        self.assertEqual(push.remote_cancel_status, 11, "没核验到就不许改本地行")

    def test_the_api_route_returns_the_updated_push_row(self):
        client = self._api_client()
        with self._patched_extra():
            response = client.post(self.URL, {})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        row = response.data["data"]
        self.assertEqual(row["remote_cancel_status"], 12)
        self.assertEqual(row["cancel_initiator"], "taker")
        self.assertIs(row["agree_cancel_available"], False, "已处理完就不再可点")

    def test_the_push_detail_row_carries_initiator_and_agree_availability(self):
        client = self._api_client()
        url = f"/api/business/orders/{ORDER_NO}/platform-push/"
        with self._patched_client():
            response = client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:400])
        row = response.data["data"][0]
        self.assertEqual(row["cancel_initiator"], "taker")
        self.assertIs(row["agree_cancel_available"], True)

        # 我方发起 ⇒ 不是打手那一侧的按钮；判不了（空）⇒ 也不给。
        for initiator, expected in (("us", False), ("", False)):
            DailiantongPush.objects.filter(pk=self.push.pk).update(
                cancel_initiator=initiator
            )
            with self._patched_client():
                response = client.get(url)
            row = response.data["data"][0]
            self.assertIs(row["agree_cancel_available"], expected, initiator)

        # 撤销已不在流程里 ⇒ 即使发起方仍是 taker 也不可用（CancelUID 不会被清理）。
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_cancel_status=10, cancel_initiator="taker"
        )
        with self._patched_client():
            response = client.get(url)
        row = response.data["data"][0]
        self.assertIs(row["agree_cancel_available"], False)

    def test_settled_amounts_are_only_exposed_for_status_16_and_17(self):
        """⭐ 2026-10-28：这两格只在撤销中(16) / 已结算(17) 下发；其它状态给 null。"""
        client = self._api_client()
        url = f"/api/business/orders/{ORDER_NO}/platform-push/"
        cases = (
            (17, "1.50", "0.00", "1.50", "0.00"),
            (16, "2.00", "0.50", "2.00", "0.50"),
            (11, "1.50", "0.00", None, None),
            (12, "1.50", "0.00", None, None),
            (13, "1.50", "0.00", None, None),
        )
        for status, pay, rep, want_pay, want_rep in cases:
            with self.subTest(status=status):
                DailiantongPush.objects.filter(pk=self.push.pk).update(
                    remote_status=status,
                    remote_pay_level_bal=Decimal(pay),
                    remote_rep_ensure_bal=Decimal(rep),
                )
                with self._patched_client():
                    response = client.get(url)
                row = response.data["data"][0]
                self.assertEqual(row["settled_pay_level_bal"], want_pay)
                self.assertEqual(row["settled_rep_ensure_bal"], want_rep)

        # 上游字段为空 / 解析不出 ⇒ null（即使状态是 17）。
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_status=17,
            remote_pay_level_bal=None,
            remote_rep_ensure_bal=None,
        )
        with self._patched_client():
            response = client.get(url)
        row = response.data["data"][0]
        self.assertIsNone(row["settled_pay_level_bal"])
        self.assertIsNone(row["settled_rep_ensure_bal"])

    def test_the_push_detail_row_carries_the_raw_cancel_requirement(self):
        """⭐ 2026-10-28 真机：`CancelCmt` 原样下发；`CancelReason` 单独一格。"""
        requirement = (
            '接单者"新手USR-SYNTH-806325"申请撤销,需发单者支出:0.00, '
            "接单者支出:0.00. 请尽快协商解决.撤销原因：好的"
        )
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_status=16,
            remote_cancel_status=11,
            remote_cancel_cmt=requirement,
            remote_cancel_reason="好的",
        )
        client = self._api_client()
        url = f"/api/business/orders/{ORDER_NO}/platform-push/"
        with self._patched_client():
            response = client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:400])
        row = response.data["data"][0]
        self.assertEqual(row["cancel_cmt"], requirement, "原文照发，⛔ 不改写")
        self.assertEqual(row["cancel_reason"], "好的")


class CancelWithdrawRequestTests(_ExtraActionTestBase):
    """⭐ 2026-10-28：取消**我方发起**的协商撤销（`Flag=1`，无金额/无密码）。"""

    URL = f"/api/business/orders/{ORDER_NO}/platform-push/cancel-request/"

    def setUp(self):
        super().setUp()
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_status=16,
            remote_cancel_status=11,
            cancel_initiator="us",
            cancel_requested_at=timezone.now(),
            auto_arbitrate=True,
        )
        self.push.refresh_from_db()
        # 取消撤销**没有写前回读**（Flag=1 不需要金额）⇒ 唯一一次详情就是写后回读。
        self.fake.detail = {"Status": 16, "CancelStatus": 10}

    def test_cancel_request_sends_flag_one_without_money_or_password(self):
        with self._patched_extra():
            push = extra_module.cancel_withdraw_request(
                self.order, operator=self.operator
            )

        forms = self.fake.forms_for("LevelOrderCancel")
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form["ODSerialNo"], SERIAL)
        self.assertEqual(form["Flag"], "1", "取消撤销必须是 Flag=1")
        self.assertNotIn("PayPass", form, "Flag=1 实测不需要支付密码")
        self.assertNotIn("PayLevelBal", form)
        self.assertNotIn("RepEnsureBal", form)
        self.assertEqual(self.fake.detail_calls, 1, "只有写后回读（写前不再多读一次）")

        push = self._refresh()
        self.assertEqual(push.remote_cancel_status, 10)
        self.assertIsNone(push.cancel_requested_at, "成功必须清掉自动仲裁计时起点")
        self.assertFalse(push.auto_arbitrate)
        self.assertTrue(
            self._audit_rows("order.dailiantong_withdraw")
            .filter(reason__contains="取消撤销申请成功")
            .exists()
        )

    def test_taker_initiated_cancel_is_refused_with_agree_hint(self):
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            cancel_initiator="taker", cancel_requested_at=None
        )
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.cancel_withdraw_request(
                    self.order, operator=self.operator
                )
        message = str(ctx.exception)
        self.assertIn("打手发起", message)
        self.assertIn("同意撤销", message)
        self.assertIn("客服介入", message)
        self.assertEqual(self.fake.actions, [], "打手发起 ⇒ 零上游调用")

    def test_not_negotiating_is_refused_with_zero_upstream_calls(self):
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_cancel_status=10
        )
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.cancel_withdraw_request(
                    self.order, operator=self.operator
                )
        self.assertIn("协商撤销中", str(ctx.exception))
        self.assertEqual(self.fake.actions, [])

    def test_upstream_refusal_keeps_the_original_text(self):
        self.fake.responses["LevelOrderCancel"] = {
            "Result": -1,
            "Err": "撤销状态已变化",
        }
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.cancel_withdraw_request(
                    self.order, operator=self.operator
                )
        self.assertIn("撤销状态已变化", str(ctx.exception))

    def test_readback_still_negotiating_is_not_success(self):
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.cancel_withdraw_request(
                    self.order, operator=self.operator
                )
        self.assertIn("没能核验到", str(ctx.exception))
        push = self._refresh()
        self.assertEqual(push.remote_cancel_status, 11)
        self.assertIsNotNone(
            push.cancel_requested_at, "没核验到就不许清掉自动仲裁计时起点"
        )

    def test_the_api_route_returns_the_updated_push_row(self):
        client = self._api_client()
        with self._patched_extra():
            response = client.post(self.URL, {})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["remote_cancel_status"], 10)


class CancelInitiatorTests(_ActionTestBase):
    """⭐ h13：撤销发起方的判定（真机口径见 `docs/DAILIANTONG-API.md` §6.13）。"""

    def test_matching_cancel_uid_to_accept_uid_means_taker(self):
        detail = {
            "CreateUID": "USR-SYNTH-CREATOR-01",
            "AcceptUID": "USR-SYNTH-TAKER-01",
            "CancelUID": "USR-SYNTH-TAKER-01",
        }
        self.assertEqual(
            dailiantong_push_module.cancel_initiator_from_detail(detail), "taker"
        )

    def test_matching_cancel_uid_to_create_uid_means_us(self):
        detail = {
            "CreateUID": "USR-SYNTH-CREATOR-01",
            "AcceptUID": "USR-SYNTH-TAKER-01",
            "CancelUID": "USR-SYNTH-CREATOR-01",
        }
        self.assertEqual(
            dailiantong_push_module.cancel_initiator_from_detail(detail), "us"
        )

    def test_empty_or_unknown_uid_is_not_guessed(self):
        for cancel_uid in ("", "USR-SYNTH-SOMEONE-ELSE"):
            detail = {
                "CreateUID": "USR-SYNTH-CREATOR-01",
                "AcceptUID": "USR-SYNTH-TAKER-01",
                "CancelUID": cancel_uid,
            }
            self.assertEqual(
                dailiantong_push_module.cancel_initiator_from_detail(detail), "",
                cancel_uid,
            )

    def test_store_remote_fields_persists_the_initiator(self):
        push = self.push
        dailiantong_push_module.store_remote_fields(
            push,
            {
                "CreateUID": "USR-SYNTH-CREATOR-01",
                "AcceptUID": "USR-SYNTH-TAKER-01",
                "CancelUID": "USR-SYNTH-TAKER-01",
            },
            status=16,
            cancel_status=11,
        )
        self.assertEqual(push.cancel_initiator, "taker")


class NoRejectNegotiationTests(SimpleTestCase):
    """⭐ 上游**没有**「拒绝撤销」这一档 ⇒ 本仓不提供拒绝按钮的数据。

    这两条断言把「不做」变成可执行判据：① 客户端 Flag 常量恰好是文档的 `{0,1,2}`；
    ② 平台动作路由里没有 `reject` / `refuse` / `deny` 这类路径。
    """

    def test_the_upstream_cancel_flags_are_exactly_apply_cancel_agree(self):
        from apps.business.dailiantong_client import (
            CANCEL_FLAG_AGREE,
            CANCEL_FLAG_APPLY,
            CANCEL_FLAG_CANCEL,
        )

        self.assertEqual(
            {CANCEL_FLAG_APPLY, CANCEL_FLAG_CANCEL, CANCEL_FLAG_AGREE}, {0, 1, 2}
        )

    def test_no_reject_or_refuse_platform_route_exists(self):
        from apps.business import urls as business_urls

        def _flatten(patterns):
            for pattern in patterns:
                if hasattr(pattern, "url_patterns"):
                    yield from _flatten(pattern.url_patterns)
                else:
                    yield str(pattern.pattern)

        platform_paths = [
            path for path in _flatten(business_urls.urlpatterns) if "platform-push" in path
        ]
        rejected = [
            path
            for path in platform_paths
            if any(word in path for word in ("reject", "refuse", "deny"))
        ]
        self.assertEqual(rejected, [], "上游没有拒绝档 ⇒ 本仓不许出现拒绝路由")
        agrees = [path for path in platform_paths if "agree-cancel" in path]
        self.assertEqual(len(agrees), 1, f"同意撤销路由必须恰好一条：{agrees}")


class ValueActionTests(_ExtraActionTestBase):
    """评价打手：只有已结算；`OtherUserID` 取推送行的 `accept_uid`；无读面 ⇒ 只认成功码。"""

    def test_settled_order_is_valued_with_the_stored_accept_uid(self):
        self._set_accept_uid("USR-TAKER-0009")
        self.fake.detail = {"Status": 17, "CancelStatus": 10}
        with self._patched_extra():
            extra_module.value_push_order(
                self.order, operator=self.operator, eval_type=10, comment="打得好"
            )

        forms = self.fake.forms_for("LevelOrderValue")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["ODSerialNo"], SERIAL)
        self.assertEqual(forms[0]["OtherUserID"], "USR-TAKER-0009")
        self.assertEqual(forms[0]["EvalType"], "10")
        self.assertEqual(forms[0]["Comment"], "打得好")
        # 评价没有读面 ⇒ 写前只回读一次（判状态），写后不回读。
        self.assertEqual(self.fake.detail_calls, 1)
        self.assertTrue(self._audit_rows("order.dailiantong_value").exists())
        self.push.refresh_from_db()
        self.assertEqual(self.push.remote_status, 17)
        self.assertTrue(self.push.is_active)

    def test_comment_is_optional_and_can_fall_back_to_detail_accept_uid(self):
        self.fake.detail = {"Status": 17, "CancelStatus": 10, "AcceptUID": "USR-DETAIL-1"}
        with self._patched_extra():
            extra_module.value_push_order(
                self.order, operator=self.operator, eval_type=13
            )
        form = self.fake.forms_for("LevelOrderValue")[0]
        self.assertEqual(form["OtherUserID"], "USR-DETAIL-1")
        self.assertEqual(form["EvalType"], "13")
        self.assertNotIn("Comment", form)

    def test_unsettled_order_cannot_be_valued(self):
        self._set_accept_uid("USR-TAKER-0009")
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.value_push_order(
                    self.order, operator=self.operator, eval_type=10
                )
        self.assertIn("只有已结算", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_order_without_accept_uid_cannot_be_valued(self):
        self.fake.detail = {"Status": 17, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.value_push_order(
                    self.order, operator=self.operator, eval_type=10
                )
        self.assertIn("没有接单打手 UID", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])


class RemoteStatusLabelTests(SimpleTestCase):
    """⭐ 真机：`Status=18` = **已下架**（上游码表没列这一档）。

    现场：单被下架后代练通回 `Status=18`，我方轮询同步是通的（`remote_status` 写成了 18），
    但码表只到 17 ⇒ 界面显示「未知（18）」⇒ `REMOTE_STATUS_LABELS` 必须补 18。
    ⛔ **只改一处就红**：前端 `src/frontend/src/layui-island/platformPush.ts::REMOTE_STATUS_LABELS`
    由 `pages/__tests__/order-orderview.spec.ts` 钉同一句字面值（两处必须同值）。
    """

    def test_off_shelf_status_18_has_a_chinese_label(self):
        self.assertEqual(actions_module.REMOTE_STATUS_LABELS[18], "已下架")

    def test_the_label_helper_does_not_say_unknown_for_18(self):
        """`_status_label(18)` 必须是「已下架」，⛔ 不再是真机那个「未知（18）」。"""
        self.assertEqual(actions_module._status_label(18), "已下架")
        # 兜底仍要如实说「未知（码）」——⛔ 别为了这一档把兜底也改掉。
        self.assertEqual(actions_module._status_label(99), "未知（99）")


class OffSellActionTests(_ExtraActionTestBase):
    """上架 / 下架：下架只放未接手（11）、上架放未接手或已下架（11 / 18）；回读 `IsPub` 变成目标值。"""

    def test_order_is_taken_off_shelf_and_read_back(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "IsPub": 1}
        self.fake.next_details = [{"Status": 11, "IsPub": 0}]
        with self._patched_extra():
            extra_module.off_sell_push_order(
                self.order, operator=self.operator, flag=0
            )
        form = self.fake.forms_for("LevelOrderOffSell")[0]
        self.assertEqual(form["ODSerialNo"], SERIAL)
        self.assertEqual(form["Flag"], "0")
        self.assertEqual(self.fake.detail_calls, 2)
        self.assertTrue(self._audit_rows("order.dailiantong_off_sell").exists())

    def test_order_is_put_back_on_shelf_and_read_back(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "IsPub": 0}
        self.fake.next_details = [{"Status": 11, "IsPub": 1}]
        with self._patched_extra():
            extra_module.off_sell_push_order(
                self.order, operator=self.operator, flag=1
            )
        self.assertEqual(self.fake.forms_for("LevelOrderOffSell")[0]["Flag"], "1")

    def test_accepted_order_cannot_be_off_sold(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "IsPub": 1}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.off_sell_push_order(
                    self.order, operator=self.operator, flag=0
                )
        self.assertIn("只有未接手的代练通订单才能下架", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_off_shelf_order_can_be_put_back_on_shelf(self):
        """⭐ 真机放宽：`Status=18`（已下架）**可以上架**回 11。

        旧闸只认 `Status=11` ⇒ 下架后永远上不了架（业务方实测被卡）；原始接口
        `LevelOrderOffSell(Flag=1)` 实测能上架 ⇒ 上架方向必须放行 18。
        """
        self.fake.detail = {"Status": 18, "CancelStatus": 10, "IsPub": 0}
        self.fake.next_details = [{"Status": 11, "IsPub": 1}]
        with self._patched_extra():
            extra_module.off_sell_push_order(
                self.order, operator=self.operator, flag=1
            )
        form = self.fake.forms_for("LevelOrderOffSell")[0]
        self.assertEqual(form["ODSerialNo"], SERIAL)
        self.assertEqual(form["Flag"], "1")
        # 写前回读（判状态 / IsPub）+ 写后回读（判 IsPub）
        self.assertEqual(self.fake.detail_calls, 2)
        self.assertTrue(self._audit_rows("order.dailiantong_off_sell").exists())

    def test_in_progress_order_cannot_be_put_back_on_shelf(self):
        """上架只放 11 / 18：12（正在代练）必须拒，且走的是**状态闸**的中文（口径取上游原话）。"""
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "IsPub": 0}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.off_sell_push_order(
                    self.order, operator=self.operator, flag=1
                )
        message = str(ctx.exception)
        self.assertIn("只有未接手或已下架的代练通订单才能上架", message)
        self.assertIn("正在代练", message)
        # 前置闸 ⇒ 零上游写调用（只有那一次判状态的详情读）
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_off_shelf_order_cannot_be_taken_off_shelf_again(self):
        """下架仍**只放未接手（11）**：18 回读 `IsPub=0`、已经在下架态，再下架没有意义。

        依据：上游准入口径是「只有未接手或已下架的订单才可以修改」（`docs/DAILIANTONG-API.md`
        §4），「已下架」那档能做的动作是**上架**，不是再下一次架；且状态闸在 `IsPub` 一致性
        闸**之前**判 ⇒ 这里钉的是状态闸那句。
        """
        self.fake.detail = {"Status": 18, "CancelStatus": 10, "IsPub": 0}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.off_sell_push_order(
                    self.order, operator=self.operator, flag=0
                )
        message = str(ctx.exception)
        self.assertIn("只有未接手的代练通订单才能下架", message)
        self.assertIn("已下架", message)
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_already_in_target_state_is_not_sent_again(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "IsPub": 0}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.off_sell_push_order(
                    self.order, operator=self.operator, flag=0
                )
        self.assertIn("已经是「下架」状态", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_missing_is_pub_readback_is_not_success(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "IsPub": 1}
        self.fake.next_details = [{"Status": 11}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.off_sell_push_order(
                    self.order, operator=self.operator, flag=0
                )
        self.assertIn("没有回吐上下架状态", str(ctx.exception))


class LockActionTests(_ExtraActionTestBase):
    """锁定 / 解锁：锁放行 12 / 13 / 14、解只放行 15；回读 `Status`（解锁回锁定前状态）。

    ⭐ 2026-10-28 真机改准：`Status=15` = **被我们锁定**（`Locked=1` 在 14 上执行 ⇒ 15；
    `Locked=0` ⇒ 回到 14）。旧实现只放 12、解锁回读也只认 12，两个方向都会误拒。
    """

    def test_the_status_label_for_15_is_locked_by_us(self):
        """15 的码表字面值 = 「已锁定」（⛔ 不是「帐号锁定」）。"""
        self.assertEqual(actions_module.REMOTE_STATUS_LABELS[15], "已锁定")

    def test_in_progress_order_is_locked_and_read_back(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 15}]
        with self._patched_extra():
            push = extra_module.lock_push_order(
                self.order, operator=self.operator, locked=1
            )
        form = self.fake.forms_for("LevelOrderLock")[0]
        self.assertEqual(form["Locked"], "1")
        self.assertEqual(self.fake.detail_calls, 2)
        self.assertEqual(push.remote_status, 15)
        self.assertTrue(self._audit_rows("order.dailiantong_lock").exists())

    def test_waiting_acceptance_order_is_locked(self):
        """13（等待验收）也在允许锁定的档里（真机口径 12/13/14）。"""
        self.fake.detail = {"Status": 13, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 15}]
        with self._patched_extra():
            push = extra_module.lock_push_order(
                self.order, operator=self.operator, locked=1
            )
        self.assertEqual(self.fake.forms_for("LevelOrderLock")[0]["Locked"], "1")
        self.assertEqual(push.remote_status, 15)

    def test_exception_state_14_is_locked_and_unlocks_back_to_14(self):
        """⭐ 真机状态流：14 → 锁 ⇒ 15 → 解锁 ⇒ **回到 14**（⛔ 不写死 12）。"""
        # ① 14（订单异常）上锁 ⇒ 回读 15
        self.fake.detail = {"Status": 14, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 15}]
        with self._patched_extra():
            push = extra_module.lock_push_order(
                self.order, operator=self.operator, locked=1
            )
        self.assertEqual(self.fake.forms_for("LevelOrderLock")[0]["Locked"], "1")
        self.assertEqual(push.remote_status, 15)

        # ② 解锁 ⇒ 回读 14（上游回到锁定前的状态）
        #    ⚠️ 假上游：第 1 次详情用 `detail`、之后依次弹 `next_details`
        #    ⇒ 这一次的写前 / 写后两读都走队列（15 → 14）。
        self.fake.next_details = [{"Status": 15}, {"Status": 14}]
        with self._patched_extra():
            push = extra_module.lock_push_order(
                self.order, operator=self.operator, locked=0
            )
        forms = self.fake.forms_for("LevelOrderLock")
        self.assertEqual(len(forms), 2)
        self.assertEqual(forms[1]["Locked"], "0")
        self.assertEqual(push.remote_status, 14, "解锁回读 14 必须被认成成功")

        # 审计记**真实**落档（订单异常），⛔ 不写死「正在代练」。
        last = self._audit_rows("order.dailiantong_lock").order_by("-id").first()
        self.assertEqual(last.to_value, "订单异常")

    def test_locked_order_is_unlocked_and_read_back(self):
        self.fake.detail = {"Status": 15, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 12}]
        with self._patched_extra():
            push = extra_module.lock_push_order(
                self.order, operator=self.operator, locked=0
            )
        self.assertEqual(self.fake.forms_for("LevelOrderLock")[0]["Locked"], "0")
        self.assertEqual(push.remote_status, 12)

    def test_unlock_readback_still_locked_is_not_success(self):
        """解锁后回读仍然是 15 ⇒ 不能报成功（回读判据 = `Status != 15`）。"""
        self.fake.detail = {"Status": 15, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 15}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.lock_push_order(
                    self.order, operator=self.operator, locked=0
                )
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertIn("已锁定", str(ctx.exception))

    def test_unaccepted_order_cannot_be_locked(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.lock_push_order(
                    self.order, operator=self.operator, locked=1
                )
        self.assertIn("只有「正在代练 / 等待验收 / 订单异常」", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_cancelling_order_cannot_be_locked(self):
        """16 撤销中不在允许档里：上游虽然接受调用（状态不变），本仓**不跟随**。"""
        self.fake.detail = {"Status": 16, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.lock_push_order(
                    self.order, operator=self.operator, locked=1
                )
        self.assertIn("只有「正在代练 / 等待验收 / 订单异常」", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_readback_status_mismatch_is_not_success(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 12}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.lock_push_order(
                    self.order, operator=self.operator, locked=1
                )
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().remote_status, None)


class BlacklistActionTests(_ExtraActionTestBase):
    """拉黑打手：UID 只取推送行 / 详情的 `AcceptUID`；无读面 ⇒ 只认成功码。"""

    def test_taker_is_blacklisted_with_the_stored_accept_uid(self):
        self._set_accept_uid("USR-TAKER-0009")
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_extra():
            extra_module.blacklist_taker(self.order, operator=self.operator)
        form = self.fake.forms_for("AddBlackList")[0]
        self.assertEqual(form["UID"], "USR-TAKER-0009")
        self.assertEqual(form["Flag"], "0")
        self.assertTrue(self._audit_rows("order.dailiantong_blacklist").exists())

    def test_remove_blacklist_sends_flag_one(self):
        self._set_accept_uid("USR-TAKER-0009")
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_extra():
            extra_module.blacklist_taker(
                self.order, operator=self.operator, flag=1
            )
        self.assertEqual(self.fake.forms_for("AddBlackList")[0]["Flag"], "1")

    def test_order_without_accept_uid_is_rejected_without_writing(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.blacklist_taker(self.order, operator=self.operator)
        self.assertIn("没有接单打手 UID", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])


class OrderTemplateServiceTests(_ExtraActionTestBase):
    """代练通侧订单要求模板：列表归一化 + 四个写动作都回读列表。"""

    def test_list_normalises_upstream_rows(self):
        self.fake.templates = [
            {"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "1"},
            {"ID": 7002, "Title": "模板乙", "Temp": "内容乙", "IsDefault": "0"},
        ]
        with self._patched_extra():
            data = extra_module.list_order_templates()
        self.assertEqual(data["record_count"], 2)
        self.assertEqual(
            data["items"],
            [
                {"id": 7001, "title": "模板甲", "temp": "内容甲", "is_default": True},
                {"id": 7002, "title": "模板乙", "temp": "内容乙", "is_default": False},
            ],
        )

    def test_create_returns_the_template_id_and_reads_it_back(self):
        self.fake.next_template_id = 7001
        self.fake.templates = [
            {"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}
        ]
        with self._patched_extra():
            data = extra_module.create_order_template(
                operator=self.operator, title="模板甲", temp="内容甲"
            )
        form = self.fake.forms_for("OrderRequireTempAdd")[0]
        self.assertEqual(form["Title"], "模板甲")
        self.assertEqual(form["Temp"], "内容甲")
        self.assertEqual(form["IsDefault"], "0")
        self.assertEqual(data["id"], 7001)
        self.assertTrue(self._audit_rows("order.dailiantong_template").exists())

    def test_create_without_numeric_id_is_not_success(self):
        self.fake.responses["OrderRequireTempAdd"] = {
            "Result": 1,
            "Err": "添加成功.",
        }
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.create_order_template(
                    operator=self.operator, title="模板甲", temp="内容甲"
                )
        self.assertIn("没有返回模板 ID", str(ctx.exception))

    def test_update_sends_only_given_fields_and_reads_back(self):
        self.fake.templates = [
            {"ID": 7001, "Title": "新标题", "Temp": "内容甲", "IsDefault": "0"}
        ]
        with self._patched_extra():
            extra_module.update_order_template(
                operator=self.operator, temp_id=7001, title="新标题"
            )
        form = self.fake.forms_for("OrderRequireTempUpdate")[0]
        self.assertEqual(form["TempID"], "7001")
        self.assertEqual(form["Title"], "新标题")
        self.assertNotIn("Temp", form, "没给的字段不该发送（= 不改内容）")
        self.assertNotIn("IsDefault", form)

    def test_update_readback_mismatch_is_not_success(self):
        self.fake.templates = [
            {"ID": 7001, "Title": "老标题", "Temp": "内容甲", "IsDefault": "0"}
        ]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.update_order_template(
                    operator=self.operator, temp_id=7001, title="新标题"
                )
        self.assertIn("回读不一致", str(ctx.exception))

    def test_delete_verifies_the_template_is_gone(self):
        self.fake.template_pages = [
            [{"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}],
            [],
        ]
        with self._patched_extra():
            data = extra_module.delete_order_template(
                operator=self.operator, temp_id=7001
            )
        self.assertTrue(data["deleted"])
        self.assertEqual(len(self.fake.forms_for("OrderRequireTempDelete")), 1)

    def test_delete_readback_still_present_is_not_success(self):
        self.fake.template_pages = [
            [{"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}],
            [{"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}],
        ]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.delete_order_template(
                    operator=self.operator, temp_id=7001
                )
        self.assertIn("仍然有这条模板", str(ctx.exception))

    def test_set_default_reads_back_is_default(self):
        self.fake.templates = [
            {"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "1"}
        ]
        with self._patched_extra():
            extra_module.set_default_order_template(
                operator=self.operator, temp_id=7001
            )
        self.assertEqual(
            len(self.fake.forms_for("OrderRequireTempSetDefault")), 1
        )

    def test_set_default_readback_not_default_is_not_success(self):
        self.fake.templates = [
            {"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}
        ]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.set_default_order_template(
                    operator=self.operator, temp_id=7001
                )
        self.assertIn("仍是「否」", str(ctx.exception))


class OrderStatsServiceTests(_ExtraActionTestBase):
    """按日统计：归一化 + 缓存（第二次不再打上游）。"""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()

    def tearDown(self):
        from django.core.cache import cache

        cache.clear()
        super().tearDown()

    def test_stats_are_normalised_and_cached(self):
        with self._patched_extra():
            first = extra_module.platform_order_stats(
                start=date(2026, 10, 20), end=date(2026, 10, 26)
            )
            second = extra_module.platform_order_stats(
                start=date(2026, 10, 20), end=date(2026, 10, 26)
            )

        self.assertEqual(first, second)
        self.assertEqual(len(self.fake.forms_for("LevelOrderStat")), 1, "第二次应命中缓存")
        form = self.fake.forms_for("LevelOrderStat")[0]
        self.assertEqual(form["IsPub"], "1")
        self.assertEqual(form["sDate"], "2026-10-20 00:00:00")
        self.assertEqual(form["eDate"], "2026-10-26 23:59:59")
        self.assertEqual(first["items"][0]["QDate"], "2026-10-26")
        self.assertEqual(first["total"]["PubCount"], "3")
        self.assertEqual(first["record_count"], None)

    def test_different_direction_is_a_different_cache_entry(self):
        with self._patched_extra():
            extra_module.platform_order_stats(
                start=date(2026, 10, 20), end=date(2026, 10, 26), is_pub=1
            )
            extra_module.platform_order_stats(
                start=date(2026, 10, 20), end=date(2026, 10, 26), is_pub=0
            )
        self.assertEqual(len(self.fake.forms_for("LevelOrderStat")), 2)

    def test_paging_is_forwarded_and_part_of_the_cache_key(self):
        with self._patched_extra():
            extra_module.platform_order_stats(
                start=date(2026, 10, 20),
                end=date(2026, 10, 26),
                page_index=2,
                page_size=10,
            )
            extra_module.platform_order_stats(
                start=date(2026, 10, 20),
                end=date(2026, 10, 26),
                page_index=3,
                page_size=10,
            )
        forms = self.fake.forms_for("LevelOrderStat")
        self.assertEqual(len(forms), 2)
        self.assertEqual(forms[0]["PageIndex"], "2")
        self.assertEqual(forms[0]["PageSize"], "10")
        self.assertEqual(forms[1]["PageIndex"], "3")


class ExtraActionApiTests(_ExtraActionTestBase):
    """接口层：正常信封 / 上游失败 200 / 入参坏形状 / 订单不可见 / 403。"""

    URL_UP_PRICE = f"/api/business/orders/{ORDER_NO}/platform-push/up-price/"
    URL_ARBITRATION = f"/api/business/orders/{ORDER_NO}/platform-push/arbitration/"
    URL_VALUE = f"/api/business/orders/{ORDER_NO}/platform-push/value/"
    URL_OFF_SELL = f"/api/business/orders/{ORDER_NO}/platform-push/off-sell/"
    URL_LOCK = f"/api/business/orders/{ORDER_NO}/platform-push/lock/"
    URL_BLACKLIST = f"/api/business/orders/{ORDER_NO}/platform-push/blacklist/"
    URL_STATS = "/api/business/platform-integrations/stats/"
    URL_TEMPLATES = "/api/business/platform-integrations/order-templates/"

    def setUp(self):
        super().setUp()
        self.client = self._api_client()

    def test_up_price_api_returns_the_updated_push_row(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 11, "Price": "7.50"}]
        with self._patched_extra():
            response = self.client.post(self.URL_UP_PRICE, {"up_price": "2.50"})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(Decimal(response.data["data"]["price"]), Decimal("7.50"))

    def test_the_push_detail_row_carries_arbitration_availability(self):
        """⭐ 2026-10-28：详情读面的每一行也带「能不能申请客服介入」两键（本地快照口径）。"""
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            remote_cancel_status=11,
            cancel_requested_at=timezone.now() - timedelta(hours=2),
        )
        self.fake.detail = {
            "Status": 16,
            "CancelStatus": 11,
            "PayLevelBal": "0.00",
            "RepEnsureBal": "0.00",
        }
        url = f"/api/business/orders/{ORDER_NO}/platform-push/"

        with self._patched_client():
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        row = response.data["data"][0]
        self.assertIs(row["arbitration_available"], True)
        self.assertEqual(row["arbitration_blocked_reason"], "")

        # 未满 1 小时 ⇒ 不可用，原因里带剩余分钟数。
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            cancel_requested_at=timezone.now() - timedelta(minutes=10)
        )
        actions_module.clear_withdraw_defaults_cache()
        with self._patched_client():
            response = self.client.get(url)
        row = response.data["data"][0]
        self.assertIs(row["arbitration_available"], False)
        self.assertIn("分钟", row["arbitration_blocked_reason"])

    def test_upstream_business_failure_is_200_with_the_original_text(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        self.fake.responses["LevelOrderUpPrice"] = {
            "Result": -3,
            "Err": "支付密码错误.",
        }
        with self._patched_extra():
            response = self.client.post(self.URL_UP_PRICE, {"up_price": "2.50"})
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 1002, response.data)
        self.assertIn("支付密码错误.", response.data["msg"])

    def test_bad_input_shapes_are_1000_and_never_touch_upstream(self):
        cases = (
            (self.URL_UP_PRICE, {}),
            (self.URL_UP_PRICE, {"up_price": "abc"}),
            (self.URL_VALUE, {}),
            (self.URL_VALUE, {"eval_type": 99}),
            (self.URL_OFF_SELL, {}),
            (self.URL_OFF_SELL, {"flag": 2}),
            (self.URL_LOCK, {}),
            (self.URL_LOCK, {"locked": 9}),
            (self.URL_BLACKLIST, {"flag": 9}),
            (self.URL_TEMPLATES, {}),
            (self.URL_TEMPLATES, {"title": "甲"}),
        )
        with self._patched_extra():
            for url, body in cases:
                with self.subTest(url=url, body=body):
                    response = self.client.post(url, body)
                    self.assertEqual(response.status_code, 200, response.content[:400])
                    self.assertEqual(response.data["code"], 1000, response.data)
        self.assertEqual(self.fake.calls, [], "形状不对不该碰上游")

    def test_stats_api_returns_the_normalised_payload(self):
        from django.core.cache import cache

        cache.clear()
        with self._patched_extra():
            response = self.client.get(
                self.URL_STATS, {"start": "2026-10-20", "end": "2026-10-26"}
            )
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(response.data["data"]["items"][0]["QDate"], "2026-10-26")
        self.assertEqual(response.data["data"]["start"], "2026-10-20")

    def test_stats_api_rejects_malformed_dates_and_reversed_ranges(self):
        for query in (
            {"start": "abc", "end": "2026-10-26"},
            {"start": "2026-10-26", "end": "2026-10-20"},
            {"is_pub": 9},
        ):
            with self.subTest(query=query):
                response = self.client.get(self.URL_STATS, query)
                self.assertEqual(response.status_code, 200, response.content[:400])
                self.assertEqual(response.data["code"], 1000, response.data)

    def test_template_api_creates_and_lists(self):
        self.fake.next_template_id = 7001
        self.fake.templates = [
            {"ID": 7001, "Title": "模板甲", "Temp": "内容甲", "IsDefault": "0"}
        ]
        with self._patched_extra():
            created = self.client.post(
                self.URL_TEMPLATES, {"title": "模板甲", "temp": "内容甲"}
            )
            listed = self.client.get(self.URL_TEMPLATES)
        self.assertEqual(created.data["code"], 0, created.data)
        self.assertEqual(created.data["data"]["id"], 7001)
        self.assertEqual(listed.data["code"], 0, listed.data)
        self.assertEqual(len(listed.data["data"]["items"]), 1)

    def test_template_detail_updates_and_deletes(self):
        self.fake.templates = [
            {"ID": 7001, "Title": "新标题", "Temp": "内容甲", "IsDefault": "0"}
        ]
        with self._patched_extra():
            updated = self.client.patch(
                f"{self.URL_TEMPLATES}7001/",
                json.dumps({"title": "新标题"}),
                content_type="application/json",
            )
        self.assertEqual(updated.data["code"], 0, updated.data)
        self.assertEqual(updated.data["data"]["title"], "新标题")

        self.fake.template_pages = [
            [{"ID": 7001, "Title": "新标题", "Temp": "内容甲", "IsDefault": "0"}],
            [],
        ]
        with self._patched_extra():
            removed = self.client.delete(f"{self.URL_TEMPLATES}7001/")
        self.assertEqual(removed.data["code"], 0, removed.data)

    def test_template_update_without_any_field_is_1000(self):
        response = self.client.patch(
            f"{self.URL_TEMPLATES}7001/",
            json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertEqual(response.data["code"], 1000, response.data)

    def test_unknown_order_is_a_business_error(self):
        response = self.client.post(
            "/api/business/orders/20261027000000009999/platform-push/up-price/",
            {"up_price": "2.50"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertEqual(response.data["msg"], "订单不存在或不可见")

    def test_extra_write_endpoints_require_the_platform_write_node(self):
        """⭐ 写端点要 `order.platform_write`，**读**端点仍要 `order.list`。

        两向都钉（这是 M5 的核心判据）：零节点 ⇒ 全 403；只持 `order.list` ⇒
        **写**端点 403、**读**端点放行到服务层；只持 `order.platform_write` ⇒ 反过来。
        ⛔ 只测「零节点 403」抓不到 M5 那个洞（修复前它们挂的是 `order.list`）。
        """
        from unittest import mock

        write_cases = (
            ("post", self.URL_UP_PRICE, {"up_price": "2.50"}),
            ("post", self.URL_ARBITRATION, {}),
            ("post", self.URL_VALUE, {"eval_type": 10}),
            ("post", self.URL_OFF_SELL, {"flag": 0}),
            ("post", self.URL_LOCK, {"locked": 1}),
            ("post", self.URL_BLACKLIST, {}),
            ("post", self.URL_TEMPLATES, {"title": "甲", "temp": "乙"}),
        )
        read_cases = (
            ("get", self.URL_STATS, None),
            ("get", self.URL_TEMPLATES, None),
        )

        def _call(client, method, url, body):
            return client.get(url) if method == "get" else client.post(url, body)

        # ① 零节点：写 + 读都 403。
        denied = make_logged_in_client(
            username="dlt-extra-denied", groups=(), nodes=()
        )
        with mock.patch(
            "apps.business.dailiantong_extra_actions._open_client",
            side_effect=self._client_factory,
        ) as patched:
            for method, url, body in (*write_cases, *read_cases):
                with self.subTest(nodes="none", url=url, method=method):
                    response = _call(denied, method, url, body)
                    self.assertEqual(response.status_code, 403, response.content[:400])
            self.assertEqual(patched.call_count, 0, "403 请求不该构造上游客户端")

        # ② 只持 order.list：写全 403（M5 修复点），读放行（只是顺序：读会进服务层，
        #    上游被桩接住 ⇒ 断言「不是 403」就够，不重复断言它的业务结果）。
        list_only = make_logged_in_client(
            username="dlt-extra-list-only", groups=("rdlst",), nodes=(NODE,)
        )
        with mock.patch(
            "apps.business.dailiantong_extra_actions._open_client",
            side_effect=self._client_factory,
        ):
            for method, url, body in write_cases:
                with self.subTest(nodes="list-only", url=url, method=method):
                    response = _call(list_only, method, url, body)
                    self.assertEqual(response.status_code, 403, response.content[:400])
            for method, url, body in read_cases:
                with self.subTest(nodes="list-only-read", url=url, method=method):
                    response = _call(list_only, method, url, body)
                    self.assertNotEqual(
                        response.status_code, 403,
                        f"读端点不该被写节点拦住：{url} {response.content[:200]}",
                    )

        # ③ 只持 order.platform_write：写不是 403、读是 403。
        write_only = make_logged_in_client(
            username="dlt-extra-write-only", groups=("wron",),
            nodes=(PLATFORM_WRITE_NODE,),
        )
        with mock.patch(
            "apps.business.dailiantong_extra_actions._open_client",
            side_effect=self._client_factory,
        ):
            for method, url, body in write_cases:
                with self.subTest(nodes="write-only", url=url, method=method):
                    response = _call(write_only, method, url, body)
                    self.assertNotEqual(
                        response.status_code, 403,
                        f"写端点不该再要求 order.list：{url} {response.content[:200]}",
                    )
            for method, url, body in read_cases:
                with self.subTest(nodes="write-only-read", url=url, method=method):
                    response = _call(write_only, method, url, body)
                    self.assertEqual(response.status_code, 403, response.content[:400])


    def test_no_credentials_leak_into_the_new_endpoints(self):
        self._set_accept_uid("USR-TAKER-0009")
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        responses = []
        self.fake.next_details = [{"Status": 11, "Price": "7.50"}]
        with self._patched_extra():
            responses.append(self.client.post(self.URL_UP_PRICE, {"up_price": "2.50"}))
        self.fake.detail = {"Status": 16, "CancelStatus": 11}
        self.fake.next_details = [{"Status": 16, "CancelStatus": 14}]
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            cancel_requested_at=timezone.now() - timedelta(hours=2)
        )
        with self._patched_extra():
            responses.append(self.client.post(self.URL_ARBITRATION, {}))
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "IsPub": 1}
        self.fake.next_details = [{"Status": 11, "IsPub": 0}]
        with self._patched_extra():
            responses.append(self.client.post(self.URL_OFF_SELL, {"flag": 0}))
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 15}]
        with self._patched_extra():
            responses.append(self.client.post(self.URL_LOCK, {"locked": 1}))
        with self._patched_extra():
            responses.append(self.client.post(self.URL_BLACKLIST, {}))

        blob = json.dumps([r.data for r in responses], default=str)
        blob += json.dumps(
            list(
                OperationLog.objects.values(
                    "action", "summary", "reason", "from_value", "to_value", "actor"
                )
            ),
            default=str,
        )
        for secret in (APP_SECRET, PAY_PASSWORD_MD5, LEAKY_GAME_PASS):
            self.assertNotIn(secret, blob)


# ── P7：撤回时页面填的两个金额──────────────────────────────


class WithdrawAmountsTests(_ActionTestBase):
    """`LevelOrderCancel` 的 `PayLevelBal` / `RepEnsureBal` 改成**页面可填**。

    业务方口径：对标撤销弹窗有三格（支付代练费 / 赔保证金 / 撤销理由），
    旧实现两个金额**静默从详情取**，操作人看不到也改不了。
    """

    def test_amounts_from_the_page_are_sent_verbatim(self):
        self.fake.detail = {
            "Status": 12,
            "CancelStatus": 10,
            "PayLevelBal": "2.00",
            "RepEnsureBal": "0.00",
        }
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            actions_module.withdraw_push(
                self.order,
                operator=self.operator,
                comment="打手已被别的渠道接走",
                pay_level_bal=Decimal("3.50"),
                rep_ensure_bal=Decimal("1.25"),
            )

        forms = self.fake.forms_for("LevelOrderCancel")
        self.assertEqual(len(forms), 1)
        self.assertEqual(Decimal(forms[0]["PayLevelBal"]), Decimal("3.50"))
        self.assertEqual(Decimal(forms[0]["RepEnsureBal"]), Decimal("1.25"))
        self.assertEqual(forms[0]["Comment"], "打手已被别的渠道接走")
        self.assertTrue(forms[0].get("PayPass"), "申请撤销仍要支付密码（客户端现算）")

    def test_amounts_not_given_fall_back_to_the_detail(self):
        self.fake.detail = {
            "Status": 12,
            "CancelStatus": 10,
            "PayLevelBal": "2.00",
            "RepEnsureBal": "0.50",
        }
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        with self._patched_client():
            actions_module.withdraw_push(self.order, operator=self.operator)

        form = self.fake.forms_for("LevelOrderCancel")[0]
        self.assertEqual(Decimal(form["PayLevelBal"]), Decimal("2.00"))
        self.assertEqual(Decimal(form["RepEnsureBal"]), Decimal("0.50"))

    def test_a_negative_amount_is_rejected_before_any_upstream_call(self):
        with self.assertRaises(DailiantongPushError) as ctx:
            actions_module.withdraw_push(
                self.order, operator=self.operator, pay_level_bal=Decimal("-1.00")
            )
        self.assertIn("支付代练费不能是负数", str(ctx.exception))
        self.assertEqual(self.fake.calls, [], "前置闸不通过时⛔ 一次上游调用都不许有")

    def test_the_api_accepts_the_three_cells(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "PayLevelBal": "2.00"}
        self.fake.next_details = [{"Status": 12, "CancelStatus": 11}]
        client = self._api_client()
        with self._patched_client():
            response = client.post(
                URL_WITHDRAW,
                data=json.dumps(
                    {
                        "comment": "协商撤销",
                        "pay_level_bal": "1.00",
                        "rep_ensure_bal": "0.00",
                    }
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        form = self.fake.forms_for("LevelOrderCancel")[0]
        self.assertEqual(Decimal(form["PayLevelBal"]), Decimal("1.00"))


# ── P3：从所有平台撤回──────────────────────────────────────


URL_WITHDRAW_ALL = f"/api/business/orders/{ORDER_NO}/platform-pushes/withdraw-all/"


class WithdrawAllPlatformsTests(_ActionTestBase):
    def test_no_push_at_all_is_a_chinese_error_and_no_upstream_call(self):
        DailiantongPush.objects.all().delete()
        with self.assertRaises(DailiantongPushError) as ctx:
            actions_module.withdraw_all_platforms(self.order, operator=self.operator)
        self.assertIn("没有抛到任何平台", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_one_platform_success_is_reported(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        self.fake.next_details = [_deleted_detail()]
        with self._patched_client():
            result = actions_module.withdraw_all_platforms(
                self.order, operator=self.operator
            )

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 0)
        row = result["results"][0]
        self.assertTrue(row["ok"])
        self.assertEqual(row["platform"], 10)
        self.assertEqual(row["state"], DailiantongPush.State.CLOSED)
        self.assertIn("删除", row["message"])
        self.assertEqual(
            self._refresh().state, DailiantongPush.State.CLOSED
        )

    def test_partial_success_is_reported_per_platform(self):
        """多平台时**逐平台回结果**：一个成功、一个（本版没客户端）如实报失败。"""
        DailiantongPush.objects.create(
            order=self.order,
            platform=1,
            state=DailiantongPush.State.SENT,
            serial_no="19000000000000000062",
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=ORDER_NO,
        )
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        self.fake.next_details = [_deleted_detail()]
        with self._patched_client():
            result = actions_module.withdraw_all_platforms(
                self.order, operator=self.operator
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        by_platform = {row["platform"]: row for row in result["results"]}
        self.assertTrue(by_platform[10]["ok"])
        self.assertFalse(by_platform[1]["ok"])
        self.assertIn("只对接了代练通", by_platform[1]["message"])

    def test_the_api_returns_per_platform_results(self):
        self.fake.detail = {"Status": 11, "CancelStatus": 10}
        self.fake.next_details = [_deleted_detail()]
        client = self._api_client()
        with self._patched_client():
            response = client.post(
                URL_WITHDRAW_ALL, data=json.dumps({}), content_type="application/json"
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["succeeded"], 1)
        self.assertIn("results", payload["data"])

    def test_the_api_says_clearly_when_nothing_was_pushed(self):
        DailiantongPush.objects.all().delete()
        client = self._api_client()
        response = client.post(
            URL_WITHDRAW_ALL, data=json.dumps({}), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 1002)
        self.assertIn("没有抛到任何平台", payload["msg"])


# ── P7-详情：重发失败 / 已关闭的推送────────────────────────


URL_REPUSH = f"/api/business/orders/{ORDER_NO}/platform-push/repush/"


class RepushTests(_ActionTestBase):
    def _patched_both(self):
        from unittest import mock

        return (
            mock.patch(
                "apps.business.dailiantong_actions._open_client",
                side_effect=self._client_factory,
            ),
            mock.patch(
                "apps.business.dailiantong_push._open_client",
                side_effect=self._client_factory,
            ),
        )

    def _make_failed(self):
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            state=DailiantongPush.State.FAILED, serial_no=None
        )
        self.push.refresh_from_db()

    def test_failed_push_is_reissued_through_the_normal_gates(self):
        self._make_failed()
        self.fake.responses["LevelOrderAdd"] = {
            "Result": 1,
            "Err": "19000000000000000099",
        }
        first, second = self._patched_both()
        with first, second:
            new_push = actions_module.repush_failed_push(
                self.order,
                operator=self.operator,
                price=Decimal("5.00"),
                zone_server_id="100100010000000",
            )

        self.assertEqual(new_push.state, DailiantongPush.State.SENT)
        self.assertEqual(new_push.serial_no, "19000000000000000099")
        forms = self.fake.forms_for("LevelOrderAdd")
        self.assertEqual(len(forms), 1, "重发必须真的走一次发单")
        self.assertEqual(Decimal(forms[0]["Price"]), Decimal("5.00"))
        self.assertEqual(forms[0]["ZoneServerID"], "100100010000000")
        self.assertTrue(
            self._audit_rows("order.dailiantong_repush").exists(),
            "重发这件事本身要留审计",
        )

    def test_defaults_come_from_the_previous_push_row(self):
        self._make_failed()
        self.fake.responses["LevelOrderAdd"] = {
            "Result": 1,
            "Err": "19000000000000000098",
        }
        first, second = self._patched_both()
        with first, second:
            actions_module.repush_failed_push(
                self.order,
                operator=self.operator,
                zone_server_id="100100010000000",
            )
        form = self.fake.forms_for("LevelOrderAdd")[0]
        self.assertEqual(Decimal(form["Price"]), Decimal("5.00"))
        self.assertEqual(Decimal(form["Ensure1"]), Decimal("0.00"))
        self.assertEqual(int(form["TimeLimit"]), 24)

    def test_a_sent_push_cannot_be_reissued(self):
        with self.assertRaises(DailiantongPushError) as ctx:
            actions_module.repush_failed_push(
                self.order,
                operator=self.operator,
                zone_server_id="100100010000000",
            )
        message = str(ctx.exception)
        self.assertIn("已抛过", message)
        self.assertIn(SERIAL, message, "拒绝文案要带已有平台单号（P4）")
        self.assertNotIn("LevelOrderAdd", self.fake.actions)

    def test_a_pending_push_cannot_be_reissued(self):
        DailiantongPush.objects.filter(pk=self.push.pk).update(
            state=DailiantongPush.State.PENDING
        )
        with self.assertRaises(DailiantongPushError):
            actions_module.repush_failed_push(
                self.order,
                operator=self.operator,
                zone_server_id="100100010000000",
            )

    def test_the_api_reissues_without_publishing_to_the_hall(self):
        self._make_failed()
        old_hall_price = self.order.hall_price
        self.order.status = OrderStatus.REFUNDED
        self.order.save(update_fields=["status"])
        self.fake.responses["LevelOrderAdd"] = {
            "Result": 1,
            "Err": "19000000000000000097",
        }
        client = self._api_client()
        first, second = self._patched_both()
        with first, second:
            response = client.post(
                URL_REPUSH,
                data=json.dumps({"zone_server_id": "100100010000000"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["serial_no"], "19000000000000000097")
        self.assertIn("LevelOrderAdd", self.fake.actions)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.REFUNDED)
        self.assertEqual(self.order.hall_price, old_hall_price)
        self.assertFalse(self._audit_rows("order.throw_to_hall").exists())


# ── 只读：平台留言 / 进度图（详情页）──────────────────────────


URL_MESSAGES = f"/api/business/orders/{ORDER_NO}/platform-push/messages/"
URL_PROGRESS = f"/api/business/orders/{ORDER_NO}/platform-push/progress/"


class PushReadTests(_ActionTestBase):
    def test_the_message_list_is_requested_with_the_documented_max(self):
        """业务方口径：要**全部**留言 ⇒ `Max` 取文档上限 500。

        判据打在**真实出口**上（上游收到的表单值），⛔ 不断言常量本身：
        `docs/references/dailiantong/open-api-v3.4.md`「获取订单留言」明写「不能超过500」。
        """
        self.fake.message_rows = [
            {"Msg": f"第{i}条", "MsgType": 11, "CreateDate": "t"} for i in range(3)
        ]
        with self._patched_client():
            actions_module.read_push_messages(self.order)
        form = self.fake.forms_for("LevelOrderMessageList")[0]
        self.assertEqual(form.get("Max"), "500")

    def test_system_and_user_messages_are_all_returned_without_filtering(self):
        """⛔ 不做任何过滤：系统消息（`MsgType=10`）与用户 / 打手消息（11）都要在列表里。"""
        self.fake.message_rows = [
            {"Msg": "系统：订单已开始代练", "MsgType": 10, "OperationID": "系统"},
            {"Msg": "打手：已上号", "MsgType": 11, "OperationID": "接单者"},
            {"Msg": "发单者：收到", "MsgType": 11, "OperationID": "发单者"},
        ]
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)
        self.assertEqual(data["error"], "")
        self.assertEqual([row["text"] for row in data["messages"]], [
            "系统：订单已开始代练",
            "打手：已上号",
            "发单者：收到",
        ])

    def test_messages_are_returned_with_system_flag(self):
        self.fake.message_rows = [
            {"Msg": "你好，什么时候上号", "CreateDate": "2026-10-27 10:00:00"},
            {
                "Msg": (
                    "已接单，请按规则开工"
                    "（系统自动回复·订单 20261027000000000001·规则 7）"
                ),
                "CreateDate": "2026-10-27 10:01:00",
            },
        ]
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)

        self.assertEqual(data["error"], "")
        self.assertEqual(len(data["messages"]), 2)
        self.assertFalse(data["messages"][0]["is_system"])
        self.assertTrue(data["messages"][1]["is_system"])
        self.assertEqual(data["messages"][0]["time"], "2026-10-27 10:00:00")

    def test_upstream_message_fields_are_mapped(self):
        """⭐ 上游字段 ⇒ `{sender, uid, nickname, is_system}`。

        上游字段（`docs/references/dailiantong/open-api-v3.4.md` 的「获取订单留言」）：
        `MsgType` 10 系统消息 / 11 用户消息；`OperationID` = 留言发布方（接单者 / 发单者 /
        客服）；`NickName` = 发布人昵称；`UID` = 发布人用户 ID。三类留言都要能取到（包括**打手**发的）。
        """
        self.fake.message_rows = [
            {
                "Msg": "打手：已上号，预计三小时",
                "MsgType": 11,
                "OperationID": "接单者",
                "UID": "USR-SYNTH-GRABBER",
                "NickName": "合成打手",
                "CreateDate": "2026-10-27 10:00:00",
            },
            {
                "Msg": "客服：收到，注意时间",
                "MsgType": 11,
                "OperationID": "发单者",
                "UID": "USR-SYNTH-PUBLISHER",
                "NickName": "合成客服",
                "CreateDate": "2026-10-27 10:01:00",
            },
            {
                "Msg": "系统：订单已开始代练",
                "MsgType": 10,
                "OperationID": "系统",
                "CreateDate": "2026-10-27 10:02:00",
            },
        ]
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)

        self.assertEqual(data["error"], "")
        self.assertEqual([row["text"] for row in data["messages"]], [
            "打手：已上号，预计三小时",
            "客服：收到，注意时间",
            "系统：订单已开始代练",
        ])
        self.assertEqual([row["sender"] for row in data["messages"]], [
            "接单者", "发单者", "系统",
        ])
        self.assertEqual([row["uid"] for row in data["messages"]], [
            "USR-SYNTH-GRABBER", "USR-SYNTH-PUBLISHER", "",
        ])
        self.assertEqual([row["nickname"] for row in data["messages"]], [
            "合成打手", "合成客服", "",
        ])
        self.assertEqual(
            [row["is_system"] for row in data["messages"]], [False, False, True]
        )
        self.assertEqual(data["messages"][0]["time"], "2026-10-27 10:00:00")

    def test_message_sender_falls_back_to_the_nickname_then_uid(self):
        """`OperationID` 缺失时退回 `NickName`，再缺退回 `UID`；都没有 ⇒ 空串（⛔ 不编）。"""
        self.fake.message_rows = [
            {"Msg": "有昵称", "NickName": "合成打手", "UID": "USR-1", "CreateDate": "t1"},
            {"Msg": "只有 UID", "UID": "USR-2", "CreateDate": "t2"},
            {"Msg": "都没有", "CreateDate": "t3"},
        ]
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)
        self.assertEqual(
            [row["sender"] for row in data["messages"]], ["合成打手", "USR-2", ""]
        )

    def test_an_empty_message_list_is_not_an_error(self):
        """空列表 ≠ 读失败：`messages=[]` + `images=[]` + `error=""`，页面才能说「还没有留言」。"""
        self.fake.message_rows = []
        self.fake.progress_rows = []
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)
        self.assertEqual(
            data,
            {"messages": [], "images": [], "images_error": "", "error": ""},
        )

    def test_our_auto_reply_marker_is_returned_verbatim(self):
        """⭐ FX3：上游内容里含我们发过的「系统自动回复」标记 ⇒ **原样返回**，⛔ 不过滤。"""
        ours = (
            "已接单，请按规则开工"
            "（系统自动回复·订单 20261027000000000001·规则 7）"
        )
        self.fake.message_rows = [
            {"Msg": ours, "MsgType": 11, "OperationID": "发单者", "CreateDate": "t"}
        ]
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)
        self.assertEqual(data["messages"][0]["text"], ours)
        self.assertTrue(
            data["messages"][0]["is_system"],
            "我们自己的系统自动回复要标成系统留言（即使是用户消息通道发出的）",
        )

    def test_a_sent_message_shows_up_in_the_history(self):
        """⭐ FX3：发送接口真能发（上游收到 `LevelOrderMessageAdd`），且回读能取到它。"""
        client = self._api_client()
        with self._patched_client():
            sent = client.post(
                URL_MESSAGE,
                data=json.dumps({"msg": "请尽快上号"}),
                content_type="application/json",
            )
            history = client.get(URL_MESSAGES)
        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.json()["code"], 0, sent.json())
        self.assertEqual(self.fake.actions.count("LevelOrderMessageAdd"), 1)
        form = self.fake.forms_for("LevelOrderMessageAdd")[0]
        self.assertEqual(form.get("Msg"), "请尽快上号")
        self.assertEqual(form.get("ODSerialNo"), SERIAL)
        # 回读：上游列表里已经有这条留言 ⇒ 页面拿得到（发送 → 历史 是同一份数据）。
        self.assertEqual(history.status_code, 200)
        payload = history.json()
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["error"], "")
        self.assertIn(
            "请尽快上号", [row["text"] for row in payload["data"]["messages"]]
        )

    def test_a_failed_message_read_is_a_chinese_reason_through_the_api(self):
        self.fake.timeout_actions.add("LevelOrderMessageList")
        client = self._api_client()
        with self._patched_client():
            response = client.get(URL_MESSAGES)
        self.assertEqual(response.status_code, 200, "读失败⛔ 不许 500")
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertEqual(payload["data"]["messages"], [])
        self.assertEqual(payload["data"]["images"], [])
        self.assertIn("读取代练通留言失败", payload["data"]["error"])

    def test_the_messages_response_carries_the_same_progress_images(self):
        """业务方口径：留言 API 没有图片字段 ⇒ `images` 与进度列表**同源**。

        上游 `LevelOrderMessageList` 只有 `MsgType / UID / NickName / CreateDate / Msg /
        OperationID`（`docs/references/dailiantong/open-api-v3.4.md:675`），图片在
        `LevelOrderProgressList` ⇒ 留言响应里附同一单的全部图。
        """
        self.fake.message_rows = [{"Msg": "已上号", "MsgType": 11}]
        self.fake.progress_rows = [
            {
                "Msg": "接单后自动发送的图片",
                "Img": "https://oss.synth/tt/test/order/qr-1.png",
                "CreateDate": "2026-10-27 10:02:00",
            },
            {
                "Msg": "客服补充的进度截图",
                "Img": "tt/test/order/shot-2.png",
                "CreateDate": "2026-10-27 10:03:00",
            },
        ]
        with self._patched_client():
            messages = actions_module.read_push_messages(self.order)
            progress = actions_module.read_push_progress(self.order)

        self.assertEqual(messages["error"], "")
        self.assertEqual(messages["images_error"], "")
        self.assertEqual(messages["images"], progress["progress"])
        self.assertEqual(messages["images"][0]["caption"], "接单后自动发送的图片")
        self.assertEqual(
            messages["images"][0]["url"], "https://oss.synth/tt/test/order/qr-1.png"
        )
        self.assertEqual(
            messages["images"][1]["url"],
            "",
            "对象名没有下载签名 ⇒ `url` 留空，⛔ 不编一个点不开的链接",
        )

    def test_a_failed_image_read_keeps_the_messages_and_gives_a_chinese_reason(self):
        """图片读失败不该把已经取到的留言一起丢掉；失败要说中文原因。"""
        self.fake.message_rows = [{"Msg": "已上号", "MsgType": 11}]
        self.fake.timeout_actions.add("LevelOrderProgressList")
        with self._patched_client():
            data = actions_module.read_push_messages(self.order)
        self.assertEqual(data["error"], "")
        self.assertEqual([row["text"] for row in data["messages"]], ["已上号"])
        self.assertEqual(data["images"], [])
        self.assertIn("读取代练通进度图失败", data["images_error"])

    def test_progress_images_keep_the_object_key_and_only_url_when_it_is_http(self):
        self.fake.progress_rows = [
            {
                "Msg": "接单后自动发送的图片",
                "Img": "https://oss.synth/tt/test/order/qr-1.png",
                "CreateDate": "2026-10-27 10:02:00",
            },
            {
                "Msg": "客服补充的进度截图",
                "Img": "tt/test/order/shot-2.png",
                "CreateDate": "2026-10-27 10:03:00",
            },
        ]
        with self._patched_client():
            data = actions_module.read_push_progress(self.order)

        self.assertEqual(data["error"], "")
        self.assertEqual(
            data["progress"][0]["url"], "https://oss.synth/tt/test/order/qr-1.png"
        )
        self.assertEqual(data["progress"][1]["img"], "tt/test/order/shot-2.png")
        self.assertEqual(
            data["progress"][1]["url"], "", "对象名没有下载签名 ⇒ ⛔ 不编一个点不开的链接"
        )

    def test_upstream_failure_is_a_chinese_error_not_an_exception(self):
        self.fake.timeout_actions.add("LevelOrderMessageList")
        self.fake.timeout_actions.add("LevelOrderProgressList")
        with self._patched_client():
            messages = actions_module.read_push_messages(self.order)
            progress = actions_module.read_push_progress(self.order)
        self.assertIn("读取代练通留言失败", messages["error"])
        self.assertEqual(messages["messages"], [])
        self.assertEqual(messages["images"], [])
        self.assertIn("读取代练通进度图失败", progress["error"])

    def test_reads_do_not_write_anything(self):
        before = OperationLog.objects.count()
        self.fake.message_rows = []
        self.fake.progress_rows = []
        with self._patched_client():
            actions_module.read_push_messages(self.order)
            actions_module.read_push_progress(self.order)
        self.assertEqual(OperationLog.objects.count(), before, "只读端点⛔ 不写审计")

    def test_the_api_returns_both_lists(self):
        self.fake.message_rows = [{"Msg": "hi", "CreateDate": "t"}]
        self.fake.progress_rows = [{"Msg": "cap", "Img": "tt/x.png"}]
        client = self._api_client()
        with self._patched_client():
            messages = client.get(URL_MESSAGES)
            progress = client.get(URL_PROGRESS)
        self.assertEqual(messages.status_code, 200)
        self.assertEqual(messages.json()["data"]["messages"][0]["text"], "hi")
        # 留言响应也带同一单的图片（`images` 来自进度列表）。
        self.assertEqual(messages.json()["data"]["images"][0]["caption"], "cap")
        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.json()["data"]["progress"][0]["caption"], "cap")


# ── 确认完单：补一条路由（服务层已有）────────────────────────────────────


URL_FINISH = f"/api/business/orders/{ORDER_NO}/platform-push/finish/"


class FinishRouteTests(_ActionTestBase):
    def test_the_route_calls_accept_push_and_reads_back_settled(self):
        self.fake.detail = {"Status": 13, "CancelStatus": 10}
        self.fake.next_details = [{"Status": 17, "CancelStatus": 10}]
        client = self._api_client()
        with self._patched_client():
            response = client.post(URL_FINISH)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertEqual(self.fake.actions.count("LevelOrderOver"), 1)
        self.assertEqual(self._refresh().remote_status, 17)
        self.assertTrue(self._audit_rows("order.dailiantong_accept").exists())

    def test_a_non_waiting_order_is_rejected_with_zero_writes(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        client = self._api_client()
        with self._patched_client():
            response = client.post(URL_FINISH)
        payload = response.json()
        self.assertEqual(payload["code"], 1002)
        self.assertIn("等待验收", payload["msg"])
        self.assertNotIn("LevelOrderOver", self.fake.actions)


# ── 详情页补充动作：补款 / 编辑平台单 / 追加截图──────────────


URL_AMOUNT = f"/api/business/orders/{ORDER_NO}/platform-push/amount/"
URL_EDIT = f"/api/business/orders/{ORDER_NO}/platform-push/edit/"
URL_SCREENSHOT = f"/api/business/orders/{ORDER_NO}/platform-push/screenshot/"

#: 一张最小的合成 PNG（魔数正确即可：服务层只按字节魔数判图种）。
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"synthetic-image-body"


class AddAmountTests(_ExtraActionTestBase):
    def test_amount_is_added_and_read_back(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 12, "Price": "6.00"}]
        with self._patched_extra():
            push = extra_module.add_push_amount(
                self.order, operator=self.operator, amount=Decimal("1.00")
            )

        forms = self.fake.forms_for("LevelOrderModify_Special")
        self.assertEqual(len(forms), 1)
        self.assertEqual(str(forms[0]["Kind"]), "0", "补款是 Kind=0")
        # ⭐ 真机读数：`Val` 必须是**整数元**的字面量——发 "1.00" 会被
        # `-5 补款金额不能含有小数！` 拒（见 docs/DAILIANTONG-API.md §6.5）。
        self.assertEqual(str(forms[0]["Val"]), "1")
        self.assertTrue(forms[0].get("PayPass"), "补款要支付密码")
        self.assertEqual(self.fake.detail_calls, 2, "写完必须回读 Price")
        self.assertEqual(push.price, Decimal("6.00"))
        self.assertTrue(self._audit_rows("order.dailiantong_amount").exists())

    def test_zero_amount_is_rejected_before_any_upstream_call(self):
        with self.assertRaises(DailiantongPushError):
            extra_module.add_push_amount(
                self.order, operator=self.operator, amount=Decimal("0.00")
            )
        self.assertEqual(self.fake.calls, [])

    def test_a_decimal_amount_is_rejected_locally_with_zero_upstream_writes(self):
        """⭐ 2026-10-28 真机：1.5 元 → `-5 补款金额不能含有小数！` ⇒ 本地先挡。"""
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_amount(
                    self.order, operator=self.operator, amount=Decimal("1.50")
                )
        self.assertIn("整数元", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_an_amount_below_one_yuan_is_rejected_locally(self):
        """⭐ 2026-10-28 真机：0 元 → `-3 补款金额不能低于1元.` ⇒ 本地先挡。"""
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_amount(
                    self.order, operator=self.operator, amount=Decimal("0")
                )
        self.assertIn("不能低于 1 元", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_an_unaccepted_order_cannot_be_topped_up(self):
        """补款只给**已接手**的单；未接手（11）本地先挡、零上游写调用。"""
        self.fake.detail = {"Status": 11, "CancelStatus": 10, "Price": "5.00"}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_amount(
                    self.order, operator=self.operator, amount=Decimal("1.00")
                )
        self.assertIn("正在代练", str(ctx.exception))
        self.assertEqual(self.fake.actions, ["LevelOrderDetail"])

    def test_a_pending_review_order_can_be_topped_up(self):
        """待验收（13）放行（2026-10-28 真机：13 补款 1 元成功、Price 3→4）。"""
        self.fake.detail = {"Status": 13, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 13, "Price": "6.00"}]
        with self._patched_extra():
            push = extra_module.add_push_amount(
                self.order, operator=self.operator, amount=Decimal("1.00")
            )
        self.assertEqual(push.price, Decimal("6.00"))

    def test_readback_mismatch_is_not_reported_as_success(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 12, "Price": "5.00"}]
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_amount(
                    self.order, operator=self.operator, amount=Decimal("1.00")
                )
        self.assertIn("没能核验到", str(ctx.exception))
        self.assertEqual(self._refresh().price, Decimal("5.00"))

    def test_insufficient_balance_is_a_chinese_error_with_zero_writes(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "Price": "5.00"}
        self.fake.balance = "1.00"
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_amount(
                    self.order, operator=self.operator, amount=Decimal("2.00")
                )
        self.assertIn("余额不足", str(ctx.exception))
        self.assertNotIn("LevelOrderModify_Special", self.fake.actions)

    def test_the_api_route_works(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10, "Price": "5.00"}
        self.fake.next_details = [{"Status": 12, "Price": "7.00"}]
        client = self._api_client()
        with self._patched_extra():
            response = client.post(
                URL_AMOUNT, data=json.dumps({"amount": "2.00"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(response.json()["data"]["price"], "7.00")

    def test_the_api_route_rejects_a_decimal_amount_as_1000(self):
        client = self._api_client()
        with self._patched_extra():
            response = client.post(
                URL_AMOUNT, data=json.dumps({"amount": "1.50"}),
                content_type="application/json",
            )
        body = response.json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("整数元", body["msg"])
        self.assertEqual(self.fake.calls, [])


class EditPushOrderTests(_ExtraActionTestBase):
    def _editable_detail(self):
        return {
            "Status": 11,
            "CancelStatus": 10,
            "Title": "旧标题",
            "TimeLimit": 24,
            "Price": "5.00",
            "Ensure1": "0.00",
            "Ensure2": "0.00",
            "Memo": "旧说明",
            "Label1": "",
            "Label2": "",
            "GameAcc": "acct0001",
            "GamePass": "pass0001",
            "Actor": "待定角色",
            "CurrInfo": "其它:无",
            "Require": "接单后联系客服获取账号",
        }

    def test_title_is_edited_and_read_back(self):
        self.fake.detail = self._editable_detail()
        after = self._editable_detail()
        after["Title"] = "新标题"
        self.fake.next_details = [after]
        with self._patched_extra():
            extra_module.edit_push_order(
                self.order, operator=self.operator, title="新标题"
            )

        forms = self.fake.forms_for("LevelOrderModify")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["Title"], "新标题")
        self.assertEqual(Decimal(forms[0]["Price"]), Decimal("5.00"), "金额原样回传、⛔ 不改钱")
        self.assertTrue(forms[0].get("PayPass"), "改单要支付密码")
        self.assertEqual(self.fake.detail_calls, 2)
        self.assertTrue(self._audit_rows("order.dailiantong_edit").exists())

    def test_off_shelf_18_is_also_editable(self):
        """⭐ `Status=18 已下架` 也放行。

        上游原话是「只有**未接手或已下架**的订单才可以修改」；旧实现只放 11，
        把同样可编辑的 18 挡住了（与上架闸 H7 同族残条）。
        """
        self.fake.detail = dict(self._editable_detail(), Status=18)
        after = dict(self._editable_detail(), Status=18, Title="新标题")
        self.fake.next_details = [after]

        with self._patched_extra():
            extra_module.edit_push_order(
                self.order, operator=self.operator, title="新标题"
            )

        forms = self.fake.forms_for("LevelOrderModify")
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["Title"], "新标题")

    def test_accepted_order_cannot_be_edited(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.edit_push_order(
                    self.order, operator=self.operator, title="新标题"
                )
        self.assertIn("未接手", str(ctx.exception))
        self.assertNotIn("LevelOrderModify", self.fake.actions)

    def test_requirement_is_replaced_inside_actors_only(self):
        self.fake.detail = self._editable_detail()
        after = self._editable_detail()
        after["Require"] = "新要求"
        self.fake.next_details = [after]
        with self._patched_extra():
            extra_module.edit_push_order(
                self.order, operator=self.operator, requirement="新要求"
            )
        actors = self.fake.forms_for("LevelOrderModify")[0]["Actors"].split("|*|")
        self.assertEqual(actors[0], "acct0001", "前四段原样保留")
        self.assertEqual(actors[4], "新要求")

    def test_title_readback_mismatch_is_not_success(self):
        self.fake.detail = self._editable_detail()
        self.fake.next_details = [self._editable_detail()]  # 回读仍是旧标题
        with self._patched_extra():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.edit_push_order(
                    self.order, operator=self.operator, title="新标题"
                )
        self.assertIn("没能核验到", str(ctx.exception))

    def test_empty_edit_is_a_chinese_error(self):
        with self.assertRaises(DailiantongPushError) as ctx:
            extra_module.edit_push_order(self.order, operator=self.operator)
        self.assertIn("没有任何要修改", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_the_api_route_works(self):
        self.fake.detail = self._editable_detail()
        after = self._editable_detail()
        after["Title"] = "新标题"
        self.fake.next_details = [after]
        client = self._api_client()
        with self._patched_extra():
            response = client.post(
                URL_EDIT, data=json.dumps({"title": "新标题"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)


class ScreenshotTests(_ExtraActionTestBase):
    def _patched_upload(self):
        from unittest import mock

        return mock.patch(
            "apps.business.dailiantong_extra_actions.put_object"
        )

    def test_image_is_uploaded_and_confirmed_by_progress_readback(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        with self._patched_extra(), self._patched_upload() as upload:
            result = extra_module.add_push_screenshot(
                self.order, operator=self.operator, data=PNG_BYTES
            )

        self.assertEqual(upload.call_count, 1)
        object_key = result["img"]
        self.assertIn("tt/", object_key)
        self.assertIn(ORDER_NO, object_key)
        self.assertIn("/shot-", object_key, "手工补图的对象名标签与接手后自动发的图分开")
        self.assertTrue(object_key.endswith(".png"))
        # 上传到 OSS 的字节与 Content-Type 来自魔数判图。
        args = upload.call_args.args
        self.assertEqual(args[2], PNG_BYTES)
        self.assertEqual(args[3], "image/png")

        add_forms = self.fake.forms_for("LevelOrderProgressAdd")
        self.assertEqual(len(add_forms), 1)
        self.assertEqual(add_forms[0]["Img"], object_key)
        # ⛔ 假成功：必须有进度回读。
        self.assertEqual(self.fake.actions.count("LevelOrderProgressList"), 1)
        self.assertTrue(self._audit_rows("order.dailiantong_progress").exists())

    def test_upload_price_does_not_hide_a_missing_readback(self):
        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        self.fake.progress_readback_includes_upload = False
        with self._patched_extra(), self._patched_upload():
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_screenshot(
                    self.order, operator=self.operator, data=PNG_BYTES
                )
        self.assertIn("没能核验", str(ctx.exception))

    def test_a_non_image_upload_is_rejected_before_any_upstream_call(self):
        with self.assertRaises(DailiantongPushError) as ctx:
            extra_module.add_push_screenshot(
                self.order, operator=self.operator, data=b"not-an-image"
            )
        self.assertIn("无法识别", str(ctx.exception))
        self.assertEqual(self.fake.calls, [])

    def test_terminal_order_gets_zero_writes(self):
        self.fake.detail = {"Status": 17, "CancelStatus": 10}
        with self._patched_extra(), self._patched_upload() as upload:
            with self.assertRaises(DailiantongPushError) as ctx:
                extra_module.add_push_screenshot(
                    self.order, operator=self.operator, data=PNG_BYTES
                )
        self.assertIn("已结算", str(ctx.exception))
        self.assertEqual(upload.call_count, 0, "终态单⛔ 不上传任何东西")

    def test_the_api_accepts_multipart_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.fake.detail = {"Status": 12, "CancelStatus": 10}
        client = self._api_client()
        upload = SimpleUploadedFile("shot.png", PNG_BYTES, content_type="image/png")
        with self._patched_extra(), self._patched_upload():
            response = client.post(
                URL_SCREENSHOT, data={"image": upload, "caption": "补一张图"}
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["code"], 0)
        self.assertIn("/shot-", payload["data"]["img"])
        self.assertEqual(payload["data"]["caption"], "补一张图")
