"""代练通「**有、我们没用上**」的那批动作（dlt-d）。本模块只回答一件事：**这张已经抛到代练通的单，还能再做哪个平台动作、做完怎么算数**。协议层在 `dailiantong_client.py`（签名 / 信封 / 重试红线），前置闸与回读的公共件在 `dailiantong_actions.py`（本模块**复用**它们，⛔ 不复制第二套）。本模块自己的两份新东西是： 1. 八个动作的服务函数（加价 / 客服介入 / 评价 / 上下架 / 锁单 / 拉黑 / 按日统计 / 代练通侧订单要求模板）； 2. **代练通侧模板**那五条（列表 / 新增 / 修改 / 删除 / 设默认）——⚠️ 它与本仓自己的「代练要求模板」（`PlatformPushSettings` 里那一份）是**两回事**，⛔ 别混名。 ## 逐动作的前提 / 回读判据（真机事实见 `docs/DAILIANTONG-API.md` §4 / §6.2） | 动作 | 上游 | 写前前提（本地先挡） | 成败判据（回读） | |---|---|---|---| | 加价 | `LevelOrderUpPrice` | **只有未接手（`Status=11`）**（§4 #34） | 详情 `Price` == 原价 + 加价额 | | 客服介入 | `LevelOrderRequestArbitration` | **协商撤销中（`CancelStatus=11`）且转入该状态满 1 小时**（真机定案 2026-10-28；其余状态一律被上游拒） | 详情 `CancelStatus ∈ {14,15}` 或 `Status=14` |
| 评价 | `LevelOrderValue` | **只有已结算（`Status=17`）**（§4 #11 「未达最终状态」） | ⛔ 上游没有评价读面 ⇒ 只认成功码 | | 上架 / 下架 | `LevelOrderOffSell` | **下架**：只有未接手（`Status=11`）；**上架**：未接手 **或已下架（`Status=18`）**。两个方向都要 `IsPub` 与目标不冲突 | 详情 `IsPub` 变成期望值 | | 锁定 / 解锁 | `LevelOrderLock` | 锁：**正在代练 / 等待验收 / 订单异常（`Status=12/13/14`）**；解：**只有已锁定（`Status=15`）** | 详情 `Status` 变成 15（锁）；解锁回到**锁定前的状态**（12/13/14，不写死 12） | | 拉黑打手 | `AddBlackList` | 推送行里要有接单打手 `accept_uid` | ⛔ 上游没有黑名单读面 ⇒ 只认成功码 | | **同意撤销**（h13） | `LevelOrderCancel(Flag=2)` | 推送行 `remote_cancel_status=11`（写前详情复核）；两个金额取详情现值、空按 0 | 详情 `CancelStatus ∈ {12,15,16}` | | 按日统计 | `LevelOrderStat` | 无（只读） | 上游回包；**带缓存** |
| 模板五条 | `OrderRequireTemp*` | 入参形状（标题 / 内容长度、模板 ID 是整数） | 列表回读：目标行存在 / 消失 / 字段值 | - **写动作一律不自动重试**（客户端红线）；超时 / 连接失败按「**结果未知**」说清楚。 - **评价 / 拉黑**确实没有可回读的读面（详情与列表都不回吐评价 / 黑名单）⇒ 只认上游成功码。这是**如实登记**的例外，⛔ 不假装回读、也不把「没核验」写成「已核验」。 - `LevelOrderStat` 的回包不是 `<Action>` 列表信封，而是 `LevelOrderStatList` + `LevelOrderStatTotal` 两个键的对象（`docs/DAILIANTONG-API.md` §4 #27）⇒ 归一化在这里做。 ## 失败分层（⛔ 不要 500） | 场景 | 表现 | |---|---| | 本地前置闸（没有推送行 / 没有上游单号 / 状态不允许 / 入参越界） | `DailiantongPushError`，**零上游写调用** | | 上游**明确**回业务失败 | `DailiantongPushError`，文案含上游 `Err` **原文** | | 超时 / 连接失败 | `DailiantongPushError`，按「**未知**」说清楚，⛔ 不重试写动作 | | 写成功但**回读失败 / 回读不符** | `DailiantongPushError`，文案是「**已提交但没核验到**」，⛔ 不报成功 | ## 凭据红线（`AGENTS.md` §4）审计只写单号 / 金额 / 状态摘要；`app_secret` / `pay_password_md5` / 派生的 `PayPass` 由客户端发送时现算，本模块**从不**接触。上游详情里的明文 `GamePass` 也不进审计 / 日志 / 异常文案——本模块只取 `Status` / `CancelStatus` / `Price` / `IsPub` / `AcceptUID` / `TimeLimit` 几个标量。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.dailiantong_actions import (
    AUDIT_WITHDRAW,
    _active_integration,
    _close,
    _guard_terminal,
    _latest_active_push,
    _open_client,
    _read_detail,
    _readback_detail,
    _require_operator,
    _save_remote_snapshot,
    _status_label,
)
from apps.business import dailiantong_qr as qr_module
from apps.business import dailiantong_push as push_module
from apps.business.dailiantong_client import (
    BLACKLIST_FLAG_ADD,
    BLACKLIST_FLAG_REMOVE,
    CANCEL_FLAG_AGREE,
    CANCEL_FLAG_CANCEL,
    EVAL_TYPES,
    EVAL_TYPE_BAD,
    EVAL_TYPE_NORMAL,
    EVAL_TYPE_SATISFIED,
    EVAL_TYPE_VERY_SATISFIED,
    LOCK_FLAG_LOCKED,
    LOCK_FLAG_UNLOCKED,
    OFF_SELL_FLAG_OFF,
    OFF_SELL_FLAG_ON,
    SPECIAL_KIND_AMOUNT,
    TEMP_IS_DEFAULT,
    TEMP_IS_NOT_DEFAULT,
    DailiantongError,
    DailiantongTransportError,
    OssUploadError,
    put_object,
)
from apps.business.dailiantong_push import (
    DEFAULT_PLATFORM,
    MIN_PRICE,
    DailiantongPushError,
    _as_int,
    _check_active_balance,
    _fmt_money,
    _to_decimal,
)
from apps.business.models import DailiantongPush
from apps.core.storage import IMAGE_EXT_BY_CONTENT_TYPE, sniff_image_bytes

# ── 审计动作码（登记在 `apps/audit/actions.py`；⛔ 不带凭据）──────────────

AUDIT_UP_PRICE = "order.dailiantong_up_price"
AUDIT_ARBITRATION = "order.dailiantong_arbitration"
AUDIT_VALUE = "order.dailiantong_value"
AUDIT_OFF_SELL = "order.dailiantong_off_sell"
AUDIT_LOCK = "order.dailiantong_lock"
AUDIT_BLACKLIST = "order.dailiantong_blacklist"
AUDIT_TEMPLATE = "order.dailiantong_template"
#: ⭐（详情页三个补充动作）：补款 / 编辑平台单 / 追加进度图。
AUDIT_AMOUNT = "order.dailiantong_amount"
AUDIT_EDIT = "order.dailiantong_edit"
AUDIT_PROGRESS = "order.dailiantong_progress"
#: ⭐ h13：**同意撤销**（`LevelOrderCancel(Flag=2)`，打手申请撤销后我方同意）。
AUDIT_AGREE_CANCEL = "order.dailiantong_agree_cancel"

#: 详情页「追加截图」的对象名标签（与 `build_object_key` 的默认 `qr` 区分：
#: 接手后自动发的图 vs 客服手工补的图）。
SCREENSHOT_OBJECT_TAG = "shot"
#: 追加截图没填说明时的默认正文（⛔ 不空着；上游 `Msg` 是必填的展示文案）。
PUSH_SCREENSHOT_DEFAULT_CAPTION = "客服补充的进度截图"
#: 追加截图的说明长度上限（上游没有明说，本地先挡；与留言的 512 同量级）。
PUSH_SCREENSHOT_CAPTION_MAX_LENGTH = 200

#: 订单动作的审计 `target_kind`（与抛单线同值；列宽 16，必须**恰好** ≤16）。
AUDIT_TARGET_KIND = "dailiantong_push"
#: 模板动作没有订单行，`target` 指向「代练通侧模板」这一族（列宽 16 ⇒ 15 字符）。
AUDIT_TEMPLATE_TARGET_KIND = "dailiantong_temp"

# ── 远端码（`docs/DAILIANTONG-API.md` §5）──────────────────────────────────

REMOTE_STATUS_UNACCEPTED = 11
REMOTE_STATUS_IN_PROGRESS = 12
REMOTE_STATUS_WAITING_ACCEPTANCE = 13
REMOTE_STATUS_ABNORMAL = 14
REMOTE_STATUS_LOCKED = 15
REMOTE_STATUS_CANCELLING = 16
REMOTE_STATUS_SETTLED = 17
#: `Status=18` **已下架**（上游码表没列 18，这一档是**真机观测**）：
#: 单 `41017966281869299594` 被下架后回 `Status=18`，再用 `LevelOrderOffSell(Flag=1)`
#: 能上架回 `11` ⇒ 18 是事实态。⛔ 别把它当异常码（那正是「下架后永远上不了架」的成因）。
REMOTE_STATUS_OFF_SHELF = 18

#: ⛔ **已删除**：原有一个 `ARBITRATION_STATUSES`（12/13/14/15/16 视为「可以申请客服介入」）。真机定案后这条**不成立**（12 / 13 / 16+cancel12 / 17 全部被上游拒，
#: 见 `docs/DAILIANTONG-API.md` §6.9）⇒ 判据改成「`CancelStatus=11` 且转入满 1 小时」，
#: 唯一实现是 `dailiantong_push.arbitration_availability`（⛔ 不在这里再写一份）。

#: `CancelStatus` 的「客服介入中 / 客服已处理」（申请介入的回读证据）。
CANCEL_STATUS_ARBITRATION = 14
CANCEL_STATUS_ARBITRATION_HANDLED = 15
CANCEL_STATUS_ARBITRATION_EVIDENCE = frozenset(
    {CANCEL_STATUS_ARBITRATION, CANCEL_STATUS_ARBITRATION_HANDLED}
)

#: 允许「补款」的远端状态（`LevelOrderModify_Special` 的 `Kind=0`）。 ⭐ 真机（两次都测了）： - **正在代练（12）**：补款 1 元 ⇒ `{"Result":1,"Err":"修改成功."}`、回读 `Price 2 → 3`；
#: - **待验收（13）**：补款 1 元 ⇒ `{"Result":1,"Err":"修改成功."}`、回读 `Price 3 → 4`； - 小数 / 0 元分别是 `-5 补款金额不能含有小数！.` / `-3 补款金额不能低于1元.`。
#: ⛔ 未接手（11）不在内：补款不是加价（加价走 `up_price_push`，只有 11）。 14/15/16 是异常 / 锁定 / 撤销过程态，没有实测依据 ⇒ 本地先挡，不拿客户的钱去试。
AMOUNT_STATUSES = frozenset(
    {REMOTE_STATUS_IN_PROGRESS, REMOTE_STATUS_WAITING_ACCEPTANCE}
)

# ── 上架 / 下架的状态闸（⭐ 真机放宽）────────────────────────────

#: 上下架方向 → (允许的远端状态, 那句准入的**中文口径**)。 ⭐ 真机（单 `41017966281869299594`）：这张单被**下架**后上游回 `Status=18` （上游码表没这一档；我方轮询同步是通的、`remote_status` 确实写成了 18），我方旧闸只认
#: `Status=11` ⇒ **下架后永远上不了架**（业务方实测被卡）。用原始接口 `LevelOrderOffSell(Flag=1)` 实测能把它上架回 `Status=11` ⇒ 上游允许，本地闸必须放行。两个方向**准入不同**（⛔ 别合成一个集合）：
#: - **下架**（`Flag=0`）：只有未接手 11。18 已经是「已下架」（回读 `IsPub=0`），再下架没有意义； - **上架**（`Flag=1`）：11 / 18 都行 —— 这两档就是上游自己的准入口径，原话见 `docs/DAILIANTONG-API.md` §4 `LevelOrderModify` 的拒绝文案「`-1 只有未接手或已下架的订单才可以修改`」（上游把「未接手 / 已下架」当同一档可改状态）。
OFF_SELL_ALLOWED = {
    OFF_SELL_FLAG_OFF: (frozenset({REMOTE_STATUS_UNACCEPTED}), "未接手"),
    OFF_SELL_FLAG_ON: (
        frozenset({REMOTE_STATUS_UNACCEPTED, REMOTE_STATUS_OFF_SHELF}),
        "未接手或已下架",
    ),
}

# ── ⭐：协商撤销满 24 小时 ⇒ 自动申请客服介入（`dailiantong_sync` 每轮）──

#: `CancelStatus=10` 未撤销（取消撤销成功的目标档）。
CANCEL_STATUS_NOT_CANCELLED = 10
#: `CancelStatus=11`「协商撤销中」＝ 自动仲裁的候选档（业务方口径）。
CANCEL_STATUS_NEGOTIATING = 11
#: 撤销申请满 1 小时之前**不许调仲裁**：上游会拒（白打一次请求）。
AUTO_ARBITRATE_MIN_AGE = timedelta(hours=1)
#: 撤销申请满 24 小时仍未处理 ⇒ 自动进入仲裁（业务方默认口径）。
AUTO_ARBITRATE_AFTER = timedelta(hours=24)
#: 失败退避：每 6 小时再试一次（⛔ 不每 30 秒刷屏）。
AUTO_ARBITRATE_RETRY = timedelta(hours=6)
#: 失败上限：到这个次数就停手，可读原因留在 `auto_arbitrate_note`（等人工处理）。
AUTO_ARBITRATE_MAX_ATTEMPTS = 5

#: 自动仲裁段的三种结果（`auto_arbitrate_one` 的 `status`）。
AUTO_ARBITRATE_SKIPPED = "skipped"
AUTO_ARBITRATE_DONE = "done"
AUTO_ARBITRATE_FAILED = "failed"

#: `auto_arbitrate_note` 的列宽（模型列宽的唯一出处）。
AUTO_ARBITRATE_NOTE_MAX_LENGTH = DailiantongPush._meta.get_field(
    "auto_arbitrate_note"
).max_length

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoArbitrateOutcome:
    """一次自动仲裁尝试的结论：`skipped`（闸没过）/ `done`（已收口）/ `failed`（退避）。"""

    status: str
    message: str


# ── 业务常量 ────────────────────────────────────────────────────────────

#: 评价内容上限（文档 `Comment` String(512)）。
VALUE_COMMENT_MAX_LENGTH = 512
#: 锁单原因上限（文档 `Reason` String[128]）。
LOCK_REASON_MAX_LENGTH = 128
#: 代练通侧模板标题上限（文档只写 `Title`，没写长度；本地先挡 64，内容按文档 1024）。
ORDER_TEMPLATE_TITLE_MAX_LENGTH = 64
ORDER_TEMPLATE_CONTENT_MAX_LENGTH = 1024
#: 代练通侧模板每账号上限（文档原文「每人最多10个模板」）。
ORDER_TEMPLATE_MAX_COUNT = 10

#: 按日统计：默认窗口 = 最近 7 天（含今天）；缓存 60 秒（页面按日看，不需要秒级）。
STATS_DEFAULT_DAYS = 7
STATS_CACHE_TTL_SECONDS = 60.0
#: 统计的 `IsPub`：1 发单 / 0 接单（文档原文）。
STATS_SIDE_PUBLISH = 1
STATS_SIDE_ACCEPT = 0
STATS_SIDES = frozenset({STATS_SIDE_PUBLISH, STATS_SIDE_ACCEPT})

#: `EvalType` → 中文（只服务审计与人话；⛔ 不做分支判据）。
EVAL_TYPE_LABELS = {
    EVAL_TYPE_VERY_SATISFIED: "非常满意",
    EVAL_TYPE_SATISFIED: "满意",
    EVAL_TYPE_NORMAL: "一般",
    EVAL_TYPE_BAD: "很差",
}


# ── 小工具 ──────────────────────────────────────────────────────────────


def _record_action(
    push: DailiantongPush,
    order,
    operator,
    *,
    action: str,
    reason: str,
    from_value: str = "",
    to_value: str = "",
    target_name: str = "",
    system: bool = False,
) -> None:
    """写一条平台动作审计（`target` 指向推送行；⛔ 不含任何凭据）。 ⚠️ 与 `dailiantong_actions._record_action` 形状相同，但**名字必须在本模块里再定义一次**：
    `tests/test_operation_log_coverage.py` 的 `_node_writes_log` 按「**裸名 + 当前模块**」
    解析登记助手（`_record_action` 已在 `_RECORD_FORWARDERS` 里），本模块不定义它时那条链会在半路断掉、六个新端点全被报「声称已埋点却找不到埋点」。"""
    actor_pk = getattr(operator, "pk", None)
    record(
        log_type=ACTION_LOG_TYPES[action],
        action=action,
        actor=(getattr(operator, "username", "") or "").strip() or "系统",
        actor_id=actor_pk,
        # `operator=None`（同步命令驱动的机器动作）必须显式声明机器来源，
        # 否则 `record()` 的 fail-closed 判据会拒（同 `dailiantong_actions._record_action`）。
        system=bool(system or actor_pk is None),
        target_kind=AUDIT_TARGET_KIND,
        target_id=str(push.pk),
        target_name=target_name or order.order_no,
        order_no=order.order_no,
        game_name=(order.game.name or "") if order.game_id else "",
        from_value=from_value,
        to_value=to_value,
        reason=reason,
    )


def _record_template_action(
    operator,
    *,
    template_id: int | None,
    title: str,
    reason: str,
) -> None:
    """写一条**代练通侧模板**的审计（没有订单行；⛔ 不装模板正文）。"""
    record(
        log_type=ACTION_LOG_TYPES[AUDIT_TEMPLATE],
        action=AUDIT_TEMPLATE,
        actor=(getattr(operator, "username", "") or "").strip() or "系统",
        actor_id=getattr(operator, "pk", None),
        target_kind=AUDIT_TEMPLATE_TARGET_KIND,
        target_id="" if template_id is None else str(template_id),
        target_name=title,
        reason=reason,
    )


def _positive_amount(value, *, label: str) -> Decimal:
    """金额闸：能解析、`> 0`、最多两位小数（⛔ 不用真值判断 0，D-0015 的 `is not None` 口径）。"""
    try:
        amount = _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError(f"{label}不是有效数字。") from exc
    if amount <= 0:
        raise DailiantongPushError(f"{label}必须大于 0。")
    if amount != amount.quantize(Decimal("0.01")):
        raise DailiantongPushError(f"{label}最多两位小数。")
    return amount


def _integer_yuan_amount(value, *, label: str) -> Decimal:
    """补款的金额闸：**整数元、≥ 1**。 ⭐ 真机（`LevelOrderModify_Special` + `Kind=0`）： - `1.5` → `{"Result":-5,"Err":"补款金额不能含有小数！."}`
    - `0` → `{"Result":-3,"Err":"补款金额不能低于1元."}` - `1` → `{"Result":1,"Err":"修改成功."}`
    ⇒ 这两条都必须在**本地**先挡（零上游写调用）；金额仍是 `Decimal`（AGENTS.md §5-E），返回的是整数值的 `Decimal`，发上游时由调用方转成 `"1"` 这种整数字面量。"""
    try:
        amount = _to_decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError(f"{label}不是有效数字。") from exc
    if amount != amount.to_integral_value():
        raise DailiantongPushError(f"{label}必须是整数元（代练通不接受小数）。")
    if amount < 1:
        raise DailiantongPushError(f"{label}不能低于 1 元。")
    return amount



def _is_pub_flag(value) -> bool | None:
    """把详情里的 `IsPub`（`1` / `0` / `"是"` / `"否"` / `True`）归一成真布尔。
    ⚠️ 解析不出来 / 没回吐 ⇒ `None`（= **不知道**，⛔ 不当成 False）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    text = str(value).strip().lower()
    if text in ("1", "true", "是", "已上架", "上架"):
        return True
    if text in ("0", "false", "否", "已下架", "下架"):
        return False
    return None


def _is_default_flag(value) -> bool:
    """`IsDefault`（`1` / `0` / `"1"` / `True`）→ 真布尔；解析不出 ⇒ `False`。"""
    return _is_pub_flag(value) is True


def _normalise_stat_payload(payload: dict, *, is_pub: int, start: date, end: date) -> dict:
    """`LevelOrderStat` 回包 → `{items, total, record_count, start, end, is_pub}`。
    上游回包形状（`docs/DAILIANTONG-API.md` §4 #27）：`LevelOrderStatList`（行数组）+
    `LevelOrderStatTotal`（汇总）。空结果是**空字符串**（同列表信封的既有观察）⇒ 归一成 `[]`。"""
    rows = payload.get("LevelOrderStatList")
    if rows is None:
        # 兜底：若哪天它改成 `<Action>` 列表信封，也吃得下。
        rows = payload.get("LevelOrderStat")
    if isinstance(rows, str) or not isinstance(rows, list):
        # 空结果是**空字符串**（同列表信封的既有观察）⇒ 归一成空数组。
        rows = []
    items = [row for row in rows if isinstance(row, dict)]
    total = payload.get("LevelOrderStatTotal")
    if not isinstance(total, dict):
        total = {}
    return {
        "items": items,
        "total": total,
        "record_count": _as_int(payload.get("RecordCount")),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "is_pub": is_pub,
    }


def _stats_cache_key(
    *,
    schema: str,
    is_pub: int,
    start: date,
    end: date,
    page_index: int,
    page_size: int,
) -> str:
    """统计缓存键：**租户 schema 必须在键里**（统计是账号级数据，⛔ 不能跨租户命中）。"""
    return (
        f"dlt-order-stat:{schema or 'public'}:{is_pub}:"
        f"{start.isoformat()}:{end.isoformat()}:{page_index}:{page_size}"
    )


def _call_require_temp_list(client):
    """取模板列表（`OrderRequireTempList`）；把上游失败翻成中文业务错误。"""
    try:
        return client.require_temp_list()
    except DailiantongTransportError as exc:
        raise DailiantongPushError(
            f"读取代练通订单要求模板失败（{exc.message}）。请稍后重试。"
        ) from exc
    except DailiantongError as exc:
        raise DailiantongPushError(
            f"读取代练通订单要求模板失败（上游提示：{exc.message}）。"
        ) from exc


def _template_rows(page) -> list[dict]:
    """把上游模板行归一成 `{id, title, temp, is_default}`（只取四个可用字段）。"""
    rows: list[dict] = []
    for raw in page.rows:
        rows.append(
            {
                "id": _as_int(raw.get("ID")),
                "title": str(raw.get("Title") or ""),
                "temp": str(raw.get("Temp") or ""),
                "is_default": _is_default_flag(raw.get("IsDefault")),
            }
        )
    return rows


def _find_template(rows: list[dict], temp_id: int) -> dict | None:
    for row in rows:
        if row.get("id") == temp_id:
            return row
    return None


# ── 加价（只有未接手）────────────────────────────────────────────────────


def up_price_push(
    order,
    *,
    operator,
    up_price,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """未接手单**加价**（`LevelOrderUpPrice`，要支付密码）。 ⛔ **只有未接手（`Status=11`）能加**（文档 + 实测 §4 #34）；已接手 / 已结算先本地挡掉，
    零写调用。回读判据 = 详情 `Price` **等于原价 + 加价额**（核验不到 / 不等 ⇒ 中文错误、
    ⛔ 不改本地行）。核验通过后同步本地推送行的 `price`。"""
    _require_operator(operator)
    amount = _positive_amount(up_price, label="加价金额")

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    old_price_text = _fmt_money(push.price)
    new_price_text = ""
    try:
        detail = _read_detail(client, push, action_name="加价")
        status = _as_int(detail.get("Status"))
        if status != REMOTE_STATUS_UNACCEPTED:
            raise DailiantongPushError(
                "只有未接手的代练通订单才能加价，"
                f"这张单当前状态是「{_status_label(status)}」。"
            )

        base_price = detail.get("Price")
        if base_price is None:
            # 详情没回吐 `Price` 时退回本地行（它是我们发单时的那一价）。
            base_price = push.price
        try:
            base = _to_decimal(base_price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DailiantongPushError(
                "代练通详情里没有可识别的当前价格，不能加价。"
                "请稍后重试、或到代练通后台核对。"
            ) from exc
        expected = base + amount
        old_price_text = _fmt_money(base)
        new_price_text = _fmt_money(expected)
        if expected < MIN_PRICE:
            raise DailiantongPushError(
                f"加价后的代练通价格不能低于 {_fmt_money(MIN_PRICE)} 元，"
                f"当前会变成 {_fmt_money(expected)} 元。"
            )

        try:
            client.up_price(push.serial_no, amount)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"加价结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的价格，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次加价（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="加价")
        actual = detail_after.get("Price")
        if not _money_equals(actual, expected):
            raise DailiantongPushError(
                "已提交加价，但回读代练通的价格是"
                f"「{actual if actual is not None else '空'}」，"
                f"预期「{_fmt_money(expected)}」，没能核验到。请人工核对。"
            )

        push.price = expected
        _save_remote_snapshot(push, detail_after, extra_fields=("price",))
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_UP_PRICE,
            reason=str(exc),
            from_value=old_price_text,
            to_value=new_price_text,
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_UP_PRICE,
        reason="回读 Price 已等于原价加加价额",
        from_value=old_price_text,
        to_value=new_price_text,
    )
    return push


def _money_equals(actual, expected: Decimal) -> bool:
    """金额相等判据（`is not None`；⛔ 不用真值，D-0015）。"""
    if actual is None:
        return False
    try:
        return _to_decimal(actual) == expected
    except (InvalidOperation, TypeError, ValueError):
        return False


# ── 申请客服介入 ────────────────────────────────────────────────────────


def request_push_arbitration(
    order,
    *,
    operator,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """申请客服介入（`LevelOrderRequestArbitration`）。 ## 写前闸（⭐ 真机定案，`docs/DAILIANTONG-API.md` §6.9）代练通只允许在**「协商撤销中（`CancelStatus=11`）」且转入该状态满 1 小时**时申请；其余状态上游一律回 `订单异常，无法申请仲裁介入`（12 / 13 / 16+cancel12 / 17 实测全拒）。
    ⇒ 本地按**当次详情回读**的 `CancelStatus` 先判，具体三档见 `dailiantong_push.arbitration_availability`： - 不是 11 ⇒ 中文错误 + **上游原话**（零写调用）； - 是 11、本地记了 `cancel_requested_at` 但**不足 1 小时** ⇒ 中文错误 + **还需几分钟**；
    - 是 11、本地**没有** `cancel_requested_at`（打手侧发起 / 历史行没记到）⇒ **放行给上游判**，上游拒了就把它的原文带回（⛔ 不本地永久禁用）。另有两条既有闸：已在客服介入流程里（`CancelStatus=14/15`）⇒ 直接挡掉；回读判据 = `CancelStatus ∈ {14,15}` **或** `Status=14`，核验不到 ⛔ 不报成功。"""
    _require_operator(operator)
    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        detail = _read_detail(client, push, action_name="申请客服介入")
        _guard_terminal(detail, action_name="申请客服介入")
        cancel_status = _as_int(detail.get("CancelStatus"))
        if cancel_status in CANCEL_STATUS_ARBITRATION_EVIDENCE:
            raise DailiantongPushError(
                "这张单已经在客服介入流程里（撤销状态「"
                f"{'客服介入中' if cancel_status == CANCEL_STATUS_ARBITRATION else '客服已处理'}」），"
                "不用重复申请。"
            )
        if cancel_status is None:
            # 详情里连撤销状态都没有 ⇒ 判不了「是不是协商撤销中」，⛔ 不猜。
            raise DailiantongPushError(
                "代练通详情里没有撤销状态，暂时判断不了能不能申请客服介入，"
                "请稍后重试、或到代练通后台核对。"
            )
        available, blocked_reason = push_module.arbitration_availability(
            push, cancel_status=cancel_status
        )
        if not available:
            raise DailiantongPushError(blocked_reason)

        try:
            client.request_arbitration(push.serial_no)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"申请客服介入的结果未知（{exc.message}）。"
                "请到代练通后台核对该单的撤销状态，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次客服介入申请（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="申请客服介入")
        cancel_after = _as_int(detail_after.get("CancelStatus"))
        status_after = _as_int(detail_after.get("Status"))
        if (
            cancel_after not in CANCEL_STATUS_ARBITRATION_EVIDENCE
            and status_after != REMOTE_STATUS_ABNORMAL
        ):
            raise DailiantongPushError(
                "已提交客服介入申请，但回读代练通详情没有看到客服介入状态"
                f"（撤销状态「{_status_label(cancel_after)}」、订单状态「{_status_label(status_after)}」），"
                "没能核验到。请到代练通后台核对。"
            )

        _save_remote_snapshot(push, detail_after)
    except DailiantongPushError as exc:
        _record_action(
            push, order, operator, action=AUDIT_ARBITRATION, reason=str(exc)
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_ARBITRATION,
        reason="回读已看到客服介入状态",
    )
    return push


# ── ⭐ h13：同意撤销（打手申请撤销后我方同意）────────────────────

#: 「同意撤销」交给上游的事由（`LevelOrderCancel` 的 `Comment`；打手 / 客服看得到）。
AGREE_CANCEL_COMMENT = "同意撤销"
#: 「同意撤销」写后回读的**成功档**：`12` 协商已处理 / `15` 客服已处理 / `16` 客服强制撤销。
#: ⚠️ 单一出处是 `dailiantong_push.CANCEL_STATUS_REVOKED`（⛔ 不在这里再写一份字面量）。
AGREE_CANCEL_DONE_STATUSES = push_module.CANCEL_STATUS_REVOKED


def _detail_amount_or_zero(detail: dict, *names: str, label: str) -> tuple[Decimal, bool]:
    """取详情里的撤销金额；**空 ⇒ 0**（返回 `(Decimal("0"), True)`，调用方写清）。
    - 取不到 / 空串 / `None` ⇒ `Decimal("0")` + `empty=True`；
    - 有值但解析不了 ⇒ `DailiantongPushError`（⛔ 不拿 0 冒充一个看不懂的值）。"""
    found, raw = push_module._detail_field(detail, *names)
    if not found or raw is None:
        return Decimal("0"), True
    if isinstance(raw, str) and not raw.strip():
        return Decimal("0"), True
    try:
        return _to_decimal(raw), False
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DailiantongPushError(
            f"代练通详情里的「{label}」不是有效金额（{raw!r}），没有执行同意撤销。"
            "请到代练通后台核对。"
        ) from exc


def agree_cancel(
    order, *, operator, platform: int = DEFAULT_PLATFORM
) -> DailiantongPush:
    """同意打手发起的协商撤销（`POST …/platform-push/agree-cancel/` 的服务层）。上游只有「申请(0) / 取消(1) / 同意(2)」三档，**没有拒绝**（`docs/DAILIANTONG-API.md` §6.13）：拒绝只能等满 1 小时后申请客服介入（仲裁），或与打手在留言里沟通。 ## 前置（本地先挡，零写调用）推送行 `remote_cancel_status == 11`（协商撤销中）。⚠️ 这只是本地快照；写前还会
    **回读详情**复核一次 `CancelStatus`——不是 11 就中文报错（上游也会拒，但不白打一次写）。 ## 两个金额（`PayLevelBal` / `RepEnsureBal`） `Flag=2` 要求带这两格，值**取写前详情现值**；详情里为空 ⇒ **按 0 传**，并把「详情为空、按 0 传」写进审计 `reason`（⛔ 不静默替业务方决定）。 ## 成败判据 - 写成功不算成功：**写后回读** `CancelStatus ∈ {12,15,16}`（协商已处理 / 客服已处理 /
    客服强制撤销）才算核验通过；核验不到 ⇒ 中文错误 + 回读到的状态，⛔ 不报成功； - 上游明确拒绝 / 超时 ⇒ 中文错误，文案带**上游原话**； - 核验通过后同步推送行，并**立刻**把本地订单状态按远端回写（⛔ 不等 1 小时兜底轮询； `Status=16 + CancelStatus=12` ⇒ 本地「已退单」，见 `local_status_for_remote`）。成功返回更新后的推送行；任何失败抛 `DailiantongPushError`（中文、可展示，⛔ 不 500）。"""
    _require_operator(operator)
    push = _latest_active_push(order, platform)
    local_cancel_status = _as_int(push.remote_cancel_status)
    if local_cancel_status != CANCEL_STATUS_NEGOTIATING:
        raise DailiantongPushError(
            "这条代练通推送现在不在「协商撤销中」（本地记录的撤销状态："
            f"「{_status_label(local_cancel_status)}」，码 "
            f"{local_cancel_status if local_cancel_status is not None else '空'}），"
            "没有可同意的撤销协商。请先同步状态再操作。"
        )

    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        try:
            # 写前回读：既核验「现在仍是协商撤销中」，也取两个金额的现值。
            detail = _read_detail(client, push, action_name="同意撤销")
            cancel_status = _as_int(detail.get("CancelStatus"))
            if cancel_status != CANCEL_STATUS_NEGOTIATING:
                raise DailiantongPushError(
                    "代练通那边这张单现在已经不是「协商撤销中」（撤销状态："
                    f"「{_status_label(cancel_status)}」，码 "
                    f"{cancel_status if cancel_status is not None else '空'}），"
                    "不能同意撤销。请先同步状态再操作。"
                )

            pay_level_bal, pay_empty = _detail_amount_or_zero(
                detail, "PayLevelBal", label="支付代练费"
            )
            rep_ensure_bal, rep_empty = _detail_amount_or_zero(
                detail, "RepEnsureBal", label="赔偿保证金"
            )
            amount_note = ""
            if pay_empty or rep_empty:
                empty_fields = [
                    label
                    for label, empty in (
                        ("支付代练费", pay_empty),
                        ("赔偿保证金", rep_empty),
                    )
                    if empty
                ]
                amount_note = (
                    f"；详情里「{' / '.join(empty_fields)}」为空，已按 0 传给上游"
                )

            try:
                client.cancel_order(
                    push.serial_no,
                    CANCEL_FLAG_AGREE,
                    pay_level_bal=pay_level_bal,
                    rep_ensure_bal=rep_ensure_bal,
                    comment=AGREE_CANCEL_COMMENT,
                )
            except DailiantongTransportError as exc:
                raise DailiantongPushError(
                    f"提交「同意撤销」的结果未知（{exc.message}）。"
                    "请到代练通后台核对这张单的撤销状态，再决定是否重试。"
                ) from exc
            except DailiantongError as exc:
                raise DailiantongPushError(
                    f"代练通没有接受这次「同意撤销」（上游提示：{exc.message}）。"
                ) from exc

            detail_after = _readback_detail(client, push, action_name="同意撤销")
            cancel_after = _as_int(detail_after.get("CancelStatus"))
            if cancel_after not in AGREE_CANCEL_DONE_STATUSES:
                raise DailiantongPushError(
                    "已提交「同意撤销」，但回读代练通的撤销状态是「"
                    f"{_status_label(cancel_after)}」（码 "
                    f"{cancel_after if cancel_after is not None else '空'}），"
                    "没有变成「协商已处理 / 客服已处理 / 客服强制撤销」，没能核验到。"
                    "请到代练通后台核对。"
                )

            _save_remote_snapshot(push, detail_after)
            # ⛔ 不等下一轮同步：本地订单状态立刻按刚回读的远端事实回写（失败只留痕、
            # ⛔ 不把「上游已经同意撤销」这一事实报成失败——见 helper 的 docstring）。
            push_module.apply_remote_status_after_readback(push)
        except DailiantongPushError as exc:
            # 失败也留痕（含「已提交但没核验到」「结果未知」这两种最需要人来看的）。
            _record_action(
                push,
                order,
                operator,
                action=AUDIT_AGREE_CANCEL,
                reason=f"同意撤销失败：{exc}",
            )
            raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_AGREE_CANCEL,
        reason=(
            "回读撤销状态 "
            f"{_status_label(_as_int(detail_after.get('CancelStatus')))}"
            f"（码 {_as_int(detail_after.get('CancelStatus'))}），"
            f"金额：支付代练费 {_fmt_money(pay_level_bal)} / "
            f"赔偿保证金 {_fmt_money(rep_ensure_bal)}{amount_note}"
        ),
    )
    return push


def cancel_withdraw_request(
    order, *, operator, platform: int = DEFAULT_PLATFORM
) -> DailiantongPush:
    """取消**我方发起**的协商撤销（`LevelOrderCancel(Flag=1)`）；返回推送行。 ⭐ 真机：前端点的「取消撤销」此前**没有后端路由**（404 降级成「已发出」，实际零上游调用）⇒ 本函数 + `cancel-request/` 端点就是补这一条。 ## 前置（本地先挡，零上游调用） 1. 有效推送的 `remote_cancel_status == 11`（协商撤销中）；
    2. 发起方**必须是我们**：本地 `cancel_requested_at` 非空，或 `cancel_initiator == "us"`。 - 打手发起 ⇒ 中文「这次撤销是打手发起的，请用「同意撤销」或等 1 小时后申请客服介入」； - 判不了（`cancel_initiator == ""` 且无本地记录）⇒ 中文「判断不了发起方，请到后台核对」。 ## 上游 `LevelOrderCancel(Flag=1)`：**不需要 PayPass、不需要两个金额**（真机
    `{"Result":1,"Err":"取消撤销成功."}`）。 ## 成败 **写后回读**：`CancelStatus` 回到 `10 未撤销` 或 `13 撤销已取消` 才算成功；回读到 12/14 等其它状态 ⇒ 中文「已提交但没核验到」+ 回读值；⛔ 不报成功。成功后清掉本地 `cancel_requested_at` 与 `auto_arbitrate` 待办（否则 24 小时自动仲裁还挂着）； `auto_arbitrated_at` 作为历史事实保留。任何失败都写审计（复用 `order.dailiantong_withdraw`，语义是撤回/撤销线的一环）。"""
    _require_operator(operator)
    push = _latest_active_push(order, platform)
    cancel_status = _as_int(push.remote_cancel_status)
    if cancel_status != CANCEL_STATUS_NEGOTIATING:
        raise DailiantongPushError(
            "这条代练通推送现在不在「协商撤销中」（本地记录的撤销状态："
            f"「{_status_label(cancel_status)}」，码 "
            f"{cancel_status if cancel_status is not None else '空'}），"
            "没有可取消的撤销申请。"
        )
    requested_by_us = (
        push.cancel_requested_at is not None
        or push.cancel_initiator == push_module.CANCEL_INITIATOR_US
    )
    if not requested_by_us:
        if push.cancel_initiator == push_module.CANCEL_INITIATOR_TAKER:
            raise DailiantongPushError(
                "这次撤销是打手发起的，请用「同意撤销」处理，"
                "或等转入协商撤销满 1 小时后申请客服介入。"
            )
        raise DailiantongPushError(
            "判断不了这次撤销是谁发起的（本地没有发起记录，远端也没回传发起方），"
            "请先同步状态或到代练通后台核对；如果是打手发起，请用「同意撤销」处理。"
        )

    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        try:
            client.cancel_order(push.serial_no, CANCEL_FLAG_CANCEL)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"取消撤销的结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的撤销状态，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次「取消撤销」（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="取消撤销")
        cancel_after = _as_int(detail_after.get("CancelStatus"))
        # 先落库回读到的远端状态（12/13/14 也要如实保存），再判成败。
        _save_remote_snapshot(push, detail_after)
        if cancel_after not in (CANCEL_STATUS_NOT_CANCELLED, 13):
            raise DailiantongPushError(
                "已提交「取消撤销」，但回读代练通的撤销状态是「"
                f"{_status_label(cancel_after)}」（码 "
                f"{cancel_after if cancel_after is not None else '空'}），"
                "没有回到「未撤销 / 撤销已取消」，没能核验到。请到代练通后台核对。"
            )

        # 成功：清待办态（cancel_requested_at 是自动仲裁的计时起点）。
        push.cancel_requested_at = None
        push.auto_arbitrate = False
        push.auto_arbitrate_attempts = 0
        push.auto_arbitrate_next_at = None
        push.auto_arbitrate_note = ""
        push.save(
            update_fields=[
                "cancel_requested_at",
                "auto_arbitrate",
                "auto_arbitrate_attempts",
                "auto_arbitrate_next_at",
                "auto_arbitrate_note",
                "updated_at",
            ]
        )
        push_module.apply_remote_status_after_readback(push)
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_WITHDRAW,
            reason=f"取消撤销失败：{exc}",
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_WITHDRAW,
        reason=(
            "取消撤销申请成功（回读 CancelStatus="
            f"{_as_int(detail_after.get('CancelStatus'))}），已清掉自动仲裁待办"
        ),
    )
    return push


# ── ⭐：自动申请客服介入（协商撤销满 24 小时）────────────────────


def _open_client_for_automation(integration):
    """自动仲裁段的客户端构造点：**按属性查找** push 模块的那一个构造点。 ⚠️ 为什么不直接用本模块顶部绑定的 `_open_client`：`dailiantong_sync` 的测试只
    patch `apps.business.dailiantong_push._open_client`（与轮询 / 撤回段同一个替换点，
    `AGENTS.md` §5-E）。用本模块绑定的那份会绕过桩、去连真实上游。"""
    return push_module._open_client(integration)


def _clip_auto_note(text: str) -> str:
    """按 `auto_arbitrate_note` 列宽截断（⛔ 不靠数据库 `DataError` ⇒ 500）。"""
    return (text or "")[:AUTO_ARBITRATE_NOTE_MAX_LENGTH]


def auto_arbitrate_denial(push, *, now=None) -> str | None:
    """自动仲裁的**全部准入判据**；返回中文原因 / `None`（可以尝试）。 ⚠️ 这里的判据与 `auto_arbitrate_candidates` 的查询条件**同源**：查询负责把绝大多数
    不该跑的行筛掉（性能），本函数负责逐行复核（单测直接调它、以及扫描后的二次确认）。唯一一个查询里**没有**的条件是「满 1 小时」——24 小时门槛已经更严，它在这里是
    给**直接调用**（将来若把门槛改成更短）的硬闸：上游实测 1 小时内会拒。"""
    now = now or timezone.now()
    if not push.auto_arbitrate:
        return "这张单没有勾选「协商撤销后自动申请仲裁」。"
    if push.auto_arbitrated_at is not None:
        return "已经自动申请过客服介入，不重复申请。"
    if push.remote_cancel_status != CANCEL_STATUS_NEGOTIATING:
        return (
            "远端撤销状态不是「协商撤销中」（"
            f"{_status_label(push.remote_cancel_status)}），不自动申请仲裁。"
        )
    if push.cancel_requested_at is None:
        return "没有记录协商撤销的申请时间（不是本系统发起的撤销），不自动申请仲裁。"
    age = now - push.cancel_requested_at
    if age < AUTO_ARBITRATE_MIN_AGE:
        return (
            "协商撤销还不满 1 小时（上游会拒绝），先不自动申请仲裁。"
        )
    if age < AUTO_ARBITRATE_AFTER:
        return (
            "协商撤销还不满 24 小时，先等打手 / 客服处理，暂不自动申请仲裁。"
        )
    if (push.auto_arbitrate_attempts or 0) >= AUTO_ARBITRATE_MAX_ATTEMPTS:
        return (
            f"自动申请仲裁已经失败 {AUTO_ARBITRATE_MAX_ATTEMPTS} 次，已停止自动重试；"
            "请到代练通后台人工处理。"
        )
    if push.auto_arbitrate_next_at is not None and push.auto_arbitrate_next_at > now:
        return "在自动申请仲裁的失败退避窗口内，下一轮再试。"
    return None


def auto_arbitrate_candidates(*, now=None):
    """本轮**该处理**的推送行（查询条件；逐行还要过 `auto_arbitrate_denial`）。"""
    now = now or timezone.now()
    return (
        DailiantongPush.objects.filter(
            # 只有代练通有这个动作（多平台骨架里其它平台未接入）。
            platform=DEFAULT_PLATFORM,
            state=DailiantongPush.State.SENT,
            is_active=True,
            auto_arbitrate=True,
            remote_cancel_status=CANCEL_STATUS_NEGOTIATING,
            auto_arbitrated_at__isnull=True,
            cancel_requested_at__isnull=False,
            cancel_requested_at__lte=now - AUTO_ARBITRATE_AFTER,
        )
        .filter(
            Q(auto_arbitrate_next_at__isnull=True)
            | Q(auto_arbitrate_next_at__lte=now)
        )
        .filter(auto_arbitrate_attempts__lt=AUTO_ARBITRATE_MAX_ATTEMPTS)
        .exclude(serial_no__isnull=True)
        .exclude(serial_no="")
        .order_by("id")
    )


def _mark_auto_arbitration_done(push, *, now, note: str) -> AutoArbitrateOutcome:
    """记「已收口」（申请成功 / 远端已经在流程里 / 状态已不再适用）：只做一次。"""
    message = _clip_auto_note(note)
    push.auto_arbitrated_at = now
    push.auto_arbitrate_next_at = None
    push.auto_arbitrate_note = message
    push.save(
        update_fields=[
            "auto_arbitrated_at",
            "auto_arbitrate_next_at",
            "auto_arbitrate_note",
            "updated_at",
        ]
    )
    # ⚠️ 机器来源：同步命令驱动，⛔ 不冒充任何客服账号（`actor_id=None` + `system=True`）。
    _record_action(
        push,
        push.order,
        None,
        action=AUDIT_ARBITRATION,
        reason=f"自动仲裁：{message}",
        system=True,
    )
    return AutoArbitrateOutcome(AUTO_ARBITRATE_DONE, message)


def _mark_auto_arbitration_failure(push, *, now, reason: str) -> AutoArbitrateOutcome:
    """记一次失败 + 退避；到上限就停手并留下可读原因（⛔ 不刷屏）。"""
    attempts = (push.auto_arbitrate_attempts or 0) + 1
    stopped = attempts >= AUTO_ARBITRATE_MAX_ATTEMPTS
    text = f"自动申请客服介入失败（第 {attempts} 次）：{reason}"
    if stopped:
        text += (
            f"；已停止自动重试（上限 {AUTO_ARBITRATE_MAX_ATTEMPTS} 次），"
            "请到代练通后台人工处理"
        )
    push.auto_arbitrate_attempts = attempts
    push.auto_arbitrate_next_at = None if stopped else now + AUTO_ARBITRATE_RETRY
    push.auto_arbitrate_note = _clip_auto_note(text)
    # `last_error` 会被下一轮回读成功清空 ⇒ 真正持久的可读原因在 `auto_arbitrate_note`；
    # 这里同时写一份是为了让它在页面的「最近错误」里立刻可见（排障口径同其它动作）。
    push.last_error = push_module._clip_error(text)
    push.save(
        update_fields=[
            "auto_arbitrate_attempts",
            "auto_arbitrate_next_at",
            "auto_arbitrate_note",
            "last_error",
            "updated_at",
        ]
    )
    _record_action(
        push,
        push.order,
        None,
        action=AUDIT_ARBITRATION,
        reason=f"自动仲裁：{text}",
        system=True,
    )
    return AutoArbitrateOutcome(AUTO_ARBITRATE_FAILED, text)


def auto_arbitrate_one(push, *, now=None) -> AutoArbitrateOutcome:
    """对一条推送尝试一次「自动申请客服介入」；返回结论（不抛预期失败）。流程（每一步都对应业务方 的口径）： 1. 过 `auto_arbitrate_denial`（开关 / 时间门槛 / 只做一次 / 退避 / 上限）； 2. **写前回读**详情：撤销状态已是「客服介入中 / 已处理」⇒ 无需重复申请（记已收口）；
    已不是「协商中」或状态不支持介入 ⇒ 记已收口（再试也不会变，⛔ 不白打 5 次）； 3. `LevelOrderRequestArbitration`（**只调一次**，⛔ 写动作不重试）； 4. **写后回读**：`CancelStatus ∈ {14,15}` 或 `Status=14` 才算成功；
    核验不到 ⇒ 按失败退避（⛔ 不假报成功）。成功 / 失败都写审计（机器来源）与 `auto_arbitrate_note`；失败每 6 小时再试，最多 `AUTO_ARBITRATE_MAX_ATTEMPTS` 次。"""
    now = now or timezone.now()
    denial = auto_arbitrate_denial(push, now=now)
    if denial:
        return AutoArbitrateOutcome(AUTO_ARBITRATE_SKIPPED, denial)

    integration = _active_integration(push.platform)
    client = _open_client_for_automation(integration)
    try:
        try:
            detail = _read_detail(client, push, action_name="自动申请客服介入")
        except DailiantongPushError as exc:
            return _mark_auto_arbitration_failure(push, now=now, reason=str(exc))

        cancel_status = _as_int(detail.get("CancelStatus"))
        if cancel_status in CANCEL_STATUS_ARBITRATION_EVIDENCE:
            return _mark_auto_arbitration_done(
                push,
                now=now,
                note=(
                    "代练通已经在客服介入流程里（撤销状态 "
                    f"{cancel_status}），无需重复申请"
                ),
            )
        if cancel_status != CANCEL_STATUS_NEGOTIATING:
            return _mark_auto_arbitration_done(
                push,
                now=now,
                note=(
                    "撤销状态已不是「协商撤销中」（当前 "
                    f"{_status_label(cancel_status)}），不再自动申请仲裁"
                ),
            )
        # ⚠️ 合并口径（真机定案，main 侧）：能否申请客服介入**只看 `CancelStatus=11` 且转入满 1 小时**（唯一实现 `dailiantong_push.arbitration_availability`）。
        # fx-A 原来在这里按 `Status` 查 `ARBITRATION_STATUSES`，那份集合已被真机证伪（12 / 13 / 16+cancel12 / 17 全部被上游拒）且 main 已删除 ⇒ ⛔ 不在这里另判一份。
        # 上面的 `cancel_status` 闸 + `auto_arbitrate_denial` 的时间闸已经覆盖全部准入条件。

        try:
            client.request_arbitration(push.serial_no)
        except DailiantongTransportError as exc:
            return _mark_auto_arbitration_failure(
                push,
                now=now,
                reason=(
                    f"申请客服介入的结果未知（{exc.message}），"
                    "请到代练通后台核对该单的撤销状态"
                ),
            )
        except DailiantongError as exc:
            return _mark_auto_arbitration_failure(
                push,
                now=now,
                reason=f"代练通没有接受这次客服介入申请（上游提示：{exc.message}）",
            )

        try:
            detail_after = _readback_detail(
                client, push, action_name="自动申请客服介入"
            )
        except DailiantongPushError as exc:
            return _mark_auto_arbitration_failure(push, now=now, reason=str(exc))

        cancel_after = _as_int(detail_after.get("CancelStatus"))
        status_after = _as_int(detail_after.get("Status"))
        if (
            cancel_after in CANCEL_STATUS_ARBITRATION_EVIDENCE
            or status_after == REMOTE_STATUS_ABNORMAL
        ):
            _save_remote_snapshot(push, detail_after)
            return _mark_auto_arbitration_done(
                push,
                now=now,
                note=(
                    "已自动申请客服介入（回读撤销状态 "
                    f"{_status_label(cancel_after)} / 订单状态 "
                    f"{_status_label(status_after)}）"
                ),
            )
        return _mark_auto_arbitration_failure(
            push,
            now=now,
            reason=(
                "已提交客服介入申请，但回读代练通详情没有看到客服介入状态"
                f"（撤销状态「{_status_label(cancel_after)}」、"
                f"订单状态「{_status_label(status_after)}」）"
            ),
        )
    finally:
        _close(client)


def auto_arbitrate_pending_pushes(*, now=None) -> tuple[int, int]:
    """`dailiantong_sync` 每轮调用：处理所有到期的协商撤销行；返回 `(已收口, 失败)`。
    ⚠️ 逐行独立异常边界：一行炸了不影响其它行（与同步命令其它段同一条纪律）。"""
    now = now or timezone.now()
    done = 0
    failed = 0
    for push in auto_arbitrate_candidates(now=now).iterator():
        try:
            outcome = auto_arbitrate_one(push, now=now)
        except Exception as exc:  # noqa: BLE001 —— 单行未预期失败不中断整轮
            failed += 1
            logger.warning(
                "dailiantong_sync: 自动申请仲裁失败 push=%s（%s）",
                push.pk,
                type(exc).__name__,
            )
            continue
        if outcome.status == AUTO_ARBITRATE_DONE:
            done += 1
        elif outcome.status == AUTO_ARBITRATE_FAILED:
            failed += 1
    return done, failed


# ── 评价打手 ────────────────────────────────────────────────────────────


def value_push_order(
    order,
    *,
    operator,
    eval_type,
    comment: str | None = None,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """评价打手（`LevelOrderValue`）。 - ⛔ 只有**已结算（`Status=17`）**的单能评价（实测 `-2 订单未达最终状态`）；
    - `OtherUserID` 取推送行里存的 `accept_uid`（详情 `AcceptUID` 的落库值）；
    - ⚠️ 评价**没有读面**（详情 / 列表都不回吐）⇒ 本动作**只认上游成功码**，这是如实登记的例外（⛔ 不假装回读）。"""
    _require_operator(operator)
    try:
        eval_type = int(eval_type)
    except (TypeError, ValueError) as exc:
        raise DailiantongPushError("评价满意度必须是整数。") from exc
    if eval_type not in EVAL_TYPES:
        raise DailiantongPushError(
            "评价满意度只能是 10（非常满意）/ 11（满意）/ 12（一般）/ 13（很差）。"
        )
    text = (comment or "").strip()
    if len(text) > VALUE_COMMENT_MAX_LENGTH:
        raise DailiantongPushError(
            f"评价内容不能超过 {VALUE_COMMENT_MAX_LENGTH} 个字符。"
        )

    push = _latest_active_push(order, platform, allow_settled_closed=True)
    integration = _active_integration(platform)
    client = _open_client(integration)
    label = EVAL_TYPE_LABELS[eval_type]
    try:
        detail = _read_detail(client, push, action_name="评价打手")
        status = _as_int(detail.get("Status"))
        if status != REMOTE_STATUS_SETTLED:
            raise DailiantongPushError(
                "只有已结算（已完单）的代练通订单才能评价打手，"
                f"这张单当前状态是「{_status_label(status)}」。"
            )
        accept_uid = _accept_uid(push, detail)
        if not accept_uid:
            raise DailiantongPushError(
                "这张单在代练通还没有接单打手 UID（可能还没被接手 / 还没同步过），"
                "不能评价。请先同步一次状态、或到代练通后台核对。"
            )

        try:
            client.value_order(
                push.serial_no,
                other_user_id=accept_uid,
                eval_type=eval_type,
                comment=text or None,
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"评价打手的结果未知（{exc.message}）。"
                "请到代练通后台核对是否已经评价过，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次评价（上游提示：{exc.message}）。"
            ) from exc

        # 上游没有评价读面 ⇒ 只把回读到的状态 / 接单人刷新一次，不改本地其它字段。
        _save_remote_snapshot(push, detail)
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_VALUE,
            reason=str(exc),
            to_value=label,
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_VALUE,
        reason="上游回成功码（评价没有读面，不强制回读）",
        to_value=label,
    )
    return push


def _accept_uid(push: DailiantongPush, detail: dict) -> str:
    """接单打手 UID：优先推送行里存的 `accept_uid`，其次本次详情的 `AcceptUID`。"""
    stored = (push.accept_uid or "").strip()
    if stored:
        return stored
    return str(detail.get("AcceptUID") or "").strip()


# ── 上架 / 下架 ─────────────────────────────────────────────────────────


def off_sell_push_order(
    order,
    *,
    operator,
    flag,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """上架 / 下架（`LevelOrderOffSell`，`Flag` 1 上架 / 0 下架）。 - 写前闸（⭐ 真机放宽，见 `OFF_SELL_ALLOWED`）：**下架**只允许未接手（`Status=11`）；
    **上架**允许未接手（11）**或已下架（18）**——下架后上游回 18，旧闸只认 11 会让这张单 **永远上不了架**（业务方实测被卡）。其余状态一律中文错误（口径取上游原话「未接手或已下架」）；
    方向与 `IsPub` 已一致 ⇒ 中文错误（⛔ 不重复发同一个方向）； - 回读判据：详情 `IsPub` 变成期望值（`True` / `False`）。详情没回吐 ⇒ 「没能核验到」。"""
    _require_operator(operator)
    try:
        flag = int(flag)
    except (TypeError, ValueError) as exc:
        raise DailiantongPushError("上下架方向必须是 1（上架）或 0（下架）。") from exc
    if flag not in (OFF_SELL_FLAG_ON, OFF_SELL_FLAG_OFF):
        raise DailiantongPushError("上下架方向必须是 1（上架）或 0（下架）。")
    action_name = "上架" if flag == OFF_SELL_FLAG_ON else "下架"
    expected = flag == OFF_SELL_FLAG_ON
    allowed_statuses, allowed_text = OFF_SELL_ALLOWED[flag]

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    before_text = ""
    after_text = "上架" if expected else "下架"
    try:
        detail = _read_detail(client, push, action_name=action_name)
        status = _as_int(detail.get("Status"))
        if status not in allowed_statuses:
            raise DailiantongPushError(
                f"只有{allowed_text}的代练通订单才能{action_name}，"
                f"这张单当前状态是「{_status_label(status)}」。"
            )
        before = _is_pub_flag(detail.get("IsPub"))
        before_text = "已上架" if before is True else "已下架" if before is False else "未知"
        if before is expected:
            raise DailiantongPushError(
                f"这张代练通订单已经是「{'上架' if expected else '下架'}」状态，"
                f"不用重复{action_name}。"
            )

        try:
            client.off_sell(push.serial_no, flag=flag)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"{action_name}结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的上下架状态，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次{action_name}（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name=action_name)
        after = _is_pub_flag(detail_after.get("IsPub"))
        if after is None:
            raise DailiantongPushError(
                f"已提交{action_name}，但代练通详情没有回吐上下架状态，没能核验到。"
                "请到代练通后台核对。"
            )
        if after is not expected:
            raise DailiantongPushError(
                f"已提交{action_name}，但回读代练通的上下架状态仍是「"
                f"{'已上架' if after else '已下架'}」，没能核验到。请人工核对。"
            )

        _save_remote_snapshot(push, detail_after)
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_OFF_SELL,
            reason=str(exc),
            from_value=before_text,
            to_value=after_text,
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_OFF_SELL,
        reason="回读 IsPub 已等于目标状态",
        from_value=before_text,
        to_value=after_text,
    )
    return push


# ── 锁定 / 解锁 ─────────────────────────────────────────────────────────

#: 允许「锁定」的远端状态：**正在代练 / 等待验收 / 订单异常（12 / 13 / 14）**。 ⭐ 真机改准：旧口径只放 12；实测在 `Status=14` 上 `LevelOrderLock(Locked=1)`
#: 会把状态推到 15、`Locked=0` 解锁又回到 14 ⇒ 12 / 13 / 14 都是上游允许的锁定入口。 ⚠️ 上游对 `Status=16 / 12` 也接受锁定 / 解锁调用（状态不变）——本仓**不跟随**：
#: 页面只在 12 / 13 / 14 露「锁定」、只在 15 露「解锁」。
LOCKABLE_REMOTE_STATUSES = frozenset(
    {
        REMOTE_STATUS_IN_PROGRESS,
        REMOTE_STATUS_WAITING_ACCEPTANCE,
        REMOTE_STATUS_ABNORMAL,
    }
)


def lock_push_order(
    order,
    *,
    operator,
    locked,
    reason: str | None = None,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """锁定 / 解锁（`LevelOrderLock`，`Locked` 1 加锁 / 0 解锁）。 ⭐ 真机改准（`Status=15` 的真实含义）： - **锁**：`Status=12 / 13 / 14` 都能锁（在 14 上 `Locked=1` 实测把 `Status` 推到 15、
    `OrderLocked=true`）；文字档位「正在代练 / 等待验收 / 订单异常」； - **解**：只有 `Status=15` 能解；解锁后**回到锁定前的状态**（可能是 14、也可能是 12 / 13， ⛔ 不写死 12）；
    - 回读判据：锁 ⇒ 详情 `Status == 15`；解 ⇒ 详情 `Status != 15`（回到锁定前状态即可，本地同步按回读结果走，不需要特殊处理）。 - ⚠️ 上游对 `Status=16 / 12` 也接受锁定 / 解锁调用（状态不变）——本仓**不跟随**：只在 12 / 13 / 14 露「锁定」、只在 15 露「解锁」。"""
    _require_operator(operator)
    try:
        locked = int(locked)
    except (TypeError, ValueError) as exc:
        raise DailiantongPushError("锁定方向必须是 1（锁定）或 0（解锁）。") from exc
    if locked not in (LOCK_FLAG_LOCKED, LOCK_FLAG_UNLOCKED):
        raise DailiantongPushError("锁定方向必须是 1（锁定）或 0（解锁）。")
    action_name = "锁定" if locked == LOCK_FLAG_LOCKED else "解锁"
    text = (reason or "").strip()
    if len(text) > LOCK_REASON_MAX_LENGTH:
        raise DailiantongPushError(
            f"{action_name}原因不能超过 {LOCK_REASON_MAX_LENGTH} 个字符。"
        )

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    before_text = ""
    if locked == LOCK_FLAG_LOCKED:
        # 锁定的目标是确定的（上游实测：不管从 12 / 13 / 14 锁，回读都变 15）。
        after_text = _status_label(REMOTE_STATUS_LOCKED)
        expected_text = after_text
    else:
        # 解锁**没有固定目标码**：回到锁定前的状态（14 / 12 / 13 都可能）。
        after_text = "解锁（回到锁定前的状态）"
        expected_text = "不再是「已锁定」"
    try:
        detail = _read_detail(client, push, action_name=action_name)
        status = _as_int(detail.get("Status"))
        before_text = _status_label(status)
        if locked == LOCK_FLAG_LOCKED:
            if status not in LOCKABLE_REMOTE_STATUSES:
                raise DailiantongPushError(
                    "只有「正在代练 / 等待验收 / 订单异常」的代练通订单才能锁定，"
                    f"这张单当前状态是「{before_text}」。"
                )
        else:
            if status != REMOTE_STATUS_LOCKED:
                raise DailiantongPushError(
                    "只有「已锁定」的代练通订单才能解锁，"
                    f"这张单当前状态是「{before_text}」。"
                )

        try:
            client.lock_order(
                push.serial_no, locked=locked, reason=text or None
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"{action_name}结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的锁定状态，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次{action_name}（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name=action_name)
        status_after = _as_int(detail_after.get("Status"))
        if locked == LOCK_FLAG_LOCKED:
            ok = status_after == REMOTE_STATUS_LOCKED
        else:
            ok = status_after != REMOTE_STATUS_LOCKED
        if not ok:
            raise DailiantongPushError(
                f"已提交{action_name}，但回读代练通的状态是"
                f"「{_status_label(status_after)}」，"
                f"预期「{expected_text}」，没能核验到。请人工核对。"
            )
        if locked == LOCK_FLAG_UNLOCKED:
            # 审计 / 提示记**真实**落到哪一档（14 / 12 / 13），⛔ 不写死「正在代练」。
            after_text = _status_label(status_after)

        _save_remote_snapshot(push, detail_after)
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_LOCK,
            reason=str(exc),
            from_value=before_text,
            to_value=after_text,
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_LOCK,
        reason=(
            "回读 Status 已等于目标状态"
            if locked == LOCK_FLAG_LOCKED
            else "回读 Status 已不再是「已锁定」"
        ),
        from_value=before_text,
        to_value=after_text,
    )
    return push


# ── 拉黑 / 解拉黑打手 ───────────────────────────────────────────────────


def blacklist_taker(
    order,
    *,
    operator,
    flag=BLACKLIST_FLAG_ADD,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """拉黑 / 解拉黑**这张单的接单打手**（`AddBlackList`）。 `UID` 只从推送行的 `accept_uid`（详情 `AcceptUID` 的落库值）取；没有 ⇒ 中文业务错误
    （⛔ 不拿我们的单号 / 账号去试）。 ⚠️ 上游**没有黑名单读面** ⇒ 只认成功码（如实登记的例外）。"""
    _require_operator(operator)
    try:
        flag = int(flag)
    except (TypeError, ValueError) as exc:
        raise DailiantongPushError("拉黑方向必须是 0（拉黑）或 1（解除）。") from exc
    if flag not in (BLACKLIST_FLAG_ADD, BLACKLIST_FLAG_REMOVE):
        raise DailiantongPushError("拉黑方向必须是 0（拉黑）或 1（解除）。")
    action_name = "拉黑" if flag == BLACKLIST_FLAG_ADD else "解除拉黑"

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        detail = _read_detail(client, push, action_name=action_name)
        uid = _accept_uid(push, detail)
        if not uid:
            raise DailiantongPushError(
                "这张单还没有接单打手 UID（可能还没被接手 / 还没同步过），"
                f"不能{action_name}。请先同步一次状态、或到代练通后台核对。"
            )

        try:
            client.add_blacklist(uid, flag=flag)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"{action_name}结果未知（{exc.message}）。"
                "请到代练通后台核对该打手的黑名单状态，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次{action_name}（上游提示：{exc.message}）。"
            ) from exc

        # 上游没有黑名单读面 ⇒ 只把这次回读到的接单人 UID 存下来（下次不用再猜）。
        _save_remote_snapshot(push, detail)
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_BLACKLIST,
            reason=str(exc),
            target_name=(push.accept_uid or ""),
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_BLACKLIST,
        reason=f"上游回成功码（{action_name}没有读面，不强制回读）",
        target_name=uid,
    )
    return push


# ── 按日统计（只读，带缓存）─────────────────────────────────────────────


def platform_order_stats(
    *,
    start: date,
    end: date,
    is_pub: int = STATS_SIDE_PUBLISH,
    page_index: int = 1,
    page_size: int = 50,
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """代理上游 `LevelOrderStat`（按日统计），**按租户 schema + 日期 + 方向**缓存 60 秒。
    ⚠️ 统计是**账号级**数据（每个租户自己的代练通账号）⇒ 缓存键必须带 `connection.schema_name`，否则租户 A 的数字会出现在租户 B 的页面上。"""
    if start > end:
        raise DailiantongPushError("统计的开始日期不能晚于结束日期。")
    if is_pub not in STATS_SIDES:
        raise DailiantongPushError("统计方向必须是 1（发单）或 0（接单）。")

    schema = (getattr(connection, "schema_name", "") or "").strip() or "public"
    key = _stats_cache_key(
        schema=schema,
        is_pub=is_pub,
        start=start,
        end=end,
        page_index=page_index,
        page_size=page_size,
    )
    cached = cache.get(key)
    if cached is not None:
        return cached

    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        try:
            payload = client.order_stat(
                is_pub=is_pub,
                s_date=f"{start.isoformat()} 00:00:00",
                e_date=f"{end.isoformat()} 23:59:59",
                page_index=page_index,
                page_size=page_size,
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"读取代练通按日统计失败（{exc.message}）。请稍后重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"读取代练通按日统计失败（上游提示：{exc.message}）。"
            ) from exc
    finally:
        _close(client)

    data = _normalise_stat_payload(payload, is_pub=is_pub, start=start, end=end)
    cache.set(key, data, STATS_CACHE_TTL_SECONDS)
    return data


def default_stats_window() -> tuple[date, date]:
    """统计接口的默认窗口：**最近 7 天（含今天）**。"""
    today = timezone.localdate()
    return today - timedelta(days=STATS_DEFAULT_DAYS - 1), today


# ── 代练通侧订单要求模板（⚠️ 与我们自己的「代练要求模板」是两回事）──────


def list_order_templates(
    *,
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """代练通侧订单要求模板列表（`OrderRequireTempList`）。返回 `{items: [{id, title, temp, is_default}], record_count}`；`record_count` 缺失
    时为 `None`（⛔ 不拿 0 冒充「查过了、没有模板」）。"""
    integration = _active_integration(platform)
    client = _open_client(integration)
    try:
        page = _call_require_temp_list(client)
    finally:
        _close(client)
    return {"items": _template_rows(page), "record_count": page.record_count}


def create_order_template(
    *,
    operator,
    title: str,
    temp: str,
    is_default: bool = False,
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """新增代练通侧订单要求模板（`OrderRequireTempAdd`）。
    ⚠️ 上游成功时 `Err` 里装的是**模板 ID**（不是「成功」文案）⇒ 解析不出 ID 时
    ⛔ 不报成功（说「已提交但没核验到模板 ID」）。"""
    _require_operator(operator)
    title, temp = _validate_template_fields(title, temp)
    is_default_value = TEMP_IS_DEFAULT if is_default else TEMP_IS_NOT_DEFAULT

    integration = _active_integration(platform)
    client = _open_client(integration)
    template_id: int | None = None
    try:
        try:
            upstream = client.add_require_temp(
                title=title, temp=temp, is_default=is_default_value
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"新增代练通订单要求模板的结果未知（{exc.message}）。"
                "请打开模板列表核对是否已创建，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次新增模板（上游提示：{exc.message}）。"
            ) from exc

        template_id = _as_int(upstream)
        if template_id is None:
            raise DailiantongPushError(
                "已提交新增模板，但代练通没有返回模板 ID"
                f"（上游原文：{upstream or '空'}），没能确认模板已创建。请打开列表核对。"
            )

        # 回读：新模板必须在列表里、且字段与提交一致。
        rows = _template_rows(_call_require_temp_list(client))
        found = _find_template(rows, template_id)
        if found is None:
            raise DailiantongPushError(
                f"已提交新增模板（上游返回 ID={template_id}），但回读列表里没有这条模板，"
                "没能核验到。请人工核对。"
            )
        if found["title"] != title or found["temp"] != temp:
            raise DailiantongPushError(
                "已提交新增模板，但回读到的标题 / 内容与提交的不一致，没能核验到。"
                "请人工核对。"
            )
    except DailiantongPushError as exc:
        _record_template_action(
            operator, template_id=template_id, title=title, reason=str(exc)
        )
        raise
    finally:
        _close(client)

    _record_template_action(
        operator,
        template_id=template_id,
        title=title,
        reason="新增模板成功（回读列表已存在且字段一致）",
    )
    return {
        "id": template_id,
        "title": title,
        "temp": temp,
        "is_default": is_default,
    }


def update_order_template(
    *,
    operator,
    temp_id,
    title: str | None = None,
    temp: str | None = None,
    is_default: bool | None = None,
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """修改代练通侧订单要求模板（`OrderRequireTempUpdate`）；回读列表逐项比对。"""
    _require_operator(operator)
    temp_id = _validate_template_id(temp_id)
    if title is None and temp is None and is_default is None:
        raise DailiantongPushError("没有要修改的内容（标题 / 内容 / 是否默认至少给一个）。")
    title = _validate_optional_template_text(
        title,
        label="模板标题",
        max_length=ORDER_TEMPLATE_TITLE_MAX_LENGTH,
    )
    temp = _validate_optional_template_text(
        temp,
        label="模板内容",
        max_length=ORDER_TEMPLATE_CONTENT_MAX_LENGTH,
    )

    integration = _active_integration(platform)
    client = _open_client(integration)
    display_title = title or f"模板 {temp_id}"
    try:
        try:
            client.update_require_temp(
                temp_id,
                title=title,
                temp=temp,
                is_default=(
                    None
                    if is_default is None
                    else (TEMP_IS_DEFAULT if is_default else TEMP_IS_NOT_DEFAULT)
                ),
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"修改代练通订单要求模板的结果未知（{exc.message}）。"
                "请打开模板列表核对，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次修改模板（上游提示：{exc.message}）。"
            ) from exc

        rows = _template_rows(_call_require_temp_list(client))
        found = _find_template(rows, temp_id)
        if found is None:
            raise DailiantongPushError(
                f"已提交修改模板（ID={temp_id}），但回读列表里找不到这条模板，"
                "没能核验到。请人工核对。"
            )
        mismatches: list[str] = []
        if title is not None and found["title"] != title:
            mismatches.append(f"标题「{found['title']}」≠「{title}」")
        if temp is not None and found["temp"] != temp:
            mismatches.append("内容不一致")
        if is_default is not None and found["is_default"] is not is_default:
            mismatches.append(f"是否默认「{'是' if found['is_default'] else '否'}」")
        if mismatches:
            raise DailiantongPushError(
                "已提交修改模板，但回读不一致（" + "、".join(mismatches) + "），"
                "没能核验到。请人工核对。"
            )
        display_title = found["title"] or display_title
    except DailiantongPushError as exc:
        _record_template_action(
            operator, template_id=temp_id, title=display_title, reason=str(exc)
        )
        raise
    finally:
        _close(client)

    _record_template_action(
        operator,
        template_id=temp_id,
        title=display_title,
        reason="修改模板成功（回读字段一致）",
    )
    return found


def delete_order_template(
    *,
    operator,
    temp_id,
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """删除代练通侧订单要求模板（`OrderRequireTempDelete`）；回读列表确认已消失。"""
    _require_operator(operator)
    temp_id = _validate_template_id(temp_id)

    integration = _active_integration(platform)
    client = _open_client(integration)
    display_title = f"模板 {temp_id}"
    try:
        rows_before = _template_rows(_call_require_temp_list(client))
        before = _find_template(rows_before, temp_id)
        if before is not None and before["title"]:
            display_title = before["title"]

        try:
            client.delete_require_temp(temp_id)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"删除代练通订单要求模板的结果未知（{exc.message}）。"
                "请打开模板列表核对，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次删除模板（上游提示：{exc.message}）。"
            ) from exc

        rows_after = _template_rows(_call_require_temp_list(client))
        if _find_template(rows_after, temp_id) is not None:
            raise DailiantongPushError(
                f"已提交删除模板（ID={temp_id}），但回读列表里仍然有这条模板，"
                "没能核验到。请人工核对。"
            )
    except DailiantongPushError as exc:
        _record_template_action(
            operator, template_id=temp_id, title=display_title, reason=str(exc)
        )
        raise
    finally:
        _close(client)

    _record_template_action(
        operator,
        template_id=temp_id,
        title=display_title,
        reason="删除模板成功（回读列表已不存在）",
    )
    return {"id": temp_id, "deleted": True}


def set_default_order_template(
    *,
    operator,
    temp_id,
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """设默认代练通侧订单要求模板（`OrderRequireTempSetDefault`）；回读列表确认 `IsDefault`。"""
    _require_operator(operator)
    temp_id = _validate_template_id(temp_id)

    integration = _active_integration(platform)
    client = _open_client(integration)
    display_title = f"模板 {temp_id}"
    try:
        try:
            client.set_default_require_temp(temp_id)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"设默认代练通订单要求模板的结果未知（{exc.message}）。"
                "请打开模板列表核对，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次设默认模板（上游提示：{exc.message}）。"
            ) from exc

        rows = _template_rows(_call_require_temp_list(client))
        found = _find_template(rows, temp_id)
        if found is None:
            raise DailiantongPushError(
                f"已提交设默认模板（ID={temp_id}），但回读列表里找不到这条模板，"
                "没能核验到。请人工核对。"
            )
        if not found["is_default"]:
            raise DailiantongPushError(
                f"已提交设默认模板（ID={temp_id}），但回读到的 `IsDefault` 仍是「否」，"
                "没能核验到。请人工核对。"
            )
        display_title = found["title"] or display_title
    except DailiantongPushError as exc:
        _record_template_action(
            operator, template_id=temp_id, title=display_title, reason=str(exc)
        )
        raise
    finally:
        _close(client)

    _record_template_action(
        operator,
        template_id=temp_id,
        title=display_title,
        reason="设默认模板成功（回读 IsDefault 已为是）",
    )
    return found


# ── 模板入参校验（视图层已有序列化器；服务层再挡一层，便于直接调用）──────


def _validate_template_id(value) -> int:
    try:
        temp_id = int(value)
    except (TypeError, ValueError) as exc:
        raise DailiantongPushError("模板 ID 必须是整数。") from exc
    if temp_id <= 0:
        raise DailiantongPushError("模板 ID 必须是正整数。")
    return temp_id


def _validate_template_text(
    value: str,
    *,
    label: str,
    max_length: int,
) -> str:
    text = (value or "").strip()
    if not text:
        raise DailiantongPushError(f"{label}不能为空。")
    if len(text) > max_length:
        raise DailiantongPushError(f"{label}不能超过 {max_length} 个字符。")
    return text


def _validate_optional_template_text(
    value: str | None,
    *,
    label: str,
    max_length: int,
) -> str | None:
    """`None` = 这次不改这一项（⛔ 不把「没给」当成空串发上去）。"""
    if value is None:
        return None
    return _validate_template_text(value, label=label, max_length=max_length)


def _validate_template_fields(title, temp) -> tuple[str, str]:
    return (
        _validate_template_text(
            title,
            label="模板标题",
            max_length=ORDER_TEMPLATE_TITLE_MAX_LENGTH,
        ),
        _validate_template_text(
            temp,
            label="模板内容",
            max_length=ORDER_TEMPLATE_CONTENT_MAX_LENGTH,
        ),
    )


# ── 补款（`LevelOrderModify_Special` kind=0）──────────────────
# ⚠️ 与「加价」（`LevelOrderUpPrice`）不是一件事： - 加价 = 改单的**代练价**，只有未接手能做； - 补款 = 在已接手的单上**追加**一笔代练费（`Kind=0` + `Val=金额`，要支付密码），
# 实测（§6.2）「补款 1 元」把 `Price 2 → 3`、冻结也跟着 `2 → 3`。本仓此前**没有**这个动作的服务封装（`dailiantong_client.modify_special` 有 `kind=0` 能力） ⇒ 详情页「补款」按文档补上这条服务。


def add_push_amount(
    order,
    *,
    operator,
    amount,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """给代练通订单**补款**（`LevelOrderModify_Special` 的 `Kind=0`）。 ## 判据 - 金额必须**整数元、≥ 1**（`_integer_yuan_amount`；真机 `-5` / `-3` 的本地前置）； - 写前回读：状态必须是**正在代练（12）或待验收（13）**（`AMOUNT_STATUSES`）——
    未接手 / 异常 / 锁定 / 撤销中 / 已结算一律中文拒绝、**零上游写调用**； - 写前回读：已结算 / 已强制撤销 ⇒ `_guard_terminal` 中文拒绝（零写调用）； - **补款会占我方可用余额**（实测冻结随价格一起涨）⇒ 写前过余额闸（`need = amount`，与发单/改价同一条 A1 口径）；
    - 写后回读 `Price` **必须等于原价 + 补款额**；核验不到 / 不等 ⇒ 中文错误、 ⛔ 不改本地行（升级 `unknown` 会挡住后续人工处理，而且钱可能已经扣了）。 ⚠️ 上游**不推回调**（§6.2 结论 1）⇒ 必须回读才算数。 ⚠️ 发上游的 `Val` 是**整数字面量**（`"1"`，不是 `"1.00"`）：真机对小数回 `-5`。"""
    _require_operator(operator)
    amount = _integer_yuan_amount(amount, label="补款金额")

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    old_price_text = _fmt_money(push.price)
    new_price_text = ""
    try:
        detail = _read_detail(client, push, action_name="补款")
        _guard_terminal(detail, action_name="补款")
        status = _as_int(detail.get("Status"))
        if status not in AMOUNT_STATUSES:
            raise DailiantongPushError(
                "补款只能给「正在代练」或「待验收」的代练通订单做，"
                f"这张单当前状态是「{_status_label(status)}」。"
            )

        base_price = detail.get("Price")
        if base_price is None:
            base_price = push.price
        try:
            base = _to_decimal(base_price)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DailiantongPushError(
                "代练通详情里没有可识别的当前价格，不能补款。"
                "请稍后重试、或到代练通后台核对。"
            ) from exc
        expected = base + amount
        old_price_text = _fmt_money(base)
        new_price_text = _fmt_money(expected)

        # ⭐ A1 口径：补款会真的加冻结，余额必须先够（只算这一笔，与加价同源）。
        _check_active_balance(client, need=amount)

        try:
            client.modify_special(
                push.serial_no, kind=SPECIAL_KIND_AMOUNT, val=int(amount)
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"补款结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的价格，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次补款（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="补款")
        actual = detail_after.get("Price")
        if not _money_equals(actual, expected):
            raise DailiantongPushError(
                "已提交补款，但回读代练通的价格是"
                f"「{actual if actual is not None else '空'}」，"
                f"预期「{_fmt_money(expected)}」，没能核验到。请人工核对。"
            )

        push.price = expected
        _save_remote_snapshot(push, detail_after, extra_fields=("price",))
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_AMOUNT,
            reason=str(exc),
            from_value=old_price_text,
            to_value=new_price_text,
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_AMOUNT,
        reason="回读 Price 已等于原价加补款额",
        from_value=old_price_text,
        to_value=new_price_text,
    )
    return push


# ── 编辑平台订单（`LevelOrderModify`，只改非金额字段）───────────


#: 详情里 `Actors` 拆成的六段（§6.1 第 2 条实测：`GameAcc` / `GamePass` / `EnGamePass` /
#: `Actor` / `CurrInfo` / `Require`）。⚠️ `EnGamePass` 是**密文串**，⛔ 不用它拼回 `Actors`。
_ACTORS_DETAIL_FIELDS = ("GameAcc", "GamePass", "Actor", "CurrInfo", "Require")


def _actors_from_detail(detail: dict) -> str:
    """把详情回吐的账号资料拼回 `Actors` 五段（认不出任何一段 ⇒ 空串，⛔ 不编）。"""
    parts = [str(detail.get(name) or "").strip() for name in _ACTORS_DETAIL_FIELDS]
    if not any(parts):
        return ""
    return "|*|".join(parts)


def _detail_value(detail: dict, *names: str):
    """按候选键名（大小写不保证）从详情里取第一个**存在且非空**的值。"""
    folded = {str(key).strip().lower(): value for key, value in detail.items()}
    for name in names:
        value = folded.get(name.strip().lower())
        if value is not None and str(value).strip() != "":
            return value
    return None


def edit_push_order(
    order,
    *,
    operator,
    title: str | None = None,
    memo: str | None = None,
    requirement: str | None = None,
    zone_server_id: str | None = None,
    groups: str | None = None,
    label1: str | None = None,
    label2: str | None = None,
    platform: int = DEFAULT_PLATFORM,
) -> DailiantongPush:
    """编辑代练通订单的**非金额字段**（`LevelOrderModify`）。 ## 能改什么、为什么只改这些 - 可改：`title`（标题）/ `memo`（备忘）/ `requirement`（`Actors` 第五段 = 代练要求）/ `zone_server_id`（区服）/ `groups`（频道）/ `label1`、`label2`（标签）。 - ⛔ **金额一律不在这里改**：改价走 `…/platform-push/price/`（它有自己的状态闸与回读）。
    本函数把详情里的 `Price` / `Ensure1` / `Ensure2` / `TimeLimit` **原样回传**，代价是「上游把这些字段当必填」时也能过，且**不会顺手改钱**。 - ⚠️ **只对「未接手 / 已下架」的单生效**（实测 `-1 只有未接手或已下架的订单才可以修改`）：写前先按 `Status ∈ {11, 18}` 判（零写调用），其它状态给中文错误。
    ⭐ L4：`Status=18 已下架` **放行**——上游原话就是「未接手**或已下架**」才能改，旧实现只放 11，把同样可编辑的 18 挡住了（与上架闸 H7 同族残条）。 - 账号资料（`Actors`）优先用**详情回吐的五段**（保住已经在代练通上的内容）；详情没回吐时才用本单的本地占位模板 + 页面给的要求。 ## 成败判据写后回读详情：改了标题就比 `Title`、改了要求就比 `Require`（详情回吐了才比；没回吐 ⇒ 文案里说「未能核对」，⛔ 不假报成功）。核验通过后同步本地行。"""
    _require_operator(operator)
    normalized_title = (title or "").strip() or None
    if normalized_title is not None:
        if len(normalized_title) < push_module.TITLE_MIN_LENGTH:
            raise DailiantongPushError(
                f"标题至少 {push_module.TITLE_MIN_LENGTH} 个字符。"
            )
        if len(normalized_title) > push_module.TITLE_MAX_LENGTH:
            raise DailiantongPushError(
                f"标题不能超过 {push_module.TITLE_MAX_LENGTH} 个字符"
                f"（当前 {len(normalized_title)} 个）。"
            )
    if memo is not None and len(memo) > push_module.MEMO_MAX_LENGTH:
        raise DailiantongPushError(
            f"订单说明不能超过 {push_module.MEMO_MAX_LENGTH} 个字符。"
        )
    if requirement is not None and len(requirement) > push_module.REQUIRE_TEMPLATE_MAX_LENGTH:
        raise DailiantongPushError(
            "代练要求不能超过 "
            f"{push_module.REQUIRE_TEMPLATE_MAX_LENGTH} 个字符。"
        )
    groups = (groups or "").strip() or None
    if groups is not None and len(groups) > push_module.GROUPS_MAX_LENGTH:
        raise DailiantongPushError(
            f"发单频道不合法（超过 {push_module.GROUPS_MAX_LENGTH} 个字符）。"
        )
    label1 = (label1 or "").strip() or None
    label2 = (label2 or "").strip() or None
    for label_name, label_value in (("标签1", label1), ("标签2", label2)):
        if label_value is not None and len(label_value) > push_module.LABEL_MAX_LENGTH:
            raise DailiantongPushError(
                f"{label_name}不能超过 {push_module.LABEL_MAX_LENGTH} 个字符。"
            )
    selected_zone = (zone_server_id or "").strip() or None

    if not any(
        [
            normalized_title,
            memo is not None,
            requirement is not None,
            selected_zone,
            groups,
            label1,
            label2,
        ]
    ):
        raise DailiantongPushError(
            "这次没有任何要修改的内容，请至少填一项（标题 / 订单说明 / 代练要求 / "
            "区服 / 频道 / 标签）。"
        )

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    old_summary = ""
    try:
        detail = _read_detail(client, push, action_name="编辑订单")
        _guard_terminal(detail, action_name="编辑订单")
        status = _as_int(detail.get("Status"))
        # ⭐ L4：上游口径 = 「未接手**或已下架**」都能改（实测原话）；
        # 只放 11 会把同样可编辑的 18 挡住。
        if status not in (REMOTE_STATUS_UNACCEPTED, REMOTE_STATUS_OFF_SHELF):
            raise DailiantongPushError(
                "只有「未接手 / 已下架」的代练通订单才能编辑，"
                f"这张单当前状态是「{_status_label(status)}」。"
            )

        def _required(name: str):
            value = _detail_value(detail, name)
            if value is None:
                raise DailiantongPushError(
                    f"代练通详情里没有「{name}」，无法安全地提交编辑"
                    "（改单要求把其余字段一起带上）。请稍后重试、或到代练通后台核对。"
                )
            return value

        new_title = normalized_title or str(_required("Title"))
        new_time_limit = _required("TimeLimit")
        new_price = _required("Price")
        new_ensure1 = _required("Ensure1")
        new_ensure2 = _required("Ensure2")
        old_summary = f"标题：{str(_detail_value(detail, 'Title') or '')}"

        actors = _actors_from_detail(detail)
        if not actors:
            # 详情没回吐账号资料 ⇒ 用本地占位模板（页面给的代练要求优先）。
            requirement_text = (requirement or "").strip()
            if not requirement_text:
                requirement_text = push_module.require_template_from_settings()
            actors = push_module.build_actors(order, requirement=requirement_text)
        elif requirement is not None:
            # 详情回吐了账号资料：只替换第五段（代练要求），其余四段原样保留。
            parts = actors.split("|*|")
            while len(parts) < 5:
                parts.append("")
            parts[4] = (requirement or "").strip()
            actors = "|*|".join(parts[:5])

        new_memo = memo if memo is not None else _detail_value(detail, "Memo")
        new_label1 = label1 if label1 is not None else _detail_value(detail, "Label1")
        new_label2 = label2 if label2 is not None else _detail_value(detail, "Label2")

        try:
            client.modify_order(
                push.serial_no,
                title=new_title,
                time_limit=new_time_limit,
                price=new_price,
                ensure1=new_ensure1,
                ensure2=new_ensure2,
                zone_server_id=selected_zone,
                memo=(str(new_memo) if new_memo is not None else None),
                actors=actors,
                groups=groups,
                label1=(str(new_label1) if new_label1 is not None else None),
                label2=(str(new_label2) if new_label2 is not None else None),
            )
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"编辑代练通订单的结果未知（{exc.message}）。"
                "请到代练通后台核对这张单的内容，再决定是否重试。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这次编辑（上游提示：{exc.message}）。"
            ) from exc

        detail_after = _readback_detail(client, push, action_name="编辑订单")
        unverified: list[str] = []
        if normalized_title is not None:
            actual_title = _detail_value(detail_after, "Title")
            if actual_title is None:
                unverified.append("标题（详情没回吐 Title）")
            elif str(actual_title) != normalized_title:
                raise DailiantongPushError(
                    "已提交编辑，但回读代练通的标题是"
                    f"「{actual_title}」，与提交的「{normalized_title}」不一致，"
                    "没能核验到。请人工核对。"
                )
        if requirement is not None:
            actual_require = _detail_value(detail_after, "Require")
            if actual_require is None:
                unverified.append("代练要求（详情没回吐 Require）")
            elif str(actual_require) != (requirement or "").strip():
                unverified.append("代练要求（回读与提交不一致）")

        _save_remote_snapshot(push, detail_after)
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_EDIT,
            reason=str(exc),
            from_value=old_summary,
        )
        raise
    finally:
        _close(client)

    reason = "已提交编辑"
    if unverified:
        reason += "；未能核对：" + "、".join(unverified)
    else:
        reason += "，回读已核对"
    _record_action(
        push,
        order,
        operator,
        action=AUDIT_EDIT,
        reason=reason,
        from_value=old_summary,
        to_value=f"标题：{str(_detail_value(detail_after, 'Title') or new_title)}",
    )
    return push


# ── 给已有平台单追加截图────────────────────────────────────


def add_push_screenshot(
    order,
    *,
    operator,
    data: bytes,
    declared_content_type: str = "",
    caption: str = "",
    platform: int = DEFAULT_PLATFORM,
) -> dict:
    """给已经在代练通的单**追加一张进度图**（`GetSTS` → `put_object` → `progress_add` → 回读）。三步（与接手后发图同一套协议，`docs/DAILIANTONG-API.md` §4 #15 / #16）： 1. `GetSTS` 取 OSS 临时凭证（⛔ 不缓存、⛔ 不落库）；
    2. `put_object` 上传到**租户前缀**的对象名（`tt/<schema>/<我方单号>/shot-<毫秒>.<ext>`）； 3. `progress_add` 写进度；⚠️ 它**会假成功**（对不存在的单号也回 `添加完成`） ⇒ 必须回读 `progress_list` 看到这个对象名才算成功。
    返回 `{"img": 对象名, "caption": …, "url": ""}`（键名即前端契约）。 ⚠️ 图种以**字节魔数**为准（`sniff_image_bytes`），⛔ 不信 `Content-Type` / 扩展名。 ⚠️ 失败分层同本模块其余动作：一律 `DailiantongPushError`（中文、可展示、⛔ 不 500）。"""
    _require_operator(operator)
    if not isinstance(data, bytes) or not data:
        raise DailiantongPushError("请选择要上传的图片文件。")
    content_type = sniff_image_bytes(data)
    if content_type is None:
        raise DailiantongPushError("图片类型无法识别（只支持 jpg / png）。")
    ext = IMAGE_EXT_BY_CONTENT_TYPE[content_type]
    text = (caption or "").strip()[:PUSH_SCREENSHOT_CAPTION_MAX_LENGTH] or (
        PUSH_SCREENSHOT_DEFAULT_CAPTION
    )

    push = _latest_active_push(order, platform)
    integration = _active_integration(platform)
    client = _open_client(integration)
    object_key = ""
    try:
        detail = _read_detail(client, push, action_name="追加截图")
        _guard_terminal(detail, action_name="追加截图")

        try:
            sts = client.get_sts()
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"取 OSS 临时凭证失败（{exc.message}），没有上传图片。"
            ) from exc

        object_key = qr_module.build_object_key(
            order.order_no, ext, tag=SCREENSHOT_OBJECT_TAG
        )
        try:
            put_object(sts, object_key, data, content_type)
        except OssUploadError as exc:
            raise DailiantongPushError(exc.message) from exc

        try:
            client.progress_add(push.serial_no, text, img=object_key)
        except DailiantongTransportError as exc:
            raise DailiantongPushError(
                f"上传成功但提交进度的结果未知（{exc.message}）。"
                "请稍后刷新进度、或到代练通后台核对这张图是否已出现。"
            ) from exc
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"代练通没有接受这张进度图（上游提示：{exc.message}）。"
            ) from exc

        # ⛔ 自证：`LevelOrderProgressAdd` 会假成功 ⇒ 回读进度列表看到对象名才算数。
        try:
            page = client.progress_list(
                push.serial_no, max_count=qr_module.PROGRESS_READBACK_MAX
            )
        except DailiantongError as exc:
            raise DailiantongPushError(
                f"已提交进度图，但回读进度列表失败（{exc.message}），没能核验。"
            ) from exc
        if not qr_module._progress_contains(page.rows, object_key):
            raise DailiantongPushError(
                "已提交进度图，但回读进度列表里没看到这张图，没能核验。"
                "请稍后刷新进度、或到代练通后台核对。"
            )
    except DailiantongPushError as exc:
        _record_action(
            push,
            order,
            operator,
            action=AUDIT_PROGRESS,
            reason=str(exc),
        )
        raise
    finally:
        _close(client)

    _record_action(
        push,
        order,
        operator,
        action=AUDIT_PROGRESS,
        reason="回读进度列表已确认出现这张图",
    )
    return {"img": object_key, "caption": text, "url": ""}
