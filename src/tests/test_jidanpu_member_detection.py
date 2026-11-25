"""下游鸡蛋铺线：绑定期检测的三条（Task 2；spec §9.1 / §2.3）。

被检的三件事：**能登录 / 来源名唯一命中 / 三条权限的页面可达**。
权限那一条走**探页面可达性**（spec A13）：无权限 = `302` 跳到 `/ErrorPage/nopermission`、
有权限 = `200`；⛔ 不真录测试单、⛔ 不为判权限去调任何写接口。

⚠️ 桩打在**被查找的那个模块**上（`AGENTS.md` §5-E / E-0029）：本线领域层在未注入时用
`apps.business.jidanpu.open_client_with_http` ⇒ 那个接缝单独有两道用例（接上 / 不许被碰）。
⚠️ `_Scripted` 的**形状**照抄 `src/tests/test_jidanpu_hall_launch.py`（`.call(method, path, **kwargs)`
/ `.calls` / `.last_envelope`）；本线多两样：`.ensure_login()` 与 `.get(path, follow_redirects=False)`
（注入态下**同一个对象**兼任 SDK 客户端与 HTTP 边界）。
⚠️ 文案期望是**测试里的字面量**，⛔ 不从被测常量派生——自指断言在文案被改坏时**不会红**
（`AGENTS.md` §5-G / E-0024）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `member-a`、
密码 `pw-canary-a`、来源值写成「合成…」。⛔ 零真实网络。
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError, AuthError, ConfigError, JidanpuError

from apps.business import jidanpu, jidanpu_member
from apps.business.models import JidanpuMemberBinding

BASE_URL = "https://jidanpu-a.test.invalid"
ACCOUNT = "member-a"
PASSWORD = "pw-canary-a"

#: 三个要探的页面路径，**逐字**抄 spec §2.3 的节点挂载（⛔ 不从模块常量派生）。
ALL_PAGES = ("/Order/addorder", "/System/systemlog", "/Order/orderlist")

#: 无权限页（spec §2.3 **实测**：302 跳到它）。
NO_PERMISSION = "/ErrorPage/nopermission"
#: 登录页那一跳（用来造「302 不是去无权限页」这条噪点）。
LOGIN_REDIRECT = "/Login/Index"

#: 来源字典里**恰好一条**「团团通系统」（实测形状：`name` = 前缀字母 + `-` + 名字）。
SYNTH_SOURCES_OK = [{"name": "d-团团通系统", "value": "合成来源值甲"}]

# ── 期望文案（字面量，独立于实现）─────────────────────────────────────────────
LOGIN_FAILURE = "登录失败：账号或密码错误"
SOURCE_AMBIGUOUS = "匹配到多个同名来源，请先在下家把名字改到唯一"
RECORD_FAILURE = "该账号打不开『录入订单』页面，说明没有对应权限，请在下家鸡蛋铺里给这个职位勾上"
LOG_FAILURE = "该账号打不开『系统日志』页面，说明没有对应权限，请在下家鸡蛋铺里给这个职位勾上"
HALL_FAILURE = (
    "该账号打不开『管理订单』页面，说明可能没有『将订单发到内部抢单大厅』权限，"
    "请在下家鸡蛋铺里给这个职位勾上"
)


def _source_missing(name: str) -> str:
    return f"鸡蛋铺里没有名为『{name}』的订单来源，请到下家鸡蛋铺后台手工创建后再回来检测"


class _Scripted:
    """`JidanpuClient` 的替身 + 探页面用的 **HTTP 边界**（本任务给它加 `pages=`）。

    - `pages`: 路径 → `True`（能开，200）/ `False` 或缺省（无权限，302 `nopermission`）。
    - `page_status` / `page_location`: 覆盖上面的脚本，造「既不是 200、也不是 302 nopermission」
      那类**判不了权限**的响应。
    - `login_error`: `ensure_login()` 要抛的异常（造登录失败）。
    """

    def __init__(
        self,
        *,
        sources=None,
        pages=None,
        login_error=None,
        page_status=None,
        page_location=NO_PERMISSION,
    ):
        self.sources = list(SYNTH_SOURCES_OK if sources is None else sources)
        self.pages = dict(pages or {})
        self.login_error = login_error
        self.page_status = page_status
        self.page_location = page_location
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}
        #: `ensure_login()` 被调了几次（登录失败必须**短路**，不再打上游）。
        self.logins = 0
        #: 每一次探页面：`(路径, follow_redirects)`。
        self.probes: list[tuple[str, object]] = []

    def ensure_login(self):
        self.logins += 1
        if self.login_error is not None:
            raise self.login_error

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == jidanpu.SOURE_LIST_PATH:
            self.last_envelope = {"code": 0, "msg": "success"}
            return list(self.sources)
        raise AssertionError(f"未预期的端点：{method} {path}")

    def get(self, path, **kwargs):
        self.probes.append((path, kwargs.get("follow_redirects")))
        if self.page_status is not None:
            headers = {"Location": self.page_location} if self.page_location else {}
            return httpx.Response(self.page_status, headers=headers)
        if self.pages.get(path):
            return httpx.Response(200, text="<html>合成页面</html>")
        return httpx.Response(302, headers={"Location": NO_PERMISSION})


@contextmanager
def _ctx(client):
    yield client


#: 真 `httpx.Client`。模块级取好，这样下面那个工厂替换 `jidanpu.httpx.Client` 之后
#: 仍能造出**真的**客户端（只是 transport 换成 `httpx.MockTransport`）。
_REAL_HTTPX_CLIENT = httpx.Client


def _http_factory(handler):
    """把 `jidanpu.httpx.Client(...)` 换成「真客户端 + MockTransport」⇒ **零网络**。"""

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return factory


class MemberDetectionTests(TenantTestCase):
    def setUp(self):
        user = User.objects.create_user(username=ACCOUNT, password="pw-a")
        self.binding = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account="acc-a", password=PASSWORD,
        )

    def _report(self, client):
        return jidanpu_member.detect_binding(self.binding, client=client)

    # ── 三条判据的路径 ──────────────────────────────────────────────────────

    def test_the_probed_paths_are_the_verbatim_ones(self):
        """三个页面路径逐字钉住（spec §2.3）：⛔ 拼错一个就是一个恒假的检测。"""
        self.assertEqual(
            (
                jidanpu_member.MEMBER_RECORD_PAGE,
                jidanpu_member.MEMBER_LOG_PAGE,
                jidanpu_member.MEMBER_HALL_PAGE,
            ),
            ALL_PAGES,
        )

    def test_a_healthy_binding_passes_and_probes_exactly_those_three_pages(self):
        """三条全过 ⇒ `ok=True`、`failures=()`，且**真的探了那三个页面**、只读一个端点。"""
        client = _Scripted(pages=dict.fromkeys(ALL_PAGES, True))
        report = self._report(client)
        self.assertTrue(report.ok)
        self.assertEqual(report.failures, ())
        self.assertEqual(client.logins, 1)
        self.assertEqual([path for path, _flag in client.probes], list(ALL_PAGES))
        self.assertEqual([(m, p) for m, p, _kw in client.calls], [("GET", jidanpu.SOURE_LIST_PATH)])

    def test_missing_hall_permission_is_named(self):
        """缺「发到内部抢单大厅」⇒ 必须**点名**报这一条（不是笼统的"权限不足"）。"""
        client = _Scripted(pages={
            "/Order/addorder": True, "/System/systemlog": True, "/Order/orderlist": False,
        })
        report = self._report(client)
        self.assertFalse(report.ok)
        self.assertEqual(report.failures, (HALL_FAILURE,))
        self.assertIn("将订单发到内部抢单大厅", HALL_FAILURE)

    def test_missing_record_page_permission_is_named(self):
        """缺录单权限 ⇒ 点名到「录入订单」页。"""
        client = _Scripted(pages={
            "/Order/addorder": False, "/System/systemlog": True, "/Order/orderlist": True,
        })
        report = self._report(client)
        self.assertEqual(report.failures, (RECORD_FAILURE,))

    def test_missing_log_page_permission_is_named(self):
        """缺系统日志权限 ⇒ 点名到权限节点原文「系统日志」。"""
        client = _Scripted(pages={
            "/Order/addorder": True, "/System/systemlog": False, "/Order/orderlist": True,
        })
        report = self._report(client)
        self.assertEqual(report.failures, (LOG_FAILURE,))
        self.assertIn("系统日志", LOG_FAILURE)

    def test_all_missing_permissions_are_all_named_in_order(self):
        """三条同时缺 ⇒ **每条各报一条**（用户一次就知道要勾哪些），顺序固定。"""
        client = _Scripted(pages=dict.fromkeys(ALL_PAGES, False))
        report = self._report(client)
        self.assertEqual(report.failures, (RECORD_FAILURE, LOG_FAILURE, HALL_FAILURE))

    # ── 登录那一条 ──────────────────────────────────────────────────────────

    def test_login_failure_is_named_and_stops_before_any_upstream_read(self):
        """凭据错 ⇒ 只报登录失败，且**一个上游请求都不再发**（探页面会以登录页 302 收场，
        那是「检测本身失败」而不是「没有权限」）。"""
        client = _Scripted(
            pages=dict.fromkeys(ALL_PAGES, True),
            login_error=AuthError("登录失败：合成原因"),
        )
        report = self._report(client)
        self.assertEqual(report.failures, (LOGIN_FAILURE,))
        self.assertEqual(client.logins, 1)
        self.assertEqual(client.calls, [])
        self.assertEqual(client.probes, [])

    # ── 来源名那一条 ────────────────────────────────────────────────────────

    def test_source_missing_is_named(self):
        """来源不存在 ⇒ 报**用户填的那个名字**并让他去下家手工创建。"""
        client = _Scripted(
            sources=[], pages=dict.fromkeys(ALL_PAGES, True)
        )
        report = self._report(client)
        self.assertEqual(report.failures, (_source_missing("团团通系统"),))

    def test_source_missing_names_the_configured_source_name(self):
        """名字是**这一行填的那个**（默认名只是回落），⛔ 不写死「团团通系统」。"""
        self.binding.source_name = "合成来源名乙"
        self.binding.save(update_fields=["source_name"])
        client = _Scripted(sources=[], pages=dict.fromkeys(ALL_PAGES, True))
        report = self._report(client)
        self.assertEqual(report.failures, (_source_missing("合成来源名乙"),))

    def test_source_ambiguous_is_named(self):
        """匹配到多条同名来源 ⇒ 让用户先去下家改名。"""
        client = _Scripted(
            sources=[
                {"name": "d-团团通系统", "value": "合成来源值甲"},
                {"name": "e-团团通系统", "value": "合成来源值乙"},
            ],
            pages=dict.fromkeys(ALL_PAGES, True),
        )
        report = self._report(client)
        self.assertEqual(report.failures, (SOURCE_AMBIGUOUS,))

    def test_a_bad_source_does_not_hide_a_missing_permission(self):
        """来源与权限**各自独立**：来源不存在时页面那条照样要报（否则用户要来回跑两趟）。"""
        client = _Scripted(
            sources=[], pages={"/Order/addorder": False, "/System/systemlog": True,
                               "/Order/orderlist": True},
        )
        report = self._report(client)
        self.assertEqual(report.failures, (_source_missing("团团通系统"), RECORD_FAILURE))

    # ── 探页面的判据边界（⛔ 不许猜）────────────────────────────────────────

    def test_a_probe_outcome_that_is_neither_200_nor_nopermission_is_a_loud_failure(self):
        """302 到登录页 / 5xx / 302 没有 `Location` ⇒ **检测本身失败**（抛异常）。

        ⛔ 这些都不是「没有权限」：把它们说成没权限，会让人去改一个没坏的权限设置。
        """
        for status, location in ((302, LOGIN_REDIRECT), (500, None), (302, None), (403, None)):
            with self.subTest(status=status, location=location):
                client = _Scripted(
                    pages=dict.fromkeys(ALL_PAGES, True),
                    page_status=status, page_location=location,
                )
                with self.assertRaises(JidanpuError):
                    self._report(client)

    def test_the_probe_is_read_only_and_does_not_follow_redirects(self):
        """探页面**只有 GET**、`follow_redirects=False`，且除了来源字典没有任何其它调用。

        失败方向：把 `follow_redirects` 放开 ⇒ 302 被跟到那个错误页（200）⇒
        无权限会被误判成**有权限**（本仓最险的那种假绿）。
        """
        client = _Scripted(pages=dict.fromkeys(ALL_PAGES, False))
        self._report(client)
        self.assertEqual(client.probes, [(path, False) for path in ALL_PAGES])
        for method, path, _kwargs in client.calls:
            self.assertEqual((method, path), ("GET", jidanpu.SOURE_LIST_PATH))

    # ── 建连接缝（`client=` 注入 vs 自己建连）───────────────────────────────

    def test_without_injection_it_opens_the_connection_itself(self):
        """没给注入 ⇒ 走 `jidanpu.member_session`（桩打在**被查找的那个模块**上）。"""
        stub = _Scripted(pages=dict.fromkeys(ALL_PAGES, True))

        @contextmanager
        def fake_open(binding):
            self.assertIs(binding, self.binding)
            yield stub, stub

        with mock.patch.object(jidanpu, "member_session", fake_open):
            report = jidanpu_member.detect_binding(self.binding)
        self.assertTrue(report.ok)
        self.assertEqual(stub.logins, 1)
        self.assertEqual([path for path, _flag in stub.probes], list(ALL_PAGES))

    def test_injection_never_touches_the_connection_helper(self):
        """对照：给了注入就**不许**去碰建连（接缝两道各钉一次，E-0029）。"""
        client = _Scripted(pages=dict.fromkeys(ALL_PAGES, True))

        def boom(binding):  # pragma: no cover - 走到这里就是红的
            raise AssertionError("注入态不该自己建连")

        with mock.patch.object(jidanpu, "member_session", boom):
            report = jidanpu_member.detect_binding(self.binding, client=client)
        self.assertTrue(report.ok)

    def test_incomplete_credentials_are_a_config_error_not_a_report(self):
        """凭据不全 ⇒ `ConfigError`（**我们**的错误，响亮上行），⛔ 不伪装成一条普通失败。"""
        self.binding.password = ""
        self.binding.save(update_fields=["password"])
        with self.assertRaises(ConfigError):
            jidanpu_member.detect_binding(self.binding)

    # ── 真 HTTP 边界（零网络）：建连 + 登录 cookie + 三个页面 ────────────────

    def test_the_whole_path_works_at_the_real_http_boundary(self):
        """⭐ 把桩下移到 **HTTP 边界**（真 `httpx.Client` + `MockTransport`，零网络）：
        真 SDK 登录一次、jar 里的会话 cookie 被三个探页面请求带上、302 判成无权限。

        ⚠️ 上面那些 `_Scripted` 用例证明「我们怎么判」，这一条证明「判据真的接在 HTTP 上」
        （登录那一跳、`follow_redirects=False`、`Location` 读取都在真客户端的链路上）。
        """
        seen: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append((path, request.headers.get("cookie", "")))
            if path == "/Login/Login":
                # 登录成功 = 严格 `code == 1` **且** jar 里真的拿到会话 cookie。
                return httpx.Response(
                    200, json={"code": 1, "msg": "ok", "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth-session-a; Path=/"},
                )
            if path == jidanpu.SOURE_LIST_PATH:
                return httpx.Response(
                    200, json={"code": 0, "msg": "success", "data": SYNTH_SOURCES_OK}
                )
            if path == jidanpu_member.MEMBER_LOG_PAGE:
                return httpx.Response(302, headers={"Location": NO_PERMISSION})
            return httpx.Response(200, text="<html>合成页面</html>")

        with mock.patch.object(jidanpu.httpx, "Client", _http_factory(handler)):
            report = jidanpu_member.detect_binding(self.binding)

        self.assertEqual(report.failures, (LOG_FAILURE,))
        # ⚠️ 探的是**相对路径**（客户端建连时设了 `base_url`）：路径逐字、不是拼出来的 URL。
        self.assertEqual(
            [path for path, _c in seen if path in ALL_PAGES], list(ALL_PAGES)
        )
        # 登录真的发生了，而且三个页面的请求都带着它拿到的会话 cookie
        # （⇒ 探页面前确实 `ensure_login()` 过、共用同一个 jar）。
        self.assertIn("/Login/Login", [path for path, _c in seen])
        for path, cookie in seen:
            if path in ALL_PAGES:
                self.assertIn("sl-session=synth-session-a", cookie)
