"""上家鸡蛋铺线（客服个人绑定）：绑定期**四项只读检测** + 上传页隐藏域取值（计划 Task 3）。

被检的四件事（spec §9.2，判据以它为准）：① 能登录（严格模式）；② 能读上家**抢单大厅**
（`GET /OrderAjax/GetInternalOrderGrabbingList` 第 1 页）；③ 能读**「我的订单」**
（`GET /OrderAjax/GetMyOrderList`）；④ **能取到店铺标识**——`sid` / `gameAccId` / `taskId`
三个值**全部**从上传页 `GET /Order/UploadPic` 的**隐藏域**读出来（⭐ spec §2.4 / T7 / G3：
✅ 已实测闭合的取法，⛔ 一个值都不许硬编码、⛔ 不许猜位置）。

⚠️ **证明力边界（照 spec §9.2 如实登记）**：这四项**只覆盖「读」**。三个写端点
（`RecOrderByInternal` / `SetOrderSta` / `UploadOrderPic`）**没有页面可探、也不许拿真实动作去试**
⇒ 本文件里那条「检测不碰任何写端点」的用例只证明**检测期**没写；写失败要响亮报错是
Task 8 / Task 9 的责任。
⚠️ `detect_binding` 在**登录失败时短路**（照反方向线 `jidanpu_member.detect_binding` 的同一口径）：
没有会话时后面三项拿到的只会是「请先登录」/登录页 302 ⇒ 那是**检测本身失败**，
把它们说成「没权限」会让人去改一个没坏的权限设置。
⚠️ 桩分两个注入口（`client=` 打 SDK 边界、`http=` 打 HTTP 边界）：真实形态里
`jidanpu.open_client_with_http` 一次产出这两样。⛔ 桩不接真网络。
⚠️ **②③ 的成功码是我们自己判的（`code == 0`）**：SDK 的宽松解包把 `code > 0` 一律判成功 ⇒
失败信封（非空 `msg` + 空 `data` + `code != 0`）**不抛异常**、会误绿。所以另有一条
**真 SDK + `MockTransport`** 的用例真的过信封层（⛔ 不是桩抛异常伪造的）。
⚠️ **上传页按状态码分档**：只有 `200`（值缺/空）与 `302` 到无权限页 ⇒ 第 4 项的人话；
连接失败 / `5xx` / `302` 到登录页 ⇒ 检测本身失败、**响亮上行**（⛔ 不伪装成「上家没给店铺标识」）。
⚠️ 文案期望是**测试里的字面量**，⛔ 不从被测常量派生（E-0024：自指断言在被测常量被改坏时不会红）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `upstream-a`、
密码 `pw-canary-a`、单号 `20260928000000000123`（全零段，一眼是合成值）、`sid` 用 `10001` /
`20002` 两个**不同**的合成值（换一个值也必须跟着变 ⇒ 钉死「读出来的、不是写死的」）。
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import (
    ApiError,
    AuthError,
    ConfigError,
    JidanpuError,
    RateLimitError,
    ServerError,
    SessionExpiredError,
    TransportError,
)

from apps.business import jidanpu, jidanpu_upstream
from apps.business.models import JidanpuUpstreamBinding, OrderStatus

BASE_URL = "https://jidanpu-upstream-a.test.invalid"
ACCOUNT = "upstream-a"
PASSWORD = "pw-canary-a"

#: 端点路径**逐字**抄 spec §2.1 / §2.3 / §2.4（⛔ 不从模块常量派生）。
HALL_PATH = "/OrderAjax/GetInternalOrderGrabbingList"
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"
REC_ORDER_PATH = "/OrderAjax/RecOrderByInternal"
SET_STATUS_PATH = "/OrderAjax/SetOrderSta"
UPLOAD_PIC_PATH = "/OrderAjax/UploadOrderPic"
VIEW_PIC_PATH = "/OrderAjax/ViewPic"
OSS_POLICY_PATH = "/CommonPermissionAjax/GetAliOssPolicy"
UPLOAD_PAGE_PATH = "/Order/UploadPic"
#: 无权限页（spec §2.3 **实测**：302 跳到它）。
NO_PERMISSION = "/ErrorPage/nopermission"
#: 登录页那一跳（造「302 不是去无权限页」这条噪点）。
LOGIN_REDIRECT = "/Login/Index"

UPSTREAM_ORDER_ID = "20260928000000000123"
UPSTREAM_ORDER_ID_B = "20260928000000000124"
SID_SYNTH = "10001"
SID_SYNTH_B = "20002"

# ── 期望文案（字面量，独立于实现；spec §9.2 的四行人话）─────────────────────────
LOGIN_FAILURE = "登录失败：账号或密码错误"
HALL_FAILURE = "该账号打不开上家的抢单大厅，请让上家给它开这个权限"
MY_ORDERS_FAILURE = "该账号打不开上家的『我的订单』，请让上家给它开这个权限"
SID_FAILURE = "取不到上家的店铺标识，截图功能会失败，请先联系上家确认"

_MISSING = object()


def _upload_page_html(*, sid=SID_SYNTH, game_acc_id="0", task_id="0",
                      order_id=UPSTREAM_ORDER_ID):
    """合成上传页：隐藏域里带 `orderId` / `gameAccId` / `taskId` / `sid`（spec §2.4 的形状）。

    ⚠️ 属性顺序**故意打乱**、`sid` 那个还写成单引号无自闭合形态 ⇒ 一并钉住解析器
    不依赖书写顺序 / 引号风格。传 `None` 的那个键就**不渲染**（造「缺项」）。
    """
    parts = ["<html><body><form method=\"post\">"]
    if order_id is not None:
        parts.append(f'<input type="hidden" name="orderId" value="{order_id}" />')
    if game_acc_id is not None:
        parts.append(f"<input name='gameAccId' type='hidden' value='{game_acc_id}'>")
    if task_id is not None:
        parts.append(f'<input type="hidden" value="{task_id}" name="taskId"/>')
    if sid is not None:
        parts.append(f"<input value='{sid}' name='sid' type='hidden'>")
    # 一个可见输入：⛔ 不许被当成隐藏域读进来。
    parts.append('<input type="text" name="remark" value="合成备注" />')
    parts.append("</form></body></html>")
    return "".join(parts)


class _StubClient:
    """`JidanpuClient` 的替身：按端点回包 / 抛错，并记录调用集合。

    - `login_error`：`ensure_login()` 要抛的异常（造登录失败）。
    - `hall` / `hall_error`：大厅第 1 页的行 / 要抛的异常。
    - `my_pages` / `my_error` / `omit_my_count` / `my_forever`：我的订单逐页回包的形状。
    """

    def __init__(self, *, login_error=None, hall=None, hall_error=None, my_pages=None,
                 my_error=None, omit_my_count=False, my_forever=False, my_count=None,
                 omit_hall_count=False):
        self.login_error = login_error
        self.hall_rows = list(hall if hall is not None else [{"orderId": UPSTREAM_ORDER_ID}])
        self.hall_error = hall_error
        self.omit_hall_count = omit_hall_count
        self.my_pages = [list(page) for page in (my_pages or [[{"orderId": UPSTREAM_ORDER_ID}]])]
        self.my_error = my_error
        self.omit_my_count = omit_my_count
        self.my_forever = my_forever
        self.my_count = my_count
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}
        self.logins = 0

    def ensure_login(self):
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == HALL_PATH:
            if self.hall_error is not None:
                raise self.hall_error
            self.last_envelope = {"code": 0, "msg": "success", "data": self.hall_rows}
            if not self.omit_hall_count:
                self.last_envelope["count"] = len(self.hall_rows)
            return list(self.hall_rows)
        if path == MY_ORDERS_PATH:
            if self.my_error is not None:
                raise self.my_error
            page = int(kwargs["query"]["page"])
            if self.my_forever:
                rows = [{"orderId": f"{UPSTREAM_ORDER_ID}-{page}"}]
                total = _MISSING
            elif page <= len(self.my_pages):
                rows = list(self.my_pages[page - 1])
                total = sum(len(p) for p in self.my_pages)
            else:
                rows = []
                total = sum(len(p) for p in self.my_pages)
            if self.my_count is not None:
                total = self.my_count
            self.last_envelope = {"code": 0, "msg": "success", "data": rows}
            if total is not _MISSING and not self.omit_my_count:
                self.last_envelope["count"] = total
            return list(rows)
        raise AssertionError(f"未预期的端点：{method} {path}")


class _StubHttp:
    """探页面用的 **HTTP 边界**替身（`probe_pages` 那个形状：只看状态码 / `Location`）。

    `error=` 让 `get()` 抛一个 `httpx` 异常（造「连接失败」那一档）。
    """

    def __init__(self, *, status=200, html=None, location="", error=None):
        self.status = status
        self.html = _upload_page_html() if html is None else html
        self.location = location
        self.error = error
        self.gets: list[tuple[str, dict]] = []

    def get(self, path, **kwargs):
        self.gets.append((path, kwargs))
        if self.error is not None:
            raise self.error
        if self.status != 200:
            headers = {"Location": self.location} if self.location else {}
            return httpx.Response(self.status, headers=headers, text="<html>合成错误页</html>")
        return httpx.Response(200, text=self.html)


#: 真 `httpx.Client`。模块级取好，这样下面那个工厂替换 `jidanpu.httpx.Client` 之后
#: 仍能造出**真的**客户端（只是 transport 换成 `httpx.MockTransport`）。
_REAL_HTTPX_CLIENT = httpx.Client


def _http_factory(handler):
    """把 `jidanpu.httpx.Client(...)` 换成「真客户端 + MockTransport」⇒ **零网络**。"""

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


class UpstreamDetectTests(TenantTestCase):
    def setUp(self):
        user = User.objects.create_user(username=ACCOUNT, password="pw-a")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account="acc-a", password=PASSWORD,
        )

    def _detect(self, *, client=None, http=None):
        return jidanpu_upstream.detect_binding(
            self.binding,
            client=_StubClient() if client is None else client,
            http=_StubHttp() if http is None else http,
        )

    # ── 常量与纯函数（钉「值」，⛔ 不钉常量名）───────────────────────────────

    def test_the_endpoint_constants_are_the_verbatim_ones(self):
        """五个端点路径逐字钉住（spec §2.1 / §2.3 / §2.4）：⛔ 拼错一个就是一次静默失败。"""
        self.assertEqual(jidanpu_upstream.HALL_PATH, HALL_PATH)
        self.assertEqual(jidanpu_upstream.MY_ORDERS_PATH, MY_ORDERS_PATH)
        self.assertEqual(jidanpu_upstream.REC_ORDER_PATH, REC_ORDER_PATH)
        self.assertEqual(jidanpu_upstream.UPLOAD_PIC_PATH, UPLOAD_PIC_PATH)
        self.assertEqual(jidanpu_upstream.VIEW_PIC_PATH, VIEW_PIC_PATH)
        self.assertEqual(jidanpu_upstream.OSS_POLICY_PATH, OSS_POLICY_PATH)
        self.assertEqual(jidanpu_upstream.UPLOAD_PAGE_PATH, UPLOAD_PAGE_PATH)
        # ⛔ 提交完成**复用**既有常量，不复制字面量（计划的 Interfaces）。
        self.assertEqual(jidanpu.SET_STATUS_PATH, SET_STATUS_PATH)

    def test_machine_actor_is_the_planned_value(self):
        self.assertEqual(jidanpu_upstream.MACHINE_ACTOR, "上家鸡蛋铺同步")

    def test_status_text_is_the_order_status_label(self):
        """状态码 → 中文 label 的**唯一出处**是 `OrderStatus(value).label`（⛔ 不另写映射表）。"""
        self.assertEqual(jidanpu_upstream.status_text(OrderStatus.IN_PROGRESS), "代练中")
        self.assertEqual(jidanpu_upstream.status_text("REFUNDED"), "已退单")
        self.assertEqual(jidanpu_upstream.status_text(OrderStatus.UNASSIGNED), "未分配")
        with self.assertRaises(ValueError):
            jidanpu_upstream.status_text("NOT_A_STATUS")

    # ── 四项：正常一遍过 ────────────────────────────────────────────────────

    def test_a_healthy_binding_passes_all_four_and_only_reads(self):
        """四项全过 ⇒ `ok=True` / `problems=()`，且只登录一次、只打两个读端点 + 一次上传页。"""
        client = _StubClient()
        http = _StubHttp()
        report = jidanpu_upstream.detect_binding(self.binding, client=client, http=http)
        self.assertTrue(report.ok)
        self.assertEqual(report.problems, ())
        self.assertEqual(client.logins, 1)
        self.assertEqual(
            [(method, path) for method, path, _kw in client.calls],
            [("GET", HALL_PATH), ("GET", MY_ORDERS_PATH)],
        )
        # 大厅第 1 页、⛔ 不带 `gameName`（GC 22 / §16.1：简称会让上游静默返回 0 行）。
        self.assertEqual(client.calls[0][2]["query"], {"page": 1})
        # 探上传页：给 `orderId`、且**不跟 302**（跟了就看不到无权限页）。
        self.assertEqual([path for path, _kw in http.gets], [UPLOAD_PAGE_PATH])
        self.assertEqual(http.gets[0][1]["params"], {"orderId": UPSTREAM_ORDER_ID})
        self.assertIs(http.gets[0][1]["follow_redirects"], False)

    # ── 四项：逐条点名（含登录短路）─────────────────────────────────────────

    def test_four_problems_are_named_one_by_one(self):
        """四条判据各自失败时都要**点名是哪一项**（人话里含该项的关键词）。"""
        scenarios = {
            "登录": (_StubClient(login_error=AuthError("合成：登录被拒")), _StubHttp()),
            "抢单大厅": (
                _StubClient(hall_error=ApiError("合成：大厅不可读", code=0)), _StubHttp(),
            ),
            "我的订单": (
                _StubClient(my_error=ApiError("合成：我的订单不可读", code=0)), _StubHttp(),
            ),
            "店铺标识": (_StubClient(), _StubHttp(html=_upload_page_html(sid=None))),
        }
        for keyword, (client, http) in scenarios.items():
            with self.subTest(keyword=keyword):
                report = jidanpu_upstream.detect_binding(self.binding, client=client, http=http)
                self.assertFalse(report.ok)
                self.assertTrue(
                    any(keyword in problem for problem in report.problems), report.problems,
                )

    def test_login_failure_is_named_and_stops_before_any_upstream_read(self):
        """凭据错 ⇒ 只报登录失败，且**一个上游请求都不再发**（没有会话时后三项只会误报）。"""
        client = _StubClient(login_error=AuthError("合成：登录被拒"))
        http = _StubHttp()
        report = jidanpu_upstream.detect_binding(self.binding, client=client, http=http)
        self.assertEqual(report.problems, (LOGIN_FAILURE,))
        self.assertEqual(client.logins, 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(http.gets, [])

    def test_missing_hall_access_is_named(self):
        """② 大厅读不到（业务码非 0）⇒ 点名「抢单大厅」并给可执行的人话。"""
        client = _StubClient(hall_error=ApiError("合成：大厅不可读", code=0))
        report = self._detect(client=client)
        self.assertEqual(report.problems, (HALL_FAILURE,))

    def test_missing_my_orders_access_is_named(self):
        """③ 我的订单读不到 ⇒ 点名「我的订单」。"""
        client = _StubClient(my_error=ApiError("合成：我的订单不可读", code=0))
        report = self._detect(client=client)
        self.assertEqual(report.problems, (MY_ORDERS_FAILURE,))

    def test_hall_and_my_orders_failures_are_both_named(self):
        """② 与 ③ **互相独立**：两条都坏 ⇒ 两条各报一次（用户一次就知道要开哪些权限）。"""
        client = _StubClient(
            hall_error=ApiError("合成：大厅不可读", code=0),
            my_error=ApiError("合成：我的订单不可读", code=0),
        )
        report = self._detect(client=client)
        self.assertEqual(report.problems, (HALL_FAILURE, MY_ORDERS_FAILURE))

    def test_hall_my_orders_and_upload_fields_failures_are_all_named(self):
        """②③④ 同时坏 ⇒ **三条各报一次**（互不短路），⛔ 不在第一条失败处停下。"""
        client = _StubClient(
            hall_error=ApiError("合成：大厅不可读", code=0),
            my_error=ApiError("合成：我的订单不可读", code=0),
        )
        report = jidanpu_upstream.detect_binding(
            self.binding, client=client, http=_StubHttp(html=_upload_page_html(sid=None)),
        )
        self.assertFalse(report.ok)
        self.assertEqual(len(report.problems), 3)
        self.assertIn(HALL_FAILURE, report.problems)
        self.assertIn(MY_ORDERS_FAILURE, report.problems)
        self.assertIn(SID_FAILURE, report.problems[2])
        self.assertIn("sid", report.problems[2])

    def test_probe_order_id_falls_back_from_my_orders_to_the_hall_then_to_empty(self):
        """探上传页的 `orderId` 三级回落：我的订单第一条 → 大厅第一条 → **空串**。

        ⚠️ 现有用例只覆盖了第 1 级；后两级**未真机验证**（模块 docstring 已如实登记），
        但契约要钉住：⛔ 不许在「我的订单」为空时漏探、也⛔ 不许把 `None` 传下去。
        """
        # 第 2 级：我的订单为空 ⇒ 用大厅第 1 页第一条。
        http = _StubHttp()
        client = _StubClient(hall=[{"orderId": UPSTREAM_ORDER_ID_B}], my_pages=[[]])
        jidanpu_upstream.detect_binding(self.binding, client=client, http=http)
        self.assertEqual(http.gets[0][1]["params"], {"orderId": UPSTREAM_ORDER_ID_B})

        # 第 3 级：两边都空 ⇒ 用空串去探（`sid` 是店铺级标识、与具体单无关）。
        http2 = _StubHttp()
        client2 = _StubClient(hall=[], my_pages=[[]])
        jidanpu_upstream.detect_binding(self.binding, client=client2, http=http2)
        self.assertEqual(http2.gets[0][1]["params"], {"orderId": ""})

    # ── 第 4 项：上传页隐藏域 ────────────────────────────────────────────────

    def test_missing_sid_is_reported(self):
        """④ 上传页 200、但隐藏域里没有 `sid` ⇒ 第 4 项失败（截图链走不通，不许静默放行）。"""
        report = self._detect(http=_StubHttp(html=_upload_page_html(sid=None)))
        self.assertFalse(report.ok)
        self.assertEqual(len(report.problems), 1)
        self.assertIn(SID_FAILURE, report.problems[0])
        self.assertIn("sid", report.problems[0])

    def test_upload_page_missing_any_of_the_three_values_fails_the_fourth_item(self):
        """⭐ `sid` / `gameAccId` / `taskId` **缺任何一个** ⇒ 第 4 项失败（且点名缺的是哪一个）。

        失败方向：只检查 `sid`（把另两个当成「反正都是 0」）⇒ `gameAccId` / `taskId`
        这两格会**全绿**，而截图那一步会拿不到 `gameAccId` / `taskId`。
        """
        kwargs_for = {"sid": "sid", "gameAccId": "game_acc_id", "taskId": "task_id"}
        for missing in ("sid", "gameAccId", "taskId"):
            with self.subTest(missing=missing):
                html = _upload_page_html(**{kwargs_for[missing]: None})
                report = self._detect(http=_StubHttp(html=html))
                self.assertFalse(report.ok)
                self.assertEqual(len(report.problems), 1)
                self.assertIn(SID_FAILURE, report.problems[0])
                self.assertIn(missing, report.problems[0])

    def test_blank_required_upload_fields_count_as_missing(self):
        """⭐ 空 / 纯空白**不算取到**：只判「键在不在」⇒ `sid=""` 会误绿，截图那一步才炸。

        `sid` 是拼 OSS key 用的**纯数字串**，空串拼出来的 key 一定失败；与「缺哪项点名哪项」
        同口径。⚠️ 合成值里 `taskId` / `gameAccId` 的**正常**取值就是 `"0"`（非空）——
        所以这里要打的是「空 / 纯空白」，⛔ 不是「值为 0」。
        """
        cases = {
            "sid": ("sid", ""),
            "gameAccId": ("game_acc_id", "   "),
            "taskId": ("task_id", "\t"),
        }
        for name, (kwarg, blank) in cases.items():
            with self.subTest(blank=name):
                report = self._detect(http=_StubHttp(html=_upload_page_html(**{kwarg: blank})))
                self.assertFalse(report.ok)
                self.assertEqual(len(report.problems), 1)
                self.assertIn(SID_FAILURE, report.problems[0])
                self.assertIn(name, report.problems[0])

    def test_a_page_without_any_hidden_fields_is_reported(self):
        """上传页 200、但一个隐藏域都没有 ⇒ 第 4 项失败（三个都缺）。"""
        html = "<html><body>合成：没有隐藏域</body></html>"
        report = self._detect(http=_StubHttp(html=html))
        self.assertFalse(report.ok)
        for name in ("sid", "gameAccId", "taskId"):
            self.assertIn(name, report.problems[0])

    def test_an_unreadable_upload_page_is_reported(self):
        """上传页 302 到无权限页 ⇒ 第 4 项失败（人话仍是 §9.2 那条「取不到店铺标识」）。"""
        http = _StubHttp(status=302, location=NO_PERMISSION)
        report = self._detect(http=http)
        self.assertEqual(report.problems, (SID_FAILURE,))

    def test_upload_page_hidden_fields_are_read_not_hardcoded(self):
        """⭐ T7：三个值**都从上传页隐藏域读**；换一份页面 ⇒ 读出来的值跟着变。

        失败方向：把真实 `sid` 写死在代码里 ⇒ 第二条（换成 `20002` 的那份）立刻红。
        """
        fields = jidanpu_upstream.fetch_upload_page_fields(
            _StubHttp(html=_upload_page_html(sid=SID_SYNTH)),
            remote_order_id=UPSTREAM_ORDER_ID,
        )
        self.assertEqual(fields["sid"], SID_SYNTH)
        self.assertEqual(fields["gameAccId"], "0")
        self.assertEqual(fields["taskId"], "0")
        self.assertEqual(fields["orderId"], UPSTREAM_ORDER_ID)
        # 可见输入不进「隐藏域」字典（⛔ 别把整页 input 都当隐藏域）。
        self.assertNotIn("remark", fields)

        other = jidanpu_upstream.fetch_upload_page_fields(
            _StubHttp(html=_upload_page_html(sid=SID_SYNTH_B, game_acc_id="7", task_id="9")),
            remote_order_id=UPSTREAM_ORDER_ID_B,
        )
        self.assertEqual(other["sid"], SID_SYNTH_B)
        self.assertEqual(other["gameAccId"], "7")
        self.assertEqual(other["taskId"], "9")

    def test_fetch_upload_page_fields_classifies_each_non_200_page(self):
        """⭐ 非 200 各档**分开**（⛔ 别合成一档、⛔ 不都变成第 4 项那句人话）：

        - 302 到**无权限页** ⇒ `ApiError`（已知业务形态，`detect_binding` 认它 ⇒ 第 4 项）；
        - 302 到**登录页** ⇒ `SessionExpiredError`（会话失效，**不是**没权限）；
        - `5xx` ⇒ `ServerError`；连接失败 ⇒ `TransportError`；
        - 其余状态码（如 404）⇒ 裸 `JidanpuError`（判不了是权限还是上游改版，fail-closed）。
        """
        cases = [
            ("302 无权限页", dict(status=302, location=NO_PERMISSION), ApiError, True),
            ("302 登录页", dict(status=302, location=LOGIN_REDIRECT), SessionExpiredError, False),
            ("上游 5xx", dict(status=500), ServerError, False),
            ("未知 404", dict(status=404), JidanpuError, False),
            ("连接失败", dict(error=httpx.ConnectError("合成：连不上")), TransportError, False),
        ]
        for label, kwargs, expected, expect_api_error in cases:
            with self.subTest(case=label):
                http = _StubHttp(**kwargs)
                with self.assertRaises(expected) as ctx:
                    jidanpu_upstream.fetch_upload_page_fields(
                        http, remote_order_id=UPSTREAM_ORDER_ID,
                    )
                # ⚠️ `ApiError` 是第 4 项的通道 ⇒ 只有「302 无权限页」那一档才允许是它。
                self.assertEqual(isinstance(ctx.exception, ApiError), expect_api_error)

    def test_upload_page_infrastructure_failures_are_loud_not_a_named_problem(self):
        """⭐ 连接失败 / 5xx / 302 到登录页 ⇒ **检测本身失败**（原样上行），
        ⛔ 不许吞成第 4 项那句「取不到店铺标识，请联系上家」——那会让人去改一个没坏的权限设置
        （与模块 docstring 的承诺 / 登录短路的理由自相矛盾）。"""
        cases = [
            ("连接失败", dict(error=httpx.ConnectError("合成：连不上")), TransportError),
            ("上游 5xx", dict(status=502), ServerError),
            ("302 到登录页", dict(status=302, location=LOGIN_REDIRECT), SessionExpiredError),
            ("未知 404", dict(status=404), JidanpuError),
        ]
        for label, kwargs, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(expected) as ctx:
                    self._detect(http=_StubHttp(**kwargs))
                # ⚠️ 加一道「⛔ 不是 `ApiError`」：否则 `detect_binding` 会把它吞成第 4 项的人话。
                self.assertNotIsInstance(ctx.exception, ApiError)

    # ── 检测**不碰任何写端点** ──────────────────────────────────────────────

    def test_detection_never_calls_a_write_endpoint(self):
        """⭐ 桩钉调用集合：四项检测里**一个写端点都不许出现**、且方法只有 `GET`。"""
        client = _StubClient()
        jidanpu_upstream.detect_binding(self.binding, client=client, http=_StubHttp())
        paths = [path for _method, path, _kw in client.calls]
        for forbidden in (REC_ORDER_PATH, SET_STATUS_PATH, UPLOAD_PIC_PATH,
                          VIEW_PIC_PATH, OSS_POLICY_PATH):
            self.assertNotIn(forbidden, paths, forbidden)
        self.assertTrue(all(method == "GET" for method, _p, _kw in client.calls))

    # ── 取数原语（后续任务消费）─────────────────────────────────────

    def test_hall_page_single_request_returns_rows_and_the_envelope_count(self):
        """⭐ D-0366：`fetch_hall_page` 包装已删，检测路直接用共用原语 `_fetch_page`。"""
        client = _StubClient(hall=[{"orderId": "s-1"}, {"orderId": "s-2"}])
        rows, count = jidanpu_upstream._fetch_page(
            client, jidanpu_upstream.HALL_PATH, page=3,
        )
        self.assertEqual(rows, ({"orderId": "s-1"}, {"orderId": "s-2"}))
        self.assertEqual(count, 2)
        self.assertEqual(client.calls[0][2]["query"], {"page": 3})

    def test_hall_page_returns_none_when_the_envelope_has_no_count(self):
        """⭐ `count` 缺失 ⇒ 回 **`None`**（`int | None` 的三态），⛔ 绝不「编一个数」。

        失败方向（本用例要红的那个变异）：回落到本页行数 ⇒ 第 1 页 `len(rows) >= count`
        立刻成立 ⇒ 20 行当全量（下一任务的「拉全」判据在第 1 页就判完成），
        正是设计要消灭的静默少拿。
        """
        client = _StubClient(hall=[{"orderId": "s-1"}, {"orderId": "s-2"}], omit_hall_count=True)
        rows, count = jidanpu_upstream._fetch_page(
            client, jidanpu_upstream.HALL_PATH, page=1,
        )
        self.assertEqual(len(rows), 2)
        self.assertIsNone(count)

    def test_hall_page_shape_defence_is_loud(self):
        """载荷不是列表 ⇒ 响亮抛（⛔ 不静默当成空页——「空结果不等于没有数据」，E-0004）。

        ⚠️ 信封 `code` 显式给 `0`，让失败**只能**由形状防御触发（⛔ 不被成功码判据挡住而假绿）。
        """
        client = _StubClient()
        client.last_envelope = {"code": 0, "msg": "success", "data": {"unexpected": True}}
        client.call = lambda method, path, **kwargs: {"unexpected": True}  # type: ignore[method-assign]
        with self.assertRaises(ApiError):
            jidanpu_upstream._fetch_page(client, jidanpu_upstream.HALL_PATH, page=1)

    def test_fetch_my_orders_walks_every_page_and_stops_at_the_count(self):
        """自己按 `page` 翻（⛔ 不用 SDK 的 `iter_all`，它拿 `page_size=100` 判尾页会少拿）。"""
        pages = [
            [{"orderId": "p-1"}, {"orderId": "p-2"}],
            [{"orderId": "p-3"}],
        ]
        client = _StubClient(my_pages=pages)
        rows = jidanpu_upstream.fetch_my_orders(client)
        self.assertEqual([row["orderId"] for row in rows], ["p-1", "p-2", "p-3"])
        self.assertEqual(
            [kw["query"]["page"] for _m, _p, kw in client.calls], [1, 2],
        )

    def test_fetch_my_orders_refuses_to_silently_truncate(self):
        """翻到上限还没结束（且壳里没有可用 `count`）⇒ **响亮**报「拉不全」，⛔ 不静默交半份。"""
        client = _StubClient(my_forever=True)
        with mock.patch.object(jidanpu_upstream, "MY_ORDERS_MAX_PAGES", 3):
            with self.assertRaises(ApiError):
                jidanpu_upstream.fetch_my_orders(client)
        self.assertEqual(len(client.calls), 3)

    def test_fetch_my_orders_refuses_a_short_result_when_the_count_says_more(self):
        """壳里 `count=5`、实取 1 行就碰到空页 ⇒ **响亮**报「拉不全」（spec §10）。
        ⛔ 不许把「上游自报的总数没交齐」当成「拉完了」。"""
        client = _StubClient(my_pages=[[{"orderId": "p-1"}]], my_count=5)
        with self.assertRaises(ApiError):
            jidanpu_upstream.fetch_my_orders(client)
        self.assertEqual([kw["query"]["page"] for _m, _p, kw in client.calls], [1, 2])

    def test_detection_reads_only_the_first_my_orders_page(self):
        """②③ 是**单页预检**：⛔ 不为了「能不能读」去把整轮翻完（翻不完是 `fetch_my_orders`
        那边的响亮失败，⛔ 别伪装成「这个账号没权限」）。"""
        client = _StubClient(
            my_pages=[[{"orderId": "p-1"}], [{"orderId": "p-2"}]], my_count=99,
        )
        report = self._detect(client=client)
        self.assertTrue(report.ok)
        self.assertEqual(
            [(method, path) for method, path, _kw in client.calls],
            [("GET", HALL_PATH), ("GET", MY_ORDERS_PATH)],
        )
        for _method, path, kwargs in client.calls:
            self.assertEqual(kwargs["query"], {"page": 1}, path)

    # ── 建连接缝（`client=` / `http=` 注入 vs 自己建连）────────────────────────

    def test_without_injection_it_opens_the_connection_itself(self):
        """没给注入 ⇒ 走 `jidanpu.open_client_with_http`（桩打在**被查找的那个模块**上，E-0029）。"""
        stub = _StubClient()
        http = _StubHttp()

        @contextmanager
        def fake_open(binding):
            self.assertIs(binding, self.binding)
            yield stub, http

        with mock.patch.object(jidanpu, "open_client_with_http", fake_open):
            report = jidanpu_upstream.detect_binding(self.binding)
        self.assertTrue(report.ok)
        self.assertEqual(stub.logins, 1)
        self.assertEqual([path for path, _kw in http.gets], [UPLOAD_PAGE_PATH])

    def test_injection_never_touches_the_connection_helper(self):
        """对照：给了注入就**不许**去碰建连（接缝两道各钉一次，E-0029）。"""
        client = _StubClient()

        def boom(binding):  # pragma: no cover - 走到这里就是红的
            raise AssertionError("注入态不该自己建连")

        with mock.patch.object(jidanpu, "open_client_with_http", boom):
            report = jidanpu_upstream.detect_binding(self.binding, client=client, http=_StubHttp())
        self.assertTrue(report.ok)

    def test_injecting_only_one_side_of_the_connection_is_a_loud_programming_error(self):
        """只给 `client=` / 只给 `http=` ⇒ `ApiError`（⛔ 不拿半个注入去猜另一半、⛔ 不静默建连）。"""
        cases = [
            ("只给 client", {"client": _StubClient()}),
            ("只给 http", {"http": _StubHttp()}),
        ]
        for label, kwargs in cases:
            with self.subTest(case=label):
                with self.assertRaises(ApiError):
                    jidanpu_upstream.detect_binding(self.binding, **kwargs)

    def test_incomplete_credentials_are_a_config_error_not_a_report(self):
        """凭据不全 ⇒ `ConfigError`（**我们**的错误，响亮上行），⛔ 不伪装成一条普通失败。"""
        self.binding.password = ""
        self.binding.save(update_fields=["password"])
        with self.assertRaises(ConfigError):
            jidanpu_upstream.detect_binding(self.binding)

    # ── 真 HTTP 边界（零网络）：建连 + 登录 cookie + 三个请求 ────────────────

    def test_the_whole_path_works_at_the_real_http_boundary(self):
        """⭐ 把桩下移到 **HTTP 边界**（真 `httpx.Client` + `MockTransport`，零网络）：
        真 SDK 登录一次、会话 cookie 被两个读端点与上传页都带上、隐藏域从真 HTML 里读出来。

        ⚠️ 上面那些 `_Stub*` 用例证明「我们怎么判」，这一条证明「判据真的接在 HTTP 上」。
        """
        seen: list[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append((request.method, path, request.headers.get("cookie", "")))
            if path == "/Login/Login":
                return httpx.Response(
                    200, json={"code": 1, "msg": "ok", "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth-session-a; Path=/"},
                )
            if path == HALL_PATH:
                return httpx.Response(200, json={
                    "code": 0, "msg": "success", "count": 1,
                    "data": [{"orderId": UPSTREAM_ORDER_ID}],
                })
            if path == MY_ORDERS_PATH:
                return httpx.Response(200, json={
                    "code": 0, "msg": "success", "count": 1,
                    "data": [{"orderId": UPSTREAM_ORDER_ID}],
                })
            if path == UPLOAD_PAGE_PATH:
                return httpx.Response(200, text=_upload_page_html())
            raise AssertionError(f"未预期的请求：{request.method} {path}")

        with mock.patch.object(jidanpu.httpx, "Client", _http_factory(handler)):
            report = jidanpu_upstream.detect_binding(self.binding)

        self.assertTrue(report.ok, report.problems)
        paths = [path for _m, path, _c in seen]
        self.assertEqual(paths.count("/Login/Login"), 1)
        self.assertIn(HALL_PATH, paths)
        self.assertIn(MY_ORDERS_PATH, paths)
        self.assertIn(UPLOAD_PAGE_PATH, paths)
        # 登录真的发生了，而且后续每个请求都带着那次登录拿到的会话 cookie（共用同一个 jar）。
        for method, path, cookie in seen:
            if path != "/Login/Login":
                self.assertIn("sl-session=synth-session-a", cookie, (method, path))

    def test_a_read_failure_envelope_is_a_named_failure_at_the_real_envelope_layer(self):
        """⭐ 第 2/3 项的判据必须**真的过业务壳**（真 SDK + `MockTransport`，⛔ 不是桩抛异常）。

        失败信封 `{"code":1,"msg":"没有权限","data":[]}`（**非空 `msg` + 空 `data` + `code != 0`**）：
        SDK 的宽松解包按 `code > 0` 判**成功**、不抛异常 ⇒ 只有我们显式判 `code == 0` 才拦得住。
        ⛔ 失败方向：去掉成功码判据 ⇒ 这一条会变成 `ok=True`（误绿）。
        """
        failures = {HALL_PATH: HALL_FAILURE, MY_ORDERS_PATH: MY_ORDERS_FAILURE}
        for bad_path, expected in failures.items():

            def handler(request: httpx.Request, bad_path=bad_path) -> httpx.Response:
                path = request.url.path
                seen.append(path)
                if path == "/Login/Login":
                    return httpx.Response(
                        200, json={"code": 1, "msg": "ok", "data": "ok"},
                        headers={"Set-Cookie": "sl-session=synth-session-a; Path=/"},
                    )
                if path == bad_path:
                    return httpx.Response(200, json={"code": 1, "msg": "没有权限", "data": []})
                if path == MY_ORDERS_PATH:
                    return httpx.Response(200, json={
                        "code": 0, "msg": "success", "count": 1,
                        "data": [{"orderId": UPSTREAM_ORDER_ID}],
                    })
                if path == HALL_PATH:
                    return httpx.Response(200, json={
                        "code": 0, "msg": "success", "count": 1,
                        "data": [{"orderId": UPSTREAM_ORDER_ID}],
                    })
                if path == UPLOAD_PAGE_PATH:
                    return httpx.Response(200, text=_upload_page_html())
                raise AssertionError(f"未预期的请求：{request.method} {path}")

            with self.subTest(bad_path=bad_path):
                seen: list[str] = []
                with mock.patch.object(jidanpu.httpx, "Client", _http_factory(handler)):
                    report = jidanpu_upstream.detect_binding(self.binding)
                # 「真的过信封层」的判据：那笔请求确实从 HTTP transport 发出去了。
                self.assertIn(bad_path, seen)
                self.assertFalse(report.ok)
                self.assertEqual(report.problems, (expected,))

    def test_a_502_from_the_hall_is_a_loud_failure_not_a_named_problem(self):
        """5xx 是**检测本身失败**（`ServerError`），⛔ 不当成「这个账号没权限」——
        说成没权限会让人去改一个没坏的权限设置（`probe_pages` 的同一条口径）。"""
        client = _StubClient()
        client.hall_error = ServerError("合成：上游 502", status_code=502)
        with self.assertRaises(ServerError):
            self._detect(client=client)

    def test_a_rate_limited_hall_is_a_loud_failure(self):
        """限流（`RateLimitError`）同样是**检测本身失败**：它是暂时的，不是权限问题。"""
        client = _StubClient()
        client.hall_error = RateLimitError("合成：限流中", retry_after=30)
        with self.assertRaises(RateLimitError):
            self._detect(client=client)
