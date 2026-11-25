"""代练通抛单业务层：前置闸 → 组装载荷 → 发单 → 落库 → 留痕；协议/签名在 `dailiantong_client.py`。红线（`docs/DAILIANTONG-API.md` §6.1/§9，判据 `tests.test_dailiantong_push`）：写动作超时/断连一律落 `state=unknown`、⛔ 不重试不自动改状态；价格下限 2 元（按游戏/区服不同，本地只挡全局、其余上游回中文）；`Actors` 用固定占位模板（账号 8 位/密码 8 位/角色名 ≥2 字，「接单后联系客服」放代练要求段）；凭据不进 `last_error`/审计/异常文案。失败分层：前置闸抛 `DailiantongPushError`，上游业务失败回 `state=failed` 行，传输失败回 `state=unknown` 行（⛔ 不抛 500）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db import connection as db_connection
from django.db.models import Count, Q
from django.utils import dateparse, timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.dailiantong_client import (
    DEFAULT_BASE_URL,
    DailiantongClient,
    DailiantongConfig,
    DailiantongError,
    DailiantongTransportError,
)
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, changes_approved_penalty_price
from apps.business.models import (
    DailiantongPush,
    DailiantongZoneMap,
    EmpType,
    Game,
    OrderStatus,
    Performance,
    PlatformIntegration,
    PlatformPushSettings,
    SettleStatus,
)
from apps.business.peer_sync import PEER_DIRECT_PENDING_MESSAGE
from apps.business.platform_egress import dailiantong_base_url
from apps.rbac.permissions import has_node_now

logger = logging.getLogger(__name__)

#: 代练通平台 id（与 `PlatformIntegration.Platform.DAILIANTONG` 同值；这里写字面量
#: 是为了让「默认平台」在签名上就是一个普通 int，不把枚举带进调用方）。
DEFAULT_PLATFORM = 10

#: 实测全局最低价 2 元（1 元 `-4`、2 元成功）；最低价按游戏/区服不同（通用区实测 5 元），
#: ⛔ 不要因某次 `-4` 改成 5——本常量只放全局下限，其余交给上游（本地无区服粒度表）。
MIN_PRICE = Decimal("2.00")

#: 标题 2–128 字（真机实测推翻文档「2–28 汉字」：超长不报 `-8`，但服务端静默截到 128）。
#: ⇒ 本地必须自己截，否则内容被上游切得莫名其妙。
TITLE_MIN_LENGTH = 2
TITLE_MAX_LENGTH = 128

#: 占位资料固定段：账号不能固定（上游「同一游戏账号已发布不能重复发」，固定会让第二张单被拒，
#: 按订单号派生见 `_actor_account`）；密码/角色名固定即可（实测长度校验只要求 8 位/≥2 字）。
#: 第 4 段是代练通「订单说明」栏的展示内容（见 `build_actors`），⛔ 不是固定常量。
ACTORS_PASSWORD = "ttpass01"
ACTORS_ROLE = "待定角色"
ACTORS_REQUIREMENT = "接单后联系客服获取账号"

#: `Actors` 第 4 段上限 480：整列上限 1024，各段+分隔符合计 1000 ≤ 1024；超长优先截第 4 段，
#: 保第 5 段（代练要求）完整。
ACTORS_CURRENT_INFO_MAX_LENGTH = 480

#: 「代练/陪玩要求」模板长度上限（上游 `Actors` 整列 1024，前四段+分隔符约 45 字，500 留足余量；
#: 出处 `docs/references/dailiantong/open-api-v3.4.md:555`）。唯一出处：接口校验与 `Actors` 组装都按它取（⛔ 不抄字面量）。
REQUIRE_TEMPLATE_MAX_LENGTH = 500

#: 「拒绝新手和介入率」的取值上限（上游文档：`LimitAccept` = 拒绝客服介入**百分比**大于
#: 此值的人接单 ⇒ 0–100）。服务层与接口层共用这一个出处。
LIMIT_ACCEPT_MAX = 100

#: 「自动下架天数」本地上界 30：取自证据件 `public/replica/order-pushjdp.html` 的下拉档
#: （上游文档只写默认 7 天、没写上界）；`0`=「不设置」档，前端不上送这个键。
OFF_SELL_DAYS_MAX = 30

#: `gamezoneserver` 的模块级缓存时长（10 分钟）。上游一次回 4327 条、没有分页参数
#: （§4 #1），页面上每敲一个字都打一次不可接受。
ZONES_CACHE_TTL_SECONDS = 600.0

#: 审计动作码（登记在 `apps/audit/actions.py`）。
AUDIT_ACTION = "order.dailiantong_push"
#: 审计的 `target_kind`（列宽 16，必须**恰好** ≤16；多一个字符会被 `record()` 静默截断）。
AUDIT_TARGET_KIND = "dailiantong_push"

#: `last_error` 的列宽（模型列宽的唯一出处；服务层截断按它取，不抄数字）。
LAST_ERROR_MAX_LENGTH = DailiantongPush._meta.get_field("last_error").max_length

#: 远端终态（`docs/DAILIANTONG-API.md` §5）：`Status=17` 已结算 + `CancelStatus∈{12,15,16}` 撤销
#: 已完成（实现=`TERMINAL_REMOTE_CANCEL_STATUSES` 别名，⛔ 别处不再写字面量）。这是**轮询闸门**
#: 判据、非状态机判据：终态行仍是 `sent`、仍占有效推送唯一索引，⛔ 不得因此再抛一次。
TERMINAL_REMOTE_STATUSES = frozenset({17})

#: 远端「订单已被删除」的证据片段（人工删单/上游清理，非读取失败）；单一出处，
#: 自动撤回和状态回读共用 `remote_absent_evidence`。
#: 口径要窄：⛔ 不把任意业务错误读成「已删除」，否则暂时性失败被误判终态、页面不再轮询。
REMOTE_ABSENT_HINTS = (
    "已被删除",
    "已删除",
    "删除成功",
    "没有找到",
    "不存在",
    "查不到",
)

#: `pending` 行超过这个时长仍没有结果 ⇒ 视为「进程在发单中途被杀」，标成 `unknown`。
#: 业务口径（审计修复 P1-4）：`unknown` 会挡住再次抛单、并进入人工核对；
#: 而 `pending` 没有任何出口（同步只轮询 sent / unknown，闸 4 只回「请稍后刷新」）。
PENDING_TIMEOUT_SECONDS = 300

#: 回读连续失败的退避（分钟）：1 / 5 / 15 / 60。第 5 次失败后停止轮询（P3-12）。
SYNC_BACKOFF_MINUTES = (1, 5, 15, 60)
#: 回读连续失败上限（达到后 `is_active=False`，原因写进 `last_error` 下发读面）。
MAX_SYNC_FAILURES = 5

#: 人工把 `unknown` 标成 `failed` 时，核对依据的最短长度（一句话，⛔ 不许空着点）。
MANUAL_FAILURE_EVIDENCE_MIN_LENGTH = 5

# ── 远端状态 → 本地订单状态（计划 §2）────────────────────
#: 业务方口径「状态必须与代练通一致」；唯一映射出处（同步回写/并发抢单都读它，⛔ 别处不抄一份）。
#: 14/15/16/18 只记录不改本地状态（见 `REMOTE_STATUS_RECORD_ONLY`；下架=「没在卖」，等 12/13/17/撤销完成再对齐）。
REMOTE_STATUS_LOCAL_STATUS = {
    11: OrderStatus.WAITING_GRAB,
    12: OrderStatus.IN_PROGRESS,
    13: OrderStatus.PENDING_REVIEW,
    17: OrderStatus.COMPLETED,
}

#: 远端 `Status=16`「撤销中」（真机码；M3 的重发文案要区分它和「刚被接手」）。
REMOTE_STATUS_CANCELLING = 16
#: 远端 `Status=13`「等待验收」：首次进入时记本地验收倒计时起点（⛔ 不同步上游）。
REMOTE_STATUS_WAITING_ACCEPTANCE = 13
#: 远端 `Status` 里**只记录、不改本地状态**的码（Rev.9 §2 的 14 / 15 / 16 + 10 临时订单
#: + 18 已下架：真机，上游码表没列这一档，但下架后实测回 18）。
REMOTE_STATUS_RECORD_ONLY = frozenset({10, 14, 15, 16, 18})

#: 「远端撤销已完成」的 `CancelStatus`（`docs/DAILIANTONG-API.md` §5）：12 协商已处理 / 15 客服已处理 /
#: 16 客服强制撤销；三档 ⇒ 读面「已撤销/已强制撤销」、轮询终态、本地落已退单（h1 真机 bug 曾只认 16）。
#: 唯一出处：轮询闸门 `TERMINAL_REMOTE_CANCEL_STATUSES` 就是本集合的别名。
CANCEL_STATUS_REVOKED = frozenset({12, 15, 16})
#: 撤销还在流程里的码：`11` 协商撤销中 / `14` 客服介入中（读面显示「撤销中 / 客服介入中」）。
CANCEL_STATUS_IN_FLIGHT = frozenset({11, 14})
#: `14` 客服介入中（撤销流程里的一个具体码；读面按它给「客服介入中」）。
CANCEL_STATUS_ARBITRATING = 14
#: 远端「不算已接手」的否定词表（10 临时订单/11 未接手/18 已下架；唯一出处，别处不另写一套）。
#: ⛔ 18 必须在此：已下架=没在卖没打手，当「已接手」会让大厅抢单/抛大厅/reconcile 全误判（H7）。
#: 放宽/收紧前先读 `docs/DAILIANTONG-API.md` §5 与 §6 的真机记录。
REMOTE_STATUS_NOT_ACCEPTED = frozenset({10, 11, 18})
CANCEL_STATUS_NOT_CANCELLED = 10
#: 协商撤销中（`LevelOrderCancel(Flag=0)` 提交后的远端状态）。⭐ D8：这也是「可重发」的
#: 远端依据之一；但服务层优先认本地 `cancel_requested_at`（⛔ 不能只靠远端码猜）。
CANCEL_STATUS_NEGOTIATING = 11
#: 远端**已有人接手**的撤销码（协商中 / 已处理 / 客服介入 / 客服处理 / 强制撤销）——
#: 它们本身就是「这张单曾经被接过」的证据（Rev.9 §4 的「代练通优先」判据用）。
CANCEL_STATUS_IMPLIES_ACCEPTED = frozenset({11, 12, 14, 15, 16})

#: 远端撤销流程的**终态**（轮询闸门）：就是 `CANCEL_STATUS_REVOKED` 那三档。
#: ⚠️ 别名而不是第二次字面量 —— 两处漂移会让「已撤销的单还在轮询」复活（h1 的 bug）。
TERMINAL_REMOTE_CANCEL_STATUSES = CANCEL_STATUS_REVOKED

# ── 撤销协商的发起方（`cancel_initiator`）────────────
# 真机判据（`docs/DAILIANTONG-API.md` §6.13）：我方 `LevelOrderCancel(Flag=0)` ⇒ `CancelUID==CreateUID`；打手申请 ⇒ `==AcceptUID`。
# ⚠️ `CancelUID` 只记「最近一次」发起方（`CancelStatus` 回 10 仍留旧值）⇒ 同意撤销须叠加 `CancelStatus == 11`（`agree_cancel_availability`）。
CANCEL_INITIATOR_TAKER = "taker"
CANCEL_INITIATOR_US = "us"
#: 三个合法取值（空串 = 无法判定）；读面只下发这三个字面量。
CANCEL_INITIATORS = frozenset({"", CANCEL_INITIATOR_TAKER, CANCEL_INITIATOR_US})

#: 读面「综合状态」的 token → 中文（`display_status` / `display_status_label`）。
#: 前端「交易状态」列优先显示后端下发的 label（⛔ 页面不得只看 `remote_status`）。
#: token 是**稳定契约**：前端按它取色档（`platformPush.ts::DISPLAY_STATUS_BADGE_CLASSES`）。
DISPLAY_STATUS_LABELS = {
    "temp_order": "临时订单",
    "unaccepted": "未接手",
    "in_progress": "正在代练",
    "waiting_review": "等待验收",
    "abnormal": "订单异常",
    # ⭐ 真机核实：`Status=15` = **被我们锁定**（`LevelOrderLock(Locked=1)` 的结果；
    # 解锁会回到锁定前的 12/13/14）⇒ 读面中文「已锁定」，⛔ 不是「帐号被平台锁」。
    # 与 `dailiantong_actions.REMOTE_STATUS_LABELS[15]` / 前端 `platformPush.ts` 三处同值。
    "locked": "已锁定",
    "cancelling": "撤销中",
    "settled": "已结算",
    "revoked": "已撤销",
    "force_revoked": "已强制撤销",
    "arbitrating": "客服介入中",
    # ⭐ 真机：被下架后上游回 `Status=18`（上游码表
    # 只有 10–17）⇒ 读面给「已下架」。色档在 `platformPush.ts::DISPLAY_STATUS_BADGE_CLASSES`
    # 取对标 default 的灰（对标没有「已下架」这一档，那一条是**推断**，见该文件注释）。
    "off_shelf": "已下架",
}

#: 远端 `Status` → 读面 token（`CancelStatus` 没给出可判语义时才走这里）。
DISPLAY_STATUS_BY_REMOTE_STATUS = {
    10: "temp_order",
    11: "unaccepted",
    12: "in_progress",
    13: "waiting_review",
    14: "abnormal",
    15: "locked",
    16: "cancelling",
    17: "settled",
    # ⭐ 真机：`Status=18` = 已下架（上游码表没列这一档）。
    18: "off_shelf",
}

#: 本地订单状态里的**终态**（业务方）：「已完成 / 已退单」都表示这张单已收口。
#: 远端撤销完成要把本地写成已退单时，本地若已是**另一个**终态 ⇒ 只记原因、⛔ 不来回改。
LOCAL_TERMINAL_STATUSES = frozenset({OrderStatus.COMPLETED, OrderStatus.REFUNDED})
_LOCAL_TERMINAL_STATUS_NAMES = frozenset(str(status) for status in LOCAL_TERMINAL_STATUSES)

#: 本地状态里「还在做 / 已经做完」的档：远端说「已接手」时，本地一定是这几档之一。
_LOCAL_STATUS_AFTER_ACCEPT = (
    OrderStatus.IN_PROGRESS,
    OrderStatus.PENDING_REVIEW,
    OrderStatus.ISSUE,
    OrderStatus.PAUSED,
)

#: 代练通同步（机器）写状态 / 写审计时用的操作人名。
#: ⚠️ 照 `jidanpu_member.MACHINE_ACTOR` / `jidanpu_upstream.MACHINE_ACTOR` 的同形口径：
#: 它不是任何一个人类账号，`actor_id=None` + `system=True`。
SYNC_ACTOR = "代练通同步"

#: 每租户占位接单人「代练通」：不可登录（`is_active=False`+unusable password），自动创建/复用；
#: 存在同名且可登录的账号时 `placeholder_taker_user()` 响亮失败（⛔ 绝不停用真人账号）。
#: 判据按固定 username（⛔ 不按显示名相等判断；读面另有 `taker_is_platform` 布尔键）。
VIRTUAL_TAKER_USERNAME = "dailiantong_taker"
VIRTUAL_TAKER_DISPLAY_NAME = "代练通"

#: ⭐ 业务方）：代练通**已无活跃订单**（全部推送结束）⇒ 清掉占位接手人。
#: 审计动作码（登记在 `apps/audit/actions.py`；机器行）。
AUDIT_CLEAR_TAKER = "order.dailiantong_clear_taker"
CLEAR_TAKER_REASON = "代练通已无活跃订单（全部结束）⇒ 清除占位接手人"

#: A4：「内部打手先接 ⇒ 撤回代练通订单」的**有界重试**上限（超过就停，原因进 `remote_note`）。
MAX_CLAIM_WITHDRAW_ATTEMPTS = 3

# ── ⭐ H3-1：撞单 0 撤 ──────────────────────────────────────

#: 「撞单」里算**已接手**的远端状态（正在代练 12 / 等待验收 13）。
#: ⚠️ 只认这两档：11 还没人接（撞单无从谈起）、16 撤销中 / 17 已结算是**不需要也不能**
#: 再申请撤销的档。唯一实现（扫描与动作函数共用这一份）。
CONFLICT_ACCEPTED_REMOTE_STATUSES = frozenset({12, 13})

#: 撞单 0 撤的**有界重试**上限（超过就停手，原因进 `conflict_zero_refund_note`）。
#: 与自动仲裁段同一档：5 次 × 6 小时 ≈ 30 小时后交人工，⛔ 不每 30 秒刷上游。
MAX_CONFLICT_ZERO_REFUND_ATTEMPTS = 5
#: 撞单 0 撤失败后的退避（与 `AUTO_ARBITRATE_RETRY` 同值；都是「上游暂时性失败」的量级）。
CONFLICT_ZERO_REFUND_RETRY = timedelta(hours=6)
#: `conflict_zero_refund_note` 的列宽（模型列宽的唯一出处）。
CONFLICT_ZERO_REFUND_NOTE_MAX_LENGTH = DailiantongPush._meta.get_field(
    "conflict_zero_refund_note"
).max_length

#: `remote_note` 的列宽（模型列宽的唯一出处）。
REMOTE_NOTE_MAX_LENGTH = DailiantongPush._meta.get_field("remote_note").max_length


class DailiantongPushError(Exception):
    """前置闸业务失败：可直接展示给操作人、未调用写上游；接口层翻成 `code=1002` 中文 `msg`。
    与 `DailiantongError`（上游回了业务失败，不抛、返回推送行）分开。"""


# ── 小工具 ──────────────────────────────────────────────────────────────


def _to_decimal(value) -> Decimal:
    """把入参归一成 `Decimal`（`str()` 中转避免二进制浮点的 `0.1` 精度尾巴）。"""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _fmt_money(value) -> str:
    """金额展示格式：两位小数（⛔ 不要 `5.0000`/`5`）；只服务展示，金额比较在 `_to_decimal` 后的原始值上做。"""
    return format(_to_decimal(value).quantize(Decimal("0.01")), "f")


def _clip_error(message: str) -> str:
    """按 `last_error` 列宽截断（⛔ 不靠数据库 `DataError` ⇒ 500）。"""
    return (message or "")[:LAST_ERROR_MAX_LENGTH]


def _as_int(value) -> int | None:
    """上游状态码可能是 int 也可能是 str（§3 第 1 条）；**解析不了就返回 `None`**。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def remote_absent_evidence(message: str) -> str:
    """上游明确说订单不存在时，返回命中的文字片段。"""
    text = (message or "").strip()
    if "或" in text and ("接手" in text or "接单" in text):
        return ""
    for hint in REMOTE_ABSENT_HINTS:
        if hint in text:
            return hint
    return ""


def _project_label(order) -> str:
    """订单的「项目 / 标题」可读值：**以 `project_text` 为准**（D9），没有才用 FK。"""
    text = (order.project_text or "").strip()
    if text:
        return text
    if order.project_id:
        return (order.project.name or "").strip()
    return ""


#: 标题截断时优先落刀的「词界」：中英文标点与空格（业务方反馈：硬截会把句子切成半个词）。
#: 收的是在截断处收尾后仍读得通的字符（含开括号）；不收右括号/右引号——落刀在它们前面时不该把单个 `】` 留在末尾。
TITLE_BOUNDARY_CHARS = "，。、；：！？,.;:!? 【（"


def _title_piece(value) -> str:
    """标题里的一段：把连续空白（含换行）压成一个空格（标题是单行短文本）。"""
    return " ".join(str(value or "").split())


def _truncate_title(title: str, limit: int) -> str:
    """截到 `limit` 字以内：优先在标点/空格处落刀（含该标点），落刀后不足 2 字退回硬截。
    ⛔ 不按字节截断（中英文混排会把中文劈成半个字符）。"""
    head = title[:limit]
    for index in range(len(head) - 1, -1, -1):
        if head[index] in TITLE_BOUNDARY_CHARS:
            candidate = head[: index + 1].rstrip()
            if len(candidate) >= TITLE_MIN_LENGTH:
                return candidate
            break
    return head.rstrip()


def build_title(order) -> str:
    """组装上游 `Title`：项目/标题 + `Order.content` 摘要，本地保证 2–128 字；⛔ 游戏名一个字不进标题
    （业务方真机口径，`tests.test_dailiantong_push.TitleTests` 钉着），完整内容走 `Memo`（`build_memo`）。
    ⛔ 超长上游不报错、静默截断（§9 R8 实测）⇒ 必须本地截：优先标点/空格落刀，找不到词界才硬截。"""
    item = _title_piece(_project_label(order))
    excerpt = _title_piece(getattr(order, "content", "") or "")
    title = " ".join(part for part in (item, excerpt) if part).strip()
    if not title:
        title = "代练订单"
    if len(title) > TITLE_MAX_LENGTH:
        title = _truncate_title(title, TITLE_MAX_LENGTH)
    if len(title) < TITLE_MIN_LENGTH:
        # 补的是**业务上说得通**的中性词，不是空格（上游会当空标题）。
        title = f"{title}代练"[:TITLE_MAX_LENGTH].strip()
    return title


def _actor_account(order_no) -> str:
    """按订单号派生 8 位占位账号（同单稳定、不同单不同）。⛔ 不能固定：上游「同一游戏账号已发布
    不能重复发」会让第二张单被拒（§9 R8，按真钱风险对待）；用摘要而非「单号后 6 位」（前缀不同尾号相同会撞）。
    """
    digest = hashlib.md5(
        str(order_no).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return f"tt{digest[:6]}"


def _derived_digits(order_no, *, salt: str, length: int) -> str:
    """按订单号派生稳定数字串（同单恒定、不同单几乎不撞）；`salt` 区分字段（手机/QQ），
    避免同一摘要直接复用让手机号与 QQ 号呈现关联（不必要的信息泄漏）。
    """
    digest = hashlib.md5(
        f"{salt}:{order_no}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    # 摘要本身是十六进制；取十进制数字流（把 a–f 丢掉会让长度不够，所以循环补摘要）。
    digits = "".join(char for char in digest if char.isdigit())
    counter = 0
    while len(digits) < length:
        counter += 1
        extra = hashlib.md5(
            f"{salt}:{counter}:{order_no}".encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        digits += "".join(char for char in extra if char.isdigit())
    return digits[:length]


def _contact_mobile(order_no) -> str:
    """按订单号派生的稳定手机号（11 位、`1` 开头，非真实号码）；计划 §2.3：联系电话/QQ 不再用配置字段、
    改按单号派生（同单稳定，客服回查不换号）。⚠️ 上游是否校验手机格式未实测（计划 §7 已登记风险）。
    """
    return "1" + _derived_digits(order_no, salt="mobile", length=10)


def _contact_qq(order_no) -> str:
    """按订单号派生的稳定 QQ 号（9 位、首位非 0）；占位值非真实用户 QQ（`AGENTS.md` §5-C 合成值口径）。"""
    digits = _derived_digits(order_no, salt="qq", length=9)
    # 首位 1–9：`0` 开头的 QQ 号不像号（上游若做格式校验会被拒）。
    first = str(int(digits[0]) % 9 + 1)
    return first + digits[1:]


def require_template_from_settings() -> str:
    """全局「代练/陪玩要求」模板（空=用既有占位文案）；唯一出处 `PlatformPushSettings.require_template`，
    超长按 `REQUIRE_TEMPLATE_MAX_LENGTH` 截（接口层已拦，这里防直接调服务层）。
    """
    template = (PlatformPushSettings.get_solo().require_template or "").strip()
    if not template:
        return ACTORS_REQUIREMENT
    return template[:REQUIRE_TEMPLATE_MAX_LENGTH]


def _truncate_actors_text(text: str, limit: int) -> str:
    """截到 `limit` 字内：按标点/空格落刀+省略号，找不到才硬截；结果总长 ≤ `limit`（省略号计入）。
    第 4 段是代练通「订单说明」栏的展示内容，不能切出半句话。
    """
    if len(text) <= limit:
        return text
    head = text[: max(1, limit - 1)]
    for index in range(len(head) - 1, -1, -1):
        if head[index] in TITLE_BOUNDARY_CHARS:
            candidate = head[: index + 1].rstrip()
            if candidate:
                return (candidate + "…")[:limit]
            break
    return (head.rstrip() + "…")[:limit]


def _actors_current_info(order) -> str:
    """`Actors` 第 4 段 = 代练项目标题（业务方口径：代练通按项目识别的订单内容，⛔ 不是订单说明、不取 `memo`）。
    取值 `project_text` → `project.name` → 兜底「代练订单」；超长按 `ACTORS_CURRENT_INFO_MAX_LENGTH` 截（保第 5 段完整）；
    ⛔ 用户文本里的 `|*|` 必须替换（否则 `Actors` 被切成 6 段、上游报参数错）。"""
    text = (getattr(order, "project_text", "") or "").strip()
    if not text:
        project = getattr(order, "project", None)
        text = (getattr(project, "name", "") or "").strip()
    if not text:
        text = "代练订单"
    text = text.replace("|*|", "| * |")
    return _truncate_actors_text(text, ACTORS_CURRENT_INFO_MAX_LENGTH)


def build_actors(order, *, requirement: str | None = None) -> str:
    """组装 `Actors`：`账号|*|密码|*|角色名|*|当前游戏信息|*|代练要求`；账号 8 位（按订单派生）、密码 8 位、
    角色名 ≥2 字满足实测长度校验（§6.1：整段文案当账号会被 `-8` 拒）。第 4 段=代练项目标题（见 `_actors_current_info`），
    第 5 段=全局模板（空用 `ACTORS_REQUIREMENT`，计划 §2.4）；`requirement=None`=自读设置（`push_order` 预读传入，同轮只一次 `get_solo()`）。"""
    account = _actor_account(order.order_no)
    text = (require_template_from_settings() if requirement is None else requirement) or ""
    text = text.strip() or ACTORS_REQUIREMENT
    return "|*|".join(
        [
            account,
            ACTORS_PASSWORD,
            ACTORS_ROLE,
            _actors_current_info(order),
            text[:REQUIRE_TEMPLATE_MAX_LENGTH],
        ]
    )


def _contact_name(operator, integration) -> str:
    """`ContactName` 的占位值。上游会**强制改写成账号昵称**（§6.1 第 5 条），
    所以我们给的只是「别是空串」的兜底：操作员昵称 / 用户名 / 平台账号。"""
    for attr in ("first_name", "username"):
        value = (getattr(operator, attr, "") or "").strip()
        if value:
            return value
    return (integration.account or "").strip() or "客服"


def _active_integration(platform: int) -> PlatformIntegration:
    """本租户**启用且默认**的平台账号；缺配置 / 缺凭据都翻成中文业务错误。"""
    integration = PlatformIntegration.objects.filter(
        platform=platform, is_enabled=True, is_default=True
    ).first()
    if integration is None:
        raise DailiantongPushError(
            "本租户还没有「启用且默认」的代练通账号。"
            "请先到「系统 → 发平台设置 → 交易平台对接设置」新增一个代练通账号并设为默认。"
        )

    # ⚠️ 只报**字段名**，⛔ 不带任何值（凭据红线）。
    missing = []
    if not (integration.app_id or "").strip():
        missing.append("AppId")
    if not (integration.app_secret or "").strip():
        missing.append("AppSecret")
    if not (integration.account or "").strip():
        missing.append("平台账号/商户用户ID")
    if not (integration.pay_password_md5 or "").strip():
        missing.append("支付密码")
    if missing:
        raise DailiantongPushError(
            "代练通账号的配置不完整（缺：" + "、".join(missing) + "）。"
            "请到「交易平台对接设置」补齐后再发单。"
        )
    return integration


def _open_client(integration: PlatformIntegration) -> DailiantongClient:
    """把配置行变成客户端。**测试替换点**：单测 patch 本函数即可完全避开真实网络。"""
    try:
        config = DailiantongConfig(
            app_id=integration.app_id,
            app_secret=integration.app_secret,
            user_id=integration.account,
            pay_password_md5=integration.pay_password_md5,
            base_url=dailiantong_base_url(DEFAULT_BASE_URL),
        )
    except ValueError as exc:
        # 配置期校验（md5 形状 / https / 空值）——消息里只有字段名，没有凭据值。
        raise DailiantongPushError(f"代练通账号配置不可用：{exc}") from exc
    return DailiantongClient(config)


def _close(client) -> None:
    """尽力关闭客户端（测试桩可能没有 `close`）。"""
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _check_active_balance(client, *, need: Decimal) -> None:
    """第 5 道闸：`ActiveBal >= need`，不足时给「差多少」。A1 实测：发单只冻 `Price`（代练费；`Price=2+双金1/1`
    实测我方冻结只 +2.00），两份保证金是打手侧要求、不占发单方余额 ⇒ `need` 只等于 price（`push_order`/`modify_push_price`）。
    ⛔ 不要把 `ensure1+ensure2` 加回 `need`：会让余额够发 2 元单的账号被误判不足、文案说出与实测相反的「保证金合计」。"""
    money = client.user_money()
    if not isinstance(money, dict) or money.get("ActiveBal") is None:
        raise DailiantongPushError(
            "代练通没有返回可识别的可用余额，暂时无法发单。请稍后重试或到代练通后台核对账户。"
        )
    try:
        active = _to_decimal(money["ActiveBal"])
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError(
            "代练通返回的可用余额不是有效金额，暂时无法发单。请稍后重试或到代练通后台核对账户。"
        ) from exc
    if active < need:
        raise DailiantongPushError(
            f"代练通账户可用余额不足：这次需要 {_fmt_money(need)} 元（代练费；"
            "两份保证金由代练通在打手侧收取，不占我方余额），"
            f"当前可用 {_fmt_money(active)} 元，还差 {_fmt_money(need - active)} 元。"
            "请先到代练通充值后再发单。"
        )


def _existing_active_push(order, platform: int) -> DailiantongPush | None:
    """该单在该平台仍算在跑的最新推送；已收口的远端终态行不算。真机 bug：`sent`+`remote_status=16`+`remote_cancel_status=12`
    （撤销已完成）+`is_active=False` 仍被当成「挡着重发」⇒ 判据 = `state∈ACTIVE_STATES` 且远端未收口（`Status=17` 或
    `CancelStatus∈{12,15,16}` 已结束）；⚠️ `Status=16`+`CancelStatus=10` 仍算在跑、⛔ 不放行。只读；落 `closed` 的是 `finalize_finished_pushes` 与一次性修数命令。"""
    return (
        DailiantongPush.objects.filter(
            order=order,
            platform=platform,
            state__in=DailiantongPush.ACTIVE_STATES,
        )
        # 远端已收口的两类：已结算 / 撤销已完成。
        .exclude(remote_status=17)
        .exclude(remote_cancel_status__in=(12, 15, 16))
        .order_by("-id")
        .first()
    )


def finalize_finished_pushes(order, *, platform: int = DEFAULT_PLATFORM) -> int:
    """重抛前收口已撤销的推送，以及本地订单已有终态的已结算推送。"""
    qs = DailiantongPush.objects.filter(
        order=order,
        platform=platform,
        state__in=DailiantongPush.ACTIVE_STATES,
    ).filter(
        Q(remote_status=17)
        | Q(remote_cancel_status__in=(12, 15, 16))
    )
    if order.status not in LOCAL_TERMINAL_STATUSES:
        # 已结算但本地尚未完成的推送交给回写收口，重抛不能提前关闭它。
        qs = qs.exclude(remote_status__in=TERMINAL_REMOTE_STATUSES)
    closed = 0
    for push in qs.order_by("id"):
        if push.remote_status == 17:
            reason = "远端已结算（Status=17），这条推送已收口"
        else:
            reason = (
                f"远端已撤销完成（CancelStatus={push.remote_cancel_status}），"
                "这条推送已收口"
            )
        push.state = DailiantongPush.State.CLOSED
        push.is_active = False
        push.remote_note = _clip_note(reason)
        push.save(
            update_fields=["state", "is_active", "remote_note", "updated_at"]
        )
        closed += 1
    return closed


def cancel_application_submitted(push) -> bool:
    """D8：这条推送的撤销申请已提交（重发闸判据）。证据优先级 ⛔ 不猜：本地 `cancel_requested_at` 非空首选（可审计）；
    否则远端 `remote_cancel_status == 11`（协商撤销中的事实，历史行也能放行，放行时补记本地时刻）。`CancelStatus=11` 有
    我方/打手两种发起方，本函数只判「撤销在途」、不分发起方；打手发起档可否重发属待确认口径，⛔ 不在此收紧/放宽。"""
    if push is None:
        return False
    # ⭐ C1：本地 `cancel_requested_at` **单独不算数**——远端撤销可能
    # 被取消（真机 16/11 → 12/10），本地列没清就会放行重发 ⇒ 第二张真钱单。
    # 只有远端仍在途（`CancelStatus ∈ {11,14}`）才允许重发；回读看到 10/13 时会清本地标记。
    return push.remote_cancel_status in CANCEL_STATUS_IN_FLIGHT


def cancel_application_submitted_for_order(
    order, *, platform: int = DEFAULT_PLATFORM
) -> bool:
    """该单最新一条有效推送是否已提交撤销申请（D8 放宽判据）。⛔ 只看最新一条：
    重发出的新行还没有撤销申请，不能拿旧行的撤销记录给第二、三次重发当通行证。
    """
    return cancel_application_submitted(_existing_active_push(order, platform))


#: 代练通原话（真机，`docs/DAILIANTONG-API.md` §6.9）：转入协商撤销状态
#: **1 小时后**才允许申请仲裁介入；其它状态一律回下面这句。
ARBITRATION_MIN_WAIT = timedelta(hours=1)
ARBITRATION_UPSTREAM_REFUSAL = "订单异常，无法申请仲裁介入"


def arbitration_availability(
    push,
    *,
    cancel_status: int | None = None,
    now=None,
) -> tuple[bool, str]:
    """「申请客服介入」能不能点 + 不能点的中文原因（⛔ 只读、不打上游、不写库）。真机定案：只有「协商撤销中 `CancelStatus=11`」
    且转入满 1 小时才允许，其余上游一律回「订单异常，无法申请仲裁介入」；`cancel_status` 缺省用本地快照（读面按钮提示，动作端点必须传当次详情回读值）。
    判据：非 11 ⇒ 不可用（带上游原话）；11 无 `cancel_requested_at` ⇒ 放行给上游判（⛔ 不替上游永久禁用）；有时刻 ⇒ 满 1 小时才可用，未满回「还需 X 分钟」。"""
    if cancel_status is None:
        cancel_status = push.remote_cancel_status
    if cancel_status is None:
        return False, "还没有回读到代练通的状态，请先同步这张单再申请客服介入。"
    if cancel_status != CANCEL_STATUS_NEGOTIATING:
        return False, (
            "当前状态不能申请客服介入（代练通原话："
            f"{ARBITRATION_UPSTREAM_REFUSAL}）。"
            "代练通只允许在「协商撤销中」且转入该状态满 1 小时后申请客服介入。"
        )
    requested_at = push.cancel_requested_at
    if requested_at is None:
        # 本地没记到时刻（打手侧发起 / 历史行）⇒ 算不出满没满，交上游判。
        return True, ""
    elapsed = (now or timezone.now()) - requested_at
    if elapsed >= ARBITRATION_MIN_WAIT:
        return True, ""
    remaining = ARBITRATION_MIN_WAIT - elapsed
    # 向上取整到分钟：剩 1 秒也要说「还需 1 分钟」（⛔ 不显示 0 分钟）。
    minutes = max(1, int((remaining.total_seconds() + 59) // 60))
    return False, f"转入协商撤销状态还不满 1 小时，还需 {minutes} 分钟才能申请客服介入。"


def cancel_initiator_from_detail(detail: dict) -> str:
    """从详情回包判定「最近一次撤销协商发起方」；判不了 ⇒ 空串（判据表见 `CANCEL_INITIATOR_TAKER` 处）。
    `CancelUID` 空/缺 ⇒ `""`；== `CreateUID` ⇒ `"us"`；== `AcceptUID` ⇒ `"taker"`；都不等 ⇒ `""`（⛔ 不猜）。先判我方
    （同时等于两个 UID 时按我方处理更保守）；UID 比较用 `casefold()` 防上游大小写不稳定让判定静默失效。"""
    if not isinstance(detail, dict):
        return ""
    cancel_uid = _detail_text(detail, "CancelUID", "ApplyUser").casefold()
    if not cancel_uid:
        return ""
    create_uid = _detail_text(detail, "CreateUID", "CreateUserID").casefold()
    accept_uid = _detail_text(detail, "AcceptUID", "AcceptUserID").casefold()
    if create_uid and cancel_uid == create_uid:
        return CANCEL_INITIATOR_US
    if accept_uid and cancel_uid == accept_uid:
        return CANCEL_INITIATOR_TAKER
    return ""


def agree_cancel_availability(push) -> bool:
    """「同意撤销」按钮能不能点（⛔ 只读）。判据 = `CancelStatus=11` 且发起方是打手（`cancel_initiator=="taker"`，
    见 `cancel_initiator_from_detail`）。两个条件缺一不可：撤销结束后 `CancelUID` 不清理 ⇒ 光看发起方会给已无可同意的按钮；
    我方自己发起的协商也是 11、「同意」语义不同 ⇒ 必须是 taker，判不了不给按钮。只作按钮提示，放行以动作端点当次详情回读为准。"""
    if push is None:
        return False
    if _as_int(push.remote_cancel_status) != CANCEL_STATUS_NEGOTIATING:
        return False
    return push.cancel_initiator == CANCEL_INITIATOR_TAKER


def _latest_unsuperseded_push(order, platform: int) -> DailiantongPush | None:
    return (
        DailiantongPush.objects.filter(order=order, platform=platform)
        .exclude(state=DailiantongPush.State.FAILED)
        .order_by("-id")
        .first()
    )


def authoritative_push(
    order, *, platform: int = DEFAULT_PLATFORM, applying_settlement=False
) -> DailiantongPush | None:
    """已结算的推送只有未被重发接管时才能完单；其余状态优先听最新未结束推送。"""
    if applying_settlement:
        return _latest_unsuperseded_push(order, platform)
    active_rows = list(
        DailiantongPush.objects.filter(
            order=order,
            platform=platform,
            state__in=(DailiantongPush.State.SENT, DailiantongPush.State.PENDING),
        ).order_by("-id")
    )
    if (
        active_rows
        and active_rows[0].state == DailiantongPush.State.SENT
        and active_rows[0].remote_status in TERMINAL_REMOTE_STATUSES
    ):
        return active_rows[0]

    sent_rows = [push for push in active_rows if push.state == DailiantongPush.State.SENT]
    for push in sent_rows:
        if (
            push.remote_status in TERMINAL_REMOTE_STATUSES
            or push.remote_cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
        ):
            continue
        return push
    pending = next((push for push in active_rows if push.state == DailiantongPush.State.PENDING), None)
    return pending or (sent_rows[0] if sent_rows else None)


def _raise_if_already_pushed(order, platform: int) -> DailiantongPush | None:
    """第 4 道闸：同单同平台已有有效推送时拒绝（库层部分唯一索引兜底）。P4：文案必须带已有平台单号+「已抛过」字样
    （按钮常驻后客服要能一眼看出单号）。D8：已提交协商撤销的旧推送 ⇒ 放行并返回它（新行记 `replaces_push`）；远端已被接手
    未申请撤销 ⇒ 用业务方原话拒。H7：`Status=18` 不算已接手 ⇒ 落「已抛过」句（⛔ 不说「已被接手」）但仍拒（重发会造第二张单）。"""
    if order.status not in LOCAL_TERMINAL_STATUSES:
        latest = _latest_unsuperseded_push(order, platform)
        if (
            latest is not None
            and latest.state == DailiantongPush.State.SENT
            and latest.remote_status in TERMINAL_REMOTE_STATUSES
        ):
            raise DailiantongPushError("代练通已结算，本地正在同步完单，请先同步交易状态。")
    existing = _existing_active_push(order, platform)
    if existing is None:
        return None
    serial = (existing.serial_no or "").strip()
    serial_text = f"已有平台单号：{serial}" if serial else "已有平台单号：未记录"
    if existing.state == DailiantongPush.State.SENT:
        if cancel_application_submitted(existing):
            # ⭐ D8：撤销申请已提交 ⇒ 放行重发。⚠️ 本函数在**预览**（GET）也会被调用
            # ⇒ 这里**不写库**；历史行只有远端 11 时，由 `push_order` 在真的重发前补记。
            return existing
        if remote_is_accepted(existing):
            # ⭐ M3：16/10 是「撤销中」而不是「刚被接手」，
            # 不能再让客服去「申请撤销」（已经在撤了）。
            if existing.remote_status == REMOTE_STATUS_CANCELLING:
                raise DailiantongPushError(
                    "这张单已在代练通「撤销中」（Status=16），撤销完成前不能重发；"
                    "如撤销卡住，请用「取消撤销」或等满 1 小时后申请客服介入。"
                    f"（{serial_text}）"
                )
            # 业务方原话（⛔ 不要改成别的措辞）。
            raise DailiantongPushError(
                "当前订单已被代练通接手，如要重发请先申请撤销。"
                f"（{serial_text}）"
            )
        raise DailiantongPushError(
            f"这张单已抛过代练通（{serial_text}），不能重复抛单。"
        )
    if existing.state == DailiantongPush.State.UNKNOWN:
        raise DailiantongPushError(
            "这张单已抛过代练通，但上次状态未知（可能已经发出去了；"
            f"{serial_text}），请先人工核对代练通后台记录，确认没有成功后再处理。"
        )
    raise DailiantongPushError(
        "这张单已抛过代练通（有一条抛单正在处理中，"
        f"{serial_text}），请稍后刷新状态，不要重复发单。"
    )


# ── 本地状态闸 / 并发抢单（计划 §2 / §4）──────────────────────────────


def taker_of(order):
    """该单 MAIN 绩效行的当前接手人；`None`=还没人接。「有没有接手人」的权威判据就是这条 MAIN 行的 `user`
    （与 `claim_order` / `order_status.taker_present_pool_entry_denial` 同源），⛔ 不要拿 `claimed_at` 反推。
    """
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    return perf.user if perf is not None else None


def normal_taker_of(order):
    """该单的**真实**接手人：占位账号「代练通」不算（它代表代练通侧的打手）。"""
    taker = taker_of(order)
    if taker is None or (taker.username or "") == VIRTUAL_TAKER_USERNAME:
        return None
    return taker


def push_locks_local_status(order, *, platform: int = DEFAULT_PLATFORM) -> DailiantongPush | None:
    """该单是否有一条仍算在平台跑的推送行；有就返回它。M2：判据统一为 `state=sent` 且 `is_active=True`（与前端
    `hasActivePlatformPush` 同一把尺子）：终态行被 finalize 后不锁；`pending`/`failed` 不算，`unknown` 无单号不锁（人工核对
    标记失败是出口）。回读连续失败 5 次停轮询（非终态、`is_active=False`）也放开本地状态——L5 已知劈叉，按前端同判据统一，原因在 `last_error`。"""
    return (
        DailiantongPush.objects.filter(
            order_id=order.pk,
            platform=platform,
            state=DailiantongPush.State.SENT,
            # ⭐ M2：与前端 `is_active` 同判据（终态行被 finalize 后不再锁本地状态）。
            is_active=True,
        )
        .order_by("-id")
        .first()
    )


def local_status_locked_denial(
    order, *, platform: int = DEFAULT_PLATFORM
) -> str | None:
    """A2（Rev.9 §2）：已抛到代练通的单禁止手工改状态，返回中文原因/`None`。业务方原话「状态必须与代练通一致」⇒
    单子在代练通活着时本地状态只能由同步回写。判定入口在状态机唯一写入口 `apply_transition` ⇒ 覆盖所有改状态路径；
    机器路径（同步自己）与「内部打手先接」（Rev.9 §4）用显式开关放行；本函数只读、不建用户、不打上游。"""
    push = push_locks_local_status(order, platform=platform)
    if push is None:
        return None
    serial = (push.serial_no or "").strip() or "（未记录）"
    return (
        f"这张单已经抛到代练通（上游单号：{serial}），"
        "本地状态由代练通同步决定，不能手工修改；"
        "请在代练通后台操作，同步（有回调时即时，兜底约 1 小时一轮）会把状态回写过来。"
    )


def remote_is_accepted(push: DailiantongPush | None) -> bool:
    """远端是否已有打手接手（Rev.9 §4「代练通优先」判据）。任一成立即「已接手」：`Status` 不在 {10,11,18}
    （18=没在卖没打手，真机，见 `REMOTE_STATUS_NOT_ACCEPTED`）；或 `CancelStatus` 落在接手后才会出现的撤销档
    （不受 `Status=18` 影响——撤销已动起来说明确实有人接过）。⚠️ 未回读（`remote_status is None`）不算：⛔ 不因「不知道」拒内部打手。"""
    if push is None or push.state != DailiantongPush.State.SENT:
        return False
    if (
        push.remote_status is not None
        and push.remote_status not in REMOTE_STATUS_NOT_ACCEPTED
    ):
        return True
    return (
        push.remote_cancel_status is not None
        and push.remote_cancel_status in CANCEL_STATUS_IMPLIES_ACCEPTED
    )


def platform_taken_denial(
    order, *, platform: int = DEFAULT_PLATFORM
) -> str | None:
    """远端已被代练通接单 ⇒ 本单不许再指派/抢单（业务方新口径），返回中文拒绝/`None`。`/assign/`（含批量）、大厅抢单、
    `/throw-to-hall/`（另一句文案）三入口共用同一判据与文案；「已接手」唯一出处=`remote_is_accepted()`。只抛未接（11/18/未回读）
    不拒：内部照旧指派、成功后 `withdraw_for_internal_claim` 撤平台单。⛔ 不走「先指派再自动协商撤销」（回读会换回占位打手，语义矛盾）。"""
    push = _existing_active_push(order, platform)
    if not remote_is_accepted(push):
        return None
    who = (push.remote_taker_name or push.remote_taker_uid or "").strip() or "代练通打手"
    return (
        f"这张单已被代练通接单（接单人：{who}），不能再指派；"
        "要换人请先在代练通协商撤销"
    )


def claim_blocked_by_platform_denial(
    order, *, platform: int = DEFAULT_PLATFORM
) -> str | None:
    """A4（Rev.9 §4 第 1 条）：代练通先被接 ⇒ 内部大厅抢单直接拒绝，返回中文原因/`None`。只在「有有效推送且远端已接手」
    时拒：只抛未接（11）与 18 已下架同族（`REMOTE_STATUS_NOT_ACCEPTED`）不拒，由 `reconcile_internal_claims` 撤代练通单（§4 第 2 条）。
    文案与 `/assign/` 统一（`platform_taken_denial` 同一句话），判据本体只保留一处。"""
    return platform_taken_denial(order, platform=platform)


def hall_throw_blocked_by_platform_denial(
    order, *, platform: int = DEFAULT_PLATFORM
) -> str | None:
    """D4（fx-D）：远端已接手 ⇒ 本地不能再抛大厅（否则同步滞后时两个打手同接一张单）。判据与 A2 同一份实现
    （⛔ 不在别处再写「什么叫已接手」）：有有效推送（`ACTIVE_STATES` 且 `is_active=True`）且 `remote_is_accepted()`；
    撤销申请已提交 ⇒ 允许重抛大厅（D8：两条链路一起重来）；只抛未接（11）/18 已下架不挡。返回中文原因/`None`。"""
    push = _existing_active_push(order, platform)
    if push is None or not push.is_active:
        return None
    if cancel_application_submitted(push):
        # ⭐ D8（业务方口径）：撤销申请已提交 ⇒ **允许重新抛大厅**
        # （重发是「代练通 + 大厅」两条一起重来）。旧推送仍照旧轮询。
        return None
    if not remote_is_accepted(push):
        return None
    who = (push.remote_taker_name or push.remote_taker_uid or "").strip() or "代练通打手"
    return (
        f"这张单已被代练通接单（接单人：{who}），不能再抛大厅。"
        "请先在代练通处理这张单（或等同步把状态回写过来）。"
    )


def _clip_note(text: str) -> str:
    """按 `remote_note` 列宽截断（⛔ 不靠数据库 `DataError`）。"""
    return (text or "")[:REMOTE_NOTE_MAX_LENGTH]


# ── 占位接单人「代练通」（Rev.9 §2 / Q1 的落地：每租户一个虚拟账号）──────


def _is_placeholder_taker(user: User) -> bool:
    return (
        user.username == VIRTUAL_TAKER_USERNAME
        and not user.is_active
        and not user.has_usable_password()
    )


def placeholder_taker_user() -> User:
    """本租户的占位接单人「代练通」：不可登录、自动创建/复用（业务方拍板=计划 §2 Q1：每租户一个虚拟账号）。
    `is_active=False`+unusable password ⇒ 任何登录入口进不来；存在同名且可登录的账号 ⇒ 响亮失败（同步记 `last_error` 等人工处理），
    ⛔ 绝不停用/清密真人账号。旧名账号（「代练通打手」）就地改 `first_name` 复用；判据只认 username，⛔ 不按显示名找、不建第二个。"""
    user, created = User.objects.get_or_create(
        username=VIRTUAL_TAKER_USERNAME,
        defaults={
            "first_name": VIRTUAL_TAKER_DISPLAY_NAME,
            "is_active": False,
        },
    )
    if created:
        user.set_unusable_password()
        user.is_active = False
        user.save(update_fields=["password", "is_active"])
        return user
    if not _is_placeholder_taker(user):
        raise DailiantongPushError(
            f"本租户已有一个可登录的账号叫「{VIRTUAL_TAKER_USERNAME}」，"
            "它不是代练通的占位账号，本系统不能复用它、也不能停用它。"
            "请管理员给那个账号改名或停用，再让同步重试。"
        )
    # ⭐旧名账号（历史上叫「代练通打手」）就地改名复用。
    # 判据只认 username；显示名是**可改的产物**，⛔ 不是识别账号的依据。
    if user.first_name != VIRTUAL_TAKER_DISPLAY_NAME:
        user.first_name = VIRTUAL_TAKER_DISPLAY_NAME
        user.save(update_fields=["first_name"])
    return user


def _ensure_placeholder_profile(user, order) -> None:
    """给占位账号补档案 + 本单游戏的接单权限。⛔ 必须建档案：D-0252 的游戏范围闸按 `games_of(taker)` 判，
    没有档案=空集=拒；占位账号语义是「代练通侧的打手」⇒ 能接任何游戏，每用到新游戏就补进去（只加不删）。
    """
    from apps.accounts.models import StaffProfile

    profile = StaffProfile.objects.filter(user=user).first()
    if profile is None:
        profile = StaffProfile.objects.create(user=user)
    if order.game_id is not None and not profile.games.filter(pk=order.game_id).exists():
        profile.games.add(order.game_id)


def _display_name(user) -> str:
    """账号的人话名字（昵称优先、空串回落登录账号；唯一出处 `accounts.display`）。"""
    from apps.accounts.display import user_display_name

    return user_display_name(user)


def _status_label(status) -> str:
    """状态码的中文名（认不出就原样给码，⛔ 不编一个不存在的状态名）。"""
    return dict(OrderStatus.choices).get(str(status), str(status))


def _machine_nodes_for(target) -> frozenset[str]:
    """机器路径（外部平台驱动）写状态用的节点集 = 该目标态的合法可达节点并集。不是给人类端点的旁路：
    只被代练通同步（及其驱动的 claim/assign）调用，人类端点仍走 `nodes_of(user)`。授权判据本体在状态机
    （`transition_denial`），这里只如实声明「系统代外部事实执行」时的节点，⛔ 不在别处复制这份逻辑。"""
    from apps.business.order_status import machine_target_nodes

    return machine_target_nodes(target)


# ── 远端字段落库 + 远端状态 → 本地状态回写（Rev.9 §2）──────────────────────


def _detail_text(detail: dict, *names: str) -> str:
    """在详情回包里取一串文本（键名大小写不保证；取不到 / 空 ⇒ 空串）。"""
    found, raw = _detail_field(detail, *names)
    if not found or raw is None:
        return ""
    return str(raw).strip()


def _detail_decimal(detail: dict, *names: str) -> Decimal | None:
    """在详情回包里取一个金额：取不到 / 空串 / 解析不出 ⇒ `None`（⛔ 不写 0 冒充）。"""
    found, raw = _detail_field(detail, *names)
    if not found or raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return _to_decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _detail_int(detail: dict, *names: str) -> int | None:
    """在详情回包里取一个整数：取不到 / 空串 / 解析不出 ⇒ `None`（⛔ 不写 0 冒充）。"""
    found, raw = _detail_field(detail, *names)
    if not found or raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_remote_datetime(value):
    """把上游时间字符串（`2015-11-26 23:09:40`）解析成带时区的 datetime；解析不出 ⇒ `None`
    （⛔ 不拿「回读时刻」冒充接单时刻，那会让页面说假话）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    parsed = dateparse.parse_datetime(text)
    if parsed is None:
        day = dateparse.parse_date(text)
        if day is None:
            return None
        parsed = timezone.datetime.combine(day, timezone.datetime.min.time())
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def store_remote_fields(
    push: DailiantongPush, detail: dict, *, status=None, cancel_status=None
) -> tuple[str, ...]:
    """把回读到的远端多字段落到推送行，返回实际变了的列名。A2 存储面（Rev.9 §2）：接单人 UID/昵称、接单时间、撤销状态/原因；
    h13 另写 `cancel_initiator`（见 `cancel_initiator_from_detail`）；`CancelReason`/`CancelCmt` 分开落库（后者缺失退回前者）；
    `PayLevelBal`/`RepEnsureBal` 落「已结」两格原始值（读面只在 16/17 下发）；`last_synced_at` 由调用方写；⚠️ 详情没回传的字段一律保留旧值（⛔ 不用空值擦掉已记事实）。"""
    changed: list[str] = []

    def _set(field: str, value) -> None:
        if value is None:
            return
        if getattr(push, field) == value:
            return
        setattr(push, field, value)
        changed.append(field)

    if status is not None:
        _set("remote_status", status)
    if cancel_status is not None:
        _set("remote_cancel_status", cancel_status)

    uid = _detail_text(detail, "AcceptUID", "AcceptUserID")
    if uid:
        _set(
            "remote_taker_uid",
            uid[: DailiantongPush._meta.get_field("remote_taker_uid").max_length],
        )
    name = _detail_text(detail, "AcceptUserName")
    if name:
        _set(
            "remote_taker_name",
            name[: DailiantongPush._meta.get_field("remote_taker_name").max_length],
        )
    accepted_at = _parse_remote_datetime(
        _detail_text(detail, "StartDate", "AcceptDate")
    )
    if accepted_at is not None:
        _set("remote_accepted_at", accepted_at)
    # ⭐`CancelReason` 与 `CancelCmt` **分开存**（读面分别下发 `cancel_reason` /
    # `cancel_cmt`）。`CancelCmt` 是上游拼好的「撤销要求」原文（含支出金额），
    # 客服点「同意撤销」时要原样看到；详情**没有** `CancelCmt` 键时才退回 `CancelReason`。
    cancel_reason = _detail_text(detail, "CancelReason")
    if cancel_reason:
        _set(
            "remote_cancel_reason",
            cancel_reason[
                : DailiantongPush._meta.get_field("remote_cancel_reason").max_length
            ],
        )
    found_cmt, raw_cmt = _detail_field(detail, "CancelCmt")
    if found_cmt:
        # 键存在（哪怕值是空串）⇒ 以它为准：空串也写回，⛔ 不拿旧的文案冒充「这次的要求」。
        cancel_cmt = "" if raw_cmt is None else str(raw_cmt).strip()
        _set(
            "remote_cancel_cmt",
            cancel_cmt[
                : DailiantongPush._meta.get_field("remote_cancel_cmt").max_length
            ],
        )
    elif cancel_reason:
        # 上游没这个键 ⇒ 按业务方口径退回 `CancelReason`。
        _set(
            "remote_cancel_cmt",
            cancel_reason[
                : DailiantongPush._meta.get_field("remote_cancel_cmt").max_length
            ],
        )
    # ⭐ 业务方）：详情页「已结代练费/已赔保证金」两格的原始值；读面只在 Status 16/17 下发（见 `settled_amounts_for_push`）。
    # 键存在但值空/解析不出 ⇒ 写 `None`（清旧值，⛔ 不用旧金额冒充）；键不存在 ⇒ 保留旧值（「缺失不擦」口径）。
    for field, names in (
        ("remote_pay_level_bal", ("PayLevelBal",)),
        ("remote_rep_ensure_bal", ("RepEnsureBal",)),
    ):
        found, _raw = _detail_field(detail, *names)
        if not found:
            continue
        value = _detail_decimal(detail, *names)
        if getattr(push, field) != value:
            setattr(push, field, value)
            changed.append(field)
    # ⭐ 业务方）：打手完成单数 / 投诉取消次数（详情原值；缺键保留旧值，
    # 键在但空 / 坏值 ⇒ 写 None，读面给 null，⛔ 不用 0 冒充）。
    for field, names in (
        ("accept_over_count", ("AcceptOverCount",)),
        ("cust_cancel_count", ("CustCancelCount",)),
    ):
        found, _raw = _detail_field(detail, *names)
        if not found:
            continue
        value = _detail_int(detail, *names)
        if getattr(push, field) != value:
            setattr(push, field, value)
            changed.append(field)
    # ⭐ 业务方）：本地验收倒计时起点——**首次进入 13** 记 now；离开 13 清空。
    # ⚠️ 只认这次回读到的 `Status`（`status is None` = 上游没给，⛔ 不据此清）。
    if status is not None:
        if status == REMOTE_STATUS_WAITING_ACCEPTANCE:
            if push.remote_verify_started_at is None:
                push.remote_verify_started_at = timezone.now()
                changed.append("remote_verify_started_at")
        elif push.remote_verify_started_at is not None:
            push.remote_verify_started_at = None
            changed.append("remote_verify_started_at")
    # ⭐ h13：撤销协商发起方（读面 `cancel_initiator` / 「同意撤销」按钮）。
    # ⚠️ 只在**判得出来**时写：`CancelUID` 空 / 与两个 UID 都不等 ⇒ 保留旧值（不要用空值
    # 擦掉已经观测到的事实）；判得出来就覆盖（可能是打手→我方的新一轮协商）。
    initiator = cancel_initiator_from_detail(detail)
    if initiator:
        _set("cancel_initiator", initiator)
    # ⭐ C1：远端撤销已取消（10/13）⇒ 清本地撤销标记（否则重发闸会被陈旧标记放行）。
    for field in _clear_stale_cancel_fields(push):
        if field not in changed:
            changed.append(field)
    return tuple(changed)


def _clear_stale_cancel_fields(push) -> list[str]:
    """远端撤销已取消（`CancelStatus=10/13`）⇒ 清本地撤销标记/自动仲裁待办；返回实际改动列名（调用方负责 save）。
    ⭐ C1：`cancel_requested_at` 是重发闸/抛大厅/放宽状态闸的依据，撤销被取消必须跟着清，否则同单出第二张单。
    ⭐ H2：已有 NULL 在途行时不硬清标——清回 NULL 会让本行重新落进 `uniq_dailiantong_push_active` 部分唯一索引、与新行撞 `IntegrityError`（行被当回读失败永久停摆）⇒ 本行收 `closed`+写 `remote_note`、保留撤销标记（撞单 0 撤仍收它当候选）；自动仲裁列与索引无关、照旧清。"""
    if push.remote_cancel_status not in (CANCEL_STATUS_NOT_CANCELLED, 13):
        return []
    # ⭐ H2：同一 (order, platform) 是否已有另一条**在索引里**的在途行（NULL 标记）。
    #    `ACTIVE_STATES` 与 `uniq_dailiantong_push_active` 的 `state__in` 是同一集合。
    sibling = (
        DailiantongPush.objects.filter(
            order_id=push.order_id,
            platform=push.platform,
            state__in=DailiantongPush.ACTIVE_STATES,
            cancel_requested_at__isnull=True,
        )
        .exclude(pk=push.pk)
        .order_by("-id")
        .first()
    )
    fields: list[str] = []
    if sibling is not None:
        # 不硬清 `cancel_requested_at`（见 docstring）；只清自动仲裁待办 + 收口本行。
        if push.auto_arbitrate:
            push.auto_arbitrate = False
            fields.append("auto_arbitrate")
        if push.auto_arbitrate_attempts:
            push.auto_arbitrate_attempts = 0
            fields.append("auto_arbitrate_attempts")
        if push.auto_arbitrate_next_at is not None:
            push.auto_arbitrate_next_at = None
            fields.append("auto_arbitrate_next_at")
        if push.auto_arbitrate_note:
            push.auto_arbitrate_note = ""
            fields.append("auto_arbitrate_note")
        if push.remote_status in TERMINAL_REMOTE_STATUSES:
            # 保留旧行的撤销标记，等新推送结果明确后再处理这次结算。
            return fields
        push.state = DailiantongPush.State.CLOSED
        push.is_active = False
        push.remote_note = _clip_note(
            "回读显示撤销申请已被取消（CancelStatus="
            f"{push.remote_cancel_status}），但同一订单在同一平台已有一条在途推送"
            f"（推送 #{sibling.pk}）。这条旧行收口为 closed，且**保留**本地撤销标记"
            "（若清回空会与在途行撞部分唯一索引）；远端这张单可能仍活着，"
            "撞单 0 撤会把它当候选处理，也可到代练通后台人工核对"
        )
        fields.extend(["state", "is_active", "remote_note"])
        return fields
    if push.cancel_requested_at is not None:
        push.cancel_requested_at = None
        fields.append("cancel_requested_at")
    if push.auto_arbitrate:
        push.auto_arbitrate = False
        fields.append("auto_arbitrate")
    if push.auto_arbitrate_attempts:
        push.auto_arbitrate_attempts = 0
        fields.append("auto_arbitrate_attempts")
    if push.auto_arbitrate_next_at is not None:
        push.auto_arbitrate_next_at = None
        fields.append("auto_arbitrate_next_at")
    if push.auto_arbitrate_note:
        push.auto_arbitrate_note = ""
        fields.append("auto_arbitrate_note")
    if fields:
        push.remote_note = _clip_note(
            "回读显示撤销申请已被取消（CancelStatus="
            f"{push.remote_cancel_status}），已清除本地撤销标记与自动仲裁待办"
        )
        fields.append("remote_note")
    return fields


#: 验收倒计时默认 48h（业务方口径：代练通默认 48 小时，本地算、不同步上游）。取值链=本单配置 → 租户默认 → 本常量；
#: 当前两类配置都不存在 ⇒ 实际只走 48h，将来加配置时改 `verify_window_hours_for_push` 一处、读面不动。
DEFAULT_VERIFY_WINDOW_HOURS = 48
#: 兼容旧名（此前误用 24h 发布过一版；新代码统一读 `DEFAULT_VERIFY_WINDOW_HOURS`）。
VERIFY_DEADLINE_HOURS = DEFAULT_VERIFY_WINDOW_HOURS


def remaining_seconds_for_push(push, *, now=None) -> int | None:
    """「剩余时间」列：`remote_accepted_at + time_limit_hours` 还差多少秒（来源=发单提交的 `TimeLimit` + 详情
    `StartDate` 落库的 `remote_accepted_at`）。没接单时间/没时限 ⇒ `None`（前端画「—」，⛔ 不拿 0 或创建时刻冒充）；
    已过期 ⇒ 0（不把负数画成负时间）。"""
    accepted_at = push.remote_accepted_at
    limit = push.time_limit_hours
    if accepted_at is None or not limit:
        return None
    deadline = accepted_at + timedelta(hours=int(limit))
    return max(0, int((deadline - (now or timezone.now())).total_seconds()))


def verify_window_hours_for_push(push) -> int:
    """本单验收时限（小时）：本单配置 → 租户默认 → 48h。当前 `Order`/租户设置都没有验收时限字段（已全仓检索）
    ⇒ 永远回 48；保留逐级读取的形状，将来加列时只改这里、读面不动。"""
    for owner in (getattr(push, "order", None),):
        configured = getattr(owner, "verify_window_hours", None)
        if configured:
            try:
                value = int(configured)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return DEFAULT_VERIFY_WINDOW_HOURS


def verify_deadline_for_push(
    push, *, now=None
) -> tuple["datetime | None", int | None, bool]:
    """验收倒计时 `(终点, 剩余秒数, 是否已过期)`：起点=`remote_verify_started_at`（首次进入 13 由 `store_remote_fields` 记）；
    没有起点 ⇒ `(None, None, False)`；剩余=终点-now，过期给负数、`overdue=True`（业务方允许负或 0）。"""
    started = push.remote_verify_started_at
    if started is None:
        return None, None, False
    deadline = started + timedelta(hours=verify_window_hours_for_push(push))
    remaining = int((deadline - (now or timezone.now())).total_seconds())
    return deadline, remaining, remaining < 0


def settled_amounts_for_push(push) -> tuple[str | None, str | None]:
    """详情页「已结代练费 / 已赔保证金」读面值 `(pay, rep)` 两位小数字符串。上游没有独立「已结」字段，文档只有
    `PayLevelBal`/`RepEnsureBal` 且只在撤销上下文有意义 ⇒ 本仓落详情原值，读面只在 `Status∈{16,17}` 下发，
    其它给 `None`（前端「—」），避免把「申请撤销时想给的金额」当「已结金额」；解析不出 ⇒ `None`（⛔ 不写 `0.00` 冒充）。"""
    if _as_int(getattr(push, "remote_status", None)) not in (16, 17):
        return None, None
    pay = push.remote_pay_level_bal
    rep = push.remote_rep_ensure_bal
    return (
        _fmt_money(pay) if pay is not None else None,
        _fmt_money(rep) if rep is not None else None,
    )


def display_status_for_remote(status, cancel_status) -> tuple[str | None, str]:
    """远端 `(Status, CancelStatus)` → 读面综合状态 `(display_status, 中文 label)`；h1 真机 bug：只看 `Status` 会让「协商已处理」单一直显示「撤销中」⇒ 前端优先用综合状态。规则（业务方定案，⛔ 改顺序前先回去读）：`Status=17` 先判（否则「页面已撤销、本地已结算」矛盾）；`CancelStatus` 12/15→`revoked`、16→`force_revoked`、11→`cancelling`、14→`arbitrating`、13（撤销已取消）与其余→按 `Status`（10–18 各一档，18=已下架真机）；两码都没有/未知 ⇒ `(None,"")`（前端画「—」）。"""
    if status == 17:
        return "settled", DISPLAY_STATUS_LABELS["settled"]
    if cancel_status in CANCEL_STATUS_REVOKED:
        # 16 强制撤销与 12/15 的区别只体现在文字上（色档同为「已撤销」那一档）。
        if cancel_status == 16:
            return "force_revoked", DISPLAY_STATUS_LABELS["force_revoked"]
        return "revoked", DISPLAY_STATUS_LABELS["revoked"]
    if cancel_status in CANCEL_STATUS_IN_FLIGHT:
        # 11 协商撤销中 / 14 客服介入中：都**还没撤销完**，按具体码给中文（既不是已撤销、
        # 也不该被当成「按 Status 显示」的普通状态）。
        token = (
            "arbitrating"
            if cancel_status == CANCEL_STATUS_ARBITRATING
            else "cancelling"
        )
        return token, DISPLAY_STATUS_LABELS[token]
    # `13` 撤销已取消 / 10 未撤销 / 未知码 ⇒ 回到按 `Status` 显示。
    token = DISPLAY_STATUS_BY_REMOTE_STATUS.get(status) if status is not None else None
    if token is None:
        return None, ""
    return token, DISPLAY_STATUS_LABELS[token]


def local_status_for_remote(status, cancel_status):
    """远端 `(Status, CancelStatus)` → 本地 `OrderStatus`；`None`=不改本地状态（映射表=`REMOTE_STATUS_LOCAL_STATUS`，计划 §2+h1 口径）。⛔ 改优先级前先回去读业务方口径：`17` 先判（钱已结算不该被撤销码改回已退单）；`CancelStatus∈{12,15,16}` 是撤销已完成的事实（12=打手已同意、15=客服已处理，真机定案），即使 `Status` 还停在 12/13 也按已退单写——旧口径「看着还在做就不写」会让本地永远停在代练中（h1）；「本地已是另一终态就不来回改」在 `apply_remote_status` 判（要留原因）。"""
    if status == 17:
        return OrderStatus.COMPLETED
    if cancel_status in CANCEL_STATUS_REVOKED:
        return OrderStatus.REFUNDED
    return REMOTE_STATUS_LOCAL_STATUS.get(status)


def _apply_local_status(order, target, *, completing_dailiantong_settlement=False) -> str:
    """把本地订单状态写成 `target`（走唯一写入口 `apply_transition`），返回中文结论。机器节点集=`_machine_nodes_for(target)`；`allow_status_change_while_pushed=True`（本次写入就是同步，不受 A2 闸约束）；目标回池且接手人是占位账号 ⇒ `allow_taker_release=True`（远端说未接手时本地不该还挂占位接手人）；状态真变才写 `order.transition` 机器审计（`system=True`）。⚠️ `COMPLETED` 要走两步：状态机「已完成」只有 `PENDING_REVIEW→` 入边（D-0071），远端 17 而本地停在 12/11 时直接写会抛「非法状态迁移」⇒ 同一事务里先补 `PENDING_REVIEW`（也是远端真实走过的路）再写 `COMPLETED`。"""
    if str(order.status) == str(target):
        return ""
    from apps.business.order_status import apply_transition

    steps: list = [target]
    if (
        str(target) == str(OrderStatus.COMPLETED)
        and str(order.status) != str(OrderStatus.PENDING_REVIEW)
    ):
        steps = [OrderStatus.PENDING_REVIEW, target]

    origin_status = from_status = order.status
    current = taker_of(order)
    release = (
        str(target) in (str(OrderStatus.UNASSIGNED), str(OrderStatus.WAITING_GRAB))
        and current is not None
        and (current.username or "") == VIRTUAL_TAKER_USERNAME
    )
    with transaction.atomic():
        for step in steps:
            apply_transition(
                order,
                step,
                nodes=_machine_nodes_for(step),
                actor=SYNC_ACTOR,
                allow_taker_release=release,
                allow_status_change_while_pushed=True,
                completing_dailiantong_settlement=completing_dailiantong_settlement,
            )
            record(
                log_type=ACTION_LOG_TYPES["order.transition"],
                action="order.transition",
                actor=SYNC_ACTOR,
                actor_id=None,
                system=True,
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                field_name="status",
                from_value=_status_label(from_status),
                to_value=_status_label(step),
                reason="代练通远端状态回写（Rev.9 §2）",
            )
            from_status = step
    return (
        "本地状态已按代练通回写："
        f"{_status_label(origin_status)} → {_status_label(target)}"
    )


def _assign_placeholder_taker(order) -> str:
    """远端已被接 ⇒ 本地接手人必须是占位账号「代练通」；返回中文结论。走既有指派咽喉 `order_writes.assign_order`（⛔ 不直接写 `Performance.user`）：实名闸（D-0245）/游戏范围闸（D-0252，靠 `_ensure_placeholder_profile` 补档案）/已结账闸/七列回退/`set_taker` 重算全照走；两边同时接（Rev.9 §4 第 3 条）在此收口：真人接手时先 `reset_taker_performance`（回退七列：归属人/审核状态/生效绩效/覆盖价/接单时刻/时限/项目）再写占位账号，本地打手被撤销、绩效回退。FX2：远端已接但本地无发单价也照记人（`allow_missing_price=True`，接手人/时刻/冷静期照写，绩效金额留待 `/hall-price/` 补价重算、`remote_note` 说清）；⛔ 不把 `hall_price` 从代练通价抄过来——发单价给内部打手、代练通价给平台打手，是另一个价。"""
    current = taker_of(order)
    if current is not None and _is_placeholder_taker(current):
        return ""
    missing_price = order.hall_price is None

    taker = placeholder_taker_user()
    _ensure_placeholder_profile(taker, order)
    from apps.business import order_writes

    ctx = order_writes.WriteContext(
        actor=SYNC_ACTOR,
        log_actor=SYNC_ACTOR,
        log_actor_id=None,
        nodes=_machine_nodes_for(OrderStatus.IN_PROGRESS),
        user=None,
    )
    outcome = order_writes.assign_order(
        order,
        taker=taker,
        ctx=ctx,
        dry_run=False,
        allow_status_change_while_pushed=True,
        # ⭐ FX2：没有发单价时也把接手人记上（金额那一项留待补价）。
        allow_missing_price=missing_price,
    )
    if not outcome.ok:
        raise DailiantongPushError(
            f"把本地接手人记为「{VIRTUAL_TAKER_DISPLAY_NAME}」失败："
            + (outcome.msg or "未知原因")
        )
    parts: list[str] = []
    if current is not None:
        parts.append(
            f"代练通先被接走，本地接手人「{_display_name(current)}」已撤销"
            "（绩效同口径回退）"
        )
    parts.append(
        f"本地接手人 = {VIRTUAL_TAKER_DISPLAY_NAME}（占位账号，不可登录）"
    )
    if missing_price:
        parts.append(
            "这张单还没有「发单价」，绩效金额暂时算不出来；补上发单价后会自动补算"
        )
    return "；".join(parts)


def release_placeholder_taker_if_no_active_pushes(order) -> str:
    """代练通没有活跃订单后清掉占位接手人（业务方口径），返回中文结论。判据（全满足才动手）：无任何 `is_active=True` 推送（含其它平台）、接手人是占位账号（判据=固定 `username`，⛔ 不按显示名）、本地状态是 `REFUNDED` 或 `UNASSIGNED`（池内不变量 D-0032：池内不许挂接单人；重复回读会再写回占位人，要能再次收口）；已完成/代练中/待验收 ⛔ 不动。⭐ I1 只清人不动状态：⛔ 不走 `apply_transition(UNASSIGNED)`（会把已退单改写成未分配，而终态行不再轮询 ⇒ 永久少一单）——直接 `reset_taker_performance` 清 MAIN 行接单人（覆盖 user/review_status/bonus/taker_price/claimed_at/hours/project+待结算申请）、保留原状态；幂等（已空直接返回）；⚠️ 失败只落 `remote_note` 不抛（回读已成功，⛔ 不把同步算失败）。"""
    if order is None or getattr(order, "deleted_at", None) is not None:
        return ""
    if DailiantongPush.objects.filter(order=order, is_active=True).exists():
        return ""
    current = taker_of(order)
    if current is None or (current.username or "") != VIRTUAL_TAKER_USERNAME:
        return ""
    if str(order.status) not in (
        str(OrderStatus.REFUNDED),
        str(OrderStatus.UNASSIGNED),
    ):
        return ""

    from apps.business.performance import reset_taker_performance

    perf = (
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        .select_related("user")
        .first()
    )
    if perf is None:
        return ""
    former_name = _display_name(current)
    try:
        # ⭐ I1：只清绩效行的人，⛔ 不改订单状态（REFUNDED 保持 REFUNDED）。
        reset_taker_performance(perf)
    except Exception as exc:  # noqa: BLE001 —— 只记原因，⛔ 不把同步算失败
        return (
            f"{CLEAR_TAKER_REASON}失败：{str(exc) or type(exc).__name__}（请人工处理）"
        )

    record(
        log_type=ACTION_LOG_TYPES[AUDIT_CLEAR_TAKER],
        action=AUDIT_CLEAR_TAKER,
        actor=SYNC_ACTOR,
        actor_id=None,
        system=True,
        target_kind="user",
        target_id=str(current.pk),
        target_name=former_name,
        order_no=order.order_no,
        game_name=(order.game.name or "") if order.game_id else "",
        reason=CLEAR_TAKER_REASON,
    )
    return CLEAR_TAKER_REASON


def is_platform_settled_performance(performance: Performance) -> bool:
    """辨认代练通占位打手由平台结账的本地记录。"""
    return (
        performance.settle_status == SettleStatus.SETTLED
        and performance.settled_at is not None
        and performance.settled_by == ""
        and performance.settlement_method == ""
        and performance.online_payroll_batch_id is None
        and performance.user_id is not None
        and _is_placeholder_taker(performance.user)
    )


@transaction.atomic
def settle_performances_on_remote_settled(order) -> str:
    """平台已付款后只标记打单绩效结账，保留原有结账痕迹和客服结账状态。"""
    pending = Performance.objects.filter(
        order=order,
        emp_type=EmpType.MAIN,
        settle_status=SettleStatus.UNSETTLED,
    ).select_related("user").select_for_update(of=("self",)).order_by("pk")
    now = timezone.now()
    settled = 0
    for performance in pending:
        if performance.online_payroll_batch_id:
            raise DailiantongPushError("代练通已结算，且本地有在线付款记录，请人工核对")
        if performance.is_settled:
            # 脏行（状态列还是未结账、却留着 settled_at / settled_amount）：⛔ 不覆盖。
            logger.warning(
                "代练通已结算，但绩效行带着结账痕迹（不覆盖）performance=%s order=%s",
                performance.pk,
                order.order_no,
            )
            continue
        performance.settle_status = SettleStatus.SETTLED
        performance.settled_at = now
        performance.save(update_fields=["settle_status", "settled_at", "updated_at"])
        record(
            log_type=ACTION_LOG_TYPES["performance.settle"],
            action="performance.settle",
            actor=SYNC_ACTOR,
            actor_id=None,
            system=True,
            target_kind="user",
            target_id=performance.user_id,
            target_name=(
                _display_name(performance.user) if performance.user_id else ""
            ),
            order_no=order.order_no,
            field_name="settle_status",
            from_value=SettleStatus.UNSETTLED.label,
            to_value=SettleStatus.SETTLED.label,
            reason="代练通已结算 ⇒ 绩效置已结",
        )
        settled += 1
    if not settled:
        return ""
    return f"代练通已结算 ⇒ {settled} 条打单绩效已置为已结账"


@transaction.atomic
def apply_remote_status(push: DailiantongPush, *, order=None) -> str:
    """按当前权威推送回写本地状态；平台已结算时，完单与打单结账一并提交。"""
    if order is None:
        order = push.order
    if order is None or getattr(order, "deleted_at", None) is not None:
        return ""
    settling = push.remote_status in TERMINAL_REMOTE_STATUSES
    if settling:
        from apps.business.order_writes import lock_order_row

        # 手动验收和轮询可能同时读到已结算，拿锁后按最新状态判断。
        order = lock_order_row(order)
        if order is None or order.deleted_at is not None:
            return ""
        push.refresh_from_db(fields=["state"])
    if push.state != DailiantongPush.State.SENT:
        # `pending` / `failed` / `unknown` 没有远端事实；`closed` 的远端单已经删掉，
        # 都不该再驱动本地状态（否则一次撤回会被同步拉回去）。
        return ""
    authoritative = authoritative_push(
        order, platform=push.platform, applying_settlement=settling
    )
    if authoritative is None or authoritative.pk != push.pk:
        if (
            settling
            and authoritative is not None
            and authoritative.state in (DailiantongPush.State.PENDING, DailiantongPush.State.UNKNOWN)
        ):
            raise DailiantongPushError("新推送结果尚未确认，保留这条已结算推送等待同步")
        return ""

    target = local_status_for_remote(push.remote_status, push.remote_cancel_status)
    if push.platform_only and target == OrderStatus.WAITING_GRAB:
        target = None
    accepted = remote_is_accepted(push)
    notes: list[str] = []

    # 本地与远端终态冲突时先留痕，避免改变已收口的订单和接手人。
    if (
        str(target) in _LOCAL_TERMINAL_STATUS_NAMES
        and str(order.status) in _LOCAL_TERMINAL_STATUS_NAMES
        and str(order.status) != str(target)
    ):
        _, display_label = display_status_for_remote(
            push.remote_status, push.remote_cancel_status
        )
        outcome = "撤销已完成" if target == OrderStatus.REFUNDED else "已结算"
        return (
            f"代练通{outcome}（{display_label or _status_label(target)}），"
            f"本地订单已是终态「{_status_label(order.status)}」，"
            "按「终态不来回改」保留本地状态，请人工核对。"
        )

    if accepted and str(target) != str(OrderStatus.REFUNDED):
        # ⭐撤销完成（target=REFUNDED）⛔ 不再补占位接手人——
        # 代练通那边已经没有活跃单，补上只会让「全部结束 ⇒ 清占位人」多绕一圈
        # （且重复回读时会把刚清掉的占位人又写回来）。
        note = _assign_placeholder_taker(order)
        if note:
            notes.append(note)
    else:
        # 远端还没人接（10 / 11 / 18 已下架）：本地若已有**真实**接手人 ⇒ 「内部打手先接」
        # 那一档（Rev.9 §4 第 2 条）。⛔ 此时不把本地拉回「待抢单」：撤回代练通由
        # `reconcile_internal_claims` 收口。
        if normal_taker_of(order) is not None:
            return ""

    if target is not None:
        note = _apply_local_status(
            order,
            target,
            completing_dailiantong_settlement=settling,
        )
        if note:
            notes.append(note)
    # ⭐ H3-2：远端已结算 ⇒ 绩效置已结（放在状态回写**之后**：本地先落「已完成」，
    # 绩效跟着结账；任何一步失败都会抛出 ⇒ 由 `refresh_push_status` 记 `last_error`
    # 并**保持轮询**，下一轮重试，⛔ 不假报成功）。
    if settling:
        try:
            note = settle_performances_on_remote_settled(order)
        except Exception as exc:  # noqa: BLE001 —— 换成可读中文再抛（原文可能带 DB 细节）
            raise DailiantongPushError(
                "代练通已结算，但本地绩效置为已结账失败："
                f"{str(exc) or type(exc).__name__}"
            ) from exc
        if note:
            notes.append(note)
    # ⭐ 业务方）：本地已退单 + 代练通**再无活跃推送** ⇒ 清掉占位接手人
    # （打手申请撤销 → 客服同意 → 平台单结束后的收口）。失败只落 remote_note、⛔ 不抛。
    release_note = release_placeholder_taker_if_no_active_pushes(order)
    if release_note:
        notes.append(release_note)
    return "；".join(notes)


# ── A3：抛代练通前补发内部大厅（Rev.9 §3）──────────────────────────────────


def order_in_internal_hall(order) -> bool:
    """「已发内部大厅」判据 = 当前就在大厅（`Order.status == WAITING_GRAB`）。不用 `published_at`/`throw_to_hall`
    审计行：那回答「曾经发过」（`published_at` 首次接手也会写），A3 要的是「现在在不在厅里」——撤回/退单回池后两者还在
    但单已不在厅 ⇒ 必须补发（D-0318 重抛场景）。池内两态都有「没有接手人」不变量（D-0032）⇒ 不必再查 MAIN 行。"""
    return str(order.status) == str(OrderStatus.WAITING_GRAB)


def ensure_internal_hall(
    order, *, operator, hall_price=None, hall_price_label: str = "同发内部抢单价"
) -> str:
    """A3（Rev.9 §3）：抛代练通前该单还没发内部大厅 ⇒ 同时发（大厅价=发单价），返回提示（空串=已在大厅）。「同发内部抢单价」
    传入 `hall_price` 时在补发这一次写进 `Order.hall_price`（盖 `local_action_at`，上家迟到事件会覆盖的六列之一）；已在大厅的单
    不写价（改价走 `/hall-price/`，⛔ 不在此另开改价路）；⚠️ 失败只提示、不判整体失败（业务方裁定：大厅是内部渠道，不挡真钱发单）。"""
    if order_in_internal_hall(order):
        return ""
    from apps.business.order_status import (
        IllegalTransition,
        apply_transition,
        stamp_local_action,
    )

    if hall_price is not None:
        value = _to_decimal(hall_price)
        if value < 0:
            return "补发内部大厅没成功：同发内部抢单价不能是负数。"
        if order.hall_price != value:
            from apps.business import jidanpu_assignment_lock
            from apps.business.order_writes import lock_order_row

            with transaction.atomic():
                locked_order = lock_order_row(order)
                if locked_order is None:
                    return "补发内部大厅没成功：订单已变化，请刷新后重试。"
                order.hall_price = locked_order.hall_price
                if order.hall_price != value:
                    if changes_approved_penalty_price(locked_order, value):
                        return f"补发内部大厅没成功：{PENALTY_AMOUNT_LOCKED}"
                    if (locked_order.status == OrderStatus.IN_PROGRESS
                            and not jidanpu_assignment_lock.lock_order_change(order.pk)):
                        return f"补发内部大厅没成功：{jidanpu_assignment_lock.ORDER_BUSY}"
                    order.hall_price = value
                    order.save(update_fields=["hall_price", "updated_at"])
                    stamp_local_action(order)

    actor = (getattr(operator, "username", "") or "").strip() or SYNC_ACTOR
    try:
        with transaction.atomic():
            apply_transition(
                order,
                OrderStatus.WAITING_GRAB,
                nodes=_machine_nodes_for(OrderStatus.WAITING_GRAB),
                actor=actor,
                actor_id=getattr(operator, "pk", None),
                hall_publisher=operator,
                # 本次是「抛代练通」这个动作的附带写入：⛔ 不受「已抛代练通禁止手改状态」约束。
                allow_status_change_while_pushed=True,
                # ⭐ D8：撤销申请已提交 ⇒ 允许把旧的「代练通打手」占位接手人清掉再回大厅
                # （否则 D2「有接手人 ⇒ 不许回池」会挡住重发时的「同发大厅」）。
                allow_taker_release=cancel_application_submitted_for_order(order),
            )
            # 复用 `order.throw_to_hall` 的动作码：这**就是**一次真实的「发到内部抢单大厅」。
            record(
                log_type=ACTION_LOG_TYPES["order.throw_to_hall"],
                action="order.throw_to_hall",
                actor=actor,
                actor_id=getattr(operator, "pk", None),
                target_kind="order",
                target_id=order.order_no,
                order_no=order.order_no,
                to_value=order.hall_price,
                reason="抛代练通前补发内部大厅（Rev.9 §3）",
            )
    except IllegalTransition as exc:
        return f"补发内部大厅没成功：{exc}"
    except Exception as exc:  # noqa: BLE001 —— 大厅失败不判整体失败（业务方裁定）
        logger.warning(
            "抛单前补发内部大厅失败 order=%s（%s）", order.order_no, type(exc).__name__
        )
        return (
            "补发内部大厅没成功"
            f"（{type(exc).__name__}）。请稍后到抢单大厅页手动抛一次。"
        )

    if hall_price is not None:
        note = (
            f"已同时发到内部抢单大厅（大厅价 = {hall_price_label} "
            f"{_fmt_money(order.hall_price)} 元）"
        )
    else:
        note = "已同时发到内部抢单大厅（大厅价 = 发单价）"
    if order.hall_price is None:
        note += "；注意：这张单没有发单价，打手在厅里暂时接不了，请补「发单价」"
    return note


# ── A4：内部打手先接 ⇒ 撤回代练通（Rev.9 §4 第 2 条）──────────────────────


def _note_claim_withdraw_failure(push: DailiantongPush, reason: str) -> None:
    """记一次「撤回代练通」失败（有界重试；到上限就停，原因留在 `remote_note`）。"""
    attempts = (push.claim_withdraw_attempts or 0) + 1
    text = f"内部打手先接单，但撤回代练通失败（第 {attempts} 次）：{reason}"
    if attempts >= MAX_CLAIM_WITHDRAW_ATTEMPTS:
        text += "；已停止自动重试，请到代练通后台手工处理"
    push.claim_withdraw_attempts = attempts
    push.remote_note = _clip_note(text)
    push.save(
        update_fields=["claim_withdraw_attempts", "remote_note", "updated_at"]
    )


def reconcile_internal_claims(
    *, platform: int = DEFAULT_PLATFORM, limit: int = 50
) -> tuple[int, int]:
    """A4（Rev.9 §4 第 2 条）：内部打手先接的单 ⇒ 同步每轮扫一次把代练通订单撤掉（回调即时唤醒、兜底 1 小时；⛔ 不在抢单事务里发上游写请求）。判据：`state=sent`+`is_active=True`+有上游单号、本地未软删、MAIN 行有真实接手人（非占位账号）、远端未接（空/10/11/18——已接走属「两边同时接」，由 `apply_remote_status` 撤本地打手，⛔ 不在此撤）、未撤过（`claim_withdraw_at` 空）且失败未到上限；未接手 ⇒ 删除（即时退款）、已接手 ⇒ 申请协商撤销并记 `claim_withdraw_at`，走 `withdraw_push_for_machine` 同一原语，成败都写 `remote_note`+审计。M3：兜底与即时路径口径统一、都传 `allow_unaccepted_delete=True`（空/10/18 一律按「未接手」删，真机实测通过；两路结论不一致会让撤回失败的平台单永久悬置）；`remote_is_accepted()` 仍含 16/17 ⇒ 那些行走判据第 3 条跳过。返回 `(成功行数, 失败行数)`。"""
    from apps.business.dailiantong_actions import (
        withdraw_push_for_machine,
    )

    handled = 0
    failed = 0
    pushes = (
        DailiantongPush.objects.select_related("order")
        .filter(
            platform=platform,
            state=DailiantongPush.State.SENT,
            is_active=True,
        )
        .exclude(serial_no__isnull=True)
        .exclude(serial_no="")
        .order_by("id")[:limit]
    )
    for push in pushes:
        order = push.order
        if order is None or getattr(order, "deleted_at", None) is not None:
            continue
        if normal_taker_of(order) is None:
            continue
        if push.claim_withdraw_at is not None:
            continue
        if (push.claim_withdraw_attempts or 0) >= MAX_CLAIM_WITHDRAW_ATTEMPTS:
            continue
        if remote_is_accepted(push):
            continue
        try:
            note = withdraw_push_for_machine(
                push,
                # ⭐ M3：与即时路径同口径（空 / 10 / 11 / 18 一律按「未接手」删除）。
                allow_unaccepted_delete=True,
            )
        except DailiantongPushError as exc:
            failed += 1
            _note_claim_withdraw_failure(push, str(exc))
            logger.warning(
                "内部打手先接，但撤回代练通失败 push=%s serial=%s（%s）",
                push.pk,
                push.serial_no,
                type(exc).__name__,
            )
            continue
        except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整轮
            failed += 1
            _note_claim_withdraw_failure(push, type(exc).__name__)
            logger.warning(
                "内部打手先接，撤回代练通出现未预期失败 push=%s（%s）",
                push.pk,
                type(exc).__name__,
            )
            continue
        handled += 1
        push.claim_withdraw_at = timezone.now()
        push.claim_withdraw_attempts = 0
        push.remote_note = _clip_note(f"内部打手已先接单，代练通侧已处理：{note}")
        push.save(
            update_fields=[
                "claim_withdraw_at",
                "claim_withdraw_attempts",
                "remote_note",
                "updated_at",
            ]
        )
    return handled, failed


# ── ⭐ H3-1：撞单 ⇒ 保留最早接单，其余 0 撤 ─────────────────


def _clip_conflict_note(text: str) -> str:
    """按 `conflict_zero_refund_note` 列宽截断（⛔ 不靠数据库 `DataError` ⇒ 500）。"""
    return (text or "")[:CONFLICT_ZERO_REFUND_NOTE_MAX_LENGTH]


def conflict_accepted_sort_key(push) -> tuple:
    """「撞单保留最早接单」（业务方口径）的唯一排序键：`remote_accepted_at`（=`StartDate`/`AcceptDate`，唯一写入点 `store_remote_fields`，唯一跨平台可比时钟）；为空退化 `push.created_at`（⚠️ 近似值不是事实——它答的是「我们何时发出」，⛔ 不拿回读时刻冒充接单时刻）；主键兜底保证同刻顺序确定（两轮之间不翻转）。⚠️ 有/无接单时间的行同线比较是已知代价，只发生在上游没回 StartDate 的脏单上。"""
    if push.remote_accepted_at is not None:
        return (push.remote_accepted_at, push.pk)
    return (push.created_at, push.pk)


def conflict_zero_refund_denial(push, *, now=None) -> str | None:
    """这条推送这一轮能不能发起 0 撤，返回中文原因/`None`（可尝试）；与 `_zero_refund_conflict_groups` 的查询条件同源
    （查询挡绝大多数不该跑的行，本函数逐行复核，也给单测/直接调用一个显式判据）。"""
    now = now or timezone.now()
    if push.conflict_zero_refund_at is not None:
        return "已经发起过撞单 0 撤（同一条只发起一次）。"
    if (push.conflict_zero_refund_attempts or 0) >= MAX_CONFLICT_ZERO_REFUND_ATTEMPTS:
        return (
            f"撞单 0 撤已经失败 {MAX_CONFLICT_ZERO_REFUND_ATTEMPTS} 次，已停止自动重试；"
            "请到代练通后台人工处理。"
        )
    next_at = push.conflict_zero_refund_next_at
    if next_at is not None and next_at > now:
        return "在撞单 0 撤的失败退避窗口内，下一轮再试。"
    if push.cancel_requested_at is not None:
        # 客服从「撤销后重发」流程里主动撤过这一条 ⇒ 那是人的决定，⛔ 不被系统 0 撤覆盖。
        return "这条推送已经提交过人工协商撤销，不参与撞单 0 撤。"
    if push.remote_status not in CONFLICT_ACCEPTED_REMOTE_STATUSES:
        return "远端状态不在「正在代练 / 等待验收」，不需要 0 撤。"
    return None


def _conflict_candidate_queryset():
    """撞单候选的唯一查询定义（两步共用）。判据：不按平台收窄（能撤的只有代练通，其它平台见 `_note_conflict_platform_not_connected`、
    零上游调用）；有上游单号；`remote_status∈{12,13}`（已接手=撞单定义）；「还在跑」两档：正常在途（`sent`+`is_active=True`+未申请撤销）
    或 H2 回归行（`closed`+撤销标记非空+`remote_cancel_status∈{10,13}`：D8 旧行撤销被远端取消、收 `closed` 不硬清 ⇒ 本地已收口远端还活着，仍须可见可 0 撤，否则兜底失效）。"""
    return (
        DailiantongPush.objects.filter(
            Q(
                state=DailiantongPush.State.SENT,
                is_active=True,
                cancel_requested_at__isnull=True,
            )
            | Q(
                state=DailiantongPush.State.CLOSED,
                cancel_requested_at__isnull=False,
                remote_cancel_status__in=(CANCEL_STATUS_NOT_CANCELLED, 13),
            ),
            remote_status__in=tuple(CONFLICT_ACCEPTED_REMOTE_STATUSES),
        )
        .exclude(serial_no__isnull=True)
        .exclude(serial_no="")
    )


def _zero_refund_conflict_groups():
    """按订单分组的撞单候选（同单 ≥2 条已接手且有效的推送）。撞单通常跨平台（`uniq_dailiantong_push_active` 保证同 (订单,平台)
    至多一条有效推送）；H2 例外：D8 旧行撤销被远端取消时收 `closed` 保留标记、同 (订单,平台) 两张远端活单，候选第 ② 支收它
    （⛔ 不为「同平台也能撞」放宽唯一约束——它防重复抛单真钱重复建单，H2 形状与它不冲突）。两步查询：先聚合「候选 ≥2」订单再取行，判据只在 `_conflict_candidate_queryset` 一处；本地已软删订单再排一次。"""
    base = _conflict_candidate_queryset()
    duplicated_order_ids = list(
        base.values("order_id")
        .annotate(total=Count("id"))
        .filter(total__gte=2)
        .values_list("order_id", flat=True)
    )
    if not duplicated_order_ids:
        return {}
    groups: dict[int, list[DailiantongPush]] = {}
    for push in (
        base.select_related("order")
        .filter(order_id__in=duplicated_order_ids)
        .order_by("order_id", "id")
        .iterator()
    ):
        order = push.order
        if order is None or getattr(order, "deleted_at", None) is not None:
            continue
        groups.setdefault(push.order_id, []).append(push)
    return groups


def _note_conflict_platform_not_connected(push, *, keeper_note: str) -> None:
    """非代练通的撞单行：零上游调用，只写可读原因（同值不重复写）。⚠️ 不置 `conflict_zero_refund_at`——
    那是「已处理完」的幂等键，其它平台适配器上线后这一行还要能被 0 撤，⛔ 不要现在把路堵死。"""
    reason = _clip_conflict_note(
        f"撞单：{keeper_note}；该交易平台（platform={push.platform}）尚未接入 0 撤能力，"
        "本条未自动处理"
    )
    if push.conflict_zero_refund_note == reason:
        return
    push.conflict_zero_refund_note = reason
    push.save(update_fields=["conflict_zero_refund_note", "updated_at"])
    logger.warning(
        "撞单 0 撤：推送行平台未接入，跳过 push=%s platform=%s", push.pk, push.platform
    )


def _mark_conflict_zero_refund_done(push, *, note: str) -> None:
    """记「已收口」：发起成功 / 远端已不需要撤 —— 只做一次（`conflict_zero_refund_at`）。"""
    push.conflict_zero_refund_at = timezone.now()
    push.conflict_zero_refund_attempts = 0
    push.conflict_zero_refund_next_at = None
    push.conflict_zero_refund_note = _clip_conflict_note(note)
    push.save(
        update_fields=[
            "conflict_zero_refund_at",
            "conflict_zero_refund_attempts",
            "conflict_zero_refund_next_at",
            "conflict_zero_refund_note",
            "updated_at",
        ]
    )


def _note_conflict_zero_refund_failure(push, reason: str, *, now=None) -> None:
    """记一次撞单 0 撤失败 + 退避；到上限就停手并留下可读原因（⛔ 不刷屏）。"""
    now = now or timezone.now()
    attempts = (push.conflict_zero_refund_attempts or 0) + 1
    stopped = attempts >= MAX_CONFLICT_ZERO_REFUND_ATTEMPTS
    text = f"撞单 0 撤失败（第 {attempts} 次）：{reason}"
    if stopped:
        text += (
            f"；已停止自动重试（上限 {MAX_CONFLICT_ZERO_REFUND_ATTEMPTS} 次），"
            "请到代练通后台人工处理"
        )
    push.conflict_zero_refund_attempts = attempts
    push.conflict_zero_refund_next_at = (
        None if stopped else now + CONFLICT_ZERO_REFUND_RETRY
    )
    push.conflict_zero_refund_note = _clip_conflict_note(text)
    # `last_error` 会被下一轮回读成功清空 ⇒ 持久原因在 `conflict_zero_refund_note`；
    # 同时写一份是为了让它在页面的「最近错误」里立刻可见（与自动仲裁段同一口径）。
    push.last_error = _clip_error(text)
    push.save(
        update_fields=[
            "conflict_zero_refund_attempts",
            "conflict_zero_refund_next_at",
            "conflict_zero_refund_note",
            "last_error",
            "updated_at",
        ]
    )


def zero_refund_conflicting_pushes(
    *, platform: int = DEFAULT_PLATFORM, now=None, limit: int = 50
) -> tuple[int, int]:
    """H3-1：撞单 ⇒ 保留最早接单（`conflict_accepted_sort_key` 最小者一字节不动），其余申请 0 撤；`dailiantong_sync` 每轮回读后调一次（⛔ 不另起 systemd 单元），返回 `(已发起并核验数, 失败数)`。开关 `auto_zero_refund_on_conflict` 关 ⇒ 立刻返回 (0,0)、零查询零上游调用（⛔ 不是降级处理）；撞单判据见 `_zero_refund_conflict_groups`（同单 ≥2 条 `is_active` 且远端已接手，天然跨平台）；其余各条走 `zero_refund_push_for_machine`（`LevelOrderCancel(Flag=0, 双金=0)`，写前/写后回读核验），只有代练通能撤——其它平台只写可读原因（`_note_conflict_platform_not_connected`）、⛔ 零上游调用、不置幂等键（适配器上线后还要能撤）。
    幂等：成功 ⇒ `conflict_zero_refund_at` 只发起一次；失败按 `CONFLICT_ZERO_REFUND_RETRY`（6 小时）退避、最多 `MAX_CONFLICT_ZERO_REFUND_ATTEMPTS`（5）次，原因留 `conflict_zero_refund_note`（⛔ 不每轮刷上游）。三件以上每轮只留最早一条、其余各自走一遍；每轮最多 `limit` 条（防积压拖成分钟级）；单条失败不中断整轮。"""
    from apps.business.dailiantong_actions import zero_refund_push_for_machine

    now = now or timezone.now()
    # ── 开关：关 ⇒ 什么都不做（⛔ 零上游调用是本条的硬判据）──
    if not PlatformPushSettings.get_solo().auto_zero_refund_on_conflict:
        return 0, 0

    handled = 0
    failed = 0
    budget = limit
    for pushes in _zero_refund_conflict_groups().values():
        if len(pushes) < 2:
            continue
        keeper = min(pushes, key=conflict_accepted_sort_key)
        keeper_note = (
            f"保留最早接单的推送 #{keeper.pk}"
            f"（接单时间 {keeper.remote_accepted_at or keeper.created_at:%Y-%m-%d %H:%M:%S}"
            f"{'' if keeper.remote_accepted_at else '，上游未回传接单时间⇒按推送创建时间'}）"
        )
        for push in pushes:
            if push.pk == keeper.pk:
                continue
            if budget <= 0:
                return handled, failed
            denial = conflict_zero_refund_denial(push, now=now)
            if denial:
                continue
            if push.platform != platform:
                # ⚠️ 不给「未接入平台」扣失败预算 / 记重试：它不是失败，是**还没能力**。
                _note_conflict_platform_not_connected(push, keeper_note=keeper_note)
                continue
            budget -= 1
            try:
                applied, note = zero_refund_push_for_machine(push)
            except DailiantongPushError as exc:
                failed += 1
                _note_conflict_zero_refund_failure(push, str(exc), now=now)
                logger.warning(
                    "撞单 0 撤失败 push=%s serial=%s（%s）",
                    push.pk,
                    push.serial_no,
                    type(exc).__name__,
                )
                continue
            except Exception as exc:  # noqa: BLE001 —— 单条失败不中断整轮
                failed += 1
                _note_conflict_zero_refund_failure(push, type(exc).__name__, now=now)
                logger.warning(
                    "撞单 0 撤出现未预期失败 push=%s（%s）",
                    push.pk,
                    type(exc).__name__,
                )
                continue
            if applied:
                handled += 1
            _mark_conflict_zero_refund_done(push, note=f"{keeper_note}；{note}")
    return handled, failed


def _remember_zone(order, zone_server_id: str) -> None:
    """把「我方 game+server ↔ 代练通区服」记住（同一组合以后自动带出）。⚠️ 订单没有区服（`server_id is None`）时跳过：
    本表唯一键是 `(game, server)`，用 `NULL` 做键会允许无数重复行（等于没记）。"""
    if order.server_id is None:
        return
    try:
        DailiantongZoneMap.objects.update_or_create(
            game_id=order.game_id,
            server_id=order.server_id,
            defaults={"zone_server_id": zone_server_id},
        )
    except IntegrityError:
        # 并发窗口：另一请求刚刚插了同一 `(game, server)`（唯一索引挡住我们这一支）
        # ⇒ 以库里的行为准，把编码更新成这次用户选的（⛔ 不让用户看到一个 500）。
        DailiantongZoneMap.objects.filter(
            game_id=order.game_id, server_id=order.server_id
        ).update(zone_server_id=zone_server_id)


def _record_push(push: DailiantongPush, order, operator, *, reason: str) -> None:
    """写一条抛单审计（`target` 指向推送行；⛔ 不含任何凭据）。"""
    record(
        log_type=ACTION_LOG_TYPES[AUDIT_ACTION],
        action=AUDIT_ACTION,
        actor=(getattr(operator, "username", "") or "").strip() or "系统",
        actor_id=getattr(operator, "pk", None),
        target_kind=AUDIT_TARGET_KIND,
        target_id=str(push.pk),
        target_name=order.order_no,
        order_no=order.order_no,
        game_name=(order.game.name or "") if order.game_id else "",
        to_value=_fmt_money(push.price),
        reason=reason,
    )


def _create_push_row(
    order,
    *,
    platform: int,
    price: Decimal,
    ensure1: Decimal,
    ensure2: Decimal,
    time_limit_hours: int,
    platform_only: bool,
    replaces_push: DailiantongPush | None = None,
) -> DailiantongPush:
    """落一行 `pending`；并发撞部分唯一索引时翻成中文业务错误（⛔ 不 500）。D8：`replaces_push` 非空 = 撤销申请后的重发，新行记下接管的旧行。"""
    prior_attempts = DailiantongPush.objects.filter(
        order=order, platform=platform
    ).count()
    try:
        with transaction.atomic():
            from apps.business.order_writes import lock_order_row

            locked = lock_order_row(order)
            if locked is None or locked.deleted_at is not None:
                raise DailiantongPushError("订单已删除，无法抛单")
            denial = _push_status_denial(locked, platform=platform)
            if denial is not None:
                raise DailiantongPushError(denial)
            _raise_if_already_pushed(locked, platform)
            if Performance.objects.filter(
                order=locked, emp_type=EmpType.MAIN, online_payroll_batch__isnull=False,
            ).exists():
                raise DailiantongPushError("打手报酬已有在线付款记录，无法抛代练通")
            return DailiantongPush.objects.create(
                order=order,
                platform=platform,
                state=DailiantongPush.State.PENDING,
                price=price,
                ensure1=ensure1,
                ensure2=ensure2,
                time_limit_hours=time_limit_hours,
                platform_only=platform_only,
                ex_order_no=order.order_no,
                attempts=prior_attempts + 1,
                replaces_push=replaces_push,
            )
    except IntegrityError as exc:
        # 只认「有效推送」那条部分唯一索引；其它完整性错误照旧响亮地 500（E-0010）。
        if "uniq_dailiantong_push_active" not in str(exc):
            raise
        raise DailiantongPushError(
            "这张单已经有一条有效的抛单记录（可能是刚刚并发提交的），"
            "已申请撤销的单才能重发；如要重发请先申请撤销。"
        ) from exc


def _save_send_result(
    push: DailiantongPush,
    order,
    operator,
    *,
    state: str,
    serial_no: str | None,
    remote_status: int | None,
    last_error: str,
    reason: str,
) -> DailiantongPush:
    """把上游结果写回链路行（`update_fields` 显式列出，⛔ 不漏列）。真机 bug：`state=failed` 必须同时 `is_active=False`——
    失败行不是「在途」，所有按 `is_active` 判的读面（无活跃平台单/状态显示/能否重抛/清占位人）会把 `failed+is_active=True`
    误当成「代练通还有活跃订单」。`failed` ⇒ 显式关轮询闸；`unknown`/`pending` 保持现状（待人工核对/发送中仍算在途）。"""
    push.state = state
    push.serial_no = serial_no
    push.remote_status = remote_status
    push.last_error = _clip_error(last_error)
    update_fields = [
        "state",
        "serial_no",
        "remote_status",
        "last_error",
        "updated_at",
    ]
    if state == DailiantongPush.State.FAILED:
        push.is_active = False
        update_fields.append("is_active")
    push.save(update_fields=update_fields)
    _record_push(push, order, operator, reason=reason)
    return push


# ── 对外：pending 超时回收（同步命令每轮先调）──────────────────────────────


def reclaim_stale_pending_pushes(
    *, now=None, timeout_seconds: int = PENDING_TIMEOUT_SECONDS
) -> int:
    """把超时仍 `pending` 的推送行标成 `unknown`，返回改动行数。P1-4：`push_order` 先落 `pending` 再发上游，进程发单
    中途被杀会永远停在 `pending`，而同步只轮询 `sent`/`unknown`、闸 4 只回「请稍后刷新」⇒ 没有任何出口；超时行标 `unknown`+
    可读原因后可走人工标记失败（重新抛单）。⚠️ 不重试、不改本地订单状态（仍不知道上游有没有建单）。"""
    now = now or timezone.now()
    threshold = now - timedelta(seconds=int(timeout_seconds))
    stale = DailiantongPush.objects.filter(
        state=DailiantongPush.State.PENDING,
        updated_at__lt=threshold,
    )
    return stale.update(
        state=DailiantongPush.State.UNKNOWN,
        last_error=_clip_error(
            f"抛单在 pending 状态超过 {int(timeout_seconds) // 60} 分钟（进程可能在发单中途退出），"
            "已自动标为状态未知：请到代练通后台核对有没有这张单，"
            "确认没有成功后可用人工命令标记失败再重抛。"
        ),
        updated_at=now,
    )


# ── 对外：unknown → failed（人工核对后的唯一出口）───────────────────────────


def mark_unknown_push_failed(
    push: DailiantongPush, *, operator, evidence: str
) -> DailiantongPush:
    """把一条 `unknown` 推送人工标成 `failed`（之后可重新抛单）。P1-5：`unknown` 是「可能已发出」档 ⛔ 永不自动重试，核对上游确实没建单后需要一个受审计的出口。只接受 `state=unknown`（其它给中文业务错误、⛔ 不改）；`evidence` 必填且至少 `MANUAL_FAILURE_EVIDENCE_MIN_LENGTH` 字；写 `order.dailiantong_push` 审计、`last_error` 记「人工核对后标记失败：<依据>」、`is_active=False`（轮询不再白打上游）。"""
    if operator is None or getattr(operator, "pk", None) is None:
        raise ValueError("mark_unknown_push_failed 需要已认证的操作人（operator.pk）")
    text = (evidence or "").strip()
    if len(text) < MANUAL_FAILURE_EVIDENCE_MIN_LENGTH:
        raise DailiantongPushError(
            "请填写人工核对依据（至少 "
            f"{MANUAL_FAILURE_EVIDENCE_MIN_LENGTH} 个字），例如「已登录代练通后台按外部单号"
            "和发布时间核对，没有这张单」。"
        )
    if push.state != DailiantongPush.State.UNKNOWN:
        raise DailiantongPushError(
            f"只有「状态未知」的推送才能人工标记失败，这一行当前是「{push.get_state_display()}」。"
        )

    push.state = DailiantongPush.State.FAILED
    push.is_active = False
    push.last_error = _clip_error(f"人工核对后标记失败：{text}")
    push.save(
        update_fields=["state", "is_active", "last_error", "updated_at"]
    )
    _record_push(
        push,
        push.order,
        operator,
        reason=f"人工核对后把状态未知标记为失败（核对依据：{text}）",
    )
    return push


# ── 对外：发单预览（一键抛单 B8）────────────────────────────

#: 一键抛单计划 §2.3 的默认发单值（**预览与真发单共用的唯一出处**）。
#: 时间要求默认 30 天 = 720 小时；两份保证金默认 0.00。
PUSH_DEFAULT_TIME_LIMIT_HOURS = 720
PUSH_DEFAULT_ENSURE = Decimal("0.00")

#: 预览里「调用方手动选了区服」的来源值。⚠️ 与 B5 的 `map|first|none` 并列：
#: 那三个描述「自动默认怎么来的」，这个描述「这次预览用的是调用方给的编码」。
ZONE_SOURCE_SELECTED = "selected"


def default_push_price(order):
    """发单页默认价格：`hall_price`（发单价）优先 → `platform_price` → `None`；只接受 >0 的值（`0` 视为「没定价」，
    返回 `None` 让页面留空要求手填，⛔ 不把 0 塞进价格框）。业务方口径：`platform_price` 默认是 0，先取它会显示 0.00。"""
    for value in (order.hall_price, order.platform_price):
        if value is not None and value > 0:
            return value
    return None


#: 上游 `Memo`（代练通备忘）的列宽上限：`String(2048)`
#: （`docs/references/dailiantong/open-api-v3.4.md:554`）。
MEMO_MAX_LENGTH = 2048

#: 上游 `Groups`（发单频道）的列宽上限：`String(50)`
#: （`docs/references/dailiantong/open-api-v3.4.md:556`：`PUBLIC` 公共频道（默认）/
#: `GOODCHANNEL` 优质频道；也支持内部分组 id，英文逗号分隔）。
GROUPS_MAX_LENGTH = 50

#: 上游 `Label1` / `Label2`（标签）的列宽上限：`String（512）`
#: （`docs/references/dailiantong/open-api-v3.4.md:552-553`）。
LABEL_MAX_LENGTH = 512


def build_memo(order) -> str:
    """发单载荷 `Memo`：`project_text`（代练内容）→ `content`（客户原始需求）→ `remark`（重要备注）换行分隔（strip 后非空才拼、去重），
    本地按字符截到 2048（⛔ 不交给上游回列宽错误）；三段都空 ⇒ 空串（按不上送处理）。FX1 真机：代练内容可能在 `project_text` 而
    `content`/`remark` 空，旧实现只拼前两段 ⇒ 发出的 `Memo` 为空、打手看不到要做什么；⚠️ 预览与真发同源（页面传了 memo 两处都用页面那份，一字不改）。"""
    parts: list[str] = []
    for value in (
        getattr(order, "project_text", ""),
        getattr(order, "content", ""),
        getattr(order, "remark", ""),
    ):
        text = (value or "").strip()
        # 去重：同一段被填在两个格子里的情况真实存在（页面会把代练内容同时写进
        # 「项目 / 代练内容」与「客户原始需求」）⇒ ⛔ 不把同一句话贴两遍。
        if text and text not in parts:
            parts.append(text)
    return "\n".join(parts)[:MEMO_MAX_LENGTH]


def split_actors(actors: str) -> dict[str, str]:
    """把 `Actors` 五段拆成具名键（预览接口用；⛔ 不另拼一份账号资料）。"""
    parts = (actors or "").split("|*|")
    while len(parts) < 5:
        parts.append("")
    return {
        "account": parts[0],
        "password": parts[1],
        "role": parts[2],
        "current_info": parts[3],
        "requirement": parts[4],
    }


def _zone_name_for_id(zone_server_id: str) -> str:
    """从**已有缓存 / 字典**里取区服的平台名（取不到就空串，⛔ 不编一个）。"""
    try:
        rows = _cached_zone_rows()
    except (DailiantongPushError, DailiantongError):
        return ""
    row = _zone_row_for_id(rows, zone_server_id)
    return _zone_value(row, "ServerName", "servername") if row is not None else ""


def _push_status_denial(order, *, platform: int = DEFAULT_PLATFORM) -> str | None:
    """抛单的本地状态闸（`push_order` 与 `preview.can_push` 共用同一判据/文案）。业务方真机口径：没有活跃代练通订单时「已退单」
    也可重抛。允许 `UNASSIGNED`/`WAITING_GRAB`/`REFUNDED`（「有没有活跃推送」由「已有有效推送」闸拒——⛔ 不在此查推送，拒因要保持
    「不能重复抛单」的准确性）；D8：已提交协商撤销的单照旧放行（打手失联重发场景）；其它状态（代练中/待验收/已完成/问题单/已暂停）仍拒并给中文原因。"""
    if order.peer_direct_pending is not None:
        return PEER_DIRECT_PENDING_MESSAGE
    if order.status in (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB):
        return None
    if (
        order.status != OrderStatus.COMPLETED
        and cancel_application_submitted_for_order(order, platform=platform)
    ):
        return None
    if str(order.status) == str(OrderStatus.REFUNDED):
        return None
    return (
        "只有「未分配」「待抢单」，或「已退单且代练通没有进行中的订单」才能抛到代练通，"
        f"这张单当前状态是「{order.get_status_display()}」。"
    )


def _mark_waiting_grab_after_push(order, operator) -> str:
    """抛单成功后把本地订单置为「待抢单」，返回中文提示（空=不用改）。只有 `UNASSIGNED`/`REFUNDED` 两档补这一步
    （已是 `WAITING_GRAB` 不重复流转——状态机无自环；`IN_PROGRESS` 等 D8 重发不动）；走既有唯一写入口 `apply_transition`
    （⛔ 不直接写字段）；审计复用 `order.throw_to_hall`（这就是一次「发到内部抢单大厅」的落地）。"""
    if str(order.status) not in (
        str(OrderStatus.UNASSIGNED),
        str(OrderStatus.REFUNDED),
    ):
        return ""
    from apps.business.order_status import apply_transition

    actor = (getattr(operator, "username", "") or "").strip() or SYNC_ACTOR
    try:
        apply_transition(
            order,
            OrderStatus.WAITING_GRAB,
            nodes=_machine_nodes_for(OrderStatus.WAITING_GRAB),
            actor=actor,
            actor_id=getattr(operator, "pk", None),
            hall_publisher=operator,
            allow_status_change_while_pushed=True,
            allow_taker_release=cancel_application_submitted_for_order(order),
        )
    except Exception as exc:  # noqa: BLE001 —— 发单已经成功，状态没改只是提示、⛔ 不报失败
        return f"代练通已发单成功，但本地状态未能改成「待抢单」：{exc}"
    record(
        log_type=ACTION_LOG_TYPES["order.throw_to_hall"],
        action="order.throw_to_hall",
        actor=actor,
        actor_id=getattr(operator, "pk", None),
        target_kind="order",
        target_id=order.order_no,
        order_no=order.order_no,
        to_value=order.hall_price,
        reason="抛代练通成功后置为待抢单",
    )
    return ""


def _preflight_hard(order) -> tuple[bool, str]:
    """硬闸（`can_push=false` 只放「在弹窗里改不了」的原因，业务方拍板），只有三类：① 没有「启用且默认」的代练通配置；② 本地状态不允许（`UNASSIGNED`/`WAITING_GRAB`/`REFUNDED` 三档，已退单能否真抛由「已有有效推送」闸接着判）；③ 该单已有有效推送。⛔ 价格 <2 元、区服没匹配不算硬闸（弹窗里就能改，禁用提交会废掉「一键抛单只改价格」⇒ 进 `warnings` 只提示不禁用）；与 `push_order` 前置闸同一批判据与文案（⛔ 不另写一套），区别只是余额检查不在预览里（计划 B8：由 summary 负责）。"""
    try:
        _active_integration(DEFAULT_PLATFORM)
    except DailiantongPushError as exc:
        return False, str(exc)
    denial = _push_status_denial(order)
    if denial is not None:
        return False, denial
    try:
        _raise_if_already_pushed(order, DEFAULT_PLATFORM)
    except DailiantongPushError as exc:
        return False, str(exc)
    return True, ""


def _preview_warnings(price, *, zone: dict, zone_error: str = "") -> list[str]:
    """**软问题**（可以在弹窗里改）：价格 / 区服。返回可读中文列表，⛔ 不禁用提交。"""
    warnings: list[str] = []
    if price is None:
        warnings.append(
            "这张单还没有「发平台价格」或「发内部抢单大厅价」，请先填写价格再提交。"
        )
    elif price < MIN_PRICE:
        warnings.append(
            f"抛单价格低于 {_fmt_money(MIN_PRICE)} 元（代练通实测最低 2 元），"
            f"当前是 {_fmt_money(price)} 元，请改价后再提交。"
        )
    if not zone.get("zone_server_id"):
        warnings.append(
            zone_error or "还没有可用的代练通区服，请手动选择区服。"
        )
    return warnings


def _resolve_preview_zone(order, selected: str) -> tuple[dict, str]:
    """预览的区服：没给参数 ⇒ B5 的自动默认；给了 ⇒ 用调用方那个编码（仍回 `map|first|none`）。⚠️ `zone_source` 只有
    `map|first|none` 三个值（前端契约）：调用方手动给的编码不是第四种来源——它要么正好是本游戏记住的映射（`map`）、
    要么是游戏名首匹配（`first`），否则就是没有自动默认（`none`），前端拿到的枚举不为多一种值。"""
    try:
        auto = default_zone_for_game(order.game_id)
    except (DailiantongPushError, DailiantongError) as exc:
        auto = {
            "zone_server_id": None,
            "server_name": None,
            "source": ZONE_SOURCE_NONE,
        }
        zone_error = str(exc)
    else:
        zone_error = ""

    if not selected:
        return auto, zone_error

    name = _zone_name_for_id(selected)
    if auto.get("zone_server_id") == selected:
        source = auto.get("source") or ZONE_SOURCE_NONE
    else:
        source = ZONE_SOURCE_NONE
    return {"zone_server_id": selected, "server_name": name, "source": source}, ""


def preview_push(
    order, *, zone_server_id: str | None = None, memo: str | None = None
) -> dict:
    """发单页默认值预览（一键抛单 B8）：与 `push_order` 实际发送同源——前端曾自己派生账号/QQ/电话导致「页面显示一套、发出去另一套」⇒ 默认值唯一来源收进后端；返回键名即契约（`can_push`/`reason`/`warnings`/`price`/`ensure*`/`time_limit_hours`/`zone_*`/`game_id`/`title`/`memo`/`Actors` 五段/联系方式/门槛默认，完整键表外移 docs/code-comment-notes.md），⛔ 只读（不写库、不发单、不进审计）。`can_push=false` 只放三类弹窗里改不了的原因（同 `_preflight_hard`，⛔ 不放宽收紧）；价格 <2/没价格/没区服 ⇒ `can_push=true`+`warnings`（⛔ 不据此禁用提交）；⛔ 不查余额（由 `GET /api/business/platform-integrations/summary/` 负责，30 秒缓存）；唯一可能打上游的是区服字典查询（模块级 10 分钟缓存 `ZONES_CACHE_TTL_SECONDS`），拉不到进 `warnings`+`zone_source="none"`、⛔ 不让预览 500。"""
    settings_row = PlatformPushSettings.get_solo()
    price = default_push_price(order)

    selected = (zone_server_id or "").strip()
    zone, zone_error = _resolve_preview_zone(order, selected)

    can_push, reason = _preflight_hard(order)
    warnings = _preview_warnings(price, zone=zone, zone_error=zone_error)

    # ⭐预览的「订单说明」与真发同源（同一份 memo → `Actors` 第 4 段）。
    preview_memo = build_memo(order) if memo is None else memo
    actors = split_actors(
        build_actors(
            order,
            requirement=require_template_from_settings(),
        )
    )
    return {
        "can_push": can_push,
        "reason": reason,
        "warnings": warnings,
        "price": None if price is None else _fmt_money(price),
        "ensure1": _fmt_money(PUSH_DEFAULT_ENSURE),
        "ensure2": _fmt_money(PUSH_DEFAULT_ENSURE),
        "time_limit_hours": PUSH_DEFAULT_TIME_LIMIT_HOURS,
        "zone_server_id": zone["zone_server_id"],
        "server_name": zone["server_name"],
        "zone_source": zone["source"],
        "game_id": order.game_id,
        "title": build_title(order),
        # ⭐ P8/P9：与 `push_order` 同口径——页面传了就用页面那份（一字不改），
        # 没传才回落「代练内容 + 客户原始需求 + 重要备注」的拼装值（FX1）。
        "memo": preview_memo,
        "game_account": actors["account"],
        "game_password": actors["password"],
        "game_role": actors["role"],
        "current_info": actors["current_info"],
        "requirement": actors["requirement"],
        "contact_qq": _contact_qq(order.order_no),
        "contact_phone": _contact_mobile(order.order_no),
        "power_level": settings_row.default_power_level,
        "limit_accept": settings_row.default_limit_accept,
    }


# ── 对外：抛单 ──────────────────────────────────────────────────────────


def _detail_field(detail: dict, *names: str) -> tuple[bool, object]:
    """在详情回包里按候选键名取值（键大小写上游不保证），返回 `(有没有这个键, 原值)`——⛔ 用 `detail.get()` 分不清
    「值是 0」与「根本没有这个键」（D-0015 同类坑）。"""
    if not isinstance(detail, dict):
        return False, None
    for name in names:
        if name in detail:
            return True, detail[name]
    folded = {str(key).strip().lower(): value for key, value in detail.items()}
    for name in names:
        key = name.strip().lower()
        if key in folded:
            return True, folded[key]
    return False, None


def _verify_thresholds(
    client, serial_no: str, *, power_level: int | None, limit_accept: int | None
) -> tuple[str, str]:
    """发单后回读上游详情核对门槛（计划 §2.4「⛔ 不假报成功」）；两个门槛都是 `None` ⇒ 返回 `("","")`、零额外上游调用，否则回读一次 `LevelOrderDetail` 逐项对，返回 `(problem, unverified)`。`problem`（回读失败/非整数/与提交不一致）落 `last_error`+审计；`unverified`（详情没回传该字段——厂商文档没列这两个键，「没回传」是常态非异常，写进 `last_error` 会让每次成功抛单都顶着一行「错误」）只落审计 reason、留「未能核对」，⛔ 不写「核对成功」。⚠️ 两者都不改推送行 `state`：单已真发出（真钱冻结），`unknown` 会挡人工重抛=再建一张真钱单（`docs/DAILIANTONG-API.md` §9 R11）。"""
    if power_level is None and limit_accept is None:
        return "", ""
    checks: list[tuple[str, str, int]] = []
    if power_level is not None:
        checks.append(("PowerLevel", "最低完单数", power_level))
    if limit_accept is not None:
        checks.append(("LimitAccept", "拒绝新手和介入率", limit_accept))

    try:
        detail = client.order_detail(serial_no)
    except DailiantongError as exc:
        # 业务失败与传输失败都算「没核对到」；⛔ 不重试、⛔ 不改 state。
        return f"已发出，但门槛回读失败（{exc.message}），无法确认门槛是否生效", ""

    problems: list[str] = []
    missing: list[str] = []
    for key, label, expected in checks:
        found, raw = _detail_field(detail, key, key.lower())
        if not found:
            missing.append(f"{label}（{key}）上游详情没有回传，未能核对")
            continue
        actual = _as_int(raw)
        if actual is None:
            problems.append(f"{label}（{key}）上游回传的不是整数，无法确认")
        elif actual != expected:
            problems.append(f"{label}提交 {expected}，上游回读 {actual}")
    problem = (
        "已发出，但门槛回读不一致：" + "；".join(problems) if problems else ""
    )
    unverified = (
        "上游详情没有回传门槛字段：" + "；".join(missing) if missing else ""
    )
    return problem, unverified


def push_order(
    order,
    *,
    price,
    ensure1,
    ensure2,
    time_limit_hours,
    zone_server_id,
    operator,
    platform: int = DEFAULT_PLATFORM,
    power_level: int | None = None,
    limit_accept: int | None = None,
    requirement: str | None = None,
    off_sell_days: int | None = None,
    memo: str | None = None,
    groups: str | None = None,
    label1: str | None = None,
    label2: str | None = None,
    hall_price_same=None,
) -> DailiantongPush:
    """把一张本地订单真发单到代练通；前置闸顺序=配置 → 订单状态 → 价格（≥2.00 全局实测下限）→ 已有有效推送 → 余额（`ActiveBal`）。通过后组装载荷 →（写 `DailiantongZoneMap`）→ 落 `pending` → `LevelOrderAdd`：成功 ⇒ `sent`+单号+`remote_status=11`；业务失败 ⇒ `failed`；超时/断连 ⇒ `unknown`（⛔ 只调一次永不自动重试）；三种结果都写审计；前置闸不通过抛 `DailiantongPushError`。门槛（`power_level`/`limit_accept`，`None`=不上送）发单成功后回读核对，不一致/没核对只写可读原因、⛔ 不改 `state`（§9 R11：重抛=再建真钱单）。
    `memo` 页面填的优先一字不改上送（超长抛中文错误；`None` 回落 `build_memo`；同一份也进 `Actors` 第 4 段）；`requirement` 空则用全局模板、超长本地截；`off_sell_days`/`groups`/`label1`/`label2` 透传（`None`=不上送/不设置）；`hall_price_same` 不是代练通参数、只用于抛前补发内部大厅（没传=本次 `price`，真机核实 ⛔ 不沿用 `Order.hall_price` 旧值）；`Mobile`/`QQ` 按订单号派生（计划 §2.3，不再用配置联系字段）；`ContactName` 会被上游强制改写成账号昵称、只给占位。参数契约全表外移 docs/code-comment-notes.md。"""
    if operator is None or getattr(operator, "pk", None) is None:
        # 编程错误（不是业务失败）：接口层永远传认证态；静默当机器行会丢操作人。
        raise ValueError("push_order 需要已认证的操作人（operator.pk）")
    if platform != DEFAULT_PLATFORM:
        # 本版只有代练通有客户端；其它平台 id 若走到这里，⛔ 绝不能拿代练通的凭据去发。
        raise DailiantongPushError("目前只有代练通支持抛单（平台 id=10）。")

    price = _to_decimal(price)
    ensure1 = _to_decimal(ensure1)
    ensure2 = _to_decimal(ensure2)
    time_limit_hours = int(time_limit_hours)
    zone_server_id = str(zone_server_id or "").strip()

    # 门槛入参先归一（`bool` 是 `int` 的子类，⛔ 别让 `True` 变成 1）。
    if power_level is not None:
        if isinstance(power_level, bool) or not isinstance(power_level, int) or power_level < 0:
            raise DailiantongPushError("要求最低完单数必须是 0 或正整数。")
    if limit_accept is not None:
        if (
            isinstance(limit_accept, bool)
            or not isinstance(limit_accept, int)
            or limit_accept < 0
            or limit_accept > LIMIT_ACCEPT_MAX
        ):
            raise DailiantongPushError(
                f"拒绝新手和介入率必须是 0–{LIMIT_ACCEPT_MAX} 的整数（留空表示不设置）。"
            )

    # ① 配置
    integration = _active_integration(platform)

    # ② 订单状态（⭐ 计划 §2.5：`UNASSIGNED` / `WAITING_GRAB`；⭐已退单 +
    #    没有活跃推送也可重抛——「活跃推送」由第 ④ 步按既有文案拒）。
    # ⭐ D8：已提交协商撤销的单允许**从代练中 / 待验收重发**（打手失联场景）。
    status_denial = _push_status_denial(order, platform=platform)
    if status_denial is not None:
        raise DailiantongPushError(status_denial)

    # ③ 价格下限（⛔ 不交给上游回 -8）
    if price < MIN_PRICE:
        raise DailiantongPushError(
            f"抛单价格不能低于 {_fmt_money(MIN_PRICE)} 元（代练通实测最低 2 元），"
            f"当前填的是 {_fmt_money(price)} 元。"
        )
    if ensure1 < 0 or ensure2 < 0:
        raise DailiantongPushError("保证金不能是负数。")
    if time_limit_hours < 1:
        raise DailiantongPushError("时限必须至少 1 小时。")
    if not zone_server_id:
        raise DailiantongPushError(
            "请先选择这张单对应的代练通区服（区服编码不能为空）。"
        )
    if len(zone_server_id) > 32:
        raise DailiantongPushError("代练通区服编码不合法（超过 32 个字符）。")

    # ⭐ P8 / P9：页面「当前订单说明」原样上送——**只挡长度**（一字不改，⛔ 不 strip）。
    if memo is not None and len(memo) > MEMO_MAX_LENGTH:
        raise DailiantongPushError(
            f"发到平台的订单说明不能超过 {MEMO_MAX_LENGTH} 个字符"
            f"（当前 {len(memo)} 个）。"
        )
    # ⭐ 发单频道 / 标签（上游 `Groups` / `Label1` / `Label2`）：透传，只做空值与长度清洗。
    groups = (groups or "").strip() or None
    if groups is not None and len(groups) > GROUPS_MAX_LENGTH:
        raise DailiantongPushError(
            f"发单频道不合法（超过 {GROUPS_MAX_LENGTH} 个字符）。"
        )
    label1 = (label1 or "").strip() or None
    label2 = (label2 or "").strip() or None
    for label_name, label_value in (("标签1", label1), ("标签2", label2)):
        if label_value is not None and len(label_value) > LABEL_MAX_LENGTH:
            raise DailiantongPushError(
                f"{label_name}不能超过 {LABEL_MAX_LENGTH} 个字符。"
            )
    # ⭐ 同发内部抢单价：**不是**代练通参数，只服务「抛单前补发内部大厅」那一步。
    can_publish_to_hall = has_node_now(operator, "order.send_to_grab_hall")
    if can_publish_to_hall and hall_price_same is not None:
        try:
            hall_price_same = _to_decimal(hall_price_same)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DailiantongPushError("同发内部抢单价不是有效数字。") from exc
        if hall_price_same < 0:
            raise DailiantongPushError("同发内部抢单价不能是负数。")

    # ④ 已有有效推送（⭐ D8：已提交撤销 ⇒ 放行重发，返回被接管的旧行）
    # ⭐ C1：远端撤销已被取消（CancelStatus=10/13）时，本地 `cancel_requested_at` 是陈旧的
    # ⇒ 判重发闸前先清掉（并留一句可读原因）；否则会被它放行成第二张真钱单。
    stale = _existing_active_push(order, platform)
    if stale is not None:
        stale_fields = _clear_stale_cancel_fields(stale)
        if stale_fields:
            try:
                stale.save(update_fields=[*stale_fields, "updated_at"])
            except IntegrityError as exc:
                # ⭐ H2（审计）：与 `refresh_push_status` 同一条 UPDATE 路径——
                # 清 `cancel_requested_at` 可能撞 `uniq_dailiantong_push_active`（并发窗口）。
                # 结构修复在 `_clear_stale_cancel_fields`；这里翻成可读中文，⛔ 不 500。
                if "uniq_dailiantong_push_active" not in str(exc):
                    raise
                raise DailiantongPushError(
                    "这张单的旧推送刚从「撤销中」回到「撤销已取消」，但同一平台已有一条"
                    "在途推送，旧行的本地撤销标记不能清（会撞唯一索引）；"
                    "下一轮同步会把它收口，请稍后重试重发。"
                ) from exc
            record(
                log_type=ACTION_LOG_TYPES["order.dailiantong_withdraw"],
                action="order.dailiantong_withdraw",
                actor=SYNC_ACTOR,
                actor_id=None,
                system=True,
                target_kind="dailiantong_push",
                target_id=str(stale.pk),
                target_name=order.order_no,
                order_no=order.order_no,
                game_name=(order.game.name or "") if order.game_id else "",
                reason=stale.remote_note,
            )
    # ⭐先把「远端已收口、本地还 sent」的旧行真正收成 closed，
    # 再判重发闸（否则撤销完成的旧行会一直挡重发，见 `finalize_finished_pushes`）。
    finalize_finished_pushes(order, platform=platform)
    replaces_push = _raise_if_already_pushed(order, platform)
    if replaces_push is not None and replaces_push.cancel_requested_at is None:
        # ⭐ L2（审计）：按 CancelUID 判定出的发起方回填，⛔ 不把打手发起的撤销误标成「我方发起」（唯一出处 `cancel_initiator_from_detail`）：
        # "us" ⇒ 确为我方提交 ⇒ 补记时刻（D8 一致依据）；"taker"/""（判不出）⇒ 不回填（补记会把「想同意的撤销」标成我方发起；宁可不放行也不写假事实）。
        # ⚠️ 不回填就不能重发——`cancel_requested_at` 非空正是旧行退出部分唯一索引的条件（D8 设计），不回填还新建行会撞库 ⇒ 显式拒并说清下一步。
        if replaces_push.cancel_initiator != CANCEL_INITIATOR_US:
            if replaces_push.cancel_initiator == CANCEL_INITIATOR_TAKER:
                raise DailiantongPushError(
                    "这张单在代练通侧的撤销是**打手发起**的，本地不能记成我方发起的协商"
                    "撤销（否则客服可能误点「取消撤销」）；请先在代练通「同意撤销」或等它"
                    "处理完，再重发。"
                )
            raise DailiantongPushError(
                "这张单在代练通侧已有撤销协商，但本地判不出发起方（拿不到 CancelUID）；"
                "为避免把打手发起的撤销误标成我方发起，本次不重发。"
                "请先到代练通后台核对这次撤销的处理结果，再重发。"
            )
        # 历史行只有远端 `CancelStatus=11` 且发起方是我方：真的重发前把本地依据补记下来。
        replaces_push.cancel_requested_at = timezone.now()
        replaces_push.save(update_fields=["cancel_requested_at", "updated_at"])

    # ⑤ 余额（唯一会打上游的一道前置闸；只读动作）
    # ⭐ A1（Rev.9 §1，实测）：只冻代练费 ⇒ `need` **只等于 price**，⛔ 不再加两份保证金。
    need = price
    client = _open_client(integration)
    try:
        _check_active_balance(client, need=need)

        # ── 载荷组装（全部在本地做长度兜底）────────────────────────────
        title = build_title(order)
        # ⭐ P8/P9：**页面填的「当前订单说明」优先**（一字不改上送）；
        # 没传这个键才回落「代练内容 + 客户原始需求 + 重要备注」的拼装（`build_memo`，FX1）。
        memo_text = build_memo(order) if memo is None else memo
        # 「代练/陪玩要求」：⭐ I3——**页面填的值优先**，空则用全局模板
        # （模板本身空 ⇒ 用既有占位文案）。超长按 `REQUIRE_TEMPLATE_MAX_LENGTH` 截断。
        requirement_text = (requirement or "").strip()
        if not requirement_text:
            requirement_text = require_template_from_settings()
        actors = build_actors(order, requirement=requirement_text)
        _remember_zone(order, zone_server_id)

        hall_note = ""
        if can_publish_to_hall:
            hall_note = ensure_internal_hall(
                order,
                operator=operator,
                hall_price=hall_price_same if hall_price_same is not None else price,
                hall_price_label=(
                    "同发内部抢单价" if hall_price_same is not None else "发平台价格"
                ),
            )

        push = _create_push_row(
            order,
            platform=platform,
            price=price,
            ensure1=ensure1,
            ensure2=ensure2,
            time_limit_hours=time_limit_hours,
            platform_only=not can_publish_to_hall,
            replaces_push=replaces_push,
        )
        # ⚠️ 内存属性（⛔ 不落库）：大厅补发那一句要随响应回给发单人。
        # 形状照 `order.release_request_cancelled` 的既有先例。
        push.hall_note = hall_note

        try:
            serial_no = client.add_order(
                zone_server_id=zone_server_id,
                title=title,
                time_limit=time_limit_hours,
                price=price,
                ensure1=ensure1,
                ensure2=ensure2,
                actors=actors,
                contact_name=_contact_name(operator, integration),
                # ⭐ 计划 §2.3：联系方式按订单号派生（稳定随机，⛔ 不再用配置里的联系字段）。
                mobile=_contact_mobile(order.order_no),
                qq=_contact_qq(order.order_no),
                limit_accept=limit_accept,
                power_level=power_level,
                # ⭐ P8/P9：页面填的说明原样上送；空串 = 这次不带备忘。
                memo=memo_text or None,
                # ⭐发单频道（`PUBLIC` / `GOODCHANNEL` / 内部分组 id）与两个标签。
                groups=groups,
                label1=label1,
                label2=label2,
                # ⭐ I3：页面「自动下架天数」走上游 `OffSellDays`
                # （上游默认 7 天；`None` = 不上送 ⇒ 用上游默认）。
                off_sell_days=off_sell_days,
                ex_order_no=order.order_no,
            )
        except DailiantongTransportError as exc:
            # ⛔ 不知道成没成 ⇒ unknown，且**不**在这里重试（客户端对写动作也不重试）。
            return _save_send_result(
                push,
                order,
                operator,
                state=DailiantongPush.State.UNKNOWN,
                serial_no=None,
                remote_status=None,
                last_error=exc.message,
                reason=f"状态未知：{exc.message}",
            )
        except DailiantongError as exc:
            # 上游**明确**说失败 ⇒ 可以改完再抛（failed 不挡下一次）。
            return _save_send_result(
                push,
                order,
                operator,
                state=DailiantongPush.State.FAILED,
                serial_no=None,
                remote_status=None,
                last_error=exc.message,
                reason=f"发单失败：{exc.message}",
            )

        serial_no = (serial_no or "").strip()
        if not serial_no:
            # 实测没出现过；但不能把「成功却没有单号」写成 sent（那会让对账永远找不到单）。
            return _save_send_result(
                push,
                order,
                operator,
                state=DailiantongPush.State.UNKNOWN,
                serial_no=None,
                remote_status=None,
                last_error="发单成功但上游没有返回订单号，请人工核对。",
                reason="状态未知：上游没有返回订单号",
            )

        # ⭐ 计划 §2.4：发单后回读核对门槛（没传门槛 ⇒ 零额外调用）。
        problem, unverified = _verify_thresholds(
            client, serial_no, power_level=power_level, limit_accept=limit_accept
        )
        reason = f"发单成功，上游单号：{serial_no}"
        if problem:
            reason = f"{reason}；{problem}"
        if unverified:
            reason = f"{reason}；{unverified}"
        result = _save_send_result(
            push,
            order,
            operator,
            state=DailiantongPush.State.SENT,
            serial_no=serial_no,
            # 实测发单成功后 `Status=11`（未接手，§6.1）——先记下来，T9c 再用详情回读刷新。
            remote_status=11,
            last_error=problem,
            reason=reason,
        )
        # 允许同发时，成功后再尝试让未入厅的单进入大厅。
        if can_publish_to_hall:
            waiting_note = _mark_waiting_grab_after_push(order, operator)
            if waiting_note:
                result.last_error = _clip_error(
                    "；".join(part for part in (result.last_error, waiting_note) if part)
                )
                result.save(update_fields=["last_error", "updated_at"])
        return result
    finally:
        _close(client)


# ── 对外：状态回读（T9c 定时任务会调）────────────────────────────────────


def _close_absent_push(
    push: DailiantongPush, message: str, *, evidence: str
) -> DailiantongPush:
    """P1：回读报「已被删除/没有找到/不存在/查不到」⇒ 把推送行收成远端终态（真机：人工删单后被当读取失败重试 5 次停轮询、本地停「已发送/未接手」说反话、单再也抛不出去）。四件事缺一不可：`state=closed`（不在 `ACTIVE_STATES`/唯一索引条件里 ⇒ 允许重抛）、`is_active=False`（不再每轮白打上游）、`remote_note` 写可读长期结论（`last_error` 每轮成功会被清空、装不下）、清 `last_error`/退避计数/下次回读时间。⚠️ 不改本地订单状态（P1 只收链路行，改它会与「已抛单禁止手改状态」闸互相打脸）；正常返回、下一轮不再轮询。"""
    detail = (message or "").strip() or evidence
    push.state = DailiantongPush.State.CLOSED
    push.is_active = False
    push.remote_note = _clip_note(
        f"代练通回读提示「{detail}」，判定远端订单已被删除 / 查不到，"
        "这条推送已关闭（停止轮询；如需重新发单可以再抛一次）。"
    )
    push.last_error = ""
    push.last_synced_at = timezone.now()
    push.sync_attempts = 0
    push.next_sync_at = None
    push.save(
        update_fields=[
            "state",
            "is_active",
            "remote_note",
            "last_error",
            "last_synced_at",
            "sync_attempts",
            "next_sync_at",
            "updated_at",
        ]
    )
    logger.info(
        "代练通远端订单查不到，推送行收成 closed push=%s serial=%s（%s）",
        push.pk,
        push.serial_no,
        evidence,
    )
    return push


def refresh_push_status(push: DailiantongPush, *, client=None) -> DailiantongPush:
    """回读平台详情并同步本地。平台已结算后，必须等本地回写成功才停轮询。"""
    if not push.serial_no:
        raise DailiantongPushError("这条推送还没有上游订单号，无法同步状态。")

    owns_client = client is None
    if owns_client:
        integration = _active_integration(push.platform)
        client = _open_client(integration)
    try:
        try:
            detail = client.order_detail(push.serial_no)
        except DailiantongError as exc:
            # ⭐ P1：上游说「这张单已经不存在」⇒ **远端终态**，不是读取失败。
            # 旧行为把它当失败重试 5 次后停轮询：推送行停在 `sent`、本地显示「已发送 /
            # 未接手」（说反话），而且这张单**再也抛不出去**（`sent` 占着部分唯一索引）。
            evidence = remote_absent_evidence(exc.message)
            if evidence:
                return _close_absent_push(push, exc.message, evidence=evidence)
            # 业务失败与传输失败都要留痕；`DailiantongTransportError` 是它的子类。
            push.last_error = _clip_error(exc.message)
            push.save(update_fields=["last_error", "updated_at"])
            raise

        status = _as_int(detail.get("Status"))
        cancel_status = _as_int(detail.get("CancelStatus"))
        # ⭐ 审计修复（P2-7）：detail 回包**两个状态都读不到**时，
        # ⛔ 不能当成功：既不写 last_synced_at，也不清 last_error（否则页面会显示
        # 「同步成功、无错误」，而其实什么都没读到）。
        if status is None and cancel_status is None:
            push.last_error = _clip_error(
                "代练通详情里没有可识别的 Status / CancelStatus，"
                "这次回读没有确认到远端状态（不回写同步时间）。"
            )
            push.save(update_fields=["last_error", "updated_at"])
            raise DailiantongPushError(push.last_error)
        changed = store_remote_fields(
            push, detail, status=status, cancel_status=cancel_status
        )
        # ⭐ dlt-d：接单打手 UID 是「拉黑打手」的唯一来源；
        # 详情里有值时补存（`store_remote_fields` 已存过的同名值不重复写）。
        accept_uid = str(detail.get("AcceptUID") or "").strip()
        if accept_uid and "accept_uid" not in changed:
            push.accept_uid = accept_uid
            changed = [*changed, "accept_uid"]
        if (
            push.remote_cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
            and push.remote_status not in TERMINAL_REMOTE_STATUSES
        ):
            # 只在撤销完成时提前退出活动列表，让回写可以清除占位接手人。
            push.is_active = False
            changed = [*changed, "is_active"]
        push.last_synced_at = timezone.now()
        push.last_error = ""
        # 回读成功 ⇒ 退避计数归零（P3-12）。
        push.sync_attempts = 0
        push.next_sync_at = None
        try:
            push.save(
                update_fields=[
                    *changed,
                    "last_synced_at",
                    "last_error",
                    "sync_attempts",
                    "next_sync_at",
                    "updated_at",
                ]
            )
        except IntegrityError as exc:
            # ⭐ H2（审计）：这条 UPDATE 唯一会撞的就是 `uniq_dailiantong_push_active`——`store_remote_fields`
            # 把 `cancel_requested_at` 清回 NULL（远端撤销被取消）时可能已有另一条 NULL 在途行（D8 重发的 B）
            # ⇒ 重新落进索引撞库；结构修复在 `_clear_stale_cancel_fields`，这里兜的是并发窗口（预检后 save 前又插进一条）：⛔ 不 500、也不伪装成「回读失败」——给可读中文，下一轮预检收口。
            if "uniq_dailiantong_push_active" not in str(exc):
                raise
            logger.warning(
                "代练通推送行清标撞到部分唯一索引 push=%s serial=%s（下一轮按 H2 收口）",
                push.pk,
                push.serial_no,
            )
            raise DailiantongPushError(
                "回读显示撤销申请已被取消，但同一订单在同一平台已有一条在途推送，"
                "这条旧推送的本地撤销标记不能清（否则会撞唯一索引）；"
                "下一轮同步会把它收口，页面稍后刷新即可。"
            ) from exc
        if "cancel_requested_at" in changed:
            # ⭐ C1：回读看到撤销被取消（10/13）⇒ 本地标记已清，留一条机器审计。
            record(
                log_type=ACTION_LOG_TYPES["order.dailiantong_withdraw"],
                action="order.dailiantong_withdraw",
                actor=SYNC_ACTOR,
                actor_id=None,
                system=True,
                target_kind="dailiantong_push",
                target_id=str(push.pk),
                target_name=push.order.order_no,
                order_no=push.order.order_no,
                game_name=(
                    (push.order.game.name or "") if push.order.game_id else ""
                ),
                reason=push.remote_note or "回读显示撤销申请已取消，已清除本地撤销标记",
            )

        apply_remote_status_after_readback(
            push, finalize_terminal=True, cancel_cleared="cancel_requested_at" in changed
        )
        return push
    finally:
        # ⚠️ 只关**本函数自己开的**那个客户端；注入的那份由调用方负责（见 docstring）。
        if owns_client:
            _close(client)


def apply_remote_status_after_readback(
    push: DailiantongPush, *, finalize_terminal=False, cancel_cleared=False
) -> str:
    """应用已保存的回读结果。本地写入失败时保留轮询和可读错误。"""
    terminal = (
        push.remote_status in TERMINAL_REMOTE_STATUSES
        or push.remote_cancel_status in TERMINAL_REMOTE_CANCEL_STATUSES
    )
    try:
        note = apply_remote_status(push)
    except Exception as exc:  # noqa: BLE001 —— 外部已成功，本地失败单独留痕
        logger.warning(
            "代练通远端状态回写本地失败 push=%s serial=%s（%s）",
            push.pk,
            push.serial_no,
            type(exc).__name__,
        )
        message = str(exc) or type(exc).__name__
        push.last_error = _clip_error(
            f"远端状态已读到（{_status_label(push.remote_status)}），"
            f"但本地订单状态回写失败：{message}"
        )
        if terminal:
            push.is_active = True
        updated = DailiantongPush.objects.filter(pk=push.pk).exclude(
            state=DailiantongPush.State.CLOSED
        ).update(last_error=push.last_error, is_active=push.is_active, updated_at=timezone.now())
        if not updated:
            push.refresh_from_db(fields=["state", "last_error", "is_active"])
        return ""

    cancel_note = "回读显示撤销申请已被取消（CancelStatus={}），已清除本地撤销标记"
    if finalize_terminal and terminal:
        reasons = [note] if note else []
        if cancel_cleared:
            reasons.append(cancel_note.format(push.remote_cancel_status))
        if not reasons:
            reasons.append(
                "远端已到终态（Status="
                f"{push.remote_status if push.remote_status is not None else '未返回'} / "
                f"CancelStatus={push.remote_cancel_status if push.remote_cancel_status is not None else '未返回'}），"
                "这条推送已收口"
            )
        push.state = DailiantongPush.State.CLOSED
        push.is_active = False
        push.last_error = ""
        push.remote_note = _clip_note("；".join(reasons))
        updated = DailiantongPush.objects.filter(pk=push.pk).exclude(
            state=DailiantongPush.State.CLOSED
        ).update(
            state=push.state, is_active=False, last_error="",
            remote_note=push.remote_note, updated_at=timezone.now(),
        )
        if not updated:
            push.refresh_from_db(fields=["state", "is_active", "last_error", "remote_note"])
    elif note:
        push.remote_note = _clip_note(
            f"{note}；{cancel_note.format(push.remote_cancel_status)}" if cancel_cleared else note
        )
        push.save(update_fields=["remote_note", "updated_at"])
    return note


# ── 对外：区服字典（带模块级 TTL 缓存）──────────────────────────────────


_zones_lock = threading.Lock()
#: `(抓取时刻 monotonic, 上游原始行)`。`None` / 过期 ⇒ 重新拉。
_zones_cache: tuple[float, tuple[dict, ...]] | None = None


def clear_zones_cache() -> None:
    """清空区服缓存（测试用；生产不需要手工清）。"""
    global _zones_cache
    with _zones_lock:
        _zones_cache = None


def _fetch_zone_rows() -> tuple[dict, ...]:
    integration = _active_integration(DEFAULT_PLATFORM)
    client = _open_client(integration)
    try:
        return tuple(client.zones().rows)
    finally:
        _close(client)


def _cached_zone_rows(*, force_refresh: bool = False) -> tuple[dict, ...]:
    global _zones_cache
    now = time.monotonic()
    with _zones_lock:
        cached = _zones_cache
    if (
        not force_refresh
        and cached is not None
        and now - cached[0] < ZONES_CACHE_TTL_SECONDS
    ):
        return cached[1]

    rows = _fetch_zone_rows()
    with _zones_lock:
        _zones_cache = (time.monotonic(), rows)
    return rows


def _zone_value(row: dict, *names: str) -> str:
    """取上游行里的值（键的大小写不保证；`ServerName` 实测是这个写法）。"""
    for name in names:
        if name in row and row[name] is not None:
            return str(row[name])
    return ""


def list_zones(query: str = "", *, force_refresh: bool = False) -> list[dict]:
    """代练通全区服字典，按关键字过滤后返回 `[{zone_server_id, server_name}]`。模块级 TTL 缓存（`ZONES_CACHE_TTL_SECONDS`
    10 分钟）：4327 条一次拉完、本地过滤（⛔ 不每敲一个字打一次上游）；关键字匹配 `ServerName`（不区分大小写）或区服编码、
    空=全部；顺序保留上游返回顺序（「英雄联盟/电信/艾欧尼亚」层级在名字里，排序交给页面）。"""
    rows = _cached_zone_rows(force_refresh=force_refresh)
    keyword = (query or "").strip().lower()
    result: list[dict] = []
    for row in rows:
        zone_server_id = _zone_value(row, "ZoneServerID", "zoneserverid", "ZoneServerId")
        server_name = _zone_value(row, "ServerName", "servername")
        if not zone_server_id:
            continue
        if keyword and keyword not in server_name.lower() and keyword not in zone_server_id.lower():
            continue
        result.append(
            {"zone_server_id": zone_server_id, "server_name": server_name}
        )
    return result


# ── 对外：默认区服（一键抛单计划 §2.3 / B5）────────────────────────────

#: 「默认区服」的三种来源（接口原样回给页面；页面据此决定要不要显示「更换」提示）。
ZONE_SOURCE_MAP = "map"       # 本游戏选过的映射（`DailiantongZoneMap`）优先
ZONE_SOURCE_FIRST = "first"   # 字典里与游戏名**精确/归一化同名**的**第一条**（原序，B3）
ZONE_SOURCE_NONE = "none"     # 匹配不到


#: ⭐ B3：游戏名归一化时要剔掉的**修饰词**。
#: 代练通字典里的游戏名常常带这些尾巴（「王者荣耀国际服」「英雄联盟手游」），
#: 而我们的游戏名是干净的那个 ⇒ 不做这一步就永远匹配不到（下拉 0 个选项、发不出单）。
GAME_NAME_NOISE_WORDS = (
    "国际服",
    "国服",
    "手游",
    "端游",
    "怀旧服",
    "经典服",
    "正式服",
    "体验服",
    "重制版",
    "重置版",
)


def _game_name_head(server_name: str) -> str:
    """代练通 `ServerName` 的第一段（字典形如「王者荣耀/安卓QQ区」⇒ `王者荣耀`）；本仓只认 `/` 分隔（实测字典就是这个形状），没有 `/` 时整串就是游戏名。"""
    return (server_name or "").split("/", 1)[0].strip()


def normalize_game_name(value: str) -> str:
    """游戏名归一化：去空白/括号内容/常见修饰词，再 casefold（B3 第二级匹配用）。只服务「同一个游戏的不同写法」：
    ⛔ 不做模糊/包含/编辑距离匹配——会把「王者荣耀世界」判成「王者荣耀」（B3 原始缺陷就是前缀匹配混进别的游戏）。"""
    text = str(value or "").casefold()
    # 去掉中英文括号及其内容（「王者荣耀（手游）」这类写法）。
    text = re.sub(r"[（(][^（）()]*[）)]", "", text)
    # 去掉所有空白与常见分隔符。
    text = re.sub(r"[\s·・_\-—]+", "", text)
    for word in GAME_NAME_NOISE_WORDS:
        text = text.replace(word.casefold(), "")
    return text


def _zones_matching_game_name(rows, game_name: str) -> list[dict]:
    """按我方游戏名匹配上游字典（B3 三级口径，保留字典原序）：① 精确同名（`ServerName` 第一段与游戏名相等，忽略大小写）；
    ② 归一化后同名（两边去空格/括号内容/修饰词再精确比）；③ 都不中 ⇒ 空列表（页面要求手选）；返回精确档在前、归一化档在后
    ⇒ 默认值优先拿精确同名那条。⛔ 不许 `startswith`（实测「王者荣耀」会命中「王者荣耀世界」并被当默认写进映射表一直错下去）、不许包含/模糊匹配：宁可匹配不到要求手选，也不猜。"""
    raw = (game_name or "").strip()
    if not raw:
        return []
    wanted_exact = raw.casefold()
    wanted_folded = normalize_game_name(raw)

    exact: list[dict] = []
    folded: list[dict] = []
    for row in rows:
        zone_server_id = _zone_value(
            row, "ZoneServerID", "zoneserverid", "ZoneServerId"
        )
        server_name = _zone_value(row, "ServerName", "servername")
        if not zone_server_id:
            continue
        head = _game_name_head(server_name)
        item = {"zone_server_id": zone_server_id, "server_name": server_name}
        if head.casefold() == wanted_exact:
            exact.append(item)
        elif wanted_folded and normalize_game_name(head) == wanted_folded:
            folded.append(item)
    # 精确在前（默认值优先拿它），归一化在后（下拉仍可看到同游戏的另一种写法）。
    return exact + folded


def list_zones_for_game(game_id: int) -> list[dict]:
    """某款我方游戏在代练通字典里的全部区服（字典原序；匹配不到=空列表），供「更换区服」下拉使用。
    游戏不存在/匹配不到 ⇒ `[]`（⛔ 不是错误：页面自己提示手动选）；字典拉不到 ⇒ 让异常冒给接口层（⛔ 不假装空）；
    模块缓存由 `_cached_zone_rows` 统一负责（与 `/zones/?q=` 同一份）；只读本租户库 `Game` 表，⛔ 不接受 tenant 参数。"""
    game = Game.objects.filter(pk=game_id).first()
    if game is None:
        return []
    return _zones_matching_game_name(_cached_zone_rows(), game.name)


def _zone_row_for_id(rows, zone_server_id: str) -> dict | None:
    for row in rows:
        if _zone_value(row, "ZoneServerID", "zoneserverid", "ZoneServerId") == zone_server_id:
            return row
    return None


def default_zone_for_game(game_id: int) -> dict:
    """一张单的默认代练通区服 `{zone_server_id, server_name, source}`。取值顺序（计划 §2.3/B5，⛔ 不要改顺序）：
    ① `DailiantongZoneMap` 该游戏最近一次选过的区服（`map`）；② 字典按我方游戏名匹配（B3 三级：精确→归一化→不中），命中列表第一条（`first`——「第一条」是妥协，同一游戏多区时可能不是想要的，页面必须显示选中的区服）；③ 都不中 ⇒ `none`。⚠️ 字典是上游数据（10 分钟缓存）：拉不到时有映射仍回映射、没映射让异常冒给接口层翻中文（⛔ 不假装「匹配不到」——「上游没这游戏」与「上游暂时不可用」是两件事）；只读本租户库，⛔ 不接受 tenant 参数。"""
    game = Game.objects.filter(pk=game_id).first()
    if game is None:
        return {
            "zone_server_id": None,
            "server_name": None,
            "source": ZONE_SOURCE_NONE,
        }

    mapping = (
        DailiantongZoneMap.objects.filter(game_id=game.pk)
        .order_by("-updated_at", "-id")
        .first()
    )
    try:
        rows = _cached_zone_rows()
    except (DailiantongPushError, DailiantongError):
        if mapping is None:
            raise
        rows = ()

    if mapping is not None:
        row = _zone_row_for_id(rows, mapping.zone_server_id)
        return {
            "zone_server_id": mapping.zone_server_id,
            "server_name": _zone_value(row, "ServerName", "servername") if row else "",
            "source": ZONE_SOURCE_MAP,
        }

    matches = _zones_matching_game_name(rows, game.name)
    if matches:
        return {
            "zone_server_id": matches[0]["zone_server_id"],
            "server_name": matches[0]["server_name"],
            "source": ZONE_SOURCE_FIRST,
        }
    return {
        "zone_server_id": None,
        "server_name": None,
        "source": ZONE_SOURCE_NONE,
    }


# ── 对外：右面板账户摘要（一键抛单计划 §2.3 / B6）──────────────────────

#: 账户摘要（平台 / 账号 / 可用余额 / 冻结余额）的缓存时长（秒）。
#: 业务口径是「实时查、30s 缓存」：发单页每次打开/聚焦都读它，⛔ 不能每读一次打一次上游。
SUMMARY_CACHE_TTL_SECONDS = 30.0

_summary_lock = threading.Lock()
#: `(schema, 配置行 pk) -> (抓取时刻 monotonic, 摘要 dict)`。
#: ⚠️ 键里**必须带 schema**（多租户共用一个 Python 进程；只按 pk 会把 A 的余额给 B）。
_summary_cache: dict[tuple[str, int], tuple[float, dict]] = {}


def clear_summary_cache() -> None:
    """清空账户摘要缓存（测试用；生产不需要手工清）。"""
    with _summary_lock:
        _summary_cache.clear()


def _summary_base(integration=None) -> dict:
    """摘要的固定骨架（成功 / 失败两条路都补齐同样的键，页面不用判空分支）。"""
    platform = DEFAULT_PLATFORM
    return {
        "platform": platform,
        "platform_label": str(PlatformIntegration.Platform(platform).label),
        "account": (integration.account or "").strip() if integration is not None else "",
        "configured": integration is not None,
        "available_balance": None,
        "frozen_balance": None,
        "error": "",
    }


def _parse_money(value) -> Decimal | None:
    """把上游金额（int/float/字符串）归一成 `Decimal`；解析不了返回 `None`。全程 Decimal（`str()` 中转，⛔ 不 `Decimal(float)`）、
    ⛔ 不用真值判边界。"""
    if value is None:
        return None
    try:
        return _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def platform_account_summary(*, force_refresh: bool = False) -> dict:
    """发单页右面板账户摘要 `{platform, platform_label, account, configured, available_balance, frozen_balance, error}`。
    无配置 ⇒ `configured=False`+可读中文 `error`（含下一步）；查询失败 ⇒ 余额两格 `None`+可读中文（⛔ 不带凭据）；成功 ⇒ 两位小数字符串（`null` 只在失败时出现，⛔ 不拿 `"0.00"` 冒充「查不到」）。成功与失败都缓存 30 秒（发单页反复刷新不打爆上游），
    缓存键带当前 schema（多租户硬要求：只按 pk 会把 A 的余额给 B）；⚠️ 无配置不缓存（没有上游调用）；凭据红线：返回值只有平台名/账号/金额/中文原因，⛔ 没有 `app_secret`/`pay_password_md5`/任何 `GetSTS` 三件套。"""
    try:
        integration = _active_integration(DEFAULT_PLATFORM)
    except DailiantongPushError as exc:
        return _summary_base() | {"error": str(exc)}

    schema = (getattr(db_connection, "schema_name", "") or "").strip() or "public"
    cache_key = (schema, int(integration.pk))
    if not force_refresh:
        with _summary_lock:
            cached = _summary_cache.get(cache_key)
        if (
            cached is not None
            and time.monotonic() - cached[0] < SUMMARY_CACHE_TTL_SECONDS
        ):
            return dict(cached[1])

    summary = _summary_base(integration)
    client = None
    try:
        client = _open_client(integration)
        money = client.user_money()
        if not isinstance(money, dict):
            summary["error"] = (
                "代练通没有返回可识别的余额数据。请稍后重试，或到代练通后台核对账户。"
            )
        else:
            available = _parse_money(money.get("ActiveBal"))
            frozen = _parse_money(money.get("FreezeBal"))
            if available is None or frozen is None:
                summary["error"] = (
                    "代练通返回的余额不是有效金额。请稍后重试，或到代练通后台核对账户。"
                )
            else:
                summary["available_balance"] = _fmt_money(available)
                summary["frozen_balance"] = _fmt_money(frozen)
    except DailiantongPushError as exc:
        summary["error"] = str(exc)
    except DailiantongError as exc:
        summary["error"] = f"查询代练通余额失败：{exc.message}"
    except Exception as exc:  # noqa: BLE001 —— 存储 / 客户端构造之外的意外也要可读
        # ⛔ 只记类型名：异常文本可能带请求细节（凭据不进任何文案）。
        summary["error"] = f"查询代练通余额失败（{type(exc).__name__}）"
    finally:
        if client is not None:
            _close(client)

    with _summary_lock:
        _summary_cache[cache_key] = (time.monotonic(), dict(summary))
    return summary


# ── 对外：余额不足预警（运营配套）──────────────────────────────


def _pending_push_price(order):
    """这张单「如果要抛代练通」会占用多少可用余额（`Decimal`/`None`=没定价）。口径=`default_push_price`（发单价优先 →
    平台价 → 无）、只认 >0；与 `push_order` 余额闸同源（A1 实测：只冻代练费，⛔ 不加两份保证金）。"""
    price = default_push_price(order)
    if price is None:
        return None
    return _to_decimal(price)


def balance_alert(*, force_refresh: bool = False) -> dict:
    """余额不足预警（只读）：`{available, frozen, needed, shortfall, is_low, ...}`。「未来 24h 待发单所需」= 当前挂大厅/待分配
    且还没抛到代练通（无有效推送）的 `UNASSIGNED`/`WAITING_GRAB` 单，按各自发单价（`hall_price`→`platform_price`，只认 >0）逐张加总——本仓没有「定时批量抛单」表（抛单是人点的），只能用「现在就能抛、还没抛的单」近似；已有推送=钱已冻结过 ⛔ 不重复计入；没价格的单不计入（单独回 `unpriced_count` 让页面提示）；`is_low = available < needed`、`shortfall = needed - available`（>0 才算缺口）。⚠️ 只读（不写库、不发单、不进审计）；唯一上游调用是 `UserMoney`（复用 `platform_account_summary` 的 30 秒缓存）；无配置/上游失败 ⇒ `error` 可读中文、金额两格 `null`（⛔ 不说「余额充足」）。"""
    from apps.business.models import Order

    total = Decimal("0.00")
    priced = 0
    unpriced = 0
    orders = (
        Order.objects.filter(
            status__in=(OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB),
            deleted_at__isnull=True,
        )
        .exclude(
            dailiantong_pushes__state__in=DailiantongPush.ACTIVE_STATES,
        )
        .distinct()
        .order_by("id")
    )
    for order in orders.iterator():
        price = _pending_push_price(order)
        if price is None:
            unpriced += 1
            continue
        priced += 1
        total += price

    summary = platform_account_summary(force_refresh=force_refresh)
    available = _parse_money(summary.get("available_balance"))
    frozen = _parse_money(summary.get("frozen_balance"))

    alert = {
        "platform": DEFAULT_PLATFORM,
        "platform_label": summary.get("platform_label", ""),
        "account": summary.get("account", ""),
        "configured": summary.get("configured", False),
        # 未来 24h 待发单所需（本函数的口径，见 docstring）
        "needed": _fmt_money(total),
        "pending_orders": priced,
        "unpriced_orders": unpriced,
        # 账户余额（与右面板同源）
        "available": None if available is None else _fmt_money(available),
        "frozen": None if frozen is None else _fmt_money(frozen),
        "is_low": False,
        "shortfall": "0.00",
        "error": summary.get("error", ""),
    }
    if available is None:
        # 查不到余额 ⇒ ⛔ 不判断「够不够」（说「充足」是假话），把原因交给页面。
        return alert

    shortfall = total - available
    alert["is_low"] = available < total
    alert["shortfall"] = _fmt_money(shortfall if shortfall > 0 else Decimal("0.00"))
    if alert["is_low"]:
        alert["message"] = (
            f"代练通可用余额 {_fmt_money(available)} 元，低于未来 24 小时待发单所需的 "
            f"{_fmt_money(total)} 元（缺口 {alert['shortfall']} 元），"
            "请先充值，否则抛单会因「可操作资金不足」失败。"
        )
    elif unpriced > 0:
        alert["message"] = (
            f"可用余额 {_fmt_money(available)} 元，覆盖已定价的 {priced} 张待发单"
            f"（{_fmt_money(total)} 元）；另有 {unpriced} 张待发单还没定价，未计入。"
        )
    else:
        alert["message"] = (
            f"可用余额 {_fmt_money(available)} 元，覆盖未来 24 小时待发单所需的 "
            f"{_fmt_money(total)} 元。"
        )
    return alert
