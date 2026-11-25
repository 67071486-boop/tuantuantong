"""押金闸**对谁生效**的唯一判据（D-0269 押金，业务方 要求 App 与网页分开控制）。两个开关的分工： - `DEPOSIT_ENFORCED`：租户级总闸——网页端与 App **一起**拦； - `DEPOSIT_ENFORCED_APP`：**只拦 App 令牌**（`AppTokenAuthentication` 认出来的 Bearer 请求），
网页会话不受影响。 ⚠️ 上家镜像单的接单路径**也用这一条判据**（`jidanpu_upstream_views`，起它也会冻结押金，见 D-0271）——⛔ 不要再为「上家单」另写一份 App 判据。 ⛔ 不要在视图里各写一份 `isinstance(request.auth, AppToken) and ...`：这条判据一旦两处实现就会漂移，
两处漂移的后果是「网页端悄悄开始拦人」或「App 端悄悄不拦」——两者都是钱的事。"""
from django.conf import settings

from apps.accounts.models import AppToken


def is_app_request(request) -> bool:
    """这条请求是不是**打手 App 的 Bearer 令牌**请求。 ⭐ 2026-10-25 线上热修（某租户真机：无押金打手在 **移动端** 接走了上家镜像卡）：打手 App 走的是 **`TakerApp`** 令牌（`request.auth.taker`），而本函数此前只认
    `AppToken` ⇒ `DEPOSIT_ENFORCED_APP` 对**移动端接单完全不生效**。现在两族令牌都认。 ⚠️ `TakerApp` 用**懒导入**（`apps.takerapi.models` 与 business 之间有导入环）。
    与 `deposit_enforced_for` 的 App 分支**同一条事实**，抽出来是给**不依赖押金开关**的消费者用（例：`apps.business.claim_channel` 判接单渠道）。⛔ 不要在视图里另写 `isinstance(request.auth, AppToken)`：两处各判一次会漂移。"""
    auth = getattr(request, "auth", None)
    if isinstance(auth, AppToken):
        return True
    from apps.takerapi.models import TakerApp  # 懒导入：避免 business ↔ takerapi 的导入环

    return isinstance(auth, TakerApp)


def deposit_enforced_for(request) -> bool:
    """这个请求的接单要不要过押金闸。 ⚠️ `request.auth` 的取值由 DRF 的认证类决定：App 令牌 = `AppToken` 实例， Session 会话 = `None`。**取不到 auth 一律按网页端处理**（fail-open 的是"不拦"，
    但总闸 `DEPOSIT_ENFORCED` 仍会拦——不是安全缺口，是"App 专属闸对匿名/会话不生效"）。 ⚠️ **同时带会话 cookie 与 Bearer** 时（`DEFAULT_AUTHENTICATION_CLASSES` 里 Session 在前），
    DRF 认的是会话 ⇒ 这里判成网页端。App 登录**不写 session**（见 `app_auth_views` 模块 docstring），正常 App 流量不会命中这个混合形状；要防的话应改判据看 `Authorization` 头， ⛔ 但那样会与 DRF 的实际认证结果脱节（既有登记，刻意不改）。"""
    if settings.DEPOSIT_ENFORCED:
        return True
    if not settings.DEPOSIT_ENFORCED_APP:
        return False
    return is_app_request(request)
