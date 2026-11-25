"""下游鸡蛋铺线的**状态回拉（慢路）+「立即同步」端点**
（plan `2026-10-03-member-status-scope` Task B 的新形态；spec §6.2 / §9.3）。

新形态 = **整店拉「已完成」那一档（逐页拉全）→ 按远端 `orderId` 匹配链路 → 只写「待验收」**。

被钉住的契约：

- **取数**：筛 `orderSta=已完成`、时间维度 = **完成时间**（`timeType=30`，⛔ 不是录单时间）；
  窗口 = **增量与首次同一档，都回看 3 天**（业务方 2026-11-07 拍板；实测铺侧完成档集中北京时间
  零点后才可匹配，只看水位的窗在本地日翻篇后不再回看 = 永久漏单）：有水位 ⇒
  `since = 水位 − 3 天`；无水位（首次 / 被清空）⇒ `now − 3 天`；水位很旧 ⇒ 窗**自然变大**
  （⛔ 不被往前压回）。实发日期串的跨度再卡在
  460 天以内（服务端硬限，plan §2 实测；⚠️ `stime` 向下取整到日、`etime` 向上取整到次日末 ⇒
  卡口要留死区，见 `test_a_very_old_watermark_still_sends_a_window_within_the_server_cap`）；
- **处理集合**：只留「我方还是**非终态**」的单——`COMPLETED` / `REFUNDED`
  （`PULL_TERMINAL_STATUSES`）与**已软删**的单都不进集合 ⇒ 它们**一个字节都不写**。
  ⚠️ 「**已退单**不写」是**钱的承重判据**：把退掉的单写成「待验收」= 复活一张退单，
  而退款那条路是裸 `update()`、跑不到退款结清 ⇒ 钱会算错；
- **写回**：远端那一行 `orderId` 逐字命中链路 **且** `orderSta == 已完成` ⇒ 我方写
  `待验收`（`PENDING_REVIEW`）；我方已经是待验收 ⇒ **一个字都不写**（幂等，第二轮零写）；
  命中即写链路行的 `last_in_at`（语义 = 「上次**拉取到**这张单的时刻」，见
  `test_a_successful_round_advances_the_watermark` 的注释）；
- ⭐ **订单截图拉回来（B 段）在本网里显式让开**（`_stub` 把
  `jidanpu_member._pull_remote_screenshots` 换成「没有失败」的那一跳，2026-10-05 加）：
  本网钉的是**状态回写与水位**，而那一跳需要「答得出 `ViewPic`」的替身；它的形状由
  `tests/test_jidanpu_member_pics.py` 专网钉（入库两种图种 / 幂等 / 两种信封 /
  拉图失败仍写「待验收」），⛔ 不是「本网看不见就不测」；
- **写前复核**（第二道闸）：用库里**最新**那一行再跑一次
  `jidanpu_hall._b1_status_rollback_blocked`（⛔ 不另写一份「已结账 / 已退单」的判据）——
  「取集合 → 拉整表 → 写」之间隔着网络往返；
- **缺节点 / 状态机拒绝 ⇒ 响亮**（台账 + `last_error`），⛔ 不静默跳过；
- **水位**：只有「**拉全 且 没有 loud**」才推进 `status_pull_at = now`（否则下一轮窗口自然变大
  ⇒ 不漏）；⚠️ **处理集合为空时也推进**（plan §4 Task B-补 第 3 条：集合空 ⇒ 现存链路没有一张
  需要拉 ⇒ 不推进的话，长期闲置之后第一次有单时会从很旧的水位算起、一次拉回最长的整店
  「已完成」）；**拉不全 / 上游报错 ⇒ 本轮一行都不写**（照 `jidanpu_reconcile` 的同一条口径）；
- **`map_remote_status` / `REMOTE_STATUS_MAP` / `REMOTE_STATUS_LABELS` /
  `is_known_remote_status` / `UNKNOWN_REMOTE_STATUS`**：本线**不再用它们拉状态**，但**都保留**
  ——两张表是**上家线的底表**（`jidanpu_upstream.py` 并入 / 直接调用），而
  `map_remote_status` 本模块仍在用（`_adopted_throw_verdict` 的认领裁决）
  ⇒ 它们那一组用例**留在这里**（⛔ 不是「本模块没人用就删」；`AGENTS.md` §5-E / E-0029 同族）；
- `reconcile_and_queue`：「对账 ⇒ 排事件」**只有一份实现**，视图的补推调它
  （⛔ 不许在视图里内联第二份；它是漏单的最后一道兜底，Task 10 的定时循环也调它）；
- `POST /api/business/member/jidanpu-sync-now/`：**只作用于 `request.user` 那一行**
  （⛔ 不接受任何用户 / 租户参数）、内部走**与慢路完全相同**的 `pull_statuses`；
  频控 = 同会话最小间隔 + 在途互斥 + 失败退避（P2 spec §17.6 的形状）。

⚠️ 断言一律打**数据库行** / **桩收到的调用**（⛔ 不断言 logger，E-0009）；
桩打在**被查找的模块** `apps.business.jidanpu` 上（E-0029）——**新形态查的是
`jidanpu.fetch_completed_orders_page`**（旧形态那套 `fetch_order_by_key1` 的桩已随逐单循环删掉；
桩打错模块 = 恒真的假绿）。
⚠️ 夹具一律**合成值**（E-0077）、零真实网络。期望值一律写字面量（⛔ 不自指，E-0024）。
"""
from __future__ import annotations

import ast
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_member
from apps.business.api import jidanpu_member_views
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
    SettleStatus,
)
from tests.p5_auth_helpers import make_logged_in_client

#: ⚠️ 客户端级桩（`client.call` + `client.last_envelope` 两个出口）**形状只此一处**：
#: 直接引 Task A 那一网里那份（它自己注明形状照 `tests/test_jidanpu_reconcile.py` 的
#: `_OrderListStub`），⛔ 不在这里凭印象另造一个假 client——造错了等于测一个不存在的客户端。
from tests.test_jidanpu_member_status_watermark import _OrderListStub
from tests.test_operation_log_ledger import INSTRUMENTED

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

BASE_URL = "https://jidanpu-a.test.invalid"
BINDING_USER = "member-status-a"
OTHER_USER = "member-status-b"
ORDER_NO = "20260926000000004001"
ORDER_NO_B = "20260926000000004002"
JID = "JDP-00000000000000004001"
JID_B = "JDP-00000000000000004002"
MACHINE_ACTOR = "下家鸡蛋铺同步"

#: ⭐ 兜底（①式回拉覆盖同店②式链路）的夹具值（全部合成，E-0077）。
API_USER = "member-status-api-a"
API_BARE_USER = "member-status-api-bare"
API_OTHER_USER = "member-status-api-other"
API_CLIENT_ID = "synthetic-member-status-client"
API_CLIENT_SECRET = "synthetic-member-status-secret"
#: 兄弟行（②式那条）默认挂的三个节点（与 `_Base` 里①式那条**同一组**：改状态 + 完单 + 大厅接单）。
SIBLING_NODES = (
    "order.change_status", "order.change_status_completed", "grab_hall.take_order",
)
#: 一条**没有挂任何节点**的职位（组名 ≤ 5 字 = `Role.name` 的列宽）。
#: ⚠️ 不复用「客服」：那个职位在本类 setUp 里被挂过改状态节点（`Role` 按组名共享）。
NO_NODE_GROUP = "无权限"
#: **另一家店**的下家地址（合成值；不同店的单⛔ 不许被覆盖）。
OTHER_BASE_URL = "https://jidanpu-b.test.invalid"
OTHER_ORDER_NO = "20260926000000004003"
OTHER_JID = "JDP-00000000000000004003"

SYNC_NOW_URL = "/api/business/member/jidanpu-sync-now/"
BINDING_URL = "/api/business/member/jidanpu-binding/"

#: 业务码字面量（⛔ 不从 `ErrorCode` 派生——自指断言在常量被改坏时不会红，E-0024）。
CODE_OK = 0
CODE_BUSINESS = 1002
CODE_NOT_CONFIGURED = 1015

#: 台账 kind 字面量（spec §4.3）。
KIND_STATUS_IN = "member.status_in"

#: 审计动作字面量：状态回拉走的就是「订单状态迁移」这件事（与 PUT /transition/ 同一句文案）。
AUDIT_ACTION = "order.transition"

#: `member.status_in` 台账里必须出现的人话片段（运维要能看懂发生了什么）。
REASON_INCOMPLETE_HINT = "没拉全"
REASON_UPSTREAM_HINT = "上游报错"
REASON_NODE_HINT = "节点"
REASON_BLOCKED_HINT = "挡住"

#: 远端那一档的筛选值（本端点的 `orderSta` 参数就是**中文名本身**）。
REMOTE_COMPLETED_LABEL = "已完成"
#: 本路写回的目标态（下游已完成 ⇒ 我方「待验收」）。
TARGET_STATUS_LABEL = "待验收"


def _row(order_sta: str = REMOTE_COMPLETED_LABEL, *, order_id: str = JID) -> dict:
    """一行**真实形状**的订单列表回包（字段名取自 spec 的 82 字段表；全是合成值）。

    ⚠️ 新形态走的是**整表列表**（`GetOrderList`）⇒ 行里承重的只有 `orderId`（与链路行逐字
    相等）与 `orderSta`（实做里那句「命中 **且** `orderSta == 已完成`」是 fail-closed 的第二道筛）。
    """
    return {"orderId": order_id, "orderSta": order_sta, "isThrowOrder": "N"}


class _Remote:
    """`apps.business.jidanpu.fetch_completed_orders_page` 的脚本替身（只有这一个出口）。

    ⚠️ 桩打在**被查找的那个模块** `apps.business.jidanpu` 上（E-0029）：新形态查的是
    `jidanpu.fetch_completed_orders_page`；旧形态那套 `jidanpu.fetch_order_by_key1` 的桩
    已随逐单循环删掉。
    """

    def __init__(self, rows=None, *, count=None, error=None, on_call=None):
        self.rows = [dict(row) for row in (rows or [])]
        #: `None` ⇒ 总数 = 回包行数（= 「拉全」的常态）；显式给 ⇒ 原样当回包 `count`
        #: （替身自己算 `len(rows)` 就永远对得上 ⇒ 「拉不全」那几条会恒真）。
        self.count = len(self.rows) if count is None else count
        self.error = error
        #: 每次翻页前跑一下（用来构造「取集合 → 拉整表 → 写」窗口里的竞态）。
        self.on_call = on_call
        #: 每次翻页记一条（判据打在**发出去的窗**上）。
        self.calls: list[dict] = []

    def fetch_completed_orders_page(
        self, client, *, window, page=1, limit=1000, game_name="全部游戏"
    ):
        self.calls.append(
            {"window": dict(window), "page": page, "limit": limit, "game_name": game_name}
        )
        if self.on_call is not None:
            self.on_call()
        if isinstance(self.error, Exception):
            raise self.error
        start = (page - 1) * limit
        return jidanpu.OrderListPage(
            rows=tuple(self.rows[start:start + limit]), count=self.count
        )


class _WindowedRemote:
    """按**发出去的那个窗**筛行的替身（模拟服务端按完成时间过滤）。

    ⚠️ `_Remote` 不管窗是什么都回同一批行 ⇒ 它只能证明「窗被发出去了」，证明不了
    「窗边界前 1 分钟完成的那张单**真的被拉到了**」。本替身只服务边界重叠那一条用例。
    """

    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.calls: list[dict] = []

    def fetch_completed_orders_page(
        self, client, *, window, page=1, limit=1000, game_name="全部游戏"
    ):
        self.calls.append(dict(window))
        start = datetime.strptime(window["stime"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(window["etime"], "%Y-%m-%d %H:%M:%S")
        rows = [row for row in self.rows if start <= row["completed_at"] <= end]
        return jidanpu.OrderListPage(rows=tuple(rows), count=len(rows))


@contextmanager
def _stub(remote: _Remote, *, pull_screenshots=None):
    """把「订单列表那一跳」换成替身，**并让开 B 段的拉图那一跳**。

    ⚠️ 打桩位置：`jidanpu.fetch_completed_orders_page`（**被查找的那个模块**，E-0029）。

    ⚠️ **为什么本网默认让开 B 段（订单截图拉回来）那一跳**（加）：
    `_pull_statuses` 现在在写「待验收」之前会打一次 `ViewPic`（计划
    `2026-10-05-member-order-pics` 的 B 段）。本网钉的是**状态回写与水位**，它用的那些替身
    （`_Remote` 只答订单列表、真 SDK 客户端没有可用的对端）会让那一跳变成一次**真网络请求**
    + 一条「拉图失败」台账，把本网的判据（`_notes(...).get()` / `last_error`）搅浑。
    ⇒ 这里把 `jidanpu_member._pull_remote_screenshots` 显式让开（`return_value=None`
    = 「没有失败」）；**B 段自己的形状**（入库两种图种 / 幂等 / 两种信封 / 失败仍写待验收）
    由 `tests/test_jidanpu_member_pics.py` 专网钉，⛔ 不是「不测了」。
    ⚠️ `pull_screenshots=` 给一个字符串 ⇒ 那一跳当**失败**返回（用来钉「失败也不挡」）。
    """
    with mock.patch.object(
        jidanpu, "fetch_completed_orders_page",
        side_effect=remote.fetch_completed_orders_page,
    ), mock.patch.object(
        jidanpu_member, "_pull_remote_screenshots", return_value=pull_screenshots,
    ):
        yield


class _Clock:
    """可推进的假时钟（**只为闸门**注入；`pull_statuses` 的 `timezone.now()` 不受影响）。

    ⚠️ API 级用例必须能控制时间：频控的最小间隔（2 秒）与退避（5/15/60 秒）都是
    墙钟行为，不注入时钟就只能靠 `sleep`（慢且脆）。
    """

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def _wait_seconds(message: str):
    """从闸门的人话里取出「还要等几秒」（`…请 N 秒后再试`）。取不到 ⇒ `None`。"""
    match = re.search(r"(\d+) 秒", message or "")
    return None if match is None else int(match.group(1))


# ══ ① 映射表：**保留**（上家线的底表 + 本模块 `_adopted_throw_verdict` 仍在用）═══════


class MapRemoteStatusTests(SimpleTestCase):
    """`map_remote_status`：码 → 我方状态（spec §7；码表出自 `docs/JIDANPU-API.md` §6.1）。

    ⚠️ **本线（收窄）不再用它拉状态**，但这一组**留在原处**：
    ① 两张表（`REMOTE_STATUS_MAP` / `REMOTE_STATUS_LABELS`）是**上家线的底表**
       （`jidanpu_upstream.py:1767/1772` 把本模块这两张表**并入**它自己的表）；
    ② 本模块 `_adopted_throw_verdict`（认领裁决：用它认出「未分配」⇒ 补抛；「已退单」
       自 D-0257起先改回未分配再补抛，其余档不碰）**仍在调它**
       ⇒ ⛔ 「本模块没人用了就删」这条推理不成立（`AGENTS.md` §5-E 的同族教训）。
    """

    #: 码 → 我方状态（**字面量期望值**；含 spec §7 的每一档与旧 spec §7.2 的池内两态）。
    CODES = (
        ("WFP", "UNASSIGNED"),
        ("DSH", "IN_PROGRESS"),
        ("DLZ", "IN_PROGRESS"),
        ("DYS", "PENDING_REVIEW"),
        ("YWC", "COMPLETED"),
        ("YZT", "PAUSED"),
        ("WTD", "ISSUE"),
        ("YTD", "REFUNDED"),
        ("CXZ", "ISSUE"),
        ("ZCZ", "ISSUE"),
        ("YZC", "ISSUE"),
        ("YCX", "ISSUE"),
        ("YCZ", "ISSUE"),
    )

    #: 远端 `orderSta` **实际下发的**是中文标签（spec §5.7 实测）⇒ 中文也要认。
    LABELS = (
        ("未分配", "UNASSIGNED"),
        ("待上号", "IN_PROGRESS"),
        ("代练中", "IN_PROGRESS"),
        ("待验收", "PENDING_REVIEW"),
        ("已完成", "COMPLETED"),
        ("已暂停", "PAUSED"),
        ("问题单", "ISSUE"),
        ("已退单", "REFUNDED"),
        ("撤销中", "ISSUE"),
        ("仲裁中", "ISSUE"),
        ("已仲裁", "ISSUE"),
        ("已撤销", "ISSUE"),
        ("异常中", "ISSUE"),
    )

    def test_every_code_of_the_table(self):
        for token, expected in self.CODES:
            with self.subTest(token=token):
                self.assertEqual(str(jidanpu_member.map_remote_status(token)), expected)

    def test_every_chinese_label_of_the_table(self):
        for token, expected in self.LABELS:
            with self.subTest(token=token):
                self.assertEqual(str(jidanpu_member.map_remote_status(token)), expected)

    def test_the_dsh_merge_is_not_pending_review(self):
        """业务方 A12：`DSH` 是**待上号**（不是「待审核」）⇒ 合并进代练中（E-0005）。"""
        self.assertEqual(jidanpu_member.map_remote_status("DSH"), "IN_PROGRESS")
        self.assertNotEqual(jidanpu_member.map_remote_status("DSH"), "PENDING_REVIEW")

    def test_an_unknown_code_is_issue_and_never_completed(self):
        """兜底（spec §7 末行）：认不出来 ⇒ 问题单；⛔ 绝不猜成已完成。"""
        self.assertEqual(jidanpu_member.map_remote_status("ZZZ"), "ISSUE")
        self.assertNotEqual(jidanpu_member.map_remote_status("ZZZ"), "COMPLETED")
        self.assertFalse(jidanpu_member.is_known_remote_status("ZZZ"))
        self.assertTrue(jidanpu_member.is_known_remote_status("YWC"))

    def test_the_five_cancelled_codes_all_land_on_issue(self):
        for token in ("CXZ", "ZCZ", "YZC", "YCX", "YCZ"):
            with self.subTest(token=token):
                self.assertEqual(jidanpu_member.map_remote_status(token), "ISSUE")


# ══ ② `pull_statuses`（慢路）═══════════════════════════════════════════════════


class _Base(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.game = Game.objects.create(name="合成游戏甲")
        # ⚠️ 三个节点都是承重的：`order.change_status` = 通用改状态；
        #    `order.change_status_completed` = 改「已完成」；`grab_hall.take_order` = 大厅接单。
        self.user_client = make_logged_in_client(
            username=BINDING_USER,
            groups=("代练员",),
            nodes=(
                "order.change_status",
                "order.change_status_completed",
                "grab_hall.take_order",
            ),
        )
        self.user = User.objects.get(username=BINDING_USER)
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-status-a",
            account_name="传令兵甲",
            password="pw-status-a",
            source_name="团团通系统",
            enabled=True,
        )

    # ── 工具 ─────────────────────────────────────────────────────────────
    def _order(self, *, order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS, taker=True,
               settled=False):
        order = Order.objects.create(
            game=self.game,
            order_no=order_no,
            status=status,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目甲",
        )
        Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            user=self.user if taker else None,
            temp_amount=Decimal("12.34"),
            settle_status=SettleStatus.SETTLED if settled else SettleStatus.UNSETTLED,
        )
        return order

    def _link(self, order, *, remote_id=JID, in_hall=False):
        return JidanpuMemberOrderLink.objects.create(
            binding=self.binding,
            order=order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    def _notes(self, order):
        return JidanpuSyncEvent.objects.filter(
            kind=KIND_STATUS_IN, target_ref=order.order_no
        )

    def _round_notes(self):
        """**整轮失败**那条台账（没有具体单可指认 ⇒ `target_type="binding"`）。"""
        return JidanpuSyncEvent.objects.filter(
            kind=KIND_STATUS_IN, target_ref=str(self.binding.pk)
        )

    def _pull(self, remote: _Remote, *, now=None):
        with _stub(remote):
            return jidanpu_member.pull_statuses(self.binding, now=now or self.now)


class PullStatusesTests(_Base):
    def test_completion_rechecks_the_link_and_taker_after_picture_download(self):
        replacement = User.objects.create_user(username="synthetic-new-owner")
        for index, change in enumerate(("withdraw", "remote_id", "retire", "taker")):
            with self.subTest(change=change):
                order = self._order(order_no=f"2026111900000000{index:04d}")
                link = self._link(order, remote_id=f"SYNTHETIC-{change}")

                def change_during_pull(**kwargs):
                    if change == "taker":
                        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(user=replacement)
                    else:
                        field, value = {
                            "withdraw": ("withdrawn_at", self.now),
                            "remote_id": ("jidanpu_order_id", "SYNTHETIC-REPLACED"),
                            "retire": ("retired_at", self.now),
                        }[change]
                        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(**{field: value})
                    return None

                with mock.patch.object(jidanpu_member, "_pull_remote_screenshots", side_effect=change_during_pull):
                    outcome = jidanpu_member.apply_remote_completion(
                        binding=self.binding, order=order, link=link, remote_status="已完成",
                        nodes=frozenset({"order.change_status", "order.change_status_completed"}),
                        now=self.now, client=object(),
                    )
                self.assertEqual(outcome.verdict, "skipped")
                order.refresh_from_db()
                self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_withdrawn_copy_cannot_complete_an_order_after_reassignment(self):
        order = self._order()
        link = self._link(order)
        link.withdrawn_at = self.now
        link.save(update_fields=["withdrawn_at"])
        replacement = User.objects.create_user(username="synthetic-replacement")
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(user=replacement)
        self.assertEqual(self._pull(_Remote([_row()], count=1)), 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)


    def test_it_writes_pending_review_when_the_downstream_order_is_completed(self):
        """⭐ 新形态的主路径：下游**已完成** + 我方**代练中** ⇒ 写「待验收」。

        判据全部打在**终态**上：订单行的 `status`、`order.transition` 审计的前后值、
        `member.status_in` 台账（`done`）——⛔ 不打 logger（E-0009）。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        link = self._link(order)
        remote = _Remote([_row()], count=1)

        applied = self._pull(remote)

        self.assertEqual(applied, 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)
        # 留痕：机器操作人 + 状态迁移那一句（与 PUT /transition/ 同一动作）。
        log = OperationLog.objects.order_by("id").last()
        self.assertEqual(log.action, AUDIT_ACTION)
        self.assertEqual(log.actor, MACHINE_ACTOR)
        self.assertEqual(log.order_no, ORDER_NO)
        self.assertEqual(log.from_value, "代练中")
        self.assertEqual(log.to_value, TARGET_STATUS_LABEL)
        # 台账：一条 `done`（人话）。
        note = self._notes(order).get()
        self.assertEqual(note.status, "done")
        self.assertIn(REMOTE_COMPLETED_LABEL, note.reason)
        self.assertIn(TARGET_STATUS_LABEL, note.reason)
        #: 命中的链路写「上次**拉取到**这张单的时刻」（收窄写入）。
        link.refresh_from_db()
        self.assertEqual(link.last_in_at, self.now)

    def test_the_round_asks_for_completed_orders_with_a_window_covering_the_watermark(self):
        """⭐ 本轮**真正发出去**的参数：`orderSta=已完成` + `timeType=30` + `stime` 覆盖到「水位 − 3 天」。

        ⚠️ 这一条走**真** `jidanpu.fetch_completed_orders_page`（只把 SDK 客户端换成桩，
        `client` 是本线函数的注入口）⇒ 判据打在**真正发出去的载荷**上，
        ⛔ 不是打在「我那个替身收到了什么」上。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        #: 水位固定在**本地正午**（避免用例在 00:00 附近跑时把日期算错）。
        watermark = timezone.make_aware(
            datetime.combine(
                timezone.localdate(self.now) - timedelta(days=10),
                datetime.min.time(),
            )
        ) + timedelta(hours=12)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            status_pull_at=watermark
        )
        self.binding.refresh_from_db()
        client = _OrderListStub(rows=[_row()], count=1)

        applied = jidanpu_member.pull_statuses(self.binding, now=self.now, client=client)

        self.assertEqual(applied, 1)
        sent = client.calls[0]
        self.assertEqual(sent.get("orderSta"), REMOTE_COMPLETED_LABEL)
        self.assertEqual(sent.get("timeType"), 30)
        self.assertEqual(sent.get("page"), 1)
        self.assertEqual(sent.get("limit"), 1000)
        self.assertEqual(sent.get("gameName"), "全部游戏")
        #: 窗：下界 = **水位 − 3 天**再向下取整到那一天、上界**向上取整到次日末**
        #: （照既有同类函数的格式，⛔ 不自创）。
        self.assertEqual(
            sent.get("stime"),
            f"{timezone.localdate(watermark - timedelta(days=3)).isoformat()} 00:00:00",
        )
        self.assertEqual(
            sent.get("etime"),
            f"{(timezone.localdate(self.now) + timedelta(days=1)).isoformat()} 23:59:59",
        )

    def test_an_incremental_round_looks_back_three_days_from_the_watermark(self):
        """⭐ 有水位 ⇒ `since = 水位 − 3 天`（增量与首次同一档；业务方 2026-11-07 拍板）。

        旧口径（水位 − 3 分钟）会给出 `2026-10-19`；新口径必须给 `水位 − 3 天` 那一天
        ⇒ 本条同时当「3 分钟重叠已废除」的回归钉。
        """
        now = timezone.make_aware(datetime(2026, 10, 20, 0, 5, 0))
        watermark = timezone.make_aware(datetime(2026, 10, 20, 0, 2, 0))
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            status_pull_at=watermark
        )
        self.binding.refresh_from_db()
        client = _OrderListStub(rows=[], count=0)

        jidanpu_member.pull_statuses(self.binding, now=now, client=client)

        self.assertEqual(client.calls[0].get("stime"), "2026-10-17 00:00:00")

    def test_a_first_round_without_a_watermark_looks_back_three_days(self):
        """⭐ 无水位（首次启用 / 被清空）⇒ `since = now − 3 天`（首次兜底回看 3 天）。"""
        now = timezone.make_aware(datetime(2026, 10, 20, 12, 0, 0))
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        self.assertIsNone(self.binding.status_pull_at)
        client = _OrderListStub(rows=[], count=0)

        jidanpu_member.pull_statuses(self.binding, now=now, client=client)

        self.assertEqual(client.calls[0].get("stime"), "2026-10-17 00:00:00")

    def test_a_very_old_watermark_looks_back_three_days_before_the_watermark(self):
        """⭐ 水位很旧（10 天前）⇒ `since = 水位 − 3 天`，窗自然变大，⛔ 不被 `now − 3 天` 压回去。

        `stime` 必须是**水位 − 3 天**那一天；它远早于「now − 3 天」，证明停摆那一段没有被
        3 天下限截掉（窗自然变大 ⇒ 补得回来）。
        判别力：若有人把增量窗压平成 `now − 3 天` ⇒ `stime` 落到 10-17 ⇒ 本条红。
        """
        now = timezone.make_aware(datetime(2026, 10, 20, 12, 0, 0))
        watermark = timezone.make_aware(datetime(2026, 10, 10, 0, 2, 0))
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            status_pull_at=watermark
        )
        self.binding.refresh_from_db()
        client = _OrderListStub(rows=[], count=0)

        jidanpu_member.pull_statuses(self.binding, now=now, client=client)

        self.assertEqual(client.calls[0].get("stime"), "2026-10-07 00:00:00")

    def test_an_order_finished_one_minute_before_the_watermark_is_still_pulled(self):
        """⭐⭐ 跨日边界单的行为判据：水位前 1 分钟完成的单必须在本轮被拉到并写回。

        构造：水位 = 10 天前 00:00:30，单在 23:59:30 完成（比水位早 1 分钟、跨午夜）。
        3 天回看窗把水位前一天整日罩住 ⇒ 这一行必然在窗里 ⇒ 写「待验收」。
        判别力：把回看去掉（`since = 水位`）⇒ 窗下界取整后这一行不在窗里 ⇒ 本用例红
        （订单仍是「代练中」、`applied == 0`）。
        """
        now = timezone.make_aware(datetime(2026, 10, 20, 12, 0, 0))
        watermark = timezone.make_aware(datetime(2026, 10, 10, 0, 0, 30))
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            status_pull_at=watermark
        )
        self.binding.refresh_from_db()
        remote = _WindowedRemote(
            [{**_row(), "completed_at": datetime(2026, 10, 9, 23, 59, 30)}]
        )

        with mock.patch.object(
            jidanpu, "fetch_completed_orders_page",
            side_effect=remote.fetch_completed_orders_page,
        ), mock.patch.object(
            jidanpu_member, "_pull_remote_screenshots", return_value=None,
        ):
            applied = jidanpu_member.pull_statuses(
                self.binding, now=now, client=object()
            )

        self.assertEqual(applied, 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    def test_an_order_completed_yesterday_is_pulled_even_after_the_day_rolls_over(self):
        """⭐⭐ 2026-11-07 漏单案的**回归钉**：本地日翻篇后，昨天的完成单必须还罩在窗里。

        事故形状（当日实漏 3,530 张）：铺侧完成档集中北京时间零点后才可匹配；旧增量窗
        只覆盖「水位当天」⇒ 水位一翻日（stime 从昨天跳到今天），昨天完成的单就永久出窗。
        构造：水位 = 今天 08:00（翻日后的轮已推过），单在**昨天 14:00** 完成。
        3 天回看窗的 `stime` = 10-17 ⇒ 这一行在窗里 ⇒ 拉到并写「待验收」。
        判别力：旧口径（水位 − 3 分钟 ⇒ `stime` = 10-20 00:00）⇒ 这一行不在窗里
        ⇒ 本用例红（订单仍是「代练中」、`applied == 0`）。
        """
        now = timezone.make_aware(datetime(2026, 10, 20, 8, 10, 0))
        watermark = timezone.make_aware(datetime(2026, 10, 20, 8, 0, 0))
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            status_pull_at=watermark
        )
        self.binding.refresh_from_db()
        remote = _WindowedRemote(
            [{**_row(), "completed_at": datetime(2026, 10, 19, 14, 0, 0)}]
        )

        with mock.patch.object(
            jidanpu, "fetch_completed_orders_page",
            side_effect=remote.fetch_completed_orders_page,
        ), mock.patch.object(
            jidanpu_member, "_pull_remote_screenshots", return_value=None,
        ):
            applied = jidanpu_member.pull_statuses(
                self.binding, now=now, client=object()
            )

        self.assertEqual(applied, 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    def test_a_very_old_watermark_still_sends_a_window_within_the_server_cap(self):
        """⭐⭐ Task B-补 2：水位 **500 天前** ⇒ **实发**跨度 ≤ 460 天（判据打在发出去的两个字符串上）。

        为什么需要它：`stime` 向下取整到日、`etime` 向上取整到次日末 ⇒ 实发跨度比 `now − since`
        多约 2 天；水位 ≳458 天前时实发 462 天 ⇒ 服务端拒（「…需控制在460天以内…」）⇒ 响亮 +
        不推水位 ⇒ **每轮同一个窗、永久停滞**。修法 = 卡口收到 `now − 457 天`。

        判别力（把卡口改回 460 天）：`stime` 会落到 `now − 460 天` 那一天 ⇒ 实发跨度 > 460 天 ⇒
        本用例红。⚠️ 判据用的是**发出去的两个日期串**（⛔ 不是 `since` 这个中间量）。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            status_pull_at=self.now - timedelta(days=500)
        )
        self.binding.refresh_from_db()
        client = _OrderListStub(rows=[_row()], count=1)

        jidanpu_member.pull_statuses(self.binding, now=self.now, client=client)

        sent = client.calls[0]
        start = datetime.strptime(sent["stime"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(sent["etime"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(end - start, timedelta(days=460))
        # 哨兵：这一轮确实把那个 500 天的水位**推进去了**（否则「≤ 460」在一个 3 天窗上也会成立）。
        self.assertLessEqual(
            start,
            datetime.combine(
                timezone.localdate(self.now - timedelta(days=450)), datetime.min.time()
            ),
        )

    def test_an_empty_working_set_still_advances_the_watermark(self):
        """⭐ Task B-补 3：处理集合为空 ⇒ 照旧 `return 0`，但**推进水位**。

        为什么安全：集合空本身就证明「**现存链路没有一张需要拉**」（都已终态 / 已软删）；
        不推进的话，一个长期闲置的绑定下一次有单时会从很旧的水位算起 ⇒ 可能一次拉回最长的
        整店「已完成」。

        判别力：把这一支改回裸 `return 0`（不写水位）⇒ 本条红。
        ⚠️ 健康列**不动**（别的路可能刚写过一条真错，一次空转不该把它清掉）。
        """
        order = self._order(status=OrderStatus.COMPLETED)
        self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 0)

        self.assertEqual(remote.calls, [], "集合为空 ⇒ 一次上游都不打")
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.status_pull_at, self.now)
        self.assertIsNone(self.binding.last_ok_at, "空转不动健康列")
        self.assertEqual(self.binding.last_error, "")

    def test_a_pull_writeback_does_not_echo_a_status_out(self):
        """⭐ 整体评审 I-7 来源闸：回拉写回 ⇒ **0 条** `member.status_out`。

        缺了这道闸，每写回一笔就排一条**发往对方**的改状态事件，而反向映射非双射
        （我方「代练中」→ 对方「代练中」，对方原本可能是「待上号」）⇒ 会把对方刚设的状态改掉。
        判别力：去掉 `order_status.apply_transition` 的来源闸（无条件排）⇒ 这条红。
        ⚠️ 只关这一个钩子：状态照写、订单审计照记（下面两条断言是「没顺手关掉别的」的对照）。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 1)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)   # 状态照写
        self.assertEqual(OperationLog.objects.filter(action=AUDIT_ACTION).count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind="member.status_out", target_ref=order.order_no
            ).count(),
            0,
        )

    def test_a_completed_downstream_order_moves_a_takerless_order_to_pending_review(self):
        """⭐ 我方**没有接手人** + 下游已完成 ⇒ 写「待验收」（⛔ **不写**「代练中」）。

        旧形态在这里会走「远端代练中 ⇒ 顺手接手」那一支（真机 2026-09-27 的「代练中却没有
        接手人」环）；新形态**只拉「已完成」** ⇒ 这一档不存在，接手那件事归 **3 秒日志快路**
        （它有自己那一组用例）。判据：没有接手人的单照样写「待验收」，且**没有**任何接手动作。
        """
        order = self._order(status=OrderStatus.WAITING_GRAB, taker=False)
        self._link(order)
        remote = _Remote([_row()], count=1)

        with mock.patch.object(jidanpu_member, "claim_order") as claim:
            applied = self._pull(remote)

        self.assertEqual(applied, 1)
        claim.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)
        log = OperationLog.objects.order_by("id").last()
        self.assertEqual(log.to_value, TARGET_STATUS_LABEL)
        self.assertNotEqual(log.to_value, "代练中")

    def test_a_completed_order_is_not_in_the_working_set(self):
        """⭐ 我方**已完成**的单：不进处理集合 ⇒ 一个字节都不写、**一次上游都不打**。

        （这条同时也是「终态不进集合」的判据：整表形态的窗口是**整店**口径、必然带回别的单的
        行，能保留且必须保留的是「我方终态的单**一个字节都不写**」。）
        """
        order = self._order(status=OrderStatus.COMPLETED, settled=False)
        self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 0)

        self.assertEqual(remote.calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self._notes(order).count(), 0)

    def test_a_terminal_order_is_never_written_even_when_the_row_is_in_the_payload(self):
        """⭐ 更强的一版：整表回包里**同时有**这张已完成单的行 ⇒ 照样一个字节都不写。

        （上一条的「零上游请求」由「集合为空」造成；这一条把它的行**真放进回包**，
        证明的不是「没查」，而是「查到了也不写」。）
        """
        done = self._order(order_no=ORDER_NO, status=OrderStatus.COMPLETED)
        done_link = self._link(done, remote_id=JID)
        live = self._order(order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS)
        self._link(live, remote_id=JID_B)
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        self.assertEqual(self._pull(remote), 1)

        done.refresh_from_db()
        live.refresh_from_db()
        self.assertEqual(done.status, OrderStatus.COMPLETED)
        self.assertEqual(live.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self._notes(done).count(), 0)
        done_link.refresh_from_db()
        self.assertIsNone(done_link.last_in_at)

    def test_a_second_round_over_the_same_payload_writes_nothing(self):
        """⭐ 幂等：同一份回包连跑两轮 ⇒ 第二轮**零写**（`target == order.status`）。

        判据打在**终态**上：订单行 / 审计行 / 台账行三样都不许增（返回条数也是 0）。
        ⚠️ `last_in_at` **不在**这三样里：它的语义是「上次拉取到这张单的时刻」，命中了就刷新
        （见 `test_a_successful_round_advances_the_watermark` 的注释）。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 1)
        logs_after_first = OperationLog.objects.count()
        notes_after_first = self._notes(order).count()
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

        self.assertEqual(self._pull(remote, now=self.now + timedelta(minutes=3)), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(OperationLog.objects.count(), logs_after_first)
        self.assertEqual(self._notes(order).count(), notes_after_first)

    def test_a_remote_row_that_matches_no_link_writes_nothing(self):
        """远端回包里的行**匹配不上**任何链路（`orderId` 不相等）⇒ 不写、也不记账。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order, remote_id=JID)
        remote = _Remote([_row(order_id="JDP-00000000000000009999")], count=1)

        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self._notes(order).count(), 0)

    def test_an_incomplete_pull_is_loud_and_does_not_advance_the_watermark(self):
        """⭐ **拉不全 ⇒ 一行都不写 + 不推水位**（照 `jidanpu_reconcile` 的同一条口径）。

        构造：壳里的 `count=5`，而我们只收到 1 行、且不满一页 ⇒ 累计行数 < 总数。
        判别力（变异②）：让「拉不全」那一支也推进 `status_pull_at` ⇒ 本条红。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote([_row()], count=5)

        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self._notes(order).count(), 0)
        note = self._round_notes().get()
        self.assertEqual(note.status, "failed")
        self.assertIn(REASON_INCOMPLETE_HINT, note.reason)
        self.binding.refresh_from_db()
        self.assertIn(REASON_INCOMPLETE_HINT, self.binding.last_error)
        self.assertIsNotNone(self.binding.last_error_at)
        self.assertIsNone(self.binding.status_pull_at)

    def test_an_upstream_error_is_loud_and_does_not_advance_the_watermark(self):
        """整表取数那一跳上游报错 ⇒ 响亮（台账 + `last_error`）+ **不推水位**。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote(error=ApiError("上游炸了", code=None))

        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        note = self._round_notes().get()
        self.assertEqual(note.status, "failed")
        self.assertIn(REASON_UPSTREAM_HINT, note.reason)
        self.binding.refresh_from_db()
        self.assertIn(REASON_UPSTREAM_HINT, self.binding.last_error)
        self.assertIsNotNone(self.binding.last_error_at)
        self.assertIsNone(self.binding.status_pull_at)

    def test_an_issue_order_is_still_pulled(self):
        """我方「问题单」+ 下游已完成 ⇒ 写「待验收」（新口径下**非终态**都在处理集合里）。"""
        order = self._order(status=OrderStatus.ISSUE)
        self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 1)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    def test_a_settlement_landing_mid_round_blocks_the_write(self):
        """**写前复核**是承重的（第二道闸）：集合是在**取数之前**筛的，而「筛 → 拉整表 → 写」
        之间隔着网络往返 ⇒ 那一单可能刚好被结账 + 完成。B-1 的挡住集合把它拦下：最多只许留
        一行 `skipped` 台账，⛔ 不许把「已完成且已结账」的单写回「待验收」。

        判别力：去掉写前那次 `_b1_status_rollback_blocked` 复核 ⇒ 这一单会撞上状态机自己的
        「已结账不许改状态」⇒ 台账变 `failed` + `last_error` 非空 ⇒ 三条断言同时红。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS, settled=False)
        link = self._link(order)

        def _settle_and_complete():
            Performance.objects.filter(order_id=order.pk, emp_type=EmpType.MAIN).update(
                settle_status=SettleStatus.SETTLED
            )
            Order.objects.filter(pk=order.pk).update(status=OrderStatus.COMPLETED)

        remote = _Remote([_row()], count=1, on_call=_settle_and_complete)

        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        note = self._notes(order).get()
        self.assertEqual(note.status, "skipped")
        self.assertIn(REASON_BLOCKED_HINT, note.reason)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertIsNone(self.binding.last_error_at)
        #: 挡住**不算** loud ⇒ 这一轮照旧推水位（与旧形态同口径）。
        self.assertEqual(self.binding.status_pull_at, self.now)
        #: 命中那一刻就写了「上次拉取到这张单的时刻」——它与「最终有没有写回」无关。
        link.refresh_from_db()
        self.assertEqual(link.last_in_at, self.now)

    def test_a_completion_landing_mid_round_is_skipped_even_when_unsettled(self):
        """⭐ E-0259（R3-M1）主钉：取数后、写前复核前那单刚变「已完成」⇒ skip（**不论结账**）。

        与上一条「已结账 + 已完成」的竞态**互补**：B-1 的挡住集合对 COMPLETED 只挡
        「已结账」的那一半（那是**钱**的安全，且与收敛线共用同一把尺子）——**未结账**的
        COMPLETED 在那把尺子下是「可回退」的 ⇒ 修前会被写回「待验收」并清掉完单时间
        （`completed_at=None`）。修后：fresh 复核先看「我方是不是已完成」——是 ⇒ 一个字节
        都不写（对齐「我方已完成单一个字不写」的既有承诺；上家线同位用「写入面只开
        代练中一格」挡的正是它）。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS, settled=False)
        link = self._link(order)

        def _complete_only():
            """竞态那一刀：只完成、**不结账**（把钱那一档留给 B-1 自己的用例）。"""
            Order.objects.filter(pk=order.pk).update(
                status=OrderStatus.COMPLETED, completed_at=self.now
            )

        remote = _Remote([_row()], count=1, on_call=_complete_only)

        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        #: ⛔ 完单时间不许被清（修前：写回待验收那一跳把它清成 None）。
        self.assertEqual(order.completed_at, self.now)
        note = self._notes(order).get()
        self.assertEqual(note.status, "skipped")
        self.assertIn(REASON_BLOCKED_HINT, note.reason)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        #: skip **不算** loud ⇒ 这一轮照旧推水位（与「已结账」那一档同口径）。
        self.assertEqual(self.binding.status_pull_at, self.now)
        link.refresh_from_db()
        self.assertEqual(link.last_in_at, self.now)

    def test_the_shared_gate_never_writes_a_completed_order_regardless_of_settlement(self):
        """⭐ E-0259 钉②（伤害形态本体）：「判 → 写」共用那一段拿到**已完成**的我方单
        ⇒ 不写、`completed_at` 保留——**不论结账与否**。

        与上一条的差别：那条走慢路整轮（内存对象是**取集合时**的旧状态 ⇒ 修前会被状态机的
        复读判挡成 `failed`）；这一条把单**直接**递给 `apply_remote_completion`（下家线三条路
        共用的唯一「判 → 写」，docstring 明说 ⛔ 三条路不许各自一套）——内存与库都是
        COMPLETED ⇒ 复读不拦、B-1 对「未结账的 COMPLETED」也不拦 ⇒ 修前真把单写回
        「待验收」并清掉完单时间（`completed_at=None`），这正是 E-0259 记的伤害形态。
        判据打在**终态**上：状态、完单时间、审计行、台账。
        """
        from apps.rbac.permissions import nodes_of

        order = self._order(status=OrderStatus.IN_PROGRESS, settled=False)
        Order.objects.filter(pk=order.pk).update(
            status=OrderStatus.COMPLETED, completed_at=self.now
        )
        #: ⭐ 内存与库**都是** COMPLETED（`refresh_from_db` 把库里的完成态读回手里）——
        #: 不刷的话内存还是 IN_PROGRESS，修前会先撞状态机的「复读比对」而不是本闸。
        order.refresh_from_db()
        link = self._link(order)

        outcome = jidanpu_member.apply_remote_completion(
            binding=self.binding, order=order, link=link,
            remote_status=REMOTE_COMPLETED_LABEL,
            nodes=nodes_of(self.user), now=self.now,
        )

        self.assertEqual(outcome.verdict, jidanpu_member.REMOTE_DONE_SKIPPED)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.completed_at, self.now)
        self.assertEqual(OperationLog.objects.count(), 0)
        note = self._notes(order).get()
        self.assertEqual(note.status, "skipped")
        self.assertIn(REASON_BLOCKED_HINT, note.reason)

    def test_a_refunded_order_is_never_pulled(self):
        """③ `REFUNDED` **不进处理集合** ⇒ 一个字节都不写。

        ⚠️ ⛔ **不断言「没发请求」**：整表形态下「发不发请求」由**别的单**决定，不由这一张决定
        （下面那条把它的行真放进回包）。
        """
        order = self._order(status=OrderStatus.REFUNDED)
        link = self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self._notes(order).count(), 0)
        link.refresh_from_db()
        self.assertIsNone(link.last_in_at)

    def test_a_refunded_order_is_not_written_even_when_the_row_is_in_the_payload(self):
        """⭐⭐ **钱的承重判据**（裁定 9 的那一半）：我方**已退单**的单，即使它的行真在回包里，
        也⛔ 不许被写成「待验收」——那等于**复活一张退掉的单**，而退款那条路是裸 `update()`、
        跑不到退款结清 ⇒ 钱会算错。

        判别力（变异①）：把 `OrderStatus.REFUNDED` 从排除集合里去掉 ⇒ 本条红
        （状态被写成待验收、台账多一行、`last_in_at` 也被写）。
        """
        refunded = self._order(order_no=ORDER_NO, status=OrderStatus.REFUNDED)
        refunded_link = self._link(refunded, remote_id=JID)
        live = self._order(order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS)
        self._link(live, remote_id=JID_B)
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        self.assertEqual(self._pull(remote), 1)

        refunded.refresh_from_db()
        self.assertEqual(refunded.status, OrderStatus.REFUNDED)
        self.assertEqual(self._notes(refunded).count(), 0)
        refunded_link.refresh_from_db()
        self.assertIsNone(refunded_link.last_in_at)
        live.refresh_from_db()
        self.assertEqual(live.status, OrderStatus.PENDING_REVIEW)

    def test_a_missing_node_is_loud_and_nothing_changes(self):
        """缺节点 ⇒ 台账 + `last_error`（⛔ 不静默跳过）；代价：这条状态回不来。

        ⚠️ 新形态下它同时是「**不推水位**」的判据之一：这一轮有 loud ⇒ `status_pull_at` 不动
        （下一轮窗口自然更大 ⇒ 不漏）。
        """
        bare = User.objects.create_user(username="member-status-bare", password="pw-bare-a")
        binding = JidanpuMemberBinding.objects.create(
            user=bare,
            jidanpu_base_url=BASE_URL,
            account="acc-bare-a",
            account_name="传令兵乙",
            password="pw-bare-a",
            enabled=True,
        )
        order = Order.objects.create(
            game=self.game,
            order_no="20260926000000004099",
            status=OrderStatus.IN_PROGRESS,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目乙",
        )
        JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id="JDP-00000000000000004099"
        )
        remote = _Remote([_row(order_id="JDP-00000000000000004099")], count=1)

        with _stub(remote):
            applied = jidanpu_member.pull_statuses(binding, now=self.now)

        self.assertEqual(applied, 0)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        note = JidanpuSyncEvent.objects.get(kind=KIND_STATUS_IN, target_ref=order.order_no)
        self.assertEqual(note.status, "failed")
        self.assertIn(REASON_NODE_HINT, note.reason)
        binding.refresh_from_db()
        self.assertTrue(binding.last_error)
        self.assertIsNotNone(binding.last_error_at)
        self.assertIsNone(binding.status_pull_at)

    def test_a_disabled_binding_does_nothing(self):
        order = self._order()
        self._link(order)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.binding.refresh_from_db()
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 0)
        self.assertEqual(remote.calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_a_soft_deleted_order_is_skipped(self):
        """软删的单**不进处理集合** ⇒ 一个字节都不写（它在我方已经没有立足点）。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        link = self._link(order)
        Order.all_objects.filter(pk=order.pk).update(deleted_at=order.created_at)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 0)

        self.assertEqual(remote.calls, [])
        self.assertEqual(
            Order.all_objects.get(pk=order.pk).status, OrderStatus.IN_PROGRESS
        )
        self.assertEqual(OperationLog.objects.count(), 0)
        self.assertEqual(self._notes(order).count(), 0)
        link.refresh_from_db()
        self.assertIsNone(link.last_in_at)

    def test_a_successful_round_advances_the_watermark(self):
        """⭐ 成功一轮 ⇒ 推进 `status_pull_at`（下一轮的窗从这里开始）+ 写健康列。

        ⚠️ **`last_in_at` 的语义在此明确定为「上次**拉取到**这张单的时刻」**：新形态没有逐单
        读取了，它是「远端整表返回的行里**命中了这条链路**（且那一行是 `已完成`）」那一刻写的
        （收窄写入）。选它而⛔ 不是「不写」的理由：链路行那一列在页面上就是给运维看「这条链路
        还活着吗」，整批不写会让它变成死列；而「写回成功才写」会把「远端确实还有这张单」这个
        事实与「我方这一次改了状态没有」混成一件事。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        link = self._link(order)
        remote = _Remote([_row()], count=1)

        self.assertEqual(self._pull(remote), 1)

        self.binding.refresh_from_db()
        self.assertEqual(self.binding.status_pull_at, self.now)
        self.assertEqual(self.binding.last_ok_at, self.now)
        self.assertEqual(self.binding.last_error, "")
        link.refresh_from_db()
        self.assertEqual(link.last_in_at, self.now)

    def test_the_return_value_counts_only_the_applied_statuses(self):
        """返回契约不变：回**写回的条数**（命令行按它打「回拉 N 条」）。

        两张单都命中了回包，但第二张**已经是待验收** ⇒ 只算第一张。
        """
        first = self._order(order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS)
        second = self._order(order_no=ORDER_NO_B, status=OrderStatus.PENDING_REVIEW)
        self._link(first, remote_id=JID)
        self._link(second, remote_id=JID_B)
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        self.assertEqual(self._pull(remote), 1)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(second.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(OperationLog.objects.count(), 1)


# ══ ②·五 ①式回拉覆盖「同一家店、别的绑定（含②式）」的链路 ═════════════════════
#
# 真机读数：对方关掉「开放平台接入权限」⇒ ②式取 token 被拒、回调一条都不到
# （18:47 起），而**同一家店**的①式账号密码登录仍能读到批量状态。业务方要的兜底 =
# 「①式那一轮顺带把同店②式链路下的已完成单也写成待验收」。
#
# 被钉住的判据（每条都要能红）：
# 1. ①式回拉**真的**把「同店②式链路」的已完成单写成「待验收」，台账记在**那条链路自己的
#    绑定**名下（⛔ 不是消费方）；
# 2. **停用**的兄弟绑定 ⛔ 不覆盖（停用是用户自己按下的开关）；
# 3. **不同店**（不同 `jidanpu_base_url`）⛔ 不覆盖（拿 A 店凭据动 B 店的单）；
# 4. 已经是「待验收」⇒ ①式 / ②式**谁后到都一个字不写**（幂等）；
# 5. 节点门 / 写前 B-1 复核对兄弟链路**照样生效**；
# 6. 开关 `JIDANPU_MEMBER_PULL_COVERS_SIBLINGS` 默认开、关掉 ⇒ 回到旧范围。
#
# ⚠️ 判据一律打在**数据库行**（订单状态 / 审计行 / 台账行）上（E-0009）。


class PullCoversSameStoreSiblingsTests(_Base):
    """①式（password）状态回拉**覆盖同一家店别的绑定**的链路（②式失灵的兜底）。"""

    def setUp(self):
        super().setUp()
        #: 默认的兄弟行 = 一行**②式**绑定（界面真形态：账号密码留空；同 BASE_URL）。
        self.sibling = self._make_sibling(API_USER)

    # ── 工具 ─────────────────────────────────────────────────────────────
    def _make_sibling(self, username, *, base_url=BASE_URL, enabled=True, groups=("客服",),
                      nodes=SIBLING_NODES):
        """一行**同一家店**的兄弟绑定（默认②式；账号密码留空 = 界面真形态）。

        ⚠️ `groups` **必须能换**：节点判据读的是「组 → 职位 → 节点」，而 `Role` 是**按组名**
        共享的 —— 本类 setUp 给「客服」职位挂过改状态节点，若「没有节点」的那条用例仍把
        用户放进「客服」，它就**继承**了那几个节点（用例会以「居然写成功了」的形式假绿）。
        """
        make_logged_in_client(username=username, groups=groups, nodes=nodes)
        return JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=username),
            jidanpu_base_url=base_url,
            account="", password="", mode="openapi",
            client_id=f"{API_CLIENT_ID}-{username}",
            client_secret=f"{API_CLIENT_SECRET}-{username}",
            enabled=enabled,
        )

    def _sibling_order(self, binding, *, order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS):
        """兄弟链路那张我方单（MAIN 绩效行落在**那条链路自己的主人**名下）。"""
        order = self._order(order_no=order_no, status=status)
        Performance.objects.filter(order_id=order.pk, emp_type=EmpType.MAIN).update(
            user=binding.user
        )
        return order

    def _sibling_link(self, binding, order, *, remote_id=JID_B):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id=remote_id,
        )

    # ── 1. 主路径：①式回拉写兄弟链路 ─────────────────────────────────────
    def test_a_password_pull_writes_pending_review_for_a_completed_sibling_api_order(self):
        """⭐ 主钉（红证据）：改前这单仍是**代练中**；改后写成**待验收**。"""
        own = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(own, remote_id=JID)
        sibling = self._sibling_order(self.sibling)
        sibling_link = self._sibling_link(self.sibling, sibling)
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        applied = self._pull(remote)

        self.assertEqual(applied, 2)
        own.refresh_from_db()
        sibling.refresh_from_db()
        self.assertEqual(own.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(sibling.status, OrderStatus.PENDING_REVIEW)
        #: 台账记在**那条链路自己的绑定**名下（⛔ 不是消费方那条①式）。
        note = JidanpuSyncEvent.objects.get(
            kind=KIND_STATUS_IN, target_ref=ORDER_NO_B,
            payload__member_binding_id=self.sibling.pk,
        )
        self.assertEqual(note.status, "done")
        self.assertIn(REMOTE_COMPLETED_LABEL, note.reason)
        #: 审计行：机器操作人（与①式自己那条同形）。
        log = OperationLog.objects.filter(order_no=ORDER_NO_B).get()
        self.assertEqual(log.actor, MACHINE_ACTOR)
        self.assertEqual(log.to_value, TARGET_STATUS_LABEL)
        sibling_link.refresh_from_db()
        self.assertEqual(sibling_link.last_in_at, self.now)

    def test_a_pull_runs_when_only_the_sibling_has_links(self):
        """⭐ 本绑定**一条自己的链路都没有**、同店兄弟有 ⇒ 这一轮照旧要打上游并写回。

        判别力：把「处理集合」只按本绑定算（旧的 `pullable_links(binding)`）⇒ 集合空 ⇒
        早退 ⇒ 兄弟那张永远不被覆盖。
        """
        sibling = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, sibling)
        remote = _Remote([_row(order_id=JID_B)], count=1)

        self.assertEqual(self._pull(remote), 1)

        self.assertNotEqual(remote.calls, [], "兄弟有链路 ⇒ 这一轮必须打上游")
        sibling.refresh_from_db()
        self.assertEqual(sibling.status, OrderStatus.PENDING_REVIEW)

    # ── 2. 停用不覆盖 ────────────────────────────────────────────────────
    def test_a_disabled_sibling_binding_is_not_covered(self):
        """停用 = 用户自己按下的开关（spec §8.4）⇒ 它名下的单⛔ 不替它办。"""
        own = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(own, remote_id=JID)
        JidanpuMemberBinding.objects.filter(pk=self.sibling.pk).update(enabled=False)
        order = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, order)
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        self.assertEqual(self._pull(remote), 1)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._notes(order).count(), 0)

    # ── 3. 不同店不覆盖 ──────────────────────────────────────────────────
    def test_an_api_order_at_another_store_is_not_covered(self):
        """同店判据 = `jidanpu_base_url` 逐字相等 ⇒ 另一家店的单一个字都不写。"""
        own = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(own, remote_id=JID)
        other = self._make_sibling(API_OTHER_USER, base_url=OTHER_BASE_URL)
        order = self._sibling_order(other, order_no=OTHER_ORDER_NO)
        self._sibling_link(other, order, remote_id=OTHER_JID)
        remote = _Remote([_row(order_id=JID), _row(order_id=OTHER_JID)], count=2)

        self.assertEqual(self._pull(remote), 1)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._notes(order).count(), 0)

    def test_a_sibling_that_switches_stores_during_discovery_is_not_completed(self):
        old_order = self._sibling_order(self.sibling)
        old_link = self._sibling_link(self.sibling, old_order)
        new_order = self._sibling_order(self.sibling, order_no=OTHER_ORDER_NO)
        discover = jidanpu_member._pull_extra_bindings

        def discover_then_switch(binding):
            siblings = discover(binding)
            JidanpuMemberOrderLink.objects.filter(pk=old_link.pk).update(retired_at=self.now)
            self.sibling.jidanpu_base_url = OTHER_BASE_URL
            self.sibling.save(update_fields=["jidanpu_base_url"])
            self._sibling_link(self.sibling, new_order)
            return siblings

        remote = _Remote([_row(order_id=JID_B)], count=1)
        with mock.patch.object(jidanpu_member, "_pull_extra_bindings", side_effect=discover_then_switch):
            self.assertEqual(self._pull(remote), 0)

        new_order.refresh_from_db()
        self.assertEqual(new_order.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(self._notes(new_order).first())

    def test_a_sibling_disabled_during_the_fetch_is_not_completed(self):
        order = self._sibling_order(self.sibling)
        link = self._sibling_link(self.sibling, order)

        def disable_sibling():
            JidanpuMemberBinding.objects.filter(pk=self.sibling.pk).update(enabled=False)

        remote = _Remote([_row(order_id=JID_B)], count=1, on_call=disable_sibling)
        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        link.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(link.last_in_at)

    def test_a_paused_sibling_can_recover_an_older_completion_after_resume(self):
        order = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, order)
        watermark = self.now - timedelta(days=30)
        self.binding.status_pull_at = watermark
        self.binding.save(update_fields=["status_pull_at"])
        completed_at = (watermark + timedelta(days=1)).replace(tzinfo=None)
        row = {**_row(order_id=JID_B), "completed_at": completed_at}

        sibling_id = self.sibling.pk

        class PauseDuringFetch(_WindowedRemote):
            def fetch_completed_orders_page(self, *args, **kwargs):
                JidanpuMemberBinding.objects.filter(pk=sibling_id).update(enabled=False)
                return super().fetch_completed_orders_page(*args, **kwargs)

        self.assertEqual(self._pull(PauseDuringFetch([row])), 0)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.status_pull_at, watermark)
        self.assertEqual(self.binding.last_error, "")
        JidanpuMemberBinding.objects.filter(pk=self.sibling.pk).update(enabled=True)

        self.assertEqual(self._pull(_WindowedRemote([row]), now=self.now + timedelta(minutes=3)), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    def test_a_stale_source_session_cannot_complete_a_siblings_order(self):
        order = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, order)

        def move_source():
            JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(jidanpu_base_url=OTHER_BASE_URL)

        remote = _Remote([_row(order_id=JID_B)], count=1, on_call=move_source)
        self.assertEqual(self._pull(remote), 0)

        order.refresh_from_db()
        self.binding.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        self.assertIsNone(self.binding.status_pull_at)

    def test_a_failed_old_session_does_not_stamp_a_replacement_store(self):
        order = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, order)

        def move_source():
            JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(jidanpu_base_url=OTHER_BASE_URL)

        remote = _Remote(error=ApiError("synthetic read failure"), on_call=move_source)
        self.assertEqual(self._pull(remote), 0)

        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertIsNone(self.binding.status_pull_at)

    def test_same_store_sibling_secret_rotation_does_not_discard_completion(self):
        order = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, order)

        def rotate_secret():
            JidanpuMemberBinding.objects.filter(pk=self.sibling.pk).update(client_secret="new-synthetic-secret")

        remote = _Remote([_row(order_id=JID_B)], count=1, on_call=rotate_secret)
        self.assertEqual(self._pull(remote), 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    # ── 4. 幂等（②式 / ①式先后到达）──────────────────────────────────────
    def test_a_sibling_already_pending_review_is_left_untouched_by_the_pull(self):
        """②式先到（已写待验收）⇒ ①式回拉后到：⛔ 一个字不写。"""
        own = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(own, remote_id=JID)
        sibling = self._sibling_order(self.sibling, status=OrderStatus.PENDING_REVIEW)
        sibling_link = self._sibling_link(self.sibling, sibling)
        logs_before = OperationLog.objects.count()
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        self.assertEqual(self._pull(remote), 1)

        sibling.refresh_from_db()
        self.assertEqual(sibling.status, OrderStatus.PENDING_REVIEW)
        #: 只多自己那一条审计（兄弟那条一个字都不写）。
        self.assertEqual(OperationLog.objects.count(), logs_before + 1)
        self.assertEqual(self._notes(sibling).count(), 0)
        #: `last_in_at` 的既有语义是「上次**拉取到**这张单」⇒ 命中就刷新（⛔ 不算「写了订单」）。
        sibling_link.refresh_from_db()
        self.assertEqual(sibling_link.last_in_at, self.now)

    def test_an_api_round_after_the_pull_writes_nothing_more(self):
        """①式先到（回拉写了待验收）⇒ ②式那条路后到：⛔ 一个字不写。

        ⚠️ 走的仍是**三条路共用**的那一段（`apply_remote_completion`）——②式恢复之后
        回调 / 兜底对账落到的是同一个函数，幂等判据只在它里面（⛔ 这里不另判一次）。
        """
        from apps.rbac.permissions import nodes_of

        sibling = self._sibling_order(self.sibling)
        sibling_link = self._sibling_link(self.sibling, sibling)
        remote = _Remote([_row(order_id=JID_B)], count=1)
        self.assertEqual(self._pull(remote), 1)          # ①式先写
        sibling.refresh_from_db()
        logs_after = OperationLog.objects.count()
        notes_after = self._notes(sibling).count()

        outcome = jidanpu_member.apply_remote_completion(
            binding=self.sibling, order=sibling, link=sibling_link,
            remote_status=REMOTE_COMPLETED_LABEL,
            nodes=nodes_of(self.sibling.user), now=self.now + timedelta(minutes=1),
        )

        self.assertEqual(outcome.verdict, jidanpu_member.REMOTE_DONE_SKIPPED)
        sibling.refresh_from_db()
        self.assertEqual(sibling.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(OperationLog.objects.count(), logs_after)
        self.assertEqual(self._notes(sibling).count(), notes_after)

    # ── 5. 节点门 / B-1 对兄弟链路照样生效 ───────────────────────────────
    def test_a_sibling_without_the_status_node_is_denied_and_loud(self):
        """兄弟那条链路的主人**没有**改状态节点 ⇒ 节点门照样挡住（台账记在它名下 + 响亮）。"""
        JidanpuMemberBinding.objects.filter(pk=self.sibling.pk).update(enabled=False)
        bare = self._make_sibling(API_BARE_USER, groups=(NO_NODE_GROUP,), nodes=())
        bare_order = self._sibling_order(bare)
        self._sibling_link(bare, bare_order)
        remote = _Remote([_row(order_id=JID_B)], count=1)

        self.assertEqual(self._pull(remote), 0)

        bare_order.refresh_from_db()
        self.assertEqual(bare_order.status, OrderStatus.IN_PROGRESS)
        note = JidanpuSyncEvent.objects.get(
            kind=KIND_STATUS_IN, target_ref=ORDER_NO_B,
            payload__member_binding_id=bare.pk,
        )
        self.assertEqual(note.status, "failed")
        self.assertIn(REASON_NODE_HINT, note.reason)
        #: 响亮落在**消费方**（①式）那份绑定上：既有口径「有 loud ⇒ 写 last_error + 不推水位」。
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.last_error)
        self.assertIsNone(self.binding.status_pull_at)

    def test_a_settlement_landing_mid_round_blocks_the_sibling_write(self):
        """写前复核（B-1）对兄弟链路照样生效：拦住 ⇒ 只留一条 skipped 台账。"""
        sibling = self._sibling_order(self.sibling)
        sibling_link = self._sibling_link(self.sibling, sibling)

        def _settle_and_complete():
            Performance.objects.filter(
                order_id=sibling.pk, emp_type=EmpType.MAIN
            ).update(settle_status=SettleStatus.SETTLED)
            Order.objects.filter(pk=sibling.pk).update(status=OrderStatus.COMPLETED)

        remote = _Remote([_row(order_id=JID_B)], count=1, on_call=_settle_and_complete)

        self.assertEqual(self._pull(remote), 0)

        sibling.refresh_from_db()
        self.assertEqual(sibling.status, OrderStatus.COMPLETED)
        note = JidanpuSyncEvent.objects.get(
            kind=KIND_STATUS_IN, target_ref=ORDER_NO_B,
            payload__member_binding_id=self.sibling.pk,
        )
        self.assertEqual(note.status, "skipped")
        self.assertIn(REASON_BLOCKED_HINT, note.reason)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertEqual(self.binding.status_pull_at, self.now)
        sibling_link.refresh_from_db()
        self.assertEqual(sibling_link.last_in_at, self.now)

    # ── 6. 开关 ──────────────────────────────────────────────────────────
    def test_the_switch_is_on_by_default(self):
        self.assertIs(settings.JIDANPU_MEMBER_PULL_COVERS_SIBLINGS, True)

    @override_settings(JIDANPU_MEMBER_PULL_COVERS_SIBLINGS=False)
    def test_switching_it_off_restores_the_old_scope(self):
        """关掉 ⇒ 回到「只覆盖本绑定链路」的旧行为（原有用例全绿的那一套范围）。"""
        own = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(own, remote_id=JID)
        sibling = self._sibling_order(self.sibling)
        self._sibling_link(self.sibling, sibling)
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)

        self.assertEqual(self._pull(remote), 1)

        own.refresh_from_db()
        sibling.refresh_from_db()
        self.assertEqual(own.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(sibling.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self._notes(sibling).count(), 0)


# ══ ③ 「对账 ⇒ 排事件」只有一份实现 ═════════════════════════════════════════════


class ReconcileAndQueueTests(_Base):
    """控制器裁定 1：抽成一个函数、视图改为调用它（⛔ 不许留两份实现）。"""

    def test_it_queues_the_three_kinds_and_returns_counts(self):
        hall = self._order(order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB, taker=False)
        assigned = self._order(
            order_no=ORDER_NO_B, status=OrderStatus.IN_PROGRESS, taker=True
        )
        # 一条「本地有链路、但对方不该再有」的单（已软删）。
        gone = self._order(
            order_no="20260926000000004003", status=OrderStatus.IN_PROGRESS
        )
        # ⚠️ 接单人**故意换成外人**（不是绑定主人）：按现行口径「接手人是这家下家自己的
        #    账号」那一档整店不撤（见 `tests/test_member_same_store_withdraw.py`）——
        #    本用例钉的是「软删的单要进 to_withdraw」，与那一档无关。
        outsider = User.objects.create_user(
            username="status-outsider-taker", password="pw-a"
        )
        Performance.objects.filter(order=gone, emp_type=EmpType.MAIN).update(user=outsider)
        Order.all_objects.filter(pk=gone.pk).update(deleted_at=gone.created_at)
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=gone, jidanpu_order_id="JDP-00000000000000004003"
        )
        # `base_url` 与 `game` 决定范围；大厅单要被算进「他的范围内」。
        stats = jidanpu_member.reconcile_and_queue(self.binding, now=self.now)

        self.assertEqual(stats.launch, 1)
        self.assertEqual(stats.assign, 1)
        self.assertEqual(stats.withdraw, 1)
        self.assertEqual(stats.at, self.now)
        for order, kind in (
            (hall, "member.launch"),
            (assigned, "member.assign"),
            (gone, "member.withdraw"),
        ):
            with self.subTest(kind=kind):
                self.assertTrue(
                    JidanpuSyncEvent.objects.filter(
                        kind=kind, target_ref=order.order_no, status="pending"
                    ).exists()
                )

    def test_overlapped_order_is_reported_in_the_stats_with_a_human_reason(self):
        """⭐ 整体评审 I-1：同址重叠被跳过的单**不逐单落台账**，但必须由 `QueueStats.skipped`
        带出来（**人话原因**，⛔ 不是光秃秃的计数）。

        判别力：把 `reconcile_and_queue` 的 `skipped` 改回 `len(result.skipped)`（计数）⇒
        人话那两条断言红；把归属判定整支删掉 ⇒ 三条一起红。
        """
        hall = self._order(order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB, taker=False)
        # 第二个代练员：**同址**、游戏范围重叠（夹具默认给当前租户全部游戏）。
        make_logged_in_client(username=OTHER_USER, groups=("代练员",), nodes=())
        later = JidanpuMemberBinding.objects.create(
            user=User.objects.get(username=OTHER_USER),
            jidanpu_base_url=BASE_URL,
            account="acc-status-later",
            account_name="传令兵乙",
            password="pw-status-b",
            source_name="团团通系统",
            enabled=True,
        )
        self.assertGreater(later.pk, self.binding.pk)  # 构造物：更晚建 ⇒ 归属给更早那份

        stats = jidanpu_member.reconcile_and_queue(later, now=self.now)

        self.assertEqual(stats.launch, 0)
        self.assertEqual(stats.skipped_count, 1)
        reason = stats.skipped[0]
        self.assertIn("跳过", reason)
        # 人话要点名「是谁在负责推这张单」——只报数字运维看不出找谁
        self.assertIn(self.binding.account, reason)
        # 被跳过的那份**没有**排 member.launch，也**没有**逐单落台账（spec §5.4 的收口改准）
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="member.launch", target_ref=hall.order_no
            ).exists()
        )
        self.assertEqual(JidanpuSyncEvent.objects.count(), 0)

    def test_there_is_exactly_one_implementation_of_reconcile_then_queue(self):
        """视图里⛔ 不许再出现 `reconcile(` / `queue_member_event(` 的调用（AST 判据）。"""
        self.assertFalse(
            hasattr(jidanpu_member, "backfill_enabled"),
            "旧的内联实现必须已经删掉（⛔ 不留两份）",
        )
        source = Path(jidanpu_member_views.__file__).read_text(encoding="utf-8")
        called: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called.add(node.func.id)
        self.assertNotIn("reconcile", called)
        self.assertNotIn("queue_member_event", called)
        self.assertIn("reconcile_and_queue", called)

    def test_enabling_still_backfills_through_the_endpoint(self):
        """视图改调 `reconcile_and_queue` 之后，「启用补推」那一跳照旧落台账。"""
        # 先把这一行停用：只有**翻真**那一刻才补推（`was_enabled` 那道判据）。
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        order = self._order(status=OrderStatus.WAITING_GRAB, taker=False)
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": "acc-status-a",
            "password": "pw-status-a",
            "source_name": "团团通系统",
            "enabled": True,
        }
        with mock.patch.object(
            jidanpu_member,
            "detect_binding_with_login_identity",
            return_value=(jidanpu_member.DetectionReport(ok=True), None),
        ):
            res = self.user_client.put(
                BINDING_URL, json.dumps(payload), content_type="application/json"
            )
        self.assertEqual(res.json()["code"], CODE_OK, res.content)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="member.launch", target_ref=order.order_no
            ).exists()
        )


# ══ ④ 频控闸门（P2 spec §17.6 的形状）═════════════════════════════════════════


class SyncNowGateTests(SimpleTestCase):
    """闸门本体：最小间隔 / 在途互斥 / 失败退避（时间全部显式注入 ⇒ 判据确定）。"""

    def test_a_second_attempt_inside_the_minimum_interval_is_refused(self):
        gate = jidanpu_member_views._SyncNowGate()
        ok, _ = gate.acquire("k", now=0.0)
        self.assertTrue(ok)
        gate.release("k", failed=False, now=0.5)
        ok, message = gate.acquire("k", now=1.0)
        self.assertFalse(ok)
        self.assertIn("秒后再试", message)
        # 间隔过去 ⇒ 放行
        self.assertTrue(gate.acquire("k", now=10.0)[0])

    def test_a_round_in_flight_blocks_a_second_attempt(self):
        gate = jidanpu_member_views._SyncNowGate()
        self.assertTrue(gate.acquire("k", now=0.0)[0])
        ok, message = gate.acquire("k", now=99.0)
        self.assertFalse(ok)
        self.assertIn("正在同步", message)
        gate.release("k", failed=False, now=100.0)
        self.assertTrue(gate.acquire("k", now=200.0)[0])

    def test_a_failure_backs_off_and_a_success_clears_it(self):
        gate = jidanpu_member_views._SyncNowGate()
        gate.acquire("k", now=0.0)
        gate.release("k", failed=True, now=0.1)
        # 退避 5 秒（§17.6 的 5 → 15 → 60）
        self.assertFalse(gate.acquire("k", now=4.0)[0])
        self.assertTrue(gate.acquire("k", now=6.0)[0])
        gate.release("k", failed=False, now=6.1)
        self.assertTrue(gate.acquire("k", now=9.0)[0])

    def test_the_ladder_is_five_fifteen_sixty(self):
        self.assertEqual(
            jidanpu_member_views.SYNC_NOW_BACKOFF_LADDER_SECONDS, (5, 15, 60)
        )
        self.assertEqual(jidanpu_member_views.SYNC_NOW_MIN_INTERVAL_SECONDS, 2)
        gate = jidanpu_member_views._SyncNowGate()
        moment = 0.0
        waits = []
        for _ in range(3):
            gate.acquire("k", now=moment)
            gate.release("k", failed=True, now=moment)
            # 找放行点，量出这次退避有多长
            step = 1.0
            while not gate.acquire("k", now=moment + step)[0]:
                step += 1.0
            waits.append(int(step))
            moment = moment + step
        self.assertEqual(waits, [5, 15, 60])


# ══ ⑤「立即同步」端点 ═══════════════════════════════════════════════════════════


class SyncNowApiTests(_Base):
    def setUp(self):
        super().setUp()
        # 频控状态是**进程内**的 ⇒ 每个用例换一个新的闸门，免得上一个用例的退避串味。
        jidanpu_member_views._SYNC_NOW_GATE = jidanpu_member_views._SyncNowGate()
        self.other_client = make_logged_in_client(
            username=OTHER_USER,
            groups=("代练员",),
            nodes=("order.change_status",),
        )
        self.other = User.objects.get(username=OTHER_USER)

    def _post(self, client=None, body=None):
        return (client or self.user_client).post(
            SYNC_NOW_URL,
            json.dumps(body or {}),
            content_type="application/json",
        )

    def test_it_syncs_the_callers_own_binding(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote([_row()], count=1)

        with _stub(remote):
            res = self._post()

        body = res.json()
        self.assertEqual(body["code"], CODE_OK, res.content)
        self.assertEqual(body["data"]["applied"], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PENDING_REVIEW)

    def test_without_a_binding_it_says_so_and_calls_no_upstream(self):
        remote = _Remote([_row()], count=1)
        with _stub(remote):
            res = self._post(client=self.other_client)
        body = res.json()
        self.assertEqual(body["code"], CODE_NOT_CONFIGURED, res.content)
        self.assertTrue(body["msg"])
        self.assertEqual(remote.calls, [])

    def test_a_disabled_binding_is_refused(self):
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        remote = _Remote([_row()], count=1)
        with _stub(remote):
            res = self._post()
        self.assertEqual(res.json()["code"], CODE_NOT_CONFIGURED, res.content)
        self.assertEqual(remote.calls, [])

    def test_it_cannot_touch_another_users_binding(self):
        """越权面 = 0：A 的绑定与订单在 B 的请求下**一行都不动**（判据打在数据库行上）。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote([_row()], count=1)

        with _stub(remote):
            res = self._post(client=self.other_client)  # B 没有绑定

        self.assertEqual(res.json()["code"], CODE_NOT_CONFIGURED, res.content)
        self.assertEqual(remote.calls, [])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_request_parameters_cannot_choose_the_row(self):
        """请求体里塞别人的 id / user ⇒ 被忽略（本端点的行主只来自 `request.user`）。"""
        # A（`self.user`）那一行 + 一张单：
        # ⚠️ 整表形态下「A 那张单」也会出现在 B 那一跳的回包里（同一家店、同一档「已完成」）
        #    ⇒ 判据必须是「B 那一跳**只写自己链路上的单**」，而不是「没查 A 的单」。
        theirs = self._order(order_no=ORDER_NO, status=OrderStatus.IN_PROGRESS)
        theirs_link = self._link(theirs, remote_id=JID)
        # B（`self.other`）自己的绑定 + 一张单（应当被他这一跳回拉）。
        other_binding = JidanpuMemberBinding.objects.create(
            user=self.other,
            jidanpu_base_url="https://jidanpu-b.test.invalid",
            account="acc-status-b",
            account_name="传令兵乙",
            password="pw-status-b",
            enabled=True,
        )
        mine = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO_B,
            status=OrderStatus.IN_PROGRESS,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目乙",
        )
        Performance.objects.create(
            order=mine, emp_type=EmpType.MAIN, user=self.other,
            temp_amount=Decimal("12.34"),
        )
        JidanpuMemberOrderLink.objects.create(
            binding=other_binding, order=mine, jidanpu_order_id=JID_B
        )
        remote = _Remote([_row(order_id=JID), _row(order_id=JID_B)], count=2)
        with _stub(remote):
            res = self._post(
                client=self.other_client,
                body={
                    "user": self.user.pk,
                    "user_id": self.user.pk,
                    "binding_id": self.binding.pk,
                    "id": self.binding.pk,
                    "account": "acc-status-a",
                },
            )

        self.assertEqual(res.json()["code"], CODE_OK, res.content)
        theirs.refresh_from_db()
        mine.refresh_from_db()
        # A 那一张**一行都不动**（连它的链路行都没被写）；B 那张按 B 自己的绑定回拉。
        self.assertEqual(theirs.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(mine.status, OrderStatus.PENDING_REVIEW)
        theirs_link.refresh_from_db()
        self.assertIsNone(theirs_link.last_in_at)
        self.assertEqual(self._notes(theirs).count(), 0)

    def test_the_second_click_inside_the_interval_does_not_reach_upstream(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote([_row()], count=1)

        with _stub(remote):
            first = self._post()
            second = self._post()

        self.assertEqual(first.json()["code"], CODE_OK, first.content)
        self.assertEqual(second.json()["code"], CODE_BUSINESS, second.content)
        self.assertTrue(second.json()["msg"])
        self.assertEqual(len(remote.calls), 1)

    def test_an_upstream_failure_makes_the_next_click_wait(self):
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        remote = _Remote(error=ApiError("上游炸了", code=None))

        with _stub(remote):
            failed = self._post()
            again = self._post()

        # 预期失败**不抛**（后台轮询要能继续跑别的绑定）⇒ 信封仍是成功码 + 台账 + last_error
        self.assertEqual(failed.json()["code"], CODE_OK, failed.content)
        self.binding.refresh_from_db()
        self.assertTrue(self.binding.last_error)
        self.assertEqual(again.json()["code"], CODE_BUSINESS, again.content)
        self.assertEqual(len(remote.calls), 1)

    def test_a_successful_round_clears_the_failure_backoff(self):
        """**成功一轮 ⇒ 退避计数清零**（控制器 Important 评审，2026-09-27）。

        判别力：把判据改回 `after != before`（成功会把 `last_error_at` 清成 `None`
        ⇒ 被当成失败）⇒ 本用例红：成功之后的下一跳会被说成「上次同步失败」而不是
        「同步太频繁」，且再失败时退避会从 60 秒那一档接着数。

        ⚠️ 这条**必须**打在 API 级：`SyncNowGateTests` 直接喂 `failed=` 布尔，
        绕过了「这一轮失败了没有」那段检测，抓不到这个 bug。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        clock = _Clock()
        jidanpu_member_views._SYNC_NOW_GATE = jidanpu_member_views._SyncNowGate(clock=clock)
        failing = _Remote(error=ApiError("上游炸了", code=None))
        succeeding = _Remote([_row(order_id=JID)], count=1)

        # ① 第一轮：失败 ⇒ 写下失败时刻、进第一档退避（5 秒）。
        clock.t = 0.0
        with _stub(failing):
            first = self._post()
        self.assertEqual(first.json()["code"], CODE_OK, first.content)
        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.last_error_at)

        # ② 第二轮（越过 5 秒退避）：**成功** ⇒ `last_error_at` 被清空。
        clock.t = 10.0
        with _stub(succeeding):
            second = self._post()
        self.assertEqual(second.json()["code"], CODE_OK, second.content)
        self.binding.refresh_from_db()
        self.assertIsNone(self.binding.last_error_at)

        # ③ 成功之后**立刻**再点：只该被「最小间隔」挡（⛔ 不是退避）⇒ 计数确实清零了。
        clock.t = 10.5
        too_soon = self._post().json()
        self.assertEqual(too_soon["code"], CODE_BUSINESS, too_soon)
        self.assertNotIn("失败", too_soon["msg"])
        self.assertIn("太频繁", too_soon["msg"])

        # ④ 再失败一轮 ⇒ 退避从**第一档（5 秒）**重新开始：1 秒后再点应只剩 4 秒。
        clock.t = 13.0
        with _stub(failing):
            self._post()
        clock.t = 14.0
        blocked = self._post().json()
        self.assertIn("失败", blocked["msg"])
        self.assertEqual(_wait_seconds(blocked["msg"]), 4)

    def test_two_failing_rounds_increment_the_backoff_ladder(self):
        """反面对照（控制器 Important 评审）：连续两轮失败 ⇒ 计数**确实递增**（第二档 15 秒）。

        ⚠️ 没有这一条，「清零」那条也可能只是「你把检测写成了恒 False」而照样绿。
        """
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self._link(order)
        clock = _Clock()
        jidanpu_member_views._SYNC_NOW_GATE = jidanpu_member_views._SyncNowGate(clock=clock)
        failing = _Remote(error=ApiError("上游炸了", code=None))

        clock.t = 0.0
        with _stub(failing):
            self._post()
        clock.t = 6.0  # 越过第一档（5 秒）
        with _stub(failing):
            self._post()
        clock.t = 7.0
        blocked = self._post().json()
        self.assertIn("失败", blocked["msg"])
        # 第二档 = 15 秒；6.0 + 15 = 21 ⇒ 7.0 时还剩 14 秒。
        self.assertEqual(_wait_seconds(blocked["msg"]), 14)

    def test_the_endpoint_is_classified_in_the_operation_log_ledger(self):
        entry = INSTRUMENTED["apps/business/api/jidanpu_member_views.py"]
        self.assertEqual(
            entry[("post", "api/business/member/jidanpu-sync-now/")],
            AUDIT_ACTION,
        )

    def test_the_endpoint_calls_the_slow_path_function_itself(self):
        """`sync-now` 与慢路**同一段代码**（spec §9.3 明文）：视图调的必须是 `pull_statuses`。

        ⛔ 不许在这里另写一份「查状态 / 写状态」的实现——那条路会跟慢路漂移，
        而漂移的症状是「页面上点得动、定时对账却不一样」这种**静默**的分叉。
        """
        source = Path(jidanpu_member_views.__file__).read_text(encoding="utf-8")
        called: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertIn("pull_statuses", called)
