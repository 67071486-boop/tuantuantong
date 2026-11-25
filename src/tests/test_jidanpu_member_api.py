"""「我的资料 → 功能设置」里的**下家鸡蛋铺绑定**三条端点。被钉住的契约（spec §8.4 / §8.5 / §9.1 / §9.2）：`GET /api/business/member/jidanpu-binding/` 只读字段 + 健康状态、⛔ 绝不回显密码、没绑定时回 `data: null` 的信封（⛔ 不是 404）；`PUT` 同一路径只写 `request.user` 那一行（新建或覆盖），`enabled=True` ⇒ 同步跑一次绑定检测、不过 ⇒ 不落库并点名报缺项，改了 `account` / `jidanpu_base_url` ⇒ 把在飞的台账事件收敛成终态 `skipped`（人话原因、⛔ 不再重试；控制器裁定 7）、链路行保留；`POST …/test-connection/` 跑检测、把人话结果回给页面、⛔ 不落库。
⚠️ 判据打**数据库行**而不是状态码（`AGENTS.md` §5-G）：「B 读不到 / 改不到 A」断言的是 A 那一行仍然原样、以及 B 那行确实归 B，不是「返回了 403」。密码那条是搜**响应字节**的 canary 用例（照 `test_jidanpu_test_connection.py` 的形状）。检测用桩换掉 `jidanpu_member.detect_binding_with_login_identity`（**被查找的那个模块**，E-0029）：本模块只验「端点怎么用检测结论」，检测本身的三条判据由 `test_jidanpu_member_detection.py` 钉、身份那一跳由 `test_jidanpu_member_identity_heal.py` 钉。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `member-a`、密码 `pw-canary-a`；⛔ 零真实网络。"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_member, jidanpu_member_openapi_outbox, jidanpu_openapi
from apps.business.api import jidanpu_member_views
from apps.business.models import (
    Approval,
    ApprovalKind,
    ApprovalStatus,
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PenaltyTargetType,
    Performance,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client
from tests.test_jidanpu_member_consume import CLIENT, JID, _stub, _Upstream
from tests.test_operation_log_ledger import INSTRUMENTED
#: ⭐ **②式的假上游与密钥文件临时目录 helper 住在一个中立模块里**（⛔ 不再写第二个假上游：两份桩必然漂；与 `tests/test_jidanpu_member_openapi_consume.py` 复用 `tests.test_jidanpu_openapi._Upstream` 是**同一条纪律**）。
#: ⚠️ 这一整块住在中立的 `tests/_openapi_fixtures.py`、⛔ 不住在任何一个测试模块里：放进某个测试模块会让「谁清理那半边，本模块的测试就 `ImportError`」（⛔ 别再把它搬回任何测试模块）。
#: ⚠️ 密钥文件的默认路径是**生产形态**（`$R/shared/callback-keys.env`）⇒ 不指走的话，任何一次②式保存都会去写一个本机不存在的目录并**响亮失败**（那是设计：写不进就整体回滚）。
from tests._openapi_fixtures import (
    INNER_PERMISSION_MSG,
    OPENAPI_CLIENT_ID,
    OPENAPI_GAME_ROW,
    OPENAPI_SENTINEL_SECRET,
    OPENAPI_SENTINEL_TOKEN,
    OUTER_GATE_MSG,
    WRITE_ENDPOINT_NAMES,
    _OpenApiUpstream,
    _patch_openapi_upstream,
    _temp_callback_keys_file,
)

URL = "/api/business/member/jidanpu-binding/"
TEST_URL = "/api/business/member/jidanpu-binding/test-connection/"

BASE_URL = "https://jidanpu-a.test.invalid"
BASE_URL_B = "https://jidanpu-b.test.invalid"
ACCOUNT = "member-a"
ACCOUNT_B = "member-b"
PASSWORD = "pw-canary-a"
PASSWORD_B = "pw-canary-b"

#: 检测失败的人话（**字面量**，⛔ 不从被测模块的常量派生——自指断言不会红，E-0024）。
LOGIN_FAILURE = "登录失败：账号或密码错误"

#: 换绑时收敛在飞事件的人话原因必须点明「人工核对」（运维要能看懂发生了什么）。
REBIND_REASON_HINT = "人工核对"

#: 审计句子里显示的站点名（去协议头、去尾斜杠）——⛔ 不是账号、更不是密码。
HOST_NAME = "jidanpu-a.test.invalid"

# ── ⭐ 手动推送端点（计划 §2）──────────────────────────────────────────
PUSH_URL = "/api/business/member/jidanpu-push-now/"
#: 端点的节点门 = 「我的订单」那颗（⛔ 不新增节点）。
PUSH_NODE = "my_orders.send_to_my_system"
#: 夹具里两张单：`PUSH_ORDER_NO` 是他接的，`PUSH_OTHER_ORDER_NO` 是别人接的。
PUSH_ORDER_NO = "20261023000000009001"
PUSH_OTHER_ORDER_NO = "20261023000000009002"
#: 「不是他当前接手的单」那句人话（计划 §2 第 2 条的**逐字**文案，⛔ 不从被测常量派生）。
PUSH_NOT_CLAIMED_LITERAL = "不是当前接手的单"
#: 串行闸被别人占着时那句人话（`jidanpu_member.OUTBOX_BUSY_REASON` 的**逐字**副本，E-0024）。
#: ⚠️ 措辞刻意**不诱导重点**：单子已排队、下一轮会自动推。
PUSH_BUSY_LITERAL = "已排队：另一轮推送正在跑，几秒后会自动推出去"
#: 没绑定时那句人话里必须出现的关键词（计划 §2 第 1 条：要告诉用户去哪绑）。
PUSH_NOT_BOUND_HINT = "我的资料 → 功能设置"
#: 没有链路时那句人话（`jidanpu_member.STATUS_NO_LINK_REASON` 的**逐字**副本，E-0024）。
PUSH_NO_LINK_LITERAL = "本线台账里没有这张单的链路，无法改对方状态"


def _detect(ok, failures=()):
    """把 `jidanpu_member.detect_binding_with_login_identity` 换成固定结论的桩。
    ⚠️⭐ 视图的检测与登录显示名解析走**同一条连** ⇒ 桩打在**被查找的那个名字**上（E-0029）；返回的第二个元素是登录显示名（`None` = 这一跳没解析出来）。
    本文件只验「端点怎么用检测结论」，身份那一跳由 `tests/test_jidanpu_member_identity_heal.py` 钉。"""
    return mock.patch.object(
        jidanpu_member,
        "detect_binding_with_login_identity",
        return_value=(
            jidanpu_member.DetectionReport(ok=ok, failures=tuple(failures)), None,
        ),
    )


class JidanpuMemberBindingApiTests(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(username=ACCOUNT)
        self.user = User.objects.get(username=ACCOUNT)

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _put(self, *, client=None, **overrides):
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": "测试账号甲",
            "password": PASSWORD,
            "source_name": "团团通系统",
            "enabled": False,
        }
        payload.update(overrides)
        return (client or self.client).put(
            URL, json.dumps(payload), content_type="application/json"
        )

    def _create_row(self, **overrides):
        """落一行属于**当前登录者**的绑定（默认走端点新建，断言它真的落了库）。"""
        res = self._put(**overrides)
        self.assertEqual(res.json()["code"], 0, res.content)
        return JidanpuMemberBinding.objects.get(user=self.user)

    def _other_client(self, username=ACCOUNT_B):
        return make_logged_in_client(username=username), User.objects.get(username=username)

    # ── 读：只读字段 + 健康状态，⛔ 不回显密码 ──────────────────────────────

    def test_unbound_get_returns_null_envelope_not_404(self):
        """没绑定时 `data: null`（**不是** 404）——「还没绑」是正常状态，不是错误。"""
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], 0)
        self.assertIsNone(body["data"])

    def test_get_returns_health_columns_and_no_password_key(self):
        self._create_row()
        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["jidanpu_base_url"], BASE_URL)
        self.assertEqual(data["account"], "测试账号甲")
        self.assertIs(data["enabled"], False)
        self.assertEqual(data["fast_poll_seconds"], 3)
        self.assertEqual(data["reconcile_seconds"], 180)
        self.assertIn("checked_at", data)
        self.assertIn("last_error", data)
        self.assertIs(data["password_set"], True)
        # 密码是 write_only ⇒ **键本身**都不进响应
        self.assertNotIn("password", data)

    def test_the_status_pull_interval_is_readable_and_writable(self):
        """⭐ 状态回拉的间隔：GET 回显 600、PUT 真的落库、漏传不打回默认值。
        ⛔ 弄红它：把 `status_pull_seconds` 从序列化器拿掉（页面上没这一格）或从 `_PUT_WRITE_FIELDS` 拿掉
        （校验通过、库里那一格却不会被写——本仓最恨的静默形态）；⚠️ 默认值 600 / 旧列 `reconcile_seconds` 180 不动：见 `tests/test_jidanpu_member_schedule.py` 那条默认值用例。"""
        self._create_row()
        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["status_pull_seconds"], 600)

        res = self._put(status_pull_seconds=900)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.status_pull_seconds, 900)

        #: 页面今天还没这一格 ⇒ 不带它的 PUT **不许**把库里的值打回默认（那只证明「能写」）。
        res = self._put()
        self.assertEqual(res.json()["code"], 0, res.content)
        row.refresh_from_db()
        self.assertEqual(row.status_pull_seconds, 900, "PUT 没带这一格却把它重置了")

        #: 越界值挡在序列化器层（下界 1），⛔ 不许落到 `int4` 列上变成 500（E-0010）。
        res = self._put(status_pull_seconds=0)
        self.assertNotEqual(res.json()["code"], 0, res.content)

    def test_password_never_appears_in_any_response_body(self):
        """密码 canary：搜**响应字节**，不是搜某个字段。"""
        create = self._put()
        self.assertNotIn(PASSWORD, create.content.decode(), create.content)
        read = self.client.get(URL)
        self.assertNotIn(PASSWORD, read.content.decode(), read.content)
        # 改密码那次也不回显新值
        update = self._put(password=PASSWORD_B)
        self.assertNotIn(PASSWORD_B, update.content.decode(), update.content)
        # 检测（不论过不过）也不带凭据
        with _detect(False, failures=(LOGIN_FAILURE,)):
            checked = self.client.post(TEST_URL)
        self.assertNotIn(PASSWORD_B, checked.content.decode(), checked.content)
        # 操作日志那一行同样不许出现密码
        for row in OperationLog.objects.all():
            self.assertNotIn(PASSWORD_B, row.summary)
            self.assertNotIn(PASSWORD_B, row.target_name or "")

    # ── 越权面 = 0：只作用于 `request.user` 那一行 ─────────────────────────

    def test_cannot_read_another_members_binding(self):
        """A 有绑定、B 没有 ⇒ B 读到的是 `null`，且 A 那一行**原样**（判据打在行上）。"""
        self._create_row()
        client_b, _user_b = self._other_client()
        body = client_b.get(URL).json()
        self.assertIsNone(
            body["data"], "B 读到了 A 的绑定行——读取没有按 request.user 收敛"
        )
        row_a = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row_a.account, "测试账号甲")
        self.assertEqual(row_a.password, PASSWORD)

    def test_cannot_touch_another_members_binding_even_with_a_forged_user_field(self):
        """B 的 PUT 带 `user=A` 也必须只落 B 自己那一行；A 的行一个字节都不许变。"""
        self._create_row()
        client_b, user_b = self._other_client()
        res = self._put(
            client=client_b,
            jidanpu_base_url=BASE_URL_B,
            account="测试账号乙",
            password=PASSWORD_B,
            user=self.user.pk,
        )
        self.assertEqual(res.json()["code"], 0, res.content)
        row_a = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row_a.account, "测试账号甲")
        self.assertEqual(row_a.password, PASSWORD)
        self.assertEqual(row_a.jidanpu_base_url, BASE_URL)
        row_b = JidanpuMemberBinding.objects.get(user=user_b)
        self.assertEqual(row_b.account, "测试账号乙")
        self.assertEqual(row_b.jidanpu_base_url, BASE_URL_B)
        self.assertEqual(JidanpuMemberBinding.objects.count(), 2)
        # B 的 GET 也只看得到自己那一行
        self.assertEqual(client_b.get(URL).json()["data"]["jidanpu_base_url"], BASE_URL_B)

    # ── 写：新建 / 覆盖 + 与业务写入同事务的操作日志 ────────────────────────

    def test_put_creates_my_row_and_logs_the_create_action(self):
        self._create_row()
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.user_id, self.user.pk)
        self.assertEqual(row.password, PASSWORD)
        self.assertEqual(row.source_name, "团团通系统")
        log = OperationLog.objects.get()
        self.assertEqual(log.action, "system.jidanpu_member_binding_create")
        self.assertEqual(log.actor, ACCOUNT)
        self.assertEqual(log.actor_id, self.user.pk)
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.summary, f"【member-a】新增代练员的下家鸡蛋铺绑定：{HOST_NAME}")

    def test_put_overwrites_the_same_row_and_blank_password_keeps_the_old_one(self):
        """换账号 = 覆盖同一行；密码留空 = 不改（页面回显不了密码，留空只能是「不动它」）。"""
        self._create_row()
        self._put(account="测试账号乙", password="")
        self.assertEqual(JidanpuMemberBinding.objects.count(), 1)
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account, "测试账号乙")
        self.assertEqual(row.password, PASSWORD)
        log = OperationLog.objects.order_by("id").last()
        self.assertEqual(log.action, "system.jidanpu_member_binding_update")
        self.assertEqual(log.summary, f"【member-a】编辑代练员的下家鸡蛋铺绑定：{HOST_NAME}")

    def test_the_throw_price_percent_boundary_is_pinned_on_both_sides(self):
        """⭐ 抛单百分比的**边界**（业务方定 1–100）：0 与 101 必须**被拒且点名到那一格**
        （0 会把一张免费单真送进对方大厅），1 与 100 必须**放行**——⛔ 只测 80 这种中间值
        抓不到「范围整个没生效」。"""
        self._create_row()
        for bad, want in ((0, "不小于 1"), (101, "不能大于 100")):
            with self.subTest(bad=bad):
                res = self._put(throw_price_percent=bad)
                self.assertNotEqual(res.json()["code"], 0)
                self.assertIn(want, res.content.decode())
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.throw_price_percent, 100)  # 被拒 ⇒ 一行都没改

        for good in (1, 100):
            with self.subTest(good=good):
                self._put(throw_price_percent=good)
                row.refresh_from_db()
                self.assertEqual(row.throw_price_percent, good)

    def test_hall_sync_enabled_defaults_true_and_roundtrips(self):
        """⭐「同步大厅订单」开关（D-0299）的**读写契约**：① 新建时缺键 ⇒ True（存量行为不变；「缺省 True」钉的是这一档）；
        ② PUT `False` ⇒ 落库 `False`、GET 回显 `False`；③ 再 PUT `True` ⇒ 翻回来（只写 False 的实现回不去）。
        判别力：字段漏出 `fields` / `_PUT_WRITE_FIELDS` ⇒ ②那一档落不了库，红。"""
        self._create_row()
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertIs(row.hall_sync_enabled, True)  # ① 缺键 = 模型默认 True
        self.assertIs(self.client.get(URL).json()["data"]["hall_sync_enabled"], True)

        self._put(hall_sync_enabled=False)          # ② 关
        row.refresh_from_db()
        self.assertIs(row.hall_sync_enabled, False)
        self.assertIs(self.client.get(URL).json()["data"]["hall_sync_enabled"], False)

        self._put(hall_sync_enabled=True)           # ③ 再开（开关是双向的）
        row.refresh_from_db()
        self.assertIs(row.hall_sync_enabled, True)

    def test_put_without_the_hall_sync_key_keeps_the_stored_value(self):
        """**更新**时不带键 ⇒ 保持库里那一格（⛔ 不是被默认值悄悄顶回 True）。
        ⚠️ 与上一条的「缺省 True」分得开：那是**新建**那一档；更新档「没带 = 不动它」，否则任何不带这一格的保存都会把用户关掉的开关静默打开。
        判别力：把字段写成缺键落默认值的显式声明 ⇒ 这里红。"""
        self._create_row(hall_sync_enabled=False)
        self._put(account="测试账号丁")  # 不带 hall_sync_enabled
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account, "测试账号丁")
        self.assertIs(row.hall_sync_enabled, False)

    def test_the_ledger_declares_the_update_action_the_view_actually_writes(self):
        """把「登记表声明」与「视图真写的两个动作」钉在一起（一个 PUT 两个分支两个动作）。"""
        ledger_action = INSTRUMENTED["apps/business/api/jidanpu_member_views.py"][
            ("put", "api/business/member/jidanpu-binding/")
        ]
        self.assertEqual(ledger_action, "system.jidanpu_member_binding_update")
        self._create_row()
        self.assertEqual(
            OperationLog.objects.order_by("id").last().action,
            "system.jidanpu_member_binding_create",
        )
        self._put(account="测试账号丙")
        self.assertEqual(OperationLog.objects.order_by("id").last().action, ledger_action)

    def test_blank_source_name_falls_back_to_the_model_default(self):
        self._create_row(source_name="   ")
        self.assertEqual(
            JidanpuMemberBinding.objects.get(user=self.user).source_name, "团团通系统"
        )

    def test_a_bad_address_is_rejected_and_nothing_is_written(self):
        res = self._put(jidanpu_base_url="no-scheme.test.invalid")
        self.assertEqual(res.json()["code"], 1000, res.content)
        self.assertEqual(JidanpuMemberBinding.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    # ── 启用的机械落实：检测不过 ⇒ 不落库；过 ⇒ 落库 + checked_at ──────────

    def test_enable_failing_detection_creates_nothing_and_names_the_problem(self):
        """新建 + `enabled=True` 而检测不过 ⇒ **一行都不许落**，文案点名到具体缺项。"""
        with _detect(False, failures=(LOGIN_FAILURE,)):
            res = self._put(enabled=True)
        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn(LOGIN_FAILURE, body["msg"])
        self.assertEqual(JidanpuMemberBinding.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_enable_failing_detection_leaves_the_existing_row_untouched(self):
        """已有行 + 检测不过 ⇒ 覆盖**不发生**：账号、开关、`checked_at` 全是旧值。"""
        self._create_row()
        with _detect(False, failures=(LOGIN_FAILURE,)):
            res = self._put(enabled=True, account="测试账号乙")
        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn(LOGIN_FAILURE, body["msg"])
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account, "测试账号甲")
        self.assertEqual(row.jidanpu_base_url, BASE_URL)
        self.assertIs(row.enabled, False)
        self.assertIsNone(row.checked_at)

    def test_enable_passing_detection_persists_and_stamps_checked_at(self):
        """检测过 ⇒ 落库、开关真打开、`checked_at` 写当前时刻；检测读的是**这一行**。"""
        with _detect(True) as detect:
            res = self._put(enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertIs(row.enabled, True)
        self.assertIsNotNone(row.checked_at)
        self.assertEqual(row.password, PASSWORD)
        # 检测必须拿到「落库后会是什么」的那一行，且它是本人那一行
        checked = detect.call_args.args[0]
        self.assertIsInstance(checked, JidanpuMemberBinding)
        self.assertEqual(checked.user_id, self.user.pk)
        self.assertEqual(checked.jidanpu_base_url, BASE_URL)
        self.assertEqual(checked.account, "测试账号甲")
        self.assertEqual(
            parse_datetime(res.json()["data"]["checked_at"]), row.checked_at
        )

    def test_enable_stores_the_login_display_name_from_the_same_connection(self):
        """⭐ PUT 启用时把**同一连查到的登录显示名**落库（引擎列的缓存）。
        失败方向：视图不把 `login_name` 写回 `account_name` ⇒ 本用例红。"""
        with mock.patch.object(
            jidanpu_member,
            "detect_binding_with_login_identity",
            return_value=(jidanpu_member.DetectionReport(ok=True), "测试同步账号甲"),
        ):
            res = self._put(enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account_name, "测试同步账号甲")
        # 响应里也是新名字（序列化器读的是内存对象；不同步内存就还是旧值）
        self.assertEqual(res.json()["data"]["account_name"], "测试同步账号甲")

    def test_enable_without_a_resolved_name_keeps_the_column_untouched(self):
        """⭐ 对照：身份那一跳没解析出来（`login_name is None`）⇒ **不改** `account_name`、
        ⛔ 不报错、⛔ 不落空串（E-0169：空值不冒充真值）。"""
        self._create_row()  # 先有一行（account_name 默认空串）
        JidanpuMemberBinding.objects.filter(user=self.user).update(
            account_name="页面原先填的名字"
        )
        with _detect(True):
            res = self._put(enabled=True, account="测试账号乙")
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account_name, "页面原先填的名字")

    def test_test_connection_stores_the_login_display_name(self):
        """⭐「测试连接」动作把登录显示名落库（页面显示的才是真的）。
        失败方向：动作不写 `.update(account_name=…)` ⇒ 本用例红。"""
        self._create_row()
        with mock.patch.object(
            jidanpu_member,
            "detect_binding_with_login_identity",
            return_value=(jidanpu_member.DetectionReport(ok=True), "测试同步账号甲"),
        ):
            res = self.client.post(TEST_URL)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertTrue(res.json()["data"]["ok"])
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account_name, "测试同步账号甲")

    def test_test_connection_without_a_resolved_name_writes_nothing(self):
        """⭐ 对照：测试连接那一跳没解析出显示名 ⇒ **不写**、`code` 照旧 0（失败不报错）。"""
        self._create_row()
        JidanpuMemberBinding.objects.filter(user=self.user).update(
            account_name="页面原先填的名字"
        )
        with _detect(True):
            res = self.client.post(TEST_URL)
        self.assertEqual(res.json()["code"], 0, res.content)
        row = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row.account_name, "页面原先填的名字")

    def test_enable_returns_the_reconcile_summary_with_the_skip_reason(self):
        """⭐ 补推那一跳的返回值**不许丢**——同址重叠被跳过的单，必须在响应的 `data.reconcile` 里看得见
        （计数 + 人话原因）。判别力：把 `reconcile_and_queue(...)` 的返回值丢回原地（不放进响应）
        ⇒ `body["data"]["reconcile"]` 直接 KeyError ⇒ 本用例红。"""
        game = Game.objects.create(name="合成游戏甲")
        grant_games(self.user, game)          # 当前登录者（setUp 里还没有游戏）
        earlier_client, earlier_user = self._other_client()
        grant_games(earlier_user, game)
        JidanpuMemberBinding.objects.create(
            user=earlier_user,
            jidanpu_base_url=BASE_URL,        # 同址 ⇒ 归属给更早建的那一份
            account="acc-earlier",
            password=PASSWORD_B,
            source_name="团团通系统",
            enabled=True,
        )
        hall = Order.objects.create(
            game=game,
            order_no="20260926000000005001",
            status="WAITING_GRAB",
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目甲",
        )

        with _detect(True):
            res = self._put(enabled=True, account="测试账号甲")

        body = res.json()
        self.assertEqual(body["code"], 0, res.content)
        reconcile = body["data"]["reconcile"]
        self.assertEqual(reconcile["launch"], 0)
        self.assertEqual(reconcile["skipped_count"], 1)
        self.assertIn("跳过", reconcile["skipped"][0])
        # 被跳过的那一份**没有**排 launch（不是「报了跳过、其实也推了」）
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="member.launch", target_ref=hall.order_no
            ).exists()
        )

    def test_put_does_not_clobber_health_columns_written_concurrently(self):
        """⭐ lost update：PUT 的落库只写**配置类列**。`enabled=True` 的检测是一次网络往返，
        期间同步引擎完全可能写 `last_*` 健康列；整行 `save()` 会把它们覆盖回这次请求开始时的旧值
        （这里是 `None` ⇒ 页面上的「上次成功」被静默抹掉且无报错）；失败方向：改回整行 `save()` ⇒ 本用例红。"""
        binding = self._create_row()
        self.assertIsNone(binding.last_ok_at)  # 构造物：旧值是 None（被覆盖了才看得出来）
        stamped = timezone.now()

        def detect_and_stamp(*args, **kwargs):
            # 模拟并发的同步引擎：在检测那次网络往返里写健康列。
            JidanpuMemberBinding.objects.filter(pk=binding.pk).update(last_ok_at=stamped)
            return jidanpu_member.DetectionReport(ok=True), None

        with mock.patch.object(
            jidanpu_member,
            "detect_binding_with_login_identity",
            side_effect=detect_and_stamp,
        ):
            res = self._put(account="测试账号乙", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)

        row = JidanpuMemberBinding.objects.get(pk=binding.pk)
        self.assertEqual(row.account, "测试账号乙")  # 配置确实落库了
        self.assertIs(row.enabled, True)
        self.assertEqual(row.last_ok_at, stamped)  # 并发写的健康列**没被覆盖**

    def test_enable_without_credentials_never_calls_the_detector(self):
        """地址 / 账号 / 密码不全 ⇒ 直接回「还没配置」，⛔ 不去打上游。"""
        with _detect(True) as detect:
            res = self._put(enabled=True, password="")
        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("还没配置", body["msg"])
        self.assertFalse(detect.called, "凭据不全时不得调用检测")
        self.assertEqual(JidanpuMemberBinding.objects.count(), 0)

    # ── 换绑处置：在飞事件收敛成 skipped、链路行保留 ──────────────────────

    def test_rebinding_converges_in_flight_events_and_keeps_the_link_row(self):
        """换账号 ⇒ pending / failed 的事件收敛成**终态 `skipped`**（作废、⛔ 不再重试）+ 人话原因；链路行**保留**。
        ⚠️ 终态必须是 `skipped` 而不是 `failed`（控制器裁定 7）：`failed` 会继续重试——绑定再被启用时就会拿**新凭据**去改旧单（spec §8.4 明令禁止），而且那条 `failed` 行还会挡住同键的新触发。
        跨任务的那条端到端判据在 `tests/test_jidanpu_member_consume.py::test_rebinding_then_reenable_never_retries`。"""
        binding = self._create_row()
        binding.checked_at = timezone.now()
        binding.save(update_fields=["checked_at"])
        mine = dict(
            direction="out",
            kind="member.launch",
            target_type="order",
            target_ref="20260926000000001901",
            payload={"member_binding_id": binding.pk},
        )
        pending = JidanpuSyncEvent.objects.create(status="pending", **mine)
        failed = JidanpuSyncEvent.objects.create(status="failed", **mine)
        done = JidanpuSyncEvent.objects.create(status="done", **mine)
        other_binding = JidanpuSyncEvent.objects.create(
            status="pending", **{**mine, "payload": {"member_binding_id": binding.pk + 999}}
        )
        game = Game.objects.create(name="合成游戏甲")
        order = Order.objects.create(game=game, order_no="20260926000000001901")
        link = JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id="JDP-合成-1"
        )

        self._put(account="测试账号乙", password="")

        pending.refresh_from_db()
        failed.refresh_from_db()
        done.refresh_from_db()
        other_binding.refresh_from_db()
        self.assertEqual(pending.status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(failed.status, JidanpuSyncEvent.Status.SKIPPED)
        # ⛔ 别削弱断言：终态（= skipped，上面那条已钉死）、人话原因、链路行仍在，三样都要钉住。
        self.assertNotEqual(pending.reason, "")
        self.assertIn(REBIND_REASON_HINT, pending.reason)
        self.assertEqual(failed.reason, pending.reason)
        self.assertEqual(done.status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(other_binding.status, JidanpuSyncEvent.Status.PENDING)
        self.assertTrue(
            JidanpuMemberOrderLink.all_objects.filter(pk=link.pk).exists(),
            "换绑不得删除链路行（它是历史事实）",
        )
        binding.refresh_from_db()
        self.assertIsNone(
            binding.checked_at, "换了凭据之后，上一份凭据的检测结论不再成立"
        )

    def test_same_order_and_remote_number_can_be_reused_after_rebinding(self):
        binding = self._create_row()
        game = Game.objects.create(name="合成历史链接游戏")
        order = Order.objects.create(game=game, order_no="20261119000000000102")
        old = JidanpuMemberOrderLink.objects.create(binding=binding, order=order, jidanpu_order_id="SYNTHETIC-ID")
        self.assertEqual(self._put(jidanpu_base_url="https://another-shop.test.invalid").json()["code"], 0)
        current = JidanpuMemberOrderLink.objects.create(binding=binding, order=order, jidanpu_order_id="SYNTHETIC-ID")
        self.assertEqual(list(order.jidanpu_member_links.values_list("pk", flat=True)), [current.pk])
        self.assertEqual(JidanpuMemberOrderLink.all_objects.filter(binding=binding, order=order).count(), 2)
        old.refresh_from_db()
        self.assertIsNotNone(old.retired_at)

    def test_rebinding_isolates_old_links_from_new_status_events(self):
        binding = self._create_row()
        game = Game.objects.create(name="合成换店游戏")
        order = Order.objects.create(game=game, order_no="20261119000000000101")
        link = JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id="SYNTHETIC-OLD-SHOP",
        )
        response = self._put(jidanpu_base_url="https://new-shop.test.invalid")
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(jidanpu_member.queue_status_out(order), 0)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(pk=link.pk).exists())
        self.assertTrue(JidanpuMemberOrderLink.all_objects.filter(pk=link.pk).exists())

    def test_changing_only_the_source_name_is_not_a_rebinding(self):
        """只改来源名不是换绑 ⇒ 不动在飞事件（那条链路仍归这份绑定）。"""
        binding = self._create_row()
        event = JidanpuSyncEvent.objects.create(
            direction="out",
            kind="member.launch",
            target_type="order",
            target_ref="20260926000000001901",
            status="pending",
            payload={"member_binding_id": binding.pk},
        )
        self._put(source_name="合成来源名乙", password="")
        event.refresh_from_db()
        self.assertEqual(event.status, JidanpuSyncEvent.Status.PENDING)
        self.assertEqual(event.reason, "")

    # ── 测试连接：回人话、⛔ 不落库 ────────────────────────────────────────

    def test_test_connection_reports_failures_without_writing_anything(self):
        binding = self._create_row()
        before = (binding.checked_at, binding.last_error, binding.enabled, binding.updated_at)
        with _detect(False, failures=(LOGIN_FAILURE,)):
            body = self.client.post(TEST_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["ok"], False)
        self.assertEqual(body["data"]["messages"], [LOGIN_FAILURE])
        binding.refresh_from_db()
        self.assertEqual(
            (binding.checked_at, binding.last_error, binding.enabled, binding.updated_at),
            before,
            "测试连接是只读的：一行都不许写",
        )
        self.assertEqual(JidanpuMemberBinding.objects.count(), 1)

    def test_test_connection_reports_success_with_no_messages(self):
        self._create_row()
        with _detect(True):
            body = self.client.post(TEST_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["ok"], True)
        self.assertEqual(body["data"]["messages"], [])

    def test_test_connection_without_a_binding_is_a_named_failure(self):
        body = self.client.post(TEST_URL).json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("还没配置", body["msg"])
        self.assertEqual(JidanpuMemberBinding.objects.count(), 0)

    def test_test_connection_without_credentials_says_not_configured(self):
        """存量行凭据不全（如 shell 直接写库）⇒ 说「还没配置」，⛔ 不拿它去打上游。"""
        JidanpuMemberBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="测试账号甲", password="",
        )
        with _detect(True) as detect:
            body = self.client.post(TEST_URL).json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("还没配置", body["msg"])
        self.assertFalse(detect.called)

    def test_test_connection_only_ever_reads_my_own_row(self):
        """B 点测试连接 ⇒ 检测读的必须是 B 自己那行；A 那行一个字节都不变。"""
        self._create_row()
        client_b, user_b = self._other_client()
        with _detect(False, failures=(LOGIN_FAILURE,)) as detect:
            client_b.post(TEST_URL)
        self.assertFalse(detect.called, "B 还没有绑定 ⇒ 不该拿 A 的行去检测")
        row_a = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(row_a.account, "测试账号甲")
        self.assertFalse(JidanpuMemberBinding.objects.filter(user=user_b).exists())


# ══ ⑤b 缓存的「下家员工 uid」列（业务方选 C）═════════════════════════
# `input_order_kefu` 是**从上游读回来**的（我们那个下家账号在对方店铺里的员工 uid），⛔ 不是用户填的
# ⇒ 请求体伪造它必须被静默忽略；换绑（改账号 / 改地址）时它必须**一起清掉**（`AGENTS.md` §5-E / E-0027：给字段加写入点必须回查回退 / 清理路径）。


class InputOrderKefuColumnTests(TenantTestCase):
    """⚠️ 独立成类（⛔ **不**继承 `JidanpuMemberBindingApiTests`）：继承会把那 24 条基础用例
    再跑一遍（3 个类 = 同一个断言跑 3 次），既拖时间又让「Ran N」这个读数失真。"""

    def setUp(self):
        self.client = make_logged_in_client(username=ACCOUNT)
        self.user = User.objects.get(username=ACCOUNT)

    def _put(self, **overrides):
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": "测试账号甲",
            "password": PASSWORD,
            "source_name": "团团通系统",
            "enabled": False,
        }
        payload.update(overrides)
        return self.client.put(
            URL, json.dumps(payload), content_type="application/json"
        )

    def _create_row(self):
        res = self._put()
        self.assertEqual(res.json()["code"], 0, res.content)
        return JidanpuMemberBinding.objects.get(user=self.user)

    def _stamp(self, binding, value="70001"):
        JidanpuMemberBinding.objects.filter(pk=binding.pk).update(input_order_kefu=value)
        binding.refresh_from_db()
        return binding

    def test_a_rebinding_clears_the_cached_downstream_staff_uid(self):
        """换了账号 / 地址 ⇒ 缓存的那个 uid 不再成立，必须清掉（⛔ 不拿旧 uid 去填新账号的单）。"""
        binding = self._stamp(self._create_row())

        self._put(account="测试账号乙", password="")

        self.assertEqual(
            JidanpuMemberBinding.objects.get(pk=binding.pk).input_order_kefu, ""
        )

    def test_a_save_that_is_not_a_rebinding_keeps_the_cached_uid(self):
        """反向控制：只改来源名**不是**换绑 ⇒ ⛔ 不许顺手清掉（那会让每按一次保存就重取一次）。"""
        binding = self._stamp(self._create_row())

        self._put(source_name="合成来源名乙", password="")

        self.assertEqual(
            JidanpuMemberBinding.objects.get(pk=binding.pk).input_order_kefu, "70001"
        )

    def test_a_request_body_cannot_forge_the_cached_uid(self):
        """它是上游读回来的只读列：请求体里塞它会被静默忽略，读端点能看到它。"""
        binding = self._create_row()

        self._put(input_order_kefu="99999", password="")

        self.assertEqual(
            JidanpuMemberBinding.objects.get(pk=binding.pk).input_order_kefu, ""
        )
        self.assertIn("input_order_kefu", self.client.get(URL).json()["data"])


# ══ ① 绑定冲突：中文人话 + **点名占用者**（业务方）═══════════════════
# 业务方报的原文是数据库那句生硬的唯一约束信息 ⇒ 入口处先查再报，用人话点名**是谁占着这个账号**。
# ⚠️ 判据三条一起钉：① 文案逐字；② 点名了占用者的**显示名**；③ 一行都没落库；数据库那道唯一约束**不放宽**（它是并发下的最后一道闸）。


class BindingConflictTests(TenantTestCase):
    """同一「地址 + 账号」被别的账号绑过 ⇒ 中文人话 + 点名；自己保存自己不算冲突。"""

    def setUp(self):
        self.client = make_logged_in_client(username=ACCOUNT)
        self.user = User.objects.get(username=ACCOUNT)
        self.other_client, self.other_user = (
            make_logged_in_client(username=ACCOUNT_B),
            User.objects.get(username=ACCOUNT_B),
        )

    def _put(self, *, client, base_url=BASE_URL, account, **overrides):
        payload = {
            "jidanpu_base_url": base_url,
            "account": account,
            "password": PASSWORD,
            "source_name": "团团通系统",
            "enabled": False,
        }
        payload.update(overrides)
        return client.put(URL, json.dumps(payload), content_type="application/json")

    def _occupy(self, *, owner, base_url=BASE_URL, account="测试账号甲", enabled=False):
        return JidanpuMemberBinding.objects.create(
            user=owner,
            jidanpu_base_url=base_url,
            account=account,
            password=PASSWORD_B,
            source_name="团团通系统",
            enabled=enabled,
        )

    def test_binding_someone_elses_address_and_account_is_named_in_chinese(self):
        """占用时报**中文人话**，且点名**那个占用者的显示名**；一行都不落库。"""
        owner = User.objects.create_user(
            username="member-owner-a", password="pw", first_name="测试客服甲"
        )
        self._occupy(owner=owner)

        res = self._put(client=self.other_client, account="测试账号甲")

        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        # ⚠️ 逐字文案（⛔ 不从被测模块派生常量：自指断言不会红，E-0024）
        self.assertEqual(
            body["msg"], "该鸡蛋铺账号已被 测试客服甲 绑定，如需换绑请先解除绑定"
        )
        self.assertFalse(
            JidanpuMemberBinding.objects.filter(user=self.other_user).exists(),
            "冲突被拒 ⇒ B 一行都不许落",
        )
        row = JidanpuMemberBinding.objects.get(user=owner)
        self.assertEqual(row.account, "测试账号甲")

    def test_the_occupier_without_a_display_name_is_named_by_username(self):
        """占用者没有显示名（`first_name` 空）⇒ 用他的**用户名**点名（⛔ 不留一句无名的话）。"""
        owner = User.objects.create_user(username="member-owner-b", password="pw")
        self._occupy(owner=owner)

        body = self._put(client=self.other_client, account="测试账号甲").json()

        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("member-owner-b", body["msg"])

    def test_a_disabled_binding_still_occupies_the_account(self):
        """**已停用**的绑定也算占用（照本线「地址级去重含已停用」的既有口径）。"""
        owner = User.objects.create_user(
            username="member-owner-c", password="pw", first_name="测试客服乙"
        )
        self._occupy(owner=owner, enabled=False)

        body = self._put(client=self.other_client, account="测试账号甲").json()

        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("测试客服乙", body["msg"])

    def test_saving_my_own_row_again_is_not_a_conflict(self):
        """⚠️ 自己保存自己那一行**不能**报冲突（PUT 是覆盖写，页面总会带上原来的值）。"""
        self._occupy(owner=self.user)
        row = JidanpuMemberBinding.objects.get(user=self.user)

        res = self._put(client=self.client, account="测试账号甲", password="")

        self.assertEqual(res.json()["code"], 0, res.content)
        row.refresh_from_db()
        self.assertEqual(row.account, "测试账号甲")
        self.assertEqual(JidanpuMemberBinding.objects.count(), 1)

    def test_a_different_address_or_account_does_not_conflict(self):
        """别的账号 / 别的地址不受影响：同地址换账号、同账号换地址都要能落库。"""
        owner = User.objects.create_user(
            username="member-owner-d", password="pw", first_name="测试客服丙"
        )
        self._occupy(owner=owner)

        same_address_other_account = self._put(
            client=self.other_client, account="测试账号乙"
        )
        self.assertEqual(same_address_other_account.json()["code"], 0,
                         same_address_other_account.content)

        third_client, third_user = (
            make_logged_in_client(username="member-third"),
            User.objects.get(username="member-third"),
        )
        other_address_same_account = self._put(
            client=third_client, base_url=BASE_URL_B, account="测试账号甲"
        )
        self.assertEqual(other_address_same_account.json()["code"], 0,
                         other_address_same_account.content)
        self.assertTrue(JidanpuMemberBinding.objects.filter(user=third_user).exists())


# ══ ⑥ ②式（官方 Open Platform API）：写入口 / 读响应 / 密钥文件 ═══════════════════
# 规格：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §2.1（`mode` 是唯一判据）/ §4.2–§4.3（列与不变式）/ §5.3（0600 密钥文件）/ §9.4（健康三问）/ §11.2–§11.5（界面与逐字文案）/ §13.3-C3/C8/C10。
# ⚠️ 形状与**上家线**（`tests/test_jidanpu_upstream_api.py` 的 `JidanpuUpstreamOpenApiBindingTests`）逐条同形，⛔ 不另发明一套；每条判据都写清「怎么弄红它」——弄不红的判据等于没写。

#: ②式那一路的合成夹具（⛔ 一个真值都没有）。
MEMBER_OPENAPI_BASE_URL = "https://jidanpu-member-openapi-api.test.invalid"
MEMBER_OPENAPI_USERNAME = "member-openapi-t"
#: ⭐ **哨兵密钥**：只允许出现在库里（响应 / 逐条表 / 文案里都不许有它）。
MEMBER_OPENAPI_SECRET = "sentinel-secret-must-not-leak-member-0001"


class JidanpuMemberOpenApiBindingTests(TenantTestCase):
    """①式②式的**写入口**与**读响应**（spec §2.1 / §4.3 / §11.2 / §11.3 / §9.4）。"""

    def setUp(self):
        #: ②式绑定保存时会**真的写**那份 0600「码 → 密钥」文件 ⇒ 指到临时目录。
        self._keys_file = _temp_callback_keys_file(self)
        #: 启用前那次探测会真的打只读端点 ⇒ 换成**零网络**的假上游（只换 socket 那一层）。
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.upstream = _OpenApiUpstream()
        upstream_patcher = _patch_openapi_upstream(self.upstream)
        upstream_patcher.start()
        self.addCleanup(upstream_patcher.stop)
        self.client = make_logged_in_client(username=MEMBER_OPENAPI_USERNAME)
        self.user = User.objects.get(username=MEMBER_OPENAPI_USERNAME)

    def _put(self, *, client=None, **overrides):
        payload = {
            "jidanpu_base_url": MEMBER_OPENAPI_BASE_URL,
            "account": MEMBER_OPENAPI_USERNAME,
            "password": "pw-canary-member-openapi",
            "enabled": False,
        }
        payload.update(overrides)
        return (client or self.client).put(
            URL, json.dumps(payload), content_type="application/json"
        )

    def _create_openapi_row(self, **overrides):
        """走**端点**建一条②式绑定（判据要打在「调用方真正能做的事」上）。
        ⚠️ 刻意**不带** `account` / `password`（②式的界面把这两格藏起来了，spec §11.2）⇒ 这一条同时钉住「②式不需要账号密码也能建出来」。"""
        fields = {
            "mode": "openapi",
            "client_id": OPENAPI_CLIENT_ID,
            "client_secret": MEMBER_OPENAPI_SECRET,
            "account": "",
            "password": "",
        }
        fields.update(overrides)
        res = self._put(**fields)
        self.assertEqual(res.json()["code"], 0, res.content)
        return JidanpuMemberBinding.objects.get(user=self.user)

    # ── 写入口：真能建出②式绑定 ─────────────────────────────────────────────

    def test_changing_openapi_client_id_isolates_previous_order_links(self):
        binding = self._create_openapi_row()
        game = Game.objects.create(name="合成接口换绑游戏")
        order = Order.objects.create(game=game, order_no="20261119000000000103")
        old = JidanpuMemberOrderLink.objects.create(binding=binding, order=order, jidanpu_order_id="SYNTHETIC-API-OLD")
        response = self._put(
            mode="openapi", account="", password="", client_id="synthetic-new-client",
            client_secret=MEMBER_OPENAPI_SECRET,
        )
        self.assertEqual(response.json()["code"], 0)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(pk=old.pk).exists())
        self.assertTrue(JidanpuMemberOrderLink.all_objects.filter(pk=old.pk).exists())

    def test_an_openapi_binding_can_be_created_through_the_endpoint(self):
        """⭐ 界面上提交 `mode` / `client_id` / `client_secret` 要真的落库。
        ⛔ 弄红它：把这三个字段从序列化器拿掉，或把 `mode` / `client_id` / `client_secret` 从 `_PUT_WRITE_FIELDS` 拿掉（校验通过、库里那一格却不会被写）⇒ 本用例红。"""
        binding = self._create_openapi_row()
        self.assertEqual(binding.mode, "openapi")
        self.assertEqual(binding.client_id, OPENAPI_CLIENT_ID)
        self.assertEqual(binding.client_secret, MEMBER_OPENAPI_SECRET)
        self.assertEqual((binding.account, binding.password), ("", ""))
        #: 码由模型层自动生成（②式才有意义）：没有它拼不出回调地址。
        self.assertTrue(binding.callback_code, "②式绑定必须有回调码（保存时自动生成）")

    def test_switching_back_to_password_mode_is_persisted(self):
        """⭐ **②→① 的切换必须落库**（`mode` 是唯一判据 ⇒ 它没写进去 = 用户以为切了、其实没切）。
        ⛔ 弄红它：把 `"mode"` 从 `_PUT_WRITE_FIELDS` 拿掉 ⇒ PUT 回 `code 0`，而库里的 `mode` 还是 `openapi`（**静默**：页面看起来切成功了，下一次同步仍走官方 API）。"""
        self._create_openapi_row()
        res = self._put(mode="password", account=MEMBER_OPENAPI_USERNAME,
                        password="pw-canary-member-openapi", client_id="", client_secret="")
        self.assertEqual(res.json()["code"], 0, res.content)
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(binding.mode, "password", "切回①式没有落库（那一格被静默丢掉了）")
        self.assertEqual(res.json()["data"]["mode"], "password")

    def test_the_client_id_can_be_changed_and_an_empty_one_keeps_the_stored_value(self):
        """`client_id` 可读可写；**空串 = 不修改**（照 `password` 的既有口径）。⛔ 弄红它：把空串当成「清掉」
        （`pending_instance` 里只放行 `password` 那一格）⇒ 第三次 PUT 之后 `client_id` 变空串。
        ⚠️ 每次都把 `client_secret` 一起带上（值不变）：不带的话，变异版会在**同一笔请求**里把密钥也清成空串 ⇒ 被 `validate()` 的「②式密钥必填」挡在进库之前（回 `1000`）⇒ `client_id` 那一格根本没被写 ⇒ 本用例看不出那一处变异（实测踩到）；带上密钥之后，「空串 = 清掉」这条变异无处可躲。"""
        self._create_openapi_row()
        self._put(mode="openapi", client_id="cid-synthetic-member-0002",
                  client_secret=MEMBER_OPENAPI_SECRET)

        res = self._put(mode="openapi", client_id="", client_secret=MEMBER_OPENAPI_SECRET)
        self.assertEqual(res.json()["code"], 0, res.content)
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(binding.client_id, "cid-synthetic-member-0002",
                         "空串应当表示「不改」，不是「清掉」")
        self.assertEqual(binding.client_secret, MEMBER_OPENAPI_SECRET)

    # ── ⛔ 密钥绝不出现在任何响应里（C3）────────────────────────────────────

    def test_the_secret_never_appears_in_any_response_and_has_no_key_at_all(self):
        """⭐ **C3**：`client_secret` 连**键**都不出现在响应里，只回 `client_secret_set` 布尔。
        判据两条（各挡一种形态）：① **值**：拿哨兵搜**响应字节**（不是「看起来没有」）；② **键**：`"client_secret" not in data`（⚠️ 是**字典键**判据——写成子串判据会被 `client_secret_set` 自己凑成假红）。
        ⛔ 弄红它：把 `client_secret` 从 `write_only` 里移出来 ⇒ 两条都红。"""
        created = self._create_openapi_row()
        create = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID,
                           client_secret=MEMBER_OPENAPI_SECRET, account="", password="")
        read = self.client.get(URL)
        update = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID,
                           client_secret="", account="", password="")
        for label, res in (("create", create), ("read", read), ("update", update)):
            with self.subTest(where=label):
                raw = res.content.decode("utf-8")
                self.assertNotIn(MEMBER_OPENAPI_SECRET, raw, "响应里出现了密钥值")
                data = res.json()["data"]
                self.assertNotIn("client_secret", data, "响应里出现了 client_secret 这个**键**")
                self.assertNotIn("password", data)
                self.assertIs(data["client_secret_set"], True)
                self.assertIs(data["password_set"], False)   # ②式：密码那格是空的
        #: 正向哨兵：库里**真的**有那把密钥（否则上面那些否定断言在「什么都没存」时也成立）
        created.refresh_from_db()
        self.assertEqual(created.client_secret, MEMBER_OPENAPI_SECRET)
        #: 操作日志那一行也不许出现密钥（审计句子只说站点地址）
        for row in OperationLog.objects.all():
            self.assertNotIn(MEMBER_OPENAPI_SECRET, row.summary)
            self.assertNotIn(MEMBER_OPENAPI_SECRET, row.target_name or "")

    def test_the_stored_secret_survives_a_put_that_does_not_carry_it(self):
        """⭐ 空串 = **不修改**（`client_secret` 只写不读 ⇒ 页面上根本回显不出它）。
        ⛔ 弄红它：写成「空就清掉」⇒ 库里那把密钥被抹掉，而回调从此**必然验签失败**（静默）。"""
        self._create_openapi_row()
        self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID, client_secret="",
                  account="", password="")
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(binding.client_secret, MEMBER_OPENAPI_SECRET)
        self.assertIs(self.client.get(URL).json()["data"]["client_secret_set"], True)

    # ── C2：②式必填 / ①式不判 ──────────────────────────────────────────────

    def test_openapi_without_a_client_id_is_rejected_and_names_the_field(self):
        """**C2 前半**：`mode="openapi"` 且 `client_id` 空 ⇒ 拒绝，且文案点名到那一格。
        ⛔ 弄红它：把序列化器的 `validate()` 里那一段删掉 ⇒ 回 `code 0` 且落一行「②式但没 client_id」的绑定。"""
        res = self._put(mode="openapi", client_id="", client_secret=MEMBER_OPENAPI_SECRET,
                        account="", password="")
        body = res.json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, res.content)
        self.assertIn("client_id", body["msg"])
        self.assertFalse(JidanpuMemberBinding.objects.exists(), "校验不过 ⇒ 一行都不落")

    def test_openapi_without_a_secret_is_rejected_with_the_verbatim_message(self):
        """②式且**从没存过**密钥 ⇒ 拒绝（spec §11.5 逐字）。
        ⚠️ 与「更新时允许空」并不矛盾：**判的是这次请求之后那一格是什么**。"""
        res = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID, client_secret="",
                        account="", password="")
        body = res.json()
        self.assertEqual(body["code"], ErrorCode.VALIDATION, res.content)
        self.assertIn("client_secret", body["msg"])
        self.assertIn("只在保存时填一次", body["msg"])
        self.assertFalse(JidanpuMemberBinding.objects.exists())

    def test_switching_to_openapi_without_credentials_is_rejected(self):
        """⭐ ①→② 的那一跳**必须**把两格补齐（⛔ 经这个端点造不出一行「②式但没 client_id」）。
        ⛔ 弄红它：把 `_openapi_required_now()` 的「请求声明了 `mode`」那条去掉 ⇒ 本用例红。"""
        self._put(account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi")
        res = self._put(mode="openapi", account=MEMBER_OPENAPI_USERNAME,
                        password="pw-canary-member-openapi", client_id="", client_secret="")
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, res.content)
        self.assertEqual(
            JidanpuMemberBinding.objects.get(user=self.user).mode, "password",
            "校验不过 ⇒ `mode` 一个字都不许改",
        )

    def test_password_mode_still_requires_the_account(self):
        """**C2 后半**（回归）：①式没有账号 ⇒ 仍然拒绝（这一条一个字都没放松）。
        ⛔ 弄红它：把 `validate_account` 整个删掉 ⇒ 本用例红（会落一行没有账号的①式绑定）。"""
        res = self._put(account="", password="pw-canary-member-openapi")
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, res.content)
        self.assertIn("账号", res.json()["msg"])

    def test_a_create_without_a_mode_is_password_mode_and_does_not_demand_the_two_cells(self):
        """⭐ **没声明 `mode` ⇒ ①式**（`mode` 是唯一判据）⇒ 新建时**不**强制那两格。
        ⚠️ 这一条钉的就是「触发条件只看**请求体里那一格**」那半句：⛔ 不许因为「这是一次新建」就把「②式必填」挂上去
        （那会让一次普通的①式保存被拒、逼用户去补他根本用不到的两格）；⛔ 弄红它：让 `_openapi_required_now()` 对「新建且没给 `mode`」回 True ⇒ 本用例被拒且 `msg` 点名 `client_id`。"""
        res = self._put(account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi")

        self.assertEqual(res.json()["code"], 0, res.content)
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertEqual(binding.mode, "password")
        self.assertEqual(binding.client_id, "")

    def test_a_plain_update_on_an_openapi_row_does_not_demand_the_secret_again(self):
        """⚠️ **有意的放宽（写清楚，别读成漏判）**：请求**没带 `mode`** 的更新不强制那两格。理由（spec §4.3 的「为什么不落 DB」栏自己写着）：`client_secret` 只写不读 ⇒ 更新路径上判不了「库里那一把还在不在」，硬判会把**每一次无关保存**（改地址 / 关开关）都变成「必须重填密钥」= 把合法更新拒掉。
        ⇒ 这一行的「②式但没有 client_id」（ORM / shell 直写那种遗留形态）不会被这一层拦，
        而由**另外两道**兜住：凭据不全 ⇒ 「还没配置…client_id…」+ 启用前的探测。"""
        JidanpuMemberBinding.objects.create(
            user=self.user, jidanpu_base_url=MEMBER_OPENAPI_BASE_URL,
            account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi",
            mode="openapi", client_secret=MEMBER_OPENAPI_SECRET, enabled=False,
        )
        res = self._put(account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi")
        self.assertEqual(res.json()["code"], 0, res.content)
        #: 但一旦想**启用**，缺 client_id 这一件事就被说出来（第二道兜底）
        res = self._put(account=MEMBER_OPENAPI_USERNAME,
                        password="pw-canary-member-openapi", enabled=True)
        body = res.json()
        self.assertEqual(body["code"], ErrorCode.PLATFORM_NOT_CONFIGURED, res.content)
        self.assertIn("client_id", body["msg"])
        self.assertIn("还没配置", body["msg"])
        JidanpuMemberBinding.objects.get(user=self.user).refresh_from_db()
        self.assertIs(JidanpuMemberBinding.objects.get(user=self.user).enabled, False)

    # ── ④ 读响应里的回调地址（+ 原因）─────────────────────────────────────

    def test_the_read_response_carries_the_callback_address_with_that_code(self):
        """⭐ spec §11.3：地址由**后端算好**回给页面；地址里那串码就是本行那一串。
        ⛔ 弄红它：把 `callback_url` 从序列化器拿掉（页面只能自己拼域名 ⇒ 规则一改必漂）。"""
        binding = self._create_openapi_row()
        for label, res in (("get", self.client.get(URL)),
                           ("put", self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID,
                                             client_secret=""))):
            with self.subTest(where=label):
                data = res.json()["data"]
                self.assertEqual(
                    data["callback_url"],
                    f"https://tenant.test.com/api/jidanpu/callback/{binding.callback_code}",
                )
                self.assertEqual(data["callback_url_reason"], "")
                #: ⛔ 地址里不许出现身份 / 密钥（C11 同族）
                for secret in (MEMBER_OPENAPI_SECRET, OPENAPI_CLIENT_ID):
                    self.assertNotIn(secret, data["callback_url"])

    def test_a_missing_domain_gives_a_reason_instead_of_a_blank_address(self):
        """⭐ **C10**：算不出地址 ⇒ **空串 + 原因**（⛔ 不静默给空、⛔ 不拼半截地址）；判据用**字面量**（`主域名`）而不是 `CALLBACK_URL_REASON_NO_DOMAIN`（拿被测常量当期望值的断言不会红，E-0024）。
        ⚠️ 「域名取不到」怎么造：把 `tenant_primary_domain` 打桩成回空串（**它**就是那个「查域名」的唯一出口）——⛔ **不能**去删 `platform.Domain` 那一行：那一行**同时是 Host 路由行**（django-tenants），删掉之后请求根本到不了视图（回的是 HTML）
        ⇒ 那会变成一条**假红**（E-0081 同族）。"""
        self._create_openapi_row()
        with mock.patch.object(jidanpu_openapi, "tenant_primary_domain", return_value=""):
            data = self.client.get(URL).json()["data"]
        self.assertEqual(data["callback_url"], "")
        self.assertIn("主域名", data["callback_url_reason"])

    def test_a_password_mode_row_explains_why_there_is_no_callback_address(self):
        """①式没有回调地址 ⇒ 原因那一格要说清「是方式的问题」，⛔ 不是留一个空串让人猜。"""
        self._put(account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi")
        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["callback_url"], "")
        self.assertIn("账号 + 密码", data["callback_url_reason"])

    # ── ⭐ §9.4 第 2 问：那句人话进响应，而水位那一列⛔ 不进 ────────────────

    def test_the_read_response_carries_the_consumer_sentence_and_never_the_watermark(self):
        """⭐ `consumer_state`（`{text, bad}`）必须到得了页面，而 `callback_watermark_at` ⛔ **一个字节都不许出现在响应里**（spec §4.2 / §13.3-C8）。
        两条判据各挡一种形态（⛔ 都要能红）：① **键**：`"callback_watermark_at" not in data`；② **值**：拿一个**独一无二**的水位时刻去搜**响应字节**（⛔ 只判键的话，「字段改个名字回出去」这种形态照样漏）。⛔ 弄红它：把 `callback_watermark_at` 加进序列化器 `fields` ⇒ ①② 都红；把 `consumer_state` 拿掉 ⇒ 下面那几条断言红。
        ⚠️ 人话的**内容**不在这一条里判（阈值与四种结论由 `tests/test_jidanpu_member_openapi_consume.py` 的 `HealthTests` 钉），这里只钉「它出得去、形状是那两格、水位出不去」。"""
        self._create_openapi_row()
        watermark = timezone.datetime(2026, 10, 9, 3, 7, 11, tzinfo=dt_timezone.utc)
        JidanpuMemberBinding.objects.filter(user=self.user).update(
            callback_watermark_at=watermark, callback_last_at=watermark,
        )
        res = self.client.get(URL)
        data = res.json()["data"]
        #: ① 键不在
        self.assertNotIn("callback_watermark_at", data, "水位那一列不许上页面（spec §13.3-C8）")
        #: ② 值不在（ISO 字符串形状与 DRF 的渲染口径一致）
        self.assertNotIn("2026-10-09T03:07:11", res.content.decode("utf-8"))
        #: 那句人话在，且**只有两格**（⛔ 不许把内部读数 `last_at` / `watermark_at` 带出去）
        self.assertEqual(set(data["consumer_state"]), {"text", "bad"})
        self.assertTrue(data["consumer_state"]["text"])
        self.assertIs(data["consumer_state"]["bad"], False)

    def test_a_password_mode_row_gets_the_not_applicable_sentence(self):
        """①式那一行的 `consumer_state` 要**如实**说「没有回调要消费」，⛔ 不是留一个空格子。"""
        self._put(account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi")
        state = self.client.get(URL).json()["data"]["consumer_state"]
        self.assertIn("账号 + 密码", state["text"])
        self.assertIs(state["bad"], False)

    # ── ⭐ 保存时写那份 0600 密钥文件（spec §5.3）───────────────────────────

    def test_saving_an_openapi_binding_writes_the_0600_key_file(self):
        """⭐ 保存②式绑定 ⇒ 「码 → 密钥」那一行**真的**落进了那份文件（0600）。判据打在**文件字节**上
        （⛔ 不是「函数被调过」）：接收器读的就是这份文件，写错地方 / 写错内容在生产上表现为**全部回调 401**，而页面与库里一切正常（静默）。另加一条 `ensure_callback_key` 的**调用判据**（1 次、且参数就是这一行）。
        ⛔ 弄红它：把视图里那一行 `ensure_callback_key(binding)` 删掉 ⇒ 文件里没有那一行。"""
        with mock.patch.object(
            jidanpu_openapi, "ensure_callback_key", wraps=jidanpu_openapi.ensure_callback_key,
        ) as ensured:
            binding = self._create_openapi_row()
        self.assertEqual(ensured.call_count, 1, "保存②式绑定必须写一次回调密钥文件")
        self.assertEqual(ensured.call_args.args[0].pk, binding.pk)
        text = self._keys_file.read_text(encoding="utf-8")
        self.assertIn(f"{binding.callback_code}\t{MEMBER_OPENAPI_SECRET}", text)
        self.assertEqual(self._keys_file.stat().st_mode & 0o777, 0o600, "密钥文件必须是 0600")

    def test_enabling_an_openapi_binding_also_writes_the_key_file(self):
        """⭐ **启用那一档**同样要写：`enabled` 的两档都照写（「先配好、再启用」是正常顺序）。⚠️ 与上一条合起来才完整、不是重复：上一条的 `_put` 默认 `enabled=False`，本条的请求是 `enabled=True`——错写成 `if binding.enabled: ensure_callback_key(binding)` ⇒ **上一条**红；错写成 `if not binding.enabled: …` ⇒ **本条**红；只留一条时另一种错法**静默通过**（后果：启用后回调全部 401，页面与库里一切正常）。
        ⛔ 弄红它：把 `jidanpu_openapi.ensure_callback_key(binding)` 挪到 `if binding.enabled:` 里面。
        ⚠️ 判据要打在**这一次请求**写出来的字节上 ⇒ 先把建行时写下的那份文件删掉（否则「文件还在」会让本条在「这次没写」时照样绿）。"""
        binding = self._create_openapi_row()
        self.upstream.happy()
        #: 前置对照：建行那一次已经写过 ⇒ 先清掉，「文件在不在」才对本次请求有判别力。
        #: ⚠️ `missing_ok=True`：不这么写，本条在「建行那一步没写」的变异下会以 `FileNotFoundError` **ERROR**
        #:    （那是上面那条用例该红的地方）——本条要能**自己**分辨「启用那一档写没写」。
        self._keys_file.unlink(missing_ok=True)
        self.assertFalse(self._keys_file.exists(), "前置：本次请求之前那份文件不在")

        res = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID, client_secret="",
                        account="", password="", enabled=True)

        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertTrue(
            JidanpuMemberBinding.objects.get(pk=binding.pk).enabled, "前置：这一行真的被启用了"
        )
        self.assertTrue(self._keys_file.exists(), "启用那一档也必须写那份文件")
        self.assertIn(f"{binding.callback_code}\t{MEMBER_OPENAPI_SECRET}",
                      self._keys_file.read_text(encoding="utf-8"))
        self.assertEqual(self._keys_file.stat().st_mode & 0o777, 0o600, "密钥文件必须是 0600")

    def test_a_password_row_writes_no_key_file(self):
        """反向对照：①式保存 ⇒ 那份文件**一个字节都不许写**（写了会让那条回调必然 401）。
        ⛔ 弄红它：把 `ensure_callback_key` 换成「无条件写一行」⇒ 本用例红。"""
        self._put(account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi")
        self.assertFalse(self._keys_file.exists(), "①式不该产生回调密钥文件")

    def test_a_key_file_write_failure_rolls_the_whole_save_back(self):
        """⭐ spec §5.3 第 2 条的代价栏：**写不进那份文件 ⇒ 整个保存回滚**；⛔ 不许留「库里说配好了、文件里没有」的半态：那种绑定的回调**必然 401/503**，而页面（`enabled` + `client_secret_set` 都好好的）**看不出来**。
        判据三条（⛔ 都要能红）：① 请求**响亮失败**（`CallbackKeysError` 冒到 HTTP 层 ⇒ 500，⛔ 不伪装成业务结果）；② 库里**一行都没有**（新建那一支）；③ 文件里也没有那一行；⛔ 弄红它：把 `ensure_callback_key(binding)` 挪到 `with transaction.atomic():` **之外**（或吞掉它）⇒ ①③ 里至少一条红。
        ⚠️ 注入点是 `_write_all`（**写盘字节的唯一出口**，`tests/test_jidanpu_callback_keys.py` 用的同一个），⛔ 不是给 `ensure_callback_key` 打桩——那样测的是桩、不是这条接线。"""
        def _boom(fd, data):
            raise OSError("合成：写盘失败")

        with mock.patch.object(jidanpu_openapi, "_write_all", _boom):
            with self.assertRaises(jidanpu_openapi.CallbackKeysError):
                self._create_openapi_row_raising()
        self.assertFalse(JidanpuMemberBinding.objects.exists(), "写失败 ⇒ 库里一行都不许留")
        self.assertFalse(self._keys_file.exists(), "写失败 ⇒ 文件里也不许留半行")

    def _create_openapi_row_raising(self):
        """同 `_create_openapi_row`，但**不断言**返回码（写失败那一档要的是异常）。"""
        return self._put(
            mode="openapi", client_id=OPENAPI_CLIENT_ID,
            client_secret=MEMBER_OPENAPI_SECRET, account="", password="",
        )

    def test_a_key_file_write_failure_leaves_an_existing_row_untouched(self):
        """⭐ 更新那一支同样要整体回滚：⛔ 不许「库里换了密钥、文件里还是旧密钥」。
        ⛔ 弄红它：把 `ensure_callback_key` 放到 `save()` **之前**或事务外 ⇒ 库里那一格变了。"""
        binding = self._create_openapi_row()
        before = JidanpuMemberBinding.objects.get(pk=binding.pk)
        new_secret = "sentinel-secret-must-not-leak-member-0002"

        def _boom(fd, data):
            raise OSError("合成：写盘失败")

        with mock.patch.object(jidanpu_openapi, "_write_all", _boom):
            with self.assertRaises(jidanpu_openapi.CallbackKeysError):
                self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID,
                          client_secret=new_secret, account="", password="")

        after = JidanpuMemberBinding.objects.get(pk=binding.pk)
        self.assertEqual(after.client_secret, before.client_secret, "写失败 ⇒ 库里那一格一个字都不许变")
        self.assertEqual(after.updated_at, before.updated_at)

    # ── §2.3-C2：换方式 = 换凭据 ⇒ 在飞事件收敛 ────────────────────────────

    def test_switching_the_mode_converges_in_flight_events(self):
        """⭐ spec §2.3-C2：`password` → `openapi` 也要收敛在飞事件。
        ⛔ 弄红它：把 `mode` 从「换绑」的触发条件里去掉（只看 `account` / `base_url`）⇒ 事件留在 `pending`，
        而换方式之后它会**拿着新凭据去重试旧事件**。"""
        binding = self._create_openapi_row(mode="password", client_id="", client_secret="",
                                          account=MEMBER_OPENAPI_USERNAME,
                                          password="pw-canary-member-openapi")
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind="member.complete", target_type="order",
            target_ref="20261008000000000901",
            status=JidanpuSyncEvent.Status.PENDING,
            payload={"member_binding_id": binding.pk},
        )

        res = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID,
                        client_secret=MEMBER_OPENAPI_SECRET, account="", password="")
        self.assertEqual(res.json()["code"], 0, res.content)
        event.refresh_from_db()
        self.assertEqual(event.status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertIn(REBIND_REASON_HINT, event.reason or "")

    # ── 启用前的那次探测（spec §11.4 末段 / §25.2）────────────────────────

    def test_enabling_an_openapi_binding_runs_the_probe_and_never_the_login_detector(self):
        """②式启用 ⇒ 跑**探测**（真的打那五个只读端点）；探测通过 ⇒ 真的启用并盖上 `checked_at`。
        ⛔ 弄红它：去掉 PUT 里那个 `if _is_openapi(binding):` 分支 ⇒ ②式会去走①式的 `detect_binding_with_login_identity`（拿空账号密码登录，必然失败）⇒ 本用例红。"""
        self._create_openapi_row()
        self.upstream.happy()
        with mock.patch.object(jidanpu_member, "detect_binding_with_login_identity") as legacy:
            res = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID, client_secret="",
                            account="", password="", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(legacy.called, "②式⛔ 不许走登录式检测（它没有账号密码）")
        self.assertEqual(
            {row["path"] for row in self.upstream.requests},
            {
                jidanpu_openapi.TOKEN_PATH,
                jidanpu_openapi.GAMES_PATH,
                jidanpu_openapi.PROJECT_PATH,
                jidanpu_openapi.ORDER_STATUS_PATH,
                jidanpu_openapi.ORDER_PROCESS_IMGS_PATH,
            },
            "②式启用前跑的就是那张逐条表上的那五发（⛔ 一个写接口都不打）",
        )
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertIs(binding.enabled, True)
        self.assertIsNotNone(binding.checked_at)

    def test_enabling_is_refused_when_the_order_status_permission_is_missing(self):
        """⭐ **§25.2 的收窄**：缺 `GetOrderStatus` 权限 ⇒ **拒绝启用**（这一跳缺了，状态同步不可能）。
        ⛔ 弄红它：把视图那一行改成 `if not probe.ok:`（= 收窄之前的样子）⇒ 本用例红；或把 `ENABLE_GATE_STEPS` 里的
        `GetOrderStatus` 去掉 ⇒ 本用例红。"""
        self._create_openapi_row()
        self.upstream.happy()
        self.upstream.reply(
            jidanpu_openapi.ORDER_STATUS_PATH, {"code": 0, "msg": INNER_PERMISSION_MSG}
        )
        res = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID, client_secret="",
                        account="", password="", enabled=True)
        body = res.json()
        self.assertEqual(body["code"], ErrorCode.PLATFORM_NOT_CONFIGURED, res.content)
        #: ⭐ ① 明说**跑不了**（⛔ 不许再出现「不影响」—— 照用逐字文案就是说假话）
        self.assertNotIn("不影响", body["msg"], body["msg"])
        #: ⭐ ② 点名到**那一跳**
        self.assertIn(
            jidanpu_openapi.STEP_LABELS[jidanpu_openapi.STEP_ORDER_STATUS], body["msg"]
        )
        #: ⭐ ③ **照旧**给那句可自助的指引（与「测试连接」同一份来源，⛔ 不另写一套）
        self.assertIn("权限设置", body["msg"])
        self.assertIn("你自己就能做", body["msg"])
        self.assertNotIn("联系鸡蛋铺", body["msg"])
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertIs(binding.enabled, False, "缺 GetOrderStatus 权限 ⇒ ⛔ 不许启用")
        self.assertIsNone(binding.checked_at)
        self.assertEqual(binding.mode, "openapi", "拒绝启用 ⇒ 别的格照旧一个字都不许动")

    def test_enabling_is_allowed_when_only_the_games_list_permission_is_missing(self):
        """⭐ **§25.2 的收窄**：只缺「取游戏列表」权限 ⇒ **允许启用**（那把闸只看 `token` + `GetOrderStatus`）。
        ⛔ 弄红它：把视图那一行改回 `if not probe.ok:` ⇒ 本用例红。"""
        self._create_openapi_row()
        self.upstream.happy()
        self.upstream.reply(jidanpu_openapi.GAMES_PATH, {"code": 0, "msg": INNER_PERMISSION_MSG})
        res = self._put(mode="openapi", client_id=OPENAPI_CLIENT_ID, client_secret="",
                        account="", password="", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        binding = JidanpuMemberBinding.objects.get(user=self.user)
        self.assertIs(binding.enabled, True, "只缺游戏列表权限 ⛔ 不该挡着启用（spec §25.2）")
        self.assertIsNotNone(binding.checked_at)
        #: ⚠️ 闸收窄**不等于**少探少报：五项照样全探、缺的那一项照样点名（信息口径仍是「没全绿」）
        probe = jidanpu_openapi.probe_read_endpoints(binding)
        self.assertEqual([row.step for row in probe.checks], list(jidanpu_openapi.PROBE_ORDER))
        self.assertFalse(probe.ok, "信息口径仍是「五项没有全绿」")
        self.assertTrue(probe.gate_ok, "但那把闸只看两跳 ⇒ 通")

    def test_the_credential_set_is_picked_by_mode_not_by_a_non_empty_client_id(self):
        """⭐ spec §2.1 / E-0021 家族：①式行上**留着** `client_id`（切来切去不重输的形态）⇒ 「测试连接」必须走**登录式**那一支，
        ⛔ 不许看 `client_id` 非空就走官方 API；⛔ 弄红它：把分流判据写成 `if binding.client_id:` ⇒ 本用例红。
        ⚠️ 两个桩都返回**真实形状**的结论（⛔ 不返回裸 `MagicMock`）：裸 `MagicMock` 漏进 `api_response` 会让响应渲染**挂住**（`json` 编码一个 `MagicMock` 会死循环，整条用例既不红也不绿）——「**挂住**更不算 killed」；桩返回真形状时走错分支只会干脆地断言失败。"""
        JidanpuMemberBinding.objects.create(
            user=self.user, jidanpu_base_url=MEMBER_OPENAPI_BASE_URL,
            account=MEMBER_OPENAPI_USERNAME, password="pw-canary-member-openapi",
            mode="password", client_id=OPENAPI_CLIENT_ID,
            client_secret=MEMBER_OPENAPI_SECRET, enabled=False,
        )
        report, _name = (
            jidanpu_member.DetectionReport(ok=True, failures=()), None,
        )
        probe_report = jidanpu_openapi.ProbeReport(checks=tuple(
            jidanpu_openapi.EndpointProbe(step=step, probed=True, ok=True)
            for step in jidanpu_openapi.PROBE_ORDER
        ))
        with mock.patch.object(
            jidanpu_member, "detect_binding_with_login_identity",
            return_value=(report, _name),
        ) as legacy, mock.patch.object(
            jidanpu_openapi, "probe_read_endpoints", return_value=probe_report,
        ) as probe:
            body = self.client.post(TEST_URL).json()
        self.assertTrue(legacy.called, "①式必须走登录式检测")
        self.assertFalse(probe.called, "①式⛔ 不许因为 client_id 非空就走官方 API 探测")
        self.assertEqual(
            set(body["data"]),
            {"ok", "messages", "source_options", "source_options_ok", "source_ok"},
            "①式的键集合一字未变",
        )
        self.assertEqual(body["data"]["ok"], True)
        self.assertEqual(body["data"]["messages"], [])


# ══ ⑥-2 ⭐ **一个上家域名共用一个回调码**（业务方裁定）══════════════════
# 业务方原话：一个租户对某一个上家域名的回调地址应该按照**租户维度唯一**，不然多个打手绑定岂不是要填多个回调地址——同一个下家鸡蛋铺系统上只有**一处**「回调地址」设置项 ⇒ 多个打手绑同一个域名时只能填同一个地址。
# ⚠️ 判据打在**页面真正拿到的东西**上（`callback_url`）：业务方报的就是「要填多个地址」，判据必须能回答「两条绑定的回调地址是不是同一个」，⛔ 不是「两个码字符串相等」就够（地址 = 码 + 租户主域名，两条轴都要真）；每条都写清「怎么弄红它」。

MEMBER_SHARED_BASE_URL = "https://jidanpu-shared-domain.test.invalid"
MEMBER_OTHER_BASE_URL = "https://jidanpu-other-domain.test.invalid"


class JidanpuMemberSharedCallbackCodeTests(TenantTestCase):
    """⭐ 同一个域名下多个打手 ⇒ **同一个回调地址**；不同域名 ⇒ **不同**。"""

    def setUp(self):
        #: ②式保存会真的写那份 0600「码 → 密钥」文件 ⇒ 指到临时目录。
        self._keys_file = _temp_callback_keys_file(self)
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.upstream = _OpenApiUpstream()
        patcher = _patch_openapi_upstream(self.upstream)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _bind(self, *, username, base_url, account="", secret=MEMBER_OPENAPI_SECRET):
        """以一个**打手**的身份走真端点建一条②式绑定；回 `(响应里的 data, 库里那一行)`。"""
        client = make_logged_in_client(username=username)
        payload = {
            "mode": "openapi",
            "jidanpu_base_url": base_url,
            "account": account,
            "password": "",
            "client_id": OPENAPI_CLIENT_ID,
            "client_secret": secret,
            "source_name": "团团通系统",
            "enabled": False,
        }
        res = client.put(URL, json.dumps(payload), content_type="application/json")
        body = res.json()
        self.assertEqual(body["code"], 0, body)
        row = JidanpuMemberBinding.objects.get(user=User.objects.get(username=username))
        return body["data"], row

    def test_two_members_on_the_same_domain_share_one_callback_address(self):
        """⭐ **判据本体**：第二个打手绑**同一个域名** ⇒ 存得上，且地址与第一个**逐字相同**。⛔ 弄红它（两条独立的路，各自都能让它红）：
        ① 把 `_ensure_callback_code` 里「复用同域名兄弟行那个码」那一支去掉 ⇒ 两条绑定的 `callback_url` 不同（那条 `assertEqual` 红）；
        ② 把模型那条 `(jidanpu_base_url, account)` 约束的 `condition` 去掉 ⇒ **第二个打手根本存不上**（`_bind` 里那句 `code == 0` 就红）——这正是业务方报的「保存不了」。"""
        first_body, first_row = self._bind(username="member-share-a",
                                           base_url=MEMBER_SHARED_BASE_URL)
        second_body, second_row = self._bind(username="member-share-b",
                                             base_url=MEMBER_SHARED_BASE_URL)

        self.assertNotEqual(first_row.pk, second_row.pk, "自证夹具：两个打手各一行")
        self.assertEqual(second_body["callback_url"], first_body["callback_url"],
                         "同一个域名下的两个打手拿到**两个**回调地址 ⇒ 用户要填两遍（业务方报的形态）")
        self.assertTrue(second_body["callback_url"].startswith("https://"), second_body)
        self.assertEqual(second_row.callback_code, first_row.callback_code)

    def test_two_members_on_different_domains_get_different_callback_addresses(self):
        """⭐ 反向对照：**不同域名** ⇒ 地址**必须不同**（⛔ 不是「所有绑定共用一个码」）。
        ⛔ 弄红它：把复用写成「无条件抄已经存在的那一串码」（不加域名过滤）⇒ 本条红。⚠️ 为什么这条**不能省**：密钥文件是「一个码一行」，
        两个域名共用一串码时后保存的那个会把前一个域名的密钥盖掉 ⇒ 那一边的回调**验签不过**（而页面上两条地址看起来都正常）。"""
        first_body, _ = self._bind(username="member-diff-a", base_url=MEMBER_SHARED_BASE_URL)
        other_body, _ = self._bind(username="member-diff-b", base_url=MEMBER_OTHER_BASE_URL)
        self.assertNotEqual(other_body["callback_url"], first_body["callback_url"])

    def test_an_openapi_binding_is_not_blocked_by_someone_elses_password_account(self):
        """⭐ 那个「地址 + 账号」占用判据**只管①式**：别人的①式账号不挡我的②式绑定。⚠️ 形态是**真实**的：一个打手先按①式绑过
        （留着账号），切到②式之后那一格的值照旧在（界面只是把它藏起来）⇒ 载荷里带着同一个账号去保存。
        ⛔ 弄红它：把 `validate()` 里那个 `mode=password` 的过滤去掉（或把「②式直接跳过」那两行删掉）⇒ 本条收到的会是「该鸡蛋铺账号已被 测试客服甲 绑定…」而不是成功。"""
        owner = User.objects.create_user(
            username="member-owner-shared", password="pw", first_name="测试客服甲",
        )
        JidanpuMemberBinding.objects.create(
            user=owner, jidanpu_base_url=MEMBER_SHARED_BASE_URL, account="测试账号甲",
            password=PASSWORD_B, source_name="团团通系统", enabled=False,
        )

        body, row = self._bind(username="member-share-c", base_url=MEMBER_SHARED_BASE_URL,
                               account="测试账号甲")

        self.assertEqual(row.account, "测试账号甲", "②式那一格的值照旧落库（⛔ 不是被拒）")
        self.assertTrue(body["callback_url"].startswith("https://"))


# ══ ⑦ ②式「测试连接」的端点层判据（spec §9.2 / §20.2 / §13.3-C9）══════════════


class JidanpuMemberOpenApiTestConnectionTests(TenantTestCase):
    """②式探测的端点层判据（⛔ 零真实网络：真 `httpx.Client` + `MockTransport`）。"""

    def setUp(self):
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self._keys_file = _temp_callback_keys_file(self)
        self.client = make_logged_in_client(username="member-openapi-probe")
        self.user = User.objects.get(username="member-openapi-probe")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user, jidanpu_base_url=MEMBER_OPENAPI_BASE_URL, account="", password="",
            mode="openapi", client_id=OPENAPI_CLIENT_ID,
            client_secret=MEMBER_OPENAPI_SECRET, enabled=False,
        )
        self.upstream = _OpenApiUpstream()
        patcher = _patch_openapi_upstream(self.upstream)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self, data):
        return {row["step"]: row for row in data["checks"]}

    def test_the_endpoint_returns_a_per_endpoint_table_and_writes_nothing(self):
        """⭐ ②式的出参是**一张逐条表**（spec §20.2-1），而且这一跳**一行都不落库**。
        ⚠️ 顺序与行数**恰好**（⛔ 不放宽成 `toContain`）：五项、按 `PROBE_ORDER`。
        ⛔ 弄红它：把分流删掉（②式走①式那一支）⇒ 出参里没有 `checks` ⇒ 本用例红。"""
        self.upstream.happy()
        before = (self.binding.checked_at, self.binding.last_error, self.binding.enabled,
                  self.binding.updated_at, self.binding.callback_count,
                  self.binding.account_name)
        res = self.client.post(TEST_URL)
        body = res.json()
        self.assertEqual(body["code"], 0, res.content)
        data = body["data"]
        self.assertIs(data["ok"], True)
        self.assertEqual(data["messages"], [])
        self.assertEqual(data["actions"], [])
        self.assertEqual(
            [row["step"] for row in data["checks"]],
            ["token", "GetGames", "GetProject", "GetOrderStatus", "GetOrderProcessImgs"],
        )
        self.assertEqual(len(data["checks"]), 5, "逐条表恰好 5 行")
        for row in data["checks"]:
            with self.subTest(step=row["step"]):
                self.assertEqual(
                    set(row), {"step", "label", "probed", "ok", "kind", "reason",
                               "message", "action"},
                    "逐条表的字段名就是页面要渲染的契约",
                )
                self.assertTrue(row["ok"])
                self.assertTrue(row["label"])
        self.binding.refresh_from_db()
        self.assertEqual(
            (self.binding.checked_at, self.binding.last_error, self.binding.enabled,
             self.binding.updated_at, self.binding.callback_count, self.binding.account_name),
            before, "测试连接是只读的：一行都不许写（account_name 那一格也不许动）",
        )

    def test_the_three_kinds_give_three_pairwise_different_sentences(self):
        """⭐ 三种档位**两两不同**（⛔ 不许退化成一句「连接失败」）。⚠️ 一次请求一种档位、三条读数放在一起比：
        这条判据是**跨档**的，单独一条档位的用例抓不到「两档合并成一句」。⛔ 弄红它：把 ②/③ 两档的文案合并成同一句
        ⇒ `len(set(texts)) < 3` ⇒ 本用例红。"""
        texts, actions = [], []
        for label, configure in (
            ("outer", lambda: self.upstream.reply(
                jidanpu_openapi.TOKEN_PATH, {"code": 0, "msg": OUTER_GATE_MSG})),
            ("inner", lambda: (self.upstream.happy(), self.upstream.reply(
                jidanpu_openapi.GAMES_PATH, {"code": 0, "msg": INNER_PERMISSION_MSG}))),
            ("other", lambda: self.upstream.boom(
                jidanpu_openapi.TOKEN_PATH, httpx.ConnectError("合成连接失败"))),
        ):
            with self.subTest(kind=label):
                jidanpu_openapi.reset_token_cache()
                self.upstream.routes.clear()
                self.upstream.errors.clear()
                configure()
                data = self.client.post(TEST_URL).json()["data"]
                self.assertIs(data["ok"], False, label)
                texts.append("\n".join(data["messages"]))
                actions.append("\n".join(data["actions"]))
        self.assertEqual(len(set(texts)), 3, texts)
        self.assertEqual(len(set(actions)), 3, actions)
        #: 外层 / 内层各自不许串味（合并成一句「连接失败」是这条线最忌讳的错）
        outer_text, outer_action = texts[0], actions[0]
        inner_text, inner_action = texts[1], actions[1]
        self.assertIn("联系鸡蛋铺", outer_text)
        self.assertIn("不影响订单状态同步", inner_text)
        self.assertIn("权限设置", inner_action)
        self.assertIn("你自己就能做", inner_action)
        self.assertNotIn("权限设置", outer_text + outer_action)
        self.assertNotIn("联系鸡蛋铺", inner_text + inner_action)
        self.assertNotIn("没有权限", outer_text)

    def test_the_third_kind_says_it_is_our_side_and_never_blames_permissions(self):
        """⭐ **C9 的对照**（E-0081 同族）：网络异常 ⇒ ③「我们这边」，⛔ 绝不许说成「没有权限」。"""
        self.upstream.boom(jidanpu_openapi.TOKEN_PATH, httpx.ConnectError("合成连接失败"))
        data = self.client.post(TEST_URL).json()["data"]
        self.assertIs(data["ok"], False)
        self.assertEqual(self.rows(data)["token"]["kind"], "other")
        text = "\n".join(data["messages"])
        self.assertIn("我们这边", text)
        self.assertIn("反馈给管理员", text)
        self.assertNotIn("权限", text)
        self.assertNotIn("联系鸡蛋铺", text)

    def test_the_probe_only_ever_touches_the_read_endpoints_and_the_token(self):
        """⛔ **绝不许为了探测去打写接口**（spec §11.4 / §20.2）。
        判据打在**真的发出去的请求**上：路径**恰好**是那 5 个（⛔ 不放宽成 `toContain`），而且那五个写接口的名字一个都不出现；
        除了取 token 那一发，其余全是 GET 且**空体**。"""
        self.upstream.happy()
        self.client.post(TEST_URL)
        expected = {
            jidanpu_openapi.TOKEN_PATH,
            jidanpu_openapi.GAMES_PATH,
            jidanpu_openapi.PROJECT_PATH,
            jidanpu_openapi.ORDER_STATUS_PATH,
            jidanpu_openapi.ORDER_PROCESS_IMGS_PATH,
        }
        self.assertEqual(set(self.upstream.paths()), expected)
        for name in WRITE_ENDPOINT_NAMES:
            with self.subTest(endpoint=name):
                self.assertNotIn(name, " ".join(self.upstream.paths()))
        for row in self.upstream.requests:
            if row["path"] == jidanpu_openapi.TOKEN_PATH:
                continue
            with self.subTest(path=row["path"]):
                self.assertEqual(row["method"], "GET")
                self.assertEqual(row["body"], "")

    def test_the_view_does_not_build_official_api_paths_by_itself(self):
        """结构性判据：路径字面量**只许**在客户端模块里（视图⛔ 不另立一套）。
        ⚠️ 与上面那条**互补**：行为判据抓「真的打出去了没有」，这条抓「下一个改动有没有在视图里手搓一个路径」。"""
        source = Path(jidanpu_member_views.__file__).read_text(encoding="utf-8")
        self.assertNotIn("/api/OpenPlatform/", source)
        self.assertNotIn("/oauth/token", source)

    def test_the_probe_refuses_a_row_that_is_openapi_but_has_no_client_id(self):
        """②式但缺 `client_id` ⇒ 一句**准确**的话（说法「还没配置…client_id…」），⛔ 不去打上游。
        ⚠️ 夹具刻意让**①式那把判据认为齐全**（地址 / 账号 / 密码都非空——「两套凭据切来切去都留着」的真实形态）：只有把分流判据真正**按 `mode`** 分，这一条才会被②式那把判据挡住。
        ⛔ 弄红它：把 `_credentials_missing` 写成只看①式那三个字段（实测：第一版夹具里账号密码是空的 ⇒ ①式那把判据**恰好也说缺** ⇒ 变异 SURVIVED，看不出分流有没有分）⇒ 本用例红。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            account="acc-openapi-probe", password="pw-canary-openapi-probe", client_id="",
        )
        body = self.client.post(TEST_URL).json()
        self.assertEqual(body["code"], ErrorCode.PLATFORM_NOT_CONFIGURED, str(body))
        self.assertIn("client_id", body["msg"])
        self.assertEqual(self.upstream.requests, [], "凭据不全⛔ 一发都不许打")

    def test_the_password_branch_still_returns_its_original_keys(self):
        """**回归**：①式那一支的**键集合一字未变**（既有页面与既有用例逐字依赖它）。
        ⛔ 弄红它：把②式那张表也回给①式（或给①式顺手加一个 `checks` 键）⇒ 本用例红。"""
        self.binding.mode = "password"
        self.binding.account = "member-openapi-probe"
        self.binding.password = "pw-canary-member-openapi"
        self.binding.save(update_fields=["mode", "account", "password"])
        with mock.patch.object(
            jidanpu_member, "detect_binding_with_login_identity",
            return_value=(jidanpu_member.DetectionReport(ok=True, failures=()), None),
        ):
            body = self.client.post(TEST_URL).json()
        self.assertEqual(
            set(body["data"]),
            {"ok", "messages", "source_options", "source_options_ok", "source_ok"},
        )


# ══ 手动推送端点（计划 `2026-10-23-taker-my-orders-push.md` §2）═══════════════
# 判据分两层，各自的出口不同（⛔ 不要合成一条「看着都过了」的大用例）：**本类**钉端点的契约——权限（401 / 403）、无绑定 1002、逐单归属、排哪条事件、只跑**一轮** outbox、汇总计数与逐单结果、50 条上限、去重保序、只碰自己的绑定；**能不能真的推出去**（标签、回读、防倒退、自动路径不变）由 `tests/test_jidanpu_member_consume.py` 用真桩钉住（那里有 `_Upstream`）。
# ⚠️ 本类**不接真上游**：①式那一轮要登录（`line.spawn_client`），所以「跑一轮」用下面两个替身之一：要么接**真引擎但注入哨兵 client**（前提是这条链一次客户端都不用，见 record 那一条），要么把 `_run_push_outbox` 换成「把在飞事件标成 done」的替身；桩打在被查找的模块上（E-0029）。


def _fake_run_outbox_marking_done(binding):
    """把这一轮**真的跑了**：该绑定在飞的事件逐条标成 `done`，回一份计数（不碰网络）。
    ⚠️ 只服务本文件「端点怎么汇总」那几条判据；引擎自己的行为在 consume 那两个文件里被钉住。"""
    done = JidanpuSyncEvent.objects.filter(
        direction="out",
        status__in=("pending", "failed"),
        payload__member_binding_id=binding.pk,
    ).update(status="done", reason="", processed_at=timezone.now())
    return jidanpu_member.OutboxStats(done=done)


class JidanpuMemberPushNowApiTests(TenantTestCase):
    """`POST /api/business/member/jidanpu-push-now/`（计划 §2）。
    ⚠️ 判据一律打**数据库行**与**响应体**（`AGENTS.md` §5-G），⛔ 不断言 logger；夹具一律合成值（`…test.invalid` / `push-taker`），⛔ 零真实网络。"""

    def setUp(self):
        self.game = Game.objects.create(name="推送线游戏甲")
        # ⚠️ 必须**同时**给一个 Group：`_grant_nodes` 只把节点挂到「该用户已在的组」对应的
        # Role 上（没有组 ⇒ 一个节点都挂不上 ⇒ 端点回 403，看起来像判据写错了）。
        self.client = make_logged_in_client(
            username="push-taker", groups=("代练员",), nodes=(PUSH_NODE,)
        )
        self.user = User.objects.get(username="push-taker")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-push",
            password=PASSWORD,
            source_name="团团通系统",
            enabled=True,
        )
        self.order = self._order(PUSH_ORDER_NO, taker=self.user)
        # 「别人接的单」：MAIN 绩效行挂在别人名下（同租户、未软删）⇒ 归属判据应当拒。
        other = User.objects.create_user(username="push-other", password=PASSWORD)
        self.other_order = self._order(PUSH_OTHER_ORDER_NO, taker=other)

    # ── 夹具工具 ────────────────────────────────────────────────────────────
    def _order(self, order_no, *, taker=None, status=OrderStatus.IN_PROGRESS):
        order = Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=status,
            hall_price=Decimal("88.00"),
        )
        if taker is not None:
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, user=taker,
                temp_amount=Decimal("1.00"),
            )
        return order

    def _link(self, *, binding=None, order=None, remote_id="JDP-PUSH-0001", in_hall=False):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding or self.binding,
            order=order or self.order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    def _push(self, order_nos, action, *, client=None, extra=None):
        payload = {"order_nos": order_nos, "action": action}
        #: `extra` 只为「请求体塞伪造键」那条用例（`user` / `binding` / `taker_id`）——
        #: 序列化器没有这些字段 ⇒ DRF 静默忽略它们（这正是要钉的那条判据）。
        payload.update(extra or {})
        return (client or self.client).post(
            PUSH_URL, json.dumps(payload), content_type="application/json"
        )

    def _items_by_no(self, body):
        return {row["order_no"]: row for row in body["data"]["items"]}

    def _approved_penalty(self, *, status=OrderStatus.COMPLETED, target=None):
        approval = Approval.objects.create(
            kind=ApprovalKind.PENALTY, status=ApprovalStatus.APPROVED,
            original_order=self.other_order, initiator=self.other_order.performances.get(emp_type=EmpType.MAIN).user,
            target=target or self.user, target_type=PenaltyTargetType.WORKER,
            amount=Decimal("30.00"), reason="合成罚款", decided_at=timezone.now(),
        )
        self.order.status = status
        self.order.hall_price = Decimal("-30.00")
        self.order.source_order_no = "罚-20261122000000000001"
        self.order.penalty_approval = approval
        self.order.save(update_fields=["status", "hall_price", "source_order_no", "penalty_approval"])
        return self.order

    def _penalty_upstream(self, *, price=-30.00, status="未分配", in_hall=False, **options):
        source_no = self.order.source_order_no
        return _Upstream(source_order_no=source_no, lookup={
            source_no: {
                "orderId": JID, "orderIdView": source_no, "price": price, "orderSta": status,
                "isThrowOrder": "Y" if in_hall else "N",
            },
        }, **options)

    def _run_penalty_outbox(self, binding):
        return jidanpu_member.run_outbox(binding, now=timezone.now(), client=CLIENT)

    def test_historical_approved_penalty_can_be_manually_recorded_once(self):
        order = self._approved_penalty()
        jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind="member.assign", target_ref=order.order_no).exists())
        original_link = self._link(order=self.other_order, remote_id="synthetic-original-remote")
        old_event = jidanpu_member.queue_member_event(order, self.binding, kind="member.assign")
        upstream = self._penalty_upstream()
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            self.assertEqual(jidanpu_member.consume_event(old_event, client=CLIENT), "skipped")
            self.assertEqual(upstream.calls, [])

            body = self._push([order.order_no], "record").json()
            self.assertEqual(body["code"], 0, body)
            self.assertEqual(body["data"]["pushed"], 1, body)

            calls = list(upstream.calls)
            JidanpuMemberOrderLink.objects.filter(order=order).update(withdrawn_at=timezone.now())
            body = self._push([order.order_no], "record").json()
            self.assertEqual(body["data"]["skipped"], 1, body)
            self.assertIn("不重复录单", body["data"]["items"][0]["reason"])
            self.assertEqual(upstream.calls, calls)

        payload = dict(upstream.calls)["add_order"]
        self.assertEqual(payload["form_payload"]["price"], "-30.00")
        self.assertEqual(payload["source_order_no"], order.source_order_no)
        self.assertEqual(upstream.names().count("add_order"), 1)
        self.assertNotIn("throw_order", upstream.names())
        self.assertNotIn("set_order_status", upstream.names())
        self.assertFalse(JidanpuMemberOrderLink.objects.get(binding=self.binding, order=order).in_hall)
        original_link.refresh_from_db()
        self.assertEqual(original_link.jidanpu_order_id, "synthetic-original-remote")
        self.assertIsNone(original_link.last_out_at)

    def test_normally_assigned_penalty_is_recorded_even_if_completed_before_the_worker_runs(self):
        original_target = User.objects.get(username="push-other")
        order = self._approved_penalty(status=OrderStatus.UNASSIGNED, target=original_target)
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).delete()
        actor = make_logged_in_client(username="assign-approved-penalty", is_superuser=True)
        url = f"/api/business/orders/{order.order_no}/"
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind="member.assign", target_ref=order.order_no).exists())

        body = actor.post(f"{url}assign/", {"user_id": self.user.pk}, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        event = JidanpuSyncEvent.objects.get(kind="member.assign", target_ref=order.order_no)
        for target in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
            body = actor.post(f"{url}transition/", {"target": target}, content_type="application/json").json()
            self.assertEqual(body["code"], 0, body)

        upstream = self._penalty_upstream()
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(dict(upstream.calls)["add_order"]["form_payload"]["price"], "-30.00")
        self.assertFalse(JidanpuMemberOrderLink.objects.get(binding=self.binding, order=order).in_hall)

    def test_historical_penalty_response_tracks_its_rearmed_event(self):
        order = self._approved_penalty()
        with mock.patch.object(
            jidanpu_member_views, "_run_push_outbox",
            return_value=jidanpu_member.OutboxStats(blocked_reason="合成队列忙"),
        ):
            first = self._push([order.order_no], "record").json()
        self.assertEqual(first["data"]["failed"], 1, first)
        event = JidanpuSyncEvent.objects.get(kind="member.assign", target_ref=order.order_no)
        event.status = "failed"
        event.attempts = jidanpu_member.OUTBOX_MAX_ATTEMPTS
        event.save(update_fields=["status", "attempts"])
        later = jidanpu_member.queue_member_event(order, self.binding, kind="member.assign", status="skipped")

        with _stub(self._penalty_upstream()), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            second = self._push([order.order_no], "record").json()

        self.assertEqual(second["data"]["pushed"], 1, second)
        event.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual((event.status, later.status), ("done", "skipped"))

    def test_manual_record_preserves_the_guards_for_ordinary_and_unassigned_orders(self):
        self.order.status = OrderStatus.COMPLETED
        self.order.hall_price = Decimal("-30.00")
        self.order.save(update_fields=["status", "hall_price"])
        upstream = self._penalty_upstream()
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            completed = self._push([self.order.order_no], "record").json()
            self.assertIn("本次旧指派作废", completed["data"]["items"][0]["reason"])

            order = self._approved_penalty(status=OrderStatus.UNASSIGNED)
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).delete()
            unassigned = self._push([order.order_no], "record").json()
            self.assertIn("不是当前接手", unassigned["data"]["items"][0]["reason"])

        self.assertEqual(upstream.calls, [])
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind="member.assign", target_ref=order.order_no).count(), 1)

    def test_manual_penalty_record_does_not_change_or_duplicate_remote_conflicts(self):
        order = self._approved_penalty()
        for price, status, in_hall, reason in (
            (-25, "未分配", False, "金额与审批不符"),
            (-30, "已退单", False, "已退单"),
            (-30, "未分配", True, "已在大厅"),
        ):
            with self.subTest(price=price, status=status, in_hall=in_hall):
                upstream = self._penalty_upstream(price=price, status=status, in_hall=in_hall, remote_exists=True)
                with _stub(upstream), mock.patch.object(
                    jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
                ):
                    body = self._push([order.order_no], "record").json()
                self.assertEqual(body["data"]["skipped"], 1, body)
                self.assertIn(reason, body["data"]["items"][0]["reason"])
                self.assertNotIn("add_order", upstream.names())
                self.assertNotIn("set_order_status", upstream.names())
                self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=order).exists())

    def test_manual_penalty_record_uses_the_current_assignee_even_when_approval_named_someone_else(self):
        other = User.objects.get(username="push-other")
        order = self._approved_penalty(target=other)
        upstream = self._penalty_upstream()
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            body = self._push([order.order_no], "record").json()
        self.assertEqual(body["data"]["pushed"], 1, body)
        self.assertEqual(dict(upstream.calls)["add_order"]["form_payload"]["price"], "-30.00")
        self.assertTrue(JidanpuMemberOrderLink.objects.filter(order=order, binding=self.binding).exists())

    def test_old_penalty_assignment_cannot_send_to_a_former_assignee(self):
        order = self._approved_penalty(status=OrderStatus.IN_PROGRESS)
        actor = make_logged_in_client(username="reassign-approved-penalty", is_superuser=True)
        other = User.objects.get(username="push-other")
        grant_games(other, self.game)
        new_binding = JidanpuMemberBinding.objects.create(
            user=other, jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B, password=PASSWORD_B, enabled=True,
        )
        self.assertEqual(jidanpu_member.queue_assign(order, taker_id=self.user.pk), 1)
        old_event = JidanpuSyncEvent.objects.get(kind="member.assign", target_ref=order.order_no)
        body = actor.post(
            f"/api/business/orders/{order.order_no}/assign/", {"user_id": other.pk}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        new_event = JidanpuSyncEvent.objects.get(
            kind="member.assign", target_ref=order.order_no, payload__member_binding_id=new_binding.pk,
        )
        upstream = self._penalty_upstream()
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(old_event, client=CLIENT), "skipped")
            self.assertEqual(upstream.calls, [])
            self.assertEqual(jidanpu_member.consume_event(new_event, client=CLIENT), "done")
        self.assertTrue(JidanpuMemberOrderLink.objects.filter(order=order, binding=new_binding).exists())
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=order, binding=self.binding).exists())

    def test_unmarked_penalty_never_records_through_a_same_store_fallback_binding(self):
        order = self._approved_penalty(status=OrderStatus.IN_PROGRESS)
        other = User.objects.get(username="push-other")
        fallback = JidanpuMemberBinding.objects.create(
            user=other, jidanpu_base_url=self.binding.jidanpu_base_url,
            account=ACCOUNT_B, password=PASSWORD_B, enabled=True,
        )
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.assertEqual(jidanpu_member.queue_assign(order, taker_id=self.user.pk), 0)
        event = jidanpu_member.queue_member_event(order, fallback, kind="member.assign")
        upstream = self._penalty_upstream()
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(upstream.calls, [])

    def _exhausted_penalty_record_with_remote_id(self, order, *, remote_id):
        event = jidanpu_member.queue_member_event(order, self.binding, kind="member.assign", status="failed", payload={
            "jidanpu_order_id": remote_id, "recorded": True, "recording_source_order_no": order.source_order_no,
            "recording_store": self.binding.jidanpu_base_url,
        })
        event.attempts = jidanpu_member.OUTBOX_MAX_ATTEMPTS
        event.save(update_fields=["attempts"])

    def test_manual_penalty_checks_cached_remote_id_instead_of_linking_a_different_order(self):
        order = self._approved_penalty()
        self._exhausted_penalty_record_with_remote_id(order, remote_id="synthetic-mismatched-ref")
        upstream = self._penalty_upstream(remote_exists=True)
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            body = self._push([order.order_no], "record").json()
        self.assertEqual(body["data"]["failed"], 1, body)
        self.assertIn("订单号与之前记录不同", body["data"]["items"][0]["reason"])
        self.assertEqual(upstream.names(), ["fetch_order_by_key1"])
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=order).exists())

    def test_manual_penalty_checks_cached_remote_price_before_linking(self):
        order = self._approved_penalty()
        self._exhausted_penalty_record_with_remote_id(order, remote_id=JID)
        upstream = self._penalty_upstream(price=-25, remote_exists=True)
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            body = self._push([order.order_no], "record").json()
        self.assertEqual(body["data"]["failed"], 1, body)
        self.assertIn("金额与审批不符", body["data"]["items"][0]["reason"])
        self.assertEqual(upstream.names(), ["fetch_order_by_key1"])
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=order).exists())

    def test_manual_penalty_outside_the_exact_lookup_window_cannot_record_after_no_result(self):
        order = self._approved_penalty()
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.now() - timedelta(days=jidanpu.ORDER_LIST_LOOKBACK_DAYS + 1),
        )
        upstream = self._penalty_upstream()
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            body = self._push([order.order_no], "record").json()
        self.assertEqual(body["data"]["skipped"], 1, body)
        self.assertIn("早于鸡蛋铺可核查", body["data"]["items"][0]["reason"])
        self.assertIn("fetch_order_by_key1", upstream.names())
        self.assertNotIn("add_order", upstream.names())

    def test_manual_penalty_record_cannot_use_the_original_orders_source_number(self):
        order = self._approved_penalty()
        self.other_order.source_order_no = order.source_order_no
        self.other_order.save(update_fields=["source_order_no"])
        upstream = self._penalty_upstream()
        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            body = self._push([order.order_no], "record").json()
        self.assertEqual(body["data"]["skipped"], 1, body)
        self.assertEqual(upstream.calls, [])

    def test_manual_penalty_record_stops_on_a_retired_link_to_another_binding(self):
        order = self._approved_penalty()
        other = User.objects.get(username="push-other")
        other_binding = JidanpuMemberBinding.objects.create(
            user=other, jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B, password=PASSWORD_B,
        )
        retired = self._link(binding=other_binding, order=order, remote_id="synthetic-old-store-id")
        retired.retired_at = timezone.now()
        retired.save(update_fields=["retired_at"])
        upstream = self._penalty_upstream()

        with _stub(upstream), mock.patch.object(
            jidanpu_member_views, "_run_push_outbox", side_effect=self._run_penalty_outbox,
        ):
            body = self._push([order.order_no], "record").json()

        self.assertEqual(body["data"]["skipped"], 1, body)
        self.assertIn("旧绑定可能已经录过", body["data"]["items"][0]["reason"])
        self.assertEqual(upstream.calls, [])
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=order).exists())

    def test_manual_openapi_penalty_record_sends_only_private_negative_price(self):
        order = self._approved_penalty()
        self.binding.mode = JidanpuMemberBinding.Mode.OPENAPI
        self.binding.save(update_fields=["mode"])
        client = mock.Mock()
        client.create_order.return_value = JID
        status = SimpleNamespace(creat_time="2026-01-01 12:00:00")
        client.find_order_status_with_absence.side_effect = [(None, True), (status, False), (status, False)]
        run_openapi_outbox = jidanpu_member_openapi_outbox.run_member_status_outbox

        with mock.patch.dict(os.environ, {"JIDANPU_MEMBER_OPENAPI_OUTBOX": "1"}), mock.patch.object(
            jidanpu_member_openapi_outbox, "run_member_status_outbox",
            side_effect=lambda binding, *, now: run_openapi_outbox(binding, now=now, client=client),
        ):
            body = self._push([order.order_no], "record").json()

        self.assertEqual(body["data"]["pushed"], 1, body)
        client.create_order.assert_called_once()
        sent = client.create_order.call_args.kwargs
        self.assertEqual((sent["price"], sent["assign_price"], sent["rec_user"]), (-30.00, -30.00, "0"))
        self.assertEqual(sent["source_order_id"], order.source_order_no)
        self.assertFalse(JidanpuMemberOrderLink.objects.get(binding=self.binding, order=order).in_hall)

    def test_manual_openapi_penalty_cannot_treat_a_legacy_cached_id_as_a_receipt(self):
        order = self._approved_penalty()
        self.binding.mode = JidanpuMemberBinding.Mode.OPENAPI
        self.binding.save(update_fields=["mode"])
        self._exhausted_penalty_record_with_remote_id(order, remote_id=JID)
        client = mock.Mock()
        run_openapi_outbox = jidanpu_member_openapi_outbox.run_member_status_outbox
        with mock.patch.dict(os.environ, {"JIDANPU_MEMBER_OPENAPI_OUTBOX": "1"}), mock.patch.object(
            jidanpu_member_openapi_outbox, "run_member_status_outbox",
            side_effect=lambda binding, *, now: run_openapi_outbox(binding, now=now, client=client),
        ):
            body = self._push([order.order_no], "record").json()
        self.assertEqual(body["data"]["failed"], 1, body)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=order).exists())
        self.assertEqual(client.mock_calls, [])

    # ── 权限：401 / 403 ────────────────────────────────────────────────────
    def test_anonymous_is_rejected_with_401(self):
        from django.test import Client as PlainClient

        response = PlainClient(SERVER_NAME="tenant.test.com").post(
            PUSH_URL,
            json.dumps({"order_nos": [PUSH_ORDER_NO], "action": "record"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_without_the_node_is_rejected_with_403(self):
        """节点门 = `my_orders.send_to_my_system`（与列表同一颗，⛔ 不新增节点）。
        ⚠️ 只断状态码分不出是「没登录」还是「没节点」⇒ 401 与 403 各一条（上一条 + 本条）。"""
        client = make_logged_in_client(username="push-no-node", nodes=())
        response = self._push([PUSH_ORDER_NO], "record", client=client)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            JidanpuSyncEvent.objects.exists(), "403 的请求一个事件都不许排"
        )

    # ── 无启用绑定 ⇒ 1002 人话 ─────────────────────────────────────────────
    def test_no_enabled_binding_is_1002_with_a_human_message(self):
        """没有启用中的绑定 ⇒ **1002 + 一句人话**（告诉用户去哪绑），⛔ 不是 500 / 空 msg。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        body = self._push([PUSH_ORDER_NO], "record").json()
        self.assertEqual(body["code"], 1002, body)
        self.assertIn(PUSH_NOT_BOUND_HINT, body["msg"])
        self.assertIsNone(body["data"])
        self.assertFalse(JidanpuSyncEvent.objects.exists())

    # ── record：排 member.assign，且整批只跑一轮 outbox ─────────────────────
    def test_record_queues_a_member_assign_and_settles_it(self):
        """手动录单由真实消费者核对已有的远端订单并完成。"""
        link = self._link(in_hall=False)
        sdk = mock.Mock()
        sdk.last_envelope = {"code": 0, "msg": "", "count": 1}
        sdk.call.return_value = [{
            "orderId": link.jidanpu_order_id, "orderIdView": self.order.order_no,
            "orderSta": "未分配", "isThrowOrder": "N",
        }]
        with mock.patch.object(
            jidanpu_member_views,
            "_run_push_outbox",
            side_effect=lambda binding: jidanpu_member.run_outbox(
                binding, now=timezone.now(), client=sdk,
            ),
        ) as runner:
            body = self._push([PUSH_ORDER_NO], "record").json()
        sdk.call.assert_called_once()
        self.assertEqual(sdk.call.call_args.args, ("POST", jidanpu.ORDER_LIST_PATH))
        self.assertEqual(sdk.call.call_args.kwargs["data"]["key1"], link.jidanpu_order_id)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(runner.call_count, 1, "整批只许跑一轮 outbox（⛔ 不逐单建连）")
        data = body["data"]
        self.assertEqual(
            (data["requested"], data["pushed"], data["skipped"], data["failed"]),
            (1, 1, 0, 0),
        )
        self.assertEqual(
            data["items"], [{"order_no": PUSH_ORDER_NO, "result": "done", "reason": ""}]
        )
        event = JidanpuSyncEvent.objects.get(
            kind=jidanpu_member.KIND_ASSIGN, target_ref=PUSH_ORDER_NO
        )
        self.assertEqual(event.status, "done")
        self.assertEqual(
            dict(event.payload or {}).get("member_binding_id"), self.binding.pk,
            "事件的绑定必须是**本人那一行**",
        )

    # ── status：事件带 manual、结果按台账汇总 ────────────────────────────
    def test_status_queues_a_manual_event_and_reports_the_ledger_result(self):
        """`status` ⇒ 事件 payload 带 **`manual: true`**，逐单结果按台账结论回。
        ⚠️ 这里把 `_run_push_outbox` 换成「标 done」的替身（「真的推得出去」由 consume 那两条钉住）——本类问的是**端点有没有把手动开关交下去、有没有如实汇总**。"""
        self._link()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.PENDING_REVIEW)
        with mock.patch.object(
            jidanpu_member_views,
            "_run_push_outbox",
            side_effect=_fake_run_outbox_marking_done,
        ) as runner:
            body = self._push([PUSH_ORDER_NO], "status").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(body["data"]["pushed"], 1)
        event = JidanpuSyncEvent.objects.get(
            kind=jidanpu_member.KIND_STATUS_OUT, target_ref=PUSH_ORDER_NO
        )
        self.assertIs(dict(event.payload or {}).get("manual"), True)
        self.assertEqual(
            dict(event.payload or {}).get("member_binding_id"), self.binding.pk
        )

    def test_status_without_a_link_is_skipped_with_the_existing_human_reason(self):
        """没有链路 ⇒ 这一单 `skipped` + 既有的那句「没有链路」人话，⛔ 不排空转事件。"""
        with mock.patch.object(jidanpu_member_views, "_run_push_outbox") as runner:
            body = self._push([PUSH_ORDER_NO], "status").json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual((data["skipped"], data["pushed"], data["failed"]), (1, 0, 0))
        self.assertEqual(
            self._items_by_no(body)[PUSH_ORDER_NO]["reason"], PUSH_NO_LINK_LITERAL
        )
        self.assertFalse(JidanpuSyncEvent.objects.exists(), "没有链路 ⇒ 不排事件")
        runner.assert_not_called()

    # ── 归属：不是他当前接手的单 ⇒ skipped，零事件 ──────────────────────────
    def test_an_order_that_is_not_mine_is_skipped_and_queues_nothing(self):
        with mock.patch.object(jidanpu_member_views, "_run_push_outbox") as runner:
            body = self._push([PUSH_OTHER_ORDER_NO], "record").json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual((data["skipped"], data["pushed"], data["failed"]), (1, 0, 0))
        self.assertEqual(
            self._items_by_no(body)[PUSH_OTHER_ORDER_NO]["reason"],
            PUSH_NOT_CLAIMED_LITERAL,
        )
        self.assertFalse(JidanpuSyncEvent.objects.exists(), "不是他的单不许排任何事件")
        runner.assert_not_called()

    # ── 去重保序 + 一次跑一轮 ──────────────────────────────────────────────
    def test_duplicates_are_deduped_keeping_the_input_order(self):
        """`[B, A, B]` ⇒ `requested=2`、`items` 顺序 = **首次出现**的顺序、台账两行、跑一轮。"""
        second = self._order("20261023000000009003", taker=self.user)
        with mock.patch.object(
            jidanpu_member_views,
            "_run_push_outbox",
            side_effect=_fake_run_outbox_marking_done,
        ) as runner:
            body = self._push(
                [second.order_no, PUSH_ORDER_NO, second.order_no], "record"
            ).json()
        data = body["data"]
        self.assertEqual(data["requested"], 2)
        self.assertEqual(
            [row["order_no"] for row in data["items"]],
            [second.order_no, PUSH_ORDER_NO],
        )
        self.assertEqual((data["pushed"], data["skipped"], data["failed"]), (2, 0, 0))
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=jidanpu_member.KIND_ASSIGN).count(), 2
        )

    # ── 入参拒绝：>50 / 空 / 未知动作 ⇒ 1000 ───────────────────────────────
    def test_more_than_fifty_orders_is_1000(self):
        body = self._push([f"2026{i:016d}" for i in range(51)], "record").json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("50", body["msg"])
        self.assertFalse(JidanpuSyncEvent.objects.exists())

    def test_an_empty_list_or_an_unknown_action_is_1000(self):
        self.assertEqual(self._push([], "record").json()["code"], 1000)
        self.assertEqual(self._push([PUSH_ORDER_NO], "bogus").json()["code"], 1000)
        # 空白单号不是一个单号（⛔ 静默丢掉会让页面以为推了 N 笔）
        self.assertEqual(self._push(["   "], "record").json()["code"], 1000)
        self.assertFalse(JidanpuSyncEvent.objects.exists())

    # ── 只碰「自己那一条绑定」 ──────────────────────────────────────────────
    def test_status_only_queues_for_my_own_binding(self):
        """⭐ 手动推送只推**操作人自己**绑定的下家：同一张单上别人那份绑定**不该**被排事件。
        判别力：把 `queue_status_out(..., binding=本行)` 的 `binding=` 去掉（= 给这张单**所有**有链路的绑定排事件）
        ⇒ 本用例红（会多出一行别人的事件，而那一行还会带着 `manual: true` 去改别人的下家）。"""
        self._link()
        other_user = User.objects.create_user(username="push-other-bind", password=PASSWORD)
        other_binding = JidanpuMemberBinding.objects.create(
            user=other_user,
            jidanpu_base_url=BASE_URL_B,
            account="acc-push-b",
            password=PASSWORD,
            enabled=True,
        )
        self._link(binding=other_binding, remote_id="JDP-PUSH-0002")
        with mock.patch.object(
            jidanpu_member_views,
            "_run_push_outbox",
            side_effect=_fake_run_outbox_marking_done,
        ):
            body = self._push([PUSH_ORDER_NO], "status").json()
        self.assertEqual(body["code"], 0, body)
        events = JidanpuSyncEvent.objects.filter(kind=jidanpu_member.KIND_STATUS_OUT)
        self.assertEqual(events.count(), 1, "只该给操作人自己那份绑定排事件")
        self.assertEqual(
            dict(events.first().payload or {}).get("member_binding_id"),
            self.binding.pk,
        )

    # ── 伪造请求体键：一律被忽略（判据打数据库行）─────────────────────────────
    def test_forged_user_binding_and_taker_keys_in_the_body_are_ignored(self):
        """⭐ 请求体里塞 `user` / `binding` / `taker_id` ⇒ **一律被忽略**（越权面 = 0）。形状照
        `test_cannot_touch_another_members_binding_even_with_a_forged_user_field`：判据打在**数据库行**上（事件挂在谁那份绑定下、哪条单号），⛔ 不是只看状态码。
        判别力：只要实现里读了请求体里的 `taker_id` / `binding`，事件就会落到别人那份绑定上 ⇒ 本用例红。"""
        other_user = User.objects.create_user(username="push-forged", password=PASSWORD)
        other_binding = JidanpuMemberBinding.objects.create(
            user=other_user,
            jidanpu_base_url=BASE_URL_B,
            account="acc-forged",
            password=PASSWORD,
            enabled=True,
        )
        with mock.patch.object(
            jidanpu_member_views,
            "_run_push_outbox",
            side_effect=_fake_run_outbox_marking_done,
        ):
            body = self._push(
                [PUSH_ORDER_NO],
                "record",
                extra={
                    "user": other_user.pk,
                    "binding": other_binding.pk,
                    "taker_id": other_user.pk,
                },
            ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            (body["data"]["requested"], body["data"]["pushed"]), (1, 1)
        )
        events = JidanpuSyncEvent.objects.filter(kind=jidanpu_member.KIND_ASSIGN)
        self.assertEqual(events.count(), 1)
        payload = dict(events.first().payload or {})
        self.assertEqual(payload.get("member_binding_id"), self.binding.pk)
        self.assertEqual(events.first().target_ref, PUSH_ORDER_NO)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                payload__member_binding_id=other_binding.pk
            ).exists(),
            "请求体里的 binding/taker_id 被读到了 ⇒ 事件排到别人那份绑定下",
        )

    # ── ②式绑定 + 那条线开关关着 ⇒ 逐单 failed（reason 是引擎那句 blocked_reason）──
    def test_an_openapi_binding_surfaces_the_blocked_line_reason_per_order(self):
        """②式绑定（那条出向线**默认关着**）⇒ 逐单 `failed`，`reason` = 引擎那句 `blocked_reason`。
        ⚠️ 这条钉两件事：① 端点按 `mode` 走了②式自己的入口（⛔ 不是拿①式登录去打它）；② 引擎回的 `blocked_reason` **没有被吞**
        （pending 的事件 ⇒ 逐单 failed + 那句话），页面因此能看到「为什么没推出去」。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(mode="openapi")
        self.binding.refresh_from_db()
        #: 绑定那条线的开关（字面值：⛔ 不从被测模块 import，E-0024）。
        #: 显式置空 ⇒ 与开发机 `.env` 里的取值无关（`outbox_enabled` 每次现读环境变量）。
        with mock.patch.dict(
            os.environ, {"JIDANPU_MEMBER_OPENAPI_OUTBOX": ""}, clear=False
        ):
            body = self._push([PUSH_ORDER_NO], "record").json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual((data["failed"], data["pushed"], data["skipped"]), (1, 0, 0))
        reason = self._items_by_no(body)[PUSH_ORDER_NO]["reason"]
        self.assertEqual(reason, jidanpu_member_openapi_outbox.BLOCKED_SWITCH_OFF_REASON)
        self.assertIn("默认关着", reason, "页面必须看得出是「这条线没开」，不是「对方拒了」")
        event = JidanpuSyncEvent.objects.get(
            kind=jidanpu_member.KIND_ASSIGN, target_ref=PUSH_ORDER_NO
        )
        self.assertEqual(event.status, "pending", "引擎没跑 ⇒ 事件原样留在 pending，不许改它")

    # ── 串行闸被别人占着 ⇒ 逐单 failed + 那句「已排队：…几秒后会自动推出去」──────────
    def test_a_busy_outbox_guard_surfaces_as_a_per_order_failure(self):
        """⭐ 引擎拿不到**按绑定的串行闸** ⇒ 逐单 failed。`run_outbox` 回 `blocked_reason` 而事件留在 `pending`
        ⇒ `_push_outcome` 的 pending 分支必须把它翻成 `failed` + **那句人话**（⛔ 不许 code 0 一片空白、也⛔ 不许写成「对方拒了」）；⚠️ 措辞是**排队**而不是失败：结果档仍记 `failed`（契约只有三档，⛔ 不新增第四档）——这一档的语义残差写在 `OUTBOX_BUSY_REASON` 的注释里。
        判别力：把 `_push_outcome` 的 pending 分支改成回 `skipped` / 空原因 ⇒ 本用例红。"""

        @contextmanager
        def _no_lock(binding):
            yield False

        with mock.patch.object(jidanpu_member, "_outbox_guard", _no_lock):
            body = self._push([PUSH_ORDER_NO], "record").json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual((data["failed"], data["pushed"], data["skipped"]), (1, 0, 0))
        # ⚠️ 钉**字面值**（E-0024）：拿生产常量跟它自己比，改坏了不会红。
        self.assertEqual(
            self._items_by_no(body)[PUSH_ORDER_NO]["reason"], PUSH_BUSY_LITERAL
        )
        event = JidanpuSyncEvent.objects.get(
            kind=jidanpu_member.KIND_ASSIGN, target_ref=PUSH_ORDER_NO
        )
        self.assertEqual(event.status, "pending", "拿不到闸 ⇒ 事件必须原样留在 pending")


class PushOutboxDispatchTests(TenantTestCase):
    """`_run_push_outbox` 按 `mode` 选**既有**入口。
    ⚠️ 为什么必须有这一条：②式绑定**没有账号 / 密码**，若一律调①式的 `jidanpu_member.run_outbox`，只会在建连处得到「凭据不完整」
    ⇒ 一单都推不出去却记成失败（E-0202 / spec §27.1 同一个坑）。判据只有 `mode`（⛔ 不用 `client_id` 真值反推）。"""

    def setUp(self):
        self.user = User.objects.create_user(username="push-dispatch", password=PASSWORD)
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-dispatch",
            password=PASSWORD,
            enabled=True,
        )

    def test_a_password_binding_uses_the_shared_engine(self):
        with (
            mock.patch.object(
                jidanpu_member, "run_outbox",
                return_value=jidanpu_member.OutboxStats(done=1),
            ) as shared,
            mock.patch.object(
                jidanpu_member_openapi_outbox, "run_member_status_outbox"
            ) as openapi,
        ):
            stats = jidanpu_member_views._run_push_outbox(self.binding)
        self.assertEqual(stats.done, 1)
        shared.assert_called_once()
        openapi.assert_not_called()

    def test_an_openapi_binding_uses_its_own_entry(self):
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(mode="openapi")
        self.binding.refresh_from_db()
        with (
            mock.patch.object(jidanpu_member, "run_outbox") as shared,
            mock.patch.object(
                jidanpu_member_openapi_outbox, "run_member_status_outbox",
                return_value=jidanpu_member.OutboxStats(blocked_reason="②式那条线还没开"),
            ) as openapi,
        ):
            stats = jidanpu_member_views._run_push_outbox(self.binding)
        self.assertEqual(stats.blocked_reason, "②式那条线还没开")
        openapi.assert_called_once()
        shared.assert_not_called()
