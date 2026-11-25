from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.business.api.serializers import GameLibrarySerializer
from apps.business.models import GameLibrary
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node


class GameLibraryView(APIView):
    """平台游戏库候选清单（只读）；新增条目只改数据，不改前端。"""

    permission_classes = [IsAuthenticated]

    def get_permissions(self):
        base = [permission() for permission in self.permission_classes]
        others = [permission for permission in base if type(permission) is not IsAuthenticated]
        return [IsAuthenticated(), has_node("game.server")(), *others]

    def get(self, request):
        rows = GameLibrary.objects.filter(is_active=True)
        return api_response({"items": GameLibrarySerializer(rows, many=True).data})
