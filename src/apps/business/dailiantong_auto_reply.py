"""代练通「申请验收后」自动回复发送引擎（T14）。 ⭐ **改准（一键抛单计划 §2.1 / B1）**：「接单后」(`trigger=0`) 已并入 `apps.business.dailiantong_qr.send_accept_qr`（一次接手最多一条留言 + 一张图、共用一个已发标记；图源 = 本单群码 > 规则图片）。本模块**只负责「申请验收后」(`trigger=10`)**—— 否则「同一张单被接手后收到两条留言 + 两张图」的重复发送问题不会消失。本模块回答的问题是：**代练通上的单到 `Status=13`（等待验收）之后，按本租户配置的「申请验收后」规则给打手发一段话术（可选带一张图），并且同一档只发一次**。规则表是 `PlatformAutoReplyRule`（T13）。协议层 / 客户端 / OSS 上传全部复用既有实现： - 客户端构造点：`dailiantong_push._open_client`（凭据只在那一个地方读）； - OSS 上传：`dailiantong_client.put_object`（**⛔ 不重新实现签名**）； - 读图判图种：`dailiantong_qr._load_picture`（与接手后发送同一条口径）； - 留言正文 + 系统标记：`dailiantong_qr.build_message_text`（两条线共用一处口径）。 ## 触发判据（只看**回读后的**远端状态） | `remote_status` | 含义（`docs/DAILIANTONG-API.md` §5） | 触发哪一档 | |---|---|---| | `13` | 等待验收（打手申请完单） | `trigger=10`「申请验收后」 |
| 其它（含 `12`） | — | 什么都不做（零上游调用；`12` 由 `dailiantong_qr` 负责） | ⚠️ `remote_status` 必须来自 `refresh_push_status()` 的**回读结果**，⛔ 不是回调报文里的 `Status`（`dailiantong_sync` 的模块 docstring 已写明：回调只当闹钟）。本模块不关心它是谁写的，只读推送行上已经落库的值。 ⚠️ **先判取消态、再判触发档**（评审裁决）：`remote_cancel_status` 不是空（还没回读到）也不是 `10`（未撤销）⇒ 一条都不发。打手已经在申请撤单时再发「欢迎接单」类话术只会添乱；与群二维码线同向（那条线跳过 11/12/14/15/16），本线按裁决更严： **不是空、不是 10 就不发**。 ## 规则优先级（同一档内） 1. **具体游戏的规则优先于「全部游戏」的规则**：`game == order.game` 的那条先选；只有没有具体游戏规则时才退到 `game is null`（全部游戏）。 2. 同一 `(platform, game, trigger)` **数据库层只允许一条**（起； `PlatformAutoReplyRule.Meta.constraints`）⇒ 「同优先级多条」在库层已经不可能。这里仍按模型排序（`sort_order` 升序、同值按 `id`）取第一条，只为在迁移前的存量数据上也确定。都没有匹配 ⇒ 什么都不做（不写审计、不打上游）。 ## 幂等（审计修复 P1-2） `DailiantongAutoReplySent` 的 **`(push, rule)` 唯一约束**仍是幂等键，但**占位行在发送之前**
就插入（`state=pending` + `claim_token`）： - 发送前先回读进度图（`/ar-<规则 id>-`）与留言（**全文相等 + 系统标记**）⇒ 已发过的步骤不重放； - 整体成功才把占位收成 `state=sent`；上游失败删除自己那次占位 ⇒ 下一轮可重试； - 进程在发送中途被杀 ⇒ 占位留在 `pending`，未超过 `AUTO_REPLY_CLAIM_STALE_SECONDS` 时另一个进程不重复发；过期后重新占用，并靠回读避免重复内容。 ## 发送顺序与「部分成功」语义（T14 明确要求写清） **先图片（若规则配了图）再文字**。这样： | 失败点 | 消费者（打手）看到什么 | 下一轮 | |---|---|---| | 读图 / `GetSTS` / OSS 上传 / 发进度 / 回读核验失败 | **什么都看不到**（文字还没发） | 重试整段，无重复 | | 图片已成功、**文字发送失败** | 订单进度里有一张图，**没有话术** | ⭐ 先回读进度：发现本规则的对象名已在列表里 ⇒ **不重复传图 / 不重复发进度**，只补发文字 | | 文字已成功、收尾前进程崩溃 | 完整的一条话术（可能还有图） | ⭐ 先回读留言：带系统标记的全文已在列表里 ⇒ **不重复发话术**，只把占位收成 `sent` | 图片对象名形如 `tt/<租户 schema>/<我方单号>/ar-<规则 id>-<毫秒>.<扩展名>`（毫秒逐次取值 ⇒ 回读自证不会被上一轮的旧图骗过；租户前缀防跨租户同名覆盖）。「图片此前已发」的判据 = 进度列表里出现 `/ar-<规则 id>-` 这个片段。 ## 自证（⛔ 不信 `Result=1`） `LevelOrderProgressAdd` 会假成功（`docs/DAILIANTONG-API.md` §9 R6）⇒ 图片的成败判据是 **发完之后再读一次 `LevelOrderProgressList`，列表里出现本次对象名**。文字没有独立的回读判据（上游没有「按内容查留言」的可靠口径），以 `LevelOrderMessageAdd` 拿到成功码为准。 ## 审计（⛔ 不刷屏、⛔ 不带凭据） - **成功**：每条 `(push, rule)` 恰好一条（幂等台账保证不会重复）； - **失败**：只有**失败原因变化**时才写一条。比较基准是推送行上的 `auto_reply_last_error`（持久副本；`last_error` 每轮会被状态回读清空，不能当比较基准）； - 审计只写单号 / 触发档 / 结果摘要；⛔ 不写规则正文、⛔ 不写 OSS 对象名、 ⛔ 不写 `GetSTS` 三件套 / AppSecret / 支付密码。 ## 失败分层（⛔ 不要 500）预期内的失败（没配置账号、没图、上游业务错误、传输超时、回读没出现）一律翻成 `failed` 并写可读原因；只有编程错误向上抛（由 `dailiantong_sync` 的单条异常边界接住，不影响整轮、也不影响同轮的群二维码）。"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import dailiantong_push as push_module
from apps.business import dailiantong_qr as qr_module
from apps.business.dailiantong_client import (
    DailiantongError,
    OssUploadError,
    put_object,
)
from apps.business.dailiantong_push import (
    DailiantongPushError,
    _as_int,
    _clip_error,
)
from apps.business.models import (
    DailiantongAutoReplySent,
    DailiantongPush,
    PlatformAutoReplyRule,
)

logger = logging.getLogger(__name__)

# ── 审计动作码（登记在 `apps/audit/actions.py`；⛔ 不带凭据 / 对象名 / 规则正文）────
AUDIT_ACTION = "order.dailiantong_auto_reply"
#: 审计的 `target_kind`：与抛单 / 平台动作 / 群二维码线同值（列宽 16，必须**恰好** ≤16）。
AUDIT_TARGET_KIND = "dailiantong_push"
#: 机器行（定时任务触发，没有认证态操作人）。
SYSTEM_ACTOR = "系统"

# ── 平台 / 远端码 / 时机码 ──────────────────────────────────────────────
#: 本版唯一能跑引擎的平台（代练通；与 `PlatformAutoReplyRule.platform` 默认值同源）。
PLATFORM = 10
#: 「正在代练」（打手已接手）——与接手后发送线同一个常量（`dailiantong_qr` 已定义 12）。
#: ⚠️ 本引擎**不再处理** `12`（那条由 `dailiantong_qr.send_accept_qr` 负责）。
REMOTE_STATUS_IN_PROGRESS = qr_module.REMOTE_STATUS_IN_PROGRESS
#: 「等待验收」（打手申请完单）。`docs/DAILIANTONG-API.md` §5 的码表。
REMOTE_STATUS_WAITING_REVIEW = 13

#: 时机码 → 文案。⚠️ 码值**逐字照证据件**（`PlatformAutoReplyRule.Trigger` 的 docstring
#: 记了出处）：`0` = 接单后 / `10` = 申请验收后 —— ⛔ 不是 10 / 20。
TRIGGER_AFTER_ACCEPT = PlatformAutoReplyRule.Trigger.AFTER_ACCEPT  # 0（不在本引擎）
TRIGGER_AFTER_REVIEW_REQUEST = PlatformAutoReplyRule.Trigger.AFTER_REVIEW_REQUEST  # 10
TRIGGER_LABELS = {
    int(TRIGGER_AFTER_ACCEPT): "接单后",
    int(TRIGGER_AFTER_REVIEW_REQUEST): "申请验收后",
}
#: 撤销态闸门：`CancelStatus` **为空**（还没回读到）或 **10**（未撤销）才发。
#: 评审裁决：打手已在申请撤单时再发「欢迎接单」类话术只会添乱； ⚠️ 与群二维码线的 `CANCEL_STATUSES_IN_PROGRESS`（11/12/14/15/16）**有意不同**：
#: 本线按裁决「不是空、也不是 10 ⇒ 一律不发」（`13` 撤销已取消也归入不发）。
CANCEL_STATUS_NOT_CANCELLED = 10
#: 远端状态 → 时机码（**这张表就是触发判据**；改它必须同一次改测试）。
#: ⚠️ 起只留 `13 → 申请验收后`：`12 → 接单后` 已并入 `dailiantong_qr`。
TRIGGER_BY_REMOTE_STATUS = {
    REMOTE_STATUS_WAITING_REVIEW: TRIGGER_AFTER_REVIEW_REQUEST,
}

# ── 图片 ────────────────────────────────────────────────────────────────
#: 进度文案（这张图是「按自动回复规则发的」，⛔ 不复述规则正文——正文在留言里）。
PROGRESS_CAPTION = "自动回复图片"
#: 对象名前缀 / 单号清洗与群二维码线**同一个出处**（⛔ 不另写一份正则）。
OBJECT_KEY_PREFIX = qr_module.OBJECT_KEY_PREFIX
_OBJECT_KEY_SAFE_RE = qr_module._OBJECT_KEY_SAFE_RE
#: 进度回读一次取多少条（与群二维码线同一个值；理由见 `dailiantong_qr.PROGRESS_READBACK_MAX`）。
PROGRESS_READBACK_MAX = qr_module.PROGRESS_READBACK_MAX

#: 发送占位的失效时长（秒）。进程在发送中途被杀 ⇒ 占位留在台账里，超过这个时长才允许
#: 下一轮重新占用；期间本规则不重复发（先回读进度图 / 留言，补发只补缺的步骤）。
AUTO_REPLY_CLAIM_STALE_SECONDS = 600
#: 发出去的完整正文上限（与手动留言同一个 512 字口径；规则正文列宽 200，正常不会撞）。
AUTO_REPLY_MESSAGE_MAX_LENGTH = 512

# ── 结果状态 ────────────────────────────────────────────────────────────
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_ALREADY_SENT = "already_sent"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class AutoReplyOutcome:
    """一次 `dispatch_for_push` 的结果（调用方按 `status` 分档，⛔ 不要只看真值）。"""

    status: str
    reason: str = ""
    rule_id: int | None = None
    trigger: int | None = None
    with_image: bool = False

    @property
    def sent(self) -> bool:
        return self.status == STATUS_SENT

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED


# ── 小工具 ──────────────────────────────────────────────────────────────


def build_object_key(
    order_no, rule_id: int, ext: str, *, epoch_ms: int | None = None
) -> str:
    """`tt/<租户 schema>/<我方单号>/ar-<规则 id>-<毫秒>.<扩展名>`。 ⚠️ 租户前缀是**硬要求**（审计修复 P3-13）：不同租户的同名单号不能互相
    覆盖 OSS 对象。schema 只从当前连接取（`dailiantong_qr._schema_name`）。毫秒逐次取时刻：同一规则在不同轮次得到不同对象名，这样「上一轮传了一半」的对象
    不会覆盖这一轮，回读自证也就不会自欺（与群二维码线的取舍相同）。"""
    safe_no = qr_module._safe_segment(order_no, fallback="order")
    safe_schema = qr_module._safe_segment(qr_module._schema_name(), fallback="public")
    stamp = int(epoch_ms) if epoch_ms is not None else int(time.time() * 1000)
    return (
        f"{OBJECT_KEY_PREFIX}/{safe_schema}/{safe_no}/"
        f"ar-{int(rule_id)}-{stamp}.{ext}"
    )


def build_message_text(content: str, *, order_no, rule_id: int) -> str:
    """拼出真正发出去的正文：规则正文 + **可识别的系统标记**。 ⭐ 只按「首句 / 前 40 字」认领会把打手的历史留言（例如配了「好的」「收到」这种短句时）误当成我们发过的 ⇒ 静默不回却记账成功。
    ⇒ 每条自动回复末尾都带 `（系统自动回复·订单 <我方单号>·规则 <规则 id>）`，回读时按**全文相等**认领。标记本身让「打手先发了同样的短句」不可能误撞。
    ⚠️ 2026-10-27（B1）起**唯一出处**在 `dailiantong_qr.build_message_text` （接手后发送线与本引擎共用一份标记口径）；这里只转发，⛔ 别再抄一份模板。"""
    return qr_module.build_message_text(content, order_no=order_no, rule_id=rule_id)


def _text_already_delivered(rows, message: str) -> bool:
    """留言回读里是否已经出现**与我们这次要发的全文完全相同**的留言。
    全文相等（去掉首尾空白）而不是包含 / 首句：短句规则（「好的」）不会因为打手的
    历史留言而被误判成「已发」。"""
    target = (message or "").strip()
    if not target:
        return False
    for row in rows:
        for value in row.values():
            if isinstance(value, str) and value.strip() == target:
                return True
    return False


def _image_marker(rule_id: int) -> str:
    """判「这一规则的图是否已经出现在进度列表里」的片段。对象名本身是 `tt/<租户 schema>/<单号>/ar-<规则 id>-<毫秒>.<ext>` ⇒ 用
    `/ar-<id>-` 作片段：它带前导 `/` 与结尾 `-`，不会与 `ar-11-` / 别的单号误撞。"""
    return f"/ar-{int(rule_id)}-"


def _trigger_label(trigger: int | None) -> str:
    return TRIGGER_LABELS.get(int(trigger), "自动回复") if trigger is not None else "自动回复"


def _select_rule(order, trigger: int) -> PlatformAutoReplyRule | None:
    """按「具体游戏优先 → 全部游戏」选一条启用中的规则；没有就返回 `None`。
    ⚠️ 同一优先级多条时按模型排序取第一条（见模块 docstring 的「规则优先级」）。"""
    base = PlatformAutoReplyRule.objects.filter(
        platform=PLATFORM, trigger=trigger, is_enabled=True
    )
    specific = None
    if order.game_id is not None:
        specific = (
            base.filter(game_id=order.game_id)
            .order_by("sort_order", "id")
            .first()
        )
    if specific is not None:
        return specific
    return base.filter(game__isnull=True).order_by("sort_order", "id").first()


def _record(push: DailiantongPush, *, reason: str) -> None:
    """写一条审计（机器行；`target` 指向推送行；⛔ 不含凭据 / 对象名 / 规则正文）。"""
    order = push.order
    record(
        log_type=ACTION_LOG_TYPES[AUDIT_ACTION],
        action=AUDIT_ACTION,
        actor=SYSTEM_ACTOR,
        system=True,
        target_kind=AUDIT_TARGET_KIND,
        target_id=str(push.pk),
        target_name=order.order_no,
        order_no=order.order_no,
        game_name=(order.game.name or "") if order.game_id else "",
        reason=reason,
    )


def _save_failure(push: DailiantongPush, reason: str) -> str:
    """落失败原因：`last_error`（页面现有展示位）+ `auto_reply_last_error`（持久副本）。"""
    reason = _clip_error(reason)
    push.last_error = reason
    push.auto_reply_last_error = reason
    push.save(
        update_fields=["last_error", "auto_reply_last_error", "updated_at"]
    )
    return reason


def _fail(
    push: DailiantongPush,
    reason: str,
    *,
    trigger: int | None,
    audit: bool = True,
    rule: PlatformAutoReplyRule | None = None,
    token: str = "",
) -> AutoReplyOutcome:
    """失败出口：**先删掉自己那次占位**（下一轮可重试）、写原因、按需写审计。
    ⚠️ 同一个失败原因**只写一条审计**：定时任务每轮重试一次，不这么收口就会变成
    每天几千条同文案的审计。比较基准是上一轮落库的完整文案（含触发档前缀）。"""
    if token and rule is not None:
        DailiantongAutoReplySent.objects.filter(
            push=push,
            rule=rule,
            claim_token=token,
            state=DailiantongAutoReplySent.State.PENDING,
        ).delete()
    message = f"{_trigger_label(trigger)}自动回复失败：{reason}"
    previous = (push.auto_reply_last_error or "").strip()
    message = _save_failure(push, message)
    if audit and message != previous:
        _record(push, reason=message)
    logger.warning(
        "代练通自动回复未发送 push=%s order=%s：%s",
        push.pk,
        push.order.order_no,
        message,
    )
    return AutoReplyOutcome(STATUS_FAILED, message, trigger=trigger)


def _skip(reason: str, *, trigger: int | None = None, target=None) -> AutoReplyOutcome:
    """跳过出口：**零上游调用、零审计**。 ⭐ 审计修复（P3-16）：跳过原因不再只写 debug —— 落进
    `auto_reply_last_error`（读面 `GET /api/business/platform-pushes/` 会下发），
    页面才看得到「为什么没回」。只在原因变化时写库，避免每轮写一次同文案。"""
    if target is not None:
        message = _clip_error(f"跳过：{reason}")
        if (target.auto_reply_last_error or "") != message:
            target.auto_reply_last_error = message
            target.save(update_fields=["auto_reply_last_error", "updated_at"])
    logger.debug(
        "代练通自动回复跳过 push=%s：%s",
        getattr(target, "pk", "-"),
        reason,
    )
    return AutoReplyOutcome(STATUS_SKIPPED, reason, trigger=trigger)


# ── 幂等占位（库层，发送之前）───────────────────────────────────────────────


def _claim_send(push, rule) -> tuple[str, str]:
    """在本规则的台账行上占位（发送之前）。返回 `(状态, 令牌)`：`状态` 取 `"claimed"` / `"already"` / `"in_progress"`。
    - `already`：这一 (推送, 规则) 已经发成功过； - `in_progress`：另一个进程正拿着**未过期**的占位；
    - 占位过期（进程被杀）⇒ 用条件 UPDATE 抢回来（令牌不同则抢不到）。"""
    token = uuid.uuid4().hex
    now = timezone.now()
    with transaction.atomic():
        try:
            row, created = DailiantongAutoReplySent.objects.get_or_create(
                push=push,
                rule=rule,
                defaults={
                    "state": DailiantongAutoReplySent.State.PENDING,
                    "claim_token": token,
                    "sent_at": now,
                },
            )
        except IntegrityError:
            # 并发窗口：另一进程刚插入同一 (push, rule) ⇒ 重新读一次按状态分档。
            row = DailiantongAutoReplySent.objects.get(push=push, rule=rule)
            created = False
    if created:
        return "claimed", token

    if row.state == DailiantongAutoReplySent.State.SENT:
        return "already", ""

    stale_before = now - timedelta(seconds=AUTO_REPLY_CLAIM_STALE_SECONDS)
    if row.sent_at is not None and row.sent_at >= stale_before:
        return "in_progress", ""

    updated = DailiantongAutoReplySent.objects.filter(
        pk=row.pk,
        state=DailiantongAutoReplySent.State.PENDING,
        sent_at=row.sent_at,
        claim_token=row.claim_token,
    ).update(claim_token=token, sent_at=now)
    if not updated:
        return "in_progress", ""
    return "claimed", token


def _mark_sent(push, rule, token: str) -> bool:
    """把占位收成 `sent`（条件 UPDATE：只有自己那次占位能收）。"""
    updated = DailiantongAutoReplySent.objects.filter(
        push=push,
        rule=rule,
        claim_token=token,
        state=DailiantongAutoReplySent.State.PENDING,
    ).update(
        state=DailiantongAutoReplySent.State.SENT,
        claim_token="",
        sent_at=timezone.now(),
    )
    return bool(updated)


# ── 客户端（测试替换点；⛔ 不复制凭据读取）────────────────────────────────


def _open_client(integration):
    """**测试替换点**：委托抛单模块的同一个客户端构造点。"""
    return push_module._open_client(integration)


def _close(client) -> None:
    push_module._close(client)


def _progress_contains(rows, object_key: str) -> bool:
    """进度回读里是否出现这次上传的对象名（宽容形态：裸对象名或拼成 URL 都算）。"""
    return qr_module._progress_contains(rows, object_key)


def _image_already_delivered(client, serial_no: str, rule_id: int) -> bool:
    """本规则的图是否**已经在订单进度里**（上一轮「图成功、文字失败」的补发判据）。
    读失败时抛 `DailiantongError`（由调用方翻成可读失败）：⛔ 不能在「不知道」的情况下
    又传一张图——那正是要避免的重复。"""
    page = client.progress_list(serial_no, max_count=PROGRESS_READBACK_MAX)
    marker = _image_marker(rule_id)
    for row in page.rows:
        for value in row.values():
            if isinstance(value, str) and marker in value:
                return True
    return False


# ── 对外：按推送行触发自动回复 ───────────────────────────────────────────


def dispatch_for_push(push: DailiantongPush) -> AutoReplyOutcome:
    """按**推送行上已回读的远端状态**给打手发一条自动回复（幂等，可每轮调）。触发判据 / 优先级 / 发送顺序 / 部分失败语义见模块 docstring。前置（不满足都**零上游调用**）： 1. 远端**不在撤销流程中**：`remote_cancel_status` 为空（还没回读到）或 `10`（未撤销）； ⚠️ 先判取消态、再判触发档（评审裁决）；
    2. 远端状态是 `13`（申请验收后）；⚠️ `12`（接单后）由 `dailiantong_qr` 负责； 3. 推送行还在轮询窗口内（`is_active=True` 且 `state` 是有效推送）； 4. 平台是代练通； 5. 命中一条启用中的规则（具体游戏优先 → 全部游戏）； 6. 这条推送**没对这条规则发过**（`DailiantongAutoReplySent` 没有对应行）；
    7. 有上游单号。通过后：按需读图 → `GetSTS` → OSS 上传 → 发进度 → **回读自证** → 发文字。整体成功才写台账；任何一步失败写 `last_error` / `auto_reply_last_error`， **不写台账**（下一轮重试，每轮每规则至多一次）。返回 `AutoReplyOutcome`；本函数**不抛**业务失败（配置 / 存储 / 上游 / 上传的预期失败都翻成 `failed`），只有编程错误会向上抛。"""
    # ⚠️ 先判取消态、再判触发档（避免「撤销中仍收到接单话术」）。
    cancel_status = _as_int(push.remote_cancel_status)
    if cancel_status is not None and cancel_status != CANCEL_STATUS_NOT_CANCELLED:
        return _skip(
            f"订单撤销流程中（CancelStatus={cancel_status}），不自动回复",
            target=push,
        )

    status = _as_int(push.remote_status)
    trigger = TRIGGER_BY_REMOTE_STATUS.get(status)
    if trigger is None:
        return _skip(
            f"远端状态 {status} 不在本引擎的范围内（只处理「申请验收后」）",
            target=push,
        )

    if not push.is_active or push.state not in DailiantongPush.ACTIVE_STATES:
        return _skip("这条推送已经不在轮询窗口内", trigger=trigger, target=push)
    if push.platform != PLATFORM:
        return _skip(f"平台 {push.platform} 没有自动回复引擎", trigger=trigger, target=push)

    order = push.order
    rule = _select_rule(order, trigger)
    if rule is None:
        return _skip(
            f"{_trigger_label(trigger)}没有可用的自动回复规则",
            trigger=trigger,
            target=push,
        )

    if DailiantongAutoReplySent.objects.filter(
        push=push, rule=rule, state=DailiantongAutoReplySent.State.SENT
    ).exists():
        return AutoReplyOutcome(
            STATUS_ALREADY_SENT,
            "这条推送已经按这条规则回复过",
            rule_id=rule.pk,
            trigger=trigger,
        )

    serial_no = (push.serial_no or "").strip()
    if not serial_no:
        return _fail(
            push, "这条推送还没有上游订单号，不能自动回复", trigger=trigger, rule=rule
        )

    content = (rule.content or "").strip()
    if not content:
        # T13 的接口要求正文非空；这里防库里的脏行（⛔ 不向上游发一条空留言）。
        return _fail(push, "自动回复规则的正文字段是空的", trigger=trigger, rule=rule)

    # ⭐ I-A：真正发出去的正文带系统标记；回读按全文相等认领（⛔ 不靠首句猜）。
    outgoing = build_message_text(
        content, order_no=push.order.order_no, rule_id=rule.pk
    )
    if len(outgoing) > AUTO_REPLY_MESSAGE_MAX_LENGTH:
        return _fail(
            push,
            "自动回复正文加系统标记后超过 "
            f"{AUTO_REPLY_MESSAGE_MAX_LENGTH} 字，未发送，请缩短规则正文",
            trigger=trigger,
            rule=rule,
        )

    image_key = (rule.image_key or "").strip()

    # 读图在打上游之前做：存储里没有这张图时不浪费一次 GetSTS（且失败无任何可见副作用）。
    image_bytes = b""
    image_content_type = ""
    image_ext = ""
    if image_key:
        try:
            image_bytes, image_content_type, image_ext = qr_module._load_picture(
                image_key, "", label="自动回复图片"
            )
        except qr_module._MediaReadError as exc:
            return _fail(
                push, f"读取自动回复图片失败：{exc}", trigger=trigger, rule=rule
            )
        except Exception as exc:  # noqa: BLE001 —— 存储后端的异常族由后端决定
            # ⛔ 只记类型名：异常文本可能带对象名 / 存储路径。
            return _fail(
                push,
                f"读取自动回复图片失败（{type(exc).__name__}）",
                trigger=trigger,
                rule=rule,
            )

    try:
        integration = push_module._active_integration(push.platform)
    except DailiantongPushError as exc:
        return _fail(
            push, f"代练通账号配置不可用：{exc}", trigger=trigger, rule=rule
        )

    token = ""
    image_reused = False
    image_sent_now = False
    message_done = False
    object_key = ""

    client = None
    try:
        client = _open_client(integration)

        # ① 动上游之前先回读：图 / 话术哪一步已经发过了？
        #    （上一轮「图成功、文字失败」或「文字成功、写台账前进程被杀」都靠这一步认领）
        try:
            image_reused = (
                _image_already_delivered(client, serial_no, rule.pk)
                if image_key
                else False
            )
            message_page = client.message_list(
                serial_no, max_count=PROGRESS_READBACK_MAX
            )
        except DailiantongError as exc:
            return _fail(
                push,
                f"回读已发留言 / 进度失败（{exc.message}），不能判断是否已经发过",
                trigger=trigger,
                rule=rule,
            )
        message_done = _text_already_delivered(message_page.rows, outgoing)

        # ② 发送前在台账上占位（库层唯一约束 + 令牌条件 UPDATE）。
        claim_status, token = _claim_send(push, rule)
        if claim_status == "already":
            return AutoReplyOutcome(
                STATUS_ALREADY_SENT,
                "这条推送已经按这条规则回复过",
                rule_id=rule.pk,
                trigger=trigger,
            )
        if claim_status == "in_progress":
            return _skip(
                "另一个进程正在发送这条自动回复，本进程不重复发",
                trigger=trigger,
                target=push,
            )

        # ③ 只补**还没发过**的步骤：先图（若配置了）后文字。
        if image_key and not image_reused:
            try:
                sts = client.get_sts()
            except DailiantongError as exc:
                return _fail(
                    push,
                    f"取 OSS 临时凭证失败：{exc.message}",
                    trigger=trigger,
                    rule=rule,
                    token=token,
                )

            object_key = build_object_key(order.order_no, rule.pk, image_ext)
            try:
                put_object(sts, object_key, image_bytes, image_content_type)
            except OssUploadError as exc:
                return _fail(
                    push,
                    f"上传自动回复图片失败：{exc.message}",
                    trigger=trigger,
                    rule=rule,
                    token=token,
                )

            try:
                client.progress_add(serial_no, PROGRESS_CAPTION, img=object_key)
            except DailiantongError as exc:
                return _fail(
                    push,
                    f"发自动回复图片失败：{exc.message}",
                    trigger=trigger,
                    rule=rule,
                    token=token,
                )

            # ⛔ 自证：`LevelOrderProgressAdd` 会假成功 ⇒ 必须回读列表看到这个对象名。
            try:
                page = client.progress_list(
                    serial_no, max_count=PROGRESS_READBACK_MAX
                )
            except DailiantongError as exc:
                return _fail(
                    push,
                    f"已发图片，但回读进度列表失败（{exc.message}），没能核验",
                    trigger=trigger,
                    rule=rule,
                    token=token,
                )
            if not _progress_contains(page.rows, object_key):
                return _fail(
                    push,
                    "已发图片，但回读进度列表里没看到这张自动回复图片，没能核验",
                    trigger=trigger,
                    rule=rule,
                    token=token,
                )
            image_sent_now = True

        if not message_done:
            try:
                client.message_add(serial_no, outgoing)
            except DailiantongError as exc:
                return _fail(
                    push,
                    f"发自动回复话术失败：{exc.message}",
                    trigger=trigger,
                    rule=rule,
                    token=token,
                )
    finally:
        if client is not None:
            _close(client)

    # ── 整体成功：把发送前的占位收成 `sent`（条件 UPDATE：只收自己那次）────────
    if not _mark_sent(push, rule, token):
        # 收尾条件没命中（另一个进程先收尾了）：⛔ 不把重复行为播报成我们的成功。
        return AutoReplyOutcome(
            STATUS_ALREADY_SENT,
            "这条推送已经按这条规则回复过",
            rule_id=rule.pk,
            trigger=trigger,
        )

    previous = (push.auto_reply_last_error or "").strip()
    cleared = ["auto_reply_last_error", "updated_at"]
    push.auto_reply_last_error = ""
    # 只在 `last_error` 装的是**本模块**的失败文案时才清（别把群二维码 / 回读的错误擦掉）。
    if push.last_error and push.last_error == previous:
        push.last_error = ""
        cleared.append("last_error")
    push.save(update_fields=cleared)

    parts = [f"{_trigger_label(trigger)}自动回复已发送"]
    if image_key and image_sent_now:
        parts.append("含图片")
        if message_done:
            parts.append("话术此前已发，本次只补图")
    elif image_key and image_reused and not message_done:
        parts.append("图片此前已发，本次补发话术")
    elif message_done and not image_sent_now:
        parts.append("回读确认已发齐，补记账")
    reason = "（" + "，".join(parts[1:]) + "）" if len(parts) > 1 else ""
    reason = parts[0] + reason
    _record(push, reason=reason)
    logger.info(
        "代练通自动回复已发送 push=%s order=%s rule=%s trigger=%s image=%s",
        push.pk,
        order.order_no,
        rule.pk,
        trigger,
        bool(image_key),
    )
    return AutoReplyOutcome(
        STATUS_SENT,
        reason,
        rule_id=rule.pk,
        trigger=trigger,
        with_image=bool(image_key),
    )
