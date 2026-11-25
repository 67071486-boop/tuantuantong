"""回调接收器的 WSGI 入口（gunicorn 起的就是它：`apps.callback.wsgi:application`）。

## ⛔ 为什么**不经过** Django

本机的 Django 形态是 `django-tenants`：**每个请求都要拿 Host 去 `platform.Domain` 里认租户**
（`TenantMainMiddleware`）。而 `callback.suqing.chat` 不是任何租户的域名 ⇒ 走 Django 那条路
只会在中间件层 404（`apps/core/views.py` 里 `health` 的 docstring 记着同一条形态）。
接收器**不需要库、不需要租户上下文**，绕开它才能保证「不依赖数据库、不被中间件拖慢」。

⚠️ **代价（如实）**：Django 那套中间件、CSRF、认证**都不生效**。所以本端点的访问控制只有两层：
① nginx 只把**下面那两条路径形状**转进来（其余一律 404）；
② **验签**——没通过一律非 2xx。⛔ 别以为这里还有别的东西在挡。

## 两条路径（⛔ 增量：老的那条一个字都没动）

| 路径 | 谁在用 | 密钥从哪来 |
|---|---|---|
| `/jidanpu/v1/notify`（**精确**，`NOTIFY_PATH`） | 已上线的那条线（`callback.suqing.chat`） | `shared/callback.env` 里那把**单一** `clientSecret` |
| `/api/jidanpu/callback/<22 位码>`（前缀 + 定长码，`CALLBACK_PATH_PREFIX`） | 官方 Open Platform API 那条线（spec §5） | `shared/callback-keys.env` 里**这个码**那一行的密钥（`config.load_callback_secret`） |

⚠️ **本模块不认识 Host、也不判断租户**（⛔ 不许加）：新路径挂在**每个租户自己的域名**上，
"域名 → 租户"这件事由 nginx 那侧决定；接收器只看**路径**里的那串码。

⚠️ **没有租户名 / schema 名 / 订单号**：码是每绑定一串的随机码（spec §5.1）。
`/api/jidanpu/callback/demo` 那种**可枚举**的地址是**故意**不做的
（谁都能把常见工作室名试一遍，确认"这个域名属于哪家、在不在跑"）。

## 行为只有三件（业务方 定的第一版范围，⛔ 没有第四件）

1. **收 POST**：**先验签，再落盘**（H-1）——
   - **验签通过** ⇒ 把**原始报文（逐字节）+ 请求头 + 来源 IP + 收到时间**原样落盘；
   - **验签不通过 / 码不认识 / 报文不是 JSON / 超限** ⇒ **原文丢弃**，只写一条
     **元数据行**（时刻 / 来源 IP / 长度 / sha256 / 拒绝原因）⇒ 任何人拿这个公网端点
     写盘，单条上限从 ~1.3MB 降到几百字节；
   - 另按**来源 IP** 做进程内固定窗限流（429，**不落盘**：连元数据行也只写一次日志，
     否则限流本身又成了写盘放大口）；
2. **验 `sign`**（`apps/callback/signature.py`：空值不参与、按 key 排序、小写 hex、常量时间比较）；
3. **回 `{}` + HTTP 200**。

⛔ 不做业务解析、⛔ 不按租户分发、⛔ 不发任何网络调用、⛔ 不碰数据库、⛔ 不读 `callback-keys.env`
以外的东西（那是"按码取密钥"**唯一**允许读的文件）。
⚠️ 唯一允许的"解析"是**为了验签**把报文体解成 JSON 字典 —— 落盘的永远是**原始字节**，
⛔ 绝不是 `json.loads` 之后 `dumps` 回来的那份（那会换掉空白的形状、键的顺序、数字的写法）。

## 状态码契约（全部 fail-closed，⛔ 没有"跳过验签"这一档）

| 情形 | 状态码 | 记录里的 `sign_reason` | 哪条路 |
|---|---|---|---|
| 验签通过 | 200 | `ok` | 两条 |
| `sign` 缺失 / 空 | 401 | `sign_missing` | 两条 |
| `sign` 对不上 | 401 | `sign_mismatch` | 两条 |
| 某字段不是标量（拼不出待签串） | 401 | `unsigned_value` | 两条 |
| ⭐ **码不在密钥文件里**（或那一行没有密钥） | 401 | `unknown_callback_code` | 仅新路径 |
| 报文不是 JSON 对象 | 400 | `body_not_json` | 两条 |
| **我们**读不到密钥（配置问题） | 503 | `secret_unavailable` | 两条 |
| 报文超过接收上限 | 413 | `body_too_large` | 两条 |
| ⭐ **同一来源 IP 固定窗内超限**（H-1 限流） | 429 | （不落盘） | 两条 |
| 路径形状不是上面那两种 | 404 | （不落盘） | —— |
| 方法不是 POST | 405 | （不落盘） | —— |
| 落盘失败 | 500 | （落盘本来就没成功） | —— |

⚠️ 四处**有意**的选择，都不是随手写的：
- `secret_unavailable` 回 **503** 而不是 401：密钥文件没放好是**我们的**问题，
  回 401 等于对第三方说"你的签名错了"（那是**说假话**，会把排查方向带偏）。
- ⭐ `unknown_callback_code`（码不在文件里）回 **401 而不是 503**：从第三方看，这条回调
  **归不到任何能验签的绑定**上、和"签名对不上"是同一个结果；而 503 留给「**我们**坏了」
  （文件读不到）那一档。⚠️ 两个异常类**互不为子类**（`config.CallbackCodeUnknown` /
  `config.SecretUnavailable`）⇒ 判据**不能**被合成一个码（spec §13.1 的 A3 对 A4）。
- 非 POST **不落盘**：这个路径对公网可达，把每个 `GET` 都记下来等于白送一个"免签名写盘放大口"。
- 超限回 413 且 `body_stored=false`：报文仍要**留痕**（长度 + sha256），但**不落原文**，
  免得一个超大请求把磁盘写满（这是"只记有用的、不记全部字节"的唯一一处例外，如实登记在 spec 代价段）。
- ⭐ **H-1起「不落原文」扩到全部拒收档**：验签失败 / 码不认识 / 报文不是
  JSON 的记录带一格 `body_dropped_reason`（非空 = 原文已丢弃、只有元数据）；
  验签通过的记录那一格是 `None`（`body_stored=true`、逐字节原文在）。
  记录的**键集合仍然统一**（`_new_record` 给全所有键）⇒ `jq` 筛子不会静默漏行。

## 记录里的「从哪条路进来的」

⛔ **没有新增字段**：`path` 本来就在记录里（`_new_record`），而它的**取值域**现在有两种形状
——`/jidanpu/v1/notify` 是旧路径；`/api/jidanpu/callback/<码>` 是新路径、码就在路径里。
消费者的分流判据因此天然可读（spec §5.4 第 3 条 / §5.5：`path == f"{CALLBACK_PATH_PREFIX}{码}"`，用**全等**）。
⚠️ 为什么**不加**一个 `route` / `callback_code` 字段：`record_version` 的**键集合统一**是这条线的
既有纪律，而**旧路径记录的键集合一个都不许变**（那条线正线上跑着、700+ 条真实回调按老形状留档）
⇒ 加字段 = 改旧路径的落盘形状。`record_version` 从 2 升到 **3**（`path` 的取值域变了、
`sign_reason` 多一个可能值），这条**两条路共用同一个版本号**（记录的**形状**是同一套，
读者不该靠版本号去猜"这条是旧路径的吧"；⚠️ 已经在磁盘上的旧记录**原样不动**）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

from apps.callback import config, signature, store

logger = logging.getLogger(__name__)

#: 旧路径：**精确匹配**（带尾斜杠、带子路径一律 404）。nginx 那边同样是精确 `location =`。
#: ⛔ 这个字面量**不许改**：线上那条路（`callback.suqing.chat`）就是按它转进来的。
NOTIFY_PATH = "/jidanpu/v1/notify"

#: ⭐ 新路径的前缀（spec §5.1 / §5.4 第 1 条）：`/api/jidanpu/callback/<22 位随机码>`。
#: ⚠️ 与 `deploy/nginx/tuantuantong.conf` 那条 location 的正则**同一个形状**（T5 负责改那份模板）。
CALLBACK_PATH_PREFIX = "/api/jidanpu/callback/"

#: 码的定长（spec §5.1：`secrets.token_urlsafe(16)` = 22 个字符）。
#: 三条承重（⛔ 别放宽成 `+` / `*` / 前缀匹配）：
#: ① nginx 那条 location 用**同一个**定长正则钉住路径形状；
#: ② 多一个字符 / 少一个字符 / 带尾斜杠 / 带子路径一律 **404 且不落盘**（这个端点对公网可达，
#: 不能白送一个免签名写盘口）；
#: ③ 码是**定长**的 ⇒ 消费者的分流判据能做到**全等**比对（⛔ 不用 `startswith`，那样前缀相同就全收）。
CALLBACK_CODE_LENGTH = 22

#: 码的字母表（base64url：`A-Za-z0-9-_`，⛔ 不含 `/`、⛔ 不含 `=`）。
_CALLBACK_CODE_RE = re.compile(rf"[A-Za-z0-9_-]{{{CALLBACK_CODE_LENGTH}}}")

#: 记录格式版本（以后改字段含义时靠它分辨老记录，⛔ 别删）。
#: 2 → **3**（spec §5.4）：`path` 的取值域变了（多了一种形状）、`sign_reason` 多一个可能值
#: （`unknown_callback_code`）。⚠️ 两条路共用这一个版本号；⛔ 不按路分成两个版本
#: （记录的**键集合**是同一套，见模块 docstring 末节）。
#: 3 → **4**（H-1）：新增 `body_dropped_reason`（非空 = 原文已丢弃、只有元数据）。
#: ⚠️ 磁盘上的老记录（version 2/3）没有这一格、**原样不动**（读侧一律 `.get`）。
RECORD_VERSION = 4

#: **落盘**原文的上限。文档里的回调字段都是短标量（最长的是 `note`），正常报文是几百字节；
#: nginx 那一层给 `client_max_body_size 1m`。这里留 2 MiB = nginx 的 2 倍 ⇒ 正常流量永远碰不到它，
#: 而"绕过 nginx 直接打 `127.0.0.1:8002`"这种情形仍有一个明确的上限。
MAX_BODY_BYTES = 2 * 1024 * 1024

#: **读**的上限（`Content-Length` 比它还大 ⇒ 一个字节都不读，直接 413）。
#: 为什么要有它：`wsgi.input` 是流，把 1 GB 的体读进来只为算个 sha256 就是一次自伤。
HARD_READ_LIMIT = 8 * 1024 * 1024

#: 每次从 `wsgi.input` 读多少。
READ_CHUNK = 64 * 1024

# ── H-1：按来源 IP 的**进程内固定窗限流** ─────────────────
#: ⚠️ **进程内的局限（如实）**：这个计数器住在**每个 gunicorn worker 的进程内存**里
#: （接收器单元是 `--workers 2`）⇒ ① 实际上是「每 worker 各 N 次」，全局上限 ≈ 2×N；
#: ② 重启即清零；③ 多台机器各算各的。它挡的是「一个脚本循环打」这类**粗放放大**，
#: ⛔ 不是精确配额。要精确配额得用共享存储（本进程⛔ 不碰数据库 / 不引依赖，
#: 那是回调接收线的边界，见模块 docstring）。
RATE_LIMIT_WINDOW_SECONDS = 60.0
#: 每个来源 IP 在一个窗口内最多接受多少个请求（含**被拒**的那些：拒收也计数）。
#: 可用可选环境变量 `CALLBACK_RATE_LIMIT_PER_MINUTE` 覆盖（⛔ 不是必填 env；
#: 非法 / ≤ 0 ⇒ 回落默认值并记 warning，⛔ 不静默按 0 把端点打死）。
RATE_LIMIT_MAX_REQUESTS = 120
#: 进程内最多记住多少个来源 IP；超过就**先清过期项**，仍超 ⇒ 整体清空（最坏退化成
#: 「短暂放行」而不是内存无界增长 —— 公网端点不能让伪造 IP 撑爆内存）。
RATE_LIMIT_MAX_TRACKED = 4096
#: 可选覆盖的 env 名（见 `RATE_LIMIT_MAX_REQUESTS`）。
ENV_RATE_LIMIT = "CALLBACK_RATE_LIMIT_PER_MINUTE"

#: `{来源 IP: (窗口起点 monotonic 秒, 本窗口内已计数)}`。⛔ 不做模块级永久缓存：窗口到期
#: 会覆盖 / 清理（见 `_rate_limited`）。
_RATE_WINDOWS: dict[str, tuple[float, int]] = {}

#: ⭐ 拒收元数据行的**窗口内聚合**（H-1 评审意见「元数据行专有键 / 聚合」）：
#: `{(来源 IP, sha256（没有就按长度）, 拒绝原因): 窗口起点}`。
#: 同一条被拒报文在同一个窗口里反复到达 ⇒ **只写第一条**（否则重放同一份报文仍是
#: 每发一次写一行的放大口，哪怕单条已经很小）。⚠️ 只在**拒收**档聚合：验签通过的记录
#: ⛔ 绝不合并（那是真实事件，一条就是一条）。
_REJECTED_SEEN: dict[tuple[str, str, str], float] = {}

_JSON_HEADERS = (
    ("Content-Type", "application/json; charset=utf-8"),
    # ⛔ 别缓存：回调是"事件"，缓存一条事件没有任何意义，而中间层缓存会让"没收到"变成假象。
    ("Cache-Control", "no-store"),
)


def _respond(start_response, status: str, payload: dict[str, Any]) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    start_response(status, list(_JSON_HEADERS) + [("Content-Length", str(len(body)))])
    return [body]


def _headers(environ: dict[str, Any]) -> list[list[str]]:
    """请求头 → `[[名字, 值], …]`（名字按字典序，便于比对两次报文）。

    ⚠️ **这一份不是逐字节的**：WSGI 给的是 `名字 → 值` 的映射，
    ① 名字被规范化成 `HTTP_X_REAL_IP` 这种形状（**原始大小写丢了**）；
    ② **同名重复头会被折叠**（HTTP 允许重复头）。⇒ 留痕的权威永远是 `body_*` 那几个字段，
    头这一份只用来回答"这条是谁打来的、带了什么"。
    """
    pairs: list[list[str]] = []
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            pairs.append([key[len("HTTP_"):].replace("_", "-").lower(), str(value)])
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            pairs.append([key.replace("_", "-").lower(), str(value)])
    pairs.sort(key=lambda item: item[0])
    return pairs


def _source_ip(environ: dict[str, Any]) -> tuple[str, str]:
    """`(来源 IP, 直连对端地址)`。

    ⚠️ 来源 IP 取 `X-Real-IP`：nginx 的 `proxy_params` 里是
    `proxy_set_header X-Real-IP $remote_addr` —— 它**覆盖写**（不是追加）⇒ 客户端自己伪造的
    `X-Real-IP` 到不了这里。⚠️ 成立的前提是"只有 nginx 能连到本端口"（我们绑 `127.0.0.1`）。
    两个都记：`X-Real-IP` 缺省时来源 IP 回落到对端地址，而 `peer_addr` 永远是**真实对端**
    （正常情况是 `127.0.0.1`）—— 两者不一致时才说明这一条是经 nginx 来的。
    """
    peer = str(environ.get("REMOTE_ADDR") or "")
    real = str(environ.get("HTTP_X_REAL_IP") or "").strip()
    return (real or peer), peer


def _now_iso() -> str:
    """收到时间（UTC，带微秒与偏移量）。"""
    return datetime.now(timezone.utc).isoformat()


def _read_body(environ: dict[str, Any]) -> tuple[bytes, str | None, int, bool]:
    """读报文，返回 `(保留的字节, 全文 sha256, 实际长度, 是否完整保留)`。

    ⚠️ sha256 **按读到的全部字节**算（即使超限也一直算到最后）⇒ 超限记录里那个 sha256
    仍是**这一份报文**的指纹，排查时能判断"是不是同一份被重发了"。
    """
    stream = environ.get("wsgi.input")
    raw_length = str(environ.get("CONTENT_LENGTH") or "").strip()
    try:
        declared = int(raw_length) if raw_length else None
    except ValueError:
        declared = None

    # `Content-Length` 比读的上限还大 ⇒ 一个字节都不读（别为了算指纹把 1 GB 拉进来）。
    if declared is not None and declared > HARD_READ_LIMIT:
        return b"", None, declared, False

    hasher = hashlib.sha256()
    kept: list[bytes] = []
    kept_bytes = 0
    total = 0
    while stream is not None:
        if declared is not None:
            remaining = declared - total
            if remaining <= 0:
                break
            want = min(READ_CHUNK, remaining)
        elif total >= HARD_READ_LIMIT:
            break
        else:
            want = READ_CHUNK
        chunk = stream.read(want)
        if not chunk:
            break
        total += len(chunk)
        hasher.update(chunk)
        if kept_bytes < MAX_BODY_BYTES:
            take = min(MAX_BODY_BYTES - kept_bytes, len(chunk))
            kept.append(chunk[:take])
            kept_bytes += take
    return b"".join(kept), hasher.hexdigest(), total, total <= MAX_BODY_BYTES


def _decode_text(body: bytes) -> str | None:
    """能按 UTF-8 解出来就同时留一份可读副本（**便利镜像**，⛔ 不是权威）。

    ⚠️ 权威永远是 `body_b64`（逐字节）。这一份只是让 `grep` / 管理命令能直接看中文，
    也顺带回答"这份报文到底是不是 UTF-8"。
    """
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _new_record(environ: dict[str, Any]) -> dict[str, Any]:
    """记录骨架：**所有键都在场**（缺省值是 None / 空）。

    ⚠️ 形状统一是**有意的**：排查时要能拿 `jq` 直接筛字段，而"某条记录少了某个键"
    会让筛子静默漏掉它（本仓口径：静默空比报错更贵）。
    ⚠️ ⛔ **不许为了新路径往这里加字段**（例如 `route` / `callback_code`）：线上那份事件文件里
    700+ 条真实回调是老形状，而**旧路径记录的键集合一个都不许变**；「哪条路进来的」由 `path`
    的取值域表达（见模块 docstring 末节）。
    """
    source_ip, peer_addr = _source_ip(environ)
    return {
        "record_version": RECORD_VERSION,
        "received_at": _now_iso(),
        "method": str(environ.get("REQUEST_METHOD") or ""),
        "path": str(environ.get("PATH_INFO") or ""),
        "query_string": str(environ.get("QUERY_STRING") or ""),
        "source_ip": source_ip,
        "peer_addr": peer_addr,
        "headers": _headers(environ),
        "sign_ok": False,
        "sign_reason": None,
        "sign_keys": [],
        "sign_ok_declared_fields_only": None,
        "json_ok": False,
        "body_len": 0,
        "body_sha256": None,
        "body_stored": False,
        #: ⭐ H-1（record_version 4）：非空 = **原文已丢弃、只留元数据**（验签没过 /
        #: 码不认识 / 不是 JSON / 超限）；验签通过的记录是 `None`（原文逐字节在）。
        "body_dropped_reason": None,
        "body_b64": None,
        "body_text": None,
    }


def _rate_limit_max() -> int:
    """本进程的窗口上限：`RATE_LIMIT_MAX_REQUESTS`，可被可选 env 覆盖（非法 ⇒ 回落 + warning）。"""
    raw = os.environ.get(ENV_RATE_LIMIT)
    if raw is None or not raw.strip():
        return RATE_LIMIT_MAX_REQUESTS
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning(
            "%s=%r 不是正整数 ⇒ 回落默认 %s（⛔ 不按 0 关掉限流）",
            ENV_RATE_LIMIT, raw, RATE_LIMIT_MAX_REQUESTS,
        )
        return RATE_LIMIT_MAX_REQUESTS
    return value


def _prune_rate_windows(moment: float) -> None:
    """清掉**窗口已过期**的 IP；仍超过上限 ⇒ 整体清空（见 `RATE_LIMIT_MAX_TRACKED`）。"""
    expired = [
        ip for ip, (start, _count) in _RATE_WINDOWS.items()
        if moment - start >= RATE_LIMIT_WINDOW_SECONDS
    ]
    for ip in expired:
        _RATE_WINDOWS.pop(ip, None)
    if len(_RATE_WINDOWS) > RATE_LIMIT_MAX_TRACKED:
        _RATE_WINDOWS.clear()


def _rate_limited(source_ip: str, *, now: float | None = None) -> tuple[bool, bool]:
    """按来源 IP 的**固定窗**计数：回 `(是否拒收, 是不是本窗口第一次拒收)`。

    ⚠️ 第二个返回值只服务**日志采样**：攻击者每秒几千个请求时，每个 429 都打一条
    warning 本身就是一份日志洪水 ⇒ 每窗口每 IP 只打第一条（状态码照回 429）。
    """
    moment = time.monotonic() if now is None else now
    limit = _rate_limit_max()
    window_start, count = _RATE_WINDOWS.get(source_ip, (moment, 0))
    if moment - window_start >= RATE_LIMIT_WINDOW_SECONDS:
        window_start, count = moment, 0
    count += 1
    _RATE_WINDOWS[source_ip] = (window_start, count)
    if len(_RATE_WINDOWS) > RATE_LIMIT_MAX_TRACKED:
        _prune_rate_windows(moment)
    limited = count > limit
    return limited, limited and count == limit + 1


def _should_persist_rejection(record: dict[str, Any]) -> bool:
    """这条**拒收**元数据行该不该落盘（同 IP + 同 sha256 + 同原因，一个窗口内只写一次）。"""
    sha = record.get("body_sha256") or f"len:{record.get('body_len')}"
    key = (record.get("source_ip") or "", str(sha), str(record.get("body_dropped_reason") or ""))
    moment = time.monotonic()
    started = _REJECTED_SEEN.get(key)
    if started is not None and moment - started < RATE_LIMIT_WINDOW_SECONDS:
        return False
    _REJECTED_SEEN[key] = moment
    if len(_REJECTED_SEEN) > RATE_LIMIT_MAX_TRACKED:
        expired = [
            item for item, start in _REJECTED_SEEN.items()
            if moment - start >= RATE_LIMIT_WINDOW_SECONDS
        ]
        for item in expired:
            _REJECTED_SEEN.pop(item, None)
        if len(_REJECTED_SEEN) > RATE_LIMIT_MAX_TRACKED:
            _REJECTED_SEEN.clear()
    return True


def _finish_rejected(
    start_response, record: dict[str, Any], status: str, payload: dict[str, Any]
) -> list[bytes]:
    """拒收档的落盘出口：先过**窗口内聚合**，该写才写（响应码 / 响应体一个字不变）。"""
    if not _should_persist_rejection(record):
        logger.info(
            "同一条被拒报文在窗口内重复到达 ⇒ 不再写第二条元数据行：source_ip=%s reason=%s",
            record.get("source_ip"), record.get("body_dropped_reason"),
        )
        return _respond(start_response, status, payload)
    return _finish(start_response, record, status, payload)


def _mark_body_dropped(record: dict[str, Any], reason: str) -> None:
    """把记录降级成**元数据行**：原文 / 请求头 / 查询串一律丢弃（H-1 的核心）。

    ⚠️ **键集合不动**（`_new_record` 已经给全）⇒ 只是值变空 + `body_dropped_reason` 非空。
    ⛔ 别在这里保留 `headers`：「先落盘后验签」的放大面正是**攻击者可控的字节**，
    而请求头同样是攻击者可控的（几 KB 级）⇒ 拒收只留时刻 / 来源 IP / 长度 / sha256 / 原因。
    ⚠️ `path` **保留**：消费侧靠它把「属于本绑定的回调」分流，也靠它区分两条路；
    它受 `NOTIFY_PATH` / 22 位定长码的路径形状闸约束（⛔ 不是任意长串）。
    """
    record["body_stored"] = False
    record["body_b64"] = None
    record["body_text"] = None
    record["body_dropped_reason"] = reason
    record["headers"] = []
    record["query_string"] = ""


def _fill_body(record: dict[str, Any], body: bytes, sha256: str | None, length: int, stored: bool) -> None:
    record["body_len"] = length
    record["body_sha256"] = sha256
    record["body_stored"] = stored
    if stored:
        # ⛔ 逐字节：base64 是**权威副本**；`body_text` 只是"能读"的那一份（可能为 None）。
        record["body_b64"] = base64.b64encode(body).decode("ascii")
        record["body_text"] = _decode_text(body)


def _parse_json_object(body: bytes) -> dict[str, Any] | None:
    """只为**验签**把报文体解成 dict。⛔ 这个结果不许拿去覆盖落盘的原文。"""
    try:
        params = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return params if isinstance(params, dict) else None


def _finish(
    start_response, record: dict[str, Any], status: str, payload: dict[str, Any], *, durable=False
) -> list[bytes]:
    """**先落盘、再回**（业务方口径）。⚠️ 落盘失败 ⇒ 异常往外抛（gunicorn 回 500），⛔ 绝不回 200。"""
    store.append_record(record, durable=durable)
    return _respond(start_response, status, payload)


def callback_code_of_path(path: str) -> str | None:
    """⭐ 从**新路径**里取出那串码；不是这个前缀、或形状不对 ⇒ `None`。

    判据 = **剥掉前缀之后的整个剩余部分**必须恰好匹配 `[A-Za-z0-9_-]{22}`
    （`fullmatch`，⛔ 不是 `match` / `search` / `startswith`）：

    - 少一个字符 / 多一个字符 / 带尾斜杠（`…/<码>/`）/ 带子路径（`…/<码>/x`）
      / 22 位里混进 `/` 或 `+` 或 `=` ⇒ **一律 `None`** ⇒ 调用方回 404、且**不落盘**；
    - ⚠️ 为什么这么严：这个端点对公网可达，而"近似匹配"等于白送一个免签名写盘口
      （spec §5.4 第 1 条）。⚠️ nginx 那一侧用的是**同一个**定长正则（T5 负责那份模板）。
    """
    if not path.startswith(CALLBACK_PATH_PREFIX):
        return None
    code = path[len(CALLBACK_PATH_PREFIX):]
    return code if _CALLBACK_CODE_RE.fullmatch(code) else None


def _secret_for(route: str, code: str | None, official_secret_lookup=None) -> str:
    """按**路**取密钥（唯一一处分派）。⛔ 两条路**不许互相兜底**。

    - `route == "notify"`（旧路径）⇒ `config.load_client_secret()`：`shared/callback.env` 里那把
      单一 `clientSecret`。⛔ 这条读取路径**一个字节都没改**（线上正跑着，见模块 docstring）；
    - `route == "callback"`（新路径）⇒ `config.load_callback_secret(code)`：`shared/callback-keys.env`
      里**这个码**那一行的密钥。

    ⛔ **不做"另一把试试"**：旧路径去查码 / 新路径回落到旧密钥，都会让**一个绑定**的密钥能验
    **另一条路**的报文 —— 那是把两条线的边界拆了。判据见 `src/tests/test_callback_receiver.py`
    的「两条路互不兜底」那条（两侧各能红）。
    """
    if route == "callback":
        lookup = official_secret_lookup or config.load_callback_secret
        return lookup(code or "")
    return config.load_client_secret()


def application(environ: dict[str, Any], start_response, *, official_secret_lookup=None, durable=False):
    """WSGI 入口（唯一的写入方）。

    ## ⭐ H-1：**先验签、再落盘** + 按来源 IP 限流

    旧顺序是「读报文 → 原样落盘 → 验签」⇒ 任何公网 POST（路径形状对就行）都能写一条
    最高 ~1.3MB 的记录，验签失败照样写，且无限流。现在的顺序：

    1. 路径 / 方法形状闸（401/404/405 与旧口径逐字不变，**不落盘**）；
    2. **按来源 IP 的固定窗限流**（超限 ⇒ **429 且不落盘**：限流本身不能变成写盘放大口，
       日志每窗口每 IP 只打第一条）；
    3. 读报文（既有上限）→ **验签**；
    4. **验签通过** ⇒ 落**完整记录**（逐字节原文）；**任何拒收** ⇒ 只落**元数据行**
       （时刻 / 来源 IP / 长度 / sha256 / 拒绝原因；`body_dropped_reason` 非空）。

    状态码与响应体**只新增了一档 429**（按来源 IP 限流，H-1）：其余各档全部 fail-closed、
    与旧口径**逐字不变**（见模块 docstring 那张表）——变的还有「拒收时磁盘上留下什么」。
    """
    path = str(environ.get("PATH_INFO") or "")
    code: str | None = None
    if path == NOTIFY_PATH:
        route = "notify"
    else:
        code = callback_code_of_path(path)
        if code is None:
            # ⛔ 不落盘（与既有「非 POST / 别的路径不落盘」同一条理由：公网可达的端点，
            # 不能白送一个"免签名写盘放大口"）。
            return _respond(start_response, "404 Not Found", {"error": "not_found"})
        route = "callback"

    if str(environ.get("REQUEST_METHOD") or "").upper() != "POST":
        # ⛔ 不落盘（见模块 docstring 的有意选择）；两条路同一口径。
        return _respond(start_response, "405 Method Not Allowed", {"error": "method_not_allowed"})

    # ⭐ H-1 第 2 步：限流排在**读报文之前**（省掉读盘 / 算 sha 的开销，也不为攻击流量
    # 写任何字节）。⚠️ 进程内计数 ⇒ 每个 gunicorn worker 各算一份（单元是 --workers 2，
    # 实际全局上限约为配置值的 2 倍），局限如实写在常量注释里。
    source_ip, _peer = _source_ip(environ)
    limited, first_rejection = _rate_limited(source_ip)
    if limited:
        if first_rejection:
            logger.warning(
                "回调按来源 IP 限流（窗口 %s 秒内超过 %s 次）：source_ip=%s path=%s"
                " ⇒ 回 429 且不落盘（限流不能变成写盘放大口）",
                int(RATE_LIMIT_WINDOW_SECONDS), _rate_limit_max(), source_ip, path,
            )
        return _respond(start_response, "429 Too Many Requests", {"error": "rate_limited"})

    record = _new_record(environ)
    body, sha256, length, stored = _read_body(environ)
    record["body_len"] = length
    record["body_sha256"] = sha256

    if not stored:
        record["sign_reason"] = "body_too_large"
        _mark_body_dropped(record, "body_too_large")
        logger.warning(
            "回调报文超限已拒绝（%s 字节 > 落盘上限 %s）：sha256=%s source_ip=%s",
            length,
            MAX_BODY_BYTES,
            sha256,
            record["source_ip"],
        )
        return _finish_rejected(start_response, record, "413 Payload Too Large", {"error": "body_too_large"})

    params = _parse_json_object(body)
    if params is None:
        record["sign_reason"] = "body_not_json"
        _mark_body_dropped(record, "body_not_json")
        logger.warning(
            "回调报文不是 JSON 对象 ⇒ 验不了签：len=%s sha256=%s source_ip=%s",
            length,
            sha256,
            record["source_ip"],
        )
        return _finish_rejected(start_response, record, "400 Bad Request", {"error": "body_not_json"})

    record["json_ok"] = True
    try:
        secret: str | None = _secret_for(route, code, official_secret_lookup)
    except config.CallbackCodeUnknown:
        # ⭐ 码不认识（或那一行没有密钥）：**401** —— 与验签失败同一档（对第三方来说都是
        # "这条报文验不了签"），⛔ 不是 200、⛔ 也不是 503（503 留给"我们的文件读不到"）。
        record["sign_reason"] = signature.REASON_UNKNOWN_CALLBACK_CODE
        _mark_body_dropped(record, signature.REASON_UNKNOWN_CALLBACK_CODE)
        logger.warning("回调码未登记或无可用密钥 ⇒ 回 401")
        return _finish_rejected(start_response, record, "401 Unauthorized", {"error": "sign_invalid"})
    except config.SecretUnavailable:
        record["sign_reason"] = signature.REASON_SECRET_UNAVAILABLE
        _mark_body_dropped(record, signature.REASON_SECRET_UNAVAILABLE)
        if route == "callback" and official_secret_lookup is not None:
            logger.error("回调验签不可用：无法读取工作室绑定密钥 ⇒ 回 503")
        else:
            logger.error(
                "回调验签不可用：读不到密钥文件 %s ⇒ 回 503（把 0600 密钥文件放好并重启本单元）。",
                config.env_file_path() if route == "notify" else config.callback_keys_path(),
            )
        return _finish_rejected(
            start_response, record, "503 Service Unavailable", {"error": "verifier_unavailable"}
        )

    check = signature.verify_sign(params, params.get(signature.SIGN_FIELD_NAME), secret)
    record["sign_ok"] = check.ok
    record["sign_reason"] = check.reason
    record["sign_keys"] = list(check.signed_keys)
    if check.ok_declared_fields_only is not None:
        # 取证：主判没过、但**只按文档字段表**能过 ⇒ 双方对"哪些字段参与签名"的理解不一致
        # （真机首日就是这个形状：`action=30` 带 `imgUrl`/`name`，只算文档字段会误拒）。
        record["sign_ok_declared_fields_only"] = check.ok_declared_fields_only

    if check.ok:
        # ⭐ H-1 第 4 步：**只有验签过的请求才落原文**（逐字节权威副本）。
        _fill_body(record, body, sha256, length, True)
        return _finish(start_response, record, "200 OK", {}, durable=durable)

    # ⭐ 验签失败 ⇒ 元数据行（原文 / 请求头 / 查询串全部丢弃）。
    _mark_body_dropped(record, check.reason or "sign_invalid")
    logger.warning(
        "回调验签不通过（%s）：sha256=%s source_ip=%s action=%s sign_ok_declared_fields_only=%s"
        "（本条只落元数据行，原文已丢弃）",
        check.reason,
        sha256,
        record["source_ip"],
        params.get("action"),
        record.get("sign_ok_declared_fields_only"),
    )
    return _finish_rejected(start_response, record, "401 Unauthorized", {"error": "sign_invalid"})


__all__ = [
    "application",
    "NOTIFY_PATH",
    "CALLBACK_PATH_PREFIX",
    "CALLBACK_CODE_LENGTH",
    "callback_code_of_path",
    "MAX_BODY_BYTES",
    "HARD_READ_LIMIT",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RATE_LIMIT_MAX_REQUESTS",
]
