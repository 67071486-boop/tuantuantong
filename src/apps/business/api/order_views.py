from collections import defaultdict
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from decimal import Decimal
from functools import partial

from django.conf import settings
from django.contrib.auth.models import User
from django.db import OperationalError, connection, transaction
from django.db.models import QuerySet
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounts.display import display_names_for_usernames, user_display_name
from apps.accounts.models import FavoriteAssignee
from apps.audit.actions import ACTION_LOG_TYPES, DETAIL_HIDDEN_ACTIONS
from apps.audit.display import actor_display_names, summary_for_display
from apps.audit.log import record, record_order_read
from apps.audit.models import OperationLog
from apps.business import (
    hall_preferred,
    jidanpu_direct_taker,
    jidanpu_member,
    jidanpu_upstream,
    peer_hall,
    split_group,
    upstream_receipt_sync,
    xgj_receipt_sync,
)
from apps.business.api.base import EnvelopeModelViewSet, OrderListPagination
from apps.business.api.jidanpu_direct_taker_views import (
    JidanpuFirstOptionsSerializer, JidanpuTakerAssignmentSerializer, JidanpuTakerSearchSerializer,
    JidanpuTakerVerifySerializer,
)
from apps.business.api.order_batch import parse_order_nos, run_batch, transition_targets
from apps.business.api.order_page_relations import order_page_relations
from apps.business.api.serializers import (
    ActorSerializer,
    AssignSerializer,
    AssigneeOptionsSerializer,
    BatchAssignSerializer,
    BatchDeleteSerializer,
    BatchHallPriceSerializer,
    BatchOrderTypeSerializer,
    BatchPaymentStatusSerializer,
    BatchPriceSerializer,
    BatchRecomputeSerializer,
    BatchRefundSerializer,
    BatchTransitionSerializer,
    ClearTakerSerializer,
    CreateOrderAssignSerializer,
    DeleteOrderSerializer,
    FavoriteAssigneeSerializer,
    FilterOptionsSerializer,
    HallListSerializer,
    HallPriceChangeSerializer,
    HallSearchSerializer,
    KefuOptionsSerializer,
    OrderNoteSerializer,
    OrderPenaltyListSerializer,
    OrderSearchSerializer,
    OrderDetailSerializer,
    OrderSerializer,
    OrderSplitListSerializer,
    PeerSplitOrderSerializer,
    OrderTransitionReasonSerializer,
    OrderUpdateSerializer,
    OrderBalanceUpdateSerializer,
    PaymentStatusChangeSerializer,
    PenaltyGroupSearchSerializer,
    PriceChangeSerializer,
    RecycleBinListSerializer,
    RecycleBinSearchSerializer,
    SalesAgentChangeSerializer,
    SplitOrderSerializer,
    ThrowToHallSerializer,
    XgjReceiptSyncSerializer,
    V2BatchDeleteSerializer,
)
from apps.business.approvals import missing_penalty_approval_logs
from apps.business.claim import ClaimError, claim_order
from apps.business.claim_channel import channel_for
from apps.business.commission import CommissionError
from apps.business.deposit_gate import deposit_enforced_for
from apps.business.finance_read_cache import catalog_options
from apps.business.game_scope import assert_game_allowed, game_scope, games_of
from apps.business.hall_simulated import viewer_sees_simulated
from apps.business.performance_amount import AmountError as _AmountError
from apps.business.intake_parser import parse_order_text
from apps.business.models import (
    MONEY_FIELDS,
    EmpType,
    Game,
    GameServer,
    Order,
    OrderSource,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from apps.business.order_flags import can_view_offer_price, can_view_taker, can_view_total_stats, scope_by_creator
# ⭐ 录单的**领域核心**（唯一实现）搬进了
# `apps/business/order_intake.py`；本视图的 `create` 与开放平台对外录单调**同一个**函数。
# `resolve_assign_taker` 是「指定的接手人能不能用」那条判据的唯一实现（`/assign/` 也用它）。
from apps.business.order_intake import (
    SPLIT_MEMBER_ZERO_PRICES,
    IntakeActor,
    create_order,
    resolve_assign_taker,
)
from apps.business.order_no import generate_order_no
from apps.business.order_status import (
    ISSUE_REASON_REQUIRED_MSG,
    IllegalTransition,
    apply_transition,
    stamp_local_action,
)
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, changes_approved_penalty_price
from apps.business.order_writes import (
    WriteContext,
    _fail,
    assign_order,
    change_hall_price,
    change_payment_status,
    change_price,
    change_sales_agent,
    clip_sales_agent,
    delete_order_write,
    lock_order_row,
    queue_externalpay_recheck,
    recompute_customer_commission_write,
    refund_total_write,
    resolve_sales_agent_candidate,
    set_default_assign_preference,
    sync_sales_agent_name,
    transition_order,
    update_order_fields,
    _record_taker_price_change,
)
# ⭐ D-0223：编辑订单那道「禁止录入相同的来源单号」闸读的开关。
from apps.business.system_switches import switch_enabled
# 上游订单互通（spec §7.1/§7.2）：订单域语义动作各记一条同事务待投递事件；本模块只在成功路径里调
#（清人走 performance._on_pool_entry 那条既有漏斗，⛔ 同一事实不实现两次）。/assign、/delete 的入队在 order_writes，写价三条同理；
# 本模块只剩 /claim 的 enqueue_order_grabbed，别把另外两个名字加回来。
from apps.business.peer_dispatch import enqueue_order_grabbed
# ⚠️ 本模块不再直接引用 performance 写侧符号（那些语句已搬进 order_writes.assign_order，唯一判据出处 spec §D5）；
# ⛔ 不 import main_performance_of——它在缺行时响亮抛 Performance.DoesNotExist，B-1 删无主行后 /assign、/submit 会变 HTTP 500；
# ensure_main_performance_of / create_performance_for_order / zero_performance_amounts 仍被本模块其它动作使用（/clear-taker、复制新单、/submit 容错读），保留。
from apps.business.performance import (
    create_performance_for_order,
    ensure_main_performance_of,
    zero_performance_amounts,
)
# ⚠️ `soft_delete_order` **不再**由本模块直接调用（搬进 `order_writes.delete_order_write`）
# ⇒ 死 import 一并删掉（与 A4 同口径）；`restore_order` 仍是 `/restore/` 的动作体。
from apps.business.recycle import restore_order
#: ⭐ 客服预计绩效线：/clear-taker/?isDelEmp=Y 清零 MAIN 两个金额列但不过 recompute_order_amounts ⇒ 主挂点在这里看不见
#:（计划 §2 T1.2 显式挂点 11）；它是客服预计的减项，必须显式补刷。
#: ⚠️ refresh_customer_estimate 永不抛 ⇒ 本端点成功/失败语义不变。
from apps.business.settlement_hooks import refresh_customer_estimate
from apps.business.selectors import (
    EFFECTIVE_TAKER_PRICE,
    SPLIT_COMPLETED_MEMBERS,
    SPLIT_IS_MAIN,
    SPLIT_IS_BALANCE_ANCHOR,
    SPLIT_MEMBERS,
    TAKER_IS_PLATFORM,
    TAKER_NAME,
    TAKER_SETTLED,
    assignee_options as assignee_option_rows,
    available_favorite_assignees,
    default_assignee_payload,
    favorite_assignees,
    fold_split_members,
    kefu_options as kefu_option_rows,
    latest_platform_push_prefetch,
    order_list_summary,
    penalty_children_for_parents,
    penalty_list_visibility,
    penalty_order_rows,
    publisher_options,
    search_hall,
    search_orders,
    search_recycle_bin,
    split_completed_members_expression,
    split_is_main_expression,
    split_is_balance_anchor_expression,
    split_members_expression,
    taker_is_platform_expression,
    taker_name_expression,
    taker_settled_expression,
    taker_options,
)
from apps.business.validation import (
    ValidationProblem,
    normalize_project_text,
    validate_order_chain,
    validate_required,
)
from apps.business.peer_sync import PEER_DIRECT_PENDING_MESSAGE
from apps.core.authentication import TenantBoundAuthentication
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response, error_response
from apps.core.error_codes import ErrorCode
from apps.core.exceptions import _first_message
from apps.core.permissions import reuse_request_permission_reads
from apps.core.scoping import scope_queryset
from apps.core.short_cache import hall_cache_ttl, short_cached_payload
from apps.rbac.permissions import has_node, has_node_now, nodes_of

#: 「这个键根本没在请求体里」的哨兵；⛔ 不能用 None 顶替：None/""/0 都是有语义的取值（E-0021 同族），
#: 拿 None 当「没带」会让「显式清空这一格」静默落回「没带」，在权限门上开一个口子。
_ABSENT = object()

#: 镜像卡隐藏单（Order.hall_card 非空）详情面放行的白名单：判据=只读（retrieve + 全部 detail GET action，今天只有 audit；
#: tests.test_order_read_path_net 按 AST 枚举两者对拍恰好相等）。
#: ⛔ 写动作一律不放行（fail-closed）：隐藏单只是镜像卡同步给下家大厅的载体，⛔ 不是本租户的一笔生意。
HALL_SHADOW_DETAIL_READ_ACTIONS = frozenset({"retrieve", "audit"})


def _clip_sales_agent(value: str) -> str:
    """本模块薄别名：唯一实现在 order_writes.clip_sales_agent（写侧也用它；领域核反向 import 视图层私有函数会把依赖方向倒过来）。
    ⛔ 别把实现抄回来（两份裁剪判据会漂移，表现是「同一个人两个名字」或「超宽时一处 500、一处静默截断」）；保留别名是因本模块调用点与注释按此名引用它，
    partial_update 的「值真的变了」闸也用同一裁剪口径去比（否则会造出反向假拒绝，理由见 order_writes.clip_sales_agent 的 docstring）。"""
    return clip_sales_agent(value)


def _hall_price_of(order, raw_price):
    """把 raw_price 按这一笔自己的 total_price 折算成 (Decimal, reason, issue)。解析与闸门全部复用单笔的 HallPriceChangeSerializer（spec §4.1/§D5 不复制校验逻辑），
    ⛔ 不在这里另写 ×pct÷100；% 档的分母必须是当前这一笔（spec §7-T4：两笔接单价不同⇒两个不同结果）⇒ 不能像 BatchPriceSerializer 在循环外只跑一次。
    ⚠️ ValidationError.detail 形状随字段而变 ⇒ 用 apps.core.exceptions._first_message 摊平成一句人话（与单笔端点同一个函数、同一条文案，前端把 msg 原样显示在确认清单里）；⛔ 不另写第二套摊平口径（必然分叉）。"""
    payload = HallPriceChangeSerializer(data={"price": raw_price}, context={"order": order})
    if not payload.is_valid():
        # 单笔端点那一支由 DRF 的异常处理器翻成 200 + 1000（`raise_exception=True`）；
        # 逐笔这一支翻成 `ineligible` 的 1000（复用同一个序列化器 + 同一个摊平函数）。
        return None, "", {
            "code": int(ErrorCode.VALIDATION),
            "msg": _first_message(payload.errors),
        }
    return payload.validated_data["price"], payload.validated_data["reason"], None


#: 空串不按「没传」处理的检索参数：枚举码单选，空串是非法取值；丢掉空串=静默全量，spec §8 第 15 条要求 ?payment_status= 回 1000。
#: 前端不选时不发这个参数（toOrderParams 丢空值）⇒ 不影响正常客户端。
#: 本常量是模块级唯一出处：列表与导出（OrderExportView）两入口都经 order_search_params 用它。
BLANK_IS_A_VALUE = frozenset({"payment_status"})


def order_search_params(request, serializer_class):
    """查询参数过一遍序列化器，返回 validated_data（模块级共享实现）。请求来源的值进类型化的列或查询之前一律过序列化器（E-0010）：
    ?game=abc 直接 filter 抛 ValueError、?price_max=abc 抛 ValidationError，都不在业务分支里 ⇒ HTML 500；参数校验收在这一个口（列表 5 个调用点与导出共用）。
    空串按「没传」处理（前端常送 ?status= 是「没选」不是非法值）；⚠️ 例外是 BLANK_IS_A_VALUE 里的参数（空串要进校验、由序列化器拒成 1000）。"""
    raw = {
        k: v
        for k, v in request.query_params.items()
        if v != "" or k in BLANK_IS_A_VALUE
    }
    params = serializer_class(data=raw)
    params.is_valid(raise_exception=True)
    return params.validated_data


class _HallPriceParseOrder:
    """请求级 %/金额解析用的空分母替身（只有 total_price 一个属性被读）。请求级那一次还没有具体哪一笔 ⇒ 用 total_price=0 的替身跑一遍，
    拿到「格式是否合法 + % 是否超上限」这两个与分母无关的答案；真正的金额由逐笔 _hall_price_of(order, …) 按该笔自己的接单价算出。
    ⛔ 别把这个替身当「一批共用的分母」传进循环（那会让 % 档两笔得到同一个结果）。"""

    total_price = Decimal("0")


#: 请求级解析用的空分母替身实例。
_HALL_PRICE_PARSE_ORDER = _HallPriceParseOrder()


def _operation_log_row(row, actor_display, *, source_order_no=""):
    """OperationLog 行 → 订单详情页两处日志用的 10 键字典。前 6 个键的名字与顺序一字不动（既有页面与用例在用：tests/test_order_api.py::test_audit_endpoint_returns_transition_and_price_history），新键（id/summary/log_type/actor_display）都是只读输出、一律追加在末尾；⚠️ actor_display 由调用方显式传入（审计日志线 spec §3.6）：本函数不查库，两个出口各自解析一次（逐行查=N+1），形状=显示名 or row.actor（昵称取不到或空串时回落登录账号），⛔ 不许出现空白列（与 apps/audit/api/serializers.py::get_actor_display 同一口径）。本函数是 audit 与 notes 唯一的行形状定义——两处各拼一份必然漂移；summary 只由服务端渲染（前端不许自己拼句子）。"""
    return {
        "action": row.action,
        "actor": row.actor,
        "reason": row.reason,
        "from": row.from_value,
        "to": row.to_value,
        "created_at": local_iso(row.created_at),
        # ── 新键（追加在末尾，不改既有 6 键）────────────────────────────
        # `id`：行主键，供前端渲染列表时做 key / tie-break（`created_at` 会撞）。
        "id": row.pk,
        # `summary`：**服务端已渲染好的那句「操作说明」**（对标 `remark` 那一列的整句）。
        # ⛔ 前端不许自己拼句子——渲染输入只能是行内列（spec §5.9）。
        "summary": summary_for_display(row, source_order_no=source_order_no),
        # `log_type`：8 值分类 code（对标有 `logType`，便于将来分档）。
        "log_type": row.log_type,
        # `actor_display`：操作人**显示名**（昵称，空则回落 `actor`）——前端「操作人」列取它。
        # 昵称**不落这一行的任何列**（`actor_name` 是写入面给 `summary` 用的），
        # 这里是读面按 `actor_id` 现查的显示派生值。
        "actor_display": actor_display,
    }


def _sorted_nonempty_params(request):
    """请求的查询参数 → 排序后的 (名, 值) 元组列表（短缓存的键料，web 与 taker 两个读口共用）。与 order_search_params 的「空串=没传」同一口径：
    空串参数不进键（对筛选本来就不生效，进键只会白白分裂缓存）。这里只产缓存键（json 序列化），值不进任何类型化的列或查询；真正校验仍由各 Serializer 在 build 里做。"""
    return sorted((k, v) for k, v in request.GET.items() if v != "")


def _hall_viewer_fingerprint(request):
    """大厅查看者的按人维度指纹（短缓存键的组成部分）：①游戏权限（games_of，管理员短路⇒自然全集；scope_queryset 同一来源）②免押开关（viewer_sees_simulated）③价格脱敏（HallListSerializer.REDACTED_PRICE_FIELDS 里每项自己的判据）④灰区脱敏（can_view_offer_price）——缺一个就是「换了人命中别人的缓存」。⚠️ ③与④今天是同一个判据、刻意双份 ⛔ 当冗余删（③走序列化器脱敏、④为灰行独立装配路径留位，将来灰行换判据时它已在键里）；⚠️ 必须在缓存键里：缓存命中时 build 根本不执行；这几判读本来就在请求路径里发生，这里只是提前算一次并进键 ⛔ 不为此新开重查询。"""
    games = tuple(sorted(games_of(request.user).values_list("id", flat=True)))
    simulated = viewer_sees_simulated(request)
    redacted = tuple(
        bool(pred(request.user))
        for _name, pred in HallListSerializer.REDACTED_PRICE_FIELDS
    )
    gray = bool(can_view_offer_price(request.user))
    return (games, simulated, redacted, gray)


_ORDER_LIST_STATEMENT_TIMEOUT_MS = 4000


@dataclass(eq=False)
class _HallSource:
    rows: QuerySet
    render: Callable
    moment: str
    price_rows: QuerySet | None = None
    matches: Callable | None = None
    snapshot: dict | None = None

    def candidates(self, *, eager=False):
        if self.price_rows is None and not eager:
            return [
                (time, (self, pk)) for pk, time in self.rows.values_list("pk", self.moment)
            ]
        rows = self.rows if eager else self.price_rows
        relevant = [row for row in rows if self.matches is None or self.matches(row)]
        if eager:
            self.snapshot = {row.pk: row for row in relevant}
        return [(getattr(row, self.moment), (self, row.pk)) for row in relevant]

    def load(self, keys):
        if self.snapshot is not None:
            return self.snapshot
        return self.rows.in_bulk(keys)


def _read_hall_page(page):
    keys_by_source = defaultdict(list)
    for source, pk in page:
        keys_by_source[source].append(pk)
    loaded = {source: source.load(keys) for source, keys in keys_by_source.items()}
    return [
        source.render(row)
        for source, pk in page
        if (row := loaded[source].get(pk)) is not None
        and (source.matches is None or source.matches(row))
    ]


def _hall_mirror_source(
    rows, *, render, price_in_range, display_price, binding, remote_price, price_params, preferred_only,
):
    has_price_filter = any(value is not None for value in price_params.values())
    source = _HallSource(rows, render, "first_seen_at")
    if not has_price_filter and not preferred_only:
        return source

    def matches(row):
        return (
            (not has_price_filter or price_in_range(row, **price_params))
            and (not preferred_only or hall_preferred.is_preferred_price(display_price(row)))
        )

    source.matches = matches
    # Compare the same rounded display price using just the pricing columns.
    source.price_rows = rows.select_related(None).select_related(binding).only(
        "first_seen_at", "hall_price_percent_at_sync", remote_price, f"{binding}__hall_price_percent",
    )
    return source


def _merge_hall_candidates(available, exited, *, eager=False):
    rows = peer_hall.merge_hall_rows_peer(
        *(source.candidates(eager=eager) for source in available),
    ) if available else []
    if exited:
        rows.extend(hall_preferred.merge_exited_rows(
            *(source.candidates(eager=eager) for source in exited),
        ))
    return rows


class OrderViewSet(EnvelopeModelViewSet):
    """订单接口。lookup_field 用 order_no（对外标识、客服会念的那个）；不做物理删除（spec §6），取消走状态流转到 REFUNDED。
    ⚠️ 一律用 api_response() 构造响应 ⛔ 不手搓 Response({...})：只有 api_response() 打 _enveloped 标记，手搓的信封会被 finalize_response 再包一层、业务码被静默冲成 0。
    ⚠️ 每一处 OrderSerializer(order) 都必须传 context={"request": request}（allowed_transitions 按调用方节点算，漏传则 request 为 None→节点空集→该字段对所有人变空）；逐 action 节点门见 ORDER_PERMISSIONS + get_permissions()（要求节点 code，groups=(...) 不再自动放行）。"""

    #: 逐 action 的节点 code 表（spec §4.2：api 点的消费方是 permission_classes）。⚠️ 元组=any（任一命中即放行，与 has_node(*codes) 一致）；
    #: ⚠️ 字符串必须是字面量：tests/test_rbac_structure.py 的 B2 走 AST 取非 docstring 字符串常量，变量拼的 code 看不见；
    #: ⚠️ 未登记在表里的 action 走默认权限类（fail-closed：仍要登录），不是默认放行。
    ORDER_PERMISSIONS = {
        "list": ("order.list",),
        "retrieve": ("order.view_detail", "order.view_content"),
        "create": ("order.add_edit",),
        "partial_update": ("order.add_edit", "order.submit_edit"),
        "update": ("order.add_edit", "order.submit_edit"),
        "parse": ("order.add_edit",),
        # 审计留痕是订单详情的一部分（对标 26 /Order/OrderView），退款/标签三个点我们没有对应功能、不在此挂。
        # ⚠️ 放宽：audit 对齐 retrieve 的 any 元组——只持 order.view_content 的账号能开详情页、也要能读同一页的日志（放宽既有端点，不是收紧）。
        # 登记：docs/plans/2026-09-22-order-detail-replica.md §2.1。
        "audit": ("order.view_detail", "order.view_content"),
        # ⭐ notes 挂对标专门节点 order.add_log（27，对标 /OrderAjax/SaveOrderPublieMark，与本端点 1:1 同功能；recon order-view.md §7）。
        # ⛔ 不借 order.view_detail：会让「只看详情」的自定义角色变成能写日志、按对标勾了 27 的管理员反而不生效（Ruling 10：docs/plans/2026-09-21-rbac-endpoint-enforcement.md）。
        # ✅ 默认角色不受影响：seed.BUILTIN_ROLE_MODULES 按 order 栏目全批非 flag 子节点（判据 tests/test_rbac_structure.py::test_b11_order_add_log_is_granted_by_the_module_wholesale）。
        "notes": ("order.add_log",),
        "transition": ("order.change_status",),
        # ⭐ 抛大厅是独占闸（业务方拍板）——只认 36 order.send_to_grab_hall；原 any(20/36/1162) 会让只持 20/1162 的人照样抛、36 形同虚设。
        # ⚠️ 带价仍要 1162（throw_to_hall 动作体里第二道闸）；⛔ 那个判据不进本元组（本元组只回答「为什么能到这个端点」）。
        # ⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取类体字符串常量。
        "throw_to_hall": ("order.send_to_grab_hall",),
        "withdraw": ("order.withdraw_from_grab_hall",),
        "assign": ("order.assign_taker", "order.submit_payout_price"),
        "peer_direct_assignment_options": ("order.assign_taker", "order.clear_taker", "order.submit_payout_price"),
        "jidanpu_direct_assignment_options": ("order.assign_taker",),
        "jidanpu_direct_assignment_search": ("order.assign_taker",),
        "jidanpu_direct_assignment_verify": ("order.assign_taker",),
        "jidanpu_direct_assignment": ("order.assign_taker",),
        "peer_direct_assignment_search": ("order.assign_taker",),
        "peer_direct_assignment": ("order.assign_taker",),
        "peer_direct_assignment_draft_options": ("order.assign_taker",),
        "peer_direct_assignment_draft_search": ("order.assign_taker",),
        "claim": ("grab_hall.take_order",),
        # ⛔ remote_claim 这一格已随租户级鸡蛋铺线拆除（docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md）：action 与路由都删了。
        # ⚠️ 上家线自己的镜像单接单入口是独立视图 JidanpuUpstreamHallClaimView，节点门同样是 grab_hall.take_order。
        "clear_taker": ("order.clear_taker",),
        "assignee_favorites": ("order.assign_taker", "order.submit_payout_price"),
        # ⭐ D-0146（方案②）：指派弹层候选名单端点。判弹层两个动作各自主张节点的并集（assign 的 any 元组 + clear_taker）：两入口共用同一层弹层、同一份名单。
        # ⛔ 这是「谁能读名单」的放宽，不是「谁能被指派」的放宽（数据范围与 GET /api/accounts/ 同源）；⛔ 不借 order.list（否则「只看订单列表」的角色读到全租户账号名单）。
        # ⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取字符串常量。
        "assignee_options": (
            "order.assign_taker",
            "order.submit_payout_price",
            "order.clear_taker",
        ),
        # 节点 code 是 `order.submit_work`（提交完单）。语义仍是 `/submit/`（代练员交单）。
        # 字符串必须是字面量：`tests/test_rbac_wiring.py` 的 T2 要在位点锚点里看见这个 code，
        # 留着旧串会变成假登记。
        "submit": ("order.submit_work",),
        # ⭐ 报问题单复用 order.submit_work(105)（业务方原话：复用并改名「提交完单和提交问题单」，改名见 apps/rbac/catalog.py）。
        # ⛔ 不许挂 order.change_status(16)：16 是通用 /transition/ 的门，持它的人能把任意单改成任意状态；业务方要的是「代练员只能把自己接的、代练中的单改成问题单」，这三条约束 16 一条都表达不了。
        # ⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取字符串常量（见本表上方注释）。
        "report_issue": ("order.submit_work",),
        "price": ("order.submit_payout_price", "order.edit_payout_price"),
        # 改收款状态挂对标专门节点 22（order.change_payment_status），不借读端点或通用编辑节点（Ruling 10）。
        # ⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取字符串常量（见本表上方注释）。
        "payment_status": ("order.change_payment_status",),
        # ⭐ 换接单销售客服归属（业务方拍板「换销售客服=提成也换人」）：只用这一个节点（对标 Menu_ID 1272 order.allow_change_sales_agent）——
        # ⛔ 不追加 add_edit/assign_taker 之类的 any 元组（那会让「能改单」自动等于「能把提成搬给别人」）。本动作是唯一能改 sales_user（提成归属）的路；
        # 通用 PATCH 改不了这一列：值变了时没节点⇒403、持节点⇒业务码 1000（单独写派生快照列会造出「列表显示乙、提成归甲」）。⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取字符串常量。
        "sales_agent": ("order.allow_change_sales_agent",),
        # 改发单价挂对标专门节点 20（order.set_offer_price，已 available=True，这里是新增消费方而不是翻真）。
        # 对标 1161/1162 的字面也是「修改发单价」，但它们被 /price/（改接单价）消费 ⇒ 归属不动（spec §9 第 10 条，登记为残差）。
        "hall_price": ("order.set_offer_price",),
        #: 复制成新单与录单同一个节点（都是「新增一单」的能力，spec §8.1）。⚠️ 对标的 order.restrict_create（禁止录入新订单）本项目未实现（裁定属产品决策）
        #: ⇒ 不单独判它：只判复制不判录单会造出「录单页能建、复制不能」的自相矛盾（残差 spec §12-1）。
        "copy": ("order.add_edit",),
        # ⭐ 拆单「继续拆分订单」挂与录单同一个节点（拆分=新增订单，业务方与 spec §2.3 入口 2 同一条口径）。
        # ⛔ 不新增权限节点、⛔ 不翻真：catalog.EXPECTED_NODE_COUNT 与权限网零改动。⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取字符串常量。
        "split": ("order.add_edit",),
        "split_peer": ("order.add_edit",),
        "hall": ("grab_hall.take_order",),
        # ⭐ D-0144：软删（对标 23）/ 回收站列表与恢复（对标 1219——**一个点管两件事**，照对标）。
        # ⛔ 1219 在目录里是 `NodeKind.PAGE`（已翻 `available=True`），
        #    但服务端拦截就落在下面这两条上——与对标「一个点同时管进页与还原」同形。
        "delete": ("order.delete",),
        "restore": ("order.recycle",),
        "recycle_bin": ("order.recycle",),
        # ⭐ D13：候选名单端点挂本页自己的 order.list（与 list 动作同一个 code）⇒ catalog.py / available 计数零改动（不翻真、不增点）。
        # ⚠️ apps/rbac/wiring.py 的 SITES["order.list"] 说明文字必须与本表同步。
        "filter_options": ("order.list",),
        "dictionary_options": ("order.list",),
        # ⭐ 订单管理页「销售客服」候选名单端点挂同一个本页节点 order.list。⛔ 不借 system_user.list（GET /api/accounts/ 的门，默认「客服」不持 ⇒ 盒子在、点开没人）；
        # ⛔ 也不借 assignee_options 的并集（那是指派动作的门，客服不持 ⇒ 403）——理由与 filter_options 逐条相同。
        # ⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取字符串常量。
        "kefu_options": ("order.list",),
        # ⭐ D-0160：管理订单页的批量端点。**逐动作挂各自那一个节点**，
        # 与单笔同一个 code——批量不是一种新权限，只是同一件事的批形态。
        "batch_price": ("order.submit_payout_price", "order.edit_payout_price"),
        "batch_payment_status": ("order.change_payment_status",),
        # ⭐ 订单管理页「同步收款」：本动作**复用** `order.change_payment_status`
        #    （22）——它做的唯一写操作就是「未收款 → 已结款」，与单笔/批量改收款同一件事。
        #    ⛔ 不新增节点：业务方要的是一颗操作同一字段的按钮，不是一套新权限。
        "xgj_receipt_sync": ("order.change_payment_status",),
        "batch_types": ("order.add_edit", "order.submit_edit"),
        "batch_hall_price": ("order.set_offer_price",),
        # ⭐ D-0160（A9）：批量指派打手/批量记退款，与各自单笔端点同一个 code（批量不是新权限，只是同一件事的批形态；spec §D6）。
        # ⚠️ batch_assign 是 any 元组（与单笔 assign 逐字相同）；带 price/fee 时另有 order.submit_payout_price 的请求级 403 闸，写在动作体里、逐单循环之前（⛔ 不是本表这条）。
        # ⚠️ batch_refunds 与 refund_views.REFUND_NODE 同 code、两处各写字面量（spec §D6 明文要求）——⛔ 不要 import 那边消重（结构网扫不到字面量）。
        "batch_assign": ("order.assign_taker", "order.submit_payout_price"),
        "batch_refunds": ("order.online_refund",),
        # ⭐ D-0160（A8）：批量软删/批量改状态，与各自单笔端点同一个 code（批量不是新权限；spec §D6）。
        # ⚠️ batch_transition 是写形态兜底门：target=COMPLETED 时 get_permissions() 按 target 改判 order.change_status_completed、
        # target 缺失（读取形态）时改判下面那条 any 元组。见那里的说明。
        "batch_delete": ("order.delete",),
        "batch_delete_v2": ("order.delete",),
        "batch_transition": ("order.change_status",),
        # ⭐ D-0160（A10）：重算客服销售提成。没有单笔对应物（spec §D15）⇒ 节点门是业务方裁定（Q1=候选 B）：挂 finance.change_settlement_status
        #（「重算客服提成」与结账/撤销结账/人工修正由同一批人操作）。⚠️ 这是该节点在本仓第一个跨 app 消费方（finance.* 挂在 business 端点上），
        # 已同批在 apps/rbac/wiring.py 位点补登记、重渲 docs/RBAC-WIRING-MAP.md。⛔ 不许放宽成 any 元组（spec §10-Q1 裁定块明禁）。⚠️ 字面量：tests/test_rbac_structure.py 的 B2 走 AST 取类体字符串常量。
        "batch_recompute_commission": ("finance.change_settlement_status",),
    }

    #: transition/batch_transition「目标态→更精确节点 code」例外表（D-0071；⭐ 追加 WAITING_GRAB）：16=非已完成、17=变为已完成（apps/rbac/catalog.py）；WAITING_GRAB 目标归 36（对标「发到内部抢单大厅」，业务方拍板项 F）；未登记的 target 落回 ORDER_PERMISSIONS 的 transition/batch_transition 格（=16）。
    #: ⚠️ 读取点必须写在 get_permissions() 函数体内（tests/test_rbac_wiring.py 的 T2 只扫位点锚点那个方法体）；与 order_status.GENERIC_TRANSITION_NODES 同口径，对拍网 test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint（夹具恰好持 16+36 ⇒ 只证明两处同源、不证明本格）；本格自己的判别钉子 = tests/test_rbac_throw_hall_gate.py 的 pin7/pin8（只持 36 不持 16 的账号：列表==待抢单、看不到其它目标态）。
    TRANSITION_TARGET_PERMISSIONS = {
        OrderStatus.COMPLETED: ("order.change_status_completed",),
        OrderStatus.WAITING_GRAB: ("order.send_to_grab_hall",),
    }

    def dispatch(self, request, *args, **kwargs):
        if request.method == "GET" and self.action_map.get("get") == "hall":
            with ExitStack() as self._hall_permission_scope:
                return super().dispatch(request, *args, **kwargs)
        return super().dispatch(request, *args, **kwargs)

    def check_permissions(self, request):
        # DRF authenticates before this hook. The session authenticator also serves Order Finance.
        scope = getattr(self, "_hall_permission_scope", None)
        if scope is not None and isinstance(
            request.successful_authenticator, TenantBoundAuthentication,
        ):
            scope.enter_context(reuse_request_permission_reads(request.user))
        return super().check_permissions(request)

    def get_permissions(self):
        """逐 action 装配节点判据；与默认权限类取并集（不替换）。transition/batch_transition 按请求体 target 挑 code：COMPLETED⇒17、WAITING_GRAB⇒36、其余⇒16（见 TRANSITION_TARGET_PERMISSIONS）；与 OrderSerializer.get_allowed_transitions 是同一个判据（两侧都按目标态取同一个 code，读侧映射出处=order_status.GENERIC_TRANSITION_NODES），只改一侧会被 tests/test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint 当场抓到（它拿端点当 oracle）；读 self.request.data 是刻意的——畸形请求体在权限层就 ParseError(400)、比「先按 16 放行再发现 target 要 17」更早更一致（self.request 不存在的结构测试不读请求体）。
        ⚠️ 为什么是并集：默认类=「这一层的最低门槛」（今天=IsAuthenticated），节点判据=「这个动作还要哪个 code」，两者合取——替换默认类会让「接口需要登录」这条全局不变式只在本视图失效。⚠️ 401 的出口是权限层、不是认证器（已读源码核实）：permission_denied 只要 request.authenticators 非空且 successful_authenticator 为空就抛 NotAuthenticated(401)（rest_framework/views.py:174-180，不看哪个权限类拒的）；本项目 TenantBoundAuthentication.authenticate() 对匿名返回 None（apps/core/authentication.py:14-15）而 DEFAULT_AUTHENTICATION_CLASSES 非空 ⇒ 条件恒成立；perform_authentication 的函数体就是 request.user（rest_framework/views.py:321-329），只负责惰性触发认证；401/403 分流点是 permission_denied 里那个条件（check_permissions 在 views.py:331-342 调它；initial 顺序见 views.py:419-421），不是「哪一层抛的异常」——这不是一条能靠 HTTP 状态码验证的性质（能验证它的是结构断言）。仍显式写 IsAuthenticated 三理由：①跟随框架级默认（DEFAULT_PERMISSION_CLASSES 就是它）②全局默认哪天改这里自动跟随、只有一个落点 ③把「这层要登录」写在声明处、不依赖框架 401 的间接路径。⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），不是默认放行；新增 action 忘登记 ⇒ 只是没有细粒度门，不会变成公开端点。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.ORDER_PERMISSIONS.get(self.action)
        if self.action == "transition":
            # 目标态 `COMPLETED` 要的是**另一个**节点（17）、`WAITING_GRAB` 要 36
            # （见 `TRANSITION_TARGET_PERMISSIONS`）。
            request = getattr(self, "request", None)
            data = getattr(request, "data", None) if request is not None else None
            target = data.get("target") if hasattr(data, "get") else None
            codes = self.TRANSITION_TARGET_PERMISSIONS.get(str(target), codes)
        if self.action == "batch_transition":
            # ⭐ A8（spec §D10/§D6）——与单笔 transition 同形再写一条。① target==COMPLETED ⇒ order.change_status_completed、target==WAITING_GRAB ⇒
            # order.send_to_grab_hall（同一张例外表）；② target 缺失/空串 ⇒ 读取形态（读选项不是写）：取 ("order.change_status","order.change_status_completed") 的 any 元组，
            # ⛔ 只放宽读、写那两半一个字不许跟着宽（target 一旦给了就走①/③）；③ 其余合法 target ⇒ 落回 ORDER_PERMISSIONS["batch_transition"]（=16）。
            request = getattr(self, "request", None)
            data = getattr(request, "data", None) if request is not None else None
            target = data.get("target") if hasattr(data, "get") else None
            if not target:
                codes = ("order.change_status", "order.change_status_completed")
            else:
                codes = self.TRANSITION_TARGET_PERMISSIONS.get(str(target), codes)
        if not codes:
            return base
        # IsAuthenticated 提到最前并保证恰好一个（重复无害但像笔误，显式去重）；其余默认类一条都不丢。
        # ⚠️ 判等必须 type(p) is not IsAuthenticated、不要 isinstance：isinstance 会把子类也一并丢掉——正是「其余默认类一条都不丢」要防的事。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    lookup_field = "order_no"
    lookup_value_regex = r"\d{20}"
    # ⚠️ 订单列表比别的列表多一个统计块 ⇒ 用 `OrderListPagination`（它只在被显式设置时
    #    才写 `summary`；大厅共用同一个分页器，见 `apps/business/api/base.py` 的哨兵）。
    pagination_class = OrderListPagination
    http_method_names = ["get", "post", "patch", "head", "options"]

    #: 保留为类属性别名（既有引用 `self.BLANK_IS_A_VALUE` 仍生效；策略本体的唯一出处是
    #: 模块级那个同名常量，⛔ 不要在这里再写一份字面值）。
    BLANK_IS_A_VALUE = BLANK_IS_A_VALUE

    def _search_params(self, serializer_class):
        """薄调用：策略本体是模块级 order_search_params（导出视图调同一个函数）。⛔ 本方法只保留「用 self.request 转调」这一件事——
        把策略再抄一份到这里，两个入口就会静默分叉（表现是「导出的是另一批数据」）。"""
        return order_search_params(self.request, serializer_class)

    def get_queryset(self, data=None):
        if data is None:
            data = self._search_params(OrderSearchSerializer)
        qs = search_orders(
            status=data.get("status"),
            payment_status=data.get("payment_status"),
            customer_kind=data.get("customer_kind"),
            game_id=data.get("game"),
            server_id=data.get("server"),
            project_text=data.get("project_text"),
            keyword=data.get("keyword"),
            is_urgent=data.get("is_urgent"),
            tag=data.get("tag"),
            date_from=data.get("date_from"),
            date_to=data.get("date_to"),
            # ⭐ D-0144：新增的筛选参数（键名与 `OrderSearchSerializer` 的字段同名）。
            order_source=data.get("order_source"),
            # ⭐ 公司店铺分组：按「来源所属分组」筛；导出侧同批转发。
            source_group=data.get("source_group"),
            order_type=data.get("order_type"),
            sales_user=data.get("sales_user"),
            kefu_type=data.get("kefu_type"),
            is_leak=data.get("is_leak"),
            show_price_type=data.get("show_price_type"),
            price_min=data.get("price_min"),
            price_max=data.get("price_max"),
            # ⭐ 价格口径线：`3 接发组合` 档的**发单价**那一对区间。
            hall_price_min=data.get("hall_price_min"),
            hall_price_max=data.get("hall_price_max"),
            order_nos=data.get("order_nos"),
            batch_lookup_nos=data.get("batch_lookup_nos"),
            taker_user=data.get("taker_user"),
            hall_publisher_user=data.get("hall_publisher_user"),
            # ⭐（拆单）：按拆单组取**该组全部成员**（显式给值 ⇒ 列表不折叠）。
            split_id=data.get("split_id"),
            # ⭐（业务方拍板）：镜像卡隐藏单不进列表；只读 detail 动作能看到它（HALL_SHADOW_DETAIL_READ_ACTIONS=retrieve+audit，恰好集合由 tests.test_order_read_path_net 按 AST 枚举 detail GET 动作对拍，漏登记/塞写动作网会红）。「隐藏」的语义=客服排障按单号看得到是哪张镜像卡派生的与它的留痕，⛔ 不是「写动作能拿它干活」：写动作拿到 get_object() 的 404→既有业务码 1002（与「不存在」同形）；/delete、/restore 另走 _order_including_deleted（同样排除）。⚠️ audit 必须一起放行：它读的是详情页留痕，挡掉它 ⇒ 轮询 /audit/ 回 1002、反复弹「记录不存在」。
            include_hall_shadow=(self.action in HALL_SHADOW_DETAIL_READ_ACTIONS),
        )
        # ⭐ 「订单来源」显示值读 order.order_source.name（order_source_display_name 挂字典，否则回落 source 快照）⇒ FK 必须预取
        #（否则一页 N 行 N 条字典查询；判据 tests/test_order_source_intake.py::OrderListSourceDisplayNameTests）。加在 get_queryset() 这个列表与 get_object()
        # 共用的构造点（详情序列化器也回同一派生键）；FK 是单值关系 ⇒ join 不放大订单行（order_list_summary 聚合不受影响）。
        qs = qs.select_related("order_source")
        # ⚠️ 列表载荷的接单人（spec §5.3）：相关子查询注解，OrderListSerializer 读它。⛔ 不能改成 performances__user__username 的 join
        #（performances 是多值关系，join 会放大订单行）。注解在 get_queryset() 加一次（列表与详情共用构造点；详情序列化器不声明这个键，多出一列只是开销）。
        qs = qs.annotate(**{TAKER_NAME: taker_name_expression()})
        # ⭐（业务方要求）：订单列表「接手人」列判「这个接手人是不是代练通的占位账号」（显示名「代练通」、文字转紫）。判据按 username（FK 派生），
        # ⛔ 不按显示名串相等；与 TAKER_SETTLED 同形（Exists 相关子查询、恒回布尔）。详情路径也带这个注解：不改语义，只多一列开销。
        qs = qs.annotate(**{TAKER_IS_PLATFORM: taker_is_platform_expression()})
        # ⭐ 钱包发薪线：「发单价」列的绿色「结」标记=该单打手绩效（emp_type=MAIN）是否已结账。与 TAKER_NAME 同为相关子查询注解
        #（⛔ 不在 SerializerMethodField 里逐行查：一页 200 行=200 次查询）；Exists 恒回 TRUE/FALSE ⇒ 没有 MAIN 行（大厅行/未接单）就是 False。
        # ⚠️ 详情路径也带这个注解：不改语义，只多一列开销（与 TAKER_NAME/SPLIT_MEMBERS 同一理由）。
        qs = qs.annotate(**{TAKER_SETTLED: taker_settled_expression()})
        # ⭐（拆单）：列表载荷 split_members（组内成员总数；非拆单=0）的取值口。与 TAKER_NAME 同一形状（相关子查询注解）⇒ 与 order_list_summary
        # 的聚合兼容（Query.get_aggregation 只把「会进 GROUP BY 的注解」并进内层，标量子查询两者都是一条、不放大行）。详情路径也带注解：不改语义，只多一列开销。
        qs = qs.annotate(**{SPLIT_MEMBERS: split_members_expression()})
        # ⭐ D-0357（G16，拆单组协作重设计）：列表载荷再加两键的取值口——split_completed_members（组内已完成数，v2 组卡「完成 X/N」）
        # 与 split_is_main（本行是否组内主单；非拆单恒 False）。与 SPLIT_MEMBERS 同形（相关子查询注解、Case 兜空串档）、与聚合兼容；
        # 详情路径也带注解但不声明键，只多一列开销（同上面那条注释的理由）。
        qs = qs.annotate(
            **{
                SPLIT_COMPLETED_MEMBERS: split_completed_members_expression(),
                SPLIT_IS_MAIN: split_is_main_expression(),
                SPLIT_IS_BALANCE_ANCHOR: split_is_balance_anchor_expression(),
            }
        )
        # 数据权限的唯一卡点（spec §4.4 / P5 §7.3 缓解 2）：P5 只交机制，调用方必须显式过一遍；规则本体在 apps/business/game_scope.py
        #（scoping.py 不内置业务规则，E-0006）。⚠️ 按游戏权限收窄 ⇒ 这条也作用于 get_object()（详细理由见 filter_queryset 说明）：
        # 看不见的行对这个视图就是「不存在」。租户管理员的短路在 scope_queryset 里（is_tenant_admin），这里不再判一次。
        qs = scope_queryset(self.request.user, qs, scope_filter=game_scope("game_id"))
        # ⭐ 平台推送读面：列表载荷 platform_push 读该单最近一条代练通推送行，一次 Prefetch 取回整页（⛔ 不在序列化器里逐行 filter：一页 200 行=200 条查询）；
        # 判据见 tests/test_dailiantong_platform_data.py 的 SQL 条数不随行数增长那条。
        # ⚠️ 只挂 list：get_queryset() 被 get_object() 共用而它们不消费这个键（详情「代练通」页签走 GET orders/<order_no>/platform-push/）⇒ 不白付一次预取查询。
        if self.action == "list":
            qs = qs.prefetch_related(latest_platform_push_prefetch())
        # ⚠️ order.restrict_to_self_created 不在这里施加：get_object() 用同一个 queryset，挂在这里会让受限用户对别人的单拿到 404——
        # flag 的失败形态是「数据被过滤」（spec §5.2），不是「存在的行看起来不存在」（Ruling 9）。它收口在 filter_queryset() 且只对 list 生效。
        return qs

    def filter_queryset(self, queryset, *, fold_splits=True):
        """order.restrict_to_self_created 的施加点：只收窄列表（Ruling 9）。⛔ 不能挂 get_queryset()：get_object() 也走 self.filter_queryset(self.get_queryset())（rest_framework/generics.py:87），挂上去=对全部走 get_object() 的 detail 动作一起收窄，受限用户看别人的单拿到「记录不存在或已被删除」、与「这单真的不存在」不可区分；flag 的失败形态是「数据被过滤」（spec §5.2）——少行，不是「存在的行看起来不存在」（不逐条枚举动作名：漏一个就是可被证伪的绝对句，判据是「走不走 get_object()」，那条边界是 DRF 的、不随新增动作漂移；列表路径 rest_framework/mixins.py:38）。⚠️ 大厅（hall）不经过本方法（直接 paginate_queryset(search_hall(...))）——刻意：大厅里的单往往不是代练员自己录的；与 scope_queryset 是合取（P5 数据权限卡点与 RBAC flag 各管一维），先后不影响结果集。
        ⚠️ 与 game_scope（按游戏权限收窄）刻意取相反做法（失败语义不同）：game_scope 收的是可见范围本身——不在范围内的行用户本就不该知道它存在 ⇒ get_object() 对它 200+1002 是期望形态、所以进 get_queryset()；本 flag 收的是「可见范围之内的一行」（用户有权看这一行、只是这条 flag 不让他看）⇒ 404 会把「被过滤」伪装成「不存在」（Ruling 9 要避免的）⇒ 只对 list 施加。判据=「被收掉的那一行，用户是否本就有权知道它存在」。⭐ 拆单折叠也挂本方法：必须先 scope_by_creator 再折叠——折叠是 Exists 相关子查询、依赖当次集合，顺序反了「主单是别人录的、子单是我录的」那一档整组消失（判据 tests/test_order_split.py::SplitRestrictToSelfCreatedTests）。"""
        queryset = super().filter_queryset(queryset)
        if self.action != "list":
            return queryset
        # ⚠️ 顺序承重：先按 created_by 收窄再折叠。折叠不是行内 WHERE，是 Exists 相关子查询、判据依赖当次集合（selectors.fold_split_members）；
        # 反序在这一档会整组消失：「主单是别人录的、子单是我录的」⇒ 折叠先按未收窄集合保下我看不见的主单、折掉我的子单，再收掉主单 ⇒ 行数为 0。
        # 判据：tests/test_order_split.py::SplitRestrictToSelfCreatedTests。
        queryset = scope_by_creator(self.request.user, queryset)
        # ⭐（拆单）折叠成「组内只留 id 最小者」。⚠️ 显式给了 split_id 就不折叠（展开档要的就是该组全部成员，含主单）；判据读过完序列化器的那份值
        #（_search_params，E-0010）：空串在 order_search_params 已丢成「没传」⇒ 与缺键同档。
        # ⚠️ 落点必须是 filter_queryset（⛔ 不是 get_queryset()）：后者被 get_object() 共用，折叠挂上去子单打不开详情页——「子单是真实订单行、既有入口一律不动」是本线承重判据（spec §2.4）。
        if fold_splits and not self._search_params(OrderSearchSerializer).get("split_id"):
            queryset = fold_split_members(queryset)
        return queryset

    def list(self, request, *args, **kwargs):
        restore_timeout = not connection.get_autocommit()
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                # pg_settings 的 statement_timeout 以毫秒计，0 表示没有上限。
                cursor.execute(
                    """
                    SELECT setting,
                           CASE WHEN setting::int = 0 OR setting::int > %s
                                THEN set_config('statement_timeout', %s, true)
                           END
                    FROM pg_catalog.pg_settings
                    WHERE name = 'statement_timeout'
                    """,
                    [_ORDER_LIST_STATEMENT_TIMEOUT_MS, f"{_ORDER_LIST_STATEMENT_TIMEOUT_MS}ms"],
                )
                previous_timeout, applied_timeout = cursor.fetchone()
                response = self._list_response()
                # 外层事务仍活着时，释放保存点会保留 SET LOCAL；出错时由回滚还原。
                if restore_timeout and applied_timeout is not None:
                    cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous_timeout])
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) != "57014":
                raise
            unavailable = status.HTTP_503_SERVICE_UNAVAILABLE
            return error_response(unavailable, "订单查询耗时过长，请缩小筛选范围或稍后重试。", unavailable)
        return response

    def _list_response(self):
        group = self._search_params(PenaltyGroupSearchSerializer)
        if group.get("group_penalty_children") or group.get("penalty_parent_order_no"):
            return self._penalty_list(group)
        data = self._search_params(OrderSearchSerializer)
        queryset = self.filter_queryset(self.get_queryset(data))
        visible_scope = (
            self.filter_queryset(self.get_queryset({}), fold_splits=False)
            if can_view_total_stats(self.request.user) else None
        )
        self.paginator.summary, self.paginator.known_total = order_list_summary(
            queryset, user=self.request.user, related_scope=visible_scope,
        )
        page = self._paginate_order_rows(
            queryset, ids_first=data.get("taker_user") is not None and not data.get("split_id"),
        )
        if page is None:
            return Response(self.get_serializer(queryset, many=True).data)
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def _paginate_order_rows(self, queryset, *, ids_first):
        page_number = self.request.query_params.get(self.paginator.page_query_param)
        if not ids_first and page_number in (None, "", "1"):
            return self.paginate_queryset(queryset)

        # 选页只取订单 ID，回查时保留原筛选条件与页内顺序。
        page_ids = self.paginate_queryset(queryset.values_list("pk", flat=True))
        if not page_ids:
            return page_ids
        rows = {order.pk: order for order in queryset.filter(pk__in=page_ids).order_by()}
        return [rows[pk] for pk in page_ids if pk in rows]

    def get_paginated_response(self, data):
        if self.action == "list" and settings.TT_CHAT_ORDER_GROUPS_ENABLED and data:
            from apps.internal_chat.order_groups import followable_order_numbers

            rows = list(data)
            for groups in (self.paginator.page_relations or {}).values():
                for members in groups.values():
                    rows.extend(members)
            eligible = followable_order_numbers({row["order_no"] for row in rows}, self.request.user.pk)
            for row in rows:
                row["chat_follow_up_available"] = row["order_no"] in eligible
        return super().get_paginated_response(data)

    def _penalty_list(self, group):
        data = self._search_params(OrderSearchSerializer)
        exact_matches = self.filter_queryset(self.get_queryset(data), fold_splits=False)
        parent_no = group.get("penalty_parent_order_no")
        split_id = data.get("split_id")
        include_unmatched = group.get("include_unmatched_split_members")

        # 展开时去掉定位原单的条件；状态等筛选和数据权限照常应用。
        related_filters = {
            key: value for key, value in data.items()
            if key not in ("keyword", "project_text", "order_nos", "batch_lookup_nos", "split_id")
        }
        related_scope = self.filter_queryset(self.get_queryset(related_filters), fold_splits=False)
        visible_scope = self.filter_queryset(self.get_queryset({}), fold_splits=False)
        if parent_no:
            rows = penalty_children_for_parents(
                related_scope, visible_scope, [parent_no],
                exact_split_matches=exact_matches if split_id else None,
            )
        elif split_id:
            matches = visible_scope.filter(split_id=split_id) if include_unmatched else exact_matches
            rows = penalty_order_rows(
                matches, related_scope, visible_scope, exact_split_matches=exact_matches,
            )
        else:
            matches = fold_split_members(exact_matches)
            page_filter = penalty_list_visibility(exact_matches)
            rows = penalty_order_rows(matches.filter(page_filter), related_scope, visible_scope)
            self.paginator.summary, self.paginator.known_total = order_list_summary(
                matches, user=self.request.user, page_filter=page_filter,
                related_scope=visible_scope if can_view_total_stats(self.request.user) else None,
            )

        page = self._paginate_order_rows(
            rows, ids_first=bool(parent_no) or (data.get("taker_user") is not None and not split_id),
        )
        context = self.get_serializer_context()
        # 录单人昵称按序列化批次缓存，每批从未填充缓存的 context 开始。
        items = OrderPenaltyListSerializer(page, many=True, context=context.copy()).data
        if group.get("include_page_relations") and not parent_no and not split_id:
            self.paginator.page_relations = order_page_relations(
                items, exact_matches=exact_matches, related_scope=related_scope,
                visible_scope=visible_scope, include_unmatched=include_unmatched, context=context,
            )
        return self.get_paginated_response(items)

    def get_serializer_class(self):
        if self.action == "list":
            # ⭐（拆单）：列表载荷比基类多末尾两个键（split_id/split_members），而基类被大厅/回收站/待归属三处复用、各有恰好集合的键表钉子
            # ⇒ 用子类只把这两个键加在订单列表这条读面上（理由逐条见 OrderSplitListSerializer 的 docstring）。
            return OrderSplitListSerializer
        if self.action in ("partial_update", "update"):
            # 内部编辑只放开 total_price 字段；写核心仍只允许拆分组非锚点改价。
            # 判据按 **action 名**而不是 HTTP 方法：`PUT` 今天被 http_method_names
            # 挡着，但那是 HTTP 层的事，守卫不该依赖它。
            return OrderBalanceUpdateSerializer
        return OrderSerializer

    @action(detail=True, methods=["post"], url_path="delete")
    def delete(self, request, order_no=None):
        """软删一笔订单（spec §5.1 / D5 / D6-④）。⚠️ 方法名恰好叫 delete 有三道闸（accounts 线实测踩过，别删任何一道）：①类级 http_method_names 不含 delete；
        ②第一条语句是方法守卫——@action(methods=["post"]) 只决定注册哪个映射（ViewSetMixin.as_view 把处理器挂到 self.post），而 dispatch 按 getattr(self, request.method.lower()) 找处理器 ⇒ 名叫 delete 的方法同时会成为 HTTP DELETE 的处理器；
        ③用例 tests/test_order_recycle.py::SoftDeleteLandingTests::test_delete_is_post_only。⚠️ 取对象必须走 Order.all_objects：默认 manager 排除已删 ⇒ 分不出「已删（幂等成功）」与「不存在（1002）」。"""
        if request.method != "POST":
            return self.http_method_not_allowed(request)
        params = DeleteOrderSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        is_del_emp = params.validated_data["isDelEmp"]
        order = self._order_including_deleted(order_no)      # 不存在 ⇒ 1002（在事务之前）
        if order is None:
            return api_response(code=ErrorCode.BUSINESS, msg="记录不存在或已被删除。")
        # ⚠️ `validate_actor=False`：`/delete/` **不读、也不校验**请求体的 `actor`
        # （`soft_delete_order` 的操作人就是认证态的 `User` 实例）⇒ `ctx.actor` 与
        # `ctx.log_actor` **同源**。⛔ 不要为此给 `_write_context` 再加参数。
        ctx = self._write_context(request, validate_actor=False)
        # ⭐ A5（spec §D5）：动作体搬进领域核心 `order_writes.delete_order_write`
        # （单笔与批量共用同一份判据）；本端点只做「取对象（含已删）→ 调核心 → 包信封」。
        # 响应形状逐字不变（`{order_no, deleted_at, isDelEmp}`）。
        previous = order.deleted_at
        outcome = delete_order_write(
            order, is_del_emp=is_del_emp, ctx=ctx, dry_run=False
        )
        if not outcome.ok:
            # 领域层**今天没有**失败分支（唯一的判据「取不到对象」已在上面判掉）
            # ——这一支是防御性的：真出现失败就按业务码 + msg 原样回，不静默当成功。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        order.refresh_from_db()
        return api_response(
            {
                "order_no": order.order_no,
                # 幂等分支下 deleted_at 保持第一次的值（previous 与它相同）。判据写 is not None 而不是 or（E-0021：与本仓其余边界判据口径一致）。
                # ⚠️ D-0302：走 local_iso（项目时区 +08:00，与同文件审计行同一条出口）——旧写法裸 .isoformat() 出 UTC 串，管理端删除弹窗截串直显差 8 小时。
                "deleted_at": local_iso(
                    previous if previous is not None else order.deleted_at
                ),
                "isDelEmp": is_del_emp,
            }
        )

    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, order_no=None):
        """把已软删的订单恢复（spec §5.3）。restore 不是 HTTP 动词 ⇒ 不需要 D5 的方法守卫；「非 POST ⇒ 405」由类级 http_method_names（无 put/delete）
        与 router 的方法映射共同保证，用例见 test_restore_is_post_only。"""
        order = self._order_including_deleted(order_no)
        if order is None:
            return api_response(code=ErrorCode.BUSINESS, msg="记录不存在或已被删除。")
        restore_order(order=order, actor=request.user)
        return api_response({"order_no": order.order_no})

    @action(detail=False, methods=["get"], url_path="recycle-bin")
    def recycle_bin(self, request):
        """回收站列表（spec §5.2）。⚠️ url_path="recycle-bin" 是列表级动作 ⇒ 生成 api/business/orders/recycle-bin/，不匹配详情模式（lookup_value_regex）⇒ 路由不冲突（DRF DefaultRouter 也把列表级路由排在详情级之前）；⛔ 将来若命名成 20 位数字会撞详情路由，所以留这句话。⭐ 订单日志人名线（spec §3-B2）：「删除人」除账号快照 deleted_by 外还回账号名称 deleted_by_name——映射在 paginate_queryset 之后对本页去重后一次查库（apps/accounts/display.py::display_names_for_usernames），⛔ 不在序列化器逐行查（N+1）；查不到的账号由序列化器回落 deleted_by。"""
        data = self._search_params(RecycleBinSearchSerializer)
        qs = search_recycle_bin(
            game_id=data.get("game"),
            status=data.get("status"),
            deleted_from=data.get("deleted_from"),
            deleted_to=data.get("deleted_to"),
            order_source=data.get("order_source"),
            keyword=data.get("keyword"),
            game_account=data.get("game_account"),
        )
        # ⚠️ 数据权限的**同一个卡点**（与订单列表同源）：列表 / 详情两条路径共用一处规则。
        qs = scope_queryset(request.user, qs, scope_filter=game_scope("game_id"))
        page = self.paginate_queryset(qs)
        # ⭐ 删除人显示名：本页去重后一次查询（spec §3-B2/判据 7、8）。⚠️ 位置必须在 paginate_queryset 之后（只需本页那几十个账号，对全集查是白付出）；
        # ⛔ 不许挪进序列化器（逐行查库=N+1）。⚠️ 用 page or []：分页被关掉时 paginate_queryset 回 None（形状与空页一致）。
        deleted_by_names = display_names_for_usernames(
            row.deleted_by for row in (page or [])
        )
        # ⚠️ **回收站专用**列表序列化器（spec §5.2 / §7.3 第 10、11 列）：它比订单列表多
        # `deleted_at` / `deleted_by` / `deleted_by_name` 三个键。⛔ 别换回 `OrderListSerializer`
        # ——那样页面「删除时间 / 删除人」两列只能**静默**显示空，而这页的每一行必然有删除时间。
        serializer = RecycleBinListSerializer(
            page,
            many=True,
            context={"request": request, "deleted_by_names": deleted_by_names},
        )
        return self.get_paginated_response(serializer.data)

    @action(detail=False, methods=["get"], url_path="dictionary-options")
    def dictionary_options(self, request):
        def build():
            # Preserve existing dropdown ordering and its 200-row limit per dictionary.
            games = Game.objects.filter(deleted_at__isnull=True)
            return {
                "games": list(games.values("id", "name")[:200]),
                "servers": list(GameServer.objects.values("id", "name", "game")[:200]),
                "order_sources": list(OrderSource.objects.values("id", "name")[:200]),
            }

        return api_response(catalog_options(build))

    @action(detail=False, methods=["get"], url_path="filter-options")
    def filter_options(self, request):
        """候选名单（spec §5.6 / D13）：给「代练打手/发单人」一格一份与筛选同源的名单。⚠️ 节点是本页自己的 order.list——⛔ 不复用 GET /api/accounts/（它判 system_user.list，默认「客服」不持 ⇒ 「盒子在、点开没人可选」）。⚠️ 数据范围=本租户、未软删订单里实际出现过的人，再过同一道数据权限卡点（与列表筛选同源；⚠️ 例外是 order.restrict_to_self_created——它只由 filter_queryset 施加在 list 上，本动作不过它 ⇒ 持该旗标的账号可能「候选里有、筛不到」）。⚠️ 纯读：不发任何写请求、不写审计行（那三张表只管写端点）；列表级 url_path 与详情路由不冲突（同 recycle-bin）。"""
        data = self._search_params(FilterOptionsSerializer)
        limit = data.get("limit") or 200
        game_id = data.get("game")
        # 空串（含只送空白）视同未传；`or None` 把「没传」与「送了空白」并成同一档。
        search = data.get("search") or None
        # ⚠️ **两道卡点都在 `*_options()` 里面**（`scope_queryset` + `game_scope`）——
        #    视图这里**不**再做任何收窄、也**不**逐人查（那是 N+1）。
        # ⚠️ `viewer` 是必填关键字参数（漏传 ⇒ 响亮 `TypeError`，不会静默回全租户名单）。
        takers, takers_truncated = taker_options(
            viewer=request.user, game_id=game_id, limit=limit, search=search
        )
        publishers, publishers_truncated = publisher_options(
            viewer=request.user, game_id=game_id, limit=limit, search=search
        )
        return api_response(
            {
                "takers": takers,
                "publishers": publishers,
                "takers_truncated": takers_truncated,
                "publishers_truncated": publishers_truncated,
            }
        )

    @action(detail=False, methods=["get"], url_path="kefu-options")
    def kefu_options(self, request):
        """「销售客服」候选名单（业务方原话「管理订单页面的销售客服下拉都是空的」）。为什么需要它（spec §1-A）：本页那一格在证据件里是空壳、对标由运行期 JS 灌候选 ⇒ 下拉恒空，本端点就是那一格的候选源。
        ⚠️ 节点是本页自己的 order.list——⛔ 不复用 GET /api/accounts/（判 system_user.list，默认「客服」不持 ⇒ 「盒子在、点开没人」）、⛔ 也不复用 assignee-options（它的门是指派动作的并集，客服不持 ⇒ 403），理由与同页 filter_options 逐条相同。
        ⚠️ 候选口径与 filter-options 不是同一个语义（业务方裁定、冻结）：本端点=「客服相关人员（内置客服+客服主管）∪ 当前登录人」，filter-options=「未软删订单里出现过的人」⇒ 载荷用不同的键 items，⛔ 不要把两种语义并成一个名字（§5-C）；口径与理由见 selectors.kefu_options()。⚠️ 纯读；列表级 url_path 与详情路由不冲突。"""
        data = self._search_params(KefuOptionsSerializer)
        items, truncated = kefu_option_rows(
            viewer=request.user, limit=data.get("limit")
        )
        return api_response({"items": items, "truncated": truncated})

    @action(detail=False, methods=["get"], url_path="assignee-options")
    def assignee_options(self, request):
        """指派弹层的候选名单（D-0146 方案②）。为什么需要它：弹层原走 GET /api/accounts/，那个端点的 list 动作判 system_user.list ⇒ 持 order.assign_taker/clear_taker 但没有该节点的角色（如「客服」）拉名单回 403、弹层里点不到人；本端点把节点门换成弹层自己那两个动作的并集（ORDER_PERMISSIONS["assignee_options"]）、不碰 system_user。
        ⚠️ 数据范围与 GET /api/accounts/ 完全一致（取数直接复用账号列表那一层：selectors.assignee_options → apps.accounts.selectors.list_accounts）——换端点只换「谁有资格读」、不放宽「谁能被指派」；列表级 url_path 与详情路由不冲突；纯读：不发写请求、不写审计行。
        ⭐ data 键演进（全是加法，items 行形状与筛选口径一个字没动）：D-0147 加 roles（本租户全部角色名，取值同源——委托 apps.rbac.selectors.role_names，与 GET /api/accounts/roles/ 是同一个函数 ⛔ 不是两份实现；门不变）；D-0179（录单页快速指派）加 defAssRecManUid/defAssRecMan（形状照对标 GetAllUserByGameName 同名键）=当前操作人自己的默认接手人（业务方原话「他勾了并提交，下次他录单自动带出那个人」；⛔ 读的是认证态用户自己的 StaffProfile.default_assignee，没有任何参数能指定读谁的；没设 ⇒ 0/""；⛔ 没把提成参数加进行里——本端点读面比 /api/accounts/ 宽，塞提成会变成绕过 system_user.restrict_view_commission_params 的旁路）；再加 defAssIsHall（业务方裁定 R1/R2，feat/default-assign-hall）：True ⇒ 有大厅发单权限且默认档位为「内部抢单」（此时 defAssRecManUid 为 0/""，两列互斥由 order_writes.set_default_assign_preference 保证；有意不抄对标的魔法值 999999——对标把「内部抢单」塞进 defAssRecManUid 本身，我们用语义化布尔键说同一件事、口径逐字不变；顶层键 5→6）。"""
        data = self._search_params(AssigneeOptionsSerializer)
        items, truncated, roles = assignee_option_rows(
            role=data.get("role"),
            business_type=data.get("business_type"),
            key=data.get("key"),
            limit=data.get("limit"),
        )
        return api_response(
            {
                "items": items,
                "truncated": truncated,
                "roles": roles,
                **default_assignee_payload(request.user),
            }
        )

    @action(
        detail=False, methods=["get", "put"], url_path="assignee-favorites",
        http_method_names=["get", "put", "head", "options"],
    )
    def assignee_favorites(self, request):
        if request.method == "PUT":
            form = FavoriteAssigneeSerializer(data=request.data)
            form.is_valid(raise_exception=True)
            user_id = form.validated_data["user_id"]
            if form.validated_data["favorite"]:
                if not available_favorite_assignees().filter(pk=user_id).exists():
                    return api_response(msg="该打手已不可用", code=ErrorCode.VALIDATION)
                FavoriteAssignee.objects.get_or_create(operator=request.user, assignee_id=user_id)
            else:
                FavoriteAssignee.objects.filter(operator=request.user, assignee_id=user_id).delete()
        return api_response(favorite_assignees(request.user))

    def _resolve_assign_taker(self, request, *, user_id, price=None, fee=None):
        """`/assign/` 与「建单+指派」共用的请求级校验（E-0021：一处实现）。返回 (taker, error_response)：error_response 非 None 时调用方原样返回它（那是业务码 1000 的「载荷指向了不可用的目标」，不是授权失败）。
        ⚠️ 三条判据的顺序与搬迁前逐字相同：①目标不存在 ②目标已停用（⭐ 这两条已搬进领域核心 apps/business/order_intake.py::resolve_assign_taker——录单核心也要判同一件事、而它拿不到 request；⛔ 本方法不再自己写那两句文案）③带 price/fee 时的两道 order.submit_payout_price 闸（HTTP 403，PermissionDenied；判据一律 is not None——0 是合法金额，E-0021）。
        ⛔ 抽出来不是为了「少写几行」：两处各写一份必然漂移，后果是「某个入口悄悄绕过了改钱的闸门」。行为逐字不变：返回的信封、文案、业务码与顺序都与搬迁前一致。"""
        taker, error = resolve_assign_taker(user_id)
        if error is not None:
            return None, error
        if price is not None and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改接单价的权限")
        if fee is not None and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改绩效金额的权限")
        return taker, None

    def peer_direct_assignment_options(self, request, order_no=None):
        from apps.business import peer_direct_taker
        from apps.business.api.peer_direct_taker_views import DirectTakerDraftGameSerializer

        order = self.get_object()
        can_view_price = can_view_offer_price(request.user)
        can_assign = has_node_now(request.user, "order.assign_taker") and can_view_price
        studio_game = order.game
        if can_assign and "game_id" in request.query_params:
            serializer = DirectTakerDraftGameSerializer(data=request.query_params)
            serializer.is_valid(raise_exception=True)
            studio_game = self._peer_direct_draft_game(request, serializer.validated_data["game_id"])
        return api_response(data=peer_direct_taker.assignment_options(
            order, studio_game=studio_game, can_assign=can_assign, can_view_price=can_view_price,
            can_view_assignee=can_view_taker(request.user),
        ))

    def jidanpu_direct_assignment_options(self, request, order_no=None):
        from apps.business import jidanpu_direct_queue, jidanpu_first_assignment

        form = JidanpuFirstOptionsSerializer(data=request.query_params)
        form.is_valid(raise_exception=True)
        can_view_price = can_view_offer_price(request.user)

        def options(order):
            if can_view_price:
                operation_id = form.validated_data.get("operation_id")
                queued = jidanpu_direct_queue.options(order, operation_id=operation_id)
                if operation_id is not None or queued is not None:
                    return queued
                result = jidanpu_first_assignment.options(order, studio_id=form.validated_data.get("studio_id"))
                if result is not None:
                    return result
            return jidanpu_direct_taker.options(
                order, can_view_price=can_view_price, refresh=form.validated_data["refresh"],
            )

        return self._handle_jidanpu_direct_assignment(request, options)

    def jidanpu_direct_assignment_search(self, request, order_no=None):
        form = JidanpuTakerSearchSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        if not can_view_offer_price(request.user):
            raise PermissionDenied("需要查看发单价的权限才能搜索鸡蛋铺下家打手")
        from apps.business import jidanpu_first_assignment

        handler = jidanpu_first_assignment.search if "studio_id" in form.validated_data else jidanpu_direct_taker.search
        return self._handle_jidanpu_direct_assignment(request, handler, **form.validated_data)

    def jidanpu_direct_assignment(self, request, order_no=None):
        form = JidanpuTakerAssignmentSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        if not can_view_taker(request.user):
            raise PermissionDenied("需要查看接手人的权限才能指派鸡蛋铺下家打手")
        if not can_view_offer_price(request.user):
            raise PermissionDenied("需要查看发单价的权限才能指派鸡蛋铺下家打手")
        if "price" in form.validated_data and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改发单价的权限")
        if "fee" in form.validated_data and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改绩效金额的权限")
        from apps.business import jidanpu_direct_queue, jidanpu_first_assignment

        order = self.get_object()
        price = form.validated_data.get("price")
        proposed_price = price if price is not None else order.hall_price
        if changes_approved_penalty_price(order, proposed_price):
            return api_response(msg=PENALTY_AMOUNT_LOCKED, code=ErrorCode.BUSINESS)
        try:
            if "operation_id" in form.validated_data:
                result = jidanpu_direct_queue.enqueue(order, operator=request.user, **form.validated_data)
            elif "studio_id" in form.validated_data:
                result = jidanpu_first_assignment.assign(order, operator=request.user, **form.validated_data)
            else:
                result = jidanpu_direct_taker.assign_jidanpu_taker(order, operator=request.user, **form.validated_data)
        except jidanpu_direct_queue.OperationConflict as exc:
            return api_response(msg=str(exc), code=ErrorCode.VALIDATION, data={"operation_conflict": True})
        except jidanpu_direct_taker.AssignmentError as exc:
            return api_response(msg=str(exc), code=ErrorCode.VALIDATION)
        return api_response(data=result)

    def jidanpu_direct_assignment_verify(self, request, order_no=None):
        if not can_view_taker(request.user) or not can_view_offer_price(request.user):
            raise PermissionDenied("需要查看接手人与发单价的权限才能核对指派")
        order = self.get_object()
        form = JidanpuTakerVerifySerializer(data=request.data)
        form.is_valid(raise_exception=True)
        from apps.business import jidanpu_direct_queue, jidanpu_first_assignment

        try:
            queued = jidanpu_direct_queue.options(order)
            if queued is not None and queued["operation_status"] == "working":
                if queued["pending"] != form.validated_data["pending_ref"]:
                    raise jidanpu_direct_taker.AssignmentError(jidanpu_direct_taker.CHANGED)
                result = queued
            elif jidanpu_first_assignment.is_pending(order):
                result = jidanpu_first_assignment.verify(order, operator=request.user, **form.validated_data)
            else:
                result = jidanpu_direct_taker.verify_jidanpu_assignment(order, operator=request.user, **form.validated_data)
        except jidanpu_direct_taker.AssignmentError as exc:
            return api_response(msg=str(exc), code=ErrorCode.VALIDATION)
        return api_response(data=result)

    def _handle_jidanpu_direct_assignment(self, request, handler, **data):
        if not can_view_taker(request.user):
            raise PermissionDenied("需要查看接手人的权限才能指派鸡蛋铺下家打手")
        order = self.get_object()
        try:
            result = handler(order, **data)
        except jidanpu_direct_taker.AssignmentError as exc:
            return api_response(msg=str(exc), code=ErrorCode.VALIDATION)
        return api_response(data=result)

    def peer_direct_assignment_search(self, request, order_no=None):
        from apps.business import peer_direct_taker
        from apps.business.api.peer_direct_taker_views import DirectTakerSearchSerializer

        order = self.get_object()
        serializer = DirectTakerSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if "query" in values and not can_view_offer_price(request.user):
            raise PermissionDenied("需要查看发单价的权限才能搜索下家打手")
        try:
            result = peer_direct_taker.search_taker(order, **values)
        except peer_direct_taker.DirectTakerError as exc:
            return api_response(msg=str(exc), code=exc.code)
        return api_response(data=result)

    def _peer_direct_draft_game(self, request, game_id):
        can_edit = has_node_now(request.user, "order.add_edit") or has_node_now(request.user, "order.submit_edit")
        if not can_edit or not can_view_offer_price(request.user):
            raise PermissionDenied("需要录入或编辑订单并能查看发单价，才能搜索下家打手")
        return get_object_or_404(
            games_of(request.user).filter(is_active=True, deleted_at__isnull=True), pk=game_id,
        )

    def peer_direct_assignment_draft_options(self, request):
        from apps.business import peer_direct_taker
        from apps.business.api.peer_direct_taker_views import DirectTakerDraftGameSerializer

        serializer = DirectTakerDraftGameSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        game = self._peer_direct_draft_game(request, serializer.validated_data["game_id"])
        return api_response(data={"studios": peer_direct_taker.available_studios(game)})

    def peer_direct_assignment_draft_search(self, request):
        from apps.business import peer_direct_taker
        from apps.business.api.peer_direct_taker_views import DirectTakerDraftSearchSerializer

        serializer = DirectTakerDraftSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        game = self._peer_direct_draft_game(request, values["game_id"])
        studio_id = values["studio_id"]
        try:
            result = peer_direct_taker.search_taker_for_game(
                game, studio_id=studio_id, account_name=values.get("account_name"), query=values.get("query"),
            )
        except peer_direct_taker.DirectTakerError as exc:
            return api_response(msg=str(exc), code=exc.code)
        return api_response(data=result)

    def peer_direct_assignment(self, request, order_no=None):
        from apps.business import peer_direct_taker
        from apps.business.api.peer_direct_taker_views import DirectTakerRequestSerializer

        if not can_view_offer_price(request.user):
            raise PermissionDenied("需要查看发单价的权限才能直接指派给下家打手")
        order = self.get_object()
        serializer = DirectTakerRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if "price" in serializer.validated_data and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改发单价的权限")
        try:
            result = peer_direct_taker.assign_to_studio(
                order, user=request.user, **serializer.validated_data,
            )
        except peer_direct_taker.DirectTakerError as exc:
            return api_response(msg=str(exc), code=exc.code)
        return api_response(data=result)

    def _platform_taken_error(self, order):
        """⭐ 业务方新口径：远端已被代练通接单 ⇒ 本单不许再指派。返回 (is_blocked, msg)；判据本体是唯一出处 dailiantong_push.platform_taken_denial（「已接手」的定义 ⛔ 不在这里再写一份），本方法只负责把它翻成视图层的业务信封（1002）。⚠️ 只拦「已被接手」这一档（远端 12/13/14/15 与撤销在途——都意味着平台侧已有打手）：只抛到平台、还没人接（Status=11 / 18 已下架 / 还没回读）时本地照旧可以指派，指派成功后由 withdraw_for_internal_claim 把平台单删掉（即时退款）；⛔ 不走「先指派、再自动协商撤销」——那会把客服的操作变成「本地指派成功、平台打手继续干」的双打手状态。"""
        from apps.business.dailiantong_push import platform_taken_denial

        denial = platform_taken_denial(order)
        return denial is not None, denial

    def _order_including_deleted(self, order_no):
        """按单号取单，包含已软删（软删/恢复自己的取对象口）。⚠️ 这是 all_objects 的读路径（spec §4.3-C4）：默认 manager 取不到已删单。
        判定口径与订单详情一致：不在自己的游戏可见范围内 ⇒ 与「不存在」同一个出口（1002），这正是本视图既有 get_queryset() 的口径。"""
        qs = Order.all_objects.select_related("game")
        # ⭐ 隐藏单**只放行 retrieve** ⇒ 软删 / 恢复这两个口也不能碰它
        #    （与 get_object 的其余动作同一个失败出口：按「不存在」处理）。
        qs = qs.exclude(hall_card__isnull=False).exclude(
            upstream_hall_card__isnull=False
        )
        qs = scope_queryset(self.request.user, qs, scope_filter=game_scope("game_id"))
        return qs.filter(order_no=order_no).first()

    def hall(self, request):
        """内部抢单大厅：只列「已抛单、还没人接」的订单（spec §5）。⚠️ 路径按 spec §5 端点表是 /api/business/hall——不是计划正文写的 orders/hall/（两处冲突，按「以 spec 为准」取值）；实现形状仍按计划意图：OrderViewSet 上的读端点、复用 OrderListPagination 与 search_hall，没有另建 ViewSet。⚠️ 能不能看大厅只由节点回答（spec §4.3/§5；Ruling 35）：类级节点门=ORDER_PERMISSIONS["hall"]=grab_hall.take_order（对标 50），已认证无该节点 ⇒ HTTP 403（传输层，不是 200+业务码）；放行认「系统管理员」职位（is_tenant_admin：nodes_of 对该职位短路为全目录；只置 is_superuser 不短路）；旧的状态机角色词表（roles_of/ALL_ROLES/GROUP_TO_ROLES）已删、不要再引用。支持筛选的参数只有六个、与 HallSearchSerializer 声明字段同一集合：game/server/project_text/keyword/price_min/price_max——其余参数静默忽略（不报错也不会真的筛）；响应体 HallListSerializer 比订单列表多一个接单价 hall_price（Ruling 34）。
        ⭐ 本端点是四路合并读口：我方原生单（search_hall，origin=native）∪ 上家抢单大厅镜像单（jidanpu_upstream.upstream_hall_queryset，origin=upstream）∪ 模拟单(active)（D-0296）∪ 团团通上家镜像单（peer_hall，origin=peer，D-0303）；灰区（仅 include_exited）=离厅快照∪模拟单(retired)，按离厅时刻倒序排在可接区之后；各路共用同一组筛选与同一道游戏权限卡点，按归并顺序分页后读取详情（total=各路合计；代价登记 spec §14-12 残差）；行形状两路恰好同键（HallListSerializer.Meta.fields ↔ REMOTE_ROW_BASE，tests/test_jidanpu_hall_merge.py 钉住）；镜像行不带游戏名字符串键（前端拿游戏字典解，spec §11-29）；入参先过 HallSearchSerializer（E-0010，否则畸形值 HTML 500）。⭐ 带 1 秒进程内短缓存（apps/core/short_cache.py）：键=端点+请求参数（空串不计）+_hall_viewer_fingerprint 四个按人维度；TTL=settings.HALL_LIST_CACHE_TTL_S（.env 可改，0=紧急关闭）；代价是新鲜度≤TTL：刚被接走/刚抛的单最迟 1 秒后可见——与「先到先得+1012 已被接走」口径相容（取舍全文见 short_cache 模块 docstring）。"""
        # ⚠️ 函数体里没有职位门（单轴化）：原先的 Role.TAKER 职位闸随状态机换轴删除（spec 2026-09-23-rbac-single-axis-design.md §6）——
        # 「能不能看大厅」只由类级节点门 ORDER_PERMISSIONS["hall"]（=grab_hall.take_order，对标 50）回答，check_permissions() 先于本函数体执行。
        # 自定义职位只要勾了这个节点就能看大厅，与内置「代练员」同一套判据（角色是数据、节点是权限）。
        def build():
            if not connection.get_autocommit():
                return build_page()
            with transaction.atomic(), connection.cursor() as cursor:
                # Preserve the same eligible rows across counting, paging and loading details.
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                return build_page()

        def build_page():
            data = self._search_params(HallSearchSerializer)
            params = {
                "game_id": data.get("game"),
                "server_id": data.get("server"),
                "project_text": data.get("project_text"),
                "keyword": data.get("keyword"),
            }
            price_params = {"price_min": data.get("price_min"), "price_max": data.get("price_max")}
            preferred_only = data.get("preferred_only")
            preferred_on = hall_preferred.preferred_enabled()
            available = (
                self._hall_available_rows(request, params, price_params, preferred_only)
                if not preferred_only or preferred_on else []
            )
            exited = self._hall_exited_stream(request, game_id=params["game_id"]) if data.get("include_exited") else []
            selected = self.paginate_queryset(_merge_hall_candidates(available, exited))
            page = _read_hall_page(selected)
            if len(page) < len(selected):
                # An enclosing READ COMMITTED transaction can use the original one-read-per-source path.
                page = _read_hall_page(self.paginate_queryset(_merge_hall_candidates(available, exited, eager=True)))
            if not preferred_on:
                for row in page:
                    row["is_preferred"] = False
            return self.get_paginated_response(page).data

        # ⭐ 短缓存：键=端点+请求参数（空串不计）+按人指纹，构成与取舍见 apps/core/short_cache.py。命中时 build（连着权限卡点与脱敏）不执行
        # ⇒ 按人维度必须在键里，不能只靠 build 里的检查。⚠️ 本端点分页在 build 里面（self.paginate_queryset）⇒ page/page_size 必须留在键里，
        # ⛔ 别学 taker 侧把它们剔掉（hall_views.TAKER_CACHE_SKIP_PARAMS——那边分页在缓存之后，才剔得动）。
        payload = short_cached_payload(
            (
                "hall/web/v1",
                _sorted_nonempty_params(request),
                _hall_viewer_fingerprint(request),
            ),
            ttl=hall_cache_ttl(),
            build=build,
        )
        # ⚠️ payload 已是 `api_response()` 造好的信封（build 里 `get_paginated_response`
        #    的 `.data`）；这里带 `_enveloped` 标记，防 `EnvelopeResponseMixin.finalize_response`
        #    把它再包一层（二次包装的形状 = data.data，客户端读 code 读不到）。
        response = Response(payload)
        response._enveloped = True
        return response

    def _hall_available_rows(self, request, params, price_params, preferred_only):
        native_qs = scope_queryset(
            request.user, search_hall(**params, **price_params), scope_filter=game_scope("game_id"),
        )
        if preferred_only:
            native_qs = native_qs.filter(
                **{f"{EFFECTIVE_TAKER_PRICE}__gt": hall_preferred.PREFERRED_PRICE_THRESHOLD},
            )
        serializer = HallListSerializer(context={"request": request})
        native = _HallSource(native_qs, serializer.to_representation, "created_at")
        upstream_qs = scope_queryset(
            request.user, jidanpu_upstream.upstream_hall_queryset(**params),
            scope_filter=game_scope("game_id"),
        )
        upstream = _hall_mirror_source(
            upstream_qs, render=jidanpu_upstream.serialize_upstream_row,
            price_in_range=jidanpu_upstream.upstream_hall_price_in_range,
            display_price=jidanpu_upstream.upstream_mirror_hall_price,
            binding="owner_binding", remote_price="remote_rec_fee",
            price_params=price_params, preferred_only=preferred_only,
        )
        peer_qs = scope_queryset(
            request.user, peer_hall.peer_hall_queryset(**params), scope_filter=game_scope("game_id"),
        )
        peer = _hall_mirror_source(
            peer_qs, render=peer_hall.serialize_peer_row, price_in_range=peer_hall.peer_hall_price_in_range,
            display_price=peer_hall.display_price, binding="peer_upstream_binding",
            remote_price="remote_hall_price", price_params=price_params, preferred_only=preferred_only,
        )
        simulated = self._hall_simulated_stream(
            request, **params, price_params=price_params, show=viewer_sees_simulated(request),
        )
        return [native, upstream, simulated, peer]

    def _hall_simulated_stream(self, request, *, game_id, server_id, project_text,
                               keyword, price_params, show=True):
        """按原有游戏权限和个人免押规则收窄模拟卡。"""
        from apps.business.models import HallSimulatedOrder

        qs = HallSimulatedOrder.objects.filter(
            status=HallSimulatedOrder.Status.ACTIVE
        ).order_by("-generated_at", "-id")
        if not show:
            qs = qs.none()
        if game_id is not None:
            qs = qs.filter(game_id=game_id)
        if server_id is not None:
            qs = qs.filter(server_id=server_id)
        if project_text:
            qs = qs.filter(project_text__icontains=project_text)
        if keyword:
            qs = qs.filter(display_order_no__icontains=keyword)
        price_min = price_params.get("price_min")
        price_max = price_params.get("price_max")
        if price_min is not None:
            qs = qs.filter(price__gte=price_min)
        if price_max is not None:
            qs = qs.filter(price__lte=price_max)
        qs = scope_queryset(request.user, qs, scope_filter=game_scope("game_id"))
        return _HallSource(qs, partial(hall_preferred.simulated_row, exited=False), "generated_at")

    def _hall_exited_stream(self, request, *, game_id):
        """灰区只按游戏和可见权限筛选，保留离厅时刻和价格脱敏。"""
        from apps.business.models import HallExitSnapshot, HallSimulatedOrder

        snap_qs = HallExitSnapshot.objects.select_related("game").order_by("-exited_at", "-id")
        if game_id is not None:
            snap_qs = snap_qs.filter(game_id=game_id)
        snap_qs = scope_queryset(request.user, snap_qs, scope_filter=game_scope("game_id"))
        render_snapshot = partial(hall_preferred.snapshot_row, can_view_price=can_view_offer_price(request.user))
        snapshots = _HallSource(snap_qs, render_snapshot, "exited_at")

        sim_qs = HallSimulatedOrder.objects.filter(status=HallSimulatedOrder.Status.RETIRED)
        if not viewer_sees_simulated(request):
            sim_qs = sim_qs.none()
        if game_id is not None:
            sim_qs = sim_qs.filter(game_id=game_id)
        sim_qs = scope_queryset(request.user, sim_qs, scope_filter=game_scope("game_id"))
        sim_qs = sim_qs.annotate(
            _hall_exit_at=Coalesce("retired_at", "generated_at"),
        ).order_by("-retired_at", "-id")
        render_simulated = partial(hall_preferred.simulated_row, exited=True)
        return [snapshots, _HallSource(sim_qs, render_simulated, "_hall_exit_at")]

    def _actor(self, request):
        """从请求体取操作人——**必须过校验**，不能直接进 varchar(64)。"""
        serializer = ActorSerializer(data={"actor": request.data.get("actor") or ""})
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data["actor"]

    def _write_context(self, request, *, validate_actor=True):
        """把请求里与写动作有关的东西装成领域层的 WriteContext。⚠️ `user` 是**认证态的 `User` 实例**（不是用户名 / 主键）：`transition_order` 把它当 `apply_transition` 的 `hall_publisher`（「发单人 = 把订单抛到大厅的人」）、`assign_order` 在直写点读它（D-0369：指派 / 改派也写发单人 = 操作人），其余动作不读。
        ⚠️ actor 走 `self._actor(request)`（业务列 `order.updated_by`，P2 契约取请求体值）；`log_actor`/`log_actor_id` 取认证态（R20：留痕的操作人不可伪造）——两者刻意不同源。⚠️ `validate_actor=False` 只给「不读、也不校验请求体 actor」的端点用（该分支下 ctx.actor 与 ctx.log_actor 同取认证态；⛔ 不枚举动作名——枚举会随新增动作腐烂，E-0011）；默认 True：其余动作照旧走 `ActorSerializer`，超长/非法的请求体 actor 仍是 1000。"""
        actor = (
            self._actor(request)
            if validate_actor
            else (request.user.get_username() or "")
        )
        return WriteContext(
            actor=actor,
            log_actor=request.user.get_username(),
            log_actor_id=request.user.pk,
            nodes=nodes_of(request.user),
            user=request.user,
        )

    def create(self, request, *args, **kwargs):
        """建单（POST /api/business/orders/），可选地在同一次请求里完成指派（D-0179）。⭐ 本方法自 T6 只是一层壳：领域核心搬进了 apps/business/order_intake.py::create_order（唯一实现），开放平台的对外录单 POST /api/open/v1/orders/ 调的是同一个函数（⛔ 不许在 apps/openapi/** 里抄第二份建单逻辑）；⇒ 全部语义的权威说明在 create_order 的 docstring 里（三道前置闸/建单事务/抛单支/指派支/remember_assignee 四态/1022·1023 两条失败形状），⛔ 本处不复制一份（两处重复的说明必然漂移）。
        本方法只做两件事：①把请求形状过那两个序列化器（OrderSerializer + CreateOrderAssignSerializer——那两个键都不是订单列 ⛔ 塞不进 OrderSerializer，它 exclude=[]、多出来的键会被喂给 Order.objects.create）②把「谁在录」装配成 IntakeActor（认证态用户/留痕两列/状态机节点集/游戏准入/sales_agent 兜底/指派用的 WriteContext）。
        ⚠️ 行为逐字不变（搬迁只把「读 request 的那几处」换成读 IntakeActor 字段；payload_for_order 就是搬迁前那一句 OrderSerializer(order, context=...).data）；四个入参求值比原来略早（都是纯读）⇒ 行为不变，只有「被拒的请求也多做一次节点查询」这一点代价（如实登记：不构成契约、也不落库）。"""
        serializer = OrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # 「建单 + 指派」那一段的入参：独立序列化器（那两个键都不是订单列，⛔ 塞不进
        # `OrderSerializer`——它 `exclude = []`，多出来的键会被喂给 `Order.objects.create`）。
        assign_input = CreateOrderAssignSerializer(data=request.data)
        assign_input.is_valid(raise_exception=True)
        outcome = create_order(
            serializer=serializer,
            assign_input=assign_input,
            actor=IntakeActor(
                user=request.user,
                # 留痕的 `actor` 列与 `Order.created_by` **同取认证态用户名**
                # （`User.get_username()` 就返回 `username`）——搬迁前是两句分开写的，值相同。
                log_actor=request.user.get_username(),
                log_actor_id=request.user.pk,
                nodes=nodes_of(request.user),
                # ⚠️ `validate_actor=False`（搬迁前那一句的实参）：指派那一步的 `actor` 与
                # `log_actor` **同取认证态**（不读请求体的 `actor`）。
                write_ctx=self._write_context(request, validate_actor=False),
                sales_agent_fallback=_clip_sales_agent(user_display_name(request.user)),
                # 写侧准入：只能录自己有权限的游戏（业务方 裁定）。
                assert_game=lambda game: assert_game_allowed(request.user, game),
                # ⭐ 本线（D-0223）：管理端录单**受**「禁止录入相同的来源单号」
                #    开关管（对外录单不传这一格 = 不受管；业务方裁定本开关只拦手工）。
                guard_duplicate_source_order_no=True,
            ),
            payload_for_order=lambda order: OrderSerializer(
                order, context={"request": request}
            ).data,
        )
        return outcome.response

    def retrieve(self, request, *args, **kwargs):
        order = self.get_object()
        record_order_read(
            action="order.view_credentials",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            order_no=order.order_no,
            source_order_no=order.source_order_no,
        )
        return api_response(
            data=OrderDetailSerializer(order, context={"request": request}).data
        )

    def partial_update(self, request, *args, **kwargs):
        """编辑订单。订单号、状态及非拆分订单改价不能经此修改；拆分组价格由核心按锚点守恒（见 OrderBalanceUpdateSerializer）。留痕是一个被改字段一行（spec §5.1 order.update/§5.9 R8）：field_name 存真实列名、模板里的中文由 FIELD_LABELS 派生（record() 的渲染器做，调用方不得直传显示名）；值没变的字段不写行——同值 PATCH 不该产生「改了 A→A」的噪音。
        ⚠️ R21：本方法此前没有事务边界（spec §7.4 那句「order_views 已有 atomic()」对它是假的）——一次编辑会写多行留痕，中途失败会留下「订单改了、留痕只写了一半」的脏态（E-0022 家族）⇒ 现在 save() 与全部 record(...) 一起进同一个 atomic()（要么全成要么全回滚）；块内没有可预期的业务失败分支（校验都在块外）所以不需要 set_rollback，万一将来加进来必须 set_rollback(True) 再 return（E-0022）。
        通用 PATCH 改不了销售客服这一列：带了 sales_agent 且值与现值不同——没节点回 403；有节点回业务码 1000 +「请使用换销售客服接口」；它是 Order.sales_user（提成归属）的派生快照，单独写会造出「列表显示乙、提成归甲」；换人只有 POST /orders/<单号>/sales-agent/（同一事务里改归属、搬绩效行、同步名字、重算）。值没变（整表单提交带上原值）不判。这是行为变更、不是放宽判据（SalesAgentChangeGateTests）。"""
        order = self.get_object()
        serializer = self.get_serializer(order, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        # ⭐ 换销售客服的节点门（见 docstring）。落点选在动作体里、update_order_fields 之前，与三处既有先例（_resolve_assign_taker 价格闸、/clear-taker 绩效闸、batch_assign）同形：
        # raise PermissionDenied ⇒ apps/core/exceptions.py 按既有翻译路径回 HTTP 403 + code 403（不是业务码）。⚠️ 放这里而不是放 order_writes.update_order_fields：那个核心是两个调用方的共用落库口
        #（本动作+批量 /batch/types），批量那条只传 order_type、与换销售客服无关，把闸塞进核心会带上与本动作无关的入参依赖。⚠️ 本仓无 PUT（http_method_names 不含 put）⇒ PATCH 是编辑订单唯一的 HTTP 写入路。
        raw_sales_agent = serializer.validated_data.get("sales_agent", _ABSENT)
        if raw_sales_agent is not _ABSENT:
            # ⚠️ 比较前与录单那一路用同一个裁剪 helper（_clip_sales_agent）：判据必须拿「这一行落库会变成什么」去比，否则造出反向假拒绝——库里存的是裁过的值（≤列宽），请求体在同一列宽外再补几个字，裁剪后落库一字没变、原始值比却判「变了」；存量行里的超宽值（varchar(64) 之前的历史数据）不做反向裁剪——那种行今天任何 PATCH 都会 value too long 500（与本次改动无关）。
            # ⚠️ is not _ABSENT 而不是真值：sales_agent:"" 是一次真实的清空，必须判（真值会把空串当成「没带」，在权限门上开静默口子）。
            if _clip_sales_agent(raw_sales_agent) != _clip_sales_agent(order.sales_agent):
                if not has_node_now(request.user, "order.allow_change_sales_agent"):
                    raise PermissionDenied("你没有改销售客服的权限")
                # ⭐ 持节点也不许走通用 PATCH：这一列是显示名快照、真值由 Order.sales_user（归属人，钱跟它走）派生 ⇒ 单独写它就是在制造「名字是甲、提成归乙」；改归属只有 POST /orders/<单号>/sales-agent/ 一条路（同一事务改归属+搬绩效行+同步名字+重算）。
                # ⚠️ 明确拒绝 ⛔ 不是静默忽略（AGENTS.md §5-E 静默丢字段一族）；用业务错误码不是 403——这个人的授权是够的、错的是路径（403 会显示成「没有权限执行该操作」）；
                # 放在动作体里的理由与上面那道节点门逐字相同（见本方法 docstring）。
                return api_response(
                    data=None,
                    msg=(
                        "接单销售客服不能在编辑订单里单独修改，请使用换销售客服接口"
                        "（POST /api/business/orders/<单号>/sales-agent/）"
                    ),
                    code=ErrorCode.VALIDATION,
                )
        # ⭐ 本线（D-0223，业务方裁定）：开关「录单时禁止录入相同的来源单号」开着时，手工编辑把 source_order_no 改成别人已用的号 ⇒ 拒（业务码 1000）。⚠️ 放视图层、update_order_fields 之前（与销售客服闸同一落点理由）：那个核心是三方共用的落库口（手工 PATCH/批量/开放平台改单），业务方裁定本开关只拦手工 ⇒ 塞进核心会给另外两条路带上与裁定相反的行为。
        # ⚠️ 判据用「validated_data 里带了 source_order_no」（is not None 不适用：这里是「这份载荷动了哪一格」；空串档由下面的「值非空」放过——空号永远不拦）；exclude(pk=order.pk) 保证「PATCH 回自己原值」不误拦。
        # ⚠️ 回收站不占号（deleted_at__isnull=True，与录单闸同一条口径）；与录单闸同一条竞态残差：exists() 查询、无唯一约束（D-0223 如实登记）。
        if "source_order_no" in serializer.validated_data and switch_enabled(
            "forbid_duplicate_source_order_no"
        ):
            new_source_no = serializer.validated_data.get("source_order_no")
            if new_source_no and Order.objects.filter(
                source_order_no=new_source_no, deleted_at__isnull=True
            ).exclude(pk=order.pk).exists():
                return api_response(
                    data=None,
                    msg=(
                        f"来源单号 {new_source_no} 已存在"
                        "（系统设置已开启禁止重复录入），不能重复录入"
                    ),
                    code=ErrorCode.VALIDATION,
                )
        try:
            outcome = update_order_fields(
                order,
                validated=serializer.validated_data,
                request_user=request.user,
                ctx=self._write_context(request),
                dry_run=False,
            )
        except ValidationProblem as problem:
            return api_response(data=None, msg=str(problem), code=problem.code)
        if not outcome.ok:
            if outcome.code == 403:
                raise PermissionDenied(outcome.msg)
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        assigned = outcome.detail["order"]
        # ⛔ 本端点（编辑订单）不碰代练通订单（审计 H1 修复）。这里曾挂「指派成功后立刻撤回代练通」——可它挂在编辑接口上，而编辑接口根本不能指派（OrderUpdateSerializer 没有 taker 字段）
        # ⇒ 客服改个备注/电话就会把一张真钱平台单删掉或发起协商撤销、事由还写着「内部打手已接单」（假话）。
        # 撤回只留在真正指派成功的地方：/assign/、大厅抢单、batch/assign（三处都先过「远端已接单⇒拒」闸，见 _platform_taken_error）。
        return api_response(data=OrderSerializer(assigned, context={"request": request}).data)

    @action(detail=False, methods=["post"])
    def parse(self, request):
        """把粘贴的订单文本解析成结构化字段。不建单——解析结果回给前端填进表单、允许客服人工修正（这是对标物的行为：粘贴→失焦解析→人工可改）；
        unmatched 必须一起返回：客服要知道哪几项没认出来，而不是静默留空。
        ⭐ 录单识别设置线：规则来源改由当前租户的配置驱动（build_rules(<覆盖值>)，见 apps/business/recognition.py）；引擎与请求/响应形状（fields/matched/unmatched）逐字未动（spec §5.3）。"""
        text = request.data.get("content", "")
        from apps.business.recognition import rules_for_tenant

        result = parse_order_text(text, rules=rules_for_tenant())
        fields = {
            key: (f"{value:.2f}" if key in MONEY_FIELDS and isinstance(value, Decimal) else value)
            for key, value in result.fields.items()
        }
        return api_response(
            data={
                "fields": fields,
                "matched": list(result.matched),
                "unmatched": list(result.unmatched),
            }
        )

    @action(detail=True, methods=["post"], url_path="split-peer")
    def split_peer(self, request, order_no=None):
        params = PeerSplitOrderSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        return self.split_priced_order(
            self.get_object(), values=params.validated_data,
            price_context=self._write_context(request, validate_actor=False),
            assert_game=lambda game: assert_game_allowed(request.user, game),
            allow_completed=True,
            may_edit_price=lambda: (
                has_node_now(request.user, "order.submit_payout_price")
                or has_node_now(request.user, "order.edit_payout_price")
            ),
        )

    @staticmethod
    def split_priced_order(visible_order, *, values, price_context, assert_game, may_edit_price,
                           check_orders=None, allow_completed=False):
        order_no = visible_order.order_no
        operator = price_context.log_actor
        actor_id = price_context.log_actor_id
        with transaction.atomic():
            group_locked_members = []
            if visible_order.split_id:
                group_locked_members = list(
                    Order.objects.select_for_update()
                    .filter(split_id=visible_order.split_id)
                    .order_by("id")
                )
                order = next(
                    (member for member in group_locked_members if member.order_no == order_no),
                    None,
                )
            else:
                order = Order.objects.select_for_update().filter(order_no=order_no).first()
                if order is not None and order.split_id:
                    group_locked_members = list(split_group.active_members(order.split_id).select_for_update())
            if order is None:
                return api_response(data=None, msg="订单已变化，请刷新后重试", code=ErrorCode.VALIDATION)
            if check_orders is not None:
                check_orders(group_locked_members or [order])
            if any(member.peer_direct_pending is not None for member in [order, *group_locked_members]):
                return api_response(msg=PEER_DIRECT_PENDING_MESSAGE, code=ErrorCode.BUSINESS)
            new_orders = values["new_orders"]
            completed = order.status == OrderStatus.COMPLETED
            splitting_completed = completed and allow_completed and bool(new_orders)
            if new_orders and completed and not allow_completed:
                transaction.set_rollback(True)
                return api_response(
                    data=None, msg="已完成的订单不能再拆分", code=ErrorCode.VALIDATION
                )
            if not order.split_id and not new_orders:
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg="未拆分订单至少需要新增一个拆出订单",
                    code=ErrorCode.VALIDATION,
                )
            if order.split_id and order.split_kind not in {"PEER", "LEGACY", ""}:
                transaction.set_rollback(True)
                return api_response(
                    data=None, msg="订单拆分类型不支持继续拆分", code=ErrorCode.VALIDATION
                )
            try:
                assert_game(order.game)
            except ValidationProblem as problem:
                transaction.set_rollback(True)
                return api_response(data=None, msg=str(problem), code=problem.code)

            old_price = Decimal(order.total_price)
            group_id = order.split_id or generate_order_no()
            group_kind = (
                order.split_kind
                if order.split_kind in {"PEER", "LEGACY"}
                else ("LEGACY" if order.split_id else "PEER")
            )
            existing_inputs = values["existing_orders"]
            existing_members = []
            if existing_inputs:
                if not order.split_id:
                    transaction.set_rollback(True)
                    return api_response(
                        data=None,
                        msg="未拆分订单不能修改已有组员内容",
                        code=ErrorCode.VALIDATION,
                    )
                if not group_locked_members:
                    group_locked_members = list(
                        Order.objects.select_for_update()
                        .filter(split_id=order.split_id)
                        .order_by("id")
                    )
                by_order_no = {member.order_no: member for member in group_locked_members}
                requested_order_nos = [item["order_no"] for item in existing_inputs]
                if order.order_no in requested_order_nos or any(
                    order_no not in by_order_no for order_no in requested_order_nos
                ):
                    transaction.set_rollback(True)
                    return api_response(
                        data=None,
                        msg="只能修改当前拆分组内的其他有效订单",
                        code=ErrorCode.VALIDATION,
                    )
                existing_members = [by_order_no[order_no] for order_no in requested_order_nos]
                try:
                    for member in existing_members:
                        assert_game(member.game)
                except ValidationProblem as problem:
                    transaction.set_rollback(True)
                    return api_response(data=None, msg=str(problem), code=problem.code)
            anchor = (
                group_locked_members[0]
                if group_locked_members
                else order
            )
            shipping_main_no = anchor.split_ship_main_order_no or anchor.order_no
            shipping_main = split_group.shipping_main_order(anchor)
            if splitting_completed:
                checked_games = {order.game_id}
                try:
                    for member in (*group_locked_members, shipping_main):
                        if member is None:
                            continue
                        if member.hall_card_id is not None or member.upstream_hall_card_id is not None:
                            raise ValidationProblem("拆分组包含不可操作的订单")
                        if member.game_id not in checked_games:
                            assert_game(member.game)
                            checked_games.add(member.game_id)
                except ValidationProblem as problem:
                    transaction.set_rollback(True)
                    return api_response(data=None, msg=str(problem), code=problem.code)
            shipping_source_no = shipping_main.source_order_no if shipping_main else ""
            requested_source_price = values.get("source_total_price", old_price)
            if "source_total_price" in values and anchor.pk == order.pk:
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg="拆分组原单接单价由系统调平，不能直接修改",
                    code=ErrorCode.VALIDATION,
                )
            if any(
                "total_price" in item and member.pk == anchor.pk
                for member, item in zip(existing_members, existing_inputs, strict=True)
            ):
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg="拆分组原单接单价由系统调平，不能直接修改",
                    code=ErrorCode.VALIDATION,
                )
            source_delta = requested_source_price - old_price
            existing_price_updates = [
                (member, item["total_price"])
                for member, item in zip(existing_members, existing_inputs, strict=True)
                if "total_price" in item and item["total_price"] != member.total_price
            ]
            prices_changed = source_delta != 0 or bool(existing_price_updates)
            if prices_changed and not may_edit_price():
                raise PermissionDenied("你没有修改接单价的权限")
            if prices_changed and anchor.pk != order.pk:
                try:
                    assert_game(anchor.game)
                except ValidationProblem as problem:
                    transaction.set_rollback(True)
                    return api_response(data=None, msg=str(problem), code=problem.code)
            deducted_price = sum(
                (item["total_price"] for item in new_orders), Decimal("0.00")
            )
            remaining_price = requested_source_price - deducted_price
            if anchor.pk == order.pk:
                anchor_price = old_price - deducted_price - sum(
                    (new_price - member.total_price for member, new_price in existing_price_updates),
                    Decimal("0.00"),
                )
                remaining_price = anchor_price
            else:
                anchor_price = anchor.total_price - source_delta - sum(
                    (new_price - member.total_price for member, new_price in existing_price_updates),
                    Decimal("0.00"),
                )
            remaining_price_serializer = PriceChangeSerializer(data={"total_price": remaining_price})
            anchor_price_serializer = PriceChangeSerializer(data={"total_price": anchor_price})
            if not remaining_price_serializer.is_valid() or not anchor_price_serializer.is_valid():
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg=(
                        "拆分后原订单接单价超出有效金额范围"
                        if anchor.pk == order.pk
                        else "拆分后接单价超出有效金额范围"
                    ),
                    code=ErrorCode.VALIDATION,
                )
            remaining_price = remaining_price_serializer.validated_data["total_price"]
            anchor_price = anchor_price_serializer.validated_data["total_price"]
            anchor_price_changed = anchor.pk != order.pk and anchor_price != anchor.total_price
            previous_text = order.project_text
            remaining_text = normalize_project_text(
                values["remaining_project_text"]
            )
            existing_updates = [
                (member, normalize_project_text(item["project_text"]))
                for member, item in zip(existing_members, existing_inputs, strict=True)
                if "project_text" in item and normalize_project_text(item["project_text"]) != member.project_text
            ]
            if splitting_completed:
                pay_affected = [
                    order,
                    *(member for member, _ in existing_price_updates),
                    *(member for member, _ in existing_updates),
                ]
                if anchor_price_changed:
                    pay_affected.append(anchor)
                legacy_anchor = anchor if group_kind == split_group.LEGACY else None
                if split_group.has_split_payment_lock(pay_affected, legacy_anchor=legacy_anchor, lock=True):
                    transaction.set_rollback(True)
                    return api_response(
                        data=None, msg="报酬已结账或正在付款，不能继续拆分", code=ErrorCode.VALIDATION,
                    )
            if (
                not new_orders
                and previous_text == remaining_text
                and not existing_updates
                and not prices_changed
            ):
                transaction.set_rollback(True)
                return api_response(
                    data=None, msg="拆分内容没有变化", code=ErrorCode.VALIDATION
                )
            source_changed = (
                previous_text != remaining_text
                or old_price != remaining_price
                or order.split_id != group_id
                or order.split_kind != group_kind
                or order.split_ship_main_order_no != shipping_main_no
            )
            if source_changed:
                order.project_text = remaining_text
                order.total_price = remaining_price
                order.split_id = group_id
                order.split_kind = group_kind
                order.split_ship_main_order_no = shipping_main_no
                order.updated_by = operator
                order.save(update_fields=[
                    "project_text", "total_price", "split_id", "split_kind", "split_ship_main_order_no",
                    "updated_by", "updated_at",
                ])
                if old_price != remaining_price and price_context.recheck_external_refunds:
                    queue_externalpay_recheck(order)
                stamp_local_action(order)
            if previous_text != remaining_text:
                record(
                    log_type=ACTION_LOG_TYPES["order.update"],
                    action="order.update",
                    actor=operator,
                    actor_id=actor_id, system=actor_id is None,
                    target_kind="order",
                    target_id=order.order_no,
                    order_no=order.order_no,
                    source_order_no=order.source_order_no,
                    field_name="project_text",
                    from_value=previous_text,
                    to_value=remaining_text,
                )
            updated_member_nos = []
            for member, project_text in existing_updates:
                before_text = member.project_text
                member.project_text = project_text
                member.updated_by = operator
                member.save(update_fields=["project_text", "updated_by", "updated_at"])
                stamp_local_action(member)
                updated_member_nos.append(member.order_no)
                record(
                    log_type=ACTION_LOG_TYPES["order.update"],
                    action="order.update",
                    actor=operator,
                    actor_id=actor_id, system=actor_id is None,
                    target_kind="order",
                    target_id=member.order_no,
                    order_no=member.order_no,
                    source_order_no=member.source_order_no,
                    field_name="project_text",
                    from_value=before_text,
                    to_value=project_text,
                )
            for member, new_member_price in existing_price_updates:
                old_member_price = member.total_price
                member.total_price = new_member_price
                member.updated_by = operator
                member.save(update_fields=["total_price", "updated_by", "updated_at"])
                if new_member_price != old_member_price:
                    if price_context.recheck_external_refunds:
                        queue_externalpay_recheck(member)
                    _record_taker_price_change(
                        member, old_member_price, new_member_price,
                        price_context,
                    )
            if anchor_price_changed:
                anchor_old_price = anchor.total_price
                anchor.total_price = anchor_price
                anchor.updated_by = operator
                anchor.save(update_fields=["total_price", "updated_by", "updated_at"])
                if price_context.recheck_external_refunds:
                    queue_externalpay_recheck(anchor)
                _record_taker_price_change(
                    anchor, anchor_old_price, anchor_price, price_context,
                    reason="拆分组接单价自动调平",
                )
            if old_price != remaining_price:
                _record_taker_price_change(
                    order, old_price, remaining_price, price_context,
                    reason="拆分窗口扣除新单或调整组内接单价",
                )
            created = []
            child_fields = {field: getattr(order, field) for field in OrderViewSet.SPLIT_CHILD_FIELDS}
            if new_orders:
                split_group.lock_child_source_numbers()
            child_source_nos = split_group.child_source_numbers(shipping_source_no, shipping_main_no)
            for item in new_orders:
                child_fields["source_order_no"] = next(child_source_nos)
                child = Order.objects.create(
                    order_no=generate_order_no(),
                    status=OrderStatus.UNASSIGNED,
                    split_id=group_id,
                    split_kind=group_kind,
                    split_ship_main_order_no=shipping_main_no,
                    project_text=normalize_project_text(item["project_text"]),
                    total_price=item["total_price"],
                    created_by=operator,
                    sales_user=order.sales_user,
                    **child_fields,
                )
                created.append(child)
                create_performance_for_order(child)
                record(
                    log_type=ACTION_LOG_TYPES["order.create"],
                    action="order.create",
                    actor=operator,
                    actor_id=actor_id, system=actor_id is None,
                    target_kind="order",
                    target_id=child.order_no,
                    order_no=child.order_no,
                    source_order_no=child.source_order_no,
                    reason=f"由订单 {order.source_order_no or order.order_no} 拆分生成",
                    to_value=child.hall_price,
                )
            if group_kind == "PEER" and (old_price != remaining_price or prices_changed):
                refresh_rows = [order]
                if anchor_price_changed:
                    refresh_rows.append(anchor)
                refresh_rows.extend(row for row, _ in existing_price_updates)
                seen_refresh_ids = set()
                for priced_member in refresh_rows:
                    if priced_member.pk not in seen_refresh_ids:
                        seen_refresh_ids.add(priced_member.pk)
                        refresh_customer_estimate(priced_member)

        return api_response(data={
            "order_no": order.order_no,
            "split_id": group_id,
            "split_kind": group_kind,
            "source_total_price": str(remaining_price),
                "updated": updated_member_nos,
            "created": [
                {"order_no": child.order_no, "source_order_no": child.source_order_no}
                for child in created
            ],
            "members": [
                {
                    "order_no": member.order_no,
                    "project_text": member.project_text,
                    "total_price": str(member.total_price),
                    "split_is_balance_anchor": member.pk == anchor.pk,
                }
                for member in [
                    order,
                    *[row for row in group_locked_members if row.pk != order.pk],
                    *created,
                ]
            ],
        })

    @action(detail=True, methods=["get"])
    def audit(self, request, order_no=None):
        """状态流转与改价的留痕（spec §6：详情含状态流转历史）——没有这个出口，留痕在 P2 里就是只写不读的表，价值恰恰在于事后能查。⚠️ 本端点已改读新表 OperationLog（spec §9「迁移后」/ A9 一次改到位）：写路径已不再往旧 AuditLog 写行，继续读旧表只会得到一张恒空的表；action 取值是新 code（前端标签映射若仍按旧取值分支会回退成原始 code，spec §11-8 登记的代价）。⭐ 复刻线：行里新增 id/summary/log_type 三键（订单日志与操作日志两块都要用）；⚠️ 既有 6 键的名字与顺序一字不动、新键一律追加在末尾（既有页面与用例在用）；summary 是服务端渲染好的整句「操作说明」，前端 ⛔ 不许自己拼句子；行形状只在 _operation_log_row() 一处定义。⭐ 审计日志线（spec §3.6）：第 10 个键 actor_display=操作人昵称（空则回落 actor），昵称批量解析一次（actor_display_names(rows)，⛔ 逐行查库=N+1）后逐行传给 _operation_log_row()。"""
        order = self.get_object()
        queryset = OperationLog.objects.filter(order_no=order.order_no)
        if request.query_params.get("scope") == "business":
            queryset = queryset.exclude(action__in=DETAIL_HIDDEN_ACTIONS)
        rows = list(queryset.order_by("-created_at", "-id"))
        if restored := missing_penalty_approval_logs(order, rows):
            rows.extend(restored)
            rows.sort(key=lambda row: (row.created_at, row.pk), reverse=True)
        names = actor_display_names(rows)
        return api_response(
            data=[
                _operation_log_row(
                    row, names.get(row.actor_id) or row.actor_name or row.actor or "系统",
                    source_order_no=order.source_order_no,
                )
                for row in rows
            ]
        )

    @action(detail=True, methods=["post"])
    def notes(self, request, order_no=None):
        """给这一单写一条订单日志（订单详情页「添加」框）。本仓没有对标那种独立的「业务事件流」模型（GetOrderLogs；两路证据：grep ^class .*Log 只有 AuditLog/OperationLog、/logs 路径全仓零命中）⇒ 这里写的仍是一行 OperationLog（与 /audit/ 同一张表、同一个读出口），只是换一个动作 code order.note 把它与状态流转/改价区分开（计划 §2.2 拍板②）；内容进 reason，summary 由模板 order.note（「添加订单日志：{内容}」）渲染。两条硬规矩：⛔ 调用方不能直传 summary（spec §5.9：record() 根本没有这个形参，多传一个直接 TypeError；模板是唯一渲染出口）；⛔ 操作人取认证态（spec §5.9/R20：入参里的 actor 只按既有写端点同口径过一遍长度校验（超长 ⇒ 1000 不是 500）、不落任何列——与 /transition/、录单的「请求体身份一律忽略」同一规则）。
        权限挂对标已有的专门节点 order.add_log（27，见 ORDER_PERMISSIONS）；⛔ 不借同页读端点 order.view_detail（那会让「只看详情」的自定义角色变成能写日志、按对标勾了 27 的管理员反而不生效）。资源不存在（含别的租户的单号）⇒ 走 get_object() 的 404 分支：HTTP 200 + 业务码 1002，一行都不写。返回新建的那一行（与 /audit/ 同形状）——前端「添加成功后就地插入」不必再取一次数（对标也是就地 prepend，见 recon §5）。
        ⚠️ 单行也要解析 actor_display（审计日志线 spec §3.6）：同样经唯一出处 actor_display_names()（传一个单元素列表），⛔ 不在本方法里自己写一份 first_name or username；⭐ 这一行不再多查一次用户——record() 刚刚按 actor_id 查过一次并把结果落在 actor_name 列上、本方法返回的正是那一行 ⇒ 直接取 row.actor_name（同一份「显示名」口径：record() 与 actor_display_names() 用的是同一个 apps/accounts/display.py::user_display_name；语义等价逐档核对：人为行⇒昵称、用户已删⇒两处都回落 actor、机器行⇒都是 actor、空串档 or row.actor 兜住）；⚠️ audit 那条批量读面照旧用 actor_display_names()（历史行的 actor_name 可能为空，且它一次解析一页、是真正的批量入口）。"""
        order = self.get_object()
        serializer = OrderNoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # 只有一次写入（这一行留痕本身就是业务写入），`atomic()` 今天不改变任何行为；
        # 保留它是为了与"本模块的写入点一律在事务里"这条形状一致（将来若加第二笔写入，
        # 不会出现半写；`return` 退出补偿路径的坑见 E-0022，本方法块内没有 return）。
        with transaction.atomic():
            row = record(
                log_type=ACTION_LOG_TYPES["order.note"],
                action="order.note",
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind="order",
                target_id=order.order_no,
                # 计划 §2.2 明写：`target_name` 也落单号（订单日志的人工内容里没有别的名字列）。
                target_name=order.order_no,
                order_no=order.order_no,
                source_order_no=order.source_order_no,
                reason=serializer.validated_data["content"],
            )
        # ⚠️ 只回一行 ⇒ 显示名直接用刚写下的那一列（省掉同一次请求里的第二次主键查询）。
        return api_response(data=_operation_log_row(
            row, row.actor_name or row.actor, source_order_no=order.source_order_no,
        ))

    @action(detail=True, methods=["post"])
    def transition(self, request, order_no=None):
        """状态流转。P3 起这条通用入口不再是后门（spec §13.2）：它必须 ①校验身份（nodes_of(request.user)，匿名=空集=拒绝）②同步绩效（进/出 IN_PROGRESS 时改 Performance.user）③与留痕同事务（与 /price/ 对称）。⚠️ actor 取认证态的用户名、不再读请求体的 actor——请求体里的身份是可伪造的，留痕就会记下一个「谁都可以填」的名字。⚠️ target=WAITING_GRAB 时同时写发单人（F5）：业务方口径「发单人=把订单抛到大厅的人」⇒ 本端点也是写入点，走 apply_transition 的 hall_publisher= 漏斗（不在这里另开 save()）；其余目标态不碰它。
        target=COMPLETED（D-0071）：本端点接受「已完成」——对标没有审核，订单状态变成已完成绩效即生效；授权判据是节点 order.change_status_completed(17)、不是「只能由 /review/ 触发」：由 get_permissions() 按 target 挑（见那里的说明），失败形态是 403（与其它动作的节点门一致）、不是 200+业务码。原先那道「订单完成只能由绩效审核触发」的路径守卫（spec §4.1.1）随 D-0071 取消：那道闸门是我们自加的、对标没有的内控步骤、会挡住钱；E-0028 的教训同时被纠正——该教训原本的正解是「路径要真的排他」，而现在是「这条排他性本身被业务方取消了」。⭐ reason（D-0337）：可选的「说明原因」——「更改订单状态」弹窗选中问题单/已暂停/已退单时前端要求填（对标 setordersta.js 的 radio(status) 监听：这三个状态显示 .remark 输入行；对标还有「异常中」、我们没有），原因随载荷来到这里、经 transition_order(remark=…) 落进留痕行：reason 列 + order.transition 模板渲染出的 summary 尾段「（原因：…）」（空值时渲染器剔除，summary 与不带原因的旧文案逐字相同）；后端必填闸不加（前端闸+宽松可选的裁决见 OrderTransitionReasonSerializer）。"""
        order = self.get_object()
        target = request.data.get("target")
        if not target:
            return api_response(data=None, msg="缺少 target", code=ErrorCode.VALIDATION)
        # ⭐ D-0337：「说明原因」（可选；≤200、trim、可空——校验口径唯一出处 OrderTransitionReasonSerializer）。必填闸在前端（弹窗选中问题单/已暂停/已退单时拦「请输入原因」），
        # 后端对任何目标态都宽松可选（与开放平台 remark 同一裁决）。原因经 transition_order(remark=…) 落留痕行 reason 列、并经 order.transition 模板进 summary
        #（「…（原因：…）」；空值时渲染器剔除该段，summary 与既有文案逐字相同）。
        reason = OrderTransitionReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        outcome = transition_order(
            order,
            target=target,
            remark=reason.validated_data["reason"],
            # ⚠️ `validate_actor=False`：本端点**不校验请求体的 `actor`**（搬迁前逐字如此，
            #    它被忽略——留痕与 `order.updated_by` 都取认证态）。用默认值会凭空多出
            #    一条 1000 分支：超长 `actor` 从「被忽略」变成「请求非法」。
            ctx=self._write_context(request, validate_actor=False),
            dry_run=False,
        )
        if not outcome.ok:
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        return api_response(
            data=OrderSerializer(outcome.detail["order"], context={"request": request}).data
        )

    # ── P3 派单动作（spec §4.1/§5）：四个动作同一套骨架——事务+身份（nodes_of(request.user)）+留痕。
    # ⚠️ 绩效同步一律不在这里调（挂 apply_transition 的钩子，spec §4.1.2）；⚠️ OrderSerializer 必须带 context={"request": request}，
    # 否则 allowed_transitions 对所有人变空（静默丢功能）；四个动作的响应都带了。

    @action(detail=True, methods=["post"], url_path="throw-to-hall")
    def throw_to_hall(self, request, order_no=None):
        """抛单到内部抢单大厅。可带抛单价，不传则用订单已有的默认接单价（spec §5）。本端点没有来源态闸门 ⇒ 它是四条 →WAITING_GRAB 边的写入口（order_status.TRANSITIONS 的节点集都含本端点的类级门之一）：UNASSIGNED/ISSUE/PAUSED/REFUNDED → WAITING_GRAB（这里曾写「UNASSIGNED → WAITING_GRAB 只允许客服」——①「只允许客服」是旧角色轴的措辞（该轴已删）②把可达来源态说少成一条，两句都错；改法=按实现写，spec §11 第 5 条/§16.4 第 5 条）。
        准入=两层节点门（都不是职位）：①类级门=ORDER_PERMISSIONS["throw_to_hall"]=独占 order.send_to_grab_hall（36；此前是 any(20/36/1162)，只持 20 或 1162 的人照样能抛）②只有当载荷带 price 时再单独要 order.submit_payout_price（改价是能改变金额的动作，见下方那段注释）。
        成功路径同时把发单人写成认证态的 request.user（Order.hall_publisher；spec §6：撤回不清它，再抛一次覆盖成新的人）。"""
        order = self.get_object()
        # ⭐ D4（fx-D）：远端已被代练通打手接手 ⇒ 本地不许再抛大厅（否则平台打手与内部打手会同时接同一张单）。
        # 判据本体在 dailiantong_push：有效推送 + remote_is_accepted()（「代练通优先」同一份实现）。⚠️ 懒导入：dailiantong_push 会拉起审计/客户端，本模块是热模块。
        from apps.business.dailiantong_push import (
            cancel_application_submitted_for_order,
            hall_throw_blocked_by_platform_denial,
        )

        # ⭐ D8：撤销申请已提交 ⇒ 允许重发时**重新抛大厅**（连占位接手人一起
        # 释放）；否则 D2「有接手人 ⇒ 不许回池」/ A2「已抛代练通禁止手改状态」会把它挡住。
        rethrow_after_cancel = cancel_application_submitted_for_order(order)
        platform_denial = hall_throw_blocked_by_platform_denial(order)
        if platform_denial is not None:
            return api_response(
                data=None, msg=platform_denial, code=ErrorCode.BUSINESS
            )
        serializer = ThrowToHallSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        price = serializer.validated_data.get("price")
        # 改接单价是能改变金额的动作，单独收口（spec §4.3/§6.3；D1/Ruling 12）。⚠️ 判据是节点 code order.submit_payout_price（对标 1162「修改发单价提交操作」），不是 legacy business.override_taker_price；
        # ⛔ 这一层不能用 can_edit_taker_price（=not flag_enabled(restrict_edit_taker_price)）：D3 让 flag fail-open、内置职位 BUILTIN_ROLE_FLAGS 空集 ⇒ 等于谁都能改价、最承重的闸门空转（它只用在 /adjust 第二层，位置见 performance_views.py）。
        # ⚠️ 必须独立于类级门（类级门=独占 36、只回答「能到抛单端点」；「能不能改价」只能用那一个改价 code 单独问——36 持了 1162 没持 ⇒ 不带价 200/带价 403，有意的两层）；判据放事务之前（只依赖「谁+有没有带价」，提前拒绝就不会「先改状态再靠回滚兜底」，E-0022 同族）；必须是 PermissionDenied（HTTP 403 + code 403，已认证但无权限是传输层语义，P5 §4.2）。
        if price is not None and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改接单价的权限")
        proposed_price = price if price is not None else order.hall_price
        if changes_approved_penalty_price(order, proposed_price):
            return api_response(msg=PENALTY_AMOUNT_LOCKED, code=ErrorCode.BUSINESS)
        actor = getattr(request.user, "username", "") or ""
        with transaction.atomic():
            try:
                apply_transition(
                    order, OrderStatus.WAITING_GRAB,
                    nodes=nodes_of(request.user), actor=actor,
                    # ⭐ WAITING_GRAB 是池内目标态 ⇒ 回池钩子（performance._on_pool_entry）会跑，从「已退单」过来时它写的两条冲销留痕是人为动作、必须带操作人主键
                    # ⇒ 从认证态传下去（⛔ 不是让域层拿 order.updated_by 反查）。
                    actor_id=request.user.pk,
                    # 发单人 = 抛单的人（认证态事实，与 `created_by` 同源；spec §6）。
                    # ⚠️ **走漏斗**（F5）：写入由 `apply_transition` 的
                    # `update_fields` 负责，本视图**不再自己 save 这一列**。
                    hall_publisher=request.user,
                    # ⭐ D8：已提交撤销 ⇒ 允许重发时把旧「代练通」占位人释放 / 跳过
                    # A2「已抛代练通禁止手改状态」；没有撤销申请时两格都是 False（行为不变）。
                    allow_status_change_while_pushed=rethrow_after_cancel,
                    allow_taker_release=rethrow_after_cancel,
                )
            except IllegalTransition as exc:
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            if price is not None:
                order.hall_price = price
                # 发单人由上面的漏斗写。本视图只负责价：只在带了价时保存，
                # `update_fields` 只列 `hall_price` / `updated_at`。不带价时不保存。
                order.save(update_fields=["hall_price", "updated_at"])
                # ⭐ A8/L-6：本视图直接写发单价 ⇒ 同事务盖「我方最后一次本地动作时刻」（唯一实现 stamp_local_action；上面那次 apply_transition 盖的是状态入口那笔、本支是价写点自己的一笔
                # ——穷举网按「每个写点自己盖」对拍，⛔ 不靠「同事务里别人盖过」的口头保证）。
                stamp_local_action(order)
                # ⭐ P3：发单价是下行要同步的两样之一 ⇒ 写价后立刻记一行 `order.update`。
                # ⚠️ 只能挂在**带价**这一支（`is not None`，E-0021）；不带价时这一列没变，
                #    写了就是一条假事件。
                from apps.business.peer_dispatch import notify_order_price_changed
                notify_order_price_changed(order)
                # ⭐ T3：**同一处**把价跟到下游鸡蛋铺那条线（第三方下家）——
                #    与上一句不是同一条线（那一句是团团通租户 ↔ 团团通租户，D-0152）。
                #    ⚠️ `jidanpu_member` 已在本模块顶部 import（既有那几处触发点同一个名字）。
                jidanpu_member.queue_price_update(order)
            record(
                log_type=ACTION_LOG_TYPES["order.throw_to_hall"],
                action="order.throw_to_hall",
                actor=actor,
                actor_id=request.user.pk,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                # 发单价 = `Order.hall_price`（带价则已写入，未带价则沿用订单已有值）
                to_value=order.hall_price,
            )
            # ⛔ 这里没有租户级 hall.launch 那一句——那个模块与整条线已拆除（docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md）⇒ 这一档只剩个人绑定线一条。
            # ⭐ 下游鸡蛋铺线（spec §5.3 第 1 行）：同一个事务里给「本线归属该单的绑定」排一条 member.launch（对方录单+抛单）；它自己会排一次「提交后当场试一次」（transaction.on_commit）⇒ 抛单不必等下一轮后台。
            # ⚠️ 位置在 atomic() 块内、record(...) 之后：上面的 IllegalTransition 失败路径走不到这里 ⇒ 不会留下「抛单没成、事件却写了」。
            jidanpu_member.queue_hall_launch(order)
        return api_response(data=OrderSerializer(order, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def withdraw(self, request, order_no=None):
        """撤回抛单：只接受 WAITING_GRAB → UNASSIGNED（spec §4.1/§5）。⚠️ 入口显式判前置状态（Ruling 27①）：光靠状态机边不够——ISSUE/PAUSED/IN_PROGRESS → UNASSIGNED 都是指向 UNASSIGNED 的边（后两条还分别是「问题单/暂停单退回池内」与「清除接单人」的入口），任何「有人接的问题单/暂停单/代练中单」都会被本动作迁走、对 IN_PROGRESS 还会静默地执行一次清人——业务上是错的：/withdraw 的语义是「撤回抛单」；拒绝走业务码 ErrorCode.BUSINESS（与 IllegalTransition 的失败形态一致；不要新造码，1012/1013/1014 已被占用）。
        📌 被这道闸门挡掉的来源并没有失去入口：问题单/暂停单要清接单人走 /clear-taker（它按 Ruling 27② 刻意不限制来源态），或通用 /transition/。
        绩效回退由 apply_transition 的钩子负责（spec §4.1.2/D-0032：迁入「未分配/待抢单」一律回退三元组、不看来源态）；⛔ 不要为了「对称」在这里补一刀。"""
        order = self.get_object()
        if order.status != OrderStatus.WAITING_GRAB:
            return api_response(
                data=None,
                msg=f"只有待抢单可以撤回，当前状态：{order.status}",
                code=ErrorCode.BUSINESS,
            )
        actor = getattr(request.user, "username", "") or ""
        with transaction.atomic():
            try:
                apply_transition(
                    order, OrderStatus.UNASSIGNED,
                    nodes=nodes_of(request.user), actor=actor,
                    # ⭐ 同 `/throw-to-hall/`：`UNASSIGNED` 也是池内目标态 ⇒ 回池钩子会跑
                    #    ⇒ 人为触发的冲销留痕要带操作人主键（认证态）。
                    actor_id=request.user.pk,
                )
            except IllegalTransition as exc:
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            record(
                log_type=ACTION_LOG_TYPES["order.withdraw"],
                action="order.withdraw",
                actor=actor,
                actor_id=request.user.pk,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
            )
            # ⛔ 这里没有租户级「去鸡蛋铺撤单」那一句——那个模块与整条线已拆除（docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md）⇒ 这一档只剩个人绑定线一条（口径与门槛都由 jidanpu_member.queue_withdraw 自判）。
            # ⭐ 下游鸡蛋铺线（spec §5.3 第 4 行）：撤回抛单 ⇒ 这张单不再挂在我方大厅 ⇒ 同事务给「已经/即将录到对方系统」的绑定排退单；门槛（这张单推出去过没有）由 queue_withdraw 自判（对普通撤回是 no-op）。
            # ⚠️ 位置在 atomic() 块内、record(...) 之后：失败路径走不到这里（不会留下「撤回没成、事件却写了」）。
            jidanpu_member.queue_withdraw(order)
        return api_response(data=OrderSerializer(order, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def assign(self, request, order_no=None):
        """指派给指定代练员：UNASSIGNED → IN_PROGRESS（spec §4.3）。⚠️ 能不能指派只由节点回答（单轴化）：类级节点门=ORDER_PERMISSIONS["assign"]=any(order.assign_taker, order.submit_payout_price)。原先这里还有一道函数体职位闸门（Role.CS，Ruling 28）——它防的是：大厅边 WAITING_GRAB → IN_PROGRESS 允许旧角色轴的「代练员」，于是代练员可以 POST /assign {"user_id": 任何人} 把大厅单直接拉进 IN_PROGRESS 并指定给别人、绕开抢单条件更新（AND user_id IS NULL）与 §7.2 的抢单限制（现行四支：NULL 接单价/禁自助接单/价格区间/持有上限，落点 apps/business/claim.py::check_grab_limits）；📌 那道职位闸门随单轴化删除：过类级门必须持 order.assign_taker（对标 18）、这条节点本就是「指派」的判据——自定义职位勾了它就能指派、与内置客服同一套（本线的目标形态；代价如实：客服主管现在也能指派）。📌「禁自助接单」已落地（StaffProfile.grab_forbid_self_service+读取层+上述那一支，复用 1013）：只拦 /claim、/assign 不禁、大厅照常可见、缺档案=不禁止（对标原名见 js/userlist.js:305/:772，旧文档写作「禁止公开接单」是转述；登记见 spec §16.4 #21 与 D-0091）。
        已认证但无节点 ⇒ HTTP 403（PermissionDenied，传输层语义，不是 200+业务码）；📌 租户管理员（持「系统管理员」职位，判据=apps.core.permissions.is_tenant_admin）拿到全目录（nodes_of 口径，与 hall 同源），只置 is_superuser 不持该职位的账号不短路；⛔ 不取 roles_of/ALL_ROLES/GROUP_TO_ROLES——已删符号（口径见 apps/business/roles.py）。📌 判据放在 get_object() 之前：授权先于资源查找，未授权者无法用「404 与 403 的差别」探测某个订单号是否存在。来源态由状态机边回答（(src, IN_PROGRESS) 对每个非自环来源态都有边），不再有「只接受 UNASSIGNED」的入口闸；保存指派=先清接手人、再指派，任何状态都同一个动作。
        📌 /assign/ 因此是一个「换人」动作（spec §4.3）：进入事务后若那条 MAIN 绩效行已有接单人，先跑既有 reset_taker_performance（七列回退）再 set_taker(新接手人)——只有真正换人（perf.user_id != taker.id）才回退七列（R-2b），同一个人再保存⇒跳过回退、保住他自己的 taker_price 覆盖价与 bonus_amount；若该行被软删则同一步复活它（清 deleted_at/deleted_by）、不新建行（uniq_order_main_taker 只允许一条 MAIN 行）；带 price 时是 reset→set_taker→写价→再重算一次 temp_amount，保证「预计绩效=有效基价×提成率」用的是新发单价。⛔ R-2a：该 MAIN 绩效行已结账 ⇒ 本动作在进入事务之前就返回 1014（三字段并集判据，见 order_writes.assign_order 的 docstring）——不看是否换人，要让开路先撤销结账。⛔ R-3（控制方裁定；spec §4.3）：order.status 已经是 IN_PROGRESS 时跳过 apply_transition——TRANSITIONS 没有自环、本动作目标态就是 IN_PROGRESS ⇒ 不跳过时「代练中」的单保存指派一律 1002（换人/改 hours/project/改发单价全做不了），而代练中正是最常见状态；⛔ 不给状态机加自环（自环会经 get_allowed_transitions 泄漏成「代练中→代练中」下拉选项）；R-2a 闸、R-2b 的「同人才保留」、set_taker、写价后重算与 record(...) 留痕全部照旧（授权那半没变：类级节点门仍是那两个 code，状态机只负责回答「这条边在不在」——「代练中」那一档按 R-3 不咨询状态机，这句对其余来源态成立）。⭐ A4：动作体已搬进领域核心 apps/business/order_writes.assign_order（唯一判据出处，spec §D5）——本动作只剩请求级校验（AssignSerializer、「目标用户不存在/已停用」、带 price/fee 时那两道 order.submit_payout_price 闸）+ 包信封；逐单判据（sales_user/is_settled/状态机边）与五条特例（R-3 跳迁移、R-2b 同人跳过回退、软删行复活、缺行在事务内补、钱钩子不在本入口跑）见那个函数的 docstring，⛔ 不要再在这里复制一份。"""
        order = self.get_object()
        # ⭐ 业务方新口径：已被代练通接单（远端 12/13/14/15）⇒ 不许再指派。⛔ 不再「先指派、再自动协商撤销」——响应说成功、下一轮回读又把打手换回占位人（M4 的语义矛盾），且平台打手与内部打手会同时干同一张单。
        # 判据在 _platform_taken_error（本体=dailiantong_push.platform_taken_denial）。
        # 位置在请求级校验之后、核心之前：目标人不存在/403 那些错误与这一单无关，保持原顺序（先回答「这次请求本身对不对」，再回答「这一单能不能指派」）。
        serializer = AssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user_id = serializer.validated_data["user_id"]
        price = serializer.validated_data.get("price")
        fee = serializer.validated_data.get("fee")
        hours = serializer.validated_data.get("hours")
        project = serializer.validated_data.get("project")
        # ⭐ 请求级校验收在 _resolve_assign_taker 一处（D-0179 起与「建单+指派」共用）：目标不存在/已停用 ⇒ 业务码 1000；带 price/fee 的两道 order.submit_payout_price 闸
        #（判据 is not None，E-0021：0 是合法边界）⇒ HTTP 403（传输层语义，P5 §4.2）。位置在调核心之前（⇒ 也在批量侧逐单循环之前）：
        # 它们只依赖「谁+有没有带这个键」、与具体哪一单无关 ⇒ 失败是整批 403，⛔ 不是逐笔不合格（那会把授权语义错译成数据不合格）。逐条理由见该方法 docstring。
        taker, error = self._resolve_assign_taker(
            request, user_id=user_id, price=price, fee=fee
        )
        if error is not None:
            return error
        blocked, taken_msg = self._platform_taken_error(order)
        if blocked:
            return api_response(data=None, msg=taken_msg, code=ErrorCode.BUSINESS)
        outcome = assign_order(
            order,
            taker=taker,
            price=price,
            fee=fee,
            hours=hours,
            project=project,
            chat_follow_up=serializer.validated_data.get("chat_follow_up", False),
            # ⚠️ validate_actor=False：本端点不读、也不校验请求体的 actor（搬迁前逐字如此，它被忽略——留痕与 order.updated_by 都取认证态）。
            # 用默认值会凭空多出一条 1000 分支：超长/非法的 actor 从「被忽略」变成「请求非法」。
            ctx=self._write_context(request, validate_actor=False),
            dry_run=False,
            # ⭐（业务方）：已抛代练通的单也允许**先指派、随后立刻撤回**平台单；
            # 不再被「已抛代练通禁止手工改状态」挡住（撤回失败时会在响应 msg 里明说）。
            allow_status_change_while_pushed=True,
        )
        if not outcome.ok:
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        assigned = outcome.detail["order"]
        # ⭐（业务方）：指派成功后**立刻**撤掉该单的代练通订单（同步返回结果）。
        # ⚠️ 本地指派已经生效；上游撤不掉时⛔ 不静默成功，把中文提示（含上游原话）随响应回给客服。
        from apps.business.dailiantong_actions import withdraw_for_internal_claim

        _had_push, withdraw_msg = withdraw_for_internal_claim(assigned)
        return api_response(
            data=OrderSerializer(assigned, context={"request": request}).data,
            msg=withdraw_msg,
        )

    def claim(self, request, order_no=None):
        """抢单（代练员的动作，spec §4.3/§5）。⚠️ 路径不在订单路由上：spec §5 的端点表写的是 POST /api/business/hall/{order_no}/claim，所以这里不是 @action——做成 action 会额外生成 orders/{order_no}/claim/（同一件事两个入口、其中一个还是被 spec 否决的写法）；显式路由在 urls.py（控制方 Ruling 33），与大厅同一手法。⚠️ 条件更新与订单状态必须同一个事务（spec §7.4）：否则会出现「绩效写了人、订单还停在大厅」的脏行，而这两者之间没有任何数据库约束能表达联合一致性；claim_order 是纯逻辑（自己不开事务），事务边界就落在这里——与 /assign 等三个动作同形、与 spec §7.4 表里那一行（端点为事务单位）一致。
        ⚠️ 授权入口就是这里：类级节点门是 grab_hall.take_order（对标 50，ORDER_PERMISSIONS["claim"]），「已认证但无该节点」按 P5 §4.2 是传输层语义 ⇒ HTTP 403。状态机这一层不构成兜底闸门：WAITING_GRAB → IN_PROGRESS 这条边的节点集是 {order.change_status, grab_hall.take_order}，而重写之前 claim_order 传给 apply_transition 的是常量 {Role.TAKER}（旧角色轴），所以「客服也会被状态机拦住」是假的——实测（去掉闸门）客服以 code=0 抢单成功；现在 claim_order 收调用方的节点集（下面把已经算好的 nodes 传进去）、状态机真的会判，但它仍然不是授权入口（claim_order 收 ID、不认身份），⛔ 每个新调用点都必须自己先过闸门。📌 判据放在 get_object() 之前：授权先于资源查找（与 /assign 同理）。⚠️ 接线只有一处不同：claim_order 自己重新取一次行（它收的是 ID，理由见 apps/business/claim.py），所以这里 refresh_from_db() 是必须的——否则写进留痕与响应的是视图手上那份过期的 status。"""
        nodes = nodes_of(request.user)          # 只算一次：类级门与 `claim_order` 用**同一份**判据
        order = self.get_object()
        # ⚠️ C1：抢单人是本单的销售提成归属人 ⇒ 显式拒绝。机制与 /assign 那条同源：录单已为 order.sales_user 建了一条 SALES 绩效行，而 uniq_performance_order_user 是不分 emp_type 的 (order,user) 唯一约束；claim_order 的条件更新会把 MAIN 行的 user 写成同一个人 ⇒ UniqueViolation ⇒ 500（修复前实测）。⛔ 不要放宽约束（test_my_orders_api.py 那条既有不变量用例）；拒绝只是拒绝：不动客服行、不动 sales_user、不写任何绩效行。
        # 📌 业务码暂用 ErrorCode.BUSINESS（1002）——P4-7/P4-12 收口时应给它一个专用码。
        # ⚠️ 判据放 transaction.atomic() 之前（还没有任何写入），与 get_object() 之后的其它前置判据同形。
        if request.user.id == order.sales_user_id:
            return api_response(
                data=None,
                msg=(
                    "该账号是本单的销售提成归属人，不能同时抢自己录的单"
                    "（同一订单同一人只能有一条绩效）；若要支持兼岗需先放开约束"
                ),
                code=ErrorCode.BUSINESS,
            )
        actor = getattr(request.user, "username", "") or ""
        with transaction.atomic():
            try:
                # ⚠️ 接住返回值：`claim_order` 收 ID、**自己重新取行**（见它的 docstring），
                #    `order` 是视图手上那一份，所以「这次接单有没有作废解冻申请」这个
                #    **内存属性**只挂在它返回的实例上（不是模型列，不落库）。
                claimed = claim_order(
                    user_id=request.user.id,
                    order_id=order.id,
                    nodes=nodes,
                    enforce_deposit=deposit_enforced_for(request),
                )
            except ClaimError as exc:
                # ⚠️ 与下面那条 except 同形：显式回滚，不依赖「四支限制都在写入之前校验」这个不变量（E-0022：atomic() 内正常 return 会提交块内已落库的写入）。今天每条 ClaimError 确实都早于绩效条件更新（claim.py 的 NULL 价/禁自助接单/价格区间/接单上限/rowcount==0），但那是不变量、不是结构保证：谁把某一支挪到写入之后，这里就变成「200+业务码+脏行」的静默失效。一行成本换掉它（M3）。
                transaction.set_rollback(True)
                return api_response(data=None, msg=str(exc), code=exc.code)
            except IllegalTransition as exc:
                # 预期失败 ⇒ 给业务码而不是让它 500。这条支路有两条成因、只有②在本视图可达：①调用方节点集不含本条边所需节点——本视图不可达（类级节点门判 grab_hall.take_order，下面传给 claim_order 的 nodes 就是 nodes_of(request.user)，节点集交集必非空；前提「内存里的 from-state 就是 WAITING_GRAB」由 claim.py:167 的来源态守卫保证，去掉那处守卫入口门放行也照样可达（UNASSIGNED→IN_PROGRESS 节点集交集为空）；①服务的是不带这道门的调用点——claim_order 收 ID、不认身份，那些调用点上状态机才是唯一授权判据 ⇒ 不能省，Ruling 50）；
                # ②写状态前复读发现调用方手上是陈旧状态（复读+行锁）——可达：claim_order 在更早处取的订单行，到写状态之前可能已被并发撤回改成 UNASSIGNED ⇒ 复读比对失败 ⇒ 抛 IllegalTransition；tests/test_claim_concurrency.py::test_a_committed_withdraw_is_not_overwritten_by_an_inflight_claim 造的正是这个顺序 ⇒ 下面的显式回滚是必须、不是纵深防御（并发撤回能走到这里是因为抢单会复读比对；「不回读所以只是丢更新」在行锁落地后不成立，不要把成因写回「不回读」）。
                # ⚠️ claim_order 的绩效条件更新已经写过行，正常 return 会提交那半截写入（E-0022：atomic() 是事务、不是回滚点）⇒ 必须显式回滚，否则留下「绩效行有接单人、订单还停在待抢单」的脏行——spec §7.4 明说这种联合一致性任何数据库约束都表达不了，且它返回 200+业务码看着像正常；⚠️ 必须是 IllegalTransition 不是 ValueError——后者会把写库路径冒出的未预期 ValueError 也伪装成业务结果（E-0010）。
                transaction.set_rollback(True)
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            except (CommissionError, _AmountError) as exc:
                # 重算 `temp_amount` 时提成不可算（§16.4 #17）：条件更新**已经写过行**，
                # 这里是块内 `return` ⇒ 必须显式回滚，否则留下「绩效有人、订单还在大厅」的脏行（E-0022）。
                transaction.set_rollback(True)
                return api_response(data=None, msg=exc.message, code=exc.code)
            order.refresh_from_db()
            record(
                log_type=ACTION_LOG_TYPES["order.claim"],
                action="order.claim",
                actor=actor,
                actor_id=request.user.pk,
                # {接单人} → `target_name`（R11）；`target_id` 放数字 id
                target_kind="user",
                target_id=request.user.id,
                target_name=request.user.username,
                order_no=order.order_no,
                source_order_no=order.source_order_no,
                to_value=order.hall_price,
                # {渠道}（spec §5.9：reason 只放纯值）——值域与判据的唯一出处是 apps/business/claim_channel.py：App/小程序走 Bearer 令牌接单（过去一律被记成「系统网页」，本线修的就是这一句）；网页会话仍是「系统网页」。
                # 统计侧（app_stats）按这个值分「移动端接单」。⛔ 别在这里另写字符串。
                reason=channel_for(request),
            )
            # ⭐（spec §7.1 第 2 行）：抢单成功 ⇒「这一单在大厅被这个账号抢到」记一条待投递事件；排在 claim_order(...) 成功之后、record(...) 之后（条件更新已落库、order 已 refresh；上面三处 set_rollback+return 的失败路径走不到这里 ⇒ 不会留下「没抢到、事件却写了」的脏行）。
            # ⛔ 必须显式传 occurred_at=timezone.now()：claim_order 里那次 apply_transition 已经先经状态漏斗入队了一条 order.update（此刻 MAIN 行归属人已是抢单人），两条事件共用同一个默认值 order.updated_at，而 worker 取活最老优先 ⇒ 下家先应用更窄的 order.update 并推进 last_applied_at、这条 order.grab 随后命中接收侧 <= 的 stale 闸被良性跳过 ⇒ 它带的整张白名单（total_price/remark/project_text/客户联系方式/content/game）静默丢掉；对账水位是同一个 last_applied_at ⇒ 自愈也补不回来。
            # ⚠️ 与上面那条换人撤单同一理由、同一写法；入队仍排在全部写入之后，换的是时刻来源、不是顺序。判据：tests/test_peer_p3_event_ordering.py。
            enqueue_order_grabbed(order, request.user, occurred_at=timezone.now())
            # ⛔ 本档不排任何撤下事件——租户级「去鸡蛋铺撤单」那句随那个模块与整条线拆除（docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md）；enqueue_order_grabbed 那条上下游事件照旧。⚠️ 位置口径不变：仍在 transaction.atomic() 块内、record(...) 之后，上面三处 set_rollback + return 的失败路径走不到这里。
        payload = OrderSerializer(order, context={"request": request}).data
        payload["release_request_cancelled"] = bool(
            getattr(claimed, "release_request_cancelled", False)
        )
        # ⭐（业务方）：抢单成功后**立刻**撤掉该单的代练通订单（事务已提交）。
        # ⚠️ 同上：本地抢单已生效；上游撤不掉时随响应回中文提示，⛔ 不假装撤回成功。
        from apps.business.dailiantong_actions import withdraw_for_internal_claim

        _had_push, withdraw_msg = withdraw_for_internal_claim(order)
        return api_response(data=payload, msg=withdraw_msg)

    # ⛔ remote_claim 这个 action 已整条删除（连同路由 hall/remote/<source_system>/<remote_order_id>/claim/ 与 ORDER_PERMISSIONS 那一格）：
    # 它接的是 P2（租户级）鸡蛋铺镜像单，随租户级鸡蛋铺线一起拆除（docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md）。
    # ⚠️ 上家线自己的镜像单接单入口不是它：独立视图 JidanpuUpstreamHallClaimView（hall/upstream/<pk>/claim/，路由与视图都还在）。

    @action(detail=True, methods=["post"], url_path="clear-taker")
    def clear_taker(self, request, order_no=None):
        """清除接单人/改派：迁到 UNASSIGNED，绩效回退三元组（spec §4.1.2/D-0032）。⚠️ 接受任何指向 UNASSIGNED 的来源态——就是 TRANSITIONS 里所有以 UNASSIGNED 为目标的边（节点集都含 order.change_status 与 order.clear_taker）；业务方裁定给 REFUNDED 加了回池出边（REFUNDED→UNASSIGNED / REFUNDED→WAITING_GRAB）之后，指向 UNASSIGNED 的边共 5 条：IN_PROGRESS/ISSUE/PAUSED/WAITING_GRAB/REFUNDED（原写「共 4 条」是加边之前的计数）。刻意不限制成只接受 IN_PROGRESS（Ruling 27②）：在 D-0032 之下「迁入未分配⇒无接单人」，所以它从任何这些来源都真的会清人——包括「清掉问题单/暂停单的接单人」这个正当动作，限制来源反而会挡掉它。
        各来源：IN_PROGRESS=正常「清除接单人」+三元组回退；ISSUE/PAUSED=退回未分配并清人（D-0032 的直接后果）；WAITING_GRAB=等效于 /withdraw（池内的单本来就没有接单人，三元组回退是空操作）——两个入口的差别只是语义、不是写入；REFUNDED=退单后重新开始（裁定第 3 条，同样回退三元组；这是加边带来的连带可达路径、不是新语义：本端点的契约就是「任何来源→UNASSIGNED」，退单只是新增的一个来源）。
        绩效回退由 apply_transition 的钩子负责——这里只做状态与留痕，另调一遍就是两套机制。"""
        order = self.get_object()
        actor = getattr(request.user, "username", "") or ""
        # ⚠️ 入参校验必须在 apply_transition 之前（spec §5.2/§5.6）：序列化器失败时一行都不许写。serializer.is_valid(raise_exception=True) 抛的 ValidationError
        # 由 apps/core/exceptions.py 统一翻成 HTTP 200 + code=1000（既有形状，不要自己 catch）。
        serializer = ClearTakerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        is_del_emp = serializer.validated_data["isDelEmp"]
        # ⭐（业务方裁定）：isDelEmp=Y 会把两个绩效金额列归零（=真的会改钱）⇒ 与 /assign/ 带 fee 同一条收口，额外要节点 order.submit_payout_price(1162)。
        # ⚠️ N 不改钱、不要它。📌 判据放在 apply_transition 之前：被拒时一行都不写（与入参校验同形）；⚠️ 走节点判据（has_node_now），不是 legacy codename。
        if is_del_emp == "Y" and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改绩效金额的权限")
        # 「原接单人」必须在清除之前取：apply_transition 的钩子会把 perf.user 清空，之后再查只剩 None。用容错查询（不是 main_performance_of 的「缺行就 500」口径）：
        # 旧单可能根本没有 MAIN 绩效行，这不该让本端点变 500。
        # ⭐ D-0144（D1）：起点 order 来自本视图 get_object()（默认 manager）⇒ 已删单在更早一步就落 1002 ⇒ 这里不加 order__deleted_at 条件（加了只是多一次无谓 JOIN）。
        _perf = (
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
            .select_related("user")
            .first()
        )
        former_taker = _perf.user if (_perf is not None and _perf.user_id) else None
        with transaction.atomic():
            try:
                # ⭐ D2 例外①（R1，spec §4.1.2 枚举表第①行）：本调用就是「显式清除当前接单人」这个动作本身——它的实现就是迁入 UNASSIGNED，
                # 必须显式打开 allow_taker_release，否则死锁（「要清人，请先清人」）。该开关只跳过 D2 判据，其余判据（边/节点/复读/已结账）一个字不放过。
                apply_transition(
                    order, OrderStatus.UNASSIGNED,
                    nodes=nodes_of(request.user), actor=actor,
                    # ⭐ 同 `/withdraw/`：池内目标态 ⇒ 回池钩子会跑 ⇒ 人为触发的冲销留痕
                    #    要带操作人主键（认证态）。这正是从「已退单」回池的主路径。
                    actor_id=request.user.pk,
                    allow_taker_release=True,
                )
            except IllegalTransition as exc:
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            # ⚠️ isDelEmp=Y ⇒ 在既有回退之外把两个金额列归零（C15/§5.2）。⚠️ 归零用的那条行必须在 apply_transition 之后取：迁入 UNASSIGNED 的钩子（_on_pool_entry）会经 ensure_main_performance_of 补建缺的那条 MAIN 行（create_performance_for_order，新行 temp_amount=hall_price）；旧实现把取行放事务之前 ⇒ 「缺行」档跳过归零、补建出来的行留着 hall_price（实测 77.00 而不是 0.00），Y 静默等价于 N（E-0027）。
            # ⛔ 旧句「不要改成 ensure_main_performance_of（那会给旧单凭空建行）」与实现相反、已被实测证伪（缺行档实测 rows=1，行已经被钩子建出来了）⇒ 直接复用钩子同一个取行口径（缺行就补），比「事务前容错查询」更严：钩子被清掉时后者仍会漏。
            # ⚠️ 同一个归零实现（Z6）：垃圾桶走 /adjust/ 的固定载荷、这里直接调函数，两处都落进 write_performance_amounts。
            if is_del_emp == "Y":
                perf = ensure_main_performance_of(order)
                zero_performance_amounts(perf)
                perf.save(update_fields=["temp_amount", "bonus_amount", "updated_at"])
                # ⭐（客服预计绩效线，计划 §2 T1.2 显式挂点 11）：刚把客服预计的减项清零 ⇒ 补刷一次客服预计（订单这时已被迁到 UNASSIGNED、_on_pool_entry 也刷过一次；
                # 那次刷的是「清人之后、归零之前」的值，本次才是最终值）。refresh_customer_estimate 对「值没变」不写库 ⇒ 这里多半是一次纯读。
                refresh_customer_estimate(order)
            record(
                log_type=ACTION_LOG_TYPES["order.clear_taker"],
                action="order.clear_taker",
                actor=actor,
                actor_id=request.user.pk,
                target_kind="user",
                target_id=former_taker.id if former_taker is not None else "",
                # ⭐（spec §3-A2）：人名落账号名称（user_display_name，昵称优先/空串回落登录账号）；关系为空 ⇒ 保持空串（⛔ 不渲染 None）。
                # ⚠️ 那条模板是「原接单人：{target_name}」——这里换的是被清掉的前接单人的名字，不是操作人（操作人由【actor_name】前缀给，两者⛔不可混）。
                target_name=(
                    user_display_name(former_taker)
                    if former_taker is not None
                    else ""
                ),
                order_no=order.order_no,
            )
            # ⭐ 下游鸡蛋铺线（spec §5.3 第 4 行的「回池」）：清人 ⇒ 这张单回到未分配、不再属于原来那个代练员 ⇒ 同事务给已推过的绑定排退单。
            # ⚠️ 门槛由 queue_withdraw 自判（没推出去过的单在这里是 no-op）。⚠️ 位置在 atomic() 块内、record(...) 之后：失败路径走不到这里。
            jidanpu_member.queue_withdraw(order)
        return api_response(data=OrderSerializer(order, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def submit(self, request, order_no=None):
        """提交完单（代练员，spec §5/§6.2；金额口径见 P4 的 B3）。写两样、同一个事务：①状态 IN_PROGRESS→PENDING_REVIEW ②review_status=待审核（外加既有留痕）。
        ⭐ R2（编排方裁定）：本端点不写任何金额——既不写 Performance.bonus_amount、也不写 temp_amount；新口径下「钱只在 COMPLETED 生成」（D3），而「交单即生效、预计跟随生效」（D-0049 方案 A）的前提已废 ⇒ 交单不再是生成点、也不再是预计值的刷新点；bonus 的计算（原先那句 commission_amount(...)）一并删掉——它算出来的数不再有任何消费方，留着就是「算完就丢」的死代码。⚠️ 代价（R2 要求如实登记）：temp_amount 同时是扣点基数（settlement_amount.payable_net 的 base）⇒ 交单后扣点基数保持「预计」值：认领/指派那一刻按当时提成率算出的 temp_amount 会一直留到订单变成 COMPLETED（或改价类路径重算它），中间改提成率不再刷新它（「认领后、交单前改率⇒按旧值少扣钱」的口径按 R2 重新打开、方向是可能少扣钱；登记在 docs/specs/2026-09-28-order-page-rules-design.md §8）。
        ⚠️ bonus_amount 不含扣点（P4 的 B3）：P4 的应付净额=bonus_amount−扣点额+Σ调整记录，扣点折进来会让 P4 双重扣点；扣点额由 performance_amount.deduct_amount() 单独算（给 P4 复用）。⚠️ 来源态必须显式判 IN_PROGRESS：ISSUE/PAUSED→PENDING_REVIEW 这两条边的节点集是 {order.change_status}(16)，持 16 的人打这个端点就能把一张问题单直接送进验收、跳过代练员交单（与 /withdraw、/assign 的来源态闸门同形）。⚠️ 必须是「这张单的接单人」：只判「持 order.submit_work」不够——否则另一个持该节点的账号能替别人交单。"""
        order = self.get_object()
        # ⭐ 那条 MAIN 绩效行可能已经被 B-1 删掉（业务方裁定「可以直接删掉无主绩效单」）⇒ 不能用 main_performance_of(order)：缺行即抛 Performance.DoesNotExist ⇒ /submit/ 变 HTTP 500。⚠️ 本处刻意「缺行就拒」⛔ 不补行（与 /assign/ 有意不同，两处口径依据各自写在注释里）：这条路径要求「你就是这张单的接单人」，缺 MAIN 行=没有本地接单人 ⇒ 答案唯一、既有对应出口就是下面那句 403。
        # 补一条 user 为空的行再拒绝是白写、代价还是永久的：B-1 的幂等标记（hall.remote_taken）存在 ⇒ B-1 不会再删它一次 ⇒ 一次失败的提交请求就把业务方要清掉的那条无主行留回来（正是 §15-T23-a 那条「0 元无主行」）。
        perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
        if perf is None or perf.user_id != request.user.id:
            # 资源级授权 ⇒ 传输层语义（403），与 /assign、/hall 的闸门同形。⚠️ 放在来源态判据之前：先确认「这是你的单」再说「这单现在能不能交」；
            # 「没有那一行」与「那一行不是你的」在这条路径上是同一个业务含义（你不是这张单的接单人）⇒ 共用同一个出口、同一句话，⛔ 不新造文案。
            raise PermissionDenied("该单不是你接的，不能提交")
        if order.status != OrderStatus.IN_PROGRESS:
            return api_response(
                data=None,
                msg=f"只有代练中的单可以提交，当前状态：{order.status}",
                code=ErrorCode.BUSINESS,
            )

        actor = getattr(request.user, "username", "") or ""
        with transaction.atomic():
            try:
                apply_transition(
                    order, OrderStatus.PENDING_REVIEW,
                    nodes=nodes_of(request.user), actor=actor,
                )
            except IllegalTransition as exc:
                # 预期失败 ⇒ 业务码。上面那道来源态判据（order.status != IN_PROGRESS）判的是当时那一份状态；apply_transition 还会在写状态之前复读+行锁，而 get_object() 到那次复读之间
                # 可以插进一次并发迁移（清人/送问题单/暂停/另一个并发的 /submit）⇒ 复读比对失败 ⇒ IllegalTransition——这条支路在真并发下可达，写成「纵深」是低估。
                # 显式回滚而不是依赖「此刻还没写过」这个不变量（E-0022）。
                transaction.set_rollback(True)
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            # ⛔ R2（编排方裁定）：/submit/（打手交单）不写任何金额——既不写 bonus_amount、也不写 temp_amount。新口径下「钱只在 COMPLETED 生成」，
            # 而「交单即生效、预计跟随生效」（D-0049 方案 A）的前提已废 ⇒ 这里不再算 bonus（算出来没有任何消费方，留着就是「算完就丢」的死代码）。
            perf.review_status = ReviewStatus.PENDING
            perf.save(update_fields=["review_status", "updated_at"])
            record(
                log_type=ACTION_LOG_TYPES["performance.submit"],
                action="performance.submit",
                actor=actor,
                actor_id=request.user.pk,
                # {接单人} → `target_name`（R11）＝ 交单的代练员（上面已判过就是他）
                target_kind="user",
                target_id=request.user.id,
                target_name=request.user.username,
                order_no=order.order_no,
                # ⚠️ **留痕语义重定（spec §8-5）**：动作模板逐字是
                #    「…的完单，绩效金额：{to_value}」。R2 之后本次**未写任何金额**
                #    ⇒ 显式写 `0.00`（「本次未写金额」），⛔ 不再记那个已被删掉的 `bonus`。
                to_value=Decimal("0.00"),
            )
        return api_response(data=OrderSerializer(order, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="report-issue")
    def report_issue(self, request, order_no=None):
        """报问题单（代练员；业务方裁定）。业务方裁定原文：「更改状态只能从代练中更改为待验收或者问题单，不能随便改动其他状态」——「待验收」那一步走的是已有的 /submit/（order.submit_work，目标写死 PENDING_REVIEW，行为不变），本端点是那句话的另一半：IN_PROGRESS → ISSUE。为什么这是一条受限通道、而不是复用通用 /transition/：通用流转的门是 order.change_status(16)，持它的人可以把任意单改成任意状态；业务方要的约束是三条——「你接的单」「从代练中」「改成问题单」——16 一条都表达不了 ⇒ 把 16 给代练员等于放开全部状态（正是业务方那句要挡的事）。所以本端点把三条约束全部写死：①调用方必须是这张单的接单人（下面的 403）②来源态必须是 IN_PROGRESS（下面的 200+1002）③目标写死 ISSUE——本函数根本不接受 target 参数，请求体里给什么都改不了这个事实。
        为什么挂 order.submit_work(105) 而不是另开一个权限点：业务方裁定「复用 order.submit_work，并把它改名为『提交完单和提交问题单』」（目录改名见 apps/rbac/catalog.py）；⛔ 这个复用只放宽到 ISSUE 这一个目标——状态机那侧的登记在 order_status._TARGET_NODES[OrderStatus.ISSUE]（含「谁消费该节点」的实测清单与「不构成提权」的论证），本端点又把来源态写死 ⇒ 持 105 的人经本端点只能到 ISSUE、经 /submit/ 只能到 PENDING_REVIEW，别的目标态没有第三个写入口。⚠️ 真写复用 transition_order（与 /transition/ 同一条领域路径）：它自带 order.transition 留痕（from/to 都写中文标签）、目标态钩子、状态复读与行锁；⛔ 不要在这里另写一套状态判断、也不要另开一个 record(...)——那会变成第二套判据与第二条留痕（E-0021 的形状），必然与 /transition/ 漂移；⚠️ transition_order 在 dry_run=False 时自己开 transaction.atomic()（apply_transition 的 select_for_update 要求调用方在事务里，见它的 docstring）⇒ 本视图不再包一层，与 /transition/ 视图逐条相同。"""
        order = self.get_object()
        # ⚠️ 与 /submit/ 同一个取行方式：容错查询（filter(...).first()），缺 MAIN 行=「没有本地接单人」⇒ 与「那一行不是你的」走同一个 403 出口。
        # ⛔ 不补行：/submit/ 那段注释里的完整理由同样适用——B-1 的幂等标记（hall.remote_taken）在 ⇒ 一次失败的请求会把业务方要清掉的无主行永久留回来。
        perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
        if perf is None or perf.user_id != request.user.id:
            # 资源级授权 ⇒ 传输层语义（403），与 `/submit/`、`/assign/`、`/hall/` 的闸门同形。
            # ⚠️ 放在来源态判据**之前**：先确认「这是你的单」，再说「这单现在能不能报问题单」。
            #    （顺序反了的话，别人能靠错误文案探出「这张单现在是什么状态」。）
            raise PermissionDenied("该单不是你接的，不能提交问题单")
        if order.status != OrderStatus.IN_PROGRESS:
            # ⚠️ 文案里的状态值口径与 `/submit/` 那一句**逐字同源**（`order.status` 原样，
            #    ⛔ 不取 `.label`）：两条受限通道的报错必须能被同一个前端映射读懂。
            return api_response(
                data=None,
                msg=f"只有代练中的单可以报问题单，当前状态：{order.status}",
                code=ErrorCode.BUSINESS,
            )
        reason = OrderTransitionReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        remark = reason.validated_data["reason"]
        if not remark:
            return api_response(msg=ISSUE_REASON_REQUIRED_MSG, code=ErrorCode.VALIDATION)
        outcome = transition_order(
            order,
            target=OrderStatus.ISSUE,
            remark=remark,
            # ⚠️ `validate_actor=False`：本端点不读、也不校验请求体的 `actor`（与 `/transition/`
            #    同口径）。用默认值会凭空多出一条 1000 分支：超长 `actor` 从「被忽略」
            #    变成「请求非法」。
            ctx=self._write_context(request, validate_actor=False),
            dry_run=False,
        )
        if not outcome.ok:
            # 预期失败（并发下状态已被别人改走 ⇒ `apply_transition` 的复读抛
            # `IllegalTransition`）⇒ 业务码，与 `/transition/` 同一出口。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        return api_response(
            data=OrderSerializer(outcome.detail["order"], context={"request": request}).data
        )

    @action(detail=True, methods=["post"])
    def price(self, request, order_no=None):
        """改价——独立动作，便于单独授权与留痕（spec §6）。入参必须走序列化器：若把请求里的原始值直接赋给 DecimalField 再 save，
        非法值会在写库时抛 Django 的 ValidationError——它不在 P1 异常处理器的业务分支里，结果是 HTML 500；走 DRF 的 ValidationError 才会按约定得到 HTTP 200 + code=1000。"""
        order = self.get_object()
        serializer = PriceChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        outcome = change_price(
            order,
            total_price=serializer.validated_data["total_price"],
            reason=serializer.validated_data.get("reason", ""),
            ctx=self._write_context(request),
            dry_run=False,
        )
        if not outcome.ok:
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        return api_response(
            data=OrderSerializer(outcome.detail["order"], context={"request": request}).data
        )

    @action(detail=False, methods=["post"], url_path="batch/price")
    def batch_price(self, request):
        """批量改接单价（spec §4.1 第 4 行；两段式见 §D2/D3/D4）。⚠️ _write_context(request) 用默认值 validate_actor=True：本端点的单笔对应物 /price/ 走的就是
        _actor(request)（ActorSerializer）⇒ 批量必须同口径（否则批量会新造出一个「请求体 actor 被校验/不被校验」的行为差）。逐笔判据一律来自
        order_writes.change_price（唯一判据出处，§D5）；本动作只做请求级校验（body 序列化器 + parse_order_nos）。"""
        payload = BatchPriceSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        dry_run = payload.validated_data["dry_run"]
        confirmed_effects = payload.validated_data.get("confirmed_effects")
        if not dry_run and len(order_nos) > 1 and confirmed_effects is None:
            return api_response(
                data=None,
                msg="批量改接单价前需要确认订单名单，请刷新管理订单页面后再操作",
                code=ErrorCode.BUSINESS,
            )
        ctx = self._write_context(request)
        total_price = payload.validated_data["total_price"]
        reason = payload.validated_data.get("reason", "")
        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=dry_run,
            per_order=lambda order, ctx, dry_run: change_price(
                order, total_price=total_price, reason=reason, ctx=ctx, dry_run=dry_run
            ),
            ctx=ctx,
            confirmed_effects=confirmed_effects,
            before_lock=lambda rows: list(
                Order.objects.select_for_update()
                .filter(
                    split_id__in={
                        row.split_id
                        for row in rows
                        if row.split_id and row.split_kind in ("", "LEGACY", "PEER")
                    }
                )
                .order_by("id")
                .values_list("id", flat=True)
            ),
        )

    @action(detail=False, methods=["post"], url_path="batch/payment-status")
    def batch_payment_status(self, request):
        """批量改收款状态（spec §4.1 第 6 行；两段式见 §D2/D3/D4）。⚠️ _write_context(request) 用默认值 validate_actor=True：单笔对应物 /payment-status/ 走的就是
        _actor(request)（ActorSerializer）⇒ 批量必须同口径（见 _write_context 的说明；⛔ 不要因为「批量前端还没接」就省掉这一格）。逐笔判据一律来自
        order_writes.change_payment_status（唯一判据出处，§D5）；本动作只做请求级校验（body 序列化器 + parse_order_nos）。"""
        payload = BatchPaymentStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        ctx = self._write_context(request)
        payment_status = payload.validated_data["payment_status"]
        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            per_order=lambda order, ctx, dry_run: change_payment_status(
                order, payment_status=payment_status, ctx=ctx, dry_run=dry_run
            ),
            ctx=ctx,
        )

    @action(detail=False, methods=["post"], url_path="xgj-receipt-sync")
    def xgj_receipt_sync(self, request):
        """手动「同步收款」：按录单时间范围查上家收款，已结账的未收款单改成已结款。⭐ D-0359 起按顺序查三条上家通道：闲管家（原样转调）→鸡蛋铺①式→团团通上家（peer）——只有「已结账」一个方向会写（单向闩）；夜间定时（sync_upstream_receipts）只跑①式+peer（计划 docs/plans/2026-10-26-receipt-sync-button.md §2-3、§2.5）。三条顺序承重：①入参过序列化器（两个 DateField + validate() 的 ≤31 天判据）⇒ 非法 200+1000；②MAX_ORDERS_PER_RUN 上限（5000，原 500）用同一个 candidates_qs() 的 count() 判（与执行循环同源，计划 B2）——超限不跑、回 1000「请缩小时间范围」、零上游调用（闲管家按次计费）；③重入探测：请求线程用自己的连接 pg_try_advisory_lock 试拿即放，拿不到 ⇒ 1037（另一轮正在跑）；拿到之后才起线程——线程体自己还会再拿一次真锁（run_upstream_receipt_sync 复用同一个 _sync_guard namespace），极小竞态下只有真锁赢家跑（计划 §2-4）。
        ⚠️ 请求路径不出网：本动作只做校验+数行+起 daemon 线程，随后立刻 {"accepted": true}（PROD-02）。
        ⚠️ 不写库：真正的写（改收款状态+留痕）在领域层、走既有 change_payment_status ⇒ 本动作不记操作日志（登记在 tests/test_operation_log_ledger.py 的 DELIBERATELY_NOT_LOGGED）。"""
        payload = XgjReceiptSyncSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        date_from = payload.validated_data["date_from"]
        date_to = payload.validated_data["date_to"]

        total = xgj_receipt_sync.count_candidates(date_from, date_to)
        if total > xgj_receipt_sync.MAX_ORDERS_PER_RUN:
            return api_response(
                data=None,
                msg=(
                    f"本次范围内有 {total} 笔未收款单，超过单次上限 "
                    f"{xgj_receipt_sync.MAX_ORDERS_PER_RUN} 笔，请缩小时间范围"
                ),
                code=ErrorCode.VALIDATION,
            )

        if xgj_receipt_sync.sync_is_running():
            return api_response(
                data=None,
                msg="同步收款正在运行，请稍后再试",
                code=ErrorCode.XGJ_RECEIPT_SYNC_BUSY,
            )

        # ⭐ D-0359：线程体从 xgj_receipt_sync.start_xgj_receipt_sync 换成编排器 upstream_receipt_sync.start_upstream_receipt_sync——手动跑含闲管家通道（编排器内部转调 run_xgj_receipt_sync 原样）+①式+peer；
        # 本端点的 body/权限码/busy 码 1037/预算预检（仍只算闲管家候选，其余通道各自在运行内自封顶）全部不变。
        # 锁是同一个 namespace（0x58474A52），编排器与旧实现互斥不变。
        upstream_receipt_sync.start_upstream_receipt_sync(
            date_from=date_from, date_to=date_to,
        )
        return api_response(data={"accepted": True})

    @action(detail=False, methods=["post"], url_path="batch/types")
    def batch_types(self, request):
        """批量改订单类型（spec §4.1 第 3 行/§9-R7）。⚠️ 只传 order_type 一个键给核心：update_order_fields 读 validated 里多个键（game/project_text/server/category/project/order_source …），把 payload.validated_data 整个塞进去会把 order_nos/dry_run 也带进去（setattr(order, "dry_run", True) 这种字段不存在 ⇒ save() 报错；即便存在也是批量成了第二条写通道，§9-R7 明禁）；⛔ 不要「顺手」把 validated_data 直接传下去。⚠️ _write_context(request) 用默认值 validate_actor=True：与单笔对应物 PATCH /orders/<单号>/（partial_update）同口径。
        ⚠️ ValidationProblem 的翻码：update_order_fields 会跑 validate_required/validate_order_chain/assert_game_allowed，它们原样向上抛 ValidationProblem（spec §D5 明写「翻成业务码是视图的事」）；单笔那边 partial_update 是 except ValidationProblem → api_response(code=problem.code)，批量这边每一笔都是「这一笔能不能做」的答案 ⇒ 翻成逐笔 _fail(problem.code, str(problem))（run_batch 的 per_order 契约就是 WriteOutcome，它抛出去会变成 500）；⛔ 这不是第二套判据：用的是同一个 problem.code 与 str(problem)。"""
        payload = BatchOrderTypeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        ctx = self._write_context(request)
        order_type = payload.validated_data["order_type"]

        def per_order(order, ctx, dry_run):
            try:
                return update_order_fields(
                    order,
                    validated={"order_type": order_type},
                    request_user=request.user,
                    ctx=ctx,
                    dry_run=dry_run,
                )
            except ValidationProblem as problem:
                return _fail(problem.code, str(problem))

        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            per_order=per_order,
            ctx=ctx,
        )

    @action(detail=False, methods=["post"], url_path="batch/hall-price")
    def batch_hall_price(self, request):
        """批量改发单价（spec §4.1 第 5 行/§4.2/§4.3）。⚠️ _write_context(request) 用默认值 validate_actor=True：与单笔对应物 /hall-price/ 同口径。
        ⚠️ price 的解析器要跑两次、这是刻意的（spec §7-T4 第 2 条）：①请求级一次（data=request.data）——与某一笔无关的判据在这里给出 1000（缺 price/空串、% 是否超上限、直写金额是否过金额闸），与单笔端点的请求级行为对齐；②逐笔再一次（_hall_price_of，带该笔自己的 total_price）——% 档的分母逐笔不同、换算必须逐笔做。
        ⚠️ 金额闸因此横跨两次（原 docstring 把「金额闸」整条列进请求级、与实现不符）：「直写金额」那一半是请求级（①），「折算后金额」那一半是逐笔的（②）——同一份请求可以对 A 笔合格、对 B 笔因折算结果撑爆 12 位而不合格（B 进 ineligible，判据同 HallPriceChangeSerializer.validate() 的第 4 步；用例 test_per_order_failure_carries_the_flattened_message）。⛔ 不要合并成一次：合并就只能取一个分母（错），或把请求级闸门推到逐笔（那会让「发单价格式错误」从整批 1000 变成每一笔各一条 ineligible）；⛔ 也不要把折算后的金额闸挪到请求级去判（那要求一个「这批单共用的分母」，各笔 total_price 不同 ⇒ 会造出第二套、错的口径）。"""
        payload = BatchHallPriceSerializer(
            data=request.data, context={"order": _HALL_PRICE_PARSE_ORDER}
        )
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        ctx = self._write_context(request)
        raw_price = str(request.data["price"])

        def per_order(order, ctx, dry_run):
            price, reason, issue = _hall_price_of(order, raw_price)
            if issue is not None:
                return _fail(issue["code"], issue["msg"])
            return change_hall_price(
                order, price=price, reason=reason, ctx=ctx, dry_run=dry_run
            )

        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            per_order=per_order,
            ctx=ctx,
        )

    @action(detail=False, methods=["post"], url_path="batch/delete")
    def batch_delete(self, request):
        return self._run_order_batch_delete(request, BatchDeleteSerializer)

    @action(detail=False, methods=["post"], url_path="batch/delete-v2")
    def batch_delete_v2(self, request):
        return self._run_order_batch_delete(request, V2BatchDeleteSerializer)

    def _run_order_batch_delete(self, request, serializer_class):
        payload = serializer_class(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        dry_run = payload.validated_data["dry_run"]
        reason = payload.validated_data.get("reason", "")
        if (
            serializer_class is V2BatchDeleteSerializer
            and not dry_run and len(order_nos) > 3 and not reason
        ):
            return api_response(
                data=None, msg="删除超过 3 笔订单时，请填写删除原因", code=ErrorCode.VALIDATION
            )

        ctx = self._write_context(request, validate_actor=False)
        is_del_emp = payload.validated_data["isDelEmp"]
        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=dry_run,
            per_order=lambda order, ctx, dry_run: delete_order_write(
                order, is_del_emp=is_del_emp, ctx=ctx, dry_run=dry_run, reason=reason
            ),
            ctx=ctx,
            include_deleted=True,
            confirmed_effects=payload.validated_data.get("confirmed_effects"),
        )

    @action(detail=False, methods=["post"], url_path="batch/transition")
    def batch_transition(self, request):
        """批量改状态——一个端点两种形态（spec §D10/§4.1 第 2 行/§4.2）。target 缺失（或空串）⇒ 只读：调 order_batch.transition_targets 返回这批单可选目标的并集+每笔自己的可选集（⛔ 一行不写、不开事务；那条请求的 dry_run 不参与判断，响应恒 dry_run:True、applied:[]）；有 target ⇒ 标准两段式（§D2/D3/D4），逐笔判据来自 order_writes.transition_order（唯一判据出处 §D5；它已接上「状态机边+节点+已结账回池」三道闸）。
        ⚠️ 为什么要这个读取形态：改状态弹窗今天靠 fetchOrder(单号) 拿 allowed_transitions，勾 N 笔就发 N 发读请求不可接受（每页最多 100 笔），而那份可选集由后端按调用方节点算 ⇒ 前端 ⛔ 不许镜像状态机（spec §D10）。⚠️ 两种形态的节点门不同：读取形态取 order.change_status/order.change_status_completed 的 any 元组，写形态按 target 分流——见 get_permissions() 里那一段（读选项不是写，⛔ 不许因此把写那半放宽）。
        ⚠️ _write_context(request, validate_actor=False)：单笔 /transition/ 不读也不校验请求体的 actor（它被忽略——留痕与 order.updated_by 都取认证态）⇒ 批量必须同口径；读取形态不构造 ctx（它一行不写）。"""
        payload = BatchTransitionSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        target = payload.validated_data.get("target")
        if not target:
            # ⭐ 读取形态：⛔ 不构造 ctx、不开事务、不碰 `dry_run`。
            return transition_targets(request, order_nos)
        ctx = self._write_context(request, validate_actor=False)
        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            # ⭐ D-0337：批量「改状态」的说明原因（可选，形状与单笔同一出处）逐笔透传
            #    ——同一批的每一笔留痕行都带同一条原因（弹窗批量支与单笔支共用同一份表单）。
            per_order=lambda order, ctx, dry_run: transition_order(
                order, target=target, remark=payload.validated_data["reason"], ctx=ctx, dry_run=dry_run
            ),
            ctx=ctx,
        )

    @action(detail=False, methods=["post"], url_path="batch/assign")
    def batch_assign(self, request):
        """批量指派打手（spec §4.1 第 1 行/§4.2 的 assign 行/§4.3）。⚠️ _write_context(request, validate_actor=False)：单笔 /assign/ 不读也不校验请求体的 actor（搬迁前逐字如此，它被忽略——留痕与 order.updated_by 都取认证态）⇒ 批量必须同口径，否则超长/非法的 actor 会从「被忽略」变成多出来的一个 1000。
        ⭐ 请求级 vs 逐单的分工（spec §D5；编排方 Ambiguity #1/#2）——请求级（本动作里、run_batch 之前各做一次）：AssignSerializer、taker 不存在（1000）/已停用（1000）、带 price/fee 时的两道 has_node_now("order.submit_payout_price") 闸（403）；它们只依赖「谁+有没有带这个键」、与具体哪一单无关 ⇒ 失败是整批的；⛔ 两道闸不进核心函数：那样会把 403 变成逐笔 ineligible（授权语义错译成数据不合格）；⚠️ 判据一律 is not None（E-0021：price=0/fee=0 是合法边界，⛔ 不是「没传」）。
        逐单（order_writes.assign_order 里）：游戏范围闸（D-0252，1002，文案带游戏名）、taker==order.sales_user（1002）、MAIN 行已结账（1014）、状态机边不可达（1002）——同一个 taker 对不同单结论可能不同 ⇒ 必须逐笔判（⛔ 不在这里复制一份）。"""
        payload = BatchAssignSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        # ⭐ 「指定的用户不存在 / 已停用」的判据走**唯一实现** `resolve_assign_taker`
        #    （E-0021：两处各写一份必然漂移，⛔ 不在这里内联抄第二份）。
        taker, error = resolve_assign_taker(payload.validated_data["user_id"])
        if error is not None:
            return error
        price = payload.validated_data.get("price")
        fee = payload.validated_data.get("fee")
        hours = payload.validated_data.get("hours")
        project = payload.validated_data.get("project")
        if price is not None and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改接单价的权限")
        if fee is not None and not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有改绩效金额的权限")
        ctx = self._write_context(request, validate_actor=False)

        def _assign_one(order, ctx, dry_run):
            """⭐ 批量与单笔同一套口径（审计 M1）：远端已被代练通接单（12/13/14/15）⇒ 拒（与单笔 /assign/ 同一份判据、同一句话）；未接手 ⇒ 允许「已抛平台的单」改成本地代练中（allow_status_change_while_pushed，与单笔同参数）、真写成功后由 after_apply 立刻撤掉平台单。
            ⚠️ 撤回不能放在这里：run_batch 的 apply 形态整批在一个事务里跑，真实上游写请求不该进事务（与单笔「事务提交后再撤」同一条纪律）。"""
            blocked, taken_msg = self._platform_taken_error(order)
            if blocked:
                return _fail(ErrorCode.BUSINESS, taken_msg)
            return assign_order(
                order,
                taker=taker,
                price=price,
                fee=fee,
                hours=hours,
                project=project,
                chat_follow_up=payload.validated_data.get("chat_follow_up", False),
                ctx=ctx,
                dry_run=dry_run,
                # ⭐ 与单笔 `/assign/` 同口径：未接手的已抛单允许先改本地状态，
                # 随后（本地提交后）立刻把平台单撤掉。
                allow_status_change_while_pushed=True,
            )

        def _withdraw_after_apply(orders):
            """整批提交成功后：逐单撤掉代练通订单（与单笔 /assign/ 同一个原语）。只在真实提交之后调用（run_batch 保证）；
            任何一单撤不掉都不静默，把中文提示（含单号）拼进响应的 msg——局部失败不推翻已经生效的本地指派。"""
            from apps.business.dailiantong_actions import withdraw_for_internal_claim

            messages: list[str] = []
            for order in orders:
                had_push, withdraw_msg = withdraw_for_internal_claim(order)
                if had_push and withdraw_msg:
                    messages.append(f"{order.order_no}：{withdraw_msg}")
            return "；".join(messages)

        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            per_order=_assign_one,
            ctx=ctx,
            # 预检（dry_run）**不撤回**：一行都没写，不该动平台单。
            after_apply=(
                None if payload.validated_data["dry_run"] else _withdraw_after_apply
            ),
        )

    @action(detail=False, methods=["post"], url_path="batch/refunds")
    def batch_refunds(self, request):
        """批量设置累计退款额——每笔各设成同一个累计额、幂等键逐笔（spec §4.1 第 7 行/§D13）。⚠️ _write_context(request, validate_actor=False)：单笔 /refunds 不读也不校验请求体的 actor（RefundRequestSerializer 里没有这个字段）⇒ 批量必须同口径；⚠️ 逐笔判据一律来自 order_writes.refund_total_write（它转调 refunds.refund_total_denial / assert_sales_commissions_configured，同一批抽出的判据函数）⇒ 本动作只负责把该笔自己的 request_id 递进去，⛔ 不在这里重写 COMPLETED/金额/提成率那三条闸。
        ⭐ key_by_order_no 就是「按下标对齐」的实现（spec §D9/§D13）：order_nos[k] 配 request_ids[k]；长度不等已在校验里挡成 1000，所以 zip(..., strict=True) 在这里不是装饰——它让「两条信息不同长」响亮失败而不是静默截断（静默截断的后果是「某些单拿到别人的键」=错账方向，而那不会有任何测试自己冒出来）；⚠️ 两个列表都取自本次请求：apply 时前端只送合格子集（§D3）及其对应的键 ⇒ ⛔ 不要在服务端把它们对齐到某个「全集」（那会把子集的键错配到全集的下标上）。
        ⚠️ dry_run 判不出 1010（request_id 重复）是已登记的取舍（spec §9-R9）：那条事实由数据库具名唯一约束在 INSERT 时回答，而 dry_run 不 INSERT ⇒ 撞不到；⛔ 不为此加 exists() 预查（那会让「去掉唯一约束 ⇒ 测试必须变红」的判别力实验失效）。"""
        payload = BatchRefundSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        ctx = self._write_context(request, validate_actor=False)
        refund_total = payload.validated_data["refund_total"]
        reason = payload.validated_data["reason"]
        key_by_order_no = dict(
            zip(order_nos, payload.validated_data["request_ids"], strict=True)
        )
        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            per_order=lambda order, ctx, dry_run: refund_total_write(
                order, refund_total=refund_total, reason=reason,
                request_id=key_by_order_no[order.order_no], ctx=ctx, dry_run=dry_run,
            ),
            ctx=ctx,
        )

    @action(detail=False, methods=["post"], url_path="batch/recompute-commission")
    def batch_recompute_commission(self, request):
        """批量重算客服销售提成（spec §4.1 第 9 行/§D15）。⚠️ 本端点没有单笔对应物（spec §D15 明写「本仓没有这个端点，也没有这个函数」）⇒ 三条口径都不是照搬其它批量动作推出来的：节点门=ORDER_PERMISSIONS["batch_recompute_commission"]=("finance.change_settlement_status",)——业务方裁定 Q1=候选 B；_write_context(request, validate_actor=False)——既然没有单笔对应物，就没有「单笔读不读请求体 actor」可比对 ⇒ 取不读也不校验那一档（actor 与 log_actor 同取认证态；⛔ 不要因为「别的批量动作都带 actor」就顺手改成默认值——那会凭空多出一条 1000 分支，而它对业务没有任何意义）；body=BatchRecomputeSerializer 只有 order_nos+dry_run 两个键（这颗按钮不带业务值）。⚠️ 逐笔判据一律来自 order_writes.recompute_customer_commission_write（唯一判据出处，§D5：无归属人 1002/已结账 1014/缺行补建/已完成写金额/未完成只补空行/算不出提成率 1000）⇒ ⛔ 本动作里不重写任何一条；⚠️ dry_run 的判定面只含「纯内存/纯读」的闸、不试算提成金额（spec §4.3 的零副作用；与 §9-R8~R10 同一口径）——那三条残差登记在 spec §9。"""
        payload = BatchRecomputeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        order_nos, error = parse_order_nos(request.data.get("order_nos"))
        if error:
            return api_response(data=None, msg=error, code=ErrorCode.VALIDATION)
        ctx = self._write_context(request, validate_actor=False)
        return run_batch(
            request,
            order_nos=order_nos,
            dry_run=payload.validated_data["dry_run"],
            per_order=lambda order, ctx, dry_run: recompute_customer_commission_write(
                order, ctx=ctx, dry_run=dry_run
            ),
            ctx=ctx,
        )

    @action(detail=True, methods=["post"], url_path="payment-status")
    def payment_status(self, request, order_no=None):
        """改收款状态——纯人工标记三档、不联动退款/退单/结账（业务方裁定）。与 /price/ 同形的四件事：入参走序列化器（非法值 ⇒ 200+code=1000，不是 HTML 500）；
        写与留痕同事务；留痕的操作人取认证态；新旧同值不留痕但仍 save。"""
        order = self.get_object()
        serializer = PaymentStatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        outcome = change_payment_status(
            order,
            payment_status=serializer.validated_data["payment_status"],
            ctx=self._write_context(request),
            dry_run=False,
        )
        if not outcome.ok:
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        return api_response(
            data=OrderSerializer(outcome.detail["order"], context={"request": request}).data
        )

    @action(detail=True, methods=["post"], url_path="sales-agent")
    def sales_agent(self, request, order_no=None):
        """换这一单的接单销售客服归属（业务方拍板的新能力，本线 feat/sales-agent）。业务方原话「换销售客服=提成也换人」；落成三件事（都在 order_writes.change_sales_agent 的同一个 transaction.atomic() 里）：①Order.sales_user=目标用户（决定客服提成归谁的 FK）②该单 SALES 绩效行 user 改成目标用户、行不存在就补建 ③Order.sales_agent（文本快照）同步成目标用户的显示名、金额按新人立刻重算（订单未完成⇒按既有口径不写金额）。⚠️ 对标没有这个能力：鸡蛋铺只有订单列表页一颗手动的「重算客服提成」按钮、且不改归属人（它重算的还是同一个人头上的金额）⇒ 这是我方新造的能力（取舍由业务方拍板，决策登记由总指挥收尾统一做）。为什么必须是专用端点（⛔ 不是放开通用 PATCH）：sales_user 在 OrderSerializer/OrderUpdateSerializer 的 read_only_fields 里（两条注释写明「决定客服提成的唯一归属，只能由视图按认证态写入」）；通用 PATCH 改归属会让「订单归属人」与「SALES 绩效行的 user」不一致 ⇒ 提成付给错的人且没有任何报错（bonus_amount 可能已结账）——所以「钱」这件事只走这一条能单独授权、单独留痕的路（与 /price/ 同一个理由）。
        四条判据（顺序承重，每条都有判别用例）：①该单 SALES 行已结账⇒拒绝（业务码 1014，文案与 order_writes.recompute_customer_commission_write 那道闸同源同字）——必须在改任何东西之前判（改了 sales_user 再拒会留下半成品）；判据是模型属性 Performance.is_settled（三字段并集）⛔ 不在这里手抄那三个字段（E-0021：同一事实两套判据）；⭐ Z1：这条判据落在本视图显式开的 atomic() 里、那句查询本身就是 select_for_update(of=("self",)) 的加锁读（锁内复查的是读回来的那一份、不是锁前缓存，并发结账的 TOCTOU 因此收口）②目标不在候选里/不存在⇒拒绝（业务码 1000）；候选口径同源 selectors.kefu_candidates_qs（与 GET /orders/kefu-options/ 是同一份判据，见 order_writes.resolve_sales_agent_candidate 的 docstring）③目标在本单已有绩效行（任意 emp_type）⇒拒绝（业务码 1000+指名提示）：Performance 的唯一约束 uniq_performance_order_user 是 (order,user)、不含 emp_type ⇒ 直接改会撞约束抛 IntegrityError ⇒ HTTP 500 ④目标==本单现有归属人⇒视为无事发生（幂等）：成功返回且不写任何东西——不落一次「把 A 改成 A」的假变更留痕（与 /price/ 的 R27.2「同值不留痕」同口径）；判据是一次改动的前置条件、不改任何数据；排在①之后（已结账的单连「确认一下」都不许，判据更严的那条先答）。⚠️ 写与留痕在同一个 atomic() 里（在 change_sales_agent 内）：留痕动作码 order.change_sales_agent，from_value/to_value 是显示名（「从谁改到谁」看得见，⛔ 不是一句「已更新」）；⭐ Z1：本视图自己再包一层 transaction.atomic()——①的加锁复查必须在真事务里（select_for_update 在 autocommit 下会 500）；②/③的拒绝也在这一层里显式 transaction.set_rollback(True) 再 return（E-0022：预期失败不裸退；虽然它们排在 change_sales_agent 的任何写入之前，但「代码顺序恰好没写」不是结构保证）。"""
        order = self.get_object()
        payload = SalesAgentChangeSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        target_id = payload.validated_data["user"]
        # ⭐⭐ Z1：① 是判定、下面是写入，原先两段之间既没有事务也没有锁 ⇒ 并发结账能把这条 SALES 行结掉、这边照换归属（TOCTOU）。
        # 本视图原先自己不开事务（change_sales_agent 内部才开）⇒ 加锁必须落在这里显式开的 atomic() 里，否则 select_for_update 在 autocommit 下会 500（I1③）。
        # 锁的形状与其余五处同源：对 SALES 绩效行 select_for_update(of=("self",)) 重取并复查 is_settled；被拒时 set_rollback(True) 再 return（E-0022）。
        with transaction.atomic():
            # ⭐⭐ I4：先锁订单行、再锁绩效行（与 /assign/ 同向）。本支下面 change_sales_agent 也会锁订单行，原先订单行锁排在这条 SALES 绩效行锁之后
            # ⇒ 与 /assign/（apply_transition 先锁 Order）方向相反、两笔并发会 AB-BA 成环。锁形状的唯一出处 = order_writes.lock_order_row。
            lock_order_row(order)
            # ① 已结账（判据=模型属性 Performance.is_settled；文案与 recompute_customer_commission_write 那道闸同源同字）。在任何写入之前判。
            # ⚠️ order__deleted_at__isnull=True 与另外两处 SALES 行查询逐字同源（order_writes.recompute_customer_commission_write / settlement_hooks.recompute_customer_commission）：已软删单的客服行不算提成。
            # ⚠️ .order_by("id")：单行也用同一条形状（锁序不漂）；of=("self",)：不加它会把 join 上的 business_order 行一起锁住（那时与 change_sales_agent 里那句 Order 锁的先后就取决于执行顺序，锁序不可读）。
            customer = (
                Performance.objects.select_for_update(of=("self",))
                .filter(
                    order=order, emp_type=EmpType.SALES,
                    order__deleted_at__isnull=True,
                )
                .order_by("id")
                .first()
            )
            if customer is not None and customer.is_settled:
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg="该单的客服提成已结账，不能重算；请先撤销结账",
                    code=ErrorCode.PERFORMANCE_STATE_INVALID,
                )
            # ⚠️ 审计前值取加锁后读回的那一份（I1）：order 是 get_object() 的实例，并发换过归属时它的 sales_user/sales_agent 已是旧值 ⇒ change_sales_agent 会拿它算 from_value 与 old_user（会写一句假话、也可能把 A→A 判成换人）。
            # 下面那次 change_sales_agent 自己会锁订单行；这里在它之前先读一次真值，两列是同一个事实的两面（归属+显示名快照）。
            order.refresh_from_db(fields=["sales_user", "sales_agent"])

            # ④ 幂等：目标就是现在的归属人 ⇒ 不改归属、不搬绩效行、不写假变更留痕。放在②/③之前是刻意的——这一档不读候选（绩效行上面刚读过），「他今天已经不在候选里了」不该把一次无操作判成失败。
            # ⭐ 这一档不是「什么都不做」：名字快照可能已被改脏（Order.sales_agent != 归属人的显示名，例如旧版 PATCH 单独改过它）——那正是「列表显示乙、提成归甲」的现场 ⇒ 先把名字对齐回归属人（不变式修复）再成功返回；⚠️ 仍然不写留痕：order.change_sales_agent 的语义是「把甲换成乙」，这一档没有发生变更（与 /price/ R27.2 同口径）——对齐不是一次换人，那一行照旧不写（⛔ 别为了「看得见修好了」加一行日志：它会说一句没发生的事）。
            # ⚠️ 对齐用的显示名不经过候选口径（resolve_sales_agent_candidate）：那一档会判「他在不在候选里」，而这里只是把已经在任的那个人的名字写回去（他今天可能已不在候选里，那不是把他换掉的理由）。
            if order.sales_user_id is not None and order.sales_user_id == target_id:
                sync_sales_agent_name(
                    order,
                    actor=self._write_context(request, validate_actor=False).actor,
                )
                data = OrderSerializer(order, context={"request": request}).data
            else:
                # ② / ③：候选范围与「已有绩效行」两条闸（判据在领域层，⛔ 不在这里重写）。
                target, target_name, error = resolve_sales_agent_candidate(
                    order=order, user_id=target_id, viewer=request.user
                )
                if error is not None:
                    transaction.set_rollback(True)
                    return api_response(data=None, msg=error.msg, code=error.code)

                outcome = change_sales_agent(
                    order,
                    target=target,
                    target_name=target_name,
                    ctx=self._write_context(request, validate_actor=False),
                    dry_run=False,
                )
                if not outcome.ok:
                    # `change_sales_agent` 已在它自己的内层 savepoint 里回滚过；这里再标记外层
                    # 回滚是 E-0022 的同一条纪律（预期失败不许裸退出补偿路径）。
                    transaction.set_rollback(True)
                    return api_response(
                        data=None, msg=outcome.msg, code=outcome.code
                    )
                data = OrderSerializer(
                    outcome.detail["order"], context={"request": request}
                ).data
        return api_response(data=data)

    @action(detail=True, methods=["post"], url_path="hall-price")
    def hall_price(self, request, order_no=None):
        """改发单价——写 Order.hall_price，不动订单状态。写完价后，该单有 MAIN 行、user 非空且提成可算时，同步重算 Performance.temp_amount（边界与跳过档见动作体注释）。⭐ F1/R-6：①订单当前状态是「已完成」时，那次重算会把 Performance.bonus_amount 用同一算式、同一有效基价一起写（共用的 performance.recompute_order_amounts）；②该单 MAIN 绩效行已结账 ⇒ 本端点一律拒绝（业务码 1014、一行不写），与其他两条写价路径同一道闸。入参经 HallPriceChangeSerializer（% 与金额两支都在那一处解析与过闸）；写与留痕同事务；新值==旧值时不留痕但仍 save（同 /price/ 的 R27.2 口径）。
        ⚠️ 比例档的分母是 Order.total_price（页面上「接单价」那一列）、不是 hall_price——「接单价/发单价」最容易抄反的地方（spec §5.2）；换算与舍入复用 performance_amount.commission_amount，本视图不再写第二套 ×pct÷100。⚠️ 对标的 action 三值（30/40/50）只决定客户端入口与文案，服务端是同一个动作 ⇒ 不引入一个没有服务端分支的参数（spec §5.2，登记为形状差异）。"""
        order = self.get_object()
        serializer = HallPriceChangeSerializer(
            data=request.data, context={"order": order}
        )
        serializer.is_valid(raise_exception=True)
        outcome = change_hall_price(
            order,
            price=serializer.validated_data["price"],
            reason=serializer.validated_data["reason"],
            ctx=self._write_context(request),
            dry_run=False,
        )
        if not outcome.ok:
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        return api_response(
            data=OrderSerializer(outcome.detail["order"], context={"request": request}).data
        )

    # ── 复制成新订单（spec §8，管理订单页右键菜单）──
    #: 复制时要搬过去的字段（spec §8.2 的「复制」行）。不含：hall_price（业务方「不能带发单价」——新单这一列是空的，发大厅时再设）；refund_fee（新单没有退款记录，照抄旧值=假账）；order_no（新生成）、status（恒 UNASSIGNED）、sales_user（单独取原单值，见动作体）；审计列（created_by 取认证态操作人、updated_by/时间戳模型自己写）。FK 一律用 <名>_id（只搬引用不重查行；也避免触碰被 PROTECT 的配置表）。
    #: ⭐ split_id 刻意不在本表（写入点回查，E-0027）：复制出来的是新的独立单（不是拆单组成员）⇒ 走模型默认空串「不是拆单」；判据 tests/test_order_split.py::SplitCopyAndRecycleTests（同批回查的软删/恢复两条：组 ID 原样保留、已软删成员不算进 split_members——同一用例钉住）。
    COPY_FIELDS = (
        "game_id", "server_id", "category_id", "project_id", "order_source_id",
        "project_text", "content", "demand", "time_requirement",
        "game_account", "game_sub_account", "game_role_name",
        "current_info_1", "current_info_2",
        "customer_phone", "customer_qq", "customer_ww",
        "order_type", "payment_method", "source", "source_order_no",
        "sales_agent", "tag", "remark", "is_urgent",
        "total_price", "received_price", "platform_price", "fee",
    )

    def copy(self, request, order_no=None):
        """复制成新订单（spec §8）：内容照搬、状态回初始、发单价不带、提成归原单销售。口径逐条见 spec §8.2，四条例外都是业务方裁定（不要"改进"）：新单恒 UNASSIGNED；hall_price 不复制（「不能带发单价」）；refund_fee 不复制（新单没有退款记录，照搬=假账）；sales_user 照抄原单（提成归原单的销售客服），原单为空⇒新单也为空 ⛔ 不回落到操作人——那会静默改变提成归属（算错钱的方向）。created_by=认证态操作人（与 create 同源：request.user.username，⛔ 不是请求体的 actor——那会让 order.restrict_to_self_created 按客户端可伪造的值过滤）。
        ⚠️ 复用 create 的两道校验（validate_required / validate_order_chain）：复制出来的单也必须是合法单——原单合法⇒正常不会失败，但不要假设它一定通过，仍走同一条「翻译成业务码」的路（E-0010 的形状：预期失败不许 500）；⚠️ 绩效行不复制老单的：走 create_performance_for_order(新单)，客服行按新单的 sales_user 建——老单在 COMPLETED 时可能已有 bonus_amount（甚至已结账），照搬过来就是假账。
        ⚠️ 事务边界：建单/建绩效/写日志在同一个 atomic() 里（照 create 的结构）；块内可预期失败（提成不可算）先 transaction.set_rollback(True) 再 return（E-0022：atomic() 是事务不是回滚点，裸 return 会提交半笔写入）；真正未预期的错误仍按 E-0010 响亮地 500、不伪装成业务结果。⚠️ 留痕必须在绩效建成之后（与 create 同形）：绩效没建成就回滚 → 不能留下「订单已复制、日志说已复制」的行（E-0063 那类「动作没做成却写了成功日志」的方向）。"""
        source = self.get_object()
        try:
            validate_required(game=source.game, project_text=source.project_text)
            validate_order_chain(
                game=source.game,
                server=source.server,
                category=source.category,
                project=source.project,
            )
            # 写侧准入（顺序理由见 create）：复制出来的新单继承原单的游戏（COPY_FIELDS 里搬 game_id，请求体选不了游戏）⇒ 判的就是原单的游戏。
            # ⚠️ 本调用点对非管理员不做功：能看见原单 ⇔ 原单游戏在我名下（get_object() 过同一条 queryset）。它真正做功的路径是「读侧过了 scope_queryset 的管理员旁路、
            # 写侧 assert_game_allowed 没有旁路」——租户管理员能看见原单、但 games_of(admin) 可能是空集。该不对称是既定接口（不另造判据），边界如实写进报告、不在代码里替它做决定。
            assert_game_allowed(request.user, source.game)
        except ValidationProblem as problem:
            return api_response(data=None, msg=str(problem), code=problem.code)
        with transaction.atomic():
            # ⭐ D-0144（A3，**写侧**）：新建的单 `deleted_at` 天然为 `NULL` ⇒ 这里一行不改，
            #    也不需要条件。起点 `source` 来自 `get_object()`（默认 manager）⇒ 已删单到不了这里。
            new = Order.objects.create(
                order_no=generate_order_no(),
                status=OrderStatus.UNASSIGNED,
                # 录单人 = 当前操作人；提成归属 = **原单的**销售（业务方裁定，spec §8.2）。
                created_by=request.user.username,
                sales_user=source.sales_user,
                **{field: getattr(source, field) for field in self.COPY_FIELDS},
            )
            # ⚠️ 与 create 的那处 except 同形（见那里的说明）：本调用当前也会建客服行，而客服行不读提成率 ⇒ CommissionError/AmountError 目前同样没有可触发路径；
            # 保留这个翻译点是最小改动（将来录单/复制重新依赖薪酬配置时，这里是唯一的落点）。不可预期的错误不在这里接。
            try:
                create_performance_for_order(new)
            except (CommissionError, _AmountError) as exc:
                transaction.set_rollback(True)
                return api_response(data=None, msg=exc.message, code=exc.code)
            record(
                log_type=ACTION_LOG_TYPES["order.copy"],
                action="order.copy",
                # 留痕的操作人取**认证态**（R20），不是可伪造的请求体值。
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind="order",
                target_id=new.order_no,
                order_no=new.order_no,
                source_order_no=new.source_order_no,
                from_value=source.order_no,
                reason=source.source_order_no or source.order_no,
            )
        return api_response(data=OrderSerializer(new, context={"request": request}).data)

    # ── 「继续拆分订单」（订单详情页）：拆单落子单时要照抄的列（<名>_id 形式，只搬引用、不重查行）。
    # 不含：order_no（新生成）、status（恒 UNASSIGNED）、split_id（显式给组 ID）、project_text（每个子单不同）、created_by（认证态操作人）、sales_user（显式取当前单）、全部价格列（由 SPLIT_MEMBER_ZERO_PRICES 显式置 0）与审计列。
    # ⛔ 与 copy 的 COPY_FIELDS 刻意分开：那份是「复制成新单」的业务口径（不复发单价/不复退款额），本线是「拆单」的另一套口径（价格全 0）——合并成一张表会让改一处口径时另一处跟着变（两件事、两个判据）。
    SPLIT_CHILD_FIELDS = (
        "game_id", "server_id", "category_id", "project_id", "order_source_id",
        "content", "demand", "time_requirement",
        "game_account", "game_sub_account", "game_role_name",
        "current_info_1", "current_info_2",
        "customer_id", "customer_kind", "customer_kind_is_manual",
        "customer_phone", "customer_qq", "customer_ww",
        "order_type", "payment_method", "source", "source_order_no",
        "sales_agent", "tag", "remark", "is_urgent",
    )

    @action(detail=True, methods=["post"])
    def split(self, request, order_no=None):
        """继续拆分订单（业务方裁定「详情页也要能做」）。⚠️ 这是推断级设计、不是复刻（AGENTS.md §5-E 的三级标注）：对标那个入口（ViewSplitOrder/详情页那颗 .spliteorder）在 .tools/recon 下零抓件，docs/jidanpu-endpoints.json 的 406 条端点里 path 含 split 的也是 0 条 ⇒ 入参、返回与交互都是本线自定的（spec §2.3 入口 2/§2.6 待决项 2 已登记）。语义（逐条都是判据）：当前这单成为组内第一笔（显示层主单）：project_text=projects[0]、价格列一律原样保留（⛔ 一个都不动）；projects[1:] 各落一笔新单：资料照抄当前单、价格全 0（SPLIT_MEMBER_ZERO_PRICES，与录单那条入口同一份口径）；全部共享同一个 split_id、已在组里⇒落进同一组（取 order.split_id 现值，⛔ 不产生第二个组：连拆两次仍然只有一个组）；新单 status 恒 UNASSIGNED（照 copy 的口径：新单就是新单，⛔ 不继承当前单的状态——它没人接、也没做过活）；新单的提成归属（sales_user）/名字快照（sales_agent）照抄当前单（「资料一样」；⛔ 不回落成操作人——那会静默改变提成归属，与 copy 同一条）。
        拒绝路径（全部业务码+一句人话，⛔ 不是 500）：项目数<2/有空项⇒SplitOrderSerializer 回 1000（在建单之前）；订单不存在或已软删⇒get_object() 走 404 ⇒ 全局异常处理器翻成 200+1002+「记录不存在或已被删除」（与详情页同一条既有口径）；订单已完成⇒1000+一句人话（⛔ 一行都不写）——只拒这一档状态、其余状态照拆（竞态残差如实登记：本闸读的是 get_object() 拿到的实例，与随后的写入之间没有行锁 ⇒ 极端并发下可能拆到一张刚变完成的单）。事务与留痕：「改当前单+落 N 笔新单+逐笔绩效+留痕」在同一个 transaction.atomic() 里 ⇒ 任一笔失败⇒一笔都不落（可预期失败先 transaction.set_rollback(True) 再 return，E-0022）；留痕：当前单的项目名变更记一条 order.update（field_name="project_text"），每笔新单记一条 order.create（与录单同一个动作词：它就是「新增了一单」）；⛔ 不新造动作码、⛔ 不新增权限节点（节点门=本动作的 order.add_edit，ORDER_PERMISSIONS["split"]，与录单同一条）。
        出网事件一处都不推、与录单/复制同口径（§7.2.1 裁决 2「建单不是状态变化」；全仓也没有「建单」类事件——出网的订单事件恰好六种（peer_dispatch.py 的模块 docstring 逐条点名）：order.assign/order.grab/order.withdraw/order.update/order.screenshot.add/order.screenshot.delete；而 order.update 那条的口径是「状态+发单价」、改项目名不在里面）；⚠️ 这条不是"忘了接"：它登记在 tests/test_openapi_event_sites_net.py 的 EVENT_SITES（OrderViewSet.split/有意不推，条目里写着四点依据），并由同文件的 test_the_split_create_site_still_does_not_push 钉住「这一处的作用域里没有 enqueue*/notify*/queue* 调用 ⇒ 哪天有人在这里补了入队，那条登记与判据一起红」。"""
        params = SplitOrderSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        projects = params.validated_data["projects"]
        return self.split_local_order(
            self.get_object(), projects=projects, operator=request.user.get_username(),
            actor_id=request.user.pk, assert_game=lambda game: assert_game_allowed(request.user, game),
        )

    @staticmethod
    def split_local_order(order, *, projects, operator, actor_id, assert_game, check_orders=None):
        if order.split_kind == "PEER":
            return api_response(
                data=None, msg="并列拆单组请使用新版拆分操作", code=ErrorCode.VALIDATION
            )
        if str(order.status) == str(OrderStatus.COMPLETED):
            return api_response(
                data=None,
                msg="已完成的订单不能再拆分",
                code=ErrorCode.VALIDATION,
            )
        # 写侧准入（与 create/copy 同一条既有纪律）：子单继承的是当前单的游戏，判的就是它。对非管理员这条恒真（能看见这张单 ⇔ 这款游戏在他名下）；
        # 它真正做功的是「读侧过了 scope_queryset 的管理员旁路、写侧没有旁路」那一档（理由与 copy 的注释逐字相同）。
        # 已在组里 ⇒ 落进**同一组**（⛔ 不产生第二个组）；否则新开一个组 ID
        # （形状复用 `generate_order_no()`，与录单那条入口同源）。
        with transaction.atomic():
            if order.split_id:
                members = list(split_group.active_members(order.split_id).select_for_update())
                order = next((member for member in members if member.pk == order.pk), None)
            else:
                order = lock_order_row(order)
                members = (
                    list(split_group.active_members(order.split_id).select_for_update())
                    if order is not None and order.split_id
                    else []
                )
            if order is None or order.split_kind == split_group.PEER or order.status == OrderStatus.COMPLETED:
                return api_response(data=None, msg="订单已变化，请刷新后重试", code=ErrorCode.VALIDATION)
            if any(member.peer_direct_pending is not None for member in [order, *members]):
                return api_response(msg=PEER_DIRECT_PENDING_MESSAGE, code=ErrorCode.BUSINESS)
            try:
                assert_game(order.game)
            except ValidationProblem as problem:
                return api_response(data=None, msg=str(problem), code=problem.code)
            if check_orders is not None:
                check_orders(members or [order])
            group_id = order.split_id or generate_order_no()
            previous_text = order.project_text
            anchor = members[0] if members else order
            shipping_main_no = anchor.split_ship_main_order_no or anchor.order_no
            shipping_main = split_group.shipping_main_order(anchor)
            shipping_source_no = shipping_main.source_order_no if shipping_main else ""
            # ① 当前单成为组内第一笔：项目名改成 `projects[0]`、盖上组 ID；
            #    ⚠️ **价格列一个都不动**（业务方口径「保证最终接单价不变」）。
            order.project_text = normalize_project_text(projects[0])
            order.split_id = group_id
            order.split_kind = "LEGACY"
            order.split_ship_main_order_no = shipping_main_no
            order.updated_by = operator
            # ⚠️ `update_fields` 必须把 `updated_at` 一起列上（`auto_now` 只在被列出时才写）；
            #    新列漏加就是「值只在内存里、没落库」的静默丢弃（模型上那条注释是踩过的坑）。
            order.save(
                update_fields=["project_text", "split_id", "split_kind", "split_ship_main_order_no", "updated_by", "updated_at"]
            )
            # ⭐ A8/L-6：拆分真的改了本单的接手项目 ⇒ 同事务盖「我方最后一次本地动作时刻」（唯一实现 stamp_local_action；否则本地刚拆完，一条更旧的上家事件会把项目名冲回旧值）。
            # ②里那些新建的子单不盖——新单还没有 PeerOrderLink，写了也是 0 行。
            stamp_local_action(order)
            record(
                log_type=ACTION_LOG_TYPES["order.update"],
                action="order.update",
                actor=operator,
                actor_id=actor_id, system=actor_id is None,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                source_order_no=order.source_order_no,
                field_name="project_text",
                from_value=previous_text,
                to_value=order.project_text,
            )
            # ② 其余各段各落一笔新单：资料照抄当前单、**价格全 0**、状态回初始。
            created = []
            child_fields = {field: getattr(order, field) for field in OrderViewSet.SPLIT_CHILD_FIELDS}
            split_group.lock_child_source_numbers()
            child_source_nos = split_group.child_source_numbers(shipping_source_no, shipping_main_no)
            for segment in projects[1:]:
                child_fields["source_order_no"] = next(child_source_nos)
                child = Order.objects.create(
                    order_no=generate_order_no(),
                    status=OrderStatus.UNASSIGNED,
                    split_id=group_id,
                    split_kind="LEGACY",
                    split_ship_main_order_no=shipping_main_no,
                    project_text=normalize_project_text(segment),
                    created_by=operator,
                    sales_user=order.sales_user,
                    **SPLIT_MEMBER_ZERO_PRICES,
                    **child_fields,
                )
                created.append(child)
                create_performance_for_order(child)
                record(
                    log_type=ACTION_LOG_TYPES["order.create"],
                    action="order.create",
                    actor=operator,
                    actor_id=actor_id, system=actor_id is None,
                    target_kind="order",
                    target_id=child.order_no,
                    order_no=child.order_no,
                    source_order_no=child.source_order_no,
                    # {reason} 用当前单的**展示单号**（⭐ D-0303：来源号非空取它、
                    # 空回退内置号）。人读面才换值；上面的 `order_no` / `target_id` 是寻址键。
                    reason=f"由订单 {order.source_order_no or order.order_no} 拆分生成",
                    to_value=child.hall_price,
                )
        return api_response(
            data={
                "order_no": order.order_no,
                "split_id": group_id,
                # ⚠️ 形状变更（string[]→行[]）：tests/test_order_split.py 与前端 SplitOrderResult.created 同一次改；消费方当前只读 created.length。
                "created": [
                    {
                        "order_no": child.order_no,
                        "source_order_no": child.source_order_no,
                    }
                    for child in created
                ],
            }
        )

