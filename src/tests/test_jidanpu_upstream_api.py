"""「我的资料 → 功能设置」的**上家鸡蛋铺绑定**三条端点判据（spec §8.4 / §9.2 / §9.3 / §11-18 / §11-19 / §11-31；换绑 / 停用 / 删号三档的判据见各用例 docstring）。
判据打**数据库行**与**桩的调用集合**（`AGENTS.md` §5-G，⛔ 不打 logger）；零真实网络：检测桩掉 `jidanpu_upstream.detect_binding`（**被查找的那个模块**，E-0029）、建连桩掉 `jidanpu.open_client_with_http`、`sync_hall` 直接喂桩客户端；夹具一律**合成值**（`AGENTS.md` §5-C / E-0077：`*.test.invalid` / `kefu-up-*` / 全零段单号）。
"""
from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db.models import ProtectedError
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import (
    jidanpu,
    jidanpu_member,
    jidanpu_openapi,
    jidanpu_openapi_consume,
    jidanpu_openapi_reconcile,
    jidanpu_upstream,
)
from apps.business.api import jidanpu_member_views, jidanpu_upstream_views
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_operation_log_ledger import INSTRUMENTED

URL = "/api/business/member/upstream-jidanpu-binding/"
TEST_URL = "/api/business/member/upstream-jidanpu-binding/test-connection/"
SYNC_NOW_URL = "/api/business/member/upstream-jidanpu-sync-now/"

#: `TenantTestCase` 的租户域名（与 `make_logged_in_client` 的默认值同源）。
TENANT_DOMAIN = "tenant.test.com"

#: 取数端点**逐字**（⛔ 不从被测模块常量派生）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"
HALL_PATH = "/OrderAjax/GetInternalOrderGrabbingList"

BASE_URL = "https://jidanpu-upstream-api.test.invalid"
BASE_URL_B = "https://jidanpu-upstream-api-b.test.invalid"
ACCOUNT = "kefu-up-api"
ACCOUNT_B = "kefu-up-api-b"
ACCOUNT_NEXT = "kefu-up-api-next"
PASSWORD = "pw-canary-up-api"
PASSWORD_B = "pw-canary-up-api-b"
NEXT_USERNAME = "kefu-up-api-next-user"

#: ②式「立即同步」那一组用例的绑定人（⛔ 与本文件别处那个用户名不同：同一个用例里
#: `make_logged_in_client` 只建一次用户）。
SYNC_NOW_OPENAPI_USERNAME = "kefu-syncnow-openapi"

ORDER_NO = "20260928000000000711"
ORDER_NO_B = "20260928000000000712"
REMOTE_ID = "20260928000000000711"
REMOTE_ID_B = "20260928000000000712"

#: 检测失败的人话（**字面量**，⛔ 不从被测模块常量派生——自指断言不会红，E-0024）。
LOGIN_FAILURE = "登录失败：账号或密码错误"
#: 换绑时收敛在飞事件的人话原因要点名「人工核对」。
REBIND_REASON_HINT = "人工核对"
#: 审计句子里显示的站点名（去协议头、去尾斜杠）——⛔ 不是账号、更不是密码。
HOST_NAME = "jidanpu-upstream-api.test.invalid"
CREATE_ACTION = "system.jidanpu_upstream_binding_create"
UPDATE_ACTION = "system.jidanpu_upstream_binding_update"

#: 远端那一格实际下发的是**中文标签**（spec §5.7 实测）。
REMOTE_ISSUE = "问题单"
REMOTE_PENDING_REVIEW = "待验收"

#: 「本轮有失败行」时的人话（**字面量**，⛔ 不从被测模块常量派生——自指断言不会红，E-0024）。
SYNC_NOW_FAILED_MESSAGE = "有 1 条回拉失败，请稍后重试"

#: ⚠️ **在夹具重绑之前**抓下的两线**真实模块级**闸门实例：`JidanpuUpstreamBindingApiTests.setUp` 会把
#: `jidanpu_upstream_views._SYNC_NOW_GATE` 换成新实例 ⇒ 拿重绑后的值做 `assertIsNot` 会被夹具**自己**保证（恒真）。
#: `test_the_sync_now_gate_is_not_shared_with_the_downstream_line` 钉「两线各自的模块级闸门不是同一对象」⇒ 必须在**导入那一刻**（任何 `setUp` 之前）各存一份快照。
UPSTREAM_GATE_AT_IMPORT = jidanpu_upstream_views._SYNC_NOW_GATE
MEMBER_GATE_AT_IMPORT = jidanpu_member_views._SYNC_NOW_GATE


def _post_function(source_path, class_name):
    """取某个视图类 `post` 方法的 AST 节点（⛔ 不认文档字符串 / 注释里的同名文本）。"""
    tree = ast.parse(Path(source_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "post":
                    return sub
    return None


def _calls_module_function(node, module_name, function_name) -> bool:
    """`node` 的函数体里有没有 `module_name.function_name(...)` 这一次调用（按 AST 判）。
    ⚠️ 必须同时钉接收者模块：只收集 `.attr` 的话，反方向线那个同名的 `jidanpu_member.pull_statuses` 照样算通过。
    """
    if node is None:
        return False
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == function_name
            and isinstance(func.value, ast.Name)
            and func.value.id == module_name
        ):
            return True
    return False


def _detect(ok, problems=()):
    """把 `jidanpu_upstream.detect_binding` 换成固定结论的桩。"""
    return mock.patch.object(
        jidanpu_upstream,
        "detect_binding",
        return_value=jidanpu_upstream.DetectionReport(ok=ok, problems=tuple(problems)),
    )


class _MyOrdersStub:
    """「我的订单」取数桩（SDK 边界）：按这一份行集合回包，并记录调用集合。"""

    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {"code": 0, "msg": "success", "data": [], "count": 0}

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs)))
        if self.error is not None:
            raise self.error
        assert path == MY_ORDERS_PATH, path
        rows = self.rows if int(kwargs["query"]["page"]) == 1 else []
        self.last_envelope = {
            "code": 0, "msg": "success", "data": list(rows), "count": len(self.rows),
        }
        return list(rows)


class _HallStub:
    """大厅取数桩：第 1 页回这一份行，之后一律空页（探页那一跳）。"""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {"code": 0, "msg": "success", "data": list(self.rows),
                                    "count": len(self.rows)}

    def call(self, method, path, **kwargs):
        page = kwargs["query"]["page"]
        self.calls.append((method, path, dict(kwargs)))
        assert path == HALL_PATH, path
        rows = list(self.rows) if page == 1 else []
        self.last_envelope = {"code": 0, "msg": "success", "data": rows, "count": len(self.rows)}
        return rows


def _connection(stub):
    """把 `jidanpu.open_client_with_http` 换成「产出这个桩」的上下文管理器（零真实网络）。"""

    @contextmanager
    def fake_open(binding):
        yield stub, None

    return fake_open


class JidanpuUpstreamBindingApiTests(TenantTestCase):
    def setUp(self):
        #: 频控状态是**进程内**的 ⇒ 每个用例换一个新的闸门，免得上一个用例的退避串味。
        jidanpu_upstream_views._SYNC_NOW_GATE = jidanpu_member_views._SyncNowGate()
        #: ⚠️ 节点 `order.change_status` 是**状态回拉写回**那一跳的门（本文件只测「本人那一行的链路真的被回拉」）。
        #: ⚠️ `_grant_nodes` 只往**已有 Group** 上挂节点 ⇒ 必须同时给一个组
        #:（⛔ 只给 nodes 会静默一个节点都没挂上，症状是「回拉被拒」而看起来像实现坏了）。
        self.client = make_logged_in_client(
            username=ACCOUNT, groups=("客服",), nodes=("order.change_status",),
        )
        self.user = User.objects.get(username=ACCOUNT)
        self.other_client = make_logged_in_client(username=ACCOUNT_B)
        self.other = User.objects.get(username=ACCOUNT_B)
        # ⭐ 审计日志线（spec §3.3）：人为操作的正文会带 `【显示名】`，而夹具**故意**让上家账号与操作人登录名同串（`ACCOUNT = "kefu-up-api"`）
        # ⇒ 给两个操作人各一个**昵称**，让正文里出现的是**人**、不是被配置的上家账号。
        # ⛔ 这不是放宽判据：`test_no_credential_field_is_ever_returned` 的 `assertNotIn(ACCOUNT, row.summary)` 一个字没动，操作人登录名照旧只落在 `actor` 列上。
        for user, nickname in ((self.user, "测试客服甲"), (self.other, "测试客服乙")):
            user.first_name = nickname
            user.save(update_fields=["first_name"])
        self.game = Game.objects.create(name="合成游戏甲")
        self.now = timezone.now()
        #: 当前登录者那一行 = 本地址**最早**的一份（A2 的 owner）。
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
        )
        #: 计划里 A2 那一档的 helper 名（`self.older` = 最早的、要停掉的那一份）。
        self.older = self.binding
        #: 同地址**下一份**启用绑定（停掉 `self.older` 之后轮到它）。
        next_user = User.objects.create_user(username=NEXT_USERNAME, password="pw-next")
        self.newer = JidanpuUpstreamBinding.objects.create(
            user=next_user, jidanpu_base_url=BASE_URL, account=ACCOUNT_NEXT,
            password=PASSWORD_B, enabled=True,
        )

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _put(self, *, client=None, **overrides):
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": ACCOUNT,
            "password": PASSWORD,
            "enabled": False,
        }
        payload.update(overrides)
        return (client or self.client).put(
            URL, json.dumps(payload), content_type="application/json"
        )

    def _create_row(self, **overrides):
        """落一行属于**当前登录者**的绑定（默认走端点新建）。"""
        res = self._put(**overrides)
        self.assertEqual(res.json()["code"], 0, res.content)
        return JidanpuUpstreamBinding.objects.get(user=self.user)

    def _link(self, order, *, remote_id=REMOTE_ID, binding=None):
        return JidanpuUpstreamOrderLink.objects.create(
            binding=binding or self.binding, order=order, upstream_order_id=remote_id,
        )

    def _order(self, *, order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS, sales_user=None):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=status,
            hall_price="12.34", total_price="50.00", project_text="合成项目甲",
            sales_user=sales_user,
        )

    def make_mirror_row(self, *, owner):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=REMOTE_ID,
            owner_binding=owner, status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game, first_seen_at=self.now, last_seen_at=self.now,
        )

    def make_in_flight_event(self, binding, *, kind="upstream.complete"):
        return JidanpuSyncEvent.objects.create(
            direction="out", kind=kind, target_type="order", target_ref=ORDER_NO,
            status=JidanpuSyncEvent.Status.PENDING,
            payload={"upstream_binding_id": binding.pk},
        )

    def disable(self, binding):
        """走**端点**把这一份停掉（`enabled=False`；检测那一跳不会被跑）。"""
        self.assertEqual(binding.pk, self.binding.pk, "本 helper 只服务当前登录者那一行")
        res = self._put(enabled=False)
        self.assertEqual(res.json()["code"], 0, res.content)
        binding.refresh_from_db()
        self.assertIs(binding.enabled, False)

    def run_one_round(self):
        """停用之后由**下一份启用绑定**跑一轮大厅镜像（A2 的 owner 轮换靠它）。"""
        stub = _HallStub([{"orderId": REMOTE_ID, "gameName": self.game.name,
                           "recFee": "88.00"}])
        return jidanpu_upstream.sync_hall(binding=self.newer, client=stub, now=self.now)

    def alert_like_rows(self):
        """「该客服的在途单需要人工接手」那类提示 / 告警台账的行数。
        ⚠️ 关键词是保守枚举（⛔ 没有「理由里含某个词就永久豁免」那种后门）：业务方对 A17 的答复是「**不用管**」⇒ 这一档一行都不该新增。
        """
        keywords = ("接手", "转给", "告警")
        return sum(
            1 for reason in JidanpuSyncEvent.objects.values_list("reason", flat=True)
            if any(word in (reason or "") for word in keywords)
        )

    def owner_of_the_mirror(self):
        return JidanpuUpstreamHallOrder.objects.get().owner_binding_id

    # ── 读：单行资源 + 健康状态，⛔ 不回显密码 ──────────────────────────────

    def test_unbound_get_returns_null_envelope_not_404(self):
        """没绑定时 `data: null`（**不是** 404）——「还没绑」是正常状态，不是错误。"""
        res = self.other_client.get(URL)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["code"], 0)
        self.assertIsNone(body["data"])

    def test_get_returns_my_own_row_and_the_health_columns(self):
        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["jidanpu_base_url"], BASE_URL)
        self.assertEqual(data["account"], ACCOUNT)
        self.assertIs(data["enabled"], True)
        self.assertEqual(data["hall_poll_seconds"], 30)
        self.assertEqual(data["my_orders_poll_seconds"], 60)
        #: ⭐ 口径修订：勾兑百分比是本资源的**可读可写**配置列（默认 100）。
        self.assertEqual(data["hall_price_percent"], 100)
        for key in ("checked_at", "last_run_at", "last_ok_at", "last_error", "last_error_at"):
            self.assertIn(key, data)
        self.assertIs(data["password_set"], True)
        # 密码是 write_only ⇒ **键本身**都不进响应（spec §11-18）。
        self.assertNotIn("password", data)

    def test_only_my_own_row_is_readable_and_writable(self):
        """⭐ spec §11-19：本人那一行才是这一对端点的对象——B 读到的是 `null`（判据打在行上）；B 的 PUT 带 `user=A` 也只落 B 自己那一行。
        ⛔ 另两条线的行**读不到**——本资源只认 `JidanpuUpstreamBinding`（同一 URL 下三套表零关系，spec §4.1）：租户级 `JidanpuBinding` 已随该线拆除、那一行造不出来，夹具只留**存活**的反方向 `JidanpuMemberBinding` 一行当对照（⛔ 下面「读到的是自己 / `null` / 字段不串线」的断言没有放宽）。"""
        # 另一条**存活**线的行也在 A 名下（同一个 URL 的租户里）：⛔ 一个字段都不许串进来。
        JidanpuMemberBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL_B, account="member-acc",
            password="pw-member", source_name="团团通系统",
        )

        data = self.client.get(URL).json()["data"]
        self.assertEqual(data["account"], ACCOUNT)
        for foreign in ("source_name", "fast_poll_seconds", "reconcile_seconds",
                        "throw_price_percent"):
            self.assertNotIn(foreign, data, foreign)

        body = self.other_client.get(URL).json()
        self.assertIsNone(body["data"], "B 读到了 A 的绑定行——读取没有按 request.user 收敛")

        res = self._put(
            client=self.other_client, jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B,
            password=PASSWORD_B, user=self.user.pk,
        )
        self.assertEqual(res.json()["code"], 0, res.content)
        row_a = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertEqual((row_a.account, row_a.password, row_a.jidanpu_base_url),
                         (ACCOUNT, PASSWORD, BASE_URL))
        row_b = JidanpuUpstreamBinding.objects.get(user=self.other)
        self.assertEqual((row_b.account, row_b.jidanpu_base_url), (ACCOUNT_B, BASE_URL_B))
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), 3)  # 含 setUp 的 newer
        self.assertEqual(
            self.other_client.get(URL).json()["data"]["jidanpu_base_url"], BASE_URL_B
        )

    # ── 凭据 canary：任何响应体里都搜不到密码 ──────────────────────────────

    def test_no_credential_field_is_ever_returned(self):
        """⭐ spec §11-18：搜**响应字节**（不是「看起来没有」）。"""
        create = self._put(password=PASSWORD)
        self.assertNotIn(PASSWORD, create.content.decode(), create.content)
        read = self.client.get(URL)
        self.assertNotIn(PASSWORD, read.content.decode(), read.content)
        update = self._put(password=PASSWORD_B)
        self.assertNotIn(PASSWORD_B, update.content.decode(), update.content)

        with _detect(False, problems=(LOGIN_FAILURE,)):
            checked = self.client.post(TEST_URL)
        self.assertNotIn(PASSWORD_B, checked.content.decode(), checked.content)

        with _detect(True):
            enabled = self._put(enabled=True, password=PASSWORD_B)
        self.assertNotIn(PASSWORD_B, enabled.content.decode(), enabled.content)

        # 操作日志那一行同样不许出现密码（⛔ 账号也不许——审计句子只说站点地址）。
        for row in OperationLog.objects.all():
            self.assertNotIn(PASSWORD_B, row.summary)
            self.assertNotIn(PASSWORD, row.summary)
            self.assertNotIn(ACCOUNT, row.summary)
            self.assertNotIn(PASSWORD_B, row.target_name or "")

    # ── 写：新建 / 覆盖 + 与业务写入同事务的操作日志 ────────────────────────

    def test_put_creates_a_row_and_logs_the_create_action(self):
        """⭐ `_create` 那一支：没有行 ⇒ 建行 + `..._create`（登记表装不下它，单测直接钉）。"""
        res = self._put(
            client=self.other_client, jidanpu_base_url=BASE_URL_B,
            account=ACCOUNT_B, password=PASSWORD_B,
        )
        self.assertEqual(res.json()["code"], 0, res.content)
        binding = JidanpuUpstreamBinding.objects.get(user=self.other)
        self.assertEqual(binding.user_id, self.other.pk)
        self.assertEqual(binding.password, PASSWORD_B)
        log = OperationLog.objects.get()
        self.assertEqual(log.action, CREATE_ACTION)
        self.assertEqual(log.actor, ACCOUNT_B)
        self.assertEqual(log.actor_id, self.other.pk)
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.summary, "【测试客服乙】新增上家鸡蛋铺绑定：jidanpu-upstream-api-b.test.invalid")

    def test_put_overwrites_the_same_row_and_blank_password_keeps_the_old_one(self):
        """换账号 = 覆盖同一行；密码留空 = 不改（页面回显不了密码，留空只能是「不动它」）。"""
        before = JidanpuUpstreamBinding.objects.count()
        self._put(account="kefu-up-api-other", password="")
        self.assertEqual(JidanpuUpstreamBinding.objects.count(), before)
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertEqual(row.account, "kefu-up-api-other")
        self.assertEqual(row.password, PASSWORD)
        log = OperationLog.objects.order_by("id").last()
        self.assertEqual(log.action, UPDATE_ACTION)
        self.assertEqual(log.summary, f"【测试客服甲】编辑上家鸡蛋铺绑定：{HOST_NAME}")

    def test_the_ledger_declares_the_update_action_the_view_actually_writes(self):
        """把「登记表声明」与「视图真写的两个动作」钉在一起（一个 PUT 两个分支两个动作）。"""
        entry = INSTRUMENTED["apps/business/api/jidanpu_upstream_views.py"]
        self.assertEqual(
            entry[("put", "api/business/member/upstream-jidanpu-binding/")], UPDATE_ACTION
        )
        self._put(client=self.other_client, jidanpu_base_url=BASE_URL_B,
                  account=ACCOUNT_B, password=PASSWORD_B)
        self.assertEqual(OperationLog.objects.order_by("id").last().action, CREATE_ACTION)
        self._put(client=self.other_client, jidanpu_base_url=BASE_URL_B,
                  account=ACCOUNT_B + "-2", password=PASSWORD_B)
        self.assertEqual(OperationLog.objects.order_by("id").last().action, UPDATE_ACTION)

    def test_a_bad_address_is_rejected_and_nothing_is_written(self):
        res = self._put(jidanpu_base_url="no-scheme.test.invalid")
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, res.content)
        self.assertEqual(JidanpuUpstreamBinding.objects.filter(
            jidanpu_base_url="no-scheme.test.invalid").count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_the_poll_seconds_boundary_is_pinned_on_both_sides(self):
        """⭐ 间隔边界（spec §10；默认值现由 `UPSTREAM_HALL_POLL_SECONDS` 管辖、见 models.py）：0 秒没有意义 ⇒ **被拒且点名到那一格**。
        ⚠️ 0 在闸门那一侧是「配置写坏 ⇒ 按本列默认值兜底」，⛔ 不是「每轮都跑」⇒ 端点这一层必须挡住它（E-0010 / E-0021 同族）；本用例钉的是边界 0 被拒，不是默认值本身。
        """
        self._create_row()
        wants = {
            "hall_poll_seconds": "上家大厅拉取间隔必须不小于 1 秒",
            "my_orders_poll_seconds": "「我的订单」拉取间隔必须不小于 1 秒",
        }
        for field, want in wants.items():
            with self.subTest(field=field):
                res = self._put(**{field: 0})
                self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, res.content)
                # ⚠️ 文案要**点名到那一格**（不带字段名的「该值必须大于…」用户看不出是哪一格）。
                self.assertIn(want, res.json()["msg"])
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertEqual((row.hall_poll_seconds, row.my_orders_poll_seconds), (30, 60))
        # 上界也是边界：能放行一个正常值（⛔ 别把 min_value 写成恒拒）。
        self._put(hall_poll_seconds=60, my_orders_poll_seconds=1)
        row.refresh_from_db()
        self.assertEqual((row.hall_poll_seconds, row.my_orders_poll_seconds), (60, 1))

    # ── ⭐ 口径修订（业务方）：勾兑百分比（判据 ④ / ⑦）────────────

    def test_the_percent_boundary_is_pinned_on_both_sides(self):
        """⭐ 判据 ④（**端点层**）：0 / 101 两支都要被拒、**点名到那一格**、且一行都不落。
        ⛔ 0 = 一张免费单发进我方大厅（与 `LAUNCH_NO_PRICE_REASON` 同族）、>100 = 我方倒贴；显式声明的 `IntegerField` 不继承模型校验器 ⇒ 少了这里的范围，越界值会落到 PostgreSQL `smallint` 上抛 `DataError` → 500（E-0010）。
        """
        self._create_row()
        for bad in (0, 101):
            with self.subTest(percent=bad):
                res = self._put(hall_price_percent=bad)
                self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, res.content)
                # ⚠️ 文案要点名到那一格（不带字段名的「该值必须…」用户看不出是哪一格）。
                self.assertIn("发单价百分比", res.json()["msg"])
        self.assertEqual(
            JidanpuUpstreamBinding.objects.get(user=self.user).hall_price_percent, 100
        )
        # 两个端点值都要放行（⛔ 别把范围写成恒拒 —— 那会让下一条用例的正面路径也死）。
        for good in (1, 100):
            with self.subTest(percent=good):
                res = self._put(hall_price_percent=good)
                self.assertEqual(res.json()["code"], 0, res.content)
                self.assertEqual(
                    JidanpuUpstreamBinding.objects.get(
                        user=self.user
                    ).hall_price_percent,
                    good,
                )

    def test_the_percent_range_is_pinned_in_the_model_too(self):
        """⭐ 判据 ④（**模型层**）：`full_clean()` 对 0 / 101 都要抛，1 / 100 都要放行。
        ⚠️ 这一层**不是**入口的替代（`save()` / `update()` 不跑它）——守的是「将来多一个写入点」那条路（E-0021 同族）。失败方向：把校验器摘掉 ⇒ 本用例红。
        """
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        for bad in (0, 101):
            with self.subTest(percent=bad):
                row.hall_price_percent = bad
                with self.assertRaises(ValidationError):
                    row.full_clean()
        for good in (1, 100):
            with self.subTest(percent=good):
                row.hall_price_percent = good
                row.full_clean()          # 不抛就是放行（⛔ 别把校验器写成恒拒）
        row.hall_price_percent = 100
        row.save(update_fields=["hall_price_percent"])

    def test_put_writes_the_percent_column_without_clobbering_health_columns(self):
        """⭐ 判据 ⑦：PUT 能改这个新列，且**窄写**（健康列不被打回请求开始时的旧值）。
        两个方向各钉一半：① `hall_price_percent` **在** `_PUT_WRITE_FIELDS` 里 ⇒ 真的落库；② 落库仍是 `save(update_fields=…)` ⇒ 检测那次网络往返期间同步引擎写的 `last_ok_at` 不被整行 `save()` 覆盖回去（照既有 lost-update 用例的口径）。
        """
        self._create_row()
        binding = JidanpuUpstreamBinding.objects.get(user=self.user)
        stamped = timezone.now()

        def detect_and_stamp(*args, **kwargs):
            JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_ok_at=stamped)
            return jidanpu_upstream.DetectionReport(ok=True)

        with mock.patch.object(jidanpu_upstream, "detect_binding",
                               side_effect=detect_and_stamp):
            res = self._put(hall_price_percent=50, enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        binding.refresh_from_db()
        self.assertEqual(binding.hall_price_percent, 50)     # ① 新列真的能改
        self.assertEqual(binding.last_ok_at, stamped)        # ② 并发写的健康列没被抹掉
        self.assertEqual(res.json()["data"]["hall_price_percent"], 50)

    def test_put_does_not_clobber_health_columns_written_concurrently(self):
        """⭐ lost update：`enabled=True` 的检测是一次网络往返，期间同步引擎可能写健康列。
        失败方向：落库改回整行 `save()` ⇒ `last_ok_at` 被覆盖回请求开始时的旧值（这里是 `None`）。"""
        self._create_row()
        binding = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertIsNone(binding.last_ok_at)
        stamped = timezone.now()

        def detect_and_stamp(*args, **kwargs):
            JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_ok_at=stamped)
            return jidanpu_upstream.DetectionReport(ok=True)

        with mock.patch.object(jidanpu_upstream, "detect_binding",
                               side_effect=detect_and_stamp):
            res = self._put(account="kefu-up-api-stamped", enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        binding.refresh_from_db()
        self.assertEqual(binding.account, "kefu-up-api-stamped")
        self.assertIs(binding.enabled, True)
        self.assertEqual(binding.last_ok_at, stamped)

    # ── 启用的机械落实：检测不过 ⇒ 一行都不落；过 ⇒ 落库 + `checked_at` ──────

    def test_put_enabling_without_passing_detection_is_refused(self):
        """⭐ spec §9.2：检测不过 ⇒ 不许 `enabled=True`，且**一行都不落**、文案点名缺项。"""
        with _detect(False, problems=(LOGIN_FAILURE,)):
            res = self._put(enabled=True)
        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn(LOGIN_FAILURE, body["msg"])
        self.assertFalse(JidanpuUpstreamBinding.objects.filter(user=self.other).exists())
        self.assertEqual(OperationLog.objects.count(), 0)

    def test_put_enabling_without_passing_detection_leaves_the_existing_row_untouched(self):
        """已有行 + 检测不过 ⇒ 覆盖**不发生**：账号、开关、`checked_at` 全是旧值。"""
        with _detect(False, problems=(LOGIN_FAILURE,)):
            res = self._put(account="kefu-up-api-should-not-land", enabled=True)
        self.assertNotEqual(res.json()["code"], 0, res.content)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account, ACCOUNT)
        self.assertIs(self.binding.enabled, True)
        self.assertIsNone(self.binding.checked_at)

    def test_put_enabling_passing_detection_persists_and_stamps_checked_at(self):
        self.binding.checked_at = None
        self.binding.save(update_fields=["checked_at"])
        with _detect(True) as detect:
            res = self._put(enabled=True)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.binding.refresh_from_db()
        self.assertIs(self.binding.enabled, True)
        self.assertIsNotNone(self.binding.checked_at)
        # 检测读的是**这一行**（本人那一行），⛔ 不是请求体里的任何东西。
        checked = detect.call_args.args[0]
        self.assertIsInstance(checked, JidanpuUpstreamBinding)
        self.assertEqual(checked.user_id, self.user.pk)
        self.assertEqual(checked.account, ACCOUNT)

    def test_put_enabling_without_credentials_never_calls_the_detector(self):
        """地址 / 账号 / 密码不全 ⇒ 直接回「还没配置」，⛔ 不去打上游。"""
        with _detect(True) as detect:
            res = self._put(
                client=self.other_client, jidanpu_base_url=BASE_URL_B,
                account=ACCOUNT_B, password="", enabled=True,
            )
        body = res.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("还没配置", body["msg"])
        self.assertFalse(detect.called, "凭据不全时不得调用检测")
        self.assertFalse(JidanpuUpstreamBinding.objects.filter(user=self.other).exists())

    # ── 测试连接：回人话、⛔ 不落库 ────────────────────────────────────────

    def test_test_connection_reports_failures_without_writing_anything(self):
        before = (self.binding.checked_at, self.binding.last_error, self.binding.enabled,
                  self.binding.updated_at)
        with _detect(False, problems=(LOGIN_FAILURE,)):
            body = self.client.post(TEST_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["ok"], False)
        self.assertEqual(body["data"]["messages"], [LOGIN_FAILURE])
        self.binding.refresh_from_db()
        self.assertEqual(
            (self.binding.checked_at, self.binding.last_error, self.binding.enabled,
             self.binding.updated_at),
            before, "测试连接是只读的：一行都不许写",
        )

    def test_test_connection_reports_success_with_no_messages(self):
        with _detect(True):
            body = self.client.post(TEST_URL).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["ok"], True)
        self.assertEqual(body["data"]["messages"], [])

    def test_test_connection_without_a_binding_is_a_named_failure(self):
        body = self.other_client.post(TEST_URL).json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertIn("还没配置", body["msg"])
        self.assertFalse(JidanpuUpstreamBinding.objects.filter(user=self.other).exists())

    def test_test_connection_only_ever_reads_my_own_row(self):
        """B 点测试连接 ⇒ 拿的必须是 B 自己那一行（B 没有 ⇒ 不许拿 A 的去检测）。"""
        with _detect(False, problems=(LOGIN_FAILURE,)) as detect:
            self.other_client.post(TEST_URL)
        self.assertFalse(detect.called, "B 还没有绑定 ⇒ 不该拿 A 的行去检测")

    # ── 「立即同步」：只动本人链路、与慢路同一段代码、会话级节流 ─────────────

    def test_sync_now_only_touches_my_own_links(self):
        """⭐ `pull_statuses` 的扫描面按 `binding` 收敛：远端那份行里有 B 的单也没关系。
        判据打在**数据库行**上：A 的单被回拉推进（代练中 → 问题单 ⇒ 状态机收「问题单」），B 的单与链路**一个字节都不动**（⛔ 不是「没报错」）。"""
        mine = self._order()
        link_a = self._link(mine)
        theirs = self._order(order_no=ORDER_NO_B)
        other_binding = JidanpuUpstreamBinding.objects.create(
            user=self.other, jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B,
            password=PASSWORD_B, enabled=True,
        )
        link_b = self._link(theirs, remote_id=REMOTE_ID_B, binding=other_binding)

        stub = _MyOrdersStub([
            {"orderId": REMOTE_ID, "orderSta": REMOTE_ISSUE},
            {"orderId": REMOTE_ID_B, "orderSta": REMOTE_ISSUE},
        ])
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            body = self.client.post(SYNC_NOW_URL).json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        link_a.refresh_from_db()
        link_b.refresh_from_db()
        self.assertEqual(mine.status, OrderStatus.ISSUE)
        self.assertEqual(theirs.status, OrderStatus.IN_PROGRESS)
        self.assertIsNotNone(link_a.last_in_at)
        self.assertIsNone(link_b.last_in_at, "B 的链路被 A 的那一跳碰了")

    def test_sync_now_is_scoped_to_my_own_row_even_with_forged_body_fields(self):
        """请求体里塞别人的 id / `user` ⇒ 被忽略（行主只来自 `request.user`）。"""
        stub = _MyOrdersStub([])
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            res = self.other_client.post(
                SYNC_NOW_URL,
                json.dumps({"user": self.user.pk, "binding_id": self.binding.pk}),
                content_type="application/json",
            )
        # B 没有绑定 ⇒ 直接「还没配置」；A 那一行不许被这一跳动到。
        self.assertNotEqual(res.json()["code"], 0, res.content)
        self.assertEqual(stub.calls, [])

    def test_sync_now_a_disabled_binding_is_refused(self):
        self.binding.enabled = False
        self.binding.save(update_fields=["enabled"])
        stub = _MyOrdersStub([])
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            body = self.client.post(SYNC_NOW_URL).json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertEqual(stub.calls, [])

    def test_sync_now_without_a_binding_is_a_named_failure(self):
        stub = _MyOrdersStub([])
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            body = self.other_client.post(SYNC_NOW_URL).json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertTrue(body["msg"])
        self.assertEqual(stub.calls, [])

    def test_the_second_click_inside_the_interval_does_not_reach_upstream(self):
        """同会话最小间隔（⛔ 不许连点把上游刷爆）。
        ⚠️ 一轮是**两发**（① 窗口 + ② 同窗口「已退单」）⇒ 桩的调用数是 **2**（⛔ 不是「整表 1 发」：窗口拉取下一轮就是两发）。"""
        mine = self._order()
        self._link(mine)
        stub = _MyOrdersStub([{"orderId": REMOTE_ID, "orderSta": REMOTE_ISSUE}])
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            first = self.client.post(SYNC_NOW_URL)
            second = self.client.post(SYNC_NOW_URL)
        self.assertEqual(first.json()["code"], 0, first.content)
        self.assertEqual(second.json()["code"], ErrorCode.BUSINESS, second.content)
        self.assertIn("太频繁", second.json()["msg"])
        self.assertEqual(len(stub.calls), 2)

    def test_a_round_already_in_flight_is_refused(self):
        """在途互斥：同一会话上一轮还没跑完 ⇒ 这一跳不打上游。
        ⚠️ 键用 patch 固定，好让「占住窗」与「视图取窗」是同一个键（真实实现里键 = 会话）。"""
        gate = jidanpu_upstream_views._SYNC_NOW_GATE
        stub = _MyOrdersStub([])
        with mock.patch.object(jidanpu_upstream_views, "_sync_now_key", return_value="k"):
            self.assertTrue(gate.acquire("k")[0])
            with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
                body = self.client.post(SYNC_NOW_URL).json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS, body)
        self.assertIn("正在同步", body["msg"])
        self.assertEqual(stub.calls, [])

    def test_an_upstream_failure_makes_the_next_click_wait(self):
        """失败退避：上游炸了 ⇒ 这一跳把人话回给页面，紧接着再点被退避挡住。"""
        from jidanpu.errors import ApiError

        mine = self._order()
        self._link(mine)
        stub = _MyOrdersStub(error=ApiError("合成：上游炸了", code=None))
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            failed = self.client.post(SYNC_NOW_URL)
            again = self.client.post(SYNC_NOW_URL)
        self.assertNotEqual(failed.json()["code"], 0, failed.content)
        self.assertTrue(failed.json()["msg"])
        self.assertEqual(again.json()["code"], ErrorCode.BUSINESS, again.content)
        self.assertIn("失败", again.json()["msg"])
        self.assertEqual(len(stub.calls), 1)

    def test_a_failed_round_is_reported_and_the_next_click_backs_off(self):
        """⭐ 远端**一行都没有** ⇒ 那一轮**不抛异常**，但 `pull_statuses` 为这一行写了一条 `failed` 台账 ⇒ 视图必须据此把它当**失败**：① 信封回业务失败码 +「有 N 条回拉失败」（⛔ 不是 `code 0` + 一片空白——那是「成功但什么都没发生」）；② 闸门按**退避阶梯**挡住紧接着的第二次点击（⛔ 只剩 2 秒最小间隔兜底）。
        ⚠️ 本线**不写健康列** ⇒ 「这一轮失败了没有」只能看**本轮结果**（`PullRoundOutcome.failed`），⛔ 不能拿台账里有没有 `failed` 行判——`note_once` 按原因去重，第二轮回**写不出新行**，退避阶梯会只生效一次。
        失败方向（实测读数）：把视图里「记为失败」那一步改成恒假 ⇒ 第二次点击只剩 2 秒最小间隔、回的信封**不含「失败」**⇒ 本用例红（第一次的信封仍由 `outcome.failed` 判、照样对 ⇒ 这条用例的判别力**落在②**）。"""
        mine = self._order()
        self._link(mine)
        stub = _MyOrdersStub([])  # 远端一行都没有 ⇒ **不抛异常**的预期失败
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(stub)):
            first = self.client.post(SYNC_NOW_URL)
            calls_after_first = len(stub.calls)
            second = self.client.post(SYNC_NOW_URL)

        body = first.json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertEqual(body["msg"], SYNC_NOW_FAILED_MESSAGE, body)
        self.assertEqual(body["data"]["failed"], 1, body)
        self.assertEqual(body["data"]["applied"], 0, body)
        # 台账那一行照旧留着（失败要留痕），订单状态没被动。
        notes = JidanpuSyncEvent.objects.filter(target_ref=ORDER_NO)
        self.assertEqual(notes.count(), 1, "失败的留痕行数不对")
        self.assertEqual(notes.get().status, JidanpuSyncEvent.Status.FAILED)
        mine.refresh_from_db()
        self.assertEqual(mine.status, OrderStatus.IN_PROGRESS)

        # ② 第二次点击**被退避挡住**：桩的调用数不增加（⛔ 不是「没报错」）。
        # ⚠️ 一轮共 **4 发**：① 窗口 + ② 同窗口「已退单」+ ③ 这张单的 `key1` 单查——③ 是**两问**
        #    （默认视图问不到 ⇒ 再问一次「`key1` + `orderSta=已退单`」）；都回空集 ⇒ ③ 的「读不到」正是这一轮的失败。
        self.assertEqual(calls_after_first, 4, stub.calls)
        self.assertEqual(second.json()["code"], ErrorCode.BUSINESS, second.content)
        self.assertIn("失败", second.json()["msg"])
        self.assertEqual(len(stub.calls), calls_after_first, "第二次点击没有被退避挡住——又真打了一次上游")

    def test_the_sync_now_gate_is_not_shared_with_the_downstream_line(self):
        """⭐ 两条线各有各的节流窗：本线点一下**不许**把反方向线的「立即同步」一起挡住。
        ⚠️ 判据打在**导入那一刻抓下的真实模块属性**（`UPSTREAM_GATE_AT_IMPORT` / `MEMBER_GATE_AT_IMPORT`）上，⛔ **不是** `setUp` 重绑之后的值——那一步每例都塞新实例，会让 `assertIsNot` 被夹具自己保证、**恒真**。
        失败方向：源码那一行写成 `_SYNC_NOW_GATE = jidanpu_member_views._SYNC_NOW_GATE` ⇒ 两个快照是**同一个对象** ⇒ 本用例红。"""
        self.assertIsNot(UPSTREAM_GATE_AT_IMPORT, MEMBER_GATE_AT_IMPORT)

    def test_the_endpoint_calls_the_slow_path_function_itself(self):
        """⭐ spec §9.3：与 §5.3 的慢路**同一段代码**（`jidanpu_upstream.pull_statuses`）——⛔ 不许在视图里另写一份「查状态 / 写状态」，那会跟慢路**静默**分叉（症状：页面上点得动、定时那一轮却不一样）。
        ⚠️ 判据必须**同时钉住模块与调用点**：只收集 `.attr` 的话，反方向线那个**同名**函数 `jidanpu_member.pull_statuses` 照样算通过——`tests/test_operation_log_ledger.py` 的覆盖率网豁免正是引用本文件这条 AST 判据。
        ⇒ 这里只认 `JidanpuUpstreamSyncNowView.post` 函数体里、接收者是 `jidanpu_upstream` 的那一跳；失败方向：把调用改成 `jidanpu_member.pull_statuses`（同名换模块）⇒ 本用例红。"""
        post = _post_function(
            jidanpu_upstream_views.__file__, "JidanpuUpstreamSyncNowView"
        )
        self.assertIsNotNone(post, "JidanpuUpstreamSyncNowView.post 不见了")
        self.assertTrue(
            _calls_module_function(post, "jidanpu_upstream", "pull_statuses"),
            "post 里没有调用 jidanpu_upstream.pull_statuses",
        )

    # ── 三档：换绑 / 停用 / 删号（spec §8.4；⛔ 最容易漏，单独一组）────────────

    def test_a_rebind_converges_in_flight_events_to_skipped(self):
        """⭐ spec §8.4 第 1 行：换账号 / 换地址 ⇒ 在飞事件收敛成 **`skipped`**（⛔ 不是 `failed`：那会让「换绑 → 停用 → 再启用」拿**新凭据**去重试旧事件）+ 人话原因（⛔ 不静默）。
        ⚠️ 同时钉住边界：`direction="in"` 的**审计台账**（`upstream.status_in`）哪怕状态列也叫 `failed`，也**一个都不许动**——它不是待办，动它就是把审计改成假话。"""
        mine = self._order()
        link = self._link(mine)
        binding_id = self.binding.pk
        pending = self.make_in_flight_event(self.binding, kind="upstream.complete")
        failed = self.make_in_flight_event(self.binding, kind="upstream.hall_claim")
        done = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", target_type="order",
            target_ref=ORDER_NO, status=JidanpuSyncEvent.Status.DONE,
            payload={"upstream_binding_id": binding_id},
        )
        other_binding_event = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", target_type="order",
            target_ref=ORDER_NO, status=JidanpuSyncEvent.Status.PENDING,
            payload={"upstream_binding_id": binding_id + 999},
        )
        ledger = JidanpuSyncEvent.objects.create(
            direction="in", kind="upstream.status_in", target_type="order",
            target_ref=ORDER_NO, status=JidanpuSyncEvent.Status.FAILED,
            reason="合成：回拉被状态机拒绝", payload={"upstream_binding_id": binding_id},
        )

        with _detect(True):
            res = self._put(account="kefu-up-api-rebound", password=PASSWORD_B)
        self.assertEqual(res.json()["code"], 0, res.content)

        pending.refresh_from_db()
        failed.refresh_from_db()
        done.refresh_from_db()
        other_binding_event.refresh_from_db()
        ledger.refresh_from_db()
        self.assertEqual(pending.status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertEqual(failed.status, JidanpuSyncEvent.Status.SKIPPED)
        self.assertIn(REBIND_REASON_HINT, pending.reason)
        self.assertEqual(failed.reason, pending.reason)
        self.assertTrue(pending.processed_at)
        self.assertEqual(done.status, JidanpuSyncEvent.Status.DONE)
        self.assertEqual(other_binding_event.status, JidanpuSyncEvent.Status.PENDING)
        self.assertEqual(ledger.status, JidanpuSyncEvent.Status.FAILED)
        self.assertEqual(ledger.reason, "合成：回拉被状态机拒绝")
        # 链路行**保留**（它是历史事实），⛔ 不拿新凭据去改旧单。
        self.assertIsNotNone(JidanpuUpstreamOrderLink.all_objects.get(pk=link.pk).retired_at)
        self.assertFalse(JidanpuUpstreamOrderLink.objects.filter(pk=link.pk).exists())
        self.binding.refresh_from_db()
        self.assertIsNone(
            self.binding.checked_at, "换了凭据之后，上一份凭据的检测结论不再成立"
        )

    def test_old_order_list_response_is_discarded_after_rebinding(self):
        row = {
            "orderId": REMOTE_ID, "orderIdView": "synthetic-old-store-view-no",
            "orderSta": "代练中", "gameName": self.game.name, "recFee": "80.00",
        }
        stub = _MyOrdersStub([row])
        call = stub.call

        def fetch_then_rebind(method, path, **kwargs):
            rows = call(method, path, **kwargs)
            if len(stub.calls) == 1:
                with _detect(True):
                    response = self._put(jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B, enabled=True)
                self.assertEqual(response.json()["code"], 0, response.content)
            return rows

        stub.call = fetch_then_rebind
        outcome = jidanpu_upstream.sync_my_orders(binding=self.binding, client=stub, now=self.now)

        self.assertEqual(outcome.recorded, 0)
        self.assertFalse(Order.objects.exists())
        self.binding.refresh_from_db()
        self.assertIsNone(self.binding.my_orders_progress)

        # 旧会话落后的重试不会去读上游，也不能覆盖新身份的进度。
        stub.calls.clear()
        binding = JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)
        binding.identity_revision -= 1
        stale = jidanpu_upstream.sync_my_orders(binding=binding, client=stub, now=self.now)
        self.assertFalse(stale.read)
        self.assertEqual(stub.calls, [])

    def test_old_hall_response_cannot_add_a_card_to_the_new_binding(self):
        stub = _HallStub([{"orderId": REMOTE_ID, "gameName": self.game.name, "recFee": "80.00"}])
        call = stub.call

        def fetch_then_rebind(method, path, **kwargs):
            rows = call(method, path, **kwargs)
            if len(stub.calls) == 1:
                with _detect(True):
                    response = self._put(jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B, enabled=True)
                self.assertEqual(response.json()["code"], 0, response.content)
            return rows

        stub.call = fetch_then_rebind
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)

        self.assertEqual(outcome.listed, 0)
        self.assertFalse(JidanpuUpstreamHallOrder.objects.exists())

    def test_a_rebound_account_neither_uses_old_pictures_nor_queues_old_completion(self):
        order = self._order()
        link = self._link(order)
        event, _ = jidanpu_upstream.queue_pull_pics_request(
            order=order, link=link, requested_by=self.user,
        )
        response = self._put(jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B)
        self.assertEqual(response.json()["code"], 0, response.content)
        with _detect(True):
            response = self._put(jidanpu_base_url=BASE_URL_B, account=ACCOUNT_B, enabled=True)
        self.assertEqual(response.json()["code"], 0, response.content)
        self.binding.refresh_from_db()
        stub = _MyOrdersStub()
        jidanpu_upstream.run_pull_pics_requests(self.binding, now=self.now, client=stub)
        event.refresh_from_db()
        self.assertEqual((event.status, stub.calls), ("skipped", []))

        order.status = OrderStatus.COMPLETED
        order.save(update_fields=["status"])
        self.assertEqual(jidanpu_upstream.queue_complete(order), 0)

    def test_disabling_hands_the_hall_mirror_over_to_the_next_binding(self):
        """⭐ spec §8.4 / A2：停掉同地址最早那份 ⇒ 镜像行的 `owner_binding` 轮到下一份启用绑定。"""
        mirror = self.make_mirror_row(owner=self.older)
        self.disable(self.older)

        outcome = self.run_one_round()

        self.assertTrue(outcome.is_owner, outcome.reason)
        mirror.refresh_from_db()
        self.assertEqual(mirror.owner_binding_id, self.newer.pk)

    def test_disabling_does_not_converge_in_flight_events(self):
        """⭐ spec §8.4 第 2 行：`enabled=False` **没换凭据** ⇒ 只是把开关关掉，⛔ **不收敛在飞事件**（凭据没变，再启用该继续；收敛它会让待办静默作废）。
        ⚠️ 判据是**端点级**、走 PUT 的**窄写**那一支（同一份凭据 + 只改间隔 + 关开关）；失败方向：把 `rebinding` 判据放宽成「停用也当换绑」（`or not binding.enabled`）⇒ 在飞事件被写成 `skipped` ⇒ 本用例红。⛔ 「停用」与「换绑」是两档：判据只认 `previous_credentials != 新凭据`。"""
        pending = self.make_in_flight_event(self.binding)
        res = self._put(enabled=False, hall_poll_seconds=120)
        self.assertEqual(res.json()["code"], 0, res.content)
        self.binding.refresh_from_db()
        self.assertIs(self.binding.enabled, False)
        self.assertEqual(self.binding.hall_poll_seconds, 120)  # 窄写真的落了库

        pending.refresh_from_db()
        self.assertEqual(
            pending.status, JidanpuSyncEvent.Status.PENDING,
            "停用把在飞事件收敛掉了——凭据没变，再启用本该继续",
        )
        self.assertIsNone(pending.processed_at)
        self.assertEqual(pending.reason, "")

    def test_deleting_the_user_is_blocked_by_a_historical_order_that_credits_them(self):
        """⭐ spec §8.4 第 3 行 / `Order.sales_user` 的 `PROTECT`：**删号不许带走历史订单的提成归属**——那会把已结算的销售提成变成无主数据（钱的凭据被删）。
        ⚠️ `test_deleting_the_user_does_not_take_the_ledger_or_the_mirror_away` 的 `_order()` **刻意不给 `sales_user`**、根本碰不到 PROTECT（把 `on_delete` 改 `CASCADE` 也全绿）；本用例把 `sales_user` 指到该客服、再走**同一个删号路径**（`user.delete()`）⇒ 必须抛 `ProtectedError`，且订单行与归属**原样还在**。"""
        mine = self._order(sales_user=self.user)
        with self.assertRaises(ProtectedError):
            self.user.delete()
        mine.refresh_from_db()
        self.assertEqual(mine.sales_user_id, self.user.pk, "历史订单的提成归属被带走了")
        self.assertTrue(Order.objects.filter(pk=mine.pk).exists())
        # 反方向：绑定行确实还在（`ProtectedError` 让整笔删除**一步都没提交**）。
        self.assertTrue(JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).exists())

    def test_deleting_the_user_does_not_take_the_ledger_or_the_mirror_away(self):
        """spec §8.4 第 3 行：**台账与镜像行活着**（前者无 FK、后者 `SET_NULL`）；
        ⚠️ **链路随绑定一起消失**（`CASCADE`，它是跟踪结构）——这三条 `on_delete` 是刻意的差别。
        """
        mirror = self.make_mirror_row(owner=self.binding)
        mine = self._order()
        self._link(mine)
        self.make_in_flight_event(self.binding)
        events = JidanpuSyncEvent.objects.count()

        self.binding.user.delete()

        self.assertEqual(JidanpuSyncEvent.objects.count(), events)  # 台账留着
        mirror.refresh_from_db()
        self.assertIsNone(mirror.owner_binding_id)                 # 镜像行留着、owner 置空
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 0)  # 链路随绑定消失
        self.assertFalse(JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).exists())

    def test_disabling_never_invents_a_handover_prompt(self):
        """⭐ spec §11-31 / A17 / §16.3（业务方答「**不用管**」）：停用一个绑定的**前后**，⛔ **不许**新增任何指向「该客服的在途单需要人工接手」的提示 / 告警（⛔ 不许自己发明「转给谁」的机制）；同时**保留** A2 的 owner 轮换。"""
        self.make_mirror_row(owner=self.older)
        before_alerts = self.alert_like_rows()
        before_rows = JidanpuSyncEvent.objects.count()

        self.disable(self.binding)
        self.run_one_round()

        self.assertEqual(self.alert_like_rows(), before_alerts)
        #: 更强的一侧：这一档**一行台账都不该新增**（「不用管」= 没有新东西要人看）。
        self.assertEqual(JidanpuSyncEvent.objects.count(), before_rows)
        self.assertEqual(self.owner_of_the_mirror(), self.newer.pk)  # ← A2 那条照旧成立


class JidanpuUpstreamBindingAnonymousTests(TenantTestCase):
    """「没权限不行」这一档：本线三条端点**不新增 RBAC 节点**，全靠 fail-closed 的默认门。
    ⚠️ 匿名请求必须显式打在**租户域名**上：`TenantTestCase` 的 `self.client` 用默认 `SERVER_NAME="testserver"` ⇒ `TenantMainMiddleware` 在进 urlconf 之前就抛 `Http404`（HTML 响应）——那会让本条即使端点没挂门也红、判据失去判别力（同型约定见 `tests/test_my_profile_api.py`）。
    ⚠️ **跨租户**这一档在本线是**结构性不可达**的（不是没测）：一租户一库（D-0137），认证态没有跨库通道，四条端点一律 `filter(user=request.user)` ⇒ 一个租户的凭据进不了另一个租户的库；库/表级隔离由 `tests/test_jidanpu_isolation.py` 钉，本文件用「A 读不到 / 改不到 B 的行」与「请求体塞 `user` 被忽略」把它钉在端点上。"""

    def test_every_endpoint_refuses_anonymous_before_the_view_body(self):
        anon = Client(SERVER_NAME=TENANT_DOMAIN)
        cases = (
            ("get", URL, None),
            ("put", URL, {}),
            ("post", TEST_URL, None),
            ("post", SYNC_NOW_URL, None),
        )
        for method, url, body in cases:
            with self.subTest(method=method, url=url):
                if body is None:
                    res = getattr(anon, method)(url)
                else:
                    res = getattr(anon, method)(
                        url, json.dumps(body), content_type="application/json"
                    )
                self.assertEqual(res.status_code, 401, (method, url, res.content))
                # ⛔ 一行都不许落（连「还没配置」那种业务答复都不该出现）。
                self.assertEqual(JidanpuUpstreamBinding.objects.count(), 0)



# ②式夹具（`OPENAPI_*` / `OUTER_GATE_MSG` / `_OpenApiUpstream` 等那几件）已整块搬到中立的 `tests/_openapi_fixtures.py`，两条线各自 import 那一份：
# ⛔ 不要在别处再抄一份假上游（两份桩必然漂）；⛔ 不要再往本模块搬回来。
# ⚠️ 本模块**不持有**任何②式夹具（本节的 `WITHDRAWN_*` 是**撤除判据**自己的夹具，不是②式的）。


# ②式（官方开放平台 API）**已从「上家」这一侧撤除**（业务方裁定：绑上家用的是**别人家**的鸡蛋铺账号，手里不可能有别人的 API 密钥 ⇒ 这种对接只对「自己那套」下家线有意义；撤除**不丢真实数据**——两台生产机上②式上家绑定一条都没有）。⛔ ①式那一整套（`JidanpuUpstreamBindingApiTests`）**一条都没删**。
# 本节钉三件事（**都能红**）：① 写入口**不再接受**②式那三格；② 读响应**不再回** `client_secret_set` / `callback_url` / `callback_url_reason`；③ ①式那两支**逐字不变**（检测仍走 `detect_binding`、同步仍走登录连接 + `pull_statuses`，⛔ 一次都不碰官方 API 那两段）。
# ⭐ **「PUT 塞 `mode=openapi`」的口径 = 静默忽略**（⛔ 不拒、⛔ 也不改库）：DRF `ModelSerializer` 对不在 `fields` 里的键本就静默忽略，而「拒」要么改掉①式既有 PUT 的行为面、要么给已撤功能留代码 ⇒ 唯一与「①式逐字不变」相容；⛔ 不许是「静默成功」的假话——用例钉住信封 `code==0` + 库里三格没写 + 响应无那三个键。

#: 撤除判据里的合成夹具（⛔ 一个真值都没有）。
WITHDRAWN_CLIENT_ID = "cid-synthetic-withdrawn-0001"
WITHDRAWN_SENTINEL_SECRET = "sentinel-secret-must-not-be-accepted-0001"

#: ⛔ 撤除后**响应里连键都不许有**的那六格（`mode` 也一起：它已不在契约里）。
WITHDRAWN_RESPONSE_KEYS = (
    "mode", "client_id", "client_secret", "client_secret_set",
    "callback_url", "callback_url_reason",
)


class JidanpuUpstreamOpenApiWithdrawnTests(TenantTestCase):
    """②式撤除后的**接口面**判据（⛔ 与①式那套分开住，免得夹具互相串味）。"""

    def setUp(self):
        #: 频控状态是**进程内**的 ⇒ 每个用例换一个新的闸门（照①式那个类的同一条口径）。
        jidanpu_upstream_views._SYNC_NOW_GATE = jidanpu_member_views._SyncNowGate()
        self.client = make_logged_in_client(
            username=ACCOUNT, groups=("客服",), nodes=("order.change_status",),
        )
        self.user = User.objects.get(username=ACCOUNT)

    def _put(self, **overrides):
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": ACCOUNT,
            "password": PASSWORD,
            "enabled": False,
        }
        payload.update(overrides)
        return self.client.put(URL, json.dumps(payload), content_type="application/json")

    # ── ① 写入口不再接受②式那三格（口径：静默忽略）─────────────────────────

    def test_a_put_declaring_openapi_is_silently_ignored_and_writes_nothing_extra(self):
        """⭐ **判据（撤除）**：往 PUT 里塞 `mode=openapi` + `client_id` + `client_secret` ⇒ **静默忽略**：信封 `code==0`（⛔ 不报错）、库里那三格**一格都没被写**（⛔ 不改库）、响应里**连键都没有**（⛔ 不给「已保存成官方API」的假象）。
        ⛔ 弄红它（三种改法都能红）：① 把 `mode` / `client_id` / `client_secret` 加回 `JidanpuUpstreamBindingSerializer.Meta.fields`（或回 `_PUT_WRITE_FIELDS`）⇒ 中间那三条「库里一格没变」红；② 改成「收到就报错」（`mode` 加回必填 `ChoiceField` / 在 `validate()` 里拒）⇒ 第一条 `code==0` 红；③ 让响应回 `mode` / `client_secret_set` ⇒ 最后那一段「响应里没有这些键」红。"""
        res = self._put(
            mode="openapi",
            client_id=WITHDRAWN_CLIENT_ID,
            client_secret=WITHDRAWN_SENTINEL_SECRET,
        )
        body = res.json()
        #: ⓵ ⛔ 不报错（DRF 对不在 `fields` 里的键就是静默忽略）。
        self.assertEqual(body["code"], 0, res.content)
        #: ⓶ ⛔ 不改库：那三格**一格都没被写**。
        row = JidanpuUpstreamBinding.objects.get(user=self.user)
        self.assertEqual(row.mode, JidanpuUpstreamBinding.Mode.PASSWORD)
        self.assertEqual(row.client_id, "")
        self.assertEqual(row.client_secret, "")
        #: ⓷ ⛔ 响应里连键都没有（⛔ 不许出现「已保存成官方API」那种假话）。
        for key in WITHDRAWN_RESPONSE_KEYS:
            self.assertNotIn(key, body["data"], f"撤除后响应里仍然有 `{key}`")
        #: ⓸ ⛔ 密钥一个字节都不许出现在响应里（连库里都没进，更不该回出去）。
        self.assertNotIn(WITHDRAWN_SENTINEL_SECRET, res.content.decode())
        #: ⓹ ①式那几格**照旧**被写进去了（⛔ 不是「整条 PUT 被静默丢掉」那种更坏的结果）。
        self.assertEqual(row.jidanpu_base_url, BASE_URL)
        self.assertEqual(row.account, ACCOUNT)
        self.assertTrue(row.password)

    def test_the_read_response_no_longer_carries_the_withdrawn_keys(self):
        """⭐ **判据（撤除）**：读响应（GET）里**没有** `mode` / `client_id` / `client_secret_set` / `callback_url` / `callback_url_reason`。
        ⛔ 弄红它：把 `mode`（或 `client_secret_set` / `callback_url`）加回 `Meta.fields` ⇒ 本用例红。"""
        self._put()
        data = self.client.get(URL).json()["data"]
        for key in WITHDRAWN_RESPONSE_KEYS:
            self.assertNotIn(key, data, f"撤除后读响应里仍然有 `{key}`")

    # ── ② ①式那两支逐字不变：⛔ 一次都不碰官方 API 那两段 ──────────────────

    def test_test_connection_never_goes_through_the_openapi_probe(self):
        """⭐ **判据（①式逐字不变）**：「测试连接」走 `jidanpu_upstream.detect_binding`，⛔ `jidanpu_openapi.probe_read_endpoints` 一次都不许被调（那一支已撤）。
        ⚠️ 两个桩都返回**真实形状**的结论（⛔ 不返回裸 `MagicMock`：裸桩漏进 `api_response` 会让响应渲染**挂住**，而「挂住」不算「弄红」——§5-G）；夹具那行**故意留一格 `client_id`**（那一列没删）⇒ 同时钉住「⛔ 不许用 `client_id` 非空去反推②式」（E-0021 家族）。
        ⛔ 弄红它：把「按 `binding.client_id` 非空分流」的写法（或 `is_openapi_binding` 那一支）加回「测试连接」⇒ 本用例红（`probe.called` 为真）。"""
        JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=False,
            #: ⭐ 库里**留着**一格 `client_id`（那一列没删，见模块 docstring 的裁定块）。
            client_id=WITHDRAWN_CLIENT_ID,
        )
        with mock.patch.object(jidanpu_upstream, "detect_binding") as legacy, \
                mock.patch.object(jidanpu_openapi, "probe_read_endpoints") as probe:
            legacy.return_value = jidanpu_upstream.DetectionReport(ok=True, problems=())
            body = self.client.post(TEST_URL).json()
        self.assertTrue(legacy.called, "①式必须走登录式检测")
        self.assertFalse(probe.called, "②式那一支已撤 ⇒ ⛔ 不许再走官方 API 探测")
        self.assertEqual(body["data"], {"ok": True, "messages": []})

    def test_sync_now_never_runs_the_openapi_rounds(self):
        """⭐ **判据（①式逐字不变）**：「立即同步」走**登录连接 + `pull_statuses`**，⛔ 官方 API 那两段（回调消费 / 兜底对账）一次都不许被调。
        ⚠️ 夹具那行**故意留一格 `client_id`**（那一列没删）⇒ 同时钉住「⛔ 不许用 `client_id` 非空去反推②式」（E-0021 家族）。
        ⛔ 弄红它：把「按 `binding.client_id` 非空分流」的写法（或 `is_openapi_binding` 那一支）加回「立即同步」⇒ 那两段之一被调 ⇒ 本用例红。"""
        binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, enabled=True,
            #: ⭐ 库里**留着**一格 `client_id`（那一列没删，见模块 docstring 的裁定块）。
            client_id=WITHDRAWN_CLIENT_ID,
        )
        outcome = jidanpu_upstream.PullRoundOutcome(applied=1, failed=0)
        with mock.patch.object(jidanpu, "open_client_with_http", _connection(_MyOrdersStub())), \
                mock.patch.object(jidanpu_upstream, "pull_statuses",
                                  return_value=outcome) as pull, \
                mock.patch.object(jidanpu_openapi_consume, "consume_callbacks") as consume, \
                mock.patch.object(jidanpu_openapi_reconcile,
                                  "reconcile_openapi_statuses") as reconcile:
            body = self.client.post(SYNC_NOW_URL).json()
        self.assertTrue(pull.called, "①式这一跳必须走 `pull_statuses`")
        self.assertFalse(consume.called, "②式那一支已撤 ⇒ ⛔ 不许再跑回调消费")
        self.assertFalse(reconcile.called, "②式那一支已撤 ⇒ ⛔ 不许再跑兜底对账")
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1, body)
        self.assertTrue(binding.enabled)

    # ── ③ 从被撤掉的那一组里**搬过来**的①式回归（⛔ 不是新加的放宽）──────────

    def test_password_mode_still_requires_the_account(self):
        """①式没有账号 ⇒ 仍然拒绝（这一条一个字都没放松）：②式撤除后「放宽账号必填」不复存在，但「①式必须有账号」照旧承重（⛔ 不是放宽、也不是新语义）。"""
        res = self._put(account="")
        self.assertEqual(res.json()["code"], ErrorCode.VALIDATION, res.content)
        self.assertIn("账号", res.json()["msg"])
