"""客服看板的个人订单只读统计。"""
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.audit.models import OperationLog
from apps.business.game_scope import game_scope
from apps.business.models import OrderStatus
from apps.business.order_flags import can_view_total_stats, scope_by_creator
from apps.business.selectors import fold_split_members, search_orders
from apps.core.envelope import api_response
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node


class ServiceDashboardView(APIView):
    permission_classes = [IsAuthenticated, has_node("order.list")]

    def get(self, request):
        if not can_view_total_stats(request.user):
            raise PermissionDenied()
        orders = scope_queryset(request.user, search_orders(), scope_filter=game_scope("game_id"))
        orders = scope_by_creator(request.user, orders).filter(sales_user=request.user)
        today = timezone.localdate()
        counts = {status: fold_split_members(orders.filter(status=status)).count() for status in OrderStatus.values}
        followed = OperationLog.objects.filter(
            actor_id=request.user.pk, created_at__date=today,
            order_no__in=orders.order_by().values("order_no"),
        ).order_by().values("order_no").distinct().count()
        attention = fold_split_members(orders.filter(status__in=[OrderStatus.ISSUE, OrderStatus.PAUSED])).order_by("-updated_at", "-id")[:10]
        return api_response(data={
            "status_counts": counts,
            "today_created": orders.filter(created_at__date=today).count(),
            "today_completed": orders.filter(completed_at__date=today).count(),
            "today_followed": followed,
            "attention_orders": [{
                "order_no": row.order_no,
                "source_order_no": row.source_order_no,
                "status": row.status,
                "game_name": row.game.name if row.game else "",
                "project_text": row.project_text,
                "updated_at": timezone.localtime(row.updated_at).isoformat(),
            } for row in attention],
        })
