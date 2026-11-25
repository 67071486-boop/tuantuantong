from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.cache import add_never_cache_headers
from rest_framework.exceptions import MethodNotAllowed, NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.roles import WORKER
from apps.core.app_version import android_version_code
from apps.core.authentication import AppTokenAuthentication, TenantBoundAuthentication
from apps.core.envelope import api_response
from apps.core.permissions import is_tenant_admin
from apps.core.plane import PLANE_NOT_FOUND_MESSAGE, on_public_plane

from . import core_bridge, serializers, service
from .app_access import AppChatAuthentication, issue_app_chat_grant
from .maintenance import ChatMaintenanceMixin
from .models import Conversation, ConversationPreference, GroupMember, OrderGroup
from .realtime import internal_chat_available, issue_presence_ticket, issue_ticket
from .system_notifications import SYSTEM_NAME, SYSTEM_SENDER_ID


def _validated(serializer, data, *, partial=False):
    schema = serializer(data=data, partial=partial)
    schema.is_valid(raise_exception=True)
    return schema.validated_data


class ChatView(ChatMaintenanceMixin, APIView):
    authentication_classes = [TenantBoundAuthentication]
    requires_order_groups = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if on_public_plane():
            raise NotFound(PLANE_NOT_FOUND_MESSAGE)
        if self.requires_order_groups and not settings.DEBUG and not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
            raise NotFound()
        expected_user = request.headers.get("X-Chat-Page-User")
        if (
            expected_user is not None
            and isinstance(request.successful_authenticator, TenantBoundAuthentication)
            and expected_user != str(request.user.pk)
        ):
            raise PermissionDenied("登录状态已变更，请刷新页面后再打开聊天")


class AppChatView(ChatView):
    authentication_classes = [AppChatAuthentication, TenantBoundAuthentication]


def _app_chat_grant_response(token, *, deadline=None, android_version=None, include_host_capabilities=False):
    data = {"grant": issue_app_chat_grant(token, deadline=deadline, android_version=android_version)}
    if include_host_capabilities:
        data["close_in_webview"] = True
        data["retain_webview"] = True
    response = api_response(data)
    add_never_cache_headers(response)
    return response


class AppChatWorkerView(ChatMaintenanceMixin, APIView):
    authentication_classes = [AppTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not internal_chat_available() or not request.user.groups.filter(name=WORKER).exists():
            raise NotFound()


class AppChatAccessView(AppChatWorkerView):
    def post(self, request):
        return _app_chat_grant_response(
            request.auth, android_version=android_version_code(request), include_host_capabilities=True,
        )


class AppChatUnreadView(AppChatWorkerView):
    def get(self, request):
        response = api_response({"unread_count": service.unread_total(request.user.pk)})
        add_never_cache_headers(response)
        return response

    def post(self, request):
        response = api_response({"token": issue_ticket(request.user.pk)})
        add_never_cache_headers(response)
        return response


class AppChatRefreshView(ChatMaintenanceMixin, APIView):
    authentication_classes = [AppChatAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        return _app_chat_grant_response(
            request.auth.token, deadline=request.auth.deadline, android_version=request.auth.android_version,
        )


class AppChatMeView(ChatMaintenanceMixin, APIView):
    authentication_classes = [AppChatAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.core.auth_views import _user_payload

        response = api_response({"user": _user_payload(request.user, request)})
        add_never_cache_headers(response)
        return response


class OrderChatView(AppChatView):
    def get_order(self, request, order_no):
        from apps.business.api.my_orders_views import MyOrdersViewSet, get_own_order_detail_performance
        from apps.business.api.order_views import OrderViewSet

        if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or (
            len(order_no) != 20 or not order_no.isascii() or not order_no.isdigit()
        ):
            raise Http404
        # Reuse both detail checks without opening credentials or enqueuing QR fetches.
        operator_view = OrderViewSet()
        operator_view.action, operator_view.request = "retrieve", request
        operator_view.kwargs = {"order_no": order_no}
        try:
            operator_view.check_permissions(request)
            order = operator_view.get_object()
        except (Http404, PermissionDenied):
            worker_view = MyOrdersViewSet()
            worker_view.action = "order_detail"
            try:
                worker_view.check_permissions(request)
                order = get_own_order_detail_performance(request.user, order_no).order
            except PermissionDenied as exc:
                raise Http404 from exc
        return order

    def get_group(self, request, order_no):
        return get_object_or_404(OrderGroup.objects.select_related("conversation"),order=self.get_order(request,order_no))

    def core_summary(self, request, order_no):
        self.get_order(request,order_no)
        return core_bridge.order_preview(request.user.pk,order_no)

    def get(self, request, order_no):
        if core_bridge.enabled():
            return api_response(self.core_summary(request,order_no))
        group = self.get_group(request, order_no)
        return api_response(service.order_summary(group, request.user.pk))


class OrderChatMessagesView(OrderChatView):
    def post(self, request, order_no):
        if not core_bridge.enabled():
            raise MethodNotAllowed('POST')
        self.get_order(request,order_no)
        params = _validated(serializers.MessageSend,request.data)
        return api_response(core_bridge.order_send(request.user,order_no,**params))

    def get(self, request, order_no):
        params = _validated(serializers.MessageQuery, request.query_params)
        if core_bridge.enabled():
            summary = self.core_summary(request,order_no)
            if summary['id'] is None:
                return api_response({'messages':[],'has_more':False,'has_newer':False})
            return api_response(core_bridge.operation('history',request.user.pk,summary['id'],query=params,readonly=True))
        group = self.get_group(request, order_no)
        return api_response(service.order_history(group, viewer_id=request.user.pk, **params))


class OrderChatImageView(OrderChatView):
    def get(self, request, order_no, message_id):
        if core_bridge.enabled():
            summary = self.core_summary(request,order_no)
            if summary['id'] is None:
                raise Http404
            data = core_bridge.operation('image',request.user.pk,summary['id'],query={'message_id':message_id},readonly=True)
        else:
            data = service.order_image(self.get_group(request,order_no),message_id,viewer_id=request.user.pk)
        response = api_response(data)
        add_never_cache_headers(response)
        return response


class OrderChatJoinView(OrderChatView):
    http_method_names = ["post", "options"]

    def post(self, request, order_no):
        from .order_groups import join_order_group

        if core_bridge.enabled():
            summary = self.core_summary(request,order_no)
            if summary['id'] is None:
                raise Http404
            return api_response(core_bridge.call('join',request.user.pk,query={'conversation_id':summary['id']},payload={'grant':True,'viewer':service.person_payload(request.user)}))
        self.get_group(request, order_no)
        group = join_order_group(request.user, lambda: self.get_group(request, order_no))
        return api_response(service.order_summary(group, request.user.pk))


class OrderChatReopenView(OrderChatView):
    http_method_names = ['post', 'options']

    def post(self, request, order_no):
        if not core_bridge.enabled():
            raise Http404
        summary = self.core_summary(request,order_no)
        if summary['id'] is None:
            raise Http404
        return api_response(core_bridge.operation('reopen',request.user.pk,summary['id']))


class UnreadView(AppChatView):
    def get(self, request):
        response = api_response({'unread_count': service.unread_total(request.user.pk)})
        add_never_cache_headers(response)
        return response


class ContactsView(AppChatView):
    def get(self, request):
        params = _validated(serializers.ContactsQuery, request.query_params)
        return api_response(service.contacts(request.user, params["q"]))


class ContactDirectorySettingsView(ChatView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not is_tenant_admin(request.user):
            raise PermissionDenied()

    def get(self, request):
        return api_response(service.directory_settings())

    @transaction.atomic
    def put(self, request):
        params = _validated(serializers.ContactDirectoryUpdate, request.data)
        directory, changed_fields = service.update_directory_settings(**params)
        if changed_fields:
            action = "system.internal_chat_directory_update"
            record(
                log_type=ACTION_LOG_TYPES[action], action=action,
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="chat_directory", target_id="1", field_name="、".join(changed_fields),
            )
        return api_response(directory)


class ConversationsView(AppChatView):
    def get(self, request):
        params = _validated(serializers.ConversationQuery, request.query_params) if core_bridge.enabled() else {}
        return api_response(service.conversations(request.user.pk, **params))

    def post(self, request):
        params = _validated(serializers.ConversationStart, request.data)
        return api_response(service.start_conversation(request.user, **params))


class ConversationPreferencesView(AppChatView):
    def post(self, request, conversation_id):
        params = _validated(serializers.ConversationPreferenceUpdate, request.data, partial=True)
        return api_response(service.update_preferences(request.user.pk, conversation_id, **params))


class MessagesView(AppChatView):
    def get(self, request, conversation_id):
        params = _validated(serializers.MessageQuery, request.query_params)
        return api_response(service.history(request.user.pk, conversation_id, **params))

    def post(self, request, conversation_id):
        params = _validated(serializers.MessageSend, request.data)
        return api_response(service.send(request.user, conversation_id, **params))


class MessageSearchView(AppChatView):
    def get(self, request, conversation_id):
        params = _validated(serializers.MessageSearchQuery, request.query_params)
        return api_response(service.search_messages(request.user.pk, conversation_id, **params))


class MessageImageView(AppChatView):
    def get(self, request, conversation_id, message_id):
        response = api_response(service.read_image(request.user.pk, conversation_id, message_id))
        add_never_cache_headers(response)
        return response


class OrderMessagePinView(AppChatView):
    def post(self, request, conversation_id):
        params = _validated(serializers.OrderMessagePin, request.data)
        return api_response(service.pin_order_message(request.user.pk, conversation_id, **params))


class ReadView(AppChatView):
    def post(self, request, conversation_id):
        params = _validated(serializers.MarkRead, request.data)
        seq = service.mark_read(request.user.pk, conversation_id, **params)
        return api_response({"read_seq": seq})


class NotificationConversationView(ChatView):
    worker_only = False
    accept_direct = False

    def core_target(self, request, conversation_id, **query):
        if (not settings.TT_CHAT_ORDER_GROUPS_ENABLED
                or not 0 < conversation_id <= serializers.MAX_BIGINT
                or self.worker_only and not request.user.groups.filter(name=WORKER).exists()):
            raise Http404
        target = core_bridge.operation('notification', request.user.pk, conversation_id, query=query)
        if target.get('kind') == 'direct' and not self.accept_direct:
            raise Http404
        return target

    def get_target(self, request, conversation_id):
        if core_bridge.enabled():
            return self.core_target(request, conversation_id)
        if (
            not settings.TT_CHAT_ORDER_GROUPS_ENABLED
            or not 0 < conversation_id <= serializers.MAX_BIGINT
            or (self.worker_only and not request.user.groups.filter(name=WORKER).exists())
        ):
            raise Http404
        if self.accept_direct:
            direct = Conversation.objects.filter(
                Q(first_user=request.user) | Q(second_user=request.user) | Q(system_recipient=request.user),
                pk=conversation_id,
            ).select_related("first_user", "second_user").first()
            if direct is not None:
                return direct
        return get_object_or_404(
            GroupMember.objects.select_related("group"),
            group__conversation_id=conversation_id,
            local_user_id=request.user.pk,
            left_at__isnull=True,
        )


class NotificationMessagesView(NotificationConversationView):
    def get(self, request, conversation_id):
        from .order_groups import can_join_order_group

        params = _validated(serializers.MessageQuery, request.query_params)
        if core_bridge.enabled():
            response = api_response(self.core_target(request, conversation_id, **params))
            add_never_cache_headers(response)
            return response
        target = self.get_target(request, conversation_id)
        page = service.history(request.user.pk, conversation_id, **params)
        senders = {message["sender_id"] for message in page["messages"]}
        remark = ConversationPreference.objects.filter(
            conversation_id=conversation_id, user=request.user,
        ).values_list("remark", flat=True).first()
        if isinstance(target, Conversation) and target.system_recipient_id is not None:
            details = {
                "kind": "system", "peer_name": SYSTEM_NAME, "order_no": "",
                "self_member_id": request.user.pk, "can_reply": False,
                "senders": [{"id": SYSTEM_SENDER_ID, "name": SYSTEM_NAME, "studio_name": ""}],
            }
        elif isinstance(target, Conversation):
            peer = target.second_user if target.first_user_id == request.user.pk else target.first_user
            details = {
                "kind": "direct", "peer_name": user_display_name(peer), "order_no": "",
                "self_member_id": request.user.pk, "can_reply": True,
                "senders": [
                    {"id": user.pk, "name": user_display_name(user), "studio_name": ""}
                    for user in (request.user, peer) if user.pk in senders
                ],
            }
        else:
            details = {
                "order_no": target.group.order_no, "self_member_id": target.pk,
                "can_reply": can_join_order_group(target.group),
                "senders": list(target.group.members.filter(pk__in=senders).values("id", "name", "studio_name")),
            }
        response = api_response({**page, **details, "remark": remark or ""})
        add_never_cache_headers(response)
        return response

    def post(self, request, conversation_id):
        self.get_target(request, conversation_id)
        params = _validated(serializers.MessageSend, request.data)
        return api_response(service.send(request.user, conversation_id, **params))


class NotificationReadView(NotificationConversationView, ReadView):
    def post(self, request, conversation_id):
        self.get_target(request, conversation_id)
        return super().post(request, conversation_id)


class NotificationImageView(NotificationConversationView, MessageImageView):
    def get(self, request, conversation_id, message_id):
        self.get_target(request, conversation_id)
        if not 0 < message_id <= serializers.MAX_BIGINT:
            raise Http404
        return super().get(request, conversation_id, message_id)


class RevokeView(AppChatView):
    def post(self, request, conversation_id, message_id):
        return api_response(service.revoke(request.user.pk, conversation_id, message_id))


class SyncView(AppChatView):
    def get(self, request):
        params = _validated(serializers.SyncQuery, request.query_params)
        return api_response(service.changes_since(request.user.pk, **params))


class RealtimeTicketView(AppChatView):
    def post(self, request):
        response = api_response({
            "token": issue_ticket(request.user.pk),
            "user": {"id": request.user.pk, "identity_plane": connection.schema_name},
        })
        add_never_cache_headers(response)
        return response


class PresenceTicketView(AppChatView):
    def post(self, request, conversation_id):
        if not internal_chat_available() or not 0 < conversation_id <= serializers.MAX_BIGINT:
            raise NotFound()
        viewer_id = request.user.pk
        peer_id = service.direct_conversation_peer(viewer_id, conversation_id)
        response = api_response({"token": issue_presence_ticket(viewer_id, conversation_id, peer_id)})
        add_never_cache_headers(response)
        return response
