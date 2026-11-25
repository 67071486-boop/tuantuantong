"""取令牌端点 `POST /api/open/oauth/token`（v2 spec §5.1/§5.4）。⛔ 本端点回 **OAuth2 标准体**（成功 200 三键 / 失败 401 恰好两键，⛔ 没有本仓信封——业务方拍板选项 A：第三方按标准读 `access_token`，套信封会让它永远读不到令牌）；判据=`tests/test_openapi_token.py` 键集恰好断言 + `tests/test_no_route_500.py` 的 `OAUTH2_SHAPED_ROUTES`。
承重：`authentication_classes=[]` 必须显式写（不写=吃全局会话认证 ⇒ 没登录的第三方被 401 挡在门口，而它是机器对机器；写 Bearer=先拿令牌才能换令牌，自相矛盾）；本端点不写操作日志（凭据交换零业务副作用，登记在 `test_operation_log_ledger`）；两道限流闸（IP 失败闸早退 + 兜底额度）答复都是 429+固定文案，流程与形状唯一实现在 `token_exchange_response`（⛔ 不抄第二份）。"""
from rest_framework import exceptions
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.openapi import rate_limit
from apps.openapi.authentication import (
    BEARER_SCHEME,
    client_ip,
    count_ip_failure,
    ip_failure_gate_already_exceeded,
)
from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec, app_is_enabled

# ⚠️ 这一行不参与判定——是**再导出**：既有用例把桩打在本模块 `ApiApp` 名字上
# （`tests/test_openapi_rate_limit.py` 两条：闸门早退"不查库"+对照"桩接得上"，承重，E-0029）。
# ⛔ 不许为"名字干净"去掉；判定一律走 `spec.app_model`（同一个类对象 ⇒ 桩照样拦得住）。
from apps.openapi.models import ApiApp  # noqa: F401  （再导出，见上面那条注释）
from apps.openapi.tokens import (
    TIMING_PARITY_SECRET,
    TOKEN_TTL_SECONDS,
    constant_time_equals,
    issue,
)

#: 唯一支持的授权类型（§5.1）。⚠️ 其它值一律 `unsupported_grant_type`（⛔ 不是"忽略它继续"）。
GRANT_TYPE_CLIENT_CREDENTIALS = "client_credentials"

#: 成功响应里的 `token_type`。⚠️ 与认证头里的方案名是**同一个词** ⇒ 字面量取自
#: `authentication.BEARER_SCHEME`，⛔ 不在这边再写一遍。
TOKEN_TYPE = BEARER_SCHEME

#: `unsupported_grant_type` 那句描述（**对外契约的一部分**：接入文档逐字列出）。
#: ⚠️ 值取自 spec（本名字是既有用例 import 的那个 ⇒ 保留为别名）。
UNSUPPORTED_GRANT_TYPE_DESCRIPTION = OPENAPI_SPEC.unsupported_grant_type_description

#: OAuth2 的两句 `error`（§5.4 逐字）。⚠️ 它们是**对外契约**，用例按字面量比对
#: （E-0024：钉值不钉常量），所以这里是模块级常量、由端点与用例共用一份字面量。
ERROR_INVALID_CLIENT = "invalid_client"
ERROR_UNSUPPORTED_GRANT_TYPE = "unsupported_grant_type"

#: ⭐ 限流那一档的 `error`（RFC 6749 §5.2 的取值，v2 §13.11-1 ② 逐字给出）。
ERROR_TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"

#: 取令牌端点的兜底额度（每来源 IP 每窗口，**含成功与失败**——失败同样花一次凭据比较+一次索引查询）。
#: ⛔ 不是配额（正常业务差四个数量级碰不到，只为"失控/刷量"存在）；唯一出处=spec（`OPENAPI_SPEC.token_fallback_max_requests`，⛔ 不写第二遍）。
#: ⚠️ 这个**模块级别名必须留着**（既有用例按它 import 与打桩）⇒ `TokenView.post` 必须**调用时**读本模块名并传参（从 spec 现取会让补丁静默失效，E-0029 同族）；不按应用分桶：换令牌那一刻没有认证态，按调用方声称的 `client_id` 分桶=乱写一个就换新桶。
TOKEN_FALLBACK_MAX_REQUESTS = OPENAPI_SPEC.token_fallback_max_requests

#: 兜底额度的窗口长度（秒）。与上面的数字一起构成「600 次 / 分钟 / 来源 IP」。
TOKEN_FALLBACK_WINDOW_SECONDS = OPENAPI_SPEC.token_fallback_window_seconds

#: 限流档的 `error_description`：**一句固定的话**，⛔ 与调用内容无关、⛔ 不拼调用方给的值
#: （三档凭据失败回逐字相同的一句就是为了不给枚举面；限流句若带"还剩几次"就立刻变成新的可分辨信道）。
#: 对外契约一部分（用例按字面量比对）；值取自 spec（本名字是既有用例 import 的那个 ⇒ 保留为别名）。
RATE_LIMITED_DESCRIPTION = OPENAPI_SPEC.rate_limited_description


def _oauth2_error(error: str, description: str) -> Response:
    """401＋**恰好两键**的 OAuth2 错误体（⛔ 不经本仓信封）。只有这一处构造这个形状：
    两个错误分支共用它 ⇒ 不会出现"其中一条路顺手包了信封"（§5.4「多了一种错误形状」最易漂处）。"""
    return Response({"error": error, "error_description": description}, status=401)


def _oauth2_rate_limited() -> Response:
    """**429**＋恰好两键的 OAuth2 错误体（限流档唯一出口）。⛔ 不许退化成普通信封：本端点每种答复都是 OAuth2 体，调用方按标准读 `error`/`error_description`，包一层=永远读不到原因。
    两道闸共用这一个出口 ⇒ `error`/`error_description` 逐字相同 ⛔ 不存在"其中一条换了句话"的漏法。
    状态码 **429** ⛔ 不是 401：它不是"凭据不对"而是"现在别来"——混成 401 会让调用方去改凭据（改一百遍也没用）。"""
    return Response(
        {"error": ERROR_TEMPORARILY_UNAVAILABLE, "error_description": RATE_LIMITED_DESCRIPTION},
        status=429,
    )


def token_exchange_response(
    *,
    spec: CredentialSpec,
    request,
    fallback_max_requests: int,
    fallback_window_seconds: int,
) -> Response:
    """**共用**的取令牌流程（`client_id`/`client_secret` ⇒ 令牌或 OAuth2 错误体）。抽成收 spec/参数的形状：打手侧有自己的一条取令牌端点（spec §2），四件易漂事（OAuth2 体构造/三档失败合并/两道闸/兜底额度）判据收在一处 ⛔ 不抄第二份（AGENTS §5-E）。
    入参必收 `spec`（哪一族）与两个额度参数：既有用例按**模块名**打桩（`mock.patch.object(token_views, "TOKEN_FALLBACK_MAX_REQUESTS", …)`）⇒ 视图必须调用时读模块名再传进来，否则补丁静默失效（E-0029 同族）。
    流程顺序（顺序本身是判据的一半）：0.IP 失败闸早退（已超限/来源不可解析 fail-closed ⇒ 429，⛔ 不查库不比密钥）→ 0.5.兜底额度 `consume`（排在体解析之前：被挡请求不再花解析+索引查询）→ 1.`grant_type`（协议层判据，不记失败计数）→ 2.凭据（一次索引查询+常量时间比较+`state`+绑定账号可用 D-0333，四档逐字同一句、未命中照比一次防时序信道、都按来源 IP 记一次失败）。"""
    source_ip = client_ip(request)
    # ── 闸 ①：按来源 IP 的失败闸（与 Bearer 那条路共用同一份实现/同一行计数）──
    # ⭐ 早退：已超限 ⇒ 直接拒 ⛔ 不查库、⛔ 不比密钥；来源不可解析也走这一支（fail-closed）
    # ⇒ **不读也不写任何桶**（归并到占位桶=共享毒桶）。
    if ip_failure_gate_already_exceeded(source_ip, spec=spec):
        return _oauth2_rate_limited()
    # ── 闸 ②：**兜底额度**（每来源 IP / `fallback_max_requests` 次每窗口，含成功与失败）──
    allowed, _retry_after = rate_limit.consume(
        scope=spec.token_fallback_scope,
        # ⚠️ 规范化（`rate_limit.ip_ident`）：`source_ip` 是请求头里的**原文**，
        # `ident` 列只有 64 字符 ⇒ 与失败闸用的是**同一个**规范化出口。
        # ⚠️ 走到这一行的来源**一定是可解析的**（闸 ① 已经把不可解析的挡掉了）。
        ident=rate_limit.ip_ident(source_ip),
        limit=fallback_max_requests,
        window_seconds=fallback_window_seconds,
        spec=spec,
    )
    if not allowed:
        return _oauth2_rate_limited()
    # ⚠️ DRF 的 `Request.POST` 会**顺手触发请求体解析** ⇒ 错的 Content-Type 抛 `UnsupportedMediaType`（415）、
    # 坏 JSON 抛 `ParseError`（400）——都是**信封**形状、不属于本端点契约（§5.4：每种失败都是 OAuth2 体）。
    # 所以把"体解不出字段"归一成**字段缺失**，落进已定义的失败分支。
    try:
        fields = request.POST
    except (exceptions.ParseError, exceptions.UnsupportedMediaType):
        fields = {}

    grant_type = fields.get("grant_type")
    if grant_type != GRANT_TYPE_CLIENT_CREDENTIALS:
        # ⚠️ 这一档**不记**失败计数：它不是"猜凭据"（判据：把 `grant_type` 写错 21 次
        # 也不会把这个来源拉黑，见 `tests/test_openapi_rate_limit.py`）。
        return _oauth2_error(
            ERROR_UNSUPPORTED_GRANT_TYPE, spec.unsupported_grant_type_description
        )

    client_id = fields.get("client_id")
    client_secret = fields.get("client_secret")
    # ⚠️ 表与列名经 spec（`spec.app_model` / `spec.app_key_field` / `spec.secret_field`）——
    # 既有用例把桩打在本模块的 `ApiApp` 名字上，而打的是**同一个 manager 实例**的属性
    # ⇒ 照样拦得住（理由见文件头那条"再导出"注释）。
    app = spec.app_model.objects.filter(**{spec.app_key_field: client_id}).first()
    # ⚠️ 两个分支都要付一次比较（未命中时拿 `TIMING_PARITY_SECRET` 照比一次、结果丢弃），
    # 否则"应用不存在"比"密钥错"少一次比较 —— 那是应用枚举的一条时序信道。
    secret_ok = constant_time_equals(
        getattr(app, spec.secret_field) if app is not None else TIMING_PARITY_SECRET,
        client_secret,
    )
    # ⭐ **绑定账号必须可用**（D-0333）：与三档凭据失败并成同一支（同一句话、同样记一次失败 ⇒ 任何可观察面都分辨不出「绑定账号被停用」与「凭据无效」，防探测，同族先例 `NO_TAKER_NODE_MSG`；停用的打手换不出新令牌=业务方拍板）。
    # 未命中（`app is None`）或本族无绑定账号（`None`）时取 `True`：前者本来就要拒，⛔ 别让取属性把 `None` 炸成 500。
    bound_user_active = (
        getattr(app, spec.bound_user_field).is_active
        if app is not None and spec.bound_user_field is not None
        else True
    )
    if (
        app is None
        or not secret_ok
        or not app_is_enabled(app, spec)
        or not bound_user_active
    ):
        # ⭐ 按来源 IP 记一次失败（复用与 Bearer 同一行计数的既有实现）⇒「猜 client_secret」从此有闸；
        # 第 `failure_max + 1` 次：`consume` 判定已不放行 ⇒ 回 **429**（⛔ 不是 401——那次不再是"凭据无效"语义）。
        if count_ip_failure(source_ip, spec=spec):
            return _oauth2_rate_limited()
        # ⭐ 三档共用**同一句** `error_description`（§5.4 的硬要求）：字面量取自 spec，
        # 与 Bearer 认证那条路**同源**（spec 的 `invalid_credentials_msg` 只有一个出处）。
        return _oauth2_error(ERROR_INVALID_CLIENT, spec.invalid_credentials_msg)

    return Response(
        {
            "access_token": issue(
                getattr(app, spec.app_key_field), getattr(app, spec.secret_field)
            ),
            # ⚠️ 从常量取，⛔ 不写字面量：`expires_in` 与令牌里那个 `exp` 差多少
            # 由 `TOKEN_TTL_SECONDS` 一处决定（判据 = 扫 `7200` 字面量的那条用例）。
            "expires_in": TOKEN_TTL_SECONDS,
            "token_type": TOKEN_TYPE,
        }
    )


class TokenView(APIView):
    """`POST /api/open/oauth/token`：`client_id`/`client_secret` 换短期令牌（§5.1）。校验顺序（顺序是判据的一半）：0.IP 失败闸早退（已超限/来源不可解析 fail-closed ⇒ 429 ⛔ 不查库不比密钥）→ 0.5.兜底额度（含成功与失败，排在体解析之前）→ 两道闸必须排在 `grant_type` 之前（限流是"现在别来"，反着会让刷量来源用 `unsupported_grant_type` 换到 401）→ 1.`grant_type`（协议层，不记失败计数）→ 2.凭据（一次索引查询+常量时间比较+`state`；三档逐字同一句防枚举；未命中照比一次防时序信道；应用行不缓存=停用/轮换立刻生效；三档都按 IP 记失败，第 21 次起 429）。判据=`tests/test_openapi_rate_limit.py`（`UnparsableSourceIpTests`：垃圾来源一行桶都不产生；`TokenEndpointFailureGateTests`）。
    ⛔ 为什么不可解析回 429 不回 401：这一档根本没看凭据，`invalid_client` 是说假话（调用方会去改凭据）；429="现在别来"与"来源无法识别 ⇒ 不放行"同义，且与额度超限同一句固定文案（与调用内容无关）。
    ⛔ 入参只读 `request.POST` 并把"体解不出"归一成字段缺失（`request.data` 会抛 415/400 信封形状；`request.POST` 也会先解析、抛的异常在 `post()` 收成空字段）；闸的顺序与响应形状住在 `token_exchange_response`，本类只留契约与薄壳 `post()`（第二族调同一份 ⛔ 不抄第二遍）。"""

    #: ⛔ **空列表是承重的**，不是"忘了写"：理由逐条见模块 docstring（本端点没有认证态）。
    authentication_classes = []
    #: fail-closed 的全局默认权限类是 `IsAuthenticated`；本端点是**匿名**可达的
    #: ⇒ 必须显式 `AllowAny`（否则 401 会来自权限层、而那句话不是 OAuth2 体）。
    permission_classes = [AllowAny]
    #: ⭐ 供**结构网**按框架事实识别本类的类属性：`tests/test_no_route_500.py` 的 `_is_oauth2_shaped`
    #: 读它、把本端点归进 `OAUTH2_SHAPED_ROUTES`（畸形入参=401+恰好两键）。⛔ 由每个这类视图类各自声明
    #: （基类不预置）⇒ 新加回信封的 APIView 不会因继承被误判成 OAuth2 形状。
    oauth2_response_contract = True

    def post(self, request):
        """整段流程搬进 `token_exchange_response`（收 spec/参数的形状）。本方法保留成**薄壳**：OAuth2 体构造+三档失败合并+两道闸+兜底额度是同一个事实，只能有一份实现（理由见那个函数的 docstring ⛔ 不在这里重述）。
        ⚠️ 额度必须**调用时读本模块的模块级名字**再传进去：既有用例按那个模块名打桩，从 spec 现取会让补丁静默失效（E-0029 同族）。"""
        return token_exchange_response(
            spec=OPENAPI_SPEC,
            request=request,
            fallback_max_requests=TOKEN_FALLBACK_MAX_REQUESTS,
            fallback_window_seconds=TOKEN_FALLBACK_WINDOW_SECONDS,
        )
