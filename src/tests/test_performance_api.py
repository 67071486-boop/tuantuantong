"""B1-4：绩效列表 `GET /api/business/performances`（spec §5；筛选项见 §16.4 #4）。

五条判据：
- **谁**：持 `my_performance.export` **或** `order.list` **任一**即能进列表（D-0219，
  2026-10-10；此前只有前者）；**看得到多少行**由**数据范围**决定——
  持 `order.list`（管理侧）看全部，其余（含代练员）只看**自己**的行（spec §6 第 11 行 / §13 Q3）；
- **筛选**：人 / 收入类型 / 审核状态 / 结账状态 / 时间区间，每条取向各一条用例；
  ⭐ 线 C 起时间区间再按 `date_type` 选列（录单 / 验收 / 结账）；
- **边界与畸形值**：空串按「没传」（P2 的既有口径），非法值 → **200 + code 1000**（不是 HTML 500，E-0010）；
- **数据权限接线**：`scope_queryset` 必须**真的被用上**——桩打在**返回值**上（清空），
  并配不接桩的对照（P3a 的教训：只测函数本身 = 接线没人守）；
- ⭐ **`date_type` 两份手写清单的结构网**（`DateTypeContractTests`，线 C P2-2）：
  序列化器的 `choices` 与 `performance_views.PERFORMANCE_DATE_FIELDS` 的键**恰好相等**。

⚠️ 金额断言钉**字面值**（E-0024）。
"""
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business.api.performance_views import PERFORMANCE_DATE_FIELDS
from apps.business.api.serializers import PerformanceListSearchSerializer
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance, ReviewStatus, SettleStatus
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import grant_games, make_logged_in_client

URL = "/api/business/performances/"

#: `src/` 根（本文件在 `src/tests/` 下）——回流网要按**相对路径**读生产模块的源码文本。
_SRC = Path(__file__).resolve().parents[1]

#: ：绩效列表的类级节点门 = `my_performance.export`
#: （`PerformanceViewSet.PERFORMANCE_PERMISSIONS["list"]`）。`groups` 只建 Group、不放行。
#: ⭐ **D-0219**：列表门是**任一即进**（本点 ∪ `order.list`）——本常量仍只
#: 表示「自助查看门」那一把钥匙（代练员形态：进得了列表、只看自己的行）；
#: 「只持 `order.list`」那一侧由 `test_order_list_node_alone_lists_all_rows` 单独钉。
PERF_LIST_NODES = ("my_performance.export",)

#: **管理侧**（看全量行）的判据 = `order.list`（spec §6 第 11 行 / §13 Q3 的借用）。
#: 不持它的人（如代练员）只看自己的行；两个节点合起来才是「客服/客服主管」的生产授予。
PERF_MANAGEMENT_NODES = PERF_LIST_NODES + ("order.list",)


class PerformanceApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.taker_a = User.objects.create_user(username="taker_a")
        self.taker_b = User.objects.create_user(username="taker_b")
        self.perf_a = self._perf("20260920000000000001", self.taker_a, "88.00", bonus="17.60")
        self.perf_b = self._perf("20260920000000000002", self.taker_b, "100.00")
        # 客服可看（spec §4.3）；下面各自换人时再登录
        self.client = make_logged_in_client(
            username="cs1", groups=("客服",), nodes=PERF_MANAGEMENT_NODES
        )
        # ⭐ 「按游戏权限收窄」：绩效列表按**请求方**的游戏权限收窄 ⇒
        #    这个夹具显式声明它持有本夹具唯一的那款游戏（否则列表恒空）。
        grant_games(User.objects.get(username="cs1"), self.game)

    def _perf(self, no, user, price, bonus="0.00", **kw):
        order = Order.objects.create(
            game=self.game, order_no=no, status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal(price),
        )
        return Performance.objects.create(
            order=order, emp_type=kw.pop("emp_type", EmpType.MAIN), user=user,
            temp_amount=Decimal(price), bonus_amount=Decimal(bonus), **kw
        )

    def _login(self, *groups, username, nodes=PERF_MANAGEMENT_NODES):
        self.client = make_logged_in_client(username=username, groups=groups, nodes=nodes)
        user = User.objects.get(username=username)
        # 同 `setUp`：换了登录人 ⇒ 新登录人也必须显式持有本夹具的游戏。
        grant_games(user, self.game)
        return user

    def _body(self, params=None):
        return self.client.get(URL, params or {}).json()

    def _nos(self, params=None):
        return sorted(row["order_no"] for row in self._body(params)["data"]["items"])

    # ── 谁可以看 ────────────────────────────────────────────────
    def test_cs_can_list_with_the_p2_pagination_shape(self):
        body = self._body()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        # P2 的分页形状 + ⭐ 的 `summary`（绩效页 `#total` 汇总行，
        # emp-emplist 补完计划 §2.2；六个金额的算式由
        # `tests/test_emp_emplist_backend.py::SummaryTests` 钉）。
        self.assertEqual(
            set(data), {"items", "total", "page", "page_size", "summary"}
        )
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["page_size"], 20)

    def test_reviewer_can_list(self):
        self._login("客服主管", username="reviewer1")
        self.assertEqual(self._body()["code"], 0)

    def test_list_requires_the_export_node(self):
        """列表的类级门 = `my_performance.export`（有 ⇒ 200 / 无 ⇒ 403）。

        ⚠️ Task 4 删掉了 `list()` 里那道职位门（旧 `{CS, REVIEWER}`）⇒ 今天这条 403
        **只有一个来源**：`PERFORMANCE_PERMISSIONS["list"]` 的节点门。
        旧版靠 `assertNotIn("绩效", msg)` 把节点门文案与职位门文案分开——职位门删掉后
        那条判别**恒真**（E-0063 的「恒真判据」形状），已删除。

        ⚠️ 拒绝侧是「客服主管 + **零节点**」：节点挂在 `Role` 上而 `Role.name` 唯一，
        同组的下一个 client 会继承前面用例挂上的授予；零节点是唯一与职位/组名无关的拒绝侧构造。
        """
        allowed = make_logged_in_client(
            username="cs_export", groups=("客服",), nodes=PERF_LIST_NODES
        )
        grant_games(User.objects.get(username="cs_export"), self.game)
        denied = make_logged_in_client(
            username="cs_no_export", groups=("客服主管",), nodes=()
        )
        self.assertEqual(allowed.get(URL).status_code, 200)
        self.assertEqual(denied.get(URL).status_code, 403)

    def test_order_list_node_alone_lists_all_rows(self):
        """⭐ D-0219（2026-10-10）：只持 `order.list`（**不持** 1163）⇒ 也能进列表，
        且数据范围按既有收口看到**全部行**（`order.list` 同时是管理侧标记）。

        这就是业务方报的那条现场：不勾「允许代练员或下家导出自己的绩效记录」（1163）时
        客服进不了绩效列表页、也就改不了打手绩效——裁定「有订单管理（修改）权限的人
        就该能进、能改」。改的是**列表那一道门**（任一即进）；`/adjust` 仍只认 1162
        （那条在 `tests/test_adjust_api.py::test_d0219_order_list_alone_does_not_grant_adjust`）。

        ⚠️ 节点是**字面量**（⛔ 不从 `PERFORMANCE_PERMISSIONS` 派生——自指断言在表被改坏时不会红）。
        ⚠️ 组名用本文件**独一份**的「D0219」：节点挂在**与组同名的 Role** 上（Role.name 唯一），
        用「客服」会继承 setUp 挂上的 `PERF_MANAGEMENT_NODES`（含 1163）⇒ 本条会退化为
        「既有行为」的假绿（`test_list_requires_the_export_node` 里登记过的同族坑）。
        ⚠️ 拒绝侧（两把钥匙都不持 ⇒ 403）由上面 `test_list_requires_the_export_node` 的
        零节点构造覆盖，这里只钉「第二把钥匙」这一侧。
        """
        self.client = make_logged_in_client(
            username="cs_only_order_list", groups=("D0219",), nodes=("order.list",)
        )
        grant_games(User.objects.get(username="cs_only_order_list"), self.game)

        body = self._body()

        self.assertEqual(body["code"], 0)
        # 非空哨兵 + 全部行（夹具里就两条别人的行：taker_a / taker_b）
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(
            {row["username"] for row in body["data"]["items"]}, {"taker_a", "taker_b"}
        )

    def test_taker_lists_only_own_rows(self):
        """代练员持 `my_performance.export` ⇒ 能进列表，但**只看得到自己的行**。

        改写来历：原用例 `test_taker_cannot_list` 断言「代练员 403」，那是因为当时
        `list()` 里有一道 `{Role.CS, Role.REVIEWER}` **职位门**（单轴化已删）。此后不能再靠
        职位拒绝（自定义职位本来就该能进），改用**数据范围**：不持 `order.list` 的人只看自己
        （spec §6 第 11 行 / §13 Q3）。

        ⚠️ 判据用**用户名**（租户内唯一），不是主键；`summary` 吃的是**同一份** queryset
        ⇒ 一并断言汇总只算自己那一行（证明数据范围落在与汇总同源的那一处）。
        """
        self.client = make_logged_in_client(
            username="taker_self", groups=("代练员",), nodes=PERF_LIST_NODES
        )
        me = User.objects.get(username="taker_self")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。绩效列表按**请求方**的
        # `StaffProfile.games` 收窄 ⇒ 不补本夹具的游戏，下面那条非空哨兵会先撞上空集
        # （本条走的是直接的 `make_logged_in_client`，绕过了 `_login` 里那次授权）。
        # 期望值一个字没改。
        grant_games(me, self.game)
        mine = self._perf("20260920000000000003", me, "50.00")

        body = self._body()

        self.assertEqual(body["code"], 0)
        rows = body["data"]["items"]
        # 非空哨兵 + 恰好只有自己那一条（空集过不了；两条别人的行也过不了）
        self.assertEqual({row["username"] for row in rows}, {"taker_self"})
        self.assertEqual([row["order_no"] for row in rows], [mine.order.order_no])
        self.assertEqual(body["data"]["summary"]["temp_amount"], "50.00")

    def test_service_sees_all_rows(self):
        """客服持 `order.list` ⇒ 看得到全部行（回归：数据范围不能把管理侧也收窄）。"""
        body = self._body()                     # setUp 的 client = 客服 + 管理侧节点

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(
            {row["username"] for row in body["data"]["items"]},
            {"taker_a", "taker_b"},
        )

    def test_anonymous_is_rejected(self):
        response = Client(SERVER_NAME="tenant.test.com").get(URL)
        self.assertEqual(response.status_code, 401)

    # ── 筛选 ────────────────────────────────────────────────────
    def test_filters_by_user(self):
        self.assertEqual(
            self._nos({"user": self.taker_a.id}), [self.perf_a.order.order_no]
        )
        self.assertEqual(
            self._nos({"user": self.taker_b.id}), [self.perf_b.order.order_no]
        )

    def test_filters_by_emp_type(self):
        self.perf_b.emp_type = EmpType.SALES
        self.perf_b.save(update_fields=["emp_type"])
        self.assertEqual(self._nos(), sorted([self.perf_a.order.order_no, self.perf_b.order.order_no]))
        self.assertEqual(self._nos({"emp_type": EmpType.MAIN}), [self.perf_a.order.order_no])
        self.assertEqual(self._nos({"emp_type": EmpType.SALES}), [self.perf_b.order.order_no])

    def test_filters_by_review_status(self):
        """`review_status` 可以是 NULL（还没交单）——按某个状态筛时**不**包含 NULL 行。"""
        self.perf_a.review_status = ReviewStatus.PENDING
        self.perf_a.save(update_fields=["review_status"])
        self.assertEqual(self._nos({"review_status": ReviewStatus.PENDING}), [self.perf_a.order.order_no])
        self.assertEqual(self._nos({"review_status": ReviewStatus.APPROVED}), [])

    def test_filters_by_settle_status(self):
        self.perf_a.settle_status = SettleStatus.SETTLED
        self.perf_a.save(update_fields=["settle_status"])
        self.assertEqual(self._nos({"settle_status": SettleStatus.SETTLED}), [self.perf_a.order.order_no])
        self.assertEqual(self._nos({"settle_status": SettleStatus.UNSETTLED}), [self.perf_b.order.order_no])

    def test_filters_by_date_range(self):
        from datetime import timedelta

        from django.utils import timezone

        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)
        yesterday = today - timedelta(days=1)
        self.assertEqual(self._nos({"date_from": tomorrow.isoformat()}), [])          # 未来 → 空
        self.assertEqual(len(self._nos({"date_from": yesterday.isoformat()})), 2)      # 过去 → 全中
        self.assertEqual(len(self._nos({"date_to": today.isoformat()})), 2)            # 含今天
        self.assertEqual(self._nos({"date_to": yesterday.isoformat()}), [])            # 截止昨天 → 空

    # ── ⭐ 线 C／⭐：`date_type` 选列 ──
    def test_date_type_selects_the_column_for_the_same_date_range(self):
        """⭐ 同一对 `date_from` / `date_to`，按 `date_type` 打**不同的列**。

        夹具（`created_at` 是 `auto_now_add` ⇒ 建行时定不了，只能建完 `.update()` 回写）：

        | 行 | 录单 `created_at` | 交单（订单列） | 结账 `settled_at` |
        |---|---|---|---|
        | `perf_a` | 1 天前 | 10 天前 | 20 天前 |
        | `perf_b` | 2 天前 | 3 天前 | **NULL**（未结账） |

        ⚠️ **本用例在批次 5 改过一次口径**：原本第 ③ 条钉的是 `reviewed` 档打
        `Performance.reviewed_at`；那一档已改名换列成 `completed`（打 `Order.completed_at`），
        所以这里换成 `submitted`（打 `order__submitted_at`）——**判据的形态一字未改**
        （「同一对窗口、换档 ⇒ 换一批行」），只是被钉的那一档换了名字与列。
        `completed` 档自己那两条（根因修复 + 空值行不出现）见下面两个用例。

        判别力：若实现把 `date_type` 忽略掉、永远打 `created_at`，下面三条期望
        （`[perf_a]` / `[perf_b]` / `[]`）会各自变成两行 ⇒ 三处都红。
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        today = timezone.localdate()
        Performance.objects.filter(pk=self.perf_a.pk).update(
            created_at=now - timedelta(days=1),
            settled_at=now - timedelta(days=20),
        )
        Performance.objects.filter(pk=self.perf_b.pk).update(
            created_at=now - timedelta(days=2),
            settled_at=None,
        )
        Order.objects.filter(pk=self.perf_a.order_id).update(
            submitted_at=now - timedelta(days=10),
        )
        Order.objects.filter(pk=self.perf_b.order_id).update(
            submitted_at=now - timedelta(days=3),
        )

        # ① 30 天宽窗 + **默认档** ⇒ 两行都在（阳性对照：这个区间本身有判别力，不是恒空）。
        wide = {"date_from": (today - timedelta(days=30)).isoformat()}
        self.assertEqual(len(self._nos(wide)), 2)
        # ② 同一个宽窗按**结账**筛 ⇒ **只有 `perf_a`**：`perf_b` 的 `settled_at` 是 NULL，
        #    `NULL >= 日期` 不为真 ⇒ **自然被排除**。这不是漏行（与结算记录页
        #    `settled_at__date` 的既有口径一致），是本线的承重判据。
        self.assertEqual(
            self._nos({**wide, "date_type": "settled"}),
            [self.perf_a.order.order_no],
        )
        # ③ 近 5 天的窄窗：按**默认（录单）**两行都在；换**交单**档只剩 `perf_b`（3 天前）
        #    —— `perf_a` 的交单时间（10 天前）在窗外，证明打的是订单那一列而不是录单。
        narrow = {
            "date_from": (today - timedelta(days=5)).isoformat(),
            "date_to": (today - timedelta(days=1)).isoformat(),
        }
        self.assertEqual(len(self._nos(narrow)), 2)
        self.assertEqual(
            self._nos({**narrow, "date_type": "submitted"}),
            [self.perf_b.order.order_no],
        )
        # ④ 同一个窄窗按**结账**筛 ⇒ 空：`perf_a` 的结账时间（20 天前）在窗外、
        #    `perf_b` 是 NULL（未结账行**不出现**）。
        self.assertEqual(self._nos({**narrow, "date_type": "settled"}), [])

    # ── ⭐⭐：五档，且**「验收」改接订单的完单时间** ──
    def test_date_type_completed_reads_the_order_completion_time_not_reviewed_at(self):
        """⭐ **本批的根因修复**：`completed` 档打的是 `Order.completed_at`，⛔ 不是 `reviewed_at`。

        ## 为什么这条是**承重**判据（不是「多测一档」）

        批次 4 把「验收时间」接到 `Performance.reviewed_at`，而那一列在**生产 kexing 的
        11,596 条绩效行上全空**（唯一写入点是 `/review/` 绩效审核，这个租户从不走）
        ⇒ 参数能选中列、单测全绿，**线上筛不出任何东西**（设计 §1 的实测读数）。

        ## 夹具与判别力（**两个不同的值**摆在两列上）

        | 变量 | `reviewed_at` | `Order.completed_at` |
        |---|---|---|
        | `perf_a` | **20 天前** | **3 天前** |
        | `perf_b` | 空 | 空 |

        窗口取「近 5 天」⇒：
        - 打 `order__completed_at`（正确实现）⇒ 只剩 `perf_a`；
        - 打 `reviewed_at`（批次 4 的实现）⇒ **空**（`perf_a` 的值在窗外、`perf_b` 是 NULL）

        ⇒ 把实现改回 `reviewed_at`，本用例当场红（这正是它存在的理由）。
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        today = timezone.localdate()
        Performance.objects.filter(pk=self.perf_a.pk).update(
            reviewed_at=now - timedelta(days=20),
        )
        Order.objects.filter(pk=self.perf_a.order_id).update(
            completed_at=now - timedelta(days=3),
        )
        Performance.objects.filter(pk=self.perf_b.pk).update(reviewed_at=None)
        Order.objects.filter(pk=self.perf_b.order_id).update(completed_at=None)
        window = {
            "date_from": (today - timedelta(days=5)).isoformat(),
            "date_to": (today - timedelta(days=1)).isoformat(),
        }

        self.assertEqual(
            self._nos({**window, "date_type": "completed"}),
            [self.perf_a.order.order_no],
        )
        # ⛔ `reviewed` 这个取值**不再被接受**（改名换列，不留两套契约）。
        body = self._body({**window, "date_type": "reviewed"})
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIsNone(body["data"])

    def test_date_type_published_and_submitted_read_the_order_columns(self):
        """⭐ 发单 / 交单两档打的是**订单**的两个新列（各自可空 ⇒ 空值行不出现）。

        夹具（两张单各自只在一列上有值，且值都落在近 5 天的窗内）：

        | 单 | `published_at` | `submitted_at` |
        |---|---|---|
        | `perf_a` | **2 天前** | 空 |
        | `perf_b` | 空 | **1 天前** |

        ⇒ 按 `published` 筛只剩 `perf_a`、按 `submitted` 筛只剩 `perf_b`。
        判别力：两档若都打到同一列（或都打到 `created_at`），两条期望必有一条红。
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        today = timezone.localdate()
        Order.objects.filter(pk=self.perf_a.order_id).update(
            published_at=now - timedelta(days=2), submitted_at=None,
        )
        Order.objects.filter(pk=self.perf_b.order_id).update(
            published_at=None, submitted_at=now - timedelta(days=1),
        )
        window = {
            "date_from": (today - timedelta(days=5)).isoformat(),
            "date_to": (today - timedelta(days=1)).isoformat(),
        }

        self.assertEqual(
            self._nos({**window, "date_type": "published"}),
            [self.perf_a.order.order_no],
        )
        self.assertEqual(
            self._nos({**window, "date_type": "submitted"}),
            [self.perf_b.order.order_no],
        )

    def test_each_date_type_excludes_rows_with_a_null_column(self):
        """⭐ 五档里**四档可空** ⇒ 每档「空值行不出现」各一条（设计 §2.4 的末条要求）。

        ## 两段（第二段是**非空哨兵**，缺了它第一段会被「实现恒返回空」蒙过去）

        ① 把两行的五列**全部**推到窗外（四列可空 ⇒ 清 `NULL`；`created_at` 是 `NOT NULL`
           ⇒ 推到 90 天前）⇒ 五档在同一对窗口下**都必须返回空集**；
        ② 逐档把 `perf_a` 那一档的列摆回窗内 ⇒ 那一档必须**只剩 `perf_a`**
           （`perf_b` 的那一列是 `NULL` ⇒ 不出现）。⛔ 这一段不是装饰：没有它，
           「某一档被写成常量空」也会让第 ① 段全绿（恒真判据的形状）。

        ⚠️ 每轮结束都把值还原：`created_at` 是 `NOT NULL` ⇒ 还原成「90 天前」、
        ⛔ 不是 `None`（`Performance.created_at` 上写 `None` 会 `NotNullViolation`，实测）。
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        today = timezone.localdate()
        outside = now - timedelta(days=90)
        Performance.objects.all().update(
            reviewed_at=None, settled_at=None, created_at=outside,
        )
        Order.objects.all().update(
            published_at=None, submitted_at=None, completed_at=None,
        )
        window = {
            "date_from": (today - timedelta(days=5)).isoformat(),
            "date_to": (today - timedelta(days=1)).isoformat(),
        }
        # ① 五档在这一组「全 NULL / 全在窗外」的夹具下**都**返回空集。
        for date_type in ("created", "published", "submitted", "completed", "settled"):
            with self.subTest(date_type=date_type, phase="all-outside"):
                self.assertEqual(self._nos({**window, "date_type": date_type}), [])
        # ② 非空哨兵：逐档把 `perf_a` 摆回窗内 ⇒ 只剩它。
        inside = now - timedelta(days=3)
        for date_type in ("created", "published", "submitted", "completed", "settled"):
            path = PERFORMANCE_DATE_FIELDS[date_type]
            column = path.split("__")[-1]
            queryset = (
                Order.objects.filter(pk=self.perf_a.order_id)
                if "__" in path
                else Performance.objects.filter(pk=self.perf_a.pk)
            )
            restore = outside if column == "created_at" else None
            with self.subTest(date_type=date_type, phase="sentinel"):
                queryset.update(**{column: inside})
                self.assertEqual(
                    self._nos({**window, "date_type": date_type}),
                    [self.perf_a.order.order_no],
                )
                queryset.update(**{column: restore})

    def test_date_type_absent_behaves_exactly_like_created(self):
        """⭐ 不传 `date_type` ⇒ 与显式 `date_type=created` **逐字相同**（默认值的承重判据）。

        把两行的 `created_at` 推到 10 / 20 天前，窗口取 [12 天前, 9 天前]：
        默认档必须命中 `perf_a`（10 天前）——**非空哨兵**排除了「两侧都恒空所以相等」的假绿；
        若默认值被改成别的档（例如 `settled`）或没有默认值，`absent` 会立刻变空 ⇒ 红。
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        today = timezone.localdate()
        Performance.objects.filter(pk=self.perf_a.pk).update(
            created_at=now - timedelta(days=10)
        )
        # `perf_b` 推到窗外（20 天前）：它是「区间真的在筛」的阴性对照。
        Performance.objects.filter(pk=self.perf_b.pk).update(
            created_at=now - timedelta(days=20)
        )
        window = {
            "date_from": (today - timedelta(days=12)).isoformat(),
            "date_to": (today - timedelta(days=9)).isoformat(),
        }

        absent = self._nos(window)

        self.assertEqual(absent, [self.perf_a.order.order_no])
        self.assertEqual(self._nos({**window, "date_type": "created"}), absent)

    def test_empty_string_filter_is_treated_as_not_provided(self):
        """`?emp_type=&user=`（前端「没选」常送空串）**不是**非法值，也不该筛出空集。"""
        self.assertEqual(len(self._nos({"emp_type": "", "user": "", "review_status": ""})), 2)

    # ── 畸形值：200 + 业务码，不是 500 ──────────────────────────
    def test_malformed_filters_are_business_codes_not_500(self):
        """畸形值得**可辨的**业务码，不是 HTML 500（E-0010）。

        ⚠️ `?page=abc` 走的是**另一条**分支：DRF 分页器抛 `NotFound`，
        而 `apps/core/exceptions.py` 把 `NotFound` 映射成 **`BUSINESS`(1002)**
        （「记录不存在或已被删除」），不是 `VALIDATION`。
        本用例的期望值是**实测**出来的（第一版写成 1000，实测 1002 ⇒ 按实测改），
        这正是「先读代码/实测确定返回形态，再写断言」那条纪律。
        """
        cases = [
            ({"emp_type": "BOGUS"}, ErrorCode.VALIDATION),
            ({"review_status": "BOGUS"}, ErrorCode.VALIDATION),
            ({"settle_status": "BOGUS"}, ErrorCode.VALIDATION),
            ({"user": "abc"}, ErrorCode.VALIDATION),
            ({"user": "0"}, ErrorCode.VALIDATION),      # min_value=1：0 在入口就拒
            ({"date_from": "garbage"}, ErrorCode.VALIDATION),
            # ⭐ 线 C：`date_type` 是 `ChoiceField` ⇒ 表外的取值走**同一条**入口校验，
            #    200 + 1000（⛔ 不是 400、也不是静默当成默认档）。
            ({"date_type": "BOGUS"}, ErrorCode.VALIDATION),
            # 对标证据件里的数字档（10/20/25/30/40）**不是**本参数的契约：25 也不合法。
            ({"date_type": "25"}, ErrorCode.VALIDATION),
            ({"page": "abc"}, ErrorCode.BUSINESS),      # 分页器的 NotFound 分支
        ]
        for params, expected in cases:
            with self.subTest(params=params):
                body = self._body(params)
                self.assertEqual(body["code"], expected)
                self.assertIsNone(body["data"])

    # ── 载荷 ────────────────────────────────────────────────────
    def test_payload_carries_the_fields_the_list_needs(self):
        row = next(
            r for r in self._body()["data"]["items"]
            if r["order_no"] == self.perf_a.order.order_no
        )
        self.assertEqual(row["username"], "taker_a")
        self.assertEqual(row["game_name"], "原神")
        self.assertEqual(Decimal(row["bonus_amount"]), Decimal("17.60"))   # 钉字面值
        self.assertEqual(Decimal(row["temp_amount"]), Decimal("88.00"))
        self.assertEqual(row["review_status"], None)
        self.assertEqual(row["settle_status"], SettleStatus.UNSETTLED)
        self.assertIn("id", row)                                          # `/adjust` 要用的 performance_id

    # ── ⭐ 二轮隐藏单的绩效行不进绩效读面 ──────────────
    def test_a_hall_shadow_order_is_not_listed(self):
        """镜像卡派生的隐藏单**即使带 MAIN 绩效行**也不进绩效列表 / 导出 / `summary`。

        ⚠️ 生产上隐藏单**没有** MAIN 行（它只是把卡同步给下家大厅的载体）；这里造一条
        带行的，是**防御性**判据——「没有行」这种隐式形状会随生命周期变化而失效，
        显式排除才是承重的。三面同源（列表 / 导出 / `summary` 都吃 `performance_queryset`）
        ⇒ 一条用例同时守住。
        """
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        card = make_mirror(
            game=self.game, bound_by=User.objects.get(username="cs1"),
            no="SYN-SHADOW-PERF",
        )
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261026000000000003",
            with_main=True, main_user=self.taker_a,
        )

        body = self._body()

        self.assertEqual(body["code"], 0, body)
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertIn(self.perf_a.order.order_no, nos)        # 正对照：真单看得见
        self.assertNotIn(hidden.order_no, nos)
        self.assertEqual(body["data"]["total"], 2, "隐藏单的绩效行混进了总数")
        self.assertEqual(
            Performance.objects.filter(order=hidden, emp_type=EmpType.MAIN).count(),
            1,
            "夹具自证：隐藏单**确实**有一条 MAIN 行（否则本条测不到排除）",
        )

    # ── 数据权限接线（判别力看变异）──────────────────────────────
    def test_scoping_seam_is_actually_used(self):
        """桩打在**返回值**上：`scope_queryset` 被换成「清空」，列表必须真的变空。

        只断言「被调用」是不够的——视图完全可以调用它却丢掉返回值。
        配一条不接桩的对照，排除「空集来自别的条件」。
        """
        self.assertEqual(len(self._nos()), 2)                    # 对照：本来看得见两条
        with mock.patch(
            "apps.business.api.performance_views.scope_queryset",
            # ⚠️ 桩的**签名**与真实调用一致：本线（按游戏权限收窄）起调用多传了
            #    `scope_filter=` ⇒ 不收 `**kwargs` 会以 `TypeError` 红。
                        #    **返回值与两条断言一字未改**（同 `test_p3a_isolation.py`
            #    那次处置；不是放宽判据）。
            side_effect=lambda user, qs, **kwargs: qs.none(),
        ) as seam:
            self.assertEqual(self._nos(), [])
        self.assertTrue(seam.called, "绩效列表没有经过 scope_queryset 卡点")


class DateTypeContractTests(SimpleTestCase):
    """⭐ 线 C 的**结构网**：`date_type` 的两份手写清单必须**恰好相等**（线 C P2-2）。

    ## 它守的是什么

    `date_type` 的合法取值写在**两处**，各有各的职责：

    - `PerformanceListSearchSerializer.date_type` 的 `choices` —— 管**收什么**（入口校验）；
    - `performance_views.PERFORMANCE_DATE_FIELDS` 的**键** —— 管**打哪一列**（施加区间）。

    只往 `choices` 加一档、忘了加映射，`performance_queryset` 里那句
    `PERFORMANCE_DATE_FIELDS[data["date_type"]]` 就会 **`KeyError` ⇒ HTML 500**，
    而不是本仓口径的业务码 `1000`；而**今天没有任何用例会红**（P2-2 评审指出）。
    本类就是那条「会红」的网。

    ## 两条断言的分工（⛔ 两条都不是「包含即可」）

    1. 两侧**恰好相等** ⇒ 挡住「加了一档只改一处」的**单边**漂移；
    2. 两侧都等于**字面值** `{"created", "published", "submitted", "completed", "settled"}`
       ⇒ 挡住「两边一起改名」（那种漂移下第 1 条仍然绿，但前端 `emp-emplist.ts::DATE_TYPE_MAP`
       发的字面值会**静默失配**——请求带一个后端不认的值、回 `1000`）。
       ⚠️ 期望值**写字面值**、⛔ 不从被测模块派生（E-0024 同族：自指断言不会红）。

    ⭐ **批次 5 · 线 E：三档 → 五档**，且原 `reviewed` **改名换列**成 `completed`
    （打 `order__completed_at`，不再打生产上零写入的 `Performance.reviewed_at`）。
    第三条断言把这件事钉成**恰好集合**：`reviewed` 作为一个取值**不存在**了（⛔ 不留两套契约）。

    ⚠️ **比的是什么形状**（读 `rest_framework/fields.py:1477-1491` 核实，⛔ 不是猜的）：
    `ChoiceField.choices` 是 `_set_choices` 里 `flatten_choices_dict(...)` 的产物
    （`fields.py:142-158`），即 `{取值: 显示名}` 的 dict ⇒ `set(field.choices)`
    就是**取值**的集合，与 `PERFORMANCE_DATE_FIELDS` 的**键**同形可比。
    """

    def test_date_type_choices_and_column_map_are_exactly_the_same_set(self):
        choices = set(PerformanceListSearchSerializer().fields["date_type"].choices)

        # ① 两份手写清单恰好相等（单边漂移 ⇒ 红）
        self.assertEqual(choices, set(PERFORMANCE_DATE_FIELDS))
        # ② 两侧都等于对外的字面值契约（两边一起改名 ⇒ ① 绿而本条红）
        five = {"created", "published", "submitted", "completed", "settled"}
        self.assertEqual(choices, five)
        self.assertEqual(set(PERFORMANCE_DATE_FIELDS), five)
        # ③ `reviewed` **不再被接受**（改名换列，⛔ 不留两套契约）
        self.assertNotIn("reviewed", choices)
        self.assertNotIn("reviewed", set(PERFORMANCE_DATE_FIELDS))

    def test_the_default_is_still_created(self):
        """默认档仍是 `created`（录单时间）——「不传它时行为与改动前逐字相同」靠它。"""
        field = PerformanceListSearchSerializer().fields["date_type"]
        self.assertEqual(field.default, "created")


#: ⭐⭐ **的回流网**：`date_type` 每一档映射到的列，**由谁写**。
#:
#: ## 为什么要有这张表（曾经的实际缺陷）
#:
#: 批次 4 把「验收时间」接到 `Performance.reviewed_at` 上：接线是对的、单测全绿、
#: `DateTypeContractTests` 也绿（两份清单恰好相等），而**生产 kexing 的 11,596 条绩效行上
#: 那一列全空**（唯一写入点是 `/review/` 绩效审核，这个租户从不走）⇒ 线上筛不出任何东西。
#: **「参数能选中一列」与「这一列有人写」是两件事**，而当时没有任何判据把后者钉住。
#:
#: ⇒ 本表把每一档的列与**它的生产写入模块**写在一起，由下面三条断言逐条对拍：
#:   ① 这张表的键与 `PERFORMANCE_DATE_FIELDS` 的键**恰好相等** ⇒ 给读面接新一档时，
#:      **同一次**必须回答「这一列由谁写」（漏了这里就红）；
#:   ② 每条的 `column` 与映射里的列名**逐字相同**；
#:   ③ 每条 `sources` 里列出的生产模块，其**源码 AST 里真的把那个列名当关键字参数名用过**
#:      （`f(published_at=…)` / `.update(submitted_at=…)` / `save(update_fields=[…])` 之类）
#:      —— ⛔ 不是拿注释或文档对拍（那正是批次 4 那种「写在文档里」的假接线）。
#:
#: ⚠️ **证明力边界（如实登记）**：③ 回答的是「源码里确实有这么一次写入」，
#: **不是**「生产上真的写过」。后者只能由生产读数回答（批次 4 的教训正是「单测绿 ≠ 线上有数据」）
#: ——所以每一条另附 `witness`：一条**跑得动**的用例的路径，那是「这条生产路径确实写得出值」
#: 的可重跑证据。
DATE_TYPE_COLUMN_WRITERS = {
    "created": {
        "column": "created_at",
        # `auto_now_add=True`：写入点是模型字段定义本身，⛔ 不是某个业务函数。
        "sources": ("apps/business/models.py",),
        # 夹具经 `Performance.objects.create(...)` 建行 ⇒ 走的就是这条生产路径；
        # `created_at` 由 `auto_now_add` 写。
        "witness": (
            "tests.test_performance_api.PerformanceApiTests"
            "::test_payload_carries_the_fields_the_list_needs"
        ),
    },
    "published": {
        "column": "order__published_at",
        # 写入实现只有一处：`order_time_fields._mark`（两个 `mark_*` 都经它）。
        # ⚠️ **调用点（谁来触发）不在这里列、也不靠本网守**：那一半由
        # `tests/test_order_time_fields.py::TimeFieldWritePathNetTests` 的**恰好集合**网钉
        # （① 抛到大厅 = `order_status.apply_transition` 传了 `hall_publisher`；
        #   ② 第一次接手 = `claim.claim_order` 与 `performance.set_taker`）。
        # 本表只回答「这一列的写入实现**在源码里真的存在**」。
        "sources": ("apps/business/order_time_fields.py",),
        # 另有 `/claim` 与 `/assign` 各一条，都断在数据库行上。
        "witness": (
            "tests.test_order_column_fields.PublishedAtWritePathTests"
            "::test_throw_to_hall_writes_published_at_to_the_database_row"
        ),
    },
    "submitted": {
        "column": "order__submitted_at",
        # 同上：写入实现只有一处（`order_time_fields._mark`，经 `mark_submitted`）；
        # 「谁能触发」那一半由 `TimeFieldWritePathNetTests` 的 `PENDING_REVIEW_PATHS` 钉
        # （本轮穷举出**三条**：唯一写入口 `apply_transition`，以及 `peer_inbound` 那两条
        # **绕过状态机**的直写路——它们当初漏掉过一次，正是本网存在的理由）。
        "sources": ("apps/business/order_time_fields.py",),
        "witness": (
            "tests.test_order_column_fields.SubmittedAtWritePathTests"
            "::test_submit_writes_submitted_at_to_the_database_row"
        ),
    },
    "completed": {
        "column": "order__completed_at",
        # 唯一写入点：`apply_transition` 里 `target == COMPLETED` 写、离开清
        "sources": ("apps/business/order_status.py",),
        "witness": (
            "tests.test_order_column_fields.CompletedAtWritePathTests"
            "::test_entering_completed_writes_completed_at_to_the_database_row"
        ),
    },
    "settled": {
        "column": "settled_at",
        # 人工和在线结账共用的落库模块给本轮结清的绩效写这一列。
        "sources": ("apps/business/settlement_persistence.py",),
        "witness": (
            "tests.test_p4_settle_meta.SettledMetaWriteTests"
            "::test_T1_settling_writes_the_actor_and_a_non_null_time_on_every_row"
        ),
    },
}


def _written_column_names(path):
    """`path` 的源码 AST 里，**真的处于写入形态**的列名集合。

    ## 为什么不能只看「列名出现过」

    第一版把判据写成「列名出现在关键字参数名**或任意字符串字面量**里」⇒ 一个只**读**这一列的
    文件也能满足（实测：`apps/openapi/api/order_extra_views.py` 只在载荷示例里写了一句
    `"submitted_at": None` 就过）。那是**假绿**——本网存在的理由就是「这一列得有人**写**」。

    ## 现在只认四种**写入形态**（其余一律不算）

    ① 赋值：`<任意>.published_at = …`；
    ② `**写入方法**` 的列名**关键字实参**：`.update(published_at=…)` / `.create(…)` /
       `.get_or_create(…)` / `.save(… published_at=…)`（`filter(...)` / `values(...)` 之类**不算**）；
    ③ `save(update_fields=[…, "<列名>", …])` / `bulk_update(rows, [<列名>])` 里的**字符串元素**；
    ④ `defaults={"<列名>": …}` 里的**字符串键**（`update_or_create` / `get_or_create` 的写法）；
    ⑤ `**动态列写助手**` 的 `field="<列名>"` 实参（`order_time_fields._mark(order, field=…)`
       就是这一形态——它的列名是**算出来的**，所以永远不可能以 ①–④ 的形式出现）。

    ⚠️ 证明力边界：它回答「源码里有这么一次**写入形态**」，⛔ 不回答「生产上真的写过」、
    也不回答「那个写入点会被执行到」——后两者看 `witness`（可重跑用例）与生产读数。
    ⚠️ ⑤ 是**窄口径**（只认 `field=<字符串>` 这一个关键字）：写别的关键字名的动态助手
    本网看不见 ⇒ 那时把它登记进 ①–④ 里的某一种，或扩大 ⑤。反向对照见
    `test_the_write_form_checker_rejects_a_read_only_file`（⛔ 不让「只读文件」混过去）。
    """
    import ast

    write_methods = {
        "update", "update_or_create", "create", "get_or_create",
        "bulk_create", "bulk_update", "save", "set", "save_base", "add",
    }
    names = set()

    def _key_strings(dict_node):
        """`{...}` 里的**字符串键**（用于 `defaults={"published_at": …}`）。"""
        if not isinstance(dict_node, ast.Dict):
            return
        for key in dict_node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)

    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute):
                    # ① 实例属性赋值：`order.published_at = …`
                    names.add(target.attr)
                elif isinstance(target, ast.Name):
                    # ① 模型字段声明：`created_at = models.DateTimeField(…)`
                    names.add(target.id)
        elif isinstance(node, ast.Call):
            func = node.func
            method = func.attr if isinstance(func, ast.Attribute) else None
            if method in write_methods:
                for kw in node.keywords:
                    if kw.arg:
                        names.add(kw.arg)
                    if kw.arg == "defaults":
                        _key_strings(kw.value)
                    if kw.arg == "update_fields" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        for element in kw.value.elts:
                            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                                names.add(element.value)
                # `bulk_update(rows, ["published_at"])` 的第二个位置实参
                if method == "bulk_update" and len(node.args) >= 2:
                    fields = node.args[1]
                    if isinstance(fields, (ast.List, ast.Tuple)):
                        for element in fields.elts:
                            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                                names.add(element.value)
            # ⑤ 动态列写助手的 `field="<列名>"`（⛔ 只认这一个关键字名，口径见 docstring）
            for kw in node.keywords:
                if (
                    kw.arg == "field"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    names.add(kw.value.value)
    return names


def _witness_test_exists(dotted):
    """`模块.类::用例名` 形态的 `witness` **真的指得到一条用例**（独立评审 P2-1）。

    ⚠️ 第一版只写了 `assertTrue(entry.get("witness"))` ⇒ 评审把它变形成
    `tests.zzz_nope.NopeClass::test_does_not_exist_at_all` 照样绿。现在照
    `tests/test_order_time_fields.py` 里那一版 `importlib` 校验同形。
    """
    import importlib

    target, _, test_name = dotted.partition("::")
    if not test_name:
        return False
    module_name, _, class_name = target.rpartition(".")
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError):
        return False
    return hasattr(cls, test_name)


class DateTypeColumnWriterContractTests(TenantTestCase):
    """⭐ 批次 5 · 线 E 的**教训回流网**：每一档的列都必须登记「由谁写」，且那名**真的写着它**。

    三条断言各能被一个具体的改坏弄红：

    ① 这张表的键与 `PERFORMANCE_DATE_FIELDS` 的键**恰好相等** ⇒ 给读面接新一档时漏登记
       「这一列由谁写」⇒ 红；
    ② 每条的 `column` 与映射里的列名逐字相同 ⇒ 映射改了、本表忘了改 ⇒ 红；
    ③ 每条 `sources` 的源码 AST 里真的把那个列名当关键字参数名（或 `update_fields` 里的
       字符串）用过 ⇒ 登记了一个**不存在**的写入点 ⇒ 红。
    """

    def test_every_date_type_has_a_registered_writer(self):
        self.assertEqual(
            set(DATE_TYPE_COLUMN_WRITERS),
            set(PERFORMANCE_DATE_FIELDS),
            "`date_type` 的档位与「这一列由谁写」的登记表不是恰好同一集合："
            "新增/改名一档时，必须**同一次**在 DATE_TYPE_COLUMN_WRITERS 里回答"
            "『这一列由谁写、生产上有没有写过』（批次 4 把验收时间接到零写入的"
            "`Performance.reviewed_at` 上，就是这么漏掉的）",
        )

    def test_registered_columns_match_the_mapping(self):
        for date_type, entry in DATE_TYPE_COLUMN_WRITERS.items():
            with self.subTest(date_type=date_type):
                self.assertEqual(entry["column"], PERFORMANCE_DATE_FIELDS[date_type])

    def test_each_registered_writer_source_really_writes_that_column(self):
        """③ 写入点不是「写在文档里」——拿生产模块的**源码 AST** 的**写入形态**对拍。

        ⚠️ 判据收紧过（独立评审 P2-2）：从「列名出现过」改成「**真的处于写入形态**」
        （赋值 / 写入方法的 kwarg / `update_fields` 列表元素 / `defaults` 键）——
        否则一个只**读**这一列的文件也能满足。
        ⚠️ 证明力边界见 `_written_column_names` 的 docstring：它回答「源码里有这么一次写入」，
        ⛔ 不回答「生产上真的写过」（那要看 `witness` 那条用例 + 生产读数）。
        """
        for date_type, entry in DATE_TYPE_COLUMN_WRITERS.items():
            # 五种列名在代码里都取**末段**（`order__published_at` → `published_at`）。
            literal = entry["column"].split("__")[-1]
            for source in entry["sources"]:
                path = _SRC / source
                with self.subTest(date_type=date_type, source=source):
                    self.assertTrue(path.is_file(), f"{source} 不存在")
                    self.assertIn(
                        literal,
                        _written_column_names(path),
                        f"{source} 的源码里看不到对 `{literal}` 的**写入形态**（赋值 / 写入方法的"
                        f"关键字实参 / `update_fields` 元素 / `defaults` 键）—— `{date_type}` "
                        f"那一档登记的写入点已经不成立了（要么实现搬了家、要么这张表是照着愿望填的）",
                    )

    def test_the_write_form_checker_rejects_a_read_only_file(self):
        """⭐ **反向对照**（独立评审 P2-2 的实测）：只**读**这一列的文件不能满足③。

        语料：`apps/openapi/api/order_extra_views.py` —— 它只在载荷**示例**里写了一句
        `"submitted_at": None`，评审指出第一版判据（「列名出现过」）对它**假绿**。
        现在它是**阴性对照**：本用例断言它**不**算写入 ⇒ 判据收紧有效。
        """
        read_only = _SRC / "apps/openapi/api/order_extra_views.py"
        self.assertTrue(read_only.is_file())
        self.assertIn("submitted_at", read_only.read_text(encoding="utf-8"))   # 哨兵：语料里确实有它
        self.assertNotIn("submitted_at", _written_column_names(read_only))

    def test_every_registered_witness_points_at_a_real_test(self):
        """⭐ 每条 `witness` 都必须指得到**一条真用例**（独立评审 P2-1）。

        判别力：把任一条 `witness` 改成 `tests.zzz_nope.NopeClass::test_does_not_exist_at_all`
        ⇒ 当场红（第一版只判「非空字符串」，那种变形照样绿）。
        """
        for date_type, entry in DATE_TYPE_COLUMN_WRITERS.items():
            with self.subTest(date_type=date_type):
                witness = entry.get("witness")
                self.assertTrue(witness, f"`{date_type}` 没登记 `witness`")
                # ⛔ 值必须是**纯路径形态**（`模块.类::用例名`）：中文说明写注释里，别塞进值。
                self.assertRegex(
                    witness, r"^tests\.[A-Za-z0-9_.]+::[A-Za-z0-9_]+$",
                    f"`{date_type}` 的 `witness` 不是「模块.类::用例名」的纯路径形态：{witness!r}"
                    "（说明文字请写进注释）",
                )
                self.assertTrue(
                    _witness_test_exists(witness),
                    f"`{date_type}` 的 witness 指不到一条真用例：{witness}",
                )
