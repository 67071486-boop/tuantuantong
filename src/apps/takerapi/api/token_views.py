"""打手侧取令牌端点 `POST /api/taker/oauth/token`（spec `docs/specs/2026-10-08-taker-open-api-design.md` §2）。

## ⛔ 本模块只有"本族的那两件"，其余全在共用流程里

| 一件 | 在哪 |
|---|---|
| 流程（两道闸的顺序 / `grant_type` / 三档凭据失败合并 / 响应体形状） | `apps/openapi/api/token_views.py::token_exchange_response`（**唯一**实现，⚠️ 本模块调它，⛔ 不抄） |
| 本族是哪一份 spec | 本模块（`spec=TAKER_SPEC`） |
| 本族的**兜底额度** | 本模块（读**本模块**的两个模块级名字） |
| 类级契约（`authentication_classes = []` / `permission_classes` / OAuth2 形状标记） | 本模块（与上家侧那条逐字同款） |

⭐ **为什么"兜底额度"必须在本模块显式传、⛔ 不许让共用流程自己去读**
`token_exchange_response` 的 `fallback_max_requests` / `fallback_window_seconds` 是
**必填 keyword-only** —— 那个签名本身就是为了这一族设计的（spec §4.1-4 的地雷）：
> 打手侧若照抄 openapi 那个薄壳（读 `token_views.TOKEN_FALLBACK_MAX_REQUESTS`），
> 会**静默用到上家侧的额度**（600/分钟）——行为看起来一切正常，只有"量级是别人的"没有症状。

⇒ 本模块读的是**它自己**的两个模块级名字（值是 `TAKER_SPEC` 的字段，300/分钟）
⇒ 既让将来的打桩打得中（`E-0029`），又**结构上拿不到**上家侧那个数字。
⚠️ ⛔ **不许** import `apps.openapi.api.token_views` 的那两个常量当"默认值"用。

## ⛔ 对外契约与上家侧那条取令牌端点**逐字相同**（形状，不是文案来源）

| 情形 | HTTP | 响应体 |
|---|---|---|
| 成功 | **200** | `{"access_token": …, "token_type": "Bearer", "expires_in": 7200}` |
| 凭据错 / 应用不存在 / 应用停用 | **401** | `{"error": "invalid_client", "error_description": …}` |
| `grant_type` 不是 `client_credentials` | **401** | `{"error": "unsupported_grant_type", "error_description": …}` |
| 两道闸挡下 | **429** | `{"error": "temporarily_unavailable", "error_description": …}` |

⚠️ 失败体**恰好两键**（⛔ 没有本仓信封的 `code` / `msg` / `data`）—— 逐条理由（为什么是
标准体、为什么 `authentication_classes` 必须是**空列表**、为什么 `request.POST` 会把
"体解不出来"归一到"字段缺失"）写在共用流程与上家侧那个视图类的 docstring 里，
⛔ 不在本模块重述一遍（那样两处会漂）。**判据是结构网的"恰好"那一条**：
`tests/test_no_route_500.py::OAUTH2_SHAPED_ROUTES`（本端点同一次登记进那一档）。

## ⚠️ 本端点**不认会话 cookie**、也**不认 Bearer 令牌**

它的入参就是凭据本身（`client_id` / `client_secret`）⇒ 一个**没登录**的第三方机器
本来就**不该**登录（机器对机器）。⚠️ 这条同时是 `tests/test_no_route_500.py` 里
`_is_token_only` 对**空列表**那条特判要绕开的形状（"一个认证类都不认" ≠ "只认令牌"）。

## ⚠️ 留痕：本端点**不写**操作日志（登记在 `tests/test_operation_log_ledger.py`）

它是**凭据交换**，不产生任何业务副作用（不建 / 不改 / 不删业务数据）。唯一的写入方在
**别的**端点（认证层写 `TakerApp.last_used_at`，那是列级时间戳、不是业务动作），
而本端点连那一次都不写（它不跑任何认证类）。
"""
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.openapi.api.token_views import token_exchange_response
from apps.takerapi.credentials import TAKER_SPEC

#: ⭐ **打手侧取令牌端点的兜底额度**（spec §4.1-4 的地雷就落在这里）。
#: ⚠️ **唯一出处是 `TAKER_SPEC`**（⛔ 数字不在本模块写第二遍，⛔ 更不许去读上家侧那个常量）；
#: 这个**模块级别名必须留着**：将来给打手侧写打桩用例时按它
#: `mock.patch.object(taker_token_views, "TAKER_TOKEN_FALLBACK_MAX_REQUESTS", …)`
#: 才能打得中（`E-0029`：从 spec 现取会让补丁**静默失效**）。
TAKER_TOKEN_FALLBACK_MAX_REQUESTS = TAKER_SPEC.token_fallback_max_requests

#: 兜底额度的窗口长度（秒）。与上面的数字一起构成「300 次 / 分钟 / 来源 IP」。
TAKER_TOKEN_FALLBACK_WINDOW_SECONDS = TAKER_SPEC.token_fallback_window_seconds


class TakerTokenView(APIView):
    """`POST /api/taker/oauth/token`：用 `client_id` / `client_secret` 换一个短期令牌（spec §2）。

    ## ⛔ 路径**不带尾斜杠**（spec §2 的 `/api/taker/oauth/token` 逐字）

    与上家侧那条（`/api/open/oauth/token`）同一口径：契约里就是这个拼写，
    `tests/test_no_route_500.py` 的 `OAUTH2_SHAPED_ROUTES` 里登记的字面量也**不带**斜杠
    —— 写成带斜杠的版本那条网当场红（那正是它枚举的作用）。

    ## 类级契约（三条，逐条与上家侧那个视图类**同款**）

    - `authentication_classes = []`：**空列表是承重的**，不是"忘了写"——本端点没有
      可用的认证态（它的入参就是凭据）。不写 ⇒ DRF 取全局默认（会话认证）⇒ 第三方被
      401 挡在门口；写 `[TakerAppBearerAuthentication]` ⇒ "先拿令牌才能换令牌"，自相矛盾。
    - `permission_classes = [AllowAny]`：fail-closed 的全局默认是 `IsAuthenticated`，
      本端点是**匿名**可达的 ⇒ 必须显式放开（否则 401 会来自权限层、而那句话不是 OAuth2 体）。
    - `oauth2_response_contract = True`：供**结构网**按框架事实把它归进 `OAUTH2_SHAPED_ROUTES`
      那一类（⚠️ 该属性由**每个这类视图类各自声明**，基类不预置 ⇒ 新加一个回信封的 APIView
      不会因为继承被误判成 OAuth2 形状）。

    ## ⛔ 一整套流程**一行都不在本类里**

    `post()` 是一个**薄壳**：把本族的 spec 与**本族的**两个额度交给共用流程
    `token_exchange_response`（⛔ 不许在这里把两道闸 / 三档失败合并抄一遍——
    那是 spec §4 第 10 项点名的"两份 OAuth2 响应体形状漂移"）。
    """

    #: ⛔ **空列表是承重的**，不是"忘了写"（理由见类 docstring 与 `apps/openapi` 那条同名视图）。
    authentication_classes = []
    #: fail-closed 的全局默认权限类是 `IsAuthenticated`；本端点是**匿名**可达的。
    permission_classes = [AllowAny]
    #: ⭐ 供**结构网**按框架事实识别本类的类属性（与上家侧那条同一条手法）。
    oauth2_response_contract = True

    def post(self, request):
        """⭐ 唯一实现是 `token_exchange_response`；本方法只负责"传本族的东西"。

        ⚠️ **额度必须在调用时读本模块的模块级名字**（`TAKER_TOKEN_FALLBACK_MAX_REQUESTS` /
        `TAKER_TOKEN_FALLBACK_WINDOW_SECONDS`）再传进去：将来按那两个名字打桩才拦得住，
        从 spec 现取会让补丁**静默失效**（`E-0029` 同族）。
        ⛔ **不许**读 `apps.openapi.api.token_views.TOKEN_FALLBACK_MAX_REQUESTS`
        （那正是 spec §4.1-4 要挡的形状：打手侧静默用上家侧的额度）。
        """
        return token_exchange_response(
            spec=TAKER_SPEC,
            request=request,
            fallback_max_requests=TAKER_TOKEN_FALLBACK_MAX_REQUESTS,
            fallback_window_seconds=TAKER_TOKEN_FALLBACK_WINDOW_SECONDS,
        )
