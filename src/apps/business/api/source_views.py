"""订单来源字典的 CRUD 端点。

形状照 `config_views.py::GameViewSet`（链顶集合、没有父级过滤、不声明 `permission_classes`）。
写动作挂 `ConfigAuditMixin`：文案照抄对标（logType=40），句式在 `SYSTEM_ORDER_SOURCE_*`。
"""
from django.db import transaction
from rest_framework.permissions import IsAuthenticated

from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.api.config_views import ConfigAuditMixin
from apps.business.api.serializers import OrderSourceSerializer
from apps.business.models import OrderSource
from apps.business.source_groups import groups_for_source
from apps.rbac.permissions import has_node


class OrderSourceViewSet(ConfigAuditMixin, EnvelopeModelViewSet):
    """订单来源字典，列表同时供录单下拉使用。"""

    audit_actions = (
        "system.order_source_create",
        "system.order_source_update",
        "system.order_source_destroy",
    )
    #: spec §4.2 的 `target_kind` 词表 + M3b 新增的 `order_source`（见 spec §4.2 的 M3b 补记）。
    #: 16 字符以内（`order_source` = 12），否则 `record()` 会按列宽静默截断。
    audit_target_kind = "order_source"

    queryset = OrderSource.objects.select_related("group").prefetch_related("groups")
    serializer_class = OrderSourceSerializer
    pagination_class = EnvelopePagination

    #: 逐 action 的节点 code 表，形状照 `order_views.py::OrderViewSet.ORDER_PERMISSIONS`。⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的字符串常量，变量拼出来的 code 不会被它看见（该判据回答「`available=True` 的点有没有消费方」）。
    #: ⚠️ **未登记在表里的 action（`list` / `retrieve` / …）一律走默认权限类**（fail-closed：仍要登录），**不是**默认放行——行为逐字不变。
    ORDER_SOURCE_PERMISSIONS = {
        "create": ("system.add_edit_source",),
        "update": ("system.add_edit_source",),
        "partial_update": ("system.add_edit_source",),
        "destroy": ("system.delete_source",),
    }

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in ("update", "partial_update"):
            return queryset.select_for_update(of=("self",))
        return queryset

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    @staticmethod
    def _current(instance, field):
        if field == "groups":
            return groups_for_source(instance)
        return ConfigAuditMixin._current(instance, field)

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。⚠️ **为什么是并集**：
        默认类表达的是"这一层的最低门槛"（今天 =`IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`
        ），节点判据表达的是"这个动作还要哪个 code"。两者是**合取**，不是二选一——替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），**不是**"默认放行"。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.ORDER_SOURCE_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`：
        #    `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    def _audit_columns(self, instance):
        """对标句式里的 `排序` → `to_value`、`备注` → `reason`（spec §5.9）。
        ⚠️ `OrderSource` 的三个被渲染字段（`name` / `order_by` / `remark`）**都有真实列**，没有一个被丢出句子：
        `name` 走 `target_name`（R11），另外两个在这里。⚠️ `from_value` **刻意留空**：对标原句只有一个 `排序` 槽位（编辑后那一刻的值），没有「变更前」的位置；本线不发明一个对标没有的前后值对（"照抄"就是照抄）。"""
        return {"to_value": str(instance.order_by), "reason": instance.remark}