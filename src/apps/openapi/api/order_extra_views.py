"""对外 v2 的**只读扩展端点**（spec `docs/specs/2026-10-08-open-platform-v2-parity-design.md` §6.1/§6.4/§13.10）：status / images / ranks / bills / games / projects 六条路由。
共同装配（照 `order_query_views.ApiOrderView` 形状、认证换 Bearer）：`ApiAppBearerAuthentication` + ⛔ 显式空 `permission_classes`（§13.10 一个应用=本租户全部业务数据，节点门/游戏范围属 v1 面）+ 兜底闸（类属性 ⇒ 401 先于 429、被挡请求不进桶）。
⛔ 本模块只读不写业务表（唯一写库在认证层 `ApiApp.last_used_at`；审计反向钉子在 `test_operation_log_coverage`）；租户上下文只来自连接、⛔ 不读请求参数；查询走默认 manager（已软删取不到）。`bills/` 复用 `settlement_rows` 但**不套**内部按登录人收窄的卡点（Bearer 下是匿名用户、套了列表恒空；卡点留在内部视图原处）。"""
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.business.api.serializers import SettlementListSerializer
from apps.business.api.settlement_views import SettlementViewSet, settlement_rows
from apps.business.models import EmpType, Game, Order, OrderScreenshot, Project
from apps.business.performance_amount import AmountError
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.order_image_views import OrderImageClaimMixin
from apps.openapi.api.order_write_views import OrderStatusChangeMixin
from apps.openapi.authentication import ApiAppBearerAuthentication
from apps.openapi.serializers import _iso, _live_performances, image_row, money
from apps.openapi.throttling import ApiAppFallbackThrottle

#: 「接单排行榜」不实现（§13.10-1 裁定）⇒ 路径保留、恒回这句 + `OPENAPI_RANKS_NOT_AVAILABLE`。
#: 这句是对外契约的一部分（接入文档逐字列出）。不是 404：保留路径=让第三方看得出「有这个接口、只是没开」
#: （AGENTS §5-D：没有后端的部分不置灰、点了要说破）。
RANKS_NOT_AVAILABLE_MSG = "接单排行榜尚未开放"

#: 分页契约（§6.4）：`page` 默认 1；`page_size` 默认 20、**上限 100**。
#: ⚠️ 这两个数字是**对外契约的一部分**（接入文档逐字列出），改它们要同一次改文档。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PageQuerySerializer(serializers.Serializer):
    """三个列表端点共用的分页入参（唯一校验口）。请求来源的值一律过序列化器（AGENTS §5-E / E-0010）：
    `?page=abc` 直接进切片会 500，本仓对参数不合法的既定答复是 HTTP 200 + `code=1000`。
    ⚠️ `page_size` **不给 `max_value`**：超上限是**夹到上限**（§6.4）⛔ 不是报错；钳制只有一处实现（`page_window`）。"""

    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(required=False, min_value=1, default=DEFAULT_PAGE_SIZE)


def page_window(request) -> tuple[int, int]:
    """分页入参 ⇒ `(page, page_size)`；`page_size` 超上限夹到上限。钳制写在这里而非序列化器 `max_value`（理由见 `PageQuerySerializer`）；⛔ 别在端点里再写一遍 `min(...)`（「上限是多少」出现两处出处）。
    ⚠️ 必须保持公开名、且 `order_query_views` 的 `updated_since` 分支**必须调本函数**（⛔ 不许在那边自己拼第二份窗口计算）：
    两边是同一套分页口径（默认 20 / 上限 100 / 畸形 `1000`），判据是「这类请求还会不会 500」而非「这个字段」（AGENTS §5-E）。"""
    params = PageQuerySerializer(data=request.query_params)
    params.is_valid(raise_exception=True)
    return params.validated_data["page"], min(
        params.validated_data["page_size"], MAX_PAGE_SIZE
    )


def _paginated(queryset, request, row) -> dict:
    """统一分页载荷：`data = {"count", "results", "page", "page_size"}`（§6.4 契约，本函数是唯一装配出口 ⛔ 端点别自己拼）。`count`=命中总数（退页不变）；`page`/`page_size` 回**实际生效**值（第三方不必猜夹到多少）；页号超范围 ⇒ 空 `results`+真实 `count`（⛔ 不报错：NotFound 口径留给「业务对象不存在」）；`row` 是函数——载荷装配只有端点自己知道。
    ⛔ **不复用** `EnvelopePagination`：那是页面端形状（`items`/`total`/上限 200），§6.4 逐字是另一套（`count`/`results`/上限 100）——复用会三处偏离契约，对外契约发布后不能悄悄改。
    ⚠️ 因此本函数自己做切片，但入参解析仍走序列化器 ⇒ 畸形入参回 `1000`，与页面端分页器抛 `NotFound` 的 `1002` 刻意不同（两条路的入参校验器不同）。"""
    page, page_size = page_window(request)
    offset = (page - 1) * page_size
    rows = list(queryset[offset : offset + page_size])
    return {
        "count": queryset.count(),
        "results": [row(item) for item in rows],
        "page": page,
        "page_size": page_size,
    }


class ProjectQuerySerializer(serializers.Serializer):
    """`GET /api/open/v1/projects/` 的查询参数（`game_id` 必填）。**必填是契约**（§6.1）：不给回 `1000`，
    ⛔ 不回全部项目——「漏传参数」被当「不过滤」是一次静默越权面（同族先例：`OrderQuerySerializer` 对 `source_order_no` 的口径）。
    非整数/非正数同样 `1000`（进不了 `filter()`）。"""

    game_id = serializers.IntegerField(
        required=True,
        min_value=1,
        error_messages={"required": "缺少参数：game_id"},
    )


def _claimed_at(order) -> str | None:
    """这张单的接单时刻（`Performance.claimed_at`），读不到 ⇒ `None`。`Order` 上没有接单人/接单时间列——接单时刻只存在于绩效行（抢单与指派两种来源都写它）。
    取**打单工资行**（`EmpType.MAIN`）：客服行（`SALES`）的 `user` 语义是"销售归属人"、建行不写 `claimed_at`，拿它当接单时刻=恒空假值；`uniq_order_main_taker` 保证 MAIN 行最多一条（无取哪条的分叉）。
    软删排除复用 `_live_performances`（与 `order_payload.costs` 同一判据，事实只有一处实现）。"""
    for row in _live_performances(order):
        if row.emp_type == EmpType.MAIN:
            return _iso(row.claimed_at)
    return None


class ApiOrderExtraView(APIView):
    """本模块五条端点的共同装配（认证/无权限类/兜底闸；理由见模块 docstring）。
    用普通 `APIView`（⛔ 不是 ViewSet）：路由是显式 `path()`，对外接口没有集合语义（D5：没有创建/修改/删除）。"""

    authentication_classes = [ApiAppBearerAuthentication]
    #: ⛔ 空列表是**有意**的（v2 §13.10：一个应用凭据 = 本租户全部业务数据；
    #: 权限节点与游戏范围过滤属 v1 面、待收口）。⚠️ 空列表在 DRF 里就是"不做权限检查"，
    #: 与"没写这个属性"（会吃 `DEFAULT_PERMISSION_CLASSES` 的 `IsAuthenticated`）**不是**一回事。
    permission_classes = []
    throttle_classes = [ApiAppFallbackThrottle]

    @staticmethod
    def _live_order(order_no):
        """按我方订单号取在用的那张单（取不到 ⇒ `None`）。
        本模块唯一的 `Order.objects` 读点（三个带 `<order_no>` 的端点共用；⛔ 各写一份=第二个判据，§5-E）；
        默认 manager ⇒ 已软删取不到（D-0144，与既有查单端点同口径），且隐藏单 exclude 掉。"""
        return (
            Order.objects.filter(order_no=order_no)
            .exclude(hall_card__isnull=False)
            .exclude(upstream_hall_card__isnull=False)
            .prefetch_related("performances")
            .first()
        )

    @classmethod
    def _require_order(cls, order_no):
        """取那张在用的单；取不到 ⇒ `NotFound`（经本仓异常处理器变成 `code=1002` + HTTP 200）。"""
        order = cls._live_order(order_no)
        if order is None:
            # 与既有查单端点**同一个出口**：`NotFound`（⛔ 不是 404、也不是自造文案）。
            raise NotFound()
        return order


class OrderStatusView(OrderStatusChangeMixin, ApiOrderExtraView):
    """`GET /api/open/v1/orders/<order_no>/status/` —— 状态+四个时间点。`POST` 与 `GET` 同路径（§6.1 两条契约共用一条路由）⇒ 写的一半来自 `OrderStatusChangeMixin`（必须 mixin 的框架事实见其 docstring）；装配仍是本类三条，mixin 自带同一套值（无漂移）。
    `data` 恰好六键：`status`/`status_text`/`created_at`（录单）/`claimed_at`（接单，见 `_claimed_at`）/`submitted_at`（**恒 `null`**）/`completed_at`（验收）。三个非空时刻 ISO 8601 带时区；可空的两个没值 ⇒ `None` ⛔ 不是空串。
    ⭐ `submitted_at` 恒 `null` 是**契约值**（§13.8-3/§13.10-2：本系统没有"交单时间"列；⛔ 不从 `OperationLog` 回查——那会把审计表变成对外契约承重件；要真值=动状态机加列，须业务方拍板另立线）；用例由「同一次响应里其余三个时刻有值」当对照钉住。"""

    def get(self, request, order_no):
        order = self._require_order(order_no)
        return api_response(
            {
                "status": order.status,
                "status_text": order.get_status_display(),
                "created_at": order.created_at.isoformat(),
                "claimed_at": _claimed_at(order),
                # ⛔ 恒 null（契约值，理由见类 docstring）：本系统没有"交单时间"这一列。
                "submitted_at": None,
                "completed_at": _iso(order.completed_at),
            }
        )


class OrderImagesView(OrderImageClaimMixin, ApiOrderExtraView):
    """`GET /api/open/v1/orders/<order_no>/images/` —— 进度图列表（分页）。`POST .../images/` 认领与 GET 同住一个类（同路径第二条路由走不到）；写的一半在 `OrderImageClaimMixin`，真正落库在 `claim_screenshot`；本模块仍不写业务表。
    取行口径与内部 `screenshot_views` 同一：`status=ready` + 软删单排除（D-0144）+ `order_by("pk")`（最旧在前；`OrderScreenshot` 刻意无 `Meta.ordering`，顺序由读路径自己声明）。每项恰好七键；⛔ 不给内部主键 `id`；`url`=预签名直链（生产 COS 整点对齐 1–2 小时 / 本机 300 秒；第三方拿到即用、⛔ 长期缓存，过期 403）。
    ⭐ 上传人给**显示名**（`user_display_name` 唯一出处，⛔ 不自己拼第二份规则）而⛔ 不是账号主键——内部主键是不必要暴露面、且与 `order_payload` C 组「员工身份不给」冲突；配套 `select_related("uploaded_by")`（非空外键不预取=每行一条查询）；判据=`tests/test_openapi_order_extra.py`。"""

    def get(self, request, order_no):
        # ⚠️ 先判"这张单在不在"：不存在 ⇒ `1002`（与既有查单端点同形），⛔ **不是**"空列表"——
        # 后者会让第三方分不清"这单没有图"与"这单根本不在你这里"（E-0004 同族的静默形态）。
        self._require_order(order_no)
        rows = (
            OrderScreenshot.objects.filter(
                order__order_no=order_no,
                order__deleted_at__isnull=True,
                status=OrderScreenshot.STATUS_READY,
            )
            # ⚠️ 预取上传人：显示名要读 `uploaded_by` 那一行（不预取 = 每行一条查询）。
            .select_related("uploaded_by")
            .order_by("pk")
        )
        return api_response(
            _paginated(
                rows,
                request,
                # ⚠️ 载荷装配=`apps.openapi.serializers.image_row`（同一段代码；认领端点调的就是它）
                # ⇒「认领回来的那一项」与「列表里那一项」逐键同形（⛔ 不拼第二份）；
                # 搬迁理由写在那里的 docstring。
                image_row,
            )
        )


class RankListView(ApiOrderExtraView):
    """`GET /api/open/v1/ranks/` —— 接单排行榜：⛔ 不实现（§13.10-1）。路径保留、恒回 HTTP 200+`code=1024`+`RANKS_NOT_AVAILABLE_MSG`、`data=None`。
    这是「尚未开放」档，与「查不到」(`1002`)、「参数错」(`1000`) 都不是同一个意思 ⇒ 新开了一个码（⛔ 拿既有码顶替会让第三方读成别的意思）。
    对标 `GetRecOrderRankList` 的对应物不存在（`home.order_ranking` 在 `apps/rbac/wiring.py` 登记为「无对应端点/页面」）；本方法一个查询都不发（数据面无副作用）。"""

    def get(self, request):
        return api_response(
            data=None,
            msg=RANKS_NOT_AVAILABLE_MSG,
            code=ErrorCode.OPENAPI_RANKS_NOT_AVAILABLE,
        )


#: 对外**账单行**的键白名单（恰好这九个）：`order_no`/`emp_type`/三金额（`temp_amount`/`bonus_amount`/`taker_price`）/`payable_net`/结账三键（`settle_status`/`settled_amount`/`settled_at`）。
#: ⛔ **按白名单挑、不按黑名单滤**：黑名单会让内部序列化器将来新增的列**静默**出现在对外契约上（"外部多一个键"=契约变更）。
#: ⛔ 不给员工身份（`user`/`username`/`display_name`/`settled_by`，`order_payload` C 组裁定）、⛔ 不给内部主键；字段集是契约，改它要同一次改文档+用例字面量集合。
BILL_ROW_KEYS = (
    "order_no",
    "emp_type",
    "temp_amount",
    "bonus_amount",
    "taker_price",
    "payable_net",
    "settle_status",
    "settled_amount",
    "settled_at",
)


def _bill_row(performance) -> dict:
    """账单一行的对外载荷：从**内部结算列表的载荷**里挑白名单键。复用 `SettlementListSerializer` 而非自己拼
    （金额字符串形状/`payable_net` 算式/`settled_amount` 快照语义仓里各只有一处实现；⛔ 本模块不重算=第二份判据）。
    ⚠️ 代价如实：内部新增列时本函数不自动跟上（白名单挡住=要的形状），`tests/test_openapi_order_extra.py` 键集断言会在那一刻红。"""
    rendered = SettlementListSerializer(performance).data
    return {key: rendered[key] for key in BILL_ROW_KEYS}


class BillListView(ApiOrderExtraView):
    """`GET /api/open/v1/bills/` —— 账单列表（内部叫"结算记录"；分页+日期筛选）。取行=**复用内部那个取行口** `settlement_rows(data)`（⛔ 本模块不写第二份查询；基础集+9 个筛选+两条软删排除；入参校验也复用 `SettlementViewSet._params`：空串按"没选"剔除后再过序列化器）。
    ⭐ 数据范围=**本租户全部**（§13.10）——与内部端点唯一差别是**不套** `scope_queryset` 游戏收窄：Bearer 下 `request.user` 是匿名、套上去 `games_of` 空集 ⇒ 列表恒空；卡点本身没搬家没去掉（留在内部视图，那是安全边界）。
    日期筛选 `date_from`/`date_to`=`YYYY-MM-DD` 本地时区切天、两端都含、筛 `Performance.settled_at`（与内部同口径）；每行恰好 `BILL_ROW_KEYS` 九键；`AmountError`（扣点越界等）整表拒绝回业务码 ⛔ 不降级成"这一行算 0"（静默错账，E-0010 同族）。"""

    def get(self, request):
        # ⚠️ 入参校验复用内部结算列表同一个口（`SettlementViewSet._params`：空串剔掉再过序列化器），
        # ⛔ 不在本模块重写（"空串算不算没传"第二处判据）；传 `self` 是因为 `_params` 只读 `self.request.query_params`。
        data = SettlementViewSet._params(self)
        rows = settlement_rows(data)
        try:
            payload = _paginated(rows, request, _bill_row)
        except AmountError as exc:
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(payload)


class GameListView(ApiOrderExtraView):
    """`GET /api/open/v1/games/` —— 游戏字典（分页）。取行与内部 `GameViewSet` 列表同一口径：软删排除（D-0085）、顺序取模型自己的 `Meta.ordering`（⛔ 本模块不另写排序，"字典按什么排"两处出处会漂）。
    每项恰好四键 `id`/`name`/`is_active`/`sort_order`；⛔ 不给 `order_count`（内部序列化器有它：按 `Order.all_objects` 计数、含已软删——对第三方是"你这里有多少单"的间接泄漏面）。
    ⛔ 不按 `is_active` 过滤：启停是租户自己的开关，第三方拿 `is_active` 自己决定要不要展示（替它过滤=让它以为"这款游戏不存在"）。"""

    def get(self, request):
        rows = Game.objects.filter(deleted_at__isnull=True)
        return api_response(
            _paginated(
                rows,
                request,
                lambda game: {
                    "id": game.pk,
                    "name": game.name,
                    "is_active": game.is_active,
                    "sort_order": game.sort_order,
                },
            )
        )


class ProjectListView(ApiOrderExtraView):
    """`GET /api/open/v1/projects/?game_id=<id>` —— 项目字典（分页；`game_id` 必填）。取行与内部 `?game=` 同口径：`Project` 没有 game 列 ⇒ 过滤走 `category__game_id`；软删排除 `deleted_at is null`（D-0085，分类/游戏软删会级联给下级打时间戳，不必再各判一次）。
    `game_id` 三个失败档都回 `1000`：没传（⛔ 不回全部项目=静默越权面）/ 非整数非正数 / 本租户里不存在（判据=当前连接所属 schema 的游戏表，别的租户的 id 在本租户通常就是"不存在"，AGENTS §4；措辞与 `validate_games` 同一形状 ⛔ 不另造）。
    每项恰好五键 `id`/`name`/`base_price`/`category`/`sort_order`；`base_price` 走 `money()`（没填 ⇒ `null`）；`category` 给分类名（与 `order_payload` 同口径）；⛔ 不给 `order_count`（理由同 `GameListView`）。"""

    def get(self, request):
        params = ProjectQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        game_id = params.validated_data["game_id"]
        if not Game.objects.filter(pk=game_id).exists():
            # ⚠️ 判据是"存在性"（⛔ 不是"这款游戏还活着"）：软删游戏的下级会被级联打时间戳
            # ⇒ 那条路返回的是**空列表**（"这款游戏没有可用项目"），而不是把 id 判成非法。
            return api_response(
                data=None,
                msg=f"游戏不存在：{[game_id]}",
                code=ErrorCode.VALIDATION,
            )
        rows = Project.objects.filter(
            deleted_at__isnull=True, category__game_id=game_id
        ).select_related("category")
        return api_response(
            _paginated(
                rows,
                request,
                lambda project: {
                    "id": project.pk,
                    "name": project.name,
                    "base_price": money(project.base_price),
                    "category": project.category.name,
                    "sort_order": project.sort_order,
                },
            )
        )
