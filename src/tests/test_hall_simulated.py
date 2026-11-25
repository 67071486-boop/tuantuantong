"""大厅**模拟单**的生成 / 回收 / 假接手端点（D-0296，2026-10-24）。

## 这张网钉什么

- **开关只闸「生成新单」**：关（默认）⇒ **不补卡**；回收段（active 2h → retired、
  retired 2h → 删、**快照 7 天 → 删**）在开关门外**恒跑**（评审 M-1：快照清理是
  三路共用的唯一回收点，不开模拟单的租户也必须清）；且租户边界——A 开 B 不动；
- **补充**：有历史订单（未软删）的游戏每轮 2~3 张（目标每游戏**每轮随机取一次**）；
  没有历史订单的游戏零张；
- **价格**：`max(10, 源价 × uniform(1.3, 1.5))`——源价 `0` / `None` 都落到下限 `10`；
- **防重复**：近 4 小时被抽过的源单不再抽（含已下架的卡）；
- **回收**：active 满 2h → retired；retired 满 2h → 删；快照满 7 天 → 删；
- **假接手端点**：active → retired，**无论结果**回 `1012` + 逐字文案；对 retired 再点仍 `1012`；
  ⭐ **可见性闸（X-01）**：读侧三条件（App 令牌 / 没开个人免押 / 无冻结押金）
  对写侧同宽，判据与读侧**同一份函数**（`hall_simulated.viewer_sees_simulated`）——
  PC 会话 / 免押 / 有冻结三类人拿到的是**与「已被接走」逐字相同**的 `1012`、卡不下架；
- ⛔ **全程不建任何 `Order` 行**（`Order.all_objects` 计数不变——模拟单不进资金面）。

每条钉子的变异方向写在各自 docstring 里（把它弄红的改法）。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import connection, tenant_context

from apps.accounts.models import StaffProfile
from apps.business.api.serializers import HallListSerializer
from apps.business.hall_simulated import (
    GRAY_KEEP_SECONDS,
    MIN_PRICE,
    SNAPSHOT_KEEP_DAYS,
    price_for_source,
    run_tenant_round,
)
from apps.business.models import (
    EmpType,
    Game,
    HallExitSnapshot,
    HallSimulatedOrder,
    Order,
    OrderStatus,
    Performance,
    SystemSwitch,
)
from apps.platform.models import Tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

SIM_SWITCH_KEY = "hall_simulated_orders"
SIM_CLAIM_URL = "/api/business/hall/sim/{pk}/claim/"
SIM_MSG = "此单已被人接手，请刷新"

#: 合成租户 B（边界用例；照 test_system_switches 的同款做法）。
SCHEMA_B = "hall_sim_b"
DOMAIN_B = "hall-sim-b.test.local"


def _order_no(n: int) -> str:
    return f"2026102400000000{n:04d}"


def _app_poster(username: str, game) -> tuple:
    """给**已有用户**配一枚 AppToken，回 (Client, 原始令牌)——调用方逐请求带 Bearer。

    ⭐ X-01起模拟单写侧也过「只在移动端」那道闸：能点动演示卡的
    夹具必须走 AppToken 令牌面（照 `test_hall_api.py::_app_body` 的同一形状）。
    用户本体由 `make_logged_in_client` 造（带组 / 节点 / 游戏档案），这里只补令牌。
    """
    from apps.accounts.models import AppToken
    from apps.business.peer_sync import hash_token, new_token

    raw = new_token()
    AppToken.objects.create(
        user=User.objects.get(username=username),
        key_hash=hash_token(raw),
        client="sim-claim-app",
        expires_at=timezone.now() + timedelta(hours=1),
    )
    return Client(SERVER_NAME="tenant.test.com"), raw


class PriceForSourceTests(TenantTestCase):
    """纯函数：价格下限与加价区间（不碰库）。"""

    def test_a_zero_or_missing_source_price_lands_on_the_floor(self):
        """源价 `0` / `None` ⇒ 下限 `10`（D-0015：0 是合法金额，不是"没传"）。"""
        self.assertEqual(price_for_source(Decimal("0")), MIN_PRICE)
        self.assertEqual(price_for_source(None), MIN_PRICE)
        self.assertEqual(price_for_source(Decimal("0.00")), MIN_PRICE)

    def test_a_priced_source_is_marked_up_within_the_band(self):
        """源价 100 ⇒ 展示价 ∈ [130, 150]，两位小数。"""
        for _ in range(30):
            price = price_for_source(Decimal("100.00"))
            self.assertGreaterEqual(price, Decimal("130.00"))
            self.assertLessEqual(price, Decimal("150.00"))
            self.assertEqual(price, price.quantize(Decimal("0.01")))

    def test_a_negative_source_price_also_lands_on_the_floor(self):
        """负价源（合法金额）× 加价仍 < 下限 ⇒ 下限兜底（`max` 的判据）。"""
        self.assertEqual(price_for_source(Decimal("-50.00")), MIN_PRICE)


class _SimFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="模拟单游戏")
        self.game_empty = Game.objects.create(name="无单游戏")
        self._orders_at_setup = Order.all_objects.count()

    def _source_order(self, n, *, price="88.00", game=None):
        order = Order.objects.create(
            game=game or self.game, order_no=_order_no(n),
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal(price) if price is not None else None,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00"),
        )
        return order

    def _turn_on(self):
        SystemSwitch.objects.create(key=SIM_SWITCH_KEY, value="Y")

    def _run(self, now=None):
        return run_tenant_round(now=now or timezone.now())

    def _sims(self, **filters):
        return HallSimulatedOrder.objects.filter(**filters).order_by("pk")


class SwitchGateTests(_SimFixture):
    """开关**只**闸「生成新单」；回收段（含快照清理）恒跑（评审 M-1，2026-10-24）。

    旧契约「开关关 ⇒ 连回收都不做」已按 M-1 废止——那条断言把「快照只增不删」
    钉成了契约。这里拆成三条、各有独立的红判据。
    """

    def test_switch_off_creates_no_new_cards(self):
        """开关关（默认 N）⇒ **不生成新单**（有历史单也不补）。

        变异方向：把 `run_tenant_round` 里包 `_top_up` 的开关门删掉 ⇒ 本条红
        （关着也建出 2~3 张卡）。
        """
        self._source_order(1)
        self._source_order(2)
        self._source_order(3)
        result = self._run()
        self.assertEqual(result["created"], 0, "开关关着还在建卡")
        self.assertTrue(result["switched_off"])
        self.assertEqual(self._sims().count(), 0)

    def test_switch_off_still_recycles_due_active_cards(self):
        """开关关 ⇒ 到点的 active 模拟单**仍被下架**（回收段在开关门外）。

        变异方向：把回收段挪回开关门内（M-1 之前的旧形状）⇒ 本条红（卡停在 active）。
        """
        now = timezone.now()
        stale = HallSimulatedOrder.objects.create(
            game=self.game, display_order_no=_order_no(9001),
            project_text="过期卡", price=Decimal("10.00"),
            status=HallSimulatedOrder.Status.ACTIVE,
            generated_at=now - timedelta(seconds=GRAY_KEEP_SECONDS + 60),
            payload={},
        )
        result = self._run(now=now)
        stale.refresh_from_db()
        self.assertEqual(stale.status, HallSimulatedOrder.Status.RETIRED, "到点的卡没被下架")
        self.assertEqual(stale.retired_at, now)
        self.assertEqual(result["retired"], 1)
        # 同一轮：开关关 ⇒ 不补卡（下架腾出的空缺不会被新卡填上）。
        self.assertEqual(result["created"], 0)
        self.assertEqual(
            self._sims(status=HallSimulatedOrder.Status.ACTIVE).count(), 0
        )

    def test_snapshot_cleanup_is_not_gated_by_the_switch(self):
        """开关关 ⇒ 快照 7 天清理**照跑**（它是三路快照共用的唯一回收点）。

        变异方向：把 `_recycle`（或其中快照段）挪回开关门内 ⇒ 本条红
        （8 天前的快照原样留着、`snapshots` 计数为 0）。
        """
        now = timezone.now()
        stale = HallExitSnapshot.objects.create(
            order=None, mirror=None, source=HallExitSnapshot.Source.NATIVE,
            exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN, game=self.game,
            payload={"order_no": _order_no(9002)},
            exited_at=now - timedelta(days=8),
        )
        fresh = HallExitSnapshot.objects.create(
            order=None, mirror=None, source=HallExitSnapshot.Source.NATIVE,
            exit_kind=HallExitSnapshot.ExitKind.CLAIMED, game=self.game,
            payload={"order_no": _order_no(9003)},
            exited_at=now - timedelta(days=1),
        )
        result = self._run(now=now)
        self.assertFalse(
            HallExitSnapshot.objects.filter(pk=stale.pk).exists(), "超期快照没清"
        )
        self.assertTrue(
            HallExitSnapshot.objects.filter(pk=fresh.pk).exists(), "未到点的快照被误删"
        )
        self.assertEqual(result["snapshots"], 1)


class TenantBoundaryTests(_SimFixture):
    """租户边界：A 的开关与历史单**不会**波及 B（`AGENTS.md` §4）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.platform.provisioning import provision_tenant

        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="模拟单边界租户 B", domain=DOMAIN_B
        )
        # ⚠️ 不做手工清理：`test` 租户由 `TenantTestCase` 自己拆；B 的行留在测试库
        #    （schema_name 不同名，不与下一类的 `test` 租户冲突——test_system_switches 同款）。

    def test_turning_the_switch_on_in_a_does_not_create_cards_in_b(self):
        """A 开 + A 有历史单 ⇒ A 建卡；B 没开开关 ⇒ B 一张卡都没有（两路证据）。"""
        self._turn_on()
        self._source_order(1)
        self._source_order(2)
        self._source_order(3)
        result = self._run()
        self.assertNotIn("switched_off", result, "A 的开关没生效")
        self.assertGreaterEqual(self._sims().count(), 2, "A 没建出卡（夹具坏了）")
        try:
            with tenant_context(self.tenant_b):
                # B：同一套代码、同一拍，但**没有**开关行 ⇒ 默认 N ⇒ 不补新卡。
                # （回收段恒跑是 M-1 之后的口径；B 里没有任何卡与快照，回收自然零命中。）
                Game.objects.create(name="B 的游戏")
                result = run_tenant_round()
                self.assertTrue(result.get("switched_off"), result)
                self.assertEqual(result.get("created"), 0, result)
                self.assertEqual(HallSimulatedOrder.objects.count(), 0)
        finally:
            connection.set_tenant(self.tenant_a)


class TopUpTests(_SimFixture):
    def test_only_games_with_orders_get_two_to_three_cards(self):
        """有历史单的游戏补到 2~3 张；没有历史单的游戏零张。

        变异方向：把「该游戏有历史订单」的筛选删掉 ⇒ `game_empty` 也会被建卡 ⇒ 红；
        把目标改成"每轮补满一个常量"⇒ 数值断言随机构不成 ⇒ 红（多轮统计的期望不再成立）。
        """
        self._turn_on()
        self._source_order(1)
        self._source_order(2)
        self._source_order(3)
        self._run()
        n = self._sims(game=self.game, status=HallSimulatedOrder.Status.ACTIVE).count()
        self.assertIn(n, (2, 3), f"目标张数跑出 2~3 之外：{n}")
        self.assertEqual(
            self._sims(game=self.game_empty).count(), 0, "没有历史单的游戏不该有卡"
        )

    def test_sources_are_not_reused_within_the_cooldown(self):
        """防重复：同一源单在 4 小时冷却内不再被抽（跨轮、含已下架的卡）。"""
        self._turn_on()
        for i in range(3):
            self._source_order(10 + i)
        self._run()
        first_round = list(self._sims())
        self.assertGreaterEqual(len(first_round), 2, "第一轮没建卡（夹具坏了）")
        # 立刻再跑一轮：上一轮的源单都在冷却里。
        self._run()
        sources = list(
            HallSimulatedOrder.objects.exclude(source_order=None)
            .values_list("source_order_id", flat=True)
        )
        self.assertEqual(
            len(sources), len(set(sources)), f"同一源单被抽了两次：{sources}"
        )
        # 且没有卡是凭空的：每张卡的 source 都指向真实历史单。
        for sim in self._sims():
            self.assertIsNotNone(sim.source_order_id)
            self.assertTrue(
                Order.all_objects.filter(pk=sim.source_order_id).exists()
            )

    def test_cards_never_collide_with_real_order_numbers(self):
        """展示单号 20 位、本表唯一，且**不与 `Order.order_no` 相撞**。"""
        self._turn_on()
        self._source_order(20)
        self._source_order(21)
        self._run()
        for sim in self._sims():
            self.assertEqual(len(sim.display_order_no), 20)
            self.assertTrue(sim.display_order_no.isdigit())
            self.assertFalse(
                Order.all_objects.filter(order_no=sim.display_order_no).exists(),
                "模拟卡占了真实订单号",
            )

    def test_no_order_rows_are_ever_created(self):
        """⛔ 全程不建任何 `Order` 行（`all_objects` 计数不变——两路证据：轮前 = 轮后）。"""
        self._turn_on()
        self._source_order(30)
        self._source_order(31)
        self._source_order(32)
        before = Order.all_objects.count()
        self._run()
        self.assertEqual(Order.all_objects.count(), before)
        self.assertGreaterEqual(self._sims().count(), 2, "卡没建出来（夹具坏了）")

    def test_the_payload_is_the_hall_shape_with_zero_pii(self):
        """payload 键集与 `HallListSerializer` 恰好同形；客户隐私键一律空。"""
        self._turn_on()
        source = self._source_order(40)
        self._run()
        sim = self._sims().get()
        self.assertEqual(set(sim.payload), set(HallListSerializer.Meta.fields))
        self.assertEqual(sim.payload["order_no"], sim.display_order_no)
        self.assertEqual(sim.payload["origin"], "simulated")
        self.assertEqual(sim.payload["sim_pk"], sim.pk)
        self.assertIs(sim.payload["is_preferred"], True)
        self.assertIs(sim.payload["is_exited"], False)
        self.assertIsNone(sim.payload["exit_kind"])
        # ⛔ 零 PII：联系方式 / 角色名一律不进模拟卡（源单上哪怕有也不抄）。
        self.assertEqual(sim.payload["customer_phone"], "")
        self.assertEqual(sim.payload["customer_ww"], "")
        self.assertEqual(sim.payload["game_role_name"], "")
        # 项目形态复刻源单。
        self.assertEqual(sim.project_text, source.project_text)
        self.assertEqual(sim.is_urgent, source.is_urgent)


class CandidateSamplingSqlTests(_SimFixture):
    """候选取样在 **SQL 端**完成（`ORDER BY random()` + `LIMIT`），不再全量拉 pk 入内存。

    钉 2026-11 改写的形状与它的全部意义：候选查询自带随机排序与截断（变异方向：
    摘 `order_by(RawSQL("random()", []))` ⇒ 缺 `ORDER BY (random())` ⇒ 红；把
    `[:missing]` 改回内存侧切片 ⇒ 缺 `LIMIT` ⇒ 红。⚠️ 字面形状以本仓 Django 5.2
    编译产物为准：表达式带括号（`ORDER BY (random()) ASC`），裸写 `ORDER BY random()`
    匹配不到任何真实 SQL）。候选不足目标时「可用源单用尽」日志与「只建出可用数」
    的旧行为照旧（变异方向：删日志分支 ⇒ 红）。
    """

    def test_candidate_query_orders_by_random_and_limits_in_sql(self):
        """候选查询的 SQL 含 `ORDER BY (random())` 且带 `LIMIT`（取样在库端完成）。"""
        self._turn_on()
        for i in range(5):
            self._source_order(90 + i)
        with CaptureQueriesContext(connection) as ctx:
            self._run()
        sampling = [
            q["sql"] for q in ctx.captured_queries if "ORDER BY (random())" in q["sql"]
        ]
        self.assertTrue(sampling, "候选查询没有在 SQL 端取样（缺 ORDER BY (random())）")
        self.assertTrue(
            all("LIMIT" in sql for sql in sampling),
            f"取样查询没把截断推进 SQL（缺 LIMIT）：{sampling}",
        )

    def test_candidates_short_of_target_still_log_exhaustion(self):
        """候选 < 目标 ⇒ 记「可用源单用尽」日志，且只建出可用数（轮不炸）。"""
        self._turn_on()
        self._source_order(95)
        with self.assertLogs("apps.business.hall_simulated", level="INFO") as logs:
            created = self._run()["created"]
        self.assertEqual(created, 1)
        self.assertTrue(
            any("可用源单用尽" in line for line in logs.output),
            f"候选不足时没记「可用源单用尽」日志：{logs.output}",
        )


class PayloadCreatedAtTimezoneTests(_SimFixture):
    """D-0302（2026-10-24）：模拟卡 payload 的 `created_at` 是**项目时区** ISO 串。

    payload 是**生成时固化**进 JSONField 的（读侧 `simulated_row` 原样透出）⇒ 本用例钉
    写点：`generated_at` 固定 UTC 04:30 ⇒ payload 北京墙钟 12:30（+08:00）；并钉
    「旧 UTC 串的存量行读侧原样透出、不炸」（混合窗口，旧行随保留期自愈）。
    """

    def test_the_payload_created_at_is_a_project_timezone_string(self):
        from django.utils.dateparse import parse_datetime

        self._turn_on()
        self._source_order(50)
        self._run(now=parse_datetime("2026-10-24T04:30:00+00:00"))
        sim = self._sims().get()
        self.assertEqual(sim.payload["created_at"], "2026-10-24T12:30:00+08:00")

    def test_the_read_side_passes_legacy_utc_payloads_through_unharmed(self):
        from apps.business import hall_preferred
        from django.utils.dateparse import parse_datetime

        self._turn_on()
        self._source_order(51)
        self._run(now=parse_datetime("2026-10-24T04:30:00+00:00"))
        sim = self._sims().get()
        # 伪装一条「修复前写入」的旧形状行：payload 里是 UTC 串。
        HallSimulatedOrder.objects.filter(pk=sim.pk).update(
            payload={**sim.payload, "created_at": "2026-10-24T04:30:00+00:00"},
        )
        sim.refresh_from_db()
        row = hall_preferred.simulated_row(sim, exited=False)
        self.assertEqual(row["created_at"], "2026-10-24T04:30:00+00:00")


class RecycleTests(_SimFixture):
    def _card(self, n, *, status=HallSimulatedOrder.Status.ACTIVE,
              generated_at=None, retired_at=None):
        return HallSimulatedOrder.objects.create(
            game=self.game, display_order_no=_order_no(n),
            project_text="回收夹具卡", price=Decimal("10.00"),
            status=status,
            generated_at=generated_at or timezone.now(),
            retired_at=retired_at,
            payload={},
        )

    def test_active_cards_retire_after_the_gray_window(self):
        """active 满 2h → retired（`retired_at` 落在轮次时刻）；未满的照旧 active。"""
        self._turn_on()
        now = timezone.now()
        stale = self._card(
            50, generated_at=now - timedelta(seconds=GRAY_KEEP_SECONDS + 1)
        )
        fresh = self._card(51, generated_at=now)
        self._run(now=now)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, HallSimulatedOrder.Status.RETIRED)
        self.assertEqual(stale.retired_at, now)
        self.assertEqual(fresh.status, HallSimulatedOrder.Status.ACTIVE)
        self.assertIsNone(fresh.retired_at)

    def test_retired_cards_persist_like_real_gray_cards_then_deleted_at_seven_days(self):
        """retired 灰卡与正常灰卡同一条 7 天线（2026-10-24 业务方改准）：满 7 天删、未满留。

        ⚠️ 下架（2h）只是**可接时长**到点；撤下后的灰卡要像正常单一样留在
        「已被抢」区——⛔ 不再 2 小时即删（改这条前的旧口径）。
        """
        self._turn_on()
        now = timezone.now()
        stale = self._card(
            52, status=HallSimulatedOrder.Status.RETIRED,
            generated_at=now - timedelta(days=SNAPSHOT_KEEP_DAYS + 1),
            retired_at=now - timedelta(days=SNAPSHOT_KEEP_DAYS, seconds=1),
        )
        fresh = self._card(
            53, status=HallSimulatedOrder.Status.RETIRED,
            generated_at=now - timedelta(days=1), retired_at=now,
        )
        self._run(now=now)
        self.assertFalse(
            HallSimulatedOrder.objects.filter(pk=stale.pk).exists(), "超 7 天灰卡没删"
        )
        self.assertTrue(
            HallSimulatedOrder.objects.filter(pk=fresh.pk).exists(), "没到点的灰卡被误删"
        )
        # 对照自证：2h 前后的两张（旧口径会删的）都还在——证明删的是 7 天线不是 2h 线。
        two_hours_ago = self._card(
            54, status=HallSimulatedOrder.Status.RETIRED,
            generated_at=now - timedelta(hours=5),
            retired_at=now - timedelta(seconds=GRAY_KEEP_SECONDS + 1),
        )
        self._run(now=now)
        self.assertTrue(
            HallSimulatedOrder.objects.filter(pk=two_hours_ago.pk).exists(),
            "retired 满 2h 被删 = 旧口径复活",
        )

    def test_snapshots_are_deleted_after_seven_days(self):
        """快照满 7 天 → 删除（三路共用一张快照表，回收不按来源分）；未满的保留。"""
        self._turn_on()
        now = timezone.now()
        stale = HallExitSnapshot.objects.create(
            order=None, mirror=None, source=HallExitSnapshot.Source.NATIVE,
            exit_kind=HallExitSnapshot.ExitKind.WITHDRAWN, game=self.game,
            payload={"order_no": _order_no(60)},
            exited_at=now - timedelta(days=8),
        )
        fresh = HallExitSnapshot.objects.create(
            order=None, mirror=None, source=HallExitSnapshot.Source.NATIVE,
            exit_kind=HallExitSnapshot.ExitKind.CLAIMED, game=self.game,
            payload={"order_no": _order_no(61)},
            exited_at=now - timedelta(days=1),
        )
        self._run(now=now)
        self.assertFalse(HallExitSnapshot.objects.filter(pk=stale.pk).exists())
        self.assertTrue(HallExitSnapshot.objects.filter(pk=fresh.pk).exists())


class SimClaimEndpointTests(_SimFixture):
    """假接手端点：`POST /api/business/hall/sim/<pk>/claim/`。

    ⭐ X-01起带**可见性闸**：只有「App 令牌 + 没开个人免押 +
    没有冻结押金」的打手点得动演示卡（与读侧同一份判据）；`self.claimer`
    是 PC 会话夹具——本类里它专钉「被闸拒绝」的形状，能拿走卡的请求走
    `self.app_claim(...)`（AppToken 面）。
    """

    def setUp(self):
        super().setUp()
        self.claimer = make_logged_in_client(
            username="sim-claimer", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order",),
        )
        self.app_client, self.app_token_raw = _app_poster("sim-claimer", self.game)
        self.anon = Client(SERVER_NAME="tenant.test.com")

    def app_claim(self, card_pk):
        """以**合格 App 查看者**（免押关、无冻结）点假接手——能拿走卡的唯一夹具。"""
        return self.app_client.post(
            SIM_CLAIM_URL.format(pk=card_pk), HTTP_AUTHORIZATION="Bearer " + self.app_token_raw
        )

    def _card(self, n=70, *, status=HallSimulatedOrder.Status.ACTIVE):
        return HallSimulatedOrder.objects.create(
            game=self.game, display_order_no=_order_no(n),
            project_text="假接手夹具卡", price=Decimal("66.00"),
            status=status, generated_at=timezone.now(), payload={},
        )

    def test_claiming_an_active_card_retires_it_and_answers_1012_verbatim(self):
        """active → retired；**无论 rowcount** 回 `1012` + 逐字文案 + `data=None`。

        ⭐ 夹具 = App 令牌 + 免押关 + 无冻结（可见性闸放行的唯一形状；X-01 起
        PC 会话夹具拿不到卡——那条拒绝形状由下面的三条拒绝用例钉）。
        变异方向：把「条件更新」改成真建单 / 改成返回 code=0 ⇒ 分别红在
        「状态没变 / 无订单行」与「信封逐字」；把可见性闸摘掉 ⇒ 本条仍绿
        （闸只收窄、不放松——拒绝面由三条拒绝用例钉）。
        """
        card = self._card()
        response = self.app_claim(card.pk)
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG)
        self.assertIsNone(body["data"])
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.RETIRED)
        self.assertIsNotNone(card.retired_at)
        # ⛔ 全程没有新订单行（假接手不是真接单）。
        self.assertEqual(Order.all_objects.count(), self._orders_at_setup)

    def test_claiming_a_retired_card_still_answers_1012(self):
        """对已下架的卡再点：条件更新命中 0 行 ⇒ 仍是同一封信封（幂等）。"""
        card = self._card(71, status=HallSimulatedOrder.Status.RETIRED)
        response = self.app_claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG)

    def test_pc_session_viewer_gets_the_taken_envelope_and_no_retire(self):
        """⭐ X-01 拒绝三态①（PC 会话）：持节点、免押关、无冻结，**但请求是会话面**。

        回包与「已被接走」**逐字相同**（`1012` + 同一句人话）且**卡片不下架**——
        与读侧同口径：不给「存在但你看不见」的差别留缝。
        变异方向：把 `post()` 开头的 `viewer_sees_simulated` 闸摘掉 ⇒ 本条红
        （卡被下架）。
        """
        card = self._card(74)
        response = self.claimer.post(SIM_CLAIM_URL.format(pk=card.pk))
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG)
        self.assertIsNone(body["data"])
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.ACTIVE, "PC 会话把演示卡拿走了")

    def test_quota_on_app_viewer_gets_the_taken_envelope_and_no_retire(self):
        """⭐ X-01 拒绝三态②（个人免押）：App 令牌 + `deposit_quota_enabled=True`。

        老打手读面看不见演示卡 ⇒ 写侧同样拿不到；回「已被接走」逐字信封、卡不动。
        变异方向：摘可见性闸 ⇒ 本条红。
        """
        StaffProfile.objects.filter(user=User.objects.get(username="sim-claimer")).update(
            deposit_quota_enabled=True
        )
        card = self._card(75)
        response = self.app_claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG)
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.ACTIVE, "免押查看者把演示卡拿走了")

    def test_frozen_deposit_app_viewer_gets_the_taken_envelope_and_no_retire(self):
        """⭐ X-01 拒绝三态③（有冻结押金）：App 令牌 + 免押关 + `frozen > 0`。

        第三道判据（业务方确认）对写侧同宽。`frozen=0` 的边界归
        「可见」一族，由读侧用例（`test_hall_api`）与押金闸用例覆盖——这里钉
        「冻结着钱 ⇒ 拒绝」这半边。变异方向：摘可见性闸 ⇒ 本条红。
        """
        from apps.accounts.models import TakerWallet

        TakerWallet.objects.create(
            user=User.objects.get(username="sim-claimer"), frozen=Decimal("50.00")
        )
        card = self._card(76)
        response = self.app_claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG)
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.ACTIVE, "冻着押金的打手把演示卡拿走了")

    def test_anonymous_and_nodeless_callers_are_rejected_before_any_write(self):
        """匿名 401；持证无节点 403——卡不被下架（准入先于写）。"""
        card = self._card(72)
        anon_response = self.anon.post(SIM_CLAIM_URL.format(pk=card.pk))
        self.assertEqual(anon_response.status_code, 401)
        no_node = make_logged_in_client(
            username="sim-no-node", groups=("客服",), games=[self.game], nodes=(),
        )
        no_node_response = no_node.post(SIM_CLAIM_URL.format(pk=card.pk))
        self.assertEqual(no_node_response.status_code, 403)
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.ACTIVE)

    def test_only_post_is_routed(self):
        """「只有 POST」：持证打 GET ⇒ 405（带转换器的路由逃出显式路由腿，这里 HTTP 钉）。"""
        card = self._card(73)
        response = self.app_client.get(
            SIM_CLAIM_URL.format(pk=card.pk), HTTP_AUTHORIZATION="Bearer " + self.app_token_raw
        )
        self.assertEqual(response.status_code, 405, response.content[:200])


class SimClaimDepositGateTests(_SimFixture):
    """假接手的**押金闸**（2026-10-24 业务方改准「和正常订单一样的流程」）。

    闸开着时：押金不够 ⇒ `1013` + **与真单逐字相同**的押金人话，且**卡片不下架**
    （真单抢输也不下架，同一形状）；够 ⇒ `1012` 下架，且**一分钱没动**（校验包在
    回滚事务里，结算 / 冻结当场撤销）。闸关着 ⇒ 完全不碰押金（对照组）。
    ⭐ X-01起可见性闸排在押金闸**之前**：本类夹具全部换成
    「App 令牌 + 免押关 + 无冻结」的合格查看者（能走到押金闸的唯一形状）；
    开免押的人到不了押金闸——那条由可见性闸拒绝用例钉（见上类）。
    """

    def setUp(self):
        super().setUp()
        make_logged_in_client(
            username="sim-gate-claimer", groups=("代练员",), games=[self.game],
            nodes=("grab_hall.take_order",),
        )
        self.claimer_user = User.objects.get(username="sim-gate-claimer")
        self.app_client, self.app_token_raw = _app_poster("sim-gate-claimer", self.game)
        from apps.accounts.models import TakerWallet

        self.wallet = TakerWallet.objects.create(
            user=self.claimer_user, balance=Decimal("0.00")
        )

    def _claim(self, card_pk):
        return self.app_client.post(
            SIM_CLAIM_URL.format(pk=card_pk), HTTP_AUTHORIZATION="Bearer " + self.app_token_raw
        )

    def _wallet_frozen_balance(self):
        self.wallet.refresh_from_db()
        return self.wallet.frozen, self.wallet.balance

    def _card(self, n=80, *, status=HallSimulatedOrder.Status.ACTIVE):
        return HallSimulatedOrder.objects.create(
            game=self.game, display_order_no=_order_no(n),
            project_text="押金闸夹具卡", price=Decimal("66.00"),
            status=status, generated_at=timezone.now(), payload={},
        )

    def test_gate_on_with_insufficient_funds_answers_verbatim_deposit_message(self):
        """闸开 + 零钱 ⇒ `1013` + 「押金不足：…」逐字；**卡片不下架**、钱包零写入。"""
        with override_settings(DEPOSIT_ENFORCED=True):
            card = self._card(80)
            response = self._claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1013, body)
        self.assertIn("押金不足", body["msg"], body)
        self.assertIn("请先充值", body["msg"], body)
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.ACTIVE, "押金不够却把卡拿走了")
        frozen, balance = self._wallet_frozen_balance()
        self.assertEqual((frozen, balance), (Decimal("0.00"), Decimal("0.00")))

    def test_gate_on_with_enough_balance_answers_1012_without_touching_money(self):
        """闸开 + 余额充足 ⇒ `1012` 下架照旧；**余额 / 冻结一分未动**（回滚事务的证明）。"""
        from apps.business.models import HallSimulatedOrder

        self.wallet.balance = Decimal("1000.00")
        self.wallet.save(update_fields=["balance"])
        with override_settings(DEPOSIT_ENFORCED=True):
            card = self._card(81)
            response = self._claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG)
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.RETIRED)
        frozen, balance = self._wallet_frozen_balance()
        self.assertEqual((frozen, balance), (Decimal("0.00"), Decimal("1000.00")))

    def test_gate_on_quota_on_viewer_is_blocked_by_visibility_before_the_deposit_gate(self):
        """⭐ X-01：免押查看者连押金闸都到不了——可见性闸先行，回「已被接走」逐字信封。

        旧形状（免押 + 额度 0 ⇒ 走到押金闸拿 `1012`）已废：免押的人在**读侧就
        看不见**演示卡，写侧 `post()` 第一道闸就把他挡在 `ensure_deposit_for_claim`
        之前 ⇒ 卡不下架、钱包零写入、`1013` 的差别根本不出现（存在性 oracle 关死）。
        变异方向：把可见性闸挪到押金闸之后 ⇒ 本条红（回包变成 `1013` 押金人话）。
        """
        from apps.accounts.models import StaffProfile

        StaffProfile.objects.filter(user=self.claimer_user).update(
            deposit_quota_enabled=True
        )
        with override_settings(DEPOSIT_ENFORCED=True):
            card = self._card(82)
            response = self._claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], SIM_MSG, "拒绝信封必须与「已被接走」逐字相同，不露押金差别")
        card.refresh_from_db()
        self.assertEqual(card.status, HallSimulatedOrder.Status.ACTIVE, "免押查看者把演示卡拿走了")
        frozen, balance = self._wallet_frozen_balance()
        self.assertEqual((frozen, balance), (Decimal("0.00"), Decimal("0.00")))

    def test_gate_off_skips_the_deposit_check_entirely(self):
        """闸关（默认环境）+ 零钱 ⇒ `1012` 照旧——「闸关不碰押金」的对照组。"""
        card = self._card(83)
        response = self._claim(card.pk)
        body = response.json()
        self.assertEqual(body["code"], 1012, body)
        frozen, balance = self._wallet_frozen_balance()
        self.assertEqual((frozen, balance), (Decimal("0.00"), Decimal("0.00")))
