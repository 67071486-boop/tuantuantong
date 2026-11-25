"""公司店铺分组的 CRUD 端点。"""

from rest_framework.permissions import IsAuthenticated

from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.api.config_views import ConfigAuditMixin
from apps.business.api.serializers import OrderSourceGroupSerializer
from apps.business.models import OrderSourceGroup
from apps.business.source_groups import group_source_count
from apps.rbac.permissions import has_node


class OrderSourceGroupViewSet(ConfigAuditMixin, EnvelopeModelViewSet):
    audit_actions = (
        "system.order_source_group_create",
        "system.order_source_group_update",
        "system.order_source_group_destroy",
    )
    #: 列宽 16（`apps/audit/models.py` 的 `target_kind`）以内；`order_source_group` = 18
    #: 会被 `apps/audit/log.py::_clip` **静默截断** ⇒ 这里用 12 字符的 `source_group`。
    audit_target_kind = "source_group"

    serializer_class = OrderSourceGroupSerializer
    pagination_class = EnvelopePagination

    #: 逐 action 的节点 code 表（**复用**来源字典的两条节点，业务方拍板④）。
    #: ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取字符串常量。
    ORDER_SOURCE_GROUP_PERMISSIONS = {
        "create": ("system.add_edit_source",),
        "update": ("system.add_edit_source",),
        "partial_update": ("system.add_edit_source",),
        "destroy": ("system.delete_source",),
    }

    def get_queryset(self):
        return (
            OrderSourceGroup.objects.annotate(source_count=group_source_count())
            .order_by(*OrderSourceGroup._meta.ordering)
        )

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。

        与 `source_views.OrderSourceViewSet.get_permissions` 逐字同形；未登记在表里的
        action（`list` / `retrieve` / …）走默认类（fail-closed：仍要登录）。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.ORDER_SOURCE_GROUP_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`
        #    （`isinstance` 会把 `IsAuthenticated` 的子类当成同一类一并丢掉）。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    def get_object(self):
        """取目标行时**顺手记下改名前的名字**，供 `_audit_columns` 的 `from_value` 用。⚠️ 为什么不在 `perform_update` 里取：
        埋点覆盖率网按「哪个类定义了 `perform_update`」解析 `record(...)` 的落点（`xgj_views.XgjShopBindingViewSet.finalize_response` 的注释记过
        这条实测）⇒ 只要本类自己定义 `perform_update`，那条网就只在本类函数体里找 `record(`，判「声称埋点却找不到」。`get_object()` 在 DRF 里早于 `perform_update`且**不承担埋点职责**，用它记旧值不碰那条判据。"""
        instance = super().get_object()
        # 只对写路径有意义；读路径记一下也无害（没有 `_audit_columns` 调用）。
        self._audit_previous_name = instance.name
        return instance

    def _audit_columns(self, instance):
        """分组改名把**旧名**放 `from_value`（计划 §3.2 / §6 ⑧）。

        句子槽位只有「分组」+「原名称」两个：新增/删除句没有 `{from_value}` ⇒ 回空；
        更新句固定带旧名（`order_by` 单独改动也算一次编辑，显示原名仍是真的）。
        """
        if self.action in ("update", "partial_update"):
            return {"from_value": getattr(self, "_audit_previous_name", "")}
        return {}