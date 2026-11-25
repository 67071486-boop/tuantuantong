"""站内通知的三个接口（spec §5）：`GET /api/business/notifications`、`POST /api/business/notifications/{notification_id}/read` 与（App 角标用的）`GET /api/business/notifications/unread-count/`——**所有人**（登录即可）。⚠️ 通知**不保证送达**，是提示而非凭据（spec §8.2）——这里的已读态不是审计/结算依据；**序列化器定义在本模块里**（硬边界：不往 `api/serializers.py` 里加）；📌 **谁的通知**：queryset 本身就收窄成 `user == request.user`，所以「别人的通知」在本视图里表现为**资源不存在**（不是靠一道额外的 403 判据）。
"""
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.business.api.base import EnvelopePagination
from apps.business.models import Notification
from apps.core.envelope import api_response
from apps.core.scoping import scope_queryset


def _visible_notifications(user):
    """本人可见的通知：**只列自己的** + **已软删单的通知不出现**。列表（`NotificationViewSet`）与未读计数（`NotificationUnreadCountView`）**共用**这一份，⛔ 不要再写第二份过滤条件——两处漂移会让角标数出来的行在列表里打不开、也读不掉。**只列自己的**：收窄发生在 queryset 本身，所以 `get_object()` 对别人的行抛的是 Http404（由统一异常处理器翻成 200 + 业务码 BUSINESS，见 core/exceptions.py）。
    ⭐ D-0144：已软删单的通知不出现。写 `.exclude(order__deleted_at__isnull=False)`，**不是**在说 `.filter(order__deleted_at__isnull=True)` 会错——⚠️ **两者在 Django 5.2 下等价**（`Notification.order` 虽是 `SET_NULL`，但两种写法都会把 `order` 为 NULL 的通知行**留住**，SQL 都是 `LEFT OUTER JOIN`；依据读源码 + 实测：`django/db/models/sql/query.py` 的 `build_filter` 里 `require_outer` 命中两个不同分支，两边都被提升成外连接；`.exclude` 那支 SQL 是 `NOT (business_order.deleted_at IS NOT NULL)`，NULL-order 行 LEFT JOIN 后 `deleted_at` 为 NULL ⇒ `NOT (NULL IS NOT NULL)` = TRUE ⇒ 留下来）。**选 `.exclude` 只是因为读起来更直白**，不是正确性要求。
    ⭐ **真正要守的事实**：NULL-order 的通知行必须留住——用例 `tests/test_order_soft_delete_read_paths.py::NotificationListTests.test_notifications_of_a_deleted_order_are_hidden_but_nulls_are_kept`。
    """
    qs = (
        Notification.objects.select_related("order")
        .filter(user=user)
        .exclude(order__deleted_at__isnull=False)
    )
    # 数据权限的唯一卡点（spec §4.4）：基线 pass-through，但接线必须在，
    # 且 `tests/test_notification.py` 有判别力测试钉住「接线真的被用上」。
    return scope_queryset(user, qs)


class NotificationSerializer(serializers.ModelSerializer):
    """通知的对外形状（spec §5 未逐字给字段，取 brief §4.3 的清单）。"""

    order_no = serializers.SerializerMethodField()
    # ⭐ 来源单号（D-0301）：工作台 / 我的消息 / App 通知三处「来源号优先、
    #    空回退 `order_no`」展示回退的取数前提。**实时读 `order` FK**，不走 `payload`——
    #    存量通知行的 payload 没有这个键（那是一个写入时刻的快照），FK 才是可靠来源。
    source_order_no = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "kind",
            "order_no",
            # ⭐（D-0301）追加在 `order_no` 之后，既有键的位置一字不动。
            "source_order_no",
            "payload",
            "read_at",
            "created_at",
        ]

    def get_order_no(self, obj):
        # `order` 可空（`SET_NULL`：订单被删后通知仍在）。
        return obj.order.order_no if obj.order_id else None

    def get_source_order_no(self, obj):
        # 同 `get_order_no` 的可空口径；`Order.source_order_no` 是 `blank=True` 的
        # 非 NULL 文本列 ⇒ 有单时是（可能为空的）字符串、无单时是 `None`。
        return obj.order.source_order_no if obj.order_id else None


class NotificationViewSet(viewsets.GenericViewSet):
    """站内通知（spec §5）。成功响应必须自己走 `api_response()`——`GenericViewSet`
    不像 `EnvelopeModelViewSet` 那样自动包信封。"""

    pagination_class = EnvelopePagination
    serializer_class = NotificationSerializer
    # URL 里的参数名是 `notification_id`（spec §5 的路径），不是 DRF 默认的 `pk`。
    lookup_field = "pk"
    lookup_url_kwarg = "notification_id"

    def get_queryset(self):
        # 只列自己的 + 已软删单的通知不出现；口径与未读计数共用
        # `_visible_notifications`（那里面有完整依据与踩坑说明）。
        return _visible_notifications(self.request.user)

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def read(self, request, notification_id=None):
        """标记已读。**幂等**：已经读过就不动 `read_at`，仍返回成功。"""
        notification = self.get_object()
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
        return api_response(data=self.get_serializer(notification).data)


class NotificationUnreadCountView(APIView):
    """未读通知数（打手 App 底部「消息」tab 的角标）：`GET /api/business/notifications/unread-count/`。**口径**：`read_at IS NULL` 的本人通知条数；出参 = `{"notifications": 3}`（`data` 里只有一个键，⛔ 不要再包 `items` / `total`——它不是列表）。
    **只读、认证态**：租户上下文只来自 `request.user`，⛔ 不接受任何租户参数；匿名 ⇒ DRF 先抛 `NotAuthenticated`（401，走项目统一信封）。**与列表同一份可见性**：共用 `_visible_notifications()`，所以「已软删单的通知」既不出现在列表里、也不计入角标——否则那条通知在 App 里永远看不到、也读不掉，角标会一直清不掉。读端点：不写 `OperationLog`。
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        count = _visible_notifications(request.user).filter(read_at__isnull=True).count()
        return api_response(data={"notifications": count})
