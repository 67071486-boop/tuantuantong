"""Tenant-scoped access to WeCom archive metadata and display keys."""

import re
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic

from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.db.models import Exists, F, Func, Q, Value
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.crypto import salted_hmac
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.dailiantong_push_views import _not_found, _visible_order
from apps.business.models import Order
from apps.business.xgj_push import _TRIM_CHARS
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.permissions import is_tenant_admin
from apps.rbac.permissions import has_node
from apps.wecom.archive import (
    ArchiveError,
    _ArchiveSession,
    archive_program,
    decrypt_message_key,
    retry_rejected_token,
    search_chat_ids,
    search_message_ids,
)
from apps.wecom.archive_contacts import external_sender_badges
from apps.wecom.archive_order_session import order_archive_viewer_userid
from apps.wecom.group_sync import _contacts_client
from apps.wecom.models import WecomAppConfig, WecomArchiveMessage, WecomArchiveOrderGroup, WecomGroupChat


ARCHIVE_VIEWER_SESSION_KEY = "wecom_archive_viewer"
_PROOF_SALT = "wecom.archive.order-chat"
_PROOF_MAX_AGE = 15 * 60
# Cover the native login challenge and the subsequent hour-long view session.
_ORDER_READ_PROOF_MAX_AGE = 65 * 60
_SEARCH_WINDOW = timedelta(days=3)
_MAX_SEARCH_WINDOW = timedelta(days=31)
_NAME_SEARCH_BUDGET = 3
_NEAR_SEARCH_BUDGET = 3
_GROUP_LIMIT = 10
_REMOTE_GROUP_LIMIT = 3
_PAGE_BUDGET = 11
_GROUP_LOOKUP_BUDGET = 3
_MESSAGE_PAGE_SIZE = 30
_SOURCE_ORDER_NUMBER = re.compile(r"(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{6,128}")


def archive_viewer_userid(user):
    if not user.is_authenticated:
        return ""
    viewers = settings.WECOM_ARCHIVE_VIEWERS
    userid = viewers.get(str(user.pk)) if isinstance(viewers, dict) else None
    return userid if isinstance(userid, str) else ""


def archive_viewer_enabled(user):
    return bool(archive_program() and archive_viewer_userid(user))


def archive_viewer_verified(request, config, *, allow_legacy=False):
    identity = request.session.get(ARCHIVE_VIEWER_SESSION_KEY)
    userid = archive_viewer_userid(request.user)
    if not isinstance(identity, dict) or not userid:
        return False
    expires_at = identity.get("expires_at")
    context_matches = (
        identity.get("tenant_schema") == connection.schema_name
        and identity.get("agent_id") == config.agent_id
    )
    legacy = (
        allow_legacy and connection.schema_name == "kexing"
        and "tenant_schema" not in identity and "agent_id" not in identity
    )
    return (
        (context_matches or legacy)
        and identity.get("user_id") == str(request.user.pk)
        and identity.get("wecom_userid") == userid
        and identity.get("corp_id") == config.corp_id
        and identity.get("identity_revision") == config.identity_revision
        and type(expires_at) in (int, float)
        and expires_at >= timezone.now().timestamp()
    )


def archive_order(request, order_no):
    if not re.fullmatch(r"[0-9]{20}", order_no):
        return None
    return _visible_order(request, order_no)


def _config():
    config = WecomAppConfig.objects.filter(tenant_schema=connection.schema_name, group_sync_enabled=True).first()
    return config if config and config.is_ready() else None


def order_archive_config():
    return _config() if archive_program() else None


def _membership_userid(user):
    # Callers require a configured viewer; only tenant admins may omit membership.
    return None if is_tenant_admin(user) else archive_viewer_userid(user)


def _order_archive_userid(request, config):
    userid = order_archive_viewer_userid(request, config)
    if not userid and archive_viewer_verified(request, config, allow_legacy=True):
        return archive_viewer_userid(request.user)
    return userid


def _eligible_groups(config, membership_userid):
    groups = WecomGroupChat.objects.filter(
        identity_revision=config.identity_revision, dismissed=False, pending_dismissal=False, pending=False,
    ).exclude(name="")
    if membership_userid is not None:
        groups = groups.filter(
            Q(details__owner=membership_userid)
            | Q(details__member_list__contains=[{"type": 1, "userid": membership_userid}]),
        )
    return groups


@dataclass(frozen=True)
class _GroupCandidate:
    chat_id: str
    name: str
    message_id: str = ""


def _lookup_group(config, chat_id, userid, *, deadline):
    client = None
    try:
        client = _contacts_client(config)
        client._http.close()
        client._http = _ArchiveSession(min(deadline, monotonic() + _GROUP_LOOKUP_BUDGET))
        client.auto_retry = False
        response = retry_rejected_token(client, lambda: client.external_contact.get_group_chat_info(chat_id))
    except Exception:
        raise ArchiveError("企微客户群详情查询失败") from None
    finally:
        if client is not None:
            client._http.close()
    if not isinstance(response, dict) or type(response.get("errcode")) is not int or response["errcode"] != 0:
        raise ArchiveError("企微客户群详情查询失败")
    details = response.get("group_chat")
    if not isinstance(details, dict) or details.get("chat_id") != chat_id:
        raise ArchiveError("企微客户群详情格式异常")
    members = details.get("member_list")
    is_member = isinstance(members, list) and any(
        isinstance(member, dict) and type(member.get("type")) is int
        and member["type"] == 1 and member.get("userid") == userid
        for member in members
    )
    if userid is not None and details.get("owner") != userid and not is_member:
        return None
    name = details.get("name")
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ArchiveError("企微客户群名称不可用")
    return _GroupCandidate(chat_id, name)


def _source_pattern(source):
    pattern = re.escape(source)
    if source.isascii() and source.isdigit():
        return rf"(?<![0-9]){pattern}(?![0-9])"
    return pattern


def _order_number_pattern(number):
    token_chars = "A-Za-z0-9" if number.isdigit() else "A-Za-z0-9_-"
    return rf"(?<![{token_chars}]){re.escape(number)}(?![{token_chars}])"


def _closed_groups(config):
    return WecomGroupChat.objects.filter(identity_revision=config.identity_revision).filter(
        Q(dismissed=True) | Q(pending_dismissal=True),
    )


def _inactive_groups(config):
    return WecomGroupChat.objects.filter(identity_revision=config.identity_revision).filter(
        Q(dismissed=True) | Q(pending_dismissal=True) | Q(pending=True),
    )


def _archive_claim(request, config):
    return {
        "schema": connection.schema_name,
        "corp": config.corp_id,
        "agent": config.agent_id,
        "program": archive_program(),
        "revision": config.identity_revision,
        "user": str(request.user.pk),
    }


def _viewer_claim(request, config):
    return {**_archive_claim(request, config), "viewer": archive_viewer_userid(request.user)}


def _proof_claim(request, config, order, chat_id, match, message_id, number, remember):
    return {
        **_archive_claim(request, config),
        "session": salted_hmac(_PROOF_SALT, request.session.session_key or "").hexdigest(),
        "order": order.order_no,
        "order_id": order.pk,
        "number": number,
        "chat": chat_id,
        "match": match,
        "message": message_id,
        "remember": remember,
    }


def _checked_proof(request, config, order, chat_id, proof, number, *, max_age):
    if not proof or len(proof) > 2048:
        return {}
    try:
        claim = signing.loads(proof, salt=_PROOF_SALT, max_age=max_age)
    except signing.BadSignature:
        return {}
    if not isinstance(claim, dict):
        return {}
    match = claim.get("match")
    message_id = claim.get("message")
    if match not in ("name", "message") or not isinstance(message_id, str):
        return {}
    if (match == "message") != bool(message_id):
        return {}
    remember = claim.get("remember")
    if type(remember) is not bool:
        return {}
    expected = _proof_claim(request, config, order, chat_id, match, message_id, number, remember)
    return claim if claim == expected else {}


def _group_payload(request, config, order, rows, *, match, complete, number=""):
    remember = bool(number and complete and len(rows) == 1)
    return api_response({
        "enabled": True,
        "complete": complete,
        "groups": [
            {
                "chat_id": row.chat_id,
                "name": row.name,
                "match": match,
                "proof": signing.dumps(
                    _proof_claim(request, config, order, row.chat_id, match, row.message_id, number, remember),
                    salt=_PROOF_SALT,
                ) if number else "",
                **({"remember": True} if remember else {}),
            }
            for row in rows
        ],
    })


def _archive_failure(message):
    return api_response(code=ErrorCode.WECOM_REMOTE_FAILED, msg=message)


def _archive_unavailable():
    return api_response(code=ErrorCode.WECOM_NOT_CONFIGURED, msg="企微会话查看未授权")


class ArchiveView(APIView):
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response


class ArchiveOrderView(ArchiveView):
    permission_classes = [has_node("order.view_detail", "order.view_content")]


def _lookup_candidates(config, chat_ids, userid, *, deadline, limit=_REMOTE_GROUP_LIMIT):
    candidates = []
    complete = len(chat_ids) <= limit
    for chat_id in chat_ids[:limit]:
        if monotonic() >= deadline:
            return candidates, False
        try:
            candidate = _lookup_group(config, chat_id, userid, deadline=deadline)
        except ArchiveError:
            complete = False
            continue
        if candidate is not None:
            candidates.append(candidate)
    return candidates, complete


def _indexed_message_hits(config, message_ids):
    return dict(
        WecomArchiveMessage.objects.filter(
            identity_revision=config.identity_revision, msg_id__in=message_ids,
        ).values_list("msg_id", "chat_id")
    )


def _archive_group_labels(config, chat_ids):
    return dict(WecomGroupChat.objects.filter(
        identity_revision=config.identity_revision, chat_id__in=chat_ids,
    ).values_list("chat_id", "name"))


def _order_group_links(config, order):
    matching_number = Q(matched_order_no=order.order_no)
    source = (order.source_order_no or "").strip()
    if source and source != order.order_no:
        # Previously linked custom source numbers remain valid while unambiguous.
        matching_number |= Q(matched_order_no=source) & ~Exists(_duplicate_legacy_number(order, source))
    return WecomArchiveOrderGroup.objects.filter(
        matching_number,
        order_id=order.pk, config_id=config.pk, identity_revision=config.identity_revision,
        program_id=archive_program(),
    )


def _linked_candidates(config, order):
    chat_ids = list(
        _order_group_links(config, order).exclude(chat_id__in=_closed_groups(config).values("chat_id"))
        .order_by("chat_id").values_list("chat_id", flat=True).distinct()[:_GROUP_LIMIT + 1]
    )
    labels = _archive_group_labels(config, chat_ids[:_GROUP_LIMIT])
    rows = [_GroupCandidate(chat_id, labels.get(chat_id) or order.order_no) for chat_id in chat_ids[:_GROUP_LIMIT]]
    return rows, len(chat_ids) <= _GROUP_LIMIT


def _message_candidates(config, source, message_ids):
    indexed = _indexed_message_hits(config, message_ids)
    mentions = {}
    for message_id in message_ids:
        if message_id in indexed:
            mentions.setdefault(indexed[message_id], message_id)
    closed = set(_closed_groups(config).filter(chat_id__in=mentions).values_list("chat_id", flat=True))
    active = [chat_id for chat_id in mentions if chat_id not in closed]
    complete = len(indexed) == len(set(message_ids))
    labels = _archive_group_labels(config, active[:_GROUP_LIMIT])
    rows = [_GroupCandidate(chat_id, labels.get(chat_id) or source, mentions[chat_id]) for chat_id in active[:_GROUP_LIMIT]]
    return rows, complete and len(active) <= _GROUP_LIMIT


def _message_windows(created_at, now):
    start = created_at - _SEARCH_WINDOW
    near_end = min(now, created_at + _SEARCH_WINDOW)
    if int(near_end.timestamp()) <= int(start.timestamp()):
        return []
    wide_end = min(now, start + _MAX_SEARCH_WINDOW)
    wide_start = wide_end - _MAX_SEARCH_WINDOW
    return [
        (int(start.timestamp()), int(near_end.timestamp())),
        (int(wide_start.timestamp()), int(wide_end.timestamp())),
    ]


def _duplicate_legacy_number(order, source):
    trimmed = Func(F("source_order_no"), Value(_TRIM_CHARS), function="BTRIM")
    return Order.objects.exclude(pk=order.pk).alias(_archive_source=trimmed).filter(
        Q(order_no=source) | Q(_archive_source=source),
    )


def _eligible_legacy_number(order):
    source = (order.source_order_no or "").strip()
    if source == order.order_no or not _SOURCE_ORDER_NUMBER.fullmatch(source):
        return ""
    return "" if _duplicate_legacy_number(order, source).exists() else source


def _search_order_names(config, number, *, deadline):
    pattern = _order_number_pattern(number)
    cached = list(_eligible_groups(config, None).filter(
        name__contains=number, name__regex=pattern,
    ).order_by("name", "pk").values_list("chat_id", flat=True)[:_GROUP_LIMIT + 1])
    try:
        remote, complete = search_chat_ids(config, number, deadline=deadline)
    except ArchiveError:
        remote, complete = [], False
    candidates = list(dict.fromkeys([*remote, *cached]))
    inactive = set(_inactive_groups(config).filter(chat_id__in=candidates).values_list("chat_id", flat=True))
    chat_ids = [chat_id for chat_id in candidates if chat_id not in inactive]
    rows, checked = _lookup_candidates(config, chat_ids, None, deadline=deadline)
    return [row for row in rows if re.search(pattern, row.name)], complete and checked


def _search_message_windows(config, order, number, *, deadline):
    windows = _message_windows(order.created_at, timezone.now())
    hits = {}
    complete = False
    message_searched = False
    for index, (start_time, end_time) in enumerate(windows):
        now = monotonic()
        if now >= deadline:
            break
        stage_deadline = min(deadline, now + _NEAR_SEARCH_BUDGET) if index == 0 else deadline
        try:
            message_ids, searched = search_message_ids(
                config, number, start_time=start_time, end_time=end_time, deadline=stage_deadline,
            )
        except ArchiveError:
            continue
        message_searched = True
        hits.update(dict.fromkeys(message_ids))
        complete = searched and index == len(windows) - 1
    if windows and not message_searched:
        raise ArchiveError("企微会话搜索暂不可用，请稍后重试")
    return list(hits), complete or not windows


def _order_match_number(request, config, order, chat_id, *, remember=False):
    proof = request.headers.get("X-Wecom-Chat-Proof", "")
    max_age = _PROOF_MAX_AGE if remember else _ORDER_READ_PROOF_MAX_AGE
    number = order.order_no
    claim = _checked_proof(request, config, order, chat_id, proof, number, max_age=max_age)
    if not claim:
        number = _eligible_legacy_number(order)
        claim = _checked_proof(request, config, order, chat_id, proof, number, max_age=max_age) if number else {}
    if not claim or (remember and not claim["remember"]) or _closed_groups(config).filter(chat_id=chat_id).exists():
        return ""
    if claim["match"] == "message":
        return number if _archived_messages(config, chat_id).filter(msg_id=claim["message"]).exists() else ""
    deadline = monotonic() + _PAGE_BUDGET
    if not _inactive_groups(config).filter(chat_id=chat_id).exists():
        try:
            group = _lookup_group(config, chat_id, None, deadline=deadline)
        except ArchiveError:
            if remember:
                raise
        else:
            if group and re.search(_order_number_pattern(number), group.name):
                return number
    if remember:
        return ""
    # A stale title alone does not open the chat. A synced hit for this chat can.
    message_ids, _ = _search_message_windows(config, order, number, deadline=deadline)
    return number if _archived_messages(config, chat_id).filter(msg_id__in=message_ids).exists() else ""


def _remember_order_group(config, order, number, chat_id):
    with transaction.atomic():
        # Keep the same lock order as the group synchronization worker.
        if not _current_archive_config(config).select_for_update().exists():
            return None
        if not Order.objects.select_for_update().filter(pk=order.pk, source_order_no=order.source_order_no).exists():
            return None
        if number != order.order_no and _eligible_legacy_number(order) != number:
            return None
        if _closed_groups(config).filter(chat_id=chat_id).exists():
            return None
        return WecomArchiveOrderGroup.objects.get_or_create(
            order_id=order.pk, config_id=config.pk, identity_revision=config.identity_revision, program_id=archive_program(),
            matched_order_no=number, chat_id=chat_id,
        )


class ArchiveOrderGroupInput(serializers.Serializer):
    chat_id = serializers.CharField(max_length=256, trim_whitespace=False)


class OrderArchiveGroupsView(ArchiveOrderView):
    def get(self, request, order_no):
        order = archive_order(request, order_no)
        if order is None:
            return _not_found()
        config = order_archive_config()
        if config is None:
            return api_response({"enabled": False, "groups": [], "complete": True})

        linked, linked_complete = _linked_candidates(config, order)
        if linked:
            return _group_payload(request, config, order, linked, match="linked", complete=linked_complete)

        source = _eligible_legacy_number(order)
        numbers = (order.order_no, source) if source else (order.order_no,)
        deadline = monotonic() + _PAGE_BUDGET
        complete = True
        for number in numbers:
            rows, checked = _search_order_names(config, number, deadline=min(deadline, monotonic() + _NAME_SEARCH_BUDGET))
            complete = complete and checked
            if rows:
                return _group_payload(request, config, order, rows, match="name", complete=complete, number=number)

        for number in reversed(numbers):
            message_deadline = min(deadline, monotonic() + _NEAR_SEARCH_BUDGET) if number == source else deadline
            try:
                message_ids, searched = _search_message_windows(
                    config, order, number, deadline=message_deadline,
                )
            except ArchiveError:
                if number == order.order_no:
                    return _archive_failure("企微会话搜索暂不可用，请稍后重试")
                complete = False
                continue
            rows, indexed = _message_candidates(config, number, message_ids)
            complete = complete and searched and indexed
            if rows:
                return _group_payload(request, config, order, rows, match="message", complete=complete, number=number)
        return _group_payload(request, config, order, [], match="message", complete=complete)

    def post(self, request, order_no):
        order = archive_order(request, order_no)
        if order is None:
            return _not_found()
        config = order_archive_config()
        if config is None:
            return _archive_unavailable()
        if not _order_archive_userid(request, config):
            return _not_found()
        query = ArchiveOrderGroupInput(data=request.data)
        query.is_valid(raise_exception=True)
        chat_id = query.validated_data["chat_id"]
        try:
            number = _order_match_number(request, config, order, chat_id, remember=True)
        except ArchiveError:
            return _archive_failure("企微客户群详情暂时无法核对，请稍后重试")
        if not number:
            return _not_found()
        with transaction.atomic():
            if archive_order(request, order_no) is None:
                return _not_found()
            link = _remember_order_group(config, order, number, chat_id)
            if link is None:
                return _not_found()
            association, created = link
            if created:
                record(
                    log_type=ACTION_LOG_TYPES["order.wecom_chat_link"], action="order.wecom_chat_link",
                    actor=request.user.get_username(), actor_id=request.user.pk, target_kind="order",
                    target_id=order.order_no, order_no=order.order_no, to_value=str(association.pk),
                )
        return api_response({"chat_id": chat_id})


class ArchiveMessagesQuery(serializers.Serializer):
    chat_id = serializers.CharField(max_length=256, trim_whitespace=False)
    before = serializers.IntegerField(required=False, min_value=1, max_value=9223372036854775807)


def _current_archive_config(config):
    return WecomAppConfig.objects.filter(
        pk=config.pk, tenant_schema=connection.schema_name, identity_revision=config.identity_revision,
        enabled=True, group_sync_enabled=True,
    )


def _config_is_current(config):
    return _current_archive_config(config).exists()


def _archived_messages(config, chat_id):
    return WecomArchiveMessage.objects.filter(identity_revision=config.identity_revision, chat_id=chat_id)


def _older_than(boundary):
    return Q(sent_at__lt=boundary.sent_at) | Q(sent_at=boundary.sent_at, pk__lt=boundary.pk)


def _newer_than(boundary):
    return Q(sent_at__gt=boundary.sent_at) | Q(sent_at=boundary.sent_at, pk__gt=boundary.pk)


def _messages_payload(request, config, page, *, next_before, next_after=None, userid=None):
    if userid is None:
        userid = archive_viewer_userid(request.user)
    try:
        result = []
        for row in page:
            message = {"msgid": row.msg_id, "secret_key": decrypt_message_key(row), "sent_at": row.sent_at.isoformat()}
            if row.sender_id and row.sender_type is not None:
                message["sender"] = {
                    "id": row.sender_id,
                    "type": row.sender_type,
                    "is_self": row.sender_type == WecomArchiveMessage.SenderType.EMPLOYEE and row.sender_id == userid,
                }
            result.append(message)
    except ArchiveError:
        return _archive_failure("企微会话密钥不可用，请联系管理员核对存档公钥配置")

    external = [
        message["sender"] for message in result
        if message.get("sender", {}).get("type") == WecomArchiveMessage.SenderType.EXTERNAL
    ]
    if external:
        badges = external_sender_badges(config, (sender["id"] for sender in external))
        for sender in external:
            sender.update(badges.get(sender["id"], {}))
        if not _config_is_current(config):
            return _archive_unavailable()

    data = {"messages": result, "next_before": next_before, "requires_login": False}
    if next_after is not None:
        data["next_after"] = next_after
    return api_response(data)


def _older_messages(request, config, messages, before, *, userid=None):
    if before is not None:
        boundary = messages.filter(pk=before).only("sent_at").first()
        if boundary is None:
            return api_response(code=ErrorCode.VALIDATION, msg="会话分页已失效，请刷新")
        messages = messages.filter(_older_than(boundary))
    rows = list(messages.order_by("-sent_at", "-pk")[:_MESSAGE_PAGE_SIZE + 1])
    page = rows[:_MESSAGE_PAGE_SIZE]
    next_before = page[-1].pk if len(rows) > _MESSAGE_PAGE_SIZE else None
    return _messages_payload(request, config, page, next_before=next_before, userid=userid)


class OrderArchiveMessagesView(ArchiveOrderView):
    def get(self, request, order_no):
        order = archive_order(request, order_no)
        if order is None:
            return _not_found()
        config = order_archive_config()
        if config is None:
            return _archive_unavailable()

        query = ArchiveMessagesQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        chat_id = query.validated_data["chat_id"]
        if _closed_groups(config).filter(chat_id=chat_id).exists():
            return _not_found()
        linked = _order_group_links(config, order).filter(chat_id=chat_id).exists()
        matched_number = order.order_no
        if not linked:
            try:
                matched_number = _order_match_number(request, config, order, chat_id)
            except ArchiveError:
                return _archive_failure("企微客户群详情暂时无法核对，请稍后重试")
            if not matched_number:
                return _not_found()
        messages = _archived_messages(config, chat_id)
        userid = _order_archive_userid(request, config)
        if not userid:
            return api_response({"messages": [], "next_before": None, "requires_login": True})
        if not _config_is_current(config):
            return _not_found()

        before = query.validated_data.get("before")
        response = _older_messages(request, config, messages, before, userid=userid)
        if response.data["code"] != ErrorCode.OK:
            return response
        current_order = archive_order(request, order_no)
        if current_order is None or (current_order.pk, current_order.source_order_no) != (order.pk, order.source_order_no):
            return _not_found()
        if linked and not _order_group_links(config, current_order).filter(chat_id=chat_id).exists():
            return _not_found()
        if matched_number != order.order_no and _eligible_legacy_number(current_order) != matched_number:
            return _not_found()
        if not _config_is_current(config) or _closed_groups(config).filter(chat_id=chat_id).exists():
            return _not_found()
        return response
