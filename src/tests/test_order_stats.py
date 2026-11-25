"""P3e Task 3：接单统计端点（`GET /api/business/order-stats`）的判别力用例。

被测契约（计划的 Interfaces **逐字**，对照物 = 对标账号页 `.recTotalDiv`「接单统计」卡片 /
`/UserAjax/GetRecOrderTotalList`）：

- **行集合 = 该租户全部「有效状态正常」的子账号**（`User.is_active=True`，复用账号线
  `list_accounts`）——**窗口只改数值、不改名单** ⇒ 没接单的人也在表里
  （`orderCount=0` / `lastRecOrderDate="无"`）；禁用的人排除（对标橙字那半句）。
- **排序** = `orderCount` 降序 → `uid` 升序（对标同分顺序不稳定 ⇒ 我方加稳定 tie-break），
  `idx` = **全量**行序 + 1（不是「本页行序」）。
- **行字段 8 个（逐字）**：`idx` / `uid` / `userName` / `orderCount` / `noSettAmount` /
  `complateRate` / `createUserDate` / `lastRecOrderDate`。
- 窗口 `stime` / `etime` **必填、`yyyy-MM-dd`、含两端**。
- **`gameName` = 按「这个人的游戏权限」（`StaffProfile.games`）筛行集合**：没有该权限的人整行消失，
  **剩下的人的数字一个都不变**（对标实测：`gameName=原神` 时 `count` 102→101、共同 uid 的
  `orderCount` 差异 = 0；卡片那个下拉的 i18n 标签就叫「游戏权限」）。
  游戏名**在租户里不存在** ⇒ `1000`；游戏**存在但没人有该权限** ⇒ **正常成功 + 空表**。
- **`limit` 上限 = 2000**（本端点专用分页类；照对标 `limits` 的最大档）——**超限 / 非法一律
  `1000` + msg**，**绝不静默截断**；全局 `EnvelopePagination` 不动。
- **错误三分支**（缺参 / 带时分秒 / 未知游戏名）**一律显式 `1000` + 说真话的 msg**
  ——**刻意改掉**对标的「HTTP 200 + `code:0` + 空表（39 字节）」那种静默空。

⚠️ **判别力纪律**（AGENTS.md §5 / E-0009 / E-0024）：
- 每条断言打**值**（字面量），不打常量、不打「有一行」；
- 负向断言前先证明**同一请求能看到正例**（否则空表/空集能让 `assertNotIn` 假通过）；
- 跨租户判据用**租户内唯一、且两租户取值不同的名字**，**不用主键**（两个 schema 的
  `auth_user` 序列各自从 1 开始）。
"""
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    SettleStatus,
)
from apps.core.error_codes import ErrorCode
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

User = get_user_model()

#: RBAC 节点 code（`apps/rbac/catalog.py` 里已有；本线把它接上并把 `available` 翻真）。
ORDER_STATS_NODE = "system_user.view_order_stats"
URL = "/api/business/order-stats"

GAME_A = "原神"
GAME_B = "崩坏：星穹铁道"

DOMAIN_A = "tenant.test.com"
DOMAIN_B = "p3e-stats-b.test.local"
SCHEMA_B = "p3e_stats_b"
#: 两个租户的标记账号名（**取值不同**；跨租户判据不许用主键）。
MARKER_A = "iso-stats-only-in-a"
MARKER_B = "iso-stats-only-in-b"


def _no(n):
    """20 位订单号（P2 起订单的对外标识是 `order_no`）。"""
    return f"20260923{n:012d}"


def _at(day, hour=12, minute=0):
    """窗口用例的固定时刻（`Asia/Shanghai`，`USE_TZ=True`）。"""
    return timezone.make_aware(datetime(2026, 9, day, hour, minute))


class _StatsFixtureMixin:
    """两个测试类共用的夹具：游戏、账号、带「接单时刻」的绩效行。"""

    def setUp(self):
        # ⚠️ 必须转调：`TenantTestCase.setUp` 才是「把连接钉到本租户 schema」的落点。
        super().setUp()
        self.game = Game.objects.create(name=GAME_A)
        self.other_game = Game.objects.create(name=GAME_B)
        self._seq = 0

    def _account(self, username, *, active=True, first_name=""):
        user = User.objects.create_user(username=username)
        if first_name:
            user.first_name = first_name
            user.save(update_fields=["first_name"])
        if not active:
            user.is_active = False
            user.save(update_fields=["is_active"])
        return user

    def _profile(self, user, *games):
        """给账号建档案并授予**游戏权限**（`StaffProfile.games`）。

        `gameName` 筛行集合的判据就是这个 M2M（`apps/accounts/models.py` 的注释：
        「游戏权限：至少一款」）。
        """
        profile = StaffProfile.objects.create(user=user)
        profile.games.set(games)
        return profile

    def _claim(
        self,
        user,
        *,
        claimed_at,
        status=OrderStatus.IN_PROGRESS,
        game=None,
        temp="88.00",
        bonus="0.00",
        settle_status=SettleStatus.UNSETTLED,
        deduct_mode="",
        deduct_value="0.00",
        adjustments=(),
    ):
        """造一条「某人接的单」：订单 + MAIN 绩效行（`claimed_at` = 接单时刻）。

        ⚠️ 直接摆行、**不经** `/claim`：窗口边界要的是**确定的接单时刻**，而抢单路径
        写的是 `timezone.now()`（时间不可控）。「抢单真的写这一列」由
        `tests/test_claim.py` 与 `tests/test_dispatch_api.py` 各自的写入用例钉住。
        """
        self._seq += 1
        order = Order.objects.create(
            game=game or self.game,
            order_no=_no(self._seq),
            status=status,
            hall_price=Decimal("88.00"),
        )
        perf = Performance.objects.create(
            order=order,
            user=user,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal(temp),
            bonus_amount=Decimal(bonus),
            deduct_mode=deduct_mode,
            deduct_value=Decimal(deduct_value),
            settle_status=settle_status,
            claimed_at=claimed_at,
        )
        for amount, settle in adjustments:
            PerformanceAdjustment.objects.create(
                performance=perf,
                kind=PerformanceAdjustment.Kind.MANUAL,
                amount=Decimal(amount),
                reason="接单统计用例夹具",
                settle_status=settle,
            )
        return order, perf

    def _adjustment(self, perf, amount, *, settle_status=SettleStatus.UNSETTLED):
        return PerformanceAdjustment.objects.create(
            performance=perf,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="接单统计用例夹具",
            settle_status=settle_status,
        )


class OrderStatsApiTests(_StatsFixtureMixin, TenantTestCase):
    """端点契约、口径与错误分支。"""

    def setUp(self):
        super().setUp()
        # ⚠️ `_grant_nodes` 只把节点挂到**用户所在的 Group** 上 ⇒ 必须同时给 `groups`，
        #    否则节点一个都挂不上（症状是 403，看起来像判据写错）。
        self.client = make_logged_in_client(
            username="stats-viewer", groups=("客服",), nodes=(ORDER_STATS_NODE,),
            # ⚠️ 显式 opt-out（默认值翻面）：本类只测端点契约，游戏由
            # 下面那句 `grant_games(self.viewer, self.game, self.other_game)` 逐款补。
            games=[],
        )
        self.viewer = User.objects.get(username="stats-viewer")
        # ⚠️ **本夹具显式声明请求方持有哪些游戏**（按游戏权限收窄之后）：
        # 取数层按**看统计的人**的游戏权限收窄被统计的行 ⇒ 不授权的请求方什么都看不到
        # （不是回归，是新契约）。这里给 A + B 两款，本类既有夹具的接单都落在这两款上，
        # 因而它们的数字逐字段不变；只用得到单款的用例自己建自己的客户端。
        grant_games(self.viewer, self.game, self.other_game)

    # ── 工具 ───────────────────────────────────────────────────────
    def _get(self, **params):
        response = self.client.get(URL, params)
        return response, response.json()

    def _rows(self, body):
        """取信封里的 `items`，先断言端点本身成功（前置哨兵）。"""
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _row_of(self, body, username):
        rows = [row for row in self._rows(body) if row["userName"] == username]
        self.assertEqual(len(rows), 1, f"{username} 在统计表里应恰好一行：{self._rows(body)}")
        return rows[0]

    def _window(self, **extra):
        """一个覆盖所有夹具日期的窗口（`2026-09-01` ~ `2026-09-30`）。"""
        return {"stime": "2026-09-01", "etime": "2026-09-30", **extra}

    # ── 行集合 ─────────────────────────────────────────────────────
    def test_rows_cover_every_active_account_including_those_without_a_claim(self):
        """行集合 = 全部**有效**账号（窗口只改数值、不改名单）；禁用的人排除。"""
        taker = self._account("stats-taker")
        self._claim(taker, claimed_at=_at(10))
        idle = self._account("stats-idle")
        disabled = self._account("stats-disabled", active=False)
        self._claim(disabled, claimed_at=_at(10))     # 有单，但账号被禁用 ⇒ 不进表

        body = self._get(**self._window())[1]

        names = [row["userName"] for row in self._rows(body)]
        self.assertIn("stats-taker", names)
        self.assertIn("stats-idle", names)            # 没接单的人**也在表里**
        self.assertNotIn("stats-disabled", names)     # 禁用的人排除
        # 数值：没接单的人 `orderCount=0` / `lastRecOrderDate="无"`（照对标，**不是** null）
        idle_row = self._row_of(body, "stats-idle")
        self.assertEqual(idle_row["orderCount"], 0)
        self.assertEqual(idle_row["lastRecOrderDate"], "无")
        self.assertEqual(idle_row["complateRate"], 0.0)
        self.assertEqual(idle_row["noSettAmount"], 0.0)
        # 正向对照：禁用的人那条单**真的存在**（否则 `assertNotIn` 可能由「它本来就没有行」凑出来）
        self.assertTrue(
            Performance.objects.filter(user=disabled, claimed_at__isnull=False).exists()
        )
        # 视图账号自己也是活跃子账号 ⇒ 在表里（`orderCount=0`）
        self.assertIn("stats-viewer", names)

    def test_row_carries_exactly_the_eight_contracted_keys(self):
        """行字段是**逐字契约**：多一个少一个都要红（前端按它取值）。"""
        taker = self._account("stats-keys")
        self._claim(taker, claimed_at=_at(10))

        row = self._row_of(self._get(**self._window())[1], "stats-keys")

        self.assertEqual(
            sorted(row),
            [
                "complateRate",
                "createUserDate",
                "idx",
                "lastRecOrderDate",
                "noSettAmount",
                "orderCount",
                "uid",
                "userName",
            ],
        )
        self.assertEqual(row["uid"], taker.pk)

    def test_user_name_is_the_display_name_and_falls_back_to_the_login_account(self):
        """`userName` 对标物 = 我们的 `display_name`（`accounts/serializers.py` 的映射表）；
        它为空时回落**登录账号**（列标题「姓名/名称」）。"""
        named = self._account("stats-named", first_name="有姓名的")
        self._account("stats-unnamed")

        body = self._get(**self._window())[1]

        self.assertEqual(self._row_of(body, "有姓名的")["uid"], named.pk)
        self.assertEqual(self._row_of(body, "stats-unnamed")["userName"], "stats-unnamed")

    def test_create_user_date_comes_from_the_staff_profile(self):
        """`createUserDate` = `StaffProfile.created_at` 的**日期**（`yyyy-MM-dd`）。"""
        profiled = self._account("stats-profiled")
        profile = StaffProfile.objects.create(user=profiled)
        StaffProfile.objects.filter(pk=profile.pk).update(
            created_at=timezone.make_aware(datetime(2026, 1, 2, 3, 4, 5))
        )
        self._account("stats-no-profile")

        body = self._get(**self._window())[1]

        self.assertEqual(self._row_of(body, "stats-profiled")["createUserDate"], "2026-01-02")
        # 没有档案行（例如 `seed_tenant` 灌的 `admin`）⇒ **空串**，不是编一个日期
        self.assertEqual(self._row_of(body, "stats-no-profile")["createUserDate"], "")

    # ── 窗口 ───────────────────────────────────────────────────────
    def test_the_window_includes_both_ends_and_excludes_the_neighbours(self):
        """`stime` / `etime` **含两端**：早一天 / 当天 / 晚一天三条对照。"""
        taker = self._account("stats-window")
        self._claim(taker, claimed_at=_at(10, 0, 0))       # 边界日（stime）
        self._claim(taker, claimed_at=_at(11, 23, 59))
        self._claim(taker, claimed_at=_at(12, 0, 0))       # 边界日（etime）
        self._claim(taker, claimed_at=_at(13, 0, 0) - timedelta(microseconds=1))
        self._claim(taker, claimed_at=None)

        # 三天全包，包含窗口结束前最后一微秒。
        row = self._row_of(
            self._get(stime="2026-09-10", etime="2026-09-12")[1], "stats-window"
        )
        self.assertEqual(row["orderCount"], 4)
        self.assertEqual(row["lastRecOrderDate"], "2026-09-12")

        # 只包前两日 ⇒ 2 条（etime 当天**含**）
        self.assertEqual(
            self._row_of(
                self._get(stime="2026-09-10", etime="2026-09-11")[1], "stats-window"
            )["orderCount"],
            2,
        )
        # 只包后两日 ⇒ 3 条（stime 当天**含**）
        self.assertEqual(
            self._row_of(
                self._get(stime="2026-09-11", etime="2026-09-12")[1], "stats-window"
            )["orderCount"],
            3,
        )
        # 早一天 / 晚一天 ⇒ 0 条（不是「回落成全部」）
        early = self._row_of(
            self._get(stime="2026-09-09", etime="2026-09-09")[1], "stats-window"
        )
        self.assertEqual(early["orderCount"], 0)
        self.assertEqual(early["lastRecOrderDate"], "无")
        late = self._row_of(
            self._get(stime="2026-09-13", etime="2026-09-13")[1], "stats-window"
        )
        self.assertEqual(late["orderCount"], 0)

    def test_the_window_accepts_the_earliest_and_latest_calendar_dates(self):
        taker = self._account("stats-extreme-window")
        self._claim(taker, claimed_at=_at(10))
        self._claim(taker, claimed_at=None)

        for stime, etime in (
            ("0001-01-01", "2026-09-10"),
            ("2026-09-10", "9999-12-31"),
        ):
            with self.subTest(stime=stime, etime=etime):
                row = self._row_of(
                    self._get(stime=stime, etime=etime)[1],
                    "stats-extreme-window",
                )
                self.assertEqual(row["orderCount"], 1)
                self.assertEqual(row["lastRecOrderDate"], "2026-09-10")

    # ── 完单率 ─────────────────────────────────────────────────────
    def test_complate_rate_counts_pending_review_and_completed_only(self):
        """完单率 = (待验收 + 已完成) ÷ 窗口内接单数；**百分比数值**（前端拼 `%`）。

        口径依据：对标 `cols` 的 templet 原文「订单完成率(只计算待验收+已完成的)」，
        且它用 `res.complateRate + '%'` 拼接 ⇒ 值本身是百分数（不是 0~1 的比率）。
        判别力：只数「已完成」⇒ 25.0；把「问题单/已退单」也算进来 ⇒ 75.0。
        """
        taker = self._account("stats-rate")
        for status in (
            OrderStatus.PENDING_REVIEW,
            OrderStatus.COMPLETED,
            OrderStatus.ISSUE,
            OrderStatus.REFUNDED,
        ):
            self._claim(taker, claimed_at=_at(10), status=status)

        row = self._row_of(self._get(**self._window())[1], "stats-rate")

        self.assertEqual(row["orderCount"], 4)          # 四种状态都算「接单」
        self.assertEqual(row["complateRate"], 50.0)

    # ── 还未结算的钱 ────────────────────────────────────────────────
    def test_no_sett_amount_reuses_the_settlement_payable_layer(self):
        """`noSettAmount` = Σ **未结账**绩效行的 `settlement_amount.payable_net`
        （= `bonus_amount − 扣点额 + Σ 未结账调整记录`）。

        三条判别力：
        - 已结账的绩效行**不计**（否则第二个人的 99 会加进来）；
        - **已结账**的调整记录**不计**（否则第三个 -100 会把总额变成负数）；
        - 扣点项在算式里（否则第三个 30 而不是 26）。
        期望值写字面量：25.00 + 26.00 = 51.00。
        """
        taker = self._account("stats-amount")
        # ① 未结账 + 一条未结账调整（-5）⇒ 30 - 0 - 5 = 25
        _, perf1 = self._claim(taker, claimed_at=_at(10), temp="100.00", bonus="30.00")
        self._adjustment(perf1, "-5.00")
        # ② 已结账 ⇒ 整行不计
        self._claim(
            taker,
            claimed_at=_at(10),
            bonus="99.00",
            settle_status=SettleStatus.SETTLED,
        )
        # ③ 未结账 + 固定扣点 4 + 一条**已结账**调整（-100，不许计入）⇒ 30 - 4 = 26
        _, perf3 = self._claim(
            taker,
            claimed_at=_at(10),
            temp="100.00",
            bonus="30.00",
            deduct_mode="1",
            deduct_value="4.00",
        )
        self._adjustment(perf3, "-100.00", settle_status=SettleStatus.SETTLED)

        row = self._row_of(self._get(**self._window())[1], "stats-amount")

        self.assertEqual(row["noSettAmount"], 51.0)

    def test_a_broken_amount_row_is_not_silently_zeroed(self):
        """某一行的应付净额**算不出来**（固定扣点 > 预计绩效）⇒ 整表显式失败，
        **不是**把那一行按 0 算（静默错账），也不是 HTML 500。"""
        taker = self._account("stats-broken")
        self._claim(
            taker,
            claimed_at=_at(10),
            temp="10.00",
            bonus="30.00",
            deduct_mode="1",
            deduct_value="99.00",       # > temp_amount(10) ⇒ `deduct_amount` 抛 `AmountError`
        )

        response, body = self._get(**self._window())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIsNone(body["data"])
        self.assertIn("扣点金额不能超过预计绩效总计", body["msg"])

    # ── 排序 ───────────────────────────────────────────────────────
    def test_sorting_is_order_count_desc_then_uid_asc_and_reproducible(self):
        """`orderCount` 降序 → `uid` 升序；两条连续请求逐行相同（对标同分顺序不稳定）。"""
        low = self._account("stats-low")
        first = self._account("stats-tie-first")
        second = self._account("stats-tie-second")
        self._claim(low, claimed_at=_at(10))
        self._claim(first, claimed_at=_at(10))
        self._claim(first, claimed_at=_at(11))
        self._claim(second, claimed_at=_at(10))
        self._claim(second, claimed_at=_at(11))

        body1 = self._get(**self._window())[1]
        body2 = self._get(**self._window())[1]

        rows = self._rows(body1)
        # 同分两人（first / second）按 uid 升序；低分排最后
        tied = [row["uid"] for row in rows if row["orderCount"] == 2]
        self.assertEqual(tied, sorted(tied))
        self.assertLess(rows.index(self._row_of(body1, "stats-tie-first")),
                        rows.index(self._row_of(body1, "stats-low")))
        self.assertEqual(
            [(row["uid"], row["idx"]) for row in rows],
            [(row["uid"], row["idx"]) for row in self._rows(body2)],
        )
        # 前置哨兵：排名从 1 开始且连续
        self.assertEqual([row["idx"] for row in rows], list(range(1, len(rows) + 1)))

    def test_idx_is_the_global_rank_not_the_page_rank(self):
        """`idx` = **全量**行序 + 1：翻到第 2 页时第 2 名的 `idx` 仍是 2（不是 1）。"""
        top = self._account("stats-rank-top")
        second = self._account("stats-rank-second")
        self._claim(top, claimed_at=_at(10))
        self._claim(top, claimed_at=_at(11))
        self._claim(second, claimed_at=_at(10))

        body = self._get(page=2, limit=1, **self._window())[1]

        rows = self._rows(body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["userName"], "stats-rank-second")
        self.assertEqual(rows[0]["idx"], 2)

    # ── 分页 ───────────────────────────────────────────────────────
    def test_limit_defaults_to_one_hundred_and_the_endpoint_ceiling_is_two_thousand(self):
        """`limit` 不传 ⇒ 100（照对标）；本端点上限 = **2000**（对标 `limits` 的最大档）。

        `limit=2000` 必须原样回 `page_size=2000`：若沿用全局 `EnvelopePagination` 的
        `max_page_size=200`，DRF 的 `_positive_int(..., cutoff=200)` 会把它**静默截成 200**
        ⇒ 本断言就是那条「静默截断」的判别力用例。
        """
        self._account("stats-page")

        self.assertEqual(self._get(**self._window())[1]["data"]["page_size"], 100)
        self.assertEqual(self._get(limit=2000, **self._window())[1]["data"]["page_size"], 2000)
        self.assertEqual(self._get(limit=1000, **self._window())[1]["data"]["page_size"], 1000)
        self.assertEqual(self._get(limit=1, **self._window())[1]["data"]["page_size"], 1)

    def test_a_limit_over_the_ceiling_is_an_explicit_error_not_a_silent_truncation(self):
        """`limit` 超上限 / 比 1 小的非法值 ⇒ 业务码 `1000` + msg 说明（**不许截断、不许静默**）。"""
        self._account("stats-page-bad")

        for bad_limit in (3000, 0, -1):
            with self.subTest(limit=bad_limit):
                response, body = self._get(limit=bad_limit, **self._window())

                self.assertEqual(response.status_code, 200)
                self.assertEqual(body["code"], ErrorCode.VALIDATION, body)
                self.assertIsNone(body["data"])
                self.assertIn("limit", body["msg"])

    def test_a_page_beyond_the_last_one_is_an_empty_success_not_a_fake_not_found(self):
        """`page` 是**合法整数但超末页** ⇒ **成功空页**（`code 0` + `items: []` + **真实 `total`**）。

        判别力：越界 page 若照 DRF 分页原样抛 `NotFound`，本仓信封会把它翻成
        `1002「记录不存在或已被删除」`——那是**假话**（没有任何记录不存在，只是这一页没有内容），
        而 Task 4 的「选游戏重取（照对标**不回**第 1 页）」正会踩到它：
        页数变少后拿到 1002，layui 当失败处理，对标却是 `count` + `data:[]` 的成功。
        """
        self._account("stats-oob")        # + setUp 的 `stats-viewer` ⇒ 表里**恰好 2 行**
        first = self._get(**self._window())[1]
        self.assertEqual(first["data"]["total"], 2)       # 前置哨兵：确实只有 2 行

        for page in (2, 99999):
            with self.subTest(page=page):
                response, body = self._get(page=page, **self._window())

                self.assertEqual(response.status_code, 200)
                self.assertEqual(body["code"], 0, body)   # **不是** 1002
                self.assertEqual(body["msg"], "")
                self.assertEqual(body["data"]["items"], [])
                self.assertEqual(body["data"]["total"], 2)   # 真实总数仍在（不是 None）
                self.assertEqual(body["data"]["page"], page)

    # ── 游戏筛选 = 按「这个人的游戏权限」筛**行集合**（数值不变）─────────
    def test_game_name_filters_the_row_set_by_game_permission_and_leaves_numbers_untouched(self):
        """`gameName` = 按**这个人的游戏权限**（`StaffProfile.games`）筛掉没有该权限的人；
        **剩下的人的数字一个都不变**。

        对标实测（数字**逐字**抄在**仓库内**的计划
        `docs/plans/2026-09-22-p3e-self-service-prohibit-and-doc-debt.md` 的 Task 3 Interfaces——
        原指针指向的调研台账**不在 git**、指针会烂）：`gameName=原神` 时 `count` 102→101（少一个人），
        而两边**共同 uid 的 `orderCount` 差异 = 0** ⇒ 它筛的是**名单**、不是把数字缩到该游戏；
        卡片那个下拉的 i18n 标签就叫「游戏权限」。

        ⚠️ **判别力设计（本用例是这条口径的验收点）**：
        - `stats-perm-a` **有 GAME_A 权限**，但两条单**全在 GAME_B** 上 ⇒
          「按订单游戏收窄数字」的实现在 `gameName=GAME_A` 下会把他从 2 条变成 0 条；
        - `stats-perm-b` **没有 GAME_A 权限**（只有 GAME_B），但他有一条 **GAME_A** 的单 ⇒
          「按订单游戏收窄数字」的实现会把他**留在表里**（只是数字变小）。
        两种错误形态各被一个人的数据钉住；四个数值字段另用**字面量**断言。
        """
        permitted = self._account("stats-perm-a")
        other = self._account("stats-perm-b")
        self._profile(permitted, self.game)        # 有「原神」权限
        self._profile(other, self.other_game)      # 只有「崩坏」权限
        self._claim(permitted, claimed_at=_at(10), game=self.other_game,
                    bonus="30.00", status=OrderStatus.IN_PROGRESS)
        self._claim(permitted, claimed_at=_at(11), game=self.other_game,
                    bonus="40.00", status=OrderStatus.COMPLETED)
        self._claim(other, claimed_at=_at(10), game=self.game, bonus="66.00")

        unfiltered = self._rows(self._get(**self._window())[1])
        filtered = self._rows(self._get(gameName=GAME_A, **self._window())[1])

        names_unfiltered = {row["userName"] for row in unfiltered}
        names_filtered = {row["userName"] for row in filtered}
        # 前置哨兵：不带筛时两个人都看得到（否则下面的 `assertNotIn` 可能由空表凑出来）
        self.assertIn("stats-perm-a", names_unfiltered)
        self.assertIn("stats-perm-b", names_unfiltered)
        # ① 没有该游戏权限的人**整行消失**（哪怕他有该游戏的单）
        self.assertNotIn("stats-perm-b", names_filtered)
        self.assertIn("stats-perm-a", names_filtered)
        self.assertTrue(names_filtered < names_unfiltered)

        # ② 剩下的人四个数值**与不带筛时逐字段相同**（另给字面量，避免自指断言）
        kept = [row for row in filtered if row["userName"] == "stats-perm-a"][0]
        self.assertEqual(kept["orderCount"], 2)
        self.assertEqual(kept["noSettAmount"], 70.0)
        self.assertEqual(kept["complateRate"], 50.0)
        self.assertEqual(kept["lastRecOrderDate"], "2026-09-11")

        before = {row["userName"]: row for row in unfiltered}
        for row in filtered:
            previous = before[row["userName"]]
            for field in ("orderCount", "noSettAmount", "complateRate", "lastRecOrderDate"):
                self.assertEqual(
                    row[field], previous[field], (row["userName"], field, row, previous)
                )

    def test_a_game_that_exists_but_nobody_has_permission_for_is_a_legitimate_empty_result(self):
        """游戏**存在**但**没人有该权限** ⇒ 正常成功 + 空列表 + `total=0`
        （**合法空结果，不是错误**——与「游戏名不存在 ⇒ `1000`」成对）。

        前置哨兵：同一批数据在「不带 `gameName`」与「筛他自己有权限的那款」时**看得见他**
        ——否则这个空表可能只是「本来就没有人」凑出来的。
        """
        # ⚠️ 本用例的前提是「**A 游戏没人有权限**」⇒ 夹具把请求方改成只持 B
        # （`setUp` 的默认是 A+B，那是别的用例要的）——不这么做的话请求方自己就持有 A，
        # 这个空表会变成「有一个人」，前提被悄悄换掉。
        self.viewer.staff_profile.games.set([self.other_game])
        taker = self._account("stats-perm-only-b")
        self._profile(taker, self.other_game)

        response, body = self._get(gameName=GAME_A, **self._window())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total"], 0)
        # 前置哨兵（两路）
        self.assertIn(
            "stats-perm-only-b",
            [row["userName"] for row in self._rows(self._get(**self._window())[1])],
        )
        self.assertIn(
            "stats-perm-only-b",
            [row["userName"]
             for row in self._rows(self._get(gameName=GAME_B, **self._window())[1])],
        )

    def test_a_soft_deleted_game_is_not_a_valid_filter(self):
        """软删除（D-0085）的游戏**视同不存在** ⇒ `1000`（**不是**成功空表、更不是成功带人）。

        判别力：存在性判据若只有 `Game.objects.filter(name=…)`，软删的游戏仍算「存在」⇒
        带它取数会回 `200 + code 0 + total=1`（把挂着该权限的人照旧带出来），
        而该游戏早从各页下拉里消失了——那是自相矛盾的成功。
        """
        # ⚠️ 前置哨兵要的 `total == 1` 建立在「只有那个打手持有 A」上 ⇒ 请求方改成只持 B
        # （`setUp` 的默认是 A+B）；不撤掉请求方的 A，这里会变成 2。
        self.viewer.staff_profile.games.set([self.other_game])
        taker = self._account("stats-soft-deleted")
        self._profile(taker, self.game)
        self._claim(taker, claimed_at=_at(10))

        # 前置哨兵：**没删**时它是合法筛选项、成功且看得见那个人
        # （否则下面的 1000 可能由「游戏名本来就没人有权限」之类的空结果凑出来）。
        self.assertEqual(
            self._get(gameName=GAME_A, **self._window())[1]["data"]["total"], 1
        )

        Game.objects.filter(pk=self.game.pk).update(deleted_at=timezone.now())

        response, body = self._get(gameName=GAME_A, **self._window())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIsNone(body["data"])
        self.assertIn(GAME_A, body["msg"])

    # ── 错误三分支：一律显式 1000，绝不静默空 ─────────────────────────
    def test_missing_stime_or_etime_is_an_explicit_validation_error(self):
        """缺 `stime` / 缺 `etime` ⇒ `1000` + 说真话的 msg（**不是** 200 + code 0 + 空表）。"""
        _, no_params = self._get()
        self.assertEqual(no_params["code"], ErrorCode.VALIDATION)
        self.assertIsNone(no_params["data"])
        self.assertIn("stime", no_params["msg"])
        self.assertIn("etime", no_params["msg"])

        _, only_stime = self._get(stime="2026-09-01")
        self.assertEqual(only_stime["code"], ErrorCode.VALIDATION)
        self.assertIn("etime", only_stime["msg"])

    def test_a_datetime_instead_of_a_date_is_rejected(self):
        """带时分秒 ⇒ `1000`（格式**只吃** `yyyy-MM-dd`；对标「不要静默空」）。"""
        _, body = self._get(stime="2026-09-01 00:00:00", etime="2026-09-30")

        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIsNone(body["data"])
        self.assertIn("yyyy-MM-dd", body["msg"])
        self.assertIn("stime", body["msg"])

    def test_an_unknown_game_name_is_rejected(self):
        """未知游戏名 ⇒ `1000` + 把那个名字写进 msg（不是空表）。"""
        _, body = self._get(gameName="这款游戏不存在", **self._window())

        self.assertEqual(body["code"], ErrorCode.VALIDATION)
        self.assertIsNone(body["data"])
        self.assertIn("这款游戏不存在", body["msg"])

    def test_the_three_error_branches_are_distinguishable_by_their_messages(self):
        """同码三分支（都 `1000`）必须靠 msg 可区分——否则前端分不出让人改哪一个。"""
        messages = [
            self._get()[1]["msg"],
            self._get(stime="2026-09-01 00:00:00", etime="2026-09-30")[1]["msg"],
            self._get(gameName="这款游戏不存在", **self._window())[1]["msg"],
        ]
        self.assertEqual(len(set(messages)), 3, messages)

    def test_a_malformed_page_or_limit_is_rejected_instead_of_silently_ignored(self):
        """`page=abc` / `page=0` / `page=-1` / `limit=abc` ⇒ `1000`；
        **不是** DRF 的静默回落（`limit` 会悄悄变 100）。

        ⚠️ 与 `test_a_page_beyond_the_last_one_is_an_empty_success_not_a_fake_not_found` 的分界：
        **非整数 / `<1`** 是参数错（`1000`）；**合法整数但超末页**是空页（成功）。
        """
        for bad_page in ("abc", "0", "-1"):
            with self.subTest(page=bad_page):
                _, body = self._get(page=bad_page, **self._window())
                self.assertEqual(body["code"], ErrorCode.VALIDATION)
                self.assertIsNone(body["data"])
                self.assertIn("page", body["msg"])

        _, bad_limit = self._get(limit="abc", **self._window())
        self.assertEqual(bad_limit["code"], ErrorCode.VALIDATION)
        self.assertIn("limit", bad_limit["msg"])

    # ── 权限 ───────────────────────────────────────────────────────
    def test_the_endpoint_requires_its_rbac_node(self):
        """无 `system_user.view_order_stats` 节点 ⇒ **HTTP 403** + 可辨 msg。

        ⚠️ 对照账号用**新 Group**（`groups=()`）而不是「客服」：夹具 `_grant_nodes` 把节点挂在
        **Group → Role** 上，本用例的 `setUp` 已把节点挂给了「客服」这个 Group ⇒ 再用「客服」
        建对照账号会**白拿**该节点，判据变成恒真的 200（实测踩到过）。
        """
        # ⚠️ 显式 opt-out（默认值翻面）：本用例只断 403 节点门，
        # 与游戏范围无关 ⇒ 不建档案。
        self.client = make_logged_in_client(username="stats-no-node", games=None)

        response, body = self._get(**self._window())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(body["code"], 403)
        self.assertIn("接单统计", body["msg"])

    def test_the_endpoint_requires_authentication(self):
        """匿名 ⇒ **HTTP 401**（认证层语义，不是业务码）。"""
        anonymous = Client(SERVER_NAME="tenant.test.com")
        response = anonymous.get(URL, self._window())

        self.assertEqual(response.status_code, 401)



    def test_a_hall_shadow_order_is_not_counted(self):
        """⭐ 评审 Important 7：接单统计基集排除隐藏单（金额 / 单数都不算）。"""
        from tests.hall_shadow_helpers import make_mirror

        taker = self._account("stats-shadow")
        self._profile(taker, self.game)
        normal, _ = self._claim(taker, claimed_at=_at(10), bonus="88.00")
        hidden, _ = self._claim(taker, claimed_at=_at(10), bonus="999.00")
        hidden.hall_card = make_mirror(
            game=self.game, bound_by=taker, no="SYN-SHADOW-STATS"
        )
        hidden.save(update_fields=["hall_card"])

        body = self._get(**self._window())[1]
        row = self._row_of(body, taker.username)
        self.assertEqual(row["orderCount"], 1)
        self.assertEqual(row["noSettAmount"], 88.0)


class OrderStatsCrossTenantTests(_StatsFixtureMixin, TenantTestCase):
    """跨租户：A 的接单统计在 B 读不到（判据用**名字**，不用主键）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="P3e 接单统计隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A。跨域名请求不会在响应结束时还原 schema。
        connection.set_tenant(self.tenant)
        super().setUp()
        self.client_a = make_logged_in_client(
            domain=DOMAIN_A, username="stats-iso-viewer-a",
            groups=("客服",), nodes=(ORDER_STATS_NODE,),
            # ⚠️ 显式 opt-out（默认值翻面）：游戏由下面那句 `grant_games` 补。
            games=[],
        )
        # ⚠️ 请求方要显式持有游戏权限（收窄之后，没有权限 ⇒ 空表）
        grant_games(User.objects.get(username="stats-iso-viewer-a"), self.game)
        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="仅 B 的游戏")
                taker_b = User.objects.create_user(username=MARKER_B)
                order_b = Order.objects.create(
                    game=game_b,
                    order_no=_no(901),
                    status=OrderStatus.IN_PROGRESS,
                    hall_price=Decimal("88.00"),
                )
                Performance.objects.create(
                    order=order_b,
                    user=taker_b,
                    emp_type=EmpType.MAIN,
                    temp_amount=Decimal("88.00"),
                    claimed_at=timezone.make_aware(datetime(2026, 9, 10, 12, 0)),
                )
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B, username="stats-iso-viewer-b",
                    groups=("客服",), nodes=(ORDER_STATS_NODE,),
                    # ⚠️ 显式 opt-out（默认值翻面）：游戏由下面的 `grant_games` 补。
                    games=[],
                )
                # ⚠️ B 的请求方持有 B 自己的游戏（权限是**租户内**的：拿 B 的账号 + B 的游戏）
                grant_games(User.objects.get(username="stats-iso-viewer-b"), game_b)
        finally:
            connection.set_tenant(self.tenant)

    def _get_b(self):
        try:
            return self.client_b.get(URL, {"stime": "2026-09-01", "etime": "2026-09-30"},
                                     SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

    def test_a_tenants_stats_are_invisible_in_tenant_b(self):
        """A 的标记账号（有接单）在 B 的统计里**不出现**；B 自己的标记账号出现。"""
        # A 侧：标记账号 + 一条窗口内接单（不建 client，直接摆数据）
        taker_a = User.objects.create_user(username=MARKER_A)
        order_a = Order.objects.create(
            game=self.game,
            order_no=_no(900),
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order_a,
            user=taker_a,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("88.00"),
            claimed_at=timezone.make_aware(datetime(2026, 9, 10, 12, 0)),
        )

        # 非空哨兵：A 自己的请求**看得见** A 的标记账号（否则下面的 `assertNotIn` 是空表假通过）
        body_a = self.client_a.get(
            URL, {"stime": "2026-09-01", "etime": "2026-09-30"}, SERVER_NAME=DOMAIN_A
        ).json()
        self.assertEqual(body_a["code"], 0, body_a)
        names_a = [row["userName"] for row in body_a["data"]["items"]]
        self.assertIn(MARKER_A, names_a)
        self.assertNotIn(MARKER_B, names_a)

        # B 侧：看得见 B 自己的标记账号，**看不见** A 的
        body_b = self._get_b().json()
        self.assertEqual(body_b["code"], 0, body_b)
        names_b = [row["userName"] for row in body_b["data"]["items"]]
        self.assertIn(MARKER_B, names_b)
        self.assertNotIn(MARKER_A, names_b)


class OrderStatsGameScopeTests(_StatsFixtureMixin, TenantTestCase):
    """按**看统计的人**的游戏权限收窄**被统计的行**。

    ⚠️ **与既有 `?gameName=` 的口径差别（两件事，刻意不混）**：
    - `?gameName=X`（`order_stats_rows` 的账号集过滤，见 `order_stats.py` 模块 docstring
      第 7 条）筛的是**表里出现哪些人**——按**被统计的人**自己的游戏权限决定；不带它时名单不变。
    - 本类钉的是**数字**：某个人的接单量只统计**看统计的人**有权限的那些游戏的行
      （`Performance → order__game_id`）。没有权限的游戏的单**不进任何人的数字**
      （那个人还在表里、`orderCount` 为 0）。
    - 两套叠加时：先由 `gameName` 决定名单，再由查看者的游戏权限决定每一行**数哪些单**。
    - **严格口径（业务方）**：查看者一款游戏权限都没有 ⇒ **整张表都不给**
      （不是「一张全 0 的同事名单」）。
    """

    def _scoped_client(self, username, *games):
        """建一个**只看得到 `games`** 的登录客户端（RBAC 节点照给，权限维度单独测）。"""
        client = make_logged_in_client(
            username=username, groups=("客服",), nodes=(ORDER_STATS_NODE,),
            # ⚠️ 显式 opt-out（默认值翻面，**这条直接决定判别力**）：
            # 本夹具要造「只有 A 权限」的查看者，若夹具按新默认给全部游戏，
            # 下面的 `grant_games(..., A)` 只会**并集**（并了等于没并）⇒
            # 「只有 A ⇒ B 数字为 0」当场变成恒真。`games=[]` 先钉成零游戏。
            games=[],
        )
        if games:
            grant_games(User.objects.get(username=username), *games)
        return client

    def _get_with(self, client, **params):
        response = client.get(URL, params)
        return response, response.json()

    def _window(self, **extra):
        """一个覆盖所有夹具日期的窗口（`2026-09-01` ~ `2026-09-30`）。"""
        return {"stime": "2026-09-01", "etime": "2026-09-30", **extra}

    def _rows_of(self, body):
        self.assertEqual(body["code"], 0, body)
        return {row["userName"]: row for row in body["data"]["items"]}

    def test_a_viewer_with_one_game_counts_only_that_games_claims(self):
        """① 只有 A 权限的人看统计 ⇒ **只统计 A 游戏的行**（字面计数：2 / 0）。

        A 游戏两条、B 游戏一条：查看者只有 A 权限 ⇒ A 的那个人 `orderCount` 是 **2**、
        B 的那个人是 **0**（B 的那条单一条都不进数字，人还在表里）。
        """
        client = self._scoped_client("stats-scope-a", self.game)
        in_a = self._account("scope-count-a")
        in_b = self._account("scope-count-b")
        self._claim(in_a, claimed_at=_at(10), game=self.game)
        self._claim(in_a, claimed_at=_at(11), game=self.game)
        self._claim(in_b, claimed_at=_at(10), game=self.other_game)

        body = self._get_with(client, **self._window())[1]
        rows = self._rows_of(body)

        # 表里还是那三个人（收窄的是**数字**，不是名单）
        self.assertEqual(sorted(rows), ["scope-count-a", "scope-count-b", "stats-scope-a"])
        self.assertEqual(rows["scope-count-a"]["orderCount"], 2)
        self.assertEqual(rows["scope-count-b"]["orderCount"], 0)
        self.assertEqual(body["data"]["total"], 3)

    def test_an_admin_counts_the_claims_of_every_game(self):
        """② 管理员 ⇒ 两种游戏的行都统计（A 的 2 条与 B 的 1 条都进数字）。"""
        admin = make_logged_in_client(username="stats-scope-admin", is_superuser=True)
        in_a = self._account("scope-admin-a")
        in_b = self._account("scope-admin-b")
        self._claim(in_a, claimed_at=_at(10), game=self.game)
        self._claim(in_a, claimed_at=_at(11), game=self.game)
        self._claim(in_b, claimed_at=_at(10), game=self.other_game)

        body = self._get_with(admin, **self._window())[1]
        rows = self._rows_of(body)

        self.assertEqual(rows["scope-admin-a"]["orderCount"], 2)
        self.assertEqual(rows["scope-admin-b"]["orderCount"], 1)
        self.assertEqual(body["data"]["total"], 3)

    def test_a_viewer_without_any_game_sees_nothing_at_all(self):
        """③ 空游戏 ⇒ `items` 为空且 `total == 0`（字面 `0`）。

        正例对照（同一批数据、同一个客户端）：补上 A 权限后同一个人立刻在表里且
        `orderCount == 1` ⇒ 空是「查看者没有游戏权限」造成的，不是「本来就没有数据」。
        """
        client = self._scoped_client("stats-scope-none")
        taker = self._account("scope-none-taker")
        self._claim(taker, claimed_at=_at(10), game=self.game)

        response, body = self._get_with(client, **self._window())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total"], 0)

        grant_games(User.objects.get(username="stats-scope-none"), self.game)
        after = self._get_with(client, **self._window())[1]
        self.assertEqual(after["data"]["total"], 2)
        self.assertEqual(self._rows_of(after)["scope-none-taker"]["orderCount"], 1)

    def test_a_viewer_with_one_game_still_sees_the_claims_of_that_game(self):
        """④ 反例（正例对照）：有 A 权限的人 ⇒ A 的行**在**结果里且被计入
        （那个人同时有一条 B 的单 ⇒ `orderCount` 是 **1**，不是 2、也不是 0）。

        这条证明上面的收窄不是「一律空」。
        """
        client = self._scoped_client("stats-scope-positive", self.game)
        taker = self._account("scope-positive-taker")
        self._claim(taker, claimed_at=_at(10), game=self.game)
        self._claim(taker, claimed_at=_at(11), game=self.other_game)

        body = self._get_with(client, **self._window())[1]
        rows = self._rows_of(body)

        self.assertIn("scope-positive-taker", rows)
        self.assertEqual(rows["scope-positive-taker"]["orderCount"], 1)
        self.assertEqual(rows["scope-positive-taker"]["lastRecOrderDate"], "2026-09-10")


class OrderStatsRowsSignatureTests(TenantTestCase):
    """C-1（收口评审）：`order_stats_rows(..., viewer=)` 是**必填 keyword-only**。

    ⚠️ **为什么这条重要**：`viewer=None` 时 `games_of(None)` 是空集 ⇒ `scope_queryset`
    的 `scope_filter` 把行收成空 ⇒ **静默回空表**（改动前是「不过滤 = 全量」，是
    Task 4 之后才出现的形态变化）。今天唯一的生产调用方（`order_stats_views`）总传
    `viewer=request.user` ⇒ **无现网影响**；但将来漏传会把统计**静默**清空（没有异常、
    没有日志、页面只显示 0）。
    **为什么选「必填」而不是「在 docstring 写明 + 钉一条 `viewer=None ⇒ 空表`」**：
    本仓的先例是前者——`settle_performances(..., user=)` 与 `revoke_settlements(..., user=)`
    都把数据权限的主体做成**必填 keyword-only**，docstring 原话是「做成必填是为了让
    『漏传』变成 `TypeError` 而不是静默放行」。同一种风险用同一种收口。

    判别力：把签名改回 `viewer=None` ⇒ 本用例红（调用**不抛**，回 `[]`）。
    """

    def test_a_missing_viewer_is_a_type_error_not_a_silent_empty_table(self):
        from apps.business.order_stats import order_stats_rows

        with self.assertRaises(TypeError):
            # ⛔ 故意漏传 `viewer`：它必须**在调用点**就响亮报错，而不是回一张空表。
            order_stats_rows(stime="2026-09-01", etime="2026-09-30")
