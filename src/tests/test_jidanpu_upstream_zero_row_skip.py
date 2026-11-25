"""上家线 D-0321（2026-10-29 业务方拍板「批，开零行游戏隔离」）：零行游戏跳过态。

背景（生产实锤）：某绑定（180 秒/轮、逐游戏拉取）里「游戏伊莫」查询每轮
ServerError ⇒ 按 D-0266 的 fail-safe 每轮都判「拉取不完整」⇒ 下架检测停摆（停滞卡
堆积 220+），且拉取按游戏顺序走 ⇒ 伊莫**后面**的游戏连新卡都进不来。

修法（只豁免零行游戏）：连败 3 轮（约 9 分钟）⇒ 该绑定对该游戏进「跳过态」——不再逐轮
查询（每小时探一次当发现通道），其失败不进本轮完整性判定 ⇒ 其余游戏的完整性判定与
消失判定照常恢复；探测成功或该游戏长出第一张 `listed` 行 ⇒ 退出跳过态。
⚠️ **已有 `listed` 行的游戏一字不豁免**（那些卡还在大厅里，宁停勿误判）。

判据打**真实出口**（任务书口径：轮判定结果 / 日志 / 行状态 / 桩的调用集合）：
- 五条用例：① 连败 3 轮 ⇒ 第 4 轮起不查、本轮判完整、消失判定恢复；
  ② 有行游戏连败 ⇒ 整轮不判消失（fail-safe 一字不宽）；③ 探测成功 ⇒ 退出跳过、恢复每轮查询；
  ④ 跳过态下出现 listed 行 ⇒ 立即退出；⑤ 每小时探测节奏（`now` 注入——仓里现成做法）。
- 变异敏感：把跳过态分支的「不查」拆掉 ⇒ ①红；把「零行才豁免」放宽成「全豁免」⇒ ②红。
  （D-0266 时代的既有分界钉——异常壳整轮 fail-closed——在
  `test_jidanpu_upstream_pull.py` / `test_jidanpu_upstream_gone_failclosed.py` 照旧承重。）
⭐ **D-0366迁移注**：无游戏名全量路随该决策停用 ⇒ 本文件桩里旧的
`rows=`（无游戏名路）夹具形状一并退役；跳过态游戏行的唯一来路 = **每小时探测**
（④已按此重写）。夹具一律合成值：游戏名 `零行失败游戏` / `零行健康游戏`，单号
`2026102900000000xxxx`（全零段）；状态容器是**进程内**模块级 dict ⇒ setUp/tearDown
都要清（防跨用例串味）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_upstream
from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
)
from tests.test_jidanpu_upstream_pull import BASE_URL, _StubClient

_LISTED = JidanpuUpstreamHallOrder.Status.LISTED
_GONE = JidanpuUpstreamHallOrder.Status.GONE

#: 现场轮距（180 秒/轮）——只用 `now` 注入推进时间，不 sleep。
ROUND = timedelta(seconds=180)

_LOGGER = "apps.business.jidanpu_upstream"


class _WarningCollector(logging.Handler):
    """收 WARNING 及以上日志的临时 handler（`assertNoLogs` 没法按消息文本过滤）。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class ZeroRowGameSkipTests(TenantTestCase):
    """D-0321：零行游戏的跳过态（连败 3 轮进、探测成功 / 出现 listed 行出）。"""

    def setUp(self):
        jidanpu_upstream._ZERO_ROW_GAME_SKIP.clear()
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username="up-zrgi", password="pw-zrgi")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-zrgi",
            password="pw-canary-zrgi", enabled=True,
        )
        #: 坏游戏**先建**（同 sort_order 下按 id 升序 ⇒ 逐游戏循环里排在前）——
        #: 生产现场的形态：伊莫在前，它挂了后面的游戏跟着遭殃。
        self.game_bad = Game.objects.create(name="零行失败游戏")
        self.game_ok = Game.objects.create(name="零行健康游戏")

    def tearDown(self):
        jidanpu_upstream._ZERO_ROW_GAME_SKIP.clear()

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def _hall_row(self, *, order_id, game_name):
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "gameName": game_name,
            "gameArea": "安卓", "gameServer": "微信区",
            "project": "合成项目", "orderRemark": "合成备注", "roleName": "合成角色",
            "recFee": "88.00", "createTime": "2026-10-29 11:46:54",
        }

    def _row_ok(self, order_id="20261029000000000201"):
        return self._hall_row(order_id=order_id, game_name=self.game_ok.name)

    def _mirror(self, *, order_id, game):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL, upstream_order_id=order_id,
            status=_LISTED, game=game, owner_binding=self.binding,
            first_seen_at=self.now,
        )

    def _stub(self, *, per_game_ok=True, fail_bad=True, per_game_bad=None):
        per_game = {}
        if per_game_ok:
            per_game[self.game_ok.name] = [self._row_ok()]
        if per_game_bad is not None:
            per_game[self.game_bad.name] = per_game_bad
        return _StubClient(
            per_game_rows=per_game,
            fail_game_names={self.game_bad.name} if fail_bad else frozenset(),
        )

    def _asked(self, stub, game_name):
        return [q for _m, _p, q in stub.calls if q.get("gameName") == game_name]

    def _learn_skip(self, *, rounds=3):
        """连败 `rounds` 轮把 game_bad 打进跳过态；回每轮的 outcome（都该是不完整）。"""
        outcomes = []
        for i in range(1, rounds + 1):
            outcomes.append(jidanpu_upstream.sync_hall(
                binding=self.binding, client=self._stub(),
                now=self.now + ROUND * i,
            ))
        return outcomes

    # ── ① 连败 3 轮 ⇒ 第 4 轮起不查、判完整、消失判定恢复 ──────────────────

    def test_zero_row_game_is_skipped_from_round_4_and_delisting_recovers(self):
        vanished = self._mirror(order_id="20261029000000000101", game=self.game_ok)
        with self.assertLogs(_LOGGER, level="WARNING") as wcm:
            outcomes = self._learn_skip()
        # 学习期（第 1–3 轮）：fail-safe 照旧——每轮都判不完整、谁都不判消失。
        for i, outcome in enumerate(outcomes, start=1):
            self.assertFalse(outcome.complete, f"学习期第 {i} 轮不该判完整")
            self.assertIn(self.game_bad.name, outcome.reason)
            self.assertEqual(outcome.gone, 0)
        vanished.refresh_from_db()
        self.assertEqual(vanished.status, _LISTED, "学习期把应消失的行标走了（fail-safe 被放宽）")
        # 进跳过态的日志**恰好一行**（不许每轮刷屏）。
        enter_lines = [r for r in wcm.records if "进入跳过态" in r.getMessage()]
        self.assertEqual(len(enter_lines), 1, "进跳过态应只打一行日志")

        # 第 4 轮：game_bad 处于跳过态 ⇒ 不再查询 ⇒ 完整性对其余游戏成立、消失判定恢复。
        with self.assertLogs(_LOGGER, level="INFO") as cm:
            stub = self._stub()
            outcome = jidanpu_upstream.sync_hall(
                binding=self.binding, client=stub, now=self.now + ROUND * 4,
            )
        self.assertTrue(outcome.complete, "剔除跳过态游戏后本轮应判完整")
        self.assertEqual(outcome.reason, "")
        self.assertEqual(outcome.gone, 1, "消失判定没有恢复")
        vanished.refresh_from_db()
        self.assertEqual(vanished.status, _GONE, "应消失的行没有被标走")
        self.assertIsNotNone(vanished.gone_at)
        self.assertEqual(self._asked(stub, self.game_bad.name), [], "第 4 轮仍在查询跳过态游戏")
        self.assertTrue(self._asked(stub, self.game_ok.name), "其余游戏照常查询")
        # 审计口径：判完整的轮把被剔除的游戏名写清；且第 4 轮没有再打「进入跳过态」。
        self.assertTrue(
            any("剔除" in r.getMessage() and self.game_bad.name in r.getMessage()
                for r in cm.records),
            "判完整的轮没有把剔除的游戏名写进日志（审计看不出「伊莫被跳过」）",
        )
        self.assertFalse(
            any("进入跳过态" in r.getMessage() for r in cm.records),
            "跳过态已生效的轮不该再打进态日志",
        )

    # ── ② 有行游戏连败 ⇒ 整轮不判消失（fail-safe 一字不宽）────────────────

    def test_a_game_with_listed_rows_never_gets_exempted(self):
        """game_bad 名下**有** listed 镜像 ⇒ 连败 5 轮也不进跳过态、每轮照查、整轮不判消失。

        变异敏感（两根钉）：把「零行才豁免」放宽成「全豁免」后，`_exit_zero_row_skips_with_rows`
        会在进态的同一轮末尾把它拉回（防纵深）⇒ **行为断言仍绿**；但那道放宽会真的**写进
        跳过态并打「进入跳过态」日志** ⇒ 末尾两根钉（进态日志为零 + 状态容器无条目）必红。
        """
        at_risk = self._mirror(order_id="20261029000000000101", game=self.game_bad)
        collector = _WarningCollector()
        logger = logging.getLogger(_LOGGER)
        logger.addHandler(collector)
        try:
            for i in range(1, 6):
                stub = self._stub()
                outcome = jidanpu_upstream.sync_hall(
                    binding=self.binding, client=stub, now=self.now + ROUND * i,
                )
                self.assertFalse(outcome.complete, f"第 {i} 轮：有行游戏的失败不该被豁免")
                self.assertIn(self.game_bad.name, outcome.reason)
                self.assertEqual(outcome.gone, 0)
                self.assertTrue(
                    self._asked(stub, self.game_bad.name),
                    f"第 {i} 轮：有行游戏不该进跳过态（仍在每轮查询才对）",
                )
        finally:
            logger.removeHandler(collector)
        at_risk.refresh_from_db()
        self.assertEqual(at_risk.status, _LISTED, "有行游戏的在挂镜像被误标 gone")
        self.assertIsNone(at_risk.gone_at)
        # 钉①：有行游戏**从不进跳过态** ⇒ 不该出现任何「进入跳过态」日志。
        self.assertFalse(
            any("进入跳过态" in r.getMessage() for r in collector.records),
            "有行游戏被写进了跳过态（「零行才豁免」被放宽）",
        )
        # 钉②：状态容器里不该有它的任何条目（连败计数都不该留）。
        states = jidanpu_upstream._ZERO_ROW_GAME_SKIP.get(
            jidanpu_upstream._hall_round_key(self.binding), {}
        )
        self.assertNotIn(
            self.game_bad.pk, states,
            "有行游戏在状态容器里留了条目（「零行才豁免」被放宽）",
        )

    # ── ②b 查询成功即清零连败──────────────────

    def test_a_successful_round_resets_the_consecutive_failure_count(self):
        """失败 2 轮 → 第 3 轮成功 → 再失败 2 轮 ⇒ **仍不进跳过态**（连败未满 3）。

        五条主用例都是直线连败；若将来有人误删成功路径上的清零（`_pull_per_game_rows`
        成功分支的 `skip_states.pop`），「断续失败」会被累计成连败 ⇒ 提前进跳过态。
        变异敏感：删掉清零 ⇒ 第 4 轮 counter 到 3（进态日志出现 + 第 5 轮 game_bad
        被跳过不查询）⇒ 本用例三处断言一起红。
        """
        collector = _WarningCollector()
        logger = logging.getLogger(_LOGGER)
        logger.addHandler(collector)
        try:
            # 第 1–2 轮：失败（counter 1→2）。
            for i in (1, 2):
                stub = self._stub()
                outcome = jidanpu_upstream.sync_hall(
                    binding=self.binding, client=stub, now=self.now + ROUND * i,
                )
                self.assertFalse(outcome.complete)
            # 第 3 轮：查询**成功**（success-empty 的成功也算成功）⇒ 连败清零。
            stub = self._stub(fail_bad=False, per_game_bad=[])
            outcome = jidanpu_upstream.sync_hall(
                binding=self.binding, client=stub, now=self.now + ROUND * 3,
            )
            self.assertTrue(outcome.complete, "第 3 轮全成功却没判完整")
            self.assertTrue(self._asked(stub, self.game_bad.name))
            # 第 4–5 轮：再失败 2 轮 ⇒ counter 只能是 1→2（重新数的），未满 3。
            for i in (4, 5):
                stub = self._stub()
                outcome = jidanpu_upstream.sync_hall(
                    binding=self.binding, client=stub, now=self.now + ROUND * i,
                )
                self.assertFalse(
                    outcome.complete,
                    f"第 {i} 轮：断续失败被累计成连败，提前进了豁免",
                )
                self.assertTrue(
                    self._asked(stub, self.game_bad.name),
                    f"第 {i} 轮：断续失败被累计成连败，game_bad 被跳过没查询",
                )
        finally:
            logger.removeHandler(collector)
        # 钉①：全程不该出现「进入跳过态」日志（成功清零后连败从未满 3）。
        self.assertFalse(
            any("进入跳过态" in r.getMessage() for r in collector.records),
            "成功轮没有清零连败（断续失败被累计，提前进了跳过态）",
        )
        # 钉②：状态容器里的条目是**重新数的 2**（不是累计 4），且不在跳过态。
        state = jidanpu_upstream._ZERO_ROW_GAME_SKIP.get(
            jidanpu_upstream._hall_round_key(self.binding), {}
        ).get(self.game_bad.pk)
        self.assertIsNotNone(state, "第 4–5 轮的失败没被记账（连败计数丢了）")
        self.assertEqual(state.consecutive_failures, 2, "连败没有被成功轮清零")
        self.assertFalse(state.skip)

    # ── ③ 探测成功 ⇒ 退出跳过、恢复每轮查询 ────────────────────────────────

    def test_probe_success_exits_skip_and_resumes_roundly_queries(self):
        self._learn_skip()
        learned_at = self.now + ROUND * 3
        # 半小时后：未到探测点 ⇒ 本轮一次都不查 game_bad。
        stub = self._stub()
        jidanpu_upstream.sync_hall(
            binding=self.binding, client=stub, now=learned_at + timedelta(minutes=30),
        )
        self.assertEqual(self._asked(stub, self.game_bad.name), [], "探测点未到不该查")

        # 61 分钟后：探测到点 ⇒ 查一次且**成功** ⇒ 退出跳过态（一行日志）。
        with self.assertLogs(_LOGGER, level="INFO") as cm:
            stub = self._stub(fail_bad=False, per_game_bad=[])
            outcome = jidanpu_upstream.sync_hall(
                binding=self.binding, client=stub,
                now=learned_at + timedelta(minutes=61),
            )
        self.assertTrue(self._asked(stub, self.game_bad.name), "探测到点没有去探")
        self.assertTrue(outcome.complete)
        self.assertTrue(
            any("跳过态解除" in r.getMessage() and self.game_bad.name in r.getMessage()
                for r in cm.records),
            "探测成功退出跳过态没有打日志",
        )

        # 3 分钟后再一轮（仍在原探测窗口内）：恢复**每轮**查询——若没退出跳过态，
        # 这一刻不该被查（上一小时窗还没走完）。
        stub = self._stub(fail_bad=False, per_game_bad=[])
        outcome = jidanpu_upstream.sync_hall(
            binding=self.binding, client=stub,
            now=learned_at + timedelta(minutes=64),
        )
        self.assertTrue(
            self._asked(stub, self.game_bad.name),
            "探测成功后没有恢复每轮查询（还困在跳过态的探测节奏里）",
        )
        self.assertTrue(outcome.complete)

    # ── ④ 跳过态下出现 listed 行 ⇒ 立即退出 ────────────────────────────────

    def test_probe_success_exits_skip_and_persists_the_first_listed_row(self):
        """⭐ D-0366 迁移注：旧行为「无游戏名路带进第一张 listed 行 ⇒ 立即退出」随
        无游戏名路退役——跳过态里行的**唯一来路**是每小时探测。本用例钉：探测成功
        带回第一张 listed 行 ⇒ **该轮**就落库 + 跳过态退出（容器空），下一轮恢复每轮查询。
        """
        self._learn_skip()
        learned_at = self.now + ROUND * 3
        row_bad = self._hall_row(
            order_id="20261029000000000301", game_name=self.game_bad.name,
        )
        with self.assertLogs(_LOGGER, level="INFO") as cm:
            stub = self._stub(fail_bad=False, per_game_bad=[row_bad])
            outcome = jidanpu_upstream.sync_hall(
                binding=self.binding, client=stub,
                now=learned_at + timedelta(minutes=61),
            )
        self.assertTrue(outcome.complete)
        self.assertTrue(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id=row_bad["orderId"], status=_LISTED,
                game=self.game_bad,
            ).exists(),
            "探测带进来的 listed 行没有落库",
        )
        self.assertTrue(
            any("跳过态解除" in r.getMessage() and self.game_bad.name in r.getMessage()
                for r in cm.records),
            "探测成功退出跳过态没有打日志",
        )
        # 跳过态容器清空（探测成功即退出，不留残条目）。
        self.assertEqual(
            jidanpu_upstream._ZERO_ROW_GAME_SKIP.get(
                jidanpu_upstream._hall_round_key(self.binding), {}
            ),
            {},
        )

        # 下一轮（仍在原探测窗内）：game_bad 被**每轮查询** ⇒ 证明跳过态已退出。
        stub = self._stub(fail_bad=False, per_game_bad=[row_bad])
        jidanpu_upstream.sync_hall(
            binding=self.binding, client=stub,
            now=learned_at + timedelta(minutes=64),
        )
        self.assertTrue(
            self._asked(stub, self.game_bad.name),
            "探测成功后没有恢复每轮查询（还困在跳过态的探测节奏里）",
        )

    # ── ⑤ 每小时探测节奏（now 注入）───────────────────────────────────────

    def test_probe_rhythm_is_hourly(self):
        self._learn_skip()
        learned_at = self.now + ROUND * 3
        probe_at = learned_at + timedelta(minutes=61)
        schedule = [
            # (时刻, 该不该查 game_bad, 说明)
            (learned_at + timedelta(minutes=30), False, "进态后半小时：未到点"),
            (learned_at + timedelta(minutes=59), False, "59 分钟：仍未到点"),
            (probe_at, True, "61 分钟：到点探一次（失败 ⇒ 节奏重置）"),
            (probe_at + timedelta(minutes=10), False, "失败探测后 10 分钟：不该再探"),
            (probe_at + timedelta(minutes=61), True, "重置后再满 1 小时：再探一次"),
        ]
        for moment, should_ask, why in schedule:
            with self.subTest(why=why):
                stub = self._stub()
                outcome = jidanpu_upstream.sync_hall(
                    binding=self.binding, client=stub, now=moment,
                )
                self.assertEqual(
                    bool(self._asked(stub, self.game_bad.name)), should_ask,
                    f"{why}：game_bad 的查询节奏不对",
                )
                # 跳过态游戏（含探测失败）不进完整性判定 ⇒ 这些轮都该判完整。
                self.assertTrue(outcome.complete, f"{why}：本轮不该被判不完整")
