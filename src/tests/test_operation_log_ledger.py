"""操作日志**埋点覆盖率的登记表**（spec §5.7 / §5.8 / §10.3 的显式集合）。本模块**只有数据**，断言在 `tests/test_operation_log_coverage.py`；它是「新功能落地时人要改的那一个文件」：新增或改动写端点后，那条测试会把未登记的端点报出来，改这里就行。
三张表怎么用（spec §10.3 第 4 点）：① `INSTRUMENTED`（spec §5 标「做」）——写端点 → 埋点动作；⚠️ 登记表**不能**替代码声明「我埋了」：测试按这里的 `action` 找到该端点的视图方法、要求在**它的源码里**读到 `record(`——本表是**指针**不是免责声明，代码里那行删掉断言就红（Task 12 的变异实验证明的正是这一点）。② `DELIBERATELY_NOT_LOGGED`（spec §5.7）——**刻意不记**的写端点带理由；必须**真实存在**于枚举集合里（不存在 ⇒ 红）且与 `INSTRUMENTED` 互斥；它们**不是**「漏了埋点」，是裁定后有意为之。③ `DEFERRED_ACTIONS`（spec §5.8）——**挂账**动作：今天**没有端点**，测试对每条断言「它声明的未来端点**现在不在**枚举集合里」，端点一旦出现而它还留在挂账表 ⇒ **红**（「功能上线必须补埋点」的自动提醒）；⚠️ **挂账不塞进不记白名单**——那会把「还没做」伪装成「有意不做」。④ `RESOLVED_ACTIONS`——**挂账的收口**（去向已定死）：既不是挂账（会让下一个人以为还有活没干）、也不是「刻意不记」（端点不存在）、也不是「不做」（其中有已上线的）；键集合关系由 `test_resolved_actions_are_explicitly_registered` 用等号/互斥钉住。
路径写法（归一化口径，见 `test_operation_log_coverage._normalise`）：`str(URLPattern.pattern)` 去掉 DefaultRouter 的 `.format` 重复路由、正则锚点与转义，`(?P<名>…)` 参数写成 Django 风格占位符，前缀 `^`（router include 残留）也去掉；例 `api/business/orders/<order_no>/throw-to-hall/`。抢单路由用共享的 `order_no` 转换器，键必须写成 `<order_no:order_no>`，否则同时报「未分类」和「找不到」。键一律 `(写方法小写, 归一化路径)`；`GET` / `HEAD` / `OPTIONS` **不是写方法**、不进前两张表（spec §10.3 只关心写端点）。挂账项例外：`test_deferred_actions_have_no_endpoint_yet` 的判据打在**端点全集（含读方法）**上，`future_endpoint_kind` 只作**形状登记**（由 `test_deferred_actions_are_explicitly_kinded` 钉住必须显式写 `read` / `write`），**不**按它选集合。⚠️ **普通函数视图**（`path("x/", some_view)`）没有方法集，枚举记成 `("<plain-view>", 路径)` 并**一律当写端点**（最保守一侧）：将来真加了函数视图，`test_every_enumerated_write_endpoint_is_classified` 会逼你给它埋点或登记「刻意不记」。今天全项目在覆盖范围内没有函数视图。"""
from collections import namedtuple  # noqa: E402  （紧跟 docstring，便于读者先看到数据形状）

# ---------------------------------------------------------------------------
# 覆盖范围（spec §10.3 第 1 点）—— 路由前缀 → 挂载模块
# ---------------------------------------------------------------------------

#: 三个 `DefaultRouter` 的挂载点 + `apps/core/urls.py`（认证端点）。测试用**递归遍历 `django.urls.get_resolver()`** 得到端点、再用这份前缀把范围声明出来；`test_three_routers_are_all_reachable` 按模块反查注册表，保证「声明了却没真的走到的 router」不会静默溜过去。⚠️ 用 `tuple` 不是 `dict`：`api/` 同时挂着 accounts 与 rbac **两个** router，`dict` 会吞掉一个。
#: 各行注记（router 形状）：`api/business/`=router（games/servers/categories/projects/orders/performances）；`api/`×2=rbac（role-authorities / permission-nodes，由 apps.core.urls include）+ accounts；`api/audit/`、`api/auth/`=无 router / 四条普通 APIView（login/logout/me/csrf）。
COVERAGE_SCOPE: tuple[tuple[str, str], ...] = (
    ("api/business/", "apps.business.urls"),
    ("api/", "apps.rbac.urls"),
    ("api/", "apps.accounts.urls"),
    ("api/audit/", "apps.audit.urls"),
    ("api/auth/", "apps.core.urls"),
    # 开放平台**对外**查单端点（spec §7.1 / §7.2）。⚠️ 与端点**同一次**登记（spec §17.1：先登记后建端点会让「声明了模块却一个 leaf 都没有 ⇒ 红」当场红）。本模块**无 router**（两条显式 `path()`、都只收 GET）⇒ 它同时要进 `test_operation_log_coverage.py` 的 `NON_ROUTER_MODULES`。
    ("api/open/", "apps.openapi.urls"),
    ("api/integration/v1/", "apps.openapi.integration_urls"),
    # 打手侧开放接口（spec `docs/specs/2026-10-08-taker-open-api-design.md` §8）。⚠️⭐ **登记时点 = 第一条真实路由的那一次**：这张网有一条判据是「`COVERAGE_SCOPE` 声明的模块**必须真的产出至少一个 leaf**」⇒ `urlpatterns` 还是空表时登记会**当场红**（红得有道理；现场读数见 `apps/takerapi/urls.py` 的模块 docstring）。⚠️ 本模块**无 router**（全是显式 `path()`）⇒ 同时进了 `NON_ROUTER_MODULES`（两处都是**恰好**断言）。
    ("api/taker/", "apps.takerapi.urls"),
    # ⚠️ 这一格**一个字都不用改**（它是"前缀 → 模块"的声明，⛔ 不是端点清单）——这正是"按模块枚举而不是手抄 URL 清单"的价值：新端点自动落进覆盖网；⚠️ 上面注释里的端点数**不是**现在的真值（.py 注释不适用「只增不改」），以本网运行时枚举到的为准。⭐⭐ 实名认证（T8）：用户自助那两个端点住在一份**独立的** `urlpatterns`（`apps/realname/urls.py`，被 `config/urls.py` 直接 `include()`）；⚠️ 它没有 router ⇒ 同时进了 `NON_ROUTER_MODULES`（两处都是**恰好**断言）；⛔ 不登记它的后果是三条网同时红（`undeclared` / `unclassified` / `produced - declared` 各自点名）。
    ("api/auth/realname/", "apps.realname.urls"),
    # ⭐⭐ 微信公众号集成（D-0268）：**两份** urlconf 同一次登记——API 端点（设置 / 菜单 / 匿名回调 / OAuth 入口与回跳）与**独立绑定确认页**（挂在 `/wechatmp/`，不在 `api/` 之外的前缀写法见 config/urls.py）。⚠️ 都没有 router（全是显式 `path()`）⇒ 同时进了 `NON_ROUTER_MODULES`（两处都是**恰好**断言）。
    ("api/wechatmp/", "apps.wechatmp.urls"),
    ("wechatmp/", "apps.wechatmp.page_urls"),
    # ⭐ QQ 机器人：设置页端点，独立 `urlpatterns`（`apps/qqbot/urls.py`，显式 `path()`、无 router）⇒ 同一次登记进 `NON_ROUTER_MODULES`（两处都是**恰好**断言）。写端点里只有 PATCH binding 一条记动作（`INSTRUMENTED`）；其余见下面 `DELIBERATELY_NOT_LOGGED` 的「QQ 机器人」块。
    ("api/qqbot/", "apps.qqbot.urls"),
    # ⭐ 企微自建应用线（D-0379）：设置页端点，独立 `urlpatterns`（`apps/wecom/urls.py`，
    # 显式 `path()`、无 router）⇒ 同一次登记进 `NON_ROUTER_MODULES`（两处都是**恰好**断言）。
    # 写端点里只有 PUT config 一条记动作（计划钉死照 qqbot 先例）；其余见
    # `DELIBERATELY_NOT_LOGGED` 的「企微自建应用」块。
    ("api/wecom/", "apps.wecom.urls"),
    # ⭐ 频道文章作品线：工作室频道的**公开读** urlconf（`apps/business/channel_urls.py`，三条只读显式 `path()`、无 router）。⚠️ 它与 `NON_ROUTER_MODULES` **同一次**登记（两处都是**恰好**断言）。本模块**没有任何写端点**（全 `GET`），登记它是为了让「新挂一块 urlconf 却没人声明」那条独立复核继续绿——⛔ 不是给它加审计负担。
    ("api/channel/", "apps.business.channel_urls"),
    ("api/internal-chat/", "apps.internal_chat.urls"),
)

# ---------------------------------------------------------------------------
# 1. 做（spec §5 标「做」的端点）—— 埋点动作指针
# ---------------------------------------------------------------------------

#: 外层键 = 承载该端点的**视图模块**（人读的定位信息）；值 = `(方法, 归一化路径) -> action code`。
#: `action` 取自 `apps/audit/actions.py` 的登记码。测试用它定位视图方法
#: （`cls.<actions[method]>`）并检查其源码里有没有 `record(`。
INSTRUMENTED: dict[str, dict[tuple[str, str], str]] = {
    "apps/core/wxpusher_views.py": {
        ("post", "api/auth/wxpusher/binding/progress"): "user.update",
        ("post", "api/auth/wxpusher/binding/link-confirm"): "user.update",
    },
    "apps/openapi/api/integration_images.py": {
        ("delete", "api/integration/v1/orders/<order_no:order_no>/images/<int:image_id>/"):
            "order.delete_screenshot",
    },
    "apps/openapi/api/integration_binding.py": {
        ("put", "api/integration/v1/taker-bindings/<sdk_subject:external_user_id>/"): "user.update",
        ("delete", "api/integration/v1/taker-bindings/<sdk_subject:external_user_id>/"): "user.update",
    },
    "apps/openapi/api/integration_hall.py": {
        ("post", "api/integration/v1/taker/hall/<order_no:order_no>/claim/"): "order.claim",
        ("post", "api/integration/v1/taker/orders/<order_no:order_no>/status/"): "order.transition",
    },
    "apps/openapi/api/integration_takers.py": {
        ("post", "api/integration/v1/takers/"): "user.create",
        ("patch", "api/integration/v1/takers/<int:taker_id>/"): "user.update",
        ("post", "api/integration/v1/takers/<int:taker_id>/enable/"): "user.status",
        ("post", "api/integration/v1/takers/<int:taker_id>/disable/"): "user.status",
        ("post", "api/integration/v1/takers/<int:taker_id>/delete/"): "user.delete",
        ("post", "api/integration/v1/takers/<int:taker_id>/restore/"): "user.restore",
    },
    "apps/openapi/api/integration_orders.py": {
        ("post", "api/integration/v1/orders/"): "order.create",
        ("patch", "api/integration/v1/orders/<order_no:order_no>/"): "order.update",
        ("post", "api/integration/v1/orders/<order_no:order_no>/status/"): "order.transition",
        ("put", "api/integration/v1/orders/<order_no:order_no>/assign-price/"): "order.change_offer_price",
        ("post", "api/integration/v1/orders/<order_no:order_no>/assign/"): "performance.assign",
        ("post", "api/integration/v1/orders/<order_no:order_no>/reassign/"): "performance.assign",
        ("post", "api/integration/v1/orders/<order_no:order_no>/publish/"): "order.throw_to_hall",
        ("post", "api/integration/v1/orders/<order_no:order_no>/withdraw/"): "order.withdraw",
        ("post", "api/integration/v1/orders/<order_no:order_no>/clear-taker/"): "order.clear_taker",
        ("post", "api/integration/v1/orders/<order_no:order_no>/delete/"): "order.delete",
        ("post", "api/integration/v1/orders/<order_no:order_no>/restore/"): "order.restore",
        ("post", "api/integration/v1/orders/<order_no:order_no>/notes/"): "order.note",
        ("post", "api/integration/v1/orders/<order_no:order_no>/split/"): "order.create",
        ("post", "api/integration/v1/orders/<order_no:order_no>/split-peer/"): "order.create",
    },
    "apps/business/api/config_views.py": {
        ("post", "api/business/games/"): "system.game_create",
        ("patch", "api/business/games/<pk>/"): "system.game_update",
        ("put", "api/business/games/<pk>/"): "system.game_update",
        ("delete", "api/business/games/<pk>/"): "system.game_destroy",
        ("post", "api/business/servers/"): "system.server_create",
        ("patch", "api/business/servers/<pk>/"): "system.server_update",
        ("put", "api/business/servers/<pk>/"): "system.server_update",
        ("delete", "api/business/servers/<pk>/"): "system.server_destroy",
        ("post", "api/business/categories/"): "category.create",
        ("patch", "api/business/categories/<pk>/"): "category.update",
        ("put", "api/business/categories/<pk>/"): "category.update",
        ("delete", "api/business/categories/<pk>/"): "category.destroy",
        ("post", "api/business/projects/"): "project.create",
        ("patch", "api/business/projects/<pk>/"): "project.update",
        ("put", "api/business/projects/<pk>/"): "project.update",
        ("delete", "api/business/projects/<pk>/"): "project.destroy",
    },
    # ⭐ 游戏 logo 上传：**认领**是写 `Game.logo_key` 的唯一入口 ⇒ 落一条行。动作码**复用** `system.game_update`（logo 是「编辑游戏」的一部分，⛔ 不新造语义重复的码——那会同时动 `ACTION_TEMPLATES` 的条数钉子，与自助注册复用 `user.create` 同一条口径）；路径是**显式 `path()` + `<int:pk>` 转换器**，拼写照归一化实测写 `<int:pk>`（不是 router 的 `<pk>`；先例见 `api/accounts/self-registration/invites/<int:pk>/revoke/`）。
    # ⚠️ `record(...)` 直接写在 `GameLogoClaimView.post` 的方法体里（与写库同一个 `transaction.atomic()`）⇒ 不必登记任何转发助手。⚠️ 同文件的**取票**端点（`…/logo/ticket`）只签发凭证、不落任何业务行，登记在下面的 `DELIBERATELY_NOT_LOGGED`（同头像 / 首页二维码两条线的取票口径）。
    "apps/business/api/game_logo_views.py": {
        ("post", "api/business/games/<int:pk>/logo"): "system.game_update",
    },
    "apps/business/api/order_views.py": {
        ("post", "api/business/orders/"): "order.create",
        ("patch", "api/business/orders/<order_no>/"): "order.update",
        # ⚠️ 这里**没有** `put`——订单与配置/账号/权限三域**刻意不同**：`OrderViewSet.http_method_names = ["get", "post", "patch", "head", "options"]`（`order_views.py:103`）把 PUT 挡成 **405**，所以 PUT 不是这条路由的写端点。实测：`PUT /api/business/orders/{20位}/` → **405**，`PATCH` 同路径 → 200；配置域 `/games/999/` 的 PUT 则 → 200。这条口径由 `test_orders_detail_put_is_blocked_by_http_method_names` 钉住。
        ("post", "api/business/orders/<order_no>/transition/"): "order.transition",
        ("post", "api/business/orders/<order_no>/throw-to-hall/"): "order.throw_to_hall",
        ("post", "api/business/orders/<order_no>/withdraw/"): "order.withdraw",
        ("post", "api/business/orders/<order_no>/clear-taker/"): "order.clear_taker",
        ("post", "api/business/orders/<order_no>/price/"): "order.change_taker_price",
        # ⭐ 改收款状态 / 改发单价：两条都是 `@action` 形态、**带**尾斜杠；拼写照实测（本项目在这条网上两次踩到带 / 不带尾斜杠，见挂账表里 `performance.settle` / `performance.export` 的注释）。
        ("post", "api/business/orders/<order_no>/payment-status/"): "order.change_payment_status",
        ("post", "api/business/orders/<order_no>/hall-price/"): "order.change_offer_price",
        # ⭐ 换接单销售客服归属：埋点落在**领域核心** `order_writes.change_sales_agent` 的方法体里（审计必须与「订单两列 + 绩效行归属 + 金额重算」在**同一个** `transaction.atomic()` 里，与 `change_price` / `assign_order` 那几条同一条结构理由）⇒ 名字已登记进 `test_operation_log_coverage._CROSS_MODULE_FORWARDERS`。
        # ⚠️ 拼写照**实测**：本端点与 `payment-status` 同为 `@action(detail=True, methods=["post"])` ⇒ 带尾斜杠（本项目在这条网上踩过两次带 / 不带尾斜杠，见挂账表里 `performance.settle` / `performance.export` 的注释）；真值以覆盖网报出的键为准。
        ("post", "api/business/orders/<order_no>/sales-agent/"): "order.change_sales_agent",
        ("post", "api/business/orders/<order_no>/submit/"): "performance.submit",
        # ⭐ 报问题单：`/report-issue/` 复用 `transition_order`（`record(...)` 就在它体内，已由 `_RECORD_FORWARDERS` 登记）⇒ 动作词与单笔 `/transition/` **同一个** `order.transition`：它**就是**一次订单状态迁移（IN_PROGRESS → ISSUE），⛔ 不新造动作码。
        # ⚠️ 拼写照**实测**（覆盖网报出的键）：`api/business/orders/<order_no>/report-issue/`。
        ("post", "api/business/orders/<order_no>/report-issue/"): "order.transition",
        ("post", "api/business/orders/<order_no>/assign/"): "performance.assign",
        ("post", "api/business/orders/<order_no:order_no>/peer-direct-assignment/"): "performance.assign",
        ("post", "api/business/orders/<order_no:order_no>/jidanpu-direct-assignment/"): "order.jidanpu_direct_assign",
        ("post", "api/business/orders/<order_no:order_no>/jidanpu-direct-assignment/verify/"): "order.jidanpu_direct_assign",
        # ⭐ 订单详情页「添加订单日志」框：它写的就是**这一张表**的一行（动作 `order.note`，新拟），所以必须登记为「做」；埋点直接落在 `OrderViewSet.notes` 的方法体里（走 `record(...)`，不借助手）。
        ("post", "api/business/orders/<order_no>/notes/"): "order.note",
        ("post", "api/business/hall/<order_no:order_no>/claim/"): "order.claim",
        # ⛔ 租户级（P2）的镜像单接单端点已随那条线拆除（原登记 `hall/remote/<source_system>/<remote_order_id>/claim/`；端点连同 `OrderViewSet.remote_claim` 随租户级鸡蛋铺线整条拆除，见 `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）⇒ 登记表**同一次**删掉这一行（「登记表里的端点必须真实存在于枚举集合里」那条腿会把它报出来）。⚠️ 动作码 `order.jidanpu_remote_claim` **仍然存在**（下面那条上家接单还在用它）。
        # ⭐ 上家鸡蛋铺线：**本线**的大厅镜像单接单。埋点动作**复用** `order.jidanpu_remote_claim`（自己那张镜像表的接单；`record(...)` 的落点在领域层 `jidanpu_upstream.promote_upstream_mirror` 的同事务里，与 P2 那条**同一个** `jidanpu_hall.ASSIGN_AUDIT_ACTION`）。⚠️ 拼写照**实测**（先跑一次覆盖网读它给出的键）：路径段用共享的 `int` 转换器 ⇒ 归一化拼写保留 `<int:pk>`。
        # ⚠️ 链路另在两处登记（`test_operation_log_coverage.py`）：跨模块 `_CROSS_MODULE_FORWARDERS["claim_upstream_mirror"]` 与同模块 `_RECORD_FORWARDERS` 里的 `promote_upstream_mirror`。
        ("post", "api/business/hall/upstream/<int:pk>/claim/"): "order.claim",
        # ⭐ 复制成新订单。显式 `path()` + 共享 `order_no` 转换器 ⇒ 归一化拼写带转换器（与 claim / refunds 同形）。埋点动作 `order.copy` 为**新拟**（对标没有这个动作，见 `apps/audit/actions.py` 的 `ORDER_COPY` 常量）。新动作两个字典都已登记，键集一致性由 `tests/test_operation_log_model.py` 钉住。
        ("post", "api/business/orders/<order_no:order_no>/copy"): "order.copy",
        # ⭐ 拆单：订单详情页的「继续拆分订单」。它是 `@action(detail=True, methods=["post"])`（router 生成、看 `lookup_value_regex` `\d{20}`）⇒ 归一化拼写**不带**转换器前缀（与 `delete` / `restore` 同形）。⚠️ 拼写照**实测**（先跑一次覆盖网读它给出的键）：`api/business/orders/<order_no>/split/`。
        # ⚠️ 动作词**复用** `order.create`：这个端点干的事就是「新增订单」（当前单改项目名那一条另记 `order.update`，在同一事务里、同一个方法体内）⇒ ⛔ 不新造动作码、⛔ 不新造权限节点（节点门 = 录单那条 `order.add_edit`）。
        ("post", "api/business/orders/<order_no>/split/"): "order.create",
        ("post", "api/business/orders/<order_no>/split-peer/"): "order.create",
        # ⭐ D-0144：软删 / 恢复。两条都是 `@action` 形态、**带**尾斜杠。⚠️ 拼写照**实测**（本项目在这条网上已三次踩到拼写）：先跑一次 `test_every_enumerated_write_endpoint_is_classified` 读它给出的实际键，再按实际值登记——此处实测为 `<order_no>`（详情路由的 `lookup_value_regex` 经归一化写成 Django 风格占位符，**不带** `:order_no` 转换器前缀）。
        ("post", "api/business/orders/<order_no>/delete/"): "order.delete",
        ("post", "api/business/orders/<order_no>/restore/"): "order.restore",
        # ⭐ 批量端点逐单走**同一个**领域核心 ⇒ 动作词与单笔**同一个**、日志逐单一行（不是一条汇总日志）。
        # 本条走 `_CROSS_MODULE_FORWARDERS` 的 `change_price` 指针解析到 `order_writes` 里那条 `record(...)`（与上一行的单笔 `/price/` 同一个落点）。
        ("post", "api/business/orders/batch/price/"): "order.change_taker_price",
        # ⭐ 另外三条批量端点：三条都走**已经登记过**的 `_CROSS_MODULE_FORWARDERS` 指针（与各自单笔同一个落点）：`change_hall_price` / `update_order_fields` / `change_payment_status` ⇒ 动作词逐条与单笔相同、日志逐单一行。⛔ 这不是放宽：把对应核心函数体里的 `record(...)` 删掉，本网照样红（解析到 `order_writes` 后走同一套判据）。
        # ⚠️ 拼写照**实测**（跑一次 `test_every_enumerated_write_endpoint_is_classified` 读它给出的键，再按实际值登记——本表的既有纪律）。
        ("post", "api/business/orders/batch/payment-status/"): "order.change_payment_status",
        ("post", "api/business/orders/batch/types/"): "order.update",
        ("post", "api/business/orders/batch/hall-price/"): "order.change_offer_price",
        # ⭐ 软删 / 改状态两条批量端点：两条都走**已经登记过**的 `_CROSS_MODULE_FORWARDERS` 指针（与各自单笔同一个落点）：`delete_order_write`（→ `recycle.soft_delete_order` 里的 `record`）与 `transition_order`（`record` 直接在它体内）⇒ 动作词与单笔**同一个**、日志**逐单一行**（不是一条汇总日志，spec §D7）。
        # ⛔ 这不是放宽：把对应核心函数体里的 `record(...)` 删掉，本网照样红（解析到 `order_writes` / `recycle` 后走 `_node_writes_log` 的同一套判据）。⚠️ 拼写照**实测**（与上面四条同一条纪律）：覆盖网报出来的键就是下面这两个。
        ("post", "api/business/orders/batch/delete/"): "order.delete",
        ("post", "api/business/orders/batch/delete-v2/"): "order.delete",
        ("post", "api/business/orders/batch/transition/"): "order.transition",
        # ⭐ 指派 / 记退款两条批量端点：两条都走**已经登记过**的 `_CROSS_MODULE_FORWARDERS` 指针（与各自单笔同一个落点）：`assign_order` 与 `refund_total_write`（后者再转 `refunds._write_refund_total`）⇒ 动作词与单笔**同一个**（`performance.assign` / `order.refund`）、日志**逐单一行**（不是一条汇总日志，spec §D7）。
        # ⛔ 这不是放宽：把对应核心函数体（或它调用的写入函数）里的 `record(...)` 删掉，本网照样红（解析到 `order_writes` / `refunds` 后走 `_node_writes_log` 的同一套判据）。⚠️ 拼写照**实测**（先跑一次覆盖网读它给出的键，再按实际值登记——本表的既有纪律）。
        ("post", "api/business/orders/batch/assign/"): "performance.assign",
        ("post", "api/business/orders/batch/refunds/"): "order.refund",
        # ⭐ 重算客服销售提成（spec §D15 / §D7）。⚠️ 这是批量端点里**唯一的新动作词**（spec §10-Q2 = 候选 A：`performance.recompute_customer_commission`，归既有 `LogType.PERFORMANCE`）——其余 8 条批量端点都复用单笔的动作词。
        # 埋点直接落在领域核心 `order_writes.recompute_customer_commission_write` 的 `record(...)` 里（与「补建 SALES 行 + 写金额」同一个 `transaction.atomic()`）⇒ 走 `_CROSS_MODULE_FORWARDERS` 的指针解析。
        # ⚠️ 拼写照**实测**（先跑一次覆盖网读它给出的键，再按实际值登记——本表的既有纪律）。
        ("post", "api/business/orders/batch/recompute-commission/"):
            "performance.recompute_customer_commission",
    },
    "apps/business/api/performance_views.py": {
        ("post", "api/business/performances/<pk>/adjust/"): "performance.adjust",
        # ⭐ D-0147：反推发单价端点，与 `/adjust/` 共用 `performance.adjust` 这个动作模板（同一件事的两个入口，不新造模板）。
        # 拼写照**实测**（先跑一次覆盖网读它给出的键）＝ `<pk>` + 尾斜杠，与本模块既有两条同形。
        ("post", "api/business/performances/<pk>/adjust-amount/"): "performance.adjust",
        ("post", "api/business/performances/<pk>/review/"): "performance.review",
        # ⭐ D-0147：垃圾桶（**软删**绩效行 + 同一步清接手人）。与上面两条同形（`<pk>` + 尾斜杠）；埋点**直接落在** action 方法体里（`record(...)` 在同一个 `transaction.atomic()` 里，不借助手 ⇒ 不走跨模块登记）。
        # ⚠️ 与既有硬删端点 `POST api/business/performances/delete`（无尾斜杠）**不是**同一条路由：那条走 `PerformanceDeleteView`，本条的路径多一层 `<pk>/`。
        ("post", "api/business/performances/<pk>/delete/"): "performance.delete",
    },
    # ── P4 结算 / 退款 / 人工修正──────────────
    # ⚠️ 这三条端点的 `record(...)` 收在**领域服务**里（`settlement.py` / `refunds.py`），视图只做翻译（「六步收在一个函数」的既有结构）⇒ 覆盖网靠 `_CROSS_MODULE_FORWARDERS` 显式登记这两个服务函数跟进（见 `test_operation_log_coverage.py` 的那个常量）。⚠️ `订单退款` 那条路径**没有尾斜杠**、且带共享 `order_no` 转换器（P4-9 的实测拼写）。
    "apps/business/api/settlement_views.py": {
        ("post", "api/business/settlements/settle"): "performance.settle",
    },
    "apps/business/api/refund_views.py": {
        (
            "post",
            "api/business/orders/<order_no:order_no>/refunds",
        ): "order.refund",
    },
    # 图片存储线：**删除**订单截图记一条（spec §9.2 的裁定：只给不可恢复的动作留痕）。⚠️ 同文件里那两个 POST（签发上传凭证 / 认领落库）**刻意不记**，登记在 `DELIBERATELY_NOT_LOGGED`。
    "apps/business/api/screenshot_views.py": {
        (
            "delete",
            "api/business/orders/screenshots/<int:pk>",
        ): "order.delete_screenshot",
    },
    "apps/business/api/adjustment_views.py": {
        (
            "post",
            "api/business/performances/<int:performance_id>/adjustments",
        ): "performance.adjust_manual",
    },
    # ── 绩效页的撤销 / 删除两个写端点──
    # ⚠️ 与 P4 结账同一形状：`record(...)` 收在领域服务 `performance_writes.py` 里（撤销要「每条绩效一行 + 每条被回退的调整记录一行」、删除要「先写后删」，都必须在同一个 `atomic()` 里）⇒ 走 `_CROSS_MODULE_FORWARDERS` 登记 `revoke_settlements` / `delete_performances` 跟进（见 `test_operation_log_coverage.py` 的那个常量）。
    "apps/business/api/performance_write_views.py": {
        ("post", "api/business/settlements/revoke"): "performance.settle",
        ("post", "api/business/performances/delete"): "performance.delete",
    },
    # ── 闲管家店铺对接的三个写端点──────────
    # ⚠️ 与 M3b 的阿奇索店铺对接**同款处置**：埋点落在 `config_views.py::ConfigAuditMixin` 的三个 DRF 钩子上，覆盖网按 `(类, 方法名)` 解析到那个 Mixin 里。`http_method_names` 关掉了 PUT ⇒ 没有 `put` 条目。
    "apps/business/api/xgj_views.py": {
        ("post", "api/business/xgj-bindings/"): "system.xgj_binding_create",
        ("patch", "api/business/xgj-bindings/<pk>/"): "system.xgj_binding_update",
        ("delete", "api/business/xgj-bindings/<pk>/"): "system.xgj_binding_destroy",
        # ⭐ per-source：手动「同步店铺」是**写端点**（落店铺行 + 来源），但它不改这一行本身 ⇒ 单独一个动作（不借 `_update`，那会是一句假陈述）。保存时那次自动同步不记（那条路自己的 create/update 已记）。
        ("post", "api/business/xgj-bindings/<pk>/sync-shops/"): "system.xgj_binding_sync_shops",
    },
    # ── 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.7）──────
    # 重试发货是**写端点**（每条落一行发货记录 `TaobaoOrderSendLog` + 一行订单日志）。⚠️ `record(...)` 收在领域模块 `apps/business/xgj_ship.py` 里（记录 + 日志必须在**同一个** `atomic()` 里，事务边界在 `_write_attempt` 内）⇒ 与 Task L / P4 同一结构理由，走 `_CROSS_MODULE_FORWARDERS` 登记 `retry_row`、并在 `_RECORD_FORWARDERS` 里登记它的两跳（见 `test_operation_log_coverage.py` 那两个常量）。
    # ⚠️ 本集合**没有** `post api/business/tb-send-logs/` 那一条：视图是 `GenericViewSet`，不生成新增 / 编辑 / 删除路由（只有只读列表 + 重试两条路，见 `urls.py` 注册处）。
    "apps/business/api/tb_send_log_views.py": {
        ("post", "api/business/tb-send-logs/retry/"): "order.ship_result",
    },
    "apps/accounts/views.py": {
        ("post", "api/accounts/"): "user.create",
        ("patch", "api/accounts/<pk>/"): "user.update",
        # 同 `orders/<order_no>/`：`AccountViewSet.update` 是 PUT 与 PATCH 的共同落点。
        ("put", "api/accounts/<pk>/"): "user.update",
        ("post", "api/accounts/<pk>/status/"): "user.status",
        ("post", "api/accounts/status/"): "user.status_batch",
        ("post", "api/accounts/<pk>/reset-password/"): "user.reset_password",
        # ⭐「删除用户」= **软删（可恢复）**。
        # 两个动作都只注册 POST（与本类的 405 边界一致，D3：不暴露硬删）。
        ("post", "api/accounts/<pk>/delete/"): "user.delete",
        ("post", "api/accounts/<pk>/restore/"): "user.restore",
        # ⭐ 实名认证：管理页那**两条写**端点。⚠️ 另两条（`GET realname-logs/` / `realname-quota/`）是**读**端点 ⇒ 不在枚举集合里（本表只收写方法）。
        # ⚠️ **没有** `patch api/accounts/<pk>/` 那一条：它与 `put` 共用 `update` 入口，又与 `api/accounts/realname-setting/` 共用 `partial_update` 入口（分派看 `self.action`）——后者那一条在下面单列。
        ("patch", "api/accounts/realname-setting/"): "user.update",
        ("post", "api/accounts/<pk>/reset-realname/"): "user.realname_reset",
    },
    # ── 自助注册线（spec `2026-10-16-self-registration-links-design.md` §3.3）──
    # ⚠️ 只有 `post register/` 一条是「做」：它建账号，埋点照 `_record_account_created` 的形状（`user.create` + 逐游戏 `user.game_permission_add`），落在 `SelfRegisterPublicView.post` 经 `_record_self_registered`（转发助手已登记进 `test_operation_log_coverage.py` 的 `_RECORD_FORWARDERS`）。⚠️ actor 是 `invite.created_by`（匿名请求没有登录态，`request.user` 是 `AnonymousUser`，`record()` 的 fail-closed 判据会当场抛 `ValueError`）——与 `opened_by` 同源，选择依据登记在助手 docstring 与 spec §8-6。另两条写端点（保存配置 / 生成链接）**刻意不记**，登记在下面的 `DELIBERATELY_NOT_LOGGED`。
    "apps/accounts/selfreg_views.py": {
        ("post", "api/accounts/self-registration/register/"): "user.create",
        # ⭐ D-0253：链接台账的「作废」端点（立即过期）——**同一次登记**（新增写端点不登记，`test_operation_log_coverage.py` 的分类网会红）。埋点直接落在 `InviteRevokeView.post` 的方法体里（同一事务内先置 `expires_at` 再 `record(...)`；三档拒绝路径写前 return、不留痕）。动作码 `user.selfreg_revoke`（**新拟**：对标没有「注册链接」这个功能、没有模板可照抄——模板与槽位纪律见 `apps/audit/actions.py::USER_SELFREG_REVOKE` 常量处）。
        #    ⚠️ 键的占位符照归一化实测写 **`<int:pk>`**：显式 `path()` 的转换器不去掉（`_normalise` 只把 `(?P<名>…)` 正则组归成 `<名>`；router 的 `<pk>` 才是那形状，先例见 `api/taker/apps/<int:pk>/deliveries/`——写成 `<pk>` 两张网同时红：分类网报「未分类」、埋点网报「声称已埋点却找不到」）。
        ("post", "api/accounts/self-registration/invites/<int:pk>/revoke/"): "user.selfreg_revoke",
    },
    # ── 实名认证线（T8）：用户**自己**提交一次核验 ───────────
    # ⚠️ 只有 `post` 一条：`GET api/auth/realname/` 是**读**端点，不在枚举集合里。埋点落在 `RealNameVerifyView.post` 的方法体里（位置在 `services.verify_for_user` **之后**：只有真的落了流水才留痕；被 1025/1026/1027 拒掉的那几档在上面就 return 了）。
    "apps/realname/views.py": {
        ("post", "api/auth/realname/verify/"): "user.realname_verify",
    },
    # ── 我的资料线：自助改资料端点 ──────────────────────────
    # ⚠️ 只有 `patch` 一条：`GET api/auth/profile` 是**读**端点，不在枚举集合里。埋点直接落在 `MyProfileView.patch` 的方法体里（同一处 `atomic()` 内写一行 `user.self_update`，且**只在确有字段变化时**写——同值重提交不留痕）。
    "apps/core/profile_views.py": {
        ("patch", "api/auth/profile"): "user.self_update",
    },
    "apps/rbac/views.py": {
        ("post", "api/role-authorities/"): "system.role_create",
        ("patch", "api/role-authorities/<pk>/"): "system.role_update",
        ("put", "api/role-authorities/<pk>/"): "system.role_update",
        ("delete", "api/role-authorities/<pk>/"): "system.role_destroy",
        ("post", "api/role-authorities/<pk>/copy/"): "system.role_copy",
    },
    # ── 订单来源 / 店铺绑定两个域（埋点落在 ConfigAuditMixin）──────
    # ⚠️ 两者都**自己没有** `create` / `update` / `destroy`：埋点落在 `apps/business/api/config_views.py::ConfigAuditMixin` 的三个 DRF 钩子上（`perform_create` / `perform_update` / `perform_destroy`），覆盖网按 `(类, 方法名)` 解析到那个 Mixin 里，再跟到 `_record_saved` / `_record_config` → `record(...)` ⇒ 有人把某个 ViewSet 的 `audit_actions` 撤掉、或把 Mixin 去掉 ⇒ 该端点落到差集里变红。
    # ⚠️ `order-sources` **允许 PUT**（4 个写端点）；`shop-bindings` 的 `http_method_names` 关掉了 PUT（3 个写端点，`PUT → 405` 有单测），所以这里**没有** `put` 条目。
    "apps/business/api/source_views.py": {
        ("post", "api/business/order-sources/"): "system.order_source_create",
        ("put", "api/business/order-sources/<pk>/"): "system.order_source_update",
        ("patch", "api/business/order-sources/<pk>/"): "system.order_source_update",
        ("delete", "api/business/order-sources/<pk>/"): "system.order_source_destroy",
    },
    # ⭐ 公司店铺分组（计划 `2026-10-26-order-source-groups.md` §3.2）：与新文件 `source_group_views.py` 同一次登记——4 条写端点（含 PUT），埋点落 `ConfigAuditMixin` 的三个 DRF 钩子（与 `source_views.py` 同款解析路径）。
    "apps/business/api/source_group_views.py": {
        ("post", "api/business/order-source-groups/"): "system.order_source_group_create",
        ("put", "api/business/order-source-groups/<pk>/"): "system.order_source_group_update",
        ("patch", "api/business/order-source-groups/<pk>/"): "system.order_source_group_update",
        ("delete", "api/business/order-source-groups/<pk>/"): "system.order_source_group_destroy",
    },
    "apps/business/api/shop_binding_views.py": {
        ("post", "api/business/shop-bindings/"): "system.shop_binding_create",
        ("patch", "api/business/shop-bindings/<pk>/"): "system.shop_binding_update",
        ("delete", "api/business/shop-bindings/<pk>/"): "system.shop_binding_destroy",
    },
    # ── 录单识别设置：改一行的关键词（`system.*` 新动作）────────
    # ⚠️ **拼写照实测**（本项目在这条网上已三次踩到拼写）：显式 `path("recognition-settings/<str:item_code>/")` 归一化后写作 `<str:item_code>`（**不是**计划里预测的 `<item_code>`）；判据里的归一化器只把 `(?P<名>…)` 那类正则占位写成 `<名>`，路径转换器原样保留。⚠️ 只有 `patch` 一条：本页没有增删（`path()` 形态），GET 不是写方法。
    "apps/business/api/alipay_authorization_views.py": {
        ("post", "api/business/alipay-authorizations/"): "finance.alipay_authorization",
        ("post", "api/business/alipay-authorizations/<int:pk>/authorize/"): "finance.alipay_authorization",
        ("post", "api/business/alipay-authorizations/<int:pk>/refresh/"): "finance.alipay_authorization",
        ("post", "api/business/alipay-authorizations/<int:pk>/identity/"): "finance.alipay_authorization",
        ("post", "api/business/alipay-authorizations/<int:pk>/identity-refresh/"): "finance.alipay_authorization",
        ("post", "api/business/alipay-authorizations/<int:pk>/revoke/"): "finance.alipay_authorization",
    },
    "apps/business/api/online_payroll_views.py": {
        ("post", "api/business/online-payroll/batches/"): "finance.online_payroll",
        ("post", "api/business/online-payroll/batches/<uuid:batch_id>/cancel/"): "finance.online_payroll",
    },
    "apps/business/api/recognition_views.py": {
        (
            "patch",
            "api/business/recognition-settings/<str:item_code>/",
        ): "system.recognition_keywords_update",
    },
    # ── 系统开关（D-0223）：改一个系统设置开关（`system.*` 新动作）────
    # ⚠️ 与 recognition 同一条注记：显式 `path("system-switches/<str:key>/")` 归一化后写作 `<str:key>`；只有 `patch` 一条（GET 不是写方法，本资源无增删）。
    "apps/business/api/system_switch_views.py": {
        (
            "patch",
            "api/business/system-switches/<str:key>/",
        ): "system.system_switch_update",
    },
    # ── 马上来查码配置（D-0348）：单行配置的保存（PATCH）────────────
    # ⚠️ 只有 `patch` 一条：本资源只有 get/patch，其余 405（有单测）。⚠️ `record(...)` 在视图 `patch` 方法体里（config save 之后；改的是凭据与启停——照 QQ 机器人设置页的口径必须记台账，⛔ 不进「刻意不记」）。GET 是读，不进本表。
    "apps/business/api/order_qr_config_views.py": {
        (
            "patch",
            "api/business/order-qr/config/",
        ): "system.order_qr_config_update",
    },
    # ── 交易平台对接设置：账号三条写路径 + 全局设置保存 ─────────
    # ⚠️ 账号集合走 router ⇒ 归一化后是 `<pk>`（`lookup_value_regex` 只认数字，但归一化不保留具体取值）；`PUT settings/` 是显式 `path()` 的单例资源。⚠️ 四个动作的 `record(...)` 都在视图方法体里（`perform_create` / `perform_update` / `perform_destroy` / settings 的 `put`），且与业务写入同一个 `transaction.atomic()`。
    "apps/business/api/platform_integration_views.py": {
        (
            "post",
            "api/business/platform-integrations/",
        ): "system.platform_integration_create",
        (
            "patch",
            "api/business/platform-integrations/<pk>/",
        ): "system.platform_integration_update",
        (
            "delete",
            "api/business/platform-integrations/<pk>/",
        ): "system.platform_integration_destroy",
        (
            "put",
            "api/business/platform-integrations/settings/",
        ): "system.platform_settings_update",
    },
    # ── 交易平台自动回复：规则 CRUD 三条写路径 ───────────
    # ⚠️ 集合走 router ⇒ 归一化后是 `<pk>`；三个动作的 `record(...)` 都在视图方法体里（`perform_create` / `perform_update` / `perform_destroy`），与业务写入同一事务。
    "apps/business/api/approval_views.py": {
        ("patch", "api/business/approvals/settings/"): "system.approval_settings_update",
        ("post", "api/business/approvals/<int:pk>/decision/"): "order.penalty_approve",
    },
    "apps/business/api/platform_auto_reply_views.py": {
        (
            "post",
            "api/business/platform-auto-replies/",
        ): "system.platform_auto_reply_create",
        (
            "patch",
            "api/business/platform-auto-replies/<pk>/",
        ): "system.platform_auto_reply_update",
        (
            "delete",
            "api/business/platform-auto-replies/<pk>/",
        ): "system.platform_auto_reply_destroy",
        # ⭐ 规则图片上传：router 的 `@action(detail=False, url_path="upload-image")` ⇒ 归一化路径是集合下的字面量段（不是 `<pk>`）。它的 `record(...)` 写在同一模块的 `upload_image` 方法体里，**不写规则行**。
        (
            "post",
            "api/business/platform-auto-replies/upload-image/",
        ): "system.platform_auto_reply_image_upload",
    },
    # ── 代练通抛单：真发单一条写路径 ───────────────────
    # ⚠️ 显式 `path()` + `<order_no>` 转换器 ⇒ 归一化拼写**原样保留转换器**（与 `hall/<order_no:order_no>/claim/`、`orders/<order_no:order_no>/refunds` 同形；⛔ 不是 `<order_no>`）。⚠️ `record(...)` 在**领域层** `apps.business.dailiantong_push._record_push`（前置闸 / 上游结果 / 落库都在那个模块；视图只取订单 + 调服务）⇒ `push_order` 已登记进 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`（跟到服务层那次 `record`），覆盖网据此认证本端点。
    # ⚠️ `GET platform-push/` 与 `GET zones/` 是读端点，按本表口径不需要登记。
    "apps/business/api/dailiantong_push_views.py": {
        (
            "post",
            "api/business/orders/<order_no:order_no>/push-to-platform/",
        ): "order.dailiantong_push",
    },
    # ── 代练通平台动作：撤回 / 改价 / 补时 / 留言四条写路径 ──────────────────
    # 撤回 / 改价 / 补时 / 留言各一条；路径同样带 `<order_no>` 转换器 ⇒ 归一化拼写**原样保留转换器**（同上）。
    # ⚠️ `record(...)` 在**领域层** `apps.business.dailiantong_actions._record_action`（前置闸 / 上游调用 / 回读 / 审计都在那个模块；视图只取订单 + 调服务）⇒ 四个服务函数已登记进 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`，`_record_action` 登记在同模块的 `_RECORD_FORWARDERS`（跟到那次 `record`），覆盖网据此认证四个端点。
    "apps/business/api/dailiantong_action_views.py": {
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/withdraw/",
        ): "order.dailiantong_withdraw",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/price/",
        ): "order.dailiantong_price",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/time/",
        ): "order.dailiantong_time",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/message/",
        ): "order.dailiantong_message",
        # ⭐ 详情页补充动作：确认完单（复用 `accept_push`）/ 重发（`repush_failed_push`）/ 从所有平台撤回（`withdraw_all_platforms`）。三个服务函数都在 `apps.business.dailiantong_actions`，已登记进覆盖网的 `_CROSS_MODULE_FORWARDERS`。
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/finish/",
        ): "order.dailiantong_accept",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/repush/",
        ): "order.dailiantong_repush",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-pushes/withdraw-all/",
        ): "order.dailiantong_withdraw",
    },
    # ── 第三方平台订单管理页的工具栏动作：一个端点多个动作 ──
    # ⚠️ **一个端点对应多个动作码**（本表的数据形状装不下）：登记的是**主路径** `order.dailiantong_withdraw`（批量撤回），其余动作码见实现：改成系统状态 ⇒ `order.transition`、记退款 ⇒ `order.refund`、标签 / 标记 / 加急 ⇒ `order.update`、改接单价 ⇒ `order.change_taker_price`、仲裁（D 线未就绪时的本地标记）⇒ `order.note`、确认完单 ⇒ `order.dailiantong_accept`、同步交易状态 ⇒ `order.dailiantong_sync`。先例：`system.jidanpu_binding_update` 那条 PUT 也是一个方法两个动作、只登记一路。
    # ⚠️ `record(...)` 的落点：① 平台动作在领域层 `dailiantong_actions`（`_record_action`）；② 本地动作在 `order_writes` 的既有核心（`transition_order` / `refund_total_write` / `change_price` / `update_order_fields`）；③ 仲裁的本地标记在**本视图模块**的 `_run_platform_push_action` 里直接 `record(...)` ⇒ `_run_platform_push_action` 已登记进 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`，覆盖网据此认证本端点。
    "apps/business/api/platform_push_action_views.py": {
        (
            "post",
            "api/business/platform-pushes/actions/",
        ): "order.dailiantong_withdraw",
    },
    # ── 代练通「有、我们没用上」的动作：十条写路径 ──────
    # 订单六条（加价 / 客服介入 / 评价 / 上下架 / 锁单 / 拉黑）+ 代练通侧模板四条（新增 / 修改 / 删除 / 设默认）。
    # ⚠️ `record(...)` 在**领域层** `apps.business.dailiantong_extra_actions`：订单六条经 `_record_action`、模板四条经 `_record_template_action`（两个助手都在 `_RECORD_FORWARDERS` 里）；服务函数登记在 `_CROSS_MODULE_FORWARDERS`。⚠️ 订单六条的路径带 `<order_no>` 转换器 ⇒ 归一化拼写**原样保留转换器**；模板明细的路径按实测写 `<int:temp_id>`。
    "apps/business/api/dailiantong_extra_views.py": {
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/up-price/",
        ): "order.dailiantong_up_price",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/arbitration/",
        ): "order.dailiantong_arbitration",
        # ⭐ 同意撤销（打手申请撤销后我方同意）。
        #    `record(...)` 同样经领域层 `agree_cancel` → `_record_action`。
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/agree-cancel/",
        ): "order.dailiantong_agree_cancel",
        # ⭐ 取消撤销（复用 withdraw 动作码；领域层 `cancel_withdraw_request`）。
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/cancel-request/",
        ): "order.dailiantong_withdraw",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/value/",
        ): "order.dailiantong_value",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/off-sell/",
        ): "order.dailiantong_off_sell",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/lock/",
        ): "order.dailiantong_lock",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/blacklist/",
        ): "order.dailiantong_blacklist",
        (
            "post",
            "api/business/platform-integrations/order-templates/",
        ): "order.dailiantong_template",
        (
            "patch",
            "api/business/platform-integrations/order-templates/<int:temp_id>/",
        ): "order.dailiantong_template",
        (
            "delete",
            "api/business/platform-integrations/order-templates/<int:temp_id>/",
        ): "order.dailiantong_template",
        (
            "post",
            "api/business/platform-integrations/order-templates/<int:temp_id>/default/",
        ): "order.dailiantong_template",
        # ⭐ 详情页补充动作：补款 / 编辑平台单（只非金额字段）/
        #   追加进度截图。三个服务函数在 `apps.business.dailiantong_extra_actions`
        #   （`_record_action` 已登记），覆盖网据此认证。
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/amount/",
        ): "order.dailiantong_amount",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/edit/",
        ): "order.dailiantong_edit",
        # ⚠️ 同一个视图的第二个拼写（前端两条线分别报过 `edit/` 与 `modify/`）：
        #    ⛔ 不是重复登记——枚举按路径去重，两条路径是两个真实端点。
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/modify/",
        ): "order.dailiantong_edit",
        (
            "post",
            "api/business/orders/<order_no:order_no>/platform-push/screenshot/",
        ): "order.dailiantong_progress",
    },
    # ── 租户级鸡蛋铺绑定（已拆除）＋ 下游 / 上家个人绑定──────────
    # ⚠️ 只有 `put` 一条：显式 `path()`、无 router 的集合形态，也**没有** POST / PATCH / DELETE（`JidanpuBindingView` 只定义 get/put，其余 405，有单测）。**这条 PUT 有两个分支两个动作**：没有行 ⇒ `system.jidanpu_binding_create`、已有行 ⇒ `system.jidanpu_binding_update`；本表形状 `(方法, 路径) -> 一个 action` **装不下**一个方法两个动作，登记 **update** 那一支（常规路径），`_create` 那一支由 `tests/test_jidanpu_binding.py` 的 `test_the_put_endpoint_writes_both_declared_audit_actions` **直接钉住**（本行字面值与视图真写 action 绑在一起：任一侧漂移都红）。⛔ 租户级 `jidanpu_views.py` / `jidanpu-binding/` 那一条（连同动作码）已随租户级鸡蛋铺线整条拆除、登记**同一次**删掉（⛔ 不是放宽：本表仍是「指针表」，只是指针指的端点已不存在；下面的个人绑定两条还在）。
    # ── 下游鸡蛋铺线：代练员**个人**绑定（「我的资料 → 功能设置」）── ⚠️ 只有 `put` 一条：显式 `path()`、无 router，也**没有** POST / PATCH / DELETE（`JidanpuMemberBindingView` 只定义 get/put，其余 405）。**这条 PUT 有两个分支两个动作**（与上面租户级绑定同形）：没有行 ⇒ `system.jidanpu_member_binding_create`、已有行 ⇒ `..._update`；本表装不下一个方法两个动作，登记 **update** 那一支，`_create` 那一支由 `tests/test_jidanpu_member_api.py` 的 `test_the_ledger_declares_the_update_action_the_view_actually_writes` **直接钉住**（字面值与视图真写 action 绑在一起：任一侧漂移都红）。⚠️ `test-connection` 是**只读上游、不落库** ⇒ 见下面「刻意不记」。# ⭐ 「立即同步」端点：**不是**只读端点——这一跳真的改我方订单状态（spec §6.2 状态回拉）。留痕落在**领域层** `jidanpu_member.pull_statuses` → `_pull_statuses` → `apply_remote_completion`（「判 → 写」整段抽进最后那一跳，`record(...)` 在它体内；端点只做节流与翻译）⇒ 覆盖网靠 `test_operation_log_coverage._CROSS_MODULE_FORWARDERS` 登记**链上那三个名字**跟进。动作码复用 `order.transition`（**就是**一次订单状态迁移，与 PUT `/transition/`、T7 自动接手同为机器操作人写），⛔ 不为「机器点的」另造只有它会写的动作（同一件事两套文案必然漂移）；操作人 = 机器名（`actor_id` 留空）。
    "apps/business/api/jidanpu_member_views.py": {
        ("put", "api/business/member/jidanpu-binding/"):
            "system.jidanpu_member_binding_update",
        ("post", "api/business/member/jidanpu-sync-now/"): "order.transition",
    },
    # ── 上家鸡蛋铺线：客服**个人**绑定（「我的资料 → 功能设置」）──
    # ⚠️ 只有 `put` 一条：本资源走**显式 `path()`**、没有 router 的集合形态，也**没有** POST / PATCH / DELETE（`JidanpuUpstreamBindingView` 只定义 get/put，其余 405）。⚠️ **这条 PUT 有两个分支两个动作**（与上面两条绑定同形）：没有行 ⇒ `system.jidanpu_upstream_binding_create`、已有行 ⇒ `..._update`；本表装不下一个方法两个动作，这里登记 **update** 那一支（本资源的常规路径）；`_create` 那一支由 `tests/test_jidanpu_upstream_api.py` 的 `test_the_ledger_declares_the_update_action_the_view_actually_writes` **直接钉住**。⚠️ `test-connection` 是**只读上游、不落库** ⇒ 见下面「刻意不记」。
    # ⭐ 「立即同步」端点：与反方向线的同一条同形——留痕落在**领域层** `jidanpu_upstream.pull_statuses`，动作码复用 `order.transition`（这**就是**一次订单状态迁移，操作人 = 机器名、`actor_id` 留空）。⚠️ 覆盖网靠 `test_operation_log_coverage._CROSS_MODULE_FORWARDERS` 的 `pull_statuses` 名字解析跟进（那个名字已登记、指向反方向线；名字一旦登记就不再校验调用点模块——这是那个常量的既有边界，见它的说明）。另有 AST 判据钉住「视图调的就是 `jidanpu_upstream.pull_statuses`」（`test_the_endpoint_calls_the_slow_path_function_itself`：它按 AST 同时钉**接收者模块**与**调用点在 `JidanpuUpstreamSyncNowView.post` 里**，⛔ 不是只收集 `.attr` 那个可被同名函数骗过的旧写法——按 AST 同时钉两端）。
    "apps/business/api/jidanpu_upstream_views.py": {
        ("put", "api/business/member/upstream-jidanpu-binding/"):
            "system.jidanpu_upstream_binding_update",
        ("post", "api/business/member/upstream-jidanpu-sync-now/"): "order.transition",
    },
    # ── 上下游订单互通线：**页面**侧的绑定 / 解绑 / 改大厅两格 ──
    # ⚠️ 三条都是 `APIView`（没有 router 的 `actions`）⇒ 覆盖网按「类上有没有 `post` / `put` / `delete`」判可达方法，`record(...)` **直接落在方法体里**（不走跨模块登记）。⚠️ **拼写照实测**（本项目在这条网上已多次踩到拼写，见同文件其它条目）：明细路由用的是 Django 的 `path()` 转换器 `<int:pk>`，覆盖网的归一化器只把 `(?P<名>…)` 那类正则占位写成 `<名>`、**路径转换器原样保留** ⇒ 这里逐字写 `<int:pk>`（不是 `<pk>`）。实测：写成 `<pk>` 时 `test_instrumented_endpoints_really_call_record` 会报「端点不在枚举集合里」。
    # ⚠️ 两个**机器同步端点**（`peer-sync/bind/` / `unbind/`）不在本表：它们是「机器对机器、没有页面操作人」，登记在下面的 `DELIBERATELY_NOT_LOGGED`。
    "apps/business/api/peer_views.py": {
        ("post", "api/business/peer-bindings/"): "system.peer_binding_create",
        # ⭐ 改「同步上家抢单大厅」/「大厅价格百分比」两格（**新拟**动作码；`record(...)` 在 `put` 方法体里、与 `row.save()` 同一个 `atomic()` ⇒ 同模块 `_RECORD_FORWARDERS` 不需要新条目）。
        ("put", "api/business/peer-bindings/<int:pk>/"): "system.peer_binding_update",
        ("delete", "api/business/peer-bindings/<int:pk>/"): "system.peer_binding_delete",
        # ⭐⭐ **接收端点**从 `DELIBERATELY_NOT_LOGGED` 挪进本表：业务方要求订单详情能看到同步来源与动作 ⇒ 它现在**真的**经 `peer_inbound.apply_events` / `apply_upstream_events` 写订单日志（落在领域层、与订单写入同一个 `atomic()`，⛔ 不搬进视图）。⚠️ 动作码取 `peer.inbound_order` 这一条代表码：同一端点还写 `peer.inbound_assign/grab/update/screenshot`、`peer.hall_claim`、`peer.upstream_status/screenshot`；本表的判据是「端点名下**至少一条**链落到 `record(`」（见 coverage 网 `_RECORD_FORWARDERS` 的说明），所以这里只登记一条代表码，⛔ 不是「只写了这一条」。
        ("post", "api/business/peer-sync/inbound/events/"): "peer.inbound_order",
        ("post", "api/business/peer-sync/direct-taker/"): "peer.inbound_order",
    },
    # ── 绑定上家系统设置页收口（计划 `2026-10-04-upstream-binding-list-rework.md`）──
    # ⚠️ 只登记**写**端点：同一模块里的 `UpstreamBindingListView` **只有 `get`**（纯读，不落库）⇒ 它不进本表（读方法不是本表的取集口径，见模块 docstring）；行级 `GET`（编辑弹窗的回填，D-0256）同理不进。⚠️ `record(...)` 就在 `delete` 方法体里、与 `row.delete()` 同一个 `atomic()` ⇒ 同模块 `_RECORD_FORWARDERS` 不需要新条目。
    # ⭐ E-0255 / D-0256：绑定改**多行**资源，行级创建 / 编辑两条写端点。两支的 `record(...)` 都住在**共用的**落库+留痕实现 `jidanpu_upstream_views._persist_binding_with_audit` 里（⛔ 不抄第二份判据）⇒ 那一跳登记在 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`；动作码沿用既有那对 `_create` / `_update`（留痕 target 指到行）。
    "apps/business/api/upstream_binding_views.py": {
        ("post", "api/business/upstream-bindings/jidanpu/"):
            "system.jidanpu_upstream_binding_create",
        ("put", "api/business/upstream-bindings/jidanpu/<int:pk>/"):
            "system.jidanpu_upstream_binding_update",
        ("delete", "api/business/upstream-bindings/jidanpu/<int:pk>/"):
            "system.jidanpu_upstream_binding_delete",
    },
    # ── 闲管家推送接收端（spec `2026-10-04-xgj-push-inbound-design.md` §7-8）──
    # ⚠️ 只有**订单**那条在这里：它是**写**端点（按推送把单改成已退单 + 两条订单日志）。路由是**只收 POST 的字面 `path()`**（`xgj/push/order/`，带尾斜杠）。⚠️ 登记的动作码取 `order.xgj_push_refund`：同一条路径还写既有的 `order.transition`（状态事实）、已结账那一档写 `..._refund_settled`（spec §D5 / §D6）——本表一格只能放一个 code，取最能指认这一条端点的那个。
    # ⚠️ `record(...)` 不在视图里（视图只做收口与转发）：它在领域层 `apps/business/xgj_push.py::handle_order_push` 里、与状态写入**同一个** `atomic()` ⇒ 那一跳登记在 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`。
    "apps/business/api/xgj_push_views.py": {
        ("post", "api/business/xgj/push/order/"): "order.xgj_push_refund",
    },
    # ── 开放平台应用管理─────────────────────────────────────
    # ⚠️ 四条写端点，**全是 `APIView`**（本模块没有 router 的 `actions`）⇒ 覆盖网按「类上有没有 `post` / `patch` / `put`」判可达方法，`record(...)` **直接落在方法体里**（不走跨模块登记）。⚠️ **拼写照实测**（本项目在这条网上多次踩到拼写）：明细路由用 Django 的 `path()` 转换器 `<int:pk>`，而覆盖网的归一化器只把 `(?P<名>…)` 那类正则占位写成 `<名>`、**路径转换器原样保留** ⇒ 这里逐字写 `<int:pk>`（⛔ 不是 `<pk>`）。先例：上面 `apps/business/api/peer_views.py` 那两条的注释（写错时 `test_instrumented_endpoints_really_call_record` 会报「端点不在枚举集合里」）。
    # ⚠️ `PATCH` 一条**一个请求可能写多行**（每个真正变化的字段一行，动作码同一个）；本表一格只放一个 code，取的就是那一个。纯 no-op（`PATCH {}` / 重发同值）**一行都不写**（R26 口径），由 `tests/test_openapi_app_api.py` 钉住。
    "apps/openapi/api/app_views.py": {
        ("post", "api/open/apps/"): "system.open_api_app_create",
        ("patch", "api/open/apps/<int:pk>/"): "system.open_api_app_update",
        # ⛔ 权限设置 / 游戏范围两个 `PUT` 已随 v1 面删除（设计文档 §13.1），登记同一次去掉——本表与真实枚举集合是**恰好**对拍（留一条已不存在的端点 ⇒ 覆盖网当场红）。
        ("post", "api/open/apps/<int:pk>/rotate-secret/"):
            "system.open_api_app_rotate_secret",
        # ⭐ T9a：推送台账的**手工重投**（设计文档 §7.4 的「可手工重投」那一半）。⚠️ 台账的**读**端点（`GET .../deliveries/`）**不在这张表里**：本表的枚举只取写方法（`GET` / `HEAD` / `OPTIONS` 不进 `INSTRUMENTED`，见本模块 docstring）。⚠️ 两个转换器（`<int:pk>` 与 `<int:delivery_pk>`）**逐字**写（归一化器保留转换器模板）。
        ("post", "api/open/apps/<int:pk>/deliveries/<int:delivery_pk>/retry/"):
            "system.open_api_delivery_retry",
    },
    # ── 开放平台对外 API v2 的**写端点**（设计文档 §6.1 / §6.3）──────────
    # ⚠️ 两条都是 `APIView` 上的 HTTP 方法名处理器（`OrderStatusChangeMixin.post` / `OrderPriceAssignView.put`）⇒ 覆盖网按「类上有没有 `post` / `put`」判可达方法。⚠️ **`record(...)` 不在视图里**：两条写路径都把动作体交给领域核心 `apps/business/order_writes.py`（`transition_order` / `change_hall_price`），留痕与业务写入在**同一个** `transaction.atomic()` 里 ⇒ 那一跳登记在 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`（两条**都已在**：它们本来就是内网 `/transition/` 与 `/hall-price/` 的收口函数）。
    # ⚠️ **拼写照实测**：两条路由都用 Django 的 `path()` 转换器 `<order_no:order_no>`，而覆盖网的归一化器只把 `(?P<名>…)` 那类正则占位写成 `<名>`、**路径转换器原样保留** ⇒ 这里逐字写 `<order_no:order_no>`（先例 = 上面那两条 `<int:pk>` 的注释；写错时 `test_instrumented_endpoints_really_call_record` 会报「端点不在枚举集合里」）。⚠️ `POST .../status/` 与 T4 的**只读** `GET .../status/` **同一条路径**（§6.1 的两条契约共用一条路由、两个方法挂在同一个视图类上）⇒ 那条路径因此**必须**从 `tests/test_operation_log_coverage.py` 的只读反向钉子（`MUST_HAVE_NO_WRITE_METHOD`）里移走，并由下面这一格**接住**它的写属性（⛔ 不许让它既不在只读表、也不在写表里）；两条写端点的动作码取**领域核心**真正写下的那两个（与内网同一条路径同源）。
    "apps/openapi/api/order_write_views.py": {
        ("post", "api/open/v1/orders/<order_no:order_no>/status/"): "order.transition",
        ("put", "api/open/v1/orders/<order_no:order_no>/assign-price/"):
            "order.change_offer_price",
        # ⭐ 开放平台对外 API v2 的**改单**端点（设计文档 §6.1 / §6.3）——它是一条 mixin（`OrderUpdateMixin.patch`，宿主是 `order_query_views.OrderDetailView`：**详情**那条路径上 GET/PATCH 同住一个类，理由 = Django 路由先匹配先赢，见那个 mixin 的 docstring）。⚠️ **拼写照实测**：路由用 `path()` 转换器 `<order_no:order_no>`，覆盖网的归一化器**原样保留**转换器 ⇒ 这里逐字写 `<order_no:order_no>`。
        #    ⚠️ **`record(...)` 不在视图里**：动作体（含留痕）在领域核心 `apps/business/order_writes.py::update_order_fields` 里（内网单笔 PATCH 与批量 `batch/types` 调的是**同一个函数**）⇒ 那一跳登记在 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`（`update_order_fields` **本来就在**：它是内网 `partial_update` 的收口函数）。本格的动作码取它写下的那一条（与内网改单同一个 `order.update`）。
        ("patch", "api/open/v1/orders/<order_no:order_no>/"): "order.update",
    },
    # ── 开放平台对外 API v2 的**录单**端点（设计文档 §6.1 / §13.14-3）──
    # ⚠️ 它是 `OrderCreateMixin.post`（一条 **mixin**，宿主是 `order_query_views.OrderSourceQueryView`——那条路径上 GET/POST 同住一个类）⇒ 覆盖网按「类上有没有 `post`」判可达方法。⚠️ **拼写照实测**：这条路由**不带**路径转换器（就是 `v1/orders/`）⇒ 归一化路径逐字是 `api/open/v1/orders/`（带尾斜杠）。
    # ⚠️ `record(...)` **不在视图里**：录单的动作体（含留痕）在领域核心 `apps/business/order_intake.py::create_order` 里（内部录单调的是**同一个函数**）⇒ 那一跳登记在 `tests/test_operation_log_coverage.py` 的 `_CROSS_MODULE_FORWARDERS`（`create_order` → `apps.business.order_intake`）。本格的动作码取它写下的那一条（与内部录单同一个 `order.create`）。
    "apps/openapi/api/order_create_views.py": {
        ("post", "api/open/v1/orders/"): "order.create",
    },
    # ── 问题反馈线（spec `2026-10-08-feedback-design.md` §8）─────────────
    # ⚠️ 四条端点里**只有一条**记：管理员处置（`PATCH`）。另外两条写端点（提交 / 签发票据）登记在下面的 `DELIBERATELY_NOT_LOGGED`，理由逐条写在那一节。列表那条是 `GET`，本来就不在这张网的取集口径里。⚠️ `record(...)` **直接落在** `patch` 方法体里（本模块不借助手、不走跨模块登记）。
    # ⚠️ **拼写照实测**：明细路由用 Django 的 `path()` 转换器 `<int:pk>`，覆盖网的归一化器只把 `(?P<名>…)` 那类正则占位写成 `<名>`、**路径转换器原样保留** ⇒ 这里逐字写 `<int:pk>`（⛔ 不是 `<pk>`）。先例见上面 `peer_views` / `app_views` 两处。
    "apps/business/api/feedback_views.py": {
        ("patch", "api/business/feedback/<int:pk>/"): "feedback.handle",
    },
    # ── 企微群码**锁定应用**的单例管理（设计文档 §7.3 / D7）────────────
    # ⚠️ **三条路由、但只有两条写端点**：`GET`（单例读）与 `GET …/deliveries/` 是读，按本表的取集口径（只取写方法）根本不进来；`PATCH wecom-qr-app/` 那一格**也不在本表**——它与 `POST` **共用同一条路径**，而枚举的 key 是 `(方法, 路径)` ⇒ 下面那两格的路径**不带** pk（单例形状，D7），`PATCH` 那条与 `POST` 那条**只差方法名**，本表一格只能放一个 code ⇒ 两个都登记（两行）。
    # ⚠️ `record(...)` **不在这两条写路径的视图方法里**：它们调本模块那个**模块级**直通函数 `_record_app`，后者把活交给 `apps/openapi/api/app_views.py` 的 `ApiAppAdminView._record_app`（**唯一**一份形状，已登记在覆盖网的 `_RECORD_FORWARDERS` + `_CROSS_MODULE_FORWARDERS` 里）⇒ 覆盖网按名字解析落到那里、再进它的方法体找 `record(`。逐条理由见 `WecomQrAppAdminView` 的类 docstring。⚠️ 动作码**复用**既有那三个（⛔ 不新增动作码——那会把「六处登记」的改动面一起放大）：创建走 `system.open_api_app_create`、轮换走 `system.open_api_app_rotate_secret`；**拼写照实测**：这两条路由**不带**路径转换器（单例形状）⇒ 归一化路径就是 `api/open/wecom-qr-app/` 与 `api/open/wecom-qr-app/rotate-secret/`（都带尾斜杠）。
    "apps/openapi/api/wecom_qr_app_views.py": {
        ("post", "api/open/wecom-qr-app/"): "system.open_api_app_create",
        ("patch", "api/open/wecom-qr-app/"): "system.open_api_app_update",
        ("post", "api/open/wecom-qr-app/rotate-secret/"):
            "system.open_api_app_rotate_secret",
    },
    # ── 打手侧开放接口线的**内部应用管理**端点 ────────────────────
    # spec `docs/specs/2026-10-08-taker-open-api-design.md` §6 / §8。⚠️ 三条写端点、**全是没有 router 的 `APIView`**（本模块的 `urlpatterns` 全是显式 `path()`，⛔ 一个 router 都不建——理由见 `apps/takerapi/urls.py` 的模块 docstring）⇒ 覆盖网按「类上有没有 `post` / `patch`」判可达方法。⚠️ `record(...)` **不在视图方法里**：三条都经**本模块自己的**埋点助手 `_record_app` 调 `record(...)`（形状照 `apps/openapi/api/app_views.py`；该助手名已登记在 `test_operation_log_coverage.py` 的 `_RECORD_FORWARDERS` 里，而它**按被走模块自己的 AST 解析** ⇒ 本模块这份助手是真的被走了一遍，⛔ 不是"名字撞上了就算"）。
    # ⚠️ **拼写照实测**：两条明细路由用 Django 的 `path()` 转换器 `<int:pk>`，而覆盖网的归一化器只把 `(?P<名>…)` 那类正则占位写成 `<名>`、**路径转换器原样保留** ⇒ 这里逐字写 `<int:pk>`（⛔ 不是 `<pk>`）。先例见上面 peer_views / app_views / feedback 三处。⚠️ 集合那条（`api/taker/apps/`）**同时**收 `GET`（列表）与 `POST`（创建）——本表只登记它的**写**那一个方法；`GET` 不进这张网（`GET` / `HEAD` / `OPTIONS` 不是写方法），⛔ 也**不许**把它塞进 `test_operation_log_coverage.py` 的只读反向钉子（`MUST_HAVE_NO_WRITE_METHOD`）：那条路径上**真的有**写方法，塞进去会当场红。⚠️ 三条的动作码与上家侧那一族**故意分开**（`system.taker_api_app_*` vs `system.open_api_app_*`）：两条通道的凭据是两张表、两套契约、两个页面，混成一个码会让日志页把"上家的凭据"与"打手的凭据"读成同一件事。
    "apps/takerapi/api/app_views.py": {
        ("post", "api/taker/apps/"): "system.taker_api_app_create",
        ("patch", "api/taker/apps/<int:pk>/"): "system.taker_api_app_update",
        ("post", "api/taker/apps/<int:pk>/rotate-secret/"):
            "system.taker_api_app_rotate_secret",
        # ⭐ T5 回调线：**手工重投**一条投递失败的回调推送（`docs/specs/2026-10-09-taker-callback-design.md` §7 的「失败手工重投」那一半）。⚠️ 上面那段注释的「三条」**不含**本条（.py 注释不适用「只增不改」，以本网运行时枚举到的为准；本作用域写端点共四条）。⚠️ **两个转换器**（`<int:pk>` 与 `<int:delivery_pk>`）**逐字**写（归一化器保留转换器模板）——与上家侧那条 `.../deliveries/<int:delivery_pk>/retry/` 同款拼写。⚠️ `record(...)` 同样**不在视图方法里**：它经本模块自己的埋点助手 `_record_app` 调 `record(...)`（那个助手名已登记在 `test_operation_log_coverage.py` 的 `_RECORD_FORWARDERS` 里，且按被走模块自己的 AST 解析 ⇒ 是真被走了一遍）。
        #    ⚠️ 动作码与上家侧那条**故意分开**（`system.taker_api_delivery_retry` vs `system.open_api_delivery_retry`）：两条通道的台账是两张表、收件人是两类人，混成一个码会让日志页把"重投给上家的"与"重投给打手的"读成同一件事。⚠️ 台账的**读**端点（`GET api/taker/apps/<int:pk>/deliveries/`）**不在这张表里**：本表的枚举只取写方法（`GET` / `HEAD` / `OPTIONS` 不进 `INSTRUMENTED`）⇒ 它进 `tests/test_operation_log_coverage.py` 的 `MUST_HAVE_NO_WRITE_METHOD`。
        ("post", "api/taker/apps/<int:pk>/deliveries/<int:delivery_pk>/retry/"):
            "system.taker_api_delivery_retry",
    },
    # ── 打手侧开放接口线的**对外抢单**端点 ──────────────────────
    # spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 2 行。⚠️ 它是**写**端点（抢单：写绩效行的接手人 + 订单状态迁移）⇒ 必须有一次 `record(...)`；那一次**直接落在** `post` 方法体里（本模块不借助手、也不走跨模块登记）。⚠️ **拼写照实测**：路由用 `path()` 转换器 `<order_no:order_no>`，而覆盖网的归一化器**原样保留**转换器 ⇒ 这里逐字写 `<order_no:order_no>`（⛔ 不是 `<order_no>`）。本行同样是**本网先报出来**的（先跑一次分类网，它报「未分类写端点」点名本路径）。
    # ⚠️ 动作码取**领域层那一个**（`order.claim` =「大厅自助接单」）：本端点就是那件事的对外入口（⛔ 不新造动作码——那要同一次改 `apps/audit/actions.py` 的模板表与 `tests/test_operation_log_model.py` 那几条字面量计数，属单端点范围之外）。与内网那条 `/claim/` **同源**：`claim_order` 是两条路共用的那一份实现；区别落在两列上：`actor` 写「打手侧应用 <名>」+ `system=True`（这条路上没有"人"这个操作人），`reason` 写渠道「打手对接接口」（内网那条写「系统网页」）。⚠️ 同模块的**只读**那条（`api/taker/v1/hall/`，拉大厅）不进本表：`GET` 不是写方法，覆盖网的枚举本身就不取它；它进的是 `test_operation_log_coverage.py` 的 `MUST_HAVE_NO_WRITE_METHOD`（反向钉子）。
    "apps/takerapi/api/order_write_views.py": {
        ("post", "api/taker/v1/hall/<order_no:order_no>/claim/"): "order.claim",
    },
    # ── D-0296：**对外镜像单接单**端点 ─────────────────────────
    # 镜像单正式接到时复用普通接单日志，由领域层写一次。
    "apps/takerapi/api/hall_views.py": {
        ("post", "api/taker/v1/hall/upstream/<int:pk>/claim/"): "order.claim",
    },
    # ── 打手侧开放接口线的**对外改状态**端点 ─────────────────────
    # spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 5 行 / §5.3-4。⚠️ 它是**写**端点 ⇒ 必须有一次 `record(...)`。⭐ 但那一次**不在本模块**里：判据与库写入都走领域口 `apps/business/order_writes.py::transition_order`（内网 `/transition/`、批量两段式、同步链路调的是**同一个函数**），留痕是它内部那一行 `order.transition`（spec §6 硬规则 2：迁移必须留痕）⇒ ⛔ 本模块**不补第二行** `record`（那会让一次成功的改状态写**两行**操作日志，而 `tests/test_takerapi_order_status_api.py` 的判据正是"**恰好一行**"）。
    # ⚠️ **拼写照实测**：路由用 `path()` 转换器 `<order_no:order_no>`，归一化器原样保留转换器 ⇒ 这里逐字写 `<order_no:order_no>`（⛔ 不是 `<order_no>`）。本行同样是**本网先报出来**的（先跑一次分类网，它报「未分类写端点」点名本路径）。⚠️ 动作码取**领域层那一个**（`order.transition` =「更改订单状态」）：本端点就是那件事的对外入口（⛔ 不新造动作码——那要同一次改 `apps/audit/actions.py` 的模板表与 `tests/test_operation_log_model.py` 那几条字面量计数，属单端点范围之外）；与内网那条 `/transition/` **同源**：两条路调的是**同一个**领域函数。区别落在 `actor` 上：写「打手侧应用 <名>」+ `system=True`（这条路上没有"人"这个操作人）。⚠️ **如实登记的一处代价**：本端点的 `reason` 列被调用方给的 `remark` 占了 ⇒「这一笔来自打手对接接口」这件事**不进操作日志**（与 T5 抢单那条不同，它把渠道写在 `reason` 上）。补偿 = `actor` 前缀 + 幂等台账那一行；理由与"为什么不在本任务新开一列"写在 `api/order_status_views.py` 的模块 docstring 里。
    "apps/takerapi/api/order_status_views.py": {
        ("post", "api/taker/v1/orders/<order_no:order_no>/status/"): "order.transition",
    },
    # ── 微信公众号集成（D-0268）的**绑定确认页** ────────────────────
    # ⚠️ `record(...)` **不在 `post` 方法体里**：它经自方法委派（`self._post_bind` / `self._post_unbind`）落进模块级助手 `_bind_openid` / `_unbind_openid`（与「绑定落库同一个 `atomic()`」，事务边界在那里）。⚠️ `_method_writes_log` 的自方法委派只跟 DRF 入口名（`(a)(b)(c)` 三类）⇒ 这条链的四跳都**显式登记**进 `test_operation_log_coverage.py` 的 `_RECORD_FORWARDERS`（`_post_bind` / `_post_unbind` / `_bind_openid` / `_unbind_openid`），少一跳它就报「声称已埋点却找不到 `record(`」。
    # ⚠️ **拼写照实测**：确认页挂在 `/wechatmp/`（`api/` 之外）⇒ 枚举出的归一化路径是 `wechatmp/bind-confirm`（**没有 `api/` 前缀**，⛔ 不是 `api/wechatmp/…`）；且 `bind-confirm` **不带尾斜杠**（`page_urls.py` 逐字）。⚠️ 动作码登记的是 `system.wechatmp_binding_create`：同一 `post` 里绑定 / 解绑 / 登录三动作共用一条路径，本表一格一个码 ⇒ 取**主动作**（绑定；解绑的动作码 `system.wechatmp_binding_delete` 在运行时由 `_unbind_openid` 自己写）。
    "apps/wechatmp/bind_views.py": {
        ("post", "wechatmp/bind-confirm"): "system.wechatmp_binding_create",
    },
    # ── QQ 机器人：保存机器人凭据 / 开关 ─────────
    # ⭐ 改的是**凭据与启停**（换 AppID / 换 secret / 动启用与认证开关），「谁在什么时候改过」事后必须能回答（rotate-secret 先例「只记动作」的同一条纪律）——「反复覆盖的配置行、记了只会刷满」的理由**对 secret 不成立**：secret 恰恰是**低频、不可回读、出事要追**的那一格。
    # ⛔ **只记动作不记值**：`app_secret` 只写不读（models 的红线），`record(...)` 直接落在 `QQBotBindingView.patch` 方法体里（不走跨模块登记），被改的**字段名**走 `field_name` 列，值一概不进任何列。
    "apps/qqbot/api/views.py": {
        ("patch", "api/qqbot/binding/"): "system.qqbot_binding_update",
    },
    # ⭐ 企微自建应用线（D-0379）：PUT config 记动作（计划 §1.4 钉死照
    # `system.qqbot_binding_update` 先例；与 wechatmp 配置 PUT 的「刻意不记」是
    # 两条线的不同裁定，各自留痕）。⚠️ `record(...)` 直接写在 `put` 方法体里。
    "apps/wecom/views.py": {
        ("put", "api/wecom/config/"): "system.wecom_config_update",
    },
    "apps/accounts/wxpusher_admin_views.py": {
        ("patch", "api/business/wxpusher-config/"): "system.wxpusher_config_update",
    },
    "apps/wecom/notification_views.py": {
        ("put", "api/wecom/notification/settings/"): "system.wecom_config_update",
    },
    "apps/wecom/aftersales_views.py": {
        ("put", "api/wecom/aftersales/settings/"): "system.wecom_config_update",
    },
    "apps/internal_chat/views.py": {
        ("put", "api/internal-chat/settings/"): "system.internal_chat_directory_update",
    },
    "apps/internal_chat/visitor_views.py": {
        ("put", "api/internal-chat/visitor/settings/"): "system.internal_chat_directory_update",
        ("post", "api/internal-chat/visitor/desk/availability/"): "system.internal_chat_visitor_availability",
        ("post", "api/internal-chat/visitor/desk/conversations/<int:conversation_id>/transfer/"): (
            "system.internal_chat_visitor_transfer"
        ),
        ("post", "api/internal-chat/visitor/desk/conversations/<int:conversation_id>/close/"): (
            "system.internal_chat_visitor_close"
        ),
    },
    "apps/internal_chat/system_notice_views.py": {
        ("put", "api/internal-chat/system-notification-settings/"): "system.internal_chat_notice_settings_update",
    },
    "apps/wecom/archive_views.py": {
        ("post", "api/wecom/orders/<order_no:order_no>/chat/groups/"): "order.wecom_chat_link",
    },
    # ── 打手钱包 / 提现审核线（D-0269）────────────────
    # ⭐ 三条都是**动钱**的写操作（解冻 / 提现申请当场扣余额 / 发起充值），「谁在什么时候动了多少钱」事后必须能回答。⚠️ 充值那条记的是**发起**（钱还没到账；到账走 Jeepay 回调、机器对机器不记）。
    "apps/accounts/app_wallet_views.py": {
        ("post", "api/app/wallet/release/"): "wallet.release",
        ("post", "api/app/wallet/withdraw/"): "wallet.withdraw",
        ("post", "api/app/wallet/deposit/"): "wallet.deposit",
    },
    # ── 打手 App 开放注册──────────────────────────────────
    # ⭐ 免登录、不验短信验证码的自助建号端点：**必须留痕**——「谁在什么时候注册了哪个账号」是事后纠纷（抢单、结算、封禁）里唯一的来源。动作码复用 `user.create`（自助注册线 `SelfRegisterPublicView` 先例：两条路建的是同一种东西；⛔ 不为它新造一个语义重复的码，那会同时动 `ACTION_TEMPLATES` 的条数钉子）。
    # ⚠️ `record(...)` 直接写在 `AppRegisterView.post` 方法体里（与建号同一个 `atomic()`），所以**不用**登记覆盖率网的 `_RECORD_FORWARDERS`。
    "apps/accounts/app_auth_views.py": {
        ("post", "api/app/auth/register/"): "user.create",
    },
    # ⭐ 提现审核后台：6 个写动作各一条。⚠️ 列表是读，不记。
    #    全部共用类内助手 `_record_withdraw_review`（已登记进覆盖率网的
    #    `_RECORD_FORWARDERS`）。
    "apps/business/api/withdraw_admin_views.py": {
        ("post", "api/business/withdrawals/<pk>/approve/"): "finance.withdraw_approve",
        ("post", "api/business/withdrawals/<pk>/reject/"): "finance.withdraw_reject",
        ("post", "api/business/withdrawals/<pk>/transfer/"): "finance.withdraw_transfer",
        ("post", "api/business/withdrawals/<pk>/refresh/"): "finance.withdraw_refresh",
        ("post", "api/business/withdrawals/<pk>/mark-paid/"): "finance.withdraw_mark_paid",
        ("post", "api/business/withdrawals/<pk>/interrupt/"): "finance.withdraw_interrupt",
    },
    # ⭐ 客服后台打手钱包管理 / 游戏押金金额：两个**写**端点各一条动作；列表（钱包 / 押金规则）是读、不在本表取集里。⚠️ 调账的重复提交（幂等命中）**不写第二行**——那是同一笔，不是第二次操作。⚠️ 两个写动作各经本模块自己的埋点助手（`_record_adjust` / `_record_rule_update`），助手名已登记进覆盖率网的 `_RECORD_FORWARDERS`；⛔ 改名要同批改那里。
    # ⭐ 打手个人押金额度（计划 §6）：`PUT …/takers/<id>/deposit-quota/` 是写端点。**主动作取金额那半** `finance.taker_deposit_quota_amount`（一次保存通常落多条金额行；开关那半用 `finance.taker_deposit_quota_switch`，同一次请求里可能同时出现、也可能只出现一条）。⚠️ `record(...)` 直接写在该视图的 `put` 方法体里（⛔ 不经助手），覆盖率网按 AST 抓得到。
    "apps/business/api/wallet_admin_views.py": {
        ("post", "api/business/wallets/<pk>/adjust/"): "finance.taker_wallet_adjust",
        ("put", "api/business/deposit-rules/<int:game_id>/"):
            "finance.taker_deposit_rule_update",
        ("put", "api/business/takers/<int:user_id>/deposit-quota/"):
            "finance.taker_deposit_quota_amount",
    },
}

# ---------------------------------------------------------------------------
# 2. 刻意不记（spec §5.7）—— 写端点 → 为什么不记
# ---------------------------------------------------------------------------

# 全部是 spec §5.7 的表。⚠️ 只列**写方法**：一切 `GET` 不在枚举集合里，所以「GET 一律不记」这条口径由枚举本身保证，不需要在这里列一堆 GET。
DELIBERATELY_NOT_LOGGED: dict[tuple[str, str], str] = {
    ("post", "api/business/orders/export-jobs/"): (
        "只提交导出任务，文件准备后由后台事务记录 order.export"
    ),
    ("post", "api/business/settlements/export-jobs/"): (
        "只提交导出任务，文件准备后由后台事务记录 settlement.export"
    ),
    ("post", "api/integration/v1/taker-token/"): "凭据交换，无业务写入",
    ("post", "api/integration/v1/orders/<order_no:order_no>/images/ticket/"):
        "复用运营进度图上传取票，沿用原操作日志口径",
    ("post", "api/integration/v1/orders/<order_no:order_no>/images/"):
        "复用运营进度图认领，沿用原操作日志口径",
    ("post", "api/integration/v1/taker/orders/<order_no:order_no>/images/ticket/"):
        "复用打手进度图上传取票，沿用原操作日志口径",
    ("post", "api/integration/v1/taker/orders/<order_no:order_no>/images/"):
        "复用打手进度图认领，沿用原操作日志口径",
    ("post", "api/business/approvals/"): "申请人和日期保存在审批记录",
    ("post", "api/business/approvals/media/ticket/"): "仅签发证据截图上传凭证",
    ("put", "api/business/approvals/media/upload/<str:ticket>/"): "上传待提交截图，审批提交前不建立记录",
    ("post", "api/business/online-payroll/preview/"): "只读金额和付款账号预览",
    ("post", "api/business/online-payroll/batches/<uuid:batch_id>/payment/"): "仅查原付款单或请求重新获取同一批次付款凭据，不制单",
    # ⭐ 打手 App 线（D-0269）：`/api/app/*` 的写端点分类。认证四条与现有 `/api/auth/login` 同口径（认证层事件不记）；回调是机器对机器（同 peer-sync 两个机器端点）。钱包三条（解冻 / 提现申请 / 充值下单）已补埋点、登记在上面的 `INSTRUMENTED` ⇒ 本表**不再有它们**。
    ("post", "api/app/auth/login/"): "认证层事件不记（与 /api/auth/login 同口径）",
    ("post", "api/app/auth/sms/send/"): "认证层事件不记（发验证码）",
    ("post", "api/app/auth/sms/login/"): "认证层事件不记（验证码登录）",
    ("post", "api/app/auth/logout/"): "认证层事件不记（与 /api/auth/logout 同口径）",
    ("post", "api/app/pay/notify/"): "机器对机器回调（同 peer-sync 两个端点）",
    # ⭐ 小程序 openid：换一张**付款用**的凭证，不是身份绑定（openid 不认身份），每次进小程序都会重新取；且值本身（openid）不该进日志 ⇒ 与认证层事件同口径，不记。
    ("post", "api/app/wallet/wx-openid/"): (
        "换取付款用的小程序 openid（非身份绑定，每次进小程序重取；值不进日志）⇒ 不记"
    ),
    # ⭐ 打手「我的收入」的**提前结算申请**（spec `2026-10-23-taker-income-and-early-settlement.md` §3.4）⇒ **刻意不记**。理由（三条，逐条对得上本表既有口径）：① 这条端点只写**一行** `PerformanceSettleRequest`——申请人 / 申请时间 / 状态都在那行上，本身就是这条动作的留痕（不是"改完值什么都没留下"的那种写）；② 它**不动钱**：不写钱包、不改绩效的结账列、不改订单状态（spec §3.4 第 7 条），真正动钱的是财务结账 `/api/business/settlements/settle/`，那条已有 `performance.settle` 埋点（`INSTRUMENTED` 的 `settlement_views` 那一格）；③ 它是**打手对自己的单**发起的自助动作，与 `wallet.*` 那三条「动钱」不同：记进操作日志只会让财务日志页多出一堆「某人申请了一下」的噪声。
    ("post", "api/app/income/settle-requests/"): (
        "打手自助申请提前结算：状态只落在 PerformanceSettleRequest 行本身"
        "（含申请人 / 申请时间 / 状态），不动金额、不动订单与绩效的结账列；"
        "真正动钱的财务结账已有 performance.settle 埋点 ⇒ 不重复记"
    ),
    # ⭐ 网页端批量申请提前结算（D-0365）：与上面 App 端**同一个领域动作**（`create_settle_request` 同一份判据链）⇒ 豁免理由逐条同款（三条照抄那边）：① 只写 `PerformanceSettleRequest` 行（申请人 / 时刻 / 状态都在行上）；② 不动钱；③ 打手自助动作，记日志只会给财务日志页添噪声。⛔ 与 App 端是**两条路径**（`api/app/…` 与 `api/business/…`），都要各自登记——少一条，分类网会以「没分类的写端点」报红。
    ("post", "api/business/my-orders/settle-requests/"): (
        "打手自助批量申请提前结算（网页端，与 App 端同一份 create_settle_request 判据）："
        "逐条只落 PerformanceSettleRequest 行（申请人 / 时间 / 状态都在行上），"
        "不动金额、不动订单与绩效的结账列；"
        "真正动钱的财务结账已有 performance.settle 埋点 ⇒ 不重复记"
    ),
    # ⛔ 打手钱包三条写（解冻 / 提现申请 / 发起充值）已移进上面的 `INSTRUMENTED`，⛔ 不要加回本表。
    # ⭐ 微信公众号集成（D-0268）：四条写端点的分类。配置保存 / 菜单动作照「反复覆盖的单例配置 / 无本地行的对外动作」那一族口径（同自助注册配置 PUT、webhook 测试消息）；回调是机器对机器（同 peer-sync 两个机器端点）。**绑定确认页的 POST 不在这张表**——它有真埋点（绑定 / 解绑是账号安全动作，值得留痕），登记在上面的 `INSTRUMENTED`。
    ("put", "api/wechatmp/config/"): (
        "公众号配置是租户内反复覆盖的**单例行**（appsecret/token 留空=保持不变的覆盖式保存），"
        "没有可指认的业务实体变更；照自助注册配置 PUT 的同一条口径 ⇒ 不记操作日志"
    ),
    ("post", "api/wechatmp/menu/sync/"): (
        "同步预设菜单写的是**微信侧**的菜单状态，本地不落任何业务行"
        "（同 webhook「发送测试消息」的口径）⇒ 不记操作日志"
    ),
    ("delete", "api/wechatmp/menu/"): "同上：删除菜单是微信侧动作，本地不落行",
    ("post", "api/wechatmp/callback"): (
        "微信服务器的**机器回调**（无页面操作人，认证面匿名），只翻绑定行的 subscribed 位；"
        "事件不是页面操作 ⇒ 照机器对机器端点的同一条口径不记操作日志"
    ),
    # ⭐ 企微自建应用线（D-0379）：四条写端点的分类。回调是机器对机器（同 wechatmp callback
    # 口径）；自检 / 测试发送是「无本地行的对外动作」（同 wechatmp menu/sync 口径——
    # 自检顺带刷新 token 缓存列，那是凭据缓存的正常维护、不是页面操作）。
    ("post", "api/wecom/config/selfcheck/"): (
        "连通性自检只**真拉一次**企微 token 并回报告，不改任何配置行"
        "（token 缓存列的刷新是凭据缓存的正常维护）⇒ 不记操作日志"
    ),
    ("post", "api/wecom/config/test_send/"): (
        "测试发消息写的是**企微侧**的会话，本地不落任何业务行"
        "（同 wechatmp「同步预设菜单」的口径）⇒ 不记操作日志"
    ),
    ("post", "api/wecom/callback"): (
        "企微服务器的**机器回调**（无页面操作人，认证面匿名），只写回调消息台账行；"
        "事件不是页面操作 ⇒ 照机器对机器端点的同一条口径不记操作日志"
    ),
    ("post", "api/wecom/orders/<order_no:order_no>/group-qr/"): (
        "打开群聊自动取码、续期及并发轮询，不记人员操作日志；群码配置表保存当前编号和状态，"
        "可识别当前成功配置但不承诺保留完整创建历史；异常记受控日志，不包含二维码地址或访问凭据"
    ),
    ("post", "api/wecom/archive/login/"): (
        "验证企微身份后仅更新当前登录会话，按登录鉴权口径不记操作日志"
    ),
    ("post", "api/wecom/archive/search/"): (
        "会话搜索只读，用请求正文传查询词以免进入访问日志，不记操作日志"
    ),
    ("post", "api/wecom/orders/<order_no:order_no>/chat/login/"): (
        "验证企微身份后只写当前登录会话，不变更订单或企业配置，按登录鉴权口径不记操作日志"
    ),
    # 系统内部公告与推送规则这两组写端点**没有** `record(...)`（主仓实装时未登记），如实登记为「刻意不记」，⛔ 不是静默漏分类；补真埋点需要单独裁定审计动作 / 日志类型。
    ("post", "api/business/system-notices/"): "公告是租户内配置行，主仓实装时未接入操作日志（合并窗口补登）",
    ("patch", "api/business/system-notices/<pk>/"): "同上",
    ("put", "api/business/system-notices/<pk>/"): "同上",
    ("delete", "api/business/system-notices/<pk>/"): "同上",
    ("post", "api/business/order-push-settings/"): "推送规则是租户内配置行，主仓实装时未接入操作日志（合并窗口补登）",
    ("patch", "api/business/order-push-settings/<pk>/"): "同上",
    ("put", "api/business/order-push-settings/<pk>/"): "同上",
    ("delete", "api/business/order-push-settings/<pk>/"): "同上",
    ("post", "api/business/order-push-settings/<pk>/test/"): "发送一条测试消息，不改业务实体（合并窗口补登）",
    # ⭐ 首页二维码横幅（D-0262）：后台首页「二维码横幅」的五个写端点**照 `system-notices` 先例**刻意不记——二维码是租户内首页配置行（添加 / 换图 / 启停 / 删除都不改业务实体，行上另有 `created_by` 快照），主仓实装时未接入操作日志 ⇒ 如实登记为「刻意不记」，⛔ 不是静默漏分类。⚠️ 键逐字取自本网失败输出（`test_every_enumerated_write_endpoint_is_classified` 报出的那 5 个 `(method, path)`，含路由转换器 `<pk>` 的原样形状），不是照同族猜的。
    ("post", "api/business/home/qr-codes/"): "二维码是租户内首页配置行，主仓实装时未接入操作日志（照 system-notices 先例，合并窗口补登）",
    ("put", "api/business/home/qr-codes/<pk>/"): "同上",
    ("patch", "api/business/home/qr-codes/<pk>/"): "同上",
    ("delete", "api/business/home/qr-codes/<pk>/"): "同上",
    ("post", "api/business/home/qr-codes/media/ticket/"): "签发图片直传凭证，不写任何业务实体（照 system-notices 先例补登）",
    # ⭐ 游戏 logo 上传：**取票**端点刻意不记——它只签发一张预签名 PUT 凭证、**不写库**（`Game.logo_key` 只在认领时写，那条已登记进 `INSTRUMENTED`）；同头像 `…/media/<kind>/ticket` 与首页二维码取票的既有口径。
    ("post", "api/business/games/<int:pk>/logo/ticket"): "签发图片直传凭证，不写任何业务实体（同头像 / 首页二维码取票口径）",
    ("post", "api/auth/login"): "裁定 #4：登录不记（spec §5.7 第 1 行）",
    # ⭐ 自助注册线：保存配置 / 生成链接两条**刻意不记**。保存配置只改租户内那一行单例配置，且生成链接时游戏池与开关会**逐条快照**进 `SelfRegistrationInvite`（行上有 `created_by` + `created_at` + 池 + 开关）——「谁在什么时候按什么配置生成的」由台账行自己承载（Feedback 行先例：同一事实不写两处）；配置行是反复覆盖的单例，记操作日志只会刷满（同图片存储线的口径）。
    ("put", "api/accounts/self-registration/config/"): (
        "自助注册配置是租户内反复覆盖的**单例行**，没有可指认的业务实体变更"
        "（生成链接那一刻的池与开关会快照进 `SelfRegistrationInvite` 行留痕）⇒ 不记操作日志"
    ),
    ("post", "api/accounts/self-registration/invites/"): (
        "生成一次性链接：`SelfRegistrationInvite` 行**自己**就带着「谁、什么时候、"
        "按什么池与开关生成的」（`created_by` / `created_at` / `games` / "
        "`forbid_orders_default`），再记一条操作日志是把同一事实写两处（Feedback 行先例）"
    ),
    ("post", "api/auth/logout"): "裁定 #4：登出不记（spec §5.7 第 1 行）",
    ("post", "api/auth/order-finance-ticket/"): "仅签发短期登录凭证，沿用认证层事件口径",
    ("<plain-view>", "api/auth/wxpusher/login"): "验证和确认短时签名登录，与账号密码登录一样不记操作日志",
    ("post", "api/auth/wxpusher/orders/<order_no:order_no>/continue"): "只返回官方 WxPusher 跳转地址",
    ("post", "api/auth/wxpusher/hall/continue"): "只返回官方 WxPusher 跳转地址",
    ("post", "api/auth/wxpusher/binding/start"): "签发个人短期绑定码，只写待验证状态",
    ("post", "api/auth/wxpusher/binding/cancel"): "取消本人待完成的订阅链接，保留现有通知绑定",
    ("post", "api/auth/wxpusher/binding/callback"): "记录待验证的 WxPusher 订阅结果，不变更通知绑定",
    ("post", "api/auth/wxpusher/binding/link-code"): "验证已订阅身份并发送一次性验证码，不变更通知绑定",
    ("post", "api/auth/wxpusher/binding/scan"): "查询订阅并发送一次性验证码，只写待验证状态",
    ("post", "api/auth/wxpusher/binding/test"): "本人验证手机通知渠道的固定测试消息，不修改业务记录",
    ("post", "api/auth/wxpusher/binding/confirm"): "本人自助设置手机通知渠道，沿用自助账号设置不入操作日志",
    ("delete", "api/auth/wxpusher/binding"): "本人关闭手机通知渠道，沿用自助账号设置不入操作日志",
    ("post", "api/business/wxpusher-config/test/"): "发送固定测试消息，结果记在消息发送记录。",
    ("patch", "api/auth/wxpusher/preferences"): "本人设置接收哪些手机推送，沿用自助账号设置不入操作日志",
    ("patch", "api/auth/wxpusher/hall-subscription"): "本人设置新单订阅游戏和筛选偏好。",
    ("post", "api/auth/wxpusher/inbox/<int:pk>/read"): "本人标记自己的站内通知已读。",
    ("patch", "api/auth/wxpusher/staff/preferences"): "本人设置员工手机推送偏好。",
    ("post", "api/auth/wxpusher/staff/binding/start"): "签发本人员工通知的短期绑定码。",
    ("post", "api/auth/wxpusher/staff/binding/progress"): "本人扫码绑定员工通知渠道。",
    ("post", "api/auth/wxpusher/staff/binding/test"): "本人发送固定试发消息，结果写到发送记录。",
    ("delete", "api/auth/wxpusher/staff/binding"): "本人解除员工通知渠道。",
    ("post", "api/internal-chat/visitor/guest/register/"): "访客身份记录保存登记时间和来源摘要，手机号仅保存在访客身份中。",
    ("post", "api/internal-chat/visitor/guest/conversations/"): "创建只供访客与指定接待人员使用的会话。",
    ("post", "api/internal-chat/visitor/guest/conversations/<int:conversation_id>/messages/"): "访客发言由消息记录保存。",
    ("post", "api/internal-chat/visitor/guest/conversations/<int:conversation_id>/read/"): "访客更新自己的阅读位置。",
    ("post", "api/internal-chat/visitor/desk/conversations/<int:conversation_id>/messages/"): "接待人员发言由消息记录保存。",
    ("post", "api/internal-chat/visitor/desk/conversations/<int:conversation_id>/read/"): "接待人员更新自己的阅读位置。",
    ("post", "api/internal-chat/app/access/"): "认证后签发本人的短期聊天凭据，认证只节流更新令牌最近使用时间。",
    ("post", "api/internal-chat/app/refresh/"): "在原会话时限内续签本人的短期聊天凭据，不写业务记录。",
    ("post", "api/internal-chat/app/unread/"): "为本人签发接收未读事件的短期连接票据，认证只节流更新令牌最近使用时间。",
    ("post", "api/internal-chat/app/order-groups/<int:conversation_id>/messages/"): "群聊发言由消息记录保存。",
    ("post", "api/internal-chat/app/order-groups/<int:conversation_id>/read/"): "本人更新群聊阅读位置。",
    ("post", "api/internal-chat/order-groups/<int:conversation_id>/messages/"): "群聊发言由消息记录保存。",
    ("post", "api/internal-chat/order-groups/<int:conversation_id>/read/"): "本人更新群聊阅读位置。",
    ("post", "api/internal-chat/notification-conversations/<int:conversation_id>/messages/"): "聊天发言由消息记录保存。",
    ("post", "api/internal-chat/notification-conversations/<int:conversation_id>/read/"): "本人更新聊天阅读位置。",
    ("post", "api/internal-chat/app/notification-conversations/<int:conversation_id>/messages/"): "聊天发言由消息记录保存。",
    ("post", "api/internal-chat/app/notification-conversations/<int:conversation_id>/read/"): "本人更新聊天阅读位置。",
    ("post", "api/internal-chat/conversations/"): "创建站内聊天会话。",
    ("post", "api/internal-chat/conversations/<int:conversation_id>/messages/"): "聊天发言由消息记录保存。",
    ("post", "api/internal-chat/conversations/<int:conversation_id>/messages/<int:message_id>/revoke/"): (
        "撤回聊天发言由消息状态保存。"
    ),
    ("post", "api/internal-chat/conversations/<int:conversation_id>/pin/"): "更新群聊内的消息置顶。",
    ("post", "api/internal-chat/conversations/<int:conversation_id>/preferences/"): "本人设置聊天会话偏好。",
    ("post", "api/internal-chat/conversations/<int:conversation_id>/read/"): "本人更新聊天阅读位置。",
    ("post", "api/internal-chat/conversations/<int:conversation_id>/presence-ticket/"): "为本人签发当前单聊的短期在线状态订阅票据。",
    ("post", "api/internal-chat/orders/<order_no:order_no>/join/"): "站内入群由群成员记录保存。",
    ("post", "api/internal-chat/orders/<order_no:order_no>/messages/"): "内部群聊发言由消息记录保存，不改变订单业务记录。",
    ("post", "api/internal-chat/orders/<order_no:order_no>/reopen/"): "重开由聊天核心持久会话事件保存，不改变订单状态或业务记录。",
    ("post", "api/internal-chat/service/browser-authorize/"): "签名回查只读取有效会话、聊天及订单权限。",
    ("post", "api/internal-chat/service/order-authorizations/"): "签名内部接口仅批量读取当前订单权限，不写业务记录。",
    ("post", "api/internal-chat/service/order-intents/"): "签名内部接口传输聊天待办，确认仅更新内部传输进度，不改变订单业务记录。",
    ("post", "api/internal-chat/service/peer-chat/"): (
        "签名服务间接口只入队、读取、确认或延后跨工作室聊天投递，传输进度保存在同步台账。"
    ),
    ("post", "api/internal-chat/direct-ticket/"): "签发本人短期浏览器聊天票据，不改变订单或聊天记录。",
    ("post", "api/internal-chat/realtime-ticket/"): "签发本人短期聊天连接票据。",
    # ⭐ 我的资料线：自助改密。
    ("post", "api/auth/password"): (
        "裁定 #4 同族：对标操作日志目录里没有「自助改密」动作（只有管理员重置换别人 "
        "`user.reset_password`），本仓把认证层事件排除在操作日志外（登录/登出同理）⇒ 不埋点。"
    ),
    # ⭐ 频道文章作品线：文章的写端点 + 两枚媒体端点，**照 `system-notices` / 首页二维码横幅的先例刻意不记**——文章与频道简介都是**租户内内容**（行上自带 `created_by` / `created_at` / `updated_at` 快照），改一篇稿件 / 改一句简介不属于「动钱、动审核、交单」那类事后要追责的实体变更；两枚媒体端点更是一个字节都不写库。⛔ 这不是静默漏分类：要补真埋点得先裁定审计动作码与日志类型（`ACTION_TEMPLATES` 的条数钉子也要同动），属另一条线。
    # ⚠️ 键逐字取自本网失败输出（`test_every_enumerated_write_endpoint_is_classified` 报出的 9 个 `(method, path)`，含 router 转换器 `<pk>` 的原样形状），不是照同族猜。
    ("post", "api/business/articles/"): "文章是租户内内容行，照 system-notices 先例不记",
    ("put", "api/business/articles/<pk>/"): "同上",
    ("patch", "api/business/articles/<pk>/"): "同上",
    ("delete", "api/business/articles/<pk>/"): "同上",
    ("post", "api/business/articles/<pk>/publish/"): "发布 / 下架只翻文章行的状态（同一条内容行）",
    ("post", "api/business/articles/<pk>/unpublish/"): "同上",
    ("post", "api/business/articles/media/ticket"): "签发图片直传凭证，不写任何业务实体（同头像 / 游戏 logo 取票口径）",
    ("post", "api/business/articles/media/claim"): "只校验对象并回读链接，不写任何业务实体",
    ("put", "api/business/channel-profile/"): "频道简介是租户内反复覆盖的单例行，没有可指认的业务实体变更",
    ("post", "api/audit/events/"): (
        "本线新增的读操作上报接口**自己**：它写的就是日志，再记一条自己的日志会自激"
        "（spec §5.7 第 2 行）"
    ),
    ("post", "api/business/notifications/<int:notification_id>/read/"): (
        "已读是**体感状态**不是业务动作；对标无此功能（spec §5.7 第 3 行）"
    ),
    ("post", "api/business/orders/parse/"): (
        "粘贴解析是**录单前的辅助读**，不落业务数据；`order.create` 已有留痕"
        "（spec §5.7 第 4 行）"
    ),
    # ⭐ 订单管理页「同步收款」（计划 §5-M7）：本端点**不写任何东西**——它只过序列化器、数一次候选行、做一次 advisory lock 探测，然后起 daemon 线程。真正的写（改收款状态 + 两条留痕）在领域层 `xgj_receipt_sync` 里，走既有的 `change_payment_status` + `xgj_push._write_receipt_log` ⇒ 那些动作已有自己的日志码 ⇒ 本端点**刻意不记**操作日志（记了只会多一行「某人点了一下」的噪声，且没有可指认的变更）。
    ("post", "api/business/orders/xgj-receipt-sync/"): (
        "端点只校验 + 数行 + 起后台线程，**不写库**；真正的写（未收款→已结款 + 两条留痕）"
        "在领域层走既有 `change_payment_status` / `xgj_push._write_receipt_log`，"
        "那两条动作已各有自己的日志码 ⇒ 本端点刻意不记"
    ),
    # ⭐ 读上游平台订单，**不写库**（spec §5.7 第 5 行）。
    # 与 `orders/parse/` 同类：只回填前端表单，不落业务数据；`order.create` 已有留痕。
    ("post", "api/business/read-platform-order/"): (
        "读上游平台订单、**不落库**：只回填录单页表单，不写任何业务数据；与 "
        "`orders/parse/` 同类的录单前辅助读（spec §5.7 第 5 行）"
    ),
    # ⭐ 闲管家对接线的两条**读上游**端点。
    # 与 `read-platform-order/` 同类：POST 只是传输形状（入参放 body），
    # 业务语义是**读**——两条都不落任何业务数据。
    ("post", "api/business/xgj-bindings/<pk>/test-connection/"): (
        "拿这一行的凭据调上游 `user/authorize/list`「测试连接」：**只读上游**、"
        "不写任何业务数据（与 `read-platform-order/` 同类，spec §5.7 第 5 行）。"
        "⚠️ 与写配置的那三条不同：它不改这一行，也不该在日志里留下一条「改了什么」的假陈述"
    ),
    ("post", "api/business/xgj/order-detail/"): (
        "按订单号查上游订单详情并**原样透传**给前端：**不落库**、不写任何业务数据"
        "（与 `read-platform-order/` 同类，spec §5.7 第 5 行）"
    ),
    # ⛔ 租户级 `jidanpu-binding/test-connection/` 已随租户级鸡蛋铺线整条拆除，登记表**同一次**删掉它（本表这一档要求「端点必须真实存在于枚举集合里」，留着会让那一条腿红）。同族的「登录上游列一页人员」那条（写引擎列 `account_uid` / `account_name`、非用户配置）也随那条线一并拆除。
    # ⭐ 下游鸡蛋铺线：代练员点「测试连接」。与上面租户级那条同类：POST 只是传输形状，业务语义是**读**——跑一遍绑定检测、把人话结论回给页面，**不落任何业务数据**，也不写 `checked_at` / `last_error`（那两个列的写入方是 PUT 的启用那一刻与后续期的同步引擎）⇒ 不记操作日志。
    ("post", "api/business/member/jidanpu-binding/test-connection/"): (
        "拿本人那一行的凭据跑绑定检测（登录 / 来源名 / 三条权限的页面可达性）："
        "**只读上游**、不写任何业务数据、也不改那一行配置"
        "（与 `jidanpu-binding/test-connection/` 同类，spec §5.7 第 5 行）"
    ),
    # ⭐ 上家鸡蛋铺线：客服点「测试连接」。与上面两条同类：POST 只是传输形状，业务语义是**读**——跑一遍四项检测、把人话结论回给页面，**不落任何业务数据**，也不写 `checked_at` / `last_error`（那两个列的写入方是 PUT 的启用那一刻与后续期的同步引擎）⇒ 不记操作日志。
    ("post", "api/business/member/upstream-jidanpu-binding/test-connection/"): (
        "拿本人那一行的凭据跑四项只读检测（登录 / 大厅 / 我的订单 / 上传页隐藏域）："
        "**只读上游**、不写任何业务数据、也不改那一行配置"
        "（与 `member/jidanpu-binding/test-connection/` 同类，spec §5.7 第 5 行）"
    ),
    # ⭐ 手动推送（计划 `2026-10-23-taker-my-orders-push.md` §2）：「指派给我的订单」工具栏两颗按钮的端点（勾选的行 ⇒ 录单 / 推状态）。⚠️ 它**不写我方业务数据**：订单 / 绩效 / 金额 / 状态一个字节都不动，改的是**对方**系统里那张单 ⇒ 没有「改了本地什么」可记。
    # ⛔ 台账**只覆盖「真的排出事件」的那几单**，而且**只能间接指认操作人**（`payload.member_binding_id` ⇒ `binding.user`，一个绑定归一个人）；三档**一条台账都没有**：「不是他当前接手的单」（`claimed_perf_of` 判掉）/ 「没有链路」（排不出事件）/ 入参非法（视图之前就被序列化器拒掉）⇒ 台账**不是**这张端点的访问日志。
    # ⚠️ 如实登记：这是**刻意不记**、不是「漏了」；业务方将来若要一条「谁手动推了单」的操作日志，要先在 `apps/audit/actions.py` 取一个动作码（全局编号资源），⛔ 不在这里顺手复用别人的码（例如 `order.transition`——那会把「推给对方」说成「我方订单换了状态」，是一句假话）。
    ("post", "api/business/member/jidanpu-push-now/"): (
        "把手动勾选的行变成出向推送台账事件并立刻跑一轮：**不改我方任何业务数据**"
        "（订单 / 绩效 / 金额 / 状态都不动），改的是对方系统里那张单 ⇒ 没有「改了本地什么」可记。"
        "⚠️ 台账**不是**逐单留痕：只有真的排出事件的那几单在 `JidanpuSyncEvent` 里有一行，"
        "操作人靠 `payload.member_binding_id` ⇒ `binding.user` **间接**指认；"
        "「不是他接手的单」/「没有链路」/「入参非法」三档**零台账**"
    ),
    # ⭐ 打手 App 按需同步群二维码：它只往鸡蛋铺同步台账排一条 `upstream.pull_pics_in` **请求行**（`payload.trigger = app_detail`，带 `requested_by_user_id`）⇒ 那**一行台账自己就是留痕**（谁、哪一单、什么时候要拉的、拉成没有），不改订单 / 绩效 / 金额 / 状态中的任何一格。
    # ⚠️ 拉图由 `jidanpu_sync` 的既有消费段执行，结论**写回同一条请求行**（`status` / `reason` / `processed_at`）——再记一条操作日志是把同一事实写两处（Feedback 行先例）。
    ("post", "api/business/my-orders/<order_no:order_no>/screenshots/sync/"): (
        "只往鸡蛋铺同步台账排一条拉图请求行（`JidanpuSyncEvent` 的 `upstream.pull_pics_in` + "
        "`payload.trigger=app_detail`，行上带请求人 / 单号 / 时刻与最终结论）："
        "不改订单 / 绩效 / 金额 / 状态，真正的拉图由 `jidanpu_sync` 的既有消费段执行 ⇒ "
        "台账行自己就是留痕，不重复记操作日志"
    ),
    # ⭐ 握手与解绑两条**同步端点**：都是**机器对机器**、没有页面操作人（计划 Global Constraint 11 明写要登记在这里），但**认证形态不同、别写成一回事**：`bind/` 是**账号 + 口令**握手：`authentication_classes = []` ⇒ 此时**还没有令牌**（它正是来取令牌的），也 ⛔ 不认会话 cookie（spec §9-S2 / 裁决 R5）；`unbind/` 才只认 Bearer 令牌。
    # ⚠️ **留痕口径**：绑定事实由 `PeerDownstreamBinding` **那一行自身**承载（`token_in_hash` / `token_out` / `state` / `revoked_at`），但 ⛔ **不是**「每次握手都写一条留痕」——`bound_at` 是 `auto_now_add`，**重绑不刷新它**（判据见 `tests/test_peer_binding_api.py` 的 `test_rebinding_the_same_account_rotates_the_token_and_reactivates`）⇒ 它只能读作「这一行**第一次**是什么时候建立的」，不能读作「最近一次握手」。
    ("post", "api/business/peer-sync/bind/"): (
        "机器对机器、非人操作：这是**账号+口令握手**（`authentication_classes = []`——"
        "此时还没有令牌、也 ⛔ 不认会话 cookie）⇒ 没有页面操作人；绑定事实由 "
        "`PeerDownstreamBinding` 那一行自身承载（⚠️ `bound_at` 是 `auto_now_add`，"
        "重绑**不刷新**它，只能读作「这一行第一次建立的时间」）。操作日志的语义是"
        "「谁在页面上干了什么」（计划 Global Constraint 11）"
    ),
    ("post", "api/business/peer-sync/unbind/"): (
        "机器对机器、非人操作：认证只认 Bearer 令牌（无页面操作人）；解绑事实由绑定表"
        "自身留痕（上家侧 `revoked_at`、下家侧 `deleted_at`）。操作日志的语义是"
        "「谁在页面上干了什么」（计划 Global Constraint 11）"
    ),
    # ⛔ `peer-sync/inbound/events/`（下家侧**接收**端点）已挪进上面的 `INSTRUMENTED`——它现在**真的写**订单日志（`peer.inbound_*` / `peer.upstream_*` / `peer.hall_claim`，落在 `peer_inbound` 的领域层），⛔ 别再把它加回本表（加了会让 `test_deliberately_not_logged...` 的互斥断言红）。
    # ⭐ 被问一方的**只读对账**端点。⚠️ POST 只是**传输形状**（单号清单放 body），业务语义是**读**：按 `(upstream_binding, peer_order_no)` 查 `PeerOrderLink` 并回报水位（`max(last_applied_at, local_action_at)`）。⛔ 一行都不写——不落 link 行、不碰绑定健康列（`_record_inbound_health` 只在接收端点里调）⇒ 没有可留痕的业务动作，记一条只会是「改了什么」的假陈述。
    ("post", "api/business/orders/<order_no:order_no>/peer-direct-assignment/search/"): (
        "只查已授权下家的账号名称和搜索凭证，无订单写入"
    ),
    ("post", "api/business/orders/<order_no:order_no>/jidanpu-direct-assignment/search/"): (
        "只读同一订单关联的鸡蛋铺打手名单，无订单写入"
    ),
    ("post", "api/business/orders/peer-direct-assignment/search/"): (
        "录单前只查已授权下家的账号名称和搜索凭证，无订单写入"
    ),
    ("post", "api/business/peer-sync/reconcile/"): (
        "机器对机器、非人操作：认证只认 Bearer 令牌（无页面操作人）；它只是**只读问答**"
        "（如实回报 `PeerOrderLink` 的水位），不写任何业务数据（与 `read-platform-order/` "
        "同类）。操作日志的语义是「谁在页面上干了什么」（计划 Global Constraint 11 / spec §16.8）"
    ),
    # ⭐ 上家侧**大厅全量拉取**端点（计划 `2026-10-25-peer-hall-pull` §1 / §2）。
    ("post", "api/business/peer-sync/hall/list/"): (
        "机器对机器、非人操作：认证只认 Bearer 令牌（无页面操作人），调用方是下家的拉取进程。"
        "它读的是本租户在厅卡、写的是 `PeerSyncEvent(kind='hall.pull')` **证据台账**"
        "（Z20 判据的第二类来源，不是可指认的业务实体变更）；共享事实由绑定行"
        "（`hall_push_enabled` / `state`）与镜像行自身承载。操作日志的语义是"
        "「谁在页面上干了什么」（计划 Global Constraint 11）"
    ),
    # ⭐ 闲管家推送接收端（spec §D9 / §6-7）：**商品**推送那条。它只验签 + ack，一个字节都不写（`xgj_push.handle_product_push` 里没有任何写库 / 写日志），所以没有可留痕的业务动作——记一条只会是「改了什么」的假陈述。⚠️ **订单**那条**不在这里**：它会改状态并写订单日志 ⇒ 登记在上面的 `INSTRUMENTED`。
    ("post", "api/business/xgj/push/product/"): (
        "机器对机器、非人操作：闲管家的服务器推商品消息，本线只按 §2.2 的公式验签后回 "
        "`{\"result\":\"success\",\"msg\":\"接收成功\"}`，**不解析、不落库、不改任何数据**"
        "（spec §D9 / §6-7 第二节）⇒ 没有可留痕的业务动作"
    ),
    # ⭐ 图片存储线：本机/CI 专用的直存端点（`local` 后端才登记路由）。
    ("put", "api/media/blob/<path:key>"):
        "本机存储后端专用的直存端点：仅 `MEDIA_STORAGE_BACKEND=local` 时登记路由，生产不存在；"
        "开发/CI 的落盘动作不是业务动作，故不记操作日志",
    # ⭐ 图片存储线：签发上传凭证 / 认领落库**刻意不记**（spec §9.2 的裁定）。理由：这两个动作只产生「一张图被挂到订单上」，而**不可恢复**的动作是删除（它已在 `INSTRUMENTED` 里指向 `order.delete_screenshot`）。⚠️ 本仓只给删除留痕，是为了不让操作日志被高频上传刷满。
    ("post", "api/business/orders/<order_no:order_no>/screenshots/ticket"):
        "签发上传凭证：只落一条 `pending` **中间态**行并给出直传地址——它**不是业务结果**"
        "（图还没传上来、也还没归到订单上），故不记操作日志；"
        "⚠️ 如实说明：这个端点的**确有**一次 `INSERT`（评审指正过），理由是它是上传过程的一部分，"
        "而 spec §9.2 只给「不可恢复的删除」留痕",
    ("post", "api/business/screenshot-browser/start"):
        "生成外部浏览器短时上传凭据，只写临时授权",
    ("post", "api/business/screenshot-browser/exchange"):
        "兑换一次性上传凭据，只更新临时授权",
    ("post", "api/business/screenshot-browser/ticket"):
        "外部浏览器上传取票，沿用既有截图上传留痕口径",
    ("post", "api/business/screenshot-browser/claim"):
        "外部浏览器上传认领，沿用既有截图上传留痕口径",
    ("put", "api/business/screenshot-browser/blob/<int:screenshot_id>"):
        "临时接收已签发的进度图片，沿用截图上传留痕口径",
    ("post", "api/business/orders/<order_no:order_no>/screenshots"):
        "认领刚直传上去的对象：把 `pending` 行置为 `ready`，属**可重复的上传动作**；"
        "不记操作日志（同上，spec §9.2）",
    # ⭐⭐ 开放平台 v2（设计文档 §13.10-3）：对外的**取票 + 认领**两条端点。它们是上面内网那两条的**对外开放版**，而且**调的就是同一个领域函数**（`apps/business/api/screenshot_views.py` 的 `issue_screenshot_ticket` / `claim_screenshot`）⇒ 留痕口径与内网逐字相同：`spec §9.2` 只给「不可恢复的动作」留痕（本域里那条是**删除**订单截图），高频上传记了只会把操作日志刷满。
    #    ⚠️ **如实说明两条**（⛔ 别把这两条读成"漏埋点"）：① **取票**那条确有**一次 `INSERT`**（落一行 `pending`）与一次预签名，但它不是业务结果（图还没传上来、也还没归到订单上）；② **认领**那条把行置成 `ready`（图确实归到订单上了，并**两笔入队**：peer 链路广播 + 开放平台推送 `order.image_added`）——「有副作用」**不等于**「要记操作日志」：本仓按**动作的可恢复性**分档，⛔ 不是按"有没有写库"。
    #    ⚠️ **为什么登记在这里而不是 `INSTRUMENTED`**：后者要求该 HTTP 方法的代码路径上**真的有一次 `record(...)`**（`_certified_as_instrumented` / `test_instrumented_endpoints_really_call_record`），而这两条路**不写**审计（给它们新增动作码 = 六处登记的**扩大改动面**，按 `AGENTS.md` §5-G 不属单端点自行决定）⇒ 下面两条让它们**被分类**：⛔ 不落进"既不在只读表、也不在写表"的静默空洞（`test_operation_log_coverage` 的 `test_every_enumerated_write_endpoint_is_classified` 会先红）。
    ("post", "api/open/v1/orders/<order_no:order_no>/images/ticket/"):
        "对外开放平台**取上传票**：只落一条 `pending` **中间态**行 + 一句预签名直传地址，"
        "**不是业务结果**（与内网 `screenshots/ticket` 是**同一个领域函数**、同一条口径，spec §9.2）",
    ("post", "api/open/v1/orders/<order_no:order_no>/images/"):
        "对外开放平台**认领**：把 `pending` 行置为 `ready`（与内网 `screenshots` 的认领是"
        "**同一个领域函数**）⇒ 属**可重复的上传动作**，不记操作日志（同上，spec §9.2）",
    # ⭐ 实名资源包配置线（spec §2.4）：租户管理员录入平台密钥包。**刻意不记**操作日志，三条理由（如实登记，⛔ 不是漏埋点）：① 写的是**平台侧资源**（`public` 的 `platform_realnamepackage`），不是本租户的业务数据——操作日志的语义是「谁在本租户干了什么」；② 动作**可逆**：包可停用、可另录新包（不可恢复的动作才必须留痕，spec §9.2 同一口径）；③ 动作码没有对标出处（对标本页没有配置入口、是站外购买）⇒ 新拟一个动作码要动 `ACTION_TEMPLATES` + 计数钉子，不属单端点自行决定。
    # ⚠️ `GET realname-packages/` 是**读**端点，本表只收写方法、不用登记。
    ("post", "api/accounts/realname-packages/"):
        "录入平台侧实名密钥包：写的是平台资源（public 表、非租户业务数据），动作可逆"
        "（包可停用、可重录），且动作码无对标出处（新拟动作码不属该线自行决定）"
        "⇒ 刻意不记操作日志（spec §2.4 / spec §9.2 的「只给不可恢复的动作留痕」同口径）",
    # ⭐ 图片存储线：改头像 / 改微信收款码**刻意不记**（spec §9.2 的裁定）。⚠️ 这两条与「订单截图删除记一条」是同一条口径的两面：**只给不可恢复的动作留痕**；换头像 / 换收款码都是可随时重做的自助动作，记了只会把操作日志刷满。
    ("post", "api/auth/profile/media/<str:kind>/ticket"):
        "签发用户媒体（头像 / 微信收款码）的直传凭证：只发一张一次性上传地址，"
        "**没有业务实体变更**；不记操作日志（spec §9.2）",
    ("post", "api/auth/profile/media/<str:kind>"):
        "认领用户媒体：把对象名写进档案的两个媒体列，属**可重复的自助动作**；"
        "不记操作日志（spec §9.2 只给不可恢复的动作留痕）",
    # ⭐ 开放平台 v2（设计文档 §5.1）：**取令牌**端点。它是**凭据交换**：拿 `client_id` / `client_secret` 换一个 7200 秒的短期令牌，**不产生任何业务副作用**——不建单、不改单、不删任何数据，也不改应用的任何配置列（`last_used_at` 那一格是**认证层**在业务端点上写的，本端点 `authentication_classes = []` ⇒ 连那一次都不写）。唯一"产生"的东西是一个可以被重新签发的令牌本身。
    # ⚠️ 它与上面几条 `test-connection/` 的**形态不同、别写成一回事**：那些是**只读上游**，本条连上游都不碰，是**纯本地凭据交换**。⛔ 将来若给它加上"令牌台账"之类的库写入，这条登记**必须同一次**重审（那时它就有一条可留痕的业务动作了）。
    ("post", "api/open/oauth/token"): (
        "开放平台取令牌端点（v2 §5.1）：**凭据交换**——用 `client_id` / `client_secret` "
        "换一个短期令牌，不产生任何业务副作用（不建 / 不改 / 不删任何业务数据，"
        "也不写应用的配置列）⇒ 没有可留痕的业务动作，记一条只会是「改了什么」的假陈述。"
        "⚠️ 它是**机器对机器**的入口（没有页面操作人），与上面几条 `test-connection/` 不同之处："
        "那些是只读上游，本条不碰任何上游"
    ),
    # ⭐⭐ 打手侧开放接口（spec `2026-10-08-taker-open-api-design.md` §2 / §8）：**打手侧的取令牌**端点。逐条口径与上面那一条（上家侧）**同款**，但**两族各自登记**：⛔ 这两条不是"同一件事写两遍"——它们是**两条独立的对外契约**，各自可能单独变化（打手侧将来要加令牌台账，只动这一条；上家侧不受影响）。
    # ⚠️ 与上家侧那条的差别只有**凭据表**（`TakerApp` vs `ApiApp`）与**路径前缀**：形态（OAuth2 标准体 / `authentication_classes = []` / 纯本地凭据交换）逐字相同。⛔ 将来若给它加上"令牌台账"之类的库写入，这条登记**必须同一次**重审——与上家侧那条同一个触发条件。
    ("post", "api/taker/oauth/token"): (
        "打手侧取令牌端点（spec §2）：**凭据交换**——用 `client_id` / `client_secret` "
        "换一个短期令牌，不产生任何业务副作用（不建 / 不改 / 不删任何业务数据，"
        "也不写打手侧应用的配置列）⇒ 没有可留痕的业务动作，记一条只会是「改了什么」的假陈述。"
        "⚠️ 它是**机器对机器**的入口（没有页面操作人），且是**纯本地凭据交换**（不碰任何上游）；"
        "与上家侧 `api/open/oauth/token` 那一条**同款但各自独立**（两族两条契约）"
    ),
    # ⭐⭐ 大厅**模拟单**假接手（D-0296）：**刻意不记**。这一跳只把一张**演示卡**（`HallSimulatedOrder`）置 `retired`——模拟单不建订单行、不进状态机 / 结算 / 对账（模型 docstring 的硬边界），「谁把哪张演示卡点掉了」没有审计价值；且无论结果都回 `1012`（设计如此），记一条「改了什么」只会是假陈述。⛔ 将来若模拟单接进任何真资金面，这条登记必须同一次重审。
    ("post", "api/business/hall/sim/<int:pk>/claim/"): (
        "大厅模拟单假接手（D-0296）：只把一张**演示卡**下架（active → retired），"
        "不建订单行、不进状态机 / 结算 / 对账，且无论结果都回同一封「已被接手」信封"
        "⇒ 没有可留痕的业务动作"
    ),
    # ⭐ 问题反馈线（spec `2026-10-08-feedback-design.md` §8 的前两行）：
    #    提交与签发上传凭证**刻意不记**，处置那一跳**记**（见上面的 `INSTRUMENTED`）。
    ("post", "api/business/feedback/"):
        "提交反馈**刻意不记**（spec §8 第 1 行）：这一行自己就带着「谁、什么时候、报了什么」"
        "（`Feedback.created_by` + `created_at` + `content`），再记一条操作日志是把同一事实"
        "写两处（E-0021 族），而提交是**高频**动作——记了只会把操作日志刷满。"
        "⚠️ 与「反馈是审计痕迹、不做删除」不冲突：留痕由 `Feedback` 那张表承载。",
    ("post", "api/business/feedback/media/ticket/"):
        "签发反馈截图的上传凭证**刻意不记**（spec §8 第 2 行）：只发一张一次性上传地址，"
        "**没有业务实体变更**；与图片存储线两处先例（订单截图 ticket / 用户媒体 ticket）"
        "同一口径（spec §9.2 只给不可恢复的动作留痕）。",
    # ⭐ 打手侧开放接口线的**对外取票 + 认领**两条：**照上家侧的先例**（`api/open/v1/orders/<order_no>/images/ticket/` 与 `.../images/` 那两条，它们登记在同一张表里），逐条给出理由：① **同一个函数**：这两条调的正是 `apps/business/api/screenshot_views.py` 的 `issue_screenshot_ticket` / `claim_screenshot`（内网那两个端点也调它）——而那两条**本来就不记**（内网那两个端点早就在本表里，理由 = `spec §9.2` 只给不可恢复的动作留痕）；既然判据与库写入是**同一份**，留痕口径也必须同一份（⛔ 不许"内网不记、对外记"——那会让同一个动作在两条路上有两种审计行为）。② **新开动作码是"扩大改动面"动作**：要同一次改 `apps/audit/actions.py` 的模板表 + `tests/test_operation_log_model.py` 那几条字面量计数 ⇒ 按 `AGENTS.md` §5-G 的升级规则不属单端点自行决定。
    #   ⭐ **代价补偿（照上家侧那条，⛔ 别只读成"没留痕"）**：两条都套着幂等入口 ⇒ 每一笔成功调用都在 `TakerApiIdempotency` 里留下一行（`app` + `method` + `path` + `created_at`），而 `TakerApp` 上有 `name` / `app_key`；行上的 `uploaded_by`（= 绑定账号）是第二重指纹 ⇒「哪张图、什么时候、用哪套凭据认领的」**对得上**。⛔ 但也别写成"一定有留痕"：那两行**确实不在** `OperationLog` 里。
    ("post", "api/taker/v1/orders/<order_no:order_no>/images/ticket/"):
        "打手侧对外**取票**端点（T6，spec §5 端点表第 6 行）**刻意不记**："
        "它与内网 `ScreenshotTicketView.issue_ticket`、上家侧取票端点调的是**同一个**"
        "`issue_screenshot_ticket` ⇒ 沿用同一条留痕口径（spec §9.2 只给不可恢复的动作留痕；"
        "那两条内网端点早就在本表里）。⚠️ 只发一张一次性上传地址、那一行还是 `pending`，"
        "**没有终态的业务实体变更**。补偿：幂等台账 `TakerApiIdempotency` 那一行 +"
        "行上的 `uploaded_by`（= 绑定账号）。",
    ("post", "api/taker/v1/orders/<order_no:order_no>/images/"):
        "打手侧对外**认领**端点（T6，spec §5 端点表第 6 行）**刻意不记**：理由与上面取票那条"
        "逐字同源（同一个 `claim_screenshot`、同上家侧认领端点与内网认领端点的同一条口径）。"
        "⚠️ 它的库写入（`pending → ready` + 两笔入队广播）**是可追溯的**：行上有 `uploaded_by`"
        "（= 绑定账号）与 `ready_at`，幂等台账那行有 `app` / `path` / `created_at`。",
    # ⭐⭐ QQ 机器人：设置页的写端点里**只有 PATCH binding 记动作**（在上面的 `INSTRUMENTED`，去向与理由登记在那边）；其余**刻意不记**。理由分三组（每组都照既有先例，⛔ 不是漏分类）：① **租户内配置行**（remark / push-config / join-policy / menu 的本地格）：反复覆盖的配置，没有「不可恢复的动作」，记了只会把操作日志刷满（先例：`order-push-settings` / `self-registration/config` 与图片存储线 §9.2 的口径）；② **自带留痕的写入**（test-push 建 `QQBotOutboundMessage` 行、approve 建 `QQBotJoinRequestLog` 行）：「谁、什么时候、对哪个群、结果如何」由那一行自己承载（`created_at` / `decision` / `error_code`），再记操作日志是把同一事实写两处（先例：`SelfRegistrationInvite` 行）；③ **POST 形状的读**（share-link 只调上游换一个链接，不落任何业务数据）：与 `read-platform-order/` 同类（spec §5.7 第 5 行）。
    # ⚠️ 拼写照**实测**（先跑一次覆盖网读它给出的键）：这批路由是**显式 `path()`**（不是 router 生成）⇒ 归一化**保留**转换器前缀 `<int:pk>`（与 `api/taker/apps/<int:pk>/…` 那条同款；router 生成的路由才是 `<pk>` 形状）。
    ("patch", "api/qqbot/groups/<int:pk>/"): (
        "群备注是租户内的标注列（官方投影列不可写）：反复覆盖的配置行，无可留痕的终态实体变更"
        "（与 `order-push-settings` 同组先例）"
    ),
    ("put", "api/qqbot/groups/<int:pk>/push-config/"): (
        "推送配置是租户内配置行（OneToOne 整份替换、反复覆盖），与 `order-push-settings` "
        "那条「刻意不记」的裁定同口径"
    ),
    ("post", "api/qqbot/groups/<int:pk>/test-push/"): (
        "发送一条测试消息，不改任何业务实体（与 `order-push-settings/<pk>/test/` 同款理由）；"
        "行落库事实由 `QQBotOutboundMessage` 那一行自己承载（kind=test_push、created_at）"
    ),
    ("post", "api/qqbot/groups/<int:pk>/join-requests/approve/"): (
        "审批动作**无论成败**都写一行 `QQBotJoinRequestLog`（decision / reject_reason / "
        "error_code / acted_at）——留痕表自己就是审计面，再记操作日志是把同一事实写两处"
        "（先例：`SelfRegistrationInvite` 行）"
    ),
    ("patch", "api/qqbot/groups/<int:pk>/join-policy/"): (
        "入群审批策略是租户内配置列（mode 枚举 + JSON 白名单，反复覆盖），"
        "与 `order-push-settings` 同组先例"
    ),
    ("put", "api/qqbot/menu/"): (
        "群菜单写的是**远端**配置（`put_menu` 代理）；本地只有 `menu_version` 一格缓存列"
        "（ws/worker 的读取缓存，不是业务实体变更），记操作日志只会是「改了什么」的假陈述"
    ),
    ("post", "api/qqbot/share-link/"): (
        "调上游换一个分享链接、**不落任何业务数据**（与 `read-platform-order/` 同类，"
        "spec §5.7 第 5 行）"
    ),
    # ⭐ D-0303（团团通大厅镜像 v1）：下家镜像行的**确认制抢单**两条（内网管理面 + 对外 taker API，同一个领域口 `peer_hall.claim_peer_mirror`）。刻意不记：该动作此刻**还没有订单**可记——只做镜像行预占（`claiming_by` / `claiming_at` 两列本身就是这条动作的留痕，不是「改完什么都没留」）+ 排一条 `hall.claim` 事件；正式订单由上家 `order.assign` 推回才落地，那条链路的状态写入（`peer_inbound.apply_events`）已有各自的留痕与写入点网登记。

    ("post", "api/business/hall/peer/<int:pk>/claim/"): (
        "确认制预占无订单可记（镜像行 claiming 两列即留痕；转正留痕在 inbound "
        "order.assign 链路）"
    ),
    ("post", "api/taker/v1/hall/peer/<int:pk>/claim/"): (
        "同上（同一领域口 `peer_hall.claim_peer_mirror` 的对外形状）"
    ),
}

# ---------------------------------------------------------------------------
# 3. 挂账（spec §5.8）—— 动作 → DeferredAction
# ---------------------------------------------------------------------------

# ⚠️ 三个字段各有用途，缺一不可：`future_endpoint`：功能落地后**会存在**的端点路径（或 `None`，此时见下面第三条）；`future_endpoint_kind`：那个端点是 **"read"** 还是 **"write"**——⚠️ **必须显式写**（`test_deferred_actions_are_explicitly_kinded` 钉住）：端点落地时是**读**形状的挂账项若只在写端点集合里查「端点是否出现」，它**永远不会红**；今天挂账断言因此打在**端点全集（含读）**上，kind 只作形状登记、**不**用来选集合。⚠️ 今天本表只有 2 条：`user.rate_stat`（有 `future_endpoint`、`kind="read"`）与 `library_5.all`（在 `DEFERRED_WITHOUT_ENDPOINT` 豁免集合里，两字段都是 `None`）。`why_pending`：为什么挂账（非空）。
# ⚠️ `future_endpoint` 只写**路径**、不写 HTTP 方法：今天是纸面推断，写死方法会假装精确。kind 是粗粒度的事实（导出/统计是读、增删改是写），这个粒度今天能站得住。
DeferredAction = namedtuple(
    "DeferredAction", ["why_pending", "future_endpoint", "future_endpoint_kind"]
)

DEFERRED_ACTIONS: dict[str, DeferredAction] = {
    # ⭐ 曾挂账的动作（`order.export` / `performance.settle` / `performance.export` / `performance.delete` / `user.self_update` / `user.switches` / `order.delete` / `order.restore` / `system.order_source_*`）均已随功能落地收口移出：去向登记在 `RESOLVED_ACTIONS` 或各自埋点处的注释里，不再留在本表。
    # ⛔ 挂账项的「预测端点」必须同时写对 **① 逐字路径（含尾斜杠）** 与 **② HTTP 语义（读 / 写）**，两条都按**实测**填（先跑一次覆盖网 / `resolve()` 读它给出的键），不按推断填——历史上两条挂账分别错在这两个维度（提醒**红对了结果、红错了理由**，真正该盯的写端点反而在视野外）。
    "user.rate_stat": DeferredAction(
        "完单率统计（前端 `notOpen` 占位，spec §5.8）",
        "api/accounts/rate-stat/",
        "read",
    ),
    # ⭐ 订单来源 CRUD / 店铺对接 / 软删恢复 / 绩效删除等曾挂账项均已收口（功能已落地并埋点，不回到本挂账表；挂账的定义是「对标有、我方功能未落地」）。`user.switches` 的去向见 `RESOLVED_ACTIONS`（它预测的 `api/accounts/<pk>/switches/` 端点**不会出现**；它**不是**「刻意不记」、也**不是**「不做」——三张表的区别见那一节）。
    "library_5.all": DeferredAction(
        "「5 号库」模块我方不存在（spec §5.6 / §5.8；对标本租户实测 0 行）",
        None,
        None,
    ),
}

#: **明确不会有端点**的挂账项（豁免集合，必须**显式枚举**）。⚠️ 不能用「`future_endpoint is None` 就自动豁免」——那是个文字后门：任何一条都能靠改写成 `None` 永久逃过「上线必须补埋点」的提醒。
#: 新增豁免必须是有意为之，并写清理由；`test_deferred_actions_are_explicitly_kinded` 会检查「在集合里 ⇒ 两个字段都为 None」「不在集合里 ⇒ 两个字段都必须有」。
DEFERRED_WITHOUT_ENDPOINT: frozenset[str] = frozenset({
    # 「5 号库」整块模块都不存在：不是少一个端点，是整条功能线没有落点。
    "library_5.all",
})

# ---------------------------------------------------------------------------
# 3b. 挂账的**收口**（动作 → 去向）—— 既不是挂账，也不是「刻意不记」
# ---------------------------------------------------------------------------

#: 曾经躺在 `DEFERRED_ACTIONS` 里、今天**去向已经定死**的动作 → 去向说明。
#: **为什么要有这一节**（它存在的全部理由就是「这三种状态不能互相冒充」）：留在 `DEFERRED_ACTIONS` 里 = 声明「对标有、我方功能未落地」⇒ 会让下一个人以为**还有活没干**（本节的第一个成员正是这个形状：4 个开关里有 2 个早就上线了）；塞进 `DELIBERATELY_NOT_LOGGED` = 那张表断言的对象是**真实存在的写端点**（`test_deliberately_not_logged_entries_exist_and_are_disjoint`），而这里这些动作的**端点根本不存在** ⇒ 放进去当场红；塞进 `NOT_BUILT_ACTIONS` = 那张表是「**没有**上线计划」的「不做」，而本节的动作里有**一部分已经上线并被使用**（只是记在别的动作名下）。
#: ⚠️ 与 `DEFERRED_WITHOUT_ENDPOINT` 的「显式枚举」同一条规矩：本表的键集合由 `test_resolved_actions_are_explicitly_registered` 用**等号**钉住，并要求它与 `DEFERRED_ACTIONS` / `NOT_BUILT_ACTIONS` **互斥**；把某条**同时**留在挂账表里，正是「收口做了一半」的形状（变异证明：还原一条挂账条目 ⇒ 该用例红）。⚠️ **不**为「与 `DELIBERATELY_NOT_LOGGED` 不冲突」写断言：那张表的键是 `(方法, 路径)`、这里的键是动作名，两者交集**恒为空**——恒真的自指断言（E-0065 同族），刻意不写。⚠️ **它不承接「上线必须补埋点」的提醒**（那是 `DEFERRED_ACTIONS` 的职责）：这里动作去向已定——**有的并进别的动作名下、有的没有端点；我的资料线起也允许「有端点且已埋点」的收口**（`user.self_update` 就是这一档，端点 `PATCH api/auth/profile` 登记在 `INSTRUMENTED` 里）⇒ 它不需要、也**不许**再要一条 `future_endpoint`（那会让「收口」与「挂账」两张表的区别再次含混）。
RESOLVED_ACTIONS: dict[str, str] = {
    "user.self_update": (
        "端点已上线：`PATCH api/auth/profile`（我的资料线，2026-09-24）。"
        "⚠️ 实际路径是 `api/auth/profile`，**不是**本表原先预测的 `api/auth/me/profile/`"
        "（预测拼错在本文件已有两次先例：performance.settle / performance.export）。"
        "埋点动作 `user.self_update` 已登记进 `INSTRUMENTED`，模板逐字照抄对标"
        "（`apps/audit/actions.py::USER_SELF_UPDATE`）。"
    ),
    "user.switches": (
        "收口（Task 10，2026-09-23）：对标那句「更改了{名}的禁止抢内部订单的开关」对应的 4 个开关，"
        "去向逐条定死 ⇒ 本动作**不再挂账**、它的端点（`api/accounts/<pk>/switches/`）**不会出现**。"
        "①「禁止结算」`StaffProfile.settle_forbidden` 与 ②「禁抢内部单」（对标那列的现名是"
        "「**禁自助接单**」，D-0091 改准过术语）`StaffProfile.grab_forbid_self_service`："
        "**已落地**，写入走**既有的** "
        "`PATCH/PUT api/accounts/<pk>/`（记账动作 `user.update`，见 `INSTRUMENTED`）"
        "——**不新增** `user.switch_*` 动作、**不新增** `switches/` 端点"
        "（计划 §7 曾提议拆成两个动作，执行期没有采用；见 §7 的更正块）。"
        "③「免登录验证」与 ④「客户电话隐藏」：业务方 2026-09-22 **明确删掉**（不是「还没做」）。"
        "⚠️ 原文里「`StaffProfile.no_sms_on_intake` **不在**这 4 个之列」这句仍然成立，"
        "但该字段本身已按 D-0086 彻底删除（迁移 `accounts/0002`）⇒ 今天账号线能顶替它的东西也没有了。"
        "📌 **合并 `main` 的证据（2026-09-23）**：`main` 那一侧当时把这条挂账**收窄**而不是收口"
        "（它保留了 `DEFERRED_ACTIONS[\"user.switches\"]`），并给出那两个开关的落库提交号 "
        "`b0cbdeb` + `423dd61`——**上面「已落地」那一句的事实依据就是它们**。"
        "本线取**收口**口径（移出挂账、登记到本表）：两张表由 "
        "`test_operation_log_coverage.py::…resolved…` 断言**互斥**，两边都留会当场红；"
        "而 `main` 那条「仍断言该端点不存在」的判据**一个字都没丢**——它落在本表的 "
        "`switches/` 端点上（同一件事，换了登记处）。"
    ),
}

# ---------------------------------------------------------------------------
# 4. 「不做」—— 与挂账不同：**没有**上线计划
# ---------------------------------------------------------------------------

# 单独一张表：它既不是「刻意不记的**现有**写端点」（端点不存在），也不是「挂账」（不会上线，所以不该带「上线时必须补埋点」的提醒）。登记在这里只为让读者看到分类完备。
#: `user.delete` 不在这张「不做」表里。硬删做不了（`Performance.user` 与 `Order.sales_user` 都是 `on_delete=PROTECT`），落成可恢复的软删：`POST api/accounts/<pk>/delete/` 与 `.../restore/`（见 `INSTRUMENTED`）。硬删仍然不做；「停用」不再是唯一移除路径。
NOT_BUILT_ACTIONS: dict[str, str] = {
    "order.change_price_no_assign": "spec §5 标「不做」的动作；今天没有写入者，也没有端点",
}
