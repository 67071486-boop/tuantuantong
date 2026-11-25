"""开放平台对外端点的**极宽兜底闸**（spec `docs/specs/2026-10-07-open-platform-api-design.md`
§4-D11 / §9-4 / §9-5；T6）。

## ⭐ 这里是什么、⛔ 不是什么（本节存在的唯一理由：别让它被读成「配额」）

业务方 选定 **不设每应用配额**（目的是测服务器到底能跑到多少）⇒ 本闸**只有一个作用**：
把**失控**（第三方重试写错成了死循环、脚本跑飞、压测忘关）挡住，让机器不至于被**一个**应用拖死。
额度**故意定得极宽**（默认 6000 次 / 分钟 / 应用）⇒ **正常业务永远碰不到它**。

⛔ **不要把它读成「有配额」**：没有按套餐分级、没有按应用分配、没有「用超了要提额」这回事。
⚠️ **真正的第一道防线是「停用应用」**（应用管理页上的一次操作，秒级生效、立刻 401）：
它便宜、可靠、逻辑上没有绕过面，但**要人先发现问题**。本闸只负责在那之前别让机器被打死。

## 如实登记的代价（⛔ 不许只写好处；spec §9-4）

1. 计数落在租户库的 `OpenApiRateBucket`（`rate_limit.consume` 一次原子 UPSERT）。
   多 worker 读同一行，额度不会按 worker 数放大。
   `MultiConnectionEquivalenceTests` 用两条独立连接打满额度，总放行次数恰好等于 `limit`，
   且写的是同一行。那不是真 gunicorn 双 worker 的实测。
   额度仍是兜底、不是配额；停用应用仍是第一道防线。
2. **窗口是「固定窗口」，不是滑动窗口**：桶按「当前时刻 // 窗口秒数」分（见 `bucket_key`）
   ⇒ 跨窗口边界时，**相邻两个窗口各自**都能放满额度（最坏约两倍）。
   这是**有意**的：固定窗口只要「一个计数 + 一个 TTL」，没有历史列表 —— 读起来、对起来、
   排障时都便宜；而本闸的用途（挡失控）不在乎边界那一点误差。
3. **额度可调 ⇒ 调小会打到正在跑的第三方**。`OPENAPI_FALLBACK_MAX_REQUESTS` 是**上家侧**的
   唯一出处（⛔ 别在视图 / 用例里再写一份数字）。⭐⭐ **T2**：额度**按族给**
   （`SpecDrivenFallbackThrottle` 的两个钩子，基础类⛔ 不给默认额度）—— 见那个类的
   docstring 里登记的那颗地雷与 `PerFamilyQuotaTests`。
4. ⛔ **它挡不住「乱猜 appKey」**：本闸**按应用**分桶，而猜出来的 key 谁都不属于
   ⇒ 那些请求**不进任何桶**、在这里完全不受限。那条攻击面由 `apps.openapi.authentication`
   里**按来源 IP 的失败计数**兜住（T3 交付的那份）⇒ 两者**各算各的**，
   ⛔ **不要**把两个桶合并（一个是「某个应用别把机器打满」，一个是「某个来源别在这里穷举」，
   维度与阈值都不同）。
5. **超限给出去的那句话是 DRF 出厂的**（⛔ 不是本仓写的）：本仓 `LANGUAGE_CODE=zh-hans`，
   而 DRF **自带** zh_Hans 语言包 ⇒ 它渲染成中文（**实测读数**，从本线端点的 429 响应体里读到的：
   `{"code":429,"msg":"请求已被限流。 预计 17 秒后可用。","data":null}`）。
   ⛔ 这里**不自己拼文案**：状态码与文案都归 T8 的对外文档定，自己拼一句就等于同一个事实
   有两处出处（`AGENTS.md` §5-E）。
   接口契约层面**钉住的是**：HTTP **429** ＋ 统一信封里 `code=429` ＋ `data=null`
   ＋ 标准 `Retry-After` 头（整数秒、≥ 1）。

## ⭐ 为什么装在 `throttle_classes` 上（= 为什么 **401 一定先于 429**）

`APIView.initial()` 里的顺序是**认证 → 权限 → 限流**（DRF 自己的 `views.py` 里 `initial` 那三行）。
把本类放进视图的 `throttle_classes` ⇒ 它**只在认证与权限都过了之后**才会被调用：

- 没带签名 / 签名错 / 应用已停用 / 来源 IP 不在白名单 ⇒ 认证层**抛 401**，
  `check_throttles` **根本不会执行** ⇒ 这些请求**一个字节都不进桶**。
  这正是要的形状：⛔ 别让「没带签名」的请求先把桶打满，再让**合法应用**替它吃 429
  （那会把「进不去门」变成「里面的人被赶出来」，且**没有任何症状指向真正的来源**）。
- 验签过但**节点集合为空 / 不含所需节点** ⇒ 权限层**抛 403**，同样不进桶。
- 也正因为顺序如此，桶的键才敢**直接读 `request.auth`**（那一刻它一定是已通过的那个 `ApiApp`）。

⚠️ **另一条可选做法与它为什么不选**：把限流写在视图体里（`get()` 的第一行）也能做到
「认证之后」，但那样 ① 每个端点都要抄一行、**漏一个端点就是静默地没有闸**；
② `Throttled` 的 `Retry-After` 头、以及「同一次请求里限流器只被调用一次」这类细节
都要自己重写一遍。⇒ 用 DRF 的装配点：`ApiOrderView` 是两个查单端点的共同基类，
**一处**声明、两个端点同时生效（⛔ 不是"每个视图各写一份"）。

## 超限怎么回（⛔ 不自己造状态码）

本模块**只**让 `allow_request()` 返回 `False`；DRF 会抛 `rest_framework.exceptions.Throttled`
⇒ 经本项目的 `envelope_exception_handler`（`apps/core/exceptions.py`）变成
**HTTP 429 ＋ `{code: 429, msg: …, data: null}`**（该处理器对 Throttled 走「保留 HTTP 状态码、
code 取同一数值」那一支，⛔ 不是统一伪装成 200）。
"""
from __future__ import annotations

import time

from django.db import connection
from rest_framework.throttling import BaseThrottle

from apps.openapi import rate_limit
# ⚠️⭐ T2：桶模型**不再从这里 import** —— 一律经 `spec.bucket_model` 取
# （`apps/openapi/credentials.py` 的模块 docstring 写着"为什么必须集中成一束"）。
from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec

#: ⭐ 兜底额度：**每应用每窗口**放行多少次。⚠️ 6000/分钟是**兜底**，⛔ **不是配额**
#: （spec §4-D11 / §9-5）：正常业务碰不到它，它只挡失控。
#: ⚠️ **唯一出处是 spec**（`OPENAPI_SPEC.app_fallback_max_requests`，⛔ 数字不在这里写第二遍）；
#: 这个**模块级别名**必须留着，因为既有用例按它 import 与打桩
#: （`tests/test_openapi_throttling.py` 的 `mock.patch.object(throttling,
#: "OPENAPI_FALLBACK_MAX_REQUESTS", …)`）⇒ 决策点上也必须在**调用时**读这个模块名，
#: 否则那个补丁会**静默失效**（E-0029 同族）。
OPENAPI_FALLBACK_MAX_REQUESTS = OPENAPI_SPEC.app_fallback_max_requests

#: 窗口长度（秒）。与上面的数字一起构成「6000 次 / 分钟」。
OPENAPI_FALLBACK_WINDOW_SECONDS = OPENAPI_SPEC.app_fallback_window_seconds

#: 计数键前缀。⚠️ 完整键 = 前缀 + 租户 schema + 应用 + **窗口序号**（见 `bucket_key`）。
#: 活路径不再读这个前缀。计数键是 DB 行的 `(scope, ident, window_start)`。
OPENAPI_FALLBACK_KEY_PREFIX = "openapi-fallback"


def _app_ident(app, *, spec: CredentialSpec) -> str:
    """桶的应用维度：那个应用的 `app_key`（非机密、租户内唯一）。取不到 ⇒ 空串。

    ⚠️ 用 `app_key` 而**不是 pk**：pk 在每个 schema 里**各自从 1 开始**，两个租户的
    「第 7 号应用」是同一个数字 —— schema 段虽然已经把两侧分开了，但键里带上 `app_key`
    会让日志 / 缓存转储里**一眼看得出是哪个应用**（排障时这一点比省几个字节值钱）。

    ⚠️⭐ T2：**列名经 spec**（`spec.app_key_field`）；⛔ 两族的这一列同名是
    巧合、不是契约（`credential spec` 的字段名就是为这种"哪一列是应用标识"准备的）。
    ⭐⭐ **T2b：`spec` 必填 keyword-only**（⛔ 默认值已去掉）——
    两族的 `app_key` **列名今天相同**，所以漏传**不会报错、也不会看错列**，
    只会让"这一族用哪一列"这件事静默按上家侧处理（与 `_failure_count` / `consume`
    那一族同款的"静默串台面"）⇒ 统一成响亮。
    """
    value = getattr(app, spec.app_key_field, None)
    return value if isinstance(value, str) else ""


def bucket_key(app, now: float) -> str:
    """当前窗口的计数键：**前缀 + 租户 schema + 应用 + 窗口序号**。

    上家侧已退役的内存计数留下的函数，活路径不调用它。
    活路径的 `ident` 是纯 `app_key`；租户隔离靠表所在的 schema，不靠键里再拼 schema。
    打手侧不调本函数。`_app_ident` 的 spec 必填，这里写死上家侧那一份。

    窗口序号 = `now // 窗口秒数`：同一窗口落在同一个键上，跨过边界自然换键。
    """
    window = int(now // OPENAPI_FALLBACK_WINDOW_SECONDS)
    return (
        f"{OPENAPI_FALLBACK_KEY_PREFIX}:"
        # 上家侧遗留函数：spec 必填，这里写死 OPENAPI_SPEC。打手侧不调它。
        f"{connection.schema_name}:{_app_ident(app, spec=OPENAPI_SPEC)}:{window}"
    )


def _seconds_until_next_window(now: float) -> int:
    """还要等几秒才跨过当前窗口（给 `Retry-After` 用；⛔ 必须 ≥ 1 的整数）。

    ⚠️ DRF 把 `wait()` 的返回值按 `'%d'` 塞进 `Retry-After`（它自己 `math.ceil` 一次），
    所以这里返回**整数**；`max(1, …)` 是防「刚好卡在边界上算出 0」——
    `Retry-After: 0` 等于叫对方立刻重试，那正是要避免的形状。

    活路径不再调用。秒数只在 `rate_limit._retry_after`，留两份会漂开。
    """
    remaining = OPENAPI_FALLBACK_WINDOW_SECONDS - (now % OPENAPI_FALLBACK_WINDOW_SECONDS)
    return max(1, int(remaining))


class SpecDrivenFallbackThrottle(BaseThrottle):
    """按应用分桶的**固定窗口**兜底闸的**形状**（收 spec + **显式**给额度；装配方式与理由见模块 docstring）。

    ⚠️ **不要**把它装到页面端点 / 应用管理端点上：那些端点走会话认证、`request.auth` 不是
    凭据模型（本类在那里会一律放行 —— 等于装了个装饰）。它只为**对外查单**端点存在。

    ## ⚠️⭐ T2：桶经 `self.spec` 取

    桶表（表名 + 唯一约束名，见 `rate_limit._bucket_table_and_constraint`）、`scope`、
    `app_key` 那一列的列名 —— 全部经 `self.spec` 取（改造前是逐处硬编码）。

    ## ⭐⭐ T2：**额度也按族给**，且⛔ **基础类不给默认额度**

    ⚠️ **今天只有上家侧一个子类**（`ApiAppFallbackThrottle`），本任务⛔ **不实现打手侧**
    （那是 T2b）—— 但评审点名的**地雷**必须在这里登记清楚，否则 T2b 会踩：

    > 改造前额度读的是**本模块的模块级名字**（`OPENAPI_FALLBACK_MAX_REQUESTS`，
    > 既有打桩用例 `test_the_default_quota_is_the_module_constant_and_the_patch_reaches_it`
    > 要求它）。若打手侧照"只覆盖 `spec` 就完事"的形状复用，它会**静默用上家侧的额度**
    > （6000/分钟）：行为看起来一切正常，只有"量级是别人的"这一件事没有任何症状。

    ⇒ 落地成**两个类 + 两个钩子**：

    | 谁 | 干什么 |
    |---|---|
    | 本类（`SpecDrivenFallbackThrottle`） | 共用流程；`spec` **不给值**、`fallback_max_requests()` / `fallback_window_seconds()` **不给实现** |
    | `ApiAppFallbackThrottle` | 上家侧：`spec = OPENAPI_SPEC`，两个钩子读**本模块的模块级名字**（既有打桩入口原样保留） |

    ⛔ **子类必须继承本类，⛔ 不许继承 `ApiAppFallbackThrottle`**：后者把两个钩子实现成了
    "读上家侧的模块名"，从它继承就等于把上家侧额度一起继承过去（那正是上面那颗地雷）。
    ⛔ 漏给 `spec` / 漏给额度 ⇒ 第一次请求就**响亮**报错（`AttributeError` /
    `NotImplementedError`），⛔ 不是静默按上家侧跑。
    判据（能红）：`tests/test_openapi_throttling.py::PerFamilyQuotaTests`。
    """

    #: 本类服务哪一族凭据 —— ⛔ **本类故意不给值**：子类必须显式声明
    #: （漏声明 ⇒ 第一次请求 `AttributeError`，⛔ 不是默认落到上家侧）。
    spec: CredentialSpec

    #: 取时刻的出口（照 DRF `SimpleRateThrottle.timer` 的形状留一个可替换点）。
    timer = time.time

    def __init__(self) -> None:
        # `allow_request` 里算出来的「还要等几秒」；`wait()` 要用它。
        self._retry_after: int | None = None

    def fallback_max_requests(self) -> int:
        """本族的兜底额度：**每应用每窗口**放行多少次。

        ## ⛔ 为什么基础类**不给实现**（这一条是判据，不是风格）

        继承一个"有默认值"的实现 = 拿到**上一族**的额度，而额度不对**没有任何症状**
        （闸照常工作、请求照常放行，只有"量级是别人的"）。⇒ 基础类**只声明形状**：
        子类必须自己说清额度从哪来（上家侧读它自己模块的模块级名字，打手侧读它自己那份
        —— 两者各自可被自己那族的用例打桩）。
        """
        raise NotImplementedError(
            f"{type(self).__name__} 没有给出兜底额度（fallback_max_requests）："
            "每一族必须显式声明自己的额度，⛔ 不许继承上一族的"
            "（静默用别人的额度不会报错，只会在量级上不对）。"
            "见 apps/openapi/throttling.py 的模块 docstring。"
        )

    def fallback_window_seconds(self) -> int:
        """本族兜底额度的**窗口长度**（秒）。⛔ 理由与 `fallback_max_requests` 逐字相同。"""
        raise NotImplementedError(
            f"{type(self).__name__} 没有给出兜底窗口（fallback_window_seconds）："
            "每一族必须显式声明自己的窗口，⛔ 不许继承上一族的。"
            "见 apps/openapi/throttling.py 的模块 docstring。"
        )

    def allow_request(self, request, view) -> bool:
        """放行 ⇒ `True`（并**记一次**）；超限 ⇒ `False`（由 DRF 翻成 429）。

        ## `request.auth` 的两条边界（一条不计数、一条照记）

        - **`request.auth` 不是本线那个模型**（`None`，或没有 `app_key` 属性的东西）⇒ 直接放行。
          生产上走不到这一支（认证层在缺头 / 签名错时**总是**抛 401，根本轮不到限流），
          保留它是为了让「限流器被装到了会话端点上」这种将来**不至于**把 `None.app_key` 崩成 500。
          ⛔ 但**不要**据此把它读成第二道认证闸：它只是"没桶可记"，不是"拒绝"。
        - ⚠️ `app_key` 存在但**不是字符串**（正常走不到）⇒ 仍**照记**，只是键里那一段是空串。
          ⛔ 不为它特判：`app_key` 是 `NOT NULL` 的 `CharField`，那种输入意味着 `request.auth`
          压根不是本线的模型 —— 加一条永不执行的分支只会让读者以为它承重（`AGENTS.md` §5-E）。

        ## 计数

        `rate_limit.consume` 一次原子 UPSERT 完成「记 + 判」，并发下不会少记。
        `ident` 是纯 `app_key`；租户隔离靠行所在的 schema。
        `count <= 额度` 才放行：第 N 次放行、第 N+1 次被挡。
        `Retry-After` 的秒数来自 `consume()` 的第二个返回值。

        ## ⚠️⭐ T2：桶经 `spec`（额度走**本族自己**的钩子）

        `scope` / `ident` / 桶表都来自 `self.spec`（源码里不再出现任何模型名）；
        ⚠️ **额度与窗口走 `self.fallback_max_requests()` / `self.fallback_window_seconds()`**
        —— 两个钩子都**在调用时**取（上家侧那对实现读的是模块级名字 ⇒ 既有打桩照样拦得住，
        E-0029 同族）；⛔ 基础类不给实现（理由见那两个钩子的 docstring）。
        """
        spec = self.spec
        app = getattr(request, "auth", None)
        if app is None or not hasattr(app, spec.app_key_field):
            return True
        now = self.timer()
        allowed, retry_after = rate_limit.consume(
            scope=spec.app_fallback_scope,
            ident=_app_ident(app, spec=spec),
            # ⚠️ 经**本族的钩子**取（⛔ 不写死任何一个模块名 / spec 字段）：上家侧那一对
            # 读模块级名字，既有打桩用例按那个名字打 ⇒ 补丁仍然拦得住（E-0029）。
            limit=self.fallback_max_requests(),
            window_seconds=self.fallback_window_seconds(),
            now=now,
            spec=spec,
        )
        if not allowed:
            self._retry_after = retry_after
            return False
        return True

    def wait(self) -> int:
        """DRF 在 `allow_request` 返回 `False` 后调它 ⇒ 变成 `Retry-After` 头（整数秒）。

        ⚠️ 兜底值取 `1`（而不是 `None`）：`None` 会让 DRF 不带 `Retry-After`
        ⇒ 调用方只能盲目重试。本闸既然知道窗口还剩多久，就把它给出去。
        """
        return self._retry_after if self._retry_after is not None else 1


class ApiAppFallbackThrottle(SpecDrivenFallbackThrottle):
    """**上家侧（开放平台）**那一族的兜底闸（= 生产唯一在用的子类）。

    ## ⚠️ 两个钩子读**本模块的模块级名字**（⛔ 不是"顺手写个数字"）

    `OPENAPI_FALLBACK_MAX_REQUESTS` / `OPENAPI_FALLBACK_WINDOW_SECONDS` 是既有用例
    **import 与打桩的两个入口**（`tests/test_openapi_throttling.py` 的
    `mock.patch.object(throttling, "OPENAPI_FALLBACK_MAX_REQUESTS", …)`）
    ⇒ 两个钩子必须**在调用时**读那两个模块名。从 spec 现取 / 写死字面量都会让补丁
    **静默失效**（E-0029 同族：症状是"限流没生效"，根因却在测试接错了桩）。
    ⚠️ 两个名字的值本身仍然取自 spec（⛔ 数字不在本模块写第二遍）。

    ## ⛔ 打手侧（T2b）**不要**继承本类

    本类把 `fallback_max_requests()` / `fallback_window_seconds()` 实现成了"读**上家侧**的
    模块名" —— 从它继承就等于连**上家侧的额度**一起继承（那颗地雷的形状）。
    打手侧应当继承 `SpecDrivenFallbackThrottle`，在**它自己的模块**里定义自己的两个常量
    与 `spec = <它自己那一份>`（理由与判据见基类 docstring 与 `PerFamilyQuotaTests`）。
    """

    #: 本类服务哪一族凭据（= 上家侧那一份）。
    spec: CredentialSpec = OPENAPI_SPEC

    def fallback_max_requests(self) -> int:
        """上家侧的兜底额度：**读本模块的模块级名字**（既有打桩入口，见类 docstring）。"""
        return OPENAPI_FALLBACK_MAX_REQUESTS

    def fallback_window_seconds(self) -> int:
        """上家侧兜底额度的窗口：**读本模块的模块级名字**（同上）。"""
        return OPENAPI_FALLBACK_WINDOW_SECONDS

