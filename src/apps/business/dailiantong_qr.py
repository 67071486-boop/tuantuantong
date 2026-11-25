"""接手后自动发送（群二维码 / 规则图片 / 规则话术）到代练通（T12 + 一键抛单）。本模块回答的问题是：**代练通上的单被接手（`Status=12` 正在代练）之后，怎么把我方「一张图 + 一条话术」送过去，以及送没送到怎么自证**。 ⭐ **改准（一键抛单计划 §2.1 / B1）——「接手后发送」只有一个入口**：原来有两条各自发送的链路（本模块的群码 + `dailiantong_auto_reply` 的「接单后」规则），同一张单可能收到 **2 条留言 + 2 张图**。现在合并成**一次接手 = 最多一条留言 + 最多一张图**，且两者共用一个已发标记（`DailiantongPush.qr_sent_at`）： | 内容 | 来源 | |---|---| | 图（最多一张） | ① 本单 `OrderScreenshot(img_type=IMG_GROUP_QR, status=ready)` → ② 该游戏「接单后」启用规则的 `image_key` → ③ **不发图** | | 文字（最多一条） | 该游戏「接单后」启用规则的 `content`（具体游戏优先于「全部游戏」）；**没有规则就不发文字**（⛔ 无默认话术） | ⛔ **不再使用 `HomeQrCode`**（首页 QQ 群码是给客户加群的）——兜底分支与「兜底群码」文案一并删除。两张图都没有、也没有规则话术 ⇒ 返回 `skipped_no_content` 并写可读原因， **不置** `qr_sent_at`（之后补上图/规则时下一轮还能发）。 ⚠️ 开关口径（⭐ 业务方口径）：`PlatformPushSettings.auto_send_group_qr_after_accept` （原 9 组第 6 条）**只管「图」**——它名字就是「平台接手后自动**发群二维码**」： - **文字**：只看「有没有匹配的启用规则」（与开关无关），有规则就发； - **图片**：看开关（开 = 按优先级发图；关 = 不发图）；
- 两者仍合并成**一次发送**（一条留言 + 至多一张图），只是判据不同； - 「申请验收后」(`Status=13` / `trigger=10`) 仍由 `dailiantong_auto_reply` 独立负责，不受这个开关影响。 ⇒ 开关关 + 有规则 = **只发文字**；开关关 + 无规则（且有本单码）= **什么都不发** （图被开关挡住、又没有话术）。⚠️ 一次接手只发一次：开关关时只发了文字的，之后再把开关打开**不会补发**那张图（`qr_sent_at` 已置）。 ## 三步（厂商文档 `docs/references/dailiantong/openapi-v3.4.md`「上传截图流程」） | 步 | 调用 | 关键点 | |---|---|---| | ① | `GetSTS` | 每次上传前现取阿里云 OSS 临时三件套（有失效时间、⛔ 不缓存、⛔ 不落库） | | ② | `put_object`（阿里云 OSS V1 签名） | 对象名形如 `tt/<租户 schema>/<我方单号>/qr-<毫秒时间戳>.<扩展名>`；上传成功后得到**对象名**（不带 http） | | ③ | `LevelOrderProgressAdd(Img=<对象名>)` + `LevelOrderMessageAdd` | 先发图（挂进订单进度），再发话术 | ## 自证（⛔ 不信 `Result=1`） `LevelOrderProgressAdd` 对**不存在的单号**也回成功（`docs/DAILIANTONG-API.md` §9 R6）⇒ 本模块的成败判据 = **图：`LevelOrderProgressList` 回读里出现本次对象名**； **文字：`LevelOrderMessageList` 回读里出现本模块的系统标记**（`（系统自动回复·订单 …·规则 …）`，标记里同时含我方单号与规则 id，不会与打手的历史留言误撞）。缺一条都算失败：写
`last_error` / `qr_last_error`，**不置 `qr_sent_at`**，下一轮再试（每轮至多一次）。 ⚠️ 回读**显式多取**（`PROGRESS_READBACK_MAX` / `MESSAGE_READBACK_MAX`）并**全量匹配**：上游默认首屏若不含最新那批，核验会永远失败 ⇒ 每轮都重发，比漏发更糟。 ## 幂等（单标记：`qr_sent_at`）「留言 + 图」是**一条逻辑**，共用一个终态标记 `DailiantongPush.qr_sent_at`：非空 = 这条推送的接手后发送已经完成，⛔ 不再发。真正保证不重复的是三层： 1. **发送前回读**：先读留言列表（系统标记）与进度列表（`tt/<schema>/<单号>/qr-` 前缀） ⇒ **已发过的步骤不再重放**；两步都在就只补记 `qr_sent_at`（零写调用）。 2. **库层占位**：还要发时用条件 UPDATE 抢 `qr_send_started_at` + `qr_claim_token`；抢不到 = 另一个进程正在发，本进程退出。占位超过 `QR_CLAIM_STALE_SECONDS` 才可重新占用。 3. **有界重试**：失败释放自己那次占位、`qr_attempts + 1`；连续 `MAX_CONSECUTIVE_FAILURES` 次后停止自动重发并写可读原因。换图 / 换规则 / **开关状态变化**（来源键变化）都会把失败次数清零，恢复重试。 ⚠️ `last_error` 会被 `refresh_push_status()` 在每次回读成功时清空（每轮），所以失败原因另存一份 **`qr_last_error`**：它不被远端状态回读覆盖，既由 `GET /api/business/platform-pushes/` 的读面下发（页面 tooltip / 排查用），也用于「同一个失败原因只写一条审计」（⛔ 不让每轮刷一条审计）。 ## 失败分层（⛔ 不要 500） | 场景 | 表现 | |---|---| | 已发过 / 撤销流程中 / 另进程占位中 | 返回 `skipped`，**零上游写调用** | | 开关关 + **没有规则话术**（图被开关挡住） | `skipped_no_content` + 「开关关且无话术」可读原因，零上游调用 | | 没有任何可发内容（无图、无规则话术） | 返回 `skipped_no_content`，`last_error` 写清原因，零上游调用 | | 连续失败到上限 | 返回 `skipped_retry_limit`，零上游调用 | | 读存储失败 / `GetSTS` 失败 / OSS 上传失败 / 进度失败 / 留言失败 / 回读没出现 | 返回 `failed`，`last_error` + `qr_last_error` 记原因（⛔ 不含凭据、⛔ 不含对象名），不置 `qr_sent_at` | ## 凭据红线（`AGENTS.md` §4） `GetSTS` 回来的 `SecurityToken` / `AccessKeyId` / `AccessKeySecret` **只活在这次调用的局部变量里**：⛔ 不进日志、⛔ 不进审计、⛔ 不进 `last_error` / `qr_last_error`、⛔ 不进异常文案。审计只写单号与**结果摘要**（规则正文也不进审计）；对象名属于内部标识，同样不写进人话（同鸡蛋铺传图线的隐私口径）。"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import connection as db_connection
from django.db.models import Q
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import dailiantong_push as push_module
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
    DailiantongPush,
    OrderScreenshot,
    PlatformAutoReplyRule,
    PlatformPushSettings,
)
from apps.core.storage import IMAGE_EXT_BY_CONTENT_TYPE, IMAGE_MAGIC, get_storage

logger = logging.getLogger(__name__)

# ── 审计动作码（登记在 `apps/audit/actions.py`；⛔ 不带凭据 / 对象名）────────────
AUDIT_ACTION = "order.dailiantong_qr"
#: 审计的 `target_kind`：与抛单 / 平台动作线同值（列宽 16，必须**恰好** ≤16）。
AUDIT_TARGET_KIND = "dailiantong_push"
#: 机器行（定时任务触发，没有认证态操作人）。
SYSTEM_ACTOR = "系统"

# ── 远端码（`docs/DAILIANTONG-API.md` §5）────────────────────────────────
#: 「正在代练」= 打手已接手（本功能的触发条件）。
REMOTE_STATUS_IN_PROGRESS = 12
#: 「等待验收」（打手申请完单）。同码表 §5：`12→13` 可能落在两次轮询之间 ⇒ 到了 13 而
#: 还没发过接手后内容，也要**补发**（否则那张单永远不发；评审 P3-11 的既有口径）。
REMOTE_STATUS_WAITING_REVIEW = 13
#: 接手后发送的候选远端状态（⭐ 与 `dailiantong_auto_reply` 的「申请验收后」共用 13，
#: 但两条线做的事不同：这里发「接手后内容」，那边发「申请验收后」规则）。
ACCEPT_STAGE_REMOTE_STATUSES = (
    REMOTE_STATUS_IN_PROGRESS,
    REMOTE_STATUS_WAITING_REVIEW,
)
#: 撤销流程**已经动起来**的 `CancelStatus`（11 协商中 / 12 已处理 / 14 客服介入中 /
#: 15 客服已处理 / 16 客服强制撤销）。这些状态下不再发群码：
#: 发给一个正在撤销的订单只会打扰打手。`10`（未撤销）/ `13`（撤销已取消）照常发。
CANCEL_STATUSES_IN_PROGRESS = frozenset({11, 12, 14, 15, 16})

#: 代练通平台 id（与 `PlatformIntegration.Platform.DAILIANTONG` / `dailiantong_auto_reply` 同值）。
PLATFORM = 10
#: 「接单后」时机码：码值与文案的唯一出处是 `PlatformAutoReplyRule.Trigger`
#: （证据件 `system-platformautoreply.html:98-101` 的 `value=0`）。
TRIGGER_AFTER_ACCEPT = int(PlatformAutoReplyRule.Trigger.AFTER_ACCEPT)

# ── 话术 / 进度文案（⛔ 先常量化；业务方要改口径只改这里）────────────────────
# ⭐（B1）：留言正文**只来自「接单后」规则**（`PlatformAutoReplyRule.content`），没有默认话术；这里不再有旧的两套模板（本单专属 / 兜底）。真正发出去的正文会带一个**可识别的系统标记**（见 `build_message_text`）：上游没有客户端
# 消息 ID，发送前回读只能靠内容认领。标记里含我方单号与规则 id，不会与打手的历史留言误撞（这与 `dailiantong_auto_reply` 的标记**逐字同一个出处**——两条线共用 `build_message_text`）。
MESSAGE_MARKER_TEMPLATE = "（系统自动回复·订单 {order_no}·规则 {rule_id}）"
#: 进度图的人话标题（图可能是本单群码，也可能是规则图片 ⇒ 标题保持中性）。
PROGRESS_CAPTION = "接单后自动发送的图片"

#: 「既没有图、也没有规则话术」时的可读原因（原样进 `last_error` / `qr_last_error`）。
NO_CONTENT_REASON = (
    "无可发送内容（本单没有可用群二维码，该游戏「接单后」规则也没有话术/图片）"
)
#: 开关关 + 没有规则话术时的可读原因：图被开关挡住、又没有文字 ⇒ 本次什么都不发。
SWITCH_OFF_AND_NO_TEXT_REASON = (
    "未开启「平台接手后自动发群二维码」，且该游戏「接单后」规则没有话术，本次不发"
)

# ── 对象名 ──────────────────────────────────────────────────────────────

#: OSS 对象名前缀（业务口径 `tt/<租户 schema>/<我方单号>/qr-<时间戳>.<扩展名>`）。
OBJECT_KEY_PREFIX = "tt"
#: 群二维码对象名的固定标签：回读时用 `/qr-` 这个片段认「这条单的群码图」。
OBJECT_KEY_TAG = "qr"
#: 单号里只保留安全字符（我们自己的单号是纯数字；这条防的是脏数据把 `/` 带进对象名）。
_OBJECT_KEY_SAFE_RE = re.compile(r"[^0-9A-Za-z_-]")

#: 图种 → 扩展名（只认存储/认领侧的既有白名单：jpg|jpeg|png）。
#: ⚠️ **同一份事实只留一处**（B1）：映射表住在 `apps.core.storage`
#: （`IMAGE_EXT_BY_CONTENT_TYPE`），这里保留本模块的旧名字给既有调用点 / 测试用。
EXT_BY_CONTENT_TYPE = IMAGE_EXT_BY_CONTENT_TYPE

# ── 幂等 / 有界重试（审计修复 P1-1 / P1-2）─────────────────────

#: 连续失败达到这个次数后**停止自动重发**（可读原因写进 `qr_last_error`）。
#: 半分钟一轮 × 5 ≈ 2.5 分钟；达到上限后不再每轮白打上游 + 给打手刷重复消息。
MAX_CONSECUTIVE_FAILURES = 5

#: 发送占位的失效时长（秒）。进程被杀后占位留在库里，超过这个时长才允许下一轮重新占用；
#: 期间**先回读**已有留言 / 进度图，已发出去的步骤不会重放。
QR_CLAIM_STALE_SECONDS = 600

#: 进度 / 留言回读一次取多少条（同 `PROGRESS_READBACK_MAX`；留言列表用同一个上限）。
MESSAGE_READBACK_MAX = 100

# ── 结果状态 ────────────────────────────────────────────────────────────
STATUS_SENT = "sent"
STATUS_ALREADY_SENT = "already_sent"
#: 没有任何可发内容（无图 + 无规则话术）：不置已发标记，配置好了下一轮还能发。
STATUS_SKIPPED_NO_CONTENT = "skipped_no_content"
STATUS_SKIPPED_CANCELLING = "skipped_cancelling"
#: 连续失败达到上限，已停止自动重发（等人工处理；换图后自动重置）。
STATUS_SKIPPED_RETRY_LIMIT = "skipped_retry_limit"
#: 另一个进程正拿着发送占位（库层互斥），本进程不重复发。
STATUS_SKIPPED_IN_PROGRESS = "skipped_in_progress"
#: ⭐ dl-F：这一行的平台没有「接手后发图 + 话术」能力（未接入平台）。
STATUS_SKIPPED_UNSUPPORTED_PLATFORM = "skipped_unsupported_platform"
STATUS_FAILED = "failed"

#: 图片来源（写进读面 / 测试断言；⛔ 不进人话审计以外的任何地方）。
SOURCE_ORDER_QR = "order"
SOURCE_RULE_IMAGE = "rule"

#: 进度回读一次取多少条（`LevelOrderProgressList` 的 `Max`）。 ⚠️ 为什么要显式多取：本功能**唯一**的成败判据是「回读里出现本次对象名」。
#: 不传 `Max` 就只能看到上游默认首屏；若默认首屏不是最新那批（或历史进度很多），已上传成功的图永远核验不到 ⇒ 每轮重发留言 + 图，比漏发更糟。 100 是上游文档口径内的大值（文档没写上限；同族 `Max` 的实测上限是 500，
#: 但这里只需要覆盖「最近一批」，取 100 兼顾请求大小）。⚠️ 仍然**没有分页**接口：单条订单进度超过 100 条时理论上仍可能漏，登记在报告「担心点」里。
PROGRESS_READBACK_MAX = 100


class _MediaReadError(ValueError):
    """读到的图**内容**不可用（空对象 / 认不出图种）。消息是本模块自己写的安全文案。"""


@dataclass(frozen=True)
class QrSendOutcome:
    """一次 `send_accept_qr` 的结果（调用方按 `status` 分档，⛔ 不要只看真值）。"""

    status: str
    reason: str = ""
    object_key: str = ""
    source: str = ""

    @property
    def sent(self) -> bool:
        return self.status == STATUS_SENT

    @property
    def failed(self) -> bool:
        return self.status == STATUS_FAILED

    @property
    def skipped(self) -> bool:
        return self.status in (
            STATUS_ALREADY_SENT,
            STATUS_SKIPPED_NO_CONTENT,
            STATUS_SKIPPED_CANCELLING,
            STATUS_SKIPPED_RETRY_LIMIT,
            STATUS_SKIPPED_IN_PROGRESS,
        )


# ── 小工具 ──────────────────────────────────────────────────────────────


def image_send_enabled() -> bool:
    """全局设置「平台接手后自动发群二维码」（`SK158`，默认 0 = 不发图）。 ⚠️ 业务方口径：这个开关**只管图**，⛔ 不管规则话术
    （话术只看有没有匹配的启用规则）。 ⛔ 单行配置的唯一入口是 `PlatformPushSettings.get_solo()`；这里不缓存
    （每轮一次，用户关开关后下一轮就该生效）。"""
    return (
        PlatformPushSettings.get_solo().auto_send_group_qr_after_accept
        == PlatformPushSettings.GroupQrSend.AUTO
    )


def _schema_name() -> str:
    """当前租户 schema 名（对象名的第一段；⛔ 不接受调用方传入租户标识）。"""
    name = (getattr(db_connection, "schema_name", "") or "").strip()
    return name or "public"


def _safe_segment(value: str, *, fallback: str) -> str:
    return _OBJECT_KEY_SAFE_RE.sub("", str(value or "")) or fallback


def build_object_key(
    order_no,
    ext: str,
    *,
    epoch_ms: int | None = None,
    schema: str | None = None,
    tag: str | None = None,
) -> str:
    """`tt/<租户 schema>/<我方单号>/<tag>-<毫秒时间戳>.<扩展名>`。 ⚠️ 租户前缀是**硬要求**（审计修复 P3-13）：不同租户的同名单号不能互相覆盖 OSS 对象。schema 只从当前连接取（`_schema_name`），⛔ 不接受请求参数。
    ⚠️ 时间戳取**毫秒**且逐次取时刻：同一张单在不同轮次会得到不同对象名，这样「上一轮传了一半」的对象不会被这一轮覆盖（回读判据也就不会自欺）。 ⭐：`tag` 可覆盖文件名前缀（默认 `qr`）——详情页「追加截图」用 `shot`，
    这样回读认领时能分清「接手后自动发的图」与「客服手工补的图」。 ⚠️ `tag` 只允许字母数字与 `-`（对象名要能直接放进 URL / 审计不装它）。"""
    safe_no = _safe_segment(order_no, fallback="order")
    safe_schema = _safe_segment(_schema_name() if schema is None else schema, fallback="public")
    stamp = int(epoch_ms) if epoch_ms is not None else int(time.time() * 1000)
    safe_tag = _safe_segment(tag, fallback=OBJECT_KEY_TAG) if tag is not None else OBJECT_KEY_TAG
    return f"{OBJECT_KEY_PREFIX}/{safe_schema}/{safe_no}/{safe_tag}-{stamp}.{ext}"


def build_qr_marker(order_no, *, schema: str | None = None) -> str:
    """这条单「我们的群码进度图」在回读里的可识别片段：`tt/<schema>/<单号>/qr-`。
    用它判「图是不是已经发过」——比整对象名宽（跨轮次时间戳不同），又比「任意图片」
    窄（只认我们自己的命名空间）。"""
    safe_no = _safe_segment(order_no, fallback="order")
    safe_schema = _safe_segment(_schema_name() if schema is None else schema, fallback="public")
    return f"{OBJECT_KEY_PREFIX}/{safe_schema}/{safe_no}/{OBJECT_KEY_TAG}-"


def build_message_marker(order_no, rule_id) -> str:
    """这条「接手后话术」在回读里的可识别标记（含我方单号 + 规则 id）。
    上游没有客户端消息 ID ⇒ 只能靠内容认领。标记里带**规则 id**（不是正文）：客服后来
    改规则正文也不会让「这一轮认不出上一轮发过的话术」——一次接手只该有一条留言。"""
    return MESSAGE_MARKER_TEMPLATE.format(order_no=order_no, rule_id=int(rule_id))


def build_message_text(content: str, *, order_no, rule_id: int) -> str:
    """真正发出去的正文：规则正文 + **系统标记**（回读按标记认领，⛔ 不靠首句猜）。
    ⚠️ 与 `dailiantong_auto_reply` 共用**同一个出处**（那边从这里 import / 转发）：
    两条线发出去的标记逐字一致，回读判据才只有一套口径。"""
    text = (content or "").strip()
    return f"{text}{build_message_marker(order_no, rule_id)}"


def marker_in_rows(rows, marker: str) -> bool:
    """留言回读里是否已经出现这个系统标记（任意字符串字段包含它即算）。"""
    if not marker:
        return False
    for row in rows:
        for value in row.values():
            if isinstance(value, str) and marker in value:
                return True
    return False


def select_accept_rule(order) -> PlatformAutoReplyRule | None:
    """选「接单后」那条启用中的规则：**具体游戏优先 → 全部游戏**；没有就 `None`。
    ⚠️ 起 `(platform, game, trigger)` 有库层唯一约束（计划 §2.2）⇒ 同一优先级
    至多一条；这里仍按 `sort_order, id` 取第一条，只为在约束上线前的存量数据上也确定。"""
    base = PlatformAutoReplyRule.objects.filter(
        platform=PLATFORM,
        trigger=TRIGGER_AFTER_ACCEPT,
        is_enabled=True,
    )
    specific = None
    if order.game_id is not None:
        specific = base.filter(game_id=order.game_id).order_by("sort_order", "id").first()
    if specific is not None:
        return specific
    return base.filter(game__isnull=True).order_by("sort_order", "id").first()


def _pick_accept_image(order, rule) -> tuple[str, str, str]:
    """按计划 §2.1 的顺序取**那一张图**：① 本单 ready 群码 → ② 规则图片 → ③ 不发图。
    返回 `(来源, 存储对象名, 行上声明的 Content-Type)`；没有可用图时 `("", "", "")`。
    ⛔ 本函数**不**再看 `HomeQrCode`（首页 QQ 群码是给客户加群用的，计划 §2.1 已删）。"""
    shot = (
        OrderScreenshot.objects.filter(
            order=order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )
        .order_by("pk")
        .first()
    )
    if shot is not None:
        return SOURCE_ORDER_QR, shot.object_key, (shot.content_type or "")

    if rule is not None:
        rule_key = (rule.image_key or "").strip()
        if rule_key:
            return SOURCE_RULE_IMAGE, rule_key, ""
    return "", "", ""


def _load_picture(
    media_key: str, declared_content_type: str, *, label: str = "接手后发送的图片"
) -> tuple[bytes, str, str]:
    """从**我方存储**读图，返回 `(bytes, content_type, ext)`。 - 图种以**字节魔数**为准（`apps.core.storage.IMAGE_MAGIC`，与上传认领侧同一个出处）；
    魔数认不出才退回行上声明的 `content_type`（`PlatformAutoReplyRule.image_key` 没有这一列）； - 两者都给不出图种 ⇒ `_MediaReadError`（⛔ 不猜一个扩展名交给 OSS）。
    - `label` 只影响**可读错误文案**（接手后发送的图片 / 自动回复图片）；读图判图种逻辑同一份。 ⚠️ 异常消息**不含对象名**（内部标识；同鸡蛋铺传图线的隐私口径）。"""
    storage = get_storage()
    with storage.open_stream(media_key) as stream:
        data = stream.read()
    if not data:
        raise _MediaReadError(f"{label}文件是空的")

    content_type = ""
    for magic, magic_type in IMAGE_MAGIC:
        if data.startswith(magic):
            content_type = magic_type
            break
    if not content_type:
        candidate = (declared_content_type or "").strip().lower()
        if candidate in EXT_BY_CONTENT_TYPE:
            content_type = candidate
    if not content_type:
        raise _MediaReadError(f"{label}图片类型无法识别（只支持 jpg / png）")
    return data, content_type, EXT_BY_CONTENT_TYPE[content_type]


def _progress_contains(rows, object_key: str) -> bool:
    """进度回读里是否出现了这次上传的对象名。宽容形态：上游可能把 `Img` 存成裸对象名，也可能拼成完整 URL（实测文档两种口径都
    出现过）⇒ 只要**任意字符串值**里包含对象名就算出现。对象名本身带毫秒时间戳，
    不会与历史进度里的旧图误撞。"""
    for row in rows:
        for value in row.values():
            if isinstance(value, str) and object_key and object_key in value:
                return True
    return False


def _record(push: DailiantongPush, *, reason: str) -> None:
    """写一条审计（机器行；`target` 指向推送行；⛔ 不含凭据 / 对象名）。"""
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


def _save_failure(
    push: DailiantongPush, reason: str, *, count_attempt: bool = False
) -> str:
    """落失败原因：`last_error` + `qr_last_error`（持久副本）；按需累加连续失败次数。"""
    reason = _clip_error(reason)
    update_fields = ["last_error", "qr_last_error", "updated_at"]
    if count_attempt:
        push.qr_attempts = (push.qr_attempts or 0) + 1
        update_fields.append("qr_attempts")
    push.last_error = reason
    push.qr_last_error = reason
    push.save(update_fields=update_fields)
    return reason


def _fail(
    push: DailiantongPush,
    reason: str,
    *,
    audit: bool = True,
    token: str = "",
    count_attempt: bool = True,
) -> QrSendOutcome:
    """失败出口：**先释放自己那次占位**、累加失败次数、写原因、按需写审计。连续失败达到 `MAX_CONSECUTIVE_FAILURES` 时把「已停止自动重发 + 怎么恢复」直接写进
    可读原因（下一轮 `send_accept_qr` 看到上限就不再打上游）。 ⚠️ `count_attempt=False` 用于**不是发送本身失败**的档（例如还没有上游单号）：
    那类问题重试多少次都不会好，⛔ 不该烧掉发送重试预算。"""
    if token:
        _release_claim(push, token)
    push.refresh_from_db(fields=["qr_attempts", "qr_last_error", "last_error"])
    previous = (push.qr_last_error or "").strip()
    attempts = (push.qr_attempts or 0) + 1
    if count_attempt and attempts >= MAX_CONSECUTIVE_FAILURES:
        reason = (
            f"连续失败 {attempts} 次，已停止自动重发：{reason}。"
            "请人工处理；更换本单群码 / 该游戏「接单后」规则的图片后会自动重置重试次数。"
        )
    reason = _save_failure(push, reason, count_attempt=count_attempt)
    if audit and reason != previous:
        _record(push, reason=reason)
    logger.warning(
        "代练通接手后内容未发送 push=%s order=%s：%s",
        push.pk,
        push.order.order_no,
        reason,
    )
    return QrSendOutcome(STATUS_FAILED, reason)


def _skip(push: DailiantongPush, status: str, reason: str, *, write_reason: bool) -> QrSendOutcome:
    """跳过出口：**不打上游**；`write_reason=True` 时把可读原因落进两个错误列。"""
    if write_reason:
        _save_failure(push, reason)
    logger.info(
        "代练通接手后内容跳过 push=%s order=%s：%s",
        push.pk,
        push.order.order_no,
        reason,
    )
    return QrSendOutcome(status, reason)


def _claim_send(push: DailiantongPush) -> tuple[bool, str]:
    """库层发送占位（条件 UPDATE）：返回 `(是否抢到, 令牌)`。
    这是**跨进程**的互斥点：两个进程都看到「还没发」时，只有一个能把
    `qr_send_started_at` 从空（或过期）改成当前时刻；另一个直接退出。"""
    token = uuid.uuid4().hex
    now = timezone.now()
    stale_before = now - timedelta(seconds=QR_CLAIM_STALE_SECONDS)
    updated = (
        DailiantongPush.objects.filter(pk=push.pk, qr_sent_at__isnull=True)
        .filter(
            Q(qr_send_started_at__isnull=True)
            | Q(qr_send_started_at__lt=stale_before)
        )
        .update(qr_send_started_at=now, qr_claim_token=token, updated_at=now)
    )
    return bool(updated), token


def _release_claim(push: DailiantongPush, token: str) -> None:
    """只释放**自己那次**占位（令牌不匹配 = 已被别人接手，⛔ 不动）。"""
    DailiantongPush.objects.filter(pk=push.pk, qr_claim_token=token).update(
        qr_send_started_at=None, qr_claim_token="", updated_at=timezone.now()
    )


# ── 客户端（测试替换点；⛔ 不复制凭据读取）────────────────────────────────


def _open_client(integration):
    """**测试替换点**：委托抛单模块的同一个客户端构造点。"""
    return push_module._open_client(integration)


def _close(client) -> None:
    push_module._close(client)


# ── 对外：接手后发送（唯一入口）───────────────────────────────────────────


def _finish_sent(push: DailiantongPush, *, token: str = "") -> bool:
    """原子收尾：置 `qr_sent_at`、清占位 / 失败原因 / 重试次数。
    返回 `False` = 另一个进程已经先收尾（条件 UPDATE 没命中），调用方按「已发过」处理。"""
    now = timezone.now()
    queryset = DailiantongPush.objects.filter(pk=push.pk, qr_sent_at__isnull=True)
    if token:
        queryset = queryset.filter(qr_claim_token=token)
    updated = queryset.update(
        qr_sent_at=now,
        qr_send_started_at=None,
        qr_claim_token="",
        qr_attempts=0,
        qr_last_error="",
        last_error="",
        updated_at=now,
    )
    push.refresh_from_db()
    return bool(updated)


def send_accept_qr(push: DailiantongPush) -> QrSendOutcome:
    """**接手后发送的唯一入口**：给一条已接手的推送发**最多一条话术 + 最多一张图**。前置（任何一条不满足都**零上游调用**）： 1. （开关只决定**发不发图**，⛔ 不决定发不发话术）； 1.5 ⭐ dl-F：这一行的 `platform` 是代练通（接手后发图 + 话术是代练通专属的上游语义；别的平台没有这套能力 ⇒ 跳过并写可读原因，⛔ 不拿代练通的客户端去打它）； 2. 这条推送**还没发过**（`qr_sent_at is None`，留言+图共用的**单标记**）； 3. 远端不在撤销流程中（`remote_cancel_status` 不在 11/12/14/15/16）；
    4. 有上游单号； 5. 至少有一项可发内容（图：**开关开着时**本单 ready 群码 / 该游戏「接单后」规则的图片；文字：该游戏「接单后」规则的正文，**与开关无关**）——两项都没有 ⇒ `skipped_no_content`； 6. 连续失败次数未达上限（`qr_attempts < MAX_CONSECUTIVE_FAILURES`；换图 / 换规则自动归零）。 ⚠️ **开关只管图**（⭐ 业务方口径）：`image_send_enabled()` 为假时 `media_key` 恒为空 ⇒ 只发规则话术（有规则的话）；没有规则 ⇒ `skipped_no_content`
    并给「开关关 + 无话术」的可读原因。 ⭐ 幂等三层（审计修复 P1-1 / P1-2）——**先回读、再占位、再分批补发**： - 动上游之前先读留言列表 + 进度列表，用「系统标记（含单号 + 规则 id）」和「`tt/<schema>/<单号>/qr-` 片段」判断哪几步已经发过； - 已发过的步骤**不再重放**（上一轮图成功、留言失败 ⇒ 本轮只补留言）； - 两步都已在 ⇒ 只补记 `qr_sent_at`，零写调用； - 还要发才用条件 UPDATE 抢**库层发送占位**（`qr_send_started_at` + `qr_claim_token`），抢不到 = 另一个进程在发，本进程直接退出； - 任何失败都释放自己那次占位、连续失败次数 +1；到上限停止重发并写可读原因。返回 `QrSendOutcome`；本函数**不抛**业务失败（存储 / 上游 / 上传的预期失败都翻成 `failed`），只有编程错误会向上抛。"""
    if push.qr_sent_at is not None:
        return QrSendOutcome(STATUS_ALREADY_SENT, "这一条推送已经完成接手后发送")

    # ⭐ dl-F（多平台骨架）：接手后发送只有代练通适配器有；别的平台的行在这里就停，
    # ⛔ 零上游调用（调用方 `dailiantong_sync` 已经先按行分派，这里是直接调用方的兜底）。
    if int(push.platform or 0) != PLATFORM:
        return _skip(
            push,
            STATUS_SKIPPED_UNSUPPORTED_PLATFORM,
            f"平台 {push.platform} 没有接手后发送能力（只支持代练通），不自动发送",
            write_reason=True,
        )

    order = push.order
    cancel_status = _as_int(push.remote_cancel_status)
    if cancel_status is not None and cancel_status in CANCEL_STATUSES_IN_PROGRESS:
        return _skip(
            push,
            STATUS_SKIPPED_CANCELLING,
            f"订单撤销流程中（CancelStatus={cancel_status}），不自动发送接手后内容",
            write_reason=False,
        )

    serial_no = (push.serial_no or "").strip()
    if not serial_no:
        # ⚠️ 无单号不是发送失败：不消耗连续失败预算。
        return _fail(
            push,
            "这条推送还没有上游订单号，不能发送接手后内容",
            count_attempt=False,
        )

    # ⭐ 内容来源：一条规则（接单后，**与开关无关**）+ 一张图（**开关开着才发**；
    #    图源顺序 = 本单群码优先，其次规则图片）。
    rule = select_accept_rule(order)
    image_enabled = image_send_enabled()
    if image_enabled:
        source, media_key, declared_content_type = _pick_accept_image(order, rule)
    else:
        source, media_key, declared_content_type = "", "", ""
    message = ""
    message_marker = ""
    if rule is not None and (rule.content or "").strip():
        message_marker = build_message_marker(order.order_no, rule.pk)
        message = build_message_text(
            rule.content, order_no=order.order_no, rule_id=rule.pk
        )
    if not media_key and not message:
        # 两项都没有 ⇒ 跳过，但**必须留下可读原因**；不置 qr_sent_at ⇒ 之后补上
        # 本单群码 / 规则图片 / 规则话术后，下一轮还能发。
        if not image_enabled:
            return _skip(
                push,
                STATUS_SKIPPED_NO_CONTENT,
                SWITCH_OFF_AND_NO_TEXT_REASON,
                write_reason=True,
            )
        return _skip(
            push, STATUS_SKIPPED_NO_CONTENT, NO_CONTENT_REASON, write_reason=True
        )

    # 换图 / 换规则 = 新的失败预算（否则一次上游抖动用完 5 次后就永远不再试）。
    retry_source_key = (
        f"{source}:{media_key}:{rule.pk if rule is not None else 0}"
    )[:255]
    if (push.qr_retry_source_key or "") != retry_source_key:
        push.qr_retry_source_key = retry_source_key
        push.qr_attempts = 0
        push.save(update_fields=["qr_retry_source_key", "qr_attempts", "updated_at"])
    if (push.qr_attempts or 0) >= MAX_CONSECUTIVE_FAILURES:
        return _skip(
            push,
            STATUS_SKIPPED_RETRY_LIMIT,
            f"连续失败 {push.qr_attempts} 次，已停止自动重发；"
            "请人工处理后更换本单群码 / 该游戏「接单后」规则的图片（会自动重置重试次数）",
            write_reason=False,
        )

    data = b""
    content_type = ""
    ext = ""
    if media_key:
        try:
            data, content_type, ext = _load_picture(
                media_key, declared_content_type, label="接手后发送的图片"
            )
        except _MediaReadError as exc:
            return _fail(push, f"读取接手后发送的图片失败：{exc}")
        except Exception as exc:  # noqa: BLE001 —— 存储后端的异常族由后端决定
            # ⛔ 只记类型名：异常文本可能带对象名 / 存储路径。
            return _fail(push, f"读取接手后发送的图片失败（{type(exc).__name__}）")

    try:
        integration = push_module._active_integration(push.platform)
    except DailiantongPushError as exc:
        return _fail(push, f"代练通账号配置不可用：{exc}")

    image_marker = build_qr_marker(order.order_no) if media_key else ""
    object_key = ""
    token = ""

    client = None
    try:
        client = _open_client(integration)

        # ① 动上游之前先回读：哪几步已经发过了？（上一轮图成功 / 留言失败 / 进程被杀）
        try:
            message_page = client.message_list(
                serial_no, max_count=MESSAGE_READBACK_MAX
            )
            # 本轮没有话术要发 ⇒ 这一步天然完成；有才去回读里认标记。
            message_done = (
                marker_in_rows(message_page.rows, message_marker)
                if message_marker
                else True
            )
            if media_key:
                progress_page = client.progress_list(
                    serial_no, max_count=PROGRESS_READBACK_MAX
                )
                image_done = _progress_contains(progress_page.rows, image_marker)
            else:
                image_done = True  # 本轮没有图要发 ⇒ 这一半天然完成
        except DailiantongError as exc:
            return _fail(
                push,
                f"回读已发留言 / 进度失败（{exc.message}），不能判断是否已经发过",
            )

        if message_done and image_done:
            # 两步都在上游了（典型：上一轮全部发成功、但收尾前进程被杀）。
            # ⛔ 不再重发，只补记账；失败原因 / 重试次数一起清掉。
            reason = "回读确认接手后内容都已存在，补记发送时间"
            if not _finish_sent(push):
                return QrSendOutcome(
                    STATUS_ALREADY_SENT, "这一条推送已经完成接手后发送"
                )
            _record(push, reason=reason)
            logger.info(
                "代练通接手后内容回读确认已发齐 push=%s order=%s",
                push.pk,
                order.order_no,
            )
            return QrSendOutcome(STATUS_SENT, reason, object_key="", source=source)

        # ② 库层占位：两个进程只有一个能往下走。
        claimed, token = _claim_send(push)
        if not claimed:
            fresh = (
                DailiantongPush.objects.filter(pk=push.pk)
                .values("qr_sent_at")
                .first()
            )
            if fresh and fresh["qr_sent_at"] is not None:
                return QrSendOutcome(
                    STATUS_ALREADY_SENT, "这一条推送已经完成接手后发送"
                )
            return _skip(
                push,
                STATUS_SKIPPED_IN_PROGRESS,
                "另一个进程正在发送这条单的接手后内容，本进程不重复发",
                write_reason=False,
            )

        # ③ 只补**还没发过**的步骤；顺序沿用「先传图、再留言」（图失败时留言一次都不发；
        #    留言失败时图还在，下一轮回读会跳过图、只补留言）。
        if media_key and not image_done:
            try:
                sts = client.get_sts()
            except DailiantongError as exc:
                return _fail(push, f"取 OSS 临时凭证失败：{exc.message}", token=token)

            object_key = build_object_key(order.order_no, ext)
            try:
                put_object(sts, object_key, data, content_type)
            except OssUploadError as exc:
                return _fail(push, exc.message, token=token)

            try:
                client.progress_add(serial_no, PROGRESS_CAPTION, img=object_key)
            except DailiantongError as exc:
                return _fail(push, f"发进度失败：{exc.message}", token=token)

            # ⛔ 自证：`LevelOrderProgressAdd` 会假成功 ⇒ 必须回读列表看到这个对象名。
            # ⚠️ 显式 `Max`（理由见 `PROGRESS_READBACK_MAX`）：⛔ 不拿上游默认首屏核验。
            try:
                page = client.progress_list(
                    serial_no, max_count=PROGRESS_READBACK_MAX
                )
            except DailiantongError as exc:
                return _fail(
                    push,
                    f"已发进度，但回读进度列表失败（{exc.message}），没能核验",
                    token=token,
                )
            if not _progress_contains(page.rows, object_key):
                return _fail(
                    push,
                    "已发进度，但回读进度列表里没看到这张图，没能核验",
                    token=token,
                )

        if message and not message_done:
            try:
                client.message_add(serial_no, message)
            except DailiantongError as exc:
                return _fail(push, f"发留言失败：{exc.message}", token=token)
    finally:
        if client is not None:
            _close(client)

    if not _finish_sent(push, token=token):
        # 收尾条件没命中（另一个进程先收尾了）：⛔ 不把重复行为播报成我们的成功。
        return QrSendOutcome(STATUS_ALREADY_SENT, "这一条推送已经完成接手后发送")

    parts = []
    if media_key:
        parts.append(
            "本单群码已发送" if source == SOURCE_ORDER_QR else "接单后规则图片已发送"
        )
    if message:
        parts.append("规则话术已发送")
    if not parts:  # 防御：上面已经按「至少一项」闸过，这里只防未来改坏
        parts.append("接手后内容已发送")
    reason = "；".join(parts)
    _record(push, reason=reason)
    logger.info(
        "代练通接手后内容已发送 push=%s order=%s source=%s message=%s image=%s",
        push.pk,
        order.order_no,
        source,
        bool(message),
        bool(media_key),
    )
    return QrSendOutcome(STATUS_SENT, reason, object_key=object_key, source=source)
