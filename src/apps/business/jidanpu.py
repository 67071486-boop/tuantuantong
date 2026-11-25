"""团团通的鸡蛋铺 SDK 适配，只做上游请求，不访问业务数据库。
既有操作沿用 SDK 判据，清接手人另核实该端点的实测成功码。
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx
from django.utils import timezone
from jidanpu import JidanpuClient, JidanpuConfig
from jidanpu.errors import (
    ApiError,
    AuthError,
    ConfigError,
    JidanpuError,
    ServerError,
    TransportError,
    # ⭐ **（出口池线 T3）**：本模块**再导出**它给调度侧按类型判（出口池的切池判据
    #    写的就是 `jidanpu.UpstreamBlockedError`）。⚠️ 本文件里没有第二处用到这个名字是**故意的**
    #    ——别当无用的 import 删掉（删了 `jidanpu_sync` 的 468 判据会 ImportError/AttributeError）。
    UpstreamBlockedError,
)

logger = logging.getLogger(__name__)

#  ⛔ 本模块**只借它的模型级常量** `SOURCE_NAME_DEFAULT`（来源名默认值的唯一出处）：
#  自己不查库、不落库（`models` 不 import 本模块，不会成环）。⭐ 常量住 `models` 的模块级
#  （原属已删除的 `JidanpuBinding` 类属性，同名同值同语义搬来），读到的还是「团团通系统」。
from apps.business.jidanpu_proxy import proxy_url
from apps.business.platform_egress import vendor_transport
from apps.business.models import SOURCE_NAME_DEFAULT

#: 「测试连接」用的端点（spec §2.5，`risk=read`）。
USERS_PATH = "/UserAjax/GetUserList"
#: 一页取多少。本端点的 `limit` 是「已登记参数、但实测值未记录」（`jidanpu/spec/endpoints.json`），
#: 100 是保守取值——本期只要「能登录 + 有一小撮样本」就够。⛔ 别拿别的端点的实测值当依据：
#: spec §11-T7 量到 2000 的是订单列表端点（`AGENTS.md` §5-E / E-0007：不同端点不共用参数模板）。
USERS_PAGE_SIZE = 100
#: 回给页面的样本条数上限（spec §6 的 `users_sample`）。
USERS_SAMPLE_SIZE = 10

#: 请求鸡蛋铺时用的 `User-Agent`：**浏览器级**（对端文档明写，`docs/JIDANPU-API.md:82`），取值照
#: spec §2.2 **逐字**抄（Windows Chrome 129），⛔ 不自报 SDK、别顺手换版本号——这个串只在
#: 「看起来像浏览器」上承重（实测租户机登录曾被对端 WAF 按 IP 拦成 HTTP 468）；⛔ 不改 `src/jidanpu/**`（vendored）：本仓自己传 `JidanpuConfig(user_agent=…)`，SDK 默认值一个字不动。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class UserListPage:
    """`GetUserList` 一页的结果。 `count` 取业务壳的 `count`（总人数，spec §2.5）；壳里没有可用的 `count` 时回落**本页行数**
    （低报总人数，但绝不编一个数）。"""

    count: int
    rows: tuple[dict, ...]


def credentials_of(binding) -> tuple[str, str, str] | None:
    """取一行绑定的上游凭据；不完整就回 `None`（调用方一律按 `1015` 处理）。 ⚠️ **再 strip 一次**（序列化器已 strip 过）：覆盖历史数据 / shell 写库这类绕过序列化器的路径
    （与 `xgj_views.credentials_of` 同一条理由）；密码**不进任何日志**（spec §8-S1）。"""
    if binding is None:
        return None
    base_url = (binding.base_url or "").strip()
    account = (binding.account or "").strip()
    password = (binding.password or "").strip()
    if not base_url or not account or not password:
        return None
    return base_url, account, password


def _user_list_page(client) -> UserListPage:
    """`GET /UserAjax/GetUserList` 一页 + 总人数（P1 `fetch_users` 的实现，抽出来共用）。
    ⚠️ **抽出来只为共用一次建连**（`inspect_binding` 与 `fetch_users` 都走这里），
    行为与 P1 的 `fetch_users` 一字不变。"""
    rows = client.call("GET", USERS_PATH, query={"page": 1, "limit": USERS_PAGE_SIZE})
    envelope = client.last_envelope or {}

    if not isinstance(rows, list):
        # 形状不对是**未预期的上游变更**：响亮地按上游失败报（1017），不静默当成空列表
        # ——「空结果不等于没有数据」（`AGENTS.md` §5-E / E-0004）。
        raise ApiError(
            f"{USERS_PATH} 的 data 不是列表（是 {type(rows).__name__}）", code=None
        )

    count = envelope.get("count")
    # `bool` 是 `int` 的子类，而 `count: true` 不是条数（照 SDK `iter_all` 的判据）。
    total = count if isinstance(count, int) and not isinstance(count, bool) else len(rows)
    return UserListPage(count=total, rows=tuple(rows))


def fetch_users(binding) -> UserListPage:
    """登录鸡蛋铺并拉**一页**人员列表（spec §2.5）。 ⚠️ SDK 异常（`ApiError` / `TransportError` / `AuthError`）一律原样上行、由视图翻译成人话 + 业务码
    （spec §5.7）；⚠️ 编程错误（`TypeError` 之类）**不接**——响亮地 500，不伪装成上游故障（E-0010）。"""
    with open_client(binding) as client:
        return _user_list_page(client)


# ── P2 加法：大厅双向同步用到的 9 个上游操作（spec §4–§6）─────────────────────────
# ⚠️ 本节函数一律**收 `client`**，调用方（`jidanpu_hall.py`）一轮只建一次连（各自建连 = N 次登录）、
#    P1 的 `fetch_users(binding)` 契约不变；⛔ **不传 `success_rule=`**（spec §5.7 / D15：读端点 `lenient`、写端点 `positive_code`），失败一律 SDK 抛 `ApiError`。

#: 管理端订单列表：**单查 / 回读（与会员线慢路整表）共用的端点**（单查走 `fetch_order_by_key1`，
#: `key1` 匹配语义见它的 docstring）。⛔ 上家大厅拉取**不走这里**（D-0366：大厅只走逐游戏段查，
#: 端点见 `jidanpu_upstream.HALL_PATH`；`orderSta=抢单中` 那条换源路已退役）。
ORDER_LIST_PATH = "/OrderAjax/GetOrderList"
#: 录单（方向二）。成功时回包 `code` = 新单的**内部主键 `oid`**（T19 实测，两轮独立复现）。
ADD_ORDER_PATH = "/OrderAjax/AddOrderAjax"
#: 抛单进内部大厅。
THROW_PATH = "/OrderAjax/UpdateDLOrderRevPriceByOrderID"
#: **改这张远端单的接单价**（T3）。⭐ 载荷与成功码**已真机实测复核**（原始回包只在**不进 git** 的
#: `.tools/recon/jidanpu-change-order-price-*.txt`）：`POST {orderId, fee}` ⇒ `code=1`、回读
#: **接单价字段是 `price`**、其余格不变 ⇒ **不是**抛单格（抛单价归 `THROW_PATH`；逐格清单见 `change_order_price`）。
CHANGE_ORDER_PRICE_PATH = "/OrderAjax/ChangeOrderPrice"
#: **从内部抢单大厅撤下**这一张单（⛔ 不是退单）。⭐ 业务方明确要求这个动作（本线原先因
#: 「载荷未实测」取消过它）；登记 `method=POST` / `risk=write` ⇒ 成功判据 = SDK 默认 `positive_code`
#: （与对标页面 JS 的 `if (data.code > 0)` 一致）、参数只要 `["orderId"]`；⚠️ **写之前先读**（`isThrowOrder == "Y"` 才写；字段语义见 `OWN_SOURCE_VALUE_FIELD`、`_adopted_throw_verdict`）。
RELEASE_TO_HOME_PATH = "/OrderAjax/RelesOrderToHomeByOrderID"
#: **抢单**（接下内部抢单大厅这一口价）——入方向「占住」用的就是它。⭐ 抢单**不改远端发单价**
#: （`recFee` 原样保留），这是它与 `ASSIGN_PATH` 的关键区别；载荷**只有 `orderId`**（对标 JS
#: `recThisOrder` 实测，实测读数见 `grab_order` 的 docstring、原始输出只在**不进 git** 的 `.tools/recon/`）。
GRAB_PATH = "/OrderAjax/RecOrderByInternal"
#: 指派给下家鸡蛋铺员工。入方向占住仍用 GRAB_PATH，以保留远端价格。
ASSIGN_PATH = "/OrderAjax/SetOrderZhiPaiByIDX"
CLEAR_TAKER_PATH = "/OrderAjax/CleanOrderZhiPaiByIDX"
#: 改状态 / 改退单（管理端端点，`changeSoureType=20` 必带）。
SET_STATUS_PATH = "/OrderAjax/SetOrderSta"
#: 接单人日志（拿「谁接了」的**唯一稳定路径**：数值 `userId`）。⚠️ **本端点未登记**在 vendor 的
#: `spec/endpoints.json`（实测）⇒ 照常调用、**不传 `success_rule=`**（未登记 ⇒ `lenient`，与已登记
#: 读端点同一个默认；自造第二层判据会误杀读端点，spec §12-2.3），形状防御在我们这边（`fetch_ass_logs`）。
ASS_LOGS_PATH = "/OrderAjax/GetOrderAssLogs"
#: 订单来源字典（绑定期检测「团团通系统」是否存在）。
SOURE_LIST_PATH = "/SystemAjax/GetOrderSoureList"
#: **指派页真正用的**取人端点（spike-1 §Q2③ 实测 102 行，字段是 `id` 不是 `uid`）。
ASSIGNABLE_USERS_PATH = "/UserAjax/GetAllUserTableByGameName"
#: 「这一行**有效 / 可被指派**」的字段名 = `isValid`（真机只读实测：行里**没有 `sta`**——那是
#: `GetUserList` 另一个端点的字段 ⛔ 别搬；`userType` / `outJobDate` / `loginAcc` 也不参与判据）。
#: 唯一出处：`inspect_binding` 的判据与出参键都读它；字段猜错只会**静默**读成空串恒假（SDK 不逐字段校验，E-0005 / E-0007 族）。
ASSIGNABLE_ACTIVE_FIELD = "isValid"

#: **当前登录账号**在鸡蛋铺的身份（`GET /api/UserApi/GetUserInfo`）——同步账号身份自愈与
#: 「测试连接」落库的**唯一身份源**。⚠️ 未登记在 `spec/endpoints.json` ⇒ 与 `ASS_LOGS_PATH` 同档
#: （不传 `success_rule=`；自造第二层判据会误杀读端点，spec §12-2.3；对标按 `data.code > 0` 判 ⛔ 不照抄）。
LOGIN_USER_INFO_PATH = "/api/UserApi/GetUserInfo"

#: 「接单销售客服」候选名单（录单页那一格**单选**、值是**员工 uid**——录单页 JS `getCustomerService()`
#: 把回包渲染成 `value="{item.value}"` 的 radio）。⚠️ 未登记在 SDK 清单 ⇒ 与 `ASS_LOGS_PATH` 同档
#: （不传 `success_rule=`、形状 `_as_rows` 兜）；另一人员端点 `ASSIGNABLE_USERS_PATH` 是另一份数据 ⛔ 别混用（E-0007）。
KEFU_CANDIDATES_PATH = "/UserAjax/GetAllUserByGroupName"
#: 客服那一组的 `groupType`（录单页 JS 里的字面值）。
KEFU_GROUP_TYPE = 20
#: 「**当前登录账号**」在回包里的标记值（实测：98 行里**恰好 1 行**是它）。
KEFU_SELECTED_VALUE = "selected"

#: 「全部游戏」= **不筛游戏**（业务方指定的大厅筛选口径）；`ORDER_LIST_KEY1_MANDATORY_PARAMS`
#: 的唯一字面值出处。⭐ D-0366 起大厅拉取不走这里（逐游戏段查带完整 `game.name`），
#: 现役消费者只剩慢路对账 / 单查那条参数模板。
HALL_GAME_NAME_ALL = "全部游戏"

#: `GetOrderList` 的**非时间**必带参数（spec §17.6.1：漏传不报错、**静默返回空集**）；取值照
#: spike-1 §Q2 的实测请求。⚠️ 这是**参数模板**，⛔ 不是业务口径、别在别处再拼一份；**时间窗不在
#: 这里**——必须按**调用当天**算（见 `order_list_window`，写死的窗随日历漂移、超跨度什么都查不到）。
ORDER_LIST_KEY1_MANDATORY_PARAMS: dict[str, object] = {
    "page": 1,
    "limit": 20,
    "minPrice": -999999,
    "maxPrice": 999999,
    "gameName": HALL_GAME_NAME_ALL,
    "timeType": 10,
}

#: ⭐ `GetOrderList` 的**完成时间**维度取值 = `30`（下游已完成那一档按**完成时间**框窗）；出处 = 两级实测证据
#: （⛔ 不是推断）：对标页 `dateType` 选项原文（`10 录单 / 20 开始 / 30 完成 / 40 收款 / 50 退款`，读码与更正见 `docs/JIDANPU-API.md` §4.1）+ 真机实测（逐字读数见 `docs/plans/2026-10-03-member-status-scope.md` §2）；
#: ⚠️ 模板里那格 `timeType: 10` 是**单查**路径的口径 ⛔ 不改，本常量只服务「按完成时间拉已完成」。
ORDER_TIME_TYPE_COMPLETED = 30

# ── 大厅列表（方向一）的参数集：取值出处 = 业务方的 `/Order/orderlist` 抓包；换端点原因：抢单大厅端点（`GetInternalOrderGrabbingList`）实测限流严重、而大厅同步要尽量频繁跑，同一份抓包证明带 `orderSta=抢单中` 的 `POST ORDER_LIST_PATH` 回同一批单（该换源路已退役，D-0366）──
#: 下面两组键**只此一处**（⛔ 别在别处再拼一份）、时间窗与必带参数共用 `_order_list_params`；
#: **必须显式给空串**的筛选键：漏传是「静默返回空集」、与「真的没单」同形（⛔ 不靠「不传就等于不筛」）。
HALL_ORDER_EMPTY_FILTER_KEYS: tuple[str, ...] = (
    "key1", "key2", "key3", "key4",
    "gameArea", "gameServer", "gameJob", "project",
    "recUserId", "customerServiceUserId", "orderSoure", "setSta",
    "groupName", "isJiaji", "isLeakOrder", "operatorName",
    "projectTypeName", "batchOrderId", "keytid", "orderLabel",
)
#: 抓包里出现、**我们一个都不依赖**的检索项：照抓包的默认值补上（⛔ 不静默省略，
#: 理由同上——漏传是「静默返回空集」，与「真的没有单」同形）。
HALL_ORDER_SEARCH_DEFAULTS: dict[str, object] = {
    "showPriceType": 0,
    "selectSeach1": 0,
    "key4Type": 0,
    "bussType": 0,
    "kefuType": 0,
    "seachMutPrice1": -999999,
    "seachMutPrice2": 99999999,
    "seachMutPrice3": -999999,
    "seachMutPrice4": 99999999,
}

#: 服务端对 `stime` / `etime` 的**跨度硬上限（天）**：服务端 `msg` 自报 + 实测边界 **460 过 / 461 拒**
#: （探针报告 `p2-window-report.md` §1–§2）。⛔ 超跨度的回包与「合法空窗」完全同形、`lenient` 会判成功
#: 回 `[]` ⇒ 静默；⚠️ 与起始日**无关**（2019 年的 100 天窗也受理）——⛔ 别误记成 E-0038 的 90 天下限。
ORDER_LIST_MAX_SPAN_DAYS = 460

#: 单查的回看天数：与对标页 `/Order/orderlist` 自己的默认窗一致（90 天）；`[今天-90 天 00:00:00,
#: 今天+1 天 23:59:59]` ⇒ 跨度 92 ≤ 460（实测可命中今天 / 31 / 80 天前的单；代价：录单早于 90 天查不到，
#: 但本端点用途是「回读刚录 / 刚抛的单」，余量极大）。⛔ **别为「更保险」放大**：> 460 一张都查不到。
ORDER_LIST_LOOKBACK_DAYS = 90
#: 承载**我方自造来源单号**的远端字段（T19 实测：写进 `souresOrderID` 的值**只**落在它上面；
#: `soureOrderId` 这个字段名根本不存在）。
OWN_SOURCE_VALUE_FIELD = "orderIdView"
#: 退单的目标态（`sta` 是**中文标签**，不是 code —— spec §5.7 实测）。
ORDER_REFUNDED_LABEL = "已退单"
#: ⭐ 撤下抛单（`release_order_to_home`）的**目标态** `orderSta` 标签。真机读数（原始输出只在
#: **不进 git** 的 `.tools/recon/`）：对**在厅**的单（`isThrowOrder=Y`）撤下 ⇒ `code=1「撤销成功」`、
#: 回读 `isThrowOrder=Y→N` 且 `orderSta=未分配`（同时 `recFee` 被清零，见 `jidanpu_member._consume_unlist`）。
ORDER_UNASSIGNED_LABEL = "未分配"
#: ⭐ 「对方抢先接走」那一档的 `orderSta` 标签（同一轮真机读数）：对**已被接走**的单撤下 ⇒ 回包仍是
#: `code=1「撤销成功」`、回读一个字没变 ⇒ 该原语对已被接走的单**无效且仍回成功码**；「回读
#: `isThrowOrder != "Y"`」分不出「我们撤下」与「对方抢先接走」——**只有 `orderSta` 这一格能分辨**。
ORDER_IN_PROGRESS_LABEL = "代练中"
#: 管理端改状态的 `changeSoureType`（实测：缺了被服务端判「请勿尝试跨权限提交」）。
MANAGEMENT_CHANGE_SOURCE_TYPE = 20
#: 远端 `remark` 的页面上限（`maxlength=50`，spec §5.7）。
HALL_REMARK_MAX = 50
#: 可指派人员的取数上限（**保守取值、不是实测上限**：spike-1 §Q2③ 实测 102 行，
#: 我们取 200 留余量；⚠️ 本端点的 `limit` 上限**未测**，所以这里只当一页的请求量）。
ASSIGNABLE_USERS_PAGE_SIZE = 200

#: 候选同步账号列表的**受控上限**（spec §10：`users_sample` 的 10 条上限不改、候选列表另用）。
#: ⚠️ 本端点 `limit` 上限未测 ⇒ 保守取值。**与 `ASSIGNABLE_USERS_PAGE_SIZE` 同源**：两者不一致时
#: 「能选的人」与「被核对的人」错位（检测通过却在页面选不到）⇒ ⛔ 别再写一个独立字面值。
CANDIDATES_LIMIT = ASSIGNABLE_USERS_PAGE_SIZE


@dataclass(frozen=True)
class AddOrderResult:
    """录单回包的两个可用部分。 ⚠️ `remote_oid` 取的是**业务壳的 `code`**（实测 = 新单的 `oid`），⛔ 不是 `data`。
    `None` = 壳里没有可用的正整数 `code`（理论上不会发生：`code=0` 会被 SDK 判失败并抛错）。"""

    remote_oid: int | None
    message: str


#: 「当场尝试」这一次跨系统调用的**单跳预算**（秒）：当场那次跑在**用户的 HTTP 请求里**，SDK 默认
#: （30 秒 + 重试阶梯 ⇒ 单请求最坏 ≈91.5 秒）会把「加速」变成「按住几分钟」——重试阶梯本来就是后台队列的职责；
#: 5 秒 = 上界（远小于后台阶梯）与下界（不把健康的慢上游当场判失败、白背一次 `attempts` 退避）的折中；⛔ 本仓没有上游延迟实测分布（`.tools/recon` 只记响应形状）——政策值、非实测 p99，⛔ 别调回默认阶梯。
IMMEDIATE_ATTEMPT_TIMEOUT = 5.0
#: 当场那次**只打一次**：不开 SDK 的重试阶梯（`0` = 每个请求只尝试 1 次）。
#: ⛔ 后台那条一眼都别动（`open_client(binding)` 不传预算 = 原样默认 `max_retries=2`）。
IMMEDIATE_ATTEMPT_MAX_RETRIES = 0


# ── 执行租约**心跳**（B6）：每次真发 HTTP 请求之前续一次租 ──────────────
#: 当前执行者的续租回调（`None` = 这次调用不属于任何 outbox 事件的执行：大厅拉取 / 绑定期检测 /
#: 「测试连接」没有租约、不该续租）。⚠️ 用 **contextvar** 而不是模块级变量：同进程可能有**嵌套**的执行上下文，contextvar 让内层退出后自动还原外层，模块级变量会被内层覆盖掉。
_lease_heartbeat: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "jidanpu_lease_heartbeat", default=None
)


@contextmanager
def upstream_heartbeat(renew: Callable[[], None]) -> Iterator[None]:
    """把 `renew` 挂成「**每次真发上游 HTTP 请求之前**跑一次」的钩子（执行租约心跳）。 ⚠️ 粒度是**每一次 HTTP 请求**：一跳里 SDK 自己还会重试 / 走 401/302 恢复（实测一次
    `client.call` 最多发 **16** 次 HTTP）⇒ 按请求续则租约只需覆盖单请求最坏时长（≈90 秒）、可取很短（180/60 秒）， SIGKILL 后空等从小时级压到分钟级；⚠️ 心跳只续自己的（`renew`
    闭包 `lease_owner` 令牌，判据 `filter(lease_owner=自己)`，见 `jidanpu_hall.renew_event_lease`）⇒ ⛔ 抢到别人租约的执行者**续不动**别人的租约。"""

    reset = _lease_heartbeat.set(renew)
    try:
        yield
    finally:
        _lease_heartbeat.reset(reset)


def tick_lease_heartbeat(request=None) -> None:
    """httpx 的 `request` 事件钩子本体（`open_client` 挂在**每一个**客户端上）。 ⚠️ httpx 会**位置传一个
    `httpx.Request`**（本函数用不到、不读不改它，只为对齐钩子调用约定）； 没有执行上下文 ⇒ 什么都不做（大厅拉取等常态路径零开销）。⚠️ **续租失败不打断这次上游请求**
    （只记 warning）：「只有一个人能落终态」由收尾的**所有权条件**（`_finish_event`）兜底， 把心跳失败翻成异常只会让本来能成的调用变成假故障。"""

    renew = _lease_heartbeat.get()
    if renew is None:
        return
    try:
        renew()
    except Exception:  # noqa: BLE001 —— 见 docstring：心跳失败不打断上游请求
        logger.warning("执行租约续租失败（不打断本次上游请求）", exc_info=True)


# ── 一轮的**请求预算 / 墙钟上限**（收窄「一轮耗时尾部无界」那条已登记残差）──────────
#: 一轮最多往上发多少次 HTTP 请求（**默认值**；`None` 不在取值范围——停用只能显式改调用点）。依据：正常一轮 ≈51 次（`docs/STATUS.md` 进方向残差 ≈18,312 次/小时 ÷ 360 轮）+ 慢路尖峰数百次（plan `2026-09-26-jidanpu-inbound-discovery.md` 附录 G-1）、本波实测空闲一轮仅 29 次 ⇒ 1200 只掐「无界尾部」；真撞上是**响亮且可重试**（`complete=False`、下一轮重来），⛔ 不是静默截断。
#: ⛔ 无上游 p99 分布（政策值非实测分位）；`tests/test_jidanpu_round_budget.py` 钉「实测请求数 == 假上游真被调次数」，⛔ 别拿本常量当期望值。
ROUND_REQUEST_BUDGET = 1200
#: 一轮从**起点**算起最多允许跑多少秒（`jidanpu_hall.run_round` 用它换算绝对截止点）；最快拍
#: `hall_poll_seconds=10` ⇒ 600 秒 = 60 拍、实测一轮 0.012~0.014 秒 ⇒ 对正常轮没有约束作用；
#: ⚠️ **只拦「还没发出去的下一次请求」**（已发出的由 SDK 超时兜底，真上界 ≈ 截止点 + 91.5 秒，⛔ 不是「到点即断」）；撞上它的那段**不推进自己的进度锚**（下个闸门一到就重来，仍受 10 秒闸门约束）。
ROUND_DEADLINE_SECONDS = 600
#: `RoundStats.limit_hit` / `RoundSummary.limit_hit` 的两个取值（人话：撞的是哪一条上限）。
LIMIT_REQUEST_BUDGET = "请求预算"
LIMIT_DEADLINE = "墙钟"


#: 「**上游失败**」这一族的**单一出处**（元组 + 下面的判定函数）：语义 = **问题不在我们这边**（4xx / 5xx /
#: 连接超时重试耗尽）；⚠️ `ApiError` 里也装业务码失败 ⇒ 本族回答「没拿到上游答复 / 上游自己坏了」，业务成败永远看各端点自己的判据（⛔ 别拿异常类型当业务结论）、结论一律响亮但可重试、⛔ 不置 `unexpected`（那是自家 bug 的报警位，`jidanpu_sync --once` 退出码 / cron 发信看它）；
#: ⛔ 不该进：`AuthError` / `RateLimitError`（专属判据与文案见 `api/jidanpu_views.py`；同步引擎限流走 `jidanpu_hall._stage_failure` 第三档，同样不并进）与 `ConfigError`（本地配置错，不是上游失败）。
UPSTREAM_FAILURE_ERRORS = (ApiError, ServerError, TransportError)


def is_upstream_failure(exc: BaseException) -> bool:
    """`exc` 是不是**上游失败族**（`UPSTREAM_FAILURE_ERRORS`）——这一族的第二半出处。 **接异常**用
    `except UPSTREAM_FAILURE_ERRORS`；手上已有异常实例、要判算不算上游噪音时用本函数。 ⛔ 语义边界见
    `UPSTREAM_FAILURE_ERRORS`（含那条措辞边界）：返回 `False` 的异常按**自家 bug** 处理（置 `unexpected`、响亮）。"""
    return isinstance(exc, UPSTREAM_FAILURE_ERRORS)


class RoundBudgetExceeded(RuntimeError):
    """本轮自设的**节流上限**被撞（请求预算 / 墙钟）——⛔ **不是上游失败**。 ⚠️ **有意不继承** `ApiError`：落库口径不同——上游失败照既有口径推进那一段的进度锚（否则持续失败
    会被按拍重打），撞本上限**一格锚都不动**（下轮立刻重来，⛔ 不留「这段这轮跑过了」的假象）； ⛔ 不许记 `unexpected`（会让 `jidanpu_sync --once` 非零退出、cron 每分钟发信）、⛔ 也不许因此改任何 outbox 事件的状态（`consume_outbox` 显式重抛它，见那里的注释）。"""


@dataclass
class RoundStats:
    """一轮的**实测值**（请求次数 / 起点 / 撞了哪条上限）——`open_client` 的**出参**。
    ⚠️ 由调用方（`jidanpu_hall.run_round`）建好并**持有**（⛔ 不落数据库列、⛔ 不加迁移）；
    `started_at` 由调用方给这一轮的起点 ⇒ 墙钟的「上限秒数」= `deadline - started_at`，读数与限值同源。"""

    #: 这一轮的起点（⚠️ 权威出处是 `run_round` 开头那个 `now`，⛔ 不在这里另读一次墙钟）。
    started_at: datetime = field(default_factory=timezone.now)
    #: **已发出**的 HTTP 请求次数（被上限拦下的那一次**不算**——它没有发出去）。
    requests: int = 0
    #: 撞的是哪条上限（空串 = 没撞）；取值是 `LIMIT_REQUEST_BUDGET` / `LIMIT_DEADLINE`。
    #: ⚠️ **先撞先记**（同轮两条都撞时留第一条）：它只是人话标签，两条都撞时
    #: `reasons` 里两条原因都在。
    limit_hit: str = ""

    @property
    def elapsed_seconds(self) -> float:
        """从本轮起点到**此刻**的秒数（墙钟）。"""
        return (timezone.now() - self.started_at).total_seconds()


def _make_round_budget_hook(*, request_budget, deadline, stats) -> Callable[[object], None]:
    """造「本轮计数 + 两条上限」的 httpx `request` 钩子（与租约心跳钩子**并存**）。 ⚠️ httpx 按钩子**列表顺序**逐个调 ⇒ 本钩子排在心跳**之后**（先续租、再判上限，反了「正要发的
    这一跳」不再续租）；计数粒度 = **每一次真发 HTTP 请求之前**（SDK 一次 `call` 内部还有重试与 401/302 恢复、实测最多 16 次，按业务跳计会少算）。⚠️ **先判后加**：`stats.requests`
    **恒等于** 真发出去的请求数（撞限的那次没发出去、不算），多段连撞时消息里的「已发 N 次」也稳定 （「先加后判」从第 2 次被拦起就会多 1，违反 `RoundSummary.request_count` 的口径）。"""

    def tick_round_budget(request=None) -> None:
        # ⚠️ 形参收得下 httpx 位置传进来的 `httpx.Request`（与 `tick_lease_heartbeat` 同约；
        #    本钩子用不到那个对象，⛔ 不读它、⛔ 不改它）。
        # ⚠️ `sent` 必须在**两条判定之前**读、且**只在两条都放行之后**才 `+= 1`。
        sent = stats.requests
        if request_budget is not None and sent >= request_budget:
            stats.limit_hit = stats.limit_hit or LIMIT_REQUEST_BUDGET
            raise RoundBudgetExceeded(
                f"本轮请求预算上限 {request_budget} 次已撞："
                f"实测本轮已发 {sent} 次，第 {sent + 1} 次请求被拦下"
            )
        if deadline is not None and timezone.now() >= deadline:
            limit_seconds = (deadline - stats.started_at).total_seconds()
            stats.limit_hit = stats.limit_hit or LIMIT_DEADLINE
            raise RoundBudgetExceeded(
                f"本轮墙钟上限 {limit_seconds:.0f} 秒已撞："
                f"实测本轮已跑 {stats.elapsed_seconds:.1f} 秒，下一次请求被拦下"
            )
        stats.requests += 1

    return tick_round_budget


#: `open_client(..., proxy=…)` 的「**没给**」哨兵：默认 = 出口池的当前出口（`jidanpu_proxy.proxy_url()`）。
#: ⛔ **不能拿 `None` 当默认值**：`None` 是「直连」这个合法取值（`jidanpu_proxy.DIRECT_TOKENS`）——
#: 那会把「谁都没指定」与「显式要直连」混成一件事（`egress_probe` 就是显式传 `None` 探直连的）。
_USE_POOL_OUTLET = object()


def _new_http_client(
    config: JidanpuConfig, *, request_hook: Callable[[object], None] | None = None,
    proxy: object = _USE_POOL_OUTLET,
) -> httpx.Client:
    """建 SDK 用的 httpx 客户端 + 挂执行租约心跳钩子（可选再挂一个本轮预算钩子）。 ⚠️ 参数**逐字对齐** `Transport.__init__` 的默认建法（`src/jidanpu/transport.py:415-419`）； ⛔ **`follow_redirects=False`
    承重**（302 到登录页要被判 `SessionExpiredError`，SDK 才会「清会话 + 重登一次 + 重发」；打开重定向会把登录页 HTML 当业务回包）——改这两个值 = 改 SDK 行为，⛔ 别顺手调； `event_hooks`
    是本仓自建客户端的**唯一**理由；`request_hook` **追加在心跳之后**（续租承重、 上限只是节流）、默认 `None` ⇒ 与接线前**逐字一致**；`proxy` 默认走出口池、`None` = 显式直连 （见 `_USE_POOL_OUTLET`，⛔ 除探针外没有调用点传它）。"""

    hooks: list[Callable[[object], None]] = [tick_lease_heartbeat]
    if request_hook is not None:
        hooks.append(request_hook)
    transport = vendor_transport(config.api_base)
    network = (
        {"transport": transport} if transport is not None
        else {"proxy": proxy_url() if proxy is _USE_POOL_OUTLET else proxy}
    )
    return httpx.Client(
        timeout=config.timeout,
        follow_redirects=False,
        headers={"User-Agent": config.user_agent},
        event_hooks={"request": hooks},
        **network,
    )


@contextmanager
def open_client(
    binding, *, timeout=None, max_retries=None,
    request_budget=None, deadline=None, round_stats=None,
    proxy: object = _USE_POOL_OUTLET,
):
    """按一行绑定建一个 SDK 客户端（凭据不全 ⇒ `ConfigError`）。 ⚠️ 登录是**惰性**的（首次 `call()` 才登录，不为建连多打一次上游）；凭据只从**这一行**取 （S3 / spec §11-S3：不接受任何请求来源的地址 / 账号）、密码**不进日志**（`ConfigError` 只说「不完整」）。 ⚠️ `timeout` / `max_retries` /
    `request_budget` / `deadline` / `round_stats` 全部可选：**不传 ⇒ 与接线前逐字一致**（SDK 没有 per-call 覆盖、只能按实例换值，所以 kwargs 只透传到本函数；语义与 默认值见 `IMMEDIATE_ATTEMPT_TIMEOUT`、`ROUND_REQUEST_BUDGET`、`ROUND_DEADLINE_SECONDS`、`RoundStats`
    各自注释——前四个是**输入**（拦不拦），`round_stats` 是**出参**（量到了多少），⛔ 别混）； 本函数自建 httpx 客户端只为挂心跳钩子（SDK 不给挂载点），生命周期由本函数负责、⛔ 不泄漏连接； `proxy`：不传 = 出口池当前出口、传 URL = 显式走它、传 `None` = 显式直连（`egress_probe` 加法， ⛔ 既有调用点一个字不用改）。"""

    credentials = credentials_of(binding)
    if credentials is None:
        raise ConfigError("鸡蛋铺地址 / 传令兵账号 / 密码不完整")
    base_url, account, password = credentials
    overrides: dict = {}
    if timeout is not None:
        overrides["timeout"] = timeout
    if max_retries is not None:
        overrides["max_retries"] = max_retries
    # ⚠️ **`user_agent=` 必须显式给**：SDK 的默认值自报 `jidanpu-sdk/0.1.1`，
    #    会被对端 WAF 盯上（见 `BROWSER_USER_AGENT` 的注释）。`_new_http_client` 读的是同一个
    #    config ⇒ 下面那个 httpx 客户端的 `User-Agent` 头也跟着变（⛔ 不用改两处）。
    config = JidanpuConfig(
        base_url=base_url, account=account, password=password,
        user_agent=BROWSER_USER_AGENT, **overrides
    )
    hook = None
    if round_stats is not None or request_budget is not None or deadline is not None:
        stats = round_stats if round_stats is not None else RoundStats()
        hook = _make_round_budget_hook(
            request_budget=request_budget, deadline=deadline, stats=stats
        )
    with _new_http_client(config, request_hook=hook, proxy=proxy) as http_client:
        # ⚠️ `http_client=` 让 `Transport` **不**接管关闭（`_owns_client=False`）⇒ 由上面
        #    那个 `with` 负责；⛔ 别把 `with http_client` 去掉（会泄漏连接池）。
        with JidanpuClient(config, http_client=http_client) as client:
            # ⚠️ **本仓自己挂的引用**（SDK 没有公开访问器，⛔ 不改 `src/jidanpu/`，也不是 SDK 字段、
            #    别指望 re-vendor 保留）：`jidanpu_upstream_pics` 给 **ViewPic 那一跳**套 per-request
            #    超时用（见 `_ViewPicHttp`：同一连接池 / 同一 cookie / 同一套事件钩子）。
            client._tt_http = http_client
            yield client


def _as_rows(rows, path: str) -> tuple[dict, ...]:
    """形状防御：载荷**必须是「全是对象的列表」**，否则响亮抛 `ApiError`（1017）。 ⛔ 不静默当成空列表、也不静默丢畸形的行——两者都会把「上游改版
    / 我们调用错」说成 「远端没有数据」（与「真的没有」同形，E-0004：单查回 `None`、拉取少单且零留痕）。
    代价方向是**故意的**：一行畸形 ⇒ 本轮中止 + 台账可见 + 下一轮重试，⛔ 别改回过滤； 合法空列表照常回空。"""
    if not isinstance(rows, list):
        raise ApiError(f"{path} 的 data 不是列表（是 {type(rows).__name__}）", code=None)
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            # `code=None`：这是**我们**报的形状错，⛔ 不伪造一个上游业务码。
            raise ApiError(
                f"{path} 的 data 第 {index} 行不是对象（是 {type(row).__name__}）",
                code=None,
            )
    return tuple(rows)


def _today() -> date:
    """当天（业务时区）。单独一层 = 测试可以注入一个固定时钟。
    ⛔ **不做模块级常量**：那是 import 时算一次，进程活多久就错多久（本模块踩过这个坑）。"""
    return timezone.localdate()


def order_list_window(*, today: date) -> dict[str, str]:
    """单查用的时间窗：**按当天算的滚动近 90 天**（跨度 92 天 ≤ 460，实测可命中）。 ⛔ 不许写死 `2000-01-01`
    / `2099-12-31`（实测超跨度被服务端拒成 `code=0 / count=0` + 一句 msg、 宽松解包判成**成功**回
    `[]` ⇒ 单查恒回 `None`，与「远端真的没这张单」同形——见 `ORDER_LIST_MAX_SPAN_DAYS`）。"""
    return {
        "stime": f"{(today - timedelta(days=ORDER_LIST_LOOKBACK_DAYS)).isoformat()} 00:00:00",
        "etime": f"{(today + timedelta(days=1)).isoformat()} 23:59:59",
    }


def _order_list_params(
    *, key1: str, page: int = 1, limit: int = 20, window: dict | None = None,
    game_name: str | None = None,
) -> dict:
    """`GetOrderList` 的完整参数：6 个非时间必带项 + 时间窗 + 单查键。 ⚠️ `window` /
    `game_name` 是慢路（T3）加法、有默认值 ⇒ 既有调用点逐字不变： `window` 让慢路给**更宽**的实测窗（近
    180 天）；`game_name` 复刻大厅那条 `gameName` 回落 （⛔ 不在这里另拼一份模板）。"""
    data = dict(ORDER_LIST_KEY1_MANDATORY_PARAMS)
    data.update(window if window is not None else order_list_window(today=_today()))
    data["page"] = page
    data["limit"] = limit
    data["key1"] = key1
    if game_name is not None:
        data["gameName"] = game_name
    return data


def _order_list_search_params(
    *, key1: str, page: int = 1, limit: int = 20, window: dict | None = None,
    game_name: str | None = None,
) -> dict:
    """`GetOrderList` 的**完整**参数 = 必带项 + 时间窗 + 抓包里的空筛选键 / 检索默认值。 ⚠️ 那 20 个空筛选键必须**显式给**（「漏传
    ⇒ 静默返回空集」的实测硬限，少给一个与 「真的没单」同形，见 `HALL_ORDER_EMPTY_FILTER_KEYS`）；两组键出自对标页自己的抓包、
    大厅与慢路共用同一份（⛔ 不各拼一份，E-0007）；慢路不带 ⇒ `count=0` 被判成「拉全了」、 安全网只剩 ≤200 的逐单复核（在途量级下极易顶掉、持续不动作）。"""
    data = _order_list_params(
        key1=key1, page=page, limit=limit, window=window, game_name=game_name
    )
    data.update(dict.fromkeys(HALL_ORDER_EMPTY_FILTER_KEYS, ""))
    data.update(HALL_ORDER_SEARCH_DEFAULTS)
    data["key1"] = key1
    return data


def _order_list_page(
    client, *, key1: str, page: int = 1, limit: int = 20, window: dict | None = None
):
    rows = client.call(
        "POST", ORDER_LIST_PATH,
        data=_order_list_params(key1=key1, page=page, limit=limit, window=window),
    )
    return _as_rows(rows, ORDER_LIST_PATH)


def fetch_order_by_key1(
    client, *, key: str, expect_field: str, expect_value: str, window: dict | None = None
) -> dict | None:
    """按 `key1` 定位一行，并**逐字回校验**目标字段（spec §4.5 / §5.2 / §5.5 的 T19 订正块）。 ⚠️ 实测语义 = 「`orderId` 精确相等」**OR**「`orderIdView` 子串匹配」⇒
    `key1=<oid>` 会误命中无关单： **任何**调用点都必须给 `expect_field` / `expect_value`，⛔ 不许拿 `count == 1` 当「查到了」（E-0004 同族）； `None` = 第一页没有任何一行逐字相等（=
    远端没这张单，调用方记台账——响亮不静默）；时间窗默认滚动 90 天 ⇒ 录单早于 90 天查不到，⛔ 别拼大窗（>460 被拒且与「真没有」同形）；`window` 是慢路加法（同函数复核）、 ⛔ 不为此另造第二个单查实现。"""
    for row in _order_list_page(client, key1=key, window=window):
        if str(row.get(expect_field, "")) == expect_value:
            return row
    return None


def fetch_order_recorded_by(client, *, remote_order_id: str, user_id: str) -> dict | None:
    """Use the native order-list filter for the employee who recorded the order."""
    data = _order_list_search_params(key1=remote_order_id)
    data.update(customerServiceUserId=user_id, kefuType=5)
    rows = _as_rows(client.call("POST", ORDER_LIST_PATH, data=data), ORDER_LIST_PATH)
    envelope = client.last_envelope or {}
    code, count = envelope.get("code"), envelope.get("count")
    if type(code) is not int or code != 0 or type(count) is not int or count != len(rows):
        raise ApiError("鸡蛋铺录单人筛选未返回可核实结果", endpoint=ORDER_LIST_PATH)
    return next((row for row in rows if str(row.get("orderId")) == remote_order_id), None)


ORDER_RECYCLE_PATH = "/OrderAjax/GetOrderRecycleList"
RESTORE_RECYCLED_ORDER_PATH = "/OrderAjax/RefOrderRecycle"
CHANGE_SOURCE_ORDER_NO_PATH = "/OrderAjax/ChangeOrderSoureId"


def find_order_for_repair(client, *, remote_order_id: str | None, window: dict, recycle=False,
                          source_order_no: str | None = None, remote_oid: int | None = None) -> dict | None:
    """按内部号查正常列表或回收站，只有完整有效的查询才能确认未命中。"""
    key = source_order_no if source_order_no is not None else remote_order_id
    path = ORDER_RECYCLE_PATH if recycle else ORDER_LIST_PATH
    page_size = 20
    total = None
    for page in range(1, 11):
        if recycle:
            payload = client.call("GET", path, query={
                **window, "gameName": HALL_GAME_NAME_ALL, "orderSta": "",
                "key1": key, "key2": "", "orderSoure": "", "page": page, "limit": page_size,
            })
        else:
            filters = _order_list_params(key1=key, page=page, limit=page_size, window=window)
            filters.update(minPrice="", maxPrice="")
            payload = client.call("POST", path, data=filters)
        rows = _as_rows(payload, path)
        envelope = client.last_envelope or {}
        code, count = envelope.get("code"), envelope.get("count")
        if (
            type(code) is not int or code != 0 or envelope.get("msg") != ""
            or type(count) is not int or count < 0
            or (total is not None and count != total)
            or len(rows) != min(page_size, max(count - (page - 1) * page_size, 0))
        ):
            raise ApiError("鸡蛋铺订单查询没有返回完整结果", endpoint=path)
        for row in rows:
            remote_matches = remote_order_id is None or str(row.get("orderId") or "") == remote_order_id
            source_matches = source_order_no is None or row.get(OWN_SOURCE_VALUE_FIELD) == source_order_no
            oid_matches = remote_oid is None or str(row.get("oid") or "") == str(remote_oid)
            if remote_matches and source_matches and oid_matches:
                return row
        if count <= page * page_size:
            return None
        total = count
    raise ApiError("鸡蛋铺订单查询超过可确认页数", endpoint=path)


def restore_recycled_order(client, *, remote_order_id: str) -> None:
    client.call("POST", RESTORE_RECYCLED_ORDER_PATH, data={"orderId": remote_order_id})


def change_source_order_no(client, *, remote_order_id: str, source_order_no: str) -> None:
    client.call("POST", CHANGE_SOURCE_ORDER_NO_PATH, data={
        "orderId": remote_order_id, "value": source_order_no,
    })


# ── T3：慢路（整表对账）的取数（plan `2026-09-26-jidanpu-inbound-discovery` §4.2）──
#: 慢路一轮的取数窗（天）：依据 = spec `2026-09-24-jidanpu-sync-design.md` §11-T7 的实测「近 180 天」；⚠️ 窗的语义是
#: **录单时间**（`timeType=10`，`docs/JIDANPU-API.md` 第 127–133 行的页面原文）——远端订单没有「最后修改时间」（spec §5.2）⇒ 只能按录单时间框行；跨度 181 天 ≤ 实测上限 460（`ORDER_LIST_MAX_SPAN_DAYS`）。
RECONCILE_WINDOW_DAYS = 180
#: 慢路整表取数的**一页请求行数**。⚠️ 实测悬崖（同一 dev 租户 / 180 天窗）：页大小 `200~1200` ✅、
#: `1300~2300` ❌ HTTP 500 ⇒ 取 **1000**（距悬崖 1.3 倍余量）；两个条件才崩（≳1300 **且**桶行数 ≥ 页大小，行数不足则不受影响）⇒ 旧读数「2000 也正常」是那次桶只有 1192 行骗的；
#: 单桶行数上界 = `MAX_PAGES` × 本值、超了按「拉不全」响亮处理；⛔ 本值只是请求量、不是拉全保障（拉全按「累计 ≥ `count`」判，见 `jidanpu_reconcile._fetch_label`）。
ORDER_LIST_PAGE_SIZE = 1000


@dataclass(frozen=True)
class OrderListPage:
    """`GetOrderList` 的一页（慢路用）。 ⚠️ `count` 是 `int | None`：`None` = 壳里没有**可用**的 `count`（没给
    / 负数 / 布尔）⇒ 判不了拉全 （与 `LogPage` 同一条 fail-closed 口径，⛔ 不许拿它比大小）。⚠️ **没有**「全部游戏回落」这一格：
    这一支有 **12 档**、单档回空是常态 ⇒ 回落不能在页级别判（整轮判据在 `jidanpu_reconcile._pull_all`， 12 档全回空才回落）。"""

    rows: tuple[dict, ...]
    count: int | None = None


def reconcile_window(*, today: date) -> dict[str, str]:
    """慢路取数窗：`[今天 - 180 天 00:00:00, 明天 23:59:59]`（录单时间口径）。 ⚠️
    与 `order_list_window`（滚动 90 天）是**两条窗**、各有各的调用方（那条服务「回读我方
    刚录 / 刚抛的单」，这条服务「整表对账要看更宽的在途集」）——⛔ 别合成一个， 合成后必有一边的语义被改掉。"""
    return {
        "stime": f"{(today - timedelta(days=RECONCILE_WINDOW_DAYS)).isoformat()} 00:00:00",
        "etime": f"{(today + timedelta(days=1)).isoformat()} 23:59:59",
    }


def _reconcile_attempt(
    client, *, status_label: str, window: dict, page: int, limit: int, game_name: str
):
    """发**一次**慢路查询，回 `(原始载荷, 顶层 count)`；⛔ 不在这里判载荷形状。 ⚠️ `count` 判据：`bool` 是 `int` 的子类（`count: true`
    不是条数）、**负数也不是条数** ⇒ 都回 `None` ——放行负数会让调用方「累计 ≥ `count`」恒真、半份数据被判「拉全了」（fail-open，绕过 「拉不全 ⇒
    整轮不动作」不变量；大厅那一支一直有此守卫）；⛔ `count = 0` 是合法值、 别与负数并一个分支；参数集走 `_order_list_search_params`（与大厅共用，见那里的注释）。"""
    data = _order_list_search_params(
        key1="", page=page, limit=limit, window=window, game_name=game_name
    )
    data["orderSta"] = status_label
    payload = client.call("POST", ORDER_LIST_PATH, data=data)
    envelope = client.last_envelope or {}
    count = envelope.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        return payload, None
    if count < 0:
        return payload, None
    return payload, count


def fetch_reconcile_orders_page(
    client, *, status_label: str, window: dict, page: int = 1,
    limit: int = ORDER_LIST_PAGE_SIZE, game_name: str = HALL_GAME_NAME_ALL,
) -> OrderListPage:
    """`POST GetOrderList` 按**状态中文标签**取一页（慢路整表对账用）。 ⚠️ 筛选值给**中文标签**（`orderSta=未分配` 等）：本端点的 `orderSta` 参数就是中文名本身 （`docs/JIDANPU-API.md`
    第 331 行读码结论；`WFP` / `DLZ` 那种码只在响应 `total` 字典的键上， ⛔ 别拿码当筛选值）；参数模板**只此一处**（漏传键 = 静默空集）⇒ ⛔ 不少给任何必带键； ⛔ **本函数不做
    `gameName` 回落**：大厅一支只有一次查询、回落成立，本模块 12 档、单档回空是常态 ⇒ 页级别回落会让常态触发整轮回落到语义未实测的空串上（整轮判据在 `jidanpu_reconcile._pull_all`）。"""
    payload, total = _reconcile_attempt(
        client, status_label=status_label, window=window, page=page, limit=limit,
        game_name=game_name,
    )
    return OrderListPage(
        rows=_as_rows(payload, ORDER_LIST_PATH), count=total
    )


def fetch_completed_orders_page(
    client, *, window: dict, page: int = 1, limit: int = ORDER_LIST_PAGE_SIZE,
    game_name: str = HALL_GAME_NAME_ALL,
) -> OrderListPage:
    """`POST GetOrderList` 取**一页**「已完成」的单（下游已完成那一档；形状同 `fetch_reconcile_orders_page`）。 ⚠️ **时间维度是完成时间**（`timeType=ORDER_TIME_TYPE_COMPLETED`）：沿用模板默认
    `10`（录单时间）会让 「录单很早、刚完成」的单落在窗外——⛔ 模板默认值本身一字不动（那是单查那条路径的口径）；筛选值同样是 **中文标签**（`orderSta=已完成`，⛔ 别拿 `YWC`
    那种码）；参数模板只此一处、不改写调用方给的 `gameName`、 不做回落；`count` 沿用 `_reconcile_attempt` 的 fail-closed 口径（无可用 `count` ⇒ `None`，按「拉不全」处理）。"""
    data = _order_list_search_params(
        key1="", page=page, limit=limit, window=window, game_name=game_name
    )
    data["timeType"] = ORDER_TIME_TYPE_COMPLETED
    data["orderSta"] = "已完成"
    payload = client.call("POST", ORDER_LIST_PATH, data=data)
    envelope = client.last_envelope or {}
    count = envelope.get("count")
    # 布尔是 `int` 的子类（`count: true` 不是条数），负数也不是（放行会让调用方的
    # 「累计行数 ≥ count」**恒真** ⇒ 半份数据被判成「拉全了」）——与 `_reconcile_attempt` 同一条。
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = None
    return OrderListPage(rows=_as_rows(payload, ORDER_LIST_PATH), count=count)


def add_order(
    client, *, form_payload: dict, top_level: dict, source_order_no: str
) -> AddOrderResult:
    """录单（spec §5.2）。 ⚠️ `strParameter` = 整张录单表单的 JSON 再 URL 编码一次（旧 spec §2.8-1 实测）：表单值不能是 HTML 默认值（空串 ⇒ 服务端
    `(int)JToken` 抛 `FormatException` ⇒ HTTP 500），必须来自 `jidanpu_order_template`； ⚠️ 顶层参数与表单 JSON 是**两份**（旧
    spec §2.8-2 实测：`gameName` 顶层是游戏名、表单里是游戏 ID） ⇒ 收两个字典 ⛔ 不合成为一；`souresOrderID` 是硬要求（空 ⇒ 服务端拒），两份里**都要**有。"""
    data = dict(top_level)
    data["souresOrderID"] = source_order_no
    data["strParameter"] = quote(
        json.dumps(form_payload, ensure_ascii=False, separators=(",", ":"))
    )
    client.call("POST", ADD_ORDER_PATH, data=data)
    envelope = client.last_envelope or {}
    code = envelope.get("code")
    remote_oid = code if isinstance(code, int) and not isinstance(code, bool) else None
    return AddOrderResult(remote_oid=remote_oid, message=str(envelope.get("msg", "")))


def throw_order(client, *, remote_order_id: str, fee) -> None:
    """把一张远端单抛进它的内部大厅（spec §5.3 实测载荷）。 ⚠️ `fee` 写成字符串（元、2 位小数；⛔ 回读值不当我方权威值，spec §5.4）；⛔ `orderSta` 不是判据（抛单 前后都是
    `未分配`）——回读只看 `isThrowOrder == 'Y'` 与 `recFee == 折算后的抛单价`（`fee` 由调用方经 `jidanpu_member._throw_fee` = `hall_price ×
    throw_price_percent / 100` 给出，只有 percent==100 才恰好等于 `hall_price`）； 本函数**不算**那个百分比（口径只有 `_throw_fee` 一处，E-0021 同族）。"""
    client.call(
        "POST", THROW_PATH,
        data={
            "orderId": remote_order_id,
            "fee": str(fee),
            "action": 10,
            "insureFee": 0,
            "pushPriceMode": 10,
        },
    )


def change_order_price(client, *, remote_order_id: str, fee) -> None:
    """改一张远端单的**接单价**（`price` 那一格；实测复核读数见 `CHANGE_ORDER_PRICE_PATH` 的注释）。 ⚠️ `fee` 写**字符串**；成败交 SDK（`risk=write`
    ⇒ `positive_code`，⛔ 不解析回包、不写第二层判据，E-0087）； ⚠️ 调用方必须**回读钉值**（`jidanpu_member._consume_price`：写接口不抛
    ≠ 那一格真变了）；「这张单还挂在 对方大厅」时改 `price` 顶不掉由 `recFee` 承载的抛单价 ⇒ `_consume_price` 会**再抛一次**（`throw_order`）。"""
    client.call(
        "POST", CHANGE_ORDER_PRICE_PATH,
        data={"orderId": remote_order_id, "fee": str(fee)},
    )


def release_order_to_home(client, *, remote_order_id: str) -> None:
    """从内部抢单大厅撤下原单。调用方须先确认仍在大厅，写后回读离厅且未分配。
    订单已被接走时也可能收到成功回包，不能只凭回包确认撤下。"""
    client.call("POST", RELEASE_TO_HOME_PATH, data={"orderId": remote_order_id})


def grab_order(client, *, remote_order_id: str) -> None:
    """**抢下**一张仍在大厅的远端单（入方向「占住」用；⛔ 不是指派）。 ⭐ 抢单 = **接下大厅这口价**：远端发单价（`recFee`）**原样保留**——本调用不发任何金额，从根上不产生 「自家占住把远端发单价写成 0」那个 0（那个 0
    会被字段收敛当成远端改价跟回本地、`hall_price` 归零）； ⚠️ 载荷**只有 `orderId`**（对标 JS `recThisOrder` 实测，token 不带也能成功）⇒ ⛔ 别加 `fee` / `userId`； 成败交 SDK（`positive_code`，⛔
    不写第二层判据）；**失败文案不可当判据**（实测：重复抢单回的是与 「已被别人接走」同一句 `code=0`）⇒ 调用方（`jidanpu_hall._try_assign`）一律**回读 `isThrowOrder`**， ⛔ 不看 `msg`。"""
    client.call("POST", GRAB_PATH, data={"orderId": remote_order_id})


def assign_order(
    client, *, remote_order_id: str, user_id: str, user_name: str, fee,
    hours=None, project: str = "",
) -> None:
    """按明确的打手编号指派，成功必须收到整数业务码 1。"""
    client.call(
        "POST", ASSIGN_PATH,
        data={
            "orderId": remote_order_id,
            "strParameter": json.dumps(
                [{
                    "userId": str(user_id),
                    "userName": user_name,
                    "fee": float(Decimal(str(fee))),
                    "hours": "" if hours is None else hours,
                    "project": project,
                }],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "isAutoAss": "N",
            "sphone": "",
            "isOpenThrowOrderPricePerce": "N",
            "taskNeedJobId": "",
        },
        retry_on_sent=False,
        retry_on_rejection=False,
    )
    envelope = client.last_envelope or {}
    code = envelope.get("code")
    if type(code) is not int or code != 1:
        raise ApiError("指派未返回成功码", endpoint=ASSIGN_PATH, code=code, body=envelope)


def clear_order_taker(client, *, remote_order_id: str) -> None:
    """清除当前接手人，保留代练接手人的绩效记录。"""
    client.call(
        "POST", CLEAR_TAKER_PATH, data={"orderId": remote_order_id, "isDelEmp": "N"},
        success_rule="positive_code", retry_on_sent=False,
    )
    envelope = client.last_envelope or {}
    code = envelope.get("code")
    if type(code) is not int or code != 1:
        raise ApiError("清接手人未返回成功码", endpoint=CLEAR_TAKER_PATH, code=code, body=envelope)


def set_order_status(
    client, *, remote_order_id: str, status_label: str, remark: str = ""
) -> None:
    """改状态 / 退单（管理端端点，spec §5.7 实测载荷）。 ⚠️ `sta` 是**中文标签**；`changeSoureType=20`
    **必带**；`remark` ≤50 字（超出直接拒， ⛔ 不静默截断——静默截断会让「原因」变成一句看不出被截过的话）。
    ⚠️ `isSyncTaoBao` **固定 `N`**（「同步淘宝发货」不在本期范围）。"""
    if len(remark) > HALL_REMARK_MAX:
        raise ValueError(f"退单原因最多 {HALL_REMARK_MAX} 字，收到 {len(remark)} 字")
    client.call(
        "POST", SET_STATUS_PATH,
        data={
            "orderId": remote_order_id,
            "sta": status_label,
            "changeSoureType": MANAGEMENT_CHANGE_SOURCE_TYPE,
            "remark": remark,
            "isSyncTaoBao": "N",
        },
    )


def fetch_ass_logs(client, *, remote_order_id: str) -> tuple[dict, ...]:
    """取该单的接单人日志（`userId` 的**唯一稳定来源**，spike-1 §Q2③ 实测）。
    ⚠️ 见 `ASS_LOGS_PATH` 的注释：未登记端点、**不传** `success_rule=`、形状由我们校验。"""
    rows = client.call("GET", ASS_LOGS_PATH, query={"orderId": remote_order_id})
    return _as_rows(rows, ASS_LOGS_PATH)


def fetch_sources(client) -> tuple[dict, ...]:
    """订单来源字典（spec §10 的绑定期检测用）。实际条数会漂 ⇒ ⛔ 不写死条数。"""
    return _as_rows(client.call("GET", SOURE_LIST_PATH), SOURE_LIST_PATH)


def fetch_assignable_users(
    client, *, page_size: int = ASSIGNABLE_USERS_PAGE_SIZE, page: int = 1,
    game_name: str = "", task_id: str = "0", key: str = "",
) -> tuple[dict, ...]:
    """**可被指派**的人员列表（指派页真正用的那个端点；字段里的人 id 是 `id`）。
    ⚠️ 这是**同步账号**的候选人来源（spec §10）；⛔ 它**不代表**我方要建人员映射
    （那条线已取消，spec §17.2）。"""
    query = {"page": page, "limit": page_size}
    if game_name:
        query.update(gameName=game_name, taskId=task_id, userRoleId=0, userOrderBy=0, bussType=0, key=key)
    rows = client.call("GET", ASSIGNABLE_USERS_PATH, query=query)
    return _as_rows(rows, ASSIGNABLE_USERS_PATH)


def fetch_kefu_candidates(client) -> tuple[dict, ...]:
    """「接单销售客服」候选名单（录单页那一格的数据源）。 ⚠️ 参数集照录单页 JS（见 `KEFU_CANDIDATES_PATH`
    的注释）：`isSelectCurrentUser=Y` 是**承重**的 ——它让「当前登录账号」那一行带上 `selected`，`own_staff_uid`
    靠它认人。 ⚠️ 未登记端点 ⇒ 与 `ASS_LOGS_PATH` 同档：成败只看 SDK 抛不抛，形状由 `_as_rows` 兜。"""
    rows = client.call(
        "GET", KEFU_CANDIDATES_PATH,
        query={
            "groupType": KEFU_GROUP_TYPE,
            "isSelectCurrentUser": "Y",
            "isOnlySelectSelf": "N",
            "dy": 1,
        },
    )
    return _as_rows(rows, KEFU_CANDIDATES_PATH)


def own_staff_uid(client) -> str:
    """**当前登录账号**在下家店铺里的员工 uid；认不出来回空串（⛔ 绝不猜一个）。用途：录单表单的 `inputOrderKefu`（业务方裁定「填**绑定账号自己**」）。判据**只有一条**：名单里**恰好一行**带
    `selected == "selected"`（录单页就用它预选当前账号，真机实测 98 行里恰好 1 行）；零行 / **多于一行** （认不出谁 ⇒ 填错比不填更糟）/ `value` 不是数值（形状变了，⛔
    不把文本填进对方的 uid 字段） 三种一律回空串、由调用方「不填 + 留人话台账」；上游异常（`JidanpuError` 家族）**向上抛** （⛔ 不吞——吞了就分不清「接口挂了」还是「认不出人」）。"""
    rows = fetch_kefu_candidates(client)
    selected = [
        row for row in rows
        if str(row.get("selected") or "").strip() == KEFU_SELECTED_VALUE
    ]
    if len(selected) != 1:
        return ""
    value = str(selected[0].get("value") or "").strip()
    return value if value.isdigit() else ""


# ── **登录账号**的身份（同步账号 = 我们登录的那个账号）─────────
# ⚠️ 与上面 `own_staff_uid` 的区别（⛔ 别当成同一件事）：那个回答「录单表单要填谁」（客服名单里被标记为
#    「当前登录账号」的那一行，走 `KEFU_CANDIDATES_PATH`）；本节回答「**这个会话在鸡蛋铺是谁**」——抢单只认会话身份 ⇒ 远端把单记在这个账号名下，收敛与回声抑制都拿它的显示名去比。


def _positive_uid(raw) -> str:
    """上游身份里的 uid 那一格 → **正整数字符串**；读不出来回空串（⛔ 不猜、不截断）。 ⚠️ `bool` 是 `int` 的子类 ⇒
    先排掉（`Id: true` 不是 uid，照 SDK `iter_all` 的判据）。 ⚠️ 只收十进制整数字面（`str.isdigit`）；`"551.0"`
    / `"uid=551"` 一律算读不出来 ——它们不是「另一个写法」，是**形状变了**（E-0005 / E-0007 族）。"""
    if isinstance(raw, bool):
        return ""
    text = str(raw).strip()
    if not text.isdigit():
        return ""
    return text if int(text) > 0 else ""


def _info_value(info: dict, *keys: str):
    """大小写键兼容取值：回第一个非 `None` 的值（实测今日回包是小写 `id`/`name`/`account`，
    老形态是大写 `Id`/`Account`——⛔ 不硬编码「上游一定哪种」）。"""
    for key in keys:
        value = info.get(key)
        if value is not None:
            return value
    return None


def fetch_login_user(client) -> dict:
    """**当前登录账号**在鸡蛋铺的身份对象（`GET /api/UserApi/GetUserInfo`）。 ⭐ 是**登录账号**的身份源（⛔
    不是「可被指派名单里被选中的那个人」）：抢单只认会话身份 ⇒ 远端把单记在**这个**账号名下。⚠️ 见 `LOGIN_USER_INFO_PATH` 的注释（未登记端点、不传
    `success_rule=`）；形状由本函数校验（`data` 必须是对象，不对则响亮抛 `ApiError`——⛔ 不静默， 否则「读不出来」与「身份是空的」同形，E-0004）。"""
    data = client.call("GET", LOGIN_USER_INFO_PATH)
    if not isinstance(data, dict):
        raise ApiError(
            f"{LOGIN_USER_INFO_PATH} 的 data 不是对象（是 {type(data).__name__}）",
            code=None,
        )
    return data


def _login_uid(info) -> str:
    raw_id = _info_value(info, "Id", "id")
    uid = _positive_uid(raw_id)
    if not uid:
        raise ApiError(
            f"{LOGIN_USER_INFO_PATH} 的 Id/id 不是正整数（是 {raw_id!r}）", code=None
        )
    return uid


def fetch_login_uid(client) -> str:
    """只校验当前登录账号编号，省去显示名的名单回退。"""
    return _login_uid(fetch_login_user(client))


def resolve_login_identity(client) -> tuple[str, str]:
    """显示名依次取身份响应、可指派名单、人员页、登录账号。
    名单读取失败会抛异常，只有成功读完仍无结果时才返回空名。"""
    info = fetch_login_user(client)
    uid = _login_uid(info)
    direct = str(_info_value(info, "name", "Name") or "").strip()
    if direct:
        return uid, direct
    for row in fetch_assignable_users(client):
        if str(row.get("id", "")).strip() == uid:
            name = str(row.get("userName") or "").strip()
            if name:
                return uid, name
    for row in _user_list_page(client).rows:
        if str(row.get("uid", "")).strip() == uid:
            name = str(row.get("userName") or "").strip()
            if name:
                return uid, name
    account = str(_info_value(info, "account", "Account") or "").strip()
    return uid, account


# ── T2：快路要翻的**那两个日志**（plan `2026-09-26-jidanpu-inbound-discovery` §4.1）：本节函数一律**收 `client`**（同因：调用方一轮只建一次连）；⛔ **不走 `code` 判成败**（plan §3.2-2 / E-0087：两个端点都是 `risk=read` ⇒ SDK 给它们 `lenient`，`src/jidanpu/client.py:364`），成败只看 SDK 抛不抛、形状由 `_as_rows` 兜底，⛔ 别抄写端点的 `code>0` 口径──
#: 系统操作日志：秒级 `stime`/`etime`；行里 `soureOrderId` = 来源单号、`joinOrderId` = 相关ID。⚠️ 实测两条硬口径
#: （`docs/JIDANPU-API.md` §4.7、`docs/RECON-operation-log.md` §1.4）：① `stime`/`etime` **必须带时分秒**（只给日期 ⇒ 静默 `count=0`，与「真的没有」同形）；② 起始日**没有**约 90 天的下限（`stime=2020-01-01` 实测正常）。
SYSTEM_LOG_PATH = "/SystemAjax/GetSysmteLogList"
#: 交易订单操作记录（plan §4.1）：**只吃 `yyyy-MM-dd`**（带时分秒 ⇒ HTTP 500），
#: 且 `sdate` 早于约 90 天 / 窗口宽到 120 天以上 ⇒ 静默 `count=0`。
TRADE_ACTION_LOG_PATH = "/OrderAjax/GetPushPlatfromOrderActionLogMangeList"
#: 两个日志端点各自的**时间格式**（⛔ 同名参数不等价，E-0007 / E-0038）。
SYSTEM_LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TRADE_LOG_DATE_FORMAT = "%Y-%m-%d"
#: 「全部」的筛选值（两个端点各自的实测口径：`logType=-1` / `actionValue=-1`）。
SYSTEM_LOG_ALL_TYPES = "-1"
TRADE_LOG_ALL_ACTIONS = "-1"
#: 每页请求多少行（说的是**交易日志**那个端点，与慢路 `ORDER_LIST_PAGE_SIZE` 不是同一件事）。
#: ⚠️ 保守取值、非实测上限：交易日志实测 `limit=2000` 正常返回（`.tools/recon/jdp_p2/report2.txt`），
#: 取 200 留余量；真被远端截断靠「累计 ≥ `count`」判「拉不全」⇒ 水位不推进（⛔ 不拿这个数当保障）。
LOG_PAGE_SIZE = 200


@dataclass(frozen=True)
class LogPage:
    """一个日志端点的一页（`count` = 响应顶层的**总行数**）。 ⚠️ `count` 是 `int | None`：`None` = 壳里没有**可用**的
    `count`（没给 / 负数 / 布尔）⇒ 判不了拉全 （fail-closed 同一条口径：调用方必须按「拉不全」处理，⛔ 不许拿它比大小）；**负数不是条数**
    （放行会让 `len(rows) < count` 恒假 ⇒ 半份数据判「拉全了」）；`0` 是**合法值** ⛔ 别与负数并一个分支。"""

    rows: tuple[dict, ...]
    count: int | None = None


def _log_page(client, path: str, query: dict) -> LogPage:
    """发**一次**日志查询并做**形状防御**（载荷必须是「全是对象的列表」）。 ⛔ 不判 `code`（见本节顶部的注释）：形状不对 ⇒ 响亮抛 `ApiError`（`_as_rows`
    口径）， 不静默当空列表——「回空」与「调用错」同形正是本线要防的坑（plan §3.2-1）； ⚠️ 没有**可用**的 `count`（没给 / 负数
    / 布尔）⇒ `None`：调用方（`jidanpu_logs._fetch_pages`） 按「拉不全」处理（理由见 `LogPage` 的 docstring）。"""
    payload = client.call("GET", path, query=query)
    envelope = client.last_envelope or {}
    count = envelope.get("count")
    # `bool` 是 `int` 的子类，而 `count: true` 不是条数（照 `_user_list_page` 的判据）；
    # 负数同样不是条数（修复波 1，与 `_reconcile_attempt` 同一条判据）。
    usable = isinstance(count, int) and not isinstance(count, bool) and count >= 0
    return LogPage(rows=_as_rows(payload, path), count=count if usable else None)


def fetch_system_log_page(
    client, *, start: datetime, end: datetime, page: int = 1, limit: int = LOG_PAGE_SIZE
) -> LogPage:
    """`GET /SystemAjax/GetSysmteLogList` 一页（参数按该端点自己的实测模板，`docs/JIDANPU-API.md` §4.7）。 ⚠️ 筛选键**全部显式给值**（漏传
    = 静默返回空集，同 `ORDER_LIST_KEY1_MANDATORY_PARAMS` 的 实测硬限）⇒ ⛔ 不靠「不传就等于不筛」；⚠️ `start` / `end`
    是**朴素本地时间**（上游回串不带 时区）、按 `SYSTEM_LOG_TIME_FORMAT` 逐字格式化 ⇒ ⛔ 传进来的值不要先做时区换算 （plan §6 把时区列为未闭合项）。"""
    query = {
        "stime": start.strftime(SYSTEM_LOG_TIME_FORMAT),
        "etime": end.strftime(SYSTEM_LOG_TIME_FORMAT),
        "gameName": "",
        "key1": "",
        "key2": "",
        "user": "",
        "logType": SYSTEM_LOG_ALL_TYPES,
        "page": page,
        "limit": limit,
    }
    return _log_page(client, SYSTEM_LOG_PATH, query)


def fetch_trade_action_log_page(
    client, *, start_day: date, end_day: date, page: int = 1, limit: int = LOG_PAGE_SIZE
) -> LogPage:
    """`GET /OrderAjax/GetPushPlatfromOrderActionLogMangeList` 一页。 ⚠️ `sdate` / `edate` **只吃 `yyyy-MM-dd`**（带时分秒
    ⇒ HTTP 500，实测）⇒ 本函数收 `date` 而不是 `datetime`：类型上就让人没法传时分秒。 ⚠️ `actionValue` / `actionUserId`
    / `key` **三个都必须显式给**：实测**漏传就 HTTP 500** （`.tools/recon/jdp_p2/report2.txt` 的 `miss_all_three`）。"""
    query = {
        "sdate": start_day.strftime(TRADE_LOG_DATE_FORMAT),
        "edate": end_day.strftime(TRADE_LOG_DATE_FORMAT),
        "actionValue": TRADE_LOG_ALL_ACTIONS,
        "actionUserId": "",
        "key": "",
        "page": page,
        "limit": limit,
    }
    return _log_page(client, TRADE_ACTION_LOG_PATH, query)


# ── P2-A 加法：绑定期检测（spec §10）──────────────────────────────────────────
# ⚠️ `inspect_binding` **一次建连、三个只读端点**（人员页 / 来源字典 / 可指派名单，各自建连会让每次
#    「测试连接」多两次登录）；本节**只读上游、不落库**：⛔ 不写 `last_ok_at` / `last_error`（那是同步引擎的列）。


def source_label(row) -> str:
    """一行来源在**我们这边**的名字（= 库里 `source_name` 存的那个值）——**唯一出处**。 实测形状（真机只读复核，原始记录只在**不进 git** 的
    `.tools/recon/source-picker/`）：`name` = **前缀字母 + `-` + 名字**、`value` 与「`-` 之后那一段」同值 ⇒ 名字取第一个 `-` 之后那段 （没有 `-` 的按整串算）；⚠️
    **两处必须读同一个函数**（`match_sources` 与 `/test-connection/` 出参的 `label`）——各切一份会漂移，用户选中的名字与库里保存的会**悄悄不一致**。"""
    raw = str(row.get("name", ""))
    _head, sep, tail = raw.partition("-")
    return tail if sep else raw


def source_options(rows) -> tuple[dict, ...]:
    """把来源行压成**页面下拉**要的形状：`{name, label}`。 `label` = **要显示、也是要保存**的名字（`source_label`）；`name` = 上游原始名（只作排障， ⛔ 页面不许拿它当保存值）；⛔ 不带
    `value`：真机实测它与 `label` **同值**——带上只会让人 拿它当落库值（口径是**仍存名字**）。"""
    return tuple(
        {"name": str(row.get("name", "")), "label": source_label(row)} for row in rows
    )


def match_sources(rows, wanted: str) -> tuple[dict, ...]:
    """在来源字典里按**名字**匹配（spec §10）。比较的是 `source_label`（第一个 `-` **之后**的那一段）与 `wanted`——判据与出参里的
    `label` **同一个出处**（见 `source_label` 的 docstring）。"""
    return tuple(row for row in rows if source_label(row) == wanted)


@dataclass(frozen=True)
class BindingChecks:
    """绑定页「测试连接」的全部读结果（spec §10）。**加法**：P1 的三个字段语义不变。 ⚠️ `sync_user_uid` / `sync_user_name` 照实回「请求里选中的是谁」，`sync_user_ok` 才是「可被指派」
    的判据（⛔ 不是一回事）；`assignable_truncated` = 这一页**可能不是全部**可指派人员 ⇒ 调用方必须 把话说老实（⛔ 不许把「这一页里没有他」说成「他不可被指派」）；`users_ok` = 「`GetUserList`
    那一跳 读到了没有」（非致命），`False` 时 `user_count` / `users_sample` 是**空值**、⛔ 不是「真的 0 人」 （`False` 是降级路径，真机探针没走到过它 ⇒ 由单元用例覆盖）。"""

    login_ok: bool
    #: 人员列表那一跳读到了没有（`False` = 降级，见类 docstring 最后一条）。
    users_ok: bool
    user_count: int
    users_sample: tuple[dict, ...]
    source_ok: bool
    source_value: str
    source_candidates: tuple[dict, ...]
    sync_user_ok: bool
    sync_user_uid: str
    sync_user_name: str
    candidates: tuple[dict, ...]
    assignable_truncated: bool = False
    #: ⭐ **对方来源字典的全量行**（D-0177）：页面下拉的**唯一**数据源。⚠️ 与 `source_candidates`
    #: 不是一回事（后者只是**与 wanted 匹配上的**那些行，既有的两条文案靠它）；本字段是**整份字典**
    #: （`source_options(rows)` 形状）。⛔ 别把两者合并——合并会让「同名多条」的计数变成 1。
    source_options: tuple[dict, ...] = ()


def inspect_binding(binding, *, account_uid: str, source_name: str = "") -> BindingChecks:
    """一次建连、三个只读端点，回答 spec §10 的两问（**只读上游、不落库**）。 ⚠️ **不落库**（要写身份的动作走 `inspect_binding_with_login_identity`，写库由动作做）； `source_name` 优先取**页面输入框此刻的值**（用户改名未保存时查库会漏报 / 假报）、空/缺省才回落 库里那份；`source_value` 只在**唯一命中**时给值（多命中留空让页面按 `source_candidates`
    提示， ⛔ 不替用户挑）；`sync_user_ok` 判据 = uid 在可指派名单里**且**该行 `isValid='Y'`（⛔ 不拿 `loginAcc` 匹配——实测全空；⛔ 不拿 `sta`——那是另一个端点的字段，照它判恒假 ⇒「能选人却说 不可被指派」，E-0005 / E-0007 族）；名单**只取一页** ⇒ 顶到页上限时 `assignable_truncated=True`、 视图必须说出来（⛔
    不改成分页取全——成本不划算）；⚠️ **人员列表那一跳非致命**（客服类同步账号 可能没有人员管理权限，两问走另外两个端点）⇒ 失败只降级 `users_ok=False`，⛔ 不判整条失败、 ⛔ 也不静默吞（降级如实回）、⛔ 别删那一跳（总人数与样本是白拿的信息）；⚠️ **`ConfigError` 不在降级范围**（那是我们的配置 / 清单错，显式 `raise`）；来源 / 可指派两跳照旧致命 （失败整条上行，视图翻 1016 / 1017）。"""
    with open_client(binding) as client:
        return _inspect_with_client(
            binding, client, account_uid=account_uid, source_name=source_name
        )


def inspect_binding_with_login_identity(
    binding, *, account_uid: str, source_name: str = ""
) -> tuple[BindingChecks, tuple[str, str] | None]:
    """**一次建连**：跑 `inspect_binding` 的三跳，再用**同一条连**解析登录账号身份。 ⭐ 合成一次建连：动作要在检测成功后把登录账号身份落库，再建一次连会多一次登录、还破掉
    「点一次测试连接只建一次连」的判据（隔离用例按 `call_count` 钉它）；⛔ **本函数仍然只读上游、 不落库**；身份那一跳**失败不报错**（上游异常 / `Id`
    读不出 / 显示名两处都没查到 ⇒ `identity=None`， 测试连接的结论照旧返回）；⛔ 不把 uid 单独落库（半边落地会清空 `account_name`、页面看着像配好了）。"""
    with open_client(binding) as client:
        checks = _inspect_with_client(
            binding, client, account_uid=account_uid, source_name=source_name
        )
        try:
            uid, name = resolve_login_identity(client)
            identity = (uid, name) if name else None
        except JidanpuError:
            identity = None
    return checks, identity


def _inspect_with_client(
    binding, client, *, account_uid: str, source_name: str = ""
) -> BindingChecks:
    """`inspect_binding` 的**同一条连**版本（调用方已建好连；⛔ 仍然只读上游、不落库）。
    ⚠️ 只把「建连」那一步挪出去（`inspect_binding` 与 `inspect_binding_with_login_identity`
    共用同一个身体）：读的三跳、降级口径、判据**一个字未改**（见 `inspect_binding` 的 docstring）。"""
    try:
        page = _user_list_page(client)
    except ConfigError:
        # ⛔ 不降级：这不是上游失败，是**我们**的配置 / 清单错（F5）。
        raise
    except JidanpuError:
        # 降级：只丢「人员共 N 人」，⛔ 不丢 `login_ok`（登录已经成功了才对）。
        page = None
    sources = fetch_sources(client)
    assignable = fetch_assignable_users(client)

    # ⚠️ **要检查的名字优先取调用方给的那个**（页面输入框此刻的值），空/缺省才回落库里那份
    #    ——理由见本函数的 docstring（F4：检查的就是你要保存的那个）。
    wanted = (
        (source_name or "").strip()
        or (binding.source_name or "").strip()
        or SOURCE_NAME_DEFAULT
    )
    hits = match_sources(sources, wanted)
    source_value = str(hits[0].get("value", "")) if len(hits) == 1 else ""

    wanted_uid = (account_uid or "").strip()
    sync_uid = ""
    sync_name = ""
    for row in assignable:
        if wanted_uid and str(row.get("id", "")) == wanted_uid:
            # 找到了就照实回 uid / 姓名（哪怕他停用了）：页面要说清「不可指派的是哪个人」。
            sync_uid = wanted_uid
            sync_name = str(row.get("userName", ""))
            break

    #: 同步账号的判据：**只认这一份名单 + 那一行的有效标志**（见 `ASSIGNABLE_ACTIVE_FIELD`）。
    sync_user_ok = bool(sync_uid) and any(
        str(row.get("id", "")) == sync_uid
        and str(row.get(ASSIGNABLE_ACTIVE_FIELD, "")) == "Y"
        for row in assignable
    )

    return BindingChecks(
        login_ok=True,
        users_ok=page is not None,
        # ⚠️ 降级时给的是**空值**（0 / 空元组），不是编一个「0 人」（见 `users_ok`）。
        user_count=0 if page is None else page.count,
        users_sample=() if page is None else page.rows[:USERS_SAMPLE_SIZE],
        source_ok=len(hits) == 1,
        source_value=source_value,
        source_candidates=hits,
        sync_user_ok=sync_user_ok,
        sync_user_uid=sync_uid,
        sync_user_name=sync_name,
        candidates=tuple(assignable[:CANDIDATES_LIMIT]),
        # 顶到页上限 ⇒ **可能**还有下一页（`>=` 而不是 `==`：桩给多了也不该说「这是全部」）。
        assignable_truncated=len(assignable) >= ASSIGNABLE_USERS_PAGE_SIZE,
        # ⭐ 整份字典（不只是命中集）：页面下拉靠它填（D-0177）。
        source_options=source_options(sources),
    )


# ── 本线加法：下游个人绑定的建连 + 页面可达性探测（spec §2.3 / §9.1 A13）────────────
# ⚠️ 本节只服务「团团通 → 下家鸡蛋铺（代练员个人绑定）」这条线（decision D-0161）；⛔ 既有 `open_client` 一字不改
#    （那一线已上线、读的是另一张表的 `base_url`，本线列叫 `jidanpu_base_url`，不能共用建连函数）；⚠️ 探页面**不能走 `client.call`**（页面回 HTML、SDK 按业务壳解包只会报「非 JSON 响应」）⇒ 把 HTTP 层交出来、由调用方直接看**状态码与 `Location`**（spec §2.3 实测判据）。

#: 无权限时服务端 302 跳到这里（spec §2.3 的**实测**判据，那个页面标题是「没有权限访问」）。
#: ⚠️ 有权限 = 200；⛔ 二者之外的任何结果都**判不了权限**（见 `probe_pages`）。
NO_PERMISSION_PATH = "/ErrorPage/nopermission"


class _PooledJidanpuClient(JidanpuClient):
    """Track SDK reauthentication even when the renewed cookie value repeats."""

    login_generation = 0

    def _on_unauthorized(self):
        # Both the SDK transport's 401/403 and the session-expired retry enter here.
        self.login_generation += 1
        super()._on_unauthorized()


def _open_client_pair(binding) -> tuple[JidanpuClient, httpx.Client]:
    """建一对**由调用方长期持有**的 `(client, http)`（⛔ 不是上下文管理器；用完就关的包装是 `open_client_with_http`）。 ⚠️ 要「不带 with」版本的原因（`docs/plans/2026-10-20-upstream-waf-risk-reduction.md` §2.1）：
    上家线要**跨轮复用同一个客户端**（同一个 cookie jar = 只登录一次）⇒ 池必须留住这一对； 凭据只从这一行取（S3）、登录是惰性的（建这一对本身**零网络**）；⛔ `follow_redirects=False` 必须显式给（跟在 302 后面就看不到 `nopermission`，会把「无权限」误判成「有权限」）；
    `User-Agent` 头必须显式给——承重面是**绕开 Transport 的裸 http 调用**（走 Transport 的请求本来 就有 UA setdefault，不给这个头就发 `python-httpx/x.y`）；SDK 构造失败时自己关掉 http （⛔ 不把「关」的责任丢给还没拿到这一对的调用方）。"""
    base_url = (binding.jidanpu_base_url or "").strip()
    account = (binding.account or "").strip()
    password = (binding.password or "").strip()
    if not base_url or not account or not password:
        raise ConfigError("鸡蛋铺地址 / 传令兵账号 / 密码不完整")
    config = JidanpuConfig(
        base_url=base_url, account=account, password=password,
        user_agent=BROWSER_USER_AGENT,
    )
    #: ⭐ 这一对用的是**哪个出口**（建连时取一次；468 的归属按它记）。
    transport = vendor_transport(config.api_base)
    proxy = None if transport is not None else proxy_url()
    network = {"transport": transport} if transport is not None else {"proxy": proxy}
    http = httpx.Client(
        base_url=config.api_base, timeout=config.timeout,
        follow_redirects=False, headers={"User-Agent": config.user_agent},
        **network,
    )
    try:
        client = _PooledJidanpuClient(config, http_client=http)
        # ⚠️ 本仓自己挂的引用（理由与 `open_client` 同一处）：给 ViewPic 那一跳
        #    套 per-request 超时；⛔ 不是 SDK 字段。
        client._tt_http = http
        #: ⭐ **这个客户端是哪个出口建出来的**：
        #: 468 的归属要按它记 —— ⛔ 不是「记账那一刻的当前出口」。同一轮里 A 出口被切走之后，
        #: 仍在用 A 客户端的那几段迟到的 468 **不许**把新出口 B 的计数推高（那会把健康出口误隔离）。
        client._tt_egress_outlet = proxy
    except Exception:
        http.close()
        raise
    return client, http


def close_client_pair(client, http) -> None:
    """关掉 `_open_client_pair` 建的那一对（⛔ 顺序承重：先 SDK、再 HTTP 层）。
    ⚠️ 为什么两样都要关：SDK 只关**自己**建的客户端（`Transport._owns_client` 为假时
    `close()` 什么都不做）⇒ 只调 `client.close()` 会泄漏连接池。"""
    try:
        client.close()
    finally:
        http.close()


@contextmanager
def open_client_with_http(binding):
    """本线专用：建一个**能拿到 HTTP 层**的 SDK 客户端，产出 `(client, http)`。 ⚠️ 为什么需要它：探页面可达性看的是**状态码 /
    `Location`**，而 SDK 的 `call` 按业务壳 解包、看不到这两样（页面是 HTML ⇒ 只会报「非 JSON 响应」）。 ⚠️ 建与关都在
    `_open_client_pair` / `close_client_pair` 里）：本函数只剩 「用完就关」这一层，下家那条线与既有调用点**一个字不用改**。"""
    client, http = _open_client_pair(binding)
    try:
        yield client, http
    finally:
        close_client_pair(client, http)


def _schema_name() -> str:
    """当前租户 schema 名（进客户端池的键 ⇒ 租户之间不串会话）。 ⚠️ 延迟 import 照 `jidanpu_openapi._schema_name` 的先例：本模块只在**真的建池键**时才需要
    Django 的连接对象，纯函数用例 / `manage.py check` 不该被 ORM 牵扯。"""
    from django.db import connection

    return getattr(connection, "schema_name", "") or ""


class UpstreamClientPool:
    """进程级 SDK 客户端池：同一绑定跨轮复用同一个 client（= 同一个 cookie jar = 一次登录）。 ⭐ 必要性：每轮新建客户端 ⇒ 每轮重新登录，登录密度曾把租户机 IP 逼到被对端 WAF 封 （实测两分钟 88 次登录 ≈ 6 万次/天，登录一律 468）⇒ 会话必须**跨轮**复用；会话过期**不**在 本类处理（SDK 自己在 302/401/403 上清会话 + 重登一次，⛔
    本类不重造那套判据）。键 = `(租户 schema, 绑定 pk, base_url, account, password)`：schema 必带（⛔ 租户边界——pk 是租户库内 自增，不带 schema 会把 A 租户的会话给 B 用；schema 只取 `connection.schema_name`）；基址 / 账号 进键（换账号 = 另一个上游身份）；密码进键（改密自然换新 client；⛔
    键含明文 ⇒ 不进日志、不进 异常文案、`__repr__` 不打印键）。加锁线程安全（⛔ 不拿「今天单线程」当依据）；池由**调用方**创建、 不做模块级全局单例（测试不串味）。已知边界（如实登记）：停用 / 删除绑定的客户端留到进程退出 （代价小、常驻进程随发版重启，⛔ 只有长期反复增删绑定且进程几个月不重启才需要 prune）； 会话过期后的重登**没有专门用例**（读码结论、非实测覆盖——如实挂账）。"""

    def __init__(self):
        self._lock = threading.Lock()
        #: 键 → `(client, http)`；⚠️ 键里有密码明文 ⇒ ⛔ 不在任何地方打印这个字典。
        self._pairs: dict[tuple[str, object, str, str, str], tuple] = {}
        #: 键 → 同一绑定的**串行锁**（web 接单：同一账号的会话不被两个线程同时用）。
        self._locks: dict[tuple[str, object, str, str, str], threading.Lock] = {}
        #: ⭐ **出口代际**：`mark_all_stale()` 自增一次 ⇒ 上一代建出来的客户端全部作废。
        #: ⚠️ 用代际号而不是一个 bool：标记之后新建的客户端属于新一代，⛔ 不会在下一轮又被判作废
        #: （否则每次 `acquire` 都重建 = 每轮重新登录，正是对端 WAF 封 IP 的老路）。
        self._generation = 0
        #: 键 → 那一对客户端是**哪一代**建出来的。
        self._pair_generation: dict[tuple[str, object, str, str, str], int] = {}

    def __repr__(self) -> str:  # pragma: no cover —— 只为「别把密码打出来」这条承诺
        # ⛔ **不打印键**：键里有密码明文（见类 docstring）。只报个数。
        return f"<UpstreamClientPool: {len(self._pairs)} 个客户端>"

    def _key(self, binding) -> tuple[str, object, str, str, str]:
        """这一行绑定的池键（⛔ 拼法只有这一处）；凭据不全 ⇒ `ConfigError`（文案不带值）。"""
        base_url = (binding.jidanpu_base_url or "").strip()
        account = (binding.account or "").strip()
        password = (binding.password or "").strip()
        if not base_url or not account or not password:
            raise ConfigError("鸡蛋铺地址 / 传令兵账号 / 密码不完整")
        return (_schema_name(), binding.pk, base_url, account, password)

    def _lock_for(self, key) -> threading.Lock:
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def mark_all_stale(self) -> None:
        """出口变了 ⇒ 池里**所有**客户端都作废（下一次 `acquire` 逐个关旧建新）。 ⚠️ **为什么是整池**：出口在建连时烙进客户端，只丢「报拦截那一条」的话，其余绑定继续从旧出口发请求、它们的
        468 会被记到**新**出口账上（健康出口被误隔离）；⚠️ **只打标记、⛔ 不在这里关**： 调用点可能在**段中间**，当场关会让后续段撞 `client has been closed`、被记成「某段未完成」+ 整轮退避（**假故障**，
        实测形态）——关闭 / 重建留给下一次 `acquire`（以及 `release_all`）； 两个池（上家 / 下家）各调一次（出口是进程级共享的）。"""
        with self._lock:
            self._generation += 1

    @contextmanager
    def session(self, binding):
        """借用**同一个**上家会话（同一绑定串行用），退出时不关闭客户端。 ⚠️ 给 **web 请求路径**（打手点接单）用：修之前每次接单都新建连接
        + 重新登录（集中抢单 N 个人 = N 次登录，正是 WAF 最易触发的形态），现在复用池里那一份。⚠️ **同一绑定串行**：
        SDK 的会话 / cookie 不为并发设计（两个线程同时用可能双登录或互相清会话）⇒ 每绑定一把锁 排成队列（接单本来就几秒，排队可接受）。"""
        key = self._key(binding)
        pair = self.acquire(binding)
        lock = self._lock_for(key)
        with lock:
            yield pair

    def invalidate(self, binding) -> None:
        """丢掉这一行绑定**当前**那一对客户端（下一次 `acquire` 会重建）。 ⚠️ 只在**登录被拒 / WAF 拦截**时由调度侧调用（`AuthError` /
        `UpstreamBlockedError`）—— 丢掉后 `_open_client_pair` 走 `proxy_url()` = **当前**出口（⭐
        出口是**粘性**的，只有连续拦截到 阈值才推进，单纯丢一次会话**不再**换出口）；⛔ 不在本方法里判断「该不该丢」（判据在调用点）、 ⛔ 不重建（留给 `acquire`）。"""
        key = self._key(binding)
        with self._lock:
            pair = self._pairs.pop(key, None)
            self._pair_generation.pop(key, None)
        if pair is not None:
            close_client_pair(*pair)

    def acquire(self, binding) -> tuple[JidanpuClient, httpx.Client]:
        """取这一行绑定的 `(client, http)`：已有就复用，没有就建一个并**留住**。 ⚠️
        返回的这一对**不归调用方关闭**（那正是复用的含义）——关在 `release_all()`。 ⚠️ 同一绑定**换了凭据**（密码
        / 账号 / 基址）⇒ 旧的那一对**当场关掉**再换新的 （⛔ 不然会泄漏连接池，而进程是常驻的）。"""
        key = self._key(binding)
        with self._lock:
            existing = self._pairs.get(key)
            # ⭐ 同一代的那一份才复用；`mark_all_stale()` 之后上一代的**一律换新**
            #    （出口换了 ⇒ 那一份还挂在旧 IP 上）。
            if existing is not None and self._pair_generation.get(key) == self._generation:
                return existing
            if existing is not None:
                self._pairs.pop(key, None)
                self._pair_generation.pop(key, None)
                close_client_pair(*existing)
            # ⚠️ 同一个绑定（同 schema + 同 pk）名下的**旧凭据**客户端一律关掉：
            #    键变了就意味着「这一行的上游身份换了」，旧客户端再也不会被用到。
            for stale_key in [
                other for other in self._pairs
                if other[:2] == key[:2] and other != key
            ]:
                self._pair_generation.pop(stale_key, None)
                close_client_pair(*self._pairs.pop(stale_key))
            pair = _open_client_pair(binding)
            self._pairs[key] = pair
            self._pair_generation[key] = self._generation
            return pair

    def release_all(self) -> None:
        """关掉池里所有客户端并清空池（进程退出 / `--loop` 结束 / `--once` 跑完时调一次）。 ⚠️
        幂等（池空什么都不做）；单个客户端关失败**不拦**后面的、warning 只写异常类型名 （`str(exc)` 可能带上游回包 / 凭据片段）；⛔
        **不往调用方抛**：本函数在 `finally` 里跑， 抛出去会盖掉真正的原因（例如那趟本来就是被 `Ctrl-C` 打断的）。"""
        with self._lock:
            pairs = list(self._pairs.values())
            self._pairs.clear()
            self._pair_generation.clear()
        for client, http in pairs:
            try:
                close_client_pair(client, http)
            except Exception as exc:  # noqa: BLE001 —— 见 docstring（收尾不在 finally 里抛）
                logger.warning(
                    "上游客户端池收尾时关闭失败，已跳过（进程即将退出）：%s",
                    type(exc).__name__,
                )


def probe_pages(http, paths: tuple[str, ...]) -> dict[str, bool]:
    """探「这些页面能不能打开」（spec §2.3 / §9.1 的 A13 判据；⛔ 只读，不写任何数据）。 判据（**实测**，spec §2.3）：`200` ⇒ **有权限**；`302`
    且 `Location` 指向 `NO_PERMISSION_PATH` ⇒ **无权限**；⛔ **其余一律算「检测本身失败」**（抛异常，不猜、不静默放行）——302 到登录页 /
    5xx / 连接失败都**不等于**「没有权限」，把它们说成没权限会让人去改一个没坏的权限设置； ⚠️ `http` 必须是设了 `base_url` 的那个客户端，路径一律以 `/` 开头。"""
    results: dict[str, bool] = {}
    for path in paths:
        try:
            response = http.get(path, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise TransportError(f"探测页面 {path} 时连接失败（{exc}）") from exc
        status = response.status_code
        location = response.headers.get("Location") or ""
        if status == 200:
            results[path] = True
        elif status == 302 and NO_PERMISSION_PATH in location:
            results[path] = False
        else:
            detail = f"，跳到 {location}" if location else ""
            raise ApiError(
                f"探测页面 {path} 得到 HTTP {status}{detail}：既不是正常页面、"
                "也不是无权限页 ⇒ 判不了权限",
                code=None,
            )
    return results


# ── 本线加法：系统操作日志（日志快路的取数端点，spec §2.2 / §6.1）：与租户级大厅线零关系（只服务「团团通 → 下家鸡蛋铺」线，D-0161）⛔ 不要接进 `run_round`；⛔ 拼写就是 `GetSysmteLogList`（对标自己的拼写，实测）——别"修正"成 `GetSystemLogList`（那是一个 404，读端点的宽松判据只会把它当成普通调用失败）；本端点未登记在 SDK 清单 ⇒ 照 `ASS_LOGS_PATH` 先例：不传 `success_rule=`（未登记 ⇒ `lenient`）、形状防御在我们这边（`fetch_system_logs` 走 `_as_rows`）。

#: 系统操作日志：`GET`、`stime` / `etime` 格式**带时分秒**、时间窗**无上限**（spec §2.2 实测）。
SYSTEM_LOG_PATH = "/SystemAjax/GetSysmteLogList"
#: 该端点的时间参数格式（**实测**，spec §2.2）。⚠️ 同产品的**另一个**日志端点
#: （`GetPushPlatfromOrderActionLogMangeList`）只吃 `yyyy-MM-dd`、传时分秒直接 500
#: ⇒ 两个端点**不共用参数模板**（E-0007）。本模块只定义这一个端点自己的格式。
SYSTEM_LOG_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
#: 订单相关的 `logType`（spec §2.2：本线只追订单动作；`0` / `-1` 是"全部"）。
SYSTEM_LOG_ORDER_TYPE = 10


def format_log_time(moment) -> str:
    """把时刻写成该端点吃的那种**带时分秒**的字符串（业务时区，⛔ 不是 UTC 墙钟）。
    ⚠️ 唯一出处：`poll_log` 的游标与 `fetch_system_logs` 的 `etime` 都走它
    ——两处各写一份 `strftime` 必然漂移（一处改了另一处没改 ⇒ 窗口错位、静默漏行）。"""
    return timezone.localtime(moment).strftime(SYSTEM_LOG_TIME_FORMAT)


def fetch_system_logs(
    client, *, stime: str, log_type: int = SYSTEM_LOG_ORDER_TYPE, etime: str | None = None
) -> tuple[dict, ...]:
    """取系统操作日志（spec §2.2 / §6.1 的日志快路取数）。 ⚠️ `etime` 缺省 = **此刻**（业务时区，`format_log_time`）；调用方（`poll_log`）通常只给
    `stime` （游标）。⚠️ **形状防御**：载荷必须是「全是对象的列表」，否则响亮抛 `ApiError`（1017，`_as_rows`
    口径）——⛔ 不静默当空列表（那会把「上游改版 / 调用错」说成「这段时间没有日志」）；成败**只**看 SDK 抛不抛（⛔ 不看 `last_envelope["code"]`）。"""
    query = {
        "stime": stime,
        "etime": etime if etime is not None else format_log_time(timezone.now()),
        "logType": log_type,
    }
    return _as_rows(client.call("GET", SYSTEM_LOG_PATH, query=query), SYSTEM_LOG_PATH)


# ── T6：**单条订单回溯**的取数端点（plan `2026-09-26-jidanpu-inbound-discovery` §3.1 / §5-T6）：本节函数**只给人看**（排障 / 回溯），⛔ 不进任何自动判据、⛔ 不当触发器（plan §3.3-3）；与 `SYSTEM_LOG_PATH` 那两个「快路」端点**零关系**（那两个能按时间增量拉、行里带对单键，这一个只能按单查、行里没有任何能对上我方单的键，T1 实测 plan §3.1）。
#: 订单详情页「订单日志」区的端点（那串 `【时间】【操作人】动作` 就是它）。⚠️ **POST-only**（`GET` 在本产品上不存在，T1 实测七种参数组合全 404）⇒ 只能用 `POST`；⚠️ 未登记在 `spec/endpoints.json` ⇒ 与 `ASS_LOGS_PATH` 同先例：不传 `success_rule=`（未登记 ⇒ `lenient`）、形状防御在我们这边（`fetch_order_logs` 走 `_as_rows`）。
ORDER_LOGS_PATH = "/OrderAjax/GetOrderLogs"
#: 页面上 `#viewType` 隐藏域的默认值（`orderview.js` 把它原样塞进 POST body）。
#: ⚠️ **收下但忽略语义**：实测 `10` / `20` / 空串回**同样**的行（plan §6-5）⇒ 只照页面默认值给，
#: ⛔ 不按它做任何分支、⛔ 不当判据。
ORDER_LOGS_VIEW_TYPE = 20


@dataclass(frozen=True)
class OrderLogPage:
    """`GetOrderLogs` 的一次答复。 ⚠️ `envelope_code` **只作展示**：实测两个样本里它疑似「行数」（`code:6` ↔ 6 行，**推断级**） ⇒ ⛔ 谁都不许拿它判成功 / 失败 /
    判空（判据是 `data` 列表本身）；`view_type` 照实回显 「这次发的值」——语义未定（收下但忽略）。"""

    rows: tuple[dict, ...]
    envelope_code: int | None = None
    view_type: int = ORDER_LOGS_VIEW_TYPE


def fetch_order_logs(
    client, *, remote_order_id, view_type: int = ORDER_LOGS_VIEW_TYPE
) -> OrderLogPage:
    """取**一张单**的订单日志（plan §3.1 的两个实测硬限都在这里）。 ⚠️ **只能按单查**（没有时间窗 / 分页 / 游标，参数全部被忽略，T1 实测）⇒ 只收一个单号； **`orderId` 必给**——缺 / 错 ⇒ 上游回 `200` + `code=0` +
    **空数组**，与「这张单真的没有日志」 同形 ⇒ 发请求前就拒空单号（`ValueError`：这是调用错，不是查不到）；**行里没有对单键** （`Order_ID` / `LogType` 每行都是 `null`，T1 实测）⇒ 调用方自己持 `orderId` （走
    `jidanpu_logs.remote_order_keys`）；⛔ **不看 `code` 判成败**（plan §3.2-2 / E-0087， 那是展示字段）；端点未登记 ⇒ SDK 不校验形状 ⇒ 形状错必须在这里响亮（`_as_rows`）、 ⛔ 不能静默当空列表。"""
    remote_order_id = str(remote_order_id or "").strip()
    if not remote_order_id:
        raise ValueError(
            "orderId 不能为空：本端点没有单号时回 200 + 空数组，与「这张单没有日志」同形"
        )
    payload = client.call(
        "POST", ORDER_LOGS_PATH, data={"orderId": remote_order_id, "viewType": view_type}
    )
    envelope = client.last_envelope or {}
    code = envelope.get("code")
    return OrderLogPage(
        rows=_as_rows(payload, ORDER_LOGS_PATH),
        # `bool` 是 `int` 的子类，而 `code: true` 不是行数（照 `_user_list_page` 的判据）。
        envelope_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        view_type=view_type,
    )


#: web 请求路径（接单）用的**进程级**会话池：一个 gunicorn worker 进程一份。
#: ⚠️ 与 `jidanpu_sync` 命令里的池是**两个对象**（两个进程，各管各的会话）；
#: 每个进程各自只登录一次/绑定，已经足够把「每次接单一次登录」降下来。
_SHARED_POOL: "UpstreamClientPool | None" = None
_SHARED_POOL_LOCK = threading.Lock()


@contextmanager
def claim_session(binding):
    """接单那一刻的上家会话（**进程级复用** + 同一绑定串行）。 ⚠️ `jidanpu_upstream._claim_upstream`
    的**唯一取 client 口**（测试也按它打桩）；修之前那里 直接 `open_client(binding)`
    ⇒ 每次接单都新建连接 + 重新登录。`AuthError`（登录被拒 / 会话失效） ⇒ 丢掉这一份，下一次调用按当前出口重建。"""
    pool = shared_pool()
    try:
        with pool.session(binding) as (client, _http):
            yield client
    except AuthError:
        pool.invalidate(binding)
        raise


def shared_pool() -> "UpstreamClientPool":
    """取 web 进程共用的会话池（懒建；线程安全）。"""
    global _SHARED_POOL
    with _SHARED_POOL_LOCK:
        if _SHARED_POOL is None:
            _SHARED_POOL = UpstreamClientPool()
        return _SHARED_POOL


#: 下家①式（代练员自己绑的鸡蛋铺）用的会话池：**与上家池分开**（两条线的绑定表与失效口径不同）。
_SHARED_MEMBER_POOL: "UpstreamClientPool | None" = None
_SHARED_MEMBER_POOL_LOCK = threading.Lock()


def member_pool() -> "UpstreamClientPool":
    """取下家①式的进程级会话池（懒建；线程安全）。"""
    global _SHARED_MEMBER_POOL
    with _SHARED_MEMBER_POOL_LOCK:
        if _SHARED_MEMBER_POOL is None:
            _SHARED_MEMBER_POOL = UpstreamClientPool()
        return _SHARED_MEMBER_POOL


@contextmanager
def member_session(binding):
    """下家①式的一对 `(client, http)`：**跨轮复用同一份登录会话**。 ⚠️ 修之前 `OutboxLine.spawn_client`
    每轮有事件就新建连接 + 登录一次；它和上家共用出口 IP， 失败重试的登录密度会推高 WAF 评分——现在复用池里那一份，`AuthError`
    ⇒ 丢掉并按当前出口重建。 ⚠️ 早退不受影响：`run_outbox` 在**事件为空**时于 `spawn_client` 之前就返回（零登录）。"""
    pool = member_pool()
    try:
        with pool.session(binding) as pair:
            yield pair
    except AuthError:
        pool.invalidate(binding)
        raise
