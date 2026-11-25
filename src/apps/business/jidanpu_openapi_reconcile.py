"""鸡蛋铺官方 API 的**兜底对账**（本线 T8）：回调漏了的时候靠它兜住（spec §8）。 > spec：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` > §8（兜底对账，全节）/ §12.1-D6（代价）/ §13.4 的 **E9 / E10**；任务书 T8。 ## 一分钟看懂它在干什么回调（T6）是**主路**：对方推一条 → 我们主动查一次权威状态 → 写回我方订单。但这条路**确定会漏**（spec §8.1 的三条实测理由：重试策略文档没写、实测 0.5% 重复、 **我们每次发版重启那段是确定会漏的**），而「漏了」在回调这条路里**看不出来**：没收到就是没收到，台账上也不会有一行。本模块是**兜底**：低频（`reconcile_seconds`，默认 600 秒）、**逐单** （`GetOrderStatus?orderId=` 是单数参数 ⇒ 只能一条一条查）、**有上限**（50 张）地把「手上有链路、还没到终态」的在途单捞出来，各查一次权威状态。 ``` 一轮（每 reconcile_seconds 一次） ① 候选 = 本绑定名下**在途**的链路（见下「查什么」；只 `.count()` + 切窗口，⛔ 不全量捞） ② 预算 50 = **最新 15 张**（从未回拉 + 待抢单）+ **轮转 35 张**；候选更多 ⇒ 响亮登记 ③ 逐单：查 GetOrderStatus ⇒ plan_openapi_status 裁决 ⇒ 该写才写（「这一张单」的报文坏 ⇒ 记一条 `reconcile_stuck` 跳过它；整条路的故障 ⇒ 本轮停住） ④ 顺带：把「回调漏了多少、漏了哪些」变成**台账上看得见的一行** ``` ## 查什么（⛔ 别把全部历史单都查一遍；spec §8.2）候选 = **本绑定**名下的 `JidanpuUpstreamOrderLink`，**且**同时满足三条： | 判据 | 为什么 | |---|---| | `terminated_at IS NULL` | 已停跟踪 = 两边都到过终态（模型 `is_terminated` 的既定语义）⇒ 查了也写不进去 | | 我方订单**没软删** | `apply_transition` 加锁读的是默认 manager（已软删单读不到）⇒ 放它往下走会**抛异常**（同 T6 第 ⑤ 步那条判据） | | 我方状态 ∉ `CLOSED_STATUSES`（已完成 / 已退单） | spec §8.2 的「还没到终态」= **在途**才算「可能已经变了但我们不知道」 | ⚠️ 三条都是**候选的定义**（不是「查了之后跳过」）⇒ 它们各自**不**留台账，理由与「已停跟踪的链路不查」同族：那是**按定义不做**的事，逐轮为它留痕只会刷屏。 ⚠️ 复用 `jidanpu_reconcile.CLOSED_STATUSES`（租户级那条线对「我方闭合态」的既有枚举） ——⛔ 不在这里另写一份「哪些算终态」。 ## 上限怎么处置（⭐ 本模块唯一有裁量的一处，spec §8.2 与任务书的合成口径）
- **上限 = 50**（`OPENAPI_RECONCILE_MAX_PER_ROUND`，spec §8.2「取小」）。 ⚠️ 调用方可以传一个**更小**的 `max_orders`（「立即同步」那一跳是**同步 web 请求**，见 `reconcile_openapi_statuses` 的 `max_orders` 那一段）；不传 = 上面这个契约值。 - **50 怎么分（停滞修复，G3）**：一轮 50 张 = **最新 15 张**（`last_in_at IS NULL` 且我方「待抢单」，按 `pk` 降序）+ **轮转 35 张**。为什么要给新单留固定名额：轮转窗口按 `pk` 升序走，一张刚建的单排在最后，要等一整圈（在途 4700+ 条时约 6.8 小时）才轮到 —— 而「刚建的单、对方已经接走」恰恰是最需要立刻对齐的那一类（2026-10-22 事故那张单就是这样：14:16 建单、14:35 被接、14:50 人工指派）。 ⚠️ **`last_in_at` 的实际情况（已知口径，⛔ 不是 bug）**：这一列今天**只有上家链路** （`JidanpuUpstreamOrderLink`，由 `jidanpu_upstream` 的慢路写）有写入点； **下家链路 `JidanpuMemberOrderLink.last_in_at` 没有任何写入者** ⇒ 下家线上「`last_in_at IS NULL`」恒成立，「最新 15 张」实际取的是**我方还停在「待抢单」的、按 `pk` 降序的前 15 张**。要真区分「从未回拉」得**另开一条线**给下家对账补写 `last_in_at` （本计划不做，⚠️ 别把这里的恒成立当成「判据坏了」）。 - **窗口起点是钟摆的纯函数（G1）**： `start = (int(now.timestamp()) // interval * width) % total` （`interval` = 该绑定自己的 `reconcile_seconds`；`width` = 轮转那一段的宽度）。 ⇒ 进程重启**不改变**窗口位置（⛔ 不再有进程内游标）；一圈走完自然回到开头。 ⚠️⚠️ **步长是「轮转那一段的宽度」（50 的预算下是 35），⛔ 不是 `cap`（50）**：步长 50 而每轮只覆盖 35 ⇒ 每圈都会留下 **15 个永远不在任何窗口里**的空位（那一截既不在本轮的 `[start, start+35)`，也不在下一轮的 `[start+50, …)`）⇒ 那些单**永久** 轮不到。计划字面公式写的是 `cap`，那是会漏单的写法，**下一任别按计划字面改回 50**。代价如实登记：窗口位置与「上一轮实际停在哪」不再严格衔接（同一条单可能在相邻两轮各被查到一次，`我方 == 目标` 那一格让重查不产生任何写入）—— 换来的是「发版重启后不会永远从最前面 50 张重来」。 - **撞上限的处置 = 「只推迟不丢」**：候选比预算多时**响亮登记**一条台账（`openapi.reconcile_cap`）+ 进本轮 `findings`（⇒ 调度层盖进绑定 `last_error`）——
⛔ 不是「悄悄漏掉一部分」（spec §8.2 明禁的那一形态）。 ⚠️ **与 spec §8.2 的一处口径差异（如实登记，⛔ 不假装一致）**：那一格写的是「超过上限 ⇒ **本轮不动作** + 响亮登记」（照 `jidanpu_reconcile.py` 的 `LOOKUP_CAP_REASON` 先例）。本模块实现的是「**查前 N 张（轮转窗口）+ 剩下的下一轮必然接着查** + 响亮登记」。取舍理由： - §12.1-**D6** 明写兜底对账的承诺是「最坏情况一张单的状态**延迟约 10 分钟**对齐」；若「本轮不动作」，那么一个**在途单一直多于 50 张**的绑定就**永远不动作**（候选集不会自己变小），那条承诺对它**永远**不成立 —— 而这类绑定恰恰是最需要兜底的； - §8.2 那一格真正禁止的是「**悄悄**漏掉一部分」，本实现既不静默（台账 + `last_error`）也不丢（窗口轮转 ⇒ 每张单在 `ceil(候选数 / 35)` 轮内**必然**被查到）。 - 📌 **重新评估的触发条件**：若运维口径改回「超过上限就整轮不动」（例如对方限流实测很紧），把 `_window()` 换成「候选 > 上限 ⇒ 直接回空窗口 + 登记」即可，其余一个字不用动。 ## 与回调那条路怎么「不打架」（spec §8.3 的三条，逐条落地） | 条 | 落地形态 | |---|---| | ① **同一个闸、同一个幂等判据** | 本模块**不自己判**「该不该写」：整段交给 `jidanpu_openapi_consume.apply_authoritative_status`（它内部调 `plan_openapi_status` ⇒ `should_write_status`）⇒ 两条路同一个判据、`ours == target` 两边都不写 | | ② **顺序**（一轮里先消费回调、后对账） | 由调度层保证：`jidanpu_sync._run_upstream_binding` 里对账那一段排在 ⓪ 回调消费**之后**（同轮里不会出现「对账把回调刚写的值又写回去」——即使写了也是同一个值，它俩算的是同一个目标态） | | ③ **写入面完全一致** | 同一个 `apply_transition` 出口 + 同一个 `nodes_of(binding.user)` + 同一个 `suppress_outbound_sync=True` + 同一个 `actor`（它流进 `order.updated_by` ⇒ 换成第二个名字就等于写面不一致）+ 同一套审计列。⛔ 不许对账那条路「多写一列」（判据 = `tests.test_jidanpu_openapi_reconcile` 的 E9 两条断言） | ⚠️ 同一张单两边都想写时**谁说了算**：**没有优先级**，因为两边算的是**同一个目标态** （都来自 `GetOrderStatus` 的权威值 + 同一个 `should_write_status`）⇒ 谁先写谁后写，落到我方订单上的**都是同一个值**；而「我方 == 目标 ⇒ 不写」那一格让第二笔**连审计都不产生**。真正的串行化点是 `apply_transition` 自己的**行锁 + 复读**（陈旧状态抛 `IllegalTransition` ⇒ 只登记不改状态，E-0022 的补偿路径）。 ## 失败要分类（G2）一张单「查不动」有两种完全不同的原因，处置相反： | 档 | 指纹 | 处置 | |---|---|---| | **这一张单**坏了（`data` 不是对象 / `data.status` 不是非空字符串） | `kind=OTHER` + `http_status=200` + **没有** `code` + `step=GetOrderStatus` | 留一条 `openapi.reconcile_stuck`（带单号）⇒ **跳过它，接着查下一张**（⛔ 一张坏单不许把整个窗口永久钉死） | | 传输层 / 5xx / 取 token / 权限①②/ 别的业务失败 | 其余一切（含 `kind=OTHER` 但 `http_status` 不是 200，或带了 `code`） | **本轮就停在这里**（窗口不前移那套在本模块已由钟摆接管；但这一轮不再往下打） | ⚠️ 判据只有一处：`_is_order_specific_failure`。⚠️ 它的输入是 `status_queries` 记下的**失败指纹** `(step, kind, http_status, has_code)` —— ⛔ 不解析 `msg` 文案。 ## ⛔ 本模块**不**做什么（边界，别顺手加） - ⛔ **不打任何写接口**：只调 `GetOrderStatus`（⛔ 录单 / 改单 / 传图 / 改价是第二期）。 - ⛔ **不判「该不该写」**（唯一出处是 `plan_openapi_status` → `should_write_status`）。 - ⛔ **不碰模型 / 迁移**（T1 那九列是冻结的）：窗口位置是**由时钟算出来的**，一个字节都不落库。 - ⛔ **不做健康列展示**（T12）、⛔ 不碰前端、⛔ 不改接收器那条线。 - ⛔ **不加间隔闸**：闸门（`reconcile_seconds`）在调度层（`jidanpu_sync` 的 `_poll_seconds` + 进程内锚），照 spec §8.3 那一行的口径 —— 本模块只管「跑一轮」。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone as django_timezone

from apps.business import jidanpu_openapi, jidanpu_openapi_consume, jidanpu_upstream
from apps.business.jidanpu_reconcile import CLOSED_STATUSES
from apps.business.models import (
    JidanpuSyncEvent,
    JidanpuUpstreamOrderLink,
    OrderStatus,
)

logger = logging.getLogger(__name__)

#: 一轮**最多查几张**（spec §8.2：「取小」，第一版 **50**）——这是**兜底**，不是主路。 ⚠️ 上限的用途是保护上游（spec §12.2-R2 明写「官方 API 的限流没量过」）与让一轮的
#: 耗时 / 请求数有界；撞上限的处置见模块 docstring（窗口轮转 ⇒ 只推迟不丢）。 ⚠️ 它**不是**「每张单每轮都查」：候选超过它时每张单的周期 = `候选数 / 35 × reconcile_seconds`
#: （35 = 轮转那一段的宽度，见 `OPENAPI_RECONCILE_FRESH_PER_ROUND`）。
OPENAPI_RECONCILE_MAX_PER_ROUND = 50

#: ⭐ **最新优先**那一段占几个名额（停滞修复，G3）：一轮 50 = 最新 15 + 轮转 35。判据 = `last_in_at IS NULL`（从未回拉过）且我方「待抢单」，按 `pk` **降序**。
#: ⚠️ 下家链路的 `last_in_at` **没有写入点** ⇒ 那一半判据在下家线上恒成立，实际就是「待抢单、`pk` 降序」（已知口径与理由见模块 docstring 那条 ⚠️）。
#: ⚠️ 为什么份额是固定的（而不是「有多少算多少」）：轮转那一段的**步长**必须固定，否则同一张单可能被跳过一整圈（步长随最新那批的数量抖动 ⇒ 覆盖出现空洞）。
OPENAPI_RECONCILE_FRESH_PER_ROUND = 15

#: 预留「最新优先」名额所需的**最小**预算：预算太小（`< 2 × 15`）时整份预算都给轮转
#: （否则轮转只剩个位数，一圈要走几十轮）。「立即同步」那一跳传一个更小的 `max_orders`
#: 时走的就是这一支（那时「先把最新的几张看一眼」比「按 pk 轮转」更有用，但它本来就只查几张）。
OPENAPI_RECONCILE_FRESH_MIN_BUDGET = 2 * OPENAPI_RECONCILE_FRESH_PER_ROUND

#: 本线**自己**的台账 kind（与 T6 的 `openapi.*` 同前缀；⛔ 复用它那几个名字会让
#: 「这条台账是谁写的」查不出来）。长度 ≤ 32（列宽）。
KIND_RECONCILE_CAP = "openapi.reconcile_cap"
#: ⭐ 兜底对账**跳过一张坏单**（G2）：`kind` 与上面那个**必须**分开——
#: 「整轮没兜全（候选多于预算）」与「这一张单本身查不动」是两种事实，处置与人话都不同。
KIND_RECONCILE_STUCK = "openapi.reconcile_stuck"

#: `target_type` 那一列的取值（≤ 16 字符）：对账这条路上**没有我方订单可指认**的那些事实
#: （撞上限）用它当幂等键的一半。⛔ 不用 T6 的 `callback`：那会让两类事实混成一堆。
TARGET_TYPE_RECONCILE = "reconcile"

#: 人话（⛔ 不含凭据）。⚠️ **⛔ 不把会变动的计数写进 `reason`**：台账的幂等键是 `(kind, target_type, target_ref, reason)` 四元组 ⇒ 计数一进 reason，候选数每变一次就多一条台账
#: （刷屏，而不是留痕）。⇒ 这一句只点名**上限这个常量**，计数进 `payload`（仓内先例： `jidanpu_logs._write_round_event` 与 `jidanpu_reconcile` 的跳过类 reason 同一条口径）。
REASON_CAP = (
    "本绑定这一轮要兜底查的在途单**超过一轮的上限（{cap} 张）**⇒ 本轮只查一个轮转窗口"
    "（查过的那一格下一轮让位），剩下的**下一轮必然接着查**"
    "（⛔ 不是「悄悄漏掉一部分」；具体张数在 payload 里）"
)

#: 人话：**跳过一张坏单**（⛔ 不含凭据、⛔ 不含会变动的计数——计数进 `payload`）。
REASON_STUCK = (
    "这张单查不动（对方回的成功报文里 `data.status` 的形状不对 / `data` 不是对象）"
    "⇒ 本轮**跳过它继续查下一张**；单号见台账的 target_ref"
)


def reset_process_state() -> None:
    """**空操作**——G1之后本模块**没有**进程内状态了。 ⚠️ 为什么还留着这个函数：`tests.*` 与 `tests.test_jidanpu_openapi_entry_gate` 在
    `setUp` / `addCleanup` 里调它（那是游标时代的隔离动作）。删掉函数要改一圈调用点，
    而「保留一个空函数」把改动面收在**本模块**里；⛔ 别因为它还在就以为还有状态要清。"""


# ══ 一、一轮的结论（纯数据；调度层照它打 stdout / 盖 last_error）════════════════


@dataclass(frozen=True)
class ReconcileOutcome:
    """一轮兜底对账跑了什么（人话汇总 + 计数，⛔ 不含任何凭据）。 - `enabled`：这一行绑定能不能跑对账（②式 + 凭据齐）。`False` ⇒ 一次库都不查、一次上游都不打； - `candidates`：本轮**在途**的候选张数（口径见模块 docstring「查什么」）；
    - `queried`：真打出去的 `GetOrderStatus` 次数（≤ `OPENAPI_RECONCILE_MAX_PER_ROUND`）； - `applied` / `skipped` / `failed`：写了状态 / 有结论但没写 / 要人看的条数；
    - `deferred`：本轮**没查**的候选张数（撞上限 / 中途停住那一档；0 = 全查完了）； - `stopped`：本轮**中途停住**（传输层 / 5xx / 取 token / 权限档失败）⇒ 这一轮不再往下打； - `stuck`：本轮**跳过**的「这一张单坏了」条数（每条都有一条 `reconcile_stuck` 台账）； - `findings`：要人能看见的几句人话（台账另有行；调度层把它们盖进 `last_error`）。"""

    enabled: bool = False
    candidates: int = 0
    queried: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    deferred: int = 0
    stopped: bool = False
    #: ⭐ 本轮**跳过**的坏单条数（G2；计进 `failed` 的总数里，但单独给一格：它不等于「整轮坏了」）。
    stuck: int = 0
    findings: tuple[str, ...] = ()
    #: ⭐ **这一轮没跑**的人话原因（spec §9.5 ②：凭据刚被①档拒过 ⇒ 取数整体不打了）。
    #: 空串 = 正常跑了（`enabled=False` 那一档不是它：那是「这行不走②式 / 凭据不全」）。
    blocked_reason: str = ""
    #: ⭐ **这一段的取 token 那一跳被拒过吗**（spec §9.5 ②，T12；任一档）。见消费那一边同名字段。
    token_rejected: bool = False

    @property
    def alert(self) -> str:
        """要写进绑定 `last_error` 的那一句话（⛔ 空串 = 没有要人看见的事）。"""
        return ("；".join(self.findings))[: jidanpu_upstream.ROUND_REASON_MAX]

    @property
    def text(self) -> str:
        """stdout 汇总里的那一段（⛔ 空串 = 这一行不是②式，别打噪音）。"""
        if not self.enabled:
            return ""
        if self.blocked_reason:
            return "对账 本轮未跑（凭据刚被拒，退避窗口内）"
        if not self.candidates:
            return "对账 无在途单"
        head = f"对账 候选 {self.candidates}/查 {self.queried}"
        if self.queried:
            head += f"/写 {self.applied}/跳过 {self.skipped}/要人看 {self.failed}"
        if self.deferred:
            head += f"· 推迟 {self.deferred}（撞上限，下一轮接着查）"
        if self.stuck:
            head += f"· 跳过坏单 {self.stuck}"
        if self.stopped:
            head += "· 中途停住（下一轮重试）"
        return head


# ══ 二、候选与窗口（⛔ 判据只有这一处）══════════════════════════════════════════


def _candidates(binding):
    """本绑定名下**在途**的链路（口径见模块 docstring「查什么」；三条判据各自承重）。 ⚠️ 这是**上家线**的候选（⭐ `jidanpu_openapi_consume.upstream_line().candidates` 指向它）。下家线（`JidanpuMemberBinding`）的候选在 `jidanpu_member_openapi`，判据是**下家线自己**
    那一条（`jidanpu_member.pullable_links`）——两张链路表的列不同、终态口径也不同， ⛔ 不许互相套用。 ⭐ **回一个 QuerySet（⛔ 不是一个 list）**（停滞修复，G3）：调用方要 `.count()` + `LIMIT/OFFSET` 切窗口；先把它全量材料化成 list（在途 4700+ 条时每轮
    全捞一遍）正是本线要修的三件事之一。 ⇒ 三条判据全部**落到 SQL**（`order__deleted_at` / `order__status`）：`LIMIT/OFFSET` 要按 SQL 过滤**之后**的集合切，Python 侧再过滤会让偏移量与行数对不上（静默错位）。 ⚠️ `select_related("order")` 照旧：拿到窗口里那几行时，`_apply` 要读 `link.order`。 ⚠️ 排序**固定按主键升序**：钟摆窗口要「按候选表的次序一圈一圈走」就靠它稳定（⛔ 不按时间 / 不按状态：那些会变，窗口的分母会在两轮之间抖动）。"""
    return (
        JidanpuUpstreamOrderLink.objects.filter(binding=binding, terminated_at__isnull=True)
        .filter(order__deleted_at__isnull=True)
        .exclude(order__status__in=CLOSED_STATUSES)
        .select_related("order")
        .order_by("pk")
    )


def _window(total: int, *, now: datetime, interval: int, width: int) -> tuple[int, int]:
    """钟摆窗口的 `[start, end)` —— **纯函数**：同样的 `(total, now, interval, width)` 必得同样的窗口。 `start = (int(now.timestamp()) // interval * width) % total`
    ⚠️ **为什么不再用进程内游标**（2026-10-22 线上事故）：游标住在**进程内存**里 ⇒ 每次发版重启都归零 ⇒ 每轮又从最前面 50 张重来，排在后面的 4700+ 条永远轮不到。钟摆的起点与进程生命周期**无关** ⇒ 重启不回零，一圈走完自然回到开头（`% total`）。
    ⚠️ `width` 必须**与轮转那一段实际取多少张一致**（G3：35）：步长比实际取的多 ⇒ 每圈都会留下一段**永远不在任何窗口里**的空洞（`start` 每次跳 50，窗口只覆盖 35）。 ⚠️ 候选比窗口窄（`total <= width`）⇒ 整份给出去（`[0, total)`），不留空洞。"""
    interval = max(1, int(interval or 0))
    width = max(1, int(width))
    if total <= width:
        return 0, total
    start = (int(now.timestamp()) // interval * width) % total
    return start, min(start + width, total)


def _is_order_specific_failure(fingerprint) -> bool:
    """这次失败是不是「**这一张单**」的事（⇒ 跳过它继续下一张），⛔ 而不是整条路的事。判据四格**合取**（输入 = `status_queries` 记下的指纹 `(step, kind, http_status, has_code)`）： 1. `step == GetOrderStatus` —— 取 token 那一步的任何失败都不是「这张单」的事；
    2. `kind is OTHER` —— 权限档（①②）是**整份凭据**的事； 3. `http_status == 200` —— 传输层 / 5xx / 3xx 一律不是「这张单」的事； 4. **没有** `upstream_code` —— 带了业务码的 `OTHER` 是「对方自己的故障码」，照旧停住
    （⚠️ 那与「没有相关订单信息」不同：后者走 `absent_order_error` ⇒ `OUTCOME_SKIPPED`，根本不会回到这里）。 ⇒ 剩下的**只有**「成功信封 + `data` 的形状不对」这一档（`data` 不是对象 / `data.status` 不是非空字符串）—— 那是**一张单的报文**的问题，下一张单照样该查。 ⚠️ 判据只有这一处；⛔ 不解析 `msg` 文案（对方改一个字我们就会走错档）。"""
    if fingerprint is None:
        return False
    step, kind, http_status, has_code = fingerprint
    return (
        step == jidanpu_openapi.STEP_ORDER_STATUS
        and kind is jidanpu_openapi.FailureKind.OTHER
        and http_status == 200
        and not has_code
    )


# ══ 三、留痕（台账；⛔ 复用既有的幂等写口，不自己写第二份）══════════════════════


def _note_reconcile(*, line, binding, target_ref: str, kind: str, reason: str, status: str,
                    payload: dict | None = None) -> bool:
    """对账这条路上**没有我方订单可指认**的那些事实的台账（撞上限那一档）。 ⚠️ 复用 `jidanpu_upstream._note_once_ref`：它是本仓「没有 order / mirror 可指认」那一档 **唯一**的幂等写口（⛔ 不在这里另写一份去重；T6 的 `_note_event` 走的是同一个口，
    只是 `target_type` 不同）。 ⚠️ `payload` 里⛔ 不许放凭据：绑定 id / 上家单号 / 张数都是定位用的事实。 ⚠️ 「这一行归哪份绑定」那个键名由**这一条线**给（`line.payload_key`）：上家
    `upstream_binding_id` / 下家 `member_binding_id`（⛔ 混用会让排查时认错线）。 ⚠️ 两条线的 kind **必须不同**（见 `LineKinds`）：`target_ref` 里只有绑定主键，而两张表的 pk 可以都是 `1` ⇒ kind 相同就会**互相吞掉**对方的台账（四元组去重）。"""
    data = {line.payload_key: binding.pk}
    data.update(payload or {})
    return jidanpu_upstream._note_once_ref(
        target_type=TARGET_TYPE_RECONCILE, target_ref=target_ref[:191], kind=kind,
        status=status, reason=reason[: jidanpu_openapi_consume.REASON_MAX],
        direction=JidanpuSyncEvent.Direction.IN, payload=data,
    )


# ══ 四、主入口：跑一轮兜底对账 ══════════════════════════════════════════════════


def reconcile_openapi_statuses(binding, *, now: datetime | None = None,
                              token_blocked: str = "",
                              max_orders: int | None = None,
                              line=None) -> ReconcileOutcome:
    """跑**一轮**兜底对账（spec §8）：查在途单的权威状态，把回调漏掉的那些补上。调用点：`jidanpu_sync._run_upstream_binding` 里 **⓪ 回调消费之后**（spec §8.3 的顺序），下家线（`JidanpuMemberBinding`）的同一个位置见 `jidanpu_sync._run_member_binding`；按该绑定自己的 `reconcile_seconds` 判到点（锚在调度层，⛔ 本模块不加闸）； ⭐ 另一个调用点是「立即同步」端点（②式那一跳，**同步 web 请求**——见 `max_orders`）。 ⚠️ 本函数**不接任何请求来源的东西**：一行绑定 + 一个可选的 `now`（给用例）——
    ⛔ 没有租户标识、没有地址、没有凭据入参（`AGENTS.md` §4 的租户边界）。 ⭐ **`line`（下家线收口）**：这一行绑定属于**哪条线**（`CallbackLine`，定义与语义见 `jidanpu_openapi_consume.CallbackLine`）。`None` ⇒ 上家线（`jidanpu_openapi_consume.upstream_line()`，行为**逐字**是加这个参数之前的那一份）。 ⚠️ 本模块**一行判据都不新增**：候选（`line.candidates`）、查→判→写（`line.apply`）、台账键名（`line.payload_key`）全部来自那一条线 ⇒ 两条线的差异**只有**「线对象」这一处，
    而「窗口轮转只推迟不丢」那套口径**只有这一份**（⛔ 别为下家再写一份）。 ⭐ **`max_orders`**：这一轮最多查几张单；`None` ⇒ 本模块的契约值 `OPENAPI_RECONCILE_MAX_PER_ROUND`（⛔ 默认值的出处只有它这一处）。 ⚠️ 为什么要有这个入参：「立即同步」那一跳是**同步 web 请求**（用户在页面上等它返回， gunicorn 的 worker 超时是 30 秒）⇒ 它必须用比常驻调度**小得多**的窗口（常驻那条路撞上限只是「下一拍接着查」，而这一跳撞上限是「页面一直转圈」）。 ⚠️ 窗口的语义**两条路共用一份**（`_window` 那个钟摆纯函数）：换一个 `cap` 只影响「这一轮查哪一段」，⛔ 不改变「只推迟、不丢」这条口径。 ⭐ **`token_blocked`（spec §9.5 ②，T12）**：非空 = 调度层已经知道这一份凭据刚被①档拒过 ⇒ **一次都不打那个必被拒的接口**（连候选都不查），回一个「本轮未跑」的结论。 ⚠️ 为什么必须由调度层传：**同一轮里消费那一段刚刚拒过**，本段再打一次就是「反复打一个必拒的接口」——实测一轮会因此打**两次**取 token（T12 的探针读数）。"""
    line = line or jidanpu_openapi_consume.upstream_line()
    now = now or django_timezone.now()
    if jidanpu_openapi.openapi_credentials_of(binding) is None:
        # ⛔ ①式 / 凭据不全 ⇒ **一次库都不查、一次上游都不打**（与 T6 那条提前返回同一条口径）。
        return ReconcileOutcome(enabled=False)
    if token_blocked:
        return ReconcileOutcome(
            enabled=True, findings=(token_blocked,), blocked_reason=token_blocked,
        )

    #: ⭐ 候选是**懒查询**（⛔ 不是 list）：下面只 `.count()` 一次 + 用 `LIMIT/OFFSET` 切窗口，
    #: 在途几千条时不再每轮全量材料化（G3）。
    candidates = line.candidates(binding)
    total = candidates.count()
    if not total:
        return ReconcileOutcome(enabled=True)

    #: 本轮这一条路的窗口上限（⛔ 默认值只在模块常量那一处定义，这里只做「传了就照传的算」）。
    cap = OPENAPI_RECONCILE_MAX_PER_ROUND if max_orders is None else max_orders
    #: 该绑定自己的对账间隔 ⇒ 钟摆窗口每过这么久走一格（⛔ 不在这里另设第二套间隔判据：
    #: 「到不到点」仍然只由调度层按同一列判）。
    interval = getattr(binding, "reconcile_seconds", 0) or 0
    #: ⭐ 预算怎么分（G3）：够大 ⇒ 最新 15 + 轮转（cap-15）；太小 ⇒ 整份给轮转。
    fresh_quota = (
        OPENAPI_RECONCILE_FRESH_PER_ROUND
        if cap >= OPENAPI_RECONCILE_FRESH_MIN_BUDGET
        else 0
    )
    rotating_width = cap - fresh_quota

    #: ⭐ **最新优先那一段**（判据 = 从未回拉过 + 我方「待抢单」，`pk` 降序）：
    #: 只要这几张单在候选里，本轮**一定**查它们（轮转那一段按 `pk` 升序走，新单排最后）。
    fresh = (
        list(
            candidates.filter(
                last_in_at__isnull=True, order__status=OrderStatus.WAITING_GRAB,
            ).order_by("-pk")[:fresh_quota]
        )
        if fresh_quota
        else []
    )
    #: **轮转那一段**：起点按钟（`_window`），宽度 = `rotating_width`。
    #: ⚠️ 「最新优先」没用掉的名额补给轮转（它落在下一轮起点**之前** ⇒ 只重叠、不跳格）。
    start, _rotating_end = _window(
        total, now=now, interval=interval, width=rotating_width,
    )
    budget = rotating_width + (fresh_quota - len(fresh))
    rotating = list(candidates[start:start + budget])
    if len(rotating) < budget and start:
        #: 窗口跨到表尾 ⇒ 从表头补上（⛔ 不能少给：少给就是「这一圈有一段永远不在窗口里」）。
        rotating += list(candidates[: budget - len(rotating)])

    window: list = []
    seen_pks: set[int] = set()
    for link in (*fresh, *rotating):
        if link.pk in seen_pks:
            continue
        seen_pks.add(link.pk)
        window.append(link)
    #: ⚠️ 单一预算的**最后一道闸**：窗口永远不超过 `cap`（⛔ 不许「窗口 100 而 cap 50」——
    #: 那会让第 51 张在 `status_of` 里撞上限回 `None` ⇒ RETRY ⇒ 又一轮冻死）。
    window = window[:cap]
    deferred = total - len(window)

    findings: list[str] = []
    if deferred:
        # ⭐ 撞上限 ⇒ **响亮登记**（⛔ 不是静默：这一行就是「兜底这一轮没兜全」的唯一痕迹）。 ⚠️ `note_once` 会把重复的同一句去重（⛔ 不刷屏），而 `findings` **每轮都进**
        # （⇒ 调度层每轮都把那句话盖在绑定 `last_error` 上）——那正是「看得见」：一个在途单长期多于上限的绑定，页面上要一直能看到「兜底没兜全」。
        reason = REASON_CAP.format(cap=cap)
        _note_reconcile(
            line=line, binding=binding, target_ref=f"{binding.pk}:cap", kind=line.kinds.reconcile_cap,
            status="failed", reason=reason,
            payload={
                "cap": cap,
                "candidates": total,
                "deferred": deferred,
                "window_start": start,
            },
        )
        findings.append(reason)

    applied = skipped = failed = stuck = 0
    processed = 0
    stopped = False
    with jidanpu_openapi_consume.status_queries(
        binding, max_orders=cap
    ) as queries:
        for link in window:
            # ⭐ 逐单走**与回调同一条**写入面（查 → 裁决 → 该写才写）：两条路调的是同一个
            # `line.apply`（上家 = `apply_authoritative_status`，下家 = 下家线自己那一段）。
            outcome = line.apply(
                order=link.order, link=link, binding=binding,
                order_no=line.remote_order_id(link),
                queries=queries, now=now, findings=findings,
                trigger=jidanpu_openapi_consume.TRIGGER_RECONCILE,
            )
            if outcome == jidanpu_openapi_consume.OUTCOME_RETRY:
                fingerprint = queries.last_failure
                if _is_order_specific_failure(fingerprint):
                    # ⭐ **只有「这一张单」的失败才让过**（G2）：留一条带单号的 `reconcile_stuck`
                    # 台账，然后**接着查下一张**（⛔ 一张坏单不许把整个窗口永久钉死 ——
                    # 那次事故里那种「一格永远不动」的形态就是这么来的）。
                    _note_reconcile(
                        line=line, binding=binding, target_ref=link.order.order_no,
                        kind=line.kinds.reconcile_stuck, status="failed", reason=REASON_STUCK,
                        payload={
                            "order_no": link.order.order_no,
                            "remote_order_no": line.remote_order_id(link),
                            "failure": {
                                "step": fingerprint[0],
                                "kind": str(fingerprint[1]),
                                "http_status": fingerprint[2],
                                "has_code": bool(fingerprint[3]),
                            },
                        },
                    )
                    stuck += 1
                    failed += 1
                    processed += 1
                    continue
                # 传输层 / 5xx / 取 token / 权限①② / 对方故障码 ⇒ **本轮就停在这里**
                # （同 T6 那条批处理口径：一次上游故障不该被放大成一整窗的失败台账）。
                # 「要人看」的那句话已经在 findings 里。
                stopped = True
                break
            processed += 1
            if outcome == jidanpu_openapi_consume.OUTCOME_APPLIED:
                applied += 1
            elif outcome == jidanpu_openapi_consume.OUTCOME_SKIPPED:
                skipped += 1
            else:
                failed += 1
        queried = queries.queried
    if stuck:
        #: 一句**有界**的汇总进错误列（每张坏单自己还有一条 `reconcile_stuck` 台账）。
        findings.append(f"对账 本轮跳过 {stuck} 张查不动的单（单号见台账）")

    return ReconcileOutcome(
        enabled=True, candidates=total, queried=queried, applied=applied,
        skipped=skipped, failed=failed, stuck=stuck,
        # 「推迟」= 候选里本轮**没查**的（撞上限剩下的那一截 + 中途停住时窗内没走完的）。
        deferred=total - processed, stopped=stopped, findings=tuple(findings),
        token_rejected=queries.token_rejected,
    )


__all__ = [
    "KIND_RECONCILE_CAP",
    "KIND_RECONCILE_STUCK",
    "OPENAPI_RECONCILE_FRESH_PER_ROUND",
    "OPENAPI_RECONCILE_MAX_PER_ROUND",
    "REASON_CAP",
    "REASON_STUCK",
    "ReconcileOutcome",
    "reconcile_openapi_statuses",
    "reset_process_state",
]
