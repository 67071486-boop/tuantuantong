"""官方 Open Platform API 客户端（`apps/business/jidanpu_openapi.py`）的判据（逐条对着 spec §13.3 的 C9 与任务书）：⭐ `code > 0` vs `code == 1` 的分水岭——造一个 `code=605764` 的成功响应（实测：只读回 `1`、录单回 `605764`）⇒ 必须判成功，写成 `code == 1` ⇒ 本文件红；⭐ §9.1 的 A/B 两档必须分开（C9）：`您没有开放平台接入权限` ⇒ ①、`没有权限` / `你没有权限`（两处实测文案不一样）⇒ ②（只读端点那一格）；⚠️ 取 token 那一步宽严不同——那里「权限」二字本身就是强信号（还谈不上「某个接口的权限」）⇒ `any`（任一命中即 ①），只读端点那一格仍是 `all` ⇒ ②（两处用户指引不同，⛔ 别合成一条）；⭐ C 档必须有对照（C9 末段点名的 E-0081 同族「探针用法错」）：网络异常 / 5xx / 404 ⇒ 一律 ③，文案里不许出现「权限」那两个字。
token 缓存 / 续期：连着调两次只取一次 token；到期才续；换密钥 / 换地址必须重新取。`GetProject` 不带 `gameId` ⇒ 404（spec §3.3 F11 实测）⇒ 必须响亮失败，⛔ 不是空元组。⛔ 密钥 / token 不进日志 / 异常 / 返回值：拿哨兵值搜异常文本、日志与 `repr`。⛔ 探测那一趟零写请求（行为判据 + 常量集合判据）。⭐ 五个写封装：每个写端点一条「失败信封必须被判失败」（含 `code = 0` + 非空 `msg` 那种假成功，E-0087 的原始坑）+ 一条成功（`code` 取不固定的值）+ 参数名逐字（打桩读 JSON 体）+ 鉴权头 / 基址；⚠️ 本组只测封装本身——五个方法今天一个调用方都没有（接口/进程/视图都不碰它们）。⭐⭐ 「回包里恰好 5 格」那条网（真机改准）：`GetOrderStatus` 的 `data` 实测只有 `status` / `creatTime` / `operTime` / `doneTime` / `verifyTime` ⇒ 客户端的 `OrderStatus`、假上游回的键、那 5 格本身三处同时钉成字面量；⛔ 按文档猜给客户端加 `orderId` / `price` / `recFee`、假上游照着回 ⇒ 用例全绿、真机上「回读取号 / 回读钉值」两条判据恒假。
⛔ 零真实网络：一律「真 `httpx.Client` + `httpx.MockTransport`」，只把 socket 那一层换掉 ⇒ 请求构造、参数名、`Authorization` 头都是真的（照本仓既有做法）；地址用 `.invalid` 顶级域——桩万一没接上它永远解析不了，不会真的打到真系统上。⛔ 夹具一律合成值（`AGENTS.md` §5-C / E-0077）：地址 `…test.invalid`、`client_id` 用 `cid-synthetic-…`、密钥与 token 用 `sentinel-…`（只允许出现在内存里）；⚠️ 那三句实测的对方文案（`您没有开放平台接入权限` / `没有权限` / `你没有权限`）是照抄 spec 的读数——它们是判据的一部分（F15 明写「不能只匹配一个字符串」），不是凭证。"""
import dataclasses
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qsl

import httpx
from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_openapi
from apps.business.models import JidanpuMemberBinding, JidanpuUpstreamBinding
from apps.business.jidanpu_openapi import (
    FailureKind,
    OpenApiError,
    ORDER_PROCESS_IMGS_PATH,
    ORDER_STATUS_PATH,
    GAMES_PATH,
    PROJECT_PATH,
    TOKEN_PATH,
    #: 二期步骤 1 的五个写端点路径（⛔ 别在用例里手抄路径：那会让「路径常量被改错」检不出来）。
    CREATE_ORDER_PATH,
    CHANGE_ORDER_STATUS_PATH,
    MODIFY_ORDER_PRICE_PATH,
    MODIFY_ORDER_PATH,
    UPLOAD_IMG_PATH,
    #: ⭐ 状态回包那个对象（「恰好 5 格」的网要数它的字段）。
    OrderStatus,
    #: ⭐⭐ 「对方明确说没有这张单」那道判据的**两步**（`absent_order_error` 的第 4 条要它们）。
    STEP_ORDER_STATUS,
    STEP_TOKEN,
)

#: 合成地址：`.invalid` 是保留顶级域 ⇒ **永远解析不了**（桩没接上时不可能"真的通"）。
BASE_URL = "https://jidanpu-openapi.test.invalid"
#: ⭐ **下家那条线**（`JidanpuMemberBinding`）的合成地址：与上面那个**刻意不同**
#: ⇒ 「取到的是不是**这一行**的地址」才判得出来（同值的话取错也绿）。
MEMBER_BASE_URL = "https://jidanpu-member-openapi.test.invalid"
CLIENT_ID = "cid-synthetic-0001"
#: ⭐ **哨兵密钥**：只允许出现在内存里（日志 / 异常 / `repr` / 返回值里都不许有它）。
SENTINEL_SECRET = "sentinel-secret-must-not-leak-0001"
#: ⭐ **哨兵 token**（上游"发回来"的那个）：同样不许出现在日志 / 异常 / 返回值里。
SENTINEL_TOKEN = "sentinel-token-must-not-leak-0002"

#: 实测的对方文案（spec §3.3 F12/F13/F15）。⛔ 别在这里加别的「没权限」写法当兜底。
OUTER_GATE_MSG = "您没有开放平台接入权限"
INNER_PERMISSION_MSGS = ("没有权限", "你没有权限")

#: 实测：录单成功回的那个 `code`（spec §3.2 F6 / 回调接收器线 spec 附录 A.7）。
RECORD_ORDER_SUCCESS_CODE = 605764

#: 一个形状完整的订单状态回包（字段名与内容取自 spec §3.3 F8 的实测样例，值为合成值）。
ORDER_STATUS_DATA = {
    "status": "未分配",
    "creatTime": "2026-01-02 03:04:05",
    "operTime": None,
    "doneTime": None,
    "verifyTime": None,
}

# ── ⭐⭐ 「恰好 5 个字段」那条网的两个**字面量**期望（⛔ 不从被测模块派生）────────────────
#: ⭐⭐ **真机实测**：`GetOrderStatus` 回包 `data` 里的键恰好这 5 个（`?orderId=` 与 `?sourceId=` 各测一次，键集合一样；没有 `orderId` / `price` / `recFee`）。
#: ⚠️ 写成字面量（⛔ 不从 `jidanpu_openapi` 导入任何集合）：谁往假上游里塞一格、或往 `OrderStatus` 上加一格都当场红。
ORDER_STATUS_REAL_KEYS = frozenset({"status", "creatTime", "operTime", "doneTime", "verifyTime"})
#: 那 5 格在**客户端**那一侧对应的字段名（`OrderStatus` 的字段）。⛔ 同样写字面量。
ORDER_STATUS_REAL_FIELDS = frozenset(
    {"status", "creat_time", "oper_time", "done_time", "verify_time"}
)

#: **只读**端点的路径（结构网的那一半；与下面写的那一组**分开**钉，理由见那条用例）。
READ_ENDPOINT_PATHS = (
    "/api/OpenPlatform/GetOrderStatus",
    "/api/OpenPlatform/GetOrderProcessImgs",
    "/api/OpenPlatform/GetGames",
    "/api/OpenPlatform/GetProject",
)

#: **写**端点的路径（二期步骤 1 起本模块真的有写封装了 ⇒ 这五个名字应当出现；顺序照文档副本 `.tools/recon/jidanpu-openapi/raw-doc.txt` 那一页的先后）。
#: ⛔ 「写接口的名字一个都不许出现」那句今天在本文件源码上是假的——它的原意（探测那一趟一个写接口都不打）由两条行为判据接着守：
#: `test_the_probe_never_touches_a_write_endpoint`（本文件）与 `tests/test_jidanpu_member_api.py::…the_probe_only_ever_touches_the_read_endpoints…`。
WRITE_ENDPOINT_PATHS = (
    "/api/OpenPlatform/CreateOrder",
    "/api/OpenPlatform/ModifyOrder",
    "/api/OpenPlatform/ChangeOrderStatus",
    "/api/OpenPlatform/UploadImg",
    "/api/OpenPlatform/ModifyOrderPrice",
)

#: 真 `httpx.Client`：模块级取好，这样替换 `jidanpu_openapi.httpx.Client` 之后仍能造出**真的**客户端。
_REAL_HTTPX_CLIENT = httpx.Client


def _json_body(request: httpx.Request) -> object:
    """这一发请求的 **JSON 体**（⛔ 按 `content-type` 判：不是 JSON 体就回 `None`）。
    ⚠️ 不硬解：取 token 那一发是表单（`grant_type=…`），硬解会在那条路上抛 `JSONDecodeError`——
    把「读一条记录」变成一次失败（探针自己的用法错，E-0004 同族）。"""
    if "json" not in (request.headers.get("content-type") or ""):
        return None
    body = request.content.decode("utf-8") if request.content else ""
    return json.loads(body) if body else None


class _Clock:
    """可控时钟（只为 token 续期那两条用例）：⛔ 不动全局 `time`，只替换本模块里的那个名字。"""

    def __init__(self, now=10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Upstream:
    """**零网络**的假鸡蛋铺官方 API：按路径给回包，并把每一次请求记下来。
    ⚠️ 记录里故意留一份 `authorization` 头与表单——判据要钉「token 发对了地方」、
    「`client_secret` 只出现在 token 那一次的请求体里」。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict[str, tuple[int, object]] = {}
        self.errors: dict[str, Exception] = {}
        self.token_status = 200
        self.token_payload = None
        self.expires_in = 3600

    # ── 配置 ──────────────────────────────────────────────────────────────────
    def reply(self, path, *, status=200, payload=None) -> "_Upstream":
        self.routes[path] = (status, payload)
        return self

    def boom(self, path, exc: Exception) -> "_Upstream":
        self.errors[path] = exc
        return self

    def token_reply(self, *, status=200, payload=None, expires_in=3600) -> "_Upstream":
        self.token_status = status
        self.token_payload = payload
        self.expires_in = expires_in
        return self

    # ── httpx 接线 ────────────────────────────────────────────────────────────
    def factory(self, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self.handler), **kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append({
            "method": request.method,
            "path": path,
            #: ⭐ **整条 URL**（含 scheme / host / port）：判据要钉「请求真的打到了**这一行**的
            #: `jidanpu_base_url`」——只记 `path` 的话，基址取错（空串 / 取了别人的）看不出来。
            "url": str(request.url),
            "params": dict(request.url.params),
            "authorization": request.headers.get("authorization"),
            "content_type": request.headers.get("content-type"),
            "form": dict(parse_qsl(request.content.decode("utf-8"))) if request.content else {},
            #: ⭐ **JSON 请求体**（写端点用；⛔ 不是 JSON 体就回 `None`）：写方法的判据要钉
            #: 「参数名与值**逐字**发对了没有」，而表单那一路解不出 JSON 体（见 `_json_body`）。
            "json": _json_body(request),
        })
        error = self.errors.get(path)
        if error is not None:
            raise error
        if path in self.routes:
            status, payload = self.routes[path]
        elif path == TOKEN_PATH:
            status = self.token_status
            payload = self.token_payload
            if payload is None:
                payload = {"access_token": SENTINEL_TOKEN, "token_type": "bearer",
                           "expires_in": self.expires_in}
        else:
            status, payload = 200, {"code": 1, "msg": None, "data": None}
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        if payload is None:
            return httpx.Response(status, text="")
        return httpx.Response(status, json=payload)

    # ── 读数 ──────────────────────────────────────────────────────────────────
    def token_calls(self) -> list[dict]:
        return [row for row in self.requests if row["path"] == TOKEN_PATH]

    def api_calls(self) -> list[dict]:
        return [row for row in self.requests if row["path"] != TOKEN_PATH]

    def last_api_call(self) -> dict:
        calls = self.api_calls()
        assert calls, "一条只读端点的请求都没发出去（桩没接上？）"
        return calls[-1]


def _make_openapi_binding(*, username="openapi-cs", base_url=BASE_URL, client_id=CLIENT_ID,
                          client_secret=SENTINEL_SECRET, mode="openapi"):
    """合成一行**②式**绑定（⛔ 不用任何真值）。
    ⚠️ `account` 由 `username` 派生：`(jidanpu_base_url, account)` 是唯一约束 ⇒ 同一个用例里造两行时账号必须跟着用户名走，否则第二行撞约束。"""
    user = User.objects.create_user(username=username, password="pw-synthetic")
    return JidanpuUpstreamBinding.objects.create(
        user=user, jidanpu_base_url=base_url, account=f"acc-{username}", password="pw-synthetic",
        mode=mode, client_id=client_id, client_secret=client_secret,
    )


class OpenApiTestBase(TenantTestCase):
    """公共夹具：一行②式绑定 + 一个假上游（把 `httpx.Client` 换成 MockTransport）。"""

    def setUp(self):
        # ⚠️ 两个方向都清一遍：token 缓存是**模块级**的（跨用例会串），而它正是本文件的判据之一。
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.upstream = _Upstream()
        self.binding = _make_openapi_binding()
        patcher = mock.patch.object(jidanpu_openapi.httpx, "Client", self.upstream.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def openapi(self):
        """开一个客户端上下文（调用方用 `with`）。
        ⚠️ ⛔ **别把这个方法叫 `client`**：`django.test.TestCase` 在 `_pre_setup` 里把 `self.client` 设成 Django 测试客户端实例，实例属性盖掉类方法 ⇒ `self.client()` 变成「调用一个 `Client` 实例」的 `TypeError`（踩过一次，30 个用例一起红）。"""
        return jidanpu_openapi.openapi_client(self.binding)

    def call(self, method: str, *args, **kwargs):
        with self.openapi() as client:
            return getattr(client, method)(*args, **kwargs)


# ── 端点常量与两个纯函数（判据只有一处出处）──────────────────────────────────────

class EndpointAndPureFunctionTests(OpenApiTestBase):
    def test_the_paths_are_the_verbatim_ones(self):
        """五个路径逐字钉住（spec §3.3 F8–F11 / §3.1 F2）：⛔ 拼错一个就是一次静默失败。"""
        self.assertEqual(TOKEN_PATH, "/oauth/token")
        self.assertEqual(ORDER_STATUS_PATH, "/api/OpenPlatform/GetOrderStatus")
        self.assertEqual(ORDER_PROCESS_IMGS_PATH, "/api/OpenPlatform/GetOrderProcessImgs")
        self.assertEqual(GAMES_PATH, "/api/OpenPlatform/GetGames")
        self.assertEqual(PROJECT_PATH, "/api/OpenPlatform/GetProject")

    def test_is_success_code_pins_the_criterion(self):
        """⭐ **成功判据 = `code > 0`** 的唯一出处（spec §3.2 F5/F6）。
        ⛔ 弄红它：写成 `code == 1` ⇒ 第 1 行断言红；写成 `code == 0` ⇒ 前两行红。"""
        self.assertTrue(jidanpu_openapi.is_success_code(RECORD_ORDER_SUCCESS_CODE))
        self.assertTrue(jidanpu_openapi.is_success_code(1))
        self.assertTrue(jidanpu_openapi.is_success_code(2))
        self.assertFalse(jidanpu_openapi.is_success_code(0))
        self.assertFalse(jidanpu_openapi.is_success_code(-1))
        # `bool` 是 `int` 的子类，而 `code: true` 不是成功码
        self.assertFalse(jidanpu_openapi.is_success_code(True))
        # 不是整数 ⇒ 判失败（协议问题走 ③，⛔ 不猜它的形状）
        self.assertFalse(jidanpu_openapi.is_success_code("1"))
        self.assertFalse(jidanpu_openapi.is_success_code(None))

    def test_classify_failure_tells_the_two_permission_failures_apart(self):
        """⭐ **C9**：①②**必须分开**（它们的用户指引完全不同）。"""
        self.assertIs(
            jidanpu_openapi.classify_failure(
                step=jidanpu_openapi.STEP_ORDER_STATUS, http_status=200, code=0, msg=OUTER_GATE_MSG,
            ),
            FailureKind.CREDENTIALS,
        )
        for msg in INNER_PERMISSION_MSGS:
            with self.subTest(msg=msg):
                self.assertIs(
                    jidanpu_openapi.classify_failure(
                        step=jidanpu_openapi.STEP_GAMES, http_status=200, code=0, msg=msg,
                    ),
                    FailureKind.PERMISSION,
                )

    def test_classify_failure_never_reads_a_non_business_failure_as_permission(self):
        """⭐ **C9 的对照**（E-0081 同族的「探针用法错」）：**网络 / 5xx ⇒ ③**，⛔ 不许判成 ①②。
        ⚠️ 四种情形各自判一次：没有这组对照，「把连接失败映射成没权限」的变异在本文件里只会红一半
        （只有 ③ 那条红，分不清是哪一侧错）。"""
        cases = (
            ("网络异常（没有 HTTP 状态）", dict(http_status=None, code=None, msg=None)),
            ("5xx", dict(http_status=502, code=None, msg=None)),
            ("404", dict(http_status=404, code=None, msg=None)),
            ("HTTP 200 但 code 缺席（协议问题）", dict(http_status=200, code=None, msg="没有权限")),
        )
        for name, kwargs in cases:
            with self.subTest(case=name):
                self.assertIs(
                    jidanpu_openapi.classify_failure(
                        step=jidanpu_openapi.STEP_ORDER_STATUS, **kwargs
                    ),
                    FailureKind.OTHER,
                )

    def test_an_http_401_or_403_is_a_credential_problem(self):
        """HTTP 层就被拒 ⇒ ①（实测的内层权限不足走的是 **HTTP 200 + code=0 + msg**）。"""
        for status in (401, 403):
            with self.subTest(status=status):
                self.assertIs(
                    jidanpu_openapi.classify_failure(
                        step=jidanpu_openapi.STEP_ORDER_STATUS, http_status=status, code=None,
                        msg="Unauthorized",
                    ),
                    FailureKind.CREDENTIALS,
                )

    def test_a_token_step_rejection_without_the_outer_words_is_other(self):
        """⭐ 取 token 被业务拒绝也**要看报文**：对方用自己的故障码 + 一句「系统繁忙」表达它自己那边出故障
        ⇒ 判 ① 会把用户支使去找鸡蛋铺开一个不需要开的权限（指引错方向）⇒ 只有报文命中 `OUTER_GATE_WORDS` 才判 ①，否则判 ③。
        ⛔ 弄红它：改回「`step == STEP_TOKEN` ⇒ 无条件 ①」（本用例第一条就红）；⚠️ `msg=None` / 空串那一格也判 ③（⛔ 不许猜「大概是没开通」）。"""
        cases = (
            "系统繁忙，请稍后重试",   # 对方自己的故障码 + 说自己忙
            "服务异常",
            "",
            None,
        )
        for msg in cases:
            with self.subTest(msg=repr(msg)):
                self.assertIs(
                    jidanpu_openapi.classify_failure(
                        step=jidanpu_openapi.STEP_TOKEN, http_status=200, code=0, msg=msg,
                    ),
                    FailureKind.OTHER,
                )
                # ⭐ 本格期望 ③（OTHER）；报文命中外层门槛词才判 ①。
        # ⚠️ 取 token 这一步按**任一命中**判（§23.2：这一步「权限」二字本身就是强信号——还谈不上「某个接口的权限」）；
        # ⛔ 这一格不是删掉：它是「只回『没有权限』」这个真实形态的钉子（原样留着、只改期望）；只读端点那一格一个字没变（那里的「权限」是内层含义 ⇒ 判 ②，见下一条用例）。
        self.assertIs(
            jidanpu_openapi.classify_failure(
                step=jidanpu_openapi.STEP_TOKEN, http_status=200, code=0, msg="没有权限",
            ),
            FailureKind.CREDENTIALS,
        )
        # 对照（回归）：真的说了外层门槛 ⇒ 仍然是 ①
        self.assertIs(
            jidanpu_openapi.classify_failure(
                step=jidanpu_openapi.STEP_TOKEN, http_status=200, code=0, msg=OUTER_GATE_MSG,
            ),
            FailureKind.CREDENTIALS,
        )
        # ⭐ 反向对照（§23.2 的另一半）：**只读端点那一格仍按「两个词都命中」** ——
        # 那里只回「没有权限」是**内层**含义（门开了、这一项没配）⇒ ②，⛔ 不许跟着上面改成 ①。
        self.assertIs(
            jidanpu_openapi.classify_failure(
                step=jidanpu_openapi.STEP_ORDER_STATUS, http_status=200, code=0, msg="没有权限",
            ),
            FailureKind.PERMISSION,
        )

    def test_redaction_replaces_every_secret_it_is_given(self):
        text = jidanpu_openapi._redact(
            f"body={SENTINEL_SECRET} header=Bearer {SENTINEL_TOKEN}", (SENTINEL_SECRET, SENTINEL_TOKEN)
        )
        self.assertNotIn(SENTINEL_SECRET, text)
        self.assertNotIn(SENTINEL_TOKEN, text)
        self.assertIn(jidanpu_openapi.REDACTED, text)


# ── 凭据只从「绑定那一行」取（租户边界）──────────────────────────────────────────

class CredentialsTests(OpenApiTestBase):
    def test_credentials_come_only_from_that_row(self):
        self.assertEqual(
            jidanpu_openapi.openapi_credentials_of(self.binding),
            (BASE_URL, CLIENT_ID, SENTINEL_SECRET),
        )

    def test_the_factory_takes_only_the_binding_row(self):
        """⛔ **不接受任何请求来源的地址 / 账号**：签名里除了那一行只有一个超时。
        ⚠️ 结构性判据：有人日后为了"方便"加一个 `base_url=` 入口，这里当场红。"""
        self.assertEqual(
            list(inspect.signature(jidanpu_openapi.openapi_client).parameters),
            ["binding", "timeout"],
        )
        self.assertEqual(
            list(inspect.signature(jidanpu_openapi.openapi_credentials_of).parameters),
            ["binding"],
        )

    def test_a_password_mode_row_is_refused_before_any_request(self):
        """`mode` 是**唯一**判据（spec §2.1）：①式行即使留着旧的 `client_id` 也不许走②式。"""
        binding = _make_openapi_binding(username="password-mode-cs", mode="password")
        self.assertIsNone(jidanpu_openapi.openapi_credentials_of(binding))
        with self.assertRaises(jidanpu_openapi.ConfigError):
            with jidanpu_openapi.openapi_client(binding):
                self.fail("①式绑定竟然建出了②式的客户端")
        self.assertEqual(self.upstream.requests, [], "被拒的行竟然发了请求")

    def test_incomplete_credentials_are_refused_without_echoing_any_value(self):
        """凭据不全 ⇒ `ConfigError`，且**文案里不带任何值**（照 `jidanpu.open_client` 的口径）。"""
        binding = _make_openapi_binding(username="half-cs", client_secret="")
        self.assertIsNone(jidanpu_openapi.openapi_credentials_of(binding))
        with self.assertRaises(jidanpu_openapi.ConfigError) as ctx:
            with jidanpu_openapi.openapi_client(binding):
                self.fail("凭据不全竟然建出了客户端")
        self.assertNotIn(CLIENT_ID, str(ctx.exception))
        self.assertNotIn(SENTINEL_SECRET, str(ctx.exception))
        self.assertEqual(self.upstream.requests, [], "凭据不全竟然发了请求")

    def test_a_blank_secret_is_treated_as_missing(self):
        """只有空白的 `client_secret` 等于没填（序列化器 strip 过，这里再挡一道）。"""
        binding = _make_openapi_binding(username="blank-cs", client_secret="   ")
        self.assertIsNone(jidanpu_openapi.openapi_credentials_of(binding))


# ── ⭐ 官方 API 客户端是**两条线通用**的：下家那张表（`JidanpuMemberBinding`）也要能用 ──

def _make_member_openapi_binding(*, username="member-openapi-cs", base_url=MEMBER_BASE_URL,
                                 client_id=CLIENT_ID, client_secret=SENTINEL_SECRET,
                                 mode="openapi"):
    """合成一行**下家线**的②式绑定（`JidanpuMemberBinding`；⛔ 零真值）。
    ⚠️ 地址写的是真实列 `jidanpu_base_url`（下家表没有 `base_url` 那个只读别名属性——别名只在上家模型上），
    这正是本节要钉的那处不对称。"""
    user = User.objects.create_user(username=username, password="pw-synthetic")
    return JidanpuMemberBinding.objects.create(
        user=user, jidanpu_base_url=base_url, account=f"acc-{username}", password="pw-synthetic",
        mode=mode, client_id=client_id, client_secret=client_secret,
    )


class MemberLineCredentialsTests(TenantTestCase):
    """⭐ **②式客户端对「下家那条线」的行真的能取到凭据**（spec §4.2 的下家那张表）。
    ⚠️ 取址那一步原来读 `binding.base_url`——那是 `JidanpuUpstreamBinding` 上的只读别名属性（`models.py::base_url`，为让 `jidanpu.open_client` 那类既有函数可用）；`JidanpuMemberBinding` 压根没有它（只有真实列 `jidanpu_base_url`）⇒ `getattr(binding, "base_url", "")` 永远回空串 ⇒ `openapi_credentials_of` 回 `None` ⇒ 本模块对任何一行下家②式绑定都 `ConfigError`（表面症状「地址不完整」，库里地址填得好好的：静默）。
    ⇒ 判据打在**真实出口**上：`openapi_credentials_of` 的返回值、缓存键、真的发出去那一发的 URL 基址（⛔ 不是读一个私有属性）；夹具走 ORM + 本模块 `_Upstream` 桩（MockTransport）⇒ 零真实网络。"""

    def setUp(self):
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.upstream = _Upstream()
        self.binding = _make_member_openapi_binding()
        patcher = mock.patch.object(jidanpu_openapi.httpx, "Client", self.upstream.factory)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_credentials_of_a_member_row_are_not_none(self):
        """⭐ **本类的核心判据**：下家行（只有真实列 `jidanpu_base_url`）取得到三元组。
        ⚠️ 弄红它：把取址换回老写法 `getattr(binding, "base_url", "")`（或把 `_base_url_of` 的两个候选名
        顺序对调、并让 `base_url` 缺席时直接回空）⇒ 本条红（`None`，期望是那三元组）。"""
        self.assertEqual(
            jidanpu_openapi.openapi_credentials_of(self.binding),
            (MEMBER_BASE_URL, CLIENT_ID, SENTINEL_SECRET),
        )

    def test_the_cache_key_of_a_member_row_is_not_none(self):
        """缓存键那一支与取址**同源**（它也会漏取址）⇒ 单独钉一次。"""
        self.assertIsNotNone(jidanpu_openapi.openapi_cache_key(self.binding))

    def test_a_member_row_sends_its_request_to_its_own_base_url(self):
        """⭐ **真出口**：真发出去的那一发请求，URL 基址 = **这一行**的 `jidanpu_base_url`。
        ⚠️ 比断言 `client._base_url` 硬：拼接时用了另一个来源它照样绿；⚠️ 期望值用另一条线那个地址的
        否定也钉一次：取到「上家那一行的地址」同样算错。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        with jidanpu_openapi.openapi_client(self.binding) as client:
            games = client.fetch_games()
        self.assertIsInstance(games, tuple)
        call = self.upstream.last_api_call()
        self.assertTrue(
            call["url"].startswith(MEMBER_BASE_URL + GAMES_PATH),
            f"请求没打到这一行的地址上：{call['url']!r}",
        )
        self.assertNotIn(BASE_URL, call["url"])

    def test_a_password_mode_member_row_is_refused_before_any_request(self):
        """`mode` 是**唯一**判据（spec §2.1）在下家那张表上同样成立。"""
        binding = _make_member_openapi_binding(username="member-password-mode", mode="password")
        self.assertIsNone(jidanpu_openapi.openapi_credentials_of(binding))
        with self.assertRaises(jidanpu_openapi.ConfigError):
            with jidanpu_openapi.openapi_client(binding):
                self.fail("下家的①式绑定竟然建出了②式的客户端")
        self.assertEqual(self.upstream.requests, [], "被拒的行竟然发了请求")


class BaseUrlOfTests(TenantTestCase):
    """取址助手 `_base_url_of` **自己**的判据：两条线的取法、以及**顺序**。
    ⚠️ 为什么要单独钉顺序：`jidanpu_base_url` 是两张表都有的真实列，`base_url` 只是上家表上的别名；
    把顺序对调、并让「真实列在先」那条失效 ⇒ 下家行静默回空串（上面那一节就是它的终端读数）。"""

    def test_it_prefers_the_real_column_over_the_alias(self):
        """两处都有值时**认真实列** `jidanpu_base_url`（⛔ 不是先看别名）。"""
        row = SimpleNamespace(jidanpu_base_url="https://real-column.test.invalid",
                              base_url="https://alias.test.invalid")
        self.assertEqual(jidanpu_openapi._base_url_of(row), "https://real-column.test.invalid")

    def test_it_falls_back_to_the_alias_when_the_column_is_missing(self):
        """只有别名时回落 `base_url`（上家那种老形状的读法也认）。"""
        row = SimpleNamespace(base_url="https://alias-only.test.invalid")
        self.assertEqual(jidanpu_openapi._base_url_of(row), "https://alias-only.test.invalid")

    def test_it_strips_and_treats_blank_as_missing(self):
        """首尾空白照旧 `strip()`（复制粘贴很常见）；全空白 = **取不到**（回空串）。"""
        self.assertEqual(
            jidanpu_openapi._base_url_of(SimpleNamespace(jidanpu_base_url="  https://x.test.invalid \n")),
            "https://x.test.invalid",
        )
        self.assertEqual(
            jidanpu_openapi._base_url_of(SimpleNamespace(jidanpu_base_url="   ", base_url="  ")), "",
        )
        self.assertEqual(jidanpu_openapi._base_url_of(SimpleNamespace()), "")
        self.assertEqual(jidanpu_openapi._base_url_of(None), "")

    def test_upstream_rows_still_read_the_same_value(self):
        """⭐ **回归**：上家行走这个助手取到的值与它那个别名**逐字相同**（行为不变）。"""
        binding = _make_openapi_binding(username="alias-parity-cs")
        self.assertEqual(jidanpu_openapi._base_url_of(binding), binding.base_url)
        self.assertEqual(jidanpu_openapi._base_url_of(binding), BASE_URL)
        self.assertEqual(
            jidanpu_openapi.openapi_credentials_of(binding),
            (BASE_URL, CLIENT_ID, SENTINEL_SECRET),
        )


# ── ⭐ 成功判据：`code > 0`（分水岭就在这一节）────────────────────────────────────

class SuccessCriterionTests(OpenApiTestBase):
    def test_a_success_response_with_code_605764_is_a_success(self):
        """⭐ **这条就是「`code > 0` vs `code == 1`」的分水岭**（spec §3.2 F6 实测）。
        实测：只读端点成功回 `1`，而录单成功回 `605764` ⇒ 判据只能是 `code > 0`；写成 `code == 1` ⇒ 本用例红
        （`code=605764` 会被当成失败，抛 `OpenApiError`）；带 `code=1` 的对照 ⛔ 免得「判据换成恒真」也能过。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": RECORD_ORDER_SUCCESS_CODE, "msg": None, "data": ORDER_STATUS_DATA,
        })
        status = self.call("get_order_status", order_id="remote-synthetic-0001")
        self.assertEqual(status.status, "未分配")
        self.assertEqual(status.creat_time, ORDER_STATUS_DATA["creatTime"])
        self.assertIsNone(status.oper_time)

        jidanpu_openapi.reset_token_cache()
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                       "data": ORDER_STATUS_DATA})
        status = self.call("get_order_status", order_id="remote-synthetic-0002")
        self.assertEqual(status.status, "未分配")

    def test_a_zero_code_is_a_failure(self):
        """`code == 0` 是**失败**（⛔ 别照登录式读端点的口径判它成功）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 0, "msg": "订单不存在", "data": None})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("get_order_status", order_id="remote-synthetic-0003")
        self.assertIs(ctx.exception.kind, FailureKind.OTHER)
        self.assertEqual(ctx.exception.upstream_code, 0)
        self.assertIn("订单不存在", str(ctx.exception))

    def test_the_four_read_endpoints_parse_their_shapes(self):
        """四个端点各自的形状（顺带钉**参数名**：`orderId` / `sourceId` / `gameId` 逐字）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                       "data": ORDER_STATUS_DATA})
        self.upstream.reply(ORDER_PROCESS_IMGS_PATH, payload={"code": 1, "msg": None, "data": [
            {"url": "https://oss.test.invalid/a.jpg", "uploadTime": "2026-01-02 03:04:05",
             "name": "进度图", "note": None},
        ]})
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": [
            {"id": 1, "name": "游戏甲"}, {"id": 2, "name": "游戏乙"},
        ]})
        self.upstream.reply(PROJECT_PATH, payload={"code": 1, "msg": None,
                                                   "data": ["项目甲", "项目乙"]})

        with self.openapi() as client:
            client.get_order_status(source_id="source-synthetic-0001")
            self.assertEqual(self.upstream.last_api_call()["params"], {"sourceId": "source-synthetic-0001"})
            imgs = client.fetch_order_process_imgs(order_id="remote-synthetic-0004")
            self.assertEqual(self.upstream.last_api_call()["params"], {"orderId": "remote-synthetic-0004"})
            self.assertEqual(len(imgs), 1)
            self.assertEqual(imgs[0]["name"], "进度图")
            games = client.fetch_games()
            self.assertEqual(self.upstream.last_api_call()["params"], {})
            self.assertEqual([row["name"] for row in games], ["游戏甲", "游戏乙"])
            projects = client.fetch_projects(game_id=1)
            self.assertEqual(self.upstream.last_api_call()["params"], {"gameId": "1"})
            self.assertEqual(projects, ("项目甲", "项目乙"))

    # ── ⭐⭐ 「回包里恰好那 5 格」那条网 + 一个「按来源单号查一次」的入口 ──────────────────

    def test_the_order_status_shape_is_exactly_the_five_real_keys(self):
        """⭐⭐ **恰好集合网**：客户端 `OrderStatus` 的字段**恰好**是真机回包的那 5 格（多一个就红）。
        ⚠️ 这条网存在的理由（真机实测抓到的形态）：按文档猜给 `OrderStatus` 加过 `order_id` / `price` / `rec_fee` 三格、假上游照着回 ⇒ 用例全绿而真机「回读里压根没有那一格」⇒「回读拿号」与「回读钉值」两条判据恒假 ⇒ 本网把三处同时钉成字面量：假上游回的键 / 客户端暴露的字段 / 那 5 格本身。
        判别力：① 给 `OrderStatus` 加回任一格（如 `order_id: str = ""`）⇒ 字段集合那条红；② 往假上游 `ORDER_STATUS_DATA` 加一格 ⇒ 键集合那条红；③ 删掉 5 格里任一格 ⇒ 两条都红。"""
        # ① 客户端那一侧：**恰好** 5 个字段（字面量期望，⛔ 不从被测模块派生）。
        names = {f.name for f in dataclasses.fields(OrderStatus)}
        self.assertEqual(len(names), 5, f"OrderStatus 暴露了 {len(names)} 格：{sorted(names)}")
        self.assertEqual(names, ORDER_STATUS_REAL_FIELDS)
        # ② 那 5 格**真的**是 5 格（防「两个期望集合一起被改坏」的自指坏法）。
        self.assertEqual(len(ORDER_STATUS_REAL_KEYS), 5)
        # ③ 假上游那一侧：回的键**恰好**同一批 5 个（真机读数就是这一批）。
        self.assertEqual(set(ORDER_STATUS_DATA), ORDER_STATUS_REAL_KEYS)
        # ④ 正向对照：这条网钉的那个回包**真的**解析得动（否则「恰好」可以靠"什么都不回"骗过）。
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None, "data": ORDER_STATUS_DATA,
        })
        status = self.call("get_order_status", order_id="remote-synthetic-0002")
        self.assertEqual(status.status, "未分配")
        self.assertEqual(status.creat_time, ORDER_STATUS_DATA["creatTime"])
        # ⑤ ⭐ **回读里没有系统订单号**（那条「回读取号 / 比号」的判据因此**不存在**）：
        #    ⇒ 谁想在②式里"按来源单号回读取号"，先看这一条。
        self.assertFalse(
            [name for name in ORDER_STATUS_REAL_KEYS if "order" in name.lower()],
            "对方的状态回包里出现了带 order 的键 ⇒ 真机读数变了，改准这条网再改实现",
        )

    def test_an_unknown_extra_key_from_the_peer_does_not_break_parsing(self):
        """⭐ 对照：对方哪天在 `data` 里**多加一格**（我们不认识的）⇒ 照常解析，⛔ 不许因此崩。
        ⚠️ `data` 的键集合是对方控制的 ⇒ 上面「恰好 5 格」的网守的是我们自己（客户端字段 / 假上游读数），
        ⛔ 不该变成「对方一加字段就整条路崩」；反过来 `data` 本身畸形必须响亮抛——由 `test_a_malformed_payload_is_loud_not_silently_empty` 守着（⛔ 不在这里抄第二份）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None,
            "data": {**ORDER_STATUS_DATA, "remark": "对方哪天多加的一格（合成）"},
        })
        status = self.call("get_order_status", order_id="remote-synthetic-0014")
        self.assertEqual(status.status, "未分配")
        self.assertEqual({f.name for f in dataclasses.fields(status)}, ORDER_STATUS_REAL_FIELDS)

    def test_find_order_status_reads_absence_as_none_and_everything_else_as_an_error(self):
        """⭐ `find_order_status`：**对方明确说没有这张单 ⇒ `None`**；其余失败**照旧抛**。
        ⚠️ 录单挡重预读的判据口（对方已有这张单 ⇒ ⛔ 不建第二张）；⛔ 不解析 `msg`：判据是「HTTP 200 + 一个失败码 + 不是权限档」（`_answered_despite_business_failure`，探测那一趟已在用的同一处）；⚠️⚠️ 「没有这张单」与「参数不合法」共用同一个 `code=0`（真机实测：前者 `msg="没有相关订单信息"`、后者 `msg="系统单号和来源单号不可同时为空"`，`data` 都是空串）⇒ 本判据分不出这两档，口径、兜底与代价写在 `find_order_status` 的 docstring 里；⚠️⚠️ 查得到 ≠ 能认领：真机回包里没有 `orderId` ⇒ 查得到时调用方拿不到对方系统订单号（见 `jidanpu_member_openapi_outbox.CLAIM_WITHOUT_REMOTE_ID_REASON`）⇒ 本用例只钉「查得到 / 查不到 / 哪种失败照抛」，⛔ 不钉任何"取号"。
        判别力：① 删掉「应答了但业务失败」那一支（一律抛）⇒ 第一条断言红（每次录单都会卡死）；② 把权限档也当成「没有」⇒ 第三条红（缺权限时盲目录单）；③ 把传输层错误也当成「没有」⇒ 第二条红。"""
        # ① 对方应答了、但业务上失败（⭐ **真机逐字读数**：`msg="没有相关订单信息"` + `data=""`
        #    ——真机从不按文档口径回「没有相关订单」+ `data=None`。
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 0, "msg": "没有相关订单信息", "data": "",
        })
        with self.openapi() as client:
            self.assertIsNone(client.find_order_status(source_id="source-synthetic-0003"))
            # ② 传输层出问题 ⇒ 抛（⛔ 不许当成「对方没有」，那会重复录一张真单）。
            self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("合成连接失败"))
            with self.assertRaises(OpenApiError):
                client.find_order_status(source_id="source-synthetic-0004")
        # ③ 权限档（外层门槛没开通）⇒ 抛（⛔ 同上：缺权限不是「没有这张单」）。
        self.upstream.errors.clear()
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 0, "msg": OUTER_GATE_MSG, "data": None,
        })
        with self.openapi() as client:
            with self.assertRaises(OpenApiError):
                client.find_order_status(source_id="source-synthetic-0005")
        # ④ 正向对照：对方**有**这张单 ⇒ 照常回那个对象（否则上面几条会因「压根走不通」而假绿）。
        #    ⚠️ 判据只到「查得到」为止 —— 那个对象里**没有** `order_id` 可读（真机形态）。
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None, "data": ORDER_STATUS_DATA,
        })
        with self.openapi() as client:
            found = client.find_order_status(source_id="source-synthetic-0006")
        self.assertIsNotNone(found)
        self.assertEqual(found.status, "未分配")
        self.assertEqual({f.name for f in dataclasses.fields(found)}, ORDER_STATUS_REAL_FIELDS)

    def test_a_token_step_business_failure_is_not_read_as_an_absent_order(self):
        """⭐⭐ 判据的第 4 条：**取 token 那一步**的业务失败 ⛔ 不许当成「对方没有这张单」。
        ⚠️ 为什么承重（`/oauth/token` 与 `GetOrderStatus` 共用同一失败信封形状）：取 token 那一步回「HTTP 200 ＋ `code=0` ＋ 一句业务失败」时前三条判据全都成立，可那一次压根没问到「这张单在不在」；读成「没有」有两个方向都坏的静默后果：① ②式回调那条路当轮收口成终态 + 推水位（那条回调的状态永久不会再写）；② 退单那条路把链路标成「退成过」（`withdrawn_at`）⇒ 一次取 token 抖动换来一张单不再退。
        判别力：① 删掉 `absent_order_error` 的 `exc.step == STEP_ORDER_STATUS` ⇒ 第一 / 第二条红（token 那一步会被读成「没有这张单」）；② 删掉 `kind is FailureKind.OTHER` ⇒ 第三条红。"""
        # ① 判据本体：**同形状**、只有 `step` 不同 ⇒ 结论必须相反。
        kwargs = {"kind": FailureKind.OTHER, "http_status": 200, "upstream_code": 0}
        self.assertTrue(
            jidanpu_openapi.absent_order_error(
                OpenApiError(step=STEP_ORDER_STATUS, **kwargs)
            ),
            "GetOrderStatus 那一步的业务失败必须读成「对方没有这张单」",
        )
        self.assertFalse(
            jidanpu_openapi.absent_order_error(OpenApiError(step=STEP_TOKEN, **kwargs)),
            "取 token 那一步的业务失败被当成了「没有这张单」",
        )
        #: ② 权限档：同一个 step、同一档 HTTP / code ⇒ 仍不许当成「没有这张单」。
        self.assertFalse(
            jidanpu_openapi.absent_order_error(
                OpenApiError(step=STEP_ORDER_STATUS, kind=FailureKind.PERMISSION,
                             http_status=200, upstream_code=0)
            )
        )
        #: ③ 端到端接上了没有：取 token 回业务失败 ⇒ `find_order_status` **照旧抛**（⛔ 不回 `None`）。
        self.upstream.token_reply(status=200, payload={"code": 0, "msg": "系统繁忙"})
        with self.openapi() as client:
            with self.assertRaises(OpenApiError):
                client.find_order_status(source_id="source-synthetic-0008")

    def test_a_malformed_payload_is_loud_not_silently_empty(self):
        """形状不对（data 不是列表 / 行不是对象 / 状态不是字符串）⇒ **响亮抛**，⛔ 不回空。"""
        with self.openapi() as client:
            self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": {"a": 1}})
            with self.assertRaises(OpenApiError):
                client.fetch_games()
            self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": [1, 2]})
            with self.assertRaises(OpenApiError):
                client.fetch_games()
            self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                           "data": {"status": ""}})
            with self.assertRaises(OpenApiError):
                client.get_order_status(order_id="remote-synthetic-0005")
            # 合法空列表**照常回空**（空 ≠ 畸形）
            self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
            self.assertEqual(client.fetch_games(), ())

    def test_order_status_needs_exactly_one_key(self):
        """`orderId` / `sourceId` **二选一**：两个都不给、或两个都给 ⇒ `ValueError`（调用方的错）。"""
        with self.openapi() as client:
            with self.assertRaises(ValueError):
                client.get_order_status()
            with self.assertRaises(ValueError):
                client.get_order_status(order_id="a", source_id="b")
            # 正向对照：单独给任一个都行（否则上面两条会因「压根走不通」而假绿）
            self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                           "data": ORDER_STATUS_DATA})
            self.assertEqual(client.get_order_status(order_id="a").status, "未分配")
            self.assertEqual(client.get_order_status(source_id="b").status, "未分配")


# ── ⭐ 失败三档（§9.1 / C9）─────────────────────────────────────────────────────

class FailureKindTests(OpenApiTestBase):
    def _fail_with_msg(self, msg):
        """让订单状态端点回一个「HTTP 200 + code=0 + msg」的业务失败，回那个异常。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 0, "msg": msg, "data": None})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("get_order_status", order_id="remote-synthetic-0006")
        return ctx.exception

    def test_the_two_permission_failures_are_told_apart(self):
        """⭐ **C9 / §9.1**：①（外层门槛没开通）与②（内层某一项没权限）**必须分开**。
        ⛔ 弄红它：把 `classify_failure` 里那两个分支合并成一个 ⇒ 本用例必有一条红。"""
        outer = self._fail_with_msg(OUTER_GATE_MSG)
        self.assertIs(outer.kind, FailureKind.CREDENTIALS)

        for msg in INNER_PERMISSION_MSGS:
            with self.subTest(msg=msg):
                jidanpu_openapi.reset_token_cache()
                inner = self._fail_with_msg(msg)
                self.assertIs(inner.kind, FailureKind.PERMISSION)

    def test_the_guidance_for_the_two_kinds_is_different(self):
        """⭐ 两档的**用户指引完全不同**（这是分档存在的理由）：
        ① 只能找鸡蛋铺开通 / 改用登录式；② 自己把那一项的权限开出来。
        """
        outer = self._fail_with_msg(OUTER_GATE_MSG)
        jidanpu_openapi.reset_token_cache()
        inner = self._fail_with_msg("没有权限")

        # ① 的文案是 spec §9.3 逐字那一段，且**原样带着对方的原文**
        self.assertIn("还没开通「开放平台」接口", outer.user_text)
        self.assertIn(OUTER_GATE_MSG, outer.user_text)
        self.assertNotIn("请检查网络", outer.user_text)
        self.assertIn("鸡蛋铺", outer.user_action)

        # ② 的文案是 spec §11.5 逐字那一段，且**逐项点名**（点的是失败的那一步）
        self.assertIn("权限还没配全", inner.user_text)
        self.assertIn("查订单状态", inner.user_text)
        self.assertIn("不影响订单状态同步", inner.user_text)
        self.assertIn("权限", inner.user_action)

        self.assertNotEqual(outer.user_text, inner.user_text)
        self.assertNotEqual(outer.user_action, inner.user_action)

    def test_a_network_failure_is_never_read_as_a_permission_problem(self):
        """⭐ **C9 的对照**（E-0081 同族）：连接失败 ⇒ **③**，文案里不许出现「权限」。
        ⛔ 弄红它：把网络异常也映射成「没权限」⇒ 本用例两条断言都红。"""
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("合成连接失败"))
        with self.assertRaises(OpenApiError) as ctx:
            self.call("get_order_status", order_id="remote-synthetic-0007")
        self.assertIs(ctx.exception.kind, FailureKind.OTHER)
        self.assertEqual(ctx.exception.transport, "ConnectError")
        self.assertNotIn("权限", ctx.exception.user_text)
        self.assertIn("连不上鸡蛋铺的开放平台", ctx.exception.user_text)

    def test_a_5xx_is_never_read_as_a_permission_problem(self):
        self.upstream.reply(ORDER_STATUS_PATH, status=502, payload="<html>网关错误</html>")
        with self.assertRaises(OpenApiError) as ctx:
            self.call("get_order_status", order_id="remote-synthetic-0008")
        self.assertIs(ctx.exception.kind, FailureKind.OTHER)
        self.assertEqual(ctx.exception.http_status, 502)
        self.assertNotIn("权限", ctx.exception.user_text)

    def test_a_non_json_body_is_a_protocol_failure(self):
        self.upstream.reply(ORDER_STATUS_PATH, status=200, payload="<html>登录页</html>")
        with self.assertRaises(OpenApiError) as ctx:
            self.call("get_order_status", order_id="remote-synthetic-0009")
        self.assertIs(ctx.exception.kind, FailureKind.OTHER)

    def test_the_token_step_rejection_with_the_outer_gate_wording_is_a_credential_problem(self):
        """取 token 这一步被拒 **且报文说了外层门槛** ⇒ **①**（回归）。
        ⚠️ 两个子情形都要钉：业务级拒绝（HTTP 200 + code=0）与 HTTP 401；⚠️ 名字里的 `_with_the_outer_gate_wording_`
        标明这一步不再是无条件 ①（见下面那条 `..._without_the_outer_gate_wording_is_other`）。"""
        self.upstream.token_reply(payload={"code": 0, "msg": OUTER_GATE_MSG})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("fetch_games")
        self.assertIs(ctx.exception.kind, FailureKind.CREDENTIALS)
        self.assertIs(ctx.exception.step, jidanpu_openapi.STEP_TOKEN)

        jidanpu_openapi.reset_token_cache()
        self.upstream.token_reply(status=401, payload={"code": 0, "msg": "invalid_client"})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("fetch_games")
        self.assertIs(ctx.exception.kind, FailureKind.CREDENTIALS)

    def test_a_token_step_failure_of_their_own_system_is_not_a_permission_problem(self):
        """⭐ 对方用自己的故障码 + 「系统繁忙」说自己挂了 ⇒ **③**（不是「没开通权限」）。
        ⛔ 弄红它：把 `classify_failure` 的取 token 那一档改回「无条件 ①」⇒ 本用例第一条断言红；
        ⭐ 两条断言各自承重：分档必须对（否则指引错方向）、对方原文必须带给用户（spec §9.3 纪律 1：③ 这一档同样不许吞掉）。"""
        THEIR_BUSY_MSG = "系统繁忙，请稍后重试"
        self.upstream.token_reply(payload={"code": 0, "msg": THEIR_BUSY_MSG})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("fetch_games")

        self.assertIs(ctx.exception.kind, FailureKind.OTHER)
        self.assertIs(ctx.exception.step, jidanpu_openapi.STEP_TOKEN)
        # ⛔ 不是「你没开通开放平台」那句（那是 ① 的话，会让用户去开一个不需要开的权限）
        self.assertNotIn("还没开通「开放平台」接口", ctx.exception.user_text)
        # ⭐ 对方原文如实带给用户
        self.assertIn(THEIR_BUSY_MSG, ctx.exception.user_text)
        self.assertIn(THEIR_BUSY_MSG, str(ctx.exception))
        self.assertIn("我们这边", ctx.exception.user_action)

    def test_a_token_response_without_a_usable_expiry_is_loud(self):
        """`expires_in` 读不出来 ⇒ ③（⛔ 不默认一个「大概一小时」的猜测值：两种猜法都静默）。"""
        self.upstream.token_reply(payload={"access_token": SENTINEL_TOKEN, "token_type": "bearer"})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("fetch_games")
        self.assertIs(ctx.exception.kind, FailureKind.OTHER)
        self.assertIs(ctx.exception.step, jidanpu_openapi.STEP_TOKEN)

    def test_get_project_without_game_id_is_a_404_and_not_an_empty_tuple(self):
        """⭐ `GetProject` **不带 `gameId` ⇒ 404**（spec §3.3 F11 实测）。两层判据：
        ① 空 `gameId` 在发请求之前就被挡掉（否则调用方会把它看成"网络问题"）；② 上游真回 404 时响亮失败，
        ⛔ 绝不回空元组（那会把「我们漏传了参数」说成「这款游戏没有常用项目」）。"""
        with self.openapi() as client:
            with self.assertRaises(ValueError):
                client.fetch_projects(game_id="")
            with self.assertRaises(ValueError):
                client.fetch_projects(game_id="   ")
            # 空值只被**本地**挡下 ⇒ 一个 `GetProject` 请求都没白打（正向哨兵在下面）
            self.assertEqual(
                [row for row in self.upstream.requests if row["path"] == PROJECT_PATH], []
            )

            self.upstream.reply(PROJECT_PATH, status=404, payload="<html>404</html>")
            with self.assertRaises(OpenApiError) as ctx:
                client.fetch_projects(game_id="1")
        self.assertIs(ctx.exception.kind, FailureKind.OTHER)
        self.assertEqual(ctx.exception.http_status, 404)
        # `gameId=1` 真的发出去了，而且只发了这一次（⛔ 别把「回空元组」当成「没有常用项目」）
        self.assertEqual(
            len([row for row in self.upstream.requests if row["path"] == PROJECT_PATH]), 1
        )
        self.assertEqual(self.upstream.last_api_call()["params"], {"gameId": "1"})

    def test_game_id_zero_is_a_value_not_a_missing_argument(self):
        """⛔ 判空**不看真值**：`game_id=0` 会被 `if not game_id` 当成「没传」（E-0021 家族）。"""
        self.upstream.reply(PROJECT_PATH, payload={"code": 1, "msg": None, "data": []})
        with self.openapi() as client:
            self.assertEqual(client.fetch_projects(game_id=0), ())
        self.assertEqual(self.upstream.last_api_call()["params"], {"gameId": "0"})


# ── token 缓存 / 续期（对方有限流，这条线的调用会进常驻循环）──────────────────────

# ── 失败归因：这一批失败是「哪个租户的哪一份绑定」打出来的──────────

class FailureAttributionTests(OpenApiTestBase):
    """⭐ `OpenApiError` 带「是哪个绑定 / 租户」的归因：逐单查状态 / 兜底对账一次几十上百个请求，
    「哪一份绑定坏了」是排障时最先要回答的问题，光看 `step`（哪一步）答不出来。
    ⚠️ 归因字段⛔ 不是凭据（schema 名与行主键在绑定页面上本来就看得到）⇒ 可以进文本；密钥仍只走 `secrets` 抹除路径（下面第三条用例钉住）。"""

    def _endpoint_failure(self):
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 0, "msg": "没有权限", "data": None})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("get_order_status", order_id="remote-synthetic-0010")
        return ctx.exception

    def _token_failure(self):
        self.upstream.token_reply(payload={"code": 0, "msg": OUTER_GATE_MSG})
        with self.assertRaises(OpenApiError) as ctx:
            self.call("fetch_games")
        return ctx.exception

    def test_an_endpoint_failure_says_which_tenant_and_binding_it_came_from(self):
        exc = self._endpoint_failure()
        self.assertEqual(exc.binding_id, self.binding.pk)
        self.assertEqual(exc.tenant_schema, connection.schema_name)
        self.assertIn(f"绑定 {self.binding.pk}", str(exc))
        self.assertIn(f"租户 {connection.schema_name}", str(exc))

    def test_a_token_failure_carries_the_same_attribution(self):
        """取 token 那一步的失败**也**要带归因（它是最容易被漏掉的一档）。"""
        exc = self._token_failure()
        self.assertEqual(exc.binding_id, self.binding.pk)
        self.assertEqual(exc.tenant_schema, connection.schema_name)
        self.assertIs(exc.step, jidanpu_openapi.STEP_TOKEN)

    def test_the_attribution_is_not_a_place_for_credentials(self):
        """⭐ 加了归因之后，密钥 / token **照样**一个字都不进文本。"""
        for exc in (self._endpoint_failure(), self._token_failure()):
            with self.subTest(step=exc.step):
                text = f"{exc}{exc.user_text}{exc.user_action}{exc!r}"
                self.assertNotIn(SENTINEL_SECRET, text)
                self.assertNotIn(SENTINEL_TOKEN, text)

    def test_a_failure_without_an_origin_says_nothing_about_one(self):
        """`origin` 缺席时⛔ 不许印出 `None` 这种噪声（构造点可以不带它）。"""
        exc = OpenApiError(step=jidanpu_openapi.STEP_GAMES, kind=FailureKind.OTHER)
        self.assertIsNone(exc.binding_id)
        self.assertIsNone(exc.tenant_schema)
        self.assertNotIn("绑定", str(exc))
        self.assertNotIn("租户", str(exc))


# ── token 缓存 / 续期 ──────────────────────────────────────────────────────────

class TokenCacheTests(OpenApiTestBase):
    def test_the_token_request_is_the_documented_form(self):
        """`POST /oauth/token`（`grant_type=client_credentials` + `client_id` + `client_secret`）。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")
        token_calls = self.upstream.token_calls()
        self.assertEqual(len(token_calls), 1, "非空哨兵：token 那一次请求真的发出去了")
        call = token_calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["form"], {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": SENTINEL_SECRET,
        })

    def test_the_token_body_may_have_no_code_field(self):
        """⭐ 一条**文档明写**的形状差别：`/oauth/token` 的成功体是 OAuth2 平铺体（`access_token` / `token_type` /
        `expires_in`，没有 `code` 这一格），失败才回 `{"code": 0, "msg": …}`。⛔ 弄红它：取 token 也按「必须 `code > 0`」判
        ⇒ 第一段红（每次成功取 token 都判成失败 = 这条路完全用不了）；第二段反向对照：带成功 `code` 的成功体也照收。"""
        # ① 文档那个成功体：没有 `code`
        self.upstream.token_reply(payload={"access_token": SENTINEL_TOKEN,
                                           "token_type": "bearer", "expires_in": 7200})
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")
        self.assertEqual(self.upstream.last_api_call()["authorization"], f"Bearer {SENTINEL_TOKEN}")

        # ② 带成功 `code` 的成功体（另一种形状）也照收
        jidanpu_openapi.reset_token_cache()
        self.upstream.token_reply(payload={"code": 1, "access_token": SENTINEL_TOKEN,
                                           "token_type": "bearer", "expires_in": 7200})
        self.call("fetch_games")
        self.assertEqual(len(self.upstream.token_calls()), 2)
        self.assertEqual(len(self.upstream.api_calls()), 2)

    def test_two_calls_share_one_token(self):
        """⭐ 连着调两次**只取一次 token**。⛔ 弄红它：去掉缓存 ⇒ 这里变 2。
        ⚠️ 正向哨兵：两个端点都真的打了（否则「只取一次 token」在「一次都没打」时也成立）。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                       "data": ORDER_STATUS_DATA})
        with self.openapi() as client:
            client.fetch_games()
            client.get_order_status(order_id="remote-synthetic-0010")
        self.assertEqual(len(self.upstream.api_calls()), 2)
        self.assertEqual(len(self.upstream.token_calls()), 1)

    def test_a_second_client_reuses_the_cached_token(self):
        """⭐ 缓存是**按绑定**的（不是按客户端实例）：再开一个 `with` 也只用同一个 token。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")
        self.call("fetch_games")
        self.assertEqual(len(self.upstream.api_calls()), 2)
        self.assertEqual(len(self.upstream.token_calls()), 1)

    def test_the_token_is_sent_as_a_bearer_header(self):
        """token 只出现在 `Authorization: Bearer …` 里（不塞查询串、不塞请求体）。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")
        call = self.upstream.last_api_call()
        self.assertEqual(call["authorization"], f"Bearer {SENTINEL_TOKEN}")
        self.assertEqual(call["form"], {})
        self.assertNotIn(SENTINEL_TOKEN, str(call["params"]))

    def test_an_expired_token_is_fetched_again(self):
        """⭐ 按返回的 `expires_in` **续期**：到期后重新取，未到期不重取（两条都要钉）。"""
        clock = _Clock()
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        with mock.patch.object(jidanpu_openapi, "time", SimpleNamespace(monotonic=clock)):
            self.call("fetch_games")
            # ① 还没到期 ⇒ 不重取（否则「缓存」等于没有）
            clock.advance(60)
            self.call("fetch_games")
            self.assertEqual(len(self.upstream.token_calls()), 1)
            # ② 越过 `expires_in`（3600 秒）⇒ 必须重取
            clock.advance(3600)
            self.call("fetch_games")
            self.assertEqual(len(self.upstream.token_calls()), 2)
        self.assertEqual(len(self.upstream.api_calls()), 3)

    def test_a_short_expires_in_does_not_degenerate_into_a_fetch_every_time(self):
        """⚠️ `expires_in` 比续期余量还短时，⛔ **不许**退化成「每次调用都取一遍 token」。"""
        self.upstream.token_reply(expires_in=10)
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")
        self.call("fetch_games")
        self.assertEqual(len(self.upstream.api_calls()), 2)
        self.assertEqual(len(self.upstream.token_calls()), 1)

    def test_changing_the_secret_or_the_address_takes_a_new_token(self):
        """⭐ 缓存键含**密钥指纹与地址**：换了就得重新取（⛔ 不把 A 系统的 token 拿去打 B 系统）。
        ⚠️ 两个维度各一条：spec §3.1 F1 实测「两个不同的地址背后是两套系统」⇒ 地址换了必须重取，
        密钥换了同理。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")

        another_secret = _make_openapi_binding(username="rotated-cs",
                                              client_secret="sentinel-secret-rotated-0003",
                                              base_url=BASE_URL)
        with jidanpu_openapi.openapi_client(another_secret) as client:
            client.fetch_games()
        self.assertEqual(len(self.upstream.token_calls()), 2, "换了密钥竟然复用了旧 token")

        another_url = _make_openapi_binding(username="other-url-cs",
                                            base_url="https://jidanpu-openapi-b.test.invalid")
        with jidanpu_openapi.openapi_client(another_url) as client:
            client.fetch_games()
        self.assertEqual(len(self.upstream.token_calls()), 3, "换了地址竟然复用了旧 token")
        self.assertEqual(len(self.upstream.api_calls()), 3)

    def test_another_tenant_does_not_reuse_the_token(self):
        """⭐ 租户边界：两个租户的绑定 pk 可以都是 1 ⇒ 缓存键里必须有 schema。
        ⚠️ 两个 schema 的主键序列各自从 1 开始（`AGENTS.md` §5-G 点过这个坑）⇒ 用另一个 schema 造同 pk 的行，
        判据打「token 取了第二次」。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.call("fetch_games")
        keys = list(jidanpu_openapi._token_cache.keys())
        self.assertEqual(len(keys), 1, "非空哨兵：缓存里真的有一条")
        schema = keys[0][0]
        self.assertTrue(schema, "缓存键里的 schema 名是空的 ⇒ 跨租户会串 token")
        self.assertEqual(keys[0][1], self.binding.pk)


# ── ⛔ 只读那条路：取数与探测都不许发出写请求 ────────────────────────────────────
# ⚠️ 本模块现在有写封装了 ⇒「本模块不许有任何写请求」不是一句真话；这一节守的是仍在跑的那条路：
# 四个只读端点与「测试连接」的探测一个业务写请求都不打（写方法本身另有专门那一节）。

class ReadOnlyTests(OpenApiTestBase):
    def test_only_get_is_used_except_for_the_token_step(self):
        """⭐ **行为判据**：跑完四个只读端点，唯一的 POST 是取 token 那一次。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                       "data": ORDER_STATUS_DATA})
        self.upstream.reply(ORDER_PROCESS_IMGS_PATH, payload={"code": 1, "msg": None, "data": []})
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        self.upstream.reply(PROJECT_PATH, payload={"code": 1, "msg": None, "data": []})
        with self.openapi() as client:
            client.get_order_status(order_id="remote-synthetic-0011")
            client.fetch_order_process_imgs(order_id="remote-synthetic-0011")
            client.fetch_games()
            client.fetch_projects(game_id="1")
        methods = {row["path"]: set() for row in self.upstream.requests}
        for row in self.upstream.requests:
            methods[row["path"]].add(row["method"])
        self.assertEqual(len(methods), 5, f"应当恰好打到 5 个路径，实到 {sorted(methods)}")
        for path, seen in methods.items():
            with self.subTest(path=path):
                self.assertEqual(seen, {"POST"} if path == TOKEN_PATH else {"GET"})

    def test_the_module_declares_exactly_the_read_and_write_paths(self):
        """⭐ **结构性判据**：模块源码里 `/api/OpenPlatform/` 的路径字面量恰好是那九条（四个只读 ＋ 五个写），两组各自恰好；写成恰好集合（⛔ 不放宽成 `toContain`）⇒ 路径拼错一个当场红。
        ⚠️ 两条各自钉的理由（代价不一样）：只读少一条只是少取一项数（页面 / 日志上看得出）；写少一条 = 一次静默没写进去；多出一条写路径 = 多一条谁都能调、却没有任何判据钉过它参数表的写口子。⛔ 也不放宽成「包含」——那会让「路径常量被删掉、方法里手写字符串」悄悄过关。
        ⚠️ 五个写名字应当在本模块里出现（写封装就在那里）；「写接口名字一个都不许出现」的原意（探测不打写接口）由 `test_the_probe_never_touches_a_write_endpoint` 接着守；⛔ 弄红它：任一条写路径常量改错（如 `.../ModifyOrders`）⇒ `write_paths` 与总数两条都红。"""
        source = Path(jidanpu_openapi.__file__).read_text(encoding="utf-8")
        found = set(re.findall(r'"(/api/OpenPlatform/[A-Za-z]+)"', source))
        read_paths = set(READ_ENDPOINT_PATHS)
        write_paths = set(WRITE_ENDPOINT_PATHS)
        #: ⚠️ 先钉两组**各自的条数**（登记表本身是判据的一部分）：否则「模块里删掉一条、
        #: 这里也顺手删一条」这两下会互相抵消，而网一句话都不说。
        self.assertEqual(len(read_paths), 4, "只读那一组的登记表本身要恰好四条")
        self.assertEqual(len(write_paths), 5, "写那一组的登记表本身要恰好五条")
        self.assertEqual(
            found & read_paths, read_paths, f"只读路径少 / 拼错了：{sorted(read_paths - found)}"
        )
        self.assertEqual(
            found & write_paths, write_paths, f"写路径少 / 拼错了：{sorted(write_paths - found)}"
        )
        self.assertEqual(
            found, read_paths | write_paths,
            f"多出了没登记过的路径：{sorted(found - read_paths - write_paths)}",
        )


# ── 二期步骤 1：五个写端点（只加封装；⛔ 本模块里今天没有任何调用方）。这一节钉五件事（每件都能红）：
# ① ⭐ 失败信封必须被判失败——含 `code = 0` + 非空 `msg` 那种假成功（E-0087 的原始坑）；② ⭐ 成功——`code > 0` 且 `code` 取不固定的值（⛔ 别把判据写成 `code == 1`）；③ 参数名逐字（打桩读 JSON 体，断键集合与值；⭐ 尤其 `assignPrice` 那一格）；④ 鉴权头 / 基址照既有只读方法（`Authorization: Bearer …` ＋ 那一行自己的地址）；⑤ 三条结构性规则（`orderId`/`sourceId` 二选一、`ModifyOrder` 至少一个、`remark` 条件必需）＋ 一条不按枚举校验 `status`（实测那三个值文档里没有）。
# ⚠️ 判据一律打在真的发出去的请求与真的抛出来的异常上（⛔ 不打在 docstring 里）；本组只测封装本身——这五个方法今天一个调用方都没有，「上线后行为零变化」的判据在别处（那些调用方自己的用例）。

#: ⭐ 五个写方法各一份**合成**参数 ＋ 那一发**应当**发出去的 JSON 体（键与值都逐个钉）。
#: ⚠️ 一份表驱动到底（五个端点各写一遍五个用例 = 25 段几乎一样的代码，那种重复必然漂）；⚠️ ⛔ 夹具一律合成值（`AGENTS.md` §5-C / E-0077）：`测试…甲/乙`、`*-synthetic-*`、`.invalid`。
#: ⚠️ `step` 用字面量（⛔ 不用 `jidanpu_openapi.STEP_*`）：那才是「这一步叫什么」的判据，而 `STEP_LABELS` 是按这些名字取人话的（下面会顺手钉「名字登记过」）。
WRITE_CALLS: dict[str, dict] = {
    "create_order": {
        "path": CREATE_ORDER_PATH,
        "step": "CreateOrder",
        "kwargs": {
            "game_name": "测试游戏甲", "game_area": "测试大区甲", "project": "测试项目甲",
            "hours": 2, "order_soures": "测试来源甲",
            #: ⭐ 这两格是**这个端点最容易写错的地方**：`price` 是**订单总价**（20），
            #: 「我方发单价」是 `assignPrice`（10）—— 照字面把 10 落进 `price` 就是把
            #: 「给打手的价」当成「订单总价」写进对方的单（已实测的口径，二期计划 §2.4）。
            "price": 20, "assign_price": 10,
            "customer_id": "customer-synthetic-0001", "rec_user": "0",
            "source_order_id": "TT-SYNTHETIC-0001", "remark": "合成备注",
        },
        "body": {
            "gameName": "测试游戏甲", "gameArea": "测试大区甲", "project": "测试项目甲",
            "hours": 2, "orderSoures": "测试来源甲",
            "price": 20, "assignPrice": 10,
            "customerId": "customer-synthetic-0001", "recUser": "0",
            "souresOrderID": "TT-SYNTHETIC-0001", "remark": "合成备注",
        },
        #: 录单成功时上游回的那一格：**系统订单号**（文档：拆单时逗号分隔）。
        "data": "20260102030405060708",
    },
    "change_order_status": {
        "path": CHANGE_ORDER_STATUS_PATH,
        "step": "ChangeOrderStatus",
        "kwargs": {"order_id": "remote-synthetic-0002", "status": "待上号", "remark": ""},
        "body": {"orderId": "remote-synthetic-0002", "status": "待上号", "remark": ""},
        "data": None,
    },
    "modify_order_price": {
        "path": MODIFY_ORDER_PRICE_PATH,
        "step": "ModifyOrderPrice",
        "kwargs": {"order_id": "remote-synthetic-0003", "price": 55},
        "body": {"orderId": "remote-synthetic-0003", "price": 55},
        "data": "",
    },
    "modify_order": {
        "path": MODIFY_ORDER_PATH,
        "step": "ModifyOrder",
        "kwargs": {
            "game_name": "测试游戏乙", "project": "测试项目乙",
            #: ⭐⭐ **真机补齐的那一格**（文档标「可选」、**实际必填**：
            #: 少它 ⇒ 对方回 `code=0 游戏大区为必填项`）。
            "game_area": "测试大区乙",
            "order_id": "remote-synthetic-0004", "source_order_id": "TT-SYNTHETIC-0004",
        },
        #: ⚠️ 这一格的单号叫 **`orderID`**（大写 `ID`），别的端点叫 `orderId` —— 逐字钉住它。
        #: ⭐⭐ **恰好这 5 格**：`gameArea` 与 `souresOrderID` 都是 真机读数实测的必填格
        #: （文档只写「`orderID`/`souresOrderID` 至少一项」、把 `gameArea` 标成可选 ⇒ 同一次改准）。
        "body": {
            "gameName": "测试游戏乙", "gameArea": "测试大区乙", "project": "测试项目乙",
            "orderID": "remote-synthetic-0004", "souresOrderID": "TT-SYNTHETIC-0004",
        },
        "data": None,
    },
    "upload_img": {
        "path": UPLOAD_IMG_PATH,
        "step": "UploadImg",
        "kwargs": {
            "img_url": "https://jidanpu-member-openapi.test.invalid/pic-synthetic-0001.jpg",
            "note": "合成说明甲", "source_id": "TT-SYNTHETIC-0005", "img_type": 1,
        },
        #: ⚠️ 这一格叫 **`ImgType`**（大驼峰），别的都是小驼峰 —— 逐字钉住它。
        "body": {
            "sourceId": "TT-SYNTHETIC-0005",
            "imgUrl": "https://jidanpu-member-openapi.test.invalid/pic-synthetic-0001.jpg",
            "note": "合成说明甲", "ImgType": 1,
        },
        "data": None,
    },
}

#: ⭐ 五个写端点各一条**成功**读数：`code` 取不固定的值（⛔ 谁把它写成 `code == 1`，这里就红）。
#: · `605764` = 实测的录单成功码（spec §3.2 F6；`is_success_code` 的 docstring 也记着它）；· `200` = 文档给 `ModifyOrderPrice` 的那个成功示例 ⇒ 同一家的成功码本来就不固定；
#: · 其余三个是刻意取的怪值：只要 `> 0` 就必须判成功。
WRITE_SUCCESS_CODES: dict[str, int] = {
    "create_order": 605764,
    "change_order_status": 8412,
    "modify_order_price": 200,
    "modify_order": 6,
    "upload_img": 90210,
}

#: ⭐ 三个**必须被判失败**的信封（每个写端点各跑一遍）。
WRITE_FAILURE_ENVELOPES: tuple[dict, ...] = (
    #: ① ⭐ **假成功**：`code = 0` ＋ 一句**非空** `msg` —— 宽松解包（只判 `data` 键在不在）
    #:    会放行它，而它其实是一次**失败**（对方一个字都没写）。**这一条就是 E-0087 的原始坑**，
    #:    也是本节存在的第一理由。
    {"code": 0, "msg": "创建失败：游戏名称不存在", "data": None},
    #: ② 负码（`code > 0` 才是成功 ⇒ 负码是失败）。
    {"code": -1, "msg": "参数错误", "data": None},
    #: ③ **压根没有 `code` 这一格**（上游改版 / 打错了地址）：fail-closed，必须判失败。
    {"msg": "系统繁忙", "data": None},
)


class WriteEndpointTests(OpenApiTestBase):
    """二期步骤 1 的五个写方法（⚠️ 只测**封装本身**：这些方法今天没有任何调用方）。"""

    # ── 小工具 ────────────────────────────────────────────────────────────────
    def write_ok(self, name) -> dict:
        """让上游对该端点回一个**成功**信封，调一次那个方法，回**那一发请求的记录**。"""
        spec = WRITE_CALLS[name]
        self.upstream.requests.clear()
        self.upstream.reply(spec["path"], payload={
            "code": WRITE_SUCCESS_CODES[name], "msg": "成功", "data": spec["data"],
        })
        self.call(name, **spec["kwargs"])
        rows = self.upstream.api_calls()
        self.assertEqual(len(rows), 1, f"{name} 应当恰好发一发业务请求，实到 {rows}")
        return rows[0]

    # ── ① 失败信封必须被判失败 ────────────────────────────────────────────────
    def test_every_write_endpoint_judges_a_failure_envelope_as_a_failure(self):
        """⭐ **失败信封必须被判失败**（`code <= 0` ⇒ 必须抛 `OpenApiError`），三个信封各跑一遍。
        ⭐ 第一条是 E-0087 的原始坑：`code = 0` ＋ 非空 `msg` 的假成功信封——宽松解包（只判 `data` 键在不在）会放行它、「一次静默的失败」被记成成功；⛔ 弄红它：去掉 `_write` 里那一句 `_raise_for_envelope(...)`（或把 `is_success_code` 改成 `code >= 0`）⇒ 当场红。
        ⚠️ 判据不止「抛了」：异常必须带得上游的 `code` 与对方原话（带不上原文人无从知道对方说了什么——spec §9.3 纪律 1）。"""
        for name, spec in WRITE_CALLS.items():
            for envelope in WRITE_FAILURE_ENVELOPES:
                with self.subTest(method=name, code=envelope.get("code")):
                    jidanpu_openapi.reset_token_cache()
                    self.upstream.requests.clear()
                    self.upstream.reply(spec["path"], payload=dict(envelope))
                    with self.assertRaises(OpenApiError) as ctx:
                        self.call(name, **spec["kwargs"])
                    exc = ctx.exception
                    self.assertEqual(exc.step, spec["step"])
                    self.assertEqual(exc.upstream_code, envelope.get("code"))
                    if envelope.get("msg"):
                        self.assertIn(envelope["msg"], str(exc))
                    #: 人话名字登记过（否则失败文案里会出现一个裸的英文步骤名）
                    self.assertTrue(jidanpu_openapi.STEP_LABELS.get(spec["step"]), exc.step)
                    #: 正向哨兵：那一发**真的**打出去了（否则这是「压根没调用」造出来的假绿）
                    self.assertEqual(
                        [row["path"] for row in self.upstream.api_calls()], [spec["path"]],
                    )

    # ── ② 成功（`code` 不固定）────────────────────────────────────────────────
    def test_every_write_endpoint_succeeds_on_a_non_fixed_code(self):
        """⭐ 每个写端点一条**成功**：`code > 0` 就算成功，⚠️ `code` 取不固定的值。
        ⛔ 弄红它：判据写成 `code == 1` ⇒ `create_order`（`605764`）/ `change_order_status`（`8412`）/
        `modify_order`（`6`）/ `upload_img`（`90210`）四行当场红——写端点的成功码不是 `1`、也不固定（`is_success_code` 的 docstring 记的正是这件事）；⚠️ 返回值应当是上游 `data` 原样（录单那一格就是系统订单号）。"""
        for name, spec in WRITE_CALLS.items():
            with self.subTest(method=name, code=WRITE_SUCCESS_CODES[name]):
                jidanpu_openapi.reset_token_cache()
                self.upstream.requests.clear()
                self.upstream.reply(spec["path"], payload={
                    "code": WRITE_SUCCESS_CODES[name], "msg": "成功", "data": spec["data"],
                })
                result = self.call(name, **spec["kwargs"])
                #: 正向哨兵：请求**真的**发出去了（否则「没抛异常」在「一次都没打」时也成立）
                self.assertEqual(len(self.upstream.api_calls()), 1, self.upstream.requests)
                self.assertEqual(result, spec["data"], "返回值应当是上游 data 原样")
                self.assertTrue(jidanpu_openapi.is_success_code(WRITE_SUCCESS_CODES[name]))

    def test_a_successful_write_is_not_turned_into_a_failure_by_the_data_shape(self):
        """⭐ 写**成功之后**⛔ **不因 `data` 的形状**把这一次写入报成失败。
        ⚠️ `code > 0` 时对方已经写进去了；若再补一道「`data` 必须是字符串」的形状判据，调用方会收到异常 ⇒ 它会重试
        ⇒ 在对家建出第二张单（或把同一次状态推两遍）⇒ 形状是调用方按用途自己判的事；⛔ 弄红它：`_write` 里加「`data` 不是字符串就抛」⇒ 本用例红。"""
        spec = WRITE_CALLS["create_order"]
        for data in (None, "", {"unexpected": True}, ["a", "b"]):
            with self.subTest(data=data):
                jidanpu_openapi.reset_token_cache()
                self.upstream.requests.clear()
                self.upstream.reply(spec["path"], payload={
                    "code": WRITE_SUCCESS_CODES["create_order"], "msg": "创建成功", "data": data,
                })
                self.assertEqual(self.call("create_order", **spec["kwargs"]), data)

    # ── ③ 参数名逐字 ──────────────────────────────────────────────────────────
    def test_every_write_endpoint_sends_the_documented_parameters_verbatim(self):
        """⭐ **参数名逐字**：打桩读请求体，键集合与值逐个钉（⛔ 不写成 `toContain`）。
        ⭐ 尤其 `create_order` 那一格：我方发单价要走 `assignPrice`（⛔ 不许落 `price`）——已实测：落 `price` 会把「给打手的价」当成「订单总价」写进对方的单；⛔ 弄红它：`body["assignPrice"]` 改成 `body["price"]` ⇒ 本用例红。
        ⚠️ 断言键集合全等（多一个没登记的参数、少一个必填参数都当场红）；请求体必须是 JSON（`content-type: application/json`），⛔ 不是表单。"""
        for name, spec in WRITE_CALLS.items():
            with self.subTest(method=name):
                jidanpu_openapi.reset_token_cache()
                sent = self.write_ok(name)
                self.assertEqual(sent["path"], spec["path"])
                self.assertEqual(sent["method"], "POST")
                self.assertIn("json", sent["content_type"] or "", sent["content_type"])
                self.assertEqual(set(sent["json"]), set(spec["body"]), "参数名集合不对")
                for key, value in spec["body"].items():
                    with self.subTest(method=name, param=key):
                        self.assertEqual(sent["json"][key], value)
                #: ⛔ 参数不许同时塞进查询串（这条路的读端点用 query、写端点用 JSON，两套形状）
                self.assertEqual(sent["params"], {})

    # ── ④ 鉴权头 / 基址 ───────────────────────────────────────────────────────
    def test_a_write_call_uses_the_bearer_header_and_that_rows_own_base_url(self):
        """⭐ **鉴权头 / 基址**照既有只读方法：`Authorization: Bearer <token>` ＋ **这一行**的地址。
        ⚠️ 两件都钉在真的发出去的那一发上（⛔ 不打在 `repr` 或注释里）：业务那几发只带 `Bearer`（`client_secret` 只在取 token 那一发里出现）；地址取这一行的 `jidanpu_base_url` ⇒ 判据用整条 URL（只判 `path` 看不出基址取错 / 取空），用两行地址不同的绑定各打一发来钉。
        ⛔ 弄红它：去掉 `_post` 里的 `headers={"Authorization": …}` ⇒ 第一段红。"""
        spec = WRITE_CALLS["create_order"]
        sent = self.write_ok("create_order")
        self.assertEqual(sent["authorization"], f"Bearer {SENTINEL_TOKEN}")
        self.assertEqual(sent["url"], f"{BASE_URL}{spec['path']}")
        #: ⛔ 密钥 / token 都不许出现在业务请求体里
        payload = json.dumps(sent["json"], ensure_ascii=False)
        self.assertNotIn(SENTINEL_SECRET, payload)
        self.assertNotIn(SENTINEL_TOKEN, payload)
        #: 非空哨兵：token 真的取了一次，且密钥只出现在那一发的表单里
        token_calls = self.upstream.token_calls()
        self.assertEqual(len(token_calls), 1, "非空哨兵：token 真的取了一次")
        self.assertEqual(token_calls[0]["form"].get("client_secret"), SENTINEL_SECRET)

        #: 另一**行**绑定（地址刻意不同）⇒ 写请求打的是**它自己**的地址
        other = _make_openapi_binding(username="openapi-write-other", base_url=MEMBER_BASE_URL)
        self.upstream.requests.clear()
        self.upstream.reply(spec["path"], payload={
            "code": WRITE_SUCCESS_CODES["create_order"], "msg": "成功", "data": spec["data"],
        })
        with jidanpu_openapi.openapi_client(other) as client:
            client.create_order(**spec["kwargs"])
        other_call = self.upstream.last_api_call()
        self.assertEqual(other_call["url"], f"{MEMBER_BASE_URL}{spec['path']}")

    # ── ⑤ 三条结构性规则 + `status` 不按枚举校验 ──────────────────────────────
    def test_the_two_order_reference_rules_are_not_the_same_rule(self):
        """`orderId`/`sourceId` 的**二选一**与 `ModifyOrder` 的**至少一个**（⛔ 两条不是同一条规则）。
        ⚠️ 判据三条：① 两个都不给 ⇒ `ValueError`；② 两个都给 ⇒ `ValueError`（只对「二选一」那两个端点）；
        ③ 正向对照（单独给任一个都能真的发出去，否则上面两条因「路径压根走不通」假绿）；⛔ 弄红它：`_exactly_one_ref` 的 `len(given) != 1` 放宽成 `< 1` ⇒ 第一段红。"""
        for name, kwargs_key in (("change_order_status", "order_id"), ("upload_img", "source_id")):
            spec = WRITE_CALLS[name]
            with self.subTest(method=name):
                #: ① 两个都不给 ⇒ 调用方的编程错误（⛔ 不是上游失败，⛔ 也不发请求）
                without = {key: value for key, value in spec["kwargs"].items()
                           if key not in ("order_id", "source_id")}
                self.upstream.requests.clear()
                with self.assertRaises(ValueError):
                    self.call(name, **without)
                self.assertEqual(self.upstream.requests, [], "被判为调用错误时⛔ 一发都不许打")
                #: ② 两个都给 ⇒ 同上
                with self.assertRaises(ValueError):
                    self.call(name, **{**spec["kwargs"], "order_id": "remote-synthetic-0009",
                                       "source_id": "TT-SYNTHETIC-0009"})
                self.assertEqual(self.upstream.requests, [])
                #: ③ 正向对照：**只给**其中一个 ⇒ 真的发出去（否则上面的红是「走不通」凑的）
                only_one = {key: value for key, value in spec["kwargs"].items()
                            if key not in ("order_id", "source_id")}
                only_one[kwargs_key] = spec["kwargs"][kwargs_key]
                jidanpu_openapi.reset_token_cache()
                self.upstream.requests.clear()
                self.upstream.reply(spec["path"], payload={
                    "code": WRITE_SUCCESS_CODES[name], "msg": "成功", "data": None,
                })
                self.call(name, **only_one)
                self.assertEqual(len(self.upstream.api_calls()), 1)

        #: `ModifyOrder`：**至少一个**（两个都给是**合法**的 —— 与上面那两个端点规则不同）。
        #: ⚠️ 这一条判的是**本地**那道「明显编程错误」闸（比实测松，见 `modify_order` 的
        #: docstring「文档 vs 实际」：实测**两个都必填**）⇒ ⛔ 别把它读成「给一个就够」。
        spec = WRITE_CALLS["modify_order"]
        self.upstream.requests.clear()
        with self.assertRaises(ValueError):
            self.call("modify_order", game_name="测试游戏乙", project="测试项目乙",
                      game_area="测试大区乙")
        self.assertEqual(self.upstream.requests, [])
        sent = self.write_ok("modify_order")  # 两个都给：照发，且两格都在（上面已有键集合判据）
        self.assertEqual(sent["json"]["orderID"], "remote-synthetic-0004")
        self.assertEqual(sent["json"]["souresOrderID"], "TT-SYNTHETIC-0004")

    def test_the_game_area_cell_of_modify_order_has_no_default(self):
        """⭐⭐ 判据：`game_area` 是**必填形参**（⛔ 没有缺省值）⇒ 少它就在**调用处**当场炸。
        ⚠️ 为什么必须是「无缺省」：真机实测少这一格 ⇒ 对方回 `code=0 游戏大区为必填项`、每一发都被拒（写着 `failed` 退避到 `abandoned`）；给它自造缺省值等于替调用方编一个大区（会把单改到别的大区上，比失败更坏）⇒ 让它缺不了，调用方必须显式给出（②式那条线的取值口是 `jidanpu_member_openapi_outbox._game_area(order)`）。
        ⛔ 弄红它：给 `game_area` 加一个缺省值（如 `game_area=""` 或某个常量）⇒ 本用例红。"""
        spec = WRITE_CALLS["modify_order"]
        without = {k: v for k, v in spec["kwargs"].items() if k != "game_area"}
        self.upstream.requests.clear()
        with self.assertRaises(TypeError):
            self.call("modify_order", **without)
        self.assertEqual(self.upstream.requests, [], "参数不齐时⛔ 一发都不许打")

    def test_change_order_status_does_not_police_the_status_enumeration(self):
        """⭐ 文档那份 `status` 枚举**不全**：实测 `未分配` / `待上号` / `已退单` 都收（二期计划 §2.3 / §2.4）
        ⇒ ⛔ 本方法不按枚举校验，值逐字转发；⛔ 弄红它：加一句「只收文档那六个值」的校验 ⇒ 本用例红
        （那会把实测能用的值挡在门外——判据的出处是对方，不是文档那份枚举）。"""
        spec = WRITE_CALLS["change_order_status"]
        for status in ("未分配", "待上号", "已退单"):
            with self.subTest(status=status):
                jidanpu_openapi.reset_token_cache()
                self.upstream.requests.clear()
                self.upstream.reply(spec["path"], payload={
                    "code": WRITE_SUCCESS_CODES["change_order_status"], "msg": "已成功提交1笔订单",
                    "data": None,
                })
                self.call("change_order_status", order_id="remote-synthetic-0002", status=status,
                          remark="合成备注")
                sent = self.upstream.last_api_call()
                self.assertEqual(sent["json"]["status"], status, "状态值必须逐字转发")

    def test_a_remark_is_required_for_the_two_statuses_the_doc_names(self):
        """文档明写：改**问题单** / **已暂停** 时 `remark` 必需 ⇒ ⛔ **本地就挡**（`ValueError`）。
        ⚠️ 两条正向对照（否则这条判据会把合法调用一起挡掉）：① 这两个状态给了 `remark` ⇒ 照发；② 别的状态空 `remark` ⇒ 照发；
        ⛔ 弄红它：去掉 `change_order_status` 里那一句 `raise ValueError(…)` ⇒ 第一段红。"""
        spec = WRITE_CALLS["change_order_status"]
        for status in ("问题单", "已暂停"):
            with self.subTest(status=status, remark="缺失"):
                self.upstream.requests.clear()
                with self.assertRaises(ValueError):
                    self.call("change_order_status", order_id="remote-synthetic-0002", status=status)
                self.assertEqual(self.upstream.requests, [], "本地判出的调用错误⛔ 不许打上游")
            with self.subTest(status=status, remark="空白"):
                with self.assertRaises(ValueError):
                    self.call("change_order_status", order_id="remote-synthetic-0002", status=status,
                              remark="   ")
                self.assertEqual(self.upstream.requests, [])
            with self.subTest(status=status, remark="给了"):
                jidanpu_openapi.reset_token_cache()
                self.upstream.requests.clear()
                self.upstream.reply(spec["path"], payload={
                    "code": WRITE_SUCCESS_CODES["change_order_status"], "msg": "成功", "data": None,
                })
                self.call("change_order_status", order_id="remote-synthetic-0002", status=status,
                          remark="合成备注甲")
                self.assertEqual(self.upstream.last_api_call()["json"]["remark"], "合成备注甲")
        #: ② 别的状态 + 空 `remark` ⇒ 照发（⛔ 不是「有 remark 就拦」）
        self.write_ok("change_order_status")

    def test_remark_none_omits_the_key_while_a_blank_string_still_sends_it(self):
        """⭐ **三档取值**：`remark=None` ⇒ **整格不发**；空串 / 缺省 ⇒ 照旧发一个空的这一格。
        ⚠️ `None` 那档的请求体被逐字钉成恰好两格（退单 / 撤下大厅那一档）；空串照发是已经上线的推状态那一档的形态（照①式那份表单恒发这一格）⇒ 两条各自能红；「把空串也顺手省掉」是没在真机上量过的报文形态变更——纪律是「先加能力、⛔ 不动已经上线的东西」。
        ⛔ 弄红它：去掉 `change_order_status` 里 `if remark is not None:` 那道判断（回到恒发）⇒ 第一条红；改成 `if remark:` ⇒ 第二条红。"""
        self.write_ok("change_order_status")
        #: ① ⭐ `None` ⇒ 键都不出现：
        self.upstream.requests.clear()
        jidanpu_openapi.reset_token_cache()
        self.call("change_order_status", order_id="remote-synthetic-0002",
                  status="已退单", remark=None)
        sent = self.upstream.last_api_call()["json"]
        self.assertEqual(sent, {"orderId": "remote-synthetic-0002", "status": "已退单"})
        self.assertNotIn("remark", sent)
        #: ② 空串（缺省）⇒ 照旧发一个**空的**这一格（⛔ 不许顺手改掉）：
        self.upstream.requests.clear()
        jidanpu_openapi.reset_token_cache()
        self.call("change_order_status", order_id="remote-synthetic-0002", status="已退单")
        sent = self.upstream.last_api_call()["json"]
        self.assertEqual(sent.get("remark"), "")
        self.assertIn("remark", sent)


# ── ⛔ 密钥与 token 不进日志 / 异常 / 返回值 ──────────────────────────────────────

class SecretDisciplineTests(OpenApiTestBase):
    def test_the_secret_and_the_token_never_appear_in_errors_or_logs(self):
        """⛔ 哨兵密钥与哨兵 token **都不许**出现在异常文本、日志与 `repr` 里。
        ⚠️ 对抗性构造：让上游把我们的密钥回显在 `msg` 里（真实存在的形态：服务端把请求体塞进校验错文案）；① 档文案必须把对方原文带给用户（spec §9.3 纪律 1）⇒ 本用例钉「带原文」与「不泄漏」两件事同时成立。
        ⚠️ 正向哨兵：真的抛了异常、真的记了日志（否则下面的否定断言在"什么都没发生"时也成立）。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 0, "msg": f"{OUTER_GATE_MSG}（请求体：{SENTINEL_SECRET}）", "data": None,
        })
        with self.assertLogs(level="DEBUG") as logs:
            with self.assertRaises(OpenApiError) as ctx:
                self.call("get_order_status", order_id="remote-synthetic-0012")

        self.assertTrue(logs.output, "正向哨兵：一条日志都没记 ⇒ 下面的否定断言没有判别力")
        texts = {
            "异常文本": str(ctx.exception),
            "异常 repr": repr(ctx.exception),
            "user_text": ctx.exception.user_text,
            "日志": "\n".join(logs.output),
        }
        for label, text in texts.items():
            with self.subTest(where=label):
                self.assertNotIn(SENTINEL_SECRET, text)
                self.assertNotIn(SENTINEL_TOKEN, text)
        # 脱敏真的发生了（⛔ 不是"压根没带原文"凑出来的）
        self.assertIn(jidanpu_openapi.REDACTED, ctx.exception.user_text)
        self.assertIn(OUTER_GATE_MSG, ctx.exception.user_text)

    def test_the_client_repr_and_the_token_holder_hide_the_values(self):
        """`repr(client)` 与 token 持有者的 `repr` 里都不许出现密钥 / token。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": []})
        with self.openapi() as client:
            client.fetch_games()
            self.assertNotIn(SENTINEL_SECRET, repr(client))
            self.assertNotIn(SENTINEL_TOKEN, repr(client))
            self.assertIn(jidanpu_openapi.REDACTED, repr(client))
            # 客户端实例上**没有** token 列（⇒ 它不可能出现在任何返回结构里）
            self.assertFalse(hasattr(client, "token"))
            holder = list(jidanpu_openapi._token_cache.values())[0]
            self.assertNotIn(SENTINEL_TOKEN, repr(holder))
        self.assertEqual(len(jidanpu_openapi._token_cache), 1, "非空哨兵：缓存里真的有一条")

    def test_the_returned_structures_carry_no_token(self):
        """返回值里只有业务值（⛔ 没有 token、没有密钥）。
        ⚠️ 字段表是恰好集合（照本仓「不让凭据混进返回结构」那条纪律）：真机实测回包里没有 `order_id` / `price` / `rec_fee` 那三格 ⇒ 字段表是实测的 5 个（多写一格就是一句假话，见 `OrderStatus` 的那一节）；
        ⛔ 弄红它：往 `OrderStatus` 上加一个带 token / 密钥的字段 ⇒ 本用例红。"""
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                       "data": ORDER_STATUS_DATA})
        status = self.call("get_order_status", order_id="remote-synthetic-0013")
        self.assertNotIn(SENTINEL_TOKEN, repr(status))
        self.assertNotIn(SENTINEL_SECRET, repr(status))
        self.assertEqual(
            [field_name for field_name in status.__dataclass_fields__],
            ["status", "creat_time", "oper_time", "done_time", "verify_time"],
        )
        # ⭐ 与那条「恰好 5 格」的网**同一个期望**（两处都是字面量 ⇒ 谁改坏一处都红）。
        self.assertEqual(set(status.__dataclass_fields__), ORDER_STATUS_REAL_FIELDS)

    def test_a_config_error_from_a_missing_row_says_nothing_about_values(self):
        """整行都没有（`None`）⇒ 同样是「不是②式」的配置错，且文案里没有值。"""
        with self.assertRaises(jidanpu_openapi.ConfigError) as ctx:
            with jidanpu_openapi.openapi_client(None):
                self.fail("`None` 竟然建出了客户端")
        self.assertNotIn(SENTINEL_SECRET, str(ctx.exception))
        self.assertEqual(self.upstream.requests, [])


# ②式的「测试连接」= 逐条只读探测（spec §9.2 / §11.4 / §20.2）：这一组钉的是探测那一趟本身（顺序 / 省着打 / 三档 / 逐条表 / 不泄漏），
# 端点那一层（`POST …/test-connection/` 的出参形状、按 `mode` 分流、不落库）在 `tests/test_jidanpu_upstream_api.py` 里。

#: 一个形状正确的游戏行（`id` 就是 `GetProject` 要的 `gameId`）。
GAME_ROW = {"id": "game-synthetic-0001", "name": "测试游戏甲"}
#: 对方对一个**不存在的单号**会给的那种业务失败（spec §9.2 的 ③ 探测法）。
NO_SUCH_ORDER_MSG = "订单不存在"


class ProbeTests(OpenApiTestBase):
    """`probe_read_endpoints` 的判据（逐条表 + 省着打 + 三档话术）。
    ⚠️ 零真实网络（沿用本文件的 `_Upstream` = 真 `httpx.Client` + `MockTransport`）；夹具一律合成值；
    ⛔ 一个写接口都不打（另有专门用例钉住）。"""

    def probe(self):
        return jidanpu_openapi.probe_read_endpoints(self.binding)

    def rows(self, report):
        """逐条表压成 `{step: 行}`（断言的落点是**行**，不是「列表里第几个」）。"""
        return {row.step: row for row in report.checks}

    def happy_upstream(self):
        """一个「四项都通」的假上游（默认回包在 `GetGames` 上不是列表 ⇒ 必须显式配）。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": [GAME_ROW]})
        return self.upstream

    def test_the_table_lists_every_probed_step_in_order(self):
        """⭐ 逐条表：**恰好**那五项、顺序固定（token 在最前）。"""
        self.happy_upstream()
        report = self.probe()
        self.assertEqual([row.step for row in report.checks], list(jidanpu_openapi.PROBE_ORDER))
        self.assertEqual(
            [row.step for row in report.checks],
            ["token", "GetGames", "GetProject", "GetOrderStatus", "GetOrderProcessImgs"],
        )
        for row in report.checks:
            with self.subTest(step=row.step):
                self.assertTrue(row.probed, row.skip_reason)
                self.assertTrue(row.ok, row.reason)
                self.assertEqual(row.kind, "")
                self.assertEqual(row.message, "")
                self.assertTrue(row.label, "每一行都要有人话名字（页面直接显示它）")
        self.assertTrue(report.ok)
        self.assertEqual(report.messages, ())
        self.assertEqual(report.actions, ())

    def test_the_probe_spends_exactly_one_call_per_endpoint(self):
        """⭐ **省着打**：1 次 token + 4 次只读 = 恰好 5 发；`GetProject` 复用 `GetGames` 的 id。
        ⛔ 弄红它：把 `GetProject` 改成自己去拿一次游戏列表 / 用别的 id ⇒ 请求集合就变了。"""
        self.happy_upstream()
        report = self.probe()
        self.assertTrue(report.ok)

        self.assertEqual(len(self.upstream.token_calls()), 1)
        self.assertEqual(len(self.upstream.requests), 5, self.upstream.requests)
        self.assertEqual(
            sorted(row["path"] for row in self.upstream.api_calls()),
            sorted([GAMES_PATH, PROJECT_PATH, ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH]),
        )
        by_path = {row["path"]: row for row in self.upstream.api_calls()}
        #: `gameId` 就是上一步第一行的 `id`（⛔ 不是另造一个）
        self.assertEqual(by_path[PROJECT_PATH]["params"], {"gameId": GAME_ROW["id"]})
        #: 两个带单号的探测用的是**那个不存在的探单号**
        for path in (ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH):
            with self.subTest(path=path):
                self.assertEqual(by_path[path]["params"], {"orderId": jidanpu_openapi.PROBE_ORDER_ID})
                self.assertEqual(by_path[path]["method"], "GET")

    def test_a_failed_token_stops_the_probe_and_says_why_the_rest_was_not_tried(self):
        """⭐ **失败即停**：取 token 就失败 ⇒ 后面四项**一发都不打**，但**每一行都在表里**。
        ⛔ 弄红它：把「失败即停」去掉（继续探那四项）⇒ 请求数从 1 变 5。"""
        self.upstream.token_reply(payload={"code": 0, "msg": OUTER_GATE_MSG})
        report = self.probe()

        self.assertEqual(len(self.upstream.requests), 1, self.upstream.requests)
        self.assertFalse(report.ok)
        rows = self.rows(report)
        self.assertTrue(rows["token"].probed)
        self.assertFalse(rows["token"].ok)
        self.assertEqual(rows["token"].kind, "credentials")
        for step in ("GetGames", "GetProject", "GetOrderStatus", "GetOrderProcessImgs"):
            with self.subTest(step=step):
                self.assertFalse(rows[step].probed)
                self.assertIn("未探测", rows[step].reason)
                self.assertIn(jidanpu_openapi.STEP_LABELS["token"], rows[step].reason)
                #: 未探测的那几行**继承**挡住它那一跳的指引（用户在**同一行**上就看到该做什么）
                self.assertEqual(rows[step].action, rows["token"].action)

    def test_the_outer_gate_and_the_inner_permission_are_two_different_sentences(self):
        """⭐ **§20.1 的两层权限 = 两套指引**（这是本条线的承重口径）：外层（官方没开通）`您没有开放平台接入权限` ⇒ 「联系鸡蛋铺开通 / 改用账号+密码」；内层（那个 API 子账户缺这一项）`没有权限` ⇒ 「去权限设置里给这个 API 子账户开」。
        ⛔ 两条不许是同一句、也⛔ 不许互相串味（外层那句里不许出现"权限设置"、内层那句里不许出现"联系鸡蛋铺"）——合并成一句就是本条线最忌讳的那种错。"""
        # ── 外层：取 token 就被拒 ───────────────────────────────────────────────
        self.upstream.token_reply(payload={"code": 0, "msg": OUTER_GATE_MSG})
        outer = self.probe()
        jidanpu_openapi.reset_token_cache()
        outer_text = "\n".join(outer.messages)
        outer_action = "\n".join(outer.actions)
        self.assertIn("联系鸡蛋铺", outer_text)
        self.assertIn("开通", outer_text)
        self.assertIn("账号 + 密码", outer_text)          # 「或者改用账号 + 密码方式对接」
        self.assertNotIn("权限设置", outer_text)

        # ── 内层：token 通了，但某一项没权限 ─────────────────────────────────────
        self.upstream.token_reply(payload=None)           # 恢复默认（能拿到 token）
        self.happy_upstream()
        self.upstream.reply(GAMES_PATH, payload={"code": 0, "msg": "没有权限", "data": None})
        inner = self.probe()
        rows = self.rows(inner)
        inner_action = rows["GetGames"].action
        inner_text = "\n".join(inner.messages)

        self.assertEqual(rows["GetGames"].kind, "permission")
        self.assertEqual(rows["GetGames"].reason, "没有权限")
        self.assertIn("权限设置", inner_action)
        self.assertIn("子账户", inner_action)
        self.assertIn("你自己就能做", inner_action)
        #: ⛔ 内层那句**不许**写「请联系鸡蛋铺」（spec §20.2-2：按"你能自己开"写）
        self.assertNotIn("联系鸡蛋铺", inner_action)
        #: 内层点名到**那一个**接口，并照 spec §11.5 说清「不影响订单状态同步」
        self.assertIn(jidanpu_openapi.STEP_LABELS["GetGames"], inner_text)
        self.assertIn("不影响订单状态同步", inner_text)
        #: ⛔ 两条指引不是同一句（合并即红）
        self.assertNotEqual(outer_text, inner_text)
        self.assertNotEqual(outer_action, inner_action)
        self.assertNotIn("没有权限", outer_text)
        self.assertNotIn(OUTER_GATE_MSG, inner_text)

    def test_one_endpoint_without_permission_does_not_stop_the_others(self):
        """⭐ 内层权限是**逐项**的（§20.1）⇒ 一项没权限**不挡**其它项（一次点清缺哪几项）。
        ⛔ 弄红它：把 `GetGames` 的失败也当成「失败即停」⇒ `GetOrderStatus` 那一行变 `probed=False`，
        下面两条断言就红。"""
        self.happy_upstream()
        self.upstream.reply(GAMES_PATH, payload={"code": 0, "msg": "没有权限", "data": None})
        report = self.probe()
        rows = self.rows(report)
        self.assertFalse(report.ok)
        self.assertEqual(rows["GetGames"].kind, "permission")
        self.assertTrue(rows["GetOrderStatus"].probed)
        self.assertTrue(rows["GetOrderStatus"].ok)
        #: ⭐ 上一步没给出游戏行 ⇒ `GetProject` **不发那一发**（省着打），且说清为什么
        self.assertFalse(rows["GetProject"].probed)
        self.assertIn("gameId", rows["GetProject"].reason)
        self.assertEqual(
            [row for row in self.upstream.requests if row["path"] == PROJECT_PATH], []
        )

    def test_a_nonexistent_order_counts_as_reachable_with_their_wording(self):
        """⭐ spec §9.2：带**一个不存在的单号**探 ⇒ 「只要有响应、不是权限错就算通」。
        ⛔ 弄红它：把这一档也算成不可用（`probe_path` 去掉 `order_id_probe`）⇒ 本用例红；⚠️ 判据两条：行是「可用」，
        而且把对方原话如实带上（⛔ 不许静默成一个绿勾）。"""
        self.happy_upstream()
        for path in (ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH):
            self.upstream.reply(path, payload={"code": 0, "msg": NO_SUCH_ORDER_MSG, "data": None})
        report = self.probe()
        rows = self.rows(report)
        self.assertTrue(report.ok, report.messages)
        for step in ("GetOrderStatus", "GetOrderProcessImgs"):
            with self.subTest(step=step):
                self.assertTrue(rows[step].ok)
                self.assertEqual(rows[step].kind, "")
                self.assertIn("接口可达", rows[step].reason)
                self.assertIn(NO_SUCH_ORDER_MSG, rows[step].reason)

    def test_a_network_failure_on_an_endpoint_is_the_third_kind_and_says_we_are_broken(self):
        """③ 档（我们这边 / 网络）：⛔ **绝不许**说成「你没有权限」（spec §9.1-C）。
        ⛔ 弄红它：把网络异常也映射成「没权限」⇒ 下面两条断言都红。"""
        self.happy_upstream()
        self.upstream.boom(GAMES_PATH, httpx.ConnectError("合成连接失败"))
        report = self.probe()
        rows = self.rows(report)
        self.assertEqual(rows["GetGames"].kind, "other")
        self.assertIn("我们这边", rows["GetGames"].message)
        self.assertNotIn("权限", rows["GetGames"].message)

    def test_the_probe_never_touches_a_write_endpoint(self):
        """⛔ **一个写接口都不打**（spec §11.4 / §20.2）：行为判据 + 请求形状判据。
        ⚠️ 页面上点一次「测试连接」就打一次写接口 = 给用户一个「点一下就可能改线上单」的按钮 ⇒ 这条判据必须打在
        真实出口（真的发出去的请求）上，⛔ 不是打在注释里。"""
        self.happy_upstream()
        self.probe()
        allowed = {
            TOKEN_PATH, GAMES_PATH, PROJECT_PATH, ORDER_STATUS_PATH, ORDER_PROCESS_IMGS_PATH,
        }
        self.assertEqual({row["path"] for row in self.upstream.requests}, allowed)
        for row in self.upstream.api_calls():
            with self.subTest(path=row["path"]):
                #: 只读端点一律 GET，而且**没有请求体**（写接口才带 body）
                self.assertEqual(row["method"], "GET")
                self.assertEqual(row["form"], {})
        #: token 那一发是唯一的 POST（那是取 token，不是写业务数据）
        self.assertEqual(
            {row["method"] for row in self.upstream.token_calls()}, {"POST"}
        )

    def test_the_table_carries_neither_the_secret_nor_the_token(self):
        """⛔ 表里一个字都不许有凭据（它要回给页面）。正向哨兵：表里**有**内容。"""
        self.happy_upstream()
        self.upstream.reply(GAMES_PATH, payload={"code": 0, "msg": "没有权限", "data": None})
        report = self.probe()
        raw = json.dumps(report.as_data(), ensure_ascii=False)
        self.assertIn("没有权限", raw, "正向哨兵：表里什么都没有时，下面两条否定断言没有判别力")
        self.assertNotIn(SENTINEL_SECRET, raw)
        self.assertNotIn(SENTINEL_TOKEN, raw)
        self.assertNotIn("client_secret", raw)

    def test_the_probe_judges_the_envelope_not_the_data_shape(self):
        """⭐ 探测判的是「这个接口能不能用」（**信封层**），⛔ 不判 `data` 长什么样。
        ⚠️ 构造：`GetOrderStatus` 回一个成功信封但 `data` 里没有 `status`（`client.get_order_status` 会为它抛 ③）⇒ 探测那一趟必须仍然报「可用」——它问的不是「这单的数据对不对」；⛔ 弄红它：把形状判据塞进探测里（拿 `get_order_status` 去探）⇒ 本用例红。
        ⚠️ 唯一的例外是 `GetGames`：它的形状判据留着（见下面第二条），因为它的返回值我们要用（`gameId` 交给 `GetProject`）⇒ 形状不对 = 这一项用不了。"""
        self.happy_upstream()
        self.upstream.reply(ORDER_STATUS_PATH, payload={"code": 1, "msg": None,
                                                       "data": {"no_status": True}})
        rows = self.rows(self.probe())
        self.assertTrue(rows["GetOrderStatus"].ok, rows["GetOrderStatus"].reason)
        self.assertTrue(rows["GetOrderProcessImgs"].ok)

    def test_get_games_shape_is_judged_because_we_consume_its_value(self):
        """`GetGames` 那一项**连形状一起判**（本趟唯一的例外，判据的理由写在实现里）。
        ⚠️ 形状不对 ⇒ 那一行不可用（③ 档，原因点名形状），并且不发 `GetProject` 那一发
        （拿不到 `gameId`，打了只会 404）。"""
        self.upstream.reply(GAMES_PATH, payload={"code": 1, "msg": None, "data": {"nope": 1}})
        report = self.probe()
        rows = self.rows(report)
        self.assertEqual(rows["GetGames"].kind, "other")
        self.assertIn("列表", rows["GetGames"].reason)
        self.assertFalse(rows["GetProject"].probed)
        self.assertEqual(
            [row for row in self.upstream.requests if row["path"] == PROJECT_PATH], []
        )
