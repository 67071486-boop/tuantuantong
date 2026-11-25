import re
from types import SimpleNamespace

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business import jidanpu_upstream, peer_hall
from apps.business.api.serializers import HallListSerializer
from apps.business.dailiantong_push import claim_blocked_by_platform_denial
from apps.business.game_scope import game_scope
from apps.business.order_flags import can_view_offer_price
from apps.business.roles import WORKER
from apps.business.selectors import EFFECTIVE_TAKER_PRICE, search_hall
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node_now

TARGET_SOURCES = ("local", "upstream", "peer")
_HALL_NODE = "grab_hall.take_order"


def valid_hall_claim_target(source, ref):
    if not isinstance(ref, str):
        return False
    if source == "local":
        return re.fullmatch(r"[0-9]{20}", ref) is not None
    return source in ("upstream", "peer") and re.fullmatch(r"[1-9][0-9]{0,14}", ref) is not None


def _hall_target_candidates(source, ref):
    if source == "local":
        candidates = search_hall().filter(order_no=ref)
    elif source == "upstream":
        candidates = jidanpu_upstream.upstream_hall_queryset().filter(pk=int(ref))
    else:
        candidates = peer_hall.peer_hall_queryset().filter(pk=int(ref))
    return candidates.filter(game__deleted_at__isnull=True, game__is_active=True)


def _local_target_can_be_claimed(order):
    return (
        getattr(order, EFFECTIVE_TAKER_PRICE) is not None
        and order.hall_price is not None
        and claim_blocked_by_platform_denial(order) is None
    )


def hall_claim_target_is_available(source, ref, game_id):
    """Recheck order availability just before reserving the delivery."""
    if on_public_plane() or not valid_hall_claim_target(source, ref):
        return False
    candidates = _hall_target_candidates(source, ref).filter(game_id=game_id)
    if source == "upstream":
        # upstream_mirror_hall_price always supplies a displayed price, even when recFee is missing.
        return candidates.exists()
    order = candidates.first()
    if order is None:
        return False
    if source == "local":
        return _local_target_can_be_claimed(order)
    return peer_hall.display_price(order) is not None


def hall_notice_card(source, ref, game_id):
    """Read the shared card fields used by subscriptions in the current tenant."""
    if on_public_plane() or not valid_hall_claim_target(source, ref):
        return None
    order = _hall_target_candidates(source, ref).filter(game_id=game_id).first()
    if order is None:
        return None
    if source == "local":
        if not _local_target_can_be_claimed(order):
            return None
        card = {
            "order_no": order.order_no, "source_order_no": order.source_order_no,
            "hall_price": getattr(order, EFFECTIVE_TAKER_PRICE), "project_text": order.project_text,
        }
        server_name = order.server.name if order.server_id is not None else ""
    else:
        card = (
            jidanpu_upstream.serialize_upstream_row(order) if source == "upstream"
            else peer_hall.serialize_peer_row(order)
        )
        if card["hall_price"] is None:
            return None
        server_name = " ".join(filter(None, (card["remote_game_area"], card["remote_game_server"])))
    return {**card, "game_name": order.game.name, "server_name": server_name}


def find_hall_claim_target(user, source, ref):
    """Read the referenced hall card under the worker's current game permissions."""
    if (on_public_plane()
            or not valid_hall_claim_target(source, ref)
            or not user.is_active
            or not user.groups.filter(name=WORKER).exists()
            or not has_node_now(user, _HALL_NODE)):
        return None

    candidates = _hall_target_candidates(source, ref)
    order = scope_queryset(user, candidates, scope_filter=game_scope("game_id")).first()
    if order is None:
        return None
    if source == "local":
        if not _local_target_can_be_claimed(order):
            return None
        row = HallListSerializer(order, context={"request": SimpleNamespace(user=user)}).data
        server_name = order.server.name if order.server_id is not None else ""
    else:
        if source == "upstream":
            row = jidanpu_upstream.serialize_upstream_row(order)
        else:
            row = peer_hall.serialize_peer_row(order)
        if row["hall_price"] is None:
            return None
        if not can_view_offer_price(user):
            row["hall_price"] = None
        server_name = " ".join(filter(None, (row["remote_game_area"], row["remote_game_server"])))
    return {**row, "game_name": order.game.name, "server_name": server_name}


class HallClaimTargetQuery(serializers.Serializer):
    source = serializers.ChoiceField(choices=TARGET_SOURCES)
    ref = serializers.CharField(max_length=20, trim_whitespace=False)

    def validate(self, attrs):
        if not valid_hall_claim_target(attrs["source"], attrs["ref"]):
            raise serializers.ValidationError({"ref": "订单链接无效。"})
        return attrs


class HallClaimTargetView(APIView):
    def get(self, request):
        query = HallClaimTargetQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        row = find_hall_claim_target(request.user, **query.validated_data)
        if row is None:
            response = api_response(msg="订单已被接走或不在抢单大厅", code=ErrorCode.ORDER_ALREADY_TAKEN)
        else:
            response = api_response(row)
        response["Cache-Control"] = "private, no-store"
        return response
