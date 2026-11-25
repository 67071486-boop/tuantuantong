"""团团通 ← 上家鸡蛋铺（客服个人绑定）：本线主体模块（决策 D-0176；spec=`docs/specs/2026-09-28-upstream-jidanpu-sync-design.md`，计划=`docs/plans/2026-09-28-upstream-jidanpu-sync.md`）。本线与下家线（`jidanpu_member`）的表、行、开关互不共用；租户级绑定那张表已拆除；`jidanpu.py` 只当建连和上游原语用。板块：①检测与取数原语——端点常量（⛔ 复用 `jidanpu.SET_STATUS_PATH` 不复制字面量）、`_fetch_page`/`fetch_my_orders`（可带时间窗/状态/`key1`，见 `my_orders_window` 与 plan `2026-10-04-my-orders-windowed-pull.md`）、⭐ 上传页隐藏域取值 `fetch_upload_page_fields`（`sid`/`gameAccId`/`taskId` 唯一出处）、四项只读检测 `detect_binding`+`DetectionReport`； ②大厅镜像——`pull_all_pages`（⛔ 不用 SDK `iter_all`：默认 page_size=100 判尾页而上游每页固定 20 ⇒ 第一页就静默少拿，spec B5/§13.1-R3）、地址级去重 `enabled_bindings`/`dedupe_owner`（A2：同地址只有 `id` 最小的启用绑定拉大厅 ⛔ 指派单那一路不走）、⭐ 游戏四级匹配 `match_game_by_upstream_name`（A15/spec §16.1 唯一出处：精确→归一化→子串→不中 ⇒ 该行 skipped+「加别名」台账 ⛔ 绝不硬塞）、`sync_hall`（镜像幂等键=`(上家地址, orderId)` ⛔ 匹配键一律 `orderId` 绝不用可见单号 `orderIdView`）、大厅读口 `upstream_hall_queryset`/`upstream_hall_rows`/`serialize_upstream_row` 与两路归并 `merge_hall_rows_two`（行形状恰好同键 ⛔ 不另造一套筛选；P2 第三路已随 `2026-10-08-remove-thirdparty-jidanpu-sync.md` 拆除，只收两路）； ③指派单 ⇒ 未分配池——在途门槛 `IN_FLIGHT_TOKENS`（A7/spec §5.2 唯一出处：`orderSta` 不在集合的行一律不录+台账人话，否则首次绑定把该账号全部历史单灌进未分配池）、`sync_my_orders`、`record_assigned_order`（`status=UNASSIGNED`、录单人=该绑定的客服：`sales_user`+`sales_agent` 快照+`create_performance_for_order` 一次建两条绩效行；客户价 `total_price ← recFee`（A9/§15.1：上家 `price` 一个金额列都不落）、`hall_price`=勾兑发单价（`recFee × hall_price_percent / 100`，与大厅线转正共用 `upstream_hall_price`））、`_ensure_link`（幂等键=`(binding, 上家单号)`；并发撞唯一约束 ⇒ `IntegrityError` 连同这次新建的订单一起回滚，调用方按「已做过」处理）、⭐ 复活重录 `_reassign_resurrected_order`（D-0324 生产实锤：已退单被重新指派 ⇒ 重录新单+链路重置挂到新单，旧退单一字不动留作历史；24h 窗外由 `recheck_terminated_links` 兜底重核、不进每轮③轮转——WAF 压发数）；⚠️ 这一路不套地址级去重（上家把单指派给哪个账号就录到哪个客服名下，与「谁负责拉大厅」两件事，见 `dedupe_owner` docstring）。
④四项检测（spec §9.2）与证明力边界：①严格模式登录成功（`LOGIN_FAILED`）；②大厅第 1 页可读；③「我的订单」可读（②③壳里 `code == 0`，⛔ 不靠 SDK 宽松解包——失败信封 `{"code":1,"msg":"没有权限","data":[]}` 会被它放行 ⇒ 误绿，E-0087 读侧同族，见 `_require_read_success`）；④上传页三个隐藏域全非空（`UPLOAD_FIELDS_UNREADABLE`）。⚠️ 四项只覆盖「读」：三个写动作（`RecOrderByInternal`/`SetOrderSta`/`UploadOrderPic`）没有页面可探、也不许拿真实动作去试 ⇒ 检测只是预检，真判据=写失败要响亮报错+落台账+写绑定 `last_error`；⚠️ 登录失败即短路（照 `jidanpu_member.detect_binding` 同一口径：无会话只会「请先登录」/302 ⇒ 是检测本身失败，报成没权限会让人去改一个没坏的权限设置）；⚠️ `ApiError` 以外的失败（`TransportError`/`ServerError`/`RateLimitError`/`SessionExpiredError`）一律原样上行——那是检测本身失败，别指错方向；第 4 项上传页照此分档（见 `fetch_upload_page_fields`）。 `sid` 取法（✅ T7 已实测闭合 ⛔ 不用再探位置）：来自上传页隐藏域（`fetch_upload_page_fields` 把隐藏输入读成 dict，三个值全部从这里取 ⛔ 一个都不硬编码、⛔ 不猜位置）；⚠️ 「换一个 `sid` 会不会被拒」仍未验（spec T8）⛔ 别写成已验；⚠️ 检测时 `remote_order_id` 取「我的订单」第一条、回落大厅第 1 页第一条、再没有才用空串去探（`sid` 是店铺级标识）——这条回落未真机验证（spec §2.7 G3 只覆盖「有单时」）⇒ 真机联调要核一次：若空 `orderId` 拿不到隐藏域，第 4 项会误红挡住启用。 ⑤状态/内容回拉+上家退单自动跟退——`map_remote_status` 收窄版（spec §7 主表+两处收窄改准块：`DYS`/待验收 ⇒ `None`，我方自己提交完成的回音，照旧表回拉会真的把已完成单回退并静默清空完单时间；`WFP`/未分配/待抢单 ⇒ `None`，本线由镜像行表达；其余沿用 `jidanpu_member` 的码表含「认不出来 ⇒ 问题单」兜底）；⭐ 写入面只开一格 `should_write_status`（spec §5.3：只有「我方=代练中」才允许被上游状态推进，其余一切状态含我方已完成 ⇒ 只登记一个字不写——读码依据：`order_status.TRANSITIONS` 边表整体放开、`COMPLETED → PENDING_REVIEW` 是合法边拦不住，而 `apply_transition` 离开 `COMPLETED` 会清空 `completed_at` ⇒ 必须在这里挡住）；⭐ 上家退单（`YTD`）⇒ 自动跟退 `follow_refund`（A11/spec §15.3：走既有退单原语 `apply_transition(→ REFUNDED)` ⇒ 绩效侧由它按既有口径一起回退；⛔ 不自己写 `refund_fee`、⛔ 不自己动绩效、⛔ 不自己建 `OrderRefund`——退单不再建 `OrderRefund(kind=FULL)` 事件行、不写 `refund_fee`，含上家跟退，口径见 `docs/specs/2026-10-03-refund-amount-and-restore-sync-design.md` §G2/§G5-3）；⚠️ 已结账的已完成复用 `jidanpu_common.status_rollback_blocked` 挡住（D-0156 同一口径，残差 §13.1-R21），⭐ 该闸按 D-0287 收窄：`settled_by == "system"`（钱包自动发薪）的行不再挡、照常跟退并由退单钩子回冲；⛔ 财务手工结账的行照旧挡住；两层留痕（spec §15.3 第④步：审计 `order.transition` actor=机器名+台账 `upstream.status_in` 人话）；内容回拉 `_pull_content` 只补我方为空的字段、金额一律不回写，「上游值与我方不同但未覆盖」留一条台账（`upstream.content_in`）；只跟踪在途单 `is_terminated`（spec §5.4：我方 `COMPLETED`+上家 ∈ {待验收, 已完成, 已退单} ⇒ 写链路 `terminated_at`，此单往后不打任何上游读）；`pull_statuses` 独立一轮入口（自己拉一次「我的订单」、只处理已有链路的行；调度常规路径是 `sync_my_orders` 同一份行顺带回拉，spec §5.3；两条路共用「窗口①②并集+在途按单补核③」⭲ `_my_orders_round_rows`/`_check_missing_links` ⛔ 整表拉取已删）。
⑥我方 → 上家的两个写动作——写原语 `rec_order_by_internal`/`submit_completion`（载荷逐字照 spec §2.3 实测：`{"orderId": …}`/`{"orderId": …, "sta": "待验收"}`；成功判据=我们自己判 `code > 0`，GC 7/E-0087 ⛔ 不靠 SDK 宽松解包）；outbox `queue_hall_claim`/`queue_complete`+`consume_event`/`run_outbox`（`KIND_HALL_CLAIM`/`KIND_COMPLETE`；执行租约复用 `jidanpu_common.execution_lease`）；大厅接单请求路径 `claim_upstream_mirror`（本地预占 → 同事务写事件 → 赢下租约才打上游）与转正 `promote_upstream_mirror`（A13：`sales_user`=负责拉大厅那份绑定的客服）；⭐ 转正取事件那份绑定（事件归属=链路/提成归属，不一致时按事件绑定转正+人话台账 ⛔ 不分叉），⛔ 不读会轮换的 `row.owner_binding`；四条硬口径：A16（接单不额外推状态）、A5（传图失败 ⇒ 零提交）、只有「已完成」才提交、G4（先回读再提交/提交后回读确认）——详见文件末尾那一节的注释；⭐ 其余硬口径：转正只吞「两边都没价」那一档（判据=`MissingPriceError` 异常类型 ⛔ 不是恒假的「当时有没有价」）、显式动作给烧完额度的行重新发额度（⛔ 不静默空转）、认不出「已被接走」时人话点明需人工核对。"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser

import httpx
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Greatest, Now
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from jidanpu.errors import (
    ApiError,
    AuthError,
    JidanpuError,
    ServerError,
    SessionExpiredError,
    TransportError,
)

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu
#: ⚠️ 从既有公共层复用行形状基底与几条台账文案（⛔ 不复制一份：两处各自漂移会让前端「一张表渲染两套键」与「同一个事实两种说法」静默分叉）；
#: ⭐ `jidanpu_common.status_rollback_blocked`（「已结账挡住」）与 `jidanpu_member` 的状态码表（映射收窄版）都是读它们的公开口径 ⛔ 不复制第二份。
from apps.business import jidanpu_common, jidanpu_member
from apps.business.claim_channel import record_successful_claim
from apps.business.claim import ClaimError, GrabCandidate, check_grab_limits
from apps.business.jidanpu_common import (
    GONE_REASON,
    JIDANPU,
    KIND_RESCUED,
    MISSING_ORDER_ID_REASON,
    NO_COUNT_REASON,
    REMOTE_ROW_BASE,
    RESCUE_REASON,
    TERMINAL_REAPPEAR_REASON,
)
from apps.business.models import (
    EmpType,
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderScreenshot,
    OrderStatus,
    OrderType,
    Performance,
)
from apps.business.order_no import generate_order_no
from apps.business.order_status import IllegalTransition, apply_transition, can_transition
from apps.business.performance import create_performance_for_order, set_taker
from apps.business.performance_amount import MissingPriceError
from apps.business.upstream_game_rules import allowed_upstream_game, upstream_game_price_percent
from apps.core.datetime_display import local_iso
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import nodes_of

logger = logging.getLogger(__name__)

# ── 端点常量（spec §2.1 / §2.3 / §2.4；⛔ 提交完成复用 `jidanpu.SET_STATUS_PATH`）────────

#: 上家**抢单大厅**取数口（打手账号可读；spec §2.1 **实测**）。
#: ⚠️ 与租户级那条线用的 `GetOrderList` **不是一个端点**：后者对打手账号返回 **0 行**（spec §2.2）。
HALL_PATH = "/OrderAjax/GetInternalOrderGrabbingList"
#: 「指派给我的订单」取数口（本线的主取数口；spec §2.1 / §2.2 **实测**）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"
#: 大厅接单（写；spec §2.3 **实测**）。
REC_ORDER_PATH = "/OrderAjax/RecOrderByInternal"
#: 传图第三步（写；spec §2.4 **实测**）。
UPLOAD_PIC_PATH = "/OrderAjax/UploadOrderPic"
#: 判断上家有没有图（读；spec §2.4 **实测**：`msg.data` 非空即已有图）。
VIEW_PIC_PATH = "/OrderAjax/ViewPic"
#: 阿里云 OSS 直传签名（读；spec §2.4 **实测**，走 SDK）。
OSS_POLICY_PATH = "/CommonPermissionAjax/GetAliOssPolicy"
#: ⭐ **上传页**——不是 JSON 端点，要直接看 HTTP（`sid` / `gameAccId` / `taskId` 的出处）。
UPLOAD_PAGE_PATH = "/Order/UploadPic"

#: 机器动作在审计里的 `actor`（`actor_id` 一律留空：不是真人点的）。
MACHINE_ACTOR = "上家鸡蛋铺同步"

#: 上传页隐藏域里本线**必须要到**的三个键（缺任何一个 ⇒ 截图链走不通；spec §2.4）。
REQUIRED_UPLOAD_FIELDS: tuple[str, ...] = ("sid", "gameAccId", "taskId")

#: ⚠️ 上游列表端点每页固定 20 行（spec §2.1 实测，本线翻页只带 `page`、不带 `limit`）。⛔ 它不参与任何判据（加固把原先依赖它的「末页是否满页」判据删了：上游改小页大小时会静默退化）——
#: 现在只作背景事实留在这里，判据见 `_pull_all_pages_detail` 的「探页」与 `SUSPECT_SHORT_COUNT_REASON`。

#: 一次性查询的保护上限。定时同步用持久窗口分批续拉。
MY_ORDERS_MAX_PAGES = 50

#: ⭐ 「我的订单」一轮的**时间窗**（小时）：最近 **24** 小时（计划 §1-①）。
#: ⚠️ 窗口按**调用当刻**算（`my_orders_window`），⛔ 不写模块级常量——模块级常量在 import 时
#: 算一次，常驻同步进程活多久就错多久（`jidanpu._today` 的注释记过同一个坑）。
MY_ORDERS_WINDOW_HOURS = 24
#: ⭐ 退单视图并集用的状态筛选值（计划 §1-②）：同窗口再拉一次这一档，与默认视图按 `orderId` 取并集；⛔ 不声明「退单一定看得见」：它的可见性真机两轮读数相反、未定论（计划 §0 最后一行）。「已退单」那一档的状态标签（真机实测：中文标签 ⛔ 不是 code）；
#: ⚠️ ⛔ 用它的那一发绝不能带时间窗：受控实验实测「`orderSta=已退单`+任意 `stime`/`etime`（1 小时…30 天全试过）」恒为 0 行（那张单的录单时间明明落在窗内）⇒ 带了窗=没问；
#: ⚠️ 也不要用「不带 `key1` 的退单视图」去兜：真机实测那一发回的是整张表（含非退单行），单量一上来就是上千页 ⇒ 本线只用「`key1`+它」这一种形态（见 `_my_order_row_by_key1`）。
MY_ORDERS_REFUND_STATUS = "已退单"
#: ⭐ 在途链路按单补核的每轮上限（计划 §1-③）：没出现在①②行集里的在途链路，按 `last_in_at` 最旧优先逐单 `key1` 查这么多张；超出的下一轮轮到（⛔ 不饿死同一批）。
#: ⚠️ 值定为 5（`docs/plans/2026-10-20-upstream-waf-risk-reduction.md` §2.4）：对端 WAF 就是在请求量偏大时把来源 IP 封掉的（登录一律 468）⇒ 每轮按单补核的发数要压下来。代价如实登记：在途链路多过 5 条时，补核轮转一圈从 `⌈在途/20⌉` 轮变成 `⌈在途/5⌉` 轮（在途 20 张 ⇒ 从 1 轮变 4 轮；按「我的订单」默认 60 秒一拍算，最旧的单最坏 4 分钟才轮到一次）；
#: ⛔ 不因为「这样更慢」再把它调回去：这条上限管的是对上游的发数，不是本地算力。
MY_ORDERS_LINK_CHECK_MAX = 5


# ── ⭐ 自家回环卡拦截（业务方拍板「代码根治回环」，生产探针实锤）────────
# 现场（租户 S）：该租户对同一家鸡蛋铺（chuyin）既当下家会员往外抛、又当上家往回拉 ⇒ 自家抛出去的单（抛出键=`effective_source_order_no`，逐字落在对方 `orderIdView` 上，见 `jidanpu_member.py`/`jidanpu.OWN_SOURCE_VALUE_FIELD`）被对方挂进大厅、再被自己的拉取轮拉回来，以「原价×0.9×0.85」量级（实测 0.765：19.13=25×0.765、53.55=70×0.765）出现在自家打手大厅；判别铁证=回环镜像行的 `upstream_payload.orderIdView` 恰是我方持有的单号（正常第三方单不会带这个特征）。
# ⚠️ 存量行：命中判据的已有镜像行在行循环里转 `gone` 且不复活（`listed`/`skipped` ⇒ 条件更新置 `gone`；`gone` ⇒ 不复活；`claiming`/`promoted` ⇒ 状态一律不翻，防炸并发接手）——台账/日志与「不建镜像」同一形态、天然幂等；库存 20 张存量回环卡的删除仍归数据处置线，本线只让它们不再出现在打手大厅。

#: 回环拦截的台账 kind（形态照 `upstream.hall_skipped` / `upstream.hall_gone` 那一族）。
KIND_HALL_LOOP_INTERCEPT = "upstream.hall_loop_intercept"
#: 回环拦截的台账 `reason`（幂等键的一部分；**固定一句人话**，⛔ 不掺单号——单号在 `target_ref`）。
HALL_LOOP_THROW_REASON = (
    "回环卡拦截：这张上家大厅卡的展示单号恰是我方抛给下家鸡蛋铺的单号"
    "（自家单绕了一圈回来了），不建镜像"
)

def _hall_round_key(binding) -> tuple[str, int]:
    """进程内按 `(上家地址, 绑定 pk)` 记一份状态的键（⛔ 不能用 pk 单键：多租户下 pk 会撞）。"""
    return (binding.jidanpu_base_url, binding.pk)


#: 大厅一轮最多翻多少页（**兜底**，⛔ **不是每页行数**：每页固定 20 行由上游决定，spec §2.1 / B5）。
#: 正常停止条件是「累计行数 ≥ 壳里的 `count`」；这个上限只在**上游骗人**（`count` 远大于真实总量）
#: 或壳里没有 `count` 时兜底，命中即**响亮**报「拉不全」（⛔ 不静默交半份，spec §10）。
HALL_MAX_PAGES = 1000


BINDING_CHANGED_REASON = "上家绑定已换店、换账号或停用，本轮结果已跳过"
REBINDED_EVENT_REASON = "绑定已更换凭据，本事件不再重试，请人工核对"

def status_text(value) -> str:
    """状态 → 中文 label（唯一出处：`OrderStatus(value).label`，spec §7/计划 Task 3）。⚠️ ⛔ 不 import 别人的私有 `_status_text`、⛔ 不写字面量映射表（那两样都会各自漂移）；
    ⚠️ 非法取值照 `OrderStatus` 抛 `ValueError`：那是我们的编程错误，该响亮（E-0010）。"""
    return OrderStatus(value).label


# ── 人话文案（spec §9.2 的四行；⛔ 缺哪条报哪条，绝不来一句笼统的「权限不足」）──────────

#: ① 登录失败（凭据错 / 被拒）。
LOGIN_FAILED = "登录失败：账号或密码错误"
#: ② 打不开上家抢单大厅。
HALL_UNREADABLE = "该账号打不开上家的抢单大厅，请让上家给它开这个权限"
#: ③ 打不开上家的「我的订单」。
MY_ORDERS_UNREADABLE = "该账号打不开上家的『我的订单』，请让上家给它开这个权限"
#: ④ 取不到店铺标识（上传页 302 到无权限页 / 隐藏域**缺项或空值**）。
#: ⚠️ 连接失败 / 5xx / 302 到登录页**不在这里**：那些是检测本身失败，原样抛异常上行。
UPLOAD_FIELDS_UNREADABLE = "取不到上家的店铺标识，截图功能会失败，请先联系上家确认"


def _upload_fields_problem(missing: tuple[str, ...]) -> str:
    """第 4 项失败的人话 + **点出缺的是哪几个键**（方便排障，⛔ 不改变 §9.2 的那句人话）。"""
    return f"{UPLOAD_FIELDS_UNREADABLE}（上传页缺少：{'、'.join(missing)}）"


def _missing_required_fields(fields: dict[str, str]) -> tuple[str, ...]:
    """三个必备隐藏域里键缺失、或值为空的那些（键不在/`value.strip()` 为空）。⚠️ 只判「键在不在」不够：`sid=""`/`gameAccId="   "` 同样是没取到——`sid` 是拼 OSS key 用的纯数字串，空串拼出来的 key 一定失败，而截图那一步才会炸；
    与「缺哪项点名哪项」同口径（人话里要能看出空的是哪几个）。"""
    missing: list[str] = []
    for name in REQUIRED_UPLOAD_FIELDS:
        value = fields.get(name)
        if value is None or not value.strip():
            missing.append(name)
    return tuple(missing)


# ── 报告 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DetectionReport:
    """一次绑定检测的结论。`ok=False` 时 `problems` 一条一因、逐条点名（②③④各自独立 ⛔ 不在第一条失败处停下——用户想一次就知道要处理哪些事）；①登录失败是唯一的短路点（见模块 docstring）；
    ⚠️ `problems` 只装判据层面的失败（登录被拒/某端点读不到/隐藏域缺项）；「网络断/上游 5xx/限流」这类检测本身的失败是抛异常，不进这里。"""

    ok: bool
    problems: tuple[str, ...] = ()


# ── 取数原语 ─────────────────────────────────────────────────────────────────


def _as_rows(payload, path: str) -> tuple[dict, ...]:
    """形状防御：载荷必须是「全是对象的列表」，否则响亮抛 `ApiError`。⛔ 不静默当成空列表（`AGENTS.md` §5-E/E-0004）：那会把「上游改版/我们调用错」说成「远端没有数据」；
    ⚠️ 本函数刻意不 import `jidanpu._as_rows`：那是另一个模块的私有名（计划只允许本线 import 它公开的那几个符号）。"""
    if not isinstance(payload, list):
        raise ApiError(f"{path} 的 data 不是列表（是 {type(payload).__name__}）", code=None)
    for row in payload:
        if not isinstance(row, dict):
            raise ApiError(f"{path} 的行不是对象（是 {type(row).__name__}）", code=None)
    return tuple(payload)


def _envelope_count(client) -> int | None:
    """业务壳里的 `count`（总行数）；拿不到/不是非负整数 ⇒ `None`。⚠️ `bool` 是 `int` 的子类，而 `count: true` 不是条数（照 SDK `iter_all` 的既有判据）；
    ⚠️ 回 `None`（而不是回落到本页行数）是刻意的：回落会让「拉不全」看起来像「拉完了」，正是 B5/spec §10「拉不全必须响亮」要消灭的形态。"""
    envelope = getattr(client, "last_envelope", None) or {}
    count = envelope.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    return None


#: 读端点的成功码（本线这两个端点各自的实测判据，spec §9.2 第 2/3 项：大厅/我的订单都是 `code == 0`）。
#: ⚠️ ⛔ 不要推广到别的端点：同一家产品里写端点成功码相反（`code > 0`，spec §2.3/§11-9），而别的读端点实测还有 `1`/`200` 两种成功码（SDK `_endpoint_success_rule` 的注释）——同名参数/同族端点的模板都不共用，成功码同理（E-0007/E-0087）。
_READ_SUCCESS_CODE = 0


def _require_read_success(client, path: str) -> None:
    """读端点的成功码判据：壳里的 `code` 必须恰好是 `0`（spec §9.2/计划 Task 3 Step 3）。⚠️ 这是本线这两个端点（大厅/我的订单）的实测判据 ⛔ 不是「读端点一律 0」的通用规则（别的读端点实测有 `1`/`200`；写端点相反是 `> 0`）——见 `_READ_SUCCESS_CODE` 的注释；⛔ 不靠 SDK 的宽松解包：SDK 的 `lenient` 规则把 `code > 0` 一律判成功 ⇒ 失败信封 `{"code":1,"msg":"没有权限","data":[]}`（非空 `msg`+空 `data`+`code != 0`）会被放行、第 2/3 项就误绿；而这类壳不抛异常，只有显式判成功码才拦得住（E-0087 的读侧同族 ⛔ 调用不抛 ≠ 成功）；
    ⚠️ 判据打在 `client.last_envelope`（SDK 的真实出口，`Transport` 每次响应都写它）上：壳缺失/`code` 不是 `0`（含 `bool`）⇒ 响亮抛 `ApiError`（fail-closed ⛔ 不当成「能读」）。"""
    envelope = getattr(client, "last_envelope", None)
    code = envelope.get("code") if isinstance(envelope, dict) else None
    if isinstance(code, bool) or code != _READ_SUCCESS_CODE:
        raise ApiError(
            f"{path} 的业务壳 code={code!r}（读端点成功码必须是 {_READ_SUCCESS_CODE}）："
            "判不了「能读」",
            code=code if not isinstance(code, bool) else None,
        )


def _fetch_page(
    client, path: str, *, page: int, page_param: str = "page", query: dict | None = None
) -> tuple[tuple[dict, ...], int | None]:
    """取一页并回 `(行, count 或 None)`。⚠️ 两个取数口共用同一个参数模板形状（只带页码），成功码判据也共用：读端点=`code == 0`（`_require_read_success`）⛔ 不靠 SDK 宽松规则；
    ⚠️ `page_param` 由 `pull_all_pages` 透传（默认 `"page"`）：上游两个端点的页码参数名目前都是 `page`，但参数名不是契约（E-0007 同族）⇒ 留一格可配 ⛔ 不散落字面量；
    ⚠️ `query`（窗口拉取加法）是额外的收窄参数（`stime`/`etime`/`orderSta`/`key1`），与页码合并后一并送出；`None` ⇒ 逐字保持原先的「只带页码」形状（大厅那一支的调用点因此一个字节都不变）。"""
    params = dict(query or {})
    params[page_param] = page
    payload = client.call("GET", path, query=params)
    _require_read_success(client, path)
    return _as_rows(payload, path), _envelope_count(client)


def my_orders_window(*, now: datetime, hours: int) -> dict[str, str]:
    """「我的订单」一轮的取数窗：`[now - hours, now]`，格式 `"YYYY-MM-DD HH:MM:SS"`。⚠️ 格式照租户级 `jidanpu.order_list_window`（同一条实测口径：`stime`/`etime` 生效）；⚠️ 按调用当刻算：`now` 由调用方给（调度那一轮的 `now`）⛔ 不写模块级常量——import 时算一次会让常驻同步进程「活多久就错多久」（`jidanpu._today` 的注释记过同一个坑）；
    ⚠️ `now` 是 aware（`timezone.now()` 给的是 UTC）时先转业务时区：上游认的是它自己的墙上时间（`TIME_ZONE = "Asia/Shanghai"`），拿 UTC 直接格式化会让窗尾早 8 小时 ⇒ 最近 8 小时的新单一条都收不到；naive 的 `now` 原样用（调用方自己负责时区）。"""
    end = timezone.localtime(now) if timezone.is_aware(now) else now
    start = end - timedelta(hours=hours)
    stamp = "%Y-%m-%d %H:%M:%S"
    return {"stime": start.strftime(stamp), "etime": end.strftime(stamp)}


def fetch_my_orders(
    client, *, window_hours=None, order_sta=None, key1=None, now=None
) -> tuple[dict, ...]:
    """「我的订单」一轮取数（自己按 `page` 翻；spec §2.1 每页固定 20 行）。⚠️ ⛔ 不用 SDK 的 `iter_all()`（spec B5）：它拿默认 `page_size=100` 判「不满一页即尾页」⇒ 第一页（20 行）就被当成尾页、静默少拿。⚠️ 停止条件：累计行数 ≥ 壳里的 `count`（`count` 拿到就用它 ⛔ 不回落到本页行数——回落会让「拉不全」看起来像「拉完了」）；`count` 拿到却先碰到空页 ⇒ 上游自报的总数没交齐 ⇒ 响亮报「拉不全」⛔ 不当「拉完了」（spec §10 硬口径）；`count` 拿不到时只靠空页收尾；翻到 `MY_ORDERS_MAX_PAGES` 还没结束 ⇒ 同样响亮报「拉不全」⛔ 不静默交半份。
    ⭐ 收窄参数（窗口拉取；默认全 `None` ⇒ 参数形状与原先逐字相同）：`window_hours`=带上 `my_orders_window(now, hours)` 算出的 `stime`/`etime`；`order_sta`=带上 `orderSta` 筛选值（⚠️ 值是中文标签，真机实测 8 档生效；⛔ 「全部」必须传空串——传中文「全部」⇒ 0 行（真机实测）；本函数不传 `orderSta` 就是「全部」 ⛔ 别用 `order_sta="全部"` 表达它）；
    `key1`=单查键（订单号/角色名/手机号；⚠️ 命中语义是「`orderId` 精确相等 OR `orderIdView` 子串」⇒ 调用方必须逐字回校验（见 `_my_order_row_by_key1`）⛔ 不拿 `count == 1` 当判据）；`now`=算窗口用的时刻（默认 `timezone.now()`=调用当刻；注入只为测试/复用同一轮的时刻）。"""
    query: dict = {}
    if window_hours is not None:
        query.update(
            my_orders_window(
                now=now if now is not None else timezone.now(), hours=window_hours,
            )
        )
    if order_sta is not None:
        query["orderSta"] = order_sta
    if key1 is not None:
        query["key1"] = key1
    rows: list[dict] = []
    page = 1
    while page <= MY_ORDERS_MAX_PAGES:
        page_rows, count = _fetch_page(client, MY_ORDERS_PATH, page=page, query=query)
        rows.extend(page_rows)
        if count is not None and len(rows) >= count:
            return tuple(rows)
        if not page_rows:
            if count is not None:
                raise ApiError(
                    f"{MY_ORDERS_PATH} 拉不全：壳里 count={count}，实取 {len(rows)} 行时已到空页",
                    code=None,
                )
            return tuple(rows)
        page += 1
    raise ApiError(
        f"{MY_ORDERS_PATH} 翻了 {MY_ORDERS_MAX_PAGES} 页仍未结束（已取 {len(rows)} 行）："
        "确认它是否真的分页，或调大 MY_ORDERS_MAX_PAGES",
        code=None,
    )


def _my_order_row_by_key1(client, *, remote_id: str, order_sta: str | None = None) -> dict | None:
    """按 `key1=<上家单号>` 单查那一行，并逐字回校验 `orderId`（计划 §1-④/§2-5）。⚠️ `key1` 的实测语义是「`orderId` 精确相等 OR `orderIdView` 子串（LIKE `%key%`）」⇒ 命中集里可能有别的单 ⇒ ⛔ 不拿 `count == 1` 当判据（`jidanpu.fetch_order_by_key1` 的同一口径），必须是 `str(row["orderId"]) == remote_id`；
    ⚠️ 读的同一份 `fetch_my_orders`（⛔ 不另写取数口）：只带 `key1`、不带时间窗——真机实测那一发就是纯 `key1`（计划 §0），故单查不受 24 小时窗限制；⚠️ 回 `None`=「这一发读不到这一行」 ⛔ 不判「远端没有这张单」（spec §5.3 末行/E-0004 同族）；
    ⭐ `order_sta`=退单那一档的兜底问法（受控实验修）：默认视图看不见已退单的单（真机实测：`key1` 对一张已退单的单回 0 行），而 `key1 + orderSta=已退单` 能命中（同一张单实测回 1 行）；⚠️ ⛔ 这一发绝不能带时间窗：真机实测「退单视图+任意 `stime`/`etime`（1 小时…30 天全试过）」恒为 0 行 ⇒ 带了窗就等于没问。"""
    for row in fetch_my_orders(client, key1=remote_id, order_sta=order_sta):
        if str(row.get("orderId", "") or "") == str(remote_id):
            return row
    return None


# ── ⭐ 上传页隐藏域（`sid` / `gameAccId` / `taskId` 的唯一出处）────────────────────


class _HiddenFieldParser(HTMLParser):
    """把页面里的隐藏输入收成一个 dict（`type="hidden"` 的 `<input>`；同名取第一个）。⚠️ 用标准库 `html.parser`（⛔ 不引第三方依赖）：它按属性名给值，天然处理属性顺序/单双引号/自闭合，比正则稳；只认 `type="hidden"`（可见输入不进这个字典，spec 说的是「隐藏域」）；
    ⚠️ 同名重复时不覆盖：取第一个（重复的隐藏域本身就是上游的异常形态，留第一个比「最后一个赢」更可复现）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs) -> None:
        if tag.lower() != "input":
            return
        attributes = {name.lower(): (value if value is not None else "") for name, value in attrs}
        if attributes.get("type", "").strip().lower() != "hidden":
            return
        name = attributes.get("name", "").strip()
        if not name or name in self.fields:
            return
        self.fields[name] = attributes.get("value", "")


def _hidden_fields(html: str) -> dict[str, str]:
    parser = _HiddenFieldParser()
    parser.feed(html)
    parser.close()
    return parser.fields


def fetch_upload_page_fields(http, *, remote_order_id: str) -> dict[str, str]:
    """抓上传页 `GET /Order/UploadPic?orderId=…`，把隐藏域读成 dict（spec §2.4/T7）。⚠️ 判据只看状态码（照 `jidanpu.probe_pages` 的口径 ⛔ 不靠业务壳——页面回的是 HTML），但各档要分对 ⛔ 别合成一档：`200` ⇒ 解析隐藏域（三个必备键的缺项/空值由调用方按「没取到」判，见 `_missing_required_fields`）；`302` 且 `Location` 指向无权限页 ⇒ `ApiError`（已知业务形态：这个账号打不开上传页 ⇒ 第 4 项的人话，`detect_binding` 认它）；基础设施类失败一律响亮上行 ⛔ 不伪装成「上家没给店铺标识」——连接失败 ⇒ `TransportError`；`5xx` ⇒ `ServerError`；`302` 到登录页等其它跳转 ⇒ `SessionExpiredError`（会话失效，与「无权限」不是一档——说成没权限会让人去改一个没坏的权限设置）；其余状态码（如 `404`）⇒ 裸 `JidanpuError`：判不了是权限还是上游改版 ⇒ fail-closed 响亮上行（⛔ 不塞进第 4 项那句人话——那会把我们的 bug 说成上家的问题；⛔ 也不用 `ApiError`——那正是第 4 项的通道，会被吞掉）；
    ⚠️ `follow_redirects=False` 必须显式给：跟在 302 后面就看不到无权限页，会把「没权限」误判成「拿到页面了」；
    ⚠️ 注入的 `http` 由调用方持有（`jidanpu.open_client_with_http` 产出的那个已设 `base_url`）；路径一律以 `/` 开头。"""
    try:
        response = http.get(
            UPLOAD_PAGE_PATH,
            params={"orderId": remote_order_id},
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise TransportError(f"请求上传页 {UPLOAD_PAGE_PATH} 时连接失败（{exc}）") from exc

    status = response.status_code
    location = response.headers.get("Location") or ""
    if status == 200:
        return _hidden_fields(response.text)
    if status == 302 and jidanpu.NO_PERMISSION_PATH in location:
        raise ApiError(
            f"上传页 {UPLOAD_PAGE_PATH} 得到 HTTP 302（302 跳到无权限页）：拿不到店铺标识",
            code=None,
        )
    if status >= 500:
        raise ServerError(
            f"上传页 {UPLOAD_PAGE_PATH} 得到 HTTP {status}：上游服务端错误",
            status_code=status,
        )
    if status == 302:
        raise SessionExpiredError(
            f"上传页 {UPLOAD_PAGE_PATH} 得到 HTTP 302（跳到 {location or '登录页'}）：会话失效"
        )
    detail = f"（跳到 {location}）" if location else ""
    raise JidanpuError(
        f"上传页 {UPLOAD_PAGE_PATH} 得到 HTTP {status}{detail}："
        "既不是正常页面、也不是无权限页 ⇒ 判不了是权限还是上游改版"
    )


# ── 四项只读检测 ─────────────────────────────────────────────────────────────


@contextmanager
def _connection(binding, client, http):
    """产出 `(sdk_client, http)`：两个注入口都给了就用注入的，都没给就自己建连。⚠️ 注入态只为可测性（⛔ 不是业务参数、⛔ 不许来自请求）：凭据只从 `binding` 那一行取；真实形态里这两样由同一次 `jidanpu.open_client_with_http(binding)` 产出；
    ⚠️ 只给一个 ⇒ 那是我们的编程错误，响亮抛 `ApiError`（⛔ 不拿半个注入去猜另一半）。"""
    if client is not None and http is not None:
        yield client, http
        return
    if client is None and http is None:
        with jidanpu.open_client_with_http(binding) as (sdk, http_client):
            yield sdk, http_client
        return
    raise ApiError("检测的注入态必须同时给 client 与 http（两样由同一次建连产出）", code=None)


def _first_order_id(rows: tuple[dict, ...]) -> str:
    """第一行的 `orderId`（没有就回空串）。⚠️ 匹配键一律用 `orderId`，⛔ **绝不用** `orderIdView`。"""
    for row in rows:
        value = row.get("orderId")
        if value:
            return str(value)
    return ""


def _display_source_no(payload, *, fallback: str) -> str:
    """上家「展示/来源单号」（`orderIdView`）优先，空则回落平台内部号。⭐ 业务方口径（计划 `docs/plans/2026-10-20-upstream-source-order-no-display.md`）：来源单号列=上家页面上展示的那个号；鸡蛋铺族每张单两个号：`orderId`=平台内部号、`orderIdView`=展示号；我方往鸡蛋铺录单时自己的来源单号也正是落在对方 `orderIdView` 上展示（同源实测，T19）；早先取错层存了 `orderId`——格式与我方内置单号同形，业务方看起来就是「来源单号没同步过来」；
    ⛔ 只做展示值，绝不做匹配键：`orderIdView` 实测会重号（B4），链路/镜像的 `upstream_order_id` 仍一律 `orderId`（`_first_order_id` 那条 ⛔ 照旧、一个字不动）。"""
    value = str(((payload or {}).get("orderIdView")) or "").strip()
    return value or fallback


def detect_binding(binding, *, client=None, http=None) -> DetectionReport:
    """四项只读预检（spec §9.2；⛔ 不打任何写端点、⛔ 不落库）。⚠️ `client`/`http` 是可测性注入口（两个要么都给、要么都不给）：都不给就自己 `jidanpu.open_client_with_http(binding)` 建连；登录失败 ⇒ 立刻返回、不再打上游（短路理由见模块 docstring）；第 4 项探的上传页要用一个 `orderId`：取「我的订单」第一条，回落大厅第 1 页第一条，再没有才用空串（`sid` 是店铺级标识、与具体单无关）——⚠️ 回落未真机验证，见模块 docstring；
    ⚠️ ②③ 是单页预检（各打第 1 页）⛔ 不做整轮全量拉取：spec §9.2 的判据就是「这个端点 `code == 0`」（我们自己判，见 `_require_read_success` ⛔ 不靠 SDK 宽松解包）；全量翻页拉不完是同步那一步的响亮失败（`fetch_my_orders`）⛔ 别把它伪装成「这个账号没权限」；
    ⚠️ 第 4 项只在「页面读到、但三个值缺/空」与「302 到无权限页」两档回人话；上传页的连接失败/5xx/302 到登录页/未知状态码都是检测本身失败，原样上行；`ApiError` 以外的失败（网络/5xx/限流/会话失效）原样上行 ⛔ 不在这里翻译成业务码或「没权限」。"""
    problems: list[str] = []

    with _connection(binding, client, http) as (sdk, http_client):
        try:
            # 显式登录一次：后三项共用同一个 cookie jar；不先登录的话每个请求都会
            # 「请先登录」/ 302 到登录页（⇒ 检测本身失败，⛔ 会被误报成没权限）。
            sdk.ensure_login()
        except AuthError:
            return DetectionReport(ok=False, problems=(LOGIN_FAILED,))

        hall_rows: tuple[dict, ...] = ()
        try:
            hall_rows, _count = _fetch_page(sdk, HALL_PATH, page=1)
        except ApiError:
            problems.append(HALL_UNREADABLE)

        my_rows: tuple[dict, ...] = ()
        try:
            my_rows, _count = _fetch_page(sdk, MY_ORDERS_PATH, page=1)
        except ApiError:
            problems.append(MY_ORDERS_UNREADABLE)

        # ② 与 ③ 各自独立：一条坏不影响另一条照跑（用户一次就知道要开哪些权限）。
        probe_order_id = _first_order_id(my_rows) or _first_order_id(hall_rows)
        try:
            fields = fetch_upload_page_fields(http_client, remote_order_id=probe_order_id)
        except ApiError:
            # ⚠️ 这里只认「上传页 302 到无权限页」这一档（`fetch_upload_page_fields` 里唯一的
            # ApiError）：连接失败 / 5xx / 302 到登录页 / 未知状态码都是**检测本身失败**，
            # 各自抛非 ApiError 的异常 ⇒ 原样上行，⛔ 不伪装成「上家没给店铺标识」。
            problems.append(UPLOAD_FIELDS_UNREADABLE)
        else:
            missing = _missing_required_fields(fields)
            if missing:
                problems.append(_upload_fields_problem(missing))

    return DetectionReport(ok=not problems, problems=tuple(problems))


# ══════════════════════════════════════════════════════════════════════════════
# 大厅镜像（自己翻页 + 地址级去重 + upsert + 三路归并）
# ══════════════════════════════════════════════════════════════════════════════

#: 大厅镜像行的状态常量别名（读起来比 `JidanpuUpstreamHallOrder.Status` 短）。
_HALL_LISTED = JidanpuUpstreamHallOrder.Status.LISTED
_HALL_CLAIMING = JidanpuUpstreamHallOrder.Status.CLAIMING
_HALL_PROMOTED = JidanpuUpstreamHallOrder.Status.PROMOTED
_HALL_GONE = JidanpuUpstreamHallOrder.Status.GONE
_HALL_SKIPPED = JidanpuUpstreamHallOrder.Status.SKIPPED

#: `gone` 重新见到即自动复活；`promoted` 仍只记台账（复活会造成重复接手）。别再合并出一个「终态集合」常量：两支语义不同。
#: 复活的台账 kind/reason 常量（`KIND_RESCUED`/`RESCUE_REASON`）唯一出处是 `jidanpu_common`（与 rescue 命令共用，E-0011 家族：搜「主词」只有一个出处）。

#: 非 owner 的那一轮的人话原因（地址级去重 A2：同地址只有最早那份启用绑定拉大厅）。
NOT_OWNER_REASON = "本地址另有更早的启用绑定负责拉大厅，这一份本轮不打上游"

#: 拉不全（`count` 到手、累计没到）的人话原因。
PULL_INCOMPLETE_REASON = "拉取不完整：累计取回 {got} 行，未达上游自报的总数 {count}"

#: 翻页撞上限（上游 `count` 骗人 / 壳里没有 `count` 时兜底）的人话原因。
PAGE_CAP_REASON = "翻页达到上限 {max_pages} 页仍未拉全，未判「消失」"

#: ⭐ （业务方口径；加固后不依赖页大小）：只要累计到了上家自报的 `count` 就停（=我们是因为自己的停止条件停的，不是上游用空页告诉我们到头了）⇒ 再探一页确认；探到行=`count` 可能偏小、后面还有 ⇒ 这一轮按「不完整」处理 ⛔ 不判任何行「消失」；
#: ⚠️ 它不能复用 `PULL_INCOMPLETE_REASON`：那句说的是「未达上家自报的总数」，而本档恰恰达到了那个数 ⇒ 复用会让台账说假话（这一格要的是「我们够到了它、但怀疑它本身偏小」）。
SUSPECT_SHORT_COUNT_REASON = (
    "上家自报的总数 {count} 可能有误：累计到它之后再探一页仍有行"
    "⇒ 本轮按「拉取不完整」处理，不判任何行「消失」（下一轮再核）"
)

#: ⭐ D-0266（E-0266）：**逐游戏拉取**未判全 / 查询失败时的人话原因。
#: ⚠️ 它进 `HallOutcome.reason` ⇒ 调用方必须写进绑定 `last_error`（`complete=False` 的
#: 响亮口径）；那一轮 `_mark_gone` 不跑——该游戏段在挂的镜像行不被误标 gone（H2）。
HALL_PER_GAME_INCOMPLETE_REASON = (
    "逐游戏拉取未判全（游戏「{name}」：{detail}）⇒ 本轮按「拉取不完整」处理，"
    "不判任何行「消失」（下一轮再核）"
)

#: ⭐ D-0321（业务方拍板「批，开零行游戏隔离」）——零行游戏跳过态的两个参数。背景（生产实锤）：某绑定（180 秒/轮）里「游戏伊莫」逐游戏查询每轮 ServerError ⇒ 按 D-0266 的 fail-safe 整轮判「拉取不完整」⇒ 下架检测停摆（停滞卡堆积 220+），且拉取按游戏顺序走 ⇒ 伊莫后面的游戏连新卡都进不来；
#: 修法（只豁免零行游戏）：连败满 `ZERO_ROW_GAME_SKIP_ROUNDS` 轮（约 9 分钟）⇒ 该绑定对该游戏进「跳过态」——不再逐轮查询（改为每小时探一次当发现通道），其失败也不再进本轮完整性判定 ⇒ 其余游戏的完整性判定与消失判定照常恢复；
#: ⚠️ 已有 listed 行的游戏一字不豁免（那些卡还在大厅里，宁停勿误判）。
ZERO_ROW_GAME_SKIP_ROUNDS = 3
ZERO_ROW_GAME_PROBE_EVERY = timedelta(hours=1)

#: 四级匹配都不中 ⇒ 该行**不进大厅**、留一条**人话**台账（spec §16.1 第 4 级 / §13.1-R7）。
#: ⚠️ 业务方口径：**认错游戏 = 打手看到不该他看的单，比漏一张更糟** ⇒ 宁可漏也不猜。
UNMATCHED_GAME_REASON = (
    "上家的游戏「{name}」在我方对不上（四级匹配都不中）：这一行不进大厅，请人工加别名或建同名游戏"
)

#: 子串那一级**命中多只**时的台账 kind（取名字最长的那只，⛔ 不静默取第一条）。
GAME_AMBIGUOUS_KIND = "upstream.hall_game_ambiguous"

#: 台账 `target_ref`（191 字符）与 `reason` 里游戏名的截断上限（`reason` 是 `CharField(255)`）。
_TARGET_REF_MAX = 191
GAME_NAME_IN_REASON_MAX = 60

#: 镜像行的投影字段上限（列宽）——上游给超长值时截断，只影响展示：原样那一行仍完整留在 `upstream_payload`（spec §4.3）；
#: `project_text` 跟 `JidanpuUpstreamHallOrder.project_text` 的列宽走（镜像列有意不动；`Order.project_text` 已放宽到 900 是 D-0218 的事，两回事——抬到 900 会让 301~900 字的上游行以 `value too long` 炸掉整轮同步，已退回）。
_MIRROR_FIELD_MAX = {
    "remote_game_area": 64,
    "remote_game_server": 64,
    "project_text": 300,
    "remote_remark": 255,
    "remote_role_name": 128,
}


# ── 台账写口 ─────────────────────────────────────────────────────────────────


def _record(*, direction: str, kind: str, status: str, target_type: str, target_ref: str,
            reason: str = "", payload: dict | None = None) -> JidanpuSyncEvent:
    """本线的台账写口（`JidanpuSyncEvent` 一张表兼 outbox/台账，spec §4.4）。⚠️ `payload` 里不许放凭据（spec §8.3/GC 3）；
    ⛔ 不 import `jidanpu_common.write_event`：那是另一条线的写口，本线自己一处落点更清楚（形状照它）。"""
    return JidanpuSyncEvent.objects.create(
        direction=direction, kind=kind, status=status,
        target_type=target_type, target_ref=target_ref, reason=reason, payload=payload,
    )


def _note_once_ref(*, target_type: str, target_ref: str, kind: str, reason: str,
                   status: str = "done", direction: str = "in",
                   payload: dict | None = None) -> bool:
    """台账幂等写口的唯一实现：键=`(kind, target_type, target_ref, reason)` 四元组。回 `True`=这次写了；`False`=早有同键的一行（本轮什么都没写）。⚠️ 拆出它只为让没有 order/mirror 可指认的那一档（缺 `orderId` 的上游行）也能走同一道幂等判据；语义与 `note_once` 逐字相同；⚠️ 键里刻意没有 `binding` 字段（实测后的裁定）：判别项由调用方按自己那条事实的粒度组合进 `target_ref`——`_note_record_skipped` 前置 `binding.pk`（同一个 `orderId` 会被派给同地址的多份绑定，那是每份各自的一件事 ⛔ 少了它第二份会被静默吞掉），而 `sync_hall` 缺 `orderId` 那一档用地址（只有 owner 拉大厅 ⇒ 同地址只一份）；
    ⚠️ 但归属不一致那一处（`_consume_hall_claim` 的 `KIND_HALL_OWNER_MISMATCH`）刻意不把事件绑定放进键，三条依据：①那条 `reason` 是身份无关的人话（既不点名事件绑定、也不点名当时的 owner）⇒ 第二行只会是逐字重复的一句，把它纳进键就是把同一句原因放大成 N 行（本线已定的去重口径）；⚠️ 这句人话会在事件绑定那位客服已停用时多带一段点名后缀（`_owner_mismatch_reason`/R24）——后缀仍是不带任何绑定/客服标识的事实描述 ⇒ 同一停用状态下仍是同一句、仍只记一行（换了停用状态才换 `reason`、才新记一条）；②它修不了「轮换到第三份绑定」那一幕：那条路上真正在重复的是同一条事件（`binding` 是排事件那一步写死的常量、重试不改）⇒ 键加不加它，第二次照样被吞；③一条事件重试期间 owner 从 B 轮换到 C，事实与处置完全相同（都是「请人工核对这个地址下的绑定开关」）⇒ 记第二行不带来新信息（`payload.upstream_binding_id` 也已指向事件那一步的绑定）；
    ⇒ 这是刻意的形态：这一格的粒度=`(地址, 上家单号)` ⛔ 别按「每个绑定一行」去改它。"""
    if JidanpuSyncEvent.objects.filter(
        kind=kind, target_type=target_type, target_ref=target_ref, reason=reason
    ).exists():
        return False
    _record(direction=direction, kind=kind, status=status, target_type=target_type,
            target_ref=target_ref, reason=reason, payload=payload)
    return True


def note_once(*, order=None, mirror=None, kind: str, reason: str, status: str = "done",
              direction: str = "in", payload: dict | None = None) -> bool:
    """「同一条目+同一 kind+同一句原因」只记一条台账，防每轮灌一条不变的事实。回 `True`=这次写了；`False`=早有同键的一行。⚠️ 幂等键=`(kind, target_type, target_ref, reason)` 四元组：`order` ⇒ 我方订单号；`mirror` ⇒ `(上家地址, 上家单号)`（镜像表的唯一键，spec §4.3）——两者都基于租户内唯一且跨地址不撞的值；⛔ 断言留痕要看数据库行（`AGENTS.md` §5-G/E-0009）；⚠️ 为什么键里不能不含 `reason`（评审实测的根因）：同一个 `target_ref` 上会先后得出不同的结论——例：一张单先因「上家游戏名对不上」记一条 `upstream.hall_skipped`，之后它成了终态又出现在大厅 ⇒ 该记「终态再现」；键里少了 `reason`，后一句会被前一句静默吞掉（实测台账行数 2→2、该记的一条没加），而台账是本线唯一的留痕出口 ⇒ 等于这件事没发生；仓内先例：`jidanpu_hall._note_once` 与 `jidanpu_member._note_order_once` 的同类 helper 键都含 `reason`（照它们对齐 ⛔ 不自创）；⚠️ 口径：同一句原因只记一条；换了原因就要新记一条（既不把不变的事实放大成 N 行，也不让不同的结论互相遮蔽）；
    ⚠️ `direction` 默认 `"in"`：本 helper 服务的是读侧/只登记的那些事实（不是 outbox 待办）——会写上游的两个动作另走 outbox（spec §4.4 的「只作台账是刻意的」）；
    ⚠️ 给 `mirror` 时 `payload` 自动带上 `upstream_binding_id`（spec §4.4：本线的台账必须能回答「这行归哪一份绑定」；⛔ 仍不许放凭据，GC 3）；两个都没给 ⇒ 响亮抛 `ValueError`：没有幂等键的去重就是「每轮灌一条」。"""
    if order is not None:
        target_type, target_ref = "order", order.order_no
    elif mirror is not None:
        target_type = "hall"
        target_ref = f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"[:_TARGET_REF_MAX]
    else:
        raise ValueError("note_once 必须给 order 或 mirror（否则没有幂等键，会每轮灌台账）")
    data = dict(payload or {})
    if mirror is not None and mirror.owner_binding_id is not None:
        data.setdefault("upstream_binding_id", mirror.owner_binding_id)
    return _note_once_ref(
        target_type=target_type, target_ref=target_ref, kind=kind, reason=reason,
        status=status, direction=direction, payload=data or None,
    )


# ── 地址级去重（A2）──────────────────────────────────────────────────────────


def enabled_bindings():
    """启用中的本线绑定（按 `id` 升序 ⇒ **最早建的那份在前**，A2 的候选序）。"""
    return JidanpuUpstreamBinding.objects.filter(enabled=True).order_by("id")


def dedupe_owner(address: str) -> JidanpuUpstreamBinding | None:
    """同一上家地址负责拉大厅的那一份 = 「启用中、`id` 最小」；一个都没有 ⇒ `None`。⚠️ 形状照 `jidanpu_member.py::dedupe_owner` 的同一条口径（A2 的地址级去重）：同地址重叠时归最早的那一份 ⛔ 不硬塞、⛔ 不按「谁先跑」；
    ⚠️ 只算启用中的：停用的绑定不会拉任何东西，让它占着归属会把大厅变成没人拉；
    ⚠️ ⛔ 指派单那一路不走这个去重（spec §11-5/§5.2）：那一份的口径是「上家把这个账号指给了谁」，与「谁负责拉大厅」是两件事（这里不套这个判据）。"""
    return (
        JidanpuUpstreamBinding.objects.filter(jidanpu_base_url=address, enabled=True)
        .order_by("id")
        .first()
    )


# ── 翻页：自己按 `page` 拉全（⛔ 不用 SDK `iter_all`）────────────────────────


def _round_is_complete(
    count: int | None, rows, hit_page_cap: bool, more_after_count: bool
) -> bool:
    """「这一轮拉全了吗」的唯一判据（`pull_all_pages` 与 `sync_hall` 共用一处 ⛔ 不各写一遍）。⚠️ `count` 是 `int | None`，缺了就回 `None`——⛔ 绝不「编一个数」：壳里没有可用的 `count` 时不许回落到本页行数（那会让「拉全 = 累计行数 ≥ count」在第 1 页就成立 ⇒ 20 行当全量，正是设计要消灭的静默少拿）；`count is None`=判不了 ⇒ 必须按「不完整」处理（⛔ 别比大小：`0 >= None` 会 `TypeError`——那是故意的）；⚠️ 撞翻页上限（`hit_page_cap`）永远不算拉全（那是兜底，不是尾页）；
    ⚠️ `more_after_count`（业务方口径；探页判据）：我们是因为累计行数 ≥ `count` 才停的（=用自己的停止条件停的 ⛔ 不是上游用空页告诉我们到头了）⇒ 再探一页确认；探到还有行 ⇒ 上家自报的 `count` 偏小 ⇒ 这一轮不算拉全（⛔ 不判任何行「消失」）——保守方向：可疑就不判消失；⚠️ 为什么不用「末页是否满页」判（加固轮删掉的那个写法）：那要知道上游每页多少行，而本线翻页只带 `page`、不带 `limit` ⇒ 只能写死一个常数（实测 20）⇒ 上游一旦调小页大小这个档会静默退化（比不出「满页」⇒ 又误标消失）；探页判据不依赖页大小；
    ⚠️ 代价（已登记）：累计恰好到 `count` 的正常轮次会多打一次上游（大厅 300 秒一轮，可接受）——比「误标消失」便宜。⛔ 别把它读成「拉全」的替代判据。"""
    return (
        count is not None
        and len(rows) >= count
        and not hit_page_cap
        and not more_after_count
    )


def _pull_all_pages_detail(
    client, *, path: str, page_param: str, query: dict | None = None
) -> tuple[tuple[dict, ...], int | None, bool, bool]:
    """翻页的细节版（给 `sync_hall` 拼人话原因用）：回 `(行, count, 是否撞上限, 到数后是否还有行)`。⚠️ `query`（D-0266 加法）是额外的收窄参数（逐游戏路带 `{"gameName": <完整名>}`），与页码合并后一并送出；`None`（默认）⇒ 只带页码的形状（⭐ D-0366 起同步轮只走带 `query` 的逐游戏段查；无游戏名调用只剩 `pull_all_pages` 的既有用例与绑定检测的只读探针）；
    停止条件（按 spec §10/B5）：`count` 到手 ⇒ 累计行数 ≥ `count` 就停（⛔ 不用「本页不满一页」判尾页——那正是 B5 的坑）；停之前再探一页（加固）：探到行 ⇒ 置 `more_after_count`（上家自报的 `count` 可能偏小 ⇒ 这一轮按不完整处理，由 `complete` 判）；探到空页 ⇒ 确认拉全；⚠️ 探页只用来取信号，它的行不进结果集（下一轮会正常拉到它们）⛔ 不一路翻到底；
    空页 ⇒ 停（`count` 没到就是「不完整」，由 `complete` 判 ⛔ 不静默当拉完）；`count` 拿不到 ⇒ 继续翻到空页，但结果永远不算拉全（`None`=判不了，本函数的契约）；翻到 `HALL_MAX_PAGES` ⇒ 撞上限（响亮 ⛔ 不静默交半份）；⚠️ 若恰好在最后一页因 `count` 而停 ⇒ 没法再探（⛔ 不许越过上限）⇒ 保守当「还有行」（宁可判不完整，也⛔ 不误标消失）。"""
    rows: list[dict] = []
    count: int | None = None
    hit_page_cap = False
    more_after_count = False
    page = 1
    while True:
        page_rows, page_count = _fetch_page(
            client, path, page=page, page_param=page_param, query=query
        )
        # ⚠️ `count` 取**第一个非 `None` 的页**给的值（哪一页给了就用它，`None` = 没给）。
        if count is None:
            count = page_count
        rows.extend(page_rows)
        if count is not None and len(rows) >= count:
            #: ⭐ 加固：到数就停，但「到数」是**我们自己的停止条件**，不是上游说「到头了」
            #: ⇒ **探下一页**取信号（⛔ 不依赖「上游每页固定 20 行」那个会过期的常数）。
            if page >= HALL_MAX_PAGES:
                #: 已经在上限那一页、再探就越界 ⇒ 保守：当「还有行」处理（⛔ 不误标消失）。
                more_after_count = True
                break
            probe_rows, _probe_count = _fetch_page(
                client, path, page=page + 1, page_param=page_param, query=query
            )
            more_after_count = bool(probe_rows)
            break
        if not page_rows:
            break
        if page >= HALL_MAX_PAGES:
            hit_page_cap = True
            break
        page += 1
    return tuple(rows), count, hit_page_cap, more_after_count


def pull_all_pages(client, *, path: str, page_param: str = "page") -> tuple[tuple[dict, ...], bool]:
    """按 `page` 翻全一个列表端点，回 `(行, 是否拉全)`。⚠️ 「拉全」只在 `count is not None` 时成立（`count` 到手且累计行数 ≥ 它、且没撞翻页上限、且到数后探页确认真没有下一页——最后一条见 `_round_is_complete` 的探页加固口径）；`count is None`=判不了 ⇒ 必须按「不完整」处理（⛔ 别比大小：`0 >= None` 会 `TypeError`——那是故意的；判据收口在 `_round_is_complete`）；
    ⚠️ ⛔ 不用 SDK 的 `iter_all()`（spec B5/§13.1-R3）：它拿默认 `page_size=100` 判「不满一页即尾页」，而上游每页固定 20 行 ⇒ 第一页就被当成尾页、静默少拿。"""
    rows, count, hit_page_cap, more_after_count = _pull_all_pages_detail(
        client, path=path, page_param=page_param
    )
    complete = _round_is_complete(count, rows, hit_page_cap, more_after_count)
    return rows, complete


# ── ⭐ 游戏四级匹配（A15 / spec §16.1 的**唯一出处**）─────────────────────────


def _normalize_game_name(name: str) -> str:
    """归一化：**只用于比较**，⛔ 不写回任何列（spec §16.1 的口径）。口径 = 去所有空白 + 全角转半角 + `casefold()`（⛔ 不引第三方库）。"""
    text = "".join(ch for ch in (name or "") if not ch.isspace())
    converted: list[str] = []
    for ch in text:
        code = ord(ch)
        # ASCII 可见区的全角形态（！…～）逐一映射回半角；其余字符原样。
        converted.append(chr(code - 0xFEE0) if 0xFF01 <= code <= 0xFF5E else ch)
    return "".join(converted).casefold()


def _note_game_ambiguity(upstream_name: str, chosen: Game) -> None:
    """子串命中多只时留一条**人话**台账（⛔ 不静默取第一条；同名字只留一条）。"""
    key = _normalize_game_name(upstream_name)[:_TARGET_REF_MAX]
    if JidanpuSyncEvent.objects.filter(kind=GAME_AMBIGUOUS_KIND, target_ref=key).exists():
        return
    _record(
        direction="in", kind=GAME_AMBIGUOUS_KIND, status="done", target_type="hall",
        target_ref=key,
        reason=(
            f"上家游戏名「{upstream_name[:GAME_NAME_IN_REASON_MAX]}」在我方匹配到多只游戏，"
            f"已取名字最长的「{chosen.name[:GAME_NAME_IN_REASON_MAX]}」（请人工核对 / 加别名）"
        ),
    )


def match_game_by_upstream_name(name: str, *, candidates=None) -> Game | None:
    """上家 `gameName` → 我方 `Game` 的四级匹配（A15/spec §16.1 的唯一出处）。①精确同名（逐字相等）；②归一化相等（去空格/全半角/大小写，`_normalize_game_name`）；③子串（一方含另一方；例：上家「重返未来：1999」↔我方「重返未来」）；④都不中 ⇒ `None`：调用方（`sync_hall`）把该行记成 `skipped`+一条「请人工加别名」的台账 ⛔ 绝不硬塞进某个游戏——业务方口径：认错游戏=打手看到不该他看的单，比漏一张更糟；
    ⚠️ 只在非软删的游戏里找（软删=租户以为删掉了 ⇒ 挂上去等于让大厅出现一款他不要的游戏）；⚠️ 第③级命中多只 ⇒ 取名字最长的那只（更具体）+留一条台账；并列时取 `id` 最小者；
    ⚠️ 本函数是「上家名 → 我方游戏」的唯一判据 ⛔ 别在别处再写一套（两处判据必然漂移）；⛔ 不改 `jidanpu_hall.match_game_by_name`（那是 P2 那条线的同名匹配，只服务它自己）；⚠️ `candidates` 只为一轮里只查一次游戏表（`sync_hall` 预加载），语义不变；不传时自己查（测试与单点调用走这条路）。"""
    if not (name or "").strip():
        return None
    if candidates is None:
        candidates = list(Game.objects.filter(deleted_at__isnull=True))

    # ① 精确同名
    for game in candidates:
        if game.name == name:
            return game

    normalized = _normalize_game_name(name)
    # ② 归一化相等（并列时取 id 最小的那只，判据确定、可复现）
    normalized_hits = [g for g in candidates if _normalize_game_name(g.name) == normalized]
    if normalized_hits:
        return min(normalized_hits, key=lambda g: g.pk)

    # ③ 子串（用**归一化**后的串比，避免全半角 / 空格把子串判丢）
    substring_hits = [
        g for g in candidates
        if normalized and (
            normalized in _normalize_game_name(g.name)
            or _normalize_game_name(g.name) in normalized
        )
    ]
    if not substring_hits:
        return None  # ④ 都不中
    longest = max(substring_hits, key=lambda g: (len(g.name), -g.pk))
    if len(substring_hits) > 1:
        _note_game_ambiguity(name, longest)
    return longest


# ── 镜像行的投影 / upsert / 标 gone（spec §5.1）────────────────────────────


def _rec_fee_or_none(value) -> Decimal | None:
    """上家 `recFee` → `Decimal`（两位小数）；空/认不出 ⇒ `None`（=远端没给价）。⚠️ `None` 是合法结果，含义只有一个：上家那一列没给价（镜像行的 `remote_rec_fee` 列照实留 NULL）；⛔ 别把这里改成回 0：这一层是原样投影，客户价的 `0.00` 兜底（`total_price` 那两处 `if rec_fee is None`）与发单价的 0 口径（`upstream_hall_price` 的 `None ⇒ Decimal("0.00")`）各自在自己的边界上做——把 0 从这里灌进去会让「上家到底给没给价」这条跟钱无关的原始事实消失；
    ⭐⭐ 负数恢复直落（D-0306 业务方终裁）：`recFee < 0` 不再折成 `None`，按原值（两位小数）返回 ⇒ 直落 `total_price`/`hall_price`/绩效基数——这是「订单流负价放行」在鸡蛋铺拉取侧的落点（与 peer 订单事件路同一口径）；审计那条「负的接单价没有任何业务含义」的建议已被业务方推翻；⛔ 不要顺手把落库侧再挡一次：`total_price`/`hall_price` 的负值边界是合法的（D-0015），真正的上游烂数据由「对账/人工」处置 ⛔ 不靠这里静默改数；
    ⚠️ `0` 与正数照常（`Decimal("-0")` 归一化后等于 0，不算负数那一档）。"""
    if value is None or value == "":
        return None
    try:
        fee = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return fee


def upstream_hall_price(remote_rec_fee, binding, *, game_id=None, percent_at_sync=None) -> Decimal:
    """新单取游戏设置，卡片有快照时优先用快照。鸡蛋铺缺价按零元算。"""
    if remote_rec_fee is None:
        remote_rec_fee = Decimal("0.00")
    if percent_at_sync is not None:
        percent = percent_at_sync
    elif game_id is not None:
        percent = upstream_game_price_percent(binding, game_id)
    else:
        percent = getattr(binding, "hall_price_percent", None) or 100
    return (Decimal(remote_rec_fee) * percent / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def upstream_mirror_hall_price(row, *, binding=None) -> Decimal:
    return upstream_hall_price(
        row.remote_rec_fee, row.owner_binding if binding is None else binding,
        percent_at_sync=row.hall_price_percent_at_sync,
    )


def _remote_datetime(value) -> datetime | None:
    """上家 `createTime` → 带时区的 `datetime`；认不出 ⇒ `None`。
    ⚠️ ⛔ 不猜格式：只认 Django 认得的 ISO 形态；认不出就 `None`（原样那一行仍留在 `upstream_payload`）；它只影响展示，不参与任何判据（大厅排序用的是 `first_seen_at`）。"""
    if not value or not isinstance(value, str):
        return None
    parsed = parse_datetime(value.strip().replace(" ", "T"))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _text(value, *, limit: int) -> str:
    """上游文本 → 我方列（`str` + 按列宽截断；原样仍留在 `upstream_payload`）。"""
    return str(value if value is not None else "")[:limit]


def _mirror_fields(row: dict, *, game: Game | None, owner_binding) -> dict:
    """上家大厅行 → 镜像行的投影字段（照 P2 `project_row` 的口径，本线自建）。
    ⚠️ `owner_binding` 每次都写当前负责拉大厅的那一份（`sync_hall` 的调用者）：`JidanpuUpstreamHallOrder.owner_binding` 的语义就是「当前负责拉取的绑定」，地址级去重轮换到下一份之后它要跟着走（spec §8.4 第 3 行/A13 的提成归属按它算）。"""
    return {
        "owner_binding": owner_binding,
        "game": game,
        "remote_game_area": _text(row.get("gameArea"), limit=_MIRROR_FIELD_MAX["remote_game_area"]),
        "remote_game_server": _text(
            row.get("gameServer"), limit=_MIRROR_FIELD_MAX["remote_game_server"]
        ),
        "project_text": _text(row.get("project"), limit=_MIRROR_FIELD_MAX["project_text"]),
        "remote_remark": _text(row.get("orderRemark"), limit=_MIRROR_FIELD_MAX["remote_remark"]),
        "remote_role_name": _text(
            row.get("roleName"), limit=_MIRROR_FIELD_MAX["remote_role_name"]
        ),
        "remote_rec_fee": _rec_fee_or_none(row.get("recFee")),
        "remote_created_at": _remote_datetime(row.get("createTime")),
        "upstream_payload": row,
    }


def _unmatched_reason(game_name: str) -> str:
    return UNMATCHED_GAME_REASON.format(name=game_name[:GAME_NAME_IN_REASON_MAX])


def _touch(mirror, *, now: datetime, status: str, owner_binding) -> None:
    """**条件更新** `last_seen_at` + 归属绑定（条件带「读到的那个状态」⇒ 并发改走时不抢回去）。"""
    JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk, status=status).update(
        last_seen_at=now, owner_binding=owner_binding
    )


def _mark_gone(
    *, binding: JidanpuUpstreamBinding, client, seen: set[str], now: datetime,
) -> int:
    """本轮完整、且某 `listed` 行没出现 ⇒ 该游戏段一跳新鲜段查复核后标 `gone`+台账（⛔ 不删行，spec §5.1）。⚠️ 只在整轮拉全时调用（调用方判）——「没看到」在拉不全时不能当「消失了」。⭐ 候选判据（D-0366；业务方拍板「以后别用空游戏名查」）：候选=`listed` 且不在本轮段行 `seen` 里，且其游戏的段查询本轮成功（含 0 行成功——YF-01 的 success-empty 排除随本决策删除：竹屿整厅口恒回 1 张+三家上家 `key1` 复核对在厅活卡盲查（E-0314），旧口径下段空游戏的滞留单永标不掉，还把初音活卡 7 天误标 gone 5 次（churn 台账实锤）⇒「成功回 0 行」=有证据）；「本轮成功」的机械判据=该游戏不在 D-0321 跳过态表里（`_ZERO_ROW_GAME_SKIP`）：正常游戏整轮 complete ⇒ 它的段必然已判全（有证据）；跳过态游戏探测成功的那一轮会把状态条目删掉（有证据），探测失败/未到探测点 ⇒ 条目还在 ⇒ 本轮无证据不进候选（fail-closed 对「失败」档保留；注意与「成功回 0 行」区分——0 行成功=有证据）；`game` 为空/软删游戏同样不进候选（本轮没有它的段查=无证据；`listed` 行恒有 game，这一支是防御）；
    ⭐ 复核=该游戏段重新查一次（新鲜请求；同游戏多候选只复核一次）：复核行里仍不见该单 ⇒ 才落 `gone`；复核这一跳拿不到可信结论（`JidanpuError`，或复核自己不完整——count 缺失/撞上限/探页有行）⇒ 本轮跳过该游戏的全部候选（宁漏标勿误标：不落终态、不留台账，下一轮完整扫会再判一次）；⛔ D-0258 的 `jidanpu.fetch_order_by_key1` 复核删除（E-0314 实证：竹屿/初音/团团工作室三家对在厅活卡也查无——那一跳在这三家是盲查；分页窗口的根因留档 E-0257）；
    ⚠️ 落库是条件更新：候选读完到落库之间被并发改走（例如我方打手发起接单）⇒ 拿不到就不算、也不记「已消失」台账（那条台账会说假话）。"""
    gone = 0
    skip_pks = set(_ZERO_ROW_GAME_SKIP.get(_hall_round_key(binding), {}))
    candidates = list(
        JidanpuUpstreamHallOrder.objects.filter(
            upstream_base_url=binding.jidanpu_base_url, status=_HALL_LISTED,
        )
        .exclude(upstream_order_id__in=seen)
        # ⭐ D-0321 跳过态（/ 无 game）⇒ 本轮无证据，不进候选（fail-closed 保留）。
        .exclude(game_id__in=skip_pks)
        .filter(game__deleted_at__isnull=True)
        .select_related("game")
        .order_by("pk")
    )
    #: 同游戏多候选只复核一次：游戏 pk → 复核行集；`None` = 复核没答上来
    #: （⇒ 跳过该游戏的全部候选，宁漏标勿误标）。
    rechecked: dict[int, tuple[dict, ...] | None] = {}
    for mirror in candidates:
        if mirror.game_id not in rechecked:
            try:
                rows, count, hit_page_cap, more_after_count = _pull_all_pages_detail(
                    client, path=HALL_PATH, page_param="page",
                    query={"gameName": mirror.game.name},
                )
            except JidanpuError:
                rechecked[mirror.game_id] = None
            else:
                rechecked[mirror.game_id] = (
                    rows
                    if _round_is_complete(count, rows, hit_page_cap, more_after_count)
                    else None
                )
        if not binding.current_identity_rows().filter(enabled=True).exists():
            break
        recheck_rows = rechecked[mirror.game_id]
        if recheck_rows is None:
            # 复核查询失败 / 复核自己不完整 ⇒ 「仍不见」不可信 ⇒ 本轮跳过该候选
            # （不落终态、不留台账；下一轮完整扫会再判一次）。
            continue
        if mirror.upstream_order_id in {
            str(row.get("orderId", "") or "") for row in recheck_rows
        }:
            # 复核查得到 ⇒ 那张单还在（本轮「没看到」是分页窗口的假象）⇒ **不标**，按既有
            # 非终态路径走 `_touch`（条件更新刷新 `last_seen_at` + 归属绑定；下一轮正常拉到
            # 时照旧刷新投影）。
            _touch(mirror, now=now, status=_HALL_LISTED, owner_binding=binding)
            continue
        updated = JidanpuUpstreamHallOrder.objects.filter(
            pk=mirror.pk, status=_HALL_LISTED
        ).update(status=_HALL_GONE, gone_at=now)
        if not updated:
            continue
        gone += 1
        # ⭐ 离厅快照（D-0296）：条件更新带着 `status=_HALL_LISTED` ⇒ rowcount > 0 就是「原状态是 listed」的唯一判据，走到这里才写 `withdrawn` 灰卡（claiming → gone 的 `_claim_refused` 不写，见接单处注释）；
        #    ⚠️ 快照失败响亮留痕 ⛔ 不把一轮拉取打挂。
        try:
            from apps.business import hall_snapshot

            hall_snapshot.record_mirror_exit(
                mirror,
                exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.WITHDRAWN, at=now,
            )
        except Exception:  # noqa: BLE001 —— 辅助数据失败不影响消失判定（响亮留痕）
            logger.exception("镜像离厅快照写入失败（不影响消失判定）mirror=%s", mirror.pk)
        _record(
            direction="in", kind="upstream.hall_gone", status="done", target_type="hall",
            target_ref=f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"[:_TARGET_REF_MAX],
            reason=GONE_REASON, payload={"upstream_binding_id": binding.pk},
        )
        # ⭐ D-0325：卡离厅 ⇒ 它派生的隐藏单跟着退款，并把下家大厅里的
        #    副本撤掉（`upstream_hall_shadow.refund_for_card`；无隐藏单时它自己回 0）。
        #    「上游 gone + 下家已接」的竞态由消费侧 `_consume_withdraw` 的写前回读闸保护。
        from apps.business import upstream_hall_shadow

        upstream_hall_shadow.refund_for_card(mirror, now=now)
    return gone


@dataclass(frozen=True)
class HallOutcome:
    """一轮大厅镜像的汇总（`sync_hall` 的返回值；调度层拿它打 stdout/写 `last_error`）。`pulled`=本轮从大厅取回的行数（含被跳过的行，不是「新挂了几个」；⚠️ D-0366 起行来源=逐游戏段查 ⇒ 若上游把同一 `orderId` 在两个游戏段里都回，它会按段出现次数重复计入——库态无害：upsert 幂等；单卡单段的正常前提下不发生）；`listed`=本轮在我方大厅里的行数（新建+刷新+gone 复活（D-0300）三种来源都算）；`skipped`=本轮没进大厅的行数（缺 `orderId`/游戏四级都不中）；`gone`=本轮被判「已消失」的行数（只在 `complete` 时可能 > 0）；
    `complete`=本轮是否拉全（⚠️ `False` 时 `reason` 是人话原因，必须被调用方写进绑定 `last_error`/打到 stdout ⛔ 不许静默：拉不全就判「消失」会误撤在挂的单）；`reason`=不完整的原因（`complete=True` 且是 owner 时为空串）；`is_owner`=这份绑定是否本地址 owner（`False` ⇒ 本轮一次上游都没打，调用方不许拿它当失败 ⛔ 不写 `last_error`）。"""

    pulled: int = 0
    listed: int = 0
    skipped: int = 0
    gone: int = 0
    complete: bool = True
    reason: str = ""
    is_owner: bool = True


# ── ⭐ D-0321：零行游戏的跳过态（进程内，无迁移、无新表）──────────


@dataclass
class _ZeroRowGameSkip:
    """一个 `(绑定, 游戏)` 上的零行跳过态（D-0321）。`consecutive_failures`=逐游戏查询（`JidanpuError` 那一档）的连败轮数，成功一次即清零（状态条目整个删除）；`skip`=是否处于跳过态（连败满 `ZERO_ROW_GAME_SKIP_ROUNDS` 轮时置 `True`）；
    `last_probe_at`=上次探测（跳过态下每小时一次的发现通道）的时刻；探测失败也更新它（下一小时再探），成功则整个状态删除（退出跳过态）。"""

    consecutive_failures: int = 0
    skip: bool = False
    last_probe_at: datetime | None = None


#: 进程内：`(上家地址, 绑定 pk)` → `{游戏 pk: 跳过态}`（键形照 `_hall_round_key`——地址+绑定 pk ⛔ 不用 pk 单键：多租户下 pk 会撞）。⚠️ 已知代价（业务方批的）：进程重启 ⇒ 计数清零 ⇒ 连败 3 轮重新学出来（约 9 分钟）；⚠️ 单同步进程串行轮（现状）⇒ 不加锁，同一把键的形态；
#: ⭐ D-0366 起它同时是消失判定的「本轮有无证据」来源：还在表里的游戏 ⇒ 本轮没有它的段查证据 ⇒ 名下 `listed` 镜像不进 `_mark_gone` 的候选（见该函数 docstring）。
_ZERO_ROW_GAME_SKIP: dict[tuple[str, int], dict[int, _ZeroRowGameSkip]] = {}


def _binding_game_has_listed_rows(binding, game) -> bool:
    """D-0321 的「零行」判据：该绑定名下该游戏当前有没有 `listed` 镜像行。⚠️ 候选形状与 `_mark_gone` 同源（`upstream_base_url`+状态；不按 `owner_binding` 过滤——同地址只有 owner 拉大厅，两者等价）；`.exists()` 打在 `(地址, 游戏, listed)` 上，量级=该绑定该游戏的在挂数 ⛔ 不是全表扫；
    且只在失败已经发生时查一次（成功路径一次都不查）。"""
    return JidanpuUpstreamHallOrder.objects.filter(
        upstream_base_url=binding.jidanpu_base_url, game=game, status=_HALL_LISTED,
    ).exists()


def _exit_zero_row_skips_with_rows(binding, games) -> None:
    """跳过态期间该游戏长出第一张 `listed` 行（本轮逐游戏路带进来的）⇒ 立即退出跳过态（删状态+一行日志；下一轮起恢复每轮查询）。
    在 `sync_hall` 的行处理循环之后调用（那时本轮的 listed 行已经落库）；跳过态里的游戏通常零头（只有连败的零行游戏才会进来）⇒ 每轮最多几次 `.exists()`。"""
    states = _ZERO_ROW_GAME_SKIP.get(_hall_round_key(binding))
    if not states:
        return
    for game in games:
        state = states.get(game.pk)
        if state is None or not state.skip:
            continue
        if _binding_game_has_listed_rows(binding, game):
            del states[game.pk]
            logger.info(
                "零行游戏跳过态解除（该游戏出现了 listed 行，下一轮起恢复每轮查询）"
                " binding=%s game=%s", binding.pk, game.name,
            )


def _pull_per_game_rows(
    client, *, games, binding=None, now=None
) -> tuple[tuple[dict, ...], str]:
    """⭐ 逐游戏拉取——⭐ D-0366 起是全量路的本体：对我方游戏字典的每只游戏，带完整 `game.name` 分页拉一遍大厅，回 `(行, 未判全原因)`（二元组；原第三返回值 `success_empty` 随 YF-01 删除一并去掉——D-0366 口径下「成功回 0 行」=有证据，由 `_mark_gone` 的段查复核消费 ⛔ 不再在这里排除任何候选）。口径（GC 22 依旧）：⛔ 只带完整 `game.name`（上游只认完整名，简称/截断名会静默回 0 行）⛔ 不派生任何「更短的形式」，行与游戏的匹配仍全在本地（四级匹配，`sync_hall` 做）；翻页/探页/上限复用 `_pull_all_pages_detail`（`query=` 扩展 ⛔ 不写第二份翻页），只带 `page`、不带 `limit`（与既有路同一翻页口径）；任一游戏未判全/查询失败 ⇒ 原因进返回值，调用方把整轮判 `complete=False`（H2 fail-closed）：否则「部分游戏完整+某游戏查询失败」的轮里，该游戏在挂的镜像行不在 `seen` ⇒ 会成误标候选（该游戏本轮无证据，`_mark_gone` 的候选判据也不收它——两道闸）；查询失败收 `JidanpuError`（业务壳拒绝+传输层），detail 只留异常类名（隐私口径）；首个未判全的游戏之后停：本轮已经判不了「消失」，剩下的游戏下一轮再拉（⛔ 不为已注定不完整的轮把 N 个查询全打完）；⚠️ 残差（如实登记，计划 T1-8）：单个游戏的段内部「撤+加」分页窗口（R3-U1 同族）对本路仍盲 ⇒ 消失判定由 `_mark_gone` 的「段查复核」一跳兜。
    ⭐ D-0321（业务方拍板）：零行游戏的跳过态（`binding`/`now` 缺省时整段不生效，行为与修前逐字一致）。查询失败时若该绑定名下该游戏零张 `listed` 行（`_binding_game_has_listed_rows`，只在失败发生时查一次）⇒ 连败+1；连败满 `ZERO_ROW_GAME_SKIP_ROUNDS`（=3，约 9 分钟）⇒ 进跳过态（一行 warning 日志）；⚠️ 触发进态的这一轮照旧判不完整——豁免从下一轮起（业务方口径「连败 3 轮 ⇒ 第 4 轮起不再查询」）；
    跳过态里：本轮不再查询该游戏；到探测点（每小时一次）才查一次当发现通道——探测失败不进本轮完整性判定（留在跳过态，下一小时再探）；探测答上来 ⇒ 退出跳过态（一行 info 日志）并照常处理探到的行（⭐ D-0366 注：探测成功的轮该游戏有证据——状态条目已删 ⇒ 其镜像照常进 `_mark_gone` 的候选；探测失败/未到点 ⇒ 无证据、不进候选）；完整性判定 ⇒ 失败清单里剔除跳过态的游戏后为空才判完整（其余游戏照常），判完整的轮打一行 info 日志点名被剔除的游戏（审计能看出「伊莫被跳过」）；⚠️ 已有 `listed` 行的游戏一字不豁免：它们的查询失败仍走原 fail-safe 整轮不判消失（那些卡还在大厅里，宁停勿误判）；查询成功 ⇒ 连败清零（状态条目删除）；跳过态里长出第一张 `listed` 行 ⇒ 由 `_exit_zero_row_skips_with_rows`（`sync_hall` 行循环后调）立即退出。"""
    collected: list[dict] = []
    skip_states = (
        _ZERO_ROW_GAME_SKIP.setdefault(_hall_round_key(binding), {})
        if binding is not None else None
    )
    skipped_this_round: list[str] = []
    for game in games:
        state = skip_states.get(game.pk) if skip_states is not None else None
        if state is not None and state.skip:
            # ⭐ D-0321：跳过态。未到探测点 ⇒ 本轮不查；到点 ⇒ 探一次（发现通道）。
            if (
                now is None
                or state.last_probe_at is None
                or now - state.last_probe_at >= ZERO_ROW_GAME_PROBE_EVERY
            ):
                try:
                    rows, count, hit_cap, more_after = _pull_all_pages_detail(
                        client, path=HALL_PATH, page_param="page",
                        query={"gameName": game.name},
                    )
                except JidanpuError:
                    # 探测失败：留在跳过态、下一小时再探；⛔ 不进本轮完整性判定。
                    state.last_probe_at = now
                    skipped_this_round.append(game.name)
                    continue
                # 探测答上来了 ⇒ 退出跳过态（一行日志）；探到的行往下照常并入本轮
                #（包括「未判全 ⇒ 整轮不完整」那条——它已经恢复成普通游戏了）。
                del skip_states[game.pk]
                logger.info(
                    "零行游戏跳过态解除（每小时探测成功，恢复每轮查询）"
                    " binding=%s game=%s", binding.pk, game.name,
                )
            else:
                skipped_this_round.append(game.name)
                continue
        else:
            try:
                rows, count, hit_cap, more_after = _pull_all_pages_detail(
                    client, path=HALL_PATH, page_param="page",
                    query={"gameName": game.name},
                )
            except JidanpuError as exc:
                if (
                    binding is not None
                    and now is not None
                    and not _binding_game_has_listed_rows(binding, game)
                ):
                    # ⭐ D-0321：零行游戏才豁免——连败计数；满 3 轮 ⇒ 进跳过态（一行
                    #    warning）。⚠️ **本轮**的失败照旧进完整性判定（第 1–3 轮不豁免）；
                    #    有 listed 行的游戏走不进这个分支 ⇒ 原 fail-safe 一字不宽。
                    state = skip_states.setdefault(game.pk, _ZeroRowGameSkip())
                    state.consecutive_failures += 1
                    if (
                        state.consecutive_failures >= ZERO_ROW_GAME_SKIP_ROUNDS
                        and not state.skip
                    ):
                        state.skip = True
                        state.last_probe_at = now
                        logger.warning(
                            "零行游戏逐游戏查询连败 %d 轮，进入跳过态"
                            "（下一轮起不再逐轮查询，改为每小时探测一次；"
                            "本行只在状态转移时打）binding=%s game=%s",
                            ZERO_ROW_GAME_SKIP_ROUNDS, binding.pk, game.name,
                        )
                return tuple(collected), HALL_PER_GAME_INCOMPLETE_REASON.format(
                    name=game.name[:GAME_NAME_IN_REASON_MAX], detail=type(exc).__name__,
                )
        if _round_is_complete(count, rows, hit_cap, more_after):
            # 成功 ⇒ 连败清零（状态条目整个删除——「不在表里」就是默认态）。
            if skip_states is not None:
                skip_states.pop(game.pk, None)
            collected.extend(rows)
            continue
        if count is None:
            detail = NO_COUNT_REASON
        elif hit_cap:
            detail = PAGE_CAP_REASON.format(max_pages=HALL_MAX_PAGES)
        elif more_after:
            detail = SUSPECT_SHORT_COUNT_REASON.format(count=count)
        else:
            detail = PULL_INCOMPLETE_REASON.format(got=len(rows), count=count)
        return tuple(collected), HALL_PER_GAME_INCOMPLETE_REASON.format(
            name=game.name[:GAME_NAME_IN_REASON_MAX], detail=detail,
        )
    if skipped_this_round:
        # ⭐ D-0321：判完整的轮把剔除的游戏名写清（审计能看出「伊莫被跳过」）；
        # 每轮最多这一行，⛔ 不逐游戏刷屏。
        logger.info(
            "本轮逐游戏拉取判完整，剔除 %d 个跳过态零行游戏（其查询失败未计入）：%s"
            " binding=%s", len(skipped_this_round), "、".join(skipped_this_round),
            binding.pk if binding is not None else None,
        )
    return tuple(collected), ""


def _loop_throw_keys(rows) -> set[str]:
    """⭐ 自家回环卡的判据（业务方拍板）：本轮拉到的行里，展示单号（`orderIdView`）恰等于某张「已抛给下家鸡蛋铺」的本地单的抛出键的那批键值。口径（从严、宁漏勿误）：抛出键=`effective_source_order_no(order)`（`jidanpu_member` 的唯一出处：`source_order_no` 非空白用它、否则回落 `order_no`——我方录单到对方系统时 `souresOrderID` 写的就是它，且对方逐字把它落在 `orderIdView` 上（T19 实测，`jidanpu.OWN_SOURCE_VALUE_FIELD`）⇒ 回环卡的 `orderIdView` 恰是这个值）；「已抛出去」=该单存在 `JidanpuMemberOrderLink` 行（抛单录成功后的回执链路，`jidanpu_member._ensure_link`）：只在有链路的单上认命中——撞号而无关的单（对方第三方单的 `orderIdView` 恰与本租户某单同号、但那张单从没抛给任何下家）⇒ 不拦（「宁漏勿误」：拦错的代价是把一张真第三方单挡在我方大厅外）；
    peer 镜像卡那条路天然被覆盖：`PeerHallMirror` 落 `listed` 即建派生隐藏单、其 `source_order_no`=`peer_order_no`（`hall_shadow` 的三处写点）⇒ 它抛出去的键就是 `peer_order_no`，走 `Order` 两列即可命中 ⛔ 不另查 `PeerHallMirror`；价格（0.9×0.85 量级）刻意不进判据：折扣是对方可调的旋钮、也是数值巧合的重灾区——身份判据（单号+抛出链路）已足够强，拿数值当闸会把「对方改了折扣」的回环卡漏掉、把价格巧合的真单误拦；⚠️ 软删单不参与（走 `Order.objects`，默认排除已删）：被删单的回环卡不拦，与现状（照常建镜像）一致——删单业务上已作废，拦它没有收益；
    性能：每轮一次批量查询（`Order` JOIN `JidanpuMemberOrderLink`，`IN` 一批 `orderIdView`）⛔ 不逐卡三连查：一轮 60–80 张（全量轮可到数百张）也只此一发；查完在 Python 侧按 `effective_source_order_no` 的口径对拍（`.only` 只取两列，命中候选通常个位数）。"""
    views = {
        str(row.get("orderIdView", "") or "").strip() for row in rows
    } - {""}
    if not views:
        return set()
    keys: set[str] = set()
    candidates = (
        Order.objects
        .filter(jidanpu_member_links__isnull=False)
        .filter(Q(source_order_no__in=views) | Q(order_no__in=views))
        .only("order_no", "source_order_no")
    )
    for order in candidates:
        key = jidanpu_member.effective_source_order_no(order)
        if key in views:
            keys.add(key)
    return keys


#: ⭐ D-0335 清道夫认「已终态」的事件状态集：`done` / `skipped` 是收敛终态，`abandoned`
#: 是烧完重试额度的终态。`failed` **不在内**——还有额度的事件由后台轮重试
#: （`_consume_hall_claim` 的 `claimed` 断点续转正），碰它会打断崩溃恢复。
_HALL_CLAIM_SETTLED_EVENT_STATUSES = ("done", "skipped", "abandoned")


def _hall_claim_event_settled(mirror) -> bool:
    """⭐ D-0335：这张镜像行的 `upstream.hall_claim` 事件是否已终态（清道夫判据）。按 `payload__mirror_id` 找（⛔ 不反拼 `target_ref`：ref 里嵌的是排队那一刻的绑定 pk，`owner_binding` 会轮换/`SET_NULL` 后拼不出；`queue_hall_claim` 的 payload 恒带 `mirror_id`）；
    取最新一条（同键重排/`rearm` 会一条接一条）；一条都没有（极端：排队事务与拉取轮的历史错位）按「未终态」处理——不碰那行，宁可保守。"""
    event = (
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_HALL_CLAIM, payload__mirror_id=mirror.pk
        )
        .order_by("-id")
        .first()
    )
    if event is None:
        return False
    return event.status in _HALL_CLAIM_SETTLED_EVENT_STATUSES


def _admit_unmapped_card(mirror, *, fields, binding, game, now):
    if game is None:
        return False
    with transaction.atomic():
        current = binding.current_identity_rows().filter(enabled=True).select_for_update().first()
        if current is None or not allowed_upstream_game(current, game.pk):
            return False
        percent = upstream_game_price_percent(current, game.pk) if current.game_rules is not None else None
        updated = JidanpuUpstreamHallOrder.objects.filter(
            pk=mirror.pk, status=mirror.status,
        ).update(
            status=_HALL_LISTED, hall_price_percent_at_sync=percent,
            gone_at=None, last_seen_at=now, **fields,
        )
        if updated:
            from apps.accounts.wxpusher_hall import queue_hall_publication

            mirror.game = game
            queue_hall_publication("upstream", mirror, listed_at=now)
        return updated


def sync_hall(
    *, binding: JidanpuUpstreamBinding, client, now: datetime,
) -> HallOutcome:
    """拉一轮上家抢单大厅并镜像（spec §5.1/A2/A15）。⭐ D-0366（E-0314）——行来源只有逐游戏段查：业务方拍板「以后别用空游戏名查」（竹屿整厅口恒回 1 张+竹屿/初音/团团工作室三家 `key1` 复核对在厅活卡盲查——无游戏名全量路与 key1 复核两块基石在这类上家身上全部失真）。流程：地址级去重（非 owner ⇒ 一次上游都不打）→ 逐游戏（完整 `game.name`，GC 22 简称禁令依旧）拉全 → 逐行四级匹配我方游戏 → 按 `(地址, orderId)` upsert 镜像行（缺 `orderId` 跳过+台账）→ 只有拉全才把本轮没出现的 `listed` 行段查复核后标 `gone`（D-0366；见 `_mark_gone`）；
    ⭐ D-0300（业务方拍板）——终态再现分两支（原文「终态不复活」只对 `promoted` 仍成立）：`gone` 行在任何一轮里重新见到即自动复活回 `listed`（条件更新+清灰卡快照+`upstream.hall_rescued` 台账，照 `rescue_gone_mirrors` 命令的同一套语义；⛔ 不要求本轮 complete——完整轮、不完整轮都复活；与 `_mark_gone` 只在完整轮跑的不对称是有意的：「没看到」在拉不全时不可信，「看到了」任何时候都是直接证据）；`promoted` 仍只记台账 ⛔ 不复活（该行已转成我方正式订单，复活会造成重复接手）；
    ⚠️ GC 22/spec §16.1：简称禁止当上游 query（上游只认完整游戏名，传简称会静默返回 0 行）⇒ 每一发大厅 GET 都带完整 `game.name`（⛔ 无游戏名的整厅 GET 已随 D-0366 停用；防回潮钉子=`test_jidanpu_upstream_pull` 的「无 gameName GET 零次」）；⚠️ 匹配键一律 `orderId`（B4）⛔ 绝不用 `orderIdView`（实测会重号）；⚠️ 拉不全 ⇒ ⛔ 不判「消失」，原因进 `HallOutcome.reason` 由调用方响亮处理。"""
    owner = dedupe_owner(binding.jidanpu_base_url)
    if owner is None or owner.pk != binding.pk:
        # 同地址另有更早的启用绑定 ⇒ 这一份**一次上游都不打**（A2）。
        return HallOutcome(reason=NOT_OWNER_REASON, is_owner=False)

    # 一轮只查一次游戏表（`match_game_by_upstream_name` 的 `candidates` 注入口）。
    candidates = list(Game.objects.filter(deleted_at__isnull=True))

    # ⭐ D-0366（E-0314）：行来源只有逐游戏段查——无游戏名整厅路随本决策停用（竹屿整厅口恒回 1 张+三家 key1 复核是在厅活卡盲查，业务方拍板「以后别用空游戏名查」）；D-0266 的逐游戏拉取（`_pull_per_game_rows`）升格为全量路本体；
    #    任一游戏未判全/查询失败 ⇒ 整轮 `complete=False`（H2 fail-closed，见 `_pull_per_game_rows`）⇒ 下面 `_mark_gone` 不跑；拉到的行仍照常并入镜像（下一轮不再重复拉）；
    #    ⭐ YF-01 已随本决策删除：「成功回 0 行」的段=有证据，消失判定由 `_mark_gone` 的段查复核消费（同它的 docstring）。
    sync_games = candidates
    if binding.game_rules is not None:
        tracked_games = set(JidanpuUpstreamHallOrder.objects.filter(
            upstream_base_url=binding.jidanpu_base_url,
            status__in=(_HALL_LISTED, _HALL_CLAIMING, _HALL_GONE),
            game_id__isnull=False,
        ).values_list("game_id", flat=True))
        sync_games = [
            game for game in candidates
            if game.pk in tracked_games or allowed_upstream_game(binding, game.pk)
        ]
    rows, reason = _pull_per_game_rows(client, games=sync_games, binding=binding, now=now)
    if not binding.current_identity_rows().filter(enabled=True).exists():
        return HallOutcome(reason=BINDING_CHANGED_REASON, is_owner=False)
    complete = not reason

    listed = 0
    skipped = 0
    seen: set[str] = set()
    # ⭐ 自家回环卡拦截（含存量行）：**每轮一次**批量算出本轮行里的
    # 「我方抛出键」（`_loop_throw_keys` 的口径与性能判据见它的 docstring）；行循环里在
    # `existing` 读取之后、D-0300 复活分支之前消费它——新卡不建镜像、存量卡转 gone 不复活。
    loop_throw_keys = _loop_throw_keys(rows)
    for row in rows:
        remote_order_id = str(row.get("orderId", "") or "")
        if not remote_order_id:
            # ⛔ 没有幂等键 ⇒ 不落镜像行（落下去也无法与下一轮对上）；响亮留一条台账。
            # ⚠️ 这里**没有** mirror 可指认（没落行）⇒ 走 `_note_once_ref` 用「绑定地址」当
            # `target_ref`：同一句原因只记一条（⛔ 不是每轮一条——那正是 `note_once` 的存在理由）。
            skipped += 1
            _note_once_ref(
                target_type="hall", target_ref=binding.jidanpu_base_url[:_TARGET_REF_MAX],
                kind="upstream.hall_skipped", status="skipped",
                reason=MISSING_ORDER_ID_REASON,
                payload={"upstream_binding_id": binding.pk},
            )
            continue
        seen.add(remote_order_id)
        game_name = str(row.get("gameName", "") or "")
        game = match_game_by_upstream_name(game_name, candidates=candidates)
        existing = JidanpuUpstreamHallOrder.objects.filter(
            upstream_base_url=binding.jidanpu_base_url, upstream_order_id=remote_order_id
        ).first()

        # ⭐ 自家回环卡拦截（扩展到存量行）：展示单号恰是我方抛出去的键（判据从严：该单必须有下家鸡蛋铺链路，见 `_loop_throw_keys`）；
        #    ⚠️ 承重次序：这一段必须排在 D-0300 复活分支之前——复活由「拉到的卡命中 gone 镜像」触发、与 `seen` 无关；挪到复活分支之后，存量回环行会被「重新见到即复活」拉回 listed；
        #    台账/日志与「不建镜像」那一档同一形态（同 kind/target_ref/reason ⇒ 天然幂等：同一张回环卡先被新建拦截、后来又以存量行命中，也只留一条）。
        view = str(row.get("orderIdView", "") or "").strip()
        if view and view in loop_throw_keys:
            skipped += 1
            noted = _note_once_ref(
                target_type="hall",
                target_ref=f"{binding.jidanpu_base_url}/{view}"[:_TARGET_REF_MAX],
                kind=KIND_HALL_LOOP_INTERCEPT, status="skipped",
                reason=HALL_LOOP_THROW_REASON,
                payload={
                    "upstream_binding_id": binding.pk,
                    "upstream_order_id": remote_order_id,
                },
            )
            if noted:
                # 状态切换式日志（照零行隔离的日志纪律）：只在台账**首写**那一轮打一行
                # warning；之后每轮再拉到同一张回环卡都静默跳过，⛔ 不刷屏。
                logger.warning(
                    "上家拉取侧拦截自家回环卡（展示单号 %s 命中我方抛出键：新卡不建镜像；"
                    "存量卡转 gone 且不复活）binding=%s upstream_order_id=%s",
                    view, binding.pk, remote_order_id,
                )
            if existing is None:
                # 新卡：**不建镜像**（首轮拦截，它没有镜像行也不进消失判定的候选）。
                continue
            if existing.status in (_HALL_LISTED, _HALL_SKIPPED):
                # 存量回环行：条件更新置 **gone**（`gone_at=now`；⛔ 不刷 `last_seen`
                # ——它不是一张正常在厅的卡；⛔ 不写灰卡快照——回环卡没有「灰区价值」）。
                # 并发改走（rowcount=0）⇒ 静默跳过（照 `_mark_gone` 的条件更新口径）。
                updated = JidanpuUpstreamHallOrder.objects.filter(
                    pk=existing.pk, status__in=(_HALL_LISTED, _HALL_SKIPPED),
                ).update(status=_HALL_GONE, gone_at=now)
                if updated:
                    # 顺清已有快照（防「可接区一张 + 灰区一张」；照 D-0300 复活分支的
                    # 同一形态：辅助数据失败响亮留痕、⛔ 不回滚状态更新）。
                    try:
                        from apps.business import hall_snapshot

                        hall_snapshot.clear_mirror_snapshots(existing)
                    except Exception:  # noqa: BLE001 —— 辅助数据失败不影响拦截（响亮留痕）
                        logger.exception(
                            "回环拦截置 gone 后的快照清理失败（不影响拦截）mirror=%s", existing.pk,
                        )
                    # ⭐ D-0325：被拦的存量卡转 gone ⇒ 此前已抛出去的
                    #    下家副本必须撤（回环卡不撤 = 下家大厅留一张永动机卡）。
                    from apps.business import upstream_hall_shadow

                    upstream_hall_shadow.refund_for_card(existing, now=now)
            # `gone` 命中 ⇒ 保持**不复活**（在这一支内 continue，⛔ 不落进下面的
            # D-0300 复活分支）；`claiming` / `promoted` ⇒ **状态一律不翻**（防炸并发
            # 接单 / 已转正的行），连 `last_seen` 都不刷——它们走完台账就到此为止。
            continue

        if existing is None:
            with transaction.atomic():
                # 与保存游戏规则串行，避免新卡错过首次冻结旧卡。
                current = binding.current_identity_rows().filter(enabled=True).select_for_update().first()
                if current is None:
                    return HallOutcome(pulled=len(rows), listed=listed, skipped=skipped,
                                       reason=BINDING_CHANGED_REASON, is_owner=False)
                game_id = game.pk if game is not None else None
                if not allowed_upstream_game(current, game_id):
                    skipped += 1
                    continue
                mirror, created = JidanpuUpstreamHallOrder.objects.get_or_create(
                    upstream_base_url=binding.jidanpu_base_url,
                    upstream_order_id=remote_order_id,
                    defaults={
                        "status": _HALL_LISTED if game is not None else _HALL_SKIPPED,
                        "first_seen_at": now,
                        "last_seen_at": now,
                        "hall_price_percent_at_sync": (
                            upstream_game_price_percent(current, game_id)
                            if current.game_rules is not None else None
                        ),
                        **_mirror_fields(row, game=game, owner_binding=current),
                    },
                )
                if created and game is not None:
                    from apps.accounts.wxpusher_hall import queue_hall_publication

                    queue_hall_publication("upstream", mirror, listed_at=now)
            if not created:
                continue
            if game is None:
                skipped += 1
                note_once(
                    mirror=mirror, kind="upstream.hall_skipped", status="skipped",
                    reason=_unmatched_reason(game_name),
                )
            else:
                listed += 1
                # ⭐ D-0325：新卡进厅 ⇒ 派生隐藏单并抛给合格下家（价闸/硬闸都在 `upstream_hall_shadow` 那一处）。
                #    ⭐ S-06（第五轮审计）：逐行隔离——单卡派生路径若因数据形态抛错，只响亮留痕跳过这一张 ⛔ 不让行循环在这里断（后面的行不处理、`_mark_gone` 跑不到 ⇒ 消失判定停摆、幽灵卡堆积；与快照清理同形）。
                from apps.business import upstream_hall_shadow

                try:
                    upstream_hall_shadow.ensure_for_card(mirror, now=now)
                except Exception:  # noqa: BLE001 —— 单卡失败不影响整轮（响亮留痕、不回滚）
                    logger.exception(
                        "镜像卡隐藏单派生失败（跳过该卡，不影响本轮其余行）"
                        "binding=%s mirror=%s card=%s",
                        binding.pk, mirror.pk, mirror.upstream_order_id,
                    )
            continue

        if existing.status == _HALL_GONE:
            fields = _mirror_fields(row, game=game, owner_binding=binding)
            if existing.game_id is None:
                revived = _admit_unmapped_card(
                    existing, fields=fields, binding=binding, game=game, now=now,
                )
            else:
                if game is None:
                    fields.pop("game")
                revived = JidanpuUpstreamHallOrder.objects.filter(
                    pk=existing.pk, status=_HALL_GONE
                ).update(status=_HALL_LISTED, gone_at=None, last_seen_at=now, **fields)
            if not revived:
                # 读到落写之间被并发改走（例如我方打手已发起接单）⇒ 拿不到就不算、
                # 也不记台账（那条台账会说假话；照 rescue 命令的既有口径）。
                continue
            # ⭐ 离厅快照（D-0296）：镜像复活回大厅了 ⇒ 删掉它的灰卡（防「可接区一张+灰区一张」一单双卡）。⚠️ 这一步失败要响亮留痕但不回滚复活：快照是读面的辅助数据，复活本身不能陪葬
            #    （照 rescue 命令的既有处理；clear 只按 mirror 的 pk 删，无需先刷新）。
            try:
                from apps.business import hall_snapshot

                hall_snapshot.clear_mirror_snapshots(existing)
            except Exception:  # noqa: BLE001 —— 辅助数据失败不影响复活（响亮留痕）
                logger.exception("镜像复活后的快照清理失败（不影响复活）mirror=%s", existing.pk)
            _record(
                direction="in", kind=KIND_RESCUED, status="done", target_type="hall",
                target_ref=(
                    f"{existing.upstream_base_url}/{existing.upstream_order_id}"
                    [:_TARGET_REF_MAX]
                ),
                reason=RESCUE_REASON, payload={"upstream_binding_id": binding.pk},
            )
            listed += 1
            # ⭐ D-0325：卡复活回厅 ⇒ 隐藏单复活并重抛（同一张接着用，卡 gone 期间退掉的下家副本由这里补回）。⚠️ 先 `refresh_from_db()`：上面那次 `.update(**fields)` 只写了库，内存实例还停在旧投影上——隐藏单的价闸/改价读的正是 `remote_rec_fee`，不刷新会把旧价当新价；
            #    ⭐ S-06：派生失败逐行隔离（响亮留痕、复活本身已落库不回滚）。
            from apps.business import upstream_hall_shadow

            existing.refresh_from_db()
            try:
                upstream_hall_shadow.ensure_for_card(existing, now=now)
            except Exception:  # noqa: BLE001 —— 单卡失败不影响整轮（响亮留痕、不回滚）
                logger.exception(
                    "镜像复活后的隐藏单重抛失败（跳过该卡，不影响本轮其余行）"
                    "binding=%s mirror=%s card=%s",
                    binding.pk, existing.pk, existing.upstream_order_id,
                )
            continue
        if existing.status == _HALL_PROMOTED:
            # ⛔ `promoted` 仍终态不复活（D-0300 保留的一支）：该行已转成**我方正式
            #    订单**，复活会造成重复接单，这条口子不能开。状态一个字不改，只碰
            #    一次 `last_seen_at` + 记台账（原「终态不复活」分支的语义原样保留）。
            _touch(existing, now=now, status=existing.status, owner_binding=binding)
            note_once(
                mirror=existing, kind="upstream.hall_skipped", status="skipped",
                reason=TERMINAL_REAPPEAR_REASON,
            )
            continue
        if existing.status == _HALL_CLAIMING:
            # 正在接手/转正流程上，拉取轮不许把它抢回去（也不重算投影字段）。⭐ D-0335 清道夫（「消掉抢单锁定」的存量与崩溃残渣出口）：唯一例外=这次接单的 `upstream.hall_claim` 事件已终态（done/skipped/abandoned）且链路也没建出来 ⇒ 它永远等不到收口（F6 的「烧完额度永久停在 claiming」、或进程死在半路后事件烧完额度）⇒ 当场放回大厅；
            #    ⚠️ `failed` 且还有额度的不碰：那是后台轮正在重试的崩溃恢复路（`_consume_hall_claim` 的 `claimed` 断点续转正）；⚠️ 事件按 `payload__mirror_id` 找：`target_ref` 里嵌的是排队那一刻的绑定 pk，而 `owner_binding` 会轮换/`SET_NULL`，反拼不可靠；payload 恒带 `mirror_id`（`queue_hall_claim`）；
            #    ⚠️ 放回的行照复活两支（gone 复活/skipped→listed）同一纪律补隐藏单（`ensure_for_card` 幂等；S-06 同款逐行隔离，失败响亮留痕不拦本轮）。
            if not _hall_claim_event_settled(existing):
                _touch(existing, now=now, status=_HALL_CLAIMING, owner_binding=binding)
                continue
            if _link_for_by_remote(binding, existing.upstream_order_id) is not None:
                _touch(existing, now=now, status=_HALL_CLAIMING, owner_binding=binding)
                continue
            from apps.business import hall_snapshot, upstream_hall_shadow

            released = JidanpuUpstreamHallOrder.objects.filter(
                pk=existing.pk, status=_HALL_CLAIMING
            ).update(
                status=_HALL_LISTED,
                claiming_by_id=None,
                claiming_at=None,
                last_error=HALL_CLAIM_STALE_RELEASED_REASON[:255],
            )
            if released:
                try:
                    hall_snapshot.clear_mirror_snapshots(existing)
                except Exception:  # noqa: BLE001 —— 辅助数据失败不拦收敛（响亮留痕）
                    logger.exception(
                        "清道夫放回的灰卡清理失败 mirror=%s", existing.pk
                    )
                _record(
                    direction="in", kind=KIND_HALL_CLAIM_RELEASED, status="done",
                    target_type="hall",
                    target_ref=(
                        f"{existing.upstream_base_url}/{existing.upstream_order_id}"
                        [:_TARGET_REF_MAX]
                    ),
                    reason=HALL_CLAIM_STALE_RELEASED_REASON,
                    payload={"mirror_id": existing.pk, "upstream_binding_id": binding.pk},
                )
                listed += 1
                existing.refresh_from_db()
                try:
                    upstream_hall_shadow.ensure_for_card(existing, now=now)
                except Exception:  # noqa: BLE001 —— 单卡失败不影响整轮（响亮留痕，S-06 同款）
                    logger.exception(
                        "清道夫放回后的隐藏单补抛失败（跳过该卡，不影响本轮其余行）"
                        "binding=%s mirror=%s card=%s",
                        binding.pk, existing.pk, existing.upstream_order_id,
                    )
            continue

        fields = _mirror_fields(row, game=game, owner_binding=binding)
        if existing.status == _HALL_SKIPPED:
            if game is None:
                _touch(existing, now=now, status=_HALL_SKIPPED, owner_binding=binding)
                skipped += 1
                note_once(
                    mirror=existing, kind="upstream.hall_skipped", status="skipped",
                    reason=_unmatched_reason(game_name),
                )
            else:
                # `skipped → listed`：重评游戏映射 + 刷新投影（游戏后来补上了 / 加了别名）。
                updated = _admit_unmapped_card(
                    existing, fields=fields, binding=binding, game=game, now=now,
                )
                if not updated:
                    continue
                listed += 1
                # ⭐ D-0325：进厅 ⇒ 派生隐藏单并抛（此前 skipped 期没有）。
                #    ⚠️ 先 `refresh_from_db()`（上面那次 `.update` 只写了库，理由同复活支）。
                #    ⭐ S-06：派生失败逐行隔离（响亮留痕，进厅本身已落库不回滚）。
                from apps.business import upstream_hall_shadow

                existing.refresh_from_db()
                try:
                    upstream_hall_shadow.ensure_for_card(existing, now=now)
                except Exception:  # noqa: BLE001 —— 单卡失败不影响整轮（响亮留痕、不回滚）
                    logger.exception(
                        "skipped→listed 的隐藏单派生失败（跳过该卡，不影响本轮其余行）"
                        "binding=%s mirror=%s card=%s",
                        binding.pk, existing.pk, existing.upstream_order_id,
                    )
            continue

        # `listed` 行：刷新投影。⚠️ 游戏映射**失效**时（游戏被软删 / 改名对不上）**不把 `game`
        # 覆写成 NULL**：模型把 `game=NULL` 绑给 `skipped`，而 spec §5.1 没有 `listed → skipped`
        # 这条边 ⇒ 保留旧 FK、状态不动，只留一条台账（与 P2 的同一条处置同形）。
        if game is None:
            fields.pop("game", None)
            note_once(
                mirror=existing, kind="upstream.hall_skipped", status="skipped",
                reason=_unmatched_reason(game_name),
            )
        listed += 1
        JidanpuUpstreamHallOrder.objects.filter(
            pk=existing.pk, status=_HALL_LISTED
        ).update(last_seen_at=now, **fields)
        # ⭐ D-0325：每轮 listed 刷新 ⇒ 对一次隐藏单（无单且价好 ⇒ 补建——覆盖「上家改价把 0 元卡改成有价」「旧轮没建出来」；有单 ⇒ `sync_price_for_card` 同值 no-op，上家改价/换绑定百分比靠它传导到下家）；幂等：建/复活才排 launch；
        #    ⚠️ 先 `refresh_from_db()`：上面那次 `.update(**fields)` 把新价写进了库，而内存实例还停在上一轮的 `remote_rec_fee` 上——不刷新，改价那一跳会把旧价算成新价、同值 no-op，改价永远传不到下家（一次索引查询/行/轮，与隐藏单取数同量级）；
        #    ⭐ S-06：派生失败逐行隔离（响亮留痕；投影刷新已落库不回滚，本轮照常走到 `_mark_gone`——单卡抛错不再把整条绑定的大厅轮/消失判定带停，同 R3-M2 形状）。
        from apps.business import upstream_hall_shadow

        existing.refresh_from_db()
        try:
            upstream_hall_shadow.ensure_for_card(existing, now=now)
        except Exception:  # noqa: BLE001 —— 单卡失败不影响整轮（响亮留痕、不回滚）
            logger.exception(
                "listed 刷新的隐藏单对账失败（跳过该卡，不影响本轮其余行与消失判定）"
                "binding=%s mirror=%s card=%s",
                binding.pk, existing.pk, existing.upstream_order_id,
            )

    # ⭐ D-0321：跳过态里长出第一张 `listed` 行（本轮行循环里带进来的）⇒ 立即退出跳过态（在行循环之后调，本轮落库的行已经可见）；下一轮起恢复每轮查询；⚠️ 不论本轮 complete 与否都查——「出现了 listed 行」任何时候都是直接证据（与 D-0300 复活分支同一条不对称口径）；
    #    ⭐ D-0366 次序注：消失判定先于摘跳过态跑——`_mark_gone` 的候选闸读的是「本轮拉取时」的跳过态（那一刻没查过的游戏=无证据 ⛔ 不进候选）；摘态（D-0321）随后照旧，被摘的游戏下一轮起恢复每轮查询、自然恢复证据来源。
    gone = (
        _mark_gone(binding=binding, client=client, seen=seen, now=now)
        if complete
        else 0
    )
    _exit_zero_row_skips_with_rows(binding, candidates)
    return HallOutcome(
        pulled=len(rows), listed=listed, skipped=skipped, gone=gone,
        complete=complete, reason=reason,
    )


# ── 大厅读口那一路 + 三路归并 ────────────────────────────────


def upstream_hall_queryset(*, game_id=None, server_id=None, project_text=None, keyword=None):
    """本线镜像单的候选集（与另两路同一组筛选/同一语义，spec §16.1 第 4 条）。⚠️ `server_id is not None` ⇒ 整张表置空：镜像是文本区服、没有 FK，传 `?server=` 时它不出现（与 P2 镜像那一路同口径 ⛔ 不是「按文本碰运气」）；⚠️ `keyword` 只搜可见字段（上家单号/展示单号 `orderIdView`/游戏名/区/服/项目/备注）⛔ 不搜 `upstream_payload` 里被投影掉的字段（同 P2 的 S1 口径）；⚠️ 展示单号是例外——它在原样行里但确实是可见字段（P2 搜它的列），故照 P2 同口径搜 JSON 里那一个键；⚠️ `game` 只用 FK id（`select_related` 只为 `keyword` 那次 join ⛔ 不为解引用取名字）；⚠️ 游戏权限卡点由调用方（大厅读口）照另两路那样过 `scope_queryset`——本函数只做筛选 ⛔ 不在这里写第二道权限判据（两处判据必然漂移）。
    ⭐ `select_related("owner_binding")` 是承重的：投影 `serialize_upstream_row` 要读 `row.owner_binding` 上的勾兑比例，而大厅是把整个候选集逐行序列化的 ⇒ 漏了它=每行一次查绑定表（N+1，按候选集大小付费）；由 `tests/test_jidanpu_upstream_offer_price.py` 的查询计数用例钉住。"""
    qs = JidanpuUpstreamHallOrder.objects.filter(
        status=_HALL_LISTED,
        # ⭐ 归属绑定必须**启用中**（E-0288）：绑定停用后名下的 `listed` 镜像行会滞留大厅——卡片看得见、接单被 `claim_upstream_mirror` 的「归属绑定必须启用」前置拒掉（命中即下架 `listed → gone`）。
        #    可见与可接**同一道判据**，让这类僵尸行在读口就地消失；数据仍保留，绑定恢复启用且上游还在挂时由同步自然重新上厅。
        owner_binding__enabled=True,
    ).select_related("game", "owner_binding")
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    if server_id is not None:
        return qs.none()
    if project_text:
        qs = qs.filter(project_text__icontains=project_text)
    if keyword:
        qs = qs.filter(
            Q(upstream_order_id__icontains=keyword)
            #: 展示单号（`remote_order_view`）在**原样行**里（本表没有它的列）——照 P2
            #: `remote_hall_queryset` 搜 `remote_order_view` 的**同一口径**：
            #: 「可见字段」里它也在列，⛔ 不能因为它是 JSON 里的一员就不搜。
            | Q(upstream_payload__orderIdView__icontains=keyword)
            | Q(game__name__icontains=keyword)
            | Q(remote_game_area__icontains=keyword)
            | Q(remote_game_server__icontains=keyword)
            | Q(project_text__icontains=keyword)
            | Q(remote_remark__icontains=keyword)
        )
    return qs.order_by("-first_seen_at", "-id")


def upstream_hall_price_in_range(row, *, price_min=None, price_max=None) -> bool:
    """镜像行是否落在 `?price_min` / `?price_max` 区间内（**按展示价**，与另两路同语义）。
    价的**唯一出处** `upstream_hall_price`（⛔ 不是 `remote_rec_fee`——那是客户价）；判据一律 `is not None`（E-0021，`?price_min=0` 必须真的筛）、两端**含**（与另两路 `hall_price__gte/__lte` 同语义）。
    ⭐ 口径修订：上家没给 `recFee` 的行展示价是 **`0.00`**（`upstream_hall_price` 恒回 `Decimal`）⇒ `?price_min=0` 命中、`=1` 排除，⛔ 没有「没有展示价」那一档。"""
    price = upstream_mirror_hall_price(row)
    if price_min is not None and price < price_min:
        return False
    if price_max is not None and price > price_max:
        return False
    return True


def _money_text(value) -> str | None:
    """金额统一成**两位小数字符串**（与另两路行的金额键同形；`None` 原样回 `None`）。"""
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def _is_preferred_price(price) -> bool:
    """镜像行的优选判定——**薄转发**到 `hall_preferred.is_preferred_price`（阈值、`is not None` 判据只住在那一处，⛔ 不在本模块写第二份）。
    本包装存在的理由只是把函数级懒导入收在一处（`serialize_upstream_row` 每行调它，import 语句放那里会重复）。"""
    from apps.business import hall_preferred

    return hall_preferred.is_preferred_price(price)


def serialize_upstream_row(row: JidanpuUpstreamHallOrder) -> dict:
    """本线镜像行 → **与另两路恰好同键**的字典（spec §4.6 / §16.1 第 4 条）。
    ⚠️ 键表基底**复用** P2 的 `REMOTE_ROW_BASE`（⛔ 不复制一份：两处各自漂移会让前端「一张表渲染两套键」）；`origin` 三档可分辨 `native` / `remote` / `upstream`；带 `game`（FK id）供大厅分组 / `?game=` 三路共用，⛔ 不解引用游戏名（多一次查询，软删游戏会变假「在架」名）；`remote_order_view` 取 `orderIdView`、空则回落 `upstream_order_id`（只是展示值，⛔ 不作匹配键）。
    ⭐ `hall_price` 是勾兑后的我方发单价（算法唯一出处 `upstream_hall_price`）：`recFee` × 本行 owner 绑定 `hall_price_percent`——⛔ 不是 `recFee` 原值（那是客户价，本线键表没有它）。"""
    data = dict(REMOTE_ROW_BASE)
    payload = row.upstream_payload if isinstance(row.upstream_payload, dict) else {}
    view = str(payload.get("orderIdView") or "") or row.upstream_order_id
    price = upstream_mirror_hall_price(row)
    data.update(
        {
            "origin": "upstream",
            "source_system": JIDANPU,
            "remote_order_id": row.upstream_order_id,
            "remote_order_view": view,
            "remote_game_area": row.remote_game_area,
            "remote_game_server": row.remote_game_server,
            #: 本线镜像表**没有**这一列（上家没给工期描述）⇒ 与另两路同键、取空串。
            "remote_hours_desc": "",
            #: ⭐ **镜像行主键**：本线接单端点 `POST /api/business/hall/upstream/<int:pk>/claim/` 的 `<pk>` 就是这一行（`JidanpuUpstreamHallOrder.pk`）——前端拿到它才点得动那颗「接手此单」。
            #: ⚠️ 本线路是**唯一**填这个键的地方（另两路恒 `None`，键表仍恰好同键）；⛔ 别改成上家单号 / 展示单号：端点的 `<int:pk>` 按**本表主键**取行。
            "mirror_pk": row.pk,
            "game": row.game_id,
            "project_text": row.project_text,
            "remark": row.remote_remark,
            "game_role_name": row.remote_role_name,
            #: ⭐ **发单价**（打手在大厅里看到的就是「接这单能拿多少」）：上家 `recFee` × 本行 owner 绑定上的 `hall_price_percent`（默认 100 = 原价接手）。
            #: ⚠️ 口径修订：`recFee` 为空 / 认不出 ⇒ 按 `0.00` 算（`upstream_hall_price` 恒回 `Decimal`）⇒ 这里恒读到 `"0.00"` 字符串，⛔ 不再有 `None`；比例只作用在这里，同行的 `total_price`（客户价）不在本线键表里（镜像行不是订单）。
            "hall_price": _money_text(price),
            #: ⚠️ D-0302：走 `local_iso`（项目时区 +08:00）——内部大厅读口原生行的 `created_at` 是 DRF `DateTimeField`（+08:00），镜像行裸 datetime 经信封渲染成 UTC 串 ⇒ 同一页两族混屏、镜像行差 8 小时。
            #: ⚠️ `remote_created_at` 由 `_remote_datetime` 产出（naive ⇒ `make_aware`）、`first_seen_at` 是模型列，两支都保证 aware，`local_iso` 不会响。
            "created_at": local_iso(row.remote_created_at or row.first_seen_at),
            #: ⭐⭐ D-0296 加法键：镜像行**可接行**取值——`is_preferred` 按**勾兑后的展示价**（上面 `hall_price` 的原始值，⛔ 不是脱敏后的形状）判，阈值出处 `hall_preferred.PREFERRED_PRICE_THRESHOLD`；其余三键用基线（`sim_pk`=`None`、`is_exited`=`False`、`exit_kind`=`None`，已在 `REMOTE_ROW_BASE` 里）。
            #: ⚠️ 函数级懒导入（照 `claim_upstream_mirror` 里同一手法）：避免在本模块导入图顶部再拉一条 `hall_preferred → claim` 的链。
            "is_preferred": _is_preferred_price(price),
        }
    )
    return data


def upstream_hall_rows(*, price_min=None, price_max=None, **filters) -> list[tuple[datetime, dict]]:
    """本线镜像行的 `(进入大厅时刻, 行)` 列表（**已过筛选 / 已序列化**，给大厅读口那一路）。形状与原生那一路同构（都是 `(时刻, dict)` 二元组），两路可直接喂 `merge_hall_rows_two`。
    ⚠️ 价格区间在这里（内存里）过，⛔ 不在 `upstream_hall_queryset` 的 SQL 里（展示价是算出来的，理由见那个函数的 docstring）。
    ⚠️ **不带游戏权限卡点**（与 `upstream_hall_queryset` 同口径）：读口走「`upstream_hall_queryset` → `scope_queryset` → `serialize_upstream_hall_rows`」，本函数是「不加权限门」那一档的便捷入口（测试在用）。"""
    return serialize_upstream_hall_rows(
        upstream_hall_queryset(**filters), price_min=price_min, price_max=price_max
    )


def serialize_upstream_hall_rows(
    rows, *, price_min=None, price_max=None
) -> list[tuple[datetime, dict]]:
    """已 scoped 的镜像行（queryset / 任意可迭代）→ **已按展示价筛过**的 `(时刻, 行)` 列表；价格判定与 `upstream_hall_rows` 共用**同一条**（只写在 `upstream_hall_price_in_range` 一处）。
    存在的理由是读口必须**先过权限卡点**（那一步要 queryset），所以它拿「已收窄的行」而非筛选参数。
    ⚠️ 行来自 `upstream_hall_queryset` ⇒ 已 `select_related("owner_binding")`，这一跳的 `row.owner_binding` **不查库**。"""
    return [
        (row.first_seen_at, serialize_upstream_row(row))
        for row in rows
        if upstream_hall_price_in_range(row, price_min=price_min, price_max=price_max)
    ]


def merge_hall_rows_two(
    native: list[tuple[datetime, dict]],
    upstream: list[tuple[datetime, dict]],
) -> list[dict]:
    """两条**各自已按时间倒序**的流做归并（⛔ 不动 P2 的 `merge_hall_rows`）。原名 `merge_hall_rows_three` 收三路，P2 镜像路随租户级线拆除（`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）⇒ 收两路、名字改准；排序键 / 并列次序 / 返回形状一个字没改（纯减法）。
    - 排序键：原生 = `Order.created_at`、本线 = `JidanpuUpstreamHallOrder.first_seen_at`（两路同义：「进入大厅的时刻」）；
    - **相同时间戳 ⇒ 原生 → 本线**（并列次序写进排序键自己的性质，⛔ 不依赖「构造顺序 + 稳定排序」这两个间接前提）。"""
    items = [(moment, 0, payload) for moment, payload in native]
    items.extend((moment, 1, payload) for moment, payload in upstream)
    items.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [payload for _moment, _rank, payload in items]


# ══════════════════════════════════════════════════════════════════════════════
# 上家指派单 ⇒ 我方未分配池（A3）+ 老单门槛（A7）+ 钱的口径（A9）
# ══════════════════════════════════════════════════════════════════════════════

#: 录进未分配池的**在途状态**（A7 / spec §5.2 的**唯一出处**）。不在集合里的行一律**不录**（留台账 + 人话）——否则**第一次绑定**就会把该账号名下全部历史单（已完成 / 已退单 / 问题单 …）灌进我方未分配池，正是 A7 要避免的「老单越滚越多」。
#: ⚠️ 值是上家**实际下发的中文标签**（spec §2.1 **实测**：`orderSta` 给中文，⛔ 不是 `DSH` / `DLZ` 那种码）⇒ 集合里放中文；⛔ 改成码会让真实回包**一条都录不进来**。
IN_FLIGHT_TOKENS: tuple[str, ...] = ("待上号", "代练中")

#: 录进未分配池的台账 kind（spec §5.2 的 `upstream.record`）。
RECORD_KIND = "upstream.record"
#: 不录的台账 kind（spec §5.2 的 `upstream.record_skipped`）。
RECORD_SKIPPED_KIND = "upstream.record_skipped"

#: 录单成功的人话（spec §5.2 的原文句式 + 落点）。
RECORD_REASON = "上家指派给「{name}」，已录进我方未分配池（录单人 = 该客服，等人指派打手）"
#: 不在在途集合里 ⇒ 不录（A7 的门槛，人话点明是哪个状态）。
RECORD_NOT_IN_FLIGHT_REASON = (
    "上家这一单的状态是「{token}」，不在在途范围（待上号 / 代练中）内 ⇒ 未录进我方未分配池"
)
#: `orderSta` 空 / 缺失 ⇒ **判不了**在途 ⇒ 不录（⛔ 不猜、⛔ 不按历史单处理成别的）。
RECORD_NO_STATUS_REASON = "上家这一单没有可读的状态（orderSta 为空）⇒ 未录进我方未分配池"
#: 缺 `orderId` 的行没有幂等键 ⇒ 不录（⛔ 不拿 `orderIdView` 顶替：实测它会重号，B4）。
RECORD_MISSING_ORDER_ID_REASON = "上家这一单缺少 orderId ⇒ 没有幂等键，未录进我方未分配池"
#: 游戏名四级都不中 ⇒ 建不了单（`Order.game` 非空）⇒ 不录 + 请人工加别名。
#: ⚠️ 与大厅那一路同一条业务口径：**认错游戏比漏一张更糟**（spec §16.1 第 3 条）⇒ 宁可漏也不猜。
RECORD_UNMATCHED_GAME_REASON = (
    "上家这一单的游戏「{name}」在我方对不上（四级匹配都不中）⇒ 未录进我方未分配池，请人工加别名"
)

#: ⭐ D-0324（生产实锤）：上家**重新指派**（复活同一张上家单）⇒ 重录新单的台账 kind。
#: 命名照本文件既有 kind 惯例（`upstream.record` / `upstream.status_in` 那一族：`upstream.<动作>`）。
REASSIGN_KIND = "upstream.reassign"
#: 重录的人话：reason 要写清「上家重新指派、已重录新单」（D-0324 的拍板口径），
#: 并点明旧单的去向（留作历史 ⇒ 对账时两边单号都对得上）。
REASSIGN_REASON = (
    "上家把这张单重新指派（同一上家单号、状态回到在途）⇒ 已重录为新单进未分配池"
    "（录单人 = 该客服）；上一张已退单留作历史，未作任何改动"
)


@dataclass(frozen=True)
class MyOrdersOutcome:
    """一轮「我的订单」同步的汇总（`sync_my_orders` 的返回值）。`pulled` = 本轮从「我的订单」**取回的行数**（含被门槛挡住的行）；`recorded` = 本轮**新建**的我方订单数（⭐ D-0324 起**含**「上家复活同一张单 ⇒ 重录的新单」）；`linked` = **已有链路**、这一轮走回拉分支的行数（回拉本身见 `_pull_row`）。
    `skipped` = 因门槛（不在途 / 没状态 / 缺 `orderId` / 游戏对不上）**没录**的行数；`followed` = 本轮**真的跟上家一起退了**的行数（调度层把它当 stdout 的「跟退 J」；⚠️ 与 `linked` 不同：已结账被挡住那一档**不**计进来）。
    `backfill_pending` 表示正常用完本轮预算，下轮还会续拉。真实读取异常仍向上抛。"""

    pulled: int = 0
    recorded: int = 0
    linked: int = 0
    skipped: int = 0
    followed: int = 0
    backfill_pending: bool = False
    read: bool = True


# ── 链路 / 文本的小工具（⛔ 全是收窄写入，绝不整行 `save()`）────────────────────


def _link_for_by_remote(binding, remote_id: str):
    """按 `(binding, 上家单号)` 取链路行（本线幂等键的一半，spec §4.2）；没有 ⇒ `None`。"""
    return (
        JidanpuUpstreamOrderLink.objects.filter(
            binding=binding, upstream_order_id=remote_id
        )
        .select_related("order")
        .first()
    )


def _touch_link(link, *, now: datetime) -> None:
    """已有链路的一轮：**只**刷新 `last_in_at`（⛔ 不重建订单）。状态 / 内容回拉也挂在这一支上；本函数只保证「已录过的不会变成第二张单」。"""
    JidanpuUpstreamOrderLink.objects.filter(
        Q(last_in_at__isnull=True) | Q(last_in_at__lt=now), pk=link.pk,
    ).update(last_in_at=now)


def _is_stale_pull(link, *, now: datetime) -> bool:
    return any(stamp is not None and now < stamp for stamp in (link.last_in_at, link.terminated_at))


def _clip_for_order(value, field_name: str) -> str:
    """一段展示 / 上游文本 → `Order.<field_name>` 的字符串值（超宽按列宽截断）。⚠️ 列宽**从模型元数据取**（⛔ 不抄 `300` / `128` / `64` 这类数字：抄一份就与模型漂移）；超宽值直接写会被 PostgreSQL 以 `value too long` 拒掉、整笔录单回滚（口径与 `apps/business/peer_inbound.py::_clip_to_column` 同源）。
    ⚠️ `None` ⇒ 空串（这些列都是 `blank=True` 的非空列）。"""
    text = "" if value is None else str(value)
    width = Order._meta.get_field(field_name).max_length
    return text if width is None else text[:width]


def _ensure_link(
    binding, order, *, remote_id: str, in_hall: bool, now: datetime
) -> JidanpuUpstreamOrderLink:
    """建链路行（同一 `(binding, order)` 已有 ⇒ 原样返回它，⛔ 不重建）。
    ⚠️ **唯一约束的兜底在调用方**：并发下 `(binding, upstream_order_id)` 撞唯一约束 ⇒ 这一句原样抛 `IntegrityError`，由调用方的 `atomic()` 把它**连同这次新建的订单**一起回滚，再按「已做过」处理。⛔ 不要在这里吞掉它——吞掉只回滚链路、订单会留下 ⇒ 同一张上家单变成**两张**我方单（本线最忌讳的那种错）。
    ⚠️ 写入面是**收窄**的（`create` 只写构造时给出的列）——⛔ 不整行 `save()`。"""
    link = JidanpuUpstreamOrderLink.objects.filter(binding=binding, order=order).first()
    if link is not None:
        return link
    return JidanpuUpstreamOrderLink.objects.create(
        binding=binding,
        order=order,
        upstream_order_id=remote_id,
        in_hall=in_hall,
        last_in_at=now,
    )


def _upstream_order_source_fields(binding) -> dict:
    """⭐ D-0261：上家单落池 / 转正共用的「订单来源」两列（**恰好这一份** helper，⛔ 不抄两份）。
    配了来源 ⇒ `{"order_source": <字典项>, "source": <当时的名字>}`——`source` 是**文本快照**（上家路没有「换绑」形态，每单按**当时**的字典项名快照；与手工录单 `source_snapshot_overrides` 的「同一 FK 不重拍名字」不是一回事，⛔ 不把两条口径合并）；未配置 / 字典项被删（`SET_NULL`）⇒ `{}`（fail-soft，⛔ 不挡接单、⛔ 不回落）；**已有**单不受影响（`Order.source` 快照留下，`Order.order_source` 随删置空）。
    ⚠️ 列宽不变式：`OrderSource.name` 上限 10 ≤ `Order.source` 上限 64 ⇒ 快照**不会截断**（两侧列宽网各自钉着；⛔ 不再抄一份 clip——没有能失败的场景就不写防御）。"""
    source = binding.order_source
    if source is None:
        return {}
    return {"order_source": source, "source": source.name}


def _create_pool_order(binding, row, *, game, now: datetime, percent_at_sync=None) -> Order:
    """`record_assigned_order` 的**建单核心**（订单 + 绩效行 + 录单台账；**不含**链路）。
    ⭐ D-0324 拆出：重录路径（`_reassign_resurrected_order`）与首次录池**共用同一份投影**——两处各写一份必然漂移（`CONTENT_FIELDS` 与 `record_assigned_order` 的投影同一组，是同一条纪律）。⚠️ 只在**调用方的事务里**跑（本函数不自己开事务；台账与建单同事务，⛔ 不留「单在、这件事没发生过」的半截状态）。
    钱的口径 / 内容投影见 `record_assigned_order` 的 docstring（唯一出处，这里不复制第二份）。"""
    #: ⭐ ：用 `is None` 判有没有价（⛔ 不用 `or`——`Decimal("0.00")` 是**假值**，
    #: 合法的 `recFee=0` 会被兜底值**静默顶掉**；今天两者恰好同值看不出来，兜底值一改就会
    #: 现形，`AGENTS.md` §5-E / E-0021 一族）。
    rec_fee = _rec_fee_or_none(row.get("recFee"))
    order = Order.objects.create(
        order_no=generate_order_no(),
        #: ⭐（业务方口径，`orderIdView` 计划）：来源单号 = **上家展示号**
        #:    （`orderIdView` 优先，缺 ⇒ 回落 `remote_id`＝`orderId`）——平台内部号
        #:    与打手在大厅看到的展示号对不上（见 `_display_source_no`）。
        source_order_no=_display_source_no(
            row, fallback=str(row.get("orderId", "") or "")
        ),
        status=OrderStatus.UNASSIGNED,
        #: ⭐ 探页加固（业务方口径）：`order_type` 写「代练」= `OrderType.BOOST`
        #:    ——与仓库里另一条同类路（`jidanpu_hall.promote_mirror` 转正）同一致；⛔ 留空会让
        #:    订单页的「订单类型」是空、`grab_limits` 也只能走兜底分支。
        order_type=OrderType.BOOST,
        game=game,
        #: 录单人 = 该绑定的客服（A3）。两列语义不同（P4-1）：`sales_user` 是真正的
        #: 用户引用（绩效行按它建），`sales_agent` 只是展示用文本快照。
        sales_user=binding.user,
        sales_agent=_clip_for_order(user_display_name(binding.user), "sales_agent"),
        #: `created_by` 刻意保持**用户名**快照（与模型那一列的定义一致，⛔ 不是显示名）。
        created_by=binding.user.get_username(),
        #: ⭐ 钱：客户价 ← 上家接单价 `recFee`（A9），`recFee` 空 ⇒ 0.00（照既有录单口径）；⭐ **发单价** = `recFee × 该绑定 hall_price_percent / 100`——算法唯一出处 `upstream_hall_price`（与大厅线转正**同一个** helper）。
        #: ⚠️ 口径修订：`recFee` 空 / 认不出 ⇒ `hall_price = 0.00`（按 0 勾兑，⛔ 不再留空、⛔ 不再有「请填发单价」那一档）。
        total_price=Decimal("0.00") if rec_fee is None else rec_fee,
        hall_price=upstream_hall_price(
            rec_fee, binding, game_id=game.pk, percent_at_sync=percent_at_sync,
        ),
        project_text=_clip_for_order(row.get("project"), "project_text"),
        content=_clip_for_order(row.get("orderContent"), "content"),
        remark=_clip_for_order(row.get("orderRemark"), "remark"),
        game_role_name=_clip_for_order(row.get("roleName"), "game_role_name"),
        game_account=_clip_for_order(row.get("gameAcc"), "game_account"),
        game_sub_account=_clip_for_order(row.get("zgameAcc"), "game_sub_account"),
        # ⭐ D-0261：订单来源 + 名字快照（与转正同一份 helper；未配来源 ⇒ {}）。
        **_upstream_order_source_fields(binding),
    )
    create_performance_for_order(order)
    #: 台账与建单**同事务**：⛔ 不留「单在、这件事没发生过」的半截状态。
    note_once(
        order=order, kind=RECORD_KIND, status="done",
        reason=RECORD_REASON.format(
            name=_clip_for_order(user_display_name(binding.user), "sales_agent")
        ),
        payload={"upstream_binding_id": binding.pk},
    )
    return order


def _has_pending_hall_claim(binding, card) -> bool:
    if card is None or card.status != _HALL_CLAIMING or card.claiming_at is None:
        return False
    # An interrupted send with no receipt can still enter the manual assignment pool.
    return JidanpuSyncEvent.objects.filter(
        Q(payload__send_started=False) | Q(payload__claimed=True)
        | Q(lease_owner__isnull=False, lease_expires_at__gt=Now()),
        direction="out", kind=KIND_HALL_CLAIM, status__in=("pending", "failed"),
        attempts__lt=OUTBOX_MAX_ATTEMPTS,
        payload__upstream_binding_id=binding.pk, payload__binding_revision=binding.identity_revision,
        payload__mirror_id=card.pk, payload__taker_id=card.claiming_by_id,
        payload__claiming_at=_claim_reservation_key(card.claiming_at),
    ).exists()


def _direct_assignment_card(binding, row, card):
    if (
        card is None or card.owner_binding_id != binding.pk or card.game_id is None
        or card.status not in (_HALL_LISTED, _HALL_GONE)
    ):
        return None
    previous_payload = card.upstream_payload if isinstance(card.upstream_payload, dict) else {}
    return None if _loop_throw_keys((row, previous_payload)) else card


def record_assigned_order(binding, row, *, game, now: datetime) -> Order | None:
    """把一行「上家指派给我的单」录进我方**未分配池**（A3 / spec §5.2）。`status=UNASSIGNED`、`order_type=BOOST`（探页加固）；**录单人 = 该绑定的客服**（`sales_user` 引用 + `sales_agent` 显示名文本快照；绩效行由既有 `create_performance_for_order` 一次建两条——打手那条 `user` **空着**等人指派、客服那条 = `order.sales_user`）；内容投影照 spec §5.2（`project_text`/`content`/`remark`/`game_role_name`/`game_account`/`game_sub_account`）；游戏按**四级匹配**（`match_game_by_upstream_name`）对上，对不上回 `None`（`Order.game` 非空，⛔ 不硬塞，spec §16.1 第 3 条）。
    ⭐ D-0261：「来源留空」只对**未配来源**的绑定成立——绑定配了「订单来源」字典项 ⇒ 经 `_upstream_order_source_fields`（与转正**同一份** helper）带 `order_source` + `source` 名字快照，未配来源照旧两列全空（fail-soft）。钱（**A9 / §15.1**）：`total_price ← recFee`（上家接单价 = 我方客户价）、`hall_price` = 勾兑后的我方发单价（算法唯一出处 `upstream_hall_price`；比例**只作用于发单价** ⛔ 不动 `total_price`；`recFee` 空 ⇒ `0.00` ⛔ 不留空）；上家 `price` **一个金额列都不落**（⛔ 含「顺手回落」兜底）。
    ⚠️ 幂等：先查 `(binding, 上家单号)` 链路；并发撞 `(binding, upstream_order_id)` 唯一约束 ⇒ `IntegrityError` 把新单一起回滚、重查后按「已做过」返回**既有**订单（⛔ 不当失败、⛔ 不留第二张单）。⚠️ 这一路⛔ 不套地址级去重（那是「谁负责拉大厅」，见 `dedupe_owner`）：同一上家地址 N 份绑定，各自账号被指派的单各录各的客服名下。⚠️ 已终止链路的「复活重录」在 `_reassign_resurrected_order`（D-0324）——链路已存在时本函数按「已做过」返回旧单，⛔ 别在这里再开一个口子。"""
    remote_id = str(row.get("orderId", "") or "")
    if not remote_id:
        return None
    try:
        with transaction.atomic():
            current = binding.current_identity_rows().filter(enabled=True).select_for_update().first()
            if current is None:
                return None
            existing = _link_for_by_remote(current, remote_id)
            if existing is not None:
                _touch_link(existing, now=now)
                return existing.order
            mirror = JidanpuUpstreamHallOrder.objects.select_for_update(of=("self",)).select_related("game").filter(
                upstream_base_url=current.jidanpu_base_url, upstream_order_id=remote_id,
            ).first()
            if _has_pending_hall_claim(current, mirror):
                return None
            card = _direct_assignment_card(current, row, mirror)
            if card is None:
                if game is None or not allowed_upstream_game(current, game.pk):
                    _note_record_skipped(
                        binding=current, target_ref=f"{current.jidanpu_base_url}/{remote_id}",
                        reason=_record_game_skip_reason(game, row),
                    )
                    return None
                order = _create_pool_order(current, row, game=game, now=now)
            else:
                percent = card.hall_price_percent_at_sync
                if percent is None:
                    percent = current.hall_price_percent
                order = _create_pool_order(
                    current, row, game=game or card.game, now=now, percent_at_sync=percent,
                )
            _ensure_link(binding, order, remote_id=remote_id, in_hall=False, now=now)
            if card is not None:
                # 真实指派已离厅。已有隐藏单撤下，旧卡以后也不再复活。
                from apps.business import upstream_hall_shadow

                upstream_hall_shadow.refund_for_card(card, now=now)
                card.status = _HALL_PROMOTED
                card.promoted_order = order
                card.save(update_fields=["status", "promoted_order"])
    except IntegrityError:
        existing = _link_for_by_remote(binding, remote_id)
        if existing is None:
            raise
        _touch_link(existing, now=now)
        return existing.order
    return order


def _reassign_resurrected_order(binding, link, row, *, game, now: datetime) -> tuple[Order | None, str | None]:
    """同号重新指派时重录未分配单，旧单留作历史。
    第二个返回值只表示已确认的游戏匹配或范围错误。
    """
    with transaction.atomic():
        locked = _locked_pull_link(binding, link)
        if locked is None or locked.terminated_at is None or _is_stale_pull(locked, now=now):
            return None, None
        current = locked.binding
        if game is None or not allowed_upstream_game(current, game.pk):
            return None, _record_game_skip_reason(game, row)
        previous = locked.order
        order = _create_pool_order(current, row, game=game, now=now)
        locked.order = order
        locked.terminated_at = None
        locked.in_hall = False
        locked.miss_count = 0
        locked.miss_since = None
        locked.miss_at = None
        locked.completed_submitted_at = None
        locked.last_in_at = now
        #: ⚠️ 收窄写入面（照 `_reset_miss` 的形状）：只动重置清单里的列，⛔ 不整行 save。
        locked.save(update_fields=[
            "order", "terminated_at", "in_hall", "miss_count", "miss_since",
            "miss_at", "completed_submitted_at", "last_in_at",
        ])
        note_once(
            order=order, kind=REASSIGN_KIND, status="done",
            reason=REASSIGN_REASON,
            payload={
                "upstream_binding_id": binding.pk,
                "previous_order_no": previous.order_no,
            },
        )
    return order, None


def _refresh_terminal_observation(binding, link, *, token, now) -> None:
    if token not in TERMINAL_REMOTE_TOKENS:
        return
    with transaction.atomic():
        current = _locked_pull_link(binding, link)
        if current is not None and current.terminated_at is not None:
            _touch_link(current, now=now)


def _record_game_skip_reason(game, row) -> str:
    if game is not None:
        return "这款游戏未加入该上家绑定的同步范围"
    return RECORD_UNMATCHED_GAME_REASON.format(
        name=str(row.get("gameName") or "")[:GAME_NAME_IN_REASON_MAX],
    )


def _note_record_skipped(*, binding, target_ref: str, reason: str) -> None:
    """「这一行没录」的台账（幂等：同一绑定 + 同一上家行 + 同一原因只留一条，⛔ 不每轮灌）。走 `_note_once_ref`：这些行**没有我方订单**可指认（`note_once` 需要 `order` 或 `mirror`），幂等键用「绑定 / 绑定地址 / 上家单号」（缺 `orderId` 时退化成绑定地址）。
    ⚠️ 键里必须有 `binding`：本线**允许同地址多份绑定**（A2 地址级去重只管「谁拉大厅」，⛔ 不管指派单），上家会把**同一个 `orderId`** 派给多个账号 ⇒ 两份绑定的 `target_ref` 与 `reason`（不含单号、只有状态 token）会**逐字相同** ⇒ 键里少了 `binding`，第二份的「没录」会被第一份**静默吞掉**（实测两份各 `skipped=1`、台账只剩 **1** 行）⇒ 台账答不了「这行归哪份绑定」（spec §5.2 ⛔ 不静默 / §4.4）。
    ⚠️ `binding.pk` **前置**（⛔ 不后置到 `target_ref` 尾部、⛔ 不塞进 `reason`）：`target_ref` 是 `CharField(191)` 且调用方传入的串本身已在 191 处右截断（后置会被长地址截掉、键又退化成不含 `binding`）；`reason` 是给人读的人话（台账页直接显示）且在 255 处右截断（塞机器标识污染 + 后置同样被截）。⚠️ 同族残差（登记，刻意不改）：地址很长时**同一绑定**下两个不同 `orderId` 仍可能截成同一串撞键（计划 Task 4 补丁的 X2 同族）。"""
    _note_once_ref(
        target_type="remote_order",
        target_ref=f"{binding.pk}|{target_ref}"[:_TARGET_REF_MAX],
        kind=RECORD_SKIPPED_KIND,
        status="skipped",
        reason=reason[:255],
        payload={"upstream_binding_id": binding.pk},
    )


# ── ⭐ 窗口拉取（①②并集）+ 在途按单补核（③）────────────────────────────────
#: 见计划 `docs/plans/2026-10-04-my-orders-windowed-pull.md` §1。三条能力**只用真机读数都成立的那两条**（`stime`/`etime` 生效、`key1` 生效）；退单可见性**未定论**（计划 §0 末行）⇒ ②只做「并集」，⛔ 不承诺「上家退单一定看得见」。


def _my_orders_round_rows(client, *, now: datetime) -> tuple[dict, ...]:
    """本轮的 ①② **并集**：窗口视图 + **同窗口**的「已退单」视图，按 `orderId` 去重。
    ⚠️ 并集是安全的：退单那一档可见性**未定论**（真机两轮读数相反）——要么比默认视图多出那一行、要么与默认视图逐字相同 ⇒ 合并两种读数都不会把已有的行弄丢；⛔ 不声明「退单一定看得见」、⛔ 不改「跟退」本身的判据（计划 §4 待决）。
    ⚠️ 缺 `orderId` 的行**只由①那一份承载**：它没有幂等键，②里再来一份只会把「不录 / 读不到」的**计数**灌成两倍（台账本身按原因幂等，不会多行）。"""
    rows: list[dict] = list(fetch_my_orders(
        client, window_hours=MY_ORDERS_WINDOW_HOURS, now=now,
    ))
    refund_rows = fetch_my_orders(
        client, window_hours=MY_ORDERS_WINDOW_HOURS,
        order_sta=MY_ORDERS_REFUND_STATUS, now=now,
    )
    seen = {str(row.get("orderId", "") or "") for row in rows}
    for row in refund_rows:
        remote_id = str(row.get("orderId", "") or "")
        if not remote_id or remote_id in seen:
            continue
        seen.add(remote_id)
        rows.append(row)
    return tuple(rows)


def _store_row_by_source_no(client, *, source_order_no: str, remote_id: str) -> dict | None:
    """来源单号可能复用，只接受仍对应原链路的上家订单。"""
    for row in fetch_my_orders(client, key1=source_order_no):
        if (
            str(row.get("orderIdView", "") or "") == source_order_no
            and str(row.get("orderId", "") or "") == remote_id
        ):
            return row
    return None


def _links_due_for_check(binding, *, seen_ids, observed_since=None, allow_missing=True) -> list[JidanpuUpstreamOrderLink]:
    """③ 该补核的链路：可回拉、且上家单号**没出现在①②行集里**的，最多 `MY_ORDERS_LINK_CHECK_MAX` 条。
    ⚠️ 轮转键 = `last_in_at` 最旧优先（`nulls_first`；同值再按 `pk` 保证可复现）：每次补核都 `_touch_link` 刷成当轮时刻 ⇒ 在途超过上限时，下一轮轮到的是**上一轮没轮到的那些**，⛔ 不饿死同一批（代价：最坏 `⌈在途/5⌉` 轮一圈，计划 §3-2）。
    ⚠️ `terminated_at__isnull=True` 与 `_is_pullable` 的①是**冗余**关系——两条都留着：软删 / 已退单那两档**只有** `_is_pullable` 管。"""
    candidates = (
        JidanpuUpstreamOrderLink.objects.filter(binding=binding, terminated_at__isnull=True)
        .select_related("order")
        .order_by(F("last_in_at").asc(nulls_first=True), "pk")
    )
    if observed_since is not None:
        candidates = candidates.filter(Q(last_in_at__lt=observed_since) | Q(last_in_at__isnull=True))
    if not allow_missing:
        candidates = candidates.order_by(Greatest("last_in_at", "last_partial_probe_at").asc(nulls_first=True), "pk")
    due: list[JidanpuUpstreamOrderLink] = []
    for link in candidates:
        if link.upstream_order_id in seen_ids:
            continue
        if not _is_pullable(link):
            continue
        due.append(link)
        if len(due) >= MY_ORDERS_LINK_CHECK_MAX:
            break
    return due


#: ⭐ （业务方裁定）：「连续查不到」判「上家已移除当前接手人」的门槛。
#: 三次**跨轮次**（不是同一轮的重复）、且跨度 ≥ 5 分钟；中间查到一次就清零。
#: ⚠️ 只有「上家单号 / 来源单号都回 0 行」才计数；登录失败 / 网络失败 / 5xx **不计数**。
MISS_CONFIRM_COUNT = 3
MISS_CONFIRM_SPAN = timedelta(minutes=5)

REMOVED_TAKER_REASON = (
    "上家已移除当前接手人（连续 {count} 次按上家单号与来源单号都查不到，自 {since} 起）"
    "⇒ 已按「已退单」收口"
)
REMOVED_TAKER_SETTLED_REASON = (
    "上家已移除当前接手人（连续 {count} 次查不到），但这一单**已结过账** ⇒ 只标记、不改状态，请人工核对"
)


def _reset_miss(link) -> None:
    """清零「连续查不到」计数（查到一次 / 已收口 / 被拒之后都调它）。"""
    if link.miss_count or link.miss_since is not None or link.miss_at is not None:
        link.miss_count = 0
        link.miss_since = None
        link.miss_at = None
        link.save(update_fields=["miss_count", "miss_since", "miss_at"])


def _bump_miss(link, *, now) -> bool:
    """记一次「三路都查不到」；回「够不够三连门槛（次数 + 跨度）」。"""
    if link.miss_at is not None and now <= link.miss_at:
        return False
    if not link.miss_count or link.miss_since is None:
        link.miss_count = 1
        link.miss_since = now
    else:
        link.miss_count = min(link.miss_count + 1, 32767)
    link.miss_at = now
    link.save(update_fields=["miss_count", "miss_since", "miss_at"])
    return bool(
        link.miss_count >= MISS_CONFIRM_COUNT
        and link.miss_since is not None
        and (now - link.miss_since) >= MISS_CONFIRM_SPAN
    )


def _maybe_conclude_removed_taker(binding, link, *, now) -> bool:
    """「三连查不到」的收口：没结账 ⇒ 走跟退（改已退单）；结过账 ⇒ 只留提醒、不改状态。
    ⚠️ 钱的事**不自己判**：走既有 `follow_refund`（它内部按「已结账」挡并给理由）；本函数只负责计数与台账人话。⛔ 这里是**唯一**把「查不到」升级成状态写入的地方。"""
    if not _bump_miss(link, now=now):
        return False
    order = link.order
    count = link.miss_count
    since = link.miss_since
    try:
        outcome = follow_refund(order, now=now, binding=binding)
    except IllegalTransition as exc:
        detail = str(exc)
        # ⚠️ 结账挡住那一档走**专用人话**（业务方要求「有钱的单只标记、不改状态、日志写清楚」）；
        #    其余拒绝（缺节点等）仍用既有的「被状态机拒绝」那句。
        reason = (
            REMOVED_TAKER_SETTLED_REASON.format(count=count)
            if "结账" in detail
            else REFUND_DENIED_REASON.format(detail=detail)
        )
        _note_order(binding=binding, order=order, status="failed", reason=reason)
        _reset_miss(link)
        return False
    if outcome.changed:
        _note_order(
            binding=binding, order=order, status="done",
            reason=REMOVED_TAKER_REASON.format(
                count=count,
                # ⚠️ D-0302：这句话进**订单台账**给人看；`miss_since` 存的是
                #    UTC 时刻，旧写法裸 strftime 出的是 UTC 墙钟（差 8 小时）⇒ 先 localtime。
                since=(
                    timezone.localtime(since).strftime("%m-%d %H:%M") if since else ""
                ),
            ),
        )
        _mark_terminated(link, now=now)
        _reset_miss(link)
        return True
    _note_order(
        binding=binding, order=order, status="failed",
        reason=REMOVED_TAKER_SETTLED_REASON.format(count=count),
    )
    _reset_miss(link)
    return False


def _my_order_row_anywhere(client, link) -> dict | None:
    """「这一单还在不在我的订单里」的**三连问**（③按单补核与 D-0324 兜底重核共用的一份实现）。第一问：默认视图 `key1` 单查（⛔ 不带时间窗——单查不受 24h 窗限制，`_my_order_row_by_key1`）；0 行 ⇒ 第二问：同一 `key1` 再问**退单视图**——⭐ **退单兜底**（受控实验修）：默认视图**看不见已退单的单**（实测：`key1` 对已退单的单回 0 行）⇒ 不问这一发，跟退**永远不会触发**；⚠️ 只在默认视图问不到时才多发这一发（命中率高时零额外请求）；⛔ 这一发**不带时间窗**（实测「退单视图 + 任意窗」恒为 0 行）。
    仍 0 行 ⇒ 第三问：按**来源单号**（`orderIdView`）问——⭐ **来源单号兜底**：上家移除接手人后「我的订单」看不见它，但按来源单号还能定位 ⇒ 再问这一发（`_store_row_by_source_no`，逐字回校验 `orderIdView`）。
    ⚠️ 回 `None` = 「三路都读不到」——⛔ 不判「远端没有这张单」（读不到 ≠ 不存在，spec §5.3 末行 / E-0004 同族）；怎么落口径由**调用方**决定（③走「读不到」台账 + miss 计数，兜底重核零写入）。"""
    row = _my_order_row_by_key1(client, remote_id=link.upstream_order_id)
    if row is None:
        row = _my_order_row_by_key1(
            client, remote_id=link.upstream_order_id, order_sta=MY_ORDERS_REFUND_STATUS,
        )
    if row is None:
        source_order_no = (getattr(link.order, "source_order_no", "") or "").strip()
        if source_order_no:
            row = _store_row_by_source_no(
                client, source_order_no=source_order_no, remote_id=link.upstream_order_id,
            )
    return row


def _check_missing_links(binding, *, client, now: datetime, seen_ids, probe_clock=None,
                         observed_since=None, allow_missing=True) -> tuple[int, int, int, int]:
    """③ 对**没出现在①②行集里**的在途链路按 `key1=<上家单号>` 逐单补核。回 `(hits, applied, failed, followed)`：命中（**逐字回校验**过 `orderId`）的条数、状态被写的条数、产出失败台账的条数、真的跟退的条数。
    命中 ⇒ 走**同一支** `_pull_row`（状态 / 内容回拉、跟退判据**全复用**，⛔ 不写第二份）；**0 行 ⇒ 只留一条台账人话，⛔ 不判「远端没有这张单」**（spec §5.3 末行口径：读不到 ≠ 不存在）。
    ⚠️ 无论命中与否都 `_touch_link`（它是「上次拉取时刻」：这一轮确实按单拉过它）——那也正是**轮转**的实现方式。"""
    hits = applied = failed = followed = 0
    for link in _links_due_for_check(binding, seen_ids=seen_ids, observed_since=observed_since,
                                     allow_missing=allow_missing):
        if not binding.current_identity_rows().filter(enabled=True).exists():
            break
        observed_at = probe_clock() if probe_clock is not None else now
        row = _my_order_row_anywhere(client, link)
        if not binding.current_identity_rows().filter(enabled=True).exists():
            break
        if row is None:
            with transaction.atomic():
                current = _locked_pull_link(binding, link)
                if current is None or _is_stale_pull(current, now=observed_at) or not _is_pullable(current):
                    continue
                if not allow_missing:
                    # 只推进轮转时刻，无命中不能影响列表缺失判据。
                    JidanpuUpstreamOrderLink.objects.filter(
                        Q(last_partial_probe_at__isnull=True) | Q(last_partial_probe_at__lt=observed_at),
                        pk=current.pk,
                    ).update(last_partial_probe_at=observed_at)
                    continue
                _touch_link(current, now=observed_at)
                _note_order(
                    binding=binding, order=current.order, status="failed",
                    reason=REMOTE_ROW_MISSING_REASON,
                )
                failed += 1
                # 三连查不到：没结账才跟退，已结账只留提醒。
                if _maybe_conclude_removed_taker(binding, current, now=observed_at):
                    applied += 1
                    followed += 1
            continue
        hits += 1
        outcome = _pull_row(link, row, binding=binding, client=client, now=observed_at)
        if outcome.applied:
            applied += 1
        if outcome.failed:
            failed += 1
        if outcome.followed:
            followed += 1
    return hits, applied, failed, followed


def _apply_my_order_batch(binding, client, rows, *, now, candidates, seen_ids):
    recorded = linked = skipped = followed = 0
    for row in rows:
        remote_id = str(row.get("orderId", "") or "")
        if not remote_id:
            skipped += 1
            _note_record_skipped(
                binding=binding,
                target_ref=binding.jidanpu_base_url[:_TARGET_REF_MAX],
                reason=RECORD_MISSING_ORDER_ID_REASON,
            )
            continue
        seen_ids.add(remote_id)
        ref = f"{binding.jidanpu_base_url}/{remote_id}"[:_TARGET_REF_MAX]
        link = _link_for_by_remote(binding, remote_id)
        if link is not None:
            if _is_stale_pull(link, now=now):
                continue
            # ⭐ D-0324：**已终止**链路 + 这一行的状态回了在途（待上号 / 代练中）= 上家把同一张单**重新指派**给我方账号 ⇒ 重录新单 + 链路重置（`_reassign_resurrected_order`），旧退单一字不动留作历史。
            #    ⚠️ 只有 `terminated_at` 非空那一档开这扇门：软删 / 我方已退单那两档（`_is_pullable` 的②③）不是「上家复活」，照旧零写入。
            token = _token_of(row.get("orderSta"))
            if link.terminated_at is not None and token in IN_FLIGHT_TOKENS:
                game = match_game_by_upstream_name(
                    str(row.get("gameName") or ""), candidates=candidates,
                )
                order, skip_reason = _reassign_resurrected_order(
                    binding, link, row, game=game, now=now,
                )
                if skip_reason is not None:
                    skipped += 1
                    _note_record_skipped(
                        binding=binding, target_ref=ref, reason=skip_reason,
                    )
                elif order is not None:
                    recorded += 1
                continue
            linked += 1
            if link.terminated_at is not None:
                _refresh_terminal_observation(binding, link, token=token, now=now)
            elif _is_pullable(link):
                if _pull_row(
                    link, row, binding=binding, client=client, now=now,
                ).followed:
                    followed += 1
            continue
        token = "" if row.get("orderSta") is None else str(row.get("orderSta")).strip()
        if not token:
            skipped += 1
            _note_record_skipped(
                binding=binding, target_ref=ref, reason=RECORD_NO_STATUS_REASON,
            )
            continue
        if token not in IN_FLIGHT_TOKENS:
            skipped += 1
            _note_record_skipped(
                binding=binding, target_ref=ref,
                #: ⚠️ 不额外截断 token：真实的 `orderSta` 是两三个汉字，整句长度由
                #: `_note_record_skipped` 的 255 上限兜（⛔ 不为此借游戏名那个常量）。
                reason=RECORD_NOT_IN_FLIGHT_REASON.format(token=token),
            )
            continue
        game = match_game_by_upstream_name(
            str(row.get("gameName") or ""), candidates=candidates
        )
        order = record_assigned_order(binding, row, game=game, now=now)
        if order is None:
            skipped += 1
            continue
        recorded += 1
    return MyOrdersOutcome(
        pulled=len(rows), recorded=recorded, linked=linked, skipped=skipped, followed=followed,
    )


def sync_my_orders(*, binding, client, now: datetime, probe_clock=None) -> MyOrdersOutcome:
    """分批录入指派单，期间可正面核对状态，扫完整段后才判断缺失。"""
    if not binding.enabled:
        return MyOrdersOutcome(read=False)
    from apps.business.jidanpu_upstream_backfill import pull_batches

    batches = []
    candidates = None
    seen_ids = set()

    def apply_rows(rows):
        nonlocal candidates
        if not binding.current_identity_rows().filter(enabled=True).exists():
            return
        if candidates is None:
            candidates = list(Game.objects.filter(deleted_at__isnull=True))
        batches.append(_apply_my_order_batch(
            binding, client, rows, now=now, candidates=candidates, seen_ids=seen_ids,
        ))

    report = pull_batches(binding=binding, client=client, now=now, apply_rows=apply_rows)
    hits = check_followed = 0
    if report.read:
        hits, _, _, check_followed = _check_missing_links(
            binding, client=client, now=now, seen_ids=seen_ids, probe_clock=probe_clock,
            observed_since=report.observed_since, allow_missing=report.complete,
        )
    if report.error is not None:
        raise report.error
    return MyOrdersOutcome(
        pulled=sum(batch.pulled for batch in batches) + hits,
        recorded=sum(batch.recorded for batch in batches),
        linked=sum(batch.linked for batch in batches) + hits,
        skipped=sum(batch.skipped for batch in batches),
        followed=sum(batch.followed for batch in batches) + check_followed,
        backfill_pending=report.read and not report.complete,
        read=report.read,
    )


# ── ⭐ D-0324：已终止链路的兜底重核（窗口洞收口）───────────────────────────────

#: 兜底重核**已终止**链路时、每份绑定每次最多核多少条（照 `MY_ORDERS_LINK_CHECK_MAX` 的 MAX 常量惯例；`terminated_at` **最旧优先**）。
#: ⚠️ 它是**对上游的发数闸**（每条最多三连问 = 3 发 `key1`），⛔ 不是本地算力闸——调大它之前先想 WAF（压发数的同一条纪律）。
TERMINATED_RECHECK_MAX = 20


@dataclass(frozen=True)
class TerminatedRecheckOutcome:
    """一次兜底重核的汇总（`recheck_terminated_links` 的返回值；判据打**数据库行**，这里只是 stdout / 排障的汇总面）。
    - `reassigned`：真的重录了新单的条数（链路已重置挂到新单）；`still_terminal`：查到了、但上家状态仍不在途（待验收 / 已完成 / 已退单 …）的条数——**零写入**（「停跟踪 = 往后不为它写任何东西」对没复活的单照旧成立）；`missing`：三路都读不到的条数（⛔ 不判「远端没有这张单」，零写入）；
    - `skipped`：在途但游戏四级都不中 ⇒ 没重录的条数（已留 `upstream.record_skipped` 台账请人工加别名）；`lines`：每张单一行的人话（命令原样打到 stdout，运维看得到每一张的结论）。"""

    reassigned: int = 0
    still_terminal: int = 0
    missing: int = 0
    skipped: int = 0
    lines: tuple[str, ...] = ()


def recheck_terminated_links(
    binding, *, client, now: datetime, limit: int = TERMINATED_RECHECK_MAX,
    upstream_order_id: str | None = None, games=None, probe_clock=None,
) -> TerminatedRecheckOutcome:
    """兜底重核该绑定的**已终止**链路（D-0324 窗口洞收口；管理命令 `recheck_terminated_links` 调的就是本函数——⛔ 命令不另写第二份实现）。
    ## 为什么不在③的每轮轮转里做（D-0324 登记的选型理由：b 兜底命令，而非 a 扩扫描面）——①段的 24h 窗按**上家录单时间**收窄 ⇒ 复活发生在窗外（退单后隔了天把天再指派）时那一行**永远**不进①②的行集，而③ `_links_due_for_check` 又**显式排除** terminated 链路 ⇒ 每轮的路都到不了它；把 terminated 纳入③要付出**永久**加发：terminated 链路随完单量**无限增长**（每张走完的单一条），照③每轮上限 5 条算 = 每轮固定多 5 发 `key1`（最坏 15 发），且**稀释在途链路的轮转**（三连判定依赖跨轮间隔）——正是 WAF 压发数（`MY_ORDERS_LINK_CHECK_MAX` 20→5，`docs/plans/2026-10-20-upstream-waf-risk-reduction.md`）要避免的形态 ⇒ 照 `jidanpu_member.reconcile_and_queue` / `rescue_gone_mirrors` 先例做**低频 / 一次性兜底命令**：不发常态请求，跑的时候才打上游。
    ## 行为——逐单走与③**同一份**三连问（`_my_order_row_anywhere`；`key1` 单查不带窗 ⇒ 天然覆盖 24h 窗外的复活，这正是窗口洞的收口）；查到且 `orderSta ∈ IN_FLIGHT_TOKENS` ⇒ **同一份** `_reassign_resurrected_order` 重录 + 链路重置（⛔ 不写第二份重录实现）；查到明确终态时只刷新观察时间，保留旧单和终止时间；三路都读不到时不写入，不计入在途链路的 miss；排序 `terminated_at` 最旧优先 + `limit` 上限（照③轮转惯例）；`upstream_order_id` 给了就只核那一张——**生产两笔卡死单的定向回补口**（D-0324 实锤现场）；停用的绑定 ⇒ 全零（spec §8.4，与 `sync_my_orders` 同一道闸）。"""
    if not binding.enabled:
        return TerminatedRecheckOutcome()
    links = (
        JidanpuUpstreamOrderLink.objects.filter(
            binding=binding, terminated_at__isnull=False,
        )
        .select_related("order")
        .order_by(F("terminated_at").asc(nulls_first=True), "pk")
    )
    if upstream_order_id is not None:
        links = links.filter(upstream_order_id=str(upstream_order_id))
    candidates = (
        list(Game.objects.filter(deleted_at__isnull=True)) if games is None else list(games)
    )
    reassigned = still_terminal = missing = skipped = 0
    lines: list[str] = []
    for link in links[: max(0, limit)]:
        observed_at = probe_clock() if probe_clock is not None else now
        row = _my_order_row_anywhere(client, link)
        if row is None:
            missing += 1
            lines.append(f"{link.upstream_order_id}：三路都读不到（读不到 ≠ 不存在），不动")
            continue
        token = _token_of(row.get("orderSta"))
        if token not in IN_FLIGHT_TOKENS:
            _refresh_terminal_observation(binding, link, token=token, now=observed_at)
            still_terminal += 1
            lines.append(f"{link.upstream_order_id}：上家仍在「{token}」（不在途），不动")
            continue
        previous_no = link.order.order_no
        game = match_game_by_upstream_name(
            str(row.get("gameName") or ""), candidates=candidates,
        )
        order, skip_reason = _reassign_resurrected_order(binding, link, row, game=game, now=observed_at)
        if skip_reason is not None:
            skipped += 1
            _note_record_skipped(
                binding=binding,
                target_ref=(
                    f"{binding.jidanpu_base_url}/{link.upstream_order_id}"[:_TARGET_REF_MAX]
                ),
                reason=skip_reason,
            )
            lines.append(f"{link.upstream_order_id}：未重录，{skip_reason}")
        if order is None:
            continue
        reassigned += 1
        lines.append(
            f"{link.upstream_order_id}：上家重新指派（{token}）⇒ 已重录新单 {order.order_no}"
            f"（原单 {previous_no} 留作历史）"
        )
    return TerminatedRecheckOutcome(
        reassigned=reassigned, still_terminal=still_terminal, missing=missing,
        skipped=skipped, lines=tuple(lines),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 状态 / 内容回拉（spec §5.3）+ 上家退单自动跟退（A11 / §15.3）+「写入面只开一格」+ 只跟踪在途单（§5.4）
# ══════════════════════════════════════════════════════════════════════════════

#: 台账 kind（spec §4.4 的表）：回拉发现**状态** / **内容**变化。
KIND_STATUS_IN = "upstream.status_in"
KIND_CONTENT_IN = "upstream.content_in"

#: ⭐ D-0267（E-0265）：认领单**群码截图拉取**的台账 kind。⚠️ **每次尝试一行（⛔ 不走 note_once 计数）**：它兼任**重试计数**——note_once 按 `(kind, target_type, target_ref, reason)` 四元组去重，第二轮起不再落行 ⇒ 计数恒 1、上限形同虚设。查数走现成索引 `jdp_event_target_idx`（`target_type, target_ref, created_at`）。
KIND_PULL_PICS_IN = "upstream.pull_pics_in"
#: 每单**自动**尝试的上限（S3：成功 / 空 / 失败**每次都计数**；到顶停手 + 终态台账）。⚠️ 代价如实：客户在平台上**晚传**的群码，5 轮后不再自动拉；人工重触发入口 = 打手在 App 里点开详情时排的**手工请求**（见 `queue_pull_pics_request`）。
#: ⚠️ 手工请求**不吃**这个额度：计数只数「自动那一轮留下的行」（`payload.trigger` 缺席的那些），否则线上已经烧完 5 次的单点开详情也照样不拉。
PULL_PICS_MAX_ATTEMPTS = 5
PULL_PICS_DONE_REASON = "已从上家拉回截图（新存 {pulled} 张、此前已有 {existing} 张{extra}）"
PULL_PICS_EMPTY_REASON = "上家这张单还没有可存的图（第 {attempt}/{max} 次自动尝试）"
PULL_PICS_FAILED_REASON = "上家截图没拉成：{detail}（第 {attempt}/{max} 次自动尝试）"
PULL_PICS_STOP_REASON = (
    "上家截图已自动尝试 {max} 次仍未拉到，停止自动重试；客户晚传的群码请人工到上家核对"
)

# ── 打手 App 的手工拉图请求（业务方拍板）────────────────────────────────────
# 形态：**入队一条 `upstream.pull_pics_in` 事件**，`payload["trigger"] = app_detail` ⇒ `jidanpu_sync` 的下一拍（`LOOP_SLEEP_SECONDS = 3`）在 `_run_upstream_binding` 里用 `run_pull_pics_requests` 消费，真正拉图仍走**同一份** `pull_order_screenshots`（⛔ 没有第二条拉图实现）。
# 选「入队 + 下一拍」而不是「请求里同步打上游」：请求线程不必等上游（ViewPic + 逐张下载最坏是分钟级），而 3 秒的一拍已经够快（页面在 3 秒 / 8 秒各刷一次详情）。

#: 手工请求在 `payload` 里的标记值：① 区分「待办请求」与「自动那一轮的台账行」；② 让自动计数把它排除掉（见 `PULL_PICS_MAX_ATTEMPTS` 的注释）。
PULL_PICS_TRIGGER_APP = "app_detail"
#: 同一单的**冷却**（秒）：冷却内重复调用**不再入队**（判据 = 最近一条本标记请求行的
#: `created_at`，**落在数据库里** ⇒ 跨进程 / 跨 worker 都可靠，不像进程内缓存会各算各的）。
PULL_PICS_REQUEST_COOLDOWN_SECONDS = 30
#: 手工请求行（`pending`）上写的那句人话（页面上 / 台账里看得出「谁要它拉的」）。
PULL_PICS_REQUEST_REASON = "打手在 App 里请求同步本单群二维码"
PULL_PICS_MANUAL_EMPTY_REASON = "上家这张单还没有可存的图（本次由打手在 App 里手动触发）"
PULL_PICS_MANUAL_FAILED_REASON = "上家截图没拉成：{detail}（本次由打手在 App 里手动触发）"
#: 消费时**不再打上游**的两档（请求排完之后现场变了）：单已不在回拉扫描面 / 本地已有群码。
PULL_PICS_REQUEST_GONE_REASON = "这张单已不在同步范围（已停跟踪 / 已软删 / 已退单），本次不拉图"
PULL_PICS_REQUEST_ALREADY_REASON = "本地已经有群二维码了，本次不重复拉取"

#: **不进扫描面**的我方状态：已退单（下游线收窄后，那一侧的对应常量叫 `jidanpu_member.PULL_TERMINAL_STATUSES`）
#: ——那一档由「退单」这件事本身表达（且上家那边还挂着这张单，A12 / §15.4），再回拉只会空转。
PULL_EXCLUDED_STATUSES = frozenset({OrderStatus.REFUNDED})

#: 上家「待验收」那一档（码 + 中文）——**它是我方自己提交完成之后的回音**（spec §7 改准块 1）。⛔ 照旧表映射会真的把它写进我方：`order_status.TRANSITIONS` 是**整体放开**的（`COMPLETED → PENDING_REVIEW` 是**合法边**，状态机拦不住），而 `apply_transition` 离开 `COMPLETED` 时会把 `completed_at` **静默清空**、没有任何东西会报错。
PENDING_REVIEW_REMOTE_TOKENS = frozenset({"DYS", "待验收"})
#: 上家「池内形态」的档（`WFP` / 未分配 / 待抢单）——spec §7 主表写的是**（不映射）**：
#: 本线由**镜像行**表达、⛔ 不回拉；它同时兜住 §5.3 / D-f 的「池内两态不被上游推成代练中」。
POOL_REMOTE_TOKENS = frozenset({"WFP", "未分配", "待抢单"})
#: 上家「已退单」那一档（码 + 中文）——它走 `follow_refund` 那条路（A11 / §15.3），
#: ⛔ 不走普通的状态写回（那一档要**钱一起回退**，走既有退单原语）。
REFUNDED_REMOTE_TOKENS = frozenset({"YTD", "已退单"})
#: spec §5.4 的**远端那一半**终态：上家 ∈ {待验收, 已完成, 已退单}（码与中文都收）。
TERMINAL_REMOTE_TOKENS = frozenset({"DYS", "待验收", "YWC", "已完成", "YTD", "已退单"})

#: ⭐ **本线收窄后的状态码表**（spec §7 主表 + 改准块 1）——「`REMOTE_STATUS_MAP` / `REMOTE_STATUS_LABELS` 的本线收窄版」：在 `jidanpu_member` 的原表上盖两处（`待验收` / 池内形态）。
#: ⚠️ `None` 是**有意义的取值**（≠「键不在」）：`map_remote_status` 必须能区分「这一档本线明确**不映射**」与「认不出来的码」（后者落兜底 = 问题单）。
#: ⚠️ ⛔ **不另抄一整张表**：底表仍从 `jidanpu_member` 取（唯一出处，两处各自漂移必然分叉）。
REMOTE_STATUS_MAP: dict[str, str | None] = {
    **jidanpu_member.REMOTE_STATUS_MAP,
    "DYS": None,
    "WFP": None,
}
REMOTE_STATUS_LABELS: dict[str, str | None] = {
    **jidanpu_member.REMOTE_STATUS_LABELS,
    "待验收": None,
    "未分配": None,
    "待抢单": None,
}

#: 内容回拉的**列清单（枚举）**：`(我方列, 上游键)`，与 `record_assigned_order` 的投影同一组。
#: ⚠️ 区服在我方是 `server` **FK**（没有文本列）⇒ 本线不回拉区服文本；金额列**一个都不在**这里
#: （spec §5.3 末行：金额一律不回写，钱只在我方建单那一次落）。
CONTENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("project_text", "project"),
    ("content", "orderContent"),
    ("remark", "orderRemark"),
    ("game_role_name", "roleName"),
    ("game_account", "gameAcc"),
    ("game_sub_account", "zgameAcc"),
)

#: 跟退成功的**审计行**人话（spec §15.3 第 ④ 步 (a)：复用既有动作码 `order.transition` 的 `reason`）。
_FOLLOW_REFUND_BODY = "把这一单退了（已退单）⇒ 我方按既有退单原语自动跟着退（含全额退款与绩效冲减）"
FOLLOW_REFUND_REASON = "上家" + _FOLLOW_REFUND_BODY
#: 跟退成功的**台账**人话（spec §15.3 第 ④ 步 (b)：括号里要点名**上家账号显示名**）。显示名取 `JidanpuUpstreamBinding.account_name`（那一列的 `verbose_name` 就是「上家账号显示名」）。
#: ⛔ **空白时⛔ 不回落 `binding.account`**——那一列是**登录账号**（凭据面，`test_the_refund_notes_never_carry_credentials` 逐字禁它出现在台账 / 审计文本里）；没有显示名就照审计那一句（见 `_follow_refund_note_reason`）。
FOLLOW_REFUND_NOTE_REASON = "上家（{name}）" + _FOLLOW_REFUND_BODY
#: 幂等出口：我方已经是 `REFUNDED`。
ALREADY_REFUNDED_REASON = "我方这一单已经是「已退单」⇒ 跟退幂等：状态与钱都一个字不动"
#: **已结账挡住**（✅ T19 已定）：与既有 D-0156 同一口径；⛔ 本线不自己合、⛔ 不私自放宽。
SETTLED_KEEP_REASON = (
    "我方这一单已完成且该单 MAIN 绩效行已结账 ⇒ 按 D-0156 挡住、不自动退；"
    "上家已退单，两边不一致，请人工对账（要真正做到已结账也回退需另开一条线，spec R21）"
)

#: 「写入面只开一格」的几句人话（spec §5.3）。
STATUS_NOT_IN_PROGRESS_REASON = (
    "我方这一单的状态是「{ours}」，不是「代练中」⇒ 写入面只开一格：上家不能推进这张单的状态，只登记"
)
STATUS_UNMAPPED_REASON = "上家这一档在本线不映射（待验收 / 池内形态）⇒ 只登记，不改我方状态"
STATUS_POOL_UNMAPPED_REASON = (
    "上家这一档（{token}）是它的池内形态，本线由镜像行表达 ⇒ ⛔ 不回拉，只登记"
)
STATUS_ALREADY_REASON = "上家状态映射为「{target}」，与我方当前状态一致 ⇒ 不写（幂等）"
STATUS_ILLEGAL_EDGE_REASON = (
    "上家状态映射为「{target}」，但从我方「{ours}」走不到它（状态机不许）⇒ 只登记，请人工核对"
)
#: `DYS` 的两档：有提交记录 = 我们自己的回音；没有 = 异常 / 别人操作过（spec §7 改准块 1）。
DYS_ECHO_REASON = (
    "上家这一单是「待验收」，正是我方自己提交完成之后的回音 ⇒ ⛔ 一个字不改"
    "（照旧表回拉会把已完成单改回待验收并静默清空完单时间）"
)
DYS_UNEXPECTED_REASON = (
    "上家这一单是「待验收」，但我方没有提交过完成（异常 / 别人操作过）⇒ 不改状态，只登记，请人工核对"
)

#: 终态 / 读不到 / 写回 / 被拒的几句人话。
TERMINATED_REASON = "两边都到终态（我方「已完成」+ 上家「{token}」）⇒ 停止跟踪这一单，往后不为它打上游读"
REMOTE_ROW_MISSING_REASON = (
    "本轮「我的订单」窗口里没有这一行、且按单号（`key1`）补核也回 0 行 ⇒ 一律当「读不到」，"
    "⛔ 不判远端没有这张单（读不到 ≠ 不存在）"
)
REMOTE_STATUS_MISSING_REASON = "上家这一单没有可读的状态（orderSta 为空）⇒ 本轮不回拉"
REMOTE_STATUS_UNKNOWN_REASON = "上家返回了对不上的状态「{token}」⇒ 按约定归为问题单（⛔ 不猜）"
REMOTE_STATUS_APPLIED_REASON = "上家状态「{token}」与我方不一致，已回写为「{target}」"
STATUS_DENIED_REASON = "状态回拉被状态机拒绝（多半是该账号缺节点）⇒ 只登记，不改状态：{detail}"
REFUND_DENIED_REASON = "跟退被状态机 / 领域层拒绝（多半是该账号缺节点）⇒ 状态与钱都没动：{detail}"

#: 内容回拉的两句人话（⛔ 只点名**我方列**，⛔ 不回显上游自由文本）。
CONTENT_FILLED_REASON = "上家补上了我方为空的这几格：{fields}"
CONTENT_KEEP_REASON = "上家这几格与我方不同、但我方已有值 ⇒ ⛔ 不覆盖（只补我方为空的字段）：{fields}"


def _note_order(*, binding, order, reason: str, status: str, kind: str = KIND_STATUS_IN) -> bool:
    """状态 / 内容回拉的台账写口：`note_once` + **归属绑定**（spec §4.4 的 payload 要求）。
    ⚠️ 幂等键仍是 `note_once` 的四元组 `(kind, target_type, target_ref, reason)`，`target_ref` 取**我方订单号**（租户内唯一；一张单挂一条链路是本线的常规形态）——⛔ **不**照 `_note_record_skipped` 的 `f"{binding.pk}|…"` 形状：那一路的键是「绑定 + 上家单号」（那一档**没有我方订单**可指认，评审修复只针对它）；两种对象的幂等口径不同，照抄会让台账的 `target_ref` 不再是订单号、消费方（订单页 / 总览）按订单号查不到它。
    ⚠️ `payload` 里⛔ 不许放凭据（spec §8.3 / GC 3）。"""
    return note_once(
        order=order, kind=kind, status=status, reason=reason,
        payload={"upstream_binding_id": binding.pk},
    )


def _token_of(value) -> str:
    """上游状态格的归一化（`None` ⇒ 空串；其余 `strip`）。判据一律用空串 / `is None`。"""
    return "" if value is None else str(value).strip()


def map_remote_status(token) -> str | None:
    """上家 `orderSta` → 我方 `OrderStatus`；**本线的收窄版**（spec §7 主表 + 改准块 1）。`待验收` / `DYS` ⇒ **`None`**：它是我方**自己提交完成之后的回音**，⛔ 不改状态；`WFP` / 未分配 / 待抢单 ⇒ **`None`**：spec §7 主表写的就是**（不映射）**，本线由镜像行表达；其余 ⇒ 走本线的 `REMOTE_STATUS_MAP` / `REMOTE_STATUS_LABELS`（底表 = `jidanpu_member` 的原表，**唯一出处**）；认不出来的**非空** token 落**兜底 = 问题单**（spec §7 末行），调用方还要留一条人话台账。
    ⚠️ `已退单` 这里仍映射成 `REFUNDED`：它**不是**「写回」——调用方按 `REFUNDED_REMOTE_TOKENS` 把它路由到 `follow_refund`（钱一起回退，spec §15.3.1）。"""
    token = _token_of(token)
    if not token:
        return None
    if token in REMOTE_STATUS_MAP:
        return REMOTE_STATUS_MAP[token]
    if token in REMOTE_STATUS_LABELS:
        return REMOTE_STATUS_LABELS[token]
    return jidanpu_member.UNKNOWN_REMOTE_STATUS


def should_write_status(order, *, mapped) -> tuple[bool, str]:
    """「写入面只开一格」的判据（spec §5.3）：回 `(写不写, 人话原因)`。真 = **恰好**同时满足三条：① `mapped` 不是 `None`（待验收 / 池内形态一律不写）；② 我方当前状态**就是**「代练中」（`IN_PROGRESS`）——这是**唯一**开着的那一格；③ 这条边在状态机里**存在**（`can_transition(ours, target)`）。其余情况一律 `(False, 人话)` ⇒ 调用方**只登记**。
    ⚠️ **为什么必须有 ②**（读码，⛔ 不是「状态机会拦住」）：`order_status.TRANSITIONS` 是**整体放开**的「任意态 → 任意态」，`COMPLETED → PENDING_REVIEW` 是**合法边** ⇒ 状态机**拦不住**回退；而 `apply_transition` 离开 `COMPLETED` 时会把 `completed_at` 清空 ⇒ 放它写就是「已完成单被改成待验收 + **完单时间静默消失**」，没有任何东西会报错。
    ⚠️ **为什么必须有 ③**：入 `COMPLETED` 的边只认「待验收」⇒ 我方还在代练中时上家报 `YWC`（已完成），这条边**不存在** ⇒ 走「不写 + 人话台账，请人工核对」（spec §7 那一格）。⚠️ ③ 用的是 `can_transition(..., nodes=None)` 的**边存在性**语义（⛔ 不是授权判断）：授权在 `apply_transition` 里按调用方给的节点集判——两处各判一套必然漂移（E-0021 同族）。"""
    if mapped is None:
        return False, STATUS_UNMAPPED_REASON
    ours = str(order.status)
    target = str(mapped)
    if ours == target:
        return False, STATUS_ALREADY_REASON.format(target=status_text(target))
    if ours != str(OrderStatus.IN_PROGRESS):
        return False, STATUS_NOT_IN_PROGRESS_REASON.format(ours=status_text(ours))
    if not can_transition(ours, target):
        return False, STATUS_ILLEGAL_EDGE_REASON.format(
            target=status_text(target), ours=status_text(ours)
        )
    return True, ""


def _unmapped_reason(token: str, *, link) -> str:
    """`mapped is None` 那一档的**细分人话**（`should_write_status` 拿不到链路的提交记录）。"""
    if token in PENDING_REVIEW_REMOTE_TOKENS:
        #: ⭐ spec §7 改准块 1：**已有**提交记录 ⇒ 预期回音；**没有**提交记录 ⇒ 异常，等人看。
        return DYS_ECHO_REASON if link.completed_submitted_at is not None else DYS_UNEXPECTED_REASON
    if token in POOL_REMOTE_TOKENS:
        return STATUS_POOL_UNMAPPED_REASON.format(token=token)
    return STATUS_UNMAPPED_REASON


def is_terminated(order, remote_token) -> bool:
    """spec §5.4 的终态判据：我方 `COMPLETED` **且** 上家 ∈ {待验收, 已完成, 已退单}。
    ⚠️ 远端那一格**码与中文都收**（实测下发的是中文标签，spec §2.1；码表见 `docs/JIDANPU-API.md` §6.1）。
    ⚠️ 它只管**停止跟踪**；⛔ 不等于「不改状态」——`YTD` 那一档还要**跟退**（§15.3），由调用方先路由，别用本函数短路掉它。"""
    if str(order.status) != str(OrderStatus.COMPLETED):
        return False
    return _token_of(remote_token) in TERMINAL_REMOTE_TOKENS


def _mark_terminated(link, *, now) -> None:
    """写链路 `terminated_at`（**条件更新**：并发下只认第一次，⛔ 不覆盖已写上的时刻）。"""
    updated = JidanpuUpstreamOrderLink.objects.filter(
        pk=link.pk, terminated_at__isnull=True
    ).update(terminated_at=now)
    if updated:
        link.terminated_at = now


def _is_pullable(link) -> bool:
    """这一条链路这一轮**在不在回拉的扫描面里**（spec §5.3）。不在 = ① 已停跟踪（`terminated_at` 非空）② 我方订单已软删 ③ 我方状态在 `PULL_EXCLUDED_STATUSES`（已退单）。
    ⚠️ **软删要单独判**：`link.order` 走的是 forward FK 描述符（`_base_manager`）⇒ **看得到**已删行。⛔ 别图省事用 `Order.objects` 去过滤——那条路会把「有链路但该跳过」与「没有链路」混成同一种形状（Task 5 的 `_link_for_by_remote` 已经按 `(binding, 单号)` 取过链路了）。
    ⚠️ **① 与 `_pullable_links` 的 DB 过滤是「冗余」关系**: 独立回拉那一支先 `filter(terminated_at__isnull=True)`、再逐个过本函数 ⇒ **只删**①（Python 这一道）会 SURVIVED（被 DB 过滤掩盖）；而 `sync_my_orders` 那一支按 `(binding, 单号)` 取链路、**只有**①这一道 ⇒ 两处都**不能删**，但也⛔ **别以为删任一道会有用例红**（两条路径各测一支时看不出来）。"""
    if link.terminated_at is not None:
        return False
    order = link.order
    if getattr(order, "deleted_at", None) is not None:
        return False
    return order.status not in PULL_EXCLUDED_STATUSES


def _locked_pull_link(binding, link):
    # Serialize writes with rebind and ensure the row still points to the same order.
    if not binding.current_identity_rows().filter(enabled=True).select_for_update().exists():
        return None
    return (
        JidanpuUpstreamOrderLink.objects.filter(
            pk=link.pk, binding=binding, order_id=link.order_id,
            upstream_order_id=link.upstream_order_id,
        )
        .select_related("order")
        .select_for_update(of=("self", "order"))
        .first()
    )


def _pullable_links(binding) -> list[JidanpuUpstreamOrderLink]:
    """该绑定名下**在回拉扫描面里**的链路（spec §5.3；具体判据见 `_is_pullable`）。⚠️ 这里的 `terminated_at__isnull=True` 与 `_is_pullable` 的①是**冗余**关系（见那边的注释）。"""
    links = (
        JidanpuUpstreamOrderLink.objects.filter(binding=binding, terminated_at__isnull=True)
        .select_related("order")
    )
    return [link for link in links if _is_pullable(link)]


def _pull_content(order, row, *, binding) -> tuple[str, ...]:
    """内容回拉：**只补我方为空的字段**（spec §5.3），回实际写了的列名（排序后）。
    - 上游值非空、我方该列为空 ⇒ 写（按**列宽**截断：超宽在真库上会被 PG 以 `value too long` 拒掉整笔）；我方已有值且与上游**不同** ⇒ ⛔ **不覆盖**（我方人工改过的值优先）+ 一条人话台账；
    - 金额：⛔ **一律不回写**（`CONTENT_FIELDS` 里根本没有金额列——钱只在我方建单那一次落）。"""
    updates: dict[str, str] = {}
    kept: list[str] = []
    for field_name, remote_key in CONTENT_FIELDS:
        remote = str(row.get(remote_key) or "")
        if not remote:
            continue
        current = getattr(order, field_name) or ""
        if not current:
            updates[field_name] = _clip_for_order(remote, field_name)
        elif current != remote:
            kept.append(field_name)
    if updates:
        with transaction.atomic():
            Order.objects.filter(pk=order.pk).update(**updates)
            for name, value in updates.items():
                setattr(order, name, value)
            _note_order(
                binding=binding, order=order, kind=KIND_CONTENT_IN, status="done",
                reason=CONTENT_FILLED_REASON.format(fields="、".join(sorted(updates))),
            )
    if kept:
        _note_order(
            binding=binding, order=order, kind=KIND_CONTENT_IN, status="skipped",
            reason=CONTENT_KEEP_REASON.format(fields="、".join(sorted(kept))),
        )
    return tuple(sorted(updates))


def _pull_claimed_order_pics(*, link, order, binding, client, now) -> None:
    """⭐ 认领单的**群码截图拉取**（D-0267 / E-0265；挂点 = `_pull_row` 内容回拉之后）。
    ## 触发条件（三条都满足才打上游）——① **已转正**：镜像行（按 `(绑定地址, 上家单号)` 唯一键反查）的 `promoted_order` 非空（H6 订正：判据挂在**镜像行**上——链路上没有这个列；没有进过大厅的指派单不在范围；S4：②式 openapi 绑定的写面是刻意另写的一份，`_pull_row` 只在①式轮跑 ⇒ 本条只覆盖①式）；② **尚无「本拉取归属」的成功记录**（H5 订正：判据是本 kind 的 **`done` 台账行** ⛔ 不是「订单名下有无任意截图行」——打手自己传的本地图会把群码**永久挡住**，恰打在要救的场景；行级幂等另由 `store_remote_pics` 的 `object_key` 判重兜底，同一张图第二轮不再存一行）；③ **自动尝试计数 < `PULL_PICS_MAX_ATTEMPTS`**（S3：成功 / 空 / 失败**每次都计数**，见 `KIND_PULL_PICS_IN`；到顶落一条**终态**台账（note_once 防复读）后停手；计数只数「自动那一轮的行」——`payload.trigger` 缺席的那些，打手手工请求**不吃**这份额度，见 `PULL_PICS_MAX_ATTEMPTS` 的注释）。
    ## 失败口径（照 member 线 `_pull_remote_screenshots` 逐字同构）——拉图的**任何**失败都**不挡状态回写**：本函数在 `_pull_row` 的状态写之前跑，预读 / 计数的 DB 查询异常由整轮的 fail-closed 兜底（响亮失败），进 `_pull_claimed_order_pics` 之后的上游 / 落库失败只记台账、从不向上抛（⛔ 不进 loud、不写绑定 `last_error`、不挡水位）；「部分成功（存 2 报 1）」算**成功轮**（S3：`pulled`/`existing` 非空即成功，失败张数进人话）；最外层 `except Exception` 是口径③的最后一道（兜我们自己代码的错 / 存储后端的未预期异常），异常类名进人话，⛔ 不静默；`uploaded_by` 照 member 线口径：`order.sales_user or binding.user`（S1）。"""
    mirror = JidanpuUpstreamHallOrder.objects.filter(
        upstream_base_url=binding.jidanpu_base_url, upstream_order_id=link.upstream_order_id,
    ).first()
    if mirror is None or mirror.promoted_order_id is None:
        return
    ref = order.order_no
    if JidanpuSyncEvent.objects.filter(
        kind=KIND_PULL_PICS_IN, target_type="order", target_ref=ref, status="done",
    ).exists():
        return
    attempts = JidanpuSyncEvent.objects.filter(
        kind=KIND_PULL_PICS_IN, target_type="order", target_ref=ref,
    ).exclude(
        # ⚠️ `has_key`（⛔ 不是 `exclude(payload__trigger=…)`）：后者的 SQL 是
        # `NOT (payload->>'trigger' = …)`，对**没有该键的行**求值为 NULL ⇒ 那些行会被
        # 一起排除掉，自动尝试的计数就永远停在 0、上限形同虚设。
        payload__has_key="trigger"
    ).count()
    if attempts >= PULL_PICS_MAX_ATTEMPTS:
        note_once(
            order=order, kind=KIND_PULL_PICS_IN, status="failed",
            reason=PULL_PICS_STOP_REASON.format(max=PULL_PICS_MAX_ATTEMPTS),
            payload={"upstream_binding_id": binding.pk},
        )
        return
    _pull_pics_once(
        link=link, order=order, binding=binding, client=client, now=now,
        attempt_no=attempts + 1,
    )


def _pull_pics_once(*, link, order, binding, client, now, attempt_no: int | None = None,
                    request_event: JidanpuSyncEvent | None = None) -> None:
    """真正拉一次图、并把结论落到台账（**自动轮与打手手工请求共用这一份**）。
    - `request_event` 为空 = 自动那一轮（`_pull_claimed_order_pics`）：结论**新落一行** `upstream.pull_pics_in`，人话带「第 x/5 次自动尝试」；
    - `request_event` 非空 = 打手在 App 里排的那条请求（`payload.trigger`）：结论**写回那条请求行**（`status` / `reason` / `processed_at`），⛔ 不另落一行、也不带自动计数。"""
    # 函数内 import：本模块与 `jidanpu_upstream_pics` 成环（它 import 本模块的端点常量），
    # 与 member 线 `_pull_remote_screenshots` 的同一句写法。
    from apps.business import jidanpu_upstream_pics

    manual = request_event is not None

    def allow_store() -> bool:
        current = _locked_pull_link(binding, link)
        return current is not None and _is_pullable(current) and (manual or not _is_stale_pull(current, now=now))

    def finish(status: str, reason: str) -> None:
        with transaction.atomic():
            if not allow_store():
                if manual:
                    _finish_pull_pics_request(
                        request_event, status="skipped", reason=PULL_PICS_REQUEST_GONE_REASON,
                    )
                return
            if manual:
                _finish_pull_pics_request(request_event, status=status, reason=reason)
            else:
                _record(
                    direction="in", kind=KIND_PULL_PICS_IN, status=status,
                    target_type="order", target_ref=order.order_no,
                    reason=reason, payload={"upstream_binding_id": binding.pk},
                )

    try:
        outcome = jidanpu_upstream_pics.pull_order_screenshots(
            client, order=order, remote_order_id=link.upstream_order_id,
            uploaded_by=order.sales_user or binding.user, now=now, allow_store=allow_store,
        )
    except Exception as exc:  # noqa: BLE001 —— 口径③最后一道：拉图失败不挡状态回写
        detail = type(exc).__name__
        if manual:
            finish("failed", PULL_PICS_MANUAL_FAILED_REASON.format(detail=detail))
        else:
            finish("failed", PULL_PICS_FAILED_REASON.format(
                detail=detail, attempt=attempt_no, max=PULL_PICS_MAX_ATTEMPTS,
            ))
        return
    if outcome.pulled or outcome.existing:
        extra = f"；另有 {len(outcome.failed)} 张没存上" if outcome.failed else ""
        reason = PULL_PICS_DONE_REASON.format(
            pulled=len(outcome.pulled), existing=len(outcome.existing), extra=extra,
        )
        finish("done", reason)
        return
    if outcome.reason:
        if manual:
            finish("failed", PULL_PICS_MANUAL_FAILED_REASON.format(detail=outcome.reason))
        else:
            finish("failed", PULL_PICS_FAILED_REASON.format(
                detail=outcome.reason, attempt=attempt_no, max=PULL_PICS_MAX_ATTEMPTS,
            ))
        return
    if manual:
        finish("skipped", PULL_PICS_MANUAL_EMPTY_REASON)
        return
    finish("skipped", PULL_PICS_EMPTY_REASON.format(
        attempt=attempt_no, max=PULL_PICS_MAX_ATTEMPTS,
    ))


def _finish_pull_pics_request(event: JidanpuSyncEvent, *, status: str, reason: str) -> None:
    """把一条手工请求行落成结论（`pending` → `done` / `failed` / `skipped`）。
    ⚠️ 用 `.update()` 收窄写：请求行上还有别的执行者可能在看（`run_pull_pics_requests` 每拍扫 `pending`）⇒ 整行 `save()` 会把并发写进去的列覆盖回旧值。"""
    clipped = reason[:255]
    processed_at = timezone.now()
    updated = JidanpuSyncEvent.objects.filter(pk=event.pk, status="pending").update(
        status=status, reason=clipped, processed_at=processed_at,
    )
    if not updated:
        return
    event.status = status
    event.reason = clipped
    event.processed_at = processed_at


@transaction.atomic
def queue_pull_pics_request(*, order, link, requested_by, now=None):
    """打手在 App 里点开详情时**排一条手工拉图请求**。回 `(事件行, 冷却剩余秒数)`：`0` = 这次真的排了一条新的；`> 0` = 冷却内命中上一次请求（**原样回那条行**，⛔ 不重复入队）。
    ⚠️ **冷却判据在数据库里**（最近一条本标记请求行的 `created_at` + `PULL_PICS_REQUEST_COOLDOWN_SECONDS`）：进程内缓存（`cache.add`）在多 worker / 与 `jidanpu_sync` 不同进程的形态下**各算各的**（同一单会被几个 worker 各排一条）⇒ 选数据库，判据只有一处、跨进程一致。
    ⚠️ 只写**一行台账**（`pending`）：⛔ 一次上游都不打、⛔ 不动订单 / 截图。"""
    if _locked_pull_link(link.binding, link) is None:
        return None, 0
    now = now or timezone.now()
    window_start = now - timedelta(seconds=PULL_PICS_REQUEST_COOLDOWN_SECONDS)
    recent = (
        JidanpuSyncEvent.objects.filter(
            direction="in", kind=KIND_PULL_PICS_IN, target_type="order",
            target_ref=order.order_no,
            payload__trigger=PULL_PICS_TRIGGER_APP,
            created_at__gte=window_start,
        )
        .order_by("-created_at")
        .first()
    )
    if recent is not None:
        left = (
            recent.created_at
            + timedelta(seconds=PULL_PICS_REQUEST_COOLDOWN_SECONDS)
            - now
        ).total_seconds()
        # 向上取整到秒：`left=0.4` 也要回 1（回 0 会被调用方读成「这次排上了」）。
        return recent, max(1, int(left) + 1)
    event = _record(
        direction="in", kind=KIND_PULL_PICS_IN, status="pending",
        target_type="order", target_ref=order.order_no,
        reason=PULL_PICS_REQUEST_REASON,
        payload={
            "upstream_binding_id": link.binding_id,
            "trigger": PULL_PICS_TRIGGER_APP,
            "requested_by_user_id": requested_by.pk,
        },
    )
    return event, 0


def has_pending_pull_pics_requests(binding) -> bool:
    """该绑定名下有没有**待消费**的手工拉图请求（`jidanpu_sync` 用它省掉建连）。"""
    return JidanpuSyncEvent.objects.filter(
        direction="in", kind=KIND_PULL_PICS_IN, status="pending",
        payload__trigger=PULL_PICS_TRIGGER_APP,
        payload__upstream_binding_id=binding.pk,
    ).exists()


def run_pull_pics_requests(binding, *, now, client) -> int:
    """消费该绑定的手工拉图请求；回处理条数（`jidanpu_sync` 每一拍调一次）。
    ⚠️ 拉图**仍然只走** `jidanpu_upstream_pics.pull_order_screenshots`（经 `_pull_pics_once`）——⛔ 这不是第二条拉图实现，只是「谁来触发」多了一个入口。⚠️ 执行前**重判两件现场**（请求排下之后可能已经变了）：① 链路还在不在回拉扫描面（`_is_pullable`：停跟踪 / 软删 / 已退单 ⇒ 不拉）；② 本地**已有**群二维码 ⇒ 不重复打上游（`PULL_PICS_REQUEST_ALREADY_REASON`）。
    ⚠️ 拉图失败**只落台账、绝不向上抛**（与自动那一轮口径③同源）：一条坏单不许拖垮本轮其它请求，也不该让整条绑定进退避。⚠️ 请求行自己就是结论出口（`_finish_pull_pics_request`）⇒ 页面 / 台账都看得到「拉了没有、为什么没拉」。"""
    events = list(
        JidanpuSyncEvent.objects.filter(
            direction="in", kind=KIND_PULL_PICS_IN, status="pending",
            payload__trigger=PULL_PICS_TRIGGER_APP,
            payload__upstream_binding_id=binding.pk,
        ).order_by("created_at", "id")
    )
    handled = 0
    for event in events:
        if not binding.current_identity_rows().filter(enabled=True).exists():
            break
        handled += 1
        link = (
            JidanpuUpstreamOrderLink.objects.filter(
                binding=binding, order__order_no=event.target_ref,
            )
            .select_related("order")
            .first()
        )
        if link is None or not _is_pullable(link):
            _finish_pull_pics_request(
                event, status="skipped", reason=PULL_PICS_REQUEST_GONE_REASON,
            )
            continue
        order = link.order
        if OrderScreenshot.objects.filter(
            order=order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        ).exists():
            _finish_pull_pics_request(
                event, status="skipped", reason=PULL_PICS_REQUEST_ALREADY_REASON,
            )
            continue
        _pull_pics_once(
            link=link, order=order, binding=binding, client=client, now=now,
            request_event=event,
        )
    return handled


@dataclass(frozen=True)
class FollowOutcome:
    """一次跟退的结论（`follow_refund` 的返回值）。
    - `changed=True` ⇒ 走了既有退单原语（状态 → 已退单，**钱一起回退**）；`changed=False` ⇒ 没动：`reason` 是人话（幂等 / 已结账挡住）。
    ⚠️ 「被状态机拒绝」那一档**不在这里**（它是异常，由调用方按 `REFUND_DENIED_REASON` 记账）：本类只装「按口径决定**不做**」的两种。"""

    changed: bool
    reason: str


def follow_refund(order, *, now, actor=MACHINE_ACTOR, binding=None) -> FollowOutcome:
    """上家退单 ⇒ 我方跟退（A11 / spec §15.3）。⛔ **不新造钱路径**：只调**既有**退单原语（`apply_transition(→ REFUNDED)`）⇒ 绩效侧由它按既有口径一起回退（✅ T17 已定：「跟着退，并且钱一起回退」= 打手绩效清零 + 客服提成目标式冲减到 0，spec §15.3.1）；⚠️ **不是**「只改状态、不动钱」——业务方明确否掉了那种做法，⛔ 别改回去。⚠️ **口径（D-0181 改准）**：退单（含上家跟退）不再建 `OrderRefund(kind=FULL)` 事件行、不写 `refund_fee`，绩效侧「打手绩效清零 + 客服提成目标式冲减到 0」**照旧**；本函数**一行代码都没改**（只调 `apply_transition`）⇒ 口径变更落在 `refunds.on_order_refunded` 那一侧。
    ⚠️ **已结账挡住**（✅ T19 已定）：闸复用既有 `jidanpu_common.status_rollback_blocked`（D-0156 的**同一把尺子**，⛔ 不复制第二份），⭐ **D-0287 改准**：现在只挡「已结账 **且** `settled_by != "system"`」——钱包成为发薪唯一出口之后**每一张完成单**都会被自动标「已结账」（`settled_by="system"`），旧判据让**每一笔上家退单都被挡住**、跟退**事实上停机**（钱留在打手钱包里）；今天系统自动发薪的行**照常跟退**：状态照样迁 `REFUNDED`，钱由 `refunds.on_order_refunded` 的第 1.6 步回冲（钱包扣回 + 结账四列复位），⛔ **财务手工结账的行仍挡住**——那些钱从来没进过钱包，扣了就是凭空多扣（规格 §3）。⚠️ 脏行（`settled_by="system"`、`settled_amount > 0` 而 `settled_at` 为空）会在回冲幂等键的格式化处**响亮报错**（`TypeError`）⇒ 整笔回滚，⛔ **不静默**回一句「请人工」（`D-0287` ③）。⚠️ **幂等**：我方已经是 `REFUNDED` ⇒ `ALREADY_REFUNDED_REASON`（`REFUNDED` 也没有自环边）。
    ⚠️ **节点集**：机器驱动的写入，按反方向线（`jidanpu_member._pull_statuses`）的既有做法取**绑定客服**的节点集——给了 `binding` 就用 `nodes_of(binding.user)`，没给才回落该单客服（`order.sales_user`）；⛔ **不接受请求来源**。⚠️ **`binding` 的默认 `None` 只是「同族形状」**（`jidanpu_member._pull_statuses` 那一族签名都有它）：唯一的生产调用点 `_follow_refund_row` 永远显式传它，⛔ 别在别处省略；省略时回落 `order.sales_user` 是 **fail-closed** 的：该单没有客服 ⇒ `nodes_of(None)` 是**空集** ⇒ `apply_transition` 会以「缺节点」**响亮拒绝**（`_follow_refund_row` 记一条 failed 台账），⛔ **不会静默放行**。⚠️ **`now`**：与同族入口同形（一轮一个时刻）；跟退本身不落任何时刻列——账的时刻由既有审计 / 台账行自己的 `auto_now_add` 表达。⚠️ **必须在事务里**：`apply_transition` 的复读用 `select_for_update`（autocommit 下会响亮抛 `TransactionManagementError`）；审计与状态写在**同一个** `atomic()` 里（⛔ 不留「状态变了、账没记」的半截，E-0022 的纪律）。"""
    if str(order.status) == str(OrderStatus.REFUNDED):
        return FollowOutcome(False, ALREADY_REFUNDED_REASON)
    main = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if jidanpu_common.status_rollback_blocked(order, main):
        #: ✅ T19 已定：已结账的已完成 ⇒ 挡住（D-0156 同一口径）；调用方只登记 + 提醒人工。
        return FollowOutcome(False, SETTLED_KEEP_REASON)
    nodes = nodes_of(binding.user if binding is not None else order.sales_user)
    from_status = order.status
    with transaction.atomic():
        apply_transition(
            order, OrderStatus.REFUNDED, nodes=nodes, actor=actor,
            suppress_outbound_sync=True, notice_reason=FOLLOW_REFUND_REASON,
        )
        #: **两层留痕的第 (a) 层**（spec §15.3 第 ④ 步）：复用既有动作码 `order.transition`
        #: ——**这**就是一次订单状态迁移，⛔ 不新造一个只有机器会写的动作（与
        #: `jidanpu_member._pull_statuses` 的同一句注释口径一致）。`actor_id` 留空（不是真人点的）。
        record(
            log_type=ACTION_LOG_TYPES["order.transition"],
            action="order.transition",
            actor=actor, actor_id=None,
            # ⭐ 上家退回驱动的机器来源 ⇒ 显式声明（见 `log.py`）。
            system=True,
            target_kind="order", target_id=order.order_no, order_no=order.order_no,
            field_name="status",
            from_value=status_text(from_status), to_value=status_text(OrderStatus.REFUNDED),
            reason=FOLLOW_REFUND_REASON,
        )
    return FollowOutcome(True, FOLLOW_REFUND_REASON)


@dataclass(frozen=True)
class _RowPullOutcome:
    """一条链路的回拉结论（**内部**；⛔ 不是对外接口）。"""

    applied: bool = False   # 状态被写了（含跟退）
    followed: bool = False  # 真的跟上家一起退了（钱一起回退）
    #: 本轮这一行**产出了一条 `status="failed"` 的台账**（⛔ 与 `applied` 不互斥：「认不出的 token」那一档既留一条失败台账、又按兜底（问题单）继续走写回判据）。
    #: ⚠️ 它是「有没有失败」的**唯一**来源：`applied` 只回答「写了几行状态」，远端一行都没有时它是 0，答不出「这一轮其实失败了」（探针实测的静默）。
    failed: bool = False


def _follow_refund_note_reason(outcome: FollowOutcome, *, binding) -> str:
    """跟退台账的人话（spec §15.3 第 ④ 步 (b)）：有**上家账号显示名**就括号点名，否则照审计那一句。
    ⚠️ ⛔ **不回落登录账号**（`binding.account` 是凭据面，见 `FOLLOW_REFUND_NOTE_REASON` 的注释）；`outcome.changed=False` 的两档（幂等 / 已结账挡住）**照原句**——它们不是「跟着退」。"""
    if not outcome.changed:
        return outcome.reason
    name = (getattr(binding, "account_name", "") or "").strip()
    return FOLLOW_REFUND_NOTE_REASON.format(name=name) if name else outcome.reason


def _follow_refund_row(link, *, binding, now, terminated: bool) -> _RowPullOutcome:
    """`YTD` 那一档的落点（`follow_refund` + 台账 + 终态）。"""
    order = link.order
    try:
        outcome = follow_refund(order, now=now, binding=binding)
    except IllegalTransition as exc:
        detail = str(exc)
        # ⚠️ 结账挡住那一档走**专用人话**（业务方要求「有钱的单只标记、不改状态、日志写清楚」）；其余拒绝（缺节点等）仍用既有的「被状态机拒绝」那句。
        #    ⚠️ 这一支的人话用 `SETTLED_KEEP_REASON`——与 `follow_refund` 预检那一档（`status_rollback_blocked` 命中）同一句、同一口径。
        #    可达路径实证：`apply_transition` 的 `settled_status_change_denial` 抛的消息含「结账」（order_status.py）。
        reason = (
            SETTLED_KEEP_REASON
            if "结账" in detail
            else REFUND_DENIED_REASON.format(detail=detail)
        )
        _note_order(binding=binding, order=order, status="failed", reason=reason)
        return _RowPullOutcome(failed=True)
    _note_order(
        binding=binding, order=order,
        status="done" if outcome.changed else "skipped",
        reason=_follow_refund_note_reason(outcome, binding=binding),
    )
    if outcome.changed or terminated:
        _mark_terminated(link, now=now)
    return _RowPullOutcome(applied=outcome.changed, followed=outcome.changed)


def _pull_row(link, row, *, binding, client, now) -> _RowPullOutcome:
    """回拉**一行**（状态 + 内容 + 认领单截图；spec §5.3）。调用方保证这一行有链路且在扫描面里。顺序（每一步都有理由，⛔ 别调换）：① `orderSta` 空 ⇒ **判不了** ⇒ 只留一条「读不到」台账（⛔ 不猜）；② **内容回拉**（与状态写不写无关：spec §5.3 的「只补空格」对我方已完成单同样适用）；②b ⭐ **认领单的群码截图拉取**（D-0267 / E-0265：已转正（镜像行 `promoted_order` 非空）+ 尚无本拉取归属的成功记录 + 尝试计数未到顶 ⇒ 拉，见 `_pull_claimed_order_pics`；**任何失败都不挡后面的状态回写**（口径③），且指派单（无镜像行）在此零开销——一次镜像查询就返回）；③ `YTD` ⇒ `follow_refund`——**先于**终态判定：已结账挡住那一档也要留「提醒人工」那条台账，而终态判定会提前 `return`（那样就把它吞了）；④ 认不出来的 token ⇒ 先留一条人话台账（spec §7 末行）；
    ⑤ `mapped is None`（待验收 / 池内形态）⇒ **只登记**；⚠️ 这一支要在**终态台账之前**判——「我方已完成 + 上家待验收」既是终态又是 `DYS` 收窄档，DYS 那条「预期回音 / 异常」的人话更具体，⛔ 不能被一句笼统的「停止跟踪」顶掉；随即照 §5.4 写 `terminated_at`；⑥ 两边都终态（非 DYS / 池内档）⇒ 写 `terminated_at` + 台账（spec §5.4）；⑦ `should_write_status`（**写入面只开一格**）⇒ 不写就**只登记**；
    ⑧ 写：`apply_transition(..., suppress_outbound_sync=True)`（来源闸 B7：⛔ 不对外回声）+ 既有动作码 `order.transition` 的审计（actor = 机器名、`actor_id` 留空、前后值 = 状态中文 label）。⚠️ `client`（D-0267 加法）：截图拉取与状态回拉**同一个**绑定凭据的客户端（`_mark_gone` 也在用它打上游）——三个调用点（`sync_my_orders` / `_check_missing_links` / `pull_statuses`）的作用域里都有它，逐字透传。"""
    with transaction.atomic():
        current = _locked_pull_link(binding, link)
        if current is None or _is_stale_pull(current, now=now) or not _is_pullable(current):
            return _RowPullOutcome()
        order = current.order
        _touch_link(current, now=now)
        _reset_miss(current)
        token = _token_of(row.get("orderSta"))
        if not token:
            _note_order(
                binding=binding, order=order, status="failed",
                reason=REMOTE_STATUS_MISSING_REASON,
            )
            return _RowPullOutcome(failed=True)
        _pull_content(order, row, binding=binding)

    _pull_claimed_order_pics(link=current, order=order, binding=binding, client=client, now=now)
    with transaction.atomic():
        refreshed = _locked_pull_link(binding, current)
        if refreshed is None or _is_stale_pull(refreshed, now=now) or not _is_pullable(refreshed):
            return _RowPullOutcome()
        return _apply_pulled_status(refreshed, token, binding=binding, now=now)


def _apply_pulled_status(link, token, *, binding, now) -> _RowPullOutcome:
    order = link.order
    terminated = is_terminated(order, token)
    if token in REFUNDED_REMOTE_TOKENS:
        return _follow_refund_row(link, binding=binding, now=now, terminated=terminated)

    unreadable = not jidanpu_member.is_known_remote_status(token)
    if unreadable:
        #: 兜底那一档**不许静默**（spec §7 末行）：先响亮留痕，再按兜底（问题单）走下面的写回判据。
        #: ⚠️ 它同时是**本轮的一条失败**（`unreadable=True`）：本轮确实产出了一条 `status="failed"` 的台账 ⇒ 「立即同步」要据此退避 + 把失败数回给页面（⛔ 别指望 `applied`——那一格答的是「写了几行状态」）。
        _note_order(
            binding=binding, order=order, status="failed",
            reason=REMOTE_STATUS_UNKNOWN_REASON.format(token=token),
        )

    mapped = map_remote_status(token)
    if mapped is None:
        _note_order(
            binding=binding, order=order, status="skipped",
            reason=_unmapped_reason(token, link=link),
        )
        if terminated:
            #: 这一档也照 §5.4 停跟踪（「我方已完成 + 上家待验收」正是两边都终态）。
            _mark_terminated(link, now=now)
        return _RowPullOutcome(failed=unreadable)
    if terminated:
        _mark_terminated(link, now=now)
        _note_order(
            binding=binding, order=order, status="done",
            reason=TERMINATED_REASON.format(token=token),
        )
        return _RowPullOutcome(failed=unreadable)

    write, reason = should_write_status(order, mapped=mapped)
    if not write:
        _note_order(binding=binding, order=order, status="skipped", reason=reason)
        return _RowPullOutcome(failed=unreadable)

    from_status = order.status
    denial: str | None = None
    with transaction.atomic():
        try:
            apply_transition(
                order, mapped, nodes=nodes_of(binding.user), actor=MACHINE_ACTOR,
                suppress_outbound_sync=True,
            )
        except IllegalTransition as exc:
            #: 可预期拒绝（边 / 节点 / 陈旧状态）⇒ 记账，**不中断本轮别的行**。
            #: ⚠️ E-0022：预期失败要 `set_rollback(True)` 再退出，⛔ 不能裸 `return`
            #: （那会把块内前面的写入提交成一笔半截账）。
            transaction.set_rollback(True)
            denial = str(exc)
        else:
            record(
                log_type=ACTION_LOG_TYPES["order.transition"],
                action="order.transition",
                actor=MACHINE_ACTOR, actor_id=None,
                # ⭐ 机器来源 ⇒ 显式声明。
                system=True,
                target_kind="order", target_id=order.order_no, order_no=order.order_no,
                field_name="status",
                from_value=status_text(from_status), to_value=status_text(mapped),
            )
    if denial is not None:
        _note_order(
            binding=binding, order=order, status="failed",
            reason=STATUS_DENIED_REASON.format(detail=denial),
        )
        return _RowPullOutcome(failed=True)
    _note_order(
        binding=binding, order=order, status="done",
        reason=REMOTE_STATUS_APPLIED_REASON.format(token=token, target=status_text(mapped)),
    )
    return _RowPullOutcome(applied=True, failed=unreadable)


@dataclass(frozen=True)
class PullRoundOutcome:
    """一轮**独立**状态 / 内容回拉的汇总（`pull_statuses` 的返回值）。`applied` = 本轮**写了状态**的行数（跟退也算写了状态）；`failed` = 本轮**产出了失败台账**的行数（远端没有这一行 / `orderSta` 为空 / 认不出的 token / 状态机拒绝 / 跟退被拒）。
    ⚠️ **为什么要有 `failed`**（探针实测）：本线「立即同步」**不写健康列**（已裁定口径）⇒ 视图只能从**本轮结果**判「这一轮失败了没有」。只有 `applied` 时，「远端一行都没有」这种**不抛异常的预期失败**回的是 `code 0 + applied=0`：① 闸门的失败退避阶梯（5 / 15 / 60 秒）对这一类失败**从不生效**（只剩 2 秒最小间隔）；② 页面看到「成功但什么都没发生」。
    ⚠️ ⛔ 这两个数是**本轮**的读数，⛔ 不是「台账里有没有 failed 行」——`note_once` 按原因去重，同一句失败第二轮回**写不出新行**，拿台账判会让退避阶梯只生效一次。"""

    applied: int = 0
    failed: int = 0


def pull_statuses(*, binding, client, now, probe_clock=None) -> PullRoundOutcome:
    """跑一轮**独立**的状态 / 内容回拉，回本轮的 `PullRoundOutcome`（spec §5.3 / §5.4）。
    ⚠️ **调度那一轮的常规路径是 `sync_my_orders`**（**同一份**「我的订单」行顺带回拉，spec §5.3 的「同一轮」）；本函数是**独立一轮**的入口（手工补一轮 / 端点复用），它自己再拉一次「我的订单」，**只处理已有链路的行**——⛔ 不建单、⛔ 不碰指派单的门槛台账。⚠️ **取数与筛选与调度那一路逐字同源**（窗口拉取：①② `_my_orders_round_rows` 的并集 + ③ `_check_missing_links` 的按单补核）——⛔ 不在这里另写一份取数（那条路会跟调度漂移，症状是「页面上点得动、定时那一轮却不一样」这种**静默**的分叉）。⚠️ **没有可回拉的行 ⇒ 一次上游都不打**（返回全零）：这是「停跟踪」的判据（spec §5.4），判据要打在**桩的调用次数**上（⛔ 不是「没报错」）；停用的绑定 ⇒ 返回全零（spec §8.4）。
    ⚠️ ①② 里**没有某一行** ⇒ 一律当「读不到」（spec §5.3 末行），⛔ 不判「远端没有这张单」（老单上的真实变化不能因为这一轮没看见就丢掉，E-0004 同族）；⚠️ 它同时计进 `failed`（这是**不抛异常**的失败，调用方不能只看 `applied`）。⚠️ **这一档也吃 ③ 的每轮上限**：在途链路多过 `MY_ORDERS_LINK_CHECK_MAX` 时，超出上限的那些**这一轮不补核、也不写「读不到」台账**，轮到下一轮（轮转键见 `_links_due_for_check`）——与调度那一路同一条代价（计划 §3-2）。"""
    if not binding.enabled:
        return PullRoundOutcome()
    links = _pullable_links(binding)
    if not links:
        return PullRoundOutcome()
    rows = _my_orders_round_rows(client, now=now)
    if not binding.current_identity_rows().filter(enabled=True).exists():
        return PullRoundOutcome()
    by_id: dict[str, dict] = {}
    for row in rows:
        remote_id = str(row.get("orderId", "") or "")
        if remote_id:
            by_id.setdefault(remote_id, row)
    terminal_rows = {
        remote_id: token for remote_id, row in by_id.items()
        if (token := _token_of(row.get("orderSta"))) in TERMINAL_REMOTE_TOKENS
    }
    if terminal_rows:
        terminal_links = JidanpuUpstreamOrderLink.objects.filter(
            Q(last_in_at__isnull=True) | Q(last_in_at__lt=now),
            binding=binding, terminated_at__lte=now, upstream_order_id__in=terminal_rows,
        )
        for link in terminal_links:
            _refresh_terminal_observation(binding, link, token=terminal_rows[link.upstream_order_id], now=now)
    applied = 0
    failed = 0
    for link in links:
        row = by_id.get(link.upstream_order_id)
        if row is None:
            # ③ 那一支统一处理（含「按单补核」与 0 行时的「读不到」台账）。
            continue
        outcome = _pull_row(link, row, binding=binding, client=client, now=now)
        if outcome.applied:
            applied += 1
        if outcome.failed:
            failed += 1
    _hits, more_applied, more_failed, _followed = _check_missing_links(
        binding, client=client, now=now, seen_ids=set(by_id), probe_clock=probe_clock,
    )
    return PullRoundOutcome(applied=applied + more_applied, failed=failed + more_failed)


# ══ 我方 → 上家的两个写动作（A4 大厅接单 / A5 提交完成）══════════════════
# 两个动作**都走 outbox**（spec §4.4 / §6.4）：请求路径与触发点只写一行事件，上游调用一律由消费者在事务外做（`run_outbox`）。四条硬口径（⛔ 别顺手改）：① **A16（接单不额外推状态）**：大厅接单**只有** `RecOrderByInternal` 一次写——接单动作本身就让对方那张单变「代练中」（spec §2.3 实测）⇒ ⛔ 不再单独推一次 `SetOrderSta`，判据是桩的**调用集合**（⛔ 不是「没报错」）；② **A5 闸门**：提交完成前先过传图（`push_order_screenshots(...).blocks_completion`）——传图失败 ⇒ **一次 `SetOrderSta` 都不发**；③ **只有「已完成」才提交**（spec §6.2）：我方订单不是 `COMPLETED` ⇒ 一个字都不发上游；④ **G4 兜底**：提交完成**先回读**（上家已是「待验收 / 已完成」⇒ 幂等短路、只补 `completed_submitted_at`），提交后**再回读确认**（不确认 ⇒ `failed` + 退避，⛔ 不当成功）。
# 台账口径（spec §4.4）：`JidanpuSyncEvent` 一张表兼 outbox 与台账 ⇒ 两个动作的台账行**就是**那两行 `direction="out"` 的事件；`target_ref` 带**绑定归属**（`<binding_id>|<对象>`），`payload.upstream_binding_id` 是 `run_outbox` 的取活键（照各条拉取路的 `note_once` 语义：同一句原因不放大成 N 行——`queue_*` 挡**在飞**的同键行、收敛后不再重记）。⛔ 失败原因**不带凭据**（spec §8.3 / §11-18），并进 `binding.last_error`（传图链刻意留给这里收口）。⚠️ 执行租约**复用** `jidanpu_common` 的那套（`execution_lease` / `OUTBOX_LEASE_SECONDS`）：「请求路径与后台轮只有一个打上游」的判据只有那一份（实测结论），⛔ 本线不另写一处占位，租约的收尾由 `execution_lease` 的 `finally` 负责（⛔ 本模块不碰两列）。

#: 大厅接单（A4）：我方大厅有人接了镜像单 ⇒ 用上家账号在上家大厅把这单接下（spec §6.1）。
KIND_HALL_CLAIM = "upstream.hall_claim"
#: 提交完成（A5）：我方订单进「已完成」⇒ 上家 `SetOrderSta(sta=待验收)`（spec §6.2）。
KIND_COMPLETE = "upstream.complete"
#: ⭐ F1：**只作台账**的一条事实——事件的绑定与镜像行「当前 owner」不一致。
#: ⚠️ **不在 `OUTBOX_KINDS` 里**、`direction="in"`（`JidanpuSyncEvent` 一张表兼台账 / outbox，
#: spec §4.4）⇒ `run_outbox` 不会把它当待办取活（⛔ 别顺手加进 `OUTBOX_KINDS`）。
KIND_HALL_OWNER_MISMATCH = "upstream.hall_owner_mismatch"

#: 本消费者认的 kind 全集（`run_outbox` 取活时按它过滤）。
OUTBOX_KINDS = (KIND_HALL_CLAIM, KIND_COMPLETE)

# ── **只作台账**的三条（A12 / A14；⛔ 一次上游写都不打）─────────────────
#: 我方**退款**不同步上游（A12 / spec §15.4）：上家打手账号没有删单 / 退单权限
#: ⇒ ⛔ 不找端点、⛔ 不重试、⛔ 不进 outbox，只留一条人话。
KIND_REFUND_SKIPPED = "upstream.refund_skipped"
#: 我方**删除**（软删）不同步上游（A12 / spec §15.4）——与退款**同一句事由**、**不同一句人话**
#: （两件事各自可查；⚠️ 键里少了 `reason` 会让后发生的被前一件静默吞掉，见 `note_once`）。
KIND_DELETE_SKIPPED = "upstream.delete_skipped"
#: 我方**改问题单**不推上游（A14 / spec §15.6）：上家**全局禁用**了「问题单」（spec §2.3 实测）
#: ⇒ 只在我方内部生效，上家那边保持原状。
KIND_ISSUE_SKIPPED = "upstream.issue_skipped"

#: 上面三条的**字面**人话（spec §15.4 要求点明「上家账号无权限，未同步」；
#: §15.6 要求点明「我方问题单不推上游」）。⚠️ 只写结论、⛔ 不带任何凭据（GC 3）。
REFUND_NOT_SYNCED_REASON = (
    "我方退款不同步上游：上家账号无权限（打手账号没有删单 / 退单权限），未同步"
)
DELETE_NOT_SYNCED_REASON = (
    "我方删除不同步上游：上家账号无权限（打手账号没有删单 / 退单权限），未同步"
)
ISSUE_NOT_SYNCED_REASON = (
    "我方问题单不推上游：上家全局禁用了「问题单」，只在我方内部生效"
)

#: 失败退避：`min(2 ** attempts × 基准, 上限)`（照反方向线 `jidanpu_member` 的阶梯）。
OUTBOX_BACKOFF_BASE_SECONDS = 60
OUTBOX_BACKOFF_MAX_SECONDS = 3600
#: 到达这个尝试次数后**不再自动重试**、留台账待人看。
OUTBOX_MAX_ATTEMPTS = 5

#: 提交完成发的 `sta`：**中文标签逐字**「待验收」（spec §6.2 实测载荷）。
#: ⚠️ 取本模块自己的 `status_text`（= `OrderStatus(...).label`，那边注释里就是本线的**唯一出处**），
#: ⛔ 不写字面量、也⛔ 不再借 `jidanpu_member` 的标签表（那张表已随推送收窄被换掉）。
COMPLETION_STATUS_LABEL = status_text(OrderStatus.PENDING_REVIEW)

#: 上家「已经收到过提交完成」的状态（幂等短路的判据，spec §6.2 第 ② 步）：
#: 「待验收 / 已完成」——⛔ **不含**「已退单」（那另有跟退路径）。
SUBMITTED_REMOTE_TOKENS = PENDING_REVIEW_REMOTE_TOKENS | frozenset({"YWC", "已完成"})

#: 上家「这张单已经不在大厅」的文案里**可能**出现的词（保守分类，spec §6.1 的 ⑥ 那两句）。
#: ⚠️ **未实测**（spec §2.7 的 G4 只登记了「重复提交」未验）⇒ **认不出来一律按可重试**，
#: ⛔ 绝不因为猜不中就判「不在大厅」（误判 gone 会把一张真的挂在大厅的单丢掉）。
HALL_CLAIM_TAKEN_TOKENS: tuple[str, ...] = (
    "已被接走", "已被接取", "已被接手", "已被抢", "已接取", "不在大厅", "已被其他",
    # ⭐ 真机实测补两条（某租户线上 90 分钟内 13 次）：上家对「这张单已经被别的打手接走」回的原话是「该订单已被接或不在抢单中（上游码 0）」——它不含上面任何一条长词 ⇒ 被当成「认不出的拒绝」⇒ 打手看到「暂时无法接单」、镜像行永久卡在 claiming。
    #    ⚠️ 用**短词** `已被接` 覆盖「已被接走 / 已被接取 / 已被接或…」这一族；`不在抢单中` 单独列（它不含「大厅」两个字）。
    "已被接", "不在抢单中",
)

# ── 人话原因（⛔ 不放任何凭据 / 对象名；spec §6.2 / §6.3 / §8.3）────────────────────

HALL_CLAIM_DONE_REASON = "已在上家抢单大厅接单，并转正为我方订单"
HALL_CLAIM_ALREADY_REASON = "这一单已经在我方系统里（链路已有标记）⇒ 不重复接"
HALL_CLAIM_LINK_CONFLICT_REASON = "上家已确认接单，本地订单已另行处理，请核对接手人"
HALL_CLAIM_TAKEN_REASON = "上家那张单已经不在大厅了（已被接走）⇒ 不转正"
HALL_CLAIM_FAILED_REASON = "上家拒绝了这次大厅接单"
#: ⭐ F6：认不出「已被接走」那一档的**完整**人话。这一档只能保守按**可重试**处理（⛔ 不误判 `gone`：误判会把一张真的挂在大厅的单丢掉）。
#: ⭐ **D-0335（清道夫在役）**：本理由现在**只属 outbox 消费路**（请求路认不出的拒绝已改为当场放回，见 `HALL_CLAIM_RELEASED_TO_HALL_REASON`）；outbox 烧完额度后，`sync_hall` 的清道夫（`_hall_claim_event_settled` + 放回支）会把残行自动放回大厅——不再需要人工重置。
HALL_CLAIM_UNCLEAR_REFUSAL_REASON = HALL_CLAIM_FAILED_REASON + (
    "，而且那句话认不出是不是「已被接走」⇒ 镜像单留在「接单中」按退避重试；"
    "烧完额度后由拉取轮自动放回大厅（D-0335 清道夫），无需人工重置"
)
HALL_CLAIM_PROMOTE_RETRY_REASON = "上家已接单，但我方转正没落上（并发）⇒ 按退避重试"
#: ⭐ D-0335（业务方拍板「消掉抢单锁定」）：**请求路**认不出的拒绝**不再按退避重试**——那是把卡锁在 `claiming`（对所有人消失）、打手拿「临时失败」、烧完额度后**永久卡死没有任何恢复入口**（F6 / L-5 现场）的旧形态。
#: 依据：`rec_order_by_internal` 抛 `ApiError` = 上游回了**失败信封**（E-0087：失败信封必须判失败）⇒ 这次接单**没有被上家收下**，单还在上家大厅 ⇒ 当场放回 `listed` 是事实、不是赌。这句写进行 `last_error` 与台账 `reason`（运维可见）。
HALL_CLAIM_RELEASED_TO_HALL_REASON = (
    "上家没有受理这次接单（认不出拒绝原因），镜像单已放回大厅，可以重新接单"
)
#: ⭐ D-0335：**请求路传输层失败**（连接 / 超时 / 会话建不起）同一条拍板下的收口人话
#:（行 `last_error` 与台账 `reason`）。残差（如实登记）：若上游其实已收下而响应丢失，
#: `sync_my_orders` 的 `record_assigned_order` 下一轮把那张单录回**未分配池**（打手不自动挂上）。
HALL_CLAIM_TRANSPORT_RELEASED_REASON = (
    "上游暂时打不通，这次接单没有完成，镜像单已放回大厅，可以重新接单"
)
#: ⭐ D-0335：打手**当场**看到的那句（`ClaimError` 文案；R1 / R2 两条收敛共用——对打手而言
#: 两者的可感事实相同：这一枪没成交、卡回大厅了）。台账侧的分因靠上面两个 reason 区分。
HALL_CLAIM_RELEASED_MESSAGE = "这次接单没有完成，卡片已放回大厅，可以重新接单"
#: ⭐ D-0335：清道夫（`sync_hall` 行循环）把「事件已终态、永远等不到收口」的 `claiming` 残行
#: 放回大厅时写进行 `last_error` 的人话。⚠️ `failed` 且还有额度的不碰——那是后台轮正在重试的
#: 崩溃恢复路（`_consume_hall_claim` 的 `claimed` 断点）。
HALL_CLAIM_STALE_RELEASED_REASON = (
    "这次接单的流程已中断（事件已收口但单一直没转正），镜像单已放回大厅，可以重新接单"
)
HALL_CLAIM_MIRROR_MISSING_REASON = "台账里的镜像单已不存在，本次接单作罢"
#: ⭐ F1：事件归属与镜像行「当前 owner」不一致（含 owner 已被删
#: 置空）⇒ **不静默**：留一条人话台账。转正 / 链路 / 提成仍按**事件那份绑定**记
#: （依据与代价见 `promote_upstream_mirror` 的 docstring）。
HALL_CLAIM_OWNER_MISMATCH_REASON = (
    "镜像行的归属绑定（当前负责拉大厅的那一份）与本次事件的绑定不是同一份"
    "（同地址绑定轮换过）⇒ 链路与提成仍按**本次事件那份**记，请人工核对这个地址下的绑定开关"
)
#: ⭐ F1 收口（spec §15.5 改准块第 3 条 / §13.1-R24，业务方裁定）：事件归属那份绑定的**客服已停用**时，拼在上述那句人话后面**点名**这件事——提成与订单仍记在该停用账号名下（⛔ 不改派、⛔ 不回 `skipped`），运维据此才知道该找谁对账。
#: ⚠️ 判据**只认** `user.is_active`：R24 与 §15.5 第 3 条的措辞就是 `is_active=False` 的「客服停用」；§8.4 的「停用（`enabled=False`）」是**绑定开关**那一档，钱落在**在职**客服名下、不构成 R24 那句「钱记在停用账号上」的代价，⛔ 不并进这句点名（原句末尾「核对绑定开关」已覆盖它）。
HALL_CLAIM_OWNER_MISMATCH_INACTIVE_SUFFIX = (
    "；⚠️ 而且本次事件那份绑定的客服已停用（账号已停用）"
    "⇒ 提成与订单仍记在该停用账号名下，请人工核对"
)
#: ⭐ D-0335：清道夫放回的台账 kind（`direction="in"`，只作台账；⛔ 不进 `OUTBOX_KINDS`——
#: `run_outbox` 不取活它）。不复用 `upstream.hall_rescued`：那句是「gone 复活」的语义。
KIND_HALL_CLAIM_RELEASED = "upstream.hall_claim_released"
HALL_CLAIM_ROW_NOT_CLAIMING_REASON = "镜像单的占住权不在本事件的打手手上，本次接单作罢"
HALL_CLAIM_ROW_GONE_REASON = "镜像单已是终态（已消失 / 已跳过），本次接单作罢"
#: 转正那一笔订单审计的 `reason`（spec §6.1 第 ⑤ 步要「人话写明由哪家上家接单」）。
HALL_CLAIM_AUDIT_REASON = "由 {address} 上家抢单大厅接单"

COMPLETE_DONE_REASON = "已在上家提交完成（上家已变「待验收」）"
COMPLETE_ALREADY_REASON = "上家那张单已是「待验收 / 已完成」⇒ 不重复提交（幂等）"
COMPLETE_NOT_DONE_REASON = (
    "我方这一单不是「已完成」（当前「{status}」）⇒ 按本线口径不提交上家"
)
COMPLETE_LINK_MISSING_REASON = "台账里的链路已不存在（换绑 / 删号）⇒ 本次提交作罢"
COMPLETE_TERMINATED_REASON = "这一单的链路已停止跟踪（两边都到终态）⇒ 本次提交作罢"
COMPLETE_READBACK_FAILED_REASON = "提交前回读上家状态失败"
COMPLETE_FAILED_REASON = "上家拒绝了这次提交完成"
COMPLETE_CONFIRM_FAILED_REASON = "提交后回读上家状态失败"
COMPLETE_NOT_CONFIRMED_REASON = "提交后回读未确认已变「待验收」⇒ 不算成功（按退避重试）"
COMPLETE_PIC_BLOCKED_REASON = (
    "截图没传成功（上家要求提交完成前有图）⇒ 本次一次提交都不发"
)
#: 台账里的绑定行没了（`payload.upstream_binding_id` 指向的行被删）⇒ 终态，⛔ 不重试。
OUTBOX_BINDING_MISSING_REASON = (
    "台账里的绑定已不存在（upstream_binding_id 指向的行没了），本事件不再重试，请人工核对"
)
#: 未预期异常的台账原因（⛔ 只带异常**类型名**，不带 `str(exc)`：正文可能带上游回包 / 凭据）。
UNEXPECTED_EVENT_REASON = "未预期异常（{exc}）：台账 {kind}/{ref} 本轮未完成，按退避重试"
#: 到尝试上限时追加在那句原因后面的后缀。
ABANDONED_SUFFIX = "（已重试 {n} 次，不再自动重试，请人工核对）"
#: 一轮没有任何人话原因可写时的兜底（⛔ 不许写空串）。
ROUND_UNKNOWN_FAILURE_REASON = "本轮未完成，但没有留下具体原因（请查本地台账的最近几行）"
#: `last_error` 的防爆上限（它是 `TextField`；只防爆、不靠数据库截断）。
ROUND_REASON_MAX = 1000


@dataclass(frozen=True)
class OutboxStats:
    """一轮 `run_outbox` 的计数（形状照反方向线 `jidanpu_member.OutboxStats`）。
    - `done`：本轮消费成功的事件数；`failed`：本轮留下 `failed`（还会按退避再试）的事件数；`skipped`：本轮收敛成**终态** `skipped` 的事件数（有意的「不动作」）；
    - `deferred`：本轮**没动**（退避窗内等待 / 租约被别人持有）的事件数；`abandoned`：已到尝试上限、**不再自动重试**的事件数；`paused`：绑定已停用 ⇒ 本轮一个事件都没动（spec §8.4）。"""

    done: int = 0
    failed: int = 0
    skipped: int = 0
    deferred: int = 0
    abandoned: int = 0
    paused: bool = False


class UpstreamOutboxUnexpectedFailures(RuntimeError):
    """一个或多个事件抛出**未预期**异常（逐事件边界：其余事件照跑完再抛）。
    ⚠️ 未预期异常**响亮抛**（⛔ 不伪装成业务失败）：`failures` 是人话原因清单，`stats` 与正常返回同义 ⇒ 调用方拿到计数、把原因记进绑定的 `last_error`。"""

    def __init__(self, failures: list[str], *, stats: OutboxStats):
        super().__init__("；".join(failures))
        self.failures = list(failures)
        self.stats = stats


# ── 两个写原语（**形状逐字**照 spec §2.3 的实测载荷）────────────────────────────


def _write_judge():
    """函数内 import：`jidanpu_upstream_pics` 在**模块级** import 本模块 ⇒ 反向 import 会成环。
    ⚠️ 写端点成功判据（`_is_write_success`，`code > 0`）与取壳口（`_envelope_code`）**只有那一份定义**（GC 7 / spec §11-9 / E-0087）——⛔ 本模块不写第二份：两份判据必然漂移，而写端点的失败信封（`code=0` + 非空 `msg` + 空 `data`）恰好是 SDK 宽松规则会放行的那一种。"""
    from apps.business import jidanpu_upstream_pics

    return jidanpu_upstream_pics


def _require_write_success(client, *, path: str, action: str) -> None:
    """写端点的成功判据：壳里的 `code` 必须 **`> 0`**（GC 7 / spec §2.3 / §11-9）。
    ⚠️ 调用方显式传 `success_rule="lenient"`（与传图链的 `register_pic` 逐字同一手法）：这三个端点在 SDK 的清单里都是 `risk="write"` ⇒ SDK 默认按 `positive_code` 判定、**它自己会在我们的判据之前抛 `ApiError`** ⇒ 这一段就成了不可达的装饰（改成恒真也不会红）；显式 `lenient` 把「成功语义」的唯一出口交回我们自己（两种规则的合取与 `positive_code` 逐形状等价，⛔ 不是放宽）。
    ⚠️ `ApiError` 里**带上业务壳**（`body=`）：上层要靠它取上游那句 `msg` 判「已被接走」并把人话接进台账（⛔ 只有一句「被拒绝了」运维查不出根因）。"""
    judge = _write_judge()
    code = judge._envelope_code(client)
    if not judge._is_write_success(code):
        raise ApiError(
            f"{path} 的业务壳 code={code!r}（写端点成功码必须 > 0）：{action}失败",
            code=code if not isinstance(code, bool) else None,
            body=judge._last_envelope(client),
        )


def rec_order_by_internal(client, *, remote_order_id: str) -> None:
    """上家大厅接单（写；spec §2.3 **实测**：`data={"orderId": …}` ⇒ `code=1` +「接单成功…」）。
    ⭐ **A16**：这一跳**本身就是全部**——接单之后对方那张单自己就变「代练中」并离开大厅（spec §2.3 的实测）⇒ ⛔ 调用方**不许**再推一次 `SetOrderSta`（判据 = 桩的调用集合）。"""
    client.call(
        "POST", REC_ORDER_PATH, data={"orderId": remote_order_id}, success_rule="lenient",
    )
    _require_write_success(client, path=REC_ORDER_PATH, action="大厅接单")


def submit_completion(client, *, remote_order_id: str) -> None:
    """上家**提交完成**（写；spec §2.3 **实测**：`data={"orderId": …, "sta": "待验收"}`）。⚠️ 载荷**逐字**只有这两个键：⛔ **不带** `changeSoureType` / `remark` / `isSyncTaoBao`（那三个是管理端退单那条端点的形状，spec §2.3 的实测载荷里没有）。
    ⚠️ 成功码 = **提交笔数**（实测「已成功提交N笔订单」）⇒ 判据是 `code > 0`（`code=2` 也算成功）。"""
    client.call(
        "POST", jidanpu.SET_STATUS_PATH,
        data={"orderId": remote_order_id, "sta": COMPLETION_STATUS_LABEL},
        success_rule="lenient",
    )
    _require_write_success(client, path=jidanpu.SET_STATUS_PATH, action="提交完成")


# ── 取上游那一行 / 读它的 `orderSta`（**复用**指派单 / 回拉那两份取数原语）──────────────


def _remote_row(client, remote_id: str) -> dict | None:
    """在「我的订单」里按 `key1=<orderId>` 找那一行；没有 ⇒ `None`（提交完成的回读）。
    ⚠️ 按**单号单查**（`_my_order_row_by_key1`：只带 `key1`，并**逐字回校验** `orderId`）——⛔ 不为回读一张单去拉整张表（全量受 `MY_ORDERS_MAX_PAGES=50` ⇒ 1000 行硬顶）。
    ⚠️「没有这一行」**不当**「远端没有这张单」（spec §5.3 末行 / E-0004 同族）：调用方按「读不到」处理（提交完成那一支 ⇒ 退避重试，⛔ 不硬提交）。"""
    return _my_order_row_by_key1(client, remote_id=remote_id)


def _upstream_message(exc: ApiError) -> str:
    """上游业务壳里那句 `msg`（取不到 ⇒ 空串）。⚠️ 只取 `msg`，⛔ 不回显整包 body。"""
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    return str(body.get("msg", "") or "").strip().replace("\n", " ")


def _looks_taken(message: str) -> bool:
    """这句上游文案是不是在说「这张单已经不在大厅了」（spec §6.1 的 ⑥）。
    ⚠️ 保守分类（token 表见 `HALL_CLAIM_TAKEN_TOKENS`，**未实测**）：认不出来一律按**可重试**（⛔ 不误判 `gone`——误判会把一张真挂在大厅的单丢掉，比多试几次贵得多）。
    ⚠️ 保守口径的代价（F6，登记在 spec §13.1-R23）与它的 D-0335 出口：认不出的拒绝重试到 `OUTBOX_MAX_ATTEMPTS` 之后事件收敛成 `abandoned`、镜像行留在 `claiming`——旧行为「永久停在那里、没有任何恢复入口」，⭐ D-0335 起 `sync_hall` 的清道夫（`_hall_claim_event_settled` 判事件终态 + 无链路 ⇒ 放回 `listed`）把它接走，不再需要人工重置；请求路（`_claim_refused`）已**不再走到本理由**——它当场放回大厅。"""
    return any(token in message for token in HALL_CLAIM_TAKEN_TOKENS)


# ── 排事件（触发点 / 请求路径**只写一行**，⛔ 一次上游都不打）────────────────────


def _queue_out_event(*, kind: str, binding, target_type: str, target_ref: str,
                     payload: dict, rearm: bool = False) -> JidanpuSyncEvent:
    """写一行本线的 outbox 事件；**在飞**的同键行已存在 ⇒ 原样返回它（⛔ 不排第二条）。幂等键 = `(direction, kind, target_ref, binding)` 且只看 `pending` / `failed`（`done` / `skipped` 不挡：同一张单可以**再一次**被触发——那是「换句新记一条」）。⚠️ `payload["upstream_binding_id"]` **一律写 int**（`run_outbox` 的取活键就按它，写成字符串就永远取不到活）。
    ⭐ `rearm`（**F3**；口径照反方向线 `apps/business/jidanpu_member.py::queue_member_event` 的同一处取舍）：**显式动作**排事件时传 `True` ⇒ 命中的那一行若**已经烧完重试额度**（`attempts >= OUTBOX_MAX_ATTEMPTS`，`run_outbox` 此后把它归进 `abandoned`、**一次上游都不打**），就把额度与原因**复位**、让它再跑一次。⚠️ **为什么必须有**：去重只排除 `done` / `skipped`、`failed` 一律挡 ⇒ 一条烧完额度的行会被**每一次**新触发命中，于是「人工再动一次」变成**静默空转**（`queue_*` 照样回「已排队」、台账不新增一行、`run_outbox` 只记 `abandoned`，**没有任何恢复入口**）。
    ⚠️ 只复位**烧完额度**的行：还在额度内的行照旧按退避等（⛔ 连点几下不能绕过退避）。⛔ **周期对账不传 `rearm`**（默认 `False`）：那是「到点看一眼」，没有上限就变成无限重试——本线的两个触发点（**人接单** / 我方订单进**「已完成」**）都是显式动作 ⇒ 两处都传 `True`；将来若加周期对账来排这两类事件，必须走默认值。"""
    ref = target_ref[:_TARGET_REF_MAX]
    existing = (
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=kind, target_ref=ref,
            status__in=("pending", "failed"),
            payload__upstream_binding_id=binding.pk,
        )
        .order_by("id")
        .first()
    )
    data = {**payload, "upstream_binding_id": binding.pk}
    if existing is not None:
        replaced = kind == KIND_COMPLETE and any(
            (existing.payload or {}).get(key) != value for key, value in payload.items()
        )
        if replaced or (rearm and (existing.attempts or 0) >= OUTBOX_MAX_ATTEMPTS):
            next_payload = data if replaced else existing.payload
            updated = JidanpuSyncEvent.objects.filter(
                pk=existing.pk, status__in=("pending", "failed"), payload=existing.payload,
            ).update(
                attempts=0, status="pending", reason="", processed_at=None, payload=next_payload,
            )
            if updated:
                existing.attempts = 0
                existing.status = "pending"
                existing.reason = ""
                existing.processed_at = None
                existing.payload = next_payload
                return existing
        else:
            return existing
    return _record(
        direction="out", kind=kind, status="pending",
        target_type=target_type, target_ref=ref, payload=data,
    )


def _claim_reservation_key(reserved_at: datetime | None) -> str | None:
    return reserved_at.astimezone(UTC).isoformat() if reserved_at is not None else None


def queue_hall_claim(mirror_row, *, taker_id: int, channel="", downstream="", sync_actor="") -> JidanpuSyncEvent | None:
    """触发点（spec §6.1 第 ② 步）：我方大厅有人接了镜像单 ⇒ 排 `upstream.hall_claim`。归属绑定 = **镜像行的 `owner_binding`**（A2 的地址级去重结果，spec §4.3）：⚠️ 没有 owner（停用 / 解绑过）⇒ 回 `None`（⛔ 不排一条**算不出提成归谁**的事件）；⚠️ 本地预占（`listed → claiming`）与这一行**同事务**（调用方保证，spec §6.1 第 ①② 步）；⭐ `rearm=True`：这是**人点出来的接单动作**（显式动作）⇒ 烧完额度的同键旧行要能再跑一次（F3）。
    ⚠️ **可达性与判据（⭐ D-0335 起「一生一次」只剩成功路）**：D-0335 起 `claim_upstream_mirror` 的两条失败路（认不出的拒绝 / 传输层失败）**当场把行放回 `listed`**（`_release_claiming_to_hall`，`sync_hall` 清道夫再兜存量）⇒ 同一张行可以被再次显式接单，`rearm` 是**可达**的（由 `test_a_released_row_can_be_claimed_again` 钉住「放回 → 再点 → 第二条事件 → 第二次真打上游」）；仍然成立的只有**成功路**：`listed → claiming → promoted` 后行进终态，不会再有第二次显式动作命中它（由 `test_the_request_path_cannot_re_enter_an_already_claimed_row` 钉住）；`test_an_explicit_hall_re_queue_gives_a_burned_out_event_a_new_budget`（直调本函数造出现场）继续钉「额度 / 状态 / 原因 / `processed_at` 四值复位 + 下一次真的打上游」。
    ⛔ **别把 `rearm=True` 删掉 / 改成 `False`**：本函数是模块的公开触发点，任何直接 / 重试入口一旦命中它，少了这一格就是**静默空转**（`queue_*` 照样回「已排队」、`run_outbox` 只记 `abandoned`、没有任何恢复入口）——正是 F3 要消灭的形态。"""
    binding = mirror_row.owner_binding
    if binding is None:
        return None
    return _queue_out_event(
        kind=KIND_HALL_CLAIM,
        binding=binding,
        target_type="hall",
        target_ref=f"{binding.pk}|{mirror_row.upstream_base_url}/{mirror_row.upstream_order_id}",
        payload={
            "mirror_id": mirror_row.pk,
            "upstream_order_id": mirror_row.upstream_order_id,
            "binding_revision": binding.identity_revision,
            "claiming_at": _claim_reservation_key(mirror_row.claiming_at),
            "send_started": False,
            "taker_id": taker_id,
            "claim_channel": channel,
            "claim_downstream": downstream,
            "claim_sync_actor": sync_actor,
        },
        rearm=True,
    )


def queue_complete(order) -> int:
    """触发点（spec §6.2 的入口）：我方订单进「已完成」⇒ 给**已有链路**的绑定排提交完成。⭐ **零绑定即零成本**（本函数挂在全仓最热的写入口 `apply_transition` 上）：**一次**查询取链路（`select_related` 带上绑定 ⇒ ⛔ 不查第二次），空就立刻返回。
    ⚠️ 判据是「**本地有没有链路**」而不是「有没有绑定」（与 `jidanpu_member.queue_status_out` 同一条口径）：状态是发给**已经录到上家那张单**的，没有链路发过去只会得到一条 `skipped`；已停止跟踪的链路（两边都终态）**不排**（spec §5.4：往后不为它打任何上游写）；回「**已排队**的事件数」（含被去重命中的在飞行）——判据要「恰好一条」。
    ⭐ `rearm=True`（F3）：这是**我方订单进「已完成」那一次状态变更**排的事件（显式动作）⇒ 烧完额度的同键旧行要能再跑一次；⚠️ 将来若有周期对账来排同类事件，必须走默认 `rearm=False`（见 `_queue_out_event` 的口径）。"""
    links = list(
        JidanpuUpstreamOrderLink.objects.filter(order=order).select_related("binding")
    )
    queued = 0
    for link in links:
        if link.is_terminated:
            continue
        _queue_out_event(
            kind=KIND_COMPLETE,
            binding=link.binding,
            target_type="order",
            target_ref=f"{link.binding_id}|{order.order_no}",
            payload={
                "link_id": link.pk,
                "upstream_order_id": link.upstream_order_id,
                "completed_at": _completion_version(order),
            },
            rearm=True,
        )
        queued += 1
    return queued


# ── 触发点接线（`order_status` + `recycle` 两处**只加**）───────────────
# ⚠️ 这两个函数是 `apply_transition` / `soft_delete_order` 尾上那两个调用点的**唯一出口**（spec §6.2 / §6.4 / §15.4 / §15.6）。语义按「我方动作 ⇒ 要不要打上游」分三档：**进「已完成」= 唯一一个会排队的状态写**；退款 / 删除 / 问题单 = **零上游写 + 一条人话**；其余状态 = 什么都不做。


def _tracks_order(order) -> bool:
    """本线有没有在跟这张单（有一条链路行，**含已停止跟踪的**）。⚠️ **三个「只记账」出口统一过这道闸**，理由三条：① **语义**：那两句人话断言的是「**上家那边还挂着这张单**」（spec §15.4 / §13.1-R12）——一张本线**压根没录过**的单，上家那边什么都没有，记这句话就是**说假话**（后来者会照着它去找一张不存在的上家单）；② **成本**：不挂本线链路的租户，每一次退款 / 问题单 / 删单都要多写两笔（`note_once` 的存在性查询 + 插入），过闸后是**一次** `exists()` 就返回；③ **一致性**：`queue_complete` 本身就按「本地有没有链路」短路，反方向线 `jidanpu_member.queue_status_out` 是**同一条口径**（判据 = 本地有没有链路，其用例 `test_a_status_change_without_any_link_queues_nothing` 钉着它）⇒ 本线三个分支**统一**按它，⛔ 不留一个特例。
    ⚠️ **已停止跟踪（`terminated_at` 非空）的链路仍然算**：那表示两边都到过终态，而**上家那张单仍在**（待验收 / 已完成）⇒ 「人工去沟通」照样成立。"""
    return JidanpuUpstreamOrderLink.objects.filter(order=order).exists()


def on_order_status_changed(order, from_status) -> None:
    """我方订单状态变更之后本线的动作（挂在**唯一状态写入口** `apply_transition` 上）。`COMPLETED` ⇒ `queue_complete(order)`：**只排队**（spec §6.2 明文「漏斗里只排队、⛔ 一次上游都不打」——真打上游的是 outbox 消费者 `consume_event` / `run_outbox`）；`REFUNDED` ⇒ **零上游写**（A12 / spec §15.4）+ 一条台账人话：上家打手账号**没有**删单 / 退单权限，业务方裁定「不同步上去」；`ISSUE` ⇒ **零上游写**（A14 / spec §15.6）+ 一条台账人话：上家**全局禁用**「问题单」；其余（未分配 / 待抢单 / 代练中 / 待验收 / 已暂停）⇒ **什么都不做**——⚠️ 含业务方第三轮裁定的「我方接手 ⇒ ⛔ 不推上家『代练中』」（A16 / spec §16.2：**接单动作本身**就让对方那张单变「代练中」，spec §2.3 实测）⇒ 本线不额外推一次。
    ⚠️ **退款 / 问题单那两档都先过 `_tracks_order`**（本线没在跟这张单 ⇒ 一个字都不写），与 `queue_complete` 的链路闸同一口径（见 `_tracks_order` 的三条理由）。
    ⚠️ **调用方必须在来源闸（`suppress_outbound_sync`）之内**：本函数的两档都会写出口（一档排队、两档写台账），而回拉写回不许对外回声（B7 / spec §11-17）⇒ 上家退我方的那一单**不算**「我方退款」，⛔ 不写那句「上家账号无权限」的人话。⚠️ `from_status` 只进台账 `payload`（「从哪个态退到已退单」是可读的排障信息），⛔ **不进幂等键**：同一单从哪个态退到 `REFUNDED` 不改变结论，换来源态不该多灌一行。⚠️ 本函数**自己不做上游调用**、也不建连（判据见 `tests/test_jidanpu_upstream_triggers.py::CompletionTriggerTests`）。"""
    target = str(order.status)
    if target == str(OrderStatus.COMPLETED):
        queue_complete(order)
        return
    if target == str(OrderStatus.REFUNDED):
        if _tracks_order(order):
            note_once(
                order=order, kind=KIND_REFUND_SKIPPED, status="skipped",
                reason=REFUND_NOT_SYNCED_REASON,
                payload={"from_status": str(from_status)},
            )
        return
    if target == str(OrderStatus.ISSUE):
        if _tracks_order(order):
            note_once(
                order=order, kind=KIND_ISSUE_SKIPPED, status="skipped",
                reason=ISSUE_NOT_SYNCED_REASON,
                payload={"from_status": str(from_status)},
            )


def on_order_deleted(order) -> None:
    """我方订单**软删**之后本线的动作（挂在 `recycle.soft_delete_order` 的首次删除分支上）。⛔ **零上游写**（A12 / spec §15.4）：上家打手账号没有删单权限，业务方裁定「不同步上去」⇒ 只留一条人话台账；⚠️ 代价已登记（spec §13.1-R12）：**上家那边还挂着这张单**，要人工去沟通——那是业务方明确选的形态，⛔ 不当作缺陷、也⛔ 不在这里找端点补一个退单。
    ⚠️ 先过 `_tracks_order`（本线没在跟这张单 ⇒ 一个字都不写）：那句人话断言的是「上家那边还挂着这张单」，对本线没录过的单记它就是**说假话**——这条闸同时让反方向线那条「从没推出去过的单删掉 ⇒ 一行都不写」的反向控制仍然成立（它按 `target_ref` 数**整张**台账）。
    ⚠️ 与旁边那两条 `queue_withdraw` 的**形状不同**是刻意的：`jidanpu_hall.queue_withdraw` / `jidanpu_member.queue_withdraw` **会**各排一条**真待办**（那是**另外两条线**对**它们自己的**远端写的，有真消费者）；本线**一个字都不排**（⛔ 别把三者混成同一件事）。"""
    if not _tracks_order(order):
        return
    note_once(
        order=order, kind=KIND_DELETE_SKIPPED, status="skipped",
        reason=DELETE_NOT_SYNCED_REASON,
    )


# ── 请求路径：大厅接单（本地预占 → 同事务写 outbox → 赢下租约才打上游）──────────

#: ⭐ 业务方报障修法：接单命中**停用 / 缺失绑定**名下仍标 `listed` 的僵尸卡（E-0288 读口过滤后滞留；App 缓存旧大厅列表 / 按单号兜底两条通路都还命中得了它们）⇒ 就地下架时台账用的成因句。
#: 与拉取侧 `_mark_gone` 的 `GONE_REASON` 同表不同因，⛔ 不复用那句——那句说的是「上游把它撤了」，这句说的是「我方绑定停了」。
BINDING_DISABLED_GONE_REASON = "归属同步账号已停用（或缺失），僵尸镜像卡接单命中后就地拆雷下架"

#: 停绑批量下架（生效点 + 一次性命令共用）的台账成因句：与上面那句同 kind 不同因——
#: 那句说「接单命中后就地拆雷」，这句说「停绑/换绑生效点批量下架」。
BINDING_DISABLED_BATCH_GONE_REASON = "归属同步账号已停用（或换绑/删除），无人接管的孤儿镜像卡批量下架"


def orphan_hall_rows(candidates):
    """同地址仍有启用绑定时，让下一轮同步接管镜像卡。"""
    successor = JidanpuUpstreamBinding.objects.filter(
        jidanpu_base_url=OuterRef("upstream_base_url"), enabled=True,
    )
    return candidates.filter(~Exists(successor))


@transaction.atomic
def batch_unlist_orphan_hall_rows(candidates, *, now: datetime | None = None) -> int:
    """锁住待抢的孤儿卡，同批下架并收尾本地和下家副本。"""
    from apps.business import hall_snapshot, upstream_hall_shadow

    now = now or timezone.now()
    mirrors = list(
        orphan_hall_rows(candidates.filter(status=_HALL_LISTED)).select_related("owner_binding")
        .select_for_update(of=("self",), no_key=True).order_by("pk")
    )
    if not mirrors:
        return 0
    JidanpuUpstreamHallOrder.objects.filter(pk__in=[mirror.pk for mirror in mirrors]).update(
        status=_HALL_GONE, gone_at=now,
    )
    hall_snapshot.record_mirror_exits(
        mirrors, exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.WITHDRAWN, at=now,
    )
    JidanpuSyncEvent.objects.bulk_create([
        JidanpuSyncEvent(
            direction="in", kind="upstream.hall_gone", status="done", target_type="hall",
            target_ref=(
                f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"[:_TARGET_REF_MAX]
            ),
            reason=BINDING_DISABLED_BATCH_GONE_REASON,
            payload={"upstream_binding_id": mirror.owner_binding_id},
        )
        for mirror in mirrors
    ], batch_size=250)
    upstream_hall_shadow.refund_for_cards(mirrors, now=now)
    return len(mirrors)


def unlist_orphan_hall_rows(binding: JidanpuUpstreamBinding, *, now: datetime | None = None) -> int:
    """停绑/换绑生效点：批量下架这份绑定名下的孤儿镜像卡（接管判据见 `orphan_hall_rows`）。"""
    return batch_unlist_orphan_hall_rows(
        JidanpuUpstreamHallOrder.objects.filter(
            owner_binding=binding, status=_HALL_LISTED,
        ).order_by("pk"),
        now=now,
    )


# ── ⭐ 点卡先探一跳（僵尸窗治理二期③，2026-11-05 业务方拍板）：claim 前的滞卡新鲜复核 ──

#: 「滞卡」判据：`last_seen_at` 落后超过 `2 × hall_poll_seconds`（完整拉取周期＋余量）才复核。
CLAIM_STALE_RECHECK_LAG_MULTIPLIER = 2
#: 上述判据的秒数下限（防 15s 级配置的抖动误触发）。⛔ 无开关配置：要调走轻量档改常量。
CLAIM_STALE_RECHECK_MIN_LAG_SECONDS = 90
#: 复核翻页上限（≥60 张段）防长尾；撞上限未见 = 「判不了」⇒ 放行（宁漏标勿误标）。
CLAIM_STALE_RECHECK_MAX_PAGES = 3
#: 复核「不在」下架的台账成因句（与 `BINDING_DISABLED_GONE_REASON` 同 kind 不同因）。
CLAIM_STALE_RECHECK_GONE_REASON = "接单前新鲜复核：上游大厅已查无此单，滞留镜像卡就地下架"


def _claim_stale_seconds(binding) -> int:
    """「滞卡」的滞后阈值（秒）= `max(2 × hall_poll_seconds, 90)`。⚠️ 列存 `0`（配置写坏）时
    2×0=0，由 90 秒下限兜住——与调度侧 `_poll_seconds` 的默认值兜底在阈值上同效（90 > 2×默认30）。"""
    return max(
        CLAIM_STALE_RECHECK_LAG_MULTIPLIER * (binding.hall_poll_seconds or 0),
        CLAIM_STALE_RECHECK_MIN_LAG_SECONDS,
    )


def _claim_row_is_stale(row, binding, *, now: datetime) -> bool:
    """该 `listed` 行是不是「滞卡」：`last_seen_at` 落后 `now` 超过一个完整拉取周期＋余量。
    `last_seen_at` 为空（从没确认过在厅）⇒ 不是滞卡（判据不成立，照现状放行）。"""
    if row.last_seen_at is None:
        return False
    return (now - row.last_seen_at) > timedelta(seconds=_claim_stale_seconds(binding))


def _claim_stale_recheck(client, *, game_name: str, order_id: str) -> bool | None:
    """接单前的段查复核（先探一跳）：带**完整游戏名**翻页找 `orderId`，三态：
    `True`=在 / `False`=确定不在（空页=段尾且壳 count 与已扫行数相符）/
    `None`=判不了（撞页上限；空页但 count>已扫行数的假空页，E-0257 同族）⇒ 调用方放行。
    查询形状与 `_mark_gone` 的段查复核同款（E-0314 ⛔ 不另造）；命中早退 ⛔ 不翻完整段。"""
    page = 1
    scanned = 0
    count: int | None = None
    while True:
        rows, page_count = _fetch_page(
            client, HALL_PATH, page=page, page_param="page",
            query={"gameName": game_name},
        )
        if count is None:
            count = page_count
        scanned += len(rows)
        if order_id in {str(row.get("orderId", "") or "") for row in rows}:
            return True
        if not rows:
            if count is not None and count > scanned:
                return None
            return False
        if page >= CLAIM_STALE_RECHECK_MAX_PAGES:
            return None
        page += 1


def _claim_recheck_defuse_gone(row, *, now: datetime) -> bool:
    """复核「不在」的落库（一次小事务，照停绑拆雷支完整形态）：条件更新 `listed → gone` ⇒
    灰卡 + `upstream.hall_gone` 台账 + 隐藏单退款；并发拿不到 rowcount ⇒ `False` 零写入。"""
    from apps.business import hall_snapshot

    with transaction.atomic():
        updated = JidanpuUpstreamHallOrder.objects.filter(
            pk=row.pk, status=_HALL_LISTED
        ).update(status=_HALL_GONE, gone_at=now)
        if not updated:
            transaction.set_rollback(True)
            return False
        try:
            hall_snapshot.record_mirror_exit(
                row,
                exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.WITHDRAWN,
                at=now,
            )
        except Exception:  # noqa: BLE001 —— 辅助数据失败不拦拆雷（响亮留痕）
            logger.exception("复核下架快照写入失败 mirror=%s", row.pk)
        _record(
            direction="in", kind="upstream.hall_gone", status="done",
            target_type="hall",
            target_ref=(
                f"{row.upstream_base_url}/{row.upstream_order_id}"[:_TARGET_REF_MAX]
            ),
            reason=CLAIM_STALE_RECHECK_GONE_REASON,
            payload={"upstream_binding_id": row.owner_binding_id},
        )
        from apps.business import upstream_hall_shadow

        upstream_hall_shadow.refund_for_card(row, now=now)
    return True


def _release_claiming_to_hall(row, *, taker_id: int, note: str, now: datetime) -> bool:
    """⭐ D-0335：把一次**没有成交**的接单当场放回大厅（`claiming → listed`）。「消掉抢单锁定」的落点 helper：请求路两条失败（认不出的拒绝 / 传输层失败）不再把卡锁在 `claiming` 等后台慢慢收口，而是当场放回。
    判据（条件更新）= 行还是 `claiming`；命中 ⇒ 清两接单列（`claiming_by` / `claiming_at`，钱包解冻闸 `_has_active_work` 读它们，不清就是 L-5 的「押金无出口」）、写 `last_error` 人话、**删离厅灰卡**（照 `hall_snapshot.clear_mirror_snapshots` 的「复活回大厅防一单双卡」同一条纪律）。
    没命中（并发已转正 / 已被别的路收口）⇒ 回 `False`、什么都不写。"""
    from apps.business import hall_snapshot

    updated = JidanpuUpstreamHallOrder.objects.filter(
        pk=row.pk, status=_HALL_CLAIMING, claiming_by_id=taker_id, claiming_at=now
    ).update(
        status=_HALL_LISTED,
        claiming_by_id=None,
        claiming_at=None,
        last_error=note[:255],
    )
    if not updated:
        return False
    try:
        hall_snapshot.clear_mirror_snapshots(row)
    except Exception:  # noqa: BLE001 —— 辅助数据失败不拦收敛（响亮留痕，照既有写点口径）
        logger.exception("放回大厅的灰卡清理失败 mirror=%s", row.pk)
    return True


def claim_upstream_mirror(
    *, user_id: int, mirror_id: int, now: datetime | None = None,
    actor: str = "", actor_id: int | None = None,
    enforce_deposit: bool | None = None,
    enforce_limits: bool = True,
    channel="", downstream="", sync_actor="",
) -> Order:
    """镜像单接单的请求路径（spec §6.1 的 ①–⑤）。**顺序逐条照抄 P2 的 `claim_remote`**。前置：行必须 `listed`（否则 `1012` 已被接走 / `1002` 处理中）；归属绑定停用 / 缺失 ⇒ 按已下架口径回 `1012` 并**命中即下架**（`listed → gone`，业务方报障修法，见下面那支的注释）；⭐ 滞卡（`last_seen_at` 落后超过 `_claim_stale_seconds` 阈值）⇒ 抢单前先做一次段查新鲜复核（「点卡先探一跳」，见 `_claim_stale_recheck`）；抢单限制四支**在任何写入之前**跑（`check_grab_limits`，⛔ 不另开一道门）；本地预占（**条件更新** `listed → claiming` + `claiming_by`）与 outbox 事件**同事务**；⭐ **提交之后必须赢下同一条执行租约才打上游**（⛔ 不持租约不打上游）——这条请求路径与后台轮（`run_outbox`）看的是**同一条**在飞事件；⛔ **租户上下文只来自认证态**：`user_id` 由端点在**已认证的请求**上算出来，不接受任何「哪个租户」的入参（`AGENTS.md` §4）。
    ⭐ **`enforce_limits`（D-0325）**：缺省 `True` = 既有调用点（网页 / takerapi 的人类接单）行为逐字不变；**只有 `jidanpu_member.machine_claim_order` 的镜像隐藏单分支传 `False`**（裁定 8 的同一条口径：机器接手 = 外部事实已定——下家打手已在对方系统接走副本 ⇒ 按「对方接单 ⇒ 我方自动接手」无条件收口，被按人闸门挡住会造出「我方没接手、对方在做」的错位）。
    `False` 跳过三道**按人**的闸：`check_grab_limits` 四支、优选闸（`check_preferred_claim`）、优选单强制押金（D-0296 那半）——`enforce_deposit=False` 另行跳过押金本体（与 `claim_order` 的机器口径同形）。"""
    now = now or timezone.now()
    row = JidanpuUpstreamHallOrder.objects.filter(pk=mirror_id).first()
    if row is None or row.status != _HALL_LISTED:
        if (
            row is not None
            and row.status == _HALL_CLAIMING
            and row.claiming_by_id == user_id
        ):
            raise ClaimError(jidanpu_common.IN_PROGRESS_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
        raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)

    binding = row.owner_binding
    if binding is not None and binding.jidanpu_base_url != row.upstream_base_url:
        # The next successful poll for the old shop can take over this mirror.
        raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
    if binding is None or not binding.enabled:
        # ⭐ 业务方报障（40 次被拒全部打在停用绑定名下仍标 `listed` 的镜像卡上）：旧口径回 `1015`「还没配置好鸡蛋铺同步账号，请联系管理员」——这句话让打手去找管理员，而这批卡是 E-0288 读口过滤后滞留的**僵尸卡**，对打手而言与「已下架」不可区分 ⇒ 诚实化：按已下架口径回 `1012`，并**命中即下架**（listed → gone，735 张被点一颗拆一颗）。
        #    写法照拉取侧 `_mark_gone` 的完整形态：条件更新（拿不到 ⇒ 并发已改走，不写留痕）+ `withdrawn` 灰卡快照 + `upstream.hall_gone` 台账（kind 复用消失判定那一档，成因靠 `reason` 区分）。
        #    ⚠️ 绑定恢复启用且上游还在挂时，`sync_hall` 的 gone 复活（D-0300）会把它自然拉回大厅——与 E-0288 的「数据保留」口径一致，这里不是终局删除。
        from apps.business import hall_snapshot

        updated = JidanpuUpstreamHallOrder.objects.filter(
            pk=row.pk, status=_HALL_LISTED
        ).update(status=_HALL_GONE, gone_at=now)
        if updated:
            try:
                hall_snapshot.record_mirror_exit(
                    row,
                    exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.WITHDRAWN,
                    at=now,
                )
            except Exception:  # noqa: BLE001 —— 辅助数据失败不拦接单收口（响亮留痕）
                logger.exception("停用绑定僵尸卡下架快照写入失败 mirror=%s", row.pk)
            _record(
                direction="in", kind="upstream.hall_gone", status="done",
                target_type="hall",
                target_ref=(
                    f"{row.upstream_base_url}/{row.upstream_order_id}"[:_TARGET_REF_MAX]
                ),
                reason=BINDING_DISABLED_GONE_REASON,
                payload={"upstream_binding_id": binding.pk if binding else None},
            )
            # ⭐ D-0325：僵尸卡下架 ⇒ 隐藏单退款 + 撤下家副本
            #    （`refund_for_card` 无隐藏单时自己回 0）。
            from apps.business import upstream_hall_shadow

            upstream_hall_shadow.refund_for_card(row, now=now)
        # ⚠️ `defused` 只在拆雷真的落库时置位（并发已改走 ⇒ updated == 0 ⇒ False）：
        # 带 @idempotent 的对外端点据此保住这笔写入（见 `ClaimError` 的 docstring）。
        raise ClaimError(
            jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN,
            defused=bool(updated),
        )

    # ⚠️ 四支限制在**条件更新之前**跑（被拒时一行都不写；E-0022 同族）。⭐ **闸门读的是勾兑后的发单价**（业务口径「打手在大厅里看到的就是接这单能拿多少」）：与投影 `serialize_upstream_row`、转正 `_claim_upstream` **同一个** helper、**同一份**绑定口径（= 上面那个 `binding`）——三处同源。
    #    ⛔ 修前这里读的是 `row.remote_rec_fee`（**上家原价** = 我方客户价）⇒ 与大厅展示价错位：比例 50 / `recFee=88`（展示 44.00）时，`grab_price_max=60` 的打手**看到 44 却被拒**，而 `grab_price_min=60` 的打手**看到 44 低于下限却放行**（两个方向都由 `tests/test_jidanpu_upstream_offer_price.py` 钉住）。
    #    **口径修订**：上家没给价 ⇒ 发单价按 **0.00** 算 ⇒ 闸门把它当成一张 **0 元单**：设了「最低接单价 > 0」的打手**看得见、接不了**（价格区间那一支拒）。这是本次口径接受的后果（已登记为残差）。
    if enforce_limits:
        check_grab_limits(
            user_id=user_id,
            candidate=GrabCandidate(
                hall_price=upstream_mirror_hall_price(row, binding=binding),
                order_type=OrderType.BOOST,
            ),
        )
    # ⭐ 优选闸（D-0296）：镜像单的「优选」按**勾兑后的展示价**判（与上面那道闸同价源）；个人打手的两档门槛（没完成过单 / 完成数异常多）。
    #    ⚠️ 排在 `check_grab_limits` **之后**（与原生 claim 链同一顺序）；⛔ 本函数**不重复**那五支的任何判据。⚠️ 机器接手（`enforce_limits=False`，D-0325）两道按人的闸一起跳（裁定 8）。⚠️ 函数内懒导入（`hall_preferred` → `claim`，顶层 import 会在本模块的导入图上多一条环）。
    from apps.business import hall_preferred, hall_snapshot

    if enforce_limits:
        hall_preferred.check_preferred_claim(
            user=User.objects.get(pk=user_id), mirror=row
        )

    # ⭐ 点卡先探一跳（二期③）：滞卡在**任何本地写入之前**段查复核——确定不在 ⇒ 回 1012 并
    #    就地下架（换掉注定失败的抢单写路径，不排 outbox、不进熔断计数）；在 / 判不了（撞
    #    页上限、假空页）⇒ 静默放行（宁漏标勿误标）。复核失败（JidanpuError）⇒ fail-open。
    if (
        _claim_row_is_stale(row, binding, now=now)
        and row.game_id is not None
        and row.upstream_order_id
    ):
        try:
            with jidanpu.claim_session(binding) as client:
                in_hall = _claim_stale_recheck(
                    client,
                    game_name=row.game.name,
                    order_id=row.upstream_order_id,
                )
        except JidanpuError:
            logger.debug("接单前滞卡复核查询失败，按现状放行 mirror=%s", row.pk)
        else:
            if in_hall is False:
                defused = _claim_recheck_defuse_gone(row, now=now)
                raise ClaimError(
                    jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN,
                    defused=defused,
                )

    with transaction.atomic():
        if not binding.current_identity_rows().filter(enabled=True).exists():
            raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
        # ⭐ 解冻冷静期（选项 A）：上家单也是「接单」⇒ 冷静期内的解冻申请在这里作废（已到期的先结算回余额，再由下面的补冻从余额重新扣）。
        # ⚠️ 必须**先于**下面的补冻：反过来会先看到「旧 frozen 够用」而跳过补冻、随后才结算，留下「有单、零冻结」的窟窿（与 `claim.claim_order` 同一顺序、同一个函数）。
        # ⚠️ 在同一个事务里：预占失败（`updated != 1` / 事件建不出来）会连着作废一起回滚。
        from apps.accounts.wallet import cancel_release_on_claim

        cancel_release_on_claim(user_id)

        # ⚠️ 押金冻结（业务方：上家单与自家单**一样**）：与本地预占**同一个事务**，余额不足时在**打上游之前**就拒（可预期失败），预占失败回滚会连带撤销冻结。
        # ⚠️ 镜像行没匹配到游戏（`game_id` 空）⇒ 没有押金规则可查，不冻（不是静默漏：这一列由拉取投影写入，空值本身已在大厅/台账里可见）。
        from django.conf import settings

        deposit_gate = (
            settings.DEPOSIT_ENFORCED if enforce_deposit is None else enforce_deposit
        )
        # ⭐ 优选单强制押金（D-0296）：租户总闸关着也要冻。⚠️ **无条件覆盖**（⛔ 不设「`enforce_deposit is None`」档）：本函数**没有机器路径**（只有人类接单端点调它），而它的两个人类入口——内部镜像接单视图传的是 `deposit_enforced_for(request)`（具体 bool，总闸关时 = False）、takerapi 镜像接单不传（None）——旧的「`is None` 才覆盖」条件在前者上**永不命中**（H-1 现场：网页镜像接优选单不冻押金）⇒ 优选镜像一律强制执行押金校验。
        #    ⚠️ 判价用**勾兑后的展示价**（与上面优选闸同价源）。
        #    ⭐ 优选租户开关（D-0328）：开关关（N）⇒ 本覆盖整支跳过（高价镜像回归普通接单规则，押金只按租户总闸 / 调用方显式给的那一档）。
        if (
            enforce_limits
            and not deposit_gate
            and hall_preferred.preferred_enabled()
            and hall_preferred.is_preferred_price(
                upstream_mirror_hall_price(row, binding=binding)
            )
        ):
            deposit_gate = True
        if deposit_gate and row.game_id is None:
            # ⚠️ 没有匹配到游戏就无法查押金规则 ⇒ fail-closed（⛔ 不能跳过冻结照打上游：
            # 转正那一步也会因为没有 game 而建不出单，只会留下 ghost claiming）。
            raise ClaimError(
                "该单没有匹配到游戏，无法冻结押金，请稍后重试",
                ErrorCode.BUSINESS,
            )
        if deposit_gate and row.game_id is not None:
            from apps.accounts.wallet import WalletError, ensure_deposit_for_claim

            try:
                ensure_deposit_for_claim(
                    User.objects.get(pk=user_id), row.game,
                    ref_no=row.upstream_order_id or str(row.pk),
                )
            except WalletError as exc:
                raise ClaimError(str(exc), exc.code) from exc

        updated = JidanpuUpstreamHallOrder.objects.filter(
            pk=row.pk, status=_HALL_LISTED,
            owner_binding__in=binding.current_identity_rows().filter(enabled=True),
        ).update(
            status=_HALL_CLAIMING, claiming_by_id=user_id, claiming_at=now,
        )
        if updated != 1:
            transaction.set_rollback(True)
            raise ClaimError(
                jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN
            )
        # ⭐ 离厅快照（D-0296）：`listed → claiming` 成功 = 这张镜像**离开大厅**的那一刻 ⇒ 写一条 `claimed` 灰卡（写点收拢在 `hall_snapshot.py`）。
        #    ⚠️ 之后的 `claiming → promoted`（`promote_upstream_mirror`）**不重复写**；接单被上家拒（`_claim_refused` 的 claiming → gone）也**不写**——那时它的原状态不是 `listed`（灰卡已经在上面那一次写过了）。
        #    ⚠️ 快照是读面的辅助数据：失败响亮留痕，⛔ 不把一次合法接单打挂。
        try:
            hall_snapshot.record_mirror_exit(
                row, exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.CLAIMED, at=now,
            )
        except Exception:  # noqa: BLE001 —— 辅助数据失败不回滚接单（响亮留痕）
            logger.exception(
                "镜像离厅快照写入失败（不影响接单）mirror=%s", row.pk
            )
        row.claiming_at = now
        event = queue_hall_claim(
            row, taker_id=user_id, channel=channel, downstream=downstream, sync_actor=sync_actor,
        )
        if event is None:
            transaction.set_rollback(True)
            raise ClaimError(
                jidanpu_common.NOT_CONFIGURED_MESSAGE, ErrorCode.PLATFORM_NOT_CONFIGURED
            )

    with jidanpu_common.execution_lease(
        event, now=now, lease_seconds=jidanpu_common.OUTBOX_LEASE_SECONDS
    ) as token:
        if token is None:
            if not binding.current_identity_rows().exists() and _release_rebound_unstarted_claim(
                row, event, taker_id=user_id, reserved_at=now, now=now, lease_token=None,
            ):
                raise ClaimError(BINDING_CHANGED_REASON, ErrorCode.PLATFORM_QUERY_FAILED, defused=True)
            return _claim_while_another_executor_owns(row=row, user_id=user_id)
        return _claim_upstream(
            binding, row=row, event=event, user_id=user_id, now=now,
            actor=actor, actor_id=actor_id, lease_token=token,
        )


def _claim_while_another_executor_owns(*, row, user_id: int) -> Order:
    """请求路径**抢不到租约**时的收敛（照 P2 `_claim_while_another_executor_owns` 的三态）。⛔ 绝不在这种情况下照打上游（那正是租约要消灭的重复接单）；⛔ 也不外抛未预期异常——一次正常接单不该变成 500。三种形状（**重读行**，不猜）：行已 `promoted` 且占住人就是本次打手 ⇒ 把这张单按成功交回去；已 `gone` ⇒ 复用「已被其他打手接取」；
    其余（`claiming`，持约方正同步在跑）⇒ ⭐ D-0335 改准：回 **`1012`**（旧的 `1002`「正在抢单，请稍后刷新查看」让人对着旧快照手动刷新——App 只对 `1012` 自动刷新列表；持约方秒级必收口，刷新后看到的已是终局）。"""
    fresh = JidanpuUpstreamHallOrder.objects.filter(pk=row.pk).first()
    if fresh is not None and fresh.status == _HALL_PROMOTED:
        order = fresh.promoted_order
        if order is not None and fresh.claiming_by_id == user_id:
            return order
    # 其余两种形状（`gone`；`claiming` 且持约方正同步在跑）同一句**终局**话（⭐ D-0335）：
    # 旧口径对 `claiming` 档回 `1002`「正在抢单，请稍后刷新查看」，让人对着旧快照手动刷新
    # （App 只对 `1012` 自动刷新列表）；持约方秒级必收口，刷新后看到的已是终局 ⇒ 一律 `1012`。
    raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)


def _release_failed_claim(row, event, *, taker_id, reason, now, lease_token) -> bool:
    with transaction.atomic():
        owned = JidanpuSyncEvent.objects.filter(
            _current_outbox_predicate(event), lease_owner=lease_token,
        )
        if not owned.select_for_update().exists():
            return False
        released = _release_claiming_to_hall(row, taker_id=taker_id, note=reason, now=now)
        if released:
            _converge(event, reason=reason, now=now)
        return released


def _release_rebound_unstarted_claim(row, event, *, taker_id, reserved_at, now, lease_token) -> bool:
    retired = Q(status="skipped", reason=REBINDED_EVENT_REASON)
    state = retired if lease_token is None else retired | _current_outbox_predicate(event)
    with transaction.atomic():
        unstarted = JidanpuSyncEvent.objects.filter(
            state, pk=event.pk, lease_owner=lease_token, payload__send_started=False,
            payload__claiming_at=_claim_reservation_key(reserved_at),
        ).exclude(payload__claimed=True, payload__has_key="claimed")
        if not unstarted.select_for_update().exists():
            return False
        released = _release_claiming_to_hall(
            row, taker_id=taker_id, note=REBINDED_EVENT_REASON, now=reserved_at,
        )
        if released:
            _skip(event, reason=REBINDED_EVENT_REASON, now=now)
            batch_unlist_orphan_hall_rows(JidanpuUpstreamHallOrder.objects.filter(pk=row.pk), now=now)
        return released


def _save_claim_progress(event, *, lease_token, **progress) -> bool:
    payload = {**(event.payload or {}), **progress}
    saved = JidanpuSyncEvent.objects.filter(
        _current_outbox_predicate(event), lease_owner=lease_token, payload=event.payload,
    ).update(payload=payload)
    if saved:
        event.payload = payload
    return bool(saved)


def _check_unstarted_claim_binding(binding, row, event, *, user_id, now, lease_token) -> None:
    enabled = binding.current_identity_rows().values_list("enabled", flat=True).first()
    if enabled is True:
        return
    released = enabled is None and _release_rebound_unstarted_claim(
        row, event, taker_id=user_id, reserved_at=now, now=now, lease_token=lease_token,
    )
    raise ClaimError(BINDING_CHANGED_REASON, ErrorCode.PLATFORM_QUERY_FAILED, defused=released)


def _claim_upstream(binding, *, row, event, user_id, now, actor, actor_id, lease_token) -> Order:
    """先接上家单再转正；未接成时只释放这次请求持有的预占。"""
    # ⭐ 复用**进程级会话**：修之前每次接单都 `open_client` ⇒ 新建连接 + 重新登录一次；集中抢单时 N 个人 = N 次登录，是 WAF 封 IP 最直接的触发点。
    #    现在走 `shared_pool().session()`：池里有会话就复用，只有「没有 / 被拒后 invalidate」才会重新登录；同一绑定的接单在池里**串行**，避免两个线程同时用一份会话。
    upstream_claimed = bool((event.payload or {}).get("claimed"))
    claim_requested = False
    if not upstream_claimed:
        _check_unstarted_claim_binding(
            binding, row, event, user_id=user_id, now=now, lease_token=lease_token,
        )
    try:
        with jidanpu.claim_session(binding) as client:
            if not upstream_claimed:
                _check_unstarted_claim_binding(
                    binding, row, event, user_id=user_id, now=now, lease_token=lease_token,
                )
                if not _save_claim_progress(event, lease_token=lease_token, send_started=True):
                    _check_unstarted_claim_binding(
                        binding, row, event, user_id=user_id, now=now, lease_token=lease_token,
                    )
                    return _claim_while_another_executor_owns(row=row, user_id=user_id)
                claim_requested = True
                rec_order_by_internal(client, remote_order_id=row.upstream_order_id)
                upstream_claimed = True
                if not _save_claim_progress(event, lease_token=lease_token, claimed=True):
                    return _claim_while_another_executor_owns(row=row, user_id=user_id)
    except JidanpuError as exc:
        if upstream_claimed:
            raise
        if claim_requested and isinstance(exc, ApiError):
            return _claim_refused(
                binding, row=row, event=event, exc=exc, now=now, lease_token=lease_token,
            )
        if not claim_requested:
            _check_unstarted_claim_binding(
                binding, row, event, user_id=user_id, now=now, lease_token=lease_token,
            )
        # 登录失败和传输失败同样撤回本次预占。镜像与事件必须一起提交，
        # 否则下一位打手可能复用尚未收口的待办。
        reason = HALL_CLAIM_TRANSPORT_RELEASED_REASON
        released = _release_failed_claim(
            row, event, taker_id=user_id, reason=reason, now=now, lease_token=lease_token,
        )
        if not released:
            return _claim_while_another_executor_owns(row=row, user_id=user_id)
        _stamp_binding_error(binding, reason=reason, now=now)
        raise ClaimError(HALL_CLAIM_RELEASED_MESSAGE, ErrorCode.PLATFORM_QUERY_FAILED) from exc
    # 转正需要数据库行锁，退出连接池后再拿锁。
    order = promote_upstream_mirror(
        row, binding=binding, detail=row.upstream_payload, user_id=user_id, now=now,
        actor=actor, actor_id=actor_id, claim_payload=event.payload,
    )
    if order is not None:
        return order
    # 条件更新拿不到 ⇒ 这一行被别人先动过：以**库里的实际值**为准（照 P2 的 ownership 口径）。
    fresh = JidanpuUpstreamHallOrder.objects.filter(pk=row.pk).first()
    if fresh is not None and fresh.last_error == HALL_CLAIM_LINK_CONFLICT_REASON:
        _skip(event, reason=HALL_CLAIM_LINK_CONFLICT_REASON, now=now)
        raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN, defused=True)
    if fresh is not None and fresh.promoted_order_id is not None:
        if fresh.claiming_by_id == user_id:
            return fresh.promoted_order
        raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
    reason = HALL_CLAIM_PROMOTE_RETRY_REASON
    _retry(event, reason=reason, now=now)
    _stamp_binding_error(binding, reason=reason, now=now)
    raise ClaimError(jidanpu_common.TEMPORARY_FAILURE_MESSAGE, ErrorCode.PLATFORM_QUERY_FAILED)


def _owns_claim_event_locked(event, *, lease_token) -> bool:
    return JidanpuSyncEvent.objects.select_for_update().filter(
        _current_outbox_predicate(event), lease_owner=lease_token, payload=event.payload,
    ).exists()


def _resolve_taken_claim(row, event, *, now, lease_token) -> bool:
    from apps.business import upstream_hall_shadow

    payload = event.payload or {}
    claim_key = payload.get("claiming_at")
    if claim_key and claim_key != _claim_reservation_key(row.claiming_at):
        return False
    with transaction.atomic():
        if not _owns_claim_event_locked(event, lease_token=lease_token):
            return False
        updated = JidanpuUpstreamHallOrder.objects.filter(
            pk=row.pk, status=_HALL_CLAIMING, claiming_by_id=payload.get("taker_id"),
            claiming_at=row.claiming_at,
        ).update(status=_HALL_GONE, gone_at=now, last_error=HALL_CLAIM_TAKEN_REASON)
        if not updated:
            return False
        upstream_hall_shadow.refund_for_card(row, now=now)
        _converge(event, reason=HALL_CLAIM_TAKEN_REASON, now=now)
    return True


def _claim_refused(binding, *, row, event, exc, now, lease_token):
    """接单被上家拒：终局（`gone`）或当场放回大厅（D-0335），两条都收敛事件 + 写 `last_error`，然后抛 `ClaimError`。"""
    message = _upstream_message(exc)
    if _looks_taken(message):
        if not _resolve_taken_claim(row, event, now=now, lease_token=lease_token):
            return _claim_while_another_executor_owns(row=row, user_id=(event.payload or {})["taker_id"])
        _stamp_binding_error(binding, reason=HALL_CLAIM_TAKEN_REASON, now=now)
        raise ClaimError(jidanpu_common.ALREADY_TAKEN_MESSAGE, ErrorCode.ORDER_ALREADY_TAKEN)
    # ⭐ D-0335（业务方拍板「消掉抢单锁定」）：认不出的拒绝**当场放回大厅**。依据：`ApiError` = 上游回了失败信封（E-0087）⇒ 这次接单没有被上家收下，单还在上家大厅 ⇒ 回 `listed` 是事实不是赌。旧口径（留 `claiming` + `_retry` 退避）把卡锁成「所有人接不了、打手拿临时失败、押金冻着、解冻闸拦着」，烧完额度后**永久卡死没有任何恢复入口**（F6 / L-5 现场）——正是业务方点名要消掉的「锁住不让别人接」。
    #    ⚠️ **只改请求路**：outbox 消费路 `_consume_hall_claim` 的 `_retry` 支**一字不动**——它是「进程死在打上游之前」的崩溃恢复机制本体（事件 `claimed=False` 由后台补接），事件烧完额度后的残行由 `sync_hall` 的清道夫放回（D-0335 同批）。
    reason = jidanpu_member._upstream_reason(HALL_CLAIM_RELEASED_TO_HALL_REASON, exc)
    # ⭐ release 与 converge **同事务**（理由与竞态形状见 `_claim_upstream`
    #    传输支那边的注释；这里是同一条纪律的第二处落点）。
    taker_id = (event.payload or {})["taker_id"]
    released = _release_failed_claim(
        row, event, taker_id=taker_id, reason=reason, now=now, lease_token=lease_token,
    )
    if not released:
        return _claim_while_another_executor_owns(row=row, user_id=taker_id)
    _stamp_binding_error(binding, reason=reason, now=now)
    raise ClaimError(
        HALL_CLAIM_RELEASED_MESSAGE, ErrorCode.PLATFORM_QUERY_FAILED
    )


# ── 转正（spec §6.1 第 ⑤ 步；全在**同一个事务**里）──────────────────────────────


def _promote_accepted_pool_order(link, *, reservation, binding, row, claim_payload, now, hall_price):
    from apps.business.dailiantong_push import local_status_locked_denial
    from apps.business.jidanpu_assignment_lock import lock_order_change
    from apps.business.order_writes import lock_order_row
    from apps.business.upstream_hall_shadow import refund_for_card

    order = lock_order_row(link.order)
    compatible = (
        claim_payload.get("claimed") is True and row.claiming_at is not None
        and claim_payload.get("claiming_at") == _claim_reservation_key(row.claiming_at)
        and order is not None and order.created_at >= row.claiming_at
        and order.status == OrderStatus.UNASSIGNED and order.published_at is None
        and order.peer_direct_pending is None
        and order.sales_user_id == binding.user_id and order.game_id == row.game_id
        and link.terminated_at is None and not link.in_hall
    )
    if compatible and lock_order_change(order.pk):
        main = Performance.objects.select_for_update().filter(order=order, emp_type=EmpType.MAIN).first()
        if (
            main is not None and main.user_id is None and not main.is_settled
            and local_status_locked_denial(order) is None
            and not Performance.objects.filter(
                order=order, online_payroll_batch__isnull=False, settled_at__isnull=True,
            ).exists()
        ):
            order.status = OrderStatus.IN_PROGRESS
            order.hall_price = hall_price
            order.save(update_fields=["status", "hall_price", "updated_at"])
            refund_for_card(row, now=now)
            return order, main

    # A local order already took another path. Clear the old reservation without handing it to the wrong taker.
    reservation.update(
        status=_HALL_PROMOTED if order is not None else _HALL_GONE,
        promoted_order=order, gone_at=now if order is None else None,
        claiming_by=None, claiming_at=None, last_error=HALL_CLAIM_LINK_CONFLICT_REASON,
    )
    refund_for_card(row, now=now)
    return None, None


def promote_upstream_mirror(
    row, *, binding, detail: dict | None, user_id: int, now: datetime,
    actor: str = MACHINE_ACTOR, actor_id: int | None = None, claim_payload=None,
) -> Order | None:
    """大厅镜像单**转正**成我方订单（spec §6.1 第 ⑤ 步的固定顺序，同一个事务）：① 建 `Order`（`IN_PROGRESS`；字段按 §5.2 的投影口径；钱按 §15.1）→ ② `create_performance_for_order`（`sales_user` 非空 ⇒ 客服那条也建）→ ③ `set_taker(MAIN, 我方打手)`（**这一单是我方打手做的** ⇒ 正常算绩效）→ ④ 建链路行（`in_hall = False`）→ ⑤ 条件更新镜像行 `claiming → promoted` + 订单审计。⭐ **提成归属（A13 / §15.5）**：`sales_user` = **真正去上家把这张单接下的那一份绑定**（= 入参 `binding`，即事件 `payload["upstream_binding_id"]` 记下的那份）的客服；按转正那一刻写死、换绑不追溯（spec §15.5 的代价）。
    ⭐ **归属取的是 `binding` 这个入参，⛔ 不是 `row.owner_binding`**：`row.owner_binding` 的语义是「**此刻**负责拉大厅的那一份」（§8.4：停用 / 换绑时 `sync_hall` 会把 `claiming` 行的 owner 改写成接管者），而**这一单的上游那一跳用的是 `binding` 的凭据**（事件 `payload["upstream_binding_id"]`、`run_outbox` 的取活键也是它）。两边在「A 停用 → B 接管 → A 再启用后消费 A 的旧事件」这条路上会不一致：按 `row.owner_binding` 建链路会造出「链路 / 提成归 B、上游却是 A 的凭据」的分叉 ⇒ 后续 `upstream.complete` 排在 B 上、而 **B 的账号里没有这张单** ⇒ 一直重试到 `abandoned`，上家那张单**永远停在「代练中」**。代价（如实）：同地址绑定轮换期间，提成记在**实际打上游的那一份**的名下，而不是「转正那一刻的地址 owner」——§15.5 的「按当时那一刻写死」不变，只是「那一刻的那一份」以**事件归属**为准（消费侧由 `_consume_hall_claim` 传进来，⛔ 不再读那个会轮换的指针）。
    ⚠️ **`hall_price` = 勾兑后的发单价**（**口径修订**，推翻 T18 的「留空」口径）：= 上家 `recFee` × **本事件那份绑定**的 `hall_price_percent` / 100（默认 100 = 原价接手；算法唯一出处 `upstream_hall_price`）⇒ 打手在大厅里看到的数与转正后拿到的数是**同一个**；⛔ 比例**只作用于发单价**（`total_price ← 上家 recFee`，A9 / §15.1，**不受比例影响**；上家 `price` **一个金额列都不落**）。⭐ **口径修订**：`recFee` 为空 / 认不出 ⇒ 发单价按 **`0.00`** 算（`upstream_hall_price` 恒回 `Decimal`）⇒ **这一路不再有「算不出价」那一档**：绩效照 `0.00` 算（`taker_price` 仍保持 NULL，事后填发单价时 `/hall-price/` 的重算照常生效）。⭐ **（D-0261）**：建单带「订单来源」两列（与录池路**同一份** `_upstream_order_source_fields`：配了 ⇒ `order_source` + `source` 名字快照；未配 ⇒ 全空）。⚠️ `None` = 条件更新拿不到（别人先转正 / 行已不是本次打手的 claiming），或 `binding` / `row.game_id` 缺一个（没有游戏就不能建订单、没有绑定就不知道链路挂谁）⇒ 整个事务回滚，**不建第二张单**（第二道闸是 `(binding, upstream_order_id)` 的唯一约束）。"""
    detail = detail or {}
    if binding is None or row.game_id is None:
        return None
    claim_payload = claim_payload or {}
    taker = User.objects.get(pk=user_id)
    #: ⭐ **转正落库字段（两条支——复用隐藏单 / 新建——同一份，D-0325）**：⛔ 不抄第二处，
    #:    抄了必然漂移，「复用支与新建支逐字同形」就没法钉（用例按同一份 fields 断言）。
    promote_fields = {
        "status": OrderStatus.IN_PROGRESS,
        # 与仓库里同类路（`jidanpu_hall.promote_mirror` 转正）同一档：一律「代练」。
        "order_type": OrderType.BOOST,
        "game": row.game,
        # ⭐ A13 + F1：录单人 / 提成 = **本次事件那份绑定**的客服（两列语义不同：
        # 一个是引用、一个是快照）。
        "sales_user": binding.user,
        "sales_agent": _clip_for_order(user_display_name(binding.user), "sales_agent"),
        "created_by": taker.get_username(),
        # ⭐ 钱（A9 / §15.1 + **口径修订**）：客户价 ← 上家 `recFee`（**不受比例影响**）；
        #    **发单价** = 勾兑后的价（`recFee × 该绑定 hall_price_percent / 100`，算法唯一出处
        #    `upstream_hall_price`）；`recFee` 空 / 认不出 ⇒ `hall_price=0.00`。
        "total_price": (
            Decimal("0.00") if row.remote_rec_fee is None else row.remote_rec_fee
        ),
        "hall_price": upstream_mirror_hall_price(row, binding=binding),
        "project_text": row.project_text,
        "content": _clip_for_order(detail.get("orderContent"), "content"),
        "remark": _clip_for_order(
            detail.get("orderRemark") or row.remote_remark, "remark"
        ),
        "game_role_name": _clip_for_order(
            detail.get("roleName") or row.remote_role_name, "game_role_name"
        ),
        "game_account": _clip_for_order(detail.get("gameAcc"), "game_account"),
        "game_sub_account": _clip_for_order(detail.get("zgameAcc"), "game_sub_account"),
        # ⭐ D-0261：订单来源 + 名字快照（与录池同一份 helper；未配来源 ⇒ {}）。
        **_upstream_order_source_fields(binding),
    }
    with transaction.atomic():
        if not binding.current_identity_rows().select_for_update().exists():
            return None
        reservation = JidanpuUpstreamHallOrder.objects.filter(
            pk=row.pk, status=_HALL_CLAIMING, claiming_by_id=user_id, claiming_at=row.claiming_at,
        )
        claim_key = claim_payload.get("claiming_at")
        if (claim_key and claim_key != _claim_reservation_key(row.claiming_at)) or not (
            reservation.select_for_update().exists()
        ):
            return None
        existing = _link_for_by_remote(binding, row.upstream_order_id)
        main = None
        order = None
        if existing is not None:
            order, main = _promote_accepted_pool_order(
                existing, reservation=reservation, binding=binding, row=row,
                claim_payload=claim_payload, now=now, hall_price=promote_fields["hall_price"],
            )
            if order is None:
                return None
        # ⭐⭐ **D-0325：复用优先——这张卡派生过隐藏单 ⇒ 同一张单接着用**（peer 式 `peer_inbound._promote_claiming_mirror` 范式），⛔ 不「退旧建新」（会在同地址重复录单、还要额外清场）。收编条件 = 隐藏单仍在「未转正」形态（`WAITING_GRAB` / `REFUNDED` / 软删——后两档是卡 gone 退掉 / 被清理后又被接单的复活场景）；**清空 `upstream_hall_card`**（转正后它就是我方真单，必须回到订单列表）。
        #    ⚠️ **`source_order_no` 转正改写（D-0342 现行口径；历史拍板见 docs/DECISIONS.md D-0325）**：复用支把这一列改写成镜像报文里的**上家展示号**（`orderIdView`，经 `_display_source_no` 同一出处、空则不动）——转正后它就是我方真实订单，D-0272 全端口径（搜索 / 列表 / 导出）按展示号；回环语义在转正后**反转**：上家再挂同 view 号的新卡＝真回环，改写后判据反而正确命中（行为矩阵与全部消费点枚举见 `docs/plans/2026-10-30-promote-source-no.md`）。
        #    创建期口径不变：隐藏单仍写内部号（`upstream_hall_shadow._projection_fields`），防回环判据误杀上游原卡。新建支仍用展示号（`_display_source_no`）——两支在转正后**同值**，同地址认领支的回读键（`_existing_remote_order` 按 `effective_source_order_no`）也因此对两支都认得上家原单、不重录。
        view_no = _display_source_no(
            row.upstream_payload if isinstance(row.upstream_payload, dict) else None,
            fallback="",
        )
        #: 空展示号 ⇒ 不碰这一列（回落内部号的旧行为兜底；⛔ 不写空串进非空列）。
        promoted_source_no = {"source_order_no": view_no} if view_no else {}
        shadow = Order.all_objects.filter(upstream_hall_card=row).first() if order is None else None
        if shadow is not None and (
            shadow.deleted_at is not None
            or shadow.status in (OrderStatus.WAITING_GRAB, OrderStatus.REFUNDED)
        ):
            updated = Order.all_objects.filter(pk=shadow.pk).update(
                deleted_at=None,
                deleted_by="",
                upstream_hall_card=None,
                updated_at=now,
                **promote_fields,
                **promoted_source_no,
            )
            if updated:
                order = shadow
                from apps.business.order_writes import queue_externalpay_recheck
                queue_externalpay_recheck(order)
                for name, value in promote_fields.items():
                    setattr(order, name, value)
                for name, value in promoted_source_no.items():
                    setattr(order, name, value)
                order.deleted_at = None
                order.deleted_by = ""
                order.upstream_hall_card = None
                order.updated_at = now
        if order is None:
            if shadow is not None:
                # 隐藏单在别的状态（`IN_PROGRESS` 等说明数据被手改——正常生命周期里 `upstream_hall_card` 非空只可能是 WAITING_GRAB / REFUNDED）⇒ 不收编：响亮留痕后照新建支，那张畸形行留在隐藏面等人工处置。
                logger.warning(
                    "上家镜像隐藏单状态不可收编，转正走新建支：order_no=%s status=%s mirror=%s",
                    shadow.order_no, shadow.status, row.pk,
                )
            order = Order.objects.create(
                order_no=generate_order_no(),
                #: ⭐ （业务方口径改准，与录池路同一份 helper）：来源单号 = **上家展示号**
                #:    （镜像报文里的 `orderIdView` 优先，缺 ⇒ 回落 `upstream_order_id`＝`orderId`）。
                source_order_no=_display_source_no(
                    row.upstream_payload if isinstance(row.upstream_payload, dict) else None,
                    fallback=row.upstream_order_id,
                ),
                **promote_fields,
            )
        if main is None:
            main = create_performance_for_order(order)
        try:
            set_taker(main, taker)
        except MissingPriceError:
            # ⭐ **F2留下的窄口径**：这一支**只吞**「两边都没价」那一档（`MissingPriceError`），别的 `AmountError` 原样上抛。⚠️ **口径修订之后它在本路径上已经够不到了**：上家没给价时 `upstream_hall_price` 回 `0.00`（⛔ 不再回 `None`）⇒ 建出来的单 `hall_price` 恒有值 ⇒ `effective_taker_price` 不会再抛 `MissingPriceError`——留着它是**防御性写法、不是承重逻辑**（去掉它没有任何用例会红），真正要守的「不许吞别的金额错」由 `tests/test_jidanpu_upstream_consume.py` 的 `test_a_different_amount_error_on_promotion_is_not_swallowed` 钉着。
            #    `set_taker` 是「先落 `user` / `claimed_at`、**最后**才重算」⇒ 万一真走到这里，那两个值**已经写进去了**，`taker_price` 保持 `NULL`、`temp_amount` 仍是 `create_performance_for_order` 建行时的值。
            pass
        link = existing or _ensure_link(
            binding, order, remote_id=row.upstream_order_id, in_hall=False, now=now
        )
        JidanpuUpstreamOrderLink.objects.filter(pk=link.pk).update(last_out_at=now)
        updated = reservation.update(status=_HALL_PROMOTED, promoted_order=order)
        # ⛔ 离厅快照（D-0296）：`claiming → promoted` **不写**——这张镜像离开大厅
        #    的那一刻是 `listed → claiming`（`claim_upstream_mirror` 里已写过
        #    `claimed` 卡），转正只是同一次接单的落库收尾，再写就是一单双卡。
        if updated != 1:
            transaction.set_rollback(True)
            return None
        sync_actor = claim_payload.get("claim_sync_actor") or None
        record_successful_claim(
            order, taker, price=main.taker_price if main.taker_price is not None else order.hall_price,
            channel=claim_payload.get("claim_channel", ""),
            downstream=claim_payload.get("claim_downstream", ""), sync_actor=sync_actor,
        )
        # ⭐ E-0283（计划 `2026-10-24-member-assign-on-mirror-claim-and-pic-repush`）：上家镜像转正 ⇒ **立即**给接手人的启用 member 绑定排 `member.assign`（秒级进对方系统，替代 180 秒对账 `to_record` 补录的那段窗口）；与内部大厅接单那一路（`claim.py::claim_order`）是**同一个入口、同一份判据**（`queue_claimed`：接手人是绑定主人 ⇒ assign；已推出去的别的绑定 ⇒ withdraw）。
        #    ⭐ **D-0227 边界判据（⛔ 别读混）**：`suppress_outbound_sync=False`——本插入点只可能来自**上家线**的镜像转正（`JidanpuUpstreamHallOrder` 全仓唯一 create 在 `sync_hall`；member/②式/peer 都不产镜像行，无第三种来源）⇒ 接手人绑定收到的是**交付**、不是回声；`suppress=True` 的既有调用点（②式 machine_claim 的镜像接手 / 上家线回拉写回）**都不经本插入点**，抑制现状一字不动。判据一句话：**上家线转正 ⇒ 推；member/②式来源的接手 ⇒ 抑制（D-0227 不动）**。
        #    ⚠️ 位置（M1）：事务块**内**、审计 `record(...)` 之后（`return order` 在块外）⇒ 事件与订单同生共死（E-0022）；转正是上面那次**条件更新的单赢家**（`updated == 1`；输家已 `set_rollback` + `return None`）⇒ 走到这里的那一次调用就是建单的那一次 ⇒ 天然「恰好排一次」，worker 重试路径另有链路/终态幂等闸不会二次 promote。副作用清点：接手人无启用绑定 ⇒ 不排队（`_queue_unlist_if_in_hall` 无链路返回 0、withdraw 半边对新单为空 ⇒ 两条空查询，量级可忽略）。
        jidanpu_member.queue_claimed(
            order, taker_id=user_id, suppress_outbound_sync=False,
        )
        from apps.internal_chat.order_groups import assignment_changed

        assignment_changed(order, taker, jidanpu_only=True)
        if not sync_actor:
            from apps.internal_chat.order_grab_qr_notices import enqueue

            enqueue(order, user_id=taker.pk, claimed_at=main.claimed_at)
    return order


# ── 消费者（`consume_event` 三态 / `run_outbox` 一轮）──────────────────────────


def _binding_of(event):
    """事件归属的绑定；`payload["upstream_binding_id"]` 不在 / 不是 int / 行没了 ⇒ `None`。"""
    raw = (event.payload or {}).get("upstream_binding_id")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    return JidanpuUpstreamBinding.objects.filter(pk=raw).first()


def _current_outbox_predicate(event) -> Q:
    current = Q(pk=event.pk, status__in=("pending", "failed"))
    if event.kind == KIND_COMPLETE:
        current &= Q(payload=event.payload)
    return current


def _finish(event, *, status: str, reason: str, now: datetime) -> str:
    updated = JidanpuSyncEvent.objects.filter(_current_outbox_predicate(event)).update(
        status=status, reason=reason[:255], processed_at=now,
    )
    return status if updated else "skipped"


def _converge(event, *, reason: str, now: datetime) -> str:
    """**终局**的「不动作」：收成 `done` + 人话原因（⛔ 不记 `failed`——那会被反复取活重试）。"""
    return _finish(event, status="done", reason=reason, now=now)


def _skip(event, *, reason: str, now: datetime) -> str:
    """终态的「不动作」：⛔ 不调上游、⛔ 不计 `attempts`、⛔ 不再重试。"""
    return _finish(event, status="skipped", reason=reason, now=now)


def _retry(event, *, reason: str, now: datetime) -> str:
    """预期失败：`failed` + `attempts += 1`（用 `F()` 自增，并发下不互相覆盖）+ 人话原因。⚠️ 到 `OUTBOX_MAX_ATTEMPTS` 时在那句原因后面**追加**「不再自动重试」——事件仍是 `failed`（留台账待人看），但 `run_outbox` 不会再取它（`abandoned`）。"""
    attempts = (event.attempts or 0) + 1
    text = reason
    if attempts >= OUTBOX_MAX_ATTEMPTS:
        text = f"{reason}{ABANDONED_SUFFIX.format(n=attempts)}"
    updated = JidanpuSyncEvent.objects.filter(_current_outbox_predicate(event)).update(
        status="failed", reason=text[:255], processed_at=now,
        attempts=F("attempts") + 1,
    )
    if not updated:
        return "skipped"
    event.status = "failed"
    event.reason = text[:255]
    return "failed"


def _stamp_binding_error(binding, *, reason: str, now: datetime) -> None:
    """把失败人话写进绑定的 `last_error`（**收窄写入**，⛔ 不整行 `save()`；spec §9.1）。
    ⚠️ **空串守卫**：`reason` 为空 ⇒ 写 `ROUND_UNKNOWN_FAILURE_REASON` 那句固定人话，⛔ **绝不写空串**——`last_error=""` 配一个**新的** `last_error_at` 会让页面 / 告警读到「某一刻失败过、可原因栏是空的」，而它**与「从没失败过」长得一模一样**（同口径先例：本模块 `_round_failure_reason` 与 `jidanpu_member._stamp_poll_health` 的 `... or ROUND_UNKNOWN_FAILURE_REASON`；`tests/test_jidanpu_upstream_schedule.py` 的空 `hall.reason` 用例钉住本档）。"""
    text = reason or ROUND_UNKNOWN_FAILURE_REASON
    binding.current_identity_rows().update(
        last_error=text[:ROUND_REASON_MAX], last_error_at=now
    )


def _stamp_binding_ok(binding, *, now: datetime) -> None:
    """**一轮读路完整成功** ⇒ 盖 `last_ok_at` 并清掉旧错（**收窄写入**）。口径与 `run_outbox` 成功时的既有 `_stamp_health`（`last_ok_at=now` + `last_error=""` + `last_error_at=None`）**同一份**：运维判「这条线在不在跑」看的就是 `last_ok_at` ⇒ **只跑读取、没有待发事件的绑定不许永远显示「从未成功」**。
    ⚠️ **判据不在这里**：「这一轮读路算不算完整成功」要看两条读路各自的到点情况与三段各自的异常边界 ⇒ 只有调度那一层（`jidanpu_sync._run_upstream_binding`）判得了，它**在整轮末尾**调本函数。⛔ **不要在 `sync_hall` / `sync_my_orders` 里各自盖**：段与段有先后（outbox → 大厅 → 我的订单），先成功的段会把**后失败**那一段刚写下的 `last_error` 清掉 ⇒ 那条错会**静默消失**（顺序敏感正是判据必须留在末尾的原因）。"""
    binding.current_identity_rows().update(
        last_ok_at=now, last_error="", last_error_at=None
    )


def _stamp_binding_run(binding, *, now: datetime) -> None:
    """记「这一轮**跑过**这一行」（**收窄写入**，只动 `last_run_at`）。⚠️ **存在的理由（T8 收口）**：②式（官方API）那一行**不跑 outbox**（写接口第一期不做，spec §15-N3），而 `last_run_at` 原本只由 outbox / 两条登录式读路推进 ⇒ 不补这一笔，②式绑定的 `last_run_at` 会**永远为空**：页面上显示「从未运行」，而它其实每 3 秒都在跑（回调消费 + 兜底对账）——**那是说假话**。
    ⚠️ 口径与 `run_outbox` 空转那一支（`update(last_run_at=now)`）**逐字相同**：记的是「上次**跑**」而不是「上次成功」（成功看 `last_ok_at`，失败也推进这一列）；⛔ 不要拿它当「成功」——它一个字节都不碰 `last_ok_at` / `last_error`。"""
    binding.current_identity_rows().update(last_run_at=now)


def _pic_block_reason(outcome) -> str:
    """A5 闸门那句人话：**点名是哪一步、哪一张**（`FailedShot.reason` 已含，⛔ 不含对象名）。"""
    if outcome.failed:
        return outcome.failed[0].reason
    return outcome.reason or COMPLETE_PIC_BLOCKED_REASON


def consume_event(event, *, client=None, http=None, oss_post=None) -> str:
    """消费一条事件，回 `"done"` / `"failed"` / `"skipped"`（三态与事件行一致）。⚠️ `client` / `http` / `oss_post` 是**为可测性留的注入口**（照 `detect_binding` 的形状）：给了就用它、没给就自己 `jidanpu.open_client_with_http(binding)` 建连；⛔ 它们**不是**业务参数。
    ⚠️ 结构性守卫：只消费 `pending` / `failed`（纵深防御，⛔ 不依赖调用方记得筛）；`done` / `skipped` 是终态，直接原样返回、一行不写。
    ⚠️ **未预期**异常不在这里翻译（让它响亮抛）；**预期**失败走 `_retry`（failed + attempts+1）。"""
    if event.status not in ("pending", "failed"):
        return event.status
    binding = _binding_of(event)
    now = timezone.now()
    if binding is None:
        return _skip(event, reason=OUTBOX_BINDING_MISSING_REASON, now=now)
    handler = _HANDLERS.get(event.kind)
    if handler is None:
        # 未知 kind = 写方 / 接线坏了 ⇒ 响亮抛（`run_outbox` 的逐事件边界会记下来、继续下一条）。
        raise ValueError(f"本消费者不认识的台账类型：{event.kind}")
    if client is None:
        with jidanpu.open_client_with_http(binding) as (sdk, real_http):
            return handler(
                event, binding=binding, client=sdk, http=http or real_http,
                now=now, oss_post=oss_post,
            )
    return handler(
        event, binding=binding, client=client, http=http, now=now, oss_post=oss_post
    )


def _owner_mismatch_reason(binding) -> str:
    """归属不一致那句台账人话；事件绑定那位客服**已停用**时**点名**（spec §15.5 第 3 条 / R24）。⚠️ **去重口径（本线已定，⛔ 不放大成 N 行）**：停用这一事实**拼进同一句 `reason`**，⛔ 不为它单开一条台账、⛔ 不把事件绑定 / 客服身份写进 `_note_once_ref` 的键——键里含 `reason` ⇒ 「同地址 + 同单号 + 同一种停用状态」仍然只有一行；只有停用状态变了（启用 ⇄ 停用）才另记一行，那是**换了事实**，照 `note_once` 的口径本来就该新记一条（依据：`_note_once_ref` 的键口径 M-3——刻意不把绑定放进键，那句原因身份无关、第二行只会是逐字重复）。
    ⚠️ 判据只认 `binding.user.is_active`（不认 `binding.enabled`）：理由见上方常量的注释。"""
    if not binding.user.is_active:
        return HALL_CLAIM_OWNER_MISMATCH_REASON + HALL_CLAIM_OWNER_MISMATCH_INACTIVE_SUFFIX
    return HALL_CLAIM_OWNER_MISMATCH_REASON


def _consume_hall_claim(event, *, binding, client, http, now, oss_post=None) -> str:
    """`upstream.hall_claim`：在上家大厅把这张单接下，成功才转正（spec §6.1）。⚠️ **幂等**（brief 的「已接过的链路里已有标记 ⇒ 不重复接」）：④ 先看链路——同一 `(binding, 上家单号)` 已有链路 ⇒ 这一单**早就在我方系统里**，⛔ 一次上游都不打。
    ⚠️ **A16**：成功路径**只有** `rec_order_by_internal` 一次写（⛔ 没有任何状态写）。"""
    payload = event.payload or {}
    mirror_id = payload.get("mirror_id")
    taker_id = payload.get("taker_id")
    row = (
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror_id).first()
        if isinstance(mirror_id, int) and not isinstance(mirror_id, bool)
        else None
    )
    if row is None:
        return _skip(event, reason=HALL_CLAIM_MIRROR_MISSING_REASON, now=now)

    if not (
        binding.current_identity_rows().filter(enabled=True).exists()
        and JidanpuSyncEvent.objects.filter(_current_outbox_predicate(event)).exists()
    ):
        return "skipped"
    lease_token = jidanpu_common.current_event_lease_owner(event)
    if (
        row.upstream_base_url != binding.jidanpu_base_url
        or payload.get("binding_revision", binding.identity_revision) != binding.identity_revision
    ):
        if lease_token is not None and row.claiming_at is not None and _release_rebound_unstarted_claim(
            row, event, taker_id=taker_id, reserved_at=row.claiming_at, now=now, lease_token=lease_token,
        ):
            return "skipped"
        return _skip(event, reason=BINDING_CHANGED_REASON, now=now)

    # ① 幂等：链路里已有标记 ⇒ 已经在我方系统里了（⛔ 不重复接、⛔ 不打上游）。
    existing = _link_for_by_remote(binding, row.upstream_order_id)
    if existing is not None and not payload.get("claimed"):
        _converge_promoted(row, order=existing.order, now=now)
        return _converge(event, reason=HALL_CLAIM_ALREADY_REASON, now=now)

    # ② 行的状态：终态 ⇒ 收敛；不在本次事件的打手手上 ⇒ 让开（⛔ 不硬抢别人的单）。
    if row.status == _HALL_PROMOTED:
        return _converge(event, reason=HALL_CLAIM_ALREADY_REASON, now=now)
    if row.status in (_HALL_GONE, _HALL_SKIPPED):
        return _skip(event, reason=HALL_CLAIM_ROW_GONE_REASON, now=now)
    if row.status != _HALL_CLAIMING or row.claiming_by_id != taker_id:
        return _skip(event, reason=HALL_CLAIM_ROW_NOT_CLAIMING_REASON, now=now)

    # ②b ⭐ **F1 的归属守卫**：转正 / 建链路 / 提成一律按**本事件那份绑定**（= 这一跳打上游用的凭据；`run_outbox` 的取活键也是它），⛔ 不读 `row.owner_binding`——那是「**此刻**谁负责拉大厅」的指针，停用 / 换绑时会被 `sync_hall` 改写成接管者（§8.4）。
    #     两边不一致（含 owner 已被 `SET_NULL`）时**照样按事件绑定转正**，但**⛔ 不静默**：留一条人话台账请人工核对。依据与代价见 `promote_upstream_mirror` 的 docstring（不这么做就会造出「链路 / 提成归 B、上游却是 A 的凭据」的分叉）。
    #     ⭐ F1 收口（§15.5 第 3 条 / R24）：事件绑定那位客服**已停用**时，这句人话要**点名**它（提成仍归停用账号 ⇒ 运维得知道找谁对账）——理由与去重口径见 `_owner_mismatch_reason`。
    if row.owner_binding_id != binding.pk:
        _note_once_ref(
            target_type="hall",
            target_ref=f"{row.upstream_base_url}/{row.upstream_order_id}"[:_TARGET_REF_MAX],
            kind=KIND_HALL_OWNER_MISMATCH, status="done",
            reason=_owner_mismatch_reason(binding),
            payload={"upstream_binding_id": binding.pk},
        )

    # ③ 打上游（⛔ 只有这一跳；A16：接单动作本身就让对方变「代练中」）。
    if not payload.get("claimed"):
        if not _save_claim_progress(event, lease_token=lease_token, send_started=True):
            return "skipped"
        try:
            rec_order_by_internal(client, remote_order_id=row.upstream_order_id)
        except ApiError as exc:
            message = _upstream_message(exc)
            if _looks_taken(message):
                return "done" if _resolve_taken_claim(row, event, now=now, lease_token=lease_token) else "skipped"
            with transaction.atomic():
                if not _owns_claim_event_locked(event, lease_token=lease_token):
                    return "skipped"
                return _retry(
                    event,
                    reason=jidanpu_member._upstream_reason(HALL_CLAIM_UNCLEAR_REFUSAL_REASON, exc),
                    now=now,
                )
        if not _save_claim_progress(event, lease_token=lease_token, claimed=True):
            return "skipped"

    # ④ 转正（同一事务；条件更新拿不到 ⇒ 别人先动过）。
    order = promote_upstream_mirror(
        row, binding=binding, detail=row.upstream_payload, user_id=taker_id, now=now,
        actor=MACHINE_ACTOR, actor_id=None, claim_payload=event.payload,
    )
    if order is None:
        fresh = JidanpuUpstreamHallOrder.objects.filter(pk=row.pk).first()
        if fresh is not None and fresh.last_error == HALL_CLAIM_LINK_CONFLICT_REASON:
            return _skip(event, reason=HALL_CLAIM_LINK_CONFLICT_REASON, now=now)
        if fresh is not None and fresh.status == _HALL_PROMOTED:
            return _converge(event, reason=HALL_CLAIM_ALREADY_REASON, now=now)
        return _retry(event, reason=HALL_CLAIM_PROMOTE_RETRY_REASON, now=now)
    _converge_promoted(row, order=order, now=now)
    return _converge(event, reason=HALL_CLAIM_DONE_REASON, now=now)


def _converge_promoted(row, *, order, now) -> None:
    """把还停在 `claiming` 的镜像行收敛成 `promoted`（已是终态 ⇒ 一个字不改）。"""
    JidanpuUpstreamHallOrder.objects.filter(pk=row.pk, status=_HALL_CLAIMING).update(
        status=_HALL_PROMOTED, promoted_order=order
    )


COMPLETE_ORDER_DELETED_REASON = "我方订单已删除，本次不向上家提交验收"
COMPLETE_REPLACED_REASON = "订单已进入另一次完成流程，本次旧待办不提交验收"


def _completion_version(order) -> str | None:
    return order.completed_at.isoformat() if order.completed_at is not None else None


def _completion_target(event, binding, *, lock=False):
    payload = event.payload or {}
    link_id = payload.get("link_id")
    if type(link_id) is not int:
        return None
    current = binding.current_identity_rows().filter(enabled=True)
    if lock:
        current = current.select_for_update()
    if not current.exists():
        return None
    links = JidanpuUpstreamOrderLink.objects.filter(pk=link_id, binding=binding).select_related("order")
    if lock:
        links = links.select_for_update(of=("self", "order"))
    link = links.first()
    if (
        link is None
        or event.target_ref != f"{link.binding_id}|{link.order.order_no}"
        or payload.get("upstream_order_id", link.upstream_order_id) != link.upstream_order_id
    ):
        return None
    return link


def _completion_block_reason(link, *, version) -> str:
    if link is None:
        return COMPLETE_LINK_MISSING_REASON
    if link.is_terminated:
        return COMPLETE_TERMINATED_REASON
    if link.order.deleted_at is not None:
        return COMPLETE_ORDER_DELETED_REASON
    if link.order.status != OrderStatus.COMPLETED:
        return COMPLETE_NOT_DONE_REASON.format(status=status_text(link.order.status))
    if _completion_version(link.order) != version:
        return COMPLETE_REPLACED_REASON
    return ""


def _confirm_completion(event, *, binding, version, reason, now) -> str:
    with transaction.atomic():
        link = _completion_target(event, binding, lock=True)
        blocked = _completion_block_reason(link, version=version)
        if blocked:
            return _skip(event, reason=blocked, now=now)
        outcome = _converge(event, reason=reason, now=now)
        if outcome == "done" and (
            link.completed_submitted_at is None
            or (link.order.completed_at is not None
                and link.completed_submitted_at < link.order.completed_at)
        ):
            JidanpuUpstreamOrderLink.objects.filter(pk=link.pk).update(
                completed_submitted_at=now, last_out_at=now,
            )
        return outcome


def _consume_complete(event, *, binding, client, http, now, oss_post=None) -> str:
    """先回读并补图，再提交验收；换绑、删单与重新完成都会使旧路径失效。"""
    link = _completion_target(event, binding)
    if link is None:
        return _skip(event, reason=COMPLETE_LINK_MISSING_REASON, now=now)
    # 没存完单时间的历史待办用本次开始时的时间校对后续回包。
    version = (event.payload or {}).get("completed_at", _completion_version(link.order))
    blocked = _completion_block_reason(link, version=version)
    if blocked:
        return _skip(event, reason=blocked, now=now)
    if not JidanpuSyncEvent.objects.filter(_current_outbox_predicate(event)).exists():
        return "skipped"
    order = link.order
    if link.completed_submitted_at is not None and (
        order.completed_at is None or link.completed_submitted_at >= order.completed_at
    ):
        return _converge(event, reason=COMPLETE_ALREADY_REASON, now=now)

    remote_id = link.upstream_order_id
    try:
        row = _remote_row(client, remote_id)
    except ApiError as exc:
        return _retry(
            event,
            reason=jidanpu_member._upstream_reason(COMPLETE_READBACK_FAILED_REASON, exc),
            now=now,
        )
    if row is None:
        return _retry(event, reason=REMOTE_ROW_MISSING_REASON, now=now)
    if _token_of(row.get("orderSta")) in SUBMITTED_REMOTE_TOKENS:
        return _confirm_completion(event, binding=binding, version=version,
                                   reason=COMPLETE_ALREADY_REASON, now=now)

    link = _completion_target(event, binding)
    blocked = _completion_block_reason(link, version=version)
    if blocked:
        return _skip(event, reason=blocked, now=now)

    from apps.business import jidanpu_upstream_pics

    outcome = jidanpu_upstream_pics.push_order_screenshots(
        client, order=link.order, binding=binding, link=link, http=http, now=now,
        oss_post=oss_post, skip_if_remote_has_pic=False, only_missing=True,
    )

    link = _completion_target(event, binding)
    blocked = _completion_block_reason(link, version=version)
    if blocked:
        return _skip(event, reason=blocked, now=now)
    if not JidanpuSyncEvent.objects.filter(_current_outbox_predicate(event)).exists():
        return "skipped"
    if outcome.blocks_completion:
        return _retry(event, reason=_pic_block_reason(outcome), now=now)

    try:
        submit_completion(client, remote_order_id=remote_id)
    except ApiError as exc:
        return _retry(
            event,
            reason=jidanpu_member._upstream_reason(COMPLETE_FAILED_REASON, exc),
            now=now,
        )

    try:
        confirmed = _remote_row(client, remote_id)
    except ApiError as exc:
        return _retry(
            event,
            reason=jidanpu_member._upstream_reason(COMPLETE_CONFIRM_FAILED_REASON, exc),
            now=now,
        )
    if confirmed is None or _token_of(confirmed.get("orderSta")) not in SUBMITTED_REMOTE_TOKENS:
        return _retry(event, reason=COMPLETE_NOT_CONFIRMED_REASON, now=now)
    return _confirm_completion(event, binding=binding, version=version,
                               reason=COMPLETE_DONE_REASON, now=now)


_HANDLERS = {
    KIND_HALL_CLAIM: _consume_hall_claim,
    KIND_COMPLETE: _consume_complete,
}


# ── 一轮 outbox（调度层逐绑定调它一次）────────────────────────────


def _backoff_elapsed(event, *, now) -> bool:
    """这条失败事件的退避窗过去了吗（`min(2^attempts × 基准, 上限)`）。"""
    attempts = event.attempts or 0
    if attempts <= 0:
        return True
    wait = min(
        (2 ** attempts) * OUTBOX_BACKOFF_BASE_SECONDS, OUTBOX_BACKOFF_MAX_SECONDS
    )
    anchor = event.processed_at or event.created_at
    return anchor + timedelta(seconds=wait) <= now


def _unexpected_reason(event, exc: BaseException) -> str:
    """未预期异常的人话原因（⛔ 只带异常**类型名**，不带正文）。"""
    ref = (event.target_ref or "")[:120]
    return UNEXPECTED_EVENT_REASON.format(
        exc=type(exc).__name__, kind=event.kind, ref=ref
    )


def run_outbox(binding, *, now, client=None, http=None, oss_post=None) -> OutboxStats:
    """取该绑定的在飞事件、逐个消费，回一份 `OutboxStats`（spec §6.4 / §10）。⚠️ **逐事件异常边界**：一个事件抛**未预期**异常 ⇒ 记下来（`failed` + 退避）、继续下一个；全部跑完后把失败清单装进 `UpstreamOutboxUnexpectedFailures` **响亮抛出**（⛔ 不静默）；停用（`enabled=False`）⇒ 一个事件都不动（spec §8.4，`paused=True`）；到 `OUTBOX_MAX_ATTEMPTS` 不再重试（`abandoned`），留台账待人看。
    ⚠️ **每个事件都要先赢下执行租约**（`jidanpu_common.execution_lease`，与 P2 线**同一份**）：请求路径与后台轮看的是同一条在飞事件 ⇒ 没有它就会各打一次上游（P2 修复波 3 的实测）。
    ⚠️ 健康列一律 `.update()` **收窄写入**（整行 `save()` 会把并发写进去的别的列覆盖回旧值）；`client` / `http` / `oss_post` 同 `consume_event`：只为可测性注入；真实形态下一轮只建一次连，而且**没有在飞事件时一次连都不建**。"""
    now = now or timezone.now()
    if not binding.enabled:
        return OutboxStats(paused=True)
    events = list(
        JidanpuSyncEvent.objects.filter(
            direction="out", kind__in=OUTBOX_KINDS,
            payload__upstream_binding_id=binding.pk,
            status__in=("pending", "failed"),
        ).order_by("created_at", "id")
    )
    if not events:
        # 空转：只打「上次运行时刻」，⛔ 不建连、⛔ 不动成功 / 失败两列。
        binding.current_identity_rows().update(last_run_at=now)
        return OutboxStats()
    if client is None:
        with jidanpu.open_client_with_http(binding) as (sdk, real_http):
            tally, unexpected, failures = _consume_all(
                events, binding=binding, client=sdk, http=http or real_http,
                now=now, oss_post=oss_post,
            )
    else:
        tally, unexpected, failures = _consume_all(
            events, binding=binding, client=client, http=http, now=now,
            oss_post=oss_post,
        )
    stats = OutboxStats(**tally)
    _stamp_health(binding, now=now, stats=stats, unexpected=unexpected, failures=failures)
    if unexpected:
        raise UpstreamOutboxUnexpectedFailures(unexpected, stats=stats)
    return stats


def _consume_all(events, *, binding, client, http, now, oss_post=None):
    """逐个消费（⛔ 顺序即 `created_at, id`）；回 `(计数 dict, 未预期原因清单, 逐条失败人话)`。"""
    tally = {"done": 0, "failed": 0, "skipped": 0, "deferred": 0, "abandoned": 0}
    unexpected: list[str] = []
    failures: list[str] = []
    for event in events:
        if (event.attempts or 0) >= OUTBOX_MAX_ATTEMPTS:
            tally["abandoned"] += 1
            continue
        if not _backoff_elapsed(event, now=now):
            tally["deferred"] += 1
            continue
        with jidanpu_common.execution_lease(
            event, now=now, lease_seconds=jidanpu_common.OUTBOX_LEASE_SECONDS
        ) as token:
            if token is None:
                # 别人正持有租约（多半是同一次请求路径）⇒ 这一轮让开，下一轮再来。
                tally["deferred"] += 1
                continue
            if event.kind == KIND_HALL_CLAIM:
                event.refresh_from_db()
                if event.lease_owner != token or event.status not in ("pending", "failed"):
                    tally["skipped"] += 1
                    continue
            try:
                outcome = _HANDLERS[event.kind](
                    event, binding=binding, client=client, http=http,
                    now=now, oss_post=oss_post,
                )
            except Exception as exc:  # noqa: BLE001 —— 逐事件异常边界（照既有 outbox 的形状）
                reason = _unexpected_reason(event, exc)
                _retry(event, reason=reason, now=now)
                unexpected.append(reason)
                failures.append(event.reason)
                tally["failed"] += 1
                continue
        if outcome == "skipped":
            tally["skipped"] += 1
        elif outcome == "done":
            tally["done"] += 1
        else:
            tally["failed"] += 1
            failures.append(event.reason)
    return tally, unexpected, failures


def _round_failure_reason(stats: OutboxStats, unexpected: list[str],
                          failures: list[str] = ()) -> str:
    """把本轮失败压成一句人话（⛔ 不写空串：空 `last_error` + 新时间戳看起来像「成功没清干净」）。"""
    parts = [*failures, *unexpected]
    if stats.failed:
        parts.append(f"本轮有 {stats.failed} 条台账事件仍失败，请查台账")
    if stats.abandoned:
        parts.append(
            f"有 {stats.abandoned} 条事件已到重试上限、不再自动重试，请人工核对"
        )
    unique = [part for part in dict.fromkeys(parts) if part]
    return "；".join(unique)[:ROUND_REASON_MAX] or ROUND_UNKNOWN_FAILURE_REASON


def _stamp_health(binding, *, now, stats: OutboxStats, unexpected: list[str],
                  failures: list[str] = ()) -> None:
    """整轮的记账（⛔ 收窄写入）：失败 ⇒ `last_error`；有成功且无失败 ⇒ `last_ok_at` + 清旧错。"""
    fields: dict = {"last_run_at": now}
    if unexpected or stats.failed or stats.abandoned:
        fields["last_error"] = _round_failure_reason(stats, unexpected, failures)
        fields["last_error_at"] = now
    elif stats.done:
        fields["last_ok_at"] = now
        fields["last_error"] = ""
        fields["last_error_at"] = None
    binding.current_identity_rows().update(**fields)

