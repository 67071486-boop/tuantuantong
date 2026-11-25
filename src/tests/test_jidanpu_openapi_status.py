"""T7：鸡蛋铺官方 API 状态 → 团团通订单状态（映射 + 未识别口径）的判据。

## 这份测试守什么（逐条对着 spec §6 全节与 §13.4 的 E4 / E5 / E7）

1. ⭐ **映射表逐条**（spec §6.2）：一张**恰好集合**的表对照 + 逐行 `subTest`，期望值全是**字面量**
   （⛔ 不从生产常量取 —— 那是自指断言，常量被改坏时它不会红）。**含「未分配」**
   （F17 实测：新建单就是它，而文档的枚举里没有这个值）。
2. ⭐ **未识别状态**（E4）：给一个假的、没见过的值 ⇒ 走**明确口径**那一支
   （登记一条待人工核对 + 一个字都不写 + ⛔ 不是 `ISSUE` + ⛔ 不是静默 none）。
3. ⭐ **「该不该写」的判据只有一处**：AST 收全仓（定义恰好一次）+ 那个函数的**调用点计数**
   （本模块恰好一处）+ **逐格对拍**（本模块的裁决必须恒等于那一个函数）。
4. **E5 幂等**：同状态重复 ⇒ 不写（靠既有判据的「我方 == 目标 ⇒ 不写」那一格）。
5. **E7**：⛔ 没有时间入参（`timestamp` 偏 +8 小时 ⇒ 它一个字都不参与）。

## 两条纪律

- ⛔ **不碰库**：这一段的进与出都是**纯数据**（一个带 `.status` 的对象 + 几个字符串）
  ⇒ 用 `SimpleTestCase`（本仓 `test_order_status.py` 对纯函数也是这个做法）。
- ⛔ **夹具一律合成值**：订单号 `TT-SYNTH-…`、绑定 id 用小整数、认不出来的状态用
  「外星状态」（任何表里都不可能有的值）。
"""
import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.business import jidanpu_openapi, jidanpu_upstream

SRC_ROOT = Path(__file__).resolve().parents[1]
#: 「该不该写」那一个判据函数的名字（⛔ 全仓只许定义一次）。
CRITERION_NAME = "should_write_status"
#: 它在本仓的**唯一**定义处（spec §6.3 明令复用的那一个）。
CRITERION_HOME = "apps/business/jidanpu_upstream.py"

#: 我方 8 个状态的**字面量**（⛔ 不从 `OrderStatus` 取：那是自指断言）。
ALL_STATUSES = (
    "UNASSIGNED", "WAITING_GRAB", "IN_PROGRESS", "PENDING_REVIEW",
    "COMPLETED", "ISSUE", "PAUSED", "REFUNDED",
)

#: ⭐ 映射表的**逐行期望**（键与值都是字面量；spec §6.2 那一张表，含实测的「未分配」）。
EXPECTED_MAP: dict[str, str | None] = {
    "未分配": "UNASSIGNED",     # 实测 F17（文档枚举里没有这个值）
    "待上号": "IN_PROGRESS",    # 实测 F16（端到端）
    "代练中": "IN_PROGRESS",    # 读码
    "待验收": None,             # 读码（我方自己提交完成之后的回音 ⇒ 明确不映射）
    "已暂停": "PAUSED",         # 读码
    "问题单": "ISSUE",          # 读码
    "已完成": "COMPLETED",      # 读码
    "已退单": "REFUNDED",       # 读码 + ⚠️ 第一期不直接写（走跟退那条路）
    "待抢单": None,             # 读码（显示层的覆盖，数据里没有这个值）
}

#: 「跟退」那一档：`write=False` 是**路由**给出的，**显式枚举**在这里 ⇒ 豁免不是后门。
REFUND_ROWS = ("已退单",)

#: 一个**认不出来**的值（任何表里都不会有它）。
UNKNOWN_TOKEN = "外星状态"

#: ⭐ 全矩阵（9 行 × 8 个我方状态）里**唯一会写**的三格 —— 手算的字面量。
#: 推导（三步都用**既有判据**的口径，见 `jidanpu_upstream.should_write_status`）：
#: ① `mapped is None` ⇒ 不写（`待验收` / `待抢单`）；
#: ② 我方不是「代练中」⇒ 不写 ⇒ **只有 `ours=IN_PROGRESS` 那一列可能写**；
#: ③ 目标与我方相同 ⇒ 不写（幂等）⇒ `待上号` / `代练中`（都映射到 `IN_PROGRESS`）**永不写**；
#:    再叠加边存在性：入 `COMPLETED` 只剩「待验收 →」（`_COMPLETED_SOURCE`）⇒ `已完成` 也**永不写**。
#: 剩下的就是这三格：`未分配 → UNASSIGNED`、`已暂停 → PAUSED`、`问题单 → ISSUE`。
EXPECTED_WRITABLE_CELLS = {
    ("未分配", "IN_PROGRESS"),
    ("已暂停", "IN_PROGRESS"),
    ("问题单", "IN_PROGRESS"),
}


def _order(ours: str):
    """一个**只带 `.status`** 的载体（照 `should_write_status` 的既有签名；⛔ 不碰库、不建行）。"""
    return SimpleNamespace(status=ours)


def _plan(ours: str, remote):
    return jidanpu_openapi.plan_openapi_status(_order(ours), remote_status=remote)


def _python_files():
    """`src/**` 下所有 .py（⛔ 不进 `.venv` / `node_modules` / `__pycache__`）。"""
    skipped = {".venv", "node_modules", "__pycache__", ".git"}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if skipped & set(path.parts):
            continue
        yield path


def _definitions_of(name: str) -> dict[str, int]:
    """全仓按 AST 收「叫这个名字的函数定义」（文件名 → 定义个数）。"""
    found: dict[str, int] = {}
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )
        if count:
            found[str(path.relative_to(SRC_ROOT))] = count
    return found


# ── 1. 映射表逐条（含「未分配」）────────────────────────────────────────────────

class StatusMapTests(SimpleTestCase):
    def test_the_table_is_exactly_these_rows_with_literal_values(self):
        """⭐ **恰好集合**（两向）：多一行 / 少一行 / 值写成别的码都要红。

        ⛔ 弄红它：把 `未分配` 从表里删掉（少一行），或把它写成 `ISSUE`（值不对）。
        """
        actual = {
            key: (None if value is None else str(value))
            for key, value in jidanpu_openapi.OPENAPI_STATUS_MAP.items()
        }
        self.assertEqual(actual, EXPECTED_MAP)

    def test_every_row_maps_the_way_its_source_says(self):
        """逐行过一遍规划器：`plan(...).mapped` 逐条等于**字面量**（含 `None` 那两行）。"""
        for row, expected in EXPECTED_MAP.items():
            with self.subTest(row=row):
                self.assertEqual(
                    _plan("UNASSIGNED", row).mapped,
                    expected,
                    f"「{row}」这一行映射错了",
                )

    def test_the_five_login_line_values_are_not_carried_over(self):
        """⭐ spec §6.2 的 ⚠️ 段：`已撤销`/`撤销中`/`仲裁中`/`已仲裁`/`异常中` **刻意不在本表里**。

        登录式的底表把这几个映射成 `ISSUE`（读码），但那是**登录式**那一格的口径 ⇒
        本线一律走 §6.4（登记 + 人工核对）。⛔ 弄红它：把它们照抄进表并映射成 `ISSUE`。
        """
        for token in ("已撤销", "撤销中", "仲裁中", "已仲裁", "异常中"):
            with self.subTest(token=token):
                self.assertNotIn(token, jidanpu_openapi.OPENAPI_STATUS_MAP)
                plan = _plan("IN_PROGRESS", token)
                self.assertIsNone(plan.mapped)
                self.assertNotEqual(str(plan.mapped), "ISSUE")

    def test_a_login_line_code_is_not_a_key_here(self):
        """键的形状不一样（spec §6.2）：登录式的码（`WFP` 之类）在本表里**认不出来**。

        ⇒ 它必须走「待人工核对」那一支，⛔ 不许被当成某个状态、⛔ 也不许静默丢掉。
        """
        plan = _plan("IN_PROGRESS", "WFP")
        self.assertTrue(plan.unknown)
        self.assertIsNone(plan.mapped)

    def test_the_pool_row_can_roll_an_in_progress_order_back_to_unassigned(self):
        """⚠️ **这一格要读过再改**：`未分配` 在我方「代练中」时**会写**（回退成 `UNASSIGNED`）。

        依据链（每一环都指得出处）：spec §6.2 那一行的「写不写」栏写的是「⛔ 不写（**走 §6.3**）」
        ⇒ 交那**一个**判据判；而 `should_write_status` 的 ② 只要求「我方当前是代练中」、
        ③ 只要求这条边存在（`IN_PROGRESS → UNASSIGNED` 在整体放开的边表里存在）⇒ 结论是 `True`。

        ⛔ 本用例**不是**给这个行为背书：它是把这一格**钉住**，让它可见（若裁定「未分配 一律不写」，
        实现与这一格**同一次**改；那也是改 `should_write_status`/路由，⛔ 不是在别处再写一套判据）。
        """
        plan = _plan("IN_PROGRESS", "未分配")
        self.assertTrue(plan.write)
        self.assertEqual(str(plan.mapped), "UNASSIGNED")
        # ⚠️ 只在「代练中」这一列会写：别的我方状态一律不写（②那道闸）。
        for ours in ALL_STATUSES:
            if ours == "IN_PROGRESS":
                continue
            with self.subTest(ours=ours):
                self.assertFalse(_plan(ours, "未分配").write)

    def test_the_not_mapped_rows_only_register(self):
        """表里**明确写着 `None`** 的两行（待验收 / 待抢单）：不写、也不是「认不出来」。"""
        for token in ("待验收", "待抢单"):
            for ours in ALL_STATUSES:
                with self.subTest(token=token, ours=ours):
                    plan = _plan(ours, token)
                    self.assertFalse(plan.write)
                    self.assertIsNone(plan.mapped)
                    self.assertFalse(plan.unknown)
                    self.assertIsNone(plan.kind)
                    self.assertIn(token, plan.reason)

    def test_the_refund_row_is_routed_and_not_written(self):
        """⭐ `已退单`：**不直接写**（要走跟退那条路，spec §6.2 / §15-N4）⇒ 只登记 + 人工核对。

        ⚠️ 这是**路由**（那一档走哪条路），⛔ 不是第二个「该不该写」的判据 ——
        既有登录式那条线也是先路由 `follow_refund`、再轮到 `should_write_status`。
        """
        for ours in ALL_STATUSES:
            with self.subTest(ours=ours):
                plan = _plan(ours, "已退单")
                self.assertEqual(plan.route, jidanpu_openapi.ROUTE_REFUND)
                self.assertFalse(plan.write)
                self.assertEqual(str(plan.mapped), "REFUNDED")
                self.assertIn("跟退", plan.reason)
                self.assertIn("人工核对", plan.reason)


# ── 2. 未识别状态的口径（E4）───────────────────────────────────────────────────

class UnknownStatusTests(SimpleTestCase):
    def test_an_unknown_status_goes_down_the_explicit_path(self):
        """⭐ **E4**：没见过的值 ⇒ **订单一个字都不写** + 一条**必须登记**的台账 + 留痕。

        ⛔ 弄红它：把认不出来的值兜底成 `ISSUE`（本仓登录式那条线的兜底），
        或让它静默变成 `mapped=None, unknown=False`（= 与「待验收」那种明确不映射混成一团）。
        """
        for ours in ALL_STATUSES:
            with self.subTest(ours=ours):
                plan = _plan(ours, UNKNOWN_TOKEN)
                self.assertFalse(plan.write, "认不出来的状态⛔ 一个字节都不许写订单")
                self.assertIsNone(plan.mapped, "⛔ 不许猜一个映射（尤其不许猜成问题单）")
                self.assertTrue(plan.unknown)
                self.assertEqual(plan.kind, jidanpu_openapi.OPENAPI_UNMAPPED_STATUS_KIND)
                self.assertIn(UNKNOWN_TOKEN, plan.reason, "台账必须带上远端**原值逐字**")

    def test_the_unknown_path_is_not_silent_and_not_a_problem_order(self):
        """⭐ **E4 的 ②③**：⛔ 不许变成 `ISSUE`、⛔ 不许静默丢（必须留得下一条待人工核对）。"""
        plan = _plan("IN_PROGRESS", UNKNOWN_TOKEN)
        # ②：⛔ 不许变成问题单 —— 判据打在**结论**上（`mapped` 根本不是 `ISSUE`），
        # ⛔ 不去搜 reason 里有没有「问题单」那三个字：那句话本身就写着「不映射成问题单」
        # （用词判据在自家文案上是**恒假/恒真**的噪声，E-0011 家族）。
        self.assertIsNone(plan.mapped)
        self.assertNotEqual(str(plan.mapped), "ISSUE")
        self.assertNotIn("ISSUE", str(plan.mapped))
        # 判「静默丢」的口径：这条结论必须**带着可登记的凭据**（kind + 非空人话）
        # —— 一个「什么都不说」的 `mapped=None` 才是静默丢。
        self.assertTrue(plan.kind)
        self.assertTrue(plan.reason.strip())

    def test_the_unknown_kind_is_a_new_kind_that_fits_the_column(self):
        """spec §6.4-② 要求用**新**的 kind，且它要能存进台账那一列（`max_length=32`）。"""
        kind = jidanpu_openapi.OPENAPI_UNMAPPED_STATUS_KIND
        self.assertEqual(kind, "openapi.unmapped_status")
        self.assertLessEqual(len(kind), 32)
        # ⛔ 别与既有登录式那条线的 kind 混用（那是另一条线的事）
        for other in ("upstream.status_in", "upstream.content_in", "log.repull"):
            self.assertNotEqual(kind, other)

    def test_the_unknown_payload_carries_the_four_facts_and_no_credentials(self):
        """spec §6.4-②：台账要带上**远端原值 / 订单号 / 绑定 id / 查单时刻** 四件事实。

        ⛔ 同时钉住「不放凭据」（照 `_note_order` 的既有纪律）。
        """
        payload = jidanpu_openapi.unmapped_status_payload(
            remote_status=UNKNOWN_TOKEN, order_no="TT-SYNTH-0001", binding_id=7,
            queried_at=datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(payload["remote_status"], UNKNOWN_TOKEN)
        self.assertEqual(payload["order_no"], "TT-SYNTH-0001")
        self.assertEqual(payload["upstream_binding_id"], 7)
        self.assertEqual(payload["queried_at"], "2026-10-08T12:00:00+00:00")
        for name in payload:
            lowered = name.lower()
            for banned in ("secret", "token", "password", "client_id"):
                self.assertNotIn(banned, lowered, f"payload 的键 {name} 看着像凭据")

    def test_an_empty_authoritative_status_is_not_an_unknown_one(self):
        """「没有值」与「有个怪值」是两件事：前者⛔ 不写也不登记待人工核对（照既有口径）。"""
        for empty in ("", "   ", None):
            with self.subTest(value=repr(empty)):
                plan = _plan("IN_PROGRESS", empty)
                self.assertFalse(plan.write)
                self.assertFalse(plan.unknown)
                self.assertIsNone(plan.kind)
                self.assertIn("status 为空", plan.reason)


# ── 3. 幂等（E5）与「没有时间入参」（E7）────────────────────────────────────────

class IdempotenceAndTimeTests(SimpleTestCase):
    def test_the_same_status_is_never_written_again(self):
        """⭐ **E5**：同状态重复回调 ⇒ **不写**（靠既有判据的「我方 == 目标 ⇒ 不写」那一格）。

        ⛔ 弄红它：去掉那一格（本模块若另写一套判据、忘了幂等，本用例就会红）。
        """
        for row, expected in EXPECTED_MAP.items():
            if expected is None or row in REFUND_ROWS:
                # `None` 那两行根本不映射；跟退那一档走**路由**（它本来就不写），两者都另有用例。
                continue
            with self.subTest(row=row):
                plan = _plan(expected, row)
                self.assertFalse(plan.write)
                self.assertIn("一致", plan.reason)
                self.assertIn("幂等", plan.reason)

    def test_a_future_callback_timestamp_cannot_change_the_verdict(self):
        """⭐ **E7**：回调的 `timestamp` **一个字都不参与**（F19：偏 +8 小时）。

        ⚠️ 这是**结构性**判据（E7 的**行为**判据在消费者那条线上：T6 的 `action=10` 端到端）：
        它证明的是「这个判据函数**根本看不到**时间」⇒「未来时间就丢弃」这类逻辑**无处可写**。
        ⛔ 弄红它：给签名加一个 `timestamp=None`（或 `now`）入参。
        """
        future = "2099-12-31 23:59:59"
        with self.assertRaises(TypeError):
            jidanpu_openapi.plan_openapi_status(
                _order("IN_PROGRESS"), remote_status="待上号", timestamp=future,
            )
        with self.assertRaises(TypeError):
            jidanpu_openapi.plan_openapi_status(
                _order("IN_PROGRESS"), remote_status="待上号", callback_status="待上号",
            )

    def test_the_planner_reads_only_our_status_and_the_authoritative_value(self):
        """⭐ spec §6.1-B4：**回调里的 `status` 不是入参**（它只决定「要不要去查」）。"""
        params = set(inspect.signature(jidanpu_openapi.plan_openapi_status).parameters)
        self.assertEqual(params, {"order", "remote_status"})


# ── 4. 「该不该写」的判据只有一处 ───────────────────────────────────────────────

class SingleCriterionTests(SimpleTestCase):
    def test_the_criterion_is_defined_exactly_once_in_the_whole_tree(self):
        """⭐ AST 收全仓：`should_write_status` **恰好定义一次**，且就在既有的那个文件里。

        ⛔ 弄红它：把那个函数复制一份到别的模块（哪怕是"先放着"）⇒ 定义数变成 2。
        """
        found = _definitions_of(CRITERION_NAME)
        self.assertEqual(found, {CRITERION_HOME: 1}, f"该判据的定义处不唯一：{found}")

    def test_the_openapi_module_delegates_to_that_single_function_exactly_once(self):
        """⭐ 调用点计数：本模块**恰好一处**调用那个函数，且那一处就在规划器里。

        两半各自承重：
        - 「恰好一处」⇒ 模块里没有第二条同义实现（多一个调用点就是多一个真相源）；
        - 「在规划器里」⇒ 结论确实经过它（而不是绕过去自己算）。
        """
        tree = ast.parse(
            (SRC_ROOT / "apps/business/jidanpu_openapi.py").read_text(encoding="utf-8-sig")
        )
        definitions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "plan_openapi_status"
        ]
        self.assertEqual(len(definitions), 1, "规划器应当恰好定义一次")

        def delegating_calls(node):
            return [
                call for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == CRITERION_NAME
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "jidanpu_upstream"
            ]

        inside = delegating_calls(definitions[0])
        self.assertEqual(len(inside), 1, "规划器里对那一个判据的调用应当恰好一处")
        module_wide = delegating_calls(tree)
        self.assertEqual(len(module_wide), 1, "本模块里对那一个判据的调用点应当恰好一处")
        # ⛔ 也别在本模块里自己定义一个同名的
        self.assertFalse([
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == CRITERION_NAME
        ])

    def test_every_cell_agrees_with_the_shared_function(self):
        """⭐ **逐格对拍**：规划器的裁决必须**恒等于**那一个判据给出的裁决。

        ⛔ 弄红它：在本模块里另写一套「该不该写」（哪怕只差一格）⇒ 至少一格不一致。
        ⚠️ 豁免**显式枚举**在 `REFUND_ROWS`（那一档的 `write=False` 来自**路由**，
        由 `test_the_refund_row_is_routed_and_not_written` 单独钉）。
        """
        rows = [row for row in EXPECTED_MAP if row not in REFUND_ROWS] + [UNKNOWN_TOKEN]
        for row in rows:
            for ours in ALL_STATUSES:
                with self.subTest(row=row, ours=ours):
                    plan = _plan(ours, row)
                    expected, _ = jidanpu_upstream.should_write_status(
                        _order(ours), mapped=plan.mapped
                    )
                    self.assertIs(plan.write, expected)

    def test_the_whole_matrix_of_writes_is_exactly_these_three_cells(self):
        """⭐ 全矩阵（9 行 × 8 个我方状态）里**只有这三格会写** —— 手算的字面量对照。

        推导写在 `EXPECTED_WRITABLE_CELLS` 的注释里（三步都用既有判据的口径）。
        ⚠️ 这条把「复用同一个判据」的**后果**摊开：这一期的官方 API 线**基本不改状态**
        （`待上号` / `代练中` 这两档在既有闸门下永远写不了）—— 那是 spec §6.3「写入面只开一格」
        的既定代价，⛔ 不是本模块的缺陷；要放宽得另开一条线（spec §6.3 的 ⚠️ 段）。
        """
        cells = {
            (row, ours)
            for row in list(EXPECTED_MAP) + [UNKNOWN_TOKEN]
            for ours in ALL_STATUSES
            if _plan(ours, row).write
        }
        self.assertEqual(cells, EXPECTED_WRITABLE_CELLS)
