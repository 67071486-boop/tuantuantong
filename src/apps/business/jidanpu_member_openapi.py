"""**下家**鸡蛋铺②式（官方 Open Platform API）那条路：回调消费 + 兜底对账 + 健康那句人话（规格 = `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §5/§8/§9.4＋补记 §19–§28）。⭐ **回调只当提醒、不当真相**：对方变「已完成」⇒ 立刻机器接手 / 写「待验收」（D-0226；唯一写出口 = `order_status.apply_transition`），其余不写状态；水位用我们自己的 `received_at`（报文 `timestamp` 实测偏 +8h）。
⛔ 本模块**一行「该不该写状态」的判据都没新造**：处理集合 / 逐字「已完成」/ 写前二道闸全在 `jidanpu_member.pullable_links` 与 `apply_remote_completion`（下家线唯一那两处，慢路 / 回调 / 兜底对账三路共用），本模块只把权威值查回来、判认不认识。原模块头的台账档位表、「对方说没有这张单」同族逐档判法、自愈四条硬口径、健康三问算式与「不做什么」边界清单见 `docs/code-comment-notes.md` 本文件分节；⛔ 不打写接口、不碰模型 / 序列化器 / 前端 / 接收器 / 上家线。
"""
from __future__ import annotations

import os
from contextlib import ExitStack

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone as django_timezone

from apps.audit.models import OperationLog
from apps.business import (
    jidanpu_assignment_lock,
    jidanpu_common,
    jidanpu_member,
    jidanpu_openapi,
    jidanpu_openapi_consume,
    jidanpu_openapi_health,
    jidanpu_openapi_reconcile,
    jidanpu_relaunch,
)
from apps.business.models import (
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
)
from apps.rbac.permissions import nodes_of

# ══ 一、台账 kind（⛔ 两条线**必须**取不同的名字：`kind` 是「这条是谁写的」的判据）══════
#: 订单维度的结论**复用下家线既有的** `member.status_in`（②式回调与兜底对账做的就是「从下家把状态拉回来」），
#: ⛔ 不新造 kind（那会让页面多一个筛不到、且语义重复的取值）。
KIND_STATUS_IN = jidanpu_member.KIND_STATUS_IN
#: 没有通过验签（⛔ 一个字节都不改订单）。
KIND_SIGN_REJECTED = "member.openapi_unsigned"
#: 报文读不出来（没落原文 / 不是 JSON / 缺 `orderNo`）。
KIND_UNREADABLE = "member.openapi_unreadable"
#: 回调码没有**启用中的②式绑定**认领（两条线一起看：码是接收器那一侧的共享命名空间）。
KIND_UNCLAIMED_CODE = "member.openapi_unclaimed"
#: 本绑定名下没有这条链路 ⇒ 找不到我方订单。
KIND_UNKNOWN_ORDER = "member.openapi_unknown_order"
#: 不是 `action=10`（第一期只消费状态）。
KIND_OTHER_ACTION = "member.openapi_other_action"
#: 查权威状态失败（⛔ 一个字都不写 + 水位不前移）。
KIND_QUERY_FAILED = "member.openapi_query_failed"
#: 权威状态**认不出来**（⛔ 不猜、⛔ 不映射成问题单、⛔ 不静默丢）。
KIND_UNKNOWN_STATUS = "member.openapi_unknown_status"
#: 事件文件里**不是本线路径**的行（旧路径 / 形状不对）。
KIND_FOREIGN_PATH = "member.openapi_foreign_path"
#: 本绑定的一条事件**没排在水位之后**（乱序：接收器的钟被回拨 / 将来的双写进程）⇒ 看得见的跳过。
KIND_STALE_SKIPPED = "member.openapi_stale_skipped"
#: 兜底对账撞上一轮上限（**只推迟、不丢**）。
KIND_RECONCILE_CAP = "member.openapi_reconcile_cap"
#: 兜底对账**跳过一张坏单**（停滞修复，G2）：「这一张单」的形状坏（`data.status`
#: 不是非空字符串 / `data` 不是对象）⇒ 本轮不再为它停住，只留这条痕（带单号）后继续下一张。
KIND_RECONCILE_STUCK = "member.openapi_reconcile_stuck"
#: 老地址那条例外（见「二·五」）：老回调地址送来的单号在**本租户**里找不到任何链路 ⇒ 只留痕、
#: ⛔ 不进错误列（老地址收的是整台鸡蛋铺系统的回调，「不是我们的单」是常态），刻意与 `KIND_UNKNOWN_ORDER` 分开。
KIND_LEGACY_UNKNOWN_ORDER = "member.openapi_legacy_unknown"
#: **自愈补上了**（见模块「自愈」节）：按 `orderNo` 找不到链路、用 `sourceOrderNo` 反查到我方那张单且那一格**原是空的**
#: ⇒ 把 `orderNo` 补了进去（`status="done"`：办成了的事，不是异常）。
KIND_HEALED = "member.openapi_healed"
#: ⭐ **自愈补不上**（两档：那一格已有别的号 / 同来源单号查到多张我方单）
#: ⇒ ⛔ 不改任何链路（⛔ 绝不覆盖已有的号、⛔ 不猜是哪一张）+ 一条要人看的痕。
KIND_HEAL_BLOCKED = "member.openapi_heal_blocked"

# ══ 二、人话（⛔ 不含凭据；`reason` 列宽 255）══════════════════════════════════
#: ⭐ **这个回调码下的一整组绑定**里都没有这条链路（改准：原句是「本绑定名下」——
#: 新口径下同一个码下有多个打手，说「本绑定」就成了一句话假话：明明只查了自己那一行）。
REASON_UNKNOWN_ORDER = (
    "回调说下家单号 {order_no}（我方单号 {source_order_no}），但这个回调码下的一整组绑定里"
    "都没有这条链路（同一个上家域名的多个打手共用这一个回调码）⇒ ⛔ 不建单、不改任何订单，只留痕"
)
#: 这条单属于**同一个回调码下的另一条绑定**、而那一行**不消费回调**（①式 / 已停用）、且**不是同一家店**
#: （跨店不代写，见 `_sibling_lands_here`）⇒ 本条这一轮没有人办它（留 `skipped` 订单台账、⛔ 不进错误列）。
#: ⚠️ 同一家店的那一档不走这里（B 档：由消费方照常应用，应用身份 = owner）。
REASON_GROUP_NOT_CONSUMING = (
    "回调说下家单号 {order_no}：它属于**同一个回调码下**的另一条绑定（#{owner_pk}），"
    "而那一行{why} ⇒ 本轮没有人在消费这条回调"
    "（启用那一行 / 切回②式之后由它自己那一轮办）"
)
#: 链路在，但我方订单**不在处理集合里**（判据①的逐单形态）。
REASON_NOT_PULLABLE = (
    "下家单号 {order_no} 对应的链路{why} ⇒ 本轮**不查权威状态**、不改订单"
    "（它按定义不进处理集合，见 `PULL_TERMINAL_STATUSES`）"
)
#: 判据①被筛掉的两个理由（逐字进人话）。
NOT_PULLABLE_SOFT_DELETED = "的我方订单已软删（进了回收站）"
NOT_PULLABLE_TERMINAL = "的我方订单已到终态（已完成 / 已退单）"
NOT_PULLABLE_WITHDRAWN = "的下家副本已撤回"
NOT_PULLABLE_RETIRED = "的下家副本已随换绑失效"
REASON_AMBIGUOUS_REMOTE_ID = "鸡蛋铺单号与来源单号无法确认同一个副本，本次跳过"
#: 查权威失败：回调那一条路（水位不前移）。
REASON_QUERY_FAILED = (
    "查权威状态失败（{detail}）⇒ 这一条回调**一个字都不写**，且水位不前移（下一轮重试）"
)
#: 查权威失败：兜底对账那一条路（⛔ 不说「水位」：对账那条路没有水位，停的是**窗口**）。
REASON_QUERY_FAILED_RECONCILE = (
    "查权威状态失败（{detail}）⇒ 这一单**一个字都不写**，本轮的对账窗口就此停住"
    "（下一轮从同一个窗口重试；⛔ 不静默跳过、⛔ 不拿旧值顶上）"
)
# ── 「对方明确说**没有这张单**」⇒ 终态收口───────────────
#: 明确的无单信封按终态处理，避免钉住后续回调的水位。
#: 两句按**触发方式**分（对账那条路没有水位）；都**不进 `findings`**（⛔ 不盖 `last_error`：有结论的终态，留痕出口是台账那一行）。
REASON_REMOTE_ORDER_GONE = (
    "下家单号 {order_no}：对方明确说**没有这张单**（明确无单回执）⇒ 终态收口：这一条回调**一个字都不写**、"
    "**不再重试**，水位照常前移（⛔ 不再把它后面的回调一起钉住）"
)
#: 同上一档，兜底对账那一条路的人话（⛔ 不说「水位」：那条路停的是**窗口**）。
REASON_REMOTE_ORDER_GONE_RECONCILE = (
    "下家单号 {order_no}：对方明确说**没有这张单**（明确无单回执）⇒ 终态收口：这一单**一个字都不写**、"
    "**不再重试**，本轮的对账窗口照常轮转（⛔ 不再把它后面的在途单一起钉住）"
)
#: 认不出来的权威值。
REASON_UNKNOWN_STATUS = (
    "下家官方API 返回了一个本线认不出来的状态「{token}」⇒ ⛔ 一个字节都不写订单，"
    "登记一条待人工核对（⛔ 不猜、⛔ 不映射成问题单、⛔ 不静默丢）"
)
#: 权威值**缺失**（空）：照上家线口径「判不了就什么都不写」，⛔ 不当成「认不出来的值」（没有值与有个怪值是两件事）。
#: ⚠️ 这一档**到得了**（⛔ 不是死代码）：客户端判据是「非空字符串」⇒ 纯空白的 `status` 过得去、被 `strip()` 成空走这条；
#: 「`data` 里根本没有 `status`」是客户端**抛异常**那一档（查不动，水位不前移）。
REASON_STATUS_MISSING = "下家官方API 这一次没有给出可读的状态（status 为空）⇒ 这一轮什么都不写"

# ── 老地址那条例外（业务方点名的「一个例外」；边界见「二·五」）──────
#: 环境变量名：只有这个变量点名的那**一个**租户（schema 名）才接管老回调地址的行。用「租户名」而非布尔开关：
#: 「老地址 → 哪个租户」是**机器级**事实 ⇒ 写成点名的值，别的机器照抄这一行也不会生效，比 `=1` 开关安全。
ENV_LEGACY_CALLBACK_TENANT = "JIDANPU_LEGACY_CALLBACK_TENANT"

#: 老地址送来的单号在**本租户**里找不到任何链路（⛔ **不进错误列**：那是「不是我们的单」，不是故障）。
REASON_LEGACY_UNKNOWN_ORDER = (
    "老回调地址送来的下家单号 {order_no}（我方来源单号 {source_order_no}）在**本租户**里"
    "找不到任何链路 ⇒ ⛔ 不建单、不改任何订单；老地址收的是**整台鸡蛋铺系统**的回调，"
    "「不是我们的单」是常态 ⇒ 只留痕、⛔ 不当成故障（不进错误列）"
)
#: 老地址那条路上「这条单属于本租户的另一行、而那一行不消费回调」的人话
#: （⛔ 不说「同一个回调码下」：老地址**根本没有码**）。
REASON_LEGACY_GROUP_NOT_CONSUMING = (
    "老回调地址送来的下家单号 {order_no} 属于**本租户**的另一条绑定（#{owner_pk}），"
    "而那一行{why} ⇒ 本轮没有人在消费这条回调"
    "（启用那一行 / 切回②式之后由它自己那一轮办）"
)

# ── ⭐ 自愈（缺系统单号那一档）的三句人话（见模块「自愈」那一节）──────────────────────
#: **补上了**那一档（`status="done"`）：说清「为什么原来没有号」（录单时对方已经有这张单 ⇒
#: 官方 API 不给号）与「补的是什么」，让人一眼看懂这一行是**好事**、⛔ 不是故障。
REASON_HEALED = (
    "这条回调说下家单号 {order_no}（我方来源单号 {source_order_no}）⇒ 按它找不到链路，"
    "但用来源单号对上了我方订单 {our_order_no}，而那条链路的系统单号那一格**原来是空的**"
    "（录单时对方已经有这张单、官方 API 不给号）⇒ 已把 {order_no} 补上，"
    "这一条回调**照常继续处理**（⛔ 只补空、⛔ 不覆盖已有的号）"
)
#: **冲突**那一档：链路那一格**已有号**且不等于这条回调给的号 ⇒ ⛔ 绝不覆盖。措辞刻意写「不等于回调给的号」：
#: 那一格的值只是首尾多几个空白时按既有**全等**口径同样对不上（`_link_of` 用 `filter(jidanpu_order_id=order_no)`），
#: 说成「另一个号」会是一句假话。
REASON_HEAL_CONFLICT = (
    "这条回调说下家单号 {order_no}（我方来源单号 {source_order_no}）⇒ 按来源单号找到我方订单 "
    "{our_order_no}，但它在本组里的链路那一格**已经有号**（「{existing}」）、不等于这条回调给的号 "
    "⇒ ⛔ 绝不覆盖（自愈只补空），也⛔ 不拿 {order_no} 的权威状态去写这张单，请人工核对"
)
#: **多张**那一档：同一个有效来源单号对上了**多张**我方单 ⇒ 认不出该补哪一张（⛔ 不猜）。
#: ⚠️ 这件事与日志快路那一档（`jidanpu_member.DUPLICATE_SOURCE_ORDER_NO_REASON`）是**同一类**事实
#: —— 那边按链路 id 认最早一条，这边**连补都不补**（补错一格就是把两张单的链路接错）。
REASON_HEAL_DUPLICATE = (
    "这条回调说下家单号 {order_no}（我方来源单号 {source_order_no}）⇒ 按它找不到链路，"
    "而按来源单号对上了我方 {count} 张单（有效来源单号都是它）⇒ 认不出该补哪一张，"
    "⛔ 一格都不补、⛔ 不改任何订单；请人工核对来源单号（与日志快路那一档同一类事实）"
)

#: 两档的**同一条纪律**（照既有那两档：新路径要人看 ⇒ 进 `findings`；老地址是常态 ⇒ 不进）：
#: `(status, 进不进 findings)`。
_HEAL_BLOCKED_NEW = ("failed", True)
_HEAL_BLOCKED_LEGACY = ("skipped", False)

# ── `action=30`（订单有新截图）：报文里带着 `imgUrl` ⇒ 直接入库 ────────────────
#: 实测形状（真机首日那 39 条）：`{action:"30", orderNo, sourceOrderNo, note, imgUrl, name, timestamp}`
#: —— `imgUrl` / `name` **不在**文档字段表里，但**参与了签名**（`apps/callback/signature.py` 的注释记着那件事）。
ACTION_NEW_SCREENSHOT = "30"
FIELD_IMG_URL = "imgUrl"
FIELD_NOTE = "note"
#: 报文里那三个字段名（⭐ **唯一出处是引擎那一份**：`jidanpu_openapi_consume` 的 `FIELD_*`
#: 常量组 ⇒ ⛔ 不在这里另抄一份字面量）。
FIELD_ACTION = jidanpu_openapi_consume.FIELD_ACTION
FIELD_ORDER_NO = jidanpu_openapi_consume.FIELD_ORDER_NO
FIELD_SOURCE_ORDER_NO = jidanpu_openapi_consume.FIELD_SOURCE_ORDER_NO
#: A 段（回调带 `imgUrl`）落库失败的人话（⛔ 不含 URL / `accessid` / 对象名 —— 隐私口径）。
REASON_PIC_IN_FAILED = "回调带了一张新截图，但没存进来（{reason}）⇒ 只留痕、⛔ 不改任何订单"
#: A 段「报文里没带可用的 `imgUrl`」那一档（action=30 却没有直链）⇒ 留痕、⛔ 不静默。
REASON_PIC_IN_NO_URL = (
    "这条 action=30（订单有新截图）的回调里**没有可用的 imgUrl**（{detail}）⇒ ⛔ 什么都不做，只留痕"
)

#: 三条人话按**触发方式**取（⛔ 别在调用点手拼：同一个判断出现在两处必然漂）。
_QUERY_FAILED_REASONS: dict[str, str] = {
    jidanpu_member.TRIGGER_CALLBACK: REASON_QUERY_FAILED,
    jidanpu_member.TRIGGER_RECONCILE: REASON_QUERY_FAILED_RECONCILE,
}
#: ⭐ 「对方明确说没有这张单」⇒ 终态收口那两句人话（同样按触发方式分，见上面那两条的注释）。
_GONE_REASONS: dict[str, str] = {
    jidanpu_member.TRIGGER_CALLBACK: REASON_REMOTE_ORDER_GONE,
    jidanpu_member.TRIGGER_RECONCILE: REASON_REMOTE_ORDER_GONE_RECONCILE,
}

# ── 中间档自动接手（D-0226，spec §2.2 / §2.4）────────────────────────────────
#: 远端「有人在做」的那几档 = 映射那一格 ∈ {进行中, 待验收}。判据**复用映射表**（`map_remote_status` 唯一出处），⛔ 不另写一张字面量表；「未分配 / 已暂停 / 问题单 / 已退单 / 撤销中 / 仲裁中…」不在集合里 ⇒ 保持今日行为（退单 / 撤下是 outbox 那条线的职责，spec §4）。
CLAIM_MID_TARGETS = frozenset({OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW})
#: 远端「已完成」的**字面值**——⭐ 与 `apply_remote_completion` 第一行的判据**同一个值**：
#: 「先接手、再完成」那两步必须挂在同一条判据上（各写一份必然漂 ⇒ 直接引用那一处常量，
#: 本行不再是字面量）。
REMOTE_DONE_LABEL = jidanpu_member.REMOTE_DONE_STATUS


def wants_machine_claim(*, mapped, token, order_status) -> bool:
    """这一档远端状态 + 我方状态 ⇒ 会不会触发机器接手（判据只有这一处）。抽成纯函数的唯一理由：对账的
    **dry-run** 要能回答「这一单会不会被接手、接给谁」而又不能调会写库的 `_apply_member_status`
    ⇒ 写入段与本命令共用同一处（⛔ 命令里另写一份 `mapped in … and status == …` 会漂出说假话的预览）。"""
    return (
        mapped in CLAIM_MID_TARGETS or token == REMOTE_DONE_LABEL
    ) and order_status == OrderStatus.WAITING_GRAB


# ══ 二·五、老地址那条例外：只对一台机器 / 一个租户成立（⛔ 不是通用做法）════════
# ⚠️⚠️ 边界承重：① 行归哪个租户是**机器级**事实（闸 = `ENV_LEGACY_CALLBACK_TENANT`，默认关，⛔ 不许改成无条件）；② 报文没码 ⇒ 靠 `orderNo` 在本租户 `JidanpuMemberOrderLink.jidanpu_order_id` 里找链路、落到链路所属绑定（⛔ 不是跑这一轮的那行）；③ 找不到单号 ⇒ 只留台账、⛔ 不进错误列；④ ⛔ 只处理状态与 `action=30`，别的 action 照旧只留痕。（背景与逐条展开见 `docs/code-comment-notes.md` 本文件分节。）


def legacy_notify_route_enabled() -> bool:
    """老回调地址（`/jidanpu/v1/notify`）的行，本租户要不要接管（边界见「二·五」）。判据 = `ENV_LEGACY_CALLBACK_TENANT` **非空**且与当前租户 schema 名**全等**（⛔ 不 `startswith` / 不归一化大小写：租户名是定长标识，模糊匹配会把两个租户混成一个）。⚠️ 每次调用都重读环境变量（照 `apps/callback/config.py` 同一口径，「缓存过一次就再也不看」是这类开关最常见的静默坑）；租户标识只来自 `connection.schema_name`（⛔ 不接受参数传租户）。
    """
    wanted = (os.environ.get(ENV_LEGACY_CALLBACK_TENANT) or "").strip()
    if not wanted:
        return False
    return wanted == (getattr(connection, "schema_name", "") or "")


def member_all_bindings() -> list:
    """**本租户**的全部下家绑定（按 `pk` 升序）——老地址那条路专用。⚠️ 不能用按回调码分组的 `member_group_bindings`：
    老地址的报文里**没有码**，而同租户可能有两行②式绑定 ⇒ 只在同码组里找会把**别人家的单**报成
    「找不到这条链路」（假警报）。租户边界仍由库决定（只查当前 schema）：⛔ 不接受任何租户参数。"""
    return list(JidanpuMemberBinding.objects.order_by("pk"))

# ══ 三、台账写口（⛔ 幂等键与下家线既有的那两个 helper **同一套**）════════════════
#: `target_type` 列的取值（≤16 字符）：②式这条路上没有我方订单可指认的事实用它当幂等键的一半。与上家线取同一个值是有意的：
#: 它是一类**事实**的名字，「这一行归哪条线」由 `kind` 前缀与 `payload` 键名回答（⛔ 不靠 target_type 分线）。
TARGET_TYPE_CALLBACK = "callback"


#: ⭐ 报文取值：**复用引擎那一份**（`strip()` 后的字符串、`None` ⇒ 空串；⛔ 不在这里另写一份
#: —— 同一件事两处判据必然漂，而「空值算不算有值」正是那条判据最容易漂的地方，E-0021 家族）。
_text_of = jidanpu_openapi_consume._text_of


def _note_ref(*, binding, target_ref: str, kind: str, reason: str, status: str = "done",
              payload: dict | None = None) -> bool:
    """本线「没有我方订单可指认」事实的**幂等**写口；回「这次真的写了吗」。幂等键 = `(direction, kind, target_type, target_ref, reason, payload.member_binding_id)`，与下家线既有的 `_note_order_once` / `_note_binding_once` **逐条同形**（⛔ 不是第三套去重口径）。不复用 `jidanpu_upstream._note_once_ref`：它往 payload 写 `upstream_binding_id`，而两张绑定表主键**各自从 1 开始** ⇒ 排查会认错线（键必须用下家的）。⛔ `payload` 不放凭据（id / 码 / 单号 / sha 是定位事实）。
    """
    text = reason[:255]
    existing = (
        JidanpuSyncEvent.objects.filter(
            direction=JidanpuSyncEvent.Direction.IN, kind=kind, target_type=TARGET_TYPE_CALLBACK,
            target_ref=target_ref[:191], reason=text,
            payload__member_binding_id=binding.pk,
        )
        .order_by("id")
        .first()
    )
    if existing is not None:
        return False
    data = {"member_binding_id": binding.pk}
    data.update(payload or {})
    jidanpu_common.write_event(
        direction=JidanpuSyncEvent.Direction.IN, kind=kind, status=status,
        target_type=TARGET_TYPE_CALLBACK, target_ref=target_ref[:191], reason=text,
        payload=data,
    )
    return True


def _note_binding_ref(*, binding, kind: str, reason: str, status: str = "failed",
                      payload: dict | None = None) -> bool:
    """绑定维度（没有单号可指认，例如「查不动」）的幂等台账；`target_ref` = 绑定主键。"""
    return _note_ref(
        binding=binding, target_ref=str(binding.pk), kind=kind, reason=reason,
        status=status, payload=payload,
    )


# ══ 四、一条事件的落地：找链路 → 查权威 → 判 → 写（⭐ 回调与对账共用这一段）══════════

def member_group_bindings(binding) -> list:
    """与这一行**共用同一个回调码**的那些绑定（含它自己，按 `pk` 升序）。为什么是「一组」而非「一行」（业务方裁定）：同租户 + 同 `jidanpu_base_url` 共用一个回调码 ⇒ 同码多绑定是常态，回调只带一个 `orderNo`——⛔ 只看自己那行会把**别人家的单**报成「没有链路」（假警报）。
    判据 = `callback_code` **全等**（spec §5.5，⛔ 不 `startswith`）且非空；码为空 / ①式 ⇒ 只回自己。⛔ **不按 `mode` 过滤**：一行切回①式后码照旧留着而没人消费回调，排除它会让同码兄弟行把它的单报成「找不到链路」；「不消费」由 `_member_landed` 处置（同店 ⇒ 本行照常应用；另一店 ⇒ `skipped` 台账，见 `_sibling_lands_here`）。
    """
    code = (getattr(binding, "callback_code", "") or "").strip()
    if not code:
        return [binding]
    rows = list(
        JidanpuMemberBinding.objects.filter(callback_code=code).order_by("pk")
    )
    if all(row.pk != binding.pk for row in rows):
        # 防御（正常**不可达**：调用方拿的是库里那一行）：本行不在结果里就补上，
        # ⛔ 不许静默少一行 —— 少了它就等于「这一组里没有本行」，路由会指错。
        rows.insert(0, binding)
    return rows


def _link_of(binding, order_no, source_order_no, *, legacy: bool = False):
    """先消除副本归属歧义，再判定能否处理，避免已关闭的副本把回调让给另一张单。"""
    group = member_all_bindings() if legacy else member_group_bindings(binding)
    # 旧地址没有绑定码，换绑后的旧副本仍是判断单号冲突的证据。
    links = JidanpuMemberOrderLink.all_objects if legacy else JidanpuMemberOrderLink.objects
    matches = list(
        links.filter(binding__in=group, jidanpu_order_id=order_no).select_related("binding", "order")
    )
    if not matches:
        return None, None, None, ""
    raw = matches[0]
    if len(matches) > 1:
        source_matches = [
            link for link in matches
            if source_order_no and jidanpu_member.effective_source_order_no(link.order) == source_order_no
        ]
        if len(source_matches) != 1:
            return None, None, None, REASON_AMBIGUOUS_REMOTE_ID
        raw = source_matches[0]
        if legacy and _legacy_source_was_used_by_another_copy(matches, raw, source_order_no):
            return None, None, None, REASON_AMBIGUOUS_REMOTE_ID
    if legacy and source_order_no and _legacy_source_has_other_blank_link(group, raw, source_order_no):
        return None, None, None, REASON_AMBIGUOUS_REMOTE_ID
    owner = raw.binding
    if jidanpu_member.pullable_links(owner).filter(pk=raw.pk).exists():
        return raw, raw, owner, ""
    if raw.order.deleted_at is not None:
        why = NOT_PULLABLE_SOFT_DELETED
    elif raw.retired_at is not None:
        why = NOT_PULLABLE_RETIRED
    elif raw.withdrawn_at is not None:
        why = NOT_PULLABLE_WITHDRAWN
    else:
        why = NOT_PULLABLE_TERMINAL
    return None, raw, owner, REASON_NOT_PULLABLE.format(order_no=order_no, why=why)


def _legacy_source_was_used_by_another_copy(links, chosen, source_order_no):
    competing_order_nos = {link.order.order_no for link in links if link.order_id != chosen.order_id}
    edits = OperationLog.objects.filter(
        order_no__in=competing_order_nos, action="order.update", field_name="source_order_no",
    ).values_list("order_no", "from_value")
    max_saved_width = OperationLog._meta.get_field("from_value").max_length
    for order_no, previous in edits.iterator():
        effective = previous.strip() or order_no
        # 旧值可能被审计列截断。相同前缀不足以判断是哪张副本。
        if effective == source_order_no or (
            len(previous) == max_saved_width and source_order_no.startswith(effective)
        ):
            return True
    return False


def _legacy_source_has_other_blank_link(group, chosen, source_order_no):
    copies = JidanpuMemberOrderLink.objects.filter(
        binding__in=group, withdrawn_at__isnull=True, jidanpu_order_id__regex=r"^\s*$",
    ).exclude(order_id=chosen.order_id).select_related("order")
    return any(
        copy.order.deleted_at is None
        and jidanpu_member.effective_source_order_no(copy.order) == source_order_no
        for copy in copies.iterator()
    )


def _orders_by_source_order_no(source_order_no: str) -> list:
    """按有效来源单号找订单，粗筛容纳存量数据首尾空白。"""
    if not source_order_no:
        # ⛔ 空值不粗筛（`contains=""` 会命中全表）、也谈不上「有效来源单号」。
        return []
    rows = (
        Order.objects.filter(
            Q(source_order_no__contains=source_order_no) | Q(order_no__contains=source_order_no)
        )
        .order_by("pk")
    )
    return [row for row in rows if jidanpu_member.effective_source_order_no(row) == source_order_no]


def _heal_missing_remote_id(*, binding, order_no, source_order_no, legacy: bool,
                            findings: list[str]):
    """**自愈**：按回调 `orderNo` 找不到链路时，用报文里的 `sourceOrderNo` 反查我方那张单、补它链路上**缺的那一格**（`jidanpu_order_id`）。回 `(link, owner, outcome)`：补上了 ⇒ `(link, owner, None)` + `member.openapi_healed`/`done`，调用方**照常继续处理**（查权威 → 判 → 写，⛔ 不是「补完就 return」）；已有别的号 / 多张单 ⇒ `(None, None, NOTED/SKIPPED)` + `heal_blocked` ⛔ 到此为止（不覆盖、不猜）；找不到单 / 没有 `sourceOrderNo` ⇒ `(None, None, None)` 一句话不写，落回既有「找不到链路」处置——不新造第五档措辞、不改变「什么算故障」。
    ⚠️ **只补空、绝不覆盖**：`UPDATE` 的 `WHERE` 带上**读到的那个值**（比较后写），并发下也不盖别人刚写的号，写后再回读；查范围与 `_link_of` 逐字同源 ⇒ 「这张单归哪一行」不可能两个答案；`reason` 只有单号（⛔ 无凭据）。
    """
    order_no = (order_no or "").strip()
    source_order_no = (source_order_no or "").strip()
    if not order_no or not source_order_no:
        return None, None, None
    matched = _orders_by_source_order_no(source_order_no)
    if not matched:
        return None, None, None
    if len(matched) > 1:
        reason = REASON_HEAL_DUPLICATE.format(
            order_no=order_no, source_order_no=source_order_no, count=len(matched),
        )
        _note_binding_ref(
            binding=binding, kind=KIND_HEAL_BLOCKED,
            status=_HEAL_BLOCKED_LEGACY[0] if legacy else _HEAL_BLOCKED_NEW[0], reason=reason,
        )
        if not legacy:
            findings.append(reason)
        return None, None, (
            jidanpu_openapi_consume.OUTCOME_SKIPPED if legacy
            else jidanpu_openapi_consume.OUTCOME_NOTED
        )

    order = matched[0]
    group = member_all_bindings() if legacy else member_group_bindings(binding)
    links = list(
        JidanpuMemberOrderLink.objects.filter(binding__in=group, order=order)
        .select_related("binding", "order")
        .order_by("pk")
    )
    live_links = [
        link for link in links
        if link.withdrawn_at is None and _same_member_store(binding, link.binding)
    ]
    if links and not live_links:
        return None, None, jidanpu_openapi_consume.OUTCOME_SKIPPED
    # ⚠️ 「那一格是空的」= 空串 / 纯空白（与 `_text_of` 同一条口径：空白一律当「没有」）。
    empty = next(
        (link for link in live_links if not str(link.jidanpu_order_id or "").strip()), None
    )
    #: ⚠️ 冲突优先于补：只要本组里有一条链路的号**不等于**这条回调给的号（按既有的**全等**口径
    #: 判，⛔ 不在这里发明「归一化之后再比」的第二把尺子），就不补 —— 否则会把两张远端单
    #: 的链路接错（那正是「一张单的链路指向别人的单号」这种最贵的形态）。
    conflict = next(
        (
            link for link in live_links
            if str(link.jidanpu_order_id or "").strip() and link.jidanpu_order_id != order_no
        ),
        None,
    )
    if conflict is not None:
        reason = REASON_HEAL_CONFLICT.format(
            order_no=order_no, source_order_no=source_order_no,
            our_order_no=order.order_no, existing=conflict.jidanpu_order_id,
        )
        _note_order_ref(
            binding=conflict.binding, order=order, kind=KIND_HEAL_BLOCKED,
            status=_HEAL_BLOCKED_LEGACY[0] if legacy else _HEAL_BLOCKED_NEW[0], reason=reason,
        )
        if not legacy:
            findings.append(reason)
        return None, None, (
            jidanpu_openapi_consume.OUTCOME_SKIPPED if legacy
            else jidanpu_openapi_consume.OUTCOME_NOTED
        )
    if empty is None:
        # ⛔ 正常**到不了**（这一支只在 `_link_of` 连一条都没找到时才被调；真按 `order_no` 找得到
        #    的那条链路早在上面那一跳就被认出来了）⇒ 防御：⛔ 不凭空补、⛔ 不猜，
        #    回「没找到」让调用方落回既有那一档（那是一句**真话**：这条回调确实没配上链路）。
        return None, None, None

    owner = empty.binding
    with jidanpu_assignment_lock.sending(
        binding_id=owner.pk, order_id=order.pk, source_binding_id=binding.pk,
    ) as busy:
        if busy:
            return None, None, jidanpu_openapi_consume.OUTCOME_DEFERRED
        if (
            not jidanpu_member._binding_current_rows(binding).filter(enabled=True).exists()
            or not jidanpu_member._active_link_current(owner, empty)
            or not jidanpu_member.pullable_links(owner).filter(pk=empty.pk).exists()
        ):
            return None, None, jidanpu_openapi_consume.OUTCOME_SKIPPED
        if legacy and not _legacy_can_fill_id(owner, order):
            _note_order_ref(
                binding=owner, order=order, kind=KIND_HEAL_BLOCKED, status="skipped",
                reason="旧回调无法确认订单所属店铺，保留空单号，等待专属回调或人工核对",
            )
            return None, None, jidanpu_openapi_consume.OUTCOME_SKIPPED
        with transaction.atomic():
            jidanpu_assignment_lock.lock_remote_copy(owner.jidanpu_base_url, order_no)
            occupied = jidanpu_member._remote_copy_owned_elsewhere(owner, order, order_no)
            if not occupied:
                written = JidanpuMemberOrderLink.objects.filter(
                    pk=empty.pk, jidanpu_order_id=empty.jidanpu_order_id, withdrawn_at__isnull=True,
                ).update(jidanpu_order_id=order_no)
                empty.refresh_from_db(fields=["jidanpu_order_id"])
        if occupied:
            return None, None, jidanpu_openapi_consume.OUTCOME_SKIPPED
    if written != 1 and empty.jidanpu_order_id != order_no:
        # 这一格在我们读到它之后被别人改了（而且改的不是这个号）⇒ ⛔ 不产出结论、⛔ 不写台账
        # （下一轮按新的值重新判那一条链路的归属）。
        return None, None, None

    reason = REASON_HEALED.format(
        order_no=order_no, source_order_no=source_order_no, our_order_no=order.order_no,
    )
    _note_order_ref(
        binding=empty.binding, order=order, kind=KIND_HEALED, status="done", reason=reason,
    )
    # ⚠️ 这一格**不进 `findings`**：自愈是**办成了**的事（与「找不到链路」那一档不同名）。
    return empty, empty.binding, None


def _legacy_can_fill_id(owner, order) -> bool:
    """旧地址缺少店铺凭证，仅单店且无换绑旧副本时能沿用按来源单号补齐。"""
    addresses = {
        address for address in JidanpuMemberBinding.objects.values_list("jidanpu_base_url", flat=True).distinct()
        if (address or "").strip()
    }
    return addresses == {owner.jidanpu_base_url} and not JidanpuMemberOrderLink.all_objects.filter(
        order=order, retired_at__isnull=False,
    ).exists()


def _machine_claim_failure_reason(result) -> str:
    """机器接手三档**可预期拒绝**的人话（复用①式既有的 `AUTO_CLAIM_*_REASON` 常量）。②式对这三档**不做**①式那套
    竞态分流（查 `_main_taker_id` / 排 `member.withdraw` 是①式快路的收口逻辑）：一律「订单维度 `failed` 台账 +
    人话进 `findings`（要人看）」，照既有完成 `DENIED` 的处置形状（spec §2.4；退单 / 撤下是 outbox 那条线的职责）。"""
    if result.same_person_conflict:
        return jidanpu_member.AUTO_CLAIM_SAME_PERSON_REASON
    if result.claim_error is not None:
        return f"{jidanpu_member.AUTO_CLAIM_REFUSED_REASON}{result.claim_error}"
    return f"{jidanpu_member.AUTO_CLAIM_DENIED_REASON}{result.denial}"


def _apply_member_status(*, binding, order, link, order_no, queries, now, findings: list[str],
                         trigger: str) -> str:
    with jidanpu_assignment_lock.sending(binding_id=binding.pk, order_id=order.pk) as busy:
        if busy:
            findings.append(busy)
            return jidanpu_openapi_consume.OUTCOME_RETRY
        if not jidanpu_member._active_link_current(binding, link):
            return jidanpu_openapi_consume.OUTCOME_SKIPPED
        return _apply_member_status_locked(
            binding=binding, order=order, link=link, order_no=order_no,
            queries=queries, now=now, findings=findings, trigger=trigger,
        )


def _apply_member_status_locked(*, binding, order, link, order_no, queries, now,
                                findings: list[str], trigger: str) -> str:
    """**查权威 → 认不认识 → 判 → 写**（②式回调与兜底对账共用的**唯一**那一段），回 `OUTCOME_*` 供回调层推水位 / 计数。⭐⭐ **`binding` 是「应用身份」，不是「谁在查」**（业务方 B 档）：本函数里一切「把单接到谁名下 / 台账记给谁」的判据都读这一个入参，而**查权威**用 `queries`——跨绑定时传 `binding=链路自己的 owner` + `queries=消费方`，两件事分开。
    ⭐⭐ **「查不动」与「对方明确说没有这张单」是两档、处置相反**：前者（网络 / 5xx / 协议错 / 取 token 失败）⇒ `OUTCOME_RETRY`（不写 + 水位不动）；后者（`confirmed_absent_order_error`）⇒ **终态收口** `OUTCOME_SKIPPED`（不写 + 人话台账 + 水位前进）。⛔ 并成一档就是「说假话的留痕」，并错方向的代价是它后面所有回调都不被处理。
    """
    try:
        remote_status = queries.status_of(order_no)
    except Exception as exc:  # noqa: BLE001 —— 上游 / 配置失败一律「不写 + 本轮停在这里」
        if isinstance(exc, jidanpu_openapi.OpenApiError) and jidanpu_openapi.confirmed_absent_order_error(exc):
            # 只有明确无单才结束回调并推进水位。
            reason = _GONE_REASONS[trigger].format(order_no=order_no)
            _note_order_ref(
                binding=binding, order=order, kind=KIND_STATUS_IN, status="skipped",
                reason=reason,
            )
            return jidanpu_openapi_consume.OUTCOME_SKIPPED
        # ⛔ **回调只当提醒**的落点：查不动权威状态时**一个字都不写订单**
        # （⛔ 绝不拿回调报文里的 `status` 顶上）。
        # ⚠️ 只写异常**类型名 + 根因指纹**（`_failure_detail` 是本仓唯一那一份口径）。
        detail = jidanpu_openapi_consume._failure_detail(exc)
        reason = _QUERY_FAILED_REASONS[trigger].format(detail=detail)
        _note_binding_ref(
            binding=binding, kind=KIND_QUERY_FAILED, status="failed", reason=reason,
            payload={"order_no": order_no, "trigger": trigger},
        )
        findings.append(reason)
        return jidanpu_openapi_consume.OUTCOME_RETRY

    if remote_status is None:
        # 撞了「一轮最多查几条」的上限（本轮只推迟不丢）：水位不前移 ⇒ 下一轮接着查。
        # ⚠️⭐ **为什么 `None` 只可能是撞上限**（读码核实）：`_StatusQueries.status_of` 全文只有一条 `return None`（`if self.queried >= self.cap`），另两个出口是「缓存里的值」与 `.status`，而那个 `.status` 不可能空 / 缺（`OpenApiClient.get_order_status` 对非非空字符串一律抛 `OpenApiError`）⇒「查不到值」在这里不是 `None`，走的是上面那条 `except` 或下面的 `REASON_STATUS_MISSING`。⛔ 三档不许并档。
        reason = jidanpu_openapi_consume.REASON_ORDER_CAP.format(
            cap=queries.cap
        )
        findings.append(reason)
        return jidanpu_openapi_consume.OUTCOME_RETRY

    # ⚠️ 走到这一行时 `remote_status` **确定不是 `None`**（上面那一支已经早退）
    # ⇒ ⛔ 这里**不再判一次 `None`**：那是一个**永不可达**的分支，而且一旦把「查不到值」接在它上面，
    # 就会与「撞上限」混成同一档（前者要水位前移、后者要水位不动 —— 判据相反）。
    token = str(remote_status).strip()
    if not token:
        # 「没有值」与「有个怪值」是两件事（照上家线那条同源口径）：⛔ 什么都不写，
        # 但**不当成**「认不出来」（台账措辞与后续动作都不一样）。
        _note_order_ref(
            binding=binding, order=order, kind=KIND_STATUS_IN, status="skipped",
            reason=REASON_STATUS_MISSING,
        )
        return jidanpu_openapi_consume.OUTCOME_SKIPPED
    if not jidanpu_member.is_known_remote_status(token):
        reason = REASON_UNKNOWN_STATUS.format(token=token)
        _note_order_ref(
            binding=binding, order=order, kind=KIND_UNKNOWN_STATUS, status="failed",
            reason=reason, payload={"remote_status": token, "trigger": trigger},
        )
        findings.append(reason)
        return jidanpu_openapi_consume.OUTCOME_NOTED

    if not jidanpu_member._active_link_current(binding, link):
        return jidanpu_openapi_consume.OUTCOME_SKIPPED
    order.refresh_from_db()

    # ⭐⭐ 闸 3（D-0227 / E-0236）：`in_hall` 随权威观察刷新。token ∈ {待上号, 代练中, 待验收, 已完成} ⇒ 对方副本已离开大厅是权威事实 ⇒ 收窄 `link.in_hall=False`——无论我方状态、无论本次是否接手，进这一档就刷新；必须在 `machine_claim_order` **之前**落库（②式没有大厅监视 ⇒ 本地 `in_hall` 恒 True 永不过期，E-0236 根因①；闸 1 之后这一格是「镜像接手不会误排 unlist」的双保险）。⚠️ token=未分配 ⇒ **不**回写 True（5 格回包分不清「厅里的未分配」与「指派录进去的未指派单」，宁缺毋滥）；只收窄 `in_hall` 一列（⛔ 不整行 `save()`：链路行上还有并发写的健康列）。
    mapped = jidanpu_member.map_remote_status(token)
    wants_claim = wants_machine_claim(mapped=mapped, token=token, order_status=order.status)
    if wants_claim and jidanpu_relaunch.claim_blocked(binding, link):
        return jidanpu_openapi_consume.OUTCOME_SKIPPED
    if mapped in CLAIM_MID_TARGETS or token == REMOTE_DONE_LABEL:
        changed = JidanpuMemberOrderLink.objects.filter(
            pk=link.pk, jidanpu_order_id=link.jidanpu_order_id,
            listed_at=link.listed_at, withdrawn_at=link.withdrawn_at,
        ).update(in_hall=False)
        if not changed:
            return jidanpu_openapi_consume.OUTCOME_RETRY
    # ⭐⭐ 中间档自动接手（D-0226，spec §2.2 / §2.4）：远端 ∈「有人在做」几档**或**「已完成」、我方还在「待抢单」⇒ 与①式日志快路同一条业务口径：机器接手给绑定的代练员（⭐ 钱路径只许一份实现；失败三档 ⇒ `failed` 台账 + `findings` + `OUTCOME_NOTED`）。远端接单人身份 `GetOrderStatus` 拿不到（回包恰好 5 格）⇒ 传 `None`（只进审计、不参与接手对象选择）；我方不是「待抢单」⇒ 常态，不接手不留台账、落下面共用出口。
    #    ⭐ **闸 1（D-0227 / E-0236）**：镜像接手传 `suppress_outbound_sync=True` ⇒ `claim_order` 不对对方系统产生任何写动作（不排 assign / unlist / status_out）——镜像接手的回声正是 E-0236 把对方已接走的单推成已退单的通道；①式快路照旧不传（既有行为一字不动）。
    if wants_claim:
        result = jidanpu_member.machine_claim_order(
            binding=binding, order=order, remote_uid=None, remote_name=None,
            remote_order_id=link.jidanpu_order_id,
            source_note=f"官方API权威状态={token}",
            suppress_outbound_sync=True,
        )
        if not result.claimed:
            # 三档可预期拒绝（照既有完成 `DENIED` 的处置形状）：订单维度 `failed` 台账 + 人话进 `findings`（要人看）
            # ⇒ `OUTCOME_NOTED`：**有结论** ⇒ 水位 / 窗口照常前进（⛔ 不是「查不动」档，不许钉住后面的回调）。
            reason = _machine_claim_failure_reason(result)
            _note_order_ref(
                binding=binding, order=order, kind=KIND_STATUS_IN, status="failed",
                reason=reason,
            )
            findings.append(reason)
            return jidanpu_openapi_consume.OUTCOME_NOTED
        if mapped in CLAIM_MID_TARGETS:
            # 中间档**只接手到「进行中」**（「待验收」那一格保留给远端「已完成」的既有
            # 完成写入，spec §2.2）⇒ 本档到此为止。
            return jidanpu_openapi_consume.OUTCOME_APPLIED
        # 远端「已完成」：**先接手、重取订单行、再走既有完成写入**——拿陈旧的 `WAITING_GRAB`
        # 进 `apply_remote_completion` 会被幂等 / 边判错（spec §2.2 的同一串观察两步）。
        order.refresh_from_db()
    # （未分配 / 已暂停 / 问题单 / 已退单 / 撤销中 / 仲裁中 / 我方早已有人接 ⇒ 保持今日行为。）

    # ⭐ 判 + 写：**整段共用**（下家线唯一那一份，慢路回拉调的是同一个函数）⇒「②式多写一列 / 各判一套」在本仓没有落点。
    #    ⭐ **`openapi_pics=queries`**：②式没有①式的登录凭据 ⇒ 由②式自己的官方 API 取图口补拉一次**全量**进度图，
    #    **替代**②式下被跳过的 `ViewPic`（⛔ 不是另加一次）；传本轮的 `queries`（与查权威共用同一客户端，⛔ 不再取一次 token）。
    outcome = jidanpu_member.apply_remote_completion(
        binding=binding, order=order, link=link, remote_status=token,
        nodes=nodes_of(binding.user), now=now, client=None, openapi_pics=queries,
        trigger=trigger,
    )
    if outcome.verdict == jidanpu_member.REMOTE_DONE_APPLIED:
        return jidanpu_openapi_consume.OUTCOME_APPLIED
    if outcome.verdict == jidanpu_member.REMOTE_DONE_DENIED:
        # 状态机 / 节点门拒绝（多半是缺节点）⇒ **要人看**（台账那一条已由共用那一段写下）。
        findings.append(outcome.loud)
        return jidanpu_openapi_consume.OUTCOME_NOTED
    return jidanpu_openapi_consume.OUTCOME_SKIPPED


def _note_order_ref(*, binding, order, kind: str, status: str, reason: str,
                    payload: dict | None = None) -> None:
    """订单维度的幂等台账（复用下家线既有的 `jidanpu_member._note_order_once`：它的幂等键里已含
    `payload.member_binding_id` ⇒ ⛔ 不另写一份去重）。⚠️ 它不是 `_note_ref`：那一个的 `target_ref` 是任意
    事实标识，这一个必须是**我方单号**（页面上「这张单的台账」按 `target_ref=order_no` 查）。"""
    jidanpu_member._note_order_once(
        binding=binding, order=order, kind=kind, status=status, reason=reason,
    )


def _same_member_store(binding, owner) -> bool:
    """两行是不是**同一家店**（同 `jidanpu_base_url`；空地址不算同店）。比较用**原样字符串**（与地址级去重 /
    `owners_of` 按地址分组 / `downstream_addresses_of_taker` 逐字一致）：⛔ 不自创归一化——那会让「同店」与
    「同地址去重」两处判据漂。任一为空白 ⇒ `False`：⛔ 拿空串配对（跨店代写的代价是改错另一家店的单）。"""
    left = getattr(binding, "jidanpu_base_url", "") or ""
    right = getattr(owner, "jidanpu_base_url", "") or ""
    return bool(left.strip()) and bool(right.strip()) and left == right


def _sibling_lands_here(*, binding, owner, link) -> bool:
    """回调定位到的链路属于**同一家店的另一行**（`owner`）时：这一条要不要**照常应用**？（业务方拍板 B 档，现场见 E-0290）两档：**启用中的②式** ⇒ ⛔ 跳过（它自己那一轮会办掉，再办一次就是同一件事两个执行者——打两遍上游、两张台账）；**①式 / 已停用** ⇒ ⭐ 照常应用但**应用身份 = `owner`**（权威状态仍用消费方凭据查，「接到谁名下」用 owner 的人）。
    两道防呆（不满足就退回 skip + 台账）：① 只对**同一家店**生效（`_same_member_store`，否则是拿 A 店凭据动 B 店的单）；② 链路不在处理集合里（`link is None` ⇒ 没有可应用的状态）。「那一行会不会自己消费」复用 `_is_openapi(owner) and owner.enabled`（与调度层同一个问题，⛔ 不重写）。
    """
    if jidanpu_openapi._is_openapi(owner) and bool(owner.enabled):
        return False
    if link is None:
        return False
    return _same_member_store(binding, owner)


def _member_landed(*, binding, order_no, source_order_no, queries, now,
                   findings: list[str], legacy: bool = False) -> str:
    """**一条回调**的 ⑤–⑧ 步（引擎调它；见 `CallbackLine.landed`）；`trigger` 恒为 `TRIGGER_CALLBACK`（兜底对账走 `_member_apply`）⇒ ⛔ 不加恒定入参。同码另一条绑定的单（`owner != self`）分三档（判据 = `_sibling_lands_here`）：**启用中的②式** ⇒ ⛔ 一字不写不留痕（它自己那一轮会办，两执行者打两遍上游、两张台账）；**①式/已停用且同店** ⇒ ⭐ 照常应用（业务方拍板 B 档：官方回调是主路，本行借自己凭据查权威、写到那条链路）；**另一家店** ⇒ 订单维度 `skipped` 台账（⛔ 跨店不代写，不进 `findings`——停用是用户自己按的）。
    ⭐⭐ 跨绑定**应用身份 = `owner`**：`machine_claim_order(binding=owner)`（MAIN 绩效钱算它名下）、`nodes_of(owner.user)`（节点门按它职位）、台账记 owner 名下，**水位仍记消费方**（谁读的记给谁）——实现就是把 `binding=owner` 交给 `_apply_member_status`。`legacy=True`（老地址）：范围 = 本租户全部绑定，找不到只留 `KIND_LEGACY_UNKNOWN_ORDER` ⛔ 不进 `findings`，人话换老地址版（没有码）。
    ⭐ `orderNo` 一条链路都对不上时先试**自愈**（`_heal_missing_remote_id`）：补上了照常往下走（⛔ 不是「补完就 return」）；补不上留 `member.openapi_heal_blocked` ⛔ 一字不写订单；找不到单落回既有处置。（三档表与跨绑定身份逐条清单见 `docs/code-comment-notes.md` 本文件分节。）"""
    link, raw, owner, why = _link_of(binding, order_no, source_order_no, legacy=legacy)
    if link is None and raw is None and not why:
        # ⭐ **自愈**（见模块「自愈」节）：`orderNo` 一条链路都对不上时，用报文里的 `sourceOrderNo`
        # 反查我方那张单、补链路上缺的那格。⚠️ 只在**两条都没命中**时试（`raw` 非空 = 有链路、只是不进
        # 处理集合 ⇒ 那格已有号，没什么可补）；补上之后**照常往下走**（⛔ 不是「补完就 return」）。
        healed, healed_owner, healed_outcome = _heal_missing_remote_id(
            binding=binding, order_no=order_no, source_order_no=source_order_no,
            legacy=legacy, findings=findings,
        )
        if healed_outcome is not None:
            # 自愈**补不上**（已有别的号 / 多张单）⇒ 本函数已按既有那两档的纪律留过痕，
            # 这一条回调到此为止（⛔ 不覆盖、⛔ 不拿别人的号去写这张单）。
            return healed_outcome
        if healed is not None:
            link, owner = healed, healed_owner
    #: ⭐ **应用身份**（业务方 B 档）：`None` = 这条链路是**本行**自己的单；
    #: 非空 = 它是**同一家店的另一行**的单、而那一行不消费回调 ⇒ 由本行照常应用，
    #: 但「接到谁名下」一律用这个 `owner`（见 `_sibling_lands_here` 与 docstring 那一节）。
    identity = None
    if owner is not None and owner.pk != binding.pk:
        if not _sibling_lands_here(binding=binding, owner=owner, link=link):
            return _note_group_sibling(
                binding=binding, owner=owner,
                order=(link if link is not None else raw).order,
                order_no=order_no, legacy=legacy,
            )
        identity = owner
    if link is None:
        # ⚠️ 只有 `identity is None` 才会走到这里：`_sibling_lands_here` 对 `link is None` 恒回 False
        #    ⇒ 上面那一支已 return（不会带着 owner 身份落到这里）。
        if raw is not None:
            # 有链路但按判据①不进处理集合（已终态 / 已软删）⇒ ⛔ 不查权威（省一次上游读）、不写订单，留一条 `skipped` 的痕（**不是**失败）。⚠️ 不进 `findings`（不盖 `last_error`）：这一档在下家线自己的口径里就不是异常（慢路同一件事也只写 `skipped` 台账）；「我方完事了、对方还在推」是常态，每轮盖进错误列就是**把正常说成错误**。
            _note_order_ref(
                binding=binding, order=raw.order, kind=KIND_STATUS_IN, status="skipped",
                reason=why,
            )
            return jidanpu_openapi_consume.OUTCOME_SKIPPED
        # ⭐ 「找不到链路」那一档的留痕/进不进错误列 = **本模块唯一那一份**
        #    （`_note_unknown_order`；`action=30` 那条路调的是同一个函数）。
        return _note_unknown_order(
            binding=binding, order_no=order_no, source_order_no=source_order_no,
            legacy=legacy, findings=findings, reason=why,
        )
    return _apply_member_status(
        # ⭐ 跨绑定那一档：**应用身份 = owner**（`machine_claim_order` / `nodes_of` / 台账都读它），
        #    `queries` 仍是**消费方**的客户端（查权威状态用这一条②式的凭据）。
        binding=identity if identity is not None else binding,
        order=link.order, link=link, order_no=order_no, queries=queries,
        now=now, findings=findings, trigger=jidanpu_member.TRIGGER_CALLBACK,
    )


def _note_group_sibling(*, binding, owner, order, order_no: str,
                        legacy: bool = False) -> str:
    """这一条回调**这一轮没有人办它** ⇒ 不写订单、按缘由留痕（`_sibling_lands_here` 的「跳过」支，判据不在本函数）。两档缘由（本函数只管措辞）：**那一行自己会办** ⇒ ⛔ 一个字不写（连台账也不写——每行跑一轮，都写的话同一条事件被整组各留一行，是刷屏）；**那一行不消费但不同店** ⇒ 订单维度 `skipped` 台账（「按定义不做的事」⇒ 不进 `findings`——停用是用户自己按的，说成「出错」就是错误列说假话）。
    「同店 + 链路在处理集合」那一档不再走这里（B 档照常应用，见 `_member_landed`）；`legacy=True` 只换人话、处置逐字相同（老地址没有码）。
    """
    consumes = jidanpu_openapi._is_openapi(owner) and bool(owner.enabled)
    if consumes:
        # 那一行自己那一轮会办掉它：⛔ 一个字都不写（连台账也不写——每行跑一轮，都写的话同一条事件
        # 会被「同一个码下的每一行」各留一行，是刷屏）。
        return jidanpu_openapi_consume.OUTCOME_SKIPPED
    why = (
        "它现在是①式（用账号 + 密码那条路，不消费回调）"
        if not jidanpu_openapi._is_openapi(owner)
        else "它已被停用"
    )
    if not _same_member_store(binding, owner):
        # ⚠️ 这一句是**事实**、不是修辞：跳过它的理由正是「不是同一家店」（跨店不代写）。
        why = f"{why}，而且它和这条回调的消费方**不是同一家店**（跨店不代写）"
    reason = (
        REASON_LEGACY_GROUP_NOT_CONSUMING.format(
            owner_pk=owner.pk, order_no=order_no, why=why,
        )
        if legacy
        else REASON_GROUP_NOT_CONSUMING.format(
            owner_pk=owner.pk, order_no=order_no, why=why,
        )
    )
    _note_order_ref(
        binding=binding, order=order, kind=KIND_STATUS_IN, status="skipped", reason=reason,
    )
    # ⚠️ **不进 `findings`**：见 `_member_landed` 那张表（停用是用户自己按下去的）。
    return jidanpu_openapi_consume.OUTCOME_SKIPPED


def _member_other_action(*, binding, params, row, legacy: bool, now,
                        findings: list[str]) -> str | None:
    """`action=30`（订单有新截图）：报文里带着 `imgUrl` ⇒ **直接**入库。回 `OUTCOME_*` = 本线办掉了；回 `None` = 不是本线的事（引擎落回老口径那条痕）。快路口径：⛔ 不打额外上游请求（直链就在报文里）；⭐ 去重按**对象 key**（`remote_object_key`，⛔ 不是整条 URL——实测重复送达 0.5%、同一张图出现在两个域名上，按 URL 判重会存两行）。
    落库复用 `jidanpu_upstream_pics.store_remote_pics`（⛔ 不另造表 / 目录 / 循环）；两类失败分得开（⛔ 不合成一档）：「配不上」（找不到链路 / 没有可用 `imgUrl`）⇒ 台账 + 进 `findings`（结构问题）；「拉不动」⇒ 只留台账 ⛔ 不进 `findings`（照①式「拉图失败不挡状态」，图是附属动作）。
    找链路与状态那条路共用 `_link_of`（含 legacy）⇒「归哪一行」不可能两个答案；另一行按 `_sibling_lands_here` 同一道判据（同店 ⇒ 照常存图、`uploaded_by` 用链路主人；启用中的②式 ⇒ 不写；不同店 / 不在处理集合 ⇒ 不写）；`now` = 引擎那一轮的墙钟；⛔ 报文里的 `name`（对端上传者）不进 `alt` / `remark` / 人话（隐私口径）。"""
    if _text_of(params, FIELD_ACTION) != ACTION_NEW_SCREENSHOT:
        return None
    order_no = _text_of(params, FIELD_ORDER_NO)
    if not order_no:
        # 没有单号就定位不到订单（⛔ 不猜、也不静默丢）：留一条痕（kind 与「报文读不出来」同一档）。
        reason = REASON_PIC_IN_NO_URL.format(detail="报文里没有 orderNo")
        _note_ref(
            binding=binding, target_ref=f"{binding.pk}:action-30-no-order",
            kind=KIND_UNREADABLE, status="failed", reason=reason, payload={"action": "30"},
        )
        findings.append(reason)
        return jidanpu_openapi_consume.OUTCOME_NOTED

    source_order_no = _text_of(params, FIELD_SOURCE_ORDER_NO)
    link, raw, owner, why = _link_of(binding, order_no, source_order_no, legacy=legacy)
    if link is None and raw is None and not why:
        # ⭐ **自愈**（与 `_member_landed` **同一处 helper**，⛔ 不抄第二份）：判据与边界（只补空 / 冲突优先 / 多张不猜 / 查范围）全住在那个函数里，本处只决定什么时候试、补上了怎么往下走。
        # ⚠️ 判据与状态那条路逐字相同（`link is None and raw is None`）：`raw` 非空 = 有链路但不进处理集合 ⇒ 那格已有号，没什么可补。
        healed, healed_owner, healed_outcome = _heal_missing_remote_id(
            binding=binding, order_no=order_no,
            source_order_no=source_order_no,
            legacy=legacy, findings=findings,
        )
        if healed_outcome is not None:
            # 自愈**补不上**（已有别的号 / 多张单）⇒ 那个函数已按既有那两档的纪律留过痕，
            # 这一条回调到此为止（⛔ 不覆盖、⛔ 不拿别人的号去存这张单的图）。
            return healed_outcome
        if healed is not None:
            # ⭐ 补上了 ⇒ **照常往下走**（⛔ 不是「补完就 return」）：截图照样入库。
            link, owner = healed, healed_owner
    #: ⭐ 应用身份（与状态那条路**同一道判据** `_sibling_lands_here`，业务方 B 档）：
    #: 同一家店、而那一行不消费回调（①式 / 已停用）⇒ 由本行**照常存图**，图落在**它的单**上
    #: （`binding=identity` 只影响 `uploaded_by` 与台账的归属，`order` 本来就取自那条链路）。
    identity = None
    if owner is not None and owner.pk != binding.pk:
        if not _sibling_lands_here(binding=binding, owner=owner, link=link):
            # 这一档本行一个字都不写：① 那一行自己那一轮会办（启用中的②式）；
            # ② 不是同一家店（跨店不代写）。两档的处置与状态那条路**逐字相同**。
            return jidanpu_openapi_consume.OUTCOME_SKIPPED
        identity = owner
    if link is None:
        if raw is not None:
            # 有链路但按判据①不进处理集合（已终态 / 已软删）⇒ ⛔ 不为它存图（省一次下载）。
            _note_order_ref(
                binding=binding, order=raw.order, kind=KIND_STATUS_IN, status="skipped",
                reason=why,
            )
            return jidanpu_openapi_consume.OUTCOME_SKIPPED
        # 找不到链路 ⇒ 与状态那条路**同一档**（老地址：只留痕不进错误列；新路径：要人看）。
        return _note_unknown_order(
            binding=binding, order_no=order_no,
            source_order_no=source_order_no,
            legacy=legacy, findings=findings, reason=why,
        )
    order = link.order
    # ⚠️ 下面的台账与 `uploaded_by` 一律用**应用身份**（`identity or binding`）：
    #    跨绑定那一档的图要落在**那条链路自己的主人**名下（业务方 B 档）。
    actor = identity if identity is not None else binding
    img_url = _text_of(params, FIELD_IMG_URL)
    if not img_url:
        reason = REASON_PIC_IN_NO_URL.format(detail="报文里没有 imgUrl（或它是空的）")
        _note_order_ref(
            binding=actor, order=order, kind=KIND_UNREADABLE, status="failed", reason=reason,
        )
        # ⭐ 「配不上」那一档 ⇒ 进错误列（截图会**静默停住**正是业务方报的那个形态）。
        findings.append(reason)
        return jidanpu_openapi_consume.OUTCOME_NOTED
    outcome, failure = _store_callback_picture(
        binding=actor, source_binding=binding, link=link, img_url=img_url,
        note=_text_of(params, FIELD_NOTE), now=now,
        received_at=jidanpu_openapi_consume._event_time(row),
    )
    if failure is not None:
        _note_order_ref(
            binding=actor, order=order, kind=KIND_STATUS_IN, status="failed", reason=failure,
        )
    return outcome


def _note_unknown_order(*, binding, order_no, source_order_no, legacy: bool,
                        findings: list[str], reason="") -> str:
    """「按单号**找不到链路**」那一档的留痕（状态路与 `action=30` 路**共用这一份**）。两块分开写必然漂：`action=30` ⛔ 不该查权威状态、不能整段复用 `_member_landed`，但「留哪个 kind / 进不进错误列」必须逐字相同。两档处置（⛔ 不许合并）：**老地址**（报文没有码 ⇒ 找不到就是「不是我们的单」，常态）⇒ `KIND_LEGACY_UNKNOWN_ORDER` + `skipped` ⛔ 不进 `findings`（盖进错误列就是让健康列常年说假话）；**新路径** ⇒ `KIND_UNKNOWN_ORDER` + `failed` + 进 `findings`（码是我们发的，找不到链路要人看）。老地址 `payload` 多一格 `route`。
    """
    if legacy:
        reason = reason or REASON_LEGACY_UNKNOWN_ORDER.format(
            order_no=order_no, source_order_no=source_order_no or "（空）",
        )
        _note_ref(
            binding=binding, target_ref=f"{binding.pk}:{order_no[:120]}",
            kind=KIND_LEGACY_UNKNOWN_ORDER, status="skipped", reason=reason,
            payload={"jidanpu_order_id": order_no, "source_order_no": source_order_no,
                     "route": "legacy-notify"},
        )
        return jidanpu_openapi_consume.OUTCOME_SKIPPED
    reason = reason or REASON_UNKNOWN_ORDER.format(
        order_no=order_no, source_order_no=source_order_no or "（空）",
    )
    _note_ref(
        binding=binding, target_ref=f"{binding.pk}:{order_no[:120]}",
        kind=KIND_UNKNOWN_ORDER, status="failed", reason=reason,
        payload={"jidanpu_order_id": order_no, "source_order_no": source_order_no},
    )
    findings.append(reason)
    return jidanpu_openapi_consume.OUTCOME_NOTED


def _store_callback_picture(*, binding, source_binding, link, img_url: str, note: str, now, received_at):
    from apps.business import jidanpu_upstream_pics

    order = link.order
    guard_outcome = None
    with ExitStack() as locks:
        def download_then_guard(src):
            nonlocal guard_outcome
            data = jidanpu_upstream_pics.download_pic(src)
            # 下载期间允许撤回。下载后持锁复核，直到存储及排补推完成。
            busy = locks.enter_context(jidanpu_assignment_lock.sending(
                binding_id=binding.pk, order_id=order.pk, source_binding_id=source_binding.pk,
            ))
            if busy:
                guard_outcome = jidanpu_openapi_consume.OUTCOME_DEFERRED
            elif (
                not jidanpu_member._binding_current_rows(source_binding).filter(enabled=True).exists()
                or not jidanpu_member._active_link_current(binding, link)
                or not jidanpu_member.pullable_links(binding).filter(pk=link.pk).exists()
                or jidanpu_relaunch.confirmed_withdrawal_since(order.order_no, binding, received_at)
            ):
                guard_outcome = jidanpu_openapi_consume.OUTCOME_SKIPPED
            # 空结果让公共存图流程跳过这张图，真实处置由回调消费方返回。
            return data if guard_outcome is None else b""

        try:
            rows = ({"url": img_url, "note": note},)
            outcome = jidanpu_upstream_pics.store_remote_pics(
                jidanpu_upstream_pics.remote_pics_from_openapi_rows(rows),
                order=order, uploaded_by=order.sales_user or binding.user, now=now,
                download=download_then_guard,
                identity=jidanpu_upstream_pics.remote_object_key,
                img_types=jidanpu_member.MEMBER_PULL_IMG_TYPES,
                skip_remote_keys=jidanpu_member._pushed_remote_keys(order, binding),
            )
            reason = outcome.reason
        except Exception as exc:  # noqa: BLE001 —— 图片失败只留台账。
            reason = type(exc).__name__
    if guard_outcome is not None:
        return guard_outcome, None
    if reason:
        return jidanpu_openapi_consume.OUTCOME_NOTED, REASON_PIC_IN_FAILED.format(reason=reason)
    return jidanpu_openapi_consume.OUTCOME_APPLIED, None


def _member_apply(*, order, link, binding, order_no, queries, now, findings: list[str],
                  trigger: str) -> str:
    """**兜底对账**那一侧的「查 → 判 → 写」（引擎逐单调它；见 `CallbackLine.apply`）。
    ⭐ 与回调那一条路**共用** `_apply_member_status`（⛔ 不许各写一份）。"""
    return _apply_member_status(
        binding=binding, order=order, link=link, order_no=order_no, queries=queries,
        now=now, findings=findings, trigger=trigger,
    )


def _member_candidates(binding):
    """兜底对账的候选：本绑定名下**处理集合里**的链路（判据①唯一出处 = `pullable_links`）。为什么是这条判据（而不是照抄上家 `terminated_at IS NULL` + `CLOSED_STATUSES`）：下家链路表没有「停止跟踪」列，下家线的闭态枚举是 `PULL_TERMINAL_STATUSES`——上家那套是**另一条线**的枚举，套过来会让候选集与慢路处理集合不一致（两套判据）。排序固定按主键升序（窗口轮转靠它稳定，⛔ 不按时间 / 状态）；⭐ 回 QuerySet ⛔ 不是 list：引擎要 `.count()` + `LIMIT/OFFSET` 切窗口，全量材料化正是要修的停滞（判据本来就在 SQL 里，切片不会与过滤错位）。
    """
    return jidanpu_member.pullable_links(binding).select_related("order").order_by("pk")


def _member_remote_order_id(link) -> str:
    """下家链路上的「对方的单号」= `jidanpu_order_id`（上家那张表叫 `upstream_order_id`）。"""
    return link.jidanpu_order_id


#: ⭐ **下家线**（`JidanpuMemberBinding`）在引擎里的差异面（九个字段，语义见 `CallbackLine`）。
#: ⚠️ 它**不含任何业务判据**：判据全在 `jidanpu_member` 那两个既有函数里
#: （`pullable_links` 与 `apply_remote_completion`）。
MEMBER_LINE = jidanpu_openapi_consume.CallbackLine(
    name="member",
    kinds=jidanpu_openapi_consume.LineKinds(
        sign=KIND_SIGN_REJECTED,
        unreadable=KIND_UNREADABLE,
        unclaimed=KIND_UNCLAIMED_CODE,
        other_action=KIND_OTHER_ACTION,
        foreign_path=KIND_FOREIGN_PATH,
        stale_skipped=KIND_STALE_SKIPPED,
        reconcile_cap=KIND_RECONCILE_CAP,
        reconcile_stuck=KIND_RECONCILE_STUCK,
    ),
    binding_model=JidanpuMemberBinding,
    payload_key="member_binding_id",
    remote_order_id=_member_remote_order_id,
    note_event=_note_ref,
    landed=_member_landed,
    apply=_member_apply,
    candidates=_member_candidates,
    #: ⭐ **老地址那条例外**（`/jidanpu/v1/notify`；默认关着，只有环境变量点名的那**一个**租户
    #: 才接管 —— 边界见模块「二·五」那一节）。
    legacy_notify=legacy_notify_route_enabled,
    #: ⭐ `action=30`（订单有新截图）由本线自己办（报文里带着 `imgUrl` ⇒ 直接入库）；
    #: 别的 action 回 `None` ⇒ 落回老口径那条痕（⛔ 上家线不设这个钩子）。
    other_action=_member_other_action,
)


# ══ 五、两个入口（调度层只调它们；⛔ 不自己去拼引擎参数）══════════════════════════


def consume_member_callbacks(binding, *, now=None, events_path=None, token_blocked="",
                             max_orders: int | None = None):
    """跑一轮**下家②式**的回调消费（引擎是 `jidanpu_openapi_consume.consume_callbacks`）。
    ⚠️ 本函数**只是把 `MEMBER_LINE` 绑上去**（⛔ 没有第二份循环）：
    顺序 / 水位 / 分流 / 留痕口径全在那个引擎里，两条线共用一份。"""
    return jidanpu_openapi_consume.consume_callbacks(
        binding, now=now, events_path=events_path, token_blocked=token_blocked,
        max_orders=max_orders, line=MEMBER_LINE,
    )


def reconcile_member_statuses(binding, *, now=None, token_blocked="", max_orders: int | None = None):
    """跑一轮**下家②式**的兜底对账（引擎是 `jidanpu_openapi_reconcile.reconcile_openapi_statuses`）。
    ⚠️ 闸门（`reconcile_seconds`，本表默认 **180**）在**调度层**（`jidanpu_sync`），
    ⛔ 本函数不加闸（照上家线那条同一形状）。"""
    return jidanpu_openapi_reconcile.reconcile_openapi_statuses(
        binding, now=now, token_blocked=token_blocked, max_orders=max_orders, line=MEMBER_LINE,
    )


def member_consumer_state(binding, *, now=None) -> jidanpu_openapi_health.ConsumerState:
    """§9.4 第 2 问的那**一句人话**：这台机器上有没有东西在消费这家下家的回调。判据与阈值**只有一处**（`jidanpu_openapi_health.consumer_state`），本函数只把它绑到「这一行是下家绑定」⇒ 窗口取本表 `reconcile_seconds`（默认 180）⇒ `max(900, 10 × 180)` = **1800** 秒（上家那一边 6000 秒）。纯读：不落库、不打上游、不写日志。
    """
    return jidanpu_openapi_health.consumer_state(
        binding, now=now or django_timezone.now()
    )


__all__ = [
    "ENV_LEGACY_CALLBACK_TENANT",
    "KIND_FOREIGN_PATH",
    "KIND_HEAL_BLOCKED",
    "KIND_HEALED",
    "KIND_LEGACY_UNKNOWN_ORDER",
    "KIND_OTHER_ACTION",
    "KIND_QUERY_FAILED",
    "KIND_RECONCILE_CAP",
    "KIND_RECONCILE_STUCK",
    "KIND_SIGN_REJECTED",
    "KIND_STALE_SKIPPED",
    "KIND_STATUS_IN",
    "KIND_UNCLAIMED_CODE",
    "KIND_UNKNOWN_ORDER",
    "KIND_UNKNOWN_STATUS",
    "KIND_UNREADABLE",
    "MEMBER_LINE",
    "REASON_GROUP_NOT_CONSUMING",
    "REASON_LEGACY_UNKNOWN_ORDER",
    "REASON_UNKNOWN_ORDER",
    "consume_member_callbacks",
    "legacy_notify_route_enabled",
    "member_all_bindings",
    "member_consumer_state",
    "member_group_bindings",
    "reconcile_member_statuses",
]
