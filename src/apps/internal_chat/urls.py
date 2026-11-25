from django.urls import include, path

from apps.business import converters  # noqa: F401
from apps.core.authentication import AppTokenAuthentication

from . import browser_access, export_views, order_authority, order_intents, peer_chat, views, visitor_views
from .system_notice_views import SystemNoticeSettingsView

APP_GROUP_AUTH = {"authentication_classes": [AppTokenAuthentication], "worker_only": True}


def _visitor_conversation_urls(*, as_guest):
    conversations = visitor_views.GuestConversationsView if as_guest else visitor_views.VisitorConversationsView
    return [
        path("me/", visitor_views.VisitorMeView.as_view(as_guest=as_guest)),
        path("contacts/", visitor_views.VisitorContactsView.as_view(as_guest=as_guest)),
        path("conversations/", conversations.as_view(as_guest=as_guest)),
        path("conversations/<int:conversation_id>/", visitor_views.VisitorConversationDetailView.as_view(as_guest=as_guest)),
        path("conversations/<int:conversation_id>/messages/", visitor_views.VisitorMessagesView.as_view(as_guest=as_guest)),
        path(
            "conversations/<int:conversation_id>/messages/<int:message_id>/image/",
            visitor_views.VisitorMessageImageView.as_view(as_guest=as_guest),
        ),
        path("conversations/<int:conversation_id>/read/", visitor_views.VisitorReadView.as_view(as_guest=as_guest)),
        path("sync/", visitor_views.VisitorSyncView.as_view(as_guest=as_guest)),
        path("unread/", visitor_views.VisitorUnreadView.as_view(as_guest=as_guest)),
    ]


def _visitor_desk_urls():
    return [
        *_visitor_conversation_urls(as_guest=False),
        path("availability/", visitor_views.VisitorDeskAvailabilityView.as_view()),
        path("conversations/<int:conversation_id>/transfer/", visitor_views.VisitorDeskTransferView.as_view()),
        path("conversations/<int:conversation_id>/close/", visitor_views.VisitorDeskCloseView.as_view()),
    ]


urlpatterns = [
    path("visitor/settings/", visitor_views.VisitorSettingsView.as_view()),
    path("visitor/guest/bootstrap/", visitor_views.VisitorBootstrapView.as_view()),
    path("visitor/guest/register/", visitor_views.VisitorRegisterView.as_view()),
    path("visitor/guest/", include(_visitor_conversation_urls(as_guest=True))),
    path("visitor/desk/", include(_visitor_desk_urls())),
    path("direct-ticket/", browser_access.DirectChatTicketView.as_view(), name="internal-chat-direct-ticket"),
    path("service/browser-authorize/", browser_access.ServiceBrowserAuthorityView.as_view(), name="internal-chat-service-browser-authorize"),
    path("service/order-intents/", order_intents.ServiceOrderIntentsView.as_view(), name="internal-chat-service-order-intents"),
    path("service/peer-chat/", peer_chat.ServicePeerChatView.as_view(), name="internal-chat-service-peer-chat"),
    path("service/order-authorizations/", order_authority.ServiceOrderAuthorityView.as_view(), name="internal-chat-service-order-authorizations"),
    path("service/export/", export_views.ServiceExportView.as_view(), name="internal-chat-service-export"),
    path("service/snapshot/", export_views.ServiceSnapshotView.as_view(), name="internal-chat-service-snapshot"),
    path("app/access/", views.AppChatAccessView.as_view(), name="internal-chat-app-access"),
    path("app/refresh/", views.AppChatRefreshView.as_view(), name="internal-chat-app-refresh"),
    path("app/me/", views.AppChatMeView.as_view(), name="internal-chat-app-me"),
    path("app/unread/", views.AppChatUnreadView.as_view(), name="internal-chat-app-unread"),
    path(
        "app/order-groups/<int:conversation_id>/messages/",
        views.NotificationMessagesView.as_view(**APP_GROUP_AUTH),
        name="internal-chat-app-order-group-messages",
    ),
    path(
        "app/order-groups/<int:conversation_id>/read/", views.NotificationReadView.as_view(**APP_GROUP_AUTH),
        name="internal-chat-app-order-group-read",
    ),
    path(
        "app/order-groups/<int:conversation_id>/messages/<int:message_id>/image/",
        views.NotificationImageView.as_view(**APP_GROUP_AUTH), name="internal-chat-app-order-group-image",
    ),
    path(
        "order-groups/<int:conversation_id>/messages/", views.NotificationMessagesView.as_view(),
        name="internal-chat-order-group-messages",
    ),
    path(
        "order-groups/<int:conversation_id>/read/", views.NotificationReadView.as_view(),
        name="internal-chat-order-group-read",
    ),
    path(
        "order-groups/<int:conversation_id>/messages/<int:message_id>/image/",
        views.NotificationImageView.as_view(), name="internal-chat-order-group-image",
    ),
    path(
        "notification-conversations/<int:conversation_id>/messages/",
        views.NotificationMessagesView.as_view(accept_direct=True), name="internal-chat-notification-messages",
    ),
    path(
        "notification-conversations/<int:conversation_id>/read/",
        views.NotificationReadView.as_view(accept_direct=True), name="internal-chat-notification-read",
    ),
    path(
        "notification-conversations/<int:conversation_id>/messages/<int:message_id>/image/",
        views.NotificationImageView.as_view(accept_direct=True), name="internal-chat-notification-image",
    ),
    path(
        "app/notification-conversations/<int:conversation_id>/messages/",
        views.NotificationMessagesView.as_view(accept_direct=True, **APP_GROUP_AUTH),
        name="internal-chat-app-notification-messages",
    ),
    path(
        "app/notification-conversations/<int:conversation_id>/read/",
        views.NotificationReadView.as_view(accept_direct=True, **APP_GROUP_AUTH),
        name="internal-chat-app-notification-read",
    ),
    path(
        "app/notification-conversations/<int:conversation_id>/messages/<int:message_id>/image/",
        views.NotificationImageView.as_view(accept_direct=True, **APP_GROUP_AUTH),
        name="internal-chat-app-notification-image",
    ),
    path("orders/<order_no:order_no>/", views.OrderChatView.as_view(), name="internal-chat-order"),
    path("orders/<order_no:order_no>/messages/", views.OrderChatMessagesView.as_view(), name="internal-chat-order-messages"),
    path(
        "orders/<order_no:order_no>/messages/<int:message_id>/image/",
        views.OrderChatImageView.as_view(), name="internal-chat-order-image",
    ),
    path("orders/<order_no:order_no>/reopen/", views.OrderChatReopenView.as_view(), name="internal-chat-order-reopen"),
    path("orders/<order_no:order_no>/join/", views.OrderChatJoinView.as_view(), name="internal-chat-order-join"),
    path("contacts/", views.ContactsView.as_view(), name="internal-chat-contacts"),
    path("settings/", views.ContactDirectorySettingsView.as_view(), name="internal-chat-settings"),
    path("system-notification-settings/", SystemNoticeSettingsView.as_view(), name="internal-chat-notice-settings"),
    path("conversations/", views.ConversationsView.as_view(), name="internal-chat-conversations"),
    path("unread/", views.UnreadView.as_view(), name="internal-chat-unread"),
    path(
        "conversations/<int:conversation_id>/preferences/",
        views.ConversationPreferencesView.as_view(), name="internal-chat-preferences",
    ),
    path("conversations/<int:conversation_id>/messages/", views.MessagesView.as_view(), name="internal-chat-messages"),
    path(
        "conversations/<int:conversation_id>/messages/search/", views.MessageSearchView.as_view(),
        name="internal-chat-message-search",
    ),
    path("conversations/<int:conversation_id>/read/", views.ReadView.as_view(), name="internal-chat-read"),
    path(
        "conversations/<int:conversation_id>/presence-ticket/",
        views.PresenceTicketView.as_view(), name="internal-chat-presence-ticket",
    ),
    path("conversations/<int:conversation_id>/pin/", views.OrderMessagePinView.as_view(), name="internal-chat-pin"),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/image/",
        views.MessageImageView.as_view(), name="internal-chat-message-image",
    ),
    path(
        "conversations/<int:conversation_id>/messages/<int:message_id>/revoke/",
        views.RevokeView.as_view(),
        name="internal-chat-revoke",
    ),
    path("sync/", views.SyncView.as_view(), name="internal-chat-sync"),
    path("realtime-ticket/", views.RealtimeTicketView.as_view(), name="internal-chat-realtime-ticket"),
]
