"""订单详情中的企微群匹配读面。"""
import re
from hashlib import sha256
from time import monotonic

from django.core.cache import caches
from django.db import connection
from requests import RequestException

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.api.dailiantong_push_views import _not_found, _visible_order
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node
from apps.wecom.archive import ArchiveError, _ArchiveSession
from apps.wecom.archive_views import archive_viewer_enabled, order_archive_config
from apps.wecom.group_sync import _contacts_client, group_detail_with_names
from apps.wecom.models import WecomAppConfig, WecomGroupChat
from apps.wecom.sdk import WeChatClientException, retry_rejected_token


def _member_display_name(member):
    for key in ("group_nickname", "name"):
        value = member.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _member_brief(member):
    userid = member.get("userid")
    return {"name": _member_display_name(member), "userid": userid if isinstance(userid, str) else ""}


def _live_group_names(config, group):
    # Reuse the current application's contact access for a sole, already matched group.
    key = "wecom:roster:" + sha256(repr((
        config.tenant_schema, config.pk, config.identity_revision, config.corp_id, config.agent_id,
        config.contacts_secret or config.secret, group.chat_id, group.updated_at,
    )).encode()).hexdigest()
    cache = caches["wecom_archive_badges"]
    cached = cache.get(key)
    if cached is not None:
        return cached

    client = _contacts_client(config)
    client._http.close()
    client._http = _ArchiveSession(monotonic() + 2.5)
    client.auto_retry = False
    names = {}
    try:
        result = retry_rejected_token(client, lambda: group_detail_with_names(client, group.chat_id))
        if isinstance(result, dict) and type(result.get("errcode")) is int and result["errcode"] == 0:
            info = result.get("group_chat")
            if isinstance(info, dict) and info.get("chat_id") == group.chat_id and isinstance(info.get("member_list"), list):
                names = {
                    member["userid"]: _member_display_name(member)
                    for member in info["member_list"]
                    if isinstance(member, dict) and isinstance(member.get("userid"), str)
                }
    except (ArchiveError, RequestException, WeChatClientException, RuntimeError):
        pass
    finally:
        client._http.close()
    cache.set(key, names, 300 if any(names.values()) else 30)
    return names


class GroupListQuery(serializers.Serializer):
    after = serializers.IntegerField(required=False, default=0, min_value=0, max_value=9223372036854775807)


class OrderWecomGroupsView(APIView):
    permission_classes = [has_node("order.view_detail", "order.view_content")]

    def get(self, request, order_no):
        if not re.fullmatch(r"[0-9]{20}", order_no):
            return _not_found()
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        query = GroupListQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        archive = {}
        if order_archive_config() is not None:
            archive = {"archive_available": True, "archive_search_available": archive_viewer_enabled(request.user)}
        source = (order.source_order_no or "").strip()
        if not source:
            return api_response({"groups": [], "too_many": False, "next_after": None, **archive})
        config = WecomAppConfig.objects.first()
        if config is None:
            return api_response({"groups": [], "too_many": False, "next_after": None, **archive})
        matched = WecomGroupChat.objects.filter(identity_revision=config.identity_revision, dismissed=False, name__contains=source)
        too_many = len(list(matched.order_by("pk").values_list("pk", flat=True)[:6])) > 5
        rows = list(matched.filter(pk__gt=query.validated_data["after"]).order_by("pk")[:6])
        next_after = rows[4].pk if len(rows) > 5 else None
        rows = rows[:5]
        groups = []
        for row in rows:
            members = [
                _member_brief(member)
                for member in (row.details.get("member_list") or []) if isinstance(member, dict)
            ]
            if (
                len(rows) == 1 and not too_many and not row.pending_dismissal
                and config.group_sync_enabled and config.tenant_schema == connection.schema_name
                and config.contacts_ready() and any(member["userid"] and not member["name"] for member in members)
            ):
                names = _live_group_names(config, row)
                for member in members:
                    member["name"] = member["name"] or names.get(member["userid"], "")
            groups.append({
                "chat_id": row.chat_id, "name": row.name, "owner": row.details.get("owner", ""),
                "members": members,
                "updated_at": row.updated_at,
            })
        return api_response({"groups": groups, "too_many": too_many, "next_after": next_after, **archive})


class GroupQrRequest(serializers.Serializer):
    chat_id = serializers.CharField(max_length=256, trim_whitespace=False)


class OrderWecomGroupQrView(APIView):
    permission_classes = [has_node("order.view_detail", "order.view_content")]

    def post(self, request, order_no):
        if not re.fullmatch(r"[0-9]{20}", order_no):
            return _not_found()
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = GroupQrRequest(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = (order.source_order_no or "").strip()
        if not source:
            return _not_found()
        config = WecomAppConfig.objects.first()
        if config is None:
            return _not_found()
        def matching_groups():
            return WecomGroupChat.objects.filter(identity_revision=config.identity_revision, dismissed=False, name__contains=source)
        def too_many_groups():
            return len(list(matching_groups().values_list("pk", flat=True)[:6])) > 5
        if too_many_groups():
            return api_response({"status": "unavailable", "message": "匹配群超过5个，已暂停生成群码，请检查订单号", "qr_url": ""})
        group = matching_groups().filter(chat_id=serializer.validated_data["chat_id"]).first()
        if group is None:
            return _not_found()
        from apps.wecom.group_qr import get_or_create_qr
        def order_still_matches():
            current = _visible_order(request, order_no)
            return current is not None and (current.source_order_no or "").strip() == source and not too_many_groups()
        return api_response(get_or_create_qr(group, expected_source=source, order_still_matches=order_still_matches))
