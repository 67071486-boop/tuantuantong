"""操作日志路由。挂在 `/api/audit/` 下，路径一律带尾斜杠——跟其它业务集合路径、以及 DRF 路由的默认形状一致（`APPEND_SLASH` 只补斜杠、从不去掉：pattern 不带斜杠时，带斜杠的请求是 404，不是 301）。
`api/auth/*` 不带斜杠，那是另一套；不要拿它当理由改回。
"""
from django.urls import path

from apps.audit.api.timeline_views import (
    JidanpuBreakerListView,
    OrderTimelineView,
)
from apps.audit.api.views import (
    OperationLogActorListView,
    OperationLogEventView,
    OperationLogListView,
    OrderActionLogListView,
)

urlpatterns = [
    path("logs/", OperationLogListView.as_view(), name="audit-log-list"),
    # 交易订单操作记录（交易订单操作记录线）：只读列表，准入是 RBAC 节点
    # `order.platform_action_logs`（**与 `logs/` 的 `system.logs` 是两个门**，见视图 docstring）。
    path(
        "order-action-logs/",
        OrderActionLogListView.as_view(),
        name="audit-order-action-log-list",
    ),
    # 人员枚举：日志页「相关人员」下拉。对标是单选，不是多选。
    path("actors/", OperationLogActorListView.as_view(), name="audit-actor-list"),
    # 读操作上报（spec §6.2）：前端「进入编辑订单页面」时调，只收白名单动作、只取认证态。
    path("events/", OperationLogEventView.as_view(), name="audit-event-create"),
    # 订单时间线（D-0360）：按单号聚合全量留痕的只读口；准入复用
    # `order.platform_action_logs`（计划 §1.5 拍板①，零新增节点）。查无 / 跨租户 /
    # key 缺失同形同码（防探测，见 timeline_views 的模块 docstring）。
    path("order-timeline/", OrderTimelineView.as_view(), name="audit-order-timeline"),
    # 下家绑定读回熔断健康条（D-0360 §1.4）：只读口，判定零改动（薄包装现算函数）。
    path(
        "jidanpu-breakers/",
        JidanpuBreakerListView.as_view(),
        name="audit-jidanpu-breakers",
    ),
]
