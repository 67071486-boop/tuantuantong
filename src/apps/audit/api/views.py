"""`GET /api/audit/logs/`：操作日志检索（spec §6.1 / §10.6；尾斜杠见 R18）。 `GET /api/audit/actors/`：人员枚举（spec §6.3）。 `POST /api/audit/events/`：前端上报的读操作（spec §6.2 / §7.3）。
`GET /api/audit/order-action-logs/`：交易订单操作记录（spec `2026-10-04-trade-action-log-design.md`）。 **租户边界（AGENTS.md §4 硬性）**：四条路径都在**当前连接**上跑——日志表 `OperationLog` 与 `auth_user` 都建在租户 schema 里（`apps.audit` 在 `TENANT_APPS`；
`django.contrib.auth` 同时在 `SHARED_APPS`/`TENANT_APPS`，租户 schema 各有一份）， schema 由中间件从**认证态的宿主域名**钉在连接上。端点**不接受**任何 tenant / schema 参数：序列化器里没有这两个键，未知参数一律被忽略（不是「读了再校验」，是**根本没有入口**）。
"""
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils.functional import cached_property
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from apps.accounts.display import display_names_for_usernames
from apps.audit.api.serializers import (
    LOG_ORDERING_FIELDS,
    OperationLogActorRowSerializer,
    OperationLogActorSearchSerializer,
    OperationLogEventSerializer,
    OperationLogRowSerializer,
    OperationLogSearchSerializer,
    OperationLogWithDeleteReasonSerializer,
    OrderActionLogRowSerializer,
    OrderActionLogSearchSerializer,
)
from apps.audit.actions import ORDER_ACTION_EXCLUDED, ORDER_ACTION_LABELS
from apps.audit.display import action_label, actor_display_names
from apps.audit.log import record_order_read
from apps.audit.models import OperationLog
from apps.business.api.base import EnvelopePagination
from apps.business.models import Order
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 人员枚举的硬上限（spec §6.3）：51 个命中也只回 50 条，超出由前端继续输入收窄。
#: 它作用在**查询**上（SQL `LIMIT`），不是「取回来再切」。
ACTOR_LIMIT = 50


def _order_backfill(rows):
    """`order_no` → 订单当前值（`game_name` / `source_order_no`）的**一次性**映射（spec D7）。 **为什么需要它**：`game_name` 在 `order.*` 行上几乎恒空、`source_order_no` 只有少数写点会填 ⇒ 不回查就是两根永远空着的柱子。 ⭐ **`source_order_no` 的日志行写点枚举（评审 F-1 括号配平全扫 `record(...)` 实得 5 个；⛔ 旧句「只有 `order.create`」失实；写点的 `文件:行` 以本处为维护出处，代码挪行时先改这里再同步引用方）**： ① `order.create`（录单建单）`business/order_intake.py:321`； ② `order.create`（拆分子单）`business/api/order_views.py:2142`； ③ `order.update`（拆分首段改项目）`business/api/order_views.py:2116`；
    ④ `order.ship_result`（发货回执，`xgj_ship.py::_write_order_log`）`business/xgj_ship.py:207`； ⑤ peer 同步族（下行/上行/截图等，`peer_inbound.py::_record_peer_sync`）`business/peer_inbound.py:1142`。 **方向性结论不变**：绝大多数日志行走的是非 create 写点 ⇒ 该列多数为空，回填仍有必要。 **先例**：同族的 `actor_display_names`（`apps/audit/display.py`，本文件上方 import）就是「读时批量解析」，本函数照它的形状写。 ⚠️ **一次查询**（`order_no__in` + JOIN `game`），⛔ 不许在序列化器里逐行 `Order.objects.get`：一页 200 行会打 200 次库。 ⚠️ 只取「日志列缺值」的行去查（本行两列都有值就没必要回查），`order_no` 为空的行（如 `order.export`，spec D15）不进查询 ⇒ 序列化器保持空串。
    ⚠️ 用**默认 manager**（`Order.objects`）：已软删的单不在回填范围里——「当前值」取的是仍活着的订单（D-0144 之下软删单在常规读面上就不存在）。查不到 ⇒ 保持空， ⛔ 不为它造占位值。
    """
    order_nos = {
        row.order_no
        for row in rows
        if row.order_no and (not row.game_name or not row.source_order_no)
    }
    if not order_nos:
        return {}
    return {
        order_no: {
            "game_name": game_name or "",
            "source_order_no": source_order_no or "",
        }
        for order_no, game_name, source_order_no in Order.objects.filter(
            order_no__in=order_nos
        ).values_list("order_no", "game__name", "source_order_no")
    }


class OperationLogListView(generics.ListAPIView):
    """6 个筛选 + 项目统一分页形状 `{items,total,page,page_size}`。

    准入是 RBAC 节点 `system.logs`（spec A10）。⚠️ `has_node` **不看 `available`**
    （`apps/rbac/permissions.py` 的模块 docstring），所以目录里这条还是 `False` 也照样拦得住。
    """

    permission_classes = [has_node("system.logs")]
    pagination_class = EnvelopePagination
    serializer_class = OperationLogRowSerializer

    @cached_property
    def _validated_search_params(self):
        """查询参数**唯一**的校验口（E-0010）。返回 `validated_data`。空串按「没传」处理：前端清空筛选栏时会送 `?stime=` / `?game_name=`，那不是非法值而是「没选」。口径与三处既有先例一致
        （`business/api/order_views.py`、`business/api/performance_views.py`、 `accounts/views.py` 的 `raw = {k: v … if v != ""}`）。
        """
        query = self.request.query_params
        raw = {name: value for name, value in query.items() if value != ""}
        # `actor` 多选（spec §6.1）：`query.items()` 对重复键只留**最后一个**值，
        # 所以重复参数必须走 `getlist`；逗号分隔是 xm-select 的另一种提交形状，一并展开。
        actors = [
            part.strip()
            for value in query.getlist("actor")
            for part in value.split(",")
        ]
        actors = [actor for actor in actors if actor != ""]
        if actors:
            raw["actor"] = actors
        params = OperationLogSearchSerializer(data=raw)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`（E-0010）。
        params.is_valid(raise_exception=True)
        return params.validated_data

    def get_serializer_class(self):
        if self._validated_search_params.get("include_delete_reason"):
            return OperationLogWithDeleteReasonSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        data = self._validated_search_params
        qs = OperationLog.objects.all()  # 见模块 docstring：租户由连接（认证态）决定

        stime = data.get("stime")
        if stime is not None:
            qs = qs.filter(created_at__gte=stime)
        etime = data.get("etime")
        if etime is not None:
            qs = qs.filter(created_at__lte=etime)

        log_type = data.get("log_type")
        # `0` = 全部（spec §6.1）；判据用 `is not None` 显式区分「没传」与「传了 0」
        # 之后再单独排掉 0——不要只写 `if log_type:`，那正是 E-0021 的形状。
        if log_type is not None and log_type != 0:
            qs = qs.filter(log_type=log_type)

        game_name = data.get("game_name")
        if game_name is not None:
            qs = qs.filter(game_name=game_name)

        actors = data.get("actor")
        if actors:
            qs = qs.filter(actor__in=actors)

        order_no = data.get("order_no")
        if order_no is not None:
            # ⭐（D-0301）：双列 OR——系统内置号照旧精确命中，来源单号
            # （录入的自由文本，`OperationLog.source_order_no`）也能命中。空串在
            # `_search_params` 入口已按「没传」丢掉 ⇒ 此处非空。
            qs = qs.filter(Q(order_no=order_no) | Q(source_order_no=order_no))

        key = data.get("key")
        if key is not None:
            qs = qs.filter(summary__icontains=key)

        # 排序（spec §6.1 / §16.2 第 1 条）：`ordering` 已在序列化器里过白名单，这里再经 `LOG_ORDERING_FIELDS` 映射成**我们自己的**字段元组交给 `order_by`
        # （不是把请求值直接下发——那是 AGENTS.md §5 的 E-0010 形状）。元组的第二项是稳定 tie-break：同一 `created_at` 的多行在翻页时次序不漂。
        ordering = data.get("ordering")
        if ordering is not None:
            qs = qs.order_by(*LOG_ORDERING_FIELDS[ordering])
        # 没传 `ordering` ⇒ 不加 `order_by`，保持 `Meta.ordering = ["-created_at", "-id"]`
        # （最新在前）——请求形状与行为跟加本参数之前**逐字节相同**。
        return qs

    def get_serializer_context(self):
        """把当前页的 `actor_id` → 昵称映射与 `order_no` → 订单当前值映射塞进 context。 ⚠️ 时点：`ListModelMixin.list` 先 `paginate_queryset`（把页面挂在 `self.paginator.page` 上）再 `get_serializer` ⇒ 这里能拿到**当前页**的行，两个映射各只查当前页（一页各一次查询）。
        ⚠️ 分页关闭时 `self.paginator` 可能没有 `page` ⇒ 空列表 ⇒ 映射为空 ⇒ 序列化器全部回退 `actor` 列 / 快照原值（fail-safe，不炸；本端点的分页始终开着，这条只是兜底）。
        """
        context = super().get_serializer_context()
        page = getattr(self.paginator, "page", None) or []
        context["actor_display_names"] = actor_display_names(page)
        # ⭐（D-0341）：「来源订单号」列的读时回填（D7 口径，与下方 `OrderActionLogListView` 同一份 `_order_backfill`、同样一页只查一次）——日志行的 `source_order_no` 只有少数写点会填（全扫实得 **5 个写点**，逐条
        # `文件:行` 见 `_order_backfill` docstring；评审 F-1 改准，⛔ 不是只有 `order.create`），不回填则该列对绝大多数订单类日志恒空。序列化器只消费映射里的 `source_order_no` （`game_name` 本页仍显示快照原值，不在本线范围内）；内置单号 `order_no`
        # 由载荷原值直出、前端兜底（D-0301 的分工）。
        context["order_backfill"] = _order_backfill(page)
        return context


def order_action_rows():
    """「交易订单操作记录」页的**行集合**（本页唯一的定义处，三处调用共用它）。 = `action` 以 `order.` 开头 **减去** `ORDER_ACTION_EXCLUDED`（`apps/audit/actions.py` 的显式枚举，今天只有 `order.view_credentials` 一条 —— 业务方裁定：那是「查看账号密码」的读动作，不属于「对订单做的操作」）。 ⚠️ **三处共用一处定义**（列表 / 右侧「操作统计」的底集 / 「操作人员」候选的底集）：各自抄一遍 `filter(...).exclude(...)` 必然漂移，而漂移的症状是**静默的**
    （下拉里出现一个选了永远没结果的动作、或统计里多一条列表里看不到的）， E-0021 的同族。 ⚠️ `exclude(action__in=…)` 在空集合时是**空条件**（Django 会把 `__in=[]` 编译成恒假、但 `exclude` 恒假条件 = 全部放行）—— 今天集合非空，且结构网钉住「排除项必须真的在前缀集合里」，所以不会退化成静默空集。
    """
    return OperationLog.objects.filter(action__startswith="order.").exclude(
        action__in=ORDER_ACTION_EXCLUDED
    )


class OrderActionLogListView(generics.ListAPIView):
    """「订单管理 → 交易订单操作记录」的数据源（spec `2026-10-04-trade-action-log-design.md` §4）。 ## 行集合 = `action` 以 `order.` 开头（spec D1） ⛔ **不用 `log_type=10`**：那会依赖「今天 27 条 `order.*` 恰好都登记成 10」这个**巧合** （设计 spec §2.2 实测过两者双向无差，但那是登记表的巧合、不是契约）。用 `startswith` 语义直白，且吃得到 `action` 的 `varchar_pattern_ops` 索引。 ⚠️ 「今天两者等价」由**同批**的结构网钉住（`tests/test_operation_log_model.py`）；将来有人加一个非 `order.*` 的 10 类动作时它会红，逼他决定要不要收进本页。 ## 准入是 RBAC 节点 `order.platform_action_logs`（spec D2） **写在类体上**——RBAC 接线网的 T2 位点锚点就是本类的 `permission_classes`。
    ⛔ 不复用 `/api/audit/logs/`（那个端点挂 `system.logs`）：复用它会让「只持本页节点的人 403、只持 `system.logs` 的人白看本页数据」。**代价（如实登记）**：同一张表两个准入门，本仓第一次（设计 spec §6-1）。 ## 载荷：统一信封 + 分页的 `data` 里多 `summary` / `actors` / `actor_options` / `actions` - `summary`（右侧「操作统计」）走**现成**的 `core.envelope.paginated(summary=…)` 槽（先例 `business/api/performance_views.py`），且打在**同一份筛选后的 QuerySet 全集**上（⛔ 不是当前那 20 行）——分页发生在 `paginate_queryset`（它只返回切片，不改这份 queryset），两者天然同源。⭐ 每行带 `actor`（登录账号）与 `actor_display`（**账号名称**）。 - `actors`（「操作人员」下拉的候选，**登录账号**）与 `actions`（「操作动作」下拉的候选，即
    `ORDER_ACTION_LABELS` 的**对外投影**）随响应一起给，省一次请求（spec D3/D9）。 ⭐ **订单日志人名线（spec §3-C1）**：`actors` 是**兼容保留**的旧形状；带显示名的新形状是 `actor_options: [{"actor", "display"}]`（同一处 `_actors()` 派生，顺序/集合一致）。⛔ 筛选值仍是登录账号（`filter(actor=…)` 没换键）。 - ⚠️ `actors` / `actor_options` / `actions` 的**口径都不含任何筛选**（D9）：否则选完一个下拉就互相收窄。 - ⚠️ `actions` 存在的**理由**（spec D3）：短名的**唯一出处**留在后端，⛔ 前端不镜像那份常量——镜像 = 同一个事实两处各写 27 条中文，改一处忘另一处，下拉与表格的短名就会不一致。
    """

    permission_classes = [has_node("order.platform_action_logs")]
    pagination_class = EnvelopePagination
    serializer_class = OrderActionLogRowSerializer

    def _search_params(self):
        """查询参数**唯一**的校验口（E-0010）。返回 `validated_data`。

        空串按「没传」处理：前端清空筛选栏时会送 `?stime=` / `?key1=`，那不是非法值
        而是「没选」（与同文件 `OperationLogListView._search_params` 同一口径）。
        """
        query = self.request.query_params
        raw = {name: value for name, value in query.items() if value != ""}
        params = OrderActionLogSearchSerializer(data=raw)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`（E-0010）。
        params.is_valid(raise_exception=True)
        return params.validated_data

    def get_queryset(self):
        data = self._search_params()
        # 见模块 docstring：租户由连接（认证态）决定，查询里没有 tenant/schema 入口。
        # 行集合的唯一出处是 `order_action_rows()`（含 `ORDER_ACTION_EXCLUDED` 的排除）。
        qs = order_action_rows()

        stime = data.get("stime")
        if stime is not None:
            qs = qs.filter(created_at__gte=stime)
        etime = data.get("etime")
        if etime is not None:
            qs = qs.filter(created_at__lte=etime)

        action = data.get("action")
        if action is not None:
            qs = qs.filter(action=action)

        actor = data.get("actor")
        if actor is not None:
            qs = qs.filter(actor=actor)

        # 「系统单号 / 交易单号」关键词：两列**或**关系（spec §4.2）。值已过序列化器（≤255）。
        key1 = data.get("key1")
        if key1 is not None:
            qs = qs.filter(
                Q(order_no__icontains=key1) | Q(source_order_no__icontains=key1)
            )

        # 排序：`ordering` 已在序列化器里过白名单，这里再经**共用的** `LOG_ORDERING_FIELDS`
        # 映射成我们自己的字段元组（不是把请求值直接交给 `order_by`，E-0010）。
        ordering = data.get("ordering")
        if ordering is not None:
            qs = qs.order_by(*LOG_ORDERING_FIELDS[ordering])
        # 没传 `ordering` ⇒ 保持 `Meta.ordering`（最新在前）。
        return qs

    @staticmethod
    def _summary(queryset):
        """右侧「操作统计」：`[{action, action_label, actor, actor_display, count}]`（spec §4.4）。按 `(-count, action, actor)` 排序；分组建在**传进来的那份筛选后的 QuerySet** 上。 ⚠️ `order_by()`（**空参**）先清掉 `Meta.ordering`：不清的话那两个默认排序列会被 Django 一并塞进 `GROUP BY` ⇒ 同一 (action, actor) 被拆成多行、`count` 恒为 1。 ⭐ **订单日志人名线（spec §3-C1）**：每行追加 `actor_display`（账号名称）。 - 分组维度**仍是 `actor`（登录账号）**，⛔ 不换成显示名：两个人可能同名，按显示名分组会把
        两个不同的人合成一行（spec §6-2 如实保留身份维度）。 - 映射对本页 summary 里出现的 `actor` **去重后一次查询** （`apps/accounts/display.py::display_names_for_usernames`）；查不到 ⇒ 回落 `actor`。 - `actor` 键**保留**（加法；显示侧按 `actor_display ?? actor` 回落）。
        """
        # ⚠️ 先物化：`rows` 是一条聚合查询，下面要按它的 actor 再查一次，迭代两遍会打两次库。
        rows = list(
            queryset.order_by()
            .values("action", "actor")
            .annotate(count=Count("id"))
            .order_by("-count", "action", "actor")
        )
        # ⚠️ 一次查询：本页 summary 里出现的 actor 去重后批量解析显示名（⛔ 不逐行查）。
        actor_names = display_names_for_usernames(row["actor"] for row in rows)
        return [
            {
                "action": row["action"],
                "action_label": action_label(row["action"]),
                "actor": row["actor"],
                # 查不到（账号已硬删 / 改名）⇒ 回落登录账号，⛔ 不回空串。
                "actor_display": actor_names.get(row["actor"]) or row["actor"] or "系统",
                "count": row["count"],
            }
            for row in rows
        ]

    @staticmethod
    def _actors():
        """该租户 `order.*` 日志里出现过的 `actor` 去重列表（字典序，spec §4.5）。 ⚠️ **底集不叠任何筛选**（D9）：日期 / 动作 / 人员都不进这里，否则下拉会互相收窄。 ⚠️ **排掉空 `actor`**（spec §10.4）：`Order.updated_by` 是 `blank=True`，而
        `order.refund` 那条写点的 `actor` 来自 `order.updated_by or ""` ⇒ 空串**结构上可达**；不排的话「操作人员」下拉里多一个**空白项**（它经参数组装层会被当「没传」⇒ 选了等于没筛）。 ⚠️ `order_by("actor")` 显式取代 `Meta.ordering`：`DISTINCT` 查询下若沿用默认排序，
        排序键会被带进 SELECT ⇒ 去重失效。
        """
        return list(
            order_action_rows()
            .exclude(actor="")
            .order_by("actor")
            .values_list("actor", flat=True)
            .distinct()
        )

    @staticmethod
    def _actions():
        """「操作动作」下拉的候选 = `ORDER_ACTION_LABELS` 的**对外投影**（spec §4.5 / D3）。 **27 条** `{"code", "label"}`，按 `code` 字典序，**不带任何筛选**（每次请求全量给）。 ⚠️ **唯一出处是后端那张表**（`apps/audit/actions.py::ORDER_ACTION_LABELS`）：
        本方法只是排序 + 换形状，⛔ 不新增第二份常量；前端直接吃这一份、⛔ 不镜像。 ⚠️ 键序固定 `{"code", "label"}`（照 spec §4.5 的写法），便于前端与测试逐字比对。
        """
        return [
            {"code": code, "label": ORDER_ACTION_LABELS[code]}
            for code in sorted(ORDER_ACTION_LABELS)
        ]

    def get_serializer_context(self):
        """把当前页的昵称映射与订单回填映射塞进 context（spec §4.3 / D7）。 ⚠️ 时点：`list()` 先 `paginate_queryset`（把页面挂在 `self.paginator.page` 上）
        再 `get_serializer` ⇒ 这里拿到的是**当前页**的行，两个映射各只查一次（一页一次查询，⛔ 不是每行一次）。
        """
        context = super().get_serializer_context()
        page = getattr(self.paginator, "page", None) or []
        context["actor_display_names"] = actor_display_names(page)
        context["order_backfill"] = _order_backfill(page)
        return context

    def get_paginated_response(self, data, *, summary=None):
        """把 `summary` 透给分页器、再把 `actors` / `actor_options` / `actions` 补进 `data`。 ⚠️ **信封形状仍然只在 `core.envelope` 定义**：`summary` 走 `paginated(summary=…)` 那个现成槽；`actors` / `actor_options` / `actions` 是**同一个 `data` 字典**上新加的键（先例：`business/api/base.py::OrderListPagination` 往 payload 上补 `summary`）， ⛔ 不在这里拼 `{items,total,…}` 整只信封。 ⭐ **订单日志人名线（spec §3-C1）**：
        - `actors: string[]` **兼容保留**（部署窗口里旧前端仍能工作）：它是**登录账号**，也是「操作人员」筛选真正提交的值（后端 `actor__exact` 一个字都没改）。 - `actor_options: [{"actor": 登录账号, "display": 账号名称}]` 是**带显示名的新形状**：前端拿 `display` 当文本、`actor` 当值。查不到显示名 ⇒ `display` 回落 `actor`。 - ⚠️ 两者必须由**同一处** `_actors()` 派生（spec §6-7：同一份数据的两种形状）——顺序、集合都逐项一致，⛔ 不许各写一份去重。多出来的成本是**一次**批量解析查询。
        """
        actors = self._actors()
        # ⚠️ 一次查询：下拉候选的显示名（⛔ 不逐项查；`_actors()` 已经是全租户去重列表）。
        actor_names = display_names_for_usernames(actors)
        response = self.paginator.get_paginated_response(data, summary=summary)
        response.data["data"]["actors"] = actors
        response.data["data"]["actor_options"] = [
            {"actor": actor, "display": actor_names.get(actor, actor)}
            for actor in actors
        ]
        response.data["data"]["actions"] = self._actions()
        return response

    def list(self, request, *args, **kwargs):
        """分页行 + `summary`（打在**筛选全集**上，spec D3）+ `actors` / `actions`（spec D9）。"""
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(
            serializer.data, summary=self._summary(queryset)
        )


class OperationLogActorListView(generics.GenericAPIView):
    """「相关人员」多选下拉的数据源（spec §6.3，评审 I5）。 **为什么不能复用 `/api/accounts/`**：那个端点的准入是 `IsAccountAdmin` （`apps/accounts/views.py:58`），持有 `system.logs` 但不是账号管理员的人拉到的名单 **恒空**。这里准入与日志端点**同一条节点**，只回 `{id, username, display_name}` 三个字段（`display_name` = 昵称，空则账号名；白名单仍逐条枚举，见 `OperationLogActorRowSerializer`，spec §11.1）。
    ⚠️ **不用 `ListAPIView` + 分页器**：spec 要的是**纯列表**（下拉源，不是分页表格），所以 `get()` 显式经 `api_response()` 封口——`GenericAPIView` 不像 `EnvelopeModelViewSet` 那样自动包信封，忘了这一步会吐一个裸数组。 ⚠️ **上限落在查询上**：`[:ACTOR_LIMIT]` 编译成 SQL `LIMIT 50`，不是把全表读进内存再切。
    """

    permission_classes = [has_node("system.logs")]
    serializer_class = OperationLogActorRowSerializer

    def _search_params(self):
        """查询参数**唯一**的校验口（E-0010）。返回 `validated_data`。 ⚠️ 先转成**普通 dict** 再过序列化器（`apps/accounts/views.py:100-107` 记录的坑）： `request.query_params` 是 `QueryDict`，DRF 见到它会走 `html.parse_html_dict`
        把请求当 **HTML 表单**解析。空串按「没传」处理：前端清空搜索框时会送 `?key=`，那不是非法值而是「没选」。
        """
        raw = {
            name: value
            for name, value in self.request.query_params.items()
            if value != ""
        }
        params = OperationLogActorSearchSerializer(data=raw)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`（E-0010）。
        params.is_valid(raise_exception=True)
        return params.validated_data

    def get_queryset(self):
        data = self._search_params()
        # 见模块 docstring：租户由连接（认证态）决定，查询里没有 tenant/schema 入口。
        qs = get_user_model().objects.all()
        key = data.get("key")
        if key is not None:
            # 下拉显示昵称 ⇒ 用户会按**看到的昵称**搜；只按 username 搜会「搜不到自己看见的人」。
            # 两个条件是**或**关系（账号名或昵称命中都算），排序与上限不动（spec §11.1）。
            qs = qs.filter(
                Q(username__icontains=key) | Q(first_name__icontains=key)
            )
        # 显式排序：下拉的 50 条上限必须**确定**，否则同一份数据每次回不同的 50 条。
        return qs.order_by("username", "id")[:ACTOR_LIMIT]

    def get(self, request, *args, **kwargs):
        actors = self.get_queryset()
        return api_response(data=self.get_serializer(actors, many=True).data)


class OperationLogEventView(generics.GenericAPIView):
    """留痕使用已认证账号，兼容编辑入口的现有上报格式。"""

    permission_classes = [IsAuthenticated]
    serializer_class = OperationLogEventSerializer

    def post(self, request, *args, **kwargs):
        payload = self.get_serializer(data=request.data)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`（E-0010）。
        payload.is_valid(raise_exception=True)

        record_order_read(
            action=payload.validated_data["action"],
            order_no=payload.validated_data["order_no"],
            actor=request.user.get_username(),
            actor_id=request.user.pk,
        )
        return api_response(data=None)
