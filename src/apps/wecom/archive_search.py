"""Search a tenant's authorized WeCom archive independently of its orders."""

import re
from datetime import datetime, time, timedelta
from time import monotonic

from django.core import signing
from django.utils import timezone
from rest_framework import serializers

from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.wecom.archive import ArchiveError, search_chat_ids, search_message_ids
from apps.wecom.archive_views import (
    ArchiveMessagesQuery,
    ArchiveView,
    _archived_messages,
    _archive_failure,
    _archive_unavailable,
    _config,
    _config_is_current,
    _eligible_groups,
    _GROUP_LIMIT,
    _GROUP_LOOKUP_BUDGET,
    _inactive_groups,
    _indexed_message_hits,
    _lookup_candidates,
    _lookup_group,
    _MAX_SEARCH_WINDOW,
    _membership_userid,
    _messages_payload,
    _MESSAGE_PAGE_SIZE,
    _NAME_SEARCH_BUDGET,
    _newer_than,
    _older_messages,
    _older_than,
    _PAGE_BUDGET,
    _PROOF_MAX_AGE,
    _source_pattern,
    _viewer_claim,
    archive_viewer_enabled,
    archive_viewer_verified,
)

_SEARCH_PROOF_SALT = "wecom.archive.search-chat"
_MESSAGE_SEARCH_BUDGET = 4


class ArchiveSearchInput(serializers.Serializer):
    q = serializers.CharField(min_length=2, max_length=128)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)

    def validate(self, attrs):
        now = timezone.now()
        today = timezone.localdate(now)
        start_date, end_date = attrs.get("start_date"), attrs.get("end_date")
        if (start_date is None) != (end_date is None):
            raise serializers.ValidationError("请同时输入开始和结束日期")
        if start_date is None:
            start_date, end_date = today - (_MAX_SEARCH_WINDOW - timedelta(days=1)), today
        if end_date < start_date or end_date > today or end_date - start_date >= _MAX_SEARCH_WINDOW:
            raise serializers.ValidationError("请选择不超过 31 天且不晚于今天的搜索范围")
        zone = timezone.get_current_timezone()
        start = max(0, int(datetime.combine(start_date, time.min, tzinfo=zone).timestamp()))
        end = min(now, datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone))
        if int(end.timestamp()) <= start:
            raise serializers.ValidationError("搜索时间范围不正确")
        return {"q": attrs["q"], "start_time": start, "end_time": int(end.timestamp())}


class ArchiveSearchMessagesQuery(ArchiveMessagesQuery):
    after = serializers.IntegerField(required=False, min_value=1, max_value=9223372036854775807)
    around = serializers.CharField(required=False, max_length=256, trim_whitespace=False)

    def validate(self, attrs):
        if len({"after", "around", "before"} & attrs.keys()) > 1:
            raise serializers.ValidationError("请选择一种会话分页方式")
        return attrs


def _configured_viewer(user):
    return _config() if archive_viewer_enabled(user) else None


def _search_claim(request, config, chat_id, anchor):
    return {**_viewer_claim(request, config), "chat": chat_id, "anchor": anchor}


def _read_proof(request, config, chat_id):
    proof = request.headers.get("X-Wecom-Chat-Proof", "")
    if not proof or len(proof) > 2048:
        return None
    try:
        claim = signing.loads(proof, salt=_SEARCH_PROOF_SALT, max_age=_PROOF_MAX_AGE)
    except signing.BadSignature:
        return None
    if not isinstance(claim, dict) or not isinstance(claim.get("anchor"), str):
        return None
    return claim if claim == _search_claim(request, config, chat_id, claim["anchor"]) else None


def _invalid_group():
    return api_response(code=ErrorCode.VALIDATION, msg="会话不可查看，请重新搜索")


class ArchiveAccessView(ArchiveView):
    def get(self, request):
        return api_response({"enabled": _configured_viewer(request.user) is not None})


class ArchiveSearchView(ArchiveView):
    def post(self, request):
        config = _configured_viewer(request.user)
        if config is None:
            return _archive_unavailable()
        if not archive_viewer_verified(request, config):
            return api_response({"enabled": True, "groups": [], "complete": False, "requires_login": True})
        criteria = ArchiveSearchInput(data=request.data)
        criteria.is_valid(raise_exception=True)
        groups, complete = _search_groups(request, config, **criteria.validated_data)
        if not _config_is_current(config):
            return _archive_unavailable()
        return api_response({"enabled": True, "groups": groups, "complete": complete})


def _search_groups(request, config, *, q, start_time, end_time):
    userid = _membership_userid(request.user)
    pattern = _source_pattern(q)
    eligible = _eligible_groups(config, userid)
    local = list(eligible.filter(name__iregex=pattern).order_by(
        "name", "pk",
    ).values_list("chat_id", flat=True)[:_GROUP_LIMIT + 1])
    deadline = monotonic() + _PAGE_BUDGET
    try:
        named, names_complete = search_chat_ids(
            config, q, deadline=min(deadline, monotonic() + _NAME_SEARCH_BUDGET),
        )
    except ArchiveError:
        named, names_complete = [], False
    try:
        message_ids, messages_complete = search_message_ids(
            config, q, start_time=start_time, end_time=end_time,
            deadline=min(deadline, monotonic() + _MESSAGE_SEARCH_BUDGET),
        )
    except ArchiveError:
        message_ids, messages_complete = [], False
    indexed = _indexed_message_hits(config, message_ids)
    anchors = {}
    for msg_id in message_ids:
        if msg_id in indexed:
            anchors.setdefault(indexed[msg_id], msg_id)

    name_candidates = set(local) | set(named)
    chat_ids = list(dict.fromkeys([*local, *anchors, *named]))
    inactive = set(_inactive_groups(config).filter(chat_id__in=chat_ids).values_list("chat_id", flat=True))
    active = [chat_id for chat_id in chat_ids if chat_id not in inactive]
    if userid is not None:
        known_memberships = set(eligible.filter(chat_id__in=active).values_list("chat_id", flat=True))
        active.sort(key=lambda chat_id: chat_id not in known_memberships)
    candidates, checked = _lookup_candidates(
        config, active, userid, deadline=deadline, limit=_GROUP_LIMIT,
    )
    groups = []
    for row in candidates:
        anchor = anchors.get(row.chat_id, "")
        if not anchor and (row.chat_id not in name_candidates or not re.search(pattern, row.name, re.IGNORECASE)):
            continue
        groups.append({
            "chat_id": row.chat_id,
            "name": row.name,
            "match": "message" if anchor else "name",
            "proof": signing.dumps(_search_claim(request, config, row.chat_id, anchor), salt=_SEARCH_PROOF_SALT),
            **({"anchor_msgid": anchor} if anchor else {}),
        })
    # 企微按企业搜索，普通查看者的状态不能反映群外是否有命中。
    complete = (
        userid is None and names_complete and messages_complete and checked
        and len(local) <= _GROUP_LIMIT and len(indexed) == len(set(message_ids))
    )
    return groups, complete


class ArchiveSearchMessagesView(ArchiveView):
    def get(self, request):
        config = _configured_viewer(request.user)
        if config is None:
            return _archive_unavailable()
        if not archive_viewer_verified(request, config):
            return api_response({"messages": [], "next_before": None, "requires_login": True})
        query = ArchiveSearchMessagesQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        params = query.validated_data
        chat_id = params["chat_id"]
        proof = _read_proof(request, config, chat_id)
        if proof is None or _inactive_groups(config).filter(chat_id=chat_id).exists():
            return _invalid_group()
        try:
            group = _lookup_group(
                config, chat_id, _membership_userid(request.user), deadline=monotonic() + _GROUP_LOOKUP_BUDGET,
            )
        except ArchiveError:
            return _archive_failure("企微客户群权限暂时无法核对，请稍后重试")
        if group is None or not _config_is_current(config):
            return _invalid_group()
        messages = _archived_messages(config, chat_id)
        around, after = params.get("around"), params.get("after")
        if around is not None:
            if around != proof["anchor"]:
                return _invalid_group()
            anchor = messages.filter(msg_id=around).only("sent_at").first()
            if anchor is None:
                return _invalid_group()
            return _messages_around(request, config, messages, anchor)
        if after is not None:
            boundary = messages.filter(pk=after).only("sent_at").first()
            if boundary is None:
                return _invalid_group()
            return _messages_after(request, config, messages, boundary)
        return _older_messages(request, config, messages, params.get("before"))


def _messages_after(request, config, messages, boundary):
    rows = list(messages.filter(_newer_than(boundary)).order_by("sent_at", "pk")[:_MESSAGE_PAGE_SIZE + 1])
    page = list(reversed(rows[:_MESSAGE_PAGE_SIZE]))
    newer = page[0].pk if len(rows) > _MESSAGE_PAGE_SIZE else None
    return _messages_payload(request, config, page, next_before=None, next_after=newer)


def _messages_around(request, config, messages, anchor):
    neighbors = _MESSAGE_PAGE_SIZE // 2
    after = list(messages.filter(_newer_than(anchor)).order_by("sent_at", "pk")[:neighbors + 1])
    newer = after[neighbors - 1].pk if len(after) > neighbors else None
    top = list(reversed(after[:neighbors]))
    remaining = _MESSAGE_PAGE_SIZE - len(top)
    before = list(messages.exclude(_newer_than(anchor)).order_by("-sent_at", "-pk")[:remaining + 1])
    page = [*top, *before[:remaining]]
    older = page[-1].pk if len(before) > remaining else None
    return _messages_payload(request, config, page, next_before=older, next_after=newer)
