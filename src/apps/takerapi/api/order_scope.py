"""打手侧**对外**端点的**范围判据**：**「他是这张单的当前接手人」**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5.3-2/§9-14；服务我的单/查单/改状态/进度图四条端点）。
承重：⛔ 这份判据**不是**"游戏范围内"（T5 抢单取单口才走 `game_scope`——两行反例：范围内被别人接手的单、范围外他自己接手的单；换用 T5 那套 ⇒ `ScopeIsTheCurrentTakerNotTheGameScopeTests` 必红）；⛔ 刻意不加 `scope_queryset`/`game_scope`（加进来=第一份判据被第二份顶掉）；「不存在/不是他接的/已软删」三者共用 `TAKER_ORDER_NOT_FOUND_MSG` 同形（防枚举单号）；孤儿订单显式判 `perf is None` ⇒ `1002` ⛔ 不许 `AttributeError` 的 500。
判据逐句写在 `claimed_perf_of` 函数体里（可读、可被用例单独弄红）；幂等回放不重查本判据（如实登记：不写库不泄信息，事实以查单/我的单为准；⛔ 不改共享的 `idempotency.py`）。"""
from __future__ import annotations

from django.db.models import OuterRef, Subquery

from apps.business import selectors
from apps.business.models import EmpType, Order, Performance

#: `1002` 那一句（spec §5.3-2/§5.4）。⭐ **同形**承重：不存在/不是他接的/已软删三成因共用这句 ⇒ 防枚举单号。
#: 与大厅那条（`NOT_IN_HALL_MSG`）刻意不同：那句是"不在**大厅**"（抢单语义），这句是"不是**你的单**"（接手人语义）。
TAKER_ORDER_NOT_FOUND_MSG = "该订单不存在或不是你的单"

#: 「我的单」行的接单时刻注解名。`Order` 上没有接单时刻列（只在 `Performance.claimed_at`）⇒ 必须注解上来。
#: ⛔ 名字不带 `order__`：注解名里 `order__` 是非法字段路径、且会误导读者以为是跨表路径。
CLAIMED_AT = "claimed_at"


def claimed_order_qs(taker):
    """**他当前接手的活单**底集（`Order` queryset）——「我的单」列表端点的唯一取数口。取数三件套（口径照内部「我接的单」）：`performances__user=taker`（接手人=绑定账号，同时排除别人接的单）+ `performances__emp_type=MAIN`（接单人身份，非冗余：同人在别的单可能只有销售提成行）+ `deleted_at__isnull=True` 写在**本表**（⛔ 不是 `order__` 前缀——那会 `FieldError`；与默认 manager 过滤重叠但保留=spec 的显式判据）。
    主体是 `Order` ⛔ 不是 `Performance`：列表要一次取齐一整行 17 键（否则 N+1）；接单时刻用**相关子查询**注解（子查询自带 MAIN 条件，`uniq_order_main_taker` 保证最多一条；⛔ 不靠"join 被 annotate 复用"这个未经实测的行为，先例=`effective_taker_price_expression` docstring）。
    孤儿订单天然不在结果里——与 `claimed_perf_of` 的 `perf is None` 同源（同一不变量 ⛔ 不是两套判据）。"""
    claimed_at = (
        Performance.objects.filter(order=OuterRef("pk"), emp_type=EmpType.MAIN)
        .values("claimed_at")[:1]
    )
    return Order.objects.filter(
        performances__user=taker,
        performances__emp_type=EmpType.MAIN,
        deleted_at__isnull=True,
    # ⭐ 评审 Important 7：隐藏单不是「他接的单」⇒ 不进「我的单」列表
    # （防御性：生产上它没有 MAIN 行，但显式判据才是承重的那条）。
    ).exclude(hall_card__isnull=False).exclude(
        upstream_hall_card__isnull=False
    ).annotate(**{CLAIMED_AT: Subquery(claimed_at)})


def claimed_order_with_price_qs(taker):
    """**他当前接手的活单**+**生效接单价注解**——装配行载荷的取数口。单独一个函数的理由：列表与**查单**装配的是**同一行**（§5.2 单数版与列表逐键同形）⇒ 两处必须注**同一个算式**；⛔ 不许哪个端点自己写一遍 `Coalesce`——那会让"看到的价"与"大厅筛出的价"在覆盖价生效后静默分叉（`selectors.py` 逐条写明代价）。
    生效价算式只有一处出处（`selectors.effective_taker_price_expression()`），本函数只注上去 ⛔ 不重写算子；
    主体是 `Order`（一条 SQL 取齐一整行；⛔ 不用 `Performance` 实例逐个注——`Coalesce` 不能赋给模型实例属性，`money()` 里炸 `TypeError`，执行期实测）。"""
    return claimed_order_qs(taker).annotate(
        **{selectors.EFFECTIVE_TAKER_PRICE: selectors.effective_taker_price_expression()}
    )


def claimed_perf_of(taker, order_no):
    """按单号取**他当前接手的那条 MAIN 绩效行**；取不到/不是他的/已软删 ⇒ `None`。返回绩效行（⛔ 不是 `Order`）：四句判据的载体就是那一行，且查单要的 `claimed_at` 在它上面（判据一跳顺手带出）。
    ⚠️ 查单端点**仍是两次查询**（判据一次+装配一次；`claimed_at` 在绩效行、注解在 `Order`），两次间的竞态由 `MyOrderDetailView.get` 接 `Order.DoesNotExist` 翻 `1002`。⛔ `None` 是唯一失败形状（调用方一律翻 `1002`+同一句）；⛔ 本函数不抛 `DoesNotExist`/`AttributeError`——孤儿订单靠"查不到那一行"落 `None`（spec §9-14 ⛔ 不许 500）。
    四句判据**逐句写在函数体里**（⛔ 不藏进底集 SQL：判据必须可读、且能被一条用例单独弄红——`SoftDeletedOrderTests`/`ScopeIsTheCurrentTakerNotTheGameScopeTests`/`OrphanOrderTests` 各钉一句）；取数用 `order__order_no=…`（端点上只有单号 ⛔ 不先查一次订单）。"""
    perf = (
        Performance.objects.filter(
            order__order_no=order_no,
            emp_type=EmpType.MAIN,
        )
        # ⭐ 隐藏单（镜像卡载体）没有真实订单语义 ⇒ 与「不存在」同形（评审 Important 7）。
        # ⚠️ 写成 `exclude(...=False)`：结构网 `HallShadowExclusionNetTests` 同时校验
        # 「方法是 exclude + 比较值是 False」（`filter(isnull=True)` 语义相同但形状不同）。
        .exclude(order__hall_card__isnull=False)
        .exclude(order__upstream_hall_card__isnull=False)
        .select_related("order")
        .first()
    )
    if perf is None:
        # ⭐ **孤儿订单**（这张单**一条 MAIN 绩效行都没有**）落这一支：spec §9-14 要求它
        # 回 `1002`，⛔ **不是** `AttributeError` 的 500、也⛔ 不是"什么都放行"。
        return None
    if perf.user_id != taker.pk:
        # 单是**别人**接的（含"还没人接"）⇒ 与"不存在"同一个出口（spec §5.3-2 的"同形"）。
        return None
    if perf.order.deleted_at is not None:
        # 已软删的单不许被查 / 被改（spec §5.3-2 末句：软删判据**不是**只写在「我的单」上）。
        return None
    return perf


def claimed_order_of(taker, order_no):
    """按单号取**他当前接手的那张活单**（`Order` 实例）；取不到/不是他的/已软删 ⇒ `None`。
    `claimed_perf_of` 的薄投影（改状态与进度图的领域口要 `Order` 实例）；⛔ 不重复任何判据（一句 return）——判据只有 `claimed_perf_of` 那一份。"""
    perf = claimed_perf_of(taker, order_no)
    return None if perf is None else perf.order
