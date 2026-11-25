"""鸡蛋铺官方 API 回调的**消费者**（把接收器落盘的 JSONL 事件真正用起来；spec：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §5.4（接收器→消费者边界）/ §5.5（跑在哪、水位与幂等键）/ §6（状态映射与未识别口径）/ §13.4 的 E1–E3 / E8）。⭐ **两条线共用这一个引擎**（读文件 / 分流 / 水位 / 逐条 ①–④ 只有这一份）：「上家鸡蛋铺②式」（`JidanpuUpstreamBinding`）与「下家鸡蛋铺②式」（`JidanpuMemberBinding`，见 `jidanpu_member_openapi.py`）各自提供一份 `CallbackLine`（差异全在那九格 + 两个可选钩子里，见「二·五」）；⭐ **为什么必须共用**：水位那套口径（严格大于水位才算 pending / 中途停下推到「第一条没处理的」前一微秒 / 一条都没处理成则不动 / 文件被搬动过则清零）与「配不上的每一档都要留痕」是**同一条**口径——两处各写一份必然漂，漂了的症状是**静默丢事件**；⚠️ **上家那条线的行为逐字不变**（`upstream_line()` 那份 `CallbackLine` 的九个字段各自指向本模块加 `line` 之前那些语句，它那 1300 行用例照旧是判据），两个默认关着的可选钩子（`legacy_notify` / `other_action`）上家线**都不设**。
接收器（`apps/callback/wsgi.py`，独立 WSGI 进程）只做**原样落盘 + 验签 + 回 200**（⛔ 不碰数据库、不认识租户）；本模块是**另一个进程**（常驻同步 `jidanpu_sync --loop`）里的一层：读那个文件 → 挑出属于本绑定的事件 → **主动查一次权威状态** → 才决定要不要改我方订单（读 ⛔ 不写、不改已落盘行——那些行是取证）。五条边界（spec §5.4 的 B1–B5）：① 只读那个文件，「处理到哪了」是**消费者**的状态 ⇒ 水位存在**数据库**（`binding.callback_watermark_at`，B2）；② ⛔ **不读密钥**（B3）：比对的是**明文码**（码不是密钥），一次都不读 `callback-keys.env`；③ ⭐ **改订单之前必须先查权威状态**（B4）：回调会漏（发版重启那段确定会漏）、会重复（实测 0.5%）⇒ 「收到回调」≠「数据到了」，查失败 ⇒ **一个字都不写**（判据见 E3）；④ 只处理本绑定名下有链路的订单（B5）；⑤ **写入口只有一处**：`apply_transition`（+ 既有审计动作码 `order.transition`），与登录式上家线 `jidanpu_upstream._pull_row` 的写面**逐字同形**（同一个 `nodes_of(binding.user)` 身份口径、同一个 `actor_id=None` 的机器行）。⭐ **水位用我方 `received_at`**（接收器落盘的墙钟），⛔ **不是**回调报文里的 `timestamp`——实测那个 `timestamp` **偏 +8 小时**（F19），拿它当水位会把新事件永远排在「未来」⇒ 那个字段一个字节都不参与任何判断；幂等是**三层**（水位 ⇒ 状态值比对 ⇒ 台账四元组），⛔ **不只靠水位**（重复送达实测 0.5%，且「文件被搬走 ⇒ 水位清零 ⇒ 整份重读」的兜底路径会主动重读一遍）。
⚠️ **水位那条口径的已知边界**：`pending` 判据**严格** `> watermark` ⇒ 「`received_at` 没排在水位之后、却晚于水位才落地」的乱序事件（钟被 NTP 回拨 / 将来的双写进程）永远进不了 `pending`、**一个字都不改订单、台账里也一行都没有**（= 静默丢；全文件最大 `received_at` 那条 `rolled_back` 兜底罩不住本绑定的一条旧插行）⇒ 本线只做一件事：把它变成「看得见的跳过」（台账 `openapi.callback_stale_skipped`，`reason` **逐字**带那一条的 `received_at` 与当时水位，进 `findings` ⇒ 绑定 `last_error`；⛔ 水位算法一个字都没改；判据取「进不了 pending 的那一条」且只对**增量读之后的新尾巴**留痕，不给已处理过的历史前缀灌台账）。⭐ **增量读**：每轮只从已消费字节偏移（`callback_file_offset`，`BigIntegerField`，与水位**同一次 UPDATE** 落库）往后读——修的生产性能缺陷（kexing 现网 `events.jsonl` 已 198MB / 十几万行，旧写法每轮从头全扫 ⇒ 进程跑很久、水位几乎不前进）；读口是 `apps.callback.store.read_records_from`（半行 / 坏行 / 中途停下 ⇒ 偏移停在那一行之前，下一轮重读，⛔ 不跳过 = 不静默丢）；首轮大文件（偏移 0、水位非空、> 32MB）只读末尾 16MB 并留人话；文件变短 / mtime 倒退 / **偏移比高水位还长** ⇒ 水位与偏移**一起**清零（「变短」只认 `snapshot().high_water_bytes` 累计写入字节：轮转与保留期清理都不会让它变小，⛔ 不许换成 `total_bytes`）。⭐ **保留期清理**：已消费 + 超 24 小时的归档段由**消费侧**每轮推进偏移之后删（三条闸在 `callback_store.prune_segments` + `_prune_floor` 本机下限；⛔ 不在接收器的写热路径上；删失败只 warning）。⚠️ **不是本线路径的行也要看得见**：接收器的路径规则一变 = 本绑定整批静默消失 ⇒ 留 `openapi.callback_foreign_path`（按形状去重，⛔ 不是每行 / 每轮一条；⭐ **两档分开**：旧路径 `/jidanpu/v1/notify` = ①式登录线的**正常流量** ⇒ `skipped`、⛔ **不进** `last_error`；形状不对那一档才 `failed` 进——并成一档就把真信号一起摘掉）。⭐ **配不上的每一档都有自己的台账 kind**（完整表格外移 `docs/code-comment-notes.md`；`JidanpuSyncEvent` 页面看得到，`note_once` 四元组去重 ⇒ 同一件事不会每轮灌一条；⛔ 一律不许静默；`callback_count` 不是留痕出口）。⭐ **老地址那条例外**（`legacy_notify` 钩子）**只对一台机器 / 一个租户成立**（kexing 上那条老回调线已收 6000+ 条真回调且路径里没有码），⛔ **不许推广成通用规则**——「老地址 → 哪个租户」是**机器级**事实，判据与环境变量口径在 `jidanpu_member_openapi` 那一侧（⛔ 引擎不认识租户 / 机器；找不到单号只留台账、⛔ 不进错误列——「不是我们的单」是常态不是故障）。⛔ 本模块**不**做什么：不打任何写接口（只调只读的 `GetOrderStatus` / `GetOrderProcessImgs`）；不做兜底对账（T8 在 `jidanpu_openapi_reconcile.py`，但那一轮**复用**本模块的 `apply_authoritative_status` 与 `status_queries` ⛔ 两条路不许各写一份）；不做健康列展示、不碰前端；上家线不做图片类事件的按对象 key 去重（`other_action` 钩子只有下家线设）；不改接收器那条线的任何东西。"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.db import connection, transaction
from django.db.models import F
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_datetime

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu_openapi, jidanpu_upstream
from apps.business.models import (
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
)
from apps.business.order_status import IllegalTransition
from apps.callback import config as callback_config
from apps.callback import store as callback_store
from apps.callback import wsgi as callback_wsgi
from apps.core.datetime_display import local_iso
from apps.rbac.permissions import nodes_of

logger = logging.getLogger(__name__)

# ══ 一、常量：报文字段名与记录字段名（⛔ 逐字，两处都有权威出处）════════════════
# 回调报文的七个字段名：第三方文档口径，抄自接收器线 spec §2（`orderNo` / `sourceOrderNo` /
# `status` / `note` / `action` / `timestamp` / `sign`）。
FIELD_ACTION = "action"
FIELD_ORDER_NO = "orderNo"
FIELD_SOURCE_ORDER_NO = "sourceOrderNo"
FIELD_STATUS = "status"
FIELD_TIMESTAMP = "timestamp"

# 落盘记录的字段名：**接收器 `_new_record` 的形状**（`apps/callback/wsgi.py`，⛔ 不是转述）。
# ⚠️ 这里只列本模块真正读的那几个；记录里还有 `headers` / `peer_addr` 等（那是取证）。
FIELD_RECEIVED_AT = "received_at"
FIELD_PATH = "path"
FIELD_SIGN_OK = "sign_ok"
FIELD_SIGN_REASON = "sign_reason"
FIELD_JSON_OK = "json_ok"
FIELD_BODY_B64 = "body_b64"
FIELD_BODY_SHA256 = "body_sha256"
FIELD_BODY_STORED = "body_stored"
FIELD_SOURCE_IP = "source_ip"

#: ⭐ 第一期只消费这一种通知（spec §5.4 / §6.5）：`10` = 订单状态发生改变。
#: ⚠️ 别的 action **不读业务含义**（账单 40/50 连读都不读，spec §6.5 明写「消费者不读」）
#: ⇒ 走「只登记、不改单」那一档（⛔ 不是静默丢：台账要留一条）。
ACTION_STATUS_CHANGED = "10"

# ══ 二、留痕出口（台账 kind + 人话；⛔ 全部 ≤ 32 / ≤ 255，列宽就是判据）════════
#: 本线**订单维度**的台账 kind（与登录式上家线的 `upstream.status_in` 同形，⛔ 不复用它的名字：
#: 两条线的证据面不同，混用会让「这条台账是谁写的」查不出来）。
KIND_STATUS_IN = "openapi.status_in"
#: 没通过验签（⛔ 一个字节都不改订单）。
KIND_SIGN_REJECTED = "openapi.callback_unsigned"
#: 报文读不出来（没落原文 / 不是 JSON / 缺 `orderNo`）。
KIND_UNREADABLE = "openapi.callback_unreadable"
#: 回调码没有**启用中的②式绑定**认领（谁都不消费这一条）。
KIND_UNCLAIMED_CODE = "openapi.callback_unclaimed_code"
#: 本绑定名下没有这条链路 ⇒ 找不到我方订单。
KIND_UNKNOWN_ORDER = "openapi.callback_unknown_order"
#: 链路已停止跟踪 / 我方订单已软删（⛔ 也不为它打上游）。
KIND_NOT_TRACKED = "openapi.callback_not_tracked"
#: 不是 `action=10`（第一期只消费状态；图片 / 删除 / 账单各自有别的任务）。
KIND_OTHER_ACTION = "openapi.callback_other_action"
#: 查权威状态失败（⛔ 一个字都不写 + 水位不前移）。
KIND_QUERY_FAILED = "openapi.callback_query_failed"
#: ⭐ 事件文件里**不是本线路径**的行（旧路径 `/jidanpu/v1/notify` 的每一条、形状不对的行）。
#: ⛔ 本线不处理它们，留痕只为「接收器的路径规则一变 ⇒ 本绑定的回调**整批**静默消失」这件事看得见。
KIND_FOREIGN_PATH = "openapi.callback_foreign_path"
#: ⭐ 本绑定的**一条事件没排在水位之后**（乱序：接收器的钟被回拨 / 将来的双写进程）⇒
#: 「静默丢」变成「**看得见的跳过**」。⛔ 水位算法一个字都没改（见模块 docstring 那一节）。
KIND_STALE_SKIPPED = "openapi.callback_stale_skipped"
#: ⭐ **反复失败的同一条事件**的计次台账：一行一条事件、`attempts` 复用 `JidanpuSyncEvent.attempts`（零迁移），计满 `CALLBACK_MAX_ATTEMPTS` 后把这一行改成「已放弃并推进水位」的人话。
#: ⚠️ 只给**确认失败**计次；撞本轮上限的事件⛔ 不算（见 `_StatusQueries.cap_hit`）。
KIND_CALLBACK_RETRY = "openapi.callback_retry"

#: ⭐ 回调事件反复失败的重试上限：按**事件**计次（不是按轮、也不是按 kind），
#: 到这个数 ⇒ 放弃该事件并让水位越过它（⛔ 不再每轮卡住水位）。
CALLBACK_MAX_ATTEMPTS = 5
#: 回调事件一次确认失败的人话（`n` = 第几次；`detail` = 那一轮真实原因）。
CALLBACK_ATTEMPT_REASON = "回调事件处理失败（第 {n} 次，下一轮重试）：{detail}"
#: 到达上限后写进同一条计次台账的人话（**逐字**带「已放弃并推进水位」两个事实）。
CALLBACK_ABANDONED_REASON = "重试 {n} 次仍失败，已放弃并推进水位：{detail}"

#: 台账 / `last_error` 里人话的长度上限（列宽：`JidanpuSyncEvent.reason` 是 255）。
REASON_MAX = 255

REASON_SIGN_REJECTED = (
    "这条回调**没通过验签**（sign_reason={sign_reason}，来源 {source_ip}）⇒ ⛔ 一个字都不改订单，只留痕"
)
REASON_UNREADABLE = "这条回调的报文读不出来（{detail}）⇒ ⛔ 什么都不改，只留痕"
REASON_UNCLAIMED_CODE = (
    "事件文件里的回调码「{code}」没有任何**启用中的②式绑定**认领（绑定停了 / 码被人工改过 / 码过期）"
    "⇒ 这条回调没有任何人在消费，请人工核一次"
)
REASON_UNKNOWN_ORDER = (
    "回调说上家单号 {order_no}（来源单号 {source_order_no}），但本绑定名下没有这条链路"
    "⇒ ⛔ 不建单、不改任何订单，只留痕"
)
REASON_NOT_TRACKED = "上家单号 {order_no} 对应的链路{why} ⇒ 本轮不查权威状态、不改订单，只留痕"
REASON_TERMINATED = "已停止跟踪（两边都到过终态）"
REASON_SOFT_DELETED = "的我方订单已软删（进了回收站）"
REASON_OTHER_ACTION = (
    "本线第一期只消费 action=10（订单状态改变），这一条是 action={action} ⇒ 只登记、不改订单"
)
REASON_QUERY_FAILED = "查权威状态失败（{detail}）⇒ 这一条回调**一个字都不写**，且水位不前移（下一轮重试）"
REASON_APPLIED = (
    "官方API 的权威状态「{token}」与我方不一致，已按回调提醒回写为「{target}」"
    "（回调只当提醒：写成什么以**主动查到的权威值**为准）"
)
REASON_DENIED = "状态回写被状态机拒绝（多半是这个绑定账号缺节点）⇒ 只登记、不改状态：{detail}"

# ── ⭐ 跟退那一档（D3，L-13：②式上游遇「已退单」接既有跟退原语）──────
#: 回调那条路跟退成功的人话。
REASON_REFUND_FOLLOWED_CALLBACK = (
    "官方API 的权威状态「{token}」＝「已退单」⇒ 已**自动跟退**（状态与钱一起回退，"
    "走既有退单原语 `follow_refund`；绩效 / 钱包的回退由既有退单钩子办）"
)
#: 兜底对账那条路跟退成功的人话（⛔ 不说「按回调提醒」——这一单正是回调漏掉的那一种）。
REASON_REFUND_FOLLOWED_RECONCILE = (
    "官方API 的权威状态「{token}」＝「已退单」（**兜底对账**查得）⇒ 已**自动跟退**"
    "（状态与钱一起回退，走既有退单原语 `follow_refund`）"
)
#: 跟退被状态机拒绝那一档的人话（照 `REASON_DENIED` 的同一句式；与登录式上家线的
#: `REFUND_DENIED_REASON` 同族——⛔ 不复制它的名字，台账 kind 本来就分两条线）。
REASON_REFUND_DENIED = (
    "跟退被状态机拒绝（多半是这个绑定账号缺节点）⇒ 状态与钱都没动，只登记：{detail}"
)

# ── ⭐「这一笔是谁做的」：回调（T6）与兜底对账（T8）共用**同一段**写入面，人话不许说假话 ──
#: 回调驱动的写入（T6）。
TRIGGER_CALLBACK = "callback"
#: 兜底对账驱动的写入（T8，spec §8）。⚠️ 这一档的订单**多半压根没收到 / 没消费到回调**
#: ⇒ 那几句「按回调提醒 / 水位不前移」的人话对它**是假的**，各给一句真的。
TRIGGER_RECONCILE = "reconcile"

#: 对账那一档的同位人话（⛔ 不说「按回调提醒」：这一单就是回调漏掉的那一种）。
REASON_APPLIED_RECONCILE = (
    "官方API 的权威状态「{token}」与我方不一致，**兜底对账**已直接回写为「{target}」"
    "（这一单没有回调把它带进来，正是这一路要兜的事；写成什么仍以**主动查到的权威值**为准）"
)
#: 对账那条路上「查不动」时的人话（⛔ 不说「水位不前移」：对账那条路没有水位，停的是**窗口**）。
REASON_QUERY_FAILED_RECONCILE = (
    "查权威状态失败（{detail}）⇒ 这一单**一个字都不写**，本轮的对账窗口就此停住"
    "（下一轮从同一个窗口重试；⛔ 不静默跳过、⛔ 不拿旧值顶上）"
)
#: 对账那条路上撞上限时的人话（⚠️ 正常路径**到不了**这一格：窗口本身就是按上限切的）。
REASON_ORDER_CAP_RECONCILE = (
    "本轮对账窗口要查的订单数超过上限（{cap} 条）⇒ 只查前 {cap} 条，剩下的下一轮接着查"
    "（窗口不前移到它们之后 ⇒ ⛔ 不是「悄悄漏掉一部分」）"
)
#: 三条人话按**触发方式**取（⛔ 别在调用点手拼：同一个判断出现在两处必然漂）。
#: ⚠️ 那三张表**必须留在三个模板都定义之后**（它们在文件里分几段写，见下面的 `_APPLIED_REASONS`）。

#: 水位清零那一档的留痕（spec §5.5 的「文件被整块搬走 / 变短了」那一行）。
REASON_WATERMARK_RESET = (
    "回调事件文件比上次看到的小 / 被搬动过（或文件里最新一条比水位还旧）⇒ 本绑定的回调水位已清零，"
    "本轮不消费、下一轮把**留存下来的**事件重读一遍（重复送达靠状态幂等 + 台账四元组兜住；"
    "⚠️ 超出保留期、已被清理删掉的段⛔ 读不回来；⛔ 不静默跳过）"
)
#: 事件文件里出现**读不出来的行**（写了一半就被读到）时的留痕：⛔ 不静默跳过。
#: ⚠️ 起是**增量读**：`{line}` 是「从 `{offset}` 字节起的第几行」（不再是从文件
#: 开头数的行号 —— 那要数整份文件，正是这次要删掉的全扫）；停住的位置由 `{offset}` 给。
REASON_BROKEN_LINE = (
    "回调事件文件里从第 {offset} 字节起的第 {line} 行读不出来（多半是接收器写这一行的同时我们在读）"
    "⇒ 本轮在它之前停住，**已消费偏移不前移**（下一轮从同一位置重读这一行；⛔ 不静默跳过它后面的内容）"
)
#: ⭐ 首轮就撞上一份**很大的历史留档**（> 32MB）时的留痕：只读末尾一段、更早的按偏移跳过。
#: ⚠️ 这一条**必须**留人话（spec §9.4 的口径）：跳过历史是**有意的取舍**，⛔ 不是静默丢。
REASON_TAIL_SKIP = (
    "历史回调按偏移跳过（文件过大）：事件文件已有 {size_mb}MB、而本绑定还没有已消费偏移 "
    "⇒ 本轮只从末尾 {tail_mb}MB 开始读，更早的旧事件不再消费"
    "（水位仍在 {watermark}；要回补请把本绑定的「已消费偏移」清成 0，下一轮从**现存最早一段**"
    "重读 —— ⚠️ 超出保留期、已被清理删掉的段⛔ 读不回来）"
)
#: 撞上「一轮最多查几条」的上限时的留痕（**只推迟、不丢**）。
REASON_ORDER_CAP = (
    "本轮回调要查的订单数超过上限（{cap} 条）⇒ 只查前 {cap} 条，剩下的下一轮接着查"
    "（水位不前移到它们之后 ⇒ ⛔ 不是「悄悄漏掉一部分」）"
)
#: ⭐ 三条人话按**触发方式**取（⛔ 别在调用点手拼：同一个判断出现在两处必然漂）。
#: ⚠️ 这张表**必须留在三个模板都定义之后**（它们在文件里是分开几段写的）。
_APPLIED_REASONS: dict[str, str] = {
    TRIGGER_CALLBACK: REASON_APPLIED,
    TRIGGER_RECONCILE: REASON_APPLIED_RECONCILE,
}
#: 跟退成功那一档的人话按**触发方式**取（照 `_APPLIED_REASONS` 的同一张表形状；
#: ⛔ 别在调用点手拼——同一个判断两处必然漂）。
_REFUND_FOLLOWED_REASONS: dict[str, str] = {
    TRIGGER_CALLBACK: REASON_REFUND_FOLLOWED_CALLBACK,
    TRIGGER_RECONCILE: REASON_REFUND_FOLLOWED_RECONCILE,
}
_QUERY_FAILED_REASONS: dict[str, str] = {
    TRIGGER_CALLBACK: REASON_QUERY_FAILED,
    TRIGGER_RECONCILE: REASON_QUERY_FAILED_RECONCILE,
}
_ORDER_CAP_REASONS: dict[str, str] = {
    TRIGGER_CALLBACK: REASON_ORDER_CAP,
    TRIGGER_RECONCILE: REASON_ORDER_CAP_RECONCILE,
}
#: ⭐ 本绑定的一条事件**没排在水位之后**（乱序：钟被回拨 / 双写）⇒ 留一条「看得见的跳过」。
#: ⚠️ `received_at` 与 `watermark` 都**逐字**进人话（**这是判据**：出这种事时要能一眼看到
#: 「哪一条、当时水位在哪」）⇒ 模板别写太长，⛔ 别让 `_clip` 把这两个值截掉。
REASON_STALE_SKIPPED = (
    "⭐ 看得见的跳过：这条回调**属于本绑定**，但它的 received_at（{received_at}）"
    "没有排在水位（{watermark}）之后 ⇒ 按现有口径它进不了 pending，本轮一个字都不改订单。"
    "⚠️ 水位算法⛔ 一个字都没改（见模块 docstring「已知边界」那一节）"
)
#: ⭐ 事件文件里**不是本线路径**的行 ⇒ 按**形状**留一条台账（⛔ 不是每行 / 每轮一条）。
REASON_FOREIGN_PATH = (
    "事件文件里出现了**不是本线路径**的行（形状：{shape}；例：{sample}）⇒ 本线⛔ 不处理它"
    "（旧路径的每条都会走到这里）。留痕只为一件事：**万一接收器的路径规则变了**，"
    "本绑定的回调会整批从这一支流过去，那时台账上要看得见（⛔ 不许静默）"
)

# ── ⭐⭐ N2 兜底开关（业务方选 ②）：默认**关** ─────────────────────────
#: 关闭（默认）时，消费起点落在**已被保留期清理删掉**的范围 ⇒ 照旧**响亮抛** `OffsetOutOfRetention`（⛔ 不夹回段首、⛔ 不静默从头读；既有裁定不动）；显式打开（`1` / `true` / `yes` / `on`，大小写不敏感）后，才跳到现存最早段继续，并把丢失的 `[旧偏移, earliest_base)` 字节区间写进 `findings`（⇒ 绑定 `last_error`）与日志。
#: ⚠️ **默认必须是关**：`tests.test_jidanpu_openapi_consume` 里那条「偏移落在被删段仍响亮报错」跑的是默认配置 ⇒ 打开才能跳过，那条判据一个字都没少。
ENV_AUTO_SKIP_OUT_OF_RETENTION = "CALLBACK_AUTO_SKIP_OUT_OF_RETENTION"
#: 认作「开」的取值（其余一律当**关**：拼错 / 空串都退回安全的那一侧）。
_AUTO_SKIP_TRUTHY = frozenset({"1", "true", "yes", "on"})
#: 开关打开且真的跳过时的人话（`[旧偏移, earliest_base)` 与字节数**逐字**进句子）。
REASON_AUTO_SKIP_OUT_OF_RETENTION = (
    "回调消费偏移 {old_offset} 落在已被保留期清理删掉的范围 ⇒ 已按开关 "
    + ENV_AUTO_SKIP_OUT_OF_RETENTION
    + " 跳到现存最早段 {earliest_base}："
    "[{old_offset}, {earliest_base}) 共 {lost_bytes} 字节不再重读（这些事件已丢、不会补）"
)

#: 一轮**最多查几张订单**（去重之后的 `orderNo` 个数）。⚠️ **我们自定**（spec 没定这个数）：回调是「刚发生的事」，而常驻循环每 3 秒一拍 ⇒ 不设上限时，一次几百条的回调暴雨会让这一拍对上游打出几百次请求（spec §8.3 的代价栏记着「没量过官方 API 的限流」）。
#: ⚠️ **它只推迟、不丢**：撞上限时水位**不前移**到没处理的那几条之后，下一拍接着查；而且同一张单的多条通知（实测某单 15 条截图通知）在本轮**只查一次**（见 `_queries` 的去重）。
MAX_ORDERS_PER_ROUND = 20

#: ⭐ **首轮就撞上一份很大的历史留档**时只读末尾这么多字节（现网那个文件 198MB / 十几万行 ⇒ 首轮从头解析一遍能把这一轮拖到几分钟）。
#: ⚠️ 只在「`callback_file_offset == 0` **且** 水位非空（= 这个绑定以前消费过，只是列刚加上）**且** 文件严格大于 `FIRST_RUN_TAIL_THRESHOLD`」时生效；小文件照旧从头读（默认行为不变）。
#: ⛔ 这不改留档、不删文件、不动接收器：它只是「从哪个字节开始读」的一个选择。
FIRST_RUN_TAIL_BYTES = 16 * 1024 * 1024
FIRST_RUN_TAIL_THRESHOLD = 32 * 1024 * 1024

#: 机器动作在审计里的 `actor`（`actor_id` 一律留空：不是真人点的；与登录式上家线同一形状）。
#: ⚠️ 名字与登录式那条线**不同**：审计里要能一眼看出「这次状态是谁改的」。
MACHINE_ACTOR = "鸡蛋铺官方API回调"

#: 水位的**进程内**补充状态：`路径 → (上一次看到的**高水位**字节数, 上一次看到的 mtime, 上一轮解析成功了几条)`（高水位 = `snapshot().high_water_bytes`，⛔ 不是现存字节和：保留期清理会删掉旧归档段、让现存字节和变小，拿它当判据 = 每次清理都误判「文件被搬动」）。
#: ⚠️ 起「读到哪了」的权威是**库里的 `callback_file_offset`**（跨进程 / 跨重启）；这一份进程内记忆只剩一个职责：回答「**高水位比上次小了吗 / mtime 倒退了吗**」（纯本机观测）。为什么不把它也落库：它是**文件**级的观测（同一租户的多份绑定共用一份事件文件），而库里那一列是**绑定**级的位置；两者语义不同，合成一个必然漂。
#: ⚠️ 代价如实登记：进程重启后这一份记忆为空 ⇒ 那一轮不判「变短」；但**两条不依赖记忆的判据**（偏移比高水位还长 ⇒ 文件被换过；本轮读到的这一段全都比水位还旧 ⇒ 再核一次整份文件的最新一条）照旧生效，⛔ 别只留一条。
_FILE_STATE: dict[str, tuple[int, float | None, int | None]] = {}

#: 已经登记过「码没人认领」的那些码（键 = 租户 schema）：`note_once` 的查询**每码只做一次**，
#: 不靠一个进程内集合的话，文件里每个历史坏码都会每 3 秒打一次库（文件是只增不改的）。
_REPORTED_UNCLAIMED: dict[str, set[str]] = {}

#: 已经登记过「不是本线路径」的那些**形状**（键 = `租户 schema:绑定 id`）：旧路径的行**每条**
#: 都会走到那一支 ⇒ 不靠这个集合的话，每一行每一轮都要打一次库去问「这条记过没有」。
#: ⚠️ 键里带绑定 id：那条台账是**每份绑定各自**的一件事（`target_ref` 里有 `binding.pk`）。
_REPORTED_SHAPES: dict[str, set[str]] = {}

#: ⭐ 「清理被落后的绑定拖住」告警的**限频**记忆：键 = `(事件文件路径, floor)`，
#: 值 = 上次告警时刻。同一档 **1 小时最多一次**：否则每 3 秒一条，绑定 `last_error`
#: 会被它刷满，真故障反而看不见（复审 Important 5）。
_PRUNE_FLOOR_ALERTS: dict[tuple[str, int], datetime] = {}


def auto_skip_out_of_retention_enabled() -> bool:
    """⭐ N2 兜底开关：**默认关**，只有显式打开才返回 True（见 `ENV_AUTO_SKIP_OUT_OF_RETENTION`）。⚠️ 每次调用都重新读环境变量（⛔ 不做模块级缓存）：用例要在同一个进程里开关它，而「缓存过一次就再也不看」是这类开关最常见的静默坑。"""
    raw = (os.environ.get(ENV_AUTO_SKIP_OUT_OF_RETENTION) or "").strip().lower()
    return raw in _AUTO_SKIP_TRUTHY


def reset_process_state() -> None:
    """清空本模块的**进程内**记忆（用例之间隔离；照 `jidanpu_openapi.reset_token_cache`）。⚠️ 生产路径**不需要**它：水位在库里，这些只是「文件大小 / 已解析条数」「已登记过的坏码」「已登记过的异路径形状」与「清理告警的限频时刻」的缓存。"""
    _FILE_STATE.clear()
    _REPORTED_UNCLAIMED.clear()
    _REPORTED_SHAPES.clear()
    _PRUNE_FLOOR_ALERTS.clear()


# ══ 二·五、两条线的**差异面**（⭐ 引擎只有一份，差异只有下面这几处）══════════════
# ⚠️ 本模块的引擎（读文件 / 分流 / 水位 / 逐条 ①–③）**只有一份**；「上家鸡蛋铺②式」与「下家鸡蛋铺②式」（`JidanpuMemberBinding`）各自提供一份 `CallbackLine`；⛔ **判据与写面不许在两条路上各写一份**：一条线的 `landed` / `apply` 就是那条线「按远端单号找链路 → 查权威 → 判该不该写 → 写」的**唯一**落点。
# ⭐ **为什么必须是同一个引擎**（下家线收口）：水位那套口径（严格大于水位才算 pending / 中途停下推到「第一条没处理的」前一微秒 / 一条都没处理成则不动 / 文件被搬动过则清零）与「配不上的每一档都要留痕」是**同一条**口径，两处各写一份必然漂，而漂了之后的症状是**静默丢事件**（本仓最恨的那一形态）。


@dataclass(frozen=True)
class LineKinds:
    """一条线的台账 kind（⛔ 两条线**必须**取不同的名字：`kind` 是页面上「这条是谁写的」的判据）。⚠️ 只为**没有我方订单可指认**的那些档（验签 / 报文 / 码没人认领 / 不是状态类 action / 不是本线路径 / 乱序跳过 / 兜底对账撞上限 / 兜底对账跳过坏单）：订单维度的结论由各条线自己的 `landed` 落。"""

    sign: str
    unreadable: str
    unclaimed: str
    other_action: str
    foreign_path: str
    stale_skipped: str
    reconcile_cap: str
    #: ⭐ **兜底对账「这一张单查不动」**那一档（停滞修复，G2）。⚠️ 与 `reconcile_cap`
    #: 分开：一个是「整轮没兜全」（候选多于预算），一个是「这一张单本身坏了、已跳过」——
    #: 两种事实的处置与人话都不同，合成一个 kind 就查不出是哪一种。
    reconcile_stuck: str


@dataclass(frozen=True)
class CallbackLine:
    """一条绑定线在这个引擎里的**全部差异面**（⛔ 就这九格 + 两个**可选**钩子，别再往里加业务判据）。`name`：这条线的短名（进**进程内**游标键 ⇒ 上家 pk=1 与下家 pk=1 不许共用一格）；`kinds`：这一线的台账 kind（见 `LineKinds`）；`binding_model`：水位 / 计数写回**哪张表**（两列同名同义，只差表）；`payload_key`：台账 `payload` 里「这一行归哪份绑定」的键名（⛔ 不许混用）；`remote_order_id`：从**链路行**上取「对方的单号」（两线的列名不同）；`note_event`：**没有我方订单可指认**的那些事实的幂等写口（各线自己的既有实现）；`landed`：一条事件从「拿到 `orderNo`」到「写完 / 有结论」的**整段**（含留痕）；`apply`：**查 → 判 → 写**那一段（⭐ 兜底对账直接调它，⛔ 不许对账另写一份）；`candidates`：兜底对账的候选（各线自己那条「在途单」判据）——回 **QuerySet**（⛔ 不是 list）：引擎只 `.count()` + `LIMIT/OFFSET` 切窗口，不把在途几千条全量材料化；`legacy_notify` / `other_action`（**可选，默认 `None` = 老口径**）：见各自字段的注释——⛔ 上家线两个都不设（它的行为因此逐字不变）。"""

    name: str
    kinds: LineKinds
    binding_model: type
    payload_key: str
    remote_order_id: Callable[..., str]
    note_event: Callable[..., bool]
    landed: Callable[..., str]
    apply: Callable[..., str]
    candidates: Callable[..., object]
    #: ⭐ **老地址那条例外**（`/jidanpu/v1/notify`，见模块 docstring 那一节）：回一个「本行要不要接管老路径的行」的**无参**判据。`None`（默认）= 老口径（老路径的行一律只留痕、不越界处理）——**上家线**就是这个取值，⛔ 它的行为一个字都没变。
    #: ⚠️ 判据在**这一条线自己**那里（`jidanpu_member_openapi.legacy_notify_route_enabled`）：引擎只负责问一次、按答案分流，⛔ 不把「哪台机器 / 哪个租户」的知识写进引擎。
    legacy_notify: Callable[[], bool] | None = None
    #: ⭐ **不是 `action=10`** 的那些事件要不要由这一条线自己办（`None` = 老口径：只留一条痕）。回 `OUTCOME_*` ⇒ 引擎照它收口；回 `None` ⇒ 落回老口径（那条 `other_action` 的痕）。
    #: ⚠️ 下家线用它办 `action=30`（订单有新截图，报文里带着 `imgUrl`）；**上家线不设** ⇒ 上家线那一条路逐字不变。
    other_action: Callable[..., str | None] | None = None


# ══ 三、一轮的结论（纯数据；命令那一层照它打 stdout / 盖 last_error）════════════


@dataclass(frozen=True)
class ConsumeOutcome:
    """一轮消费者跑了什么（人话汇总 + 计数，⛔ 不含任何凭据）。`enabled`：这一行绑定是不是②式（`mode="openapi"` 且有码，`False` ⇒ 一次文件都不读）；`scanned` / `mine`：解析成功的记录条数 / 其中**属于这个回调码且在水位之后**的条数——一个回调码下可能有多行（同一个上家域名的多个打手共用一个码）⇒ 这一格是「这一轮要过一遍的事件数」，⛔ 不是「都是本行自己的单」（谁的单归谁办见 `line.landed`）；⚠️ **起 `scanned` 是「本轮真的读到并解析成功的条数」**（从已消费偏移往后那一段），⛔ 不是整份文件的条数——稳态下一轮只有新追加的那几条、甚至 0；`reset` 那一档（文件变短 / 被换过）现在**先判后读、连文件都不读** ⇒ `scanned=0`（旧写法是那份新文件的长度，没有判据依赖它）。
    `applied` / `skipped` / `failed`：写了状态 / 有结论但没写 / 留下失败留痕（查权威失败、认不出来的状态、状态机拒绝）的条数；`unclaimed`：文件里**没有任何启用②式绑定认领**的码的条数；`reset`：本轮判出「文件被搬动过」并**清零了水位**（此时 `mine` 恒为 0：本轮不消费）；`findings`：要人能看见的几句人话（台账另有行；命令那一层把它们盖进 `last_error`）。
    ⭐ `blocked_reason`：**这一轮没跑**的人话原因（spec §9.5 ②：凭据刚被①档拒过 ⇒ 本轮的取数整体不打了；空串 = 正常跑了，`enabled=False` 那一档**不是**它——那是「这行不走②式」）；⭐ `token_rejected`：**这一段的取 token 那一跳被拒过吗**（spec §9.5 ②，T12；任一档：①②③）——调度层靠它让**同一轮里的后一段**也不再打那个必被拒 / 必失败的接口（⛔ 不是靠退避窗口：③档不进窗口，而「同一轮里连打两次」本身就是 §9.5 ③ 禁止的形态）。"""

    enabled: bool = False
    scanned: int = 0
    mine: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    unclaimed: int = 0
    reset: bool = False
    watermark: datetime | None = None
    findings: tuple[str, ...] = ()
    #: ⭐ **这一轮没跑**的人话原因（spec §9.5 ②：凭据刚被①档拒过 ⇒ 本轮的取数整体不打了）。
    #: 空串 = 正常跑了（`enabled=False` 那一档**不是**它：那是「这行不走②式」）。
    blocked_reason: str = ""
    #: ⭐ **这一段的取 token 那一跳被拒过吗**（spec §9.5 ②，T12；任一档：①②③）。
    #: 调度层靠它让**同一轮里的后一段**也不再打那个必被拒 / 必失败的接口
    #: （⛔ 不是靠退避窗口：③档不进窗口，而「同一轮里连打两次」本身就是 §9.5 ③ 禁止的形态）。
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
            return "回调 本轮未跑（凭据刚被拒，退避窗口内）"
        if self.reset:
            return "回调 水位已清零（事件文件被搬动过，下一轮整份重读）"
        head = f"回调 新 {self.mine}"
        if self.mine:
            head += f"/写 {self.applied}/跳过 {self.skipped}/失败 {self.failed}"
        if self.unclaimed:
            head += f"· 没人认领的码 {self.unclaimed} 条"
        return head


# ══ 四、事件文件的读（⛔ 只读；形状与切行口径都复用接收器那一侧）════════════════


@dataclass(frozen=True)
class _Scan:
    """一次**增量**读的结果（⭐：按字节偏移，⛔ 不再每轮把整份文件读进内存）。`records`：**解析成功的前缀**（遇到第一行读不出来就停）；`new_offset`：最后一条**完整**行的末尾字节位置（绝对偏移；半行 / 坏行 ⇒ 停在那一行之前，下一轮从同一位置重读）；`broken_line`：那一行在**本段**里的 1-based 序号（`None` = 这一段都读得出来）；`line_ends`：与 `records` 一一对应的「那一行末尾」绝对字节位置（中途停下时要把偏移停回**第一条没处理的记录之前**，⛔ 不能推到段尾）；`start_offset`：这一段**实际开始读**的位置（`skip_partial` 之后的行首）。"""

    records: tuple[dict, ...]
    new_offset: int = 0
    broken_line: int | None = None
    line_ends: tuple[int, ...] = ()
    start_offset: int = 0


def _scan(path: Path, *, offset: int = 0, skip_partial: bool = False) -> _Scan:
    """从字节偏移 `offset` 起逐行读 + 解析（实现只有一处：`callback_store.read_records_from`）。⚠️ **为什么停住而不是跳过坏行**：这个文件是**追加写**的，唯一会出现「读不出来的行」的现实情形就是「接收器正在写这一行、我们读到了半行」⇒ 它后面还没有内容；跳过它继续往后读，在「文件被整块换成一份旧副本」那种形态下会把中间一段坏行**静默吞掉**。
    ⚠️ 空行容掉（正常不会有；写坏了也不会产生空行 ⇒ 它不是「坏行」那一档）；`skip_partial=True` 只给「首轮大文件从末尾一段起读」那一支（起点落在某行中间）。"""
    chunk = callback_store.read_records_from(path, offset=offset, skip_partial=skip_partial)
    return _Scan(
        chunk.records, chunk.new_offset, chunk.broken_line, chunk.line_ends, chunk.start_offset,
    )


def _prune_floor(consumed_offset: int) -> tuple[int, list[tuple[str, int, int]]]:
    """保留期清理的**安全下限**：本机所有启用②式绑定的 `callback_file_offset` 最小值。返回 `(floor, holders)`，`holders` = 卡在 `floor` 上的那些绑定（`[(线名, pk, offset), …]`，供告警**点名**「谁在拖后腿」）；一份启用的都没有 ⇒ `(consumed_offset, [])`（不额外保护）。⚠️ **为什么需要下限**（R3-M2 / 方案 a）：事件文件是**两条线共用**的一份机器级文件（`callback.config.events_path()` 里没有租户维度），而 `prune_segments` 的三条闸只看**触发清理这一份绑定**的偏移 ⇒ 只拿它自己的偏移去删段，会把**别的绑定还要读**的段删掉，那一份下一轮就撞 `OffsetOutOfRetention` 永久卡死 ⇒ 下限取「本机**所有启用②式绑定**（两条线两张表）偏移的最小值」，清理只删到它为止。
    ⚠️ **判据与 `_claimed_codes()` 逐字同形**（`mode == openapi` 且 `enabled=True` 且码非空）：停用的绑定**不会**被调度遍历 ⇒ 它不在下限里（复启后若偏移落在被删段，照旧**响亮报错**，由既有裁定与用例钉住）；没有码的绑定也不会消费事件文件，不该拖住清理；⛔ **不把①式算进来**：①式不读事件文件、偏移恒 0 ⇒ 下限永远是 0、清理永久停摆。**不进下限的那一份**（写清理由，⛔ 别为了「看起来完整」加进来）：`management/commands/dailiantong_sync.py` 的 `_consume_callbacks` 用**自己的偏移文件**读同一份事件文件、只读**现役文件**里的完整行（偏移 > 文件长度 ⇒ 回 0 重读）、**不读归档段** ⇒ 它永远不会撞 `OffsetOutOfRetention` ⇒ 既可以、也应该排除在下限外（它也没有 `callback_file_offset` 列）。
    ⭐⭐ **承重前提：一台机只服务一个租户**（D-0137「一租户一套部署」）。下限只能查**本机当前这一个租户库**里的两张表，而事件文件是**机器级**的；今天两台在役租户机各跑一个租户 ⇒ 前提成立。⚠️ 若同一台机跑多个租户（`jidanpu_sync` 的默认遍历**允许**这种形态），**别的租户库里的启用②式绑定不在下限里** ⇒ 清理会把它们还要读的段删掉；那时必须改成「按机器汇总所有租户库的下限」或「每租户一份事件文件」，⛔ 不许直接沿用本函数。"""
    rows: list[tuple[str, int, int]] = []
    for line_name, model in (
        ("upstream", JidanpuUpstreamBinding),
        ("member", JidanpuMemberBinding),
    ):
        queryset = (
            model.objects.filter(mode=jidanpu_openapi.OPENAPI_MODE, enabled=True)
            .exclude(callback_code="")
            .values_list("pk", "callback_file_offset")
        )
        rows.extend((line_name, pk, int(offset or 0)) for pk, offset in queryset)
    if not rows:
        return consumed_offset, []
    floor = min(offset for _line, _pk, offset in rows)
    holders = [(line_name, pk, offset) for line_name, pk, offset in rows if offset == floor]
    return floor, holders


def _prune_consumed_segments(path: Path, consumed_offset: int, *, now=None) -> list[str]:
    """⭐ 保留期清理：删掉**已完全消费 + 超过保留期**的归档段。**只在消费侧、每轮推进完偏移之后调**（⛔ 不在接收器的写热路径上）；判据与三条闸全在 `callback_store.prune_segments`（已消费 + 超期 + 最后一段不删），这里⛔ 不另写一份判据；⭐ **安全下限**（N2 / R3-M2）：真正传给 `prune_segments` 的是 `min(consumed_offset, floor)`，`floor` = 本机所有启用②式绑定偏移的最小值（见 `_prune_floor`）⇒ 不删**别的绑定还要读**的段。
    ⭐ **新的静默失败要有人看见**：`floor < consumed_offset` = 本绑定已消费到更后面、却被别人拖住 ⇒ 同批一条**限频**（同一档 1 小时一次）的 warning + finding（⇒ 绑定 `last_error`，页面上看得见），并**点名**拖住下限的绑定；⚠️ **删失败只 warning、⛔ 不抛**：这一轮的消费已经做完了（水位 / 偏移都写完了），让一次清理失败把整轮弄挂的代价（水位不前进、`last_error` 被盖）比「下一轮再删」贵得多——「删失败」与「被下限拖住」是两件事（一个异常、一个是**设计上的等待**），⛔ 别混成同一句话。
    回 `findings`（要人看的那几句）；`[]` = 这轮没有要人看的事。"""
    findings: list[str] = []
    try:
        #: 廉价早退：只有一个现役文件（没有归档段）⇒ 连锁都不用拿，更不用每 3 秒查两张表。
        if len(callback_store.segment_files(path)) < 2:
            return findings
        floor, holders = _prune_floor(consumed_offset)
    except Exception:  # noqa: BLE001 —— 清理是**旁路**：前置查询失败也只告警，不许影响本轮消费结论
        logger.warning(
            "回调归档段清理的前置查询失败（路径 %s，已消费偏移 %s）⇒ 本轮跳过，下一轮再试",
            path, consumed_offset, exc_info=True,
        )
        return findings
    if floor < consumed_offset:
        moment = now or django_timezone.now()
        key = (str(path), floor)
        last = _PRUNE_FLOOR_ALERTS.get(key)
        if last is None or moment - last >= timedelta(hours=1):
            _PRUNE_FLOOR_ALERTS[key] = moment
            detail = "、".join(
                f"{line_name}/#{pk}/offset={offset}" for line_name, pk, offset in holders[:5]
            )
            if len(holders) > 5:
                detail += f" 等 {len(holders)} 份"
            message = (
                f"回调归档段清理被落后的绑定拖住（路径 {path}）：本机启用②式绑定的"
                f"最小已消费偏移 = {floor}，本绑定已消费到 {consumed_offset} "
                f"⇒ 本轮只删到 {floor}；拖住下限：{detail}"
            )
            logger.warning(message)
            findings.append(message)
    try:
        deleted = callback_store.prune_segments(min(consumed_offset, floor), path=path)
    except Exception:  # noqa: BLE001 —— 清理是**旁路**：删失败只告警，不许影响本轮消费结论
        logger.warning(
            "回调归档段保留期清理失败（路径 %s，已消费偏移 %s，下限 %s）⇒ 本轮跳过，下一轮再试",
            path, consumed_offset, floor, exc_info=True,
        )
        return findings
    if deleted:
        logger.info(
            "回调归档段已按保留期删除 %s 个（已消费偏移 %s，下限 %s）：%s",
            len(deleted), consumed_offset, floor, ", ".join(entry.name for entry in deleted),
        )
    return findings


def _event_time(record: dict) -> datetime | None:
    """记录里的 `received_at` → **带时区的 UTC** `datetime`（读不出来 ⇒ `None`）。⚠️ 用的是**接收器落盘时打的墙钟**（我们自己的钟），⛔ 不是回调报文里的 `timestamp`（F19：偏 +8 小时）；落盘形状是 `datetime.now(timezone.utc).isoformat()` ⇒ 带微秒与 `+00:00`；这里容掉「没带时区」的形态（按 UTC 算，⛔ 不按本机时区猜）。"""
    raw = record.get(FIELD_RECEIVED_AT)
    if not isinstance(raw, str) or not raw.strip():
        return None
    parsed = parse_datetime(raw.strip())
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    """库里取回来的时刻 → 带时区的 UTC（`None` ⇒ `None`）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _path_code(record: dict) -> str | None:
    """记录里 `path` 那串**回调码**；不是新路径 / 形状不对 ⇒ `None`。⚠️ 判据**借接收器自己的解析函数**（`apps.callback.wsgi.callback_code_of_path`）：路径的形状（前缀 + 定长 22 + 字母表）在 nginx 的 location 正则、接收器的路径解析两处已经钉死 ⇒ 在这里再写一遍正则就是第三个真相源（spec §22.3 明令复用）；码的比对一律**全等**（spec §5.5：⛔ 不用 `startswith`，前缀相同就全收 = 把码当装饰）。"""
    path = record.get(FIELD_PATH)
    if not isinstance(path, str):
        return None
    return callback_wsgi.callback_code_of_path(path)


def _body_of(record: dict) -> tuple[dict | None, str]:
    """记录的正文 → `(参数字典, 读不出来的原因)`。⚠️ **权威永远是 `body_b64`**（逐字节）。四种读不出来的形态各有各的原因句（都进台账的人话）：没落原文（超限 413）/ base64 坏了 / 不是 UTF-8 / 不是 JSON 对象。"""
    if not record.get(FIELD_BODY_STORED):
        return None, "报文超限没落原文（body_stored=false）"
    raw = record.get(FIELD_BODY_B64)
    if not isinstance(raw, str) or not raw:
        return None, "记录里没有 body_b64"
    try:
        body = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return None, "body_b64 不是合法的 base64"
    try:
        params = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "报文不是 UTF-8 的 JSON 对象"
    if not isinstance(params, dict):
        return None, "报文不是 JSON 对象"
    return params, ""


def _text_of(params: dict, field: str) -> str:
    """报文里某一格 → `strip()` 后的字符串（缺省 / `None` ⇒ 空串；⛔ 不用真值判空）。"""
    value = params.get(field)
    return "" if value is None else str(value).strip()


def _clip(text: str, limit: int = REASON_MAX) -> str:
    """按**列宽**截断人话（`reason` 是 `CharField(255)`；⛔ 不靠数据库截断、那会响亮报错）。"""
    return text if len(text) <= limit else text[:limit]


# ══ 五、台账写口（⛔ 复用既有的幂等写口，不自己写第二份）════════════════════════


def _note_order(*, order, kind: str, reason: str, status: str, payload: dict | None = None) -> bool:
    """**订单维度**的台账（幂等键 = `(kind, target_type=order, 单号, reason)`）。⚠️ 复用 `jidanpu_upstream.note_once`（本仓唯一那一份四元组去重实现）⇒ 同一件事**只记一条**，重复送达 / 水位清零后的重读都不会灌第二条。"""
    return jidanpu_upstream.note_once(
        order=order, kind=kind, status=status, reason=_clip(reason), payload=payload,
    )


def _note_event(*, binding, target_ref: str, kind: str, reason: str, status: str = "done",
                payload: dict | None = None) -> bool:
    """**没有我方订单可指认**的那些档（验签没过 / 码没人认领 / 找不到链路……）的台账。⚠️ 复用 `jidanpu_upstream._note_once_ref`：它是本仓「没有 order / mirror 可指认」那一档**唯一**的幂等写口（⛔ 不在这里另写一份去重）；`payload` 里⛔ 不许放凭据（照 `_note_order` 的既有纪律）：绑定 id / 码 / 单号 / sha 都是定位用的事实，一条凭据都没有。"""
    data = {"upstream_binding_id": binding.pk}
    data.update(payload or {})
    return jidanpu_upstream._note_once_ref(
        target_type="callback", target_ref=target_ref[:191], kind=kind, status=status,
        reason=_clip(reason), direction=JidanpuSyncEvent.Direction.IN, payload=data,
    )


def _callback_event_ref(*, line, binding, row) -> str:
    """一条回调事件的**稳定身份**（计次台账的 `target_ref`）。取 `received_at` + `body_sha256`（接收器落盘时写的两格）：同一行 JSONL 永远给出同一个键，⛔ 不用行号 / 文件偏移（增量读的偏移会变）、⛔ 不用 `orderNo`（同一张单有多条回调，它们是**不同事件**、各自计次）。
    前缀 `line.name` + 绑定主键：两张绑定表的主键各自从 1 开始，不隔离就会把另一条线的计次读成自己的。"""
    moment = str(row.get(FIELD_RECEIVED_AT) or "")
    sha = str(row.get(FIELD_BODY_SHA256) or "")
    return f"{line.name}:{binding.pk}:{moment}:{sha}"[:191]


def _note_callback_failure(*, line, binding, row, detail: str, now) -> tuple[int, bool, str]:
    """给**一条事件**记一次确认失败，回 `(累计次数, 是否已到上限, 计次台账的人话)`。⭐ **按事件计次、零迁移**：复用 `JidanpuSyncEvent.attempts`（outbox 早就在用这一列）与台账表本身，键 = `_callback_event_ref`（一行一条事件）；⛔ 不在绑定表上加计数器：那是「同一件事两个存储」，而且还要迁移。
    ⚠️ **只给确认失败调用**：撞本轮「最多查几条」上限的那条事件**一次都不许**进这里（它下一轮就能查，不是坏事件）；判据在 `_StatusQueries.cap_hit`，调用点在 `consume_callbacks` 的 `_RETRY` 分档。
    ⚠️ 这里直接 `objects.create`（不是 `jidanpu_common.write_event`）：这一行要同时落 `attempts` / `processed_at`，而那个「台账唯一写口」的签名里没有这两个参数；其余列与它逐字同形（`payload` 里同样⛔ 不放凭据）。"""
    ref = _callback_event_ref(line=line, binding=binding, row=row)
    payload = {
        line.payload_key: binding.pk,
        FIELD_RECEIVED_AT: row.get(FIELD_RECEIVED_AT),
        FIELD_BODY_SHA256: row.get(FIELD_BODY_SHA256),
    }
    existing = (
        JidanpuSyncEvent.objects.filter(
            direction=JidanpuSyncEvent.Direction.IN,
            kind=KIND_CALLBACK_RETRY,
            target_type="callback",
            target_ref=ref,
        )
        .order_by("id")
        .first()
    )
    if existing is None:
        attempts = 1
    else:
        #: `F()` 自增（并发下不互相覆盖），随后回读这一条拿累计值。
        JidanpuSyncEvent.objects.filter(pk=existing.pk).update(
            attempts=F("attempts") + 1
        )
        existing.refresh_from_db(fields=["attempts"])
        attempts = max(existing.attempts or 0, 1)
    abandoned = attempts >= CALLBACK_MAX_ATTEMPTS
    template = CALLBACK_ABANDONED_REASON if abandoned else CALLBACK_ATTEMPT_REASON
    reason = _clip(template.format(n=attempts, detail=_clip(detail, 140)))
    if existing is None:
        JidanpuSyncEvent.objects.create(
            direction=JidanpuSyncEvent.Direction.IN,
            kind=KIND_CALLBACK_RETRY,
            target_type="callback",
            target_ref=ref,
            status="failed",
            attempts=attempts,
            reason=reason,
            processed_at=now,
            payload=payload,
        )
    else:
        JidanpuSyncEvent.objects.filter(pk=existing.pk).update(
            status="failed", reason=reason, processed_at=now, payload=payload,
        )
    return attempts, abandoned, reason


#: 已知的**旧路径**（①式登录线那条）的形状名：它的行每一条都落在「不是本线路径」那一支
#: ⇒ 这一档是**正常**的（`status="skipped"`），⛔ 不当成「要人看的失败」。
FOREIGN_SHAPE_LEGACY = "legacy-notify（旧路径 /jidanpu/v1/notify）"


def _foreign_shape(record: dict) -> tuple[str, str]:
    """不是新路径的行 → `(形状名, 样本 path)`。⚠️ **形状名进台账的幂等键**（`target_ref` 里那一段）⇒ 它必须**不随具体哪一行变**，否则文件里每一条历史行都是一条新台账（= 刷屏）；样本路径只进 `reason` / `payload`（给人看、给排查用），⛔ 不进键。
    ⚠️ 形状名自带一句人话（台账页面上光看 `other-path` 读不懂）。四种形状各自独立：`path` 读不出来 / 旧路径 / 新前缀下码的形状不合法 / 别的路径——后三种里**只有第三种**与「接收器的路径规则变了」直接相关，所以它是**要人看**的那一档。"""
    path = record.get(FIELD_PATH)
    if not isinstance(path, str) or not path:
        return "no-path（记录里没有可读的 path）", "（无）"
    if path == callback_wsgi.NOTIFY_PATH:
        return FOREIGN_SHAPE_LEGACY, path
    if path.startswith(callback_wsgi.CALLBACK_PATH_PREFIX):
        return "bad-code-shape（新前缀下、但码的形状不合法）", path
    return "other-path（别的路径）", path


def _note_foreign_path(*, line: CallbackLine, binding, row, findings: list[str]) -> None:
    """⭐ ③ 事件文件里**不是本线路径**的行 ⇒ **按形状**留一条台账（⛔ 不是每行 / 每轮一条）。⚠️ **为什么必须留**：接收器那一侧的路径规则一变（前缀 / 定长 / 中间多一段路由），本绑定的回调就会**整批**流到这一支——没有这条留痕时，现场是「回调一条都没来、台账一行都没有」（本仓最恨的静默形态）；⛔ 但本线**不处理**这些行（旧路径是①式登录线的事，spec §5.4 的边界）。
    ⚠️ **为什么不刷屏**：旧路径的行**每条**都会走到这里 ⇒ 两道闸：进程内集合（`_REPORTED_SHAPES`，`note_once` 的查询**每形状只做一次**）+ `note_once` 的四元组（形状名是键的一部分 ⇒ 换进程重启也不会灌第二条）；⭐ 台账 kind 由**这一条线**给（`line.kinds.foreign_path`）：两条线的证据面不同，混用同一个 kind 会让「这条台账是谁写的」查不出来。
    ⭐⭐ **旧路径那一档⛔ 不进 `findings`（⇒ ⛔ 不进绑定 `last_error`）**（业务方报的「错误列里一直挂着一条**不是错的错**」）：`/jidanpu/v1/notify` 是**①式登录线（旧回调线）**自己的**正常流量**——它每 3 秒都会在文件里落行，跟这条绑定**毫无关系**，把它盖进错误列就是让健康列**永久**说一句假话（本仓最恨的形态）；判据直接取 `_foreign_shape` 的分档：这一档的 `status` 就是 `"skipped"`；**要人看**的那一档（`bad-code-shape`——新前缀下码的形状不合法，**可能是接收器的路径规则变了**）照旧进——⚠️ 两档**必须分开**，并成一档就是把真信号也一起摘掉了；台账两档都照留（⛔ 不是「把这一档整个删掉」）。"""
    shape, sample = _foreign_shape(row)
    screen = f"{getattr(connection, 'schema_name', '') or ''}:{binding.pk}"
    reported = _REPORTED_SHAPES.setdefault(screen, set())
    if shape in reported:
        return
    reported.add(shape)
    #: ⭐ 旧路径那一档（另一条线的正常流量）≠ 要人看的那一档；见 docstring 末段。
    known_foreign = shape == FOREIGN_SHAPE_LEGACY
    reason = REASON_FOREIGN_PATH.format(shape=shape, sample=_clip(sample, 80))
    wrote = line.note_event(
        binding=binding, target_ref=f"{binding.pk}:foreign:{shape}", kind=line.kinds.foreign_path,
        status="skipped" if known_foreign else "failed",
        reason=reason, payload={"shape": shape, "sample_path": _clip(sample, 120)},
    )
    if wrote and not known_foreign:
        # ⚠️ **只有第一次真的写了**才进 `findings`（⇒ 绑定 `last_error`）：那些行**每轮都在**
        #    文件里，每轮都盖一句就会让健康列**永久**显示同一句话（那不是信号，是噪音）。
        findings.append(reason)


def _note_stale_skip(*, line: CallbackLine, binding, row, moment: datetime, watermark: datetime,
                     findings: list[str]) -> None:
    """⭐ ② 本绑定的一条事件**没排在水位之后**、且是上一轮之后才追加进来的 ⇒ 留痕（**看得见的跳过**）。⚠️ 调用点已经挡掉了「上一轮那个前缀里那些**已经处理过**的行」（见 `consume_callbacks` 里那个 `index >= previous_count` 的条件）——⛔ 在这里再放宽，文件里每条历史事件每轮都会留一条痕。
    ⚠️ `reason` / `payload` 里**逐字**带上那一条的 `received_at` 与**当时的水位**（判据就是它俩），⛔ 一个凭据都没有（与 `_note_event` 的既有纪律一致）；D-0302：这两个时刻嵌在台账 `reason` 里**给人看**（绑定健康页同步事件），旧写法主动 `astimezone(utc)` 出 UTC 墙钟（差 8 小时）⇒ 走 `local_iso`（项目时区 +08:00，`datetime_display.py` 的既有收口出口）；判据比对（`consume_callbacks` 里的水位减法）不经过这里。"""
    received_at = local_iso(moment)
    watermark_text = local_iso(watermark)
    reason = REASON_STALE_SKIPPED.format(received_at=received_at, watermark=watermark_text)
    sha = row.get(FIELD_BODY_SHA256)
    line.note_event(
        binding=binding, target_ref=f"{binding.pk}:stale:{sha or received_at}",
        kind=line.kinds.stale_skipped, status="failed", reason=reason,
        payload={"received_at": received_at, "watermark": watermark_text},
    )
    findings.append(reason)


# ══ 六、认不认领判定：本租户有**启用中的②式绑定**的那些码 ═══════════════════════


def _claimed_codes() -> set[str]:
    """本租户**启用中的②式绑定**认领着的那些码 + 当前租户 schema 名。⚠️ 判据是 `mode == "openapi"` **且** `enabled=True` **且** 码非空（三样都要）：停用的绑定**不会**进调度遍历 ⇒ 它的码上的回调**没有任何人消费** ⇒ 必须由**别的**启用绑定把这件事报出来（⛔ 不许静默：那正是「回调没人管」的形态）。
    ⭐ **两条线一起算**（下家线收口）：回调码是**接收器那一侧**的共享命名空间——路径只有 `/api/jidanpu/callback/<码>` 一个前缀，`shared/callback-keys.env` 也是**一份**（码 → 密钥）⇒ 「这个码有没有人认领」这条事实**必须**看两条线。
    ⚠️ **只看自己那一条会怎样**（一旦两条线都有启用中的②式绑定，就是必然形态）：上家那把消费者会把**下家**的码报成「没人认领」、下家那把会把**上家**的码报成「没人认领」⇒ 两条线各刷一条**假警报**台账，而页面上的表现是「回调没人管」。"""
    codes: set[str] = set()
    for model in (JidanpuUpstreamBinding, JidanpuMemberBinding):
        codes.update(
            model.objects.filter(mode=jidanpu_openapi.OPENAPI_MODE, enabled=True)
            .exclude(callback_code="")
            .values_list("callback_code", flat=True)
        )
    return codes


# ══ 七、主入口：跑一轮消费者 ═══════════════════════════════════════════════════


def _admit(*, row, index: int, watermark, previous_count, line: CallbackLine, binding,
           findings: list[str]) -> bool:
    """⭐ 水位那道闸：这一条要不要进本轮的 `pending`（⛔ 口径一个字都没改，只是从循环里抽出来）。`received_at` 读不出来 ⇒ 留一条 `unreadable` 台账、**不进**（⛔ 不猜、也不静默丢）；**严格**排在水位之后 ⇒ 进；没排在水位之后、且是**上一轮之后才追加进来的**（乱序：钟被回拨 / 将来的双写进程）⇒ 留一条「看得见的跳过」、不进；落在上一轮那个前缀里的旧行 ⇒ 什么都不做（它是**处理过的**，水位只会往前推）。
    ⚠️ **增量读之后**这个前缀**不在本段里**了（本轮只读到已消费偏移之后的新行）⇒ `previous_count` 恒为 `0`（`None` 只出现在「本进程还没读过这份文件」那一轮），于是「本段里每一条挡住的事件」都算「新尾巴」⇒ 那些乱序事件**照旧看得见**，⛔ 没变。
    ⚠️ **为什么要抽成函数**：老地址那条例外（见 `consume_callbacks` 的 ③）与「路径里的码等于本行的码」那一支走的是**同一道闸** ⇒ 各写一份必然漂，而漂了的症状是**静默丢事件**（本仓最恨的那一形态）。"""
    moment = _event_time(row)
    if moment is None:
        # 没有可用的接收时刻 ⇒ 排不进水位序（⛔ 不猜、也不静默丢）：只留一条痕。
        line.note_event(
            binding=binding, target_ref=f"{binding.pk}:{row.get(FIELD_BODY_SHA256) or 'no-time'}",
            kind=line.kinds.unreadable, status="failed",
            reason=REASON_UNREADABLE.format(detail="记录里没有可读的 received_at"),
        )
        return False
    if watermark is None or moment > watermark:
        return True
    if previous_count is not None and index >= previous_count:
        # ② ⭐「没排在水位之后」+「上一轮之后才追加进来的」⇒ 这一条**没被处理过**、
        #    却被水位挡住 ⇒ ⛔ 不许静默（一行台账都没有是这条口径最贵的代价）。
        #    ⚠️ 落在上一轮那个前缀里的旧行**是处理过的**（水位只会往前推）⇒ 不留痕。
        _note_stale_skip(
            line=line, binding=binding, row=row, moment=moment, watermark=watermark,
            findings=findings,
        )
    return False


def consume_callbacks(binding, *, now=None, events_path=None, token_blocked="",
                      max_orders: int | None = None,
                      line: CallbackLine | None = None) -> ConsumeOutcome:
    """读一遍事件文件，把**本绑定**那些还没处理过的回调落到我方订单上（spec §5.5）。调用点：`jidanpu_sync._run_upstream_binding` 的 **①outbox 之前**（顺序是承重的：先把对方推来的事实收敛掉，再决定要不要往外推，否则会拿**本地已过期**的状态去推）；下家线（`JidanpuMemberBinding`）的同一个位置见 `jidanpu_sync._run_member_binding`；⭐ 另一个调用点是「立即同步」端点（②式那一跳，**同步 web 请求**——见 `max_orders`）。⚠️ 本函数**不接任何请求来源的东西**：一行绑定 + 两个可选注入（`now` 给用例、`events_path` 给用例指文件）——⛔ 没有租户标识、没有地址、没有凭据入参（`AGENTS.md` §4 的租户边界）。
    ⭐ **`line`（下家线收口）**：这一行绑定属于**哪条线**（`CallbackLine`）；`None` ⇒ 上家线（`upstream_line()`，行为**逐字**是加这个参数之前的那一份）；⚠️ 引擎（读文件 / 分流 / 水位）**只有这一份**：两条线的差异全在 `line` 那几格里，⛔ 不许把这段循环复制成第二份（那必然漂，而漂了的症状是**静默丢事件**）。⭐ **`max_orders`**：本轮最多查几张单（`orderNo` 去重之后）；`None` ⇒ 本模块的契约值 `MAX_ORDERS_PER_ROUND`（⛔ 默认值的出处只有它这一处）；为什么要有这个入参：「立即同步」那一跳是**同步 web 请求**（用户在页面上等它返回，gunicorn 的 worker 超时是 30 秒）⇒ 它必须用比常驻调度**小得多**的窗口（常驻那一拍撞上限只是「下一拍接着查」，而这一跳撞上限是「页面一直转圈」）；撞上限的处置**一个字都不变**：水位不前移到没处理的那几条之后 ⇒ 只推迟、不丢。
    ⭐ **`token_blocked`（spec §9.5 ②，T12）**：非空 = 调度层**已经**知道这一份凭据刚被①档拒过（退避窗口内）⇒ 本轮**一次都不打那个必被拒的接口**，直接回一个「本轮未跑」的结论（`blocked_reason` 就是那句人话，进 `findings` ⇒ 绑定 `last_error`）；⛔ 不许静默跳过：静默跳过会让页面上的「最近一次尝试」停在很久以前，而那看起来像消费进程挂了；⚠️ **由调度层传进来、⛔ 不在本模块自己判**：这一整轮（消费 + 兜底对账）必须看到**同一个**结论——两处各判一次就是「同一件事两个判据」（T12 实测踩到的正是这个：一轮里消费与对账各打了一次取 token，见 `jidanpu_sync._run_upstream_binding` 的注释）。"""
    line = line or upstream_line()
    now = now or django_timezone.now()
    code = (getattr(binding, "callback_code", "") or "").strip()
    if not jidanpu_openapi._is_openapi(binding) or not code:
        # ⛔ ①式绑定没有回调地址、也没有码 ⇒ **一次文件都不读**（零成本，照 spec §5.5）。
        return ConsumeOutcome(enabled=False, watermark=_as_utc(binding.callback_watermark_at))
    if token_blocked:
        return ConsumeOutcome(
            enabled=True, watermark=_as_utc(binding.callback_watermark_at),
            findings=(token_blocked,), blocked_reason=token_blocked,
        )

    path = Path(events_path) if events_path is not None else callback_config.events_path()
    watermark = _as_utc(binding.callback_watermark_at)

    # ① 文件指纹（「比上次小 / mtime 倒退」那一半判据要靠它；⛔ 不落库，见 `_FILE_STATE`）。
    #    ⭐（接收器 H-1 轮转）：这里取的是**累计写入字节**（`snapshot().high_water_bytes` = 现役文件段首 + 现役文件长），⛔ 不是现役文件自己的 `st_size`、也⛔ 不是 `snapshot().total_bytes`（现存段字节和）。理由：轮转会让现役文件变短（但全局长度不变），而**保留期清理会删掉旧归档段、让现存字节和变小** ⇒ 用 `total_bytes` 判「变短」会把每一次清理都误判成「文件被搬动过」（水位清零 + 现存段重读一遍）。
    #    高水位（累计写入字节）对轮转与清理都是**不减**的，才是「文件真的变短了吗」的正确坐标；而 `callback_file_offset` 也是**全局**偏移，两者同一套坐标。
    snap = callback_store.snapshot(path)
    high_water, mtime = snap.high_water_bytes, snap.live_mtime

    previous = _FILE_STATE.get(str(path))
    findings: list[str] = []

    # ② 起点 = **已消费到的字节偏移**（⛔ 不再每轮从头扫 198MB）。四种形态：水位为空（刚被清零 / 人工清空）⇒ 从**现存最早一段**读起（更早的已被保留期清理删掉）；偏移 = 0、水位非空、文件 > 32MB ⇒ 只读末尾 16MB（首轮大文件；留一条人话）；
    #    偏移落在已被清理的范围内（> 0 且早于现存最早段）⇒ `read_records_from` 会响亮报错（⛔ 不在这里悄悄夹回来：那是另一份绑定在拖后腿 / 有人手工动过，要人看见）；其余 ⇒ 从偏移起读（稳态下每轮只读新追加的那几行）。
    stored_offset = int(getattr(binding, "callback_file_offset", 0) or 0)
    start = stored_offset
    skip_partial = False
    if watermark is None:
        #: ⚠️ 从**现存最早段**起读（`earliest_base`）而不是 0：更早的归档段可能已被保留期清理
        #: 删掉，硬传 0 会让 `read_records_from` 判「要读已删掉的范围」而响亮报错 —— 清零之后
        #: 必须还能把**留存下来的**那一段读一遍（保留期窗口内的回溯能力就在这一行）。
        start = snap.earliest_base
    elif stored_offset == 0 and high_water > FIRST_RUN_TAIL_THRESHOLD:
        start = max(0, high_water - FIRST_RUN_TAIL_BYTES)
        if start <= snap.earliest_base:
            #: 末尾 16MB 已经够到（甚至早于）现存最早段 ⇒ 从段首读起：段首必是行首，
            #: ⛔ 不能带着 `skip_partial` 去丢第一行（那会静默丢一条回调）。
            start = snap.earliest_base
        else:
            #: ⚠️ 起点落在某一行中间 ⇒ 先丢掉那半行（`read_records_from` 的 `skip_partial`）。
            skip_partial = True
        findings.append(REASON_TAIL_SKIP.format(
            size_mb=high_water // (1024 * 1024),
            tail_mb=FIRST_RUN_TAIL_BYTES // (1024 * 1024),
            watermark=local_iso(watermark),
        ))

    # ③ 「文件被搬动过 / 变短了」⇒ 水位与**已消费偏移一起**清零 + 响亮留痕（spec §5.5；E8）。三条判据一起上：
    #    **上次的高水位 / mtime**（进程内记忆，`_FILE_STATE`）；⭐ **偏移比高水位还长**（不需要记忆：偏移是从这份文件上取下来的位置，高水位比它还短 ⇒ 一定被换过；旧写法在进程重启后靠「整份读一遍」才发现，现在这一条顶那一半）；**内容不可能比水位还旧**（下面 `rolled_back`，也不需要记忆）。
    #    ⚠️ 三条都只认**高水位**，⛔ 不认 `total_bytes`：保留期清理会让现存字节和小下去，那是**正常**的（见 ① 的注释），拿它当判据 = 每次清理都整份重读。
    shrank = previous is not None and (
        high_water < previous[0]
        or (previous[1] is not None and mtime is not None and mtime < previous[1])
    )
    offset_beyond = stored_offset > high_water
    if shrank or offset_beyond:
        _FILE_STATE[str(path)] = (high_water, mtime, 0)
        _write_watermark(
            binding, model=line.binding_model, watermark=None, last_seen=None, processed=0,
            offset=0,
        )
        findings.append(REASON_WATERMARK_RESET)
        return ConsumeOutcome(
            enabled=True, scanned=0, reset=True, watermark=None, findings=tuple(findings),
        )

    # ⭐⭐ N2 兜底开关（业务方选 ②）：**默认关**。起点早于现存最早段 = 它落在**已被保留期清理删掉**的范围。默认关 ⇒ 交给 `read_records_from` 照旧**响亮抛** `OffsetOutOfRetention`（等人工处置；⛔ 不夹回段首、⛔ 不静默从头读）；显式打开 ⇒ 跳到现存最早段继续，并把丢失的 `[旧偏移, earliest_base)` 字节区间写进 findings（⇒ 绑定 `last_error`）与日志。
    #    ⚠️ 打开之后**照样不补**丢掉的那一段（它已经不在磁盘上了）——开关只是让这一份绑定不永久卡死；「丢了什么」必须有人看得见。
    if start < snap.earliest_base and auto_skip_out_of_retention_enabled():
        lost_from = start
        start = snap.earliest_base
        reason = REASON_AUTO_SKIP_OUT_OF_RETENTION.format(
            old_offset=lost_from, earliest_base=start, lost_bytes=start - lost_from,
        )
        findings.append(reason)
        logger.warning(reason)

    scan = _scan(path, offset=start, skip_partial=skip_partial)
    if scan.broken_line is not None:
        # ⛔ 不许静默：坏行本身要留痕；**它后面的内容本轮不处理**（偏移停在它之前）。
        findings.append(REASON_BROKEN_LINE.format(
            offset=scan.new_offset, line=scan.broken_line,
        ))

    latest = max(
        (t for t in (_event_time(row) for row in scan.records) if t is not None),
        default=None,
    )
    if start > 0 and watermark is not None and latest is not None and latest < watermark:
        # ⭐ 本轮读到的这一段**全都比水位还旧** ⇒ 必须分清是哪一种（判据 = **整份文件**最新一条）：
        #   ① 文件被换成一份**更旧的整份副本**（长度甚至可能变长）⇒ 水位 + 偏移清零、整份重读；② 只是**一条乱序**（接收器的钟被 NTP 回拨）追加在末尾 ⇒ ⛔ **不许**清零（清零会把 198MB 重读一遍），交给 `_admit` 留一条「看得见的跳过」。
        #   ⚠️ 这是**罕见路径**（新读到的事件全都比水位旧才会进来）：稳态下每轮只读新尾巴。
        whole = _scan(path, offset=snap.earliest_base)
        latest = max(
            (t for t in (_event_time(row) for row in whole.records) if t is not None),
            default=None,
        )
    rolled_back = watermark is not None and latest is not None and latest < watermark
    if rolled_back:
        _FILE_STATE[str(path)] = (high_water, mtime, len(scan.records))
        _write_watermark(
            binding, model=line.binding_model, watermark=None, last_seen=None, processed=0,
            offset=0,
        )
        findings.append(REASON_WATERMARK_RESET)
        return ConsumeOutcome(
            enabled=True, scanned=len(scan.records), reset=True, watermark=None,
            findings=tuple(findings),
        )
    _FILE_STATE[str(path)] = (high_water, mtime, len(scan.records))
    #: 判「这一条是**上一轮之后**才追加进来的吗」（见模块 docstring「已知边界」那一节）：
    #: **增量读**之下本轮读到的每一条都排在已消费偏移之后 ⇒ 都是「新尾巴」（`0`）；
    #: 只有「本进程还没读过这份文件」那一轮不知道前缀到哪（`None` = 不判，与旧代价同一格）。
    previous_count = None if previous is None else 0

    # ③ 分流：属于**这个回调码**的（水位之后）留在 `pending`；别的码只判「有没有人认领」。
    #    同一个租户、同一个 `jidanpu_base_url` 共用一个回调码，一个码下可能有多行，而这一轮只跑其中**一行** ⇒ `pending` 里会有「同码兄弟行」的事件。
    #    谁的单归谁办：`line.landed` 在一整组里按 `orderNo` 定位（见 `jidanpu_member_openapi._link_of`），⛔ **不是**在这里按绑定过滤掉它们（那样会把别人家的单报成「本绑定名下没有这条链路」= 假警报）。
    claimed = _claimed_codes()
    schema = getattr(connection, "schema_name", "") or ""
    #: ⭐ **老地址那条例外**开不开（这一**行绑定所属那条线**说了算；`None` ⇒ 不开）。
    #: 一轮只问一次（它在答案上是**整轮恒定**的：租户 + 环境变量）。
    legacy_ok = bool(line.legacy_notify is not None and line.legacy_notify())
    #: ⚠️ 存 `(在本段里的下标, 记录)`：中途停下时要把偏移停回**第一条没处理的记录之前**
    #: （只有下标能定位到那一行的字节位置，见 `_scan` 的 `line_ends`）。
    pending: list[tuple[int, dict]] = []
    unclaimed: set[str] = set()
    for index, row in enumerate(scan.records):
        row_code = _path_code(row)
        if row_code is None:
            if legacy_ok and str(row.get(FIELD_PATH) or "") == callback_wsgi.NOTIFY_PATH:
                # ⭐ **老地址那条例外**（`/jidanpu/v1/notify`）：这条报文里**没有码**，只能靠 `orderNo` 定位（谁的单归谁办见 `line.landed`）。
                #    ⚠️ 它走的是**同一道水位闸**（`_admit`）⇒ 老地址上几千条历史回调不会被每 3 秒重读一遍。
                if _admit(
                    row=row, index=index, watermark=watermark, previous_count=previous_count,
                    line=line, binding=binding, findings=findings,
                ):
                    pending.append((index, row))
                continue
            # 不是新路径（旧路径 `/jidanpu/v1/notify`）或形状不对 ⇒ ⛔ 不越界处理，**但要看得见**
            # （万一接收器那边的路径规则变了，本绑定的回调会整批从这里流过去；③）。
            _note_foreign_path(line=line, binding=binding, row=row, findings=findings)
            continue
        if row_code == code:
            if _admit(
                row=row, index=index, watermark=watermark, previous_count=previous_count,
                line=line, binding=binding, findings=findings,
            ):
                pending.append((index, row))
            continue
        if row_code not in claimed:
            unclaimed.add(row_code)

    for bad_code in sorted(unclaimed):
        # 每码只问一次库（进程内集合），但**必须**问一次：台账是本档唯一的留痕出口。
        reported = _REPORTED_UNCLAIMED.setdefault(schema, set())
        if bad_code in reported:
            continue
        reason = REASON_UNCLAIMED_CODE.format(code=bad_code)
        line.note_event(
            binding=binding, target_ref=f"code:{bad_code}", kind=line.kinds.unclaimed,
            status="failed", reason=reason, payload={"callback_code": bad_code},
        )
        reported.add(bad_code)
        findings.append(reason)

    outcome_unclaimed = len(unclaimed)
    if not pending:
        # 没有新事件 ⇒ 一次上游都不打（这是「不设独立间隔闸」那条口径成立的理由）。
        # ⭐ 但**读到的位置要落库**（增量读：下一轮从这里接着读）。没读到新东西时
        # `new_offset` 与库里那一列相等 ⇒ 连这一次 UPDATE 都省掉（稳态零写）。
        if scan.new_offset != stored_offset:
            _write_watermark(
                binding, model=line.binding_model, watermark=watermark, last_seen=None,
                processed=0, offset=scan.new_offset,
            )
        #: ⭐ 保留期清理：**推进完偏移之后**顺手删掉已消费 + 超期的归档段（⛔ 不在接收器的写热路径上；删失败只 warning，不影响本轮结论）。
        #: ⭐ N2：安全下限 / 拖住下限的限频告警都在这句里，它回的那几句进 `findings` ⇒ 绑定 `last_error`（页面上看得见）。
        findings.extend(_prune_consumed_segments(path, scan.new_offset, now=now))
        return ConsumeOutcome(
            enabled=True, scanned=len(scan.records), unclaimed=outcome_unclaimed,
            watermark=watermark, findings=tuple(findings),
        )

    # ④ 逐条处理（文件顺序 = 收到顺序）；查权威状态按 `orderNo` **去重**（E5：同一条单的多条
    #    通知只查一次），撞上限或查失败就在**那一条之前**停下（水位不前移 ⇒ 下一轮重试）。
    applied = skipped = failed = 0
    processed_times: list[datetime] = []
    blocked_at: datetime | None = None
    stop = False
    #: 第一条**没处理**的记录在本段里的下标（中途停下时靠它把已消费偏移停回那一行之前）。
    stop_index: int | None = None
    with _StatusQueries(binding, max_orders=max_orders) as queries:
        for index, row in pending:
            if stop:
                if stop_index is None:
                    stop_index = index
                moment = _event_time(row)
                if moment is not None and (blocked_at is None or moment < blocked_at):
                    blocked_at = moment
                continue
            moment = _event_time(row)
            if moment is None:  # pragma: no cover - 上面已经挡掉了「没有时刻」那一档
                continue
            findings_before = len(findings)
            verdict = _consume_one(
                row, binding=binding, line=line, queries=queries, now=now, findings=findings,
                legacy=legacy_ok and str(row.get(FIELD_PATH) or "") == callback_wsgi.NOTIFY_PATH,
            )
            if verdict == _DEFERRED or (verdict == _RETRY and queries.cap_hit):
                # 资源暂不可用，保留原事件，不消耗失败次数。
                stop = True
                stop_index = index
                blocked_at = moment
                if verdict == _RETRY:
                    failed += 1
                continue
            if verdict == _RETRY:
                # ⭐ **确认失败**（查权威状态抛了）：按**事件**计次；到上限 ⇒ 放弃并让水位
                # 越过它（⛔ 不再每轮卡住水位），否则照既有口径停在这里等下一轮。
                detail = (
                    findings[-1] if len(findings) > findings_before
                    else "本轮没拿到结论（查权威状态失败）"
                )
                _, abandoned, retry_reason = _note_callback_failure(
                    line=line, binding=binding, row=row, detail=detail, now=now,
                )
                if abandoned:
                    # 放弃这一条：留一条人话（`findings` ⇒ 绑定 `last_error`），把它算作
                    # 「已有结论」推进水位，然后**接着处理下一条**。
                    findings.append(retry_reason)
                    processed_times.append(moment)
                    failed += 1
                    continue
                stop = True
                stop_index = index
                failed += 1
                blocked_at = moment
                continue
            processed_times.append(moment)
            if verdict == _APPLIED:
                applied += 1
            elif verdict == _SKIPPED:
                skipped += 1
            else:
                failed += 1

    # ⑤ 推水位：全处理完 ⇒ 本批最大；中途停下 ⇒ 「第一条没处理的」前一微秒（⛔ 用最大已处理，那会让同一微秒里的后一条永远轮不到）。
    #    ⚠️ **一条都没处理成**（第一轮就撞上「查不动」）⇒ **水位一个字都不动**（连「前一微秒」都不写）：这一轮没有任何结论，就没有任何「已经处理过」的范围可言。
    if blocked_at is not None and processed_times:
        new_watermark = blocked_at - timedelta(microseconds=1)
        if watermark is not None and new_watermark < watermark:
            new_watermark = watermark
    elif processed_times:
        new_watermark = max(processed_times)
    else:
        new_watermark = watermark
    last_seen = max(processed_times) if processed_times else _as_utc(binding.callback_last_at)

    # ⑥ 已消费偏移：全处理完 ⇒ 本段末尾（坏行之前）；中途停下 ⇒ **第一条没处理的记录之前**
    #    （⛔ 不能推到段尾：那一条下一轮就读不到了 = 静默丢；它的下标由 `stop_index` 给，
    #    前一条记录的「行尾」就是它的「行首」）。
    if stop_index is not None:
        new_offset = scan.line_ends[stop_index - 1] if stop_index > 0 else scan.start_offset
    else:
        new_offset = scan.new_offset
    _write_watermark(
        binding, model=line.binding_model, watermark=new_watermark, last_seen=last_seen,
        processed=len(processed_times), offset=new_offset,
    )
    #: ⭐ 保留期清理：**推进完偏移之后**顺手删掉已消费 + 超期的归档段（⛔ 不在接收器的写热路径上；删失败只 warning，不影响本轮结论）。
    #: ⭐ N2：安全下限 / 拖住下限的限频告警都在这句里，它回的那几句进 `findings` ⇒ 绑定 `last_error`（页面上看得见）。
    findings.extend(_prune_consumed_segments(path, new_offset, now=now))
    return ConsumeOutcome(
        enabled=True, scanned=len(scan.records), mine=len(pending), applied=applied,
        skipped=skipped, failed=failed, unclaimed=outcome_unclaimed,
        watermark=new_watermark, findings=tuple(findings),
        #: ⭐ 取 token 那一跳被拒过（任一档）⇒ 让**同一轮的后一段**也别再打它（spec §9.5 ③）。
        token_rejected=queries.token_rejected,
    )


#: `_consume_one` 的四种结论（字符串常量而不是枚举：它只在本模块内用，且要能一眼读懂）。
#: `_APPLIED`：**写了**状态（或走完了写入那一支）；`_SKIPPED`：有结论、**不写**，且不需要人管（幂等 / 本线明确不映射 / 停跟踪 / 别的 action）；`_NOTED`：有结论、**不写**，但**要人看**（验签没过 / 找不到单 / 认不出来的状态 / 跟退 / 状态机拒绝）⇒ 计进 `failed`（⛔ 不是「失败」本身，而是「这一轮有事要人看」）；`_RETRY`：**没拿到结论**（查权威失败 / 撞上限）⇒ 不写、水位不前移、**本轮就此停下**。
#: ⚠️ `_RETRY` 那两档的**后续处置不同**：`_StatusQueries.cap_hit` 为真 = 撞本轮上限（**不计回调失败次数**、下一轮接着查）；为假 = 确认失败（按事件计次，到 `CALLBACK_MAX_ATTEMPTS` 放弃并推进水位）。
_APPLIED = "applied"
_SKIPPED = "skipped"
_NOTED = "noted"
_RETRY = "retry"
_DEFERRED = "deferred"

#: ⭐ 上面的结论的**公开**别名（T8 的兜底对账走同一段写入面 ⇒ 它也要判这四个结论）。
#: ⛔ **不是第二套取值**：同一批字符串对象，只是给模块外的调用方一个不走下划线的入口
#: （`_consume_one` 内部照旧用短名字）。
OUTCOME_APPLIED = _APPLIED
OUTCOME_SKIPPED = _SKIPPED
OUTCOME_NOTED = _NOTED
OUTCOME_RETRY = _RETRY
OUTCOME_DEFERRED = _DEFERRED


class _StatusQueries:
    """本轮**按需**建连的权威状态查询口（一次连接、按 `orderNo` 去重、撞上限就停）。⚠️ **懒建连**：一轮里大多数时候**没有**新事件 ⇒ 一次上游都不打（这正是 spec §5.5「不设独立间隔闸」能成立的理由）；token 由 T2 的模块级缓存持有 ⇒ 一次连接取一次 token。
    ⚠️ **两条路共用它**（回调 = T6 / 兜底对账 = T8）：查单的口径（懒建连、按单号去重、撞上限回 `None`）只在这一处实现，⛔ 不各写一份（各写一份必然漂）；两条路的上限不同（T6 是 `MAX_ORDERS_PER_ROUND`、T8 是 spec §8.2 的 50）⇒ 上限由调用方传进来（`max_orders`），`cap` 就是「本轮这一条路上限是多少」的读数。"""

    def __init__(self, binding, *, max_orders: int | None = None):
        self._binding = binding
        self._stack = None
        self._client = None
        self._cache: dict[str, str] = {}
        #: ⭐ `GetOrderProcessImgs` 的结果缓存（②式 B 段那一跳）：同一轮同一单**只打一次**
        #: （同一条单会有多条回调，而「待验收」那一刻只会发生一次；这一格是第二道保险）。
        self._imgs: dict[str, tuple[dict, ...]] = {}
        #: 本轮的查单上限。`None` ⇒ T6 那条路的契约值（⛔ 默认值只有这一处出处）。
        self.cap = MAX_ORDERS_PER_ROUND if max_orders is None else max_orders
        #: 本轮**真打出去**的查单次数（撞上限的判据就是它，见 `self.cap`）。
        self.queried = 0
        #: ⭐ **取 token 那一跳被拒过吗**（spec §9.5 ②，T12）：任一档都算（①②③）；调度层靠它让**同一轮里的后一段**（兜底对账）也不再打那个注定失败 / 必被拒的接口。
        #: ⚠️ **每轮一个实例**（`status_queries` 建的），⛔ 不跨轮记住——跨轮那件事由 `jidanpu_openapi` 的退避窗口管（它只对①档生效，见那段注释）。
        self.token_rejected = False
        #: ⭐ **最后一次查单失败的指纹** `(step, kind, http_status, has_code)`；`None` = 这一次没失败（或失败的不是 `OpenApiError`）。
        #: ⚠️ **为什么要有它**（停滞修复，G2）：兜底对账的循环从前只拿得到一个 `OUTCOME_RETRY` 字符串，分不出「**这一张单**坏了」（跳过它继续下一张）与「凭据 / 网络 / 上游挂了」（整轮停住）⇒ 一张坏单就能把整个窗口永久钉死。
        #: ⚠️ 复位时机 = **每次真的要去查之前**（见 `status_of`）：缓存命中与「撞上限」都不是一次上游失败，不能沿用上一张单的指纹。
        self.last_failure: tuple[str, object, int | None, bool] | None = None
        #: ⭐ **这一次 `status_of` 是不是撞了本轮上限**：回调消费者靠它把「**没来得及处理**」与「**确认失败**」分开——只有后者计入回调重试上限；与 `last_failure` 同一条复位口径：每次 `status_of` 开头先清。
        self.cap_hit = False

    def __enter__(self) -> "_StatusQueries":
        return self

    def __exit__(self, *exc_info) -> None:
        if self._stack is not None:
            self._stack.close()

    def _client_of(self):
        """本轮的官方 API 客户端（**懒建连**：一轮里要真的打上游时才建、只建一次）。"""
        if self._stack is None:
            self._stack = contextlib.ExitStack()
            self._client = self._stack.enter_context(
                jidanpu_openapi.openapi_client(self._binding)
            )
        return self._client

    def status_of(self, order_no: str) -> str | None:
        """这一条上家单号的**权威状态**；查不动 ⇒ 抛 `OpenApiError` / `ConfigError`（调用方接）。⚠️ 同一个 `orderNo` 本轮只查一次（实测某单会来 15 条通知，F20）；撞上限时回 `None`（调用方按「本轮停在这里」处理 ⇒ ⛔ 不是丢事件：水位不前移）。
        ⚠️⭐ **`None` 只在这一种情况下出现**（判据见 `jidanpu_member_openapi._apply_member_status` 的注释）：拿不到可读状态时**抛**（`OpenApiClient.get_order_status` 对「`data` 里没有 `status` / 它不是非空字符串」一律抛 `OpenApiError`）⇒ ⛔ 调用方**不许**把 `None` 解释成「没查到值」（那两档的处置相反：这个要水位**不动**，那个要水位**前移**）。"""
        #: ⭐ 每一次调用都先复位（缓存命中 / 成功 / 真失败都不是「撞上限」）。
        self.cap_hit = False
        if order_no in self._cache:
            return self._cache[order_no]
        if self.queried >= self.cap:
            #: ⭐ 「本轮没来得及处理」≠「确认失败」：回调消费者按这一格决定**不计次、不放弃**。
            self.cap_hit = True
            return None
        #: ⭐ 每一次**真的要去查**之前清指纹（缓存命中 / 撞上限都不是一次上游失败）。
        self.last_failure = None
        client = self._client_of()
        self.queried += 1
        try:
            status = client.get_order_status(order_id=order_no).status
        except jidanpu_openapi.OpenApiError as exc:
            # ⭐ **取 token 那一跳被拒**（任一档）⇒ 记下来给调度层（`ConsumeOutcome.token_rejected` / `ReconcileOutcome.token_rejected`）：同一轮里后一段就不该再打那个接口了（spec §9.5 ③）。
            #    ⚠️ 判据是**步骤**（`exc.step`），⛔ 不是档位——③档（网络 / 上游故障）同样不该在同一轮里连打两次，只是它**不**进退避窗口（那是跨轮的事）。
            if exc.step == jidanpu_openapi.STEP_TOKEN:
                self.token_rejected = True
            #: ⭐ 指纹（G2）：四格都取自 `OpenApiError` 自己的取证字段，⛔ 不解析 `msg` 文案。
            self.last_failure = (
                exc.step, exc.kind, exc.http_status, exc.upstream_code is not None,
            )
            raise
        self._cache[order_no] = status
        return status

    def process_imgs_of(self, order_no: str) -> tuple[dict, ...]:
        """`GetOrderProcessImgs`：一单的**全量进度图行**（②式 B 段那一步的取数口）。⭐ 它就**替代**②式下被跳过的 `ViewPic`（业务方报的「换成②式之后截图停了」）：在「远端已完成 ⇒ 写待验收」之前拉一次全量图，落库走 `jidanpu_upstream_pics.store_remote_pics`（⛔ 不另造一套表 / 目录）；⚠️ **与 `status_of` 共用同一个客户端**（`_client_of`）⇒ ⛔ 不为拉图再取一次 token、⛔ 也不新开一条连接。
        ⚠️ **这一跳不占 `cap`**：那个上限是「一轮最多查几张单的**状态**」那一件事（回调暴雨时保护上游），而拉图**只发生在真的要写「待验收」的那一单上**（一张单一辈子最多一次）；把它算进同一个上限会让「撞上限」与「拉了图」互相顶掉，那是两件事。失败照旧**抛**（`OpenApiError`）：调用方（`jidanpu_member._pull_openapi_screenshots`）把它收成一条台账人话，**⛔ 不挡「待验收」**。
        ⚠️ **同一个单号本轮只打一次**（`self._imgs`）——⚠️ 这道缓存**是承重的、不是防御性写法**（去掉它哪条用例会红：`test_a_denied_write_does_not_pull_the_images_twice_in_one_round`）：写被状态机 / 节点门**拒绝**时订单**没进**「待验收」，于是同一条单的**下一条通知**在本轮里会再走一遍「判 → 写」那一段 ⇒ 没有这道缓存就会为同一条单重复拉全量图（实测某单会来 15 条通知）。"""
        if order_no in self._imgs:
            return self._imgs[order_no]
        client = self._client_of()
        try:
            rows = client.fetch_order_process_imgs(order_id=order_no)
        except jidanpu_openapi.OpenApiError as exc:
            # 判据与 `status_of` 同源（取 token 那一跳被拒 ⇒ 同一轮的后一段也别再打它）。
            if exc.step == jidanpu_openapi.STEP_TOKEN:
                self.token_rejected = True
            raise
        self._imgs[order_no] = rows
        return rows


def status_queries(binding, *, max_orders: int | None = None) -> _StatusQueries:
    """给**两条路**（回调 T6 / 兜底对账 T8）共用的查询口（⛔ 不各写一份）。⚠️ 存在的理由只有一条：`_StatusQueries` 是私有的，而 T8 必须**用同一个**实现（懒建连 / 按单号去重 / 撞上限回 `None` 这三条口径各写一份必然漂）；它**不是**第二个实现。"""
    return _StatusQueries(binding, max_orders=max_orders)


def apply_authoritative_status(*, order, binding, order_no, queries, now, findings,
                               trigger: str = TRIGGER_CALLBACK) -> str:
    """把**一次权威状态**落到我方订单上：查 → 裁决 → 写 / 留痕（⭐ 唯一的那一段）。调用方两处（⛔ 不许再多一处）：`_consume_one` 的第 ⑥–⑧ 步（回调驱动，T6）与 `jidanpu_openapi_reconcile` 的逐单循环（兜底对账，T8）。⭐ **为什么把它抽出来**（spec §8.3 第 ③ 条）：对账的写入面必须与回调**完全一致**（同一个 `should_write_status`、同一个 `apply_transition` 出口、同一套机器身份口径），⛔ 不许对账那条路「多写一列」；把这段**整段共用**——而不是让对账再写一遍「查 + plan + 写」——是这条口径唯一能机械保证的形态。⚠️ `trigger` 只影响**人话与台账 `payload`**（`TRIGGER_CALLBACK` / `TRIGGER_RECONCILE`）：写面（`apply_transition` 的入参、审计行、`nodes`）两条路**逐字相同**（⛔ 别让 trigger 影响写面：那正是 E9 要抓的「多写一列」）；回 `OUTCOME_APPLIED` / `OUTCOME_NOTED` / `OUTCOME_SKIPPED` / `OUTCOME_RETRY`。
    ⭐ **`findings` 只收「要人看」的那两类**（`_NOTED` / `_RETRY`）——T8 收口修的一处：写成功（`_APPLIED`）与「一致 ⇒ 不写」（`_SKIPPED`）是**正常结论**，台账里已经有各自的 `done` / `skipped` 行、终端上有计数（`*.text`），⛔ **不该**再进 `last_error`——那会让绑定页面的**错误列**常年挂着一句「已回写为『已暂停』」（**把成功说成错误**）；这条以前看不出来：回调消费只服务②式，而②式在界面上的形态进不了那个循环（入口闸的缺陷）⇒ 修好入口闸之后它才会真的每天出现。"""
    try:
        remote_status = queries.status_of(order_no)
    except Exception as exc:  # noqa: BLE001 —— 上游 / 配置失败一律「不写 + 本轮停在这里」
        # ⛔ **E3 的判据**：查不动权威状态时**一个字都不写订单**（⛔ 绝不拿回调里的值顶上）。
        # ⚠️ 只写异常**类型名**（`str(exc)` 可能是上游回包，`OpenApiError` 里已脱敏，
        #    但传输层异常的正文可能带请求信息 ⇒ 与全仓口径一致，只留类型名 + 那一档人话）。
        detail = _failure_detail(exc)
        reason = _QUERY_FAILED_REASONS[trigger].format(detail=detail)
        _note_order(order=order, kind=KIND_QUERY_FAILED, status="failed", reason=reason)
        logger.warning(
            "查权威状态失败，本轮不写订单（绑定 %s，上家单号 %s）：%s",
            binding.pk, order_no, detail,
        )
        findings.append(reason)
        return _RETRY

    if remote_status is None:
        # 撞了「一轮最多查几条」的上限（本轮只推迟，不丢）。
        reason = _ORDER_CAP_REASONS[trigger].format(cap=queries.cap)
        findings.append(reason)
        return _RETRY

    plan = jidanpu_openapi.plan_openapi_status(order, remote_status=remote_status)
    if plan.route == jidanpu_openapi.ROUTE_REFUND:
        # ⭐ **跟退那一档交既有跟退原语**（D3，L-13）：不再「只登记 + 人工核对」。那一档留在②式意味着**上家退了单、我方状态与钱都停在原地**（打手钱留在钱包里 / 绩效照旧），与①式的自动跟退不对称。
        #    ⛔ 它仍**不走**下面通用的 `_write_status`（`plan.write=False` 是路由不是判据）：钱的回退必须由 `follow_refund` → 既有退单原语那条链办，⛔ 不是裸写状态列。
        return _follow_refund(
            order=order, binding=binding, plan=plan, now=now,
            findings=findings, trigger=trigger,
        )
    if not plan.write:
        # 有结论但**不写**（幂等 / 本线明确不映射 / 认不出来）：
        # 一律留一条台账（认不出来的那一档用 T7 指定的 kind + 四件事实的 payload）。
        payload = None
        if plan.unknown:
            payload = _status_payload(
                plan=plan, order=order, binding=binding, now=now, trigger=trigger,
            )
        #: ⚠️ 两档要分：「认不出来」是**要人看**的（`status="failed"`），
        #: 而「幂等 / 本线明确不映射」是**正常结论**（`status="skipped"`）。
        _note_order(
            order=order, kind=plan.kind or KIND_STATUS_IN,
            status="failed" if plan.unknown else "skipped", reason=plan.reason,
            payload=payload,
        )
        if plan.unknown:
            #: ⚠️ 只有「要人看」那一档进 `findings`（= 绑定 `last_error`）；
            #: 「幂等 / 本线明确不映射」是正常结论，台账已有一行 ⇒ ⛔ 不进错误列。
            findings.append(plan.reason)
            return _NOTED
        return _SKIPPED

    return _write_status(
        order=order, binding=binding, plan=plan, now=now, findings=findings, trigger=trigger,
    )


def _status_payload(*, plan, order, binding, now, trigger: str) -> dict:
    """「认不出来的状态 / 已回写」那条台账的 `payload`（T7 的四件事实 + **触发方式**）。⚠️ 四件事实出自 `jidanpu_openapi.unmapped_status_payload`（⛔ 不在这里重抄一遍键名）；本函数只**外加**一格 `trigger`：台账要能回答「这一笔是回调带进来的，还是兜底对账补的」（两条路共用同一段写入面 ⇒ 光看 kind / reason 分不开）。"""
    data = jidanpu_openapi.unmapped_status_payload(
        remote_status=plan.remote_status, order_no=order.order_no,
        binding_id=binding.pk, queried_at=now,
    )
    data["trigger"] = trigger
    return data


def _consume_one(row, *, binding, line: CallbackLine, queries, now, findings: list[str],
                 legacy: bool = False) -> str:
    """处理**一条**本绑定的回调，回 `_APPLIED` / `_SKIPPED` / `_NOTED` / `_RETRY`。顺序固定（每一步都有 spec 出处，⛔ 别调换）：① 验签没过 ⇒ **绝不改单**（E1/E3 的相邻档，spec §5.4 的 fail-closed）；② 报文读不出来 ⇒ 留痕（⛔ 不猜）；③ 不是 `action=10` ⇒ 交回**这一条线**（`line.other_action`；不设那条钩子就是老口径：留痕）；④ 报文里没有 `orderNo` ⇒ 留痕；⑤–⑧ **按远端单号找链路 → 查权威（B4）→ 判 → 写**：整段交给**这一条线**的 `line.landed`（⭐ 上家那一份在下面 `_upstream_landed`，下家那一份在 `jidanpu_member_openapi`）。
    ⚠️ ①–④ 是**两条线共用**的一段（`line` 只提供台账 kind）：这几步的判据与两条线的业务毫无关系（验签结果 / 报文形状 / action 取值 / 单号在不在），各写一份必然漂。
    ⚠️ **`legacy`** = 这一条来自**老地址** `/jidanpu/v1/notify`（那条例外，见 `consume_callbacks` 的 ③）：它只影响「**找不到单号时**怎么留痕 / 找链路的范围」（`line.landed` 自己的事），⛔ 不影响 ①–④ 任何一条判据；上家线那个入参**恒为 `False`**。"""
    source_ip = str(row.get(FIELD_SOURCE_IP) or "")
    if not row.get(FIELD_SIGN_OK):
        reason = REASON_SIGN_REJECTED.format(
            sign_reason=row.get(FIELD_SIGN_REASON) or "（记录里没写原因）", source_ip=source_ip or "未知",
        )
        line.note_event(
            binding=binding,
            target_ref=f"{binding.pk}:{row.get(FIELD_BODY_SHA256) or 'unsigned'}",
            kind=line.kinds.sign, status="failed", reason=reason,
            payload={
                "sign_reason": row.get(FIELD_SIGN_REASON),
                "body_sha256": row.get(FIELD_BODY_SHA256),
                "source_ip": source_ip,
            },
        )
        findings.append(reason)
        return _NOTED

    params, broken_why = _body_of(row)
    if params is None:
        reason = REASON_UNREADABLE.format(detail=broken_why)
        line.note_event(
            binding=binding,
            target_ref=f"{binding.pk}:{row.get(FIELD_BODY_SHA256) or 'unreadable'}",
            kind=line.kinds.unreadable, status="failed", reason=reason,
            payload={"body_sha256": row.get(FIELD_BODY_SHA256)},
        )
        findings.append(reason)
        return _NOTED

    action = _text_of(params, FIELD_ACTION)
    if action != ACTION_STATUS_CHANGED:
        # ⭐ **这一条线自己办不办**（下家线办 `action=30`：订单有新截图，报文里带着 `imgUrl`）；回 `None` ⇒ 落回老口径（下面那一条痕），上家线就是这一支。
        #    ⚠️ `findings` 照传：**「配不上」**的那几档（找不到链路 / 报文里没有可用的直链）与状态那条路**同源**（该进错误列）；⚠️ **「拉不动」**（下载 / 落库失败）⛔ 不进（照①式那条「拉图失败只记一条台账」的口径）——两档的分界由那一条线自己把关。
        if line.other_action is not None:
            handled = line.other_action(
                binding=binding, params=params, row=row, legacy=legacy, now=now,
                findings=findings,
            )
            if handled is not None:
                return handled
        reason = REASON_OTHER_ACTION.format(action=action or "（空）")
        line.note_event(
            binding=binding, target_ref=f"{binding.pk}:action-{action or 'empty'}",
            kind=line.kinds.other_action, status="skipped", reason=reason,
            payload={"action": action},
        )
        findings.append(reason)
        return _SKIPPED

    order_no = _text_of(params, FIELD_ORDER_NO)
    source_order_no = _text_of(params, FIELD_SOURCE_ORDER_NO)
    if not order_no:
        reason = REASON_UNREADABLE.format(detail="报文里没有 orderNo")
        line.note_event(
            binding=binding,
            target_ref=f"{binding.pk}:{row.get(FIELD_BODY_SHA256) or 'no-order-no'}",
            kind=line.kinds.unreadable, status="failed", reason=reason,
        )
        findings.append(reason)
        return _NOTED

    # ⑤–⑧ 这条线自己的落地面（找链路 → 查权威 → 判 → 写）。⭐ 兜底对账调的是同一个 `line.apply`。
    return line.landed(
        binding=binding, order_no=order_no, source_order_no=source_order_no,
        queries=queries, now=now, findings=findings, legacy=legacy,
    )


def _upstream_landed(*, binding, order_no, source_order_no, queries, now,
                     findings: list[str], legacy: bool = False) -> str:
    """**上家线**（`JidanpuUpstreamBinding`）的 ⑤–⑧ 步：找链路 → 查权威 → 裁决 → 写。⚠️ 这一段**逐字**是加 `CallbackLine` 之前 `_consume_one` 的下半截：⛔ 一个字都不许改（上家那条线**已上线**，它的用例是判据）。
    ⚠️ `legacy` 在这里**恒为 `False`**（上家线的 `legacy_notify` 是 `None` ⇒ 它压根不接管老地址的行；那条例外只属于下家线的**一台机器 / 一个租户**）⇒ ⛔ 别拿它做任何判断。"""
    link = (
        JidanpuUpstreamOrderLink.objects.filter(binding=binding, upstream_order_id=order_no)
        .select_related("order")
        .first()
    )
    if link is None:
        reason = REASON_UNKNOWN_ORDER.format(
            order_no=order_no, source_order_no=source_order_no or "（空）",
        )
        _note_event(
            binding=binding, target_ref=f"{binding.pk}:{order_no[:120]}",
            kind=KIND_UNKNOWN_ORDER, status="failed", reason=reason,
            payload={"order_no": order_no, "source_order_no": source_order_no},
        )
        findings.append(reason)
        return _NOTED

    if link.terminated_at is not None:
        # ⚠️ 这里**不查权威状态**（省一次上游读）：已停跟踪的链路按定义两边都到过终态，
        #    而写入面只开「我方代练中」那一格 ⇒ 查了也写不进去（照 `_is_pullable` 的口径）。
        reason = REASON_NOT_TRACKED.format(order_no=order_no, why=REASON_TERMINATED)
        _note_order(order=link.order, kind=KIND_NOT_TRACKED, status="skipped", reason=reason)
        findings.append(reason)
        return _SKIPPED

    order = link.order
    if getattr(order, "deleted_at", None) is not None:
        # ⚠️ **这道判据是承重的**（不是防御性写法）：`link.order` 走 forward FK 描述符
        #    （`_base_manager`）⇒ 软删单照样取得到，而唯一写入口 `apply_transition` 加锁读的是
        #    默认 manager（已软删单读不到）⇒ 放它往下走会**抛异常**（未预期 500），而不是「静默不写」。
        reason = REASON_NOT_TRACKED.format(order_no=order_no, why=REASON_SOFT_DELETED)
        _note_order(order=order, kind=KIND_NOT_TRACKED, status="skipped", reason=reason)
        findings.append(reason)
        return _SKIPPED

    # ⑥–⑧ 查权威（B4）→ `plan_openapi_status` 裁决（T7：唯一判据出处）→ 写 / 留痕。
    # ⭐ **整段共用**（spec §8.3 ③）：T8 的兜底对账调的是**同一个** `apply_authoritative_status`
    #    ⇒ 「对账那条路多写一列 / 各判一套」在本仓**没有落点**。
    return apply_authoritative_status(
        order=order, binding=binding, order_no=order_no, queries=queries, now=now,
        findings=findings, trigger=TRIGGER_CALLBACK,
    )


def _upstream_candidates(binding) -> list:
    """**上家线**的兜底对账候选（⭐ 判据的唯一出处是 `jidanpu_openapi_reconcile._candidates`）。"""
    from apps.business import jidanpu_openapi_reconcile

    return jidanpu_openapi_reconcile._candidates(binding)


def _upstream_remote_order_id(link) -> str:
    """上家链路上的「对方的单号」= `upstream_order_id`（下家那条线是 `jidanpu_order_id`）。"""
    return link.upstream_order_id


#: ⭐ **上家线**（`JidanpuUpstreamBinding`）在这段引擎里的差异面。⚠️ 它**就是**加 `line` 参数之前引擎里那些语句（⛔ 一个字都没改）：九个字段各自指向本模块既有的那个实现；它同时是兜底对账（`jidanpu_openapi_reconcile`）的默认线。
#: ⚠️ **懒建**（见 `upstream_line`）：`kinds.reconcile_cap` 的唯一出处是兜底对账那个模块，而那个模块 `import` 本模块 ⇒ 在模块级直接引用会**成环**（导入到一半就 AttributeError）。
_UPSTREAM_LINE: CallbackLine | None = None


def upstream_line() -> CallbackLine:
    """上家线的那一份差异面（⭐ 第一次调用时建好，之后复用同一个对象）。⚠️ 为什么是函数而不是模块级常量：`LineKinds.reconcile_cap` 的唯一出处是 `jidanpu_openapi_reconcile.KIND_RECONCILE_CAP`，而那个模块 import 本模块 ⇒ 模块级引用会成环；本对象**无状态**（frozen dataclass，九个字段全是函数 / 常量）⇒ 「建一次缓存起来」与「每次新建」在语义上等价，缓存只为少分配一个对象。"""
    global _UPSTREAM_LINE
    if _UPSTREAM_LINE is None:
        from apps.business import jidanpu_openapi_reconcile

        _UPSTREAM_LINE = CallbackLine(
            name="upstream",
            kinds=LineKinds(
                sign=KIND_SIGN_REJECTED,
                unreadable=KIND_UNREADABLE,
                unclaimed=KIND_UNCLAIMED_CODE,
                other_action=KIND_OTHER_ACTION,
                foreign_path=KIND_FOREIGN_PATH,
                stale_skipped=KIND_STALE_SKIPPED,
                reconcile_cap=jidanpu_openapi_reconcile.KIND_RECONCILE_CAP,
                reconcile_stuck=jidanpu_openapi_reconcile.KIND_RECONCILE_STUCK,
            ),
            binding_model=JidanpuUpstreamBinding,
            payload_key="upstream_binding_id",
            remote_order_id=_upstream_remote_order_id,
            note_event=_note_event,
            landed=_upstream_landed,
            apply=lambda *, order, link, binding, order_no, queries, now, findings, trigger: (
                apply_authoritative_status(
                    order=order, binding=binding, order_no=order_no, queries=queries, now=now,
                    findings=findings, trigger=trigger,
                )
            ),
            candidates=_upstream_candidates,
        )
    return _UPSTREAM_LINE


def _failure_detail(exc) -> str:
    """失败的人话（⛔ 只留类型名 + 一条**根因指纹**，⛔ 不带任何凭据值）。不走 `str(exc)` 的通用路径：`httpx` 那类传输异常的正文可能带请求信息（URL / 头），而本仓口径是「日志 / 台账里只留**类型名**」（`jidanpu_sync` 的四段异常边界同一形状）；⭐ 只留类型名时，**同一个异常类型下的两句不同根因会撞成同一句人话** ⇒ 台账的四元组 `(kind, target_type, target_ref, reason)` 会把**第二条结论**吞掉（`note_once` 的口径是「同一句原因只记一条」，撞成同一句就等于这件事没发生）⇒ 这里补一条**根因指纹**（`str(exc)` 的 sha256 前 8 位十六进制）：**换了根因就换一句人话**（于是各自落一条账），而指纹**推不回原文**。
    ⚠️ **代价（如实登记）**：① 指纹本身是给人「看变没变」的，⛔ 人读不出根因原文——要看原文得去日志（那里也照旧只有类型名）；② 逐字相同的重复失败**仍然只记一条**（那是 `note_once` 的既定口径，⛔ 不是缺陷）；③ 这一支**只对非 `OpenApiError`** 生效——`OpenApiError` 的人话本来就是它自己的 `_text()`（HTTP 状态 + 上游 `code` + transport 类型 + 脱敏后的对方原文，见 `jidanpu_openapi.OpenApiError._text`）⇒ 不同根因本来就落不同的账，再叠指纹只是把同一句人话弄长。
    ⭐ **①档（凭据这一层被拒）要说人话**（spec §9.5 ②，T12 补的那一格）：`_text()` 是给排查用的（「账号/凭据这一层被拒（可能还没开通「开放平台」）」），而 §9.5 ② 要求**把结论写进 `last_error`（人话）** ⇒ 这一档的用户指引只能有一个出处：`OpenApiError.user_text`（§9.3 **逐字**：去开通 / 或改用「账号 + 密码」，并**原样带上对方原文**）⇒ 把那一句接在诊断文本**之前**（⛔ 不替换它：诊断文本里有 HTTP 状态与上游 `code`，删掉就没有排查线索了），再按 `REASON_MAX` 截断；⚠️ **只对①档**换：②档（这一项权限没配）与③档（网络 / 上游故障）走原文——那两句各自就是它们该说的话，⛔ 别顺手一起换（换错方向正是 §9.1 分档要防的事）。"""
    if isinstance(exc, jidanpu_openapi.OpenApiError):
        # `OpenApiError` 构造时已经把密钥 / token 抹掉了（`_redact`）⇒ 它那句话可以用。
        diagnostic = str(exc)
        if exc.kind is jidanpu_openapi.FailureKind.CREDENTIALS:
            return _clip(f"{exc.user_text}｜诊断：{diagnostic}", REASON_MAX)
        return _clip(diagnostic, REASON_MAX)
    return (
        f"{type(exc).__name__}（{_cause_fingerprint(exc)}；我们这边 / 网络的问题，"
        "⛔ 不是对方拒绝了权限）"
    )


def _cause_fingerprint(exc) -> str:
    """`str(exc)` 的**短摘要**（sha256 的前 8 位十六进制）：换一句根因就换一个值。⚠️ ⛔ 不落原文（它可能带请求信息 / 凭据），⛔ 也不落长度 / 前缀那类能反推的信息；用 `hashlib`（⛔ 不用 `hash()`：那个按进程加盐，重启就变 ⇒ 会变成「每次重启灌一条台账」）。"""
    raw = f"{type(exc).__name__}:{exc}"
    return "根因指纹 " + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]


def _follow_refund(*, order, binding, plan, now, findings: list[str],
                   trigger: str = TRIGGER_CALLBACK) -> str:
    """**跟退那一档**的落点（⭐ D3，L-13）：交既有 `follow_refund`。三条出路（判据与登录式上家线 `jidanpu_upstream._follow_refund_row` 同一族，⛔ 不抄第二份判据——这里复用的是**同一个**领域函数，各线的差异只在台账 kind 与人话）：**跟退成功**（`changed=True`）⇒ 状态 → `REFUNDED` 且**钱一起回退**（既有退单钩子办绩效与钱包），台账 `done`，回 `_APPLIED`（这一轮**真的写了订单**）；**幂等**（我方已是「已退单」）⇒ 台账 `skipped`（正常结论，⛔ 不要人看），回 `_SKIPPED`；**被闸拦住**（已结账 **且** `settled_by != "system"`——D-0287 的那把尺子）⇒ 台账 `failed` + `findings`（要人看、状态与钱一个字不动），回 `_NOTED`；**被状态机拒绝**（`IllegalTransition`，多半缺节点）⇒ 台账 `failed` + `findings`，回 `_NOTED`。
    ⚠️ **只接 `IllegalTransition`**：`follow_refund` 里其余异常（数据缺陷的响亮报错，如 `settled_by="system"` 但 `settled_at` 缺失的脏行在回冲幂等键格式化处抛的 `TypeError`）必须**响亮冒出去**（D-0287 ③：脏行不许伪装成「请人工」把已付的钱留在钱包里），⛔ 不许收成一句「跟退失败」。
    ⚠️ 台账 kind 沿用 `KIND_STATUS_IN`（这是「上家说了这张单的状态」那条台账；幂等键是 `(kind, target, 单号, reason)` 四元组 ⇒ 几种结论各占一行、不互相顶掉）；`payload` 带 `trigger`（回调 / 兜底对账）——两条路共用本函数，台账要能回答「这一笔是谁办的」（照 `_status_payload` 的同一条口径）。"""
    try:
        outcome = jidanpu_upstream.follow_refund(order, now=now, binding=binding)
    except IllegalTransition as exc:
        reason = REASON_REFUND_DENIED.format(detail=_clip(str(exc), 120))
        _note_order(order=order, kind=KIND_STATUS_IN, status="failed", reason=reason)
        findings.append(reason)
        return _NOTED
    payload = _status_payload(plan=plan, order=order, binding=binding, now=now, trigger=trigger)
    if outcome.changed:
        reason = _REFUND_FOLLOWED_REASONS[trigger].format(token=plan.remote_status)
        _note_order(order=order, kind=KIND_STATUS_IN, status="done", reason=reason, payload=payload)
        #: ⛔ **写成功不进 `findings`**（照 `_write_status` 的同一条口径：那会把「做成了」
        #: 写进绑定 `last_error` 的**错误列**）。
        return _APPLIED
    if outcome.reason == jidanpu_upstream.ALREADY_REFUNDED_REASON:
        # 幂等：正常结论（台账 `skipped`，⛔ 不进错误列——上一轮已经跟退过了）。
        _note_order(
            order=order, kind=KIND_STATUS_IN, status="skipped",
            reason=outcome.reason, payload=payload,
        )
        return _SKIPPED
    # 被闸拦住（已结账且非系统自动发薪）：状态与钱一个字不动，**要人看**。
    _note_order(
        order=order, kind=KIND_STATUS_IN, status="failed",
        reason=outcome.reason, payload=payload,
    )
    findings.append(outcome.reason)
    return _NOTED


def _write_status(*, order, binding, plan, now, findings: list[str],
                  trigger: str = TRIGGER_CALLBACK) -> str:
    """把权威状态写进我方订单（**同一个**写入口 + 审计；spec §6.3 / §13.4-E1）。⚠️ **E-0022 的判据就在这里**：`transaction.atomic()` 是**事务不是回滚点** ⇒ 块内**不许**用裸 `return` 退出补偿路径（那会把块内前面的写入一起提交成一笔半截账，而且**没有任何异常**）；可预期的拒绝（`IllegalTransition`：边 / 节点 / 陈旧状态）⇒ `transaction.set_rollback(True)` **再**退出；不可预期的（审计写不进去这类）⇒ **不接**，让它响亮地冒出去（原子块整体回滚）。
    ⚠️ `nodes` / `actor` 口径与登录式上家线 `_pull_row` 逐字同形：`nodes_of(binding.user)`（绑定人缺节点 ⇒ 拒绝 + 只登记，⛔ 不静默放行）+ `actor_id=None` 的机器审计行；块的边界**只包住「状态 + 审计」两笔写**：台账留在块**外**（它是留痕，不该跟着业务写回滚）。
    ⭐ **`trigger` 只换人话与台账 `payload`，⛔ 一个字都不碰写面**（spec §8.3 ③）：`apply_transition` 的入参、`nodes`、`actor`、审计行的每一列，回调与兜底对账**逐字相同**（`actor` 还流进 `order.updated_by` ⇒ 它要是两套名字，「写面完全一致」当场就不成立了）；「这一笔是谁做的」如实落在**台账**上（`reason` 措辞 + `payload.trigger`）。"""
    from_status = order.status
    mapped = plan.mapped
    denial: str | None = None
    with transaction.atomic():
        try:
            jidanpu_upstream.apply_transition(
                order, mapped, nodes=nodes_of(binding.user), actor=MACHINE_ACTOR,
                suppress_outbound_sync=True,
            )
        except IllegalTransition as exc:
            # 可预期拒绝 ⇒ 先标记回滚再退出（⛔ 裸 return 会提交半截账，E-0022）。
            transaction.set_rollback(True)
            denial = str(exc)
        else:
            record(
                log_type=ACTION_LOG_TYPES["order.transition"],
                action="order.transition",
                actor=MACHINE_ACTOR, actor_id=None,
                # 机器来源 ⇒ 显式声明（`record()` 的 fail-closed 判据）。
                system=True,
                target_kind="order", target_id=order.order_no, order_no=order.order_no,
                field_name="status",
                from_value=jidanpu_upstream.status_text(from_status),
                to_value=jidanpu_upstream.status_text(mapped),
            )
    if denial is not None:
        reason = REASON_DENIED.format(detail=_clip(denial, 120))
        _note_order(order=order, kind=KIND_STATUS_IN, status="failed", reason=reason)
        findings.append(reason)
        return _NOTED
    reason = _APPLIED_REASONS[trigger].format(
        token=plan.remote_status, target=jidanpu_upstream.status_text(mapped),
    )
    _note_order(
        order=order, kind=KIND_STATUS_IN, status="done", reason=reason,
        payload=_status_payload(
            plan=plan, order=order, binding=binding, now=now, trigger=trigger,
        ),
    )
    #: ⛔ **写成功不进 `findings`**（那会把「做成了」写进绑定 `last_error` 的**错误列**）：
    #: 「写了 N 张」由 `*.text` 的计数回答，单笔的结论由台账那一条 `done` 回答。
    return _APPLIED


def _write_watermark(
    binding, *, model, watermark: datetime | None, last_seen, processed: int, offset: int,
) -> None:
    """把水位 / 「最近收到」/ 累计条数 / **已消费偏移**写回绑定（**收窄写入**，⛔ 不整行 `save()`）。`callback_watermark_at`：水位（`None` = 清零，下一轮整份重读）；`callback_last_at`：最近**真的**收到过一条**这个回调码**的回调（健康列，spec §9.4）——一个回调码下有多行（同一个上家域名的多个打手共用一个码）⇒ 这一格是「这条回调地址上的最近一次」，⛔ 不是「都是本行自己那些单的」（谁的单归谁办见 `line.landed`）；`callback_count`：累计处理过的条数（排查用；⛔ 它不是留痕出口，spec §6.4-③），同一条口径；⭐ `callback_file_offset`：**已消费到的字节偏移**——下一轮从这里往后读（⛔ 不再每轮全扫 198MB）；⚠️ 它与水位**同一次 UPDATE** 落库：分两次写的话，中间崩一次就会出现「水位走了、偏移没走」（只是重读）或者**反过来的静默丢**（偏移走了、水位没走 ⇒ 那些事件再也不会被读到）。
    ⚠️ 顺手把内存里那一行的四个属性对齐（同一轮后面的段读到的是同一个对象）；`model` 是**这一行绑定在哪张表**（`line.binding_model`）：这几列的**列名与语义两条线完全相同**，只差表 ⇒ 只有「写哪张表」这一处是差异（⛔ 不许各写一份字段集合，那必然漂）。"""
    fields: dict = {"callback_watermark_at": watermark, "callback_file_offset": offset}
    if last_seen is not None:
        fields["callback_last_at"] = last_seen
    if processed:
        fields["callback_count"] = F("callback_count") + processed
    model.objects.filter(pk=binding.pk).update(**fields)
    binding.callback_watermark_at = watermark
    binding.callback_file_offset = offset
    if last_seen is not None:
        binding.callback_last_at = last_seen
    if processed:
        binding.callback_count = (binding.callback_count or 0) + processed


__all__ = [
    "ACTION_STATUS_CHANGED",
    "CallbackLine",
    "LineKinds",
    "upstream_line",
    "MAX_ORDERS_PER_ROUND",
    "MACHINE_ACTOR",
    "OUTCOME_APPLIED",
    "OUTCOME_NOTED",
    "OUTCOME_RETRY",
    "OUTCOME_DEFERRED",
    "OUTCOME_SKIPPED",
    "TRIGGER_CALLBACK",
    "TRIGGER_RECONCILE",
    "ConsumeOutcome",
    "ENV_AUTO_SKIP_OUT_OF_RETENTION",
    "apply_authoritative_status",
    "auto_skip_out_of_retention_enabled",
    "consume_callbacks",
    "reset_process_state",
    "status_queries",
]
