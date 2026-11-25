"""`GET /api/audit/logs/` 与 `GET /api/audit/actors/` 的接口契约（spec §6.1 / §6.3 / §10.6）。

    ⚠️ **路径带尾斜杠**（R18）：spec §6.1 原文写的是不带斜杠，R18 统一成
    `/api/audit/logs/`（理由与先例见 `apps/audit/urls.py` 的 docstring）；§6.3 的
    `/api/audit/actors/` 同样带尾斜杠。

    ⚠️ **测试自己造行**：这里一律直接建 `OperationLog` 行，不依赖业务端点顺手写日志
    （否则本文件的绿会取决于别的写入方）。

    ⚠️ **行的标识用 `summary`**：它是显示字段，取值完全由测试控制。载荷里还有
    `order_no` 与 `source_order_no`（来源单号优先、内置单号兜底），不用它们当行标识。

    判据纪律（Global Constraints）：期望值写字面值（E-0024）；否定断言前先给**阳性对照**，
    否则空列表能让 `assertNotIn` 空洞通过。
"""
import warnings
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.permissions import is_account_admin
from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

ENDPOINT = "/api/audit/logs/"
ACTORS_ENDPOINT = "/api/audit/actors/"

#: `Asia/Shanghai` 无夏令时 ⇒ 固定 `+08:00` 与 `ZoneInfo("Asia/Shanghai")` 是**同一瞬时**，
#: 且不依赖本机 tzdata。
CST = dt_timezone(timedelta(hours=8))


def local(*args):
    """按本地时区（Asia/Shanghai）构造一个瞬时。"""
    return datetime(*args, tzinfo=CST)


def summaries(data):
    """响应 `data` 里每一行「操作说明」列的值——测试的行标识（见模块 docstring）。"""
    return [row["summary"] for row in data["items"]]


class LogApiTestCase(TenantTestCase):
    """公共夹具：一个持有 `system.logs` 节点的真实登录 client。

    ⚠️ **只建这一个节点，不 `seed_catalog()`**：本文件的判据只关心 `system.logs` 一条；
    灌整个目录会在每个用例的 `setUp` 里多跑几百次 upsert，而它证明不了任何本任务的事。
"""

    def setUp(self):
        node = PermissionNode.objects.create(
            benchmark_id=108, code="system.logs", name="系统日志",
            note="系统日志列表", url="/System/SystemLog",
            kind=NodeKind.PAGE, level=1,
        )
        self.client = make_logged_in_client(username="log-viewer")
        role = Role.objects.create(
            name="日志管理员", group=Group.objects.create(name="日志管理员")
        )
        role.permissions.set([node])
        get_user_model().objects.get(username="log-viewer").groups.add(role.group)

    def _get(self, **params):
        response = self.client.get(ENDPOINT, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def _data(self, **params):
        body = self._get(**params)
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    @staticmethod
    def _log(**overrides):
        fields = {
            "log_type": 10,
            "action": "order.transition",
            "actor": "甲客服",
            "order_no": "A000000000000000001",
            "game_name": "原神",
            "source_order_no": "TB-1",
            "game_role_name": "旅行者",
            "target_id": "42",
            "summary": "把订单的状态从代练中改为待验收",
            # 历史行可能没有 `actor_id`（spec §6.3 / §11.1）；测试需要显式控制它，
            # 所以默认值也写进字段表，由 `overrides` 覆盖（与其余字段同一口径）。
            "actor_id": None,
        }
        fields.update(overrides)
        return OperationLog.objects.create(**fields)


class OperationLogRowShapeTests(LogApiTestCase):
    def test_row_carries_the_eight_display_fields_plus_action(self):
        """§3 的 8 个对标列 + `action`（后续任务与重放测试要读它）+ `actor_display`。

    ⚠️ `actor_display` 是**只读显示派生字段**（spec §11.1）：不落库、不参与筛选，
    只是把「操作用户」列要显示的昵称交给前端。恰好集合断言按新事实更新，
    不放宽成 `assertIn`——多一个字段就是一次 PII 泄漏面的扩大。
    ⛔ **D-0341）**：集合补 `order_no`（内置单号原值，前端兜底的
    取数前提）——见文件头的更正块。
"""
        self._log()
        items = self._data()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(
            set(items[0]),
            {
                "target_id", "log_type", "source_order_no", "game_role_name",
                "game_name", "summary", "actor", "created_at", "action",
                "actor_display",
                "order_no",
            },
        )

    def test_delete_reason_is_opt_in_and_limited_to_delete_rows(self):
        self._log(action="order.delete", reason="测试删除原因")
        self._log(action="order.transition", reason="测试状态原因")
        self.assertTrue(all("delete_reason" not in row for row in self._data()["items"]))
        rows = self._data(include_delete_reason="true")["items"]
        self.assertEqual(
            {row["action"]: row["delete_reason"] for row in rows},
            {"order.delete": "测试删除原因", "order.transition": ""},
        )

    def test_rows_come_back_newest_first_with_local_timestamps(self):
        self._log(summary="早的那条", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(summary="晚的那条", created_at=local(2026, 9, 21, 10, 0, 0))
        items = self._data()["items"]
        self.assertEqual([i["summary"] for i in items], ["晚的那条", "早的那条"])
        # 时间戳按**本地时区**渲染（`Asia/Shanghai` = +08:00），与对标「操作时间」列同形
        self.assertEqual(items[0]["created_at"], "2026-09-21T10:00:00+08:00")


class SourceOrderNoDisplayTests(LogApiTestCase):
    """「来源订单号」列的取值口径（D-0341，业务方 2026-10-30）。

    页面列显示 = `source_order_no`（日志快照 → 订单当前值回填）优先，
    空了由**前端**拿 `order_no`（内置单号原值）兜底——载荷只给两个原值，
    不在载荷里拼展示值（D-0301 的分工，与 `OrderActionLogRowSerializer` 的 D7 同族）。
    所以这里的判据是「两个键各自回什么原值」，不是「列上最终显示什么」。
"""

    ORDER_NO = "20261030000000000001"

    def _order(self, *, order_no=None, source_order_no=""):
        game = Game.objects.create(name="测试游戏甲")
        return Order.objects.create(
            game=game,
            order_no=order_no or self.ORDER_NO,
            source_order_no=source_order_no,
        )

    def _row(self, **overrides):
        rows = self._data(**overrides)["items"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_snapshot_and_builtin_are_both_carried_verbatim(self):
        """日志列有来源号 ⇒ 原样透出；`order_no` 是内置单号原值（前端兜底的取数前提）。"""
        self._log(order_no=self.ORDER_NO, source_order_no="TB-SNAPSHOT-1")
        row = self._row()
        self.assertEqual(row["source_order_no"], "TB-SNAPSHOT-1")
        self.assertEqual(row["order_no"], self.ORDER_NO)

    def test_empty_snapshot_backfills_from_current_order(self):
        """日志快照空 + 订单在、有来源号 ⇒ 回填订单**当前**值（D7；多数订单类日志走这条）。"""
        self._order(source_order_no="CUR-SRC-1")
        self._log(order_no=self.ORDER_NO, source_order_no="")
        row = self._row()
        self.assertEqual(row["source_order_no"], "CUR-SRC-1")
        self.assertEqual(row["order_no"], self.ORDER_NO)

    def test_snapshot_wins_over_current_order_value(self):
        """D7 的优先级：日志列**有值**就用日志列的（记下的那一刻优先），不回查。"""
        self._order(source_order_no="CUR-NEW-1")
        self._log(order_no=self.ORDER_NO, source_order_no="SNAPSHOT-OLD")
        row = self._row()
        self.assertEqual(row["source_order_no"], "SNAPSHOT-OLD")

    def test_no_source_anywhere_leaves_source_empty_but_builtin_still_carried(self):
        """两路都拿不到来源号（快照空 + 查无此单）⇒ 来源号如实回空串、内置号**仍在载荷里**
    （前端兜底靠它，不能变成缺键）。查无此单用**不存在的单号**当阳性对照的反面。"""
        self._log(order_no=self.ORDER_NO, source_order_no="")
        row = self._row()
        self.assertEqual(row["source_order_no"], "")
        self.assertEqual(row["order_no"], self.ORDER_NO)


class OperationLogFilterTests(LogApiTestCase):
    """6 个筛选逐个：造两条**只差被筛字段**的行，断言筛出来的**恰好**是一条。"""

    def test_stime_etime_are_interpreted_in_the_local_timezone(self):
        """跨 UTC 日界的行必须按**本地日**选中（spec §10.6-②，评审 I7①）。

    本地 2026-09-21 00:30 = UTC 2026-09-20 16:30 ⇒ 落在**前一个 UTC 日**。
    把 `stime` 按 UTC 拼 aware（`make_aware(..., timezone.utc)`）会整体偏 8 小时：
    `stime` 会把这一行漏掉，`etime` 会多收次日凌晨的行。

    ⚠️ **另外断言「没有 naive datetime 警告」**：Django 5.2 对 naive 值的兜底是
    「按 `get_default_timezone()`（= `Asia/Shanghai`）补 aware + 写一条
    `RuntimeWarning`」（`DateTimeField.get_prep_value`）——瞬时**恰好是对的**，
    所以只靠值断言分辨不出「裸 `strptime`」这一版实现（实测：探针输出
    `2026-09-21T00:00:00+08:00` + 一条 naive 警告）。这条断言把那个语义
    从「框架的向后兼容兜底」拉回「我们自己的序列化器」。
"""
        self._log(summary="本地 21 日凌晨", created_at=local(2026, 9, 21, 0, 30, 0))
        # 前一个本地日（同一 UTC 日、更晚）——不该被选中
        self._log(summary="本地 20 日深夜", created_at=local(2026, 9, 20, 23, 30, 0))
        # 后一个本地日（更早的 UTC 日）——不该被选中
        self._log(summary="本地 22 日凌晨", created_at=local(2026, 9, 22, 0, 30, 0))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            body = self._get(stime="2026-09-21 00:00:00", etime="2026-09-21 23:59:59")
        self.assertEqual(
            [str(w.message) for w in caught if "naive datetime" in str(w.message)], []
        )
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual(summaries(data), ["本地 21 日凌晨"])
        self.assertEqual(data["total"], 1)

    def test_etime_selects_only_up_to_that_instant(self):
        self._log(summary="上午那条", created_at=local(2026, 9, 21, 10, 0, 0))
        self._log(summary="中午那条", created_at=local(2026, 9, 21, 12, 0, 0))
        data = self._data(etime="2026-09-21 11:00:00")
        self.assertEqual(summaries(data), ["上午那条"])

    def test_stime_and_etime_are_inclusive_at_both_ends(self):
        """区间两端都是**闭**的（`__gte` / `__lte`）。

    ⚠️ 补的是**判别力缺口**：其余用例的行都离边界 ≥30 分钟，于是
    `gte→gt` / `lte→lt` 的变异能在整份文件里存活。这里造两条**恰好落在**
    `stime` / `etime` 上的行；另加两条各差 1 秒的对照，防「把区间整个丢掉」
    的实现也能绿（那会返回 4 条）。
"""
        self._log(summary="正好在起点", created_at=local(2026, 9, 21, 10, 0, 0))
        self._log(summary="正好在终点", created_at=local(2026, 9, 21, 12, 0, 0))
        self._log(summary="起点前一秒", created_at=local(2026, 9, 21, 9, 59, 59))
        self._log(summary="终点后一秒", created_at=local(2026, 9, 21, 12, 0, 1))
        data = self._data(stime="2026-09-21 10:00:00", etime="2026-09-21 12:00:00")
        self.assertEqual(sorted(summaries(data)), sorted(["正好在起点", "正好在终点"]))
        self.assertEqual(data["total"], 2)

    def test_empty_stime_is_the_same_as_not_provided(self):
        self._log(summary="唯一的一条", created_at=local(2026, 9, 21, 10, 0, 0))
        with_empty = self._data(stime="", etime="", game_name="", order_no="")
        without = self._data()
        self.assertEqual(with_empty, without)
        self.assertEqual(summaries(with_empty), ["唯一的一条"])

    def test_log_type_zero_is_the_same_as_not_provided(self):
        self._log(summary="订单类", log_type=10)
        self._log(summary="用户类", log_type=30)
        zero = self._data(log_type=0)
        absent = self._data()
        self.assertEqual(zero, absent)
        self.assertEqual(zero["total"], 2)

    def test_log_type_selects_only_that_category(self):
        self._log(summary="订单类", log_type=10)
        self._log(summary="用户类", log_type=30)
        items = self._data(log_type=10)["items"]
        self.assertEqual(
            [(i["summary"], i["log_type"]) for i in items],
            [("订单类", 10)],
        )

    def test_game_name_matches_the_whole_value_not_a_substring(self):
        """`原神` 不该命中 `原神国际服`——**精确匹配**。

    ⚠️ 「精确」是**本实现的决定**，不是 spec 原文：§6.1 只把 `game_name` 类型化成
    「字符串」，没有钉匹配语义（`order_no` 同样是这里决定的精确匹配）。
"""
        self._log(summary="国服那条", game_name="原神")
        self._log(summary="国际服那条", game_name="原神国际服")
        items = self._data(game_name="原神")["items"]
        self.assertEqual(
            [(i["summary"], i["game_name"]) for i in items],
            [("国服那条", "原神")],
        )

    def test_actor_matches_one_actor(self):
        self._log(summary="甲做的事", actor="甲客服")
        self._log(summary="乙做的事", actor="乙客服")
        items = self._data(actor="甲客服")["items"]
        self.assertEqual(
            [(i["summary"], i["actor"]) for i in items],
            [("甲做的事", "甲客服")],
        )

    def test_repeated_actor_parameters_return_the_union_and_exclude_a_third(self):
        self._log(summary="甲做的事", actor="甲客服")
        self._log(summary="乙做的事", actor="乙客服")
        self._log(summary="丙做的事", actor="丙客服")
        data = self._data(actor=["甲客服", "乙客服"])
        self.assertEqual(sorted(summaries(data)), ["乙做的事", "甲做的事"])
        self.assertEqual(data["total"], 2)

    def test_comma_separated_actor_value_returns_the_union(self):
        """前端 `xm-select` 多选两种提交形状都要认（spec §6.1）。"""
        self._log(summary="甲做的事", actor="甲客服")
        self._log(summary="乙做的事", actor="乙客服")
        self._log(summary="丙做的事", actor="丙客服")
        data = self._data(actor="甲客服,乙客服")
        self.assertEqual(sorted(summaries(data)), ["乙做的事", "甲做的事"])
        self.assertEqual(data["total"], 2)

    def test_order_no_matches_exactly(self):
        self._log(summary="第一单", order_no="A000000000000000001")
        self._log(summary="第二单", order_no="A000000000000000002")
        items = self._data(order_no="A000000000000000001")["items"]
        self.assertEqual([i["summary"] for i in items], ["第一单"])

    def test_key_filters_on_a_summary_substring(self):
        self._log(summary="把订单的状态从代练中改为待验收")
        self._log(summary="修改接单价：88.00 -> 66.00")
        items = self._data(key="接单价")["items"]
        self.assertEqual([i["summary"] for i in items], ["修改接单价：88.00 -> 66.00"])

    def test_key_searches_only_the_summary_column(self):
        """`key` 只打在 `summary` 上（spec §6.1）——不能顺手把 `actor` / `order_no` 也 OR 进来。

    ⚠️ 补的是**判别力缺口**：上面那条用例里两行的 `actor` / `order_no` 都不是被查的串，
    于是「`key` OR 到别的列」的实现也能全绿。这里让 `key` **恰好等于一行的 `actor`**，
    而两行的 `summary` 都不含它 ⇒ 命中必须是 0；再给一条 summary 命中的**阳性对照**，
    证明这个 0 不是「筛选根本没生效」。
"""
        self._log(summary="第一条操作说明", actor="独特的人名")
        self._log(summary="第二条操作说明", actor="另一个人")
        data = self._data(key="独特的人名")
        self.assertEqual(summaries(data), [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(summaries(self._data(key="第一条")), ["第一条操作说明"])


class OperationLogPaginationTests(LogApiTestCase):
    def test_payload_is_the_project_pagination_shape_and_page_size_is_honoured(self):
        for n in (1, 2, 3):
            self._log(summary=f"第 {n} 条")
        data = self._data(page=2, page_size=2)
        self.assertEqual(set(data), {"items", "total", "page", "page_size"})
        self.assertEqual(
            (data["total"], data["page"], data["page_size"], len(data["items"])),
            (3, 2, 2, 1),
        )

    def test_page_size_is_capped_at_300(self):
        OperationLog.objects.bulk_create(
            OperationLog(log_type=10, action="order.transition", actor="甲", summary=f"第 {n} 行")
            for n in range(301)
        )
        data = self._data(page_size=1000)
        self.assertEqual(data["page_size"], 300)
        self.assertEqual(data["total"], 301)
        self.assertEqual(len(data["items"]), 300)


class OperationLogOrderingTests(LogApiTestCase):
    """`ordering` 参数（spec §6.1 / §16.2 第 1 条，2026-09-22）。

    判据是**行级顺序**且期望值写**字面值**（E-0024）：不能拿代码里的词表常量当期望，
    否则常量被改坏时断言自指、不会红。

    ⚠️ **稳定 tie-break 的判别力**靠 `test_ties_on_created_at_are_stable_in_both_directions`：
    同一 `created_at` 的多行只按时间排时数据库不保证次序 ⇒ 必须由第二个排序键（`id`）定序。
    该用例在**去掉 tie-break 的变异**下必须变红（变异日志见 Task O 报告）。
"""

    def test_created_at_ascending_is_oldest_first(self):
        """升序 = 旧在前（字面值，不是拿常量对拍）。"""
        self._log(summary="早的", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(summary="中的", created_at=local(2026, 9, 21, 10, 0, 0))
        self._log(summary="晚的", created_at=local(2026, 9, 21, 11, 0, 0))
        data = self._data(ordering="created_at")
        self.assertEqual(summaries(data), ["早的", "中的", "晚的"])

    def test_minus_created_at_is_newest_first(self):
        """降序 = 新在前（字面值）。"""
        self._log(summary="早的", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(summary="中的", created_at=local(2026, 9, 21, 10, 0, 0))
        self._log(summary="晚的", created_at=local(2026, 9, 21, 11, 0, 0))
        data = self._data(ordering="-created_at")
        self.assertEqual(summaries(data), ["晚的", "中的", "早的"])

    def test_absent_ordering_is_byte_identical_to_the_explicit_default(self):
        """不传 `ordering` 与显式 `-created_at` **同行同序**（缺省行为与今天逐字节相同）。"""
        self._log(summary="早的", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(summary="晚的", created_at=local(2026, 9, 21, 10, 0, 0))
        absent = self._data()
        explicit = self._data(ordering="-created_at")
        self.assertEqual(absent, explicit)
        # 再给一个**字面**断言：证明上面那个相等不是「两边都空」。
        self.assertEqual(summaries(absent), ["晚的", "早的"])

    def test_empty_ordering_is_the_same_as_not_provided(self):
        """`?ordering=`（空串）按「没传」处理——与既有 `stime=` / `log_type=0` 同口径。"""
        self._log(summary="唯一的一条", created_at=local(2026, 9, 21, 10, 0, 0))
        self.assertEqual(self._data(ordering=""), self._data())

    def test_ties_on_created_at_are_stable_in_both_directions(self):
        """同一 `created_at` 的多行在**两向**都按 `id` 定序，且重复请求完全一致。

    样本：6 行同一 `created_at`（`id` 递增 = 「同刻-1」…「同刻-6」）+ 两行不同时刻
    （逼出真正的排序节点，别让「全是并列」被实现顺手按物理顺序返回）。

    - 升序 tie-break = `id` ⇒ 旧在前，并列组内 `同刻-1` → `同刻-6`；
    - 降序 tie-break = `-id` ⇒ 并列组内 `同刻-6` → `同刻-1`。

    ⚠️ **重复请求 3 次**：并列行的次序在缺 tie-break 时可能「这一次恰好对」，
    所以既钉**字面**期望、也钉**多次一致**（去掉 tie-break 的变异必须让本用例变红）。
"""
        self._log(summary="最新", created_at=local(2026, 9, 21, 12, 0, 0))
        for n in range(1, 7):
            self._log(summary=f"同刻-{n}", created_at=local(2026, 9, 21, 10, 0, 0))
        self._log(summary="最旧", created_at=local(2026, 9, 21, 8, 0, 0))

        ascending = ["最旧", *[f"同刻-{n}" for n in range(1, 7)], "最新"]
        descending = ["最新", *[f"同刻-{n}" for n in range(6, 0, -1)], "最旧"]

        for _ in range(3):
            self.assertEqual(summaries(self._data(ordering="created_at")), ascending)
            self.assertEqual(summaries(self._data(ordering="-created_at")), descending)

    def test_unknown_ordering_is_rejected_without_500(self):
        """白名单外的取值 ⇒ 200 + `code=1000`，**不得** 500（E-0010 的「这类请求」判据）。

    覆盖三种「看起来像排序」的输入：别的列名、整套 `Meta.ordering`、注入形状。
"""
        self._log(summary="唯一的一条")
        self.assertEqual(self._get(ordering="id")["code"], 1000)
        self.assertEqual(self._get(ordering="-created_at,-id")["code"], 1000)
        self.assertEqual(self._get(ordering="-created_at; drop table")["code"], 1000)
        # 阳性对照：白名单内的值仍然是 0（否则「拒了」可能只是端点坏了）。
        self.assertEqual(self._get(ordering="-created_at")["code"], 0)


class OperationLogMalformedParamsTests(LogApiTestCase):
    """畸形参数一律走序列化器 ⇒ 200 + `code=1000`，**不得** 500（E-0010）。"""

    def test_unknown_log_type_is_rejected_without_500(self):
        self.assertEqual(self._get(log_type=999)["code"], 1000)

    def test_garbage_datetime_is_rejected_without_500(self):
        self.assertEqual(self._get(stime="garbage")["code"], 1000)

    def test_garbage_page_is_rejected_without_500(self):
        self.assertEqual(self._get(page="abc")["code"], 1000)

    def test_over_length_filters_are_rejected_without_500(self):
        """超长入参不得进 `varchar` 列或查询（E-0010）。

    列宽：`key` → `summary` 255、`game_name` 64、`order_no` **20**（`apps/audit/models.py`）。
    ⚠️ D-0301⭐ 精确框的**入参闸**放宽为「内置号或来源号」双列，宽度取两列的
    较大者 = 来源号列 128（`Order.source_order_no`）；审计表的 `order_no` 列本身仍是
    `varchar(20)`——Postgres 里超长字面量与短列比较不报错（只是匹配不上），21 位数字
    如今是**合法**的来源号 token、由来源号那一列接住。
    超一位就拒（200 + 1000），不是截断后静默查一个查不到的值。
"""
        self.assertEqual(self._get(key="长" * 256)["code"], 1000)
        self.assertEqual(self._get(game_name="长" * 65)["code"], 1000)
        self.assertEqual(self._get(order_no="0" * 129)["code"], 1000)

    def test_zero_page_size_is_rejected_without_500(self):
        self.assertEqual(self._get(page_size=0)["code"], 1000)


class OperationLogPermissionTests(LogApiTestCase):
    def test_user_holding_the_node_gets_200(self):
        """阳性对照：没有它，「无节点得 403」可能只是因为端点根本不可达。"""
        self.assertEqual(self.client.get(ENDPOINT).status_code, 200)

    def test_authenticated_user_without_the_node_gets_403(self):
        client = make_logged_in_client(username="no-logs-node")
        self.assertEqual(client.get(ENDPOINT).status_code, 403)


class OperationLogCrossTenantHttpTests(TenantTestCase):
    """租户 A 的登录态看不到租户 B 的行（`AGENTS.md` §4 的硬性要求）。

    本任务只做端点上的**一次**廉价检查；完整的跨租户回归套件是后续任务（spec §10.5）。
    判据用**行内唯一的显示值**（`summary`），**不用主键**——两个 schema 的主键序列
    各自从 1 开始，`filter(id=...)` 会命中「另一个租户自己的那一行」。
"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="audit_api_iso_b", name="租户 B", domain="audit-iso-b.test.local"
        )

    def setUp(self):
        # 挡住上一条用例把连接留在别的 schema 上的残留（跨域名请求不会自动还原）。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(username="iso-log-admin", is_superuser=True)

    def test_rows_of_another_tenant_are_not_visible(self):
        with tenant_context(self.other_tenant):
            OperationLog.objects.create(
                log_type=10, action="order.transition", actor="B 的客服",
                order_no="B000000000000000001", summary="B 租户自己的日志",
            )
            # 阳性对照：B 的那一行确实落库了，否则下面的否定断言空洞通过
            self.assertTrue(
                OperationLog.objects.filter(summary="B 租户自己的日志").exists()
            )

        OperationLog.objects.create(
            log_type=10, action="order.transition", actor="A 的客服",
            order_no="A000000000000000001", summary="A 租户自己的日志",
        )
        body = self.client.get(ENDPOINT).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(summaries(body["data"]), ["A 租户自己的日志"])


# ── ：`GET /api/audit/actors/` 人员枚举（spec §6.3；R18 尾斜杠）─────────
#
# **这个端点为什么存在**：日志页的「相关人员」是多选下拉，既有的
# 人员接口 `GET /api/accounts/` 的准入是 `IsAccountManager`（线 D；
# = 系统管理员 **或** 持任一账号管理节点，`apps/accounts/permissions.py`）**再叠加**
# `system_user.list` 节点门——只持有 `system.logs` 的人拉到的名单**恒空**。
# 所以这里是一个受同一节点保护、只回 `{id, username, display_name}` 的小接口。


class ActorApiMixin:
    """人员枚举端点的公共取数口。

    ⚠️ 复用 `LogApiTestCase` 的 `self.client`：它是**非超管、非账号管理员**、
    持有 `system.logs` 节点的真实登录态——正是本端点服务的对象。
"""

    def _body(self, **params):
        response = self.client.get(ACTORS_ENDPOINT, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def _actors(self, **params):
        body = self._body(**params)
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def _usernames(self, **params):
        return [row["username"] for row in self._actors(**params)]


class OperationLogActorEnumerationTests(ActorApiMixin, LogApiTestCase):
    """§6.3 的四条契约：子串命中、大小写不敏感、上限 50、只回三个字段（spec §11.1）。"""

    def test_key_returns_only_the_matching_username(self):
        User.objects.create_user("客服小甲", password="pw-x")
        User.objects.create_user("客服小乙", password="pw-x")
        self.assertEqual(self._usernames(key="小甲"), ["客服小甲"])
        # 阳性对照：换一个 key 必须命中另一条——否则上面「恰好一条」可能只是恒空。
        self.assertEqual(self._usernames(key="小乙"), ["客服小乙"])
        # 阳性对照二：不带 key 时两条都在（端点在夹具下确实能返回非空集）。
        without_key = self._usernames()
        self.assertIn("客服小甲", without_key)
        self.assertIn("客服小乙", without_key)

    def test_key_is_case_insensitive(self):
        User.objects.create_user("AliceActor", password="pw-x")
        self.assertEqual(self._usernames(key="aliceactor"), ["AliceActor"])
        self.assertEqual(self._usernames(key="ALICEACTOR"), ["AliceActor"])
        # 负向对照：另一个名字不该被卷进来（证明「匹配」不是「全都返回」）。
        self.assertEqual(self._usernames(key="bobactor"), [])

    def test_empty_key_is_the_same_as_not_provided(self):
        """空串按「没传」处理（spec：absent/empty = 不筛选）。"""
        User.objects.create_user("客服小甲", password="pw-x")
        self.assertEqual(self._actors(key=""), self._actors())
        self.assertIn("客服小甲", self._usernames(key=""))

    def test_more_than_fifty_matches_are_capped_at_fifty(self):
        """51 个命中 ⇒ 响应恰好 50 条（spec §6.3 的硬上限）。

    ⚠️ **51 个用户一次性造**：`bulk_create` 一条 INSERT 插 51 行
    （`create_user` 会逐行调 `set_password` 做口令哈希——本仓没改
    `PASSWORD_HASHERS`，走 Django 默认的 PBKDF2，51 次才是这条用例的耗时来源）。
    ⚠️ 阳性对照断言库里**真的是 51 个命中**——否则「恰好 50」可能是
    「库里本来就只有 50 个」凑出来的假绿。
"""
        User.objects.bulk_create(
            [User(username=f"bulk-actor-{n:02d}") for n in range(51)]
        )
        self.assertEqual(
            User.objects.filter(username__icontains="bulk-actor-").count(), 51
        )
        usernames = self._usernames(key="bulk-actor-")
        self.assertEqual(len(usernames), 50)
        self.assertNotEqual(len(usernames), 51)

    def test_each_row_carries_exactly_id_and_username(self):
        """行载荷的 key 集合**字面**等于 `{"id", "username", "display_name"}`。

    ⚠️ 方法名保留历史的「id_and_username」（改名的外部引用面大于收益），
    断言按 spec §11.1 的**新事实**更新：多了下拉显示用的 `display_name`。

    ⚠️ 这条断言防的是 **PII 泄漏**：账号表那一侧还挂着手机号、身份证号、
    收款账号（`apps/accounts` 的 StaffProfile / `account_payload`）——
    这个端点是给下拉用的，多吐一个字段就是把它们送到持有 `system.logs` 的人手里。
    `display_name`（昵称）本来就是账号列表页公开显示的一列，故允许；白名单仍是**逐条枚举**。
"""
        user = User.objects.create_user("客服小甲", password="pw-x")
        rows = self._actors(key="小甲")
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"id", "username", "display_name"})
        self.assertEqual(
            rows[0],
            {"id": user.pk, "username": "客服小甲", "display_name": "客服小甲"},
        )


class OperationLogActorMalformedKeyTests(ActorApiMixin, LogApiTestCase):
    """畸形 `key` 一律走序列化器 ⇒ 200 + `code=1000`，**不得** 500（E-0010）。"""

    def test_over_length_key_is_rejected_without_500(self):
        """列宽 = `auth_user.username` 的 150：151 拒（不是截断后静默查空）。"""
        # 边界内侧：150 个字符仍然合法，只是匹配不到任何人
        ok = self._body(key="长" * 150)
        self.assertEqual(ok["code"], 0, ok)
        self.assertEqual(ok["data"], [])
        # 边界外侧：超一位就拒
        too_long = self._body(key="长" * 151)
        self.assertEqual(too_long["code"], 1000, too_long)


class OperationLogActorPermissionTests(ActorApiMixin, LogApiTestCase):
    def test_non_admin_holding_the_node_gets_200(self):
        """本端点存在的直接理由（评审 I5）：只有 `system.logs`、没有账号管理员身份的人也拉得到名单。

    ⚠️ 判别力来自开头那两条断言：`has_node` 对 `is_superuser` **有短路**
    （`apps/rbac/permissions.py::has_node_now`），用一个超管夹具会让本用例
    「无论实现怎么放行都绿」——那正是评审 I5 要防的形状。
"""
        user = User.objects.get(username="log-viewer")
        self.assertFalse(user.is_superuser)
        self.assertFalse(is_account_admin(user))
        body = self._body()
        self.assertEqual(body["code"], 0, body)
        self.assertIsInstance(body["data"], list)

    def test_authenticated_user_without_the_node_gets_403(self):
        client = make_logged_in_client(username="no-logs-node-actor")
        self.assertEqual(client.get(ACTORS_ENDPOINT).status_code, 403)


class OperationLogActorCrossTenantTests(ActorApiMixin, LogApiTestCase):
    """A 的登录态看不到 B 的用户（`AGENTS.md` §4 的硬性要求）。

    ⚠️ 判据用**租户内唯一、且两租户取值不同**的 username（`iso-actor-only-in-a` /
    `-in-b`），**不用主键**：两个 schema 的 `auth_user` 自增序列各自从 1 开始，
    `filter(id=...)` 会命中「另一个租户自己的那一行」。
"""

    A_MARKER = "iso-actor-only-in-a"
    B_MARKER = "iso-actor-only-in-b"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="audit_actor_iso_b",
            name="审计人员枚举租户 B",
            domain="audit-actor-iso-b.test.local",
        )

    def setUp(self):
        # 挡住上一条用例把连接留在别的 schema 上的残留（跨域名请求不会自动还原）。
        connection.set_tenant(self.tenant)
        super().setUp()

    def test_users_of_another_tenant_are_not_visible(self):
        with tenant_context(self.other_tenant):
            User.objects.create_user(self.B_MARKER, password="pw-x")
            # 阳性对照：B 的这一行确实落库了，否则下面的否定断言空洞通过
            self.assertTrue(User.objects.filter(username=self.B_MARKER).exists())

        User.objects.create_user(self.A_MARKER, password="pw-x")
        self.assertEqual(self._usernames(key="iso-actor-only-"), [self.A_MARKER])


# ── 操作日志「操作用户」列显示昵称（spec §11.1）─────────────────────────────


def _actor_lookup_queries(case, **params):
    """跑一次 `GET /api/audit/logs/`，返回其中**取昵称**（SELECT `first_name`）的 SQL 条数。

    ⚠️ 只数含 `first_name` 的查询，不数总数：本端点的权限判据
    （`apps/rbac/permissions.py::user_node_codes`）自己也会查库，把总数当判据会掺进
    与「批不批量」无关的噪声。取昵称的查询是本文件唯一会 SELECT `first_name` 的那条。
"""
    with CaptureQueriesContext(connection) as ctx:
        case._data(**params)
    return sum(1 for query in ctx.captured_queries if "first_name" in query["sql"])


class OperationLogActorDisplayTests(LogApiTestCase):
    """「操作用户」列显示昵称（spec §11.1）。

    四种形态各有字面值断言：有昵称 / 昵称为空 / `actor_id` 为空 / 用户已删。
    期望值一律**手写中文**，不用 `user.first_name` 之类的自指表达式（E-0024）。
"""

    def _display_of_first_row(self):
        body = self._get()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"][0]["actor_display"]

    def test_nickname_wins_over_stored_username(self):
        user = User.objects.create_user("logs-actor-a", password="pw-x", first_name="测试昵称丙")
        self._log(actor="logs-actor-a", actor_id=user.pk)
        self.assertEqual(self._display_of_first_row(), "测试昵称丙")

    def test_blank_nickname_falls_back_to_username(self):
        user = User.objects.create_user("logs-actor-b", password="pw-x")
        self._log(actor="logs-actor-b", actor_id=user.pk)
        self.assertEqual(self._display_of_first_row(), "logs-actor-b")

    def test_missing_actor_id_falls_back_to_stored_string(self):
        # 历史行：迁移进来的旧行可能没有 actor_id（spec §6.3），回退到 actor 列本身。
        self._log(actor="历史账号名", actor_id=None)
        self.assertEqual(self._display_of_first_row(), "历史账号名")

    def test_deleted_actor_falls_back_to_stored_string(self):
        user = User.objects.create_user("logs-actor-c", password="pw-x", first_name="已删的人")
        self._log(actor="logs-actor-c", actor_id=user.pk)
        user.delete()
        self.assertEqual(self._display_of_first_row(), "logs-actor-c")

    def test_nickname_lookup_is_batched_not_per_row(self):
        """一页多行的昵称解析必须**一次性**查库（spec §11.1 的「批量」，不许 N+1）。

    判据：造 3 行不同用户，比较「单行请求里取昵称的 SQL 条数」与「三行请求的条数」——
    两者必须**相等**。逐行 `User.objects.get(...)` 的实现会让三行那次的条数多 2。

    ⚠️ 数的是「含 `first_name` 的查询」而不是总数（见 `_actor_lookup_queries` 的 docstring）；
    每请求的会话认证本身也会 SELECT 一次 `auth_user`（含 `first_name`）⇒ 基线是 2 而不是 1，
    所以判据是**不随行数增长**，不是钉绝对条数。
    ⚠️ 这是**行为判据**（查询次数），不是实现细节断言：无论是 `filter(pk__in=...)`
    还是别的批量手段，只要不逐行查就绿。最后的字面值断言是**阳性对照**：
    证明「条数没涨」不是「昵称根本没查」凑出来的。
"""
        users = [
            User.objects.create_user(f"batch-actor-{n}", password="pw-x", first_name=f"昵称{n}")
            for n in range(3)
        ]
        self._log(summary="第一条", actor="batch-actor-0", actor_id=users[0].pk)
        single_row_queries = _actor_lookup_queries(self, page_size=1)
        for n in (1, 2):
            self._log(summary=f"第{n}条", actor=f"batch-actor-{n}", actor_id=users[n].pk)
        self.assertEqual(_actor_lookup_queries(self, page_size=3), single_row_queries)
        self.assertEqual(
            [row["actor_display"] for row in self._data(page_size=3)["items"]],
            ["昵称2", "昵称1", "昵称0"],
        )


# ── 人员枚举「显示昵称 + 按昵称搜索」（spec §11.1）─────────────────────────


class OperationLogActorNicknameTests(ActorApiMixin, LogApiTestCase):
    """人员下拉显示昵称（spec §11.1）：字段集合、回退、按昵称可搜。

    期望值一律**手写中文**（E-0024），不用 `user.first_name` 之类的自指表达式。
"""

    def test_row_carries_exactly_id_username_display_name(self):
        user = User.objects.create_user("actor-nick-a", password="pw-x", first_name="测试昵称丙")
        rows = self._actors(key="actor-nick-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), {"id", "username", "display_name"})
        self.assertEqual(
            rows[0],
            {"id": user.pk, "username": "actor-nick-a", "display_name": "测试昵称丙"},
        )

    def test_blank_nickname_falls_back_to_username(self):
        User.objects.create_user("actor-nick-b", password="pw-x")
        self.assertEqual(self._actors(key="actor-nick-b")[0]["display_name"], "actor-nick-b")

    def test_key_matches_nickname_so_the_dropdown_is_searchable(self):
        """下拉显示昵称 ⇒ 用户会按**看到的昵称**搜（spec §11.1 有意扩的一条）。"""
        User.objects.create_user("actor-nick-c", password="pw-x", first_name="测试昵称甲")
        User.objects.create_user("actor-nick-d", password="pw-x", first_name="测试昵称乙")
        # 阳性：按昵称搜得到；阴性：另一个昵称不被卷进来。
        self.assertEqual(self._usernames(key="昵称甲"), ["actor-nick-c"])
        self.assertEqual(self._usernames(key="昵称乙"), ["actor-nick-d"])
        # 阴性对照（spec §12.1 要求）：不存在的串搜不到，证明上面不是「全都返回」。
        self.assertEqual(self._usernames(key="不存在的昵称"), [])
        # 阳性对照：按**账号名**仍然搜得到（扩展不是替换）。
        self.assertEqual(self._usernames(key="actor-nick-c"), ["actor-nick-c"])


class OrderNoDualColumnSearchTests(LogApiTestCase):
    """⭐ D-0301（2026-10-24）：审计页「订单号」精确框**双列 OR**——
    系统内置号照旧命中，来源单号（`OperationLog.source_order_no`）也能命中。"""

    def test_source_order_no_hits_and_system_no_still_hits(self):
        self._log(summary="来源号那行", order_no="A000000000000000011",
                  source_order_no="SRC-TEST-AUDIT")
        self._log(summary="内置号那行", order_no="A000000000000000012",
                  source_order_no="TB-OTHER")
        # 来源号精确命中它自己那一行（⛔ 不是子串：别的行的来源号前缀不误命中）。
        items = self._data(order_no="SRC-TEST-AUDIT")["items"]
        self.assertEqual([i["summary"] for i in items], ["来源号那行"])
        # 系统内置号照旧精确命中。
        items = self._data(order_no="A000000000000000012")["items"]
        self.assertEqual([i["summary"] for i in items], ["内置号那行"])
