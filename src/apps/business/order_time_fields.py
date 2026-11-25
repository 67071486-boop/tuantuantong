"""订单时间轴两列（`published_at` 发单时间 / `submitted_at` 交单时间）的**唯一写入实现**。批次 5 · 线 E先落了这两列与写入点，随后的修复轮把它们**收到这里一处**。抽出来的理由不是「代码好看」，而是 **E-0021**：同一个事实在多个调用点各写一遍，下一次「多一条路径」时必然漏掉一处——而这次的现场正是**漏了整整两条会进「待验收」的路径** （`peer_inbound` 的入向直写），症状与批次 4 那个缺陷同类：**列有值才筛得出来，漏一处就永远筛不出来**。 ## 两件事实（各自的「谁该调我」） | 函数 | 事实 | 什么时候调（**全部**调用点） | |---|---|---| | `mark_published` | 这张单**第一次被发出去给打手** | ① 抛到大厅成功（`order_status.apply_transition` 里传了 `hall_publisher` 那一支）；② 第一次接手（`claim.claim_order` 与 `performance.set_taker`） |
| `mark_submitted` | 这张单**第一次进入「待验收」** | 一切把状态写成 `PENDING_REVIEW` 的路径——**函数自己判状态**，所以调用方无条件调即可 | ⭐ `mark_submitted` **自己判**「这次是不是待验收」（读 `order.status` 的**内存值**，即调用方**本次刚写进去**的那个值）⇒ 调用方不必各自再抄一份 `target == PENDING_REVIEW` 的判据。 `mark_published` **不能**这样自判：「发出去给打手」这个事实**不由状态列表达** （抛大厅与接手是两个不同的动作，交接那两条路的状态是 `IN_PROGRESS`、抛大厅那条是 `WAITING_GRAB`，而 `IN_PROGRESS` 也能从别的来源态到达）⇒ 由**知道事实发生了的那个调用方**显式调。 ## 两条口径（本模块是它们唯一的落实处） 1. **只写第一次**：`WHERE <列> IS NULL` 进 `UPDATE` ⇒ 重抛 / 改派 / 二次交单**不覆盖**
（这两列的语义是「这张单**最早**什么时候发出去 / 交的单」）。 ⚠️ 判据写在 **SQL 的 `WHERE` 里**，⛔ 不是「先读内存、再决定要不要写」—— 后者在并发下会丢更新（同一个 `claim_order` 里另一个事务刚写过，内存里还是 `None`）。 2. **不清空**：离开「已完成」「待验收」都不动这两列——它们是**筛选维度**，清掉会让历史按这两档筛不出来。⚠️ 与同模型的 `completed_at` **有意不同**：那一列在「离开已完成」时会由 `apply_transition` 清空（既有语义，见 `Order.completed_at` 的注释）。 ## ⚠️ 为什么是独立的一次 `.update()`，⛔ 不是塞进调用方的 `save(update_fields=[…])` `claim_order` 与 `set_taker` 是**直接写库**的（条件更新 / `save`），而调用方手上那份 `order` **是更早读出来的**（`published_at` 还是旧的 `None`）⇒ 把这一列并进那个 `save(update_fields=…)` 就会用陈旧的 `None` 把刚落库的值**擦掉**。本模块的 `.update()` 自带 `IS NULL` 谓词 ⇒ 无论调用方手里那份有多陈旧，都不会覆盖已经写好的值。 ## 内存与库的一致性两个函数在**真的写了**时，把值也写回传进来的那个 `order` 对象（响应序列化器读的是内存对象，不同步会让接口回一个与库里不一致的 `null`）。 ⚠️ 因此调用方**必须**把「本次真正驱动这次写入的那个 `order` 实例」传进来。 ## 事务两个函数都只发一条 `UPDATE`、**自己不开事务** ⇒ 与调用方同生共死（`apply_transition` / `peer_inbound` 的调用点都在 `transaction.atomic()` 里）。"""
from django.utils import timezone

from apps.business.models import Order, OrderStatus


def mark_published(order, *, moment=None):
    """订单**第一次被发出去给打手** ⇒ 记下那一刻。返回「这次有没有写」（`bool`）。 `moment` 缺省取 `timezone.now()`；调用方要「与另一个时间戳同一时刻」时显式传
    （`claim_order` 与 `set_taker` 都传它们写 `claimed_at` 用的**同一个** `now` 对象——
    各取一次 `timezone.now()` 会差几微秒，读起来像两件事）。 ⚠️ **只写第一次、不清空**（模块 docstring 那两条口径）——重抛 / 改派都不覆盖。"""
    return _mark(order, field="published_at", moment=moment)


def mark_submitted(order, *, moment=None):
    """保存新状态后补首次交单时间。回传传事件时刻，本地流转用当前时刻。"""
    if str(order.status) != str(OrderStatus.PENDING_REVIEW):
        return False
    return _mark(order, field="submitted_at", moment=moment)


def _mark(order, *, field, moment=None):
    """两列共用的那一句写入：**只改这一列、且只改这一列仍是 `NULL` 的行**。 ⚠️ 三件必须一起做的事（逐条都有判据钉着，见 `tests/test_order_time_fields.py`）：
    1. `WHERE <列> IS NULL` —— 「只写第一次」的落实处（判据在 SQL 里，⛔ 不是内存里）； 2. 没写进任何行（`updated == 0`）⇒ **不动内存对象**、回 `False`；
    3. 真写了 ⇒ 把值同步回内存对象（响应序列化器读它），并回 `True`。 ⚠️ `updated_at` 一并带上：`QuerySet.update()` **不会**替调用方跑 `auto_now` （与 `claim.py` 的条件更新同一条口径——那一处的注释把这条读码证据写全了）。"""
    value = moment if moment is not None else timezone.now()
    updated = (
        Order.objects.filter(pk=order.pk, **{f"{field}__isnull": True})
        .update(**{field: value, "updated_at": value})
    )
    if updated:
        setattr(order, field, value)
        return True
    return False
