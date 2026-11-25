"""打手侧对外端点的**令牌认证**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §2 / §4）。

## 这里是什么（⚠️ 本模块一共只有一行"实现"）

```python
class TakerAppBearerAuthentication(ApiAppBearerAuthentication):
    spec = TAKER_SPEC
```

七步判据（取令牌 → 按 `ak` 查一行 → 重算 HMAC → `exp` → `state` → IP 白名单 → 挂
`request.auth`）、哑 HMAC、按来源 IP 的失败计数、`last_used_at` 的写法、五句文案、
401 的出口 —— **全部只有一份实现**，在 `apps/openapi/authentication.py::ApiAppBearerAuthentication`
（T2 把它改成了"逐处经 `self.spec` 取"）。⛔ **本模块不抄第二份**：抄一份的后果是两条通道的
判据会各自漂（`AGENTS.md` §5-E：同一个事实两套判据），而这里的漂是**安全面**的漂。

## ⭐ 为什么"打手令牌打不到上家侧端点"是**结构性**的、⛔ 不是靠每条端点记得过滤

`ApiAppBearerAuthentication.authenticate` 的第 2 步是
`spec.app_model.objects.filter(**{spec.app_key_field: app_key}).first()` —— 表来自 **spec**：
拿打手侧的令牌打 `/api/open/v1/…` 时，走的是上家侧那个认证器（`spec = OPENAPI_SPEC`）
⇒ 它在 **`ApiApp` 那张表**里查那个 `ak`，**必然查不到**（两个 app 各自签发自己的令牌，
`ak` 就是自己的 `app_key`）⇒ 未命中那一支 ⇒ **401**（走哑 HMAC，时序不可分辨）。
反之亦然。判据：`tests/test_takerapi_auth.py::CrossCredentialTypeTests`（spec §9-2）。

⚠️ 这条与"一张表加一列 `kind`"那条被否掉的做法的差别就在这里：后者会让两条通道共用一次查表，
"打手令牌打不到上家端点"退化成**每条端点都要记得过滤**——漏一处就是静默全租户
（spec §2 的红队 Blocking 项；逐条理由见 `apps/takerapi/models.py::TakerApp` 的 docstring ①）。

## ⛔ 打手侧对外端点**不装任何权限类**（与上家侧同口径）

范围（游戏权限 / "他名下的单"）**不是靠权限类收窄的**，而是每条端点自己按 `spec.app_model.taker`
（绑定的打手账号）算（spec §5.3）——那属于 T5/T6。⚠️ 尤其⛔ 不要装 `IsAuthenticated`：
Bearer 认证成功时返回的是 `AnonymousUser()`（理由见共用类的模块 docstring）
⇒ 合法请求会被判 401。
"""
from __future__ import annotations

from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.credentials import CredentialSpec
from apps.takerapi.credentials import TAKER_SPEC


class TakerAppBearerAuthentication(ApiAppBearerAuthentication):
    """**打手侧（`apps.takerapi`）**那一族的 Bearer 令牌认证（spec §2 的"认证类"那一行）。

    ## ⛔ 本类**只**声明 `spec`，⛔ 一个方法都不重写（这一条是判据，不是风格）

    七步判据、哑 HMAC、失败计数、`_mark_used`、`_fail`、`authenticate_header` 全部继承
    共用基类。**改一处 = 两条通道同时改**，这正是 spec §2 要的形状。

    ## ⚠️ 为什么必须覆盖 `spec`（⛔ 不能靠"基类那个默认值"）

    共用基类的 `spec` 默认是 `OPENAPI_SPEC`；不覆盖它的子类会去查 **`ApiApp`** 那张表——
    症状是**打手侧认证永远 401**（`TakerApp.app_key` 在上家侧那张表里查不到）。
    ⛔ 而那是**响亮**的失败（每条请求都 401）、不是静默串台：真正的静默面在**引擎**那几处
    （台账 / 桶 / 文案），它们自 T2 起一律走 `spec`，且 T2b 把残留的几处默认值也收掉了
    （见 `apps/openapi/credentials.py` 与 spec §4.1-1）。

    ## ⚠️ `state` 的可用值 / 三个 `scope` / 五句文案都来自 `TAKER_SPEC`

    它们**不写字面量**：打手侧那两个枚举是**它自己的**（`AppState` / `TakerApiRateBucket.Scope`），
    ⛔ 引擎 import 哪一份都错（理由见 `apps/takerapi/credentials.py` 的模块 docstring）。
    """

    #: 本类服务哪一族凭据（= 打手侧那一份；⛔ 别指望基类那个默认值）。
    spec: CredentialSpec = TAKER_SPEC
