"""**下家②式**（官方 Open Platform API）的**出向台账分派层**测试网：默认关着。
规格 `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md`；计划 `docs/plans/2026-10-09-jidanpu-openapi-write-phase2.md` §3 步骤 2/3/4。判据编号照原清单，每条都要能红：
- **步骤 2（推状态+开关+未通档）**：① 端到端——真形态②式绑定（account/password 都空，⛔ 不塞假密码，E-0202 就是被假密码骗过去的）+ 我方订单链路 + `member.status_out` 台账 ⇒ 开关跑一轮订单行真变（断言库行）、再跑一轮幂等（第二轮零写接口）；② 开关关（默认值）⇒ 一个字节不写、一次上游不打（含取 token）；③ 推前回读——远端已是目标标签 ⇒ `done` 且零写；④ 未通 kind ⇒ 显式跳过+一句人话台账（⛔ 静默、⛔ `failed`）——步骤 4 起归零，判据改由 `LineShapeTests` 恰好集合接手（`unsupported` 必须空表、`NOT_WIRED_KIND_LITERALS` 必须空元组）；⑤ ⛔ 写接口恰好只打该打的那一个（`(method, path)` 有序列表逐条相等）；⑥ ①式那条路一字没变（差异面缺省=①式那份，另有结构断言钉住）。
- **步骤 3（录单+改价）**：⑦ 端到端录单——`CreateOrder` 请求体逐格钉+链路落成+`done`；⭐ `gameArea` 永不为空（无区服⇒常量 `"官方服"`、有区服⇒那个区服名；一条网专钉「空串/`None` 都不许」；⛔ ①式照旧发空串，由 `tests/test_jidanpu_member_consume.py` 钉）；⑧ ⛔ 绝不泄露客户价（D-0162 红线）：夹具 `total_price != hall_price` ⇒ 请求体只有发单价（整包去不到总价）；⑨ 挡重——对方已有这张单 ⇒ 零录单接口；真机形态认领不了（回包没有系统订单号）⇒ 响亮失败+人工核对；⑩ 回读——录完按来源单号查不到/建单没给号 ⇒ 判失败、⛔ 不落链路，重试不重录（`recorded` 已落库）；⭐⭐ 改准（真机）：回读只剩「确认查得到」一个用途（⛔ 不取号、⛔ 不比号——回包没有 `orderId`）；⑪ 改价——写成功⇒`done`（⭐ 以写端点成功码为准，真机做不到回读钉值）、写失败⇒`failed`、大厅档⇒人话台账、无发单价⇒`skipped` 终态零上游；⑫ `remark`——问题单带固定常量、已退单不带（照①式）；⑬ 回包恰好 5 格（`status`/`creatTime`/`operTime`/`doneTime`/`verifyTime` 真机实测）⇒ ⛔ 谁假装对方多回一格，这里与 `tests/test_jidanpu_openapi.py` 两条网当场红；⑭ 拆单（`data` 逗号分隔）⇒ 取第一段落链路+人话台账（⛔ 静默丢，见 `SPLIT_ORDER_REASON`）。
- **步骤 4（退单+撤下大厅）**：⑮ 端到端退单——远端那张单真变「已退单」（假上游现场读数）+写后回读+链路 `in_hall` True→False+`done`；这一档不改我方订单行（本地退单那一刻已是已退单）⇒ 读数落链路/台账/远端三处；另一条从我方退单动作起跑（状态机 ⇒ `queue_withdraw` ⇒ 订单行真变 `IN_PROGRESS`→`REFUNDED`+远端变+链路降级；分工见 `WithdrawTests` 用例注释，⛔ 两条都不多余）；⑯ ⭐⭐ 撤下大厅②式不办（D-0249 修 E-0246）：远端任何状态 ⇒ 终态 `skipped`+人话、一次上游不打（含 token）、链路与远端不动——历史「收敛成已退单」会把接手人自己店里的活单退掉（E-0246 现场 34 张）；对账侧同形态反复跑 ⇒ 零条新 unlist（防 churn）；⑰ 已退单档请求体无 `remark` 键（逐字全等）；⑱ 回读不认 ⇒ 不许 `done`（两个 kind 各一条）；⑲ 无链路 ⇒ `skipped`+人话、一次上游不打；⑳ 写回业务失败但回读确认 ⇒ 仍 `done`（照①式「回读为准」）。
- **三条纪律**：⛔ 零真实网络（假上游复用 `tests.test_jidanpu_openapi._Upstream`/MockTransport——两个假上游必然漂所以复用同一个；地址 `.invalid`）；⛔ 夹具一律合成值（`AGENTS.md` §5-C / E-0077）；⛔ 断言打数据库行（E-0009：订单状态、台账行、链路行、绑定健康列）。
- **接线（二期步骤 5）**：`jidanpu_sync._run_member_openapi_binding` 里②式那一轮的第三段就是它 ⇒ 本模块有生产调用方；⚠️ 开关仍默认关 ⇒ 「调度真的会调它」与「真的会发出去」是两件事、判据各自能红：`SwitchTests.test_the_scheduled_round_sends_nothing_while_the_switch_is_off`（真命令一轮：零上游请求、订单/台账/链路一字不变）；`ScheduleWiringTests`（真命令一轮：一条待发台账真变 `done`；段序=回调消费→兜底对账→出向发单；②式段炸不拖垮①式绑定）。"""
from __future__ import annotations

import os
import contextlib
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock
from urllib.parse import parse_qsl

import httpx
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import Client

from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import (
    jidanpu as jidanpu_sdk,
    jidanpu_common,
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_member_openapi_outbox,
    jidanpu_openapi,
    jidanpu_openapi_consume,
)
from apps.business.order_status import apply_transition
from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
)
from apps.business.jidanpu_openapi import (
    CHANGE_ORDER_STATUS_PATH,
    CREATE_ORDER_PATH,
    MODIFY_ORDER_PATH,
    MODIFY_ORDER_PRICE_PATH,
    ORDER_STATUS_PATH,
    TOKEN_PATH,
    OpenApiError,
)
#: ⭐ 调度那两条**进程内**间隔锚（`(租户 schema 名, 绑定主键)` ⇒ 上次跑到点）：**不随事务回滚**，
#: 而每个测试类都重建 schema ⇒ 主键各自从 1 开始 ⇒ 上一个类留下的锚会命中本类主键（那一轮判「没到点」、兜底对账静默不跑）。
#: 照 `tests/test_jidanpu_member_openapi_consume.py` 同一条做法，本文件在接线那两个类里**逐用例清掉**（否则读数取决于类顺序与主键对齐——**静默**）。
from apps.business.management.commands.jidanpu_sync import (
    _MEMBER_FAST_AT,
    _MEMBER_RECONCILE_AT,
    _MEMBER_STATUS_PULL_AT,
)
#: ⭐ 复用上一轮那条线的**假上游**（⛔ 不另写第三份：多份桩必然漂）。
from tests.test_jidanpu_openapi import (
    CLIENT_ID,
    MEMBER_BASE_URL,
    SENTINEL_SECRET,
    _Upstream,
    _json_body,
)
from tests.p5_auth_helpers import P2_TEST_PASSWORD, make_logged_in_client

# ── 合成夹具值（E-0077）────────────────────────────────────────────────────────

#: 我方单号（20 位合成串）与下家那侧的远端单号。
ORDER_NO = "20261009000000000901"
JID = "JDP-OPENAPI-OUTBOX-0000000901"
#: 另一张我方单与它自己的远端单号（同一个绑定下 `(binding, 远端单号)` 是**唯一约束**
#: ⇒ 第二个用例不能复用 `JID`）。
ORDER_NO_B = "20261009000000000902"
JID_B = "JDP-OPENAPI-OUTBOX-0000000902"

#: 推送集里的三档之一（`PUSHABLE_STATUS_LABELS` 的**字面值**；⛔ 不从被测常量派生，E-0024）。
LABEL_REFUNDED = "已退单"
#: ⭐ 另两档（问题单 / 已暂停）：**步骤 3 起它们也推得通了**（`remark` 有了来源）——
#: 「问题单」用来钉「带 remark」那一半（见 `RemarkTests`）。
LABEL_ISSUE = "问题单"
#: 推之前那张单停在哪一档（真实世界：还没被推过的单停在「未分配」）。
LABEL_UNASSIGNED = "未分配"
#: 「**我们认识**、但不是目标态」的那一档——用**合成标签**（⛔ 不抄真机标签）：
#: 判据是「读到的不是目标态」、与本线认识哪几档无关（照 `test_jidanpu_member_consume.LABEL_OTHER` 的同一条处置）。
REMOTE_UNREADABLE_TOKEN = "已流转（合成）"

#: ⭐ 推「问题单 / 已暂停」时那句**固定** `remark` 的**逐字**副本（业务方裁定 B）。
#: ⛔ 不写 `模块.STATUS_REMARK`：自指断言在被断言的常量改坏时**不会红**（E-0024）。
REMARK_LITERAL = "由团团通同步过来的状态变更"

#: 开关的环境变量名（**字面值**，⛔ 不写 `模块.常量`：自指断言在被断言的常量改坏时不会红）。
ENV_LITERAL = "JIDANPU_MEMBER_OPENAPI_OUTBOX"

#: 「还没通」的 kind（**字面值**，⛔ 不从被测常量派生，E-0024）。
#: ⭐⭐ **步骤 4 起是空元组**（②式六档全通）—— 「未通档归零」这条判据就是它，
#: 由 `LineShapeTests` 的恰好集合断言一起钉住。
NOT_WIRED_KIND_LITERALS = ()

#: ⭐⭐ ②式**已经通了**的那几档（**字面值**，⛔ 不从被测常量派生）。
#: ⚠️ 步骤 2 通 1 档 / 步骤 3 通 3 档 / **步骤 4 通 2 档**（退单 + 撤下大厅）
#: 含改项目及同步来源单号，后者需要网页账号。
WIRED_KIND_LITERALS = (
    "member.status_out",
    "member.launch",
    "member.assign",
    "member.price",
    "member.withdraw",
    "member.unlist",
    "member.project",
    "member.source_order_no",
)

#: ⭐ **①式（账号 + 密码）办不了的那一档**（**字面值**）：它没有经过实测的「改对方项目名称」写接口
#: （候选端点 `/OrderAjax/ChangeOrderProject` 只到「读码」级、连 HTTP 方法都未定）⇒ 登记 `unsupported`，命中即**终态跳过 + 人话**（⛔ 静默、⛔ `failed`）。
#: ⚠️ 只有这一档：既有六档①式**照旧全通**（本文件与 `test_jidanpu_member_consume.py` 的逐条行为断言一字未改）。
UP_LINE_UNSUPPORTED_KIND_LITERALS = ("member.project",)

#: ⭐ **②式线上 `member.unlist` 的终态人话**（**字面值**，⛔ 不从被测常量派生，E-0024）。
#: ⭐⭐ D-0249（修 E-0246）：官方 API 没有不毁单的撤下原语 ⇒ 那条线上 unlist 一律终态 skip、一次上游都不打；
#: 分派拦截与对账收口都读 `unlist_unavailable_reason` 那一格（判据见 `_MEMBER_OPENAPI_LINE` 与 `jidanpu_member.reconcile`）。
UNLIST_UNAVAILABLE_REASON_LITERAL = "②式没有「不毁单的撤下」接口"


#: ⭐⭐ **真机实测**：`GetOrderStatus` 回包 `data` 的键**恰好**这 5 个（`?orderId=` 与 `?sourceId=` 两次读数键集合完全一样）。
#: ⚠️ **字面量**（⛔ 不从被测模块派生）：上一轮假上游多回了 `orderId` / `price` / `recFee` 三格，于是「回读取号 / 回读钉值」两条判据**真机恒假**、而用例**全绿**。
#: ⇒ 现在假上游只许回这 5 格：`_status_data` 自己断言，另有一条用例逐字钉它。
ORDER_STATUS_REAL_KEYS = frozenset({"status", "creatTime", "operTime", "doneTime", "verifyTime"})


def _status_data(*, status: str) -> dict:
    """假上游回的**订单状态**那一格（⭐ **恰好**真机那 5 个键，一个不多一个不少）。
    ⚠️ 两条纪律：① **只有这一处**拼这个回包（`_StatusUpstream` 与 `_WriteUpstream` 都走它——各拼一份必然漂，而漂的那天没有任何用例会红）；
    ② 拼完**当场自检**键集合（⛔ 不等到某条用例去数）：谁往这里加一格（`orderId` / `recFee` / 别的），当前这条用例立刻红，而不是"看起来更真"地骗过一串判据。"""
    data = {
        "status": status,
        "creatTime": "2026-10-09 12:00:00",
        "operTime": None,
        "doneTime": None,
        "verifyTime": None,
    }
    assert set(data) == ORDER_STATUS_REAL_KEYS, (
        "假上游的状态回包与真机读数不一致："
        f"多了 {sorted(set(data) - ORDER_STATUS_REAL_KEYS)}、"
        f"少了 {sorted(ORDER_STATUS_REAL_KEYS - set(data))}"
    )
    return data


#: ⭐⭐ **真机实测（逐字）**：`GetOrderStatus` 在「查不到这张单」与「参数不合法」两档回**同一个 `code=0`**——只有 `msg` 能分辨，`data` 两档都是**空串**（⛔ 不是 `None`）；三行读数在下面常量里逐字钉住。
#: ⚠️⚠️ 这一格正是「假上游与真机不一致」最容易栽的地方（上一轮栽过一次：照文档口径猜的 `data=None`，真机从没这么回过）⇒ 现在假上游只许回第一行逐字：
#: `_absent_order_envelope` 当场断言 + `AbsentOrderEnvelopeTests` 拿原始回包逐字钉。⚠️ 口径（⛔ 别读成「查不到 = 参数不合法」）：调用方**永远会传合法单号** ⇒ 正常路径碰不到第三行；万一碰到 ⇒ 被当成「对方没有这张单」⇒ 走录单 ⇒ **对方的「来源单号查重」响亮拒掉**（兜底在，⛔ 不静默）。
ABSENT_ORDER_REAL_ENVELOPES = (
    {"code": 0, "msg": "没有相关订单信息", "data": ""},
    {"code": 0, "msg": "没有相关订单信息", "data": ""},
    {"code": 0, "msg": "系统单号和来源单号不可同时为空", "data": ""},
)


def _absent_order_envelope() -> dict:
    """假上游回的「**对方没有这张单**」那一格（⭐ 逐字 = 真机读数，⛔ 不是文档口径）。
    ⚠️ 照 `_status_data` 的同两条纪律：**只有这一处**拼它（两个假上游各拼一份必然漂、漂了没用例红）；拼完**当场自检**——
    多一格 / 少一格 / 改一个字 / `data` 从 `""` 换成 `None` ⇒ 当前这条用例立刻红，而不是「看起来更真」地骗过后面一串判据。"""
    envelope = {"code": 0, "msg": "没有相关订单信息", "data": ""}
    assert envelope in ABSENT_ORDER_REAL_ENVELOPES, (
        "假上游的「查不到单」信封与真机读数不一致："
        f"本次 = {envelope!r}；真机三行 = {list(ABSENT_ORDER_REAL_ENVELOPES)!r}"
    )
    return envelope


class _StatusUpstream(_Upstream):
    """假上游：在上一轮那份**通用假上游**之上，加**有状态的单状态**与**按方法+路径的读数**。
    ⚠️ **故意 `super().handler(request)` 之后再改状态**（⛔ 不整份重写 `handler`）：「记录 / 路由 / 取 token 的平铺体 / 缺省回包」口径住在那**一份**里，再抄一遍就是第三个假上游（两份桩必然漂，`AGENTS.md` §5-C）。`GetOrderStatus` 按 `orderId` 回**当前**状态（有状态 ⇒ 「写后回读确认」判得动）；`ChangeOrderStatus` 写成功（`code>0`）⇒ 那个单号的当前状态**真的跟着变**（真实世界如此，而共享引擎要**回读确认**才敢收敛 `done`）。
    ⚠️ **本类自己处理的那两条路由要自己记**（`_record`）：基类那份 `handler` 的「记一次请求」排在它自己的路由**之后**，而本类对这两条路由**早返回**（按现场状态拼回包）⇒ 不自己记它们就一条都不进 `self.requests`，用例读数（「端点集合恰好」）会**看着像一次都没打**（实测踩到：读数只回一行 token，把一条**绿的**实现报成红的——假红导向错根因）。⛔ 其余路由照旧走 `super().handler(request)`。"""

    def __init__(self, *, status=LABEL_REFUNDED):
        super().__init__()
        self.statuses: dict[str, str] = {JID: status}

    def set_status(self, order_id, status):
        """把远端那张单此刻的状态摆成 `status`（`status=None` = 读不到 ⇒ 回成失败信封）。"""
        if status is None:
            self.statuses.pop(order_id, None)
        else:
            self.statuses[order_id] = status

    def _record(self, request: httpx.Request) -> None:
        """照基类 `_Upstream.handler` 记一次请求的**同样字段**（⛔ 少记一格读数就会骗人）。"""
        self.requests.append({
            "method": request.method,
            "path": request.url.path,
            "url": str(request.url),
            "params": dict(request.url.params),
            "authorization": request.headers.get("authorization"),
            "content_type": request.headers.get("content-type"),
            "form": dict(parse_qsl(request.content.decode("utf-8"))) if request.content else {},
            "json": _json_body(request),
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == ORDER_STATUS_PATH:
            self._record(request)
            order_id = request.url.params.get("orderId")
            if order_id not in self.statuses:
                # ⚠️ 远端**没有这张单** ⇒ 回**真机那个失败信封**（`code=0` ＋ `data` **空串**）：
                #    那是②式唯一能表达「查不到」的方式，而共享引擎会把它翻成「读不到」那一档
                #    （⛔ 不是「没状态」）。⭐ 形状**逐字**取自真机读数（见 `_absent_order_envelope`）。
                return httpx.Response(200, json=_absent_order_envelope())
            return httpx.Response(200, json={
                "code": 1, "msg": None, "data": _status_data(status=self.statuses[order_id]),
            })
        response = super().handler(request)
        if method == "POST" and path == CHANGE_ORDER_STATUS_PATH:
            body = _json_body(request)
            if isinstance(body, dict) and body.get("orderId"):
                self.statuses[body["orderId"]] = str(body.get("status", ""))
        return response

    def reply(self, path, *, status=200, payload=None):
        # ⛔ 这两条路由**由本类自己**按状态现场生成 ⇒ 外部再挂一条会把判据变成一个固定回包
        #    （「写后回读真的跟着变了」当场失效、而且**静默**）。
        assert path not in (ORDER_STATUS_PATH, CHANGE_ORDER_STATUS_PATH), (
            f"{path} 由本类按状态现场生成，⛔ 不许用 reply() 覆盖"
        )
        return super().reply(path, status=status, payload=payload)

    # ── 读数（⭐ 判据钉「方法 + 路径」，⛔ 不是只钉路径）─────────────────────────
    # ⚠️⛔ **不许在这里重定义 `api_calls` / `token_calls` / `last_api_call`**：基类那三条按路径滤掉取 token 那一跳，本类再包一层、把 `super().api_calls()` 的结果当新请求记进 `self.requests` ⇒ 读数**翻倍且错乱**。
    #    这里只加**投影**（只读 `self.requests`），⛔ 不碰它。

    def all_pairs(self) -> list[tuple[str, str]]:
        """`(method, path)` 的**有序**列表（含取 token 那一跳）。"""
        return [(row["method"], row["path"]) for row in self.requests]

    def api_pairs(self) -> list[tuple[str, str]]:
        """`(method, path)` 的**有序**列表（⛔ 不含取 token 那一跳）。"""
        return [(row["method"], row["path"]) for row in super().api_calls()]

    def write_pairs(self) -> list[tuple[str, str]]:
        """**写接口**那几次请求（`api_pairs` 里不是 GET 的那些）——「一次都没打错」的判据口。"""
        return [pair for pair in self.api_pairs() if pair[0] != "GET"]

    def json_calls(self, path) -> list[dict]:
        """打到某个路径的**请求体**（JSON）列表——「参数逐字发对了没有」的判据口。"""
        return [
            row["json"] for row in self.api_calls()
            if row["path"] == path and isinstance(row["json"], dict)
        ]


# ══════════════════════════════════════════════════════════════════════════════
# 步骤 3 的假上游：一张**有状态的店铺**（单状态 + 两个价 + 录单）
# ══════════════════════════════════════════════════════════════════════════════

#: 对方那侧的系统订单号（**合成值**；`CreateOrder` 成功时回给我们）。
CLAIMED_ID = "20261009000000009901"
#: 建单参数里我们发过去的**来源名称**（合成值；绑定行上配的就是它）。
SOURCE_NAME = "合成来源名甲"
#: `assignPrice` / `recFee` 那一格（= 我方发单价 `hall_price`）与 `price`（订单总价那一格）。
#: ⚠️ 两个值**刻意不同**：`price` 由对方自己算（我们不传总价），录单时我们只传发单价。
CREATED_REC_FEE = Decimal("50.00")

#: ⭐ 录单回包里的 `code`：**实测**的录单成功码（spec §3.2 F6；⛔ 不是 1）。
#: ⚠️ 用**字面量**（不从 `tests.test_jidanpu_openapi` 派生）：「录单成功回 605764」是这条线的
#: 事实，谁把它写成 `1` 就该在**读数**上红。
RECORD_SUCCESS_CODE = 605764


class _WriteUpstream(_StatusUpstream):
    """假上游：在**推状态那一份**之上，加**录单 / 改价**两条有状态的路由。
    ⚠️ **故意 `super().handler(request)` 之后再改状态**（⛔ 不整份重写 `handler`）：「记录 / 路由 / 取 token / 缺省回包 / 单状态镜像」口径住在那**一份**里，再抄一遍就是第四个假上游（多份桩必然漂，`AGENTS.md` §5-C）。`CreateOrder` ⇒ **真建一张**（分配 `CLAIMED_ID`、按**来源单号**索引）并回 `code=605764` + `data=<系统订单号>`（⭐ 远端单号的唯一来源，真机实测那一格就是它）；`ModifyOrderPrice` ⇒ 改那张单的 **`recFee`**（⭐ 实测改的就是这一格）——⚠️ 假上游**自己的账本**，⛔ **绝不放进 `GetOrderStatus` 回包**（真机回包没有它 ⇒ 放了=帮实现假装能回读钉值）；`GetOrderStatus` ⇒ 由基类按 `statuses` 回（⭐ **只回真机那 5 格**，见 `_status_data`）。
    ⚠️ 开关（各自只服务一条用例，⛔ 不兼任两件事）：`create_data` ⇒ 建单回包 `data` 那格的**覆盖值**（造「录单回了成功、但没给系统订单号」档）；`index_source=False` ⇒ 建出来的单**不按来源单号索引** ⇒ 录单后的回读查不到它（「回读查不到 ⇒ 判失败」判据的载体）。⚠️ 外部**显式**挂了 `reply(CreateOrder…)` / `boom(…)` 时由那一份说了算（本类那时不动现场）——但 `reply()` 对本类这三条路由**被禁用**（见下）。"""

    def __init__(self, *, index_source=True, create_data=None):
        super().__init__()
        self.index_source = index_source
        #: `CreateOrder` 回包里 `data` 那格的**覆盖值**（`None` = 按现场回系统订单号）。
        #: ⚠️ 用它造「录单回了成功、但没给系统订单号」那一档（真机可能的形态）。
        self.create_data = create_data
        self.create_remote_id = CLAIMED_ID
        #: 改价那一发的**失败信封**（`(code, msg)`；`None` = 照常成功）。
        #: ⚠️ 与 `reply()` 分开：`reply()` 对改价是**禁用**的（那会把"真的改了对方那一格"读数变成固定回包）；
        #: 这一档要钉的正是「**失败信封必须被判失败**」⇒ 它**不动现场**、只回失败码（照 E-0087：写接口的失败信封必须能红）。
        self.price_rejection: tuple[int, str] | None = None
        #: ⭐ 改项目那一发（`ModifyOrder`）的**失败信封**：与 `price_rejection` 同一处置
        #: （⛔ 不用 `reply()` 覆盖那一条路由：那会把「对方那一格真的变了」这条读数
        #: 变成一个固定回包）。
        self.project_rejection: tuple[int, str] | None = None
        #: 系统订单号 → 那一行的现场（`source_id` / `status` / `rec_fee`）。
        #: ⚠️ `rec_fee` 是**假上游自己的账本**（用来证明"改价真的改了对方那一格"），
        #: ⛔ **不回进 `GetOrderStatus`**（真机不回那一格）。
        self.rows: dict[str, dict] = {}
        #: 来源单号 → 系统订单号（`CreateOrder` 之后才有；挡重预读按它命中）。
        self.by_source: dict[str, str] = {}
        #: 每一次 `CreateOrder` 的请求体（读数：参数逐字发对了没有）。
        self.created: list[dict] = []

    def seed(self, *, source_id, order_id, status, rec_fee=CREATED_REC_FEE, project=None):
        """预置一张**对方已经有的**单（挡重那几条用例用）。"""
        self.rows[order_id] = {
            "source_id": source_id, "status": status, "rec_fee": rec_fee,
            #: ⭐ 对方那张单**此刻**的项目名称（改项目那几条用例要先有一个「旧值」才看得出变化）。
            "project": project,
        }
        if self.index_source:
            self.by_source[source_id] = order_id
        self.set_status(order_id, status)
        return self

    def _row_of(self, *, order_id=None, source_id=None):
        if order_id is not None:
            return self.rows.get(order_id)
        key = self.by_source.get(source_id)
        return self.rows.get(key) if key else None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == ORDER_STATUS_PATH:
            row = self._row_of(
                order_id=request.url.params.get("orderId"),
                source_id=request.url.params.get("sourceId"),
            )
            self._record(request)
            if row is None:
                # ⚠️ 「对方没有这张单」在②式下只能这样表达（失败信封 + 一句原话）——
                                #    ⭐ 形状**逐字**取自真机读数（`data` 是**空串**，⛔ 不是 `None`），
                #    见 `_absent_order_envelope`。
                return httpx.Response(200, json=_absent_order_envelope())
            # ⭐ **只回真机那 5 格**（`_status_data` 自己断言；⛔ 没有 orderId / price / recFee）。
            return httpx.Response(200, json={
                "code": 1, "msg": None, "data": _status_data(status=row["status"]),
            })
        if method == "POST" and path == CREATE_ORDER_PATH:
            response = super().handler(request)     # 记录 + errors + routes + 缺省回包
            if path in self.routes:
                # 用例显式挂了固定回包（如「录单被拒」）⇒ 由它说了算，⛔ 本类不动现场。
                return response
            body = _json_body(request) or {}
            row = {
                "source_id": str(body.get("souresOrderID") or ""),
                # 新建的单在对方那边就是「未分配」（实测：文档的状态枚举里没有这个值）。
                "status": LABEL_UNASSIGNED,
                # ⭐ `assignPrice` 落成对方的 `recFee`（实测那一格）—— 本类自己的账本，
                #    ⛔ 不回进状态回包（真机没有那一格 ⇒ 回读钉值在②式做不到）。
                "rec_fee": Decimal(str(body.get("assignPrice"))),
                # ⭐ 对方那张单的项目名称 = 我们建单时发的 `project`（本类自己的账本，
                #    ⛔ 同样不回进状态回包）—— 改项目那几条用例要拿它当「旧值」。
                "project": str(body.get("project") or ""),
            }
            remote_id = self.create_remote_id
            self.rows[remote_id] = row
            if self.index_source:
                self.by_source[row["source_id"]] = remote_id
            self.set_status(remote_id, LABEL_UNASSIGNED)
            self.created.append(dict(body))
            data = remote_id if self.create_data is None else self.create_data
            return httpx.Response(200, json={
                "code": RECORD_SUCCESS_CODE, "msg": "录单成功", "data": data,
            })
        if method == "POST" and path == MODIFY_ORDER_PRICE_PATH:
            response = super().handler(request)
            if self.price_rejection is not None:
                # 用例挂了失败信封 ⇒ 回它，且**不动现场**（对方那一格没变）。
                code, msg = self.price_rejection
                return httpx.Response(200, json={"code": code, "msg": msg, "data": None})
            if path in self.routes:
                return response
            body = _json_body(request) or {}
            row = self.rows.get(str(body.get("orderId") or ""))
            if row is not None and row["rec_fee"] is not None:
                row["rec_fee"] = Decimal(str(body.get("price")))
            return response
        if method == "POST" and path == MODIFY_ORDER_PATH:
            response = super().handler(request)
            if self.project_rejection is not None:
                # 用例挂了失败信封 ⇒ 回它，且**不动现场**（对方那一格没变）。
                code, msg = self.project_rejection
                return httpx.Response(200, json={"code": code, "msg": msg, "data": None})
            if path in self.routes:
                return response
            body = _json_body(request) or {}
            #: ⚠️ 这一格的单号叫 **`orderID`**（大写 `ID`），别的端点叫 `orderId` —— 逐字读它。
            #: ⭐⭐ **照真接口的实测口径校验那两格**（真机读数，⛔ 别把这一份放宽）：文档写 `orderID` / `souresOrderID`「至少一项」、`gameArea` 标「可选」，而**实际三者都必填**
            #: （E-0227 那一族：**假上游比真接口宽松** ⇒ 漏格在单测里全绿、到真机每一发都被拒）。两句话**逐字**是对方回的原话（`code=0` 是失败档）。
            if not str(body.get("gameArea") or "").strip():
                return httpx.Response(200, json={
                    "code": 0, "msg": "游戏大区为必填项", "data": None,
                })
            if not str(body.get("souresOrderID") or "").strip():
                return httpx.Response(200, json={
                    "code": 0, "msg": "系统要求来源单号不能为空", "data": None,
                })
            row = self.rows.get(str(body.get("orderID") or ""))
            if row is not None:
                row["project"] = str(body.get("project") or "")
            return response
        return super().handler(request)

    def reject_price(self, *, code=0, msg):
        """把改价那一发挂上**失败信封**（⛔ 不是"改了个固定回包"：现场一个字节都不动）。"""
        self.price_rejection = (code, msg)
        return self

    def reject_project(self, *, code=0, msg):
        """把改项目那一发挂上**失败信封**（同 `reject_price`：现场一个字节都不动）。"""
        self.project_rejection = (code, msg)
        return self

    def reply(self, path, *, status=200, payload=None):
        # ⛔ 这三条路由**由本类自己**按现场拼回包 ⇒ 外部再挂一条会把判据变成一个固定回包
        #    （「回读真的跟着变」「改价 / 改项目真的改了那一格」当场失效、而且**静默**）。
        # ⚠️ `CreateOrder` **允许**挂固定回包：那正是「录单被上游拒」那一档的注入点（挂了它本类就不再动现场）。
        assert path not in (ORDER_STATUS_PATH, MODIFY_ORDER_PRICE_PATH, MODIFY_ORDER_PATH), (
            f"{path} 由本类按现场生成，⛔ 不许用 reply() 覆盖"
        )
        return super().reply(path, status=status, payload=payload)

    # ── 读数（⭐ 全是只读投影，⛔ 不碰 `self.requests`）─────────────────────────

    def current_rec_fee(self, order_id=CLAIMED_ID):
        row = self.rows.get(order_id)
        return None if row is None else row["rec_fee"]

    def current_project(self, order_id=CLAIMED_ID):
        """⭐ 对方那张单**此刻**的项目名称（假上游自己的账本 ⇒ 「真的改了对方那一格」的读数口）。
        ⚠️ 它是**账本**、⛔ **不回进 `GetOrderStatus`**：真机那条单查里没有这一格
        （回进去 =「帮实现假装能回读钉值」，正是上一轮栽过的形态）。"""
        row = self.rows.get(order_id)
        return None if row is None else row.get("project")


def _muted_hall_line(base):
    """②式那一份差异面，**只把「大厅那一档」的人话抹成空**（其余逐格照抄）。
    ⚠️ 存在理由：`_consume_price` 对「这一条线没有大厅那一档、却没说清为什么」是**响亮抛**的
    ⇒ 那条判据需要一个**配错**的 `OutboxLine` 才能红（拿真的那一份永远走不到）。"""
    return jidanpu_member.OutboxLine(
        name=base.name, spawn_client=base.spawn_client, status_push=base.status_push,
        record=base.record, unsupported=base.unsupported,
        price=jidanpu_member.PricePrimitives(
            write_price=base.price.write_price, read_price=base.price.read_price,
            read_confirm_unavailable_reason=base.price.read_confirm_unavailable_reason,
            rethrow_in_hall=None, hall_not_applicable_reason="",
        ),
    )


def _muted_read_confirm_line(base):
    """②式那一份差异面，**只把「以写返回为准」那句话抹成空**（其余逐格照抄）。
    ⚠️ 存在理由同上：`_consume_price` 对「回读里没有那一格、却没说清为什么」也是**响亮抛**的 ⇒ 那条判据需要一个配错的 `OutboxLine` 才能红。
    ⚠️ 这一份**保留**大厅那一档的人话（⛔ 免得抛的是另一条判据、读数指向错的地方）。"""
    return jidanpu_member.OutboxLine(
        name=base.name, spawn_client=base.spawn_client, status_push=base.status_push,
        record=base.record, unsupported=base.unsupported,
        price=jidanpu_member.PricePrimitives(
            write_price=base.price.write_price, read_price=None,
            read_confirm_unavailable_reason="",
            rethrow_in_hall=None,
            hall_not_applicable_reason=base.price.hall_not_applicable_reason,
        ),
    )


class _StubPriceClient:
    """一个**只够走到「大厅那一档」那个分支**的假客户端（那两条判据口）。
    ⚠️ 它**不接网络**、也不假装自己是 `OpenApiClient`：只回**写价**那一跳的形状。
    ⚠️ **没有 `get_order_status`**：②式的改价那一档**不回读**（真机回包里没有那一格）⇒ 谁把回读加回来，这个桩会让它**当场 AttributeError**（比"看起来更完整"好）。"""

    def __init__(self):
        self.written: list = []

    def modify_order_price(self, *, order_id, price):
        self.written.append(Decimal(str(price)))
        return ""


class _StubProjectClient:
    """一个**只够走到「判确认口径」那个分支**的假客户端（改项目那条判据口）。
    ⚠️ 它**不接网络**、也不假装自己是 `OpenApiClient`：只回**改单**那一跳的形状（`ModifyOrder` 的参数名逐字：`game_name` / `game_area` / `project` / `order_id` / `source_order_id`——⭐ `game_area` 与 `source_order_id` 是真机补齐的两格，⛔ 缺了它们在**调用处**就 `TypeError`，所以这个桩必须跟着签名走）。
    ⚠️ **没有 `get_order_status`**：②式改项目那一档**不回读**（真机回包里没有那一格）⇒ 谁把回读加回来，这个桩会让它**当场 AttributeError**（比"看起来更完整"好）。"""

    def __init__(self):
        self.written: list = []

    def modify_order(self, *, game_name, game_area, project, order_id=None,
                     source_order_id=None, extra=None):
        #: ⭐ 整单覆盖那一发现：`extra` 是**整单覆盖那几格**（`hours` / `orderSoures` / `customerId` / `price` / `originPrice` / `remark` / `orderRemark`）
        #: ——⛔ 本桩**不判**它该有哪几格（那是 `ProjectPayloadTests` 的判据焦点），这里只逐字记下来。
        self.written.append(
            {"game_name": game_name, "game_area": game_area, "project": project,
             "order_id": order_id, "source_order_id": source_order_id,
             "extra": dict(extra or {})}
        )
        return None


class _OutboxCase(TenantTestCase):
    """临时假上游 + 一条**和界面真建出来一模一样**的②式绑定 + 我方订单 + 链路 + 台账。"""

    #: 假上游工厂（步骤 3 那几条用例换成 `_WriteUpstream`；⛔ 不另写第三份桩）。
    upstream_factory = _StatusUpstream
    #: 夹具预置一条链路吗？⭐ **录单那几条必须为假**（有链路 ⇒ `ensure_remote` 直接短路，
    #: 一条上游都不打 ⇒ 那几条用例会以「什么也没发生」的形式假绿）。
    with_link = True

    def setUp(self):
        super().setUp()
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        # ⭐ **开关默认不设**（⛔ 不显式设成 "0"：那样测的是「显式关」，而不是「默认关」）。
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(ENV_LITERAL, None)
        self.addCleanup(lambda: os.environ.pop(ENV_LITERAL, None))

        self.upstream = self.upstream_factory()
        http = mock.patch.object(jidanpu_openapi.httpx, "Client", self.upstream.factory)
        http.start()
        self.addCleanup(http.stop)

        self.game = Game.objects.create(name="测试游戏丙")
        self.binding = self.make_binding()
        self.order = self.make_order()
        self.link = None
        if self.with_link:
            self.link = JidanpuMemberOrderLink.objects.create(
                binding=self.binding, order=self.order, jidanpu_order_id=JID, in_hall=True,
            )

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_binding(self, *, enabled=True, mode="openapi", account="", password=""):
        """一行**下家②式**绑定（`JidanpuMemberBinding`）。
        ⭐ **`account` / `password` 默认留空**：那就是界面上②式的真实形态（「我的资料 → 功能设置」按②式保存时不填账号密码）；⛔ **不许塞假密码**——上一轮那条线正是被假密码骗过去的（E-0202）。
        ⚠️ `client_id` / `client_secret` 用**合成串**（照界面真填的形状）；`client_id` 里带一次性的后缀：token 缓存是**模块级**的、键含 `client_id` ⇒ 同一进程里不同用例的绑定天然不共享缓存（否则第二轮读到的是别人那次的读数）。"""
        username = f"kefu-openapi-outbox-{JidanpuMemberBinding.objects.count()}"
        make_logged_in_client(username=username, groups=("客服",))
        return JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username),
            jidanpu_base_url=MEMBER_BASE_URL,
            account=account, password=password, mode=mode,
            client_id=f"{CLIENT_ID}-outbox-{username}",
            client_secret=f"{SENTINEL_SECRET}-{username}",
            #: ⭐ 来源名：②式建单的 `orderSoures`（来源**名称**）就是它（①式那一侧是拿它去
            #: 匹配对方的来源列表）。⚠️ 显式给一个**合成**名，好钉「发出去的就是这一格」。
            source_name=SOURCE_NAME,
            enabled=enabled,
        )

    def make_order(self, *, order_no=ORDER_NO, status=OrderStatus.REFUNDED,
                   total_price=Decimal("100.00"), hall_price=Decimal("50.00")):
        return Order.objects.create(
            game=self.game, order_no=order_no, status=status,
            total_price=total_price, hall_price=hall_price,
            project_text="合成项目丙",
        )

    def queue_status_out(self, *, from_status=OrderStatus.IN_PROGRESS):
        """造一条**由既有触发点写出来**的 `member.status_out` 台账（⛔ 不手搓行）。
        ⚠️ 用 `jidanpu_member.queue_status_out`（写方的**唯一**出处）：手搓 dict 形状会让本文件
        测的是「我以为写方写什么」，而不是「写方真的写什么」（E-0202 的同族）。"""
        return jidanpu_member.queue_status_out(self.order, from_status=from_status)

    def queue_kind(self, kind, *, event_status="pending"):
        """造一条**指定 kind** 的在飞台账（五档「还没通」那几条用例用）。"""
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=kind, status=event_status,
        )

    # ── 读数 ────────────────────────────────────────────────────────────────

    def enable(self):
        """把开关拨到「开」（⭐ 只有显式取值才算开，见 `outbox_enabled`）。"""
        os.environ[ENV_LITERAL] = "1"

    def _link(self, *, in_hall=False, remote_id=JID):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    def _queue_price(self):
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PRICE, rearm=True,
        )

    def run_a_round(self, *, inject_client=False, now=None):
        """跑一轮②式出向台账。`inject_client=True` ⇒ 走**共享引擎自己建连**那条路。
        ⚠️ `now` 可注入（默认墙钟）：**失败过一次的事件有退避窗**（`min(2^attempts × 60, 3600)` 秒）⇒ 用例要「第二轮真的再跑一遍」就必须把时钟**推过那个窗**
        （⛔ 不是把 `attempts` / `processed_at` 清掉假装重排——那测的就不是「退避之后的那一轮」了）。"""
        now = now or timezone.now()
        if inject_client:
            with jidanpu_openapi.openapi_client(self.binding) as client:
                return jidanpu_member_openapi_outbox.run_member_status_outbox(
                    self.binding, now=now, client=client,
                )
        return jidanpu_member_openapi_outbox.run_member_status_outbox(self.binding, now=now)

    def db_order(self):
        """**重新查库**取订单那一行（⛔ 不读内存对象：那会把「没写」看成本地那次改动）。"""
        return Order.objects.get(pk=self.order.pk)

    def db_events(self, *, kind=None):
        rows = JidanpuSyncEvent.objects.filter(target_ref=self.order.order_no)
        return rows.filter(kind=kind) if kind else rows

    def db_out_events(self, *, kind=None):
        """只取**出向**（`direction="out"`）的台账行 —— ⛔ 别用 `db_events` 去 `.get()`：
        本线会为「留痕」写 **`direction="in"`** 的人话行，而那些行的 `kind` 与事件行**同名**（如 `member.price` / `member.launch`）
        ⇒ 不筛方向就会撞 `MultipleObjectsReturned`（实测踩到：改价那一档现在**每次**都有一条「以写返回为准」的 `in` 行）。"""
        rows = self.db_events(kind=kind).filter(direction="out")
        return rows

    def db_binding(self):
        return JidanpuMemberBinding.objects.get(pk=self.binding.pk)


# ══ ① 开关（⭐ 默认关着，且**关着的时候一个字节都不写、一次上游都不打**）════════════


class SwitchTests(_OutboxCase):
    def test_the_switch_is_off_by_default(self):
        """⭐ 默认值 = **关**（⛔ 不是「设了就算开」）。判别力：把 `outbox_enabled()` 改成恒 `True` ⇒ 本用例红。"""
        self.assertFalse(jidanpu_member_openapi_outbox.outbox_enabled())

    def test_only_explicit_truthy_values_count_as_on(self):
        """取值语义：`1/true/yes/on`（大小写不敏感、去空白）算开；**别的都算关**。
        ⚠️ 这条判据承重：排障时写 `=0` / `=false` 的意思是「**别跑**」，把它们当「开」会让一次本意是「先关掉」的操作**真的去打上游写单**。
        判别力：把 `in TRUTHY` 换成 `bool(raw)` ⇒ `"0"` / `"false"` 那两条红。"""
        for raw in ("1", "true", "TRUE", " yes ", "on", "On"):
            with self.subTest(raw=raw):
                os.environ[ENV_LITERAL] = raw
                self.assertTrue(jidanpu_member_openapi_outbox.outbox_enabled())
        for raw in ("", "0", "false", "no", "off", "2", "true1"):
            with self.subTest(raw=raw):
                os.environ[ENV_LITERAL] = raw
                self.assertFalse(jidanpu_member_openapi_outbox.outbox_enabled())

    def test_switch_off_does_not_write_a_byte_and_does_not_touch_upstream(self):
        """⭐⭐ 判据二：开关关着 ⇒ **一个字节都不写、一次上游都不打**（含取 token 那一跳）。
        判别力：把 `OutboxLine.enabled` 那道闸删掉（或让 `outbox_enabled` 恒真）⇒ 上游桩上立刻出现 `(POST, /oauth/token)` 与两条 `GetOrderStatus`、
        订单行被真的推成目标态 ⇒ 本用例三条断言全红。"""
        self.queue_status_out()
        before_order, before_event = self.db_order(), self.db_events().get()
        before_binding = self.db_binding()

        stats = self.run_a_round()

        # ⭐ 一次上游都不打（⛔ 不是「只打了一点点」）：
        self.assertEqual(self.upstream.requests, [], "开关关着却打了上游")
        # ⭐ 一个字节都不写：
        self.assertEqual(self.db_order().status, before_order.status)
        self.assertEqual(self.db_order().updated_at, before_order.updated_at)
        event = self.db_events().get(pk=before_event.pk)
        self.assertEqual((event.status, event.attempts, event.reason),
                         (before_event.status, before_event.attempts, before_event.reason))
        binding = self.db_binding()
        for field in ("last_run_at", "last_ok_at", "last_error", "last_error_at"):
            with self.subTest(field=field):
                self.assertEqual(getattr(binding, field), getattr(before_binding, field))
        # ⭐ 但**要说清为什么没跑**（⛔ 不静默）：
        self.assertEqual(stats.blocked_reason, jidanpu_member_openapi_outbox.BLOCKED_SWITCH_OFF_REASON)
        self.assertTrue(stats.blocked_reason)


# ══ ② 端到端（这条路唯一「看得见效果」的那一环）════════════════════════════════


class EndToEndTests(_OutboxCase):
    def test_one_round_really_changes_the_order_row_and_the_next_round_is_idempotent(self):
        """⭐⭐ 判据一：真建出来形态的②式绑定 ⇒ 开开关跑一轮 ⇒ **订单行真的变了**；再跑一轮 ⇒ 幂等。
        ⚠️ 第二轮走**共享引擎自己建连**那条路（`inject_client=False`）——第一轮已证明「推得动」，第二轮要证明「**不推第二次**」，而那一跳必须经过真实的分派层（⛔ 不能靠注入的客户端绕开 `spawn_client`）；**零真实网络**由假上游保证。判别力：① 把「写后回读确认」删掉 ⇒ 第一轮仍绿，但第二轮会打第二次 `ChangeOrderStatus`；② 把「推前回读」删掉 ⇒ 第二轮直接再写一次 ⇒ 第二轮断言红；③ 把②式的 `spawn_client` 换回①式的 `open_client_with_http` ⇒ 建连处抛 `ConfigError`（账号密码是空的）⇒ 第一轮就红。
        """
        self.enable()
        self.queue_status_out()
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        # ⭐ 第一轮：写**成功**了，但「写后回读」那一跳没落地（真实世界：那一跳超时 / 上游抖）
        #    ⇒ 台账留 `failed`（远端其实已经是目标态了，**我们只是没确认到**）。
        original = self.upstream.handler

        def _write_lands_then_read_breaks(request):
            response = original(request)
            if request.method == "POST" and request.url.path == CHANGE_ORDER_STATUS_PATH:
                self.upstream.set_status(JID, REMOTE_UNREADABLE_TOKEN)
            return response

        with mock.patch.object(self.upstream, "handler", _write_lands_then_read_breaks):
            first = self.run_a_round(inject_client=True)

        self.assertEqual(first.failed, 1)
        self.assertEqual(first.done, 0)
        # ⭐ 订单行**真的变了**（查库，⛔ 不读内存对象）：
        self.assertEqual(self.db_order().status, OrderStatus.REFUNDED)
        # ⭐ 台账停在 `failed`（**没确认到**就绝不写 `done`）：
        event = self.db_events(kind="member.status_out").get()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertIn(jidanpu_member.STATUS_CONFIRM_REASON, event.reason)
        # ⭐ **端点集合恰好**：取 token 一次 + 推前回读一次 + 写一次 + 写后回读一次
        #    （⛔ 一次都不许多、⛔ 一次都不许少；用 GET/POST 与路径**双向**钉）。
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("GET", ORDER_STATUS_PATH),
            ("POST", CHANGE_ORDER_STATUS_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])
        # ⭐ 写接口**逐字**收到了该收的东西（⛔ 不是「打过了就算」）：
        self.assertEqual(self.upstream.write_pairs(), [("POST", CHANGE_ORDER_STATUS_PATH)])
        write = [row for row in self.upstream.api_calls() if row["method"] == "POST"][0]
        self.assertEqual(write["json"]["orderId"], JID)
        self.assertEqual(write["json"]["status"], LABEL_REFUNDED)

        # ── 第二轮：**幂等**（重放同一条待发动作 ⇒ 一次写都不打）────────────────────
        self.upstream.set_status(JID, LABEL_REFUNDED)     # 远端此刻**已经是**目标态
        round_one_calls = len(self.upstream.requests)
        round_one_order = self.db_order().updated_at
        # ⚠️ 时钟推过退避窗（失败过一次 ⇒ 窗是 `2^1 × 60` 秒），否则这一轮只会 `deferred`。
        second = self.run_a_round(now=timezone.now() + timedelta(seconds=180))

        self.assertEqual(second.done, 1)
        self.assertEqual(second.failed, 0)
        self.assertEqual(self.db_order().status, OrderStatus.REFUNDED)
        self.assertEqual(self.db_order().updated_at, round_one_order)
        self.assertEqual(
            [pair for pair in self.upstream.all_pairs()[round_one_calls:]
             if pair[1] == CHANGE_ORDER_STATUS_PATH],
            [], "第二轮又打了一次写接口（幂等判据破了）",
        )
        # 第二轮**只**多了一次「推前回读」（远端已是目标态 ⇒ 就此收敛成 `done`）。
        self.assertEqual(self.upstream.api_pairs()[2:], [
            ("GET", ORDER_STATUS_PATH),   # 第一轮写后回读（没确认到）
            ("GET", ORDER_STATUS_PATH),   # 第二轮的推前回读（读到目标态 ⇒ 不写）
        ])
        self.assertEqual(self.db_events(kind="member.status_out").get().status, "done")

    def test_the_engine_itself_refuses_the_openapi_line_while_the_switch_is_off(self):
        """⭐ **纵深**：绕过入口闸直接拿②式那一份 `line` 调 `run_outbox` ⇒ 照样一次上游都不打。
        ⚠️ 存在的理由：入口闸在调度那一层（步骤 5 才接线），而 `run_outbox` 是**公开**的 ⇒ 只把「默认关着」交给调用方记住迟早有人漏（这条是**承重**的：它挡的是**真金白银的写调用**）。
        判别力：把 `run_outbox` 里 `line.enabled` 那一道闸删掉 ⇒ 本用例红（那一轮会真的打上游、真的把订单推出去）。"""
        self.queue_status_out()
        # ⚠️ 开关**关着**（本用例不 `enable()`），且用 `mock.patch.dict` 明确钉住默认值：
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_LITERAL, None)
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(),
                line=jidanpu_member_openapi_outbox.member_openapi_outbox_line(),
            )

        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.db_order().status, OrderStatus.REFUNDED)
        self.assertEqual(self.db_events(kind="member.status_out").get().status, "pending")
        self.assertEqual(
            stats.blocked_reason, jidanpu_member_openapi_outbox.BLOCKED_SWITCH_OFF_REASON,
        )

    def test_the_binding_is_the_shape_the_ui_really_creates(self):
        """⭐ 夹具自证：「和界面真建出来一模一样」不是嘴上说的——账号密码**都是空的**。
        ⚠️ 这条是**夹具的判据**（E-0202 的教训：夹具为了「能过闸」凑出的形态会绕开真实形态）。
        判别力：给夹具塞一个假密码 ⇒ 本用例红（而那正是上一轮被骗过去的形状）。"""
        self.assertEqual(self.binding.account, "")
        self.assertEqual(self.binding.password, "")
        self.assertEqual(self.binding.mode, "openapi")
        # ②式的凭据判据走**另外两格**（地址 + client_id + client_secret），与账号密码无关。
        self.assertIsNotNone(jidanpu_openapi.openapi_credentials_of(self.binding))
        self.assertEqual(jidanpu_openapi.openapi_credentials_of(self.binding)[0], MEMBER_BASE_URL)


# ══ ③ 推前回读（远端已经是目标态 ⇒ `done` 且**一次写都不打**）════════════════════


class PreReadTests(_OutboxCase):
    def test_a_remote_that_is_already_on_target_finishes_without_any_write(self):
        """⭐ 判据三：远端**已经是**目标标签 ⇒ `done`、⛔ 一次写接口都不打。
        ⚠️ 这一条照①式那份判据**逐条搬**（真机依据：重复推同一状态对方回失败信封「当前已是已退单状态，无需更改」⇒ 直接推会记失败 + 退避重试 5 次 + 把 `last_error` 点红）。
        判别力：把 `push.read_remote(...) == label` 那一跳删掉 ⇒ 本用例红在「恰好只有一次 GET」与「写接口零次」两条上。"""
        self.enable()
        self.queue_status_out()
        self.upstream.set_status(JID, LABEL_REFUNDED)      # 远端**已经**是目标态

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        self.assertEqual(self.db_order().status, OrderStatus.REFUNDED)
        self.assertEqual(self.db_events(kind="member.status_out").get().status, "done")
        # ⭐ 端点集合恰好 = 取 token + **一次**回读（⛔ 没有写、也没有写后回读）：
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])

    def test_a_remote_that_is_unreadable_is_not_treated_as_on_target(self):
        """⭐ 「读不到」≠「已经是目标态」：远端查不到 ⇒ **不写**、记 `failed` 等退避重试。
        ⚠️ 与上一条**成对**：少了它，「读不到就当不需要推」那种实现也能让上一条绿。
        判别力：把「读不到」当成「已达成」⇒ 本用例红（会收敛成 `done`）。"""
        self.enable()
        self.queue_status_out()
        self.upstream.set_status(JID, None)                 # 远端查不到这张单

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        self.assertEqual(self.db_order().status, OrderStatus.REFUNDED)
        event = self.db_events(kind="member.status_out").get()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertIn(jidanpu_member.STATUS_PRE_READ_FAILED_REASON, event.reason)
        # ⛔ 一次写都不许打（判不了就不盲目照推）：
        self.assertNotIn(("POST", CHANGE_ORDER_STATUS_PATH), self.upstream.api_pairs())


# ══ ④ 写后回读（写接口回成功 ≠ 对方真的变了）═══════════════════════════════════


class ConfirmReadTests(_OutboxCase):
    def test_a_write_that_did_not_land_is_not_reported_as_done(self):
        """⭐ 写接口回**成功**、但回读**没读到**目标态 ⇒ `failed` + 人话（⛔ 不收敛 `done`）。
        ⚠️ 这是 E-0087 的同族：`code>0` 只说明「对方收下了这次请求」，真正的判据是**回读**（①式那条路一以贯之的口径，②式照搬）。
        判别力：把写后回读那一跳改成「写成功即 `done`」⇒ 本用例红。"""
        self.enable()
        self.queue_status_out()
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        # 写下去之后对方**没有**变成目标态（真实世界：它改了别的格 / 被别的规则挡回）
        # ⚠️ 必须在**假上游记完这次请求之后**再改回去（本类那份 `handler` 会把状态镜像成
        #    写进去的那个值 —— 那正是「写后回读真的跟着变了」那条判据的载体）。
        original = self.upstream.handler

        def _swallow(request):
            response = original(request)
            if request.method == "POST" and request.url.path == CHANGE_ORDER_STATUS_PATH:
                self.upstream.set_status(JID, LABEL_UNASSIGNED)
            return response

        with mock.patch.object(self.upstream, "handler", _swallow):
            stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_events(kind="member.status_out").get()
        self.assertEqual(event.status, "failed")
        self.assertIn(jidanpu_member.STATUS_CONFIRM_REASON, event.reason)
        # ⭐ 端点集合恰好：读了、写了、**又读了一次**（⛔ 一次都不缺）：
        self.assertEqual(self.upstream.api_pairs(), [
            ("GET", ORDER_STATUS_PATH),
            ("POST", CHANGE_ORDER_STATUS_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])

    def test_a_confirm_read_that_errors_out_is_not_reported_as_done(self):
        """⭐ **写后那一跳报错**（不是「读到别的」、也不是「读不到」）⇒ `failed` + 那一档人话。
        ⚠️ 与下面那条（写被上游拒）、上面那条（写成功但读回别的标签）**成对**：三档成因不同（写压根没成功 / 写成功但没生效 / **确认那一跳没打通**），少一条就会有「三档收敛成同一句人话」的实现溜过去。
        判别力：把 `_consume_status_out` 里「写后回读报错」那一支改成 `return "done"` ⇒ 本用例红（实测：它是本轮**变异矩阵**里唯一逮到那个变异的一条 ⇒ 之前这条网有个洞）。"""
        self.enable()
        self.queue_status_out()
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        # 写**成功**（状态真的变了），但紧随其后的那次回读在传输层就炸了。
        original = self.upstream.handler

        def _write_ok_then_read_booms(request):
            already_wrote = any(
                row["path"] == CHANGE_ORDER_STATUS_PATH for row in self.upstream.requests
            )
            if request.method == "GET" and request.url.path == ORDER_STATUS_PATH and already_wrote:
                raise httpx.ConnectError("合成连接失败")
            return original(request)

        with mock.patch.object(self.upstream, "handler", _write_ok_then_read_booms):
            stats = self.run_a_round(inject_client=True)

        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.done, 0)
        event = self.db_events(kind="member.status_out").get()
        self.assertEqual(event.status, "failed")
        self.assertIn(jidanpu_member.STATUS_CONFIRM_READ_FAILED_REASON, event.reason)
        # ⭐ 订单行**照旧真的变了**（写确实生效了）——失败的是「**确认**」那一跳，
        #    ⛔ 不是「写没上去」；这两件事在台账上是两句不同的话。
        self.assertEqual(self.db_order().status, OrderStatus.REFUNDED)
        self.assertEqual(self.upstream.write_pairs(), [("POST", CHANGE_ORDER_STATUS_PATH)])

    def test_an_upstream_refusal_keeps_the_original_msg_in_the_reason(self):
        """⭐ 写被上游拒 ⇒ `failed` + 人话里**带着对方那句原话**（⛔ 不写一句空泛的「失败了」）。
        ⚠️ 这条判据来自本仓的既有教训（`jidanpu_member._upstream_reason` 的注释）：运维在下家系统里没有我们的日志，台账上那句 `msg` 是**唯一**能定位根因的东西。
        判别力：把 `_translate` 里的 `body={"msg": …}` 删掉 ⇒ 本用例红。"""
        self.enable()
        self.queue_status_out()
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        self.upstream.boom(CHANGE_ORDER_STATUS_PATH, OpenApiError(
            step="change_order_status", kind="permission", http_status=200,
            upstream_code=0, upstream_msg="你没有权限",
        ))

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_events(kind="member.status_out").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("你没有权限", event.reason)


class RemarkTests(_OutboxCase):
    """⭐ **`remark` 那一格**（业务方裁定 **B**：用**一句固定常量**）。
    ⚠️ 步骤 2 那份「这两档还缺 `remark` 的来源」的残差用例已被本条**取代**（它钉的是「推不通」那个**当时的**现状；现在通了 ⇒ 判据改成「**带的东西对不对**」）：推「问题单」⇒ 请求体里**带**那句固定常量（不带会被对方拒：`code=0`「系统要求问题单必须填写原因」——步骤 0 实测）；推「已退单」⇒ 请求体里**不带**（照①式：那条路历来不传 remark）。
    """

    def _push_issue(self):
        """把这张单推成「问题单」（远端此刻是「未分配」⇒ 推前回读不会短路掉这次写）。"""
        self.enable()
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO_B, status=OrderStatus.ISSUE,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"), project_text="合成项目丙",
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID_B, in_hall=True,
        )
        self.upstream.set_status(JID_B, LABEL_UNASSIGNED)
        self.queue_status_out()
        return self.run_a_round()

    def test_an_issue_push_carries_the_fixed_remark_constant(self):
        """⭐ 推「问题单」⇒ 请求体里那一格**逐字**是那句固定常量（⛔ 不是订单里的备注）。
        判别力：把 `write_remote_status` 里那一支改成不传 remark（或改成拿订单字段拼）⇒ 本用例红。
        """
        stats = self._push_issue()

        self.assertEqual(stats.done, 1)
        body = self.upstream.json_calls(CHANGE_ORDER_STATUS_PATH)[0]
        self.assertEqual(body["status"], LABEL_ISSUE)
        #: ⭐ **逐字**那句常量（⛔ 不写成 `模块.常量`：自指断言在被断言的常量改坏时不会红，E-0024）。
        self.assertEqual(body["remark"], REMARK_LITERAL)
        self.assertEqual(
            jidanpu_member_openapi_outbox.STATUS_REMARK, REMARK_LITERAL,
        )

    def test_a_refund_push_carries_no_remark_at_all(self):
        """⭐ 推「已退单」⇒ **不带** `remark`（照①式那一条路；带与不带**各自能红**）。
        判别力：把「带 remark」那一支写成「所有档都带」⇒ 本用例红。
        """
        self.enable()
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        self.queue_status_out()
        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        body = self.upstream.json_calls(CHANGE_ORDER_STATUS_PATH)[0]
        self.assertEqual(body["status"], LABEL_REFUNDED)
        self.assertEqual(body.get("remark"), "")


# ══ ⑤ 步骤 4：`member.withdraw`（退单）═════════════════════════════════════════
# ⚠️ 步骤 2/3 那一节（`NotWiredTests`：两档「显式跳过 + 一句人话」）**已被本节取代**——它钉的是「还没通」那个**当时的**现状；现在通了 ⇒ 判据改成「**真的推过去了、且带的东西对**」，「未通档归零」这条判据由 `LineShapeTests` 的**恰好集合**接手（`unsupported` 必须空表）。
# ⚠️⭐ **D-0249（修 E-0246）改准：本节只办 `member.withdraw`（退单）**。历史（原文见分支点提交）：本节曾兼办「撤下大厅」（裁定收敛成已退单、判据只认 `status == 已退单`）——E-0246 证明那个落点会把**接手人自己店里的活单**退掉 ⇒ ②式的 `member.unlist` 现在在**分派处**终态 skip、零上游调用（判据见 `UnlistUnavailableTests` 与新的现场回归那条），本节不再有它的用例；`member.withdraw`（我方真退单）**照旧无条件推**、判据不变。

#: `member.unlist` / `member.withdraw` 两个字面值（⛔ 不从被测常量派生，E-0024）。
KIND_WITHDRAW_LITERAL = "member.withdraw"
KIND_UNLIST_LITERAL = "member.unlist"


class WithdrawTests(_OutboxCase):
    """②式的退单 / 撤下大厅：**同一个动作**（`ChangeOrderStatus status=已退单`）+ 写后回读。"""

    def setUp(self):
        super().setUp()
        #: 远端那张单**此刻不是**目标态（未分配）⇒ 「写下去真的变了」才有读数可看
        #: （⛔ 别让夹具一上来就是已退单：那样写不写都绿）。
        self.upstream.set_status(JID, LABEL_UNASSIGNED)

    # ── 夹具与读数 ──────────────────────────────────────────────────────────

    def db_link(self):
        """**重新查库**读链路那一行（⛔ 不读内存对象：那会把「没写」看成本地那次改动）。"""
        return JidanpuMemberOrderLink.objects.get(pk=self.link.pk)

    def run_with_status_snapped_back(self, kind):
        """跑一轮，但**写接口落下去之后**把远端状态**改回去**（造「写成功却没真生效」）。
        形状照 `ConfirmReadTests` 那两条（那一份是本文件里既有的做法）：⛔ 不是给假上游加一个「假装」开关——现场的镜像照旧是真的，只有这一条用例在**写完那一刻**把事实改掉（真实世界：对方改了别的格 / 被别的规则挡回）。
        """
        self.enable()
        self.queue_kind(kind)
        original = self.upstream.handler

        def _swallow(request):
            response = original(request)
            if request.method == "POST" and request.url.path == CHANGE_ORDER_STATUS_PATH:
                self.upstream.set_status(JID, LABEL_UNASSIGNED)
            return response

        with mock.patch.object(self.upstream, "handler", _swallow):
            return self.run_a_round()

    # ── ① 端到端：退单 ─────────────────────────────────────────────────────

    def test_parameter_error_does_not_confirm_withdrawal_and_retries_after_recovery(self):
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)
        original = self.upstream.handler

        def reject_withdraw_and_lookup(request):
            response = original(request)
            if request.method == "POST" and request.url.path == CHANGE_ORDER_STATUS_PATH:
                self.upstream.set_status(JID, LABEL_UNASSIGNED)
                return httpx.Response(200, json={"code": 0, "msg": "合成退单被拒绝", "data": ""})
            if request.method == "GET" and request.url.path == ORDER_STATUS_PATH:
                return httpx.Response(200, json={
                    "code": 0, "msg": "系统单号和来源单号不可同时为空", "data": "",
                })
            return response

        with mock.patch.object(self.upstream, "handler", reject_withdraw_and_lookup):
            first = self.run_a_round()

        self.assertEqual(first.failed, 1)
        self.assertIsNone(self.db_link().withdrawn_at)
        self.assertTrue(self.db_link().in_hall)
        self.assertEqual(self.upstream.statuses[JID], LABEL_UNASSIGNED)
        self.assertEqual(self.run_a_round(now=timezone.now() + timedelta(hours=1)).done, 1)
        self.assertIsNotNone(self.db_link().withdrawn_at)
        self.assertEqual(self.upstream.statuses[JID], LABEL_REFUNDED)

    def test_a_withdraw_really_refunds_the_remote_order_and_downgrades_the_link(self):
        """⭐⭐ 判据十五（端到端退单）：开开关跑一轮 ⇒ **远端那张单真的变了** + 链路降级 + `done`。
        ⚠️ 判据落在**哪几行**（这一档的载体与推状态那一档不同，如实写清）：① **远端现场**（假上游自己的账本）：那张单 `未分配` → **`已退单`**；② **链路行**（数据库行）：`in_hall` True → False（重新查库）；③ **台账行**（数据库行）：`pending` → `done`。⚠️ **我方订单行**不在此列：我方退单那一刻本地就已经是「已退单」了，这一档做的是「把**远端**那张单收干净 + 把链路降级」——⛔ 不编一条它没做的事（①式那一档 `WithdrawConsumeTests` 同样断言链路行，不是订单行）。⭐ 端到端三段都钉住：**推**（写接口的请求体）/ **确认**（回读那一跳真的打过）/ **收口**（台账 `done` + 链路降级），⛔ 不看「调用过就算」。
        判别力（每条都能红）：① `_consume_withdraw` 不调 `withdraw.refund_remote` ⇒ 远端仍是「未分配」、回读不认、台账 `failed`；② 把「写后回读」那一跳删掉（写成功即 `done`）⇒ 端点集合少了那一次 `GET`；③ 把收尾那次 `in_hall=False` 删掉 ⇒ 链路行断言红。"""
        before_link = self.db_link()
        self.assertFalse(self.binding.account or self.binding.password, "夹具不该有账号密码")
        self.assertEqual(before_link.in_hall, True)

        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)
        stats = self.run_a_round()

        # ①②③ 三处**数据库行 / 远端现场**：
        self.assertEqual(self.upstream.statuses[JID], LABEL_REFUNDED, "远端那张单没被改成已退单")
        self.assertEqual(self.db_link().in_hall, False, "链路没降级")
        row = self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get()
        self.assertEqual((row.status, row.attempts), ("done", 0))
        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        # ⭐ **推 + 确认两跳都真的打了**（有序、逐条相等）：取 token → 写状态 → **回读**
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("POST", CHANGE_ORDER_STATUS_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])

    def test_a_real_local_refund_changes_the_order_row_and_then_refunds_the_remote_one(self):
        """⭐⭐ 端到端**最全的那一段**：我方真的把单退掉（走状态机）⇒ **订单行真的变了** ⇒ 那条退单台账被这一轮推到远端 ⇒ **远端那张单也变成已退单** + 链路降级。
        ⚠️ 与上一条的分工（⛔ 两条都不是多余的）：上一条从「台账已经排好」起跑、判据打在**链路行 / 台账行 / 远端现场**（因为那一档**不改我方订单行**：我方退单那一刻本地就已经是「已退单」了）；这一条从**我方的退单动作**起跑 ⇒ 「订单行真的变了」在这里是**字面成立**的（写它的是状态机，⛔ 不是本用例手改一行）。⚠️ 排队那一跳用**既有触发点** `jidanpu_member.queue_withdraw`（⛔ 不手搓台账行）。退单会**顺带**排一条 `member.status_out`（「已退单」在推送集里）⇒ 这一轮办**两条**；两者在②式下都写「已退单」，第二条的回读确认照旧生效（本用例只对 withdraw 那一行取读数）。
        判别力：① 不调 `queue_withdraw` ⇒ 一条退单台账都没有 ⇒ 远端一个字都没变（红）；② 把②式那一发接回①式（要登录凭据）⇒ ②式绑定在建连处抛 `ConfigError` ⇒ 红。"""
        order_b = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=order_b, jidanpu_order_id=JID_B, in_hall=True,
        )
        self.upstream.set_status(JID_B, LABEL_UNASSIGNED)
        self.enable()

        with transaction.atomic():
            apply_transition(
                order_b, OrderStatus.REFUNDED,
                nodes=frozenset({"order.change_status"}), actor="djsync-openapi-actor",
            )
        #: ⭐ 退单那一刻的**既有触发点**（接口层就是这么排的；⛔ 不手搓台账行）。
        jidanpu_member.queue_withdraw(order_b)

        self.run_a_round()

        # ① ⭐ **我方订单行真的变了**（重新查库）：
        self.assertEqual(Order.objects.get(pk=order_b.pk).status, OrderStatus.REFUNDED)
        # ② ⭐ 远端那张单也真的变成了「已退单」（假上游的现场读数）：
        self.assertEqual(self.upstream.statuses[JID_B], LABEL_REFUNDED)
        # ③ 链路降级 + 台账 `done`：
        self.assertEqual(
            JidanpuMemberOrderLink.objects.get(binding=self.binding, order=order_b).in_hall,
            False,
        )
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                target_ref=ORDER_NO_B, kind=KIND_WITHDRAW_LITERAL, direction="out",
            ).get().status,
            "done",
        )

    # ── ② 撤下大厅 = 终态 skip、零上游（⭐ D-0249 改准，⛔ 不是历史上的「已退单」）─────
    def test_an_unlist_is_a_terminal_skip_without_touching_upstream(self):
        """⭐⭐ 判据十六（D-0249 改准，修 E-0246）：②式的 `member.unlist` ⇒ **终态 `skipped`、一次上游都不打**——远端明明是「未分配」（历史上会被推成已退单的那一档）也**不办**。
        ⚠️⚠️ 为什么这么狠（E-0246 现场）：②式上 unlist 只可能排给**接手人自己的绑定** ⇒ 那张「未分配」的远端单就是**接手人自己店里的活单**，动它 = 退掉自己的单（业务方原话：「指派的打手和这个同步过去的鸡蛋铺是同一账号，不应该产生退单问题」）。台账 kind 照旧是 `member.unlist`（可追溯），终态形状与「还没通」同款：`skipped`、`attempts == 0`（⛔ 不是 `failed` 退避重试）。
        判别力：① 把分派拦截删掉（unlist 落回 `_consume_unlist` 四跳）⇒ OpenApiClient 没有①式那套方法 ⇒ 未预期异常，本用例红；② 把拦截改成「先预读再决定」⇒ 端点集合里多了 `GetOrderStatus` ⇒ 最后一条断言红；③ 把这一档改回「收敛成已退单」（历史形态）⇒ 远端现场与写接口断言双双红。"""
        self.upstream.set_status(JID, LABEL_UNASSIGNED)   # 「能办」的形态也不办
        self.enable()
        self.queue_kind(KIND_UNLIST_LITERAL)

        stats = self.run_a_round()

        self.assertEqual((stats.skipped, stats.failed, stats.done), (1, 0, 0))
        # ⭐ **零上游调用**（含取 token 那一跳——「一次都不打」要打到这个强度）：
        self.assertEqual(self.upstream.all_pairs(), [])
        row = self.db_out_events(kind=KIND_UNLIST_LITERAL).get()
        self.assertEqual(row.status, "skipped")
        self.assertEqual(row.attempts, 0)
        # 要人话（**字面值**，⛔ 不从被测常量派生，E-0024）：
        self.assertIn(UNLIST_UNAVAILABLE_REASON_LITERAL, row.reason)
        # 链路与远端现场一个字节都不动（撤它 = E-0246）：
        self.assertTrue(self.db_link().in_hall)
        self.assertIsNone(self.db_link().withdrawn_at)
        self.assertEqual(self.upstream.statuses[JID], LABEL_UNASSIGNED)

    # ── ③ 不带 remark（逐字全等）───────────────────────────────────────────

    def test_the_refund_request_body_is_exactly_two_keys(self):
        """⭐ 判据十七：`已退单` 那一档的请求体**恰好** `{"orderId": …, "status": …}`（无 `remark`）。
        ⚠️ 业务方裁定 B 的那一半：**只有**「问题单 / 已暂停」必带 `remark` ⇒ `已退单` **照①式不带**（①式的改状态历来不传 remark）。⭐ 判据用**逐字全等**（多一个键就不等）⇒ 无论加的是 `remark` 还是别的什么都会红。
        判别力：把 `refund_remote` 改成「所有档都带 remark」（或复用推状态那一份 `write_remote_status` 的带 remark 分支）⇒ 本用例红。"""
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)

        self.run_a_round()

        body = self.upstream.json_calls(CHANGE_ORDER_STATUS_PATH)[0]
        self.assertEqual(body, {"orderId": JID, "status": LABEL_REFUNDED})
        self.assertNotIn("remark", body)

    # ── ④ 回读不认 ⇒ 不许 done（两个 kind 各一条）────────────────────────────

    def test_a_withdraw_that_does_not_read_back_as_refunded_is_not_done(self):
        """⭐ 判据十八：**写接口回成功、但回读读到的不是「已退单」** ⇒ `failed`，⛔ 不 `done`。
        ⚠️ E-0087 的同族：`code>0` 只说明「对方收下了这次请求」，真正的判据是**回读**。
        判别力：把写后回读那一跳改成「写成功即 `done`」⇒ 本用例红（台账会变 `done`、链路会被降级）。"""
        stats = self.run_with_status_snapped_back(KIND_WITHDRAW_LITERAL)

        self.assertEqual((stats.failed, stats.done), (1, 0))
        row = self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get()
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.attempts, 1)
        self.assertIn(jidanpu_member.WITHDRAW_CONFIRM_REASON, row.reason)
        # ⛔ 没确认到 ⇒ **不许**降级链路（那会把「没办成」说成「办成了」）：
        self.assertEqual(self.db_link().in_hall, True)

    def test_an_unlist_that_would_not_read_back_as_refunded_never_gets_that_far(self):
        """⭐ 判据十八（撤下那一档，⭐ D-0249 改准）：**「写 + 回读」那一套对 unlist 不存在**——unlist 根本不打上游，skip 是终态、连重试的资格都没有。
        历史：本用例曾钉「unlist 收敛进 withdraw handler 后写后回读不认 ⇒ 不许 done」；那条收敛路已随 D-0249 摘除（E-0246）。
        判别力：把 `member.unlist` 另接一条「写 + 回读」的实现（历史形态）⇒ 本用例红（台账会变 `failed`、`attempts ≥ 1`）。"""
        self.enable()
        self.queue_kind(KIND_UNLIST_LITERAL)

        stats = self.run_a_round()

        row = self.db_out_events(kind=KIND_UNLIST_LITERAL).get()
        self.assertEqual((stats.skipped, stats.failed, stats.done), (1, 0, 0))
        self.assertEqual((row.status, row.attempts), ("skipped", 0))
        self.assertEqual(self.upstream.all_pairs(), [])
        self.assertEqual(self.db_link().in_hall, True)

    # ── ⑤ 边界：无链路 / 上游拒但回读确认 ───────────────────────────────────

    def test_a_withdraw_without_a_link_is_skipped_without_touching_upstream(self):
        """⭐ 判据十九：本地没有链路（从未录到对方系统）⇒ `skipped` + 人话，**一次上游都不打**。
        判别力：把 `_consume_withdraw` 里那句「没有链路 ⇒ skip」删掉 ⇒ 会拿着一个空远端单号去打上游 ⇒ 本用例红（第一条断言）。
        """
        self.link.delete()
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)
        before = self.db_order()

        stats = self.run_a_round()

        self.assertEqual((stats.skipped, stats.done, stats.failed), (1, 0, 0))
        self.assertEqual(self.upstream.requests, [])
        row = self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get()
        self.assertEqual(row.status, "skipped")
        self.assertTrue(row.reason.strip())
        self.assertEqual(self.db_order().status, before.status)

    def test_a_business_failure_still_wins_when_the_readback_confirms(self):
        """⭐ 判据二十：重复推送可能回**业务失败**（对方原话「当前已是已退单状态，无需更改」）⇒ 只要**回读**确认已是目标态，仍算 `done`（照①式那条既有的「回读为准」）。
        ⚠️ 这一条钉的是「写那一跳的失败**被吞掉、交给回读判**」这条设计：少了它，一个「写接口抛错就直接 `failed`」的实现也能让前几条绿。
        判别力：把 `_consume_withdraw` 里那段 `except ApiError: pass` 去掉（改成直接把失败记成 `failed`）⇒ 本用例红。"""
        self.upstream.set_status(JID, LABEL_REFUNDED)      # 远端**已经**是目标态
        self.upstream.boom(CHANGE_ORDER_STATUS_PATH, OpenApiError(
            step="change_order_status", kind="other", http_status=200,
            upstream_code=0, upstream_msg="当前已是已退单状态，无需更改",
        ))
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)

        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed), (1, 0))
        self.assertEqual(self.db_link().in_hall, False)
        self.assertEqual(self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get().status, "done")

    # ── ⑥ ⭐⭐ 对方明确说「没有这张单」⇒ 终态、⛔ 不再退──────────────

    def test_accepted_withdrawal_with_confirmed_absence_counts_in_summary(self):
        self.enable()
        self.assertEqual(jidanpu_member.queue_withdraw(self.order, summarize=True), 1)
        original = self.upstream.handler

        def disappears_after_acceptance(request):
            response = original(request)
            if request.method == "POST" and request.url.path == CHANGE_ORDER_STATUS_PATH:
                self.upstream.set_status(JID, None)
            return response

        with mock.patch.object(self.upstream, "handler", disappears_after_acceptance):
            stats = self.run_a_round()

        self.assertEqual((stats.skipped, stats.done, stats.failed), (1, 0, 0))
        self.assertIn("对方那边已无此单", self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get().reason)
        self.assertEqual(OperationLog.objects.filter(action="order.jidanpu_withdraw").count(), 1)
        self.assertEqual(self.upstream.api_pairs(), [
            ("POST", CHANGE_ORDER_STATUS_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])

    def test_accepted_withdrawal_with_business_query_failure_is_not_logged_as_success(self):
        self.enable()
        self.assertEqual(jidanpu_member.queue_withdraw(self.order, summarize=True), 1)
        original = self.upstream.handler

        def temporarily_unavailable(request):
            if request.method == "GET" and request.url.path == ORDER_STATUS_PATH:
                self.upstream._record(request)
                return httpx.Response(200, json={"code": 0, "msg": "查单服务暂不可用", "data": ""})
            return original(request)

        with mock.patch.object(self.upstream, "handler", temporarily_unavailable):
            stats = self.run_a_round()

        self.assertEqual((stats.skipped, stats.done, stats.failed), (0, 0, 1))
        self.assertIsNone(self.db_link().withdrawn_at)
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())
        self.assertEqual(self.db_events(kind="member.withdraw_summary").get().status, "pending")
        self.assertEqual(self.upstream.api_pairs(), [
            ("POST", CHANGE_ORDER_STATUS_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])
        self.assertEqual(self.run_a_round(now=timezone.now() + timedelta(hours=1)).done, 1)
        self.assertIsNotNone(self.db_link().withdrawn_at)
        self.assertEqual(self.db_events(kind="member.withdraw_summary").get().status, "done")

    def test_local_off_hall_flag_cannot_confirm_another_copy_was_cleared(self):
        self.enable()
        other = self.make_binding()
        other.jidanpu_base_url = "https://other-shop.example.test"
        other.save(update_fields=["jidanpu_base_url"])
        JidanpuMemberOrderLink.objects.create(
            binding=other, order=self.order, jidanpu_order_id=JID_B, in_hall=True,
        )
        self.upstream.set_status(JID_B, LABEL_UNASSIGNED)
        self.assertEqual(jidanpu_member.queue_withdraw(self.order, hall_only=True, summarize=True), 2)
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(in_hall=False)

        self.assertEqual(self.run_a_round().skipped, 1)
        other_stats = jidanpu_member_openapi_outbox.run_member_status_outbox(other, now=timezone.now())
        self.assertEqual(other_stats.done, 1)
        self.assertEqual(self.db_events(kind="member.withdraw_summary").get().status, "failed")
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())

    def test_a_withdraw_against_an_order_the_remote_does_not_have_is_a_terminal_skip(self):
        """⭐⭐ 判据二十一：**对方明确说没有这张单** ⇒ 终态 + 一句人话，⛔ 不再重试、⛔ 不再被排。
        真机依据：那一类单在对方系统里**已经不存在**（`GetOrderStatus` 按单号精确查都查不到）⇒ 写后回读**永远过不了** ⇒ 重试到放弃后又被对账排回来（179 条卡死；死循环）。读数分四段（⛔ 不看「调用过就算」）：① 台账落**终态 `skipped`** + **一句人话**（「对方那边已无此单，无需退单」）；② ⛔ **不烧重试额度**（`attempts == 0`：终态不是失败）；③ 链路行落「**已经退成**」（`withdrawn_at` 非空）⇒ **下一轮对账不再排它**；④ 再跑一轮（时钟推过退避窗）⇒ **一次上游都不打**、台账仍是**一行**。
        判别力：把 `_MEMBER_OPENAPI_LINE` 那份 `absent_means_gone_reason` 清成空串（退回「读不到 ⇒ 当失败重试」的旧行为）⇒ ①红（台账变 `failed`）、③红（标记没落）。"""
        #: 远端**没有这张单**（假上游按真机那个失败信封回：`code=0` + `data=""`）。
        self.upstream.set_status(JID, None)
        #: ⭐ 「对方明确说没有这张单」这一档**伴随**着写失败（对方那边没有这张单 ⇒ `ChangeOrderStatus` 回业务失败 ⇒ 共享 handler 照旧吞掉、交给回读判）。
        #: ⚠️ 必须让写那一跳真的失败：假上游的缺省回包是**成功**，而它成功时会把 `statuses[JID]` **变出来**
        #: （那就不是「对方没有这张单」了，用例会静默变成另一档）。
        self.upstream.boom(CHANGE_ORDER_STATUS_PATH, OpenApiError(
            step="change_order_status", kind="other", http_status=200, upstream_code=0,
            upstream_msg="没有相关订单信息",
        ))
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)

        stats = self.run_a_round()

        # ① 终态 + 一句人话（**字面值**，⛔ 不从被测常量派生，E-0024）
        self.assertEqual((stats.skipped, stats.failed, stats.done), (1, 0, 0))
        row = self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get()
        self.assertEqual(row.status, "skipped")
        self.assertIn("对方那边已无此单，无需退单", row.reason)
        # ② 终态不烧额度（有额度 ⇒ `_retry` 那条路）
        self.assertEqual(row.attempts, 0)
        # ③ 链路：不在大厅 + **记上「已经退成」**（对账不再排它的那个终态标记）
        link = self.db_link()
        self.assertFalse(link.in_hall)
        self.assertIsNotNone(link.withdrawn_at, "没落「已经退成」⇒ 下一轮对账又会排一条")

        # ④ 不再打、不再排、不再多一行
        before = len(self.upstream.requests)
        later = self.run_a_round(now=timezone.now() + timedelta(hours=2))
        self.assertEqual((later.skipped, later.failed, later.done), (0, 0, 0))
        self.assertEqual(len(self.upstream.requests), before, "终态之后一次上游都不许再打")
        self.assertEqual(self.db_out_events(kind=KIND_WITHDRAW_LITERAL).count(), 1)
        self.assertEqual(
            jidanpu_member.reconcile_and_queue(
                self.binding, now=timezone.now()
            ).withdraw,
            0,
            "对方已无此单 ⇒ 对账⛔ 不许再排退单",
        )

    def test_a_transport_failure_on_the_readback_is_still_a_failure(self):
        """⭐ 判据二十一的反向控制：**传输层失败**⛔ 不许被当成「对方没有这张单」。
        ⚠️ 这一条与本文件其余「反向控制」同一条纪律：少了它，一个「只要回读抛就收口成终态」的实现也能让上面那条绿——而那会把**真的故障**静默变成「这张单退过了」。
        判别力：把 `read_remote_status_or_gone` 里的 `find_order_status` 换成「`except OpenApiError: return None`」⇒ 本用例红（台账会变 `skipped`）。"""
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)
        original = self.upstream.handler

        def _boom(request):
            if request.method == "GET" and request.url.path == ORDER_STATUS_PATH:
                raise httpx.ConnectError("合成断连")
            return original(request)

        with mock.patch.object(self.upstream, "handler", _boom):
            stats = self.run_a_round()

        self.assertEqual((stats.failed, stats.skipped, stats.done), (1, 0, 0))
        row = self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get()
        self.assertEqual(row.status, "failed")
        self.assertIn(jidanpu_member.WITHDRAW_READBACK_REASON, row.reason)
        #: ⛔ 真故障不许落「已经退成」（落了就等于**永远不再退**）
        self.assertIsNone(self.db_link().withdrawn_at)

    # ── ⑦ ⭐⭐ D-0249（修 E-0246）：②式 `member.unlist` **一律终态 skip** ────
    #    ⚠️ 整节按**远端状态分档**各钉一条「skip + 零上游」；`member.withdraw`（我方真退单）**照旧无条件推**——见本节最后那一条的钉子。


class UnlistUnavailableTests(_OutboxCase):
    """⭐⭐ **D-0249（修 E-0246）**：②式 `member.unlist` **一律终态 skip、一次上游都不打**（不分档）。
    历史（原文见分支点提交）：本节曾是 D-0227 / E-0236 的「推已退单前预读闸」——预读=**未分配** ⇒ 放行推已退单；E-0246 证明**那一档正是事故本身**：②式上 unlist 只可能排给**接手人自己的绑定**，「未分配」那张远端单就是**接手人自己店里的活单**（kexing 34 张被推成已退单）⇒ D-0249：不分档，**全部 skip**——E-0236 的「别动对方正在做的单」由「**永远不打上游**」结构性承接（比闸更强，不再需要预读；探针纪律：skip 必须**终态、零请求**）。
    ⚠️ `member.withdraw`（我方真退单）**不受这一格影响**（最后一条钉子保住这个边界）。"""

    def setUp(self):
        super().setUp()
        #: 缺省摆成「未分配」（既有那条用例的口径）：本节的用例各自按档重摆。
        self.upstream.set_status(JID, LABEL_UNASSIGNED)

    def db_link(self):
        """**重新查库**读链路那一行（⛔ 不读内存对象：那会把「没写」看成本地那次改动）。"""
        return JidanpuMemberOrderLink.objects.get(pk=self.link.pk)

    def assert_unlist_skipped(self, stats):
        """四个档共用的判据体（⛔ 判据只有一份，各档只摆现场）：skip 终态 + 零请求 + 不动现场。"""
        # ① 不打：**一条请求都不许有**（含取 token ——「一次上游都不打」要打到这个强度）：
        self.assertEqual(self.upstream.all_pairs(), [])
        self.assertEqual(self.upstream.write_pairs(), [])
        # ② 终态 skip（⛔ 不是 `failed`：重试也不会变出一个不存在的接口）：
        self.assertEqual((stats.skipped, stats.failed, stats.done), (1, 0, 0))
        row = self.db_out_events(kind=KIND_UNLIST_LITERAL).get()
        self.assertEqual(row.status, "skipped")
        self.assertEqual(row.attempts, 0)
        # ③ 要人看的台账人话（**字面值**，⛔ 不从被测常量派生，E-0024）：
        self.assertIn(UNLIST_UNAVAILABLE_REASON_LITERAL, row.reason)
        # ④ 链路与远端现场一个字节都不动（撤它 = E-0246）：
        self.assertTrue(self.db_link().in_hall)
        self.assertIsNone(self.db_link().withdrawn_at)

    def test_an_unlist_with_a_taken_remote_status_is_skipped_for_humans(self):
        """⭐⭐ **E-0236 回归**：远端=待验收（对方打手在做）⇒ skip + **零请求**。
        历史：D-0227 闸 2 曾在这里拦下（skipped + 人话点名读到什么）；D-0249 之后保护更强——**根本不预读**，一条请求都不发。
        """
        self.upstream.set_status(JID, "待验收")            # 对方那张单已被接走 / 在途
        self.enable()
        self.queue_kind(KIND_UNLIST_LITERAL)

        stats = self.run_a_round()

        self.assert_unlist_skipped(stats)
        self.assertEqual(self.upstream.statuses[JID], "待验收", "远端现场被动了 = E-0236")

    def test_an_unlist_on_an_unassigned_remote_is_also_skipped(self):
        """⭐⭐ **E-0246 现场档**：远端=未分配（历史上被闸**放行**、推成已退单的那一档）⇒ 照样 skip + 零请求。
        为什么它是承重档：②式上 unlist 只排给**接手人自己的绑定**，「未分配」那张远端单 = **接手人自己店里的活单**——这正是 E-0246 的 34 张。
        """
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        self.enable()
        self.queue_kind(KIND_UNLIST_LITERAL)

        stats = self.run_a_round()

        self.assert_unlist_skipped(stats)
        self.assertEqual(self.upstream.statuses[JID], LABEL_UNASSIGNED)

    def test_an_unlist_on_an_already_refunded_remote_does_not_touch_the_link(self):
        """⭐ 远端=**已退单**（目标态「碰巧」已成立）⇒ 照样 skip，且**链路一个字节都不动**。
        ⚠️ 与历史行为的分界（原文见分支点提交）：旧形态会「done + 把链路降级 + 落 `withdrawn_at`」；skip 的语义是「这一档不办」，⛔ 不许顺手替 withdraw 把「已经退成」落下来——那个终态标记是**真退单**那一档的结论，只有它能写。
        """
        self.upstream.set_status(JID, LABEL_REFUNDED)
        self.enable()
        self.queue_kind(KIND_UNLIST_LITERAL)

        stats = self.run_a_round()

        self.assert_unlist_skipped(stats)
        self.assertEqual(self.upstream.statuses[JID], LABEL_REFUNDED)

    def test_an_unlist_against_an_order_the_remote_does_not_have_is_also_a_skip(self):
        """⭐ 对方**查无此单** ⇒ 照样 skip + 零请求，链路照旧不动。
        历史：旧形态走 `absent_means_gone` 终态收口（done + 链路降级）；D-0249 之后 unlist 不再替「对方不该有这张单」这件事收口——那是 `member.withdraw` 的差集（对账的 `to_withdraw`）与它自己的回读判据的事。
        """
        self.upstream.set_status(JID, None)
        self.enable()
        self.queue_kind(KIND_UNLIST_LITERAL)

        stats = self.run_a_round()

        self.assert_unlist_skipped(stats)

    def test_a_withdraw_is_still_pushed_unconditionally(self):
        """⭐⭐ **边界钉子**：同样远端=待验收，`member.withdraw` 照旧无条件推成已退单。
        ⚠️ 这条钉的是新格子（`unlist_unavailable_reason`）的**边界**：它只声明 `member.unlist` 不可用，⛔ 不许扩大化把退单也拦下（我方真退单，对方那份无论什么状态都该终止——既有业务规则）。少了它，一个「把新格子扩到两个 kind」的过度修正也能让上面几条全绿。
        """
        self.upstream.set_status(JID, "待验收")            # 同上一条那个「在途」档
        self.enable()
        self.queue_kind(KIND_WITHDRAW_LITERAL)

        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        self.assertEqual(self.upstream.statuses[JID], LABEL_REFUNDED, "退单被误伤 ⇒ 远端没收口")
        self.assertEqual(self.upstream.write_pairs(), [("POST", CHANGE_ORDER_STATUS_PATH)])
        self.assertEqual(self.db_out_events(kind=KIND_WITHDRAW_LITERAL).get().status, "done")
        self.assertFalse(self.db_link().in_hall)


# ══ ⑥ 结构（⭐ 两条线的差异面各自覆盖哪些 kind：恰好集合）════════════════════════


class LineShapeTests(_OutboxCase):
    def test_the_two_lines_together_cover_exactly_every_outbox_kind(self):
        """⭐ **恰好集合**：①式**六档全通 + 一档显式未通** + ②式**七档全有明确处置** ⇒ 两条线的「还没通」表合起来 = `OUTBOX_KINDS` 里**②式办不到的那一档**，而**已通集合恰好 = `OUTBOX_KINDS`**。
        ①式没有经过实测的「改对方项目名称」写接口，`member.project` 登记成「还没通」，那张表不是空的。⚠️ **D-0249 口径**：②式对 `member.unlist` 的「已通」指的是「引擎认识它、分派不会响亮抛」——办不办由 `unlist_unavailable_reason` 那一格声明（终态 skip，见 `UnlistUnavailableTests`），⛔ 不算「还没通」。⚠️ **既有六档的行为一个字都没改**：本用例下面那几条把「①式只多了这一档」也钉住了。
        判别力（五个方向都能红）：① 新增一个 kind 却没人登记（`OUTBOX_KINDS` 里多出来）⇒ 已通集合少一格 ⇒ 红；② ②式误把某一档登记回「还没通」（`unsupported` 里多一格）⇒ 红；③ 登记一个不存在的 kind ⇒ 已通集合多一格 ⇒ 红（那正是「未通档归零」的反面）；④ ②式把**已通的那一档**也登记进「还没通」⇒ 下面那条 `assertIsNone` 红；⑤ **①式多登记了一档**（把既有六档里的哪一档也说成"还没通"）⇒ 红。⚠️ ②式那一边的同一件事还有一条**导入即红**的自检（`_unsupported_kinds`）。"""
        up_line = jidanpu_member.member_outbox_line()
        openapi_line = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        wired = {jidanpu_member.KIND_STATUS_OUT, jidanpu_member.KIND_LAUNCH,
                 jidanpu_member.KIND_ASSIGN, jidanpu_member.KIND_PRICE,
                 jidanpu_member.KIND_WITHDRAW, jidanpu_member.KIND_UNLIST,
                 jidanpu_member.KIND_PROJECT, jidanpu_member.KIND_SOURCE_ORDER_NO}

        #: ⭐⭐ ①式那一张表**恰好只有 `member.project` 这一档**（字面期望，⛔ 不写 `模块.常量`）。
        self.assertEqual(
            sorted(up_line.unsupported), sorted(UP_LINE_UNSUPPORTED_KIND_LITERALS),
        )
        #: ⚠️ 而且它必须**带一句人话**（空串 = 说不清为什么 ⇒ 引擎自己会响亮抛）。
        for kind, reason in up_line.unsupported.items():
            with self.subTest(kind=kind):
                self.assertTrue(reason.strip())
        #: ⭐ ①式**既有那六档一个都不许**出现在「还没通」表里（行为一个字没变）。
        for kind in sorted(wired - set(UP_LINE_UNSUPPORTED_KIND_LITERALS)):
            with self.subTest(kind=kind):
                self.assertIsNone(up_line.unsupported.get(kind))
        self.assertEqual(sorted(openapi_line.unsupported), sorted(NOT_WIRED_KIND_LITERALS))
        #: ⭐⭐ 「未通档归零」：②式那一张表今天**是空的**（字面期望，⛔ 不写 `模块.常量`）。
        self.assertEqual(openapi_line.unsupported, {})
        self.assertEqual(
            set(up_line.unsupported) | set(openapi_line.unsupported) | wired,
            set(jidanpu_member.OUTBOX_KINDS),
        )
        # ⭐ ②式**已通**的那几档必须**不在**「还没通」表里：
        # 登记进去会让一条本来办得成的事件变成静默跳过。
        for kind in sorted(wired):
            with self.subTest(kind=kind):
                self.assertIsNone(openapi_line.unsupported.get(kind))
        # ⭐ 而且「通了的那几档」**恰好是那六档**（步骤 4 通的；将来多通一档必须同一次改这条）。
        self.assertEqual(
            set(jidanpu_member.OUTBOX_KINDS) - set(openapi_line.unsupported), wired,
        )
        #: ⭐ 那些档的字面值逐字钉住（⛔ 不从被测常量派生：自指断言改坏了不会红，E-0024）。
        self.assertEqual(sorted(wired), sorted(WIRED_KIND_LITERALS))
        self.assertEqual(
            sorted(jidanpu_member_openapi_outbox.IMPLEMENTED_KINDS), sorted(WIRED_KIND_LITERALS),
        )

    def test_neither_line_converges_anymore_and_only_the_openapi_line_declares_unlist_unavailable(self):
        """⭐⭐ **D-0249（修 E-0246）的结构判据**：两条线都**不再**收敛任何 kind，且只有②式声明「`member.unlist` 不可用」。
        历史：本用例曾钉「只有②式把 unlist 收敛到 withdraw」；那条登记已随 D-0249 摘除（收敛成退单会把**接手人自己店里的活单**退掉）。⚠️ 两条断言各自承重：`converged_kinds` 双双为 `None` ⇒ `_converged_kind` 对任何 kind 恒回它自己（引擎函数保留，机制不删——但今天没有用户）；`unlist_unavailable_reason` **只有②式非空**：①式不设（它有真撤下原语四跳），②式非空且带人话（分派拦截与对账收口都读这一格）。
        判别力：把 `unlist_unavailable_reason` 挂到①式那一份上（或两份都挂）⇒ 末段断言红；把②式这一格清空 ⇒ `UnlistUnavailableTests` 那一节当场红。"""
        up_line = jidanpu_member.member_outbox_line()
        openapi_line = jidanpu_member_openapi_outbox.member_openapi_outbox_line()

        self.assertIsNone(up_line.converged_kinds)
        self.assertIsNone(openapi_line.converged_kinds)
        # ⭐ 取数口（共享引擎真正用的那一个）：两条线对任何 kind 都恒回它自己。
        for line in (up_line, openapi_line):
            for kind in sorted(jidanpu_member.OUTBOX_KINDS):
                with self.subTest(line=line.name, kind=kind):
                    self.assertEqual(jidanpu_member._converged_kind(line, kind), kind)
        # ⭐⭐ 新格：只有②式声明「没有不毁单的撤下」，且必须带一句人话。
        self.assertIsNone(
            up_line.unlist_unavailable_reason,
            "①式有真撤下原语，声明了「不可用」= 把①式的撤下也办没了",
        )
        self.assertTrue(
            (openapi_line.unlist_unavailable_reason or "").strip(),
            "②式不声明 = unlist 会落回四跳 ⇒ E-0246 复发",
        )
        self.assertIn(UNLIST_UNAVAILABLE_REASON_LITERAL, openapi_line.unlist_unavailable_reason)

    def test_the_bridged_line_inherits_the_unlist_cell(self):
        """⭐ **派生线纪律**：桥接价档那份②式线由 `dataclasses.replace` 派生 ⇒ 新格（`unlist_unavailable_reason`）必须**自动继承**、与主份**逐字相等**。
        ⚠️ 存在的理由：replace 派生线的格子「不记得抄就静默丢」——丢了的后果是那条线上的 unlist 落回四跳 ⇒ E-0246 复发（而页面上看不出两条线的差别）。
        判别力：有人把派生改成手抄字段、漏了这一格 ⇒ 本用例红。"""
        binding_with_password = self.make_binding(account="合成账号", password="合成密码")
        plain = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        chosen = jidanpu_member_openapi_outbox.member_openapi_outbox_line_for(
            binding_with_password
        )
        self.assertIsNot(chosen, plain, "有①式凭据 ⇒ 应选中桥接线（夹具本身要先能红）")
        self.assertEqual(
            chosen.unlist_unavailable_reason, plain.unlist_unavailable_reason,
            "派生线丢了 unlist 那一格 ⇒ 那条线上的 unlist 会落回四跳（E-0246 复发）",
        )
        self.assertIn(UNLIST_UNAVAILABLE_REASON_LITERAL, chosen.unlist_unavailable_reason)

    def test_a_line_that_claims_a_kind_without_the_cell_raises_loudly(self):
        """⭐ **一致性纪律**：把某一档登记成「通了」却不给那一格 ⇒ **响亮抛**（⛔ 不是 AttributeError）。
        ⚠️ 判别力：把 `_record_of` / `_price_of` / `_withdraw_of` / `_project_of` 里那道检查删掉 ⇒ 本用例红（那时会以 `None.ensure_remote` 那种 `AttributeError` 炸在**逐事件异常边界**里，台账上只剩「未预期异常（AttributeError）」——真实成因「接线漏了一格」看不出）。
        """
        line = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        stripped = jidanpu_member.OutboxLine(
            name=line.name, spawn_client=line.spawn_client, status_push=line.status_push,
            unsupported=line.unsupported,
        )
        with self.assertRaises(ValueError):
            jidanpu_member._record_of(stripped)
        with self.assertRaises(ValueError):
            jidanpu_member._price_of(stripped)
        with self.assertRaises(ValueError):
            jidanpu_member._withdraw_of(stripped)
        with self.assertRaises(ValueError):
            jidanpu_member._project_of(stripped)

    def test_a_hall_skip_without_human_words_raises_loudly(self):
        """⭐ 配了「这一条线没有大厅那一档」却**没给人话** ⇒ 响亮抛（⛔ 不许静默少做）。
        判别力：把 `_consume_price` 里那句 `hall_not_applicable_reason` 的空值检查删掉 ⇒ 本用例红（那时台账上会出现一条**说不清为什么**的完成）。⚠️ 这条用例**只到那个分支**为止：真打上游的那一半在 `PriceTests` 那一节（那里有 `_WriteUpstream`，能真的把接单价改到位）。
        """
        base = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        mute = _muted_hall_line(base)
        self.enable()
        self.order = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.WAITING_GRAB)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID_B, in_hall=True,
        )
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PRICE, rearm=True,
        )
        #: ⛔ 用一个**假的**客户端：这一条判据要在「价写出去之后 / 判完成之前」那个点上抛，
        #: 所以只要它那个方法**够用到走进分支**即可（`PriceTests` 那一节有真读数）。
        client = _StubPriceClient()
        with self.assertRaises(ValueError):
            jidanpu_member._consume_price(
                event, binding=self.binding, client=client, http=None,
                now=timezone.now(), line=mute,
            )
        # ⚠️ 接单价**已经改了**（那一支在最前面）⇒ 抛的是「少做的那一档说不清为什么」。
        self.assertEqual(client.written, [Decimal("50.00")])

    def test_the_hall_push_gap_is_declared_on_the_openapi_line_alone(self):
        """⭐⭐ **同一件事实的两面必须同源**（②式没有「把**已经有链路**的单推进对方大厅」这个动作）。
        对账（排队）那一侧：`hall_push_unreachable_reason` 非空 ⇒ 那一档**不进差集**（判据在 `jidanpu_member.reconcile`：每 180 秒又排一条注定被终态跳过的 `member.launch` = 死循环）；消费那一侧：`throw_remote` 对「有链路 + 不在大厅」如实回 `not_available` + **同一句话**（⛔ 别两处各写一句：漂了之后台账与对账说的就不是一回事了）。⛔ **①式那一份不设这一格**：它真能抛进大厅（真接口 + 回读 `isThrowOrder == "Y"`）⇒ 那个期望在①式下**可达**，行为一个字都不许变。
        判别力（两条都能红）：把②式那一格改成**另一句话**（或空串）⇒ 本用例红；把它删回 `None`（＝旧实现）⇒ 本用例红。"""
        up_line = jidanpu_member.member_outbox_line()
        openapi_line = jidanpu_member_openapi_outbox.member_openapi_outbox_line()

        self.assertIsNone(
            up_line.hall_push_unreachable_reason,
            "①式能真把已有单抛进对方大厅 ⇒ 这一格必须是空的（⛔ 不然①式那一档会被收口掉）",
        )

        #: 消费那一侧现场：链路在、但标着**不在**对方大厅（＝「单早就在对方系统里」那一档）。
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(in_hall=False)
        outcome, reason = openapi_line.record.throw_remote(
            None, self.order, self.binding, JID,
        )

        self.assertEqual(outcome, "not_available")
        self.assertTrue(str(reason).strip(), "「没有这个动作」必须说清怎么办（⛔ 不许静默）")
        self.assertEqual(
            openapi_line.hall_push_unreachable_reason, reason,
            "对账那一侧与消费那一侧说的不是同一句话（同一件事实两处判据必然漂）",
        )

    def test_the_default_line_is_the_password_one(self):
        """⭐ 缺省 = ①式（承重）：`jidanpu_sync` 那条每 3 秒一拍的调用**一个字都不用改**。
        判别力：把 `member_outbox_line()` 改成②式那一份（或改成按 `binding.mode` 猜）⇒ 本用例红，而①式那套既有网也会跟着红。
        """
        line = jidanpu_member.member_outbox_line()
        self.assertEqual(line.name, "下家①式（账号 + 密码登录）")
        self.assertIsNone(line.enabled)
        self.assertEqual(line.blocked_reason, "")
        self.assertIs(line.spawn_client, jidanpu_member.jidanpu.member_session)
        # ①式那一份与②式那一份**不是同一个对象**（⛔ 别把两条线合成一条）。
        self.assertIsNot(line, jidanpu_member_openapi_outbox.member_openapi_outbox_line())

    def test_the_openapi_line_uses_the_official_api_client(self):
        """⭐ ②式**有自己的**取客户端方式（本步骤存在的首要理由）。
        判别力：把 `spawn_client` 换回 `jidanpu.member_session` 之外的东西 ⇒ 本用例红（而那正是 E-0202 的形态：账号密码为空的②式绑定在建连处抛 `ConfigError`）。
        """
        line = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        self.assertEqual(line.name, "下家②式（官方开放平台API）")
        self.assertIs(line.spawn_client,
                      jidanpu_member_openapi_outbox._spawn_openapi_client)
        self.assertIs(line.enabled, jidanpu_member_openapi_outbox.outbox_enabled)
        #: ⭐ 建出来的必须是**官方 API 客户端**，而且第二格（裸 HTTP）是 `None`。
        with line.spawn_client(self.binding) as (client, http):
            self.assertIsInstance(client, jidanpu_openapi.OpenApiClient)
            self.assertIsNone(http)

    def test_a_non_openapi_row_is_refused_with_its_own_human_words(self):
        """⭐ 不是②式的那一行 ⇒ 一句**自己的**人话（⛔ 与「开关没开」分开：要做的事不同）。
        ⚠️ 判据用 `is_openapi_binding`（`mode` 的**唯一**出处），⛔ 不是 `if binding.client_id:`（切回①式之后 `client_id` 还在 ⇒ 那种写法会静默走官方 API）。
        判别力：把 `mode` 那一道闸删掉 ⇒ 本用例红。"""
        self.enable()
        password_row = self.make_binding(mode="password", account="acc-x", password="pw-x")
        self.queue_status_out()

        stats = jidanpu_member_openapi_outbox.run_member_status_outbox(
            password_row, now=timezone.now(),
        )

        self.assertEqual(
            stats.blocked_reason, jidanpu_member_openapi_outbox.BLOCKED_NOT_OPENAPI_REASON,
        )
        self.assertEqual(self.upstream.requests, [])

    def test_a_disabled_binding_is_paused(self):
        """绑定被停用 ⇒ `paused=True`（**照共享引擎的同一条口径**，⛔ 不另写第二份判据）。"""
        off = self.make_binding(enabled=False)
        stats = jidanpu_member_openapi_outbox.run_member_status_outbox(
            off, now=timezone.now(),
        )
        self.assertTrue(stats.paused)
        self.assertEqual(stats.blocked_reason, "")
        self.assertEqual(self.upstream.requests, [])


# ══════════════════════════════════════════════════════════════════════════════
# ⑦ 步骤 3：`member.launch` / `member.assign`（录单）
# ══════════════════════════════════════════════════════════════════════════════


class EnsureRemoteLinkOccupiedTests(_OutboxCase):
    """⭐ E-0315 ②式同款防御钉：`ensure_remote` 尾部落链步撞「链行挂在另一张我方单上」
    ⇒ 回 `(None, LINK_OCCUPIED_REASON, "skipped", None)`，⛔ 不许带着非空远端单号返回
    （「回非空远端单号 ⇒ 链路必已存在」是该函数的承重不变量，见它的 docstring）。"""

    with_link = False  # 我方单没有链路（撞键现场：老单占着链行）

    def test_occupied_link_makes_ensure_remote_skip_without_a_remote_id(self):
        """撞键 ⇒ skipped 终态收口 + 零链路行（阳性对照：非撞键路径原样回 ok）。"""
        # 老单占链（A 族现场形态）：同绑定、同远端单号、挂在**另一张**我方单上。
        old_order = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.REFUNDED)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=old_order, jidanpu_order_id=JID, in_hall=False,
        )
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH,
            payload={"recorded": True, "jidanpu_order_id": JID, "recording_source_order_no": ORDER_NO},
        )
        client = mock.Mock()
        client.find_order_status_with_absence.return_value = (jidanpu_openapi.OrderStatus(status=LABEL_UNASSIGNED), False)
        got = jidanpu_member_openapi_outbox.ensure_remote(
            event, self.order, self.binding, client=client, now=timezone.now(), in_hall=True,
        )
        remote_id, reason, outcome, adopted = got
        self.assertIsNone(remote_id, "⛔ 不许带着非空远端单号返回（链路行没落成）")
        self.assertEqual(outcome, "skipped")
        self.assertEqual(reason, jidanpu_member.LINK_OCCUPIED_REASON)
        self.assertIsNone(adopted)
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(order=self.order).count(), 0,
            "我方单⛔ 没有链行（老单那条占用行不属它）",
        )


class _WriteCase(_OutboxCase):
    """步骤 3 的夹具：换一个**有状态的店铺**假上游 + **不预置链路**（录单那几条必须从零开始）。
    ⚠️ `with_link = False` 是**承重**的：预置了链路 ⇒ `ensure_remote` 那两条短路直接命中 ⇒ 一次上游都不打
    ⇒ 「录单真的建了单」那几条会以「什么也没发生」的形式**假绿**。"""

    upstream_factory = _WriteUpstream
    with_link = False

    def make_order(self, *, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB,
                   total_price=Decimal("100.00"), hall_price=Decimal("50.00")):
        #: ⭐ `total_price != hall_price`（100.00 vs 50.00）——「绝不泄露客户价」那条判据的载体。
        return super().make_order(
            order_no=order_no, status=status,
            total_price=total_price, hall_price=hall_price,
        )

    def queue_launch(self):
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True,
        )

    def prepare_owner_assignment(self):
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.update_or_create(
            order=self.order, emp_type=EmpType.MAIN, defaults={"user": self.binding.user},
        )

    def queue_assign(self):
        self.prepare_owner_assignment()
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN, rearm=True,
        )

    def db_link(self):
        return JidanpuMemberOrderLink.objects.filter(
            binding=self.binding, order=self.order
        ).first()

    def source_order_no(self):
        return jidanpu_member.effective_source_order_no(self.order)


class LaunchTests(_WriteCase):
    def test_a_reassigned_order_is_not_recorded_by_the_old_openapi_task(self):
        self.enable()
        self.queue_assign()
        replacement = self.make_binding()
        replacement.jidanpu_base_url = "https://other-openapi-shop.test.invalid"
        replacement.save(update_fields=["jidanpu_base_url"])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(user=replacement.user)

        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed, stats.skipped), (0, 0, 1))
        self.assertEqual(self.upstream.created, [])
        self.assertIsNone(self.db_link())


    def test_a_launch_really_creates_the_order_and_lands_the_link(self):
        """⭐⭐ 判据七（端到端录单）：开开关跑一轮「派单录单」⇒ **对方真的建出单 + 链路落成 + `done`**。
        ⚠️ 绑定是**界面真建出来那个形态**（账号密码都是空的；`LaunchFixtureTests` 另有一条自证）：拿①式的登录连接去打它会在建连处抛 `ConfigError` ⇒ 本用例红。⭐⭐ **本用例跑在「真形状」的假上游上**：状态回包**只有真机那 5 格**（没有 `orderId` / `price` / `recFee`，见 `_status_data`）⇒ 远端单号**只能**来自建单回包的 `data`（下面那条链路断言钉的就是它）。
        判别力（每一条都能红）：① 把 `create_order` 的调用删掉（或让它回 `code=0`）⇒ 链路落不下来、事件不 `done`；② 把 `recUser` 那个开关取反（launch 走 `0`）⇒ 下面那条 `REC_USER_TO_HALL` 断言红；③ ⭐ 把「远端单号取自建单 `data`」改成「回读取号」⇒ 链路那一列的远端单号是**空的** ⇒ 红（之前那版就是这个形态：真机上**每一次录单都失败**）。"""
        self.enable()
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        # ⭐ **对方真的建出了一张**（读数取自假上游自己的现场，⛔ 不是「调用过就算」）：
        self.assertEqual(len(self.upstream.created), 1)
        body = self.upstream.created[0]
        # ⭐ 参数逐格钉（键名逐字照文档；⛔ 拼错一个就是一次静默失败）：
        self.assertEqual(body["gameName"], self.game.name)
        self.assertEqual(body["project"], self.order.project_text)
        self.assertEqual(body["souresOrderID"], self.source_order_no())
        self.assertEqual(body["orderSoures"], SOURCE_NAME)
        self.assertEqual(body["recUser"], jidanpu_member_openapi_outbox.REC_USER_TO_HALL)
        # ⭐ **`gameArea` 取订单上的大区**（业务方裁定「走 a」）：本夹具那份订单
        #    **没有区服** ⇒ 必须回落常量。⚠️ 钉**字面量**（⛔ 不写 `GAME_AREA_DEFAULT`：
        #    自指断言在那个常量被改坏时**不会红**，`AGENTS.md` §5-G / E-0024 一族）。
        self.assertEqual(body["gameArea"], "官方服")
        # ⭐ **我们自己的表里没有对应列的那两格**：值取自**录单页模板**（`ADD_ORDER_TEMPLATE`
        #    —— 那串值是从真机录单页捕获的生成物，而**①式那条路发的也是它**）
        #    ⇒ 逐字钉住，⛔ 不许有人凭记忆另猜一个（如把 `hours` 硬编码 8 之外的值）。
        self.assertEqual(body["hours"], 8)
        self.assertEqual(body["customerId"], "0")
        # ⭐ **两个价字段都是发单价**（见 `NeverLeakCustomerPriceTests` 那条红线用例）。
        self.assertEqual(Decimal(str(body["price"])), self.order.hall_price)
        self.assertEqual(Decimal(str(body["assignPrice"])), self.order.hall_price)
        # ⭐ **链路真的落成了**（远端单号取自对方的 `data`；`in_hall=True` 是这一档的目标态）：
        link = self.db_link()
        self.assertIsNotNone(link)
        self.assertEqual(link.jidanpu_order_id, CLAIMED_ID)
        self.assertTrue(link.in_hall)
        self.assertIsNotNone(link.listed_at)
        self.assertIsNotNone(link.last_out_at)
        # ⭐ 台账 `done` + 远端单号进了 payload（断点续跑要用它）：
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.payload["jidanpu_order_id"], CLAIMED_ID)
        # 首次录单靠来源号挡重，并分别回读来源号与收到的系统订单号。
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("GET", ORDER_STATUS_PATH),      # 挡重预读（按来源单号：查不到才录）
            ("POST", CREATE_ORDER_PATH),     # 录单（远端单号就在它的 data 里）
            ("GET", ORDER_STATUS_PATH),      # 按来源单号确认
            ("GET", ORDER_STATUS_PATH),      # 按回包系统单号确认
        ])

    def test_the_game_area_is_the_orders_server_when_it_has_one(self):
        """⭐ 判据：订单**有大区** ⇒ 请求体里的 `gameArea` **就是那个区服名**（不回落常量）。
        ⚠️ 值来源是**订单上的大区字段**（`Order.server`，我方那个可空的区服 FK）——业务方裁定「走 a」的第一半。
        判别力：把 `_game_area` 写成恒回常量（不看订单）⇒ 本用例红；把它写成取 `order.game.name`（游戏名而不是区服名）⇒ 本用例红。"""
        self.enable()
        self.order.server = GameServer.objects.create(game=self.game, name="合成区服名乙")
        self.order.save(update_fields=["server"])
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        self.assertEqual(len(self.upstream.created), 1)
        #: ⚠️ 钉**字面量**（⛔ 不写常量 / 不写 `self.order.server.name`：前者是自指断言，
        #:    后者在「夹具没设区服」时也会绿 —— `AGENTS.md` §5-G / E-0024 一族）。
        self.assertEqual(self.upstream.created[0]["gameArea"], "合成区服名乙")

    def test_the_game_area_is_never_empty(self):
        """⭐ 判据：**发出去的请求体里** `gameArea` **永不为空串 / `None`**（真机那条就是发了空串）。
        ⭐ 这是**专门钉「空不许出现」**的那一条网（另外两条各自钉「等于什么」）：逐档走三种订单形状，**真的各录一张单**，每一档都断言这一格非空、且没有首尾空白。⚠️ 读数取自**假上游收到的请求体**（`self.upstream.created`，⛔ 不是 `_create_body` 的返回值：那是 snake_case 的中间层，钉它 = 钉的不是「发出去的那一格」）。判别力（每一档都能红）：① 把 `_game_area` 改回模板那一格（`ADD_ORDER_TEMPLATE["gameArea"] == ""`）⇒ 三档全红；② 只把回落常量那一支改回空串 ⇒ 「没有区服」「区服名是空白」两档红；③ 把「有区服」那一支的 `strip()` 去掉 ⇒ 「区服名是空白」那一档发出去的就是那串空白。
        三种形状分别录成不同的远端单。"""
        cases = (
            ("订单没有区服", None, "20261009000000000911"),
            ("区服名是空白", "   ", "20261009000000000912"),
            ("区服名正常", "合成区服名乙", "20261009000000000913"),
        )
        self.enable()
        for label, name, order_no in cases:
            with self.subTest(label=label):
                self.upstream.create_remote_id = f"synthetic-copy-{order_no}"
                binding = self.make_binding()
                order = self.make_order(order_no=order_no)
                if name is not None:
                    order.server = GameServer.objects.create(game=self.game, name=name)
                    order.save(update_fields=["server"])
                jidanpu_member.queue_member_event(
                    order, binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True,
                )
                stats = jidanpu_member_openapi_outbox.run_member_status_outbox(
                    binding, now=timezone.now(),
                )
                self.assertEqual(
                    stats.done, 1, "这一档压根没录单 ⇒ 下面的读数不是「录单那一格」的",
                )
                area = self.upstream.created[-1]["gameArea"]
                self.assertNotIn(
                    area, ("", None),
                    "gameArea 发了空值 —— 对方那一格必填 ⇒ 这一单永远录不过去",
                )
                self.assertEqual(area, area.strip(), "gameArea 带着首尾空白发出去了")

    def test_a_second_round_does_not_create_a_second_order(self):
        """⭐ 判据：**幂等** —— 同一条待发动作重放 ⇒ 不建第二张单、一次录单接口都不打。
        判别力：把「`payload` 里的远端单号 / 本地链路」那两条短路删掉 ⇒ 本用例红（第二轮会再录一次——真实世界里那会被对方按来源单号拒掉，或者真灌出第二张单）。
        """
        self.enable()
        self.queue_launch()
        self.run_a_round()
        first_round_calls = len(self.upstream.requests)

        #: 再排一条同 kind 的待发动作（在飞去重会复用**同一条**行 ⇒ 这里显式再来一条）。
        self.queue_launch()
        second = self.run_a_round()

        self.assertEqual(second.done, 1)
        self.assertEqual(len(self.upstream.created), 1, "第二轮又录了一张单")
        self.assertEqual(
            [pair for pair in self.upstream.all_pairs()[first_round_calls:]
             if pair[1] == CREATE_ORDER_PATH],
            [], "第二轮又打了一次录单接口",
        )
        self.assertEqual(JidanpuMemberOrderLink.objects.filter(order=self.order).count(), 1)

    def test_an_assign_does_not_ask_for_the_hall_and_the_launch_does(self):
        """⭐ 判据：`member.assign` **不抛大厅**（照①式那一档的目标态逐条搬）。
        ⚠️ ②式里「进不进大厅」的开关就是 `recUser` 那一格（文档：传 `999999999` 才自动进内部抢单大厅）⇒ 指派那一档必须是 `"0"`、链路 `in_hall=False`。⛔ 这一条与 `LaunchTests` 里的 `REC_USER_TO_HALL` 断言**成对**：只有一条时「两档都传同一个值」也能绿。
        """
        self.enable()
        self.queue_assign()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        body = self.upstream.created[0]
        self.assertEqual(body["recUser"], jidanpu_member_openapi_outbox.REC_USER_NOT_TO_HALL)
        link = self.db_link()
        self.assertFalse(link.in_hall)
        self.assertIsNone(link.listed_at)
        #: ⛔ 一次「抛单」类的调用都没有（②式里那一跳会如实回「没有这个动作」，
        #:   而指派这一档**根本不该走到它**）。
        self.assertEqual(
            [pair for pair in self.upstream.write_pairs() if pair[1] != CREATE_ORDER_PATH], [],
        )

    def test_a_launch_on_an_already_recorded_order_says_it_cannot_do_that(self):
        """⭐ 判据：②式**没有**「把已经录进去的单抛进对方大厅」这个动作 ⇒ 终态跳过 + 人话。
        ⚠️ 真机形状：这张单早先由「指派录单」录进去（`recUser=0`，不在对方大厅），现在用户点了「抛单」⇒ ②式**变不出**这个动作（`recUser` / `assignPrice` 只在录单那一刻可传）。⛔ 不许静默少做、也⛔ 不许谎称 `in_hall=True`：链路保持「不在大厅」+ 一句说清怎么办的人话。
        判别力：把 `throw_remote` 改成恒回 `OK` ⇒ 本用例红（那时订单会被标成「在对方大厅」，而对方那边压根没这回事——一句假话）。"""
        self.enable()
        #: 先按「指派录单」录进去（链路 `in_hall=False`），再排一条「派单录单」。
        self.queue_assign()
        self.assertEqual(self.run_a_round().done, 1)
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["status"])
        Performance.objects.filter(order=self.order, emp_type=EmpType.MAIN).update(user=None)
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(
            event.reason, jidanpu_member_openapi_outbox.THROW_NOT_AVAILABLE_REASON,
        )
        link = self.db_link()
        self.assertFalse(link.in_hall, "②式没有那个接口，⛔ 不许把链路标成「在对方大厅」")
        # ⛔ 也没有第二次录单（那一档短路在「本地链路」上）。
        self.assertEqual(len(self.upstream.created), 1)


class LaunchFixtureTests(_WriteCase):
    def test_the_binding_is_the_shape_the_ui_really_creates(self):
        """⭐ 夹具自证：「和界面真建出来一模一样」不是嘴上说的——账号密码**都是空的**。
        ⚠️ 这条是**夹具的判据**（E-0202 的教训：夹具为了「能过闸」凑出的形态会绕开真实形态）。
        判别力：给夹具塞一个假密码 ⇒ 本用例红（而那正是上一轮被骗过去的形状）。"""
        self.assertEqual(self.binding.account, "")
        self.assertEqual(self.binding.password, "")
        self.assertEqual(self.binding.mode, "openapi")
        self.assertEqual(self.binding.source_name, SOURCE_NAME)


class NeverLeakCustomerPriceTests(_WriteCase):
    def test_the_record_body_carries_the_out_price_and_never_the_customer_price(self):
        """⭐⭐ **红线（D-0162）**：录单请求体里**只许有发单价**，⛔ 一个字节的总价都不许有。
        ⚠️ 夹具刻意让两个价**取不同值**（`total_price=100.00` / `hall_price=50.00`）：同值时「发错了格」也看不出来（那正是这条判据要抓的形态）。⚠️ 判据打在**真的发出去的请求体**上（⛔ 不打在常量、也不打在返回值上）。判别力（每一格的坏法都能红）：① 拿 `price` 去填 `total_price` ⇒ 「只有发单价」那几条红；② 把总价塞进 `remark`（或任何别的格）⇒ 整包扫描那条红；③ 两个价字段只填一个 ⇒ 「都等于发单价」那一条红（②式两格都必填）。
        """
        #: 发单价与客户价**刻意不同**（判据的立身之本）。
        self.order = self.make_order(
            order_no=ORDER_NO_B, total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"), status=OrderStatus.WAITING_GRAB,
        )
        self.enable()
        self.queue_launch()

        self.run_a_round()

        body = self.upstream.created[0]
        sent = {key: Decimal(str(value)) for key, value in body.items()
                if key in ("price", "assignPrice")}
        # ⭐ 两个价字段**都在**、**都等于发单价**：
        self.assertEqual(sorted(sent), ["assignPrice", "price"])
        for key, value in sent.items():
            with self.subTest(key=key):
                self.assertEqual(value, Decimal("50.00"))
        # ⭐ **整包**里不许出现总价那个值（含任何字符串格）：
        packed = repr(body)
        self.assertNotIn("100.00", packed)
        self.assertNotIn("100.0", packed)
        # ⭐ 反向对照：发单价**确实**发出去了（否则「没有总价」可以靠「什么都不发」骗过）。
        self.assertIn("50.0", packed)

    def test_the_price_push_writes_the_out_price_and_never_the_customer_price(self):
        """⭐ 同一个红线的**改价那一半**：②式的改价请求体里也只有发单价。
        判别力：把 `write_price` 改成发 `order.total_price` ⇒ 本用例红。
        """
        self.enable()
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID, in_hall=False,
        )
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PRICE, rearm=True,
        )

        self.run_a_round()

        body = self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH)[0]
        self.assertEqual(Decimal(str(body["price"])), Decimal("50.00"))
        self.assertNotIn("100.00", repr(body))


# ══════════════════════════════════════════════════════════════════════════════
# ⑧ 步骤 3：录单后的**回读**（只剩「确认查得到」一个用途）
# ══════════════════════════════════════════════════════════════════════════════

# ⭐⭐ 回读**不取号、不比号**——`GetOrderStatus` 的回包实测**只有 5 格**（没有 `orderId`）⇒ 远端单号**只**来自建单回包的 `data`。
# ⚠️ 上一轮那两条用例（「回读单独供号」与「回读回的号是另一张 ⇒ 失败」）**已删除**：它们建在假上游多回的 `orderId` 上，真机上**永远走不到**，留着就是"测试骗过自己"。


class LaunchReadbackTests(_WriteCase):
    def _same_store_takeover_after_source_edit(self, *, create_reply):
        self.enable()
        original_binding = self.binding
        old_source = f"合成旧来源-{original_binding.pk}"
        self.order.source_order_no = old_source
        self.order.save(update_fields=["source_order_no"])
        self.upstream.index_source = False
        self.upstream.create_data = create_reply
        first = self.queue_launch()
        self.assertEqual(self.run_a_round().failed, 1)
        first.refresh_from_db()
        self.assertEqual(first.payload["recording_source_order_no"], old_source)
        self.assertTrue(first.payload["recorded"])
        self.assertIsNone(self.db_link())

        editor = make_logged_in_client(
            username=f"overwrite-source-{original_binding.pk}", groups=("客服",),
            nodes=("order.add_edit", "order.view_detail", "order.list"),
        )
        new_source = f"合成新来源-{original_binding.pk}"
        edited = editor.patch(
            f"/api/business/orders/{self.order.order_no}/",
            {"source_order_no": new_source}, content_type="application/json",
        )
        self.assertEqual(edited.json()["code"], 0, edited.json())
        self.order.refresh_from_db()
        self.assertEqual(self.order.source_order_no, new_source)
        original_binding.enabled = False
        original_binding.save(update_fields=["enabled"])
        self.binding = self.make_binding()
        self.upstream.index_source = True
        self.upstream.by_source[old_source] = CLAIMED_ID
        self.upstream.create_data = None
        now = timezone.now() + timedelta(minutes=15)
        self.assertEqual(jidanpu_member.reconcile_and_queue(self.binding, now=now).launch, 1)
        seen = len(self.upstream.created)

        stats = self.run_a_round(now=now)

        self.assertEqual(len(self.upstream.created), seen)
        self.assertNotIn(new_source, self.upstream.by_source)
        self.assertEqual(stats.done, 0)
        self.assertIsNone(self.db_link())
        first.refresh_from_db()
        self.assertEqual(first.payload["recording_source_order_no"], old_source)

    def test_a_same_store_account_cannot_rerecord_after_missing_number_and_source_edit(self):
        self._same_store_takeover_after_source_edit(create_reply="")

    def test_a_same_store_account_cannot_rerecord_with_unconfirmed_cached_number(self):
        self._same_store_takeover_after_source_edit(create_reply=CLAIMED_ID)

    def test_an_unconfirmed_cached_number_does_not_finish_on_retry(self):
        self.enable()
        self.upstream.index_source = False
        event = self.queue_launch()
        self.assertEqual(self.run_a_round().failed, 1)
        self.assertIsNone(self.db_link())
        event.refresh_from_db()
        self.assertEqual(event.payload["jidanpu_order_id"], CLAIMED_ID)
        self.upstream.rows.pop(CLAIMED_ID)

        retry = self.run_a_round(now=timezone.now() + timedelta(minutes=15))

        self.assertEqual(retry.failed, 1)
        self.assertIsNone(self.db_link())
        self.assertEqual(len(self.upstream.created), 1)

    def test_source_readback_cannot_confirm_a_returned_number_that_does_not_exist(self):
        self.enable()
        self.upstream.create_data = "synthetic-nonexistent-id"
        self.queue_launch()

        result = self.run_a_round()

        self.assertEqual(result.failed, 1)
        self.assertIsNone(self.db_link())
        self.assertIn(CLAIMED_ID, self.upstream.rows)
        self.assertEqual(len(self.upstream.created), 1)

    def test_different_creation_times_rule_out_an_existing_but_unrelated_number(self):
        self.enable()
        wrong_id = "synthetic-unrelated-copy"
        self.upstream.create_data = wrong_id
        self.upstream.seed(source_id="unrelated-source", order_id=wrong_id, status=LABEL_UNASSIGNED)
        self.queue_launch()
        original = self.upstream.handler

        def different_creation_time(request):
            response = original(request)
            if request.url.params.get("orderId") == wrong_id:
                envelope = response.json()
                envelope["data"]["creatTime"] = "2026-10-09 12:01:00"
                return httpx.Response(200, json=envelope)
            return response

        with mock.patch.object(self.upstream, "handler", different_creation_time):
            self.assertEqual(self.run_a_round().failed, 1)

        self.assertIsNone(self.db_link())
        self.assertIn("创建时间不同", self.db_out_events(kind=jidanpu_member.KIND_LAUNCH).get().reason)

    def test_a_legacy_receipt_without_original_source_needs_a_creation_time_match(self):
        self.enable()
        event = self.queue_launch()
        event.payload.update(recorded=True, jidanpu_order_id=CLAIMED_ID)
        event.save(update_fields=["payload"])
        self.upstream.seed(source_id=self.source_order_no(), order_id=CLAIMED_ID, status=LABEL_UNASSIGNED)
        original = self.upstream.handler

        def invalid_creation_time(request):
            response = original(request)
            if request.url.path == ORDER_STATUS_PATH:
                envelope = response.json()
                envelope["data"]["creatTime"] = 1791508800
                return httpx.Response(200, json=envelope)
            return response

        with mock.patch.object(self.upstream, "handler", invalid_creation_time):
            self.assertEqual(self.run_a_round().failed, 1)
        self.assertIsNone(self.db_link())
        event.refresh_from_db()
        self.assertIn("缺少原始来源单号", event.reason)

        self.assertEqual(self.run_a_round(now=timezone.now() + timedelta(minutes=15)).done, 1)
        self.assertEqual(self.db_link().jidanpu_order_id, CLAIMED_ID)
        self.assertEqual(self.upstream.created, [])

    def _change_openapi_account(self, *, store):
        editor = Client(SERVER_NAME="tenant.test.com")
        login = editor.post(
            "/api/auth/login", {"username": self.binding.user.username, "password": P2_TEST_PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])
        with mock.patch.object(jidanpu_openapi, "ensure_callback_key"):
            response = editor.put(
                "/api/business/member/jidanpu-binding/",
                {"mode": "openapi", "jidanpu_base_url": store, "account": "",
                 "client_id": "local-new-client", "client_secret": "synthetic-key", "enabled": False},
                content_type="application/json",
            )
        self.assertEqual(response.json()["code"], 0, response.json())
        self.binding.refresh_from_db()
        self.binding.enabled = True
        self.binding.save(update_fields=["enabled"])

    def _replace_source_and_binding(self, *, store):
        self.order.source_order_no = "synthetic-new-rebind-source"
        self.order.save(update_fields=["source_order_no"])
        self._change_openapi_account(store=store)

    def test_same_store_rebind_preserves_unconfirmed_legacy_progress(self):
        self.enable()
        self.upstream.index_source = False
        first = self.queue_launch()
        self.assertEqual(self.run_a_round().failed, 1)
        first.refresh_from_db()
        original_source = first.payload["recording_source_order_no"]
        first.payload.pop("recording_store")
        first.save(update_fields=["payload"])
        self._replace_source_and_binding(store=self.binding.jidanpu_base_url)
        self.upstream.by_source[original_source] = CLAIMED_ID
        self.upstream.index_source = True
        self.queue_launch()

        stats = self.run_a_round(now=timezone.now() + timedelta(minutes=15))

        self.assertEqual(len(self.upstream.created), 1)
        self.assertEqual(stats.done, 0)
        self.assertIsNone(self.db_link())
        first.refresh_from_db()
        self.assertEqual(first.status, "skipped")
        self.assertEqual(first.payload["recording_store"], self.binding.jidanpu_base_url)
        self.assertEqual(first.payload["recording_source_order_no"], original_source)

    def _check_another_account_after_an_old_rebind_lost_the_store(self, progress):
        self.enable()
        original_binding = self.binding
        original = self.queue_launch()
        JidanpuSyncEvent.objects.filter(pk=original.pk).update(
            status="skipped", reason=jidanpu_member.REBINDED_EVENT_REASON,
            payload={"member_binding_id": original_binding.pk, **progress},
        )
        original_binding.enabled = False
        original_binding.save(update_fields=["enabled"])
        self.binding = self.make_binding()
        self.order.source_order_no = "synthetic-new-source-after-legacy-rebind"
        self.order.save(update_fields=["source_order_no"])
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        self.assertIsNone(self.db_link())
        self.assertEqual(self.upstream.created, [])

    def test_another_account_cannot_rerecord_after_an_old_rebind_lost_the_store(self):
        self._check_another_account_after_an_old_rebind_lost_the_store({"jidanpu_order_id": CLAIMED_ID})

    def test_another_account_cannot_rerecord_if_old_rebind_preserves_only_source(self):
        self._check_another_account_after_an_old_rebind_lost_the_store(
            {"recording_source_order_no": "synthetic-source-at-recording"},
        )

    def test_rebinding_to_a_different_store_can_record(self):
        self.enable()
        self.upstream.index_source = False
        self.queue_launch()
        self.assertEqual(self.run_a_round().failed, 1)
        self._replace_source_and_binding(store="https://synthetic-different-store.test.invalid")
        self.upstream.index_source = True
        self.upstream.create_remote_id = "synthetic-other-store-copy"
        self.queue_launch()

        stats = self.run_a_round(now=timezone.now() + timedelta(minutes=15))

        self.assertEqual(stats.done, 1)
        self.assertEqual(self.db_link().jidanpu_order_id, "synthetic-other-store-copy")
        self.assertEqual(len(self.upstream.created), 2)

    def test_a_definite_create_rejection_does_not_block_same_store_rebind(self):
        self.enable()
        self.upstream.reply(
            CREATE_ORDER_PATH, payload={"code": 0, "msg": "没有权限", "data": None},
        )
        first = self.queue_launch()
        self.assertEqual(self.run_a_round().failed, 1)
        first.refresh_from_db()
        self.assertNotIn("recording_source_order_no", first.payload)
        self._replace_source_and_binding(store=self.binding.jidanpu_base_url)
        self.upstream.routes.pop(CREATE_ORDER_PATH)
        self.queue_launch()

        stats = self.run_a_round(now=timezone.now() + timedelta(minutes=15))

        self.assertEqual(stats.done, 1)
        self.assertEqual(len(self.upstream.created), 1)
        self.assertIsNotNone(self.db_link())

    def test_launch_takeover_cannot_claim_an_assign_record_was_sent_to_hall(self):
        self.enable()
        self.upstream.index_source = False
        first = self.queue_assign()
        self.assertEqual(self.run_a_round().failed, 1)
        JidanpuSyncEvent.objects.filter(pk=first.pk).update(attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS)
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["status"])
        next_event = self.queue_launch()
        self.upstream.by_source[self.source_order_no()] = CLAIMED_ID

        self.run_a_round(now=timezone.now() + timedelta(minutes=15))

        next_event.refresh_from_db()
        self.assertEqual(next_event.status, "skipped")
        self.assertEqual(self.db_link().jidanpu_order_id, CLAIMED_ID)
        self.assertFalse(self.db_link().in_hall)
        self.assertEqual(len(self.upstream.created), 1)

    def _check_edits_before_link(self, *, edit_during_create, assign=False, exhaust=False):
        self.enable()
        editor = make_logged_in_client(
            username="in-flight-editor", groups=("客服",),
            nodes=("order.set_offer_price", "order.add_edit", "order.view_detail", "order.list"),
        )
        url = f"/api/business/orders/{self.order.order_no}/"
        project = "录单过程中修改的合成项目"
        remark = "录单过程中修改的合成备注"
        latest_price = "73.00"
        original = self.upstream.handler

        def edit_order():
            self.assertIsNone(self.db_link())
            price = editor.post(url + "hall-price/", {"price": latest_price}, content_type="application/json")
            details = editor.patch(url, {"project_text": project, "remark": remark}, content_type="application/json")
            self.assertEqual(price.json()["code"], 0, price.json())
            self.assertEqual(details.json()["code"], 0, details.json())

        def handle_edit_mid_request(request):
            response = original(request)
            if edit_during_create and request.method == "POST" and request.url.path == CREATE_ORDER_PATH:
                edit_order()
            return response

        self.upstream.index_source = False
        queue_initial_record = self.queue_assign if assign else self.queue_launch
        recording = queue_initial_record()
        with mock.patch.object(self.upstream, "handler", handle_edit_mid_request):
            self.assertEqual(self.run_a_round().failed, 1)
            if exhaust:
                JidanpuSyncEvent.objects.filter(pk=recording.pk).update(attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS)
            if not edit_during_create:
                edit_order()
            self.run_a_round()
            self.assertEqual(self.upstream.write_pairs(), [("POST", CREATE_ORDER_PATH)])
            self.upstream.by_source[self.source_order_no()] = CLAIMED_ID
            if exhaust:
                queue_initial_record()
            recovery = timezone.now() + timedelta(minutes=15)
            self.run_a_round(now=recovery)
            self.run_a_round(now=recovery + timedelta(minutes=15))

        self.assertEqual(self.db_link().jidanpu_order_id, CLAIMED_ID)
        self.assertEqual(len(self.upstream.created), 1)
        row = self.upstream.rows[CLAIMED_ID]
        self.assertEqual((row["rec_fee"], row["project"]), (Decimal(latest_price), project))
        modify = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        self.assertEqual(modify["remark"], remark)
        self.assertEqual(modify["orderRemark"], remark)

    def test_edits_during_initial_create_wait_for_its_link(self):
        self._check_edits_before_link(edit_during_create=True)

    def test_edits_during_initial_readback_backoff_wait_for_its_link(self):
        self._check_edits_before_link(edit_during_create=False)

    def test_edits_during_an_assign_record_are_kept_even_if_it_needs_manual_retry(self):
        self._check_edits_before_link(edit_during_create=False, assign=True, exhaust=True)

    def test_parameter_error_in_launch_lookup_does_not_allow_creating(self):
        self.enable()
        self.queue_launch()
        original = self.upstream.handler

        def reject_lookup(request):
            response = original(request)
            if request.method == "GET" and request.url.path == ORDER_STATUS_PATH:
                return httpx.Response(200, json={
                    "code": 0, "msg": "系统单号和来源单号不可同时为空", "data": "",
                })
            return response

        with mock.patch.object(self.upstream, "handler", reject_lookup):
            self.assertEqual(self.run_a_round().failed, 1)

        self.assertEqual(self.upstream.created, [])
        self.assertIsNone(self.db_link())
        event = self.db_out_events(kind=jidanpu_member.KIND_LAUNCH).get()
        self.assertEqual(event.status, "failed")
        self.assertFalse(event.payload.get("recorded"))
        self.assertEqual(self.run_a_round(now=timezone.now() + timedelta(minutes=15)).done, 1)
        self.assertEqual(len(self.upstream.created), 1)

    def test_the_fake_upstream_only_ever_replies_with_the_five_real_keys(self):
        """⭐⭐ **「回包里恰好 5 格」那条网**（本文件这一侧）：假上游的状态回包只许有那 5 格。
        ⚠️ 这条网存在的理由（真机实测）：上一轮的假上游多回了 `orderId` / `price` / `recFee` 三格 ⇒ 「录单后回读取号」与「改价回读钉值」两条判据在真机上**恒假**（录单永远失败 / 改价永远未确认），而**用例全绿**。⇒ 现在三处**同时**钉成字面量：`_status_data` 自己断言 / 本用例数一遍 / 客户端那一侧由 `tests/test_jidanpu_openapi.py` 的字段集合网守。
        判别力：往 `_status_data` 里加一格（如 `"orderId": …`）⇒ 本用例与 `_status_data` 自己的断言**都会红**（⛔ 不是"看起来更真"地骗过后面一串判据）。"""
        self.assertEqual(len(ORDER_STATUS_REAL_KEYS), 5)
        self.assertEqual(set(_status_data(status=LABEL_UNASSIGNED)), ORDER_STATUS_REAL_KEYS)
        # ⭐ 正向对照：真的打得动（否则「只有 5 格」可以靠"压根不查"骗过）。
        #    ⚠️ 零网络由 `_OutboxCase.setUp` 那道 `httpx.Client` 桩保证（⛔ 不在这里再挂一次）。
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
        )
        with jidanpu_openapi.openapi_client(self.binding) as client:
            status = client.get_order_status(order_id=JID)
        self.assertEqual(status.status, LABEL_UNASSIGNED)
        # ⭐⭐ **回读里读不到系统订单号**（那一格在真机上不存在 ⇒ 谁想"回读取号"先看这里）。
        self.assertFalse(
            [name for name in ORDER_STATUS_REAL_KEYS if "order" in name.lower()],
            "状态回包里出现了带 order 的键 ⇒ 真机读数变了，改准这条网再改实现",
        )

    def test_a_create_whose_readback_finds_nothing_is_a_failure(self):
        """⭐ 判据十（回读）：录单**回了成功**，但按来源单号**查不到** ⇒ 判**失败**、⛔ 不落链路。
        ⚠️ 真机教训（①式那段注释）：**「录单成功但链路没落成」**是最贵的形态——那张单在对方那边存在、在我方永远停在待抢单 ⇒ 判据**不许**只看录单那一发的返回。
        判别力：把「录单后回读」那一跳删掉（拿 `data` 里那串直接当成功）⇒ 本用例红。"""
        self.enable()
        #: 建出来的单**不按来源单号索引** ⇒ 回读查不到它（真机形态：刚录完那次读没命中）。
        self.upstream.index_source = False
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        self.assertEqual(stats.done, 0)
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("录单后", event.reason)
        # ⭐ **链路一行都没有**（没读到号就不许落链路 —— 那正是「回读为准」的意思）：
        self.assertIsNone(self.db_link())
        # ⭐ 但「已经录过」这个事实**保住了**（`recorded` 已落库）⇒ 下一轮**绝不重录**。
        self.assertTrue(event.payload["recorded"])

    def test_the_retry_after_a_readback_failure_never_records_again(self):
        """⭐ 判据十（续）：回读失败之后重试 ⇒ **一次录单接口都不打**（重录会灌第二张真单）。
        判别力：把 `payload["recorded"]` 那一格（录单成功先落库）删掉 ⇒ 本用例红（下一轮会再录一次）。
        """
        self.enable()
        self.upstream.index_source = False
        self.queue_launch()
        self.run_a_round()
        self.assertEqual(len(self.upstream.created), 1)
        first_round_calls = len(self.upstream.requests)

        #: 回读那一侧恢复了（对方那边其实有这张单）⇒ 重试应当只**回读**、把链路补上。
        self.upstream.by_source[self.source_order_no()] = CLAIMED_ID
        second = self.run_a_round(now=timezone.now() + timedelta(seconds=180))

        self.assertEqual(second.done, 1)
        self.assertEqual(len(self.upstream.created), 1, "重试又录了一张单")
        self.assertEqual(
            [pair for pair in self.upstream.all_pairs()[first_round_calls:]
             if pair[1] == CREATE_ORDER_PATH],
            [], "重试又打了一次录单接口",
        )
        checks = [
            req["params"] for req in self.upstream.requests[first_round_calls:]
            if req["path"] == ORDER_STATUS_PATH
        ]
        self.assertEqual(checks, [{"sourceId": self.source_order_no()}, {"orderId": CLAIMED_ID}])
        self.assertEqual(self.db_link().jidanpu_order_id, CLAIMED_ID)

    def test_a_create_without_an_order_id_is_a_failure(self):
        """⭐ 判据十（空那一档）：录单回**成功**但 `data` 里**没有系统订单号** ⇒ 判**失败**。
        ⚠️ 没有号 ⇒ 链路落不下去 ⇒ 那一单在我方永远停在待抢单（而重录会被来源单号拒掉）。⛔ 不许把「回了个成功」当成「拿得到号」——⭐⭐ 尤其因为**回读里也没有号**（真机回包只有那 5 格）⇒ 这一档是**真的**拿不到号，不是"再读一次就有了"。⭐ 本用例**同时**守住「重试绝不重录」那条判据的**另一半**：这一档的 payload 里**没有**远端单号（`data` 是空的、回读也没给）⇒ 重试时唯一能挡住第二次录单的就是 `payload["recorded"]` 那一格。
        判别力：把 `_remote_order_ids` / `RECORD_NO_REMOTE_ID_REASON` 那道判据删掉（空 `data` 也照落链路）⇒ 本用例红。"""
        self.enable()
        #: 录单回的 `data` 是空串（真机可能的形态），而回读那边**也拿不到号**（回包里没有那一格）。
        self.upstream.create_data = ""
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        self.assertIsNone(self.db_link())
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("系统订单号", event.reason)
        self.assertTrue(event.payload["recorded"])
        self.assertNotIn("jidanpu_order_id", event.payload)
        # ⭐ 这一档的重试**不会**重录，靠的是**挡重预读**（对方那边查得到这张单、只是没给号）
        #    ⇒ 另一条更硬的用例见 `test_a_retry_without_any_remote_id_still_never_records_again`。
        self.assertEqual(len(self.upstream.created), 1)

    def test_a_retry_without_any_remote_id_still_never_records_again(self):
        """⭐ 判据（幂等）**最硬的那一半**：payload 里连远端单号都没有时，重试也绝不重录。
        ⚠️ 这一档存在的理由（实测）：另一条重试用例靠的是「payload 里已有远端单号」那条短路 ⇒ 把 `payload["recorded"]` 那一支删掉，**在那条用例下不会红**（本题库里那条变异一开始就是 `SURVIVED`）。本档把两条旁路都掐掉：① 建单**不按来源单号索引**（对方那边读不到这张单 ⇒ 重试时挡重预读也查不到）；② 回读也不给号（真机回包里压根没有那一格）⇒ 唯一能挡住第二次录单的就是 `recorded`。
        判别力：把 `_ensure_remote_id` 里 `payload["recorded"]` 那一支删掉 ⇒ 本用例红。"""
        self.enable()
        self.upstream.create_data = ""
        self.upstream.index_source = False
        self.queue_launch()

        first = self.run_a_round()

        self.assertEqual(first.failed, 1)
        self.assertIsNone(self.db_link())
        event = self.db_events(kind="member.launch").get()
        self.assertTrue(event.payload["recorded"])
        self.assertNotIn("jidanpu_order_id", event.payload)
        self.assertEqual(len(self.upstream.created), 1)

        first_calls = len(self.upstream.requests)
        second = self.run_a_round(now=timezone.now() + timedelta(seconds=180))

        self.assertEqual(second.failed, 1)
        self.assertEqual(len(self.upstream.created), 1, "重试又录了一张单")
        self.assertEqual(
            [pair for pair in self.upstream.all_pairs()[first_calls:]
             if pair[1] == CREATE_ORDER_PATH],
            [], "重试又打了一次录单接口",
        )

    def test_a_split_create_takes_the_first_id_and_leaves_a_human_line(self):
        """⭐ 判据（**拆单**）：`data` 是逗号分隔的多个系统订单号 ⇒ **取第一段**落链路 + **留痕**。
        ⚠️ 口径（`SPLIT_ORDER_REASON` 的注释里有完整理由）：**取第一段**（⛔ 不是响亮失败），因为链路表只存得下一个号、而拆单是对方的**正常业务行为**（判失败会把一整类正常单永久判死）；但 ⛔ **绝不静默丢** ⇒ 留一条说清「只认了第一张、其余不跟踪」的人话台账。
        判别力（两条各自能红）：① 把 `_remote_order_ids` 的"取第一段"改成"整串照用" ⇒ 链路那一列变成 `"a,b"` ⇒ 红；② 把那条留痕删掉 ⇒ 台账断言红（那就是"静默丢"）。"""
        self.enable()
        #: 对方在录单那一刻把这张单**拆**成了两张（文档：拆单时逗号分隔）。
        second_id = "20261009000000009902"
        self.upstream.create_data = f"{CLAIMED_ID},{second_id}"
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        # ⭐ 链路落的是**第一段**（不是我方拼出来的整串）：
        self.assertEqual(self.db_link().jidanpu_order_id, CLAIMED_ID)
        # ⚠️ 这一档**同时**有一个同名的 `in` 留痕行 ⇒ 取事件行必须按方向筛。
        event = self.db_out_events(kind="member.launch").get()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.payload["jidanpu_order_id"], CLAIMED_ID)
        # ⭐ 留痕：一条 `direction="in"` 的人话台账，说清"拆了几张、只认了哪一张"。
        note = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_LAUNCH, target_ref=self.order.order_no,
        ).get()
        self.assertIn("2 张", note.reason)
        self.assertIn(CLAIMED_ID, note.reason)
        self.assertIn(second_id, note.reason)
        self.assertEqual(note.status, "done")
        self.assertEqual(
            jidanpu_member_openapi_outbox.SPLIT_ORDER_REASON.format(
                count=2, first=CLAIMED_ID, extra_count=1, ids=f"{CLAIMED_ID}、{second_id}",
            ),
            note.reason,
        )

    def test_a_create_whose_data_is_not_a_string_is_a_failure(self):
        """⭐ 对照：`data` **不是字符串**（对方改版 / 回了个对象）⇒ 判失败，⛔ 不硬当单号。
        判别力：把 `_remote_order_ids` 的类型判据去掉（`str(data)` 一糊）⇒ 本用例红（那时链路那一列会落进一串 `"{'a': 1}"` 之类的**指不到任何远端单**的东西）。
        """
        self.enable()
        self.upstream.create_data = {"orderId": CLAIMED_ID}
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        self.assertIsNone(self.db_link())
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.reason, jidanpu_member_openapi_outbox.RECORD_NO_REMOTE_ID_REASON)
        #: ⛔ 不许把那个 dict `str()` 出来当单号。
        self.assertNotIn("jidanpu_order_id", event.payload)

    def test_a_rejected_record_keeps_the_upstream_message_in_the_reason(self):
        """⭐ 录单被上游拒 ⇒ `failed` + 人话里**带着对方那句原话**（⛔ 不写一句空泛的「失败了」）。
        判别力：把 `_openapi_error_reason` 里那段 `upstream_msg` 去掉 ⇒ 本用例红。
        """
        self.enable()
        self.upstream.reply(
            CREATE_ORDER_PATH,
            payload={"code": 0, "msg": "系统要求来源单号不能为空", "data": None},
        )
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_events(kind="member.launch").get()
        self.assertIn("系统要求来源单号不能为空", event.reason)
        #: ⛔ 没有链路（录单压根没成）。
        self.assertIsNone(self.db_link())


# ══════════════════════════════════════════════════════════════════════════════
# ⑨ 步骤 3：录单前的**挡重**（对方已经有这张单 ⇒ 认领，⛔ 不建第二张）
# ══════════════════════════════════════════════════════════════════════════════


class LaunchDedupeTests(_WriteCase):
    def test_an_order_the_peer_already_has_is_never_recorded_again(self):
        """⭐ 判据九（挡重）：对方**已有**这张单（按来源单号查得到）⇒ **一次录单接口都不打**。
        ⚠️ 真机形状（①式那段注释记的）：**录单成功但链路没落成**（进程中断 / 台账被重置 / 对方有人手工录过同一来源单号）⇒ 不挡重的话，重试要么被对方按「来源单号重复」拒掉、要么真灌出第二张单。⭐⭐ **改准（真机）**：这一档**认领不了**——链路那一列要的是**对方的系统订单号**，而真机回包里**没有 `orderId`**（只有 5 格）⇒ 响亮失败 + 说清「请人工去对方系统里查这张单的系统订单号」，⛔ **绝不重录**（重录才是真的造脏单）。⚠️ 上一轮那版在这里**直接认领落链路**（靠回读里的 `orderId`）——真机上那一步**永远拿不到号**，所以那版的行为只存在于假上游里。
        判别力（两条各自能红）：① 把挡重预读删掉 ⇒ 本用例红（会打一次录单——真实世界里要么被拒、要么第二张单）；② 把 `CLAIM_WITHOUT_REMOTE_ID_REASON` 那一支改成「照样录一次」⇒ 本用例红。"""
        self.enable()
        #: 对方那边**已经有**这张单（同一个来源单号，状态「未分配」）。
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=CLAIMED_ID,
            status=LABEL_UNASSIGNED, rec_fee=Decimal("50.00"),
        )
        self.queue_launch()

        stats = self.run_a_round()

        # ⭐ **一次录单接口都没打**：
        self.assertEqual(self.upstream.created, [])
        self.assertEqual(
            [pair for pair in self.upstream.write_pairs() if pair[1] == CREATE_ORDER_PATH], [],
        )
        # ⭐ **没有链路**（拿不到对方那张单的系统订单号 ⇒ 落不了）+ 响亮的人话：
        self.assertIsNone(self.db_link())
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.status, "failed")
        self.assertEqual(
            event.reason, jidanpu_member_openapi_outbox.CLAIM_WITHOUT_REMOTE_ID_REASON,
        )
        self.assertIn("人工", event.reason)
        # ⭐ 端点集合恰好：取 token + 挡重预读（一条录单都没有）。
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("GET", ORDER_STATUS_PATH),
        ])

    def test_the_retry_after_that_still_records_nothing(self):
        """⭐ 判据九（续）：挡重那一档失败之后重试 ⇒ **还是**一次录单都不打（⛔ 绝不重录）。
        ⚠️ 这一条是承重的：那一档落的是 `failed`（会退避重试）⇒ 若重试时挡重预读失效（例如把"查得到就停"写成了"查得到就录"），这里会立刻灌出第二张真单。
        判别力：把挡重预读那一支改成「只在第一次挡」⇒ 本用例红。"""
        self.enable()
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=CLAIMED_ID,
            status=LABEL_UNASSIGNED, rec_fee=Decimal("50.00"),
        )
        self.queue_launch()
        first = self.run_a_round()
        self.assertEqual(first.failed, 1)

        second = self.run_a_round(now=timezone.now() + timedelta(seconds=180))

        self.assertEqual(second.failed, 1)
        self.assertEqual(self.upstream.created, [], "重试录了一张单（真实世界 = 第二张真单）")
        self.assertIsNone(self.db_link())

    def test_the_adopted_verdict_mapping_is_kept_though_it_is_unreachable_today(self):
        """⭐ 结构判据：`adopted_verdict` 的**映射**照旧正确（虽然②式今天**到不了**它）。
        ⚠️ 为什么到不了：认领那一支要求回读给出系统订单号，而真机回包里没有 `orderId` ⇒ `ensure_remote` 的 `adopted` 恒为 `None`（见上一条）。⚠️ 但 `RecordPrimitives.adopted_verdict` 是差异面的**必需一格**（共享引擎要它），而它是"真能认领时该怎么判"的**唯一**出处 ⇒ 直接拿对象喂它（⛔ 不假装那条路走得通）。⛔ **它永远不许回 `already`**（②式读不到 `isThrowOrder` ⇒ 不许谎称"已经在大厅里"）。
        """
        verdict = jidanpu_member_openapi_outbox.adopted_verdict
        self.assertEqual(
            verdict(jidanpu_openapi.OrderStatus(status="已退单")),
            jidanpu_member.ADOPTED_VERDICT_REFUNDED,
        )
        self.assertEqual(
            verdict(jidanpu_openapi.OrderStatus(status="代练中")),
            jidanpu_member.ADOPTED_VERDICT_NOT_UNASSIGNED,
        )
        self.assertEqual(
            verdict(jidanpu_openapi.OrderStatus(status=LABEL_UNASSIGNED)),
            jidanpu_member.ADOPTED_VERDICT_NEEDS_THROW,
        )
        for label in ("", "已退单", "代练中", LABEL_UNASSIGNED, "别的"):
            with self.subTest(label=label):
                self.assertNotEqual(
                    verdict(jidanpu_openapi.OrderStatus(status=label)),
                    jidanpu_member.ADOPTED_VERDICT_ALREADY_IN_HALL,
                )

    def test_an_order_already_linked_on_the_same_address_is_not_recorded_again(self):
        """⭐ 地址级去重（照①式裁定 3）：**同一套鸡蛋铺**里已经有这张单 ⇒ 本次不录（`skipped`）。
        ⚠️ 与①式**同一条口径**：那张链路可能挂在**另一条绑定**上（含已停用的）——判据是「同一 `jidanpu_base_url` 下有没有指向这张我方单的链路行」。
        判别力：把地址级去重那一支删掉 ⇒ 本用例红。"""
        self.enable()
        other = self.make_binding()
        JidanpuMemberOrderLink.objects.create(
            binding=other, order=self.order, jidanpu_order_id=JID_B, in_hall=True,
        )
        self.queue_launch()

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        event = self.db_events(kind="member.launch").get()
        self.assertEqual(event.reason, jidanpu_member.ADDRESS_DUPLICATE_REASON)
        self.assertEqual(self.upstream.created, [])
        #: ⛔ 一次上游都不打（连挡重预读都不需要：本地已经有链路了）。
        self.assertEqual(self.upstream.requests, [])


# ══ ⑨·二 ⭐⭐ D-0249（修 E-0246）：②式的**现场回归** + 对账**无 churn** ══════
# E-0246 现场（kexing 一次真实回声、台账连号见证）：`member.launch` done（录进对方抢单大厅）→ 指派给**绑定主人自己** ⇒ `member.assign` 排队时连带排了一条 `member.unlist` ⇒ 旧形态把它收敛成「已退单」＝**接手人自己店里的那张唯一的活单被退掉**（34 张）。
# ⇒ D-0249 之后：unlist **终态 skip + 零上游**、链路照旧「在对方大厅」，且对账不再重排。


class UnlistFieldRegressionTests(_WriteCase):
    """⭐⭐ **现场回归**：launch → 指派给绑定主人 ⇒ unlist skip、零上游、对方那张单活着。"""

    def _take_by_binding_owner(self):
        """把这张单的接手人写成**绑定主人**（我方「谁接了」= MAIN 绩效行的 `user`，
        对账 `_shape` 就按它判 `ORDER_ONLY`；⛔ 不是手改订单列——那不是仓里的判据）。"""
        return Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.binding.user,
            temp_amount=Decimal("88.00"),
        )

    def test_launch_then_assign_to_the_owner_leaves_the_peer_order_alive(self):
        """⭐⭐ 现场回归：launch done → **真触发点**指派给绑定主人 ⇒ unlist 终态 skipped、`ChangeOrderStatus` **零次**调用、`link.in_hall` 仍 True、`withdrawn_at` 仍 None；assign / price 等其余事件**行为不变**。
        ⚠️ 排队那一跳用**既有触发点** `jidanpu_member.queue_assign`（⛔ 不手搓台账行）：它会按 `link.in_hall` 连带排出 unlist——**排队侧行为一字不动**（它只记「人的动作发生了」，能不能办由线决定）；本用例钉的是「办不了 + 说清为什么」。
        判别力：① 摘掉分派拦截 ⇒ unlist 落回 `_consume_unlist` 四跳 ⇒ OpenApiClient 没有①式那套方法 ⇒ 未预期异常，本用例红；② 把拦截改成「收敛成退单」（历史形态）⇒ `ChangeOrderStatus` 被打、远端变已退单 ⇒ 红。"""
        self.enable()
        self.queue_launch()
        first = self.run_a_round()
        self.assertEqual((first.done, first.failed, first.skipped), (1, 0, 0))
        link = self.db_link()
        self.assertIsNotNone(link)
        self.assertTrue(link.in_hall)
        self.assertEqual(self.upstream.statuses[CLAIMED_ID], LABEL_UNASSIGNED)
        requests_after_launch = list(self.upstream.requests)

        # 当前订单与 MAIN 接手人须与真实指派完成后的形态一致。
        self.prepare_owner_assignment()
        self.assertEqual(
            jidanpu_member.queue_assign(self.order, taker_id=self.binding.user_id), 1,
        )
        self.assertEqual(
            self.db_out_events(kind=KIND_UNLIST_LITERAL).count(), 1,
            "排队侧行为一字不动：指派仍排出 unlist（能不能办由线决定）",
        )

        second = self.run_a_round()

        # 指派先按内部号回查旧单，再完成同步。
        assign_row = self.db_out_events(kind="member.assign").get()
        self.assertEqual(assign_row.status, "done")
        self.assertEqual((second.done, second.skipped, second.failed), (1, 1, 0))
        # ⭐⭐ unlist：终态 skip + 零上游（分派处拦截，连 token 都不打）：
        unlist_row = self.db_out_events(kind=KIND_UNLIST_LITERAL).get()
        self.assertEqual((unlist_row.status, unlist_row.attempts), ("skipped", 0))
        self.assertIn(UNLIST_UNAVAILABLE_REASON_LITERAL, unlist_row.reason)
        requests = self.upstream.requests[len(requests_after_launch):]
        self.assertEqual([(r["method"], r["path"], r["params"]) for r in requests], [
            ("GET", ORDER_STATUS_PATH, {"orderId": CLAIMED_ID}),
        ])
        self.assertEqual(
            self.upstream.json_calls(CHANGE_ORDER_STATUS_PATH), [],
            "ChangeOrderStatus 被打过 = 接手人自己店里的活单被退（E-0246 本身）",
        )
        # ⭐ 链路与远端现场一个字节都不动：
        link = self.db_link()
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        self.assertEqual(self.upstream.statuses[CLAIMED_ID], LABEL_UNASSIGNED)

        # ⭐ 改价那档照旧真办（「其余事件行为不变」的另一半）：
        self._queue_price()
        third = self.run_a_round()
        self.assertEqual((third.done, third.failed, third.skipped), (1, 0, 0))
        self.assertEqual(
            len(self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH)), 1,
            "改价照旧打它自己的端点（录单那一发在前两轮，不算在这一格）",
        )
        self.assertEqual(self.db_out_events(kind="member.price").get().status, "done")
        self.assertEqual(
            self.upstream.json_calls(CHANGE_ORDER_STATUS_PATH), [],
            "三轮全程没有一枚 ChangeOrderStatus",
        )
        self.assertTrue(self.db_link().in_hall)

    def test_two_reconcile_rounds_queue_zero_new_unlist_events(self):
        """⭐⭐ 对账**无 churn**：同形态连跑两轮 `reconcile_and_queue` ⇒ **零条新 unlist 事件**。
        ⚠️ 钉的是 E-0246 的另一半：对账的 `to_unlist` 那一支按「已接手 + 还在对方大厅」排撤下——不收口的话**每 180 秒一轮**都会重排一条（终态 skip 挡得住重试、⛔ 挡不住重排）。⚠️ 形态必须真到得了那一支：MAIN 绩效行把接手人写成绑定主人 ⇒ `_shape` = `ORDER_ONLY`，链路 `in_hall=True` ⇒ 正是「已接手 + 还在对方大厅」。
        判别力：摘掉对账收口（`reconcile` 里 `unlist_unavailable_reason` 那条）⇒ 两轮各排一条 ⇒ 本用例红在「unlist 计数」上。"""
        self.enable()
        self.queue_launch()
        self.run_a_round()
        self._take_by_binding_owner()

        before = self.db_out_events(kind=KIND_UNLIST_LITERAL).count()
        first = jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        second = jidanpu_member.reconcile_and_queue(
            self.binding, now=timezone.now() + timedelta(seconds=180)
        )

        self.assertEqual((first.unlist, second.unlist), (0, 0), "对账不许再排撤下（②式）")
        self.assertEqual(
            self.db_out_events(kind=KIND_UNLIST_LITERAL).count(), before,
            "不收口就会每轮重排一条注定 skip 的 unlist（churn）",
        )


# ══════════════════════════════════════════════════════════════════════════════
# ⑩ 步骤 3：`member.price`（改价：**以写返回为准** + 「大厅那一档」的人话）
# ══════════════════════════════════════════════════════════════════════════════

# ⭐⭐ ②式**做不到**「回读钉值」——`GetOrderStatus` 的回包实测**没有 `recFee`**（只有 5 格）⇒ 只能以写端点的成功码为准，并**每次改价**留一条人话台账把这件事写清楚（`PRICE_CONFIRM_WRITE_ONLY_REASON`）。
# ⚠️ 上一轮那两条「回读没变 ⇒ 失败 / 读不到那一格 ⇒ 失败」的用例**已删除**：它们建在假上游多回的 `recFee` 上 ⇒ 真机上**每一次改价都停在「写后未能确认」**、退避到 `abandoned`（用例却全绿）。⚠️ ①式那套「回读钉值」的判据照旧由①式的用例守着（`_consume_price` 的那条分支一个字都没改）。


class PriceTests(_WriteCase):
    def test_a_price_change_the_peer_accepts_is_done_on_the_write_code_alone(self):
        """⭐⭐ 判据十一（改价）：写成功 ⇒ `done`（**以写端点的成功码为准**）+ 一条写清口径的人话。
        ⚠️ 真机依据：`GetOrderStatus` 的回包里**没有接单价那一格** ⇒ ①式那套「写后回读钉值」在②式**做不到**。⛔ 不许假装能回读（上一轮那版正是在这里恒假 ⇒ 每一次改价都退避到 `abandoned`）；⛔ 也不许"什么都不确认就 `done`"而台账上不说清楚 ⇒ 本用例钉那条人话台账。判别力（两条各自能红）：① 把 `read_price` 加回来（"假装能回读钉值"）⇒ 端点集合那条断言红（多一次 GET），而且 `done` / 台账两条也会红（真形状下它会恒判"未确认"）；② 把那条人话台账删掉 ⇒ 台账断言红（那就是"什么都不确认就 done"）。
        """
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        body = self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH)[0]
        self.assertEqual(body["orderId"], JID)
        self.assertEqual(Decimal(str(body["price"])), Decimal("50.00"))
        # ⭐ **对方那一格真的变了**（读数取自假上游自己的账本，⛔ 不是「调用过就算」）：
        self.assertEqual(self.upstream.current_rec_fee(JID), Decimal("50.00"))
        # ⭐ 端点集合**恰好**：取 token + 写价（⛔ **没有**回读那一跳 —— 真机回读里没有那一格）。
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("POST", MODIFY_ORDER_PRICE_PATH),
        ])
        self.assertEqual(self.db_out_events(kind="member.price").get().status, "done")
        # ⭐⭐ 那条人话台账（口径：以写返回为准、弱一档）—— `direction="in"` 的 `KIND_PRICE` 行：
        note = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_PRICE, target_ref=self.order.order_no,
        ).get()
        self.assertIn("成功码", note.reason)
        self.assertIn("没有接单价那一格", note.reason)
        self.assertEqual(
            jidanpu_member_openapi_outbox.PRICE_CONFIRM_WRITE_ONLY_REASON, note.reason,
        )

    def test_a_price_change_the_peer_rejects_is_failed(self):
        """⭐ 判据十一（反面）：**写接口失败**（失败信封）⇒ `failed` + 对方原话，⛔ 不收敛 `done`。
        ⚠️ ②式只剩这一条**硬**信号（写返回码）⇒ 它必须真的能把失败判出来（照 E-0087：调用不抛异常 ≠ 成功；判据在 `jidanpu_openapi._write` 那一处按 `code > 0`）。
        判别力：把 `write_price` 的 `except OpenApiError` 那一支删掉（吞掉失败照 `done`）⇒ 本用例红。"""
        self.enable()
        self._link(in_hall=False)
        self.upstream.reject_price(msg="系统要求价格不能为空")
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_out_events(kind="member.price").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("系统要求价格不能为空", event.reason)
        #: ⛔ 失败时**不许**留下那条"以写返回为准"的完成台账（那会看起来像改成功了）。
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                direction="in", kind=jidanpu_member.KIND_PRICE, target_ref=self.order.order_no,
            ).exists()
        )

    def test_a_write_only_confirmation_without_human_words_raises_loudly(self):
        """⭐ 配了「回读里没有那一格」却**没给人话** ⇒ 响亮抛（⛔ 不许"什么都不确认就 done"）。
        判别力：把 `_consume_price` 里那句 `read_confirm_unavailable_reason` 的空值检查删掉 ⇒ 本用例红（那时台账上会出现一条**说不清口径**的完成）。
        """
        base = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        mute = _muted_read_confirm_line(base)
        self.enable()
        self.order = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.WAITING_GRAB)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID_B, in_hall=False,
        )
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PRICE, rearm=True,
        )
        #: ⛔ 用一个**假的**客户端：这一条判据要在「真打上游之后 / 判完成之前」那个点上抛，
        #: 所以只要它那个方法**够用到走进分支**即可（`PriceTests` 里那两条有真读数）。
        client = _StubPriceClient()
        with self.assertRaises(ValueError):
            jidanpu_member._consume_price(
                event, binding=self.binding, client=client, http=None,
                now=timezone.now(), line=mute,
            )
        # ⚠️ 价**已经写出去了**（那一支在最前面）⇒ 抛的是「没说清确认口径」。
        self.assertEqual(client.written, [Decimal("50.00")])

    def test_the_hall_branch_writes_a_human_line_instead_of_silently_skipping_it(self):
        """⭐ 判据十一（大厅那一档，**纯②式**）：②式**没有**「补刀」这个动作 ⇒ 照旧改价、**另留一句人话台账**写清缺口（⛔ 不许静默少做）。
        ⚠️⭐ **D-0250 改准缺口的内容**：纯②式那一个端点写的是**大厅那一格 `recFee`** ⇒ 大厅那格其实改得到；**真少做的是另一半**——引擎契约「接单价 `price` 格永远要跟」在②式没有对应动作（旧句「接单价照旧改了」是把格认错了，原文见分支点提交）。那句台账要点名接单价没动、可能留旧值、怎么办（配①式凭据走桥接价档）。⚠️ 那句台账是 `direction="in"` 的 `KIND_PRICE` 行（照 `_note_order_once` 的既有形状），⛔ 与事件行本身分开（事件行仍然 `done`）；⚠️ 这一档改价会有**两条** `in` 行（一条说"以写返回为准"，一条说"接单价没动"）⇒ 判据按**内容**点名，⛔ 不靠行数。
        判别力：把 `hall_not_applicable_reason` 那一支删掉（静默 `done`）⇒ 本用例红。"""
        self.enable()
        self._link(in_hall=True)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        # ⭐ 大厅那一格**真的改了**（这个端点实测写的就是它）：
        self.assertEqual(self.upstream.current_rec_fee(JID), Decimal("50.00"))
        notes = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_PRICE, target_ref=self.order.order_no,
        )
        self.assertEqual(
            sorted(note.reason for note in notes),
            sorted([
                jidanpu_member_openapi_outbox.PRICE_CONFIRM_WRITE_ONLY_REASON,
                jidanpu_member_openapi_outbox.HALL_NOT_APPLICABLE_REASON,
            ]),
        )
        hall = notes.get(reason=jidanpu_member_openapi_outbox.HALL_NOT_APPLICABLE_REASON)
        # ⭐⭐ D-0250 改准后的缺口如实登记（三件事缺一不可）：
        # 「接单价 price 格没动」+「可能留旧值」+「怎么办（配①式账号密码走桥接）」：
        self.assertIn("大厅", hall.reason)
        self.assertIn("接单价", hall.reason)
        self.assertIn("留旧值", hall.reason)
        self.assertIn("账号密码", hall.reason)
        # ⛔ **一条「抛单 / 重抛」类的写请求都没有**（②式没有那一跳）。
        self.assertEqual(self.upstream.write_pairs(), [("POST", MODIFY_ORDER_PRICE_PATH)])

    def test_an_order_without_an_out_price_is_a_terminal_skip(self):
        """⭐ 判据十一（无发单价）：①式那一档是 `skipped` 终态（⛔ 不写 `0` —— 那是「免费送」）⇒ 照搬。
        判别力：把 `hall_price is None` 那一支删掉（或改成回落到 `total_price`）⇒ 本用例红。
        """
        Order.objects.filter(pk=self.order.pk).update(hall_price=None)
        self.enable()
        self._link(in_hall=False)
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        event = self.db_events(kind="member.price").get()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, jidanpu_member.PRICE_NO_PRICE_REASON)
        #: ⛔ 一次上游都不打（连取 token 那一跳都不许有）。
        self.assertEqual(self.upstream.requests, [])

    def test_a_price_change_without_a_link_is_a_skip_without_any_upstream(self):
        """没有链路 ⇒ 终态跳过 + 人话（照①式那一档；⛔ 一次上游都不打）。"""
        self.enable()
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        self.assertEqual(
            self.db_events(kind="member.price").get().reason,
            jidanpu_member.PRICE_NO_LINK_REASON,
        )
        self.assertEqual(self.upstream.requests, [])


# ══════════════════════════════════════════════════════════════════════════════
# ⑫ 改了「接手项目」（`Order.project_text`）⇒ 改对方那张单的项目名称
# ══════════════════════════════════════════════════════════════════════════════

# ⚠️ 背景（业务方报的）：下家线原先**只有 6 个触发点**，而「内容」只在**建单那一刻**发一次 ⇒ 建单之后再改「接手项目」**不同步**。本节的判据 = 新加的那一档真的推得动、失败照旧 `failed` + 退避、`gameName` 与建单**同源**。
# ⚠️ 排事件那一侧（「改了才排 / 没改一条都不排」）的判据在 `tests/test_jidanpu_member_triggers.py::ProjectEditTests`（走**真 HTTP 的编辑保存端点**）。

#: 项目名称那一格的**字面值**（⛔ 不从被测常量派生，E-0024）。
PROJECT_OLD_LITERAL = "合成项目丙（旧）"
PROJECT_NEW_LITERAL = "合成项目丁（新）"


class PricePasswordBridgeTests(_WriteCase):
    """⭐⭐ ②式价档**①式桥接**（**D-0250 改准语义**）：绑定行上有①式账号密码 ⇒ 价档照**引擎契约**走——**不分厅态**一律 `ChangeOrderPrice`（接单价 `price` 格）+ 回读钉 `price`；在厅再由 `rethrow_in_hall` 用 `ModifyOrderPrice` 改大厅那一格 `recFee` 并①式单查钉值；两格都 = 我方发单价（D-0162 两格同值）。
    ## 现场依据（为什么有这一档、为什么 D-0250 改准）：旧版只在「非在厅」走①式（在厅只调 `ModifyOrderPrice`、`price` 格从来不写，回读还按厅态改读 `recFee`）⇒ E-0247（生产实测，kexing 在厅改价单的真单号已外移 `docs/code-comment-notes.md`）：在厅改价后台账 `done` 而远端 `price` 旧值 / `recFee` 新值 / 我方发单价三者劈叉——「接单价永远要跟」是引擎契约（`_consume_price` docstring；权威证据 `jidanpu.py::change_order_price`「改接单价（`price` 那一格）」），在厅不能豁免。D-0250：有①式凭据不分厅态都走①式写 + 钉 `price`；在厅半句改为真的 `rethrow_in_hall`（写 `recFee` + 钉 `recFee`）。
    ⚠️ 测法：`open_client_with_http` 打在**被查找的模块**上（`apps.business.jidanpu`，E-0029），替身记录 `ChangeOrderPrice` 调用并供给①式单查快照——「真的发出去了的那一份」的判据打在替身账本与②式假上游的账本两处。⭐ 快照里 `price` / `recFee` 两格**分开摆**：「在厅」用例的 `price` 格对应①式写的钉值、`recFee` 格对应补刀的钉值，谁没钉上就红谁。"""

    def setUp(self):
        super().setUp()
        #: ⭐ 桥接的闸 = 绑定行上的①式账号密码（本类默认**配上**；存量 PriceTests 的
        #: 夹具默认空 ⇒ 走纯②式现状，两类互不干扰）。
        self.binding.account = "jidanpu-acct-bridge"
        self.binding.password = "jidanpu-pwd-bridge"
        self.binding.save()
        #: ⭐ ①式那两个函数打在**被查找的模块**上（`apps.business.jidanpu`，E-0029）：
        #: `_member_write_price` / `_member_read_price` 转手就是它们。建连那一下换成空上下文
        #: （真建连是 httpx，单元层不需要）。
        self.pw_price_calls: list[tuple[str, Decimal]] = []
        self.pw_snapshot: dict[str, dict] = {}
        self.pw_bindings: list[int] = []

        def _fake_change_order_price(client, *, remote_order_id, fee):
            self.pw_price_calls.append((remote_order_id, Decimal(str(fee))))

        def _fake_fetch(client, *, key, expect_field, expect_value):
            return self.pw_snapshot.get(key)

        def _fake_open_client(binding):
            self.pw_bindings.append(binding.pk)
            return contextlib.nullcontext((None, None))

        for target, fake in (
            ("change_order_price", _fake_change_order_price),
            ("fetch_order_by_key1", _fake_fetch),
            ("open_client_with_http", _fake_open_client),
        ):
            patcher = mock.patch.object(jidanpu_sdk, target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _queue_price(self):
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PRICE, rearm=True,
        )

    def test_an_in_hall_price_change_writes_both_cells_and_pins_both(self):
        """⭐⭐ 判据（D-0250 主用例）：**在厅 + 有凭据** ⇒ 同一事件里 `ChangeOrderPrice`（接单价 `price` 格）与 `ModifyOrderPrice`（大厅 `recFee` 格）**都发生**、`price` 由回读钉值、`recFee` 由补刀钉值 ⇒ 事件 `done`。
        判别力：把 `_bridged_write_price` 的①式枝摘掉（恢复旧条件「在厅不走①式」= E-0247 现场形状）⇒ 本用例红（`price` 格那一发消失）。
        """
        self.enable()
        other = self.make_binding(account="other-audit-store", password="synthetic-password")
        other.jidanpu_base_url = "https://other-audit-store.test.invalid"
        other.save(update_fields=["jidanpu_base_url"])
        JidanpuMemberOrderLink.objects.create(
            binding=other, order=self.make_order(order_no=ORDER_NO_B), jidanpu_order_id=JID, in_hall=True,
        )
        self._link(in_hall=True)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        #: ⭐ 两格分开摆：`price` 钉①式写的接单价、`recFee` 钉补刀写的大厅价。
        self.pw_snapshot[JID] = {
            "orderId": JID, "price": "50.00", "recFee": "50.00", "isThrowOrder": "Y",
        }
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        self.assertEqual(self.pw_bindings, [self.binding.pk] * 3)
        # ⭐ 接单价 `price` 格：①式那一发真的发生了、价逐字是我方发单价（D-0162：⛔ 绝不泄漏客户价）：
        self.assertEqual(self.pw_price_calls, [(JID, Decimal("50.00"))])
        # ⭐ 大厅 `recFee` 格：同一事件里 `ModifyOrderPrice` 也发生了、假上游账本真的变了：
        body = self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH)[0]
        self.assertEqual(Decimal(str(body["price"])), Decimal("50.00"))
        self.assertEqual(self.upstream.current_rec_fee(JID), Decimal("50.00"))
        # ⛔ 两句「弱确认 / 少做」人话都不该出现（两格都钉上了）：
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                direction="in", kind=jidanpu_member.KIND_PRICE,
                target_ref=self.order.order_no,
            ).exists()
        )
        self.assertEqual(self.db_out_events(kind="member.price").get().status, "done")

    def test_bridge_skips_the_hall_price_when_the_web_read_confirms_left(self):
        self.enable()
        self._link(in_hall=True)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        self.pw_snapshot[JID] = {
            "orderId": JID, "price": "50.00", "recFee": "10.00",
            "isThrowOrder": "N", "orderSta": LABEL_UNASSIGNED,
        }
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        self.assertEqual(self.pw_price_calls, [(JID, Decimal("50.00"))])
        self.assertEqual(self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH), [])
        self.assertEqual(self.upstream.current_rec_fee(JID), Decimal("10.00"))
        event = self.db_out_events(kind="member.price").get()
        self.assertIn("已离开大厅", event.reason)

    def test_an_in_hall_rethrow_pin_mismatch_is_not_done(self):
        """⭐ 判据（D-0250 ④）：在厅补刀写了 `recFee` 但回读**钉不上** ⇒ **不落 done**
        （failed 退避；照①式「回读钉值」判据，⛔ 不是「调用了就算」）。"""
        self.enable()
        self._link(in_hall=True)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        #: `price` 钉上了、`recFee` 停在旧值（对方「回了成功却没改」那半边）：
        self.pw_snapshot[JID] = {
            "orderId": JID, "price": "50.00", "recFee": "40.00", "isThrowOrder": "Y",
        }
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_out_events(kind="member.price").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("recFee", event.reason)
        self.assertIn("未能确认", event.reason)
        # ⭐ 前半段（接单价）已经写过、`ModifyOrderPrice` 也打了（失败发生在钉值那一步）：
        self.assertEqual(self.pw_price_calls, [(JID, Decimal("50.00"))])
        self.assertEqual(len(self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH)), 1)

    def test_an_in_hall_rethrow_write_rejection_is_failed_with_the_upstream_words(self):
        """⭐ 判据：在厅补刀的 `ModifyOrderPrice` 被拒（失败信封）⇒ `failed` + 对方原话，
        ⛔ 不收敛 `done`；大厅那一格**真的没变**（读数取自假上游账本）。"""
        self.enable()
        self._link(in_hall=True)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        ).reject_price(msg="系统要求价格不能为空")
        self.pw_snapshot[JID] = {
            "orderId": JID, "price": "50.00", "recFee": "50.00", "isThrowOrder": "Y",
        }
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_out_events(kind="member.price").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("系统要求价格不能为空", event.reason)
        self.assertIn("recFee", event.reason)
        self.assertEqual(self.upstream.current_rec_fee(JID), Decimal("10.00"))

    def test_a_non_hall_price_change_goes_through_the_password_channel(self):
        """非在厅 + 有凭据 ⇒ **①式写 + 回读钉值**，⛔ 不打 `ModifyOrderPrice`（D-0250 下
        这一枝行为与 2026-10-11 版**逐字相同**：现状回归）。"""
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        self.pw_snapshot[JID] = {"orderId": JID, "price": "50.00"}
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        # ⛔ ②式 API 那一发**不该发生**（现场实测它在非在厅形态是「回成功但不改」）：
        self.assertEqual(self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH), [])
        # ⭐ ①式那一发真的发生了、价逐字是我方发单价（D-0162：⛔ 绝不泄漏客户价）：
        self.assertEqual(self.pw_price_calls, [(JID, Decimal("50.00"))])
        # ⭐ 强确认（回读钉值）⇒ 「弱一档」那条人话台账**不该出现**：
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                direction="in", kind=jidanpu_member.KIND_PRICE,
                target_ref=self.order.order_no,
            ).exists()
        )
        self.assertEqual(self.db_out_events(kind="member.price").get().status, "done")

    def test_a_non_hall_readback_mismatch_is_a_retry_not_done(self):
        """回读不符（对方没改 / 改成别的值）⇒ **不落 done**（照①式「回读钉值」判据）。"""
        self.enable()
        self._link(in_hall=False)
        self.pw_snapshot[JID] = {"orderId": JID, "price": "40.00"}
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.failed, 1)
        event = self.db_out_events(kind="member.price").get()
        self.assertEqual(event.status, "failed")
        self.assertIn("确认", event.reason)
        self.assertEqual(self.pw_price_calls, [(JID, Decimal("50.00"))])

    def test_without_password_falls_back_to_the_api_weak_confirm(self):
        """无①式凭据（夹具默认形态，E-0202 那个真形态）⇒ **原样回落**：API + 弱确认台账。
        ⚠️ D-0250 口径：回落时 `ModifyOrderPrice` 只改得到大厅那一格 `recFee`，接单价 `price` 格没动——缺口由台账如实登记（文案见 `HALL_NOT_APPLICABLE_REASON`，在厅档才写；本条是非在厅档，只有「以写返回为准」那一句）。
        """
        self.binding.account = ""
        self.binding.password = ""
        self.binding.save()
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            rec_fee=Decimal("10.00"),
        )
        self._queue_price()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        self.assertEqual(len(self.upstream.json_calls(MODIFY_ORDER_PRICE_PATH)), 1)
        self.assertEqual(self.pw_price_calls, [])
        note = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_PRICE,
            target_ref=self.order.order_no,
        ).get()
        self.assertEqual(
            note.reason, jidanpu_member_openapi_outbox.PRICE_CONFIRM_WRITE_ONLY_REASON,
        )

    def test_line_selection_follows_password_presence(self):
        """选线单元：有凭据 ⇒ 桥接线（`read_price` / `rethrow_in_hall` 都非 None）；
        无 ⇒ 原线（`read_price` / `rethrow_in_hall` None + 两句非空人话）。"""
        bridged = jidanpu_member_openapi_outbox.member_openapi_outbox_line_for(self.binding)
        self.assertIsNotNone(bridged.price.read_price)
        # ⛔ 桥接线回读**有**那一格 ⇒ 「弱一档」那句永远不会被读到 ⇒ 必须是缺省空串：
        self.assertEqual(bridged.price.read_confirm_unavailable_reason, "")
        # ⭐ D-0250：在厅那一档是**真的**（不再是「无需重抛」+ 一句台账）：
        self.assertIsNotNone(bridged.price.rethrow_in_hall)
        self.assertEqual(bridged.price.hall_not_applicable_reason, "")

        self.binding.account = ""
        self.binding.save()
        plain = jidanpu_member_openapi_outbox.member_openapi_outbox_line_for(self.binding)
        self.assertIs(plain, jidanpu_member_openapi_outbox.member_openapi_outbox_line())
        self.assertIsNone(plain.price.read_price)
        self.assertIsNone(plain.price.rethrow_in_hall)
        self.assertTrue(plain.price.read_confirm_unavailable_reason.strip())
        self.assertTrue(plain.price.hall_not_applicable_reason.strip())


class ProjectTests(_WriteCase):
    def _link(self, *, in_hall=False, remote_id=JID):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    def _queue_project(self, order=None):
        """造一条**由既有触发点写出来**的 `member.project` 台账（⛔ 不手搓行）。"""
        return jidanpu_member.queue_project_update(order or self.order)

    def _set_project(self, text, order=None):
        """把我方订单上那一格改掉（模拟「编辑保存里真的改了那一列」）。"""
        Order.objects.filter(pk=(order or self.order).pk).update(project_text=text)

    def _assert_new_edit_survives_the_previous_result(self, old_result):
        self.enable()
        self._link()
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        api = make_logged_in_client(
            username="concurrent-project-editor", groups=("客服",),
            nodes=("order.add_edit", "order.view_detail", "order.list"),
        )

        def edit(project, remark):
            response = api.patch(
                f"/api/business/orders/{self.order.order_no}/",
                {"project_text": project, "remark": remark}, content_type="application/json",
            )
            self.assertEqual(response.json()["code"], 0, response.json())

        edit("第一次合成编辑", "第一次备注")
        original = self.upstream.handler

        def edit_during_receipt(request):
            if request.method == "POST" and request.url.path == MODIFY_ORDER_PATH:
                edit("第二次合成编辑", "第二次备注")
                if old_result == "alarm":
                    raise jidanpu_member._OutboxEventTimeout
                if old_result == "rejected":
                    self.upstream._record(request)
                    return httpx.Response(200, json={"code": 0, "msg": "上次改单被拒", "data": ""})
            return original(request)

        clock = mock.Mock()
        clock.monotonic.side_effect = (0, jidanpu_member.OUTBOX_EVENT_DEADLINE_SECONDS + 1)
        with mock.patch.object(self.upstream, "handler", edit_during_receipt):
            if old_result == "elapsed":
                with mock.patch.object(jidanpu_member, "time", clock):
                    first = self.run_a_round()
            else:
                first = self.run_a_round()

        self.assertEqual((first.done, first.failed, first.deferred), (0, 0, 1))
        pending = self.db_out_events(kind=jidanpu_member.KIND_PROJECT).get()
        self.assertEqual((pending.status, pending.attempts, pending.reason), ("pending", 0, ""))
        self.assertEqual(self.run_a_round().done, 1)
        pending.refresh_from_db()
        self.assertEqual(pending.status, "done")
        self.assertEqual(self.upstream.rows[JID]["project"], "第二次合成编辑")
        self.assertEqual(self.upstream.json_calls(MODIFY_ORDER_PATH)[-1]["orderRemark"], "第二次备注")

    def test_previous_success_does_not_finish_a_new_project_edit(self):
        self._assert_new_edit_survives_the_previous_result("accepted")

    def test_previous_rejection_does_not_delay_a_new_project_edit(self):
        self._assert_new_edit_survives_the_previous_result("rejected")

    def test_previous_alarm_does_not_consume_a_new_project_edits_retry(self):
        self._assert_new_edit_survives_the_previous_result("alarm")

    def test_previous_elapsed_timeout_does_not_consume_a_new_project_edits_retry(self):
        self._assert_new_edit_survives_the_previous_result("elapsed")

    def test_confirmed_withdrawal_prevents_a_pending_project_write(self):
        self.enable()
        link = self._link(in_hall=True)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        self.queue_kind(KIND_WITHDRAW_LITERAL)
        self._set_project(PROJECT_NEW_LITERAL)
        self.assertEqual(self._queue_project(), 1)
        original = self.upstream.handler

        def reflect_refund(request):
            response = original(request)
            if request.method == "POST" and request.url.path == CHANGE_ORDER_STATUS_PATH:
                self.upstream.rows[JID]["status"] = self.upstream.statuses[JID]
            return response

        with mock.patch.object(self.upstream, "handler", reflect_refund):
            self.run_a_round()

        link.refresh_from_db()
        self.assertIsNotNone(link.withdrawn_at)
        self.assertEqual(self.upstream.rows[JID]["status"], LABEL_REFUNDED)
        self.assertEqual(self.upstream.json_calls(MODIFY_ORDER_PATH), [])
        self.assertEqual(self.upstream.rows[JID]["project"], PROJECT_OLD_LITERAL)
        self.assertEqual(self.db_out_events(kind=jidanpu_member.KIND_PROJECT).get().status, "skipped")

    def test_a_project_change_really_lands_on_the_peer_row(self):
        """⭐⭐ 判据一（端到端）：排事件 ⇒ 消费 ⇒ **对方那一格真的变了** + 请求体逐字。
        判别力（三条都能红）：① 把 `write_project` 换成空实现（或把 `ModifyOrder` 那一段删掉）⇒ 对方账本不变 ⇒ 红；② 把 `gameName` 取成别的出处（如 `order.game_id`）⇒ 请求体那条断言红；③ 把那条「以写返回为准」的人话台账删掉 ⇒ 台账断言红（＝"什么都不确认就 done"）。⭐⭐ **请求体恰好那 5 格**（真机补齐）：`orderID` / `souresOrderID` / `gameName` / `gameArea` / `project`——少任何一格都红（⚠️ 假上游照真接口**收紧**了：缺 `gameArea` 回「游戏大区为必填项」、缺 `souresOrderID` 回「系统要求来源单号不能为空」⇒ 那一发会被判 `failed`，本用例的 `done` 与账本断言都会红）。
        """
        self.enable()
        self._link(in_hall=False)
        #: 对方那张单**此刻**的项目名是**旧值** ⇒ 「真的变了」才有读数可看
        #: （⛔ 别让夹具一上来就是新值：那样写不写都绿）。
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        self._set_project(PROJECT_NEW_LITERAL)

        self.assertEqual(self._queue_project(), 1)
        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        # ⭐⭐ 请求体：**恰好**是那张表里标「必发」的那一串（第二现场改准：原句写的是「恰好五格」，那是**整单覆盖**被量出来之前的形态——五格能"打得过"，但没发的格会被清成默认值 ⇒ 现在必须发全）。
        #   ⚠️ 那份清单的**唯一出处**是被测模块的 `MODIFY_ORDER_REQUIRED_CELLS`，逐格的取值与证据见 `MODIFY_ORDER_PAYLOAD_CELLS`；
        #   「多一格 / 少一格各自能红」由 `ProjectPayloadTests` 那两条钉住。
        body = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        self.assertEqual(
            set(body), set(jidanpu_member_openapi_outbox.MODIFY_ORDER_REQUIRED_CELLS),
        )
        self.assertEqual(
            set(body), {
                "orderID", "souresOrderID", "gameName", "gameArea", "project",
                "hours", "orderSoures", "originPrice", "price", "customerId",
                "remark", "orderRemark",
            },
        )
        self.assertEqual(body["orderID"], JID)
        self.assertEqual(body["souresOrderID"], self.source_order_no())
        self.assertEqual(body["project"], PROJECT_NEW_LITERAL)
        #: ⭐ `gameArea` **非空**（发空串 = 每一发都被真接口拒，见假上游那一段的实测原话）。
        self.assertNotIn(body["gameArea"], ("", None))
        self.assertEqual(body["gameArea"], body["gameArea"].strip())
        # ⭐⭐ **对方那一格真的变了**（读数取自假上游**自己的账本**，⛔ 不是「调用过就算」）。
        self.assertEqual(self.upstream.current_project(JID), PROJECT_NEW_LITERAL)
        # ⭐ 端点集合**恰好**：取 token + 改单（⛔ **没有**回读那一跳 —— 真机回读里没有这一格）。
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("POST", MODIFY_ORDER_PATH),
        ])
        self.assertEqual(self.db_out_events(kind="member.project").get().status, "done")
        # ⭐⭐ 那条人话台账（口径：以写返回为准、弱一档）—— `direction="in"` 的 `KIND_PROJECT` 行：
        note = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_PROJECT,
            target_ref=self.order.order_no,
        ).get()
        self.assertEqual(
            jidanpu_member_openapi_outbox.PROJECT_CONFIRM_WRITE_ONLY_REASON, note.reason,
        )
        self.assertIn("成功码", note.reason)
        self.assertIn("没有项目名称那一格", note.reason)

    def test_the_game_area_and_the_source_id_are_the_very_cells_the_create_sent(self):
        """⭐⭐ 判据（那两格的**来源**）：`gameArea` / `souresOrderID` 与**建单那一发**逐字相等。
        ⚠️ 为什么必须有它：`ModifyOrder` 这两格都是**实测必填**（真机：缺 `gameArea` ⇒ 回「游戏大区为必填项」，缺 `souresOrderID` ⇒ 回「系统要求来源单号不能为空」），而它们的值**必须与建单同源**（对方按它们认这张单 / 认这个大区）：`gameArea` ← `_game_area(order)`（那个「永不回空」的既有口径）、`souresOrderID` ← `jidanpu_member.effective_source_order_no(order)`（建单那一刻同一个函数）。⚠️ **读数打在「两个真的发出去的请求体」上**（⛔ 不是拿 `_create_body()` 的返回值比：那是我方中间层，比它 = 比不到"发出去的那一格"）。⚠️ 夹具刻意让订单**挂一个区服**（`合成区服名乙`）：不挂时两边的 `gameArea` 都回落同一个常量 ⇒ 「两边取值口不同」也能绿（这条判据的立身之本就是**两个值必须不同才有信息**）。
        判别力（两条都能红）：① 把 `write_project` 里的 `game_area=_game_area(order)` 换成 `GAME_AREA_DEFAULT`（或任何别的常量）⇒ `gameArea` 断言红；② 把 `source_order_id=…effective_source_order_no(order)` 换成 `order.order_no`（或链路里的远端单号）⇒ `souresOrderID` 断言红。⚠️ 两条**分别**能红：光把其中一格去掉也会红（假上游照真接口收紧了那两格）。"""
        self.enable()
        #: ⭐ 先**真的录一次单**（CreateOrder 那一发留下请求体当对照），再改项目。
        #: ⚠️ 夹具刻意让**来源单号与内置单号不同**（`source_order_no` 非空 ⇒ `effective_source_order_no` 取它）：
        #: 两个号同值时，「改单那一发取的是哪个字段」这条判据**恒真**（⛔ 那种夹具等于没测）。
        self.order.server = GameServer.objects.create(game=self.game, name="合成区服名乙")
        self.order.source_order_no = "TT-SYNTHETIC-PROJ-0001"
        self.order.save(update_fields=["server", "source_order_no"])
        self.queue_launch()
        first = self.run_a_round()
        self.assertEqual((first.done, first.failed), (1, 0))
        create_body = self.upstream.created[0]
        #: 非空哨兵：真要两边的值**不同**才有信息（同值时"取值口不同"也绿）。
        self.assertEqual(create_body["gameArea"], "合成区服名乙")
        self.assertNotEqual(create_body["souresOrderID"], self.order.order_no)

        #: ── 改一次「接手项目」⇒ 那一发要发 ModifyOrder ─────────────────────────────
        self._set_project(PROJECT_NEW_LITERAL)
        self.assertEqual(self._queue_project(), 1)
        second = self.run_a_round()
        self.assertEqual((second.done, second.failed), (1, 0))

        body = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        #: ⭐⭐ 两格**逐字相等**（同一个订单、同一个出处）。
        self.assertEqual(body["gameArea"], create_body["gameArea"])
        self.assertEqual(body["souresOrderID"], create_body["souresOrderID"])
        #: ⚠️ 钉**值**（⛔ 不写自指断言；`合成区服名乙` 是夹具给的字面量）。
        self.assertEqual(body["gameArea"], "合成区服名乙")

    def test_the_game_name_cell_is_the_very_same_one_the_create_body_sends(self):
        """⭐⭐ 判据（`gameName` 那一格的**来源**）：改单与建单**同一个出处**，且**非空**。
        ⚠️ 为什么必须有它：`ModifyOrder` 的 `gameName` 是**必填**，而「我方游戏名能不能直接当对方的 `gameName`」是本任务点名要核的一格（同类事故 = 建单的 `gameArea` 发空串被拒，`GAME_AREA_DEFAULT`）。判定口径 = **与②式建单发出去的那一格逐字同源**（建单那一格在真机上被对方接受过：录单成功），⛔ 不是"看起来合理"。
        判别力（两条都能红）：把 `write_project` 里的 `_game_name(order)` 换成 `order.game_id` / `""` ⇒ 第一条断言红；把建单那一格换掉而这里不换 ⇒ 同样红。"""
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        self._set_project(PROJECT_NEW_LITERAL)
        self._queue_project()

        self.run_a_round()

        body = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        #: ⭐ 与建单那一格**逐字相等**（同一个订单、同一个绑定 ⇒ 两处必须同源）。
        create_body = jidanpu_member_openapi_outbox._create_body(
            order=self.order, binding=self.binding,
            source_order_no=self.source_order_no(), in_hall=False, price_text="50.00",
        )
        self.assertEqual(body["gameName"], create_body["game_name"])
        #: ⭐ 而且它就是我方订单上那款游戏的名字（**字面值**，⛔ 不从被测常量派生）——
        #: `_OutboxCase.setUp` 建的游戏叫「测试游戏丙」。
        self.assertEqual(body["gameName"], "测试游戏丙")
        self.assertNotEqual(body["gameName"], "")

    def test_a_rejected_project_change_is_failed_with_the_peer_words(self):
        """⭐ 判据三：**写接口失败**（失败信封）⇒ `failed` + 对方原话，⛔ 不收敛 `done`、⛔ 不静默。
        ⚠️ 用 `reject_project`（**不动现场**、只回一个失败码）：照 E-0087 那条——写接口的失败信封必须能红，而"假成功"（`code=0` + 非空 `msg`）正是原始坑的形态。
        判别力：把 `write_project` 的 `except OpenApiError` 去掉（或改成吞掉不抛）⇒ 本用例红（那一发会被判成 `done`，而对方一个字都没写）。"""
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        self.upstream.reject_project(code=0, msg="游戏名称不存在")
        self._set_project(PROJECT_NEW_LITERAL)
        self._queue_project()

        stats = self.run_a_round()

        self.assertEqual((stats.done, stats.failed), (0, 1))
        event = self.db_out_events(kind="member.project").get()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        #: ⭐ 口径句 + **对方原话**（运维在下家系统里没有我们的日志 ⇒ 那句原话是唯一线索）。
        self.assertIn(jidanpu_member.PROJECT_FAILED_REASON, event.reason)
        self.assertIn("游戏名称不存在", event.reason)
        #: ⛔ 对方那一格一个字节都没变（失败绝不能被当成成功）。
        self.assertEqual(self.upstream.current_project(JID), PROJECT_OLD_LITERAL)

    def test_a_network_failure_on_the_project_write_is_failed_and_backs_off(self):
        """⭐ 判据三（另一半）：**网络失败** ⇒ 同样 `failed`；且**下一轮在退避窗内不重打**。
        判别力：把 `_retry` 换成 `_skip`（或 `_finish(done)`）⇒ 本用例红（那一档会静默丢一次「该发生的同步」，而且不重试）。
        """
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        self.upstream.boom(MODIFY_ORDER_PATH, httpx.ConnectError("合成断连"))
        self._set_project(PROJECT_NEW_LITERAL)
        self._queue_project()

        first = self.run_a_round()
        self.assertEqual(first.failed, 1)
        event = self.db_out_events(kind="member.project").get()
        self.assertEqual((event.status, event.attempts), ("failed", 1))

        # ── 第二轮：**在退避窗内** ⇒ 一次上游都不再打（`deferred`，⛔ 不是又失败一次）──
        calls_after_first = len(self.upstream.requests)
        second = self.run_a_round()
        self.assertEqual((second.deferred, second.failed), (1, 0))
        self.assertEqual(len(self.upstream.requests), calls_after_first)

        # ── 第三轮：把时钟推过退避窗（失败过一次 ⇒ `2^1 × 60` 秒）⇒ 真的再打一次 ──
        third = self.run_a_round(now=timezone.now() + timedelta(seconds=180))
        self.assertEqual(third.failed, 1)
        event = self.db_out_events(kind="member.project").get()
        self.assertEqual((event.status, event.attempts), ("failed", 2))

    def test_no_link_is_a_skip_without_any_upstream(self):
        """没有链路 ⇒ 终态跳过 + 人话（照改价那一档；⛔ 一次上游都不打）。
        ⚠️ **事件是手搓的**（`queue_member_event` 直调）：排队口 `queue_project_update` 自己就有「没有链路 ⇒ 0 条」那道闸（⛔ 不排空转行，判据在 `tests/test_jidanpu_member_triggers.py::ProjectEditTests`）⇒ 走它**永远到不了**这一档。这里要钉的是**消费侧**的兜底：链路在「排事件之后、消费之前」没了（删单清理 / 换绑）时，必须终态跳过 + 一句人话，⛔ 不是拿一个空的远端单号去乱打。照 `PriceTests` 同名用例的既有做法（同一条口径）。
        判别力：把 `_link_for` 那一段判据删掉 ⇒ 本用例红（会拿不到远端单号去乱打 / 直接炸）。"""
        self.enable()
        self._set_project(PROJECT_NEW_LITERAL)
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PROJECT, rearm=True,
        )

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        self.assertEqual(
            self.db_events(kind="member.project").get().reason,
            jidanpu_member.PROJECT_NO_LINK_REASON,
        )
        self.assertEqual(self.upstream.requests, [])

    def test_an_empty_project_name_is_a_skip_without_any_upstream(self):
        """⭐ 项目名是空的 ⇒ 终态跳过 + 人话（⛔ 不把空串发给一个必填格、白烧额度）。
        ⚠️ 真实可达性很低（编辑保存那条路上项目名是必填、②式建单也必填），但它是**兜底**：历史行 / 别的写入路径把这一列留空时，**必须留一句人话**而不是发一个空串过去（对方那一格必填 ⇒ 每一轮都会被拒 + 退避烧额度）。
        判别力：把 `PROJECT_NO_PROJECT_REASON` 那一支删掉 ⇒ 本用例红（会真打上游，读数里出现 token 与 `ModifyOrder`）。"""
        self.enable()
        self._link(in_hall=False)
        self._set_project("")
        self._queue_project()

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        event = self.db_events(kind="member.project").get()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, jidanpu_member.PROJECT_NO_PROJECT_REASON)
        self.assertEqual(self.upstream.requests, [])

    # ── ⭐⭐ 红队 I3：空**游戏名**与空项目名**同一档处置** ─────────────

    def test_an_empty_game_name_is_a_terminal_skip_without_any_upstream(self):
        """⭐⭐ 判据（红队 I3）：游戏名是空的 ⇒ **终态 `skipped` + 人话 + 一次上游都不打**。
        ⚠️ 为什么它必须与空项目名**对称**：改项目那一发的两格（`project` ＋ `gameName`）在对方那边**都是必填** ⇒ 空值的后果**完全一样**（必填格收到空值 ⇒ 对方拒 ⇒ 这一行按退避表反复重试、直到烧完额度进 `abandoned`）。⚠️ **单测原来抓不到它**：假上游不校验 `gameName`（E-0227 那一族「假上游比真接口宽松」）⇒ 那一档在真机上悄悄烂掉。⚠️ 夹具形态：`Game.name` 置成空串（列上没有非空名约束 ⇒ 历史行 / 手造行能长这样；⛔ 不是 `order.game_id is None`——那一列是 NOT NULL 外键，压根到不了，见 `jidanpu_member.order_game_name` 的 docstring「改准」那一段）。
        判别力（两条都能红）：① 把 `_consume_project` 里那段「游戏名是空的」删掉 ⇒ 本用例红：那时这一发会**真的打上游**（读数里出现取 token ＋ `ModifyOrder`），而事件会以 `done` 收口——＝「对方必填格收到空值」被我们当成办完了；② 把这一档记成 `failed`（而不是 `skipped`）⇒ 状态断言红（＝又回到烧额度那条老路）。"""
        self.enable()
        self._link(in_hall=False)
        self._set_project(PROJECT_NEW_LITERAL)
        #: ⚠️ 走 `update()`（绕过模型校验）—— 这一档本来就是**手造行 / 历史行**的兜底。
        Game.objects.filter(pk=self.game.pk).update(name="")
        self._queue_project()

        stats = self.run_a_round()

        self.assertEqual(stats.skipped, 1)
        event = self.db_events(kind="member.project").get()
        self.assertEqual(event.status, "skipped")
        #: ⭐ 人话（钉**值**：字面子串 ＋ 常量本身两条，⛔ 不写自指断言）。
        self.assertIn("游戏名是空的", event.reason)
        self.assertEqual(event.reason, jidanpu_member.PROJECT_NO_GAME_NAME_REASON)
        #: ⛔ **一次上游都不打**（连取 token 那一跳都不许有）：空值发出去只会白烧额度。
        self.assertEqual(self.upstream.requests, [])
        #: ⛔ 终态（不按退避表反复重试）。
        self.assertEqual(event.attempts, 0)

    # ── ⭐⭐ 红队 I1：出向那一格**必须裁剪**（列宽放宽之后也不能变长）──────

    def test_the_outbound_project_width_is_seven_hundred_bytes(self):
        """⭐ 判据（红队 I1 钉**值** + E-0242 改准）：出向「项目名称」那一格的宽度就是 **700 字节**。
        ⚠️ 为什么必须钉这个数（字面量，⛔ 不写 `模块.常量 == 模块.常量`）：另一条活跃分支要把 `Order.project_text` 的列宽从 300 放宽到 900（**D-0218**）——如果谁把出向这一格改成「跟着模型列宽走」，这个断言**当场红**。⚠️ E-0242：对方实测上限 ≈**732 字节**（成功 438 张全部 ≤732B、失败全部 ≥734B）⇒ 按**字符** 300 裁的旧口径（最多 900 字节）会被拒——现在按**字节** 700 裁（前端 700 字节闸同款）。
        判别力：把常量改回 300 字符口径（或去掉裁剪、或改成跟着列宽走）⇒ 本用例红。"""
        #: 350 个汉字（1050 字节 > 700）：裁完 233 个汉字（699 字节 ≤ 700），第 234 个字的
        #: 残字节被边界安全丢弃——⛔ 不多不少。
        self.assertEqual(
            jidanpu_member_openapi_outbox._clip_project("长" * 350), "长" * 233
        )
        #: 边界两档：699 字节原样过；702 字节裁到 699（多出的那个字整体丢弃）。
        self.assertEqual(
            jidanpu_member_openapi_outbox._clip_project("长" * 233), "长" * 233
        )
        self.assertEqual(
            jidanpu_member_openapi_outbox._clip_project("长" * 234), "长" * 233
        )
        #: ASCII 不受多字节影响：800 字节裁到恰好 700。
        self.assertEqual(
            jidanpu_member_openapi_outbox._clip_project("a" * 800), "a" * 700
        )
        #: ⛔ 短值**一个字节都不动**（裁剪不许顺手 strip / 归一化）。
        self.assertEqual(
            jidanpu_member_openapi_outbox._clip_project("合成项目丙"), "合成项目丙"
        )
        self.assertEqual(
            jidanpu_member_openapi_outbox.PROJECT_TEXT_OUTBOUND_MAX_BYTES, 700
        )

    def test_an_over_long_project_is_clipped_in_both_bodies_and_still_succeeds(self):
        """⭐⭐ 判据（红队 I1，端到端）：**超宽**的项目名 ⇒ 两个请求体里都被裁到宽度、且**仍然成功**。
        ⚠️ 为什么用 `mock.patch.object` 把宽度**压到 8 字节**：`Order.project_text` 这一列今天就是 300 字符宽 ⇒ 本地**造不出** 700 字节以上的值（列宽放宽是 D-0218 那条线的事）。压小宽度是把「超出宽度」这一档**变成可造**，判据仍然打在**真正发出去的请求体**上（⛔ 不是调一次 `_clip_project` 就算）。判别力（三条都能红）：① 把 `write_project` 里的 `_clip_project` 去掉 ⇒ 改单那一格是**未裁的 20 字** ⇒ 红；② 把 `_create_body` 里的 `_clip_project` 去掉 ⇒ 建单那一格是未裁的整串 ⇒ 红（⛔ 不许只裁一边：那会让"录单能过的项目名，改单过不去"）；③ 把裁剪改成判失败 / 跳过 ⇒ 下面那条 `done` 断言红（**裁剪之后必须仍然成功**）。
        """
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        #: 20 个字符（60 字节）的合成项目名（压到 8 **字节**＝恰好「长名」两字，「长」字的
        #: 残字节被边界丢弃——字节裁的边界行为顺带被这一条端到端覆盖）。
        long_project = "长名" * 10
        self._set_project(long_project)
        self._queue_project()

        # ⭐ 审计批 D1：常量与 `_clip_project` 的**唯一出处**已搬到
        #    `jidanpu_common`（①式表单共用同一把剪刀）⇒ 补丁要打在**定义它的那个模块**上
        #    （`_clip_project` 读的是自己模块的全局，打在 outbox 的别名上打不中——E-0029 同族）。
        with mock.patch.object(jidanpu_common, "PROJECT_TEXT_OUTBOUND_MAX_BYTES", 8):
            stats = self.run_a_round()
            #: ⚠️ 建单那一发的入参必须**重查**订单：`_set_project` 走的是 `.update()`
            #: ⇒ 内存里那份 `self.order` 还停在旧值上（拿它算 ⇒ 读数是夹具的旧值，不是被测行为）。
            self.order.refresh_from_db()
            create_body = jidanpu_member_openapi_outbox._create_body(
                order=self.order, binding=self.binding,
                source_order_no=self.source_order_no(), in_hall=False, price_text="50.00",
            )

        #: ⭐ **裁剪之后仍然成功**（⛔ 不是失败、⛔ 不是跳过）。
        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        #: ⭐ 改单那一发：请求体里那一格**恰好**前 8 字节＝「长名」两字、第三字残字节被丢
        #: （字面量，⛔ 不从常量派生）。
        self.assertEqual(self.upstream.json_calls(MODIFY_ORDER_PATH)[0]["project"], "长名")
        #: ⭐ 建单那一发（同一个订单、同一个出处）：**逐字同长**。
        self.assertEqual(create_body["project"], "长名")
        #: ⭐ 对方那一格真的被改成了**裁过的**值（读数取自假上游自己的账本）。
        self.assertEqual(self.upstream.current_project(JID), "长名")
        #: ⚠️ 端点集合**恰好**（裁剪**没有**多打一次上游、也没有回读那一跳）。
        self.assertEqual(self.upstream.all_pairs(), [
            ("POST", TOKEN_PATH),
            ("POST", MODIFY_ORDER_PATH),
        ])

    # ── ⭐ F-02（第五轮审计）：备注两格**同过同一把剪刀**────────────
    # ⚠️ `Order.remark` 是**无列宽**的 TextField（与 300 字符的 `project_text` 不同）
    #    ⇒ 本地**造得出** 700 字节以上的值，⛔ 不需要压常量（mock 那一手是 project 专用）。

    def test_an_over_long_remark_is_clipped_in_the_create_body(self):
        """⭐ F-02·建单那一发（`:662` 的 `_shared_body_cells`）：>732B 中文备注**裁后 ≤700B 发出**。
        250 个汉字 = 750 字节（> 对方实测上限 ≈732、> 我方出向闸 700）⇒ 裁到 233 个汉字 = 699 字节（第 234 字的残字节边界丢弃）。判据打在**真正要发出的参数表**上。
        判别力：把 `_shared_body_cells` 里的 `_clip_project` 摘掉（回 `order.remark or ""`）⇒ `body["remark"]` 是未裁的 750 字节 ⇒ 本用例红。"""
        Order.objects.filter(pk=self.order.pk).update(remark="备" * 250)
        self.order.refresh_from_db()
        body = jidanpu_member_openapi_outbox._create_body(
            order=self.order, binding=self.binding,
            source_order_no=self.source_order_no(), in_hall=False, price_text="50.00",
        )
        self.assertEqual(body["remark"], "备" * 233)
        self.assertLessEqual(len(body["remark"].encode("utf-8")), 700)

        #: ≤700B 的备注**原样**（裁剪不许顺手 strip / 归一化）。
        Order.objects.filter(pk=self.order.pk).update(remark="合成备注甲")
        self.order.refresh_from_db()
        short_body = jidanpu_member_openapi_outbox._create_body(
            order=self.order, binding=self.binding,
            source_order_no=self.source_order_no(), in_hall=False, price_text="50.00",
        )
        self.assertEqual(short_body["remark"], "合成备注甲")

    def test_an_over_long_remark_is_clipped_in_the_modify_body_and_still_succeeds(self):
        """⭐ F-02·改项目那一发（`:1203` 的 `orderRemark`）：裁后 ≤700B 发出、**裁剪之后仍然 done**。
        ⚠️ `orderRemark` 是对方**真的会写**的那一格（`MODIFY_ORDER_PAYLOAD_CELLS` 实测）⇒ 不裁的长备注整发可能被对方按字节拒 → `failed` → 退避 → `abandoned`（E-0242 当天 6 张卡单同形，只是格子从 `project` 换成备注）⇒ 「仍然 done」是承重判据（⛔ 不是「裁了就行」）。`remark` 与 `orderRemark` 同源 ⇒ 两格一起断。
        判别力：把 `_modify_project_cells` 里的 `_clip_project` 摘掉 ⇒ 两格都是未裁的 750 字节 ⇒ 本用例红；把裁剪改成判失败 / 跳过 ⇒ `done` 那条断言红。"""
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        Order.objects.filter(pk=self.order.pk).update(remark="备" * 250)
        self._queue_project()

        stats = self.run_a_round()

        #: ⭐ **裁剪之后仍然成功**（⛔ 不是失败、⛔ 不是跳过）。
        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        body = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        self.assertEqual(body["orderRemark"], "备" * 233)
        self.assertEqual(body["remark"], "备" * 233)
        self.assertLessEqual(len(body["orderRemark"].encode("utf-8")), 700)

        #: ≤700B 的备注**原样**：第二轮短备注逐字发出（同一条真实出向路径）。
        Order.objects.filter(pk=self.order.pk).update(remark="合成备注甲")
        self._queue_project()
        stats = self.run_a_round()
        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        body = self.upstream.json_calls(MODIFY_ORDER_PATH)[1]
        self.assertEqual(body["orderRemark"], "合成备注甲")
        self.assertEqual(body["remark"], "合成备注甲")

    def test_a_write_only_confirmation_without_human_words_raises_loudly(self):
        """⭐ 配了「没有可回读的那一格」却**没给人话** ⇒ 响亮抛（⛔ 不许"什么都不确认就 done"）。
        判别力：把 `_consume_project` 里那句 `read_confirm_unavailable_reason` 的空值检查删掉 ⇒ 本用例红（那时台账上会出现一条**说不清口径**的完成）。
        """
        base = jidanpu_member_openapi_outbox.member_openapi_outbox_line()
        self.assertIsNotNone(base.project)
        mute = jidanpu_member.OutboxLine(
            name=base.name, spawn_client=base.spawn_client, status_push=base.status_push,
            record=base.record, price=base.price, withdraw=base.withdraw,
            project=jidanpu_member.ProjectPrimitives(
                write_project=base.project.write_project,
                read_confirm_unavailable_reason="",
            ),
            unsupported=base.unsupported,
        )
        self.enable()
        self._link(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        self._set_project(PROJECT_NEW_LITERAL)
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_PROJECT, rearm=True,
        )
        with self.assertRaises(ValueError):
            jidanpu_member._consume_project(
                event, binding=self.binding, client=_StubProjectClient(), http=None,
                now=timezone.now(), line=mute,
            )


# ══════════════════════════════════════════════════════════════════════════════
# ⑪·二 ⭐⭐ 真机第二现场：`ModifyOrder` 是**整单覆盖** ⇒ 必须发完整载荷
# ══════════════════════════════════════════════════════════════════════════════

# ⚠️ 现场：只发那 5 格 ⇒ 对方回 `code=1 保存成功`，而**没发的格被清成默认值**（真机实测 `orderSoure` → `'其它'`、`hours` 8 → 720、`originPrice` 33.33 → 0.0；业务单上确认了前两格被改坏）。逐格读数与取值来源见被测模块的 `MODIFY_ORDER_PAYLOAD_CELLS`（那张表是**唯一**出处）。
# ⛔ 判据的形状（三层，各自能红）：① 请求体的键集合**恰好** = 那张表里标「必发」的那一串（多一格 / 少一格都红）；② ⭐⭐ **逐格**把「真的发出去的那一份载荷」复制后摘掉一格 ⇒ 假上游**照对方原话**判失败；③ 那条「必发格」清单在测试里只有一份**副本**，副本与模块漂了要红。

#: 真机 **`ModifyOrder` 缺格时的原话**（`code=0` 是失败档）。⚠️ **证据级别分开登记**（⛔ 别混成一句）：
#: `系统要求来源单号不能为空` / `游戏大区为必填项` / `系统单号和来源单号不可同时为空` 三句是**真机实测原话**（那三发逐格探测）；
#: 其余几句按**文档「必需」那一栏 / 同一族的措辞**写（真机上**没有**逐格摘掉探过——对**整单覆盖**这个性质来说，缺格的后果已经由「没发的格被清成默认值」这条**实测**结论覆盖）。
MODIFY_ORDER_MISSING_CELL_WORDS = {
    "orderID": "系统单号和来源单号不可同时为空",
    "souresOrderID": "系统要求来源单号不能为空",
    "gameName": "游戏名称为必填项",
    "gameArea": "游戏大区为必填项",
    "project": "代练项目为必填项",
    "hours": "时间要求为必填项",
    "orderSoures": "订单来源为必填项",
    "price": "订单总价为必填项",
    "originPrice": "原价为必填项",
    "customerId": "会员号不可为空",
}

#: ⭐ 「**键必须在**，但值可以是空串」的那两格（备注类）——⛔ **不放进上面那张表**：备注**空是合法取值**（我方 `order.remark` 常常就是空串），一个把空串也判成"缺格"的桩会把每一发都拒掉（那会让所有用例以「假上游太严」的形式假红）。
#: ⚠️ 但「**键在不在**」照旧要能红：这两格的键由 `ProjectPayloadTests` 的「请求体恰好是那 12 格」那条钉住（⛔ 不是靠本桩）。
MODIFY_ORDER_EMPTY_ALLOWED_CELLS = ("remark", "orderRemark")


class _StrictModifyOrderUpstream(_WriteUpstream):
    """假上游**照真接口收紧**：`ModifyOrder` 少任何一格**必发格** ⇒ 照对方原话回失败。
    ⚠️ 为什么另立一个类（⛔ 不是就地改 `_WriteUpstream`）：既有那些用例的**判据焦点**不是「缺格」这件事，把收紧塞进那一份会让它们的失败原因变成**两条混在一起**（「对方那一格真的变了吗」与「这一发缺格了吗」）。
    ⭐ 而本类**继承**它 ⇒ 记录 / 路由 / 取 token / 现场账本那些口径**仍然只有一份**（`AGENTS.md` §5-C：多份桩必然漂）。"""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == MODIFY_ORDER_PATH and path not in self.routes:
            body = _json_body(request) or {}
            for cell, words in MODIFY_ORDER_MISSING_CELL_WORDS.items():
                value = body.get(cell)
                if value is None or not str(value).strip():
                    self._record(request)
                    return httpx.Response(200, json={"code": 0, "msg": words, "data": None})
        return super().handler(request)


class ProjectPayloadTests(_WriteCase):
    """⭐ 请求体**恰好**是那张表里标「必发」的那一串 + 缺任何一格都要红。"""

    upstream_factory = _StrictModifyOrderUpstream

    #: 「必发格」的**字面量**副本（⛔ 不从被测模块派生：自指断言在被断言的常量被改坏时不会红，
    #: E-0024）。⚠️ 它必须与被测模块那张表**逐格一致**——两边不一致时由
    #: `test_the_required_cell_table_matches_the_module` 当场红。
    REQUIRED_CELL_LITERALS = (
        "orderID", "souresOrderID", "gameName", "gameArea", "project",
        "hours", "orderSoures", "originPrice", "price", "customerId",
        "remark", "orderRemark",
    )

    def _link_row(self, *, in_hall=False, remote_id=JID):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    def _run_once(self):
        """把 `member.project` 那一档**真的跑一发**（⛔ 不在测试里手搓请求体）。"""
        self.enable()
        self._link_row(in_hall=False)
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        Order.objects.filter(pk=self.order.pk).update(project_text=PROJECT_NEW_LITERAL)
        self.assertEqual(jidanpu_member.queue_project_update(self.order), 1)
        return self.run_a_round()

    def test_the_required_cell_table_matches_the_module(self):
        """⭐ 那条「必发格」清单**只有一处**（本文件那份是副本）。
        判别力：往被测模块那张表里加一格（或删一格）而不改这里 ⇒ 本用例红（⛔ 这就是「第二份清单」的守卫：清单漂了必须响）。
        """
        self.assertEqual(
            tuple(jidanpu_member_openapi_outbox.MODIFY_ORDER_REQUIRED_CELLS),
            self.REQUIRED_CELL_LITERALS,
        )

    def test_the_project_push_sends_exactly_the_required_cells(self):
        """⭐⭐ 判据（整单覆盖那条的正面）：请求体**恰好**是那 12 格，一格不多一格不少。
        ⚠️ 为什么是「恰好」而不是「至少包含」：`ModifyOrder` 是**整单覆盖** ⇒ ① **少一格** = 对方把那一格清成默认值（真机实测：`orderSoure` → `'其它'`、`hours` → `720`、`originPrice` → `0.0`）；② **多一格** = 我们多改了一格（每一格都要说清为什么要发，见那张表）。两个方向**各自能红**。
        """
        stats = self._run_once()

        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        body = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        self.assertEqual(set(body), set(self.REQUIRED_CELL_LITERALS))
        #: ⭐ 钉**值**（不是只钉键在不在）—— 每一格的取值口那一句写在被测模块那张表里。
        self.assertEqual(body["orderID"], JID)
        self.assertEqual(body["souresOrderID"], self.source_order_no())
        self.assertEqual(body["project"], PROJECT_NEW_LITERAL)
        self.assertEqual(body["gameName"], "测试游戏丙")
        self.assertEqual(body["gameArea"], "官方服")
        self.assertEqual(body["hours"], 8)
        self.assertEqual(body["orderSoures"], SOURCE_NAME)
        self.assertEqual(body["customerId"], "0")
        #: ⭐⭐ 两个价格格都是**发单价**（`hall_price` ＝ 50.00），⛔ 绝不是客户价（100.00）。
        self.assertEqual(Decimal(str(body["price"])), Decimal("50.00"))
        self.assertEqual(Decimal(str(body["originPrice"])), Decimal("50.00"))
        self.assertNotIn("100.00", repr(body))
        self.assertEqual(body["remark"], "")
        self.assertEqual(body["orderRemark"], "")
        #: ⛔ 那两格「大厅类」**不发**（实测发了也无效、且不影响 `isThrowOrder`）。
        self.assertNotIn("recUser", body)
        self.assertNotIn("assignPrice", body)
        #: ⭐ 对方那一格真的变了（读数取自假上游自己的账本）。
        self.assertEqual(self.upstream.current_project(JID), PROJECT_NEW_LITERAL)

    def test_every_required_cell_is_either_pinned_by_upstream_or_by_the_exact_key_set(self):
        """⭐ 自证：那条「必发格」清单里的**每一格**都至少有一条能红的判据在守它。
        ⚠️ 为什么必须有这一条：清单里有 12 格，而「照对方原话判失败」那一档只覆盖**值非空**的那 10 格（备注两格**空是合法取值** ⇒ 桩不能把空串判成缺格）⇒ 剩下两格只能靠「请求体**恰好**是那 12 格」那条钉住（键少了就红）。本用例把这件事**显式**说出来——⛔ 不然「哪几格其实没人守」这件事是看不出来的（而那正是「清单看起来被守住了」的假象）。
        判别力：把 `MODIFY_ORDER_REQUIRED_CELLS` 里的任一格挪走而不调整这两条判据 ⇒ 本用例红（它会说出「这一格既不在桩的严格表里、也不在允许空值的那两格里」）。"""
        strict = set(MODIFY_ORDER_MISSING_CELL_WORDS)
        empty_ok = set(MODIFY_ORDER_EMPTY_ALLOWED_CELLS)
        self.assertEqual(
            set(self.REQUIRED_CELL_LITERALS), strict | empty_ok,
        )
        #: ⛔ 两边不许重叠（重叠意味着同一格既能"必须非空"又"允许空"，那是自相矛盾的桩）。
        self.assertEqual(strict & empty_ok, set())

    def test_dropping_any_required_cell_makes_the_real_endpoint_reject_it(self):
        """⭐⭐ 判据（**逐格**能红）：从真实载荷里摘掉**任何一格**必发格 ⇒ 对方拒、当发失败。
        ⚠️ 手法：把 `write_project` 真的发出去的那一份载荷**复制后删一格**，再把**这份线上体原样直发**（`client._write`：真实 POST 出口 ⇒ 照样过假上游的缺格处理；⛔ 不去改被测代码、⛔ 也不手搓一份「我以为会发什么」的载荷）⇒ 它测的是「**真的发出去了的那一份**少了这一格会怎样」。⚠️ 为什么不走 `client.modify_order(...)`：`gameName` / `gameArea` / `project` 是它签名的**必传形参**——「摘掉这一格」在签名路上**根本构造不出来**；直发线上体才能**逐格**探（接手会话修正；原稿走签名路 ⇒ 三个 subTest `KeyError`）。⚠️ **本条判据的边界**（如实登记）：它证的是「假上游按真接口的必填口径拒了它」，⛔ 不是「真机会拒」——真机上单独摘掉 `hours` 之类的**没探过**（见 `MODIFY_ORDER_MISSING_CELL_WORDS` 的注释）。
        """
        stats = self._run_once()
        self.assertEqual(stats.done, 1)
        full = self.upstream.json_calls(MODIFY_ORDER_PATH)[0]
        self.assertEqual(set(full), set(self.REQUIRED_CELL_LITERALS))

        with jidanpu_openapi.openapi_client(self.binding) as client:
            #: ⚠️ 只逐格探**值必须非空**的那 10 格（备注两格空是合法取值 ⇒ 见
            #: `MODIFY_ORDER_EMPTY_ALLOWED_CELLS`；它们的「键在不在」由上面那条恰好键集合钉住）。
            for cell in MODIFY_ORDER_MISSING_CELL_WORDS:
                with self.subTest(cell=cell):
                    trimmed = {k: v for k, v in full.items() if k != cell}
                    self.assertNotIn(cell, trimmed)
                    with self.assertRaises(OpenApiError) as caught:
                        client._write(
                            MODIFY_ORDER_PATH,
                            body=trimmed,
                            step=jidanpu_openapi.STEP_MODIFY_ORDER,
                        )
                    #: ⭐ 照**对方原话**判失败（逐字），⛔ 不是只看「抛了个异常」。
                    self.assertEqual(caught.exception.upstream_code, 0)
                    self.assertEqual(
                        caught.exception.upstream_msg,
                        MODIFY_ORDER_MISSING_CELL_WORDS[cell],
                    )

    def test_a_clashing_extra_key_raises_loudly(self):
        """⭐ 判据：`extra` 想覆盖那 5 格之一 ⇒ **当场响亮抛**（⛔ 不静默覆盖）。
        判别力：把 `modify_order` 里那段撞键检查删掉 ⇒ 本用例红（那时 `extra` 会**悄悄改掉** `gameName` / `project` 这类各有判据的格）。
        """
        with jidanpu_openapi.openapi_client(self.binding) as client:
            with self.assertRaises(ValueError):
                client.modify_order(
                    game_name="测试游戏丙", game_area="官方服", project="合成项目丁（新）",
                    order_id=JID, source_order_id=self.source_order_no(),
                    extra={"project": "偷改的项目名"},
                )


# ══════════════════════════════════════════════════════════════════════════════
# ⑫ ⭐⭐ 红队 I2：三句**弱确认**人话里必须有「拆单」那半句
# ══════════════════════════════════════════════════════════════════════════════

# ⚠️ 背景：对方把一单拆成多张时，链路里只存得下**第一张**的系统订单号（口径见 `SPLIT_ORDER_REASON`）⇒ 之后每一发（改价 / 改项目 / 改状态）都只打在**第一张**上：写成功、`done`、弱确认人话也写了，而**其余几张那一格根本没被改**——那些 `done` 行在台账上看起来像"这一件事办完了" ⇒ 不提拆单就是**静默少做**。
# ⚠️ 判据打在**字面子串**上（⛔ 不写 `模块.SPLIT_ORDER_CAVEAT in 模块.常量` 那种自指断言，E-0024：自指断言在被断言的常量被改坏时**不会红**）。

#: 那半句里必须出现的**字面**要点（⛔ 不从被测模块派生）。
SPLIT_CAVEAT_LITERALS = (
    "拆成了多张系统订单",
    "只认第一张",
    "其余几张不会被改",
    "请人工核对",
)


class SplitOrderCaveatTests(_WriteCase):
    """那三句弱确认人话 ＋ 真实台账行都必须带上「拆单」那半句。"""

    def test_all_three_weak_confirmation_lines_carry_the_split_caveat(self):
        """⭐ 判据（三句都要有）：改价 / 大厅那一档 / 改项目那三句都点名了拆单。
        判别力（逐条能红）：把某一句末尾的 `+ SPLIT_ORDER_CAVEAT` 删掉 ⇒ 本用例红（那一句就回到"写成功＝办完了"的假象）；把半句本身删空 ⇒ 四条字面断言全红。
        """
        for name, phrase, original_literals in (
            ("PRICE_CONFIRM_WRITE_ONLY_REASON",
             jidanpu_member_openapi_outbox.PRICE_CONFIRM_WRITE_ONLY_REASON,
             ("成功码", "没有接单价那一格")),
            ("HALL_NOT_APPLICABLE_REASON",
             jidanpu_member_openapi_outbox.HALL_NOT_APPLICABLE_REASON,
             ("大厅",)),
            ("PROJECT_CONFIRM_WRITE_ONLY_REASON",
             jidanpu_member_openapi_outbox.PROJECT_CONFIRM_WRITE_ONLY_REASON,
             ("成功码", "没有项目名称那一格")),
        ):
            with self.subTest(phrase=name):
                #: 原句的要点**仍然在**（⛔ 不是"换了一句话"）。
                for literal in original_literals:
                    self.assertIn(literal, phrase, f"{name} 的原意被改掉了：{literal}")
                for literal in SPLIT_CAVEAT_LITERALS:
                    self.assertIn(literal, phrase, f"{name} 里没有拆单那半句：{literal}")

    def test_the_caveat_really_lands_on_the_ledger_row(self):
        """⭐⭐ 判据（打在**数据库行**上）：改项目跑一轮 ⇒ 那条 `done` 人话台账里真的有那半句。
        ⚠️ 与上一条**不是重复**：上一条钉的是常量文本（"写进去了没有"），这一条钉的是**台账行**（"运维在页面上真的看得到"）——常量写对了但没接进那一格时，只有这一条能红。
        判别力：把 `_MEMBER_OPENAPI_LINE.project.read_confirm_unavailable_reason` 换成别的句子 ⇒ 本用例红。"""
        self.enable()
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID, in_hall=False,
        )
        self.upstream.seed(
            source_id=self.source_order_no(), order_id=JID, status=LABEL_UNASSIGNED,
            project=PROJECT_OLD_LITERAL,
        )
        Order.objects.filter(pk=self.order.pk).update(project_text=PROJECT_NEW_LITERAL)
        jidanpu_member.queue_project_update(self.order)

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        note = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_PROJECT, target_ref=self.order.order_no,
        ).get()
        for literal in SPLIT_CAVEAT_LITERALS:
            self.assertIn(literal, note.reason)


# ══════════════════════════════════════════════════════════════════════════════
# ⑪ 步骤 3：A 段（派单传图）在②式下**还没通** ⇒ 一句人话，⛔ 不静默、⛔ 不炸
# ══════════════════════════════════════════════════════════════════════════════


class AssignScreenshotTests(_WriteCase):
    def test_a_ready_screenshot_yields_a_human_line_instead_of_crashing(self):
        """⭐ 判据：②式的 A 段（传图）是**步骤 6** ⇒ 此刻要留一句人话，⛔ 不静默、⛔ 不炸。
        ⚠️ 真机形状：这张单有一张 `ready` 的订单截图 ⇒ ①式那一份 A 段实现会因为「`http is None`（②式没有登录会话、也就没有上传页）」**响亮抛** ⇒ 整条派单事件会变成「未预期异常」。那条守卫在①式是对的（接线漏了），在②式**必须换成一句人话**。
        判别力：把 `push_screenshots=None` 那条路改成直接调①式那一份 ⇒ 本用例红（会抛 `ValueError` 而不是留台账）。"""
        self.enable()
        self.order = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.WAITING_GRAB)
        OrderScreenshot.objects.create(
            order=self.order, img_type=OrderScreenshot.IMG_ORDER,
            status=OrderScreenshot.STATUS_READY, object_key="合成/截图甲.png",
            uploaded_by=self.binding.user,
        )
        self.queue_assign()

        stats = self.run_a_round()

        # ⭐ 派单本身**照旧成功**（传图是附属，⛔ 不挡派单）：
        self.assertEqual(stats.done, 1)
        self.assertEqual(self.db_link().jidanpu_order_id, CLAIMED_ID)
        note = JidanpuSyncEvent.objects.filter(
            direction="in", kind=jidanpu_member.KIND_ASSIGN, target_ref=self.order.order_no,
        ).get()
        self.assertIn("截图", note.reason)
        self.assertIn("还没通", note.reason)

    def test_an_order_without_screenshots_gets_no_note_at_all(self):
        """⭐ 对照组：**没有**可传的图 ⇒ 连那条人话都不写（照①式「没有图 ⇒ 一次上游都不打」）。
        判别力：把「先判有没有图」那一跳删掉（每张派单都写一条人话）⇒ 本用例红。
        """
        self.enable()
        self.order = self.make_order(order_no=ORDER_NO_B, status=OrderStatus.WAITING_GRAB)
        self.queue_assign()

        stats = self.run_a_round()

        self.assertEqual(stats.done, 1)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                direction="in", kind=jidanpu_member.KIND_ASSIGN,
                target_ref=self.order.order_no,
            ).exists()
        )


# ══════════════════════════════════════════════════════════════════════════════
# ⑧ 接线（⭐ 二期**步骤 5**）：把出向发单接进**常驻同步进程**
# ══════════════════════════════════════════════════════════════════════════════

#: 跑一轮调度用的固定墙钟（⛔ 不用 `timezone.now()`：退避窗与健康列的读数要能对着字面值断言）。
SCHEDULE_NOW = datetime(2026, 10, 10, 9, 0, 0, tzinfo=dt_timezone.utc)


@contextmanager
def _fake_tenants(tenant, *, now):
    """把命令的租户遍历换成**只有这一个**租户（照 `test_jidanpu_member_openapi_consume` 的形状）。
    ⚠️ 只换租户遍历与墙钟——⛔ **不**把①②式任何一条路换成替身：本类要证明的正是
    「**真命令**跑一轮时那一段真的被调到、真的发得出去」。"""
    from apps.business.management.commands import jidanpu_sync as module

    @contextmanager
    def _noop_context(_tenant):
        yield

    objects = mock.MagicMock()
    objects.exclude.return_value.order_by.return_value = [tenant]
    patcher = mock.patch.object(module.Tenant, "objects", objects)
    #: 回调事件文件指到一个**不存在**的临时路径 ⇒ 回调消费那一段确定「零事件、零上游」
    #: （⛔ 不靠「默认路径恰好没有文件」——那是**环境**给的绿，换个机器就变）。
    events = mock.patch.dict(
        os.environ, {"CALLBACK_EVENTS_PATH": "/tmp/tt-jpw5-nonexistent-events.jsonl"},
    )
    with patcher, events, mock.patch.object(module, "tenant_context", _noop_context), \
         mock.patch.object(module.timezone, "now", return_value=now):
        yield


class ScheduleWiringTests(_OutboxCase):
    """⭐⭐ 步骤 5 的接线判据：**走真命令**（`jidanpu_sync --once`）跑一轮。
    ⚠️ 与上面那些用例的差别只有一个、但正是本步骤要证的那一件事：那些直接调 `run_member_status_outbox`（证明**这个函数**对），
    这里跑的是**常驻同步进程那一轮**（证明它**真的被接进去了**、且**开关关着时真的一个字节都不发**）。"""

    def setUp(self):
        super().setUp()
        #: 调度那两条间隔锚是**进程级**状态（照 `test_jidanpu_member_openapi_consume` 的同一条
        #: 做法逐用例清掉）：不清的话「这一轮跑没跑兜底对账」会由**上一个测试类的绑定主键**
        #: 决定 —— 那是**静默**的读数漂移，而本类要断言的正是「这一轮各段跑没跑」。
        _MEMBER_FAST_AT.clear()
        _MEMBER_RECONCILE_AT.clear()
        _MEMBER_STATUS_PULL_AT.clear()
        self.addCleanup(_MEMBER_FAST_AT.clear)
        self.addCleanup(_MEMBER_RECONCILE_AT.clear)
        self.addCleanup(_MEMBER_STATUS_PULL_AT.clear)

    def run_a_scheduled_round(self, *, expect_error=False):
        """跑**一轮常驻同步**（真命令）⇒ 回 `(CommandError | None, stdout, stderr)`。"""
        out, err = StringIO(), StringIO()
        caught = None
        tenant = mock.Mock(schema_name="tenant-openapi-outbox")
        with _fake_tenants(tenant, now=SCHEDULE_NOW):
            try:
                call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
            except CommandError as exc:
                caught = exc
        if expect_error:
            self.assertIsNotNone(caught, "本趟应当以非零退出码收尾，却没有抛 CommandError")
        else:
            self.assertIsNone(caught, f"本趟不该非零退出：{caught}")
        return caught, out.getvalue(), err.getvalue()

    def db_link(self):
        return JidanpuMemberOrderLink.objects.get(pk=self.link.pk)

    def test_the_scheduled_round_sends_nothing_while_the_switch_is_off(self):
        """⭐⭐ 判据（步骤 5）：**接线不等于开闸** —— 开关关着 ⇒ 零上游请求、订单 / 台账零变化。
        ⭐ **能红**：把 `OutboxLine.enabled` 那道闸摘掉（或让 `outbox_enabled()` 恒真）⇒ 桩上立刻出现 `(POST, /oauth/token)` 与 `ChangeOrderStatus`、台账那一行变 `done` ⇒ 下面四组断言全红（真跑过，读数见报告）。
        """
        self.queue_status_out()
        before_order = self.db_order()
        before_event = self.db_out_events().get()
        before_link = self.db_link()

        _caught, out, _err = self.run_a_scheduled_round()

        # ⭐ 一次上游都不打（⛔ 不是「只打了一点点」）——含取 token 那一跳
        self.assertEqual(self.upstream.requests, [], "开关关着，调度却打了上游")
        # ⭐ 订单 / 台账 / 链路三张表一个字节不变（断言**数据库行**，E-0009）
        order = self.db_order()
        self.assertEqual(
            (order.status, order.updated_at, order.hall_price),
            (before_order.status, before_order.updated_at, before_order.hall_price),
        )
        event = self.db_out_events().get(pk=before_event.pk)
        self.assertEqual(
            (event.status, event.attempts, event.reason),
            (before_event.status, before_event.attempts, before_event.reason),
        )
        link = self.db_link()
        self.assertEqual(
            (link.jidanpu_order_id, link.in_hall),
            (before_link.jidanpu_order_id, before_link.in_hall),
        )
        # ⭐ 但**要说清为什么没发**（⛔ 不静默）：那句人话由调度层打进 stdout
        #    （判据钉**运维真看得见的那一行**，⛔ 不是模块里那个常量对象——E-0024）。
        self.assertIn("JIDANPU_MEMBER_OPENAPI_OUTBOX", out)
        self.assertIn("默认关着", out)
        # ⚠️ 绑定的 `last_run_at` 照旧推进（那是**接线前就有的** ②式行为：记「上次跑」），
        #    但**不许**因为它就被当成「成功」：`last_ok_at` 与 `last_error` 都不动。
        fresh = self.db_binding()
        self.assertEqual(fresh.last_run_at, SCHEDULE_NOW)
        self.assertIsNone(fresh.last_ok_at)
        self.assertEqual(fresh.last_error, "")

    def test_the_scheduled_round_really_sends_a_pending_event(self):
        """⭐⭐ 判据（步骤 5）：开关**开** ⇒ 一条待发的②式台账事件**真的被发出去**。
        ⭐ 断言打在**数据库行**上（E-0009）：台账那一行的 `status` 变成 `done`（⛔ 不是「函数被调过」、⛔ 不是 logger）。
        ⭐ **能红**：把那一行接线删掉（改回「⛔ 不跑」）⇒ 台账停在 `pending`、远端一个字节没动 ⇒ 红。"""
        self.enable()
        self.queue_status_out()
        event = self.db_out_events().get()
        self.upstream.set_status(JID, LABEL_UNASSIGNED)  # 远端此刻「未分配」⇒ 该推「已退单」

        _caught, out, _err = self.run_a_scheduled_round()

        # ⭐ 台账那一行**真的** done（查库重取，⛔ 不读内存对象）
        fresh_event = self.db_out_events().get(pk=event.pk)
        self.assertEqual(fresh_event.status, "done", "②式那一轮跑完了，台账却还停在待发")
        # ⭐ 对方那边**真的**变了（假上游现场读数）+ 只打了该打的那一个写端点
        self.assertEqual(self.upstream.statuses[JID], LABEL_REFUNDED)
        self.assertEqual(self.upstream.write_pairs(), [("POST", CHANGE_ORDER_STATUS_PATH)])
        # ⭐ 健康列：`run_outbox` 自己盖了成功（这一轮真的办了事 + 没失败）
        self.assertEqual(self.db_binding().last_ok_at, SCHEDULE_NOW)
        # ⭐ 汇总里有出向发单那一段（⛔ 不是只有①②式别的段）
        self.assertIn("出向发单 完成 1", out)


class ScheduleFailureVisibilityTests(_OutboxCase):
    """⭐ plan §3 步骤 5 的判据：「②式行有 pending 事件时页面 / `last_error` 看得见」。"""

    def setUp(self):
        super().setUp()
        _MEMBER_FAST_AT.clear()
        _MEMBER_RECONCILE_AT.clear()
        _MEMBER_STATUS_PULL_AT.clear()
        self.addCleanup(_MEMBER_FAST_AT.clear)
        self.addCleanup(_MEMBER_RECONCILE_AT.clear)
        self.addCleanup(_MEMBER_STATUS_PULL_AT.clear)

    def test_a_failed_send_stays_visible_on_the_binding_after_the_round(self):
        """⭐ 发单那一档失败 ⇒ 那句话**留在**绑定的 `last_error` 上（⛔ 不被后面那次「盖成功」清掉）。
        ⚠️ 为什么必须单独钉：②式那一轮末尾有一次「盖成功」的机会（`did_something` + 没未预期异常）⇒ 少了「出向发单记了失败就不许盖成功」那一格，**`run_outbox` 刚写下的错会被当场抹掉**，而页面上看起来一切正常（那正是本仓最恨的「说假话」形态）。⚠️ 这一档是**业务失败**（远端拒绝了这次推），⛔ 不是「函数抛异常」——后者走逐段异常边界。
        """
        self.enable()
        self.queue_status_out()
        # 远端**拒绝**这次推状态：写接口那一跳断连 ⇒ 客户端翻成 `OpenApiError`（业务失败档，⛔ 不是未预期异常）⇒ 台账 `failed` + 绑定的 `last_error` 被写上。
        # ⚠️ 用 `boom`（⛔ 不用 `reply()`）：`ChangeOrderStatus` 由假上游按现场状态生成，
        # 挂固定回包会把「回读真的跟着变」那条读数变成一个死回包（本类自己就断言禁止）。
        self.upstream.boom(CHANGE_ORDER_STATUS_PATH, httpx.ConnectError("合成断连"))
        # 推前回读兜不过去：远端停在「未分配」（⛔ 不是目标态）
        self.upstream.set_status(JID, LABEL_UNASSIGNED)
        # 消费那一段也要「真的做了事」，否则末尾那次盖成功本来就不会发生（对照见下一条用例）
        consume = mock.Mock(return_value=jidanpu_openapi_consume.ConsumeOutcome(
            enabled=True, applied=1,
        ))

        with _fake_tenants(mock.Mock(schema_name="tenant-openapi-outbox"), now=SCHEDULE_NOW), \
             mock.patch.object(
                 jidanpu_member_openapi, "consume_member_callbacks", consume,
             ):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        fresh = self.db_binding()
        self.assertEqual(self.db_out_events().get().status, "failed")
        self.assertIn("仍失败", fresh.last_error)
        self.assertIn("改状态", fresh.last_error)
        self.assertIsNone(
            fresh.last_ok_at,
            "发单失败了却盖了成功 ⇒ `last_error` 被抹掉、页面上什么都看不见",
        )

    def test_a_clean_round_still_stamps_success(self):
        """对照（⛔ 不是多余）：**开关关着**（这一段按设计没跑）⇒ 照旧盖成功。
        ⚠️ 判据：`blocked_reason`（开关关着）**不算失败**——把它算进去的话，②式绑定在开闸前**永远**盖不上 `last_ok_at`（页面永远显示「从未成功」= 说假话）。
        """
        self.queue_status_out()
        consume = mock.Mock(return_value=jidanpu_openapi_consume.ConsumeOutcome(
            enabled=True, applied=1,
        ))
        with _fake_tenants(mock.Mock(schema_name="tenant-openapi-outbox"), now=SCHEDULE_NOW), \
             mock.patch.object(
                 jidanpu_member_openapi, "consume_member_callbacks", consume,
             ):
            call_command("jidanpu_sync", "--once", stdout=StringIO(), stderr=StringIO())

        fresh = self.db_binding()
        self.assertEqual(fresh.last_ok_at, SCHEDULE_NOW)
        self.assertEqual(fresh.last_error, "")
        # ⚠️ 但台账**照旧**停在待发（盖成功 ⛔ 不等于「发出去了」）
        self.assertEqual(self.db_out_events().get().status, "pending")


# ══════════════════════════════════════════════════════════════════════════════
# ⭐⭐ 假上游的「**查不到这张单**」信封：**逐字**对齐真机（实测三行）
# ══════════════════════════════════════════════════════════════════════════════


class AbsentOrderEnvelopeTests(_WriteCase):
    def test_the_absence_envelope_is_exactly_the_real_machine_reading(self):
        """⭐⭐ **恰好**是真机那三行里的第一行（多一格 / 少一格 / 改一个字 / `data` 换个类型都红）。
        ⚠️ 这条网存在的理由（**上一轮差点又栽在「假上游与真机不一致」**）：那版假上游回的是 `{"code": 0, "msg": "没有相关订单", "data": None}`——**文档口径 + 猜的形状**；而真机（实测）回的是 `{"code": 0, "msg": "没有相关订单信息", "data": ""}`（`msg` 多两个字、`data` 是**空串**）⇒ 「读不到」这一档的形状要是猜的，后面**所有**建在这条路上的判据（录单挡重、共享引擎的「读不到」档）都建在假上游上。⚠️⚠️ **同一个 `code=0`、两种意思**（真机口径）：三行里前两行**逐字相同**（不存在的 `orderId` / 不存在的 `sourceId` 就是同一个信封）、第三行只差 `msg` ⇒ 「查不到单」与「参数不合法」**只能靠 `msg` 分辨**；我们的调用方**永远会传一个合法单号** ⇒ 正常路径碰不到第三行，万一碰到 ⇒ 会被当成「对方没有这张单」⇒ 走录单 ⇒ **对方的「来源单号查重」会响亮拒掉**（兜底在，⛔ 不静默）。
        判别力（每一条都能红）：① `msg` 少两个字（改回「没有相关订单」）⇒ 逐字相等那条红；② `data` 从 `""` 改成 `None` ⇒ 逐字相等那条红（⭐ 这正是上一轮那版的形状）；③ 多一格（如 `"orderId": None`）或删掉 `msg` ⇒ 「恰好三键」那条红；④ 把假上游那两处 `_absent_order_envelope()` 换回手搓 dict（形状悄悄漂）⇒ 从原始回包取读数那条红。"""
        # ① 真机那**三行**本身逐字钉住（⛔ 不从别处派生：改一处就该在这里红）。
        self.assertEqual(len(ABSENT_ORDER_REAL_ENVELOPES), 3)
        self.assertEqual(
            ABSENT_ORDER_REAL_ENVELOPES,
            (
                {"code": 0, "msg": "没有相关订单信息", "data": ""},
                {"code": 0, "msg": "没有相关订单信息", "data": ""},
                {"code": 0, "msg": "系统单号和来源单号不可同时为空", "data": ""},
            ),
        )
        # ⭐ 「同一个 code 两种意思」这一条**在同一处可见**（改口径先改这里）：
        self.assertEqual([row["code"] for row in ABSENT_ORDER_REAL_ENVELOPES], [0, 0, 0])
        self.assertEqual(
            {row["msg"] for row in ABSENT_ORDER_REAL_ENVELOPES},
            {"没有相关订单信息", "系统单号和来源单号不可同时为空"},
        )
        self.assertEqual({row["data"] for row in ABSENT_ORDER_REAL_ENVELOPES}, {""})
        # ⭐ **恰好三键**（多 / 少都红）：
        self.assertEqual(list(ABSENT_ORDER_REAL_ENVELOPES[0]), ["code", "msg", "data"])
        self.assertEqual(list(ABSENT_ORDER_REAL_ENVELOPES[2]), ["code", "msg", "data"])
        # ② 假上游**真的回**这一格：读数取自它的**原始回包**（⛔ 不是拿常量自证）。
        request = httpx.Request(
            "GET", f"{MEMBER_BASE_URL}{ORDER_STATUS_PATH}",
            params={"sourceId": "source-synthetic-absent-0001"},
        )
        response = self.upstream.handler(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {"code": 0, "msg": "没有相关订单信息", "data": ""},
            "假上游回的「查不到单」信封与真机读数不一致（多 / 少 / 改字）",
        )
        # ③ 正向对照：这一格**真的**落在生产那条路上（真客户端把它读成「对方没有这张单」= None），
        #    ⇒ 上面那条「恰好」不是靠「这条路压根走不到」骗过的。
        with jidanpu_openapi.openapi_client(self.binding) as client:
            self.assertIsNone(client.find_order_status(source_id="source-synthetic-absent-0002"))


# ══ ⑧ 对账的**期望形态**：②式不该排「推不进已有单的大厅」这一档══════════

# ⚠️ 这一节补的是**排队那一侧**：上面 `LaunchTests` 里那条 `test_a_launch_on_an_already_recorded_order_says_it_cannot_do_that` 钉的是**消费**那一侧（终态跳过 + 人话）——⛔ 光有它**不够**：终态 `skipped` 只挡**重试**、⛔ **挡不住重排**，而对账是**每 180 秒一轮** ⇒ 每轮又排一条（今天不发作只因为写开关默认关着）。


class ReconcileHallTests(_OutboxCase):
    """⭐⭐ **主判据**：②式 + 一张「该在对方大厅」的单 + **一条已有链路** ⇒ 连跑 3 轮对账 ⇒ 台账里 **0 条** `member.launch`（⛔ 不是 3 条）。
    ⚠️ 「既有链路」这个前提是承重的：**没有链路**那一档**照旧**要排（那一档②式**真能办到**——建单的 `recUser=999999999` 让对方的单**建单那一刻**就进他家大厅）⇒ 见 `ReconcileNoLinkTests`（⛔ 两条合起来才是「只收口不可达的那一档」）。
    """

    def _make_it_a_hall_order_off_hall(self):
        """摆成「该在对方大厅」的形态：我方待抢单（在范围内、没人接）+ 链路标着**不在**大厅。"""
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.WAITING_GRAB)
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(in_hall=False)

    def _reconcile(self, *, now, line=None):
        return jidanpu_member.reconcile_and_queue(self.binding, now=now, line=line)

    def test_three_reconcile_rounds_queue_no_launch_event_at_all(self):
        """⭐⭐ **主判据的读数**：三轮对账 ⇒ `launch` 恒 0、台账里一行 `member.launch` 都没有。
        ⚠️ 每一轮都**照调度那样把它消费掉**：真机上对账与消费是一轮一轮跑的，而终态 `skipped` 只挡重试、⛔ 挡不住重排 ⇒ 不消费的话，下面那条反向控制读不出 3 条（读数是「旧实现每轮又排一条」，那正是这个环的形状）。
        判别力：把 `reconcile` 里那条收口去掉（＝旧实现）⇒ 本用例红，读数形如 `(3, 3) != (0, 0)`（三轮各排 1 条 ⇒ 台账 3 行）。"""
        self.enable()
        self._make_it_a_hall_order_off_hall()
        now = timezone.now()
        queued = 0

        for round_no in (1, 2, 3):
            with self.subTest(round_no=round_no):
                queued += self._reconcile(now=now).launch
                #: ⚠️ 每一轮都**照调度那样把它消费掉**：真机上对账与消费是一轮一轮跑的，
                #: 而终态 `skipped` 只挡重试、⛔ 挡不住重排 ⇒ 不消费就读不出「旧实现下台账 3 条」
                #: 这个形状（那正是这个环的形状）。
                self.run_a_round(now=now)

        self.assertEqual(
            (queued, self.db_out_events(kind="member.launch").count()), (0, 0),
            "三轮对账排出来的 member.launch =（差集计数, 台账行数）；旧实现这里是 (3, 3)",
        )
        #: ⭐ 一次上游都不打（连取 token 那一跳都没有：没有在飞事件时**不建连**）
        self.assertEqual(self.upstream.requests, [], "排了事件才会去建连；这里一次都不该有")

    def test_without_the_expectation_fix_the_same_shape_queues_three(self):
        """⭐ **反向控制**（「主判据能红」的另一半）：把②式那条线的**能力格**抹掉（＝旧实现的形状：期望里又有这一档）⇒ 同样三轮 ⇒ 台账里**3 条**。
        ⚠️ 只改**输入**（那一格），⛔ **不改断言**：读数「3 条」本身证明上面那条 0 条是**被这一格挡住的**，而不是「因为别的原因什么都没排」（E-0024 同族：自洽的对照证明不了任何东西）。
        """
        self.enable()
        self._make_it_a_hall_order_off_hall()
        line = replace(
            jidanpu_member_openapi_outbox.member_openapi_outbox_line(),
            hall_push_unreachable_reason=None,
        )
        now = timezone.now()

        for round_no in (1, 2, 3):
            with self.subTest(round_no=round_no):
                stats = self._reconcile(now=now, line=line)
                self.assertEqual(stats.launch, 1)
                self.run_a_round(now=now)

        rows = self.db_out_events(kind="member.launch")
        self.assertEqual(
            rows.count(), 1,
            "旧实现下每轮又排一条（真机上那个环的形状）；⭐ D-0370 终态停重排闸把"
            "「重排」这一半压断：第 1 轮排 1 条、消费成 skipped 后状态无变化 ⇒ 第 2/3 轮"
            "不排新行。反向控制由此仍具判别力——主判据的 0 行是「那一格让第一轮也不排」，"
            "这里读到 1 行 = 格子抹掉后该排的排了、只是不再循环",
        )
        #: ⚠️ 那一条是**终态跳过**（消费那一侧如实说「没有这个动作」）⇒ 挡得住重试；
        #: 重排这一半由终态停重排闸（D-0370）收口（期望侧判据不变，两层各管一半）。
        self.assertEqual({row.status for row in rows}, {"skipped"})


class ReconcileNoLinkTests(_WriteCase):
    """⭐⭐ **不许误伤**：②式 + 「该在对方大厅」的单 + **还没有链路** ⇒ 照旧走录单。
    ⚠️ 这一档的期望**可达**：②式建单的 `recUser=999999999` 让对方的单**建单那一刻**就进他家抢单大厅
    ⇒ 收口（`hall_push_unreachable_reason`）**只**该作用于「**已经有链路**」那一档。"""

    def test_a_hall_order_without_a_link_is_still_recorded(self):
        """⭐ 端到端：对账排 1 条 ⇒ 真的消费掉 ⇒ **对方真的建出单 + 链路落成 + `done`**。
        判别力：把收口的条件写成「②式一律不排 `to_launch`」⇒ `stats.launch` 变 0 且上游一次 `CreateOrder` 都没有 ⇒ 本用例红。
        """
        self.enable()
        now = timezone.now()

        stats = jidanpu_member.reconcile_and_queue(self.binding, now=now)

        self.assertEqual(stats.launch, 1, "没有链路那一档（＝该录单）被对账漏掉了")
        self.assertEqual(self.db_out_events(kind="member.launch").count(), 1)
        self.assertIsNone(self.db_link(), "还没消费，链路这会儿不该在")

        self.run_a_round(now=now)

        #: ⭐ 判据打在**对方的现场**与**数据库行**上（⛔ 不是「排了就算」）。
        self.assertEqual(len(self.upstream.created), 1)
        link = self.db_link()
        self.assertIsNotNone(link)
        self.assertTrue(link.in_hall, "②式录单那一发本来就把它放进对方大厅（recUser=999…）")
        self.assertEqual(self.db_out_events(kind="member.launch").get().status, "done")
