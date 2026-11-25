"""下游鸡蛋铺线（团团通 → 下家鸡蛋铺，代练员个人绑定）的领域层入口，七块（spec=`docs/plans/2026-09-26-downstream-jidanpu-sync.md`）：①绑定期检测（§9.1/D-0161，预检不是保证——真判据=写失败要响亮报错）②不变式引擎（§5.1/§5.2/§5.4 期望形态三档+对账差集，纯逻辑 ⛔ 不打上游、不写台账与链路行）③outbox 消费者（差集变真实动作，链路行与事件状态落库）④触发点（业务写入同事务只写台账 ⛔ 不打上游，唯一入口 `queue_member_event`）⑤日志快路+自动接手（`poll_log`，⛔ 不解析 `remark`，真被接走 ⇒ `claim_order` 接手）⑥状态回拉慢路（`pull_statuses`，plan `docs/plans/2026-10-03-member-status-scope.md`；「立即同步」走的就是它 ⛔ 没有第二份实现）⑦订单截图双向同步（`jidanpu_upstream_pics`；传图失败不挡派单、拉图失败不挡待验收）。
触发点排队口全枚举（判据各见各自 docstring ⛔ 不散在视图里）：`queue_hall_launch`/`queue_hall_return`/`queue_assign`/`queue_claimed`/`queue_withdraw`/`queue_status_out`/`queue_price_update`/`queue_project_update`/`queue_percent_reprice`（D-0326/E-0301，判据住在唯一调用点）/`queue_pic_repush_for_ready_screenshots`（E-0282）/`reconcile_and_queue`。消费三条硬口径：有「撤下」（②式没有此动作 ⇒ 终态跳过一次上游不打，`OutboxLine.unlist_unavailable_reason`/`jidanpu.RELEASE_TO_HOME_PATH`）；必须消费 `to_record` ⇒ 只录单 ⛔ 不抛单（否则「启用时补推」静默漏单）；录单前地址级去重 ⛔ 不录第二张。「有效来源单号」唯一出处=`effective_source_order_no`，录单/回读/认领预读/日志快路四处必须都走它（⛔ 不许各写一份）。
状态回拉硬口径：处理集合=我方非终态单（**「已退单不写」是钱的承重判据**——写回待验收=复活退掉的单，退款路是裸 `update()` 跑不到退款结清）；已结账不回退（复用 `jidanpu_common._b1_status_rollback_blocked` ⛔ 不另写，E-0148/D-0156）；拉不全 ⇒ 本轮一行不写+不推水位；窗口=回看 `PULL_MIN_WINDOW_DAYS` 天（有水位 `水位−本值`、无水位 `now−本值`），跨度卡 460 天服务端硬限；缺节点响亮 ⛔ 不静默。⛔ 硬约束：只读上游、不吞上游异常——「上游读不到/判不了权限」=检测本身失败响亮上行 ⛔ 不伪装成「没有权限」（那会让人去改一个没坏的权限设置）。"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from jidanpu.errors import ApiError, AuthError, JidanpuError, UpstreamBlockedError

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_assignment_lock, jidanpu_common, jidanpu_proxy, jidanpu_reconcile, jidanpu_relaunch
from apps.business.claim_channel import record_successful_claim
from apps.business.claim import ClaimError, claim_order
from apps.business.commission import CommissionError
from apps.business.game_scope import game_ids_q, games_of

logger = logging.getLogger(__name__)

#  ⛔ 只借模型级常量 `SOURCE_NAME_DEFAULT`（来源名默认值的**唯一出处**）：本线那张表的
#  同名默认值就是它，⛔ 不在这里再写一份字面值（`models` 不 import 本模块，不会成环）。
#  （原名住在租户级 `JidanpuBinding` 上，随那张表的拆除搬到 `models` 模块级；同名、同值、同语义。）
from apps.business.peer_sync import JIDANPU_DIRECT_PENDING_PLATFORM
from apps.business.models import (
    SOURCE_NAME_DEFAULT,
    ApprovalKind,
    ApprovalStatus,
    EmpType,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.business.jidanpu_relaunch import KIND_WITHDRAW, WITHDRAW_NO_LINK_REASON
from apps.business.order_status import IllegalTransition, apply_transition
from apps.business.performance_amount import AmountError
from apps.business.refunds import RefundError
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import nodes_of

# ── 三条权限各自要探的页面（spec §2.3 逐条核过节点挂的 URL）─────────────────────────

#: 录单：节点「添加和编辑订单」的页面。
MEMBER_RECORD_PAGE = "/Order/addorder"
#: 系统日志：节点「系统日志」的页面（日志快路要用）。
MEMBER_LOG_PAGE = "/System/systemlog"
#: 「将订单发到内部抢单大厅」⛔ **没有自己的页面**（它是个接口），只能探它所在的那个页面
#: （管理订单列表）⇒ **弱判据**（spec §2.3 / §13-R7 如实登记的残余）。
MEMBER_HALL_PAGE = "/Order/orderlist"

#: 探测顺序 = 报错顺序（确定性，便于用户按顺序去勾权限）。
_PAGE_PATHS: tuple[str, ...] = (MEMBER_RECORD_PAGE, MEMBER_LOG_PAGE, MEMBER_HALL_PAGE)

# ── 人话文案（spec §9.1 的文案表；⛔ 缺哪条报哪条，绝不来一句笼统的「权限不足」）──────

#: 登录失败（凭据错）。
LOGIN_FAILED = "登录失败：账号或密码错误"
#: 来源名匹配到**多条**（用户得先去下家改名）。
SOURCE_AMBIGUOUS = "匹配到多个同名来源，请先在下家把名字改到唯一"

#: 权限缺哪条报哪条：键 = 要探的页面，值 = 该页面打不开时的人话。
#: ⚠️ 说的是要勾的权限节点名字（用户在「职位」里找的就是它）：录单用业务方给的「录入订单」、系统日志用节点原文「系统日志」（页面叫「系统操作日志」）——⛔ 两套叫法不混报。
#: ⚠️ 大厅只探「它所在的页面」⇒ 弱判据，文案必须与判据强度一致（⛔ 别把「页面打不开」说成「确定没有这个接口的权限」）。
PAGE_FAILURES: dict[str, str] = {
    MEMBER_RECORD_PAGE: (
        "该账号打不开『录入订单』页面，说明没有对应权限，请在下家鸡蛋铺里给这个职位勾上"
    ),
    MEMBER_LOG_PAGE: (
        "该账号打不开『系统日志』页面，说明没有对应权限，"
        "请在下家鸡蛋铺里给这个职位勾上"
    ),
    MEMBER_HALL_PAGE: (
        "该账号打不开『管理订单』页面，说明可能没有『将订单发到内部抢单大厅』权限，"
        "请在下家鸡蛋铺里给这个职位勾上"
    ),
}


def source_missing_message(name: str) -> str:
    """「来源不存在」的人话（把用户填的那个名字原样报出来）。用 f-string 拼接 ⛔ 不用 `str.format`：
    来源名是用户输入，名字带 `{}` 时 `format` 会抛 `KeyError`（把一句提示变成 500）。"""
    return (
        f"鸡蛋铺里没有名为『{name}』的订单来源，"
        "请到下家鸡蛋铺后台手工创建后再回来检测"
    )


@dataclass(frozen=True)
class DetectionReport:
    """一次绑定检测的结论：`ok=False` 时 `failures` 一条一因、逐条点名（三条检测独立，⛔ 不在第一条失败处停下——用户想一次知道要勾哪些权限）；只装判据层失败（凭据错/来源不存在或多条/某页打不开），「上游读不到/判不了权限」是抛异常不进这里。
    ⭐ D-0177 三字段只服务页面「订单来源」下拉：`source_options`=对方来源字典全量（`{name, label}`，`label` 是要保存的名字）、`source_options_ok`=整份字典那一跳读到没有（⚠️ 不是装饰：登录没成时它是 `False`、字典为空——少了它「字典没读到」会被页面说成「对方已无此来源」，`AGENTS.md` §5-E 空结果不等于没有数据）、`source_ok`=`wanted` 唯一命中没有。
    ⚠️ 三字段默认值（`False`/空元组）是 fail-closed：老调用方（含测试桩）构造的报告只会「不判」，⛔ 不凭空多出「对方没有这个来源」的结论。"""

    ok: bool
    failures: tuple[str, ...] = ()
    source_ok: bool = False
    source_options_ok: bool = False
    source_options: tuple[dict, ...] = ()


@contextmanager
def _connection(binding, client):
    """产出 `(sdk_client, http)`：给了注入就用注入的，没给就自己建连。⚠️ 注入态（只为可测性，⛔ 不是业务参数、
    ⛔ 不许来自请求）同一个桩兼任 SDK 客户端与 HTTP 边界——真实形态里是两个对象（见 `open_client_with_http`）。"""
    if client is not None:
        yield client, client
        return
    with jidanpu.member_session(binding) as pair:
        yield pair


def detect_binding(binding, *, client=None, source_name: str = "") -> DetectionReport:
    """跑完 spec §9.1 三条检测，回逐条点名的报告（只读上游、不落库）。⚠️ `client` 是可测性注入口（⛔ 不是业务参数，凭据只从 `binding` 取，S3）；
    ⚠️ `source_name`（D-0177）=要检查的来源名，空/缺省回落 `binding.source_name`（再回落模型默认值）——「测试连接」必须传下拉此刻选中的那个，否则查的是旧名；登录失败 ⇒ 立刻返回（无会话探页面只会 302 到登录页，是「检测本身失败」不是「没有权限」）；
    来源读不到/探页面判不了权限 ⇒ 异常原样上行（SDK `JidanpuError` 家族，⛔ 不在这里翻成业务码）；需要顺带解析登录显示名的动作走 `detect_binding_with_login_identity`（共用 `_detect_with_client`，⛔ 不另抄判据）。"""
    with _connection(binding, client) as (sdk, http):
        return _detect_with_client(binding, sdk, http, source_name=source_name)


def detect_binding_with_login_identity(
    binding, *, client=None, source_name: str = ""
) -> tuple[DetectionReport, str | None]:
    """一次建连：跑 `detect_binding` 三条检测，再用同一条连解析登录账号显示名（绑定页保存与「测试连接」都要落显示名——页面显示、快路回声抑制、慢路接手人比对都读它；再建一次连多一次登录+几条上游请求）。
    ⛔ 本函数仍只读上游、不落库（写回绑定是动作的事）。⚠️ 身份那跳失败不报错（上游异常/`Id` 读不出/两个名单都没查到 ⇒ 第二个元素 `None`），检测结论照旧返回；
    `source_name` 同 `detect_binding`（D-0177）。"""
    with _connection(binding, client) as (sdk, http):
        report = _detect_with_client(binding, sdk, http, source_name=source_name)
        display_name = _login_display_name_or_none(sdk)
    return report, display_name


def _login_display_name_or_none(client) -> str | None:
    """解析登录账号显示名；解析失败/两个名单都没查到 ⇒ `None`。本线只落显示名一格：按业务方裁定（A15/R17）没有「对方账号 uid」列
    ——本线不在对方系统里指派任何人 ⇒ uid 只当查找键 ⛔ 不落库；也⛔ 不把旧名当回落（E-0169：查不到就是查不到）。"""
    try:
        _uid, name = jidanpu.resolve_login_identity(client)
    except JidanpuError:
        return None
    return name or None


def _detect_with_client(binding, sdk, http, *, source_name: str = "") -> DetectionReport:
    """`detect_binding` 的同一条连版本（调用方已建连；⛔ 仍只读上游、不落库）。只把「建连」挪出去（两个入口共用同一个身体），
    三条检测判据一字未改（见 `detect_binding` docstring）；`source_name` 同上（F4 口径）。"""
    # ⚠️ **优先级：调用方给的那个 → 库里那一行 → 模型默认值**（D-0177 / F4）：
    #    页面下拉里此刻选中的那个才是「你要保存的那个」。空串回落到模型默认值是为了
    #    不在文案里报出「名为『』的来源」。
    wanted = (
        (source_name or "").strip()
        or (binding.source_name or "").strip()
        or SOURCE_NAME_DEFAULT
    )

    try:
        # ⚠️ 显式登录一次：探页面用的是同一个 cookie jar，登录没发生的话
        #    每个页面都会 302 到登录页（⇒ 检测本身失败，见上）。
        sdk.ensure_login()
    except AuthError:
        # ⚠️ 登录都没成 ⇒ `source_options_ok=False`（默认值就是 False）：
        #    页面**不许**据空字典喊「对方已无此来源」（那是把「没读到」说成「没有」）。
        return DetectionReport(ok=False, failures=(LOGIN_FAILED,))

    sources = jidanpu.fetch_sources(sdk)
    hits = jidanpu.match_sources(sources, wanted)

    failures: list[str] = []
    if not hits:
        failures.append(source_missing_message(wanted))
    elif len(hits) > 1:
        failures.append(SOURCE_AMBIGUOUS)

    # ⚠️ 来源不对也照探页面：三条检测彼此独立，用户要一次看到全部要处理的事。
    reachable = jidanpu.probe_pages(http, _PAGE_PATHS)
    for path in _PAGE_PATHS:
        if not reachable[path]:
            failures.append(PAGE_FAILURES[path])

    return DetectionReport(
        ok=not failures,
        failures=tuple(failures),
        source_ok=len(hits) == 1,
        # 字典**真的读到了**：只有到过这一行才为 True（登录失败那支在上面已经返回）。
        source_options_ok=True,
        source_options=jidanpu.source_options(sources),
    )


# ══ 同步账号身份自愈（与租户级 D-0170 同一把尺子）════════════════════════
# ⚠️ **本线只对齐 `account_name`**：`JidanpuMemberBinding` 没有「对方账号 uid」列
#    （业务方裁定 A15 / R17）⇒ uid 只在解析显示名时当查找键，⛔ 不落库、⛔ 不加列 / 迁移。

#: 身份自愈痕迹 kind：沿用租户级已有的 `jidanpu_common.IDENTITY_HEAL_KIND`（`"binding.identity_healed"`）——同一件事实用同一个 kind。
#: ⛔ 不新造 kind：台账 kind 词表收口跨线残差还没做（后端写的 kind 多于前端登记的），新造只会再加一个没登记的取值
#:    （见 `src/frontend/src/api/jidanpuSync.ts` 的 `KIND_LABELS` docstring）。
IDENTITY_HEAL_KIND = jidanpu_common.IDENTITY_HEAL_KIND

#: 幂等键里的 `reason`（**稳定**的一句话：新旧显示名进 `payload`，⛔ 不进这里，否则同一事实
#: 每换一次身份就多一行）。⚠️ 与租户级那句**分开写**：台账里要能一眼看出是哪条线自愈的。
IDENTITY_HEAL_REASON = (
    "下游代练员绑定的同步账号显示名与登录账号不一致：已按登录账号写回"
    "（回声抑制与接手人比对都按登录账号的显示名比）"
)

#: 本线自愈痕迹的 `target_ref` 前缀。⚠️ 幂等键是 `(kind, target_ref, reason)` 三元组：租户级线用 `binding.account` 而两边 `account` 可能同名
#: ⇒ 照抄同形会让先写的那条吞掉后写的（运维看不出是哪条线自愈的）。本线每个代练员一行 ⇒ 主键才是稳定标识，
#: 前缀 `member:` 让两条线的 `target_ref` 恒可区分（⛔ 不靠 account 碰巧不同）。
IDENTITY_HEAL_REF_PREFIX = "member:"


def heal_binding_identity(binding, *, client=None) -> str | None:
    """把代练员个人绑定的同步账号显示名对齐到登录账号（业务口径「同步账号=我们登录的那个账号」，同 D-0170）。⭐ 必须对齐：快路回声抑制（`_echo_by_name`）与慢路接手人比对（`jidanpu_reconcile._sync_account_name_of`）都读它，显示名与凭据账号不一致 ⇒ 自己的日志行不被抑制 ⇒ 多打上游+可能误接手。
    行为判据：解析身份（uid 收 `GetUserInfo` 的 `Id`/`id`；显示名按 name→可指派名单→人员页→account 取——口径与来源序唯一出处 = `jidanpu.resolve_login_identity` 的 docstring，两处都没查到 ⇒ 空名）；空名 ⇒ 不写、不留痕、回 `None`（E-0169：空值唯一语义是「没查到」，⛔ 不拿回落值/uid 冒充显示名）；与 `binding.account_name`（去空白）不同才写回+留一条幂等痕迹，一致 ⇒ 回 `None` 不写不留痕；解析失败（上游异常/撞本轮预算）只跳过自愈（不写不抛不留痕，下一轮再试）。
    ⚠️ 写库后必须顺手同步内存对象 `binding.account_name = name`：调用方把同一个内存对象交给同轮后续各条路（outbox/快路/回拉/对账），只写库不改它 ⇒ 它们这轮读到的仍是旧显示名（E-0168）。落库只有显示名一格，⛔ 不为「两格对齐」加列/迁移。"""
    try:
        with _connection(binding, client) as (sdk, _http):
            uid, name = jidanpu.resolve_login_identity(sdk)
    except (JidanpuError, jidanpu.RoundBudgetExceeded):
        # 见行为④：上游失败 / 我们自己的节流都只跳过自愈，⛔ 不写、⛔ 不抛、⛔ 不留痕。
        return None
    if not name:
        # 见行为②：显示名没查到 ⇒ 不写、不留痕、下一轮再试。
        return None
    return _store_binding_login_name(binding, uid=uid, name=name)


def _binding_current_rows(binding):
    return JidanpuMemberBinding.objects.filter(
        pk=binding.pk, mode=binding.mode, jidanpu_base_url=binding.jidanpu_base_url,
        account=binding.account, password=binding.password,
        client_id=binding.client_id, client_secret=binding.client_secret,
    )


def _binding_is_current(binding):
    return _binding_current_rows(binding).exists()


def _binding_credentials_match(binding):
    return JidanpuMemberBinding.objects.filter(
        pk=binding.pk, enabled=True, mode=JidanpuMemberBinding.Mode.PASSWORD,
        jidanpu_base_url=binding.jidanpu_base_url, account=binding.account,
        password=binding.password, updated_at=binding.updated_at,
    )


def _store_binding_login_name(binding, *, uid, name, verify_binding=False):
    old_name = (binding.account_name or "").strip()
    if name == old_name:
        return None
    rows = (
        _binding_credentials_match(binding) if verify_binding
        else JidanpuMemberBinding.objects.filter(pk=binding.pk)
    )
    with transaction.atomic():
        if not rows.update(account_name=name):
            return None
        _note_identity_heal_once(
            binding=binding, old_name=old_name, new_name=name, login_uid=uid,
        )
    binding.account_name = name
    return name


FASTPATH_IDENTITY_SECONDS = 300


@dataclass(frozen=True)
class _VerifiedFastpathIdentity:
    generation: int
    binding_version: datetime
    name: str
    expires_at: float


class _FastpathIdentity:
    def __init__(self, binding, client):
        self.binding = binding
        self.client = client
        self.attempted_generation = None
        self.attempted = False
        self.binding_changed = False
        self.name = ""

    def _generation(self):
        generation = getattr(self.client, "login_generation", None)
        return generation if type(generation) is int else None

    def current_name(self):
        generation = self._generation()
        if self.attempted and self.attempted_generation == generation:
            return self.name
        self.attempted = True
        self.attempted_generation = generation
        self.name = ""
        cached = getattr(self.client, "_tt_fastpath_identity", None)
        if (
            generation is not None
            and isinstance(cached, _VerifiedFastpathIdentity)
            and cached.generation == generation
            and cached.binding_version == self.binding.updated_at
            and (self.binding.account_name or "").strip() == cached.name
            and time.monotonic() < cached.expires_at
        ):
            self.name = cached.name
            return self.name
        self.invalidate()
        try:
            uid, name = jidanpu.resolve_login_identity(self.client)
            current = self._generation()
            if current != generation:
                # Resolver fallback calls may have crossed a re-login.
                generation = current
                uid, name = jidanpu.resolve_login_identity(self.client)
            self.attempted_generation = self._generation()
            if not name or self.attempted_generation != generation:
                return ""
            if name == (self.binding.account_name or "").strip():
                self.binding_changed = not _binding_credentials_match(self.binding).exists()
            else:
                self.binding_changed = not _store_binding_login_name(
                    self.binding, uid=uid, name=name, verify_binding=True,
                )
        except (AuthError, UpstreamBlockedError):
            raise
        except (JidanpuError, jidanpu.RoundBudgetExceeded):
            self.attempted_generation = self._generation()
            return ""
        except Exception as exc:  # noqa: BLE001 —— Optional identity lookup must not stop order checks.
            self.attempted_generation = self._generation()
            logger.warning("代练员绑定 #%s 快路身份核对失败（%s）", self.binding.pk, type(exc).__name__)
            return ""
        if self.binding_changed:
            return ""
        self.name = name
        if generation is not None:
            self.client._tt_fastpath_identity = _VerifiedFastpathIdentity(
                generation, self.binding.updated_at, name, time.monotonic() + FASTPATH_IDENTITY_SECONDS,
            )
        return self.name

    def invalidate(self):
        self.client._tt_fastpath_identity = None
        self.name = ""


# ══ 不变式引擎（spec §5.1 / §5.2 / §5.4）════════════════════════════════════════
# 本段是**纯逻辑**：⛔ 不打上游、⛔ 不写 `JidanpuSyncEvent`、⛔ 不写链路行。
# 它只回答两个问题：「对方那边**应该**长什么样」与「跟本地标记差多少」。

#: 对方系统里应有的三档形态（spec §5.1 的表，逐行一档）；这三个字面值是消费者契约（下游 `kind` 由它们派生），⛔ 不要改名。
#: 「没有接手人、但必须留着对方的单」的我方状态（`_shape` 第二条守卫，真机实测）：三档都意味着已有人在推进这张单
#: （对方打手接了或流程走完）⇒ 我方「没记下谁」只说明自动接手失败，⛔ 不构成「对方不该留着它」的理由。
NO_TAKER_KEEP_STATUSES = frozenset({
    OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED,
})

SHAPE_HALL = "hall"
SHAPE_ORDER_ONLY = "order_only"
SHAPE_ABSENT = "absent"


@dataclass(frozen=True)
class ReconcileItem:
    """一条待办：哪张单 + **为什么**（人话，⛔ 不是一个光秃秃的主键）。"""

    order: Order
    shape: str
    reason: str


@dataclass(frozen=True)
class ReconcileSkip:
    """一条被跳过的单：同地址下有更早的绑定负责推它（spec §5.4/A12）。⛔ 不许静默吞掉：由 `QueueStats.skipped` 带出人话原因、调用方汇总出去；
    ⚠️ 不逐单落台账（每 180 秒一轮 × 每张重叠单一条会灌爆台账 ⇒ 只在汇总给「计数+人话」）。"""

    order: Order
    owner: JidanpuMemberBinding
    reason: str


@dataclass(frozen=True)
class ReconcileResult:
    """一轮对账的差集（只描述该做什么，⛔ 一行都不写）。`to_launch`/`to_withdraw` 对应 spec §5.1 第 1、3 行（录单+抛单/退单）；
    `to_record`=第二行「只录单」的缺单形态（⛔ 不抛大厅——会把已指派给他、还在做的单挂到对方大厅）；`to_unlist`=第二行「之前抛过 ⇒ 撤下」格
    （业务方明确要求：从对方抢单大厅拿回来，⛔ 不是退单——那会作废对方那张单）；`skipped` 见 `ReconcileSkip`；`at`=注入时钟。"""

    to_launch: tuple[ReconcileItem, ...] = ()
    to_withdraw: tuple[ReconcileItem, ...] = ()
    to_record: tuple[ReconcileItem, ...] = ()
    to_unlist: tuple[ReconcileItem, ...] = ()
    skipped: tuple[ReconcileSkip, ...] = ()
    at: datetime | None = None


def _main_taker_id(order_id):
    """这张单的接手人（MAIN 绩效行的 `user_id`）；没人接回 `None`。⚠️ 容错读：业务方裁定允许把无主 MAIN 行整条删（E-0148）
    ⇒ 缺行是正常数据形态；⛔ 不要用 `performance.py::main_performance_of`（缺行即抛 `DoesNotExist`）。"""
    row = Performance.objects.filter(order_id=order_id, emp_type=EmpType.MAIN).first()
    return None if row is None else row.user_id


def _shape(order, *, owner_id, taker_id, in_scope):
    """三档判据本体（唯一出处：`expected_shape` 与 `reconcile` 都走这里）。⭐ 真机守卫：没有接手人 ≠「对方不该有这张单」——
    对方打手接了 ⇒ 我方自动接手因绩效行唯一约束（他既是客服又是绑定代练员）失败 ⇒ 无接手人但对方在推进；只要我方状态已越过「在大厅」（代练中/待验收/已完成）一律当「对方留着它」，⛔ 绝不排退单。
    ⚠️ 顺序承重（spec §5.1 第 1、2 行）：先判「是不是他的」（是 ⇒ 只录单/不是 ⇒ 退单）再判「在不在大厅」；⚠️ 已软删排最前（spec §5.1 第三行把「已删除」并入「对方不该再有」⇒ 先给 `absent`，否则被删的指派单被永远留着）。"""
    if order.deleted_at is not None:
        return SHAPE_ABSENT
    if taker_id is not None:
        return SHAPE_ORDER_ONLY if taker_id == owner_id else SHAPE_ABSENT
    if order.status in NO_TAKER_KEEP_STATUSES:
        # 有人在推进这张单，只是我方**没记下是谁**（对方打手接了、我方自动接手却失败了）
        # ⇒ 留着对方的单，⛔ 不退单（退了会把对方正在做的工作毁掉，真机踩过）。
        return SHAPE_ORDER_ONLY
    if order.status == OrderStatus.WAITING_GRAB and in_scope:
        return SHAPE_HALL
    return SHAPE_ABSENT


def expected_shape(order, binding) -> str:
    """这张单在对方系统里应该长什么样（spec §5.1 三档之一）。「是不是他的」=MAIN 绩效行 `user_id`（订单表没有「谁接了」列，判据出处在 `claim.py::_has_taker`）；
    「在不在大厅」=`status == WAITING_GRAB` 且该单游戏在他的游戏范围内（`game_scope.games_of`：管理员=全部游戏，没档案/没勾=空集）。
    ⚠️ 指派比游戏范围强：MAIN 行是他的 ⇒ `order_only` ⛔ 不再看范围（派生事实：指派是人对单的动作，范围只决定大厅里哪些单推给他）。"""
    return _shape(
        order,
        owner_id=binding.user_id,
        taker_id=_main_taker_id(order.pk),
        in_scope=games_of(binding.user).filter(pk=order.game_id).exists(),
    )


def orders_of(binding):
    """归属该代练员的单（spec §3）：①MAIN 绩效行是他的（含终态：已完成/已退单也仍应留在对方系统）；②我方大厅里他游戏范围内的单。
    ⚠️ 范围收窄在查询里做（管理员=全部游戏，先拉全表再内存筛会把「补推一批」变「扫全表」），且那一处走 `game_scope.game_ids_q`（⛔ 不手写 `game_id__in`——它是「按游戏集合过滤」的写法出处；`&` 操作数顺序对「SQL 逐字不变」判据承重：`game_ids_q(...) & Q(...)` 编译结果与收编前手写版逐字节相同【实测】，反着写语义一样、只是文本换位）。`games_of` 空集=大厅那一支为空（⛔ 不是「不过滤」）；软删单不在两支里（默认 manager 已排除），其链路侧由 `reconcile` 兜（见 `_shape` 删除判据）。"""
    return Order.objects.filter(
        Q(performances__emp_type=EmpType.MAIN, performances__user_id=binding.user_id)
        | (
            game_ids_q("game_id", games_of(binding.user).values("pk"))
            & Q(status=OrderStatus.WAITING_GRAB)
        )
    # ⭐ 镜像卡派生的隐藏单不在对账候选集——生命周期（抛/撤/退）归 `hall_shadow` 的钩子；让对账也摆它会把同一条同步动作排两遍（且隐藏单的「期望形态」与普通在厅单不同）。
    # ⭐ D-0325：上家鸡蛋铺镜像卡派生的隐藏单（`upstream_hall_card`）同一条口径，生命周期归 `upstream_hall_shadow` 的钩子
    #    （绑定后启用不补推先前进厅的卡，与 peer 线现状对齐；卡复活/重进厅时钩子会重抛）。
    ).exclude(hall_card__isnull=False).exclude(
        upstream_hall_card__isnull=False
    ).distinct()


def dedupe_owner(order, bindings):
    """同地址重叠时这张单归谁：候选里 `id` 最小（=最早建的那一行）的那份绑定。命中判据 = 该单在它的 `expected_shape` 里不是 `ABSENT`
    ——⛔ 不另写一份「算不算他的单」（同一事实两处判据必然漂移）。⚠️ 指派单天然只有一份绑定命中（MAIN 行 `user` 只等于其中一个），
    不受重叠规则影响（spec §11 第 5 条）；谁都不命中 ⇒ `None`（⛔ 不硬塞一个归属）。"""
    hits = [row for row in bindings if expected_shape(order, row) != SHAPE_ABSENT]
    if not hits:
        return None
    return min(hits, key=lambda row: row.pk)


def _candidate_bindings(binding):
    """同地址、会真正跑对账的绑定（含当前这一行）。spec §5.4 只说「`user` 最早绑定者推」：这里把停用的排除在候选外
    （它不推任何单，占着归属会让这张单变成没人推）；当前行无论如何都在候选里。⚠️ 调用方还要再过 `_only_push_targets`（推单归属拆开）：
    本函数只答「谁会跑对账」不答「谁能推单」，两件事判据各一处 ⛔ 别混。"""
    candidates = list(
        JidanpuMemberBinding.objects.filter(
            jidanpu_base_url=binding.jidanpu_base_url, enabled=True
        )
    )
    if all(row.pk != binding.pk for row in candidates):
        candidates.append(binding)
    return candidates


def reconcile(binding, *, now, line=None):
    """一轮对账的差集（spec §5.2）：算期望形态、比本地标记、给待办。⚠️ 不逐张问上游（那会把一轮对账变 N 次请求；`in_hall` 由日志快路与慢路对账更新）、一行都不写（写 `JidanpuSyncEvent`/链路行是消费者的事）；`now` 是注入时钟（与 `run_outbox`/`poll_log`/`pull_statuses` 同口径，只进结果的 `at`）。
    ⭐ 「已经退成过」的判据落在链路列：`link.withdrawn_at` 非空 ⇒ 不再进 `to_withdraw`（目标形态已成立；⛔ 不复用 `in_hall`——它的成因不止一个，见模型那一列注释）。
    ⭐⭐ 期望形态按线算：`line.hall_push_unreachable_reason` 非空（②式：`recUser`/`assignPrice` 只在建单那一刻可传）⇒「该在对方抢单大厅」对已有链路的单不可达，收口成 `SHAPE_ORDER_ONLY` 一张不排——⛔ 不是「排出来失败再靠终态兜」：每 180 秒一轮终态 `skipped` 挡得住重试、⛔ 挡不住重排；⛔ ①式一字不变；只在「有链路+不在大厅」时收口（`in_hall=True` 收口反而会排撤下=②式毁单）。⭐⭐ D-0249（修 E-0246）撤下档同样按线收口：`unlist_unavailable_reason` 非空（②式）⇒ 不排 `to_unlist`（对方那张单留在对方大厅、未分配、活着就是②式可达期望形态）；取数口只有 `_unlist_unavailable_reason`（⛔ 别写第二份判据）；①式不设格、撤下照旧。"""
    line = line or outbox_line_for(binding)
    games = set(games_of(binding.user).values_list("pk", flat=True))
    orders = {row.pk: row for row in orders_of(binding)}
    # ⚠️ 本地有链路但已不在「归属他」集合里（被别的打手接了/撤回了/移出范围/已软删）⇒ 也要比一遍，否则那张单在对方系统里永远退不掉；用链路行的 `order`（正向 FK 走 base manager）⇒ 软删单也取得到。
    # ⭐ 镜像卡派生的隐藏单（`hall_card` 非空；D-0325 同口径 `upstream_hall_card`）在这里就排除——`orders_of` 已排掉「直接归属他」那支，但这支靠本地链路把单加回集合：隐藏单建好就 `queue_hall_launch` 过、天然有链路
    #    ⇒ 不排它仍会进 `to_launch`/`to_withdraw`，与 `hall_shadow`/`upstream_hall_shadow` 钩子把同一条同步动作排两遍；生命周期归那些钩子。
    links = {
        link.order_id: link
        for link in JidanpuMemberOrderLink.objects.filter(binding=binding)
        .exclude(order__hall_card__isnull=False)
        .exclude(order__upstream_hall_card__isnull=False)
        .select_related("order")
    }
    # ⚠️ 这一步**不能删**（那个修复动的是 `links` 的取数口，不是这里）：
    #    有链路、但已不在「归属他」集合里的单要加回来比一遍，否则那张单在对方系统里
    #    永远退不掉。⚠️ 用链路行的 `order`（正向 FK 走 base manager）⇒ 软删的单也取得到。
    for order_id, link in links.items():
        orders.setdefault(order_id, link.order)

    takers = dict(
        Performance.objects.filter(
            order_id__in=list(orders), emp_type=EmpType.MAIN, user_id__isnull=False
        ).values_list("order_id", "user_id")
    )
    # ⭐⭐ 生产事故教训：「**这家店的主人**在做的单不许退」。一次批量取齐（⛔ 不逐单查：
    #    一轮对账要过这张绑定名下全部单）。判据本体仍然只有 `downstream_addresses_of_taker`
    #    那一处，这里走它的批量形态。
    taker_addresses = _downstream_addresses_by_user(takers.values())
    # ⭐ 推单归属拆开：地址级去重的候选也要过 `_only_push_targets`（与 `owners_of`/`_assign_target_of`/`_push_evidence_ids` 同一道判据）：
    #    同店有启用中的①式时，②式那条不参与去重、也⛔ 不会被裁成 owner。
    #    ⚠️ 候选里剔掉自己是必须的（`_candidate_bindings` 会把当前行补回去）——否则②式跑自己这轮时会以「候选里只有它」把自己裁成归属。
    candidates = _only_push_targets(_candidate_bindings(binding))
    hall_candidates = [row for row in candidates if row.hall_sync_enabled]
    #: 本行**有没有推单资格**：没有 ⇒ 下面 `to_launch` / `to_record` 一支都不排
    #: （「排了也只会被这条判据挡掉」，与 D-0299 / ②式收口同一家族：让**期望计算本身**
    #: 把它当前提）。⚠️ 其余档（退单 / 撤下）**一个字不动**：那是清理，不是推单。
    push_ok = bool(_only_push_targets([binding]))
    # ⭐ 线上热修教训（同店两条绑定）：这一家店已经录过的单不再由本行补录——另一条录过的单在本行看来「没有链路」，
    #    `SHAPE_ORDER_ONLY` 会把它排成 `member.assign`（真机：一轮 1.3 万条全是「对方已有这张单」，每轮重排、把真实新事件挤到后面）。
    #    判据：该单在同地址任意一条兄弟绑定上有链路 ⇒ 对方系统里已有它 ⇒ 不补录；⚠️ 只影响 `to_record`，`to_launch` 与退单/撤下一字不动。
    sibling_linked: set[int] = set()
    _sibling_ids = [
        b.pk for b in _bindings_at([binding.jidanpu_base_url]) if b.pk != binding.pk
    ]
    if _sibling_ids:
        from apps.business.models import JidanpuMemberOrderLink as _Link

        sibling_linked = set(
            _Link.objects.filter(binding_id__in=_sibling_ids).values_list(
                "order_id", flat=True
            )
        )
    # ⭐⭐ D-0362：已落「转人工」终态行的（订单，绑定）⇒ 对账不再重排 `member.launch`（口径②「两者都不再重排」的对账半边）——终态 `skipped` 挡得住重试、⛔ 挡不住重排：不闸的话每 180 秒重排一条注定跳过的 launch（台账灌水）⇒ 让期望计算本身把它当前提。
    #    ⚠️ 窗口查询（形状照 `_latest_launch_breaker_note`：`status="skipped"` 走 `jdp_event_status_idx` 区间扫 + kind/payload 过滤，每轮恰一次）：窗过期后消费侧重新校验远端（prepare 真读）、仍逐字查无 ⇒ 重写一条新窗行，稳态=每窗每（单，绑定）恰一条。
    #    ⚠️ 只闸推单（`to_launch`）：退单/撤下/补录等清理档一字不动（转人工停的是「往对方建卡」，不是「清理对方残留」）。
    handoff_refs = set(
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_NO_ORDER_HANDOFF,
            status="skipped", target_type="order",
            payload__member_binding_id=binding.pk,
            created_at__gte=now - NO_ORDER_HANDOFF_NOTE_WINDOW,
        ).values_list("target_ref", flat=True)
    )

    to_launch: list[ReconcileItem] = []
    to_withdraw: list[ReconcileItem] = []
    to_record: list[ReconcileItem] = []
    to_unlist: list[ReconcileItem] = []
    skipped: list[ReconcileSkip] = []

    from apps.business import jidanpu_first_assignment

    drafts = jidanpu_first_assignment.retained_drafts(binding)
    for order_id in sorted(orders):
        order = orders[order_id]
        if jidanpu_first_assignment.retains_draft(
            order, links.get(order_id), drafts.get(order.order_no), taker_id=takers.get(order_id),
        ):
            continue
        if (order.peer_direct_pending or {}).get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM:
            continue
        shape = _shape(
            order,
            owner_id=binding.user_id,
            taker_id=takers.get(order_id),
            in_scope=order.game_id in games,
        )
        # 重叠去重只在**真有别的候选**时才需要算（常见形态是同地址只绑了一个人）。
        owners = hall_candidates if shape == SHAPE_HALL else candidates
        if shape != SHAPE_ABSENT and len(owners) > 1:
            owner = dedupe_owner(order, owners)
            if owner is not None and owner.pk != binding.pk:
                skipped.append(
                    ReconcileSkip(
                        order=order,
                        owner=owner,
                        reason=(
                            f"同地址下负责推这张单的绑定（{owner.account}）占着归属，"
                            "本次对账跳过这张单"
                        ),
                    )
                )
                continue

        link = links.get(order_id)
        # ⭐⭐ ②式那份「期望形态」的收口（理由见 docstring）：「该在对方抢单大厅」对已有链路的单在②式下不可达（建单后没有任何接口能推进对方大厅）
        #    ⇒ 可达终态就是 `SHAPE_ORDER_ONLY`（在对方订单列表、不在大厅）⇒ 按 `in_hall=False` 那格走（什么都不排）。⚠️ 顺序/位置承重：排在重叠去重之后
        #    （归属裁定照旧按「该在对方大厅」真实意图算）、排在 `SHAPE_HALL` 支之前；`in_hall=True` 不进本条（收口反而排撤下，见下面 D-0249）。
        if (
            shape == SHAPE_HALL
            and link is not None
            and not link.in_hall
            and line.hall_push_unreachable_reason
        ):
            shape = SHAPE_ORDER_ONLY
        if shape == SHAPE_HALL:
            if order.penalty_approval_id is not None:
                continue
            # ⭐⭐ D-0299：「同步大厅订单」关掉的绑定不参与「抛大厅」档：期望对它不成立 ⇒ `to_launch` 一张不排（「补录+抛」与「补抛」两支一起闸），⛔ 也不进 `skipped`（那是「归属别的绑定」的裁定；这里是「他不参与大厅」——每 180 秒一条的噪声谁都不需要）⇒ 让期望计算本身把它当前提（与②式收口同家族），`reconcile_and_queue` 那侧一字不动。
            #    ⛔ 不许收口成 `SHAPE_ABSENT`：那会让 `to_withdraw` 把已挂对方大厅的存量单排退单，而 D-0299 明文「关掉后不主动撤回存量单」；其余档（to_record/to_withdraw/to_unlist）一字不动。
            #    ⭐ 推单归属拆开再加一格 `push_ok`：本行不是推单目标（同店有启用中的①式而本行是②式）⇒ 抛大厅支同样一张不排、也不进 `skipped`（同 D-0299 理由）。
            if binding.hall_sync_enabled and push_ok:
                if order.order_no in handoff_refs:
                    # ⭐ D-0362：已落「转人工」终态行的那族对账不再重排（见上面 `handoff_refs` 段）；刻意不进 `skipped` 统计
                    #    （每 180 秒一条的噪声谁都不需要，D-0299 同口径）；逐单留痕由那条转人工终态行自己承载（时间线可见）。
                    pass
                elif link is None:
                    to_launch.append(
                        ReconcileItem(
                            order=order,
                            shape=shape,
                            reason="本地没有链路：这张单还没在对方录过，要录单 + 抛单",
                        )
                    )
                elif not link.in_hall:
                    to_launch.append(
                        ReconcileItem(
                            order=order,
                            shape=shape,
                            reason="本地链路标着不在对方大厅：要补一次抛单",
                        )
                    )
        elif shape == SHAPE_ORDER_ONLY:
            # ⭐ 「已指派给他、本地却没有链路 ⇒ 补录单」也是推单（会在对方建单）⇒ 同样过 `push_ok`：②式那条在同店有启用中的①式时永不推。
            #    指派那一刻的录单由 `queue_assign` → `_assign_target_of` 排到①式那条上，⛔ 不靠②式这一条补。
            if (
                link is None and push_ok and order_id not in sibling_linked
                and not _is_completed_penalty_order(order)
            ):
                to_record.append(
                    ReconcileItem(
                        order=order,
                        shape=shape,
                        reason="该单已指派给他、本地却没有链路：要补录单（⛔ 不抛大厅）",
                    )
                )
            elif link is not None and link.in_hall:
                # ⭐ spec §5.1 第二行「之前抛过 ⇒ 撤下」（业务方明确要求）：期望「只在他家订单列表」却还标着在他家大厅 ⇒ 撤下（⛔ 不是退单，`to_withdraw` 才是）；本地标记可能过期 ⇒ 消费者先读一遍再写，`isThrowOrder != "Y"` 只把标记改正、一次写都不发（见 `_consume_unlist`）。
                #    ⭐⭐ D-0249 收口（修 E-0246，与 `hall_push_unreachable_reason` 那条收口成对）：这条线没有「不毁单撤下」原语（`unlist_unavailable_reason` 非空，②式）⇒ 不排——对方那张单留在对方大厅、未分配、活着就是②式可达的期望形态；
                #    排了必被分派处终态跳过、每 180 秒一轮挡得住重试 ⛔ 挡不住重排；取数口只有 `_unlist_unavailable_reason`（⛔ 别在这里摸格另写一份）。
                if _unlist_unavailable_reason(line) is None:
                    to_unlist.append(
                        ReconcileItem(
                            order=order,
                            shape=shape,
                            reason=(
                                "我方已接手这张单、本地链路却标着它还挂在对方大厅："
                                "要把它从对方抢单大厅撤下（⛔ 不退单）"
                            ),
                        )
                    )
        elif link is not None and link.withdrawn_at is None:
            # ⭐⭐ 判据打在「终态」上（真机修的环）：`link.withdrawn_at` 非空 = 这张单在这一份绑定上已退成过、此后没再录回 ⇒ 目标形态已成立、一张不排（旧形态只看 `link is not None` ⇒ 退单办成之后没有东西记得「办成了」⇒ 下轮又排；真机读数：8710 条退单事件只牵涉 185 张单、平均一张退 47 次）；回置只有一处（`_consume_launch` 成功那支清空——回池/撤销删除后重新录单+抛单）；⛔ 不补 `ReconcileSkip`：那是「别的绑定负责推」档的载体，逐单留痕由链路列+当初那条 `done` 台账承载（每 180 秒 × 每张已退单一条会灌爆台账）。
            # ⭐⭐ 生产事故（订单 20261025094455951882）：接手人是这家店的主人 ⇒ 整店不撤——判据=「本绑定的地址在接手人名下任何绑定的地址里」（含停用，见 `downstream_addresses_of_taker`），这张单在对方那边是他自己店里正在做的活单，退掉只会毁掉对方的活（事故本体：下家日志「订单已被发单方打手接取」）。
            #    ⛔ 与「已退成过」是两件事：这里一张都没退过、只是不该退，同样不复用 `ReconcileSkip`（同一条理由：不是归属裁定；逐单留痕由消费侧 `skipped`+人话承载）。
            if binding.jidanpu_base_url in taker_addresses.get(takers.get(order_id), set()):
                continue
            to_withdraw.append(
                ReconcileItem(
                    order=order,
                    shape=shape,
                    reason=(
                        "对方不该再有这张单（我方别的打手接了 / 已撤回 / 已删除）：要退单"
                    ),
                )
            )

    return ReconcileResult(
        to_launch=tuple(to_launch),
        to_withdraw=tuple(to_withdraw),
        to_record=tuple(to_record),
        to_unlist=tuple(to_unlist),
        skipped=tuple(skipped),
        at=now,
    )


# ══ 「有效来源单号」的唯一出处 ══════════════════════════════════════════
# 说明见模块 docstring 的那一节。⛔ 本函数是**唯一**判据：四个调用点（录单表单 + 顶层参数 /
# 录单后回读 / 认领预读 / 日志快路匹配）都走它，别处不许再写 `order_no or source_order_no`。


def effective_source_order_no(order) -> str:
    """录到对方时用的来源单号：`Order.source_order_no` 非空白 ⇒ 用它（业务方口径：来源单号要尽量一致）；空串/纯空白/`None` ⇒ 回落 `Order.order_no`。
    ⚠️ 只 strip 两端（复制粘贴的首尾空白不是单号一部分）、中间一字不动——改一个字符「回读」与「日志匹配」全对不上；空串/纯空白一律当「没有」（E-0021 同族，⛔ 不把空白当单号发出去）；
    ⚠️ 上游 `souresOrderID` 长度上限没有记录（E-0007）⇒ 不截断、不自造上限，真被拒时由录单失败人话（含上游 `msg`）响亮报出；两张我方单来源单号相同是如实登记的残差：照实返回（⛔ 不在这里发明去重），日志快路按链路 id 取最早并留人话台账（见 `_link_index`）。"""
    source = str(getattr(order, "source_order_no", "") or "").strip()
    if source:
        return source
    return str(getattr(order, "order_no", "") or "").strip()


# ══ outbox 消费者（spec §5.1/§5.3/§5.4/§8.4）══════════════════════════════════
# 把引擎算出的差集变成真实动作（录单/抛单/只录单/退单/改状态）。⭐「撤下」（业务方点名 ⇒ `KIND_UNLIST`）：我方接手这张单之后，对方那张单要停在「未分配」（不在他们抢单大厅、不指派给任何人）；与 `KIND_WITHDRAW`（退单）分清：撤下只把单从大厅拿回来 ⛔ 不作废；上游原语 `jidanpu.release_order_to_home`（候选与依据见该常量注释），写之前先读一遍确认还在大厅（本地 `in_hall` 可能过期）。
# ⚠️ `member.remote_taken`（本线自己的 `direction="in"` 台账 kind）由日志快路 `poll_log` 写，状态回拉 ⛔ 不在本模块；它是真接手（spec §8.2 A6），与旧线只记账不算钱的 `hall.remote_taken` 分清。

KIND_LAUNCH = "member.launch"
KIND_ASSIGN = "member.assign"
PENALTY_RECORD_APPROVAL_KEY = "penalty_record_approval_id"
PENALTY_RECORD_SOURCE_KEY = "penalty_record_source_order_no"
PENALTY_OPENAPI_RECEIPT_KEY = "penalty_openapi_record_receipt"
PENALTY_RECORD_ASSIGNED_STATUSES = (
    OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW, OrderStatus.ISSUE, OrderStatus.PAUSED,
)
KIND_UNLIST = "member.unlist"
KIND_STATUS_OUT = "member.status_out"
#: ⭐ 改了我方发单价（`Order.hall_price`）⇒ 把下游那张单的价一起跟过来。覆盖两种形态：已抛对方大厅的单（按新价重抛 ⇒ 改 `recFee`）与只录单的单（改接单价 `price`）。
#: ⚠️ 与 `peer_dispatch.notify_order_price_changed`（团团通租户 ↔ 租户，D-0152）不是同一条线：那条早就有，本线补的是第三方鸡蛋铺。
KIND_PRICE = "member.price"
#: ⭐ 触发点⑧：我方改了「接手项目」（`Order.project_text`，页面同名、是权威值）⇒ 改下游那张单的项目名称（标题）格。
#: ⛔ 不是 `Order.content`（「客户原始需求」=对方口径 `dlcontent`）：官方 API 没有改它的接口（另案 D-0217）；⚠️ 与 `KIND_PRICE` 不是一回事（两个字段、两个端点，⛔ 别合并）。
KIND_PROJECT = "member.project"
KIND_SOURCE_ORDER_NO = "member.source_order_no"

#: 本消费者认的 kind 全集（`run_outbox` 取活时按它过滤）。
OUTBOX_KINDS = (
    KIND_LAUNCH, KIND_ASSIGN, KIND_WITHDRAW, KIND_UNLIST, KIND_STATUS_OUT, KIND_PRICE,
    KIND_PROJECT, KIND_SOURCE_ORDER_NO,
)

# ── ⭐ 订单截图双向同步（计划 `2026-10-05-member-order-pics`）：A 段（派单传图）`KIND_ASSIGN` 消费时把我方订单截图+群聊二维码（业务方口径②「全传」）传下去，「传图失败不挡派单」（派单承重、传图附属，失败只记一条台账人话）；成功也留一条 `done` 台账（带张数+图种构成——否则「一张没传」与「传了没人知道」页面上长得一样）；这一跳关掉「对面已有图就不传」的短路（下家挂过任何图——含机器人自动挂的群二维码——默认口径会一张不传且毫无痕迹）。
# B 段（下家完成 ⇒ 拉图）：状态回拉命中「已完成」时先拉回下家的图存进我方订单截图、再无条件写「待验收」，「拉图任何失败都不挡待验收」（业务方口径③）；⭐ ②式那一半：①式走 `_pull_remote_screenshots`（`GET /OrderAjax/ViewPic`），②式没有那把凭据 ⇒ 改走 `_pull_openapi_screenshots`（官方 API `GetOrderProcessImgs` 全量进度图），两路落同一位置、同一份落库代码（`jidanpu_upstream_pics.store_remote_pics`）⇒ ②式那跳是替代被跳过的 `ViewPic` 不是另加；判重按「对象 key」（`remote_object_key`，⛔ 不是整条 URL——同一张图两个域名指向同一对象）。
# ⚠️ 与上家线口径刻意相反（那边「传图失败 ⇒ 不提交完成」，见 `jidanpu_upstream_pics.PicOutcome.blocks_completion`）⛔ 别顺手对齐（两种业务承诺各有理由）；三步唯一实现在 `jidanpu_upstream_pics`（⛔ 不建第二份）；台账 kind 刻意复用既有两个（A 段失败 `KIND_ASSIGN`、B 段 `KIND_STATUS_IN`，⛔ 不新增 kind）；「补传/重试/补拉」二期登记残差。

#: A 段要传的图种（业务方口径②「全传」= 订单截图 + 群聊二维码）。
MEMBER_PUSH_IMG_TYPES: tuple[int, ...] = (OrderScreenshot.IMG_ORDER, OrderScreenshot.IMG_GROUP_QR)

#: B 段**拉回**只允许订单截图：群码是本线自己推给下家的，不需要回收。
#: ⛔ 与 `MEMBER_PUSH_IMG_TYPES`（推：两种都传）不是同一个口径，别合并。
#: 依据：回环事故（推上去的群码又被拉回来，再补推 ⇒ 单内群码 2→4→8…）。
MEMBER_PULL_IMG_TYPES: tuple[int, ...] = (OrderScreenshot.IMG_ORDER,)


def _pushed_remote_keys(order, binding) -> set[str]:
    """本链路历史推送台账里登记过的下家 OSS 对象名并集（拉回侧判重的输入）。台账那一格是 `{本地 object_key → 对面 oss_key}`
    （`historical_pushed_pic_keys`），拉回侧要对照的是 `src` 里的远端对象 key ⇒ 只取 values 求并。"""
    return {
        oss_key
        for keys in historical_pushed_pic_keys(order, binding).values()
        for oss_key in keys
    }

#: A 段传图失败的人话（⛔ 不含图 URL / `accessid` / `signature` / 对象名）。
PUSH_PIC_FAILED_REASON = "派单已成功，但订单截图没传到下家：{reason}"
#: A 段**传成功**的人话（⭐ 这是运维唯一能看出「A 段到底干了没」
#: 的信号）。带**张数 + 图种构成**，⛔ 不带任何对象名 / URL。
PUSH_PIC_DONE_REASON = "订单截图已传到下家（共 {n} 张：{kinds}）"
#: 图种 → 人话（只有这两种会被传上去：`IMG_TYPE_TO_UPLOAD` 是硬闸门 ⇒ 别的取值会响亮 `KeyError`）。
PUSHED_KIND_LABELS: dict[int, str] = {
    OrderScreenshot.IMG_ORDER: "订单截图",
    OrderScreenshot.IMG_GROUP_QR: "群二维码",
}
#: B 段拉图失败的人话（⛔ 同上）。⚠️ 它写在状态回写**之前** ⇒ ⛔ 不说「已转待验收」。
PULL_PIC_FAILED_REASON = "下家这单已完成，但订单图没拉回来（{reason}）；下一步照口径回写状态"

#: ⭐ 一轮最多拉几张单的图（真机缺陷）：一轮一次性命中几百张「刚完成」的单 ⇒ 逐张 `ViewPic`+逐张下载把整轮拖到十几分钟、挡住下一轮节奏。
#: ⚠️ 超出名额的单不丢：状态照写（业务判据一字不动），只有图留到下一轮补（待办键=`PULL_PICS_DEFERRED_REASON` 台账，由 `_pull_deferred_pics` 拾起）。
#: ⚠️ 取值 20：每单通常 1–3 张图、20 张的 `ViewPic`+下载可接受；积压按 20 张/轮收敛（回拉 10 分钟一轮 ⇒ 一小时最多补 120 张单的图）。
PULL_PICS_PER_ROUND = 20

#: 名额用完时留的那句台账人话。⚠️ 它同时是**下一轮补拉的待办键**（`_pull_deferred_pics`
#: 按这句话把待办找出来）⇒ 改文案必须**同一次**改那个查询，否则待办变孤儿（静默不补）。
PULL_PICS_DEFERRED_REASON = "本轮拉图名额用完，这张单的图留到下一轮补拉"

#: ⭐ 「**这一条线还没有 A 段（派单传图）这一跳**」的人话（`{line}` = 那条线的短名）。
#: ⚠️ 与 `PUSH_PIC_FAILED_REASON` **分开写**：那句是「传了但失败」（要重试 / 查上游），
#: 这句是「这一条线压根还没有这一跳」（要等接线）——两种成因要做的事不同，⛔ 别合成一句。
PUSH_PIC_NOT_WIRED_REASON = (
    "派单已成功，但这一条线（{line}）的截图传递二期还没通：本次没把订单截图传到下家"
    "（那一条线接通之后才谈补传）"
)

#: ⭐ E-0282/H1：A 段每次推送成功时，把 `{本地 object_key → 对面 oss_key}` 映射登记进当次台账行 `payload`（`JidanpuSyncEvent.payload` 是 JSONField、无迁移）；
#: 它是「本地 sha256 入站键」与「对面随机 oss_key」两套永不相交值空间之间唯一的桥——`only_missing` 判重（`jidanpu_upstream_pics.push_order_screenshots`）按它对照远端。
PUSHED_PIC_KEYS_PAYLOAD = "pushed_pic_keys"

#: ⭐ E-0282 / B2：到图触发补推排的那条 `KIND_ASSIGN` 在 payload 里带的标记
#: （与存量补推命令 `repush_member_pics` **同一份**——两处共用，⛔ 别各写一个字面量）。
#: 用途只有「台账上分得清这条 assign 是补图推的、不是人工指派/接单排的」。
PIC_RE_PUSH_PAYLOAD: dict = {"repush": True}
PIC_LINK_SNAPSHOT_KEY = "picture_link"
PIC_REPUSH_INTENT_KEY = "picture_intent_id"


def _picture_link_snapshot(link) -> dict:
    return {
        "id": link.pk, "remote_id": link.jidanpu_order_id,
        "listed_at": link.listed_at.isoformat() if link.listed_at is not None else None,
    }


def _is_pic_repush(kind, payload) -> bool:
    return kind == KIND_ASSIGN and (payload or {}).get("repush") is True

#: 失败退避：`min(2 ** attempts × 基准, 上限)`（spec §10 的默认值）。
OUTBOX_BACKOFF_BASE_SECONDS = 60
OUTBOX_BACKOFF_MAX_SECONDS = 3600
#: 到达这个尝试次数后**不再自动重试**、留台账待人看（spec §10 的「到阈值不再无限重试」）。
OUTBOX_MAX_ATTEMPTS = 5

#: ⭐ **单条事件的处理总时限**：一个事件从开始到结束超过这么多秒 ⇒ 记 `failed`
#: 并走既有退避 / 计数，**不让它占住整轮**（后面的事件照常处理）。
#: ⚠️ 实现选型见 `_outbox_event_deadline`：主线程用 `SIGALRM` **真的打断**（不是只记时间）。
OUTBOX_EVENT_DEADLINE_SECONDS = 60
#: 超时事件的人话原因（`{seconds:g}` 让 60 渲染成 `60`、测试里的亚秒值也读得出来）。
OUTBOX_EVENT_TIMEOUT_REASON = "单条事件处理超时（>{seconds:g}s）"

# ── 人话原因（⚠️ 只回显业务壳的 `msg`；⛔ 不回显整包 body/`str(exc)`，spec §8.5）──────
# ⚠️ 现口径（真机实测定）：⛔ 不回显上游原文——按字面做的后果是台账只剩「鸡蛋铺拒绝了这次录单」、根因不可知（下家系统里没有我们的日志，运维没有任何别的线索）。
#    ⇒ 现在只把业务壳的 `msg` 接上（`_upstream_reason`，给人看的一句业务话）；凭据脱敏由 SDK 的 `transport` 负责（`ApiError.body` 已 `redact`）。

#: 来源名匹配不到唯一一条 ⇒ 不能录单（与 `detect_binding` 的第 2 条同一条事实，但这是台账文案）。
RECORD_SOURCE_REASON = "鸡蛋铺里匹配不到唯一的下家来源，无法录单"
#: 录单被上游拒（失败信封）。
RECORD_FAILED_REASON = "鸡蛋铺拒绝了这次录单"
#: 录单后回读不到远端订单号（含空 `orderId`：写 `""` 会让下一次又满足「没录过」）。
RECORD_READBACK_REASON = "录单后回读不到对方订单号，本轮只保住「已录过」这个事实"
#: ⭐ 录单**之前**那次「对方是不是已经有这张单」的回读失败（认领预读）。
#: ⚠️ 与上面那句**分开写**：这一跳发生在录单之前，写「录单后…」会让排障的人以为单已经录过去了。
RECORD_PRE_READ_REASON = "录单前回读「对方是否已有这张单」失败，本轮不录（下一轮重试）"
#: ⭐ 认领到的远端单**已经在大厅里** ⇒ 不重复抛单。见 `_adopted_throw_verdict`。
ADOPTED_ALREADY_IN_HALL_REASON = (
    "对方那张单此刻还挂在他家大厅：本次不重复抛单，链路按「在大厅」记下"
)
#: ⭐ 认领到的远端单**已退单**、而这条线**没有「先改回未分配」这条腿**（②式）⇒
#: 终态跳过的人话。只服务没有「先改回未分配」这条腿的线（②式）。
#: ①式走补抛，不落这一档。字符串一个字不动：排障和燃料判据认这句。
ADOPTED_REFUNDED_REASON = (
    "对方那张单已退单：本次不抛单（把它重新挂进大厅是错的），等状态回拉把两边的状态对齐"
)
#: ⭐ D-0257 细化：补抛「已退单」之前的**先改回「未分配」**那一跳被上游拒绝。
#: ⚠️ 与 `ADOPTED_REFUNDED_REASON` 分工：那句是「这条线办不了 ⇒ 终态跳过」；这句是
#: 「办了、被拒了 ⇒ 可重试失败」（`_upstream_reason` 会把上游原话接在后面，走既有退避）。
REFUND_UNDO_FAILED_REASON = (
    "对方那张单已退单，改回「未分配」未成功，本轮不抛单（下一轮重试）"
)
#: ⭐ 认领到的远端单**已被人接走 / 已流转** ⇒ 不补抛、不打扰对方，交给快路与回拉。
ADOPTED_NOT_UNASSIGNED_REASON = (
    "对方那张单已经不是「未分配」（可能已被对方打手接走或已流转）：本次不抛单，"
    "等日志快路与状态回拉把两边对齐"
)
#: ⭐ 指派那一路遇到「本单没有发单价」⇒ 不录单（下家的接单价就是它，没有价就没有可写的东西）。
#: ⚠️ ⛔ 不回落客户价 `total_price`（那是底价）；也⛔ 不写 `"0"`（那等于告诉下家「这单不要钱」）。
#: 等填上发单价之后由 180 秒的对账补录（`to_record` 那一档）。
ASSIGN_NO_PRICE_REASON = (
    "该单没有发单价（下家的接单价就是它），本次不录单：填上发单价后由对账补录"
)
#: ⭐ 地址级去重（裁定 3）命中。
ADDRESS_DUPLICATE_REASON = "同一套鸡蛋铺里已有这张单，本次不重复录"
#: ⭐⭐ **A 族认领撞键（E-0315）**：建链撞 `(binding, jidanpu_order_id)` 唯一约束、重查仍查不到
#: = 链行挂在**另一张**我方单上（多为已退款的老单占着同一条远端单）⇒ 认得出的人话事实，
#: 终态 skipped（⛔ 不再 raise 烧重试）；各调用支拿 `(None, False)` 都用这一句收口 ⛔ 不抄第二份文案。
LINK_OCCUPIED_REASON = (
    "对方已有此单：这条链路被同店另一张单占用（多为已退款的老单占着同一条远端单），"
    "本单无法自动建链、不再自动重试，请人工核对两张单后处理"
)
#: 抛单被上游拒（spec §9.1 第 3 条末段 / §13-R7）：**必须点名那条权限**——
#: 「发到内部抢单大厅」（节点 36）在鸡蛋铺里**没有页面可探**（挂在接口上）⇒ 检测只能间接判，
#: 这里写失败时这句人话是用户唯一的线索（少一句，用户看到的就是一次无因的「拒绝了」）。
THROW_FAILED_REASON = (
    "鸡蛋铺拒绝了这次抛单：可能是该账号缺『将订单发到内部抢单大厅』权限，"
    "请在下家鸡蛋铺里给这个职位勾上"
)
#: 抛单后回读不确认（`isThrowOrder != "Y"`）。
THROW_CONFIRM_REASON = "抛单后回读确认失败（isThrowOrder 不是 Y）"

# ── ⭐ 终态停重排护栏（D-0370）：终态行挡得住重试 ⛔ 挡不住对账每 180 秒按同一差集重排（E-0315/E-0316 两族死循环同形）──
# 闸收口在**入队口**：自动路径（`rearm=False`）建新行前，同键已有终态行且驱动判定的底层状态相对那次终态无变化 ⇒ 不排（变化才照排）。
# 「无变化」判据=状态指纹 `_requeue_guard_fingerprint`（⛔ 不含每轮必变的时间戳，否则护栏永不生效）；无指纹行 fail-open（发版切换期自愈）；⭐ 豁免=D-0362「转人工」支终态行不写指纹（`_strip_requeue_guard_fp`）——那是每窗重申的有界循环，护栏不得掐死。

#: 终态行 payload 里「排队那一刻的状态指纹」键名（写方与读方共用这一个字面量）。
REQUEUE_GUARD_FP_KEY = "requeue_guard_fp"


def _strip_requeue_guard_fp(event) -> None:
    """把事件行 payload 里的状态指纹抹掉（库与内存实例同步）。唯一调用点=`member.launch` 的 **D-0362「转人工」支**：该支是每窗重申的有界循环，带上指纹会被终态停重排闸「指纹不变 ⇒ 永闸」永久掐死（恢复只剩人工重抛）⇒ 落终态时抹掉、护栏对无指纹行 fail-open；循环的有界性由 D-0362 自己的窗机制（`reconcile.handoff_refs` + `NO_ORDER_HANDOFF_NOTE_WINDOW`）保证。"""
    payload = dict(event.payload or {})
    if REQUEUE_GUARD_FP_KEY not in payload:
        return
    payload.pop(REQUEUE_GUARD_FP_KEY)
    _persist_payload(event, payload)
    event.payload = payload


def _requeue_guard_fingerprint(order, binding) -> str:
    """自动重排闸的**状态指纹**：驱动对账判定的字段拼成可比较的串；取哪些字段的判据=「改了它，对账对该单的差集结论会不会变」——
    `order.status`（三档第一驱动）/ 发单价**有无**（补了价要能重排）/ 链路行的**归属**与 `in_hall`/`withdrawn_at` 两档标记（to_launch/to_withdraw/to_unlist 判据全落这几列；⭐ 远端单号进指纹还有一层：数据修复把链行改指另一张单后指纹自动变，无需手工解锁）/ 接手人集合（MAIN 绩效行）。
    ⛔ **刻意不含**任何时间戳/更新时刻类字段（每轮必变会让护栏永不生效），也⛔ 不含绑定级开关（`enabled`/`hall_sync_enabled`——它们翻转只会让对账**排得出来**，差集侧本来就在判）。"""
    link = JidanpuMemberOrderLink.objects.filter(binding=binding, order=order).first()
    takers = ",".join(
        str(pk)
        for pk in sorted(
            Performance.objects.filter(
                order_id=order.pk, emp_type=EmpType.MAIN, user_id__isnull=False
            ).values_list("user_id", flat=True)
        )
    )
    return "|".join(
        (
            f"status={order.status}",
            f"price={0 if order.hall_price is None else 1}",
            "link=none"
            if link is None
            else (
                f"link=r{link.jidanpu_order_id}"
                f"h={int(bool(link.in_hall))}"
                f"w={int(link.withdrawn_at is not None)}"
            ),
            f"takers={takers or '-'}",
        )
    )


def _terminal_requeue_guard(order, binding, kind, current_fp: str):
    """同键已有终态行且状态无变化 ⇒ 回那条终态行（⛔ 不排新行）；否则回 `None`。
    「终态」=`skipped`，或 `failed` 且已烧完重试额度（`attempts >= OUTBOX_MAX_ATTEMPTS`，`run_outbox` 永不再取它）；在飞行不在本闸管辖（在飞去重已收）。
    比对口径=取**最新**一条终态行 payload 里的指纹（排队那一刻拍的）与 `current_fp` 比；行上无指纹（发版前旧行 / 人工路径建的行 / ⭐ D-0362 转人行——落终态被 `_strip_requeue_guard_fp` 抹掉，每窗重申循环豁免）⇒ 不拦（fail-open）。"""
    terminal = (
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=kind, target_type="order",
            target_ref=order.order_no, payload__member_binding_id=binding.pk,
        )
        .filter(Q(status="skipped") | Q(status="failed", attempts__gte=OUTBOX_MAX_ATTEMPTS))
        .order_by("-id")
        .first()
    )
    if terminal is None:
        return None
    stored = (terminal.payload or {}).get(REQUEUE_GUARD_FP_KEY)
    if not isinstance(stored, str) or not stored:
        return None
    if stored == current_fp:
        return terminal
    return None

# ── ⭐ D-0318（业务方口径）：「已退单」重新抛单前的前置修复那一跳（「先改成未分配然后重抛」原话）──
#: 落点=`_consume_launch` 抛单**之前**那一次 `RecordPrimitives.throw_prepare`（①式=`_member_throw_prepare`）；与 D-0257「认领」同一套判据（`adopted_verdict` 四取值），差别只是这次的单早就在我方链路里、不需要认领。⭐ 抛单前回读对方那张单读不出来（上游报错/查无此单）⇒ 本轮不抛走既有退避；⚠️ 与 `THROW_CONFIRM_REASON`（**写后**回读）分开写——这一跳在写之前，混成一句会让排障的人以为抛单已发出（同 `RECORD_PRE_READ_REASON` 的理由）。
#: 「逐字查无」（第一页无命中且非 ApiError）=终态事实可新建卡（撤回与否、人工与否不再限制；处置只在 `_consume_launch`：挂起期不新建、人工重抛不限次、自动且已自动新建过则转人工、否则全新建）；超时/5xx/解析不出/验签失败仍是本句：拒抛并计熔断燃料——字面值 ⛔ 一个字不能动，燃料表按前缀认它。
THROW_PREPARE_UNREADABLE_REASON = (
    "抛单前回读对方那张单的状态失败（读不到或查无此单），本轮不抛单（下一轮重试）"
)
#: ⭐ D-0359 §11：三重条件齐时的放行人话（「全新建卡」那一档的台账落款，见上面那段的①②③）。
#: ⭐ D-0362 起它收窄为「**人工经典档**」（人工重抛 + 退成过）那一格的落款——人工路径
#: 文案不变（既有用例逐字钉它）；其余放行档落下面那句。
THROW_PREPARE_RECREATE_REASON = "撤回单人工重抛：店里查无旧卡 ⇒ 全新建卡"
#: ⭐ D-0362：逐字查无在**非人工经典档**（自动重试 / 人工但从未退成过）时的放行人话
#: （「全新建卡」那一档的台账落款）。口径出处 = 业务方拍板「明确了没有 那就是没有 直接新建」。
THROW_PREPARE_RECREATE_AUTO_REASON = "对端明确查无此单，按拍板转新建"
#: ⭐ D-0318：前置回读到的远端状态不是「未分配」（「代练中」/已完成/已流转……）⇒ 不改它的状态、也不抛，终态跳过+点名读到的那格。
#: ⚠️ 业务方明令：⛔ 不许对「对方已有真实接手人且正在做单」的单做任何自动改状态（自动改会打断对方干活）——「代练中」就是那一档。
THROW_PREPARE_TAKEN_REASON = (
    "对方那张单此刻不是「未分配」（回读 orderSta=「{label}」）：本次不抛单、也不自动改它"
    "的状态（有人在做或已流转的单不许动），等日志快路与状态回拉把两边对齐"
)
#: ⭐ D-0318：先改回「未分配」成功、补抛仍被拒。业务方兜底「删除旧单 + 重新录单」⚠️ 鸡蛋铺 SDK 侧没有删单封装（`/OrderAjax/DelOrderByIDX` 只有端点登记与一次真机写探针，`src/jidanpu/` 与 `jidanpu.py` 没有删单函数，`git grep DelOrderByIDX src/apps` 只命中权限目录；`order_writes.delete_order` 是我方本地软删、`dailiantong_client.delete_order` 是另一平台，都不是下家删单）⇒ 如实登记为不可行，⛔ 不拿裸端点硬写（会新增未经验证的写面）。
#: 后面的句子里接抛单自己那句原因。
THROW_AFTER_UN_REFUND_FAILED_REASON = (
    "已把对方那张单改回「未分配」，但抛单仍被拒（「删除旧单重新录单」本线不可用："
    "没有删单的 SDK 函数）⇒ 需人工核对；"
)
#: 旧抛单遇到更新的撤回时停下，避免自动重试把单重新挂回对方大厅。
STALE_LAUNCH_AFTER_WITHDRAW_REASON = (
    "这次发单是撤回之前就排下的旧事件自动重试（排队时刻不晚于撤回完成时刻）："
    "对方那张单已退单，本次不改回「未分配」、也不重抛（否则会把已撤回的单自己挂回对方大厅）；"
    "要重新发单请重新发起一次"
)

#: 退单时订单行已无法定位（本仓无硬删 ⇒ 只可能来自人工改库 / 数据损坏）。
WITHDRAW_ORDER_MISSING_REASON = "订单已无法定位，无法退单，请人工核对"
#: 退单后回读出错（传输层）⇒ 本轮判不了，下一轮重试。
WITHDRAW_READBACK_REASON = "退单后回读上游出错，本轮无法确认（下一轮重试）"
#: 退单后回读**没有**显式「已退单」⇒ 不算完成（「读不到」≠「已退单」）。
WITHDRAW_CONFIRM_REASON = "退单后回读未确认已退单，未完成"
#: ⭐⭐ **回读确认支的次数封顶（E-0316，业务方拍板「按次数」）**：写后退读**读得到**但状态不是「已退单」这一支，
#: 第 3 次仍不确认 ⇒ 终态 `skipped` 收口（⛔ 不再按全量 5 次烧、更⛔ 不再被对账重排——重排那半由 D-0370 收口）；
#: ⚠️ 计数=单行内计（事件行自己的 `attempts`，`_retry` 每失败一次 +1）：新排的行从 0 重数，但状态无变化时对账排不出新行（D-0370）⇒ 一族实际只烧 3 发；判据钉在 `tests.test_jidanpu_member_consume.WithdrawConfirmGiveupTests`。
WITHDRAW_CONFIRM_MAX_ATTEMPTS = 3
WITHDRAW_CONFIRM_GIVEUP_REASON = "上家侧大概率已删卡、退款我方已完成，不再追认"
#: ⭐⭐ 生产事故的**承重**人话：接手人是**这家下家店自己的账号**（他那份绑定可能停着、真正在推单的是同店另一份绑定）⇒ **撤回作废**，那张单还在下家做。
#: 消费侧 `_consume_withdraw` 在真正发上游**之前**用它终态 `skipped`——排队侧那三处只挡新排的事件，挡不住事故现场那条**已经排好**的台账。
WITHDRAW_SAME_STORE_REASON = "接手人是这家下家的账号，撤回作废（单还在下家做）"
WITHDRAW_HALL_ONLY_KEY = "hall_only"
WITHDRAW_NOT_LISTED_REASON = "鸡蛋铺订单已离开大厅或已有人接手，本次不退单"

#: ⭐ D-0325：上家镜像隐藏单的「上游 gone + 下家已接」竞态闸（E-0236 同形）——隐藏单没有 MAIN 接手人 ⇒ 同店闸救不了它（`_main_taker_id` 回 None）；
#: 退单真正发上游之前先回读一次远端，读到的 `orderSta` 不是「未分配/已退单」（有人在做的「代练中」/已完成…）⇒ 终态跳过保下家活单 ⛔ 不连坐退单；`{label}`=实际读到的值（排障时看不出读到什么就等于没记）。
WITHDRAW_UPSTREAM_LIVE_REASON = (
    "上家镜像卡已消失，但下家这张单远端状态是「{label}」（有人在做/已完成），"
    "为保护对方活单本次不退单，请人工核对"
)
#: 撤下时本地没有链路 ⇒ 它从未录到对方系统（与 `WITHDRAW_NO_LINK_REASON` 同因不同动作）。
UNLIST_NO_LINK_REASON = "本线台账里没有这张单的链路，它从未录到对方系统，本次撤下作罢"
#: 撤下后回读出错（传输层）⇒ 本轮判不了，下一轮重试。
UNLIST_READBACK_REASON = "撤下前后回读上游出错，本轮无法确认（下一轮重试）"
#: 写完之后回读仍然 `isThrowOrder == "Y"`（或读不到）⇒ 撤下没成不算完成。⚠️ 这句同时覆盖两档：还挂在大厅、以及读得到但不是目标态（已退单/已流转）——都「未确认它停在『未分配』」。
#: 后者（`UNLIST_VERDICT_OTHER`）走 `UNLIST_CONFIRM_OTHER_SUFFIX`：同一句再缀上实际读到的 `orderSta` 值（见 `_unlist_other_reason`）。
UNLIST_CONFIRM_REASON = "撤下后回读未确认对方那张单停在「未分配」，未完成"
#: `UNLIST_VERDICT_OTHER` 档在既有那句话后面缀上的尾巴（`{label}`=实际读到的标签值）。⚠️ 形状照既有 reason：句尾一个括号补充 ⛔ 不另起一句；截断与长度见 `_unlist_other_reason`。
#: ⚠️ `str.format` 只作用在这一段（基句 `UNLIST_CONFIRM_REASON` 原样拼接——基句是给人读的句子，以后出现 `{`/`}` 不该把判据炸掉）。
UNLIST_CONFIRM_OTHER_SUFFIX = "（回读 orderSta 读到「{label}」）"
#: ⭐ 回读确认「对方那张单已被接走」（`isThrowOrder != "Y"` 且 `orderSta == 代练中`）⇒ 是对方抢先接走、不是我们撤下来的。⚠️ 该原语对已被接走的单无效、却仍回 `code=1「撤销成功」`
#: （真机读数，原始输出只在**不进 git** 的 `.tools/recon/`）⇒ ⛔ 不许当成功；⛔ 本档也不自动退单（动钱口径，需业务方裁定），只把事实响亮地留在台账里。
UNLIST_TAKEN_BY_PEER_REASON = (
    "对方已把这张单接走 ⇒ 撤下抛单对该单无效；业务目标『停在未分配』未达成，"
    "需人工核对是否要退单"
)
#: ⭐ 回读到了那一行、但 `orderSta` **读不出来**（键缺失 / `None` / 非字符串）⇒ 判不了目标态。
#: 「判不了 ≠ 目标达成」（E-0004 同族）⇒ 重试，⛔ 不许当成撤下成功。
UNLIST_STATE_UNREADABLE_REASON = (
    "撤下回读里读不到对方那张单的状态（orderSta 缺失或不是文本）："
    "本轮判不了它有没有停在「未分配」（下一轮重试）"
)
#: `_unlist_remote_verdict` 的四种判定（撤下抛单**回读**判据格的唯一出处；
#: ⛔ 别在判据处再散一个裸字面量）。
UNLIST_VERDICT_UNASSIGNED = "unassigned"  # 目标态：不在厅 + 停在「未分配」
UNLIST_VERDICT_TAKEN = "taken"  # 对方打手抢先接走（`代练中`）
UNLIST_VERDICT_UNREADABLE = "unreadable"  # `orderSta` 读不出来 ⇒ 判不了
UNLIST_VERDICT_OTHER = "other"  # 读得到、但既非目标态也非「接走」
#: ⭐ 取不到下家「录入客服」uid ⇒ **不填那个键** + 一条人话台账（业务方）。
#: ⚠️ 文案里必须点明「录入客服」这四个字（页面上那一格就叫『接单销售客服』，
#:    而对方接口里的字段名是 `inputOrderKefu`）：运维要一眼看出缺的是哪一格。
INPUT_ORDER_KEFU_UNRESOLVED_REASON = (
    "取不到下家的『录入客服』uid（认不出当前登录账号，或客服名单读不到）："
    "这张单没有填对方的客服，请人工核对"
)
#: 改状态时本地没有链路。
STATUS_NO_LINK_REASON = "本线台账里没有这张单的链路，无法改对方状态"
#: ⭐ 这一档按业务口径不推（业务方把推送集收窄成三档 + 一个来源态例外）；说清「是哪条口径把它挡下的」，让人一眼看出不是漏了而是有意的（⛔ 不静默）：
#: 「代练中」只有来源态 ∈ {已完成, 待验收, 已退单} 那一档才回改；来源态是池内两态（未分配/待抢单）的「代练中」= 我方指派/打手在我方大厅接单 ⇒ 对方那张单要停在「未分配」（业务方口径一字未改）。
STATUS_NOT_PUSHABLE_REASON = (
    "这一档按业务口径不推给下家（只推『问题单 / 已暂停 / 已退单』；『代练中』只有来源态是"
    "『已完成 / 待验收 / 已退单』时才回改）：本次不改对方状态"
)
#: ⭐ 复活档接手人闸·第一格（2026-11-05 业务方拍板，E-0318）：来源态例外要回推「代练中」，但这张单
#: **没有接手人**（MAIN 绩效行缺行/无主）⇒ 不推——推了会把下家已撤的卡改回「代练中」，
#: 而「已完成」档本就不推 ⇒ 店卡永停「代练中」（E-0318 现场：某租户下家店永停卡）。
REVERT_PUSH_NO_TAKER_REASON = (
    "这张单恢复「代练中」时我方没有接手人：无接手人的恢复不推下家，"
    "要让下家重新接单请重抛大厅"
)
#: ⭐ 接手人同店闸·第二格（E-0318；复活档与问题单/已暂停两档共用）：有接手人，但这条绑定指向的店
#: 不是接手人名下的店 ⇒ 本次不改对方状态。⚠️ 同店判据 = `jidanpu_base_url in
#: downstream_addresses_of_taker(taker_id)`（按店地址、含停用绑定；⛔ 不是 `user_id ==`——同店两条绑定/停用绑定会判漏，2026-10-25 生产事故形状，函数 docstring 有现场）。
STATUS_PUSH_NOT_TAKER_STORE_REASON = (
    "接手人不是这家店的人：这一档状态只同步接手人名下的店，本次不改对方状态"
)
#: ⭐ 手动推送**额外允许**的「待验收」那一档的标签（计划 §3）。
#: ⚠️ 「已完成」那一档的标签**不在这里**：它复用 `REMOTE_DONE_STATUS`（全仓唯一出处），
#: 由 `_manual_push_label` 现取 ⇒ ⛔ 别在这里再写一份「已完成」字面值。
MANUAL_PENDING_REVIEW_LABEL = "待验收"
#: ⭐ 手动推送**防倒退**（计划 §3）：目标 = 待验收、而推前回读对方**已是「已完成」** ⇒ 终态跳过。
#: 为什么必须有：回拉那条线会把对方「已完成」写成我方「待验收」；不挡的话，用户点一下手动推送
#: 就会把对方**已经完成**的单改回「待验收」，两条线互相打架（钱与验收口径都被搅乱）。
MANUAL_PENDING_REVIEW_AFTER_DONE_REASON = "对方已是已完成，不往回改"
#: ⭐ 同一条防倒退的第二档：对方已退单 ⇒ 同样不往回改。单独一句是因为退单与完成是两种现场，运维看台账要能分辨（⛔ 合成一句就说不出是哪种）。
#: ⚠️ 残差（如实登记）：`问题单`/`已暂停` 两档今天不防倒退——业务方只点名了「已完成/已退单」（钱的终态）；要扩它们时改这里与 `_consume_status_out` 那一处判据（⛔ 别在别处再写一份）。
MANUAL_PENDING_REVIEW_AFTER_REFUNDED_REASON = "对方已是已退单，不往回改"
#: 改状态被上游拒。
STATUS_FAILED_REASON = "鸡蛋铺拒绝了这次改状态"
#: ⭐ **推前回读**那一跳上游报错 ⇒ 判不了「对方是不是已经是目标态」。
#: 判不了就**不盲目照推**（写下去可能正好撞上「当前已是…状态，无需更改」那个失败信封，
#: 把这一跳记成失败 + 退避）⇒ 按退避重试。
STATUS_PRE_READ_FAILED_REASON = "推送前回读对方那张单时上游报错，本轮判不了它是否已是目标态"
#: ⭐ **写后回读**没读到目标态（读到别的 / 读不到）⇒ 判「这一跳没成功」。
#: ⚠️ 文案必须点明「**写后未能确认**」：读到别的与读不到是两种现场，但都不是「已到位」。
STATUS_CONFIRM_REASON = "改状态后回读未确认对方那张单已变成目标态（写后未能确认）"
#: 写后回读那一跳上游报错（与 `WITHDRAW_READBACK_REASON` 同形）。
STATUS_CONFIRM_READ_FAILED_REASON = "改状态后回读上游出错，本轮无法确认写后的终态"
#: 改价时本地没有链路（与 `STATUS_NO_LINK_REASON` **同因不同动作**：那两句各自点名自己的动作，
#: 照 `UNLIST_NO_LINK_REASON` 对 `WITHDRAW_NO_LINK_REASON` 的既有处置 ⇒ ⛔ 不复用「改对方状态」
#: 那句——它会把「改价」说成「改状态」，台账上是一句假话）。
PRICE_NO_LINK_REASON = "本线台账里没有这张单的链路，无法改对方接单价"
EDIT_WITHDRAWN_COPY_REASON = "原下家副本已撤回，本次不再同步改动"
#: 改价时这张单没有发单价（`Order.hall_price is None`）⇒ 没有可同步的价。⚠️ ⛔ 不复用 `ASSIGN_NO_PRICE_REASON`（控制方裁定）：那句落点是「还没录单 ⇒ 等 180 秒对账的 `to_record` 补录」，
#: 而本档是给已录进去的单改价、对账不查价（计划 §5 残差 1）⇒ 照抄会让台账说「由对账补录」而实际永远不会补——台账上一句假话（与 `PRICE_NO_LINK_REASON` 拒绝复用 `STATUS_NO_LINK_REASON` 同一条理由）。
#: ⚠️ 真实可达性很低：四个写价口都带值才写 ⇒ 这一档基本只对「历史/手造事件」生效，但它是终态（⛔ 不重试）。
PRICE_NO_PRICE_REASON = "该单没有发单价，本次没有可同步给下家的价（补上发单价后请再改一次价）"
#: 改价被上游拒（失败信封）。
PRICE_FAILED_REASON = "鸡蛋铺拒绝了这次改接单价"
#: 写后回读**没读到目标值**（读到别的价 / 读不到）⇒ 这一跳没成功（「读不出来」≠「改好了」）。
#: ⚠️ 文案点明「写后未能确认」：与本文件既有的 `STATUS_CONFIRM_REASON` 同形。
PRICE_CONFIRM_REASON = "改接单价后回读未确认对方那张单的接单价已是目标值（写后未能确认）"
#: 写后回读那一跳上游报错（与 `STATUS_CONFIRM_READ_FAILED_REASON` 同形）。
PRICE_CONFIRM_READ_FAILED_REASON = "改接单价后回读上游出错，本轮无法确认写后的接单价"
#: 还挂在对方大厅 ⇒ 按新价**重抛**之后，回读里 `isThrowOrder == "Y"` 但 `recFee` **不是**
#: 刚算的那个值（抛单价那一格没跟上）。⚠️ 单独一句：`THROW_CONFIRM_REASON` 只点了
#: `isThrowOrder`，用它会漏说「价没对上」这半条事实。
PRICE_THROW_FEE_CONFIRM_REASON = (
    "重抛后回读未确认对方那张单的发单价（recFee）已是目标值（写后未能确认）"
)
# ── ⭐ 「接手项目」（`Order.project_text`）那一档的人话（触发点⑧）──────────
#: 改项目时本地没有链路（与 `PRICE_NO_LINK_REASON` **同因不同动作**：各自点名自己的动作）。
PROJECT_NO_LINK_REASON = "本线台账里没有这张单的链路，无法改对方那张单的项目名称"
#: 这张单的项目名称是空的 ⇒ 没有可同步的值。⚠️ 终态（⛔ 不重试）：对方那一格也是必填（②式建单的 `project` 必填），发空串只会被拒、白烧额度；
#: 填上后会自愈（下一次编辑保存真的改了这一格 ⇒ 触发点会再排一条），所以说清「补上之后请再保存一次」。
PROJECT_NO_PROJECT_REASON = (
    "该单的项目名称是空的，本次没有可同步给下家的项目名（补上之后请再保存一次订单）"
)
#: ⭐ 这张单的游戏名是空的 ⇒ 没有可同步的值。与 `PROJECT_NO_PROJECT_REASON` 刻意对称（同一档处置：终态 `skipped` + 人话）：②式 `ModifyOrder` 的 `gameName` 是对方必填参数
#: ⇒ 发空串只会被拒、按退避反复重试直到烧完额度进 `abandoned`（⚠️ 单测抓不到：假上游不校验这一格——E-0227「假上游比真接口宽松」族）；⛔ 不回落猜测值（编一个游戏名会写进对方那张真单、甚至把单改到别的游戏上，比失败更坏——见 `jidanpu_member_openapi_outbox._game_name` docstring）。
#: ⚠️ 可达性如实登记：`Order.game` 是 NOT NULL FK、`Game.name` 在序列化器与 `peer_inbound.resolve_game` 都不收空名 ⇒ 正常路径到不了，这一档是兜底（历史行/手造行/别的写入路径）；自愈：补上非空游戏名再保存一次（重新排一条事件）。
PROJECT_NO_GAME_NAME_REASON = (
    "该单的游戏名是空的，改项目那一发要带上游戏名（对方那一格必填）⇒ 本次没有可同步的项目名，"
    "请先补上游戏名、再保存一次订单"
)
#: 改项目被上游拒（失败信封）。
PROJECT_FAILED_REASON = "鸡蛋铺拒绝了这次改项目名称"
#: ⭐ ①式（账号+密码登录）这条路今天办不了这一档 ⇒ `unsupported` 人话。必须点名两件事（⛔ 少一件就等于「说不清为什么没做」）：候选端点是谁；为什么没接（方法/参数/成功判据都没实测过）。
#: 依据（当场复核过，⛔ 不是「没找到」）：`docs/jidanpu-endpoints.json` 确有 `/OrderAjax/ChangeOrderProject`，但它 `evidence="读码"`、`params=null`、`success_rule=null`、描述「写操作，未调用」；
#: SDK 清单 `src/jidanpu/spec/frontend_calls.json` 那格连 HTTP 方法都是 `null` ⇒ 按 `AGENTS.md` §5-E（旧清单/SDK 路径必须实测复核后才可用，E-0007/E-0038 同族），拿它写生产上 1000+ 张单不可接受。
UPSTREAM_PROJECT_NOT_WIRED_REASON = (
    "①式（账号 + 密码登录）这条路还没有经过实测的「改对方订单项目名称」写接口："
    "候选端点 /OrderAjax/ChangeOrderProject 的 HTTP 方法、参数含义与成功判据都没实测过"
    "（清单里它是「读码」级证据、标着『写操作，未调用』）⇒ 本次不改对方那一格；"
    "要同步这一格，请人工在鸡蛋铺里改，或把这个账号改成②式（官方开放平台 API）绑定"
)
#: 订单行已不在（已软删）⇒ 不推送（⛔ 不拿一张已删的单去打上游）。
ORDER_MISSING_REASON = "订单已删除或已无法定位，本次不推送"
#: 两张我方单的**有效来源单号相同**时的人话台账（见 `_link_index`）。
#: ⚠️ 这是一个**如实登记的残差**，不是一种被处理好的情形：
#: 本线只保证「认最早那条链路」是确定的，另一张单请人工核对。
DUPLICATE_SOURCE_ORDER_NO_REASON = (
    "本绑定名下有两张我方单的有效来源单号相同（『{value}』）：日志快路只能按链路 id "
    "认最早的那一条，另一张请人工核对来源单号"
)
#: 台账里的绑定已不存在（`payload["member_binding_id"]` 指向的行没了）⇒ 终态，⛔ 不重试。
BINDING_MISSING_REASON = (
    "台账里的绑定已不存在（member_binding_id 指向的行没了），本事件不再重试，请人工核对"
)
#: 未预期异常的台账原因（⛔ 只带异常**类型名**，不带 `str(exc)`）。
UNEXPECTED_EVENT_REASON = "未预期异常（{exc}）：台账 {kind}/{ref} 本轮未完成，按退避重试"
#: 到尝试上限时追加在那句原因后面的后缀。
ABANDONED_SUFFIX = "（已重试 {n} 次，不再自动重试，请人工核对）"
#: 一轮没有任何人话原因可写时的兜底（⛔ 不许写空串，那会看起来像「成功但没清干净」）。
ROUND_UNKNOWN_FAILURE_REASON = "本轮未完成，但没有留下具体原因（请查本地台账的最近几行）"

#: 我方八态里推给下家的三档 → 对方 `sta` 的中文标签（业务方收窄口径，plan §1.2-6；标签出自 `docs/JIDANPU-API.md` 状态表，逐字）。
#: ⛔ 池内两态（`UNASSIGNED`/`WAITING_GRAB`）、`IN_PROGRESS`/`PENDING_REVIEW`/`COMPLETED` 都不在这里：前三者的「代练中」只在来源态例外档推（见 `REVERT_SOURCE_STATUSES`），后两者是「我方状态变了」里不推的档（plan §1.2-8）。
#: ⚠️ 写字面值 ⛔ 不是 `OrderStatus(...).label`：从我方 label 派生等于把「上游契约」绑在「我方文案」上——改我方 label 会静默改掉发给对方的状态。
PUSHABLE_STATUS_LABELS: dict[str, str] = {
    OrderStatus.ISSUE: "问题单",
    OrderStatus.PAUSED: "已暂停",
    OrderStatus.REFUNDED: "已退单",
}
#: 唯一例外的**目标标签**：来源态 ∈ `REVERT_SOURCE_STATUSES` 且目标态 = 代练中 ⇒ 推它
#: （业务方 的「回改」，plan §1.2-7）。
REVERT_PUSH_LABEL = "代练中"
#: 「回改」例外的**来源态**集合（plan §1.2-7）。⚠️ 判定只认**来源态**：来源态是池内两态
#: （未分配 / 待抢单）的「代练中」= 我方指派 / 接单 ⇒ **一律不推**（业务方 口径不变）。
#: 来源态例外是三档：已完成 / 待验收 / 已退单。少一档，客服把那一档改回代练中时下家不跟。
REVERT_SOURCE_STATUSES = frozenset({
    OrderStatus.COMPLETED, OrderStatus.PENDING_REVIEW, OrderStatus.REFUNDED,
})


@dataclass(frozen=True)
class OutboxStats:
    """一轮 `run_outbox` 的计数：`done`=消费成功；`failed`=留下 failed（按退避再试）；`skipped`=收敛成终态 skipped（有意的「不动作」）；`deferred`=没动（退避窗内等待）；`abandoned`=已到尝试上限不再自动重试；`paused`=绑定已停用 ⇒ 本轮一个事件都没动（spec §8.4）。
    `capped`=撞本轮封顶（`JIDANPU_OUTBOX_MAX_PER_ROUND`）⇒ 留到下一轮；`suspended`=熔断挂起（D-0359 D3）：该绑定回读失败达阈值 ⇒ 本轮 launch 不投（行保持 pending、发单意图不丢；每 5 分钟放行一发探测，`_launch_delivery_gate`），统计行「熔断挂起 N」就是它；`cooldown_until`=该店出口冷却截止时刻（`None`=没冷却）。
    `blocked_reason`=本轮压根没跑的人话原因（空串=正常跑了）：①式恒为空串；②式分派层在开关关着时用它说「没跑、为什么」——⛔ 与 `paused`（绑定被停用）分开：两种不同的「没动」。"""

    done: int = 0
    failed: int = 0
    skipped: int = 0
    deferred: int = 0
    abandoned: int = 0
    paused: bool = False
    #: ⭐ 本轮封顶后留给下一轮的条数（`JIDANPU_OUTBOX_MAX_PER_ROUND`，默认 100）。
    capped: int = 0
    #: ⭐ 本轮因 launch 读回熔断挂起而**未投**的条数（行保持 pending；D-0359 D3）。
    suspended: int = 0
    #: ⭐ 出口冷却截止时刻（`None` = 没在冷却）。只在①式出向用。
    cooldown_until: datetime | None = None
    blocked_reason: str = ""


class MemberOutboxUnexpectedFailures(RuntimeError):
    """一个或多个事件抛出未预期异常（逐事件边界：其余事件照跑完再抛）。⚠️ 未预期异常响亮抛（⛔ 不伪装成业务失败）：`failures` 是人话原因清单，`stats` 与正常返回同义 ⇒ 调用方拿计数、把原因记进绑定 `last_error`；
    事件的 `reason` 只带异常类型名（⛔ 不带正文——正文可能含凭据/上游回包）。"""

    def __init__(self, failures: list[str], *, stats: OutboxStats):
        super().__init__("；".join(failures))
        self.failures = list(failures)
        self.stats = stats


# ── 台账 / 链路的小工具（⛔ 全是收窄写入，绝不整行 `save()`）──────────────────────


def _persist_payload(event, payload: dict) -> None:
    """把**进度**写回事件（断点）：录单一成功就要保住「已经录过」这个事实。"""
    JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload=payload)


def _upstream_reason(prefix: str, exc: ApiError) -> str:
    """把上游那句人话接到我们的原因后面（spec §9.1 末段/§13-R7 同一口径）。⛔ 不写只有一句「被拒绝了」的原因：运维在下家系统里没有我们的日志，台账那句 `msg` 是唯一能告诉他「缺权限/缺来源/字段不合法/余额不足」的东西（实测踩过：录单被拒而台账只写「鸡蛋铺拒绝了这次录单」，根因只能回读上游原话才拿得到）。
    ⚠️ 只取业务壳的 `msg`（+业务码）⛔ 不用 `str(exc)`——那带 endpoint 与整包 body，既长又把无关字段写进页面；凭据脱敏由 SDK `transport` 负责（`ApiError.body` 已 `redact`）。"""
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    msg = str(body.get("msg", "") or "").strip().replace("\n", " ")
    code = getattr(exc, "code", None)
    if not msg:
        return prefix
    tail = f"（上游码 {code}）" if code is not None else ""
    return f"{prefix}：{msg[:120]}{tail}"


def _keeps_rearmed_payload(kind, payload):
    return kind == KIND_STATUS_OUT or kind in EDIT_INTENT_KEYS or _is_pic_repush(kind, payload)


def _current_event_result_filter(event):
    current = Q(pk=event.pk)
    if _keeps_rearmed_payload(event.kind, event.payload):
        return current & Q(payload=event.payload)
    if event.kind != KIND_WITHDRAW:
        return current
    rearmed_at = jidanpu_relaunch.withdrawal_intent(event)["rearmed_at"]
    if rearmed_at is None:
        return current & (Q(payload__rearmed_at__isnull=True) | Q(payload__rearmed_at=None))
    return current & Q(payload__rearmed_at=rearmed_at)


def _finish(event, *, status: str, reason: str, now: datetime) -> bool:
    updated = JidanpuSyncEvent.objects.filter(_current_event_result_filter(event)).update(
        status=status, reason=reason, processed_at=now,
    )
    if event.kind == KIND_WITHDRAW and updated:
        _finish_other_withdrawals(event.target_ref, event.pk, now)
    return bool(updated)


def _retry(event, *, reason: str, now: datetime) -> str:
    """预期失败：`failed` + `attempts += 1`（用 `F()` 自增，并发下不互相覆盖）+ 人话原因。⚠️ 到 `OUTBOX_MAX_ATTEMPTS` 时在原因后追加「不再自动重试」——事件仍是 `failed`（留台账待人看）但 `run_outbox` 不再取它（`abandoned`）；
    ⚠️ 同时把 `status`/`reason` 写回内存实例（`_consume_all` 靠它把逐条人话收进 `last_error`，⛔ 不为此多查一次库）。"""
    attempts = (event.attempts or 0) + 1
    text = reason
    if attempts >= OUTBOX_MAX_ATTEMPTS:
        text = f"{reason}{ABANDONED_SUFFIX.format(n=attempts)}"
    text = text[:255]
    updated = JidanpuSyncEvent.objects.filter(_current_event_result_filter(event)).update(
        status="failed", reason=text, processed_at=now, attempts=F("attempts") + 1,
    )
    if event.kind in EDIT_INTENT_KEYS and not updated:
        return "deferred"
    event.status = "failed"
    event.reason = text
    if event.kind == KIND_WITHDRAW and updated and attempts >= OUTBOX_MAX_ATTEMPTS:
        _finish_other_withdrawals(event.target_ref, event.pk, now)
    return "failed"


def _skip(event, *, reason: str, now: datetime) -> str:
    """终态的「不动作」：⛔ 不调上游、⛔ 不计 `attempts`、⛔ 不再重试。"""
    updated = _finish(event, status="skipped", reason=reason[:255], now=now)
    return "deferred" if event.kind in EDIT_INTENT_KEYS and not updated else "skipped"


#: `member.status_out` 事件 payload 里**来源态快照**的键名（写方 `queue_status_out`、
#: 读方 `_status_out_from_status` 共用这一个字面量，⛔ 不两处各写一份）。
STATUS_OUT_FROM_STATUS_KEY = "from_status"


def _status_out_from_status(event) -> str:
    """读取本次同步意图携带的来源态，不从订单当前状态反推。
    缺失、非字符串和带空白的未知取值不放行恢复状态。
    """
    raw = (event.payload or {}).get(STATUS_OUT_FROM_STATUS_KEY)
    if not isinstance(raw, str):
        return ""
    return raw


#: `member.status_out` 事件 payload 里「这次推送是人在页面上点的」键名（写方 `queue_status_out(manual=True)`、读方 `_status_out_manual` 共用）。
#: 缺省/假 ⇒ 自动路径：三档+来源态例外，行为一字不变（计划 §3）；真 ⇒ 额外允许 `待验收`/`已完成` 两档+防倒退（见 `_consume_status_out`）。
STATUS_OUT_MANUAL_KEY = "manual"
# 同一时刻的重复请求也须区分，旧发送结果不能收掉新意图。
STATUS_OUT_INTENT_KEY = "status_intent_id"
EDIT_INTENT_KEYS = {KIND_PRICE: "price_intent_id", KIND_PROJECT: "project_intent_id"}


def _status_out_manual(event) -> bool:
    """这条 `member.status_out` 是不是人在页面上点出来的（缺省/认不出一律 `False`）。⚠️ 判据是 `is True`（严格布尔）⛔ 不用真值：`payload` 是 JSON 列，历史/手造行可能是字符串 `"false"`/数字 `1`，真值判断会把 `"false"` 认成「手动」⇒ 自动路径静默换口径；
    JSON 的 `true` 到 Python 就是 `True` ⇒ `is True` 对正常写方没有损失，方向 fail-closed（=按自动口径处理）。"""
    return (event.payload or {}).get(STATUS_OUT_MANUAL_KEY) is True


def _manual_push_label(status) -> str | None:
    """手动推送额外允许的两档 → 对方 `sta` 中文标签；其余 ⇒ `None`。⚠️ 只有两档（计划 §3）：`待验收`/`已完成`。自动路径不经过本函数（`manual` 假时 `_consume_status_out` 根本不调它）⇒ 自动口径一字不变；
    ⚠️ 「已完成」标签取 `REMOTE_DONE_STATUS`（全仓唯一出处）⛔ 不在这里再写一份字面值。"""
    if status == OrderStatus.PENDING_REVIEW:
        return MANUAL_PENDING_REVIEW_LABEL
    if status == OrderStatus.COMPLETED:
        return REMOTE_DONE_STATUS
    return None


def _binding_of(event):
    """事件归属的绑定；`payload["member_binding_id"]` 不存在/不是 int/行没了 ⇒ `None`。⚠️ 只认 int（裁定 2）：字符串 id 说明写方坏了 ⛔ 不做隐式转换
    （那会把写方缺陷静默修好，而 T3 的换绑收敛按同一个键查、查不到就永远收敛不了）。"""
    raw = (event.payload or {}).get("member_binding_id")
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    return JidanpuMemberBinding.objects.filter(pk=raw).first()


def _link_for(binding, order):
    return JidanpuMemberOrderLink.objects.filter(binding=binding, order=order).first()


def _remote_copy_owned_elsewhere(binding, order, remote_id):
    return JidanpuMemberOrderLink.objects.filter(
        binding__jidanpu_base_url=binding.jidanpu_base_url, jidanpu_order_id=remote_id,
    ).exclude(order_id=order.pk).exists()


def _ensure_link(binding, order, *, remote_id: str, in_hall: bool, now: datetime):
    """Return the existing link or claim an unoccupied copy in this store."""
    link = _link_for(binding, order)
    if link is not None:
        return link, False
    try:
        with transaction.atomic():
            jidanpu_assignment_lock.lock_remote_copy(binding.jidanpu_base_url, remote_id)
            if _remote_copy_owned_elsewhere(binding, order, remote_id):
                return None, False
            link = JidanpuMemberOrderLink.objects.create(
                binding=binding, order=order, jidanpu_order_id=remote_id,
                in_hall=in_hall, listed_at=now if in_hall else None, last_out_at=now,
            )
    except IntegrityError:
        # A same-binding writer may already have saved this order's link.
        return _link_for(binding, order), False
    return link, True


def order_game_name(order) -> str:
    """我方订单上那一款游戏的名字——出向「游戏名称」格的唯一取值口。⭐ 为什么住在这里：这个值有两个消费方、必须逐字同源（⛔ 不许各取一份）：①发出去——②式建单 `CreateOrder`/改单 `ModifyOrder` 的 `gameName`（住在 `jidanpu_member_openapi_outbox._game_name`，它只是本函数一层壳；取值依据/真机证据/「我方游戏名 ≠ 对方游戏名」残差全在那里的 docstring）；②判空兜底——共享引擎 `_consume_project` 发之前判空（与空 `project_text` 同档处置：终态 `skipped`+人话，见 `PROJECT_NO_GAME_NAME_REASON`）。
    取值口径只有一条：`Order.game` 上那个游戏的名字，取不到 ⇒ 空串（⛔ 不猜、不回落常量：编一个游戏名会写进对方那张真单，比失败更坏）。
    ⚠️ 空值唯一可达来源：`Order.game` 是 NOT NULL FK ⇒ `game_id` 恒有值（`game_id is None` 到不了）；可达空值是 `Game.name` 空串/纯空白（列上无非空约束；序列化器与 `peer_inbound.resolve_game` 不收空名 ⇒ 正常路径到不了，属兜底）⇒ 判空必须打这个函数回的文本上（⛔ 不是打在 `game_id` 上：既测不出这一档、也与真正发出去的值不是同一件事）。"""
    return order.game.name if order.game_id else ""


# ── ⭐ launch 读回熔断（按绑定；N2/E-0304）：现场（审计 S-01 生产只读实测）多家下家按店分化「录单后回读」0% 成功 ⇒ launch 烧完 `OUTBOX_MAX_ATTEMPTS` 后那条 `failed` 行把 `queue_member_event` 的在飞去重槽永久占死（对账每 180 秒重排全被挡回）⇒ 整店同步静默失效+每单留一张「已录未抛」孤儿单；⛔「只解除死槽」不成立——死行一放行对账会以每秒约 10 发持续白打已回 `UpstreamBlockedError` 的对端 WAF ⇒ ①去重过滤精确化（见 `queue_member_event`）②按绑定读回熔断（挂起→半开放行一发探测→成功复位/失败续挂）两改动必须同一次落。
# ⚠️ 阈值 20 依据 S-01 实测（坏店 ≈千条/天、正常店 ≈0，坏店几分钟必触发、正常店永远到不了，⛔ 不是拍脑袋常数）；⚠️ 计数来源=台账查询（⛔ 不加模型列、零迁移）：主查询只取该绑定最新 21 条 launch 行（id 倒序走主键索引命中即停+`LAUNCH_READBACK_RECENT_WINDOW` 下界，毫秒级），只有混合窗才退到 24h 计数兜底，健康店 steady state 一次额外查询都没有（闸是懒的：本轮没有待投 launch 行就根本不判，见 D3）。
# ⭐⭐ D-0359 D1-D3（业务方三裁定，必须同一次读）：①闸门从排队侧挪到投递侧（D3 意图不丢）——新事件照常排队（pending），投不投由 `_launch_delivery_gate` 判：挂起期行保持 pending、只计统计「熔断挂起 N」，人工重抛（payload 带 `rearmed_at`）照旧直投；②探测间隔固定 5 分钟（D2，「30 分钟起×2 封顶 6 小时」退役，恢复后最坏 5 分钟自动恢复）；③首次判定挂起 `logger.warning` 一行（journald `grep 熔断` 可查）、续挂不打（⛔ 不许每 10 秒刷一条）、探测放行打一条 info；台账行保留（人话/审计用）。
KIND_LAUNCH_BREAKER_OPEN = "member.launch_breaker_open"
#: 回读失败**条数**阈值（按**事件行**数，不是按 attempts 次数）：该绑定的 launch 行里
#: 「failed 且原因属回读失败族」的行达到这个数 ⇒ 挂起。
LAUNCH_READBACK_BREAKER_THRESHOLD = 20
#: ⭐ D2：探测间隔 = **固定 5 分钟**（旧「30 分钟起 ×2、封顶 6 小时」退役，
#: 见上面 D1-D3 那段的 ②）。挂起期每满 5 分钟放行**一发**探测，回读成功即自动恢复。
LAUNCH_READBACK_PROBE_INTERVAL = timedelta(minutes=5)
#: 「回读失败族」的原因前缀判据（读码定准 ⛔ 不是猜文案）：launch 消费路径三跳纯读上游查询失败——①录单前认领预读（`_existing_remote_order` → `fetch_order_by_key1`）②录单后取远端单号（`_readback_remote_id` → 同上，S-01 三家坏店卡死的就是这一跳）③抛单前远端状态读（`_member_throw_prepare` → 同上）。三跳失败都走 `_retry`（`failed`+原因前缀在最前，后面才接上游原话/「已重试 N 次」后缀）⇒ 前缀匹配稳定。
#: ⚠️ 刻意不收 `THROW_CONFIRM_REASON`（抛单后回读确认）：那一跳混合了「写成功但读到的不是 Y」的形态（对端主动撤下等），不是纯回读失败。
#: ⛔ 也刻意不收②式（`jidanpu_member_openapi_outbox` 的同名原因文案不同、且 S-01 三家店全是①式）——②式若出现同病属后续扩面，先如实登记边界。
LAUNCH_READBACK_FAILURE_REASONS: tuple[str, ...] = (
    RECORD_PRE_READ_REASON,
    RECORD_READBACK_REASON,
    THROW_PREPARE_UNREADABLE_REASON,
)
#: 判定主查询（「最新 21 条 launch 行」）的 `created_at` 下界：2 天内的 20 条失败才够格
#: 挂起（⚠️ 族内失败两档都够不到阈值的店**两档都不管 ⇒ 永不挂起**——真盲区的机制与量级
#: 见 `_launch_readback_breaker_state` 第 3 档的登记，已改准）。
LAUNCH_READBACK_RECENT_WINDOW = timedelta(days=2)
#: 兜底计数窗：混合窗时改数「24 小时内的回读失败行」。
LAUNCH_READBACK_COUNT_WINDOW = timedelta(hours=24)
#: 熔断台账行的查询界：探测固定 5 分钟 ⇒ 持续挂起的店里最新一条熔断台账必然 ≤5 分钟老（每次放行探测都会写一条新的）；12 小时远宽于所需，
#: 超过它的行对闸门判定不可见=「上一段历史」——正好让「恢复后隔了很久又坏」的店按全新一轮重新判定（⛔ 不接续旧账）。
LAUNCH_BREAKER_NOTE_WINDOW = timedelta(hours=12)
#: 首次挂起那句人话（`{store}`=店标识、`{detail}`=判定细节）。⚠️ 点名店名与恢复条件（计划原文要求）；`reason` 列 255 宽，整句要控制在里面。
#: ⭐ D3 改准：挂起发生在投递侧，新事件照常排队（意图不丢）⇒ 人话必须说「排队暂缓投递、不会丢」，⛔ 不许再写成「不排队」。
LAUNCH_BREAKER_OPEN_REASON = (
    "对端回读持续失败，这家店（{store}）的新发单已排队暂缓投递：{detail}。"
    "排队的新单不会丢，系统每 5 分钟自动放行一发探测，"
    "回读成功即自动恢复；人工重抛不受此限"
)
#: 放行探测那句人话（`{store}` = 店标识）。⭐ D2：「第 N 发」计数与「加倍间隔」随
#: window 机制一起退役——台账行本身保留（人话 / 审计用），probe 布尔区分行种。
LAUNCH_BREAKER_PROBE_REASON = (
    "挂起期满，放行一发探测（店 {store}）：这发回读成功即恢复自动发单，失败则继续挂起"
)
#: ⭐ D-0361 挂起期挡单的台账 kind：被投递侧闸挡下的每一单落一条留痕（E-0312 病灶「被挡的单零台账」的收口）。⛔ 不在 `OUTBOX_KINDS` 里 ⇒ `run_outbox` 永不取它（照 `KIND_LAUNCH_BREAKER_OPEN` 同一条纪律）；
#: ⛔ 本行只是「这一单这一刻被挡下了」的痕迹，闸的判定与拦截行为零改动（照挡不误，只是留痕）。
KIND_LAUNCH_BLOCKED = "member.launch_blocked"
#: 挡单那句人话（`{store}` = 店标识）：照 `LAUNCH_BREAKER_OPEN_REASON` 的风格点名
#: 店铺与恢复条件；`reason` 列 255 宽，整句要控制在里面。
LAUNCH_BLOCKED_REASON = (
    "对端回读持续失败，这家店（{store}）的这一单发单已排队暂缓投递："
    "不会丢，系统每 5 分钟自动放行一发探测，回读成功即自动恢复；人工重抛不受此限"
)

# ── ⭐ D-0362：「逐字查无」终态化的两条台账 kind ─────────────────────
#: 首遇逐字查无 ⇒ 自动新建（每（订单，绑定）终身上限 1 次）那一次的终态行：既是计划 §一.1 的「台账留终态行」，也是「这一（订单，绑定）已经自动新建过」的唯一判据（防循环保护，见 `_auto_recreate_already_used`）。
#: ⛔ 不在 `OUTBOX_KINDS` 里 ⇒ `run_outbox` 永不取它（照 `KIND_LAUNCH_BREAKER_OPEN` 同一条纪律）。
KIND_NO_ORDER_RECREATED = "member.no_order_recreated"
#: 已自动新建过、回读**再遇**逐字查无 ⇒ 停手**转人工**的终态行（时间线可见）；
#: 同时是对账（`reconcile`）不再给这一族重排 `member.launch` 的**查询键**
#: （窗口见 `NO_ORDER_HANDOFF_NOTE_WINDOW`）。⛔ 同样不在 `OUTBOX_KINDS` 里。
KIND_NO_ORDER_HANDOFF = "member.no_order_handoff"
#: 转人工终态行的人话（D-0362 的台账原话；⚠️ 字面值，测试逐字钉）。
NO_ORDER_HANDOFF_REASON = "已自动新建仍查无，转人工处置"
#: 首遇查无自动新建那一次终态行的人话（计划 §一.1 原话「对端明确查无此单，按拍板转新建」
#: + 上限说明）。
NO_ORDER_RECREATED_REASON = "对端明确查无此单，按拍板转新建（自动新建，每单限一次）"
#: 挂起期（D-0359 D3 投递侧熔断挂起，D-0361 同一把闸）遇逐字查无的处置人话：
#: **挂起照旧拦截、⛔ 挂起期不放行新建**；终态化照记（事件行以本句收敛 `skipped`，
#: ⛔ 刻意**不**以回读失败族那几句开头——skip 行本就不进熔断燃料，别让前缀撞族）。
NO_ORDER_SUSPENDED_REASON = (
    "对端明确查无此单，但这家店的发单仍在挂起期（回读故障未复位）："
    "按挂起口径拦截、不自动新建；挂起解除后下一轮按查无新口径处置"
)
#: 转人工终态行的判重/对账查询窗（形状照 `LAUNCH_BREAKER_NOTE_WINDOW` 那族）：窗内恰一条（防灌台账——对账每轮都停在转人工，不逐轮灌行）。
#: 窗口过期后对账会再排一条 launch ⇒ 消费侧重新校验远端（prepare 真读）：人工已把卡补回来 ⇒ prepare 读到卡走既有判定腿自愈（⛔ 不落转人工行）；仍是逐字查无 ⇒ 重写新窗行 ⇒ 稳态=每窗每（单，绑定）恰一条 launch 行+恰一条转人工行，有界。
NO_ORDER_HANDOFF_NOTE_WINDOW = timedelta(hours=24)


def _breaker_store_label(binding) -> str:
    """熔断台账里点名「这家店」的说法：下家账号（空则用地址）。"""
    return binding.account or binding.jidanpu_base_url


def _write_launch_breaker_note(
    binding, *, probe: bool, detail: str = "",
) -> JidanpuSyncEvent:
    """写一条熔断台账（kind=`member.launch_breaker_open`、终态 `skipped`、按绑定指认）。⚠️ 行形状照 `_note_binding_once` 那族：`target_type="binding"`、`target_ref=str(binding.pk)`、`payload.member_binding_id`（留痕认得对人）；`status="skipped"` 记录的是「动作被跳过/挂起」这个事实本身 ⛔ 不能用 `pending`/`failed`（会被健康列与「有 N 条事件已到重试上限」口径误数）。
    ⭐ D2：payload 的 `window` 轮数键随「间隔加倍/6 小时封顶」一起退役（探测改固定 5 分钟，`LAUNCH_READBACK_PROBE_INTERVAL`）；台账行本身保留（人话/审计用）。
    `payload["probe"]` 布尔区分「首次挂起行」与「探测放行行」——探测放行那条同时是下一发探测的时刻锚（`_launch_delivery_gate` 读它）。"""
    if probe:
        reason = LAUNCH_BREAKER_PROBE_REASON.format(store=_breaker_store_label(binding))
    else:
        reason = LAUNCH_BREAKER_OPEN_REASON.format(
            store=_breaker_store_label(binding),
            detail=detail or f"launch 回读失败已达 {LAUNCH_READBACK_BREAKER_THRESHOLD} 次",
        )
    return jidanpu_common.write_event(
        direction="out", kind=KIND_LAUNCH_BREAKER_OPEN, status="skipped",
        target_type="binding", target_ref=str(binding.pk),
        reason=reason[:255],
        payload={
            "member_binding_id": binding.pk,
            "probe": probe,
        },
    )


def _latest_launch_breaker_note(binding, *, now) -> JidanpuSyncEvent | None:
    """该绑定还在闸门视野里的最新一条熔断台账（没有 ⇒ `None`）。⚠️ 只看 `LAUNCH_BREAKER_NOTE_WINDOW` 内的行（探测固定 5 分钟 ⇒ 持续挂起的店里最新一条必然 ≤5 分钟老；更老的行对判定不可见=「隔了很久又坏」按全新一轮重新判定）；
    ⚠️ 过滤带 `status="skipped"`：熔断台账恒为终态 skipped ⇒ 能吃 `(status, created_at)` 索引的区间扫描——健康店一次区间扫描即见底，毫秒级。"""
    return (
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH_BREAKER_OPEN, status="skipped",
            payload__member_binding_id=binding.pk,
            created_at__gte=now - LAUNCH_BREAKER_NOTE_WINDOW,
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _launch_recovered_after(binding, *, after_id: int) -> bool:
    """挂起开始之后，这个绑定有没有一条成功（done）的 launch。判据走 id 下界+id 倒序取第一行：恢复成功的店那条 done 行就在最近几十行里 ⇒ 主键倒序走几十行即停；
    仍坏着的店（全史 done=0，S-01 三家店实测）最多走「台账行距最新一条熔断台账」那一段。"""
    return (
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH,
            payload__member_binding_id=binding.pk, status="done",
            id__gt=after_id,
        )
        .order_by("-id")
        .first()
    ) is not None


def _launch_readback_breaker_state(binding, *, now) -> tuple[bool, str]:
    """这个绑定此刻该不该因回读失败挂起，回 `(是否挂起, 给人话台账用的判定细节)`。判据三档（前两档主路径、毫秒级）：①最新 20 条 launch 行里有 `done` ⇒ 不挂起（精确——「读回成功即清零恢复」，计数不住任何列，就是「最近一次成功之后还有几条回读失败」这个查询）；②最新 20 条全是回读失败族 `failed` ⇒ 挂起（精确）；③混合窗 ⇒ 退到 24h 内回读失败行数兜底（`LAUNCH_READBACK_COUNT_WINDOW`）。
    ⚠️ 第③档近似如实登记（机制为读码推演）：偏严一半——兜底计数不看 done 复位（恢复后旧失败滚出 24h 窗前再坏会被提前挂起，方向=更早探测，安全）；偏松一半（真盲区）——两档只认回读失败族，「已录成孤儿单」的重排队行走录单被拒（`RECORD_FAILED_REASON`，族外：对端按「已存在相同来源订单号」拒绝，S-01 生产台账实锤）⇒ 两档都看不见 ⇒ 族内失败够不到阈值的店（实点名 qianmu）永不挂起、循环重排有界但持续到对端修好或人工处置（⛔ 不是「个位数」，扩面拍板材料必须带这个量级；定性：低危有界、换掉「静默死」且对端修好后自愈，交换划算）。"""
    rows = list(
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH,
            payload__member_binding_id=binding.pk,
            created_at__gte=now - LAUNCH_READBACK_RECENT_WINDOW,
        )
        .order_by("-id")
        .values("status", "reason")[: LAUNCH_READBACK_BREAKER_THRESHOLD + 1]
    )
    newest = rows[: LAUNCH_READBACK_BREAKER_THRESHOLD]
    if len(newest) < LAUNCH_READBACK_BREAKER_THRESHOLD:
        # 2 天内的 launch 行不足阈值条 ⇒ 失败行更不够 ⇒ 必然不挂起（省一次兜底查询）。
        return False, ""
    if any(row["status"] == "done" for row in newest):
        return False, ""
    family = tuple(LAUNCH_READBACK_FAILURE_REASONS)
    failed = [
        row for row in newest
        if row["status"] == "failed" and (row["reason"] or "").startswith(family)
    ]
    if len(failed) == LAUNCH_READBACK_BREAKER_THRESHOLD:
        return True, f"最近 {LAUNCH_READBACK_BREAKER_THRESHOLD} 条 launch 全部回读失败"
    family_q = Q()
    for prefix in family:
        family_q |= Q(reason__startswith=prefix)
    count = (
        JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH,
            payload__member_binding_id=binding.pk,
            status="failed",
            created_at__gte=now - LAUNCH_READBACK_COUNT_WINDOW,
        )
        .filter(family_q)
        .count()
    )
    if count >= LAUNCH_READBACK_BREAKER_THRESHOLD:
        return True, f"{int(LAUNCH_READBACK_COUNT_WINDOW.total_seconds() // 3600)} 小时内 launch 回读失败 {count} 次"
    return False, ""


def readback_breaker_health(binding, *, now) -> tuple[bool, str]:
    """熔断态的只读出口（D-0360 订单审计线；⛔ 零行为改动、不写任何行）。⛔ 判定完全复用既有函数（`_latest_launch_breaker_note` + `_launch_readback_breaker_state`），本函数只按两级查询钉死调用顺序：先看「12h 窗内有没有熔断 note」（健康店一次区间扫即见底），仅窗内有 note 的绑定才算完整 state——健康绑定连第二跳都不走；回值形状照 `_launch_readback_breaker_state`。
    ⭐ 一句直白口径：健康条显示的是闸门视野内的挂起（以 12h 窗内有没有熔断 note 行为准），不是逐时刻真值——窗外历史挂起与从未落 note 的边缘态都不在这里显示。"""
    if _latest_launch_breaker_note(binding, now=now) is None:
        return False, ""
    return _launch_readback_breaker_state(binding, now=now)


def _note_launch_blocked_once(event, binding, *, now) -> None:
    """⭐ D-0361：挂起期被挡的单落一条挡单台账（每 (单，绑定) 12h 恰一条）。判重=已有同 kind、同单（`target_ref`）、同绑定（`payload.member_binding_id`）、`created_at` 在 `LAUNCH_BREAKER_NOTE_WINDOW` 内的行 ⇒ 不再写——E-0304「反复被挡不灌台账」同族口径（12h 窗 ⇒ 每单每天 ≤2 行）；
    行形状照 `_write_launch_breaker_note` 那族：终态 `skipped`（⛔ 不用 pending/failed——会被健康列与重试上限口径误数）、`target_type="order"`（时间线 jdp 节按 (target_type, target_ref) 精确收它，走 `jdp_event_target_idx`）。
    ⚠️ 本函数只加行：`_launch_delivery_gate` 的判定与拦截零改动（照挡不误，只是让挡单在时间线上可见）。"""
    already_noted = JidanpuSyncEvent.objects.filter(
        direction="out", kind=KIND_LAUNCH_BLOCKED,
        target_type="order", target_ref=event.target_ref,
        payload__member_binding_id=binding.pk,
        created_at__gte=now - LAUNCH_BREAKER_NOTE_WINDOW,
    ).exists()
    if already_noted:
        return
    jidanpu_common.write_event(
        direction="out", kind=KIND_LAUNCH_BLOCKED, status="skipped",
        target_type="order", target_ref=event.target_ref,
        reason=LAUNCH_BLOCKED_REASON.format(
            store=_breaker_store_label(binding)
        )[:255],
        payload={"member_binding_id": binding.pk},
    )


def _note_no_order_recreated(event, binding, remote_order_id) -> None:
    """⭐ D-0362：首遇逐字查无、走自动新建之前落一条终态行（计划 §一.1 的「终态行」）。⚠️ 必须写在拆锚/重录之前（顺序承重）：重录若中断，下一轮按本行判「自动新建上限已用」⇒ 宁可少建（转人工待人）也⛔ 不冒「重复建卡」的险——「每（订单，绑定）自动新建上界 1 张」的数学上界靠这个写入顺序成立。
    行形状照 `_note_launch_blocked_once` 那族：终态 `skipped`（⛔ 不用 pending/failed）、`target_type="order"`（走 `jdp_event_target_idx`）、payload 带绑定 id 与被查的旧远端单号（排障时「查的是哪张卡」是唯一现场）。"""
    jidanpu_common.write_event(
        direction="out", kind=KIND_NO_ORDER_RECREATED, status="skipped",
        target_type="order", target_ref=event.target_ref,
        reason=NO_ORDER_RECREATED_REASON[:255],
        payload={
            "member_binding_id": binding.pk,
            "jidanpu_order_id": remote_order_id,
        },
    )


def _auto_recreate_already_used(event, binding) -> bool:
    """「这一（订单，绑定）已经自动新建过吗」——D-0362 防循环保护的唯一判据。按 `KIND_NO_ORDER_RECREATED` 终态行终身存在判（⛔ 不设窗口：拍板口径是「每（订单，绑定）自动新建上限 1 次」，终身计）；
    查询形状照 `_note_launch_blocked_once` 的判重查询（`jdp_event_target_idx` 命中单行即停 + kind/payload 过滤，毫秒级）；只在「逐字查无」那一支被调（罕见路径），不给正常消费加查询。"""
    return JidanpuSyncEvent.objects.filter(
        direction="out", kind=KIND_NO_ORDER_RECREATED,
        target_type="order", target_ref=event.target_ref,
        payload__member_binding_id=binding.pk,
    ).exists()


def _note_no_order_handoff_once(event, binding, remote_order_id, *, now) -> None:
    """⭐ D-0362：已自动新建过、再遇逐字查无 ⇒ 落「转人工」终态行（每窗恰一条）。判重=已有同 kind、同单、同绑定、`created_at` 在 `NO_ORDER_HANDOFF_NOTE_WINDOW` 内的行 ⇒ 不再写——E-0304 同族口径（对账每窗至多把这一族送回来一次，见 `reconcile` 的 `handoff_refs`）；
    行形状照 `_note_no_order_recreated`（同一族：终态 skipped / target_type="order" / payload 带绑定）。"""
    already_noted = JidanpuSyncEvent.objects.filter(
        direction="out", kind=KIND_NO_ORDER_HANDOFF,
        target_type="order", target_ref=event.target_ref,
        payload__member_binding_id=binding.pk,
        created_at__gte=now - NO_ORDER_HANDOFF_NOTE_WINDOW,
    ).exists()
    if already_noted:
        return
    jidanpu_common.write_event(
        direction="out", kind=KIND_NO_ORDER_HANDOFF, status="skipped",
        target_type="order", target_ref=event.target_ref,
        reason=NO_ORDER_HANDOFF_REASON[:255],
        payload={
            "member_binding_id": binding.pk,
            "jidanpu_order_id": remote_order_id,
        },
    )


def _launch_delivery_gate(binding, *, now, held: int = 0) -> tuple[bool, bool]:
    """⭐ D-0359 D3：launch 的投递侧熔断闸——这一轮投不投。只该被 `_consume_all` 在「本轮有到点待投的 launch 行」时懒调用（每轮至多一次；本轮没有待投 launch 就根本不查，健康店一次台账区间扫描即见底）。回 `(suspended, probe_release)`：`(False,False)`=照常投；`(True,False)`=挂起（本轮 launch 全部跳过、行保持 pending=意图不丢，调用方计统计「熔断挂起 N」）；`(False,True)`=挂起期满放行一发探测（迭代序第一条待投行，其余照旧挂起）。
    判定顺序（与队列闸时代逐字同构，锚从「新行诞生」换成「行投不投」）：①没有台账行 ⇒ 按 `_launch_readback_breaker_state` 判，该挂 ⇒ 写首次挂起台账行+D1 warning，挂起；②最新台账行之后有 done 的 launch ⇒ 恢复检查（探测/挂起期新行/人工重抛成功时 done 就落台账，「读回成功即恢复」不许再等满间隔；挂起前已在飞的老行由第④步状态查询兜、恢复延迟 ≤ 一个探测间隔）⇒ 清零开闸 / 重新攒满按全新一轮重挂；③挂起未满固定 5 分钟（D2）⇒ 静默续挂（⛔ 不打日志——每 10 秒一拍的调度都打就是刷屏，「熔断挂起 N」承担可见性）；④期满 ⇒ 先补一次状态查询（D3 关键：挂起前已排队、id 早于台账行的行 `id__gt` 锚看不见 ⇒ 探测 done 进「最新 20 条」=第①档直接开闸）：已恢复 ⇒ 开闸（整批放行 ⛔ 不许一格一格放）；仍坏 ⇒ 写探测放行台账行（同时是下一发探测的时刻锚）+一条 info，放行一发。
    探测代表=行 id 最小者（事件按 `created_at, id` 排序对排队早晚一致）：确定性/幂等最好证、FIFO 公平、与旧「放行只此一发」同构（放行写新台账行、时刻锚前移，同轮后面的单照样挂起）。⚠️ 人工重抛（payload 带 `rearmed_at`）根本不进这道闸（人工旁路，照旧直投）。⚠️ 挂起期「行保持 pending」≠永久保额度：探测代表每 5 分钟烧 1 个 attempt，超长挂起下队列按「每行 ~15-25 分钟」逐张走完放弃额度落 abandoned（与全仓「不无限重试」一致）；熔断解除靠人工重抛/对账补录恢复，D1 日志承担让人尽早解锁。⚠️ 两个执行者同时踩到期满最多多放一发（串行闸 `_outbox_guard` 已并队，实际到不了），方向有界、如实登记不自证完美。"""
    note = _latest_launch_breaker_note(binding, now=now)
    if note is None:
        blocked, detail = _launch_readback_breaker_state(binding, now=now)
        if not blocked:
            return False, False
        _write_launch_breaker_note(binding, probe=False, detail=detail)
        logger.warning(
            "熔断挂起：店（%s）的 launch 投递暂停 %d 条（%s）；每 %d 分钟放行一发探测，"
            "回读成功即自动恢复，人工重抛不受此限",
            _breaker_store_label(binding), held,
            detail, int(LAUNCH_READBACK_PROBE_INTERVAL.total_seconds() // 60),
        )
        return True, False
    if _launch_recovered_after(binding, after_id=note.pk):
        blocked, detail = _launch_readback_breaker_state(binding, now=now)
        if not blocked:
            return False, False
        _write_launch_breaker_note(binding, probe=False, detail=detail)
        logger.warning(
            "熔断挂起：店（%s）恢复后再次攒满失败阈值，重新暂停 launch 投递 %d 条（%s）；"
            "每 %d 分钟放行一发探测，回读成功即自动恢复",
            _breaker_store_label(binding), held,
            detail, int(LAUNCH_READBACK_PROBE_INTERVAL.total_seconds() // 60),
        )
        return True, False
    if now < note.created_at + LAUNCH_READBACK_PROBE_INTERVAL:
        # 静默续挂：可见性由统计行的「熔断挂起 N」承担（⛔ 不打日志刷屏，D1）。
        return True, False
    # 期满：先补一刀状态查询再放探测。⭐ D3 的关键修正：挂起之前就已排队的行（id 早于台账行）由探测/人工旁路送出去并成功时，`_launch_recovered_after` 的 `id__gt` 锚看不见它们
    # ⇒ 恢复判定在这里按状态查询兜住——探测 done 出现在「最新 20 条」里=判定第①档直接开闸，恢复延迟 ≤ 一个探测间隔（D2 的承诺）。
    blocked, detail = _launch_readback_breaker_state(binding, now=now)
    if not blocked:
        return False, False
    _write_launch_breaker_note(binding, probe=True)
    logger.info(
        "熔断探测：店（%s）挂起期满，放行一发探测（回读成功即自动恢复）",
        _breaker_store_label(binding),
    )
    return False, True


# ── 触发点与消费者之间的接口 ──────────────────────────────────────────


@dataclass
class ReconcileLookupStats:
    lookups: int = 0
    hits: int = 0
    elapsed_ns: int = 0


_reconcile_lookup_stats: ContextVar[ReconcileLookupStats | None] = ContextVar(
    "reconcile_lookup_stats", default=None,
)


@contextmanager
def track_reconcile_lookups():
    stats = ReconcileLookupStats()
    token = _reconcile_lookup_stats.set(stats)
    try:
        yield stats
    finally:
        _reconcile_lookup_stats.reset(token)


def queue_member_event(order, binding, *, kind, status="pending", payload=None, rearm=False):
    """同事务登记出向事件，按绑定和订单合并在飞任务。
    人工重试可重开用尽额度的任务。已关联原单的新一轮重抛独立排队。
    """
    lock_intent = status == "pending" and rearm and (
        kind == KIND_WITHDRAW or _keeps_rearmed_payload(kind, payload)
    )
    with transaction.atomic() if lock_intent else nullcontext():
        return _queue_member_event(order, binding, kind=kind, status=status, payload=payload, rearm=rearm)


def _full_withdrawal_candidates(rows):
    return rows.filter(Q(payload__hall_only__isnull=True) | ~Q(payload__hall_only=True))


def _rearm_payload(event):
    return {**(event.payload or {}), "rearmed_at": timezone.now().isoformat()}


def _reset_exhausted_event(event) -> bool:
    if (event.attempts or 0) < OUTBOX_MAX_ATTEMPTS:
        return False
    event.attempts = 0
    event.status = "pending"
    event.reason = ""
    event.processed_at = None
    return True


def _queue_member_event(order, binding, *, kind, status, payload, rearm):
    data = dict(payload or {})
    data["member_binding_id"] = binding.pk
    pic_repush = _is_pic_repush(kind, data)
    if pic_repush:
        if PIC_LINK_SNAPSHOT_KEY not in data:
            link = _link_for(binding, order)
            if link is not None:
                data[PIC_LINK_SNAPSHOT_KEY] = _picture_link_snapshot(link)
        if rearm:
            data[PIC_REPUSH_INTENT_KEY] = uuid4().hex
    if kind == KIND_STATUS_OUT and rearm:
        data[STATUS_OUT_INTENT_KEY] = uuid4().hex
    edit_intent_key = EDIT_INTENT_KEYS.get(kind)
    if edit_intent_key and rearm:
        data[edit_intent_key] = uuid4().hex
    if status == "pending":
        withdrawal = None
        if rearm and kind in (KIND_LAUNCH, KIND_WITHDRAW):
            withdrawal = jidanpu_relaunch.latest_withdrawal(order.order_no, binding)
            if kind == KIND_LAUNCH and withdrawal is not None:
                data["after_withdraw"] = jidanpu_relaunch.withdrawal_intent(withdrawal)
        in_flight = JidanpuSyncEvent.objects.filter(
            direction="out", kind=kind, target_ref=order.order_no,
            status__in=("pending", "failed"),
            payload__member_binding_id=binding.pk,
        )
        if kind == KIND_WITHDRAW and withdrawal is not None:
            in_flight = in_flight.filter(pk=withdrawal.pk)
            if rearm and not data.get(WITHDRAW_HALL_ONLY_KEY):
                # 显式撤回须独立排队，已在执行的窄撤单可能按远端状态跳过。
                in_flight = _full_withdrawal_candidates(in_flight)
        if kind == KIND_ASSIGN:
            if pic_repush:
                # 不同副本的补图各自排队，旧截图触发不能覆盖新副本。
                snapshot = data.get(PIC_LINK_SNAPSHOT_KEY)
                picture_copy = (
                    {f"payload__{PIC_LINK_SNAPSHOT_KEY}": snapshot}
                    if snapshot is not None
                    else {f"payload__{PIC_LINK_SNAPSHOT_KEY}__isnull": True}
                )
                in_flight = in_flight.filter(payload__repush=True, **picture_copy)
            else:
                in_flight = in_flight.filter(
                    Q(payload__repush__isnull=True) | ~Q(payload__repush=True),
                )
                approval_id = data.get(PENALTY_RECORD_APPROVAL_KEY)
                if approval_id is not None:
                    in_flight = in_flight.filter(payload__penalty_record_approval_id=approval_id)
        if not rearm:
            # 对账应让新的待办越过已耗尽额度的任务。
            in_flight = in_flight.filter(attempts__lt=OUTBOX_MAX_ATTEMPTS)
        # 写本轮标记前锁旧行，避免把刚完成的旧轮结果带入下一轮。
        if rearm and (kind == KIND_WITHDRAW or _keeps_rearmed_payload(kind, data)):
            in_flight = in_flight.select_for_update()
        lookup = in_flight.order_by("-id" if withdrawal is not None else "id")
        stats = _reconcile_lookup_stats.get()
        if stats is None:
            existing = lookup.first()
        else:
            started = time.monotonic_ns()
            stats.lookups += 1
            try:
                existing = lookup.first()
            finally:
                stats.elapsed_ns += time.monotonic_ns() - started
            if existing is not None:
                stats.hits += 1
        if existing is not None:
            if (
                kind == KIND_LAUNCH
                and withdrawal is not None
                and jidanpu_relaunch.superseded_by_withdrawal(existing, withdrawal)
                and JidanpuMemberOrderLink.objects.filter(binding=binding, order=order).exists()
            ):
                # 链路已能指认原单。新一轮独立排队，旧任务在途的写回也只能落到旧行。
                now = timezone.now()
                data["rearmed_at"] = now.isoformat()
                with transaction.atomic():
                    reason = "已有新一轮撤回和抛单，本次旧抛单作废"
                    if existing.reason:
                        reason = f"{reason}。上次结果：{existing.reason}"
                    JidanpuSyncEvent.objects.filter(
                        pk=existing.pk, status__in=("pending", "failed"),
                    ).update(status="skipped", reason=reason[:255], processed_at=now)
                    return jidanpu_common.write_event(
                        direction="out", kind=kind, status=status, target_type="order",
                        target_ref=order.order_no, payload=data,
                    )
            if rearm and _reset_exhausted_event(existing):
                # 复位成「没试过」：`processed_at` 一起清掉（`_backoff_elapsed` 用
                # `processed_at or created_at` 当锚 ⇒ 不清它，退避窗会按旧锚算）。
                JidanpuSyncEvent.objects.filter(pk=existing.pk).update(
                    attempts=0, status="pending", reason="", processed_at=None,
                )
            if rearm:
                # 记录本次请求；状态意图刷新，其他任务的录单进度保留。
                rearmed_payload = _rearm_payload(existing)
                if kind == KIND_STATUS_OUT:
                    rearmed_payload[STATUS_OUT_INTENT_KEY] = data[STATUS_OUT_INTENT_KEY]
                    for field in (STATUS_OUT_FROM_STATUS_KEY, STATUS_OUT_MANUAL_KEY):
                        rearmed_payload.pop(field, None)
                        if field in data:
                            rearmed_payload[field] = data[field]
                if edit_intent_key:
                    rearmed_payload[edit_intent_key] = data[edit_intent_key]
                if pic_repush:
                    rearmed_payload.update(data)
                if kind == KIND_LAUNCH:
                    rearmed_payload.pop("after_withdraw", None)
                if "after_withdraw" in data:
                    rearmed_payload["after_withdraw"] = data["after_withdraw"]
                _persist_payload(existing, rearmed_payload)
                existing.payload = rearmed_payload
            return existing
        if not rearm:
            # ⭐ D-0370：自动路径建新行才拍状态指纹（⛔ 不在去重命中的路上白拍：指纹要两次小查询）。
            #    先过终态闸；放行 ⇒ 指纹写进新行 payload 给下一轮的终态闸比对。人工路径不拍（重抛复用旧行，指纹由原排队时刻承载）。
            guard_fp = _requeue_guard_fingerprint(order, binding)
            blocked = _terminal_requeue_guard(order, binding, kind, guard_fp)
            if blocked is not None:
                return blocked
            data[REQUEUE_GUARD_FP_KEY] = guard_fp
        # ⭐ D-0359 D3：launch 新行**照常排队**——熔断挂起挪到了投递侧
        #    （`_consume_all` 的 `_launch_delivery_gate`），排队侧不再挡（意图不丢）。
    if kind == KIND_WITHDRAW and not rearm and status == "pending":
        return _queue_reconciled_withdrawal(order, binding, data)
    return jidanpu_common.write_event(
        direction="out", kind=kind, status=status, target_type="order",
        target_ref=order.order_no, payload=data,
    )


# ══ 触发点（spec §5.3 的六行 + §5.2 的「绑定那一刻的补推」）══════════════════════
# 各入口判据全部收在这里 ⛔ 不散在视图里；每个入口只在调用方的同一个事务里写台账（调用点都在业务写入的 `transaction.atomic()` 块内），⛔ 一次上游都不调（上游由 `run_outbox` 在事务外做）。⭐ `queue_project_update`（第八入口）与 `queue_percent_reprice`（第九入口，D-0326/E-0301，范围只收窄到这一份绑定）都**自己不判「值有没有变」**——判据住在调用点（前者两个调用点同一口径：`order_writes.update_order_fields` 的 `changed` 清单与 `peer_inbound.apply_events` 接手支的旧值快照；后者唯一调用点=绑定 PUT 的旧值快照），见各自 docstring。
# ⚠️ 「谁该推这张单」只有一份判据：`owners_of` 走 T4 的 `dedupe_owner`/`expected_shape`，同地址只留 `id` 最小的一份（spec §5.4 第 2 行）——⛔ 不另写一份「算不算他的单」；⭐ 「这一行有没有推单资格」也只有一份判据：`_only_push_targets`（业务方 B 档：同店有启用中的①式时②式永不推单），三个选择点（`owners_of`/`_assign_target_of`/`_push_evidence_ids`）与对账候选都只经过它——⛔ 不许在任何调用点另判一次 `mode`。


def enabled_bindings():
    """全部启用中的绑定（⭐ ①式（账号密码）在前、②式（官方 API）在后；同一类按 id 升序）。⚠️ 调度也用它（`jidanpu_sync` 逐绑定遍历）：停用的绑定一条路都不该跑（spec §8.4）⇒ ⛔ 别在调度里另写一份「哪些绑定要跑」的查询。
    ⭐ 线上热修（某租户真机：新单 4 分钟还没推过去）：②式那一轮的回调消费会逐条打上游接口（几十秒到分钟级），排在它后面的①式 outbox（推单）只能等下一轮 ⇒ 调度顺序改成「推单那条线先跑」，让新单在一个轮次内就发出去；
    去重语义不变（同地址仍取最早的那一条），本函数只改遍历顺序 ⛔ 不改谁该跑。"""
    from django.db.models import Case, IntegerField, Value, When

    return list(
        JidanpuMemberBinding.objects.filter(enabled=True)
        .annotate(
            _line_priority=Case(
                When(mode="password", then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("_line_priority", "id")
    )


#: 凭据三列（下家地址 / 账号 / 密码）：`credentials_missing` 的唯一判据来源。
_CREDENTIAL_FIELDS = ("jidanpu_base_url", "account", "password")


def credentials_missing(binding) -> bool:
    """这一行的凭据还没配齐（地址/账号/密码任一为空）⇒ `True`。⚠️ 与「PUT 启用前的那道门」（`api/jidanpu_member_views.py::_credentials_missing`）是同一条事实——那一处已委托本函数（⛔ 两处判据必然漂移，`AGENTS.md` §5-E）；
    ⚠️ 调度用它决定「整行跳过还是跑」：凭据不全时发起一轮只会在上游认证处每轮失败，把「还没配好」说成「同步坏了」。⛔ 判据是 `strip()` 后的空串不是真值（E-0021）。"""
    return not all(
        (getattr(binding, field) or "").strip() for field in _CREDENTIAL_FIELDS
    )


def _enabled_binding_of(taker_id):
    """这个人**启用中**的那一份绑定（一人一份；没有 / 停用 ⇒ `None`）。"""
    if taker_id is None:
        return None
    return JidanpuMemberBinding.objects.filter(user_id=taker_id, enabled=True).first()


def _downstream_addresses_by_user(taker_ids) -> dict[int, set[str]]:
    """一批用户各自的「他自己家的店」地址集合（`downstream_addresses_of_taker` 的批量形态）。存在的唯一理由是查询次数：一轮对账要过全部单，逐单调一次会让每轮多 N 次查询；
    判据仍只有一处（本函数），下面那个一元的只是它的包装。"""
    ids = {pk for pk in taker_ids if pk is not None}
    if not ids:
        return {}
    result: dict[int, set[str]] = {}
    for user_id, base_url in JidanpuMemberBinding.objects.filter(
        user_id__in=ids
    ).values_list("user_id", "jidanpu_base_url"):
        # ⚠️ 空白地址**不是**地址：⛔ 拿空串与空串配对（那会把两个「还没配地址」的绑定判成同店）。
        if base_url:
            result.setdefault(user_id, set()).add(base_url)
    return result


def downstream_addresses_of_taker(taker_id) -> set[str]:
    """这个用户名下所有绑定的下家地址集合（⛔ 不筛 `enabled`）；空/查不到 ⇒ 空集。⭐⭐ 生产事故的判据：同一家下家店挂两条绑定（①式管理员启用真正在推 + ②式下家系统账号停用），客服指派给②式那个账号 ⇒ 旧判据只认同一个绑定 ⇒ 判成「我方别的打手接走了」⇒ 排 `member.withdraw` 把对方正在做的活单退了（下家日志「订单已被发单方打手接取」）。
    ⇒ 判据从「同一个绑定」改成「同一家店」：这个用户名下任何绑定指向过的地址都算「他自己家的店」。⚠️ 查不到/`taker_id` 为空 ⇒ 空集——⛔ 不是「所有地址」（那会把规则放宽成「永不退单」，spec §5.1 第三行对外人照旧成立）；
    ⚠️ 比较用原样字符串（与 `_candidate_bindings`/地址级去重口径逐字一致，⛔ 不自创归一化——那会让「同店」与「同地址去重」两处判据漂）。"""
    if taker_id is None:
        return set()
    return _downstream_addresses_by_user({taker_id}).get(taker_id, set())


def _bindings_at(addresses) -> list:
    """这些下家地址上的**全部**绑定（含**停用**的）；空集 ⇒ 空列表（⛔ 不查库）。"""
    if not addresses:
        return []
    return list(
        JidanpuMemberBinding.objects.filter(
            jidanpu_base_url__in=sorted(addresses)
        ).order_by("id")
    )


def _push_targets_for_shop(rows) -> list:
    """同店有启用中的账号式绑定时，推单只选账号式绑定。"""
    from apps.business import jidanpu_openapi

    password_rows = [row for row in rows if not jidanpu_openapi.is_openapi_binding(row)]
    if any(row.enabled for row in password_rows):
        return password_rows
    return rows


def _only_push_targets(bindings) -> list:
    """批量读取同店全部绑定，按推单归属筛选并保留候选顺序。"""
    rows = list(bindings)
    addresses = {row.jidanpu_base_url for row in rows if row.jidanpu_base_url}
    shops = defaultdict(list)
    for row in _bindings_at(addresses):
        shops[row.jidanpu_base_url].append(row)
    allowed = {row.pk for shop in shops.values() for row in _push_targets_for_shop(shop)}
    return [row for row in rows if row.pk in allowed]


def _assign_target_of(taker_id):
    """「只录单」（`member.assign`）该排到哪一份绑定上；没有可排的 ⇒ `None`。①接手人自己有启用中的绑定且它是推单目标 ⇒ 就是它（既有行为逐字不变）；②他那份停用/不是推单目标（同店还有启用中的①式）、但同一家店还有别的启用绑定 ⇒ 排到那一条上（生产事故，理由见 `downstream_addresses_of_taker` 与 `_only_push_targets`）；③都没有 ⇒ `None`（那类单由「启用那一刻的补推」的 `to_record` 兜，⛔ 不静默）。
    ⛔ 不许排到②里那条停用的，也⛔ 不许排到②式（官方 API）那条上（它永不推单，见 `_only_push_targets`）：停用的绑定不跑对账、消费者也不该替它发上游（spec §8.4）。
    ⚠️ 同店多条启用时取 `id` 最小那条（=最早建的），与 `dedupe_owner`「同地址只推最早那一份」同一口径。"""
    binding = _enabled_binding_of(taker_id)
    if binding is not None and _only_push_targets([binding]):
        return binding
    addresses = downstream_addresses_of_taker(taker_id)
    if not addresses:
        return None
    rows = JidanpuMemberBinding.objects.filter(
        jidanpu_base_url__in=sorted(addresses), enabled=True
    ).order_by("id")
    for row in _only_push_targets(rows):
        return row
    return None


def owners_of(order, *, bindings=None):
    """这张单该由哪些绑定推：不同地址各推一份，同地址只推最早的那一份。⚠️ `bindings` 是候选（给了就用给的，没给就取全部启用中的）；「是不是他的单」判据在 `dedupe_owner`/`expected_shape`（唯一出处），本函数只负责按地址分组；
    ⚠️ 同一份下家系统（同 `jidanpu_base_url`）里两个代练员的游戏范围重叠时只推一次（spec §5.4 第 2 行）；不同地址是两套互不感知的下家系统，各推一份；
    ⭐ 推单归属拆开（业务方 B 档）：分组之后先过 `_only_push_targets`——同一地址上有启用中的①式那条时，②式那条⛔ 不参与去重、永不成为推单目标（它只收回调）。判据只有那一处 ⛔ 本函数不重写。"""
    pools: dict[str, list] = {}
    for row in (enabled_bindings() if bindings is None else list(bindings)):
        pools.setdefault(row.jidanpu_base_url, []).append(row)
    owners = []
    for pool in pools.values():
        owner = dedupe_owner(order, _only_push_targets(pool))
        if owner is not None:
            owners.append(owner)
    return owners


def _launch_evidence(order_nos):
    return JidanpuSyncEvent.objects.filter(
        direction="out", kind=KIND_LAUNCH, target_type="order", target_ref__in=order_nos,
    ).order_by()


def _full_withdraw_targets(orders):
    order_ids = {order.order_no: order.pk for order in orders}
    evidence = defaultdict(set)
    # 先看抛单，再看链路，任务在两次读取之间完成也能指认副本。
    for order_no, binding_id in _launch_evidence(order_ids).filter(
        status__in=("pending", "failed", "done"),
    ).values_list("target_ref", "payload__member_binding_id"):
        if type(binding_id) is int:
            evidence[order_ids[order_no]].add(binding_id)
    for order_id, binding_id in JidanpuMemberOrderLink.objects.filter(
        order_id__in=order_ids.values(),
    ).values_list("order_id", "binding_id"):
        evidence[order_id].add(binding_id)
    binding_ids = {binding_id for ids in evidence.values() for binding_id in ids}
    allowed = {binding.pk: binding for binding in _only_push_targets(_bindings_of(binding_ids))}
    return {
        order_id: [allowed[pk] for pk in sorted(ids & allowed.keys())]
        for order_id, ids in evidence.items()
    }


def _push_evidence_ids(order, *, hall_only=False) -> set[int]:
    """找出已有远端副本或仍可能推出副本的绑定。"""
    if not hall_only:
        return {binding.pk for binding in _full_withdraw_targets([order]).get(order.pk, ())}

    launches = _launch_evidence([order.order_no]).filter(status__in=("pending", "failed"))
    ids = {
        value for value in launches.filter(attempts__lt=OUTBOX_MAX_ATTEMPTS)
        .values_list("payload__member_binding_id", flat=True)
        if type(value) is int
    }
    progress = {
        value for value in launches.filter(
            Q(payload__recorded=True) | Q(payload__jidanpu_order_id__isnull=False)
        ).values_list("payload__member_binding_id", flat=True)
        if type(value) is int
    }
    links = JidanpuMemberOrderLink.objects.filter(order=order).filter(
        Q(in_hall=True, withdrawn_at__isnull=True) | Q(binding_id__in=progress),
    )
    # 已经投递的历史副本归原绑定清理，按当前推单资格过滤会漏掉它。
    return set(links.values_list("binding_id", flat=True)) | ids


def _bindings_of(ids):
    """按 id 取绑定（含**停用**的：退单是已推出去的东西的清理，见 `queue_withdraw`）。"""
    if not ids:
        return []
    return list(JidanpuMemberBinding.objects.filter(pk__in=ids).order_by("id"))


def hall_launch_targets(order, *, exclude_addresses=()):
    owners = owners_of(order, bindings=[row for row in enabled_bindings() if row.hall_sync_enabled])
    # ⭐ D-0325 硬闸：来源上家地址一份都不推（⬆ 见 docstring；按原样字符串比对）。
    if exclude_addresses:
        owners = [
            binding for binding in owners
            if binding.jidanpu_base_url not in set(exclude_addresses)
        ]
    return owners


def queue_hall_launch(order, *, exclude_addresses: frozenset[str] | set[str] | tuple[str, ...] = ()) -> int:
    """触发点①（spec §5.3 第 1 行）：抛单进我方大厅 ⇒ 给每份归属绑定排 `member.launch`。⚠️ 只发给启用中的绑定（与既有那条线 `queue_launch` 闸门口径一致）：停用期间不推，启用那一刻由 `reconcile_and_queue` 一次性补齐（spec §5.2/A4）；⭐ D-0299：「同步大厅订单」关掉（`hall_sync_enabled=False`）的那份绑定不参与抛大厅——在「谁会收到 launch」这一层就滤掉（归属判定照旧走 `owners_of` ⛔ 不改去重语义；写台账之前滤 ⛔ 不是排出去再靠消费者跳过——那会在台账里留噪声）；只闸这一档：assign/status/withdraw/unlist/price/pics 一条不读它；已挂对方大厅的存量单不主动撤回（D-0299 明文）。
    ⭐ 推单归属拆开：`owners_of` 已经过 `_only_push_targets`——同店有启用中的①式时②式不会出现在这里（它只收回调）。
    ⭐ `exclude_addresses`（D-0325 硬闸）：绝不推回这张单的来源上家地址——上家镜像卡派生的隐藏单（`upstream_hall_shadow`）传 `{mirror.upstream_base_url}`：同一家店既是上家又是下家（租户 S-chuyin 现场）时不排除就立刻回环。⛔ 判据是原样字符串地址（与 `owners_of` 按地址分组同源，⛔ 不自创归一化）；缺省空集=行为逐字不变（既有调用点一个字节都不动）；地址比较在这一处（写台账之前）⛔ 不靠拉取侧 `_loop_throw_keys` 兜底（那是兜底不是闸门）。⚠️ 去重由 `queue_member_event` 负责（同 kind+同单+同绑定在飞只留一行）。"""
    if order.penalty_approval_id is not None:
        return 0
    owners = hall_launch_targets(order, exclude_addresses=exclude_addresses)
    for binding in owners:
        # ⚠️ `rearm=True`：这是**人工点出来的**「抛单」⇒ 烧完额度的旧行要能再试一次
        #    （E-0153 的同族；周期对账那一侧 ⛔ 不传，见 `queue_member_event`）。
        queue_member_event(order, binding, kind=KIND_LAUNCH, rearm=True)
    return len(owners)


def _queue_unlist_if_in_hall(order, binding) -> int:
    """我方接手这张单之后那张单还挂在对方大厅吗 ⇒ 是就排一条撤下（⛔ 不退单）。⭐ 业务方：我方指派/打手在我方大厅自接 ⇒ 对方那张单要停在「未分配」。
    ⚠️ 判据只有一条：本地链路标着 `in_hall`（=真的录到过对方、而且本地认为它还在大厅）。没有链路（还没消费过 `member.launch`）时排不出来，那一档由每 180 秒对账（`to_unlist`）补上——⛔ 不去猜「是不是马上要抛出去」；
    ⚠️ 本地 `in_hall` 可能过期（对方打手早接走了）⇒ 消费者先读一遍确认还在大厅才写（见 `_consume_unlist`）；⚠️ `rearm=True`：指派/自接都是人的动作。"""
    link = _link_for(binding, order)
    if link is None or not link.in_hall:
        return 0
    queue_member_event(order, binding, kind=KIND_UNLIST, rearm=True)
    return 1


def queue_hall_return(order, *, from_status=None) -> int:
    """⭐ 回池后重新进大厅（目标态=`WAITING_GRAB`）⇒ 先退旧副本、再重新录单+抛厅。业务方「回池后恢复同步」：单从打手手里退回池内、又抛回待抢单大厅时，下家那张旧副本必须先退掉（`member.withdraw`——它是「已经推出去的东西」），然后按大厅状态重新同步过去（`member.launch`=重新录单+抛单）。⛔ 不能只退不建。
    ⚠️ 判据只有目标态（`order.status == WAITING_GRAB`，⛔ 不在这里重写状态机/D2 闸）：由唯一写入口 `apply_transition` 的漏斗调用 ⇒ 任何进厅路径（`/throw-to-hall/`、录单即抛、通用 `/transition/`）都走同一条口径；⛔ 不靠 180 秒对账兜底。⚠️ `from_status` 非空且等于目标=自环（不是一次进厅）⇒ 一行都不排。
    ⚠️ 顺序承重：两条事件在同一事务里按「先退后建」写入，消费按 `(created_at, id)` ⇒ 下家不会先收到一张大厅卡、再收到退单。两条门槛都不在这里复制：「没推出去过 ⇒ 零行」由 `queue_withdraw` 自判、停用/范围外/大厅开关由 `queue_hall_launch` 自判（判据各只有一处）。⚠️ 幂等：`/throw-to-hall/` 端点自己也会调一次 `queue_hall_launch`，在飞同键由 `queue_member_event` 归并 ⇒ 只留一行。"""
    if str(order.status) != str(OrderStatus.WAITING_GRAB):
        return 0
    if from_status is not None and str(from_status) == str(OrderStatus.WAITING_GRAB):
        return 0
    return queue_withdraw(order) + queue_hall_launch(order)


def queue_assign(order, *, taker_id) -> int:
    """触发点②③（spec §5.3 第 2、3 行）：客服指派给他/他自己接了 ⇒ 只录单。⚠️ 排到哪一份绑定由 `_assign_target_of` 一处回答：优先他自己那份启用中的；他那份停用、而同一家店还有别的启用绑定 ⇒ 排到那一条上（生产事故：接手人是下家那边的系统账号，真正在推单的是同一家店的管理员那条）；都没有时一行都不写——那类单由「启用那一刻的补推」的 `to_record` 兜（否则会静默永不进场）。
    ⭐ 同一次还要看那张单是不是还挂在对方大厅：是 ⇒ 排一条 `member.unlist`（撤下 ⛔ 不退单），让它在对方那边停在「未分配」（业务方）。
    返回值只算 `member.assign` 那条（与既有调用方「排了几条 assign」的语义一致）。"""
    if order.penalty_approval_id is not None:
        return _queue_assigned_penalty_record(order, taker_id=taker_id)
    binding = _assign_target_of(taker_id)
    if binding is None:
        return 0
    # ⚠️ `rearm=True`：指派 / 他自己接手都是**人的动作**（见 `queue_member_event`）。
    queue_member_event(order, binding, kind=KIND_ASSIGN, rearm=True)
    _queue_unlist_if_in_hall(order, binding)
    return 1


def _is_completed_penalty_order(order):
    return order.status == OrderStatus.COMPLETED and order.penalty_approval_id is not None


def queue_manual_penalty_record(order, *, binding):
    """Allow the authorized personal request to retry an assigned penalty, including completed orders."""
    if order.penalty_approval_id is None or order.status not in (
        *PENALTY_RECORD_ASSIGNED_STATUSES, OrderStatus.COMPLETED,
    ):
        return None
    return _queue_penalty_record(order, binding)


def _queue_penalty_record(order, binding):
    return queue_member_event(order, binding, kind=KIND_ASSIGN, rearm=True, payload={
        PENALTY_RECORD_APPROVAL_KEY: order.penalty_approval_id,
        PENALTY_RECORD_SOURCE_KEY: order.source_order_no,
    })


def _queue_assigned_penalty_record(order, *, taker_id):
    if order.status not in PENALTY_RECORD_ASSIGNED_STATUSES:
        return 0
    binding = _enabled_binding_of(taker_id)
    if binding is None or not _penalty_order_still_current(order, binding, allow_completed=False):
        return 0
    _queue_penalty_record(order, binding)
    return 1


def queue_claimed(order, *, taker_id, suppress_outbound_sync: bool = False) -> int:
    """触发点③④（spec §5.3 第 2、3 行）：抢单成功后按「谁接了」分流——接单人就是某个绑定的主人 ⇒ 给他排 `member.assign`（只录单）；别的打手接了 ⇒ 给「已经/即将录到对方系统」的那些绑定各排一条 `member.withdraw`（spec §5.1 第三行：对方不该再有这张单）。⚠️ 两档在同一份绑定上互斥（接单人自己那份被排除）；⛔ 不是「给所有启用中的绑定发退单」：从没推出去过的单无单可退（发出去只会得到一条永远 skip 的噪声行），判据是 `_push_evidence_ids`。
    ⭐ `suppress_outbound_sync`（D-0227/E-0236：镜像接手只清冗余副本、不回声）：`True` ⇒ 跳过接手人自己绑定的 `member.assign` 与 `_queue_unlist_if_in_hall`（那一半是回声：镜像接手=对方已接走 ⇒ 我方记账，对方副本按定义不该被动），保留「其他绑定排 `member.withdraw`」那一半（那不是回声，是冗余副本清理——别的下家那份按地址去重语义是没人接的挂单，撤它是对的，抑制不许扩大化）。⚠️ 抑制时返回值不含被跳过的 assign 那一条（`taken` 仍排除接手人自己 ⇒ 不会反过来给他补一条 withdraw）；缺省 `False` ⇒ 既有调用点行为一字不变。
    ⭐⭐ `taken`：按「同一家店」排除 ⛔ 不是「同一个绑定」（生产事故）：旧实现只排除接手人自己那份启用绑定 ⇒ 他那份停用、同店另一份启用绑定真正推单时 ⇒ `taken` 空集 ⇒ 给那家店的绑定排了退单、把对方正在做的活单退了。现在排除的是同地址的全部绑定（任何人的、含停用）：接手人是这家店的主人 ⇒ 整店不撤（spec §5.1 第二行）；接手人没有任何绑定（外人）⇒ 地址集空 ⇒ 照旧退单（⛔ 别放宽成永不退单）。"""
    taker_binding = _enabled_binding_of(taker_id)
    count = 0
    if order.penalty_approval_id is not None and not suppress_outbound_sync:
        count = _queue_assigned_penalty_record(order, taker_id=taker_id)
    elif taker_binding is not None and not suppress_outbound_sync:
        # ⭐ 我方接手（他自己在我方大厅接的）⇒ 对方那张单要停在「未分配」（业务方）。
        #    ⭐⭐ D-0227：镜像接手（`suppress_outbound_sync=True`）这两条整档跳过——②式没有大厅监视 ⇒ 本地 `in_hall` 恒 True、永不过期，
        #    照排就会把对方已接走的单推成已退单（E-0236 现场：发版窗口 11 次自动接手、每次同秒 unlist-done）。
        queue_member_event(order, taker_binding, kind=KIND_ASSIGN, rearm=True)
        _queue_unlist_if_in_hall(order, taker_binding)
        count += 1
    taken = {row.pk for row in _bindings_at(downstream_addresses_of_taker(taker_id))}
    count += _queue_other_withdrawals(order, _bindings_of(_push_evidence_ids(order) - taken))
    return count


def queue_pic_repush_for_ready_screenshots(order) -> int:
    """⭐ E-0282/B2：到图触发补推——该单的 ready 截图（订单截图+群码，`MEMBER_PUSH_IMG_TYPES`「全传」口径）新到了 ⇒ 给每一条满足以下几格的链路排一条 `KIND_ASSIGN`（消费侧 A 段走 per-pic 判缺，只把缺的那几张传到下家那张单上）：①绑定 `enabled=True` 且①式（登录式）——②式的传图是二期步骤 6、未接线 ⇒ ⛔ 不排队、也⛔ 不写 `PUSH_PIC_NOT_WIRED` 噪声行（判据唯一出处 `jidanpu_openapi.is_openapi_binding`，`mode` 是唯一判据）；②链路已存在（没录到过对方的单没有「下家那张单」可传图，那一档由指派/对账先录单）；③链路不在对方大厅（`in_hall=False`）——还挂在下家大厅的副本不排补推：补推事件复用 `member.assign`，而大厅档的补图口径在 E-0282 笔记里挂着待拍板，一张迟到截图把人家大厅里的单撤下没有任何业务规则背书 ⇒ 源头就不排（消费侧另有护栏双保险，见 `_consume_assign`）；④链路没退成过（`withdrawn_at` 为空）——已退成的链路不该每张新图再排一条补推烧额度（对方系统里那张单已经不在了）；⑤本单确有 `ready` 的截图（没有 ⇒ 一次链路查询都不花、即刻返回，照 `queue_complete` 的「无候选即廉价返回」口径）。
    ⚠️ `rearm=True`：到图触发是边沿触发（每张新图至多排一次），但烧完额度的 assign 行会永久挡住补推（E-0153 同族静默空转）⇒ 必须复位额度；在飞（`pending`/`failed`）同键由 `queue_member_event` 自动归并 ⇒ 连到两张图也只排一条。
    ⚠️ 幂等与兜底：`store_remote_pics` 那条触发链刻意不在事务里（M4）⇒ 「图已落库、补推事件没排上」的窗口由 `repush_member_pics` 命令兜底（台账判据可查）；⚠️ 位置纪律：调用方把本函数放在业务写入的同一个事务里（图行 ready 与补推行同生共死），做不到的调用链（如 `store_remote_pics`）在它自己的注释里如实登记。"""
    # 函数内 import（环，照 `_push_screenshots_of_line` 那处的同一处置）：
    # `jidanpu_upstream_pics` 在模块级 import `jidanpu_upstream`，而后者又 import 本模块；
    # `jidanpu_openapi` 同样在模块级 import `jidanpu_upstream` ⇒ 两者都只能在函数内 import。
    from apps.business import jidanpu_openapi, jidanpu_upstream_pics

    # ⑤ 先判「有没有图」（绝大多数单没有 ⇒ 一次查询就返回，不碰链路表）。
    if not jidanpu_upstream_pics.choose_screenshots(order, img_types=MEMBER_PUSH_IMG_TYPES):
        return 0
    queued = 0
    links = JidanpuMemberOrderLink.objects.filter(
        order=order, binding__enabled=True,
        # ⭐ 还挂在对方大厅（in_hall）与已退成
        #    （withdrawn_at 非空）的链路都**不排**——理由见 docstring 第 ③④ 格。
        in_hall=False, withdrawn_at__isnull=True,
    ).select_related("binding")
    for link in links:
        if jidanpu_openapi.is_openapi_binding(link.binding):
            continue  # ②式传图二期未接线 ⇒ 不排队、不写噪声行（见 docstring 第 ① 格）
        queue_member_event(
            order, link.binding, kind=KIND_ASSIGN, rearm=True,
            payload={**PIC_RE_PUSH_PAYLOAD, PIC_LINK_SNAPSHOT_KEY: _picture_link_snapshot(link)},
        )
        queued += 1
    return queued


WITHDRAW_SUMMARY_KIND = "member.withdraw_summary"

WITHDRAW_ALREADY_CLEARED_REASON = "下家订单已退单，无需重复撤下"
WITHDRAW_OUTSIDE_HALL_REASON = "已确认下家订单未在大厅，无需撤下"
WITHDRAW_SUMMARY_REPLACED_REASON = "已进入新的下家撤单轮次"
WITHDRAW_COMMAND_ACCEPTED_KEY = "withdraw_command_accepted"


def _has_withdrawal_evidence(event, key):
    return event.payload.get(key) == jidanpu_relaunch.withdrawal_intent(event)


def _remember_withdrawal_evidence(event, key):
    if _has_withdrawal_evidence(event, key):
        return
    payload = {**event.payload, key: jidanpu_relaunch.withdrawal_intent(event)}
    if JidanpuSyncEvent.objects.filter(_current_event_result_filter(event)).update(payload=payload):
        event.payload = payload


def _queue_reconciled_withdrawal(order, binding, payload):
    """对账补排沿用同一挂单和同一退单意图。"""
    with transaction.atomic():
        previous = jidanpu_relaunch.latest_withdrawal(order.order_no, binding)
        summaries = []
        if previous is not None and previous.status == "failed":
            previous = JidanpuSyncEvent.objects.select_for_update().get(pk=previous.pk)
            latest = jidanpu_relaunch.latest_withdrawal(order.order_no, binding)
            if latest.pk != previous.pk and latest.status in {"pending", "failed"} and latest.attempts < OUTBOX_MAX_ATTEMPTS:
                return latest
            if latest.pk == previous.pk and previous.attempts >= OUTBOX_MAX_ATTEMPTS:
                summaries = _previous_withdrawal_summaries(order, binding, previous)
        if summaries and previous.payload.get(WITHDRAW_HALL_ONLY_KEY):
            if order.deleted_at or order.status in (OrderStatus.REFUNDED, OrderStatus.UNASSIGNED):
                summaries = []
            else:
                # 对账补排仍只撤大厅挂单，不扩大成撤真实做单。
                payload = {**payload, WITHDRAW_HALL_ONLY_KEY: True}
        event = jidanpu_common.write_event(
            direction="out", kind=KIND_WITHDRAW, status="pending", target_type="order",
            target_ref=order.order_no, payload=payload,
        )
        if summaries and _has_withdrawal_evidence(previous, WITHDRAW_COMMAND_ACCEPTED_KEY):
            _remember_withdrawal_evidence(event, WITHDRAW_COMMAND_ACCEPTED_KEY)
        for summary in summaries:
            _continue_other_withdrawal(summary, previous, event)
        return event


def _previous_withdrawal_summaries(order, binding, previous):
    if binding.jidanpu_base_url in downstream_addresses_of_taker(_main_taker_id(order.pk)):
        return []
    if JidanpuMemberOrderLink.objects.filter(
        binding=binding, order=order, listed_at__gt=jidanpu_relaunch.effective_at(previous),
    ).exists():
        return []
    summaries = JidanpuSyncEvent.objects.select_for_update().filter(
        kind=WITHDRAW_SUMMARY_KIND, status__in=("pending", "failed"), target_ref=order.order_no,
        payload__events__contains=[previous.pk],
    ).order_by("id")
    previous_intent = jidanpu_relaunch.withdrawal_intent(previous)
    return [
        summary for summary in summaries
        if summary.payload["intents"].get(str(previous.pk)) == previous_intent
    ]


def _continue_other_withdrawal(summary, previous, event):
    new_ids = sorted(event.pk if pk == previous.pk else pk for pk in summary.payload["events"])
    new_intents = dict(summary.payload["intents"])
    del new_intents[str(previous.pk)]
    new_intents[str(event.pk)] = jidanpu_relaunch.withdrawal_intent(event)
    _persist_payload(summary, {**summary.payload, "events": new_ids, "intents": new_intents})
    _finish(summary, status="pending", reason="", now=timezone.now())


def _queue_other_withdrawals(order, bindings, *, payload=None):
    """同一次接手的多店待办链接到一条汇总台账。"""
    with transaction.atomic():
        event_ids = sorted({
            queue_member_event(order, binding, kind=KIND_WITHDRAW, payload=payload, rearm=True).pk
            for binding in bindings
        })
        if not event_ids:
            return 0
        # 既有在飞任务也可能被本次引用。先锁它们，让完成者一定看到汇总。
        events = JidanpuSyncEvent.objects.select_for_update().filter(pk__in=event_ids).order_by("id")
        intents = {str(event.pk): jidanpu_relaunch.withdrawal_intent(event) for event in events}
        summaries = JidanpuSyncEvent.objects.filter(
            kind=WITHDRAW_SUMMARY_KIND, target_ref=order.order_no, payload__intents=intents,
        )
        if not summaries.exists():
            jidanpu_common.write_event(
                direction="in", kind=WITHDRAW_SUMMARY_KIND, status="pending",
                target_type="order", target_ref=order.order_no,
                payload={
                    "events": event_ids, "intents": intents, "source_order_no": order.source_order_no or "",
                },
            )
            _finish_other_withdrawals(order.order_no, event_ids[0], timezone.now())
        return len(event_ids)


def _finish_other_withdrawals(order_no, event_id, now):
    from apps.business.jidanpu_member_openapi_outbox import ABSENT_MEANS_GONE_REASON

    with transaction.atomic():
        summaries = JidanpuSyncEvent.objects.select_for_update().filter(
            kind=WITHDRAW_SUMMARY_KIND, status__in=("pending", "failed"), target_ref=order_no,
            payload__events__contains=[event_id],
        ).order_by("id")
        for summary in summaries:
            event_ids = summary.payload["events"]
            outcomes = list(JidanpuSyncEvent.objects.filter(pk__in=event_ids))
            intents = {str(event.pk): jidanpu_relaunch.withdrawal_intent(event) for event in outcomes}
            if intents != summary.payload["intents"]:
                _finish(summary, status="skipped", reason=WITHDRAW_SUMMARY_REPLACED_REASON, now=now)
                continue
            if all(
                event.status == "done" or (
                    event.status == "skipped" and (
                        event.reason == WITHDRAW_OUTSIDE_HALL_REASON
                        or (
                            event.reason == ABSENT_MEANS_GONE_REASON
                            and _has_withdrawal_evidence(event, jidanpu_relaunch.WITHDRAW_ABSENCE_CONFIRMED_KEY)
                        )
                    )
                )
                for event in outcomes
            ):
                if any(
                    _has_withdrawal_evidence(event, WITHDRAW_COMMAND_ACCEPTED_KEY) or (event.status == "done" and not event.reason)
                    for event in outcomes
                ):
                    record(
                        log_type=ACTION_LOG_TYPES["order.jidanpu_withdraw"],
                        action="order.jidanpu_withdraw", actor=MACHINE_ACTOR, system=True,
                        target_kind="order", target_id=order_no, order_no=order_no,
                        source_order_no=summary.payload["source_order_no"],
                    )
                    _finish(summary, status="done", reason="", now=now)
                else:
                    _finish(summary, status="skipped", reason="本次没有需要撤下的挂单", now=now)
            elif not any(
                event.status in {"pending", "failed"} and event.attempts < OUTBOX_MAX_ATTEMPTS
                for event in outcomes
            ):
                _finish(summary, status="failed", reason="有下家挂单尚未确认撤下", now=now)


def queue_withdraw(order, *, hall_only=False, summarize=False) -> int:
    """给其他店排退单。指派离厅时只清理大厅副本。
    停用绑定仍记入待办，恢复同步后才能清掉旧副本。
    """
    addresses = downstream_addresses_of_taker(_main_taker_id(order.pk))
    if hall_only:
        targets = _bindings_of(_push_evidence_ids(order, hall_only=True))
    else:
        targets = _full_withdraw_targets([order]).get(order.pk, ())
    bindings = [binding for binding in targets if binding.jidanpu_base_url not in addresses]
    payload = {WITHDRAW_HALL_ONLY_KEY: True} if hall_only else None
    if summarize:
        return _queue_other_withdrawals(order, bindings, payload=payload)
    for binding in bindings:
        queue_member_event(order, binding, kind=KIND_WITHDRAW, payload=payload, rearm=True)
    return len(bindings)


def queue_withdrawals(orders) -> None:
    """对同事务已下架的一批订单登记完整撤回。"""
    targets = _full_withdraw_targets(orders)
    if not any(targets.values()):
        return
    takers = dict(Performance.objects.filter(
        order_id__in=targets, emp_type=EmpType.MAIN,
    ).values_list("order_id", "user_id"))
    addresses = _downstream_addresses_by_user(takers.values())
    destinations = [
        (order.order_no, binding.pk)
        for order in orders
        for binding in targets.get(order.pk, ())
        if binding.jidanpu_base_url not in addresses.get(takers.get(order.pk), ())
    ]
    latest = jidanpu_relaunch.latest_withdrawals(
        {order_no for order_no, _ in destinations},
        {binding_id for _, binding_id in destinations},
    )
    latest_ids = [latest[key].pk for key in destinations if key in latest]
    inflight = _full_withdrawal_candidates(JidanpuSyncEvent.objects.filter(
        pk__in=latest_ids, status__in=("pending", "failed"),
    )).select_for_update().order_by("pk")
    reusable = {row.pk: row for row in inflight}
    fresh, rearmed, reset = [], [], []
    for order_no, binding_id in destinations:
        previous = latest.get((order_no, binding_id))
        existing = reusable.get(previous.pk) if previous is not None else None
        if existing is None:
            fresh.append(JidanpuSyncEvent(
                direction="out", kind=KIND_WITHDRAW, status="pending", target_type="order",
                target_ref=order_no, payload={"member_binding_id": binding_id},
            ))
            continue
        existing.payload = _rearm_payload(existing)
        if _reset_exhausted_event(existing):
            reset.append(existing)
        else:
            rearmed.append(existing)
    JidanpuSyncEvent.objects.bulk_create(fresh, batch_size=250)
    JidanpuSyncEvent.objects.bulk_update(rearmed, ["payload"], batch_size=250)
    JidanpuSyncEvent.objects.bulk_update(
        reset, ["payload", "attempts", "status", "reason", "processed_at"], batch_size=250,
    )


def queue_status_out(order, *, from_status=None, manual=False, binding=None) -> int:
    """按当前链路排状态同步，合并时采用本次来源态与手动标记。
    来源态缺失不沿用旧值；手动推送可按绑定收窄到本人的下家。
    """
    # ⚠️ `str(...)`：来源态可能以 `OrderStatus` 枚举成员传进来（内存态），落台账统一成
    #    与列里一样的**字符串**（消费者比的是 `str(OrderStatus.COMPLETED)` 那一族）。
    data = {STATUS_OUT_FROM_STATUS_KEY: str(from_status)} if from_status is not None else {}
    if manual:
        data[STATUS_OUT_MANUAL_KEY] = True
    links = list(
        JidanpuMemberOrderLink.objects.filter(order=order).select_related("binding")
    )
    if binding is not None:
        # ⚠️ 在内存里筛（⛔ 不再查一次库）：上面的查询已经是「这张单的全部链路」，
        #    多一次查询只会让这条热路径多一条 SQL。
        links = [link for link in links if link.binding_id == binding.pk]
    for link in links:
        # ⚠️ `rearm=True`：状态变更也是**一次真实的事件**（机器驱动的回拉已在写侧抑制）。
        queue_member_event(
            order, link.binding, kind=KIND_STATUS_OUT, payload=data, rearm=True
        )
    return len(links)


def _recording_evidence():
    return (
        Q(payload__recording_source_order_no__isnull=False)
        | Q(payload__recorded=True)
        | Q(payload__jidanpu_order_id__isnull=False)
    )


def _recoverable_recordings(order_no):
    return JidanpuSyncEvent.objects.filter(
        direction="out", kind__in=(KIND_LAUNCH, KIND_ASSIGN), target_type="order",
        target_ref=order_no, status__in=("pending", "failed"),
    ).filter(
        Q(kind=KIND_LAUNCH) | Q(payload__repush__isnull=True) | ~Q(payload__repush=True)
    ).filter(Q(attempts__lt=OUTBOX_MAX_ATTEMPTS) | _recording_evidence())


def _remove_waiting_edits(events, binding):
    """Keep edits waiting for a link out of the network round and request limit."""
    order_no = OuterRef("target_ref")
    return events.alias(
        recording=Exists(_recoverable_recordings(order_no).filter(payload__member_binding_id=binding.pk)),
        linked=Exists(JidanpuMemberOrderLink.objects.filter(
            binding=binding, order__order_no=order_no, order__deleted_at__isnull=True,
        )),
    ).exclude(kind__in=EDIT_INTENT_KEYS, recording=True, linked=False)


def _recording_or_linked_bindings(order):
    # The recorder saves the link before finishing its event. Read the event side first.
    recording = _recoverable_recordings(order.order_no).values_list("payload__member_binding_id", flat=True)
    return _enabled_recording_and_linked_bindings(order, recording=recording)


def _enabled_recording_and_linked_bindings(order, *, recording):
    recording_ids = {pk for pk in recording if type(pk) is int}
    linked = JidanpuMemberOrderLink.objects.filter(order=order, binding_id=OuterRef("pk"))
    return JidanpuMemberBinding.objects.alias(has_link=Exists(linked)).filter(
        Q(pk__in=recording_ids) | Q(has_link=True), enabled=True,
    )


def _queue_member_edit(order, *, kind) -> int:
    bindings = list(_recording_or_linked_bindings(order))
    for binding in bindings:
        queue_member_event(order, binding, kind=kind, rearm=True)
    return len(bindings)


def queue_price_update(order) -> int:
    """Queue the current offer price for linked or still-recording copies."""
    return _queue_member_edit(order, kind=KIND_PRICE)


def queue_project_update(order) -> int:
    """Queue the current project and remark for linked or still-recording copies."""
    return _queue_member_edit(order, kind=KIND_PROJECT)


def queue_source_order_no_update(order) -> int:
    """已录或正在录的单改号后，按绑定单独排队。"""
    recording = JidanpuSyncEvent.objects.filter(
        direction="out", kind__in=(KIND_LAUNCH, KIND_ASSIGN), target_type="order",
        target_ref=order.order_no, status__in=("pending", "failed"), attempts__lt=OUTBOX_MAX_ATTEMPTS,
    ).values_list("payload__member_binding_id", flat=True)
    bindings = _enabled_recording_and_linked_bindings(order, recording=recording)
    count = 0
    for binding in bindings:
        # 旧事件可能已读走旧号。每次变更排独立事件，消费时读订单最新值。
        jidanpu_common.write_event(
            direction="out", kind=KIND_SOURCE_ORDER_NO, status="pending", target_type="order",
            target_ref=order.order_no, payload={"member_binding_id": binding.pk},
        )
        count += 1
    return count


def queue_percent_reprice(binding) -> int:
    """触发点⑨（D-0326/E-0301）：绑定那一格「抛单百分比」（`JidanpuMemberBinding.throw_price_percent`）真的变了 ⇒ 给这一份绑定已有链路、还在对方大厅（`link.in_hall=True`）且本地状态=待抢单的卡排 `member.price`——消费者按当前百分比重抛钉值（`_throw_fee` 在消费那一刻现算，同值幂等）。本函数是纯排队口：⛔ 不读价、⛔ 不打上游、⛔ 不判「值有没有变」。为什么要有这一档（E-0301 现场）：「配置驱动价格」的配置项创建之后还能改——绑定启用那一刻的补齐（`reconcile_and_queue`）把存量单按当时的百分比抛出，之后才改百分比 ⇒ 首批已抛出的卡停在旧价、永不同步（生产实锤：租户 S 绑定 #13 建立那一秒按默认 100% 启用补齐抛出 35 张，40 分钟后才改 85 ⇒ 22 张在厅单的 `recFee` 停在原价，最后靠人工补 `member.price` 事件才追平）。
    判据（与触发点⑦⑧同一条家族纪律，⛔ 调用点之外不许再判一遍）：「值有没有变」不住在这里（拿不到旧值、也不该自己查库——判据住在唯一调用点：绑定 PUT，旧值在 `pending_instance` 改内存之前拍的快照，照触发点⑧的形状「真的变了才排」⛔ 不是「这次请求带了这一格」——页面是整表单提交，没改的那格也会带原值）；范围只收窄到这一份绑定（`filter(binding=binding)`：百分比是每个代练员自己那行上的列，别的绑定链路一张不波及）；两个闸一起判：`in_hall=True`（要补的是已抛出去还在他家大厅那批卡的抛单价 `recFee`，不在厅的卡没有这一格可改）＋本地状态=待抢单（在厅卡的本地形态；已接手/已退单的卡归 `member.withdraw`/`member.status_out` 那些线管，⛔ 不越界替它们做主）；⭐ 零链路即零成本（照 `queue_price_update` 口径：一次查询、空立刻返回 0 ⛔ 不排空转行）；`rearm=True`（改百分比是人的动作，E-0153 同族）。重复/乱序收口照 `queue_price_update`（事件行不存值、在飞行去重 `done` 不挡）。
    如实登记的边界：停用中的绑定也排（拍板口径只有「链路+在厅+待抢单」三闸 ⛔ 未加 enabled）——排出的行由 `run_outbox` 的停用闸（`paused=True`）压住等启用那一拍消费，比改价档「停用期间错过的不会自愈」少一个死角；②式的形态差异：②式只有 `ModifyOrderPrice` 一个改价口（写 `recFee`），纯②式没有「重抛」那一档 ⇒ 消费时由 `PricePrimitives.hall_not_applicable_reason` 落一条人话台账后照旧 `done`，带①式凭据的桥接价档在厅那一半照改（D-0250）——两条路都由既有消费者收口，本函数零改动；换凭据（`rebinding`）的 PUT 不触发（判据在调用点）：那是「换了一整套凭据」，旧凭据名下的在飞事件已被收敛成 `skipped`、存量卡按 spec §8.4 归人工核对——⛔ 不许拿新凭据去改旧单。"""
    links = list(
        JidanpuMemberOrderLink.objects.filter(
            binding=binding,
            in_hall=True,
            order__status=OrderStatus.WAITING_GRAB,
            # ⭐ 软删排除（第五车合并树全量报红后补上，与 `orders_of` 等兄弟读点同一条
            #    口径）：已软删的单不进下游补价——判据由 `tests.test_order_read_path_net`
            #    的 join 登记网当场报出并钉住。
            order__deleted_at__isnull=True,
        ).select_related("order")
    )
    for link in links:
        queue_member_event(link.order, binding, kind=KIND_PRICE, rearm=True)
    return len(links)


@dataclass(frozen=True)
class QueueStats:
    """`reconcile_and_queue` 一排排出去的事件条数（按 kind 分列，⛔ 不只是一个总数）：四个 kind=真的排了的台账事件条数——同 kind+同单+同绑定在飞被去重、同键终态且状态无变化被终态闸（D-0370）挡回的算不进 ⇒ 这是「对账判出的待办数」，可能大于真落的新行数；`skipped`=引擎按 A12 判给**别的绑定**推的单的**人话原因**（⛔ 不是光秃秃的计数、⛔ 也不静默），不逐单落台账（spec §5.4 收口改准），由调用方把「计数+原因」汇总出去（调度每轮打 stdout、PUT 进响应）。"""

    launch: int = 0
    assign: int = 0
    withdraw: int = 0
    unlist: int = 0
    skipped: tuple[str, ...] = ()
    at: datetime | None = None

    @property
    def skipped_count(self) -> int:
        """被跳过的单数（`skipped` 存的是人话原因 ⇒ 计数由它自己推出来）。"""
        return len(self.skipped)


def reconcile_and_queue(binding, *, now, line=None, lock_launch_orders=True) -> QueueStats:
    """按本轮差集排同步事件，定时补抛前加锁复核订单仍在大厅。"""
    from apps.business.order_writes import lock_order_row

    result = reconcile(binding, now=now, line=line)
    launch_count = 0
    for item in result.to_launch:
        if not lock_launch_orders:
            queue_member_event(item.order, binding, kind=KIND_LAUNCH)
            launch_count += 1
            continue
        with transaction.atomic():
            # 指派也持同一行锁。要么指派看到这条补抛，要么在这里丢弃旧快照。
            order = lock_order_row(item.order)
            if order is not None and order.status == OrderStatus.WAITING_GRAB:
                queue_member_event(order, binding, kind=KIND_LAUNCH)
                launch_count += 1
    for item in result.to_record:
        queue_member_event(item.order, binding, kind=KIND_ASSIGN)
    for item in result.to_withdraw:
        queue_member_event(item.order, binding, kind=KIND_WITHDRAW)
    for item in result.to_unlist:
        queue_member_event(item.order, binding, kind=KIND_UNLIST)
    return QueueStats(
        launch=launch_count,
        assign=len(result.to_record),
        withdraw=len(result.to_withdraw),
        unlist=len(result.to_unlist),
        skipped=tuple(skip.reason for skip in result.skipped),
        at=result.at,
    )


def consume_event(event, *, client=None, http=None) -> str:
    """消费一条事件，回 `"done"`/`"failed"`/`"skipped"`（三态与事件行一致）。⚠️ `client`/`http` 是可测性注入口（照 `detect_binding` 的形状）：给了就用、没给就自己 `jidanpu.member_session(binding)` 建连 ⛔ 不是业务参数。⚠️ 为什么 `http` 要一路带下去（补 A 段传图时加的，形状照上家线 `jidanpu_upstream.consume_event`）：传图第一步要读下家上传页的隐藏域（`sid`/`gameAccId`/`taskId` 的唯一出处），那一跳是裸 HTTP（页面是 HTML 不是业务壳）⇒ 必须拿到 `open_client_with_http` 那一对里的 HTTP 层 ⛔ 不在消费路径里另建一条连（一轮一次连）；
    ⚠️ 结构性守卫：只消费 `pending`/`failed`（纵深防御 ⛔ 不依赖调用方记得筛）；`done`/`skipped` 是终态，直接原样返回、一行不写。⚠️ 未预期异常不在这里翻译（响亮抛）；预期失败走 `_retry`（failed + attempts+1）。"""
    if event.status not in ("pending", "failed"):
        return event.status
    binding = _binding_of(event)
    now = timezone.now()
    if binding is None:
        return _skip(event, reason=BINDING_MISSING_REASON, now=now)
    if client is None:
        with jidanpu.member_session(binding) as (sdk, real_http):
            return _consume(
                event, binding=binding, client=sdk, http=http or real_http, now=now
            )
    return _consume(event, binding=binding, client=client, http=http, now=now)


def _consume(event, *, binding, client, http, now, line=None) -> str:
    order_id = Order.all_objects.filter(order_no=event.target_ref).values_list("pk", flat=True).first()
    with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=order_id or event.target_ref) as busy:
        if busy:
            return "deferred"
        if not JidanpuMemberBinding.objects.filter(pk=binding.pk, enabled=True).exists():
            return "deferred"
        if not _binding_is_current(binding):
            return _skip(event, reason="鸡蛋铺绑定已变化，本次旧同步作废，请人工核对旧店订单。", now=now)
        if _keeps_rearmed_payload(event.kind, event.payload):
            try:
                event.refresh_from_db()
            except JidanpuSyncEvent.DoesNotExist:
                return "skipped"
            status = event.status
        else:
            status = JidanpuSyncEvent.objects.filter(pk=event.pk).values_list("status", flat=True).first()
        if status not in ("pending", "failed"):
            return status or "skipped"
        return _consume_locked(event, binding=binding, client=client, http=http, now=now, line=line)


def _consume_locked(event, *, binding, client, http, now, line=None) -> str:
    """这一个事件该谁办：还没通的 kind ⇒ 终态跳过+人话；通的 ⇒ 共享 handler。⚠️ 缺省 `line`=①式（`_consume_status_out` 自己也是这么取的）⇒ ①式那条路（含所有既有调用点与用例）行为逐字不变（它的 `unsupported` 是空表）；⚠️ 「还没通」与「不认识的 kind」是两件事 ⛔ 别合并：前者是这一条线的阶段进度（登记在 `line.unsupported`，有明确人话，按 `skipped` 收口），后者是写方/接线坏了（既没有 handler 也没登记原因）⇒ 响亮抛；
    ⭐⭐ D-0249（修 E-0246）：`member.unlist` 在声明了「没有不毁单的撤下原语」的线上，分派处就终态跳过、一次上游都不打（`line.unlist_unavailable_reason` 非空 ⇒ `_skip`）。判据只有这一格（对账那侧的收口读同一格，取数口 `_unlist_unavailable_reason` ⛔ 别写第二份）；⭐ 拦截排在 handler 解析之前——⛔ 不许先收敛/先取 handler 再决定跳过（那样会有半路打上游的形态）。"""
    line = line or member_outbox_line()
    if event.target_type == "order":
        order = Order.objects.filter(order_no=event.target_ref).first()
        if order is not None:
            if (order.peer_direct_pending or {}).get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM:
                return "deferred"
            if order.status in (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB):
                from apps.business import jidanpu_first_assignment

                if jidanpu_first_assignment._retained_event(order, binding) is not None:
                    return "deferred"
    if event.kind == KIND_UNLIST:
        unlist_reason = _unlist_unavailable_reason(line)
        if unlist_reason is not None:
            return _skip(event, reason=unlist_reason, now=now)
    reason = _unsupported_reason(line, event.kind)
    if reason is not None:
        return _skip(event, reason=reason, now=now)
    handler = _HANDLERS.get(_converged_kind(line, event.kind))
    if handler is None:
        # 未知 kind = 写方 / 接线坏了 ⇒ 响亮抛（`run_outbox` 的逐事件边界会记下来、
        # 继续下一条，最后响亮抛出）。⛔ 不静默吞掉。
        raise ValueError(f"本消费者不认识的台账类型：{event.kind}")
    return handler(event, binding=binding, client=client, http=http, now=now, line=line)


def _launch_withdrawal_gate(event, binding, line, *, now):
    withdrawal = jidanpu_relaunch.latest_withdrawal(event.target_ref, binding)
    if jidanpu_relaunch.superseded_by_withdrawal(event, withdrawal):
        return _skip(event, reason=STALE_LAUNCH_AFTER_WITHDRAW_REASON, now=now)
    if withdrawal is None:
        return None
    if withdrawal.status in ("pending", "failed") and withdrawal.attempts < OUTBOX_MAX_ATTEMPTS:
        return "deferred"
    if jidanpu_relaunch.withdrawal_confirmed(
        withdrawal, absent_reason=_withdraw_of(line).absent_means_gone_reason,
    ):
        return None
    return _skip(event, reason="撤回未确认，本次重抛未执行", now=now)


def _recheck_relaunch(event, binding, line, order, *, now):
    if not jidanpu_relaunch.launch_is_current(event):
        return "deferred"
    order.refresh_from_db(fields=["status", "deleted_at"])
    if order.status != OrderStatus.WAITING_GRAB or order.deleted_at is not None:
        return _skip(event, reason=STALE_LAUNCH_AFTER_WITHDRAW_REASON, now=now)
    return _launch_withdrawal_gate(event, binding, line, now=now)


def _current_taker_row(logs, remote_name):
    if not remote_name:
        return None
    by_uid = {
        uid: item for item in logs if str(item.get("userName") or "").strip() == remote_name
        if (uid := _uid_of(item.get("userId"))) is not None
    }
    return next(iter(by_uid.values())) if len(by_uid) == 1 else None


def _confirmed_assignment_taker(logs, remote_name):
    """鸡蛋铺多人指派用 / 拼接姓名，明细仍按人返回。"""
    exact = [
        item for item in logs if str(item.get("userName") or "").strip() == remote_name
    ]
    if exact or "/" not in remote_name:
        return _current_taker_row(exact, remote_name)
    names = [name.strip() for name in remote_name.split("/")]
    if any(not name for name in names) or len(set(names)) != len(names):
        return None
    rows = [_current_taker_row(logs, name) for name in names]
    uids = {_uid_of(item.get("userId")) for item in rows if item is not None}
    return rows[0] if len(uids) == len(names) else None


def _is_remote_assignment_state(state):
    return map_remote_status(state) in (
        OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW,
    ) or state == REMOTE_DONE_STATUS


def _relaunch_assignment(remote, *, not_before):
    state = _order_sta_token(remote)
    if not _is_remote_assignment_state(state):
        return ""
    assigned_at = _log_moment(remote.get("assTime"))
    if str(remote.get("isThrowOrder")) != "N" or assigned_at is None or assigned_at < not_before:
        return ""
    return str(remote.get("recManName") or "").strip()


def _recover_relaunch_claim(event, *, binding, order, link, client, now):
    """写后回读前已被接走时，用精确单号和新接手时间补记，原接手日志不参与决策。"""
    remote_id = link.jidanpu_order_id
    try:
        remote = jidanpu.fetch_order_by_key1(
            client, key=remote_id, expect_field="orderId", expect_value=remote_id,
        )
        if not remote or str(remote.get("orderId")) != str(remote_id):
            return None
        remote_name = _relaunch_assignment(
            remote, not_before=link.withdrawn_at + timedelta(microseconds=1),
        )
        if not remote_name:
            return None
        logs = jidanpu.fetch_ass_logs(client, remote_order_id=remote_id)
    except ApiError as exc:
        return _retry(event, reason=_upstream_reason(THROW_PREPARE_UNREADABLE_REASON, exc), now=now)
    taker_row = _confirmed_assignment_taker(logs, remote_name)
    if taker_row is None:
        return _retry(event, reason=REMOTE_TAKER_UNREADABLE_REASON, now=now)
    remote_uid = _uid_of(taker_row.get("userId"))
    with transaction.atomic():
        locked_order = Order.objects.select_for_update().filter(pk=order.pk).first()
        current_link = _link_for(binding, order)
        if not jidanpu_relaunch.launch_is_current(event):
            return "deferred"
        if (
            locked_order is None or locked_order.status != OrderStatus.WAITING_GRAB
            or current_link is None or current_link.pk != link.pk
            or current_link.jidanpu_order_id != remote_id
            or current_link.withdrawn_at != link.withdrawn_at
        ):
            return _skip(event, reason=STALE_LAUNCH_AFTER_WITHDRAW_REASON, now=now)
        blocked = _launch_withdrawal_gate(event, binding, member_outbox_line(), now=now)
        if blocked is not None:
            return blocked
        if not jidanpu_relaunch.confirmed_withdrawal_matches_link(event, link):
            return None
        outcome = machine_claim_order(
            binding=binding, order=locked_order, remote_uid=remote_uid, remote_name=remote_name,
            remote_order_id=remote_id, source_note=f"重抛后接手 uid={remote_uid} 显示名={remote_name}",
            suppress_outbound_sync=True,
        )
        if not outcome.claimed:
            return _skip(event, reason="远端已接手，本地订单无法自动接手，请核对", now=now)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            in_hall=False, listed_at=link.withdrawn_at, last_in_at=now, withdrawn_at=None,
        )
        _finish(event, status="done", reason="重抛后已确认并同步远端接手", now=now)
    return "done"


def _old_assignment_clear_cutoff(event, link):
    clear_actions = OperationLog.objects.filter(
        order_no=event.target_ref, action="order.clear_taker", target_kind="user",
        created_at__lte=jidanpu_relaunch.effective_at(event),
    ).exclude(target_id="")
    if link.listed_at is not None:
        clear_actions = clear_actions.filter(created_at__gt=link.listed_at)
    return clear_actions.order_by("-created_at", "-pk").values_list("created_at", flat=True).first()


def _assignment_snapshot(remote):
    return (
        _order_sta_token(remote), str(remote.get("isThrowOrder", "")),
        str(remote.get("recManName") or "").strip(), _log_moment(remote.get("assTime")),
    )


def _clear_old_relaunch_taker(event, *, binding, line, client, order, link, remote, now):
    """清人前确认是本轮要释放的旧接手，清人后回读原单和当前接手记录。"""
    expected = _assignment_snapshot(remote)
    status, hall, taker_name, assigned_at = expected
    cutoff = _old_assignment_clear_cutoff(event, link)
    if not taker_name and cutoff is None:
        return remote, None
    if "recManName" not in remote:
        return None, _retry(event, reason="鸡蛋铺原单回读缺少接手人信息", now=now)
    allowed_statuses = (jidanpu.ORDER_REFUNDED_LABEL, jidanpu.ORDER_UNASSIGNED_LABEL)
    if status not in allowed_statuses or hall != "N" or (
        taker_name and (
            cutoff is None or assigned_at is None or assigned_at + timedelta(seconds=1) > cutoff
        )
    ):
        return None, _skip(event, reason="不能确认是本轮清掉的旧接手人，请核对鸡蛋铺原单", now=now)

    remote_id = link.jidanpu_order_id
    try:
        confirmed = remote
        if taker_name:
            assignments = jidanpu.fetch_ass_logs(client, remote_order_id=remote_id)
            if len(assignments) != 1 or _current_taker_row(assignments, taker_name) is None:
                return None, _skip(event, reason="鸡蛋铺原单不能确认只有原接手人，本次未清人", now=now)
            current = _member_read_remote(client, remote_id)
            if current is None:
                return None, _retry(event, reason="清接手人前未能回读鸡蛋铺原单", now=now)
            if _assignment_snapshot(current) != expected:
                return None, _retry(event, reason="鸡蛋铺原单接手情况已变化，本次未清人", now=now)
            blocked = _recheck_relaunch(event, binding, line, order, now=now)
            if blocked is not None:
                return None, blocked
            latest_link = _link_for(binding, order)
            if (
                latest_link is None or latest_link.jidanpu_order_id != remote_id
                or latest_link.withdrawn_at != link.withdrawn_at
            ):
                return None, _skip(event, reason=STALE_LAUNCH_AFTER_WITHDRAW_REASON, now=now)
            jidanpu.clear_order_taker(client, remote_order_id=remote_id)
            confirmed = _member_read_remote(client, remote_id)
        if (
            confirmed is None or "recManName" not in confirmed
            or str(confirmed.get("recManName") or "").strip()
            or str(confirmed.get("isThrowOrder")) != "N"
            or _order_sta_token(confirmed) not in allowed_statuses
            or jidanpu.fetch_ass_logs(client, remote_order_id=remote_id)
        ):
            return None, _retry(event, reason="鸡蛋铺原单未确认已清除接手人", now=now)
    except ApiError as exc:
        return None, _retry(event, reason=_upstream_reason("鸡蛋铺原单清接手人或回读失败", exc), now=now)
    return confirmed, _recheck_relaunch(event, binding, line, order, now=now)


def _consume_launch(event, *, binding, client, http, now, line=None) -> str:
    """`member.launch`：录单+抛单+写链路行（`in_hall=True`）。⚠️ 可重入/可断点续跑：`payload["jidanpu_order_id"]` 或 `payload["recorded"]` 一旦落下，重试绝不重录（上游不允许重复来源单号，重录会灌真单）；⚠️ 抛单重复推送无害（同一目标态）⇒ 每次都按当前 `order.hall_price` 抛。⭐ 二期步骤 3：上游那几跳收进 `line.record` 这一格（`RecordPrimitives`）——判据（订单在不在/有没有发单价/缺链路怎么办/认领之后要不要补抛/事件落什么终态）仍然只有这一份，两条线的差别只有「怎么录」与「怎么抛」两跳；⚠️ ②式的「抛单」折在录单那一跳里（`recUser`/`assignPrice` 只在录单那一刻可传）⇒ 它那份 `throw_remote` 只判「目标态到了没有」：本轮新建的（`recUser=999999999`）已经在他家大厅 ⇒ `OK`；单早就在对方系统里（认领/断点续跑）⇒ `NOT_AVAILABLE`+一句人话（⛔ 不许谎称 `in_hall=True`：②式没有任何接口能把已有单推进对方大厅）。
    ⭐ D-0318（业务方口径）：已经在我方链路里的单重新抛（`had_link`）时，抛单之前先经 `record.throw_prepare` 读一遍远端此刻的真实状态——已退单 ⇒ 先改回「未分配」再抛（业务方原话：「已退单的状态先改成未分配然后重抛单就行了」）；已在他家大厅 ⇒ 目标态已达成 ⛔ 不重抛（幂等）；不是未分配（对方有人在做的「代练中」等）⇒ ⛔ 不改状态、⛔ 不抛，终态跳过并点名读到的状态（⛔ 不打断对方干活）；未分配 ⇒ 直接抛、零次改状态。首次录单不读（刚录的单必然未分配）⇒ 成本与调用序列不变；判定与「认领」那一支同一个出处（`adopted_verdict` 四档）⇒ ⛔ 不写第二份判据。
    ⭐ D-0359 §11（⭐⭐ D-0362 起三重条件被放宽为单条件，处置闸见 recreate 支注释）：前置回读判「查无此单」时（三重受控条件：人工重抛 payload 带 `rearmed_at` 且晚于撤回完成时刻 + 本地链接退成过 + 第一页逐字查无——判据只在 `_member_throw_prepare`，本函数只按判定分派）⇒ `THROW_PREPARE_VERDICT_RECREATE`：拆掉两个旧锚（payload 进度键+本链接行）后重走 `ensure_remote` 的既有全新录单路，抛的是新卡；重录回来若竟认领到了卡（同来源号僵尸卡等窄竞态），判定接着流进既有四条腿（幂等收口/先改回未分配/终态跳过/直接抛（本支在分派块最前））；其余一切形态（读失败=`ApiError`/卡存在各档）原样保守。⭐⭐ D-0362（业务方拍板）：「逐字查无」升格为单条件终态事实（判据仍在 prepare，本函数只做处置闸）——recreate 支入口四档顺序承重（见该支注释）：①挂起期拦截不新建、终态化照记（D-0361 同一把闸）；②人工重抛 §11 手动路径原样直通不限次；③自动+终身已自动新建过 ⇒ 「转人工」终态行（⛔ 不再新建；对账侧同窗不捞）；④自动+上限未用 ⇒ 先落自动新建终态行（它同时是「上限已用」的终身判据）再走既有建卡路。防循环数学：每（订单，绑定）自动新建上界 1 张+转人工终态行每窗 1 条，循环断根。"""
    line = line or member_outbox_line()
    event.refresh_from_db()
    if event.status not in ("pending", "failed"):
        return event.status
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        # ⚠️ 默认 manager 挡掉的只有软删行（本仓无硬删）⇒ 一张已删的单不该再推上游。
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    if order.penalty_approval_id is not None:
        return _skip(event, reason="罚款单只同步给接手人的鸡蛋铺账号，不抛下家大厅", now=now)
    if order.hall_price is None:
        # ⛔ NULL 不是 0：把没填的抛单价当 0 会把免费单送进真大厅（沿用既有那条线的口径）。
        return _skip(event, reason=jidanpu_common.LAUNCH_NO_PRICE_REASON, now=now)
    payload = event.payload or {}
    after_withdraw = bool(payload.get("after_withdraw"))
    if order.status != OrderStatus.WAITING_GRAB:
        if after_withdraw:
            return _skip(event, reason=STALE_LAUNCH_AFTER_WITHDRAW_REASON, now=now)
        return _skip(event, reason="订单已离开团团通大厅，本次抛单作废", now=now)
    blocked = _launch_withdrawal_gate(event, binding, line, now=now)
    if blocked is not None:
        return blocked
    record = _record_of(line)
    # ⭐ D-0318 的闸：这次消费之前本地已经有链路吗？有 ⇒ 这是「补抛/重抛」（对方那张单早就存在，可能已被我们退成「已退单」）⇒ 抛单前先读一遍远端状态再决定（`throw_prepare`）；
    #    没有 ⇒ 本次是首次录单（刚录出来的单必然停在「未分配」、没进过对方大厅）⇒ ⛔ 不为它多打一次上游回读（一期录单那条路的成本与调用序列一个字都不变）。
    #    ⚠️ 必须在 `ensure_remote` 之前取：它在录单成功那一刻就会补一条链路（`in_hall=False`）⇒ 取晚了连「首次录单」都会被当成「重抛」；⭐ 行对象留着（D-0359 §11）：`throw_prepare` 的放行判据要 `withdrawn_at`——同一次查询把行带上，⛔ 不为此多打一次查询。
    had_link_row = _link_for(binding, order)
    had_link = had_link_row is not None
    if not had_link and _another_recording_owns_order(event, binding, now=now):
        return _retry(event, reason=RECORD_IN_PROGRESS_REASON, now=now)
    remote_id, reason, outcome, adopted = record.ensure_remote(
        event, order, binding, client, now=now, in_hall=True
    )
    if remote_id is None:
        return _skip(event, reason=reason, now=now) if outcome == "skipped" else _retry(
            event, reason=reason, now=now
        )
    #: ⭐ D-0318：这一轮是不是**先把它从「已退单」改回「未分配」**之后才去抛的
    #  ⇒ 抛单再被拒时要给一句**点名「删单重录不可用」**的人话（见下面那一支）。
    un_refunded = False
    prepare_reason = ""
    prepared_remote = None
    verdict = None
    # ⭐ D-0359 §11：走了「全新建卡」那一档时的人话（成功那行的台账落款；其余路径恒空串）。
    recreate_note = ""
    if adopted is not None:
        # ⭐ 认领来的单：按远端此刻的真实状态决定要不要补抛（真机实测那张单已退单）。已退单先改回「未分配」再补抛；被人接走/已流转仍不碰。
        #    ⚠️ 判据在这一条线自己那一格（`adopted_verdict`）：两条线能读到的远端事实不同——①式读得到 `isThrowOrder`（⇒ `already` 那一档），②式的单查只回状态 ⇒ 它永远不会返 `already`（②式从来不谎称「已在他家大厅」）。
        verdict = record.adopted_verdict(adopted)
    elif had_link and record.throw_prepare is not None:
        # 人工重抛可重建已确认查无的原单；旧重试和自动重建仍受同一限额约束。
        rearmed_manual = jidanpu_relaunch.is_manual_relaunch(
            event, after_withdraw=after_withdraw, withdrawn_at=had_link_row.withdrawn_at,
        )
        verdict, prepare_reason, prepared_remote = record.throw_prepare(
            client, order, binding, remote_id,
            rearmed=rearmed_manual,
            withdrawn=had_link_row.withdrawn_at is not None,
        )
        if verdict is None:
            return _retry(event, reason=prepare_reason, now=now)
    if after_withdraw:
        blocked = _recheck_relaunch(event, binding, line, order, now=now)
        if blocked is not None:
            return blocked
    if (
        after_withdraw and prepared_remote is not None and had_link_row is not None
        and had_link_row.jidanpu_order_id == remote_id and had_link_row.withdrawn_at is not None
        and verdict in (ADOPTED_VERDICT_REFUNDED, ADOPTED_VERDICT_NEEDS_THROW)
        and (
            event.attempts > 0 or "recManName" not in prepared_remote
            or str(prepared_remote.get("recManName") or "").strip()
        )
    ):
        cleared, blocked = _clear_old_relaunch_taker(
            event, binding=binding, line=line, client=client, order=order,
            link=had_link_row, remote=prepared_remote, now=now,
        )
        if blocked is not None:
            return blocked
        verdict = record.adopted_verdict(cleared)
    if verdict is not None:
        if verdict == THROW_PREPARE_VERDICT_RECREATE:
            # ⭐⭐ D-0362 处置闸：「逐字查无」的事实判据在 prepare（单条件 ⛔ 本支不写第二份「查无」判据）；这里是「照不照它行动」。顺序承重，四档：①挂起期（D-0359 D3 投递侧熔断挂起；probe 放行/人工旁路的行才会到这）⇒ 挂起照旧拦截、⛔ 挂起期不放行新建（D-0361 共存口径），终态化照记：事件行按 `NO_ORDER_SUSPENDED_REASON` 收敛 `skipped`——skip 不在回读失败族 ⇒ 既不喂燃料也不复位挂起，熔断器语义零改动（口径④）；②人工重抛（判据=`rearmed_manual`）⇒ §11 既有手动路径原样直通、不限次（每次都是人的显式动作）；③自动+终身已自动新建过 ⇒ 落「转人工」终态行（每窗恰一条）并终态跳过——⛔ 不再新建、⛔ 不再重排（对账侧同窗不捞，`reconcile.handoff_refs`）；⭐ 本档的判定打在新鲜的逐字查无上（前置回读已经真读过）⇒ 人工已在对方店里把卡补回来时 prepare 读到卡、流进既有判定腿自愈，不会错停活线；④自动+上限未用 ⇒ 先落自动新建终态行（它同时是「上限已用」的终身判据，写入顺序承重见 `_note_no_order_recreated`），再走下面的既有建卡路。防循环数学（拍板口径）：每（订单，绑定）自动新建上界 1 张+转人工行每窗 1 条 ⇒ 循环断根。
            #    ⚠️ 挂起判定用 `_launch_readback_breaker_state`（按台账行直算）⛔ 不用 `readback_breaker_health`（它以「12h 窗内有没有熔断 note」短路——人工旁路行不经过投递闸、可能还没有 note；「挂起照旧拦截」要按状态真值判，别按痕迹判）。
            if _launch_readback_breaker_state(binding, now=now)[0]:
                return _skip(event, reason=NO_ORDER_SUSPENDED_REASON, now=now)
            if not rearmed_manual:
                if _auto_recreate_already_used(event, binding):
                    _note_no_order_handoff_once(event, binding, remote_id, now=now)
                    # ⭐ D-0370 豁免：转人工终态行**不带指纹**（`_strip_requeue_guard_fp`）
                    #    ——D-0362 的每窗重申循环保留，护栏不得把它掐死。
                    _strip_requeue_guard_fp(event)
                    return _skip(event, reason=NO_ORDER_HANDOFF_REASON, now=now)
                _note_no_order_recreated(event, binding, remote_id)
            #    ⭐ D-0359 §11（⭐⭐ D-0362 起三重条件放宽为单条件，处置闸见本支顶部）：「逐字查无」已由 `_member_throw_prepare` 判定（判据只有那一处，本支不复核 ⛔ 不写第二份）⇒ 旧卡确实不在了，全新建卡。既有的全新录单路被两个旧锚挡着（`payload["jidanpu_order_id"]`/`payload["recorded"]` 的断点续跑快速通道、本链接行的短路）且地址级去重会按「同一地址已有链路」拒掉重录 ⇒ 拆锚后重走 `ensure_remote` 的既有全新录单路（预读/认领/录单/回读/落链路全是现成的）。录单若已发生而本轮中断，下一轮的认领预读（按来源单号）会把它认领回来并重建链接行——⛔ 不会因此灌第二张真单。
            #    ⭐ 本支放在分派块最前（承重）：拆锚重录回来若竟「认领到」了一张卡（prepare 按旧单号判「查无」、预读按来源单号却查到——两把钥匙、对端搜索窗口不同），它的判定（already/refunded/other/throw）在下面接着流进既有那四条腿（幂等收口/先改回未分配/终态跳过/直接抛）——⛔ 不在本支里复刻任何一条腿；放块尾会让 already/refunded 掉进响亮炸（`ValueError` 打断整轮 outbox）。
            #    ⭐ 拆锚的顺序承重：先清 payload 进度键（persist+内存同步）、后删旧链接行——两句之间崩溃 ⇒ 旧行还在 ⇒ 下轮 `had_link` ⇒ prepare（旧号）⇒ 查无 ⇒ 处置闸再判一次（⭐ D-0362：自动档此时上限已用 ⇒ 转人工停手；人工档照旧再 recreate——两个方向都有界且安全）；反过来先删行，中间崩溃会让链接行缺席一轮（日志快路/状态回拉看不见这张单），只能落到更远的认领兜底。
            progress = dict(event.payload or {})
            progress.pop("jidanpu_order_id", None)
            progress.pop("recorded", None)
            progress.pop("recording_source_order_no", None)
            progress.pop("recording_store", None)
            progress.pop("recording_split", None)
            progress.pop("recording_in_hall", None)
            _persist_payload(event, progress)
            # ⚠️ 内存实例**一起换**（照 `queue_member_event` 残差 A 那处的同一形状）：
            #    `_persist_payload` 只写库；紧接着的重跑 `ensure_remote` 读的是
            #    `event.payload`——不换它，旧锚还在内存里，重录根本不会发生。
            event.payload = progress
            had_link_row.delete()
            remote_id, reason, outcome, adopted = record.ensure_remote(
                event, order, binding, client, now=now, in_hall=True
            )
            recreate_note = prepare_reason
            if remote_id is None:
                if outcome == "skipped":
                    # 终态跳过（如地址级去重）：熔断不数 skipped 行 ⇒ 人话带上「为什么在重录」
                    # 的前缀，排障不用翻历史（`_skip` 自己截 255）。
                    return _skip(
                        event, reason=f"{recreate_note}；重录未成：{reason}", now=now
                    )
                # ⚠️ 可重试失败**不带前缀**：`RECORD_PRE_READ_REASON` / `RECORD_READBACK_REASON`
                #    是熔断「回读失败族」的**前缀**判据（`LAUNCH_READBACK_FAILURE_REASONS`），
                #    加前缀会让这类行逃出计数窗（E-0304 的闸会漏数）⇒ 原样给原因。
                return _retry(event, reason=reason, now=now)
            # ⭐ 窄竞态的对齐：重录的预读按来源单号认领到了 ⇒ 照认领那一支同一出处判，让本支开头说的既有四条腿接手（⛔ 不盲目补抛）。
            #    ⭐ 认领不是建卡 ⇒ 「全新建卡」那句人话同步作废（`prepare_reason` 一起清——`other` 档的人话由既有分支自己给，点名读到的状态），台账 ⛔ 不许说假话。
            if adopted is not None:
                verdict = record.adopted_verdict(adopted)
                prepare_reason = ""
                recreate_note = ""
            else:
                verdict = ADOPTED_VERDICT_NEEDS_THROW
        if verdict == ADOPTED_VERDICT_ALREADY_IN_HALL:
            # 已经在他家大厅 ⇒ 目标态已达成，只把链路补正。⭐ 这里也是**重复抛单幂等**的落点：
            # 第二次抛单读到 `isThrowOrder=Y` ⇒ 一次写都不打、一次都不重抛。
            link, created = _ensure_link(
                binding, order, remote_id=remote_id, in_hall=True, now=now
            )
            if link is None:
                # ⭐ E-0315：链路被同店老单占用 ⇒ 终态收口（结构性冲突认得出，不烧重试）。
                return _skip(event, reason=LINK_OCCUPIED_REASON, now=now)
            if not created:
                # ⚠️ 与下面「抛单成功」那一支**同一次写入**、同一个理由：`withdrawn_at` 是
                #    「已经退成」那个标记的**唯一**回置点（对方大厅里又有这张单了）。
                JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
                    in_hall=True, listed_at=timezone.now() if after_withdraw else now,
                    last_out_at=now, withdrawn_at=None,
                )
            _finish(event, status="done", reason="", now=now)
            return "done"
        if verdict == ADOPTED_VERDICT_REFUNDED:
            # ⭐ D-0257 细化（业务方拍板）：「撤单 → 上家退单 → 重抛」这条正常业务流必须能恢复，而上家平台对已退单不能直接抛单（业务方实测）⇒ 两步走：先 `refund_undo` 改回「未分配」，成了才落到下面「抛单」那一跳。没有这条腿的线（`refund_undo is None`，②式）仍跳过并留人话，不静默、不反复烧额度；⭐ D-0318：这条腿现在同时服务「认领来的单」与「早就录过的单重抛」。
            # 绑定了本次撤回的显式重抛已通过前置确认；其余只允许撤回完成后新排的事件。
            link = _link_for(binding, order)
            if jidanpu_relaunch.predates_confirmed_withdrawal(
                event, link, after_withdraw=after_withdraw,
            ):
                return _skip(
                    event, reason=STALE_LAUNCH_AFTER_WITHDRAW_REASON, now=now
                )
            undo = record.refund_undo
            if undo is None:
                return _skip(event, reason=ADOPTED_REFUNDED_REASON, now=now)
            undo_outcome, undo_reason = undo(client, order, binding, remote_id)
            if undo_outcome == THROW_OUTCOME_RETRY:
                # 改回被拒 / 回读出错 ⇒ 诚实可见：原因（含上游原话）进台账走既有退避。
                return _retry(event, reason=undo_reason, now=now)
            if undo_outcome != THROW_OUTCOME_OK:
                raise ValueError(
                    f"{line.name} 的「已退单改回未分配」那一跳回了不认识的取值：{undo_outcome!r}"
                )
            # 改回成功 ⇒ 与 NEEDS_THROW 同路：走下面「抛单」那一跳（含回读确认闸）。
            un_refunded = True
            verdict = ADOPTED_VERDICT_NEEDS_THROW
            if after_withdraw:
                blocked = _recheck_relaunch(event, binding, line, order, now=now)
                if blocked is not None:
                    return blocked
        if verdict == ADOPTED_VERDICT_NOT_UNASSIGNED:
            if (
                binding.mode == "password" and had_link_row is not None
                and had_link_row.withdrawn_at is not None and order.status == OrderStatus.WAITING_GRAB
                and (after_withdraw or rearmed_manual) and _main_taker_id(order.pk) is None
                and order.upstream_hall_card_id is None
            ):
                recovered = _recover_relaunch_claim(
                    event, binding=binding, order=order, link=had_link_row, client=client, now=now,
                )
                if recovered is not None:
                    return recovered
            # ⭐ D-0318：重抛那条路把**读到的那个状态**写进人话（`prepare_reason`）；
            #    认领那一支沿用既有那句（有既有用例逐字钉着，⛔ 不动它）。
            return _skip(
                event, reason=prepare_reason or ADOPTED_NOT_UNASSIGNED_REASON, now=now
            )
        # `ADOPTED_VERDICT_NEEDS_THROW`：对方系统里只是未分配地躺着 ⇒ 走下面「抛单」那一跳。
        # ⛔ 不认识的取值一律响亮抛（那是接线坏了，⛔ 不许当成「不用抛」静默放过）。
        if verdict != ADOPTED_VERDICT_NEEDS_THROW:
            raise ValueError(f"{line.name} 的远端状态判定回了不认识的取值：{verdict!r}")
    outcome, reason = record.throw_remote(client, order, binding, remote_id)
    if outcome == THROW_OUTCOME_RETRY:
        if un_refunded and adopted is None:
            # ⭐ D-0318：改回「未分配」成功了、补抛**仍被拒** ⇒ 业务方口径的兜底是「删除旧单
            #    重新录单」，而本线**没有删单的 SDK 函数** ⇒ 如实点名不可用、照旧判失败
            #    （⛔ 不静默、⛔ 不拿不存在的接口硬写）。
            reason = THROW_AFTER_UN_REFUND_FAILED_REASON + reason
        return _retry(event, reason=reason, now=now)
    if outcome == THROW_OUTCOME_NOT_AVAILABLE:
        # ⭐ 这一条线**没有**这个动作（②式）⇒ 终态跳过 + 人话（⛔ 不静默、⛔ 不反复烧额度）。
        #    ⚠️ `reason` **必须**由那条线给（空串 = 接线坏了、说不清为什么没做 ⇒ 响亮抛）。
        if not str(reason).strip():
            raise ValueError(f"{line.name} 抛单那一跳说「没有这个动作」却没给一句人话原因")
        return _skip(event, reason=reason, now=now)
    if outcome != THROW_OUTCOME_OK:
        raise ValueError(f"{line.name} 的抛单判定回了不认识的取值：{outcome!r}")
    if after_withdraw:
        blocked = _recheck_relaunch(event, binding, line, order, now=now)
        if blocked is not None:
            return blocked
    link, created = _ensure_link(
        binding, order, remote_id=remote_id, in_hall=True, now=now
    )
    if link is None:
        # ⭐ E-0315：抛单已成了、链路却被同店老单占用 ⇒ 终态收口（实践中到不了：
        #    认领/回读两支在更早的 `_ensure_link` 就已撞；防御钉：⛔ 不许 None 解引用）。
        return _skip(event, reason=LINK_OCCUPIED_REASON, now=now)
    if not created:
        # 已有链路（断点续跑/重复消费）⇒ 只把「在对方大厅」这一档补正。
        # ⭐ `withdrawn_at=None`：这里就是「已经退成」那个标记的唯一回置点（当时那条修复的可逆性）：这一支跑到了 ⇒ 对方系统里又有这张单了（回池/撤销删除 ⇒ 对账排 `to_launch` ⇒ 录单+抛单成功）
        #    ⇒ 上一次「已退」的结论作废，此后该退的还能退（⛔ 不置空 = 那张单被恢复之后永远不再退）。⚠️ 新建那一支（`created=True`）不需要写：新行的默认值就是 `None`。
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            in_hall=True, listed_at=timezone.now() if after_withdraw else now,
            last_out_at=now, withdrawn_at=None,
        )
    # ⭐ D-0359 §11：走了「全新建卡」那一档 ⇒ done 行带上那句放行人话（排障/审计的唯一现场：
    #    链路的远端单号换了，台账得说清为什么）；其余路径 `recreate_note` 恒空串（原样）。
    _finish(event, status="done", reason=recreate_note, now=now)
    return "done"


ASSIGN_STALE_REASON = "订单已撤回、结束或改派给其他店，本次旧指派作废"
PENALTY_RECORD_EXISTS_REASON = "罚款单已有鸡蛋铺订单关联，本次不重复录单"
PENALTY_RECORD_PREVIOUS_STORE_REASON = "罚款单在旧绑定可能已经录过，请人工核对后再处理"
PENALTY_RECORD_AMOUNT_REASON = "鸡蛋铺已有同来源罚款单，金额与审批不符或无法确认，请核对"
PENALTY_RECORD_REFUNDED_REASON = "鸡蛋铺已有同来源罚款单且已退单，本次不重录"
PENALTY_RECORD_IN_HALL_REASON = "鸡蛋铺同来源订单已在大厅，本次未录单或改动，请核对"
PENALTY_RECORD_EXPIRED_LOOKUP_REASON = "罚款单早于鸡蛋铺可核查的订单范围，请先人工核对"
PENALTY_RECORD_REMOTE_ID_CONFLICT_REASON = "鸡蛋铺同来源罚款的订单号与之前记录不同，请核对"
ASSIGN_REPAIR_REASON = "鸡蛋铺原单未能确认恢复为未分配"
ASSIGN_REBUILD_REASON = "鸡蛋铺查无原单，已补录"
ASSIGN_SOURCE_OCCUPIED_REASON = "鸡蛋铺已有相同来源单号，无法确认属于本订单"
ASSIGN_ALREADY_REBUILT_REASON = "本次指派已补录过，鸡蛋铺仍查不到补录的订单"
ASSIGN_API_MISSING_REASON = "开放平台查不到原单，需要网页登录核对回收站"
RECORD_IN_PROGRESS_REASON = "同一订单有先前的录单待确认，请核对下家旧订单"
REBINDED_EVENT_REASON = "绑定已更换凭据，本事件不再重试，请人工核对"
PIC_REPUSH_NO_LINK_REASON = "鸡蛋铺订单关联已变化，本次截图补推作废"
SOURCE_ORDER_NO_CONFIRM_REASON = "鸡蛋铺未确认新来源单号可查到原单"
SOURCE_ORDER_NO_WEB_REQUIRED_REASON = "同步来源单号需要鸡蛋铺网页账号和密码"


def _repair_error_reason(exc):
    if isinstance(exc, ApiError):
        return _upstream_reason(
            exc.args[0] if exc.endpoint is None else ASSIGN_REPAIR_REASON, exc,
        )[:255]
    return f"{ASSIGN_REPAIR_REASON}（{type(exc).__name__}）"


@contextmanager
def _open_assignment_web_session(binding):
    with jidanpu.member_session(binding) as (client, _http):
        yield client


def _assignment_web_session(line, binding, client):
    if not line.uses_openapi:
        return nullcontext(client)
    if (binding.account or "").strip() and (binding.password or "").strip():
        return _open_assignment_web_session(binding)
    return None


def _order_repair_windows(order, remote_id, now):
    today = timezone.localdate(now)
    start = min(today, timezone.localdate(order.created_at))
    # 认领可以挂接比本地早的单。内部号日期只扩大扫描范围。
    try:
        start = min(start, datetime.strptime(remote_id[:8], "%Y%m%d").date())
    except ValueError:
        pass
    start = max(date(2000, 1, 1), start - timedelta(days=jidanpu.ORDER_LIST_LOOKBACK_DAYS))
    end = today + timedelta(days=1)
    while end >= start:
        first = max(start, end - timedelta(days=jidanpu.ORDER_LIST_MAX_SPAN_DAYS - 1))
        yield {"stime": f"{first} 00:00:00", "etime": f"{end} 23:59:59"}
        end = first - timedelta(days=1)


def _find_repair_order(client, order, remote_id, *, now, recycle=False, source_order_no=None,
                       any_remote_id=False, remote_oid=None):
    for window in _order_repair_windows(order, remote_id, now):
        row = jidanpu.find_order_for_repair(
            client, remote_order_id=None if any_remote_id else remote_id, window=window, recycle=recycle,
            source_order_no=source_order_no, remote_oid=remote_oid,
        )
        if row is not None:
            return row
    return None


def _assign_still_current(order, binding):
    order.refresh_from_db(fields=("status", "deleted_at", "source_order_no"))
    if order.deleted_at is not None or order.status in (
        OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB, OrderStatus.REFUNDED, OrderStatus.COMPLETED,
    ):
        return False
    taker_id = _main_taker_id(order.pk)
    return binding.jidanpu_base_url in downstream_addresses_of_taker(taker_id)


def _is_penalty_record(event, order):
    return event.kind == KIND_ASSIGN and (
        order.penalty_approval_id is not None or PENALTY_RECORD_APPROVAL_KEY in (event.payload or {})
    )


def _penalty_record_still_current(event, order, binding):
    payload = event.payload or {}
    marked = PENALTY_RECORD_APPROVAL_KEY in payload
    approval_id = payload.get(PENALTY_RECORD_APPROVAL_KEY, order.penalty_approval_id)
    if (
        type(approval_id) is not int or approval_id != order.penalty_approval_id
        or payload.get(PENALTY_RECORD_SOURCE_KEY, order.source_order_no) != order.source_order_no
        or payload.get("recording_source_order_no", order.source_order_no) != order.source_order_no
        or payload.get("assign_replaced_remote_id")
    ):
        return False
    return _penalty_order_still_current(order, binding, allow_completed=marked)


def _penalty_order_still_current(order, binding, *, allow_completed):
    if (
        order.penalty_approval_id is None or order.hall_price is None or order.hall_price >= 0
        or not order.source_order_no
    ):
        return False
    statuses = PENALTY_RECORD_ASSIGNED_STATUSES
    if allow_completed:
        statuses = (*statuses, OrderStatus.COMPLETED)
    taker_id = _main_taker_id(order.pk)
    if taker_id != binding.user_id or not Order.objects.filter(
        pk=order.pk, status__in=statuses, hall_price=order.hall_price,
        source_order_no=order.source_order_no, penalty_approval_id=order.penalty_approval_id,
        penalty_approval__kind=ApprovalKind.PENALTY, penalty_approval__status=ApprovalStatus.APPROVED,
        penalty_approval__amount=-order.hall_price,
    ).exclude(
        Q(penalty_approval__original_order__source_order_no=order.source_order_no)
        | Q(penalty_approval__original_order__order_no=order.source_order_no),
    ).exists():
        return False
    return _binding_current_rows(binding).filter(user_id=taker_id, enabled=True, user__is_active=True).exclude(
        user__staff_profile__deleted_at__isnull=False,
    ).exists()


def _penalty_record_remote_conflict(order, remote):
    status = _order_sta_token(remote)
    if status == jidanpu.ORDER_REFUNDED_LABEL:
        return PENALTY_RECORD_REFUNDED_REASON
    if str(remote.get("isThrowOrder") or "") == "Y" or map_remote_status(status) == OrderStatus.WAITING_GRAB:
        return PENALTY_RECORD_IN_HALL_REASON
    amount = _remote_amount_value(remote, "price")
    if amount is None or not amount.is_finite() or amount != order.hall_price:
        return PENALTY_RECORD_AMOUNT_REASON
    return ""


def _set_event_progress(event, payload):
    _persist_payload(event, payload)
    event.payload = payload


RECORDING_PROGRESS_FIELDS = (
    "recorded", "jidanpu_order_id", "recording_source_order_no", "recording_remote_oid",
    "assign_replaced_remote_id", "recording_store", "recording_split", "recording_in_hall",
    PENALTY_OPENAPI_RECEIPT_KEY,
)


def _another_recording_owns_order(event, binding, *, now):
    """Keep uncertain recording progress with its original store and credentials."""
    recordings = JidanpuSyncEvent.objects.filter(
        direction="out", kind__in=(KIND_ASSIGN, KIND_LAUNCH), target_type="order",
        target_ref=event.target_ref,
    )
    store = binding.jidanpu_base_url
    recorded_attempts = recordings.filter(_recording_evidence())
    same_binding = Q(payload__member_binding_id=binding.pk)
    switched_account = Q(status__in=("pending", "failed")) & ~same_binding
    stopped_attempt = Q(status="skipped") & (
        Q(payload__recording_progress_transferred__isnull=True)
        | ~Q(payload__recording_progress_transferred=True)
    )
    # Old attempts saved the source before sending; a missing store leaves the result uncertain.
    legacy_retired_attempt = Q(
        status="skipped", reason=REBINDED_EVENT_REASON, payload__recording_store__isnull=True,
    ) & (
        Q(payload__recorded=True)
        | Q(payload__recording_source_order_no__regex=r"\S")
        | (Q(payload__jidanpu_order_id__isnull=False) & ~Q(payload__jidanpu_order_id=""))
    )
    if recorded_attempts.filter(
        Q(payload__recording_store=store) & (switched_account | stopped_attempt) | legacy_retired_attempt
    ).exists():
        return True
    legacy_attempts = recorded_attempts.exclude(same_binding).filter(
        status__in=("pending", "failed"), payload__recording_store__isnull=True,
    ).values_list("payload__member_binding_id", flat=True)
    legacy_binding_ids = {pk for pk in legacy_attempts if type(pk) is int}
    if JidanpuMemberBinding.objects.filter(pk__in=legacy_binding_ids, jidanpu_base_url=store).exists():
        return True
    recordings = recordings.filter(same_binding, status__in=("pending", "failed")).filter(
        _recording_evidence() | Q(payload__assign_replaced_remote_id__isnull=False)
    )
    owner = recordings.order_by("id").first()
    if owner is None or owner.pk == event.pk:
        return False
    if owner.attempts < OUTBOX_MAX_ATTEMPTS:
        return True
    # 同一绑定的 outbox 串行运行。上一事件额度用完时让本事件从原断点继续。
    progress = {
        field: value for field, value in (event.payload or {}).items()
        if field not in RECORDING_PROGRESS_FIELDS
    }
    progress.update({
        field: owner.payload[field] for field in RECORDING_PROGRESS_FIELDS
        if field in owner.payload
    })
    if progress.get("recorded") or progress.get("jidanpu_order_id"):
        progress.setdefault("recording_in_hall", owner.kind == KIND_LAUNCH)
    with transaction.atomic():
        _set_event_progress(event, progress)
        _set_event_progress(owner, {**owner.payload, "recording_progress_transferred": True})
        _skip(owner, reason="录单确认已由后续同步任务接续", now=now)
    return False


def _prepare_assign_replacement(event, link):
    payload = dict(event.payload or {})
    for field in RECORDING_PROGRESS_FIELDS:
        payload.pop(field, None)
    payload["assign_replaced_remote_id"] = link.jidanpu_order_id
    with transaction.atomic():
        removed, _counts = JidanpuMemberOrderLink.objects.filter(
            pk=link.pk, order_id=link.order_id, binding_id=link.binding_id,
            jidanpu_order_id=link.jidanpu_order_id,
        ).delete()
        if removed != 1:
            raise ApiError(ASSIGN_REPAIR_REASON)
        _set_event_progress(event, payload)


def _sync_remote_source_order_no(client, order, remote_id, *, now, current):
    expected = effective_source_order_no(order)
    if current.get(jidanpu.OWN_SOURCE_VALUE_FIELD) != expected:
        jidanpu.change_source_order_no(
            client, remote_order_id=remote_id, source_order_no=expected,
        )
    if _find_repair_order(
        client, order, remote_id, now=now, source_order_no=expected,
    ) is None:
        raise ApiError(SOURCE_ORDER_NO_CONFIRM_REASON)


def _repair_linked_assign(event, order, link, *, binding, client, now, adopted=None):
    """复用、恢复或拆除已确认查无的旧链路。"""
    remote_id = link.jidanpu_order_id
    remote = adopted if adopted is not None else _find_repair_order(client, order, remote_id, now=now)
    progress = (event.payload or {}).get("assign_restoring")
    restoring = isinstance(progress, dict) and progress.get("remote_id") == remote_id
    if remote is None:
        recycled = _find_repair_order(client, order, remote_id, now=now, recycle=True)
        if recycled is None:
            # 恢复可能恰好发生在查正常列表和查回收站之间。
            remote = _find_repair_order(client, order, remote_id, now=now)
        if recycled is not None:
            if not _assign_still_current(order, binding):
                return ASSIGN_STALE_REASON
            recycled_state = _order_sta_token(recycled)
            if not recycled_state:
                raise ApiError(ASSIGN_REPAIR_REASON)
            progress = {"remote_id": remote_id, "status": recycled_state}
            _set_event_progress(event, {**(event.payload or {}), "assign_restoring": progress})
            restoring = True
            jidanpu.restore_recycled_order(client, remote_order_id=remote_id)
            remote = _find_repair_order(client, order, remote_id, now=now)
        elif remote is None:
            if not _assign_still_current(order, binding):
                return ASSIGN_STALE_REASON
            if restoring:
                raise ApiError(ASSIGN_REPAIR_REASON)
            if (event.payload or {}).get("assign_replaced_remote_id"):
                return ASSIGN_ALREADY_REBUILT_REASON
            source_order_no = effective_source_order_no(order)
            if any(_find_repair_order(
                client, order, remote_id, now=now, recycle=recycle,
                source_order_no=source_order_no, any_remote_id=True,
            ) is not None for recycle in (False, True)):
                raise ApiError(ASSIGN_SOURCE_OCCUPIED_REASON)
            if not _assign_still_current(order, binding):
                return ASSIGN_STALE_REASON
            if effective_source_order_no(order) != source_order_no:
                raise ApiError(ASSIGN_REPAIR_REASON)
            _prepare_assign_replacement(event, link)
            return ""
    state = _order_sta_token(remote)
    if not state:
        raise ApiError(ASSIGN_REPAIR_REASON)
    if state == jidanpu.ORDER_REFUNDED_LABEL or (restoring and state == progress["status"]):
        if not _assign_still_current(order, binding):
            return ASSIGN_STALE_REASON
        if state != jidanpu.ORDER_UNASSIGNED_LABEL:
            jidanpu.set_order_status(
                client, remote_order_id=remote_id, status_label=jidanpu.ORDER_UNASSIGNED_LABEL,
            )
            remote = _find_repair_order(client, order, remote_id, now=now)
        if _order_sta_token(remote) != jidanpu.ORDER_UNASSIGNED_LABEL:
            raise ApiError(ASSIGN_REPAIR_REASON)
    if restoring or _order_sta_token(remote) == jidanpu.ORDER_UNASSIGNED_LABEL:
        JidanpuMemberOrderLink.objects.filter(pk=link.pk, jidanpu_order_id=remote_id).update(
            in_hall=remote.get("isThrowOrder") == "Y", withdrawn_at=None, last_out_at=now,
        )
    if restoring:
        payload = dict(event.payload or {})
        payload.pop("assign_restoring", None)
        _set_event_progress(event, payload)
    renaming = (event.payload or {}).get("assign_renaming_remote_id") == remote_id
    if renaming or remote.get(jidanpu.OWN_SOURCE_VALUE_FIELD) != effective_source_order_no(order):
        if not _assign_still_current(order, binding):
            return ASSIGN_STALE_REASON
        if not renaming:
            _set_event_progress(event, {
                **(event.payload or {}), "assign_renaming_remote_id": remote_id,
            })
        _sync_remote_source_order_no(client, order, remote_id, now=now, current=remote)
    return ""


def _repair_api_assign(order, link, *, binding, client, now, line):
    remote_id = link.jidanpu_order_id
    remote = line.status_push.read_remote(client, remote_id)
    state = _order_sta_token(remote)
    if not state:
        raise ApiError(ASSIGN_API_MISSING_REASON)
    if state != jidanpu.ORDER_REFUNDED_LABEL:
        return ""
    if not _assign_still_current(order, binding):
        return ASSIGN_STALE_REASON
    line.status_push.write_remote(client, remote_id, jidanpu.ORDER_UNASSIGNED_LABEL)
    remote = line.status_push.confirm_remote(client, remote_id)
    if _order_sta_token(remote) != jidanpu.ORDER_UNASSIGNED_LABEL:
        raise ApiError(ASSIGN_REPAIR_REASON)
    JidanpuMemberOrderLink.objects.filter(pk=link.pk, jidanpu_order_id=remote_id).update(
        withdrawn_at=None, last_out_at=now,
    )
    return ""


def _consume_source_order_no(event, *, binding, client, http, now, line=None):
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    link = _link_for(binding, order)
    if link is None:
        return _retry(event, reason="等鸡蛋铺录单完成后再同步来源单号", now=now)
    if link.withdrawn_at is not None:
        return _skip(event, reason=EDIT_WITHDRAWN_COPY_REASON, now=now)
    session = _assignment_web_session(line or member_outbox_line(), binding, client)
    if session is None:
        return _skip(event, reason=SOURCE_ORDER_NO_WEB_REQUIRED_REASON, now=now)
    remote_id = link.jidanpu_order_id
    try:
        with session as web_client:
            current = _find_repair_order(web_client, order, remote_id, now=now)
            if current is None:
                return _retry(event, reason=SOURCE_ORDER_NO_CONFIRM_REASON, now=now)
            _sync_remote_source_order_no(web_client, order, remote_id, now=now, current=current)
    except UpstreamBlockedError:
        raise
    except JidanpuError as exc:
        return _retry(event, reason=_repair_error_reason(exc), now=now)
    _finish(event, status="done", reason="", now=now)
    return "done"


def _consume_assign(event, *, binding, client, http, now, line=None) -> str:
    """核对或补录鸡蛋铺订单。截图补推沿用原链路。"""
    line = line or member_outbox_line()
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    if order.hall_price is None:
        # ⛔ 本单没有发单价 ⇒ **不录单**（与抛单那一档同口径）：下家的接单价就是发单价，
        #    没有价就没有可写的东西；⛔ 不写 `"0"`（那等于告诉下家这单不要钱）、
        #    ⛔ 更不回落客户价（那是我们的底价）。填上发单价后由 180 秒对账的 `to_record` 补录。
        return _skip(event, reason=ASSIGN_NO_PRICE_REASON, now=now)
    if _is_pic_repush(event.kind, event.payload):
        link = _link_for(binding, order)
        if link is None:
            return _skip(event, reason=PIC_REPUSH_NO_LINK_REASON, now=now)
        if link.withdrawn_at is not None or event.payload.get(PIC_LINK_SNAPSHOT_KEY) != _picture_link_snapshot(link):
            return _skip(event, reason="原下家副本已撤回、变更或无法确认，本次不补推截图", now=now)
        _push_screenshots_of_line(line, order=order, binding=binding, link=link, client=client, http=http, now=now)
        return "done" if _finish(event, status="done", reason="", now=now) else "deferred"
    penalty_record = _is_penalty_record(event, order)
    if not (_penalty_record_still_current(event, order, binding) if penalty_record
            else _assign_still_current(order, binding)):
        return _skip(event, reason=ASSIGN_STALE_REASON, now=now)
    existing = _link_for(binding, order)
    if penalty_record:
        if existing is not None:
            return _skip(event, reason=PENALTY_RECORD_EXISTS_REASON, now=now)
        if JidanpuMemberOrderLink.all_objects.filter(order=order).exists() or JidanpuSyncEvent.objects.filter(
            _recording_evidence(), direction="out", kind__in=(KIND_ASSIGN, KIND_LAUNCH),
            target_type="order", target_ref=event.target_ref,
        ).exclude(pk=event.pk).exclude(payload__recording_store=binding.jidanpu_base_url).exists():
            return _skip(event, reason=PENALTY_RECORD_PREVIOUS_STORE_REASON, now=now)
    if existing is None and _another_recording_owns_order(event, binding, now=now):
        return _retry(event, reason=RECORD_IN_PROGRESS_REASON, now=now)
    if existing is not None:
        try:
            session = _assignment_web_session(line, binding, client)
            if session is None:
                reason = _repair_api_assign(
                    order, existing, binding=binding, client=client, now=now, line=line,
                )
            else:
                with session as web_client:
                    reason = _repair_linked_assign(
                        event, order, existing, binding=binding, client=web_client, now=now,
                    )
        except UpstreamBlockedError:
            raise
        except JidanpuError as exc:
            return _retry(event, reason=_repair_error_reason(exc), now=now)
        if reason:
            return _skip(event, reason=reason, now=now)
    if (event.payload or {}).get("assign_replaced_remote_id") and not _assign_still_current(order, binding):
        return _skip(event, reason=ASSIGN_STALE_REASON, now=now)
    try:
        remote_id, reason, outcome, adopted = _record_of(line).ensure_remote(
            event, order, binding, client, now=now, in_hall=False,
        )
    except UpstreamBlockedError:
        raise
    except JidanpuError as exc:
        return _retry(event, reason=_repair_error_reason(exc), now=now)
    if remote_id is None:
        return _skip(event, reason=reason, now=now) if outcome == "skipped" else _retry(
            event, reason=reason, now=now
        )
    # ⛔ 不抛单 ⇒ 链路 `in_hall=False`（对方那边它只在他家订单列表里，spec §5.1 第二行）。
    #    ⚠️ `_ensure_link` 对**已存在**的链路**原样返回**（不改 `in_hall`）⇒ 下面还要单独判一次
    #    「它此刻是不是还挂在对方大厅」（先抛大厅、后被指派时就是 True）。
    link, _created = _ensure_link(binding, order, remote_id=remote_id, in_hall=False, now=now)
    if link is None:
        # ⭐ E-0315：链路被同店老单占用 ⇒ 终态收口；后面的撤下排队与传图都拿不到
        #    链路行，⛔ 不能再往下走（结构性冲突认得出，不烧重试、不触发上抛）。
        return _skip(event, reason=LINK_OCCUPIED_REASON, now=now)
    if penalty_record:
        # 已有的罚款副本只认领，不套用普通指派的远端修复、撤下和重建。
        return "done" if _finish(event, status="done", reason="", now=now) else "deferred"
    if adopted is not None:
        try:
            session = _assignment_web_session(line, binding, client)
            if session is not None:
                with session as web_client:
                    reason = _repair_linked_assign(
                        event, order, link, binding=binding, client=web_client, now=now,
                        adopted=adopted,
                    )
                if reason:
                    return _skip(event, reason=reason, now=now)
        except UpstreamBlockedError:
            raise
        except JidanpuError as exc:
            return _retry(event, reason=_repair_error_reason(exc), now=now)
    _queue_unlist_if_in_hall(order, binding)
    # ⭐ A 段：传我方截图到下家那张单（`link.jidanpu_order_id` 就是远端单号）。
    #    ⚠️ **失败不挡派单**（口径差异，见函数 docstring）⇒ 返回值只在「有失败 / 没接线」时非 None。
    _push_screenshots_of_line(
        line, order=order, binding=binding, link=link, client=client, http=http, now=now
    )
    reason = ASSIGN_REBUILD_REASON if (event.payload or {}).get("assign_replaced_remote_id") else ""
    _finish(event, status="done", reason=reason, now=now)
    return "done"


def _push_order_screenshots_to_downstream(*, order, binding, link, client, http, now
                                          ) -> str | None:
    """传图失败不挡派单，部分成功也记映射，后续只补对方缺失的图。"""
    # 函数内 import：`jidanpu_upstream_pics` 在模块级 import `jidanpu_upstream`，而
    # `jidanpu_upstream` 又 import 本模块 ⇒ 模块级 import 会成环（照上家线那处的同一处置）。
    from apps.business import jidanpu_upstream_pics

    if not jidanpu_upstream_pics.choose_screenshots(order, img_types=MEMBER_PUSH_IMG_TYPES):
        return None
    if http is None:
        # ⭐ 接线合同：`consume_event` / `run_outbox` 建连那一对里的 HTTP 层
        # 一路带到这里；生产唯一调用点必然给值（`jidanpu_sync.run_outbox(binding, now=…)`）。
        # ⛔ 只可能是注入式用法漏了它 ⇒ 让它**响亮**（E-0010），不要把编程错伪装成业务失败。
        raise ValueError(
            "传图需要 http（上传页隐藏域那一跳）；调用方必须把 "
            "open_client_with_http(binding) 的 HTTP 层带下来"
        )
    registered: list[jidanpu_upstream_pics.PushedShot] = []
    try:
        outcome = jidanpu_upstream_pics.push_order_screenshots(
            client, order=order, binding=binding, link=link, http=http, now=now,
            remote_order_id=link.jidanpu_order_id, img_types=MEMBER_PUSH_IMG_TYPES,
            # ⭐ E-0282：关掉「对面已有图就整档不传」的短路、改走 per-pic
            #    判缺（只补缺的那几张；见 docstring「per-pic 判缺」一节）。
            skip_if_remote_has_pic=False,
            only_missing=True,
            on_pushed=registered.append,
        )
    except Exception as exc:  # noqa: BLE001 — 传图失败不挡派单
        reason = PUSH_PIC_FAILED_REASON.format(reason=type(exc).__name__)
        pushed = tuple(registered)
    else:
        reason = PUSH_PIC_FAILED_REASON.format(reason=outcome.reason) if outcome.failed else None
        pushed = outcome.pushed
    extra_payload = {
        PUSHED_PIC_KEYS_PAYLOAD: {shot.object_key: shot.oss_key for shot in pushed},
    } if pushed else None
    if reason is not None:
        _note_order_once(
            binding=binding, order=order, kind=KIND_ASSIGN, reason=reason, status="failed",
            extra_payload=extra_payload,
        )
    elif pushed:
        _note_order_once(
            binding=binding, order=order, kind=KIND_ASSIGN, status="done",
            reason=PUSH_PIC_DONE_REASON.format(n=len(pushed), kinds=_pushed_kinds_text(pushed)),
            extra_payload=extra_payload,
        )
    return reason


def _pushed_kinds_text(pushed) -> str:
    """传上去的那几张的图种构成（人话；⛔ 不含对象名/URL）。⚠️ 标签表只覆盖两种图种：`jidanpu_upstream_pics.IMG_TYPE_TO_UPLOAD` 是硬闸门 ⇒ 能传上去的图必然在其中之一，别的取值在那里就响亮 `KeyError` 了
    （⛔ 不静默——所以这里 ⛔ 不写「图种不明」那种兜底话术，那会把一个不该发生的状态说成正常）。"""
    counts: dict[int, int] = {}
    for shot in pushed:
        counts[shot.img_type] = counts.get(shot.img_type, 0) + 1
    return "、".join(
        f"{PUSHED_KIND_LABELS[img_type]} {count} 张"
        for img_type, count in sorted(counts.items())
    )


def historical_pushed_pic_keys(order, binding) -> dict[str, set[str]]:
    """该链路历史台账里登记过的 `{本地 object_key → {对面 oss_key…}}`（B1 判重的取数口）。⭐ E-0282/H1：`push_order_screenshots(only_missing=True)` 传图前用它对照远端——「历史登记过的 oss_key 已在远端出现」的那几张跳过、其余照传。
    读的是 `_note_order_once` 写下的 `direction="in"`、`kind=KIND_ASSIGN` 台账行里 `payload[PUSHED_PIC_KEYS_PAYLOAD]` 那一格（一行承载一次推送的全部对象名；多行求并：同一张图在不同轮次可能以不同 oss_key 推过，全部算「推过」）；行里没有那一格/形状不对 ⇒ 该行贡献为空（缺口期：本键上线前推成功的图没有映射 ⇒ 判重认不出 ⇒ 那一张退化为重传一次，影响面有界、可接受、不炸）；
    键与值都只认非空字符串（写方坏了不在这里替它兜，照 `queue_member_event`「只认 int」的同一条纪律）。"""
    result: dict[str, set[str]] = {}
    rows = JidanpuSyncEvent.objects.filter(
        direction="in", kind=KIND_ASSIGN, target_ref=order.order_no,
        payload__member_binding_id=binding.pk,
    )
    for row in rows:
        mapping = (row.payload or {}).get(PUSHED_PIC_KEYS_PAYLOAD)
        if not isinstance(mapping, dict):
            continue
        for object_key, oss_key in mapping.items():
            if (
                isinstance(object_key, str) and object_key
                and isinstance(oss_key, str) and oss_key
            ):
                result.setdefault(object_key, set()).add(oss_key)
    return result


def _consume_withdraw(event, *, binding, client, http, now, line=None) -> str:
    """`member.withdraw`：退单（既有原语+逐字原因）+链路降级（`in_hall=False`）。⚠️ 走 `Order.all_objects`：我方先撤回/删除的那张单只剩链路行能指认它（订单已不在默认读路径里）⛔ 用默认 manager 会永远退不掉；⚠️ 回读为准（照既有那条线）：重复推送可能回业务失败（已是目标态）⇒ 一律回读目标态确认后才算 `done`，「读不到」不等于「已退单」。
    官方接口查不到时沿用终态收口和链路标记，汇总只采信明确确认的回包。
    ⭐⭐ D-0249（修 E-0246）改准：②式的「撤下大厅」（`member.unlist`）不再进本函数——官方 API 没有不毁单的撤下原语，②式线声明 `unlist_unavailable_reason` ⇒ unlist 在分派处就终态跳过、一次上游都不打（E-0246 现场：收敛成退单把接手人自己店里的活单推成已退单）⇒ 本函数的 `event.kind` 恒为 `KIND_WITHDRAW`（我方真退单，对方那份无论什么状态都该终止，无条件推——既有业务规则）。⚠️ 历史注：D-0249 之前②式 unlist 经 `converged_kinds` 收敛进本函数并带「推已退单前预读」闸（D-0227/E-0236）——那条闸只保护「对方已接走」、挡不住「未分配」这一档（事故本身）⇒ 整条收敛路随 D-0249 摘除，E-0236 的保护由「②式 unlist 永不打上游」结构性承接（比闸更强）。⚠️ `line` 缺省=①式（`member_outbox_line()`，行为逐字不变）：②式那条分派层显式传它自己那一份；⛔ 缺省不是「按 `binding.mode` 猜」（E-0202 家族）。⭐⭐ 生产事故的承重兜底：若这张单当前 MAIN 接单人名下任何绑定（⛔ 不筛 `enabled`）与 `binding.jidanpu_base_url` 同地址 ⇒ 这张单是他自己店里正在做的活单 ⇒ 一次上游都不打、终态 `skipped`+`WITHDRAW_SAME_STORE_REASON`，且 `link`（含 `withdrawn_at`）一个字都不改（事故现场：同店①式管理员启用真正在推+②式下家系统账号停用，客服指派给②式那个账号 ⇒ 旧判据判成「我方别的打手接了」⇒ ①式那条台账把对方正在做的活单退成「已退单」；⚠️ 排队侧那三处也按同一条口径不排，但挡不住已经排好的旧事件 ⇒ 这一道是承重的）。"""
    line = line or member_outbox_line()
    latest = jidanpu_relaunch.latest_withdrawal(event.target_ref, binding)
    if latest is not None and latest.pk != event.pk:
        return _skip(event, reason="已有更新的撤回任务，本次旧撤回作废", now=now)
    order = Order.all_objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=WITHDRAW_ORDER_MISSING_REASON, now=now)
    link = _link_for(binding, order)
    if link is None:
        return _skip(event, reason=WITHDRAW_NO_LINK_REASON, now=now)
    # ⭐⭐ 承重的那一道（生产事故，订单 20261025094455951882）：退单真正发上游之前再判一次「接手人是不是这家店自己的人」——排队侧那三处只挡新排的事件，而事故现场那条台账是旧代码已经排好的
    #    （`payload.member_binding_id` 指向①式那条启用绑定，而接手人是同店②式那个停用账号）⇒ 只有这一道挡得住它。⚠️ 位置必须在 `withdraw.refund_remote` 之前（一次上游都不许打）；
    #    ⚠️ `link` 一个字都不动——尤其 `withdrawn_at` 留空：这张单还在下家做，以后回池/重新推时对账要能再走（记成「退成过」会把 `reconcile` 的回置路堵死）。
    if binding.jidanpu_base_url in downstream_addresses_of_taker(_main_taker_id(order.pk)):
        return _skip(event, reason=WITHDRAW_SAME_STORE_REASON, now=now)
    remote_id = link.jidanpu_order_id
    withdraw = _withdraw_of(line)
    # ⭐⭐ D-0325：上家镜像隐藏单的竞态闸（E-0236 同形）——上游卡 `gone`（被别家接走/撤牌）与下家打手接走副本的赛跑：隐藏单没有 MAIN 接手人 ⇒ 上面那句同店闸救不了它（`_main_taker_id` 回 None ⇒ 地址集空 ⇒ 照退）；
    #    而排队侧（`upstream_hall_shadow.refund_for_card`）只在排队那一刻判，挡不住「先排好、后接走」的次序 ⇒ 闸放在消费侧最后一刻：退单真正发上游之前先只读一次远端，读到的 `orderSta` 不是「未分配/已退单」（有人在做的「代练中」/已完成…）⇒ 终态跳过+人话（保下家活单，不连坐退单，D-0325 拍板⑦）；
    #    ⚠️ `link.withdrawn_at` 不写（这张单还活着，以后真要退还能退）；「未分配/已退单/读不到」⇒ 照旧退（①式「读不到」是口径问题，不是「有人在做的证据」——拿不准的状态按既有退单流走，拿不准的次序才保守）。
    if order.upstream_hall_card_id is not None:
        current = None
        try:
            current, _ = withdraw.read_remote(client, remote_id)
        except ApiError as exc:
            return _retry(
                event, reason=_upstream_reason(WITHDRAW_READBACK_REASON, exc), now=now
            )
        if current is not None:
            label = str(current.get("orderSta", "") or "")
            if label not in (jidanpu.ORDER_UNASSIGNED_LABEL, jidanpu.ORDER_REFUNDED_LABEL):
                return _skip(
                    event, reason=WITHDRAW_UPSTREAM_LIVE_REASON.format(label=label or "?"),
                    now=now,
                )
    confirmed = None
    confirmed_absent = False
    needs_refund = True
    if latest is not None and (latest.payload or {}).get(WITHDRAW_HALL_ONLY_KEY) is True:
        try:
            confirmed, confirmed_absent = withdraw.read_remote(client, remote_id)
        except ApiError as exc:
            return _retry(event, reason=_upstream_reason(WITHDRAW_READBACK_REASON, exc), now=now)
        if confirmed is None:
            if not (confirmed_absent and withdraw.absent_means_gone_reason):
                return _retry(event, reason=WITHDRAW_READBACK_REASON, now=now)
            needs_refund = False
        elif str(confirmed.get("orderSta") or "") == jidanpu.ORDER_REFUNDED_LABEL:
            needs_refund = False
        elif not (
            map_remote_status(confirmed.get("orderSta")) in (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB)
            and (link.in_hall if line.uses_openapi else confirmed.get("isThrowOrder") == "Y")
        ):
            explicitly_outside_hall = (
                not line.uses_openapi and confirmed.get("isThrowOrder") == "N"
                and map_remote_status(confirmed.get("orderSta")) in (OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB)
            )
            reason = WITHDRAW_OUTSIDE_HALL_REASON if explicitly_outside_hall else WITHDRAW_NOT_LISTED_REASON
            return _skip(event, reason=reason, now=now)
        if binding.jidanpu_base_url in downstream_addresses_of_taker(_main_taker_id(order.pk)):
            return _skip(event, reason=WITHDRAW_SAME_STORE_REASON, now=now)
    if needs_refund:
        try:
            withdraw.refund_remote(client, remote_id)
        except ApiError:
            pass  # 对方可能已是目标态，以回读为准。
        else:
            # 下家受理后可能回读失败，重试确认时仍需认出本轮动作。
            _remember_withdrawal_evidence(event, WITHDRAW_COMMAND_ACCEPTED_KEY)
        try:
            confirmed, confirmed_absent = withdraw.read_remote(client, remote_id)
        except ApiError as exc:
            return _retry(event, reason=_upstream_reason(WITHDRAW_READBACK_REASON, exc), now=now)
    if confirmed is None and confirmed_absent and withdraw.absent_means_gone_reason:
        with transaction.atomic():
            confirmed_at = timezone.now()
            JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
                in_hall=False, withdrawn_at=confirmed_at
            )
            _remember_withdrawal_evidence(event, jidanpu_relaunch.WITHDRAW_ABSENCE_CONFIRMED_KEY)
            return _skip(event, reason=withdraw.absent_means_gone_reason, now=confirmed_at)
    refunded = confirmed is not None and str(
        (confirmed or {}).get("orderSta", "") or ""
    ) == jidanpu.ORDER_REFUNDED_LABEL
    if not refunded:
        # ⭐⭐ **E-0316（业务方拍板「按次数」）**：回读确认支第 3 次（含）起不再 `_retry`——本地退款已完成、
        #    上家那张卡的追认不下来，继续烧只会喂两族死循环的 B 族；终态 `skipped`+一句人话。
        #    ⚠️ `link`（含 `withdrawn_at`）一个字不写：目标态**没有**被确认成立，⛔ 不许把「没追认到」说成「已经退成」（「退成后不再退」的承重标记）；⚠️ 计数=单行内计（`event.attempts`，`_retry` 每失败一次 +1），第 1/2 次照常重试（上家状态短期可能自己变），第 3 次终态收口。
        if (event.attempts or 0) + 1 >= WITHDRAW_CONFIRM_MAX_ATTEMPTS:
            return _skip(event, reason=WITHDRAW_CONFIRM_GIVEUP_REASON, now=now)
        return _retry(event, reason=WITHDRAW_CONFIRM_REASON, now=now)
    # ⭐⭐ 退成之后把「已经退成」落在链路那一列上：这就是「同一张单不再被反复退」的终态判据——少了它，下一轮对账又会按 `link is not None` 排一条（台账的 `done` 不挡新的一条，去重只挡在飞）。
    #    ⚠️ 与 `in_hall=False` 同一次写入：两者都是「这一次退单办成了」的结论，分开写会在半路失败时留下「标记退过、但链路还标着在大厅」这种半新半旧的行。
    with transaction.atomic():
        confirmed_at = timezone.now()
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            in_hall=False, withdrawn_at=confirmed_at
        )
        reason = "" if _has_withdrawal_evidence(event, WITHDRAW_COMMAND_ACCEPTED_KEY) else WITHDRAW_ALREADY_CLEARED_REASON
        _finish(event, status="done", reason=reason, now=confirmed_at)
    return "done"


def _consume_status_out(event, *, binding, client, http, now, line=None) -> str:
    """按订单现态和本次意图推状态，保留接手店校验及手动推送的防倒退规则。"""
    line = line or member_outbox_line()
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    # ⭐ 手动开关（计划 §3）：**缺省 / 非 `True` ⇒ 自动口径**，下面三段一行都不受影响。
    manual = _status_out_manual(event)
    label = PUSHABLE_STATUS_LABELS.get(order.status)
    if label is None and (
        order.status == OrderStatus.IN_PROGRESS
        and _status_out_from_status(event) in REVERT_SOURCE_STATUSES
    ):
        label = REVERT_PUSH_LABEL
    if label is None and manual:
        # ⭐ 手动推送才多出来的两档（待验收 / 已完成）。⛔ 别把这两档并进
        # `PUSHABLE_STATUS_LABELS`：那张表是**自动路径**的推送集，并进去会静默改自动口径。
        label = _manual_push_label(order.status)
    if label is None:
        return _skip(event, reason=STATUS_NOT_PUSHABLE_REASON, now=now)
    # ⭐ 接手人同店闸（2026-11-05 业务方拍板，E-0318/D-0376）：复活档必须有 MAIN 接手人、且本绑定
    # 指向接手人名下的店；问题单/已暂停在**有**接手人时只推接手人那家店（没有 ⇒ 照旧发全部）；
    # 「已退单」无闸（业务方拍板发全部）、「manual=True」的两档不受闸。在取链路之前——被闸的行
    # 零链路查询、零上游调用（与上面不满足判据那一档同一条便宜顺序）。
    if label == REVERT_PUSH_LABEL:
        taker_id = _main_taker_id(order.pk)
        if taker_id is None:
            return _skip(event, reason=REVERT_PUSH_NO_TAKER_REASON, now=now)
        if binding.jidanpu_base_url not in downstream_addresses_of_taker(taker_id):
            return _skip(event, reason=STATUS_PUSH_NOT_TAKER_STORE_REASON, now=now)
    elif order.status in (OrderStatus.ISSUE, OrderStatus.PAUSED):
        taker_id = _main_taker_id(order.pk)
        if taker_id is not None and (
            binding.jidanpu_base_url not in downstream_addresses_of_taker(taker_id)
        ):
            return _skip(event, reason=STATUS_PUSH_NOT_TAKER_STORE_REASON, now=now)
    link = _link_for(binding, order)
    if link is None:
        return _skip(event, reason=STATUS_NO_LINK_REASON, now=now)
    remote_id = link.jidanpu_order_id
    push = line.status_push
    # ⭐ 推前回读：对方已经是目标态 ⇒ 这一跳已经达成，⛔ 一次写接口都不打（见 docstring 的真机依据）。
    try:
        current = push.read_remote(client, remote_id)
    except ApiError as exc:
        return _retry(
            event, reason=_upstream_reason(STATUS_PRE_READ_FAILED_REASON, exc), now=now
        )
    # ⚠️ 比较口用 `_order_sta_token`（键缺失 / `None` / 非字符串 / 空白 ⇒ 空串，fail-closed）：
    #    「读不出来」与「读到目标态」必须是**两个**结果（⛔ 别用真值判断混成一档）。
    current_token = _order_sta_token(current)
    if current_token == label:
        _finish(event, status="done", reason="", now=now)
        return "done"
    # ⭐ 防倒退（只在手动推送目标=待验收时判，计划 §3）：对方已经完成/已经退单了，我们这一次是要把它改回「待验收」⇒ 终态跳过、一次写都不打。⚠️ 两档分开说（⛔ 合成一句）：退单与完成是两种现场，台账要能分辨。
    #    ⚠️ 自动路径不判这一条：它的目标集里没有「待验收」（只有三档+代练中例外），在这里对它多判一次只会多出一条没有用例覆盖的死分支（本仓不写那种）。
    if manual and label == MANUAL_PENDING_REVIEW_LABEL:
        if current_token == REMOTE_DONE_STATUS:
            return _skip(event, reason=MANUAL_PENDING_REVIEW_AFTER_DONE_REASON, now=now)
        if current_token == jidanpu.ORDER_REFUNDED_LABEL:
            return _skip(
                event, reason=MANUAL_PENDING_REVIEW_AFTER_REFUNDED_REASON, now=now
            )
    try:
        push.write_remote(client, remote_id, label)
    except ApiError as exc:
        return _retry(event, reason=_upstream_reason(STATUS_FAILED_REASON, exc), now=now)
    # ⭐ 回读为准（照 `_consume_withdraw` 的形状）：写接口回成功不等于对方真变成了目标态（E-0087 同族）⇒ 这一跳的成败由回读判 ⛔ 不由写接口的返回判。
    #    ⚠️ 读不到（`None`）也走下面那一条（`_order_sta_token(None)` 是空串 ⇒ 不等于目标标签）：那正是「写后未能确认」那一档的人话 ⛔ 不要在这里为 `None` 另开一条分支（开了就得让原语知道自己是第几跳，而它没有那个信息——试过一版、当场把既有断言弄红）。
    try:
        confirmed = push.confirm_remote(client, remote_id)
    except ApiError as exc:
        return _retry(
            event,
            reason=_upstream_reason(STATUS_CONFIRM_READ_FAILED_REASON, exc),
            now=now,
        )
    if _order_sta_token(confirmed) != label:
        return _retry(event, reason=STATUS_CONFIRM_REASON, now=now)
    _finish(event, status="done", reason="", now=now)
    return "done"


def _consume_price(event, *, binding, client, http, now, line=None) -> str:
    """`member.price`：把我方发单价跟到下游那张单上（T3）。两种形态都要跟接单价那一格（业务方裁定，计划 §1 口径 6）：接单价 `price`（⭐ 永远要跟）：①式走 `jidanpu.change_order_price(remote_id, fee=hall_price)`（=①式那份 `write_price`）⇒ 回读钉值（`price == order.hall_price`）⛔ 不是「调用没抛异常就算完」（E-0087 同族：写接口不抛异常 ≠ 远端那一格真的变了）；⭐ ②式做不到回读钉值（它的单查里压根没有 `recFee` 那一格，真机实测）⇒ 那一条线把 `read_price` 留空（`None`）⇒ 引擎只以写端点的成功码为准+留一条写清「弱一档」的人话台账（见 `PricePrimitives.read_confirm_unavailable_reason`）；还挂在对方大厅（`link.in_hall`）⇒ 那一格的价由抛单价 `recFee` 承载、改 `price` 顶不掉它 ⇒ 再按当前价重抛一次 `jidanpu.throw_order(remote_id, fee=_throw_fee(order, binding))`（=①式那份 `rethrow_in_hall`；与 `_consume_launch` 同一个目标态、同一算式 ⇒ 可重入），再回读确认 `isThrowOrder == "Y"` 且 `recFee == 那个 fee`（钉值，不是只看状态位）。
    ⛔ 什么情况下都不改 `link.in_hall`：它记的是「对方大厅里此刻有没有这张单」这个事实，一次改价既没把单撤下来、也没把单抛上去（重抛是对已经在大厅的单改价，形态没变）⇒ 改它就是写一句假话（同 `_consume_assign` 既有口径）。⚠️ 可重入：同一事件重跑写的是同一个价、抛的是同一个价 ⇒ 幂等（与 `_consume_launch`「抛单重复推送无害」同一条）；⚠️ 上游失败/回读不符一律 `_retry`（既有退避+额度）⛔ 不静默吞、⛔ 不落 `done`；⚠️ 订单走默认 manager（同 `_consume_status_out`）：价是写给一张还在我方系统里的单的，软删的单不该再往上打（那种形态由 `member.withdraw` 收干净）。
    ⭐ 二期步骤 3：改价与回读收进 `line.price` 这一格（`PricePrimitives`）——「永远先写接单价那一格+回读钉值+还挂在对方大厅时再补一刀」这套判据仍然只有这一份，两条线的差别是「改的是哪一格/大厅那一档有没有对应动作」：①式的改价端点改的是对方的接单价 `price`，而「大厅那一档」的价由抛单价 `recFee` 承载 ⇒ 它两格都要动（`rethrow_in_hall` 就是后半段）；②式只有一个改价端点（`ModifyOrderPrice` ⇒ 改的是对方的 `recFee`，=建单 `assignPrice` 落的那一格）⇒「大厅那一档」没有对应动作——⛔ 不许静默少做：那时引擎记一条 `KIND_PRICE` 的人话台账（文案由那条线给，见 `PricePrimitives.hall_not_applicable_reason`），接单价照旧改了、事件照旧 `done`。"""
    line = line or member_outbox_line()
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    if order.hall_price is None:
        # ⛔ NULL 不是 0（沿用本文件既有的那条口径）：没价就没有可跟的东西，⛔ 不写 `"0"`（那等于告诉下家这单不要钱）；终态 `skipped`、⛔ 不重试。
        # ⛔ 不复用 `ASSIGN_NO_PRICE_REASON`：那句落点是「还没录单 ⇒ 等对账补录」，而这条事件是给已录进去的单改价、且对账不查价（计划 §5 残差 1）⇒ 照抄它就是在台账上写一句假话（见 `PRICE_NO_PRICE_REASON` 的注释）。
        return _skip(event, reason=PRICE_NO_PRICE_REASON, now=now)
    link = _link_for(binding, order)
    if link is None:
        if _recoverable_recordings(order.order_no).filter(payload__member_binding_id=binding.pk).exists():
            return "deferred"
        return _skip(event, reason=PRICE_NO_LINK_REASON, now=now)
    if link.withdrawn_at is not None:
        return _skip(event, reason=EDIT_WITHDRAWN_COPY_REASON, now=now)
    remote_id = link.jidanpu_order_id
    expected = Decimal(order.hall_price)
    price = _price_of(line)
    # ── ① 接单价那一格：永远要跟 ─────────────────────────────────────────────
    try:
        price.write_price(client, remote_id, expected, binding=binding)
    except ApiError as exc:
        return _retry(event, reason=_upstream_reason(PRICE_FAILED_REASON, exc), now=now)
    confirmed = None
    if price.read_price is None:
        # ⭐⭐ 这一条线的单查里没有那一格（②式：`GetOrderStatus` 的回包实测没有 `recFee`）⇒ 只能以写端点的成功码为准（`write_price` 没抛 = 对方回了成功码）。
        #    ⚠️ 如实登记：这比①式的「回读钉值」弱一档——①式能回读到 `price`/`recFee`，②式读不到（不是本引擎的疏忽，是对方的回包就那么 5 格）；⛔ 不许假装能回读（去读一个不存在的格 ⇒ `None != expected` 恒成立 ⇒ 每一次改价都停在「写后未能确认」、退避到 `abandoned`：真机实测就是这个形态）。
        #    ⛔ 也不许什么都不确认就 `done` 而台账上不说清楚 ⇒ 必须有一条人话台账（`read_confirm_unavailable_reason`，空 ⇒ 响亮抛：说不清为什么 = 本仓最恨的形态）。
        if not price.read_confirm_unavailable_reason.strip():
            raise ValueError(
                f"{line.name} 的回读里没有可钉的那一格，却没说清为什么（文案是空的）"
            )
        _note_order_once(
            binding=binding, order=order, kind=KIND_PRICE, status="done",
            reason=price.read_confirm_unavailable_reason,
        )
    else:
        try:
            confirmed = price.read_price(client, remote_id, binding=binding)
        except ApiError as exc:
            return _retry(
                event, reason=_upstream_reason(PRICE_CONFIRM_READ_FAILED_REASON, exc), now=now
            )
        # ⭐ **回读钉值**（⛔ 不是「调用没抛异常就算完」）：远端那一格真的等于我们要的价才算完成。
        #    ⚠️ 「读不出来」（`None`）与「读到别的价」在这一格**同一处置**（都不等于目标值）：
        #    那是原样 —— ①式那一份在远端没回 `price` 时回的也是 `None`（`_remote_amount_value`）。
        if _remote_amount_value(confirmed, "price") != expected:
            return _retry(event, reason=PRICE_CONFIRM_REASON, now=now)
    # ── ② 还挂在对方大厅那一档 ──────────────────────────────────────────────
    if not link.in_hall:
        return _finish_edit(event, now=now)
    if price.rethrow_in_hall is None:
        # ⭐ 这一条线**没有**这一档（②式：只有一个改价端点，没有「重抛改大厅那一格」这回事）
        #    ⇒ ⛔ **不许静默少做**：留一条人话台账说清「少做的是哪一档、为什么」，
        #    接单价照旧改了（上面那一段）、事件照旧 `done`。
        if not price.hall_not_applicable_reason.strip():
            raise ValueError(
                f"{line.name} 没有「大厅那一档」这个动作，却没说清为什么（文案是空的）"
            )
        _note_order_once(
            binding=binding, order=order, kind=KIND_PRICE, status="done",
            reason=price.hall_not_applicable_reason,
        )
        return _finish_edit(event, now=now)
    remote_hall = (confirmed or {}).get("isThrowOrder")
    if remote_hall == "N" or _is_remote_assignment_state(_order_sta_token(confirmed)):
        return _finish_edit(event, now=now, reason="下家副本已离开大厅，本次只同步接单价")
    if remote_hall != "Y":
        return _retry(event, reason="接单价已核对，无法确认下家副本仍在大厅，本次未重抛", now=now)
    reason = price.rethrow_in_hall(client, order, binding, remote_id)
    if reason is not None:
        return _retry(event, reason=reason, now=now)
    # ⛔ 到这里**不改** `link.in_hall`：重抛是对**已经在大厅**的那张单改价，形态没变。
    return _finish_edit(event, now=now)


def _finish_edit(event, *, now, reason="") -> str:
    updated = _finish(event, status="done", reason=reason, now=now)
    return "done" if updated else "deferred"


def _consume_project(event, *, binding, client, http, now, line=None) -> str:
    """`member.project`（触发点⑧）：把我方的「接手项目」发到下游那张单上。推的是哪一格、推给谁：`Order.project_text`（页面上叫「接手项目」，权威值）⇒ 对方那张单的 `project`（代练项目/标题）；推给已有链路且绑定启用中的那个远端单（`link.jidanpu_order_id`）；⛔ 没有链路 ⇒ 终态 `skipped`+人话（还没录进去的单，之后的录单自然带当前项目名，为它排一条事件只会得到一条永远 `skipped` 的噪声行，同 `_consume_price` 口径）。取值口径：消费时读订单的当前值（⛔ 不是排队那一刻的快照）——与 `_consume_price`/`_consume_status_out` 同一条既有口径：事件行里不存值 ⇒ 重复事件幂等（②式 `ModifyOrder` 写同一个项目名无害）、乱序由在飞去重收敛 ⇒ 终态总是「最后一次保存的那个值」 ⛔ 不会停在中间态。
    两个必填空格都为空 ⇒ 同一档处置（终态 `skipped`+一句人话，⛔ 不发空值烧额度）：那一发要两格——项目名（`project`）与游戏名（`gameName`，`ModifyOrder` 必填）⇒ 两格各自判一次空、处置刻意对称（⛔ 不一个 `skipped` 一个 `failed`）：项目名空 ⇒ `PROJECT_NO_PROJECT_REASON`（原有档）；⭐ 游戏名空 ⇒ `PROJECT_NO_GAME_NAME_REASON`（补上的那一档）：判据打在真正会发出去的那个值上（`order_game_name`，与②式发 `gameName` 同一个出处）——空值 ⇒ 对方必拒 ⇒ 退避重试到 `abandoned`（单测抓不到：假上游不校验这一格）。
    成败判据（本档比改价还弱一档，如实登记）：两条线都读不到这一格的回读（②式 `GetOrderStatus` 只有 5 格、①式那条路压根没有写接口）⇒ 只有「对方回了成功码」（`write_project` 没抛）这一条 ⇒ 由 `line.project` 那一格给出必需的那句人话，引擎每改一次写一条 `KIND_PROJECT` 台账行把它写清楚（⛔ 不许静默：不然台账上看起来像「确认过了」）。⚠️ 上游拒/回读缺失/网络失败一律 `_retry`（既有退避+额度）⛔ 不静默吞、⛔ 不落 `done`；⚠️ 订单走默认 manager（同 `_consume_price`）：项目名是写给还在我方系统里的单的，软删的单不该再往上打（那种形态由 `member.withdraw` 收干净）；⚠️ 不改链路行的任何一列：改项目名既没把单撤下来、也没把它抛上去（形态没变）。"""
    line = line or member_outbox_line()
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    project_text = order.project_text or ""
    if not project_text.strip():
        # ⛔ 空值不是「没改」：对方那一格也是必填（②式建单的 `project` 必填）⇒ 发空串只会被拒、
        #    白烧额度。终态 + 人话（补上之后再保存一次会再排一条 ⇒ 自愈），同 `PRICE_NO_PRICE_REASON`。
        return _skip(event, reason=PROJECT_NO_PROJECT_REASON, now=now)
    # ⭐⭐ 游戏名也是空的 ⇒ 同一档处置（终态 `skipped`+一句人话）。⛔ 不静默、⛔ 不写成 `failed`：②式那一发（`ModifyOrder`）的 `gameName` 是必填 ⇒ 发空串必然被对方拒 ⇒ 这一行会退避重试到烧完额度进 `abandoned`（⚠️ 单测抓不到：假上游不校验这一格）；与上面那一档刻意对称（理由与如实登记见 `PROJECT_NO_GAME_NAME_REASON` 的注释）。
    #    ⚠️ 判据打在真正会发出去的那个值上（`order_game_name` = ②式发 `gameName` 用的同一个出处，⛔ 不在这里另判一次 `order.game_id`：那既测不出「名字是空的」、也是第二份口径）。
    if not order_game_name(order).strip():
        return _skip(event, reason=PROJECT_NO_GAME_NAME_REASON, now=now)
    link = _link_for(binding, order)
    if link is None:
        if _recoverable_recordings(order.order_no).filter(payload__member_binding_id=binding.pk).exists():
            return "deferred"
        return _skip(event, reason=PROJECT_NO_LINK_REASON, now=now)
    if link.withdrawn_at is not None:
        return _skip(event, reason=EDIT_WITHDRAWN_COPY_REASON, now=now)
    remote_id = link.jidanpu_order_id
    project = _project_of(line)
    try:
        # ⚠️ `binding=` 显式给：这一发要读**绑定上的来源名**（整单覆盖那几格之一）；
        #    引擎手上本来就有它 ⇒ ⛔ 不让被调方再从链路查一次（那是一次白查，且两处判据会漂）。
        project.write_project(
            client, remote_id, project_text, order=order, binding=binding
        )
    except ApiError as exc:
        return _retry(event, reason=_upstream_reason(PROJECT_FAILED_REASON, exc), now=now)
    # ⭐⭐ 这一档没有回读可钉（两条线都一样：②式的单查没有那一格、①式压根办不了这一档）⇒ 只能以写端点的成功码为准。⛔ 不许假装能回读（去读一个不存在的格 ⇒ `None != 目标` 恒成立 ⇒ 每一次改项目都停在「写后未能确认」、退避到 `abandoned`：E-0087 同族形态），
    #    ⛔ 也不许什么都不确认就 `done` 而台账上不说清楚 ⇒ 必须留一条人话（空 ⇒ 响亮抛）。
    if not project.read_confirm_unavailable_reason.strip():
        raise ValueError(
            f"{line.name} 改项目那一跳没有可回读的那一格，却没说清为什么（文案是空的）"
        )
    _note_order_once(
        binding=binding, order=order, kind=KIND_PROJECT, status="done",
        reason=project.read_confirm_unavailable_reason,
    )
    return _finish_edit(event, now=now)


def _remote_amount_value(remote, field: str):
    """回读里某个金额字段的数值（`Decimal`）；缺键/`null`/非数值/布尔 ⇒ `None`。⚠️ 比对侧是 `Decimal`（我方金额一律 `Decimal`、两位小数出口）⇒ 收口这一个取值口，⛔ 不在判据处各写一次 `Decimal(str(...))`（两侧同一套归一化）；
    ⚠️ 远端回的是 JSON 数字（float，如 `44.44`），`str()` 给的是最短往返表示 ⇒ `Decimal("44.44") == Decimal("44.44")` 成立；⛔ 别用 `float == Decimal` 混比。"""
    raw = (remote or {}).get(field)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _order_sta_token(remote) -> str:
    """回读里 `orderSta` 的文本值（已去首尾空白）；键缺失/`None`/非字符串/空白 ⇒ 空串。⚠️ 判据与排障文案共用这一个取值口：判「是哪一档」（`_unlist_remote_verdict`）与把读到的值写进 reason（`_unlist_other_reason`）必须是同一个 token，
    否则两处会各读一次、各归一化一次而漂移（`AGENTS.md` §5-G「比对前两侧同一套归一化」同族）。"""
    raw = (remote or {}).get("orderSta")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _unlist_remote_verdict(remote) -> str:
    """对方那张单此刻是不是撤下抛单要的目标态（本仓代码里这一格的唯一判据出处）。⚠️ 调用方先确认了它已不在对方大厅（`isThrowOrder != "Y"`）——本函数判的是下一问：它是「我们撤下来了、停在未分配」，还是「对方抢先接走了」。⭐ 真机读数（原始输出只在**不进 git** 的 `.tools/recon/`）：撤下端点对已被接走的单无效、却仍回 `code=1「撤销成功」`、回读一个字没变 ⇒ 两套口径只有 `orderSta` 这一格能分辨。
    取值：`UNLIST_VERDICT_UNASSIGNED`=`orderSta == jidanpu.ORDER_UNASSIGNED_LABEL`（目标达成）；`UNLIST_VERDICT_TAKEN`=`orderSta == jidanpu.ORDER_IN_PROGRESS_LABEL`（对方接走了）；`UNLIST_VERDICT_UNREADABLE`=键缺失/`None`/非字符串/空白（判不了 ⛔ 不许当成功）；`UNLIST_VERDICT_OTHER`=读得到但既不是「未分配」也不是「代练中」（⛔ 也不许当成功）。"""
    token = _order_sta_token(remote)
    if not token:
        return UNLIST_VERDICT_UNREADABLE
    if token == jidanpu.ORDER_UNASSIGNED_LABEL:
        return UNLIST_VERDICT_UNASSIGNED
    if token == jidanpu.ORDER_IN_PROGRESS_LABEL:
        return UNLIST_VERDICT_TAKEN
    return UNLIST_VERDICT_OTHER


def _unlist_other_reason(remote) -> str:
    """`UNLIST_VERDICT_OTHER` 档的台账人话：既有那句+实际读到的 `orderSta` 值。⚠️ 为什么必须带上那个值：这一档的含义正是「读到了、但不是我们认识的任何一档」，⛔ 只写「不是目标态」等于把唯一的现场（到底读到了什么）丢掉——运维手上没有上游那一行，台账就是全部线索（与 `_upstream_reason` 同一条理由）；
    ⚠️ 长度：标签截到 60 字；整句（固定部分 ≈30 字+60）远在 `JidanpuSyncEvent.reason` 的 255 上限内，`_retry` 的 255 截断因此不会触发——⛔ 不靠那个兜底（它只截尾巴，会把「读到的是哪个档」这句话截掉一半）。"""
    return UNLIST_CONFIRM_REASON + UNLIST_CONFIRM_OTHER_SUFFIX.format(
        label=_order_sta_token(remote)[:60]
    )


def _settle_unlist_from_remote(event, remote, link, *, now) -> str:
    """按回读到的远端那一行落撤下抛单的终态（两条支路共用；判据只此一处）。目标态（不在厅+停在「未分配」）⇒ 改正链路标记+`done`（`in_hall=False` 是事实）；
    对方抢先接走（`代练中`）⇒ 改正链路标记+`skipped`+点名（⛔ 不静默、⛔ 不自动退单）；`orderSta` 读不出来/读得到但不是目标态 ⇒ `_retry`（⛔ 不当成功），且链路标记不动——与「还在大厅 ⇒ 重试」那一支同形：下一次消费还要再读一遍再判。"""
    verdict = _unlist_remote_verdict(remote)
    if verdict == UNLIST_VERDICT_UNREADABLE:
        return _retry(event, reason=UNLIST_STATE_UNREADABLE_REASON, now=now)
    if verdict == UNLIST_VERDICT_OTHER:
        # 读得到、但既不是「未分配」也不是「代练中」（例如已退单/已流转）：「不在目标态」不是「目标达成」⇒ 按未确认重试，链路标记留着下一次再判。
        # ⚠️ reason 里带上实际读到的那个标签值（`_unlist_other_reason`）：这一档的含义就是「读到了、但不是我们认识的任何一档」，光写「不是目标态」排障时看不出到底读到了什么。
        return _retry(event, reason=_unlist_other_reason(remote), now=now)
    # 到这里只剩两种**终态**判定，它们都以同一个事实为前提：它确实不在他们的厅里了。
    JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
        in_hall=False, last_out_at=now
    )
    if verdict == UNLIST_VERDICT_TAKEN:
        return _skip(event, reason=UNLIST_TAKEN_BY_PEER_REASON, now=now)
    _finish(event, status="done", reason="", now=now)
    return "done"


def _consume_unlist(event, *, binding, client, http, now, line=None) -> str:
    """`member.unlist`：把这张单从对方内部接单大厅撤下（⛔ 不是退单）。⭐ 业务方：我方接手（指派/在我方大厅自接）之后，对方那张单要停在「未分配」——不在他们的抢单大厅、也不指派给他们任何人；上游原语=`jidanpu.release_order_to_home`（候选与依据见那个常量的注释）。⚠️ 成功判据=回读到的远端那一行「不在厅 且 `orderSta == 未分配`」⛔ 不是「不在厅」。⭐ 为什么只有 `orderSta` 这一格能分辨（真机读数，原始输出只在**不进 git** 的 `.tools/recon/`）：该原语对已被对方接走的单无效、却仍回 `code=1「撤销成功」`、回读一个字没变 ⇒ 「回读 `isThrowOrder != "Y"` 就当撤下成功」会把「对方抢先接走」误报成成功；判据由 `_unlist_remote_verdict` 一处给出。⚠️ `orderSta` 读不出来（键缺失/`None`/非字符串）⇒ `_retry`：判不了 ≠ 目标达成（E-0004 同族）⛔ 不许当成成功；读回的是对方抢先接走（`代练中`）⇒ `skipped`+点名原因（`UNLIST_TAKEN_BY_PEER_REASON`）⛔ 不静默 `done`、⛔ 不自动退单（动钱口径，需业务方裁定）——本地链路标记照旧改正（它确实不在他们的厅里了）。
    ⚠️ 先读一遍、再写、再读一遍（三段式）：先读是承重的——本地 `in_hall` 只是标记、可能已过期（对方打手早把单接走了），对着一个已被接走的单调「撤销」可能把对方的指派毁掉 ⇒ `isThrowOrder != "Y"` 就一次写都不发，只把本地标记改正、并按回读落判据；再读是回读为准（照 `_consume_withdraw` 的形状）：写成功了但对方没生效 ⇒ 仍然 `failed` 重试，「读不到」不等于「已经撤下」。
    ⚠️ 如实登记的副作用（真机读数）：在生效那一档，本原语会把远端的发单价 `recFee` 清零。我方链路里无害：重新「发内部」由 `_consume_launch` 负责把价带回去（`jidanpu.throw_order(..., fee=_throw_fee(order, binding))` 现算 ⛔ 不依赖远端那个 0）。⚠️ 本波论证覆盖的范围（`AGENTS.md` §5-C ⛔ 不写未枚举的绝对句）——只证到「字段收敛」这一条路：远端 `recFee` 能进我方的那条字段路是进方向字段收敛（`jidanpu_converge`），它按 `origin_is_remote` 闸：我方自己录单推过去的单在 `_field_diffs` 里一格都不跟（`hall_price ← recFee` 就在那 6 格里）⇒ 这个 0 不经字段收敛回流到本地价；⚠️ 其余路径（对账/快路/人工录入/别的读回）本波未逐条枚举 ⇒ ⛔ 不能把这句推广成「本地价绝不会看到这个 0」。⛔ 本档既不自动退单、也不补价（动钱口径，需业务方裁定）。⚠️ 没有链路 ⇒ `skipped`+人话（从未录到对方系统，无从撤起）；链路已经标着不在大厅 ⇒ ⛔ 不白打上游、直接 `done`（⚠️ 那条早退不看 `orderSta`，本波未改、行为一字未动：缺口与命中条件登记在那个分支的注释上）。⚠️ 走默认 manager（⛔ 不是 `_consume_withdraw` 的 `Order.all_objects`）：软删的单在本线不会产生 `to_unlist`（`_shape` 先给 `absent` ⇒ 排的是退单那一档），所以遇到「取不到订单」只可能是在飞事件赶上了软删 ⇒ 那条退单才是要的清理、撤下是多余的 ⇒ 直接跳过即可 ⛔ 不为它绕过默认 manager（`tests/test_order_read_path_net.py` 的 `ALL_OBJECTS_SITES` 就是钉这件事的网）。"""
    order = Order.objects.filter(order_no=event.target_ref).first()
    if order is None:
        return _skip(event, reason=ORDER_MISSING_REASON, now=now)
    link = _link_for(binding, order)
    if link is None:
        return _skip(event, reason=UNLIST_NO_LINK_REASON, now=now)
    if not link.in_hall:
        # ⚠️ 本波未改这条早退（如实登记它的缺口 ⛔ 行为一字未动）：它不看 `orderSta`。① 为什么这条缺口窄：`to_unlist` 只在 `link.in_hall` 为真时才排（`_queue_unlist_if_in_hall` 的判据就是那一格）⇒ 要走到这里，必须是「排事件之后、消费者跑起来之前」本地那一格被别人改成假——即并发交错（例如同一张单上并发跑了一条 `member.withdraw`，`_consume_withdraw` 收尾时会 `in_hall=False`）；
        #    ② 命中时的后果：那一刻对方那张单可能已被对方打手接走（`orderSta=代练中`），而这里会把「对方已接走」当成「撤下目标已达成」、静默 `done`——正是上面刚修掉的那类误报，只是从这条更窄的入口进来（不写台账、无人看得见）；
        #    ⛔ 本波范围外：要修得先裁定「本地链路标记与远端事实谁先」的口径（谁有权把 `in_hall` 改成假、改了之后还要不要回读），本波只登记 ⛔ 不改行为、⛔ 不在这里补回读。
        _finish(event, status="done", reason="", now=now)
        return "done"
    remote_id = link.jidanpu_order_id
    try:
        before = jidanpu.fetch_order_by_key1(
            client, key=remote_id, expect_field="orderId", expect_value=remote_id
        )
    except ApiError as exc:
        return _retry(event, reason=_upstream_reason(UNLIST_READBACK_REASON, exc), now=now)
    if before is None:
        # ⚠️ 读不到 ≠ 「已经不在大厅」⇒ 判不了就不写（E-0004 同族）。
        return _retry(event, reason=UNLIST_READBACK_REASON, now=now)
    if str(before.get("isThrowOrder", "") or "") != "Y":
        # 对方那张单**早就不在大厅**（本地标记过期）⇒ ⛔ 一次写都不发，直接按回读落判据
        # （「对方接走了」这一档也要在这里点名，⛔ 不静默 `done`）。
        return _settle_unlist_from_remote(event, before, link, now=now)
    try:
        jidanpu.release_order_to_home(client, remote_order_id=remote_id)
    except ApiError:
        pass  # 业务失败（可能已经是目标态）⇒ 交给下面的回读判，⛔ 不解析文案
    try:
        confirmed = jidanpu.fetch_order_by_key1(
            client, key=remote_id, expect_field="orderId", expect_value=remote_id
        )
    except ApiError as exc:
        return _retry(event, reason=_upstream_reason(UNLIST_READBACK_REASON, exc), now=now)
    still_in_hall = confirmed is None or str(
        (confirmed or {}).get("isThrowOrder", "") or ""
    ) == "Y"
    if still_in_hall:
        return _retry(event, reason=UNLIST_CONFIRM_REASON, now=now)
    # 不在了 ⇒ 还要判它停在哪一档（我们撤下 vs 对方接走），见 `_settle_unlist_from_remote`。
    return _settle_unlist_from_remote(event, confirmed, link, now=now)


_HANDLERS = {
    KIND_LAUNCH: _consume_launch,
    KIND_ASSIGN: _consume_assign,
    KIND_WITHDRAW: _consume_withdraw,
    KIND_UNLIST: _consume_unlist,
    KIND_STATUS_OUT: _consume_status_out,
    KIND_PRICE: _consume_price,
    KIND_PROJECT: _consume_project,
    KIND_SOURCE_ORDER_NO: _consume_source_order_no,
}


# ══ 二·八、出向台账的差异面（⭐ 引擎只有一份，差异只有下面这几处）══════════════
# ⚠️ 本模块的处理逻辑（取活/退避/逐事件异常边界/六个 handler 的判据）只有一份；「下家①式（账号+密码登录）」与「下家②式（官方 Open Platform API）」各自提供一份 `OutboxLine`。⭐ 为什么必须是同一个引擎（②式收口时定）：推前回读、写后回读确认、六档 kind 的取链路口径、「读不到 ≠ 已达成」这几条判据是同一条口径，两处各写一份必然漂，而漂了之后的症状是静默写错对方那张单（本仓最恨的形态）。
# ⚠️ 形状照本仓既有的 `jidanpu_openapi_consume.CallbackLine`（「引擎一份、差异收进一个对象」）：差异只有 ①怎么取客户端、②`member.status_out` 那两跳怎么读怎么写、③哪些 kind 这条路还没通（⛔ 显式跳过+人话 ⛔ 不静默、⛔ 不当 `failed` 反复重试烧额度）。


@dataclass(frozen=True)
class StatusPushPrimitives:
    """`member.status_out` 那一档上游两跳的差异面（两条线各自实现，判据共用）。⚠️ `read_remote` 与 `confirm_remote` 是两条不同的契约 ⛔ 别合并成一个（合并过一次、当场把①式的判据改坏了：实测 `test_an_unreadable_pre_read_pushes_anyway` 从绿变红）——两次回读对「读不到」的处置本来就不同，那是业务口径：`read_remote(client, remote_order_id)`=推之前那次回读，回对方那一行原样的字典、读不到回 `None` ⇒ 调用方在 `None` 上照常推（⛔ 不能把「读不到」当成「远端已经是目标态」：①式的单查只查第一页+约 90 天窗 ⇒ 老单本来就可能永远读不到，而那些单恰恰是最需要推的）；⚠️ 「上游报错」与「读得到但没这一行」在这一格都收敛成 `None`，这是原样：①式那边 `fetch_order_by_key1` 抛的 `ApiError` 由引擎按 `STATUS_PRE_READ_FAILED_REASON` 记，回 `None` 则由引擎照常推——两条路的差别在引擎里 ⛔ 别挪进原语。
    `confirm_remote(client, remote_order_id)`=写之后那次回读，回同一个形状的字典；⭐ 读不到回 `None` ⇒ 引擎判「这一跳没达成」、记 `STATUS_CONFIRM_REASON`（写后未能确认）与退避重试（「读不到 ≠ 已达成」）；⚠️ 不是抛异常——试过那一版，它有个绕不过去的毛病：抛出来的 `ApiError` 要经过引擎的 `_upstream_reason(STATUS_CONFIRM_READ_FAILED_REASON, exc)`，而那一档文案是「回读那一跳上游报错」⇒ 台账会把「读得到、但没这一行」说成「上游报错」，而既有断言（`tests/test_jidanpu_member_consume.py::…not_confirmed…`）要的是「写后未能确认」；要在原语里换文案就得让原语知道自己是第几跳，那是第二个真相源。
    `write_remote(client, remote_order_id, status_label)`：把中文状态标签写过去；业务失败抛 `ApiError`（判据不解析它，只记人话原因+退避重试）。⚠️ 两个回读给出同一格的状态（①式 `orderSta`/②式 `data.status` 逐字放进 `orderSta`）——共享引擎只认 `_order_sta_token` 那一个取值口。"""

    read_remote: Callable[..., dict | None]
    confirm_remote: Callable[..., dict | None]
    write_remote: Callable[..., None]


# ── 认领之后该怎么办（`RecordPrimitives.adopted_verdict` 的返回值，⭐ 唯一出处）────────────
#: 认领到的远端单**此刻还挂在对方大厅**（①式：`isThrowOrder == "Y"`）⇒ 目标态已达成、
#: 只把链路补正成 `in_hall=True`。⛔ ②式**读不到**这一格（它的单查只回状态）⇒ ②式不可能返它。
ADOPTED_VERDICT_ALREADY_IN_HALL = "already"
#: 认领到的远端单已退单。⭐ D-0257（业务方拍板）：「撤单 → 上家退单 → 重抛」这条正常业务流必须能恢复
#: ⇒ 有 `RecordPrimitives.refund_undo` 这条腿的线（①式）先把对方那张单改回「未分配」再补抛；没有这条腿的线（②式）照旧终态跳过（`ADOPTED_REFUNDED_REASON`）。
ADOPTED_VERDICT_REFUNDED = "refunded"
#: 认领到的远端单**已被人接走 / 已流转** ⇒ 不补抛、不打扰对方，交给快路与回拉。
ADOPTED_VERDICT_NOT_UNASSIGNED = "other"
#: 认领到的远端单只是**未分配地躺在对方系统里** ⇒ 走「抛单那一跳」（①式真的补抛；
#: ②式那一跳没有对应接口 ⇒ 由 `throw_remote` 如实回一句「这一条线没有这个动作」）。
ADOPTED_VERDICT_NEEDS_THROW = "throw"
#: ⭐ D-0359 §11（⭐⭐ D-0362 放宽为单条件）：前置回读判「旧卡确实不在了 ⇒ 可全新建卡」（条件与依据见 `THROW_PREPARE_UNREADABLE_REASON` 那一段）。⚠️ 它只可能从 `RecordPrimitives.throw_prepare` 回——认领那一支看得到远端整行、永远判不出「查无」。
#: ⭐ 为什么是独立取值、不复用 `NEEDS_THROW`：`throw` 档的分派是拿当前这个远端单号直接去抛——查无档的旧单号已抛不动（对端没有那张卡，抛了又是一条失败、继续喂熔断），「先重录、再抛新单号」需要自己的分派支；
#: 而 `prepare_reason` 在分派里只被 `other` 档消费，挂在 `throw` 档上会被静默丢掉 ⇒ 既丢人话、也办不成事（`_consume_launch` 对本取值单独分派：拆旧锚 → 重走 `ensure_remote` 的既有全新录单路 → 照 `throw` 档抛新卡）。
THROW_PREPARE_VERDICT_RECREATE = "recreate"

# ── 「抛单那一跳」的三种结局（`RecordPrimitives.throw_remote` 的返回值）──────────────────
#: 目标态已达成（①式：抛单写成功**且**回读确认 `isThrowOrder == "Y"`；②式：本地链路已标着在厅里）。
THROW_OUTCOME_OK = "ok"
#: **可重试的失败**（上游拒绝 / 回读不符）⇒ 引擎按 `failed` + 退避重试收口。
THROW_OUTCOME_RETRY = "retry"
#: ⭐ **这一条线没有这个动作**（②式：官方 API 没有「把已经录进去的单抛进对方大厅」的接口）
#: ⇒ 引擎按**终态 `skipped`** + 人话收口（⛔ **不静默**、⛔ 不当成可重试的失败反复烧额度）。
THROW_OUTCOME_NOT_AVAILABLE = "not_available"


@dataclass(frozen=True)
class RecordPrimitives:
    """`member.launch`/`member.assign` 那一档上游那几跳的差异面（两条线各自实现，判据共用）。⛔ 判据一个字都不许搬第二份：本 dataclass 只提供上游怎么打，「订单在不在/有没有发单价/缺链路怎么办/认领之后该不该补抛/事件落什么终态」仍全部住在 `_consume_launch`/`_consume_assign` 那两个 handler 里。- `ensure_remote(event, order, binding, client, *, now, in_hall)`：⭐ 回 `(远端单号|None, 人话原因, 结局, 认领到的远端行|None)`，结局 ∈ `"ok"`/`"skipped"`（有意的「不动作」）/`"failed"`（预期失败，可重试）。这一格负责断点续跑的快速通道（payload 里的远端单号/本地链路/`recorded`）、地址级去重、录单前的挡重预读+认领、真的录一次、录完那一次回读确认；⚠️ ⭐ 远端单号从哪来由这一格的实现定（②式：建单回包的 `data`——真机实测 `GetOrderStatus` 的回包里没有 `orderId` ⇒ 「回读拿号」那条路不存在），引擎不碰这件事（只收 `remote_id`）⇒ 谁把「回读拿号」写进实现里，红的是那一条线自己的用例；⚠️ 认领那一支要自己把链路落下（照①式 `_ensure_remote_order` 的既有形状）：不落链路，那张单即使被认领了，日志快路与状态回拉依然看不见它；⚠️ `in_hall`=这一次录单动作的目标形态（`member.launch` ⇒ `True`、`member.assign` ⇒ `False`）：①式不用它（它的「进不进大厅」是下一跳 `throw_remote` 的事，录单本身只把人家的单建出来）⇒ ①式那份收到就丢；②式必须用它——文档明写「录单时自动发内部抢单大厅」的参数（`recUser`/`assignPrice`）只在录单那一刻可传；⛔ 不许在这一格里判「认领之后要不要补抛」——那是 `adopted_verdict` 那一格，且按远端此刻的真实状态判（认领来的单可能已退单/已被别人接走）。
    - `adopted_verdict(adopted)`：认领来的远端行 ⇒ 四个 `ADOPTED_VERDICT_*` 之一（⛔ 不认识的取值一律响亮抛：那是接线坏了，不是上游的错）。- `throw_prepare` 按单号读远端，返回 `(判定或 None, 原因, 远端行或 None)`。缺省没有该能力。
    - `throw_remote(client, order, binding, remote_order_id)`：回 `(THROW_OUTCOME_*, 人话原因)`；`RE` 档的原因必须是可重试的人话，`NOT_AVAILABLE` 档必须是说清「这一条线没有这个动作」的人话（引擎照写成终态 skip）。- `refund_undo(client, order, binding, remote_order_id)`：⭐ D-0257 细化——认领判定是「已退单」时先把对方那张单改回「未分配」的那一跳（管理端 `SetOrderSta`）；回 `(THROW_OUTCOME_*, 人话原因)`：`RETRY` ⇒ 引擎按可重试失败收口，`OK` ⇒ 引擎接着走 `throw_remote`（补抛）；⭐ 缺省 `None`=这一条线没有管理端改状态这条腿（②式）⇒ 引擎照旧终态 `skipped`（历史行为原样 ⛔ 不静默）。- `push_screenshots(*, order, binding, link, client, http, now)`：A 段（派单传图）；⭐ 缺省 `None`=这一条线不做这一件事（⛔ 引擎不静默：当场记一条人话台账，见 `_push_screenshots_of_line`）；②式就是这一档（它的传图是二期步骤 6）。"""

    ensure_remote: Callable[..., tuple]
    adopted_verdict: Callable[..., str]
    throw_remote: Callable[..., tuple]
    #: ⭐ D-0257 细化：「已退单」补抛前的**先改回「未分配」**那一跳
    #: （判据与取值约定见上面类 docstring 那一条；缺省 `None` = 这条线没有这条腿）。
    refund_undo: Callable[..., tuple] | None = None
    #: ⭐ D-0318：**重抛**（已在我方链路里的单）之前的前置回读 + 改状态判定。
    #: 返回判定、原因和按单号回读的远端行，缺省无该能力。
    throw_prepare: Callable[..., tuple] | None = None
    push_screenshots: Callable[..., object] | None = None


@dataclass(frozen=True)
class PricePrimitives:
    """提供改价、回读订单及写大厅价的通道。
    网页回读同时确认接单价和厅态；纯官方模式没有这份回读。
    """

    write_price: Callable[..., None]
    #: ⭐ 缺省 `None` = 这一条线的单查里**没有那一格**（②式）⇒ 以写端点成功码为准，见上面那一段。
    read_price: Callable[..., object] | None = None
    read_confirm_unavailable_reason: str = ""
    rethrow_in_hall: Callable[..., object] | None = None
    hall_not_applicable_reason: str = ""


@dataclass(frozen=True)
class WithdrawPrimitives:
    """撤单动作和回读。回读带上状态与是否收到明确无单的答复。
    absent_means_gone_reason 非空时保留查不到后的旧收口，确认标记单独用于日志。
    """

    refund_remote: Callable[..., None]
    read_remote: Callable[..., tuple[dict | None, bool]]
    absent_means_gone_reason: str = ""


@dataclass(frozen=True)
class ProjectPrimitives:
    """⭐ `member.project`（我方的「接手项目」⇒ 对方那张单的项目名称）那一档的差异面。⛔ 判据一个字都不许搬第二份：本 dataclass 只提供上游怎么打，「订单在不在/有没有链路/项目名是不是空的/什么算成」仍全部住在 `_consume_project` 那一个 handler 里。⚠️⚠️ 为什么这里没有 `read_project` 那一格（与 `PricePrimitives` 刻意不同）：`PricePrimitives` 有 `read_price`（①式能回读 `price`）；本档两条线都回读不到这一格——①式那条路压根没有这个写接口（登记在 `OutboxLine.unsupported`，见 `UPSTREAM_PROJECT_NOT_WIRED_REASON`）；②式的单查（`GetOrderStatus`）真机实测只有 5 格（`status`+四个时刻）——没有 `project` ⇒ 判据只剩「对方回了成功码」这一档（比「写后回读钉值」弱一档，如实登记）。
    ⛔ 不要为了「对称」造一个没有调用方的 `read_project`：那会写出一条没有任何用例覆盖的分支（本仓纪律：没有用例能红的判据不算判据）；将来哪条线真能回读（对方回包里加了那一格/①式那条路真接通了写接口），在那一份差异面上加这一格，并在同一个提交里补上「能红」的用例。
    所以这一格只有两样东西：- `write_project(client, remote_order_id, project_text, order=...)`：把项目名称发过去（⚠️ 两条线各自的端点与参数都不同：②式=`ModifyOrder`，它的 `gameName` 必填，见②式那份的 `write_project`；业务失败抛 `ApiError`，由 handler 翻成 `failed`+退避）。- `read_confirm_unavailable_reason`：⭐ 必需的那句人话（说清「为什么只能以写返回为准」+弱在哪）⇒ 引擎每改一次项目就写一条 `KIND_PROJECT` 台账行（同单同句去重）；⚠️ 与 `PricePrimitives.read_confirm_unavailable_reason` 同一条纪律：这一格是空串 ⇒ 引擎响亮抛（「什么都不确认就 `done`」而台账上不说清楚=本仓最恨的形态）。"""

    write_project: Callable[..., None]
    read_confirm_unavailable_reason: str = ""


@dataclass(frozen=True)
class OutboxLine:
    """一条绑定线在这个出向引擎里的全部差异面（⛔ 就这几格，别再往里加业务判据）。- `name`：这条线的短名（台账/人话里点名用）。- `spawn_client`：怎么拿到这条线自己的客户端（返回上下文管理器，产出 `(client, http|None)`）；⚠️ 这是本对象存在的首要理由：①式必须走 `jidanpu.member_session(binding)`（账号+密码建连、进程内复用会话），②式只能走 `jidanpu_openapi.openapi_client(binding)`（密钥直连）——②式绑定的账号密码是空的 ⇒ 拿①式那条路去建连只会在建连处抛 `ConfigError`（E-0202 形态）。- `status_push`：`member.status_out` 那两跳（见 `StatusPushPrimitives`）；- `record`：`member.launch`/`member.assign` 那一档（见 `RecordPrimitives`，二期步骤 3 追加）；⚠️ 与 `unsupported` 必须一致：一条线把这两个 kind 登记成「通了」却没给 `record` ⇒ 引擎响亮抛（⛔ 不许 `None.ensure_remote` 那种 AttributeError）；- `price`：`member.price` 那一档（见 `PricePrimitives`），同上的一致性纪律；- `withdraw`：`member.withdraw` 那一档（见 `WithdrawPrimitives`，二期步骤 4 追加）——⚠️ D-0249（修 E-0246）改准：②式的「撤下大厅」不再由这一格办（历史注：此前经 `converged_kinds` 收敛到这里=改已退单，那条收敛路已摘——在「接手人自己的绑定」上退掉的是接手人自己店里的活单），今天这一格只办退单；- ⭐ `project`：`member.project` 那一档（见 `ProjectPrimitives`）；⚠️ ①式没有这一格 ⇒ 它把这一档登记进 `unsupported`，⛔ 两条登记必须一致（登记成「通了」却不给这一格=接线坏了 ⇒ 引擎响亮抛）。
    - `converged_kinds`（可选，默认 `None`=没有收敛）：⭐ 「这条线上某几个 kind 收敛成同一个动作」的登记（`{kind: 真正办它的那个 kind}`）；命中时共享引擎只换 handler ⛔ 事件行的 `kind` 一个字都不改（台账要能分辨当初是哪一档；历史例：②式曾把 `member.unlist` 收敛到 `member.withdraw`，⭐ D-0249 起那份登记已摘——见 `unlist_unavailable_reason` 那格与 `_converged_kind` 的注释）；⚠️ ①式不设这一格（它的「撤下」与「退单」是两件事）⇒ 行为逐字不变；⛔ 指到一个没登记的 kind 上=接线坏了。- `unsupported`：这条线还没通的 kind → 一句人话原因（⛔ 空串/缺项都算接线坏了）；命中 ⇒ 共享引擎按终态 `skipped` 收口+把这句话写进台账（⛔ 不静默、⛔ 不写成 `failed`——那会按退避表反复重试、白烧上游额度）。
    - ⭐⭐ `hall_push_unreachable_reason`（可选，默认 `None`=可达）：这条线没有「把已有链路的单推进对方抢单大厅」这个动作时的那句人话；⚠️ 它是「期望形态」那一侧的脸：②式下这个动作根本不存在（`recUser`/`assignPrice` 只在建单那一刻可传）⇒ 对账不该把这一档排出来（排出来必然被消费侧终态跳过，每 180 秒一轮 ⇒ 死循环）；⛔ 它与 `record.throw_remote` 是同一件事的两面：那一面在消费时把结局如实回成 `THROW_OUTCOME_NOT_AVAILABLE`+同一句话（本格取值就是那句话，由用例钉住逐字相等）。- ⭐⭐ `unlist_unavailable_reason`（可选，默认 `None`=有这个原语）：这条线没有「不毁单的撤下」原语时的那句人话（⭐ D-0249 修 E-0246）；非空 ⇒ 这条线上 `member.unlist` 一律终态 `skipped`、一次上游都不打：分派拦截（`_consume`）与对账收口（`reconcile` 不排 `to_unlist`）两处判据都读这一格（取数口 `_unlist_unavailable_reason` ⛔ 别写第二份）；⛔ ②式为什么必须非空：官方 API 没有撤下接口，而「退单」落点会毁掉接手人自己店里的活单（E-0246 现场）；「停在未分配」的期望形态在②式不可达，可达的期望形态=留在对方大厅、未分配、活着（对账那侧据此不排，与 `hall_push_unreachable_reason` 那条收口成对）；⛔ ①式不设（它有真撤下原语 `release_order_to_home` 四跳 ⇒ 行为逐字不变）。- `enabled`（可选，默认 `None`=恒开）：⭐ 这条线自己的开关开没开（二期步骤 2 硬要求：②式那条分派默认关着）；回 `False` ⇒ `run_outbox` 一次上游都不打、一个字节都不写（连健康列都不动），只回一句人话（`blocked_reason`）；⚠️ 开关取值怎么读留在那条线自己那里（②式那份读环境变量）⛔ 不把「哪个环境变量名」写进本引擎；①式不设（⇒ 恒开、行为逐字不变）。- `blocked_reason`：`enabled` 回 `False` 时那句人话（进 `OutboxStats.blocked_reason`）；⚠️ 缺省空串=「这条线没有开关」（①式就是它）⇒ ⛔ 一旦有人配了 `enabled` 却忘了这句话，`run_outbox` 会响亮抛（说不清为什么没跑=本仓最恨的形态）。"""

    name: str
    spawn_client: Callable[..., object]
    status_push: StatusPushPrimitives
    unsupported: Mapping[str, str]
    #: ⭐ 二期步骤 3 追加的两格（`member.launch`/`assign` 与 `member.price` 的上游几跳）。⚠️ 缺省 `None`=这条线没有这一格 ⇒ 命中它时 `_record_of`/`_price_of` 响亮抛
    #: （⛔ 不许 `None.ensure_remote` 那种 AttributeError：那会把「接线漏了一格」伪装成「未预期异常」）。①式与②式今天都填了这两格。
    record: RecordPrimitives | None = None
    price: PricePrimitives | None = None
    #: ⭐ 二期步骤 4 追加的一格（`member.withdraw` 的上游两跳）。
    #: ⚠️ **缺省 `None` = 这一条线没有这一格** ⇒ 命中它时 `_withdraw_of` **响亮抛**
    #: （⛔ 不许 `None.refund_remote` 那种 AttributeError）。①式与②式今天**都填了**。
    withdraw: WithdrawPrimitives | None = None
    #: ⭐ 一格（`member.project` 的上游一跳，见 `ProjectPrimitives`）。⚠️ 缺省 `None`=这条线没有这一格 ⇒ 命中它时 `_project_of` 响亮抛（⛔ 不许 `None.write_project` 那种 AttributeError）；
    #: ②式填了它；①式没填、并把这一档登记进 `unsupported`（两条登记必须一致，⛔ 别一边说通了、一边不给格）。
    project: ProjectPrimitives | None = None
    #: ⭐ 二期步骤 4 追加：这条线上**收敛成同一个动作**的 kind 登记（见类 docstring）。
    #: ①式不设（`None`）= 没有收敛，行为逐字不变。
    converged_kinds: Mapping[str, str] | None = None
    #: ⭐⭐ 「把已经有链路的单推进对方抢单大厅」这一档不可达时的那句人话（缺省 `None`=可达 ⇒ ①式形态，行为逐字不变；见类 docstring 那条）。
    #: ⚠️ 只在对账（排队）读它：不可达的期望压根不进差集（`reconcile` 那条收口）——⛔ 不是「排出来、失败了再靠终态标记兜」。
    hall_push_unreachable_reason: str | None = None
    #: ⭐⭐ D-0249（修 E-0246）：这条线没有「不毁单的撤下」原语时的那句人话（缺省 `None`=有 ⇒ ①式形态，行为逐字不变；见类 docstring 那条）。非空 ⇒ 这条线上 `member.unlist` 一律终态 `skipped`、一次上游都不打——分派拦截（`_consume`）与对账收口（`reconcile` 不排 `to_unlist`）两处判据都读这一格（取数口 `_unlist_unavailable_reason` ⛔ 别写第二份）。
    #: ⛔ ②式为什么必须非空：官方 API 没有撤下接口（当时那版「撤下收敛成退单」会在「接手人自己的绑定」上把接手人自己店里的活单退掉，E-0246 现场：34 张）；「停在未分配」在②式不可达，可达的期望形态=留在对方大厅、未分配、活着（对账那侧据此不排——否则每 180 秒重排一条注定 skip 的事件，churn 不可接受）。
    #: ⛔ ①式不设（它有真撤下原语 `release_order_to_home` 四跳）。
    unlist_unavailable_reason: str | None = None
    enabled: Callable[[], bool] | None = None
    blocked_reason: str = ""
    #: ⭐ 出向降频/每轮封顶/468 冷却（业务方拍板）。`True` 只有下家①式（真实登录、被雷池按频率拦的那条线）；②式不受影响。
    #: 打开后：两条出向事件之间至少隔 `JIDANPU_MIN_REQUEST_INTERVAL_MS`；一轮最多推 `JIDANPU_OUTBOX_MAX_PER_ROUND` 条；468 ⇒ 先换下一个出口重试、池里全试完仍 468 ⇒ 该店冷却（指数翻倍、封顶 30 分钟）。
    rate_limit: bool = False
    uses_openapi: bool = False


def _member_read_remote(client, remote_order_id: str) -> dict | None:
    """①式（登录）那条路：推之前那次回读；读不到回 `None`（⭐ 照加差异面之前的原样）。⚠️ 逐字搬自 `_consume_status_out` 原来内联的那一次调用：`key1` 的命中集是「`orderId` 精确 OR `orderIdView` 子串匹配」（`fetch_order_by_key1` 的实测语义）⇒ 必须指定要比的字段与期望值（⛔ 不许拿 `count == 1` 当「查到了」）；
    ⚠️ 这里⛔ 不许把 `None` 翻成异常（本函数第一版就是那么写的，当场把①式的判据改坏）：`fetch_order_by_key1` 回 `None` 表示「这一页里没有这张单」，而①式口径是照常推（老单本来就可能永远读不到，而那些单恰恰最需要推）——见 `StatusPushPrimitives`。"""
    return jidanpu.fetch_order_by_key1(
        client, key=remote_order_id, expect_field="orderId", expect_value=remote_order_id
    )


def _member_read_withdrawal(client, remote_order_id: str) -> tuple[dict | None, bool]:
    return _member_read_remote(client, remote_order_id), False


def _member_confirm_remote(client, remote_order_id: str) -> dict | None:
    """①式（登录）那条路：写之后那次回读；⭐ 读不到回 `None`（判据在引擎里）。⚠️ 与 `_member_read_remote` 逐字同一个调用（含「上游抛错就让它抛」）：两次回读在①式这边本来就是同一件事，差别只在引擎拿到 `None` 之后怎么判（推前 ⇒ 照常推；写后 ⇒ 记「写后未能确认」+退避）；
    ⛔ 把那个差别挪进原语=让原语知道自己是第几跳，而它没有「第几跳」这个信息（试过一版、当场把既有断言弄红）。"""
    return _member_read_remote(client, remote_order_id)


def _member_write_remote(client, remote_order_id: str, status_label: str) -> None:
    """①式（登录）那条路：把中文状态标签写过去（`POST /OrderAjax/SetOrderSta`）。⚠️ 逐字搬自 `_consume_status_out` 原来内联的那一次调用（⛔ 一个参数都没动）：
    `remark` 走默认空串——共享引擎这一档历来不传 remark（退单那一档有自己的逐字原因）。"""
    jidanpu.set_order_status(client, remote_order_id=remote_order_id, status_label=status_label)


def _member_refund_remote(client, remote_order_id: str) -> None:
    """①式（登录）那条路：把这张单改成「已退单」（`POST /OrderAjax/SetOrderSta`）。⚠️ 逐字搬自 `_consume_withdraw` 原来内联的那一次调用（⛔ 一个参数都没动）：`remark` 走 `jidanpu_common.WITHDRAW_REMARK`（spec §13-T6 定下来的逐字原因，①式退单历来带它）——⛔ 别在这里改成不带（那是②式那份的形态，两条线这一格本来就不一样，见 `WithdrawPrimitives`）；
    ⚠️ 写后回读那一跳复用 `_member_read_remote`（与加差异面之前逐字同一个调用：`fetch_order_by_key1`+`orderId` 精确匹配）。"""
    jidanpu.set_order_status(
        client, remote_order_id=remote_order_id,
        status_label=jidanpu.ORDER_REFUNDED_LABEL,
        remark=jidanpu_common.WITHDRAW_REMARK,
    )


# ── ①式的「录单那几跳」（⭐ 逐字搬自 `_consume_launch` / `_ensure_remote_order` 的既有实现）──


def _member_ensure_remote(event, order, binding, client, *, now, in_hall):
    """①式：几跳照旧全在 `_ensure_remote_order` 里（⛔ 本函数一个判据都不加）。⚠️ `in_hall` 收到就丢（⛔ 不是漏了）：①式的录单不带任何「进不进大厅」的信息——文档里那两个参数是②式独有的；①式那一侧「进大厅」是下一跳 `throw_order`（`throw_remote`）
    ⇒ 本函数把这一格显式标注为不使用 ⛔ 不假装用它（谁要改①式的行为，改的是 `_ensure_remote_order` 与 `_consume_launch` ⛔ 不是这里）。"""
    return _ensure_remote_order(event, order, binding, client, now=now)


def _member_adopted_verdict(adopted) -> str:
    """①式：认领到的远端单该怎么走（原 `_adopted_throw_verdict` 的逐字转接）。⭐ D-0257 改准（业务方拍板）：「已退单」这一档不再「不碰」——历史保守裁定（真机实测那张就是已退单，怕盲目补抛把一张已退单重新挂进对方大厅）改走补抛支（先改回「未分配」，再补抛）。分类判据只在 `_adopted_throw_verdict`。"""
    return _adopted_throw_verdict(adopted)


def _member_throw_remote(client, order, binding, remote_order_id) -> tuple[str, str]:
    """①式：抛单那一跳（`throw_order`+回读确认 `isThrowOrder == "Y"`）。⚠️ 逐字搬自 `_consume_launch` 里那一段（⛔ 调用顺序与文案一个字都没动）：
    写 → `fetch_order_by_key1` 回读 → 判 `isThrowOrder`；原因文案分别是 `THROW_FAILED_REASON`/`THROW_CONFIRM_REASON`（后两档共用同一句，那是原样）。"""
    try:
        jidanpu.throw_order(
            client, remote_order_id=remote_order_id, fee=_throw_fee(order, binding)
        )
    except ApiError as exc:
        return THROW_OUTCOME_RETRY, _upstream_reason(THROW_FAILED_REASON, exc)
    try:
        confirmed = jidanpu.fetch_order_by_key1(
            client, key=remote_order_id, expect_field="orderId", expect_value=remote_order_id
        )
    except ApiError as exc:
        return THROW_OUTCOME_RETRY, _upstream_reason(THROW_CONFIRM_REASON, exc)
    thrown = confirmed is not None and str(
        (confirmed or {}).get("isThrowOrder", "") or ""
    ) == "Y"
    if not thrown:
        return THROW_OUTCOME_RETRY, THROW_CONFIRM_REASON
    return THROW_OUTCOME_OK, ""


def _member_throw_prepare(client, order, binding, remote_order_id, *,
                          rearmed=False, withdrawn=False) -> tuple[str | None, str, dict | None]:
    """①式：重抛之前先读一遍远端那张单，按它此刻的真实状态给判定（⭐ D-0318）。业务方口径（原话）：「已退单的状态先改成未分配然后重抛单就行了」——本函数只做「读+判」；改状态那一跳仍由 `RecordPrimitives.refund_undo`（`_member_refund_undo`）办、分派在 `_consume_launch`（判据只有那一份 ⛔ 不在这里再写一遍）。为什么要读（⛔ 不能凭本地标记猜）：本地链路只记「在不在厅」（`in_hall`）与「退成过没有」（`withdrawn_at`），没有远端 `orderSta` 那一格；而「已退单」「代练中」「已完成」都可能是 `in_hall=False` 的形态 ⇒ 不读就分不出「能直接抛」与「必须先改/不许碰」；业务方实测：已退单直接抛会被对方拒（「发单失败，可能这些订单已设定了接手人…」）。
    判定（与认领那一支同一个出处 `_adopted_throw_verdict`）：`already`=`isThrowOrder=Y`（还挂在他家大厅）⇒ 目标态已达成 ⛔ 不重抛（重复抛单幂等）；`refunded`=`orderSta=已退单` ⇒ 先改回「未分配」再抛；`other`=不是「未分配」（代练中/已完成/已流转…）⇒ ⛔ 不改状态、⛔ 不抛，终态跳过（⚠️ 人话里点名读到的 `orderSta`，同 `_unlist_other_reason` 的理由：台账是排障的唯一现场；业务方明令 ⛔ 不许对「对方已有真实接手人且正在做单」的单做任何自动改状态）；`throw`=只是未分配地躺着 ⇒ 直接抛（⛔ 不做多余的改状态）；⭐ `recreate`（D-0359 §11 起；⭐⭐ D-0362 放宽为单条件）=查无此单 ⇒ 旧卡确实不在了，可全新建卡。
    回读失败（⭐ D-0318 与 E-0004 的关系）：上游报错（`ApiError`：超时/5xx/解析不出/验签失败等含糊失败）⇒ `(None, THROW_PREPARE_UNREADABLE_REASON + 上游原话)`：本轮不抛、走既有退避（+计熔断燃料，回归钉在 `tests/test_jidanpu_no_order_newcard.py`）；⚠️ 「读不到」≠「可以照抛」（E-0004 同族）：一次没读到的空被当成「对方没这张单」是最难查的形态——含糊的空到现在还是这个口径（D-0362 口径③：判不出的按坏了处理）；逐字「查无此单」一律走重建：人工重抛落 `THROW_PREPARE_RECREATE_REASON`、其余落 `THROW_PREPARE_RECREATE_AUTO_REASON`。防循环保护与挂起期闸都不在本函数（它们是「照不照判定行动」的处置闸，判据与分派只有 `_consume_launch` recreate 支那一份——每（订单，绑定）自动新建上限 1 次、已用再遇转人工、挂起期拦截不新建 ⛔ 不在这里写第二份）。"""
    try:
        remote = jidanpu.fetch_order_by_key1(
            client, key=remote_order_id,
            expect_field="orderId", expect_value=remote_order_id,
        )
    except ApiError as exc:
        return None, _upstream_reason(THROW_PREPARE_UNREADABLE_REASON, exc), None
    if remote is None:
        # ⭐⭐ D-0362：逐字查无 = 单条件放行（历史口径与防循环/挂起处置闸的分工见 docstring；
        #    ⛔ 这里只判「事实」，不判「要不要行动」）。
        if rearmed and withdrawn:
            # §11 人工经典档（人工重抛 + 退成过）：人话照旧（既有用例逐字钉这句）。
            return THROW_PREPARE_VERDICT_RECREATE, THROW_PREPARE_RECREATE_REASON, None
        return THROW_PREPARE_VERDICT_RECREATE, THROW_PREPARE_RECREATE_AUTO_REASON, None
    verdict = _adopted_throw_verdict(remote)
    if verdict == ADOPTED_VERDICT_NOT_UNASSIGNED:
        # ⚠️ 标签截到 60 字（照 `_unlist_other_reason` 的长度口径）：整句远在 `reason` 的 255
        #    上限内，⛔ 不靠那个兜底截断（它只截尾巴，会把「读到的是哪个档」截掉）。
        label = _order_sta_token(remote)[:60] or "读不出来"
        return verdict, THROW_PREPARE_TAKEN_REASON.format(label=label), remote
    return verdict, "", remote


def _member_refund_undo(client, order, binding, remote_order_id) -> tuple[str, str]:
    """①式：把认领到的「已退单」先改回「未分配」，为补抛开路（⭐ D-0257 细化）。业务方实测：上家平台对已退单的单不能直接抛单 ⇒ 补抛必须先走这一跳（管理端 `POST /OrderAjax/SetOrderSta`，`changeSoureType=20` 由 `jidanpu.set_order_status` 必带）。⚠️ `order`/`binding` 是差异面的统一形状（与 `throw_remote` 同参），这一跳用不到；`remark` 走默认空串（照 `_member_write_remote` 那档既有口径——这类改状态历来不带 remark，退单那档的逐字原因是它自己的特例）。
    ⚠️ 被拒/异常 ⇒ `(RETRY, REFUND_UNDO_FAILED_REASON + 上游原话)`：诚实可见、走既有退避（到上限 abandoned），⛔ 不静默、⛔ 不写成终态 skip（那会把这条恢复路又堵死，E-0256）；
    ⚠️ 改回不带写后回读：紧随其后的 `throw_order`+回读 `isThrowOrder=Y` 就是对整条链的确认——若这一跳被上游静默吞掉，补抛那跳会被拒 ⇒ 照样落失败原因，依然可见。"""
    try:
        jidanpu.set_order_status(
            client, remote_order_id=remote_order_id,
            status_label=jidanpu.ORDER_UNASSIGNED_LABEL,
        )
    except ApiError as exc:
        return THROW_OUTCOME_RETRY, _upstream_reason(REFUND_UNDO_FAILED_REASON, exc)
    return THROW_OUTCOME_OK, ""


def _member_write_price(client, remote_order_id: str, expected, *, binding=None) -> None:
    """①式：改对方的**接单价**（`POST /OrderAjax/ChangeOrderPrice`）——逐字搬自 `_consume_price`。"""
    jidanpu.change_order_price(client, remote_order_id=remote_order_id, fee=expected)


def _member_read_price(client, remote_order_id: str, *, binding=None) -> dict | None:
    return jidanpu.fetch_order_by_key1(
        client, key=remote_order_id, expect_field="orderId", expect_value=remote_order_id,
    )


def _member_rethrow_in_hall(client, order, binding, remote_order_id):
    """①式：还挂在对方大厅 ⇒ 按新价重抛一次并回读钉价（`isThrowOrder`+`recFee`）。⚠️ 逐字搬自 `_consume_price` 的后半段（⛔ 顺序与文案一个字都没动）：重抛 → 回读 → 判 `isThrowOrder == "Y"` → 判 `recFee` 等于刚算的那个价；
    ⛔ 不改 `link.in_hall`：重抛是对已经在大厅的那张单改价，形态没变。"""
    fee = _throw_fee(order, binding)
    try:
        jidanpu.throw_order(client, remote_order_id=remote_order_id, fee=fee)
    except ApiError as exc:
        return _upstream_reason(THROW_FAILED_REASON, exc)
    try:
        confirmed = jidanpu.fetch_order_by_key1(
            client, key=remote_order_id, expect_field="orderId", expect_value=remote_order_id
        )
    except ApiError as exc:
        return _upstream_reason(THROW_CONFIRM_REASON, exc)
    thrown = confirmed is not None and str(
        (confirmed or {}).get("isThrowOrder", "") or ""
    ) == "Y"
    if not thrown:
        return THROW_CONFIRM_REASON
    if _remote_amount_value(confirmed, "recFee") != fee:
        return PRICE_THROW_FEE_CONFIRM_REASON
    return None


#: ①式那一份差异面（⭐ 每一格都逐字等于加 `OutboxLine` 之前的行为）。
_MEMBER_LINE = OutboxLine(
    name="下家①式（账号 + 密码登录）",
    spawn_client=jidanpu.member_session,
    status_push=StatusPushPrimitives(
        read_remote=_member_read_remote,
        confirm_remote=_member_confirm_remote,
        write_remote=_member_write_remote,
    ),
    record=RecordPrimitives(
        ensure_remote=_member_ensure_remote,
        adopted_verdict=_member_adopted_verdict,
        throw_remote=_member_throw_remote,
        #: ⭐ D-0257 细化：①式有管理端改状态这条腿 ⇒ 认领到「已退单」时
        #: 先改回「未分配」再补抛（两步的分派在 `_consume_launch` 那一支里）。
        refund_undo=_member_refund_undo,
        #: ⭐ D-0318：**重抛**（已在我方链路里的单）之前先读远端状态，
        #: 「已退单」⇒ 走同一条 `refund_undo` 腿再抛（业务方口径：先改未分配再重抛）。
        throw_prepare=_member_throw_prepare,
        #: A 段传图**①式是实现好的**（当时那一轮）⇒ 逐字挂既有那一个函数。
        push_screenshots=_push_order_screenshots_to_downstream,
    ),
    #: ⭐ 二期步骤 4：①式的退单那一跳逐字是原来 `_consume_withdraw` 内联的那两次调用（写带逐字原因 `remark`、回读 `fetch_order_by_key1`）⇒ 行为一个字都没变。
    #: ⚠️ ①式的撤下（`member.unlist`）不走这一格：它是另一件事（释放回「未分配」），由 `_consume_unlist` 自己那四跳办 ⇒ 本线不设 `converged_kinds`。
    withdraw=WithdrawPrimitives(
        refund_remote=_member_refund_remote,
        read_remote=_member_read_withdrawal,
    ),
    price=PricePrimitives(
        write_price=_member_write_price,
        read_price=_member_read_price,
        #: ⭐ ①式**有**「大厅那一档」（再抛一次改 `recFee`）⇒ 这一格不是 `None`。
        rethrow_in_hall=_member_rethrow_in_hall,
    ),
    #: 六档仍然全通。第七档 `member.project` ①式办不了，登记在这里：
    #: 终态 `skipped` + 一句人话，不静默、不写成 `failed` 反复重试。
    unsupported={KIND_PROJECT: UPSTREAM_PROJECT_NOT_WIRED_REASON},
    #: ⭐ ①式走**降频 / 每轮封顶 / 468 冷却**（对端雷池按频率拦的那条线）。
    #: ②式那一份**保持缺省 `False`** ⇒ 行为逐字不变。
    rate_limit=True,
)


def member_outbox_line() -> OutboxLine:
    """①式那一份差异面（`run_outbox`/`_consume_status_out` 的缺省取值）。⚠️ 缺省取①式是承重的：`jidanpu_sync` 那条在跑的调度每 3 秒调一次 `run_outbox(binding)`，⛔ 缺省值一旦变成「按 `binding.mode` 猜」，②式那条还没接线的路就会被静默走到（E-0202 家族：两处判据=迟早漂）；
    ⚠️ 本函数恒回①式那一份（上面那条判据一字未改）；「这一条绑定该用哪一份」是另一个问题，由 `outbox_line_for(binding)` 回答（它只服务对账那一侧，见那个函数）。"""
    return _MEMBER_LINE


def outbox_line_for(binding) -> OutboxLine:
    """这一条绑定该用哪一份出向差异面（①式/②式各一份）——⭐ 对账那一侧的取用口。为什么必须有它（开闸前堵的那个环）：`reconcile_and_queue` 的调用方之一「PUT 启用那一刻的补推」不看 mode，①式行与②式行走同一个入口；缺省若恒取①式，②式那一行就会拿①式的能力面去算期望 ⇒ 排出一条这条线永远办不到的动作（②式没有「把已有单推进对方大厅」的接口）⇒ 每 180 秒一轮又排一条（终态 `skipped` 只挡重试 ⛔ 挡不住重排）；⭐ E-0202 同族：新路沿用旧路的判据，症状是静默错。
    判据只有一处：`jidanpu_openapi.is_openapi_binding(binding)`（`mode` 是唯一判据 ⛔ 不看 `client_id` 真值——E-0021 家族）；⛔ 本函数不读环境变量、不看凭据，只做「取哪一份」。与 `member_outbox_line()` 那条「⛔ 别按 mode 猜」的关系（⛔ 别读混）：那条管消费（`run_outbox`/`_consume_*` 的缺省，生产上①式每 3 秒一拍，缺省恒①式是对的，②式由它自己的分派层显式传）；本函数管对账（`reconcile`/`reconcile_and_queue`）：它的生产调用方里有一处两种 mode 共用 ⇒ 「按 mode 取对应那一份」在那一处才是唯一正确的缺省；⚠️ 两处都只有一个判据来源（`mode`）⛔ 不存在第二套口径。
    ⚠️ 函数内 import（两处环，照 `_push_screenshots_of_line` 那处的同一处置）：`jidanpu_openapi` 在模块级 import `jidanpu_upstream`，而后者又 import 本模块；`jidanpu_member_openapi_outbox` 同样在模块级 import 本模块 ⇒ 两者都只能在函数内 import。"""
    from apps.business import jidanpu_member_openapi_outbox, jidanpu_openapi

    if jidanpu_openapi.is_openapi_binding(binding):
        return jidanpu_member_openapi_outbox.member_openapi_outbox_line()
    return _MEMBER_LINE


def _unsupported_reason(line: OutboxLine, kind: str) -> str | None:
    """这一条线在 `kind` 上「还没通」的人话原因；通了 ⇒ `None`。⚠️ 判据只有一处（`line.unsupported`）⇒ ①式（空表）走这里恒回 `None`，行为一个字不变；
    ⛔ 空串不算「有人话」：那种接线只会让台账上出现一条说不清为什么的跳过（本仓最恨的形态）⇒ 响亮 `ValueError`（那是写这条线的人漏了文案，不是上游的错）。"""
    reason = line.unsupported.get(kind)
    if reason is None:
        return None
    if not reason.strip():
        raise ValueError(f"{line.name} 的 {kind} 登记成「还不支持」却没给一句人话原因")
    return reason


def _unlist_unavailable_reason(line: OutboxLine) -> str | None:
    """这一条线「没有不毁单的撤下原语」那句人话；有 ⇒ `None`（①式，行为逐字不变）。⭐⭐ 这是 `member.unlist` 处置的唯一取数口（D-0249 修 E-0246）：分派拦截（`_consume`）与对账收口（`reconcile` 的 `to_unlist` 那支）两处判据都读它——⛔ 别在任何一个调用点直接摸 `line.unlist_unavailable_reason` 另写一份（两处判据迟早漂）；
    ⛔ 与 `_unsupported_reason` 同一条纪律：非 `None` 却是空串=「说不清为什么不办」⇒ 响亮抛（那是接线漏了文案，不是上游的错）。"""
    reason = line.unlist_unavailable_reason
    if reason is None:
        return None
    if not reason.strip():
        raise ValueError(f"{line.name} 声明 member.unlist 不可用，却没给一句人话原因")
    return reason


def _record_of(line: OutboxLine) -> RecordPrimitives:
    """这一条线「录单那几跳」的差异面；⭐ `unsupported` 说它通了却没给这一格 ⇒ 响亮抛。⚠️ 存在的理由（`AGENTS.md` §5-E「防御性写法不要写成承重逻辑」的反面）：这条是承重的——少了它，接线漏一格会以 `None.ensure_remote` 那种 `AttributeError` 的形式炸在逐事件异常边界里
    （台账上只剩一句「未预期异常（AttributeError）」），而真实成因是「写这条线的人忘了给 `record`」。"""
    if line.record is None:
        raise ValueError(
            f"{line.name} 把「派单录单 / 指派录单」登记成通了，却没给 record 这一格"
        )
    return line.record


def _price_of(line: OutboxLine) -> PricePrimitives:
    """这一条线「改价」的差异面；⭐ 与 `_record_of` 同一条一致性纪律。"""
    if line.price is None:
        raise ValueError(f"{line.name} 把「改价」登记成通了，却没给 price 这一格")
    return line.price


def _withdraw_of(line: OutboxLine) -> WithdrawPrimitives:
    """这一条线「退单」的差异面；⭐ 与 `_record_of` 同一条一致性纪律。⚠️ 文案里**只有「退单」**（D-0249 起这一格不再兼办撤下，见 `WithdrawPrimitives`）。"""
    if line.withdraw is None:
        raise ValueError(
            f"{line.name} 把「退单」登记成通了，却没给 withdraw 这一格"
        )
    return line.withdraw


def _project_of(line: OutboxLine) -> ProjectPrimitives:
    """这一条线「改项目名称」的差异面；⭐ 与 `_record_of` 同一条一致性纪律。⚠️ 它只在「这一条线把 `member.project` 登记成通了」时才会被调到：`_consume` 先查 `line.unsupported`（①式把这一档登记在里面 ⇒ 终态跳过 ⛔ 根本走不到这里）；
    ⇒ 这条检查挡的是接线漏了一格（有人把 `unsupported` 那一条删掉、却忘了给 `project`）——那种形态不检查的话会以 `None.write_project` 的 `AttributeError` 炸在逐事件异常边界里，
    台账上只剩一句「未预期异常（AttributeError）」，真实成因看不出来。"""
    if line.project is None:
        raise ValueError(f"{line.name} 把「改项目名称」登记成通了，却没给 project 这一格")
    return line.project


def _converged_kind(line: OutboxLine, kind: str) -> str:
    """这一条线上 `kind` 真正由哪一个 handler 办（⭐ 收敛登记的唯一取数口）。⭐ 只换 handler ⛔ 事件行的 `kind` 一个字都不改：台账要能分辨当初是哪一档（收敛过的两条痕必须各自可追溯）；
    ⚠️ 判据只有一处（`line.converged_kinds`）⇒ 今天两条线都不设这一格（⭐ D-0249 修 E-0246：②式那份 `{member.unlist: member.withdraw}` 的收敛已摘——在「接手人自己的绑定」上收敛成退单会把接手人自己店里的活单退掉；②式的 unlist 现在由 `unlist_unavailable_reason` 那格在分派处终态跳过）⇒ 本函数对任何 kind 都恒回它自己，行为逐字不变；
    ⛔ 别在 `_HANDLERS` 上另开一张「②式的表」：两处判据迟早漂。"""
    return (line.converged_kinds or {}).get(kind, kind)


def _push_screenshots_of_line(line: OutboxLine, *, order, binding, link, client, http, now
                              ) -> str | None:
    """A 段（派单传图）：交给这一条线自己那一格；⭐ 没给这一格 ⇒ 记一句人话 ⛔ 不静默。⚠️ 为什么不能直接什么都不做：①式那份的 A 段实现（`_push_order_screenshots_to_downstream`）对「调用方没把 HTTP 层带下来」是响亮抛的（那是接线漏了）——②式的 `spawn_client` 第二格本来就是 `None`（它没有登录会话，也就没有上传页）⇒ 照搬那一条会让每一张有待传截图的②式派单炸在未预期异常上；而不传图这件事必须留痕（业务方口径：派单时全传；没传是「这条线还没做」而不是「没有图」）；
    ⇒ 判据只有一处：`chosen` 非空时记一条 `KIND_ASSIGN` 的 `done` 台账（同一句话只写一条），本函数回那句人话（与①式那一格的返回值语义一致）；
    ⛔ 不挡派单：本函数永不抛（①式那一格的 `http is None` 例外只在①式自己的实现里）。"""
    pusher = _record_of(line).push_screenshots
    if pusher is not None:
        return pusher(
            order=order, binding=binding, link=link, client=client, http=http, now=now
        )
    # 函数内 import：`jidanpu_upstream_pics` 在模块级 import `jidanpu_upstream`，而后者又
    # import 本模块 ⇒ 模块级 import 会成环（照 `_push_order_screenshots_to_downstream` 那处）。
    from apps.business import jidanpu_upstream_pics

    if not jidanpu_upstream_pics.choose_screenshots(order, img_types=MEMBER_PUSH_IMG_TYPES):
        return None
    reason = PUSH_PIC_NOT_WIRED_REASON.format(line=line.name)
    _note_order_once(
        binding=binding, order=order, kind=KIND_ASSIGN, status="done", reason=reason,
    )
    return reason



def _existing_remote_order(client, order, *, source_order_no=None) -> dict | None:
    """对方系统里已经存在这张单吗（按来源单号回读）？是则回那一整行。⚠️ 这是「认领」（adoption）那一层的取数：不解析上游报错文案，直接回读；读失败（`ApiError`）向上抛，由调用方翻译成「失败可重试」——⛔ 不许把「读不到」当成「对方没有」（那会重复录一张真单）；
    ⚠️ 回整行（不是只回 `orderId`）：认领之后还要按远端此刻的真实状态决定要不要补抛（见 `_adopted_throw_verdict`）——真机实测那张单已成为「已退单」，当时的裁定是盲目补抛等于把一张已退单重新挂进对方大厅；⭐ D-0257 改准：这一档改走补抛支（先改回「未分配」再抛），而「被人接走/已流转」仍要靠整行才判得出来、照样不碰——「回整行」这个形状因此不变；
    ⚠️ 查的键是有效来源单号（`effective_source_order_no`，唯一出处）：对方把它逐字落在 `orderIdView` 上（`jidanpu.OWN_SOURCE_VALUE_FIELD`，T19 实测）。"""
    key = source_order_no or effective_source_order_no(order)
    return jidanpu.fetch_order_by_key1(
        client, key=key,
        expect_field=jidanpu.OWN_SOURCE_VALUE_FIELD, expect_value=key,
    )


def _adopted_throw_verdict(remote: dict) -> str:
    """认领到的远端单要不要补抛（唯一判据出处）。回 `"already"`（已在他家大厅）/`"refunded"`（已退单）/`"other"`（已被人接走或已流转）/`"throw"`（只是未分配地躺在对方系统里 ⇒ 补抛=我们原本要做的那个动作）。⚠️ 认领修的是「对方已经有这张单、我们却永远停在待抢单」，但它绝不允许盲目补抛：这张单在我们不知道的时候可能已被对方退单、或被对方打手接走了（真机实测那张就是已退单）⇒ 当时的保守裁定是「只有『未分配、没进过他家大厅』才补抛，其余一律不碰对方那张单，交给日志快路与状态回拉去对齐（链路行已经在认领那一刻落下了，它们看得见）」；
    ⭐ D-0257 改准（业务方拍板）：「撤单 → 上家自动退单 → 重抛」是正常业务流，上面的历史裁定把这条恢复路也堵死了（E-0256）⇒ 「已退单」档改走补抛支：①式先把它改回「未分配」（`RecordPrimitives.refund_undo`）再补抛；
    被人接走/已流转（`other`）维持不碰（不是业务方要的形态，历史裁定对它继续成立）。"""
    if str(remote.get("isThrowOrder", "") or "") == "Y":
        return "already"
    if str(remote.get("orderSta", "") or "") == jidanpu.ORDER_REFUNDED_LABEL:
        return "refunded"
    if map_remote_status(remote.get("orderSta")) != OrderStatus.UNASSIGNED:
        return "other"
    return "throw"


def _throw_fee(order, binding):
    """本次抛给对方的抛单价 = 我方发单价 × 该绑定的百分比（业务方裁定）。⚠️ 百分比是每个代练员自己那一行上的列（默认 100=不打折），范围 1–100 由序列化器与模型校验器两头卡（⛔ 0 会把免费单真送进对方大厅，见 `LAUNCH_NO_PRICE_REASON` 同族口径）；⚠️ 四舍五入到分（`ROUND_HALF_UP`，金额的常规口径）⛔ 不用 float；
    ⚠️ `or 100` 是兜底（列非空、默认 100，正常路径到不了）：校验器只在 `full_clean()` 里跑，而 `save()`/`queryset.update()` 不跑它 ⇒ shell 写库那种 0 会走到这里。方向是刻意选的：回落到 100（不打折）而不是让 0 生效——0 等于把一张免费单真送进对方大厅。"""
    percent = Decimal(int(binding.throw_price_percent or 100))
    fee = (Decimal(order.hall_price) * percent / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return fee


def _cached_input_order_kefu(order, binding, client) -> str:
    """这一行记住的「下家录入客服 uid」；没有就去取一次并存回绑定行，取不到回空串。⭐ 业务方裁定（选 C）：录单表单的 `inputOrderKefu` 填绑定账号自己在对方店铺里的员工 uid（=下家录单页的默认行为：`isSelectCurrentUser=Y` 时那一行带 `selected`）；⛔ 字段要的是 uid、不是姓名文本；也⛔ 不许再把 `ADD_ORDER_TEMPLATE` 里那个合成占位值 `'100'` 发出去（真机实测：那个 uid 在下家的客服名单里不存在，对方那张单的客服就是空的——正是业务方报的「空的订单」）；
    ⚠️ 缓存落在绑定行上（`input_order_kefu`）：取一次就够 ⛔ 不许每张单多打一次上游列表（那会让「录一批单」变成 N 次额外请求）；换绑（改账号/改地址）时由 PUT 清掉；
    ⚠️ 取不到（认不出/接口挂了/形状变了）⇒ 回空串，由调用方不填那个键+留一条人话台账（⛔ 不静默、⛔ 更不乱填一个 uid——填错人是不可逆的）。"""
    cached = str(getattr(binding, "input_order_kefu", "") or "").strip()
    if cached:
        return cached
    try:
        uid = jidanpu.own_staff_uid(client)
    except JidanpuError:
        # ⚠️ 「接口挂了」与「认不出当前账号」**分开记**在 reason 里没必要：两者对这张单的效果
        #    一样（不填 + 人工核对），而文案已经把两种成因都点了名。
        uid = ""
    if not uid:
        _note_order_once(
            binding=binding, order=order,
            reason=INPUT_ORDER_KEFU_UNRESOLVED_REASON, status="failed",
        )
        return ""
    JidanpuMemberBinding.objects.filter(pk=binding.pk).update(input_order_kefu=uid)
    binding.input_order_kefu = uid
    return uid


class RecordingRefused(Exception):
    pass


def _ensure_remote_order(event, order, binding, client, *, now, check_link=None, timing=None, raise_recording_refusal=False):
    """确保对方系统里录过这张单，回 `(远端订单号|None, 人话原因, 结局, 认领到的远端行|None)`；结局 ∈ `"ok"`/`"skipped"`（有意的「不动作」）/`"failed"`（预期失败，可重试）；第 4 项只在「认领」那一支非空（`None`=这张单不是认领来的、是这次真录下去的）——调用方（抛单那一档）要靠它按远端真实状态决定要不要补抛（见 `_adopted_throw_verdict`）。
    ⚠️ 可断点续跑，判重键是 `payload["jidanpu_order_id"]` 与 `payload["recorded"]`（⛔ 不是「有没有链路行」——录单成功与建链路行之间还有一次网络往返）：录单一成功就先把 `recorded` 落库，取号里的任何失败都不许让这次录单被遗忘（否则重试会往对方系统再灌一张真单）；⚠️ 地址级去重（裁定 3）：同一 `jidanpu_base_url` 下任何绑定（含已停用的）已有指向这张我方单的链路行 ⇒ 本次不录（`skipped`）——最早那份补停用后，另一份会把同一张单再录一遍；
    ⚠️ 录单载荷的基底来自 `jidanpu_common` 的两个公共 builder（裁定 6）⛔ 不在这里抄第二份；⚠️ 本线只在这份自己的表单上覆盖四个键——`price`（下家的接单价=我方发单价）、`orderSoures`（来源值）、`souresOrderID`（有效来源单号，见 `effective_source_order_no`）与 `inputOrderKefu`（下家录入客服 uid，见 `_cached_input_order_kefu`；取不到时摘掉这个键）——⛔ 公共 builder 服务的是另一条线，不改它。"""
    payload = dict(event.payload or {})
    penalty_record = _is_penalty_record(event, order)
    if penalty_record and not _penalty_record_still_current(event, order, binding):
        return None, ASSIGN_STALE_REASON, "skipped", None
    own = _link_for(binding, order)
    if own is not None:
        return own.jidanpu_order_id, "", "ok", None
    remote_id = str(payload.get("jidanpu_order_id") or "")
    if remote_id:
        if penalty_record:
            remote_id, reason, outcome = _readback_remote_id(
                event, order, client, check_link=check_link, expected_remote_id=remote_id,
            )
            if remote_id is None:
                return None, reason, outcome, None
        elif check_link is not None:
            check_link(remote_id)
        link, _ = _ensure_link(binding, order, remote_id=remote_id, in_hall=False, now=now)
        if link is None:
            return None, LINK_OCCUPIED_REASON, "skipped", None
        return link.jidanpu_order_id, "", "ok", None
    if payload.get("recorded"):
        remote_id, reason, outcome = _readback_remote_id(event, order, client, check_link=check_link)
        if remote_id is None:
            return remote_id, reason, outcome, None
        link, _ = _ensure_link(binding, order, remote_id=remote_id, in_hall=False, now=now)
        if link is None:
            return None, LINK_OCCUPIED_REASON, "skipped", None
        return link.jidanpu_order_id, "", "ok", None
    if JidanpuMemberOrderLink.objects.filter(
        binding__jidanpu_base_url=binding.jidanpu_base_url, order=order
    ).exists():
        return None, ADDRESS_DUPLICATE_REASON, "skipped", None

    wanted = (binding.source_name or "").strip() or SOURCE_NAME_DEFAULT
    hits = jidanpu.match_sources(jidanpu.fetch_sources(client), wanted)
    if len(hits) != 1:
        return None, RECORD_SOURCE_REASON, "failed", None
    # ⭐ 真机实测补的一层自愈：对方已经有这张单（同一个来源单号）⇒ 认领它 ⛔ 不再录第二张。真实成因（本机实测踩到）：录单成功但链路行没落成（进程中断/台账被重置/对方有人手工录过同一来源单号）⇒ 不认领的话，重试永远被对方按「来源单号重复」拒掉，而那一单在我方永远停在待抢单（快路认不出对方那张单）；
    #    ⛔ 不靠解析上游那句「已存在相同来源订单号」来判断——那是文案、会变；直接回读；⚠️ 「读不到」与「不存在」不是一回事：读失败 ⇒ 响亮失败重试 ⛔ 不当作没有；
    #    ⚠️ 失败原因用本支自己的文案（`RECORD_PRE_READ_REASON`）：这一跳发生在录单之前，写「录单后回读不到」会让排障的人以为单已经录过去了（错根因比没有根因贵）。
    source_order_no = payload.get("recording_source_order_no") or effective_source_order_no(order)
    try:
        adopted = _existing_remote_order(client, order, source_order_no=source_order_no)
    except ApiError as exc:
        return None, _upstream_reason(RECORD_PRE_READ_REASON, exc), "failed", None
    if adopted:
        if penalty_record:
            if not _penalty_record_still_current(event, order, binding):
                return None, ASSIGN_STALE_REASON, "skipped", None
            if reason := _penalty_record_remote_conflict(order, adopted):
                return None, reason, "skipped", None
        adopted_id = str(adopted.get("orderId", "") or "")
        if payload.get("assign_replaced_remote_id"):
            return None, ASSIGN_SOURCE_OCCUPIED_REASON, "failed", None
        if not adopted_id:
            # 回读到了行却没有订单号 ⇒ 与「回读失败」同档：⛔ 不许当成「对方没有」去重录。
            return None, RECORD_PRE_READ_REASON, "failed", None
        if check_link is not None:
            check_link(adopted_id)
        # ⭐ 链路行在认领那一刻就落下（`in_hall`=远端此刻的真实值）：日志快路与状态回拉都是按链路表找单的 ⇒ 不落这一行，那张单即使被认领了，快路/回拉依然看不见它（真机形状：对方打手已经接了，我方却永远停在待抢单）；
        #    ⚠️ 抛单成功之后消费者会用 `_ensure_link` 把 `in_hall` 补正（那一支已存在则更新）。
        claimed_link, _created = _ensure_link(
            binding, order, remote_id=adopted_id, now=now,
            in_hall=str(adopted.get("isThrowOrder", "") or "") == "Y",
        )
        if claimed_link is None:
            # ⭐⭐ **A 族撞键（E-0315）**：这条远端单在本店已被另一张我方单的链路占着（多为已退款老单）
            #    ⇒ 建不了链=认领不成立，终态收口（⛔ 不再响亮抛——那会让同来源新单的每次认领都烧满重试、又被对账重排，生产死循环）。
            return None, LINK_OCCUPIED_REASON, "skipped", None
        payload = dict(event.payload or {})
        payload["jidanpu_order_id"] = adopted_id
        _persist_payload(event, payload)
        return adopted_id, "", "ok", adopted
    if penalty_record and timezone.localdate(order.created_at) < (
        timezone.localdate(now) - timedelta(days=jidanpu.ORDER_LIST_LOOKBACK_DAYS)
    ):
        return None, PENALTY_RECORD_EXPIRED_LOOKUP_REASON, "skipped", None
    source_value = str(hits[0].get("value", ""))
    form = jidanpu_common.build_add_order_form(order)
    # ⚠️ 来源值进**表单**的 `orderSoures`（本机捕获的表单输入键；顶层那份由 builder 带）。
    form["orderSoures"] = source_value
    # ⭐ 业务方：来源单号用我方「来源单号」（空白才回落内置单号）——唯一出处是 `effective_source_order_no`。⚠️ 公共 builder `build_add_order_form` 按租户级那条线自己的口径写的是 `order_no`，本线在这份自己的表单上覆盖它（⛔ 不去改那个公共 builder：它服务的是另一条线）；
    #    ⚠️ 三处必须同一个值：表单 `souresOrderID`、`add_order` 的顶层参数（它写进顶层 `souresOrderID`）、以及录单后的回读（`_readback_remote_id`）。
    form["souresOrderID"] = source_order_no
    # ⭐ ⑤ 录单表单的「接单销售客服」（业务方 裁定）：填**我们这个下家账号自己**
    #    在对方店铺里的员工 uid；取不到就**把这个键摘掉**（⛔ 不发模板那个合成占位值 `'100'`，
    #    ⛔ 也不乱填一个 uid），并要求留一条人话台账（`_cached_input_order_kefu` 里写）。
    kefu_uid = _cached_input_order_kefu(order, binding, client)
    if kefu_uid:
        form["inputOrderKefu"] = kefu_uid
    else:
        form.pop("inputOrderKefu", None)
    # ⭐ 业务方裁定：下家的接单价 = 我方的发单价（`Order.hall_price`）。⛔ 不许把我方的客户价（`total_price`）/接单价暴露给对方租户——那是我们的底价，对另一个租户泄露一次就收不回来；⚠️ 只覆盖本线这一份表单：`build_add_order_form` 是租户级那条线的公共 builder（它按自己的口径取 `total_price`）⛔ 不去改它；
    #    ⚠️ 这里借 `jidanpu_common._money_text`（两位小数字符串，与订单页金额键同形）是有意的：本模块没有自己的金额格式化，抄第二份会让「两位小数」这个事实有两个出处；公开它的名字要动租户级那条线的公共模块（`jidanpu_hall` 里 6 处调用），本线不为此扩面；
    #    ⚠️ 发单价为空（`NULL`，指派那一路不要求它有值）时落到 `"0"`：刻意不去回落到 `total_price`——那会把我们的底价送给另一个租户；落 `"0"` 至多是「价没同步过去」，由下家自己在单子上补；泄价是不可逆的。⚠️ 与公共 builder 自己的回落写法同形。
    form["price"] = jidanpu_common._money_text(order.hall_price) or "0"
    if event.kind == KIND_ASSIGN and not payload.get("repush"):
        current = (_penalty_record_still_current(event, order, binding) if penalty_record
                   else _assign_still_current(order, binding))
        if not current:
            return None, ASSIGN_STALE_REASON, "skipped", None
    payload["recording_source_order_no"] = source_order_no
    payload["recording_store"] = binding.jidanpu_base_url
    _set_event_progress(event, payload)
    try:
        with timing("record") if timing else nullcontext():
            result = jidanpu.add_order(
                client, form_payload=form,
                top_level=jidanpu_common.build_add_order_top_level(
                    order, source_value=source_value
                ),
                source_order_no=source_order_no,
            )
    except ApiError as exc:
        if raise_recording_refusal and type(exc.code) is int and exc.code == 0 and exc.status_code == 200:
            message = exc.body.get("msg") if isinstance(exc.body, dict) else None
            raise RecordingRefused(
                message.strip() if isinstance(message, str) and message.strip() else "鸡蛋铺拒绝录单，请核对订单资料。"
            ) from exc
        return None, _upstream_reason(RECORD_FAILED_REASON, exc), "failed", None
    payload["recorded"] = True
    if (payload.get("assign_replaced_remote_id") or payload.get("direct_recording_owner_id")) and result.remote_oid is not None:
        payload["recording_remote_oid"] = result.remote_oid
    _set_event_progress(event, payload)
    with timing("record_readback") if timing else nullcontext():
        remote_id, reason, outcome = _readback_remote_id(event, order, client, check_link=check_link)
    if remote_id is not None:
        # ⭐ 同上：**录单成功后就把链路落下**（此刻它还在对方系统里未分配、没进过大厅 ⇒
        #    `in_hall=False`），这样即使紧随其后的那次抛单失败（甚至烧完重试额度），
        #    这张单对日志快路与状态回拉依然是**可见**的（⛔ 不会再出现「永远停在待抢单」）。
        recorded_link, _created = _ensure_link(
            binding, order, remote_id=remote_id, in_hall=False, now=now
        )
        if recorded_link is None:
            # ⭐ E-0315：同 A 族（实践中到不了：新录单号不该与本店已有链路撞）；
            #    防御钉：终态收口，⛔ 不许 None 往下流。注意此时远端**已录出一张真卡**、
            #    payload 断点已落（`jidanpu_order_id`）⇒ 人话核对后由人工/迁链处置。
            return None, LINK_OCCUPIED_REASON, "skipped", None
    return remote_id, reason, outcome, None


def _readback_remote_id(event, order, client, *, check_link=None, expected_remote_id=None):
    """录单后的独立一步：按有效来源单号回读，拿到对方的 `orderId` 并落进 payload。
    ⚠️ 查的键与录单时发出去的 `souresOrderID` 必须是同一个值（唯一出处 `effective_source_order_no`）：写的是它、回的也是它（远端逐字落在 `orderIdView` 上）。"""
    key = (event.payload or {}).get("recording_source_order_no") or effective_source_order_no(order)
    payload = dict(event.payload or {})
    try:
        if payload.get("assign_replaced_remote_id"):
            expected_oid = payload.get("recording_remote_oid")
            if expected_oid is None:
                return None, RECORD_READBACK_REASON, "failed"
            found = _find_repair_order(
                client, order, payload["assign_replaced_remote_id"], now=timezone.now(),
                source_order_no=key, any_remote_id=True, remote_oid=expected_oid,
            )
        else:
            found = jidanpu.fetch_order_by_key1(
                client, key=key, expect_field=jidanpu.OWN_SOURCE_VALUE_FIELD, expect_value=key,
            )
    except ApiError as exc:
        return None, _upstream_reason(RECORD_READBACK_REASON, exc), "failed"
    remote_id = str((found or {}).get("orderId", "") or "")
    if not remote_id:
        # ⛔ 空 `orderId` 同样算回读失败：写 `""` 会让下一次又满足「没录过」⇒ 重录。
        return None, RECORD_READBACK_REASON, "failed"
    if _is_penalty_record(event, order):
        if expected_remote_id is not None and remote_id != expected_remote_id:
            return None, PENALTY_RECORD_REMOTE_ID_CONFLICT_REASON, "failed"
        if reason := _penalty_record_remote_conflict(order, found):
            return None, reason, "failed"
    if check_link is not None:
        check_link(remote_id, found)
    _persist_payload(event, {**(event.payload or {}), "jidanpu_order_id": remote_id})
    return remote_id, "", "ok"


# ── 一轮 outbox（调度层逐绑定调它一次）────────────────────────────────────────

#: 出向台账串行闸的命名空间（advisory lock 的第一个 key）。两个 32 位 key = `(本常量, binding.pk)` ⇒ 同一绑定互斥、不同绑定互不阻塞；
#: ⚠️ 取值没有业务含义，但改它等于换一把锁：滚动发布时新老进程会各锁一把、闸门那一小段时间失效 ⇒ 只随发版一起生效，⛔ 不要按环境/按租户改。
OUTBOX_LOCK_NAMESPACE = 0x4F555442  # "OUTB"（outbox 的缩写），纯为可读性

#: 拿不到串行闸时回给调用方的人话（写进 `OutboxStats.blocked_reason`）。⚠️ 两个调用方都会看到它：web 端点（逐单 `failed` 的原因）与常驻调度（健康列）；⚠️ 措辞刻意不诱导重点（复评 Minor-3）：这张单已经进了队列，下一轮调度（①式每 3 秒一拍）会把它推出去 ⇒ 说「请稍后再试」会让用户以为这次点失败了、再点一次（多排一条事件/多一轮上传）——说「已排队+几秒后自动推」才是真话；
#: ⚠️ 语义残差（如实登记）：逐单结果档位仍记 `failed`（契约只有 `done`/`skipped`/`failed` 三档 ⛔ 不为它新增第四档）；严格说这一档是「排队中」而不是「失败」——端点把这句话原样回给页面，用户看到的不是一句失败理由。
OUTBOX_BUSY_REASON = "已排队：另一轮推送正在跑，几秒后会自动推出去"

# ── 出向降频/每轮封顶/468 冷却（业务方拍板）────────────────────
# 背景：下家出向曾被对端 WAF 整店拦成 HTTP 468（「请求频率过高」），换了一个代理 IP 仍被限流 ⇒ 光换 IP 不够，必须降频；且业务方要求「先换代理、都不行最后才停」。口径是全局一套配置（⛔ 不做按店单独配置），只作用于下家①式出向（`OutboxLine.rate_limit`）。

#: 两条下家①式出向之间至少隔多少毫秒（默认 **500ms**；`0` = 关闭降频）。
OUTBOX_MIN_REQUEST_INTERVAL_MS_DEFAULT = 500
#: 一轮最多推多少条（默认 100）；第 K+1 条留到下一轮（`OutboxStats.capped`）。⭐ D-0347（业务方拍板）定为 100：peer 人类抢单已改「请求内同步直推」（不再进本队列），队列里剩下的全是后台批量（订单/截图/大厅事件/机器路径的投递），
#: 20 条的封顶在多租户并发下排空太慢（下一轮=秒级延迟拖成十秒级）；env `JIDANPU_OUTBOX_MAX_PER_ROUND` 仍可按机覆盖（`_outbox_env_int` 读法不变）。
OUTBOX_MAX_PER_ROUND_DEFAULT = 100
#: 468 冷却的基数（默认 **5 分钟**）；**再被拦翻倍**，封顶 30 分钟。
OUTBOX_COOLDOWN_BASE_SECONDS = 300
OUTBOX_COOLDOWN_MAX_SECONDS = 1800

#: 两条出向请求的**上一次放行时刻**（`monotonic`；全局，⛔ 不分店）。
#: ⚠️ 进程内状态：拆成多个进程后各进程各算一份（与出口池的本机状态同一条代价）。
_OUTBOUND_LAST_AT: float | None = None
_OUTBOUND_RATE_LOCK = threading.Lock()
#: 可注入的时钟 / 睡眠（用例断「真的隔了 N」时替换这两格，⛔ 不真的睡）。
_outbound_monotonic = time.monotonic
_outbound_sleep = time.sleep

#: 连续进入冷却的次数（键 = `<schema>:<binding.pk>`）；成功一次即清零。
#: ⚠️ 冷却截止时刻本身存在 `jidanpu_proxy` 的共享状态里（设了状态文件就跨进程可见）。
_OUTBOUND_COOLDOWN_STREAK: dict[str, int] = {}


def _outbox_env_int(name: str, default: int, *, minimum: int = 0) -> int:
    """读一个整数环境变量：空 / 写坏 / 越界一律回落默认值（⛔ 不把坏配置当 0）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def outbox_min_request_interval_ms() -> int:
    """出向最小间隔（毫秒；每次现读，运维改完 `.env` 重启即生效）。"""
    return _outbox_env_int(
        "JIDANPU_MIN_REQUEST_INTERVAL_MS",
        OUTBOX_MIN_REQUEST_INTERVAL_MS_DEFAULT,
        minimum=0,
    )


def outbox_max_per_round() -> int:
    """一轮最多推多少条（默认 20；每次现读）。"""
    return _outbox_env_int(
        "JIDANPU_OUTBOX_MAX_PER_ROUND", OUTBOX_MAX_PER_ROUND_DEFAULT, minimum=1,
    )


def outbox_cooldown_base_seconds() -> int:
    """468 冷却基数（秒，默认 300；每次现读）。"""
    return _outbox_env_int(
        "JIDANPU_OUTBOX_COOLDOWN_SECONDS", OUTBOX_COOLDOWN_BASE_SECONDS, minimum=1,
    )


def wait_outbound_request_slot() -> None:
    """等到「距上一次出向请求 ≥ 最小间隔」再放行（全局、进程内、线程安全）。⚠️ 粒度是一条出向事件（一次 push 里的多跳 HTTP 共用这一格）：对端限流按「请求频率」算，但本引擎能拿到的稳定边界就是「这一条推完了没有」；按每条 HTTP 再拆会在 SDK 内部重试上重复计时间，反而不可预测；
    ⚠️ 时钟/睡眠做了可注入（`_outbound_monotonic`/`_outbound_sleep`）：用例断间隔时替换它们 ⛔ 不真的睡 500ms。"""
    interval = outbox_min_request_interval_ms() / 1000.0
    if interval <= 0:
        return
    global _OUTBOUND_LAST_AT
    with _OUTBOUND_RATE_LOCK:
        now = _outbound_monotonic()
        if _OUTBOUND_LAST_AT is not None:
            remaining = interval - (now - _OUTBOUND_LAST_AT)
            if remaining > 0:
                _outbound_sleep(remaining)
                now = _outbound_monotonic()
        _OUTBOUND_LAST_AT = now


def _cooldown_key(binding) -> str:
    """冷却状态的键（含 schema：跨租户主键会撞）。⛔ 不含任何凭据。"""
    return f"{connection.schema_name}:{binding.pk}"


def outbox_cooldown_until(binding) -> datetime | None:
    """这一行绑定**当前**的出口冷却截止时刻（`None` = 没冷却）。"""
    stamp = jidanpu_proxy.cooldown_until(_cooldown_key(binding))
    if stamp is None:
        return None
    return datetime.fromtimestamp(float(stamp), tz=dt_timezone.utc)


def _enter_outbox_cooldown(binding, *, now) -> datetime:
    """池里出口都试完仍 468 ⇒ 该店进入冷却（5 分钟起、再被拦翻倍、封顶 30 分钟）。"""
    key = _cooldown_key(binding)
    streak = _OUTBOUND_COOLDOWN_STREAK.get(key, 0) + 1
    seconds = min(
        outbox_cooldown_base_seconds() * (2 ** (streak - 1)),
        OUTBOX_COOLDOWN_MAX_SECONDS,
    )
    until = now + timedelta(seconds=seconds)
    _OUTBOUND_COOLDOWN_STREAK[key] = streak
    jidanpu_proxy.record_cooldown(key, until.timestamp())
    logger.warning(
        "下家绑定 #%s：出口池全部被 468 拦截 ⇒ 冷却 %s 秒（到 %s；连续第 %s 次），"
        "冷却期间不推这家、其它店照常",
        binding.pk, seconds, until.isoformat(), streak,
    )
    return until


def _leave_outbox_cooldown(binding) -> None:
    """成功一次 ⇒ 清掉冷却计数（下一次再被拦从 5 分钟重新起算）。"""
    key = _cooldown_key(binding)
    _OUTBOUND_COOLDOWN_STREAK.pop(key, None)
    jidanpu_proxy.clear_cooldown(key)


def reset_outbound_rate_limit_for_tests() -> None:
    """只给用例用：把进程内的间隔锚与冷却计数清回初始值。"""
    global _OUTBOUND_LAST_AT
    with _OUTBOUND_RATE_LOCK:
        _OUTBOUND_LAST_AT = None
    _OUTBOUND_COOLDOWN_STREAK.clear()


class _BlockDetectingClient:
    """包一层 `client.call`：记录事件内部被吞掉的 468（不改变调用方看到的行为）。⚠️ 为什么需要它（468 降频）：①式的消费支里有多处 `except ApiError` 会把 468 折成业务失败（`RETRY`/`failed`）⇒ 引擎层看不到 `UpstreamBlockedError`；业务方口径是「收到 468 就先换下一个出口重试」⇒ 引擎必须知道这一条事件撞过 468；
    ⚠️ 只包 `call`（`JidanpuClient.call` 是 SDK 的唯一出口）；`__getattr__` 把其它属性原样透出（`last_envelope`/`ensure_login`/`iter_all`… 一个都不改）。"""

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "blocked", False)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def call(self, *args, **kwargs):
        try:
            return object.__getattribute__(self, "_inner").call(*args, **kwargs)
        except UpstreamBlockedError:
            object.__setattr__(self, "blocked", True)
            raise



@contextmanager
def _outbox_guard(binding):
    """按绑定拿会话级 advisory lock：拿到 ⇒ yield `True`，拿不到 ⇒ yield `False`。为什么必须有：同一绑定的出向台账有两个调用方——常驻调度 `jidanpu_sync`（每 3 秒一轮）与 web 端点（手动推送）；取事件没有 `FOR UPDATE`，而 `_finish`/`_retry` 是无条件 pk 更新 ⇒ 两轮同时跑会把同一条事件消费两次：`record` 会向对方发两次 `CreateOrder`，后写者还可能把前一轮的 `done` 改回 `failed`（白退避重试）；⚠️ 闸门只能放在共享引擎里（两个调用方都经过 `run_outbox`）⛔ 只加在 web 端点的话调度仍会并发进来，问题原样存在。
    四条实现口径：会话级（`pg_try_advisory_lock`）而不是事务级——一轮消费里有多次网络往返（每次上游调用几秒），事务级锁要求整轮在一个事务里 ⇒ 那是把长事务写进热路径；拿不到立刻返回（⛔ 不排队等锁）——web 请求在锁上干等会把 gunicorn worker 拖到超时，手动推送宁可回一句「已排队」（见 `OUTBOX_BUSY_REASON` 的措辞理由）；`finally` 一定解锁（同一个连接）——不解锁会把连接池里那条连接永远卡住；⚠️ 同一连接上重复加同一把锁可重入（PostgreSQL 语义：需要同样次数的解锁）⇒ 嵌套调用不会死锁，代价是同一线程重入挡不住（今天没有这种调用）；
    ⚠️ 锁的作用域是当前数据库（PostgreSQL advisory lock 的事实）：本仓一租户一库 ⇒ 「同一绑定」天然不会跨租户撞车。"""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            [OUTBOX_LOCK_NAMESPACE, binding.pk],
        )
        acquired = bool(cursor.fetchone()[0])
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                [OUTBOX_LOCK_NAMESPACE, binding.pk],
            )


def _backoff_elapsed(event, *, now) -> bool:
    """这条失败事件的退避窗过去了吗（`min(2^attempts × 基准, 上限)`，spec §10）。"""
    attempts = event.attempts or 0
    if attempts <= 0:
        return True
    wait = min(
        (2 ** attempts) * OUTBOX_BACKOFF_BASE_SECONDS, OUTBOX_BACKOFF_MAX_SECONDS
    )
    anchor = event.processed_at or event.created_at
    return anchor + timedelta(seconds=wait) <= now


def _unexpected_reason(event, exc: BaseException) -> str:
    """未预期异常的人话原因（⛔ 只带异常类型名，不带正文）。"""
    ref = (event.target_ref or "")[:120]
    return UNEXPECTED_EVENT_REASON.format(
        exc=type(exc).__name__, kind=event.kind, ref=ref
    )


class _OutboxEventTimeout(BaseException):
    """单条事件处理超时（由 `SIGALRM` 的 handler 抛出）。⚠️ 继承 `BaseException` 而不是 `Exception`：消费路径里有若干处 `except Exception`（传图失败不挡派单/拉图失败不挡待验收……）——如果超时只是普通 `Exception`，那些边界会把它接住、处理函数会继续往下跑，deadline 就变成假判据；
    `BaseException` 穿过它们，由 `_consume_all` 的专用分支收口成 `failed`。"""


@contextmanager
def _outbox_event_deadline():
    """给一个事件的处理装一个真的会打断它的总时限（`SIGALRM`）。选型：选 `signal`+`ITIMER_REAL`（`SIGALRM`）——它能在处理函数内部（真正卡住的地方：上游 HTTP、DB、sleep）把控制权抢回来并抛异常，而「每步之间检查 deadline」只能在下一次检查点生效（一个卡在 socket 上的处理函数永远走不到检查点，那等于没做）；用 `setitimer` 而不是 `alarm()`——`alarm()` 只收整数秒，用例要能用亚秒值验证「真的打断」（两者是同一个 `SIGALRM` 机制）；
    ⚠️ 只在主线程可用（`signal.signal` 的硬约束）：本函数的两个生产调用点都在主线程（常驻调度 `jidanpu_sync` 的循环、gunicorn sync worker 处理手动推送请求）⇒ 生产路径上是真打断；非主线程（若将来有人在 worker 线程里调）没有安全的中断手段（跨线程 `raise` 会破坏 DB 连接/解释器状态），这里不做假判据：不装定时器，只由调用方在返回后按耗时兜底；⚠️ 退出时把上一个 handler/定时器原样还回去（嵌套调用不破坏外层定时器）；
    ⚠️ 超时点可能落在事务中间：`BaseException` 会让 `transaction.atomic()` 整体回滚（不会留下半笔账），随后 `_retry` 在新的写入里记 `failed`。"""
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(signum, frame):  # noqa: ARG001 —— signal handler 的固定签名
        raise _OutboxEventTimeout

    previous_handler = signal.signal(signal.SIGALRM, _raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, OUTBOX_EVENT_DEADLINE_SECONDS)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def run_outbox(binding, *, now, client=None, http=None, line=None) -> OutboxStats:
    """取该绑定的在飞事件、逐个消费，回一份 `OutboxStats`。⚠️ 逐事件异常边界：一个事件抛未预期异常 ⇒ 记下来（`failed`+退避）、继续下一个；全部跑完后把失败清单装进 `MemberOutboxUnexpectedFailures` 响亮抛出（⛔ 不静默、⛔ 不伪装成业务失败）。⚠️ 停用（`enabled=False`）⇒ 一个事件都不动（spec §8.4：在飞事件不再重试，留台账待人看）——`paused=True`；到 `OUTBOX_MAX_ATTEMPTS` 不再重试（`abandoned`）留台账待人看；健康列（`last_run_at`/`last_ok_at`/`last_error`/`last_error_at`）一律 `.update()` 收窄写入（裁定 5）：整行 `save()` 会把并发写进去的别的列覆盖回旧值；⚠️ `client` 同 `consume_event`：只为可测性注入；真实形态下一轮只建一次连——而且没有在飞事件时一次连都不建（否则每次快路 tick 都白登录一遍）；⚠️ `http` 同 `consume_event`：A 段传图要读下家上传页的隐藏域（裸 HTTP 那一跳）⇒ 把 `member_session` 那一对里的 HTTP 层一路带下去 ⛔ 不在消费路径里另建一条连。
    ⭐ `line`（②式收口时定）：这一行绑定属于哪条线（`OutboxLine`）；缺省=①式（`member_outbox_line()`）⇒ 既有调用点（`jidanpu_sync` 那条每 3 秒一拍）与既有用例一个字都不用改、行为逐字不变；②式那条分派层（`jidanpu_member_openapi_outbox`）显式传它自己那一份；⛔ 缺省不是「按 `binding.mode` 猜」：那样这里就成了第二处 mode 判据（E-0202 家族，两处判据迟早漂，而漂的后果是拿①式的登录连接去打一张账号密码为空的②式绑定）。
    ⭐ 按绑定的串行闸：本函数是唯一的取事件入口，而它有两个调用方（常驻调度+web 手动推送）⇒ 闸门放在这里、用 `_outbox_guard`（PostgreSQL 会话级 advisory lock，key=`(OUTBOX_LOCK_NAMESPACE, binding.pk)`）；拿不到 ⇒ 立刻回 `OutboxStats(blocked_reason=OUTBOX_BUSY_REASON)`、一个事件都不取（⛔ 不等锁）；⚠️ 位置在停用/这条线开关关着那两档早退之后、取事件之前：那两档本来一个字节都不写没必要抢锁，而取事件之前必须已经拿到锁；⚠️ 空转（没有在飞事件）照旧走：抢锁 → 查一次 → 打健康列 → 解锁，行为与以前一致（多出的只有两次 advisory lock 调用，它们不碰业务表）。"""
    line = line or member_outbox_line()
    now = now or timezone.now()
    if not binding.enabled:
        return OutboxStats(paused=True)
    if line.enabled is not None and not line.enabled():
        # ⭐ 这一条线自己的开关关着（二期步骤 2 的硬要求：②式那条分派默认关着）⇒ 一次上游都不打、一个字节都不写（连健康列都不动），只回一句人话。
        #    ⚠️ 那道闸必须在引擎里 ⛔ 不能只交给调用方记住：⛔ 不许有人绕过入口闸直接拿②式那份 `line` 来调本函数——那会拿②式的密钥直连去打真实的写接口（而本步骤只通了「推状态」，其余五档不但会失败、还会按退避表反复重试烧额度）。
        if not line.blocked_reason:
            # 「没跑，但说不出为什么」= 本仓最恨的形态 ⇒ 响亮抛（那是写这条线的人漏了文案）。
            raise ValueError(f"{line.name} 的开关关着，却没说清为什么（blocked_reason 是空的）")
        return OutboxStats(blocked_reason=line.blocked_reason)
    rate_limited = bool(getattr(line, "rate_limit", False))
    if rate_limited:
        # ⭐ 468 冷却期（业务方口径「先换代理、都不行最后才停」）：冷却期间**零请求**，
        #    只回一句人话；其它店不受影响（键含 binding.pk）。
        until = outbox_cooldown_until(binding)
        if until is not None and now < until:
            return OutboxStats(
                cooldown_until=until,
                blocked_reason=f"出口冷却中（到 {until.isoformat()}），本轮不推这家",
            )
    with _outbox_guard(binding) as acquired:
        if not acquired:
            # ⚠️ 与上面那两档「没跑」**分开**：这不是停用、也不是这条线没开，
            # 而是「同一绑定的另一轮正在跑」⇒ 调用方应当稍后重试（web 端点会如实回给用户）。
            return OutboxStats(blocked_reason=OUTBOX_BUSY_REASON)
        pending = JidanpuSyncEvent.objects.filter(
            direction="out", kind__in=OUTBOX_KINDS,
            payload__member_binding_id=binding.pk,
            status__in=("pending", "failed"),
        )
        events = list(_remove_waiting_edits(pending, binding).order_by("created_at", "id"))
        if not events:
            # 空转：只打「上次运行时刻」，⛔ 不建连、⛔ 不动成功 / 失败两列。
            _stamp_health(binding, now=now, stats=OutboxStats(), unexpected=[])
            return OutboxStats()
        if client is None:
            # ⭐ 建连那一跳由这一条线自己决定（①式登录/②式密钥直连）——见 `OutboxLine.spawn_client`。
            #    ⭐ 468 换出口要能关掉旧会话、按新出口重建 ⇒ 这里显式管上下文，`_consume_all` 通过 `reconnect()` 换一份（⛔ 不能在外层 `with` 里再进一次：`member_session` 的每绑定锁不可重入，会当场死锁）。
            opened: dict = {}

            def _close_session() -> None:
                cm = opened.pop("cm", None)
                if cm is not None:
                    cm.__exit__(None, None, None)

            def _reopen_session():
                _close_session()
                cm = line.spawn_client(binding)
                sdk, real_http = cm.__enter__()
                opened["cm"] = cm
                return sdk, http or real_http

            try:
                sdk, real_http = _reopen_session()
                tally, unexpected, failures = _consume_all(
                    events, binding=binding, client=sdk, http=real_http, now=now,
                    line=line, reconnect=_reopen_session,
                )
            finally:
                _close_session()
        else:
            tally, unexpected, failures = _consume_all(
                events, binding=binding, client=client, http=http, now=now,
                line=line, reconnect=None,
            )
        stats = OutboxStats(
            **tally,
            cooldown_until=outbox_cooldown_until(binding) if rate_limited else None,
        )
        _stamp_health(
            binding, now=now, stats=stats, unexpected=unexpected, failures=failures
        )
        if unexpected:
            raise MemberOutboxUnexpectedFailures(unexpected, stats=stats)
        return stats


def _consume_all(events, *, binding, client, http, now, line=None, reconnect=None):
    """逐个消费（⛔ 顺序即 `created_at, id`）；回 `(计数 dict, 未预期原因清单, 逐条失败人话)`。⚠️ `failures` 收的是每个本轮仍旧失败的事件自己的那句人话（`event.reason`）——它让绑定的 `last_error` 不再只有汇总句（spec §9.1 要求抛单失败要能点名「可能是缺『将订单发到内部抢单大厅』权限」，只靠一句「本轮有 N 条台账事件仍失败」看不见）；⚠️ `line` 由 `run_outbox` 透传（缺省①式，见它自己的注释）；
    ⭐ `line.rate_limit`（①式）：每推一条之前等 `JIDANPU_MIN_REQUEST_INTERVAL_MS`（全局最小间隔）；一轮最多推 `JIDANPU_OUTBOX_MAX_PER_ROUND` 条，其余留到下一轮（`capped`）；468 ⇒ 先换下一个出口重试（`reconnect()` 重建客户端），池里全试完仍 468 ⇒ 该店进入冷却（`_enter_outbox_cooldown`）并停止本轮（事件留 pending）；
    ⚠️ `reconnect` 只在真实建连那条路可用（`run_outbox` 自己管会话）；注入客户端的场景（用例/web 手动推送）拿不到新出口 ⇒ 468 仍按原来的「记 failed」收口。"""
    tally = {
        "done": 0, "failed": 0, "skipped": 0, "deferred": 0, "abandoned": 0,
        "capped": 0, "suspended": 0,
    }
    unexpected: list[str] = []
    failures: list[str] = []
    timeout_reason = OUTBOX_EVENT_TIMEOUT_REASON.format(
        seconds=OUTBOX_EVENT_DEADLINE_SECONDS
    )
    rate_limited = bool(getattr(line, "rate_limit", False))
    max_per_round = outbox_max_per_round() if rate_limited else None
    attempted = 0
    stop_round = False
    # ⭐ D-0359 D3：launch 的投递侧熔断闸。懒判定：本轮第一条到点待投的 launch 行才查一次（`_launch_delivery_gate`），缓存整个轮次；`held`=本轮待投的 launch 行数（不含人工旁路行，warning 日志用）；
    #    ⭐ 人工旁路：payload 带 `rearmed_at` 的行（`queue_member_event(rearm=True)` 写，人工重抛）根本不进这道闸、照旧直投——与队列闸时代「在飞行不受闸」的行为等价（旧机制只挡新行诞生，已在飞的行——含人工行——照常重试）。
    launch_gate: tuple[bool, bool] | None = None
    held_launches = sum(
        1 for e in events
        if e.kind == KIND_LAUNCH and not (e.payload or {}).get("rearmed_at")
    )
    for event in events:
        if (event.attempts or 0) >= OUTBOX_MAX_ATTEMPTS:
            # 到上限：不再打上游、⛔ 不改状态（留台账给人看）。
            tally["abandoned"] += 1
            continue
        if not _backoff_elapsed(event, now=now):
            tally["deferred"] += 1
            continue
        # ⭐ 投递侧熔断闸（判据与探测节奏见 `_launch_delivery_gate`）：挂起期的 launch 不投（行保持 pending=发单意图不丢，解除后队列自动放行），只计入「熔断挂起 N」；
        #    探测放行那一轮，迭代序里第一条待投 launch 行放过去（=行 id 最小者/排队最早，选型理由见 `_launch_delivery_gate` 的 docstring），其余照旧挂起；⚠️ 只闸①式语义的 launch 判定本身；人工行与其它 kind 不闸。
        if event.kind == KIND_LAUNCH and not (event.payload or {}).get("rearmed_at"):
            if launch_gate is None:
                launch_gate = _launch_delivery_gate(binding, now=now, held=held_launches)
            suspended, probe_release = launch_gate
            if suspended:
                # ⭐ D-0361：被挡的单落一条挡单台账（每 (单，绑定) 12h 恰一条）——
                #    E-0312 病灶「被挡的单零台账」的收口；拦截行为零改动（照挡不误）。
                _note_launch_blocked_once(event, binding, now=now)
                tally["suspended"] += 1
                continue
            if probe_release:
                # 放行只此一发：本轮后面的 launch 行照旧挂起。
                launch_gate = (True, False)
        if max_per_round is not None and attempted >= max_per_round:
            # ⭐ 每轮封顶：第 K+1 条**留到下一轮**（⛔ 不丢、⛔ 不记失败）。
            tally["capped"] += 1
            continue
        attempted += 1
        #: 这一条事件最多试几个出口（初始一次 + 换出口重试）。
        outlets_left = max(1, len(jidanpu_proxy.proxies()))
        tracked = _BlockDetectingClient(client) if rate_limited else None

        def _retry_on_next_outlet() -> bool:
            """撞 468 ⇒ 换下一个出口。`True` = 已换、可以重试；`False` = 进入冷却/停止。"""
            nonlocal client, http, tracked, outlets_left, stop_round
            switch = jidanpu_proxy.note_blocked(force=True)
            if switch == jidanpu_proxy.BLOCK_SWITCHED and outlets_left > 1:
                outlets_left -= 1
                jidanpu.member_pool().mark_all_stale()
                client, http = reconnect()
                tracked = _BlockDetectingClient(client) if rate_limited else None
                return True
            # 池里都试过 / 已无可用出口 ⇒ 该店冷却，**停止本轮**（事件留 pending）。
            _enter_outbox_cooldown(binding, now=now)
            tally["deferred"] += 1
            stop_round = True
            return False

        while True:
            started = time.monotonic()
            if rate_limited:
                # ⭐ 全局最小间隔（两条出向请求之间至少 N 毫秒）。
                wait_outbound_request_slot()
            if tracked is not None:
                tracked.blocked = False
            try:
                with _outbox_event_deadline():
                    outcome = _consume(
                        event, binding=binding,
                        client=tracked if tracked is not None else client,
                        http=http, now=now, line=line,
                    )
            except _OutboxEventTimeout:
                # ⭐ 单条事件总时限到点（`SIGALRM` 真的打断了处理函数）⇒ 记 `failed` + 走既有
                #   退避 / 计数，然后**继续下一条**（⛔ 不让一条卡死的事件占住整轮）。
                outcome = _retry(event, reason=timeout_reason, now=now)
                if outcome == "failed":
                    failures.append(event.reason)
                tally[outcome] += 1
                break
            except UpstreamBlockedError as exc:
                if not rate_limited or reconnect is None:
                    # ②式 / 注入客户端：保持原口径（468 记 failed、走退避）。
                    reason = _unexpected_reason(event, exc)
                    _retry(event, reason=reason, now=now)
                    unexpected.append(reason)
                    failures.append(event.reason)
                    tally["failed"] += 1
                    break
                # ⭐ 业务方口径：先换下一个出口重试（一次 468 就换，⛔ 不等连续 3 次）。
                if _retry_on_next_outlet():
                    continue
                break
            except Exception as exc:  # noqa: BLE001 —— 逐事件异常边界（控制器裁定 5）
                reason = _unexpected_reason(event, exc)
                _retry(event, reason=reason, now=now)
                unexpected.append(reason)
                failures.append(event.reason)
                tally["failed"] += 1
                break
            # ⭐ 事件内部的 468 被 handler 折成业务失败时，`_consume` 不会抛 ⇒ 靠这层包皮
            #    知道「这一条撞过 468」，按同一口径换出口重试（⛔ 不把它记成一次普通失败）。
            if tracked is not None and tracked.blocked:
                if _retry_on_next_outlet():
                    continue
                break
            if time.monotonic() - started > OUTBOX_EVENT_DEADLINE_SECONDS:
                # 没收到超时信号时也按实际耗时结算，保留新排的价格和项目改动。
                outcome = _retry(event, reason=timeout_reason, now=now)
                if outcome == "failed":
                    failures.append(event.reason)
                tally[outcome] += 1
                break
            if outcome == "skipped":
                tally["skipped"] += 1
            elif outcome == "done":
                tally["done"] += 1
            elif outcome == "deferred":
                tally["deferred"] += 1
            else:
                tally["failed"] += 1
                failures.append(event.reason)
            # 这一条**没撞 468** ⇒ 出口是通的：清掉冷却计数（下一次再被拦从基数重新起算）。
            if rate_limited:
                _leave_outbox_cooldown(binding)
            break
        if stop_round:
            break
    return tally, unexpected, failures


def _round_failure_reason(stats: OutboxStats, unexpected: list[str],
                          failures: list[str] = ()) -> str:
    """把本轮失败压成一句人话（⛔ 不写空串：空 `last_error`+新时间戳看起来像「成功没清干净」）。
    ⚠️ 逐条人话排在前面（`failures`）：抛单失败那句点名权限的提示必须能出现在绑定的 `last_error` 上（spec §9.1 末段/§13-R7）⛔ 不能只留一句汇总。"""
    parts = [*failures, *unexpected]
    if stats.failed:
        parts.append(f"本轮有 {stats.failed} 条台账事件仍失败，请查台账")
    if stats.abandoned:
        parts.append(
            f"有 {stats.abandoned} 条事件已到重试上限、不再自动重试，请人工核对"
        )
    unique = [part for part in dict.fromkeys(parts) if part]
    return "；".join(unique)[:1000] or ROUND_UNKNOWN_FAILURE_REASON


def _stamp_health(binding, *, now, stats: OutboxStats, unexpected: list[str],
                  failures: list[str] = ()) -> None:
    """整轮的记账（⛔ 收窄写入）：失败 ⇒ `last_error`；有成功且无失败 ⇒ `last_ok_at`+清旧错。
    ⚠️ 失败时不动 `last_ok_at`（它的语义是「上次成功时刻」）；只有 `skipped`/`deferred` 的轮次两项都不动（那既不是成功也不是失败）。"""
    fields: dict = {"last_run_at": now}
    if unexpected or stats.failed or stats.abandoned:
        fields["last_error"] = _round_failure_reason(stats, unexpected, failures)
        fields["last_error_at"] = now
    elif stats.done:
        fields["last_ok_at"] = now
        fields["last_error"] = ""
        fields["last_error_at"] = None
    JidanpuMemberBinding.objects.filter(pk=binding.pk).update(**fields)


# ══ 日志快路+自动接手（spec §2.2/§6.1/§6.3/§8.2/§8.3）═══════════
# 三件事，别的都不做：①拿 `binding.log_cursor` 当游标增量拉对方的系统操作日志（`logType=10` 订单相关）；②命中「本线链路上的单」⇒ 用 `fetch_ass_logs` 回查远端谁接了（数值 `userId` 是唯一稳定来源）——⚠️ 日志只当闹钟（A9）：⛔ 一个字节都不解析 `remark`（同一件事对方有多种渠道文案，实测三种）；③真被接走 ⇒ 走 `claim_order`（唯一状态机入口+`nodes_of` 的节点门）把单接手给该代练员，并写订单审计（机器操作人+人话写明「由下家鸡蛋铺接单自动接手」）。
# ⭐ 钱在我方算（spec §8.2 的 A6，⚠️ 与旧线口径不同）：旧线（P2 spec §7②）里对方的打手接了单，我方一分钱不算、只记状态与接单人；本线里下家工作室的代表账号就在我方，所以对方一接，我方就把单接手给这个账号、按我方规则进绩效/结算。⛔ 后来者别照旧线的「不算钱」口径把本线改坏：那会让同一张单少付一次，而且没有任何报错（这正是本节必须自带绩效行断言的原因）。⚠️ 失败一律响亮（spec §10 同一条口径）：读不到日志/读不到远端接单人/缺节点导致 `IllegalTransition`/被 `claim_order` 拒/上游报错 ⇒ 台账+`last_error`（收窄写入 ⛔ 不整行 `save()`）；读不到那几种不推进游标（那一行下一轮还会被拉回来）——⛔ 不静默、⛔ 不死循环。⚠️ 裁定 8：机器接手传 `claim_order(..., enforce_limits=False)` ⇒ 跳过按人的四道抢单闸门（没有接单价/禁自助接单/接单价区间/持单上限）——A6 是无条件的，被这几道挡住会造出「我方没接手、对方在做」的错位；⛔ 其余判据一道不跳。

#: 本线**日志快路**写台账用的 kind（spec §4.3 的 `member.remote_taken`，`direction="in"`）。
#: ⚠️ 与旧线的 `hall.remote_taken` **不是**同一个 kind（那条在 `jidanpu_hall` 里定义）：
#: 两条线的同一件事语义不同（旧线只记账、本线真接手），⛔ 不要合并。
KIND_REMOTE_TAKEN = "member.remote_taken"

#: 本线状态回拉（慢路）写台账用的 kind（spec §4.3 的 `member.status_in`，`direction="in"`）。消费者就是 `pull_statuses` 自己（它读到远端真实状态后当场写我方状态），
#: 台账行是留痕：`done`（写了）/`skipped`（池内两态或已结账挡住的，不改）/`failed`（读不到、认不出、缺节点——都同时进 `last_error`）。
KIND_STATUS_IN = "member.status_in"

#: 日志去重键（spec §2.2 的**实测**：同一秒可以有多行 ⇒ 只按时间当游标会漏）。
#: ⚠️ 键里虽然含 `remark`，但它**只用于去重**：⛔ 一个字节都不参与判据（A9）。
LOG_DEDUPE_KEYS = ("createTime", "joinOrderId", "remark")

#: 自动接手的**审计操作人**（spec §8.3：机器动作、而且是钱的动作 ⇒ 审计里必须一眼看出来）。
MACHINE_ACTOR = "下家鸡蛋铺同步"
#: 审计动作 code（登记在 `apps/audit/actions.py`；模板逐字含「由下家鸡蛋铺接单自动接手」）。
AUTO_CLAIM_AUDIT_ACTION = "order.jidanpu_member_auto_claim"

# ── 人话原因 / 台账文案（⛔ 不回显上游正文：它可能带上游回包 / 凭据片段，spec §8.5）──────
#: 整轮读不到系统操作日志。
LOG_FETCH_FAILED_REASON = "读不到下家的系统操作日志"
#: 日志行的时间戳不是实测出来的那个格式（上游改版 ⇒ 响亮）；⚠️ **只跳过这一行**、⛔ 不停摆
#: 整条绑定（见 `_bad_row_reason` 与 `_poll_log` 的行级隔离）。
ROW_TIME_UNREADABLE_REASON = (
    "系统操作日志里有一行的 createTime 不是 yyyy-MM-dd HH:mm:ss，"
    "已跳过这一行、本轮其余行照常处理，游标继续推进"
)
#: 坏行的人话里点名的**字段名**（spec §2.2 实测的行形状之一）。
BAD_ROW_FIELD = "createTime"
#: 回查「谁接了」失败 ⇒ **「读不到」不等于「远端没接」**（spec §2.2 的取证纪律）。
REMOTE_STATE_UNREADABLE_REASON = "读不到远端的接单人日志，无法判断这张单被接走没有"
#: 远端**这张单本身**读不到（单查抛错 / 查不到）。
REMOTE_ORDER_UNREADABLE_REASON = "读不到远端这张单，无法判断它被接走没有"
#: ⚠️ `fetch_order_by_key1` 拿到 `None`：**⛔ 不许当成「远端没有这张单」**——它只在第一页找、
#: 时间窗是调用当天往前滚约 90 天 ⇒ 老单必然拿到 `None`。一律当**读不到**处理。
REMOTE_ORDER_NOT_FOUND_REASON = (
    "远端单查里找不到这张单（可能录单早于它的约 90 天窗口）：一律当读不到处理，本轮不接手"
)
#: spec §6.1 的 ③：远端**已退单** ⇒ 只更新台账（⛔ 不接手、⛔ 不排退单）。
REMOTE_ORDER_REFUNDED_REASON = "远端这张单已退单：只更新台账，本轮不接手、也不排退单"
#: spec §6.1 的 ③：远端**还挂在他家大厅** ⇒ 没人接走（接单人日志可能是上一轮的旧人）。
REMOTE_STILL_IN_HALL_REASON = "远端这张单还挂在他家大厅：只更新台账，本轮不接手"
#: ⚠️ 接单人日志**非空、但没有可用的数值 `userId`** ⇒ 这是**读不到**，不是「健康地没被接走」。
REMOTE_TAKER_UNREADABLE_REASON = (
    "远端接单人日志非空但没有可用的数值 userId：当读不到处理，本轮不接手"
)
#: 远端日志里没有可用的接单人（照 B-1 的 `B1_NO_TAKER_REASON`：只记一条、下一轮再判）。
REMOTE_NOT_TAKEN_REASON = "远端接单人日志里没有可用的接单人，本轮不接手，等下一轮再判"
#: 远端接了、但接单人**就是**本绑定的主人（他自己在我方大厅接的）⇒ 无需自动接手。
REMOTE_TAKEN_OURS_REASON = "该单已由本绑定的代练员接手，无需自动接手"
#: 竞态（A10）：远端接了，但我方**别的**打手先接走了 ⇒ 排队去对方退单。
REMOTE_TAKEN_RACE_REASON = "远端接了此单，但我方已有别的打手接手：已排队去对方退单"
#: 机器接手**普通单**成功那条台账的人话（真有 MAIN 绩效行、钱当场按绩效算）。
AUTO_CLAIM_LEDGER_REASON = "远端接了此单，我方已自动接手给该代练员（钱在我方按绩效算）"
#: ⭐ 机器接手隐藏单成功那条台账的人话。⚠️ 不能沿用上面那句：隐藏单没有 MAIN 绩效行、这一拍只是预占了大厅卡，转正要等上家受理 `hall.claim`（`_promote_claiming_mirror`）⇒ 写「已接手/钱按绩效算」是一句假话（E-0028 同族）；
#: 两句各自只在本分支为真，所以分支选文案 ⛔ 不把两条并成一句（并了必有一条是假的）。
AUTO_CLAIM_LEDGER_MIRROR_REASON = "已预占大厅卡，待上家受理后转正"
#: ⭐ D-0325：**上家镜像卡**派生的隐藏单（`Order.upstream_hall_card` 非空）
#: 机器接手那一拍的台账人话——与 peer 那句**分支选文案**（同一条理由：这一拍只是预占了
#: 上家镜像卡、走 `claim_upstream_mirror` 的接单→转正链，钱按转正后的绩效算）。
AUTO_CLAIM_LEDGER_UPSTREAM_MIRROR_REASON = "已预占上家镜像卡，待上家受理后转正"
#: 机器接手被拒：这张单没有 MAIN 绩效行（`claim_order` 的条件更新无处可写）。⚠️ 与 A10 竞态分开：这不是「我方赢了」——落进竞态分支会在谁都没接手的情况下把对方那张单退掉（E-0148 之下「无主 MAIN 行被整条删掉」是正常形态）；
#: ⚠️ 镜像卡派生的隐藏单（`Order.hall_card` 非空）虽然也没有 MAIN 行，但它不走这一档——`machine_claim_order` 对它有专门分支（镜像 claim 路径），这句人话只留给真正的孤儿单/座位已撤回那一档。
AUTO_CLAIM_NO_TAKER_ROW_REASON = "自动接手被拒（这张单没有 MAIN 绩效行，条件更新无处可写）："
#: 机器接手被 `claim_order` 拒（**裁定 8 之后只剩这一种**）：该单此刻**不在我方抢单大厅**
#: （座位已被撤回 / 回池，或我方已有人接了但 MAIN 行读不到）——⚠️ 按人的抢单闸门
#: （接单价区间 / 持单上限 / 禁自助接单 / 没有接单价）**不再**走这一支（`enforce_limits=False`）。
AUTO_CLAIM_REFUSED_REASON = "自动接手被拒（该单此刻不在我方抢单大厅，或已被别人接走）："
#: 被状态机拒绝（多半是调用方节点集不含这条边要的节点）。
AUTO_CLAIM_DENIED_REASON = "自动接手被状态机拒绝（多半是该账号缺节点）："
#: ⭐ 慢路遇到「远端代练中、我方没接手人」而接手又没成时的兜底原因（正常情况下
#: `_handle_alarm` 会给一句更具体的话；这一句只在它没给的时候用）。
REMOTE_TAKEN_UNSYNCED_REASON = (
    "远端已是「代练中」但我方没有接手人，且本次自动接手没成功：本轮不写「代练中」状态，"
    "请查绑定的失败原因（多半要换一个代练员账号来绑）"
)
#: ⭐ 真机：接手时撞了 `uniq_performance_order_user`（同一张单、同一个人只有一条绩效行）——最常见成因：这个账号既是这张单的客服、又是下家绑定的代练员。
#: ⛔ 这条不是「重试就会好」：状态不会自己变，必须人来改绑定或改单；所以它走「响亮拒绝」，而且在记录之前就不要再打上游（`_poll_log` 的游标照推 ⛔ 不许每 3 秒重撞一次）。
AUTO_CLAIM_SAME_PERSON_REASON = (
    "自动接手被拒（这个账号在这张单上已经有绩效行——多半他既是这张单的客服、"
    "又是下家绑定的代练员，系统不允许同一人在一张单上兼两个角色）："
    "请换一个代练员账号去绑下家鸡蛋铺，或人工把这张单指派给他；"
    "本次**不改动对方那张单**"
)


def _log_moment(value):
    """把日志行的 `createTime` 解析成带时区的时刻；解析不了回 `None`。
    ⚠️ 用 `jidanpu.SYSTEM_LOG_TIME_FORMAT`（该端点时间格式的唯一出处）：同产品的另一个日志端点只吃纯日期 ⛔ 两处各写一份格式串必然漂移。"""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, jidanpu.SYSTEM_LOG_TIME_FORMAT)
    except ValueError:
        return None
    return timezone.make_aware(parsed)


def _uid_of(value):
    """远端 `userId` 的**数值**；不是数值（空串 / 名字 / `bool`）回 `None`。⚠️ `bool` 是 `int` 的子类 ⇒ 单独挡掉（`userId: true` 不是一个人）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdigit() else None


def _row_key(row: dict) -> tuple:
    """去重键（`LOG_DEDUPE_KEYS`）：把缺失的键归一成空串（`None` 与 `""` 是同一件事）。"""
    return tuple(
        "" if row.get(name) is None else str(row.get(name)) for name in LOG_DEDUPE_KEYS
    )


def _bad_row_reason(row: dict) -> str:
    """一行时间戳解析不了的日志的人话原因（要点名「是哪一行/哪个键坏了」）。⚠️ 这段文本同时是去重键（`_note_binding_once` 按 `(kind, target_ref, reason)` 去重）：同一行内容每轮算出同一个串 ⇒ 坏行只记一条 ⛔ 不会每 3 秒重复记；
    ⚠️ 行标识只取单号类字段（`joinOrderId`/`soureOrderId`）与那个坏掉的原始值 ⛔ 不回显 `remark`（自由文本，spec §8.5/A9）。"""
    raw = str(row.get("createTime") if row.get("createTime") is not None else "").strip()
    join = str(row.get("joinOrderId") or "").strip()
    source = str(row.get("soureOrderId") or "").strip()
    if join or source:
        where = f"joinOrderId={join or '（空）'} soureOrderId={source or '（空）'}"
    else:
        where = "两个单号字段都为空"
    return (
        f"{ROW_TIME_UNREADABLE_REASON}：{BAD_ROW_FIELD} 的值是『{raw[:40]}』，行标识 {where}"
    )


def _matched_link(row: dict, by_remote: dict, by_order: dict):
    """这一行日志对得上本线的哪条链路（spec §6.1 的「只留能对上的行」）。判据两条、按优先级：`joinOrderId`=链路的远端单号（对方内部单号，最准）；退而求其次 `soureOrderId`=有效来源单号（`effective_source_order_no`——它与我们录单时写进对方 `souresOrderID` 的值是同一个，唯一出处）；
    ⛔ 两条都对不上 ⇒ `None`（那不是本线录进去的单）。"""
    join = str(row.get("joinOrderId") or "")
    if join:
        link = by_remote.get(join)
        if link is not None:
            return link
    source = str(row.get("soureOrderId") or "")
    if source:
        return by_order.get(source)
    return None


def _echo_by_name(row: dict, binding, *, verified_name=None) -> bool:
    """日志只有操作人姓名，同名的大厅单还需用远端当前订单核实。"""
    name = (binding.account_name or "").strip() if verified_name is None else verified_name
    return bool(name) and str(row.get("userName") or "").strip() == name


def _remote_taken_done(binding, order_no: str, *, listed_at=None) -> bool:
    """这张单在当前这一轮上大厅里已经被本快路处理过（`done` 标记）吗——幂等，且排在上游读之前。照旧线 B-1 的 `(kind=hall.remote_taken, target_ref, status="done")` 形状：稳态下每轮都会看到同一批游标秒里的行，没有这道标记就会每轮白花一次上游回查、并可能重复接手；⚠️ 只认 `done`（终态「已接手/已确认归他」）；`failed`/`skipped` 不当标记（那会把一次「读不到」钉成永久的假账，`_note_once` 那边另有按 `(kind, ref, reason)` 去重的记法）；
    ⭐ 时间判据（N1/R3-M1，业务方拍板）：`done` 行是上一轮生命周期留下的台账事实 ⛔ 不改它的 `status`；回池重抛（`_consume_launch` 把链路更新成 `in_hall=True, listed_at=now`）之后，旧标记不能再挡住新一轮的接手。判据=标记时刻（`created_at`，`auto_now_add`）不早于链路最近一次 `listed_at`：`listed_at` 非空：只认 `created_at >= listed_at` 的 `done` 行，更早的算「上一轮的结论」；`listed_at is None`（legacy/从未进过厅）：退回旧判据（只看 `done` 行存在）⛔ 不让老数据突然失去去重。"""
    queryset = JidanpuSyncEvent.objects.filter(
        direction="in", kind=KIND_REMOTE_TAKEN, target_ref=order_no,
        status="done", payload__member_binding_id=binding.pk,
    )
    if listed_at is not None:
        queryset = queryset.filter(created_at__gte=listed_at)
    return queryset.exists()


def _note_order_once(*, binding, order, reason: str, status: str,
                     kind: str = KIND_REMOTE_TAKEN,
                     extra_payload: dict | None = None) -> JidanpuSyncEvent:
    """同一张单+同一句原因只写一条台账（照 `jidanpu_hall._note_once` 的幂等口径）。⚠️ 键是 `(kind, target_ref, reason, member_binding_id)`：同一个 `target_ref` 上不同的结论互不遮蔽，同一个结论在不同绑定名下也各留一行；没有这道去重，一次持续几轮的「远端还没接」会每 3 秒加一行（把一个不变的事实放大成 N 行）；⚠️ 键里必须带绑定 id（照 `_remote_taken_done` 的口径）：两个不同地址的绑定各自镜像同一张我方单（spec A12/§4.2 允许）时，先后得出同一结论 ⇒ 不带绑定 id 的话第二份一行都没有，而那一行 `payload.member_binding_id` 还记在别的绑定名下（留痕认错人）；⚠️ `kind` 默认是快路的 `member.remote_taken`（既有调用方逐字不变）；慢路（`pull_statuses`）显式传 `KIND_STATUS_IN`——两条路的结论不许混进同一个 kind。
    ⭐ `extra_payload`（E-0282/H1；缺省 `None` ⇒ 既有行为一字不变）：给了就把这些键并进那一行的 `payload`（`member_binding_id` 之外的结构化载荷，今天只有一处：A 段推送成功的 `{本地 object_key → 对面 oss_key}` 映射）。两种落法：新行 ⇒ 直接并进去；
    命中去重键的旧行 ⇒ 并进那一行（dict 值按 key 浅合并、其余键覆盖写）——不并的话「同一句成功人话、第二轮又推上几张」的映射会静默丢（去重键命中、映射不落 ⇒ 下一轮判重认不出 ⇒ 重复上传）；仍是一行（「同一句话只写一条」不变），只是那一行承载的映射累积起来。"""
    text = reason[:255]
    existing = (
        JidanpuSyncEvent.objects.filter(
            direction="in", kind=kind, target_ref=order.order_no, reason=text,
            payload__member_binding_id=binding.pk,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        if extra_payload:
            merged = dict(existing.payload or {})
            for key, value in extra_payload.items():
                current = merged.get(key)
                if isinstance(current, dict) and isinstance(value, dict):
                    merged[key] = {**current, **value}
                else:
                    merged[key] = value
            JidanpuSyncEvent.objects.filter(pk=existing.pk).update(payload=merged)
        return existing
    payload = {"member_binding_id": binding.pk}
    if extra_payload:
        payload.update(extra_payload)
    return jidanpu_common.write_event(
        direction="in", kind=kind, status=status,
        target_type="order", target_ref=order.order_no, reason=text,
        payload=payload,
    )


def _note_binding_once(
    *, binding, reason: str, status: str = "failed", kind: str = KIND_REMOTE_TAKEN
) -> JidanpuSyncEvent:
    """整轮失败（没有具体单可指认）记一条该绑定的台账；同一句话只写一条。
    ⚠️ `kind` 默认是快路的 `member.remote_taken`（既有调用方逐字不变）；慢路（`_fail_status_pull`）显式传 `KIND_STATUS_IN`——两条路的结论不许混进同一个 kind（与 `_note_order_once` 的同名参数同一条口径 ⛔ 不另造第三套去重）。"""
    text = reason[:255]
    existing = (
        JidanpuSyncEvent.objects.filter(
            direction="in", kind=kind,
            target_ref=str(binding.pk), reason=text,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        return existing
    return jidanpu_common.write_event(
        direction="in", kind=kind, status=status,
        target_type="binding", target_ref=str(binding.pk), reason=text,
        payload={"member_binding_id": binding.pk},
    )


def _note_identity_heal_once(
    *, binding, old_name: str, new_name: str, login_uid: str
) -> JidanpuSyncEvent | None:
    """「下游绑定的同步账号显示名已按登录账号写回」那条幂等痕迹（同一事实只一行）。幂等键=`(kind, target_ref, reason)` 三元组（照 `jidanpu_hall._note_once` 与本文件 `_note_binding_once` 那族口径）：自愈可能在每一轮都发生（有人把名字改回去、或换绑后重新启用），每轮加一行就是把一个不变的事实放大成 N 行；
    ⚠️ `target_ref = f"member:{binding.pk}"`：本线的绑定是每个代练员一行（不是单行资源）⇒ 主键才是这一行的稳定标识；前缀 `member:` 让它与租户级那条线的 `target_ref`（`binding.account`，可能同名）恒可区分（见 `IDENTITY_HEAL_REF_PREFIX` 的注释）；
    ⚠️ `target_type="binding"`：这条讲的是绑定身份 ⛔ 不是一张订单。"""
    ref = f"{IDENTITY_HEAL_REF_PREFIX}{binding.pk}"
    if JidanpuSyncEvent.objects.filter(
        kind=IDENTITY_HEAL_KIND, target_ref=ref, reason=IDENTITY_HEAL_REASON
    ).exists():
        return None
    return jidanpu_common.write_event(
        direction="in", kind=IDENTITY_HEAL_KIND, status="skipped",
        target_type="binding", target_ref=ref, reason=IDENTITY_HEAL_REASON,
        payload={
            "member_binding_id": binding.pk,
            "old_name": old_name, "new_name": new_name, "login_uid": login_uid,
        },
    )


def _stamp_binding_run(binding, *, now) -> None:
    """记「这一轮跑过这一行」（收窄写入，只动 `last_run_at`）。⚠️ 存在的理由（下家②式收口）：②式那一行不跑①式的 outbox/快路/回拉（那三段都要①式的登录凭据），而 `last_run_at` 原本只由那三段推进 ⇒ 不补这一笔，②式绑定的 `last_run_at` 会永远为空：页面上显示「从未运行」，而它其实每 3 秒都在跑（回调消费+兜底对账）——那是说假话；
    ⭐ 改准的那一格：步骤 5 起②式那一轮多了一段自己的出向发单（`jidanpu_member_openapi_outbox.run_member_status_outbox` ⛔ 不是①式那一份）；⚠️ 本函数照旧是必需的：那一段的开关（`JIDANPU_MEMBER_OPENAPI_OUTBOX`）默认关着 ⇒ 关着时它一个字节都不写（连健康列都不动），只有回调消费/兜底对账那两段可能写 `last_ok_at`；而没有「有结论的事」时（什么都没做/发单记了失败）那一格正是靠本函数记下「这一轮跑过了」；
    ⚠️ 口径与上家线 `jidanpu_upstream._stamp_binding_run` 逐字相同（同一个问题、同一份答案；两条线各一份实现只因为两张表）⇒ 记的是「上次跑」而不是「上次成功」 ⛔ 一个字节都不碰 `last_ok_at`/`last_error`。"""
    JidanpuMemberBinding.objects.filter(pk=binding.pk).update(last_run_at=now)


def _stamp_poll_health(binding, *, now, ok: bool, reason: str = "", current_rows=None) -> None:
    """一轮同步的健康记账（收窄写入 ⛔ 不整行 `save()`）。⚠️ 快路（`poll_log`）与慢路（`pull_statuses`）共用这一套列（`last_run_at`/`last_ok_at`/`last_error`/`last_error_at`）⇒ 名字里的 `poll` 是历史，不是「只给快路」；
    ⚠️ 失败时不动 `last_ok_at`（它的语义是「上次成功时刻」）；成功时清掉旧错（否则页面上会一直挂着一条已经好了的错）。"""
    fields: dict = {"last_run_at": now}
    if ok:
        fields["last_ok_at"] = now
        fields["last_error"] = ""
        fields["last_error_at"] = None
    else:
        fields["last_error"] = reason or ROUND_UNKNOWN_FAILURE_REASON
        fields["last_error_at"] = now
    if current_rows is None:
        current_rows = JidanpuMemberBinding.objects.filter(pk=binding.pk)
    current_rows.update(**fields)


def _fail_poll(binding, *, now, reason: str, detail=None) -> int:
    """整轮失败：台账（绑定级）+ `last_error` + 回 0；**⛔ 不推游标**（下一轮重试）。"""
    text = reason if detail is None else f"{reason}（{type(detail).__name__}）"
    _note_binding_once(binding=binding, reason=text)
    _stamp_poll_health(binding, now=now, ok=False, reason=text)
    return 0


#: 快路游标的**落库延迟护栏**（秒）：「推到 now」的两个分支改推 `now − 本值`——上游把日志行
#: 放进日志流有延迟（实测：接单动作之后约 4 秒那轮还没回该行），推到 now 会把还没出现的行
#: 永远甩在游标后面（「已接单没同步」的根因之一）。env 可调、`0` = 旧行为的逃生口。
LOG_CURSOR_LAG_SECONDS_DEFAULT = 60
LOG_CURSOR_LAG_SECONDS_ENV = "JIDANPU_MEMBER_LOG_CURSOR_LAG_SECONDS"


def _cursor_lag_seconds() -> int:
    """护栏秒调用时现读（运维改 `.env` 重启即生效）；空/写坏回落默认值。"""
    return _outbox_env_int(
        LOG_CURSOR_LAG_SECONDS_ENV, LOG_CURSOR_LAG_SECONDS_DEFAULT, minimum=0
    )


def _advance_cursor(binding, parsed, *, cursor: str, now, retry_from=None) -> None:
    """把游标推到本窗口里最大的 `createTime`；窗口为空或本轮没有更新的行 ⇒ 推到 `now − 护栏秒`（护栏秒 env `JIDANPU_MEMBER_LOG_CURSOR_LAG_SECONDS` 可调，缺省 60，`0` = 推到 `now` 的旧行为）。⚠️ 为什么「没有更新的行」也要往前推（安静的下家）：`stime` 是含界的（同一秒可以有多行 ⇒ 必须把那一秒整段重拉一遍，再按行内容去重）；若游标就停在那一秒，下一轮 `[游标, now]` 又会把同一批行拉回来 ⇒ 窗口一轮比一轮长。⚠️ 为什么停在前一秒还不够、要留护栏：上游落库有延迟——动作那一秒的行可能在我们拉取**之后**才进日志流，推到 `now`（或那一秒本身）会把还没出现的行永远甩在游标后面；留 60 秒让这些行下一轮还在窗内。⚠️ 钳制：`now − 护栏` 早于旧游标时不倒退（忙店走 `max(createTime)` 分支时护栏不参与）⇒ 游标恒 ≤ `now`、窗口有界（≤ 护栏 + 轮询间隔）。
    ⚠️ 代价如实登记：护栏窗内的已见行会重拉重判——接手类行首处理后有 `done` 标记兜住（零上游）；`no_claim`/`not_taken` 类行每轮重跑一次上游回查（≤ 护栏秒 ÷ 轮询间隔 次/行）；台账有 `_note_order_once` 去重不会灌水。残余：落库延迟**超过护栏秒**的行仍会漏（兜底=`claim_net` 接单兜底网；慢路回拉只兜「已完成」，兜不了「已被接走未完成」）。
    ⚠️ 用 `.update()` 收窄写入（⛔ 不整行 `save()`：游标那一列之外还有并发写的健康列）。"""
    latest = max((moment for moment, _row in parsed), default=None)
    if latest is None or jidanpu.format_log_time(latest) == cursor:
        guarded = jidanpu.format_log_time(now - timedelta(seconds=_cursor_lag_seconds()))
        # 钳制不倒退（护栏分支专用）：`now − 护栏` 早于旧游标时停在旧游标上；
        # 忙店走 `max(createTime)` 分支时护栏与钳制都不参与（既有行为逐字不变）。
        new_cursor = guarded if guarded > cursor else cursor
    else:
        new_cursor = jidanpu.format_log_time(latest)
    if retry_from is not None:
        # 日志查询包含游标这一秒，下一轮还能收到被临时占用的那一行。
        new_cursor = max(cursor, min(new_cursor, jidanpu.format_log_time(retry_from)))
    JidanpuMemberBinding.objects.filter(pk=binding.pk).update(log_cursor=new_cursor)


# ── ⭐⭐ 机器接手：抽一个共享实现（D-0226；钱路径只许一份）────────────────────
# 把①式日志快路里「`claim_order` 调用+全部守卫+审计+台账」那一整块抽成模块级函数（`machine_claim_order`），①式的 `_handle_alarm` 与②式官方API的 `jidanpu_member_openapi._apply_member_status` 共用这一份：守卫一个不少、逐字照搬——`transaction.atomic()` 内 `claim_order(user_id=binding.user_id, nodes=nodes_of(binding.user), enforce_limits=False)`（裁定 8：只跳过按人的四道抢单闸门，其余判据一道不跳）；`ClaimError`/`IllegalTransition`/`IntegrityError`（同 person 撞 `uniq_performance_order_user`）三类可预期失败一律 `transaction.set_rollback(True)`（E-0022）；成功支写订单审计（`MACHINE_ACTOR`、`actor_id=None`、`system=True`）与台账 `KIND_REMOTE_TAKEN`（`direction="in"`、`status="done"`）——与接手同一个事务（照 B-1：标记与写入分开提交会在两者之间挂掉的窗口里留下「审计已提交、标记缺失」⇒ 下一轮重复接手一次）。
# 接手对象永远是 `binding.user`（钱在我方算）：远端接单人的 uid/显示名只进审计与台账留痕、不参与接手对象选择 ⇒ ②式拿不到接单人身份不构成障碍（spec §2.1）。⭐ 对外回声与①式同流（spec §2.5）：`claim_order` 内部的 `apply_transition`/`queue_claimed` 照常跑 ⇒ 两条式排出的 outbox 事件同一形状 ⛔ 不新增也不抑制；⚠️ D-0227/E-0236：上一句对②式镜像接手不成立——那一条调用点传 `suppress_outbound_sync=True`（镜像接手不对对方系统产生任何写动作）；①式快路照旧不传（它的回声是既有行为）。


@dataclass(frozen=True)
class MachineClaimOutcome:
    """一次机器接手（`machine_claim_order`）的结论：成功，或三档可预期拒绝之一。⚠️ 只回结论，不替调用方写台账：①式的收口逻辑（竞态分流/排退单）与②式的（`findings`+`OUTCOME_NOTED`）处置不同，各自由调用方落。"""

    #: 接手成功（审计 + 台账已在同一事务里写完）。
    claimed: bool
    #: `claim_order` 抛的 `ClaimError`（条件更新 0 行：已被接走 / 不在抢单大厅 / 没有 MAIN 行）。
    claim_error: ClaimError | None = None
    #: 状态机 / 节点门拒绝（`IllegalTransition`，多半是该账号缺节点）。
    denial: IllegalTransition | None = None
    #: 撞了 `uniq_performance_order_user`（同一张单、同一个人只有一条绩效行）。
    same_person_conflict: bool = False


def machine_claim_order(
        *, binding, order, remote_uid, remote_name, remote_order_id,
                        source_note: str,
                        suppress_outbound_sync: bool = False) -> MachineClaimOutcome:
    """机器接手：把 `order` 接手给 `binding.user`（⭐ ①式快路与②式官方API共用的那一份）。谁调它（⛔ 只有这两个调用方，钱路径不许出现第二份实现）：①①式日志快路 `_handle_alarm`（那一块原本就在这里，抽出后行为逐字不变）；②②式官方API `jidanpu_member_openapi._apply_member_status` 的中间档分支（D-0226：远端「有人在做」而我方还停在「待抢单」⇒ 同一条业务口径接手）。
    ⭐ 镜像卡派生的隐藏单（`order.hall_card` 非空）：这张单没有 MAIN 绩效行（它只是把镜像卡同步给下家大厅的载体）⇒ 原生 `claim_order` 的条件更新必然 0 行、下家一接就失败、卡永远回不来；这一档改走镜像 claim 路径：`peer_hall.claim_peer_mirror` 预占同一张卡并向本行上家排 `hall.claim`，上家受理后 `_promote_claiming_mirror` 会原地把隐藏单转正（挂打手/`IN_PROGRESS`/清 `hall_card`）——与「本租户打手在大厅里点接单」同一条路径，⛔ 不建 MAIN 行、⛔ 不另抄一份接手逻辑；⚠️ 成功判据是「卡被预占+`hall.claim` 已排」，不是「订单已 `IN_PROGRESS`」（后者要等上家受理）；台账人话用 `AUTO_CLAIM_LEDGER_MIRROR_REASON`（⛔ 不是普通单那句「钱在我方按绩效算」）；`enforce_limits`/`enforce_deposit` 都传 `False`（裁定 8 口径）。参数：`remote_uid`/`remote_name`/`remote_order_id` 只进台账 payload 留痕（②式的 `GetOrderStatus` 回包恰好 5 格、拿不到接单人身份 ⇒ 前两样传 `None`）；`source_note`=接手出处的人话短句，进订单审计 `reason`（排障时回答「这次接手是被什么触发的」⛔ 不含凭据、不含远端整行）；`suppress_outbound_sync`（⭐ D-0227/E-0236）透传给 `claim_order`——只有②式镜像接手那一档传 `True`（对方接走 ⇒ 我方记账，不该对对方系统产生任何写动作），①式快路不传（日志快路看到的也是对方打手接单，但那一档的回声 assign/unlist/status_out 是既有行为——①式有大厅监视与 `_consume_unlist` 的 verdict 回读闸兜着）。
    ⭐ 上家镜像卡派生的隐藏单（D-0325；`order.upstream_hall_card` 非空）：与 peer 镜像那一档同一条口径——没有 MAIN 绩效行、原生 `claim_order` 接不了 ⇒ 改走 `claim_upstream_mirror`（上家线的镜像 claim 路径：`listed → claiming` → 上家接单 → promote 复用同一张隐藏单转正）；分支在函数入口早退（⛔ 不在下面的 `atomic` 块里打上家 HTTP——peer 分支没有网络调用可以进事务，这条有；`claim_upstream_mirror` 自己管理事务与执行租约），细节见 `_machine_claim_upstream_mirror` 的 docstring。"""
    if order.upstream_hall_card_id is not None:
        # ⭐ D-0325：上家镜像卡派生的隐藏单——下家打手接走的副本源头是
        #    上家镜像卡 ⇒ 走上家线的镜像 claim 路径（同一张单接着用）。
        return _machine_claim_upstream_mirror(
            binding=binding, order=order, source_note=source_note,
            remote_uid=remote_uid, remote_name=remote_name,
            remote_order_id=remote_order_id,
        )
    claim_error = None
    denial = None
    claimed = False
    claiming_at = ""
    #: ⭐ 撞了「同一张单、同一个人只有一条绩效行」那条唯一约束（真机）——它与
    #: 「不在大厅 / 已被别人接走」**不是一回事**（那两句话会把人引到错的方向去查）。
    same_person_conflict = False
    with transaction.atomic():
        try:
            if order.hall_card_id is not None:
                # ⭐⭐ 隐藏单（镜像卡派生的载体）没有 MAIN 绩效行 ⇒ 原生 `claim_order` 的条件更新必然 0 行（下家一接就失败、卡永远回不来）。下家在对方鸡蛋铺里接走的是那张卡 ⇒ 走既有镜像 claim 路径：`claim_peer_mirror` 预占同一张卡（`listed → claiming`、`claiming_by`=这个打手）并向上家排 `hall.claim`；上家受理后 `_promote_claiming_mirror` 会原地把这张隐藏单转正（挂打手/`IN_PROGRESS`/清 `hall_card`）。
                #    ⚠️ `enforce_limits=False` 是裁定 8 的同一条口径（外部事实已定 ⇒ 跳过按人闸门）；`enforce_deposit=False` 同理（机器接手不碰我方打手钱包）；⛔ 不在这里建 MAIN 绩效行/不改 `Order.status`：那些是转正那一刻的事（`_promote_claiming_mirror` 单点），提前写会造出「有接单人、卡还在厅」；
                #    ⭐⭐ 节点门照走（与下面 `claim_order` 那条普通机器接手同一判据）——`claim_peer_mirror` 自己不判节点，而这条分支又绕过了 `claim_order`（后者把 `nodes_of(binding.user)` 交给状态机）⇒ 不补这一道，没有接单节点的账号也能把卡从大厅接走；判据本体仍是状态机那一处（`transition_denial`，`apply_transition` 用的就是它）：按 `WAITING_GRAB → IN_PROGRESS` 这条边的节点集判——有意不额外收紧 ⛔ 不在这里另写「必须持 `grab_hall.take_order`」的第二套判据；⚠️ 拒绝走既有出口：`IllegalTransition` ⇒ 调用方落 `denied`+`AUTO_CLAIM_DENIED_REASON`（与普通机器接手缺节点时的形态逐字同形）。
                from apps.business import peer_hall
                from apps.business.order_status import transition_denial

                denial_reason = transition_denial(
                    order.status, OrderStatus.IN_PROGRESS, nodes_of(binding.user)
                )
                if denial_reason is not None:
                    raise IllegalTransition(denial_reason)

                mirror = peer_hall.claim_peer_mirror(
                    user_id=binding.user_id, mirror_id=order.hall_card_id,
                    enforce_limits=False, enforce_deposit=False,
                )
                claiming_at = mirror.claiming_at.isoformat()
            else:
                # ⚠️ 节点门照走：`nodes` 由这个账号的职位算出来，状态机这一层的授权判据一条都不绕过。
                #    ⚠️ 裁定 8：机器接手跳过「按人」的抢单闸门（`enforce_limits=False`）——spec A6 的「对方打手接了 ⇒ 我方自动接手」是无条件的，而且那时外部事实已定（单已经在对方手里）：被「接单价区间/持单上限/禁自助接单/没有接单价」挡住会造出「我方没接手、对方在做」的错位 ⇒ 绩效/结算漏发；
                #    ⛔ 只跳这四道：来源态守卫、`rowcount == 0`（已被别人接走）、节点门、`apply_transition` 的复读与已结账闸全部保留（人类入口照旧默认 `True`）。
                claimed_order = claim_order(
                    user_id=binding.user_id, order_id=order.pk,
                    nodes=nodes_of(binding.user), enforce_limits=False,
                    # 机器接手不碰我方打手钱包：押金判据已移出 `enforce_limits`，这里必须显式关掉。
                    enforce_deposit=False,
                    suppress_outbound_sync=suppress_outbound_sync,
                )
        except ClaimError as exc:
            # 可预期失败：`claim_order` 的绩效条件更新**已经写过行**（它的 docstring 明说
            # 状态机判定在条件更新之后）⇒ 必须显式回滚，否则留下「绩效行有接单人、订单还在
            # 大厅」的脏行（E-0022）。
            transaction.set_rollback(True)
            claim_error = exc
        except IllegalTransition as exc:
            # 同上：缺节点 / 陈旧状态 ⇒ 那半截写入必须回滚，且这里**不许**让它裸逃
            # （`poll_log` 的调用方是后台轮询，一个裸逃会让这一轮后面所有行都发现不了）。
            transaction.set_rollback(True)
            denial = exc
        except IntegrityError:
            # ⭐ 真机：`claim_order` 的条件更新撞了 `uniq_performance_order_user`（同一张单、同一个人只能有一条绩效行——这个账号既录了这张单又绑成代练员）。
            #    ⛔ 绝不能让 `IntegrityError` 裸逃：裸逃会跳过 `_advance_cursor` ⇒ 同一行日志每 3 秒重来一次（真机现象：每 3~4 秒一条「快路出现未预期失败（IntegrityError）」），而绑定的 `last_error` 一个字都没有（异常越过了健康列的写入）⇒ 页面上完全看不出来；
            #    ⇒ 当可预期的拒绝处理：回滚+人话原因（下面的收口逻辑会落台账+`last_error`）。
            transaction.set_rollback(True)
            same_person_conflict = True
        else:
            # ⭐ 接手成功：**审计 + 台账与这次接手同一个事务**（照 B-1：标记与写入分开提交
            #    会让两者之间挂掉的窗口里「审计已提交、标记缺失」⇒ 下一轮重复接手一次）。
            if order.hall_card_id is None:
                record_successful_claim(
                    claimed_order, binding.user, price=claimed_order.hall_price,
                    downstream=downstream_claim_label(binding), sync_actor=MACHINE_ACTOR,
                )
            jidanpu_common.write_event(
                direction="in", kind=KIND_REMOTE_TAKEN, status="done",
                target_type="order", target_ref=order.order_no,
                # ⭐ 隐藏单那一拍只是**预占卡**（转正等上家受理）⇒ 用
                #    `AUTO_CLAIM_LEDGER_MIRROR_REASON`；普通单仍用「已自动接手 / 钱按绩效算」。
                reason=(
                    AUTO_CLAIM_LEDGER_MIRROR_REASON
                    if order.hall_card_id is not None
                    else AUTO_CLAIM_LEDGER_REASON
                ),
                payload={
                    "member_binding_id": binding.pk,
                    "claiming_mirror_id": order.hall_card_id,
                    "claiming_mirror_at": claiming_at,
                    "source_note": source_note,
                    "claiming_local_user_id": binding.user_id,
                    "claim_downstream": downstream_claim_label(binding),
                    "remote_uid": remote_uid,
                    "remote_name": remote_name,
                    "remote_order_id": remote_order_id,
                },
            )
            claimed = True
    return MachineClaimOutcome(
        claimed=claimed, claim_error=claim_error, denial=denial,
        same_person_conflict=same_person_conflict,
    )


def downstream_claim_label(binding):
    return user_display_name(binding.user).strip()


def record_promoted_member_claim(order, taker, mirror, price):
    """跨团团通镜像只有正式接手后才写业务日志。"""
    if mirror.claiming_at is None:
        return
    evidence = JidanpuSyncEvent.objects.filter(
        direction="in", kind=KIND_REMOTE_TAKEN, status="done", target_ref=order.order_no,
        payload__claiming_mirror_id=mirror.pk, payload__claiming_local_user_id=taker.pk,
        payload__claiming_mirror_at=mirror.claiming_at.isoformat(),
    ).order_by("-id").values_list("payload", flat=True).first()
    if evidence is not None:
        record_successful_claim(
            order, taker, price=price,
            downstream=evidence.get("claim_downstream", ""), sync_actor=MACHINE_ACTOR,
        )


def _machine_claim_upstream_mirror(
    *, binding, order, source_note, remote_uid, remote_name, remote_order_id,
) -> MachineClaimOutcome:
    """机器接手 · 上家镜像卡派生的隐藏单那一档（D-0325）。下家打手在对方鸡蛋铺里接走的副本，源头是上家大厅镜像卡（`Order.upstream_hall_card` 非空）⇒ 走既有 `jidanpu_upstream.claim_upstream_mirror`（`listed → claiming` → 上家接单 → `promote_upstream_mirror` 复用同一张隐藏单转正）⛔ 不建第二份接手逻辑、⛔ 不在这里建 MAIN 绩效行（转正那一刻的事）。
    节点门照走（与 peer 镜像分支/普通 `claim_order` 同一判据 `transition_denial`，按 `WAITING_GRAB → IN_PROGRESS` 这条边的节点集判 ⛔ 不另写第二套）；拒绝 ⇒ `denial`（调用方落 `denied`+既有文案）；`enforce_limits=False`+`enforce_deposit=False`（裁定 8：机器接手=外部事实已定，不碰按人闸门与钱包——与 `claim_order` 机器路径同一条口径）；⚠️ 上游 HTTP 不包 DB 事务：`claim_upstream_mirror` 自己管理事务与执行租约，把网络 I/O 包进外层 `atomic` 会把连接占死（这一档与 peer 分支的结构差异是有意的）；
    `ClaimError`（上游卡已被别人接走/正在处理/租约输家收敛）⇒ 交给调用方既有的 `claim_error` 分支收口——隐藏单没有 MAIN 绩效行 ⇒ `_handle_alarm` 会落「没有 MAIN 绩效行」那一档的响亮拒绝 ⛔ 不排退单（谁都没接手的单退掉对方副本=E-0236 的事故形状；「上游 gone+下家已接」的活单由 `_consume_withdraw` 的写前回读闸保护）；传输层/会话异常（`JidanpuError` 家族，含 `AuthError`）翻成可预期失败（`ClaimError(TEMPORARY_FAILURE_MESSAGE)`）⇒ 走同一套 `claim_error` 收口 ⛔ 不裸逃打挂整轮快路（后台轮询里一个裸逃会让这一轮后面所有行都发现不了）；成功 ⇒ 审计+`member.remote_taken` 台账（peer 分支同形：`record(...)` 机器来源+一次 `write_event`，文案用本档专属那句「已预占上家镜像卡…」）。"""
    from apps.business import jidanpu_upstream
    from apps.business.order_status import transition_denial

    denial_reason = transition_denial(
        order.status, OrderStatus.IN_PROGRESS, nodes_of(binding.user)
    )
    if denial_reason is not None:
        return MachineClaimOutcome(
            claimed=False, denial=IllegalTransition(denial_reason),
        )
    try:
        promoted_order = jidanpu_upstream.claim_upstream_mirror(
            user_id=binding.user_id,
            mirror_id=order.upstream_hall_card_id,
            enforce_deposit=False,
            enforce_limits=False,
            downstream=downstream_claim_label(binding),
            sync_actor=MACHINE_ACTOR,
        )
    except ClaimError as exc:
        return MachineClaimOutcome(claimed=False, claim_error=exc)
    except JidanpuError:
        # 上游这一跳的传输 / 会话失败：可预期失败口径（不解析文案、不裸逃）。
        return MachineClaimOutcome(
            claimed=False,
            claim_error=ClaimError(
                jidanpu_common.TEMPORARY_FAILURE_MESSAGE,
                ErrorCode.PLATFORM_QUERY_FAILED,
            ),
        )
    jidanpu_common.write_event(
        direction="in", kind=KIND_REMOTE_TAKEN, status="done",
        target_type="order", target_ref=promoted_order.order_no,
        reason=AUTO_CLAIM_LEDGER_UPSTREAM_MIRROR_REASON,
        payload={
            "member_binding_id": binding.pk,
            "source_note": source_note,
            "remote_uid": remote_uid,
            "remote_name": remote_name,
            "remote_order_id": remote_order_id,
        },
    )
    return MachineClaimOutcome(claimed=True)


def _handle_alarm(binding, link, *, row, client, now, surface_session_failure=False,
                  matching_operator=False):
    with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=link.order_id) as busy:
        if busy:
            return "busy", None
        if not _binding_is_current(binding):
            return "done_marker", None
        if not JidanpuMemberOrderLink.objects.filter(
            pk=link.pk, jidanpu_order_id=link.jidanpu_order_id, withdrawn_at__isnull=True,
        ).exists():
            return "done_marker", None
        return _handle_alarm_locked(
            binding, link, row=row, client=client, now=now,
            surface_session_failure=surface_session_failure, matching_operator=matching_operator,
        )


def _handle_alarm_locked(binding, link, *, row, client, now, surface_session_failure=False,
                         matching_operator=False):
    """一行命中的闹钟：回查远端真实状态 ⇒ 该接手就接手；回 `(结局, 附注)`。结局（`_poll_log` 按它分流）：`"done_marker"`=本快路早先已经处理过这张单（⛔ 不再打上游）；`"unreadable"`=上游抛错（传输/业务失败）⇒ 整轮中止、不推游标（下一轮重试）；`"unreadable_row"`=读到了但读不出结论（远端单查不到/接单人日志非空却没有可用的数值 `userId`）⇒ 台账 `failed`+`last_error` ⛔ 不接手、⛔ 不排退单、本轮继续；`"no_claim"`=远端真实状态本来就不该接手（已退单/还挂在他家大厅）⇒ 只更新台账；以及 `"not_taken"`/`"claimed"`/`"ours"`/`"raced"`/`"refused"`/`"denied"`/`"deleted"`。
    ⚠️ `row` 只用来记「哪一行」的上下文（`joinOrderId` 进台账 payload）；⛔ `remark` 一个字都不参与判据（A9 的硬要求）。"""
    order = link.order
    if order.status == OrderStatus.WAITING_GRAB and jidanpu_relaunch.claim_blocked(binding, link):
        return "done_marker", None
    if _remote_taken_done(binding, order.order_no, listed_at=link.listed_at):
        return "done_marker", None
    if getattr(order, "deleted_at", None) is not None:
        # 已软删的单取不到（`claim_order` 走默认 manager ⇒ `DoesNotExist`）⇒ ⛔ 不去接手，
        # 也⛔ 不白打一次上游。
        _note_order_once(
            binding=binding, order=order, reason=ORDER_MISSING_REASON, status="skipped"
        )
        return "deleted", None

    remote_id = link.jidanpu_order_id
    # ① **远端这张单的真实状态**（spec §6.1 的 ①）：⛔ 不解析那行日志文本（A9）。
    #    照 `jidanpu_hall.discover_remote_taken` 的读法：`isThrowOrder == "Y"` = 还挂在他家大厅。
    try:
        found = jidanpu.fetch_order_by_key1(
            client, key=remote_id, expect_field="orderId", expect_value=remote_id
        )
    except JidanpuError as exc:
        reason = f"{REMOTE_ORDER_UNREADABLE_REASON}（{type(exc).__name__}）"
        _note_order_once(binding=binding, order=order, reason=reason, status="failed")
        if surface_session_failure and isinstance(exc, (AuthError, UpstreamBlockedError)):
            raise
        return "unreadable", reason
    if found is None:
        # ⚠️ **查不到 ≠ 远端没有这张单**（`fetch_order_by_key1` 只在第一页找、时间窗是调用当天
        #    往前滚约 90 天 ⇒ 老单会拿到 `None`）⇒ 一律当**读不到**处理：台账 + `last_error`、
        #    ⛔ 不接手（把「读不到」当成「没被接走」会把一张真被接走的单永远漏掉）。
        _note_order_once(
            binding=binding, order=order, reason=REMOTE_ORDER_NOT_FOUND_REASON,
            status="failed",
        )
        return "unreadable_row", REMOTE_ORDER_NOT_FOUND_REASON
    current_taker_name = ""
    if matching_operator:
        # 人工和同步可使用同一个鸡蛋铺账号。回读须确认本轮上厅后的真实接手。
        current_taker_name = _relaunch_assignment(
            found, not_before=link.listed_at.replace(microsecond=0),
        )
        if not current_taker_name:
            return "done_marker", None
    remote_state = _order_sta_token(found)
    if remote_state == jidanpu.ORDER_REFUNDED_LABEL:
        # spec §6.1 的 ③：远端**已退单** ⇒ 只更新台账、我方大厅不动（⛔ 不接手、⛔ 不排退单）。
        _note_order_once(
            binding=binding, order=order, reason=REMOTE_ORDER_REFUNDED_REASON,
            status="skipped",
        )
        return "no_claim", None
    if str(found.get("isThrowOrder", "") or "") == "Y":
        # spec §6.1 的 ③：远端**还挂在他家大厅** ⇒ 没人接走（接单人日志可能是上一轮的旧人）
        # ⇒ 只更新台账、⛔ 不接手。
        _note_order_once(
            binding=binding, order=order, reason=REMOTE_STILL_IN_HALL_REASON,
            status="skipped",
        )
        return "no_claim", None
    if str(found.get("isThrowOrder")) != "N" or not _is_remote_assignment_state(remote_state):
        return "no_claim", None
    if not current_taker_name:
        assigned_at = _log_moment(found.get("assTime"))
        if (
            link.listed_at is not None and assigned_at is not None
            and assigned_at < link.listed_at.replace(microsecond=0)
        ):
            return "no_claim", None
        current_taker_name = str(found.get("recManName") or "").strip()

    # ② **谁接了**（spec §6.1 的 ②）：`GetOrderAssLogs` 的数值 `userId` 是唯一稳定来源。
    try:
        logs = jidanpu.fetch_ass_logs(client, remote_order_id=remote_id)
    except JidanpuError as exc:
        # ⚠️ 「读不到」**不是**「远端没接」（拿到 `None` / 空不许当成否定结论）⇒ 响亮。
        reason = f"{REMOTE_STATE_UNREADABLE_REASON}（{type(exc).__name__}）"
        _note_order_once(
            binding=binding, order=order, reason=reason, status="failed"
        )
        if surface_session_failure and isinstance(exc, (AuthError, UpstreamBlockedError)):
            raise
        return "unreadable", reason

    # ⭐ 审计句的 uid 与显示名必须取自同一行接单人日志：这条自动接手的订单审计是钱的动作（自动接手 ⇒ 绩效/结算）的留痕（spec §8.3）——两样来自不同的行就是把两个人拼成一个人，排障时会指向一个没干这件事的下家打手；
    #    ⚠️ 上游确实存在「某一行只有显示名、`userId` 空」的形态（下面「一行都没有可用 uid」那一支就是为它写的）⇒ 先选出第一条有可用数值 uid 的行，两样再从它身上取。
    withdrawn_at = jidanpu_relaunch.listing_withdrawal_confirmed_at(binding, link)
    if withdrawn_at is not None:
        current_taker_name = _relaunch_assignment(
            found, not_before=withdrawn_at.replace(microsecond=0),
        )
        if not current_taker_name:
            return "no_claim", None
    if current_taker_name:
        taker_row = _confirmed_assignment_taker(logs, current_taker_name)
    else:
        taker_row = next(
            (item for item in logs if _uid_of(item.get("userId")) is not None), None
        )
    if taker_row is None:
        if logs:
            # ⚠️ **非空但没有可用的数值 `userId`**：这不是「健康地没被接走」，是**读不到**
            #    （可能上游改了字段名 / 那一行只有显示名）⇒ 台账 + `last_error`（也会让本轮
            #    **不清** `last_error`），⛔ 不接手。
            _note_order_once(
                binding=binding, order=order, reason=REMOTE_TAKER_UNREADABLE_REASON,
                status="failed",
            )
            return "unreadable_row", REMOTE_TAKER_UNREADABLE_REASON
        # 空日志：照 B-1 的 `B1_NO_TAKER_REASON` 口径——记一条人话台账、下一轮再判。
        _note_order_once(
            binding=binding, order=order, reason=REMOTE_NOT_TAKEN_REASON, status="skipped"
        )
        return "not_taken", None

    remote_uid = _uid_of(taker_row.get("userId"))
    remote_name = str(taker_row.get("userName") or "")
    if order.status == OrderStatus.WAITING_GRAB and jidanpu_relaunch.claim_blocked(binding, link):
        return "no_claim", None

    # ⭐ D-0226：这一块抽成了共享的 `machine_claim_order`（见它那一段的注释）；本调用点
    #    （①式日志快路）行为**逐字不变**——守卫、审计与台账都在那一份里。
    outcome = machine_claim_order(
        binding=binding, order=order, remote_uid=remote_uid, remote_name=remote_name,
        remote_order_id=remote_id,
        source_note=f"uid={remote_uid} 显示名={remote_name}",
    )
    claimed = outcome.claimed
    same_person_conflict = outcome.same_person_conflict
    claim_error = outcome.claim_error
    denial = outcome.denial

    if claimed:
        return "claimed", None
    if same_person_conflict:
        # ⛔ 走**自己的**那句话（下面的 `AUTO_CLAIM_REFUSED_REASON` 说的是「不在大厅 / 已被别人
        #    接走」，与这条成因不符 —— 错根因比没有根因更贵）。
        _note_order_once(
            binding=binding, order=order,
            reason=AUTO_CLAIM_SAME_PERSON_REASON, status="failed",
        )
        return "refused", AUTO_CLAIM_SAME_PERSON_REASON
    if claim_error is not None:
        taker_id = _main_taker_id(order.pk)
        if taker_id == binding.user_id:
            # 接单人**就是**他（他自己在我方大厅接的 / 本快路早先接过）⇒ 落终态标记，⛔ 不退单。
            _note_order_once(
                binding=binding, order=order,
                reason=REMOTE_TAKEN_OURS_REASON, status="done",
            )
            return "ours", None
        if taker_id is None:
            # ⚠️ 接手人读不出来 ≠ 我方赢了：这条分支里两种成因都不是 A10 竞态：①`ORDER_ALREADY_TAKEN`+没有接手人 ⟺ 条件更新 0 行、而这条单一条 MAIN 绩效行都没有（孤儿订单；「无主的 MAIN 行被整条删掉」是业务方裁定的正常数据形态，E-0148）；②`BUSINESS`（该订单不在抢单大厅）+没有接手人 ⟺ 座位已被撤回/回池；
            #    ⛔ 两种都不许排 `member.withdraw`：竞态那条的语义是「我方别的打手接了」，在谁都没接手时排退单会让 `_consume_withdraw` 真的把对方那张单退掉，而台账还写着「我方已有别的打手接手」（一句假话）；
            #    ⇒ 一律走响亮拒绝：台账 `failed`+`last_error` ⛔ 不排退单。
            prefix = (
                AUTO_CLAIM_NO_TAKER_ROW_REASON
                if claim_error.code == ErrorCode.ORDER_ALREADY_TAKEN
                else AUTO_CLAIM_REFUSED_REASON
            )
            reason = f"{prefix}{claim_error}"
            _note_order_once(binding=binding, order=order, reason=reason, status="failed")
            return "refused", reason
        if claim_error.code == ErrorCode.ORDER_ALREADY_TAKEN:
            # ⭐ A10 竞态：我方别的打手先接走了（`taker_id` 是别人，上面两条已排除「就是他」与「根本没有这行」）⇒ 我方赢了 ⇒ 去对方把这张单退掉；
            #    原因由 `_consume_withdraw` 用既有那句 `jidanpu_common.WITHDRAW_REMARK`（spec §13-T6 默认沿用）——⛔ 不在这里另写一份文案。
            queue_member_event(order, binding, kind=KIND_WITHDRAW)
            _note_order_once(
                binding=binding, order=order,
                reason=REMOTE_TAKEN_RACE_REASON, status="skipped",
            )
            return "raced", None
        reason = f"{AUTO_CLAIM_REFUSED_REASON}{claim_error}"
        _note_order_once(binding=binding, order=order, reason=reason, status="failed")
        return "refused", reason
    reason = f"{AUTO_CLAIM_DENIED_REASON}{denial}"
    _note_order_once(binding=binding, order=order, reason=reason, status="failed")
    return "denied", reason


def _link_index(binding, *, remote_keys, source_keys):
    """按本轮日志行实际出现的单号**点查**链路的两份视图（⛔ 不再全量拉整条绑定建索引——那是快路每 3 秒两次全表扫的实测缺陷）。远端臂走唯一索引 `uniq_jdp_member_link_remote (binding, jidanpu_order_id)`；来源臂先按**有效来源单号**圈出订单再取链路，键=`effective_source_order_no`（与我们写进对方 `souresOrderID` 的值逐字一致 ⛔ 不许再用 `order_no` 当第二把尺子）；预筛走 `Order.all_objects` 与旧全量 JOIN 的语义逐字对齐：来源号命中**软删单**时链路仍进候选、由 `_handle_alarm` 的 `deleted_at` 分支写台账（默认 manager 会把那一路静默吞掉）。链路按 `id` 升序 ⇒ 两张我方单来源单号相同时取最早那条；残差台账只在「本轮键集含撞号键」的轮里写（`_note_binding_once` 去重不变，同一句话仍只写一条）。⚠️ 单号按裸值匹配：库里存了带首尾空白的单号存量会对不上，由慢路对账兜。"""
    by_remote: dict[str, JidanpuMemberOrderLink] = {}
    if remote_keys:
        by_remote = {
            link.jidanpu_order_id: link
            for link in JidanpuMemberOrderLink.objects.filter(
                binding=binding, jidanpu_order_id__in=remote_keys
            ).select_related("order")
        }
    by_order: dict[str, JidanpuMemberOrderLink] = {}
    if source_keys:
        order_ids = Order.all_objects.filter(
            Q(source_order_no__in=source_keys) | Q(order_no__in=source_keys)
        ).values_list("pk", flat=True)
        collisions: list[str] = []
        for link in (
            JidanpuMemberOrderLink.objects.filter(binding=binding, order_id__in=order_ids)
            .select_related("order")
            .order_by("id")
        ):
            key = effective_source_order_no(link.order)
            if key in by_order:
                collisions.append(key)
                continue
            by_order[key] = link
        if collisions:
            _note_binding_once(
                binding=binding,
                reason=DUPLICATE_SOURCE_ORDER_NO_REASON.format(value=collisions[0]),
                status="failed",
            )
    return by_remote, by_order


class _LastCallOutcome:
    """本拍「快路/回拉」的实况：有没有真的打过上游、有没有撞过被吞掉的拦截。⚠️ 为什么需要它：`poll_log`/`pull_statuses` 的返回契约是「失败回 0」，而调度层必须把三种回 0/非 None 分开：①空转早退（`_pull_statuses` 一条待处理链路都没有 ⇒ 一次上游都没打）⛔ 不算真跑过；②上游报错被吞（含 468）⛔ 不许当干净轮、更不许复位连续拦截计数（否则生产上「下家已有会话后被拦」会把共享的计数清零，永远攒不到阈值）；③真的调通了、只是没有可写的东西 ⇒ 算真跑过；
    ⛔ 别拿返回值 `> 0` 当判据：那会把 ③ 判成没跑（真实成功永远不复位）；用法：调度层每轮开头 `reset()`、结束时 `take()` 取走并清零（进程内单线程调度）；
    ⚠️ 只覆盖快路/回拉两段；`run_outbox`/②式逐单查询仍各自在内部吞 468（见 RUNBOOK §3.0.2「已知覆盖缺口」）。"""

    def __init__(self):
        #: 最近一次**真实建连**用的出口（`_open_client_pair` 烙在客户端上的那一格）。
        #: ⚠️ **`reset()` 不清它**：出口归属要跨轮有效（池里的会话是跨轮复用的），
        #:    每轮开头抹掉就等于退回「按当前出口记」——正是这条口径要堵的洞。
        self.outlet: str | None = None
        self.reset()

    def record_outlet(self, outlet) -> None:
        """记下「这一发用的是哪个出口」（只认字符串：替身客户端对任何属性都回 Mock）。"""
        self.outlet = outlet if isinstance(outlet, str) and outlet else None

    def reset(self) -> None:
        self.upstream = False
        self.blocked = False

    def take(self) -> tuple[bool, bool]:
        """取走 (真的打过上游, 撞过被吞的拦截)，并把两个标记清零。"""
        outcome = (self.upstream, self.blocked)
        self.reset()
        return outcome


#: 进程内单例：⛔ 不是业务状态，只给调度层判「这一轮算不算真跑过」（见类 docstring）。
LAST_CALL = _LastCallOutcome()


def _poll_log(binding, *, client, now, identity=None) -> int:
    """一轮快路的实做（建连已由 `poll_log` 处理）。回**命中本线并处理过的行数**。"""
    #: ⭐ 记下这一发用的是**哪个出口**（468 归属按它记，与上家同一口径）。
    LAST_CALL.record_outlet(getattr(client, "_tt_egress_outlet", None))
    # ⚠️ 游标是**持久状态**，以库里的值为准（调用方手里那个实例可能是上一轮之前的旧对象
    #    —— 只读内存里的 `binding.log_cursor` 会让常驻进程里的游标**永远不前进**）。
    bindings = (
        _binding_credentials_match(binding) if identity is not None
        else JidanpuMemberBinding.objects.filter(pk=binding.pk)
    )
    stored = bindings.values_list("log_cursor").first()
    if identity is not None and stored is None:
        return 0
    cursor = ((stored[0] if stored else "") or "").strip() or jidanpu.format_log_time(
        binding.created_at or now
    )
    if identity is not None:
        identity.current_name()
        if identity.binding_changed:
            return 0
    try:
        rows = jidanpu.fetch_system_logs(client, stime=cursor)
    except JidanpuError as exc:
        if identity is not None and isinstance(exc, (AuthError, UpstreamBlockedError)):
            raise
        # ⚠️ 468 在这里**被吞掉**（返回契约是「失败回 0」）⇒ 调度层必须知道这一拍撞过拦截
        #    （⛔ 不许把它当干净轮、⛔ 更不许复位连续拦截计数）。
        if isinstance(exc, UpstreamBlockedError):
            LAST_CALL.blocked = True
        return _fail_poll(binding, now=now, reason=LOG_FETCH_FAILED_REASON, detail=exc)
    #: ⭐ 这一发**真的调通了上游**（= 全量请求发出去了、拿到回包）⇒ 算「真跑过」
    #:   （⛔ 别拿「处理了多少行」当判据：0 行也可能是一次成功调用）。
    LAST_CALL.upstream = True
    if identity is not None:
        identity.current_name()
        if identity.binding_changed:
            return 0

    parsed: list[tuple[datetime, dict]] = []
    #: 解析不了 `createTime` 的行：行级隔离（记一条台账+跳过这一行）⛔ 不许它把整条绑定的日志快路永久停摆。⚠️ 原实现是 `return _fail_poll(...)`（不推游标）：游标是持久状态、畸形行永远落在 `[游标, now]` 里 ⇒ 下一轮拉到同一行 ⇒ 游标永不前进、无终态；
    #: 现在：只跳过这一行，别的行照常处理、游标按本批可解析的最大 `createTime` 推进（本批全解析不了 ⇒ 推到 `now`）。
    skipped_rows: list[str] = []
    for row in rows:
        moment = _log_moment(row.get("createTime"))
        if moment is None:
            reason = _bad_row_reason(row)
            # 同一行内容每轮算出同一个串 ⇒ `_note_binding_once` 的去重保证只记一条
            # （⛔ 不会每 3 秒重复记同一行坏行）。
            _note_binding_once(binding=binding, reason=reason, status="skipped")
            skipped_rows.append(reason)
            continue
        parsed.append((moment, row))

    #: 本轮实际出现的键（与 `_matched_link` 同一读法）；两个集合都空 ⇒ 一条链路查询都不发。
    remote_keys: set[str] = set()
    source_keys: set[str] = set()
    for _moment, row in parsed:
        join = str(row.get("joinOrderId") or "")
        if join:
            remote_keys.add(join)
        source = str(row.get("soureOrderId") or "")
        if source:
            source_keys.add(source)
    if remote_keys or source_keys:
        by_remote, by_order = _link_index(
            binding, remote_keys=remote_keys, source_keys=source_keys
        )
    else:
        by_remote, by_order = {}, {}
    seen: set[tuple] = set()
    processed = 0
    retry_from = None
    #: 本轮撞上的**响亮失败**（被 `claim_order` 拒 / 状态机拒绝 / **读到了但读不出结论**）
    #: ——它不中止本轮（别的单照跑），但轮末必须写进 `last_error`（⛔ 不许只留一行台账就算完）。
    #: ⚠️ 坏行也进这里：留痕之外要让页面上看得见（下一轮不再出现坏行时它会被自动清掉）。
    loud: str | None = skipped_rows[0] if skipped_rows else None
    # 顺序固定（时间 → 远端单号 → 文本）：同一秒多行的处理顺序不随上游回包顺序漂移。
    for moment, row in sorted(
        parsed,
        key=lambda pair: (
            pair[0],
            str(pair[1].get("joinOrderId") or ""),
            str(pair[1].get("remark") or ""),
        ),
    ):
        key = _row_key(row)
        if key in seen:
            continue
        seen.add(key)
        link = _matched_link(row, by_remote, by_order)
        if link is None:
            continue  # 不是本线录进去的单（对方自家别的业务）
        name = None
        if identity is not None:
            name = identity.current_name()
            if identity.binding_changed:
                return processed
        matching_operator = _echo_by_name(row, binding, verified_name=name)
        if matching_operator and (
            not link.in_hall or link.listed_at is None
            or link.order.status != OrderStatus.WAITING_GRAB
        ):
            continue
        outcome, detail = _handle_alarm(
            binding, link, row=row, client=client, now=now,
            surface_session_failure=identity is not None,
            matching_operator=matching_operator,
        )
        if outcome == "unreadable":
            # 「读不到远端状态」⇒ 响亮 + **不推游标**（那一行下一轮还会回来重判）。
            _stamp_poll_health(binding, now=now, ok=False, reason=detail)
            return processed
        if outcome == "busy":
            if retry_from is None:
                retry_from = moment
            continue
        if outcome == "done_marker":
            # 早先那一轮已经处理过这张单 ⇒ 本轮**不算**处理（否则「处理了几行」会虚高）。
            continue
        if outcome in ("refused", "denied", "unreadable_row"):
            # 这三档都是**响亮**的（台账 `failed` + 轮末 `last_error`）：⛔ 不许把
            # 「读到了但读不出结论」（`unreadable_row`）当成「健康地没事」。
            loud = loud or detail
        processed += 1

    _advance_cursor(binding, parsed, cursor=cursor, now=now, retry_from=retry_from)
    _stamp_poll_health(binding, now=now, ok=loud is None, reason=loud or "")
    return processed


def poll_log(binding, *, now, client=None, verify_identity=False) -> int:
    """跑一轮日志快路，回命中本线并处理过的行数（spec §6.1）。流程：`stime = log_cursor`（空 ⇒ 绑定时刻）→ 拉系统操作日志（`logType=10`）→ 按 `LOG_DEDUPE_KEYS` 去重（同一秒多行都要处理到）→ 只留「能对上本线链路」的行（`joinOrderId`=链路远端单号，或 `soureOrderId`=有效来源单号）→ 回声抑制一道 → 逐行回查远端真实状态 ⇒ 该接手就接手；正常走完把游标推到窗口里最大的 `createTime`（窗口为空、或这一轮没有比上一轮更新的行 ⇒ 推到 `now − 落库延迟护栏`，避免窗口一轮比一轮长；护栏秒见 `_advance_cursor`）。
    ⚠️ `client` 是为可测性留的注入口（照 `detect_binding`/`consume_event` 的形状）：给了就用它、没给就自己 `jidanpu.member_session(binding)` 建连（同一绑定的会话在进程内复用）；⛔ 它不是业务参数（凭据只从 `binding` 那一行取，spec §8.5）；⚠️ 绑定停用（`enabled=False`）⇒ 一次上游都不打（spec §8.4：停用停止快路）回 0；
    ⚠️ 预期失败（上游读不到/缺节点）⇒ 台账+`last_error`、不抛（后台轮询要能继续跑别的绑定）；未预期异常（我们自己的编程错误）照旧裸逃、让调用方响亮地 500。"""
    now = now or timezone.now()
    if not binding.enabled:
        return 0
    with _connection(binding, client) as (sdk, _http):
        identity = _FastpathIdentity(binding, sdk) if verify_identity else None
        try:
            return _poll_log(binding, client=sdk, now=now, identity=identity)
        except (AuthError, UpstreamBlockedError) as exc:
            if identity is not None:
                identity.invalidate()
                _fail_poll(binding, now=now, reason=LOG_FETCH_FAILED_REASON, detail=exc)
            raise


# ══ 状态回拉（慢路；plan `2026-10-03-member-status-scope`/spec §6.2/§9.3）═══ 每一轮（默认 180 秒，或页面上点了「立即同步」）：①取该绑定链路名下、且我方还是非终态的单（`PULL_TERMINAL_STATUSES` 排除已完成/已退单，已软删的单也排除）；②按完成时间（`timeType=30`）拉整店「已完成」那一档（逐页拉全，判据照 `jidanpu_reconcile._fetch_label`）；③用远端 `orderId` 建索引，逐条链路：命中且那一行 `orderSta == 已完成` ⇒ 写「待验收」（`PENDING_REVIEW`，带 `nodes_of` 的节点门）；已经是待验收 ⇒ 一个字都不写（幂等）。
# ⚠️ 为什么不逐张查了（旧形态的病）：旧形态对链路表里每一行发一次 `fetch_order_by_key1`（O(链路数)、无游标、无预算），而这条常驻一个进程管所有绑定 ⇒ 打手做得越久越慢，别人的 3 秒快路被拖慢；且「下游已完成」那一档根本写不动（旧映射写 `COMPLETED`，而打手自己那套节点没有 `order.change_status_completed`）；⚠️ 处理集合是钱的承重判据（裁定 9）：我方 `COMPLETED`/`REFUNDED` 的单一个字节都不写——把一张已退单的单写回「待验收」=复活一张退掉的单，而退款那条路是裸 `update()`、跑不到退款结清 ⇒ 钱会算错；⚠️「已退单连查都不查」这条旧判据在整表形态下不再成立（窗口是整店口径、必然带回别的单的行），能保留且必须保留的是「一个字都不写」；⚠️ 写之前还要再过一道「已结账不回退」（spec §6.2 第 4 步第二道闸）：用库里最新的那一行跑 `jidanpu_common._b1_status_rollback_blocked`（E-0148/D-0156 换来的口径 ⛔ 不在这里另写一份）——集合是取数之前筛的，而「筛 → 拉整表 → 写」之间隔着网络往返。
# ⚠️ 失败一律响亮（spec §10 同一条口径）：拉不全/上游报错/缺节点 ⇒ 台账（`member.status_in`）+`last_error`（收窄写入 ⛔ 不整行 `save()`）⛔ 不许静默跳过；⚠️ 水位只在「拉全且没有 loud」时前进（`status_pull_at`）：否则下一轮的窗自然变大 ⇒ 不漏；⚠️ 处理集合为空时也前进（plan §4 Task B-补 第 3 条）：集合空 ⇒ 现存链路没有一张需要拉，不前进的话长期闲置的绑定下次有单时会从很旧的水位算起、一次拉回最长的整店「已完成」；⚠️ 窗=`min(now−3 天, 水位)`（量窗长度），实发日期串跨度卡 460 天（卡口留 3 天死区）；⚠️ 代价如实登记（控制器裁定 5）：`nodes` 取的是这个代练员账号的节点集，该账号没有对应节点时 `apply_transition` 会拒（`IllegalTransition`）⇒ 这条状态回不来，台账与页面上的 `last_error` 就是给人看这件事的；⚠️ 本模块⛔ 不写「谁点了按钮」的操作日志：状态变化本身由这里的 `record(...)` 留痕（动作=`order.transition`，操作人=机器名）；端点自己那一跳的分类见 `tests/test_operation_log_ledger.py` 的 `INSTRUMENTED`。

#: 状态码 → 我方 `OrderStatus`（spec §7 的表+旧 spec §7.2 的完整 13 码表）。⚠️ 新 spec §7 的表是摘要：它省略了 `DLZ`（代练中）与 `WFP`（未分配）两行，而这两行在旧 spec §7.2（本文引用的权威来源）里有定义；⛔ 漏掉 `DLZ` 会把我方自己刚推过去的代练中认成兜底问题单（一次回拉就把正常单标成问题单，而第二次就再也回不去）；⚠️「已暂停/已完成」两档的码（`YZT`/`YWC`）出自 `docs/JIDANPU-API.md` §6.1 的实测表；
#: ⭐ 本线不再用它拉状态（新形态只筛「已完成」那一档、只有一个目标态），但这张表必须留着：它是上家线的底表（`jidanpu_upstream.py:1767` 把本表并入它自己的表），删了上家线取值即坏；本模块 `_adopted_throw_verdict`（认领裁决）也还在用（见 `map_remote_status`）；
#: ⚠️ 同族教训：删任何跨模块可见的常量前，判据是全仓 grep 命中清单 ⛔ 不是「本模块里没人用了」。
REMOTE_STATUS_MAP: dict[str, str] = {
    "WFP": OrderStatus.UNASSIGNED,
    "DSH": OrderStatus.IN_PROGRESS,
    "DLZ": OrderStatus.IN_PROGRESS,
    "DYS": OrderStatus.PENDING_REVIEW,
    "YWC": OrderStatus.COMPLETED,
    "YZT": OrderStatus.PAUSED,
    "WTD": OrderStatus.ISSUE,
    "YTD": OrderStatus.REFUNDED,
    "CXZ": OrderStatus.ISSUE,
    "ZCZ": OrderStatus.ISSUE,
    "YZC": OrderStatus.ISSUE,
    "YCX": OrderStatus.ISSUE,
    "YCZ": OrderStatus.ISSUE,
}

#: 远端那一格**实际下发**的是中文标签（`orderSta`，spec §5.7 实测；T7 的 `_handle_alarm`
#: 也按 `已退单` 比）⇒ 同一张表必须同时认中文，⛔ 只认码会让真实回包**全部**落到兜底。
REMOTE_STATUS_LABELS: dict[str, str] = {
    "未分配": OrderStatus.UNASSIGNED,
    "待上号": OrderStatus.IN_PROGRESS,
    "代练中": OrderStatus.IN_PROGRESS,
    "待验收": OrderStatus.PENDING_REVIEW,
    "已完成": OrderStatus.COMPLETED,
    "已暂停": OrderStatus.PAUSED,
    "问题单": OrderStatus.ISSUE,
    "已退单": OrderStatus.REFUNDED,
    "撤销中": OrderStatus.ISSUE,
    "仲裁中": OrderStatus.ISSUE,
    "已仲裁": OrderStatus.ISSUE,
    "已撤销": OrderStatus.ISSUE,
    "异常中": OrderStatus.ISSUE,
    # ⚠️「待抢单」是**显示层**对 `orderSta` 的覆盖（旧 spec §7.2.1 读码：`isThrowOrder == "Y"`
    #    且 `isPushJdp != "Y"` 时列表把它显示成待抢单），数据里没有这个值。
    #    列在这里只为「万一对方真下发」时不把它当成认不出来的码。
    "待抢单": OrderStatus.WAITING_GRAB,
}

#: 认不出来的码的兜底（spec §7 末行：⇒ **问题单** + 台账人话原因；⛔ 不猜、⛔ 不改 `COMPLETED`）。
#: ⭐ 与上面两张表同一条：**留着**（上家线 `jidanpu_upstream.py:1882` 直接用它）。
UNKNOWN_REMOTE_STATUS = OrderStatus.ISSUE

#: 不进处理集合的两个我方终态：`COMPLETED`+`REFUNDED`（plan §1.1-4/§4 Task B 第 2 步）。⛔ `REFUNDED` 必须在这里（裁定 9 的钱那一半）：整表形态会把整店的行带回来，一旦让它进集合，一张我方已退单的单就会被写成「待验收」=复活一张退掉的单，而退款那条路是裸 `update()`、跑不到退款结清 ⇒ 钱会算错；
#: ⚠️ `COMPLETED` 同批排除（plan §1.1-4 逐字「我方已是『已完成』的单一个字都不写」）——它取代了旧形态的「`COMPLETED` 在扫描面里、由写前复核按已结账/未结账分流」那一档（D-0156 的那条分流不再落在本路；写前复核仍然作为第二道闸留着，见 `_pull_statuses`）；
#: ⚠️ `OrderStatus.REFUNDED` 与 `jidanpu_common.B1_ALWAYS_BLOCKING_STATUS` 是同一个值（后者是那条裸 `update()` 退单路径自己的口径名，见它那里的注释）。
PULL_TERMINAL_STATUSES = frozenset({OrderStatus.COMPLETED, OrderStatus.REFUNDED})

#: 远端「已完成」那一档的**字面值**（⭐ 全仓唯一出处）：慢路筛
#: `orderSta=已完成` 与②式「先接手、再完成」的分流都认这个值 ⇒ ⛔ 两处各写一份
#: （`jidanpu_member_openapi.REMOTE_DONE_LABEL` 就是它，⛔ 别再写字面量）。
REMOTE_DONE_STATUS = "已完成"

#: 取数窗的回看（天）：**增量与首次同一档**——有水位 `since = 水位 − 本值`、无水位 `since = now − 本值`
#: （业务方 2026-11-07 拍板：增量窗从「3 分钟重叠」改回「回看 3 天」。实测依据：铺侧完成档集中
#: 北京时间零点后才可匹配，增量窗只覆盖水位当天 ⇒ 本地日一翻篇、拖到次日才可见的完成单永久漏单，
#: 当日对账实漏 3,530 张；3 天窗让它们最晚次日当天被捞回，请求量 3~4 页/轮仍在预算内）。
PULL_MIN_WINDOW_DAYS = 3
#: 取数窗的**上限**（天）：服务端硬限，实测 **460 过 / 461 拒**
#: （msg：「开始时间和结束时间的时间跨度需控制在460天以内」，原始读数见 plan §2）。
PULL_MAX_SPAN_DAYS = 460
#: ⭐ 起算点的卡口（天）：`since` 不许早于 `now − 本值`。⚠️ ⛔ 不是 `PULL_MAX_SPAN_DAYS`：`stime` 向下取整到日、`etime` 向上取整到次日末 ⇒ 实发跨度比 `now − since` 多约 2 天（plan §4 Task B-补 第 2 条的实测推论）；按 460 卡会让「水位 ≳458 天前」时实发出 462 天 ⇒ 服务端拒 ⇒ 响亮+不推水位 ⇒ 每轮同一个窗、永久停滞；留 3 天死区后最坏实发跨度 = 457+2 = 459 天 ≤ 460，仍有 1 天余量；
#: ⚠️ 这个余量是取整带来的 ⛔ 与窗口下限无关 ⇒ 单列一个常量、不借 `PULL_MIN_WINDOW_DAYS`。
PULL_SPAN_ROUNDING_MARGIN_DAYS = 3
PULL_MAX_LOOKBACK_DAYS = PULL_MAX_SPAN_DAYS - PULL_SPAN_ROUNDING_MARGIN_DAYS

#: 本轮**取数没拉全**（`count` 判不了 / 累计行数 < `count` / 顶到翻页上限）时的人话。
#: ⚠️ 「拉全」的判据与文案**同源** = `jidanpu_reconcile._fetch_label`（见 `_fetch_completed_rows`）。
PULL_INCOMPLETE_REASON = "下游『已完成』这一档没拉全（{why}）⇒ 本轮一行都不写、水位不前进"
#: 整表取数那一跳**上游报错**（传输 / 业务失败）。
PULL_UNREADABLE_REASON = "拉取下游『已完成』那一档时上游报错（{exc}）⇒ 本轮一行都不写、水位不前进"

#: 被**写前复核**拦下时留的人话：B-1 的挡住集合（已结账的已完成 / 已退单，E-0148 / D-0156）
#: 加上「我方已完成」这一档（E-0259：不论结账与否——那是状态面的承诺，不是钱的那把尺子）。
REMOTE_STATUS_BLOCKED_REASON = (
    "该单已完成 / 已结账 / 已退单（按写前复核的挡住集合），本次状态回拉一个字都不改"
)
#: 写回成功（新形态只有一个来源档：远端「已完成」）。
REMOTE_STATUS_APPLIED_REASON = "远端已完成，已写回为『{target}』"
#: ⭐ D-0325：远端已完成、但这张单还是**上家镜像卡派生的隐藏单**（未转正、
#: 无 MAIN 绩效行 / 金额 / 归属）⇒ 直接写「待验收」是畸形终态 ⇒ 跳过 + 一次台账留痕给人。
REMOTE_DONE_UPSTREAM_MIRROR_REASON = (
    "下家已直接完成，但这张单还是上家镜像卡派生的隐藏单（未转正、无绩效行），"
    "不能直接写待验收；正常转正走自动接手（快路），本档请人工核对"
)

# ── ⭐「这一笔是谁做的」：下家线的**三条路共用同一段写入面**，人话不许说假话 ──────────
#: 慢路**状态回拉**（`pull_statuses`）驱动的写入 —— ⚠️ 这一句是**既有**文案，⛔ 一个字都没改。
TRIGGER_PULL = "pull"
#: ②式**回调**驱动的写入（下家线上那条「回调只当提醒、主动查权威」的路）。
TRIGGER_CALLBACK = "callback"
#: ②式**兜底对账**驱动的写入（回调漏了的时候靠它兜住）。
TRIGGER_RECONCILE = "reconcile"

#: 回调驱动的写回人话（⛔ 不说「回拉」：这一单是**对方推来一条提醒**带进来的）。
REMOTE_STATUS_APPLIED_CALLBACK_REASON = (
    "下家推来一条回调说这一单有变化 ⇒ 我们**主动查了权威状态**，确认它「已完成」，"
    "已写回为『{target}』（⭐ 回调只当提醒：写成什么以**主动查到的权威值**为准）"
)
#: 兜底对账驱动的写回人话（⛔ 不说「按回调提醒」：这一单**正是回调漏掉的那一种**）。
REMOTE_STATUS_APPLIED_RECONCILE_REASON = (
    "**兜底对账**查到这一单在下家已经「已完成」，已直接写回为『{target}』"
    "（这一单没有回调把它带进来，正是这一路要兜的事；写成什么仍以**主动查到的权威值**为准）"
)
#: 三条人话按**触发方式**取（⛔ 别在调用点手拼：同一个判断出现在两处必然漂）。
_APPLIED_REASONS: dict[str, str] = {
    TRIGGER_PULL: REMOTE_STATUS_APPLIED_REASON,
    TRIGGER_CALLBACK: REMOTE_STATUS_APPLIED_CALLBACK_REASON,
    TRIGGER_RECONCILE: REMOTE_STATUS_APPLIED_RECONCILE_REASON,
}

#: 被状态机 / 节点门拒绝（多半是缺节点——代价见本节的登记）。
REMOTE_STATUS_DENIED_REASON = "状态回拉被状态机拒绝（多半是该账号缺对应权限节点）："

#: 状态码 → 中文标签（`order.transition` 那句审计要求前后值是中文，spec §5.1）。
#: 与 `order_views._STATUS_LABELS` **同源**（都从 `OrderStatus.choices` 取），⛔ 不手写一份表。
_STATUS_TEXT = dict(OrderStatus.choices)


def _status_text(value) -> str:
    """状态码 → 中文标签；认不出的码原样返回（照 `order_views._status_text` 的口径）。"""
    return _STATUS_TEXT.get(value, value)


def _remote_token(value) -> str:
    """远端状态格的归一化（`None` ⇒ 空串；其余 strip）。判据用 `is None` 家族，⛔ 不用真值。"""
    return "" if value is None else str(value).strip()


def is_known_remote_status(value) -> bool:
    """这个远端状态值在表里认不认识（码或中文其一）。**兜底那一档要据此留人话台账**。"""
    token = _remote_token(value)
    return token in REMOTE_STATUS_MAP or token in REMOTE_STATUS_LABELS


def map_remote_status(code: str) -> str:
    """远端状态 → 我方 `OrderStatus`（spec §7 的映射表，逐档；认不出来 ⇒ `ISSUE`）。⚠️ 入参码与中文都收：spec §7 的表写在码上（`DSH`/`DYS`/…），而远端订单行实际下发的 `orderSta` 是中文标签（spec §5.7 实测）——只认一种会让真实回包全部落到兜底；⚠️ 兜底（`UNKNOWN_REMOTE_STATUS`）是问题单：⛔ 不猜、⛔ 不改 `COMPLETED`，调用方还要写一条人话台账 ⛔ 不许静默；
    ⭐ 本函数今天的调用方不是状态回拉（回拉已收窄成「只看已完成那一档」）：本模块 `_adopted_throw_verdict` 用它判「认领到的远端单是不是『未分配』」（不是 ⇒ ⛔ 不补抛）；两张表另被上家线并入 ⇒ 留着 ⛔ 不要按「回拉没人用了」删它。"""
    token = _remote_token(code)
    mapped = REMOTE_STATUS_MAP.get(token) or REMOTE_STATUS_LABELS.get(token)
    return mapped or UNKNOWN_REMOTE_STATUS


def _pull_window(*, since, now) -> dict[str, str]:
    """本轮的取数窗（完成时间口径）：`[since 当天 00:00:00, 明天 23:59:59]`。⚠️ 时间串照既有同类函数的格式（`jidanpu.reconcile_window`/`order_list_window`）：下界向下取整到那一天、上界向上取整到次日末 ⛔ 不自创格式；
    ⚠️ 窗的语义由 `timeType=30`（完成时间）承载，见 `jidanpu.fetch_completed_orders_page`。"""
    return {
        "stime": f"{timezone.localdate(since).isoformat()} 00:00:00",
        "etime": f"{(timezone.localdate(now) + timedelta(days=1)).isoformat()} 23:59:59",
    }


def _fetch_completed_rows(client, *, window) -> tuple[list[dict], bool, str, int]:
    """按「已完成」这一档翻页拉全（纯读）；回 `(行, 是否拉全, 原因, 页数)`。⚠️ 「拉全」判据与文案同源=`jidanpu_reconcile._fetch_label`（出处就在那里 ⛔ 不另立一套）：累计行数 ≥ 回包 `count`；没有可用的 `count` ⇒ 拉不全（fail-closed）；停页条件=空页/不满一页；顶到翻页上限 ⇒ 拉不全；
    ⚠️ 翻页上限复用 `jidanpu_reconcile.MAX_PAGES`（20）：本形态一轮只拉一个档（对账那边是 12 档），而最小窗（1 天）的真机实测约 1,278 行 ≈ 2 页（plan §2 的逐字读数）⇒ 20 页 = 20,000 行，对主路径是数量级的余量；真被顶到 ⇒ 按「拉不全」响亮处理、本轮不动作（⛔ 不静默交半份数据）；
    ⚠️ `count` 为负/布尔/缺失都已在 `jidanpu.fetch_completed_orders_page` 里归一成 `None`（那边的口径与 `jidanpu_reconcile._reconcile_attempt` 同源）⇒ 这里只需判 `None`。"""
    rows: list[dict] = []
    count: int | None = None
    hit_page_cap = False
    pages = 0
    page = 1
    while True:
        page_result = jidanpu.fetch_completed_orders_page(
            client, window=window, page=page,
            limit=jidanpu.ORDER_LIST_PAGE_SIZE, game_name=jidanpu.HALL_GAME_NAME_ALL,
        )
        pages += 1
        if count is None:
            count = page_result.count
        rows.extend(page_result.rows)
        got = len(page_result.rows)
        if got == 0 or got < jidanpu.ORDER_LIST_PAGE_SIZE:
            # 空页 = 到底；不满一页 = 到底（与 `_fetch_label` 逐字同一条停页条件）。
            break
        if page >= jidanpu_reconcile.MAX_PAGES:
            hit_page_cap = True
            break
        page += 1
    #: 台账里的那一档名字（与发出去的 `orderSta` 筛选值同字）。
    label = "已完成"
    if count is None:
        return rows, False, jidanpu_reconcile.NO_COUNT_REASON.format(label=label), pages
    if len(rows) < count:
        return (
            rows, False,
            jidanpu_reconcile.SHORT_PAGE_REASON.format(label=label, got=got, count=count),
            pages,
        )
    if hit_page_cap:
        return (
            rows, False,
            jidanpu_reconcile.PAGE_CAP_REASON.format(
                label=label, pages=jidanpu_reconcile.MAX_PAGES
            ),
            pages,
        )
    return rows, True, "", pages


def _fail_status_pull(binding, *, now, reason: str) -> int:
    """整轮取数失败（没有具体单可指认）：绑定级台账+`last_error`+回 0。
    ⚠️ ⛔ 不推水位：下一轮的窗从同一个水位算起 ⇒ 自然变得更大 ⇒ 只要上游恢复就补得回来（水位只在「拉全且没有 loud」时前进，见 `_pull_statuses` 的收尾）。"""
    current = _binding_current_rows(binding).filter(enabled=True)
    if current.exists():
        _note_binding_once(binding=binding, reason=reason, kind=KIND_STATUS_IN)
        _stamp_poll_health(binding, now=now, ok=False, reason=reason, current_rows=current)
    return 0


def _pull_remote_screenshots(*, order, binding, link, client, now) -> str | None:
    """B 段：把下家那张单上的图拉回来、存进我方订单截图（幂等）；回失败人话或 `None`。口径（业务方口径③）：「拉图任何失败都不挡『待验收』的回写」——`ViewPic` 报错/单张下载失败/单张落库失败一律只记一条台账人话，状态照写（调用方 `_pull_statuses` 就是这么接的：这条人话不进 `loud` ⇒ 不写 `last_error`、也不挡水位推进）；⛔ 与「静默当没图」的区别：没图（`ViewPic` 正常回空）与没拉成（异常/形状读不出）在 `jidanpu_upstream_pics.pull_order_screenshots` 里就是两种返回值（空的 `PullOutcome` vs 带 `reason` 的），这里只把后者翻成人话——这正是要避免的那种混淆；⚠️ 代价如实登记：这一轮没拉成，下一轮不会再试（写完「待验收」这张单就出了处理集合）⇒ 二期才做补拉（业务方已认可）。
    复用与归属：读列表/下载/确定性对象名/落库全在 `jidanpu_upstream_pics`（⛔ 本函数不新建第二份；连「上传页隐藏域」那一档都不需要——读图只要下家单号）；`uploaded_by` 照本线的绑定形状取：本单的 `sales_user`，为空回落这条绑定的本地用户（`JidanpuMemberBinding.user` 是非空 OneToOne ⇒ ⛔ 绝不回 `None`，那一列 NOT NULL+PROTECT）；⚠️ 这里刻意不 import `peer_inbound._screenshot_uploader`：那个函数的两个回落分支是上下游 peer 线的绑定形状（`bound_by`/`account`），本线的绑定没有那两列；
    ⚠️ 最外层那个 `except Exception` 是口径③的最后一道（`pull_order_screenshots` 自己已经把可预期的失败面收成 `failed`）：它兜的是「我们自己的编程错/存储后端抛的未预期异常」这类会让状态也一起回不去的情况，异常类名进人话 ⛔ 不静默。"""
    # 函数内 import：见 `_push_order_screenshots_to_downstream` 的同一句（成环）。
    from apps.business import jidanpu_upstream_pics

    try:
        outcome = jidanpu_upstream_pics.pull_order_screenshots(
            client, order=order, remote_order_id=link.jidanpu_order_id,
            uploaded_by=order.sales_user or binding.user, now=now,
            img_types=MEMBER_PULL_IMG_TYPES,
            skip_remote_keys=_pushed_remote_keys(order, binding),
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：拉图失败不挡待验收（业务方口径③）
        return PULL_PIC_FAILED_REASON.format(reason=type(exc).__name__)
    if not outcome.reason:
        return None
    return PULL_PIC_FAILED_REASON.format(reason=outcome.reason)


def _pull_openapi_screenshots(*, order, binding, link, pics, now) -> str | None:
    """B 段（②式版）：用官方 API 把这单的全量进度图拉回来、存进我方订单截图。⭐ 这一步替代②式下被跳过的 `ViewPic`（⛔ 不是「另加一次」）：①式那条路是「远端已完成 ⇒ 写待验收」之前在 `_pull_remote_screenshots` 里拉一次（`GET /OrderAjax/ViewPic`）；②式没有①式的登录凭据（`client=None`）⇒ 那一跳被跳过 ⇒ 换成②式之后截图就停了（业务方报的正是这个）；这里用②式自己的那把凭据打官方 API 的 `GetOrderProcessImgs`（一单的全部进度图，实测某单 15 张），落在同一个位置（写状态之前、B-1 那道「已结账不回退」复核之后）。
    口径（与①式那条路逐字同源）：⛔ 失败不挡「待验收」——读接口报错/单张下载失败/单张落库失败 ⇒ 只回一句人话（调用方记一条台账、不进 `loud` ⇒ 不写 `last_error`、不挡状态写入），状态照写；⛔ 与「静默当没图」的区别：没图（正常回空）与没拉成（异常/形状读不出）在 `jidanpu_upstream_pics.store_remote_pics` 里就是两种返回值——本函数只把后者翻成人话；落库复用既有的那一份（`jidanpu_upstream_pics.store_remote_pics`）⇒ ⛔ 不另造表/不另造目录/不另写一份落库循环；⭐ 判重按「对象 key」（`jidanpu_upstream_pics.remote_object_key` ⛔ 不是按整条 URL）：同一张图会同时出现在「回调给的 OSS 直连」与「查询接口给的 `oss.jidanpu.com`」两种域名上，而那两个域名指向同一个对象 ⇒ 按域名判重会存两行。
    参数：`pics`=②式自己的取图口（`jidanpu_openapi_consume` 的 `_StatusQueries`，它与查权威状态共用同一个客户端 ⇒ ⛔ 不为拉图再取一次 token），只要求有一个方法 `process_imgs_of(远端单号) -> 行元组`（鸭子类型 ⛔ 不 import 那一侧，那会成环）；落库的 `uploaded_by` 与①式那条路同一个出处：本单的 `sales_user`，为空回落这条绑定的本地用户（非空 OneToOne ⇒ ⛔ 绝不回 `None`）；⚠️ 最外层那个 `except Exception` 是口径③的最后一道（与 `_pull_remote_screenshots` 同一条口径）：它兜的是「我们自己的编程错/存储后端抛的未预期异常」这类会让状态也一起回不去的情况，异常类名进人话 ⛔ 不静默。"""
    # 函数内 import：见 `_pull_remote_screenshots` 的同一句（成环）。
    from apps.business import jidanpu_upstream_pics

    try:
        rows = pics.process_imgs_of(link.jidanpu_order_id)
        outcome = jidanpu_upstream_pics.store_remote_pics(
            jidanpu_upstream_pics.remote_pics_from_openapi_rows(rows),
            order=order, uploaded_by=order.sales_user or binding.user, now=now,
            identity=jidanpu_upstream_pics.remote_object_key,
            img_types=MEMBER_PULL_IMG_TYPES,
            skip_remote_keys=_pushed_remote_keys(order, binding),
        )
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：拉图失败不挡待验收（业务方口径③）
        return PULL_PIC_FAILED_REASON.format(reason=type(exc).__name__)
    if not outcome.reason:
        return None
    return PULL_PIC_FAILED_REASON.format(reason=outcome.reason)


def pullable_links(binding, *, also=()):
    """处理集合：本绑定（以及 `also` 里那几份同店绑定）名下「我方还没到终态、且没软删」的链路（⭐ 判据①的唯一出处）。⚠️ 用 SQL 级条件（`order__deleted_at__isnull=True`+`exclude(order__status__in=…)`）而不是循环里的 `getattr(order, "deleted_at", None)`：软删/终态的行根本不进集合（也就不会出现「取一半才发现要跳过」）；⭐ 为什么必须抽成函数（下家线②式收口）：②式回调消费与②式兜底对账是逐单来的（一条回调只带一个 `orderNo`），它们必须能问同一个问题（「这张单在不在处理集合里」）⇒ 各写一份判据必然漂，而漂了的后果是已退单的单被复活（`REFUNDED` 那格的口径见 `PULL_TERMINAL_STATUSES` 的注释）。
    `also=`（兜底：①式回拉覆盖同一家店别的绑定）：①式状态回拉的匹配集合要从「只本绑定链路」扩到「同一家店启用中别的绑定的链路」（含②式：对方关掉「开放平台接入权限」时②式彻底失灵，而①式登录同一家店照样能读到批量状态）；那条路的绑定面就是这一格——⛔ 判据本身一个字没改，只是 `binding` 从一行变成「本行+`also` 那几行」（`also` 的筛法在 `_pull_extra_bindings` ⛔ 不在调用点各写一份）；⚠️ ②式回调/②式兜底对账都不传 `also`（它们仍只覆盖自己的链路）；
    ⚠️ 调用方各自追加 `.select_related("order")`/`.order_by("pk")`：排序是窗口轮转的事 ⛔ 不塞进这里（慢路不关心顺序，对账关心）。"""
    rows = [binding, *also]
    return (
        JidanpuMemberOrderLink.objects.filter(binding__in=rows, withdrawn_at__isnull=True)
        .filter(order__deleted_at__isnull=True)
        .exclude(order__status__in=PULL_TERMINAL_STATUSES)
    )


# DEFERRED 保留水位等待绑定恢复，DENIED 另有需要告警的原因。
REMOTE_DONE_APPLIED = "applied"
REMOTE_DONE_SKIPPED = "skipped"
REMOTE_DONE_DENIED = "denied"
REMOTE_DONE_DEFERRED = "deferred"


@dataclass(frozen=True)
class CompletionOutcome:
    """一次「远端已完成 ⇒ 写待验收」的结论（见 `apply_remote_completion`）。"""

    verdict: str
    #: 要人看见的那句话（只有 `DENIED` 非空；调用方把它盖进 `last_error` / 进 `findings`）。
    loud: str = ""


@dataclass
class PullPicsBudget:
    """一轮「拉图」的名额（`PULL_PICS_PER_ROUND`）：用完即停，超出的留到下一轮。
    ⚠️ 一轮一个实例（由那一轮的调用方建好、逐单传进来）：⛔ 不做模块级全局——那种写法在同一进程里跑两个租户/两条轮次时会把名额混在一起，计数还会跨用例串味。"""

    remaining: int = PULL_PICS_PER_ROUND

    def take(self) -> bool:
        """还拿得到一个名额吗（拿得到就扣掉一个）。"""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _active_link_current(binding, link):
    return _binding_is_current(binding) and JidanpuMemberOrderLink.objects.filter(
        pk=link.pk, binding=binding, order_id=link.order_id, withdrawn_at__isnull=True,
        jidanpu_order_id=link.jidanpu_order_id, listed_at=link.listed_at,
    ).exists()


def _completion_link_current(binding, order, link):
    taker_id = _main_taker_id(order.pk)
    return _active_link_current(binding, link) and (
        taker_id is None or binding.jidanpu_base_url in downstream_addresses_of_taker(taker_id)
    )


def apply_remote_completion(*, binding, order, link, remote_status, nodes, now,
                            client=None, openapi_pics=None,
                            pic_budget: PullPicsBudget | None = None,
                            trigger: str = TRIGGER_PULL, pull_binding=None) -> CompletionOutcome:
    with jidanpu_assignment_lock.sending(
        binding_id=binding.pk, order_id=order.pk,
        source_binding_id=pull_binding.pk if pull_binding is not None else None,
    ) as busy:
        if busy:
            return CompletionOutcome(verdict=REMOTE_DONE_DENIED, loud=busy)
        if pull_binding is not None:
            if not _binding_current_rows(pull_binding).filter(enabled=True).exists():
                return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
            if binding.pk != pull_binding.pk:
                # 只使用消费方会话，链路所属人的同店改密无需作废本轮。
                binding = JidanpuMemberBinding.objects.filter(
                    pk=binding.pk, jidanpu_base_url=pull_binding.jidanpu_base_url,
                ).first()
                if binding is None:
                    return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
                if not binding.enabled:
                    if _remote_token(remote_status) == REMOTE_DONE_STATUS and _completion_link_current(binding, order, link):
                        return CompletionOutcome(verdict=REMOTE_DONE_DEFERRED)
                    return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
        return _apply_remote_completion(
            binding=binding, order=order, link=link, remote_status=remote_status,
            nodes=nodes, now=now, client=client, openapi_pics=openapi_pics,
            pic_budget=pic_budget, trigger=trigger,
        )


def _apply_remote_completion(*, binding, order, link, remote_status, nodes, now,
                             client=None, openapi_pics=None, pic_budget=None,
                             trigger=TRIGGER_PULL) -> CompletionOutcome:
    """把一次远端状态落到我方订单上（⭐ 下家线唯一的那段「判 → 写」）。三条调用路（⛔ 只有这三条，不许再多一条）：①慢路状态回拉（`_pull_statuses`）：远端状态来自整店「已完成」那一档的整表行（`row["orderSta"]`，`trigger=TRIGGER_PULL`）；②②式回调消费（`jidanpu_member_openapi`）：对方推一条回调 ⇒ 我们主动查一次权威状态（`GetOrderStatus`）拿到的那一格（`trigger=TRIGGER_CALLBACK`）；③②式兜底对账（`jidanpu_member_openapi`）：回调漏了的时候逐单查权威（`trigger=TRIGGER_RECONCILE`）。⭐ 为什么整段共用（任务书「判据/写面只留一处」）：下家线自己那五条「该不该写」的判据全部在这一个函数里 ⛔ 三条路没有各自的一套：①「处理集合」那一条在找链路时就用 `pullable_links` 落地（不进集合的订单根本到不了这里）；②远端状态必须逐字「已完成」（fail-closed）；③我方已经是 `PENDING_REVIEW` ⇒ 一个字都不写（幂等）；④写前第二道闸 `jidanpu_common._b1_status_rollback_blocked`（已结账/已退单不许回退）；⑤⭐（E-0259）写前复核里 fresh 是 `COMPLETED` ⇒ skip（不论结账与否），理由与 ④ 的分工见下面那段注释；⚠️ ⛔ 本函数不判「远端状态认不认识」（那是来源那一侧的事：慢路的取数已按 `orderSta=已完成` 筛过；②式那条路由 `jidanpu_member_openapi` 用 `is_known_remote_status` 判并留痕）⛔ 也不查权威（权威值由调用方查好传进来）。
    参数里两个刻意的注入点：`nodes` 调用方算——慢路一轮算一次（`_pull_statuses`），②式那两条路逐单算（`nodes_of(binding.user)`，与上家线 `_write_status` 的既有形状逐字同形：那边也是一次写算一次）；⚠️ 两边取的都是同一个出处 ⛔ 不是两套口径。`client=None`：②式那两条路没有①式的登录客户端（②式在界面上就是账号密码为空）⇒ 表示不走①式那一跳（`GET /OrderAjax/ViewPic`）；⚠️ ②式不再因此少做一件事：同一条调用会拿到 `openapi_pics`（②式自己的官方 API 取图口）⇒ 由 `_pull_openapi_screenshots` 用 `GetOrderProcessImgs` 补拉一次全量进度图，落在同一个位置（写状态之前）；⛔ 两个入参互斥：`client` 非空（①式慢路）就不用 `openapi_pics`，②式那两条路则相反；⚠️ 两个都为空（既没有①式客户端、也没给官方 API 取图口）⇒ 跳过拉图那一跳（今天的调用点里只有「直接调它的用例」会落到这一格）。
    `pic_budget`（⭐ 拉图每轮限流）只由①式慢路（`_pull_statuses`）传进来：一个 `PullPicsBudget` 管这一轮的拉图名额；名额用完 ⇒ 状态照写（业务判据一个字不动 ⛔ 不许因为限流丢单），只把图留到下一轮：写一条 `skipped` 台账（`PULL_PICS_DEFERRED_REASON`），下一轮的 `_pull_deferred_pics` 按它补拉；⚠️ ②式那两条路不传（默认 `None`=不限）：它们每轮的单数已经被各自的 `max_orders` 封住（`SYNC_NOW_OPENAPI_MAX_ORDERS`/兜底对账的轮转上限）⇒ 本轮不在本开关的范围内。"""
    if _remote_token(remote_status) != REMOTE_DONE_STATUS:
        # 取数/权威那一侧已经把「不是这一档」筛过一遍（慢路按 `orderSta=已完成` 筛、②式按 `is_known_remote_status` 筛）⇒ 这里再判一次是 fail-closed：⛔ 绝不拿一张别的档的单去改我方状态。
        #    ⚠️ 不留台账（照既有口径）：远端还没完成是常态，逐条留痕会把台账淹掉（`_pull_statuses` 的注释写着同一条）。
        return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
    if not _completion_link_current(binding, order, link):
        return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
    # 远端确实还有这张单（语义见 `last_in_at` 的既有注释）：收窄写入，⛔ 不整行 `save()`。
    JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(last_in_at=now)
    if order.status == OrderStatus.PENDING_REVIEW:
        # 已经是目标态 ⇒ **一个字都不写**（幂等；⛔ 也不写台账，否则每一轮都会给同一张单
        # 加一行噪声）。
        return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)

    # ⭐⭐ D-0325：上家镜像卡派生的隐藏单（`upstream_hall_card` 非空）⇒ 一个字都不写+一次台账留痕。这一档是「下家直接完成了副本、而我方快路没看到接单」（正常转正路是 `machine_claim_order` 的镜像分支）：隐藏单没有 MAIN 绩效行、没有金额与归属，直接写「待验收」会落一个畸形终态（金额/归属全缺、验收不了也结不了）；自动转正不在这一档做——转正要打上家上游（跨线），且上游卡此刻可能已被别家接走，自动接手会把别人的活抢过来（钱与归属的事不自动拍板）⇒ 跳过+响亮留痕给人；
    #    ⚠️ `SKIPPED` 而不是 `DENIED`：DENIED 会挡整轮水位（一轮比一轮大）；本档每轮都会再遇到，留痕用 `_note_order_once` 幂等收口。
    if order.upstream_hall_card_id is not None:
        _note_order_once(
            binding=binding, order=order, kind=KIND_STATUS_IN,
            reason=REMOTE_DONE_UPSTREAM_MIRROR_REASON, status="skipped",
        )
        return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)

    # ⭐ 写前复核「已完成/已结账/已退单 ⇒ 一个字节都不写」（第二道闸，spec §6.2 第 4 步+E-0259 追加的那一格）：集合是取数之前筛的，而「筛 → 拉整表 → 写」之间隔着一次网络往返，那一单可能刚好被结账/完成/退单 ⇒ 用库里最新的那一行再判一次，挡住就一个字节都不改；⚠️ 这里读的是新鲜行（`only("status")`）而不是循环开头那份 `link.order`：用旧值判等于没判。
    #    ⭐ ⑤ 那一格（E-0259）：fresh 是 `COMPLETED` ⇒ skip，不论结账与否。与 ④（B-1）的分工：④ 的挡住集合（`REFUNDED` 无条件挡+「`COMPLETED` 且已结账」）回答的是「这一单的状态许不许被同步回退」——那是钱的安全的尺子（E-0148/D-0156），而且 `jidanpu_converge` 那条收敛线按同一把尺子判回退 ⇒ 「未结账的 `COMPLETED`」在那把尺子下是可回退的，④ 不会挡它；但本入口要写的是「待验收」：对我方已经完成的单，不管结没结账，写回去都会把 `completed_at` 清掉、把一张已完成的单退回验收（上家线同位用「写入面只开代练中一格」挡的正是它）；
    #    ⇒ 这一道是本入口自己的状态面承诺（「我方已完成单一个字不写」）⛔ 不并进 `_b1_status_rollback_blocked`——那把尺子（公开别名 `status_rollback_blocked`，`jidanpu_common.py`）的在役消费方是 `jidanpu_upstream` 的上家跟退线（`_follow_refund`）；把「未结账 COMPLETED ⇒ 挡」并进去会顺手改掉跟退线的判定面（钱的那把尺子一字不动）；⚠️ 与 D-0257 无关：认领单「已退单 ⇒ 先改回未分配再补抛」的恢复路走 `_member_adopted_verdict` 的补抛支（另一条路，不经过本函数），不会被这一道挡。
    fresh = Order.objects.filter(pk=order.pk).only("status", "pk").first()
    block_main = Performance.objects.filter(
        order_id=order.pk, emp_type=EmpType.MAIN
    ).first()
    if (
        fresh is None
        or fresh.status == OrderStatus.COMPLETED  # E-0259：⑤ 不论结账与否
        or jidanpu_common._b1_status_rollback_blocked(fresh, block_main)
    ):
        _note_order_once(
            binding=binding, order=order, kind=KIND_STATUS_IN,
            reason=REMOTE_STATUS_BLOCKED_REASON, status="skipped",
        )
        return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)

    # ⭐ B 段：先拉图、再无条件写状态（业务方口径③）。⚠️ 放在这道「已结账不回退」复核之后：挡住的单一个字节都不写，也就不该为它去打一次 `ViewPic`/官方 API 那一次；⚠️ 拉图任何失败都不挡「待验收」：失败只记一条台账人话（不进 `loud` ⇒ 不写 `last_error`、不挡水位推进）；下一轮不会再试（写完「待验收」这张单就出了处理集合）；
    #    ⭐ 两条路二选一：①式慢路给 `client` ⇒ `ViewPic`（⛔ 逐字不变）；②式那两条路给 `openapi_pics` ⇒ 官方 API 的 `GetOrderProcessImgs` 补拉全量进度图（替代②式下被跳过的 `ViewPic` ⛔ 不是另加一次）。见函数 docstring 那两个入参。
    pull_failure: str | None = None
    if client is not None:
        if pic_budget is not None and not pic_budget.take():
            deferred_event = {
                "direction": "in", "kind": KIND_STATUS_IN, "status": "skipped",
                "target_type": "order", "target_ref": order.order_no,
                "reason": PULL_PICS_DEFERRED_REASON,
                "payload": {
                    "member_binding_id": binding.pk,
                    PIC_LINK_SNAPSHOT_KEY: _picture_link_snapshot(link),
                },
            }
            if not JidanpuSyncEvent.objects.filter(**deferred_event).exists():
                jidanpu_common.write_event(**deferred_event)
        else:
            pull_failure = _pull_remote_screenshots(
                order=order, binding=binding, link=link, client=client, now=now
            )
    elif openapi_pics is not None:
        pull_failure = _pull_openapi_screenshots(
            order=order, binding=binding, link=link, pics=openapi_pics, now=now
        )
    if pull_failure is not None:
        _note_order_once(
            binding=binding, order=order, kind=KIND_STATUS_IN,
            reason=pull_failure, status="failed",
        )

    denial: str | None = None
    from_status = str(order.status)
    # ⚠️ `apply_transition` 的复读用 `select_for_update` ⇒ 必须在事务里（它在 autocommit 下会响亮抛 `TransactionManagementError`）。
    #    ⚠️ 可预期失败必须 `set_rollback(True)` 再记账（E-0022）：状态那一列在钩子抛之前可能已经保存过，裸退出会把「状态变了、账没记」的半笔提交掉。
    from apps.business.order_writes import lock_order_row

    with transaction.atomic():
        fresh = lock_order_row(order)
        if fresh is None or not _completion_link_current(binding, fresh, link):
            return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
        current_main = Performance.objects.select_for_update().filter(
            order_id=order.pk, emp_type=EmpType.MAIN,
        ).first()
        if fresh.status in (*PULL_TERMINAL_STATUSES, OrderStatus.PENDING_REVIEW) or jidanpu_common._b1_status_rollback_blocked(fresh, current_main):
            return CompletionOutcome(verdict=REMOTE_DONE_SKIPPED)
        try:
            # ⭐ **来源闸**：本次写入是**外部同步驱动**的 ⇒ 不许再对外回声（否则每笔回拉
            #    都排一条 `member.status_out`，下一轮把我方状态推回对方）。⚠️ **只关这一个
            #    钩子**：审计、`_run_target_hooks` 的目标态钩子照常跑。
            apply_transition(
                fresh, OrderStatus.PENDING_REVIEW, nodes=nodes, actor=MACHINE_ACTOR,
                suppress_outbound_sync=True,
            )
            order.refresh_from_db()
        except IllegalTransition as exc:
            # 边不存在 / 节点不够 / 陈旧状态（复读不一致）⇒ 全部当**可预期拒绝**记账。
            transaction.set_rollback(True)
            denial = str(exc)
        except (RefundError, CommissionError, AmountError) as exc:
            # 领域层的**预期**失败（退款前置 / 提成算不出 / 金额算不出）——照
            # `order_views` 的同一条口径翻译，⛔ 不伪装成「状态非法」，也⛔ 不让它
            # 打断这一轮（别的单照跑）；`run_outbox` 的逐事件边界同形。
            transaction.set_rollback(True)
            denial = getattr(exc, "message", None) or str(exc)
        else:
            # 机器动作的**订单审计**（照 T7 的自动接手）：操作人 = 机器名、`actor_id` 留空。
            # ⚠️ 复用 `order.transition` 的动作码与文案：这**就是**一次订单状态迁移，
            #    ⛔ 不新造一个只有机器会写的动作（同一件事两套文案必然漂移）。
            record(
                log_type=ACTION_LOG_TYPES["order.transition"],
                action="order.transition",
                actor=MACHINE_ACTOR, actor_id=None,
                # ⭐ 机器来源 ⇒ 显式声明（同上一处）。
                system=True,
                target_kind="order", target_id=order.order_no,
                order_no=order.order_no, field_name="status",
                from_value=_status_text(from_status),
                to_value=_status_text(OrderStatus.PENDING_REVIEW),
            )
    if denial is not None:
        reason = f"{REMOTE_STATUS_DENIED_REASON}{denial}"
        _note_order_once(
            binding=binding, order=order, kind=KIND_STATUS_IN,
            reason=reason, status="failed",
        )
        return CompletionOutcome(verdict=REMOTE_DONE_DENIED, loud=reason)
    _note_order_once(
        binding=binding, order=order, kind=KIND_STATUS_IN, status="done",
        reason=_APPLIED_REASONS[trigger].format(
            target=_status_text(OrderStatus.PENDING_REVIEW)
        ),
    )
    return CompletionOutcome(verdict=REMOTE_DONE_APPLIED)


def _pull_covers_siblings() -> bool:
    """①式状态回拉要不要顺带覆盖同一家店别的绑定的链路（开关；默认开）。
    ⚠️ 运行期读 settings（`getattr`+默认值）：这样 `override_settings` 才生效，照 `JIDANPU_IMAGE_TIMEOUT` 那一处的同一处置；⛔ 不在模块导入时把它冻成常量。"""
    return bool(getattr(settings, "JIDANPU_MEMBER_PULL_COVERS_SIBLINGS", True))


def _pull_extra_bindings(binding) -> list:
    """本轮状态回拉的附加绑定（同店、启用中、不含自己）；旧行为 ⇒ 空列表。口径（⭐ 「匹配集合扩到哪儿」的判据只此一处）：②式（官方 API）⇒ 空（②式自己的能力一个字不改）；①式+开关开 ⇒ 同一 `jidanpu_base_url` 上全部启用中的别的绑定（含②式）；①式+开关关 ⇒ 空（=回到旧行为：只覆盖本绑定自己的链路）。
    三条判据：①同店=`jidanpu_base_url` 逐字相等（与 `_bindings_at`/`_only_push_targets`/`downstream_addresses_of_taker` 的既有口径逐字一致 ⛔ 不自创归一化）；②空/空白地址不是店 ⇒ 没有附加集合（⛔ 拿空串配对：那会把两个「还没配地址」的行判成同店，而跨店代写的代价是改错另一家店的单）；③停用的绑定不纳入：停用是用户自己按下的开关（spec §8.4「停用停止对账」）⇒ ⛔ 不替它做决定（消费方自己那份由 `pull_statuses` 的 `enabled` 闸门保证）；
    ⚠️ 返回按 `id` 升序（`nodes_of` 的缓存与「最早那一份」这类调用点靠它稳定）；⚠️ 函数内 import（环）：`jidanpu_openapi` 模块级 import 了 `jidanpu_upstream`，后者又 import 本模块（照 `_only_push_targets` 那处的同一处置）。"""
    from apps.business import jidanpu_openapi

    if jidanpu_openapi.is_openapi_binding(binding) or not _pull_covers_siblings():
        return []
    address = binding.jidanpu_base_url or ""
    if not address.strip():
        # ⚠️ 空白地址**不是**地址（与 `_only_push_targets` 的空地址判据同一口径）。
        return []
    return list(
        JidanpuMemberBinding.objects.filter(
            jidanpu_base_url=address, enabled=True,
        ).exclude(pk=binding.pk).order_by("id")
    )


def _deferred_pic_events(binding, *, also, limit):
    return list(
        JidanpuSyncEvent.objects.filter(
            direction="in", kind=KIND_STATUS_IN, target_type="order",
            status="skipped", reason=PULL_PICS_DEFERRED_REASON,
            payload__member_binding_id__in=[item.pk for item in (binding, *also)],
        ).order_by("id")[:limit]
    )


def _pull_deferred_pics(binding, *, events, links, client, now, budget) -> int:
    """按排队时的副本补拉，旧台账缺少副本身份时不猜测。"""
    by_id = {link.pk: link for link in links}
    pulled = 0
    for event in events:
        if budget.remaining <= 0:
            break
        pending = JidanpuSyncEvent.objects.filter(
            pk=event.pk, status="skipped", reason=PULL_PICS_DEFERRED_REASON,
        )
        snapshot = event.payload.get(PIC_LINK_SNAPSHOT_KEY)
        link_id = snapshot.get("id") if isinstance(snapshot, dict) else None
        link = by_id.get(link_id) if type(link_id) is int else None
        if (
            link is None or link.binding_id != event.payload.get("member_binding_id")
            or link.order.order_no != event.target_ref or _picture_link_snapshot(link) != snapshot
        ):
            pending.update(reason="旧补拉任务的下家副本已失效或无法确认，本次未补拉截图", processed_at=now)
            continue

        # 同店代查也要保护实际会话的绑定，避免用已换绑的会话读图。
        source_lock = jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=link.order_id)
        with source_lock as source_busy:
            if source_busy:
                continue
            owner_lock = (
                jidanpu_assignment_lock.sending(binding_id=link.binding_id, order_id=link.order_id)
                if link.binding_id != binding.pk else nullcontext()
            )
            with owner_lock as owner_busy:
                if owner_busy:
                    continue
                if not pending.exists():
                    continue
                # 本轮配置已过时则留到下一轮，原副本可能仍然有效。
                if (
                    link.binding.jidanpu_base_url != binding.jidanpu_base_url
                    or not _binding_is_current(binding)
                    or (link.binding_id != binding.pk and not _binding_is_current(link.binding))
                ):
                    continue
                if JidanpuMemberBinding.objects.filter(
                    pk__in=(binding.pk, link.binding_id), enabled=False,
                ).exists():
                    continue
                if (
                    not pullable_links(link.binding).filter(pk=link.pk).exists()
                    or not _completion_link_current(link.binding, link.order, link)
                ):
                    pending.update(reason="原下家副本已变更、撤回或不再需要补拉，本次未补拉截图", processed_at=now)
                    continue
                budget.take()
                failure = _pull_remote_screenshots(
                    order=link.order, binding=link.binding, link=link, client=client, now=now,
                )
                if failure is not None:
                    _note_order_once(
                        binding=link.binding, order=link.order, kind=KIND_STATUS_IN,
                        reason=failure, status="failed",
                    )
                else:
                    pulled += 1
                pending.update(status="done", processed_at=now)
    return pulled


def _pull_statuses(binding, *, client, now) -> int:
    """一轮状态回拉的实做（建连已由 `pull_statuses` 处理）；回写回的状态数。
    ⚠️ 匹配集合=本绑定链路+（①式 & 开关开时的）同店启用中别的绑定的链路；扩到哪儿的判据在 `_pull_extra_bindings`（⛔ 本函数不另写一份），处理集合的判据仍在 `pullable_links`（只是绑定面多传了 `also=`）。"""
    # ⓪ 匹配集合：本绑定链路 +（①式 & 开关开时的）同店**启用中**兄弟绑定链路。
    #: ⭐ 同上：记下这一发用的出口。
    LAST_CALL.record_outlet(getattr(client, "_tt_egress_outlet", None))
    current = _binding_current_rows(binding).filter(enabled=True)
    extras = _pull_extra_bindings(binding)
    pics_budget = PullPicsBudget()
    # 晚于本轮取数的新待办留给下一轮，不能按旧链接快照判无效。
    deferred_pics = _deferred_pic_events(binding, also=extras, limit=pics_budget.remaining * 4)
    # ① 处理集合：只留「我方还是**非终态**」的单（⭐ 判据的唯一出处是 `pullable_links`）。
    links = list(
        pullable_links(binding, also=extras).select_related("order", "binding")
    )
    if not links:
        _pull_deferred_pics(
            binding, events=deferred_pics, links=links, client=client, now=now, budget=pics_budget,
        )
        # 一条待处理的链路都没有 ⇒ 这一轮没有对象：⛔ 不动健康列（别的路可能刚写过一条真错，一次空转不该把它清掉），但推进水位（plan §4 Task B-补 第 3 条）；
        #    ⭐ 为什么推进是安全的：集合空本身就证明「现存链路没有一张需要拉」（绑定面已含同店兄弟：`pullable_links(..., also=extras)`；都已终态/已软删）⇒ 没有任何东西会因为水位的这一跳而被漏掉；
        #    反过来，不推进的话一个长期闲置（链路全终态）的绑定下一次有单时会从很旧的水位算起 ⇒ 可能一次拉回最长的整店「已完成」（那正是这条返工要堵的）。
        current.update(status_pull_at=now)
        return 0

    # ② 算窗：增量与首次同一档，都回看 `PULL_MIN_WINDOW_DAYS` 天。有水位 ⇒ `since = 水位 − 3 天`；
    #    无水位（首次启用/被清空）⇒ `since = now − 3 天`；水位很旧（长时间停摆）⇒ `since = 水位 − 3 天`
    #    更旧，窗自然变大 ⇒ 补得回来；
    #    ⛔ 不许把 `since` 往前压到 `now − 3 天`：那会把停摆超过 3 天的那一段永远补不回来（旧口径已废）。
    if binding.status_pull_at is not None:
        since = binding.status_pull_at - timedelta(days=PULL_MIN_WINDOW_DAYS)
    else:
        since = now - timedelta(days=PULL_MIN_WINDOW_DAYS)
    # ⛔ 服务端硬限：**实发**起止跨度必须 ≤ 460 天（实测 460 过 / 461 拒，plan §2 的逐字读数）。
    #    ⚠️ 卡口按 `PULL_MAX_LOOKBACK_DAYS`（= 457）而不是 460：`stime` / `etime` 各自向两端
    #    取整到日 ⇒ 实发跨度会比这里算的 `now − since` 多约 2 天（见该常量的注释）。
    since = max(since, now - timedelta(days=PULL_MAX_LOOKBACK_DAYS))
    window = _pull_window(since=since, now=now)

    # ③ 拉全那一档；**拉不全 / 上游报错 ⇒ 本轮一行都不写**（照 `jidanpu_reconcile` 的同一条口径）。
    try:
        rows, complete, why, _pages = _fetch_completed_rows(client, window=window)
    except JidanpuError as exc:
        # ⚠️ 同上：468 被这里吞掉 ⇒ 打上「撞过拦截」标记。
        if isinstance(exc, UpstreamBlockedError):
            LAST_CALL.blocked = True
        return _fail_status_pull(
            binding, now=now,
            reason=PULL_UNREADABLE_REASON.format(exc=type(exc).__name__),
        )
    #: ⭐ 真的把整店「已完成」那一档拉回来了 ⇒ 算「真跑过」。
    #:   ⚠️ 上面那条 `if not links: return 0`（零上游早退）**不会**置这个标记 —— 那正是 ① 的洞。
    LAST_CALL.upstream = True
    if not complete:
        return _fail_status_pull(
            binding, now=now, reason=PULL_INCOMPLETE_REASON.format(why=why)
        )

    # ③.5 ⭐ **拉图名额**：一轮一个 `PullPicsBudget`；**先补上一轮留下的**
    #      （见 `_pull_deferred_pics` 的口径：先留先补，积压才会缩小），剩下的名额给本轮新完成的单。
    _pull_deferred_pics(
        binding, events=deferred_pics, links=links, client=client, now=now,
        budget=pics_budget,
    )

    # ④ 建索引：`{远端 orderId: 行}`（链路行的 `jidanpu_order_id` 就是它）。
    by_remote_id: dict[str, dict] = {}
    for row in rows:
        remote_id = "" if row.get("orderId") is None else str(row.get("orderId"))
        if remote_id:
            by_remote_id[remote_id] = row

    #: 本轮每个**链路所属绑定**的节点集（`nodes_of(binding.user)`）：一场一轮算一次
    #: （与②式那两条路「逐单算」**同一个出处**，⛔ 不是两套口径）。⚠️ 同店兄弟那条链路要用
    #: **它自己**的主人的节点（⛔ 不拿消费方的节点去接别人的单，见下面那一处注释）。
    nodes_by_binding: dict[int, frozenset[str]] = {}
    applied = 0
    deferred = False
    #: 本轮撞上的**响亮失败**（缺节点 / 领域层预期失败）——它不中止本轮（别的单照跑），
    #: 但轮末必须写进 `last_error`（⛔ 不许只留一行台账就算完），且**⛔ 不许推水位**。
    loud: str | None = None

    for link in links:
        order = link.order
        row = by_remote_id.get(link.jidanpu_order_id)
        if row is None:
            # 远端这一档的行里没有这张单 ⇒ 什么都不做。⛔ **不写台账**：它**不是**失败
            # （「远端还没完成」是常态），每一轮给一批链路各留一行会把台账淹掉。
            continue
        # ⭐ 判 → 写整段交给 `apply_remote_completion`（下家线唯一那一份）：远端状态 != 逐字「已完成」/已是待验收/被 B-1 挡住/状态机拒绝，四种都在里面；⛔ 别在这里再判一遍（②式回调与兜底对账调的是同一个函数）；
        #    ⭐⭐ 应用身份=该链路自己的绑定（兜底 ⛔ 不是消费方）：`binding=` 决定节点门（`nodes_of`）与台账归属，与下家线跨绑定那条路的既有口径逐字同形（`jidanpu_member_openapi._member_landed` 的 `identity = owner`）；
        #    `client=` 仍是①式会话：状态是①式这条连读回来的，同店 ⇒ 打的是同一家店的上游。
        link_binding = link.binding
        nodes = nodes_by_binding.get(link_binding.pk)
        if nodes is None:
            nodes = nodes_of(link_binding.user)
            nodes_by_binding[link_binding.pk] = nodes
        outcome = apply_remote_completion(
            binding=link_binding, order=order, link=link,
            remote_status=row.get("orderSta"),
            nodes=nodes, client=client, now=now, trigger=TRIGGER_PULL,
            pic_budget=pics_budget, pull_binding=binding,
        )
        if outcome.verdict == REMOTE_DONE_APPLIED:
            applied += 1
        elif outcome.verdict == REMOTE_DONE_DEFERRED:
            deferred = True
        elif outcome.loud:
            # 「要人看」的那一句话（缺节点 / 领域层预期失败）照既有口径留在 `loud` 上 ⇒
            # 轮末写进 `last_error`，且**不推水位**。
            loud = loud or outcome.loud

    # 暂停发生于本轮取数以后时，保留本轮的窗口供恢复后补查。
    _stamp_poll_health(binding, now=now, ok=loud is None, reason=loud or "", current_rows=current)
    if loud is None and not deferred:
        current.update(status_pull_at=now)
    return applied


def pull_statuses(binding, *, now, client=None) -> int:
    """跑一轮状态回拉（慢路），回写回的状态数（plan `2026-10-03-member-status-scope` Task B/spec §6.2/§9.3）。流程：取该绑定（①式时还含同一家店启用中别的绑定，见下）链路名下我方非终态的单（已完成/已退单/已软删都⛔ 不进集合）→ 按完成时间（`timeType=30`）整店拉「已完成」那一档（逐页拉全；拉不全/上游报错 ⇒ 本轮一行都不写）→ 用远端 `orderId` 匹配链路 → 命中且 `orderSta == 已完成` ⇒ 写「待验收」（`PENDING_REVIEW`，节点门=该链路自己那个绑定的主人的 `nodes_of(binding.user)`）→ 写前再看一次 B-1 的挡住集合（已结账的已完成/已退单 ⇒ 一行不动）→ 写了就留一条审计+一条 `member.status_in` 台账；缺节点/状态机拒绝一律响亮。
    ⭐ 匹配集合（兜底）：①式（`mode="password"`）那一轮，除了自己的链路，还覆盖同一 `jidanpu_base_url` 上启用中别的绑定（含②式）的链路——对方关掉「开放平台接入权限」时②式彻底失灵（取 token 被拒、回调一条都不到），而①式登录同一家店仍能读到批量状态；命中后照常走 `apply_remote_completion`（⛔ 没有第二份「判 → 写」），`binding=` 传该链路自己的绑定（节点门与台账归属都按它），`client=` 仍是①式的会话；⛔ 停用的绑定不覆盖（判据见 `_pull_extra_bindings`）；⛔ ②式绑定自己仍然只覆盖自己的链路；开关 `JIDANPU_MEMBER_PULL_COVERS_SIBLINGS` 默认开，关掉即回到旧范围。
    ⚠️ 水位（`binding.status_pull_at`）只在「拉全且没有 loud」时推到 `now`；否则原地不动 ⇒ 下一轮的窗自然变大 ⇒ 只要上游恢复就补得回来；⚠️ `client` 是可测性注入口（照 `poll_log`/`consume_event` 的形状）：给了就用、没给就自己 `jidanpu.member_session(binding)` 建连（同一绑定的会话在进程内复用）⛔ 不是业务参数（凭据只从 `binding` 那一行取，spec §8.5）；⚠️ 「立即同步」端点走的就是本函数（⛔ 没有第二份实现，spec §9.3）；⚠️ 绑定停用 ⇒ 一次上游都不打（spec §8.4：停用停止对账）；⚠️ 预期失败（拉不全/上游报错/缺节点）⇒ 台账+`last_error`、不抛（后台轮询要能继续跑别的绑定）；未预期异常（我们自己的编程错误）照旧裸逃。"""
    now = now or timezone.now()
    if not binding.enabled:
        return 0
    if client is None:
        with jidanpu.member_session(binding) as (sdk, _http):
            return _pull_statuses(binding, client=sdk, now=now)
    return _pull_statuses(binding, client=client, now=now)


# ══ 接单兜底网（快路漏行时的在途单档收口）══════════════════════════════════
#: 新上厅的单优先复查，留一半预算轮转积压。
CLAIM_NET_BUDGET = 50
CLAIM_NET_RECENT_WINDOW = timedelta(minutes=30)


@dataclass
class _ClaimNetProgress:
    older: tuple[datetime | None, int] | None = None
    next_recent_id: int | None = None
    recent_first: bool = True


_claim_net_positions: dict[tuple[str, int], _ClaimNetProgress] = {}


def claim_net(binding, *, client=None, now=None) -> int:
    """跑一轮**接单兜底网**：本地仍 `WAITING_GRAB` 且链路还在对方大厅（`in_hall=True`）的单，
    逐张回查远端真实状态，已被接走 ⇒ 走**既有** `_handle_alarm` → `machine_claim_order` 自动接手
    （守卫/审计/台账/竞态分流全部是那一份，⛔ 没有第二份接手逻辑）。⭐ 它是快路游标护栏
    （`_advance_cursor`）的兜底：落库延迟超过护栏的接单行快路永远看不到，由本网每 180 秒收口
    （慢路 `pull_statuses` 只兜「已完成」，兜不了「已被接走未完成」）。结局聚合照 `_poll_log`：
    `unreadable` ⇒ 中止本轮 + `_stamp_poll_health(ok=False)`；`refused`/`denied`/`unreadable_row`
    ⇒ 记 loud、继续；轮末有 loud ⇒ 盖 `last_error`，无 ⇒ 清（与快路同一健康面）；返回处理的行数。
    ⚠️ `client` 是可测性注入口（照 `poll_log`/`pull_statuses` 的形状）；停用绑定 ⇒ 一次上游都不打回 0；
    选取集为空 ⇒ 零上游早退（照 `_pull_statuses` 空集那条：不动健康列，一次空转不该清掉别路刚写的真错）。"""
    now = now or timezone.now()
    if not binding.enabled:
        return 0
    if client is None:
        with jidanpu.member_session(binding) as (sdk, _http):
            return _claim_net(binding, client=sdk, now=now)
    return _claim_net(binding, client=client, now=now)


def _claim_net(binding, *, client, now) -> int:
    """优先复查新上厅的单，其余单从本绑定上次检查的位置继续。"""
    LAST_CALL.record_outlet(getattr(client, "_tt_egress_outlet", None))
    key = (connection.schema_name, binding.pk)
    progress = _claim_net_positions.get(key) or _ClaimNetProgress()
    links_query = JidanpuMemberOrderLink.objects.filter(
        binding=binding,
        in_hall=True,
        order__status=OrderStatus.WAITING_GRAB,
        order__deleted_at__isnull=True,
    ).select_related("order")
    recent = list(
        links_query.filter(listed_at__gte=now - CLAIM_NET_RECENT_WINDOW)
        .order_by("-listed_at", "-id")[:CLAIM_NET_BUDGET // 2]
    )
    if recent:
        links_query = links_query.exclude(pk__in=[link.pk for link in recent])
    links_query = links_query.order_by(F("listed_at").asc(nulls_last=True), "id")
    remaining = links_query
    if progress.older is not None:
        listed_at, pk = progress.older
        if listed_at is None:
            remaining = links_query.filter(listed_at__isnull=True, pk__gt=pk)
        else:
            remaining = links_query.filter(
                Q(listed_at__gt=listed_at) | Q(listed_at=listed_at, pk__gt=pk)
                | Q(listed_at__isnull=True)
            )
    budget = CLAIM_NET_BUDGET - len(recent)
    rotating = list(remaining[:budget])
    if not rotating and progress.older is not None:
        progress.older = None
        rotating = list(links_query[:budget])
    if not recent and not rotating:
        _claim_net_positions.pop(key, None)
        return 0
    _claim_net_positions[key] = progress
    if progress.next_recent_id is not None:
        for index, link in enumerate(recent):
            if link.pk == progress.next_recent_id:
                recent = recent[index:] + recent[:index]
                break
    # 轮换两组的先后，偶发单查错误中止一轮时，两组仍能继续检查。
    groups = ((recent, True), (rotating, False))
    if not progress.recent_first:
        groups = tuple(reversed(groups))
    progress.recent_first = not progress.recent_first
    processed = 0
    loud: str | None = None
    for links, is_recent in groups:
        for index, link in enumerate(links):
            if is_recent:
                progress.next_recent_id = links[(index + 1) % len(links)].pk
            else:
                progress.older = (link.listed_at, link.pk)
            outcome, detail = _handle_alarm(
                binding, link, row={"joinOrderId": link.jidanpu_order_id},
                client=client, now=now,
            )
            if outcome not in ("done_marker", "deleted", "busy"):
                LAST_CALL.upstream = True
            if outcome == "unreadable":
                _stamp_poll_health(binding, now=now, ok=False, reason=detail)
                return processed
            if outcome in ("done_marker", "busy"):
                continue
            if outcome in ("refused", "denied", "unreadable_row"):
                loud = loud or detail
            processed += 1
    _stamp_poll_health(binding, now=now, ok=loud is None, reason=loud or "")
    return processed
