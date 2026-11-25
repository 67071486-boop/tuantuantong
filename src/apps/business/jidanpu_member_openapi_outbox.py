"""**下家②式**（官方 Open Platform API）的**出向台账分派层**，**默认关着**（规格=`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md`，计划=`docs/plans/2026-10-09-jidanpu-openapi-write-phase2.md` §3 步骤 2–4，步骤 1=`jidanpu_openapi` 上那五个写方法）。本模块回答一个问题：「②式那一行的在飞出向台账（`member.*`）该由谁办、拿什么凭据办」——⭐ **七档全有明确处置**（`unsupported` 是**空表**）：`member.status_out` ⇒ `ChangeOrderStatus`；`member.launch` ⇒ `CreateOrder`（`recUser=999999999` ⇒ 录单时自动进对方抢单大厅）；`member.assign` ⇒ `CreateOrder`（`recUser=0`，⛔ 不进大厅）；`member.price` ⇒ `ModifyOrderPrice`（⭐ D-0250：实测写的是**大厅/抛单价 `recFee`**；绑定上有①式账号密码时改走桥接价档）；`member.withdraw` ⇒ `ChangeOrderStatus`（`status=已退单`，⛔ 不带 `remark`）；`member.unlist` ⇒ 🚫 **D-0249 起不办**（终态 skip+人话、一次上游都不打，`UNLIST_NOT_AVAILABLE_REASON`）；`member.project` ⇒ `ModifyOrder`（实测必填 5 格，见 `write_project`）。
⭐⭐ **「撤下大厅」在②式下不办（D-0249，修 E-0246）**：官方 API **没有**「撤下大厅/改抛单价」接口（`recUser`/`assignPrice` 只在录单那一刻可传；文档 15 个条目逐条枚举+关键词两路检索 0 命中）。**适用边界（E-0246 教训）**：`member.unlist` 结构上只可能排给**接手人自己的绑定**（queue_assign/queue_claimed/对账 `to_unlist` 全以「接手人=绑定主人」为前提）⇒ 收敛成退单=**把接手人自己店里的那张唯一的活单退掉**（业务方原话：「指派的打手和这个同步过去的鸡蛋铺是同一账号，不应该产生退单问题」）⇒ ②式线上一律**终态 skip+人话、一次上游都不打**；对方那张单**留在对方大厅、未分配、活着**=②式可达的期望形态；对账不再排出差集（防 churn，`jidanpu_member.reconcile`）。⚠️ 两条线「撤下」处置不同（⛔ 别照搬；①式=释放回「未分配」的四跳，逐格对照表见外移登记）；口径差如实登记：按业务方口径①式**也应该**改「已退单」，①式存量单量大 ⇒ 暂不动、只登记。
⭐ **为什么必须有它**：`run_outbox` 原来**无条件**建①式登录连接，而②式绑定**不填账号密码** ⇒ 建连处必抛 `ConfigError` 且看起来像「同步坏了」（E-0202 同形）⇒ 本模块给②式**自己的**取客户端方式，收进 `jidanpu_member.OutboxLine` 那**一份**差异面；引擎判据**一个字都没抄第二份**（推哪三档/来源态例外/推前回读/写后回读为准/挡重认领/录单后必回读/改价必回读钉值/无发单价终态跳过/退单必须回读确认/链路标 `in_hall=False`/记「已经退成」`withdrawn_at`/改项目的空值与弱确认——逐格对照表见外移登记）。⭐⭐ **价档①式桥接（D-0250 定语义）**：改价两段式——①接单价 `price` 格**永远要跟**（写+回读钉值）、②还挂在对方大厅 ⇒ 再补 `recFee` 那一刀并钉值；**bug（E-0247 生产实测）**=桥接线若只在「非在厅」走①式、在厅只写 `recFee` ⇒ 台账 `done` 而接单价停旧值；**D-0250**=有①式凭据时**不分厅态**，`write_price` 一律走①式 `ChangeOrderPrice`+回读钉值，在厅那半句改为真 `rethrow_in_hall`（`ModifyOrderPrice` 改 `recFee`+①式单查钉值）；无①式凭据的纯②式线保持现状（缺口见 `HALL_NOT_APPLICABLE_REASON`）。⛔ **默认关着（硬要求）**：`JIDANPU_MEMBER_OPENAPI_OUTBOX` 没开 ⇒ `run_member_status_outbox` **一次建连都不做、一个字节都不写**（连 `last_run_at` 都不动），只回一句人话；⭐ 判据要能红=关着时上游桩上**一条请求记录都不许有**（含取 token）；⭐ 生产调用方=`jidanpu_sync._run_member_openapi_binding` 的第三段（排在「回调消费 → 兜底对账」之后，顺序理由见那个方法的 docstring）；接线 ⛔ **不等于开闸**，调度层 ⛔ **不自己判那个开关**（两处判据迟早漂，漂的后果是「一个字节都不写」承诺失效）。"""
from __future__ import annotations

import dataclasses
import os
from contextlib import contextmanager
from decimal import Decimal

from jidanpu.errors import ApiError

from apps.business import jidanpu_common, jidanpu_member, jidanpu_openapi
from apps.business.jidanpu_openapi import OpenApiError

#: ⭐ 对方那几个**状态标签**的唯一出处（`已退单` / `未分配`）：它们住在①式的 `jidanpu.py` 里
#: （那是本仓为**这一个产品**的状态词定的常量），②式的单查回的就是同一套中文标签（实测两边都是「未分配 / 代练中 / 已退单 …」）⇒ ⛔ **不在本模块另抄一份字面量**
#: （抄了就会在改标签时只改一边，而症状是**静默认错状态**）。
from apps.business import jidanpu as _jidanpu_labels

# ══ 一、台账 kind（⭐ **唯一出处是 `jidanpu_member`**：⛔ 不在这里另抄一份字面量）══════════
#: 「推状态」那一档：把推送集里的我方态发给对方的改状态端点。
KIND_STATUS_OUT = jidanpu_member.KIND_STATUS_OUT
#: ⭐ **步骤 3 新通的三档**（派单录单 / 指派录单 / 改价）。
KIND_LAUNCH = jidanpu_member.KIND_LAUNCH
KIND_ASSIGN = jidanpu_member.KIND_ASSIGN
KIND_PRICE = jidanpu_member.KIND_PRICE
#: ⭐ **步骤 4 新通的一档 + ⭐⭐ D-0249（修 E-0246）定下的一档**： `member.withdraw`（退单）照旧真推（无条件——我方真退单，对方那份无论什么状态都该终止）； `member.unlist`（撤下大厅）在②式下**不再办**——官方 API 没有不毁单的撤下原语，
#: 「退单」那个落点会毁掉接手人自己店里的活单（E-0246）⇒ 终态 skip + 一句人话（见 `UNLIST_NOT_AVAILABLE_REASON`）。 ⚠️ 这条 kind **仍留在 `IMPLEMENTED_KINDS` 里**：那个集合的语义是「引擎认识它、
#: 分派不会响亮抛」，「办不办」由 `unlist_unavailable_reason` 那一格声明（⛔ **不**把它塞进 `unsupported`：那张表的语义是「还没接线」，而这里是 **接线之后裁定不办**，成因不同、运维要做的事也不同）。
KIND_WITHDRAW = jidanpu_member.KIND_WITHDRAW
KIND_UNLIST = jidanpu_member.KIND_UNLIST
#: ⭐ **新通的一档**（我方改「接手项目」⇒ 对方那张单的**项目名称**那一格）。
#: ⚠️ 与 `KIND_PRICE` 分得清：两个字段、两个端点（改价打 `ModifyOrderPrice`、改项目打
#: `ModifyOrder`）⇒ ⛔ 别按「都是改对方单上的某一格」把它们并成一条。
KIND_PROJECT = jidanpu_member.KIND_PROJECT

#: ⭐⭐ **②式七档全通**（`_unsupported_kinds()` 的「已实现」那一半的**唯一出处**）。 ⚠️ **未通档归零**是**步骤 5 开闸**的前提（计划 §3 步骤 4 的判据）。
#: ⚠️ 七档 = 原六档 + 新加的第七档（`member.project`，走 `ModifyOrder`）。 ⚠️ **D-0249 口径**：`member.unlist` 留在本集合里指的是「引擎认识它」，
#: 不是「真的办」（办不办见 `unlist_unavailable_reason` 那一格）。
IMPLEMENTED_KINDS = frozenset({
    KIND_STATUS_OUT, KIND_LAUNCH, KIND_ASSIGN, KIND_PRICE, KIND_WITHDRAW, KIND_UNLIST,
    KIND_PROJECT, jidanpu_member.KIND_SOURCE_ORDER_NO,
})


# ══ 一·二、推「问题单 / 已暂停」时那一句**固定**的 `remark`（业务方裁定 B）════
#: ⭐ **业务方原话：「b」** —— 在「把我们订单的重要备注发过去」与「固定一句话」之间选了**后者**（原话逐字：`b`）。 ⚠️ 由此定下来的三条（⛔ 别改口径，要改先问业务方）： 1. **是一句固定常量**，⛔ **不拿订单字段拼**（那是没被选中的 A）； 2. **≤ 50 字**（照 `jidanpu_common.WITHDRAW_REMARK` 的风格与那条上限）；
#: 3. ⛔ **只在那两档带**（`问题单` / `已暂停`：文档明写这两档 `remark` 必需，实测不带会被拒）；其余档（含 `已退单`）**照①式不带** —— ①式的推状态历来不传 remark，而退单那一档有自己的逐字原因（`jidanpu_common.WITHDRAW_REMARK`，由**别的**调用点给）。 ⚠️ 文案要写清「**这行字是团团通同步过来的状态变更**」：对方店里的客服看到的就是它，而他们那边没有我们的日志（照 `WITHDRAW_REMARK` 的同一条口径）。
STATUS_REMARK = "由团团通同步过来的状态变更"

# ══ 二、开关（⭐ **默认关着**）═════════════════════════════════════════════════════
#: 环境变量名。取值语义见 `outbox_enabled()`：**只有显式的肯定取值才算开**
#: （⛔ 不是「只要设了就算开」—— 排障时把 `=0` / `=false` 当成「开」是最常见的静默坑）。
ENV_MEMBER_OPENAPI_OUTBOX = "JIDANPU_MEMBER_OPENAPI_OUTBOX"

#: 算「开」的取值（逐字比较，大小写不敏感；`strip()` 之后比）。
TRUTHY = frozenset({"1", "true", "yes", "on"})

#: 开关**关着**时的整轮人话（进 `OutboxStats.blocked_reason`，也由调度那一层打进 stdout）。
BLOCKED_SWITCH_OFF_REASON = (
    "②式出向台账分派**默认关着**（环境变量 JIDANPU_MEMBER_OPENAPI_OUTBOX 没开）："
    "本轮一次上游都不打、一个字节都不写；待发动作留在台账里不动"
)

#: 这一行**不是**②式时的人话（⛔ 与「开关没开」分开：成因不同，运维要做的事也不同）。
BLOCKED_NOT_OPENAPI_REASON = (
    "这一行绑定不是「官方开放平台 API」方式（②式）：②式那套取客户端 / 写接口对它不适用，本轮不跑"
)


def outbox_enabled() -> bool:
    """②式出向分派开了没有（⭐ **默认 `False`**）。 ⚠️ **每次调用都重新读环境变量**（照 `jidanpu_member_openapi.legacy_notify_route_enabled`
    的同一条口径：测试要能在同一个进程里换值，而「缓存过一次就再也不看」是这类开关最常见的静默坑）。 ⚠️ 判据 = `strip()` 之后**逐字**落在 `TRUTHY` 里（大小写不敏感）。
    ⛔ **不是**「设了就算开」：`=0` / `=false` / `=no` / `=off` 一律算**关** ——那些人写下这几个值时的意思是「别跑」，把它们当成「开」会让一次本意是「先关掉」的操作**真的去打上游写单**。"""
    raw = (os.environ.get(ENV_MEMBER_OPENAPI_OUTBOX) or "").strip().lower()
    return raw in TRUTHY


# ══ 三、②式的取客户端（⭐ 本对象存在的首要理由）════════════════════════════════════
@contextmanager
def _spawn_openapi_client(binding):
    """②式的「一轮一次连」：产出 `(OpenApiClient, None)`。 ⚠️ **第二格恒为 `None`**（照 `jidanpu_member.run_outbox` 的既有约定：①式那一格是
    「读上传页隐藏域用的裸 HTTP 客户端」）——②式**当前通的这一档（推状态）一次裸 HTTP 都不打**；真需要它的是步骤 6 的传图（`UploadImg` 走 JSON API，也不需要它）。
    ⛔ 不许为了「凑一个非空值」乱建一条连接（那正是本步骤要消掉的那种白打）。 ⚠️ 凭据不全 / 不是②式 ⇒ `jidanpu_openapi.openapi_client` 自己抛 `ConfigError` （照①式那条约定的既有形状，⛔ 本函数不吞、也不改写它的文案）。"""
    with jidanpu_openapi.openapi_client(binding) as client:
        yield client, None


# ══ 四、`member.status_out` 那两跳的②式实现（判据在共享引擎里）══════════════════════
def _openapi_error_reason(what: str, exc: OpenApiError) -> str:
    """把官方 API 的一次失败翻成**带对方原话**的人话（照 `_upstream_reason` 的同一条口径）。 ⚠️ **为什么必须带上游那句 `msg`**：运维在下家系统里没有我们的日志，台账上那句 `upstream_msg` 是**唯一**能告诉他「是缺权限 / 凭据被拒 / 字段不合法」的东西
    （`jidanpu_member._upstream_reason` 的注释里记着同一条实测教训）。 ⚠️ 顺带把 `FailureKind`（A/B/C 三档）带上：那三档**要做的事完全不同**，而它们的码在台账上长得一样。
    ⛔ **不回显 `str(exc)`**：那里面带 endpoint 与整包 body（既长又会把无关字段写进页面）；凭据脱敏仍由 `jidanpu_openapi` 的 `_redact` 负责（`upstream_msg` 已经过一遍抹除）。"""
    bits = [f"{what}（{exc.kind}）"]
    if exc.upstream_code is not None:
        bits.append(f"上游码 {exc.upstream_code}")
    text = "：".join([bits[0], "、".join(bits[1:])]) if len(bits) > 1 else bits[0]
    if exc.upstream_msg:
        text += f"：{exc.upstream_msg[:120]}"
    return text


def _translate(what: str, exc: OpenApiError) -> ApiError:
    """`OpenApiError` → `ApiError`（⭐ 共享引擎只认后者，⛔ 不为②式在引擎里加第二个 except 分支）。 ⚠️ 两个字段各自承重（`jidanpu_member._upstream_reason` 读它们）：
    `body={"msg": …}` 给「对方原话」、`code` 给「上游业务码」。 ⚠️ 异常**类型名**进 `transport`：官方 API 那边「连不上 / 超时 / 权限」的成因而非业务码，
    只靠 `msg` 有时是空的（那时台账上至少要留下「是哪一类」）。"""
    return ApiError(
        _openapi_error_reason(what, exc),
        body={"msg": exc.upstream_msg or ""},
        code=exc.upstream_code or exc.kind,
        status_code=exc.http_status,
    )


def read_remote_status(client, remote_order_id: str) -> dict | None:
    """②式：**推之前**那次回读 ⇒ 回 `{"orderSta": <status>}`；**读不到 ⇒ 抛 `ApiError`**。 ⚠️ **回的形状与①式那条路同一格**（`orderSta`）：共享引擎的判据是 `_order_sta_token(remote) == label`（取 `orderSta`、去空白、缺失 ⇒ 空串）， ⛔ 不为了②式在引擎里加第二种取法（那就有两个真相源了）。
    ⚠️ 官方 API 的 `GetOrderStatus` 回的是 `data.status`（**中文标签**，与①式 `orderSta` 同一套取值，实测两边都是「未分配 / 代练中 / 已退单 …」）⇒ 逐字放进 `orderSta`。 ⚠️⚠️ **与①式那一格的契约差别（如实登记）**：①式的单查**读不到会回 `None`** （⇒ 照常推），②式这里**读不到抛**（⇒ 记人话 + 退避重试）。⇒ 两边的**行为**在这一档不同：
    ①式对「第一页没找到 / 约 90 天窗外的老单」照推，②式则**不盲目推**（它没有那个「只查第一页」的窗口问题：`GetOrderStatus` 是按单号直查，查不到就是这个单号在对方那儿真的没有）。 ⛔ **别把这一格对齐**：谁想统一，先回答「②式的单查真的没有那个窗口问题吗」—— 答不上来就别动（本步骤**刻意不引入**跟窗口有关的知识）。 ⚠️ ⛔ **对方回「没有相关订单」在这一格也照旧算失败**（⛔ 不许当终态收口）：对方没有这张单 ⇒ 这一发**推不了**，那是「一件该发生的同步没发生」（收口 = 静默放弃同步）。逐档的判据表见 `jidanpu_member_openapi` 模块 docstring 的「同族扫一遍」那一节。"""
    try:
        status = client.get_order_status(order_id=remote_order_id)
    except OpenApiError as exc:
        raise _translate(jidanpu_member.STATUS_PRE_READ_FAILED_REASON, exc) from exc
    return {"orderSta": status.status}


def confirm_remote_status(client, remote_order_id: str) -> dict | None:
    """②式：**写之后**那次回读 ⇒ 回 `{"orderSta": <status>}`；**读不到 ⇒ 抛 `ApiError`**。 ⚠️⚠️ **与 `read_remote_status` 的差别是刻意的，⛔ 别对齐**（见 `StatusPushPrimitives`）： ①式那两格是「同一个调用、引擎按 `None` 分档」；②式这里**读不到就是抛**
    （`_translate` 成 `ApiError`）⇒ 引擎会在 `STATUS_CONFIRM_READ_FAILED_REASON` 那一档收口（「改状态后回读上游出错，本轮无法确认写后的终态」）。 ⭐ 为什么可以不同：**②式的查不到与①式的查不到不是同一件事** —— ①式的单查只查第一页 +
    约 90 天窗，「读不到」大量是**窗口问题**（那些单照推无妨）；②式的 `GetOrderStatus` 是 **按单号直查**，读不到就是「对方那儿真的没有这个单号」（一条都推不得）。 ⛔ 谁想把两边统一，先回答「②式的直查真的没有窗口问题吗」—— 答不上来就别动。 ⚠️ ⛔ **它同样照旧算失败**（刚写成功却说没有这张单 = 自相矛盾 ⇒ 要人看）；逐档的判据表见 `jidanpu_member_openapi` 模块 docstring 的「同族扫一遍」那一节。"""
    try:
        return read_remote_status(client, remote_order_id)
    except ApiError as exc:
        # 上游那一跳报错 ⇒ 原样抛（`_translate` 已带对方 `msg`）：
        # ⚠️ **不改文案**（这里就是「读那一跳报错」那一档）。
        raise exc


def write_remote_status(client, remote_order_id: str, status_label: str) -> None:
    """②式：把中文状态标签写过去（`POST /api/OpenPlatform/ChangeOrderStatus`）。 ⚠️ `status` **收中文标签**，而且文档那份枚举**不全**（实测 `未分配` / `待上号` / `已退单` 也都收）⇒ ⛔ **本函数不按任何白名单校验**（判据的出处是对方，见 `jidanpu_openapi.OpenApiClient.change_order_status` 的 docstring）。
    ⚠️ 只发**远端单号**那一格（`orderId`）：`order_id` / `source_id` **恰好二选一**，而我们这一跳手上拿到的就是链路行的远端单号。 ⭐ **步骤 3：`remark` 那一格有了来源**（业务方裁定 **B**：用**一句固定常量**， ⛔ 不是把我们订单上的重要备注发过去）。**只在那两档带**（`问题单` / `已暂停` —— 文档明写这两档 `remark` 必需），**其余档照①式不带**：
    - 真机依据（步骤 0 实测）：改这两档**不带 `remark`** ⇒ `code=0` 「系统要求问题单必须填写原因」/「请输入备注说明」； - ⛔ `已退单` 那一档**不带**（①式的推状态历来不带 remark；退单那一档有自己的逐字原因，由别的调用点给）—— 判据是「带与不带**各自能红**」，见 `STATUS_REMARK` 的注释。 ⚠️ 判据（收哪些档、带不带）在**这一处**：`jidanpu_openapi.CHANGE_STATUS_REMARK_REQUIRED` 是「对方要求 remark」那件事的**唯一**出处，⛔ 不在这里另抄一份中文字面量。"""
    if status_label in jidanpu_openapi.CHANGE_STATUS_REMARK_REQUIRED:
        body_extra = {"remark": STATUS_REMARK}
    else:
        body_extra = {}
    try:
        client.change_order_status(
            order_id=remote_order_id, status=status_label, **body_extra
        )
    except OpenApiError as exc:
        raise _translate(jidanpu_member.STATUS_FAILED_REASON, exc) from exc


# ══ 五、`member.launch` / `member.assign` 那几跳的②式实现（判据在共享引擎里）════════════
# 二期步骤 3。⚠️ 共享引擎（`jidanpu_member._consume_launch` / `_consume_assign`）判的是：
# 订单在不在 / 有没有发单价 / 缺链路怎么办 / 认领之后要不要补抛 / 事件落什么终态； **本段只回答**「②式的录单怎么打、录完怎么回读取号、挡重预读怎么读、抛单那一跳有没有」。

#: ⛔ **②式建单**：`recUser` 的取值（文档逐字）。 ⭐ 「如果需要在录单时**自动发内部抢单大厅**，则此处传固定值：`999999999`」——
#: 这正是 `member.launch`（派单录单）与 `member.assign`（指派录单）**目标态不同**的那个开关
#: （①式那一侧靠的是**第二次调用** `throw_order`；②式只有录单那一刻能传）。
REC_USER_TO_HALL = "999999999"
#: 「不进大厅」那一档的 `recUser`（文档：「快捷指派打手的用户ID，**默认请传 0**」）。
#: ⚠️ 它与 `jidanpu_order_template.ADD_ORDER_TEMPLATE["recUser"]` **同一个取值**（都是 `"0"`）
#: ⇒ 与①式录单表单发出去的那一格等价。
REC_USER_NOT_TO_HALL = "0"

#: 录单之前那次**挡重预读**失败（读不到对方是否已有这张单）⇒ 不录、下一轮重试。
#: ⚠️ 与①式的 `RECORD_PRE_READ_REASON` **同因不同路**（那句是登录式单查的文案），
#: 但这句要**带上官方 API 的失败档**（②式的失败比①式多一层「哪一档」的信息）。
RECORD_PRE_READ_REASON = "录单前回读「对方是否已有这张单」失败"
#: 录单被上游拒（失败信封）。
RECORD_FAILED_REASON = "鸡蛋铺拒绝了这次录单"
#: ⭐ 录单回**成功**了，但 `data` 里**没有系统订单号**（文档：`data` = 系统订单号）。
#: ⚠️ 这一档不许静默放过：没有订单号 ⇒ 链路落不下去 ⇒ 那一单在我方**永远停在待抢单**，
#: 而重录又会被对方按来源单号拒掉（正是①式注释里那条真机教训的形状）。
RECORD_NO_REMOTE_ID_REASON = (
    "对方回了录单成功、但没给系统订单号（官方 API 的 data 那一格是空的）："
    "本次不重复录单，请人工核对对方那张单"
)
RECORD_READBACK_REASON = (
    "录单后按来源单号（官方 API 的 sourceId）回读查不到这张单，"
    "本轮只保住「已录过」这个事实"
)
RECORD_ID_READBACK_REASON = "录单后无法按对方返回的系统单号确认下家副本，请核对下家订单"
RECORD_ID_MISMATCH_REASON = "来源单号与系统单号查到的创建时间不同，请核对下家订单"
RECORD_ORIGINAL_SOURCE_UNKNOWN_REASON = "旧录单缺少原始来源单号，无法核对下家副本，请人工核对"
#: ⭐⭐ **拆单**（`CreateOrder` 的 `data` 是**逗号分隔**的多个系统订单号）时留下的那条人话。口径 = **取第一段**（⛔ 不是响亮失败）但**必须留痕**，三条理由：链路表只存得下一个远端单号；
#: 拆单是对方正常业务 ⇒ 判成失败 = 一整类正常单永久判死（`recorded` 已落库不重录，每轮失败烧完额度进 `abandoned`，那单永远停在待抢单）；⛔ 绝不静默丢——人话台账写清「拆成 N 张、只认第一张、
#: 其余不被跟踪」。⚠️ 残差：我方对第二张起的单**不做任何动作**（见「残差 ⑤」）。
SPLIT_ORDER_REASON = (
    "对方把这次录单拆成了 {count} 张系统订单（{ids}）：本步骤只认第一张 {first} 并落链路，"
    "其余 {extra_count} 张在本步骤里不会被跟踪（改价 / 改状态都只打到第一张）⇒ 请人工核对"
)
#: ⭐⭐ **拆单在「下游写」这一侧的那半句**—— **唯一一处**，接在下面那三句**弱确认** 人话的末尾（⛔ 不各写一份）。为什么：拆单后链路只存第一张的单号 ⇒ 之后每一发（改价 / 改项目 /
#: 改状态）都只打在第一张上，而「写成功 + `done`」在台账上像"办完了"⇒ 不提拆单 = **静默少做**。 ⛔ 口径照 `SPLIT_ORDER_REASON` **逐字对齐**（不自创一套）；判据（能红）见
#: `tests/test_jidanpu_member_openapi_outbox.py::SplitOrderCaveatTests`（逐字断言三句都含这半句； ⛔ 不写自指断言，E-0024）。
SPLIT_ORDER_CAVEAT = (
    "；⚠️ 若对方在录单时把这一单拆成了多张系统订单，本链路只认第一张，"
    "这一格只改到第一张，其余几张不会被改（拆单那一刻的留痕见录单那一条台账）⇒ 请人工核对"
)
#: ⭐ **挡重预读**读到对方**已经有**这张单 —— 但②式**拿不到**它的系统订单号（`GetOrderStatus` 的回包里没有 `orderId`：真机实测只有那 5 格）⇒ 认领不了
#: （链路那一列要的是系统订单号，而②式**没有**「按来源单号改价 / 改状态」之外的路子）。 ⚠️ 这一档**绝不重录**（重录会被对方按来源单号拒掉，且真录进去就是第二张单）
#: ⇒ 响亮失败 + 说清「请人工去对方系统里查这张单的系统订单号」。
CLAIM_WITHOUT_REMOTE_ID_REASON = (
    "对方已经有这张单（来源单号查得到），但官方 API 的单查回包里没有系统订单号 ⇒ "
    "本次不重复录单、也没法把链路认领下来，请人工在对方系统里查这张单的系统订单号"
)
#: ⭐ ②式**没有**「把已经录进去的单抛进对方抢单大厅」这个动作（文档：`recUser` / `assignPrice` **只在录单那一刻**可传；`ModifyOrder` 的参数表里没有它们）。 ⚠️ 这是**终态跳过**（⛔ 不写成 `failed`：反复重试也变不出一个不存在的接口），
#: 但要**说清怎么办**（照本模块「不许静默少做」的同一条口径）。 ⭐⭐ **同一句话有两个用它的地方**（同一件事实的两面，⛔ 别各写一句）： ① **消费**那一侧（`throw_remote` 回 `THROW_OUTCOME_NOT_AVAILABLE` 时带上它）；
#: ② **对账（排队）**那一侧（`_MEMBER_OPENAPI_LINE.hall_push_unreachable_reason` 就是它） —— 那一侧据此把「已有链路 ⇒ 该在对方大厅」这一档**压根排出差集** （否则每 180 秒排一条注定被终态跳过的 `member.launch` = 死循环；见 `jidanpu_member.reconcile`）。
THROW_NOT_AVAILABLE_REASON = (
    "②式没有「把已经录进去的单抛进对方抢单大厅」的接口（那两个参数只在录单那一刻可传），"
    "本次不动作：要它进对方大厅，请在对方系统里手工操作，或等这张单重新录一遍"
)
#: ⭐⭐ **②式没有「不毁单的撤下」这个动作**（D-0249，修 E-0246）：官方 API **没有**「撤下大厅 / 改抛单价」接口（`recUser` / `assignPrice` 只在录单那一刻可传，文档 15 个条目逐条枚举 + 关键词两路检索 0 命中）；若把「撤下」收敛成已退单，落点是**退单**——`member.unlist` 在结构上只可能排给 **接手人自己的绑定**（queue_assign / queue_claimed / 对账 `to_unlist` 全部以
#: 「接手人 = 绑定主人」为前提）⇒ 那一版退掉的是**接手人自己店里的活单** （E-0246 现场：一批活单被推成已退单、同来源单号挡重救不回）。 ⇒ 按 D-0249：②式线上 `member.unlist` **一律终态跳过、一次上游都不打**；对方那张单 **留在对方大厅、未分配、活着**就是②式可达的期望形态（「停在未分配」在②式不可达）。 ⚠️ **同一句话有两个用它的地方**（同一件事实的两面，⛔ 别各写一句）：
#: ① **分派**那一侧（`jidanpu_member._consume` 的拦截：命中 ⇒ 终态 `skipped`）； ② **对账（排队）**那一侧（`_MEMBER_OPENAPI_LINE.unlist_unavailable_reason` 就是它） —— 那一侧据此把「已接手 + 还在对方大厅」这一档**压根不排出差集** （否则每 180 秒排一条注定被终态跳过的 `member.unlist` = churn；见 `jidanpu_member.reconcile` 的成对收口）。
UNLIST_NOT_AVAILABLE_REASON = (
    "②式没有「不毁单的撤下」接口（撤下只能走已退单，会把接手人自己店里的活单退掉），"
    "本次不动对方那张单：它留在对方大厅、未分配、活着；要撤下请去对方系统手工操作"
)

# ══ 四·二、②式建单的 `gameArea`（⭐⭐ 真机抓到的那条「全量录不过去」）════════════
#: ⭐ **②式建单 `gameArea` 的回落常量**（业务方裁定，原话「走 a」）。⛔ 只有这一处： `_create_body` 的 `game_area` 那一格只许经 `_game_area()` 取本常量，⛔ 别处不许再写字面量。裁定 = 大区取**订单上的大区字段**（`Order.server`），为空 / 取不到 ⇒ 回落本常量。为什么非得改（真机实测）：②式建单发空串 ⇒ 对方拒 `游戏大区为必填项`
#: ⇒ **切成②式后所有新单都录不过去**（单测全绿也照样发生：假上游不校验这一格，E-0227 族）。 ⭐ `"官方服"` 是真机实测过、同端点同游戏能通过的值（⛔ 不是凭「看起来合理」编的）。 ⛔ **①式一个字都不许改**：①式发同一个空值（模板 `""`）且真机录得进去——差别在接口（①式 `AddOrderAjax` 不校验这一格、②式 `CreateOrder` 校验）⇒ 两条线在这一格 **必须是两个不同的值**（判据成对：②式见 `LaunchTests`，①式见 `tests/test_jidanpu_member_consume.py` 里那条钉 `""` 的用例）。
GAME_AREA_DEFAULT = "官方服"


def _game_area(order) -> str:
    """②式建单要发出去的**游戏大区**（⭐⭐ **永不为空** —— 真机那条「全量录不过去」就是发了空串）。 ⭐ 取值口径（业务方裁定「走 a」，逐条见 `GAME_AREA_DEFAULT` 的注释）： 1. **先取订单上的大区**：`Order.server`（我方那个「区服」FK，`null=True` ⇒ **可空**）
    的 `name`； 2. **为空 / 取不到**（FK 为空、或名字是空白）⇒ 回落 `GAME_AREA_DEFAULT`。 ⚠️ 判据用 `order.server_id is None`（本仓既有的 `is not None` 口径，`AGENTS.md` §5-E / E-0021）：
    真值判断看起来一样，但那是「赌这个 FK 将来不会出现 falsy 的真值」。 ⛔ **永不发空串 / `None`**：对方那一格**必填** ⇒ 发空值 = 这一单**永远录不过去** （要能红的判据见 `LaunchTests.test_the_game_area_is_never_empty`）。 ⚠️ 只 `strip()` 用来**判空**与修剪首尾空白，⛔ 不改大区名本身（它是对方的匹配键）。"""
    if order.server_id is None:
        return GAME_AREA_DEFAULT
    name = (order.server.name or "").strip()
    return name or GAME_AREA_DEFAULT


def _game_name(order) -> str:
    """②式发给对方的**游戏名称**那一格（⭐⭐ 建单与改单**共用这一个出处**）。 ## 判据（业务方问的那一格：我方游戏名能不能直接当对方的 `gameName`） **取我方订单上那个游戏的名字**：`Order.game.name`（`Order.game` 是必填 FK ⇒ 正常单上一定有值；取不到时回空串，见下）。 ### 依据（三条，按证明力排序） 1. **真机实测（最硬）**：②式建单的 `gameName` **发的就是本函数这个值** （`_create_body` 与本函数是**同一处**）—— 真机上录单**成功** （`{"code":606917,"msg":"录单成功","data":"…"}`，见 `_remote_order_ids` 的注释） ⇒ **对方认这个名字**，至少对那个游戏成立； 2. **①式在**生产**上跑的就是同一个值**：①式录单的**顶层**参数 `gameName` 也取 `order.game.name`（`jidanpu_common.build_add_order_top_level`），而①式那条路
    在生产上录了 1000+ 张单 ⇒ 这一格不是我们新编的取值； ⚠️ 注意①式**表单里**那个同名键是**游戏 ID**（`str(order.game_id)`）—— 同一个名字两种语义，⛔ 别照抄错了那一份（`build_add_order_form` 的注释）； 3. **文档口径**：`ModifyOrder` 的 `gameName` 逐字是「游戏名称（**要与对方系统里的名称一致**）」⇒ 语义与①式顶层那一格相同。 ### ⛔ 如实登记的残差（与本项目那条 `GAME_AREA_DEFAULT` 事故**同一形状**） **我方游戏名 ≠ 对方游戏名**时（两边各自建过游戏档、名字不完全一致），`gameName` 这一格会被对方拒（或改到别的游戏上）——**今天没有任何映射表**，而"能直接当"这件事的保证只到「与被 `CreateOrder` 接受的那个值**同源**」这一步（上面第 1 条）。处置口径与 `gameArea` **刻意不同**：`gameArea` 是**必填却可能为空**（我方区服 FK 可空）
    ⇒ 回落一个真机实测过的常量；`gameName` **不是空值问题**（`Order.game` 必填），而是**两份名字对不对得上**的问题 ⇒ ⛔ **不许编一个回落常量**（那会把单改到别的游戏上，比失败更坏）。落地口径 = **照发 + 上游拒绝就 `failed` + 退避 + 把对方原话写进台账** （见 `write_project` / `_translate`），并登记为已知缺口（`docs/DECISIONS.md` 的那一条）。 ⚠️ 空值只有一种来源：**`Game.name` 是空串 / 纯空白**（`order.game_id is None` 那种来源 **到不了**这里，`Order.game` 是 NOT NULL 外键；逐条见 `jidanpu_member.order_game_name` 的 docstring）。那时回空串 ⇒ 对方会拒 ⇒ 走 `failed` 那一档（⛔ 不静默、⛔ 不猜一个游戏名出来）。 ⭐⭐ 这一档不走到 `failed` —— 共享引擎 `_consume_project` 在**发之前**用**同一个** `jidanpu_member.order_game_name` 判空 ⇒ **终态 `skipped` + 一句人话**（与空 `project_text` **同一档处置**，见 `PROJECT_NO_GAME_NAME_REASON`）， ⛔ 不再按退避表白烧额度到 `abandoned`。 ⛔ **本函数仍是②式这一份的取值口**（只是实现委托给共享那个函数）：建单与改单两处都调它 ⇒ 「CreateOrder 与 ModifyOrder 的 `gameName` 逐字相等」那条判据照旧成立。"""
    return jidanpu_member.order_game_name(order)


# ── 出向「项目名称」那一格的宽度上限与裁剪（⭐ D1，实现宿主在公共件）───────
#: ⭐ **常量与剪刀的唯一出处已经搬到 `jidanpu_common`**（`PROJECT_TEXT_OUTBOUND_MAX_BYTES` 与 `_clip_project`）：**①式录单表单**（`build_add_order_form`）的 `project` / `remark` 两格也用同一把剪刀（E-0242 只修了②式半边）⇒ 那份定义
#: 不能再只住在②式这里。本模块**原名再暴露**这两个名字（照上面 `KIND_*` 那批同一条「唯一出处 + 本模块别名」的家法；⭐ 同一个对象，⛔ 不是第二份）：建单（`_create_body`）与改单（`write_project`）的调用点、以及钉它们的 `ProjectTests` 用例照旧解析得到，行为一个字不改。⛔ 改上限 / 改裁法请去 `jidanpu_common`（那里有完整的证据登记）， ⛔ 别在这里复制一份。
PROJECT_TEXT_OUTBOUND_MAX_BYTES = jidanpu_common.PROJECT_TEXT_OUTBOUND_MAX_BYTES
_clip_project = jidanpu_common._clip_project


# ── 建单参数的值来源（⭐ 每一格都指向一个**既有出处**，⛔ 不在这里凭空造值）──────────────
# ⚠️ 这张表是本步骤最容易写错的地方，逐格写清「为什么是这个值」： · `gameName` / `project`：与①式录单**同一个出处**（`order.game.name` / `order.project_text`）； ⭐ `gameName` 那一格收进 `_game_name(order)`（建单与改项目**共用一处**，判据与残差见那个函数）。 · `gameArea`：⭐ **订单上的区服 FK**（`Order.server.name`），空则回落 `GAME_AREA_DEFAULT` —— 见上面那一段（⛔ 与①式**刻意不同**：①式照旧发空串）； · `hours` / `customerId`：**我们自己的表里没有对应列**（那是鸡蛋铺录单页的
# 字段）⇒ 取**录单页模板的默认值**（`jidanpu_order_template.ADD_ORDER_TEMPLATE`，它是从真机录单页捕获的**生成物**）—— ⭐ ①式那条路**也是**发这几个值（它的表单就是这份模板）， ⇒ ②式与①式在这两格上**行为等价**，⛔ 不是我们新编的。 ⚠️ `customerId` 另有一条硬事实（spec §12.1-D5 / F18）：**传空串会让对方服务端报「系统异常」**（对方的 bug）⇒ 只能永远传非空值；模板里那个 `'0'` 就是录单页自己的默认值。 · `orderSoures`（订单**来源名称**）：②式**没有**来源列表接口（官方 API 里查不到一列来源） ⇒ 把绑定行上配置的来源名**逐字**发过去（非空才用，空则回落 `SOURCE_NAME_DEFAULT`）—— 与①式「想要匹配的那个名字」**同一个出处**（`binding.source_name`）。 ⚠️ ①式那一侧发的是**匹配到的来源 id**（`match_sources` 的 `value`）：②式没有那个接口，而文档对这一格的说明就是「订单来源**名称**」⇒ 发名字（真机读数已核对）。
_ADD_ORDER_TEMPLATE = jidanpu_common.ADD_ORDER_TEMPLATE

#: 共享那几格的**下划线键名 → 线上 JSON 键名**（⭐ 唯一出处 = `_modify_project_cells` 那一路：
#: `ModifyOrder` 的 `extra=` 需要线上 JSON 名，而 `CreateOrder` 那一路走 `create_order` 的
#: 下划线形参、映射在 SDK 参数表里）。 ⛔ 别在调用方再拼一份 —— 拼错一个键名就是「那一格对方没收到」而**对方照旧回保存成功**。
_SHARED_CELL_JSON_NAMES: dict[str, str] = {
    "hours": "hours",
    "order_soures": "orderSoures",
    "customer_id": "customerId",
    "price": "price",
    "remark": "remark",
}


def _shared_body_cells(*, order, binding, price_text: str) -> dict:
    """`CreateOrder` 与 `ModifyOrder` **共用**的那几格（⭐ 值的出处只有这一处）。 ## 它为什么存在：两个端点发的是**同一批格**（`hours` / `orderSoures` / `customerId` / `price` / `remark`），
    而对方那个改单端点是**整单覆盖**（没发的格被清成默认值，真机实测）⇒ 改单那一发也必须把这几格发全。同一件事写在两处必然漂，而**漂的那一天没有任何用例会红**（两边各自"看起来对"） ⇒ 收成这一个出参。 ⚠️ 本函数**只是那几格的唯一出处**，⛔ 不替调用方决定要发哪些格：
    `_create_body` 多两格（两个价 / `recUser` / 来源单号），`write_project` 多一整张表（见 `MODIFY_ORDER_PAYLOAD_CELLS`）。 ⚠️ 返回的键名用**建单那一套**（`order_soures` / `customer_id` 是下划线写法）—— 映射到 JSON 键名由 :data:`_SHARED_CELL_JSON_NAMES` 负责，⛔ 本函数不拼 JSON。"""
    return {
        "hours": int(_ADD_ORDER_TEMPLATE["hours"]),
        #: ⚠️ `getattr` 兜住「这一发拿不到绑定行」那一档（见 `_binding_of`）：
        #: 处置与「绑定上没配来源名」**同一档**（回落默认来源名），⛔ 不是少发这一格。
        "order_soures": (
            (getattr(binding, "source_name", "") or "").strip()
            or jidanpu_member.SOURCE_NAME_DEFAULT
        ),
        "customer_id": _ADD_ORDER_TEMPLATE["customerId"],
        "price": float(Decimal(price_text)),
        # ⭐ 出向边界裁剪（F-02）：备注与 `project` **同一把剪刀** （`jidanpu_common._clip_project` / `PROJECT_TEXT_OUTBOUND_MAX_BYTES` ＝ 700 字节）——
        # `Order.remark` 是无列宽的 TextField，入向能产出任意长的值；同值两刀必漂（E-0242）。建单（`_create_body`）与改项目（`_modify_project_cells` 的 `remark` / `orderRemark`）
        # 共用本函数 ⇒ 在这一处裁，两条线都受管。
        "remark": _clip_project(order.remark or ""),
    }


def _create_body(*, order, binding, source_order_no: str, in_hall: bool, price_text: str) -> dict:
    """②式录单的**参数表**（⭐ 键名逐字照文档；值来源见上面那一段注释）。 ⚠️ **两个价字段都填「我方发单价」**（业务方裁定 **D-0162** 那条红线的落地）： - `price`（对方口径 = **订单总价**）与 `assignPrice`（对方口径 = **接单价**，落成它的 `recFee`） **都填 `Order.hall_price`** —— 与①式「只发一个价」的行为等价、且**不泄露客户价**；
    - ⛔ **任何一个价字段都不许出现 `Order.total_price` 的值**（那是我方底价，泄露给另一个租户不可逆）。⭐ 这条判据有专门用例（取两价**不同值**的夹具，断言请求体里只有发单价、没有总价）。 ⚠️ `assignPrice > 0` **同时**配 `recUser=999999999` 才会「录单时自动发内部抢单大厅」（文档对这两格写的是同一件事）⇒ 派单/指派的差别**只有 `recUser` 这一格**。
    ⚠️ `hours` 取模板那份**整数**（`ADD_ORDER_TEMPLATE["hours"]`，文档那一格是数字类型）； ⛔ 不拿 `order.time_requirement` 去猜（那是一列自由文本，解析它 = 编一个不存在的约定）。 ⚠️ 金额走 **JSON 数字**（文档的示例是 `"price": 150`）：从 `Decimal` 转 `float` —— 两位小数的十进制值在 `json` 的最短往返表示下就是原值（⛔ 不用字符串糊过去，那会让「数字」变成「文本」，而对方那一格是 number）。"""
    return {
        "game_name": _game_name(order),
        "game_area": _game_area(order),
        # ⭐ 出向边界裁剪（`PROJECT_TEXT_OUTBOUND_MAX_BYTES` ＝ 700 字节，E-0242）：**与改单那一发同一处**
        #    （`write_project` 也走 `_clip_project`）—— 理由与如实登记见那个常量的注释。
        "project": _clip_project(order.project_text or ""),
        # ⭐⭐ 与改项目那一发**共用**的几格（唯一出处 = `_shared_body_cells`）： `hours` / `order_soures` / `customer_id` / `price` / `remark` —— **下划线名**，
        # 与 `create_order` 的形参一一对应（⛔ 这里出的是 kwargs、不是线上 JSON 体；下划线 → JSON 名的映射在 SDK 的参数表与 `_modify_project_cells` 各自那一层）。
        # ⛔ 别把这五格再抄回来：抄一份就是第二个真相源，而两个端点的取值必须逐字一致（对方按它们认这张单）。
        **_shared_body_cells(order=order, binding=binding, price_text=price_text),
        # ⭐ `assignPrice` 与 `price` **不是同一格**（实测：`CreateOrder` 的 `assignPrice`
        #    落成对方的 `recFee` = 大厅那一格）⇒ 建单这一发**两格都发**（与上面共享那几格里的
        #    `price` 同一个值 —— 都是我方发单价，⛔ 绝不泄露客户价）。
        "assign_price": float(Decimal(price_text)),
        "rec_user": REC_USER_TO_HALL if in_hall else REC_USER_NOT_TO_HALL,
        "source_order_id": source_order_no,
    }


def _remote_order_ids(data) -> tuple[str, ...]:
    """建单回包 `data` 里的**系统订单号们**（文档：拆单时**逗号分隔**）⇒ 去空白、丢空段。 ⭐⭐ **远端单号的唯一来源**（真机实测：`CreateOrder` ⇒ `{"code":606917,"msg":"录单成功","data":"20261009044818722526"}`；⛔ **不是**回读拿的
    —— `GetOrderStatus` 的回包里没有 `orderId` 这一格）。 ⚠️ **口径（本步骤定的，见 `SPLIT_ORDER_REASON` 的理由）**：返回**全部**非空段，调用方取 `[0]` 落链路、并把其余段**留痕**（⛔ 不静默丢）。
    ⚠️ 三种形态都算「没给」：`None` / 空串 / 全是空白 ⇒ 回**空元组** （⛔ **不把一个非字符串值当单号**：那是对方改版 ⇒ 响亮失败，见 `RECORD_NO_REMOTE_ID_REASON`）。 ⚠️ 空段（`"a,,b"` / 尾随逗号）**丢掉**：那是我方拼串的常见形态，⛔ 不该被当成一张真单（`"".strip()` 落进链路会造出一行**指不到任何远端单**的链路）。"""
    if not isinstance(data, str):
        return ()
    return tuple(part.strip() for part in data.split(",") if part.strip())


def _priced_text(order) -> str:
    """录单要发出去的**发单价文本**（两位小数，⛔ 绝不回落 `total_price`）。 ⚠️ 走 `jidanpu_common._money_text`（**唯一**的金额格式化出处，与①式那条路同一个函数）；
    `None` ⇒ `"0"` —— ⚠️ 与①式那一格的处置**逐字相同**（那份表单在发单价为空时也落 `"0"`）：「价没同步过去」由下家自己补，而**泄价不可逆**。真正的「没有发单价」在**上游**就被挡下了
    （`member.launch` / `member.assign` 都在 `hall_price is None` 时终态跳过）⇒ 这一档到不了。"""
    return jidanpu_common._money_text(order.hall_price) or "0"


def ensure_remote(event, order, binding, client, *, now, in_hall):
    """确保下家已有这张单。只采信明确无单回执作为新录单的依据。"""
    remote_id, reason, outcome, adopted = _ensure_remote_id(
        event, order, binding, client, now=now, in_hall=in_hall
    )
    if remote_id is not None and jidanpu_member._link_for(binding, order) is None:
        recorded_in_hall = (event.payload or {}).get("recording_in_hall", in_hall)
        link, _created = jidanpu_member._ensure_link(
            binding, order, remote_id=remote_id, in_hall=recorded_in_hall, now=now
        )
        if link is None:
            # ⭐ E-0315 ②式同款防御钉（照①式 `_ensure_remote_order` 的形状）：链行挂在另一张我方单上（同店老单占着同一条远端单）⇒ 建不了链 = 上面那条
            # 「回非空远端单号 ⇒ 链路必已存在」的承重不变量不成立，⛔ 不许带着非空 remote_id 返回（引擎后面几跳按链路列判会判错）——终态收口
            # （`LINK_OCCUPIED_REASON`），调用方按既有 `skipped` 通道收敛事件。
            return None, jidanpu_member.LINK_OCCUPIED_REASON, "skipped", None
    return remote_id, reason, outcome, adopted


def _ensure_remote_id(event, order, binding, client, *, now, in_hall):
    """`ensure_remote` 的主体（⭐ 拆出来只为让上面那条**不变量**只有一处，⛔ 不另立判据）。"""
    payload = dict(event.payload or {})
    penalty_record = jidanpu_member._is_penalty_record(event, order)
    if penalty_record and not jidanpu_member._penalty_record_still_current(event, order, binding):
        return None, jidanpu_member.ASSIGN_STALE_REASON, "skipped", None
    own = jidanpu_member._link_for(binding, order)
    if own is not None:
        return own.jidanpu_order_id, "", "ok", None
    if payload.get("recorded") or payload.get("jidanpu_order_id"):
        if penalty_record and not (
            payload.get(jidanpu_member.PENALTY_OPENAPI_RECEIPT_KEY) is True
            and payload.get("recording_source_order_no") == order.source_order_no
            and payload.get("recording_store") == binding.jidanpu_base_url
        ):
            return None, CLAIM_WITHOUT_REMOTE_ID_REASON, "failed", None
        found = _readback(order, client, payload)
        return (*found, None)
    if jidanpu_member.JidanpuMemberOrderLink.objects.filter(
        binding__jidanpu_base_url=binding.jidanpu_base_url, order=order
    ).exists():
        # 地址级去重：②式与①式**共用同一条口径** —— 同一套鸡蛋铺里已经有这张单，
        # 再录一遍就是第二张（那条链路可能挂在**另一条**绑定上，含已停用的）。
        return None, jidanpu_member.ADDRESS_DUPLICATE_REASON, "skipped", None
    source_order_no = payload.get("recording_source_order_no") or jidanpu_member.effective_source_order_no(order)
    # ── ⭐ 挡重预读：按**来源单号**先查一次（查得到 ⇒ 对方已有这张单，⛔ 绝不建第二张）─────────────
    try:
        found, _ = client.find_order_status_with_absence(
            source_id=source_order_no, require_confirmation=True,
        )
    except OpenApiError as exc:
        return None, _openapi_error_reason(RECORD_PRE_READ_REASON, exc), "failed", None
    if found is not None:
        # ⚠️⚠️ 查得到 = 对方**已经有**这张单 ⇒ ⛔ **绝不重录**（重录会被来源单号拒掉、真录进去就是第二张）。但**认领不了**：链路那一列要的是**系统订单号**，
        # 而官方 API 的单查回包里**没有 `orderId`**（真机实测只有那 5 格） ⇒ 响亮失败 + 人工核对。⭐ 这是**如实登记的残差**（见模块末尾「残差 ①」），
        # ⛔ 不是"忘了写认领"：靠回读 `orderId` 取号认领的写法在真机上**永远拿不到号**。
        return None, CLAIM_WITHOUT_REMOTE_ID_REASON, "failed", None
    if penalty_record and not jidanpu_member._penalty_record_still_current(event, order, binding):
        return None, jidanpu_member.ASSIGN_STALE_REASON, "skipped", None
    # ── 录单（⭐ 失败信封在 `jidanpu_openapi._write` 那一处判：`code <= 0` 一律抛）────────
    had_recording_attempt = bool(payload.get("recording_source_order_no"))
    payload["recording_source_order_no"] = source_order_no
    payload["recording_store"] = binding.jidanpu_base_url
    payload["recording_in_hall"] = in_hall
    jidanpu_member._set_event_progress(event, payload)
    try:
        data = client.create_order(
            **_create_body(
                order=order, binding=binding, source_order_no=source_order_no,
                in_hall=in_hall, price_text=_priced_text(order),
            )
        )
    except OpenApiError as exc:
        if not had_recording_attempt and _create_definitely_rejected(exc):
            payload.pop("recording_source_order_no")
            payload.pop("recording_store")
            payload.pop("recording_in_hall")
            jidanpu_member._set_event_progress(event, payload)
        return None, _openapi_error_reason(RECORD_FAILED_REASON, exc), "failed", None
    # ⭐⭐ **远端单号取自建单回包的 `data`**（真机实测那一格就是对方的系统订单号）。
    ids = _remote_order_ids(data)
    created = ids[0] if ids else ""
    payload["recorded"] = True
    if penalty_record:
        payload[jidanpu_member.PENALTY_OPENAPI_RECEIPT_KEY] = True
    if created:
        payload["jidanpu_order_id"] = created
    if len(ids) > 1:
        payload["recording_split"] = True
    jidanpu_member._set_event_progress(event, payload)
    if len(ids) > 1:
        # ⭐ **拆单**：留痕（⛔ 不静默丢）—— 口径与理由见 `SPLIT_ORDER_REASON`。
        jidanpu_member._note_order_once(
            binding=binding, order=order, kind=event.kind, status="done",
            reason=SPLIT_ORDER_REASON.format(
                count=len(ids), first=created, extra_count=len(ids) - 1,
                ids="、".join(ids[:3]) + ("等" if len(ids) > 3 else ""),
            ),
        )
    found = _readback(order, client, payload)
    return (*found, None)


def _create_definitely_rejected(exc):
    # Token acquisition precedes the write. A denied endpoint cannot start the write.
    return exc.step == jidanpu_openapi.STEP_TOKEN or (
        exc.http_status == 200 and exc.upstream_code == 0
        and exc.upstream_msg in (
            "没有权限", "您没有开放平台接入权限",
            "系统要求来源单号不能为空", "游戏大区为必填项",
        )
    )


def _readback(order, client, payload):
    """Verify the original source and the number returned by CreateOrder."""
    original_source = payload.get("recording_source_order_no")
    source_order_no = original_source or jidanpu_member.effective_source_order_no(order)
    try:
        by_source, _ = client.find_order_status_with_absence(
            source_id=source_order_no, require_confirmation=True,
        )
    except OpenApiError as exc:
        return None, _openapi_error_reason(RECORD_READBACK_REASON, exc), "failed"
    if by_source is None:
        return None, RECORD_READBACK_REASON, "failed"
    remote_id = str(payload.get("jidanpu_order_id") or "")
    if not remote_id:
        return None, RECORD_NO_REMOTE_ID_REASON, "failed"
    try:
        by_id, _ = client.find_order_status_with_absence(
            order_id=remote_id, require_confirmation=True,
        )
    except OpenApiError as exc:
        return None, _openapi_error_reason(RECORD_ID_READBACK_REASON, exc), "failed"
    if by_id is None:
        return None, RECORD_ID_READBACK_REASON, "failed"
    source_created = by_source.creat_time.strip() if isinstance(by_source.creat_time, str) else ""
    id_created = by_id.creat_time.strip() if isinstance(by_id.creat_time, str) else ""
    both_times_known = bool(source_created and id_created)
    same_time = both_times_known and source_created == id_created
    if not original_source and not same_time:
        return None, RECORD_ORIGINAL_SOURCE_UNKNOWN_REASON, "failed"
    # A split can legitimately return a sibling copy on the source lookup.
    if not payload.get("recording_split") and both_times_known and not same_time:
        return None, RECORD_ID_MISMATCH_REASON, "failed"
    return remote_id, "", "ok"


def adopted_verdict(adopted) -> str:
    """②式：认领到的远端单该怎么走（⭐ 三个取值，判据只用**②式读得到的那几格**）。 ⚠️⚠️ **今天在②式下到不了这里**（如实登记，⛔ 别当承重逻辑读）：认领那一支要求回读给出 **对方的系统订单号**，而真机实测 `GetOrderStatus` 的回包**没有 `orderId`** ⇒ `_ensure_remote_id` 在「对方已经有这张单」时**直接响亮失败**、`adopted` 恒为 `None`
    ⇒ 引擎**不会**调本函数（见模块末尾「残差 ①」）。留着它的理由：`RecordPrimitives.adopted_verdict` 是**必需的一格**（共享引擎要它），而它正是「真能认领时该怎么判」的**唯一**出处 —— 将来若有了拿号的路（对方加一格 / 换别的端点），它立刻可用，⛔ 不需要再写一遍判据。 ⚠️ **②式读不到 `isThrowOrder`**（`GetOrderStatus` 只回状态）⇒ 它**永远不会**回
    `ADOPTED_VERDICT_ALREADY_IN_HALL`（⛔ 不许凭「名字里没提大厅」就谎称它已经在大厅里）。 ⚠️ 其余三档与①式那三个**同一个意思**： - `已退单` ⇒ `REFUNDED`（⛔ 绝不补抛 —— 那会把一张已退单重新挂进对方大厅）； - 不是 `未分配` ⇒ `NOT_UNASSIGNED`（可能已被对方打手接走 / 已流转）⇒ 不碰对方那张单； - `未分配` ⇒ `NEEDS_THROW`（走「抛单那一跳」）—— ⚠️ ②式那一跳会如实回 `THROW_OUTCOME_NOT_AVAILABLE`（它没有这个接口）⇒ 最终是一句**说清怎么办**的人话， ⛔ 不是静默、也⛔ 不是一次假装成功。"""
    label = str(getattr(adopted, "status", "") or "").strip()
    if label == _jidanpu_labels.ORDER_REFUNDED_LABEL:
        return jidanpu_member.ADOPTED_VERDICT_REFUNDED
    if label != _jidanpu_labels.ORDER_UNASSIGNED_LABEL:
        return jidanpu_member.ADOPTED_VERDICT_NOT_UNASSIGNED
    return jidanpu_member.ADOPTED_VERDICT_NEEDS_THROW


def throw_remote(client, order, binding, remote_order_id):
    """②式：**这一条线没有**「把已经录进去的单抛进对方大厅」这个动作（如实回一句人话）。 ⚠️ **为什么还要判一次本地链路**：本轮**刚录进去**的那张单（`recUser=999999999`）**已经**
    被对方自动放进他家抢单大厅了 ⇒ 目标态达成、链路也已经按 `in_hall=True` 落定（`ensure_remote` 收的 `in_hall` 就是按这个来的）⇒ 回 `OK`。只有「单**早就在**对方系统里」（认领来的 /
    断点续跑 / 上一次是指派录单）才落进「没有这个接口」那一档。 ⚠️ ⛔ **判据是本地链路那一列**（`link.in_hall`），⛔ 不是「这一轮有没有调过 create」：那一列记的是「对方大厅里此刻有没有这张单」这个**事实**，而它正是①式那一侧同一件事的载体。"""
    link = jidanpu_member._link_for(binding, order)
    if link is not None and link.in_hall:
        return jidanpu_member.THROW_OUTCOME_OK, ""
    return jidanpu_member.THROW_OUTCOME_NOT_AVAILABLE, THROW_NOT_AVAILABLE_REASON


# ══ 五·二、`member.price` 那一档的②式实现（判据在共享引擎里）════════════════════════════
def _binding_has_password(binding) -> bool:
    """这一行绑定上**①式账号密码齐不齐**（桥接价档的闸；缺任何一项 ⇒ 走纯②式现状）。"""
    return bool((binding.account or "").strip()) and bool((binding.password or "").strip())


def _bridged_write_price(client, remote_order_id: str, expected, *, binding) -> None:
    """网页桥接写接单价，凭据来自该订单链路的绑定。"""
    with _jidanpu_labels.open_client_with_http(binding) as (password_client, _http):
        jidanpu_member._member_write_price(password_client, remote_order_id, expected)


def _bridged_read_price(client, remote_order_id: str, *, binding) -> dict | None:
    with _jidanpu_labels.open_client_with_http(binding) as (password_client, _http):
        return jidanpu_member._member_read_price(password_client, remote_order_id)


def write_price(client, remote_order_id: str, expected, *, binding=None) -> None:
    """②式：改价（`POST /api/OpenPlatform/ModifyOrderPrice`）——改的是对方的 **`recFee`**。 ⚠️⭐ **D-0250**：那一格是**大厅 / 抛单价**那一格（实测：建单的 `assignPrice` 与这个端点落的是**同一格**），⛔ **不是**接单价——接单价是远端的 `price` 格（权威证据：`jidanpu.py::change_order_price` 的实测注记「改接单价（`price` 那一格）」）。生产实测（E-0247）写完后远端 `recFee` 变了、`price` 留旧值 ⇒ 「接单价」那半边②式**没有**对应动作——那是 `HALL_NOT_APPLICABLE_REASON` 如实登记的缺口；绑定配上①式账号密码后由桥接价档补齐
    （`_bridged_write_price`，D-0250）。 ⚠️ 金额走 **JSON 数字**（文档示例 `"price": 55`）：`Decimal` → `float`，两位小数的十进制值在最短往返表示下就是原值（与 `_create_body` 同一条）。 ## ⭐⭐ 为什么②式**只能以本端点的成功码为准**（⛔ 不许假装能回读） 1. **回读里没有那一格**：真机实测——`GetOrderStatus` 的回包**恰好 5 格** （`status` / `creatTime` / `operTime` / `doneTime` / `verifyTime`）—— **没有 `recFee`、也没有 `price`** ⇒ ①式那套「写后回读钉值」在②式**读不到东西**；
    2. ⛔ **不许拿一个恒假的判据充数**：「回读 `recFee` == 目标价」这种判据在真机上 `None != 55.00` **恒成立** ⇒ **每一次改价都停在「写后未能确认」**、退避到 `abandoned` （真机实测抓到的就是这个形态）； 3. ⚠️ **如实登记：这一档比①式弱一档** —— ①式能回读到 `price` / `recFee` 那一格、 ②式不能；所以我们只知道「对方回了成功码」，**不知道**那一格真的变成了我们要的价（对方也可能"回了成功但没改"）。⛔ 这句不许省：它是这条线**唯一**的确认口径。 ⇒ 本线的 `PricePrimitives.read_price` 留空（`None`），并配一句 `PRICE_CONFIRM_WRITE_ONLY_REASON` —— 引擎会**每次改价**都留一条台账把它写清楚（同单同句去重 ⇒ 一张单最多一行）。 ⚠️ ⛔ **对方回「没有相关订单」在这一格照旧算失败**（⛔ 不许当终态收口）：改价在这一格 **读不到回读**（只有成功码）⇒ 更没资格把「对方那边没有这张单」当成「价已经同步好了」。逐档的判据表见 `jidanpu_member_openapi` 模块 docstring 的「同族扫一遍」那一节。"""
    try:
        client.modify_order_price(order_id=remote_order_id, price=float(Decimal(expected)))
    except OpenApiError as exc:
        raise _translate(jidanpu_member.PRICE_FAILED_REASON, exc) from exc


#: ⭐⭐ ②式改价的**确认口径**那句话（进 `PricePrimitives.read_confirm_unavailable_reason`）。
#: ⚠️ 它必须写清三件事（⛔ 少一件就等于"什么都不确认就 `done`"而没人知道）： **确认到哪一步**（只到"对方回了成功码"）/ **为什么只能这样**（回包里没有那一格）/
#: **弱在哪**（对方可能回了成功却没改那一格，我们看不见）。
PRICE_CONFIRM_WRITE_ONLY_REASON = (
    "②式的改价只能确认到「对方回了成功码」这一步：官方 API 的单查回包里没有接单价那一格"
    "（真机实测：只有状态与四个时刻）⇒ 无法像①式那样回读钉值；"
    "对方若「回了成功却没真改」我们看不见，请必要时在对方系统里核一眼"
    # ⭐ 拆单那半句（**同一处** `SPLIT_ORDER_CAVEAT`，⛔ 不在这里另写一份）。
    + SPLIT_ORDER_CAVEAT
)


#: ⭐⭐ 桥接价档「大厅那一档」（`_bridged_rethrow_in_hall`，D-0250）的**三句失败人话** （引擎拿它们 `_retry`；成功时一句都不写）。文案口径照①式同族
#: （`PRICE_FAILED_REASON` / `PRICE_CONFIRM_READ_FAILED_REASON` / `PRICE_THROW_FEE_CONFIRM_REASON`），
#: 但点名「大厅那一格」——因为这一段写的不是接单价（接单价归 `_bridged_write_price` 钉）。
PRICE_BRIDGE_RECFEE_FAILED_REASON = "鸡蛋铺拒绝了这次改大厅那一格的价（recFee）"
PRICE_BRIDGE_RECFEE_READ_FAILED_REASON = (
    "改大厅价后回读上游出错，本轮无法确认写后的发单价（recFee）"
)
PRICE_BRIDGE_RECFEE_CONFIRM_REASON = (
    "改大厅价后回读未确认对方那张单的发单价（recFee）已是目标值（写后未能确认）"
)


def _bridged_rethrow_in_hall(client, order, binding, remote_order_id):
    """桥接价档的「大厅那一档」（D-0250）：`ModifyOrderPrice` 直改 `recFee` ⇒ ①式单查**钉值**。签名 / 返回与①式 `_member_rethrow_in_hall` 同形：失败 / 钉不上 ⇒ 回一句人话（引擎 `_retry`），成了 ⇒ 回 `None`。
    ⚠️ 与①式**动作不同**（这里没有「重抛」：②式 `ModifyOrderPrice` 就是真机实测「在厅也写得进 `recFee`」的那个改价口），**判据同形**：写了必须回读钉上 `recFee == 我方发单价`，⛔ 不是「调用没抛异常就算完」（E-0087 同族）。两格与建单同口径（D-0162 两格同值）：`price` 格由 `_bridged_write_price` 写并钉、
    这半段钉 `recFee`。⛔ 不改 `link.in_hall`：改厅内那格的价不动「单在不在大厅」这个事实。 ⚠️ `client` 是②式 `OpenApiClient`（引擎经 `spawn_client` 给的）⇒ `modify_order_price` 用它；回读那一跳**另开**①式登录连接（形参里的 `binding` 就是链路行那个绑定）。"""
    expected = Decimal(order.hall_price)
    try:
        client.modify_order_price(order_id=remote_order_id, price=float(expected))
    except OpenApiError as exc:
        return _openapi_error_reason(PRICE_BRIDGE_RECFEE_FAILED_REASON, exc)
    try:
        with _jidanpu_labels.open_client_with_http(binding) as (password_client, _http):
            row = _jidanpu_labels.fetch_order_by_key1(
                password_client, key=remote_order_id,
                expect_field="orderId", expect_value=remote_order_id,
            )
    except ApiError as exc:
        return jidanpu_member._upstream_reason(PRICE_BRIDGE_RECFEE_READ_FAILED_REASON, exc)
    if jidanpu_member._remote_amount_value(row, "recFee") != expected:
        return PRICE_BRIDGE_RECFEE_CONFIRM_REASON
    return None


#: ⭐ 「大厅那一档在②式下没有对应动作」的那句**人话台账**（引擎在 `rethrow_in_hall is None` 且链路标着「还挂在对方大厅」时写下它——**纯②式线**（无①式凭据）专用；桥接价档有真的
#: `rethrow_in_hall`，不走这句）。⛔ **不许静默少做**。 ⚠️⭐ **D-0250**：纯②式那一个改价端点（`ModifyOrderPrice`）写的是 **大厅 / 抛单价 `recFee`** 那一格 ⇒ 「大厅那一格」其实改得到；**真少做的是另一半**——
#: 引擎契约「接单价 `price` 格永远要跟」在②式**没有对应动作** ⇒ 下家接单价可能留旧值（旧句「本次已把接单价改到位」是把格认错了，原文见分支点提交）。文案要写清**怎么办**：给这行绑定配上①式账号密码（`member_openapi_outbox_line_for` 即选中桥接价档）。
HALL_NOT_APPLICABLE_REASON = (
    "②式只有一个改价接口（ModifyOrderPrice，实测写的是大厅/抛单价那一格 recFee）"
    "⇒ 引擎契约那条「接单价 price 格永远要跟」在②式没有对应动作：本次已把大厅那一格改到位，"
    "接单价 price 格没动，下家接单价可能留旧值；要让接单价跟上，请给这行绑定配上①式账号密码"
    # ⭐ 拆单那半句（**同一处** `SPLIT_ORDER_CAVEAT`，⛔ 不在这里另写一份）。
    + SPLIT_ORDER_CAVEAT
)


# ══ 五·二·二、`member.project` 那一档的②式实现（判据在共享引擎里）══════════════════════
# 背景（业务方报「改了接手项目、下家那边没跟着变」）。⚠️ 共享引擎（`jidanpu_member._consume_project`）判的是：订单在不在 / 项目名是不是空的 / 有没有链路 / 什么算成；**本段只回答**「②式那一发怎么打」。 ⭐⭐ **那一发是「整单覆盖」⇒ 必须发完整载荷（真机第二现场实测）** 现场（**我方自建测试单**，⛔ 从没对业务单打过这个端点）：只发那 5 格 ⇒ 对方回 `保存成功`，
# 而**没发的格被清成对方的默认值** ⇒ 一次改项目把业务单改坏 2 格（`orderSoure`→`'其它'`、 `hours` `8`→`720`；⚠️ 另有 `originPrice` 会被清成 `0.0`，见下面那张表）。官方 API 读不到那几格 ⇒ 靠**①式登录式单查**（能读到 ~82 格）拍快照、逐格 diff 量出来的。 ⚠️ 证据级别：**实测** = 在那张测试单上真机量出来的（读数抄在 `.tools/recon/jidanpu-modify-order/`，不进 git）；⛔ 没有一格是推断。
MODIFY_ORDER_PAYLOAD_CELLS: tuple[tuple[str, bool, str, str], ...] = (
    # (格名, 是否必发, 本侧取值来源, 证据)
    ("orderID", True, "链路那一行的远端系统单号（`link.jidanpu_order_id`）",
     "实测必填：缺它 ⇒ 回 `code=0 msg='系统要求和来源单号不可同时为空'` 那一族"),
    ("souresOrderID", True, "`jidanpu_member.effective_source_order_no(order)`（＝建单那一刻同一个函数）",
     "实测必填：只发 `orderID` ⇒ `code=0 msg='系统要求来源单号不能为空'`"),
    ("gameName", True, "`_game_name(order)`（＝建单那一格同一个出处）",
     "文档参数表上的必需格；空值那一档由引擎先判成终态 `skipped`"),
    ("gameArea", True, "`_game_area(order)`（「永不为空」的既有口径）",
     "实测必填（文档标「可选」）：缺它 ⇒ `code=0 msg='游戏大区为必填项'`"),
    ("project", True, "`_clip_project(order.project_text or \"\")`（出向宽度上限 700 字节，E-0242）",
     "⭐ 本档要改的就是它（整单覆盖 ⇒ 不发它 = 把它清成默认值）"),
    ("hours", True, "`_ADD_ORDER_TEMPLATE[\"hours\"]`（＝建单那一格同一个取值口）",
     "⭐⭐ 实测**不发就变**：只发 5 格 ⇒ `8.0` → `720.0`"),
    ("orderSoures", True, "`binding.source_name`（空则 `SOURCE_NAME_DEFAULT`）（＝建单同一取值口）",
     "⭐⭐ 实测**不发就变**：只发 5 格 ⇒ `'〈绑定上配的来源名〉'` → `'其它'`"),
    ("originPrice", True, "`_priced_text(order)`（＝我方发单价）",
     "⭐⭐ 实测**不发就变**：省掉 ⇒ `33.33` → `0.0`；⚠️ 该端点**忽略** `price`、**写得进** `originPrice`"
     "（发 `price=44.44` ⇒ 远端 `price` 仍 `33.33`、`originPrice` 变 `44.44`）"),
    ("price", True, "`_priced_text(order)`（D-0162：⛔ 绝不发客户价）",
     "实测**被忽略**；与 `originPrice` 是同一个数的一对，同源发出去（少发它的风险已在上一行量到）"),
    ("customerId", True, "`_ADD_ORDER_TEMPLATE[\"customerId\"]`（＝建单同一取值口）",
     "实测**被忽略**；仍发：传**空串**会让对方服务端报「系统异常」（spec §12.1-D5 / F18）"),
    ("remark", True, "`_clip_project(order.remark or \"\")`（＝建单同一取值口，2026-10-29 F-02 起裁）",
     "实测**被忽略**；与建单同源（⚠️ 「零风险」是**短探针**量的——长值从未实测，"
     "现已随建单同一把剪刀裁到 700 字节）"),
    ("orderRemark", True, "`_clip_project(order.remark or \"\")`（与 `remark` 同源同剪刀，F-02）",
     "⭐⭐ 实测**可写**：发 `'TT探针备注甲'` ⇒ 远端 `orderRemark` 真的变成它"
     "（⚠️ 长值上限未实测 ⇒ 出向裁到 700 字节，E-0242 同口径）"),
)


#: 上面那张表里**不发也不会变**的格（实测读数：发一个不同的值，远端**一格都没变**）。
#: ⚠️ 它们**不进**载荷 —— 发一个已知被忽略的格只会让「这一发到底动了几格」更难读。
MODIFY_ORDER_IGNORED_CELLS: tuple[tuple[str, str], ...] = (
    ("itemRemark", "发 `'TT探针条目备注甲'` ⇒ 远端仍是 `''`"),
    ("otherRemark", "发 `'TT探针其它备注甲'` ⇒ 远端仍是 `''`"),
    ("dynamicRemark", "发 `'TT探针动态备注甲'` ⇒ 远端仍是 `None`"),
    ("gameAcc", "发 `'TT-ACC-DEMO'` ⇒ 远端仍是 `None`（我方**取不到真值**，如实登记）"),
    ("zgameAcc", "发 `'TT-ZACC-DEMO'` ⇒ 远端仍是 `None`（同上）"),
    ("accPwd", "发 `'TT-PWD-DEMO'` ⇒ 远端仍是 `None`（⛔ 尤其不发密码类）"),
    ("moblie", "发 `'13800000000'` ⇒ 远端仍是 `None`（我方取不到真值）"),
    ("sphone", "发 `'13800000000'` ⇒ 远端仍是 `None`（同上）"),
    ("customerId≠0", "发 `12345` ⇒ 远端仍是 `0`（⇒ 会员号那一格本侧写不进去）"),
    ("recUser", "发 `\"0\"` ⇒ 既不回大厅、也不改任何一格（在厅 / 不在厅各量一次）"),
    ("assignPrice", "发 `33.33` ⇒ 远端 `recFee` **不变**（`0.0` 仍是 `0.0`）"),
)

#: 上面那张表里**不发也不会变**、但我们**没有单独量过**的那几格（如实登记边界，⛔ 别当已证）： `recFee`（在厅那一发省掉它 ⇒ `33.33` 原样保留 ⇒ 它是**只读**的）、
#: `roleName` / `gameServer` / `gameJob` / `gameGroup`（远端一直是对方的默认值 `'无'` / `'全服'`
#: / `'全部'` / `'全部'`，而**我方订单上没有对应的列** ⇒ 发与不发都改不动它）。

#: `ModifyOrder` 那一发**必须发**的格（**从上面那张表派生，⛔ 不另写一份清单**）——
#: 少任何一格都会让对方把那一格清成默认值（或直接拒），见 `MODIFY_ORDER_PAYLOAD_CELLS`。
MODIFY_ORDER_REQUIRED_CELLS: tuple[str, ...] = tuple(
    name for name, required, _source, _evidence in MODIFY_ORDER_PAYLOAD_CELLS if required
)


def _binding_of(order, binding=None):
    """这一发**该用哪一行绑定**的配置（★ 只有一处判据）。 - 引擎（`jidanpu_member._consume_project`）手上有绑定 ⇒ 直接给它（⛔ 不白查一次库）； - 没给（本模块自己的用例 / 别的调用方）⇒ **从订单的链路行上取**：一单一绑定在
    `project` 这一档的入口就判过了（`_link_for` 找不到链路 ⇒ 引擎终态跳过、到不了这里） ⇒ 这里是「照旧能跑」的兜底，⛔ 不是第二条判据。 ⚠️ 取不到绑定（链路被并发删掉等）⇒ 回 `None`：取值口对 `None` 是安全的
    （`_shared_body_cells` 的来源名那一格回落 `SOURCE_NAME_DEFAULT`，与「绑定上没配来源名」同一档处置），⛔ 不凭空编一个来源名、也⛔ 不因为「读不到绑定」就**少发一格** （少发 = 对方把那格清成默认值，那正是本档要修的破坏）。"""
    if binding is not None:
        return binding
    link = order.jidanpu_member_links.select_related("binding").first()
    return None if link is None else link.binding


def _modify_project_cells(*, order, binding) -> dict:
    """改项目那一发的**完整载荷里、除那 5 格单号 / 游戏 / 项目之外**的那部分。 ⚠️ 只放 `MODIFY_ORDER_REQUIRED_CELLS` 里标「必发」、且**不属于**那 5 格的那些格
    （`hours` / `orderSoures` / `customerId` / `price` / `originPrice` / `remark` / `orderRemark`）—— 每一格的取值来源与证据见 `MODIFY_ORDER_PAYLOAD_CELLS`。
    ⛔ 不在这里新造取值口：共享的那几格走 `_shared_body_cells`，`originPrice` 走 `_priced_text`。"""
    price_text = _priced_text(order)
    cells = {
        _SHARED_CELL_JSON_NAMES[key]: value
        for key, value in _shared_body_cells(
            order=order, binding=binding, price_text=price_text
        ).items()
    }
    #: ⭐⭐ 对方**真的会写**的那一格备注（实测可写；`remark` 那一格实测被忽略）。 ⚠️ 与 `remark` **同一个出处**（`order.remark`）：两格在对方那边都指「备注」，
    #: 发两个不同的值只会让运维分不清哪个是权威 ⇒ 同源。 ⭐ 出向边界裁剪（F-02）：`_shared_body_cells` 里的 `remark` 已在源头裁过 ⇒ 这里拿到的是**已裁值**；`orderRemark` 是对方真的会写的格，
    #: 不裁的长备注整发可能被对方按字节拒 → `failed` → 退避 → `abandoned` （E-0242 当天 6 张卡单同形，只是格子从 `project` 换成备注）。同一把剪刀（`jidanpu_common._clip_project`，⛔ 不在这里另写切片）。
    cells["orderRemark"] = _clip_project(order.remark or "")
    #: ⭐⭐ `originPrice`：**唯一**一格「实测可写、且不发就会被清成 `0.0`」的价格格。取值 = 我方发单价（`_priced_text`，⛔ 绝不用 `total_price`：D-0162 那条红线）。
    #: ⚠️ 如实登记的残差（**这一格的真值我们取不到**）：若有人在**对方系统里**改过它，这一发会把它改回「我方发单价」——本侧读不到、也量不到 ⇒ 见残差 ⑧·续。
    cells["originPrice"] = float(Decimal(price_text))
    return cells
def write_project(client, remote_order_id: str, project_text: str, *, order, binding=None) -> None:
    """②式：把**项目名称**改到对方那张单上（`POST /api/OpenPlatform/ModifyOrder`）。 ## 推的是哪一格 / 打哪个端点 - **我们这一格**：`Order.project_text`（页面上的「**接手项目**」，权威值）； - **对方那一格**：`project`（文档逐字「代练项目 / 标题」）—— 与②式**建单**时发的 `project` 是**同一格**（`_create_body` 的 `project` 也是 `order.project_text`） ⇒ 改单之后对方那一格与重新录一次单的形态一致； ⭐⭐ **两处都经 `_clip_project` 按 `PROJECT_TEXT_OUTBOUND_MAX_BYTES`（700 字节）裁** （理由与如实登记见那个常量的注释）—— ⛔ 不许只裁一边（那会让"录单能过的项目名，改单过不去"，反之亦然）； - **单号**：⭐ **两格都发**（`orderID` ＝ 远端系统单号那一格，**加上** `souresOrderID` ＝ 建单那一刻发的那个来源单号）—— ⚠️ **真机实测与文档不同**：文档写「二者 **至少**一个」，**实际两个都必填**（只发 `orderID` ⇒ 对方回 `code=0 msg='系统要求来源单号不能为空'`）。 ⚠️ `orderID` 这一格是**大写 `ID`**（`ModifyOrder` 是全仓唯一这样拼的端点，别的端点叫 `orderId`），钉在 `jidanpu_openapi.OpenApiClient.modify_order` 里；本函数**不自己拼 body** （⛔ 别在调用方再写一份参数名，那是第二个真相源）。 ## ⭐⭐ 那两格「文档没写 / 写松了」但实际必填的格（**真机实测，E-0227 那一族**）。真机逐格探出来的三发读数（同一张测试单，原话逐字）： | 发出去的格 | 对方回的原话 | |---|---| | ① 只发文档参数表那 3 格（`orderID` / `gameName` / `project`） | `code=0 msg='游戏大区为必填项'` | | ② 补 `gameArea` | `code=0 msg='系统要求来源单号不能为空'` |
    | ③ 五格齐全（`orderID` / `souresOrderID` / `gameName` / `gameArea` / `project`） | `code=1 msg='保存成功'` ★ **通了** | ⇒ ⛔ **文档比实现宽松**（E-0227 的同一形状，**同一族第 N 次**，⛔ 不当偶发）： - **`souresOrderID`**：文档写「`orderID` / `souresOrderID` **至少填一项**」，**实际两项都必填** （只发 `orderID` ⇒ ② 那句）。⭐ 本函数发的那一格**必须与「建单那一刻」发的值同源**：建单走 `_ensure_remote_id` 的 `source_order_no = jidanpu_member.effective_source_order_no(order)` ⇒ `_create_body(source_order_no=…)` ⇒ 请求体的 `souresOrderID`；本函数**逐字用同一个函数** （⛔ 不另取 `order.order_no`、⛔ 不拿链路里的远端单号顶）。判据：同一张单上 `CreateOrder` 与 `ModifyOrder` 两个请求体的 `souresOrderID` **逐字相等**； - **`gameArea`**：文档那一栏标「**可选**」（⚠️ 二期计划 §2.3 那张摘要表里**压根没有它** —— 表格比原始文档还松，而实现者是照那张表写的），**实际必填**（① 那句）。 ⭐ 取值口**复用既有那一个「永不回空」的口径** `_game_area(order)` （＝ `order.server.name`，空则回落真机实测过的 `GAME_AREA_DEFAULT`； ⛔ 不在这里重写一遍取值、⛔ 不发空串）。判据：同一张单上 `CreateOrder` 与 `ModifyOrder` 两个请求体的 `gameArea` **逐字相等**。 ⚠️ **别的格呢**：③ 那一发**恰好只发了这 5 格**就 `保存成功` ⇒ 剩下的文档「可选」格（`gameServer` / `gameJob` / `hours` / `price` / `remark` …）**实测都不需要**。
    ⛔ 这句结论的边界：它只证「这一发、这张单上没有别的必填格」，推不出「任何单都没有」（带值的分支没量过）—— 真机第一次遇到别的「必填」提示时按**对方原话**回来补。 ## ⭐⭐ **这个端点是「整单覆盖」⇒ 必须发完整载荷（真机第二现场）** ⚠️ 上面那句「那 5 格就够了」只回答「**打得过**吗」——它**不回答**「打完之后别的格还在不在」。真机实测（同一张测试单）：只发那 5 格 ⇒ 对方回 `code=1 保存成功`，而**没发的格被清成对方的默认值** ⇒ 一次改项目把单改坏了（`orderSoure` → `'其它'`、`hours` → `720`、 `originPrice` → `0.0`；业务单上实测确认了前两格）。 ⇒ 本函数现在发的是 :data:`MODIFY_ORDER_PAYLOAD_CELLS` 那张表——**逐格都有真机读数**，目标是「**除了 `project`，一格不动**」（两种形态各量一次：`isThrowOrder='Y'` 与 `'N'`）。 ## ⛔ 大厅那一问的实测答案（`recUser` / `assignPrice` 会不会把单推回大厅） ⭐ **不会**，两种形态都量过：发 `recUser="0"` + `assignPrice=33.33` 之后， `isThrowOrder` **原样不变**（`'Y'` 仍是 `'Y'`、`'N'` 仍是 `'N'`），`assTime` / `assUserName` 也一个字没动 ⇒ 这一发**不会把已经不在大厅的单推回去**（那是比现状更坏的破坏）。 ⚠️ 如实登记：`assignPrice` **写不进** `recFee`（发 `33.33` 时远端 `recFee` 仍是 `0.0`）， `recUser` 也没写出任何一格 ⇒ 这两格**今天都不发**（发了也没用，详见那张表）。 ## ⭐ `gameName` 那一格：**与建单同一个出处**（⛔ 不许在这里另取一个值） `ModifyOrder` 的 `gameName` 是**必填** ⇒ 这里逐字用 `_game_name(order)`（判据、真机依据与「我方游戏名 vs 对方游戏名」那条残差**全部写在那个函数的 docstring 里**）。 ⭐ 由此得到一条能红的判据：**同一个订单**上，`CreateOrder` 与 `ModifyOrder` 两次请求体里的 `gameName` **逐字相等**（`tests/test_jidanpu_member_openapi_outbox.py::ProjectTests`）。 ## ⚠️ 确认口径：**只能以本端点的成功码为准**（与改价同一档，理由同一个）。真机实测：`GetOrderStatus` 的回包**只有 5 格**（`status` + 四个时刻），**没有 `project`** ⇒ 改完读不到那一格 ⇒ 本线**不做回读钉值**（`ProjectPrimitives` 干脆没有 `read_project` 那一格）。⛔ 不许伪造一个恒假的回读判据（`None != 目标` 恒成立 ⇒ 每一次改项目都停在「写后未能确认」、退避到 `abandoned`：E-0087 / 真机就是那个形态）。 ⛔ 也不许不确认就 `done` 而台账上不说清楚 ⇒ 那句人话在 `PROJECT_CONFIRM_WRITE_ONLY_REASON`（引擎每次写一条，同单同句去重）。 ## ⛔ 对方拒了怎么办 `modify_order` 内部按**该端点自己实测出来的成功码**判成败（`code > 0`；失败信封抛 `OpenApiError`）⇒ 本函数把它 `_translate` 成 `ApiError`（带对方原话 + 上游码 + 失败档） ⇒ 引擎记 `failed` + 退避重试（⛔ 不静默吞、⛔ 不把失败当成功）。 ⚠️ 「我方游戏名对不上对方系统里的游戏名」那一档正是从这里出来的（对方会回一句拒绝原因，运维在台账上看得见）—— 那是**如实登记**的已知缺口，不是静默。"""
    #: ⭐⭐ 整单覆盖的那几格（`hours` / `orderSoures` / `customerId` / `price` / `originPrice` /
    #: `remark` / `orderRemark`）——**值的出处只有 `_modify_project_cells` 一处**
    #: （它转手 `_shared_body_cells`，与建单那一发逐字同源）。
    modified_cells = _modify_project_cells(order=order, binding=_binding_of(order, binding))
    try:
        client.modify_order(
            game_name=_game_name(order),
            # ⭐⭐ **实测必填**（真机：少这一格 ⇒ `code=0 游戏大区为必填项` ⇒ 每一发都被拒 ⇒ `failed` → 退避 → `abandoned`）——取值口与②式**建单**
            # 那一格**同一个**：`_game_area(order)`（「永不为空」的口径在那一个函数里， ⛔ 不在这里重写一遍取值、⛔ 不发空串）。判据：同一张单上
            # `CreateOrder` 与 `ModifyOrder` 两个请求体的 `gameArea` **逐字相等**。
            game_area=_game_area(order),
            project=_clip_project(project_text),
            order_id=remote_order_id,
            # ⭐⭐ **实测必填**（同上：少这一格 ⇒ `code=0 系统要求来源单号不能为空`）—— 取值口与②式**建单那一刻**发的那个值**同一个**（`_ensure_remote_id` 里 `source_order_no = jidanpu_member.effective_source_order_no(order)`
            # ⇒ `_create_body(source_order_no=…)` ⇒ 建单请求体的 `souresOrderID`） ⇒ ⛔ **不在这里另取一个字段**（如拿 `order.order_no` 或链路里的远端单号顶）：
            # 那会让「建单发的值」与「改单发的值」变成两个真相源，而它们必须是同一个（对方按它认这张单）。判据：改单请求体的 `souresOrderID` 与建单请求体的 `souresOrderID` **逐字相等**。
            source_order_id=jidanpu_member.effective_source_order_no(order),
            # ⭐⭐ **整单覆盖 ⇒ 把必发的那些格一次发全**（真机第二现场；逐格的取值来源与真机证据见 `MODIFY_ORDER_PAYLOAD_CELLS`）。
            # ⛔ 这一大块不是「顺手多带几个字段」：少任何一格，对方就把那一格**清成默认值** （真机实测 `orderSoure` → `'其它'`、`hours` → `720`、`originPrice` → `0.0`）。
            extra=modified_cells,
        )
    except OpenApiError as exc:
        raise _translate(jidanpu_member.PROJECT_FAILED_REASON, exc) from exc


#: ⭐⭐ ②式改项目的**确认口径**那句话（进 `ProjectPrimitives.read_confirm_unavailable_reason`）。
#: ⚠️ 与 `PRICE_CONFIRM_WRITE_ONLY_REASON` 同一条纪律，必须写清三件事（⛔ 少一件就等于「什么都不确认就 `done`」而没人知道）：**确认到哪一步**（只到"对方回了成功码"）/
#: **为什么只能这样**（单查回包里没有那一格）/ **弱在哪**（对方可能回了成功却没改那一格）。
PROJECT_CONFIRM_WRITE_ONLY_REASON = (
    "②式的改项目只能确认到「对方回了成功码」这一步：官方 API 的单查回包里没有项目名称那一格"
    "（真机实测：只有状态与四个时刻）⇒ 无法回读钉值；"
    "对方若「回了成功却没真改」我们看不见，请必要时在对方系统里核一眼"
    # ⭐ 拆单那半句（**同一处** `SPLIT_ORDER_CAVEAT`，⛔ 不在这里另写一份）。
    + SPLIT_ORDER_CAVEAT
)


# ══ 五·三、`member.withdraw`（退单）那一档的②式实现（判据在共享引擎里）════════
# 二期步骤 4。⚠️ 共享引擎（`jidanpu_member._consume_withdraw`）判的是：订单在不在 / 有没有链路 / 写后回读读到什么才算成 / 成了之后链路标什么；**本段只回答**「②式的退单那一发怎么打、写后怎么回读」。 ⭐⭐ **D-0249（修 E-0246）：「撤下大厅」在②式下不由本段办** （把撤下收敛成「已退单」，实测一次调用确实同时撤出了对方的抢单大厅，但落地面是**退单**；
# 而 `member.unlist` 在结构上只可能排给**接手人自己的绑定** ⇒ 那样退掉的是接手人自己店里的活单，见模块 docstring「D-0249」那一节）。②式的 `member.unlist` 今天在**分派处**就终态跳过（`UNLIST_NOT_AVAILABLE_REASON`），⛔ 一次上游都不打。 ⚠️ ①式那条路**不是**这个口径（它是「释放回未分配」）—— 两条线的处置差异见模块 docstring「撤下大厅」那段，⛔ 别把①式的判据（`未分配` + `isThrowOrder=N`）搬到这里： ②式的单查回包**只有 5 格**（`status` + 四个时刻），**没有 `isThrowOrder`**。


def refund_remote(client, remote_order_id: str) -> None:
    """②式：把这张单改成「已退单」（`POST /api/OpenPlatform/ChangeOrderStatus`）。 ⭐ **D-0249（修 E-0246）起本函数只办退单**（官方 API 没有撤下接口；若把「撤下大厅」也收敛到这一发，在「接手人自己的绑定」上退掉的是**接手人自己店里的活单**，E-0246）。 ⛔ **不带 `remark`**：这里传的是 **`remark=None`（整格不发）** —— ⭐ 它就是业务方口径里
    那个「不带」的**落地形态**，而 **`remark=""`（发一个空的键）不是**： ①式那条推状态路用的是「空串也发」（照①式恒发那一格）；本档的请求体被逐字钉成 **恰好两格**（`orderId` + `status`）⇒ 三档取值的出处与理由见 `jidanpu_openapi.OpenApiClient.change_order_status` 的 docstring。
    ⚠️ 判据「带与不带**各自能红**」：带 remark 那一半由 `RemarkTests` 推「问题单」那条钉着，不带那一半由 `WithdrawTests` 里「请求体**恰好**两格」那条钉着。 ⚠️ 失败**抛 `ApiError`**，而**共享 handler 会把这一跳的失败吞掉** （`except ApiError: pass`，照①式的既有形状）：重复推送可能回业务失败（对方原话「当前已是已退单状态，无需更改」）⇒ 成败一律交给**写后回读**判， ⛔ 不解析这里的文案（所以 `what` 用的是既有那句「拒绝了这次改状态」—— 它只是一条给人看的上下文，判据不看它）。"""
    try:
        client.change_order_status(
            order_id=remote_order_id, status=_jidanpu_labels.ORDER_REFUNDED_LABEL,
            remark=None,
        )
    except OpenApiError as exc:
        raise _translate(jidanpu_member.STATUS_FAILED_REASON, exc) from exc


# ══ 五·二、退单那一档的**写后回读**（⭐ 修「对方已无此单 ⇒ 永远退不掉」）════
#: ⭐⭐ 「**对方明确说没有这张单**」那句话（进台账 `reason`；⛔ 不静默）。 ⚠️ 它的**载体**是 `WithdrawPrimitives.absent_means_gone_reason`（判据在共享引擎里）——
#: 这个常量只是那句话本身；谁把它清成空串 ⇒ 「读不到」会**静默**退回「当失败重试」。
ABSENT_MEANS_GONE_REASON = "对方那边已无此单，无需退单"


def read_remote_status_or_gone(client, remote_order_id: str) -> tuple[dict | None, bool]:
    """明确无单时收口撤单，查询报错交给撤单任务退避重试。"""
    try:
        status, confirmed_absent = client.find_order_status_with_absence(
            order_id=remote_order_id, require_confirmation=True,
        )
    except OpenApiError as exc:
        raise _translate(jidanpu_member.WITHDRAW_READBACK_REASON, exc) from exc
    return ({"orderSta": status.status} if status is not None else None), confirmed_absent


# ══ 六、②式**还没通**的那两档（⛔ 显式跳过 + 一句人话，⛔ 不静默、⛔ 不写成 `failed`）════
# ⚠️ 步骤 4 起本表**是空的**：`member.unlist` / `member.withdraw` 两档已经通了 ⇒ 它们的文案（步骤 2/3 那两句「二期还没通」）已随实现删除。
# ⚠️ 机制本身**保留**（`_unsupported_kinds()` 仍在导入时自检恰好集合）：下一个新 kind 落地时，这条纪律照样当场红。


def _unsupported_kinds() -> dict[str, str]:
    """②式还没通的 kind → 人话（⭐ **今天恒为空表**：七档全有明确处置）。 ⚠️ **在模块加载时算一次并自检**（⛔ 不是每次调用都猜）：本表 + `IMPLEMENTED_KINDS` 必须
    **恰好等于** `jidanpu_member.OUTBOX_KINDS`。 - 少一格（新增了一个 kind 却没人登记）⇒ ①式会照旧去办、②式会**响亮抛**
    （`_consume` 的「不认识的台账类型」那一支）——那是对的，但更早发现更好； - 多一格（登记了一个不存在的 kind）⇒ 那条文案是死代码，且会让人以为有一档要接线。 ⇒ 这里直接断言，**导入即红**（而不是等到某一轮跑到那条事件上）。"""
    implemented = set(IMPLEMENTED_KINDS)
    known = set(jidanpu_member.OUTBOX_KINDS)
    if implemented != known:
        raise AssertionError(
            "②式的 kind 登记面与 jidanpu_member.OUTBOX_KINDS 对不上："
            f"少了 {sorted(known - implemented)}、多了 {sorted(implemented - known)}"
        )
    return {}


#: ②式那一份差异面（⭐ 逐格见模块 docstring 开头的七档处置清单）。
_MEMBER_OPENAPI_LINE = jidanpu_member.OutboxLine(
    name="下家②式（官方开放平台API）",
    spawn_client=_spawn_openapi_client,
    status_push=jidanpu_member.StatusPushPrimitives(
        read_remote=read_remote_status,
        confirm_remote=confirm_remote_status,
        write_remote=write_remote_status,
    ),
    record=jidanpu_member.RecordPrimitives(
        ensure_remote=ensure_remote,
        adopted_verdict=adopted_verdict,
        throw_remote=throw_remote,
        #: ⛔ **不给** A 段（传图）：②式的传图是**步骤 6**（官方 API 只有 `UploadImg` 一条路，
        #: 而它要的图 URL / 说明与①式那一套隐藏域完全不同）⇒ `None` = 引擎记一句人话台账，
        #: ⛔ 不静默、也⛔ 不会像①式那一份那样因 `http is None` 而炸（见 `_push_screenshots_of_line`）。
        push_screenshots=None,
    ),
    price=jidanpu_member.PricePrimitives(
        write_price=write_price,
        #: ⭐⭐ ⛔ **`None`**：②式的单查回包里**没有接单价那一格**（真机实测）⇒ 回读钉值做不到
        #: ⇒ 引擎**只以写端点的成功码为准** + 每次改价留一条 `PRICE_CONFIRM_WRITE_ONLY_REASON`
        #: 台账（同单同句去重）。理由与代价见 `write_price` 的 docstring（⛔ 别改回"假装能回读"）。
        read_price=None,
        read_confirm_unavailable_reason=PRICE_CONFIRM_WRITE_ONLY_REASON,
        #: ⛔ **`None`**：②式没有「大厅那一档」（只有一个改价端点）⇒ 引擎会写下
        #: `HALL_NOT_APPLICABLE_REASON` 那条人话台账（⛔ 不静默少做）。
        rethrow_in_hall=None,
        hall_not_applicable_reason=HALL_NOT_APPLICABLE_REASON,
    ),
    # 官方回读没有大厅标记，只有明确无单或已退单能作为汇总依据。
    withdraw=jidanpu_member.WithdrawPrimitives(
        refund_remote=refund_remote,
        read_remote=read_remote_status_or_gone,
        absent_means_gone_reason=ABSENT_MEANS_GONE_REASON,
    ),
    #: ⭐ 改「接手项目」那一档（`ModifyOrder`）。 ⚠️ 这一份**只有 `write_project` 一格**：②式的单查回包里**没有** `project`
    #: （真机实测只有 5 格）⇒ 回读钉值做不到 ⇒ `ProjectPrimitives` 干脆不留那一格（⛔ 不留一个没有调用方、也没有用例覆盖的分支），配一句必填的
    #: `read_confirm_unavailable_reason`（引擎每次写一条台账把它说清楚，⛔ 不许静默）。
    project=jidanpu_member.ProjectPrimitives(
        write_project=write_project,
        read_confirm_unavailable_reason=PROJECT_CONFIRM_WRITE_ONLY_REASON,
    ),
    #: ⭐⭐ **D-0249（修 E-0246）**：②式**没有**「不毁单的撤下」原语 ⇒ 这条线上 `member.unlist` **一律终态 skip + 这句人话、一次上游都不打**（分派处拦截，
    #: 见 `jidanpu_member._consume`）；对账据此**不排**撤下（防 churn，成对收口见 `jidanpu_member.reconcile`）。为什么：官方 API 没有撤下接口，而「撤下收敛成已退单」
    #: 在「接手人自己的绑定」上退掉的是**接手人自己店里的活单**（E-0246 现场）；「停在未分配」的期望形态在②式不可达，可达的形态 = **留在对方大厅、未分配、活着**。 ⛔ **①式不设这一格**：它有真撤下原语（`release_order_to_home` 四跳），行为一个字不变。
    unlist_unavailable_reason=UNLIST_NOT_AVAILABLE_REASON,
    #: ⭐⭐ **「把已经有链路的单推进对方抢单大厅」这一档在②式下不可达**（开闸前堵的那个环）：`recUser` / `assignPrice` **只在建单那一刻**可传 ⇒ 建单之后 **没有任何接口**能把一张已经录进去的单推进对方大厅。
    #: ⇒ 对账**不该把这一档排出来**（排了必然被消费侧终态跳过，而每 180 秒一轮 ⇒ 每轮又排一条 `member.launch`，终态 `skipped` 只挡重试、⛔ 挡不住重排）。 ⚠️ 取值就是 `throw_remote` 那一侧用的**同一句话**（同一件事实的两面，
    #: 见 `THROW_NOT_AVAILABLE_REASON` 的注释；`tests/test_jidanpu_member_openapi_outbox.py` 有一条用例钉住两面**逐字相等**）。 ⛔ **①式不设这一格**：它真能抛进大厅 ⇒ 期望可达，行为一个字都不许变。
    hall_push_unreachable_reason=THROW_NOT_AVAILABLE_REASON,
    unsupported=_unsupported_kinds(),
    # ⭐ **默认关着**：`enabled` 回 `False` ⇒ `jidanpu_member.run_outbox` **一次上游都不打、
    # 一个字节都不写**（连健康列都不动），只回 `blocked_reason` 那句人话。
    # ⚠️ 开关的读取方式就住在这里（`outbox_enabled`）——⛔ 引擎里不写任何环境变量名。
    enabled=outbox_enabled,
    blocked_reason=BLOCKED_SWITCH_OFF_REASON,
    uses_openapi=True,
)


#: ⭐⭐ **桥接价档的那一份②式线**（**D-0250 定语义**）：与 `_MEMBER_OPENAPI_LINE` **只有价档不同**——三格与①式**同形**（引擎契约照办）： `write_price` 不分厅态一律①式 `ChangeOrderPrice`（接单价 `price` 格）、 `read_price` 恒读 `price` 钉值、`rethrow_in_hall` **是真的**（在厅时 `ModifyOrderPrice`
#: 直改大厅那一格 `recFee` + ①式单查回读钉 `recFee == 我方发单价`——保留真机实测「在厅 recFee 可改」的能力）。两格都钉成我方发单价（与建单 D-0162「两格同值」衔接）。其余六档**逐字同一份常量**（⛔ 不复制第二份——§5-C）。选线在 `member_openapi_outbox_line_for(binding)`（按绑定行上**有没有①式账号密码**）。
#: ⚠️ 用 `dataclasses.replace` 派生（⛔ 不手抄字段）：那会把 `unlist_unavailable_reason` 这类「不记得抄就静默丢」的格子漏掉（D-0249：非空 = ②式 unlist 终态 skip、零上游）—— frozen dataclass 正好配 replace。 ⚠️ `hall_not_applicable_reason` 留缺省空串：这一条线**有** `rethrow_in_hall`，引擎只在 `rethrow_in_hall is None` 时才读那句话（空着也永远不会被读到；真纯②式那份有自己的非空文案）。
_MEMBER_OPENAPI_LINE_BRIDGED = dataclasses.replace(
    _MEMBER_OPENAPI_LINE,
    name="下家②式（官方开放平台API · 价档①式桥接）",
    price=jidanpu_member.PricePrimitives(
        write_price=_bridged_write_price,
        read_price=_bridged_read_price,
        #: ⭐ D-0250：在厅那一档**是真的**——`ModifyOrderPrice` 直改 `recFee`（真机实测在厅
        #: 写得进）＋ ①式单查回读钉 `recFee == 我方发单价`
        #: （⛔ 不是「无需重抛」+ 一句台账的空话，E-0247）。
        rethrow_in_hall=_bridged_rethrow_in_hall,
    ),
)


def member_openapi_outbox_line() -> jidanpu_member.OutboxLine:
    """②式那一份差异面（无①式凭据的绑定用它；测试也直接拿它比对①式那一份）。"""
    return _MEMBER_OPENAPI_LINE


def member_openapi_outbox_line_for(binding) -> jidanpu_member.OutboxLine:
    """**这一行②式绑定**该用的差异面（价档桥接；D-0250 定语义）：有①式账号密码 ⇒ 桥接价档。 ## 判据与边界（⛔ E-0202 家族的教训写在这里） - **渠道族仍由 `mode` 决定**（本函数只服务 `mode=openapi` 的行；①式行走 `_MEMBER_LINE`，那个判据在 `outbox_line_for`）⇒ 不存在「第二套渠道判据」。
    - **本函数唯一的分叉 = 绑定行上①式账号密码齐不齐**（`_binding_has_password`）：齐 ⇒ 价档照**引擎契约**走（`ChangeOrderPrice` 写接单价 `price` 格 + 回读钉值，在厅再由 `_bridged_rethrow_in_hall` 补 `recFee` 那一格并钉值，D-0250）；
    不齐 ⇒ 原样纯②式（只改得到大厅那一格 `recFee`，接单价 `price` 格没动、台账照旧写明缺口与怎么办）——⛔ **不因缺凭据而报错**，那是把「配置不全」upgrade 成「功能故障」。 - ⭐ 桥接价档的定位（D-0250）：它履行的就是引擎契约本身，**不再是**「等对方 API 支持非在厅改价后回切纯②式」的临时桥（D-0223 那半句随之作废）；可回切的前提改为「对方官方 API 有了能写接单价 `price` 格、且单查回读得到那一格的端点」。"""
    if _binding_has_password(binding):
        return _MEMBER_OPENAPI_LINE_BRIDGED
    return _MEMBER_OPENAPI_LINE


# ══ 七、入口（⭐ 本模块唯一的对外函数；⭐ 步骤 5 起由调度层调它）═════════════════════════
def run_member_status_outbox(binding, *, now, client=None) -> jidanpu_member.OutboxStats:
    """**②式的一轮出向台账**（默认关着）：先过闸，过了才交给共享引擎。 ⚠️⭐ **名字里的 `status` 是历史**（步骤 2 时它只办「推状态」）：**今天它办六档 + 一档终态收口**（推状态 / 派单录单 / 指派录单 / 改价 / 退单照旧真办； `member.unlist` 在分派处终态 skip（**D-0249**：官方 API 没有不毁单的撤下原语，见 `UNLIST_NOT_AVAILABLE_REASON`）—— 名字**不改**，因为二期计划 §3 **步骤 5** 那一行写的就是这个名字（改它要**同一次**改计划，属步骤 5 的接线活儿）； ⛔ **别按名字读它的职责**。
    三条闸（顺序承重，⛔ 别调换）： 1. `binding.enabled` 为假 ⇒ `paused=True`（**照共享引擎的同一条口径**，⛔ 不自己写第二份判据：它下一句就会判同一件事）； 2. **不是②式**（`mode != "openapi"`）⇒ 一句人话、**一个字节都不写、一次上游都不打**。 ⚠️ 判据用 `jidanpu_openapi.is_openapi_binding`（**唯一**的 `mode` 出处）， ⛔ 不写 `if binding.client_id:` 那种真值判据（切回①式之后 `client_id` 还在 ⇒ 静默走错路）； 3. ⭐ **开关关着** ⇒ 由**共享引擎**那一道 `line.enabled` 闸判（本函数⛔ 不自己再判一遍：
    两处判据 = 迟早漂，而这一处漂的后果是「一个字节都不写」这条承诺失效）。 ⚠️ 闸关着时**连建连都不做**：`spawn_client` 在引擎里排在闸**之后** （见 `jidanpu_member.run_outbox` 的顺序）。 ⚠️ **闸关着时 `last_run_at` 也不动**（本函数与引擎都不调 `_stamp_health`）：⭐ 站得住脚的理由 —— 那一列的语义是「**这套流程**上次什么时候跑的」，而开关关着时这套流程**没在跑**；拿它当「进程活着」的心跳会与①式那条在跑的路混成一句话（①式与本函数**共用同一行绑定表**）。 ⛔ 代价如实登记：页面上的「上次运行」在这一行上会一直停在开关打开之前的那一刻 —— 那正是真话（步骤 5 接线之后，那一行的段**每一拍都会被调**，但只要开关没开，这里就一个字节都不写、`last_run_at` 也照旧不动；开了闸它自然就开始盖了）。 ⚠️ `client` 只为可测性注入（照共享引擎的同一条约定）；不给 ⇒ 由②式自己的 `spawn_client` 建一次连（**一轮只建一次**）。"""
    if not binding.enabled:
        return jidanpu_member.OutboxStats(paused=True)
    if not jidanpu_openapi.is_openapi_binding(binding):
        return jidanpu_member.OutboxStats(blocked_reason=BLOCKED_NOT_OPENAPI_REASON)
    return jidanpu_member.run_outbox(
        binding, now=now, client=client,
        line=member_openapi_outbox_line_for(binding),
    )


# ══ 八、残差（如实登记，⛔ 不是「已做」）══════════════════════════════════════════════
# ⚠️ ① ⭐⭐ 实测已落定：`GetOrderStatus` 回包 `data` **恰好 5 格**（`status` + 四个时刻）—— **没有 `orderId` / `price` / `recFee`**（两种查法键集合完全一样）。三条后果（⛔ 别按文档猜回那三格）：远端单号**只从建单回包 `data` 来**（回读取号 / 比号都不存在，后者恒假）；⛔ **挡重认领这一支今天到不了**（拿不到对方系统订单号 ⇒ `CLAIM_WITHOUT_REMOTE_ID_REASON`：响亮失败 + 人工核对， ⛔ 绝不重录；那一单停待抢单烧到 `abandoned`——这是**能力缺口**不是漏做：官方 API 没有「按来源单号拿系统单号」的端点，要不要补真机跑完再定）；改价回读钉值②式自己做不到（⭐ D-0250：有①式凭据的行走桥接价档钉值；纯②式只以成功码为准，比①式弱一档）。 ⚠️ ② 本模块没有「重启后重放」之类的自愈：待发动作留在台账不动，由调度那条闸决定何时跑（业务方口径：先加能力、后接线、最后开闸）。 ⚠️ ③ 「抛进对方大厅」在②式没有对应动作（接口缺口，`recUser` / `assignPrice` 只在录单那一刻可传） ⇒ 消费侧终态 `skipped` + 人话。⭐⭐ **排队那一侧同样要收**（开闸前堵死循环）：对账每 180 秒还会按「该在对方大厅」把这一档排出来，而终态 `skipped` 只挡重试、⛔ 挡不住重排 ⇒ 本模块那条线设了 `hall_push_unreachable_reason` ⇒ 对账对「已有链路 + 不在大厅」**一张都不排** （判据落点 `jidanpu_member.reconcile`；⛔ ①式不设这一格——它真能抛，行为一个字未变）。 ⚠️ ④ A 段（派单传图）没通（`push_screenshots=None`）：有待传截图时留一条 `KIND_ASSIGN` 的 `done` 人话台账（⛔ 不静默、不挡派单）。那是步骤 6。 ⚠️ ⑤ ⭐ 拆单口径 = 取第一段 + 留痕（`SPLIT_ORDER_REASON`）。残差：第二张起的单我方**不做任何动作**、对方多出的几张在我方不可见 ⇒ 真机第一次真拆单后按读数定要不要换口径（换口径只对之后的单生效）。⭐⭐ 那三句弱确认人话末尾都带 `SPLIT_ORDER_CAVEAT`
# （同一处、同一个口径，⛔ 不是三份各写一遍）。 ⚠️ ⑥ ⭐⭐ **②式建单必填九格里，`project` 这一格今天没有兜底**（逐格核出的残差）：其余八格各有一条闸（`gameName`←`Order.game` NOT NULL；`hours`/`customerId`←模板常量；`recUser`←本模块常量；`orderSoures`←`SOURCE_NAME_DEFAULT`；两价是数字且引擎在 `hall_price is None` 时先终态跳过）。 ⛔ `project` 发 `_clip_project(order.project_text or "")`（出向裁剪，E-0242；⛔ 不改「空值不兜底」），而该列 `blank=True`、「游戏 + 项目必填」那道闸只覆盖录单那两条路—— **另有四处写单点不走它**且都写 `… or ""`（`peer_inbound.py` 入站建单 / 入站更新、 `jidanpu_upstream.py` 上家单转正两处；读码结论，⚠️ 可达性未真机验证：下手前先量「这类单会不会被抛进大厅」）。⛔ **要改先问业务方**（业务方只裁定了大区那一格；编出来的项目名会写进对方真单）。影响面小于 `gameArea` 那条（现成判据 = `test_jidanpu_member_openapi_outbox.py` 录单那几条，钉的是值）。 ⚠️ ⑦ ⭐⭐ `Game.name` 可以是**空串**（历史行 / 手造行能长这样）⇒ `_game_name` 回 `""` ⇒ 必被拒 ⇒ `failed` → `abandoned`（单测抓不到：假上游不校验）。✅ 改项目那一档已收成终态 `skipped` + 人话（`PROJECT_NO_GAME_NAME_REASON`；判据 = `ProjectTests::test_an_empty_game_name_is_a_terminal_skip_without_any_upstream`）。 ⛔ 录单那两档今天仍是 `failed` + 退避（改成终态跳过会动既有六档行为面 ⇒ 先问编排方 / 业务方）。可达性很低（读码结论，未真机验证）。 ⚠️ ⑧ ⭐⭐ **`ModifyOrder` 的「文档 vs 实际」（真机实测，⛔ 别再照文档读）**：三发读数（只发 3 格 ⇒ `游戏大区为必填项`；补 `gameArea` ⇒ `来源单号不能为空`；五格齐全 ⇒ `保存成功`）与整单覆盖逐格语义（`orderSoure`→`'其它'`、`hours`→`720`、`originPrice`→`0.0`）见外移登记与 `jidanpu_openapi.modify_order` 的 docstring；现状 = 请求体补齐恰好 5 格 + 发完整载荷（`MODIFY_ORDER_PAYLOAD_CELLS`）。E-0227 同一族第 N 次，⛔ 别当偶发。 ⚠️ 确认口径照旧弱一档（单查里没有 `project` 格 ⇒ 只以成功码为准）；「这一发上没有别的必填格」 ⛔ 推不出「任何单上都没有」。⚠️ 本侧取不到真值的残差：`originPrice` 若被人在对方系统里改过，这一发会把它改回「我方发单价」（本侧读不到也量不到）；`gameAcc` / `zgameAcc` / `moblie` / `sphone` / `itemRemark` / `otherRemark` / `dynamicRemark` 实测发不进去 ⇒ 对方系统里若有人改过，我们**既读不到也写不回**。
