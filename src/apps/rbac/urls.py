"""RBAC 路由。挂在 `/api/` 下（由 `apps.core.urls` include）。

与 business / accounts 同口径：关掉 API 根视图，避免出现不带 {code,msg,data} 的响应形状。
"""
from rest_framework.routers import DefaultRouter

from apps.rbac.views import PermissionNodeViewSet, RoleAuthorityViewSet

router = DefaultRouter()
router.include_root_view = False
router.register("role-authorities", RoleAuthorityViewSet, basename="role-authority")
# ⚠️ 这一行必须排在 `PermissionNodeViewSet` 的 import 之后：早注册就是 ImportError。
router.register("permission-nodes", PermissionNodeViewSet, basename="permission-node")

urlpatterns = router.urls
