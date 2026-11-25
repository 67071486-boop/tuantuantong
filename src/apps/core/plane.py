"""非请求入口（管理命令 / 任务队列 / webhook）的平面判定（spec D14）。会话强绑身份平面（D-0009）是**请求期**机制：
`TenantSessionGuardMiddleware` 比的是`session["identity_plane"]` 与 `connection.schema_name`。
非请求入口没有会话，`connection.schema_name` 由调用方自己 `set_tenant()` 决定——此时"查询携带租户上下文"**并不足以**防止跨平面操作：一个把 A 的凭据交给 B 的 schema 去执行的任务，两边都"有租户上下文"，缺陷只体现在**凭据平面与执行平面不是同一个**。所以非请求入口必须**显式声明**凭据所属平面，并在这里校验：声明本身是调用方的责任，本函数只负责"声明的值必须与执行平面一致、且必须存在"。"""


def on_public_plane() -> bool:
    """连接是否落在平台平面（`public`）。**本函数从 `apps/rbac/permissions.py` 上提到这里**（判据统一）：`apps/core/permissions.py::is_tenant_admin` 的 `public` 分支要问同一个问题，而 `apps/core` → `apps/rbac`（`TENANT_APPS` 里的租户 app）是**反向依赖**——实测（本仓库）未配置 Django settings 时直接从 `apps/rbac/permissions.py` 引判据会在导入期抛 `ImproperlyConfigured: Requested setting TENANT_DB_ALIAS`（`django_tenants/utils.py:193` 在模块导入期就读 settings，基座模块不该带这个前置条件）；配好 settings 后 import 是通的 ⇒ 严格意义上没有循环，但反向依赖仍让上提到 `apps/core/` 更合适。
    `apps/rbac/permissions.py` 保留同名转发（`_on_public_plane`）。⚠️ 如实登记：`apps/accounts/permissions.py::is_account_admin` 里还有一处**等价的内联**比较；将来若要收口，应指向本函数。⚠️ **两个 import 放在函数内**（不是防御性写法）：本模块其余部分是纯函数，模块级 `django_tenants.utils` 会让任何 `import apps.core.plane` 都需要 settings——正是上面实测到的那个失败。
    """
    from django.db import connection
    from django_tenants.utils import get_public_schema_name

    return connection.schema_name == get_public_schema_name()


def assert_plane(identity_plane, expected_schema):
    """凭据平面（`identity_plane`）必须等于执行平面（`expected_schema`），否则抛。`identity_plane is None` 也抛：
    那是"调用方**忘了**声明"，而不是"无需声明"。默认放行会把"显式声明"变成可选项，整个判定立刻归零——所以 None 与不符同等对待。
    抛 `PermissionError`（Python 内建）而不是 DRF 的 `PermissionDenied`：非请求入口没有 HTTP 请求可供 DRF 渲染（spec D14 的代价）。"""
    if identity_plane is None or identity_plane != expected_schema:
        raise PermissionError(
            f"执行平面 {expected_schema!r} 与凭据平面 {identity_plane!r} 不一致"
        )


#: 平台平面没有该功能时的 404 文案（spec §5.6）。**唯一出处**。
PLANE_NOT_FOUND_MESSAGE = "当前平面没有此功能"


def plane_not_found_response():
    """平台平面上的「平面没有此功能」响应（404 + 固定 msg）。两个调用点：
    `apps/core/profile_views.py::MyProfileView._plane_guard` 与`apps/core/auth_views.py::ChangePasswordView.post`。抽到这里是**单一出处**：
    两处各写一遍时，msg 或状态码改一处、另一处悄悄漂移，而两边都有测试盯着各自那一份⇒ 谁也不会红（spec §5.6）。⚠️ `error_response` 在**函数内导入**（与 `on_public_plane` 的 `django_tenants.utils`同一条理由）：本模块其余部分是纯函数，模块级 `rest_framework` 会让任何`import apps.core.plane` 都多一层依赖。"""
    from apps.core.envelope import error_response

    return error_response(404, PLANE_NOT_FOUND_MESSAGE, 404)