"""认证端点：`/api/auth/{csrf,login,logout,me,site,password}`。发会话、销会话、回显身份、发 CSRF cookie、自助改密、公开租户名。改密只动自己的 `password` 列；站点名只读 `request.tenant`。
用户 CRUD 不做（账号管理走 `apps/accounts`）。 `profile` 在 `apps/core/profile_views.py`。登录有温和限流：每 IP 失败 ≥20 次/15 分钟全口拒 30 分钟；每账号失败 ≥10 次/15 分钟锁 15 分钟；成功清零。
口径只在 `apps/core/login_rate_limit.py`（同批罩住 App 密码登录与公众号绑定页登录）。
"""
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.db import connection, transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.core.envelope import api_response, error_response
from apps.core.error_codes import ErrorCode
from apps.core.login_rate_limit import RATE_LIMITED_MSG, clear_login_failures, login_blocked, login_source_ip, record_login_failure
from apps.core.plane import on_public_plane, plane_not_found_response

# D8（口径已由 spec §4.6 收窄）：**视图内**的失败——凭证错误 / 用户不存在 / 用户停用—— **一律 401 + 这一句**。三种情形必须回同一句，否则响应本身就泄露了「这个账号存在」「这个账号被停用了」。
# ⚠️ **「租户停用」不在这一句的范围里**：它由 `TenantSessionGuardMiddleware` 在**进入视图前** 短路（含匿名请求），用**守卫自己的** msg（`会话与当前租户不匹配`）且**不带** `WWW-Authenticate`；「平面失配」同理——两者都是 spec §4.6 的显式例外。
# 下面 `LoginView.post` 里那段 `is_active` 兜底走的才是本句：它只在**守卫被摘掉 / 顺序写错**时可达（实测在当前接线下达不到），不是"租户停用"这条路径的主口径。
FAIL_MESSAGE = "用户名或密码错误"

# spec §4.6：**由认证类 / DRF 产出（含视图自己构造）** 的 401 **必须带 `WWW-Authenticate`**，值取 `TenantBoundAuthentication.authenticate_header()` 的返回值。 ⚠️ **例外是守卫短路产出的两条 401**（停用租户 / 平面失配）：它们**不带**这个头
# ——"是不是认证挑战"正是测试区分两道防线的唯一可观测差异（spec §4.6）。为什么这里要手写：视图自己构造 401 时不经过 DRF 异常链，那个头是从 `get_authenticate_header()` 算出来塞进去的。不手写，登录失败就没有这个头，
# 同一状态码出现两种响应契约。
AUTH_HEADER = {"WWW-Authenticate": "Session"}


def _login_failed():
    """登录失败的唯一出口：一个地方决定 401 的形状，两处调用点无从写歪。"""
    return error_response(401, FAIL_MESSAGE, 401, headers=AUTH_HEADER)


def _my_avatar_url(user):
    """请求者**本人**头像的短时效读链接；没有档案 / 没传过 / 取不到一律 `None`。顶栏头像同步线：`/api/auth/{login,me}` 的 `data.user.avatar_url`。 ⚠️ **签名逻辑不在这里**：`apps/core/profile_views.py::_media_url` 是它的唯一出处（「对象名 → 短时效 URL」这条规则只写那一处，E-0011 同族）。 ⚠️ **`public` 平面单独早返回**（理由与 `apps/business/game_scope.py::games_of` 那条同源）： `StaffProfile` 是租户 app ⇒ `accounts_staffprofile` 这张表**只在租户 schema 里**；平台平面上
    摸反向描述符会去查一张**不存在**的表、抛 `ProgrammingError`——它**不是** `AttributeError` 的子类，`getattr` 的默认值兜不住 ⇒ 不早返回会让运营后台的 `/api/auth/me` 直接 500。平台账号在本口径里没有头像 ⇒ `None`。 ⚠️ 只读**请求者自己**的档案：取值走 `user` 上的反向 OneToOne（`related_name="staff_profile"`），本函数**没有任何「指定别人」的参数** ⇒ 裁定 A4「头像仅本人可见」是**结构保证**，不是纪律。
    """
    from apps.core.plane import on_public_plane
    from apps.core.profile_views import _media_url

    if on_public_plane():
        return None
    return _media_url(getattr(user, "staff_profile", None), "avatar_key")


def _user_payload(user, request=None, *, include_web_chat=False):
    """`data.user`（spec §4.2 字段表）。login 与 me 共用同一形状，前端只写一份解析。 `include_web_chat`（网页端 Chatwoot 小部件）：**只有网页会话的 `LoginView` / `MeView` 传 True**，App 平面（`/api/app/auth/*`）不传。配置齐全时追加 `chat_identifier` / `chat_identifier_hash` / `chat_base_url` / `chat_website_token` 四个键（同生同灭），见 `apps/accounts/web_chat.py`（判据与公式的**唯一出处**，⛔ 不在这里重写）。默认 False：App 的登录 / me 载荷形状逐字不变（那套走的是另一个收件箱）。 `identity_plane` 取**当前** schema 名而不是会话里存的值：这是端点所在平面的事实陈述，不是对会话的复述——会话是否真的绑在这个平面上，由守卫中间件与 `TenantBoundAuthentication` 判定（spec §4.3）。两者职责不同，不要合并。 **两个管理员字段必须同时暴露**（账号线 spec §5.4；后两者在租户平面上同真同假）： `is_tenant_admin` 是 P5 的契约字段，判据本身由 `apps.core.permissions.is_tenant_admin`
    定义（租户平面 = 持「系统管理员」职位；`public` 平面仍看 `is_superuser`——运营后台 SPA 菜单只靠这个字段）；`is_account_admin` 是账号线的准入判据。 **这里不重写判据**：两个字段各自走那一个函数，各写一遍就会与前端的菜单门各说各话。（我的资料线）产出两键，都由**本函数一处**产出（login 与 me 共形）： - `tenant_name`：当前租户的 `name`（工作室名）。取值 = `getattr(request.tenant, "name", "")`， **结构上不发 SQL**（读码口径，**不是**本任务的实测）：`django_tenants/middleware/main.py` 的 `select_related('tenant')` 已把实例一并取回、`request.tenant` 就是它 ⇒ 读 `.name` 不会再查库；本机/租户平面拿到的就是租户名（spec §5.4）。`request` 是**可选**参数：老调用点不传时退化成空串，而不是抛 `AttributeError`。 - `role`：**单值**职位名，复用 `apps/accounts/payloads.py::_first_role_name` （名称升序首个）——⛔ 不在这里再写一份排序规则（E-0011）。 `roles`（多值）保留不变。
    （顶栏头像同步线）另产出一键： - `avatar_url`：请求者**本人**头像的短时效读链接（没上传过 / 平台平面 ⇒ `None`）。 ⚠️ 取数走 `_my_avatar_url`（本模块）→ 复用 `profile_views._media_url`， ⛔ 不在这里再写第二份签名逻辑；⛔ 也只读**请求者自己**的档案（A4：头像仅本人可见）。 ⚠️ `request` 仍可选：缺省时该键退化成 `None`（不取数、不抛）。
    """
    # ⚠️ `apps.accounts.payloads` 是租户层，理由同下面几条：本模块在 urlconf 载入期就被
    # import（`apps.core.urls`），放模块级会让平台层在导入期加载租户层模块。
    # ⛔ 排序口径**不在这里重写**：`_first_role_name` 是它的唯一出处。
    from apps.accounts.payloads import _first_role_name
    # ⚠️ **函数内导入**（不是文件顶部）：`apps.accounts.permissions` 依赖
    # `apps.core.permissions` 与 `apps.business.roles`，而本模块被 `apps.core.urls`
    # 导入——放模块级会多一条可被 app 加载顺序触发的导入链。判据不要在这里重写。
    from apps.accounts.permissions import is_account_admin
    from apps.core.permissions import is_tenant_admin
    # 同一条理由：`apps.rbac` 是**租户 app**，本模块在 urlconf 载入期就被 import （`apps.core.urls`），放模块级会让平台层在导入期加载租户层模块。
    # ⚠️ 平面判断**不在这里**：`user_node_codes` 自己已对 `public` 平面 fail-closed 返回空集——重复实现会让两处判据有机会漂移。
    from apps.rbac.permissions import user_node_codes
    # 同一条理由（函数内导入）：`apps.business` 是租户层，本模块在 urlconf 载入期就被
    # import。规则本体不在这里重写——`apps.business.game_scope.games_of` 是它的一处出处。
    from apps.business.game_scope import games_of
    from apps.internal_chat.realtime import internal_chat_available

    tenant = getattr(request, "tenant", None)
    payload = {
        "id": user.pk,
        "username": user.get_username(),
        "display_name": getattr(user, "first_name", "") or "",
        "identity_plane": connection.schema_name,
        "is_tenant_admin": is_tenant_admin(user),
        "is_account_admin": is_account_admin(user),
        "roles": list(user.groups.values_list("name", flat=True)),
        "permissions": sorted(user.get_all_permissions()),
        # 当前账号的游戏权限 id（升序）。前端数据页的游戏下拉按它过滤；**配置面** （游戏维护页 / 账号授权页）仍走全量接口——否则会出现「看不到没勾的游戏 ⇒ 也勾不上」。
        # ⚠️ 没有档案 / 没勾游戏的账号拿到 `[]`（`games_of` 的严格口径），**不抛**：本端点在 `public` 平面上也要能跑（那里没有 `StaffProfile` 的数据，函数也不去查它）。
        "game_ids": sorted(games_of(user).values_list("pk", flat=True)),
        # 节点 code 并集：**本系统的权威判据**（D-0028）。`permissions` 是 P5 的
        # Django codename，冻结为 legacy——两套语义并存，但菜单/路由/接口都读这一份。
        "menu_permissions": sorted(user_node_codes(user)),
        # 工作室名（当前租户的 `name`）。取不到就是空串——登录页/侧栏静默回落，不是错误。
        "tenant_name": getattr(tenant, "name", "") or "",
        # 单值职位（名称升序首个，无则空串）。取值口径走 `_first_role_name`，不另写一份。
        "role": _first_role_name(user) or "",
        # 本人头像的短时效读链接（顶栏头像同步线）：没有上传过 / 平台平面 ⇒ None。
        # ⚠️ `request` 缺席 = 缺省调用 → 退化成 None（既有可选参数契约：不抛）。
        "avatar_url": _my_avatar_url(user) if request is not None else None,
    }
    if internal_chat_available():
        payload["internal_chat_enabled"] = True
    if include_web_chat:
        if is_tenant_admin(user) and not on_public_plane():
            payload["dashboard_type"] = "admin"
        elif on_public_plane():
            payload["dashboard_type"] = ""
        else:
            layouts = {value or "" for value in user.groups.values_list("role__dashboard_type", flat=True)}
            payload["dashboard_type"] = layouts.pop() if len(layouts) == 1 else ""
        # ⚠️ 函数内导入：`apps.accounts` 是租户层，理由同本函数上面几条（本模块在
        # urlconf 载入期就被 import）。判据与公式只在 `web_chat_identity` 里写一份。
        from apps.accounts.web_chat import web_chat_identity

        payload.update(web_chat_identity(user))
    return payload


@method_decorator(csrf_protect, name="dispatch")
class LoginView(APIView):
    """显式保护匿名登录。DRF 是 csrf_exempt（rest_framework/views.py:149），
    会话认证也跳过匿名请求（rest_framework/authentication.py:117-133）。
    """

    permission_classes = [AllowAny]

    def post(self, request):
        # ── 登录温和限流（D-0332）：两维闸都在 `authenticate` 之前——被挡的请求不查库、
        # 不算口令哈希。阈值与口径见 `apps/core/login_rate_limit.py`（本视图是它的三个
        # 消费者之一；⛔ 不要在这里另写一份计数）。──
        username = request.data.get("username", "")
        ip = login_source_ip(request)
        if login_blocked(ip=ip, username=username):
            # ⛔ 与登录失败**同一 HTTP 形状**（401 + `WWW-Authenticate`，spec §4.6 的视图构造 401 契约）；业务码 1038 让前端/排障能把「被限流」与「凭据错」分开
            # （`ErrorCode.LOGIN_RATE_LIMITED` 的登记注释）。文案是三口共用的一句固定话， ⛔ 不含任何「账号存在」信息。
            return error_response(
                int(ErrorCode.LOGIN_RATE_LIMITED), RATE_LIMITED_MSG, 401, headers=AUTH_HEADER
            )
        tenant = request.tenant
        # 停用租户在登录时也拒。守卫中间件排在视图之前，当前接线这一支不可达
        # （401 来自守卫，msg 是「会话与当前租户不匹配」）。仍留着：中间件被摘掉或
        # 顺序写错时，这里是唯一还在的兜底。
        if tenant is not None and not tenant.is_active:
            return _login_failed()
        user = authenticate(
            request,
            username=username,
            password=request.data.get("password", ""),
        )
        # authenticate 一次覆盖三种失败：用户名不存在、密码错、用户 `is_active=False` （`ModelBackend.user_can_authenticate`，django/contrib/auth/backends.py）。
        # 三种都返回 None，所以这里**没有分支**能泄露到底错在哪——这是结构保证，不是"记得别写分支"的纪律。
        if user is None:
            # 凭据失败：给 IP 维与账号维各记一笔（触线即设锁标记；D-0332）。
            record_login_failure(ip=ip, username=username)
            return _login_failed()

        login(request, user)
        # 登录成功 ⇒ 两维失败计数清零（D-0332；锁标记不清——锁着的时候到不了这里，
        # 理由见 `login_rate_limit.clear_login_failures`）。
        clear_login_failures(ip=ip, username=username)
        # 必须写在 login() 之后：匿名会话走 cycle_key()，数据还在；
        # 会话里已有另一个已认证用户时 login() 会 flush()，之前写的键会丢。
        # identity_plane 和 OAuth 的 openid 都属于「必须活过登录」。
        request.session["identity_plane"] = connection.schema_name
        # 网页会话的登录响应要带网页客服身份（登录成功即挂气泡，不等一次刷新）。
        response = api_response(
            {"user": _user_payload(user, request, include_web_chat=True)}
        )
        from apps.core.wxpusher_login import remember_bound_browser

        remember_bound_browser(request, response)
        return response


class LogoutView(APIView):
    """登出。**幂等**：未登录也 200（spec §4.2）。

    刻意不加 `csrf_protect`：加了它，未登录的裸 POST 会先被 CSRF 挡成 403，
    「未登录也 200」就没了。已登录时 `SessionAuthentication.enforce_csrf` 自己把关。
    """

    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        response = api_response(None)
        from apps.core.wxpusher_login import forget_bound_browser

        forget_bound_browser(response)
        return response


class MeView(APIView):
    """当前用户。"""

    # 必须显式声明：DRF 出厂默认是 AllowAny，fail-closed 默认权限类只兜「漏写」。
    # 写成 IsAuthenticated 之后，默认值被改回去，匿名也拿不到身份。
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return api_response(
            {"user": _user_payload(request.user, request, include_web_chat=True)}
        )


class SiteView(APIView):
    """站点信息（公开）：登录页在**登录前**要显示工作室名（对标 `/Login/GetCompany`，spec §5.5）。 ⚠️ **租户上下文只来自 Host**（`TenantMainMiddleware` 解析域名得到 `request.tenant`）；本端点**不接受任何参数**，也不做平面分支——取不到就给空串（登录页静默回落）。
    ⚠️ 这是本项目**第一个公开回租户名的端点**：把「这个域名属于哪个工作室」暴露给未认证访问者。与对标一致（它那个端点同样无需凭据），且租户名本来就印在登录卡片上 ⇒ 接受（代价见 spec §13）。
    """

    permission_classes = [AllowAny]

    def get(self, request):
        tenant = getattr(request, "tenant", None)
        return api_response({"tenant_name": getattr(tenant, "name", "") or ""})


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """CSRF 引导：GET 一下就把 `csrftoken` cookie 下发。

    前端在第一个非安全方法（含登录）之前先打这个端点。没有它，前端只能靠
    "先渲染一个带 {% csrf_token %} 的页面"来拿 token——SPA 没有那个页面（D15）。
    """

    permission_classes = [AllowAny]

    def get(self, request):
        return api_response(None)


#: 自助改密的新口令长度区间（spec §3-D6）。上界 18 有对标出处（证据件三个密码框
#: `maxlength="18"`）；**下界 6 是推断**（spec §13 已登记）。
NEW_PASSWORD_MIN = 6
NEW_PASSWORD_MAX = 18


class ChangePasswordView(APIView):
    """自助改密码（spec §5.3）。**不是**管理员重置换别人（那是 `accounts/views.py::reset_password`）。 - 判据 = 登录 + `home.change_password`（bid 101，本线翻真）； - 成功后 `update_session_auth_hash` ⇒ **保持登录**（不把人踢下线）；
    - ⛔ 任何分支都不把口令写进日志 / 审计；本端点**刻意不记操作日志** （对标目录无此动作 + 本仓裁定 #4「认证层事件不记」⇒ 登记进 `DELIBERATELY_NOT_LOGGED`）。
    """

    permission_classes = [IsAuthenticated]

    #: 节点门拒人时的 403 msg。⚠️ **必须显式设**：不给的话 DRF 回的是英文默认句，
    #: 中文界面直接显示它。机制与 `MyProfileView.NODE_DENIED_MESSAGE` 同款。
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        """平面守卫**放在视图体里**（见 `post`），所以这里在 `public` 平面上**不加节点门**——否则 `has_node` 在 public 恒假，请求会先被 403 挡掉，「public 平面回 404」这条判据就永远测不到（spec §5.6）。
        ⚠️ `has_node` **在方法内导入**：`apps.rbac` 是租户 app，而本模块在 urlconf 载入期就被 import（`apps.core.urls`）——放模块级会让平台层在导入期加载租户层模块（与 `_user_payload` 的各处函数内导入同一条理由）。
        """
        permissions = [IsAuthenticated()]
        if not on_public_plane():
            from apps.rbac.permissions import has_node

            gate = has_node("home.change_password")
            # ⚠️ 只在本视图自己造出来的那个类上设 message（`has_node()` 每次返回新类）。
            gate.message = self.NODE_DENIED_MESSAGE
            permissions.append(gate())
        return permissions

    def post(self, request):
        if on_public_plane():
            # 与 `MyProfileView._plane_guard` **同一出处**（`apps/core/plane.py`），
            # msg / 状态码不会两边各写一遍后漂移（spec §5.6）。
            return plane_not_found_response()

        # ⚠️ 请求体**类型**先核：DRF 的 `JSONParser` 只 `json.load` 顶层、不校验类型（`rest_framework/parsers.py::JSONParser.parse`），数组 / 字符串 / 数字都会原样成为 `request.data` ⇒ 直接 `.get()` 抛 `AttributeError` ⇒ **HTTP 500**
        # （实测：`data="[]"` 也会走到这一支）。这是「非法输入一律 `code=1000`」的形状之一（E-0010 同族），本仓已有同款先例（`apps/core/profile_views.py::MyProfileView._validate_only_editable`）。
        # ⛔ 本路由**不在** `tests/test_no_route_500.py` 的枚举来源里（只收 business / audit） ⇒ 这条兜底没有结构网兜底，判据落在 `test_my_profile_api` 里。
        if not isinstance(request.data, dict):
            return api_response(None, msg="请求体必须是对象", code=ErrorCode.VALIDATION)

        raw_old = request.data.get("old_password")
        raw_new = request.data.get("new_password")
        raw_confirm = request.data.get("confirm_password")
        # 只有「缺席 / None」归一成空串（保持既有行为：空体 ⇒ 走「原密码不正确」）；
        # ⛔ **不要用真值判断**（`or ""`）来区分它们——那会把 `[]` / `0` / `""` 混为一谈，
        # 见下面那条类型关。
        old = "" if raw_old is None else raw_old
        new = "" if raw_new is None else raw_new
        confirm = "" if raw_confirm is None else raw_confirm
        # 容器是 dict 不代表值是字符串。非字符串会在 len() 或 set_password()
        # 上抛 TypeError，变成 HTTP 500。make_password 在哈希前就拒绝非 str/bytes，
        # 不会把 list 悄悄哈希成口令再回 200。
        if not all(isinstance(v, str) for v in (old, new, confirm)):
            return api_response(None, msg="密码字段必须是字符串", code=ErrorCode.VALIDATION)

        # 校验顺序**先老后新**：不然「新口令格式对不对」会泄露给不知道老口令的人。
        # ⚠️ 校验失败一律 `api_response(..., code=ErrorCode.VALIDATION)` = **HTTP 200 + code 1000**
        # （`apps/core/exceptions.py` 的口径：业务失败不改 HTTP 状态码）。⛔ 不要用 400。
        if not request.user.check_password(old):
            return api_response(None, msg="原密码不正确", code=ErrorCode.VALIDATION)
        if not (NEW_PASSWORD_MIN <= len(new) <= NEW_PASSWORD_MAX):
            return api_response(
                None,
                msg=f"新密码长度需为 {NEW_PASSWORD_MIN}–{NEW_PASSWORD_MAX} 位",
                code=ErrorCode.VALIDATION,
            )
        if confirm != new:
            return api_response(None, msg="两次输入的新密码不一致", code=ErrorCode.VALIDATION)
        if new == old:
            return api_response(None, msg="新密码不能与原密码相同", code=ErrorCode.VALIDATION)

        with transaction.atomic():
            request.user.set_password(new)
            # ⚠️ `set_password()` **不落库**（只改内存），漏掉 save 是静默失效：
            # 接口回 200 但账号还用旧口令登得进去。
            request.user.save(update_fields=["password"])
            # 口令哈希变了 ⇒ 不刷新会话的话本请求之后立刻被 `get_user()` flush
            # （看起来像"改完就掉线"）。
            update_session_auth_hash(request, request.user)
        return api_response(None)
