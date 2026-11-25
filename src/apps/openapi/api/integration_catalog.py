from rest_framework import serializers

from apps.business.models import GameServer, OrderSource
from apps.core.envelope import api_response
from apps.openapi.api.integration_auth import IntegrationView
from apps.openapi.api.order_extra_views import _paginated


class ServerListParams(serializers.Serializer):
    game_id = serializers.IntegerField(min_value=1, max_value=9223372036854775807, required=False)


class IntegrationServerListView(IntegrationView):
    def get(self, request):
        form = ServerListParams(data=request.query_params)
        form.is_valid(raise_exception=True)
        servers = GameServer.objects.filter(game__deleted_at__isnull=True, **form.validated_data)
        return api_response(_paginated(servers, request, lambda row: {
            "id": row.pk, "game_id": row.game_id, "name": row.name,
            "is_active": row.is_active, "sort_order": row.sort_order,
        }))


class IntegrationOrderSourceListView(IntegrationView):
    def get(self, request):
        return api_response(_paginated(OrderSource.objects.all(), request, lambda row: {
            "id": row.pk, "name": row.name, "remark": row.remark, "order_by": row.order_by,
        }))
