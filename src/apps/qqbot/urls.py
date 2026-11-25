"""QQ 机器人设置页的路由。

全部是显式 `path()`（这一页只有「读 / 改一个单例 / 按群操作」，没有需要 router 的
集合 CRUD；照 `apps/business/api/system_switch_views.py` 的同一条理由）。

⚠️ 挂载点在 `config/urls.py`：`path("api/qqbot/", include("apps.qqbot.urls"))`，
必须排在 `path("", include("apps.core.urls"))` 之前（该文件的 `api/` 前缀排序纪律）。
"""
from django.urls import path

from apps.qqbot.api import views

urlpatterns = [
    path("binding/", views.QQBotBindingView.as_view(), name="qqbot-binding"),
    path("groups/", views.QQBotGroupListView.as_view(), name="qqbot-groups"),
    path("groups/<int:pk>/", views.QQBotGroupDetailView.as_view(), name="qqbot-group-detail"),
    path(
        "groups/<int:pk>/push-config/",
        views.QQBotGroupPushConfigView.as_view(),
        name="qqbot-group-push-config",
    ),
    path(
        "groups/<int:pk>/test-push/",
        views.QQBotGroupTestPushView.as_view(),
        name="qqbot-group-test-push",
    ),
    path("groups/<int:pk>/mutes/", views.QQBotGroupMutesView.as_view(), name="qqbot-group-mutes"),
    path(
        "groups/<int:pk>/join-requests/",
        views.QQBotGroupJoinRequestsView.as_view(),
        name="qqbot-group-join-requests",
    ),
    path(
        "groups/<int:pk>/join-requests/approve/",
        views.QQBotGroupJoinRequestApproveView.as_view(),
        name="qqbot-group-join-request-approve",
    ),
    path(
        "groups/<int:pk>/join-policy/",
        views.QQBotGroupJoinPolicyView.as_view(),
        name="qqbot-group-join-policy",
    ),
    path("menu/", views.QQBotMenuView.as_view(), name="qqbot-menu"),
    path("share-link/", views.QQBotShareLinkView.as_view(), name="qqbot-share-link"),
    path("messages/", views.QQBotMessageListView.as_view(), name="qqbot-messages"),
]
