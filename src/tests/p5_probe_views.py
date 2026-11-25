"""只给测试用的受保护端点：验证"拒绝"与"无权限"的机制。

它不是交付物的一部分——P5 不引入业务端点（spec §6.1）。

**按任务切分**（预检裁决）：本文件由 Task 2 建立，只放 `whoami`；
`plain_whoami`（裸 Django 视图）与 `write_probe`（副作用载体）由 Task 7 补；
`service_only` / `perm_only` 与它们的
`from apps.core.permissions import has_permission, has_role` 由 Task 9 补——
`apps/core/permissions.py` 到 Task 9 才存在，提前导入会让 Task 2/7 直接 ImportError。
`node_only`（RBAC 线的节点判据载体）由 `rbac-foundation` 分支的 Task 5 补。

⚠️ 装饰器用的是 **小写工厂** `has_role(...)` / `has_permission(...)`，不是计划里写的
`HasRole("客服")`：DRF 会无参实例化 `permission_classes` 里的每一项
（`rest_framework/views.py:283`），而 `HasRole("客服")` 是**实例**，
实测 `TypeError: 'HasRole' object is not callable`。理由与实测见
`apps/core/permissions.py` 的模块 docstring。
"""
from django.contrib.auth.models import Group
from django.db import connection
from django.http import JsonResponse

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import has_permission, has_role
from apps.rbac.permissions import has_node


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def whoami(request):
    """回显身份：用于断言"谁在看这个请求"。"""
    return JsonResponse(
        {
            "auth": request.user.is_authenticated,
            "pk": request.user.pk,
            "username": request.user.get_username(),
            "conn_schema": connection.schema_name,
        }
    )


def plain_whoami(request):
    """**裸 Django 视图**（不经 DRF）——用来证明守卫是承重件（spec §5.1-6）。

    如果这里也用 DRF，`TenantBoundAuthentication` 会替我们把跨平面会话挡掉，
    于是"摘掉守卫"这个实验根本证明不了守卫有用（评审指出这条测试极易写成零判别力）。
    所以本视图**刻意**不挂任何装饰器：它必须和 `request.user` 的加载一样朴素——
    `AuthenticationMiddleware` 把身份装进 `SimpleLazyObject`，谁先读谁就把它落到
    `request._cached_user`（django/contrib/auth/middleware.py:18-21,40）。
    """
    return JsonResponse(
        {
            "auth": request.user.is_authenticated,
            "pk": request.user.pk,
            "username": request.user.get_username(),
            "conn_schema": connection.schema_name,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def write_probe(request):
    """写一行（用于"拒绝时视图不得执行"的副作用断言，spec §5.2-7）。

    副作用载体是 `Group`：P5 不新增模型（Global Constraints），只能借一张既有表；
    借 `django.contrib.auth` 而不是 P2 的业务表，因为 auth 在**每个租户 schema** 都有表，
    且属于 P5 自己的领域——P2 正在把 `GameItem` 换成 `Game`/`Project`，
    借业务表会让本文件在 P2 合并后立刻失效。
    """
    Group.objects.create(name="probe-write")
    return JsonResponse({"ok": True})


@api_view(["GET"])
@permission_classes([has_role("客服")])
def service_only(request):
    """要求**某个职位**的端点（spec §6.2 的准入半边）。

    它是 `test_p5_rbac.py` 里「有职位 → 200 / 无职位 → 403」的判别物，也是
    spec §5.1-11 那半边「已认证但无权限的 403」的载体——Task 8 只能落 CSRF 那半边，
    因为当时没有要求职位的端点（`HasRole` 由 Task 9 产出）。

    职位名刻意**内联**（`"客服"`）而不 import `apps.business.roles.SERVICE`：
    本文件是测试夹具，写成字面量才能让「实现把 `SERVICE` 改掉」这件事在探针这条路上
    仍然按 spec 的取值断言（与 Task 8 独立抄写 `FAIL_MESSAGE` 是同一条理由）。
    """
    return JsonResponse({"ok": True})


@api_view(["GET"])
@permission_classes([has_permission("business.add_game")])
def perm_only(request):
    """要求**某个 codename** 的端点——`HasPermission` 双向测试的载体。

    与 `service_only` 分开两个视图，是为了让两个权限类各自有独立的判别物：
    合用一个端点会让「哪个类在起作用」变成推断。
    """
    return JsonResponse({"ok": True})


@api_view(["GET"])
@permission_classes([has_node("order.list")])
def node_only(request):
    """节点判据的**双向**测试载体（spec §10-A6）。

    判据点在 catalog 里刻意取 `available=False` 的 `order.list`：
    `available` 是"有没有消费方"的登记，**不是**判据的一部分——判据只问"持不持有这个
    code"。取一个已实现（`available=True`）的点，会让"判据读了 `available` 才是真的"
    这种实现也照样变绿，本载体就证明不了它该证明的东西。
    """
    return JsonResponse({"ok": True, "schema": connection.schema_name})
