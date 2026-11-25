"""三个登录口的**温和限流**（D-0332；业务方「加上登录限流，不要太高了」）。罩住哪三个口（= 审计的三个零闸登录口）：① 管理后台登录 `POST /api/auth/login`（`apps/core/auth_views.py::LoginView`）；② 打手 App 密码登录 `POST /api/app/auth/login/`（`apps/accounts/app_auth_views.py::AppLoginView`）；③ 公众号绑定确认页 `POST /wechatmp/bind-confirm` 的 `action=login`（`apps/wechatmp/bind_views.py::WechatMpBindConfirmView._post_login`）。⛔ **开放平台 / 打手开放 API 的两条 OAuth 换令牌口不在本模块**（`/api/open/oauth/token` 与 `/api/taker/oauth/token`）：它们各有**两道自己的闸**（按来源 IP 的失败闸 20 次/60 秒 + 兜底额度 600 次/60 秒，见 `apps/openapi/api/token_views.py` 与 `apps/openapi/rate_limit.py`）⇒ 再叠一道就是同一件事两套判据。
两维阈值（温和档，全部可经 settings 覆盖；读值都在函数体里 `getattr`，⛔ 不在 import 期读）：来源 IP 维——失败 ≥ **20** 次 / **15 分钟**窗口 ⇒ 该 IP 三个口**一起**拒 **30 分钟**（`LOGIN_IP_FAILURE_LIMIT` / `LOGIN_IP_FAILURE_WINDOW_SECONDS` / `LOGIN_IP_BLOCK_SECONDS`）；账号维（提交的用户名）——失败 ≥ **10** 次 / **15 分钟**窗口 ⇒ 该账号锁 **15 分钟**（`LOGIN_ACCOUNT_FAILURE_LIMIT` / `LOGIN_ACCOUNT_FAILURE_WINDOW_SECONDS` / `LOGIN_ACCOUNT_BLOCK_SECONDS`）。**数「失败」不数「尝试」**：只有 `authenticate` 判负的那次才记一笔（注册限流数尝试、拦的是「枚举手机号」，登录这条拦的是「猜密码」；数尝试会让输错两次的正常人 + 同办公室其他人的成功登录互相挤压额度）。**IP 维是兜底闸、账号维是定向闸**：换着用户名喷的撞 IP 维；盯一个账号喷的（换 IP / 分布式）撞账号维——账号维的桶键是被打的那个账号本身，攻击者**造不出新桶**（M17 教训反着用：桶键必须是「被保护的东西」，⛔ 不是攻击者可控可随手换的令牌）。**两维桶键都带租户 schema**（默认 cache 进程级、跨租户共享，理由同 `accounts/app_auth_views._app_plane_bucket_key`）；**账号键只存哈希**——提交的用户名是请求方可控、任意长度的输入，直接拼进 cache 键等于给进程内存开「键没有上界」的写入面；SHA-256 截定长十六进制还把同一账号的多种写法（带前后空格）归并进同一个桶。⚠️ **已知软限（如实登记，同 D-0251④）**：默认 cache 是 `LocMemCache`（进程内），生产 gunicorn `--workers 2` ⇒ 实际上限最坏 = 2 × 阈值；要严格按设定值得换共享存储（Redis 是新增基础设施，不引入）。
两段式（计数桶 + 锁标记）——为什么不是「超了就拒」的单桶：IP 维的窗口（15 分钟）与锁期（30 分钟）不同长；账号维要求「**锁** 15 分钟」从触线那刻起算、不是「第一次失败起算的固定窗口滚过就放」⇒ 失败记进计数桶（TTL = 窗口），触线那一刻另设一把**锁标记**（TTL = 锁期），开闸检查只看标记；代价是多一对键，换来「窗口/锁期各说各的」。边界语义（与 `realname/rate_limit.py` 同族）：第 `limit` 次失败**当场触线**设标记（那一次本身已按各口既有失败信封答了，记笔与判超同一次完成、判在读数之后）；第 `limit + 1` 次起开闸检查见到标记 ⇒ **在 `authenticate` 之前**直接拒（被挡的请求不查库、不算口令哈希）；**被挡的请求不计失败**——它们根本没走到 `authenticate` ⇒ 锁不会自我续期，锁期 TTL 到点即恢复、不做任何手工清理（cache TTL 就是恢复机制）。**成功登录清零**：`clear_login_failures()` 只删**两维的计数桶**（口令对了 ⇒ 之前的错都是同一个人的手滑，别让他后半场被前半场拖累）；**锁标记不删**——锁着的时候请求在 `authenticate` 之前就被拒了，结构上不存在「锁着还能成功」的路径，成功那一刻还能顺带清掉的两维标记反而说明闸有洞。
"""

from __future__ import annotations

import hashlib
import ipaddress

from django.conf import settings
from django.core.cache import cache
from django.db import connection

# ── 温和参数（业务方「不要太高了」：20 次/15 分钟 ⇒ 拒 30 分钟；10 次/15 分钟 ⇒ 锁 15 分钟）──
DEFAULT_IP_FAILURE_LIMIT = 20
DEFAULT_IP_FAILURE_WINDOW_SECONDS = 900  # 15 分钟
DEFAULT_IP_BLOCK_SECONDS = 1800  # 30 分钟
DEFAULT_ACCOUNT_FAILURE_LIMIT = 10
DEFAULT_ACCOUNT_FAILURE_WINDOW_SECONDS = 900  # 15 分钟
DEFAULT_ACCOUNT_BLOCK_SECONDS = 900  # 15 分钟

#: 锁定期那一档的文案：三个口共用**同一句**（与 `APP_REGISTER_RATE_LIMITED_MSG` /
#: realname `RATE_LIMITED_MSG` 同款），⛔ 不带字段名、不提账号、不提还剩几次——
#: 「这个用户名存在吗」不能从限流文案里读出来（任务口径 + openapi 取令牌端点同一纪律）。
RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"

#: 桶键前缀（防与 cache 里其它使用者撞键；schema 与取值由 `_ip_*_key` / `_account_*_key` 拼）。
_KEY_PREFIX = "login-rate"


def login_source_ip(request) -> str:
    """这次登录的**来源 IP**（过 `ipaddress` 校验；解析不了 ⇒ 空串）。取值口径**必须**走 `apps.openapi.authentication.client_ip`（nginx 覆盖写 `X-Real-IP`⇒ 生产上伪造不了；
    ⛔ 不要改成 `REMOTE_ADDR`——反代形态下它是环回地址，全世界的登录会共用一个桶，恰好是「误伤」的形状）。校验失败的归并口径与`accounts/app_auth_views._safe_ip` + `_app_plane_bucket_key` 同族：
    **空串一个显式桶**，只让「解析不出来源的请求」彼此共享额度，⛔ 不碰任何合法 IP 的桶（生产 nginx 覆盖写 ⇒ 正常请求永远解析得出来）。⚠️ 函数内导入：`apps.openapi` 是租户 app，而本模块被 `apps/core/auth_views.py`在 urlconf 载入期 import——放模块级会让平台层的载入链挂上 openapi 的模型模块。"""
    from apps.openapi.authentication import client_ip

    raw = client_ip(request)
    if not raw:
        return ""
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return ""
    return raw


def _account_ident(username) -> str:
    """账号维的桶取值：**提交的用户名**的 SHA-256 十六进制（定长 64，见模块 docstring）。
    `strip()` 只为把「同一账号带了个尾空格的两次提交」归进同一个桶；
    `authenticate`拿到的仍是视图层的原值，这里不动它。空 / 缺席 ⇒ `"-"` 一个显式桶（连账号名都没给的请求彼此共享额度）。"""
    raw = str(username or "").strip()
    if not raw:
        return "-"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ip_fail_key(ip) -> str:
    return f"{_KEY_PREFIX}-ip-fail:{connection.schema_name}:{ip or ''}"


def _ip_block_key(ip) -> str:
    return f"{_KEY_PREFIX}-ip-block:{connection.schema_name}:{ip or ''}"


def _account_fail_key(username) -> str:
    return f"{_KEY_PREFIX}-acct-fail:{connection.schema_name}:{_account_ident(username)}"


def _account_block_key(username) -> str:
    return f"{_KEY_PREFIX}-acct-block:{connection.schema_name}:{_account_ident(username)}"


def _bump(key: str, limit: int, window: int) -> bool:
    """固定窗口记一笔；返回「这次是否**触到**阈值」（`count >= limit`）。
    写法照 `apps/realname/rate_limit.py`（`cache.add` + `incr`，理由见模块 docstrin
    g 的基建选型一节，⛔ 不在这里重抄一遍）。第 `limit` 次失败返回 True ⇒ 调用方设锁标记；那一次的响应仍按各口**既有**失败信封出（记笔与判超同一次完成，判在读数之后）。"""
    if cache.add(key, 1, timeout=window):
        return limit <= 1  # 本窗口第一次失败：TTL 在这一刻设上；阈值 ≤ 1 时它就是触线那次
    try:
        count = cache.incr(key)
    except ValueError:
        # `add` 判负到 `incr` 之间键恰好滚过窗口（TTL 到期的边界竞态）⇒ 重建桶。
        # 极端竞态下少记一次：与 D-0251 ④ 登记的软限同族，如实接受。
        cache.set(key, 1, timeout=window)
        return limit <= 1
    return count >= limit


def _ip_params() -> tuple[int, int, int]:
    """IP 维的 `(阈值, 窗口秒, 锁期秒)`。⛔ 必须在**调用时**读 settings（override_settings 要生效）。"""
    return (
        getattr(settings, "LOGIN_IP_FAILURE_LIMIT", DEFAULT_IP_FAILURE_LIMIT),
        getattr(settings, "LOGIN_IP_FAILURE_WINDOW_SECONDS", DEFAULT_IP_FAILURE_WINDOW_SECONDS),
        getattr(settings, "LOGIN_IP_BLOCK_SECONDS", DEFAULT_IP_BLOCK_SECONDS),
    )


def _account_params() -> tuple[int, int, int]:
    """账号维的 `(阈值, 窗口秒, 锁期秒)`。读值时机理由同 `_ip_params`。"""
    return (
        getattr(settings, "LOGIN_ACCOUNT_FAILURE_LIMIT", DEFAULT_ACCOUNT_FAILURE_LIMIT),
        getattr(settings, "LOGIN_ACCOUNT_FAILURE_WINDOW_SECONDS", DEFAULT_ACCOUNT_FAILURE_WINDOW_SECONDS),
        getattr(settings, "LOGIN_ACCOUNT_BLOCK_SECONDS", DEFAULT_ACCOUNT_BLOCK_SECONDS),
    )


def login_blocked(*, ip, username) -> bool:
    """这次登录尝试**开闸先看一眼**：
    IP 或账号任一维有锁标记 ⇒ `True`（调用方在`authenticate` 之前直接按本口既有的失败形状回固定文案）。
    ⚠️ 两个口传进来的 `ip` / `username` 语义：`ip` = `login_source_ip()` 的返回值（合法 IP 文本或空串），`username` = 请求里提交的用户名原值（模块内部自己归一）。"""
    if cache.get(_ip_block_key(ip)):
        return True
    if cache.get(_account_block_key(username)):
        return True
    return False


def record_login_failure(*, ip, username) -> None:
    """记一次**凭据失败**（`authenticate` 判负的那一支才准调它）：两维各记一笔，
    触到阈值的那次顺手设锁标记（TTL = 锁期）。被挡住的请求⛔ 不进这里（见模块 docstring）。"""
    ip_limit, ip_window, ip_block = _ip_params()
    if _bump(_ip_fail_key(ip), ip_limit, ip_window):
        cache.set(_ip_block_key(ip), 1, timeout=ip_block)
    acct_limit, acct_window, acct_block = _account_params()
    if _bump(_account_fail_key(username), acct_limit, acct_window):
        cache.set(_account_block_key(username), 1, timeout=acct_block)


def clear_login_failures(*, ip, username) -> None:
    """成功登录后把 IP 和账号的失败计数清零。

    ⛔ 不删锁标记：锁着的时候请求在 `authenticate` 之前就被拒了，「锁着还能成功」
    在结构上不存在——能在这里顺带删到的标记只说明闸有洞（理由见模块 docstring）。
    """
    cache.delete(_ip_fail_key(ip))
    cache.delete(_account_fail_key(username))