"""订单时间线与熔断健康条的两个只读端点（D-0360，2026-11-01；尾斜杠见 R18）。 `GET /api/audit/order-timeline/?key=<单号>`：按单号聚合全量留痕出时间线（聚合与查询形状的权威出处 = `apps/audit/timeline.py` 的模块 docstring 与计划 `docs/plans/2026-11-01-order-audit.md` §1.2，本文件不重复判据）。 `GET /api/audit/jidanpu-breakers/`：全部启用下家绑定的读回熔断态（只读口，判定零改动——薄包装 `jidanpu_member.readback_breaker_health`）。
**准入**：两个端点都挂 RBAC 节点 `order.platform_action_logs`（计划 §1.5 拍板① 复用既有节点；先例 `views.py::OrderActionLogListView`）——零新增节点、零迁移。 **租户边界（AGENTS.md §4 硬性）**：与同目录其余视图同一条口径——查询全部在 **当前连接**（认证态钉住的租户 schema）上跑，端点**不接受**任何 tenant / schema 参数。跨租户的防探测判据见下面 `*_NOT_FOUND_*` 两个常量的注释。
"""
from rest_framework import generics

from apps.audit.timeline import (
    build_order_timeline,
    list_member_breaker_health,
    resolve_order_by_key,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

# ⭐ §1.6（M6）：**查无 / 跨租户 / key 缺失三种结局同形同码**——同一个业务码 + 同一句 msg + 同样的 `data=None`。按响应差异探测「他租户有没有这个单号」的口子从这里焊死（AGENTS.md §4 精神：租户上下文只来自认证态）。
# 码选型（从 `apps/core/error_codes.py` **现有**码内选，零新增）： `ErrorCode.BUSINESS`（1002）——本仓「业务查无」的既有档（先例：订单详情/软删单回 `BizError(1002)`「记录不存在或已被删除」）。
NOT_FOUND_CODE = ErrorCode.BUSINESS
NOT_FOUND_MSG = "未找到匹配的订单"


class OrderTimelineView(generics.GenericAPIView):
    """`GET /api/audit/order-timeline/?key=<单号>`（不分页，靠节上限兜底，§1.6）。 `key` 必填；先经 `resolve_order_by_key` 归一成唯一订单（双号歧义只在这一步
    处理），后续所有表只用归一后的 `order_no` 与 FK。软删单**不是查无**：返回完整时间线 + `order.is_deleted=true`（审计语义，§1.6）。
    """

    permission_classes = [has_node("order.platform_action_logs")]

    def get(self, request, *args, **kwargs):
        # `?key=` 空串 / 缺席同一档（前端清空搜索框会送 `?key=`，与同目录
        # `OperationLogListView._search_params` 的「空串按没传」口径一致）。
        key = (request.query_params.get("key") or "").strip()
        if not key:
            return api_response(data=None, msg=NOT_FOUND_MSG, code=NOT_FOUND_CODE)
        order = resolve_order_by_key(key)
        if order is None:
            return api_response(data=None, msg=NOT_FOUND_MSG, code=NOT_FOUND_CODE)
        return api_response(data=build_order_timeline(order))


class JidanpuBreakerListView(generics.GenericAPIView):
    """`GET /api/audit/jidanpu-breakers/`：启用中下家绑定的读回熔断态列表。只读、零写行为；载荷由 `list_member_breaker_health` 组装（两级查询控成本，见它的 docstring）。挂起判据的口径 = 最近一条
    `kind='member.launch_breaker_open' AND status='skipped'` 在 12h 窗内（`jidanpu_member.LAUNCH_BREAKER_NOTE_WINDOW`）。
    """

    permission_classes = [has_node("order.platform_action_logs")]

    def get(self, request, *args, **kwargs):
        return api_response(data=list_member_breaker_health())
