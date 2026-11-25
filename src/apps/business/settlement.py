"""结账 = 财务勾一批**已完成订单产生的**绩效、一次结掉（一个事务六步，spec §4.3）。不再要求 `review_status=已通过`（D-0071 取消审核闸门），但「绩效已生效」由两条真实判据承担（D-0071/D-0069 口径本身）：`order__status=COMPLETED`（缺它时代练中的单被结掉、`/submit` 回写金额不看 `settle_status` ⇒ 那笔钱永久结不了，评审实测 30 元路径）与 `user__isnull=False`（无归属人必须结不了账，前端闸门只是体验层）。
六步顺序不可改（spec §4.3 两条「不能改回去」）：第 3 步 fail-closed 必须**逐 id** 判定（总数比对表达不了「已结账行+新增未结账调整」的合法情形 ⇒ 已结账绩效被追加调整永远结不掉）；第 4 步（净额+`1011`）必须早于第 5 步（标记结账）——`adjustments` 在 UPDATE 前求值，否则 `1011` 成死代码（spec §17.1 #4）；第 5 步 `update()` 校验受影响行数（E-0022 同族，防「假成功+重复留痕」）。
求和按**绩效归属人**分组（业务方原话口径，拟定解读、登记待确认，改 `_group_totals` 一处）；⛔ 本模块不加任何 `≥0` 校验（D-0015，净额为负是 `1011`）；P4-FE-4：逐行实结金额快照进 `settled_amount`（语义=结账那一刻的应付净额，走 `bulk_update` 单独写、不并进第 5 步单条 `update()`；并发守门仍由那条 `update()` 的 rowcount 负责）；单独成模块的原因=结账同改三处而库约束表达不了联合一致性，不加事务失效模式是脏数据不是报错（spec §8）。"""
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.accounts.display import user_display_name
from apps.accounts.selectors import settle_forbidden_user_ids
from apps.accounts.wxpusher_notifications import enqueue_worker_notice
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import split_group
from apps.business.game_scope import game_scope
from apps.business.models import (
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.performance_amount import AmountError
from apps.business.settlement_amount import payable_net
from apps.business.settlement_persistence import SettleError, persist_manual_settlement
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset

#: 留痕的前后值字面量。绩效行与调整记录的 `settle_status` 是同一套取值（spec §3.3）。
SETTLE_FROM = "未结账"
SETTLE_TO = "已结账"

#: 「池内」两态（D-0032）：订单还在池里=这一轮不成立，名下未结账调整不该被结掉（钱② 残留，业务方裁定 B）。
#: ⛔ 只这两态；「不在这两态」≠ 一律可结（B-1 收紧）：调整记录还要满足「行已结账 ∨ 订单已完成」——
#: 行已结账的追加调整才是 spec §3.3 坏账出口，行未结账的要拒（否则金额进了「已结账」却永远不进实结金额）。
POOL_ORDER_STATUSES = (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB)


@dataclass(frozen=True)
class SettleResult:
    """本次结账真正标记成「已结账」的绩效 id 与调整记录 id（列表不是计数：调用方要能答「到底结了哪几笔」，与「绝不静默部分成功」配套）。
    ⭐ `skipped_performances`（业务方裁定 Q3=A）：本批已是目标状态因此被跳过的绩效 id；跳过是成功路径（200+`code=0`，业务方原话「已经是要更改的状态直接跳过即可不用报错」）。⚠️ 与「没查到」必须可区分：不存在/越权/订单未完成/无归属人/被冻结的未结账 id **不在这里**，照旧让整批 `1006` 失败；已结清行不适用那几项（本批对它是空操作），见 `settled_ids_without_pending_adjustments`。"""

    settled_performances: list
    settled_adjustments: list
    skipped_performances: list


def performances_with_unsettled_adjustments(performance_ids, *, user):
    """这批绩效里名下还有未结账调整记录的 id（「还有账没结」这个事实）。⚠️ 刻意不含第 2 步两个闸门（订单状态/禁止结算）：
    本函数答「库里还有没有账没结」而非「这次结不结得掉」，两个消费方（拒绝文案「已完全结清」支、跳过判据）要的正是这个事实，
    带闸门会让两处各说一套。⚠️ 过一次 `scope_queryset`+`game_scope`：只答「可见的这行」，不收窄会给出「已完全结清」这类假话。"""
    return set(
        scope_queryset(
            user,
            PerformanceAdjustment.objects.filter(
                performance_id__in=performance_ids,
                settle_status=SettleStatus.UNSETTLED,
                # ⭐ R-8e：读调整记录时同判父绩效行软删。当前无观测影响（消费方只在 `visible` 过滤过的行上用它），
                # 补它是与 `adjustment_views` 读链同形、免得下个读者以为漏了。
                performance__deleted_at__isnull=True,
            ),
            # ⭐「按游戏权限收窄」（`performance__order__game_id`，按本模型选）。
            scope_filter=game_scope("performance__order__game_id"),
        ).values_list("performance_id", flat=True)
    )


def settled_ids_without_pending_adjustments(performance_ids, *, user):
    """这批绩效里已完全结清（可见 ∧ `settle_status=已结账` ∧ 名下无未结账调整，与拒绝文案同源）的 id。「无未结账调整」这条减法是承重的：
    已结账行+追加的未结账调整走坏账出口（spec §3.3），⛔ 不能被当成「已完全结清」跳过——池内那一支只靠本减法挡住（变异 M1 实测：删掉 ⇒ 静默跳过少结账，
    `SettleAdjustmentsLegTests._assert_rejected_in_pool` 红；「订单已完成」那一支由调用方 `missing` 过滤兜住，删掉减法仍绿，照实登记、非「缺一不可」）。⚠️ 入参契约：调用方传「两条腿都没命中」的 id（`missing`），本函数不再过滤。"""
    settled = set(
        scope_queryset(
            user,
            Performance.objects.filter(
                id__in=performance_ids,
                settle_status=SettleStatus.SETTLED,
                # ⭐ D-0144（C11 同形物）：已软删单的绩效行在这里与「不存在或不可见」同一出口（跨关系 JOIN 不经过 `Order` 默认 manager）。
                order__deleted_at__isnull=True,
                # ⭐ R-6：单条软删的绩效行同判——不补的话已被垃圾桶软删的 id（订单还活着）会被念成「已结清、跳过」，
                # 而它在结算列表里根本看不见（一句假话）。
                deleted_at__isnull=True,
            ),
            scope_filter=game_scope("order__game_id"),
        ).values_list("id", flat=True)
    )
    return settled - performances_with_unsettled_adjustments(
        performance_ids, user=user
    )


@dataclass(frozen=True)
class SettleBatch:
    """`_resolve_batch` 的解析结果：结账与预览共用的同一份口径（P1-2/P1-10）。`rows`/`adjustments` 是第 2 步两条腿的**惰性** QuerySet（第 5 步
    `update()` 谓词在库里重判=E-0022 同族并发守门）；`skipped_performances`/`unqualified` 是第 3 步三分的后两分；`already_traced` 是 `rows` 腿里
    `is_settled` 为真但状态列仍「未结账」的脏行（结账侧整批 `1006`，⛔ 不写成跳过）。⭐ `settleable_ids` = 两条腿并集减脏行：预览金额只累加这些行（⛔ 别退化成只取 `rows` 腿——坏账出口那条腿漏了会把该结的钱算少）。"""

    rows: object
    adjustments: object
    row_ids: set
    adjustment_perf_ids: set
    skipped_performances: list
    unqualified: list
    already_traced: list

    @property
    def settleable_ids(self):
        """这一趟真的会被结掉的绩效 id（预览据此累加金额，P1-2）。"""
        return (set(self.row_ids) | set(self.adjustment_perf_ids)) - set(
            self.already_traced
        )


def _resolve_batch(ids, *, user, lock=False):
    """第 2–3 步：把 id 解析成「要结的两类东西+跳过+不合格」（P1-2/P1-10）；`ids` 契约=已去重已排序，本函数不再排。
    `lock=True`（结账）给 `rows` 加 `select_for_update(of=("self",))+order_by("id")` 并算脏行——调用方必须在事务里；`lock=False`（预览）纯只读 ⛔ 不加锁。
    ⛔ 本函数不抛 `SettleError`（预览端不能因一条不合格整页 500，处置归调用方）；⚠️ ⛔ 不要把第 2 步条件复制到别处：预览过去各写一份、数字与点下去的结果对不上（P1-2 现场），加/改闸门只改这里。"""
    # 第 2 步：解析本批要结的两类东西。⚠️ `review_status` 过滤已删（D-0071），⛔ 别加回（=非「已通过」行永远结不了账）；「绩效已生效」由两条口径承担：`order__status=COMPLETED`（缺它时代练中单被结掉、`/submit` 回写金额 ⇒ 钱永久结不了，评审实测 30 元路径）与 `user__isnull=False`（D-0069，前端闸门只是体验层）。⭐ 被冻归属人整批拒（判据唯一出处 `settle_forbidden_user_ids`，`/adjust` 同一函数）；用「先算再 exclude」而非 `filter(...=False)`——INNER JOIN 会把没有档案行的人（seed_tenant 的 admin）一并丢掉误挡（用例 `tests/test_settle_api.py::SettleForbiddenTests::test_an_owner_without_a_profile_row_is_not_blocked`）；⭐ 两条腿都要排被冻人（I-1，只排 `rows` 会从调整腿漏结、绩效行留未结账半态）；代价（业务方「冻结期间所有账单都无法结算」）：被冻人的负数调整也结不了，不做「只拒正数」分叉（对标没写符号语义，自创=没人要求的新规则）。
    forbidden_user_ids = settle_forbidden_user_ids(
        Performance.objects.filter(id__in=ids, user__isnull=False).values_list(
            "user_id", flat=True
        )
    )
    waiting_legacy_sales = split_group.unsettled_legacy_sales_waiting_ids(ids)

    # ③ `.exclude(...=True)`——钱⑤（业务方裁定）：账户级「禁止结算」（对标 `isProhibitSettStatus`）。⛔ 别写成 `.filter(...=False)`：实测两者 SQL 不同——filter 是 INNER JOIN、没档案的用户（seed_tenant 的 admin）整批被筛掉；exclude 是 LEFT OUTER JOIN、缺档案时条件为 TRUE 照样放行（「没档案」与「被标禁止结算」是两件事；判据 `SettleForbiddenGateTests` 含「没档案仍能结账」两条）。⭐ 闸门两条腿都要有（只加 `rows` 时被冻人的未结账调整仍会被结掉=少发钱）；⚠️ `forbidden_user_ids` 与 `settle_forbidden=True` 是同一件事的两种写法、都保留（前者让拒绝文案能指认到人）。
    rows = scope_queryset(
        user,
        Performance.objects.select_related("order", "user").filter(
            id__in=ids,
            settle_status=SettleStatus.UNSETTLED,
            order__status=OrderStatus.COMPLETED,
            user__isnull=False,
            order__deleted_at__isnull=True,
            deleted_at__isnull=True,
        )
        # ⭐ 镜像卡派生的隐藏单上的绩效行不许结账——不是本租户的一笔生意（没有客户价/收入），
        # 结了就是把「同步给下家大厅的载体」当真单发钱；跨关系 JOIN 不经过 `Order` 默认 manager ⇒ 必须显式补，落在第 3 步 fail-closed（整批 1006）。
        # ⚠️ 形状必须是 `exclude(...=False)`（结构网 `HallShadowExclusionNetTests` 同时校验方法与比较值；`filter(isnull=True)` 语义相同但网认不出来）。
        .exclude(order__hall_card__isnull=False)
        .exclude(order__upstream_hall_card__isnull=False)
        .exclude(user_id__in=forbidden_user_ids)
        .exclude(pk__in=waiting_legacy_sales)
        .exclude(user__staff_profile__settle_forbidden=True),
        # ⭐「按游戏权限收窄」（`order__game_id`）：看不见的行不在 `rows` ⇒ 第 3 步 fail-closed 算不合格（与「不存在」同出口）。
        scope_filter=game_scope("order__game_id"),
    )
    # ⚠️ 这条腿不看绩效行自身结账状态：已结账行被追加的新调整要能结掉（spec §3.3 坏账出口）；冻结判据在这条腿出现两次是
    # 有意的等价冗余（与 `rows` 腿同一条裁定：`forbidden_user_ids` 让拒绝文案能指认到人，`settle_forbidden=True` 同结果、都 LEFT-JOIN 安全）。
    # ⭐ 钱②残留+裁定 B（完整论证外移 docs/code-comment-notes.md）：调整不是独立的一笔钱，只有落进实结金额才算付出去 ⇒ 口径=Q 析取「行已结账 ∨ 订单已完成」，再排池内两态与被冻归属人。初版按裁定 A（`performance__order__status=COMPLETED`）被实测推翻——全额退款单上把客服提成冲回 0 的冲减会永久结不掉（`tests/test_refund_full.py::FullRefundSettledPriorDeductionTests::test_the_appended_correction_can_still_be_settled`）；B-1 堵「行未结账+订单非池内也未完成」格（探针实测：`-5.00` 从未进实结金额、settled_amount 20.00 而非 15.00，多发 5.00）。⛔ 代价（已登记勿放宽）：池内两态上的调整结不掉（`1006`）。两支各有一条用例承重：① `test_an_unsettled_row_of_a_completed_order_settles_with_its_adjustment`；② `test_a_refund_correction_on_a_settled_row_of_a_refunded_order_settles`。⭐ 同批评审 A-1：本腿 `.exclude(...)` 与 `rows` 腿同形（LEFT OUTER JOIN 缺档案仍放行），⛔ 不许写成 `.filter(...=False)`。
    adjustments = scope_queryset(
        user,
        PerformanceAdjustment.objects.select_related(
            "performance__order", "performance__user"
        ).filter(
            performance_id__in=ids,
            settle_status=SettleStatus.UNSETTLED,
        ).filter(
            Q(performance__order__status=OrderStatus.COMPLETED)
            | Q(performance__settle_status=SettleStatus.SETTLED)
        ).filter(
            # ⭐ D-0144（C12，同一函数第二处）：已软删单上的调整记录不参与结账。
            performance__order__deleted_at__isnull=True
        ).filter(
            # ⭐ R-6：绩效行自己被软删时它名下调整同样不参与——`rows` 腿已排该行，这腿若放行
            # 第 3 步会算「合格」、第 5 步只结调整留半态（判据 `test_the_adjustments_leg_is_refused_too`）。
            performance__deleted_at__isnull=True
        ).exclude(
            performance__order__status__in=POOL_ORDER_STATUSES
        ).exclude(
            performance__user_id__in=forbidden_user_ids
        ).exclude(
            performance_id__in=waiting_legacy_sales
        ).exclude(performance__user__staff_profile__settle_forbidden=True),
        # ⭐「按游戏权限收窄」（`performance__order__game_id`，与绩效腿不同路径）：这条腿承重——第 3 步把 `adjustment_perf_ids`
        # 也算「合格」，不收窄时别人游戏行上的未结账调整会被静默结掉、请求方还能从 `settled_adjustments` 看到它的 id。用例
        # `tests/test_scope_performance.py::test_settle_of_another_games_performance_is_rejected`。
        scope_filter=game_scope("performance__order__game_id"),
    )

    # 第 3 步：fail-closed 逐 id 判定（非总数比对）⇒ 整批回滚+`1006`。⚠️ `values_list()` 是对这两个 QuerySet 的克隆求值、不缓存它们本身
    # ——第 4 步必须还能在标记结账前拿到未结账调整（见模块 docstring）。⭐ P1-10：结账侧要锁绩效行（否则 TOCTOU 与 `/adjust-amount/` 互踩）；
    # `of=("self",)` 必须（两条腿带 `select_related`+外连接，锁整条 join 会连订单/账号表一起锁）、`order_by("id")` 必须（取锁顺序不定会死锁，计划 §10）；锁只在 `lock=True`（结账路径）加：预览是只读端点 ⛔ 不许拿锁。
    if lock:
        rows = rows.select_for_update(of=("self",)).order_by("id")
    # ⚠️ 这里就把 `rows` 求值到内存：加锁与「读到的最新状态」是同一件事
    #    （`select_for_update` 之后那条 SELECT 才拿锁，拿锁之前读到的都不作数）。
    locked_rows = list(rows)
    row_ids = {performance.id for performance in locked_rows}
    # ⭐ P1-10 复查 `is_settled`：`rows` 腿只看 `settle_status=未结账`，而 `is_settled` 是三字段并集（还看 `settled_at`/`settled_amount`）
    # ⇒ 「状态列未结账、却留结账痕迹」的脏行会混进来，这次不许再被结（第 5 步会覆盖已发过钱的快照）。
    # ⛔ 处置不是跳过（计划 §10）：调用方见到非空就整批 `1006`。
    already_traced = [
        performance.id for performance in locked_rows if performance.is_settled
    ]
    adjustment_perf_ids = set(adjustments.values_list("performance_id", flat=True))
    # ⭐ 第 3 步前半（业务方裁定 Q3=A）：已是目标状态 ⇒ 跳过不报错。「已完全结清」判据唯一出处 `settled_ids_without_pending_adjustments`
    # （与拒绝文案同源）。⚠️ 只对两条腿都没命中的 id 问这一次（正常路径每 id 都在 `row_ids` ⇒ 不多打查询；且这问题只对没命中的 id 才有意义）；
    # ⚠️ 其余不合格一律照旧整批拒绝（错误码文案不改）——跳过不是「凡是结不了的都跳过」，那会把真失败伪装成成功；⚠️ 第 4 步 `1011` 与跳过是两个层面（跳过的行不参与组和，那笔钱上一轮已结清）。
    missing = [
        perf_id
        for perf_id in ids
        if perf_id not in row_ids and perf_id not in adjustment_perf_ids
    ]
    already_settled = (
        settled_ids_without_pending_adjustments(missing, user=user) if missing else set()
    )
    skipped_performances = [
        perf_id for perf_id in missing if perf_id in already_settled
    ]
    unqualified = [
        perf_id for perf_id in missing if perf_id not in already_settled
    ]
    return SettleBatch(
        rows=rows,
        adjustments=adjustments,
        row_ids=row_ids,
        adjustment_perf_ids=adjustment_perf_ids,
        skipped_performances=skipped_performances,
        unqualified=unqualified,
        already_traced=already_traced,
    )


def _reject_reasons(unqualified_ids, *, user):
    """给每个不合格 id 各写一句具体原因（`{绩效 id: 原因}`，spec §4.3 第 3 步）。⚠️ 原因的唯一实现：`_reject_message`（结账一句话）与预览 `errors` 都从这里取，⛔ 预览另写判据（E-0021 同族）。判据与第 2 步两条腿同源、把「没命中」拆开解释：不存在/跨租户不可见（合并「不存在或不可见」）；归属人被禁止结算（读取层同源判据，连用户名一起报——原因在人身上）；已完全结清（= 已结账且本次无未结账调整，见 `with_unsettled_adjustments`——只看 `settle_status` 会把「已结账+追加未结账调整」说成结清，假话）；订单未完成（D-0071：完成⇒生效）；还没有接单人（D-0069）；「未审核通过」一类已删（D-0071，留着是假话，E-0028 同族）。
    顺序有意：「被冻结」排在可见性后第一个——已结账行+追加调整的新形态会走到这里，先报「已完全结清」就是假话；一个 id 可能同时满足多条不满足项，给的是第一条能解释它的原因（财务要能看懂「为什么这批结不了」）；其余顺序=已完全结清 → 订单状态 → 归属人有无 → 兜底。「订单未完成」支的文案比实际闸门宽（`adjustments` 腿判据三段并列：行已结账∨订单已完成 ∧ 不在池内 ∧ 未被冻结），落到这一支的 id 一定 `status != COMPLETED` 且行未结账，是真话（评审 B-1）。"""
    visible = {
        performance.id: performance
        for performance in scope_queryset(
            user,
            Performance.objects.select_related("order", "user").filter(
                id__in=unqualified_ids,
                # ⭐ D-0144（C10）：已软删单的绩效行与「不存在或不可见」同一出口；⭐ R-6：单条软删的绩效行同判
                # （不补会把它当活行、念出它的结账/订单状态——一句假话）。
                order__deleted_at__isnull=True,
                deleted_at__isnull=True,
            )
            .exclude(order__hall_card__isnull=False)
            .exclude(order__upstream_hall_card__isnull=False),
            # ⭐ 隐藏单上的绩效行与「不存在或不可见」同出口——它是 `rows` 腿排除的那批（第 2 步排掉的行这里也必须排，否则会念出它的订单状态=假话）。
            # ⚠️ 形状必须是 `exclude(...=False)`（结构网校验方法与比较值）。⭐「按游戏权限收窄」（`order__game_id`，与第 2 步判据同源）：
            # 这一处只产文案——别人游戏的行在这里说「不存在或不可见」，不收窄会给出「已完全结清」这类假话并泄露另一行状态。
            scope_filter=game_scope("order__game_id"),
        )
    }
    # ⚠️ 与第 2 步**同源**：调同一个读取层函数（「判据只有一处」）。
    forbidden_user_ids = settle_forbidden_user_ids(
        [performance.user_id for performance in visible.values() if performance.user_id is not None]
    )
    # 「已完全结清」支的附加事实：这条绩效名下还有没有未结账调整。⭐ 已抽成模块级 `performances_with_unsettled_adjustments`
    # ——「已是目标状态⇒跳过」的判据要的是同一个事实，两处各写一份就会漂移（事实一变、文案与跳过判据各说一套）。
    with_unsettled_adjustments = performances_with_unsettled_adjustments(
        unqualified_ids, user=user
    )
    reasons = {}
    for perf_id in unqualified_ids:
        performance = visible.get(perf_id)
        if performance is None:
            reasons[perf_id] = "不存在或不可见"
        elif performance.user_id in forbidden_user_ids:
            # ⚠️ 本支**承重**：变异 `elif False:` 会被 `SettleForbiddenGateTests` 杀死
            #    （判据是 `forbidden_user_ids` 集合，不是 `getattr(staff_profile)`）。
            reasons[perf_id] = (
                f"归属人 {performance.user.get_username()} 已被禁止结算，"
                "禁止期间的所有账单都无法结算"
            )
        elif (
            performance.settle_status == SettleStatus.SETTLED
            and perf_id not in with_unsettled_adjustments
        ):
            reasons[perf_id] = "已完全结清"
        elif performance.order.status != OrderStatus.COMPLETED:
            reasons[perf_id] = f"订单未完成，当前：{performance.order.status}"
        elif performance.user_id is None:
            reasons[perf_id] = "还没有接单人"
        else:
            reasons[perf_id] = "状态不允许结账"
    return reasons


def _reject_message(unqualified_ids, *, user):
    """把 `_reject_reasons` 的逐条原因拼成结账端点回给财务的一句话。预览要同一套原因但形状是逐 id（P1-2 `errors`），
    所以拆两个函数；原因本身仍只有 `_reject_reasons` 一处实现，⛔ 预览另写判据。"""
    reasons = _reject_reasons(unqualified_ids, user=user)
    return "以下绩效不能结账：" + "、".join(
        f"{perf_id}（{reasons[perf_id]}）" for perf_id in unqualified_ids
    )


def _traced_message(performance_ids):
    """P1-10：`rows` 腿混进「留着结账痕迹」的脏行时给财务的话。文案必须说清为什么不能结：状态列看着「未结账」，
    但 `settled_at`/`settled_amount` 已在库里 ⇒ 再结一次会覆盖已发出去那笔钱的快照。"""
    return (
        "以下绩效已经留有结账痕迹（settled_at / settled_amount），不能再次结账："
        + "、".join(str(perf_id) for perf_id in performance_ids)
    )


#: `payable_net` 读的入参属性名（`api.serializers.PayableNetField` 读的就是它）。
UNSETTLED_ADJUSTMENTS_ATTR = "unsettled_adjustments"


def unsettled_adjustments_prefetch():
    """`payable_net` 入参契约（`instance.unsettled_adjustments`）的唯一构造处：判据=`settle_status=未结账`，与结账第 2 步
    `adjustments` 过滤同源。结算列表与接单统计都从这里取——「哪些调整算未结账」必须只有一处定义（`PayableNetField` docstring
    明写「两处定义」是要防的形状）。"""
    return Prefetch(
        "adjustments",
        queryset=PerformanceAdjustment.objects.filter(
            settle_status=SettleStatus.UNSETTLED
        ),
        to_attr=UNSETTLED_ADJUSTMENTS_ATTR,
    )


def _row_nets(perfs_by_id, adjs_by_perf):
    """逐行算应付净额 `{绩效 id: 净额}`（`payable_net` 唯一调用点）。P4-FE-4 把它从 `_group_totals` 抽出来，让第 5 步能按行写
    `settled_amount` 快照——组和回答不了「这一行结了多少」；抽出不改变算式输入输出（一次求值、组和与快照两处消费）。"""
    nets = {}
    for perf_id, performance in perfs_by_id.items():
        try:
            nets[perf_id] = payable_net(performance, adjs_by_perf.get(perf_id, ()))
        except AmountError as exc:
            # 扣点越界 / 提成模式不可算：**预期失败**，按业务码翻译（不吞、不折算成 0）。
            raise SettleError(exc.message, exc.code) from exc
    return nets


def _group_totals(perfs_by_id, nets_by_id):
    """按绩效归属人分组求和 `{归属人 id: 组和}`（spec §4.3 第 4 步/§10 #18 拟定解读；Decimal，测试钉具体十进制值）。
    ⚠️ 分组判据一个字没变：仍按 `performance.user_id`、不许拿别人的盈余抵这个人的亏空；P4-FE-4 只是把入参换成已算好的
    逐行净额 `nets_by_id`——求和范围与分组维度不变，组和逐字相同（`test_settle_api`/`test_settlement_amount` 全绿即此判据）。"""
    totals = defaultdict(Decimal)
    for perf_id, performance in perfs_by_id.items():
        totals[performance.user_id] += nets_by_id[perf_id]
    return totals


def settle_performances(performance_ids, *, actor, user, reason=""):
    """把一批绩效（及其未结账调整）一次结掉，六步全在一个事务里；预期失败抛 `SettleError`。⚠️ 必填 keyword-only `user`（spec §6.1 全部租户业务查询过 `scope_queryset`；必填让漏传变 `TypeError`，静默 pass-through 会让数据权限接线名存实亡）；`reason` 直接进审计行（Ruling S，财务备注不能在留痕里丢）。⭐ P4-FE-0：「只结调整记录」不覆盖已结账行的 `settled_at`/`settled_by`（`rows` 只捞未结账 ⇒ 天然碰不到，这是正确行为——那两字段记上一轮谁何时结清；⛔ 别为「本轮也有结算人」UPDATE 已结账行，否则财务对账「谁结的这笔工资」成假话，`tests/test_p4_settle_meta.py` T2 钉住）。⭐ Q3=A（业务方）：已是目标状态 ⇒ 跳过不报错（`skipped_performances`，一行不写、整体 200+`code=0`）；⛔ 跳过≠不合格：未处于目标状态的不合格 id（不存在/越权/订单未完成/无归属人/被冻结）照旧整批 `1006`、没有部分成功；已结清行哪怕后来被冻结/退单也只按目标状态跳过（本批对它是空操作，判据=计划 §3 T2.2 契约，⛔ 不额外加三项——加了会让空操作报错）；「已结账行+追加未结账调整」照旧被结掉（坏账出口）⛔ 不算跳过。"""
    # 第 1 步的「去重」在视图的序列化器之后做：后续判据全部基于 `set`（spec §4.3 第 1 步）。
    ids = sorted(set(performance_ids))
    with transaction.atomic():
        # 先锁订单，与下家回传共用锁序。接下来按取得锁后的订单状态重新判定可否结账。
        scoped = scope_queryset(
            user, Performance.objects.filter(pk__in=ids), scope_filter=game_scope("order__game_id"),
        )
        list(Order.objects.filter(pk__in=scoped.values("order_id")).select_for_update().order_by("pk"))
        batch = _resolve_batch(ids, user=user, lock=True)
        if any(row.online_payroll_batch_id for row in batch.rows):
            raise SettleError("所选报酬已在在线付款单中", ErrorCode.SETTLE_STATE_INVALID)

        # ⭐ P1-10：加锁之后复查 `is_settled`。脏行 ⇒ 整批 `1006`（⛔ 不是跳过，
        #    也⛔ 不是「照旧结掉」——第 5 步会覆盖它已经付出过的 `settled_amount`）。
        if batch.already_traced:
            raise SettleError(
                _traced_message(batch.already_traced),
                ErrorCode.SETTLE_STATE_INVALID,
            )
        if batch.unqualified:
            raise SettleError(
                _reject_message(batch.unqualified, user=user),
                ErrorCode.SETTLE_STATE_INVALID,
            )
        rows = batch.rows
        adjustments = batch.adjustments
        skipped_performances = batch.skipped_performances
        # ⚠️ `settled_rows` 由第 4 步的 `list(rows)` 取（同一个缓存；见那一处的注释）。

        # 第 4 步：算净额并做 `1011` 判定——**必须在第 5 步之前**（否则检查成死代码）。
        adjustments_by_perf = defaultdict(list)
        for adjustment in adjustments:
            adjustments_by_perf[adjustment.performance_id].append(adjustment)
        # ⚠️ `list(rows)` 必须在第 5 步之前取：`QuerySet.update()` 会清空 `_result_cache`，之后再取会重新查库、因 `settle_status` 已变
        # 「已结账」得到空集——返回的 id 列表与审计行就没了。⭐ P1-10 起这句是缓存命中（`_resolve_batch` 已求值到内存，加锁与读到最新状态须
        # 同一次 SELECT）⇒ 不发第二条查询；⛔ 别挪到第 5 步之后（那条反例仍成立）。
        settled_rows = list(rows)
        perfs_by_id = {performance.id: performance for performance in settled_rows}
        for perf_id, items in adjustments_by_perf.items():
            # 已结账的绩效行不在 `rows` 里，但它的新调整记录也要参与净额判定。
            perfs_by_id.setdefault(perf_id, items[0].performance)
        # ⭐ P4-FE-4：**逐行**净额是 `1011` 组和与「实结金额快照」的**同一个**来源
        #    （一次求值、两处消费）——组和回答不了「这一行结了多少」。
        nets_by_id = _row_nets(perfs_by_id, adjustments_by_perf)
        totals_by_owner = _group_totals(perfs_by_id, nets_by_id)
        losers = {owner: total for owner, total in totals_by_owner.items() if total < 0}
        if losers:
            detail = "、".join(
                f"归属人 {owner}：{total}"
                for owner, total in sorted(losers.items(), key=lambda item: str(item[0]))
            )
            raise SettleError(
                f"应付净额为负，不能结账（{detail}）",
                ErrorCode.SETTLE_NEGATIVE_PAYABLE,
            )

        # 第 5 步的行数检查和审计共用同一批已读出的调整记录。
        settled_at = timezone.now()
        settled_adjustments = [
            adjustment
            for items in adjustments_by_perf.values()
            for adjustment in items
        ]
        persist_manual_settlement(
            rows,
            adjustments,
            settled_rows=settled_rows,
            expected_adjustments=len(settled_adjustments),
            amounts_by_id=nets_by_id,
            actor=actor,
            settled_at=settled_at,
        )

        # 第 6 步：写审计——每条绩效一行、每条被结调整一行。⭐ M3a：旧出口 `apps/business/audit.py::log_settle` 已删，迁到唯一写入出口
        # `apps/audit/log.py::record(...)`（spec §7.1）。⚠️ `OperationLog` 没有 `performance_id` 列，legacy 带它是为了「同一订单打手行+客服行
        # 只用 order_no 分不出结的是哪条」（spec §3.6）⇒ 身份按 R11 落 `target_kind="user"`+`target_id`/`target_name`=归属人（`uniq_performance_order_user` 保证订单×人唯一 ⇒ 三元组唯一指认绩效行，`tests/test_p4_audit` 用同单两行证明可分）；`reason` 不许丢（Ruling S）；前后值字面量与 legacy 一致（`SETTLE_FROM`/`SETTLE_TO`）。
        for performance in settled_rows:
            record(
                log_type=ACTION_LOG_TYPES["performance.settle"],
                action="performance.settle",
                actor=actor,
                actor_id=user.pk if user is not None else None,
                # ⭐ `user` 是**请求方**（数据权限那一维），唯一的生产
                # 调用点是 `settlement_views`（传 `request.user`）⇒ `None` 只可能是直调
                # ⇒ **值级**声明机器来源，⛔ 不静默通过（见 `apps/audit/log.py`）。
                system=user is None,
                target_kind="user",
                target_id=performance.user_id,
                # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`，
                # 昵称优先 / 空串回落登录账号）；归属关系为空 ⇒ 保持空串。
                target_name=(
                    user_display_name(performance.user) if performance.user_id else ""
                ),
                order_no=performance.order.order_no,
                from_value=SETTLE_FROM,
                to_value=SETTLE_TO,
                reason=reason,
            )
        for adjustment in settled_adjustments:
            performance = adjustment.performance
            record(
                log_type=ACTION_LOG_TYPES["performance.settle"],
                action="performance.settle",
                actor=actor,
                actor_id=user.pk if user is not None else None,
                # ⭐ `user` 是**请求方**（数据权限那一维），唯一的生产
                # 调用点是 `settlement_views`（传 `request.user`）⇒ `None` 只可能是直调
                # ⇒ **值级**声明机器来源，⛔ 不静默通过（见 `apps/audit/log.py`）。
                system=user is None,
                target_kind="user",
                target_id=performance.user_id,
                # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`，
                # 昵称优先 / 空串回落登录账号）；归属关系为空 ⇒ 保持空串。
                target_name=(
                    user_display_name(performance.user) if performance.user_id else ""
                ),
                order_no=performance.order.order_no,
                from_value=SETTLE_FROM,
                to_value=SETTLE_TO,
                reason=reason,
            )
        orders_by_owner = defaultdict(dict)
        for row in perfs_by_id.values():
            if row.user_id is not None:
                orders_by_owner[row.user_id][row.order_id] = row.order
        for recipient_id, orders in sorted(orders_by_owner.items()):
            order_nos = set()
            for order in orders.values():
                order_nos.update((order.order_no, order.source_order_no.strip()))
            enqueue_worker_notice(
                user_id=recipient_id, kind="settled", reference=f"settled:{settled_at.isoformat()}",
                details={
                    "count": len(orders),
                    "amount": format(totals_by_owner[recipient_id], ".2f"),
                    "occurred_at": settled_at.isoformat(),
                    "related_order_nos": sorted(order_nos - {""}),
                },
            )
        return SettleResult(
            settled_performances=[performance.id for performance in settled_rows],
            settled_adjustments=[
                adjustment.id for adjustment in settled_adjustments
            ],
            skipped_performances=skipped_performances,
        )
