"""站内通知的纯逻辑（spec §3.5 / §3.6 / §7.4 / §8.2）。**不碰 request**。 ⚠️ **通知不保证送达，是提示而非凭据**：收件人解析不到就不写行、将来按保留期清理也只是删掉提示。**不要拿它当审计 / 结算依据**——审计看 `OperationLog`（`apps/audit`，唯一写入出口 `apps/audit/log.py::record(...)`；旧 `AuditLog` 自 起只为历史保留、**代码不再写它** ——合并整合（M3a + M3b）后这句话**无条件为真**：`main` 的 P4 写入点与
订单来源 / 店铺对接三条线全部改走 `record()`，生产调用方 `grep` 归零），结算看 `Performance`。触发点（控制方 R2：**不在视图里散落**，挂「已有的真实出口」）： - `被抢走` / `待审核` → `order_status.register_target_hook`。它在 `apply_transition` 内、 `order.save()` **之后**跑，所以与状态写入同属调用方的 `transaction.atomic()` （spec §7.4 要求 `/claim`、`/assign` 的通知与状态/绩效同事务）。
- `被指派` → `Performance` 的 `pre_save` + `post_save` 信号。**为什么不能用 target hook**： `/assign` 的顺序是 `apply_transition(...IN_PROGRESS...)` **之后**才 `set_taker(perf, taker)`； hook 在 `apply_transition` 里跑，那时 `Performance.user` **还是空的**，读不到接单人。不要用 `order.updated_by` 找抛单人。`apply_transition` 只在传入 `actor` 时才覆盖它。 `/claim` 不传 `actor`，所以抢单当时它不是抢单者；但改价、编辑、带 actor 的状态流转都会改它，它只是「最后一个动过这单的人」。收件人取 `created_by`（录单时写入，稳定）。录单人和抛单人不是同一人时，通知会落到录单人。"""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.business.models import (
    EmpType,
    Notification,
    NotificationKind,
    OrderStatus,
    Performance,
)
from apps.business.order_status import register_target_hook
from apps.business.roles import SUPERVISOR


def _payload(order, from_status=None):
    """展示用载荷。**只放单号与来源态**——金额不进通知（见模型 docstring）。 ⭐（D-0301）：补 `source_order_no`——它是前端「来源号优先、空回退
    `order_no`」展示回退的取数前提；`order_no` 本身**照旧给**（消费方定位那张单的事实字段，⛔ 不从这里删键）。 ⚠️ 只影响**新建**的通知行：存量行的 payload 没有这个键，读面实时读 `order` FK 的
    `NotificationSerializer.source_order_no` 才是展示的可靠来源。"""
    data = {
        "order_no": order.order_no,
        "source_order_no": order.source_order_no,
    }
    if from_status is not None:
        data["from_status"] = str(from_status)
    return data


def notify_assigned(*, order, user_id):
    """`被指派`：收件人 = 该单的接单人（控制方 R1）。"""
    if not user_id:
        return None
    return Notification.objects.create(
        user_id=user_id,
        kind=NotificationKind.ASSIGNED,
        order=order,
        payload=_payload(order),
    )


def notify_grabbed(order, from_status):
    """`被抢走`：收件人 = **抛单人**（`order.created_by` 按 `username` 解析）。
    解析不到（空串 / 无此用户 / 已停用）⇒ **返回 `None`、不写行**，也**不抛异常**
    ——通知不保证送达，不该让抢单失败（控制方 R1）。"""
    username = (order.created_by or "").strip()
    if not username:
        return None
    thrower = (
        get_user_model().objects.filter(username=username, is_active=True).first()
    )
    if thrower is None:
        return None
    return Notification.objects.create(
        user_id=thrower.id,
        kind=NotificationKind.GRABBED,
        order=order,
        payload=_payload(order, from_status),
    )


def notify_pending_review(order, from_status):
    """`待审核`：**扇出**给该租户内全体客服主管（`客服主管` Group 且 `is_active=True`）。一个客服主管都没有 ⇒ **不写行**（返回空列表）。通知不保证送达。
    ⚠️ **2026-09-20 换词（RBAC 合并预检）**：旧职位「审核人」在存量迁移里并入「客服主管」（控制者裁决 C4），所以收件人 Group 从 `审核人` 改为 `客服主管`
    （`apps/business/roles.py::SUPERVISOR`）——`REVIEWER` 这个旧常量已随换词删除，本模块原先 `from apps.business.roles import REVIEWER` 会在导入期 `ImportError`。"""
    reviewer_ids = list(
        get_user_model()
        .objects.filter(groups__name=SUPERVISOR, is_active=True)
        .values_list("id", flat=True)
    )
    rows = [
        Notification(
            user_id=user_id,
            kind=NotificationKind.PENDING_REVIEW,
            order=order,
            payload=_payload(order, from_status),
        )
        for user_id in reviewer_ids
    ]
    if not rows:
        return []
    return Notification.objects.bulk_create(rows)


# ── target hook：`被抢走` / `待审核`（spec §7.4 的事务由调用方保证）────────
def _main_performance_taker_id(order):
    """该单 MAIN 绩效此刻的归属人 id；没有行 / 行上没人 ⇒ `None`（**用 `is not None` 口径判**）。
    只回答「这条 MAIN 行上现在有没有归属人」这一个事实——它是 `被抢走` 判据的**真实出口**，
    不是「谁接的」（那要看具体 id）。"""
    # ⭐ D-0144（C15）：`order` 是**已取到的实例**；这条 join 条件管的是「实例来自已软删单」
    #    那条路 ⇒ 已删单的 MAIN 行在这里等于「没有归属人」，不发通知。
    return (
        Performance.objects.filter(
            order=order, emp_type=EmpType.MAIN, order__deleted_at__isnull=True
        )
        .values_list("user_id", flat=True)
        .first()
    )


def _on_in_progress(order, from_status):
    """迁到 `IN_PROGRESS` 时的通知。 - `WAITING_GRAB → IN_PROGRESS` **且该单 MAIN 绩效真的有归属人** = **被抢走** ⇒ 通知抛单人； - `UNASSIGNED → IN_PROGRESS` = **指派** ⇒ **不在这里发**（由 `Performance` 信号负责，因为此刻 `Performance.user` 还没写）；
    - `ISSUE` / `PAUSED → IN_PROGRESS` = **恢复** ⇒ 不发。只看来源态不够：通用 `/transition/` 也能走这条边，但不写 `Performance.user`。没有归属人就发，会给抛单人一条假的「被抢走」。这条通用端点的门是节点 16（`order.change_status`）。只持节点 50 （`grab_hall.take_order`）的人在这里会被 403 挡住；50 在状态机边集里，是因为
    `/claim/` 是另一个写端。状态机取并集，不等于这个端点自己的门。写**非空** `user` 的是 `/assign` 的 `set_taker` 与 `/claim` 的条件更新。 `reset_taker_performance` 写的是 `None`。判据用 `is not None`。 `/claim` 先写 `user` 再迁状态，所以抢单时钩子读得到归属人；通用路径读到 `None` 就不发。已知可达：持节点 16 的人用通用 `/transition/` 可以把单停在「进行中且无接单人」。这条边没有写 `Performance.user` 的目标态钩子。"""
    if str(from_status) != str(OrderStatus.WAITING_GRAB):
        return
    if _main_performance_taker_id(order) is None:
        # 大厅边被通用 `/transition/` 走通但没有接单人 ⇒ **不发假通知**（见 docstring）。
        return
    notify_grabbed(order, from_status)


def _on_pending_review(order, from_status):
    """迁到 `PENDING_REVIEW`：**当前口径**是「任何来源」都发 `待审核` 给全体客服主管。进入「待审核」这个状态本身就意味着「有人要审」；驳回后重交会再发一次，正确。 ⚠️ **这个口径有一个已知反例（2026-09-20 B2 整体评审 FIX 8，如实登记）**：
    `ISSUE/PAUSED → PENDING_REVIEW` 这两条边（节点集都含 `order.change_status`） **也会**触发本钩子，而那种单 `/review` 会被**来源态**拒（`tests/test_review_api.py` 已实测可达）⇒ 客服主管收到一条**点不动**的提示。
    所以上面**不能**写成「任何来源都必然点得动」这类绝对句（AGENTS §5 / E-0011）。 **本轮保留现口径**（进入「待审核」就通知——它至少说明了「有条单要看一眼」）；若将来要收紧，加 `from_status == OrderStatus.IN_PROGRESS` 守卫即可。"""
    notify_pending_review(order, from_status)


register_target_hook(OrderStatus.IN_PROGRESS, _on_in_progress)
register_target_hook(OrderStatus.PENDING_REVIEW, _on_pending_review)


# ── `被指派`：Performance 的 pre_save + post_save（R2 的第二个出口）──────
@receiver(pre_save, sender=Performance)
def _remember_previous_taker(sender, instance, update_fields=None, **kwargs):
    """把该行**数据库里**的 `user_id` 暂存在实例上，供 `post_save` 判「之前有没有人接」。新建行（`pk is None`）没有「之前」⇒ 记 `None`（`post_save` 会因 `created=True` 跳过）。 ⚠️ **收窄查询（2026-09-20 B2 整体评审 FIX 10）**：只在 `pk` 已存在、且
    （`update_fields is None` 或 `"user" in update_fields`）时才查库—— `/submit`、`/adjust`、`/review` 的 `update_fields` 都**不含** `user`，不必为它们每次 `save()` 多付一条 `SELECT`。
    被跳过时 `_previous_user_id` 取**当前** `user_id`（**不能**留空/不设）： `post_save` 的守卫是「`_previous_user_id is not None`」，留空会让「接单人没变的 save」误发一条 `被指派`。 `set_taker` / `reset_taker_performance` 的 `update_fields` 都**含** `"user"`，所以这两条真实写入点的判据不受影响。"""
    if instance.pk is None:
        instance._previous_user_id = None
        return
    if update_fields is not None and "user" not in update_fields:
        # 本次 `save()` 不写 `user` ⇒ 归属人没有变化；用当前值当「之前的值」，
        # `post_save` 的 `is not None` 守卫据此跳过（**不查库**）。
        instance._previous_user_id = instance.user_id
        return
    # ⭐ D-0144（C16）：已软删单的行取不到「之前的归属人」⇒ `_previous_user_id` 为 `None`
    #    ⇒ `post_save` 的守卫跳过，不给已删单发「被指派」。
    instance._previous_user_id = (
        Performance.objects.filter(pk=instance.pk, order__deleted_at__isnull=True)
        .values_list("user_id", flat=True)
        .first()
    )


@receiver(post_save, sender=Performance)
def _on_performance_saved(sender, instance, created, **kwargs):
    """只有「一条既有的 MAIN 绩效行从**没人接**变成**有人接**」才发 `被指派`。逐条判据（缺一条就会误报，brief §3 R2）： - `created` ⇒ 录单时 `create()` 不算指派；
    - `emp_type == MAIN` ⇒ 客服的「销售提成」行不是指派； - `user_id` 非空 ⇒ 「清除接单人」（`user=None`）不算指派； - `_previous_user_id is not None` ⇒ `/submit` 的 `perf.save()`（接单人没变）**不会重复发**
    （FIX 10 之后，`update_fields` 不含 `user` 时 `pre_save` 把 `_previous_user_id` 直接设成当前 `user_id`，同样命中这条守卫，且**不查库**）。"""
    if created:
        return
    if instance.emp_type != EmpType.MAIN:
        return
    if not instance.user_id:
        return
    if getattr(instance, "_previous_user_id", None) is not None:
        return
    notify_assigned(order=instance.order, user_id=instance.user_id)
