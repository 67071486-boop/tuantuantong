"""下游鸡蛋铺线的 outbox 消费者：录单 / 抛单 / 只录单 / 退单 / 改状态 / 撤下。被钉住的契约（spec §5.1 / §5.3 / §5.4 / §8.4 / §9）：`queue_member_event` 写 `direction="out"` 台账行、`payload["member_binding_id"]` **是 int**（T3 的换绑收敛按 `payload__member_binding_id=<pk>` 命中在飞事件）、同 `(kind, target_ref, binding)` 的**在飞**行挡重复（`done` 不挡，spec §5.4）；`consume_event` 五支消费（`member.launch` / `member.assign` / `member.withdraw` / `member.unlist` / `member.status_out`，未知 kind 响亮抛），绑定已不存在 ⇒ `skipped` + 人话原因；`run_outbox` 只取**该绑定**的在飞事件、逐事件异常边界、失败退避、到上限不再重试、健康列**收窄写入**（`last_run_at` / `last_ok_at` / `last_error` / `last_error_at`）。
**地址级去重**（裁定 3）：同一 `jidanpu_base_url` 下**任何**绑定（含已停用的）已有指向同一张我方单的链路行 ⇒ 不录第二张，收敛 `skipped` + 人话原因。**消费 `to_record`**（裁定 1）：`member.assign` 只录单、⛔ 不抛单。「撤下」动作**存在**（业务方裁定，取代旧「没有撤下」口径）：常量 `KIND_UNLIST`（`"member.unlist"`）、`reconcile` 结果的 `to_unlist` 组、消费者 `_consume_unlist`，上游原语 `jidanpu.release_order_to_home`（`/OrderAjax/RelesOrderToHomeByOrderID`）；判据见本文件 `KIND_UNLIST_LITERAL` / `UnlistConsumeTests` 与 `test_jidanpu_member_triggers.py::UnlistOnOurTakeTests`。⚠️ 撤下 ≠ `member.withdraw`（退单）：撤下只把那张单从对方大厅拿回来、⛔ 不作废它。
纪律：⚠️ 断言一律打**数据库行**（`JidanpuSyncEvent` / `JidanpuMemberOrderLink` / `JidanpuMemberBinding`）与**桩收到的载荷**，⛔ 不断言 logger（`AGENTS.md` §5-G）；⚠️ 桩一律打在**被查找的那个模块** `apps.business.jidanpu` 上（E-0029），并有一条「不接桩时真实出口会被走到」的对照；⚠️ 夹具一律**合成值**（E-0077）：`member-a` / `https://jidanpu-a.test.invalid` / `pw-a`；⛔ 零真实网络。"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

import psycopg

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business import jidanpu, jidanpu_common, jidanpu_member
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import grant_games, make_logged_in_client

BASE_URL = "https://jidanpu-a.test.invalid"
BASE_URL_B = "https://jidanpu-b.test.invalid"
ACCOUNT = "member-a"
ORDER_NO = "20260926000000002001"
ORDER_NO_2 = "20260926000000002002"
JID = "JDP-00000000000000002001"
JID_2 = "JDP-00000000000000002002"

#: 换绑端点（T3 的单行资源）；本文件只借它走一次**换绑收敛**（跨任务用例）。
BINDING_URL = "/api/business/member/jidanpu-binding/"

#: 退单原因必须**逐字**沿用既有那条线的那句（spec §13-T6 默认沿用）。
#: ⚠️ 写**字面值**（⛔ 不从常量派生）：自指断言在被断言的常量改坏时不会红（E-0024）。
WITHDRAW_REMARK_LITERAL = "订单已被发单方打手接取"

#: 抛单失败的人话必须**点名到那条权限**（spec §9.1 第 3 条末段 / §13-R7）：
#: 「发到内部抢单大厅」（节点 36）在鸡蛋铺里没有页面可探 ⇒ 检测只能间接判，
#: 这句人话是用户唯一的线索。⚠️ 字面值（⛔ 不从常量派生，E-0024）。
THROW_FAILED_LITERAL = (
    "鸡蛋铺拒绝了这次抛单：可能是该账号缺『将订单发到内部抢单大厅』权限，"
    "请在下家鸡蛋铺里给这个职位勾上"
)
THROW_PERMISSION_HINT = "将订单发到内部抢单大厅"

#: 五个 kind 的**字面值**就是 触发点写台账的契约（⛔ 改名会静默换掉语义）。
KIND_LAUNCH_LITERAL = "member.launch"
KIND_ASSIGN_LITERAL = "member.assign"
KIND_WITHDRAW_LITERAL = "member.withdraw"
#: ⭐ 业务方：从对方**内部接单大厅**撤下（⛔ 不退单）。
KIND_UNLIST_LITERAL = "member.unlist"
KIND_STATUS_OUT_LITERAL = "member.status_out"

#: `member.status_out` / 退单用到的**上游中文标签**（`docs/JIDANPU-API.md` 的状态表；字面值）。
#: ⭐ 业务方把推送集收窄成**三档**（问题单 / 已暂停 / 已退单）⇒ 这三个标签是
#: `member.status_out` 唯一能当期望值的档。
LABEL_ISSUE = "问题单"
LABEL_PAUSED = "已暂停"
#: ⚠️ 「代练中」这个标签在下面（`LABEL_IN_PROGRESS`，撤下那一档也要它）。⭐ **D-0180 起**
#: 来源态例外有三档：已完成 / 待验收 / 已退单。其余来源态仍收敛成 `skipped`，不许推它。
LABEL_REFUNDED = "已退单"

#: ⭐ 撤下抛单的**目标态**（`orderSta` 的中文标签；真机读数）：撤下生效那一档停在这里。
LABEL_UNASSIGNED = "未分配"
#: ⭐ 「对方抢先接走」那一档的 `orderSta`（同上真机读数）：该原语对已被接走的单**无效且仍回`code=1`** ⇒ 只有这一格能把「我们撤下了」与「对方接走了」分开。
#: ⭐ 同一个标签也是 `member.status_out` **唯一例外**的目标值：来源态 ∈ {已完成, 待验收}来源态 ∈ {已完成, 待验收, 已退单} 时推的就是它。
LABEL_IN_PROGRESS = "代练中"
#: ⭐ 手动推送（计划 §3）**额外允许**的两档标签（**字面值**，⛔ 不从被测常量派生）。
#: 自动路径**不推**这两档（业务方的推送集只有三档 + 来源态例外）。
LABEL_PENDING_REVIEW = "待验收"
LABEL_COMPLETED = "已完成"
#: ⭐ 手动推送的**防倒退**人话（`jidanpu_member.MANUAL_PENDING_REVIEW_AFTER_DONE_REASON` 的
#: **逐字**副本；⛔ 不写常量名——自指断言在被断言的常量改坏时不会红，E-0024）。
MANUAL_ROLLBACK_BLOCKED_REASON_LITERAL = "对方已是已完成，不往回改"
#: ⭐ 同一条防倒退的**第二档**（远端已退单）。⛔ 两档文案**必须不同**
#: （退单与完成是两种现场，合成一句就等于说不出是哪种）。
MANUAL_ROLLBACK_REFUNDED_REASON_LITERAL = "对方已是已退单，不往回改"
#: ⭐ **串行闸**拿不到时的人话（`jidanpu_member.OUTBOX_BUSY_REASON` 的**逐字**副本，E-0024）。
#: ⚠️ 措辞刻意**不诱导重点**（复评 Minor-3）：单子已排队、下一轮会自动推。
OUTBOX_BUSY_REASON_LITERAL = "已排队：另一轮推送正在跑，几秒后会自动推出去"
#: ⭐ 「这一档不推」的**人话原因**（`jidanpu_member.STATUS_NOT_PUSHABLE_REASON` 的**逐字**副本；
#: ⛔ 不写 `jidanpu_member.STATUS_NOT_PUSHABLE_REASON`：自指断言在被断言的常量改坏时**不会红**，E-0024）。⚠️ 本线已删掉旧符号 `STATUS_LOCAL_TAKER_NO_PUSH_REASON` / `STATUS_UNMAPPED_REASON`与六档 `STATUS_LABELS` ⇒ 池内两态 / 我方接手 / 快照缺失这几档的人话现在**都是这一句**原因断言用本常量逐字对，不用子串「未分配」。
STATUS_NOT_PUSHABLE_REASON_LITERAL = (
    "这一档按业务口径不推给下家（只推『问题单 / 已暂停 / 已退单』；『代练中』只有来源态是"
    "『已完成 / 待验收 / 已退单』时才回改）：本次不改对方状态"
)
#: ⭐ 复活档「无接手人不推」的人话（`jidanpu_member.REVERT_PUSH_NO_TAKER_REASON` 的**逐字**副本；
#: ⛔ 不写常量名——自指断言在被断言的常量改坏时不会红，E-0024）。2026-11-05 业务方拍板（E-0318）。
REVERT_NO_TAKER_REASON_LITERAL = (
    "这张单恢复「代练中」时我方没有接手人：无接手人的恢复不推下家，"
    "要让下家重新接单请重抛大厅"
)
#: ⭐「接手人不是这家店的人」的人话（复活档与问题单/已暂停两档共用；逐字副本，E-0024 同上）。
TAKER_STORE_MISMATCH_REASON_LITERAL = (
    "接手人不是这家店的人：这一档状态只同步接手人名下的店，本次不改对方状态"
)
#: ⭐ 撤下抛单遇到「对方抢先接走」时的点名 reason（**字面值**，⛔ 不从常量派生，E-0024）。
#: 两套口径（我们撤下 / 对方接走）都留下「不在厅」这个同形事实 ⇒ 台账里必须有这一句点名。
UNLIST_TAKEN_REASON_LITERAL = (
    "对方已把这张单接走 ⇒ 撤下抛单对该单无效；业务目标『停在未分配』未达成，"
    "需人工核对是否要退单"
)
#: ⭐ 回读里 `orderSta` **读得到、但不是我们认识的任何一档**时用的合成标签（本波 `other` 档）。
#: 用**合成值**（⛔ 不抄真机标签）：这一档的判据是「值被原样写进 reason」，与本线认识哪几档无关。
LABEL_OTHER = "已流转（合成）"
#: ⭐ `other` 档在台账里落下的**整句**（**字面值**，⛔ 不从常量派生，E-0024）：
#: 既有那句 + 一个括号里缀上**实际读到的标签**。排障时看不出读到什么就等于没记。
UNLIST_OTHER_REASON_LITERAL = (
    "撤下后回读未确认对方那张单停在「未分配」，未完成"
    "（回读 orderSta 读到「已流转（合成）」）"
)


#: ⭐ ⑤ 下游「这个登录账号的员工 uid」（合成值；录单表单的 `inputOrderKefu` 就填它）。
DOWNSTREAM_STAFF_UID = "70001"
#: 模板里那个**合成占位值**（`ADD_ORDER_TEMPLATE['inputOrderKefu']`）——⛔ 一个字节都不许再发出去。
TEMPLATE_KEFU_PLACEHOLDER = "100"
#: 认不出当前账号时的人话台账（必须点明「没填客服」这件事，⛔ 不许静默）。
KEFU_UNRESOLVED_HINT = "录入客服"


def _sources():
    return [{"name": "d-团团通系统", "value": "团团通系统"}]


#: 回读桩的「调用方没设过」哨兵：`sta_after_write=None` 是**合法值**（= 写后**读不到**），
#: ⛔ 不能拿 `None` 兼任「没设过」。
_UNSET = object()


class _Upstream:
    """`apps.business.jidanpu` 那几个被消费者查找的函数的脚本替身。
    ⚠️ 只替**出口**（`add_order` / `fetch_sources` / `fetch_order_by_key1` /`throw_order` / `set_order_status`）——消费者自己的逻辑一行不替，`match_sources` 这类纯函数也走真实实现。"""

    def __init__(self, *, sources=None, add=None, lookup=None, throw=None, status=None,
                 remote_exists=False, fetch_error=None, source_order_no=ORDER_NO,
                 release=None, thrown=False, remote_id=None,
                 kefu_uid=DOWNSTREAM_STAFF_UID, kefu_error=None,
                 remote_sta=None, sta_after_write=_UNSET):
        self.sources = _sources() if sources is None else sources
        self.add = add
        self.lookup = dict(lookup or {})
        self.throw = throw
        self.status = status
        self.release = release
        self.calls: list[tuple[str, dict]] = []
        #: 本桩自己的「这张单已经录进对方系统了吗」（供上面那次**录单前**的认领预读判断）。
        self._recorded = False
        #: 「**对方已经有这张单**」（上一轮录过 / 别人录过 / 手工录过）——认领那一层要能看到它。
        self.remote_exists = remote_exists
        #: **回读**（`fetch_order_by_key1`）要抛的错：认领预读是「读不到 ⇒ 响亮重试、
        #: ⛔ 不当作对方没有」那条判据的入口，必须能注入失败。
        self.fetch_error = fetch_error
        #: ⭐ 桩侧的「对方那张单的来源单号」= 我方**有效来源单号**（`order.source_order_no`
        #: 非空用它，否则回落 `order_no`）。用例造 `source_order_no` 非空的单时要一起改它，
        #: 否则桩会按 `order_no` 答「还没录过」，把用例变成以**错误的原因**变红。
        self.source_order_no = source_order_no
        #: ⭐ 桩侧的「对方那张单此刻**还挂在他家大厅**吗」（`isThrowOrder == "Y"`）。
        #: 撤下（`member.unlist`）那一支要「先读一遍、再写、再读一遍」，所以它必须**有状态**。
        self.thrown = thrown
        #: 「远端单号」那一支的现场回包（`thrown` 决定 `isThrowOrder`）；`None` = 不造这一支。
        self.remote_id = remote_id
        #: ⭐ 下游「这个登录账号的员工 uid」（录单表单的 `inputOrderKefu`）；`""` = 认不出来。
        self.kefu_uid = kefu_uid
        #: 取 uid 那一次上游调用要抛的错（接口挂了 / 形状变了）。
        self.kefu_error = kefu_error
        #: ⭐ 对方那张单**此刻**的 `orderSta`（按远端单号回读时给它）；`None` = **读不到**
        #: （真实的「单在第一页之外 / 约 90 天窗外」那一档，与「查无此单」同形）。
        self.remote_sta = remote_sta
        #: 写成功之后回读要看到的 `orderSta`（显式 `None` = 写后**读不到**；没设 = 用写进去那个标签）。
        self.sta_after_write = sta_after_write

    def names(self):
        return [name for name, _kwargs in self.calls]

    # ── 五个出口 ─────────────────────────────────────────────────────────
    def add_order(self, client, *, form_payload, top_level, source_order_no):
        self.calls.append(("add_order", {
            "form_payload": form_payload,
            "top_level": top_level,
            "source_order_no": source_order_no,
        }))
        if isinstance(self.add, Exception):
            raise self.add
        self._recorded = True
        return jidanpu.AddOrderResult(remote_oid=900001, message="录单成功")

    def fetch_sources(self, client):
        self.calls.append(("fetch_sources", {}))
        return list(self.sources)

    def fetch_order_by_key1(self, client, *, key, expect_field, expect_value):
        self.calls.append(("fetch_order_by_key1", {
            "key": key, "expect_field": expect_field, "expect_value": expect_value,
        }))
        if isinstance(self.fetch_error, Exception):
            raise self.fetch_error
        # ⚠️ **有状态**）：按**来源单号**那次回读，在**录单发生之前**必须查不到
        # （真实世界就是这样：单还没录进去）。少了这条，新加的「认领预读」会被桩喂出一个假结果 ⇒ 走认领分支、`add_order` 一次都不调，而本表那些「录单 + 抛单」的用例会以**错误的原因**变红（看起来像实现坏了）。
        if key == self.source_order_no and not (self._recorded or self.remote_exists):
            return None
        if key in self.lookup:
            return self.lookup[key]
        # ⭐ 本桩的「对方那张单**此刻**是什么状态」（改状态那一支要「读 → 写 → 读」，所以它必须
        #    有状态）。⚠️ 排在 `lookup` **之后**：显式给了回包的用例仍然由 `lookup` 说了算
        #    （退单那一支就是这么喂「已退单」的）。`remote_sta is None` = 读不到。
        if self.remote_sta is not None and key == JID:
            # ⚠️ `isThrowOrder` 与 `orderSta` **同一行**给（D-0318）：抛单前的
            #    前置回读既要判「是不是已退单」，也要判「是不是已经挂在他家大厅」。
            return {
                "orderId": key, "orderSta": self.remote_sta, "recManName": "",
                "isThrowOrder": "Y" if self.thrown else "N",
            }
        # 「远端单号」那一支按 `thrown` 现场拼（撤下要「读→写→读」，所以它必须会变）。
        if self.remote_id is not None and key == self.remote_id:
            return {
                "orderId": key, "orderSta": "未分配",
                "isThrowOrder": "Y" if self.thrown else "N",
            }
        return None

    def throw_order(self, client, *, remote_order_id, fee):
        self.calls.append(("throw_order", {"remote_order_id": remote_order_id, "fee": fee}))
        if isinstance(self.throw, Exception):
            raise self.throw
        # ⭐ 状态化（D-0318）：真实上游**不允许抛一张「已退单」的单**（业务方
                #    实测回「发单失败，可能这些订单已设定了接手人…」）⇒ 桩必须会拒。
        #    ⚠️ 少了这一条，「先改回未分配再抛」那半段写成没写都一样绿（自洽的桩不产出结论）。
        if self.remote_sta == LABEL_REFUNDED:
            raise ApiError("发单失败，可能这些订单已设定了接手人…", code=0)
        self.thrown = True

    def set_order_status(self, client, *, remote_order_id, status_label, remark=""):
        self.calls.append(("set_order_status", {
            "remote_order_id": remote_order_id,
            "status_label": status_label,
            "remark": remark,
        }))
        if isinstance(self.status, Exception):
            raise self.status
        # ⚠️ 有状态（照 `release_order_to_home` 的 `thrown` 形状）：真实的改状态会改掉对方那张单的
        #    `orderSta`，而消费者要**写后回读确认**才敢收敛 `done`。
        self.remote_sta = (
            status_label if self.sta_after_write is _UNSET else self.sta_after_write
        )

    def own_staff_uid(self, client):
        """⭐ 下游「当前登录账号的员工 uid」（`jidanpu.own_staff_uid` 的替身）。"""
        self.calls.append(("own_staff_uid", {}))
        if isinstance(self.kefu_error, Exception):
            raise self.kefu_error
        return self.kefu_uid

    def release_order_to_home(self, client, *, remote_order_id):
        """从对方**内部接单大厅**撤下（`RelesOrderToHomeByOrderID`）。
        ⚠️ 桩是**有状态**的：成功撤下之后 `isThrowOrder` 就变 "N"（真实世界就是这样，
        而消费者要**回读确认**才敢落 `in_hall=False`）。"""
        self.calls.append(("release_order_to_home", {"remote_order_id": remote_order_id}))
        if isinstance(self.release, Exception):
            raise self.release
        self.thrown = False


@contextmanager
def _stub(up):
    """把替身接到**被查找的模块** `apps.business.jidanpu` 上。"""
    with (
        mock.patch.object(jidanpu, "add_order", side_effect=up.add_order),
        mock.patch.object(jidanpu, "fetch_sources", side_effect=up.fetch_sources),
        mock.patch.object(
            jidanpu, "fetch_order_by_key1", side_effect=up.fetch_order_by_key1
        ),
        mock.patch.object(jidanpu, "throw_order", side_effect=up.throw_order),
        mock.patch.object(jidanpu, "set_order_status", side_effect=up.set_order_status),
        mock.patch.object(
            jidanpu, "release_order_to_home", side_effect=up.release_order_to_home
        ),
        mock.patch.object(jidanpu, "own_staff_uid", side_effect=up.own_staff_uid),
    ):
        yield up


#: 注入的假客户端：桩全部忽略它，只在「不接桩」的对照里被真实出口碰到（没有 `.call`）。
CLIENT = object()


def _link_table_queries(ctx):
    """`CaptureQueriesContext` 里碰过本线**链路表**的 SQL（成本性质的判据口）。
    ⚠️ 判据是「**链路表**被碰了几次」，不是总查询数：消费一条事件本来就要读写台账
    （那是既有契约）⇒ 只有链路表这一格能分辨「先判来源、后取链路」的便宜顺序有没有被挪动。"""
    table = JidanpuMemberOrderLink._meta.db_table
    return [q["sql"] for q in ctx.captured_queries if table in q["sql"]]


class _Base(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="合成游戏甲")
        self.user = User.objects.create_user(username=ACCOUNT, password="pw-a")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-a",
            password="pw-a",
            source_name="团团通系统",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目甲",
        )

    # ── 夹具工具 ─────────────────────────────────────────────────────────
    def _link(self, *, binding=None, order=None, remote_id=JID, in_hall=True):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding or self.binding,
            order=order or self.order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    def _other_binding(self, *, base_url=BASE_URL, enabled=False, username="member-b"):
        user = User.objects.create_user(username=username, password="pw-a")
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=base_url,
            account=f"acc-{username}",
            password="pw-a",
            enabled=enabled,
        )

    def _event(self, kind, *, order=None, binding=None, payload=None):
        return jidanpu_member.queue_member_event(
            order or self.order, binding or self.binding, kind=kind, payload=payload
        )

    def _events_of_status_out(self):
        """这张单的 `member.status_out` 台账行（清掉上一轮留下的 `failed`，判据才独立）。"""
        return JidanpuSyncEvent.objects.filter(
            kind=jidanpu_member.KIND_STATUS_OUT, target_ref=self.order.order_no
        )

    def _launch_lookup(self, *, readback=JID):
        return {
            ORDER_NO: (
                None if readback is None
                else {"orderId": readback, "orderIdView": ORDER_NO}
            ),
            JID: {"orderId": JID, "isThrowOrder": "Y"},
        }


# ══ ① queue_member_event（契约 + 在飞去重）═════════════════════════════════


class QueueMemberEventTests(_Base):
    def test_kind_literals_are_the_wire_values(self):
        """五个 kind 的字面值就是 Task 6 写台账用的契约（⛔ 改名会静默换掉语义）。"""
        self.assertEqual(jidanpu_member.KIND_LAUNCH, KIND_LAUNCH_LITERAL)
        self.assertEqual(jidanpu_member.KIND_ASSIGN, KIND_ASSIGN_LITERAL)
        self.assertEqual(jidanpu_member.KIND_WITHDRAW, KIND_WITHDRAW_LITERAL)
        # ⭐ 撤下（业务方）与退单**是两个 kind**：只钉一个会放行「混为一谈」的实现
        self.assertEqual(jidanpu_member.KIND_UNLIST, KIND_UNLIST_LITERAL)
        self.assertNotEqual(KIND_UNLIST_LITERAL, KIND_WITHDRAW_LITERAL)
        self.assertEqual(jidanpu_member.KIND_STATUS_OUT, KIND_STATUS_OUT_LITERAL)
        # ⚠️ `hasattr(模块, "member.unlist")` 那种写法**恒假**（属性名里不可能有点）⇒ 那是
        #    一条自指的空断言（E-0024 族）：判据一律钉**值的字面量**，上面几条才是真判据。
        # ⚠️ 两条 `direction="in"` 的 kind 也各有自己的字面量（它们由快路 / 慢路写）。
        self.assertEqual(jidanpu_member.KIND_REMOTE_TAKEN, "member.remote_taken")
        self.assertEqual(jidanpu_member.KIND_STATUS_IN, "member.status_in")

    def test_the_outbox_cap_and_backoff_are_the_documented_values(self):
        """失败契约的数值（spec §10）：指数退避有上限、到达上限后不再无限重试。"""
        self.assertEqual(jidanpu_member.OUTBOX_BACKOFF_BASE_SECONDS, 60)
        self.assertEqual(jidanpu_member.OUTBOX_BACKOFF_MAX_SECONDS, 3600)
        self.assertEqual(jidanpu_member.OUTBOX_MAX_ATTEMPTS, 5)

    def test_queue_writes_an_out_row_with_an_int_binding_id(self):
        """⭐ 裁定 2：`payload["member_binding_id"]` **一律写 int**（T3 的换绑收敛靠它命中）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        self.assertEqual(event.direction, "out")
        self.assertEqual(event.target_type, "order")
        self.assertEqual(event.target_ref, ORDER_NO)
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)
        self.assertIs(type(event.payload["member_binding_id"]), int)
        # 直接按那个键查一次：证明换绑收敛的 `payload__member_binding_id=<pk>` 真能命中
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                payload__member_binding_id=self.binding.pk
            ).exists()
        )

    def test_queue_pins_the_binding_id_even_if_a_caller_passes_a_bad_one(self):
        """调用方塞进来的坏值不许覆盖行主（否则事件会归到别人名下）。"""
        event = self._event(
            jidanpu_member.KIND_LAUNCH,
            payload={"some_progress": 1, "member_binding_id": self.binding.pk + 999},
        )
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)
        self.assertEqual(event.payload["some_progress"], 1)

    def test_queue_blocks_a_second_in_flight_row_but_not_after_done(self):
        """spec §5.4：在飞（`pending` / `failed`）挡住重复；`done` **不挡**新行。"""
        first = self._event(jidanpu_member.KIND_LAUNCH)
        second = self._event(jidanpu_member.KIND_LAUNCH)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 1)

        JidanpuSyncEvent.objects.filter(pk=first.pk).update(status="done")
        third = self._event(jidanpu_member.KIND_LAUNCH)
        self.assertNotEqual(third.pk, first.pk)
        self.assertEqual(JidanpuSyncEvent.objects.count(), 2)

    def _burned_out(self, kind):
        """造一条**已经烧完重试额度**的在飞行（`run_outbox` 会把它归进 `abandoned`）。"""
        event = self._event(kind)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
            reason="录单时上游报错", processed_at=timezone.now(),
        )
        return JidanpuSyncEvent.objects.get(pk=event.pk)

    def test_an_explicit_action_gives_a_burned_out_row_a_new_budget(self):
        """⭐ 2026-09-27 补（E-0153 同族）：**显式动作给烧完额度的行重新发额度**，⛔ 不许静默空转。
        真机形状：`member.launch` 行 = `failed/5`，用户再点「重新抛单」时它照样被在飞去重命中、`run_outbox` 直接归 `abandoned` ⇒ **一次上游都不打**，而页面一切正常（= 业务方报的「好像只是一次性同步」）。判据钉**行上的四个值**（额度 / 状态 / 原因 / 退避锚都复位），⛔ 不是「返回了同一行」（那正是旧行为）。"""
        burned = self._burned_out(jidanpu_member.KIND_ASSIGN)
        self.assertIsNotNone(burned.processed_at)
        self.assertEqual(jidanpu_member.queue_assign(self.order, taker_id=self.user.pk), 1)
        fresh = JidanpuSyncEvent.objects.get(pk=burned.pk)
        self.assertEqual(fresh.attempts, 0)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.reason, "")
        self.assertIsNone(fresh.processed_at)

    def test_a_row_still_inside_its_budget_is_not_re_armed(self):
        """⚠️ 只复位**烧完额度**的行：还在额度内的行照旧按退避等（连点几下不能绕过退避）。"""
        event = self._event(jidanpu_member.KIND_ASSIGN)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=1, reason="上游报错", processed_at=timezone.now(),
        )
        jidanpu_member.queue_assign(self.order, taker_id=self.user.pk)
        row = JidanpuSyncEvent.objects.get(pk=event.pk)
        self.assertEqual(row.attempts, 1)
        self.assertEqual(row.status, "failed")

    def test_the_periodic_queue_does_not_re_arm(self):
        """⚠️ 周期那一侧走**默认**（`rearm=False`）⇒ 烧完额度的行**保持**不动（⛔ 不是无限重试）。
        （`reconcile_and_queue` 就是直接调 `queue_member_event`、不传 `rearm`。）"""
        burned = self._burned_out(jidanpu_member.KIND_ASSIGN)
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN
        )
        row = JidanpuSyncEvent.objects.get(pk=burned.pk)
        self.assertEqual(row.attempts, jidanpu_member.OUTBOX_MAX_ATTEMPTS)
        self.assertEqual(row.status, "failed")

    def test_queue_does_not_block_across_kinds_or_bindings(self):
        """去重键含 kind 与绑定：别的 kind / 别的绑定不受影响。"""
        launch = self._event(jidanpu_member.KIND_LAUNCH)
        assign = self._event(jidanpu_member.KIND_ASSIGN)
        other = self._event(jidanpu_member.KIND_LAUNCH, binding=self._other_binding())
        self.assertEqual(
            len({launch.pk, assign.pk, other.pk}), 3
        )


# ══ ② member.launch：录单 + 抛单 + 写链路行 ═══════════════════════════════


class LaunchConsumeTests(_Base):
    def test_the_launch_happy_path_records_throws_and_writes_the_link(self):
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            # ⭐ `fetch_order_by_key1` 出现**三次**——① 录单**前**的认领预读（查不到 ⇒ 才去录）② 录单**后**取远端单号 ③ 抛单**后**回读确认。
            # ⭐ 录单**前**多一次 `own_staff_uid`（取「录入客服 uid」）——它**只在这一行还没缓存时**出现（缓存判据见 `InputOrderKefuTests`）。
            ["fetch_sources", "fetch_order_by_key1", "own_staff_uid", "add_order",
             "fetch_order_by_key1", "throw_order", "fetch_order_by_key1"],
        )
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertEqual(link.jidanpu_order_id, JID)
        self.assertTrue(link.in_hall)
        self.assertIsNotNone(link.listed_at)
        self.assertIsNotNone(link.last_out_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.payload["jidanpu_order_id"], JID)

    def test_the_throw_price_is_the_out_price_times_the_percent(self):
        """⭐ 业务方 2026-09-27：**大厅镜像**那一档的抛单价 = 我方发单价 × 该绑定的百分比。
        判据：桩上 `throw_order` 收到的 `fee` **恰好**是 12.34 × 80% = **9.87**（四舍五入到分）。
        ⛔ 不是「fee 是正数」——那样把百分比整个忽略掉也照样绿。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            throw_price_percent=80
        )
        self.binding.refresh_from_db()
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["throw_order"]
        self.assertEqual(sent["fee"], Decimal("9.87"))

    def _adopted(self, *, throw=None, status_error=None, **row):
        """认领那一支的桩：`lookup[ORDER_NO]` 既是「录单**前**的预读结果」、也是「对方已有这张单」。⚠️ 确认抛单那一次回读走的是 `key=<远端单号>` ⇒ `JID` 那一行必须也在（`isThrowOrder=Y`）。
        ⚠️ `throw` / `status_error`（D-0257）：给「已退单先改回未分配 → 补抛」那两跳注入上游失败用（`status_error` 打在 `set_order_status` 上）；不传 = 两跳都成功。"""
        base = {"orderId": JID, "orderIdView": ORDER_NO, "orderSta": "未分配",
                "isThrowOrder": "N"}
        base.update(row)
        return _Upstream(
            lookup={ORDER_NO: base, JID: {"orderId": JID, "isThrowOrder": "Y"}},
            remote_exists=True, throw=throw, status=status_error,
        )

    def test_an_adopted_unassigned_order_gets_the_hall_listing(self):
        """⭐ 认领（2026-09-27 真机实测补）：对方**已经有这张单** ⇒ ⛔ 不录第二张，只补抛单。
        真实成因：录单成功但链路行没落成 / 台账被重置 / 对方有人手工录过同一来源单号。不认领的后果：重试永远被对方按「来源单号重复」拒掉，那一单在我方**永远停在待抢单**（日志快路只认自己链路表里的单）。判据：① 一次 `add_order` 都没调；② 链路远端单号 = **认领到的那个**；③ 抛单照做。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted()   # 「未分配、没进过大厅」⇒ 我们原本要做的那个动作照做
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertNotIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["throw_order"]["fee"], Decimal("12.34"))
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertEqual(link.jidanpu_order_id, JID)
        self.assertTrue(link.in_hall)

    def test_a_pre_read_failure_is_loud_and_never_re_records(self):
        """⭐ 认领预读**读不到** ⇒ `failed` 重试，⛔ **绝不当作「对方没有」去重录**。判据：① 一次 `add_order` 都不打（读不到就去录 = 可能灌第二张真单）；
        ② 原因用**本支自己的**文案（这一跳发生在录单**之前**，写成「录单后…」会误导排障的人）；③ 一行链路都不落（还不知道对方的订单号，⛔ 不能编一个）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(
            fetch_error=ApiError("读超时", code=0), lookup=self._launch_lookup()
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertNotIn("add_order", up.names())
        event.refresh_from_db()
        self.assertIn(jidanpu_member.RECORD_PRE_READ_REASON, event.reason)
        self.assertEqual(JidanpuMemberOrderLink.objects.count(), 0)

    def test_an_adopted_order_already_in_the_hall_is_not_thrown_again(self):
        """认领到的单**已经在他家大厅** ⇒ 目标态已达成：⛔ 一次抛单都不打，只把链路补正。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted(isThrowOrder="Y")
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertNotIn("add_order", up.names())
        self.assertNotIn("throw_order", up.names())
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall)

    def test_a_line_without_the_refund_undo_leg_still_skips_refunded(self):
        """⭐ D-0257 的**无腿线**分支（评审建议 7）：`refund_undo is None`（②式形态）⇒ 认领到已退单仍走终态跳过（`ADOPTED_REFUNDED_REASON`），⛔ 不打改回、⛔ 不抛单。
        引擎判据本身住在 `_consume_launch`（判据只有这一份）⇒ 本钉用 ①式线的**拷贝去掉`refund_undo` 那条腿**直调它，把这条分支钉成能失败的（否则它没有任何用例）。"""
        import dataclasses
        from apps.business import jidanpu_member as jpm
        legless = dataclasses.replace(
            jpm._MEMBER_LINE,
            record=dataclasses.replace(jpm._MEMBER_LINE.record, refund_undo=None),
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted(orderSta="已退单")
        with _stub(up):
            outcome = jidanpu_member._consume_launch(
                event, binding=self.binding, client=CLIENT, http=None,
                now=timezone.now(), line=legless,
            )
        self.assertEqual(outcome, "skipped")
        self.assertNotIn("set_order_status", up.names())
        self.assertNotIn("throw_order", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("已退单", event.reason)

    def test_an_adopted_refunded_order_is_un_refunded_then_rethrown(self):
        """⭐ 认领到的单**已退单** ⇒ 先改回「未分配」再补抛（⭐ D-0257 改准，2026-10-18）。
        「已退单」要先改回「未分配」再抛。已退单不能直接抛。判据：① 上游调用恰是 预读 → 改回未分配 → 抛单 → 回读（改回被跳过/顺序颠倒即红）；② 改回那一跳的目标标签是「未分配」；③ 链路 `in_hall=True`、事件 `done`。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted(orderSta="已退单")
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_sources", "fetch_order_by_key1", "set_order_status",
             "throw_order", "fetch_order_by_key1"],
        )
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_UNASSIGNED)
        self.assertEqual(sent["remark"], "")
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.attempts, 0)

    def test_a_rejected_un_refund_stops_before_the_throw_and_stays_loud(self):
        """⭐ D-0257 细化：第 1 步（已退单改回「未分配」）被上游拒绝 ⇒ ⛔ 不抛单、原因可见。
        上家平台对「改回未分配」的接受性**无真机实测**⇒ 失败必须诚实：`REFUND_UNDO_FAILED_REASON` + 上游原话进台账、走既有退避（到上限 abandoned），⛔ 不静默、⛔ 不许接着抛单。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted(
            orderSta="已退单",
            status_error=ApiError(
                "", body={"code": 0, "msg": "当前状态不允许该操作", "data": ""}, code=0
            ),
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertEqual(
            up.names(),
            ["fetch_sources", "fetch_order_by_key1", "set_order_status"],
        )
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertIn(jidanpu_member.REFUND_UNDO_FAILED_REASON, event.reason)
        self.assertIn("当前状态不允许该操作", event.reason)

    def test_a_refusal_after_the_un_refund_keeps_the_throw_reason(self):
        """改回成功、补抛被拒 ⇒ 照旧落点名权限的那句人话（`THROW_FAILED_REASON`），可见。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted(orderSta="已退单", throw=ApiError("拒绝抛单", code=0))
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertIn("set_order_status", up.names())
        self.assertIn("throw_order", up.names())
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.reason, THROW_FAILED_LITERAL)

    def test_an_unconfirmed_rethrow_after_the_un_refund_is_a_failure(self):
        """改回成功、补抛后回读 `isThrowOrder != Y` ⇒ 不算恢复（与主路同一把确认闸）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(
            lookup={ORDER_NO: {"orderId": JID, "orderIdView": ORDER_NO,
                               "orderSta": "已退单", "isThrowOrder": "N"},
                    JID: {"orderId": JID, "isThrowOrder": "N"}},
            remote_exists=True,
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertIn("set_order_status", up.names())
        self.assertIn("throw_order", up.names())
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.reason, "抛单后回读确认失败（isThrowOrder 不是 Y）")

    def test_an_adopted_order_taken_by_someone_is_not_thrown_and_stays_visible(self):
        """⭐ 认领到的单**已被对方打手接走** ⇒ ⛔ 不补抛、不打扰对方；**链路必须落下**。
        这条正是业务方报的那个真机形状（「下家打手接了我方抛过去的单，我方还是待抢单」）：我们能做的不是再抛一次，而是**让这张单对快路 / 状态回拉可见**（链路行），由它们把「对方谁接了」认出来、在我方**自动接手**（钱算在这个代练员头上）。判据钉在**链路行**上——删掉 `_ensure_remote_order` 里那次 `_ensure_link`，本用例红。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted(orderSta="代练中", userName="对方打手甲")
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertNotIn("throw_order", up.names())
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertEqual(link.jidanpu_order_id, JID)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.reason, jidanpu_member.ADOPTED_NOT_UNASSIGNED_REASON)

    def test_the_launch_payload_comes_from_the_shared_builders(self):
        """⭐ 裁定 6：录单表单与顶层参数的**基底**只能来自 `jidanpu_common` 的那两个公共 builder。
        ⚠️ 本线在这份**自己的**表单上覆盖的键一个不多一个不少、**逐项列出**：`orderSoures`（来源值）/ `price`（下家接单价 = 我方发单价）/ `souresOrderID`（**有效来源单号**，业务方）/ `inputOrderKefu`（**下家录入客服 uid**，业务方裁定⑤ ⇒ ⛔ **不再发**模板里那个合成占位值 `'100'`）。判别力：少列一项就有一次真改动**不被任何用例看见**（这里改成整份相等断言）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            jidanpu_member.consume_event(event, client=CLIENT)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(sent["source_order_no"], ORDER_NO)
        self.assertEqual(
            sent["form_payload"],
            {
                **jidanpu_common.build_add_order_form(self.order),
                "orderSoures": "团团通系统",
                # ⭐ 业务方：**下家的接单价 = 我方的发单价**（本夹具 `hall_price=12.34`）。
                "price": "12.34",
                # ⭐ 业务方：来源单号用我方**来源单号**（本夹具为空 ⇒ 回落内置单号）。
                "souresOrderID": ORDER_NO,
                # ⭐ 业务方裁定⑤：录入客服填**我们那个下家账号自己**的员工 uid。
                "inputOrderKefu": DOWNSTREAM_STAFF_UID,
            },
        )
        # ⛔ 客户价（本夹具 `total_price=50.00`）**一个数都不许出现在录单载荷里**：
        #    那是我方的底价，泄露给另一个租户收不回来。**两句一起才有判别力**——
        #    只钉 `price == 发单价` 时，把两个价对调也能绿。
        self.assertEqual(sent["form_payload"]["price"], "12.34")
        self.assertNotIn("50.00", str(sent["form_payload"]))
        self.assertEqual(
            sent["top_level"],
            jidanpu_common.build_add_order_top_level(self.order, source_value="团团通系统"),
        )

    def test_the_legacy_form_still_sends_an_empty_game_area(self):
        """⭐⭐ 判据：**①式照旧发它原来的空串**（`gameArea` 那一格），⛔ 不许把②式的常量套到它身上。真机依据（kexing，端到端真跑）：**同一个空值**在①式的**登录式**接口上**录得进去**，而在②式的**开放**接口上被拒（`上游码 0：游戏大区为必填项`⇒ ②式全量录不过去）⇒ 两条线在**这一格上必须是两个不同的值**：
        - ②式：取订单上的区服、为空回落常量（`jidanpu_member_openapi_outbox.GAME_AREA_DEFAULT`，判据在 `tests/test_jidanpu_member_openapi_outbox.py::LaunchTests`）；- ①式：**一个字都不改**（本用例就是钉这件事的那条网）。
        判别力：把任何一个非空值（含那条常量）套到①式的表单 / 顶层参数上 ⇒ 本用例红。⚠️ 钉**字面量 `""`**：⛔ 不写 `ADD_ORDER_TEMPLATE["gameArea"]` —— 那两边同源，真把模板改了就两边一起变、**不会红**（`AGENTS.md` §5-G / E-0024 一族）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            jidanpu_member.consume_event(event, client=CLIENT)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(sent["form_payload"]["gameArea"], "")
        #: ⚠️ 顶层那一份（`ADD_ORDER_TOP_LEVEL`）**也**是空串 ⇒ 两处一起钉，⛔ 别只钉一处：
        #:    只钉表单时，把常量塞进顶层那份也能绿（而那一份同样会发到对方）。
        self.assertEqual(sent["top_level"]["gameArea"], "")

    def test_the_throw_carries_the_hall_price(self):
        """默认百分比（100，不打折）时抛单价就是发单价；**打折那一档**见下一条。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            jidanpu_member.consume_event(event, client=CLIENT)
        throw = dict(up.calls)["throw_order"]
        self.assertEqual(throw["remote_order_id"], JID)
        # 消费者从**库里**读订单 ⇒ 拿到的是 Decimal（不是建单时传进去的那个字符串）
        self.assertEqual(throw["fee"], Decimal("12.34"))

    def test_a_resumed_event_with_a_remote_id_never_records_again(self):
        """⭐ 可重入：payload 已有远端单号 ⇒ ⛔ 不重复录单（只补抛单）。"""
        event = self._event(
            jidanpu_member.KIND_LAUNCH,
            payload={"jidanpu_order_id": JID, "recorded": True},
        )
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertNotIn("add_order", up.names())
        self.assertNotIn("fetch_sources", up.names())
        self.assertIn("throw_order", up.names())

    def test_a_readback_failure_never_re_records_on_retry(self):
        """⭐ 断点：录单成功后取号失败 ⇒ 重试**只回读**，⛔ 绝不再灌一张真单。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        first = _Upstream(lookup={ORDER_NO: None})
        with _stub(first):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertIn("add_order", first.names())
        event.refresh_from_db()
        self.assertEqual(event.attempts, 1)
        self.assertTrue(event.payload["recorded"])
        self.assertFalse(event.payload.get("jidanpu_order_id"))

        second = _Upstream(lookup=self._launch_lookup(), remote_exists=True)
        with _stub(second):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertNotIn("add_order", second.names())
        self.assertIn("throw_order", second.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.payload["jidanpu_order_id"], JID)

    def test_a_recording_failure_envelope_is_a_human_failure(self):
        """写端点的失败信封必须被判失败（E-0087）：failed + attempts+1 + 人话。
        ⚠️ 本用例构造的 `ApiError` **不带 `body`**（模拟「没有业务壳 / 连不上」）⇒ 原因里
        只有我们那句人话。**带壳那一支**（`body={"msg": …}`）由下一条用例钉。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(add=ApiError("该来源单号已存在", code=0), lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertIn(jidanpu_member.RECORD_FAILED_REASON, event.reason)
        self.assertEqual(JidanpuMemberOrderLink.objects.count(), 0)

    def test_a_rejection_with_a_business_shell_keeps_the_upstream_message(self):
        """⭐ 2026-09-27 实测改准：**业务壳里那句 `msg` 必须留在原因里**（否则根因不可知）。
        判据钉**那句话本身**（⛔ 不是「reason 非空」——那样把 msg 丢掉照样绿）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(
            add=ApiError("", body={"code": 0, "msg": "系统要求来源单号不能为空", "data": ""}, code=0),
            lookup=self._launch_lookup(),
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        event.refresh_from_db()
        self.assertIn("系统要求来源单号不能为空", event.reason)
        self.assertIn(jidanpu_member.RECORD_FAILED_REASON, event.reason)

    def test_a_throw_failure_keeps_the_link_without_the_hall_flag(self):
        """抛单失败 ⇒ 事件 failed + **点名到权限**的人话；⭐ 链路行**已经落下**（`in_hall=False`）。
        ⚠️ **2026-09-27 有意改准**（真机形状）：链路行不再等抛单成功才建 —— 日志快路与状态回拉**都是按链路表找单**的，等抛单成功再建会让「录过单、抛单却一直失败」的单在我方**永远停在待抢单**（对方那张单我们压根看不见）。`in_hall=False` 是**实话**：它此刻确实没进对方大厅；消费者抛单成功后会把这一格补正成 `True`。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(throw=ApiError("拒绝抛单", code=0), lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertEqual(link.jidanpu_order_id, JID)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        # ⭐ spec §9.1 末段 / §13-R7：文案必须**点名**「将订单发到内部抢单大厅」——
        #    ⛔ 不是「非空即可」（那样把整句换空、换成别的权限都照样绿）。
        self.assertEqual(event.reason, THROW_FAILED_LITERAL)

    def test_a_throw_failure_puts_the_permission_hint_on_the_binding(self):
        """⭐ spec §9.1 / §13-R7：那句点名权限的人话必须**进绑定的 `last_error`**。
        判别力：把 `_round_failure_reason` 的逐条人话去掉（只留「本轮有 N 条台账事件仍失败，
        请查台账」）⇒ 本用例红——用户看到的仍是一次无因的「拒绝了」。"""
        self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(throw=ApiError("拒绝抛单", code=0), lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual(stats.failed, 1)
        row = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
        self.assertIn(THROW_PERMISSION_HINT, row.last_error)
        self.assertIsNotNone(row.last_error_at)

    def test_an_unconfirmed_throw_is_a_failure(self):
        """抛单回读 `isThrowOrder != Y` ⇒ 不算抛成功（⛔ 不凭调用不抛异常判成功）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(
            lookup={ORDER_NO: {"orderId": JID, "orderIdView": ORDER_NO},
                    JID: {"orderId": JID, "isThrowOrder": "N"}}
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        # 链路同样已经落下（同上一条的理由），只是 `in_hall` 仍是假：抛单没确认成功。
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")

    def test_a_soft_deleted_order_is_skipped_without_touching_upstream(self):
        self.order.deleted_at = timezone.now()
        self.order.save(update_fields=["deleted_at"])
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertNotEqual(event.reason, "")

    def test_an_order_without_a_hall_price_is_skipped_without_touching_upstream(self):
        """没填抛单价 ≠ 0：`None` 不推上游（照既有那条线的口径）。"""
        Order.objects.filter(pk=self.order.pk).update(hall_price=None)
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])


# ══ ②c ⭐ 我方撤回后再重抛（D-0318）══════════════════════════════


class _AssigneeUpstream(_Upstream):
    def __init__(self, *, assigned_at, remote_sta=LABEL_IN_PROGRESS):
        super().__init__(remote_sta=remote_sta)
        self.assigned_at = assigned_at
        self.taker_name = "synthetic-taker"

    def fetch_order_by_key1(self, client, *, key, expect_field, expect_value):
        row = super().fetch_order_by_key1(
            client, key=key, expect_field=expect_field, expect_value=expect_value,
        )
        if row is not None:
            row.update(recManName=self.taker_name, assTime=self.assigned_at)
        return row

    def throw_order(self, client, *, remote_order_id, fee):
        if self.taker_name:
            raise ApiError("原单仍有接手人", code=0)
        return super().throw_order(client, remote_order_id=remote_order_id, fee=fee)

    def call(self, method, path, **kwargs):
        if method == "GET" and path == "/OrderAjax/GetOrderAssLogs":
            self.calls.append(("fetch_ass_logs", kwargs["query"]))
            return [{"userId": 10001, "userName": self.taker_name}] if self.taker_name else []
        self.calls.append(("clear_remote_taker", (method, path, kwargs["data"])))
        self.taker_name = ""
        self.last_envelope = {"code": 1}
        return None


class RethrowAfterWithdrawTests(_Base):
    """⭐ 业务方 2026-10-27 口径：「已退单的状态先改成未分配然后重抛单就行了」。背景（业务方实测）：我方撤回 / 退单会把下家那张单退成 `orderSta=已退单`、`isThrowOrder=N`；之后再抛单会被对方拒（「发单失败，可能这些订单已设定了接手人…」）。以前只修了「**认领**」那一支（D-0257：`member.launch` 发现对方已有这张单时先改回未分配，那条腿是 `RecordPrimitives.refund_undo`），而**已经录过、已经在我方链路里**的那张单重新抛时没有任何前置修复 —— 本类钉住这一支。
    六条判据（⛔ 断言一律打**上游调用顺序**与**数据库行**，不打 logger）：① 远端「已退单」⇒ 先 `set_order_status("未分配")` **再**抛（顺序颠倒即红）；② 远端「代练中」（对方有真实接手人在做单）⇒ ⛔ 一次写都不打、⛔ 不抛，终态 `skipped` + 人话里点名读到的状态（不许静默，也不许打断对方干活）；③ 远端「未分配」⇒ 直接抛、**零次**改状态（防过度修复：不该改写就不写）；
    ④ 改状态被拒 ⇒ 抛单判 `failed` 且原因可见（⛔ 不被吞、⛔ 不接着抛）；⑤ 重复抛单幂等：第二次读到 `isThrowOrder=Y` ⇒ 不重复改状态、不重复抛；⑥ 改回成功但抛单仍被拒 ⇒ 原因必须点名「删除旧单重新录单」**不可用**（残差如实登记，⛔ 不拿不存在的 SDK 函数硬写）。⚠️ **③ 的判别力靠「调用序列」**：它断言前置回读**发生过且只发生一次**——把前置修复删掉（或退化成「永远直接抛」）⇒ 本用例红；把「永远先改未分配」那种过度修复写进去 ⇒ 也红。"""

    def _withdrawn_link(self):
        """我方退成过那张单的链路（`withdrawn_at` 非空 = 本地知道对方已经「已退单」）。"""
        link = self._link(in_hall=False)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=timezone.now()
        )
        return JidanpuMemberOrderLink.objects.get(pk=link.pk)

    def test_rethrow_waits_for_confirmed_withdrawal_without_using_an_attempt(self):
        link = self._link(in_hall=True)
        withdraw = self._event(jidanpu_member.KIND_WITHDRAW)
        launch = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True,
        )
        failing = _Upstream(remote_sta=LABEL_IN_PROGRESS, sta_after_write=LABEL_IN_PROGRESS)
        with _stub(failing):
            self.assertEqual(jidanpu_member.consume_event(withdraw, client=CLIENT), "failed")
            previous_calls = failing.names()
            self.assertEqual(jidanpu_member.consume_event(launch, client=CLIENT), "deferred")
            self.assertEqual(failing.names(), previous_calls)
        launch.refresh_from_db()
        self.assertEqual((launch.status, launch.attempts), ("pending", 0))

        confirmed = _Upstream(remote_sta=LABEL_IN_PROGRESS)
        with _stub(confirmed):
            self.assertEqual(jidanpu_member.consume_event(withdraw, client=CLIENT), "done")
            self.assertEqual(jidanpu_member.consume_event(launch, client=CLIENT), "done")
        self.assertEqual(
            [args["status_label"] for name, args in confirmed.calls if name == "set_order_status"],
            ["已退单", "未分配"],
        )
        self.assertEqual(confirmed.names().count("throw_order"), 1)
        link.refresh_from_db()
        withdraw.refresh_from_db()
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        self.assertGreater(link.listed_at, withdraw.processed_at)
        self.assertLess(launch.created_at, withdraw.processed_at)

    def _start_assigned_case(self, *, assigned_at=None):
        grant_games(self.user, self.game)
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=self.user)
        self._link(in_hall=False)
        client = make_logged_in_client(
            username="relaunch-clear-actor", groups=("客服",),
            nodes=("order.list", "order.change_status", "order.clear_taker",
                   "order.send_to_grab_hall", "order.withdraw_from_grab_hall"),
        )
        order_url = f"/api/business/orders/{self.order.order_no}"

        def post(action):
            response = client.post(f"{order_url}/{action}/", {}, content_type="application/json").json()
            self.assertEqual(response["code"], 0, response)

        assigned_at = assigned_at or timezone.now() - timedelta(minutes=5)
        up = _AssigneeUpstream(
            assigned_at=timezone.localtime(assigned_at).strftime("%Y-%m-%d %H:%M:%S"),
        )
        return post, up

    def test_clear_original_downstream_taker_before_restoring_and_rethrowing(self):
        post, up = self._start_assigned_case()
        with _stub(up):
            post("clear-taker")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)
            self.assertEqual(up.remote_sta, LABEL_REFUNDED)
            self.assertEqual(up.taker_name, "synthetic-taker")
            up.calls.clear()

            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertTrue(up.thrown)
        self.assertEqual(up.remote_sta, LABEL_UNASSIGNED)
        writes = [(name, args) for name, args in up.calls
                  if name in ("set_order_status", "clear_remote_taker", "throw_order")]
        self.assertEqual([name for name, _ in writes],
                         ["set_order_status", "clear_remote_taker", "set_order_status", "throw_order"])
        self.assertEqual(writes[1][1], ("POST", "/OrderAjax/CleanOrderZhiPaiByIDX",
                                       {"orderId": JID, "isDelEmp": "N"}))
        self.assertEqual(writes[2][1]["status_label"], LABEL_UNASSIGNED)
        self.assertEqual(writes[3][1]["remote_order_id"], JID)
        self.assertTrue(JidanpuMemberOrderLink.objects.get(
            binding=self.binding, order=self.order, jidanpu_order_id=JID,
        ).in_hall)

    def test_rethrow_can_clear_old_taker_after_an_earlier_restore(self):
        post, up = self._start_assigned_case()
        with _stub(up):
            post("clear-taker")
            post("throw-to-hall")
            withdrawal = JidanpuSyncEvent.objects.filter(
                target_ref=self.order.order_no, kind=KIND_WITHDRAW_LITERAL, status="pending",
            ).latest("pk")
            self.assertEqual(jidanpu_member.consume_event(withdrawal, client=up), "done")
            up.remote_sta = LABEL_UNASSIGNED
            up.calls.clear()
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertTrue(up.thrown)
        self.assertIn("clear_remote_taker", up.names())
        self.assertNotIn("set_order_status", up.names())

    def test_clear_readback_must_confirm_removal_before_any_restore(self):
        post, up = self._start_assigned_case()
        original_call = up.call

        def still_has_assignment(method, path, **kwargs):
            if method == "GET" and not up.taker_name:
                return [{"userId": 10001, "userName": "synthetic-taker"}]
            return original_call(method, path, **kwargs)

        with _stub(up), mock.patch.object(up, "call", side_effect=still_has_assignment):
            post("clear-taker")
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertEqual(up.remote_sta, LABEL_REFUNDED)
        self.assertFalse(up.thrown)
        launch = JidanpuSyncEvent.objects.get(target_ref=self.order.order_no, kind=KIND_LAUNCH_LITERAL)
        self.assertEqual(launch.status, "failed")
        self.assertIn("未确认已清除接手人", launch.reason)
        with _stub(up), mock.patch.object(up, "call", side_effect=still_has_assignment):
            jidanpu_member.run_outbox(
                self.binding, now=timezone.now() + timedelta(minutes=5), client=up,
            )
        launch.refresh_from_db()
        self.assertEqual(launch.status, "failed")
        self.assertEqual(up.remote_sta, LABEL_REFUNDED)
        self.assertFalse(up.thrown)
        self.assertEqual(up.names().count("clear_remote_taker"), 1)

    def test_rethrow_does_not_clear_multiple_current_remote_takers(self):
        post, up = self._start_assigned_case()
        assigned = [
            {"userId": 10001, "userName": "synthetic-taker"},
            {"userId": 10002, "userName": "synthetic-another-taker"},
        ]
        with _stub(up), mock.patch.object(jidanpu, "fetch_ass_logs", return_value=assigned):
            post("clear-taker")
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertEqual(up.taker_name, "synthetic-taker")
        self.assertNotIn("clear_remote_taker", up.names())
        self.assertFalse(up.thrown)
        launch = JidanpuSyncEvent.objects.get(target_ref=self.order.order_no, kind=KIND_LAUNCH_LITERAL)
        self.assertEqual(launch.status, "skipped")
        self.assertIn("不能确认只有原接手人", launch.reason)

    def test_rethrow_waits_when_current_taker_field_cannot_be_read(self):
        post, up = self._start_assigned_case()
        remote_read = up.fetch_order_by_key1

        def missing_taker(*args, **kwargs):
            row = remote_read(*args, **kwargs)
            if row is not None:
                row.pop("recManName", None)
            return row

        with _stub(up), mock.patch.object(jidanpu, "fetch_order_by_key1", side_effect=missing_taker):
            post("clear-taker")
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertEqual(up.remote_sta, LABEL_REFUNDED)
        self.assertEqual(up.taker_name, "synthetic-taker")
        self.assertNotIn("clear_remote_taker", up.names())
        self.assertFalse(up.thrown)
        launch = JidanpuSyncEvent.objects.get(target_ref=self.order.order_no, kind=KIND_LAUNCH_LITERAL)
        self.assertEqual(launch.status, "failed")
        self.assertIn("回读缺少接手人信息", launch.reason)

    def test_rethrow_does_not_clear_a_remote_assignment_newer_than_local_clear(self):
        post, up = self._start_assigned_case()
        with _stub(up):
            post("clear-taker")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)
            up.assigned_at = timezone.localtime(timezone.now() + timedelta(seconds=3)).strftime("%Y-%m-%d %H:%M:%S")
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertEqual(up.taker_name, "synthetic-taker")
        self.assertNotIn("clear_remote_taker", up.names())
        self.assertFalse(up.thrown)
        launch = JidanpuSyncEvent.objects.get(target_ref=self.order.order_no, kind=KIND_LAUNCH_LITERAL)
        self.assertEqual(launch.status, "skipped")
        self.assertIn("不能确认是本轮清掉的旧接手人", launch.reason)

    def test_newer_withdraw_prevents_queued_old_clear_and_throw(self):
        post, up = self._start_assigned_case()
        with _stub(up):
            post("clear-taker")
            post("throw-to-hall")
            post("withdraw")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)

        self.assertEqual(up.remote_sta, LABEL_REFUNDED)
        self.assertEqual(up.taker_name, "synthetic-taker")
        self.assertNotIn("clear_remote_taker", up.names())
        self.assertFalse(up.thrown)
        launch = JidanpuSyncEvent.objects.get(target_ref=self.order.order_no, kind=KIND_LAUNCH_LITERAL)
        self.assertEqual(launch.status, "skipped")

    def test_withdraw_during_remote_clear_stops_old_restore_and_throw(self):
        post, up = self._start_assigned_case()
        original_call = up.call

        def withdraw_during_clear(method, path, **kwargs):
            result = original_call(method, path, **kwargs)
            if method == "POST":
                post("withdraw")
            return result

        with _stub(up), mock.patch.object(up, "call", side_effect=withdraw_during_clear):
            post("clear-taker")
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=up)
            jidanpu_member.run_outbox(
                self.binding, now=timezone.now() + timedelta(minutes=5), client=up,
            )

        self.assertEqual(up.remote_sta, LABEL_REFUNDED)
        self.assertFalse(up.thrown)
        launch = JidanpuSyncEvent.objects.get(target_ref=self.order.order_no, kind=KIND_LAUNCH_LITERAL)
        self.assertEqual(launch.status, "skipped")

    def test_clear_taker_and_rethrow_after_a_failed_launch(self):
        grant_games(self.user, self.game)
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=self.user)
        link = self._link(in_hall=False)
        client = make_logged_in_client(
            username="relaunch-retry-actor", groups=("客服",),
            nodes=("order.list", "order.change_status", "order.clear_taker",
                   "order.assign_taker", "order.send_to_grab_hall"),
        )
        order_url = f"/api/business/orders/{self.order.order_no}"

        def post(action, body=None):
            response = client.post(
                f"{order_url}/{action}/", body or {}, content_type="application/json"
            ).json()
            self.assertEqual(response["code"], 0, response)

        up = _Upstream(remote_sta=LABEL_IN_PROGRESS, throw=ApiError("暂时拒绝抛单", code=0))
        with _stub(up):
            post("clear-taker")
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=CLIENT)

            failed_launch = JidanpuSyncEvent.objects.get(
                kind=KIND_LAUNCH_LITERAL, target_ref=self.order.order_no,
            )
            self.assertEqual((failed_launch.status, failed_launch.attempts), ("failed", 1))
            self.assertEqual(up.remote_sta, LABEL_UNASSIGNED)
            self.assertFalse(up.thrown)

            post("assign", {"user_id": self.user.pk})
            remote_row = {"orderId": JID, "orderIdView": ORDER_NO,
                          "orderSta": LABEL_UNASSIGNED, "isThrowOrder": "N"}
            with mock.patch.object(jidanpu, "find_order_for_repair", return_value=remote_row):
                jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=CLIENT)
            post("clear-taker")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=CLIENT)
            self.assertEqual(up.remote_sta, LABEL_REFUNDED)

            up.throw = None
            up.calls.clear()
            post("throw-to-hall")
            jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=CLIENT)

        writes = [(name, args["remote_order_id"], args.get("status_label"))
                  for name, args in up.calls if name in ("set_order_status", "throw_order")]
        self.assertEqual(writes, [
            ("set_order_status", JID, LABEL_REFUNDED),
            ("set_order_status", JID, LABEL_UNASSIGNED),
            ("throw_order", JID, None),
        ])
        self.assertNotIn("add_order", up.names())
        self.assertTrue(up.thrown)
        link.refresh_from_db()
        self.assertTrue(link.in_hall)
        self.assertEqual(link.jidanpu_order_id, JID)
        self.assertIsNone(link.withdrawn_at)

    def test_withdraw_after_requeue_prevents_the_new_and_old_launches(self):
        grant_games(self.user, self.game)
        link = self._link(in_hall=False)
        stale_launch = self._launch_event()
        first_failure = timezone.now()
        JidanpuSyncEvent.objects.filter(pk=stale_launch.pk).update(
            status="failed", attempts=1, processed_at=first_failure,
        )
        client = make_logged_in_client(
            username="relaunch-withdraw-actor", groups=("客服",),
            nodes=("order.list", "order.send_to_grab_hall", "order.withdraw_from_grab_hall"),
        )
        for action in ("withdraw", "throw-to-hall", "withdraw"):
            response = client.post(
                f"/api/business/orders/{self.order.order_no}/{action}/", {},
                content_type="application/json",
            ).json()
            self.assertEqual(response["code"], 0, response)

        up = _Upstream(remote_sta=LABEL_UNASSIGNED)
        with _stub(up):
            jidanpu_member.run_outbox(
                self.binding, now=first_failure + timedelta(minutes=10), client=CLIENT,
            )

        writes = [(name, args["remote_order_id"], args.get("status_label"))
                  for name, args in up.calls if name in ("set_order_status", "throw_order")]
        self.assertEqual(writes, [("set_order_status", JID, LABEL_REFUNDED)])
        self.assertFalse(up.thrown)
        link.refresh_from_db()
        self.assertFalse(link.in_hall)
        self.assertIsNotNone(link.withdrawn_at)
        launches = JidanpuSyncEvent.objects.filter(
            kind=KIND_LAUNCH_LITERAL, target_ref=self.order.order_no,
        ).order_by("id")
        self.assertEqual(list(launches.values_list("status", flat=True)), ["skipped", "skipped"])

    def test_rethrow_waits_for_this_withdrawal_instead_of_an_older_confirmation(self):
        grant_games(self.user, self.game)
        self._link(in_hall=False)
        old_withdrawal = self._event(jidanpu_member.KIND_WITHDRAW)
        first_failure = timezone.now()
        JidanpuSyncEvent.objects.filter(pk=old_withdrawal.pk).update(
            status="failed", attempts=1, processed_at=first_failure,
        )
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_WITHDRAW, status="done",
        )
        client = make_logged_in_client(
            username="relaunch-latest-withdraw-actor", groups=("客服",),
            nodes=("order.list", "order.send_to_grab_hall", "order.withdraw_from_grab_hall"),
        )
        for action in ("withdraw", "throw-to-hall"):
            response = client.post(
                f"/api/business/orders/{self.order.order_no}/{action}/", {},
                content_type="application/json",
            ).json()
            self.assertEqual(response["code"], 0, response)

        up = _Upstream(remote_sta=LABEL_UNASSIGNED, sta_after_write=LABEL_UNASSIGNED)
        with _stub(up):
            jidanpu_member.run_outbox(
                self.binding, now=first_failure + timedelta(seconds=20), client=CLIENT,
            )
            self.assertFalse(up.thrown, "下一轮撤回未确认，不能凭旧确认抛单")

            up.calls.clear()
            up.sta_after_write = _UNSET
            jidanpu_member.run_outbox(
                self.binding, now=first_failure + timedelta(seconds=125), client=CLIENT,
            )
            self.assertEqual(up.calls, [], "过时的撤回到了重试时刻也不能打远端")

            jidanpu_member.run_outbox(
                self.binding, now=first_failure + timedelta(seconds=150), client=CLIENT,
            )

        writes = [(name, args["remote_order_id"], args.get("status_label"))
                  for name, args in up.calls if name in ("set_order_status", "throw_order")]
        self.assertEqual(writes, [
            ("set_order_status", JID, LABEL_REFUNDED),
            ("set_order_status", JID, LABEL_UNASSIGNED),
            ("throw_order", JID, None),
        ])
        self.assertTrue(up.thrown)

    def test_a_legacy_rearmed_withdrawal_keeps_its_newer_intent(self):
        self._link(in_hall=False)
        old_row = self._event(jidanpu_member.KIND_WITHDRAW)
        jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_WITHDRAW, status="done",
        )
        now = timezone.now()
        JidanpuSyncEvent.objects.filter(pk=old_row.pk).update(
            payload={**old_row.payload, "rearmed_at": now.isoformat()},
        )
        up = _Upstream(remote_sta=LABEL_UNASSIGNED)
        with _stub(up):
            jidanpu_member.run_outbox(self.binding, now=now, client=CLIENT)
        self.assertEqual(up.remote_sta, LABEL_REFUNDED)
        old_row.refresh_from_db()
        self.assertEqual(old_row.status, "done")

    def test_a_newer_withdrawal_blocks_an_old_launch_before_any_remote_call(self):
        self._link(in_hall=True)
        stale_launch = self._launch_event()
        self._event(jidanpu_member.KIND_WITHDRAW)
        upstream = _Upstream(remote_sta=LABEL_UNASSIGNED)
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(stale_launch, client=CLIENT), "skipped")
        stale_launch.refresh_from_db()
        self.assertIn("旧事件", stale_launch.reason)
        self.assertEqual(upstream.names(), [])

    def test_local_claim_after_launch_queue_prevents_rethrow(self):
        self._link(in_hall=True)
        self._event(jidanpu_member.KIND_WITHDRAW)
        launch = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH, rearm=True,
        )
        local_taker = User.objects.create_user(username="synthetic-local-taker")
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=local_taker)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        upstream = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(launch, client=CLIENT), "skipped")
        self.assertEqual(upstream.names(), [])
        self.assertEqual(Performance.objects.get(order=self.order).user_id, local_taker.pk)

    def _launch_event(self):
        return jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )

    def _link_row(self):
        return JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)

    def test_a_withdrawn_remote_order_is_un_refunded_then_rethrown(self):
        """① 已退单 ⇒ 先改回「未分配」再抛：调用序列 = 回读 → 改状态 → 抛 → 回读确认。"""
        self._withdrawn_link()
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        event = self._launch_event()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "set_order_status",
             "throw_order", "fetch_order_by_key1"],
        )
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_UNASSIGNED)
        # ⚠️ 这一跳**不带 remark**（照 `_member_write_remote` 那一档的既有口径；退单那一档
        #    的逐字原因 `WITHDRAW_REMARK` 是它自己的特例，⛔ 别搬过来）。
        self.assertEqual(sent["remark"], "")
        link = self._link_row()
        self.assertTrue(link.in_hall)
        # ⭐ 「已经退成过」那个标记必须清掉，否则对账 / 补推会一直把它当已退单。
        self.assertIsNone(link.withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, "")

    def test_a_remote_order_with_a_real_taker_is_not_touched(self):
        """② 对方「代练中」= 有真接手人在做 ⇒ ⛔ 不改状态、⛔ 不抛，响亮跳过并点名状态。"""
        self._withdrawn_link()
        up = _Upstream(remote_sta=LABEL_IN_PROGRESS)
        event = self._launch_event()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        # 只读一次（前置回读）——⛔ 一次写都不打、⛔ 不抛。
        self.assertEqual(up.names(), ["fetch_order_by_key1"])
        link = self._link_row()
        self.assertFalse(link.in_hall)
        self.assertIsNotNone(link.withdrawn_at, "没动对方那张单 ⇒ 本地标记也不许乱改")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn(LABEL_IN_PROGRESS, event.reason)

    def test_an_unassigned_remote_order_is_thrown_without_an_extra_status_write(self):
        """③ 远端「未分配」⇒ 直接抛、零次改状态（只读一次 + 抛一次 + 回读一次）。"""
        self._withdrawn_link()
        up = _Upstream(remote_sta=LABEL_UNASSIGNED)
        event = self._launch_event()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "throw_order", "fetch_order_by_key1"],
        )
        self.assertNotIn("set_order_status", up.names())
        link = self._link_row()
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)

    def test_a_rejected_un_refund_fails_loudly_and_never_throws(self):
        """④ 改回「未分配」被拒 ⇒ 抛单判 `failed` + 原因可见（⛔ 被吞、⛔ 接着抛）。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_sta=LABEL_REFUNDED,
            status=ApiError(
                "", body={"code": 0, "msg": "当前状态不允许该操作", "data": ""}, code=0
            ),
        )
        event = self._launch_event()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertEqual(up.names(), ["fetch_order_by_key1", "set_order_status"])
        self.assertNotIn("throw_order", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        # 逐字：既有那句「改回未成功」+ 上游原话（⛔ 不许只剩一句「拒绝了」）。
        self.assertIn("改回「未分配」未成功", event.reason)
        self.assertIn("当前状态不允许该操作", event.reason)

    def test_a_second_rethrow_repeats_neither_the_un_refund_nor_the_throw(self):
        """⑤ 重复抛单幂等：第二次读到 `isThrowOrder=Y` ⇒ 不再改状态、不再抛。"""
        self._withdrawn_link()
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        first = self._launch_event()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(first, client=CLIENT), "done")
            second = self._launch_event()
            self.assertEqual(jidanpu_member.consume_event(second, client=CLIENT), "done")
        self.assertEqual(up.names().count("set_order_status"), 1)
        self.assertEqual(up.names().count("throw_order"), 1)
        # 第一次 = 前置回读 + 写后回读；第二次只有前置回读（读到已在厅 ⇒ 目标态已达成）。
        self.assertEqual(up.names().count("fetch_order_by_key1"), 3)
        link = self._link_row()
        self.assertTrue(link.in_hall)
        second.refresh_from_db()
        self.assertEqual(second.status, "done")

    def test_a_refusal_after_the_un_refund_names_the_unavailable_delete_fallback(self):
        """⑥ 改回成功、抛单仍被拒 ⇒ 点名「删除旧单重新录单」不可用（残差如实登记）。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_sta=LABEL_REFUNDED,
            throw=ApiError(
                "", body={"code": 0, "msg": "拒绝抛单", "data": ""}, code=0
            ),
        )
        event = self._launch_event()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertIn("set_order_status", up.names())
        self.assertIn("throw_order", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertIn("但抛单仍被拒", event.reason)
        self.assertIn("没有删单的 SDK 函数", event.reason)
        self.assertIn("拒绝抛单", event.reason)

    def test_a_stale_launch_event_is_not_un_refunded_nor_rethrown(self):
        """⭐ 评审 I2：撤回**之前**排的 launch 自动重试 ⇒ 不改状态、不重抛。
        现场：一条早先失败的 `member.launch` 在我方撤回（远端已退单、`link.withdrawn_at`已落）之后被 `run_outbox` 自动重试。若照「已退单 ⇒ 改回未分配 + 重抛」办，**我方已经撤回的单会被系统自己挂回对方大厅**。判据：`event.created_at <= link.withdrawn_at`（旧事件的排队时刻不晚于撤回完成时刻）⇒ 终态跳过 + 人话点名「旧事件自动重试」，⛔ 一次 `set_order_status`、⛔ 一次抛单。"""
        link = self._link(in_hall=False)
        withdrawn_at = timezone.now()
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=withdrawn_at
        )
        event = self._launch_event()
        # 旧事件：早先失败、排队发生在撤回**之前**（`run_outbox` 之后会按退避自动重试它）。
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=1,
            created_at=withdrawn_at - timedelta(minutes=5),
        )
        event.refresh_from_db()
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        # 只读一次（判远端是不是已退单）；⛔ 零次改状态、⛔ 零次抛单。
        self.assertEqual(up.names(), ["fetch_order_by_key1"])
        self.assertNotIn("set_order_status", up.names())
        self.assertNotIn("throw_order", up.names())
        link = self._link_row()
        self.assertFalse(link.in_hall)
        self.assertEqual(
            link.withdrawn_at, withdrawn_at, "没动对方那张单 ⇒ 本地撤回标记也不许乱改"
        )
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("撤回", event.reason)
        self.assertIn("旧事件", event.reason)

    def test_a_launch_queued_after_the_withdraw_is_un_refunded_then_rethrown(self):
        """⭐ 人工重抛（`event.created_at > link.withdrawn_at`）⇒ 照旧先改未分配再抛。
        与上一条**同一个闸**的正向面：撤回之后**新排**的 launch 是人的真实动作，
        必须能恢复「撤单 → 上家退单 → 重抛」这条业务流。"""
        link = self._link(in_hall=False)
        withdrawn_at = timezone.now() - timedelta(minutes=5)
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=withdrawn_at
        )
        event = self._launch_event()
        self.assertGreater(event.created_at, withdrawn_at)
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "set_order_status",
             "throw_order", "fetch_order_by_key1"],
        )
        self.assertEqual(dict(up.calls)["set_order_status"]["status_label"], LABEL_UNASSIGNED)
        link = self._link_row()
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_link_without_a_withdraw_mark_keeps_the_old_behaviour(self):
        """⭐ `withdrawn_at` 为空 ⇒ 不是撤回后的重试，按原逻辑（改回未分配 + 重抛）。
        防过度修复：闸只拦「撤回完成过、且事件比它旧」这一格；从没退成过的链路
        （`withdrawn_at is None`）必须与修复前逐字同行为。"""
        self._link(in_hall=False)  # `withdrawn_at` 默认 None
        event = self._launch_event()
        self.assertIsNone(self._link_row().withdrawn_at)
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("set_order_status", up.names())
        self.assertIn("throw_order", up.names())
        self.assertIsNone(self._link_row().withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_the_first_manual_rethrow_after_withdraw_marks_the_reused_event(self):
        """⭐ 残差 A（2026-10-27）：撤回后**第一次**点击必须能重抛（即使复用了旧行）。
        现场：撤回后客服点一次「发单」⇒ `queue_hall_launch` →`queue_member_event(..., rearm=True)` 命中台账里那条旧的 `failed` launch（`created_at` 早于撤回时刻）。若只看 `created_at`，这次人工重抛会被闸当成「旧事件自动重试」跳过——业务方明确说「点一次就该抛出去」。判据：复用行上写 `payload["rearmed_at"]`（人点那一刻）⇒ 闸取它 ⇒ 改回未分配 + 抛。"""
        # 让 `queue_hall_launch` 的归属判据命中这份绑定（否则它一个事件都不排）。
        grant_games(self.user, self.game)
        link = self._link(in_hall=False)
        withdrawn_at = timezone.now()
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=withdrawn_at
        )
        old = self._launch_event()
        JidanpuSyncEvent.objects.filter(pk=old.pk).update(
            status="failed", attempts=1,
            created_at=withdrawn_at - timedelta(minutes=5),
        )
        old.refresh_from_db()
        # 人工重抛（这就是 `/throw-to-hall/` 端点走的那一段）。
        self.assertEqual(jidanpu_member.queue_hall_launch(self.order), 1)
        event = JidanpuSyncEvent.objects.get(pk=old.pk)
        self.assertEqual(event.pk, old.pk, "人工重抛复用了那条旧行（真实去重行为）")
        self.assertIn("rearmed_at", event.payload)
        rearmed_at = datetime.fromisoformat(event.payload["rearmed_at"])
        self.assertGreater(rearmed_at, withdrawn_at, "人点那一刻必须晚于撤回完成时刻")
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        # 照旧先改回「未分配」再抛（与 D-0318 的恢复腿一致）。
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "set_order_status",
             "throw_order", "fetch_order_by_key1"],
        )
        self.assertEqual(dict(up.calls)["set_order_status"]["status_label"], LABEL_UNASSIGNED)
        link = self._link_row()
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_the_periodic_reconcile_to_launch_is_not_a_manual_rearm(self):
        """④ 自动对账的 `to_launch`（不传 `rearm`）⛔ 不许被当成人工重抛。
        `reconcile_and_queue` 复用同一条旧 `failed` launch 时不会写 `rearmed_at`
        ⇒ 闸仍按 `created_at` 判 ⇒ 真正的旧事件自动重排仍跳过（⛔ 不会把撤回的单挂回去）。"""
        link = self._link(in_hall=False)
        withdrawn_at = timezone.now()
        JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
            withdrawn_at=withdrawn_at
        )
        old = self._launch_event()
        JidanpuSyncEvent.objects.filter(pk=old.pk).update(
            status="failed", attempts=1,
            created_at=withdrawn_at - timedelta(minutes=5),
        )
        old.refresh_from_db()
        result = jidanpu_member.ReconcileResult(
            to_launch=(
                jidanpu_member.ReconcileItem(
                    order=self.order, shape="IN_HALL", reason="合成：对账要求补抛"
                ),
            ),
            at=withdrawn_at,
        )
        with mock.patch.object(jidanpu_member, "reconcile", return_value=result):
            jidanpu_member.reconcile_and_queue(self.binding, now=withdrawn_at)
        event = JidanpuSyncEvent.objects.get(pk=old.pk)
        self.assertNotIn("rearmed_at", event.payload or {})
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        # 只读一次（判远端已退单）；⛔ 不改状态、⛔ 不抛。
        self.assertEqual(up.names(), ["fetch_order_by_key1"])
        self.assertEqual(self._link_row().withdrawn_at, withdrawn_at)


# ══ ②d ⭐ D-0359 §11：查无此单的有界放行——撤回单人工重抛 ⇒ 全新建卡════


class ThrowPrepareRecreateTests(_Base):
    """⭐ D-0359 §11（2026-11-01，kexing member#4 熔断事件）：`_member_throw_prepare`对「查无此单」的有界放行——生产现场 10 张撤回过的待抢单（`withdrawn_at` 已落）被人工重抛，店里旧卡已被删除 ⇒ prepare 一律按 `THROW_PREPARE_UNREADABLE_REASON`拒抛循环、20 连败引爆 launch 读回熔断，**所有新抛单被静默拦下**。修复口径（业务方令「先给我恢复功能」，行为细化、非推翻 E-0004/D-0318 的保守面）：「查无此单」在**三重受控条件**下是可判定事实——① 人工重抛（事件 payload 带`rearmed_at`）；② 本地链接退成过（`withdrawn_at` 非空）；③ 回读是逐字查无而非读失败。三条**缺一不可**（判据只有 `_member_throw_prepare` 一处）；齐了 ⇒ 全新建卡。⭐⭐ **D-0362（业务方拍板）反转登记（原口径段上文保留作历史）**：§11 的③条件（逐字查无）升格为**单条件终态事实**——①②不再限制放行。本类用例按新口径**原位反转**并逐条注明：③④⑥⑩四条由「仍拒抛」反转为「放行新建」（③⑥⑩ = 自动首遇 ⇒ 自动新建 + `member.no_order_recreated` 终态行；④ = 人工但从未退成过 ⇒ 人工路径直通、落 AUTO 档人话）；①②⑤⑦⑧⑨不受影响（人工经典档人话照旧 / 卡存在走既有档 / 窄竞态四条腿照旧）。
    ⚠️ §11 当年的**变异钉**（⑥：删 `if rearmed and withdrawn:` ⇒ ⑥红）随⑥反转**失效**——防循环保护的新变异钉（删「上限已用」判据 ⇒ 防循环主钉红）迁往`tests/test_jidanpu_no_order_newcard.py`（先红后绿证据在该文件 docstring）。新口径下的剩余拒抛面只剩**含糊失败**（`ApiError` ⇒ 仍按`THROW_PREPARE_UNREADABLE_REASON` 拒抛循环 + 燃料）；「再遇查无 ⇒ 转人工」与「挂起期查无 ⇒ 拦截不新建」两档终态行也在新模块钉。判据清单（D-0362 反转后的**现行为**；⛔ 断言一律打**上游调用序列**与**数据库行**，不打 logger）：① 三条件齐（人工经典档）⇒ 走**既有全新录单路**（`add_order` 建新卡）+ 抛的是**新**单号，链路换绑到新卡、`done` 行带放行人话（排障/审计的唯一现场）；⭐ 人工路径**不写** `member.no_order_recreated` 终态行——上限是「**自动**新建」的账，人工动作不占它（「人工重抛走 §11 手动路径」不限次的判据）；② 复用旧行的进度键（payload `jidanpu_order_id` / `recorded`）必须被拆掉——⛔ 不拆 = `ensure_remote` 永远短路拿旧号，重录根本不会发生；③ ⭐ D-0362 反转：自动重试（缺 `rearmed`）+ 撤回过 + 查无 ⇒ **自动首遇**：放行新建 + 落自动新建终态行（每（单，绑定）上限 1 次的那笔账从这里记起）；
    ④ ⭐ D-0362 反转：人工重抛但**从没退成过**（缺 `withdrawn`）⇒ 人工路径直通新建（人话落 AUTO 档那句——「撤回单人工重抛」那句只属于人工经典档）；⑤ 缺「查无」（卡存在·已退单）⇒ **原样**走既有「改回未分配再抛」档，⛔ 不重录；⑥ ⭐ D-0362 反转（原 §11 变异钉，随反转失效）：非撤回单 + 自动重试 + 查无⇒ **自动首遇**：放行新建 + 落终态行；⑦ ⭐ 窄竞态（评审 S1）：重录的预读按**来源单号**认领到一张**已挂在厅**的卡（prepare 按旧单号查无、两把钥匙窗口不同；生产实锚 = 同来源号僵尸卡）⇒ 并入 `already` 幂等腿收口（补链路、不炸、不重录、不抛）；⑧ ⭐ 窄竞态认领到**已退单**卡 ⇒ 照既有语义走「改回未分配再抛」腿（对认领到的单号改状态），⛔ 不在 recreate 支里复刻任何一条腿、⛔ 掉进块尾响亮炸（那会打断整轮 outbox）；⑨ ⭐ 评审 S3：⑦⑧这类「认领」结局的台账**不许**落「全新建卡」那句人话（实际是认领、没建卡）；⑩ ⭐ D-0362 反转（原评审 S4 拒抛钉）：`rearmed_at` **早于** `withdrawn_at` 的旧行=「人点过、后来又撤了」的自动重试 ⇒ S4 分档判据**本身没变**（仍正确地认成自动，`rearmed_manual` 为假）⇒ 走自动首遇新建（人话落 AUTO 档那句）——变的只是自动档的处置（拒抛循环 → 上限内新建）。"""

    def _link_row(self):
        return JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)

    def _withdrawn_link(self, *, withdrawn=True):
        """链接行；`withdrawn=True` 落「退成过」标记（§11 条件②的现场）。"""
        link = self._link(in_hall=False)
        if withdrawn:
            JidanpuMemberOrderLink.objects.filter(pk=link.pk).update(
                withdrawn_at=timezone.now()
            )
        return link

    def _launch_event(self, *, rearmed=False, rearmed_at=None, progress=None):
        """排一条 launch；`rearmed=True` 时 payload 带 `rearmed_at`（人工重抛的现场，`queue_member_event(rearm=True)` 复用旧行时写的就是这个键）；`rearmed_at`
        显式给时刻（评审 S4：构造「早于撤回」的旧标记）；`progress` = 复用旧行可能残留的进度键（`jidanpu_order_id` / `recorded`）。"""
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        payload = dict(event.payload or {})
        if rearmed or rearmed_at is not None:
            payload["rearmed_at"] = (
                rearmed_at if rearmed_at is not None else timezone.now()
            ).isoformat()
        payload.update(progress or {})
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(payload=payload)
        event.refresh_from_db()
        return event

    #: 「全新建卡」那一档放行人话的**字面值**（⛔ 不从常量派生，E-0024）。
    RECREATE_REASON_LITERAL = "撤回单人工重抛：店里查无旧卡 ⇒ 全新建卡"
    #: ⭐ D-0362：非人工经典档放行人话的**字面值**（AUTO 档；同上，E-0024）。
    RECREATE_AUTO_REASON_LITERAL = "对端明确查无此单，按拍板转新建"
    #: ⭐ D-0362：自动新建终态行（`member.no_order_recreated`）人话的**字面值**。
    NO_ORDER_RECREATED_REASON_LITERAL = (
        "对端明确查无此单，按拍板转新建（自动新建，每单限一次）"
    )
    #: 拒抛循环那句的**前缀**字面值（同上，E-0024）。
    UNREADABLE_PREFIX_LITERAL = "抛单前回读对方那张单的状态失败"

    def test_a_manual_rethrow_of_a_withdrawn_order_with_the_card_gone_recreates(self):
        """① 三条件齐 ⇒ 走全新录单路（add_order）+ 抛**新**卡，done 带放行人话。
        调用序列 = 前置回读（旧号查无）→ 取来源 → 录单前预读（来源号查无）→
        **add_order（建新卡）**→ 回读拿新号 → 抛新卡 → 回读确认。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._launch_event(rearmed=True)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "fetch_sources", "fetch_order_by_key1",
             "own_staff_uid", "add_order", "fetch_order_by_key1",
             "throw_order", "fetch_order_by_key1"],
        )
        # ⭐ 断言到**建卡调用**：录单真的发生了，且抛的 / 落链路的都是**新**单号。
        self.assertEqual(dict(up.calls)["throw_order"]["remote_order_id"], JID_2)
        link = self._link_row()
        self.assertEqual(link.jidanpu_order_id, JID_2)
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(
                binding=self.binding, order=self.order
            ).count(),
            1,
            "旧链接行被拆、新卡落地后恰好一行（唯一约束下的删+建）",
        )
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        # ⭐ done 行带放行人话：链路的远端单号换了，台账得说清为什么。
        self.assertEqual(event.reason, self.RECREATE_REASON_LITERAL)
        # ⭐ D-0362：人工经典档**不写**自动新建终态行（上限是「自动新建」的账，
        #    人工动作不占它 ⇒ 人工重抛不限次的判据就钉在这里）。
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="member.no_order_recreated", target_ref=ORDER_NO
            ).exists()
        )
        # 进度键换成了**新**号；人工重抛的事实键不许被顺带拆掉。
        self.assertEqual(event.payload.get("jidanpu_order_id"), JID_2)
        self.assertIn("rearmed_at", event.payload)

    def test_a_reused_row_with_old_progress_keys_still_recreates(self):
        """② 复用旧行残留进度键 ⇒ 旧锚必须被拆掉，重录照样发生。
        生产现场正是这个形状：`queue_member_event(rearm=True)` 复用旧 launch 行时「已有 payload 的进度键全部保留」。锚不拆 = `ensure_remote` 短路拿旧号，prepare 又查无 ⇒ 拒抛循环原样（熔断照炸）。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._launch_event(
            rearmed=True,
            progress={"jidanpu_order_id": JID, "recorded": True},
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["throw_order"]["remote_order_id"], JID_2)
        self.assertEqual(self._link_row().jidanpu_order_id, JID_2)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, self.RECREATE_REASON_LITERAL)

    def test_an_automatic_retry_of_a_withdrawn_order_now_recreates(self):
        """③ ⭐ D-0362 反转（原「仍拒抛」）：自动重试（缺 `rearmed`）+ 撤回过 + 查无⇒ **自动首遇**：放行新建 + 落自动新建终态行（上限账本的第一笔）。
        原口径（§11，历史）：自动重试凑不齐三条件 ⇒ 拒抛循环、喂熔断燃料。新口径（D-0362）：逐字查无是单条件终态事实，自动档在上限内直接新建——这正是喂闸死结（E-0312）的断根处：老单不再无限重排，第一轮即终态+新卡。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._launch_event()  # 没有 rearmed_at ⇒ 对账重排 / 旧行自动重试的形状
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["throw_order"]["remote_order_id"], JID_2)
        link = self._link_row()
        self.assertEqual(link.jidanpu_order_id, JID_2)
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at, "新建成功 ⇒ 撤回标记随旧链接行一起拆掉")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        # ⭐ D-0362：自动档的放行人话 = AUTO 档那句（不是「撤回单人工重抛」那句）。
        self.assertEqual(event.reason, self.RECREATE_AUTO_REASON_LITERAL)
        # ⭐ 防循环的账本行：自动新建终态行恰一条，带被查的旧远端单号。
        note = JidanpuSyncEvent.objects.get(
            kind="member.no_order_recreated", target_ref=ORDER_NO
        )
        self.assertEqual(note.status, "skipped")
        self.assertEqual(note.reason, self.NO_ORDER_RECREATED_REASON_LITERAL)
        self.assertEqual(note.payload["member_binding_id"], self.binding.pk)
        self.assertEqual(note.payload["jidanpu_order_id"], JID)

    def test_a_manual_rethrow_without_a_withdraw_mark_now_recreates(self):
        """④ ⭐ D-0362 反转（原「仍拒抛」）：人工重抛但**从没退成过** ⇒ 人工路径直通新建。
        原口径（§11，历史）：「店里查无」对没退成过的单可能是第一页窗口外的假象（E-0004 的保守面）⇒ 人工也不放行。新口径（D-0362）：逐字查无 = 单条件终态事实，人工动作直通（人话落 AUTO 档那句——「撤回单人工重抛」只属于人工经典档）；⛔ 人工路径不写自动新建终态行。"""
        self._withdrawn_link(withdrawn=False)
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._launch_event(rearmed=True)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["throw_order"]["remote_order_id"], JID_2)
        self.assertIsNone(self._link_row().withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, self.RECREATE_AUTO_REASON_LITERAL)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="member.no_order_recreated", target_ref=ORDER_NO
            ).exists(),
            "人工路径不占自动新建上限（不写终态行）",
        )

    def test_a_manual_rethrow_with_the_card_still_present_keeps_the_existing_leg(self):
        """⑤ 缺「查无」（卡存在·已退单）⇒ 原样走既有档，⛔ 不重录。
        其余两条件都齐也不许越界：卡存在 = `_adopted_throw_verdict` 的天下
        （已退单 ⇒ 改回未分配再抛），「全新建卡」只属于查无那一格。"""
        self._withdrawn_link()
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        event = self._launch_event(rearmed=True)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "set_order_status",
             "throw_order", "fetch_order_by_key1"],
        )
        self.assertNotIn("add_order", up.names())
        self.assertEqual(dict(up.calls)["set_order_status"]["remote_order_id"], JID)
        self.assertEqual(self._link_row().jidanpu_order_id, JID, "旧卡还在 ⇒ 单号不换")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, "", "既有档的 done 行原因照旧为空（不放行人话）")

    def test_a_card_gone_without_either_fact_now_recreates(self):
        """⑥ ⭐ D-0362 反转（原 §11 变异钉，随反转失效）：非撤回单 + 自动重试 + 查无（两条事实都缺）⇒ **自动首遇**：放行新建 + 落终态行。
        原口径（§11，历史）：本用例是「删 `if rearmed and withdrawn:` ⇒ 必须红」的变异钉。D-0362 把查无升为单条件终态事实 ⇒ 判据删除成了**拍板的行为本身**，本用例反转为正向用例；防循环的新变异钉（删「上限已用」判据）迁往`tests/test_jidanpu_no_order_newcard.py`（先红后绿证据在其 docstring）。"""
        self._withdrawn_link(withdrawn=False)
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._launch_event()  # 无 rearmed_at
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertIn("throw_order", up.names())
        self.assertEqual(dict(up.calls)["throw_order"]["remote_order_id"], JID_2)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, self.RECREATE_AUTO_REASON_LITERAL)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="member.no_order_recreated", target_ref=ORDER_NO
            ).exists(),
            "自动首遇必须落账本行（否则上限永远用不掉、防循环失效）",
        )

    def test_a_race_adopted_card_already_in_hall_is_idempotent_not_a_crash(self):
        """⑦ 窄竞态：重录预读认领到**已挂厅**的卡 ⇒ 并入幂等腿收口（评审 S1）。
        现场（生产实锚）：prepare 按旧单号查无、重录的预读按来源单号却查到一张`isThrowOrder=Y` 的同来源号僵尸卡。目标态（挂在厅）已达成 ⇒ 幂等收口：补链路、⛔ 不重录、⛔ 不抛、⛔ 不响亮炸（修复前本用例走到分派块尾的`ValueError`，`consume_event` 直接抛、打断整轮 outbox）。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_exists=True,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO,
                               "isThrowOrder": "Y"}},
        )
        event = self._launch_event(rearmed=True)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        # 序列 = 前置回读（旧号查无）→ 取来源 → 预读认领；⛔ 零次录单、⛔ 零次抛单。
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "fetch_sources", "fetch_order_by_key1"],
        )
        link = self._link_row()
        self.assertEqual(link.jidanpu_order_id, JID_2, "链路对到认领到的那张卡")
        self.assertTrue(link.in_hall)
        self.assertIsNone(link.withdrawn_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        # ⑨：认领不是建卡，台账 ⛔ 不许落「全新建卡」那句。
        self.assertEqual(event.reason, "")

    def test_a_race_adopted_refunded_card_walks_the_existing_un_refund_leg(self):
        """⑧ 窄竞态：认领到**已退单**卡 ⇒ 照既有语义走「改回未分配再抛」腿。
        修复前本用例同样掉进块尾响亮炸；修复后对**认领到的那张卡**（新单号）先`set_order_status("未分配")` 再抛——判据与腿**全部**是既有那一份，recreate支里没有第二份。"""
        self._withdrawn_link()
        up = _Upstream(
            remote_exists=True, remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO,
                               "isThrowOrder": "N", "orderSta": LABEL_REFUNDED}},
        )
        event = self._launch_event(rearmed=True)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            up.names(),
            ["fetch_order_by_key1", "fetch_sources", "fetch_order_by_key1",
             "set_order_status", "throw_order", "fetch_order_by_key1"],
        )
        self.assertNotIn("add_order", up.names())
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID_2, "改的是认领到的那张卡")
        self.assertEqual(sent["status_label"], LABEL_UNASSIGNED)
        self.assertEqual(self._link_row().jidanpu_order_id, JID_2)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, "", "⑨：认领+改回未分配不是「全新建卡」")

    def test_a_rearmed_mark_older_than_the_withdraw_is_an_auto_first_encounter(self):
        """⑩ ⭐ D-0362 反转（原评审 S4 拒抛钉）：`rearmed_at` **早于** `withdrawn_at`的旧行 = 自动重试 ⇒ **自动首遇新建**（人话落 AUTO 档那句）。
        S4 的「人工 / 自动」**分档判据本身没变**（早标记仍正确地认成自动，`rearmed_manual` 为假——它不落「撤回单人工重抛」那句人话就是证据）；D-0362 变的只是自动档的处置：拒抛循环 → 上限内新建 + 落终态行。
        原口径（历史）：早标记被当成人工动作会放行建卡 = 系统自己把已撤回的单挂回对方大厅；如今自动档放行是**拍板行为**（对账本来就一直给这类单排launch，§11 现场实测 ~5 次/小时），上限 1 次 + 转人工行兜住误判面。"""
        link = self._withdrawn_link()
        withdrawn_at = JidanpuMemberOrderLink.objects.get(pk=link.pk).withdrawn_at
        up = _Upstream(
            remote_id=JID_2,
            lookup={ORDER_NO: {"orderId": JID_2, "orderIdView": ORDER_NO}},
        )
        event = self._launch_event(rearmed_at=withdrawn_at - timedelta(minutes=5))
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertIn("throw_order", up.names())
        self.assertEqual(self._link_row().jidanpu_order_id, JID_2)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        # ⭐ S4 分档照旧生效：早标记**不算人工** ⇒ 人话落 AUTO 档（⛔ 不是「撤回单人工重抛」那句）。
        self.assertEqual(event.reason, self.RECREATE_AUTO_REASON_LITERAL)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="member.no_order_recreated", target_ref=ORDER_NO
            ).exists()
        )


# ══ ③ 地址级去重（裁定 3）══════════════════════════════════════════════════


class AddressLevelDedupeTests(_Base):
    def test_a_same_store_account_does_not_adopt_a_copy_linked_to_another_order(self):
        old_order = Order.objects.create(
            game=self.game, order_no="20261120000000000998",
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("31.00"),
        )
        old_binding = self._other_binding(enabled=False)
        old_link = self._link(binding=old_binding, order=old_order, in_hall=False)
        event = self._event(jidanpu_member.KIND_LAUNCH)
        upstream = _Upstream(
            lookup={
                ORDER_NO: {"orderId": JID, "orderIdView": ORDER_NO, "orderSta": "未分配", "isThrowOrder": "N"},
                JID: {"orderId": JID, "isThrowOrder": "Y"},
            },
            remote_exists=True,
        )

        with _stub(upstream):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "skipped")
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(order=self.order).exists())
        old_link.refresh_from_db()
        self.assertEqual(old_link.order_id, old_order.pk)
        self.assertNotIn("throw_order", upstream.names())
        self.assertNotIn("add_order", upstream.names())

    def test_a_disabled_binding_on_the_same_address_blocks_a_second_recording(self):
        """⭐ 同地址下**已停用**的绑定已有链路 ⇒ 本次 `skipped`，⛔ 不录第二张。"""
        other = self._other_binding(enabled=False)  # 同地址、已停用
        self._link(binding=other, remote_id="JDP-别人的-1")
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertNotIn("add_order", up.names())
        self.assertNotIn("fetch_sources", up.names())
        self.assertFalse(
            JidanpuMemberOrderLink.objects.filter(binding=self.binding).exists()
        )
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn("不重复录", event.reason)

    def test_a_binding_on_another_address_does_not_block_it(self):
        """反面：**别的地址**的链路不算（去重判据是地址级，⛔ 不是「有链路就跳过」）。"""
        other = self._other_binding(base_url=BASE_URL_B, enabled=False)
        self._link(binding=other, remote_id="JDP-别家地址-1")
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertTrue(
            JidanpuMemberOrderLink.objects.filter(binding=self.binding).exists()
        )

    def test_assign_also_respects_the_address_level_dedupe(self):
        """去重发生在**任何一次录单之前** ⇒ 只录单那一支同样受它管。"""
        self._link(binding=self._other_binding(enabled=False), remote_id="JDP-别人的-1")
        event = self._event(jidanpu_member.KIND_ASSIGN)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertNotIn("add_order", up.names())

    def test_the_unique_constraint_race_is_treated_as_already_done(self):
        """spec §5.4：链路行唯一约束兜底 —— `IntegrityError` 当「已做过」，⛔ 不当失败。⚠️ 模拟的是**真并发**：查重那一步还没看到链路（`_link_for` 头两次回 `None`），另一个进程抢先落了行 ⇒ `create` 撞唯一约束 ⇒ 重查拿到它、当已做过。
        ⚠️链路在**录单成功那一刻**就落（见 `_ensure_remote_order`）⇒ 撞约束发生在那一次，而抛单之后还会再查一次链路 ⇒ `_link_for` 一共被调 **4** 次（名单写死是故意的：少写一个会让桩以 `StopIteration` 红，那正是「调用序列变了」的信号）。"""
        raced = JidanpuMemberOrderLink(
            binding=self.binding, order=self.order, jidanpu_order_id=JID, in_hall=False,
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with (
            _stub(up),
            mock.patch.object(
                jidanpu_member, "_link_for", side_effect=[None, None, raced, raced]
            ),
            mock.patch.object(
                JidanpuMemberOrderLink.objects, "create",
                side_effect=IntegrityError("uniq_jdp_member_link_order"),
            ),
        ):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")


# ══ ①c **A 族认领撞键 → 终态跳过**（E-0315）═════════════
# 生产形状：同店一张老单（多为已退款）占着 `(binding, jidanpu_order_id)` 链行；同来源的**新单**认领建链必撞唯一约束，旧实现「重查仍查不到 ⇒ raise」让每次消费都按**未预期异常**烧满 5 次重试、终态后又被对账重排（51 单 × 10,660 条 failed 的循环）。
# 新口径：链路被老单占用 = **认得出的人话事实** ⇒ 终态 `skipped`（零上游写、不烧额度）；良性并发那一支（重查拿得到）**一字不改**（上方 `test_the_unique_constraint_race_is_...`）。数据面处置 = 迁链命令 `migrate_member_link_to_survivor`（D-0370）。

OLD_ORDER_NO = "20261103000000001001"


class LinkOccupiedStructuralConflictTests(_Base):
    def setUp(self):
        super().setUp()
        # 老单占链（A 族现场）：REFUNDED 老单持有 (binding, JID) 链行。
        self.old_order = Order.objects.create(
            game=self.game, order_no=OLD_ORDER_NO, status=OrderStatus.REFUNDED,
            hall_price="12.34", total_price="50.00",
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.old_order,
            jidanpu_order_id=JID, in_hall=True,
        )

    def _old_link(self):
        return JidanpuMemberOrderLink.objects.get(
            binding=self.binding, order=self.old_order
        )

    def _adopted(self, **row):
        """认领那一支的桩（照 `LaunchConsumeTests._adopted` 同形，本类自备一份）。"""
        base = {"orderId": JID, "orderIdView": ORDER_NO, "orderSta": "未分配",
                "isThrowOrder": "N"}
        base.update(row)
        return _Upstream(
            lookup={ORDER_NO: base, JID: {"orderId": JID, "isThrowOrder": "Y"}},
            remote_exists=True,
        )

    def test_launch_claim_that_hits_the_occupied_link_ends_as_skipped(self):
        """认领建链撞「挂在别的单上」的唯一约束 ⇒ 终态 skipped + 人话，⛔ 不烧重试。判据：① 消费结局与台账行都是 `skipped`（不是 failed、不是响亮抛）；
        ② 原因点名「链路被同店另一张单占用 + 人工核对」；③ 零上游写（认领只读；撞键后⛔ 不许再 add/throw）；④ 老单的链行一个字没动、新单没有链行；⑤ 零额度消耗（`attempts` 恒 0）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = self._adopted()
        # ⚠️ 用例跑在测试框架的 atomic 块里：真约束炸会把事务打坏 ⇒ 桩只在 `create`
        #    那一步注入约束失败（⭐ 与「良性并发」竞态用例同一形状），重查走**真库**
        #    （新单名下确无链行）⇒ 结构性冲突那一支被如实走到。
        with (
            _stub(up),
            mock.patch.object(
                JidanpuMemberOrderLink.objects, "create",
                side_effect=IntegrityError("uniq_jdp_member_link_remote"),
            ),
        ):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "skipped"
            )
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn(jidanpu_member.LINK_OCCUPIED_REASON, event.reason)
        self.assertEqual(event.attempts, 0)
        self.assertNotIn("add_order", up.names())
        self.assertNotIn("throw_order", up.names())
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(binding=self.binding).count(), 1,
            "只有老单那一条链行；新单 ⛔ 没有链行",
        )
        self.assertEqual(self._old_link().jidanpu_order_id, JID)

    def test_assign_that_hits_the_occupied_link_ends_as_skipped(self):
        """`member.assign`（只录单那一档）撞同一条占用 ⇒ 同样终态 skipped。"""
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=self.user)
        event = self._event(jidanpu_member.KIND_ASSIGN)
        up = self._adopted()
        with (
            _stub(up),
            mock.patch.object(
                JidanpuMemberOrderLink.objects, "create",
                side_effect=IntegrityError("uniq_jdp_member_link_remote"),
            ),
        ):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "skipped"
            )
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn(jidanpu_member.LINK_OCCUPIED_REASON, event.reason)
        self.assertEqual(
            JidanpuMemberOrderLink.objects.filter(binding=self.binding).count(), 1
        )

    def test_ensure_link_returns_none_pair_on_structural_conflict(self):
        """`_ensure_link` 本体：撞约束 + 重查仍 None ⇒ 回 `(None, False)`（⛔ 不再 raise）。
        阳性对照（判别力）：把 `(None, False)` 改回 `raise` ⇒ 本用例红。
        良性并发那一支（重查拿得到）由上方唯一约束竞态用例钉着，一字不改。"""
        # ⚠️ 桩形状照「良性并发」竞态用例：`create` 注入约束失败、`_link_for` 两次都回
        #    None（真库事务不被打坏）。预查一次 + 撞键后重查一次 = 恰 2 次。
        with (
            mock.patch.object(
                jidanpu_member, "_link_for", side_effect=[None, None]
            ),
            mock.patch.object(
                JidanpuMemberOrderLink.objects, "create",
                side_effect=IntegrityError("uniq_jdp_member_link_remote"),
            ),
        ):
            got = jidanpu_member._ensure_link(
                self.binding, self.order, remote_id="remote-occupied-9",
                in_hall=False, now=timezone.now(),
            )
        self.assertIsNone(got[0])
        self.assertFalse(got[1])


# ══ ②b **有效来源单号**（业务方）：唯一出处 + 四个调用点 ═════════════
# 业务方原话要点：录到鸡蛋铺时用的是我方**内置单号**（`Order.order_no`），而要求的是**优先用我方的「来源单号」`Order.source_order_no`**、**它为空白才回落 `order_no`**（双方的**内置**系统单号互不做要求，来源单号要尽量一致）。
# ⚠️ 这个判据**只有一个出处**：`jidanpu_member.effective_source_order_no`。四处调用点（录单表单 + 顶层参数 / 录单后回读 / 认领预读 / 日志快路匹配）都走它——用例逐处钉「用的就是同一个值」，⛔ 不许各写一份。

#: 合成来源单号（形状贴近真实：上游给的第三方单号）。
SOURCE_ORDER_NO = "SYNTH-SRC-2026-000001"
#: 128 = `Order.source_order_no` 的列宽；上游 `souresOrderID` 的长度上限**没有**记录。
LONG_SOURCE_ORDER_NO = "S" * 128


class EffectiveSourceOrderNoTests(_Base):
    """唯一出处函数本身：来源单号优先、空白回落内置单号。"""

    def test_it_prefers_our_source_order_no(self):
        Order.objects.filter(pk=self.order.pk).update(source_order_no=SOURCE_ORDER_NO)
        self.order.refresh_from_db()
        self.assertEqual(
            jidanpu_member.effective_source_order_no(self.order), SOURCE_ORDER_NO
        )

    def test_blank_or_whitespace_falls_back_to_the_builtin_order_no(self):
        """空串 / 纯空白**一律当「没有」**（E-0021 同族：⛔ 不用真值判）。"""
        for value in ("", "   ", "\t\n"):
            with self.subTest(value=value):
                Order.objects.filter(pk=self.order.pk).update(source_order_no=value)
                self.order.refresh_from_db()
                self.assertEqual(
                    jidanpu_member.effective_source_order_no(self.order), ORDER_NO
                )

    def test_the_source_order_no_is_not_stripped_of_inner_content(self):
        """只在**两端**去空白：中间的内容一个字节都不动（它是上游那张单的标识）。"""
        Order.objects.filter(pk=self.order.pk).update(
            source_order_no=f"  {SOURCE_ORDER_NO}  "
        )
        self.order.refresh_from_db()
        self.assertEqual(
            jidanpu_member.effective_source_order_no(self.order), SOURCE_ORDER_NO
        )


class RecordSourceOrderNoTests(_Base):
    """录单 / 回读 / 认领预读三处都用**有效来源单号**，⛔ 不再用 `order_no`。"""

    def _order_with_source(self, value):
        Order.objects.filter(pk=self.order.pk).update(source_order_no=value)
        self.order.refresh_from_db()

    def _launch(self, *, source_order_no):
        """跑一次 `member.launch`，回桩（桩侧的「对方来源单号」= 有效来源单号）。"""
        self._order_with_source(source_order_no)
        # 测试侧独立算一遍「有效来源单号」（⛔ 不调被测函数，免得判据自指）：
        # 非空白用来源单号，否则回落到这张单的内置单号。
        effective = source_order_no.strip() if source_order_no.strip() else ORDER_NO
        up = _Upstream(
            source_order_no=effective,
            lookup={
                effective: {
                    "orderId": JID, "orderIdView": effective, "isThrowOrder": "N",
                },
                JID: {"orderId": JID, "isThrowOrder": "Y"},
            },
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)
        return up, outcome

    def test_the_record_payload_carries_our_source_order_no(self):
        """录单：**表单**的 `souresOrderID` 与 `add_order` 的顶层参数都是来源单号。"""
        up, outcome = self._launch(source_order_no=SOURCE_ORDER_NO)
        self.assertEqual(outcome, "done", up.calls)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(sent["form_payload"]["souresOrderID"], SOURCE_ORDER_NO)
        self.assertEqual(sent["source_order_no"], SOURCE_ORDER_NO)
        # 反向控制：内置单号**不再**出现在载荷里（否则这条判据对旧行为也成立）
        self.assertNotEqual(SOURCE_ORDER_NO, ORDER_NO)
        self.assertNotIn(ORDER_NO, json.dumps(sent["form_payload"], ensure_ascii=False))

    def test_blank_source_order_no_still_records_the_builtin_order_no(self):
        """来源单号为空 ⇒ 回落内置单号（老单/没填来源单号的单照旧能录）。"""
        up, outcome = self._launch(source_order_no="   ")
        self.assertEqual(outcome, "done", up.calls)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(sent["form_payload"]["souresOrderID"], ORDER_NO)
        self.assertEqual(sent["source_order_no"], ORDER_NO)

    def test_the_readback_after_recording_queries_the_source_order_no(self):
        """录单**之后**的回读（`orderIdView` 那一跳）按来源单号查，⛔ 不是内置单号。"""
        up, _outcome = self._launch(source_order_no=SOURCE_ORDER_NO)
        # ⚠️ 抛单之后还有一次按 `orderId` 的确认回读 ⇒ 只取 `orderIdView` 那一路
        #    （「录单后的取号」这一步），否则会拿到抛单确认那次。
        source_reads = [
            kw for name, kw in up.calls
            if name == "fetch_order_by_key1"
            and kw["expect_field"] == jidanpu.OWN_SOURCE_VALUE_FIELD
        ]
        self.assertEqual(len(source_reads), 2, up.calls)  # 认领预读 + 录单后回读
        for read in source_reads:
            self.assertEqual(read["key"], SOURCE_ORDER_NO)
            self.assertEqual(read["expect_value"], SOURCE_ORDER_NO)

    def test_the_adoption_pre_read_queries_the_source_order_no(self):
        """认领预读（录单**之前**问「对方是不是已经有这张单」）也按来源单号查。"""
        self._order_with_source(SOURCE_ORDER_NO)
        up = _Upstream(
            source_order_no=SOURCE_ORDER_NO,
            remote_exists=True,
            lookup={
                SOURCE_ORDER_NO: {
                    "orderId": JID, "orderIdView": SOURCE_ORDER_NO,
                    "orderSta": "未分配", "isThrowOrder": "N",
                },
                JID: {"orderId": JID, "isThrowOrder": "Y"},
            },
        )
        event = self._event(jidanpu_member.KIND_LAUNCH)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        pre = [kw for name, kw in up.calls if name == "fetch_order_by_key1"][0]
        self.assertEqual(pre["key"], SOURCE_ORDER_NO)
        self.assertEqual(pre["expect_value"], SOURCE_ORDER_NO)
        # 认领到 ⇒ ⛔ 不重录（否则这条判据对「录了第二张」也成立）
        self.assertNotIn("add_order", up.names())

    def test_a_long_source_order_no_is_sent_verbatim(self):
        """超长来源单号**原样发**，⛔ 不截断（截断会让回读 / 日志匹配全部对不上）。
        ⚠️ 上游 `souresID` 的长度上限**没有**记录（E-0007：新端点的参数必须实测）⇒
        本线**不自造**一个上限；真被拒时由录单失败那句人话（含上游 `msg`）响亮报出来。"""
        up, outcome = self._launch(source_order_no=LONG_SOURCE_ORDER_NO)
        self.assertEqual(outcome, "done", up.calls)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(len(LONG_SOURCE_ORDER_NO), 128)
        self.assertEqual(sent["source_order_no"], LONG_SOURCE_ORDER_NO)
        self.assertEqual(
            sent["form_payload"]["souresOrderID"], LONG_SOURCE_ORDER_NO
        )


# ══ ③b `inputOrderKefu` = 下游「这个账号自己的员工 uid」（业务方选 C）════
# 业务方原话：录到鸡蛋铺时**销售客服没填，最终是个空的订单**。字段是表单 JSON 里的`inputOrderKefu`，值是**下家店铺的员工 uid**（⛔ 不是姓名文本）⇒ 业务方裁定「填**绑定账号自己**在下家的 uid」（= 下家录单页的默认行为：`isSelectCurrentUser=Y` 时那一行带 `selected`）。
# ⚠️ 模板里的 `'100'` 是**合成占位值**（真实抓包是下载店铺自己的员工 uid）⇒ ⛔ 不再发出去。⚠️ uid **取一次就缓存在绑定行**（`input_order_kefu`）：⛔ 不许每单多打一次上游列表。


class OwnStaffUidPrimitiveTests(SimpleTestCase):
    """`jidanpu.own_staff_uid` 的**读码判据**：从客服名单里挑出当前登录账号那一行。
    依据：录单页 JS `addorder.js` 的 `getCustomerService()` 调`GET /UserAjax/GetAllUserByGroupName?groupType=20&isSelectCurrentUser=Y&…`，回包每一行是 `{name, value}`（`value` = 员工 uid），**当前登录账号那一行带 `selected`**。"""

    def _client(self, rows):
        client = mock.Mock()
        client.call.return_value = rows
        return client

    def test_it_calls_the_registered_path_with_the_documented_params(self):
        client = self._client([{"name": "甲", "value": "7", "selected": "selected"}])
        jidanpu.fetch_kefu_candidates(client)
        client.call.assert_called_once_with(
            "GET", "/UserAjax/GetAllUserByGroupName",
            query={"groupType": 20, "isSelectCurrentUser": "Y",
                   "isOnlySelectSelf": "N", "dy": 1},
        )

    def test_the_unique_selected_row_gives_the_uid(self):
        client = self._client([
            {"name": "甲", "value": "7", "selected": ""},
            {"name": "乙", "value": "9", "selected": "selected"},
        ])
        self.assertEqual(jidanpu.own_staff_uid(client), "9")

    def test_no_selected_row_is_an_empty_answer(self):
        client = self._client([{"name": "甲", "value": "7", "selected": ""}])
        self.assertEqual(jidanpu.own_staff_uid(client), "")

    def test_two_selected_rows_are_ambiguous_and_must_not_be_guessed(self):
        """⚠️ 两行都带 `selected` ⇒ **认不出来**（⛔ 不许挑一个：填错人比不填更糟）。"""
        client = self._client([
            {"name": "甲", "value": "7", "selected": "selected"},
            {"name": "乙", "value": "9", "selected": "selected"},
        ])
        self.assertEqual(jidanpu.own_staff_uid(client), "")

    def test_a_non_digit_value_is_not_an_uid(self):
        """`value` 不是数值 ⇒ ⛔ 不许当 uid 填进对方的字段（形状防御，E-0005 族）。"""
        client = self._client([{"name": "甲", "value": "系统管理员", "selected": "selected"}])
        self.assertEqual(jidanpu.own_staff_uid(client), "")

    def test_a_non_list_payload_is_loud(self):
        client = self._client({"rows": []})
        with self.assertRaises(ApiError):
            jidanpu.fetch_kefu_candidates(client)


class InputOrderKefuTests(_Base):
    """录单载荷里的 `inputOrderKefu`：逐字等于缓存的那个 uid；取不到就**不填 + 台账**。"""

    def _launch(self, *, order=None, uid=DOWNSTREAM_STAFF_UID, error=None, kind="launch",
                remote_id=JID):
        target = order or self.order
        effective = ORDER_NO if target is self.order else target.order_no
        up = _Upstream(
            source_order_no=effective,
            kefu_uid=uid,
            kefu_error=error,
            lookup={
                effective: {"orderId": remote_id, "orderIdView": effective,
                            "isThrowOrder": "N"},
                remote_id: {"orderId": remote_id, "isThrowOrder": "Y"},
            },
        )
        event = jidanpu_member.queue_member_event(
            target, self.binding,
            kind=(jidanpu_member.KIND_LAUNCH if kind == "launch" else jidanpu_member.KIND_ASSIGN),
        )
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)
        return up, event, outcome

    def _kefu_notes(self, order):
        return JidanpuSyncEvent.objects.filter(
            direction="in", target_ref=order.order_no,
            reason__contains=KEFU_UNRESOLVED_HINT,
        )

    def test_the_record_payload_carries_the_downstream_staff_uid(self):
        """判据①：表单里的 `inputOrderKefu` **逐字等于**那个 uid。"""
        order = self.order
        order.source_order_no = ""
        up, _event, outcome = self._launch(uid="70007")
        self.assertEqual(outcome, "done", up.calls)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(sent["form_payload"]["inputOrderKefu"], "70007")

    def test_the_template_placeholder_is_never_sent(self):
        """判据②：模板那个合成占位值 `'100'` **不再出现在任何录单载荷里**。"""
        _up, _event, _outcome = self._launch(uid=DOWNSTREAM_STAFF_UID)
        self.assertNotEqual(DOWNSTREAM_STAFF_UID, TEMPLATE_KEFU_PLACEHOLDER)
        sent = dict(_up.calls)["add_order"]
        self.assertEqual(sent["form_payload"]["inputOrderKefu"], DOWNSTREAM_STAFF_UID)
        self.assertNotIn(
            TEMPLATE_KEFU_PLACEHOLDER, json.dumps(sent["form_payload"], ensure_ascii=False)
        )

    def test_the_assign_path_fills_it_too(self):
        """只录单那一档（`member.assign`）走的是同一份表单 ⇒ 也必须填上。"""
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=self.user)
        up, _event, outcome = self._launch(uid="70009", kind="assign")
        self.assertEqual(outcome, "done", up.calls)
        sent = dict(up.calls)["add_order"]
        self.assertEqual(sent["form_payload"]["inputOrderKefu"], "70009")

    def test_the_uid_is_fetched_once_and_cached_on_the_binding_row(self):
        """判据④：**不是每单一次** —— 第二张单直接用绑定行上缓存的那个 uid。"""
        first_up, _e1, first_outcome = self._launch(uid=DOWNSTREAM_STAFF_UID)
        self.assertEqual(first_outcome, "done", first_up.calls)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.input_order_kefu, DOWNSTREAM_STAFF_UID)

        second = Order.objects.create(
            game=self.game, order_no=ORDER_NO_2, status=OrderStatus.WAITING_GRAB,
            hall_price="9.00", total_price="50.00", project_text="合成项目甲",
        )
        second_up, _e2, second_outcome = self._launch(order=second, remote_id=JID_2)
        self.assertEqual(second_outcome, "done", second_up.calls)
        self.assertEqual(
            second_up.names().count("own_staff_uid"), 0,
            "第二张单不该再问一次上游（缓存没接上）",
        )
        self.assertEqual(
            dict(second_up.calls)["add_order"]["form_payload"]["inputOrderKefu"],
            DOWNSTREAM_STAFF_UID,
        )
        self.assertEqual(first_up.names().count("own_staff_uid"), 1)

    def test_a_cached_uid_is_used_without_asking_upstream(self):
        """绑定行上已经有 uid ⇒ 一次都不问上游（这就是「缓存」的判据本身）。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            input_order_kefu="70011"
        )
        self.binding.refresh_from_db()
        up, _event, outcome = self._launch()
        self.assertEqual(outcome, "done", up.calls)
        self.assertEqual(up.names().count("own_staff_uid"), 0)
        self.assertEqual(
            dict(up.calls)["add_order"]["form_payload"]["inputOrderKefu"], "70011"
        )

    def test_an_unresolvable_uid_is_left_unfilled_and_noted(self):
        """判据③：认不出当前账号 ⇒ **不填那个键**（⛔ 不放占位值）+ 一条人话台账，单照录。"""
        up, event, outcome = self._launch(uid="")
        self.assertEqual(outcome, "done", up.calls)
        sent = dict(up.calls)["add_order"]
        self.assertNotIn("inputOrderKefu", sent["form_payload"])
        notes = self._kefu_notes(self.order)
        self.assertEqual(notes.count(), 1, list(notes.values_list("reason", flat=True)))
        self.assertEqual(notes.get().status, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_an_upstream_error_while_asking_for_the_uid_is_also_unfilled_and_noted(self):
        """接口挂了 / 形状变了 ⇒ 与「认不出来」同档：不填 + 人话，⛔ 不让整单录不下去。"""
        up, _event, outcome = self._launch(
            uid="", error=ApiError("客服名单读不到", code=0)
        )
        self.assertEqual(outcome, "done", up.calls)
        self.assertNotIn("inputOrderKefu", dict(up.calls)["add_order"]["form_payload"])
        self.assertEqual(self._kefu_notes(self.order).count(), 1)

    def test_a_failed_lookup_does_not_persist_anything(self):
        """认不出来 ⇒ ⛔ 不许把空串 / 猜的值写进绑定行（下一次还要再试）。"""
        self._launch(uid="")
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.input_order_kefu, "")


# ══ ④ member.assign：只录单（裁定 1）══════════════════════════════════════


class AssignConsumeTests(_Base):
    def setUp(self):
        super().setUp()
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        Performance.objects.create(order=self.order, emp_type=EmpType.MAIN, user=self.user)

    def test_assign_records_only_and_never_throws(self):
        """⭐ 裁定 1：`to_record` 形态 = 已指派给他但本地无链路 ⇒ 只录单、⛔ 不抛单。"""
        event = self._event(jidanpu_member.KIND_ASSIGN)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("add_order", up.names())
        self.assertNotIn("throw_order", up.names())
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)
        self.assertIsNone(link.listed_at)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_assign_records_the_out_price_and_never_the_customer_price(self):
        """⭐ 业务方 2026-09-27：**被指派过去的单**录进对方时，接单价 = 我方发单价（`hall_price`）。
        与抛单那一档**无关**（那条按百分比打折）：指派是「只录单」，接单价就是发单价本身。
        ⛔ 客户价（`total_price` = 我们的底价）一个数都不许进载荷——泄露给另一个租户收不回来。"""
        # 故意给一个**打折百分比**：指派那一档**不受它影响**（打折只作用于抛进对方大厅的价）。
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(throw_price_percent=50)
        event = self._event(jidanpu_member.KIND_ASSIGN)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["add_order"]
        # 两句一起才有判别力：只钉「等于 hall_price」时，把两个价对调也能绿。
        self.assertEqual(sent["form_payload"]["price"], "12.34")
        self.assertNotIn("50.00", str(sent["form_payload"]))

    def test_assign_without_an_out_price_does_not_record_and_says_why(self):
        """⭐ 本单没有发单价 ⇒ **不录单**（⛔ 不写 `"0"`、⛔ 更不回落客户价），并说清为什么。
        口径来自 D-0162：**下家的接单价 = 我方的发单价**。没有发单价时写什么都是错的——写 `"0"` 等于告诉下家「这单不要钱」，写客户价是**把我方底价泄露给另一个租户**。填上发单价之后由 180 秒的对账（`to_record` 那一档）补录，⛔ 不会静默丢单。"""
        Order.objects.filter(pk=self.order.pk).update(hall_price=None)
        event = self._event(jidanpu_member.KIND_ASSIGN)
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.reason, jidanpu_member.ASSIGN_NO_PRICE_REASON)
        self.assertEqual(JidanpuMemberOrderLink.objects.count(), 0)

    def test_assign_with_an_existing_hall_link_keeps_the_link_until_unlist(self):
        self._link(in_hall=True)
        event = self._event(jidanpu_member.KIND_ASSIGN)
        row = {"orderId": JID, "orderIdView": ORDER_NO, "orderSta": "未分配", "isThrowOrder": "Y"}
        with mock.patch.object(jidanpu, "find_order_for_repair", return_value=row) as lookup:
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(lookup.call_args.kwargs["remote_order_id"], JID)
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall)
        self.assertTrue(JidanpuSyncEvent.objects.filter(kind="member.unlist").exists())


class AssignRepairTests(_Base):
    def setUp(self):
        super().setUp()
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.source_order_no = "SYNTH-REPAIR-CURRENT"
        self.order.save(update_fields=["status", "source_order_no"])
        self.taker = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.binding.user,
        )

    def _row(self, status, *, source=None):
        return {
            "orderId": JID, "orderIdView": source or self.order.source_order_no,
            "orderSta": status, "isThrowOrder": "N",
        }

    def _consume(self, event):
        result = jidanpu_member.consume_event(event, client=CLIENT)
        event.refresh_from_db()
        self.assertEqual(event.status, result)
        return result

    def test_missing_order_is_replaced_in_the_list_using_the_current_source_no(self):
        old = self._link()
        event = self._event(jidanpu_member.KIND_ASSIGN, payload={"jidanpu_order_id": JID})
        up = _Upstream(source_order_no=self.order.source_order_no, lookup={
            self.order.source_order_no: {
                "orderId": JID_2, "orderIdView": self.order.source_order_no, "oid": 900001,
            }
        })
        with (
            _stub(up),
            mock.patch.object(jidanpu, "find_order_for_repair", side_effect=[
                None, None, None, None, None,
                {"orderId": JID_2, "orderIdView": self.order.source_order_no, "oid": 900001},
            ]) as lookup,
        ):
            self.assertEqual(self._consume(event), "done")
        self.assertEqual([call.kwargs["recycle"] for call in lookup.call_args_list],
                         [False, True, False, False, True, False])
        self.assertEqual(lookup.call_args.kwargs["remote_oid"], 900001)
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(pk=old.pk).exists())
        link = JidanpuMemberOrderLink.objects.get(order=self.order, binding=self.binding)
        self.assertEqual(link.jidanpu_order_id, JID_2)
        self.assertFalse(link.in_hall)
        self.assertEqual(up.names().count("add_order"), 1)
        self.assertNotIn("throw_order", up.names())
        self.assertEqual(dict(up.calls)["add_order"]["source_order_no"], self.order.source_order_no)

    def test_same_source_on_an_unlinked_remote_order_is_not_adopted_or_overwritten(self):
        old = self._link()
        event = self._event(jidanpu_member.KIND_ASSIGN)
        with (
            mock.patch.object(jidanpu, "find_order_for_repair", side_effect=[
                None, None, None, {"orderId": JID_2, "orderIdView": self.order.source_order_no},
            ]),
            mock.patch.object(jidanpu, "add_order") as create,
            mock.patch.object(jidanpu, "set_order_status") as status,
        ):
            self.assertEqual(self._consume(event), "failed")
        create.assert_not_called()
        status.assert_not_called()
        old.refresh_from_db()
        self.assertEqual(old.jidanpu_order_id, JID)

    def test_read_error_keeps_existing_link_and_never_creates(self):
        old = self._link()
        event = self._event(jidanpu_member.KIND_ASSIGN)
        with (
            mock.patch.object(jidanpu, "find_order_for_repair", side_effect=ApiError("查询失败")),
            mock.patch.object(jidanpu, "add_order") as create,
        ):
            self.assertEqual(self._consume(event), "failed")
        create.assert_not_called()
        self.assertTrue(JidanpuMemberOrderLink.objects.filter(pk=old.pk).exists())

    def test_refunded_order_is_reused_and_link_becomes_live_again(self):
        old = self._link()
        old.withdrawn_at = timezone.now()
        old.save(update_fields=["withdrawn_at"])
        event = self._event(jidanpu_member.KIND_ASSIGN)
        with (
            mock.patch.object(jidanpu, "find_order_for_repair", side_effect=[
                self._row("已退单"), self._row("未分配"),
            ]),
            mock.patch.object(jidanpu, "set_order_status") as change,
            mock.patch.object(jidanpu, "add_order") as create,
        ):
            self.assertEqual(self._consume(event), "done")
        change.assert_called_once_with(CLIENT, remote_order_id=JID, status_label="未分配")
        create.assert_not_called()
        old.refresh_from_db()
        self.assertFalse(old.in_hall)
        self.assertIsNone(old.withdrawn_at)

    def test_recycle_restore_survives_lost_response_and_finishes_original_order(self):
        old = self._link()
        event = self._event(jidanpu_member.KIND_ASSIGN)
        with (
            mock.patch.object(jidanpu, "find_order_for_repair", side_effect=[
                None, self._row("问题单"), self._row("问题单"), self._row("未分配"),
            ]),
            mock.patch.object(jidanpu, "restore_recycled_order", side_effect=ApiError("回包丢失")) as restore,
            mock.patch.object(jidanpu, "set_order_status") as change,
            mock.patch.object(jidanpu, "add_order") as create,
        ):
            self.assertEqual(self._consume(event), "failed")
            self.assertEqual(event.payload["assign_restoring"]["remote_id"], JID)
            self.assertEqual(self._consume(event), "done")
        restore.assert_called_once_with(CLIENT, remote_order_id=JID)
        change.assert_called_once_with(CLIENT, remote_order_id=JID, status_label="未分配")
        create.assert_not_called()
        old.refresh_from_db()
        self.assertEqual(old.jidanpu_order_id, JID)

    def test_old_assignment_without_a_link_does_not_record_after_reassignment(self):
        event = self._event(jidanpu_member.KIND_ASSIGN)
        other = self._other_binding(base_url=BASE_URL_B, enabled=True)
        self.taker.user = other.user
        self.taker.save(update_fields=["user"])
        up = _Upstream(
            lookup={self.order.source_order_no: self._row("未分配")},
            source_order_no=self.order.source_order_no,
        )
        with _stub(up):
            result = self._consume(event)
        self.assertEqual(result, "skipped")
        self.assertNotIn("add_order", up.names())
        self.assertFalse(JidanpuMemberOrderLink.objects.filter(binding=self.binding, order=self.order).exists())

    def test_stale_assignment_cannot_reopen_an_order_after_the_taker_changed(self):
        self._link()
        event = self._event(jidanpu_member.KIND_ASSIGN)
        other = self._other_binding(base_url=BASE_URL_B, enabled=True)
        self.taker.user = other.user
        self.taker.save(update_fields=["user"])
        with (
            mock.patch.object(jidanpu, "find_order_for_repair", return_value=self._row("已退单")),
            mock.patch.object(jidanpu, "set_order_status") as change,
        ):
            self.assertEqual(self._consume(event), "skipped")
        change.assert_not_called()

    def test_source_number_change_confirms_new_search_after_partial_success(self):
        self._link(in_hall=False)
        self.assertEqual(jidanpu_member.queue_source_order_no_update(self.order), 1)
        event = JidanpuSyncEvent.objects.get(kind="member.source_order_no")
        with (
            mock.patch.object(jidanpu, "find_order_for_repair", side_effect=[
                self._row("代练中", source="SYNTH-REPAIR-OLD"), None,
                self._row("代练中"), self._row("代练中"),
            ]) as lookup,
            mock.patch.object(jidanpu, "change_source_order_no") as change,
        ):
            self.assertEqual(self._consume(event), "failed")
            self.assertEqual(self._consume(event), "done")
        change.assert_called_once_with(CLIENT, remote_order_id=JID, source_order_no=self.order.source_order_no)
        self.assertEqual(
            [call.kwargs["source_order_no"] for call in lookup.call_args_list],
            [None, self.order.source_order_no, None, self.order.source_order_no],
        )

    def test_source_number_change_does_not_contact_a_withdrawn_copy(self):
        link = self._link(in_hall=False)
        self.assertEqual(jidanpu_member.queue_source_order_no_update(self.order), 1)
        event = JidanpuSyncEvent.objects.get(kind="member.source_order_no")
        link.withdrawn_at = timezone.now()
        link.save(update_fields=["withdrawn_at"])

        with (
            mock.patch.object(jidanpu, "find_order_for_repair", return_value=self._row("已退单")) as lookup,
            mock.patch.object(jidanpu, "change_source_order_no") as change,
        ):
            self.assertEqual(self._consume(event), "skipped")

        lookup.assert_not_called()
        change.assert_not_called()

    def test_repush_cannot_swallow_a_real_assignment(self):
        repush = self._event(jidanpu_member.KIND_ASSIGN, payload={"repush": True})
        event = self._event(jidanpu_member.KIND_ASSIGN)
        self.assertNotEqual(repush.pk, event.pk)
        with mock.patch.object(jidanpu, "add_order") as create:
            self.assertEqual(self._consume(repush), "skipped")
        create.assert_not_called()


# ══ ⑤ member.withdraw：退单 + 链路降级 ═════════════════════════════════════


class WithdrawConsumeTests(_Base):
    def test_withdraw_refunds_with_the_existing_reason_and_downgrades_the_link(self):
        """退单复用既有原语与**逐字**原因；成功后链路 `in_hall=False`。"""
        self._link(in_hall=True)
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        up = _Upstream(lookup={JID: {"orderId": JID, "orderSta": LABEL_REFUNDED}})
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_REFUNDED)
        self.assertEqual(sent["remark"], WITHDRAW_REMARK_LITERAL)
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_two_other_downstreams_get_one_audit_after_last_confirmed_withdraw(self):
        other = self._other_binding(base_url="https://other.example.test", enabled=True)
        self._link()
        self._link(binding=other)
        self.order.source_order_no = "TEST-SOURCE-WITHDRAW"
        self.order.save(update_fields=["source_order_no"])
        self.assertEqual(jidanpu_member.queue_withdraw(self.order, summarize=True), 2)
        events = list(JidanpuSyncEvent.objects.filter(
            kind="member.withdraw", target_ref=self.order.order_no,
        ).order_by("id"))
        first, last = events
        reports = OperationLog.objects.filter(action="order.jidanpu_withdraw")
        confirmed = _Upstream(lookup={JID: {"orderId": JID, "orderSta": LABEL_REFUNDED}})
        with _stub(confirmed):
            self.assertEqual(jidanpu_member.consume_event(last, client=CLIENT), "done")
        self.assertEqual(reports.count(), 0)
        unconfirmed = _Upstream(lookup={JID: {"orderId": JID, "orderSta": "未分配"}})
        with _stub(unconfirmed):
            self.assertEqual(jidanpu_member.consume_event(first, client=CLIENT), "failed")
        self.assertEqual(reports.count(), 0)
        first.refresh_from_db()
        with _stub(confirmed):
            self.assertEqual(jidanpu_member.consume_event(first, client=CLIENT), "done")
        self.assertEqual(reports.count(), 1)
        self.assertIn("订单 TEST-SOURCE-WITHDRAW 已从其他下家撤下", reports.get().summary)

    def _withdrawal_pair(self, *, hall_only=False):
        other = self._other_binding(base_url="https://other.example.test", enabled=True)
        self._link()
        self._link(binding=other)
        self.assertEqual(jidanpu_member.queue_withdraw(self.order, hall_only=hall_only, summarize=True), 2)
        events = JidanpuSyncEvent.objects.filter(kind=jidanpu_member.KIND_WITHDRAW, target_ref=self.order.order_no)
        return (
            events.get(payload__member_binding_id=self.binding.pk),
            events.get(payload__member_binding_id=other.pk),
        )

    def _withdrawal_report(self):
        return JidanpuSyncEvent.objects.get(kind="member.withdraw_summary", target_ref=self.order.order_no)

    def _fail_first_withdrawal_confirmation(self, upstream):
        read_remote = upstream.fetch_order_by_key1
        reads = 0

        def fail_first_confirmation(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise ApiError("回读超时")
            return read_remote(*args, **kwargs)

        return mock.patch.object(upstream, "fetch_order_by_key1", side_effect=fail_first_confirmation)

    def test_reconcile_replaces_exhausted_event_in_the_same_withdrawal_round(self):
        self.order.status = OrderStatus.IN_PROGRESS
        self.order.save(update_fields=["status"])
        local_taker = User.objects.create_user(username="local-taker")
        Performance.objects.create(order=self.order, user=local_taker, emp_type=EmpType.MAIN)
        first, second = self._withdrawal_pair(hall_only=True)
        with _stub(_Upstream(remote_sta="未分配", thrown=False)):
            self.assertEqual(jidanpu_member.consume_event(second, client=CLIENT), "skipped")
        first.attempts = jidanpu_member.OUTBOX_MAX_ATTEMPTS - 1
        first.save(update_fields=["attempts"])
        upstream = _Upstream(remote_sta="未分配", thrown=True)
        with self._fail_first_withdrawal_confirmation(upstream), _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(first, client=CLIENT), "failed")
        summary = self._withdrawal_report()
        self.assertEqual(summary.status, "failed")
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())

        stats = jidanpu_member.reconcile_and_queue(self.binding, now=timezone.now())
        self.assertEqual(stats.withdraw, 1)
        replacement = JidanpuSyncEvent.objects.get(
            kind=jidanpu_member.KIND_WITHDRAW, status="pending", payload__member_binding_id=self.binding.pk,
        )
        self.assertNotEqual(replacement.pk, first.pk)
        self.assertTrue(replacement.payload["hall_only"])
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(replacement, client=CLIENT), "done")
        self.assertEqual(upstream.names().count("set_order_status"), 1)
        summary.refresh_from_db()
        self.assertEqual(summary.status, "done")
        self.assertEqual(OperationLog.objects.filter(action="order.jidanpu_withdraw").count(), 1)

    def test_rearmed_withdrawal_cannot_complete_an_older_summary(self):
        first, second = self._withdrawal_pair()
        with _stub(_Upstream(remote_sta="未分配")):
            self.assertEqual(jidanpu_member.consume_event(second, client=CLIENT), "done")
        replacement = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_WITHDRAW, rearm=True,
        )
        self.assertEqual(replacement.pk, first.pk)
        with _stub(_Upstream(remote_sta="未分配")):
            self.assertEqual(jidanpu_member.consume_event(replacement, client=CLIENT), "done")
        self.assertEqual(self._withdrawal_report().status, "skipped")
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())

    def test_result_from_an_in_flight_predecessor_does_not_close_rearmed_event(self):
        first, second = self._withdrawal_pair()
        with _stub(_Upstream(remote_sta="未分配")):
            self.assertEqual(jidanpu_member.consume_event(second, client=CLIENT), "done")

        upstream = _Upstream(remote_sta="未分配")
        write_remote = upstream.set_order_status

        def rearm_during_refund(*args, **kwargs):
            jidanpu_member.queue_member_event(
                self.order, self.binding, kind=jidanpu_member.KIND_WITHDRAW, rearm=True,
            )
            return write_remote(*args, **kwargs)

        upstream.set_order_status = rearm_during_refund
        with _stub(upstream):
            jidanpu_member.consume_event(first, client=CLIENT)
        first.refresh_from_db()
        self.assertTrue(first.payload["rearmed_at"])
        self.assertEqual(first.status, "pending")
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())

    def test_already_off_hall_counts_toward_one_actual_withdrawal(self):
        outside_hall, in_hall = self._withdrawal_pair(hall_only=True)
        upstream = _Upstream(remote_sta="未分配", thrown=False)
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(outside_hall, client=CLIENT), "skipped")
        self.assertNotIn("set_order_status", upstream.names())
        with _stub(_Upstream(remote_sta="未分配", thrown=True)):
            self.assertEqual(jidanpu_member.consume_event(in_hall, client=CLIENT), "done")
        self.assertEqual(self._withdrawal_report().status, "done")
        self.assertEqual(OperationLog.objects.filter(action="order.jidanpu_withdraw").count(), 1)

    def test_remote_withdrawal_is_logged_when_retry_confirms_the_accepted_command(self):
        withdrawing, outside_hall = self._withdrawal_pair(hall_only=True)
        with _stub(_Upstream(remote_sta="未分配", thrown=False)):
            self.assertEqual(jidanpu_member.consume_event(outside_hall, client=CLIENT), "skipped")

        upstream = _Upstream(remote_sta="未分配", thrown=True)
        reports = OperationLog.objects.filter(action="order.jidanpu_withdraw")
        with self._fail_first_withdrawal_confirmation(upstream), _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(withdrawing, client=CLIENT), "failed")
            self.assertFalse(reports.exists())
            withdrawing.refresh_from_db()
            self.assertEqual(jidanpu_member.consume_event(withdrawing, client=CLIENT), "done")

        self.assertEqual(upstream.names().count("set_order_status"), 1)
        self.assertEqual(reports.count(), 1)
        self.assertEqual(self._withdrawal_report().status, "done")

    def test_accepted_command_from_a_superseded_round_does_not_report_a_new_withdrawal(self):
        withdrawing, outside_hall = self._withdrawal_pair(hall_only=True)
        upstream = _Upstream(remote_sta="未分配", thrown=True)
        with self._fail_first_withdrawal_confirmation(upstream), _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(withdrawing, client=CLIENT), "failed")

        self.assertEqual(jidanpu_member.queue_withdraw(self.order, hall_only=True, summarize=True), 2)
        withdrawing.refresh_from_db()
        outside_hall.refresh_from_db()
        with _stub(_Upstream(remote_sta="未分配", thrown=False)):
            self.assertEqual(jidanpu_member.consume_event(outside_hall, client=CLIENT), "skipped")
        with _stub(upstream):
            self.assertEqual(jidanpu_member.consume_event(withdrawing, client=CLIENT), "done")

        self.assertEqual(upstream.names().count("set_order_status"), 1)
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())

    def test_two_copies_already_off_hall_do_not_produce_a_withdrawal_log(self):
        first, second = self._withdrawal_pair(hall_only=True)
        with _stub(_Upstream(remote_sta="未分配", thrown=False)):
            self.assertEqual(jidanpu_member.consume_event(first, client=CLIENT), "skipped")
            self.assertEqual(jidanpu_member.consume_event(second, client=CLIENT), "skipped")
        self.assertEqual(self._withdrawal_report().status, "skipped")
        self.assertFalse(OperationLog.objects.filter(action="order.jidanpu_withdraw").exists())

    def test_withdraw_works_for_a_soft_deleted_order(self):
        """我方先撤回 / 删除 ⇒ 走同一条路径（订单行已在默认读路径之外，靠 `all_objects`）。"""
        self._link(in_hall=True)
        self.order.deleted_at = timezone.now()
        self.order.save(update_fields=["deleted_at"])
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        up = _Upstream(lookup={JID: {"orderId": JID, "orderSta": LABEL_REFUNDED}})
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)

    def test_withdraw_without_a_link_is_skipped_without_touching_upstream(self):
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        up = _Upstream(lookup={JID: {"orderId": JID, "orderSta": LABEL_REFUNDED}})
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertNotEqual(event.reason, "")

    def test_a_withdraw_that_does_not_read_back_as_refunded_is_a_failure(self):
        """⛔ 「读不到」不等于「已退单」：只有**显式**已退单才算 done。"""
        self._link(in_hall=True)
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        up = _Upstream(lookup={JID: {"orderId": JID, "orderSta": "未分配"}})
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall)  # 没读回目标态 ⇒ ⛔ 不降级

    def test_a_business_failure_on_refund_still_wins_when_the_readback_confirms(self):
        """重复推送可能回业务失败（已是目标态）⇒ 回读确认后仍算 done（照既有那条线）。"""
        self._link(in_hall=True)
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        up = _Upstream(
            status=ApiError("该单已是该状态", code=0),
            lookup={JID: {"orderId": JID, "orderSta": LABEL_REFUNDED}},
        )
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertFalse(link.in_hall)


# ══ ⑤b ⭐⭐ 退单「办成之后不再被排」（真机那个环）════════════════════════


class WithdrawConfirmGiveupTests(_Base):
    """⭐⭐ **B 族回读确认支的 3 次封顶**（E-0316，2026-11-03 业务方拍板「按次数」）。生产形状：519 张单本地已 REFUNDED、上家回读**读得到**但状态追认不到「已退单」⇒ 旧实现照 `_retry` 烧满 5 次、终态后又被对账重排（11,810 条 failed）。新口径：这一支第 **3** 次（含）仍不确认 ⇒ 终态 `skipped` + 人话；第 1/2 次照常重试；
    ⚠️ 计数 = **单行内计**（事件行自己的 `attempts`）；`link`（含 `withdrawn_at`）一个字不写——目标态没被确认成立，⛔ 不许把「没追认到」记成「已经退成」。"""

    def _event_with_attempts(self, attempts: int):
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=attempts, reason="合成：上一轮未确认",
            processed_at=timezone.now(),
        )
        event.refresh_from_db()
        return event

    def _consume(self, event):
        """读得到但不是「已退单」的那支：写成功、写后回读回「代练中」。"""
        up = _Upstream(sta_after_write="代练中")
        with _stub(up):
            return jidanpu_member.consume_event(event, client=CLIENT)

    def test_first_and_second_unconfirmed_reads_still_retry(self):
        """第 1 / 2 次照常重试（短期内上家状态可能自己变）。"""
        self._link(in_hall=True)
        self.assertEqual(
            self._consume(self._event_with_attempts(0)), "failed"
        )
        self.assertEqual(
            self._consume(self._event_with_attempts(1)), "failed"
        )
        row = JidanpuSyncEvent.objects.filter(
            kind=jidanpu_member.KIND_WITHDRAW, target_ref=self.order.order_no
        ).latest("id")
        self.assertEqual(row.status, "failed")
        self.assertIn(jidanpu_member.WITHDRAW_CONFIRM_REASON, row.reason)

    def test_third_unconfirmed_read_ends_as_skipped_giveup(self):
        """第 3 次（attempts=2，本次再败即第 3 次）⇒ 终态 skipped + 放弃人话。"""
        self._link(in_hall=True)
        event = self._event_with_attempts(jidanpu_member.WITHDRAW_CONFIRM_MAX_ATTEMPTS - 1)
        self.assertEqual(self._consume(event), "skipped")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertIn(jidanpu_member.WITHDRAW_CONFIRM_GIVEUP_REASON, event.reason)
        self.assertEqual(event.attempts, 2, "终态收口 ⛔ 不再消耗额度")
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall, "没确认到退成 ⇒ ⛔ 链路不许说「不在大厅」")
        self.assertIsNone(link.withdrawn_at, "没确认到退成 ⇒ ⛔ 不许落「已经退成」")

    def test_boundary_second_attempt_is_not_yet_giveup(self):
        """边界负性钉：attempts=1（本次是第 2 次）⇒ 必须仍是 `failed`（钉住 ≥ 与 > 的差别）。"""
        self._link(in_hall=True)
        self.assertEqual(
            self._consume(self._event_with_attempts(
                jidanpu_member.WITHDRAW_CONFIRM_MAX_ATTEMPTS - 2
            )),
            "failed",
        )

    def test_giveup_then_unchanged_state_is_not_requeued(self):
        """B 族整链（与 D-0370 护栏互补）：放弃收口后状态无变化 ⇒ 对账排不出新行。
        读数：放弃行（skipped，带指纹）在 ⇒ `reconcile_and_queue` 照判该退，但台账
        零新行——「每 35 分钟重排一条新 skipped 行」的次级循环由此断根。"""
        now = timezone.now()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.REFUNDED)
        self.order.refresh_from_db()
        self._link(in_hall=True)
        jidanpu_member.reconcile_and_queue(self.binding, now=now)
        row = JidanpuSyncEvent.objects.filter(
            kind=jidanpu_member.KIND_WITHDRAW, target_ref=self.order.order_no
        ).latest("id")
        # 把对账排下的在飞行拨成「第 3 次放弃」的终态形态（消费侧本身的 3 次阶梯
        # 由上面三条用例钉；这里构造的是「放弃之后」的台账现场）。
        JidanpuSyncEvent.objects.filter(pk=row.pk).update(
            status="skipped", attempts=jidanpu_member.WITHDRAW_CONFIRM_MAX_ATTEMPTS - 1,
            reason=jidanpu_member.WITHDRAW_CONFIRM_GIVEUP_REASON,
            processed_at=now,
        )
        row.refresh_from_db()
        second = jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(second.withdraw, 1, "对账差集照判（该退的形态没变）")
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind=jidanpu_member.KIND_WITHDRAW, target_ref=self.order.order_no
            ).count(),
            1,
            "终态 skipped + 状态无变化 ⇒ 闸住，台账零新行（次级循环断根）",
        )


class WithdrawNoSpinTests(_Base):
    """⭐⭐ 主判据（端到端）：**退单办成之后，对账不再排第二遍**。
    真机现场（kexing 租户）：退单的判据是「本地已退单 / 已删 ＋ 有链路 ⇒ 排一次」，而**没有任何东西被标成「已经退完了」** ⇒ 每 180 秒那轮对账又排一条（去重只挡在飞，`done` 挡不住新的一条）⇒ 8710 条退单事件只牵涉 185 张单、95% 是**重复排放**、最近 1 小时还新排了 429 条（约每分钟 7 次，从 2026-10-08 就一直在转）。"""

    def setUp(self):
        super().setUp()
        # ⭐ 「该不该退」那一档与**游戏范围**无关（`absent` 判在范围之前），但「被恢复成回池」
        #    那一档要求 `in_scope=True`（`SHAPE_HALL` 的两个条件之一）⇒ 给这份绑定勾上这个游戏。
        StaffProfile.objects.create(user=self.user).games.add(self.game)

    def _withdraw_rows(self):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_WITHDRAW_LITERAL, target_ref=self.order.order_no
        )

    def _launch_rows(self):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_LAUNCH_LITERAL, target_ref=self.order.order_no
        )

    def _make_it_absent(self):
        """把这张单造成「对方不该再有这张单」：我方已退单 ＋ 已软删（spec §5.1 第三行）。"""
        self.order.status = OrderStatus.REFUNDED
        self.order.deleted_at = timezone.now()
        self.order.save(update_fields=["status", "deleted_at"])

    def _withdraw_once(self, *, now):
        """退成一次（回读确认「已退单」）⇒ 台账 `done` + 链路记上「已经退成」。"""
        with _stub(_Upstream(lookup={JID: {"orderId": JID, "orderSta": LABEL_REFUNDED}})):
            stats = jidanpu_member.run_outbox(self.binding, now=now, client=CLIENT)
        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        return JidanpuMemberOrderLink.objects.get(
            binding=self.binding, order=self.order
        )

    def test_three_reconcile_rounds_queue_exactly_one_withdraw_event(self):
        """⭐⭐ **主判据**：一张已退单的单 + 一条链路 ⇒ 退掉之后**连跑 3 轮对账**⇒ 台账里**始终只有 1 条**退单事件（⛔ 不是 4 条）。读数分三段（⛔ 不看「调用过就算」）：① 第 1 轮对账 ⇒ 排 **1** 条（该退的照旧退）；
        ② 真的把它退掉（走真消费者 + 桩，回读确认「已退单」）⇒ 台账 `done` +链路行 `withdrawn_at` 落上 —— **这就是「已经退完了」那个终态**；③ 再连跑 3 轮对账 ⇒ 每轮 `stats.withdraw == 0`、台账行数**仍是 1**。
        判别力（本条**能红**）：把 `reconcile` 里 `link.withdrawn_at is None` 那一半去掉（旧实现只看 `link is not None`）⇒ ②之后每轮又排 1 条 ⇒ 行数变 4、第 ③ 段的 `stats.withdraw` 是 1 ⇒ 本用例红。"""
        self._link(in_hall=True)
        self._make_it_absent()
        now = timezone.now()

        # ① 第 1 轮：该退 ⇒ 恰好 1 条
        first = jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(first.withdraw, 1)
        self.assertEqual(self._withdraw_rows().count(), 1)

        # ② 真的退掉（回读确认「已退单」）
        link = self._withdraw_once(now=now)
        self.assertIsNotNone(
            link.withdrawn_at, "退成之后没落「已经退成」⇒ 下一轮对账又会排一条"
        )
        self.assertEqual(self._withdraw_rows().get().status, "done")

        # ③ 再连跑 3 轮对账：⛔ 一条都不许再排（主判据的读数就在这里）
        for round_no in range(2, 5):
            with self.subTest(round_no=round_no):
                self.assertEqual(
                    jidanpu_member.reconcile_and_queue(self.binding, now=now).withdraw,
                    0,
                    "退成过的单又被对账排了一遍（这正是真机那个环）",
                )
                self.assertEqual(self._withdraw_rows().count(), 1)

    def test_a_restored_order_can_be_withdrawn_again(self):
        """⭐ **可逆性（端到端）**：退成之后单又被恢复（撤销删除 ＋ 回池 ⇒ 期望大厅）⇒ 对账排的是**抛单**（⛔ 不是退单）⇒ 抛单真打出去、链路那个「已经退成」被**清空**⇒ 此后该退的**还能退**（⛔ 不会因此「永远不再退」）。四段判据：① 先退成一次（`withdrawn_at` 非空、`in_hall=False`）；
        ② 恢复成回池形态 ⇒ 下一轮对账排 **1 条 `member.launch`**、退单 **0** 条；③ 消费那条 launch（真消费者 + 桩，回读 `isThrowOrder == "Y"`）⇒ 链路`withdrawn_at` **变回 `None`**（回置点只有这一处）；
        ④ 再把它造回「对方不该有」⇒ 对账**照旧排 1 条退单** ⇒ 能再退。判别力：把 `_consume_launch` 里那个 `withdrawn_at=None` 去掉 ⇒ 第 ③ 段红（标记永远清不掉 ⇒ 那张单**永远不再退**）；把 ①/② 的顺序反过来（先恢复再退）⇒ 第 ① 段红（没有可退的东西）。"""
        self._link(in_hall=True)
        self._make_it_absent()
        now = timezone.now()

        # ① 先退成一次
        jidanpu_member.reconcile_and_queue(self.binding, now=now)
        link = self._withdraw_once(now=now)
        self.assertIsNotNone(link.withdrawn_at)
        self.assertFalse(link.in_hall)

        # ② 恢复：撤销删除 + 回池（我方大厅）
        self.order.deleted_at = None
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["deleted_at", "status"])
        second = jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(second.withdraw, 0, "单被恢复之后该做的是抛单、⛔ 不是退单")
        self.assertEqual(second.launch, 1)
        self.assertEqual(self._launch_rows().count(), 1)

        # ③ 真抛出去 ⇒ 「已经退成」被清空（回置点）
        with _stub(_Upstream(lookup=self._launch_lookup())):
            thrown = jidanpu_member.run_outbox(self.binding, now=now, client=CLIENT)
        self.assertEqual((thrown.done, thrown.failed), (1, 0))
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertIsNone(
            link.withdrawn_at, "抛单成功之后没清「已经退成」⇒ 这张单永远不再退"
        )
        self.assertTrue(link.in_hall)

        # ④ 又该退 ⇒ 照旧排得出来（能再退）
        self._make_it_absent()
        third = jidanpu_member.reconcile_and_queue(self.binding, now=now)
        self.assertEqual(third.withdraw, 1)
        self.assertEqual(self._withdraw_rows().count(), 2)

    def test_a_withdraw_whose_readback_cannot_find_the_order_is_still_a_failure(self):
        """⭐⭐ **①式的「读不到」⛔ 不许被当成「对方没有这张单」**（只改②式，如实登记）。读码核出来的事实（判据在 `jidanpu.fetch_order_by_key1` 与它那一串参数模板）：
        ①式的单查走的是**订单列表端点** `GetOrderList` ＋ `key1`（`orderId` 精确 **OR**`orderIdView` 子串），**只查第一页**（`limit=20`），时间窗是**按录单时间**的滚动近90 天（`ORDER_LIST_LOOKBACK_DAYS`）⇒「读不到」大量是**口径**问题（90 天外的老单 /第一页被子串命中挤满），而那些单恰恰**最需要退**。⇒ 本档必须照旧收敛成 `failed` + 退避（⛔ 不终态收口、⛔ 不落「已经退成」）。
        判别力：把①式那一份 `WithdrawPrimitives.absent_means_gone_reason` 也填上一句话（「两条线一起改」）⇒ 本用例红（台账变 `skipped`、链路被标成「已经退成」）。"""
        self._link(in_hall=True)
        event = self._event(jidanpu_member.KIND_WITHDRAW)
        #: ⛔ `lookup={}` = 回读**查不到**这张单（第一页之外 / 约 90 天窗外，与「查无此单」同形）；
        #: `sta_after_write=None` = **写后那一次回读也读不到**（真实世界：老单本来就查不到，
        #: ⛔ 不设它的话桩会把写进去的标签当成读得回来 ⇒ 用例变成另一档、静默假绿）。
        up = _Upstream(lookup={}, sta_after_write=None)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertIn(jidanpu_member.WITHDRAW_CONFIRM_REASON, event.reason)
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall, "没确认到 ⇒ ⛔ 不许把链路说成「不在大厅」")
        self.assertIsNone(link.withdrawn_at, "①式「读不到」⛔ 不许落「已经退成」")


# ══ ⑥b 撤下抛单（`member.unlist`；业务方明确要求这个行为）═══════════
# 业务方报的错：「我方打手在大厅接了之后，下家那张单被从抢单大厅拉回、并且直接变成『代练中』」⇒ 要的是「**停在未分配**」（不在他们的抢单大厅、也不指派给他们任何人）。
# 本线早先因为「上游写端点载荷未实测」而**取消**了这个动作；现在业务方明确要求 ⇒用 SDK 登记里那个端点把它做出来（候选与依据见 `ReleaseOrderToHomePrimitiveTests`）。


class ReleaseOrderToHomePrimitiveTests(SimpleTestCase):
    """撤下抛单的**上游原语**：路径 / 方法 / 参数 / 载荷形状（业务方 2026-09-28）。依据（候选端点与理由见本文件 ⑥b 的注释）：
    - SDK 端点登记 `spec/endpoints.json`：`POST /OrderAjax/RelesOrderToHomeByOrderID`、`risk=write`（⇒ 成功判据 `code > 0`）、`referer=/Order/orderlist`；- `spec/write_params.json` 的该端点参数 = `["orderId"]`；
    - 对标页面 JS（`/JS2019/Order/OrderList20191212.js` 的 `CancelInternalThorwOrder`）：`data: { orderId: orderID }`，确认文案「确定从内部接单大厅撤销该订单吗？撤销后，内部接单大厅将看不到该订单」。"""

    def test_the_path_is_the_registered_endpoint(self):
        self.assertEqual(
            jidanpu.RELEASE_TO_HOME_PATH, "/OrderAjax/RelesOrderToHomeByOrderID"
        )

    def test_it_posts_only_the_order_id(self):
        client = mock.Mock()
        client.call.return_value = {"code": 1, "msg": "撤销成功"}
        jidanpu.release_order_to_home(client, remote_order_id=JID)
        client.call.assert_called_once_with(
            "POST", "/OrderAjax/RelesOrderToHomeByOrderID", data={"orderId": JID}
        )


class UnlistConsumeTests(_Base):
    """⑥b `member.unlist`：从对方**内部接单大厅**撤下（⛔ 不退单）。
    ⚠️ 三段式：**先读一遍**（判对方此刻还在不在大厅）→ **写** → **再读一遍**确认。先读是承重的：本地 `in_hall` 只是标记，可能已经过期（对方打手早接走了）——对着一个已经被接走的单调「撤销」可能把对方的指派毁掉。"""

    def _link_in_hall(self, *, in_hall=True):
        return self._link(binding=self.binding, order=self.order, in_hall=in_hall)

    def test_it_pulls_the_order_back_and_records_that_it_is_no_longer_in_the_hall(self):
        link = self._link_in_hall()
        up = _Upstream(remote_id=JID, thrown=True)
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(
            dict(up.calls)["release_order_to_home"], {"remote_order_id": JID}
        )
        link.refresh_from_db()
        self.assertIs(link.in_hall, False, "撤下确认之后链路标记必须改成「不在他家大厅」")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_it_reads_before_writing_and_never_writes_when_the_order_left_the_hall(self):
        """⭐ 对方那张单**早就不在大厅**（本地标记过期）⇒ ⛔ 一次写都不发，只把标记改正。
        判别力：去掉写之前那次读 ⇒ 本用例会在 `release_order_to_home` 上看到调用
        （对着一张已经被对方打手接走的单调「撤销」）。"""
        link = self._link_in_hall()
        up = _Upstream(remote_id=JID, thrown=False)
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertNotIn("release_order_to_home", up.names())
        link.refresh_from_db()
        self.assertIs(link.in_hall, False)

    def test_an_unreadable_remote_order_is_a_loud_retry(self):
        """读不到远端那张单 ⇒ 判不了它还在不在大厅 ⇒ `failed` 重试（⛔ 不许当成「已经撤下」）。"""
        self._link_in_hall()
        up = _Upstream(remote_id=JID, thrown=True, fetch_error=ApiError("读不到", code=0))
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertNotIn("release_order_to_home", up.names())
        event.refresh_from_db()
        self.assertEqual(event.attempts, 1)
        self.assertNotEqual(event.reason, "")

    def test_a_remote_that_is_still_in_the_hall_after_the_write_is_a_human_failure(self):
        """写完之后回读**仍然** `isThrowOrder == "Y"` ⇒ 撤下没成 ⇒ `failed` + 人话。"""
        self._link_in_hall()
        up = _Upstream(remote_id=JID, thrown=True)
        # 让「撤下」这个端点**假装成功**但其实没生效：把 thrown 重新置回 True。

        def _noop_release(client, *, remote_order_id):
            up.calls.append(("release_order_to_home", {"remote_order_id": remote_order_id}))
            up.thrown = True  # ⛔ 没生效

        up.release_order_to_home = _noop_release
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertIn("release_order_to_home", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertNotEqual(event.reason, "")

    def test_a_throw_rejection_is_a_human_failure_without_upstream_text(self):
        """撤下被上游拒 + 回读也没有离开大厅 ⇒ `failed`，人话里⛔ 不回显上游正文。"""
        self._link_in_hall()
        up = _Upstream(remote_id=JID, thrown=True, release=ApiError("拒绝撤下", code=0))
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertNotIn("拒绝撤下", event.reason)

    def test_without_a_link_it_is_skipped_with_a_human_reason(self):
        """从没录到对方系统 ⇒ 无从撤起：`skipped` + 人话，⛔ 不静默、⛔ 不打上游。"""
        event = self._event(jidanpu_member.KIND_UNLIST)
        up = _Upstream(remote_id=JID, thrown=True)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertNotEqual(event.reason, "")

    def test_a_link_already_marked_out_of_the_hall_is_a_noop(self):
        """本地标记已经是「不在大厅」⇒ 目标态已达成：⛔ 一次上游都不打。"""
        self._link_in_hall(in_hall=False)
        up = _Upstream(remote_id=JID, thrown=False)
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(up.names(), [])

    # ── ⭐ 判据修准（B 节）：目标态 = 「不在厅 **且** 停在未分配」────────────────────
    # ⚠️ 真机读数：撤下端点对**已被对方接走**的单**无效**，却仍回`code=1「撤销成功」`、回读一个字没变 ⇒ 「不在厅」这一格分不出
    # 「我们撤下来了」与「对方抢先接走了」，只有 `orderSta` 能分辨。

    def test_the_remote_ending_up_unassigned_is_the_target_state(self):
        """⭐ (a) 回归：在厅（`Y`/未分配）⇒ 调上游；回读 `N`/未分配 ⇒ `done` + 链路 `in_hall=False`。
        判别力：把「未分配」当成非目标态（例如错判成「接走」）⇒ 本用例会拿到 `skipped`。"""
        link = self._link_in_hall()
        up = _Upstream(
            lookup={JID: {"orderId": JID, "orderSta": LABEL_UNASSIGNED,
                          "isThrowOrder": "Y"}}
        )

        def _release(client, *, remote_order_id):
            up.calls.append(
                ("release_order_to_home", {"remote_order_id": remote_order_id})
            )
            up.lookup[JID] = {"orderId": JID, "orderSta": LABEL_UNASSIGNED,
                              "isThrowOrder": "N"}

        up.release_order_to_home = _release
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertIn("release_order_to_home", up.names())
        link.refresh_from_db()
        self.assertIs(link.in_hall, False)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, "")

    def test_a_remote_taken_before_the_write_is_skipped_and_never_written(self):
        """⭐ (b) **写之前**对方就接走了（前置回读 `N`/代练中）：
        ⛔ 一次上游写都不发；链路标记照旧改正（它确实不在他们的厅里了）；
        事件 `skipped` + 点名 reason（⛔ 不是 `done`、⛔ 不自动退单）。"""
        link = self._link_in_hall()
        up = _Upstream(
            lookup={JID: {"orderId": JID, "orderSta": LABEL_IN_PROGRESS,
                          "isThrowOrder": "N"}}
        )
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "skipped"
            )
        self.assertNotIn("release_order_to_home", up.names())
        link.refresh_from_db()
        self.assertIs(link.in_hall, False, "它确实不在他们的厅里了 ⇒ 标记照旧改正")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, UNLIST_TAKEN_REASON_LITERAL)

    def test_a_remote_taken_after_the_write_is_skipped_not_done(self):
        """⭐ (c) **写之后**对方接走了（延迟竞态）：前置 `Y`/未分配，写成功，回读 `N`/代练中
        ⇒ `skipped` + 点名 reason（⛔ **不是** `done`）——真机读数：该原语对已被接走的单
        **无效且仍回 `code=1`**，所以「读回 `N`」不能当成功。"""
        link = self._link_in_hall()
        up = _Upstream(
            lookup={JID: {"orderId": JID, "orderSta": LABEL_UNASSIGNED,
                          "isThrowOrder": "Y"}}
        )

        def _release_but_peer_grabs(client, *, remote_order_id):
            up.calls.append(
                ("release_order_to_home", {"remote_order_id": remote_order_id})
            )
            # 写的那一瞬间对方打手把单接走了：撤下对这个端点无效（真机读数）。
            up.lookup[JID] = {"orderId": JID, "orderSta": LABEL_IN_PROGRESS,
                              "isThrowOrder": "N"}

        up.release_order_to_home = _release_but_peer_grabs
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=CLIENT), "skipped"
            )
        self.assertIn("release_order_to_home", up.names())
        link.refresh_from_db()
        self.assertIs(link.in_hall, False)
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, UNLIST_TAKEN_REASON_LITERAL)

    def test_a_missing_remote_row_on_readback_is_a_retry(self):
        """⭐ (d) 回读**读不到**（`None`）⇒ 仍 `_retry`（「读不到」≠「已撤下」，既有语义不许漂移）。"""
        self._link_in_hall()
        up = _Upstream()  # lookup / remote_id 都不给 ⇒ 回读回 `None`
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertNotIn("release_order_to_home", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertNotEqual(event.reason, "")

    def test_a_readback_without_a_usable_order_sta_is_a_retry(self):
        """⭐ (e) 回读里 `orderSta` **读不出来** ⇒ 判不了目标态 ⇒ `_retry`（判不了 ≠ 成功）。
        三种形状都要判成「判不了」：**键缺失** / `None` / **非字符串**。"""
        for index, unreadable in enumerate(
            ("键缺失", None, 123), start=1
        ):
            with self.subTest(order_sta=unreadable):
                remote_id = f"JDP-0000000000000000300{index}"
                order = Order.objects.create(
                    game=self.game,
                    order_no=f"2026092900000000300{index}",
                    status=OrderStatus.WAITING_GRAB,
                    hall_price="12.34",
                    total_price="50.00",
                    project_text="合成项目甲",
                )
                self._link(order=order, remote_id=remote_id)
                row = {"orderId": remote_id, "isThrowOrder": "N"}
                if unreadable != "键缺失":
                    row["orderSta"] = unreadable
                up = _Upstream(lookup={remote_id: row})
                event = self._event(jidanpu_member.KIND_UNLIST, order=order)
                with _stub(up):
                    self.assertEqual(
                        jidanpu_member.consume_event(event, client=CLIENT), "failed"
                    )
                self.assertNotIn("release_order_to_home", up.names())
                event.refresh_from_db()
                self.assertEqual(event.status, "failed")
                self.assertEqual(event.attempts, 1)
                self.assertNotEqual(event.reason, "")

    def test_a_readback_with_an_unexpected_order_sta_names_the_actual_label(self):
        """⭐ (f) 回读里 `orderSta` **读得到、但既不是「未分配」也不是「代练中」**（`other` 档，例如已退单 / 已流转）⇒ 按未确认 `_retry`，且 reason 里**带上实际读到的那个标签值**（⛔ 只写「不是目标态」排障时看不出到底读到了什么——运维手上没有上游那一行）。
        判别力：把 reason 换回那句不带值的 `UNLIST_CONFIRM_REASON`（或把标签截掉）⇒ 本用例红。⚠️ 也钉住长度：整句落在 `JidanpuSyncEvent.reason` 的 255 上限内（⛔ 不靠 `_retry` 的截断）。"""
        self._link_in_hall()
        up = _Upstream(
            lookup={JID: {"orderId": JID, "isThrowOrder": "N", "orderSta": LABEL_OTHER}}
        )
        event = self._event(jidanpu_member.KIND_UNLIST)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertNotIn("release_order_to_home", up.names())
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, UNLIST_OTHER_REASON_LITERAL)
        self.assertLessEqual(len(event.reason), 255)


# ══ ⑥ member.status_out：调对方改状态端点 ═════════════════════════════════


class StatusOutConsumeTests(_Base):
    """`member.status_out`：业务方 2026-10-03 收窄成**只推三档 + 一个来源态例外 + 推前回读**；
    2026-11-05（E-0318/D-0376）再叠**接手人同店闸**：复活档必须有接手人且本绑定是接手人那家店、
    问题单/已暂停在有接手人时只推接手人那家店；已退单与手动两档不受闸。"""

    def _set_status(self, status):
        Order.objects.filter(pk=self.order.pk).update(status=status)

    def _taker(self, user=None):
        """给这张单挂一个 MAIN 接手绩效行（缺省=绑定本人 ⇒「同店」正例的夹具形态；
        ⚠️ 接手人是谁与绑定是谁是两回事——同店判据走店地址集合，不比 `user_id`）。"""
        return Performance.objects.create(
            order=self.order, user=user or self.user, emp_type=EmpType.MAIN,
        )

    def test_the_three_pushable_labels_go_out_verbatim(self):
        """⭐ 推送集只有**三档**（业务方 2026-10-03）：问题单 / 已暂停 / 已退单，
        `status_label` **逐字**相同。
        判别力：把某一档从推送集里去掉 / 换成别的文案 ⇒ 对应子用例红。"""
        self._link()
        for status, label in (
            (OrderStatus.ISSUE, LABEL_ISSUE),
            (OrderStatus.PAUSED, LABEL_PAUSED),
            (OrderStatus.REFUNDED, LABEL_REFUNDED),
        ):
            with self.subTest(status=status):
                self._set_status(status)
                event = self._event(jidanpu_member.KIND_STATUS_OUT)
                up = _Upstream()
                with _stub(up):
                    self.assertEqual(
                        jidanpu_member.consume_event(event, client=CLIENT), "done"
                    )
                sent = dict(up.calls)["set_order_status"]
                self.assertEqual(sent["remote_order_id"], JID)
                self.assertEqual(sent["status_label"], label)
                event.refresh_from_db()
                self.assertEqual(event.status, "done")

    def test_non_pushable_statuses_are_skipped_without_touching_upstream(self):
        """⭐ 待验收 / 已完成 / 未分配 / 待抢单 ⇒ `skipped`、**一次上游都不打**（业务方 2026-10-03）。
        ⚠️ 即使 `from_status` 给的是**能推的那一档**（已完成）也不行：推送集判的是**目标态**，「代练中」那个例外只对 `IN_PROGRESS` 这一档开。判别力：把「待验收 / 已完成」放回推送集 ⇒ 对应子用例红。"""
        self._link()
        for status in (
            OrderStatus.PENDING_REVIEW,
            OrderStatus.COMPLETED,
            OrderStatus.UNASSIGNED,
            OrderStatus.WAITING_GRAB,
        ):
            with self.subTest(status=status):
                self._set_status(status)
                event = self._event(
                    jidanpu_member.KIND_STATUS_OUT,
                    payload={"from_status": "COMPLETED"},
                )
                up = _Upstream()
                with _stub(up):
                    self.assertEqual(
                        jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                    )
                self.assertEqual(up.names(), [], "这一档不许对对方发出任何调用")
                event.refresh_from_db()
                self.assertEqual(event.status, "skipped")
                self.assertIn("不推", event.reason)

    def test_a_completed_source_reverts_the_downstream_to_in_progress(self):
        """⭐ 唯一例外（业务方 2026-10-03）：来源态 = **已完成** + 目标态 = 代练中 ⇒ 推「代练中」。
        ⚠️ 来源态写**字面值** `"COMPLETED"`（⛔ 不从被测常量派生：那样断言会跟着实现漂）。
        ⚠️ 2026-11-05 起（E-0318/D-0376）复活档过接手人同店闸 ⇒ 夹具挂**绑定本人**当接手人（同店正例）。
        判别力：把这个来源态从例外集合里去掉 ⇒ 本用例红（收到 `skipped`、上游只被读一次）。"""
        self._taker()
        self._link()
        self._set_status(OrderStatus.IN_PROGRESS)
        event = self._event(
            jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "COMPLETED"}
        )
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_IN_PROGRESS)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_pending_review_source_reverts_the_downstream_to_in_progress(self):
        """⭐ 例外的第二半（本线 D-0183）：来源态 = **待验收** ⇒ 同样推「代练中」。
        ⛔ 与上一条**必须分开**：只钉「已完成」那一条的话，一个「来源态恒写 COMPLETED」的实现（那是**假话**）照样能全绿。
        ⚠️ 2026-11-05 起复活档过接手人同店闸 ⇒ 夹具挂**绑定本人**当接手人（同店正例）。
        判别力：把 `PENDING_REVIEW` 从例外集合里去掉 ⇒ 本用例红。"""
        self._taker()
        self._link()
        self._set_status(OrderStatus.IN_PROGRESS)
        event = self._event(
            jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "PENDING_REVIEW"}
        )
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_IN_PROGRESS)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_refunded_source_pushes_in_progress_to_the_downstream(self):
        """⭐ 例外第三档（两线并集）：来源态 = 已退单 ⇒ 把「代练中」推给下家、事件收敛 `done`。来源态包含已退单。少了这一档，客服把退单改回代练中时下家不跟。
        ⚠️ 来源态写**字面值** `"REFUNDED"`（⛔ 不从被测模块的常量派生：那样断言会跟着实现漂）。
        ⚠️ 2026-11-05 起复活档过接手人同店闸 ⇒ 夹具挂**绑定本人**当接手人（同店正例）。
        判别力：把 `_consume_status_out` 的来源闸改成「无条件跳过 `IN_PROGRESS`」（= 回到2026-09-28 那版）⇒ 本用例红（收到 `skipped`、上游一次都没打）。"""
        self._taker()
        self._link(in_hall=True)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        event = self._event(
            jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "REFUNDED"}
        )
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_IN_PROGRESS)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_revert_without_a_taker_is_skipped_before_any_link_or_upstream(self):
        """⭐ 接手人闸·第一格（2026-11-05 业务方拍板，E-0318/D-0376）：复活档这张单**没有接手人**
        （MAIN 绩效行缺失，或行在但无主——E-0148 口径那是正常数据形态）⇒ `skipped`、零上游调用、
        **一次链路表都不查**（闸在取链路之前，与池内档同一条便宜顺序）。原因**逐字**断
        `REVERT_NO_TAKER_REASON_LITERAL`（E-0024）。现场（E-0318）：上家删单恢复 ⇒ 我方「已退单→代练中」
        把与接手无关的下家店已撤的卡改回「代练中」，之后单完成但「已完成」档不推 ⇒ 店卡永停。
        判别力：把「无接手人 ⇒ skipped」那一支删掉 ⇒ 本用例红（会发一次「代练中」）。"""
        self._link(in_hall=True)
        self._set_status(OrderStatus.IN_PROGRESS)
        for row_kind, make_row in (
            ("MAIN行缺失", lambda: None),
            ("MAIN行无主", lambda: Performance.objects.create(
                order=self.order, user=None, emp_type=EmpType.MAIN,
            )),
        ):
            with self.subTest(row=row_kind):
                self._events_of_status_out().delete()
                event = self._event(
                    jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "REFUNDED"}
                )
                up = _Upstream()
                with CaptureQueriesContext(connection) as ctx:
                    with _stub(up):
                        self.assertEqual(
                            jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                        )
                self.assertEqual(
                    up.names(), [], "无接手人的复活不许对对方发出任何调用"
                )
                event.refresh_from_db()
                self.assertEqual(event.status, "skipped")
                self.assertEqual(event.reason, REVERT_NO_TAKER_REASON_LITERAL)
                self.assertEqual(
                    _link_table_queries(ctx), [],
                    "被闸的行必须在取链路之前就收敛（零链路查询）",
                )

    def test_a_revert_taker_from_another_store_is_skipped_without_any_upstream(self):
        """⭐ 接手人闸·第二格（E-0318/D-0376）：复活档有接手人，但接手人名下的店**不含本绑定这家店**
        ⇒ `skipped`、零上游、零链路查询；原因**逐字**断 `TAKER_STORE_MISMATCH_REASON_LITERAL`。
        现场形状：接手人=我方（或另一家店）的打手，本绑定是别的下家店——那次「代练中」与这家店无关。
        判别力：把同店判据删掉（无条件照推）⇒ 本用例红（会发一次「代练中」）。"""
        other = self._other_binding(base_url=BASE_URL_B, enabled=True)
        self._taker(user=other.user)
        self._link(in_hall=True)
        self._set_status(OrderStatus.IN_PROGRESS)
        event = self._event(
            jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "REFUNDED"}
        )
        up = _Upstream()
        with CaptureQueriesContext(connection) as ctx:
            with _stub(up):
                self.assertEqual(
                    jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                )
        self.assertEqual(up.names(), [], "不是接手人的店不许对对方发出任何调用")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, TAKER_STORE_MISMATCH_REASON_LITERAL)
        self.assertEqual(
            _link_table_queries(ctx), [],
            "被闸的行必须在取链路之前就收敛（零链路查询）",
        )

    def test_a_revert_taker_on_a_disabled_binding_of_the_same_store_still_pushes(self):
        """⭐⭐ 同店判据必须是「店地址集合」（`downstream_addresses_of_taker`，**含停用绑定**），
        ⛔ 不是 `binding.user_id == taker_id`：接手人是②式下家系统账号（他那份绑定**停用**）、
        地址与本绑定**同店** ⇒ 照推（2026-10-25 生产事故形状：同店两条绑定、旧判据判漏 ⇒ 把正在做的活单退了）。
        判别力：把判据写成 `binding.user_id == taker_id` ⇒ 本用例红（被误判成「不是他的店」而 skipped）。"""
        other = self._other_binding(base_url=BASE_URL, enabled=False)
        self._taker(user=other.user)
        self._link(in_hall=True)
        self._set_status(OrderStatus.IN_PROGRESS)
        event = self._event(
            jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "REFUNDED"}
        )
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_IN_PROGRESS)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_pool_source_still_skips_in_progress_and_never_reads_the_link(self):
        """⭐ A2（D-0180）：来源态 = 池内（待抢单）⇒ 照旧 `skipped`、**零**上游调用。⚠️ 顺带钉住**便宜顺序**：这一档**一次链路表都不查**（把来源闸挪到取链路之后就红）——`apply_transition` 是全仓最热的写入口，这一档正是它每次都要走的那条路。原因断言断 `STATUS_NOT_PUSHABLE_REASON_LITERAL`（逐字，不从被测常量派生）。
        ⛔ 判据没放宽：仍然是「零上游调用 + 链路表零查询」。判别力：把来源闸删掉（无条件往下走）⇒ 本用例红（会发一次 `代练中`）。"""
        self._link(in_hall=True)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        event = self._event(
            jidanpu_member.KIND_STATUS_OUT, payload={"from_status": "WAITING_GRAB"}
        )
        up = _Upstream()
        with CaptureQueriesContext(connection) as ctx:
            with _stub(up):
                self.assertEqual(
                    jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                )
        self.assertEqual(up.names(), [], "池内来源不许对对方发出任何写调用")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, STATUS_NOT_PUSHABLE_REASON_LITERAL)
        self.assertEqual(
            _link_table_queries(ctx), [], "这一档必须在取链路之前就收敛（便宜顺序）"
        )

    def test_our_take_is_never_pushed_as_the_downstreams_in_progress(self):
        """⭐ 业务方 2026-09-28：**我方接手（`IN_PROGRESS`）不许推出成对方的「代练中」**。要的行为：我方指派 / 打手在**我方**大厅接单 ⇒ 对方那张单**停在「未分配」**；只有**对方自己的打手**在他们抢单大厅接单，那张单才在他们那边变「代练中」。判据：一次上游都不打、事件收敛成 `skipped` **且带人话原因**（⛔ 不静默）。
        ⚠️ 本用例走的是**来源态缺失**那一档（事件直接排队、payload 里没有 `from_status`）⇒ 按 fail-closed 处理（快照缺失 / 空 / 认不出一律当「**不在**例外来源集里」）。⚠️ **判别依据已换（D-0180）**：旧依据写的是「把 `STATUS_LABELS` 里的 `IN_PROGRESS`那一档**放回去** ⇒ 红」——那一格**本来就在表里**（「已完成 / 待验收 / 已退单」→ 代练中三档都要用它），所以旧依据在当时就已不成立，本批按实现改成下面这一条。
        ⛔ **新判别力 = 去掉 `payload["from_status"]` 那道来源闸**（改成无条件推`IN_PROGRESS`）⇒ 本用例红（会真的发一次 `代练中`）。原因断言用逐字常量，判据仍是零上游调用。"""
        self._link(in_hall=True)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        event.refresh_from_db()
        self.assertNotIn(
            "from_status", dict(event.payload or {}), "本用例问的是「来源态缺失」那一档"
        )
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [], "我方接手不许对对方发出任何写调用")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, STATUS_NOT_PUSHABLE_REASON_LITERAL)

    def test_a_pool_source_never_pushes_in_progress_and_never_reads_the_link(self):
        """⭐⭐ **承重反例**（业务方 2026-09-28 口径不变）：来源态是**池内两态**（未分配 / 待抢单）的「代练中」= 我方指派 / 打手在**我方**大厅接单 ⇒ **一律不推**（对方那张单停在「未分配」）。
        ⚠️ fail-closed 也在这里钉：`from_status` **缺失 / `None` / 非字符串**一律按「**不在**例外来源集里」处理 ⇒ 不推（⛔ 不做任何归一化）。
        ⚠️ 顺带钉住**便宜顺序**：这一档**一次链路表都不查**（把来源闸挪到取链路之后就红）——`apply_transition` 是全仓最热的写入口，这一档正是它每次都要走的那条路。判别力：① 把池内两态混进例外集合 ⇒ 本用例红（会发一次「代练中」）；② 把来源闸挪到取链路之后 ⇒ 链路表查询数断言红。"""
        self._link()
        self._set_status(OrderStatus.IN_PROGRESS)
        for payload in (
            {"from_status": "WAITING_GRAB"},
            {"from_status": "UNASSIGNED"},
            {"from_status": None},
            {"from_status": 123},
            {},
        ):
            with self.subTest(payload=payload):
                event = self._event(
                    jidanpu_member.KIND_STATUS_OUT, payload=dict(payload)
                )
                up = _Upstream()
                with CaptureQueriesContext(connection) as ctx:
                    with _stub(up):
                        self.assertEqual(
                            jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                        )
                self.assertEqual(up.names(), [], "池内来源不许对对方发出任何调用")
                event.refresh_from_db()
                self.assertEqual(event.status, "skipped")
                self.assertIn("不推", event.reason)
                self.assertEqual(
                    _link_table_queries(ctx), [], "这一档必须在取链路之前就收敛（便宜顺序）"
                )

    def test_issue_and_paused_pushes_to_a_store_the_taker_is_not_on_are_skipped(self):
        """⭐ 问题单/已暂停两档的接手人同店闸（2026-11-05 业务方拍板，E-0318/D-0376）：
        **有**接手人且接手人名下的店不含本绑定这家店 ⇒ `skipped`（第二格 reason 复用）、零上游、
        零链路查询。判别力：把这两档从闸里放走 ⇒ 本用例红（会发「问题单/已暂停」）。"""
        other = self._other_binding(base_url=BASE_URL_B, enabled=True)
        self._taker(user=other.user)
        self._link()
        for status, label in ((OrderStatus.ISSUE, LABEL_ISSUE), (OrderStatus.PAUSED, LABEL_PAUSED)):
            with self.subTest(status=status):
                self._events_of_status_out().delete()
                self._set_status(status)
                event = self._event(jidanpu_member.KIND_STATUS_OUT)
                up = _Upstream()
                with CaptureQueriesContext(connection) as ctx:
                    with _stub(up):
                        self.assertEqual(
                            jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                        )
                self.assertEqual(
                    up.names(), [], f"有接手人时「{label}」不许推给接手人以外的店"
                )
                event.refresh_from_db()
                self.assertEqual(event.status, "skipped")
                self.assertEqual(event.reason, TAKER_STORE_MISMATCH_REASON_LITERAL)
                self.assertEqual(
                    _link_table_queries(ctx), [],
                    "被闸的行必须在取链路之前就收敛（零链路查询）",
                )

    def test_an_issue_push_with_no_taker_still_goes_to_every_store(self):
        """问题单/已暂停两档的闸**只挡「有接手人」**的形态：无接手人（MAIN 行缺失）⇒ 照旧推全部。
        （「推出去」的正面判定由 `test_the_three_pushable_labels_go_out_verbatim` 钉——它的夹具本就没有
        MAIN 绩效行；本条补的是「闸不误伤无接手人那一档」的显式对照。）判别力：把闸写成「无接手人也 skip」
        ⇒ 本用例红。"""
        self._link()
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["status_label"], LABEL_ISSUE)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_an_issue_push_to_the_takers_own_store_still_pushes(self):
        """对照（同店 ⇒ 照推）：有接手人且接手人名下的店**含**本绑定这家店（这里接手人=绑定本人）
        ⇒ 照旧推「问题单」。判别力：把同店判据写反（同店也 skip）⇒ 本用例红。"""
        self._taker()
        self._link()
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["status_label"], LABEL_ISSUE)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_refunded_push_still_goes_to_every_store_even_when_a_taker_exists(self):
        """⭐ 已退单**无闸**（2026-11-05 业务方拍板「已退单发全部」）：有接手人且接手人不是本店的人
        ⇒ **照旧推**。为什么不闸：推前回读对已关的卡是零写动作；下家店手里的「已退单」卡要靠它关门，
        还能自愈撤单失败漏掉的网（E-0318 残局）。判别力：把已退单塞进同店闸 ⇒ 本用例红（该店收不到）。"""
        other = self._other_binding(base_url=BASE_URL_B, enabled=True)
        self._taker(user=other.user)
        self._link()
        self._set_status(OrderStatus.REFUNDED)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["status_label"], LABEL_REFUNDED)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_a_pre_read_that_already_shows_the_target_label_writes_nothing(self):
        """⭐⭐ 业务方 2026-10-03：**推前回读**命中目标态 ⇒ 直接 `done`、**一次写接口都不打**。
        真机依据（照抄，⛔ 不是推断）：重复推同一状态对方回的是**失败信封**（原话「当前已是已退单状态，无需更改」）⇒ 直接推会记失败 + 退避重试 5 次 + 把`last_error` 点红。判别力（变异②）：拆掉「推前回读命中 ⇒ 直接 `done`」那一支 ⇒ 本用例红（会打一次写接口）。"""
        self._link()
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream(remote_sta=LABEL_ISSUE)  # 对方那张单**此刻就是**目标态
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(up.names(), ["fetch_order_by_key1"], "一次写接口都不打")
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_the_latest_refunded_intent_replaces_a_pending_pool_intent(self):
        """同一待办采用最新来源态，退单恢复不会被旧池内快照吞掉。"""
        self._taker()
        self._link(in_hall=True)
        self._set_status(OrderStatus.IN_PROGRESS)
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.WAITING_GRAB)
        jidanpu_member.queue_status_out(self.order, from_status=OrderStatus.REFUNDED)
        rows = JidanpuSyncEvent.objects.filter(
            kind=jidanpu_member.KIND_STATUS_OUT, target_ref=self.order.order_no
        )
        self.assertEqual(rows.count(), 1)
        event = rows.get()
        self.assertEqual(event.payload.get("from_status"), "REFUNDED")
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(dict(up.calls)["set_order_status"]["status_label"], LABEL_IN_PROGRESS)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_status_out_for_a_hall_state_is_skipped_without_touching_upstream(self):
        """池内两态（未分配 / 待抢单）在对方那边的对应物是录单 / 抛单，⛔ 不是改状态。
        原因断言逐字断 `STATUS_NOT_PUSHABLE_REASON_LITERAL`。池内两态不推，人话就是这一档。"""
        self._link(in_hall=True)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, STATUS_NOT_PUSHABLE_REASON_LITERAL)

    def test_an_unreadable_pre_read_pushes_anyway(self):
        """⭐ 回读**读不到**（`None`）⇒ **照常推**（⛔ 不能把「读不到」当「不需要推」）。
        读不到的原因：该单查只查第一页 + 约 90 天窗（`fetch_order_by_key1` 的窗口）⇒ 老单本来就可能永远读不到；把它当成「已是目标态」会让那类单的推送**永久丢失**。判别力：把「读不到」也收敛成 `done`（= 跳过推送）⇒ 本用例红（写接口零调用）。"""
        self._link()
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()  # 回读一律 `None`
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["remote_order_id"], JID)
        self.assertEqual(sent["status_label"], LABEL_ISSUE)

    def test_a_write_that_is_not_confirmed_is_a_human_failure(self):
        """⭐ **写后回读确认**（照 `_consume_withdraw` 的「回读为准」形状）：读到**别的**标签 /
        **读不到** ⇒ `failed` + 人话原因（⛔ 不许写完就 `done`）。
        判别力：把写后回读那一支删掉（写完直接 `done`）⇒ 本用例红。"""
        self._link()
        self._set_status(OrderStatus.ISSUE)
        for after in (LABEL_PAUSED, None):
            with self.subTest(after=after):
                # ⚠️ 上一轮留下的是 `failed` 行 ⇒ 不清掉的话 `queue_member_event` 的去重会**复用它**
                #    （在飞= pending/failed）⇒ 本轮的 `attempts` 会从上一轮接着涨，判据就不再是
                #    「这一轮发生了什么」。
                self._events_of_status_out().delete()
                event = self._event(jidanpu_member.KIND_STATUS_OUT)
                up = _Upstream(sta_after_write=after)
                with _stub(up):
                    self.assertEqual(
                        jidanpu_member.consume_event(event, client=CLIENT), "failed"
                    )
                event.refresh_from_db()
                self.assertEqual(event.status, "failed")
                self.assertEqual(event.attempts, 1)
                self.assertIn("写后未能确认", event.reason)

    def test_a_pre_read_failure_is_a_loud_retry(self):
        """推前回读那一跳**上游报错** ⇒ `failed` + 人话（⛔ 不盲目照推：写下去可能正好撞上
        「当前已是目标态」那个失败信封，把这一跳记成失败 + 退避）。
        判别力：把这一支改成「回读报错也照常推」⇒ 本用例红（会发一次写接口、结果 `done`）。"""
        self._link()
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream(fetch_error=ApiError("回读炸了", code=None))
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        self.assertEqual(up.names(), ["fetch_order_by_key1"], "回读失败 ⇒ 一次写接口都不打")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)

    def test_status_out_without_a_link_is_skipped(self):
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])

    def test_status_out_failure_is_a_human_failure(self):
        self._link()
        self._set_status(OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream(status=ApiError("拒绝改状态", code=0))
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertNotIn("拒绝改状态", event.reason)


class ManualStatusPushTests(_Base):
    """⭐ 手动推送口径（2026-10-23，计划 §3）：只加一个 `manual` 开关，自动口径一个字不变。三件事各自能红：
    - `manual=True` 额外允许 `待验收` / `已完成` 两档（把这两档并进自动推送集也会让`StatusOutConsumeTests::test_non_pushable_statuses_are_skipped_without_touching_upstream` 红，那是**另一条**判据，两条各自独立）；
    - 自动路径（`manual` 缺省）对这两档仍 `skipped`（⛔ 不许因为"手动能用"就顺手放开自动）；- 手动把 `待验收` 推回去时，对方**已是已完成** ⇒ 终态跳过（防倒退）。"""

    def _set_status(self, status):
        """把这一单改成 `status`（与 `StatusOutConsumeTests` 的同名 helper 同形）。"""
        Order.objects.filter(pk=self.order.pk).update(status=status)

    def _manual_event(self):
        """排一条**手动**的 `member.status_out`（走真正的触发点，连 payload 一起验）。"""
        self.assertEqual(
            jidanpu_member.queue_status_out(self.order, manual=True), 1,
            "这条单没有链路 ⇒ 排不出事件，用例失败在前提上",
        )
        event = self._events_of_status_out().first()
        self.assertIsNotNone(event)
        event.refresh_from_db()
        # 写方真的把开关写进了 payload（⛔ 不是测试手工塞的）：下游口径靠它分流。
        self.assertIs(dict(event.payload or {}).get("manual"), True)
        return event

    def test_manual_push_allows_pending_review_and_completed(self):
        """`manual=True` ⇒ 额外允许**待验收 / 已完成**两档，标签**逐字**相同、事件收敛 `done`。
        判别力：把这两档从 `_manual_push_label` 里去掉 ⇒ 收到 `skipped`、上游一次都没打。"""
        self._link()
        for status, label in (
            (OrderStatus.PENDING_REVIEW, LABEL_PENDING_REVIEW),
            (OrderStatus.COMPLETED, LABEL_COMPLETED),
        ):
            with self.subTest(status=status):
                # 每一档自己一条事件（上一档的 done 不挡新触发）。
                JidanpuSyncEvent.objects.filter(
                    kind=jidanpu_member.KIND_STATUS_OUT, target_ref=self.order.order_no
                ).delete()
                self._set_status(status)
                event = self._manual_event()
                up = _Upstream()
                with _stub(up):
                    self.assertEqual(
                        jidanpu_member.consume_event(event, client=CLIENT), "done"
                    )
                sent = dict(up.calls)["set_order_status"]
                self.assertEqual(sent["remote_order_id"], JID)
                self.assertEqual(sent["status_label"], label)
                event.refresh_from_db()
                self.assertEqual(event.status, "done")

    def test_the_automatic_path_still_skips_pending_review_and_completed(self):
        """**自动路径**（`manual` 缺省）对这两档仍 `skipped` + 人话、**零上游写调用**。
        ⚠️ 走真正的触发点（`queue_status_out(order)`）而不是手造 payload：
        顺带钉住「自动路径**不写** `manual` 键」——写了就等于把自动口径悄悄换成手动。"""
        self._link()
        for status in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
            with self.subTest(status=status):
                JidanpuSyncEvent.objects.filter(
                    kind=jidanpu_member.KIND_STATUS_OUT, target_ref=self.order.order_no
                ).delete()
                self._set_status(status)
                self.assertEqual(jidanpu_member.queue_status_out(self.order), 1)
                event = self._events_of_status_out().first()
                event.refresh_from_db()
                self.assertNotIn(
                    "manual", dict(event.payload or {}),
                    "自动路径不许写 manual 开关（那会把自动口径切成手动）",
                )
                up = _Upstream()
                with _stub(up):
                    self.assertEqual(
                        jidanpu_member.consume_event(event, client=CLIENT), "skipped"
                    )
                self.assertEqual(up.names(), [], "自动路径这两档不许对对方发出任何写调用")
                event.refresh_from_db()
                self.assertEqual(event.reason, STATUS_NOT_PUSHABLE_REASON_LITERAL)

    def test_manual_pending_review_never_rolls_back_a_completed_remote(self):
        """防倒退（计划 §3）：目标 = 待验收、推前回读对方**已是已完成** ⇒ 终态 `skipped`。
        ⛔ 一次写都不打（只打了那一次推前回读）；原因逐字是「对方已是已完成，不往回改」。判别力：去掉这道闸 ⇒ 会真的把对方那张已完成的单改回「待验收」（`set_order_status`出现在调用清单里，本用例红）。"""
        self._link()
        self._set_status(OrderStatus.PENDING_REVIEW)
        event = self._manual_event()
        up = _Upstream(remote_sta=LABEL_COMPLETED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), ["fetch_order_by_key1"], "防倒退那一档只允许读、不许写")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, MANUAL_ROLLBACK_BLOCKED_REASON_LITERAL)

    def test_manual_completed_is_not_blocked_by_an_already_completed_remote(self):
        """对照：目标 = `已完成`、回读也是 `已完成` ⇒ 走「推前已是目标态 ⇒ done、零写入」。
        ⚠️ 有它才证明「防倒退」**只挡 `待验收` 那一档**，不是把「对方已完成」一律挡掉。"""
        self._link()
        self._set_status(OrderStatus.COMPLETED)
        event = self._manual_event()
        up = _Upstream(remote_sta=LABEL_COMPLETED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(up.names(), ["fetch_order_by_key1"])
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_manual_pending_review_never_rolls_back_a_refunded_remote(self):
        """⭐ 防倒退**第二档**（评审 Important-3 / 复评 Item 1）：远端**已退单** ⇒ 同样不往回改。
        ⛔ 一次写都不打（只打了那一次推前回读）；原因**逐字**是「对方已是已退单，不往回改」（⛔ 与「已完成」那句合成一句：两种现场要能分辨）。判别力：删掉 `_consume_status_out` 里 `current_token == jidanpu.ORDER_REFUNDED_LABEL`那一支 ⇒ 本用例红（会真的把对方那张已退单改回「待验收」，`set_order_status` 进入调用清单）。"""
        self._link()
        self._set_status(OrderStatus.PENDING_REVIEW)
        event = self._manual_event()
        up = _Upstream(remote_sta=LABEL_REFUNDED)
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), ["fetch_order_by_key1"], "防倒退那一档只允许读、不许写")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, MANUAL_ROLLBACK_REFUNDED_REASON_LITERAL)

    def test_manual_push_ignores_the_taker_store_gate(self):
        """⭐ 手动两档（待验收/已完成）**不受**接手人同店闸影响（2026-11-05 的闸只收自动口径，
        手动口径一个字不变）：有接手人且接手人不是本店的人 ⇒ 照旧推「待验收」。
        判别力：把闸挪成对 label 无差别套用 ⇒ 本用例红（手动推送被误拦）。"""
        other = self._other_binding(base_url=BASE_URL_B, enabled=True)
        Performance.objects.create(
            order=self.order, user=other.user, emp_type=EmpType.MAIN,
        )
        self._link()
        self._set_status(OrderStatus.PENDING_REVIEW)
        event = self._manual_event()
        up = _Upstream()
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        sent = dict(up.calls)["set_order_status"]
        self.assertEqual(sent["status_label"], LABEL_PENDING_REVIEW)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")


# ══ ⑦ 兜底与对照 ═══════════════════════════════════════════════════════════


class GuardAndControlTests(_Base):
    def test_an_event_pointing_at_a_deleted_binding_is_skipped(self):
        """绑定已不存在 ⇒ `skipped` + 人话原因，⛔ 不留在飞、⛔ 不静默。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            payload={"member_binding_id": self.binding.pk + 999}
        )
        event.refresh_from_db()
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertNotEqual(event.reason, "")

    def test_a_terminal_event_is_a_safe_no_op(self):
        event = self._event(jidanpu_member.KIND_LAUNCH)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(status="done")
        event.refresh_from_db()
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "done")
        self.assertEqual(up.names(), [])

    def test_the_stubs_bind_to_the_looked_up_module(self):
        """对照（E-0029）：**不接** `add_order` 的桩时，真实出口会被走到 ⇒ 桩打对了地方。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(lookup=self._launch_lookup())
        with (
            mock.patch.object(jidanpu, "fetch_sources", side_effect=up.fetch_sources),
            mock.patch.object(
                jidanpu, "fetch_order_by_key1", side_effect=up.fetch_order_by_key1
            ),
        ):
            # 真实 `jidanpu.add_order` 会调用假客户端的 `.call` ⇒ 假客户端没有它
            with self.assertRaises(AttributeError):
                jidanpu_member.consume_event(event, client=CLIENT)


# ══ ⑧ run_outbox：逐事件边界 / 退避 / 上限 / 健康列 ════════════════════════


class RunOutboxTests(_Base):
    def test_run_outbox_consumes_the_events_of_this_binding_only(self):
        mine = self._event(jidanpu_member.KIND_LAUNCH)
        other = self._other_binding(base_url=BASE_URL_B, enabled=True, username="member-c")
        theirs_order = Order.objects.create(
            game=self.game, order_no=ORDER_NO_2, status=OrderStatus.WAITING_GRAB,
            hall_price="9.00",
        )
        theirs = jidanpu_member.queue_member_event(
            theirs_order, other, kind=jidanpu_member.KIND_LAUNCH
        )
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual((stats.done, stats.failed, stats.skipped), (1, 0, 0))
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.status, "done")
        self.assertEqual(theirs.status, "pending")

    def test_run_outbox_with_nothing_to_do_does_not_open_a_connection(self):
        """没有在飞事件 ⇒ 一次连都不建（否则每次快路 tick 都白登录一遍上游）。"""
        with mock.patch.object(
            jidanpu, "member_session",
            side_effect=AssertionError("没有在飞事件时不该建连"),
        ):
            stats = jidanpu_member.run_outbox(self.binding, now=timezone.now())
        self.assertEqual(
            (stats.done, stats.failed, stats.skipped, stats.deferred, stats.abandoned),
            (0, 0, 0, 0, 0),
        )
        self.assertFalse(stats.paused)
        row = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
        self.assertIsNotNone(row.last_run_at)

    def test_run_outbox_stamps_health_rows_without_a_full_row_save(self):
        """⭐ 裁定 5：健康列**收窄写入** —— 同一行上别的列（凭据）一个都不许被覆盖。
        ⚠️ 判别力来自那个 `save` 探针（照快路同族用例的形状）：把 `_stamp_health` 的`.update(**fields)` 改回整行 `save()` ⇒ `saved` 非空 ⇒ 本用例红。只断「健康列写上了 + 凭据没变」是**假判据**：探针拿到的就是同一个内存实例，值本来就一样，整行 `save()` 也能全绿。"""
        self._event(jidanpu_member.KIND_LAUNCH)
        saved = []
        original = JidanpuMemberBinding.save

        def _spy(instance, *args, **kwargs):
            saved.append(instance.pk)
            return original(instance, *args, **kwargs)

        up = _Upstream(lookup=self._launch_lookup())
        with mock.patch.object(JidanpuMemberBinding, "save", _spy):
            with _stub(up):
                jidanpu_member.run_outbox(self.binding, now=timezone.now(), client=CLIENT)
        self.assertEqual(saved, [])
        row = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
        self.assertIsNotNone(row.last_run_at)
        self.assertIsNotNone(row.last_ok_at)
        self.assertEqual(row.last_error, "")
        self.assertIsNone(row.last_error_at)
        self.assertEqual(row.password, "pw-a")
        self.assertEqual(row.account, "acc-a")

    def test_run_outbox_stamps_last_error_on_a_failure(self):
        self._event(jidanpu_member.KIND_LAUNCH)
        up = _Upstream(add=ApiError("拒绝", code=0), lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual(stats.failed, 1)
        row = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
        self.assertNotEqual(row.last_error, "")
        self.assertIsNotNone(row.last_error_at)
        self.assertIsNone(row.last_ok_at)

    def test_run_outbox_waits_out_the_backoff(self):
        """退避窗内 ⇒ 一条都不打上游（spec §10：有上限的指数退避）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        now = timezone.now()
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=1, processed_at=now
        )
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(self.binding, now=now, client=CLIENT)
        self.assertEqual((stats.done, stats.deferred), (0, 1))
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)

    def test_run_outbox_consumes_again_once_the_backoff_has_passed(self):
        event = self._event(jidanpu_member.KIND_LAUNCH)
        now = timezone.now()
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=1, processed_at=now
        )
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=now + timedelta(hours=2), client=CLIENT
            )
        self.assertEqual(stats.done, 1)

    # ── ⭐ 单条事件处理总时限────────────────────────────────────
    def test_the_event_deadline_is_the_documented_value(self):
        """总时限的**契约值**（字面值，E-0024：⛔ 不从生产常量派生）。"""
        self.assertEqual(jidanpu_member.OUTBOX_EVENT_DEADLINE_SECONDS, 60)
        self.assertEqual(
            jidanpu_member.OUTBOX_EVENT_TIMEOUT_REASON.format(seconds=60),
            "单条事件处理超时（>60s）",
        )

    def test_a_slow_event_is_marked_failed_and_the_round_continues(self):
        """⭐ 单条事件超时 ⇒ 记 `failed` + 人话，**整轮继续**（⛔ 不占着整轮）。⛔ 弄红它：把 deadline 只记时间、不真的打断（或去掉外层包）⇒ 第一条会一路跑完（`done`），下面的 `failed` / 第二条 `done` 两条断言都会红。
        ⚠️ 用**可注入的假处理函数 sleep** 模拟卡死：真处理函数都在本用例的桩后面，这样超时点落在「处理函数内部」——那正是线上卡住的位置。"""
        slow = self._event(jidanpu_member.KIND_STATUS_OUT)

        good_order = Order.objects.create(
            game=self.game, order_no=ORDER_NO_2, status=OrderStatus.ISSUE,
            hall_price="9.00",
        )
        #: ⚠️ 远端单号用**桩认得的** `JID`（`_Upstream.fetch_order_by_key1` 的 `remote_sta`
        #: 那一支只按它回包）⇒ 第二条走真处理函数时能真的收敛成 `done`。
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=good_order,
            jidanpu_order_id=JID, in_hall=True,
        )
        fast = jidanpu_member.queue_member_event(
            good_order, self.binding, kind=jidanpu_member.KIND_STATUS_OUT
        )
        real = jidanpu_member._consume

        def _maybe_slow(event, **kwargs):
            if event.pk == slow.pk:
                time.sleep(2.0)
            return real(event, **kwargs)

        up = _Upstream()
        with _stub(up):
            with mock.patch.object(
                jidanpu_member, "_consume", side_effect=_maybe_slow,
            ):
                # ⚠️ `create=True`：改造前的红跑里这个常量还不存在，用例要红在断言上、
                #    而不是红在一个 import / 属性错误上（TDD 的 red 要能说明缺的是行为）。
                with mock.patch.object(
                    jidanpu_member, "OUTBOX_EVENT_DEADLINE_SECONDS", 0.5,
                    create=True,
                ):
                    stats = jidanpu_member.run_outbox(
                        self.binding, now=timezone.now(), client=CLIENT
                    )

        slow.refresh_from_db()
        fast.refresh_from_db()
        self.assertEqual(slow.status, "failed", "超时事件被当成成功/被跳过了")
        self.assertIn("单条事件处理超时", slow.reason)
        self.assertEqual(slow.attempts, 1, "超时也应当走既有的尝试计数")
        self.assertEqual(fast.status, "done", "超时那条把整轮占住了，后面那条没被处理")
        self.assertEqual((stats.failed, stats.done), (1, 1))

    def test_a_timed_out_event_still_follows_the_existing_attempt_cap(self):
        """⭐ 超时**不是另一套计数**：照既有「到 `OUTBOX_MAX_ATTEMPTS` 不再重试」。
        ⛔ 弄红它：给超时单独写一个 `failed` 而不走 `_retry` ⇒ 下次仍是同一档、`attempts`
        不涨；或者只涨计数但不追加上限那句 ⇒ 本用例红。"""
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        now = timezone.now()
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed",
            attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS - 1,
            reason="上一轮上游报错",
            processed_at=now - timedelta(days=1),
        )
        event.refresh_from_db()

        def _always_slow(*args, **kwargs):
            time.sleep(2.0)

        with mock.patch.object(jidanpu_member, "_consume", side_effect=_always_slow):
            with mock.patch.object(
                jidanpu_member, "OUTBOX_EVENT_DEADLINE_SECONDS", 0.5, create=True,
            ):
                stats = jidanpu_member.run_outbox(
                    self.binding, now=now, client=CLIENT
                )

        event.refresh_from_db()
        self.assertEqual(stats.failed, 1)
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, jidanpu_member.OUTBOX_MAX_ATTEMPTS)
        self.assertIn("单条事件处理超时", event.reason)
        self.assertIn("不再自动重试", event.reason)

        # 下一轮：到上限 ⇒ 走既有的 `abandoned` 档，一次都不再调用处理函数
        with mock.patch.object(
            jidanpu_member, "_consume", side_effect=AssertionError("到上限还在重试"),
        ):
            again = jidanpu_member.run_outbox(
                self.binding, now=now + timedelta(hours=2), client=CLIENT
            )
        self.assertEqual(again.abandoned, 1)

    def test_a_fast_event_is_not_touched_by_the_deadline(self):
        """默认路径：快事件照旧 `done`、`attempts` 不涨、不留失败原因（逐字不变）。"""
        self._link()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        event.refresh_from_db()
        self.assertEqual(stats.done, 1)
        self.assertEqual(event.status, "done")
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.reason, "")

    def test_run_outbox_gives_up_past_the_attempt_cap(self):
        """⭐ 失败契约：到上限**不再重试**、留台账待人看（⛔ 不无限重试）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed",
            attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
            reason="录单失败",
            processed_at=timezone.now() - timedelta(days=1),
        )
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual((stats.done, stats.abandoned), (0, 1))
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, jidanpu_member.OUTBOX_MAX_ATTEMPTS)

    def test_run_outbox_is_paused_when_the_binding_is_disabled(self):
        """spec §8.4：停用 ⇒ 在飞事件**不再重试**（留台账待人看）。"""
        event = self._event(jidanpu_member.KIND_LAUNCH)
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        self.binding.refresh_from_db()
        up = _Upstream(lookup=self._launch_lookup())
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertTrue(stats.paused)
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "pending")

    def test_one_bad_event_does_not_block_the_next(self):
        """⭐ 逐事件异常边界：一个坏事件不影响下一个；未预期异常**响亮抛**（⛔ 不伪装成业务失败）。
        ⚠️ 状态用**问题单**（推送集里的一档）。⛔ 别拿 `IN_PROGRESS` / **待验收** / **已完成** 来当「会炸的那一条」：业务方收窄后它们在**这一支**收敛成 `skipped`、一次上游都不打⇒ 本用例会以**错误的原因**变红。"""
        self._link(in_hall=True)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.ISSUE)
        bad = self._event(jidanpu_member.KIND_STATUS_OUT)
        good_order = Order.objects.create(
            game=self.game, order_no=ORDER_NO_2, status=OrderStatus.WAITING_GRAB,
            hall_price="9.00",
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=good_order, jidanpu_order_id="JDP-good-1",
            in_hall=True,
        )
        good = jidanpu_member.queue_member_event(
            good_order, self.binding, kind=jidanpu_member.KIND_WITHDRAW
        )
        up = _Upstream(
            lookup={"JDP-good-1": {"orderId": "JDP-good-1", "orderSta": LABEL_REFUNDED}},
        )
        with _stub(up):
            # 第一条（status_out）炸、第二条（withdraw）正常：桶够证明「一个坏不影响下一个」
            with mock.patch.object(
                jidanpu, "set_order_status",
                side_effect=[RuntimeError("SDK 内部炸了"), None],
            ):
                with self.assertRaises(
                    jidanpu_member.MemberOutboxUnexpectedFailures
                ) as ctx:
                    jidanpu_member.run_outbox(
                        self.binding, now=timezone.now(), client=CLIENT
                    )
        self.assertEqual(ctx.exception.stats.done, 1)
        bad.refresh_from_db()
        good.refresh_from_db()
        self.assertEqual(bad.status, "failed")
        self.assertIn("未预期异常", bad.reason)
        # ⛔ 异常正文不进台账（可能带上游回包 / 凭据片段，spec §8.5）：只留类型名
        self.assertNotIn("SDK 内部炸了", bad.reason)
        self.assertEqual(bad.attempts, 1)
        self.assertEqual(good.status, "done")

    def test_an_unexpected_failure_still_stamps_the_binding_error(self):
        """⚠️ 同上：要用**推送集里**的一档状态（`IN_PROGRESS` / 待验收 / 已完成现在收敛成
        `skipped`，一次上游都不打 ⇒ 拿它们炸不了）。
        """
        self._link(in_hall=True)
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.ISSUE)
        self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream(status=RuntimeError("SDK 内部炸了"))
        with _stub(up):
            with self.assertRaises(jidanpu_member.MemberOutboxUnexpectedFailures):
                jidanpu_member.run_outbox(
                    self.binding, now=timezone.now(), client=CLIENT
                )
        row = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
        self.assertNotEqual(row.last_error, "")
        self.assertIsNotNone(row.last_error_at)


# ══ ⑧b 按绑定的**串行闸**════════


@contextmanager
def _other_db_connection():
    """**另一条**到同一（测试）库的连接：只为「让另一个会话持有 advisory lock」。⚠️ 为什么必须真开一条：`django.db.connections` 按 alias 管，同一 alias 只有一条连接，而在同一连接上重复加同一把 advisory lock 是**可重入**的（总能拿到）⇒ 测不出「别人占着」。
    ⚠️ 参数从**当前连接**取（`get_connection_params`）⇒ 自动指向测试库（`test_<POSTGRES_DB>`），⛔ 不写死库名；退出时一定关连接（锁随会话结束释放，但留着连接会占 PostgreSQL 的 `max_connections`）。"""
    params = connection.get_connection_params()
    kwargs = {
        key: params[key]
        for key in ("dbname", "user", "password", "host", "port")
        if key in params
    }
    conn = psycopg.connect(**kwargs, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


class OutboxSerialisationTests(_Base):
    """同一绑定的两轮 outbox 不许并发消费（评审 Blocking-1）。
    背景：`jidanpu_sync` 每 3 秒对同一绑定跑 `run_outbox`，而 web 手动推送也会跑一次⇒ 取事件没有 `FOR UPDATE`、`_finish` / `_retry` 又是无条件 pk 更新 ⇒ 同一事件会被消费两次（record 并发发两次 `CreateOrder`；后写者把 `done` 改回 `failed`）。⇒ 闸门放在**共享引擎** `run_outbox` 里（⛔ 不加在 web 端点）。
    ⚠️ **如实登记**：这里覆盖「拿不到闸 ⇒ 回人话且一个事件都不动」「跑完锁释放」「另一个**连接**占着锁 ⇒ 被挡」三条；**没有**覆盖「两个线程真的同时跑同一绑定」的真实竞争窗口（那需要并行线程 + 连接池，测试库里按线程各开一条连接的代价与不确定性都大）。闸门本身的判据是 PostgreSQL 的 advisory lock —— 上面第三条已经用**第二条连接**证明了「锁跨连接可见」，这比同连接重入更能说明问题。"""

    @staticmethod
    def _advisory_locks_of_this_session() -> int:
        """当前连接持有的 advisory lock 条数（**锁泄漏**的判据；正常跑完必须是 0）。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
            return cursor.fetchone()[0]

    def test_the_lock_namespace_is_pinned(self):
        """命名空间是**锁的身份**：改它等于换一把锁（滚动发布时新老进程各锁一把）。
        ⚠️ 钉**字面值**（E-0024）：拿常量跟它自己比是自指的，改坏了不会红。"""
        self.assertEqual(jidanpu_member.OUTBOX_LOCK_NAMESPACE, 0x4F555442)

    def test_a_round_that_cannot_take_the_guard_returns_a_human_reason(self):
        """拿不到串行闸 ⇒ `blocked_reason` 是那句人话、**一个事件都不取 / 不动**。"""
        event = self._event(jidanpu_member.KIND_STATUS_OUT)

        @contextmanager
        def _no_lock(binding):
            yield False

        with mock.patch.object(jidanpu_member, "_outbox_guard", _no_lock):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual(stats.blocked_reason, OUTBOX_BUSY_REASON_LITERAL)
        self.assertEqual((stats.done, stats.failed, stats.skipped), (0, 0, 0))
        event.refresh_from_db()
        self.assertEqual(event.status, "pending", "拿不到闸 ⇒ 事件必须原样留在 pending")
        self.assertEqual(event.attempts, 0, "拿不到闸 ⇒ 不许计一次尝试（那不是失败）")

    def test_the_guard_is_released_after_a_round_and_the_next_round_can_enter(self):
        """正常跑完 ⇒ 锁**已经释放**（`pg_locks` 为 0），且第二轮能进（⛔ 不是「锁泄漏 + 重入」）。"""
        self._link()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.ISSUE)
        self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream()
        with _stub(up):
            first = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual(first.blocked_reason, "")
        self.assertEqual(first.done, 1)
        self.assertEqual(
            self._advisory_locks_of_this_session(), 0, "这一轮结束后锁必须已经释放"
        )

        # 第二轮：同一绑定再来一条（`done` 不挡新触发）⇒ 拿得到闸、真的消费掉。
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.PAUSED)
        second_event = self._event(jidanpu_member.KIND_STATUS_OUT)
        up2 = _Upstream()
        with _stub(up2):
            second = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual(second.blocked_reason, "")
        self.assertEqual(second.done, 1)
        second_event.refresh_from_db()
        self.assertEqual(second_event.status, "done")

    def test_the_guard_is_released_when_the_round_raises(self):
        """⭐ 复评 Item 4：一轮**抛异常**之后锁也必须已经释放（`finally` 的判据）。
        走既有的「未预期异常」那条路：`set_order_status` 抛 `RuntimeError` ⇒ 逐事件边界把它记成`failed`、整轮跑完再抛 `MemberOutboxUnexpectedFailures`。锁若不释放，连接就会被这一行永久卡住（连接池里的公共资源），而**下一个**手动推送 / 调度拍都再也进不来。判别力：把 `_outbox_guard` 的解锁从 `finally` 挪到正常返回之后 ⇒ 本用例红（计数 = 1）。"""
        self._link()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.ISSUE)
        self._event(jidanpu_member.KIND_STATUS_OUT)
        up = _Upstream(status=RuntimeError("SDK 内部炸了"))
        with _stub(up):
            with self.assertRaises(jidanpu_member.MemberOutboxUnexpectedFailures):
                jidanpu_member.run_outbox(
                    self.binding, now=timezone.now(), client=CLIENT
                )
        self.assertEqual(
            self._advisory_locks_of_this_session(), 0, "抛异常之后锁也必须已经释放"
        )

    def test_a_lock_held_by_another_session_blocks_the_round(self):
        """⭐ 真·跨连接：**另一条会话**占着 `(ns, binding.pk)` ⇒ 本轮被挡；放掉之后能进。
        为什么这条比「同连接连跑两轮」强：同一连接上 advisory lock 是**可重入**的
        ⇒ 锁没释放也照样能拿到；只有第二条连接才能证明「锁真的被这个 key 挡住」。"""
        self._link()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.ISSUE)
        event = self._event(jidanpu_member.KIND_STATUS_OUT)
        with _other_db_connection() as other:
            other.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                (jidanpu_member.OUTBOX_LOCK_NAMESPACE, self.binding.pk),
            )
            blocked = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
            self.assertEqual(blocked.blocked_reason, OUTBOX_BUSY_REASON_LITERAL)
            event.refresh_from_db()
            self.assertEqual(event.status, "pending")
            other.execute(
                "SELECT pg_advisory_unlock(%s, %s)",
                (jidanpu_member.OUTBOX_LOCK_NAMESPACE, self.binding.pk),
            )
        # 对照：锁放掉之后**同一个事件**真的能被消费（否则「被挡」可能只是别的原因）。
        up = _Upstream()
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )
        self.assertEqual(stats.blocked_reason, "")
        self.assertEqual(stats.done, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, "done")


# ══ ⑨ 跨任务：换绑收敛（T3）× outbox 取活 / 去重（T5）（控制器裁定 7）══════════


class RebindThenReenableTests(TenantTestCase):
    """⭐ 换绑收敛的终态必须是 `skipped`，⛔ 不是 `failed`（控制器裁定 7）。`failed` 是「还会重试」的态：绑定被**重新启用**后 `run_outbox` 会拿**新凭据**重试那些旧事件——正是 spec §8.4 明令禁止的「用新凭据去改旧单」；而且那条 `failed` 行还会挡住同一张单再次触发（去重集合含 `pending` / `failed`）。`skipped` 是既有那条线里「作废、不再重试」的口径 ⇒ 两条毛病一起没有。
    ⚠️ 这里走**真实的换绑 PUT**（不是直接调私有收敛助手）：它同时证明端点确实接上了那次收敛。"""

    def setUp(self):
        super().setUp()
        self.client_http = make_logged_in_client(username=ACCOUNT)
        self.user = User.objects.get(username=ACCOUNT)
        self.game = Game.objects.create(name="合成游戏甲")
        self.binding = self._put_binding(account="acc-a")
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB,
            hall_price="12.34", total_price="50.00", project_text="合成项目甲",
        )

    def _put_binding(self, *, account, enabled=False):
        payload = {
            "jidanpu_base_url": BASE_URL,
            "account": account,
            "password": "pw-a",
            "source_name": "团团通系统",
            "enabled": enabled,
        }
        res = self.client_http.put(
            BINDING_URL, json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(res.json()["code"], 0, res.content)
        return JidanpuMemberBinding.objects.get(user=self.user)

    def test_rebinding_then_reenable_never_retries_and_does_not_block_new_events(self):
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertEqual(event.status, "pending")  # 构造物：确实有一条在飞

        # ① 换绑（改 account）⇒ 收敛成终态 `skipped`
        self.binding = self._put_binding(account="acc-a2")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertNotEqual(event.reason, "")
        self.assertIn("人工核对", event.reason)
        self.assertEqual(event.attempts, 0)  # 收敛不是「失败」，⛔ 不涨 attempts

        # ② 绑定**重新启用**（直接改开关：启用路径的检测由 T3 的用例管，这里只验消费侧）
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=True)
        self.binding.refresh_from_db()

        up = _Upstream(
            lookup={
                ORDER_NO: {"orderId": JID, "orderIdView": ORDER_NO},
                JID: {"orderId": JID, "isThrowOrder": "Y"},
            }
        )
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=timezone.now(), client=CLIENT
            )

        # ③ **零**上游调用（用桩的调用集合断言，⛔ 不是「没报错」）
        self.assertEqual(up.names(), [])
        self.assertEqual((stats.done, stats.failed), (0, 0))
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.attempts, 0)

        # ④ `skipped` 不挡在飞判定：同一张单**再次触发**能新建一条 `pending`
        again = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_LAUNCH
        )
        self.assertNotEqual(again.pk, event.pk)
        self.assertEqual(again.status, "pending")


# ══ ⑩ ①式办不了「改项目」那一档：终态跳过 + 一句人话（⛔ 不静默）══════════
# 背景：新增的 `member.project`（我方改「接手项目」⇒ 改对方那张单的项目名称）在②式那条路上已经通了；①式（账号 + 密码登录）**没有经过实测的「改对方项目名称」写接口**（候选端点 `/OrderAjax/ChangeOrderProject` 在清单里只到「读码」级证据、连 HTTP 方法都未定）⇒ 它把这一档登记进 `OutboxLine.unsupported`：消费时**终态跳过 + 一句人话**。
# ⛔ 纪律：不许静默（那会让「改了项目没同步过去」变成一件没人知道的事），⛔ 也不许记成 `failed`（那会按退避表反复重试、白烧上游额度）。⚠️ **既有六档的行为一个字都没改**：本文件的其余用例（那六档）**一条都没动**。

#: 新 kind 的**字面值**（⛔ 不从被测常量派生：自指断言在被断言的常量改坏时**不会红**）。
KIND_PROJECT_LITERAL = "member.project"


class ProjectKindOnTheLegacyLineTests(_Base):
    def test_the_kind_is_really_in_the_outbox_scan_set(self):
        """⭐ 「`run_outbox` 真的会取到它」：不在 `OUTBOX_KINDS` 里 ⇒ 那一条永远取不到（静默不消费）。
        ⚠️ 字面值（⛔ 不写 `模块.KIND_PROJECT == 模块.KIND_PROJECT` 那种自指断言）。"""
        self.assertIn(KIND_PROJECT_LITERAL, jidanpu_member.OUTBOX_KINDS)
        self.assertEqual(jidanpu_member.KIND_PROJECT, KIND_PROJECT_LITERAL)

    def test_the_legacy_line_skips_it_with_human_words_and_without_any_upstream(self):
        """⭐⭐ 判据：①式命中这一档 ⇒ **终态 `skipped` + 一句人话**，⛔ 一次上游都不打。⚠️ 事件由**真触发点**排出来（`queue_project_update`，⛔ 不手搓行）：①式绑定 + 已有链路⇒ 触发点**照排**（它不按 mode 分流，那是第二处判据），「怎么办」由差异面说了算—— 这正是 `unsupported` 那张表的用途。
        ⚠️ `attempts == 0`：它是**终态**，⛔ 不按退避表反复重试（那会白烧上游额度）。⚠️ 人话里必须点出**两件事**（缺一件就等于"说不清为什么没做"）：**候选端点是谁** + **为什么没接**（方法/参数/成功判据都没实测过）。判别力（三条都能红）：
        ① 把 `_MEMBER_LINE` 的 `unsupported` 那条删掉 ⇒ 本用例红（引擎会走到 `_project_of` ⇒ `line.project is None` ⇒ 响亮抛，而真实世界里那会变成一条「未预期异常」台账）；② 把原因文案换成空串 ⇒ `_unsupported_reason` 当场响亮抛（说不清为什么 = 最恨的形态）⇒ 本用例红；③ 把这一档记成 `failed`（而不是 `skipped`）⇒ 状态断言红。"""
        self._link()
        self.assertEqual(jidanpu_member.queue_project_update(self.order), 1)
        event = self._event(jidanpu_member.KIND_PROJECT)
        up = _Upstream()

        with _stub(up):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")

        #: ⛔ **一次上游都不打**（那六档的桩出口一个都没被碰）。
        self.assertEqual(up.names(), [])
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.attempts, 0)
        #: ⭐ 人话：候选端点 + 为什么没接。
        self.assertIn("ChangeOrderProject", event.reason)
        self.assertIn("没实测", event.reason)
        #: ⚠️ 而**链路行一个字节都没动**（这一档压根没写上游）。
        link = JidanpuMemberOrderLink.objects.get(binding=self.binding, order=self.order)
        self.assertTrue(link.in_hall)
