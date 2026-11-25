"""`docs/OPEN-API.md` 的**字面面值** + **示例代码真跑**的判据（设计文档第 9.9 / 11.2 节）。

## 这个文件为什么存在

`docs/OPEN-API.md` 是**给第三方的对外契约**（第一行就写着读者是 AI 编程工具），
而本线**没有引入自动生成接口文档的框架** ⇒ 代价是：手写文档**会漂移**。
⇒ 缓解办法就是本文件：把文档里那几处**关键面值**与**示例代码**做成判据，
让漂移在 CI 里当场红。

## 判据分三层（各层证明什么、⛔ 不证明什么）

1. **字面面值**：文档里必须**逐字包含**对外承诺的路径 / 契约键集 / 错误码 / 那几条禁令句 /
   回调契约的字段与算法。⚠️ 它只证明"这些字还在文档里"，
   ⛔ **不证明**文档的散文与实现一致（那一半由 `tests/test_openapi_*.py` 的行为用例管）。
2. ⭐ **示例代码真的跑**：抽出文档 §9.2 的 Python 段，在本进程 `exec`（并把它的
   `__main__` 自检输出抓下来）、抽出 §9.4 的 Node 段交给 `node -e` 真跑，
   断言两者都算出文档 §9.1 那个期望签名；⚠️ **PHP 段跑不了**（本机与 CI 都没有解释器，
   实测 `which php` 无输出）⇒ 对它只做**字面**判据，并在文档里**明说跑不了**。
3. ⭐ **与实现对拍**：文档的示例算法算出来的签名，必须与**生产实现**
   （`apps.openapi.outbound.compute_signature`）算出来的**逐字相同**，
   并且文档声明的 `expires_in` 必须等于 `apps.openapi.tokens.TOKEN_TTL_SECONDS`。
   ⇒ 这一层挡的是"文档与实现各说一套"（最难替代、也最容易漂的一层）。

## ⚠️ 关于"桩"（第 2 层里换令牌那一段怎么跑）

换令牌与调业务端点都需要**真实网络**，而测试不能联网 ⇒ 文档 §9.2 的三个联网函数都留了一个
`opener` 形参（默认走真实网络）。本文件注入的是**测试内的桩**（`_Recorder` + `_StubResponse`），
**⛔ 不是真实网络、也⛔ 不是真实租户**：桩只证明"文档里那段代码的形状与请求发法是对的"
（URL / 方法 / `Content-Type` / `Authorization` / `Idempotency-Key` / 两键 vs 三键），
⛔ **不证明**服务端真的这样答复（那是 `tests/test_openapi_token.py` 的事）。

## 为什么用 `SimpleTestCase`、为什么文档路径按仓库根定位

- 本文件**一个夹具都不建、不进数据库**（只读 markdown + 跑纯函数；`outbound` 是纯函数模块）
  ⇒ 用 `SimpleTestCase`，这条判据不会因为环境或迁移坏掉而变成"红得看不懂"。
- 文档路径取 `Path(__file__).resolve().parents[2]`（= **仓库根**，与
  `src/tests/test_doc_index_tables.py` 同一写法）⇒ 从 `src/` 下跑测试也定位得到，
  ⛔ 不写相对当前工作目录的路径。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import urllib.error
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from apps.openapi import authentication, outbound
#: ⭐ `ranks/` 那句「尚未开放」（对外契约的一部分）——从**实现常量**取，
#: 这样"改了实现文案但没改文档"会红（与 `EXPECTED_FAILURE_MESSAGES` 同一手法）。
from apps.openapi.api.order_extra_views import RANKS_NOT_AVAILABLE_MSG
#: ⭐ 令牌有效期的**唯一**出处 ⇒ 文档里那个 `7200` 必须等于它（第 3 层对拍）。
from apps.openapi.tokens import TOKEN_TTL_SECONDS

#: 本文件在 `src/tests/` 下 ⇒ `parents[1]` 是 `src/`、`parents[2]` 是**仓库根**。
REPO_ROOT = Path(__file__).resolve().parents[2]
OPEN_API_MD = REPO_ROOT / "docs" / "OPEN-API.md"

# ═════════════════════════════════════════════════════════════════════════════
# 合成夹具（与文档 §9.1 逐字相同；⛔ 不是真实密钥、⛔ 不是真实订单）
# ═════════════════════════════════════════════════════════════════════════════

FIXTURE_SECRET = "tt-open-api-callback-test-secret-0001"
FIXTURE_TIMESTAMP = 1760000000
FIXTURE_DELIVERY_ID = "6f1c0f7c9a2e4f0b8a3d5c7e9f1a2b3c"
FIXTURE_BODY = {
    "delivery_id": FIXTURE_DELIVERY_ID,
    "event": "order.status_changed",
    "note": "",
    "occurred_at": "2026-10-08T12:00:00+08:00",
    "order_no": "20261008120000000042",
    "source_order_no": "AB-2026-0007",
    "status": "IN_PROGRESS",
    "timestamp": FIXTURE_TIMESTAMP,
}

#: 规范化报文的 `sha256` 小写 hex（文档 §9.1 那个字面量）。
EXPECTED_CANONICAL = (
    '{"delivery_id":"6f1c0f7c9a2e4f0b8a3d5c7e9f1a2b3c","event":"order.status_changed",'
    '"note":"","occurred_at":"2026-10-08T12:00:00+08:00","order_no":"20261008120000000042",'
    '"source_order_no":"AB-2026-0007","status":"IN_PROGRESS","timestamp":1760000000}'
)
EXPECTED_SHA256 = "d5fe626b9b0d75eaef9823e1ff2dfda53b8c97f5fd03a6344517e41278b06844"
#: ⭐ 期望签名（文档 §9.1；三份示例代码必须都算出它）。
EXPECTED_SIGNATURE = "b44b31b4a46978079edf28e78ba06f2e30fa7fad6d02d560c059befdf910286f"

# ═════════════════════════════════════════════════════════════════════════════
# 文档里必须逐字出现的东西（对外承诺面）
# ═════════════════════════════════════════════════════════════════════════════

#: 查单载荷的**逐字段白名单**（键名，按"调用方能在 `data` 里看到的键"逐个列出）。
#: ⚠️ 它**故意写死在这里**（⛔ 不从序列化器 / 常量模块 import）——那样"改实现 + 改常量"
#: 一起改就不红。判据打在**文档里是否出现这个键名本身**上（按词切分后精确比对，⛔ 不是子串包含）。
EXPECTED_DATA_KEYS = (
    # 顶层（`taker_name` 是员工身份口径的唯一显式例外）
    "order_no", "source_order_no", "status", "status_text", "game", "project",
    "total_price", "created_at", "updated_at", "source",
    "amounts", "costs", "taker_name", "content", "order", "player", "customer",
    # amounts（订单侧金额 6 个）
    "received_price", "platform_price", "hall_price", "refund_fee", "fee",
    # costs（两个键都是列表；每格四个键）
    "main", "sales", "temp_amount", "bonus_amount", "taker_price", "commission_rate",
    # content（客户写的）
    "demand", "time_requirement",
    # order（业务描述）
    "order_type", "server", "category", "project_text", "is_urgent", "tag",
    "completed_at", "payment_method", "payment_status",
    # player
    "game_account", "game_sub_account", "game_role_name", "current_info_1", "current_info_2",
    # customer
    "customer_phone", "customer_qq", "customer_ww",
)

#: 认证层**今天仍在服役**的失败文案——**从常量取**，这样"改了实现文案但没改文档"会红。
EXPECTED_FAILURE_MESSAGES = (
    authentication.IP_NOT_ALLOWED_MSG,
    authentication.BEARER_INVALID_CREDENTIALS_MSG,
    authentication.BEARER_EXPIRED_MSG,
)

#: 本 API **对外承诺**的错误码（含取令牌端点那两档 OAuth2 错误的 HTTP 码）。
EXPECTED_CODES = (
    "0",
    "401",
    "1000",
    "1001",
    "1002",
    "1014",
    "1022",
    "1023",
    "403",
    "429",
    "1024",
)

#: OAuth2 那一侧的三个 `error` 取值（取令牌端点专用，§1.1）。
EXPECTED_OAUTH2_ERRORS = ("invalid_client", "unsupported_grant_type", "temporarily_unavailable")

#: 回调推送报文的**逐字段**键名（§5.2）。
EXPECTED_CALLBACK_KEYS = (
    "event", "delivery_id", "order_no", "source_order_no",
    "status", "note", "images", "occurred_at", "timestamp", "sign",
)

#: 回调推送的三个请求头（**出站**那一族；§5.3）。
EXPECTED_CALLBACK_HEADERS = ("X-TT-Timestamp", "X-TT-Delivery", "X-TT-Signature")

#: 三种事件名（§5.1）。
EXPECTED_EVENTS = ("order.status_changed", "order.deleted", "order.image_added")

#: ⛔⭐ **已废除的主词**（v1 面）：它们**不许**再出现在这份对外契约里。
#: ⚠️ 判据必须搜**主词本身**（`AGENTS.md` §5-C：不能只搜它派生出的短语，否则"Expected: 空"
#: 给出的是**假保证**）。逐条：入站签名头 / 待签串 / 那几句已删除的 401 文案 /
#: 应用自己的授权范围（节点与游戏范围）/ 那两条已删除的配置 `PUT`。
ABOLISHED_TOKENS = (
    "stringToSign",
    "缺少签名参数",
    "签名已过期",
    "签名校验失败",
    "permission_codes",
    "game_ids",
    "all_games",
    "该应用尚未设置游戏范围",
    "order.view_detail",
    "/api/open/apps/<pk>/permissions/",
    "/api/open/apps/<pk>/games/",
    "## 2. 认证协议",
    "## 3. 可复制的签名代码",
)

#: `X-TT-Sign`（入站那个已删除的头）——⚠️ **不能用子串判**：`X-TT-Signature` 是它的前缀
#: （出站那个头**今天仍然在服役**）⇒ 用"后面不跟 `ature`"的边界判据。
ABOLISHED_HEADER_RE = re.compile(r"X-TT-Sign(?!ature)")

#: 文档里**全部** 64 位小写 hex 必须**恰好**是这两个（规范化摘要 + 期望签名）。
#: ⚠️ 这是"恰好"判据：文档里冒出第三个 64-hex（例如手抄错一个夹具值）会红。
HEX64_RE = re.compile(r"\b[0-9a-f]{64}\b")

#: 文档声明的 `expires_in` 字面量（令牌有效期）。
EXPIRES_IN_RE = re.compile(r'"expires_in":\s*(\d+)')

_FENCE_RE = re.compile(r"^```(\w*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _read_doc() -> str:
    """读文档原文（文件必须存在 —— 缺了它就是本线最严重的漂移）。"""
    return OPEN_API_MD.read_text(encoding="utf-8")


def _tokens(text: str) -> set:
    """把 markdown 切成词元集合（用于"某个键名有没有出现在文档里"的判据）。

    ⚠️ 为什么按**词元**而不是子串：`content` 是 `time_requirement` 的子串、
    `order` 是 `order_no` 的子串 ⇒ 子串判据会在"只写了 `order_no`、漏了 `order`"时**恒真**。
    ⚠️ 这里**只归一化文档那一侧**（大小写 / 切词）；被比对的期望值本身是**精确字面量**，
    不做任何改写 ⇒ ⛔ 不存在"两侧归一化不一致"那类恒真（同族 E-0083）。
    """
    return {token.lower() for token in _TOKEN_RE.findall(text)}


def _code_blocks(text: str):
    """取出全部带语言标注的围栏代码块，返回 `{语言: [代码, ...]}`。

    ⚠️ 每个语言只允许**恰好一段**（`_single_block` 会断言）——多段会让"抽出哪一段"变成
    隐式约定，而文档加一段示例就可能让判据悄悄换了目标。
    """
    blocks: dict = {}
    for language, body in _FENCE_RE.findall(text):
        if language:
            blocks.setdefault(language.lower(), []).append(body)
    return blocks


def _single_block(blocks: dict, language: str) -> str:
    candidates = blocks.get(language, [])
    assert len(candidates) == 1, (
        f"文档里 ```{language} 代码块应恰好 1 段（用来抽示例代码），实际 {len(candidates)} 段"
    )
    return candidates[0]


def _fenced_literal(text: str, marker: str) -> str:
    """取"`marker` 之后第一个围栏块"的内容（文档里的字面夹具值）。

    ⚠️ `marker` 必须**唯一指向**那一块（同一个标记出现两次会取到前一处之后的块 ⇒ 判据打到
    另一个夹具上）。⇒ 这里**显式**断言它只出现一次（⛔ 不靠"我写的时候记得"）。
    ⚠️ 找不到就**响亮**失败（⛔ 不返回空串：空串会让后面的相等判据变成"文档缺了反而绿"）。
    """
    occurrences = text.count(marker)
    assert occurrences == 1, f"夹具标记应恰好出现 1 次，实际 {occurrences} 次：{marker}"
    start = text.find(marker)
    assert start != -1, f"文档里找不到这个夹具标记：{marker}"
    lines = text[start:].splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("```"):
            continue
        body: list = []
        for candidate in lines[index + 1 :]:
            if candidate.strip() == "```":
                return "\n".join(body).strip()
            body.append(candidate)
        break
    raise AssertionError(f"标记之后没有闭合的围栏块：{marker}")


def _body_without_quotes(text: str) -> str:
    """去掉引用块（行首 `>`）。

    ⚠️ 为什么要这一步：文档**文首那段"读者是 AI"的说明本身**就要写出"没有营销话术、没有
    欢迎页那套"这类字样 —— 那是**元说明**，不是营销文案。若不排除引用块，两条判据会互相打架
    （"必须说明自己不写营销话术" 与 "不许出现那些词"），而人会为了让它变绿去删掉正当的说明。
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


# ═════════════════════════════════════════════════════════════════════════════
# 跑文档 §9.2 那段 Python：真 exec，并把它 `__main__` 自检的输出抓下来
# ═════════════════════════════════════════════════════════════════════════════


_HAS_OPEN_API_DOC = (Path(__file__).resolve().parents[2] / "docs" / "OPEN-API.md").is_file()

class _StubResponse:
    """桩响应（`_Recorder` 交给文档代码的那个"响应对象"）。

    ⚠️ 它**不是**真实网络响应：只实现文档代码用到的那三个成员
    （`read()` / 上下文管理器协议）。⛔ 别据此说"端点行为被覆盖了"。
    """

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _Recorder:
    """可注入的 `opener` 桩：把被接住的请求记下来，按顺序吐出预置的响应 / 异常。

    ⚠️ `requests` 就是**"桩真的接上了"的判据**（E-0029：桩接没接上本身要有判据）——
    用例逐条断言它的长度、方法与头，⛔ 不是只看"函数没抛异常"。
    """

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list = []

    def __call__(self, request):
        self.requests.append(request)
        assert self._responses, "桩被调用的次数超出预期（文档代码多发了一次请求）"
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _StubResponse(item)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class DocPythonSnippetTestCase(SimpleTestCase):
    """抽出文档 §9.2 的 Python 段并 `exec`（含它的 `__main__` 自检）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = _single_block(_code_blocks(_read_doc()), "python")
        # ⚠️ `__name__` 必须显式给：`exec` **不会**自动注入它（不给的话走到
        # `if __name__ == "__main__":` 会 NameError）。给 `"__main__"` 是**有意的**：
        # 于是那一段离线自检真的会跑，它的输出就是下面几条判据的读数。
        cls.namespace: dict = {"__name__": "__main__"}
        cls.stdout = io.StringIO()
        with contextlib.redirect_stdout(cls.stdout):
            exec(compile(cls.source, f"{OPEN_API_MD} §9.2", "exec"), cls.namespace)
        cls.main_output = cls.stdout.getvalue().strip()


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class ExecutableSnippetTests(DocPythonSnippetTestCase):
    """⭐ 第二层：文档里的 Python 示例**真的跑**，并与 §9.1 的字面值对拍。"""

    def test_the_python_snippet_is_not_pseudocode(self):
        """结构判据：它得是一段能独立跑的脚本（⛔ 不是"示意一下"的伪代码）。"""
        for required in ("import hashlib", "import hmac", "import json", "def fetch_token",
                         "def call_api", "def verify_callback", "def compute_signature"):
            self.assertIn(required, self.source)
        self.assertGreaterEqual(len(self.source.splitlines()), 80, "示例太短，不像可直接复制的一整段")
        self.assertNotIn("TODO", self.source)

    def test_the_snippet_defines_the_four_entry_points(self):
        for name in ("fetch_token", "call_api", "canonical_json", "compute_signature",
                     "verify_callback"):
            with self.subTest(name=name):
                self.assertTrue(callable(self.namespace[name]), f"示例里没有可调用的 {name}")

    def test_the_main_self_check_prints_the_expected_signature(self):
        """⭐ 直接跑出来的读数：文档自己那段 `__main__` 打印的必须是 §9.1 那个 hex。

        ⚠️ 这条同时是**待签串顺序**的判据：把示例里 `f"{timestamp}\\n{delivery_id}\\n…"`
        的顺序换一下，这里打印出来的就是另一个值 ⇒ 当场红。
        """
        self.assertEqual(self.main_output, EXPECTED_SIGNATURE)

    def test_the_doc_literals_are_exactly_the_two_hex_values(self):
        """文档里全部 64 位 hex 必须**恰好**是（规范化摘要、期望签名）这两个。"""
        self.assertEqual(set(HEX64_RE.findall(_read_doc())), {EXPECTED_SHA256, EXPECTED_SIGNATURE})

    def test_the_doc_fixture_table_matches_this_file(self):
        text = _read_doc()
        for literal in (FIXTURE_SECRET, str(FIXTURE_TIMESTAMP), FIXTURE_DELIVERY_ID):
            with self.subTest(literal=literal):
                self.assertIn(literal, text)
        raw = _fenced_literal(text, "`body_without_sign`（")
        self.assertEqual(json.loads(raw), FIXTURE_BODY)
        # ⚠️ 那一块**故意不是**规范化形态（键序乱、带空白）——否则"规范化"这一步就没意义了。
        self.assertNotEqual(raw, EXPECTED_CANONICAL)
        self.assertEqual(_fenced_literal(text, "规范化之后的字节（`ensure_ascii"), EXPECTED_CANONICAL)
        self.assertEqual(_fenced_literal(text, "`sha256(规范化).hexdigest()`"), EXPECTED_SHA256)
        self.assertEqual(_fenced_literal(text, "**期望签名**（`sign`"), EXPECTED_SIGNATURE)

    def test_the_snippet_computes_the_documented_canonical_form_and_digest(self):
        canonical = self.namespace["canonical_json"](FIXTURE_BODY)
        self.assertEqual(canonical.decode("utf-8"), EXPECTED_CANONICAL)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), EXPECTED_SHA256)

    def test_the_snippet_and_the_production_implementation_agree(self):
        """⭐ 第三层：文档的示例算法 vs **生产实现**（`apps.openapi.outbound`）。

        ⚠️ 两者算出来必须**逐字相同** —— 这一层挡的是"文档与实现各说一套"。
        """
        from_doc = self.namespace["compute_signature"](
            FIXTURE_SECRET, FIXTURE_TIMESTAMP, FIXTURE_DELIVERY_ID, FIXTURE_BODY
        )
        from_prod = outbound.compute_signature(
            secret=FIXTURE_SECRET,
            timestamp=FIXTURE_TIMESTAMP,
            delivery_id=FIXTURE_DELIVERY_ID,
            body_without_sign=FIXTURE_BODY,
        )
        from_prod_canonical = outbound.canonical_json(FIXTURE_BODY).decode("utf-8")
        self.assertEqual(from_doc, EXPECTED_SIGNATURE)
        self.assertEqual(from_prod, EXPECTED_SIGNATURE)
        self.assertEqual(from_prod_canonical, EXPECTED_CANONICAL)

    def test_the_injected_opener_is_really_used(self):
        """⭐ 对照（E-0029）：注入的桩**真的被用了**（⛔ 不是"参数被忽略、悄悄走真网络"）。"""
        sentinel = object()
        self.assertIs(self.namespace["_open"](object(), opener=lambda request: sentinel), sentinel)

    def test_driving_token_then_a_query_with_a_stub(self):
        """⭐ 文档那段代码真的驱动一遍：**换令牌 → 用令牌调一个查单**。

        ⚠️ 这里的 `_Recorder` 是**测试内的桩**（⛔ 不是真网络、⛔ 不是真租户）：
        它只证明"文档里那段代码发出的请求形状对"。
        """
        token_payload = {
            "access_token": "tok-from-stub",
            "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SECONDS,
        }
        envelope = {"code": 0, "msg": "", "data": {"count": 0, "results": []}}
        token_recorder = _Recorder(json.dumps(token_payload).encode("utf-8"))
        api_recorder = _Recorder(json.dumps(envelope).encode("utf-8"))

        token = self.namespace["fetch_token"](
            "https://demo.example.test", "cid-synthetic", "secret-synthetic",
            opener=token_recorder,
        )
        # ① 换令牌那一次请求的形状（桩记下来的**真实** request 对象）
        self.assertEqual(len(token_recorder.requests), 1)
        token_request = token_recorder.requests[0]
        self.assertEqual(token_request.method, "POST")
        self.assertTrue(token_request.full_url.endswith("/api/open/oauth/token"))
        self.assertEqual(
            token_request.get_header("Content-type"), "application/x-www-form-urlencoded"
        )
        self.assertEqual(
            token_request.data.decode("utf-8"),
            "grant_type=client_credentials&client_id=cid-synthetic"
            "&client_secret=secret-synthetic",
        )
        self.assertEqual(token, token_payload)

        # ② 带令牌调一个读端点（真实存在的那一条：`GET /api/open/v1/games/`）
        data = self.namespace["call_api"](
            "https://demo.example.test", token["access_token"], "/api/open/v1/games/",
            opener=api_recorder,
        )
        self.assertEqual(data, envelope["data"])
        self.assertEqual(len(api_recorder.requests), 1)
        self.assertEqual(
            api_recorder.requests[0].get_header("Authorization"), "Bearer tok-from-stub"
        )
        self.assertIsNone(api_recorder.requests[0].get_header("Idempotency-key"))

        # ③ 写端点还要带 `Idempotency-Key`（§2.2 的硬前置）
        write_recorder = _Recorder(json.dumps({"code": 0, "msg": "", "data": {}}).encode("utf-8"))
        self.namespace["call_api"](
            "https://demo.example.test", token["access_token"], "/api/open/v1/orders/",
            method="POST", body={"game": 1}, idempotency_key="idem-key-0001",
            opener=write_recorder,
        )
        self.assertEqual(
            write_recorder.requests[0].get_header("Idempotency-key"), "idem-key-0001"
        )
        self.assertEqual(write_recorder.requests[0].get_header("Content-type"), "application/json")

    def test_the_business_failure_path_reads_the_code(self):
        """业务失败是 HTTP 200 + `code != 0` ⇒ 文档那段 `call_api()` 必须**抛**（⛔ 不静默）。"""
        envelope = {"code": 1000, "msg": "缺少幂等键", "data": None}
        recorder = _Recorder(json.dumps(envelope).encode("utf-8"))
        with self.assertRaises(RuntimeError) as ctx:
            self.namespace["call_api"](
                "https://demo.example.test", "tok", "/api/open/v1/orders/",
                method="POST", body={}, opener=recorder,
            )
        self.assertIn("1000", str(ctx.exception))

    def test_the_token_failure_path_reads_the_two_oauth2_keys(self):
        """⭐ 换令牌失败是**裸两键**（⛔ 不是信封）⇒ 文档那段必须按这两键解析。"""
        payload = json.dumps(
            {"error": "invalid_client",
             "error_description": authentication.BEARER_INVALID_CREDENTIALS_MSG}
        ).encode("utf-8")
        failure = urllib.error.HTTPError(
            "https://demo.example.test/api/open/oauth/token", 401, "Unauthorized",
            {"Content-Type": "application/json"}, io.BytesIO(payload),
        )
        recorder = _Recorder(failure)
        with self.assertRaises(self.namespace["OAuth2Error"]) as ctx:
            self.namespace["fetch_token"](
                "https://demo.example.test", "cid", "wrong", opener=recorder
            )
        self.assertEqual(ctx.exception.error, "invalid_client")
        self.assertEqual(
            ctx.exception.error_description, authentication.BEARER_INVALID_CREDENTIALS_MSG
        )

    def test_verify_callback_accepts_the_fixture_and_rejects_a_tampered_body(self):
        """⭐ 验签那段真跑：给定报文与密钥算出期望 hex，且**篡改一个字节就不过**。"""
        signed = {**FIXTURE_BODY, "sign": EXPECTED_SIGNATURE}
        raw_body = self.namespace["canonical_json"](signed)
        headers = {
            # ⚠️ 头里是**字符串**（HTTP 头本来就是文本）；示例必须能吃掉这一形态。
            "X-TT-Timestamp": str(FIXTURE_TIMESTAMP),
            "X-TT-Delivery": FIXTURE_DELIVERY_ID,
            "X-TT-Signature": EXPECTED_SIGNATURE,
        }
        self.assertTrue(
            self.namespace["verify_callback"](FIXTURE_SECRET, headers, raw_body)
        )
        tampered = self.namespace["canonical_json"]({**signed, "status": "COMPLETED"})
        self.assertFalse(
            self.namespace["verify_callback"](FIXTURE_SECRET, headers, tampered)
        )
        wrong_key = self.namespace["verify_callback"]("another-secret", headers, raw_body)
        self.assertFalse(wrong_key)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class NodeSnippetTests(SimpleTestCase):
    """⭐ 抽出文档 §9.4 的 Node 段，交给 `node` **真跑**，断言它算出期望签名。

    ⚠️ 跳过条件写成 `node` 不在 PATH 时 skip ⇒ 这条判据在没装 Node 的环境里**不产出结论**
    （⛔ 不是通过）。本仓前端门禁本来就要求 Node，所以正常环境里它**总是真跑**。
    """

    def test_the_node_snippet_reproduces_the_expected_signature(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("本机没有 node ⇒ 这条判据没有读数（⛔ 不是通过）")
        source = _single_block(_code_blocks(_read_doc()), "javascript")
        completed = subprocess.run(
            [node, "-e", source], capture_output=True, text=True, timeout=60, check=False
        )
        self.assertEqual(completed.returncode, 0, f"node 跑失败：{completed.stderr[-2000:]}")
        printed = completed.stdout.strip().splitlines()[-1].strip()
        self.assertEqual(printed, EXPECTED_SIGNATURE)

    def test_the_node_snippet_does_not_use_form_encoders(self):
        """⚠️ `querystring` / `URLSearchParams` 是 form-urlencoded（与本契约无关）⇒ 不许出现在段里。"""
        source = _single_block(_code_blocks(_read_doc()), "javascript")
        self.assertNotIn("querystring", source)
        self.assertNotIn("URLSearchParams", source)
        self.assertIn("createHmac", source)
        self.assertIn("timingSafeEqual", source)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class PhpSnippetTests(SimpleTestCase):
    """⚠️ PHP 那份**跑不了**（本机与 CI 都没有解释器，实测 `which php` 无输出）⇒ **弱判据**。

    ⚠️ 如实登记：这几条只证明"文档里那段 PHP 的字面形状对不对"，
    ⛔ **不证明**它能算出期望签名 —— 那要有人在一台有 PHP 的机器上跑一遍。
    文档里也**明说**了这一点（`PhpSnippetTests` 的下一条用例会钉住那句声明）。
    """

    def test_the_php_snippet_is_present_and_pinned_by_literals(self):
        source = _single_block(_code_blocks(_read_doc()), "php")
        self.assertIn(EXPECTED_SIGNATURE, source)
        for header in EXPECTED_CALLBACK_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, source)
        for required in ("hash_hmac", "hash_equals", "hash('sha256'", "json_encode"):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_the_php_snippet_pins_the_signed_string_order(self):
        """⚠️ 待签串的三步顺序在 PHP 段里也要能逐字看出来（⛔ 不是"看起来差不多"）。"""
        source = _single_block(_code_blocks(_read_doc()), "php")
        self.assertIn(
            '$message = $timestamp . "\\n" . $deliveryId . "\\n" . $digest;', source
        )
        self.assertIn("JSON_UNESCAPED_UNICODE", source)

    def test_php_absence_is_declared_in_the_document(self):
        """⭐ 「这一段没跑过」必须**写在文档里**（⛔ 不能只在测试的注释里）。"""
        self.assertIn(
            "### 9.3 PHP（⚠️ 本机与 CI 都没有解释器 ⇒ **未跑过**，只作字面示例）",
            _read_doc(),
        )

    def test_php_absence_is_declared_in_this_file(self):
        """对照：本文件自己也写明"PHP 段跑不了"（否则下一个人会以为它跑过）。"""
        self.assertIn("没有解释器", __doc__ or "")


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class DocExistenceTests(SimpleTestCase):
    """文档存在且不含"人类教程 / 营销"形态（业务方 Q5 的形态要求）。"""

    def test_the_document_exists(self):
        self.assertTrue(OPEN_API_MD.is_file(), f"缺文件：{OPEN_API_MD}")

    def test_it_is_not_a_human_tutorial(self):
        """⛔ 业务方原话是「给一份喂给 ai 的文档即可」⇒ 不许写成欢迎页 / 申请向导那套。

        ⚠️ 判据是**正向的营销词**（正文里出现即红），⛔ 不用"必须出现某个技术词"那类恒真写法；
        ⚠️ 只扫**正文**（引用块是文首的元说明，见 `_body_without_quotes`）。
        """
        text = _body_without_quotes(_read_doc())
        for forbidden in ("欢迎使用", "点击这里申请", "立即注册", "限时优惠"):
            self.assertFalse(forbidden in text, f"正文里出现了营销 / 教程用语：{forbidden}")

    def test_it_is_a_technical_contract_not_a_tutorial(self):
        """对照：这份文档**确实是**技术契约（否则上一条"没有营销词"可由"文档是空的"平凡成立）。"""
        text = _read_doc()
        self.assertIn("Authorization: Bearer <令牌>", text)
        self.assertIn("业务失败也是 HTTP 200", text)

    def test_internal_section_references_all_resolve(self):
        """⭐ **文档内部的 `§x.y` 引用必须指到真实存在的小节**（⛔ 不留断引用）。

        ⚠️ 为什么要有这一条：这份文档是**手写**的 —— 而手写文档最典型的腐化就是
        "内容改了、章节引用没改"。对第三方来说是"点进去什么都没有"，而**没有任何门禁会报**。
        ⚠️ 写法：先收全文档的小节号（`## 3` / `### 3.3` 这种标题），再收全部 `§数字[.数字]` 引用，
        引用必须落在标题集合里。
        """
        text = _read_doc()
        headings = set(re.findall(r"^#{2,4}\s+(\d+(?:\.\d+)*)", text, re.MULTILINE))
        self.assertGreater(len(headings), 20, "小节号没抽到（抽取面坏了），这条判据不产出结论")
        referenced = set(re.findall(r"§(\d+(?:\.\d+)*)", text))
        broken = sorted(referenced - headings)
        self.assertEqual(broken, [], f"这些章节引用在文档里不存在：{broken}")


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class BreakingChangeDocTests(SimpleTestCase):
    """⭐ 这份契约**必须自己说清 v1 面已经删了**，并且**不许**把它当现行用法留着。

    ## 为什么值得单列一类

    `docs/OPEN-API.md` 的读者首先是 **AI 编程工具**（文档第一行就这么写）⇒ 留着已失效的
    契约（怎么签名 / 怎么勾权限与游戏范围）会让它**照着调一个不存在的端点、按一套已废弃的
    方式签名**。⇒ 两件事都要钉：① 破坏性变更那段话**在场**；
    ② 那些**已废除的主词本身**⛔ **不许**再出现（`AGENTS.md` §5-C：判据要搜主词，
    不能只搜派生短语，否则"Expected: 空"是**假保证**）。
    """

    def setUp(self):
        self.text = _read_doc()

    def test_the_document_carries_a_breaking_change_notice_near_the_top(self):
        """读者可能只读前 20 行 ⇒ 那段话必须在**开头**，并且说清删了什么、今天认什么。"""
        head = self.text[:4000]
        self.assertIn("### 0.1 破坏性变更（2026-10-08）", head)
        self.assertIn("已整体删除", head)
        self.assertIn("HMAC", head)
        self.assertIn("Bearer", head)

    def test_it_says_the_only_place_that_needs_a_signature_is_verifying_callbacks(self):
        self.assertIn("验证我们推给你的回调", self.text)

    def test_the_abolished_v1_face_is_gone(self):
        """⭐ 已废除的主词**一个都不许**留在文档里（⛔ 不是"留着原文加一段改准说明"）。"""
        for token in ABOLISHED_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, self.text)
        match = ABOLISHED_HEADER_RE.search(self.text)
        self.assertIsNone(match, f"入站签名头（已删除）还在文档里：{match.group(0) if match else ''}")

    def test_the_surviving_outbound_headers_are_still_documented(self):
        """对照：⛔ 别把**出站**那三个头（今天仍在服役）一起删掉（同族 E-0011 的"搜主词"陷阱）。"""
        for header in EXPECTED_CALLBACK_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class EndpointPathTests(SimpleTestCase):
    """对外路径必须**逐字**（含末尾斜杠）在文档里。"""

    def setUp(self):
        self.text = _read_doc()

    def test_token_endpoint_has_no_trailing_slash(self):
        """⭐ 取令牌那条路径**没有**末尾斜杠（写错就 404）⇒ 另一个形态不许出现。"""
        self.assertIn("`POST /api/open/oauth/token`", self.text)
        self.assertNotIn("/api/open/oauth/token/", self.text)

    def test_list_path_with_trailing_slash(self):
        self.assertIn("/api/open/v1/orders/?source_order_no=", self.text)

    def test_detail_path_with_trailing_slash(self):
        self.assertIn("/api/open/v1/orders/<order_no>/", self.text)

    def test_app_management_paths_are_present(self):
        for path in (
            "POST` | `/api/open/apps/",
            "POST` | `/api/open/apps/<pk>/rotate-secret/`",
            "GET` | `/api/open/apps/<pk>/deliveries/`",
            "POST` | `/api/open/apps/<pk>/deliveries/<delivery_pk>/retry/`",
        ):
            self.assertIn(path, self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class V2ReadOnlyEndpointDocTests(SimpleTestCase):
    """只读扩展端点的**路径与键集**必须与 `docs/OPEN-API.md` 同步。

    ⚠️ 与 `V2WriteEndpointDocTests` 同一手法：**字面量**（⛔ 不从实现 import ——
    E-0024「钉值不钉常量」），判据打在"文档里有没有这些键 / 路径"上。
    """

    #: 六条路径（与 `apps/openapi/urls.py` 的 pattern 逐字一致，含尾斜杠）。
    V2_PATHS = (
        "/api/open/v1/orders/<order_no>/status/",
        "/api/open/v1/orders/<order_no>/images/",
        "/api/open/v1/ranks/",
        "/api/open/v1/bills/",
        "/api/open/v1/games/",
        "/api/open/v1/projects/?game_id=",
    )

    #: 四个载荷的键集（**字面量**，⛔ 不从 `order_extra_views` import）。
    STATUS_KEYS = (
        "status", "status_text", "created_at", "claimed_at", "submitted_at", "completed_at",
    )
    IMAGE_KEYS = (
        # ⚠️ 上传人的键是 `uploaded_by_name`（**显示名**，⛔ 不是账号主键）。
        "img_type", "url", "original_name", "size_bytes", "remark", "uploaded_by_name", "created_at",
    )
    BILL_KEYS = (
        "order_no", "emp_type", "temp_amount", "bonus_amount", "taker_price",
        "payable_net", "settle_status", "settled_amount", "settled_at",
    )
    DICT_KEYS = ("id", "name", "is_active", "sort_order", "base_price", "category")

    def setUp(self):
        self.text = _read_doc()

    def test_the_six_v2_paths_are_in_the_document(self):
        for path in self.V2_PATHS:
            self.assertIn(path, self.text, f"文档里缺 v2 端点路径：{path}")

    def test_every_v2_payload_key_is_in_the_document(self):
        tokens = _tokens(self.text)
        missing = sorted(
            key
            for key in (*self.STATUS_KEYS, *self.IMAGE_KEYS, *self.BILL_KEYS, *self.DICT_KEYS)
            if key.lower() not in tokens
        )
        self.assertEqual(missing, [], f"文档里缺这些 v2 载荷键：{missing}")

    def test_the_ranks_not_available_message_is_word_for_word(self):
        """⭐ `ranks/` 那句是**对外契约的一部分** ⇒ 从常量取，改一处两边一起看。"""
        self.assertIn(RANKS_NOT_AVAILABLE_MSG, self.text)
        self.assertIn("1024", self.text)

    def test_it_says_submitted_at_is_a_contract_value_not_a_missing_field(self):
        """⭐ 「本系统没有交单时间」必须写在文档里。

        ⚠️ 判据取**两句**：`submitted_at` 恒为 `null` + "交单时间"这件事本身
        —— 只写键名会让第三方以为"过一阵会有值"。
        """
        self.assertIn("`submitted_at` 恒为 `null`", self.text)
        self.assertIn("交单时间", self.text)

    def test_it_says_the_read_endpoints_use_the_bearer_token(self):
        self.assertIn("Bearer", self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class V2WriteEndpointDocTests(SimpleTestCase):
    """写端点的**路径 / 字段 / 幂等口径**必须与文档同步。"""

    #: **六条**写端点的路径（与 `apps/openapi/urls.py` 的 pattern 逐字一致）。
    WRITE_PATHS = (
        "/api/open/v1/orders/",
        "/api/open/v1/orders/<order_no>/status/",
        "/api/open/v1/orders/<order_no>/assign-price/",
        "/api/open/v1/orders/<order_no>/",
        "/api/open/v1/orders/<order_no>/images/ticket/",
        "/api/open/v1/orders/<order_no>/images/",
    )

    #: 录单的字段表（**逐字**；与 `order_create_views.CREATE_FIELDS` 同名不同源 —— 正是本判据的意义）。
    CREATE_FIELDS = (
        "game", "project_text", "total_price", "source_order_no",
        "server", "game_account", "game_role_name", "content", "remark",
        "hall_price", "assignee", "throw_to_hall",
    )

    #: **改单**可接受字段表（**逐字**；与 `order_write_views._accepted_update_fields()`
    #: 同名不同源 —— 正是本判据的意义）。⚠️ 这 25 个就是"最终对外字段表"。
    UPDATE_FIELDS = (
        "game", "server", "category", "project", "project_text", "order_type",
        "source", "source_order_no", "tag", "is_urgent", "content", "demand",
        "time_requirement", "game_account", "game_sub_account", "game_role_name",
        "current_info_1", "current_info_2", "customer_phone", "customer_qq",
        "customer_ww", "payment_method", "sales_agent", "remark", "order_source",
    )

    #: ⛔ **改单明确不接受**的字段（文档必须点名，否则第三方会以为改得动）。
    UPDATE_REFUSED_FIELDS = (
        "status", "total_price", "received_price", "platform_price", "hall_price",
        "refund_fee", "fee", "sales_user", "payment_status", "order_no",
        "created_at", "updated_at", "created_by", "updated_by",
        "completed_at", "hall_publisher", "deleted_at", "deleted_by",
    )

    def setUp(self):
        self.text = _read_doc()

    def test_the_six_write_paths_are_in_the_document(self):
        for path in self.WRITE_PATHS:
            self.assertIn(path, self.text, f"文档里缺写端点路径：{path}")

    def test_every_update_field_is_in_the_document(self):
        """⭐ 改单的 25 个可接受字段与 18 个明确拒收字段都必须逐字出现在文档里。

        ⚠️ 取**词元**比对（`content` 是 `time_requirement` 的子串那类会让子串判据恒真）。
        """
        tokens = _tokens(self.text)
        missing = sorted(
            name
            for name in (*self.UPDATE_FIELDS, *self.UPDATE_REFUSED_FIELDS)
            if name.lower() not in tokens
        )
        self.assertEqual(missing, [], f"文档里缺这些改单字段：{missing}")

    def test_every_create_field_is_in_the_document(self):
        tokens = _tokens(self.text)
        missing = sorted(name for name in self.CREATE_FIELDS if name.lower() not in tokens)
        self.assertEqual(missing, [], f"文档里缺这些录单字段：{missing}")

    def test_it_says_the_change_endpoint_refuses_fields_outside_the_table(self):
        """⭐ 改单的 fail-closed（表外字段 ⇒ `1000`）必须写破：⛔ 不是"忽略"。"""
        self.assertIn("不可以改的字段（传了 ⇒ `1000`", self.text)
        self.assertIn("`PATCH {}`（一个字段都不改）是合法的", self.text)

    def test_it_says_the_write_endpoints_need_an_idempotency_key(self):
        """幂等键是**硬前置** ⇒ 文档必须写破它的名字与长度契约。"""
        self.assertIn("Idempotency-Key", self.text)
        self.assertIn("8–128", self.text)

    def test_it_says_the_source_order_no_dedup_window_is_30_days(self):
        """⭐ 去重窗口（30 天）是我们**自己的代价** ⇒ 必须写破。"""
        self.assertIn("去重窗口 = 30 天", self.text)

    def test_it_says_the_whitelist_is_fail_closed_and_names_the_keys(self):
        """⭐ 表外的键 ⇒ `1000` **并点名**（⛔ 不是"忽略"）——这条要逐字写破。"""
        self.assertIn("请求里有本 API 不接受的字段：", self.text)
        self.assertIn("fail-closed", self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class CallbackPushContractDocTests(SimpleTestCase):
    """⭐ **我们推给第三方的回调**是这份文档里最难替代的一节（第三方拿不到别的说明）。

    判据：① 报文 10 个字段逐字在场；② 三个头名逐字在场；③ 签名算法的**待签串**逐字在场；
    ④ 规范化的三条口径在场；⑤ 回执 / 退避 / 至少一次 / 预签名时效这几条硬事实在场。
    ⚠️ 它**不**证明文档与实现一致 —— 那一半由 `ExecutableSnippetTests` 的
    "文档示例 vs `outbound.compute_signature`"那条对拍管。
    """

    def setUp(self):
        self.text = _read_doc()

    def test_every_callback_payload_key_is_in_the_document(self):
        tokens = _tokens(self.text)
        missing = sorted(k for k in EXPECTED_CALLBACK_KEYS if k.lower() not in tokens)
        self.assertEqual(missing, [], f"文档里缺这些回调报文字段：{missing}")

    def test_the_three_outbound_headers_are_word_for_word(self):
        for header in EXPECTED_CALLBACK_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, self.text)

    def test_the_three_events_are_named(self):
        for event in EXPECTED_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, self.text)

    def test_the_signed_string_is_word_for_word(self):
        """⭐ 待签串**逐字**（顺序换一下就全验不过）—— 判据是那三段的固定写法。"""
        self.assertIn('f"{timestamp}\\n{delivery_id}\\n"', self.text)
        self.assertIn("sha256(canonical).hexdigest()", self.text)

    def test_the_canonicalisation_three_flags_are_all_declared(self):
        """⚠️ 三条缺一不可（少一条 ⇒ 对端复算出的摘要是另一个值，而症状是"全都验不过"）。"""
        self.assertIn("ensure_ascii=False", self.text)
        self.assertIn("sort_keys=True", self.text)
        self.assertIn('separators=(",", ":")', self.text)

    def test_the_receipt_rule_is_2xx_only(self):
        self.assertIn("只有 HTTP 2xx 算成功", self.text)

    def test_the_retry_backoff_table_is_word_for_word(self):
        """⭐ 退避六档 + 「共 6 次机会」必须逐字在场（第三方据此安排重试与告警）。"""
        self.assertIn("重试退避 10s / 60s / 5m / 15m / 1h / 6h，共 6 次机会", self.text)

    def test_the_at_least_once_semantics_and_dedup_instruction_are_declared(self):
        """⭐ 「至少一次」+「按 `delivery_id` 去重」是这一节**最重要**的两句。"""
        self.assertIn("至少一次", self.text)
        self.assertIn("按 `delivery_id` 去重", self.text)

    def test_the_presigned_image_url_lifetime_is_declared(self):
        """⚠️ `images` 里给的是**投递那一刻生成**的预签名 URL（URL 稳定化线，2026-11-09：
        生产对象存储整点对齐 1–2 小时；本机/测试 300 秒）。过期即失效的口径不变。"""
        self.assertIn("有效期 1–2 小时", self.text)
        self.assertIn("别按 url 去重", self.text)

    def test_the_callback_url_requirements_are_declared(self):
        self.assertIn("只允许 `https`", self.text)
        self.assertIn("不跟随重定向", self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class ErrorContractTests(SimpleTestCase):
    """对外承诺的错误码、失败文案、"失败也是 HTTP 200" 与 403 / 429 的含义。"""

    def setUp(self):
        self.text = _read_doc()

    def test_the_throttled_message_example_is_the_one_the_platform_actually_renders(self):
        """429 那句文案**不是本平台写的**（框架出厂的），所以文档必须按实际渲染抄。

        ⚠️ 钉的是**片段**（`请求已被限流`）而不是整句：整句里的秒数随剩余窗口变。
        """
        self.assertIn("请求已被限流", self.text)

    def test_it_says_business_failures_are_still_http_200(self):
        self.assertIn("业务失败也是 HTTP 200", self.text)

    def test_the_surviving_failure_messages_are_listed(self):
        """今天在服役的三句认证失败文案必须逐字在文档里（⛔ 不是放宽成"随便有一句就行"）。"""
        for message in EXPECTED_FAILURE_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message, self.text)

    def test_the_surviving_messages_are_distinct(self):
        # 对照：三句若被写成同一句，"逐字列出"就没有意义了（同族：文案可判别性是判据的一部分）。
        self.assertEqual(len(set(EXPECTED_FAILURE_MESSAGES)), 3)

    def test_the_oauth2_error_values_are_listed(self):
        """⭐ 取令牌端点那三档 `error` 值是**对外契约**（第三方按标准读它们）。"""
        for value in EXPECTED_OAUTH2_ERRORS:
            with self.subTest(value=value):
                self.assertIn(value, self.text)

    def test_the_token_endpoint_success_body_is_exactly_three_keys(self):
        """⭐ 令牌端点回**裸三键**（⛔ 不是信封）—— 三键逐字在场，且没有第四个。"""
        for key in ("access_token", "token_type", "expires_in"):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', self.text)
        self.assertIn("⛔ 不是本仓那个 `code` / `msg` / `data` 信封", self.text)

    def test_the_declared_expires_in_matches_the_implementation(self):
        """⭐ 第三层对拍：文档里写的 `expires_in` 必须**恰好**等于实现的有效期常量。

        ⚠️ 判据取**全部**出现处（`findall` 而不是 `search`）：只改其中一处也会红。
        """
        declared = set(EXPIRES_IN_RE.findall(self.text))
        self.assertEqual(declared, {str(TOKEN_TTL_SECONDS)},
                         f"文档里声明的 expires_in 与实现（{TOKEN_TTL_SECONDS} 秒）不一致：{declared}")

    def test_it_says_expiry_is_hard_and_there_is_no_refresh(self):
        self.assertIn("有效期到点即失效", self.text)
        self.assertIn("没有 refresh", self.text)

    def test_it_says_rotating_the_secret_kills_every_issued_token(self):
        self.assertIn("所有旧令牌立刻失效", self.text)

    def test_documented_codes_are_exactly_the_promised_ones(self):
        """⭐ 码表里出现的 `code` 值集合必须**恰好**是承诺的那几个（⛔ 不新开码、⛔ 不漏）。"""
        section = self.text.split("对外承诺的错误码", 1)
        self.assertEqual(len(section), 2, "文档里找不到「对外承诺的错误码」那一节")
        table = section[1].split("##", 1)[0]
        found = set(re.findall(r"^\| `(\d+)` \|", table, re.MULTILINE))
        self.assertEqual(found, set(EXPECTED_CODES))
        # ⛔ 判别力：多一个码会红 —— 那就是"新开码"。
        self.assertNotIn("1012", found)
        self.assertNotIn("9999", found)

    def test_it_says_429_is_a_fallback_not_a_quota(self):
        self.assertIn("不是配额", self.text)
        self.assertIn("6000 次 / 分钟 / 应用", self.text)

    def test_it_says_1003_is_not_an_open_platform_code(self):
        """⭐ `1003` 是**账号线**的码：排查顺序里必须说破"本对外面不会出现它"。"""
        self.assertIn("1003", self.text)
        self.assertIn("账号线", self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class ForbiddenThingsTests(SimpleTestCase):
    """对第三方的"须知"逐条：那几条禁令必须都在文档里。"""

    def setUp(self):
        self.text = _read_doc()

    def test_browser_direct_access_is_not_supported(self):
        self.assertIn("浏览器端直连不支持", self.text)
        self.assertIn("没有 CORS", self.text)
        self.assertIn("必须在服务端调", self.text)

    def test_rotation_has_no_grace_period(self):
        self.assertIn("旧密钥立即失效、没有宽限期", self.text)

    def test_not_found_and_out_of_scope_share_one_message(self):
        self.assertIn("回**同一句话**", self.text)
        self.assertIn("记录不存在或已被删除", self.text)

    def test_the_secret_is_plaintext_and_always_available(self):
        """⭐ 密钥口径（2026-10-07 业务方指令）与**没有放松的那一条**都要钉住。

        ① 新口径在文档里逐字在；② 旧口径那两句⛔ **不许**再作为承诺留在文档里
        （留下就是与实现相反的话，`AGENTS.md` §5-C）。
        """
        self.assertIn("`app_secret` 是明文、随时可取", self.text)
        self.assertIn("密钥改成明文展示 可以随时获取", self.text)
        self.assertIn("AppSecret 是明文、租户随时可以取到", self.text)
        # ② 旧口径不许再作为**承诺**出现（只允许出现在"已作废/改准"的上下文里）。
        lines = self.text.splitlines()
        for forbidden in ("之后永不回显", "只在两个响应里出现", "丢了就只能轮换"):
            for index, line in enumerate(lines):
                if forbidden not in line:
                    continue
                context = "\n".join(lines[max(0, index - 3) : index + 1])
                self.assertTrue(
                    any(mark in context for mark in ("改准", "作废", "原口径", "原文写的是")),
                    f"旧口径被当成承诺留在文档里：{line[:100]}",
                )
        # ③ 「不进留痕」这条**没有**放松 ⇒ 文档仍须写明（它今天由后端用例钉着）。
        self.assertIn("AppSecretNeverLoggedTests", self.text)

    def test_no_delete_endpoint(self):
        self.assertIn("只能停用不能删", self.text)
        self.assertIn("405", self.text)

    def test_content_may_carry_credentials(self):
        """`content` 是客户原文、我方不清洗 ⇒ 文档必须写明（⛔ 不许写成"已脱敏"）。

        ⚠️ 否定的判据打在**逐行**上：文档里"脱敏"只允许以否定形态出现
        （`不会把它脱敏` / `不声称"已脱敏"`）——只断一句会被"别处补一句正向话"绕过。
        """
        self.assertIn("不做任何清洗", self.text)
        for line in self.text.splitlines():
            if "脱敏" in line:
                self.assertIn("不", line, f"这一行把 `content` 说成已脱敏：{line[:80]}")

    def test_the_callback_url_must_be_public_https(self):
        """出站那条通道的 SSRF 口径也是**对第三方的须知**（⛔ 内网自建回调本批不支持）。"""
        self.assertIn("公网", self.text)
        self.assertIn("内网自建回调本批不支持", self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class FieldWhitelistTests(SimpleTestCase):
    """查单载荷的字段白名单：文档里必须**恰好**能数出契约那一组键。"""

    def test_every_contract_key_is_in_the_document(self):
        tokens = _tokens(_read_doc())
        missing = sorted(key for key in EXPECTED_DATA_KEYS if key.lower() not in tokens)
        self.assertEqual(missing, [], f"文档里缺这些契约键：{missing}")

    def test_the_groups_denied_by_the_business_owner_are_declared_as_denied(self):
        """C 组（员工身份）与 D 组（内部痕迹）是**裁决**，⛔ 不是遗漏 ⇒ 文档要说清。"""
        text = _read_doc()
        self.assertIn("有意不给，不是遗漏", text)
        for denied in ("remark", "deleted_at", "deleted_by", "sales_agent", "hall_publisher"):
            self.assertIn(denied, text)

    def test_it_declares_no_profit_field_and_says_compute_it_yourself(self):
        self.assertIn("没有\"利润\"字段", _read_doc())


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class ParityDifferenceTableTests(SimpleTestCase):
    """⭐ **对标差异表**必须留在文档里（它是"如实写"的落点，删了就成假话）。

    逐条钉住那些**第三方最容易照抄错**的差异：我们没有的字段 / 排行榜未开放 /
    交单时间恒 `null` / 上传截图两步 / `ImgType=1` 不收 / 响应码方向相反。
    """

    def setUp(self):
        self.text = _read_doc()

    def test_the_table_itself_is_declared(self):
        self.assertIn("对标差异表", self.text)

    def test_the_missing_fields_are_named_with_our_substitute(self):
        for name in ("hours", "customerId", "game_password", "gaemTf", "souresOrderID",
                     "ignoreRepeatOrderNo", "imgUrl", "ImgType=1"):
            with self.subTest(name=name):
                self.assertIn(name, self.text)

    def test_the_response_code_direction_difference_is_declared(self):
        """⚠️ 对标**写**接口是 `code > 0` 成功、我们是 `code == 0` 成功 —— 方向相反。"""
        self.assertIn("**`code == 0` 是成功**", self.text)
        self.assertIn("`code > 0` 才是成功", self.text)

    def test_the_ranks_and_submitted_at_differences_are_declared(self):
        self.assertIn("本批不实现", self.text)
        self.assertIn("`submitted_at` **恒为 `null`**", self.text)

    def test_the_two_step_screenshot_flow_is_declared(self):
        self.assertIn("取票", self.text)
        self.assertIn("认领", self.text)
        self.assertIn("不会去抓你给的 URL", self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class DocSelfConsistencyTests(SimpleTestCase):
    """文档内部自洽（⛔ 这几条与本仓实现无关，是纯文本判据，故不必进数据库）。"""

    def test_hmac_sha256_is_the_declared_algorithm(self):
        """⚠️ 今天用 HMAC-SHA256 的地方**只剩出站回调签名**（入站签名已删除，§0.1）。"""
        self.assertIn("HMAC-SHA256", _read_doc())

    def test_the_list_endpoint_says_total_is_the_match_count(self):
        self.assertIn("`total` 是\"命中的总条数\"", _read_doc())

    def test_the_list_endpoint_says_items_are_capped_at_twenty(self):
        self.assertIn("`items` 最多 20 条", _read_doc())

    def test_source_order_no_three_semantics_are_all_present(self):
        text = _read_doc()
        self.assertIn("按空串查", text)
        self.assertIn("不带这个参数", text)

    def test_nullable_amounts_are_documented_as_null_not_zero(self):
        self.assertIn("`null` 与 `\"0.00\"` 是两件事", _read_doc())

    def test_costs_are_lists_on_both_keys(self):
        self.assertIn("两个键都是列表", _read_doc())


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class ReevaluationTriggersTests(SimpleTestCase):
    """⭐「重新评估的触发条件」那一节必须留着**并且与现状相关**（⛔ 不许丢）。"""

    def setUp(self):
        self.text = _read_doc()

    def test_the_section_is_there(self):
        self.assertIn("## 10. 重新评估的触发条件", self.text)

    def test_it_carries_the_triggers_that_are_true_today(self):
        for trigger in ("浏览器端直连", "≥3 家", "内网自建回调地址", "接单排行榜"):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, self.text)


@unittest.skipUnless(_HAS_OPEN_API_DOC, "本仓库只开源网页前后端源码，不含内部文档 docs/OPEN-API.md")
class SnippetHygieneTests(SimpleTestCase):
    """⚠️ 三份示例的**结构**判据：每个语言**恰好一段**、都够长、都不是伪代码。"""

    LANGUAGE_SAMPLE_REQUIREMENTS = {
        "python": ("import hmac", "def compute_signature"),
        "php": ("<?php", "hash_hmac"),
        "javascript": ("require('crypto')", "createHmac"),
    }

    def test_each_language_has_exactly_one_block(self):
        """⭐ 每个语言只允许**恰好一段**：多段会让"抽出哪一段"变成隐式约定
        （文档加一段示例就可能让判据悄悄换了目标）。"""
        blocks = _code_blocks(_read_doc())
        for language in self.LANGUAGE_SAMPLE_REQUIREMENTS:
            with self.subTest(language=language):
                _single_block(blocks, language)

    def test_each_block_is_long_enough_to_be_a_real_sample(self):
        blocks = _code_blocks(_read_doc())
        for language, required in self.LANGUAGE_SAMPLE_REQUIREMENTS.items():
            with self.subTest(language=language):
                source = _single_block(blocks, language)
                self.assertGreaterEqual(len(source.splitlines()), 20)
                for marker in required:
                    self.assertIn(marker, source)
                self.assertNotIn("# ...", source, "示例里有省略号占位（那是伪代码）")
