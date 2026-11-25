"""打手 App 的认证端点（`/api/app/auth/*`）：**Bearer 令牌**通道，与 `/api/auth/*` 的浏览器会话
通道（cookie / CSRF）刻意分开，⛔ 不改 Web 端任何行为；登录失败一律同一句固定文案（不泄露错在哪）。
三条「输入来自请求」硬判据见 `_body` / `_safe_ip` / `AppPlaneMixin`；注册的建档口径与枚举面取舍见 `AppRegisterView`。 """
import ipaddress
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework import exceptions, serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.app_sms import SmsError, consume_login_code, issue_login_code
from apps.accounts.account_numbering import generate_display_name
from apps.accounts.choices import AccountSource, MemberType
from apps.accounts.models import StaffProfile
# ⚠️ 新人持单上限的**唯一出处**是自助注册线那一个常量（D-0269 ⑥：新注册打手 3 单/天）。
# 这里 import 而不另写一个 3 —— 两处各写一份数字，改一处漏一处时没有任何症状。
from apps.accounts.selfreg_views import SELF_REGISTER_GRAB_LIMIT_DL
# ⚠️ 逐游戏的留痕名字复用账号线那一份（⛔ 不在这儿再抄一遍「id → 游戏名」的回落规则）。
from apps.accounts.views import _game_names
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import roles
from apps.business.models import Game
from apps.business.peer_sync import hash_token, new_token
from apps.core.auth_views import NEW_PASSWORD_MAX, NEW_PASSWORD_MIN, _user_payload
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.login_rate_limit import (
    RATE_LIMITED_MSG,
    clear_login_failures,
    login_blocked,
    login_source_ip,
    record_login_failure,
)
from apps.core.plane import PLANE_NOT_FOUND_MESSAGE, on_public_plane
from apps.core.sms import mask_mobile, normalize_mobile
from apps.openapi.authentication import client_ip

User = get_user_model()

logger = logging.getLogger(__name__)

#: 令牌有效期（天）。App 端不常登录，给 30 天；改这里不影响已签发的令牌（过期时间是签发时算死的）。
APP_TOKEN_TTL_DAYS = 30

#: 客户端标识的缺省值（只作展示与排查用，⛔ 不参与任何授权判断）。
DEFAULT_CLIENT = "app-android"

class PlaneNotFound(exceptions.APIException):
    """平台平面上的 404。⛔ 不用 `exceptions.NotFound`——本仓异常处理器把它翻成 HTTP 200 +
    code 1002；`APIException` 落「保留状态码」分支才是 404 + code 404，与
    `apps/core/plane.py::plane_not_found_response` 的约定一致（评审 Major 1）。 """

    status_code = 404
    default_detail = PLANE_NOT_FOUND_MESSAGE
    default_code = "plane_not_found"


class AppPlaneMixin:
    """`public` 平面上一律 404（评审 Major 3）：所有 `/api/app/*` 视图都读**租户库**的表，
    `public` 平面没有 ⇒ 不挡就是 `ProgrammingError` 500。放在 `initial()`（认证/权限之前跑），
    匿名请求也挡得住。 """

    def initial(self, request, *args, **kwargs):
        # ⚠️ **先**跑认证/权限：匿名打受保护端点必须 401（不能因为平面判据提前变成 403/404），
        # 这是本仓 profile / password / realname 五个调用点同一套判据
        # （`apps/core/plane.py::plane_not_found_response` 的注释与既有测试）。
        super().initial(request, *args, **kwargs)
        if on_public_plane():
            raise PlaneNotFound()


def _body(request) -> dict:
    """请求体必须是对象（评审 Major 1）。不是对象就别往下走——`.get` 会 500。"""
    data = request.data
    return data if isinstance(data, dict) else {}


def _safe_ip(request):
    """`client_ip()` 的原值先过 `ipaddress`（评审 Major 2）：`SmsCode.request_ip` 是
    `GenericIPAddressField`，不可解析的串落库时抛错 ⇒ 500。这是**公开端点**，头由调用方控制。 """
    raw = client_ip(request)
    if not raw:
        return None
    try:
        ipaddress.ip_address(raw)
    except ValueError:
        return None
    return raw


def _issue_token(user, client: str = DEFAULT_CLIENT):
    """签一枚新令牌。返回 `(明文, 模型实例)`——明文只在响应里出现这一次。"""
    from apps.accounts.models import AppToken

    raw = new_token()
    token = AppToken.objects.create(
        user=user,
        key_hash=hash_token(raw),
        client=client[:32],
        expires_at=timezone.now() + timedelta(days=APP_TOKEN_TTL_DAYS),
    )
    return raw, token


def _token_payload(raw, token):
    """登录响应：明文令牌只在这里出现这一次。⚠️ D-0302：`expires_at` 走 `local_iso`（+08:00）
    ——App 的「有效期至」按串直显，裸 `.isoformat()` 出的是 UTC 串、差 8 小时。 """
    return {
        "token": raw,
        "expires_at": local_iso(token.expires_at),
    }


def _find_user_by_mobile(mobile: str):
    """手机号 → 账号：先 username（自助注册口径），再 `StaffProfile.mobile` **唯一命中**。
    返回 `None` = 「没有」或「多命中」——两者都回同一句「尚未注册」（用户能做的动作相同，
    区分只会泄露谁存在）；多命中 ⛔ 不猜「id 最小的那个」。 """
    user = User.objects.filter(username=mobile, is_active=True).first()
    if user is not None:
        return user
    matches = list(
        StaffProfile.objects.filter(
            mobile=mobile, deleted_at__isnull=True, user__is_active=True
        )
        .select_related("user")
        .order_by("id")[:2]
    )
    return matches[0].user if len(matches) == 1 else None


class AppLoginView(AppPlaneMixin, APIView):
    """用户名 + 密码 → Bearer 令牌。登录温和限流（D-0332）：每 IP 失败 ≥20 次/15 分钟拒 30 分钟、
    每账号 ≥10 次/15 分钟锁 15 分钟、成功清零——口径与实现的唯一出处 `apps/core/login_rate_limit.py`
    （⛔ 不在这里另写一份计数）。被挡答复与「账号或密码错误」同一信封形状、共用固定文案，⛔ 不泄露「账号存在」。 """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = _body(request)
        # ── 闸（先于 `authenticate`：被挡的请求不查库、不算口令哈希；D-0332）──
        # `login_source_ip` 与本文件 `_safe_ip` 是同一套取值+校验，只是空值哨兵换成限流的键口径；
        # 短信/注册存 `SmsCode.request_ip` 的仍用 `_safe_ip`（数据库可空列要 None）。
        ip = login_source_ip(request)
        username = data.get("username", "")
        if login_blocked(ip=ip, username=username):
            return api_response(None, msg=RATE_LIMITED_MSG, code=ErrorCode.LOGIN_RATE_LIMITED)
        user = authenticate(
            request,
            username=username,
            password=data.get("password", ""),
        )
        tenant = getattr(request, "tenant", None)
        if user is None or (tenant is not None and not tenant.is_active):
            # `authenticate` 已经覆盖「用户不存在 / 密码错 / is_active=False」三种，
            # 这里再带上停用租户 —— 对外同一句，不泄露是哪一种。
            record_login_failure(ip=ip, username=username)
            return api_response(None, msg="账号或密码错误", code=ErrorCode.VALIDATION)
        # 登录成功 ⇒ 两维失败计数清零（D-0332；锁标记不清，理由见限流模块）。
        clear_login_failures(ip=ip, username=username)
        raw, token = _issue_token(user, str(data.get("client") or DEFAULT_CLIENT))
        return api_response({**_token_payload(raw, token), "user": _user_payload(user, request)})


class AppSmsSendView(AppPlaneMixin, APIView):
    """发登录验证码。**不检查手机号有没有账号**——否则这个端点就成了账号枚举器。"""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = _body(request)
        mobile = normalize_mobile(data.get("mobile"))
        if not mobile:
            return api_response(None, msg="手机号格式不正确", code=ErrorCode.VALIDATION)
        try:
            issue_login_code(mobile=mobile, request_ip=_safe_ip(request))
        except SmsError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        except RuntimeError:
            # ⚠️ **只**捕外部通道的运行时失败（腾讯云报错 / 网络）；配置缺失与数据库错误必须
            # 继续 500——吞掉它们等于把「部署配错了」伪装成「短信发失败了」（评审 Major 2 / E-0010）。
            # 日志只打**脱敏**号码（`core/sms.py` 的既有纪律）。
            logger.exception("短信发送失败 mobile=%s", mask_mobile(mobile))
            return api_response(None, msg="验证码发送失败，请稍后再试", code=ErrorCode.BUSINESS)
        return api_response({"sent": True})


class AppSmsLoginView(AppPlaneMixin, APIView):
    """手机号 + 验证码 → Bearer 令牌。**不自动注册**（注册走自助注册链接）。"""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = _body(request)
        mobile = normalize_mobile(data.get("mobile"))
        code = str(data.get("code") or "").strip()
        if not mobile or not code:
            return api_response(None, msg="手机号与验证码都要填", code=ErrorCode.VALIDATION)
        try:
            consume_login_code(mobile=mobile, code=code)
        except SmsError as exc:
            return api_response(None, msg=str(exc), code=exc.code)

        user = _find_user_by_mobile(mobile)
        tenant = getattr(request, "tenant", None)
        if user is None or (tenant is not None and not tenant.is_active):
            return api_response(None, msg="该手机号尚未注册", code=ErrorCode.VALIDATION)
        raw, token = _issue_token(user, str(data.get("client") or DEFAULT_CLIENT))
        return api_response({**_token_payload(raw, token), "user": _user_payload(user, request)})


class AppLogoutView(AppPlaneMixin, APIView):
    """撤销当前令牌。**幂等**：令牌已失效时返回 401 由认证层处理，客户端照常清本地状态。"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = getattr(request, "auth", None)
        if token is not None and getattr(token, "pk", None):
            type(token).objects.filter(pk=token.pk, revoked_at__isnull=True).update(
                revoked_at=timezone.now()
            )
        return api_response(None)


class AppMeView(AppPlaneMixin, APIView):
    """当前账号信息（与 `/api/auth/me` 同形，走 Bearer）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return api_response({"user": _user_payload(request.user, request)})


# ── 开放注册────────────────────────────────────────────────────

#: 同一来源 IP 的注册尝试额度（默认 **60 次 / 小时**，与 `realname/rate_limit.py` 同一形状）。
#: ⚠️ 数的是「尝试数」不是「新建账号数」（校验失败 / 已注册 / 格式错也都 +1——枚举正是要拦的形状）；
#: 两个值都在函数体里 `getattr(settings, …)` 读（import 期读 `override_settings` 不生效）、读不到用默认值不抛；兜底闸 ⛔ 非配额。
DEFAULT_APP_REGISTER_MAX_ATTEMPTS = 60
DEFAULT_APP_REGISTER_WINDOW_SECONDS = 3600

#: 注册页拉游戏列表只读端点的额度（默认 **600 次 / 小时 / IP**）。比注册松一个数量级是**有意**的：
#: 只读且打开注册页 / 换 tab / 重试都会再拉，按 60 会把正常用户挡在「选游戏」那步；仍是**闸**，
#: 阈值在函数体里 `getattr(settings, …)` 读，测试用 `override_settings` 调低。
DEFAULT_APP_REGISTER_GAMES_MAX_REQUESTS = 600
DEFAULT_APP_REGISTER_GAMES_WINDOW_SECONDS = 3600

#: 超额那一档的文案（自有中文、带动作；⛔ 不抄 DRF 的英文默认句）。
APP_REGISTER_RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"

#: 「手机号已注册」那一档的文案。⚠️ 手机号与用户名是**同一个值**（`username = 手机号`），
#: 所以「用户名重复」与「手机号重复」共用这一句 —— 结构上不存在第二个可枚举的差异。
APP_REGISTER_MOBILE_TAKEN_MSG = "该手机号已注册，请直接登录或找回密码"

#: 密码长度区间**复用**自助改密那条口径（`core/auth_views.NEW_PASSWORD_MIN/MAX`），
#: 文案也照它写（只去掉「新」字：这里设的就是新口令）。
APP_REGISTER_PASSWORD_MSG = f"密码长度需为 {NEW_PASSWORD_MIN}–{NEW_PASSWORD_MAX} 位"

#: `game_ids` 的**形状**错误（不是数组 / 元素不是整数 / id < 1）——这一类错由序列化器字段挡，
#: 请求还没走到「这些 id 在不在本租户」那一步。
APP_REGISTER_GAME_IDS_SHAPE_MSG = "game_ids 必须是游戏 id 的整数数组，且每个 id 都 ≥ 1"

#: `game_ids` 的**取值**错误（本租户查不到 / 已软删 / 属于别的租户）。⚠️ 三类在用户看来是
#: 同一个结果（这个 id 现在不能选），所以共用一句、并**列出是哪几个 id**；
#: ⚠️ 批量里**只要有一个非法就整体拒**（⛔ 不静默丢掉非法项、也不建半个账号）。
APP_REGISTER_GAME_IDS_UNKNOWN_MSG = "游戏不存在、已删除或不属于当前工作室：{ids}"


class AppRegisterSerializer(serializers.Serializer):
    """注册载荷：`mobile` + `password`（**没有 `password_confirm`**，理由见视图 docstring）。
    ⚠️ `password` 的 `trim_whitespace=False`：口令里的空格是有效字符，⛔ 不能被默认 trim 悄悄改掉
    （否则「注册时一个口令、登录时另一个」）；`max_length=NEW_PASSWORD_MAX` 先挡超长再谈哈希（E-0010 那类）。 """

    mobile = serializers.CharField(
        max_length=32,
        trim_whitespace=True,
        error_messages={
            "required": "手机号不能为空",
            "blank": "手机号不能为空",
            "invalid": "手机号必须是字符串",
        },
    )
    password = serializers.CharField(
        max_length=NEW_PASSWORD_MAX,
        trim_whitespace=False,
        error_messages={
            "required": "密码不能为空",
            "blank": "密码不能为空",
            "invalid": "密码必须是字符串",
            "max_length": APP_REGISTER_PASSWORD_MSG,
        },
    )
    #: 可选：游戏 id 列表。**不传 / `[]` = 本租户全部未软删游戏**（App 注册页已不让勾游戏，
    #: 口径见 `AppRegisterView` docstring）；传了 ⇒ 只给这几款。
    #: `allow_empty=True`：显式 `[]` 与不传同义，两者都走「全量」分支。
    game_ids = serializers.ListField(
        child=serializers.IntegerField(
            min_value=1,
            error_messages={
                "invalid": APP_REGISTER_GAME_IDS_SHAPE_MSG,
                "min_value": APP_REGISTER_GAME_IDS_SHAPE_MSG,
                # 元素是 `null`：DRF 默认句是「该字段不能为 null。」（不带字段名）⇒ 换成
                # 本端点自己那句带字段名的（E-0074：自有文案一律带字段名）。
                "null": APP_REGISTER_GAME_IDS_SHAPE_MSG,
            },
        ),
        required=False,
        allow_empty=True,
        error_messages={"not_a_list": APP_REGISTER_GAME_IDS_SHAPE_MSG},
    )

    def validate_mobile(self, value):
        """手机号形状复用 `core/sms.normalize_mobile`（`+86` / 空格 / 连字符归一 + 11 位校验）。"""
        mobile = normalize_mobile(value)
        if not mobile:
            raise serializers.ValidationError("手机号格式不正确：请填 11 位大陆手机号")
        return mobile

    def validate_password(self, value):
        """密码强度 = 仓里现成的长度规则（`NEW_PASSWORD_MIN`–`NEW_PASSWORD_MAX`，唯一出处
        `core/auth_views`）。⚠️ 刻意不调 `password_validation.validate_password`：settings 没配
        `AUTH_PASSWORD_VALIDATORS` ⇒ 调它什么都不做、只会误导读者（判据 `tests/test_account_password.py`）。 """
        if len(value) < NEW_PASSWORD_MIN:
            raise serializers.ValidationError(APP_REGISTER_PASSWORD_MSG)
        return value

    def validate_game_ids(self, value):
        """`game_ids` 必须都在**当前租户**且**未软删**（批量整体判，不挑着要）；判据与
        `AccountWriteSerializer.validate_games` 同源，但**多一条 `deleted_at__isnull=True`**——
        `Game.objects` 不过滤软删（`business/models.py:45`），漏掉会把已下架游戏授权给新打手。返回升序去重 id，行序稳定。 """
        ids = sorted(set(value))
        found = set(
            Game.objects.filter(
                pk__in=ids, deleted_at__isnull=True
            ).values_list("pk", flat=True)
        )
        missing = sorted(set(ids) - found)
        if missing:
            # 一句话 + 具体 id：用户要知道是「哪几个不能选」，而不是笼统的「参数错误」。
            raise serializers.ValidationError(
                APP_REGISTER_GAME_IDS_UNKNOWN_MSG.format(ids=missing)
            )
        return ids


def _app_plane_bucket_key(scope: str, ip: str) -> str:
    """打手 App 限流桶键 `app-plane-<scope>:<租户 schema>:<来源 IP>`；scope 三档（register / games /
    wx-openid）各有自己的桶与阈值，⛔ 读口与写口不共用桶；schema 必须进键（默认 cache 进程级跨租户共享）；
    取不到合法 IP 用空串显式桶 ⛔ 不碰合法 IP 的桶（nginx 覆盖写 X-Real-IP ⇒ 正常请求永远有合法 IP）。"""
    return f"app-plane-{scope}:{connection.schema_name}:{ip}"


def fixed_window_allowed(*, scope: str, ip, limit: int, window: int) -> bool:
    """`cache.add` + `incr` 的**固定窗口**闸（照 `realname/rate_limit.py`）。⛔ 不抄 `selfreg_views` 的
    `get`+`set`——每次失败重设 TTL ⇒ 持续请求永不过期；`add` 原子建桶、TTL 只在建桶那一刻设一次。已知软限
    （如实登记，同 D-0251 ④）：默认 `LocMemCache` 进程内、生产 `--workers 2` ⇒ 最坏 2 × 阈值；沿用既有缓解写法，⛔ 不自造第三套实现。 """
    key = _app_plane_bucket_key(scope, ip or "")
    if cache.add(key, 1, timeout=window):
        return True  # 本窗口第一次：TTL 就在这一刻设上，窗口从这里起算
    try:
        count = cache.incr(key)
    except ValueError:
        # `add` 判负到 `incr` 之间键恰好滚过窗口（TTL 到期的边界竞态）⇒ 重建桶。
        # 极端竞态下少记一次：与上面登记的软限同族，如实接受。
        cache.set(key, 1, timeout=window)
        return True
    return count <= limit


def _register_allowed(ip) -> bool:
    """这次**注册尝试**放不放行（写口，数的是尝试数、不是新建账号数）。"""
    return fixed_window_allowed(
        scope="register",
        ip=ip,
        limit=getattr(
            settings, "APP_REGISTER_MAX_ATTEMPTS", DEFAULT_APP_REGISTER_MAX_ATTEMPTS
        ),
        window=getattr(
            settings, "APP_REGISTER_WINDOW_SECONDS", DEFAULT_APP_REGISTER_WINDOW_SECONDS
        ),
    )


def _register_games_allowed(ip) -> bool:
    """这次**拉游戏列表**放不放行（读口，阈值比注册松一个数量级）。"""
    return fixed_window_allowed(
        scope="games",
        ip=ip,
        limit=getattr(
            settings,
            "APP_REGISTER_GAMES_MAX_REQUESTS",
            DEFAULT_APP_REGISTER_GAMES_MAX_REQUESTS,
        ),
        window=getattr(
            settings,
            "APP_REGISTER_GAMES_WINDOW_SECONDS",
            DEFAULT_APP_REGISTER_GAMES_WINDOW_SECONDS,
        ),
    )


def _mobile_taken(mobile: str) -> bool:
    """这个手机号是否已有人用，**两条判据都要看**：① `username == 手机号`（注册写入口径，不过滤
    `is_active`——停用账号占的号不许被注册顶掉）；② `StaffProfile.mobile`（手工老账号用户名可能不是
    手机号，只看 ① 会漏）。②只认 `deleted_at IS NULL`：软删档案不占号，「删档留号」的半删态由 ① 兜住。 """
    if User.objects.filter(username=mobile).exists():
        return True
    return StaffProfile.objects.filter(
        mobile=mobile, deleted_at__isnull=True
    ).exists()


class AppRegisterView(AppPlaneMixin, APIView):
    """`POST /api/app/auth/register/`：手机号 + 密码 → 当场建号并**直接登录**（出参与 `AppLoginView`
    同形，不要求 `password_confirm`——客户端输入辅助，不增安全性也不改善防刷）。⚠️ 固有枚举面：
    已注册 / 未注册的响应必然不同，只能「不额外制造差异 + 探测变慢」，⛔ 别读成已解决；实现口径见下方注释。 """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        data = _body(request)
        # ── 1. 来源 IP 的固定窗口闸（**先于**一切校验：枚举者构造合法号段也照样计数）──
        if not _register_allowed(_safe_ip(request)):
            return api_response(
                None,
                msg=APP_REGISTER_RATE_LIMITED_MSG,
                code=ErrorCode.APP_REGISTER_RATE_LIMITED,
            )
        # ── 2. 形状校验：请求来源的值一律先过序列化器（AGENTS.md §5-E）──
        # `raise_exception=True` ⇒ 经统一异常处理器变 200 + code 1000 + 第一条中文文案；
        # 不用手写 `api_response` 是为了让「信封只有一处定义」这条纪律不被新端点绕过。
        serializer = AppRegisterSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        mobile = serializer.validated_data["mobile"]
        password = serializer.validated_data["password"]
        # 缺席 ⇒ `None`；显式 `[]` 被序列化器归一成空列表——两者都走下面「全部未软删游戏」分支。
        game_ids = serializer.validated_data.get("game_ids")

        # ── 3. 已占用 ⇒ 与并发撞唯一约束**同一句**（⛔ 不回任何账号信息）──
        if _mobile_taken(mobile):
            return api_response(
                None, msg=APP_REGISTER_MOBILE_TAKEN_MSG, code=ErrorCode.CONFLICT
            )

        try:
            with transaction.atomic():
                user = User(
                    username=mobile,
                    # 没有姓名入参 ⇒ `first_name` 先留空，等下面按租户编号生成（见那段注释）；
                    # ⛔ 不拿手机号塞 first_name（那是另一个字段的语义）。
                    is_staff=False,
                    is_superuser=False,
                )
                user.set_password(password)
                user.save()
                # ⭐ 显示名 = 租户配置的**账号编号**（与自助注册线同一个生成器）；未配置编号 ⇒ 空串。
                # ⚠️ 必须在事务内调用（`generate_display_name` 对配置行 `select_for_update()`，
                # 并发注册靠行锁排队，编号才不重号）。
                display_name = generate_display_name("")
                if display_name != "":
                    user.first_name = display_name
                    user.save(update_fields=["first_name"])
                profile = StaffProfile.objects.create(
                    user=user,
                    mobile=mobile,
                    # 开户人 = 注册者本人：这条记录回答「谁把这个账号开进系统的」，
                    # 而开放注册里做这件事的就是他自己（自助注册那条线记的是发链接的人事）。
                    opened_by=user,
                    # 登记最初的注册来源，供账号管理与自动结算使用。
                    account_source=AccountSource.APP,
                    # 合作用户 = 打手（业务口径；内部成员是员工账号）。
                    member_type=MemberType.PARTNER,
                    # D-0269 ⑥：新注册打手的代练持单上限。⚠️ 常量值 3 的唯一出处是
                    # 自助注册线（`grab_limit_dl=0` 才是「不限制」，这里必须留正数）。
                    grab_limit_dl=SELF_REGISTER_GRAB_LIMIT_DL,
                )
                # 游戏权限：**照抄**自助注册那条路的现成写法（`selfreg_views.py:456` 的
                # `profile.games.set(game_ids)`；关系表 `StaffProfile.games` M2M，`accounts/models.py:148`），
                # ⛔ 不自造第三种写入口；⭐ 不传 / 空列表 ⇒ 本租户全部未软删游戏，逐游戏留痕不分叉。
                if game_ids:
                    profile.games.set(game_ids)
                else:
                    game_ids = list(
                        Game.objects.filter(deleted_at__isnull=True)
                        .order_by("pk")
                        .values_list("pk", flat=True)
                    )
                    profile.games.set(game_ids)
                # 查无同名 Group ⇒ 不翻译的 500：那是租户基础数据坏了，不是请求错误
                # （与 `_persist_account` / `SelfRegisterPublicView` 同口径）。
                group = Group.objects.get(name=roles.WORKER)
                user.groups.set([group])
                # ── 4. 审计：actor = 新账号本人、target_name = 手机号。动作码复用 `user.create`
                # （自助注册线先例：同样是「新增一个用户」，⛔ 不新造第二个语义相同的动作码）。──
                record(
                    log_type=ACTION_LOG_TYPES["user.create"],
                    action="user.create",
                    actor=mobile,
                    actor_id=user.pk,
                    target_kind="user",
                    target_id=user.pk,
                    target_name=mobile,
                )
                # 逐游戏一行 `user.game_permission_add`（与 `_record_account_created` /
                # `_record_self_registered` 同形；游戏名走 `_game_names`，查不到的跳过）。
                for name in _game_names(game_ids):
                    record(
                        log_type=ACTION_LOG_TYPES["user.game_permission_add"],
                        action="user.game_permission_add",
                        actor=mobile,
                        actor_id=user.pk,
                        target_kind="user",
                        target_id=user.pk,
                        target_name=mobile,
                        game_name=name,
                    )
                raw, token = _issue_token(
                    user, str(data.get("client") or DEFAULT_CLIENT)
                )
        except IntegrityError:
            # 并发同手机号撞 `auth_user.username` 唯一约束（预检之后、写入之前的那一枪）：
            # 同事务已整体回滚 ⇒ 没有半截账号 / 档案 / 审计行。文案与预检同一句。
            return api_response(
                None, msg=APP_REGISTER_MOBILE_TAKEN_MSG, code=ErrorCode.CONFLICT
            )
        return api_response(
            {**_token_payload(raw, token), "user": _user_payload(user, request)}
        )


class AppRegisterGamesView(AppPlaneMixin, APIView):
    """`GET /api/app/auth/register/games/`：注册页**匿名**游戏下拉，载荷最小化只有 `{id, name}`
    （匿名端点上多余字段 = 免费的信息泄露面）；只回未软删、⛔ 不按 `is_active` 过滤——与注册写入侧校验
    （`validate_game_ids`）同口径，要改两处同一次改。排序显式 `-sort_order, id`（同录单/详情页下拉，⛔ 不按名字排）；独立限流桶。 """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        if not _register_games_allowed(_safe_ip(request)):
            return api_response(
                None,
                msg=APP_REGISTER_RATE_LIMITED_MSG,
                code=ErrorCode.APP_REGISTER_RATE_LIMITED,
            )
        rows = Game.objects.filter(deleted_at__isnull=True).order_by("-sort_order", "id")
        return api_response(
            {"items": [{"id": game.pk, "name": game.name} for game in rows]}
        )
