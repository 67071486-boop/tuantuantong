"""`apps/business/project_tag_cleaning.py` 的判据（批次 1 计划 Task 2）。

⚠️ 纯逻辑，**不碰数据库**（`SimpleTestCase`）——引擎的设计前提就是零 Django 依赖。

## 这份网钉什么

1. 切段：成对括号（六种）里的内容**也是段**、且段序不乱；分隔符八个各自真的在切；
2. 剥噪音：命中即丢，且**丢的是哪一条、哪个段**都可读（`noise_dropped`）；
3. 归一：全角数字→半角、**15 位以上**的长数字串整串剔掉、去空白与全角空格；
4. ⭐ **「洗成空」是可观测的返回值**（实测约 12% 的订单会洗空，⛔ 不许静默丢弃）：
   空输入 / 纯噪音 / 纯订单号三条路都要返回 `is_empty=True` 而**不是** `None`、不抛异常；
5. 口径常量与规则是**数据**（`project_tag_rules`）：词表只有一处、能整套换掉（租户级覆盖）；
6. ⚠️ **已知误伤的登记**：噪音判据是「包含」，`<业务内容>＋噪音词` 同段时整段会被丢掉
   —— 这条**有意**钉住（它是口径，不是 bug），改口径就必须同一次改这条。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C），与任何真实租户 / 真实订单无关；
真实语料只在**不进 git** 的 `.tools/recon/`。
"""
from apps.business.project_tag_cleaning import (
    CleanResult,
    clean_order_text,
    noise_hit,
    normalize_segment,
    split_segments,
)
from apps.business.project_tag_rules import (
    DEFAULT_CLEANING_RULES,
    NOISE_PHRASES,
    CleaningRules,
)
from django.test import SimpleTestCase

#: 合成的一条客服模板（⛔ 不是任何真实话术，只是形状）。
NOISE_LINE = "留红留卡"
#: 合成的另一条（词表里排在 `不包主线` 前面的那条子串关系用它钉）。
NOISE_PARENT = "不包前置任务不包主线"


class SplitSegmentTests(SimpleTestCase):
    def test_delimiters_are_exactly_the_eight_from_the_spec(self):
        """八个分隔符各自真的在切（⛔ 少一个 / 多一个都要能红）。

        判据用**字面量**而不是 `SEGMENT_DELIMITERS` 常量：拿常量比常量时，
        把常量里的 `|` 删掉这条照样绿（E-0024）。
        """
        for delimiter in ("/", "、", "\n", "|", ";", "；", "➕", "+"):
            with self.subTest(delimiter=delimiter):
                self.assertEqual(
                    split_segments(f"甲{delimiter}乙"),
                    ("甲", "乙"),
                    f"{delimiter!r} 没有把两段切开",
                )

    def test_comma_and_colon_do_not_split(self):
        """`，` 与 `：` **不是**分隔符（设计稿 §2.2 的列表里没有它们）。

        反面判据不能省：把上面那八个之外再加两个，段会被切碎、业务内容被割开。
        """
        self.assertEqual(split_segments("甲，乙：丙"), ("甲，乙：丙",))

    def test_all_six_bracket_pairs_yield_their_inner_text(self):
        """六种括号的**内容**都是段（不是被丢掉）——真实文本里 `【5部门升4】` 就是项目名本体。"""
        for opened, closed, inner in (
            ("【", "】", "甲"),
            ("「", "」", "乙"),
            ("[", "]", "丙"),
            ("『", "』", "丁"),
            ("（", "）", "戊"),
            ("(", ")", "己"),
        ):
            with self.subTest(bracket=opened):
                self.assertIn(inner, split_segments(f"{opened}{inner}{closed}"))

    def test_bracket_content_is_flattened_in_place_not_moved_to_the_end(self):
        """段序不乱：`甲【乙】丙` ⇒ `甲`、`乙`、`丙`。

        ⚠️ 这条钉的是实现里的承重细节——摊平用的是**分隔符占位**而不是
        「先收括号、再拼尾巴」。把实现改成后者，这一条会红。
        """
        self.assertEqual(split_segments("甲【乙】丙"), ("甲", "乙", "丙"))

    def test_two_pairs_on_one_line_are_two_segments(self):
        """`【甲】【乙】` 是两段——贪婪匹配会把它吃成一段。"""
        self.assertEqual(split_segments("【甲】【乙】"), ("", "甲", "", "乙", ""))

    def test_empty_and_none_input_are_both_safe(self):
        self.assertEqual(split_segments(""), ("",))
        self.assertEqual(split_segments(None), ("",))


class NormalizeTests(SimpleTestCase):
    def test_fullwidth_digits_become_halfwidth(self):
        self.assertEqual(normalize_segment("１２３"), "123")

    def test_whitespace_and_ideographic_space_are_removed(self):
        self.assertEqual(normalize_segment("甲 乙\u3000丙\t丁"), "甲乙丙丁")

    def test_fifteen_digit_run_is_stripped_and_fourteen_is_kept(self):
        """「15 位以上」＝**含** 15 位：下界两侧都要钉，⛔ 只钉一侧是恒真的。"""
        fifteen = "1" * 15
        fourteen = "1" * 14
        self.assertEqual(normalize_segment(f"单{fifteen}号"), "单号")
        self.assertEqual(normalize_segment(f"单{fourteen}号"), f"单{fourteen}号")

    def test_fullwidth_order_number_is_also_stripped(self):
        """⚠️ 顺序是承重的：先转半角再剔长数字串，否则全角订单号躲过判据。"""
        fullwidth = "１" * 15
        self.assertEqual(normalize_segment(f"单{fullwidth}号"), "单号")


class NoiseTests(SimpleTestCase):
    def test_every_default_noise_phrase_is_dropped_when_it_is_a_whole_segment(self):
        """词表里**每一条**都要真的生效（逐条跑，⛔ 不是抽查一条）。

        ⚠️ 如果词表里混进一条永远不可能命中的空串，这里会红。
        """
        for phrase in NOISE_PHRASES:
            with self.subTest(phrase=phrase):
                result = clean_order_text(phrase)
                self.assertTrue(result.is_empty)
                self.assertEqual(result.noise_dropped, (phrase,))

    def test_noise_hit_returns_the_first_matching_phrase(self):
        """一段命中多条时报**第一个**——所以 `不包前置任务不包主线` 必须排在 `不包主线` 前面。"""
        self.assertEqual(noise_hit(NOISE_PARENT), NOISE_PARENT)
        self.assertEqual(noise_hit("x不包主线y"), "不包主线")
        self.assertIsNone(noise_hit("深境螺旋12层"))

    def test_known_over_strip_is_pinned_on_purpose(self):
        """⚠️ **已知误伤，有意钉住，⛔ 不是 bug**（口径 = 任务书原文「命中噪音词的段丢掉」）。

        判据是**包含**而不是整段相等 ⇒ 业务内容与噪音词落在同一段时，**整段**被丢掉。
        实测真实语料里这类占 **6/3001 = 0.2%**（读数与它的方向登记在
        `project_tag_rules.NOISE_PHRASES` 上方）。
        ⇒ 要改成「整段相等才丢」时，**这一条必须同一次改**（E-0011：改口径就改它的网）。
        """
        result = clean_order_text(f"合成业务内容A{NOISE_LINE}")
        self.assertTrue(result.is_empty)
        self.assertEqual(result.noise_dropped, (f"合成业务内容A{NOISE_LINE}",))

    def test_a_segment_with_noise_plus_trailing_content_is_still_dropped_whole(self):
        """对照组：噪音词**不是**整段时，丢的仍然是**整段**（后面的业务内容一起没）。

        ⚠️ 与上一条是**一对**：上一条证明「在前面也会被吃掉」，这一条证明「在后面也一样」
        ⇒ 两条一起才说明「丢的是整段」这句话为真，而不是「只删掉噪音词那几个字」。
        """
        result = clean_order_text(f"{NOISE_LINE}后面的业务内容")
        self.assertTrue(result.is_empty)
        self.assertEqual(result.noise_dropped, (f"{NOISE_LINE}后面的业务内容",))


class CleanOrderTextTests(SimpleTestCase):
    def test_normal_line_keeps_its_segments(self):
        result = clean_order_text("深境螺旋12层|合成副本B")
        self.assertIsInstance(result, CleanResult)
        self.assertEqual(result.kept, ("深境螺旋12层", "合成副本B"))
        self.assertEqual(result.cleaned, "深境螺旋12层+合成副本B")
        self.assertFalse(result.is_empty)

    def test_cleaned_is_joined_with_a_plus(self):
        """拼回来的串用 `+` 连接（与词典里标准项目名的形状一致）。

        ⚠️ 写**字面量**而不是 `DEFAULT_CLEANING_RULES.segment_join`：拿常量比常量时，
        把常量改成别的字符这条照样绿（E-0024）。
        """
        self.assertEqual(clean_order_text("甲/乙").cleaned, "甲+乙")

    def test_cleaning_is_idempotent(self):
        """再洗一遍结果不变——这是「拼回去的串还能当输入」的判据。"""
        first = clean_order_text("【甲】乙/丙、留红留卡\n１２３|丁")
        second = clean_order_text(first.cleaned)
        self.assertEqual(second.cleaned, first.cleaned)
        self.assertEqual(second.kept, first.kept)

    # ── ⭐ 「洗成空」的三条路都必须可观测 ────────────────────────────────────

    def test_empty_input_reports_empty_instead_of_none(self):
        result = clean_order_text("")
        self.assertEqual(result.cleaned, "")
        self.assertEqual(result.kept, ())
        self.assertTrue(result.is_empty)

    def test_none_input_reports_empty_instead_of_raising(self):
        result = clean_order_text(None)
        self.assertTrue(result.is_empty)

    def test_pure_template_input_reports_empty_and_names_what_was_dropped(self):
        """⭐ 实测约 12% 的订单走这一条 ⇒ 它**必须**是一个可读的返回值。"""
        result = clean_order_text(f"{NOISE_LINE}\n{NOISE_PARENT}")
        self.assertTrue(result.is_empty)
        self.assertEqual(result.kept, ())
        self.assertEqual(result.noise_dropped, (NOISE_LINE, NOISE_PARENT))

    def test_pure_order_number_input_reports_empty_and_keeps_the_dropped_segment(self):
        """整段就是一个订单号 ⇒ 归一后为空：归 `empty_dropped`，⛔ 不是噪音那一栏。"""
        order_no = "2" * 20
        result = clean_order_text(order_no)
        self.assertTrue(result.is_empty)
        self.assertEqual(result.noise_dropped, ())
        self.assertEqual(result.empty_dropped, (order_no,))

    def test_clean_result_is_always_truthy_so_callers_must_use_is_empty(self):
        """⛔ 调用方不许拿 `if result` 判空：`CleanResult` 没有 `__bool__` ⇒ **永远是真值**。

        这条是**反面**判据：它证明「用真值判空」这条路是坏的，所以
        `is_empty` 这个观测点才是承重的（去掉它，洗空的单会被静默当成"没结果"）。
        """
        result = clean_order_text("")
        self.assertTrue(result.is_empty)
        self.assertTrue(bool(result))
        self.assertEqual(result.kept, ())

    # ── 规则是数据：能整套换掉（租户级覆盖的接口） ──────────────────────────

    def test_rules_can_be_swapped_without_touching_the_engine(self):
        """租户级词表：叠一条自己的，默认那条仍然生效。"""
        tenant_rules = CleaningRules(noise_phrases=NOISE_PHRASES + ("合成租户专用模板",))
        self.assertTrue(clean_order_text("合成租户专用模板", tenant_rules).is_empty)
        self.assertTrue(clean_order_text(NOISE_LINE, tenant_rules).is_empty)
        # 对照组：默认规则**不认**那条租户词（否则上面的断言在「引擎根本没看 rules」时也绿）。
        self.assertFalse(clean_order_text("合成租户专用模板").is_empty)

    def test_default_rules_come_from_the_single_source_in_rules_module(self):
        """⛔ 词表只有一处：默认规则拿的就是 `project_tag_rules.NOISE_PHRASES` 本身。"""
        self.assertIs(DEFAULT_CLEANING_RULES.noise_phrases, NOISE_PHRASES)

    def test_dropping_every_noise_phrase_from_the_rules_stops_dropping(self):
        """**弄红对照**：把词表清空 ⇒ 同一条输入不再被丢。

        没有这一条，「噪音剥离」可能是别的地方（比如归一）顺手做掉的，
        引擎「按 rules 剥」这件事就没有判别力。
        """
        empty_rules = CleaningRules(noise_phrases=())
        self.assertEqual(clean_order_text(NOISE_LINE, empty_rules).kept, (NOISE_LINE,))
