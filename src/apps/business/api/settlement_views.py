"""结算域的四个只读端点 + 结账端点（spec §4.3 / §7；P4-7 的结账、P4-8 的三个读接口、 D-0089 的「结账对象人员」候选）。 | 路径 | 方法 | 谁 | 落点 | |---|---|---|---| | `/api/business/settlements/settle` | POST | 财务 | `SettleView`（P4-7） | | `/api/business/settlements/preview` | GET | 财务 / 客服 | `SettlementPreviewView`（本线，T2.1） | | `/api/business/settlements` | GET | 财务 / 客服 | `SettlementViewSet`（P4-8） | | `/api/business/settlements/unattributed` | GET | 财务 / 客服 | `UnattributedView`（P4-8） | | `/api/business/settlements/people` | GET | 财务 / 客服 | `SettlementPeopleView`（D-0089） | | `/api/business/settlements/daily-summary` | GET | 财务 / 客服 | `SettlementDailySummaryView`（D-0319） | | `/api/business/settlements/daily-summary/sources` | GET | 财务 / 客服 | `SettlementDailySourcesView`（D-0323） | `POST /api/business/settlements/settle`——**财务手动触发、按 `performance_ids` 批量、不打款**
（业务方裁定：打款走线下）。**单条结账 = 长度为 1 的列表**，不是另一个端点。 ⚠️ **闸门用 RBAC 节点 code**（Ruling T / 计划 Global Constraints #7）： `has_node("finance.change_settlement_status")`——对标目录里的 **`84 更改结算状态`** （`apps/rbac/catalog.py` 里 `benchmark_id = 84` / `code = "finance.change_settlement_status"` 那一行； ⚠️ **不写行号**——此前裁剪后它会漂，按 id/code 指认）。它是**全仓 `has_node` 的第一个业务消费方**，顺带把「工厂返回真类、可被 DRF 无参实例化」这条契约接到真实调用点上（`tests/test_settle_api.py::SettlePermissionTests` 三个方向钉住它真的生效）。 ⛔ **不写 `has_role(FINANCE)`**：`财务` Group 已被 D-0030 删除，能力改由权限点给。 ⚠️ **闸门天然排在「解析参数」之前**：DRF 的 `initial()`（认证 → 权限）先于 `post()`/`get()` 执行，所以未授权者不会因为「参数畸形」而拿到 `1000`，也无法从错误差异里探测端点行为。 ⚠️ 业务失败一律 **200 + 业务 `code`**（`1006` / `1011` / `1000`），未认证 **401**，无权限 **403**。 ## P4-8 的三个读接口
`GET /api/business/settlements`（筛：人 / 时间窗 / `review_status` / `settle_status` / `emp_type` / 订单号，每行带 `payable_net`）、`GET /api/business/settlements/unattributed` （已 `COMPLETED` 但没有销售提成行的订单，spec §3.8）、以及住在 `adjustment_views.py` 的调整记录链。三者**都过 `scope_queryset`**（spec §6.1 逐条列明），且都是**只读**（不写库）。 ⭐ D-0089 又加了**第四个**读路径 `GET /api/business/settlements/people`（「结账对象人员」候选）：它同样过一次 `scope_queryset`（spec §6.1 的口径是「结算域的读路径逐条都过卡点」，不是「只有 P4-8 那三条过」）——否则将来一注册 `scope_filter`，它就会成为**唯一不收窄的结算读路径**（接线判别力用例见 `tests/test_settlement_api.py::SettlementPeopleTests`）。 ⭐ **`FinanceReadPermissionMixin` 为什么在这里、而不是 `base.py`**：它是**本任务三个读端点** 共用的闸门，而 `adjustment_views.py` 也是本任务的产物；`base.py` 是与具体任务无关的基础设施层（信封 / 分页基类），把一个业务闸门放进去会让它变成「谁都能顺手改」的公共地。跨模块 import 只有一处（`adjustment_views` ← 本模块），没有环。
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.business.api.serializers import (
    OrderListSerializer,
    SettlementDailySourcesSerializer,
    SettlementDailySummarySerializer,
    SettlementListSerializer,
    SettlementPeopleSearchSerializer,
    SettlementPreviewQuerySerializer,
    SettlementSearchSerializer,
    SettleRequestSerializer,
)
from apps.business.finance_read_cache import historical_settlement
from apps.business.game_scope import filter_by_game_ids, game_scope
from apps.business.models import (
    EmpType,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.business.performance_amount import AmountError
from apps.business.performance_export import build_settlement_workbook
from apps.business.selectors import display_name_expression
from apps.business.settlement import (
    SettleError,
    settle_performances,
    unsettled_adjustments_prefetch,
)
from apps.business.settlement_preview import build_settlement_preview
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node

#: 三个读接口共用的节点 code：`apps/rbac/catalog.py` 里 `benchmark_id = 80` / `code = "finance.payroll"` 那一行（⚠️ **不写行号**：裁剪会让它漂，按 id/code 指认），对标 **80「打手与客服工资」** （note 原文「允许进入打手与客服工资管理列表页面，可以浏览所有人的提成明细」）。
#: ⚠️ `has_node` **不看 `available`**（`apps/rbac/permissions.py` 的 docstring 明写），所以该点仍是 `available=False` 不影响闸门生效（登记口径见报告「顾虑」）。
FINANCE_READ_NODE = "finance.payroll"

#: 结账专用节点 code（与 `finance.payroll` 是**两个**点：那个是「看」，这个是「改结算状态」）。
#: ⚠️ 人工修正端点（`adjustment_views.MANUAL_ADJUSTMENT_NODE`）与它**同一个 code**（spec §6）。
SETTLE_NODE = "finance.change_settlement_status"


def _or_zero(value):
    """`Sum("settled_amount")` 在「该组全是 NULL 快照」时出 `None` ⇒ 回落 `Decimal("0")`。判据一律 `is not None`（E-0021）：`Decimal("0.00")` 是**假值**，用 `or` / 真值判断会把合法的 0.00 也换掉（数值碰巧相同，但同一事实两处写法会各自漂移）。
    D-0319 起先用在 `SettlementDailySummaryView.list` 的串成型；D-0323 的来源分解端点（排序键 + 串成型）复用**同一个**助手——「NULL 快照合计按 0」只有这一处定义。
    """
    return value if value is not None else Decimal("0")


def _local_day_window(day: date) -> tuple[datetime, datetime]:
    """业务时区下的「本地日」→ **aware** 的 `[当日 00:00, 次日 00:00)` 半开区间。两个结算聚合端点（`SettlementDailySummaryView` 的 `date_from`/`date_to`、 `SettlementDailySourcesView` 的 `date`）共用的**切天窗口**——结算域内只有这一处定义。 ## 为什么不用 `settled_at__date`（W-01，第五轮审计 N3 的修法） `settled_at__date…` 族在 SQL 层是 `(settled_at AT TIME ZONE 'Asia/Shanghai')::date` ——套了函数的列进不了 b-tree，`settled_at` 上就算有索引也吃不到（EXPLAIN 实测：10 万行 Seq Scan、98,560 行被滤掉，见审计报告 framework.md W-01）。半开区间把同一句话写成**裸列比较**（sargable），「查哪个本地日」的语义不变。 ## 等价性（为什么可以换）
    旧形态按**当前时区**切天、新形态的边界取**同一时区**的当日/次日 00:00 即时刻 ⇒ 本仓 `TIME_ZONE = "Asia/Shanghai"` 无夏令时（1991 年起恒为 UTC+8）⇒ 「`(settled_at AT TIME ZONE 'Asia/Shanghai')::date = day`」 ⇔「`day 00:00 <= settled_at < 次日 00:00`」逐行等价（对拍用例钉在两个测试文件里）； `TruncDate("settled_at")` 归天分组一字未动，筛与分组仍是同一套切天口径。同族先例：`xgj_receipt_stats.window_bounds`（同一条「本地日 → aware 半开区间、 ⛔ 不用 `__date` 后缀」的登记先例）——机制同源，但那是收款域 from~to 成对的窗口；本域 `date_from`/`date_to` 是**两个独立筛选**、sources 又要单日的两端，故落一个本地助手而不是跨域借用。
    ⚠️ `timezone.make_aware` 在夏令时切换的钟点会算错（vendored 源码原话 "This may be wrong around DST changes!"）——本仓时区无夏令时，不受影响；若将来改 `TIME_ZONE`，本助手与上面的等价性论证要一起重审。
    """
    start = timezone.make_aware(datetime.combine(day, time.min))
    end = timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min))
    return start, end


class FinanceReadPermissionMixin:
    """读接口的闸门：`has_node(FINANCE_READ_NODE)` + 可辨的 403 `msg`。 ⚠️ 判据是 RBAC **节点 code**（D-0028 / Ruling T）：**不要**新增 `HasPermission` （Django codename，已冻结为 legacy）或 `HasRole("财务")`（`财务` Group 已被 D-0030 删除）。
    ⚠️ `permission_denied` **必须转调 `super()`**：DRF 在那里先判「有没有认证成功」并抛 `NotAuthenticated`（401），自己直接 `raise PermissionDenied` 会把匿名也变成 403、破坏 spec §7 首段的分层（P4-7 的 `SettleView` 已按同一条写法收口）。
    ⚠️ `msg` 必须**可辨**（spec §17.4 #4）：403 有多个来源（CSRF 失败 / 未认证…），断不出权限语义的 403 会把别的原因当成「无权限」通过。
    """

    #: 子类各自给一句中文；默认值只是兜底，⛔ 不要靠它。
    denied_message = "你没有查看结算数据的权限"

    def get_permissions(self):
        """闸门 = `has_node(FINANCE_READ_NODE)`。 ⚠️ 写成**方法**而不是类级 `permission_classes`：接线位点表的 T2 判据只解析**方法体**
        （出现 `has_node(...)` 或 `.permission_classes` 才算权限装配），全仓既有锚点都是 `类.get_permissions`。行为与类属性写法一致（DRF 默认实现就是实例化 `permission_classes`）。
        """
        return [has_node(FINANCE_READ_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(request, message=self.denied_message, code=code)


class SettleView(APIView):
    """结账（spec §4.3）。六步逻辑全在 `apps/business/settlement.py`，视图只做「翻译」。"""

    def get_permissions(self):
        """闸门 = `has_node(SETTLE_NODE)`（理由同 `FinanceReadPermissionMixin.get_permissions`）。

        ⚠️ **人工修正端点共用本节点**（P4-11，spec §6「同结账，暂不单开」）——
        两个消费方都登记在接线位点表里，改这里要注意别把另一个漏掉。
        """
        return [has_node(SETTLE_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（spec §17.4 #4：403 有多个来源，断言必须能区分「无权限」与「CSRF 失败」等其他 403）。 ⚠️ **必须转调 `super()`**：DRF 的 `permission_denied` 在抛 `PermissionDenied` 之前
        先判「有没有认证成功」——未认证时它抛 `NotAuthenticated`（401）。自己直接 `raise PermissionDenied` 会把未认证也变成 403，破坏 spec §7 首段的分层要求。
        """
        super().permission_denied(request, message="你没有结账权限", code=code)

    def post(self, request):
        # 第 1 步：入参校验（空列表 / 非整数 / 超长列表 / null / 超长 reason ⇒ 200 + 1000）。
        serializer = SettleRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        actor = getattr(request.user, "username", "") or ""
        try:
            result = settle_performances(
                data["performance_ids"],
                actor=actor,
                user=request.user,
                reason=data.get("reason", ""),
            )
        except SettleError as exc:
            # 预期失败：业务码 + 整批已回滚（异常冒出 `atomic()` 块，见 settlement.py）。
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(
            data={
                "settled_performances": result.settled_performances,
                "settled_adjustments": result.settled_adjustments,
                # ⭐ 成功路径里**本来就是「已结账」目标状态、因此被跳过**的 id。
                # ⚠️ 与「查不到 / 越权 ⇒ 1006」是两件事，见 `settlement.SettleResult`。
                "skipped_performances": result.skipped_performances,
            }
        )


class SettlementPreviewView(APIView):
    """结算预览（**只读**，计划 §3 T2.1）：勾选的那批绩效现在值多少钱。「清算绩效」弹窗在点「确定结算或修改」**之前**用它填那几格（所选订单笔数 / 预计要结总额 / 实际需结总额 / 结算对象 / 微信收款码）。逐字段的口径与出处（对标实测 / 我们自己的算式 / 我们的取舍）写在 `apps/business/settlement_preview.py` 的模块 docstring 里——本视图只做三件事：过序列化器 → 调取数聚合 → 装信封。 ⚠️ **闸门 = `finance.payroll`（读节点，与结算列表 / 待归属 / 结算对象人员同一颗）**， ⛔ **不是** `finance.change_settlement_status`：本端点**不改**任何东西。 `settle` / `revoke` 两个写端点的节点一个字没动。
    ⚠️ **只读 ⇒ 零副作用**：不写库、不写审计、不改 `updated_at`（`build_settlement_preview` 只查、只算、只给收款码签名 URL）。 ⚠️ **本类为什么不复用 `FinanceReadPermissionMixin`**（照 `performance_write_views.AlipayDuplicatesView` 的先例）：接线位点表的 T2 判据解析的是 **本类自己声明的方法体**，继承来的 `get_permissions` 在 AST 里不属于本类 ⇒ 位点会判成假。判据本身是对的（位点就该钉在它真正被装配的那一处），所以这里显式写一遍——节点常量仍取 `FINANCE_READ_NODE`，⛔ 不另写一个字面量。 ⚠️ **畸形 / 缺失入参一律 200 + 业务码 `1000`**（不是 500、也不是 403）：闸门由 DRF 的 `initial()` 排在解析之前，所以未授权者拿不到 `1000`、也无法从错误差异里探测端点行为
    （与 `SettleView` / `SettlementPeopleView` 同一条分层）。
    """

    def get_permissions(self):
        """闸门 = `has_node(FINANCE_READ_NODE)`（与结算列表同一颗读节点）。

        ⚠️ 写成**方法**而不是类级 `permission_classes`：接线位点表的 T2 判据只解析方法体。
        """
        return [has_node(FINANCE_READ_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（必须转调 `super()`：它先判认证并抛 401）。

        ⚠️ 端点专属文案（不退回混入兜底那句）：403 有多个来源（CSRF 失败 / 未认证…），
        「与列表同一句」会让 `tests/test_settlement_preview_api.py` 的 msg 钉点失去分辨力。
        """
        super().permission_denied(request, message="你没有查看结算预览的权限", code=code)

    def get(self, request):
        # 入参过序列化器（E-0010）：非整数 / 空 / 超长 ⇒ 200 + code 1000，不是 500。
        # ⚠️ 空串按「没传」处理（与 `SettlementViewSet._params` 同口径）：前端常会送
        #    `?performance_ids=`，那是「没选」而不是非法值 ⇒ 由 required 报 `1000`。
        raw = {k: v for k, v in request.query_params.items() if v != ""}
        params = SettlementPreviewQuerySerializer(data=raw)
        params.is_valid(raise_exception=True)
        return api_response(
            data=build_settlement_preview(
                params.validated_data["performance_ids"], user=request.user
            )
        )


def settlement_rows(data):
    """结算列表的**取行口**（基础集 + 9 个筛选 + 两条软删排除）—— ⭐ T4 抽出的**唯一出处**。（9 = 原 8 个 + D-0323 的 `order_source` 两态筛选；`order_extra_views.BillListView` 的 docstring 同步改。） ## 为什么抽出来（T4）对外 `GET /api/open/v1/bills/` 要的正是这同一套筛选口径，而它原先**长在视图方法体里** ⇒ 对外端点只有两条路：① 抄一份（同一件事出现两处判据，`AGENTS.md` §5-E / E-0021 家族；抄袭版还会新增 `order__` join 落点，要把同一件事在结构网上再登记一遍）； ② 把它抽成这个模块级函数、两处都调它（**本做法**）。 ⛔ 外部行为**逐字不变**：本函数体就是原 `get_queryset` 里「基础集 + 筛法」那一段原样搬过来的。
    ## ⛔ 本函数**不套** `scope_queryset`（数据权限卡点）—— 这一条是安全边界卡点属**内部端点自己的装配**，留在 `SettlementViewSet.get_queryset` 里： ⛔ **绝不许**为了"让对外端点调用方便"把它从那里去掉或下移进本函数 —— 去掉等于 **放开内部结算端点的游戏范围**（那是安全事故，不是重构）。调用方要卡点就自己套，两条路的取舍**写在调用点上**： - **内部**结算列表（`SettlementViewSet.get_queryset`）⇒ 套 `scope_queryset(user, …, game_scope("order__game_id"))`（spec §6.1 的卡点）； - **对外** `bills/`（`apps/openapi/api/order_extra_views.py`）⇒ ⛔ 不套： v2 设计文档 §13.10 的裁定是「一个应用凭据 = 本租户全部业务数据」。
    ⚠️ 这不是"顺手放宽"：Bearer 请求下 `request.user` 是 `AnonymousUser` （`apps/openapi/authentication.py` 第 7 步），套上去会走 `game_scope` ⇒ `games_of(AnonymousUser)` 是**空集** ⇒ 列表**恒空**（静默空洞，E-0004 形态）——那正是本函数被抽出来的直接动因。 `data` = `SettlementSearchSerializer` 校验过的入参（入参的唯一校验口仍是 `SettlementViewSet._params`，⛔ 本函数不重新解析 query）。
    """
    # ⚠️ `prefetch_related` 是**必需**的，不是优化：`PayableNetField` 读 `instance.unsettled_adjustments`（`PayableNetField` 的 docstring 写了为何不做回落）。筛法（「未结账」）**不再在这里定义**：它收口到
    # `settlement.unsettled_adjustments_prefetch()`（唯一构造处）——接单统计（`apps/business/order_stats.py`）与这里共用同一份，避免同一判据两处定义。
    qs = Performance.objects.select_related("order", "user").prefetch_related(
        unsettled_adjustments_prefetch()
    )
    # ⭐ D-0144（C4）：已软删单的绩效行不进结算列表（跨关系 JOIN 不经过 `Order` 的    默认 manager ⇒ 必须显式补，spec §4.3-C2）。本处与 `game_scope` 那次 JOIN    复用同一条 `order` 关联。
    # ⭐ R-6（D-0147）：**绩效行自己的** `deleted_at` 同判——D-0144 的软删只随订单发生，而绩效垃圾桶会「订单活着、单条绩效行被删」，不补这一条，被删的行仍会出现在结算列表里、仍能被勾选结账。
    qs = qs.filter(order__deleted_at__isnull=True, deleted_at__isnull=True)
    # ⚠️ 判据一律 `is not None`（E-0021）：空串在入口已被剔除，此刻它与真值**等价**，
    #    但将来给某个字段加 `allow_blank=True` 时，真值判断会把「传了空串」**静默**当成没传。
    if data.get("user") is not None:
        qs = qs.filter(user_id=data["user"])
    if data.get("emp_type") is not None:
        qs = qs.filter(emp_type=data["emp_type"])
    if data.get("review_status") is not None:
        # 按某个审核状态筛**不含** `review_status IS NULL` 的行（还没交单的绩效）——
        # 与绩效列表同口径：`None` 不是「待审核」。
        qs = qs.filter(review_status=data["review_status"])
    if data.get("settle_status") is not None:
        qs = qs.filter(settle_status=data["settle_status"])
    if data.get("game") is not None:
        # 与绩效列表**同一条口径**（`performance_views.performance_queryset` 里就是
        # `order__game_id=data["game"]`）：同一事实只允许有一处定义。
        qs = qs.filter(order__game_id=data["game"])
    if data.get("order_no") is not None:
        # 精确匹配（spec §7 只给「订单号」这一格，没有模糊语义）。 ⭐（D-0301）：「订单号」精确框**双列 OR**——20 位系统号照旧命中，来源单号（录入的自由文本）也能精确命中（业务方口径「操作和业务逻辑用原订单号，
        # 展示与找回用来源号」）。仍是**精确**匹配（没有模糊语义，见序列化器注释）；空串在 `_params` 入口已按「没传」丢掉 ⇒ 此处 `v` 非空。
        qs = qs.filter(
            Q(order__order_no=data["order_no"])
            | Q(order__source_order_no=data["order_no"])
        )
    # ⭐ D-0323：**两态**的订单来源筛选（给结算记录页来源分解行的    「查看明细」用；两态语义与校验的唯一出处是    `SettlementSearchSerializer.validate_order_source`——此处只消费它的产物）：
    #    `int` ⇒ 指定来源；字面 `'none'` ⇒ **未指定来源**（`Order.order_source` 是可空 FK，    来源被删 / 录单没选都落进这一档）。`is not None` 判据（E-0021）。
    if data.get("order_source") is not None:
        if data["order_source"] == "none":
            qs = qs.filter(order__order_source__isnull=True)
        else:
            qs = qs.filter(order__order_source_id=data["order_source"])
    # 时间区间按**本地日期**收窄（`__date` 用当前时区切天），两端都是**含**的。
    if data.get("date_from") is not None:
        qs = qs.filter(settled_at__date__gte=data["date_from"])
    if data.get("date_to") is not None:
        qs = qs.filter(settled_at__date__lte=data["date_to"])
    return qs


def settlement_export_params(raw):
    params = SettlementSearchSerializer(data={k: v for k, v in raw.items() if v != ""})
    params.is_valid(raise_exception=True)
    return params.validated_data


def scoped_settlement_rows(user, data):
    return scope_queryset(user, settlement_rows(data), scope_filter=game_scope("order__game_id"))


class SettlementViewSet(FinanceReadPermissionMixin, viewsets.GenericViewSet):
    """结算列表（spec §7）。**只读**：只映射 `list`，没有 `create`/`update`/`destroy`。

    ⚠️ `http_method_names` 收窄到 GET/HEAD/OPTIONS：`GenericViewSet` 没有写方法时
    POST 本来就会 405，收窄是**结构**上的声明，少一层「将来谁加了 `create` 就悄悄多一个写入口」。
    """

    pagination_class = EnvelopePagination
    serializer_class = SettlementListSerializer
    http_method_names = ["get", "head", "options"]
    denied_message = "你没有查看结算列表的权限"

    def _params(self):
        """查询参数**唯一**的校验口（照 `performance_views._params` / `order_views._search_params`）。 ⚠️ 空串按「没传」处理：前端常会送 `?emp_type=` / `?user=`，那是「没选」而不是非法值。 ⚠️ 这里**必须**过序列化器：`?emp_type=BOGUS` / `?user=abc` / `?date_from=garbage`
        直接进 `filter()` 会抛 `ValueError`/`ValidationError`，而它们不在异常处理器的业务分支里 ⇒ **HTML 500**（E-0010 的形状）。
        """
        return settlement_export_params(self.request.query_params)

    def get_queryset(self):
        return scoped_settlement_rows(self.request.user, self._params())

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        try:
            data = self.get_serializer(page, many=True).data
        except AmountError as exc:
            # ⭐ `payable_net` **会透传 `AmountError`**（扣点越界等，见 `settlement_amount.py` 的 docstring）：列表逐行调用时必须预期这个失败形态，按业务码翻译——
            # ⛔ **不是**「净额按 0 算」（那是一笔静默错账），也不是 HTML 500（E-0010）。口径是**整表拒绝**：一行算不出来就不给一行假数；半张表比报错更容易被误读。
            return api_response(data=None, msg=exc.message, code=exc.code)
        return self.get_paginated_response(data)


class SettlementExportView(SettlementViewSet):
    """按结算列表的筛选和游戏权限导出明细。"""

    def get_permissions(self):
        return [has_node(FINANCE_READ_NODE)(), has_node("finance.export_performance")()]

    def list(self, request, *args, **kwargs):
        try:
            payload, _ = render_settlement_export(request.user, self._params())
        except AmountError as exc:
            return api_response(data=None, msg=exc.message, code=exc.code)
        record(
            log_type=ACTION_LOG_TYPES["settlement.export"], action="settlement.export",
            actor=request.user.get_username(), actor_id=request.user.pk,
        )
        response = HttpResponse(payload, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        response["Content-Disposition"] = 'attachment; filename="settlements.xlsx"'
        return response


def render_settlement_export(user, data):
    queryset = scoped_settlement_rows(user, data)
    entries_to_export = list(queryset[:20001])
    if len(entries_to_export) > 20000:
        raise ValidationError("本次导出超过 20000 行，请先收窄筛选条件")
    entries = SettlementListSerializer(entries_to_export, many=True).data
    rows = []
    for item in entries:
        amount = item["settled_amount"] if item["settled_amount"] is not None else item["payable_net"]
        label = {"打单工资": "代练报酬", "销售提成": "客服提成"}.get(item["emp_type"], item["emp_type"])
        rows.append((
            item["display_name"] or item["username"] or "", label,
            Decimal(item["temp_amount"]), Decimal(amount),
            item["source_order_no"] or item["order_no"], item["project"],
            (item["settled_at"] or "")[:16].replace("T", " "), item["settled_by"],
        ))
    return build_settlement_workbook(rows), [(entry.pk, entry.order_id) for entry in entries_to_export]


class UnattributedView(FinanceReadPermissionMixin, viewsets.GenericViewSet):
    """「待归属」清单（spec §3.8 / §7）：已 `COMPLETED` 但**没有销售提成行**的订单。 **为什么存在**：`sales_user` 回填匹配不到（P4-1）或录单时归属人为空时，那类单 **不会产生销售提成行**，钱就没人领。它们不能静默消失——这个清单是财务的处理入口。 ⭐ **判据是「没有销售提成**行**」，不是「`sales_user` 为空」**（spec §3.8 的措辞逐字）：两者**不等价**——`sales_user` 有值、但客服行没建出来（`on_order_completed` 在归属人事后被清空时**刻意不重算**，`settlement_hooks.py`）的单**同样算待归属**。
    用 `Exists` 显式写成「有没有这样一行」，而不是 `.exclude(performances__emp_type=...)`：后者对多值关系的语义要看 Django 的 `exclude` 规则，是本项目**不该赌**的一处（`AGENTS.md` §5：断言第三方行为前先读源码；这里换成不需要赌的写法）。 ⚠️ 只读、过 `scope_queryset`（spec §6.1）。 ⚠️ 用 `GenericViewSet`（而不是 `APIView` + 手工 `EnvelopePagination()`）是为了让 `pagination_class` **真的被消费**——类上声明一个 DRF 不读的属性是**看起来生效**的死配置。
    """

    pagination_class = EnvelopePagination
    serializer_class = OrderListSerializer
    http_method_names = ["get", "head", "options"]
    denied_message = "你没有查看待归属清单的权限"

    def get_queryset(self):
        # ⭐ D-0144（计划 C5 的行号落在这里）：这一处是 `Order.objects` **直读**    （默认 manager 已排除已软删的单）⇒ 一行不改，也不需要 `order__deleted_at` 条件。
        #    ⚠️ 计划 / spec 把本行标成「结账（`settle`）」，但 `settle` 走的是    `settlement.settle_performances`（那边的两处 join 已逐处补）。
        qs = (
            Order.objects.filter(status=OrderStatus.COMPLETED)
            # ⭐ 评审 Important 6：镜像卡派生的隐藏单不是「已完成的真单」⇒ 不进待归属
            #    （它没有客户 / 绩效，出现在财务清单里会让人去处理一笔不存在的生意）。
            .exclude(hall_card__isnull=False)
            .exclude(upstream_hall_card__isnull=False)
            .annotate(
                has_sales_row=Exists(
                    Performance.objects.filter(
                        order=OuterRef("pk"),
                        emp_type=EmpType.SALES,
                        # ⭐ R-6：被软删的 SALES 行不算「有销售提成行」——它已经不在了，
                        #    这笔提成没人领，必须回到待归属由财务处置（不补则静默消失）。
                        deleted_at__isnull=True,
                    )
                )
            )
            .filter(has_sales_row=False)
            # ⚠️ **F4**：本视图用 `OrderListSerializer` 出载荷，而 `hall_publisher_name` 会解引用 `Order.hall_publisher.username` ⇒ 不预取就是
            # **每行一条额外 SQL**（`?page_size=200` 即 200 条）。与 `selectors.search_orders` / `search_hall` 里的同款写法同源。
            .select_related("hall_publisher")
        )
        return scope_queryset(
            self.request.user,
            qs,
            # ⭐「按游戏权限收窄」：本视图的主语是**订单** ⇒ `game_id`
            #    （与结算列表那处的 `order__game_id` 是两条路径，按各自的模型选）。
            scope_filter=game_scope("game_id"),
        )

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)


class SettlementPeopleView(FinanceReadPermissionMixin, APIView):
    """「结账对象人员」的候选（只读，D-0089）。语义 = **在结算数据里出现过的人**，可按游戏收窄——⛔ **不是**对标的「该游戏下被分配的账号」（`StaffProfile.games`）。**偏离理由是一条实测反例**： dev 租户里唯一有结算行的人（`user=1`）`games=[]`，而被分配了游戏的那个账号 **一条绩效都没有** ⇒ 照对标语义实现的话，一选中游戏就一个人也选不出来。详情与证据见 spec §6.2。
    ⚠️ **不按 `is_active` 过滤**：本端点回答的是「数据里出现过的人」，不是「当前能登录的人」；停用账号的历史绩效行仍要能按人筛出来。 ⚠️ **过一次 `scope_queryset`**（spec §6.1：结算域的读路径逐条都过卡点）：今天它是 pass-through，将来注册 `scope_filter` 时这里会跟着收窄。 ⚠️ **必须显式 `order_by`**：`Performance.Meta.ordering` 是 `["-created_at", "-id"]`，而 `values(...).distinct()` 会继承它——Django 会把那两列**加进 SELECT 列表**兜住 ORDER BY，
    于是 `DISTINCT` 作用在 `(user_id, username, created_at, id)` 上 ⇒ **同一个人的每一行绩效都变成一条候选**（返回 200 的静默错数据，**不是 500**；2026-09-22 实测：1 行 vs 4 行）。显式 `order_by("user__username")` 同时**替换**掉 Meta 的默认排序，DISTINCT 才真正落在「人」上。 ⚠️ **不分页**：候选集 = 有绩效行的人，天然有界（租户人员量级）；出参固定 `{"items": [...]}`，与前端契约逐字一致。
    """

    # ⚠️ 端点专属文案（M-1）：与 `SettlementViewSet` / `UnattributedView` 一样各自可辨。
    # ⛔ 不要退回混入兜底那句「你没有查看结算数据的权限」——403 有多个来源，
    #    「与列表同一句」会让 `tests/test_settlement_api.py` 的 msg 钉点失去分辨力。
    denied_message = "你没有查看结算对象人员的权限"

    def get(self, request):
        # 入参过序列化器（E-0010）：非法 game ⇒ 200 + code 1000，不是 500。
        raw = {k: v for k, v in request.query_params.items() if v != ""}
        params = SettlementPeopleSearchSerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data

        # ⭐ D-0144（C6）：候选名单里的人**不来自已软删单**（同一处 join 条件）。
        # ⭐ R-6：也**不来自被软删的绩效行**——否则会出现「结算列表看不到这个人的行、
        #    人员下拉里却有他」（与 `game_scope` 收窄那条理由同源）。
        qs = Performance.objects.filter(
            user__isnull=False,
            order__deleted_at__isnull=True,
            deleted_at__isnull=True,
        )
        # 判据一律 `is not None`（E-0021）：`SettlementViewSet` 用同一个口径，
        # 别让「传了 0/空」在这里被真值判断静默丢掉（0 在入口已被 `min_value=1` 拒）。
        if data.get("game") is not None:
            # 与结算列表 / 绩效列表**同一条口径**（`order__game_id=data["game"]`）：
            # 同一事实只允许有一处定义。
            qs = qs.filter(order__game_id=data["game"])
        # 数据权限的**唯一卡点**（spec §6.1：结算域的读路径逐条都过它，见 `SettlementViewSet.get_queryset` 与 `UnattributedView.get_queryset`）。 ⭐「按游戏权限收窄」：候选集 = 结算数据里出现过的人 ⇒
        #    以**绩效行的游戏**为口径收窄（`Performance` ⇒ `order__game_id`）。    不收窄就会出现「结算列表看不到那个人的行、人员下拉里却有他」。 ⚠️ 接线有判别力用例：`tests/test_scope_performance.py::PerformanceScopeTests`
        #    `.test_settlement_people_hides_another_games_people`。
        qs = scope_queryset(
            request.user, qs, scope_filter=game_scope("order__game_id")
        )
        rows = (
            # ⭐（display-fixes 线）：加一把 `display_name`（昵称优先、空串回落登录账号）——前端下拉的**文本**用它，⛔ 不再显示登录账号。
            # ⚠️ 表达式的**唯一出处**是 `selectors.py::display_name_expression()`（SQL 注解侧口径），    ⛔ 不在这里再抄一遍 `Coalesce(NullIf(...))`（抄第二份就会各自漂移）。
            qs.annotate(name=display_name_expression())
            .values("user_id", "user__username", "name")
            .distinct()
            .order_by("user__username")
        )
        # ⚠️ `username`（登录账号）**保留**：它是既有契约、也是「这个人是谁」的事实本身； `display_name` 是**加法的**新键。排序口径**不动**（仍按登录账号升序）。
        # ⚠️ `DISTINCT` 现在落在 `(user_id, user__username, name)` 上：`username` 唯一 ⇒ `name` 也唯一 ⇒ 仍是**一人一条**（判别力用例 `test_returns_distinct_usernames_sorted`）。
        items = [
            {
                "id": row["user_id"],
                "username": row["user__username"],
                "display_name": row["name"],
            }
            for row in rows
        ]
        return api_response(data={"items": items})


def _settlement_scope(queryset, user, game_ids, apply_game_scope):
    if apply_game_scope:
        return scope_queryset(user, queryset, scope_filter=game_scope("order__game_id"))
    if game_ids is not None:
        return filter_by_game_ids(queryset, "order__game_id", game_ids)
    return queryset


class SettlementDailySummaryView(FinanceReadPermissionMixin, viewsets.GenericViewSet):
    """⭐ D-0319：结算记录页主视图的**按天聚合**端点（`GET /settlements/daily-summary`）。计划：`docs/plans/2026-10-28-settlement-daily-view.md` §2.1。一行 = 一个结账日（`settled_at` 在业务时区下的日期），同日多结算对象合并成一行；字段三把键： `date` / `order_count` / `total_amount`。 ## 聚合口径（每条都是拍板过的，不要「顺手修正」） - **只聚合已结账行**：`settle_status = 已结账` **且** `settled_at` 非空（两个条件都写，与计划 §2.1 逐字一致；单看状态列会放进「已结账但没写时间」的存量行——那行归不进任何一天）。 - **归天 = `TruncDate("settled_at")`**：Django 的 `Trunc*` 在 `tzinfo=None` 时用 **当前时区**（vendored `.venv/.../django/db/models/functions/datetime.py` 的 `TimezoneMixin.get_tzname` → `timezone.get_current_timezone_name()`），而本仓 `config/settings.py` 的 `TIME_ZONE = "Asia/Shanghai"`、`USE_TZ = True` ⇒ 按 **UTC+8** 的日期归天（D-0302 收口口径）。仓里同一机制的先例： `apps/business/report_stats.py` 的 `TruncMonth("stat_at")`（经营报表按月分组）。 ⛔ 不在这里自造时区换算（不写 `+8 hours` 的 SQL 片段、不引 `pytz`）。 - **`order_count` = distinct 订单数**：`Count("order_id", distinct=True)`——一单多条
    绩效行只计 1（业务方拍板 §6-1；若将来要「明细行数」口径，改这一处 annotate）。 - **`total_amount` = `Sum("settled_amount")`**：实结快照合计，与明细页「实际结算金额」列同源（计划 §5：预计合计不入每日行）。⚠️ **存量行**（快照列上线前结的账， `settled_amount IS NULL`）在 SQL `SUM` 里按 NULL 忽略 ⇒ **不计入合计**，但它们 **仍计入 `order_count`**（`Count` 不看金额列）——两个后果都要知道： ① **混合天**（有快照行 + NULL 快照行）：`total_amount` 只是有快照那部分的和， `order_count` 却含 NULL 行的单 ⇒ 单数与金额「对不上明细合计」； ② **全 NULL 天**：`order_count > 0` 而 `total_amount` 是 `"0.00"` （`list` 里的判空回落，不是「这天没钱」）。与明细列「null 回落 `payable_net`」的显示口径**不一致**，这是计划选定 SQL 层聚合（⛔ 不许取全量再 Python 聚合 ⇒ 回落要把 `payable_net` 的算式在 SQL 里再抄一份，那是两个口径定义处）的已知代价，如实登记不改。 - **排序 `-day`**（最新的一天在最前，财务审查视图的惯例）。⚠️ 必须显式 `order_by`： `Performance.Meta.ordering` 是 `["-created_at", "-id"]`，不替换会把那两列卷进 `GROUP BY`（`SettlementPeopleView` 的 docstring 记了同一坑的 DISTINCT 形态）。
    ## 闸门与数据范围：与 `/settlements` 完全同闸 `FINANCE_READ_NODE`（`FinanceReadPermissionMixin`）+ 同一套 `scope_queryset(user, …, game_scope("order__game_id"))`。⛔ 不新开权限节点、不放宽范围。软删排除（`order__deleted_at__isnull` + 行自己的 `deleted_at__isnull`）照 `settlement_rows` 的口径（D-0144 / R-6），但 ⛔ **不复用 `settlement_rows`**——它拖着 8 个筛选与 `payable_net` 的 `prefetch_related`（聚合用不上），照 `SettlementPeopleView` 自建最小基集的先例。 ⚠️ `http_method_names` 收窄到 GET/HEAD/OPTIONS（与 `SettlementViewSet` 同形的结构声明）；它接受 GET ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（畸形参数 ⇒ 200 + 信封）。
    """

    pagination_class = EnvelopePagination
    # 没有模型序列化器：聚合行是 `values()` 出来的 dict，`list` 里手工装载荷
    # （金额按 `report_stats._money` 的口径 `f"{Decimal:.2f}"` 串成型，与仓里金额序列化惯例一致）。
    serializer_class = None
    http_method_names = ["get", "head", "options"]
    denied_message = "你没有查看结算汇总的权限"

    def _params(self):
        """查询参数**唯一**的校验口（照 `SettlementViewSet._params` 的形态）。

        ⚠️ 空串按「没传」处理；非法日期 ⇒ 200 + `code 1000`（E-0010，不是 HTML 500）。
        """
        raw = {k: v for k, v in self.request.query_params.items() if v != ""}
        params = SettlementDailySummarySerializer(data=raw)
        params.is_valid(raise_exception=True)
        return params.validated_data

    def get_queryset(self, *, params=None, game_ids=None, apply_game_scope=True):
        data = self._params() if params is None else params
        # 只聚合已结账行（两条判据的理由见类 docstring）。
        qs = Performance.objects.filter(
            settle_status=SettleStatus.SETTLED,
            settled_at__isnull=False,
            # 软删排除（D-0144 / R-6）：与 `settlement_rows` 同口径——跨关系 JOIN 不经过
            # `Order` 的默认 manager，两条都要显式补。
            order__deleted_at__isnull=True,
            deleted_at__isnull=True,
        )
        # 时间区间按**本地日期**收窄、两端含——但筛本体是**半开区间**的 aware 即时刻
        # （`>= 当日 00:00` / `< 次日 00:00`，窗口见 `_local_day_window`）：
        # 裸列比较可吃到 `settled_at` 的 b-tree（W-01），语义与旧 `settled_at__date` 逐行等价。
        if data.get("date_from") is not None:
            window_start, _ = _local_day_window(data["date_from"])
            qs = qs.filter(settled_at__gte=window_start)
        if data.get("date_to") is not None:
            _, window_end = _local_day_window(data["date_to"])
            qs = qs.filter(settled_at__lt=window_end)
        if data.get("emp_type") is not None:
            qs = qs.filter(emp_type=data["emp_type"])
        # 数据权限卡点（与 `SettlementViewSet.get_queryset` 同一颗、同一条 join 路径）。
        qs = _settlement_scope(qs, self.request.user, game_ids, apply_game_scope)
        return (
            qs.annotate(day=TruncDate("settled_at"))
            .values("day")
            .annotate(
                order_count=Count("order_id", distinct=True),
                total_amount=Sum("settled_amount"),
            )
            # ⚠️ 显式 `order_by` 替换 Meta 默认排序（理由见类 docstring），最新在前。
            .order_by("-day")
        )

    def list(self, request, *args, **kwargs):
        params = self._params()
        pagination = (request.query_params.get("page"), request.query_params.get("page_size"))
        return api_response(historical_settlement(
            request.user, params.get("date_to"), ("daily", params, pagination),
            lambda **scope: self._daily_data(params, **scope),
        ))

    def _daily_data(self, params, *, game_ids, apply_game_scope):
        page = self.paginate_queryset(
            self.get_queryset(params=params, game_ids=game_ids, apply_game_scope=apply_game_scope)
        )
        items = [
            {
                "date": row["day"].isoformat(),
                "order_count": row["order_count"],
                # 判空回落 = `_or_zero`（D-0323 抽出的唯一出处）：全 NULL 快照的那天 `Sum` 出 `None` ⇒ 按 `Decimal("0")` 串成型。
                # ⚠️ 不用 `or` 真值判断——`Decimal("0")` 虽是假值、今天碰巧无差别，    但边界判据按仓规不写真值（同一事实两处写法会各自漂移）。
                "total_amount": f"{_or_zero(row['total_amount']):.2f}",
            }
            for row in page
        ]
        return self.get_paginated_response(items).data["data"]


class SettlementDailySourcesView(FinanceReadPermissionMixin, APIView):
    """⭐ D-0323：`GET /settlements/daily-summary/sources`——某一天的总结算金额**按订单来源分解**（业务方拍板：结算记录页「总结算金额」列可点击展开，展开后按订单来源展示各自结算了多少笔、多少钱）。出参 `{"items": [{source_id, source_name, order_count, total_amount}]}`； **来源为空**的行 `source_id=null` / `source_name=null`（前端渲染「未指定」—— `Order.order_source` 是可空 FK，来源被删 / 录单没选都落进这一档）。 ## 聚合口径：**逐条复用 `SettlementDailySummaryView` 的**（两边口径漂移会当场被 `tests/test_settlement_daily_sources.py::SourcesMatchDailySummaryTests` 的对拍钉打红） - **同一基集**：`settle_status=已结账` + `settled_at` 非空 + 两条软删排除（`order__deleted_at__isnull` / 行自己的 `deleted_at__isnull`，D-0144 / R-6）。 - **同一天窗**：`settled_at` 落在 `[当日 00:00, 次日 00:00)` 的**半开区间** （窗口算式 = `_local_day_window`，与 daily-summary 的 `date_from`/`date_to`
    同一个助手；按**当前时区**（UTC+8）切天，与 `TruncDate("settled_at")` 归天是同一套口径（D-0302 收口口径；⛔ 不自造时区换算）。写成裸列比较而不是 `settled_at__date=<day>` 是 W-01 的 sargable 改写，逐行等价（对拍钉在测试里）。 - **同两个聚合量**：`order_count = Count("order_id", distinct=True)`（distinct 订单数，与主表「总单数」同口径）、`total_amount = Sum("settled_amount")`（实结快照合计， NULL 忽略；判空回落 `_or_zero`，与 daily-summary 同一个助手）。只是 GROUP BY 从 `TruncDate` 换成 `order__order_source`（id + name 两列）。 ## 排序（**自拟值**，D-0323 登记；SQL 只负责去 Meta 排序，最终序在 Python 里排） 1. `total_amount` **降序**（金额大的来源在前——展开区是给财务「哪个店铺结得多」看的）； 2. **「未指定」行垫底**（`source_id is None` 恒最后——它不是一家店，是兜底档）； 3. 金额相同按 `source_name` **升序**（稳定、可复算）。 ⚠️ SQL 侧先显式 `order_by("order__order_source_id", "order__order_source__name")`
    **替换** `Performance.Meta.ordering`（否则 `-created_at/-id` 会被卷进 GROUP BY， `SettlementDailySummaryView` docstring 记的同一坑），Python 侧再按上面三条重排。 ## 形状与闸门 - **不分页**（照 `SettlementPeopleView` 的先例）：一天的来源行数 = 租户来源字典的量级，天然有界；出参固定 `{"items": [...]}`。 - 闸门与数据范围与 `/settlements` / `daily-summary` **完全同闸**：`FINANCE_READ_NODE` + `scope_queryset(user, …, game_scope("order__game_id"))`。⛔ 不新开节点、不放宽。 - 该天无已结账行 ⇒ 空 `items`（**不是 404**）；`date` 缺失 / 非法 ⇒ 200 + `code 1000` （E-0010，闸门先于参数解析，未授权者拿不到 1000）。 - 它接受 GET ⇒ 自动进入 `test_no_route_500` 的 explicit-routes 腿（本视图过序列化器，天然满足「畸形参数 ⇒ 200 + 信封」）。
    """

    # 端点专属文案（与 `SettlementViewSet` / `SettlementDailySummaryView` 各自可辨，
    # 403 的 msg 钉点才有分辨力）。
    denied_message = "你没有查看结算来源分解的权限"

    def get(self, request):
        # 入参过序列化器（E-0010）：date 缺失 / 非法 ⇒ 200 + code 1000，不是 HTML 500。
        # ⚠️ 空串按「没传」处理（与 `SettlementViewSet._params` 同口径）⇒ 命中 required。
        raw = {k: v for k, v in request.query_params.items() if v != ""}
        params = SettlementDailySourcesSerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        return api_response(historical_settlement(
            request.user, data["date"], ("sources", data),
            lambda **scope: self._sources_data(request.user, data, **scope),
        ))

    def _sources_data(self, user, params, *, game_ids, apply_game_scope):
        day = params["date"]

        # 聚合基集：与 `SettlementDailySummaryView.get_queryset` **逐字同一套** （判据见类 docstring；改口径先看那边的拍板记录，⛔ 不要在这里「顺手修正」）。
        # 当天窗口：与 daily-summary 的时间范围筛同一个 `_local_day_window` （半开区间、当前时区切天；裸列比较是 W-01 的 sargable 形状）。
        day_start, day_end = _local_day_window(day)
        qs = Performance.objects.filter(
            settle_status=SettleStatus.SETTLED,
            settled_at__isnull=False,
            order__deleted_at__isnull=True,
            deleted_at__isnull=True,
            settled_at__gte=day_start,
            settled_at__lt=day_end,
        )
        if params.get("emp_type") is not None:
            qs = qs.filter(emp_type=params["emp_type"])
        # 数据权限卡点（与 `SettlementDailySummaryView.get_queryset` 同一颗、同一条 join 路径）。
        qs = _settlement_scope(qs, user, game_ids, apply_game_scope)
        rows = (
            qs.values("order__order_source_id", "order__order_source__name")
            .annotate(
                order_count=Count("order_id", distinct=True),
                total_amount=Sum("settled_amount"),
            )
            # ⚠️ 显式 `order_by` 替换 Meta 默认排序（理由见类 docstring）；
            # 这里只是 SQL 侧的确定性输出，最终展示序在下面 Python 里按三条自拟规则排。
            .order_by("order__order_source_id", "order__order_source__name")
        )
        # 排序（自拟，登记在类 docstring）：金额降序 → 未指定垫底 → 名称升序。
        # ⚠️ 「未指定」组的判据是 `source_id is None`（不是 `source_name`——名字列理论上
        #    随 FK 走，id 才是分组键）。
        ordered = sorted(
            rows,
            key=lambda row: (
                row["order__order_source_id"] is None,
                -_or_zero(row["total_amount"]),
                row["order__order_source__name"] or "",
            ),
        )
        items = [
            {
                "source_id": row["order__order_source_id"],
                "source_name": row["order__order_source__name"],
                "order_count": row["order_count"],
                # 串成型同 daily-summary（`_or_zero` + `f"{Decimal:.2f}"`）。
                "total_amount": f"{_or_zero(row['total_amount']):.2f}",
            }
            for row in ordered
        ]
        return {"items": items}
