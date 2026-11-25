"""`GET /api/audit/order-action-logs/` 的接口契约
（spec `docs/specs/2026-10-04-trade-action-log-design.md` §4 / §8 的 13 条）。

⚠️ **路径带尾斜杠**（R18，与 `apps/audit/urls.py` 的其余三条同形）。

⚠️ **测试自己造行**：本端点只读 `OperationLog`，所以这里一律直接建行，
**不依赖**任何业务端点顺手写日志（那会让本文件的绿取决于别的任务的进度）。

判据纪律（Global Constraints）：期望值写字面值（E-0024）；否定断言前先给**阳性对照**，
否则空列表能让 `assertNotIn` 空洞通过；**跨租户的否定断言用显示值（`summary`）指认，
⛔ 不用主键**（两个 schema 的主键序列各自从 1 开始）。
"""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.actions import ORDER_ACTION_EXCLUDED
from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client
# 用登记表的手写动作集合做独立依据，避免从实际返回值的来源推算期望。
from tests.test_operation_log_model import EXPECTED_ORDER_ACTION_CODES

ENDPOINT = "/api/audit/order-action-logs/"

#: 本页的准入节点（spec D2）。⛔ 与 `/api/audit/logs/` 的 `system.logs` 是**两个门**。
NODE = "order.platform_action_logs"

#: `Asia/Shanghai` 无夏令时 ⇒ 固定 `+08:00` 与 `ZoneInfo("Asia/Shanghai")` 是**同一瞬时**，
#: 且不依赖本机 tzdata（与 `test_operation_log_api.py` 同源）。
CST = dt_timezone(timedelta(hours=8))

User = get_user_model()

#: ⭐ 订单日志人名线 spec §3-C1：**显示名**夹具。
#: ⚠️ 昵称与登录账号必须**不同形**（spec §5 的夹具纪律）：两者相同时
#: 「`actor_display` 取的是昵称还是账号」这条判据会**假绿**。
ACTOR_USERNAME = "tal-nick-actor"
ACTOR_NICKNAME = "测试昵称客服丁"


def local(*args):
    """按本地时区（Asia/Shanghai）构造一个瞬时。"""
    return datetime(*args, tzinfo=CST)


def summaries(data):
    """响应 `data` 里每一行「操作说明」列的值——测试的行标识（本页行的显示值）。"""
    return [row["summary"] for row in data["items"]]


def _order_backfill_queries(case, **params):
    """跑一次端点，返回其中**回查订单**（SQL 里出现 `business_game`）的查询条数。

    ⚠️ 只数含 `business_game` 的查询，不数总数：本端点的权限判据（`user_node_codes`）
    与分页计数自己也会查库，把总数当判据会掺进与「批不批量」无关的噪声。
    回填是本文件里唯一会 JOIN `business_game` 的那条查询（正因为它 JOIN 了游戏表，
    所以按该表名取就够判别）。
    """
    with CaptureQueriesContext(connection) as ctx:
        case._data(**params)
    return sum(1 for query in ctx.captured_queries if "business_game" in query["sql"])


class OrderActionLogApiTestCase(TenantTestCase):
    """公共夹具：一个**只持本页节点**（`order.platform_action_logs`）的真实登录 client。

    ⚠️ **只挂这一个节点**（`make_logged_in_client(nodes=…)` 自己 `seed_catalog()`）：
    本文件不测「系统操作日志」那一页，灌整个目录只是为了让节点存在。
    """

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回本租户。跨域名请求**不会**在响应结束时还原 schema
        #    （`django_tenants` 的中间件不还原）⇒ 上一条用例可能把连接留在别的 schema 上。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(
            username="tal-viewer", groups=("客服",), nodes=(NODE,)
        )

    @staticmethod
    def _log(**overrides):
        fields = {
            "log_type": 10,
            "action": "order.transition",
            "actor": "测试客服甲",
            "order_no": "20260101000000000001",
            "summary": "把订单的状态从代练中改为待验收",
            # 历史行可能没有 `actor_id`（spec §11.1）；测试需要显式控制它。
            "actor_id": None,
        }
        fields.update(overrides)
        return OperationLog.objects.create(**fields)

    def _get(self, **params):
        response = self.client.get(ENDPOINT, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def _data(self, **params):
        body = self._get(**params)
        self.assertEqual(body["code"], 0, body)
        return body["data"]


class OrderActionLogRowShapeTests(OrderActionLogApiTestCase):
    def test_row_carries_exactly_the_eleven_contracted_keys(self):
        """spec §4.3 的 11 个键，**恰好**——多一个都是对外契约的扩大。"""
        self._log()
        items = self._data()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(
            set(items[0]),
            {
                "order_no", "source_order_no", "game_name", "from_value", "to_value",
                "action", "action_label", "summary", "actor", "actor_display",
                "created_at",
            },
        )

    def test_payload_carries_summary_actors_and_actions_slots(self):
        """`data` 的形状 = `{items,total,page,page_size,summary,actors,actor_options,actions}`。

        ⚠️ **恰好**集合：多一个键就是对外契约的扩大（本仓的「恰好」纪律见
        `test_order_recycle.py::RECYCLE_ROW_KEYS` 那段理由）。
        ⭐ 订单日志人名线 spec §3-C1）：新键 `actor_options` 是**加法**，
        旧的 `actors: string[]` 仍在（部署窗口里旧前端只认它）。
        """
        self._log()
        self.assertEqual(
            set(self._data()),
            {
                "items", "total", "page", "page_size", "summary", "actors",
                "actor_options", "actions",
            },
        )

    def test_created_at_is_rendered_in_the_local_timezone(self):
        self._log(created_at=local(2026, 9, 21, 10, 0, 0))
        self.assertEqual(
            self._data()["items"][0]["created_at"], "2026-09-21T10:00:00+08:00"
        )


class OrderActionLogRowSetTests(OrderActionLogApiTestCase):
    """行集合 = `action` 以 `order.` 开头（spec D1）。"""

    def test_row_set_follows_the_action_prefix_not_the_log_type(self):
        """**判据是 `action` 前缀，不是 `log_type=10`**（spec D1 的承重用例）。

        三条行两两对照：① 前缀与分类都像订单 ⇒ 收；② **前缀像订单、分类不是** ⇒ **也收**
        （用 `log_type=10` 实现会把它漏掉）；③ **分类像订单、前缀不是** ⇒ **不收**
        （用 `log_type=10` 实现会把它收进来）。
        ⭐ spec §10.8）补第四条：**来源是 `system.*`、分类也不是 10** ⇒ **不收**
        ——把「**前缀 / 分类 / 来源**」三个维度都覆盖到（⛔ 是**加严**，上面三条断言一字未动）。
        """
        self._log(action="order.transition", log_type=10, summary="前缀像订单、分类也是订单")
        self._log(action="order.transition", log_type=20, summary="前缀像订单、分类不是订单")
        self._log(action="performance.review", log_type=10, summary="分类像订单、前缀不是")
        self._log(action="system.game_create", log_type=40, summary="系统来源、分类也不是订单")
        self.assertEqual(
            sorted(summaries(self._data())),
            ["前缀像订单、分类不是订单", "前缀像订单、分类也是订单"],
        )

    def test_from_and_to_values_come_back(self):
        """「变更内容」列要的两列（业务方 R4 / spec D5）真的回得来。"""
        self._log(from_value="代练中", to_value="待验收")
        row = self._data()["items"][0]
        self.assertEqual(row["from_value"], "代练中")
        self.assertEqual(row["to_value"], "待验收")

    def test_action_label_is_the_short_chinese_name(self):
        self._log(action="order.transition")
        self.assertEqual(self._data()["items"][0]["action_label"], "改订单状态")

    def test_actor_display_falls_back_to_the_stored_actor(self):
        """昵称解析取不到人（`actor_id` 为空）时回退行里存的 `actor`（spec §4.3）。"""
        self._log(actor="tal-historic-actor", actor_id=None)
        self.assertEqual(
            self._data()["items"][0]["actor_display"], "tal-historic-actor"
        )


class OrderActionLogBackfillTests(OrderActionLogApiTestCase):
    """D7：日志列有值就用日志列，为空才按 `order_no` 回查订单当前值（spec §8-4）。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260101000000000007",
            source_order_no="TB-测试来源单号7",
        )

    def test_missing_game_name_and_source_order_no_are_backfilled(self):
        self._log(order_no=self.order.order_no, game_name="", source_order_no="")
        row = self._data()["items"][0]
        self.assertEqual(row["game_name"], "测试游戏甲")
        self.assertEqual(row["source_order_no"], "TB-测试来源单号7")

    def test_rows_own_values_win_over_the_order(self):
        """日志自己的列优先——「记下来的那一刻」的值不被订单当前值覆盖（D7）。"""
        self._log(
            order_no=self.order.order_no,
            game_name="日志里的游戏",
            source_order_no="日志里的单号",
        )
        row = self._data()["items"][0]
        self.assertEqual(row["game_name"], "日志里的游戏")
        self.assertEqual(row["source_order_no"], "日志里的单号")

    def test_blank_or_unknown_order_no_stays_empty(self):
        """`order_no` 为空（如 `order.export`）或查不到订单 ⇒ 两列保持空（D7/D15）。"""
        self._log(action="order.export", order_no="", summary="导出了订单")
        self._log(order_no="20260101000000000099", summary="查不到订单的那条")
        rows = {row["summary"]: row for row in self._data(page_size=10)["items"]}
        self.assertEqual(rows["导出了订单"]["game_name"], "")
        self.assertEqual(rows["导出了订单"]["source_order_no"], "")
        self.assertEqual(rows["查不到订单的那条"]["game_name"], "")
        self.assertEqual(rows["查不到订单的那条"]["source_order_no"], "")

    def test_backfill_lookup_is_batched_not_per_row(self):
        """回填必须**一次性批量**查（spec D7），⛔ 不许每行一次。

        判据：比较「一页 1 行」与「一页 3 行」时**回查订单的 SQL 条数**——两者必须相等；
        逐行 `Order.objects.get(...)` 会让三行那次多 2。最后的字面值断言是**阳性对照**：
        证明「条数没涨」不是「压根没回查」凑出来的。
        """
        for n in range(3):
            Order.objects.create(
                game=self.game,
                order_no=f"2026010100000000{n:04d}",
                source_order_no=f"TB-{n}",
            )
        self._log(order_no="20260101000000000000", summary="第一条",
                  game_name="", source_order_no="")
        single_row_queries = _order_backfill_queries(self, page_size=1)
        for n in (1, 2):
            self._log(order_no=f"2026010100000000{n:04d}", summary=f"第{n}条",
                      game_name="", source_order_no="")
        self.assertEqual(_order_backfill_queries(self, page_size=3), single_row_queries)
        self.assertGreaterEqual(single_row_queries, 1, "根本没有回查订单——阳性对照失败")
        self.assertEqual(
            sorted(row["game_name"] for row in self._data(page_size=3)["items"]),
            ["测试游戏甲", "测试游戏甲", "测试游戏甲"],
        )


class OrderActionLogFilterTests(OrderActionLogApiTestCase):
    """5 个筛选逐个（spec §8-6）：造两条**只差被筛字段**的行，断言筛出来的恰好是一条。"""

    def test_stime_and_etime_are_inclusive_at_both_ends(self):
        self._log(summary="早的那条", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(summary="区间两端之间的那条", created_at=local(2026, 9, 21, 10, 0, 0))
        self._log(summary="晚的那条", created_at=local(2026, 9, 21, 11, 0, 0))
        data = self._data(stime="2026-09-21 09:00:00", etime="2026-09-21 10:00:00")
        self.assertEqual(summaries(data), ["区间两端之间的那条", "早的那条"])

    def test_etime_given_as_a_bare_date_includes_the_whole_day(self):
        """`etime` **只给日期** ⇒ **含当天**（业务方 2026-10-04 报的缺陷）。

        原话：「选中了 9 月 4 到 10 月 4 但是没有包括当天四号的记录」。根因：页面那两个框是
        `<input type="date">`，前端发纯日期，而 DRF 的 `DateTimeField` 会把它补成当天 `00:00:00`
        ⇒ `created_at <= 当天 00:00` 把**当天全天**排除掉。
        ⇒ `etime` 改用 `InclusiveEndDateTimeField`（`apps/audit/api/serializers.py`）。

        ⚠️ 边界取到 **`23:59:59.999999`**（PostgreSQL `timestamptz` 的最细粒度）——
        而次日 `00:00` 那条必须**不在**结果里，否则就是把区间扩过了头。
        """
        self._log(summary="前一天 23:59:59", created_at=local(2026, 10, 3, 23, 59, 59))
        self._log(summary="当天 00:00:00", created_at=local(2026, 10, 4, 0, 0, 0))
        self._log(summary="当天 12:00:00", created_at=local(2026, 10, 4, 12, 0, 0))
        self._log(
            summary="当天 23:59:59.999999",
            created_at=datetime(2026, 10, 4, 23, 59, 59, 999999, tzinfo=CST),
        )
        self._log(summary="次日 00:00:00", created_at=local(2026, 10, 5, 0, 0, 0))
        data = self._data(stime="2026-10-04", etime="2026-10-04")
        self.assertEqual(
            sorted(summaries(data)),
            ["当天 00:00:00", "当天 12:00:00", "当天 23:59:59.999999"],
        )

    def test_etime_given_with_a_time_is_not_widened(self):
        """`etime` **给到时刻**时照原样（⛔ 不替调用方扩到当天结束）——只有「纯日期」那一档扩。"""
        self._log(summary="10 点整", created_at=local(2026, 10, 4, 10, 0, 0))
        self._log(summary="10 点零 1 秒", created_at=local(2026, 10, 4, 10, 0, 1))
        self.assertEqual(summaries(self._data(etime="2026-10-04 10:00:00")), ["10 点整"])

    def test_action_selects_only_that_action(self):
        self._log(action="order.transition", summary="改状态那条")
        self._log(action="order.claim", summary="接单那条")
        self.assertEqual(summaries(self._data(action="order.claim")), ["接单那条"])

    def test_actor_matches_the_whole_value_not_a_prefix(self):
        """`actor` 是**整值相等**（spec §4.2）——前缀匹配会把别人的日志一起放出来。"""
        self._log(actor="tal-actor-a", summary="甲的那条")
        self._log(actor="tal-actor-ab", summary="甲乙的那条")
        self.assertEqual(summaries(self._data(actor="tal-actor-a")), ["甲的那条"])
        # 阳性对照 + 否定断言：前缀本身不是一个人，必须空
        self.assertEqual(summaries(self._data(actor="tal-actor")), [])

    def test_key1_matches_the_system_order_no(self):
        self._log(order_no="20260101000000000021", summary="系统单号命中")
        self._log(order_no="20260101000000000022", source_order_no="TB-命中-22",
                  summary="交易单号命中")
        self.assertEqual(summaries(self._data(key1="000021")), ["系统单号命中"])

    def test_key1_matches_the_source_order_no(self):
        self._log(order_no="20260101000000000021", summary="系统单号命中")
        self._log(order_no="20260101000000000022", source_order_no="TB-命中-22",
                  summary="交易单号命中")
        self.assertEqual(summaries(self._data(key1="命中-22")), ["交易单号命中"])

    def test_ordering_switches_between_both_directions(self):
        self._log(summary="早的那条", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(summary="晚的那条", created_at=local(2026, 9, 21, 10, 0, 0))
        self.assertEqual(summaries(self._data(ordering="created_at")),
                         ["早的那条", "晚的那条"])
        self.assertEqual(summaries(self._data(ordering="-created_at")),
                         ["晚的那条", "早的那条"])


class OrderActionLogSummaryTests(OrderActionLogApiTestCase):
    """右侧「操作统计」（spec §4.4 / §8-8）。"""

    def test_summary_groups_by_action_and_actor_with_counts(self):
        self._log(action="order.transition", actor="tal-actor-a")
        self._log(action="order.transition", actor="tal-actor-a")
        self._log(action="order.claim", actor="tal-actor-b")
        summary = self._data()["summary"]
        # ⭐ spec §3-C1）：每行多 `actor_display`。
        # ⚠️ 本用例里的 `tal-actor-a` / `tal-actor-b` **没有**对应用户行 ⇒ 回落登录账号
        #    （那一档由下面 `*ActorDisplayTests` 的「查不到」用例钉住；这里只钉分组与计数）。
        self.assertEqual(
            summary,
            [
                {"action": "order.transition", "action_label": "改订单状态",
                 "actor": "tal-actor-a", "actor_display": "tal-actor-a", "count": 2},
                {"action": "order.claim", "action_label": "大厅自助接单",
                 "actor": "tal-actor-b", "actor_display": "tal-actor-b", "count": 1},
            ],
        )

    def test_historical_unknown_action_gets_the_same_labels_on_both_sides(self):
        raw_action = "order.historical_adapter_event"
        self._log(action=raw_action, actor="", summary=raw_action)
        data = self._data()
        item, stats = data["items"][0], data["summary"][0]
        for row in (item, stats):
            self.assertEqual(row["action"], raw_action)
            self.assertEqual(row["action_label"], "订单操作")
            self.assertEqual(row["actor"], "")
            self.assertEqual(row["actor_display"], "系统")
        self.assertNotIn(raw_action, item["summary"])

    def test_summary_covers_the_whole_filtered_set_beyond_the_page(self):
        """统计打在**筛选全集**上，⛔ 不是当前那一页（spec D3 的承重判据）。"""
        for n in range(3):
            self._log(action="order.transition", actor="tal-actor-a", summary=f"第{n}条")
        data = self._data(page_size=1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["summary"][0]["count"], 3)

    def test_summary_follows_the_filters(self):
        self._log(action="order.transition", summary="留下")
        self._log(action="order.claim", summary="被筛掉")
        summary = self._data(action="order.transition")["summary"]
        self.assertEqual([row["action"] for row in summary], ["order.transition"])

    def test_summary_is_empty_when_nothing_matches(self):
        self._log()
        self.assertEqual(self._data(key1="绝对不命中的关键词")["summary"], [])


class OrderActionLogActorsTests(OrderActionLogApiTestCase):
    """「操作人员」候选（spec §4.5 / §8-9）。"""

    def test_actors_are_distinct_and_sorted(self):
        self._log(actor="tal-actor-b")
        self._log(actor="tal-actor-a")
        self._log(actor="tal-actor-b")
        self.assertEqual(self._data()["actors"], ["tal-actor-a", "tal-actor-b"])

    def test_actors_ignore_every_filter(self):
        """底集**不叠任何筛选**（D9）——否则选完一个下拉就互相收窄。"""
        self._log(actor="tal-actor-a", created_at=local(2026, 9, 21, 9, 0, 0))
        self._log(actor="tal-actor-b", created_at=local(2026, 9, 21, 20, 0, 0))
        data = self._data(
            stime="2026-09-21 08:00:00", etime="2026-09-21 10:00:00",
            action="order.transition", actor="tal-actor-a",
        )
        # 行被筛到只剩一条……
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["actors"], ["tal-actor-a", "tal-actor-b"])

    def test_actors_only_cover_order_prefixed_rows(self):
        self._log(actor="tal-actor-order")
        self._log(action="performance.review", log_type=20, actor="tal-actor-performance")
        self.assertEqual(self._data()["actors"], ["tal-actor-order"])

    def test_actors_drop_the_blank_actor(self):
        """空 `actor` **不进**下拉，且不炸（spec §10.4）。

        `Order.updated_by` 是 `blank=True`，而 `order.refund` 那条写点把 `actor` 取成
        `order.updated_by or ""` ⇒ 空串**结构上可达**（评审探针实测 `actors == ['', '探针乙']`）。
        不排的话下拉里多一个**空白项**，而它经参数组装层会被当「没传」⇒ 选了等于没筛（静默）。
        """
        self._log(actor="tal-actor-a")
        self._log(actor="")
        data = self._data()
        # 阳性对照：非空那条还在（否则「不含空串」可能只是端点坏了 / 一行都没回）
        self.assertEqual(data["actors"], ["tal-actor-a"])
        self.assertNotIn("", data["actors"])


class OrderActionLogActorDisplayTests(OrderActionLogApiTestCase):
    """⭐ 2026-10-17（订单日志人名线 spec §3-C1 / 判据 9–11）：操作统计与筛选按**账号名称**显示。

    口径 = `apps/accounts/display.py::user_display_name`（昵称优先、空串回落登录账号）。
    ⛔ 分组维度与筛选键**仍然是登录账号**（`actor`）：显示名只是**显示**，
    同名的人不能被合并、筛选也不能换成显示名（spec §6-2 / 判据 11）。
    """

    def setUp(self):
        super().setUp()
        # 一个**有昵称**的账号：昵称与登录账号不同形 ⇒ 判据有判别力。
        self.nick_user = User.objects.create_user(
            username=ACTOR_USERNAME, first_name=ACTOR_NICKNAME
        )

    def test_summary_actor_display_is_the_nickname_while_actor_stays_the_account(self):
        """判据 9：`actor_display` 是**昵称**、`actor` 仍是登录账号；两者**不同形**。"""
        self._log(action="order.transition", actor=ACTOR_USERNAME)
        row = self._data()["summary"][0]
        self.assertEqual(
            set(row), {"action", "action_label", "actor", "actor_display", "count"}
        )
        self.assertEqual(row["actor"], ACTOR_USERNAME)
        self.assertEqual(row["actor_display"], ACTOR_NICKNAME)
        # 反向：显示列里**不许**是账号（否则这条与上一行同时成立就成了自证）。
        self.assertNotEqual(row["actor_display"], row["actor"])

    def test_summary_actor_display_falls_back_to_the_account_for_an_unknown_one(self):
        """判据 9 的回落那半：查不到的账号（已硬删 / 改名）⇒ `actor_display == actor`。

        ⚠️ 必须用**阳性对照**（同一个响应里既有解析到的、也有解析不到的），
        否则「全回落」这种坏实现也能让这条绿。
        """
        ghost = "tal-ghost-actor"
        self.assertFalse(User.objects.filter(username=ghost).exists())
        self._log(action="order.claim", actor=ghost)
        self._log(action="order.transition", actor=ACTOR_USERNAME)
        by_actor = {row["actor"]: row for row in self._data()["summary"]}
        self.assertEqual(by_actor[ghost]["actor_display"], ghost)
        self.assertEqual(
            by_actor[ACTOR_USERNAME]["actor_display"], ACTOR_NICKNAME
        )

    def test_actor_options_separate_the_account_from_the_display_name(self):
        """判据 10：`actor_options` 的 `actor` 是登录账号、`display` 是昵称。

        并同时钉住「与 `actors` **同集合、同顺序**」（spec §6-7：同一处 `_actors()` 派生）。
        """
        self._log(actor=ACTOR_USERNAME)
        self._log(actor="tal-actor-b")
        data = self._data()
        # `actors`（兼容保留的旧形状）仍是登录账号、字典序
        self.assertEqual(data["actors"], sorted([ACTOR_USERNAME, "tal-actor-b"]))
        self.assertEqual(
            data["actor_options"],
            [
                # 字典序在前的那条（`tal-actor-b` < `tal-nick-actor`）：查不到的用户
                # ⇒ display 回落 actor（⛔ 不回空串）
                {"actor": "tal-actor-b", "display": "tal-actor-b"},
                {"actor": ACTOR_USERNAME, "display": ACTOR_NICKNAME},
            ],
        )
        # 顺序 / 集合与 `actors` 逐项一致（⛔ 不许各写一份去重）
        self.assertEqual(
            [option["actor"] for option in data["actor_options"]], data["actors"]
        )
        self.assertEqual(
            {option["actor"] for option in data["actor_options"]},
            set(data["actors"]),
        )

    def test_filtering_by_the_login_account_still_hits(self):
        """判据 11：⚠️ 筛选键**没有**换成显示名——`?actor=<登录账号>` 仍然命中。

        判别力：若有人把 `filter(actor=…)` 改成按显示名过滤，本用例**两条都会红**
        （用账号筛 ⇒ 空；用昵称筛 ⇒ 也空，因为那一列里存的是账号）。
        """
        self._log(actor=ACTOR_USERNAME, summary="昵称账号的那条")
        self._log(actor="tal-actor-b", summary="别人的那条")
        self.assertEqual(
            summaries(self._data(actor=ACTOR_USERNAME)), ["昵称账号的那条"]
        )
        # 反向：**显示名**不是筛选键（拿它筛必须筛不到），否则上面那条就成了恒真。
        self.assertEqual(summaries(self._data(actor=ACTOR_NICKNAME)), [])

    def test_summary_resolves_display_names_with_one_query_per_request(self):
        """实现口径（spec §3-C1）：统计行的显示名映射**一次**查询，⛔ 不逐行查。

        判别力：把映射挪进逐行循环 ⇒ 3 个不同 actor 会打出 3 条 `auth_user` 查询、本用例红。
        ⚠️ 只数命中 `auth_user` 的 SELECT（其余查询与「批不批量」无关，数进来只会让判据变脆）。
        """
        User.objects.create_user(username="tal-nick-b", first_name="测试昵称戊")
        User.objects.create_user(username="tal-nick-c", first_name="测试昵称己")
        for actor in ("tal-nick-b", "tal-nick-c", ACTOR_USERNAME):
            self._log(actor=actor)
        with CaptureQueriesContext(connection) as ctx:
            data = self._data()
        self.assertEqual(len(data["summary"]), 3)   # 非空哨兵
        # ⚠️ 只认**批量映射那一条**：`WHERE "auth_user"."username" IN (…)`。
        #    ⛔ 不数「所有带 auth_user 的查询」——会话用户的主键查询也会带这个表名。
        # ⚠️ 判别力仍在：改成逐行 `User.objects.get(username=…)` ⇒ 这条 `IN` 查询消失（0 ≠ 2）⇒ 红。
        user_queries = [
            q["sql"] for q in ctx.captured_queries
            if '"auth_user"."username" IN' in q["sql"]
        ]
        # ① summary 的显示名映射 ② `actor_options` 的显示名映射（两处各一次、都不逐行）
        self.assertEqual(
            len(user_queries), 2,
            f"统计行 + 下拉各一次 auth_user 查询，实际 {len(user_queries)} 次：{user_queries}",
        )
        # 反向：逐行 `filter(username=…)` 的形态**一条都不许有**（只数 IN 会漏掉这种实现）。
        per_row = [
            q["sql"] for q in ctx.captured_queries
            if '"auth_user"."username" =' in q["sql"]
        ]
        self.assertEqual(per_row, [], f"不许逐行按 username 查用户：{per_row}")


class OrderActionLogExcludedActionTests(OrderActionLogApiTestCase):
    """`ORDER_ACTION_EXCLUDED` 里的动作**不进这一页**（业务方 2026-10-04 裁定）。

    原话：「查看账号密码这个操作不要写进去 不需要 这不属于订单操作了」——
    `order.view_credentials` 是订单详情里点「查看账号密码」的**读动作**，不是对订单做的操作。

    ⚠️ **本类只断「这一页看不到」，⛔ 不断「有没有记」**：埋点**保留**（谁看过哪个账号的密码是
    安全审计要用的）——下面第一条用例**正面断言那一行仍在库里**，就是钉这个边界：
    哪天有人图省事把埋点删了，这条会红。
    """

    def test_the_excluded_action_is_hidden_from_the_page_but_still_recorded(self):
        self._log(
            action="order.view_credentials",
            actor="tal-excluded-actor",
            summary="看了这张单的账号密码",
        )
        self._log(action="order.transition", actor="tal-kept-actor", summary="改状态那条")
        # ① 埋点没有删：那一行**在库里**
        self.assertTrue(
            OperationLog.objects.filter(action="order.view_credentials").exists(),
            "排除的是「这一页显示什么」，⛔ 不是「要不要记」——埋点必须还在",
        )
        data = self._data()
        # ② 列表里看不到它
        self.assertEqual(summaries(data), ["改状态那条"])
        # ③ 下拉与候选里也看不到它（三处共用同一个行集合定义，spec §11）
        self.assertNotIn("order.view_credentials", [row["code"] for row in data["actions"]])
        self.assertNotIn("tal-excluded-actor", data["actors"])

    def test_the_excluded_action_cannot_be_used_as_a_filter_value(self):
        """它连**筛选项**都不是（`action` 的 `ChoiceField` 词表来自标签表）⇒ 传它当场 1000。"""
        body = self._get(action="order.view_credentials")
        self.assertEqual(body["code"], 1000, body)

    def test_the_excluded_code_is_really_in_the_registry(self):
        """自证这条排除**不是排除一个不存在的码**（那会让「两边同时少一条」也相等 ⇒ 恒真判据）。"""
        from apps.audit.actions import ACTION_TEMPLATES

        self.assertTrue(ORDER_ACTION_EXCLUDED <= set(ACTION_TEMPLATES))
        self.assertEqual(ORDER_ACTION_EXCLUDED, {"order.view_credentials"})


class OrderActionLogActionsTests(OrderActionLogApiTestCase):
    """「操作动作」候选 = `ORDER_ACTION_LABELS` 的对外投影（spec §4.5 / §8-14）。

    ⚠️ **这一份是短名的唯一出处**（D3）：前端**不镜像**那份常量，直接用 `data.actions`
    ⇒ 「下拉里那条的短名」与「表格里那一行的短名」必须来自同一处（下面最后一条钉这个）。
    """

    def test_actions_carry_exactly_the_page_codes_in_code_order(self):
        actions = self._data()["actions"]
        expected = sorted(set(EXPECTED_ORDER_ACTION_CODES) - ORDER_ACTION_EXCLUDED)
        self.assertEqual(len(actions), 59)
        self.assertEqual([row["code"] for row in actions], expected)
        self.assertIn({"code": "order.jidanpu_direct_assign", "label": "指派鸡蛋铺下家打手"}, actions)

    def test_every_action_row_carries_exactly_code_and_label(self):
        for row in self._data()["actions"]:
            self.assertEqual(set(row), {"code", "label"})
            self.assertTrue(row["label"].strip(), row)

    def test_actions_ignore_every_filter(self):
        """底集**不叠任何筛选**（D3/D9）——筛到 0 行时下拉仍要能选回全部。"""
        self._log(action="order.transition", summary="唯一的一条")
        self.assertEqual(len(self._data(key1="绝对不命中的关键词")["items"]), 0)
        self.assertEqual(len(self._data(key1="绝对不命中的关键词")["actions"]), 59)

    def test_action_labels_are_the_same_source_as_the_row_labels(self):
        """**承重判据**：下拉的短名与行里的 `action_label` 同源（spec §8-14）。

        从**调用方真正看到的载荷**里取：先在 `data.actions` 里找到本行 `action` 的短名，
        再与同一份响应里那一行的 `action_label` 比 —— 两者不等就说明短名有两个出处
        （正是「前端镜像一份常量」会造成的漂移）。
        """
        self._log(action="order.claim")
        data = self._data()
        row = data["items"][0]
        dropdown = {item["code"]: item["label"] for item in data["actions"]}
        self.assertEqual(dropdown[row["action"]], row["action_label"])


class OrderActionLogPaginationTests(OrderActionLogApiTestCase):
    """分页（spec §8-13）。"""

    def test_page_size_is_honoured_and_total_is_the_filtered_total(self):
        for n in range(5):
            self._log(summary=f"第{n}条")
        data = self._data(page_size=2)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["page_size"], 2)
        self.assertEqual(data["total"], 5)

    def test_second_page_carries_the_remaining_rows(self):
        for n in range(3):
            self._log(summary=f"第{n}条", created_at=local(2026, 9, 21, 9, n, 0))
        first = self._data(page=1, page_size=2)
        second = self._data(page=2, page_size=2)
        self.assertEqual([row["summary"] for row in first["items"]], ["第2条", "第1条"])
        self.assertEqual([row["summary"] for row in second["items"]], ["第0条"])
        self.assertEqual(second["total"], 3)


class OrderActionLogMalformedParamsTests(OrderActionLogApiTestCase):
    """畸形参数一律走序列化器 ⇒ 200 + `code=1000`，**不得** 500（E-0010 / spec §8-12）。"""

    def setUp(self):
        super().setUp()
        self._log(summary="唯一的一条")

    def test_garbage_page_is_rejected_without_500(self):
        self.assertEqual(self._get(page="abc")["code"], 1000)

    def test_zero_page_size_is_rejected_without_500(self):
        self.assertEqual(self._get(page_size=0)["code"], 1000)

    def test_garbage_datetime_is_rejected_without_500(self):
        self.assertEqual(self._get(stime="garbage")["code"], 1000)

    def test_unknown_action_is_rejected_without_500(self):
        """未知动作码 ⇒ 1000（**不许静默当「全部」**，spec §8-7）。"""
        self.assertEqual(self._get(action="order.bogus")["code"], 1000)
        # 阳性对照：白名单内的值仍然是 0（否则「拒了」可能只是端点坏了）
        self.assertEqual(self._get(action="order.transition")["code"], 0)

    def test_over_length_key1_is_rejected_without_500(self):
        self.assertEqual(self._get(key1="长" * 256)["code"], 1000)

    def test_unknown_ordering_is_rejected_without_500(self):
        self.assertEqual(self._get(ordering="id")["code"], 1000)

    def test_empty_values_are_treated_as_not_provided(self):
        """前端清空筛选栏会送空串——那是「没选」，不是非法值（与既有读端点同口径）。"""
        self.assertEqual(self._get(stime="", etime="", action="", actor="", key1="")["code"], 0)


class OrderActionLogPermissionTests(TenantTestCase):
    """准入（spec §8-10）：**两个门互不包含**——这是 D2 的承重判据。"""

    def setUp(self):
        connection.set_tenant(self.tenant)

    def test_user_holding_only_this_node_gets_200(self):
        """阳性对照：只持 `order.platform_action_logs` 就够（⛔ 不需要 `system.logs`）。"""
        client = make_logged_in_client(
            username="tal-only-this", groups=("客服",), nodes=(NODE,)
        )
        self.assertEqual(client.get(ENDPOINT).status_code, 200)

    def test_user_holding_only_the_system_logs_node_gets_403(self):
        """⛔ 只持 `system.logs`（「系统操作日志」那一页的门）**看不到本页**（D2）。"""
        client = make_logged_in_client(
            username="tal-only-syslogs", groups=("客服",), nodes=("system.logs",)
        )
        response = client.get(ENDPOINT)
        self.assertEqual(response.status_code, 403, response.content[:300])
        # 阳性对照：同一个账号在**它自己的**那一页上是通的 ⇒ 403 不是「账号本身坏了」
        self.assertEqual(client.get("/api/audit/logs/").status_code, 200)

    def test_anonymous_gets_401(self):
        self.assertEqual(Client(SERVER_NAME="tenant.test.com").get(ENDPOINT).status_code, 401)


class OrderActionLogCrossTenantTests(TenantTestCase):
    """租户 A 的登录态看不到租户 B 的 `order.*` 行（`AGENTS.md` §4 的硬性要求）。

    判据用**行内唯一的显示值**（`summary`），**不用主键**——两个 schema 的主键序列
    各自从 1 开始，`filter(id=...)` 会命中「另一个租户自己的那一行」。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_tenant, _ = provision_tenant(
            schema_name="tal_api_iso_b", name="租户 B", domain="tal-iso-b.test.local"
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(username="tal-iso-admin", is_superuser=True)

    def test_rows_of_another_tenant_are_not_visible(self):
        with tenant_context(self.other_tenant):
            OperationLog.objects.create(
                log_type=10, action="order.transition", actor="B 的客服",
                order_no="B000000000000000001", summary="B 租户自己的订单日志",
            )
            # 阳性对照：B 的那一行确实落库了，否则下面的否定断言空洞通过
            self.assertTrue(
                OperationLog.objects.filter(summary="B 租户自己的订单日志").exists()
            )

        OperationLog.objects.create(
            log_type=10, action="order.transition", actor="A 的客服",
            order_no="A000000000000000001", summary="A 租户自己的订单日志",
        )
        body = self.client.get(ENDPOINT).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(summaries(body["data"]), ["A 租户自己的订单日志"])
