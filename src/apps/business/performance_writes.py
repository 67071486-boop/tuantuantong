"""绩效记录的两条**写**动作：撤销结账 + 删除记录（业务方裁定 D-0072）。 **为什么单独成模块**（与 `settlement.py` 同因）：两条动作各自**同时改多处** （绩效行 / 调整记录 / 审计），而它们之间**没有任何数据库约束能表达联合一致性**—— 不加事务，失效模式是**脏数据而不是报错**。视图只负责「过序列化器 → 调这里 → 把预期失败翻成业务码」。 ⛔ **本模块不加任何 `≥ 0` 校验**（D-0015）：绩效金额与调整金额都允许为负。 ⛔ **`transaction.atomic()` 里不用裸 `return` 退出补偿路径**（E-0022）：可预期失败一律 `raise`，让 `atomic()` 整批回滚；补偿路径上的裸 `return` 会把前面几笔写入提交，留下「一半成功」的脏行而且**没有任何异常**。 ## 撤销结账（D-0072 ②）——只做「已结账 → 未结账」这一个方向 - ⭐ **已经是未结账的行 ⇒ 跳过、不报错（业务方裁定 Q3=A）**：与 `/settlements/settle` 对称——「勾选的行已经是目标状态」不是错误，进 `skipped_performances` 并如实报数； ⛔ 但「查不到 / 越权」仍然整批 `1006`，不做「部分成功」。 - **快照必须一起清**：`settle_status → 未结账`、`settled_at → NULL`、`settled_by → ""`、 `settled_amount → NULL`。不清 `settled_amount` 的话，一行**未结账**的绩效会带着上一轮的实结金额出现在结算记录页上——旧快照配新状态，财务会读成「这一轮也结了这么多」。 - ⭐ **「同一轮」的调整记录一起回退**：轮次判据 = `PerformanceAdjustment.settled_at`
等于**它所属绩效行的** `settled_at`（`settlement.settle_performances` 第 5 步给同一批绩效行与调整记录写的是**同一个** `settled_at`，一次结账一次取值）。配对是 **按 `(performance_id, settled_at)` 逐对**构造的，不是「两个集合里有没有同值」—— 后者会把「A 行 T1 轮结的绩效、外加一条 T2 轮的调整」在 B 行也是 T2 轮结账时误伤。 - ⚠️ **后来「只结调整」轮次（`settled_at` 更晚）不回退**——这是**有意的边界**：撤的是「这一行上一次被结清的那一轮」，而那一轮之后财务又单独结掉的调整记录是 **另一次**结账动作，撤销一次结账没有权力把它一起撤掉（否则一次撤销会掀掉两轮的钱）。钉住它的用例见 `tests/test_p4fu_performance_writes.py::RevokeBoundaryTests`。 - ⚠️ **`settled_at` 为 `NULL` 的存量行**（P4-FE-0 之前结的账，那时还没有这一列）：轮次判据取不到值 ⇒ **一条调整记录都不回退**。这是判据的如实边界，不是「顺手」清空所有已结账调整——后者会把不属于这一轮的调整记录一起掀掉。 - **并发守门**（E-0022 同族）：用**条件 `update()` + rowcount 比较**。谓词在库里重判，若本批在读之后已被别的请求改过，`UPDATE` 会命中更少/更多行 ⇒ 抛业务码让 `atomic()` 整批回滚，把「静默假成功」变成**显式失败**。两类（绩效 / 调整）各比一次。 - **审计**：整批成功后，**每条绩效一行 ＋ 每条被回退的调整记录一行**
`OperationLog`（`action=performance.settle`、`from_value=已结账`、`to_value=未结账`、 `reason=撤销原因`）。与正向结账（`settlement.py` 第 6 步）**对称**——只写绩效行会让「这条调整被回退过」逐条不可追溯。打的是**数据库行**，不是 `logger`（E-0009）。 ⭐ Task L：旧出口 `audit.py::log_settle` 已随 `audit.py` 退役 ⇒ 走唯一写入出口 `apps/audit/log.py::record(...)`。 ## 删除记录（D-0072 ①） - **已结账的行不允许删**：判据是三个字段的**并集**——`settle_status=已结账` **或** `settled_at is not None` **或** `settled_amount is not None`。三个都看而不是只看 `settle_status`：存量行里可能有「已结账但没写 `settled_at`」（P4-FE-0 之前）或状态被改回未结账却留着快照的脏行；只看一个字段会把它们当「未结账」删掉，而那正是删掉**对账凭据**。 - **打手那条绩效一律不可删**。不能只挡「还没到终态」的订单：状态图里**没有终态**—— `COMPLETED` 能进 `ISSUE/PAUSED`（再回 `COMPLETED`）、`REFUNDED` 也能回池内（`REFUNDED → {UNASSIGNED, WAITING_GRAB}`）。所以「未处于终态」这个条件恒真，规则的**如实形态**就是「MAIN 行一律不可删」。 ⛔ **不选「删掉后补建」那条路**：补建出的是「录单即建」的初始行，`user`/金额全空 ⇒ 同一个订单**静默换了身份**（上一个接单人的记录凭空消失），而 `COMPLETED` 钩子接下来会因为「打手行未审核」拒单——那是把卡死从一处挪到另一处，正是 `performance.ensure_main_performance_of` 的 docstring 说过的「跳过是错的」。 - **有调整记录的行删不掉**（`PerformanceAdjustment.performance` 是 `PROTECT`）： `ProtectedError` 必须翻成**业务码 `1001`**（照 `EnvelopeModelViewSet.destroy` 的形状）， ⛔ 不许 500、也不许静默跳过。 - **审计写在删除之前、同事务**：`OperationLog.target_id` 装的是**绩效主键**（该表**没有** `performance_id` 列，身份落在 `target_kind="performance"` + `target_id`），删行后审计行 **不会被删**——它是这次删除**唯一**的历史线索。先写后删是为了：删除因 `PROTECT` 失败时整批回滚，不会留下「记了删除、其实没删」的假留痕。"""
from dataclasses import dataclass
from functools import reduce
from operator import or_

from django.db import transaction
from django.db.models import ProtectedError, Q

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.game_scope import game_scope
from apps.business.models import (
    EmpType,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.business.settle_requests import cancel_settle_requests_for_revoke
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset

#: 撤销结账在留痕里的前后值字面量（`settlement.py` 的 `SETTLE_FROM`/`SETTLE_TO` 的**反向**；
#: 不复用那两个常量，避免把「正向」与「反向」两个语义绑在同一个符号上）。
REVOKE_FROM = "已结账"
REVOKE_TO = "未结账"

#: 「系统自动发薪」的行的 `settled_by` 字面量（结算 P0-1）。 ⚠️ 与 `apps/accounts/payout.py::SYSTEM_SETTLED_BY` 是**同一个字面量**，但本模块**不 import
#: 它**：`payout` 注册 `post_save` 接收器、又反向依赖 `business` 的几个模块，本仓 `business` 侧既有写法也是字面量（`order_status.py:256` 的「自动入账放行」、`refunds.py:424` 的
#: 「回冲只认 system」、`jidanpu_common.py` 跟退闸的「非 system 才挡」）。 ⚠️ 现在是**四处**判同一件事，改口径时四处一起改。
SYSTEM_SETTLED_BY = "system"

#: 拒绝撤销的文案（计划 §1 要求「文案说清『系统已自动发薪，请走退单回冲或人工负向调账』」）。
#: 前半句是**事实**、后半句是**出路**：财务真正能做的两件事都写出来，不然只会反复点同一颗按钮。
AUTO_PAID_REVOKE_REASON = (
    "系统已自动发薪，请走退单回冲或人工负向调账（钱已进打手钱包，撤销结账不会退钱）"
)


class RevokeError(Exception):
    """撤销结账的**预期**失败（状态不允许 / 读取后被并发改动）。形状与 `settlement.SettleError` 同构（`code` 直接进信封当业务码、`message` 直接当 `msg`）；
    块内抛它 ⇒ `atomic()` 随异常回滚，**不是** `set_rollback` + 裸 `return` 那条补偿路径。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


class DeleteError(Exception):
    """删除记录的**预期**失败（状态不允许 / 有调整记录 / 缺行）。形状同 `RevokeError`。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class RevokeResult:
    """本次撤销真正回退的**绩效 id** 与**调整记录 id**（列表而不是计数，E-0060 口径）。 ⭐ **`skipped_performances`（业务方裁定 Q3=A）**：本批里**已经是
    「未结账」这个目标状态、因此被跳过**的那些绩效 id——与 `/settlements/settle` 的同名字段对称（那颗「更改结算状态」按钮的两个方向）。跳过是**成功路径**
    （HTTP 200 + `code=0`）：「勾选的行已经是目标状态」不是错误。 ⚠️ 它与「没查到这一行」**必须能区分**：不存在 / 越权的 id **不在这里**，而是照旧让整批以 `1006` 失败（`unqualified` 那一支）。"""

    revoked_performances: list
    revoked_adjustments: list
    skipped_performances: list


@dataclass(frozen=True)
class DeleteResult:
    """本次真正删掉的**绩效 id**（列表而不是计数，E-0060 口径）。"""

    deleted: list


def already_unsettled_ids(performance_ids, *, user):
    """这批绩效里**已是「未结账」这个目标状态**的那些 id（撤销结账的跳过判据）。判据 = **可见**（`scope_queryset` + `game_scope` + 已软删单 / 已软删行排除） ∧ `settle_status=未结账`。
    ⚠️ **与「不存在或不可见」互斥**：不在这个集合里的 id 由调用方照旧判 `unqualified` ⇒ 整批 `1006`。两个形状各有一条用例（「未结账 ⇒ 跳过」与「查不到 ⇒ `1006`」），
    把它们合并会让「勾错了别人游戏的行」从报错退化成**静默成功**。 ⚠️ **软删口径与 `_revoke_reject_message` 同源**（两处都判 `deleted_at` / 订单软删）：不然一个已被垃圾桶软删的 id 会被念成「已经是未结账了，跳过吧」，而它在结算列表里根本看不见（一句假话）。"""
    return set(
        scope_queryset(
            user,
            Performance.objects.filter(
                id__in=performance_ids,
                settle_status=SettleStatus.UNSETTLED,
                # ⭐ D-0144（C13 的同形物）：已软删单的绩效行不在这里（跨关系 JOIN 不经过
                # `Order` 的默认 manager）。
                order__deleted_at__isnull=True,
                # ⭐ R-8a 的同形物：**父绩效行自己**被软删（订单还活着）时同判。
                deleted_at__isnull=True,
            ),
            # ⭐「按游戏权限收窄」：`Performance` ⇒ `order__game_id`。
            # 不在自己游戏范围内的行不进这个集合 ⇒ 落 fail-closed 的「不存在或不可见」。
            scope_filter=game_scope("order__game_id"),
        ).values_list("id", flat=True)
    )


def _revoke_reject_message(unqualified_ids, *, user):
    """给每个不能撤销的 id 各写一句**具体原因**（照 `settlement._reject_message` 的形状）。"""
    visible = {
        performance.id: performance
        for performance in scope_queryset(
            user,
            Performance.objects.filter(
                id__in=unqualified_ids,
                # ⭐ D-0144（C13 的姊妹函数，与 `settlement._reject_message` 同形）：
                # 已软删单的行在这里说「不存在或不可见」——与 C13 的取行同源，
                # 否则会给出「不是已结账状态」这类**假话**。
                order__deleted_at__isnull=True,
                # ⭐ R-8a 重做 sweep 时新发现：**父绩效行自己**被软删（订单还活着）时同理——此前只判了订单那道，传一个已被垃圾桶软删的
                # id 会念出「不是已结账状态」（把被删行当活行的假话），与 R-6 修好的 `settlement._reject_message` 那半不对称。判据：
                # `tests/test_performance_row_soft_delete_read_paths.py` 的 `RevokeRejectMessageExclusionTests`（活行对照在同一处）。
                deleted_at__isnull=True,
            ),
            # ⭐「按游戏权限收窄」：别人游戏的绩效行在这里与"不存在"同形（`_reject_message` 的逐条原因会据此说「不存在或不可见」）——
            # 判据与 `revoke_settlements` 取行那处同源，否则会给出**假话**
            # （「不是已结账状态，当前：已结账」）。
            scope_filter=game_scope("order__game_id"),
        )
    }
    reasons = []
    for perf_id in unqualified_ids:
        performance = visible.get(perf_id)
        if performance is None:
            reasons.append(f"{perf_id}（不存在或不可见）")
        else:
            reasons.append(
                f"{perf_id}（不是已结账状态，当前：{performance.settle_status or '未知'}）"
            )
    return "以下绩效不能撤销结账：" + "、".join(reasons)


def revoke_settlements(performance_ids, *, actor, user, reason):
    """把一批**已结账**绩效（及其「同一轮」结掉的调整记录）回退成未结账（D-0072 ②）。 ⚠️ **`user` 是必填 keyword-only**（同 `settle_performances`）：租户数据权限的卡点需要一个 `user`，做成必填是为了让「漏传」变成 `TypeError` 而不是静默放行。 ⚠️ **`reason` 是必填**：D-0072 明文「撤销结账必须填原因」，它直接进每一条审计行。预期失败抛 `RevokeError`（整批回滚）。
    ⭐ **「已经是未结账」⇒ 跳过、不报错（业务方裁定 Q3=A）**：第 1 步的 fail-closed 判定**之前**先摘出「已是目标状态」的那些 id（判据只有一处： `already_unsettled_ids`），它们进返回值的 `skipped_performances`，**一行都不写**，而这一次调用整体仍是成功（HTTP 200 + `code=0`）。 ⛔ **但「跳过」与「不合格」是两件事**：不存在 / 跨租户或越出游戏范围的 id **不在这里**，照旧让整批以 `1006` 失败、一行不写。**没有**「部分成功」。
    ⭐ **系统自动发薪的行 ⇒ 整批拒绝（`1014`，结算 P0-1，计划 §1 选 A）**： `settled_by == "system"` 的行，钱**已经**打进打手钱包（`accounts.payout` 在订单完成时入账）。只清四列而不退钱 ⇒ 行回到「未结账」，而完成入账的幂等键正是 `perf.is_settled` ⇒ 订单重开再完成会**再发一次**（探针实测钱包 100 → 200）。本批的处置是**拒绝、不动钱**：整批里只要有一条这样的行 ⇒ `1014` + 一句能照着做的人话（见 `AUTO_PAID_REVOKE_REASON`），**一行都不改**。手工结账的行（`settled_by` 是操作人名）钱从来没进过钱包 ⇒ 照旧只清四列。 ⚠️ 「能不能退钱」是产品口径（计划 §6 的 B 方案），要动得业务方另行拍板。"""
    ids = sorted(set(performance_ids))
    with transaction.atomic():
        # 第 1 步：解析本批要撤销的绩效行（**本租户内、已结账**）。
        candidates = scope_queryset(
            user,
            # ⚠️ `user` 一起 `select_related`：第 4 步的审计行要写「绩效归属人」的
            # 用户名（`target_name`），不预取就是每行一条查询（与 `settlement.py`
            # 的结账路径同形）。
            Performance.objects.select_related("order", "user").filter(
                id__in=ids,
                # ⭐ D-0144（C13）：已软删单的绩效行不可撤销结账（跨关系 JOIN 不经过
                # `Order` 的默认 manager ⇒ 必须显式补，spec §4.3-C2）。
                order__deleted_at__isnull=True,
            ),
            # ⭐「按游戏权限收窄」：`Performance` ⇒ `order__game_id`。
            # 不在自己游戏范围内的行 ⇒ 逐 id fail-closed 的「不存在或不可见」。
            scope_filter=game_scope("order__game_id"),
        )
        locked = list(candidates.select_for_update(of=("self",)).order_by("pk"))
        rows = candidates.filter(settle_status=SettleStatus.SETTLED)
        if any(row.settlement_method == "online" for row in locked):
            raise RevokeError("支付宝已打款的报酬不能撤销结账", ErrorCode.PERFORMANCE_STATE_INVALID)

        # ⭐ P0-1（计划 §1 选 A）：**系统自动发薪**的行 ⇒ 整批拒绝、一行不改。为什么必须在 `update()` **之前**：这里的判据一旦放过，下面的条件 `update()`
        # 会把四列清成未结账、却一分钱都不退 ⇒ 重开再完成再付一次（这是该入口要堵的洞）。 ⚠️ 从 `rows`（已按「本租户 + 已结账 + 未软删 + 游戏范围」收窄）里**识别**，
        # 而不是另起一条查询：多一条查询就多一处会漂移的谓词（E-0021 同族）。 ⚠️ 位置在「跳过 / 不合格」判定**之前**：一条 system 行足以让整批不可撤，此时再逐 id 讲「哪条已未结账」只是噪音。
        auto_paid = sorted(
            performance.id
            for performance in rows
            if performance.settled_by == SYSTEM_SETTLED_BY
        )
        if auto_paid:
            raise RevokeError(
                "以下绩效不能撤销结账："
                + "、".join(
                    f"{perf_id}（{AUTO_PAID_REVOKE_REASON}）" for perf_id in auto_paid
                ),
                # 业务码用**绩效状态不允许此操作**（`1014`，与删除记录同一族）：
                # 这不是「查不到 / 越权」（`1006`），而是「这一行的状态不许撤」。
                ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        # 逐 id fail-closed（不是总数比对）：任一 id 不合格 ⇒ 整批回滚 + `1006`。
        # ⚠️ `values_list()` 是对 `rows` 的**克隆**求值，不会缓存它本身——下面还要
        # 在 `update()` 之前 `list(rows)` 拿到「将要回退的行」。
        row_ids = set(rows.values_list("id", flat=True))
        # ⭐ 已「未结账」的 id ⇒ **跳过、不报错、如实报数**（业务方裁定 Q3=A，与 `/settlements/settle` 那一侧对称）。⚠️ 只对 `rows` 没命中的 id 去问这一次：
        # 正常撤销路径上每个 id 都在 `rows` 里 ⇒ 不多打一条查询；而这**不是**纯优化 ——「已经是目标状态」这个问题只对没命中的 id 才有意义。
        # ⛔ **其余不合格的一律照旧整批拒绝**（不存在 / 跨租户或越出游戏范围），错误码与文案一个字不改：跳过**不是**「凡是撤不了的都跳过」。
        missing = [perf_id for perf_id in ids if perf_id not in row_ids]
        already_unsettled = (
            already_unsettled_ids(missing, user=user) if missing else set()
        )
        skipped_performances = [
            perf_id for perf_id in missing if perf_id in already_unsettled
        ]
        unqualified = [
            perf_id for perf_id in missing if perf_id not in already_unsettled
        ]
        if unqualified:
            raise RevokeError(
                _revoke_reject_message(unqualified, user=user),
                ErrorCode.SETTLE_STATE_INVALID,
            )

        # 第 2 步：解析「同一轮」的调整记录。**必须在这里物化**：`update()` 会清空
        # 两份 QuerySet 的结果缓存，之后再取就查不到「已结账」的行了。
        revoked_rows = list(rows)
        # 轮次判据逐对构造（见模块 docstring 的「同一轮」段）：`settled_at` 为 NULL 的
        # 存量行取不到配对值 ⇒ 不参与（一条调整都不回退）。
        pairs = [
            Q(performance_id=performance.id, settled_at=performance.settled_at)
            for performance in revoked_rows
            if performance.settled_at is not None
        ]
        if pairs:
            adjustments = scope_queryset(
                user,
                PerformanceAdjustment.objects.select_related(
                    "performance__order", "performance__user"
                ).filter(
                    reduce(or_, pairs), settle_status=SettleStatus.SETTLED
                ),
                # ⭐「按游戏权限收窄」：`PerformanceAdjustment` 经 `performance.order` 指到游戏 ⇒ `performance__order__game_id`
                # （与上面取绩效那处**两条不同的路径**，按各自的模型选）。 ⚠️ `revoked_rows` 已被上面那次收窄 ⇒ 这条腿对非管理员不改变
                # 可撤销集合（与列表/结账的 fail-closed 合起来后它落在可见行上）；接线仍然保留，避免"这一层漏了"在代码里不可见。
                scope_filter=game_scope("performance__order__game_id"),
            )
        else:
            adjustments = PerformanceAdjustment.objects.none()
        revoked_adjustments = list(adjustments)

        # 第 3 步：条件 `update()` + rowcount 守门（E-0022 同族）。 ⚠️ `rows` / `adjustments` 的谓词里都带 `settle_status=已结账`，`update()` 在库里
        # 重判它：读取之后被别的请求改过的行不会被命中 ⇒ rowcount 与预期不符 ⇒ 抛异常让整批回滚。⛔ 不要用裸 `return` 走补偿路径。
        expected = (len(revoked_rows), len(revoked_adjustments))
        updated_rows = rows.exclude(settlement_method="online").update(
            settle_status=SettleStatus.UNSETTLED,
            settled_at=None,
            settled_by="",
            # 快照一起清：否则结算记录页会对一行**未结账**的行显示上一轮的实结金额。
            settled_amount=None,
        )
        updated_adjustments = adjustments.update(
            settle_status=SettleStatus.UNSETTLED, settled_at=None
        )
        if (updated_rows, updated_adjustments) != expected:
            raise RevokeError(
                "本批在你读取之后已被改动（绩效 %d/%d，调整记录 %d/%d），请刷新后重试"
                % (updated_rows, expected[0], updated_adjustments, expected[1]),
                ErrorCode.SETTLE_STATE_INVALID,
            )

        # 第 3.5 步（spec §8.1；**业务方当日口径**）：**这些行上的提前结算申请随撤销作废** —— `pending` **和** `settled` 都置 `cancelled`。口径：结算被撤销 ⇒ 那次「已结」**不成立**，申请不能挡住重申（业务方原话
        # 「撤销结账要能重申」）；作废后额度按 `settle_requests.QUOTA_STATES` 自然回落（`cancelled` 不占），打手可以重新申请并按 `create_settle_request` 复用那一行。 ⚠️ **与退款路径不同**：退款只作废 `pending`（已 `settled` 的申请意味着钱已按那次
        # 结账打过，退款是另一条回冲链）——两条口径的对照表在 `settle_requests` 的 docstring。 ⚠️ **幂等**由条件更新保证：同一批重复撤销再跑一次命中 0 行（不会重复扣额度）。 ⚠️ 位置在 rowcount 守门**之后**：本批若因并发被拒，一行都不该改。 ⚠️ 同一个 `transaction.atomic()`（调用方提供边界）⇒ 与四列回退一起提交 / 回滚。
        cancel_settle_requests_for_revoke(
            [performance.id for performance in revoked_rows]
        )

        # 第 4 步：审计——**每条绩效一行、每条被回退的调整记录一行**（D-0072 要求「写进订单日志」）。 ⭐ 原实现只给绩效行写，而正向结账（`settlement.py` 第 6 步）是**两类都写**的 ⇒ 「这条调整被回退过」逐条不可追溯，与业务方原话「打到订单日志里面去」不符。现在与正向结账**对称**：绩效行一行 + 每条被回退的调整记录各一行。 ⭐ **Task L**：旧出口 `apps/business/audit.py::log_settle`
        # 随 `audit.py` 一起退役 ⇒ 按 M3a 建立的形状迁到唯一写入出口 `apps/audit/log.py::record(...)`。 ⚠️ **`action` 复用 `performance.settle`**（`main` 的 `log_settle` 就是 `AuditLog.Action.SETTLE`，撤销与正向结账是同一颗「更改结算状态」按钮的两个方向， `performance_write_views.SettlementRevokeView` 也共用同一个权限节点）——
        # 句子 `修改了{人}的绩效清算状态为：{状态}` 在反向时**仍然为真**： `from_value=已结账`、`to_value=未结账`。 ⚠️ **`OperationLog` 没有 `performance_id` 列** ⇒ 身份按 §4.2 的 R12 口径落到 `target_kind="user"` + 归属人 id / 用户名（`uniq_performance_order_user` 保证「订单 × 人」唯一 ⇒ 三元组能唯一指认绩效行），与正向结账**完全同形** （`test_settle_api.py` 用「同一订单的打手行 + 客服行」证明两行可分）。 ⚠️ 断言请打数据库行（E-0009），不要断言 logger。
        for performance in revoked_rows:
            record(
                log_type=ACTION_LOG_TYPES["performance.settle"],
                action="performance.settle",
                actor=actor,
                actor_id=user.pk if user is not None else None,
                # ⭐ `user` 是**请求方**（数据权限那一维），两个 HTTP
                # 调用点都传 `request.user`；它为空只可能是非请求来源（探针 / 直调）
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
                from_value=REVOKE_FROM,
                to_value=REVOKE_TO,
                reason=reason,
            )
        for adjustment in revoked_adjustments:
            # `adjustments` 已 `select_related("performance__order", "performance__user")`
            # ⇒ 这里不新增查询。
            performance = adjustment.performance
            record(
                log_type=ACTION_LOG_TYPES["performance.settle"],
                action="performance.settle",
                actor=actor,
                actor_id=user.pk if user is not None else None,
                # ⭐ `user` 是**请求方**（数据权限那一维），两个 HTTP
                # 调用点都传 `request.user`；它为空只可能是非请求来源（探针 / 直调）
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
                from_value=REVOKE_FROM,
                to_value=REVOKE_TO,
                reason=reason,
            )
        return RevokeResult(
            revoked_performances=[performance.id for performance in revoked_rows],
            revoked_adjustments=[
                adjustment.id for adjustment in revoked_adjustments
            ],
            skipped_performances=skipped_performances,
        )


def _delete_reject_message(reasons):
    return "以下绩效不能删除：" + "、".join(reasons)


def delete_performances(performance_ids, *, actor, user):
    """删除一批绩效记录（D-0072 ①）。预期失败抛 `DeleteError`（整批回滚）。
    ⚠️ 已结账 / MAIN 行 / 有调整记录三类都**整批拒**（逐 id 说明），不做静默跳过——
    静默跳过会让「点了删除、以为删了」变成看不出错的事故。"""
    ids = sorted(set(performance_ids))
    with transaction.atomic():
        rows = scope_queryset(
            user,
            Performance.objects.select_related("order", "user").filter(
                id__in=ids,
                # ⭐ D-0144（C14）：已软删单的绩效行在这里落「不存在或不可见」（`1014`），
                # **不是**那条「MAIN 行不能删除」。
                order__deleted_at__isnull=True,
            ),
            # ⭐「按游戏权限收窄」：`Performance` ⇒ `order__game_id`。
            # 不在自己游戏范围内的行落进下面的「不存在或不可见」支路（`1014`）。
            scope_filter=game_scope("order__game_id"),
        )
        rows = rows.select_for_update(of=("self",)).order_by("pk")
        by_id = {performance.id: performance for performance in rows}

        # 逐 id fail-closed：不存在 / 不可见 ⇒ 整批拒（业务码 1014，与「状态不允许」同码、
        # 靠 msg 逐条说明——本仓的 `1006` 是**结账**专用码，删除路径不借用它）。
        missing = [perf_id for perf_id in ids if perf_id not in by_id]
        if missing:
            raise DeleteError(
                _delete_reject_message(
                    [f"{perf_id}（不存在或不可见）" for perf_id in missing]
                ),
                ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        if any(row.online_payroll_batch_id for row in by_id.values()):
            raise DeleteError("所选报酬已在在线付款单中，不能删除", ErrorCode.PERFORMANCE_STATE_INVALID)

        # 已结账不允许删（D-0072 ①）。判据取 `Performance.is_settled`（模型上的**唯一**实现，
        # 三字段并集；⛔ 本模块不再自带一份）。
        settled = [perf_id for perf_id in ids if by_id[perf_id].is_settled]
        if settled:
            raise DeleteError(
                _delete_reject_message(
                    [
                        f"{perf_id}（已结账，当前：{by_id[perf_id].settle_status}）"
                        for perf_id in settled
                    ]
                ),
                ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        # MAIN 行一律不可删（见模块 docstring 的「没有终态」论证）。
        main_rows = [
            perf_id for perf_id in ids if by_id[perf_id].emp_type == EmpType.MAIN
        ]
        if main_rows:
            raise DeleteError(
                _delete_reject_message(
                    [
                        f"{perf_id}（订单的打单工资绩效行，订单后续动作依赖它，"
                        "不能删除）"
                        for perf_id in main_rows
                    ]
                ),
                ErrorCode.PERFORMANCE_STATE_INVALID,
            )

        # 审计**先写**：`main` 的旧出口用的 `AuditLog.performance_id` 是裸整数、删行后审计行不会被删——它是这次删除**唯一**的历史线索（`main` 的 `log_performance_delete` docstring 原文）。⭐ **Task L**：迁到唯一写入出口 `record(...)`，**动作改用对标自己的 `performance.delete`**（spec §5.2 登记为
        # 「照抄」，`main` 那条路当时因为不想新增 `AuditLog.Action` 取值而复用了 `ADJUST`，本线的动作 code 是自由字符串 ⇒ 没有那条代价，按对标逐字照抄更准）。 ⚠️ 四个槽位按 spec §5.9 落列：绩效用户 → `target_name`、预计绩效金额 → `from_value`、生效绩效金额 → `to_value`、结账状态 → `reason`（纯值）,
        # 订单号 → `order_no`、绩效ID → `target_id`（对标「相关ID」列 ⇒ `target_kind` 取本线新增的 `performance`，见 spec §4.2 的 Task L 补记）。 `main` 当年带在 `reason` 里的 `emp_type` / `settled_amount` 是**它自己加**的字段（对标那句里没有）⇒ 按「照抄」不再渲染；两列在绩效列表接口上仍可查。
        for perf_id in ids:
            performance = by_id[perf_id]
            record(
                log_type=ACTION_LOG_TYPES["performance.delete"],
                action="performance.delete",
                actor=actor,
                actor_id=user.pk if user is not None else None,
                # ⭐ 同上面两处 —— `user is None` ⇒ 显式声明机器来源。
                system=user is None,
                target_kind="performance",
                target_id=performance.id,
                # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`）。
                target_name=(
                    user_display_name(performance.user)
                    if performance.user_id is not None
                    else ""
                ),
                order_no=performance.order.order_no,
                from_value=performance.temp_amount,
                to_value=performance.bonus_amount,
                reason=performance.settle_status,
            )

        # 删除：`PerformanceAdjustment.performance` 是 `PROTECT` ⇒ `ProtectedError`
        # 必须翻成业务码 `1001`（照 `EnvelopeModelViewSet.destroy` 的形状），不许 500。 ⚠️ 用 `queryset.delete()`：Django 的收集器**先收集再删**，被 PROTECT 挡住时
        # 整批一条都不删（天然整批语义），保护对象挂在异常的 `protected_objects` 上。
        try:
            scope_queryset(
                user,
                Performance.objects.filter(id__in=ids),
                # ⭐「按游戏权限收窄」：与上面取行那处**同一条过滤**
                # （漏一处就会出现「校验说看不到、删除却删得掉」）。
                scope_filter=game_scope("order__game_id"),
            ).delete()
        except ProtectedError as exc:
            protected_ids = sorted(
                {
                    obj.performance_id
                    for obj in exc.protected_objects
                    if isinstance(obj, PerformanceAdjustment)
                }
            )
            raise DeleteError(
                _delete_reject_message(
                    [
                        f"{perf_id}（还有调整记录引用它）"
                        for perf_id in (protected_ids or ids)
                    ]
                ),
                ErrorCode.CONFLICT,
            ) from exc

        return DeleteResult(deleted=ids)
