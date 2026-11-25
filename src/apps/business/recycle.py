"""订单的**软删 / 恢复**（业务方裁定，D-0144；spec §6.1）。 ⛔ **不做彻底删除**：对标三路证据一致指向没有 purge（spec §2.1.3），且 `OrderRefund.order` 是 `PROTECT`（既有设计明确假设订单永不物理删除）。 ## 写入点（`Order.deleted_at` / `Performance.deleted_at` 这两列的写入落在这里） | 列 | 本模块的函数 | 写法 | |---|---|---| | `Order.deleted_at` / `deleted_by` | `soft_delete_order` | `.save(update_fields=["deleted_at", "deleted_by"])` | | 同上（清回） | `restore_order` | 同上（写 `None` / `""`） | | `Performance.deleted_at` / `deleted_by` | `soft_delete_order` 的 `is_del_emp="Y"` 支（**含**已删订单的幂等早返回路径） | `queryset.filter(deleted_at__isnull=True).update(...)`（该单**尚未标记**的绩效行，一条 UPDATE；已标记的**不刷新** `deleted_at`） | | 同上（清回） | `restore_order` | 同上（写 `None` / `""`；⭐ 2026-11-05 起绩效行**不再**随恢复清回——业务方裁定） |
⭐ **恢复不再恢复绩效行**（业务方 2026-11-05 裁定；此前的「`deleted_at >=` 订单删除时刻的绩效行一并复活」已废止）：`restore_order` 只恢复订单本身，绩效行保持已删（需要绩效请重新处理）。`soft_delete_order` 的正式路径仍把订单的 `deleted_at` **传下去**给标记绩效行那一步——同一次操作里订单与绩效行共用同一个时间戳值（审计/留痕口径一致，不再各取一次 `timezone.now()`）。 ⚠️ 已删订单的幂等 `isDelEmp="Y"` 早返回路径仍用**当时**的 `timezone.now()`（绩效行确实是那一刻被标掉的）。 ⚠️ 这份枚举经两次 `grep -rn "deleted_at\\|deleted_by" --include=*.py apps/ | grep -v migrations` 复核过的。**其余命中要么是别的模型的同名列**（`Game` / `ProjectCategory` / `Project` / `StaffProfile` 的 `deleted_at`，以及 `accounts` 线自己那套）， **要么是对本列的读**（`OrderQuerySet.dead()`、`selectors.search_recycle_bin`、 `order_views._order_including_deleted`，以及各处的 `order__deleted_at__isnull=…`
跨关系条件）——**没有第三处写入**。 ⚠️ 上面那句「没有第三处写入」在修复 M1 之前**是假的**：`OrderSerializer` / `OrderUpdateSerializer` 都是 `exclude = []`，不列只读就允许 `POST /orders/` / `PATCH /orders/<单号>/` 直接写这两列（实测：新建的单出生即已删、 PATCH 能把在库单改成已删，且只留 `order.update` 审计行）。现在两列由 `api/serializers.py` 的 `SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS` 挡住，判据见 `tests/test_order_soft_delete_write_guard.py`。 ## 同事务与留痕「订单列 + 绩效列 + 审计行 + 鸡蛋铺同步事件」在**同一个** `with transaction.atomic():` 里（spec §6 的纪律）：软删意味着「鸡蛋铺那张对应的单也该撤下来」（业务方裁定），所以首次删除分支在 `record(...)` 之后调 `jidanpu_hall.queue_withdraw(order)`——**只写台账、不调上游**（上游由 worker 在事务外做；⭐ 起 `queue_withdraw` 另外会排一次「**提交后当场试一次**」，本地删除提交后立刻去鸡蛋铺撤下，当场失败照旧落回队列重试）。 ⚠️ 幂等早返回分支**不**排（见 `soft_delete_order`）。 ⭐ **恢复是删除的反向动作**（R1）：`restore_order` 同样在 `record(...)` 之后调 `jidanpu_hall.restore_launch(order)`（作废在飞撤下 + 继承远端单号重抛），**同一笔事务**。 **判定（存在性 / 越权 / 参数合法性）一律在调用方、在事务开始之前**完成—— `atomic()` 是事务不是回滚点，块内正常 `return` 会**提交前半截**（E-0022）。"""
import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu_assignment_lock, jidanpu_member, jidanpu_upstream, split_group
from apps.business.game_scope import assert_game_allowed
from apps.business.models import (
    Order,
    OrderStatus,
    Performance,
    SendResult,
    TaobaoOrderSendLog,
)
from apps.business.peer_sync import PEER_DIRECT_PENDING_MESSAGE
from apps.business.validation import ValidationProblem
from apps.core.error_codes import ErrorCode

logger = logging.getLogger(__name__)


def _mark_unmarked_performances(*, order, actor_name, at):
    """把该单**尚未标记**的绩效行标成已删；**已标记的不刷新** `deleted_at`。
    判据 `.filter(deleted_at__isnull=True)`（E-0021 一族：这里的边界是「有没有值」，
    不是真值）：已删行的删除时间是一条历史事实，被后来的重复请求覆盖掉就等于伪造了「什么时候删的」。"""
    Performance.objects.filter(order=order, deleted_at__isnull=True).update(
        deleted_at=at, deleted_by=actor_name
    )


def _delete_single_order(*, order, actor, actor_name, is_del_emp, at, reason="", sync_personal_orders=True, sync_peer_orders=True):
    """在调用方事务里软删一笔订单并记录原因。"""
    order.deleted_at = at
    order.deleted_by = actor_name
    order.save(update_fields=["deleted_at", "deleted_by"])
    if is_del_emp == "Y":
        # ⚠️ 传**订单的** `deleted_at`（不是另取一次 `timezone.now()`）：同一次删除里订单与
        #    绩效行共用同一个时间戳值（审计/留痕口径一致）。
        _mark_unmarked_performances(order=order, actor_name=actor_name, at=at)
    record(
        # ⚠️ `log_type` **不写死**，一律查登记表 `ACTION_LOG_TYPES`（`apps/audit/actions.py`）：
        # 它是「动作 → 日志分类」的唯一出处，写死常量会让「把登记表里的分类改坏」这条变异不在这条留痕用例上变红（只有键集对拍的 `test_operation_log_model` 会红）
        # ——那就等于绕开了单一出处。取值由 `tests/test_order_recycle.py` 钉**字面值 10** （E-0024：钉值不钉常量）。
        log_type=ACTION_LOG_TYPES["order.delete"],
        action="order.delete",
        actor=actor_name,
        actor_id=getattr(actor, "pk", None),
        # ⭐ `getattr(..., None)` 是**防御性**写法——生产的唯一调用点
        # （`order_writes` 的软删、`/restore/` 的视图）传的都是认证态 `User`。拿不到主键
        # 的档（探针 / 直调用例）⇒ **值级**声明机器来源，⛔ 不静默通过（见 `log.py`）。
        system=getattr(actor, "pk", None) is None,
        target_kind="order",
        target_id=str(order.pk),
        order_no=order.order_no,
        source_order_no=order.source_order_no,
        field_name="delete_with_performance" if is_del_emp == "Y" else "delete_keep_performance",
        reason=reason,
    )
    # 租户级鸡蛋铺撤单已拆除。首次软删只给个人绑定排退单。 ⭐ 下游鸡蛋铺线（Task 6 触发点⑤，spec §5.3 第 4 行的「删除」）：软删**首次**生效
    # ⇒ 这张单在对方系统里也不该再有 ⇒ **同事务**给已推过的本线绑定排退单。 ⚠️ 门槛与去重由 `queue_withdraw` 自判（没推出去的单回 0 行）；它同样**没有绑定
    # 闸门**。⛔ 位置在 `transaction.atomic()` 块**内**、 **只在这个首次删除分支**（幂等早返回路径不排）。
    if sync_personal_orders:
        jidanpu_member.queue_withdraw(order)
    # ⭐ 大厅离厅快照（D-0296）：软删**不经状态机**，但在厅的单被删同样会从大厅消失 ⇒ 首次删除分支在这里补一条灰卡（快照写入点的枚举见 `hall_snapshot.py` 模块 docstring——软删是状态机之外的那一个原生写点）。
    # ⚠️ **只有「在厅」（`WAITING_GRAB`）的单才写**：别的状态本来就不在大厅，没有「离厅」可记。⚠️ 软删不解除挂单（`hall_price` 原样），在厅视图的
    # `hall_price` 就是抛单价——覆写理由见 `hall_snapshot.native_payload`。 ⚠️ 快照是读面的辅助数据：失败响亮留痕，⛔ 不把一次合法的软删打挂。
    if str(order.status) == str(OrderStatus.WAITING_GRAB):
        # ⭐ 团团通之间的大厅镜像（D-0303）：在厅的单被软删 ⇒ 它也从**下家的大厅**里消失 ⇒ 同事务排一条 `hall.take`（spec §2.1：离开 `WAITING_GRAB` 的离开路
        # 之一；软删不经状态机 ⇒ 这是 `apply_transition` 之外的**第二个**写点，与上面离厅快照的写点枚举同源）。零绑定 ⇒ 入队函数自己一行都不写。
        # ⚠️ 函数内懒导入（与本模块其余跨线调用同款）。
        from apps.business.peer_dispatch import enqueue_hall_event
        from apps.business.peer_sync import HALL_TAKE_KIND

        if sync_peer_orders:
            enqueue_hall_event(order, kind=HALL_TAKE_KIND)
        try:
            from apps.business import hall_snapshot

            hall_snapshot.record_native_exit(
                order, exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.WITHDRAWN,
                at=at,
            )
        except Exception:  # noqa: BLE001 —— 辅助数据失败不回滚软删（响亮留痕）
            logger.exception(
                "软删的大厅离厅快照写入失败（不影响删除）order=%s", order.order_no
            )
    # ⭐ 上家鸡蛋铺线（Task 9 触发点②，spec §15.4 / A12）：软删**首次**生效 ⇒ 这张单在**上家**那边也不该再挂着，但上家打手账号**没有删单权限**、业务方裁定
    # 「不同步上去」⇒ 这里**一次上游写都不打**（⛔ 不排 outbox），只留一条台账人话（代价：上家那张单会一直挂着，人工沟通——spec §13.1-R12，⛔ 不当作缺陷）。
    # ⚠️ 与上面 `jidanpu_hall` / `jidanpu_member` 两条**形状不同**是刻意的：那两条是**别的线**对**它们自己的**远端写的、有真待办；本线是纯留痕。 ⚠️ 位置在**首次删除分支**（上面的幂等早返回路径不调）：本次没有新的删除动作。
    jidanpu_upstream.on_order_deleted(order)
    # ⭐ 开放平台 v2（T8，设计文档 §7.2 / §7.2.1）：软删**首次**生效 ⇒ 在**同一笔事务**里给每个配了回调的应用写一行 `order.deleted` 推送（⛔ 不是 `on_commit`：那会让本次删除回滚了、推送行却留下）。 ⚠️ **只在这个首次删除分支**排（上面的幂等早返回路径不排）：本次没有新的删除动作发生，重复排会让第三方收到两条"这单删了"，与上面三条 `jidanpu_*` 同口径。 ⭐ **时刻必须显式取 `at`**（= 刚写进 `order.deleted_at` 的那个值），
    # ⛔ **不能**从 `order.updated_at` 取：本分支的 `update_fields` 只有 `deleted_at` / `deleted_by` ⇒ `auto_now` 那一列**两边都不会被刷新** ——Django 的 `Model._save_table` 只对 `update_fields` 里列出的字段调 `f.pre_save()`（`auto_now` 挂的就是它；`DateTimeField.pre_save` 的 `auto_now`
    # 分支才 `setattr`）⇒ 读 `order.updated_at` 拿到的是**更早**的时刻，而那正是 §7.2.1 点名的坑。 ⚠️ 报文字段（单号 / 来源单号）从**现场握着的** `order` 取：软删之后默认 manager 取不到这行，而删除现场本来就有这个实例（§7.2.1 已实测「报文字段仍可读」）。 ⚠️ 函数内懒导入（与本模块其余跨线调用同款）。 ⚠️⭐ **`spec=OPENAPI_SPEC` 是显式的**（回调线 T1b）：投递引擎的公开入口从 T1b 起 **不再有**"默认回上家侧"那一格（漏传 = 当场 `TypeError`）⇒ 本处（上家侧那一族的写入点）显式声明自己是哪一族。⛔ 不许靠默认值省这一句：那正是"打手侧的回调静默写进上家侧台账"的形状（`apps/openapi/dispatch.py` 模块 docstring 第 4 条）。
    from apps.openapi import dispatch as openapi_dispatch
    from apps.openapi.credentials import OPENAPI_SPEC
    from apps.takerapi.api import callback_recipients as taker_callback

    openapi_dispatch.notify_order_deleted(
        order, spec=OPENAPI_SPEC, occurred_at=at
    )
    # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 6 行）：**紧挨着**上面那句，同一个事务、同一个 `at`（= 刚写进 `deleted_at` 的那个值）。 ⚠️ 收件人 = **当前**接手人名下的应用（spec §4.2）：库里那条 MAIN 绩效行此刻
    # **照样查得到**——`Performance.objects` 是普通 manager，`is_del_emp="Y"` 那一路只是**标记** `deleted_at`、不会让行从默认查询里消失 ⇒ `current_taker_id` 不需要
    # "删前先存一份"的特殊处理（⛔ 别在这里手工再取一次接单人：那就是第二份判据）。 ⚠️ 只在这个首次删除分支排（上面那条幂等早返回路径不排），与上一句同口径。
    taker_callback.notify_order_deleted_for_taker(order, occurred_at=at)


def _split_delete_price(order, anchor, actor, *, assert_game=None):
    if anchor is None or anchor.pk == order.pk or order.total_price == 0:
        return None
    if anchor.game_id != order.game_id:
        if assert_game is None:
            assert_game_allowed(actor, anchor.game)
        else:
            assert_game(anchor.game)
    new_price = anchor.total_price + order.total_price
    try:
        Order._meta.get_field("total_price").run_validators(new_price)
    except ValidationError as problem:
        raise ValidationProblem("返还子单接单价后，主单接单价超出有效金额范围") from problem
    return new_price


def validate_soft_delete(order, actor, *, assert_game=None):
    """批量预检只做校验，执行时在锁内复核。"""
    if order.deleted_at is None and order.split_id:
        _split_delete_price(order, split_group.active_members(order.split_id).first(), actor, assert_game=assert_game)


def _transfer_deleted_child_price(order, anchor, new_price, actor, actor_name, *, recheck_external_refunds=True):
    from apps.business.order_writes import WriteContext, _record_taker_price_change, queue_externalpay_recheck

    actor_id = getattr(actor, "pk", None)
    context = WriteContext(actor=actor_name, log_actor=actor_name, log_actor_id=actor_id)
    anchor_old_price = anchor.total_price
    child_old_price = order.total_price
    anchor.total_price = new_price
    anchor.updated_by = actor_name
    anchor.save(update_fields=["total_price", "updated_by", "updated_at"])
    # 恢复出的子单是独立单，已转回主单的金额不能再计一遍。
    order.total_price = Decimal("0.00")
    order.updated_by = actor_name
    order.save(update_fields=["total_price", "updated_by", "updated_at"])
    if recheck_external_refunds:
        queue_externalpay_recheck(anchor)
        queue_externalpay_recheck(order)
    _record_taker_price_change(anchor, anchor_old_price, new_price, context, reason="删除子单返还接单价")
    _record_taker_price_change(order, child_old_price, order.total_price, context, reason="删除子单接单价已转回主单")


def _ship_after_member_removed(order, remaining):
    members_to_ship = split_group.shipping_members(remaining)
    if not members_to_ship or any(
        member.status != OrderStatus.COMPLETED for member in members_to_ship
    ):
        return
    # 兼容旧记录：出组的单可能是唯一留有成功回执的组员。
    if TaobaoOrderSendLog.objects.filter(order_no=order.order_no, result=SendResult.SUCCESS).exists():
        return
    from apps.business.xgj_ship import schedule_auto_ship_gated

    schedule_auto_ship_gated(members_to_ship[0])


def soft_delete_order(
    *, order, actor, is_del_emp, reason="", actor_name=None,
    recheck_external_refunds=True, sync_personal_orders=True, sync_peer_orders=True, assert_game=None,
):
    """首次软删在同一事务处理组成员、金额、绩效、日志和回调。"""
    if actor_name is None:
        actor_name = actor.get_username()
    with transaction.atomic():
        # 按组内 id 顺序取锁，之后重读删除状态与金额，避免旧对象重复返还。
        group_id = order.split_id
        members = []
        if group_id:
            members = list(
                Order.objects.select_for_update().filter(split_id=group_id).order_by("id")
            )
        order.refresh_from_db(from_queryset=Order.all_objects.select_for_update())
        if order.peer_direct_pending is not None:
            raise ValidationProblem(PEER_DIRECT_PENDING_MESSAGE)
        now = timezone.now()
        if order.deleted_at is not None:
            if is_del_emp == "Y":
                _mark_unmarked_performances(order=order, actor_name=actor_name, at=now)
            return
        if order.split_id and order.split_id != group_id:
            raise ValidationProblem("拆分组成员已变化，请刷新后重试", ErrorCode.BUSINESS)

        peer_member = split_group.is_peer_member(order)
        anchor = members[0] if order.split_id and members else None
        cascade_members = []
        if split_group.cascades_on_delete(order, anchor):
            cascade_members = members[1:]
        for candidate in (order, *cascade_members):
            if (
                (candidate.status == OrderStatus.IN_PROGRESS or candidate.penalty_approval_id is not None)
                and not jidanpu_assignment_lock.lock_order_change(candidate.pk)
            ):
                raise ValidationProblem(jidanpu_assignment_lock.ORDER_BUSY, ErrorCode.BUSINESS)
        affected = {order.pk, *(member.pk for member in cascade_members)}
        if anchor is not None:
            affected.add(anchor.pk)
        if Performance.objects.filter(order_id__in=affected, online_payroll_batch__isnull=False).exists():
            raise ValidationProblem("订单关联了在线付款单，不能删除或返还拆单金额", ErrorCode.BUSINESS)
        new_price = _split_delete_price(order, anchor, actor, assert_game=assert_game)
        if new_price is not None:
            _transfer_deleted_child_price(
                order, anchor, new_price, actor, actor_name, recheck_external_refunds=recheck_external_refunds,
            )
        _delete_single_order(
            order=order, actor=actor, actor_name=actor_name,
            is_del_emp=is_del_emp, at=now, reason=reason,
            sync_personal_orders=sync_personal_orders, sync_peer_orders=sync_peer_orders,
        )
        # ⭐ G10 级联：其余活跃成员逐一走**同一个单笔口径**（各自绩效标记、各自
        #    `order.delete` 留痕 + reason「随拆单组主单删除」、各自的上下游事件与回调）；
        #    已软删的成员**幂等跳过**（那条的 deleted_at / 留痕是它自己的历史）。
        cascade_reason = f"随拆单组主单删除（{reason}）" if reason else "随拆单组主单删除"
        for member in cascade_members:
            if member.deleted_at is not None:
                continue
            _delete_single_order(
                order=member, actor=actor, actor_name=actor_name,
                is_del_emp=is_del_emp, at=now, reason=cascade_reason,
                sync_personal_orders=sync_personal_orders, sync_peer_orders=sync_peer_orders,
            )
        if peer_member:
            group_id = order.split_id
            order.split_id = ""
            order.split_kind = ""
            order.save(update_fields=["split_id", "split_kind"])
            remaining = list(split_group.active_members(group_id))
            if len(remaining) == 1:
                survivor = remaining[0]
                survivor.split_id = ""
                survivor.split_kind = ""
                survivor.save(update_fields=["split_id", "split_kind"])
                if str(survivor.status) == str(OrderStatus.COMPLETED) and not (
                    TaobaoOrderSendLog.objects.filter(
                        order_no__in=[member.order_no for member in members], result=SendResult.SUCCESS
                    ).exists()
                ):
                    from apps.business.xgj_ship import schedule_auto_ship_gated

                    schedule_auto_ship_gated(survivor)
            elif len(remaining) >= 2:
                _ship_after_member_removed(order, remaining)
        elif order.split_id != "":
            if cascade_members:
                # ⭐ G10 收尾：整组同删 ⇒ 主单与**全体**子单（含「本轮之前就已被删」的成员——它们的 `split_id` 是本线之前留下的）`split_id` **一并清除**
                # （R7：删组即终局，恢复任一成员各自为独立单——主单带客户价、子单全 0，钱各算各的）。无剩余组 ⇒ G8 收口自然不触发。
                Order.all_objects.filter(split_id=order.split_id).update(
                    split_id="", split_kind=""
                )
                order.split_id = ""
                order.split_kind = ""
                for member in cascade_members:
                    member.split_id = ""
                    member.split_kind = ""
            else:
                # ⭐ G9 出组：删的是**非主单成员**（或组内唯一残留）⇒ 同事务清本单 `split_id`；随后活跃成员**剩 1** ⇒ 那一笔的也清（拍板 6：
                # 「组内只剩一笔时自动变回独立单」）。prune 判数走**默认 manager** （本事务里刚写的删除当场可见），且成员行已在上面加过锁 ⇒ 判数稳定（行锁判据；并发双删各判各的、结果一致，
                # 极小漏裁窗口 = R6 残留）。 ⚠️ 先把组 ID 取进局部再清列：下面的 prune / 收口判数都认**原组 ID** （清完再拿 `order.split_id` 查就是空串 ⇒ 会查出全库非拆单行）。
                group_id = order.split_id
                order.split_id = ""
                order.split_kind = ""
                order.save(update_fields=["split_id", "split_kind"])
                remaining = list(split_group.active_members(group_id))
                if len(remaining) == 1:
                    survivor = remaining[0]
                    survivor.split_id = ""
                    survivor.split_kind = ""
                    survivor.save(update_fields=["split_id", "split_kind"])
                    remaining = []
                    # 已完成的幸存单转为独立单，结算和自动发货按独立单规则补做。
                    if str(survivor.status) == str(OrderStatus.COMPLETED) and not (
                        TaobaoOrderSendLog.objects.filter(
                            order_no=survivor.order_no, result=SendResult.SUCCESS
                        ).exists()
                    ):
                        from apps.business.settlement_hooks import (
                            _recompute_main_commission_for_group,
                        )
                        from apps.business.xgj_ship import schedule_auto_ship_gated

                        if not TaobaoOrderSendLog.objects.filter(
                            order_no=order.order_no, result=SendResult.SUCCESS
                        ).exists():
                            schedule_auto_ship_gated(survivor)
                        _recompute_main_commission_for_group(survivor)
                if len(remaining) >= 2:
                    _ship_after_member_removed(order, remaining)
                    # 旧拆单的客服结算仍须等待所有在组订单完成。
                    if all(member.status == OrderStatus.COMPLETED for member in remaining):
                        from apps.business.settlement_hooks import (
                            _recompute_main_commission_for_group,
                        )

                        _recompute_main_commission_for_group(remaining[0])
        if new_price is not None:
            from apps.business.settlement_hooks import refresh_customer_estimate

            anchor.refresh_from_db(fields=["split_id", "split_kind"])
            refresh_customer_estimate(anchor)


def restore_order(*, order, actor=None, actor_name=None):
    """把已软删的订单恢复（**幂等**：没删过的再恢复 ⇒ 什么都不写、不留痕）。 ⚠️ **恢复只恢复订单本身，绩效行不随订单恢复（业务方 2026-11-05 裁定）**：此前是 `deleted_at >= 订单删除时刻` 的绩效行一并复活——该行为已废止，绩效行要动请走「绩效管理」。
    ⛔ 这不改「删订单时把绩效行一起标记」那条既有行为（D-0144）——只是恢复这一侧不再清它们的标记。恢复不再把单抛回租户级鸡蛋铺：那条线已拆除，这里不调它。"""
    if order.deleted_at is None:
        return                     # 幂等
    if actor_name is None:
        actor_name = actor.get_username()
    with transaction.atomic():
        # ⭐ N6（R3-M7，业务方拍板）：删除时**在厅**的单恢复后回「未分配」，等业务方**手工**重新抛单；⛔ 不自动回厅。 - 判据：`WAITING_GRAB`（删除时在大厅）**且** `hall_card_id is None` （隐藏单不在此列——它的状态由镜像卡生命周期管，这里不碰）； ⭐ D-0325：**上家镜像卡**派生的隐藏单（`upstream_hall_card_id`）
        # 同一条口径（状态归 `upstream_hall_shadow` 的钩子，恢复不把它翻成真单）。 - ⛔ 不排 `hall.throw` / `jidanpu_member.queue_hall_launch` / 任何重抛事件：业务方原话是「恢复后是未分配、需要重新抛单」，且自动补事件会引入「事件时刻陈旧」与「旧 take 拦新 throw」两个新问题（刻意不修）；
        # - ⛔ 不走 `apply_transition`：这不是「抛厅」，是「收回在厅状态」； D-0308 的 36 节点门管的是「抛」，不管「收」； - 写形状**定死**为 `order.status = …` + `save(update_fields=[...])`： `tests/test_order_time_fields.py` 的状态写点网按**赋值形态**收键，换成 `Order.objects.filter(...).update(status=…)` 等于绕过登记。
        back_to_unassigned = (
            str(order.status) == str(OrderStatus.WAITING_GRAB)
            and order.hall_card_id is None
            and order.upstream_hall_card_id is None
        )
        order.deleted_at = None
        order.deleted_by = ""
        update_fields = ["deleted_at", "deleted_by"]
        # 已成功发过货的单即使恢复为独立单，原组仍要识别其成功回执。
        if not order.split_id and order.split_ship_main_order_no is not None and not (
            TaobaoOrderSendLog.objects.filter(
                order_no=order.order_no, result=SendResult.SUCCESS
            ).exists()
        ):
            order.split_ship_main_order_no = None
            update_fields.append("split_ship_main_order_no")
        if back_to_unassigned:
            order.status = OrderStatus.UNASSIGNED
            # ⚠️ `save(update_fields=…)` 只对列出的列跑 `auto_now`（`peer_inbound.py`
            #    的既有写法与解释同源）⇒ 必须显式带上 `updated_at`。
            update_fields += ["status", "updated_at"]
        order.save(update_fields=update_fields)
        record(
            # 同 `soft_delete_order`：`log_type` 走登记表，不写死常量。
            log_type=ACTION_LOG_TYPES["order.restore"],
            action="order.restore",
            actor=actor_name,
            actor_id=getattr(actor, "pk", None),
            # ⭐ 同 `soft_delete_order`（值级机器来源的显式声明）。
            system=getattr(actor, "pk", None) is None,
            target_kind="order",
            target_id=str(order.pk),
            order_no=order.order_no,
        )
        # ⛔ **改准**：这里原先还有一句 `jidanpu_hall.restore_launch(order)` （恢复的**反向动作**：作废在飞撤下 + 继承远端单号重新抛回**租户级**那条线的鸡蛋铺）。
        # 那个模块与整条线已整条拆除 ⇒ 恢复这一侧**不再调它**（代价见本函数 docstring 末段）。 ⚠️ 位置口径不变：恢复的写入仍全部在 `transaction.atomic()` 块内、`record(...)` 之后。
