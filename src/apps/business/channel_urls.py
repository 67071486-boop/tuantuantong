"""工作室频道的公开读路由（匿名）。 ⚠️ **独立模块、不并进 `apps/business/urls.py`**（那条 urlconf 挂在 `/api/business/` 前缀下、整体面向登录态的管理端点）：公开面是**另一条契约**
（匿名可读、只回已发布、不回 HTML），混进管理端点那棵树会让「哪几个端点是公开的」重新变成靠逐条读代码才能回答的问题。`config/urls.py` 把本模块挂在 `api/channel/`。
⚠️ 三条路径都**带尾斜杠**（计划 §1 的表）；本节点的平面守卫与可见性判据见 `apps/business/api/channel_views.py` 的模块 docstring。"""
from django.urls import path

from apps.business.api.channel_views import (
    ChannelArticleDetailView,
    ChannelArticleListView,
    ChannelProfileView,
)

urlpatterns = [
    path("profile/", ChannelProfileView.as_view(), name="channel-profile"),
    path("articles/", ChannelArticleListView.as_view(), name="channel-article-list"),
    path(
        "articles/<int:pk>/",
        ChannelArticleDetailView.as_view(),
        name="channel-article-detail",
    ),
]
