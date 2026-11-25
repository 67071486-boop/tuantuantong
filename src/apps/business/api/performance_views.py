"""绩效只读端点（P3b B1-4：GET /api/business/performances，spec §5）。为什么单独一个模块：它是绩效域的读入口（谁进得来由节点门决定；看得到多少行由数据范围决定，见 PERFORMANCE_MANAGEMENT_NODE）、与 OrderViewSet（订单域、按 order_no 取单）不同；/adjust、/review 也会落在这里。
⚠️ 只读：本 ViewSet 不提供任何写方法。绩效的金额写入只有两条路径——代练员交单（B1-3 的 /submit）与客服改金额（B1-5 的 /adjust）；审核只改状态（B1-6 的 /review）。没有通用 PATCH/PUT/DELETE。"""
import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.paginator import InvalidPage
from django.db import connection, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated

from apps.accounts.display import display_names_for_usernames, user_display_name
from apps.accounts.selectors import settle_forbidden_user_ids
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.business.api.serializers import (
    PerformanceAdjustAmountSerializer,
    PerformanceAdjustSerializer,
    PerformanceListSearchSerializer,
    PerformanceListSerializer,
    PerformanceReviewSerializer,
)
from apps.business.commission import CommissionError
from apps.business.game_scope import game_scope
from apps.business import jidanpu_assignment_lock
from apps.business.models import (
    EmpType,
    OrderStatus,
    PaymentStatus,
    Performance,
    ReviewStatus,
    SettleStatus,
)
from apps.business.order_status import IllegalTransition, _CLEAR_TAKER, apply_transition
#: ⭐ 本批（D-0142）：本视图两个金额列（temp_amount / bonus_amount）的写入口——/adjust/ 的直写与归零都只经它。
#: ⛔ 原写「唯一写入口」与实现不符（/submit/、settlement_hooks.py、全额退款 refunds.py 的 .update() 都直接写这两列、逐条见
#: performance.write_performance_amounts 的 docstring）⇒ 按实现写准、不写绝对句（E-0011）。
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, approved_penalty_price
from apps.business.performance import (
    apply_amount_to_effective_base,
    recompute_order_amounts,
    write_performance_amounts,
)
#: 「已结账」走 Performance.is_settled（三字段并集）。调用方不要再各写一份：只看 settled_at 的路径会在「只有 settled_at」的存量行上失守。
#: 它不是全仓唯一判法：order_stats._unsettled_amount 与 settlement._reject_message 只看 settle_status、对「未结账却留着 settled_amount」的脏行结论相反；把那两处也改成并集是在改钱的口径。
from apps.business.performance_amount import (
    AmountError,
    CENT,
    deduct_amount,
)
#: ⭐（2026-11-07 R3）挂负账判据的唯一实现处（快照 / 净负集）——本文件只消费。
from apps.business.performance_negative_debt import (
    negative_debt_snapshot,
    net_negative_user_ids,
)
#: ⭐（客服预计绩效线）：客服行预计绩效的重算入口（settlement_hooks 里、与「生效」共用 customer_commission_amount 那一份算式）。本视图有两个挂点：
#: ① /adjust/ 的显式 temp_amount 直写排在共用重算之后 ⇒ 主挂点读到的是旧值、必须在这里补一次；② /adjust-amount/ 走 apply_amount_to_effective_base →
#: recompute_order_amounts 的主挂点已经覆盖、本处不重复挂。
from apps.business.settle_requests import cancel_pending_settle_requests
from apps.business.source_groups import source_ids_in_group
from apps.business.settlement_hooks import refresh_customer_estimate
from apps.business.order_flags import can_edit_taker_price
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node, has_node_now, nodes_of


#: ⭐ R3（编排方裁定）：非「已完成」的订单不许直写非 0 生效绩效。
#: 两个直写口子（/adjust-amount/ 的 amount_kind="bonus" 与 legacy /adjust/ 的显式 bonus_amount 键）共用同一句文案与同一个业务码（PERFORMANCE_STATE_INVALID）。
#: ⚠️ 前端照原样显示后端这句 msg（⛔ 不新增同义文案常量，spec §4.5）。
BONUS_ONLY_ON_COMPLETED_MSG = "生效绩效只在订单变成「已完成」时按预计绩效生成，不能在这里直接改"


#: ⭐ 线 C：date_type 的取值 → 施加 date_from / date_to 的列名（本线唯一的映射处）。键与 PerformanceListSearchSerializer.date_type 的 choices 逐字对齐
#:（两处各写一遍是刻意的：序列化器管「收什么」、这里管「打哪一列」；五档各自的筛法由 tests/test_performance_api.py 的用例逐条钉住）。
#: ⚠️ 三列是 Performance 自己的列、另三档里 created / settled 也是本表列；published / submitted / completed 三档走 order__ 关系 ⇒ 新增 order__ join、同一次登记 tests/test_order_read_path_net.py（AGENTS.md §5-G / E-0175）；⚠️ published_at / submitted_at / completed_at / settled_at 四列可空 ⇒ 按那四档筛时空值行自然被排除（见 performance_queryset 里时间区间那一段的注释）。⭐⭐ 线 E：五档；reviewed 一档现由 completed 承担（换列）——原 reviewed 打 Performance.reviewed_at、而那一列在生产上全空（唯一写入点 /review/ 这个租户从不走）⇒那一档线上必然筛空；真实验收是订单进 COMPLETED 那一刻 ⇒ 改打 order__completed_at；详见序列化器那一段与设计 §1。
PERFORMANCE_DATE_FIELDS = {
    "created": "created_at",
    "published": "order__published_at",
    "submitted": "order__submitted_at",
    "completed": "order__completed_at",
    "settled": "settled_at",
}


def _parse_time_bound(value):
    """筛选时间端的原始串 → `(aware 时刻, 是否带时间)`——时区/窗口换算的唯一解析口
    （serializer 只放原始串，两格式校验见 PerformanceListSearchSerializer）。
    date-only ⇒ 当日 00:00、False；带时间 ⇒ 该时刻（Asia/Shanghai，同旧 make_aware 口径）、True。"""
    try:
        moment = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        day = datetime.strptime(value, "%Y-%m-%d").date()
        return timezone.make_aware(datetime.combine(day, time.min)), False
    return timezone.make_aware(moment), True


def search_params(query_params):
    """绩效筛选参数的唯一校验口（P4FU-B 从 PerformanceViewSet._params 抽出来）。
    ⚠️ 抽出来是为了让导出端点与列表端点共用同一段入口——D-0072/C 的要求是「复用列表的筛选入口」、把校验留在视图方法里导出就只能重写一份（重写必然漂移）；字段集由 PerformanceListSearchSerializer 定义（P4FU-C 起；本函数只负责「空串 = 没传」+ 过序列化器）；⚠️ 导出端点也走本函数 ⇒ 新增筛选参数自动对导出生效。
    ⚠️ 空串按「没传」处理：前端常会送 ?emp_type= / ?user=、那是「没选」而不是非法值；⚠️ 这里必须过序列化器：?emp_type=BOGUS / ?user=abc / ?date_from=garbage 直接进 filter() 会抛 ValueError/ValidationError、不在异常处理器的业务分支里 ⇒ HTML 500（E-0010 的形状）。"""
    raw = {k: v for k, v in query_params.items() if v != ""}
    params = PerformanceListSearchSerializer(data=raw)
    params.is_valid(raise_exception=True)      # 非法值 ⇒ 200 + code 1000
    return params.validated_data


#: 「能看全量绩效」的管理侧判据（spec §6 第 11 行 / §13 Q3，业务方裁定）。
#: ⚠️ 为什么是 order.list（14「订单管理列表」）而不是 my_performance.export：my_performance.export（1163「允许代练员或下家导出自己的绩效记录」、③线借用的近似）——代练员也有这个点；只看它的话、删掉职位门就会让代练员看到全工作室的绩效（performance_queryset 不过滤人）；order.list 客服/客服主管有、代练员没有、正好是「管理侧」的现成标记——这是已知的借用（对标没有为绩效管理页留点）、要改只改这一个常量。
#: ⭐ D-0219 把 order.list 也接进了「进得了列表」那一道门：业务方裁定「有订单管理（修改）权限的人就该能进绩效列表、改打手绩效、不该被 1163 卡住」（现场：不勾 1163 时客服进不了绩效列表页）⇒ PERFORMANCE_PERMISSIONS["list"] 改成任一即进（1163 ∪ order.list）；1163 的语义随之从「列表的唯一门」降为「自助查看门」（代练员形态：只看自己的行）；管理侧入口 = order.list——与下面这个数据范围常量是同一个节点、两道门从此用同一把管理侧标记。
PERFORMANCE_MANAGEMENT_NODE = "order.list"


#: 绩效汇总（summary）里同属「订单金额 / 差额利润」这一族的四项。
#: ⚠️ 行上的三列（order_price / net_receipts / refund_fee）由 PerformanceListSerializer.MASKED_ORDER_MONEY_FIELDS 声明——两者是不同的出参（汇总 vs 行）、所以各自列一份；「谁能看」的判据仍只有 can_view_order_money() 一处。
PERFORMANCE_ORDER_MONEY_SUMMARY_KEYS = (
    "order_price", "refund_fee", "profit_expected", "profit_actual",
)


#: 财务侧的第二把钥匙（apps/rbac/catalog.py 80「打手与客服工资」）。
#: ⭐ 业务方裁定：财务只持 finance.payroll 是合法编制、让他们看到「—/0.00」等于把真实金额藏成假数（比不藏更糟）⇒ 判据扩成两节点任一。
#: ⚠️ 与 PerformanceListSerializer.WECHAT_QR_NODE 同值但用途不同（那个管收款码链接）；不从 serializers 反向 import（本模块 import 它、会成环）。
PERFORMANCE_FINANCE_VIEW_NODE = "finance.payroll"


def can_view_order_money(user):
    """能不能看绩效载荷里的订单金额 / 退款 / 差额利润（判据的唯一出处）。
    ⭐ 业务方报过的越权展示：打手（代练员）在「我的绩效」看到了订单金额与差额利润。判据 = order.list（PERFORMANCE_MANAGEMENT_NODE、能管订单的人：管理员/客服）或 finance.payroll（PERFORMANCE_FINANCE_VIEW_NODE、财务工资那一格）——任一即看；不持两者 = 代练员/下家 ⇒ 看不到；财务侧那条是业务方明确拍板补的（见常量的注释）⛔ 不要为了「收得更紧」把它删掉：财务看不到订单金额就没法对账。
    ⚠️ 这是字段脱敏、不是 403（与 order_flags 的 flag 同一条口径）：不持该节点的人本来就该能进绩效列表、看自己的行（my_performance.export 是自助查看门）；把整页打成 403 会连「他自己挣了多少钱」一起关掉。所以本函数被三处消费：summary_from_totals()（汇总四项给 None）、PerformanceViewSet._row_payload()（行上的三列给 None；序列化器只执行、不自己判，否则「同一事实两套判据」）、导出（performance_write_views.PerformanceExportView：不持时整列删掉「订单价格/退款」（明细）与「总计订单价/总计退款」（汇总））。⚠️ 不持时给 None、⛔ 不是 0 / ""：0 会被当真值（"0.00" 恰好也是合法金额）、前端没法区分「不让看」与「真的是 0」⇒ 必须用能表达「没有这个值」的形状。"""
    return has_node_now(
        user, PERFORMANCE_MANAGEMENT_NODE, PERFORMANCE_FINANCE_VIEW_NODE
    )


def _performance_base_queryset():
    """绩效列表的基集（基判据的唯一构造处：select_related / user__isnull=False /
    软删单与软删行排除 / 双隐藏单排除）。各条判据的「为什么」见
    performance_queryset_with_debt 里的对应注释，此处不复述。"""
    qs = Performance.objects.select_related(
        "order__game", "user", "user__staff_profile", "settle_request"
    ).filter(
        user__isnull=False,
        order__deleted_at__isnull=True,
        deleted_at__isnull=True,
    )
    # ⚠️ 写成 `exclude(...=False)`（⛔ 不是 `filter(...=True)`）：结构网
    #    `tests.test_order_read_path_net.HallShadowExclusionNetTests` 校验「方法是
    #    exclude + 比较值是 False」，等价布尔翻转会让那条网失去判别力。
    return qs.exclude(order__hall_card__isnull=False).exclude(
        order__upstream_hall_card__isnull=False
    )


def performance_queryset(user, data):
    """绩效列表的筛选段（P4FU-B 抽成函数：列表与导出共用这一段、唯一出处）。
    ⚠️ 不要在两处各拼一份：导出自动跟着本函数走；扩充点只应是 PerformanceListSearchSerializer + 本函数——「导出的是另一批数据」这种 bug 只有在两处各写一套时才可能出现、且无法被测试发现。
    ⭐ 数据范围（spec §6 第 11 行 / §13 Q3）也收口在本函数末尾：持 PERFORMANCE_MANAGEMENT_NODE（order.list）⇒ 全部行；不持 ⇒ 只看自己的行；列表与导出共用这段收口、所以「持导出点 133、不持 14 的自定义职位能导出全量」这个潜伏的洞关在这里（详见函数末尾那段注释）。⚠️ 时间区间按裸列比较收窄（date-only 整天含端 / 带时间按时刻，解析唯一在 `_parse_time_bound`）；⭐ 线 C：区间判哪一列由 date_type 选——date_from / date_to 的名字与契约不变 ⛔ 不新增三个参数名（同一份筛选不许两套契约）；⭐⭐ 线 E 起是五档（created/published/submitted/completed/settled、默认 created）、原 reviewed 档已改名换列成 completed（逐条口径与「为什么改」见本函数时间区间那一段的注释 + PERFORMANCE_DATE_FIELDS）；⚠️ 边界判据一律 is not None（E-0021）：customer_type=0（销售人）是合法值、if data["customer_type"]: 会把它当成「没传」而筛错列；id 类参数（user/game/order_source）的 min_value=1 在序列化器处就拒了 0、这里只区分「传了/没传」。P4FU-C 新增（brief §2 B 的参数表、逐条给测试）：game / order_status / order_type / order_source / customer_type + customer / username / order_no / keyword / project / customer_ww；⛔ 没有数据的六类（淘宝状态/收款状态/是否实名/申请结算/排团团号/机位）不在这里伪造过滤。⭐ user IS NULL 的行不进本页（业务方现场提出）：「只有订单关联的打手产生了绩效才可以生成到这里」；⚠️ 这不改「录单即建绩效」那个设计（P3a D-0010）——行仍在库里（派单/抢单//adjust 都要用它）、只是不再出现在绩效管理页（列表/导出/summary 三处自动一致、因为它们是同一个 queryset）；⛔ 不要因为「库里查得到、列表查不到」就以为这是脏数据或把建行逻辑删掉；⚠️ 判据用 user__isnull=False（列级）、不是 user__isnull 的真值——与 E-0021 同族。⭐（2026-11-07 R3 起）本函数是 `performance_queryset_with_debt` 的薄封装：导出端点只要行集、不要挂负账红签集合（xlsx 无红签），签名不变；列表端点走后者。"""
    return performance_queryset_with_debt(user, data)[0]


def performance_queryset_with_debt(user, data):
    """绩效列表的筛选段 + 挂负账红签数据（2026-11-08 业务方拍板撤强制展示后）：返回
    `(queryset, debt_users)`。列表恢复**纯筛选语义**——不再有任何行被无视筛选塞入
    （原腿② `qs | forced` 已撤，判据表与撤除记录 = 计划 §2「已撤 2026-11-08」行）。
    `debt_users` = 挂负账打手集，只喂列表红签 context（行级标记，零查询语义副作用）；
    勾 `exclude_net_negative` ⇒ 净负打手**整人**排除（判据 = 快照 + A/B 项净额，
    `performance_negative_debt.py`，范围仅打手域）；发 `order_no` ⇒ 整段跳过
    （订单详情只看本单的行，2026-11-08 热修口径保留）。
    （上方本函数 docstring 的筛选段口径逐条继续成立。）"""
    # ⚠️ user__isnull=False 只加在这里：list / export 共用本函数、而 summary 吃的是 list() 传进来的同一份 queryset（见 PerformanceViewSet.list）⇒ 三处同源；⛔ detail 动作（/adjust、/review）走 _base_queryset() 不过本函数——那是刻意的：没有归属人的行仍要能被 /adjust 命中并回它自己的 1014。
    # ⭐ D-0144（C2/C3）：已软删单的绩效行不进绩效列表、导出与 summary（三处同源、因为都吃本函数的返回值）；跨关系 JOIN 不经过 Order 的默认 manager ⇒ 必须显式补（spec §4.3-C2）。⭐ D-0147（批 E 任务 3）：Performance.deleted_at__isnull=True 也补在这里——垃圾桶（POST /performances/<id>/delete/）只软删绩效行、不软删订单、而 Performance 侧是跨关系 JOIN ⇒ 不补这一条被删的行仍会出现在绩效列表/导出/summary 里（「删了还看得见」）；判据见 tests/test_performance_delete_api.py 的 PerformanceListBoundaryTests。
    # ⭐（提前结算申请）：settle_request 是反向一对一（PerformanceSettleRequest.performance 是 OneToOneField）、PerformanceListSerializer.settle_requested_at 读它 ⇒ 列表必须预取、否则一页 N 行就是 N 条查询（判据：test_emp_emplist_backend 的查询计数族同款）；⚠️ 它只是 LEFT JOIN、不改变行集与筛选语义。⭐ 镜像卡派生的隐藏单（Order.hall_card 非空）的绩效行不进绩效列表/导出/summary——它不是本租户的一笔生意（没有客户价、也不是本租户的收入）；⚠️ 与 order__deleted_at__isnull=True 同一个理由：绩效表走跨关系 JOIN、不经过 Order 的默认 manager ⇒ 必须显式补这一条（生产上隐藏单没有 MAIN 行、但「有没有行」不是本函数的前提；带行的防御性用例见 tests.test_performance_api）。
    qs = _performance_base_queryset()
    if data.get("user") is not None:
        qs = qs.filter(user_id=data["user"])
    if data.get("emp_type"):
        qs = qs.filter(emp_type=data["emp_type"])
    if data.get("review_status"):
        # ⚠️ 按某个审核状态筛**不含** `review_status IS NULL` 的行（还没交单的绩效）。
        #    这是刻意的：`None` 不是「待审核」，混进来会让「待审核列表」多出没交的单。
        qs = qs.filter(review_status=data["review_status"])
    if data.get("settle_status"):
        qs = qs.filter(settle_status=data["settle_status"])
    if data.get("net_receipts_state"):
        # ⭐ D-0368（业务方指认「收款状态 = 订单的收款状态（上家同步的那个）」）：筛 order.payment_status（PaymentStatus 三档：未收款/结算中/已结款；数据源 = 闲管家自动已结款 + D-0359 上家收款同步）；不接 order.received_price：生产上那一列没有值、「已收款」会结构性恒空；业务语义是 payment_status。
        # received（已收款）⇒ payment_status=SETTLED；unreceived（未收款）⇒ 非 SETTLED 的都算（未收款/结算中/任何历史值——「还没收到」的补集 ⛔ 不枚举档位防新增档被漏）。
        # ⚠️ 判据用 get(...) + 分支常量等值（不用真值）：两个取值都是非空串、空串已在 search_params 入口被丢成「没传」。
        if data["net_receipts_state"] == "received":
            qs = qs.filter(order__payment_status=PaymentStatus.SETTLED)
        else:
            qs = qs.exclude(order__payment_status=PaymentStatus.SETTLED)
    # ── P4FU-C：订单维度的筛选（全部落在 `order__` 前缀上，复用上面那次 JOIN）──
    if data.get("game") is not None:
        qs = qs.filter(order__game_id=data["game"])
    if data.get("order_status"):
        # ⭐ 多选实装：order_status 是「已校验的枚举码列表」（PerformanceListSearchSerializer.validate_order_status 的产物；与订单列表 status 同一次改、共用 _order_status_tokens 那一份判据）⇒ order__status__in。
        # 单值等价：单元素列表 ≡ 旧 order__status=<码>；⚠️ 到这里的值一定是非空列表（空串被 search_params 丢成「没传」、、 那类空 token 会被校验成 1000）⇒ 真值判据不依赖巧合也可用；保留本文件这一支既有的真值风格（上下两行同款）。
        qs = qs.filter(order__status__in=data["order_status"])
    if data.get("order_type"):
        qs = qs.filter(order__order_type=data["order_type"])
    if data.get("order_source") is not None:
        qs = qs.filter(order__order_source_id=data["order_source"])
    if data.get("source_group") is not None:
        # ⭐ 公司店铺分组（计划 §4 #4）：多一跳 `order__`，**单值 FK join**、
        # 不放大行；列表 / 导出 / `summary` 都吃本函数 ⇒ 三处同源。
        qs = qs.filter(order__order_source_id__in=source_ids_in_group(data["source_group"]))
    if data.get("customer"):
        # 对标语义就是「搜销售人（0）还是发单人（10）」：`sales_user` 是 FK（按用户名命中的
        # 是**账号**），`created_by` 是**用户名文本**列 —— 两个参数由此走不同的列。
        if data.get("customer_type") == 0:
            qs = qs.filter(order__sales_user__username__in=data["customer"])
        else:
            qs = qs.filter(order__created_by__in=data["customer"])
    if data.get("username"):
        # ⚠️ `user` 是已有的**单个 id** 参数（见上）；`username` 是新增的**批量用户名**参数。
        #    两个都留着：前者给「已知道 id」的调用方，后者给「按名字批量搜」的对标控件。
        qs = qs.filter(user__username__in=data["username"])
    if data.get("order_no"):
        qs = qs.filter(order__order_no=data["order_no"])            # 精确（与结算页同口径）
    if data.get("keyword"):
        # 对标 key1 的 placeholder 原文 = 「订单号/角色名」⇒ 两个字段的模糊命中取并集。⭐ 第 3 个搜索面：来源单号（业务方原话：「搜索1 …来源单号搜不到，如果是的话就修复吧」）。
        # ⛔ 纯加法：上面那两项（系统单号/角色名）一字未动、只并上第三项。⚠️ 如实登记的口径边界（别当成对拍结论读）：对标那一档发的 key 到底匹配系统单号还是来源单号、本线没有实测出来——.tools/recon/ 里没有该端点的回包样本（当时 cookie 已过期）；本项的依据是业务方的明确口径「来源单号也要能搜到」、不是与对标语料对拍的结果。
        # 📌 同族先例（可交叉核对）：管理订单页/抢单大厅/订单回收站三个搜索口的 KEYWORD_FIELDS 同样补过 source_order_no（apps/business/selectors.py 顶部那段注释记了同一件事的由来）；⚠️ 判据见 tests/test_p4fu_performance_reads.py::FilterTests（三个面各一条 + 一条「都不匹配 ⇒ 空」的阴性对照）。
        qs = qs.filter(
            Q(order__order_no__icontains=data["keyword"])
            | Q(order__game_role_name__icontains=data["keyword"])
            | Q(order__source_order_no__icontains=data["keyword"])
        )
    if data.get("project"):
        qs = qs.filter(order__project_text__icontains=data["project"])
    if data.get("customer_ww"):
        qs = qs.filter(order__customer_ww__icontains=data["customer_ww"])
    # ⭐ 性能线（2026-11-04）：时间区间从 `__date__gte/lte` 改写为**裸列比较**
    # （date-only 端点 = `>= 当日 00:00` / `< 次日 00:00`，算式见 `_parse_time_bound`）：
    # `__date` 族在 SQL 里是 `(列 AT TIME ZONE 'Asia/Shanghai')::date` —— 套了函数的列进不了
    # b-tree（生产 EXPLAIN 全表扫）；裸列比较语义与 `__date` 逐日等价（同 settlement 域 W-01 的同款修法与对拍网）。
    # ⚠️ 判哪一列由 date_type 指定（映射见 PERFORMANCE_DATE_FIELDS）；默认 created（录单时间）。
    # ⚠️ 四档可空（order__published_at / order__submitted_at / order__completed_at / settled_at）
    # ⇒ 按它们筛时空值行自然不满足 >= / <（SQL 三值逻辑）而被排除——与旧 `__date` 同口径、不是漏行；
    # 五档各有一条「空值行不出现」的用例（test_each_date_type_excludes_rows_with_a_null_column）。
    # ⚠️ 新增 order__ join 必须同一次登记 tests/test_order_read_path_net.py（AGENTS.md §5-G / E-0175）。
    # ⚠️ 边界判据用 is not None（E-0021）：日期值本身恒为真、但「传了 / 没传」才是这里要区分的。
    date_field = PERFORMANCE_DATE_FIELDS[data["date_type"]]
    # ⭐（2026-11-07 交单时间筛选修复）：from 恒 `>=` 该时刻；to 分流——date-only 维持
    # 「整天含端」（`< 次日 00:00`，与修前的 date-only 行为逐值相同，边界对拍钉在
    # tests/test_performance_list_totals.py::DateWindowBoundaryTests）、带时间含该秒（`<=` 该时刻）。
    if data.get("date_from") is not None:
        bound, _ = _parse_time_bound(data["date_from"])
        qs = qs.filter(**{f"{date_field}__gte": bound})
    if data.get("date_to") is not None:
        bound, has_time = _parse_time_bound(data["date_to"])
        if has_time:
            qs = qs.filter(**{f"{date_field}__lte": bound})
        else:
            qs = qs.filter(**{f"{date_field}__lt": bound + timedelta(days=1)})
    # ⭐（2026-11-07）「排除禁止结算的」单档（原三态的 all/only 档已撤，前端改挂打手多选
    # 下拉的虚拟分组哨兵）：缺省 = 不加过滤（ids 查询只放在分支里）；ids 取**全租户**
    # 被禁集、不按当前筛选 narrowed（queryset 过滤是合取的，「全租户集」施加到已收窄的 queryset
    # 与「先取该 queryset 内被禁集」恒等）；判据唯一出处 settle_forbidden_user_ids（D-0109，⛔ 不另写
    # 第二份 settle_forbidden 比较）；⛔ exclude 不是 filter(profile__settle_forbidden=False)——
    # INNER JOIN 会把没档案行的人整批筛掉（settlement.py 同族先例 + 三人组用例钉着）。
    scope = data.get("settle_forbidden_scope")
    if scope == "excluded":
        forbidden_ids = settle_forbidden_user_ids(None)
        qs = qs.exclude(user_id__in=forbidden_ids)
    # 数据权限的唯一卡点（spec §4.4）：基线是 pass-through、但接线必须在、且要有判别力测试钉住「接线真的被用上」（P3a 的教训：只测函数 = 没人守接线）。
    # ⭐「按游戏权限收窄」：Performance 经 order 指到游戏 ⇒ order__game_id；本函数是列表与导出共用的筛选段、接线一次两处生效（导出绕过收窄会是「列表看不到、导得出」）；规则本体在 apps/business/game_scope.py（scoping.py 不内置业务规则，E-0006）；租户管理员的短路在 scope_queryset 里。
    qs = scope_queryset(user, qs, scope_filter=game_scope("order__game_id"))

    # ⭐ 数据范围（spec §6 第 11 行 / §13 Q3）：持 PERFORMANCE_MANAGEMENT_NODE（order.list，14「订单管理列表」）⇒ 全部行；不持（自定义职位/代练员）⇒ 只看自己的行。
    # ⚠️ 收口必须在本函数里、不能留在 list()：导出端点（PerformanceExportView）也吃本函数的返回值、而它的门是 finance.export_performance（133）——没有 order.list；收口原先只写在 PerformanceViewSet.list() 里 ⇒ 一个持 133 而不持 14 的自定义职位能导出全工作室的绩效行（内置职位今天恰好都不满足这个组合、所以是潜伏的洞、但它正是本线要关闭的那一类数据可见性缺陷）。
    # ⚠️ 数据范围不是 403：自定义职位与代练员本来就该能看自己的绩效（my_performance.export 挂在列表上、而代练员也有这个点）；list() 与导出共用本函数返回值 ⇒ 两处口径不可能分叉、汇总（summary）也跟着收窄；⛔ 不要在 list() 里再筛一次：两处过滤会分叉、且 summary 与 items 可能不同源。
    if PERFORMANCE_MANAGEMENT_NODE not in nodes_of(user):
        qs = qs.filter(user=user)

    # 挂负账段（2026-11-08 撤强制展示后只剩红签数据 + 净负排除，纯筛选语义）：
    # 快照是红签与净负判据的唯一来源（租户级缓存，稳态每 45s 一次聚合）；净负集聚合只在
    # 勾排除时取数、随签名缓存。发 `order_no` ⇒ 整段跳过——订单详情只看本单的行，
    # 且净负排除会整人抽行，按单视图里一样是「这单的行消失」（037fea1f 口径保留）。
    debt_users = frozenset()
    if not data.get("order_no"):
        debt = negative_debt_snapshot()
        debt_users = set(debt)
        if data.get("exclude_net_negative"):
            net_neg = net_negative_user_ids(
                debt, qs, cache_key=_net_negative_cache_key(user, data)
            )
            qs = qs.exclude(user_id__in=net_neg)      # 净负打手整人排除（纯筛选）
    return qs, frozenset(debt_users)


def _money(value):
    """把一项聚合值收口成出参形状的两位小数金额。
    ⚠️ 空集是 "0.00" 而不是 null（emp-emplist 计划 §2.2）：聚合对空集返回 None、直接把 None 交给前端会让汇总行整片消失、而「这个筛选下没有数据」应当显示 0。
    ⚠️ 全程 Decimal、绝不 float()：Sum 在 PostgreSQL 上是 numeric、Django 还原成 Decimal；本函数只做 Decimal(...) 收口 + quantize(CENT)。⚠️ 返回 str 而不是 Decimal：交付物是 JSON、而 DRF 的 JSON 编码器对 Decimal 的处理是 float(obj)（rest_framework/utils/encoders.py）——Decimal("70.00") 会变成 70.0、与本项目所有金额字段在线上必然是两位小数字符串（COERCE_DECIMAL_TO_STRING：temp_amount 是 "20.00"）的口径不一致；信封里的 summary 不经过 DRF 字段、所以这一步只能在构造处做；str(Decimal("70.00")) == "70.00"、形状与那些字段逐字节相同。"""
    if value is None:
        return "0.00"
    return str(Decimal(value).quantize(CENT))


#: 聚合短缓存键前缀（防与 default alias 里 login_rate_limit / order_qr_fetch 等撞键）。
_PERF_TOTALS_CACHE_PREFIX = "perftotals"
#: ⭐（2026-11-07 R3）净负集的签名缓存键前缀（`performance_negative_debt.net_negative_user_ids`
#: 消费）。键料与 totals 键同构（视图 + 用户 + 全部筛选）；⛔ 不含金额可见性——净负集与
#: 订单金额族无关，多分一键只会白拆缓存（对计划 §5.1-4「复用签名键料」的按实收窄，登记）。
_NET_NEG_CACHE_PREFIX = "perfr3"
#: 键里的视图名段（同签名不同视图不共键；本缓存只服务绩效列表一个读口）。
_PERF_TOTALS_CACHE_VIEW = "PerformanceViewSet.list"


def _signed_cache_key(prefix, parts):
    """缓存键的统一拼法。⚠️ schema 前缀防跨租户串键（测试进程多租户共用同一份 locmem，
    照 hall 先例）；`separators` 无空格——Django 的 memcache_key_warnings 把空格判非法
    字符（CacheKeyWarning）。"""
    return (
        prefix
        + ":"
        + connection.schema_name
        + ":"
        + json.dumps(
            parts, sort_keys=True, ensure_ascii=False, default=str,
            separators=(",", ":"),
        )
    )


def _performance_totals_cache_key(user, data, *, can_view_order_money):
    """「筛选签名」→ 缓存键。签名四段：视图名 + 用户 id（数据权限 game 范围按人）+
    全部生效筛选参数（validated_data 逐项进 key）+ 订单金额可见性（summary 形状按它分叉）。"""
    return _signed_cache_key(
        _PERF_TOTALS_CACHE_PREFIX,
        {
            "view": _PERF_TOTALS_CACHE_VIEW,
            "user": user.pk,
            "money": bool(can_view_order_money),
            "filters": data,
        },
    )


def _net_negative_cache_key(user, data):
    """净负集的「筛选签名」键（R3；判据 = 命中后第二次同签名请求零聚合 SQL，S-6）。"""
    return _signed_cache_key(
        _NET_NEG_CACHE_PREFIX,
        {"view": _PERF_TOTALS_CACHE_VIEW, "user": user.pk, "filters": data},
    )


def performance_totals(queryset, *, can_view_order_money, include_settlement_summary=False):
    """列表页「总数 + 汇总」的原始聚合数（count 并进聚合后的唯一取数口；性能线 2026-11-04）。
    ⭐ 行级一条 SQL 同时算 `COUNT(DISTINCT id)` 与两个 SUM——旧实现是「分页 count() 一条 +
    行 SUM 一条」，两份 WHERE 完全相同、各扫一遍双表（生产 EXPLAIN cost 4828~8984）；
    `COUNT(DISTINCT id)` 与旧 `count()`（COUNT(*)）在基集上逐值相等：全部 join 都是 FK/OneToOne（行保持）。
    ⭐ 金额口径 D-0255「按单」：order_price / refund_fee 按 `(order_id, 价, 退款)` 元组去重后求和
    （登记：docs/FRONTEND-REPLICA-STANDARD.md §7）——⛔ 不是 `Sum(distinct=True)`（那会误并同价的不同单）；
    提成仍按**行**求和。⚠️ 聚合别名不等于被引用列名：Django 的 get_aggregation
    把聚合临时塞进 annotations，别名撞列名会解析到聚合、抛 FieldError（实测踩到）。
    ⚠️ `can_view_order_money=False` ⇒ 价/退款**连查都不查**（省一条 SQL，与旧 performance_summary 同口径）。"""
    row_aggregates = dict(
        _row_count=Count("id", distinct=True),
        _sum_temp_amount=Sum("temp_amount"),
        _sum_bonus_amount=Sum("bonus_amount"),
    )
    if include_settlement_summary:
        # 四项金额共用同一条 SQL 的快照，结账时同一笔绩效只落在其中一栏。
        completed = Q(order__status=OrderStatus.COMPLETED)
        row_aggregates.update(
            unsettled_bonus_amount=Sum(
                "bonus_amount", filter=completed & Q(settle_status=SettleStatus.UNSETTLED)
            ),
            settled_bonus_amount=Sum(
                "bonus_amount", filter=completed & Q(settle_status=SettleStatus.SETTLED)
            ),
        )
    row_totals = queryset.aggregate(**row_aggregates)
    totals = {
        "count": row_totals.pop("_row_count"),
        "temp": row_totals.pop("_sum_temp_amount"),
        "bonus": row_totals.pop("_sum_bonus_amount"),
        "order_price": None,
        "order_refund": None,
        **row_totals,
    }
    if not can_view_order_money:
        return totals
    order_totals = (
        queryset.values("order_id", "order__total_price", "order__refund_fee")
        .distinct()
        .aggregate(
            _sum_order_price=Sum("order__total_price"),
            _sum_refund_fee=Sum("order__refund_fee"),
        )
    )
    totals["order_price"] = order_totals["_sum_order_price"]
    totals["order_refund"] = order_totals["_sum_refund_fee"]
    return totals


def summary_from_totals(totals, *, can_view_order_money):
    """筛选后全集的合计（绩效页 #total 汇总行；emp-emplist 计划 §2.2）——纯出参形状，不再打库。
    ⚠️ 入参是 performance_totals 的产物（全集口径，不是当前页）；`can_view_order_money=False`
    ⇒ 四项给 None（字段脱敏、不是 403；判据只有 can_view_order_money() 一处）、
    temp_amount / bonus_amount（他自己挣的钱）照旧给；None 而不是 0/"" 的理由见那个函数的 docstring。
    ⚠️ 默认六项；金额减法按 `is not None` 判空（E-0021：0 与负数都是合法边界）。"""
    summary = {
        "temp_amount": _money(totals["temp"]),
        "bonus_amount": _money(totals["bonus"]),
    }
    if "unsettled_bonus_amount" in totals:
        summary.update(
            unsettled_bonus_amount=_money(totals["unsettled_bonus_amount"]),
            settled_bonus_amount=_money(totals["settled_bonus_amount"]),
        )
    if not can_view_order_money:
        for key in PERFORMANCE_ORDER_MONEY_SUMMARY_KEYS:
            summary[key] = None
        return summary
    order_price = totals["order_price"]
    order_refund = totals["order_refund"]
    order_net = (
        (order_price if order_price is not None else Decimal("0.00"))
        - (order_refund if order_refund is not None else Decimal("0.00"))
    )
    temp_total = totals["temp"]
    bonus_total = totals["bonus"]
    summary.update({
        "order_price": _money(order_price),
        "refund_fee": _money(order_refund),
        "profit_expected": _money(
            order_net - (temp_total if temp_total is not None else Decimal("0.00"))
        ),
        "profit_actual": _money(
            order_net - (bonus_total if bonus_total is not None else Decimal("0.00"))
        ),
    })
    return summary


class PerformanceViewSet(viewsets.GenericViewSet):
    """绩效列表（spec §5；筛选项见 spec §16.4 #4）。"""

    pagination_class = EnvelopePagination
    serializer_class = PerformanceListSerializer
    # ⚠️ **只放行 list 与两个显式动作**：`POST` 只服务于 `/adjust`、`/review`（都是 `@action`），
    # 而 `PATCH`/`PUT`/`DELETE` 一律 405 —— 于是「绩效不能被通用 PATCH 改金额」是**结构**上的，
    # 不是靠人记得别加路由（有测试钉住）。`POST` 到集合 URL 也会 405（本 ViewSet 没有 `create`）。
    http_method_names = ["get", "post", "head", "options"]

    #: 逐 action 的节点 code 表（形状与 OrderViewSet.ORDER_PERMISSIONS 同）。⚠️ 字符串必须是字面量（原因同 OrderViewSet：结构兜底走 AST 取字符串常量）；
    #: ⚠️ adjust 只表达到"能进这个动作"（= 能改接单价那条对标 1162 的 code）；"本次载荷到底有没有带 taker_price"是条件判据、permission_classes 表达不了 ⇒ 它在函数体里再判一层（③线 Ruling 12 的两层口径）；
    #: ⚠️ 未登记在表里的 action 一律走默认权限类（fail-closed：仍要登录）、不是默认放行。
    PERFORMANCE_PERMISSIONS = {
        # ⭐ D-0219：列表门「任一即进」——my_performance.export(1163，自助查看门、代练员也持) ∪ order.list(14「订单管理列表」、管理侧标记、与 PERFORMANCE_MANAGEMENT_NODE 同一个节点)。
        # 业务方裁定原话：不勾「允许代练员或下家导出自己的绩效记录」时客服进不了绩效列表页、也就改不了打手绩效——「有订单管理（修改）权限的人就该能进、能改」。
        # 元组 = any 语义（has_node_now「任一命中即放行」）、与 adjust/soft_delete 同款；数据范围不变：持 order.list ⇒ 全部行（管理侧）、只持 1163 ⇒ 只看自己的行。
        "list": ("my_performance.export", "order.list"),
        "adjust": ("order.submit_payout_price",),
        # ⭐ 本批（D-0147 批 E 任务 2）：`/adjust-amount/` 与 `/adjust/` **同门**（spec §4.6）——
        # 它也是「按节点放行就能改金额」那一行，不另立点（D-0073 禁止自造节点）。
        "adjust_amount": ("order.submit_payout_price",),
        # ⭐ D-0147 批 E 任务 3：垃圾桶（软删绩效 + 同一步清接手人）。门取 order.submit_payout_price(1162) 或 order.clear_taker(19)（spec §4.6）。
        # ⚠️⭐ 业务方裁定：不是 R-8b 的「额外要 19」那句（本表表达不了「这一行是不是当前接单人那条 MAIN 行」这个对象级条件）：这里保留的是「两件事各有一个既有节点」的粗门（并集）；真正的底门在 soft_delete() 函数体里——软删任何行都要 1162（缺则 403「你没有删除绩效的权限」）、清接手人不再额外要 19（本端点在该动作上代行 19、见函数体里 apply_transition 那一处）⇒ 只持 19 的账号仍进得来这个动作、但会被 1162 那道底门拒（403）；⛔ 不要把条件挪回类级：类级拿不到对象。
        # ⚠️ R-9 在同一层再收一层行类型：只有 emp_type == EmpType.MAIN 的行能删（SALES ⇒ 业务码 1014）⇒ 上面那句里的「任何行」今天 = 任何打单工资行；本表仍是粗门、表达不了行类型；⛔ 不借 finance.delete_performance(85)——那是既有硬删端点（POST /performances/delete）的门、本端点语义不同（软删、可还原）。⚠️ 键名是 soft_delete（不是 URL 段的 delete）：动作方法名刻意避开 HTTP 动词（否则 APIView.dispatch 的 getattr(self, "delete") 会让每条 DELETE 请求命中它、绕过 router 的 method_map 过滤，见 E-0072 / tests/test_drf_view_method_names.py）；与 adjust_amount ↔ URL adjust-amount 同形：动作名与 URL 段本就不必逐字相同。
        "soft_delete": ("order.submit_payout_price", "order.clear_taker"),
        "review": ("order.change_status_completed",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；与默认权限类取并集（不替换）。
        ⚠️ 为什么把默认类并进来：默认类表达的是"这一层的最低门槛"（今天 = IsAuthenticated，config/settings.py:110 的 DEFAULT_PERMISSION_CLASSES）、节点判据表达的是"这个动作还要哪个 code"；两者是合取、不是二选一——并集把两份声明都保留下来，理由：与框架级默认保持一致、默认值将来变化只改一处、"本动作需要登录"写在声明处。
        ⚠️ 匿名请求为什么是 401 而不是 403（已读源码核实；③线 Ruling 12 更正过一版错理由——不要再写"没有认证器 ⇒ DRF 会短路"或"has_node_now 恰好访问 .user"）：APIView.initial 依次调 perform_authentication / check_permissions / check_throttles（rest_framework/views.py:419-421）、perform_authentication 的实现就是 request.user（views.py:329）；而 permission_denied（views.py:174-180）在 request.authenticators 非空、且没有认证器成功时一律抛 NotAuthenticated（401）、与是哪个权限类拒绝无关；本项目 DEFAULT_AUTHENTICATION_CLASSES 就是 TenantBoundAuthentication（config/settings.py:101-103）、它对匿名请求 return None（apps/core/authentication.py:14-15）⇒ 401 由权限层产出；check_permissions（views.py:331）只遍历 get_permissions()。仍然显式保留 IsAuthenticated 的理由是声明与单一出处（不是"缺少它就会掉到 403"）：①与框架级的 DEFAULT_PERMISSION_CLASSES 保持一致；②那个默认值将来若变、改一处即可；③"本动作需要登录"写在声明处、而不是靠默认值或框架行为兜着。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.PERFORMANCE_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # IsAuthenticated 提到最前并保证恰好一个（默认类今天就是它；重复一份是无害的、但读起来像笔误、所以显式去重）；其余默认类一条都不丢。
        # ⚠️ 判等用 type(p) is not IsAuthenticated（不用 isinstance）：isinstance 会把子类也当成同一个而丢掉——那是一个会静默削弱本层的形状。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    def _params(self):
        """查询参数唯一的校验口——委托给模块级 search_params（导出端点共用同一份）。
        ⚠️ 判据（空串 = 没传 / 必须过序列化器 / 失败形状）全部写在那个函数的 docstring 里、这里不再复述；本方法只保留「从 self.request 取参数」这一件事。"""
        return search_params(self.request.query_params)

    def _base_queryset(self):
        """详情动作用的基础集：过数据权限卡点、但不套用筛选参数。
        ⚠️ 分流是刻意的：get_object() 会调 get_queryset()、若 detail 动作也走筛选那条路，?user=999 之类与本次操作无关的参数会让「改这张绩效」变成 404——更糟的是它会顺手解析参数、把 ?emp_type=BOGUS 变成业务码、让人以为 /adjust 有筛选能力；detail 动作不读这些参数（E-0010 只管「值进查询」）。
        ⭐「按游戏权限收窄」：get_object() 走本函数 ⇒ 别人游戏的绩效行对这个视图就是"不存在"（/adjust / /review 拿 1002）；这与 order.restrict_to_self_created 刻意不进 get_queryset() 相反、理由见 order_views.py：按游戏收窄的是可见范围本身、而那条 flag 是对可见范围内一行施加的额外限制——两者的失败语义不同。"""
        return scope_queryset(
            self.request.user,
            # ⭐ D-0144（C3）：detail 动作（/adjust、/review）不吃筛选参数、但同样不该命中已软删单的绩效行（跨关系 JOIN 不经 Order 的默认 manager）。
            # ⭐ D-0147（批 E 任务 3）：垃圾桶软删过的绩效行同理从这里消失（否则「删掉」之后 /adjust / /review 仍能改它、行为上没删）。
            # ⭐ /adjust、/review、/delete/ 的响应也走 PerformanceListSerializer（_row_payload）、那里会读反向一对一 settle_request ⇒ 这里一并预取、避免「改一次金额多一条查询」。
            Performance.objects.select_related(
                "order__game", "user", "user__staff_profile", "settle_request"
            ).filter(
                order__deleted_at__isnull=True, deleted_at__isnull=True
            ),
            scope_filter=game_scope("order__game_id"),
        )

    def get_queryset(self):
        if self.action != "list":
            return self._base_queryset()
        # 筛选项的唯一出处是模块级 `performance_queryset`（导出端点共用同一份）。
        return performance_queryset(self.request.user, self._params())

    def get_paginated_response(self, data, *, summary=None):
        """把 summary 透给分页器 ⇒ core.envelope.paginated(summary=)。
        ⚠️ 信封形状仍然只在 core.envelope 定义：本方法只搬运关键字 ⛔ 不在这里拼 {items,total,...} 的字典（那样就有两处信封定义）；⚠️ 不传 summary 时与 GenericAPIView 的默认实现逐字节相同（既有调用者无感）。"""
        return self.paginator.get_paginated_response(data, summary=summary)

    def list(self, request, *args, **kwargs):
        """绩效列表。准入 = 任一即进（D-0219）：my_performance.export(1163) 或 order.list（由 get_permissions() 的类级节点门判、元组 = any）；看得到多少行 = 数据范围（spec §6 第 11 行 / §13 Q3）：持 PERFORMANCE_MANAGEMENT_NODE（order.list，14「订单管理列表」）⇒ 全部行；不持（自定义职位/代练员）⇒ 只看自己的行。
        ⚠️ 数据范围收口在 performance_queryset 里：本方法不再自己筛一次——导出端点吃的是同一个函数、只有把收口放进那里「列表收窄、导出全量」这个洞才关得上（详见该函数末尾那段注释）；本方法只负责取 queryset + 聚合 + 分页 + 汇总。⭐ 汇总行（emp-emplist 计划 §2.2）：summary 打在同一份筛选后的 QuerySet 的全集上（不是 page 那 20 行）——两者吃同一份 queryset、筛选条件不可能分叉。
        ⭐ 性能线（2026-11-04）：count 与两个行 SUM 并成一条聚合（performance_totals）、按筛选签名短缓存（PERFORMANCE_LIST_CACHE_TTL_S，默认 45 秒 ⇒ total 与 summary 最多旧 45 秒——业务方已知并批的取舍）；items 的行查询不受缓存影响。判据：tests/test_performance_list_totals.py。
        ⭐ 业务方报过的越权展示：订单金额/退款/差额利润是「字段脱敏」、不是 403；不持 order.list 也不持 finance.payroll 的人——代练员/下家——仍然进得来、看得到自己的行与自己的提成，但：summary 的四项（order_price / refund_fee / profit_expected / profit_actual）是 None（⛔ 不是 0/""）；行上的 order_price / net_receipts / refund_fee 是 None（_row_payload() 把同一个判据交给序列化器、序列化器不自己判一次）。判据本体只有一处：can_view_order_money(user)；数据范围（行数）仍由 performance_queryset 收窄、两件事互不替代。"""
        data = self._params()
        include_settlement_summary = data.pop("include_settlement_summary", False)
        queryset, debt_users = performance_queryset_with_debt(request.user, data)
        # 挂负账打手集交给序列化器 context（红签判据；行级标记、零查询语义副作用）。
        self._negative_debt_user_ids = debt_users
        totals = self._list_totals(
            queryset, data, include_settlement_summary=include_settlement_summary
        )
        page = self._paginate_with_known_count(queryset, totals["count"])
        self._recorder_names = display_names_for_usernames(row.order.created_by for row in page)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(
            serializer.data,
            summary=summary_from_totals(
                totals, can_view_order_money=self._can_view_order_money()
            ),
        )

    def _list_totals(self, queryset, data, *, include_settlement_summary):
        """count + 汇总的原始聚合数：按筛选签名短缓存、命中则不打库（TTL = settings.PERFORMANCE_LIST_CACHE_TTL_S）。
        ⚠️ `ttl <= 0`（E-0021：0 是合法值 = 紧急关闭）零缓存读写、每次照常聚合——测试进程默认关（settings 测试块）。
        ⚠️ 缓存的只是聚合**数**；items 的行查询照常执行，缓存键含用户 id / 全部筛选 / 金额可见性 / 视图名 / schema。"""
        ttl = settings.PERFORMANCE_LIST_CACHE_TTL_S
        visible = self._can_view_order_money()

        def _compute():
            return performance_totals(
                queryset, can_view_order_money=visible,
                include_settlement_summary=include_settlement_summary,
            )

        if ttl <= 0:
            return _compute()
        cache_options = data
        if include_settlement_summary:
            cache_options = {**data, "include_settlement_summary": True}
        key = _performance_totals_cache_key(
            self.request.user, cache_options, can_view_order_money=visible
        )
        cached = cache.get(key)
        if cached is not None:
            return cached
        totals = _compute()
        cache.set(key, totals, ttl)
        return totals

    def _paginate_with_known_count(self, queryset, total):
        """翻页与 DRF `PageNumberPagination.paginate_queryset` 同形，唯独 count 用聚合算好的。
        ⚠️ `Paginator.count` 是 cached_property（django/core/paginator.py）：实例 dict 已有值时
        不再发 COUNT 查询 ⇒ 注入聚合数即可；`paginator.request` 必须先赋（EnvelopePagination
        的 get_paginated_response 读 `get_page_size(self.request)`）；越界页码照 DRF 抛 NotFound。
        ⚠️ 不走 `paginator.page()`：它会把切片上限夹到 count（`top = self.count`）——短缓存里
        旧 count 偏小时会把当前页的真实新行静默切掉；自己算 bottom/top，与「count 恰好新鲜」
        时逐字相同（count 新鲜时那个夹取本来就是空操作）。"""
        paginator = self.paginator
        paginator.request = self.request
        page_size = paginator.get_page_size(self.request)
        if not page_size:
            return None
        django_paginator = paginator.django_paginator_class(queryset, page_size)
        django_paginator.count = total
        page_number = paginator.get_page_number(self.request, django_paginator)
        try:
            number = django_paginator.validate_number(page_number)
        except InvalidPage as exc:
            raise NotFound(
                paginator.invalid_page_message.format(
                    page_number=page_number, message=str(exc)
                )
            )
        bottom = (number - 1) * page_size
        top = bottom + page_size
        paginator.page = django_paginator._get_page(
            queryset[bottom:top], number, django_paginator
        )
        return list(paginator.page)

    def _can_view_order_money(self):
        """本请求的判据结果（判据本体只有模块级 can_view_order_money() 一处）。
        ⚠️ 这里只做每请求一次的缓存：非管理员走 has_node_now 会查两次库（groups + 节点并集）、而列表一次请求要在两处用它（汇总 + 序列化器上下文）；
        ✅ 缓存值是真 bool（False 也存得下）、判据用 is None（E-0021）。"""
        cached = getattr(self, "_order_money_visible", None)
        if cached is None:
            cached = can_view_order_money(self.request.user)
            self._order_money_visible = cached
        return cached

    def get_serializer_context(self):
        """把「能不能看订单金额族」这个已判好的结果交给序列化器（判据仍只在 list() 那一处）。
        ⚠️ 序列化器是 /performances/、/adjust/、/review/ 三处响应的形状；它不自己查节点（同一事实两套判据会漂移）、只读本键；缺键 ⇒ 序列化器按 fail-closed 脱敏。
        ⭐（2026-11-07 R3）`negative_debt_user_ids` 同一通道：list() 里算好的挂负账打手集（红签）；
        /adjust、/review 走 `_row_payload` 自带 context、不带本键 ⇒ 行标记恒 `false`（fail-closed）。"""
        context = super().get_serializer_context()
        context["can_view_order_money"] = self._can_view_order_money()
        context["recorder_names"] = getattr(self, "_recorder_names", {})
        context["negative_debt_user_ids"] = getattr(
            self, "_negative_debt_user_ids", frozenset()
        )
        return context

    def _row_payload(self, perf, request):
        """绩效行的出参（唯一构造点）：把「能不能看订单金额族」一并带给序列化器。
        ⚠️ /adjust/、/adjust-amount/、/review/、/delete/ 的 data 都走这里：每条路各自 PerformanceListSerializer(perf, context={"request": request}) 时、那条「不给订单金额」的决定就不会被带上（漏一处 = 静默泄漏）。"""
        return PerformanceListSerializer(
            perf,
            context={
                "request": request,
                "can_view_order_money": self._can_view_order_money(),
            },
        ).data


    @action(detail=True, methods=["post"])
    def adjust(self, request, *args, **kwargs):
        """改绩效的接单价覆盖值 / 扣点（spec §5 / §6.3 / §8.3；P4 的 B3、B4）。
        权限：只看类级节点门 order.submit_payout_price（get_permissions()）——{CS} 职位门已删（spec §6 第 12 行）⇒ 持该节点的客服/客服主管/系统管理员都可以改绩效金额；只有当载荷里带 taker_price 时才额外问「能不能改接单价」（判据 = can_edit_taker_price、即不被 order.restrict_edit_taker_price 限制；legacy 的 business.override_taker_price 不再参与判据）——「改绩效金额/扣点」那一行按节点放行、「改接单价」是单独一行（由 flag 表达）。
        ⚠️ order.restrict_edit_taker_price 是 flag、被它挡住时的表现不是 403（spec §4.2/§5.2：「数据被过滤/字段脱敏/按钮显隐，不是 403」）：本线的前提就是「flag 读、node 门」、而"能不能到这个端点"由类级 node 门 order.submit_payout_price 回答（见 get_permissions()、本处不再重复判）；所以带 taker_price 而不得改时只忽略这一个键（响应 msg 注明）、同一载荷里的 deduct_mode/deduct_value/reason 照常应用——对标 1165 的原文正是「只能编辑其它的内容」；极性：持有该 flag = 施加「不许改接单价」的限制、can_edit_taker_price 是它的反面。四条硬判据：已结账不得改 ⇒ PERFORMANCE_STATE_INVALID(1014)、不是 P4 的 1006（B4）——判据是 Performance.is_settled 的三字段并集（裁定 A.2、与 /assign//delete//adjust-amount/ 同源）⛔ 不是只看 settle_status 单列；没有接单人的绩效不得改（P3b 计划 Q5 的裁定）⇒ 同样 1014、msg 可辨；⭐ R3（编排方裁定）：显式 bonus_amount 键只在订单当前状态 == COMPLETED 时放行、非完成态 + bonus_amount != 0 ⇒ 拒整笔（同 §4.2.3 的口径与文案、0 放行 = 归零）；改基数（接单价）后按 §6.1 的同一口径重算派生金额（否则「基数变了、金额没变」两套事实并存）——⭐ F1/裁定 A：这次重算整体走共用的 performance.recompute_order_amounts ⇒ temp_amount 总是按新基价写、bonus_amount 只在订单当前状态是 COMPLETED 时跟着写（⛔ 旧判据 review_status is not None 已废弃——它会在完成态漏写、在非完成态误写）；⭐ 改后的这一行必须仍算得出扣点额（emp-emplist §2.1 的连带）：temp_amount 变小可能让固定额扣点越界、而扣点额算不出来会让列表整表拒绝 ⇒ 在写库之前用 deduct_amount 校验、越界 ⇒ 业务码一行都不写。⚠️ bonus_amount 不含扣点（B3）：扣点由 P4 在应付净额里单独减；⚠️ 金额类预期失败（提成模式不可算/扣点越界）在事务之前算完、直接回业务码；空载荷/值没变 ⇒ code 0 且不留痕、不写库。"""
        # 职位门已删 ⇒ 准入**只剩**类级节点门 `order.submit_payout_price`
        # （见 `get_permissions()`）。副作用已由 spec §6 第 12 行登记：客服主管现在也能改金额。
        perf = self.get_object()
        serializer = PerformanceAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ⚠️ flag 的失败表现**不是 403**（spec §4.2/§5.2）：只忽略 `taker_price` 这一个键，
        #    其余字段照常应用（对标 1165：「只能编辑其它的内容」）。详见方法 docstring。
        ignored_taker_price = "taker_price" in data and not can_edit_taker_price(request.user)
        ignore_note = "接单价已按权限忽略" if ignored_taker_price else ""
        if ignored_taker_price:
            data.pop("taker_price")

        # ⚠️ **本批（D-0142）扩成五个键**：`temp_amount` / `bonus_amount` 是**直写**键
        #    （绩效弹层的行内编辑 / 垃圾桶归零，spec §5.3 / §5.7）。不列进来的后果是
        #    「声明了却不生效」——序列化器收了它们、这里却当成空载荷**静默忽略**。
        touches = {
            "taker_price", "deduct_mode", "deduct_value", "temp_amount", "bonus_amount"
        } & set(data)
        if not touches:
            # 空载荷：无副作用（与账号线「编辑空载荷无副作用」同口径）
            return api_response(
                data=self._row_payload(perf, request),
                msg=ignore_note,
            )

        # ⭐ 裁定 A.2：已结账闸的判据与 /assign/、/delete/、/adjust-amount/ 同源 = 模型属性 Performance.is_settled（settle_status / settled_at / settled_amount 三字段并集）⛔ 不是只看 settle_status 单列
        #——存量行里有「已结账但没写 settled_at」与「只有 settled_amount」两种形态、只看一列会把它们放行并真的改钱（E-0021 的形状）。
        if perf.is_settled:
            return api_response(
                data=None,
                msg="已结账的绩效不能改金额；请先撤销结账再改",
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )
        if perf.user_id is None:
            return api_response(
                data=None, msg="该绩效还没有接单人，不能改金额",
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        # ⭐ 「禁止结算」的人不许设扣点（业务方裁定「两处都拦」：结账与扣点都不许）；判据只有一处——读取层 apps.accounts.selectors.settle_forbidden_user_ids（结账侧调的是同一个函数）。
        # ⚠️ 只在本次请求带了扣点键时才查（改动 4 的裁定）：只改 taker_price（及其 temp_amount / 生效绩效重算）、只写 reason 的请求照旧放行——业务方只说了拦「设扣点」；已有的扣点值也不被本任务回溯清除；用例：tests/test_adjust_api.py::AdjustApiTests::test_a_frozen_taker_can_still_be_adjusted_without_a_deduction。
        # ⚠️ perf.user_id 到这一行一定非空（上面那条已拦掉空归属人）⇒ 不必再判。
        if {"deduct_mode", "deduct_value"} & set(data):
            if perf.user_id in settle_forbidden_user_ids([perf.user_id]):
                return api_response(
                    data=None,
                    msg="该用户已被禁止结算，禁止期间不能设置扣点",
                    code=ErrorCode.PERFORMANCE_STATE_INVALID,
                )

        # 判「这次请求到底改了什么」（只读、不改内存态、一行都还没写）。
        # ⚠️ 这里只是早退用的预判：同一个判据在加锁之后还要按加锁读回的那一份重算一次（见下面 transaction.atomic() 里的 Z1 段）——两次刻意分开、别合并：合并就只能拿锁前的旧值当判据。
        old_override, old_mode, old_value = perf.taker_price, perf.deduct_mode, perf.deduct_value
        old_temp, old_bonus = perf.temp_amount, perf.bonus_amount

        price_changed = "taker_price" in data and data["taker_price"] != old_override
        mode_changed = "deduct_mode" in data and data["deduct_mode"] != old_mode
        value_changed = "deduct_value" in data and data["deduct_value"] != old_value

        # ── 「这次请求到底改了什么」的判据（先算、再决定要不要进写库分支）──
        # ⭐ 裁定 A 的语义反转：改接单价那一支的写入（含「要不要连带写生效绩效」）整体交给共用的 recompute_order_amounts——判据与另三条写价路径（/hall-price/、/assign/ 带价、/adjust-amount/）同源、本端点不自己判一次（E-0021 的形状：同一事实两套判据）；旧代码在这里用 perf.review_status is not None 决定要不要同写 bonus_amount ⇒ ① COMPLETED 且 review_status is None 时生效绩效停在旧值（违反 F1）；② 非 COMPLETED 且 review_status 非空时反而写了生效绩效——两种都已按「订单当前状态 == COMPLETED」纠正（判据本体只在 recompute_order_amounts）。
        # ⚠️ 显式 temp_amount / bonus_amount（spec §5.3 的直写键）仍在共用重算之后覆盖 ⇒ 同一次请求里两者都给时、显式键赢（不经过提成算式）。
        explicit_temp = "temp_amount" in data
        explicit_bonus = "bonus_amount" in data
        # ⭐ R3（编排方裁定）：同一句文案、同一个口径（同 §4.2.2）；0 放行（归零）、非 0 拒。⛔ 用值比（!= 0）而不是真值——Decimal("0.00") 与负数都是合法边界（E-0021）；
        # ⛔ 位置在 explicit_bonus_changed 与「值没变 ⇒ 早退」之前：「送了一个非 0 值」本身就是一次直写意图、不因它恰好等于旧值而放行。
        if (
            explicit_bonus
            and data["bonus_amount"] != 0
            and perf.order.status != OrderStatus.COMPLETED
        ):
            return api_response(
                data=None, msg=BONUS_ONLY_ON_COMPLETED_MSG,
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )
        explicit_temp_changed = explicit_temp and data["temp_amount"] != old_temp
        explicit_bonus_changed = explicit_bonus and data["bonus_amount"] != old_bonus

        if not (
            price_changed or mode_changed or value_changed
            or explicit_temp_changed or explicit_bonus_changed
        ):
            # 值没变 ⇒ 同样无副作用（不要留一条「改了 88→88」的噪音留痕）
            return api_response(
                data=self._row_payload(perf, request),
                msg=ignore_note,
            )

        # ⚠️ 留痕的判据是「客户端显式送了这个键且值变了」、不是「这一列的值变了」：改 taker_price 会派生 temp_amount / bonus_amount（既有口径）、而那两列在既有实现里不写 field_name 行（只有 taker_price 一行）。
        # ⛔ 不许把派生也算成一行：tests/test_adjust_api.py 用 OperationLog.objects.get(order_no=…, action="performance.adjust") 取恰好一行、多写一行会让它 MultipleObjectsReturned（那是既有判据、不许改）。
        actor = getattr(request.user, "username", "") or ""
        reason = data.get("reason", "")
        from apps.business.order_writes import lock_order_row
        with transaction.atomic():
            # ⭐⭐ I4：先锁订单行、再锁绩效行（与 /assign/ 同向）。本支下面（apply_amount_to_effective_base）会写 Order.hall_price、订单行锁原先要等那次 save() 才拿到 ⇒ 与 /assign/ 方向相反、两笔并发会 AB-BA 成环。
            # 锁形状的唯一出处 = order_writes.lock_order_row。
            locked_order = lock_order_row(perf.order)
            # ⭐⭐ Z1：上面 perf.is_settled 是判定、下面是写入、两段之间不持锁 ⇒ 「判定通过 → 财务把这一行结掉 → 我们照写」会把金额改到已经结账的行上（TOCTOU）。修法照 /adjust-amount/ 已修形状（P1-10）：进事务后对绩效行做 select_for_update(of=("self",)) 再复查 is_settled。
            # of=("self",) 必须：_base_queryset() 带 select_related（订单/归属人）、锁整条 join 会把订单行与账号行一起锁住；.order_by("id")：单行也用同一条形状、批量扩写时锁序不漂；复查用加锁后读回来的那一份（locked）⛔ 不看 perf 那份内存副本；被拒时 transaction.set_rollback(True) 再 return（E-0022）。
            # ⚠️ 审计前值也改从 locked 取：加锁前别人可能已经改过金额、拿 perf 的旧值当 from_value 会在审计里写一句假话（I1）。
            locked = (
                Performance.objects.select_for_update(of=("self",))
                .order_by("id")
                .get(pk=perf.pk)
            )
            if locked.online_payroll_batch_id and not locked.is_settled:
                transaction.set_rollback(True)
                return api_response(msg="报酬已在在线付款单中", code=ErrorCode.PERFORMANCE_STATE_INVALID)
            if locked.is_settled:
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg="已结账的绩效不能改金额；请先撤销结账再改",
                    code=ErrorCode.PERFORMANCE_STATE_INVALID,
                )
            perf = locked
            # 把「这次改了哪些键」按**加锁读回的那一份**重新落成内存态 + 重算判据。
            old_override, old_mode, old_value = (
                perf.taker_price, perf.deduct_mode, perf.deduct_value,
            )
            old_temp, old_bonus = perf.temp_amount, perf.bonus_amount
            if "taker_price" in data:
                perf.taker_price = data["taker_price"]
            if "deduct_mode" in data:
                perf.deduct_mode = data["deduct_mode"]
            if "deduct_value" in data:
                perf.deduct_value = data["deduct_value"]
            price_changed = "taker_price" in data and perf.taker_price != old_override
            mode_changed = "deduct_mode" in data and perf.deduct_mode != old_mode
            value_changed = "deduct_value" in data and perf.deduct_value != old_value
            explicit_temp_changed = explicit_temp and data["temp_amount"] != old_temp
            explicit_bonus_changed = explicit_bonus and data["bonus_amount"] != old_bonus
            if perf.emp_type == EmpType.MAIN and locked_order is not None:
                approved_price = approved_penalty_price(locked_order)
                effective_price = perf.taker_price if perf.taker_price is not None else locked_order.hall_price
                if approved_price is not None and (
                    (price_changed and effective_price != approved_price)
                    or explicit_temp_changed
                    or explicit_bonus_changed
                ):
                    return api_response(msg=PENALTY_AMOUNT_LOCKED, code=ErrorCode.BUSINESS)
            if price_changed:
                # ⚠️ 派生那一步仍可能抛 `CommissionError`（提成模式不可算）⇒ 与既有一模一样地
                #    翻成业务码。异常在它自己那次 `save` **之前**抛出 ⇒ 库里还没有这一行的改动，
                #    但按 E-0022 的纪律仍显式回滚（别把「恰好没写」当成保证）。
                try:
                    recompute_order_amounts(perf)
                except CommissionError as exc:
                    transaction.set_rollback(True)
                    return api_response(data=None, msg=exc.message, code=exc.code)
            # ⚠️ 写入口只有一处（performance.write_performance_amounts）：两列一起定值 ⛔ 不要在这里直接赋值——归零的两个触发点共用同一个领域函数（spec §6 / Z6）。
            # ⚠️ 判据 is not None（E-0021）：显式键缺席时传 None = 这一列不改、不能传值（传 0 会把共用重算刚写出的值清掉）。
            write_performance_amounts(
                perf,
                temp_amount=data["temp_amount"] if explicit_temp else None,
                bonus_amount=data["bonus_amount"] if explicit_bonus else None,
            )

            # 只为校验（结果不落库：扣点是独立一项，B3）；上界按对标物原文。
            # ⚠️ 判据是「这次请求真的改了点什么」而不是「扣点自己变了」：上面的早退已经保证至少一项变了、所以这里无条件校验。三条理由：
            # ①改接单价会重算 temp_amount（既有、emp-emplist §2.1 的连带）——固定额扣点的上界就是 temp_amount、只校验「扣点自己变了」的话一次纯降价就能把这一行推进「扣点 > 预计绩效」的状态；那个状态下扣点额算不出来（deduct_amount 抛 AmountError）、列表只能整表拒绝；②⭐ 本批（D-0142）：直写变小 temp_amount 同样会越界（spec §5.3 的硬判据）；③校验在留痕与外层 save 之前；共用重算已经落过它自己那次 save ⇒ 越界时用 set_rollback(True) 整趟回滚（E-0022 的反面用法：预期失败显式回滚 + 回业务码）、响应不是「改了却报错」、也不会留下一条落不了账的绩效；⚠️ base=perf.temp_amount 用的是改后的值（上面那两步刚赋过）。
            try:
                deduct_amount(mode=perf.deduct_mode, value=perf.deduct_value, base=perf.temp_amount)
            except AmountError as exc:
                transaction.set_rollback(True)
                return api_response(data=None, msg=exc.message, code=exc.code)

            temp_changed = perf.temp_amount != old_temp
            bonus_changed = perf.bonus_amount != old_bonus

            # `field_name` 存**真实列名**（R8/R9）：模板里的中文由 `FIELD_LABELS` 派生，
            # 调用方不得直传显示名——否则同一件事会有两处可写、必然漂移。
            changes = []
            update_fields = []
            if price_changed:
                changes.append(("taker_price", old_override, perf.taker_price))
                update_fields.append("taker_price")
            if mode_changed:
                changes.append(("deduct_mode", old_mode, perf.deduct_mode))
                update_fields.append("deduct_mode")
            if value_changed:
                changes.append(("deduct_value", old_value, perf.deduct_value))
                update_fields.append("deduct_value")
            if explicit_temp_changed:
                changes.append(("temp_amount", old_temp, perf.temp_amount))
            if explicit_bonus_changed:
                changes.append(("bonus_amount", old_bonus, perf.bonus_amount))
            # ⚠️ update_fields 用值真的变了的判据（含派生）、changes 用显式送键的判据——两个判据刻意不同、别合并：派生值必须落库（既有行为）、但不该多出一行审计。
            # ⚠️ 显式键无条件进 update_fields：共用重算可能已经把这一列写成了别的值、此时「最终值 == 旧值」并不代表库里就是旧值——不补这一次 save 就会留下重算值（显式键的本意是「直写、不经过提成算式」、赢不了就成了一句假话）。
            if (temp_changed or explicit_temp) and "temp_amount" not in update_fields:
                update_fields.append("temp_amount")
            if (bonus_changed or explicit_bonus) and "bonus_amount" not in update_fields:
                update_fields.append("bonus_amount")

            perf.save(update_fields=[*update_fields, "updated_at"])
            # ⭐（客服预计绩效线，计划 §2 T1.2 的显式挂点 9）：上面那句显式 temp_amount 直写排在 recompute_order_amounts 之后 ⇒ 主挂点（挂在那个函数里）刷新客服预计时读到的还是旧的打手预计 ⇒ 必须在这里再刷一次、否则「改完预计绩效，客服预计还按旧值」。
            # ⚠️ 只对 MAIN 行调（与主挂点同一个判据）：本视图的 get_object() 走 _base_queryset()、只排掉 user IS NULL、不按 emp_type 收窄 ⇒ 一条 SALES 行也能到这里；对它调 refresh 会拿「客服算式」覆盖它自己那一列；⚠️ refresh_customer_estimate 永不抛 ⇒ 本端点的成功/失败语义不变。
            if str(perf.emp_type) == str(EmpType.MAIN):
                refresh_customer_estimate(perf.order)
            # **一行一个被改字段**（spec §5.2 的 R9 裁决）：一行放不下两对值，
            # 把它们合成一行会把「绩效金额」渲染成「生效绩效」的标签 ⇒ 审计日志里的错文案。
            for field_name, old, new in changes:
                record(
                    log_type=ACTION_LOG_TYPES["performance.adjust"],
                    action="performance.adjust",
                    actor=actor,
                    actor_id=request.user.pk,
                    # {绩效用户} → `target_name`（R11）；`target_id` 放数字 id
                    target_kind="user",
                    target_id=perf.user_id,
                    # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`）。
                    # `perf.user` 非空：本类的 `_base_queryset()` 已排掉 `user IS NULL`。
                    target_name=user_display_name(perf.user),
                    order_no=perf.order.order_no,
                    field_name=field_name,
                    from_value=old,
                    to_value=new,
                    reason=reason,
                )
        return api_response(
            data=self._row_payload(perf, request),
            msg=ignore_note,
        )


    @action(detail=True, methods=["post"], url_path="adjust-amount")
    def adjust_amount(self, request, *args, **kwargs):
        """改绩效金额（预计/生效）并反推发单价（spec §4.1/§4.6；D-0147 批 E 任务 2）。载荷 {amount_kind: "temp"|"bonus", amount: "<十进制串>", reason?: str}；成功 data = 该绩效行的最新形状（前端据此重渲染 ⛔ 不本地假装等于 V）。
        唯一真源 = Order.hall_price（发单价）、temp_amount 是它的派生值：预计绩效 = 有效基价 × 提成率 ÷ 100、有效基价 = taker_price（已设则优先）否则 hall_price；本端点把用户填的 V 反推成有效基价（performance.apply_amount_to_effective_base）、写回该落的那一列、再按新基价重算 temp_amount；amount_kind="bonus" 时另把 bonus_amount 写成 V、"temp" 时不动它。
        五条判据：已结账 ⇒ 1014（R6 的保护；判据与 /delete//assign/ 同源 = Performance.is_settled 的三字段并集，见 R-10b ⛔ 不是只看 settle_status 一列）；该行无归属人 ⇒ 算不出提成率 ⇒ 同样 1014（沿用既有「该绩效还没有接单人，不能改金额」那一支的码）；⭐ P1-10（计划 §10）：这条判据在写入前会再判一次——事务里先 Performance.objects.select_for_update(of=("self",)).order_by("id") 锁住绩效行、再拿加锁后读回来的那一份复查 is_settled（「判定通过 → 行被结掉 → 我们照写」那个窗口 ⇒ 仍然 1014、一行不写）。⭐ R3（编排方裁定）：amount_kind="bonus" 而订单当前状态不是 COMPLETED ⇒ amount != 0 时拒整笔（1014 + BONUS_ONLY_ON_COMPLETED_MSG）、amount == 0 放行（归零）；⛔ 判据用值比（E-0021）⛔ 位置在 apply_amount_to_effective_base 之前（只跳 bonus 那一写会静默改基价 = 改另一笔钱）。提成模式算不出（如 FIXED）⇒ CommissionError.code（与既有 /adjust/ 那条「算不出」支同形，spec §4.1 第 2 步「回业务码（不猜）」）。order.restrict_edit_taker_price 命中且该行已有 taker_price ⇒ HTTP 403（R-4b；落点正是 flag 保护的那一列、静默忽略会让接口说假话）；该行没有 taker_price（落点是 Order.hall_price）⇒ 照常放行。不动订单状态（R5）；⛔ 扣点相关的一概不碰；审计 = performance.adjust、一行一个真实被改的列（hall_price / taker_price / temp_amount / bonus_amount）、只写真的变了的（与既有 /adjust/ 同口径）。⚠️ 两道闸门在写库之前 ⇒ 被拒的请求一行都不写（T9 的判据）；⚠️ 写库与留痕同事务：留痕失败 ⇒ 金额改动整体回滚（E-0022 的反面用法、不是块内 return 退出）。"""
        perf = self.get_object()
        serializer = PerformanceAdjustAmountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        amount_kind = serializer.validated_data["amount_kind"]
        amount = serializer.validated_data["amount"]
        reason = serializer.validated_data["reason"]

        # ⛔ R-4b（控制方裁定；spec §4.6）：order.restrict_edit_taker_price 命中（can_edit_taker_price 为假）时按「有效基价落在哪一列」分两档：该行已有 taker_price ⇒ 落点正是这条 flag 保护的那一列 ⇒ HTTP 403、文案明说「你没有改接单价的权限」（⛔ 不许静默忽略：本端点是单目的（改金额 = 写有效基价）、忽略掉就等于接口返回成功而金额一个字没变 = 一句假话）；该行没有 taker_price ⇒ 落点是 Order.hall_price（不在这条 flag 的保护范围）⇒ 照常放行。
        # ⚠️ 与既有 /adjust/「只忽略 taker_price 这一个键」是有意的不同形：那个端点有多个键、忽略一个仍能成功；本条裁定不改 /adjust/ 的既有行为。
        # ⚠️ 位置在两道业务闸门与 transaction.atomic() 之前（与 /throw-to-hall、/assign/ 的价闸同序：授权先于状态与写入）⇒ 被拒时一行都没写。
        if perf.taker_price is not None and not can_edit_taker_price(request.user):
            raise PermissionDenied("你没有改接单价的权限")

        if perf.is_settled:
            return api_response(
                data=None,
                msg="已结账的绩效不能改金额；请先撤销结账再改",
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )
        if perf.user_id is None:
            return api_response(
                data=None,
                msg="该绩效还没有接单人，不能改金额",
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        # ⭐ R3（编排方裁定）：非「已完成」的订单不许直写非 0 生效绩效。⛔ 判据用值比（amount != 0）⛔ 不用真值 if amount:——Decimal("0.00") 与负数都是合法边界（E-0021）；R3 的字面口径是「0 放行、非 0 才拒」⇒ 负数也拒。
        # ⛔ 必须拒整笔（在 apply_amount_to_effective_base 之前）：只跳过 bonus 那一写的话、用户填的是「生效绩效」、结果静默改了发单价/接单价（改了另一笔钱）。
        # ⚠️ amount == 0 那一档不走这条拒绝：照旧走既有路径（0 会把基价反推成 0、连带改发单价——这是既有行为、本线不改它、spec §8-13 已登记）。
        if (
            amount_kind == "bonus"
            and amount != 0
            and perf.order.status != OrderStatus.COMPLETED
        ):
            return api_response(
                data=None, msg=BONUS_ONLY_ON_COMPLETED_MSG,
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        # 逐个记下库里的旧值：审计行要的是「真的变了的那一列 + 前后值」、而下面的写发生在同一个事务里、改完就取不到旧值了。
        # ⭐⭐ P1-10（计划 §10）：上面 perf.is_settled 是判定、下面是写入、两段之间不持锁 ⇒ 「判定通过 → 财务把这一行结掉 → 我们照写」会把金额改到已经结账的行上（TOCTOU）。修法：进事务后对绩效行做 select_for_update(of=("self",)) 再复查一次 is_settled。
        # of=("self",) 必须：_base_queryset() 带 select_related（订单/归属人）、锁整条 join 会把订单行与账号行一起锁住（别人的写会被无辜挡住）；.order_by("id")：单行也用同一条形状、免得以后有人把这里改成批量时忘了锁序（锁序统一为绩效行 → 钱包，计划 §0）；复查用加锁后读回来的那一份（locked）⛔ 不看 perf 那份内存副本——它正是判定阶段读到的旧状态；被拒时 transaction.set_rollback(True) 再 return（E-0022：预期失败不许裸退、否则 atomic() 会把这次请求里别的半截写入提交掉）；⚠️ 写与审计的「前值」也改从 locked 取：加锁前别人可能已经改过金额、拿 perf 的旧值当 from_value 会在审计里写一句假话。
        actor = getattr(request.user, "username", "") or ""
        from apps.business.order_writes import lock_order_row
        from apps.business.peer_sync import PEER_DIRECT_PENDING_MESSAGE
        try:
            with transaction.atomic():
                # ⭐⭐ I4：先锁订单行、再锁绩效行（与 /assign/ 同向）。apply_amount_to_effective_base 会写 Order.hall_price、订单行锁原先要等那次 save() 才拿到 ⇒ 与 /assign/（先锁 Order）方向相反、两笔并发会 AB-BA 成环。
                # 锁形状的唯一出处 = order_writes.lock_order_row。
                locked_order = lock_order_row(perf.order)
                if locked_order is not None and locked_order.peer_direct_pending is not None:
                    transaction.set_rollback(True)
                    return api_response(msg=PEER_DIRECT_PENDING_MESSAGE, code=ErrorCode.BUSINESS)
                locked = (
                    Performance.objects.select_for_update(of=("self",))
                    .order_by("id")
                    .get(pk=perf.pk)
                )
                if locked.online_payroll_batch_id and not locked.is_settled:
                    transaction.set_rollback(True)
                    return api_response(msg="报酬已在在线付款单中", code=ErrorCode.PERFORMANCE_STATE_INVALID)
                if locked.is_settled:
                    transaction.set_rollback(True)
                    return api_response(
                        data=None,
                        msg="已结账的绩效不能改金额；请先撤销结账再改",
                        code=ErrorCode.PERFORMANCE_STATE_INVALID,
                    )
                perf = locked
                order = perf.order
                if (perf.taker_price is None and locked_order is not None
                        and locked_order.status == OrderStatus.IN_PROGRESS
                        and not jidanpu_assignment_lock.lock_order_change(order.pk)):
                    return api_response(msg=jidanpu_assignment_lock.ORDER_BUSY, code=ErrorCode.BUSINESS)
                old_hall, old_taker = order.hall_price, perf.taker_price
                old_temp, old_bonus = perf.temp_amount, perf.bonus_amount
                apply_amount_to_effective_base(perf, amount)
                if amount_kind == "bonus" and perf.bonus_amount != amount:
                    perf.bonus_amount = amount
                    perf.save(update_fields=["bonus_amount", "updated_at"])
                changes = []
                if order.hall_price != old_hall:
                    changes.append(("hall_price", old_hall, order.hall_price))
                if perf.taker_price != old_taker:
                    changes.append(("taker_price", old_taker, perf.taker_price))
                if perf.temp_amount != old_temp:
                    changes.append(("temp_amount", old_temp, perf.temp_amount))
                if perf.bonus_amount != old_bonus:
                    changes.append(("bonus_amount", old_bonus, perf.bonus_amount))
                # `from_value` 为空（`None`）时 `record()` 归一化成空串——与既有 `/adjust/` 同形。
                for field_name, old, new in changes:
                    record(
                        log_type=ACTION_LOG_TYPES["performance.adjust"],
                        action="performance.adjust",
                        actor=actor,
                        actor_id=request.user.pk,
                        target_kind="user",
                        target_id=perf.user_id,
                        # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`）。
                        target_name=user_display_name(perf.user),
                        order_no=order.order_no,
                        field_name=field_name,
                        from_value=old,
                        to_value=new,
                        reason=reason,
                    )
        except (CommissionError, AmountError) as exc:
            # 提成算不出、或反推出的基价超出目标列位宽（R-10c）都是**预期失败** ⇒ 业务码；
            # `atomic()` 已随异常退出并回滚（⛔ 不在块内 `return`：那会把半截写入提交掉，E-0022）。
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(
            data=self._row_payload(perf, request)
        )

    @action(detail=True, methods=["post"], url_path="delete")
    def soft_delete(self, request, *args, **kwargs):
        """垃圾桶：软删该绩效行 + 同一步清接手人（spec §4.5/§4.6；D-0147 批 E 任务 3）。对外的 URL 是 POST /api/business/performances/<id>/delete/（url_path="delete"）；方法名取 soft_delete 而不是 delete——避免方法名撞 HTTP 动词（E-0072：那会让 DELETE 请求绕过 router 的 method_map 命中本方法；判据见 tests/test_drf_view_method_names.py）；与 adjust_amount ↔ adjust-amount 同形：动作名与 URL 段本来就不必逐字相同。载荷空（或 {}）；成功 data = {"deleted": <绩效主键>, "taker_cleared": true|false}。
        门（业务方裁定：不是 R-8b 的「额外要 19」那句；在任何写入之前判、被拒 ⇒ 传输层 403 且一行不写）：软删任何绩效行都要 order.submit_payout_price(1162) 这一道底门；要连带清接手人（当前接单人那条 MAIN 行）时也只要 1162——本端点在该动作上合法代行 order.clear_taker(19)、order.clear_taker 单独不再构成软删权。行类型门（R-9、同样在任何写入之前）：只接受 EmpType.MAIN（打单工资）的行；SALES（销售提成）⇒ 业务码 1014「只能删除接单人的绩效记录」（不是 403）。
        语义（同一事务、按序）：①已结账 ⇒ 1014（R6 的保护；判据是 settle_status / settled_at / settled_amount 三字段的并集、即模型属性 Performance.is_settled）；订单已软删的行更早一步落 1002（get_object() 走 _base_queryset 的口径）。②软删：写 deleted_at = now() / deleted_by = 当前操作人 username——复用 D-0144 既有两列 ⛔ 零迁移、不新增列；行留在库里。③若该行就是本单当前接手人那条 MAIN 行（emp_type=MAIN 且 user 非空）⇒ 同一步清接手人：apply_transition(order, UNASSIGNED, nodes=nodes_of(request.user) | _CLEAR_TAKER)（代行 19、理由见那一处注释）——既有 _on_pool_entry 钩子会做七列回退（user / review_status / bonus_amount / taker_price / claimed_at / hours / project）；⚠️ MAIN 行但 user 为空 ⇒ 只软删、不动接单人（taker_cleared=false）；SALES 行在上面那道行类型门已被拒、到不了这里。④审计：复用既有 performance.delete 模板（ACTION_LOG_TYPES 里早已登记、六个槽位全有真实来源）、target_kind="performance"、target_id=绩效主键；留痕记的是删除那一刻的值（第 3 步会把 bonus_amount 归零、之后再读就不是原值）。⚠️ 为什么不做硬删：MAIN 行硬删会让 /assign、/submit、on_order_completed /assign 缺行补行、/submit 容错读后按 403 拒、完成钩子容错——那三条不再 500；本端点仍是软删、仍拒绝 MAIN 行：硬删会丢掉那条行承载的接单记录。⚠️ 可预期失败用业务码、不裸 return 退出补偿路径（E-0022）：本动作在 atomic() 内先落了软删、若 apply_transition 因节点不够/状态陈旧抛 IllegalTransition、必须 set_rollback(True) 再回业务码——否则会提交一个「行已标删、接单人还在」的半截状态；不可预期的异常照旧响亮 500（不接）。"""
        perf = self.get_object()
        # ⭐ 业务方裁定（spec §4.5）：软删任何绩效行都只要 order.submit_payout_price(1162) 这一道底门；清接手人不额外要 19。原 R-8b 的两条判据在此收成一条（1162 是唯一底门）；原先那条「缺 19 ⇒ 403『你没有清除接单人的权限』」已按裁定删除。
        # ⛔ 类级门 PERFORMANCE_PERMISSIONS["soft_delete"] 仍是 {1162, 19} 的并集（粗门、表达不了「这一行是不是接单人那条」这个对象级条件）：只持 19 的账号走到这里会被下面这条判据拒 ⇒ 19 单独不构成软删权；⛔ 不把 1162 塞进 order_status.py 的目标节点集——那会经 GENERIC_TRANSITION_NODES 把「改成未分配」的权限摊给只有改钱权的人。
        # ⚠️ 位置在任何写入之前（与 /adjust-amount/ 的 R-4b 同序）：被拒时一行不写。
        if not has_node_now(request.user, "order.submit_payout_price"):
            raise PermissionDenied("你没有删除绩效的权限")
        # taker_cleared 不在锁外定（Z1、见下面事务内的加锁段）：那两列的取值可能与「软删那一刻」不同步、它同时决定要不要清接手人与响应里的那个布尔。
        # ⭐ R-9（控制方裁定；spec §4.5）：本端点只接受 EmpType.MAIN（打单工资）的行；SALES（销售提成）行 ⇒ 业务码拒绝（不是 403）、一行不写。裁定给的三条理由：①对标两处垃圾桶（绩效弹窗 #baroperating / 指派弹窗账单表 #baroperating3）都只出现在按接单人取数的视图里；②收窄后残差 RR-1（完成钩子往 SALES 行写提成）的「不可达」前提重新成立；③R-8b 那条「只持 19 的账号能删别人的 SALES 提成行」的权限面一并消失。
        # ⚠️ 位置：排在上面 order.submit_payout_price(1162) 那道底门之后（未授权者不该从业务码推知行类型）、Performance.is_settled 与任何写入之前；业务码取 PERFORMANCE_STATE_INVALID(1014)、与同一端点「已结账不能删」同码（两件事都是「这一行不允许删」）、靠 msg 区分。
        if perf.emp_type != EmpType.MAIN:
            return api_response(
                data=None,
                msg="只能删除接单人的绩效记录",
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )
        # ① 已结账 ⇒ 1014（`Performance.is_settled`）。另有两处只看 settle_status，见模块注释。
        if perf.is_settled:
            return api_response(
                data=None,
                msg="已结账的绩效不能删除",
                code=ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        actor = getattr(request.user, "username", "") or ""

        from apps.business.order_writes import lock_order_row
        with transaction.atomic():
            # ⭐⭐ I4：先锁订单行、再锁绩效行（与 /assign/ 同向）。本支下面会调 apply_transition（它自己 select_for_update 订单行）、订单行锁原先排在绩效行锁之后 ⇒ 与 /assign/ 方向相反、两笔并发会 AB-BA 成环。
            # 锁形状的唯一出处 = order_writes.lock_order_row。
            lock_order_row(perf.order)
            # ⭐⭐ Z1：上面的 perf.emp_type / is_settled 是判定、下面是写入、两段之间不持锁 ⇒ 并发结账能把钱结掉、这边照删。修法照 /adjust-amount/ 已修形状：事务内对绩效行 select_for_update(of=("self",)) 重取并复查 is_settled；被拒时 set_rollback(True) 再 return（E-0022）。
            # ⚠️ 行类型 / 接单人 / 留痕前值一并改从加锁读回的那一份取（I1）：锁前别人可能已清过 user / 改过金额、拿旧内存副本写审计会是一句假话。
            locked = (
                Performance.objects.select_for_update(of=("self",))
                .order_by("id")
                .get(pk=perf.pk)
            )
            if locked.online_payroll_batch_id and not locked.is_settled:
                transaction.set_rollback(True)
                return api_response(msg="报酬已在在线付款单中", code=ErrorCode.PERFORMANCE_STATE_INVALID)
            if locked.is_settled:
                transaction.set_rollback(True)
                return api_response(
                    data=None,
                    msg="已结账的绩效不能删除",
                    code=ErrorCode.PERFORMANCE_STATE_INVALID,
                )
            perf = locked
            order = perf.order
            taker_cleared = perf.emp_type == EmpType.MAIN and perf.user_id is not None
            # 留痕槽位在**写入之前**取：第 3 步的清接手人会把 `bonus_amount` 归零、把 `user` 清空。
            target_name = perf.user.get_username() if perf.user_id is not None else ""
            temp_at_delete, bonus_at_delete = perf.temp_amount, perf.bonus_amount
            settle_at_delete = perf.settle_status
            # ② 软删：复用 D-0144 的两列（不新增迁移、不新增列）。
            perf.deleted_at = timezone.now()
            perf.deleted_by = actor
            perf.save(update_fields=["deleted_at", "deleted_by", "updated_at"])
            # ⭐ ②′（spec §8.1 的第三条作废路径）：这一行被软删 ⇒ 它上面 pending 的提前结算申请作废（cancelled）；与软删同一个 atomic() ⇒ 要么都成、要么都不成。
            # ⚠️ 只作废 pending：settled 的申请意味着钱已按那次结账打过（而且已结账的行更早一步就被 1014 拒掉了）⇒ 不动。
            # ⚠️ 这一句不是下一段 apply_transition 的重复：taker_cleared=False 的行（user 早被清过）不会走 reset_taker_performance、而历史/脏行上仍可能挂着本口径上线之前的 pending 申请 ⇒ 软删是它们唯一的收口点；⚠️ 条件更新 ⇒ 与 apply_transition 那条路撞上时第二次命中 0 行（幂等）。
            cancel_pending_settle_requests([perf.pk])

            # ③ 当前接单人那条 MAIN 行 ⇒ 同一步清接手人（钩子做七列回退）。
            if taker_cleared:
                try:
                    apply_transition(
                        order, OrderStatus.UNASSIGNED,
                        # ⭐ 业务方裁定（spec §4.5）：本端点已用 order.submit_payout_price(1162) 授权（见上面的底门）、清接手人是该动作的一部分、故在此处代行 order.clear_taker(19)。
                        # 目标节点集 _TARGET_NODES[UNASSIGNED] 含 19 ⇒ 不代行的话、只持 1162 的人会在状态机那一步被拒（端点放行、状态机拒绝 = 上一批明令要消灭的双闸形状）；⛔ 不把 1162 塞进 _TARGET_NODES（那会经 GENERIC_TRANSITION_NODES 把「改成未分配」的权限摊给所有持改钱权的人、面比本动作大得多）。
                        # ⚠️ 只在本动作（删当前接单人那条 MAIN 行）上代行；/clear-taker/ 自己的权限门一个字不动——它的 19 仍是独立的门。
                        nodes=nodes_of(request.user) | _CLEAR_TAKER, actor=actor,
                        # ⭐（与截图删除端点同一类漏传，AST 扫 apply_transition 调用点时发现）：UNASSIGNED 是池内目标态 ⇒ 回池钩子会跑；从「已退单」过来时它写的两条冲销留痕是人为动作 ⇒ 必须把认证态主键传下去（⛔ 不是让域层反查 order.updated_by）。
                        # 判据：tests/test_refund_partial.py::ReversalAuditCarriesTheOperatorTests 的第二条整链用例（走本端点）。
                        actor_id=request.user.pk,
                        # ⭐ D2 例外②（R1，spec §4.1.2 的枚举表第②行）：调用这一刻该 MAIN 行的 user 仍非空（清人是回池钩子在第③步做的、而 taker_cleared 的判据就是 perf.user_id is not None）
                        # ⇒ 不开 allow_taker_release 的话、本功能会退化成「要删人先清人」死循环（清完人那条行已不再是「当前接单人」、删除语义已经不是用户当初想做的那件事，见 spec §4.1.2 的「附带风险」段）；该开关只跳过 D2 判据、其余一个字不放过。
                        allow_taker_release=True,
                    )
                except IllegalTransition as exc:
                    # 预期失败（节点不够 / 状态已被别人改过）⇒ 回滚这一趟的全部写入再回业务码。
                    transaction.set_rollback(True)
                    return api_response(
                        data=None, msg=str(exc), code=ErrorCode.BUSINESS
                    )

            # ④ 审计（与业务写入同事务）。
            record(
                log_type=ACTION_LOG_TYPES["performance.delete"],
                action="performance.delete",
                actor=actor,
                actor_id=request.user.pk,
                target_kind="performance",
                target_id=perf.id,
                target_name=target_name,
                order_no=order.order_no,
                from_value=temp_at_delete,
                to_value=bonus_at_delete,
                reason=settle_at_delete,
            )
        return api_response(
            data={"deleted": perf.id, "taker_cleared": taker_cleared}
        )

    @action(detail=True, methods=["post"])
    def review(self, request, *args, **kwargs):
        """审核绩效：通过 → 订单完成 / 驳回 → 订单回代练中（spec §5 / §6.2 / §8.1）。
        谁能审核 = 持 order.change_status_completed 的人（由 get_permissions() 的类级节点门判；③线取代了 legacy 的 business.review_performance）；⚠️ {REVIEWER} 职位门已删（spec §6 第 13 行）：业务方裁定接受「客服也能过审」——理由是对标没有审核点（D-0071「对标没有审核」+ D-0073 禁止自造节点）、且钱的钩子挂在 COMPLETED 上、与审核状态无关 ⇒ 放宽的只是「谁写那条审核留痕」。
        ⚠️ 来源态要判两层：订单在 PENDING_REVIEW 且绩效在「待审核」——只看订单状态不够：P2 遗留的 ISSUE/PAUSED → PENDING_REVIEW 是客服的边、能把一张没交过单（绩效上没有「待审核」）的单送进待验收、那时「审核通过」会把一个从未提交的金额标成已通过。⚠️ 本端点不再是「完成订单」的唯一入口（D-0071）：业务方裁定「对标没有审核、订单状态变成完成后绩效就生效」、所以通用 /transition/ 也接受 target=COMPLETED（按节点 17 授权，见 order_views.OrderViewSet.get_permissions 的 TRANSITION_TARGET_PERMISSIONS）；本端点保留——它是「走过审核动作」的那条路（写 review_status / reviewed_by / 审核留痕）、而审核不再是钱或结账的前置条件；⚠️ 状态、绩效、两条留痕同事务（spec §7.4 的同行要求）。⭐「已是已通过」这一档幂等（业务方裁定）：订单被重开回 PENDING_REVIEW 之后、绩效行上可能仍留着完成钩子写的「已通过」（R5 起进 COMPLETED 就写它）⇒ 此时再点「审核通过」不报错、不动钱、不写第二条留痕、直接回执成功；⚠️ 只放开这一档：「待审核」之外、又不是「已通过 + 本次也是通过」的形态（已驳回 / 空值 / 已经是「已通过」但本次请求是驳回）照旧走下面的来源态拒绝。"""
        perf = self.get_object()
        serializer = PerformanceReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        approved = serializer.validated_data["approved"]
        comment = serializer.validated_data["comment"]

        if perf.order.status != OrderStatus.PENDING_REVIEW:
            return api_response(
                data=None,
                msg=f"只有待验收的单可以审核，当前状态：{perf.order.status}",
                code=ErrorCode.BUSINESS,
            )
        # ⭐ 「当前已是已通过」这一档 ⇒ 幂等（业务方裁定）。⛔ 只放开这一档：还要求本次请求也是通过（approved）——「已是已通过但本次要驳回」不在裁定范围内、照旧走下面的来源态拒绝。
        # ⛔ 本档一个字节都不写（不迁状态、不动钱、不留痕），理由是两条硬约束：①「不重复生成钱」：完成钩子挂在唯一写入口 apply_transition 上（order_status._run_target_hooks）⇒ 再迁一次 COMPLETED 会重跑 settlement_hooks._activate_taker_performance 的 bonus_amount := temp_amount、而重开之后 temp_amount 可能已经变了（/adjust-amount/ 的 amount_kind="temp" 不受 R3 闸限制、只有 "bonus" 那档被 R3 判订单状态）⇒ 那会改钱、与裁定的「钱不变」冲突；②「不重复写日志」：本档不调 record(...)。
        # ⚠️ 因此订单不会因此被重新推到 COMPLETED：本档与进入时的状态逐字相同（幂等 = 不再做完成动作）；要重新完成走 POST /transition/ {target: COMPLETED}（D-0071 起它就是合法的完成入口）⛔ 不是本端点顺手代做。
        if perf.review_status == ReviewStatus.APPROVED and approved:
            return api_response(
                data=self._row_payload(perf, request)
            )
        if perf.review_status != ReviewStatus.PENDING:
            return api_response(
                data=None,
                msg=f"该绩效不在待审核状态（当前：{perf.review_status or '未提交'}），不能审核",
                code=ErrorCode.BUSINESS,
            )

        new_review_status = ReviewStatus.APPROVED if approved else ReviewStatus.REJECTED
        target = OrderStatus.COMPLETED if approved else OrderStatus.IN_PROGRESS
        actor = getattr(request.user, "username", "") or ""
        with transaction.atomic():
            perf.review_status = new_review_status
            perf.reviewed_by = actor
            perf.reviewed_at = timezone.now()
            perf.save(
                update_fields=["review_status", "reviewed_by", "reviewed_at", "updated_at"]
            )
            try:
                apply_transition(
                    perf.order, target, nodes=nodes_of(request.user), actor=actor
                )
            except IllegalTransition as exc:
                transaction.set_rollback(True)
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            except (CommissionError, AmountError) as exc:
                # ⚠️ P4-6：过审 → COMPLETED 会触发 settlement_hooks 确定客服提成、而它要读销售客服的利润提成率（commission_percent_of、可能抛 CommissionError）并算利润（settlement_amount.profit_of、可能抛 AmountError）。
                # 这两类是预期失败、按 E-0010 翻成业务码并显式回滚——此刻打手行的 review_status = 已通过 已落库、块内正常 return 会把「审过了但订单没完成」提交（E-0022）。
                transaction.set_rollback(True)
                return api_response(data=None, msg=exc.message, code=exc.code)
            if not approved and perf.emp_type == EmpType.MAIN and perf.deleted_at is None:
                from apps.business.wxpusher_events import queue_order_notice

                queue_order_notice(perf.order, perf.user_id, "review_rejected", perf.reviewed_at, reason=comment)
            # `/review` 是**一个**业务动作 ⇒ 新表**一行**（spec §5.2 的落点只有本端点）。
            # 旧实现写了两行（`REVIEW` + `STATUS_CHANGE`）：那条状态变更不是独立动作，
            # 而是本动作的**后果**，审核结果由模板的 {通过/驳回}（`to_value`）表达。
            record(
                log_type=ACTION_LOG_TYPES["performance.review"],
                action="performance.review",
                actor=actor,
                actor_id=request.user.pk,
                target_kind="order",
                target_id=perf.order.order_no,
                order_no=perf.order.order_no,
                # {备注} → `reason`（B3，纯值）；中文标签一律由模板正文提供（R10）
                field_name="review_status",
                from_value=ReviewStatus.PENDING,
                to_value=new_review_status,
                reason=comment,
            )
        return api_response(
            data=self._row_payload(perf, request)
        )
