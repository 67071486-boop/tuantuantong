"""企微自建应用的 API 路由（挂载点：`config/urls.py` 的 `api/wecom/`）。

⚠️ 回调路径**不带尾斜杠**（`callback`）：企微后台里填的 URL 逐字一致
（`/api/wecom/callback`）；其余端点带尾斜杠（DRF 侧惯例）。
"""
from django.urls import path

from apps.business import converters  # noqa: F401
from apps.wecom.aftersales_views import WecomAftersalesOptionsView, WecomAftersalesSettingsView
from apps.wecom.archive_search import ArchiveAccessView, ArchiveSearchMessagesView, ArchiveSearchView
from apps.wecom.archive_signatures import (
    ArchiveLoginView,
    ArchiveSignatureView,
    OrderArchiveLoginView,
    OrderArchiveSignatureView,
)
from apps.wecom.archive_views import OrderArchiveGroupsView, OrderArchiveMessagesView
from apps.wecom.client_lab import LabGroupsView, LabSignatureView
from apps.wecom.order_groups import OrderWecomGroupQrView, OrderWecomGroupsView
from apps.wecom.external_pay_views import ExternalPayInvalidView, ExternalPayIssuesView, ExternalPayStatusView
from apps.wecom.notification_views import WecomFeatureSettingsView, WecomNotificationRecordsView

from apps.wecom.views import (
    WecomCallbackView,
    WecomConfigView,
    WecomCustomersView,
    WecomDepartmentsView,
    WecomFollowUsersView,
    WecomGroupChatsView,
    WecomMessagesView,
    WecomSelfCheckView,
    WecomTestSendView,
    WecomUsersView,
)

urlpatterns = [
    path("aftersales/settings/", WecomAftersalesSettingsView.as_view(), name="wecom-aftersales-settings"),
    path("aftersales/options/", WecomAftersalesOptionsView.as_view(), name="wecom-aftersales-options"),
    path("client-lab/signature/", LabSignatureView.as_view(), name="wecom-client-lab-signature"),
    path("client-lab/groups/", LabGroupsView.as_view(), name="wecom-client-lab-groups"),
    path("notification/settings/", WecomFeatureSettingsView.as_view(), name="wecom-notification-settings"),
    path("notification/records/", WecomNotificationRecordsView.as_view(), name="wecom-notification-records"),
    path("external-pay/status/", ExternalPayStatusView.as_view(), name="wecom-external-pay-status"),
    path("external-pay/issues/", ExternalPayIssuesView.as_view(), name="wecom-external-pay-issues"),
    path("external-pay/invalid/", ExternalPayInvalidView.as_view(), name="wecom-external-pay-invalid"),
    # 设置：读 / 保存（secret 族留空 = 保持不变）。
    path("config/", WecomConfigView.as_view(), name="wecom-config"),
    path("config/selfcheck/", WecomSelfCheckView.as_view(), name="wecom-selfcheck"),
    path("config/test_send/", WecomTestSendView.as_view(), name="wecom-test-send"),
    # 通讯录（只读 proxy）。
    path("contacts/departments/", WecomDepartmentsView.as_view(), name="wecom-departments"),
    path("contacts/users/", WecomUsersView.as_view(), name="wecom-users"),
    # 回调消息台账（倒序分页读）。
    path("messages/", WecomMessagesView.as_view(), name="wecom-messages"),
    # 客户联系（只读 proxy）。
    path("external/follow_users/", WecomFollowUsersView.as_view(), name="wecom-follow-users"),
    path("external/customers/", WecomCustomersView.as_view(), name="wecom-customers"),
    path("external/groupchats/", WecomGroupChatsView.as_view(), name="wecom-groupchats"),
    path("orders/<order_no:order_no>/groups/", OrderWecomGroupsView.as_view(), name="wecom-order-groups"),
    path("orders/<order_no:order_no>/group-qr/", OrderWecomGroupQrView.as_view(), name="wecom-order-group-qr"),
    path("orders/<order_no:order_no>/chat/groups/", OrderArchiveGroupsView.as_view(), name="wecom-order-chat-groups"),
    path("orders/<order_no:order_no>/chat/messages/", OrderArchiveMessagesView.as_view(), name="wecom-order-chat-messages"),
    path("orders/<order_no:order_no>/chat/signature/", OrderArchiveSignatureView.as_view(), name="wecom-order-chat-signature"),
    path("orders/<order_no:order_no>/chat/login/", OrderArchiveLoginView.as_view(), name="wecom-order-chat-login"),
    path("archive/access/", ArchiveAccessView.as_view(), name="wecom-archive-access"),
    path("archive/search/", ArchiveSearchView.as_view(), name="wecom-archive-search"),
    path("archive/messages/", ArchiveSearchMessagesView.as_view(), name="wecom-archive-messages"),
    path("archive/signature/", ArchiveSignatureView.as_view(), name="wecom-archive-signature"),
    path("archive/login/", ArchiveLoginView.as_view(), name="wecom-archive-login"),
    # 企微服务器回调（匿名；企微后台填的就是这条不带尾斜杠的路径）。
    path("callback", WecomCallbackView.as_view(), name="wecom-callback"),
]
