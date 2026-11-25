"""开放平台**推送 outbox**：三类事件的入队、取件、重试记账与机会式清理（T8；设计文档 §7.2 / §7.4）。

## 一张表两种角色

`OpenApiDelivery`（T1 交付）既是 outbox 又是台账，形状照抄仓库既有先例
（`apps/business/models.py` 的 `PeerSyncEvent` + `apps/business/peer_worker.py`）。

## ⭐ 入队必须与业务写入**同一个事务**（§7.4 的**改准块**，先读它再改这里）

`notify_*` 三个入口**只** `create` 行，⛔ 不排 `transaction.on_commit`、⛔ 不发 HTTP。
理由（设计文档 §7.4 逐字）：`on_commit` 的语义是「**提交之后**才执行」——
用它写 outbox 行，业务回滚了**推送行会留下**（「单没改成、推送却发了」的半截状态）。
仓内两张既有 outbox 表（`PeerSyncEvent` / `JidanpuSyncEvent`）的**全部**插入点也都是
在调用方业务事务里直接 `create`（先例：`peer_dispatch.py` 的四个入队壳）。

⚠️ 因此**调用点必须已经在事务里**（今天 11 处业务写入点都在 `atomic()` 内）。
⛔ 本模块**不自己开事务**去"保证原子性"：自己开事务只会把"入队"与"业务写入"拆成两笔
（离得更远），而正确性来自**同处一个事务**。

## 三类事件与覆盖面（§7.2.1 的裁决；写入点逐个登记在 `tests/test_openapi_event_sites_net.py`）

| 事件 | 写入点 |
|---|---|
| `order.status_changed` | 唯一写入口 `order_status.apply_transition`（在 `suppress_outbound_sync` 闸**之外**，与同一位置的那条 peer 通知同理由）+ **7 处绕过状态机的直接写入**（上下游同步接收侧 5 处、B-1 回拉跨行 `.update()` 1 处、上行回传 1 处） |
| `order.deleted` | `recycle.soft_delete_order`（唯一领域函数；软删**不改** `Order.updated_at` ⇒ 时刻**显式取**） |
| `order.image_added` | 进度图变 `ready` 的**三个**写入点（内部认领 / 同步接收 / 拉图） |

**6 处「出生即带状态」的 create ⛔ 不推**（建单不是状态变化；第三方感知新建单靠它自己那次录单的
应答）——它们在同一张登记网里被**显式登记**为「有意不推 + 理由」，⛔ 不许掉进没人检查的空洞。

## ⭐ 去重判据（**唯一出处**，⛔ 不许"反正对方能去重"）

同一件事可能被多条路径触发（最现实的一条：peer 的 `order.update` 是 **at-least-once**，
同一个状态可以在一个 pending 窗口里进来两次）⇒ 入队前按 **(app, event, order_no)** 取
**每个应用最近的一条行**，再比**身份值**：

| 事件 | 身份值 | 判据 |
|---|---|---|
| `order.status_changed` | `payload["status"]` | 最近一条行的身份**相同** ⇒ 不重复入队 |
| `order.image_added` | `payload["images"][0]["object_key"]` | 同上 |
| `order.deleted` | **无** | 最近一条行**仍在飞**（`pending`）⇒ 不重复入队 |

⚠️ **为什么删除那条多一个"仍在飞"的限定**（这不是顺手加的，去掉它有一条真实形态会被吃掉）：
`order.deleted` 没有可比的取值，若按"最近一条行存在就跳过"，那么
**删 → 恢复 → 再删** 这条链的**第二次删除**会被第一次那条**已投递**的行吃掉
⇒ 第三方永远不知道这单现在是删的。而「同一次删除被两条路径触发」在 pending 窗口内本来
就由 `soft_delete_order` 的幂等早返回挡着（它第一次之后就不再入队）⇒ 这一档只需要挡住
**在飞**那一条。⚠️ 顺带一个**良性合并**：删 → 恢复 → 再删 之间若第一行还没投出去，
第二行不排 ⇒ 对方只收到一次"已删除"，而那正是这单的终态。

⚠️ 状态 / 图片那两档**不限定在飞**：要"回到同一个状态"必须**先经过别的状态**，
而那个状态会排一条更新的行 ⇒ 最近一条的身份自然就不同了（`X → Y → X` 的两条 `X` 都发得出去）。
⇒ 不存在"合法事件被吃掉"的形态。

## 取件：一个 `atomic()` 里打租约，⛔ 事务里不发 HTTP

照 `peer_worker.claim_due_events` 的形状（§7.4 逐字点名）：`select_for_update(skip_locked=True)`
+ `next_attempt_at = now + LEASE_SECONDS`，HTTP 在事务**外**逐条做。

## 顺序：同一订单**按产生顺序**投递（§7.4）

取件按 `(created_at, id)` 升序，并且**丢掉**「同一 `order_no` 还有更早的 `pending` 行」的那些行
（那一行迟早会到点并被投，本行必须等它）⇒ 同一订单不会**并发**、也不会**乱序**。
不同订单之间不保证顺序（§7.4 逐字）。

## 重试：`10s / 60s / 5m / 15m / 1h / 6h`（§7.4）

首次投递**立即**（入队时 `next_attempt_at` 为空 = 立刻可投）。失败后取
`BACKOFF_SECONDS[attempts - 1]`（`attempts` 是**已投递次数**）。

⚠️ **六档全部可达**：第 1–6 次失败各取一档，**第 7 次**失败才转终态 `failed`
（共 7 次投递尝试 = 1 次首发 + **6 次重投**，与 §7.3 的「共 6 次机会」指的是那六档退避同读）。
⛔ 判据**不许**写成 `attempts >= len(BACKOFF_SECONDS)`：那会让最后那档 `6h` **永远用不上**
——同族的坑在 `peer_worker.RETRY_BACKOFF_SECONDS` 的注释里逐字记着（那里是四档 + 第 5 次终态）。
⚠️ 后四档与 `peer_worker` 的 `(60, 300, 900, 3600)` **同族但不同值**：对外 HTTP 的失败更常见、
值得更快重试一次 ⇒ 这是**有意**的差异（§7.4 已登记），⛔ 不是抄漏。
超限 ⇒ `failed` 并**保留行**（页面可见、可手工重投）。

## 「地址空 / 回调停用 / 应用停用」的两处判据

- **入队时**（`eligible_apps()`）：这三种应用**根本不会被选中** ⇒ 不产生行（§7.1「不排队」）；
- **投递时**（`_app_is_deliverable`）：行已经排上了、应用随后被停用 / 地址被清空 ⇒
  该行转**终态 `failed`** + 一句人话 `reason`（**保留行**）。
  ⚠️ 这一档是**有意选的**（§7.1 只写了"不排队 / 不重试 / 不报错"三条）：
  - ⛔ **不重试**：留在 `pending` 会被每一轮重新认领一次（台账永远显示"待投递"，
    且把每轮批量名额吃光 ⇒ 别的应用被静默饿死，同族先例见 `peer_worker._mark_event_dropped`）；
  - ⛔ **不报错**：一行都不抛异常（`run_round` 照常继续）；
  - ✅ **转终态**：页面上看得见"为什么没发出去"，且**人工重投**是显式动作。
  ⚠️ 那**三句话**本身由 `spec.callback_disabled_reasons` 给（本模块⛔ 不写字面量第二遍）。

## ⭐ 引擎 / 族的切分（本模块经 `CredentialSpec` 驱动；打手侧回调线 T1）

本模块是**两族共用**的投递实现：上家侧（开放平台，本 app）与打手侧（`apps/takerapi`，
本线 T2–T4）。切法四条，逐条都有理由：

1. **引擎一行具体模型都不 import**：投递台账模型取 `spec.delivery_model`、应用模型取
   `spec.app_model`、两个 `callback_*` 列名与"回调启用"值、三个 `Status` 值、三句"不投递"
   人话**全部**取 `spec` ⇒ 连**类型注解**里也不写具体模型名（写了就得 import 它）。
2. **「哪些应用会收」由调用方给**（`enqueue_deliveries(*, apps=…)`），⛔ 引擎不替任何一族猜：
   这件事**每族语义不同**（上家侧 = 本租户所有启用且配了回调的应用，见 `eligible_apps()`；
   打手侧 = "绑定账号 == 这张单当前接手人"那一个应用，且 `order.reassigned` 还要显式传前任）
   ⇒ 收件人**函数**留在各族自己那一侧。
   ⚠️ **为什么是"已算好的一列表"、⛔ 不是"算收件人的可调用对象"**：收件人**因单而异**
   （打手侧那个既要订单、又要"前任接手人"）——做成可调用对象就得靠闭包 / 偏函数把这两个
   东西塞进去，读的人要在两个文件之间来回跳才看得出"这一单到底推给谁"；而列表在**调用点**
   一眼可见。（"可调用对象"那条路会把这个选择藏进引擎，故本线选列表。）
   ⚠️ "**零应用即零成本**"这条纪律**留在引擎内**（`if not apps: return []` 是
   `enqueue_deliveries` 的第一句）：调用方只负责**算**收件人，⛔ 不必自己再写一次早返回
   （写了就是第二份判据）。
3. **状态值与事件名都只有字符串**：`pending` / `done` / `failed` 从 `spec` 取；三个事件名是
   本模块的**字面量常量**（`EVENT_*`，⛔ 引擎不 import 各族模型的 `Event` 枚举）。
4. ⭐⭐ **公开入口的 `spec` 一律必填 keyword-only**（T1b 改准；下面是被改准的那段**原文**，
   按 `AGENTS.md` §4 的口径保留、⛔ 不删）：

   > 4. **公开入口的 `spec` 默认值是上家侧那一份**（`OPENAPI_SPEC`）：为了让**既有调用点一个字
   > 不改**（11 处业务入队点 / 常驻命令 / 既有网都按无参调）。⚠️⭐ 代价必须说破：第二族
   > **忘了**传 `spec=TAKER_SPEC` 时**不会有任何异常**，只会静默地按上家侧那一族办事
   > （查询 / 建行 / 状态都落到上家侧那张台账上）⇒ 那一族**自己**的端到端用例是**唯一**判据
   > （spec §9-7 的"跨族不串台"）。

   ⛔ **本线 T1b 口径**：上面那句代价**不接受**——"打得手侧的回调静默写进上家侧
   台账"正是 spec §2.2 最贵的那个形状（`docs/specs/2026-10-08-taker-open-api-design.md` §4.1-1
   已经为**同款**问题立过规矩：`spec` 必填 ⇒ 漏传 = `TypeError`）。⇒ 那一格默认值**收掉了**：
   `notify_order_status_changed` / `notify_order_deleted` / `notify_order_image_added` /
   `claim_due_deliveries` / `deliver_one` / `prune_done_deliveries` / `run_round` 的 `spec`
   **全部是必填 keyword-only** ⇒ 漏传**当场 `TypeError`**，⛔ 不再有"静默回上家侧"这条退路。
   ⚠️ **代价如实登记**：本任务书说的是"11 处业务入队点"，**实测 `apps/business` 下
   `notify_*` 的直接调用点是 8 处**（`recycle` 1 / `order_status` 1 /
   `api/screenshot_views` 1 / `jidanpu_upstream_pics` 1 / `peer_inbound` 4；那 11 处指的是
   **写入点**，其中状态类写入点是汇进 `apply_transition` 与 `peer_inbound` 里那几条的）
   —— 8 处**逐个显式**写上了 `spec=OPENAPI_SPEC`（⛔ 一条断言都没动），另加常驻命令 1 处与
   既有网里**每一处**直接调用（⛔ 不许用默认参数包一层"少改几处"）。
   ⚠️ **两族各自的收件人仍由各族给**：三个 `notify_*` 是**上家侧那一族的包装**
   （它们给 `apps=eligible_apps()` 与事件名字面量）⇒ 调用方给的是 `spec=OPENAPI_SPEC`；
   打手侧那一族有**它自己**的包装（收件人语义不同，spec §2.2 第 6 项）⇒ ⛔ 不复用这三个函数。

   ⚠️⭐ **T2 追加一道响亮守卫**（`_require_openapi_family`）：那三个 `notify_*` 里
   `spec is not OPENAPI_SPEC` ⇒ **当场 `ValueError`**，消息点名"打手侧请用 `apps.takerapi`
   自己的包装"。理由 = 必填只挡**漏传**、挡不住**传错族**：它们内部算收件人用的是**本族那一套**
   （`eligible_apps()`）⇒ 传错族必然"**半对半错**"（收件人按上家侧查、建行却走别族那张表），
   而且两种结果**都说不到点子上**：租户没有上家侧应用时**静默零行**、有应用时抛一句
   **关于模型类型**的话（两句实测原文见 `_require_openapi_family` 的 docstring）。
   ⛔ **通用引擎入口**（`enqueue_deliveries` / `claim_due_deliveries` / `deliver_one` /
   `prune_done_deliveries` / `run_round`）**保持必填 `spec`、⛔ 不收窄**——它们本来就该按
   `spec` 办事（第二族的常驻命令正是显式传另一份 spec 去调它们）。

   ⛔ 同理（这一条**改准前后一致**）：**引擎内部**的函数（`enqueue_deliveries` /
   `_latest_per_app` / `_is_duplicate` / `_drop_out_of_order_rows` / `_app_is_deliverable` /
   `_attempt`）的 `spec` 也是**必填 keyword-only** —— 忘了它是当场 `TypeError`。

⭐ **那条探针已经落成真测试**（T1b；⛔ 不再是一段只活在报告里的即跑即弃脚本）：
`tests/test_openapi_dispatch.py::ModelNameProbeTests`。判据：**剥掉注释与 docstring**
（`tokenize` 收 `NAME` 记号 ⇒ `COMMENT` / `STRING` 两类记号**天然不算**）之后搜
`OpenApiDelivery` / `ApiApp` / `CallbackState` / `ApiAppState`，本文件命中应为 **0**
（注解里也不写模型名，故不必给注解开口子）。
⚠️ 那条用例有**两条腿**（探针本身要能红）：第二条腿把同一个探针指向
`apps/openapi/credentials.py` 并断言那里**有**命中（≥1）—— 即"模型名只应出现在 spec 定义处"。
⛔ 反过来，**模型名只应出现在 `apps/openapi/credentials.py`**（spec 定义处）。

⚠️ **`callback_url` 本身不合法（非 https / 内网地址）时走的是另一档**：它由
`outbound.deliver()` 在**发请求之前**判定（§4-V6 的「每次投递前重新解析并校验」），
结果是**普通失败** ⇒ **照退避表重试**（⛔ 不是立刻终态）。理由：那条判据之所以要求
"每次投递前重跑"，正是因为 **DNS 可变**——"这次解析到内网"不等于"永远是内网"。
⚠️ 代价（如实登记）：一个**永久**不合法的地址会按 `10s/60s/5m/15m/1h/6h` 试满 7 次
（约 7.5 小时）才落终态 `failed`；期间台账上是 `pending` + 一句人话 `reason`。
⛔ **不许**把它改成"立刻终态"：那会把"DNS 暂时指错"变成一条**永久放弃**的推送。

## ⭐⭐ 受影响网**必须自己推导**，⛔ 不许只按任务书那张表跑（本线连撞三处）

本线实测（四处「清单不完整」，**三处**是任务书 / 设计文档的清单漏的）：T6 漏
`test_openapi_throttling`、R1 漏 `test_openapi_auth`、T8 撞上 `test_jidanpu_schedule` 里把
`SYNC_SERVICES` **整行字面值**钉住的两条断言（设计文档 §13.4-13 只写了
`test_deploy_sync_units` 的四条），以及 `test_order_status.ApplyTransitionTests`
（`SimpleTestCase`，给 `apply_transition` 尾上每一条 DB 触碰都打桩的那个用例）。

⇒ 做法（对**每一个**被改的东西各 grep 一遍，把命中的网**全部**加进受影响集）：

| 被改了什么 | 要 grep 什么 |
|---|---|
| 模块 / 函数名 | 它在 `src/tests/**` 里被谁当**夹具 / 样本 / 桩**用过 |
| **字面值**（清单 / 常量 / 码表 / 表名） | 那个字面值本身（⛔ 不是它的派生短语，`AGENTS.md` §5-C） |
| **新增的 DB 触碰** | 那个函数的调用点里，哪些用例把它当**不落库的替身**在跑（`SimpleTestCase` / 打桩组合） |

⚠️ 跑出红的先判「**是不是我引入的**」：不是 ⇒ 一并修并登记，或**停下报告**，⛔ 不许装作没看见。
"""
from __future__ import annotations

import copy
import logging
import uuid
from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone

from apps.core.storage import get_storage
from apps.openapi import outbound
from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec, app_is_enabled

logger = logging.getLogger(__name__)

#: 失败后的重排间隔（秒）：`10s / 60s / 5m / 15m / 1h / 6h`（§7.4 逐字）。
#: ⚠️ **六档全部可达**：第 1–6 次失败各取一档，**第 7 次**失败才转 `failed` 终态
#: （见模块 docstring；⛔ 不要按 `>= len(...)` 判终态，那会让 `21600` 永远用不上）。
BACKOFF_SECONDS: tuple[int, ...] = (10, 60, 300, 900, 3600, 21600)

#: 取活时打出的**租约**（秒）：投递中被占用，进程中途死掉也能在下一轮被接管
#: （照 `peer_worker.LEASE_SECONDS`，取值同）。
LEASE_SECONDS = 300

#: 一轮最多取多少条（一条一次 HTTP；分批避免一轮拖太长，照 `peer_worker.BATCH_SIZE`）。
BATCH_SIZE = 50

#: `done` 行的保留期（天）。超过它的行由 `prune_done_deliveries()` 删掉。
#: ⚠️ 与 `apps/openapi/idempotency.py::RETENTION_DAYS` 同值（30 天）是**有意的**：
#: 两张表都是"只增不减"的台账，保留期口径一致才不会让人以为其中一张有别的约定。
RETENTION_DAYS = 30

#: 一次清理最多删多少行（⛔ 不许无上限地删：它挂在**常驻投递进程**上，无上限的话积压很多时
#: 一轮会长时间持锁）。取值与 `idempotency.MAX_PRUNE_ROWS` 同。
MAX_PRUNE_ROWS = 500

#: `reason` 列的**写入方截断**上限（字符）。模型那一列是 `TextField`（没有列级长度）
#: ⇒ 由写入方自己截断（模型 docstring 逐字要求）；⛔ 别把对方整段响应体直接塞进来。
MAX_REASON_CHARS = 2000

#: 上家侧三类事件的**字面值**（spec §2.2 第 6 项：事件名由调用方给**字符串**，
#: ⛔ 引擎不 import 各族模型的 `Event` 枚举 —— 那是"具体模型"）。
#: ⚠️ 值必须与 `OpenApiDelivery.Event` 的成员**逐字相同**（既有网钉着那三个字面值）；
#: 引擎的**去重判据**按这三个名字分档（状态 / 图片比身份值、删除看"在飞"）⇒ 只写这一处。
#: ⚠️ 打手侧多一个第 4 类事件（`order.reassigned`）：那一族自己的包装里给字面量，
#: 本引擎对它不做任何特判（走"比身份值"那一档的默认分支）。
EVENT_ORDER_STATUS_CHANGED = "order.status_changed"
EVENT_ORDER_DELETED = "order.deleted"
EVENT_ORDER_IMAGE_ADDED = "order.image_added"

#: ⚠️ **历史对外名**（既有网按这三个**模块名**取它们，`tests/test_openapi_dispatch.py`
#: 的 `EligibilityTests` 就是）⇒ 名字留着，但值**只取自** `OPENAPI_SPEC` 那一格
#: （⛔ 不在这里写第二遍字面量；真正的出处是 `apps/openapi/credentials.py`）。
#: ⚠️ 它们是**上家侧那一族**的三句话：共用引擎一律走 `spec.callback_disabled_reasons`，
#: ⛔ 一个都不许在这里被引擎自己引用（否则第二族会拿到上家侧的文案）。
APP_DISABLED_REASON = OPENAPI_SPEC.callback_disabled_reasons[0]
CALLBACK_DISABLED_REASON = OPENAPI_SPEC.callback_disabled_reasons[1]
CALLBACK_EMPTY_REASON = OPENAPI_SPEC.callback_disabled_reasons[2]


def _clip_reason(text: str) -> str:
    """把失败原因截断到列的上限（**唯一**截断点，⛔ 别在调用方各截一次）。"""
    return (text or "")[:MAX_REASON_CHARS]


def eligible_apps() -> list:
    """**上家侧那一族**的收件人：这次事件要推给哪些应用 = **启用 + 回调启用 + 地址非空**（§7.1）。

    ## ⭐ 为什么它留在这里、而⛔ 不进"引擎"

    "哪些应用会收"**每族语义不同** ⇒ 引擎不收它（见模块 docstring 的「引擎 / 族的切分」）。
    本例是**上家侧**那条语义：**一个应用 = 整租户** ⇒ 收件人与"哪一张单"无关。
    ⚠️ 打手侧那一条**不同**（"绑定账号 == 这张单当前接手人"，且 `order.reassigned` 要传前任）
    ⇒ 它有自己的收件人函数，⛔ 不在这里加分支（加了就是把两族语义塞进一个函数）。

    ⚠️ 三个条件是**与**关系，逐条都有出处：
    - `state == spec.enabled_state_value`：停用应用 ⇒ **整条对外通道都不通**
      （`CallbackState` 的 docstring 逐字，它明说两个开关独立：停用应用停全部、停用回调只停推送）；
    - `callback_state == spec.callback_enabled_value`：§7.1「停用 ⇒ 不产生投递」；
    - `callback_url` 非空：§7.1「地址为空 ⇒ 不产生投递」。
    ⚠️ 三个值全部取自 `OPENAPI_SPEC`（⛔ 本函数里没有 `ApiApp` / `ApiAppState` / `CallbackState`
    的引用）：查询与判据与改造前**逐字等价**，只是"是哪张表 / 哪一列 / 哪个值"由 spec 给。

    ⚠️ **`state` 这个列名是字面量**（⛔ 不是 `spec` 里的字段）：两族的应用模型都叫这一列
    （`ApiApp.state` / `TakerApp.state`，spec §3.1 逐列对应）⇒ 与那几个"今天两族逐字相同的
    列名"同一档；真要出现写法不同的那一族，加的是 spec 字段、⛔ 不是这里的 if。

    ⚠️ **零应用即零成本**：这是入队路径上**唯一**的查询（空就立刻返回，一行都不写、
    也不去碰对象存储），与 `peer_dispatch` 的「零绑定即零成本」同一形状。
    """
    spec = OPENAPI_SPEC
    return list(
        spec.app_model.objects.filter(
            state=spec.enabled_state_value,
            **{spec.callback_state_field: spec.callback_enabled_value},
        )
        .exclude(**{spec.callback_url_field: ""})
        .exclude(kind="business_integration")
        .order_by("id")
    )


def _latest_per_app(apps, *, spec: CredentialSpec, event, order_no) -> dict:
    """每个应用在 `(event, order_no)` 下**最近的一条行**（去重判据的取数面）。

    ⚠️ 一条 `DISTINCT ON` 查询拿全部应用各一行（`distinct("app_id")` 要求 `order_by` 以它开头
    —— 这是 PostgreSQL 的语义，本仓全部环境都是 PostgreSQL 16）。⛔ 不要改成"每个应用查一次"：
    那会把入队路径变成 N+1（应用数就是 N）。
    ⛔ 也**不要**改成"取全部历史行再在 Python 里 max"：一个长寿命订单会被推很多次，
    那等于每次都把它的全部推送行读一遍。
    ⚠️ 表取自 `spec.delivery_model`（`app_id` 这个外键列名两族同形 ⇒ 字面量，见 spec §3.2）。
    """
    rows = (
        spec.delivery_model.objects.filter(
            app__in=apps, event=str(event), order_no=order_no
        )
        .order_by("app_id", "-id")
        .distinct("app_id")
    )
    return {row.app_id: row for row in rows}


def _identity_of(row, event: str) -> str:
    """一条已入队行的**身份值**（判据表见模块 docstring）。

    ⚠️ 图片那一档读的是**库里存的** `object_key`（⛔ 不是投递报文里的 `url`）——
    报文里的 `url` 是**投递那一刻**才预签出来的短时效链接，两次预签的字节不同
    ⇒ 拿它当身份等于"去重永远不命中"（静默失效，没有任何报错）。
    ⚠️ `event` 是**字符串**（`EVENT_*` 之一）：⛔ 这里不再拿模型的 `Event` 枚举比较。
    """
    payload = row.payload or {}
    if event == EVENT_ORDER_STATUS_CHANGED:
        return str(payload.get("status") or "")
    if event == EVENT_ORDER_IMAGE_ADDED:
        images = payload.get("images") or []
        if images and isinstance(images[0], dict):
            return str(images[0].get("object_key") or "")
        return ""
    return ""


def _is_duplicate(
    latest, *, spec: CredentialSpec, event: str, identity: str
) -> bool:
    """这一件事是不是已经在 outbox 里了（判据**唯一出处**，逐档理由见模块 docstring）。"""
    if latest is None:
        return False
    if event == EVENT_ORDER_DELETED:
        # 无身份值 ⇒ 只看"在飞"（⚠️ 不可放宽成"有行就跳过"：那会吃掉「删 → 恢复 → 再删」）。
        return latest.status == spec.delivery_pending_value
    return _identity_of(latest, event) == identity


def enqueue_deliveries(
    *,
    spec: CredentialSpec,
    apps,
    event: str,
    order,
    occurred_at,
    identity: str,
    extra: dict | None = None,
) -> list:
    """事件入队内核（**两族共用**）：收件人 ⇒ 去重 ⇒ **同事务**写行。回本次创建的行。

    ⚠️ **名字是公开的**（T1b 把原来的 `_enqueue` 改成公开名）：打手侧那一族的四个
    `notify_*` 包装住在**另一个 app**（`apps/takerapi/**`）里，它们要跨 app 调这个内核
    ⇒ 下划线名会让它们去 import 一个"私有"名字。⛔ **没有留旧名的下划线别名**
    （留一份别名就是两份名字，改名就白改了）。

    ## ⭐ 收件人由**调用方**给（`apps`），⛔ 引擎不自己算

    形状与理由见模块 docstring 的「引擎 / 族的切分」第 2 条（为什么是"已算好的一列表"、
    ⛔ 不是"算收件人的可调用对象"）。

    ⚠️ **判据顺序是承重的**：先看应用、空就返回，`order` 的其余列与 `extra` 都不会被碰
    ——这正是"零应用不出行、也不做任何多余工作"这条纪律的落点
    （⚠️ 调用方在这一句之前只做了"算收件人"这一件事，⛔ 没有别的副作用）。

    ⚠️ `extra` 是**逐应用**复制的（不是共享同一个 dict）：`payload` 是 `JSONField`，
    共享可变对象会让"给 A 应用加个键"顺手改到 B 应用的载荷上（那种 bug 只在多应用租户上出现）。

    ⚠️ `event` 只当**字符串**用（`str()` 一次是为了容错：各族包装给的是 `EVENT_*` 这类字面量）。
    """
    if not apps:
        return []
    # ⭐ 函数内 import（JS-03 审查）：`peer_inbound._clip_to_column` 是接收侧
    # 那份「按模型元数据裁列宽」的唯一出处（裁决 R19「有意截断、有意不报错」的口径）——
    # business ↔ openapi 的加载顺序不归这里管，⛔ 别提到模块顶部去赌它不构成环。
    from apps.business.peer_inbound import _clip_to_column

    event = str(event)
    latest = _latest_per_app(apps, spec=spec, event=event, order_no=order.order_no)
    created: list = []
    for app in apps:
        if _is_duplicate(latest.get(app.pk), spec=spec, event=event, identity=identity):
            continue
        payload = {
            "event": event,
            # 去重身份由**投递侧**给（§7.4：at-least-once ⇒ 对方按它判重），
            # ⚠️ 每个应用各一颗：它落在报文里、也落在请求头 `X-TT-Delivery` 上，
            # 而同一个应用的重投必须**沿用**同一颗 ⇒ 它存在 `payload` 里、⛔ 不是每次现生成。
            "delivery_id": uuid.uuid4().hex,
            "order_no": order.order_no,
            "source_order_no": order.source_order_no or "",
            "occurred_at": occurred_at.isoformat(),
            # ⚠️ **深**拷贝：`extra` 里可能有嵌套结构（图片那一条的 `images` 是一张列表），
            # 浅拷贝会让所有应用**共享同一个列表对象** ⇒ 将来有人往某个应用的载荷里 append
            # 一次，别的应用也跟着变（只在多应用租户上出现的 bug）。
            **copy.deepcopy(extra or {}),
        }
        created.append(
            spec.delivery_model.objects.create(
                app=app,
                event=event,
                order_no=order.order_no,
                # ⭐ 台账列宽 64 < `Order.source_order_no` 的 128（JS-03 审查）：
                # 超宽来源单号原样入列 ⇒ `varchar(64)` 溢出、投递行建不出来、连带同事务
                # 业务动作一起 500 ⇒ 入列前按**模型元数据**裁（⛔ 不手抄数字）。
                # ⚠️ **报文里保持全量**（上面的 payload）：接收方拿到的来源单号不因
                # 我方列宽而缺尾，这一格只是台账列。
                source_order_no=_clip_to_column(
                    order.source_order_no or "", spec.delivery_model, "source_order_no"
                ),
                payload=payload,
            )
        )
    return created


def _require_openapi_family(spec: CredentialSpec, *, wrapper: str) -> None:
    """⭐ **响亮守卫**（T2）：这三个 `notify_*` 是**上家侧那一族**的包装，传别族的 `spec` ⇒ 当场抛。

    ## 为什么"必填 `spec`"还不够（T1b 收口后的追加裁定第 2 条）

    必填只挡住**漏传**（`TypeError`），挡不住**传错**：这三个函数内部算收件人用的是**本族那一套**
    （`eligible_apps()` 写死查 `OPENAPI_SPEC.app_model`）⇒ 传打手侧那一份进来是"**半对半错**"：
    **收件人按上家侧查**、而**建行走的是别族那张表**（`spec.delivery_model`）。两个分支都不好，
    而且**都不是**"你传错了 spec"这句话（⚠️ 下面两句是**实测**，⛔ 不是推演）：

    - 这个租户**没有**上家侧应用 ⇒ 收件人是空 ⇒ `enqueue_deliveries` 第一句（"零应用即零成本"）
      直接返回 ⇒ **一行都不写、没有任何异常**（最贵的那一支：静默）；
    - 这个租户**有**上家侧应用 ⇒ 收件人非空 ⇒ 建行时把上家侧应用塞进别族那张表的外键
      ⇒ Django 抛一句**关于模型类型**的话（实测原文：`Cannot query "…": Must be "TakerApp"
      instance.`）—— 响亮，但**指错了地方**（读的人会去查模型定义，而不是查"我传错了 spec"）。

    ⇒ 本守卫把"传错族"换成一句**点名两族**的 `ValueError`（⛔ 不是把那支静默留着）。

    ## 判据与**边界**（⛔ 别把它扩大成"引擎也不许切族"）

    判据是**身份**（`spec is not OPENAPI_SPEC`）⇒ 抛 `ValueError`，消息点名打手侧该用哪一份。
    ⛔ **通用引擎入口**（`enqueue_deliveries` / `claim_due_deliveries` / `deliver_one` /
    `prune_done_deliveries` / `run_round`）**不设这道守卫**：它们本来就该"按 spec 办事"
    （第二族的常驻命令正是显式传 `TAKER_SPEC` 去调它们）—— 在那里收窄会把第二族一起堵死。
    ⛔ 也**不许**用默认参数 / "内部再兜一层"软化本守卫（理由同 T1b 收掉默认值那一条）。

    ⚠️ 判据用例（能红）：`tests/test_openapi_dispatch.py::NotifierFamilyGuardTests`
    —— 传别族 ⇒ `ValueError`；**对照**传 `OPENAPI_SPEC` ⇒ 不抛且真的建了行。
    """
    if spec is not OPENAPI_SPEC:
        raise ValueError(
            f"{wrapper}() 是『上家侧那一族的包装』，⛔ 不是可切族的通用入口："
            "它内部算收件人用的是本族那一套（`eligible_apps()` 查的是上家侧的应用表），"
            "换来族会得到『收件人按上家侧查、建行按那一族的表』这种半对半错的结果"
            "（要么一行都不写、要么抛一句指错地方的类型错）。"
            "打手侧请用 `apps.takerapi` 自己的包装（本线 T3），"
            "⛔ 不要复用上家侧这三个；只有通用引擎入口（`enqueue_deliveries` / "
            "`claim_due_deliveries` / `deliver_one` / `prune_done_deliveries` / `run_round`）"
            "才按传入的 `spec` 切族。"
        )


def notify_order_status_changed(
    order, *, spec: CredentialSpec, status, occurred_at
) -> list:
    """`order.status_changed`：订单状态发生变化（§7.2 第 1 行；覆盖面见模块 docstring）。

    ⚠️ 本函数是**上家侧那一族的包装**（改准块：签名从 T1b 起多了一个**必填**的 `spec`，
    其余与改造前**一字不差**——11 处业务写入点都按它调，那些调用点已各自**显式**写上
    `spec=OPENAPI_SPEC`）：它给收件人 `eligible_apps()`、给**事件名字面量**
    （spec §2.2 第 6 项：字面量写在**每族自己的** `notify_*` 包装里）。
    ⚠️ 打手侧那一族有它**自己**的包装（⛔ 不复用本函数：收件人语义不同）。
    ⚠️⭐ **`spec` 必填 keyword-only**（T1b；理由见模块 docstring 第 4 条的改准块）：
    漏传 = **当场 `TypeError`**，⛔ 不是静默按上家侧办事。⚠️ 本函数是**上家侧那一族**的包装
    （收件人 `eligible_apps()` 写死在本函数里）⇒ 调用方给的是 `spec=OPENAPI_SPEC`。
    ⚠️⭐ **还多一道响亮守卫**（T2）：`spec is not OPENAPI_SPEC` ⇒ 当场 `ValueError`
    （必填只挡漏传，挡不住**传错族**；理由逐条写在 `_require_openapi_family` 的 docstring 里）。

    ⚠️ `status` 是**必填**的（⛔ 不从 `order.status` 取）：有几处写入点是**跨行 `.update()`**，
    调用方手上的实例**是旧的**（拿它取状态会把「改成什么了」写错，而错得很安静）。
    ⚠️ `occurred_at` 也是**必填**的：语料里至少有一处（软删）根本不改 `Order.updated_at`
    ⇒ "时刻默认取 `order.updated_at`"这条会给出一条**假时刻**的推送。

    payload 的 `note` 键是 §7.3 报文形状里就有的一格（样例里它是空串）：本批所有调用点
    都不填它（留空串），⛔ 不删掉这个键——报文形状是对外契约，第三方按它解析。
    """
    _require_openapi_family(spec, wrapper="notify_order_status_changed")
    return enqueue_deliveries(
        spec=spec,
        apps=eligible_apps(),
        event=EVENT_ORDER_STATUS_CHANGED,
        order=order,
        occurred_at=occurred_at,
        identity=str(status),
        extra={"status": str(status), "note": ""},
    )


def notify_order_deleted(order, *, spec: CredentialSpec, occurred_at) -> list:
    """`order.deleted`：订单被删除（**软删**，§7.2 第 2 行）。

    ⚠️ 上家侧那一族的包装（同上面那条：收件人 + 事件名字面量由包装给）。
    ⚠️ `spec` 必填 keyword-only（T1b，同上面那条：漏传 = `TypeError`）。
    ⚠️⭐ 同上面那条：`spec is not OPENAPI_SPEC` ⇒ 当场 `ValueError`（T2 的族守卫）。

    ⚠️ `occurred_at` 必填：软删的 `update_fields` 只有 `deleted_at` / `deleted_by`
    ⇒ `Order.updated_at` 那一列**两边都不会被刷新**（Django 的 `Model._save_table` 只对
    `update_fields` 里列出的字段调 `f.pre_save()`，`auto_now` 挂的就是它）⇒
    从 `order.updated_at` 取时刻会得到一个**更早**的值（§7.2.1 点名的坑）。
    ⚠️ 报文字段从**现场握着的那条实例**取（`order_no` / `source_order_no`）——
    软删之后默认 manager 取不到这行，但删除现场本来就有实例（§7.2.1 的取证结论）。
    """
    _require_openapi_family(spec, wrapper="notify_order_deleted")
    return enqueue_deliveries(
        spec=spec,
        apps=eligible_apps(),
        event=EVENT_ORDER_DELETED,
        order=order,
        occurred_at=occurred_at,
        identity="",
    )


def notify_order_image_added(
    order, *, spec: CredentialSpec, object_key, img_type=0, remark="", occurred_at
) -> list:
    """`order.image_added`：订单新增进度图（变成 `ready`，§7.2 第 3 行）。

    ⚠️ 上家侧那一族的包装（同上面两条）。
    ⚠️ `spec` 必填 keyword-only（T1b，同上面两条：漏传 = `TypeError`）。
    ⚠️⭐ 同上面两条：`spec is not OPENAPI_SPEC` ⇒ 当场 `ValueError`（T2 的族守卫）。

    ## ⚠️ 库里存的是**稳定身份**，报文里给的是**预签名 URL**（两者**有意不同**）

    outbox 里的 `images[0]` = `{"object_key": …, "img_type": …, "remark": …}`；
    投递那一刻（`_wire_payload`）把 `object_key` 换成**当时**预签出来的 `url`。

    理由两条，⛔ 都不是风格问题：

    1. **预签名 URL 有时效**（`get_storage().presign_get()`：生产 COS 为整点对齐的 1–2 小时，
       本机 300 秒）⇒ 把它存进 outbox，一次 `6h` 之后的重投就会带着
       **过期链接**发出去（对方按链接下载一律 403，而台账上显示"投递成功"）；
    2. **对象存储是外部依赖**：在**业务事务里**预签等于把"存储抖一下"变成"录单 / 改状态失败"
       ——那是拿一个推送载荷的细节去拖垮业务写入。放在投递那一刻，失败只影响**这一次投递**
       （照退避表重试，业务写入早就提交好了）。

    ⚠️ `occurred_at` 必填（同上面两个入口：本文件不替调用方猜时刻）。
    """
    _require_openapi_family(spec, wrapper="notify_order_image_added")
    return enqueue_deliveries(
        spec=spec,
        apps=eligible_apps(),
        event=EVENT_ORDER_IMAGE_ADDED,
        order=order,
        occurred_at=occurred_at,
        identity=str(object_key),
        extra={
            "images": [
                {"object_key": str(object_key), "img_type": img_type, "remark": remark}
            ]
        },
    )


def claim_due_deliveries(*, now, spec: CredentialSpec) -> list[int]:
    """取一批到点的待投递行并打租约，回 pk 列表（§7.4；形状照 `peer_worker.claim_due_events`）。

    ⛔ **事务里不发 HTTP**：调用方（`run_round`）拿到 pk 之后、事务**外**才逐条投递。
    ⚠️ `skip_locked=True` 是承重的（同 `peer_worker` 的理由）：多台应用服务器共跑时，
    被别的进程锁住的行**跳过**而不是排队。
    ⚠️ **顺序**：先按 `(created_at, id)` 升序取，再丢掉「同一 `order_no` 还有更早的 `pending` 行」
    的那些（见模块 docstring 的「顺序」一节）——被丢掉的那些这一轮不取、也不打租约
    （打了租约等于把它们推迟 5 分钟，而它们本来就在等更早那一条）。
    ⚠️⭐ `spec` 是**必填 keyword-only**（T1b 改准；⛔ 原来那个"默认回上家侧"的值已收掉）：
    漏传 = **当场 `TypeError`**。理由见模块 docstring 第 4 条的改准块 —— 那正是
    「打手侧的常驻命令忘了传 `spec=TAKER_SPEC` ⇒ 静默按上家侧的台账办事」那个口子。
    """
    with transaction.atomic():
        rows = list(
            spec.delivery_model.objects.select_for_update(skip_locked=True)
            .filter(status=spec.delivery_pending_value)
            .filter(
                models.Q(next_attempt_at__isnull=True)
                | models.Q(next_attempt_at__lte=now)
            )
            .order_by("created_at", "id")[:BATCH_SIZE]
        )
        if rows:
            rows = _drop_out_of_order_rows(rows, spec=spec)
        for row in rows:
            row.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
        if rows:
            spec.delivery_model.objects.bulk_update(rows, ["next_attempt_at"])
    return [row.pk for row in rows]


def _drop_out_of_order_rows(rows, *, spec: CredentialSpec) -> list:
    """丢掉「同一 `order_no` 还有更早的 `pending` 行」的那些（同一订单按产生顺序投，§7.4）。

    ⚠️ 判据取的是**全表里那个单号最早的 `pending` 行**（不只是本批）：更早那条可能正在
    别人的租约里（这一轮不在本批），那种情况下本行的正确行为是**继续等**。
    ⚠️ 表与 `pending` 值都取自 `spec`（⛔ 本函数里没有具体模型 / 状态枚举）。
    """
    order_nos = {row.order_no for row in rows}
    first_pk = dict(
        spec.delivery_model.objects.filter(
            status=spec.delivery_pending_value, order_no__in=order_nos
        )
        .values_list("order_no")
        .annotate(first=models.Min("id"))
    )
    return [row for row in rows if first_pk.get(row.order_no) == row.pk]


def _app_is_deliverable(app, *, spec: CredentialSpec) -> str:
    """这个应用现在还能不能投：能 ⇒ 空串，不能 ⇒ 一句人话原因（`reason` 列直接用它）。

    ⚠️ 三条判据与**三句人话**全部由 `spec` 给（`spec.callback_disabled_reasons` 的顺序就是
    "应用停用 / 回调停用 / 地址为空"）⇒ ⛔ 本函数里一个文案字面量都没有（第二族的文案不同，
    写在这里就等于把上家侧的话发到打手侧的台账上）。
    ⚠️ 两个 `callback_*` 列名也由 spec 给（`getattr`）：列名两族今天相同、但它们是**数据**
    （先例：`secret_field` / `idem_path_field` 那几格）。
    """
    app_off, callback_off, url_empty = spec.callback_disabled_reasons
    if not app_is_enabled(app, spec):
        return app_off
    if getattr(app, spec.callback_state_field) != spec.callback_enabled_value:
        return callback_off
    if not (getattr(app, spec.callback_url_field) or "").strip():
        return url_empty
    return ""


def _wire_payload(row) -> dict:
    """把库里的 `payload` 装成**要发的** payload（今天只动一处：图片的预签名 URL）。

    ⚠️ 返回**新的** dict（不原地改 `row.payload`）：库里那份是**稳定身份**
    （`object_key`），⛔ 不许被一次投递写脏（那会让去重判据在重投之后失效）。
    ⚠️ 注解里不写具体模型（`row` 是哪一族的台账行由 `spec.delivery_model` 定）——
    见模块 docstring 的「引擎 / 族的切分」第 1 条。
    """
    payload = dict(row.payload or {})
    images = payload.get("images")
    if not images:
        return payload
    storage = get_storage()
    wired = []
    for entry in images:
        if not isinstance(entry, dict):
            continue
        wired.append(
            {
                # ⚠️ 预签**在投递这一刻**做（理由见 `notify_order_image_added`）。
                "url": storage.presign_get(str(entry.get("object_key") or "")).url,
                "img_type": entry.get("img_type", 0),
                "remark": entry.get("remark", ""),
            }
        )
    payload["images"] = wired
    return payload


def _mark_terminal(row, *, now, status, reason="") -> None:
    """把一行落成**终态**（`done` / `failed`）：写状态 + 原因 + 处理时刻，并**摘掉租约**。

    ⚠️ 终态要摘掉租约（`next_attempt_at` 的语义是"投递中被占用"）：留在那里会让它在
    5 分钟后被重新认领一次，而终态行压根不该再被取活。
    """
    row.status = status
    row.reason = _clip_reason(reason)
    row.processed_at = now
    row.next_attempt_at = None
    # ⚠️ `response_status` **必须在** `update_fields` 里：成功那一条也要把回执码写下来
    # （它是"对方到底回了什么"的唯一证据）；漏了它，成功行的这一列永远是 `NULL`，
    # 而台账上看起来一切正常。
    row.save(
        update_fields=[
            "status", "attempts", "response_status", "reason", "processed_at",
            "next_attempt_at",
        ]
    )


def _attempt(row_pk: int, *, spec: CredentialSpec, now) -> str:
    """投递一行，回 `"noop" | "delivered" | "retried" | "failed" | "dropped"`。

    ⚠️ `attempts` 在**真正发出去之前**加一（与 `peer_worker` 同口径）：它是**已投递次数**，
    退避取值 `BACKOFF_SECONDS[attempts - 1]` 靠它。
    ⚠️ 表 / 状态值 / 回调地址列 / 密钥列 / 三句"不投递"人话**全部**经 `spec` 现取
    （⛔ 本函数里没有一个具体模型名或文案字面量）。
    """
    row = (
        spec.delivery_model.objects.select_related("app")
        .filter(pk=row_pk)
        .first()
    )
    if row is None or row.status != spec.delivery_pending_value:
        return "noop"
    blocked = _app_is_deliverable(row.app, spec=spec)
    if blocked:
        # 行已经排上了、应用随后不可用 ⇒ 终态 + 人话（理由与代价见模块 docstring）。
        _mark_terminal(row, now=now, status=spec.delivery_failed_value, reason=blocked)
        return "dropped"
    row.attempts += 1
    result = outbound.deliver(
        getattr(row.app, spec.callback_url_field),
        payload=_wire_payload(row),
        secret=getattr(row.app, spec.secret_field),
    )
    if result.ok:
        row.response_status = result.status_code
        _mark_terminal(row, now=now, status=spec.delivery_done_value)
        return "delivered"
    row.response_status = result.status_code
    # ⚠️ 终态判据是 `>`（⛔ 不是 `>=`）：第 1–6 次失败各取一档退避（含 `6h`），
    # **第 7 次**失败才终态（见 `BACKOFF_SECONDS` 的注释与模块 docstring）。
    if row.attempts > len(BACKOFF_SECONDS):
        _mark_terminal(
            row, now=now, status=spec.delivery_failed_value, reason=result.reason
        )
        return "failed"
    row.status = spec.delivery_pending_value
    row.reason = _clip_reason(result.reason)
    row.next_attempt_at = now + timedelta(seconds=BACKOFF_SECONDS[row.attempts - 1])
    row.save(
        update_fields=["status", "attempts", "reason", "next_attempt_at", "response_status"]
    )
    return "retried"


def deliver_one(row_pk: int, *, now=None, spec: CredentialSpec) -> str:
    """投递一行（单条包装；给测试 / 排障用，`run_round` 走的是逐行循环）。

    ⚠️⭐ `spec` 是**必填 keyword-only**（T1b 改准；⛔ 原来那个"默认回上家侧"的值已收掉）：
    漏传 = 当场 `TypeError`（理由见模块 docstring 第 4 条的改准块）。
    """
    stamp = timezone.now() if now is None else now
    return _attempt(row_pk, spec=spec, now=stamp)


def prune_done_deliveries(*, now=None, spec: CredentialSpec) -> int:
    """机会式清理：删掉**已投递**且 `created_at` 早于「现在 − `RETENTION_DAYS` 天」的行，最多 500 行。

    回真正删掉的行数（给用例当读数用；⛔ 生产调用方不读它）。

    ## 判据（三条，都能被用例独立证伪）

    1. **时间判据只有一个**：`created_at < 明确算出的时刻`（`now - RETENTION_DAYS 天`）；
    2. **只删 `done` 行**：`pending`（还没发）/ `failed`（终态失败）都**保留**
       ——前者是待办、后者是页面上的告警面，删掉它们等于把"要发的东西"和"发不出去的东西"
       一起静默丢掉；
    3. **一次最多 `MAX_PRUNE_ROWS` 行**：先按 `created_at` 升序取最多 500 个主键，再按主键删
       （⚠️ `QuerySet.delete()` **不接受**切片，Django 会抛
       `TypeError: Cannot use 'limit' or 'offset' with delete.` ⇒ 分两步，照
       `idempotency._prune` 的既有实现）。
    ⚠️ 表与 `done` 值取自 `spec`；`spec` 是**必填 keyword-only**（T1b 改准，同 `deliver_one`：
    漏传 = 当场 `TypeError`，⛔ 不再默认回上家侧那一份）。
    """
    cutoff = (timezone.now() if now is None else now) - timedelta(days=RETENTION_DAYS)
    stale_ids = list(
        spec.delivery_model.objects.filter(
            status=spec.delivery_done_value, created_at__lt=cutoff
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:MAX_PRUNE_ROWS]
    )
    if not stale_ids:
        return 0
    deleted, _detail = spec.delivery_model.objects.filter(pk__in=stale_ids).delete()
    return deleted


# ═════════════════════════════════════════════════════════════════════════════
# 手工重投：**闸门 + 重置字段**的唯一实现（两族共用；T5b 收口）
# ═════════════════════════════════════════════════════════════════════════════


class DeliveryRetryRefused(Exception):
    """手工重投被拒：那一行**不是**投递失败（`spec.delivery_failed_value`）。

    ⚠️ **为什么是领域异常、⛔ 不是 DRF 的 `ValidationError`**：本模块是**引擎**
    （两族的视图分属两个 app），把框架的异常搬进来等于让引擎认识 HTTP 层；而"回哪个业务码、
    挂在哪个字段下"是**各视图自己的事**（今天两族的视图都翻成 `1000` + 字段 `delivery`）。
    ⚠️ 消息**由 `spec` 给**（`spec.delivery_retry_only_failed_msg`）：两族各自一句，
    ⛔ 视图里不许再写一份字面量（同一条闸两个文案 = 必然漂）。
    """


def prepare_retry(delivery, *, spec: CredentialSpec) -> None:
    """手工重投的**闸门 + 逐字段重置**（两族共用，⛔ 不许在视图里再抄一份）。

    ## 判据（只有这一条，两族逐字同一套）

    `delivery.status != spec.delivery_failed_value` ⇒ 抛 `DeliveryRetryRefused`
    （消息取 `spec.delivery_retry_only_failed_msg`）、**一个字段都不写**：

    - `done` ⇒ 拒绝：已投递成功的行再排一次就是**重复推送**——对方会**收到两次**同一个事件
      （投递是"至少一次"、对方按投递号去重，但那是我方对协议的**期望**，⛔ 不是我方可以
      主动制造重复的理由）。这条闸去掉之后**没有任何别的机制**拦得住它。
    - `pending` ⇒ 拒绝：它已经排在队列里（可能正在别人的租约里），再排一次只会把 `attempts`
      清零、把退避表重置 ⇒ 看起来"重投成功"，实际只是让它在原地多转一圈。

    否则按**收口前那一版逐字段相同**的方式重置成"立刻可投"：
    `status = spec.delivery_pending_value` / `attempts = 0` / `next_attempt_at = None`，
    且 `update_fields` **只有这三列** ⇒ `reason` / `response_status` / `processed_at`
    **刻意保留**（它们是"上一次失败"的对照信息：重投之后页面要能同时看到"我刚重投了"与
    "上一次为什么失败"；清掉就只能等下一次失败才有话可读）。⚠️ `update_fields` 就是这条
    纪律的机器可读形态。

    ## ⚠️ 调用方负责事务（本函数自己不开）

    与 `enqueue_deliveries` 同一条纪律：视图要把这次重置与它自己那条留痕放进**同一个**
    `transaction.atomic()`（本仓口径：留痕与写入同事务），所以事务边界留在视图那一层。

    ## 为什么这一段终于收到这里（T5b，红队 Important）

    收口前两族的视图各自写了一份"只有 `failed` 能重投"的判据与**逐字相同**的一句文案
    （`apps/openapi/api/app_views.py` 与 `apps/takerapi/api/app_views.py`）⇒ 同一件事两份实现，
    谁被单独放松都不会有症状（另一族照样绿）。今天它只有**这一份**，两族视图都调它，
    跨族对拍用例：`tests/test_takerapi_callback_api.py::RetryGateParityTests`（同样的三态在
    两族端点上给出同一套语义；某一族被单独放松 ⇒ 它红）。

    ⛔ 引擎里不出现任何具体模型名（模块 docstring 的「引擎 / 族的切分」第 1 条）：本函数只碰
    `delivery` 实例上那三列，列名（`status` / `attempts` / `next_attempt_at`）是**字面量**
    ——两族的台账模型逐列同名（spec §3.2），与 `eligible_apps()` 里那个 `state` 字面量同一档。
    """
    if delivery.status != spec.delivery_failed_value:
        raise DeliveryRetryRefused(spec.delivery_retry_only_failed_msg)
    delivery.status = spec.delivery_pending_value
    delivery.attempts = 0
    delivery.next_attempt_at = None
    delivery.save(update_fields=["status", "attempts", "next_attempt_at"])


def run_round(*, now=None, spec: CredentialSpec) -> dict:
    """跑一轮：取活 ⇒ 逐行投递 ⇒ 汇总 ⇒ 机会式清理。回各档计数。
    ⚠️ **逐行异常边界**：一行抛未预期异常（例如对象存储预签失败）⇒ 其余照投。
    进**汇总 `errors`** 的只有**异常类型名**（`str(exc)` 可能带对方回包 / 凭据片段，
    照 `peer_worker.run_round` 的既有口径）；**完整栈**在 `logger.exception` 里（排障要它）。
    ⚠️ 清理排在**投递之后**：它是机会式的（挂在常驻进程的一轮上），
    ⛔ 不该抢在投递前面占锁。
    ⚠️⭐ `spec` 是**必填 keyword-only**（T1b 改准；⛔ 原来那个"默认回上家侧"的值已收掉）：
    常驻命令 `openapi_dispatch` 显式传 `spec=OPENAPI_SPEC`，第二族的常驻命令**必须显式**传
    `spec=TAKER_SPEC`；漏传 = 当场 `TypeError`（理由见模块 docstring 第 4 条的改准块）。
    """
    # ⚠️ 判据用 `is not None`（E-0021 同口径）：不拿真值判这个形参。
    stamp = timezone.now() if now is None else now
    summary = {
        "delivered": 0, "retried": 0, "failed": 0, "dropped": 0, "pruned": 0, "errors": [],
    }
    for row_pk in claim_due_deliveries(now=stamp, spec=spec):
        try:
            outcome = _attempt(row_pk, spec=spec, now=stamp)
        except Exception as exc:  # noqa: BLE001 —— 逐行异常边界（未被预期的那一类）
            logger.exception("开放平台推送 %s 出现未预期失败", row_pk)
            summary["errors"].append(f"推送 {row_pk}: {type(exc).__name__}")
            continue
        if outcome in summary:
            summary[outcome] += 1
    summary["pruned"] = prune_done_deliveries(now=stamp, spec=spec)
    return summary
