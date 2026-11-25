"""开放平台 OAuth2 令牌的**纯函数**（v2 设计文档 §5.1 / §5.3；T2）。

## ⛔ 这个模块不碰 ORM、不读 cache、不读 `settings` 以外的东西

令牌的**有效性**只取决于两样东西：**载荷那串字节**与 **`app_secret`**。它完全不取决于
「库里有什么、进程里记着什么」⇒ 本模块里没有一次数据库查询、没有一次 `cache` 访问、
也没有任何模块级可变状态。这条不是为了好看，是两个可验证的后果：

1. **多 worker 下语义不变**：生产是 `gunicorn --workers 2`（实测），任何"签发时记一笔、
   校验时查那一笔"的形状**必然**半失效（§5.3 第 1 条）；无状态自签没有这个窗口。
2. **判据可以在不建库的用例里逐字对拍**（`tests/test_openapi_token.py` 的 `SimpleTestCase`
   那一档）：形状 / 载荷 / 过期边界都不需要夹具，也不需要迁移跑得起来。

⚠️ **代价（如实登记，⛔ 不许读成"没有代价"）**：令牌**不能被单独吊销**（§3.2-1 /
§14-1）；要立刻让一批令牌失效，只有轮换 `app_secret`（那会让该应用**全部**令牌一起失效）
或停用应用。这两条语义在认证层判（`authentication.py`），⛔ 不在本模块里判。

## 令牌形态（§5.3 逐字）

    token = base64url(payload_json) + "." + base64url(HMAC-SHA256(app_secret, base64url(payload_json)))
    payload = {"ak": <app_key>, "exp": <Unix 秒>}

- **签的是 `base64url(payload_json)` 这串字符**（⛔ 不是 `payload_json` 的原始字节）：
  这样校验方**先拿到哪一串、就签哪一串**，不存在"重新序列化一次、字节却不同"的分叉
  （JSON 的键序 / 空白 / `ensure_ascii` 都会影响字节，而那三样都是序列化器的实现细节）。
- **两段都去掉 `=` 填充**：填充不含信息，却会出现在 HTTP 头里（`=` 在某些转发链上是
  需要转义的字符）⇒ 去掉它，解码时按长度补回来。
- 段字符集限定为 base64url 字母表（`A-Za-z0-9_-`）：形状判据**先**过这一条正则再解码，
  于是"乱码令牌"与"签名错"走的是同一条失败路（⛔ 不会有一个分支让 `binascii` 抛出去）。

## ⛔ 令牌里**不放租户标识**（`AGENTS.md` §4 的红线一字未改）

载荷里只有 `ak` 与 `exp`。租户上下文**只**来自请求的 `Host` → 数据库连接 schema
（`ApiApp` 表本身就在租户 schema 里，`app_key` 只在**当前租户**内查）
⇒ 拿租户 A 的令牌打租户 B 的域名，在 B 的库里**查不到**这个 `ak`，一律 401。
⚠️ 载荷键集由 `tests/test_openapi_token.py` 用**恰好集合**断言钉住（⛔ 不是"含这两个键"）。

## 本模块**不**做的事（都在认证层 / 视图层）

- ⛔ 不判 `ApiApp.state`、不判 `ip_allowlist`、不查库——那些要读数据库，属
  `ApiAppBearerAuthentication`（§5.3 的校验顺序：查 `ak` → 重算 HMAC → 判 `exp` → 放行，
  `state` 与白名单是认证层在**验签之后**的两道门）。
- ⛔ 不产生任何 msg / 状态码：失败一律抛本模块的异常，由调用方翻成 401
  （同族先例：v1 面那个已删除的签名纯函数模块的 `SigningError`，写法见 git 历史）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time

#: 令牌有效期（秒）。⭐ **这个常量是 `7200` 的出处**（判据 = `tests/test_openapi_token.py`
#: 里那条扫 `apps/openapi/**` 的字面量用例：`7200` 只许出现在本文件这一处）。
#:
#: 取值理由 = §5.1 取**对标正文**那个数（正文写 7200）。⚠️ 对标的**示例**里写的是
#: `3599`，两处自相矛盾 ⇒ 我们只认正文，并把它写进我们自己的文档（§14-2 登记了这条差异）。
#: 换这个数**只影响新签发的令牌**：有效期是写在载荷里的（`exp`），已经发出去的令牌
#: 按自己那个 `exp` 到期 ⇒ 改动不会追溯作废旧令牌。
TOKEN_TTL_SECONDS = 7200

#: ⭐ **未命中分支用的假密钥**（只为让"没这个应用"与"凭据不对"付同量级的一次比较 / 摘要，
#: 结果一律丢弃）。
#:
#: ⛔ 与任何真实 `app_secret` 无关、也⛔ 不参与任何判定：它的唯一用途是**抹平时序差别**
#: （§5.3 / §5.4 的"不给应用枚举面"要求）。⛔⭐ **现行口径（T10）**：本处原写
#: 「签名认证那条老路有它自己的一份假密钥（随 v1 面删除）」—— 那条老路已在 T10 删除
#: ⇒ 今天本常量是**唯一**一份假密钥，由 Bearer 认证与取令牌端点共用，
#: ⛔ 不要在两处各写一个字面量。
TIMING_PARITY_SECRET = "openapi-timing-parity-secret"

#: 载荷段 / 签名段的字符集（base64url 去填充后的形状）。
#: ⚠️ 用 `fullmatch` 而不是 `match`：`match` 配上 `$` 时 Python 的 `$` **在尾随换行之前
#: 也匹配** ⇒ `"<段>\n"` 会被静默放行（同族实测见 git 历史里那个已删除的签名纯函数模块
#: 的时间戳正则注释）。
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-]+")


class TokenError(ValueError):
    """令牌**不可用**（形状非法 / 解码失败 / 不是 JSON 对象 / 字段缺失或类型不对 / 验签不过）。

    ## 为什么继承 `ValueError`，又为什么消息里不带收到的原文

    调用方（认证层 / 视图层）必须把它翻成 **401**：一个形状不对的令牌是**可预期**的失败，
    不是程序缺陷（`AGENTS.md` §5-E：预期失败走业务结果，未预期异常才响亮地 500）。
    ⚠️ 本异常**绝不该**以未捕获的形态冒到 500 —— 形状 / 解码 / JSON / 字段类型四类失败
    在模块内**全部**被翻成本异常，`binascii.Error` / `json.JSONDecodeError` /
    `UnicodeDecodeError` / `TypeError` 一个都不许漏出去（判据 = `tests/test_openapi_token.py`
    的畸形语料那一条，逐条断言异常类型**恰好**是本类）。

    ⚠️ `args` 里**不带**收到的那个令牌原文：它是**调用方可控**的字节，而对外失败响应
    不许回显框架 / 异常原文（同族先例：v1 面那个已删除的签名纯函数模块的异常 docstring）。
    """


class TokenExpiredError(TokenError):
    """令牌**验签通过、但已过期**（`exp` 到了）。

    ⭐ 单独一个类型是因为认证层要给它**另一句文案**："令牌已过期"可以说破的理由是
    ——**走得到这个分支的调用方已经证明了它握着 `app_secret`**（HMAC 先验过），
    所以说破"过期"不构成应用枚举面（§5.4 那条"三者逐字相同"管的是**凭据**那一档）。
    ⚠️ 它是 `TokenError` 的子类 ⇒ 调用方若只 catch `TokenError`，过期会被算进"凭据无效"
    那一档（安全上更保守，但会让调用方拿着过期令牌去查密钥）。
    """


def constant_time_equals(expected, provided) -> bool:
    """常量时间比较两串（`hmac.compare_digest` 的封装处）。

    ## 为什么要有这一层

    "比较"在本线出现两次（**签名段**与**客户端密钥**），而"用 `compare_digest` 而不是 `==`"
    是**同一个判据**（§5.1 的硬要求：比较必须常量时间）⇒ 收在这一处，
    ⛔ 不要在调用方各写一遍裸 `==`（那是第二份判据，`AGENTS.md` §5-E）。

    ## 类型口径
    两个入参**都是 `str`** 才有意义（`hmac.compare_digest` 对 `str` 只接受 ASCII 字符，
    对其它类型会抛 `TypeError`）⇒ 这里对非 `str` 一律回 `False`，
    ⛔ 不让 `TypeError` 冒出去变成 500（一个 `None` 密钥不该把端点打成 500）。
    """
    if not isinstance(expected, str) or not isinstance(provided, str):
        return False
    try:
        return hmac.compare_digest(expected, provided)
    except TypeError:
        # `str` 里含非 ASCII 字符时 `compare_digest` 会抛 `TypeError`（CPython 的实现限制）。
        # 走到这里说明某一侧是**非 ASCII 的串**：它不可能是我们签出来的东西 ⇒ 判不等。
        return False


def _b64encode(raw: bytes) -> str:
    """base64url 编码并**去掉 `=` 填充**（理由见模块 docstring）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(segment: str) -> bytes:
    """base64url 解码（形状非法 ⇒ `TokenError`）。

    ⚠️ 先过 `_SEGMENT_RE` 再解码：`binascii.a2b_base64` 在默认参数下**会忽略非法字符**，
    于是"乱码"会被静默解出一段字节，而症状是它**碰巧**解出了合法 JSON 才被发现。
    """
    if _SEGMENT_RE.fullmatch(segment) is None:
        raise TokenError("令牌段不是合法的 base64url 形状")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        # 长度不在 base64 的合法集合里（例如 `% 4 == 1`）会走到这里。
        raise TokenError("令牌段无法解码") from exc


def _sign(payload_segment: str, app_secret: str) -> str:
    """按**载荷段**算签名（`base64url(HMAC-SHA256(app_secret, 载荷段))`）。

    ⚠️ HMAC 构造只有这一处：`issue` / `verify` 与认证层的**哑 HMAC** 都经它
    （⛔ 不要在别处再 `hmac.new(...)` 一次 —— 那是第二份签名规则）。
    """
    digest = hmac.new(
        app_secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64encode(digest)


def _decode_payload(segment: str) -> dict:
    """把载荷段解成 dict（解码 / JSON / 字段三类失败一律 `TokenError`）。

    ⚠️ 返回的是**未验签**的载荷；本函数不知道密钥，也就不判签名与过期
    （那两件在 `verify` 里）——调用方⛔ 不要拿它的返回值当"可信数据"
    （认证层只从它取 `ak` 用来查库，随后的 HMAC 才是准入判据）。
    """
    raw = _b64decode(segment)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TokenError("令牌载荷不是 UTF-8") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TokenError("令牌载荷不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise TokenError("令牌载荷不是 JSON 对象")
    return payload


def _require_str(payload: dict, key: str) -> str:
    """取一个**非空 `str`** 字段（缺失 / 类型不对 / 空串一律 `TokenError`）。"""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TokenError("令牌载荷字段 %s 缺失或类型不对" % key)
    return value


def _require_int(payload: dict, key: str) -> int:
    """取一个**真 `int`** 字段。

    ⚠️ 显式排除 `bool`：`isinstance(True, int)` 是 `True`，而 `True` 当 Unix 秒用
    会让"过期时间"变成一个没人想过的值（它还会**静默通过**现在的 `exp` 比较）。
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TokenError("令牌载荷字段 %s 缺失或类型不对" % key)
    return value


def _now_seconds(now) -> int:
    """`now` 归一成 Unix 秒（不传 ⇒ 取当前时刻）。

    ⚠️ 显式传 `now` 是**判据需要**的：过期边界要能不 `sleep` 地对拍
    （`tests/test_openapi_token.py` 的 `now=issued + TOKEN_TTL_SECONDS ± 1` 那一组）。
    """
    if now is None:
        return int(time.time())
    if isinstance(now, bool) or not isinstance(now, int):
        raise TokenError("now 必须是 Unix 秒整数")
    return now


def issue(app_key: str, app_secret: str, *, now: int | None = None) -> str:
    """签发一个令牌（`payload = {"ak": app_key, "exp": now + TOKEN_TTL_SECONDS}`）。

    ## 为什么载荷里**不放 `iat`**（签发时刻）
    今天**没有任何消费方**读它（认证层只读 `ak` 用来查库；本仓这个端点不写操作留痕、
    也不记审计）。加一个字段的代价是 `verify` 必须**多判一条**（缺失 / 类型不对 / 与 `exp`
    的关系），而那三条判据今天没有任何用例能证伪 ⇒ 那正是 §5-E 说的"防御性写法"
    被当成承重逻辑读。**将来真要用到签发时刻时**（例如接入文档给第三方算"还剩多久"），
    正确做法是**同一次**在 `issue` 与 `verify` 的字段判据里加它，⛔ 不是只加签发那一侧。

    ## 入参校验
    `app_key` / `app_secret` 必须是非空 `str`（从库里读出来的两列本来就是 `str`；
    真传了别的东西，这里抛 `TokenError`，⛔ 不让 `TypeError` 冒出去）。

    ## 为什么 `exp` 是绝对时刻而不是"有效秒数"
    绝对时刻让校验方**不需要**知道签发时刻（也就不需要 `iat`），且 `exp` 被签名覆盖
    ⇒ 改它必然破坏 HMAC。
    """
    if not isinstance(app_key, str) or not app_key:
        raise TokenError("app_key 必须是非空字符串")
    if not isinstance(app_secret, str) or not app_secret:
        raise TokenError("app_secret 必须是非空字符串")
    expires_at = _now_seconds(now) + TOKEN_TTL_SECONDS
    segment = _b64encode(
        json.dumps({"ak": app_key, "exp": expires_at}, separators=(",", ":")).encode("utf-8")
    )
    return "%s.%s" % (segment, _sign(segment, app_secret))


def _read_payload(token: str) -> tuple[str, str, dict]:
    """把令牌拆成 `(app_key, 载荷段, 载荷 dict)`（形状 / 解码 / JSON / 字段类型全判）。

    ⚠️ 令牌的**形状判据只有这一处**：`unverified_payload`（认证层查库用）与 `verify`
    都经它，⛔ 不要在任何一侧再 `split(".")` 一次 —— 那会出现两份"几段、段能不能空"的规则。
    """
    if not isinstance(token, str):
        raise TokenError("令牌必须是字符串")
    segment, separator, signature = token.partition(".")
    if not separator or not segment or not signature:
        raise TokenError("令牌形状非法（须为 载荷段.签名段）")
    if "." in signature:
        raise TokenError("令牌形状非法（段数多于两段）")
    payload = _decode_payload(segment)
    return _require_str(payload, "ak"), segment, payload


def unverified_payload(token: str) -> tuple[str, str]:
    """**不验签**地取出 `(app_key, 载荷段)`；形状非法 ⇒ `TokenError`。

    ## 为什么认证层需要它（这一步是承重的，不是优化）

    §5.3 的校验顺序第一步就是「按 `ak` 查 `ApiApp`」——而 `ak` 只能从载荷里读，
    于是必须有一处"先读出 `ak`、再去查那一行的密钥、最后才验签"的入口。
    ⚠️ 这**不是**把准入判据前移：`ak` 是**公开**的（`app_key` 按设计不是机密，
    见 `models.ApiApp` 的类 docstring），拿它查库只决定"用哪把密钥验"；
    真正的准入判据仍是对那一行的 `app_secret` 重算 HMAC（`verify`）。
    ⇒ 攻击者改 `ak` 只会让验签失败（他拿不到那一行的密钥），不会因此通过。

    返回值里的**载荷段**给认证层的哑 HMAC 用（未命中应用时要付同一次摘要，
    没有这一串就没法照算）。
    """
    app_key, segment, _payload = _read_payload(token)
    return app_key, segment


def verify(token: str, app_secret: str, *, now: int | None = None) -> dict:
    """校验令牌；通过 ⇒ **已验签**的载荷 dict，失败 ⇒ `TokenError` / `TokenExpiredError`。

    ## 判据顺序（⛔ 顺序本身是判据的一半）

    1. **形状 / 解码 / JSON / 字段类型**（`unverified_payload` 那一步的同一套判据，
       经 `_decode_payload` 收口）；
    2. **签名**：按收到的**载荷段**用 `app_secret` 重算，`constant_time_equals` 比较
       ——⛔ 不是先比长度、⛔ 不是 `==`；
    3. **过期**：`now >= exp` 判过期（即 `exp` 是**死亡时刻**：到点即失效）。

    ⚠️ **签名的比较排在过期之前**：反过来的话，"改一个字符的令牌"与"合法但过期的令牌"
    回的是同一个错误类型，而调用方就没法把"你抄错了令牌"与"你去换一个"分开说
    （§5.4 只要求**凭据**那三档同文案，过期那一档要求的是"能说破"）。

    ## 过期边界（逐字钉住，`tests/test_openapi_token.py` 用 `now=` 参数对拍）
    - `now == exp` ⇒ **过期**（到点即失效，不留一秒钟的模糊区）；
    - `now == exp - 1` ⇒ 通过；
    - `now == exp + 1` ⇒ 过期。
    选"到点即失效"是为了让 `expires_in = 7200` 这句话**逐字为真**：
    `issue` 之后第 7200 秒手上那个令牌已经不能用了。

    ## 轮换密钥即全体失效（§5.3 第 3 条）
    签名用的就是 `app_secret` ⇒ 换了密钥，旧令牌的 HMAC 一律对不上（`TokenError`）。
    这条语义**不需要**额外代码，由"签的是密钥本身"结构保证；
    判据在 `tests/test_openapi_token.py`（认证层那一档）。
    """
    if not isinstance(app_secret, str) or not app_secret:
        raise TokenError("app_secret 必须是非空字符串")
    _app_key, segment, payload = _read_payload(token)
    _, _, provided_signature = token.partition(".")
    if not constant_time_equals(_sign(segment, app_secret), provided_signature):
        raise TokenError("令牌签名校验失败")
    expires_at = _require_int(payload, "exp")
    if _now_seconds(now) >= expires_at:
        raise TokenExpiredError("令牌已过期")
    return payload


def burn_timing_parity_signature(segment: str, app_secret: str = TIMING_PARITY_SECRET) -> None:
    """照算一次签名并**丢弃结果**（"哑 HMAC"，§5.3 的时序对拍）。

    ## 为什么它在这个模块里、而不是认证层自己写一句 `hmac.new`

    "怎么算这个签名"只有 `_sign` 一处实现（见它的 docstring）；认证层的未命中分支需要
    **付同一次代价**却**不使用结果**，所以这里给一个语义明确的出口：名字里就写着
    "这次计算的产物没人要"。
    ⚠️ 默认密钥是 `TIMING_PARITY_SECRET`（假密钥，⛔ 与任何真实凭据无关）。
    """
    _sign(segment, app_secret)
