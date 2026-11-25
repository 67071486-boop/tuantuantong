"""⭐ 自家回环卡拦截（业务方 2026-10-29 拍板「代码根治回环」）：上家拉取侧识别**自家回环卡**、不建镜像。

现场（生产探针实锤）：租户对同一家鸡蛋铺**既当下家会员往外抛、又当上家往回拉**
⇒ 自家抛出去的单（抛出键 = `effective_source_order_no`，对方逐字落在 `orderIdView` 上）被对方
挂进大厅、再被自己的拉取轮拉回来，以「原价×0.765」量级出现在自家打手大厅（实测 19.13=25×0.765、
53.55=70×0.765）。修法 = `sync_hall` 在**本要新建镜像行**的那一支拦截（判据与口径见
`jidanpu_upstream._loop_throw_keys` 的 docstring）。

本文件钉五条（每条**钉值**，⛔ 不是「有就行」）：

① **抛出→拉回全链**：本地单（有 `source_order_no` + `JidanpuMemberOrderLink`）被上家大厅以
   `orderIdView = 抛出键` 拉回 ⇒ **不建镜像** + **一行** warning 日志 + **一行**台账；
② **幂等**：同一张回环卡第二轮再拉到 ⇒ 台账**仍一行**、日志**零行**（note_once 幂等 + 状态切换式日志）；
③ **承重反例（真第三方单）**：展示单号不在本地任何单上 ⇒ **照常建镜像**（`listed`）——
   ⛔ 这条防「把判据写成恒真 / 恒拦」；
④ **不误拦**：展示单号恰与本地某单同号但**来源无关**（同号单没有下家链路；或命中的是
   `order_no` 而那张单的抛出键是 `source_order_no`）⇒ **不拦**（宁漏勿误）；
⑤ **性能**：一轮 80 张、其中 5 张回环 ⇒ 判据查询（`Order` JOIN `JidanpuMemberOrderLink`）
   **每轮恰好 1 次**，且总查询数与「80 张全第三方」的轮**相等**（⛔ 不逐卡三连查）。

⚠️ 判据打**真实出口**（镜像行 / 台账行 / 日志行数），夹具一律**合成值**（全零段单号、
`…test.invalid` 域名）——照 `test_jidanpu_upstream_pull.py` 的同一形态。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
)
from apps.business.order_no import generate_order_no

BASE_URL = "https://jidanpu-loop.test.invalid"
MEMBER_URL = "https://jidanpu-loop-member.test.invalid"
PASSWORD = "pw-canary-loop"
BASE_TIME = datetime(2026, 10, 29, 10, 0, tzinfo=dt_timezone.utc)

#: 合成抛出键（我方 `source_order_no`，全零段一眼是夹具）。
THROWN_KEY = "33160000000000000001"
#: 合成上家单号（chuyin 大厅里那张回环卡的 `orderId`）。
LOOP_REMOTE_ID = "20261029000000000901"
#: 合成真第三方展示单号（不属于本租户任何单）。
FOREIGN_VIEW = "42000000000000009999"

LOOP_KIND = jidanpu_upstream.KIND_HALL_LOOP_INTERCEPT


class _StubClient:
    """`JidanpuClient` 替身（⭐ D-0366 后的取数形状：只按 `gameName` 段查、页大小 20）。

    行按 `gameName` 分段；段查询按 `page` 切页（夹具行 < 20 ⇒ 第 1 页全给、第 2 页空页
    = 探页确认拉全）。⛔ 不再有无游戏名全量路可演（D-0366 停用）。
    """

    PER_PAGE = 20

    def __init__(self, *, rows):
        self.segments: dict[str, list[dict]] = {}
        for row in rows:
            self.segments.setdefault(str(row.get("gameName", "") or ""), []).append(dict(row))
        self.calls: list[tuple[str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        assert path == jidanpu_upstream.HALL_PATH, f"未预期的端点：{method} {path}"
        query = dict(kwargs.get("query") or {})
        self.calls.append((method, query))
        source = self.segments.get(str(query.get("gameName", "") or ""), [])
        page = int(query.get("page", "1"))
        page_rows = source[(page - 1) * self.PER_PAGE: page * self.PER_PAGE]
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(source),
        }
        return [dict(row) for row in page_rows]


def hall_row(*, order_id, view, game_name="测试游戏甲", rec_fee="25.00"):
    return {
        "orderId": order_id,
        "orderIdView": view,
        "gameName": game_name,
        "gameArea": "安卓",
        "gameServer": "微信区",
        "project": "合成项目",
        "orderRemark": "合成备注",
        "roleName": "合成角色",
        "recFee": rec_fee,
        "createTime": "2026-10-29 09:46:54",
    }


class UpstreamLoopInterceptTests(TenantTestCase):
    """回环卡拦截的主行为网（①②③④）+ 性能判据（⑤）。"""

    def setUp(self):
        self.now = BASE_TIME
        self.game = Game.objects.create(name="测试游戏甲")
        upstream_user = User.objects.create_user(username="up-loop", password="pw-up")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=upstream_user, jidanpu_base_url=BASE_URL, account="acc-up-loop",
            password=PASSWORD, enabled=True,
        )
        member_user = User.objects.create_user(username="member-loop", password="pw-member")
        self.member_binding = JidanpuMemberBinding.objects.create(
            user=member_user, jidanpu_base_url=MEMBER_URL,
            account="acc-member-loop", password=PASSWORD, enabled=True,
        )

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_thrown_order(self, *, source_order_no=THROWN_KEY, order_no=None):
        """一张「已抛给下家鸡蛋铺」的本地单：`source_order_no`（或回落 `order_no`）+ 会员链路。"""
        order = Order.objects.create(
            order_no=order_no or generate_order_no(), game=self.game,
            status=OrderStatus.IN_PROGRESS, order_type="BOOST",
            total_price=Decimal("25.00"),
            source_order_no=source_order_no,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.member_binding, order=order,
            jidanpu_order_id="JDP-00000000000000000901", in_hall=True,
        )
        return order

    # ── ① 抛出→拉回全链：不建镜像 + 日志一行 + 台账一行 ─────────────────────

    def test_thrown_order_comes_back_is_intercepted(self):
        self.make_thrown_order()
        stub = _StubClient(rows=[
            hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY, rec_fee="19.13"),
            hall_row(order_id="20261029000000000902", view=FOREIGN_VIEW),
        ])
        with self.assertLogs("apps.business.jidanpu_upstream", level="WARNING") as logs:
            outcome = jidanpu_upstream.sync_hall(
                binding=self.binding, client=stub, now=self.now,
            )
        # 回环卡**没建镜像**；同轮的真第三方卡照常建。
        self.assertFalse(JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id=LOOP_REMOTE_ID,
        ).exists())
        self.assertTrue(JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id="20261029000000000902",
            status=JidanpuUpstreamHallOrder.Status.LISTED,
        ).exists())
        # 台账恰一行，钉 kind / target_ref / 人话 reason 与 payload 的上家单号。
        events = JidanpuSyncEvent.objects.filter(kind=LOOP_KIND)
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.target_type, "hall")
        self.assertEqual(event.target_ref, f"{BASE_URL}/{THROWN_KEY}")
        self.assertIn("回环卡拦截", event.reason)
        self.assertEqual(event.payload["upstream_order_id"], LOOP_REMOTE_ID)
        self.assertEqual(event.payload["upstream_binding_id"], self.binding.pk)
        # 日志恰一行（状态切换式：台账首写才打）。
        self.assertEqual(len(logs.output), 1)
        self.assertIn(THROWN_KEY, logs.output[0])
        # 回环卡计入 skipped、不算 listed。
        self.assertEqual(outcome.skipped, 1)
        self.assertEqual(outcome.listed, 1)

    # ── ② 同一张回环卡第二轮再拉到：不重复留痕（幂等）──────────────────────

    def test_second_round_does_not_duplicate_trace(self):
        self.make_thrown_order()
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY)])
        with self.assertLogs("apps.business.jidanpu_upstream", level="WARNING"):
            jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        # 第二轮：同一张卡再拉到 ⇒ 不新建台账（仍 1 行）、不再打日志（assertNoLogs）。
        with self.assertNoLogs("apps.business.jidanpu_upstream", level="WARNING"):
            jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 1)
        self.assertEqual(JidanpuUpstreamHallOrder.objects.count(), 0)

    # ── ③ 承重反例：真第三方单照常建镜像 ────────────────────────────────────

    def test_genuine_third_party_card_still_builds_mirror(self):
        # 本地**有**抛出去的单（判据查询有活干），但拉到的卡展示单号与它无关。
        self.make_thrown_order()
        stub = _StubClient(rows=[hall_row(order_id="20261029000000000903", view=FOREIGN_VIEW)])
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id="20261029000000000903")
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 0)
        self.assertEqual(outcome.listed, 1)
        self.assertEqual(outcome.skipped, 0)

    # ── ④ 不误拦：同号但来源无关的两档边界 ──────────────────────────────────

    def test_view_collision_without_member_link_is_not_intercepted(self):
        """④-a：展示单号恰等于某本地单的 `source_order_no`，但那张单**从没抛给任何下家**
        （无 `JidanpuMemberOrderLink`）⇒ 不拦——宁漏勿误，防把真第三方单挡在自家大厅外。"""
        Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.IN_PROGRESS, order_type="BOOST",
            total_price=Decimal("25.00"), source_order_no=THROWN_KEY,
        )
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=LOOP_REMOTE_ID)
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 0)

    def test_view_hits_order_no_but_source_key_differs_is_not_intercepted(self):
        """④-b：展示单号等于某**已抛出**单的内置 `order_no`，但那张单的抛出键是它非空白的
        `source_order_no`（`order_no` 不是我们抛出去的值）⇒ 不拦——「恰等于抛出键」的从严档。"""
        order_no = generate_order_no()
        self.make_thrown_order(source_order_no=THROWN_KEY, order_no=order_no)
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=order_no)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=LOOP_REMOTE_ID)
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 0)

    def test_fallback_key_order_no_is_intercepted(self):
        """④-c（①的回落档对照）：抛出键回落 `order_no` 的单（`source_order_no` 空白），
        回环卡的展示单号 = 它的 `order_no` ⇒ 照拦（`effective_source_order_no` 的两档都认）。"""
        self.make_thrown_order(source_order_no="", order_no=THROWN_KEY)
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id=LOOP_REMOTE_ID,
        ).exists())
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 1)

    # ── 存量回环行（命中即转 gone 且不复活；claiming/promoted 不动）────────

    def make_loop_mirror(self, *, status, last_seen_at=None, gone_at=None):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=LOOP_REMOTE_ID,
            status=status,
            game=self.game,
            owner_binding=self.binding,
            first_seen_at=last_seen_at or self.now,
            last_seen_at=last_seen_at or self.now,
            gone_at=gone_at,
        )

    def test_existing_listed_loop_row_goes_gone_and_stays_gone(self):
        """M-1-①：存量 `listed` 回环行命中 ⇒ 条件更新置 `gone`（`gone_at=now`、⛔ 不刷
        `last_seen`）；**下一轮卡还在厅再拉到 ⇒ 仍 gone 不复活**（D-0300 的「重新见到
        即复活」被拦截分支挡住——承重次序：检查排在复活分支之前）。"""
        earlier = datetime(2026, 10, 29, 8, 0, tzinfo=dt_timezone.utc)
        self.make_thrown_order()
        self.make_loop_mirror(status=JidanpuUpstreamHallOrder.Status.LISTED,
                              last_seen_at=earlier)
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=LOOP_REMOTE_ID)
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertEqual(mirror.gone_at, self.now)
        self.assertEqual(mirror.last_seen_at, earlier)  # ⛔ 不刷 last_seen
        # 第二轮：卡还在厅、行已是 gone ⇒ 不复活（不写 hall_rescued 台账）。
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued").exists())

    def test_existing_gone_loop_row_is_not_revived(self):
        """M-1-②：存量 `gone` 行命中 ⇒ 保持不复活（`gone` 状态原样、`gone_at` 不被清）。"""
        self.make_thrown_order()
        self.make_loop_mirror(status=JidanpuUpstreamHallOrder.Status.GONE, gone_at=self.now)
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=LOOP_REMOTE_ID)
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertEqual(mirror.gone_at, self.now)  # 建行时的初值，未被复活清空
        self.assertFalse(JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued").exists())

    def test_existing_claiming_and_promoted_loop_rows_stay_untouched(self):
        """M-1-③：`claiming` / `promoted` 命中 ⇒ 行状态**一律不翻**（防炸并发接单 /
        已转正的行），也不被置 gone。"""
        self.make_thrown_order()
        claiming = self.make_loop_mirror(status=JidanpuUpstreamHallOrder.Status.CLAIMING)
        promoted_mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id="20261029000000000905",
            status=JidanpuUpstreamHallOrder.Status.PROMOTED,
            game=self.game,
            owner_binding=self.binding,
            first_seen_at=self.now,
        )
        stub = _StubClient(rows=[
            hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY),
            hall_row(order_id="20261029000000000905", view=THROWN_KEY),
        ])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        claiming.refresh_from_db()
        promoted_mirror.refresh_from_db()
        self.assertEqual(claiming.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertEqual(promoted_mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertIsNone(promoted_mirror.gone_at)

    def test_existing_row_intercept_notes_exactly_once(self):
        """M-1-④：存量行命中 ⇒ 台账 / 日志**恰一次**；第二轮再拉到不重复留痕。"""
        self.make_thrown_order()
        self.make_loop_mirror(status=JidanpuUpstreamHallOrder.Status.LISTED)
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=THROWN_KEY)])
        with self.assertLogs("apps.business.jidanpu_upstream", level="WARNING") as logs:
            jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 1)
        with self.assertNoLogs("apps.business.jidanpu_upstream", level="WARNING"):
            jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 1)

    # ── ⑤ 性能：判据每轮恰一次批量查询，不逐卡三连查 ────────────────────────

    def test_loop_intercept_adds_exactly_one_batch_query(self):
        """判据查询（`Order` JOIN `JidanpuMemberOrderLink`）**每轮恰 1 次**，且与回环卡张数
        **无关**：75 张全第三方、5 张回环、75 张回环三个轮都只查这一发——⛔ 若实现退化成
        逐卡查，第三个轮会打 75 发（这条用例当场红）。"""
        third_party = [
            hall_row(order_id=f"20261029000000001{i:03d}", view=f"4200000000000000{i:03d}")
            for i in range(75)
        ]
        loop_ids = [f"20261029000000009{i:02d}" for i in range(75)]
        self.make_thrown_order()  # 1 张已抛出单 ⇒ 回环卡共用它的抛出键

        def _run(rows):
            with CaptureQueriesContext(connection) as ctx:
                jidanpu_upstream.sync_hall(
                    binding=self.binding, client=_StubClient(rows=list(rows)), now=self.now,
                )
            judge = [q["sql"] for q in ctx.captured_queries
                     if "business_jidanpumemberorderlink" in q["sql"]
                     and "business_order" in q["sql"]]
            # 顺带钉住本轮行为，防「判据没跑」的假绿：回环卡一张镜像都没建。
            built = JidanpuUpstreamHallOrder.objects.count()
            JidanpuUpstreamHallOrder.objects.all().delete()
            JidanpuSyncEvent.objects.all().delete()
            return len(judge), built

        few_loops = [hall_row(order_id=order_id, view=THROWN_KEY) for order_id in loop_ids[:5]]

        # 轮①：75 张全第三方（判据照查一发，无命中）。
        judge_queries, built = _run(third_party)
        self.assertEqual(judge_queries, 1)
        self.assertEqual(built, 75)

        # 轮②：75 张第三方 + 5 张回环。
        judge_queries, built = _run(list(third_party) + few_loops)
        self.assertEqual(judge_queries, 1)
        self.assertEqual(built, 75)

        # 轮③：75 张回环 + 5 张第三方（判据仍只一发——逐卡实现这里会是 75 发）。
        all_loops = [hall_row(order_id=order_id, view=THROWN_KEY) for order_id in loop_ids]
        judge_queries, built = _run(all_loops + third_party[:5])
        self.assertEqual(judge_queries, 1)
        self.assertEqual(built, 5)


# ══ ⭐ D-0325：回环拦截 × 上家镜像隐藏单的交互 ═════════════════
#
# 上家镜像卡派生的隐藏单（`Order.upstream_hall_card`，抛给下家鸡蛋铺的载体）参与回环
# 判据的方式与普通单**完全一致**：创建期它的 `source_order_no` = 上家内部单号（拍板④，
# 防误杀上游原卡），抛出去之后对方把它逐字落在 `orderIdView` 上 ⇒ 拉回时按**同一个**
# `_loop_throw_keys` 命中。
# ⭐ D-0342（改准）：**转正把键改写为上家展示号**（D-0272 全端口径）——
# 转正后回环语义反转：上家再挂同 view 号的**新卡**（不同 `orderId`、没有 promoted 行挡着）
# ＝我们正在下家做的单流回上家大厅＝真回环，改写后的键（=展示号）正确命中。
# 这一组钉四件事：新卡不建镜像（含隐藏单）；存量卡转 gone 时**它派生的隐藏单退款 + 撤下家
# 副本**（D-0325 的 gone 接线）；真上游卡（view ≠ 抛出键）不受影响；**转正后的单按新键
# 拦真回环**（行为矩阵第 3 格，`docs/plans/2026-10-30-promote-source-no.md` §2）。


class LoopInterceptWithUpstreamShadowTests(TenantTestCase):
    """隐藏单作为「抛出键持有人」参与回环拦截的三条交互。"""

    def setUp(self):
        self.now = BASE_TIME
        self.game = Game.objects.create(name="测试游戏甲")
        up_user = User.objects.create_user(username="up-loop-shadow", password="pw-up")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=up_user, jidanpu_base_url=BASE_URL, account="acc-up-loop-shadow",
            password=PASSWORD, enabled=True,
        )
        member_user = User.objects.create_user(
            username="member-loop-shadow", password="pw-member"
        )
        self.member_binding = JidanpuMemberBinding.objects.create(
            user=member_user, jidanpu_base_url=MEMBER_URL,
            account="acc-member-loop-shadow", password=PASSWORD, enabled=True,
        )

    def _listed_mirror(self, remote_id, *, rec_fee="25.00"):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=remote_id,
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            owner_binding=self.binding,
            remote_rec_fee=Decimal(rec_fee),
            first_seen_at=self.now,
        )

    def _shadow(self, mirror, *, order_no=None):
        order = Order.objects.create(
            order_no=order_no or generate_order_no(),
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("25.00"),
            total_price=Decimal("25.00"),
            source_order_no=mirror.upstream_order_id,
            upstream_hall_card=mirror,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.member_binding, order=order,
            jidanpu_order_id=f"JDP-{mirror.upstream_order_id[-6:]}", in_hall=True,
        )
        return order

    def test_thrown_shadow_copy_coming_back_builds_no_mirror(self):
        """① 抛出键的持有人是**隐藏单**（有链路）⇒ 我们抛出去的副本被拉回 ⇒ 不建镜像。"""
        mirror = self._listed_mirror(LOOP_REMOTE_ID)
        shadow = self._shadow(mirror)
        self.assertEqual(shadow.source_order_no, LOOP_REMOTE_ID)
        stub = _StubClient(rows=[
            # 我们抛给下家的那张副本：内部单号是对方的、展示号 = 我们的抛出键。
            hall_row(order_id="COPY-20261029000000000001", view=LOOP_REMOTE_ID),
        ])
        with self.assertLogs("apps.business.jidanpu_upstream", level="WARNING"):
            jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id="COPY-20261029000000000001",
        ).exists())
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 1)

    def test_intercepted_gone_also_refunds_the_shadow_and_withdraws(self):
        """② 存量回环卡转 gone ⇒ 它派生的隐藏单跟着 `REFUNDED` + 排 `member.withdraw`
        （D-0325 的 gone 接线：回环卡不撤 = 下家大厅留一张永动机卡）。"""
        mirror = self._listed_mirror(LOOP_REMOTE_ID)
        shadow = self._shadow(mirror)
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=LOOP_REMOTE_ID)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror.refresh_from_db()
        shadow.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        withdraws = JidanpuSyncEvent.objects.filter(
            kind="member.withdraw", target_ref=shadow.order_no, direction="out",
        )
        self.assertEqual(withdraws.count(), 1)

    def test_genuine_upstream_card_with_a_shadow_is_not_intercepted(self):
        """③ 真上游原卡（`orderIdView` ≠ 抛出键——内部号与展示号不同的常态）⇒ 照常刷新、
        隐藏单照常在待抢（⛔ 防把判据写成「有隐藏单就拦」的恒真形态）。"""
        mirror = self._listed_mirror(LOOP_REMOTE_ID)
        shadow = self._shadow(mirror)
        # 原卡的展示号与内部号不同（`orderIdView` 是上家自己的展示值）。
        stub = _StubClient(rows=[hall_row(order_id=LOOP_REMOTE_ID, view=FOREIGN_VIEW)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror.refresh_from_db()
        shadow.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 0)

    def test_promoted_order_intercepts_a_real_loop_by_the_display_key(self):
        """⭐ D-0342 行为矩阵第 3 格（2026-10-30）：**转正后**的单（键已改写为上家展示号）
        ⇒ 上家以**新 orderId** 重挂同展示号的卡＝真回环 ⇒ 拦（不建镜像 + 台账）。
        改准前键仍是内部号、这一格漏网（会建成第二张镜像 ⇒ 重复派生）。"""
        mirror = self._listed_mirror(LOOP_REMOTE_ID)
        shadow = self._shadow(mirror)
        # 转正后形状：列已清、单在 `IN_PROGRESS`、来源号 = 上家**展示号**（转正改写）。
        shadow.status = OrderStatus.IN_PROGRESS
        shadow.source_order_no = FOREIGN_VIEW
        shadow.upstream_hall_card = None
        shadow.save(update_fields=["status", "source_order_no", "upstream_hall_card"])
        stub = _StubClient(rows=[
            # 同展示号、**新 orderId**（没有 promoted 镜像行挡它）⇒ 只有回环判据能拦。
            hall_row(order_id="RELOOP-20261030000000001", view=FOREIGN_VIEW),
        ])
        with self.assertLogs("apps.business.jidanpu_upstream", level="WARNING"):
            jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id="RELOOP-20261030000000001",
        ).exists())
        self.assertEqual(JidanpuSyncEvent.objects.filter(kind=LOOP_KIND).count(), 1)
