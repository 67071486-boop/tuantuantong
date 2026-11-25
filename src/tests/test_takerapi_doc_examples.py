"""`docs/TAKER-API.md` 的**字面面值** + **示例代码真跑**的判据。## 这个文件为什么存在：`docs/TAKER-API.md` 是**打手侧**给第三方的对外契约（第一行就写着读者是 AI 编程工具），而本线**没有引入自动生成接口文档的框架** ⇒ 代价是手写文档**会漂移**；⇒ 缓解办法就是本文件：把文档里那几处**关键面值**与**示例代码**做成判据，让漂移在门禁里当场红。⚠️ 它是照 `src/tests/test_openapi_doc_examples.py` 的**形状**自己写的（⛔ 不复制那一个）：那一份读的是**上家侧**那份文档、跑的是**回调签名**那一套；本文件读**打手侧**那份文档、跑的是**换令牌 → 拉大厅 → 抢单 → 查我的单**这条链。两份文档、两族端点、两套凭据，⛔ 不要合并，也⛔ 不要互相 import。
判据分三层（各层证明什么、⛔ 不证明什么）：1. **字面面值**：文档里必须**包含**对外承诺的路径 / 契约键集 / 错误码 / 那几条禁令句——只证明"这些字还在文档里"，⛔ **不证明**文档的散文与实现一致（那一半由 `tests/test_takerapi_*.py` 的行为用例管）。2. ⭐ **示例代码真的跑**：抽出文档 §10.2 的 Python 段，在本进程 `exec`：① 先跑它的 `__main__` 离线自检（合成桩，⛔ 不联网）；② 再**对着真实的打手侧端点**（Django 测试客户端 + 一个把 urllib 请求翻成 WSGI 请求的适配器）走完整条链 ⇒ 示例里的**每一个请求形状**（URL / 方法 / `Content-Type` / `Authorization` / `Idempotency-Key`）都被真实端点**接受或拒绝**过，⛔ 不是"看起来对"。3. ⭐ **与实现对拍**：§3.2 / §5.3 的键集**恰好**等于实现真正回的键集；§8.2 的码表**恰好**等于实现会返回的那几个；§2 端点全表里那七条**对外**路径必须落在 `apps/takerapi/urls.py` 的 `v1/…` 那八条里，而 urlconf 的**字面量集合****恰好**等于 `EndpointTableTests.CONTRACTS` 那一串（14 条 = 对外 8 ＋ 取令牌 1 ＋ 内部 5；⚠️ **两向**断言：多一条路由、少一条路由、改一个字都要红）⇒ 这一层挡的是"文档与实现各说一套"。⛔ 三条纪律（照 `AGENTS.md` §5-G 与 `E-0024`）：**键集 / 路径 / 码表一律写字面量**（⛔ 不从被测模块 import 常量、⛔ 也不 import 文档里的数字——自指断言在被断言的常量被改坏时**不会红**）；**豁免必须显式枚举**（`FORBIDDEN_TOKENS` 那张表；⛔ 不用"理由文本里含某个词"当后门）；**文档里的数字与实现里的常量⛔ 不许互相 import**（唯一例外是**行为对拍**——例如 `expires_in` 断言"实现回的 == 文档写的"，而那种对拍的两侧必须来自**两路不同的来源**）。回调推送那一章（文档 §12，第三方拿不到别的说明）同一次补两层：**字面面值**（`CallbackPushContractTests`：四类事件名 / 8 个业务键 / `reason` 三个取值 / 三个 `X-TT-*` 头 / 待签串 / 规范化三条 flag / 2xx 才算成功 / 退避六档 / 至少一次与去重 / 预签 300 秒 / 地址要求 / 如实说明）与 ⭐ **真跑＋对拍**（`CallbackSignatureSnippetTests`：文档 §12.3 验签代码 `exec`，其 `__main__` 自检真的跑；文档四行值与本仓实现 `apps.openapi.outbound` 当场算出来的**对拍**——两路来源，⛔ 任一侧单独改都会红）。
⚠️⭐ `X-TT-Timestamp` / `X-TT-Delivery` / `X-TT-Signature` / `callback_url` / `主动推送` 这五个词**已从 `FORBIDDEN_TOKENS` 移出**：它们原先在禁令表里是因为文档只有否定句；回调推送那一章（§12）真的加了这一族 ⇒ 留禁令会**逼文档说假话**，现改由 `CallbackPushContractTests` **正面**钉住（三个头逐字、事件名逐字、`callback_url` 那一格的语义）——⛔ 不是"豁免"，是**换了判据方向**；`refresh_token` / `CORS` **照旧是禁令**（那两条今天仍然是真的"本侧没有"）；「大厅不推、必须轮询」那两条判据**没有删**：见 `test_it_says_the_hall_is_not_pushed_and_polling_is_still_required`。⚠️ 证明力边界（如实写清楚）：端点那一半走 **Django 测试客户端**（真实 URLconf / 认证类 / 视图 / 数据库），⛔ **不是**真实 socket——不证明 HTTP 服务器、反代、TLS 那一层；夹具一律**合成值**（`AGENTS.md` §5-C / `E-0077`）；本文件**不**证明上家侧那份文档（`docs/OPEN-API.md`）——那是 `tests/test_openapi_doc_examples.py` 的活，两者互不影响。"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import urllib.error
import urllib.parse
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import grant_nodes

User = get_user_model()

#: 本文件在 `src/tests/` 下 ⇒ `parents[1]` 是 `src/`、`parents[2]` 是**仓库根**。
REPO_ROOT = Path(__file__).resolve().parents[2]
TAKER_API_MD = REPO_ROOT / "docs" / "TAKER-API.md"
TAKER_URLCONF = REPO_ROOT / "src" / "apps" / "takerapi" / "urls.py"

# ═════════════════════════════════════════════════════════════════════════════
# 第 1 层：文档里必须出现的对外承诺面（**字面量**，⛔ 不从实现 import）
# ═════════════════════════════════════════════════════════════════════════════

#: 端点全表里的**对外路径**（§2）。⚠️ 逐字，含尾斜杠 —— 取令牌那一条**没有**尾斜杠。
#: ⚠️ 是**八条**而不是七条：`…/images/` 与 `…/images/ticket/` 是**两条路径**（同一个族的
#: 第 2、3 步）；"七条"说的是表里那**七个编号行**（第 7 行 = 接镜像单，D-0296）。
DOCUMENTED_PATHS = (
    "/api/taker/v1/hall/",
    "/api/taker/v1/hall/<order_no>/claim/",
    "/api/taker/v1/hall/upstream/<pk>/claim/",
    "/api/taker/v1/hall/peer/<pk>/claim/",
    "/api/taker/v1/my/orders/",
    "/api/taker/v1/orders/<order_no>/",
    "/api/taker/v1/orders/<order_no>/status/",
    "/api/taker/v1/orders/<order_no>/images/",
    "/api/taker/v1/orders/<order_no>/images/ticket/",
)

#: §3.2 大厅行的**恰好 10 个键**（字面量写死；⛔ 不从 `hall_views` 派生 —— 派生是自指恒真）。
HALL_ROW_KEYS = {
    "order_no",
    "source_order_no",
    "game",
    "game_name",
    "server",
    "project_text",
    "content",
    "time_requirement",
    "is_urgent",
    "hall_price",
    "created_at",
}

#: §5.3 「我的单」**加**的那 7 个键（行 = 上面 10 个 ＋ 这 7 个 = **恰好 17**）。
MY_ORDER_EXTRA_KEYS = {
    "status",
    "status_text",
    "claimed_at",
    "updated_at",
    "customer_phone",
    "customer_ww",
    "total_price",
}

#: 图片项那一行（§7.2 的"恰好七个键"，列表与认领**同一个装配出口**）。
IMAGE_ROW_KEYS = {
    "img_type",
    "url",
    "original_name",
    "size_bytes",
    "remark",
    "uploaded_by_name",
    "created_at",
}

#: §8.2 信封那一侧**对外承诺**的业务码（⛔ 不新开码、⛔ 不漏）。
EXPECTED_ENVELOPE_CODES = {"0", "1000", "1001", "1002", "1012", "1013", "401", "429"}

#: §1.1 / §8.2 取令牌端点那一侧的三个 OAuth2 `error` 取值。
EXPECTED_OAUTH2_ERRORS = ("invalid_client", "unsupported_grant_type", "temporarily_unavailable")

#: 认证层三句失败文案（⛔ 逐字，不许按英文习惯猜措辞）。
EXPECTED_401_MESSAGES = ("应用凭据无效", "令牌已过期", "来源 IP 不在白名单内")

#: ⛔ 对外契约里**不许作为承诺出现**的东西。⚠️ 判据搜**主词本身**（`AGENTS.md` §5-C：只搜派生短语会让"Expected: 空"成为**假保证**）。
#: ⚠️ 有几处**必须在场**（它们是**否定句**：说清"本侧没有这个东西"）⇒ 豁免**显式枚举**在下面那张表里：**除**这些字面片段之外，主词一个都不许再出现。
#: ⛔⭐ 签名面相关的五个词（`callback_url` / `X-TT-Signature` / `X-TT-Timestamp` / `X-TT-Delivery` / `主动推送`）**不在本表**：它们原先的禁令针对的是"文档只有否定句"的时代；回调推送那一章（§12）落地后留着它们会**逼文档说假话**（`AGENTS.md` §5-C）⇒ 已移出、改由 `CallbackPushContractTests` **正面**钉住：三个头 + `callback_url` 那一格的语义 + 事件名逐字（⛔ 不是放宽成"不查"）。⚠️ 另外两条**没有放松**：「大厅里来了新单不推」「必须轮询」仍由 `test_it_says_the_hall_is_not_pushed_and_polling_is_still_required` 断言。
FORBIDDEN_TOKENS = (
    "refresh_token",
    "CORS",
)

#: `{主词: 允许出现的**完整字面片段**} `——出现处必须**整个落在**这些片段里。
#: ⛔ 这不是"理由文本里含某个词"的后门（那种判据恒真）：片段是**逐字**的，改一个字就红。
#: ⚠️ 每一条都对应一句**否定句**（"没有 X"），而不是任何形式的承诺。
ALLOWED_CONTEXTS = {
    "refresh_token": (
        "⛔ 没有 `refresh_token`",
        "⛔ 没有 `grant_type=refresh_token`",
        "⛔ 没有 refresh_token",
    ),
    "CORS": (
        "没有 CORS 响应头",
        "没有 CORS，必须**服务端**调（§9）",
        "没有 CORS，§9.1）⇒ 必须服务端调。",
        "就算将来开了 CORS",
    ),
}

#: §10.1 的**期望输出**（示例的 `main()` 打印的三行，逐字）。
#: ⚠️ 这一处与文档那一处是**两路来源** ⇒ "改文档不改期望"或反之都会红。
EXPECTED_MAIN_OUTPUT = (
    "token_type=Bearer expires_in=7200",
    "hall_count=1",
    "order=20261008000000000077 claim_status=IN_PROGRESS "
    "my_orders_has_it=True my_status=IN_PROGRESS",
)

#: §10.1 的合成夹具（⛔ 不是真实密钥、⛔ 不是真实订单）。
FIXTURE_CLIENT_ID = "takerdocappkey0001"
FIXTURE_CLIENT_SECRET = "taker-doc-synthetic-secret-0001"
FIXTURE_ORDER_NO = "20261008000000000077"
FIXTURE_UPDATED_SINCE = "2026-10-08T00:00:00+08:00"
FIXTURE_EXPIRES_IN = 7200

#: 契约里的分页上限（§2 的统一口径；字面量）。
MAX_PAGE_SIZE = 100

# ═════════════════════════════════════════════════════════════════════════════
# 第二期（§12 回调推送）的字面面值 —— ⛔ 全部写字面量，⛔ 不从实现 import
# ═════════════════════════════════════════════════════════════════════════════

#: §12.1 那**四类事件**（字面量；多一类 / 少一类 / 改一个字都要红）。
#: ⚠️ 第 4 类 `order.reassigned` 是**打手侧独有**的（上家侧那份文档里没有它）。
EXPECTED_CALLBACK_EVENTS = (
    "order.status_changed",
    "order.deleted",
    "order.image_added",
    "order.reassigned",
)

#: §12.2.1 的**8 个业务键**（每一类事件带其中一部分，⛔ 不是"每条报文都有这 8 个"）。
EXPECTED_CALLBACK_GENERIC_KEYS = (
    "event",
    "delivery_id",
    "order_no",
    "source_order_no",
    "status",
    "note",
    "images",
    "occurred_at",
)

#: §12.2.2 的**投递时现算的两键**（每一类事件都有）。
EXPECTED_CALLBACK_DELIVERY_KEYS = ("timestamp", "sign")

#: §12.2.3：`order.reassigned` 多的那一键，以及它**恰好三个**机器可读取值。
EXPECTED_REASSIGNED_KEY = "reason"
EXPECTED_REASSIGNED_REASONS = ("taker_removed", "refunded", "deleted")

#: §12.3 的**三个请求头**（逐字；与上家侧同一套契约）。
EXPECTED_CALLBACK_HEADERS = ("X-TT-Timestamp", "X-TT-Delivery", "X-TT-Signature")

#: §12.4 的退避**六档**（逐档字面量；改一档、少一档都要红）。
EXPECTED_BACKOFF_LADDER = ("10s", "60s", "5m", "15m", "1h", "6h")

#: 退避表里那六档的**人话**（表里写的是中文时长 ⇒ 逐档对得上才算"六档都在"）。
EXPECTED_BACKOFF_LABELS = ("10 秒", "60 秒", "5 分钟", "15 分钟", "1 小时", "6 小时")

#: §12.2 那个示例报文（`order.status_changed`）的**恰好 9 个键**
#: （= 8 个业务键里去掉 `images`，＋ `timestamp` ＋ `sign`）。
EXPECTED_CALLBACK_EXAMPLE_KEYS = frozenset(
    {
        "event",
        "delivery_id",
        "order_no",
        "source_order_no",
        "status",
        "note",
        "occurred_at",
        "timestamp",
        "sign",
    }
)

#: §12.3.1 的合成夹具（⛔ 不是真实密钥 / 订单）。
FIXTURE_CALLBACK_SECRET = "taker-doc-synthetic-secret-0001"
FIXTURE_CALLBACK_TIMESTAMP = 1760000000
FIXTURE_DELIVERY_ID = "9a1b2c3d4e5f60718293a4b5c6d7e8f9"
FIXTURE_SOURCE_ORDER_NO = "SYN-2026-0007"

#: §12.3.1 的四行值：规范化字节 / 摘要 / 期望签名。
#: ⚠️ 这三行与文档里那三段围栏**两路来源**：文档那边改一个字，这里就红。
EXPECTED_CALLBACK_CANONICAL = (
    '{"delivery_id":"9a1b2c3d4e5f60718293a4b5c6d7e8f9","event":"order.status_changed",'
    '"note":"","occurred_at":"2026-10-08T12:00:00+08:00",'
    '"order_no":"20261008000000000077","source_order_no":"SYN-2026-0007",'
    '"status":"IN_PROGRESS","timestamp":1760000000}'
)
EXPECTED_CALLBACK_SHA256 = (
    "5b983a7d9f7ae0643b4ff1d20484de705a6f1f77fd9de85d62b76fb6df1021fb"
)
EXPECTED_CALLBACK_SIGNATURE = (
    "2d3b602594aae9f2f5247d6812584d6818826224b240afc54cab41f56278869d"
)

#: §12.3 那段验签代码的三个**接缝**（改名两边一起红）+ 它的自检输出（两行，逐字）。
CALLBACK_SNIPPET_REQUIRED_NAMES = (
    "canonical_json",
    "sign_for",
    "verify_callback",
    "EXAMPLE_PAYLOAD",
    "EXAMPLE_HEADERS",
)
EXPECTED_VERIFY_SELF_CHECK_OUTPUT = (
    "sign=" + EXPECTED_CALLBACK_SIGNATURE,
    "event=order.status_changed order_no=20261008000000000077 "
    "dedup_key=" + FIXTURE_DELIVERY_ID,
)

#: 文档里那三段的定位标记（`_fenced_literal` 取"标记之后第一个围栏块"）。
CALLBACK_SNIPPET_MARKER = "**可复制的校验代码**（Python，标准库"
CALLBACK_SELF_CHECK_MARKER = "**这段自检打印的读数**"
CALLBACK_EXAMPLE_PAYLOAD_MARKER = "以 `order.status_changed` 为例（**这个例子里的值都是合成值**"

#: 对外承诺的九条**方法 + 路径**组合（§2 那张表逐行）。
DOCUMENTED_METHOD_PATHS = (
    ("GET", "/api/taker/v1/hall/"),
    ("POST", "/api/taker/v1/hall/<order_no>/claim/"),
    ("POST", "/api/taker/v1/hall/upstream/<pk>/claim/"),
    ("POST", "/api/taker/v1/hall/peer/<pk>/claim/"),
    ("GET", "/api/taker/v1/my/orders/"),
    ("GET", "/api/taker/v1/orders/<order_no>/"),
    ("POST", "/api/taker/v1/orders/<order_no>/status/"),
    ("GET", "/api/taker/v1/orders/<order_no>/images/"),
    ("POST", "/api/taker/v1/orders/<order_no>/images/ticket/"),
    ("POST", "/api/taker/v1/orders/<order_no>/images/"),
)

_FENCE_RE = re.compile(r"^```(\w*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
_TABLE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*(.*?)\s*\|", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _read_doc() -> str:
    """读文档原文（文件必须存在 —— 缺了它就是本线最严重的漂移）。"""
    return TAKER_API_MD.read_text(encoding="utf-8")


def _code_blocks(text: str) -> dict:
    """取出全部带语言标注的围栏代码块，返回 `{语言: [代码, ...]}`。"""
    blocks: dict = {}
    for language, body in _FENCE_RE.findall(text):
        if language:
            blocks.setdefault(language.lower(), []).append(body)
    return blocks


def _single_block(blocks: dict, language: str) -> str:
    """取某一语言的**恰好一段**（多段会让"抽出哪一段"变成隐式约定）。"""
    candidates = blocks.get(language, [])
    assert len(candidates) == 1, (
        f"文档里 ```{language} 代码块应恰好 1 段（用来抽示例代码），实际 {len(candidates)} 段"
    )
    return candidates[0]


def _fenced_literal(text: str, marker: str) -> str:
    """取"`marker` 之后第一个围栏块"的内容（文档里的字面夹具值）。"""
    occurrences = text.count(marker)
    assert occurrences == 1, f"夹具标记应恰好出现 1 次，实际 {occurrences} 次：{marker}"
    lines = text[text.find(marker) :].splitlines()
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


def _table_block(text: str, marker: str) -> str:
    """取 `marker` 之后的**第一张 markdown 表**（到第一行非表格内容为止）。
    ⚠️ 表之前那几行散文**一律跳过**（"标题紧跟表"与"标题 → 一段话 → 表"两种形状都存在：`## 2. 端点全表` 与 `### 7.2 入参与应答` 就分别是那两种）⇒ 一把尺子，两种情况都对。"""
    parts = text.split(marker, 1)
    assert len(parts) == 2, f"文档里找不到这一个标记：{marker}"
    lines: list = []
    started = False
    for line in parts[1].splitlines()[1:]:
        stripped = line.strip()
        if not started:
            if stripped.startswith("|"):
                started = True
            else:
                continue
        if not stripped.startswith("|"):
            break
        lines.append(line)
    return "\n".join(lines)


def _numbered_table_rows(text: str) -> dict:
    """端点全表（`| # | 方法 | 路径 | … |`）⇒ `{编号: 该行的单元格列表}`。
    ⚠️ 用**整表切格**（`_markdown_rows`）而不是"从行尾回溯到一个 `|`"那种正则：后者在"一行里有多个 `|`"时会**只取到第一格**（`|\s*(.*?)\s*\|` 是非贪婪的），症状是"抽出来的行里只有方法、没有路径"（本文件第一版就踩过）。"""
    rows = _markdown_rows(text)
    return {
        row[0]: row
        for row in rows
        if row and re.fullmatch(r"\d+", row[0])
    }


def _markdown_rows(text: str) -> list:
    """一段 markdown 表格 ⇒ 逐行的**单元格**列表（`---` 分隔行不算）。"""
    rows: list = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        rows.append(cells)
    return rows


def _urlconf_call_paths() -> tuple:
    """从 `urls.py` 的源码里抽出 `path("…", …)` 的**第一个位置实参**（AST，⛔ 不 import）。
    ⚠️ 用 `ast` 而不是 import：`apps.takerapi.urls` 的 import 会**注册路由转换器**并拉起整条视图链——判据只需要那张**字面量**表（而"源码里写了什么"正是要钉的东西）。"""
    return tuple(template for _literal, template in _urlconf_literals_and_templates())


def _urlconf_literals_and_templates() -> tuple:
    """`urls.py` 的每条 `path()` ⇒ `(字面量, **对外契约里的写法**)`。
    ⚠️ 两件事必须分开：**字面量**是源码里那个串（`v1/orders/<order_no:order_no>/`，带**转换器名**）；**对外写法**是文档契约里的写法（`v1/orders/<order_no>/`，**不带**转换器名）。
    ⛔ 拿前者去比文档会**永远不等**（那正是第一版判据红的原因：契约里不写转换器名）。"""
    import ast

    tree = ast.parse(TAKER_URLCONF.read_text(encoding="utf-8"))
    pairs: list = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "path"):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            pairs.append((first.value, _documented_writing(first.value)))
    return tuple(pairs)


def _documented_writing(literal: str) -> str:
    """`<order_no:order_no>` / `<int:pk>` ⇒ 契约里的写法 `<order_no>` / `<pk>`。"""
    return re.sub(r"<[^:<>]+:([^<>]+)>", r"<\1>", literal)


# ═════════════════════════════════════════════════════════════════════════════
# 把文档 §10.2 那段 Python 抽出来真跑（`exec` + 它的 `__main__` 自检）
# ═════════════════════════════════════════════════════════════════════════════

#: 文档那段示例里被本文件当作"接缝"使用的名字（改名两边一起红）。
SNIPPET_REQUIRED_NAMES = (
    "OAuth2Error",
    "call_api",
    "claim_order",
    "fetch_token",
    "list_hall",
    "main",
    "my_orders",
    "offline_opener",
)


class DocPythonSnippetTestCase(SimpleTestCase):
    """抽出文档 §10.2 的 Python 段并 `exec`（含它的 `__main__` 自检）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = _read_doc()
        cls.source = _single_block(_code_blocks(cls.doc), "python")
        # ⚠️ `__name__` 必须显式给：`exec` **不会**自动注入它（不给的话走到
        # `if __name__ == "__main__":` 会 NameError）。给 `"__main__"` 是**有意的**：
        # 于是那段离线自检真的会跑，它的输出就是下面几条判据的读数。
        cls.namespace: dict = {"__name__": "__main__"}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exec(compile(cls.source, f"{TAKER_API_MD} §10.2", "exec"), cls.namespace)
        cls.main_output = captured.getvalue().strip()


class ExecutableSnippetTests(DocPythonSnippetTestCase):
    """⭐ 第二层：文档里的 Python 示例**真的跑**，并与 §10.1 的字面值对拍。"""

    def test_the_document_exists(self):
        self.assertTrue(TAKER_API_MD.is_file(), f"缺文件：{TAKER_API_MD}")

    def test_the_python_snippet_is_not_pseudocode(self):
        """结构判据：它得是一段能独立跑的脚本（⛔ 不是"示意一下"的伪代码）。"""
        for required in ("import json", "import urllib.request", "def fetch_token",
                         "def call_api", "def list_hall", "def claim_order",
                         "def my_orders", "def main"):
            with self.subTest(required=required):
                self.assertIn(required, self.source)
        self.assertGreaterEqual(
            len(self.source.splitlines()), 120, "示例太短，不像可直接复制的一整段"
        )
        self.assertNotIn("TODO", self.source)

    def test_the_snippet_defines_the_documented_entry_points(self):
        for name in SNIPPET_REQUIRED_NAMES:
            with self.subTest(name=name):
                self.assertTrue(callable(self.namespace[name]), f"示例里没有可调用的 {name}")

    def test_the_main_self_check_prints_exactly_the_documented_lines(self):
        """⭐ 直接跑出来的读数：文档那段 `__main__` 打印的必须是 §10.1 那三行。"""
        self.assertEqual(tuple(self.main_output.splitlines()), EXPECTED_MAIN_OUTPUT)

    def test_the_documented_expected_output_is_word_for_word_in_the_document(self):
        """对照：那三行**同时**写在文档里（⛔ 不是只写在测试里）。"""
        block = _fenced_literal(self.doc, "**期望输出**")
        self.assertEqual(tuple(block.splitlines()), EXPECTED_MAIN_OUTPUT)

    def test_the_document_fixtures_match_this_file(self):
        for literal in (FIXTURE_CLIENT_ID, FIXTURE_CLIENT_SECRET, FIXTURE_ORDER_NO,
                        FIXTURE_UPDATED_SINCE, str(FIXTURE_EXPIRES_IN)):
            with self.subTest(literal=literal):
                self.assertIn(literal, self.doc)

    def test_the_declared_expires_in_is_the_only_number_in_the_document(self):
        """⭐ 第三层对拍：文档里声明的 `expires_in` 必须**恰好**是那一个值。
        ⚠️ 判据取**全部**出现处（`findall` 而不是 `search`）：只改其中一处也会红。"""
        declared = set(re.findall(r'"expires_in":\s*(\d+)', self.doc))
        self.assertEqual(
            declared, {str(FIXTURE_EXPIRES_IN)},
            f"文档里声明的 expires_in 与契约不一致：{declared}",
        )

    def test_it_is_not_a_human_tutorial(self):
        """⛔ 业务方要的是「喂给 AI 的一份文档」⇒ 不许写成欢迎页 / 申请向导那套。"""
        text = "\n".join(
            line for line in self.doc.splitlines() if not line.lstrip().startswith(">")
        )
        for forbidden in ("欢迎使用", "点击这里申请", "立即注册", "限时优惠"):
            with self.subTest(forbidden=forbidden):
                self.assertFalse(forbidden in text, f"正文里出现了营销 / 教程用语：{forbidden}")

    def test_it_is_a_technical_contract_not_a_tutorial(self):
        """对照：这份文档**确实是**技术契约（否则上一条"没有营销词"可由"文档是空的"平凡成立）。"""
        self.assertIn("Authorization: Bearer <令牌>", self.doc)
        self.assertIn("业务失败也是 HTTP 200", self.doc)
        self.assertIn("/api/taker/oauth/token", self.doc)

    def test_internal_section_references_all_resolve(self):
        """⭐ 文档内部的 `§x.y` 引用必须指到真实存在的小节（⛔ 不留断引用）。"""
        headings = set(re.findall(r"^#{2,4}\s+(\d+(?:\.\d+)*)", self.doc, re.MULTILINE))
        self.assertGreater(len(headings), 15, "小节号没抽到（抽取面坏了），这条判据不产出结论")
        referenced = set(re.findall(r"§(\d+(?:\.\d+)*)", self.doc))
        broken = sorted(referenced - headings)
        self.assertEqual(broken, [], f"这些章节引用在文档里不存在：{broken}")

    def test_the_forbidden_things_are_gone(self):
        """⛔ 写了就是假话的那几样（推送 / CORS / refresh_token）不许作为**承诺**出现。
        ⚠️ 判据搜**主词本身**（`AGENTS.md` §5-C：只搜派生短语会让"Expected: 空"成为**假保证**）。⚠️ **豁免显式枚举**在 `ALLOWED_CONTEXTS`（那些是否定句：说清"本侧没有这个东西"）：做法 = 把某个主词的**全部**豁免片段拼成一条 alternation，再要求每一个出现处都**落在**其中一段里——改一个字、或在别处再写一次，都会红（⛔ 不是"理由文本里含某个词"那种恒真后门）。"""
        for token in FORBIDDEN_TOKENS:
            fragments = ALLOWED_CONTEXTS.get(token, ())
            pattern = (
                re.compile("|".join(re.escape(f) for f in fragments)) if fragments else None
            )
            spans = (
                [(m.start(), m.end()) for m in pattern.finditer(self.doc)]
                if pattern is not None
                else []
            )
            for occurrence in re.finditer(re.escape(token), self.doc):
                start, end = occurrence.start(), occurrence.end()
                covered = any(a <= start and end <= b for a, b in spans)
                context = self.doc[max(0, start - 30) : end + 10]
                with self.subTest(token=token, at=start):
                    self.assertTrue(
                        covered,
                        f"「{token}」出现了，但不在任何一条显式豁免的字面片段里：…{context}…",
                    )

    def test_it_says_the_hall_is_not_pushed_and_polling_is_still_required(self):
        """⭐ **大厅不推 ＋ 必须轮询**仍在（回调推送那一章落地后这一半一个字没松动）。
        ⚠️ 文档加了回调推送（§12），但业务方同时定死「⛔ 大厅里来了新单**不推**」。判据钉两件事：① 文档里仍写着**必须轮询**（§0 总览那一行）；② 文档**明说大厅不推**。"""
        self.assertIn("必须**轮询**", self.doc)
        self.assertIn("大厅里来了新单不推", self.doc)
        # ⚠️ 判别力：把 §12.1 那句"大厅不推"删掉 ⇒ 本断言红（见任务报告的变异读数）。
        self.assertIn("大厅里来了新单，我们不推", self.doc)
        # 这两条钉的是文档里那句历史原话还在，不是「本侧没有推送」这个现状。
        self.assertIn("没有推送", self.doc)
        self.assertIn("改准（2026-10-09，第二期）", self.doc)

    def test_it_says_the_token_endpoint_body_is_not_the_envelope(self):
        """⭐ 「取令牌端点不是本仓信封」是第 2 条最容易踩的坑 ⇒ 必须写破。"""
        self.assertIn("⛔ 不是本仓那个 `code` / `msg` / `data` 信封", self.doc)

    def test_it_says_the_claim_answer_is_not_a_fact_query(self):
        """⭐ 「抢单应答只代表那一刻」必须写破（否则对端会拿它当事实查询）。"""
        self.assertIn("回放", self.doc)
        self.assertIn("事实以查单", self.doc)

    def test_it_says_keyword_searches_the_order_no_only(self):
        """⭐ `keyword` 只能按订单号搜 + 那条不变式（可搜字段 ⊆ 响应载荷）。"""
        self.assertIn("只能按订单号搜", self.doc)
        self.assertIn("可搜的字段必须 ⊆ 响应载荷的键集", self.doc)

    def test_it_says_the_hall_mirror_must_be_full_paged_and_set_compared(self):
        """⭐ 镜像同步的写法（全量翻页 + 按 `order_no` 集合对比）必须写破。"""
        self.assertIn("全量翻页", self.doc)
        self.assertIn("按 `order_no` 集合对比", self.doc)

    def test_it_says_the_contact_fields_arrive_only_after_claiming(self):
        """⭐ P2：客户联系方式**抢到之后**才给 —— 文档里必须有这句。"""
        self.assertIn("P2", self.doc)
        self.assertIn("抢到之后才有", self.doc)

    def test_it_says_completed_can_only_be_set_by_us(self):
        """⭐ P3：`COMPLETED` 只能由团团通验收 —— 文档里必须有这句。"""
        self.assertIn("只能由团团通这边验收", self.doc)
        self.assertIn("P3", self.doc)

    def test_it_says_the_token_has_no_refresh_and_rotation_kills_everything(self):
        self.assertIn("没有 refresh", self.doc)
        self.assertIn("轮换密钥", self.doc)
        self.assertIn("瞬间断电", self.doc)

    def test_it_says_the_browser_cannot_call_this_api(self):
        self.assertIn("必须在服务端调", self.doc)
        self.assertIn("没有 CORS", self.doc)

    def test_it_says_the_idempotency_key_is_mandatory_and_8_to_128(self):
        self.assertIn("Idempotency-Key", self.doc)
        self.assertIn("8–128", self.doc)

    def test_it_says_the_page_size_cap_is_100(self):
        self.assertIn("上限 100", self.doc)
        self.assertEqual(MAX_PAGE_SIZE, 100)

    def test_it_says_403_does_not_exist_on_the_public_face(self):
        """⚠️ 对外面**不装权限类** ⇒ 文档必须写破"不会返回 403"（否则对端会去猜权限）。"""
        self.assertIn("不返回 `403`", self.doc)
        self.assertIn("不装权限类", self.doc)

    def test_the_three_401_messages_are_listed_word_for_word(self):
        for message in EXPECTED_401_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message, self.doc)

    def test_the_401_messages_are_distinct(self):
        # 对照：三句若被写成同一句，"逐字列出"就没有意义了。
        self.assertEqual(len(set(EXPECTED_401_MESSAGES)), 3)

    def test_the_throttle_numbers_are_declared(self):
        """⚠️ 兜底闸的额度是"如实登记"的一部分：两族额度**不同**（本族 3000 / 300）。"""
        self.assertIn("3000 次 / 分钟 / 应用", self.doc)
        self.assertIn("每来源 IP 每 60 秒 300 次", self.doc)
        self.assertIn("不是配额", self.doc)

    def test_it_says_the_failure_gate_has_twenty_failures_per_minute(self):
        """⚠️ 按来源 IP 的失败闸（20 次 / 60 秒）是排障时**最容易误判**的一条 ⇒ 必须写破。"""
        self.assertIn("失败 20 次", self.doc)

    def test_it_says_we_do_not_fetch_the_url_you_give_us(self):
        """⛔ 「我们抓取你给的网址」不许写成承诺；正确的说法是"预签名直链，你自己 PUT"。"""
        self.assertIn("不抓取你给的网址", self.doc)
        self.assertIn("预签名直链", self.doc)

    def test_the_documented_claim_messages_are_the_real_ones(self):
        """⭐ §4.3 那两句是**实现里的字面值**，⛔ 不是我们另编的措辞。
        ⚠️ 判据取"实现源码里的字面量"（AST，⛔ 不 import `claim`：那是**领域层**模块，拉它进来只为读几个字符串不值得）⇒ 它红了就说明**领域层改了文案而文档没跟**。"""
        import ast

        import apps.business.claim as claim_module

        tree = ast.parse(Path(claim_module.__file__).read_text(encoding="utf-8"))
        literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    literals.add(first.value)
        self.assertGreater(len(literals), 3, "没抽到实现里的文案（抽取面坏了）")
        # ⚠️ **原样透传**给对端的那两句（`order_write_views` 的 else 支）⇒ 文档必须逐字有。
        for message in ("该单没有接单价，请联系客服", "你已被设置为禁止自助接单"):
            with self.subTest(message=message):
                self.assertIn(message, literals, f"实现里已经没有这句话了：{message}")
                self.assertIn(message, self.doc, f"文档里缺这句话：{message}")
        # ⚠️ 而 `1012` 那一句是**对外换过措辞**的（spec §5.4 要求写清"包含你自己刚才已经接过"）
        #    ⇒ 判据反过来：实现里那句**不应**出现在文档的对外文案里（出现了就是把内部口径
        #    当成了对外承诺），而本模块那一句必须在。
        self.assertIn("该订单已被别人接走", literals)
        self.assertIn("该订单已被接走（包含你自己刚才已经接过）", self.doc)

    def test_it_says_the_preview_url_is_short_lived(self):
        """⚠️ 图片直链是**短时效**的（默认 300 秒）⇒ 文档必须写破（否则对端会长期存档）。"""
        self.assertIn("300 秒", self.doc)


# ═════════════════════════════════════════════════════════════════════════════
# 第 3 层：与实现对拍（路径 / 键集 / 码表）
# ═════════════════════════════════════════════════════════════════════════════


class EndpointTableTests(SimpleTestCase):
    """§2 端点全表里的路径必须**恰好**对得上 `urls.py` 的真实路由集合。"""

    #: ⭐ **点名式**的期望集合（⛔ 不用 `toContain`）：多一条、少一条、改一个字都要红。14 条 `path()` = 对外 8 条（§2 那七行 ＋ images/ticket/）＋ 取令牌 1 条 ＋ 内部 5 条。⚠️ 写法 = **对外契约里的写法**（`<order_no>`、**不带**转换器名）。⭐⭐ **唯一出处**：`(urls.py 里的字面量, 对外契约里的写法)`——**按 urlconf 的顺序**。⚠️ 顺序也是判据的一部分（`assertEqual` 比的是元组）：乱序会红，逼人去看"谁改了顺序"。⚠️ 两列的差别只有**转换器名**（`<order_no:order_no>` vs 契约里的 `<order_no>`）。
#: ⛔⭐ 内部那 3 条：两条内部路由（推送台账 `apps/<int:pk>/deliveries/` 与手工重投 `…/deliveries/<int:delivery_pk>/retry/`，见 `docs/specs/2026-10-09-taker-callback-design.md` §7）与本表**同一次**登记（⛔ **不是放宽**：本表仍然是"**恰好**相等"的两向断言）；它们**不在文档 §2 的对外表里**（那张表只管 `/api/taker/v1/…` 六条对外端点）——是**内部**端点（会话认证 + 节点门），文档的 §1.2 / §12.9 有它们的说明。
#: ⛔⭐ 对外那 7 条：镜像单并进了打手 API 的大厅，接单侧同一次新增 `POST v1/hall/upstream/<int:pk>/claim/`（文档 §2 表的第 7 行 / §4.5），本表同一次补上这一条（⛔ **不是放宽**：仍是**恰好**相等的两向断言、顺序仍按 urlconf）。
    CONTRACTS = (
        ("oauth/token", "oauth/token"),
        ("v1/hall/", "v1/hall/"),
        ("v1/hall/<order_no:order_no>/claim/", "v1/hall/<order_no>/claim/"),
        ("v1/hall/upstream/<int:pk>/claim/", "v1/hall/upstream/<pk>/claim/"),
        (
            "v1/hall/peer/<int:pk>/claim/",
            "v1/hall/peer/<pk>/claim/",
        ),
        ("v1/my/orders/", "v1/my/orders/"),
        ("v1/orders/<order_no:order_no>/", "v1/orders/<order_no>/"),
        ("v1/orders/<order_no:order_no>/status/", "v1/orders/<order_no>/status/"),
        ("v1/orders/<order_no:order_no>/images/ticket/", "v1/orders/<order_no>/images/ticket/"),
        ("v1/orders/<order_no:order_no>/images/", "v1/orders/<order_no>/images/"),
        ("apps/", "apps/"),
        ("apps/<int:pk>/", "apps/<pk>/"),
        ("apps/<int:pk>/rotate-secret/", "apps/<pk>/rotate-secret/"),
        ("apps/<int:pk>/deliveries/", "apps/<pk>/deliveries/"),
        (
            "apps/<int:pk>/deliveries/<int:delivery_pk>/retry/",
            "apps/<pk>/deliveries/<delivery_pk>/retry/",
        ),
    )

    #: `urls.py` 的 15 条 `path()`（**字面量**，带转换器名）。
    #: ⚠️ 15 条 = 对外 9 条（§2 那八行 ＋ `images/ticket/`）＋ 取令牌 1 条 ＋ 内部 5 条。
    EXPECTED_LITERAL_PATHS = tuple(literal for literal, _doc in CONTRACTS)

    #: 上面那 15 条**去掉转换器名**之后 = 文档契约里那一套写法。
    EXPECTED_CALL_PATHS = tuple(doc for _literal, doc in CONTRACTS)

    #: urlconf 里 `v1/`（= 对外）那九条：⛔ 不许长出第十条对外路径。
    EXPECTED_V1_PATHS = tuple(doc for _literal, doc in CONTRACTS if _literal.startswith("v1/"))

    def setUp(self):
        self.text = _read_doc()

    def test_the_urlconf_paths_are_exactly_the_known_set(self):
        """`urls.py` 的 15 条 `path()` **恰好**是这一串（多一条就要有人来解释它）。"""
        # ⚠️ 两件事分开钉：① `urls.py` 的**字面量**（带转换器名）恰好是那一串；
        #    ② 去掉转换器名之后 = 文档契约里那套写法。
        self.assertEqual(
            _urlconf_literals_and_templates(),
            tuple(zip(self.EXPECTED_LITERAL_PATHS, self.EXPECTED_CALL_PATHS)),
        )
        self.assertEqual(_urlconf_call_paths(), self.EXPECTED_CALL_PATHS)

    def test_the_documented_table_paths_are_exactly_the_seven_rows(self):
        """⭐ 端点全表抽出来的编号行**恰好**是那八行（路径去重后 = 9 条，见模块常量的注释）。"""
        table = _markdown_rows(_table_block(self.text, "## 2. 端点全表"))
        header = table[0]
        self.assertEqual(header[:3], ["#", "方法", "路径"])
        rows = table[1:]
        # 编号 1–8；第 6 号有**三行**（同一族的三步：列表 / 取票 / 认领）；
        # 第 7 号 = 接镜像单（D-0296）；第 8 号 = 接团团通镜像单（D-0303）。
        self.assertEqual(
            [row[0] for row in rows], ["1", "2", "3", "4", "5", "6", "6", "6", "7", "8"]
        )
        found: list = []
        for row in rows:
            self.assertEqual(len(row), len(header), f"这一行列数不齐（表坏了？）：{row}")
            path = row[2].strip("`")
            self.assertTrue(
                path.startswith("/api/taker/"), f"第 3 格不是路径：{row}"
            )
            found.append(path)
        self.assertEqual(sorted(set(found)), sorted(DOCUMENTED_PATHS))

    def test_every_documented_path_is_in_the_urlconf_word_for_word(self):
        """⭐ 文档那九条路径 ≈ urlconf 的 `v1/…` 九条；两条 `images/` 路径都要在表里。"""
        documented = [p[len("/api/taker/") :] for p in DOCUMENTED_PATHS]
        # ⚠️ `EXPECTED_CALL_PATHS` 就是"去掉转换器名"的那一套（见 `CONTRACTS`）。
        self.assertEqual(
            sorted(documented),
            sorted(p for p in self.EXPECTED_CALL_PATHS if p.startswith("v1/")),
        )
        # 编号行里 `images/` 一条路径两个方法（`GET` 与 `POST` 同住一个视图类）
        # ⇒ 去重后九条。
        self.assertEqual(len(set(documented)), 9)
        self.assertIn("/api/taker/v1/orders/<order_no>/images/ticket/", self.text)
        self.assertIn("/api/taker/v1/orders/<order_no>/images/", self.text)

    def test_the_method_path_pairs_are_all_in_the_table(self):
        """⭐ 方法与路径**成对**在场（⛔ 不是"路径在就算"：`GET` 写成 `POST` 是静默契约变更）。"""
        table = _table_block(self.text, "## 2. 端点全表")
        for method, path in DOCUMENTED_METHOD_PATHS:
            with self.subTest(method=method, path=path):
                self.assertIn(f"| `{method}` | `{path}` |", table)

    def test_the_token_path_has_no_trailing_slash_spelling(self):
        """⚠️ 取令牌那条**没有**尾斜杠：另一种拼写⛔ 不许出现在文档里（写了会 404）。"""
        self.assertIn("`POST /api/taker/oauth/token`", self.text)
        self.assertNotIn("/api/taker/oauth/token/", self.text)

    def test_no_tenth_public_path_exists_in_the_urlconf(self):
        """⭐ 反向：urlconf 里 `v1/`（对外）**恰好**那九条（§2 那八个编号行 ＋ `images/ticket/`）。
        （原名 `test_no_ninth_…`：D-0303 起对外第九条 `v1/hall/peer/<pk>/claim/` 在册，本判据顺延为「不许长出第十条」。）"""
        v1_paths = tuple(p for p in _urlconf_call_paths() if p.startswith("v1/"))
        self.assertEqual(v1_paths, self.EXPECTED_V1_PATHS)
        self.assertEqual(len(v1_paths), 9)


#: 打手侧**对外**端点的载体模块（判据：`resolve()` 出来的函数必须住在这几个之一）。
#: ⚠️ 字面量（`E-0024`）：⛔ 不从被测模块 import —— 那会让"视图被搬到别的 app"这件事
#: 静默通过。
TAKER_API_VIEW_MODULES = (
    "apps.takerapi.api.hall_views",
    "apps.takerapi.api.order_write_views",
    "apps.takerapi.api.my_orders_views",
    "apps.takerapi.api.order_status_views",
    "apps.takerapi.api.order_images_views",
)


class EndpointRuntimePathTests(TenantTestCase):
    """⭐ 文档那几条路径在**真实 URLconf** 上逐条解析得到（并钉住"没有第七条对外路径"）。"""

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain

    def test_every_documented_path_resolves_into_the_taker_urlconf(self):
        from django.urls import resolve

        for template in DOCUMENTED_PATHS:
            # ⚠️ `<pk>`（接镜像单，D-0296）同样按**合成值**落实：`resolve()` 只走路由匹配、
            #    不进视图，`<int:pk>` 转换器只认数字 ⇒ 原样的 `<pk>` 会 404（那是抽取面坏了，
            #    不是路由不在）。`"7"` 是合成 pk，与夹具单号同一纪律（⛔ 不是真实行）。
            concrete = template.replace("<order_no>", FIXTURE_ORDER_NO).replace("<pk>", "7")
            with self.subTest(template=template):
                match = resolve(concrete)
                # ⚠️ 判据**不是** `match.namespace`（Django 里它装的是路由器实例的 namespace 属性，本仓实测是空串；本仓路由模块也没有 `app_name`）
                #    ⇒ 打在**真的落到了哪个模块的视图上**：`match.func.__module__` 必须是打手侧那五个对外模块之一（⛔ 不是上家侧 / 内网的视图）。
                self.assertIn(match.func.__module__, TAKER_API_VIEW_MODULES)

    def test_the_token_path_has_no_trailing_slash_variant(self):
        """⚠️ 取令牌那条拼错（多一个斜杠）就 404 ⇒ 文档里那一种拼写是**唯一**正确的。"""
        from django.urls import Resolver404, resolve

        self.assertTrue(callable(resolve("/api/taker/oauth/token").func))
        with self.assertRaises(Resolver404):
            resolve("/api/taker/oauth/token/")

    def test_ghost_public_paths_do_not_resolve(self):
        """⭐ 反向：几条"看起来像"但文档里没有的路径必须 404（⛔ 不是"碰巧也能通"）。"""
        from django.urls import Resolver404, resolve

        for ghost in (
            "/api/taker/v1/orders/",
            "/api/taker/v1/hall/%s/cancel/" % FIXTURE_ORDER_NO,
            "/api/taker/v1/orders/%s/images/tickets/" % FIXTURE_ORDER_NO,
            "/api/taker/v1/orders/%s/claim/" % FIXTURE_ORDER_NO,
        ):
            with self.subTest(ghost=ghost):
                with self.assertRaises(Resolver404):
                    resolve(ghost)


class DocPathAndKeySetTests(SimpleTestCase):
    """§3.2 / §5.3 / §7.2 的**键集**必须与文档逐条对得上（字面量，⛔ 不 import 常量）。"""

    def setUp(self):
        self.text = _read_doc()

    def _first_column_keys(self, heading: str) -> list:
        """标题之后那张表的**第一列**（`| `键` | 类型 | 说明 |` ⇒ 键名，去掉反引号）。"""
        rows = _markdown_rows(_table_block(self.text, heading))
        self.assertGreaterEqual(len(rows), 2, f"{heading} 之后没有抽到表")
        width = len(rows[0])
        keys: list = []
        for row in rows[1:]:
            if len(row) != width:
                continue
            keys.append(row[0].strip("`"))
        self.assertEqual(len(keys), width and len(rows) - 1, "有行被列数不齐筛掉了（表坏了？）")
        return keys

    def test_the_hall_row_keys_are_exactly_the_eleven(self):
        keys = self._first_column_keys("### 3.2 一行**恰好 11 个键**")
        self.assertEqual(sorted(keys), sorted(HALL_ROW_KEYS))
        self.assertEqual(len(keys), 11)

    def test_the_my_order_row_keys_are_exactly_the_eighteen(self):
        """⭐ 我的单那一行 = §3.2 的 11 个 ＋ §5.3 表里那 7 个 = **恰好 18**。"""
        extra = self._first_column_keys("### 5.3 ⭐ 行 = **恰好 18 个键**（§3.2 那 11 个 **＋** 这 7 个）")
        self.assertEqual(sorted(extra), sorted(MY_ORDER_EXTRA_KEYS))
        self.assertEqual(len(HALL_ROW_KEYS | MY_ORDER_EXTRA_KEYS), 18)
        self.assertIn("恰好 18 个键", self.text)

    def test_the_image_row_keys_are_exactly_the_seven(self):
        found = {
            token
            for token in _BACKTICK_RE.findall(_table_block(self.text, "### 7.2 入参与应答") + "\n" + self.text.split("### 7.2 入参与应答", 1)[1].split("### 7.3", 1)[0])
            if token in IMAGE_ROW_KEYS
        }
        self.assertEqual(found, IMAGE_ROW_KEYS)

    def test_the_forbidden_hall_keys_are_named_in_the_document(self):
        """⛔ 大厅行**不给**的那几类必须在文档里显式点名（否则对端会以为漏了）。"""
        for key in ("customer_phone", "customer_ww", "remark", "sales_agent",
                    "net_received", "refund_fee", "mirror_pk"):
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", self.text)

    def test_the_status_whitelist_is_named_in_the_document(self):
        for value in ("PENDING_REVIEW", "ISSUE", "PAUSED", "COMPLETED"):
            with self.subTest(value=value):
                self.assertIn(value, self.text)

    def test_the_hall_filters_are_named_in_the_document(self):
        """§3.1 的**六个筛选** ＋ 分页两个都要逐字在场。"""
        for name in ("game", "server", "project_text", "keyword", "price_min", "price_max",
                     "page", "page_size"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", self.text)


class ErrorCodeTableTests(SimpleTestCase):
    """§8.2 的码表必须**恰好**是已知集合（⛔ 不新开码、⛔ 不漏、⛔ 不混进别的码）。"""

    def setUp(self):
        self.text = _read_doc()

    def _rows_after(self, marker: str) -> list:
        """`marker` 之后那一张**数据行**（首行是表头，⛔ 不算契约的一行）。"""
        rows = _markdown_rows(_table_block(self.text, marker))
        self.assertGreaterEqual(len(rows), 2, f"{marker} 之后没有抽到表")
        return rows[1:]

    def test_the_envelope_codes_are_exactly_the_promised_ones(self):
        rows = self._rows_after("**信封那一侧**（除取令牌端点外的每一条端点）")
        found = {row[0].strip("`") for row in rows if row[0].startswith("`")}
        self.assertEqual(found, EXPECTED_ENVELOPE_CODES)
        # ⛔ 判别力：多一个码会红 —— 那就是"新开码"。
        self.assertNotIn("1003", found)
        self.assertNotIn("1014", found)
        self.assertNotIn("1024", found)

    def test_the_oauth2_error_values_are_exactly_the_three(self):
        rows = self._rows_after("**取令牌端点那一侧**（**裸 OAuth2 体**")
        found = {row[0].strip("`") for row in rows if row[0].startswith("`")}
        self.assertEqual(found, set(EXPECTED_OAUTH2_ERRORS))
        # ⛔ 判别力：把 `temporarily_unavailable` 从表里删掉、或加一个别的 `error` 值都会红。
        self.assertNotIn("invalid_grant", found)

    def test_every_documented_code_is_explained_with_a_human_sentence(self):
        """⚠️ 每一行都要有"什么情况 + 一句人话"（⛔ 不是只给一个数字）。"""
        rows = [row for row in self._rows_after("**信封那一侧**（除取令牌端点外的每一条端点）") if row[0].startswith("`")]
        self.assertEqual(len(rows), len(EXPECTED_ENVELOPE_CODES))
        for row in rows:
            code = row[0].strip("`")
            with self.subTest(code=code):
                self.assertGreaterEqual(
                    len(row), 4, f"{code} 那一行没有「什么情况 / 一句人话」两列：{row}"
                )
                self.assertTrue(row[3].strip(), f"{code} 那一行的人话是空的")


# ═════════════════════════════════════════════════════════════════════════════
# 第二期（§12 回调推送）：字面面值 ＋ 那段验签代码**真跑**并与实现对拍
# ═════════════════════════════════════════════════════════════════════════════


class CallbackPushContractTests(SimpleTestCase):
    """⭐ 文档 §12「回调推送」的**逐条契约**必须逐字在场（第三方拿不到别的说明）。
    判据：① 四类事件名逐字；② 8 个业务键 / `timestamp`·`sign` / `reason` 三张表的**第一列恰好**；③ 三个头逐字；④ 待签串与规范化的三条 flag 逐字；⑤ 2xx 才算成功；⑥ 退避六档 ＋「第 7 次才终态」；⑦ 至少一次 ＋ 按 `delivery_id` 去重；⑧ 预签 300 秒；⑨ 地址要求（https ＋ 公网 ＋ 200 字符 ＋ 保存时/投递前各一次）；⑩ 那几条**如实说明**（不在事务里 / 延迟 ≈ 10 秒 / 大厅不推 / 两事件可能同时到）；⑪ 怎么配置与手工重投。
    ⚠️ 它**不**证明文档与实现一致——那一半由 `CallbackSignatureSnippetTests` 的对拍管。⚠️ 每条断言都配了"哪一处改动会让它红"，且**不给豁免后门**（⛔ 不按"理由文本含某个词"放行）。"""

    def setUp(self):
        self.text = _read_doc()

    def _first_column(self, heading: str) -> list:
        """`heading` 之后那张表的**第一列**（去掉反引号）——表头那一行不算。"""
        rows = _markdown_rows(_table_block(self.text, heading))
        self.assertGreaterEqual(len(rows), 2, f"{heading} 之后没有抽到表")
        width = len(rows[0])
        keys = [row[0].strip("`") for row in rows[1:]]
        self.assertEqual(len(keys), width and len(rows) - 1, "有行被列数不齐筛掉了（表坏了？）")
        return keys

    # ── ① 事件集合 ──────────────────────────────────────────────────────
    def test_the_four_events_are_named_word_for_word(self):
        """四类事件名逐字（⚠️ 少一类 / 改一个字符都要红）。"""
        for event in EXPECTED_CALLBACK_EVENTS:
            with self.subTest(event=event):
                self.assertIn(event, self.text)
        # ⚠️ 判别力：第 4 类是本侧独有的 ⇒ 它必须与上家侧那三类**并列**出现。
        self.assertIn("`order.reassigned`", self.text)

    def test_it_says_the_recipient_is_only_the_current_taker(self):
        """⭐ 收件人语义（与上家侧最大的差别）必须**写破**：只推「他当前接手的单」。"""
        self.assertIn("绑定账号 == 这张单的当前接手人", self.text)
        self.assertIn("一个收件人都没有", self.text)
        self.assertIn("孤儿单", self.text)

    def test_it_says_the_hall_is_not_pushed(self):
        """⛔ 大厅新单不推（业务方裁定）—— 这是对端最容易误解的一条。"""
        self.assertIn("大厅里来了新单不推", self.text)
        self.assertIn("仍然只能**轮询**", self.text)

    def test_it_says_reassigned_and_deleted_may_arrive_together(self):
        """⚠️ 同一张单被删时两条事件**可能同时到**，且语义不同（⛔ 不是重复）。"""
        self.assertIn("可能同时到", self.text)
        self.assertIn("不是重复", self.text)

    # ── ② 报文字段 ──────────────────────────────────────────────────────
    def test_the_generic_business_keys_are_exactly_the_eight(self):
        keys = self._first_column("#### 12.2.1 业务字段（8 个键")
        self.assertEqual(sorted(keys), sorted(EXPECTED_CALLBACK_GENERIC_KEYS))
        self.assertEqual(len(keys), 8)

    def test_the_delivery_time_keys_are_exactly_timestamp_and_sign(self):
        keys = self._first_column("#### 12.2.2 投递时现算的两键")
        self.assertEqual(sorted(keys), sorted(EXPECTED_CALLBACK_DELIVERY_KEYS))

    def test_the_reassigned_reason_key_and_its_three_values_are_named(self):
        keys = self._first_column("#### 12.2.3 `order.reassigned` 多的那一键")
        self.assertEqual(keys, [EXPECTED_REASSIGNED_KEY])
        for reason in EXPECTED_REASSIGNED_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(f"`{reason}`", self.text)
        # ⚠️ 判别力：三个取值**恰好**三个（⛔ 不许写成"若干"）；`taker_removed` 是全量里最长的那个。
        self.assertEqual(len(set(EXPECTED_REASSIGNED_REASONS)), 3)

    # ── ③ 签名与三个头 ──────────────────────────────────────────────────
    def test_the_three_headers_are_word_for_word(self):
        for header in EXPECTED_CALLBACK_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, self.text)

    def test_the_signed_string_is_word_for_word(self):
        """⭐ 待签串**逐字**（顺序换一下就全验不过）—— 判据是那三段的固定写法。"""
        self.assertIn('f"{timestamp}\\n{delivery_id}\\n"', self.text)
        self.assertIn("sha256(canonical).hexdigest()", self.text)

    def test_the_canonicalisation_three_flags_are_all_declared(self):
        """⚠️ 三条缺一不可（少一条 ⇒ 对端复算出的摘要是另一个值，症状是"全都验不过"）。"""
        self.assertIn("ensure_ascii=False", self.text)
        self.assertIn("sort_keys=True", self.text)
        self.assertIn('separators=(",", ":")', self.text)

    # ── ④ 回执 / 退避 / 至少一次 / 图片 ─────────────────────────────────
    def test_the_receipt_rule_is_2xx_only(self):
        self.assertIn("只有 HTTP 2xx 算成功", self.text)

    def test_the_retry_backoff_ladder_is_word_for_word(self):
        """⭐ 退避六档 ＋「共 6 次机会」逐字（第三方据此安排重试与告警）。"""
        ladder = " / ".join(EXPECTED_BACKOFF_LADDER)
        self.assertIn(f"重试退避 {ladder}，共 6 次机会", self.text)
        rows = _markdown_rows(_table_block(self.text, f"重试退避 {ladder}"))
        # 表里那 7 行 = 首发 + 六档（⚠️ 少一档 / 多一档都要红）。
        self.assertEqual(len(rows), 8, f"退避表应恰好 7 行数据 + 1 行表头：{rows}")
        cells = " ".join(row[1] for row in rows[1:])
        for label in EXPECTED_BACKOFF_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, cells)

    def test_the_seventh_attempt_is_the_terminal_one(self):
        """⭐ 终态判据是"**第 7 次**才落 `failed`"（写"第 6 次"就是把最后一档吃掉）。"""
        self.assertIn("第 7 次仍然失败", self.text)
        self.assertIn("才落终态", self.text)

    def test_the_at_least_once_semantics_and_dedup_instruction_are_declared(self):
        """⭐ 「至少一次」＋「按 `delivery_id` 去重」是这一节**最重要**的两句。"""
        self.assertIn("至少一次", self.text)
        self.assertIn("按 `delivery_id` 去重", self.text)
        # ⚠️ 重复的**两条来源**都要写破：自动重投 ＋ 手工重投（只写一条就漏了一半）。
        self.assertIn("手工重投", self.text)
        self.assertIn("对方再收到一次", self.text)

    def test_the_presigned_image_url_lifetime_is_declared(self):
        """⚠️ `images` 里给的是**投递那一刻生成**的预签名 URL（URL 稳定化线，2026-11-09：
        生产对象存储整点对齐 1–2 小时；本机/测试 300 秒）。过期即失效的口径不变。"""
        self.assertIn("有效期 1–2 小时", self.text)
        self.assertIn("别按 url 去重", self.text)

    def test_the_callback_url_requirements_are_declared(self):
        """⭐ 地址要求：仅 https ＋ 必须公网 ＋ 不许带凭据 ＋ 保存时/投递前各一次 ＋ 200 字符。"""
        self.assertIn("只允许 `https`", self.text)
        self.assertIn("不跟随重定向", self.text)
        self.assertIn("回环", self.text)
        self.assertIn("链路本地", self.text)
        self.assertIn("不许带用户名 / 密码", self.text)
        self.assertIn("**保存时**与**每次投递前**各跑一次", self.text)
        self.assertIn("上限 200 个字符", self.text)

    def test_the_honest_limitations_are_declared(self):
        """⚠️ 这几条是**如实说明**（少一条就会让对端以为推送是事务性的 / 实时的）。"""
        self.assertIn("投递不是事务的一部分", self.text)
        self.assertIn("推送延迟 ≈ 10 秒", self.text)
        self.assertIn("你应当停止在该单上继续干活", self.text)
        self.assertIn("最终仍以查单（§5.2）为准", self.text)

    def test_it_says_how_to_configure_and_retry_by_hand(self):
        """⭐ 配置面（页面 ＋ 内部端点）必须写破，否则第三方不知道要找租户开什么。"""
        self.assertIn("【开放平台】", self.text)
        self.assertIn("打手对接", self.text)
        self.assertIn("callback_url", self.text)
        self.assertIn("callback_state", self.text)
        self.assertIn("手工重投只对 `failed` 的行开放", self.text)


class CallbackSignatureSnippetTests(SimpleTestCase):
    """⭐⭐ **真跑**：把文档 §12.3 那段验签代码抽出来跑，并与**本仓实现**对拍。
    ⚠️ 这一层是**两路来源**：一路读**文档**（那段代码 ＋ 四行值），一路读**实现**（`apps.openapi.outbound` 的 `canonical_json` / `compute_signature` / `build_signed_request`）⇒ 任一侧单独改都会红（⛔ 不是自指：⛔ 文档不从实现取值、⛔ 实现也不读文档）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = _read_doc()
        cls.source = _fenced_literal(cls.doc, CALLBACK_SNIPPET_MARKER)
        # ⚠️ `__name__` 给 `"__main__"` 是**有意的**：那段自检真的会跑，它的输出就是下一条判据。
        cls.namespace: dict = {"__name__": "__main__"}
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            exec(compile(cls.source, f"{TAKER_API_MD} §12.3", "exec"), cls.namespace)
        cls.self_check_output = captured.getvalue().strip()

    # ── ① 那段代码**真的跑** ────────────────────────────────────────────
    def test_the_snippet_is_not_pseudocode(self):
        for required in ("import hashlib", "import hmac", "import json",
                         "def canonical_json", "def sign_for", "def verify_callback"):
            with self.subTest(required=required):
                self.assertIn(required, self.source)
        self.assertNotIn("TODO", self.source)

    def test_the_snippet_defines_the_documented_entry_points(self):
        for name in CALLBACK_SNIPPET_REQUIRED_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, self.namespace)

    def test_the_self_check_prints_exactly_the_documented_lines(self):
        """⭐ 直接跑出来的读数：文档那段 `__main__` 打印的必须是本节那两行。"""
        self.assertEqual(
            tuple(self.self_check_output.splitlines()), EXPECTED_VERIFY_SELF_CHECK_OUTPUT
        )

    def test_the_documented_self_check_output_is_word_for_word_in_the_document(self):
        """对照：那两行**同时**写在文档里（⛔ 不是只写在测试里）。"""
        block = _fenced_literal(self.doc, CALLBACK_SELF_CHECK_MARKER)
        self.assertEqual(tuple(block.splitlines()), EXPECTED_VERIFY_SELF_CHECK_OUTPUT)

    # ── ② 文档那四行值（字面量，⛔ 不从实现取）──────────────────────────
    def test_the_documented_example_payload_is_the_nine_key_one(self):
        """⭐ §12.2 那个示例报文：**恰好 9 个键**（8 个业务键去掉 `images` ＋ 两键）。"""
        payload = json.loads(_fenced_literal(self.doc, CALLBACK_EXAMPLE_PAYLOAD_MARKER))
        self.assertEqual(set(payload), set(EXPECTED_CALLBACK_EXAMPLE_KEYS))
        self.assertEqual(payload["delivery_id"], FIXTURE_DELIVERY_ID)
        self.assertEqual(payload["source_order_no"], FIXTURE_SOURCE_ORDER_NO)
        self.assertEqual(payload["timestamp"], FIXTURE_CALLBACK_TIMESTAMP)
        # ⚠️ 两处示例（§12.2 的 JSON 与 §12.3 的代码夹具）必须是**同一份**：改一处就红。
        self.assertEqual(self.namespace["EXAMPLE_PAYLOAD"], payload)

    def test_the_documented_canonical_bytes_are_word_for_word(self):
        block = _fenced_literal(self.doc, "规范化之后的字节（`ensure_ascii=False")
        self.assertEqual(block, EXPECTED_CALLBACK_CANONICAL)

    def test_the_documented_sha256_is_word_for_word(self):
        block = _fenced_literal(self.doc, "`sha256(规范化).hexdigest()`")
        self.assertEqual(block, EXPECTED_CALLBACK_SHA256)

    def test_the_documented_expected_signature_is_word_for_word(self):
        block = _fenced_literal(self.doc, "⭐ **期望签名**（`sign`，小写 hex）")
        self.assertEqual(block, EXPECTED_CALLBACK_SIGNATURE)

    # ── ③ 与**实现**对拍 ────────────────────────────────────────────────
    def test_the_documented_payload_and_signature_match_the_production_implementation(self):
        """⭐⭐ 承重的一条：文档那四行值与实现算出来的**逐字相等**。
        ⚠️ 实现那一侧是**当场算的**（⛔ 不是把文档里的常量抄进代码再比——那是自指）。"""
        from apps.openapi import outbound

        payload = json.loads(_fenced_literal(self.doc, CALLBACK_EXAMPLE_PAYLOAD_MARKER))
        body_without_sign = {key: value for key, value in payload.items() if key != "sign"}
        canonical = outbound.canonical_json(body_without_sign)
        self.assertEqual(canonical.decode("utf-8"), EXPECTED_CALLBACK_CANONICAL)
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), EXPECTED_CALLBACK_SHA256)
        signature = outbound.compute_signature(
            secret=FIXTURE_CALLBACK_SECRET,
            timestamp=FIXTURE_CALLBACK_TIMESTAMP,
            delivery_id=FIXTURE_DELIVERY_ID,
            body_without_sign=body_without_sign,
        )
        self.assertEqual(signature, EXPECTED_CALLBACK_SIGNATURE)
        # 三个头（含 `Content-Type`）与文档那三行逐字一致 —— 连同"签名盖住 timestamp"。
        body_bytes, headers = outbound.build_signed_request(
            body_without_sign,
            secret=FIXTURE_CALLBACK_SECRET,
            timestamp=FIXTURE_CALLBACK_TIMESTAMP,
        )
        self.assertEqual(headers["X-TT-Signature"], EXPECTED_CALLBACK_SIGNATURE)
        self.assertEqual(headers["X-TT-Delivery"], FIXTURE_DELIVERY_ID)
        self.assertEqual(headers["X-TT-Timestamp"], str(FIXTURE_CALLBACK_TIMESTAMP))
        self.assertEqual(json.loads(body_bytes), payload)
        # ⭐ 文档那段代码算出来的签名也必须等于实现算出来的那一颗（两路来源在这里合流）。
        self.assertEqual(
            self.namespace["sign_for"](
                body_without_sign,
                secret=FIXTURE_CALLBACK_SECRET,
                timestamp=FIXTURE_CALLBACK_TIMESTAMP,
                delivery_id=FIXTURE_DELIVERY_ID,
            ),
            signature,
        )

    # ── ④ 那段代码**真的在验签**（能红）────────────────────────────────
    def test_the_snippet_verifies_the_example_and_rejects_a_tampered_one(self):
        """⭐ 判别力：拿示例验得过、把报文改一个字节就**必须**抛 —— 否则那段代码是装饰。"""
        payload = json.loads(_fenced_literal(self.doc, CALLBACK_EXAMPLE_PAYLOAD_MARKER))
        headers = dict(self.namespace["EXAMPLE_HEADERS"])
        verify = self.namespace["verify_callback"]
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        parsed = verify(raw, headers, FIXTURE_CALLBACK_SECRET)
        self.assertEqual(parsed["order_no"], payload["order_no"])
        self.assertNotIn("sign", parsed, "验签函数应当把 `sign` 摘掉再回")
        tampered = {**payload, "status": "COMPLETED"}
        with self.assertRaises(ValueError):
            verify(json.dumps(tampered, ensure_ascii=False).encode("utf-8"),
                   headers, FIXTURE_CALLBACK_SECRET)
        # ⚠️ 对照：密钥错也**必须**抛（⛔ 不是"什么都没验"）。
        with self.assertRaises(ValueError):
            verify(raw, headers, "another-synthetic-secret-0002")
        # ⚠️ 对照：三个头缺一个会 `KeyError`（文档 §12.3 第 1 步要求逐个校验它们在场）。
        with self.assertRaises(KeyError):
            verify(raw, {"X-TT-Timestamp": headers["X-TT-Timestamp"]}, FIXTURE_CALLBACK_SECRET)


# ═════════════════════════════════════════════════════════════════════════════
# ⭐⭐ 第 2 层（真跑）：把文档那段示例**对着真实的打手侧端点**跑一遍
# ═════════════════════════════════════════════════════════════════════════════

#: 来源 IP 取文档保留段（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.77"

#: 该账号持的节点（字面量，`E-0024`）：`grab_hall.take_order` ⇒ 抢单那条边（`WAITING_GRAB → IN_PROGRESS`，`order_status.py:194`）；
#: `order.change_status` ⇒ 抢单那条边**也**认它，且 `IN_PROGRESS → PENDING_REVIEW` 认它。⚠️ 两个都给，示例那条链才走得完（而它们与"账号节点被改坏"这件事互为判据）。
TAKE_ORDER_NODE = "grab_hall.take_order"
CHANGE_STATUS_NODE = "order.change_status"
GROUP_NAME = "代练员"

#: 本文件建的那个打手账号 / 应用的名字（合成值）。
TAKER_USERNAME = "docexample-taker"
APP_NAME = "文档示例应用"

#: 夹具只建**一张**厅单 ⇒ 大厅与我的单的 `count` 都必须是 1。
EXPECTED_HALL_COUNT = 1

#: 状态白名单里那一档的中文名（文档 §6.2 那张表）。
PENDING_REVIEW_LABEL = "待验收"


class _DocClientResponse:
    """把 Django 测试客户端的响应包成 urllib 那种"响应对象"（文档代码只用 `read()`）。"""

    def __init__(self, response):
        self.status_code = response.status_code
        self._body = response.content

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _doc_client_opener(client):
    """⭐ 把文档代码发出去的**真实 urllib 请求**翻成一个 Django 测试客户端的调用。
    ⚠️ 这不是"桩掉网络层"：方法 / URL / query / 头 / body **一个都不改**，只是换了一条传输（`urllib` → WSGI 测试客户端）⇒ 示例里的每一个请求形状都被**真实端点**接受或拒绝过。
    ⚠️ 顺带把每一次请求记下来（`opener.requests`）：判据除了看响应，还要看**发法**。"""
    sent: list = []

    def opener(request):
        parsed = urllib.parse.urlsplit(request.full_url)
        headers = {
            key.lower(): value for key, value in request.header_items()
        }
        data = request.data
        sent.append({
            "method": request.get_method(),
            "path": parsed.path,
            "query": parsed.query,
            "headers": headers,
            "data": data,
        })
        kwargs: dict = {"SERVER_NAME": parsed.hostname, "REMOTE_ADDR": SOURCE_IP}
        if "authorization" in headers:
            kwargs["HTTP_AUTHORIZATION"] = headers["authorization"]
        if "idempotency-key" in headers:
            kwargs["HTTP_IDEMPOTENCY_KEY"] = headers["idempotency-key"]
        if request.get_method() == "GET":
            response = client.get(
                parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True), **kwargs
            )
        else:
            response = client.post(
                parsed.path,
                data=None if data is None else data.decode("utf-8"),
                content_type=headers.get("content-type", "application/octet-stream"),
                **kwargs,
            )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, response.status_code, "error", {},
                io.BytesIO(response.content),
            )
        return _DocClientResponse(response)

    opener.requests = sent  # type: ignore[attr-defined]
    return opener


class DocumentedFlowTests(TenantTestCase):
    """⭐ 把文档 §10.2 的示例对着**真实端点**跑完整条链，并与文档承诺的期望值对拍。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.doc = _read_doc()
        cls.source = _single_block(_code_blocks(cls.doc), "python")
        cls.namespace: dict = {"__name__": "__doc_test__"}
        exec(compile(cls.source, f"{TAKER_API_MD} §10.2", "exec"), cls.namespace)

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        self.game = Game.objects.create(name="文档示例游戏")
        user = User.objects.create_user(username=TAKER_USERNAME, password="pw-12345678")
        group, _ = Group.objects.get_or_create(name=GROUP_NAME)
        user.groups.add(group)
        StaffProfile.objects.create(user=user).games.set([self.game])
        grant_nodes(user, TAKE_ORDER_NODE, CHANGE_STATUS_NODE)
        self.taker = user
        self.app = TakerApp.objects.create(
            name=APP_NAME,
            app_key=FIXTURE_CLIENT_ID,
            app_secret=FIXTURE_CLIENT_SECRET,
            taker=user,
            created_by=user,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=FIXTURE_ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
            project_text="文档示例项目",
            content="客户写的需求原文",
            customer_phone="synthetic-phone-0001",
            customer_ww="synthetic-ww",
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        self.http = Client(SERVER_NAME=self.domain_name)

    # ── 夹具 ───────────────────────────────────────────────────────────

    def _run_documented_flow(self):
        """跑文档那段 `main()`，返回 `(输出, opener, 返回值)`。"""
        opener = _doc_client_opener(self.http)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = self.namespace["main"](
                "http://" + self.domain_name,
                FIXTURE_CLIENT_ID,
                FIXTURE_CLIENT_SECRET,
                FIXTURE_ORDER_NO,
                FIXTURE_UPDATED_SINCE,
                expected_expires_in=FIXTURE_EXPIRES_IN,
                opener=opener,
            )
        return stdout.getvalue().strip(), opener, result

    def _token(self) -> str:
        """从**真实的**取令牌端点换一颗令牌（⛔ 不手搓夹具）。"""
        response = self.http.post(
            "/api/taker/oauth/token",
            {
                "grant_type": "client_credentials",
                "client_id": FIXTURE_CLIENT_ID,
                "client_secret": FIXTURE_CLIENT_SECRET,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        return json.loads(response.content)["access_token"]

    def _auth(self) -> dict:
        return {"HTTP_AUTHORIZATION": "Bearer " + self._token(), "REMOTE_ADDR": SOURCE_IP}

    # ── ⭐⭐ 示例真的跑 ─────────────────────────────────────────────────

    def test_the_documented_flow_runs_against_the_real_endpoints(self):
        """⭐⭐ 最承重的一条：示例真的驱动了真实端点，且打印的读数与文档 §10.1 逐字相同。"""
        output, opener, _result = self._run_documented_flow()
        self.assertEqual(tuple(output.splitlines()), EXPECTED_MAIN_OUTPUT)

        # ① 示例发出去的**四次**请求（换令牌 → 拉大厅 → 抢单 → 查我的单），逐条核形状。
        self.assertEqual(
            [entry["path"] for entry in opener.requests],
            [
                "/api/taker/oauth/token",
                "/api/taker/v1/hall/",
                "/api/taker/v1/hall/%s/claim/" % FIXTURE_ORDER_NO,
                "/api/taker/v1/my/orders/",
            ],
        )
        self.assertEqual(
            [entry["method"] for entry in opener.requests], ["POST", "GET", "POST", "GET"]
        )
        # ② 换令牌那一次的**体**（form-urlencoded 的三个字段，逐字）。
        token_request = opener.requests[0]
        self.assertEqual(
            token_request["headers"]["content-type"], "application/x-www-form-urlencoded"
        )
        self.assertEqual(
            dict(urllib.parse.parse_qsl(token_request["data"].decode("utf-8"))),
            {
                "grant_type": "client_credentials",
                "client_id": FIXTURE_CLIENT_ID,
                "client_secret": FIXTURE_CLIENT_SECRET,
            },
        )
        # ③ 三次业务调用都带了 Bearer（且**不是**会话 cookie）。
        for entry in opener.requests[1:]:
            with self.subTest(path=entry["path"]):
                self.assertTrue(entry["headers"]["authorization"].startswith("Bearer "))
        # ④ 只有**写**端点带幂等键（两条读端点都不带）。
        self.assertNotIn("idempotency-key", opener.requests[1]["headers"])
        self.assertIn("idempotency-key", opener.requests[2]["headers"])
        self.assertNotIn("idempotency-key", opener.requests[3]["headers"])
        # ⑤ `updated_since` 真的进了 query（带时间与时区，§5.1）。
        query = dict(urllib.parse.parse_qsl(opener.requests[3]["query"]))
        self.assertEqual(query["updated_since"], FIXTURE_UPDATED_SINCE)
        self.assertEqual(query["page_size"], str(MAX_PAGE_SIZE))

    def test_the_token_endpoint_really_returns_the_three_documented_keys(self):
        """⭐ 第三层对拍：文档承诺的 `expires_in=7200` 必须**等于实现真回的那个数**。"""
        response = self.http.post(
            "/api/taker/oauth/token",
            {
                "grant_type": "client_credentials",
                "client_id": FIXTURE_CLIENT_ID,
                "client_secret": FIXTURE_CLIENT_SECRET,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = json.loads(response.content)
        # ⚠️ 恰好三键：多一个（例如 `refresh_token`）就说明文档在说假话。
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        self.assertNotIn("refresh_token", body)
        self.assertEqual(body["token_type"], "Bearer")
        self.assertEqual(body["expires_in"], FIXTURE_EXPIRES_IN)

    def test_the_documented_flow_really_claimed_and_the_db_agrees(self):
        """⭐ 示例走完后**库里**的终态（⛔ 不看 stdout：断言打在真实的出口上）。"""
        self._run_documented_flow()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        main = Performance.objects.get(order=self.order, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, self.taker.pk)
        self.assertIsNotNone(main.claimed_at)
        # 读端点**一个字节都不写业务数据**（唯一那一次写库是认证层的 `last_used_at`）。
        self.app.refresh_from_db()
        self.assertIsNotNone(self.app.last_used_at)

    def test_the_hall_row_keys_are_exactly_the_documented_ten(self):
        """⭐⭐ §3.2 的**恰好 10 个键**：实现回的键集必须逐字等于文档那一张表。"""
        response = self.http.get(
            "/api/taker/v1/hall/",
            {"page": 1, "page_size": MAX_PAGE_SIZE},
            **self._auth(),
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = json.loads(response.content)
        self.assertEqual(payload["code"], 0, payload)
        self.assertEqual(payload["data"]["count"], EXPECTED_HALL_COUNT)
        row = payload["data"]["results"][0]
        self.assertEqual(set(row), HALL_ROW_KEYS)
        # ⛔ P2 的另一半：大厅行里**没有**客户联系方式（也没有员工身份 / 财务 / 镜像那一路）。
        for key in ("customer_phone", "customer_qq", "customer_ww", "remark", "sales_agent",
                    "created_by", "net_received", "refund_fee", "total_price", "payment_status",
                    "origin", "source_system", "remote_order_id", "mirror_pk"):
            with self.subTest(key=key):
                self.assertNotIn(key, row)

    def test_the_my_orders_row_keys_are_exactly_the_documented_seventeen(self):
        """⭐⭐ §5.3 的**恰好 17 个键**（10 + 7）—— 与文档那一张表逐字对拍。"""
        _output, _opener, result = self._run_documented_flow()
        _token, _hall, mine = result
        self.assertEqual(mine["count"], EXPECTED_HALL_COUNT)
        row = mine["results"][0]
        self.assertEqual(set(row), HALL_ROW_KEYS | MY_ORDER_EXTRA_KEYS)
        self.assertEqual(len(row), 18)
        # ⭐ P2 的另一半：**抢到之后**客户联系方式才出现。
        self.assertEqual(row["customer_phone"], "synthetic-phone-0001")
        self.assertEqual(row["customer_ww"], "synthetic-ww")

    def test_the_documented_rejection_paths_are_real(self):
        """⭐ 反向：文档写的失败档**真的**是那个码（⛔ 不是"文档说什么就是什么"）。"""
        auth = self._auth()
        # ① 第一抢成功（示例的那条路）。
        first = self.http.post(
            "/api/taker/v1/hall/%s/claim/" % FIXTURE_ORDER_NO,
            HTTP_IDEMPOTENCY_KEY="doctest-key-000001", **auth,
        )
        self.assertEqual(json.loads(first.content)["code"], 0, first.content[:300])
        # ② 换一个键再抢 ⇒ 1012（文档 §4.3：**包含你自己刚才已经接过**）。
        again = self.http.post(
            "/api/taker/v1/hall/%s/claim/" % FIXTURE_ORDER_NO,
            HTTP_IDEMPOTENCY_KEY="doctest-key-000002", **auth,
        )
        again_body = json.loads(again.content)
        self.assertEqual(again_body["code"], 1012, again_body)
        self.assertIn("包含你自己刚才已经接过", again_body["msg"])
        # ③ 不存在（也**不在厅**）的单 ⇒ 1002，与"不在厅"**同一句话**。
        ghost = self.http.post(
            "/api/taker/v1/hall/20261008000000000999/claim/",
            HTTP_IDEMPOTENCY_KEY="doctest-key-000003", **auth,
        )
        ghost_body = json.loads(ghost.content)
        self.assertEqual(ghost_body["code"], 1002, ghost_body)
        self.assertEqual(ghost_body["msg"], "该订单不存在或不在抢单大厅")
        # ④ 缺幂等键 ⇒ 1000（写端点的硬前置）。
        missing = self.http.post("/api/taker/v1/hall/%s/claim/" % FIXTURE_ORDER_NO, **auth)
        self.assertEqual(json.loads(missing.content)["code"], 1000)
        # ⑤ 单数查单：是他的 ⇒ 0；不是他的 ⇒ 1002 + 那一句。
        detail = self.http.get("/api/taker/v1/orders/%s/" % FIXTURE_ORDER_NO, **auth)
        self.assertEqual(json.loads(detail.content)["code"], 0, detail.content[:300])
        not_mine = self.http.get("/api/taker/v1/orders/20261008000000000998/", **auth)
        not_mine_body = json.loads(not_mine.content)
        self.assertEqual(not_mine_body["code"], 1002, not_mine_body)
        self.assertEqual(not_mine_body["msg"], "该订单不存在或不是你的单")

    def test_the_status_whitelist_is_enforced_by_the_real_endpoint(self):
        """⭐ §6.2 的白名单：`COMPLETED` ⇒ `1000`（点名白名单）；`PENDING_REVIEW` ⇒ 真变。"""
        _output, _opener, result = self._run_documented_flow()
        token = result[0]["access_token"]
        auth = {"HTTP_AUTHORIZATION": "Bearer " + token, "REMOTE_ADDR": SOURCE_IP}
        # ⛔ P3：`COMPLETED` 只能由团团通验收 ⇒ 对端传了回 1000。
        refused = self.http.post(
            "/api/taker/v1/orders/%s/status/" % FIXTURE_ORDER_NO,
            data=json.dumps({"status": "COMPLETED", "remark": ""}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="doctest-key-000010", **auth,
        )
        refused_body = json.loads(refused.content)
        self.assertEqual(refused_body["code"], 1000, refused_body)
        self.assertIn(PENDING_REVIEW_LABEL, refused_body["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        # 白名单里的那一档 ⇒ 真的变。
        accepted = self.http.post(
            "/api/taker/v1/orders/%s/status/" % FIXTURE_ORDER_NO,
            data=json.dumps({"status": "PENDING_REVIEW", "remark": "文档示例"}),
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="doctest-key-000011", **auth,
        )
        accepted_body = json.loads(accepted.content)
        self.assertEqual(accepted_body["code"], 0, accepted_body)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
