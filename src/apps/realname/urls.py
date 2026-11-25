"""实名认证的租户侧路由。

挂载点：`config/urls.py` 的 `path("api/auth/realname/", include("apps.realname.urls"))`
—— ⚠️ **排在 `path("", include("apps.core.urls"))` 之前**（那一份里是几条 `api/auth/…`），
照该文件顶部的排序口径：「更具体的前缀优先」。两条前缀今天不重叠，顺序不构成判据，
但写法要一致（将来 core 再加一条更宽的 `api/auth/` 路由也不会截走本模块）。

⚠️ 路径**带尾斜杠**（`api/auth/realname/verify/`），与对标端点清单（spec §9.1）逐字一致；
⛔ 不要为了"顺手"去掉（前端按 spec 写死路径）。

⚠️ **只这两条**：管理页那五个端点（列表 / 配置读写 / 余量 / 重置）是**下一批**任务
（spec §9.1 的后五条），本模块今天不假装有它们。
"""
from django.urls import path

from apps.realname.views import RealNameStatusView, RealNameVerifyView

urlpatterns = [
    # 用户给自己核验一次（`{name, id_card_no}`）。
    path("verify/", RealNameVerifyView.as_view(), name="realname-verify"),
    # 我的实名状态 + 本租户是否启用 + 剩余次数（⛔ 不含任何密钥字段）。
    path("", RealNameStatusView.as_view(), name="realname-status"),
]
