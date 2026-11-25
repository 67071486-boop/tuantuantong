"""群二维码（`OrderScreenshot.img_type=1`）的覆盖语义与**字节保留判据**（D-0316）。群码自本批起与订单截图一样走**共享前缀**并在租户之间同步 ⇒ 它的字节不再只归本租户： - 覆盖（同一张单只留最新一张 `ready` 群码）= **硬删旧行**（模型只有 `pending` / `ready`）； - 但**共享前缀**下的旧字节**不删**——链上别的租户可能正拿它显示，删了就是断链；
- `img_type=0`（订单截图）**仍照旧删字节**（规格 `2026-09-25-upstream-downstream-order-sync- design.md` 的 R1：「任一家删则链上大家都没了」）。⛔ 别把这道闸写成「共享前缀一律不删」 ——那样每删一张订单截图都留永久孤儿字节。调用点（三处，全部走 `should_delete_object_bytes`）： 1. 覆盖淘汰：本模块 `evict_previous_ready_group_qr`（本地认领经
`api/screenshot_views._evict_previous_group_qr` 委托；同步接收侧在建行之前直接调）； 2. 删除端点：`api/screenshot_views.ScreenshotDeleteView.destroy`； 3. 运维命令：`management/commands/audit_order_group_qr.py` 的 `--delete-objects`。 ⚠️ **群码的删除事件仍不广播**（`screenshot_views` 那处 `if img_type == IMG_ORDER` 保留）：覆盖语义已足够，下家删一份不该让链上都没了。"""
from __future__ import annotations

import logging

from django.db import transaction

from apps.business.media_access import is_shared_object_key
from apps.business.models import OrderScreenshot
from apps.core.storage import get_storage

logger = logging.getLogger(__name__)


def should_delete_object_bytes(*, img_type, object_key) -> bool:
    """删行时要不要连**对象字节**一起删——本批（请求批次）的**唯一决策点**。 **只有**「`img_type == 1`（群二维码）**且** key 在共享前缀下」这一种组合返回 `False`
    （不删字节）；其余一律 `True`。⇒ `img_type=0` 的共享 key **仍然删字节**。 ⚠️ `img_type` 用 `==` 比较、`object_key` 用形状判据：⛔ 别写成真值判断（`0` 是合法取值，
    而 `False == 0`；接收侧形状闸另有 `isinstance(..., bool)` 那道闸，本函数只管领域决策）。 ⚠️ 判据单一出处：前缀那一半委托 `media_access.is_shared_object_key`（D-0316 起全仓一份）。"""
    return not (
        img_type == OrderScreenshot.IMG_GROUP_QR and is_shared_object_key(object_key)
    )


def evict_previous_ready_group_qr(order_id, *, keep_pk=None) -> None:
    """群二维码**每单最多一张**：删掉本单其它 `ready` 群码（覆盖语义），字节按上面的闸处理。 - `keep_pk`：要留下的那一行。本地认领传自己（`row.pk`）；同步接收侧在**建行之前**调用 ⇒ 传 `None`（此时本单的旧行都是"上一条"）。 - **只删 `ready`**：把「还没上传完」的 `pending` 行也删掉的话，那个对象之后才传上去就 **没有行**能被 24 小时清理任务找到 ⇒ 永久孤儿。`pending` 行留给超时清理任务。
    - **不跨单**（`order_id` 精确）、不碰 `img_type=0`。 - ⚠️ 并发：两条不同 key 的 `add` 同时到达时，后到者会撞部分唯一索引 `uniq_order_ready_group_qr`（接收侧按既有 `IntegrityError` 分支响亮失败、退避重投）。本函数**不加订单行锁**（接收侧今天没有；群码同步频率极低）——如实登记。 - ⚠️ **字节删除排到事务提交之后**（`transaction.on_commit`，与
    `audit_order_group_qr --delete-objects` 同一纪律）：接收侧淘汰发生在**批事务内** （`peer_views` 的 `transaction.atomic()`），升级前遗留的**本租户前缀**群码要删字节 —— 若同步删，同批后续回滚就会出现「行回来、字节不回」的坏图。代价（如实登记）：提交成功到回调执行之间进程若崩溃，字节留成孤儿（与 `purge_pending_screenshots` 同一族残差；存储层没有 list，做不了 GC）。"""
    rows = list(
        OrderScreenshot.objects.filter(
            order_id=order_id,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )
    )
    if keep_pk is not None:
        rows = [old for old in rows if old.pk != keep_pk]
    doomed_keys = [
        old.object_key for old in rows
        if should_delete_object_bytes(img_type=old.img_type, object_key=old.object_key)
    ]
    for old in rows:
        old.delete()
    if doomed_keys:
        transaction.on_commit(
            lambda keys=tuple(doomed_keys): _delete_committed_objects(keys)
        )


def _delete_committed_objects(keys: tuple[str, ...]) -> None:
    """提交后删字节（best-effort）：失败只留孤儿、⛔ 不让一次清理失败把已成功的响应打成 500。
    ⚠️ 与 `audit_order_group_qr._delete_objects` 同一口径（那里写给运维看 stderr；这里走
    `logger.warning`，因为这是请求链路）。"""
    storage = get_storage()
    for key in keys:
        try:
            storage.delete(key)
        except Exception as exc:  # noqa: BLE001 —— 对象删失败不该让调用方看到失败
            logger.warning(
                "群码淘汰删对象失败（留下孤儿，可按 key 人工清）：%s（%s）",
                key, type(exc).__name__,
            )
