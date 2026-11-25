"""只认绑定令牌的直派端点。"""
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.business import peer_direct_taker
from apps.business.models import OrderStatus, PeerUpstreamBinding
from apps.core.authentication import PeerTokenAuthentication
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode


class DirectTakerRequestSerializer(serializers.Serializer):
    studio_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    account_name = serializers.CharField(max_length=150)
    match_token = serializers.CharField(max_length=128, required=False)
    expected_price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)


class DirectTakerSearchSerializer(serializers.Serializer):
    studio_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)
    account_name = serializers.CharField(max_length=150, required=False)
    query = serializers.CharField(max_length=150, required=False)

    def validate(self, attrs):
        if ("account_name" in attrs) == ("query" in attrs):
            raise serializers.ValidationError("请提供账号名称或搜索关键词")
        return attrs


class DirectTakerDraftGameSerializer(serializers.Serializer):
    game_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1)


class DirectTakerDraftSearchSerializer(DirectTakerSearchSerializer, DirectTakerDraftGameSerializer):
    pass


class _AssignPayloadSerializer(serializers.Serializer):
    order_no = serializers.CharField(max_length=64)
    status = serializers.ChoiceField(choices=[OrderStatus.UNASSIGNED])
    game_name = serializers.CharField(max_length=64)
    hall_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    source_order_no = serializers.CharField(max_length=128, required=False, allow_blank=True)
    project_text = serializers.CharField(required=False, allow_blank=True)
    content = serializers.CharField(required=False, allow_blank=True)
    remark = serializers.CharField(required=False, allow_blank=True)
    customer_phone = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    customer_qq = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    origin_order_no = serializers.CharField(max_length=64, required=False, allow_blank=True)


class _PeerDirectTakerSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["availability", "search", "search_matches", "assignee", "assign", "receipt"])
    account_name = serializers.CharField(max_length=150, required=False)
    query = serializers.CharField(max_length=150, required=False)
    match_token = serializers.CharField(max_length=128, required=False)
    game_name = serializers.CharField(max_length=64, required=False)
    target_ref = serializers.CharField(max_length=64, required=False)
    order_no = serializers.CharField(max_length=64, required=False)
    operation_id = serializers.UUIDField(required=False)
    binding_token_hash = serializers.RegexField(r"\A[0-9a-f]{64}\Z", required=False)
    payload = _AssignPayloadSerializer(required=False)

    def validate(self, attrs):
        required = {
            "availability": (),
            "search": ("game_name", "account_name"),
            "search_matches": ("game_name", "query"),
            "assignee": ("order_no",),
            "assign": ("target_ref", "account_name", "match_token", "operation_id", "payload"),
            "receipt": ("target_ref", "account_name", "operation_id", "binding_token_hash"),
        }[attrs["action"]]
        for key in required:
            if key not in attrs:
                raise serializers.ValidationError({key: "该字段必填"})
        return attrs


class PeerDirectTakerView(APIView):
    authentication_classes = [PeerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        binding = request._peer_binding
        if not isinstance(binding, PeerUpstreamBinding):
            return api_response(msg="该令牌不是上家绑定", code=ErrorCode.PEER_REJECTED)
        serializer = _PeerDirectTakerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data
        action = params["action"]
        try:
            if action == "availability":
                result = {
                    "enabled": peer_direct_taker.receiving_studio_available(binding, params.get("game_name")),
                    "name": request.tenant.name,
                }
            elif action == "search":
                result = peer_direct_taker.search_receiving_taker(
                    binding, game_name=params["game_name"], account_name=params["account_name"],
                )
            elif action == "search_matches":
                result = peer_direct_taker.search_receiving_takers(
                    binding, game_name=params["game_name"], query=params["query"],
                )
            elif action == "assignee":
                result = peer_direct_taker.reveal_order_taker(
                    binding, order_no=params["order_no"],
                )
                if result is not None:
                    result["studio_name"] = request.tenant.name
            elif action == "receipt":
                result = peer_direct_taker.receive_assignment_receipt(
                    binding, operation_id=params["operation_id"], target_ref=params["target_ref"],
                    account_name=params["account_name"], binding_token_hash=params["binding_token_hash"],
                )
            else:
                result = peer_direct_taker.receive_assignment(
                    binding, operation_id=params["operation_id"], target_ref=params["target_ref"],
                    account_name=params["account_name"], match_token=params["match_token"], payload=params["payload"],
                )
        except peer_direct_taker.DirectTakerError as exc:
            return api_response(msg=str(exc), code=exc.code)
        return api_response(data=result)
