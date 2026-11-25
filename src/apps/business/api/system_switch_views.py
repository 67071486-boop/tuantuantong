"""系统设置开关的两个端点（D-0223）。
- `GET   /api/business/system-switches/`（RBAC 节点 91 `system.settings`）- `PATCH /api/business/system-switches/<key>/`（自有节点 `system.edit_settings`）⚠️ **照 `recogn
ition_views.py` 的形状**（两个显式 `APIView`，⛔ 不是 ViewSet）：这一页只有"看列表 / 改一行"，没有新增与删除 ⇒ 两个显式 `path()` 比挂一个只实现一半动作的 ViewSet 更诚实，也少两条 405 路由。⚠️ **租户边界**（`AGENTS.md` §4）：查询只在当前连接 / 租户 schema 上跑，**不接受任何 tenant 参数**；`key` 只来自 URL。⚠️ **写留痕与业务写入同事务**，且**纯 no-op 不写日志行**（值没变不算一次修改）；块内**不 `return`** 退出补偿路径（E-0022）。"""
from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import SystemSwitch
from apps.business.system_switches import SWITCHES, switch_rows
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node


class SystemSwitchListView(APIView):
    """开关列表（注册表默认 ∪ 覆盖行，顺序 = 注册表顺序）。"""

    permission_classes = [has_node("system.settings")]

    def get(self, request):
        return api_response(data={"items": switch_rows()})


class SystemSwitchDetailView(APIView):
    """改一个开关的值（只收 `"Y"` / `"N"`）。"""

    permission_classes = [has_node("system.edit_settings")]

    def patch(self, request, key):
        # 请求体**必须是 JSON 对象**（与 recognition 同一条理由：DRF 的 `JSONParser`
        # 只 `json.load` 顶层、不校验类型 ⇒ 数组 / 字符串 / 数字会原样成为 `request.data`
        # ⇒ `.get(...)` 抛 `AttributeError` ⇒ HTTP 500，违反「非法输入一律 1000」E-0010）。
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        switch = SWITCHES.get(key)
        if switch is None:
            # 未知开关 ⇒ 业务码 1000（不是 404、更不是 500）：请求形状合法但 key 不在注册表。
            raise ValidationError("没有这个开关")
        value = request.data.get("value")
        if value not in ("Y", "N"):
            raise ValidationError("开关值必须是 Y 或 N")
        with transaction.atomic():
            row = SystemSwitch.objects.filter(key=key).first()
            previous = row.value if row is not None else switch.default
            if previous != value:
                # ⚠️ `updated_at` 是 `auto_now` ⇒ 必须走 `save()`（`update_or_create` 会调）。
                SystemSwitch.objects.update_or_create(key=key, defaults={"value": value})
                record(
                    log_type=ACTION_LOG_TYPES["system.system_switch_update"],
                    action="system.system_switch_update",
                    actor=request.user.get_username(),
                    actor_id=request.user.pk,
                    target_kind="system_switch",
                    target_id=key,
                    target_name=switch.name,
                    from_value=previous,
                    to_value=value,
                )
        return api_response(
            data={"key": key, "name": switch.name, "value": value}
        )