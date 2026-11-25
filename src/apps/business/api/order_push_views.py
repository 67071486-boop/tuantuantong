"""自动发消息到微信群设置（对标 `/System/OrderPushGroupMsgSet`）。端点（router，`/api/business/order-push-settings/`）：`GET`（列表，规则 + 游戏 id）、`POST`（新增）、`PATCH …/<pk>/`（编辑，含开关 `is_active`）、`DELETE …/<pk>/`（删除）、`POST …/<pk>/test/`（发一条测试消息，同步、失败当场回原因）。与对标的偏离（有意）：对标只认企微群机器人，本页把渠道做成 `wecom` / `custom` 两档（见 `order_push.py` 文件头）；对标还有一个「内部抢单群聊 ID」（`groupId`）——那是对标自家聊天系统的概念，本项目没有 ⇒ **不建字段、不画控件**。
"""

from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.models import Game, OrderPushSetting
from apps.business.order_push import PushDeliveryError, send_test_message, validate_webhook_url
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

class OrderPushSettingSerializer(serializers.ModelSerializer):
    """规则形状。`games` 用主键数组（页面只勾游戏，不需要嵌套对象）。"""

    games = serializers.PrimaryKeyRelatedField(
        queryset=Game.objects.all(),
        many=True,
        allow_empty=False,
        error_messages={"empty": "至少要选择一个游戏"},
    )

    class Meta:
        model = OrderPushSetting
        fields = [
            "id",
            "name",
            "channel",
            "webhook_url",
            "games",
            "timing",
            "msg_type",
            "call_all",
            "remark",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_webhook_url(self, value: str) -> str:
        try:
            return validate_webhook_url(value)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_name(self, value: str) -> str:
        return (value or "").strip()

    def validate_remark(self, value: str) -> str:
        return (value or "").strip()

    def validate_timing(self, value: int) -> int:
        if value not in OrderPushSetting.Timing.values:
            raise serializers.ValidationError("发送时机不在可选范围内")
        return value


class OrderPushSettingViewSet(EnvelopeModelViewSet):
    """推送规则的 CRUD + 测试发送。整个页面共用一个 RBAC 点。"""

    queryset = OrderPushSetting.objects.prefetch_related("games").all()
    serializer_class = OrderPushSettingSerializer
    pagination_class = EnvelopePagination

    #: 逐 action 的节点 code 表。字符串必须是**字面量**（`test_rbac_structure` 的 B2 判据）。
    ORDER_PUSH_PERMISSIONS = {
        "list": ("system.order_push_group_settings",),
        "retrieve": ("system.order_push_group_settings",),
        "create": ("system.order_push_group_settings",),
        "update": ("system.order_push_group_settings",),
        "partial_update": ("system.order_push_group_settings",),
        "destroy": ("system.order_push_group_settings",),
        "test_message": ("system.order_push_group_settings",),
    }

    def get_permissions(self):
        """与默认权限类取并集（形状照 `ProjectCategoryViewSet.get_permissions`）。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.ORDER_PUSH_PERMISSIONS.get(self.action)
        if not codes:
            return base
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    @action(detail=True, methods=["post"], url_path="test")
    def test_message(self, request, pk=None):
        """同步发一条测试消息。**不做异步**：用户点了按钮就该当场知道通不通。"""
        setting = self.get_object()
        try:
            send_test_message(setting)
        except (PushDeliveryError, ValueError) as exc:
            return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
        return api_response(data={"ok": True}, msg="测试消息已发送")
