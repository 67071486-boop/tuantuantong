from collections.abc import Mapping

from django.conf import settings
from django.db import transaction
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import serializers as fields
from rest_framework.exceptions import APIException, NotAuthenticated, NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.authentication import TenantBoundAuthentication
from apps.core.envelope import api_response
from apps.core.permissions import is_tenant_admin
from apps.core.plane import PLANE_NOT_FOUND_MESSAGE, on_public_plane

from . import serializers, visitor_seats, visitor_service as visitors
from .views import _validated


class Register(fields.Serializer):
    phone = fields.RegexField(r"\A1[3-9][0-9]{9}\Z", max_length=11)


class ReceptionGreeting(fields.Serializer):
    contact_id = fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT)
    enabled = fields.BooleanField()
    text = fields.CharField(allow_blank=True, max_length=4000, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["enabled"] and not attrs["text"].strip():
            raise fields.ValidationError({"text": "请填写已开启的欢迎语"})
        return attrs


class ReceptionUpdate(fields.Serializer):
    contact_ids = fields.ListField(
        child=fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT),
        max_length=visitors.MAX_RECEPTION_CONTACTS,
    )
    removed_contact_ids = fields.ListField(
        child=fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT),
        max_length=visitors.MAX_RECEPTION_CONTACTS,
        required=False,
    )
    greetings = ReceptionGreeting(many=True, max_length=visitors.MAX_RECEPTION_CONTACTS, required=False)
    greeting_sender_id = fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT, allow_null=True, required=False)
    greeting_text = fields.CharField(allow_blank=True, max_length=4000, required=False)

    def validate(self, attrs):
        ids = attrs["contact_ids"]
        if len(ids) != len(set(ids)):
            raise fields.ValidationError({"contact_ids": "接待人员不能重复选择"})
        legacy_fields = {"greeting_sender_id", "greeting_text"}
        if "greetings" in attrs:
            if legacy_fields.intersection(attrs):
                raise fields.ValidationError({"greetings": "请使用同一种欢迎语配置格式"})
            configured = [row["contact_id"] for row in attrs["greetings"]]
            if len(configured) != len(set(configured)) or not set(configured).issubset(ids):
                raise fields.ValidationError({"greetings": "欢迎语须对应接待名单中的人员，每人仅一条"})
            removed = attrs.get("removed_contact_ids", [])
            if len(removed) != len(set(removed)) or set(removed).intersection(ids):
                raise fields.ValidationError({"removed_contact_ids": "移除的接待人员不能重复或同时保留"})
            return attrs
        if "removed_contact_ids" in attrs:
            raise fields.ValidationError({"removed_contact_ids": "请使用按人欢迎语配置移除接待人员"})
        if not legacy_fields.issubset(attrs):
            raise fields.ValidationError({"greetings": "请提供欢迎语配置"})
        sender = attrs["greeting_sender_id"]
        if sender is not None and sender not in ids:
            raise fields.ValidationError({"greeting_sender_id": "欢迎语发送人必须是接待人员"})
        if attrs["greeting_text"] and sender is None:
            raise fields.ValidationError({"greeting_sender_id": "请为欢迎语选择发送人"})
        return attrs


class SeatShift(fields.Serializer):
    contact_id = fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT)
    weekday = fields.IntegerField(min_value=0, max_value=6)
    start = fields.RegexField(r"\A(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z", max_length=5, trim_whitespace=False)
    end = fields.RegexField(r"\A(?:(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00)\Z", max_length=5, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["start"] == attrs["end"]:
            raise fields.ValidationError({"end": "班次起止时间不能相同"})
        return attrs


class SeatSettings(fields.Serializer):
    kind = fields.ChoiceField(choices=visitor_seats.Seat.values)
    contact_ids = fields.ListField(
        child=fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT),
        max_length=visitors.MAX_RECEPTION_CONTACTS,
    )
    capacity = fields.IntegerField(min_value=1, max_value=1000)
    shifts = SeatShift(many=True, max_length=visitors.MAX_RECEPTION_CONTACTS * 14)

    def validate(self, attrs):
        ids = set(attrs["contact_ids"])
        if len(ids) != len(attrs["contact_ids"]):
            raise fields.ValidationError({"contact_ids": "接待人员不能重复选择"})
        if any(shift["contact_id"] not in ids for shift in attrs["shifts"]):
            raise fields.ValidationError({"shifts": "请为该席位的接待人员安排班次"})
        return attrs


class SeatSettingsUpdate(fields.Serializer):
    seats = SeatSettings(many=True, max_length=len(visitor_seats.Seat.values))

    def validate_seats(self, rows):
        if {row["kind"] for row in rows} != set(visitor_seats.Seat.values):
            raise fields.ValidationError("请同时配置人事客服和订单客服")
        return sorted(rows, key=lambda row: visitor_seats.Seat.values.index(row["kind"]))


class DeskAvailability(fields.Serializer):
    enabled = fields.BooleanField()


class DeskTransfer(fields.Serializer):
    target_id = fields.IntegerField(min_value=1, max_value=serializers.MAX_BIGINT)


class VisitorMessageSend(serializers.MessageSend):
    mention_member_ids = None

    def validate_client_id(self, value):
        if value == visitors.GREETING_CLIENT_ID:
            raise fields.ValidationError("消息标识无效")
        return value

    def validate(self, attrs):
        if set(self.initial_data) - set(self.fields):
            raise fields.ValidationError("访客会话仅支持文字、图片和引用回复")
        return super().validate(attrs)

    def preflight_guest_image(self, conversation):
        data = self.initial_data
        if not isinstance(data, Mapping):
            return None
        image = self.fields["image"].get_value(data)
        images = self.fields["images"].get_value(data)
        if image is fields.empty and images is fields.empty:
            return None
        client = self.fields["client_id"]
        client_id = self.validate_client_id(client.run_validation(client.get_value(data)))
        return visitors.preflight_guest_image(
            conversation, client_id=client_id, is_album=images is not fields.empty,
            parts=len(images) if isinstance(images, list) and images else 1,
        )


class VisitorUnavailable(APIException):
    status_code = 404
    default_detail = PLANE_NOT_FOUND_MESSAGE


class VisitorView(APIView):
    authentication_classes = [TenantBoundAuthentication]
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        if not settings.TT_INTERNAL_CHAT_VISITORS_ENABLED or on_public_plane():
            raise VisitorUnavailable()
        super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response


class VisitorSettingsView(VisitorView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not is_tenant_admin(request.user):
            raise PermissionDenied()

    def get(self, request):
        return api_response(visitors.reception_settings_payload())

    @transaction.atomic
    def put(self, request):
        if "seats" in request.data:
            params = _validated(SeatSettingsUpdate, request.data)
            payload, changed = visitors.set_reception_seats(params["seats"])
            changed_field = "访客客服席位、班次及接待上限"
        else:
            params = _validated(ReceptionUpdate, request.data)
            payload, changed = visitors.set_reception_settings(**params)
            changed_field = "访客接待人员、欢迎语"
        if changed:
            action = "system.internal_chat_directory_update"
            record(
                log_type=ACTION_LOG_TYPES[action], action=action,
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="chat_visitor", target_id="1", field_name=changed_field,
            )
        return api_response(payload)


@method_decorator(csrf_protect, name="dispatch")
class VisitorEndpoint(VisitorView):
    as_guest = True
    needs_visitor = True

    def get_authenticators(self):
        return [] if self.as_guest else super().get_authenticators()

    def get_permissions(self):
        return [AllowAny()] if self.as_guest else super().get_permissions()

    def get_authenticate_header(self, request):
        return "Visitor" if self.as_guest else super().get_authenticate_header(request)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.visitor = None
        expected_user = request.headers.get("X-Chat-Page-User")
        if self.as_guest:
            self.digest = visitors.credential_digest(request.COOKIES.get(visitors.COOKIE_NAME))
            self.visitor = visitors.visitor_for(self.digest)
            if self.needs_visitor and self.visitor is None:
                raise NotAuthenticated("请先注册访客")
            viewer_id = visitors.VISITOR_ID_OFFSET + self.visitor.pk if self.visitor else None
        else:
            self.reception = visitors.require_desk_account(request.user.pk)
            viewer_id = request.user.pk
        if expected_user is not None and (viewer_id is None or expected_user != str(viewer_id)):
            raise PermissionDenied("访客沟通身份已变更，请重新连接")

    def conversations(self):
        staff_id = None if self.as_guest else self.request.user.pk
        return visitors.visible_conversations(visitor=self.visitor, staff_id=staff_id)

    def conversation_rows(self, conversation_id):
        if conversation_id < 1 or conversation_id > serializers.MAX_BIGINT:
            raise NotFound()
        return self.conversations().filter(pk=conversation_id)

    def conversation(self, conversation_id):
        return get_object_or_404(self.conversation_rows(conversation_id))

    def me(self, request):
        if self.as_guest:
            user = visitors.guest_peer(self.visitor)
            return {"id": user["id"], "display_name": user["name"]}
        return {"id": request.user.pk, "display_name": user_display_name(request.user)}


class VisitorBootstrapView(VisitorEndpoint):
    needs_visitor = False

    def get(self, request):
        get_token(request)
        payload = {"registered": self.visitor is not None}
        if visitors.seat_mode():
            payload["seat_mode"] = True
        response = api_response(payload)
        if not self.digest or (
            self.visitor is None and visitors.VisitorIdentity.objects.filter(token_digest=self.digest).exists()
        ):
            response.set_cookie(
                visitors.COOKIE_NAME, visitors.issue_token(), max_age=visitors.SESSION_SECONDS,
                path=visitors.COOKIE_PATH, httponly=True, samesite="Lax",
                secure=not settings.DEBUG or request.is_secure(),
            )
        return response


class VisitorRegisterView(VisitorEndpoint):
    needs_visitor = False

    def post(self, request):
        if not self.digest:
            raise NotAuthenticated("访客注册已过期，请重新进入")
        params = _validated(Register, request.data)
        self.visitor, conversation_id = visitors.register(request, self.digest, **params)
        payload = {"user": self.me(request), "conversation_id": conversation_id}
        if visitors.seat_mode():
            payload["seat_mode"] = True
        return api_response(payload)


class VisitorMeView(VisitorEndpoint):
    def get(self, request):
        payload = {"user": self.me(request)}
        if not self.as_guest and self.reception.seats is not None:
            payload.update(seat_mode=True, can_receive=visitor_seats.reception_member(self.reception, request.user.pk))
        return api_response(payload)


class VisitorContactsView(VisitorEndpoint):
    def get(self, request):
        params = _validated(serializers.ContactsQuery, request.query_params)
        data = visitors.guest_contacts(params["q"]) if self.as_guest else {"contacts": [], "worker_directory_only": False}
        return api_response(data)


class VisitorConversationsView(VisitorEndpoint):
    def get(self, request):
        params = _validated(serializers.ConversationQuery, request.query_params)
        staff_id = None if self.as_guest else request.user.pk
        return api_response(visitors.list_conversations(self.conversations(), self.as_guest, staff_id=staff_id, **params))


class GuestConversationsView(VisitorConversationsView):
    def post(self, request):
        params = _validated(serializers.ConversationStart, request.data)
        return api_response(visitors.start_conversation(self.visitor, **params))


class VisitorConversationDetailView(VisitorEndpoint):
    def get(self, request, conversation_id):
        return api_response(visitors.conversation_summary(self.conversation_rows(conversation_id), self.as_guest))


class VisitorMessagesView(VisitorEndpoint):
    def get(self, request, conversation_id):
        params = _validated(serializers.MessageQuery, request.query_params)
        return api_response(visitors.history(
            self.conversation(conversation_id), as_guest=self.as_guest,
            staff_id=None if self.as_guest else request.user.pk, **params,
        ))

    def post(self, request, conversation_id):
        conversation = self.conversation(conversation_id)
        schema = VisitorMessageSend(data=request.data)
        if self.as_guest:
            retry = schema.preflight_guest_image(conversation)
            if retry is not None:
                return api_response(retry)
        schema.is_valid(raise_exception=True)
        return api_response(visitors.send_message(
            conversation, as_guest=self.as_guest,
            staff_sender=None if self.as_guest else request.user, **schema.validated_data,
        ))


class VisitorMessageImageView(VisitorEndpoint):
    def get(self, request, conversation_id, message_id):
        if not 0 < message_id <= serializers.MAX_BIGINT:
            raise NotFound()
        return api_response(visitors.read_image(
            self.conversation(conversation_id), message_id, as_guest=self.as_guest,
            staff_id=None if self.as_guest else request.user.pk,
        ))


class VisitorReadView(VisitorEndpoint):
    def post(self, request, conversation_id):
        params = _validated(serializers.MarkRead, request.data)
        return api_response(visitors.mark_read(
            self.conversation(conversation_id), self.as_guest, staff_id=None if self.as_guest else request.user.pk, **params,
        ))


class VisitorSyncView(VisitorEndpoint):
    def get(self, request):
        params = _validated(serializers.SyncQuery, request.query_params)
        staff_id = None if self.as_guest else request.user.pk
        if staff_id is not None and self.reception.seats is not None:
            visitors.refresh_desk_availability(staff_id)
        return api_response(visitors.sync(self.conversations(), as_guest=self.as_guest, staff_id=staff_id, **params))


class VisitorUnreadView(VisitorEndpoint):
    def get(self, request):
        return api_response({"unread_count": visitors.unread_count(self.conversations(), self.as_guest)})


class VisitorDeskAvailabilityView(VisitorEndpoint):
    as_guest = False

    def get(self, request):
        return api_response(visitors.desk_availability(request.user.pk))

    @transaction.atomic
    def post(self, request):
        params = _validated(DeskAvailability, request.data)
        payload, changed = visitors.set_desk_availability(request.user.pk, **params)
        if changed:
            action = "system.internal_chat_visitor_availability"
            enabled = payload["enabled"]
            record(
                log_type=ACTION_LOG_TYPES[action], action=action,
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="visitor_desk", target_id=request.user.pk,
                from_value="暂停接待" if enabled else "接待中", to_value="接待中" if enabled else "暂停接待",
            )
        return api_response(payload)


class VisitorDeskTransferView(VisitorEndpoint):
    as_guest = False

    def get(self, request, conversation_id):
        return api_response(visitors.desk_transfer_info(self.conversation(conversation_id), request.user.pk))

    @transaction.atomic
    def post(self, request, conversation_id):
        params = _validated(DeskTransfer, request.data)
        payload, _ = visitors.update_desk_conversation(self.conversation(conversation_id), request.user.pk, **params)
        action = "system.internal_chat_visitor_transfer"
        record(
            log_type=ACTION_LOG_TYPES[action], action=action,
            actor=request.user.get_username(), actor_id=request.user.pk,
            target_kind="chat_visitor", target_id=conversation_id, to_value=params["target_id"],
        )
        return api_response(payload)


class VisitorDeskCloseView(VisitorEndpoint):
    as_guest = False

    @transaction.atomic
    def post(self, request, conversation_id):
        payload, changed = visitors.update_desk_conversation(self.conversation(conversation_id), request.user.pk)
        if changed:
            action = "system.internal_chat_visitor_close"
            record(
                log_type=ACTION_LOG_TYPES[action], action=action,
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="chat_visitor", target_id=conversation_id,
            )
        return api_response(payload)
