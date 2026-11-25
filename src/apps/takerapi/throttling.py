"""打手侧对外端点的**极宽兜底闸**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §4-9 / §4.1-4）。

## 这里是什么

`SpecDrivenFallbackThrottle`（`apps/openapi/throttling.py`，T2 抽的形状）的一个子类：
按应用分桶的**固定窗口**兜底闸，桶表 / `scope` / `app_key` 列名全部经 `TAKER_SPEC` 取。
⛔ **本模块不抄第二份计数逻辑**：`allow_request` / `wait` 一个字都不重写，
只有"本族是哪一份 spec"与"本族的额度是多少"两件是打手侧自己的。

## ⛔⭐ 本类**必须**继承 `SpecDrivenFallbackThrottle`，⛔ **不许**继承 `ApiAppFallbackThrottle`

那条禁令写在 `ApiAppFallbackThrottle` 的类 docstring 里，理由是**具体的**：
后者把两个额度钩子实现成了"读**上家侧模块**的模块级名字"
（`OPENAPI_FALLBACK_MAX_REQUESTS` / `OPENAPI_FALLBACK_WINDOW_SECONDS`）
⇒ 从它继承就等于把**上家侧的额度**一起继承过去。而"额度不对"**没有任何症状**：
闸照常工作、请求照常放行，只有"量级是别人的"这一件事看不出来
（spec §4.1-4 点名的那颗地雷）。

## ⭐ 两个额度钩子读**本模块**的模块级名字（⛔ 不是"顺手写个数字"）

理由与上家侧那两个钩子**逐字相同**（`E-0029` 同族）：把额度留在**模块级的名字**上，
将来给打手侧写打桩用例时 `mock.patch.object(taker_throttling, "TAKER_FALLBACK_MAX_REQUESTS", …)`
**打得中**；写成字面量、或从 spec 现取，补丁会**静默失效**（症状是"限流没生效"，
根因却在测试接错了桩）。

⚠️ 两个名字的**值**本身住在 `apps/takerapi/credentials.py`（spec 的字段 + 那两个常量）
—— ⛔ 数字不在本模块写第二遍。⚠️ 与上家侧那条一样，这两处**如实登记**
"决策点读模块别名、spec 字段是它的初始化来源"这个口径（spec §4.1-5 ②）。

## ⚠️ 本闸**不装**在内部端点（`/api/taker/apps/…`）上

那些端点走会话认证、`request.auth` 不是凭据模型 ⇒ 本类在那里会**一律放行**
（等于装了个装饰）。它只为**对外**端点存在。⚠️ 落地时点：T5/T6 的对外端点
——⭐ **T5 已落地两条**（拉大厅 `api/hall_views.py::HallListView` 与抢单
`api/order_write_views.py::HallClaimView`，两条都把它声明在**类属性**上：
DRF 的 `initial()` 顺序是认证 → 权限 → 限流 ⇒ 401 一律先于 429、且被 401 挡掉的请求不进桶）。
"""
from __future__ import annotations

from apps.openapi.credentials import CredentialSpec
from apps.openapi.throttling import SpecDrivenFallbackThrottle
from apps.takerapi.credentials import (
    TAKER_FALLBACK_MAX_REQUESTS,
    TAKER_FALLBACK_WINDOW_SECONDS,
    TAKER_SPEC,
)

#: 打手侧的兜底额度：**每应用每窗口**放行多少次。⚠️ **唯一出处是 spec**
#: （`TAKER_SPEC.app_fallback_max_requests`，⛔ 数字不在这里写第二遍）。
#: ⚠️⭐ **与上家侧的 6000 刻意不同**（spec §4.1-4）：3000/分钟。
#: 这个**模块级别名必须留着**（打桩入口 / 可读性），口径与上家侧那个别名**逐字相同**。
TAKER_FALLBACK_MAX_REQUESTS = TAKER_SPEC.app_fallback_max_requests

#: 窗口长度（秒）。与上面的数字一起构成「3000 次 / 分钟」。
TAKER_FALLBACK_WINDOW_SECONDS = TAKER_SPEC.app_fallback_window_seconds


class TakerAppFallbackThrottle(SpecDrivenFallbackThrottle):
    """**打手侧**那一族的兜底闸（spec §4 那张表的第 9 项："taker 侧也要装一个兜底闸"）。

    ## 与上家侧那一族的**唯一**差别就是下面这三样

    | | 上家侧（`ApiAppFallbackThrottle`） | 本类 |
    |---|---|---|
    | `spec` | `OPENAPI_SPEC` | `TAKER_SPEC`（⇒ **桶表**是 `TakerApiRateBucket`） |
    | 额度 | 6000 / 60 秒 | **3000 / 60 秒**（本模块的两个常量） |
    | 基数 | 上家侧的应用（`ApiApp`） | 打手侧的应用（`TakerApp`） |

    ⛔ **不是**"继承一个、改一个数字"：共用流程（计数、`Retry-After`、边界语义）在基类里，
    本类只声明**自己那一族的**两件事（spec + 额度）。

    ## ⚠️ 桶表不同 ⇒ 两族的计数天然隔离（spec §9-16 要判据的那一条）

    `allow_request` 里的 `rate_limit.consume(...)` 带 `spec=self.spec`
    ⇒ 计数落进 `TAKER_SPEC.bucket_model`（= `TakerApiRateBucket`，**打手侧那张表**）。
    上家侧那一行落 `OpenApiRateBucket` ⇒ **同一 `app_key` / 同一来源 IP 在两张表里各算各的**。
    ⚠️ 两族的 `Scope` **取值逐字相同**（`app` / `ip` / `ip_token`）—— 隔离**不是**靠 `scope` 不同，
    而是靠**表**不同（两张表各自有自己的 `(scope, ident, window_start)` 唯一约束）。
    ⛔ 别把"两族 scope 值一样"读成串台：判据是**行落在哪张表**
    （`tests/test_takerapi_auth.py::RateBucketIsolationTests` 逐行读两张表）。
    """

    #: 本类服务哪一族凭据（= 打手侧那一份 ⇒ 计数落 `TakerApiRateBucket`）。
    spec: CredentialSpec = TAKER_SPEC

    def fallback_max_requests(self) -> int:
        """打手侧的兜底额度：**读本模块的模块级名字**（打桩入口，见模块 docstring）。"""
        return TAKER_FALLBACK_MAX_REQUESTS

    def fallback_window_seconds(self) -> int:
        """打手侧兜底额度的窗口：**读本模块的模块级名字**（同上）。"""
        return TAKER_FALLBACK_WINDOW_SECONDS
