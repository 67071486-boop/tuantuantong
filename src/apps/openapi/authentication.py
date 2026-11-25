"""开放平台对外端点的**令牌认证**（Bearer；v2 设计文档 §5.2 / §5.3）。

本模块只做一件事：把一个带 `Authorization: Bearer <令牌>` 的请求，判成「通过（并且是哪个
`ApiApp`）」或「401 + 一句固定的 msg」。**令牌的字节级规则一行都不在这里** —— 它们全在
`apps.openapi.tokens`（纯函数）；本模块只负责把请求里的东西**喂**进去、按 §5.2 的顺序判、
把失败翻成 401。⛔ 在这里再算一次签名就等于同一个事实有两套判据（`AGENTS.md` §5-E）。

## §5.2 的七步

| 步 | 判据 | 不过时 |
|---|---|---|
| 1 | `Authorization: Bearer <token>` 能取出令牌 | 401 `应用凭据无效` |
| 2 | 按令牌里的 `ak` **一次索引查询**命中一行（未命中 ⇒ **哑 HMAC**） | 401 `应用凭据无效` |
| 3 | 用那一行的 `app_secret` **重算 HMAC**（常量时间比较） | 401 `应用凭据无效` |
| 4 | `exp` 未到 | 401 `令牌已过期` |
| 5 | `state == enabled` | 401 `应用凭据无效` |
| 5.5 | ⭐ **绑定账号必须可用**（`spec.bound_user_field` 非 `None` 的族才判，D-0333） | 401 `应用凭据无效`（与第 5 步**逐字同一句**，⛔ 不说破「账号被停用」） |
| 6 | `ip_allowlist` 为空 **或** 来源 IP 在白名单内 | 401 `来源 IP 不在白名单内` |
| 7 | 通过 ⇒ 挂 `request.auth` / `request._api_app`，返回 `(AnonymousUser(), app)` | — |
| **8** | ⭐ **锁定应用的端点白名单**（`spec.locked_app_kind` 非 `None` 且该行是那一类时才判，见下） | **403 + 业务码 403**（D5 的语义，形状照本仓既有那一档 403） |

⭐ **第 8 步（企微群码线 §6.2 / D3-D5）**：**锁定应用**
（`ApiAppKind.WECOM_GROUP_QR`）只允许打
`apps/openapi/permissions.py::WECOM_GROUP_QR_ALLOWED_ENDPOINTS` 里逐字列出的
`(HTTP 方法, url_name)` 六条 ⇒ 其余**一律拒**（deny-by-default）。
⚠️ **通用应用（`ApiAppKind.GENERAL`）一个字节都不受影响**。
打手侧整族也不受影响，而且是结构性的：
本类是**两族共用**的基类（打手侧子类只覆盖 `spec = TAKER_SPEC`），而 `TakerApp`
**刻意没有** `kind` 这一列 ⇒ 这一步**必须经 `spec.locked_app_kind` 判"这一族有没有锁定应用"**，
⛔ 不能去读 `app.kind`（那样打手侧每个请求都 500）。`TAKER_SPEC.locked_app_kind` 是 `None`
⇒ 那一步的第一行就返回，**连一次属性读都不会发生**。
⚠️ 判据 / 文案 / 失败形状的唯一出处都在 `apps/openapi/permissions.py`，本模块只调一次
（⛔ 不在这里判第二遍、也⛔ 不在这里抄那句话）。

## 为什么成功时返回 `(AnonymousUser(), app)`、⛔ 不是 `(None, app)`

DRF 的 `Request.user` setter 会把值**回写到内部那个 Django `HttpRequest`**，而
`RequestMetricsMiddleware` 是在**响应之后**读 `request.user` 的。回写 `None` 会让
「先 `user is not None`、再 `user.is_authenticated`」那类写法到处埋雷（本仓就有这样的
共享工具）；返回一个真 `AnonymousUser()` 则与「未登录」**完全同形**、零副作用。
⚠️ 连带一条：对外端点**不能**用 `permission_classes = [IsAuthenticated]`
（`AnonymousUser.is_authenticated` 是 `False` ⇒ 合法请求会被判 401）；v2 的写端点按设计
**不装任何权限类**（`permission_classes = []`，设计文档 §13.10），⛔ 也别顺手改成前者。

## 三条「不做会被看穿 / 会被打穿」的措施

1. ⭐ **未命中分支的哑 HMAC**（§5.4）：命中分支要多做一次 HMAC 计算 + 常量时间比较，
   未命中直接返回 ⇒ **响应时间可分辨「有没有这个应用」**，与「回同一句话、不给应用枚举面」
   自相矛盾。这里在未命中时按收到的那一段载荷照算一遍、结果丢弃
   （`tokens.burn_timing_parity_signature`）。
2. ⭐ **按来源 IP 的失败计数**（D11）：D11 的配额**按应用**分桶，而「乱猜的凭据」谁都不属于、
   **不进任何桶** ⇒ 枚举请求完全不受限。所以这里追加一条按来源 IP 的固定窗口计数，与取令牌
   端点**共用同一套 DB 计数**（`OpenApiRateBucket`）—— 计数实现只有一份（`rate_limit`）。
3. **失败一律 401 + 统一信封**，区分靠 `msg`（认证层不用业务码表）。⛔ 不回显任何框架 /
   异常原文（同族先例：peer bind 的 `PEER_BIND_BAD_BASE_URL_MSG`）。

## 本模块的历史（⛔ 不是「从来没写过」）

本模块原本**还有一条 HMAC 签名认证路径**（v1 面：入站三个签名头 + ±300 秒时间窗 + 八步判定 +
五句失败文案），随「开放平台 v2」的收口任务（T10）**整体删除** ——
判据是 `src/**` 里搜那一族主词命中为零（设计文档 §13.5-2）。旧路径的逐条设计原文见
git 历史与设计文档 §13.1。

⚠️⭐ **主词边界（设计文档 §13.21-2）**：**出站推送**用的是**同族的另外三个头**
（`X-TT-Timestamp` / `X-TT-Delivery` / `X-TT-Signature`，`apps/openapi/outbound.py`），
它们**必须仍在**；把「`X-TT-` 这一族」当主词去搜「应命中为空」会把这套东西一起误删。

本模块今天只保留**当时两条路共用**的那几件（来源 IP 取值、白名单匹配、按来源 IP 的失败计数）
—— 它们由 Bearer 路径与取令牌端点使用。
"""
from __future__ import annotations

import ipaddress
from typing import NoReturn

from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication

from apps.openapi import permissions, rate_limit, tokens
from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec, app_is_enabled

# ⚠️⭐ T2：**这一行不参与任何判定** —— 它是**再导出**（re-export），
# 因为既有用例把桩打在**这个模块的 `ApiApp` 名字**上：
# `mock.patch.object(auth_module.ApiApp.objects, "filter", …)`
# （`tests/test_openapi_auth.py` 的两条"闸门排在查库之前 / 对照：桩接得上"）。
# ⚠️ 那两条**是承重的**（E-0029：桩没接上时"不查库"那条断言会恒真）⇒ ⛔ 不许为了
# "名字干净"把它去掉。判定一律走 `spec.app_model`（**同一个类对象** ⇒ 桩照样拦得住，
# `mock.patch.object` 打的是那个 manager **实例**上的属性）。
from apps.openapi.models import ApiApp  # noqa: F401  （再导出，见上面那条注释）

#: §5.4 那一句对外文案（**唯一出处 = spec**，⛔ 别在调用点再抄一遍字面量）。
#: ⚠️ 本名字是既有用例 import 的那个（`tests/test_openapi_auth.py::FailureMessageContractTests`
#: 直接拿它比字面量）⇒ 保留为 spec 的**别名**，⛔ 判据一条没改。
IP_NOT_ALLOWED_MSG = OPENAPI_SPEC.ip_not_allowed_msg

#: ⭐ **按来源 IP 的失败计数**（D11）。
#:
#: ## 为什么它与「应用配额」是两件事、⛔ 不能合并
#: D11 的极宽兜底闸是**按应用**分桶的（6000 次/分钟/应用）。而攻击面里最要紧的那一条是
#: **猜凭据**：猜出来的 key 谁也不属于、`ApiApp` 里查不到 ⇒ 它**不进任何一个应用桶**
#: ⇒ 枚举请求在那个维度上完全不受限。所以这一条必须**按来源 IP** 分桶，并且与配额
#: **各算各的**（一个维度管"某个应用别把机器打满"，另一个维度管"某个来源别在这里穷举"）。
#: ⚠️ 代价（如实登记）：① 计数落 **DB 行**（不是进程内缓存 —— 多 worker 下内存桶实际更宽，
#: 见下面 `_count_failure` 的改准块）；② 阈值是**每 IP 每租户**的 —— 同一个来源把凭据写错
#: 21 次，之后**一分钟内**连正确的请求也会被挡（这是有意的：那条路正是"猜"的形状；
#: 窗口很短，第三方自纠后最多等一分钟）。
#: ⚠️⭐ T2：**值与窗口取自 spec**（⛔ 数字不在这里写第二遍）。
#: 本名字是既有用例 import 的那个（`tests/test_openapi_auth.py` 按它算阈值、
#: `tests/test_openapi_token.py` 钉它是 20）⇒ 保留为 spec 的**别名**。
IP_FAILURE_MAX = OPENAPI_SPEC.failure_max
IP_FAILURE_WINDOW_SECONDS = OPENAPI_SPEC.failure_window_seconds

#: ⛔⭐ **现行口径（T10）**：本处原还有一个**缓存键前缀**常量（v1 面那条签名路径用来
#: 拼 `LocMemCache` 的键）。T3 把计数换成 DB 行之后它**已不再被任何路径读取**，T10 随 v1 面
#: 一起删除（设计文档 §13.1）。今天的计数键是 `(scope="ip", ident=来源 IP, window_start)`
#: —— 见下面 `_failure_count` 的改准表。


def client_ip(request) -> str:
    """请求的**来源 IP**（§5.2 第 6 步与 IP 失败计数共用同一个取值口径）。

    ## 取值三级优先（spec §20 拍板；⛔ 顺序不能调）

    1. **`X-Real-IP`** —— 我们这台 nginx **覆盖写**的（`proxy_set_header X-Real-IP $remote_addr`，
       不是 append）⇒ 调用方**伪造不了**它 ⇒ **首选**；
    2. 没有它 ⇒ **`X-Forwarded-For` 的【最右段】**
       —— nginx 用的是 `$proxy_add_x_forwarded_for`，它把新地址**追加在最右**，所以**最右段才是我们
       这台 nginx 亲手加的那个真客户端**；
    3. 两个头都没有 ⇒ `REMOTE_ADDR`（直连开发 / 测试的形状；反代形态下它是环回地址）。

    ## ⛔ 为什么**绝不能**取 `X-Forwarded-For` 的**最左段**（这一条是改准，不是偏好）

    `$proxy_add_x_forwarded_for` 的语义是「**把 `$remote_addr` 追加到调用方送来的那个头后面**」
    ⇒ **最左段完全由调用方控制**。而 `ip_allowlist` 是一条**安全控制** ⇒ 取最左段等于
    「任何人塞一个 `X-Forwarded-For: <白名单里的 IP>` 就绕过白名单了」（nginx 会把真 IP 追在后面，
    而那段我们不读）。它的症状是**假保证**：租户以为白名单生效了，其实没有 —— 比"没有白名单"
    更糟，因为它让人停止怀疑。

    ⚠️ 真机实测出处（总指挥在 `ssh tencent` 上读的 `/etc/nginx/proxy_params`，
    ⛔ 不是读本仓 `deploy/` 的推断）：
    ```
    2:proxy_set_header X-Real-IP $remote_addr;
    3:proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    ```
    ⇒ 两行的**语义差别就是这里选 `X-Real-IP` 优先、且 XFF 取最右的理由**。

    ## ⚠️ 信任前提（写明了，⛔ 别当它是通用解法）
    本取值假定「**本机只有一台可信反代**」（nginx 与应用在 `127.0.0.1` 上直连）。
    **将来加 CDN / 多层反代必须重新评估**：那时"最右段"会变成 **CDN 的 IP**（而不是客户端），
    白名单与失败计数都会按 CDN 的地址分桶 —— 得改成按可信代理跳数取值（`NUM_PROXIES` 那一类），
    属**新基础设施**，本期不做。

    ## 边界
    - 本函数只回答"用哪个 IP 判"，⛔ **不管**它能不能解析成合法 IP（解析与匹配在 `_ip_allowed` 里做）；
    - 头存在但是空白 ⇒ 视同没有（继续往下一级找），⛔ 不返回空串去当 IP 用；
    - `X-Forwarded-For` 的分隔是**逗号**，取最右段前逐段 `strip()`。
    """
    real_ip = _metadata_str(request, "HTTP_X_REAL_IP")
    if real_ip:
        return real_ip
    forwarded = _metadata_str(request, "HTTP_X_FORWARDED_FOR")
    if forwarded:
        # ⚠️ 取**最右**那一段（= 我们这台 nginx 追加的），⛔ 不是最左（调用方可控）。
        rightmost = [piece.strip() for piece in forwarded.split(",") if piece.strip()]
        if rightmost:
            return rightmost[-1]
    return _metadata_str(request, "REMOTE_ADDR")


def _metadata_str(request, key: str) -> str:
    """取一个 `META` 值并归一成**去首尾空白的字符串**（非字符串 / 缺失 ⇒ 空串）。

    ⚠️ 单独抽出来是为了让三个头的取值**同一口径**：`client_ip` 里若一处 `or ""`、
    另一处不判类型，`X-Forwarded-For: None` 这种（测试夹具直接塞 `None` 的写法）会在
    `.split()` 上抛 `AttributeError` ⇒ **500**，而症状是"伪造了一个怪头就把端点打挂了"。
    """
    raw = request.META.get(key)
    return raw.strip() if isinstance(raw, str) else ""


def _iter_allowlist_entries(raw: str):
    """把 `ip_allowlist` 拆成条目：**每行一个**、行内再按逗号 / 空白分。

    ⚠️ 只做拆分，不做校验 —— 校验与匹配在 `_ip_allowed` 里（一处判据）。
    """
    for line in (raw or "").splitlines():
        for piece in line.replace(",", " ").replace(";", " ").split():
            yield piece.strip()


def _ip_allowed(raw_allowlist: str, source_ip: str) -> bool:
    """来源 IP 是否在白名单内（§5.2 第 6 步）。

    ## 口径（逐条都能被用例证伪）
    - **白名单为空 ⇒ 放行**（`ip_allowlist` 的默认值就是空串，「空 = 不限制」是 §5.1 的定值）；
    - 条目可以是**单个地址**或**网段**（`ip_network(entry, strict=False)` 两种都吃）；
    - 来源 IP 与条目都走 `ipaddress`，于是 IPv4 / IPv6 与 `::ffff:a.b.c.d` 这类
      IPv4-mapped 写法由标准库统一（⛔ 不自己写字符串前缀比较 —— 那会在网段上直接错）；
    - ⚠️ **来源 IP 解析不了 ⇒ 不在白名单内**（fail-closed）：它可能是伪造头，也可能是
      nginx 没配上；两种情况下"按白名单放行"都说不通。
    - ⚠️ **白名单里某一条本身写错（拼错的 IP）⇒ 跳过那一条**，其余条目照常生效。
      这是**有意**的取舍：另一种做法（任何一条非法就整体拒绝）会让运维在页面上打错一个字
      ⇒ **应用当场全部 401**，而错的那一条**在错误消息里看不出来**（§5.4 不许回显原文）
      ⇒ 排障成本远大于收益。判据见用例里「一条乱码 + 一条命中 ⇒ 放行」。
    """
    entries = [entry for entry in _iter_allowlist_entries(raw_allowlist) if entry]
    if not entries:
        return True
    try:
        source = ipaddress.ip_address(source_ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if source.version == network.version and source in network:
            return True
    return False


def _failure_count(source_ip: str, *, spec: CredentialSpec) -> int:
    """当前窗口内该来源已累计的失败次数（**只读**，⛔ 不计数）。

    ⛔⭐ **现行口径（T3）**：存储是 DB 行（不是 `LocMemCache`）。

    | | 旧 | 新（T3 起） |
    |---|---|---|
    | 存哪 | `LocMemCache` —— **进程内全局**，多 worker 下每个进程各一份 | 桶模型的 **DB 行**（当前租户 schema 内） |
    | 键 | `<前缀>:<schema>:<来源 IP>` 的缓存键 | `(scope="ip", ident=来源 IP, window_start)`——⛔ 不带 schema（行本身住在租户 schema 内） |
    | 窗口 | 每次写重设 TTL（60 秒） | **固定窗口**（`now // 60`，与取令牌端点的兜底额度同口径） |

    ⚠️ **为什么这里用只读的 `current_count` 而⛔ 不是 `consume`**（这一条是判据，不是风格）：
    `consume` 会**加一**，而本函数是**每次请求**都要调的一次"先看一眼"。若把它写成 `consume`，
    则**合法请求也会把失败计数加一** ⇒ 闸门从「这个来源猜了多少次」变成「这个来源来了多少次」
    —— 一个正常调用方连打 21 次就会被拉黑一分钟。那正是明令不许变的「行为口径」。
    ⛔ 计数仍然只有**一个**出口（`rate_limit.consume`，只在 `_count_failure` 里调）。

    ## ⭐ R1：来源**解析不了** ⇒ 直接回 `IP_FAILURE_MAX`（**fail-closed**）

    判据 `rate_limit.is_unparsable(source_ip)` 为真时，本函数**不去读任何桶**（也⛔ 不建行），
    直接回 `IP_FAILURE_MAX` —— 那个数**正好让调用方那条 `>= IP_FAILURE_MAX` 成立** ⇒ 闸门当场拦。
    ⚠️ 读法：这个返回值**不是**"它已经失败了 20 次"这个事实，而是「**无法识别来源 ⇒ 不放行**」
    这个判决（两条闸门的判据都是 `>=`，所以取这个数就是"判决 = 拦"）。⛔ 别把它当成计数。
    ⚠️ 为什么不用"归并到一个占位桶"的老做法：那是**共享毒桶**（谁都能灌满它，
    然后把所有同样不可解析的来源整片打死），理由与判据见 `rate_limit.is_unparsable`。

    ## ⚠️⭐ T2：**读哪张桶表 / 哪个 scope / 哪个窗口**全部经 `spec`

    改造前这四件（模型 / `Scope.IP` / 阈值 / 窗口）在本函数里是硬编码的
    ⇒ 打手侧一旦复用它，**两边共用同一行失败计数**：一边穷举凭据会把另一边同一来源
    一起拉黑（spec §4 第 6 条）。

    ⭐⭐ **T2：`spec` 必填 keyword-only，⛔ 没有默认值**。

    改之前它的签名是 `spec: CredentialSpec = OPENAPI_SPEC`，于是"漏传"这件事**静默**
    按上家侧处理 —— 而同一类的另一处（`_find_existing` / `_prune` 那几个 `idem_model`）
    是必填的 ⇒ 同一种错在两个地方一个响亮一个静默，那是最坏的一种不一致（评审点名的
    "静默串台面"）。现在漏传 = **构造期就 `TypeError`**。
    ⚠️ **行为逐字不变**：生产调用点（本类的 `authenticate` 与
    `ip_failure_gate_already_exceeded`）本来就显式传 `self.spec` / `spec`，
    而那就是 `OPENAPI_SPEC` ⇒ 桶表 / scope / 阈值 / 窗口与改前**同一个值**。
    """
    if rate_limit.is_unparsable(source_ip):
        return spec.failure_max
    return rate_limit.current_count(
        scope=spec.failure_scope,
        # ⚠️ ⛔ 不直接把 `source_ip` 塞进去：它是**请求头里的原文**（可能超长 / 不是 IP），
        # 而 `ident` 列只有 64 字符 ⇒ 规范化是承重的（理由与判据见 `rate_limit.ip_ident`）。
        ident=rate_limit.ip_ident(source_ip),
        window_seconds=spec.failure_window_seconds,
        spec=spec,
    )


def _count_failure(source_ip: str, *, spec: CredentialSpec) -> None:
    """记一次失败（固定窗口：落在**当前窗口那一行**上，跨窗口自动换行）。

    ## ⛔⭐ 现行口径（T3）

    - 旧写法是"读-改-写 + 每次写重设 TTL"（并发下会少记几次，窗口其实是"最后一次失败之后
      60 秒"的滑动形状）；
    - 新写法是 `rate_limit.consume(…)` 的**一次** `INSERT … ON CONFLICT DO UPDATE … RETURNING`
      ⇒ 并发下不会少记，窗口是**固定窗口**（`now // 60`）。
    - ⚠️ **`consume` 的判定结果在这里被忽略**（`_count_failure` 只记、不判）：闸门的判定在
      `_failure_count(...) >= IP_FAILURE_MAX` 那一处，⛔ 位置与语义都没动
      ⇒ 「错满 `IP_FAILURE_MAX` 次之后，合法令牌也被拦」这条既有行为一字不变
      （判据：`tests/test_openapi_auth.py::ApiAppFailureCounterTests`，那几条断言没改）。
      ⛔ 对外可见行为（文案 / 401 / 阈值 20 与 60）一个都没动。

    ⭐ R1：来源**解析不了** ⇒ **直接返回、⛔ 一行都不写**（理由见
    `rate_limit.is_unparsable`：归并到一个占位桶 = 共享毒桶）。⚠️ 这不改变任何可见行为：
    那种来源在闸门那一处**已经**被判"拦"（`_failure_count` 回 `IP_FAILURE_MAX`），
    根本走不到记账这一步；这一句是"承重逻辑缺一不可"的那种保险（把判据做在**记**这一侧，
    而不是指望调用顺序）—— 判据见 `tests/test_openapi_rate_limit.py::UnparsableSourceIpTests`。

    ⚠️⭐ T2：落**哪张桶表 / 哪个 scope / 哪个窗口**经 `spec`（口径与
    `_failure_count` 逐字相同：读与写必须看同一张表 / 同一个 scope / 同一个窗口）。
    ⭐⭐ **T2：`spec` 必填 keyword-only**（⛔ 默认值已去掉；理由与 `_failure_count`
    的 docstring 逐字相同：漏传的后果是**静默**按上家侧的桶记 —— 一处响亮一处静默是最坏的形状）。
    """
    if rate_limit.is_unparsable(source_ip):
        return
    rate_limit.consume(
        scope=spec.failure_scope,
        # ⚠️ 规范化（`rate_limit.ip_ident`）：⛔ 不是"顺手加的防御"——见那个函数的 docstring。
        ident=rate_limit.ip_ident(source_ip),
        limit=spec.failure_max,
        window_seconds=spec.failure_window_seconds,
        spec=spec,
    )


def count_ip_failure(source_ip: str, *, spec: CredentialSpec) -> bool:
    """记一次「按来源 IP 的失败」，并回答**记完之后这个来源是否已经超限**（`True` = 超限）。

    ## 为什么要有一个"带判定"的出口（⛔ 不是给计数抄第二份）

    取令牌端点（`api/token_views.py`）需要这个判定：它超限时回的是 **OAuth2 的 429**
    （⛔ 不是本仓信封），所以它必须知道"这一次记完之后是不是已经过了阈值"。
    ⚠️ 计数实现**只有一份**（`rate_limit.consume`）；本函数存在的意义是把
    `(scope, ident, limit, window)` 这**四个参数**（它们是一个事实："按来源 IP 的失败闸"）
    收在一处 —— 抄到第二个调用点，改阈值时就会漂（`AGENTS.md` §5-E）。

    ⚠️ 边界口径（与 `consume` 的一致，逐字）：`count <= IP_FAILURE_MAX` ⇒ 这次仍算"记下了"
    ⇒ 返回 `False`（调用方照原样回 401）；第 `IP_FAILURE_MAX + 1` 次 ⇒ 返回 `True`
    （调用方回 429，⛔ 不再是 401）。

    ⭐ R1：来源**解析不了** ⇒ 直接回 `True`（= **拒**），⛔ **不读也不写桶**。
    ⚠️ 调用的语义没变：`True` 的含义仍是"这一档别再往下走"，调用方（取令牌端点）照它既有的
    契约回那一档的答复（见 `api/token_views.py`）。理由与判据见 `rate_limit.is_unparsable`。

    ⚠️⭐ T2：`spec` 决定落哪张桶表 / 哪个 scope / 哪个阈值 / 哪个窗口
    （四个参数是**同一个事实**："这一族的按来源 IP 失败闸"）。
    ⭐⭐ **T2：`spec` 必填 keyword-only**（⛔ 默认值已去掉）。既有用例里那些
    "只传来源"的调用（`count_ip_failure("::1")`）**同一次**补上了 `spec=OPENAPI_SPEC`
    这个实参 —— ⛔ 断言一条未改，改的只是"把原先由默认值兜住的那件事写出来"。
    """
    if rate_limit.is_unparsable(source_ip):
        return True
    allowed, _retry_after = rate_limit.consume(
        scope=spec.failure_scope,
        # ⚠️ 规范化（`rate_limit.ip_ident`）：⛔ 不是"顺手加的防御"——见那个函数的 docstring。
        ident=rate_limit.ip_ident(source_ip),
        limit=spec.failure_max,
        window_seconds=spec.failure_window_seconds,
        spec=spec,
    )
    return not allowed


def ip_failure_gate_already_exceeded(source_ip: str, *, spec: CredentialSpec) -> bool:
    """取令牌端点用的**早退**判据：这个来源的失败计数**已经超过** `IP_FAILURE_MAX` 吗？

    ## ⛔ 为什么这里是 `>`，而 Bearer 认证那条路是 `>=`（这一条是判据，不是笔误）

    | 谁 | 判据 | 效果 |
    |---|---|---|
    | Bearer 认证（`ApiAppBearerAuthentication.authenticate`） | `_failure_count(ip) >= IP_FAILURE_MAX` | 计数**正好等于** 20 的那一次请求就被拒（它自己不再被计数） |
    | 本函数（取令牌端点） | `_failure_count(ip) > IP_FAILURE_MAX` | 计数**正好等于** 20 时**仍放进来**，由 `count_ip_failure()` 里的 `consume` 判第 21 次（那次回 429）；**从第 22 次请求起**才走这条早退（不再查库、不再比密钥） |

    ⇒ 两条口径拒掉的**是同一批 attempt**：第 `IP_FAILURE_MAX + 1` = **21** 次尝试被拒
    （Bearer 由闸门当场拒；取令牌端点由 `consume` 的判定拒）。差别只在"谁去付那次计数查询"。
    ⚠️ **判据（⛔ 不是"看起来一样"）**：`tests/test_openapi_rate_limit.py::TokenEndpointFailureGateTests`
    的三条原始读数 —— ①前 20 次凭据失败都是 401；②**第 21 次 attempt 是 429**；③第 22 次
    （哪怕凭据是对的）直接 429 且**一次都不查 `ApiApp`**。

    ## ⚠️ 为什么不干脆写 `>=`

    两者用的是**同一行**计数（spec §13.11-1 ① 的"共用同一套 DB 计数"）。若这里也写 `>=`，
    那么**既有**用例 `tests/test_openapi_token.py::BearerAuthenticationTests::
    test_the_ip_failure_gate_blocks_even_a_valid_token` 会翻红 —— 它先用同一个 IP 攒 20 次
    **Bearer** 失败，**再从同一个 IP 换令牌**并断言那次 `issue_token()` 是 **200**。
    那条钉的是 **Bearer 路径**的既有行为（⛔ 不许为了让新闸更严去改它）
    ⇒ 换来的代价只是"取令牌端点比 Bearer 多容忍一次失败尝试"。

    ## ⭐ R1：来源**解析不了** ⇒ 直接 `True`（⛔ 不读桶）

    ⚠️ **这一支必须显式写**，⛔ 不能指望下面那个 `>`：`_failure_count` 对不可解析的来源回的是
    `IP_FAILURE_MAX`（= 20），而 `20 > 20` 是 `False` ⇒ 只靠它**拦不住**（那是给 `>=` 那一条
    闸门用的形状）。判据：`tests/test_openapi_rate_limit.py::UnparsableSourceIpTests` 里
    "取令牌端点 ⇒ 429 且一行桶都不写"那条。

    ⚠️⭐ T2：阈值与桶都经 `spec`（⛔ 数字 / 模型名不在这里写第二遍）。
    ⭐⭐ **T2：`spec` 必填 keyword-only**（⛔ 默认值已去掉；理由与 `_failure_count`
    的 docstring 逐字相同）。既有用例里那两处"只传来源"的调用**同一次**补上了
    `spec=OPENAPI_SPEC` 实参，⛔ 断言未动。
    """
    if rate_limit.is_unparsable(source_ip):
        return True
    return _failure_count(source_ip, spec=spec) > spec.failure_max


#: 授权头的方案名（契约里就是这个词）。⚠️ 取令牌端点的成功响应里 `token_type` 是**同一个词**
#: （v2 §5.1）⇒ 那边 import 本常量，⛔ 不再写一遍字面量。
BEARER_SCHEME = "Bearer"

#: ⭐ Bearer 认证失败的**统一文案**（v2 §5.4 的硬要求）：**令牌不对 / 应用不存在 / 应用停用**
#: 三档回**逐字相同**的这一句 —— 任何一档单独说破都会让这条路变成应用枚举口。
#: ⚠️ 取令牌端点（`api/token_views.py`）的 `error_description` **复用**本常量
#: （v2 §5.4 要求那三档与这里的口径逐字相同）⇒ 这句话的字面量只有**一处**（= spec）。
#: ⚠️⭐ T2：值取自 spec（本名字是既有用例 import 的那个 ⇒ 保留为别名）。
BEARER_INVALID_CREDENTIALS_MSG = OPENAPI_SPEC.invalid_credentials_msg

#: 令牌过期那一句。⚠️ **单独说破的理由**：这个分支排在验签**之后**，走得到它的调用方
#: 已经证明了它握着那一行的 `app_secret` ⇒ 说破"过期"不构成应用枚举面。
BEARER_EXPIRED_MSG = OPENAPI_SPEC.expired_msg


def _bearer_token(raw: str) -> str | None:
    """从 `Authorization` 头的**原始值**里取令牌；取不出可用令牌 ⇒ `None`。

    ## 契约与宽容（逐条写清楚，⛔ 别把宽容读成契约）
    契约只有一种形状：`Authorization: Bearer <token>`（方案名 + **一个空格** + 令牌）。
    服务端额外容忍两件**客户端常见写法**，⛔ 不写进对外文档：
    - **方案名大小写不敏感**（`Bearer` / `bearer` / `BEARER` 等价 —— RFC 7235 的 auth-scheme
      本来就大小写不敏感）；
    - **两个词之间允许多个空白**（空格 / 制表符），头尾空白一律去掉（`_metadata_str` 先 `strip()`）。

    ⚠️ **令牌本身不做大小写 / 空白归一**：它是 base64url 串（`_` / `-` 是有意义的字符），
    任何 `lower()` 或"把中间空白去掉"都会让**两个不同的令牌**映射到同一份签名输入
    —— 那是"同一个事实两套判据"的同族错误。令牌内部有没有非法字符，由 `tokens` 的形状
    正则判（⛔ 不在这里判第二遍）。
    """
    parts = raw.split() if raw else []
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != BEARER_SCHEME.lower():
        return None
    return token or None


class ApiAppBearerAuthentication(BaseAuthentication):
    """对外端点的 **Bearer 令牌**认证（v2 §5.2 / §5.3；T2）。

    | 步 | 判据 | 不过时 |
    |---|---|---|
    | 1 | `Authorization: Bearer <token>` 能取出令牌 | 401 `应用凭据无效` |
    | 2 | 按令牌里的 `ak` **一次索引查询**命中一行（未命中 ⇒ **哑 HMAC**） | 401 `应用凭据无效` |
    | 3 | 用那一行的 `app_secret` **重算 HMAC**（常量时间比较） | 401 `应用凭据无效` |
    | 4 | `exp` 未到 | 401 `令牌已过期` |
    | 5 | `state == enabled` | 401 `应用凭据无效` |
    | 5.5 | ⭐ **绑定账号必须可用**（`spec.bound_user_field` 非 `None` 的族才判，D-0333） | 401 `应用凭据无效`（与第 5 步逐字同一句） |
    | 6 | `ip_allowlist` 为空 **或** 来源 IP 在白名单内 | 401 `来源 IP 不在白名单内` |
    | 7 | 通过 ⇒ 挂 `request.auth` / `request._api_app`，返回 `(AnonymousUser(), app)` | — |

    ⭐ **第 8 步**（本类**之外**的一张表）是**锁定应用的端点白名单**：它只对
    `spec.locked_app_kind` 指定的那一类行（上家侧 = `ApiAppKind.WECOM_GROUP_QR`）生效，
    通用应用**完全不受影响**，**打手侧整族**也不受影响（`TAKER_SPEC.locked_app_kind` 是
    `None` ⇒ 那一步第一行返回；⭐ **本类是两族共用的**，而 `TakerApp` 没有 `kind` 列
    ⇒ 这一步⛔ 不能读 `app.kind`，理由见 `permissions.py` 模块 docstring）；
    失败形状是 **HTTP 403 + 业务码 403**（⛔ 不是本表那四句 401；⚠️ spec D5 字面写的
    「HTTP 200」在本仓的异常处理器里产不出来，逐条见 `permissions.py` 的改准块）。判据 / 文案 / 形状的
    **唯一出处**都在 `apps/openapi/permissions.py`（模块 docstring 的七步表下方有逐条说明）
    ⇒ ⛔ 别在本类里再判一遍、也别在这里抄那句话。

    ## ⛔ 为什么这里**没有**第二份 `client_ip` / `_ip_allowed` / 失败计数 / `_mark_used`

    来源 IP 的取值三级优先、白名单匹配、按来源 IP 的固定窗口失败计数、`last_used_at`
    的那一条 `UPDATE` —— 这四件在本模块里各只有一份实现，本类**直接调**它们
    （模块级函数 / 本类自己的方法），⛔ 一件都没有抄第二份（`AGENTS.md` §5-E）。

    ## 为什么直接继承 DRF 的 `BaseAuthentication`（⛔ 不是抽一个共用基类）

    ⛔⭐ **现行口径（T10）**：本类原先**继承那条 HMAC 签名认证类**，唯一目的是复用
    它已经把形状定死的两个共用件（写 `last_used_at` 的那条 `UPDATE` 与 `NoReturn` 的 401 出口）。
    T10 把签名面整体删掉（设计文档 §13.1）⇒ 那两个共用件**搬进本类**、基类换回 DRF 的
    `BaseAuthentication`。
    **为什么是"搬进来"而不是"抽一个共用基类"**：今天**只剩一条**认证路径
    ⇒ 那个基类会是一个**永远只有一个子类**的空壳（`AGENTS.md` §5-E「防御性写法不要写成承重逻辑」：
    抽出来之后，去掉它没有任何用例会红）。等真出现第二条路径再抽，那时它有第二个消费者。
    ⚠️ **行为逐字节不变**：两个共用件的函数体与调用点一个字都没改（判据 =
    `tests/test_openapi_auth.py` / `tests/test_openapi_token.py` 里那批断言，一条未放宽）。

    ## 与「不复用会话认证」有关的一条（⛔ 不要"顺手统一"掉）

    - **`authenticate_header` 回 `Bearer`**（会话认证那条回别的方案名）。
    - ⛔ **不判时间戳窗口**：有效期在令牌里（`exp`），⛔ 不用请求头里的时间戳。

    ## 过期那一档**不计入**按来源 IP 的失败闸（有意，给理由）
    走得到它的请求已经带着一个**验签通过**的令牌 ⇒ 它不是"猜凭据"的形状；
    把它算进那条闸只会误伤合法调用方。⚠️ 于是本类计数的**档位**是：「取不出令牌 / 形状非法」
    与「验签不过」—— 都是"这里在猜"的那两档，停用 / 过期 / 白名单三条**都不计数**。

    ## ⚠️⭐ T2：本类**自带一份 spec**（`spec` 类属性）

    查哪张凭据表 / 比哪一列密钥 / 哪个 `state` 值算可用 / 失败计数落哪张桶表 / 五句文案
    —— 全部经 `self.spec` 取（改造前是逐处硬编码）。⇒ 第二族（打手侧）只要
    `class TakerAppBearerAuthentication(ApiAppBearerAuthentication): spec = TAKER_SPEC`
    （或照同一形状写一个子类），⛔ 不必把本类抄第二份。
    ⚠️ 默认值 `OPENAPI_SPEC` = 改造前那条硬编码路径 ⇒ 行为逐字节不变。
    """

    #: 本类服务哪一族凭据（⛔ 子类**必须**覆盖它，否则会去查上家侧那张表）。
    spec: CredentialSpec = OPENAPI_SPEC

    def authenticate(self, request):
        # ⚠️ 先取 spec：后面每一处（闸门 / 查库 / 比密钥 / 状态 / 白名单 / 文案）都经它。
        spec = self.spec
        token = _bearer_token(_metadata_str(request, "HTTP_AUTHORIZATION"))
        source_ip = client_ip(request)
        # ⭐ 按来源 IP 的失败闸：**在查库之前**判。放在这里有两个理由：
        # ① 被挡住的请求不再付一次索引查询 + 一次 HMAC（闸的意义就在省这个）；
        # ② 它发生在"能不能取到令牌"之前 ⇒ 不给枚举面添一条新的可分辨信道。
        if _failure_count(source_ip, spec=spec) >= spec.failure_max:
            self._fail(request, spec.invalid_credentials_msg)
        if token is None:
            # 头缺失 / 方案不对 / 词数不对：付一次哑 HMAC 再回同一句话（否则这条路比
            # "带了错令牌"少一次摘要，响应时间就成了可分辨的信道）。
            tokens.burn_timing_parity_signature("")
            _count_failure(source_ip, spec=spec)
            self._fail(request, spec.invalid_credentials_msg)
        try:
            app_key, segment = tokens.unverified_payload(token)
        except tokens.TokenError:
            # 形状 / 解码 / JSON / 字段类型不对：与"签名错"算**同一档**（同一句话、同一次摘要）。
            tokens.burn_timing_parity_signature("")
            _count_failure(source_ip, spec=spec)
            self._fail(request, spec.invalid_credentials_msg)

        # ── 第 2 步：按 `ak` **一次索引查询**（⛔ 不逐行扫，见 `models.ApiApp`）──
        # ⚠️ 表与列名经 spec（`spec.app_model` / `spec.app_key_field`）——既有用例把桩打在
        # 本模块的 `ApiApp` 名字上，而打的是**同一个 manager 实例**的属性 ⇒ 照样拦得住
        # （理由见文件头那条"再导出"注释）。
        app = spec.app_model.objects.filter(**{spec.app_key_field: app_key}).first()
        if app is None:
            # ⭐ 哑 HMAC：按收到的那一段载荷照算一遍、**结果直接丢弃** —— 让"没这个应用"与
            # "签名不对"的耗时同量级。没有它，响应时间可分辨 `ak` 是否存在，
            # 与第 1/2 步"回同一句话、不给枚举面"自相矛盾（v2 §5.4）。
            tokens.burn_timing_parity_signature(segment)
            _count_failure(source_ip, spec=spec)
            self._fail(request, spec.invalid_credentials_msg)

        # ── 第 3 步：重算 HMAC（常量时间比较在 `tokens.constant_time_equals` 一处）──
        try:
            tokens.verify(token, getattr(app, spec.secret_field))
        except tokens.TokenExpiredError:
            # ⚠️ 必须排在 `TokenError` 之前：`TokenExpiredError` 是它的**子类**，
            # 顺序反了会把"过期"算进"凭据无效"那一档（两句话不同的判据在用例里钉住）。
            self._fail(request, spec.expired_msg)
        except tokens.TokenError:
            _count_failure(source_ip, spec=spec)
            self._fail(request, spec.invalid_credentials_msg)

        # ── 第 5 步：`state`（停用 ⇒ 与凭据错**同一句话**，§5.4 的防枚举要求）──
        # ⚠️ "能不能用"**不缓存**（每次请求读当时那一行）⇒ 停用**立刻**生效。
        # ⚠️ 可用值经 spec（`spec.enabled_state_value` 取自 `ApiAppState.ENABLED`）——
        # 打手侧那个枚举是**它自己的**，⛔ 引擎 import 哪一份都错。
        if not app_is_enabled(app, spec):
            self._fail(request, spec.invalid_credentials_msg)

        # ── 第 5.5 步：**绑定账号必须可用**（D-0333，M3 半边收口；业务方拍板）──
        # 停用 / 软删的打手（`is_active=False`；本仓软删的两笔写入同事务，恢复不碰
        # `is_active` ⇒ 这一格同时盖住两档，口径与 App 令牌链既有那半边一致）⇒
        # 其已签发的对外令牌**立即失效**，且答复与第 5 步**逐字同一句**——⛔ 不单说破
        # 「账号被停用」：说破了，凭据持有者就能探测「这个账号还活着吗」
        # （同族先例：`apps/takerapi/api/hall_views.py::NO_TAKER_NODE_MSG`）。
        # ⚠️ 位置承重：排在**验签之后**（走到这里的请求已证明握着 `app_secret`，
        # 与第 8 步那条注释同一理由）⇒ 不给"拿 `ak` 探人"留信道；与第 5 步同句同形
        # （都不计失败）⇒ 两档互相**不可分辨**。
        # ⚠️ 本族**没有**绑定账号（`spec.bound_user_field is None`，上家侧）⇒ 第一行返回，
        # 一个字节都不受影响（与第 8 步的 `locked_app_kind` 同一条结构性手法）。
        if spec.bound_user_field is not None:
            if not getattr(app, spec.bound_user_field).is_active:
                self._fail(request, spec.invalid_credentials_msg)

        # ── 第 6 步：IP 白名单（空 = 不限制）；判据与那一句话都只有一份实现 ──
        if not _ip_allowed(app.ip_allowlist, source_ip):
            self._fail(request, spec.ip_not_allowed_msg)

        # ── 第 8 步：**锁定应用的端点白名单**（设计文档 §6.2 / D3 / D4 / D5）──
        # ⭐ 位置是承重的：它排在**验签 / `state` / IP 白名单之后** ⇒ 走到这里的请求
        # **已经证明**它握着那一行的 `app_secret`。⛔ 排在验签之前会让"这条端点在不在
        # 白名单里"变成一条**不需要凭据**就能探的信道（拿假令牌扫一遍就知道端点清单了）。
        # ⚠️ **只对锁定应用生效**：通用应用在 `enforce_app_endpoint_scope` 里那一句
        # `spec.locked_app_kind` 判定处直接返回（一个字节都不受影响，D8 那条「对既有数据零行为
        # 变化」的判据由测试的对照组钉住）；⭐ **打手侧整族也走同一支**（它的 spec
        # `TAKER_SPEC.locked_app_kind` 是 `None`。本类两族共用，`TakerApp` 没有 `kind` 列。
        # 不看 spec、去读 `app.kind`，打手侧每个请求都会 500。
        # ⚠️ 判据 / 文案 / 失败形状（**HTTP 403 + 业务码 403**，见 permissions.py 的 D5 改准块）**全在**
        # `apps/openapi/permissions.py`（唯一出处），这里只调它一次。
        permissions.enforce_app_endpoint_scope(app, request, spec=spec)

        # ── 第 7 步：挂到请求上并返回 `(AnonymousUser(), app)`（理由见模块 docstring）──
        self._mark_used(app, spec=spec)
        request._api_app = app
        return AnonymousUser(), app

    def authenticate_header(self, request):
        """返回 `Bearer`（**非空** ⇒ 401 才带 `WWW-Authenticate`）。

        DRF 出厂行为是「header 为空 ⇒ 401 降级成 403」；本项目自己的异常处理器按异常类型
        重新钉死状态码、**忽略** DRF 改过的 `exc.status_code`，所以摘掉本方法时状态码仍是
        401、消失的只有 `WWW-Authenticate`（实测记录见 `apps/core/authentication.py` 里
        同款方法的 docstring）。保留它是因为 HTTP 规范要求 401 带挑战头，而这条头是 DRF
        从本方法算出来塞进响应的。⚠️ 挑战头里**不带**任何密钥 / 应用信息。
        """
        return BEARER_SCHEME

    @staticmethod
    def _mark_used(app, *, spec: CredentialSpec) -> None:
        """记 `last_used_at = now`（§5.1：**最近一次认证成功**，⛔ 不是"最近一次调用"）。

        ⚠️ 用 `QuerySet.update()` 而不是 `app.save()`：只发一条 `UPDATE`、不碰其它列，
        也⛔ 不会把 `app` 对象的其它字段一起写回（认证层手里那个对象是**只读视图**）。
        ⚠️ 如实登记代价：这是**每个成功请求一次**的写。要省掉它就得引节流（"同一应用
        min(now, 已记时刻 + N 秒) 才写"），那属于性能优化；值本身只是给人看"这个应用
        还用不用"，精度要求很低。

        ⚠️⭐ T2：**更新哪张表**经 `spec.app_model` —— 改造前这里硬编码
        `ApiApp`，打手侧复用时会去改**上家侧**应用的 `last_used_at`（spec §4 第 7 条，
        症状是"应用管理页上那个'最近使用'是别人打的"）。
        ⭐⭐ **T2：`spec` 必填 keyword-only**（⛔ 默认值已去掉；理由与
        `_failure_count` 的 docstring 逐字相同）。生产调用点（本类的 `authenticate`）
        本来就显式传 `self.spec` ⇒ 行为逐字不变。
        """
        spec.app_model.objects.filter(pk=app.pk).update(last_used_at=timezone.now())

    @staticmethod
    def _fail(request, msg: str) -> NoReturn:
        """**总是抛** 401（带这一句 `msg`）。

        ⚠️ 用 `exceptions.AuthenticationFailed`：它经本项目的 `envelope_exception_handler`
        变成 **HTTP 401 + `{code:401, msg, data:null}`**（认证层不用业务码表）。
        返回类型标 `NoReturn`，于是 `authenticate()` 里 `self._fail(...)` 之后那几行
        **不是**"失败了还往下走"，而是"上一层闸已经拦住了"——读代码时不必自己推这一点。

        ⚠️ `request` 入参当前**没被用到**：留它是因为下面每一处都要传，而一旦将来要在这里
        记日志（例如把失败按来源 IP 落审计），签名不必再改一遍。⛔ 目前**故意不记任何日志**
        ——§5.4 要求不回显原文，而记日志会让"谁在猜凭据"变成一个新的话题。
        """
        raise exceptions.AuthenticationFailed(msg)
