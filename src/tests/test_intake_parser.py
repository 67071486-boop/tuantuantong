from decimal import Decimal

from django.test import SimpleTestCase

from apps.business.intake_parser import ParseResult, parse_order_text
from apps.business.intake_rules import (
    BUILTIN_RULES,
    RECOGNITION_ITEMS,
    RULES,
    Rule,
    build_rules,
    to_decimal,
)


class IntakeParserTests(SimpleTestCase):
    """纯逻辑，不碰数据库。"""

    SAMPLE = """原神 国服 深境螺旋满星
账号：player001
角色名：旅行者
价格：199.50
电话：13800138000
"""

    def test_parses_multiple_fields_from_one_blob(self):
        result = parse_order_text(self.SAMPLE)
        self.assertIsInstance(result, ParseResult)
        self.assertEqual(result.fields["game_account"], "player001")
        self.assertEqual(result.fields["game_role_name"], "旅行者")
        self.assertEqual(result.fields["customer_phone"], "13800138000")

    def test_price_is_converted_to_decimal_not_float(self):
        result = parse_order_text(self.SAMPLE)
        self.assertEqual(result.fields["total_price"], Decimal("199.50"))
        self.assertIsInstance(result.fields["total_price"], Decimal)

    def test_price_fields_parse_negative_amounts(self):
        """**金额可以为负**（退款 / 冲抵 / 调整，业务方 2026-09-20 确认，见 D-0015）。

        粘贴解析是这个模块的头号入口，负号被丢掉就等于「录入时静默丢失一个合法值」：
        原模式 `(\\d+(?:\\.\\d{1,2})?)` 在 `价格：-199.50` 上**整条不命中**，
        字段落进 `unmatched`（不是翻成正数，也不是解析失败报错），客服只会看到「没认出来」。
        正数对照组不能省：没有它，一个把符号搞反的解析器也能让负数断言全绿。
        """
        result = parse_order_text("价格：-199.50\n实收：-10.00")
        self.assertEqual(result.fields["total_price"], Decimal("-199.50"))
        self.assertEqual(result.fields["received_price"], Decimal("-10.00"))
        self.assertNotIn("total_price", result.unmatched)
        self.assertNotIn("received_price", result.unmatched)

        positive = parse_order_text("价格：199.50")
        self.assertEqual(positive.fields["total_price"], Decimal("199.50"))
        self.assertNotIn("total_price", positive.unmatched)

    def test_unmatched_fields_are_reported_not_silently_dropped(self):
        result = parse_order_text(self.SAMPLE)
        self.assertIn("game_account", result.matched)
        self.assertIn("game_sub_account", result.unmatched)
        self.assertNotIn("game_sub_account", result.fields)

    def test_garbage_text_yields_empty_fields_and_full_unmatched(self):
        result = parse_order_text("今天天气不错，随便写点什么。")
        self.assertEqual(result.fields, {})
        self.assertEqual(set(result.unmatched), {r.field for r in RULES})

    def test_engine_is_generic_rules_are_data(self):
        """D5 的核心断言：换一份规则数据，同一个引擎照样工作。"""
        custom = (Rule(field="whatever", patterns=(r"X[：:]\s*(\S+)",)),)
        result = parse_order_text("X：hello", rules=custom)
        self.assertEqual(result.fields, {"whatever": "hello"})
        self.assertEqual(result.unmatched, ())

    def test_sub_account_is_not_parsed_as_the_main_account(self):
        """`账号` 规则原先没有左边界，会在 `子账号：sub` **里面**匹配，把 `sub` 当成主账号。

        这会把错误数据写进代练员用来登录的那个字段。
        """
        result = parse_order_text("子账号：sub\n账号：main")
        self.assertEqual(result.fields["game_account"], "main")
        self.assertEqual(result.fields["game_sub_account"], "sub")

    def test_game_name_does_not_swallow_the_account_label(self):
        """`游戏` 规则原先用 `[：:\\s]*`（零长也可），会把 `游戏账号：acc002` 解析成
        `game_name == "账号：acc002"`。"""
        result = parse_order_text("游戏账号：acc002")
        self.assertEqual(result.fields["game_account"], "acc002")
        self.assertNotIn("game_name", result.fields)

    def test_to_decimal_rejects_non_finite_values(self):
        """`to_decimal` 是给**自定义规则**用的公开 transform，接上「识别设置」界面后会出现。

        `Decimal("Infinity")` / `Decimal("NaN")` 都是合法的 Decimal，但都不是金额——
        放进 `total_price` 会在写库时炸，而且不是业务错误码那种炸法。
        """
        self.assertIsNone(to_decimal("Infinity"))
        self.assertIsNone(to_decimal("-Infinity"))
        self.assertIsNone(to_decimal("NaN"))

    def test_to_decimal_returns_none_on_garbage(self):
        self.assertIsNone(to_decimal("不是数字"))
        self.assertEqual(to_decimal("12.30"), Decimal("12.30"))

    def test_later_patterns_are_tried_when_earlier_ones_miss(self):
        rule = Rule(field="f", patterns=(r"AAA[：:]\s*(\S+)", r"BBB[：:]\s*(\S+)"))
        self.assertEqual(
            parse_order_text("BBB：ok", rules=(rule,)).fields, {"f": "ok"}
        )

    def test_first_pattern_wins_when_both_match(self):
        """顺序敏感：靠前的规则更具体，必须优先——这条防止以后乱加规则。"""
        rule = Rule(field="f", patterns=(r"具体[：:]\s*(\S+)", r"[^\s]*[：:]\s*(\S+)"))
        self.assertEqual(
            parse_order_text("具体：hit", rules=(rule,)).fields, {"f": "hit"}
        )


class RecognitionItemRegistryTests(SimpleTestCase):
    """识别项注册表 = 对标 18 项里**有订单字段的** 14 项（spec §6.1）。

    关键词**逐字照抄对标**（spec §3-D3）：任何一项多一个词都会让下面的字面值对拍变红。
    """

    #: (item_code, benchmark_id, 名称, 字段, 关键词原文)
    EXPECTED = (
        ("game_server_area", 16, "游戏区服", "server_name", "服务器,区服,平台,大区,b服,官服,卓还是,果还"),
        ("server", 18, "服务器", "server_name", "游戏服,服务器"),
        ("game_account", 2, "游戏账号", "game_account", "帐号,战网,账号,通行证"),
        ("sub_account", 4, "子副账号", "game_sub_account", "子账号,分账号,子战网,子帐号,ID"),
        ("role_name", 5, "角色名", "game_role_name", "角色,名字,名称,昵称"),
        ("current_info_1", 6, "当前信息1", "current_info_1", "装等,装备,等级,当前,排位,铭文,符文"),
        ("current_info_2", 15, "当前信息2", "current_info_2", "盟约,巅峰,天赋,英雄"),
        ("project_text", 13, "代练内容", "project_text", "项目,内容,业务,类型,要求,需求,代,任务,标题"),
        ("price", 7, "接单价格", "total_price", "价格,接单价,金额"),
        ("order_source", 14, "订单来源", "source", "来源,店铺"),
        ("source_order_no", 9, "来源单号", "source_order_no", "订单编号,单号,订单号,定单编号"),
        ("phone", 10, "联系电话", "customer_phone", "手机,电话,联系方式"),
        ("qq", 11, "QQ", "customer_qq", "qq,微信"),
        ("ww", 12, "旺旺", "customer_ww", "旺旺,淘宝旺旺"),
    )

    def test_registry_is_exactly_the_fourteen_items_in_benchmark_order(self):
        self.assertEqual(
            tuple(
                (i.code, i.benchmark_id, i.name, i.field, ",".join(i.keywords))
                for i in RECOGNITION_ITEMS
            ),
            self.EXPECTED,
        )

    def test_builtin_rules_are_the_two_rules_without_a_benchmark_row(self):
        """`game_name` / `received_price` 不进页面（spec §3-D4），但必须仍在默认规则里。"""
        self.assertEqual(
            tuple(r.field for r in BUILTIN_RULES), ("game_name", "received_price")
        )

    def test_override_replaces_the_whole_keyword_list(self):
        """改词 = **整串替换**（不是追加）；清成空串 = 这一项不再产出规则。"""
        rules = build_rules({"price": "总价"})
        fields = {rule.field: rule.patterns[0] for rule in rules}
        self.assertIn("总价", fields["total_price"])
        self.assertNotIn("接单价", fields["total_price"])
        cleared = build_rules({"price": ""})
        self.assertNotIn("total_price", {rule.field for rule in cleared})

    def test_two_items_sharing_a_field_merge_into_one_rule(self):
        """`游戏区服` + `服务器` 都映射 `server_name` ⇒ 只出一条规则（否则 `unmatched` 会误报）。"""
        server_rules = [r for r in build_rules() if r.field == "server_name"]
        self.assertEqual(len(server_rules), 1)
        self.assertIn("游戏服", server_rules[0].patterns[0])
        self.assertIn("卓还是", server_rules[0].patterns[0])


class KeywordVocabularyChangeTests(SimpleTestCase):
    """spec §3-D3 的**有意**变更：旧静态词表里、对标原文没有的词从此不认。

    这条用例把差异**逐条列出来**（未登记的差异为 0 才是结论）；它不证明"枚举完备"，
    只证明"没有未记录的差异"（E-0011 的证明力边界）。
    """

    #: 旧词表（P2 的静态 RULES）里、**对标原文没有**的标签词 ⇒ 改后必须不再识别。
    #: `(语料, 旧规则在它上面认出的字段)`——字段那一列让判据打在**具体字段**上，
    #: 而不是「整条语料一个字段都没认出来」（后者会被别的规则误命中而恒真）。
    DROPPED = (
        ("订单总价：100", "total_price"),
        ("总价：100", "total_price"),
        ("VX：abc12345", "customer_qq"),
        ("V信：abc12345", "customer_qq"),
    )

    def test_labels_without_a_benchmark_keyword_are_no_longer_recognised(self):
        for text, field in self.DROPPED:
            with self.subTest(text=text):
                result = parse_order_text(text)
                self.assertNotIn(field, result.fields, text)
                # `unmatched` 也要报出来（规则还在、只是没命中）——调用方要能提示客服手工填。
                self.assertIn(field, result.unmatched, text)

    def test_benchmark_keywords_do_recognise_the_same_values(self):
        """阳性对照：把标签换成对标原文里的词，同一个值必须认出来（否则上面那条是恒真）。"""
        self.assertEqual(parse_order_text("价格：100").fields["total_price"], Decimal("100"))
        self.assertEqual(parse_order_text("qq：abc12345").fields["customer_qq"], "abc12345")
        self.assertEqual(parse_order_text("微信：abc12345").fields["customer_qq"], "abc12345")

    def test_new_fields_are_now_recognised(self):
        """本次新打通的 5 个字段（spec §8）。

        ⚠️ 语料里的标签必须是**对标关键词本身**（`天赋`）而不是识别项名称（`当前信息2`）——
        后者的词表里没有「当前信息2」这个词。`当前信息1` 的关键词含「当前」，会把
        `当前信息2：…` 这类行吃掉（对标词表自己的粗糙，属既定行为）。
        """
        result = parse_order_text(
            "当前信息1：500\n天赋：巅峰1\n旺旺：买家甲\n单号：TB-8899\n来源：淘宝店"
        )
        self.assertEqual(result.fields["current_info_1"], "500")
        self.assertEqual(result.fields["current_info_2"], "巅峰1")
        self.assertEqual(result.fields["customer_ww"], "买家甲")
        self.assertEqual(result.fields["source_order_no"], "TB-8899")
        self.assertEqual(result.fields["source"], "淘宝店")


#: 差异的「旧侧没有 / 新侧没有」用这个哨兵表示（比 `None` 好读，且 `None` 是转换器的
#: 合法失败值，用它当哨兵会把「转不出值」和「整条没命中」混起来）。
ABSENT = "<无>"

#: 差异归类（登记表第三列之后的归类；判据按类别分别断言，防止「新增能力」被误登记成「接受损失」）。
ACCEPTED = "业务方明确接受的取舍（spec §3-D3 / §13 #2）"
TIGHT = "紧贴写法的必然代价（关键词后无分隔符；spec §13 #5 已预告）"
NEW = "本线新增的识别能力（spec §8；一条都不许丢）"


def _old_strip(raw):
    """合并前 `intake_rules.py` 的 `_strip`（字面量副本：`raw.strip()`）。"""
    return raw.strip()


#: **合并前 `main` 的 `src/apps/business/intake_rules.py` 里那个静态 `RULES` 元组**
#: ——逐字重建（模式串一个字都没动）。旧分支已被本线删除，对拍的另一侧只能在这里留副本。
#: ⚠️ 这份副本是**故意**的：断言「新规则的变化只有登记的那几条」时，两侧必须都是字面值
#:（E-0011：拿导出常量跟自己对拍是自指断言，常量改坏时不会红）。
OLD_STATIC_RULES = (
    Rule("game_account", (r"(?:^|[\s，,、;；])(?:游戏)?账号[：:\s]+([^\s，,、/]+)",), _old_strip),
    Rule("game_sub_account", (r"子账号[：:\s]+([^\s，,、/]+)",), _old_strip),
    Rule("game_role_name", (r"角色名[：:\s]*([^\s，,、/]+)",), _old_strip),
    Rule("customer_phone", (r"(?:联系电话|电话|手机)[：:\s]*(\d{6,20})",), _old_strip),
    Rule("customer_qq", (r"(?:QQ|qq|微信|VX|vx|V信)[：:\s]*([A-Za-z0-9_\-]{4,32})",), _old_strip),
    Rule("total_price", (r"(?:订单总价|总价)[：:\s]*(-?\d+(?:\.\d{1,2})?)",), to_decimal),
    Rule("received_price", (r"(?:接单实收|实收)[：:\s]*(-?\d+(?:\.\d{1,2})?)",), to_decimal),
    Rule("game_name", (r"(?:游戏)[：:\s]+([^\s，,、/]+)",), _old_strip),
    Rule("server_name", (r"(?:区服|大区|服务器)[：:\s]*([^\s，,、/]+)",), _old_strip),
    Rule("project_text", (r"(?:代练/陪玩项目|代练项目|项目)[：:\s]*([^\n]+)",), _old_strip),
)


class OldStaticRulesCorpusParityTests(SimpleTestCase):
    """spec §12.1 / §3-D3 要求的**两侧语料对拍**：旧静态 10 条 vs 新 `build_rules()`。

    断言形态是「差异集合 == 显式登记清单」——**双向都会红**：
    未登记的差异（新引入的行为变化）会红，登记了却没出现（清单过期）也红。

    证明力边界（E-0011）：它证明**在这批语料上没有未记录的差异**，
    **不**证明「新增能力完了」「损失枚举完了」——那要看语料是否按分支分层覆盖。
    """

    #: `(语料标识, 文本)`。标识进登记表，必须唯一。
    CORPUS = (
        # ── ① A 类：短关键词命中长标签（修前取错值，修后两侧一致）──
        ("代练陪玩项目", "代练/陪玩项目：满星满命"),
        # ── ② B 类：旧认、新（修前）不认；修后除「接受损失」外都必须恢复 ──
        ("陪玩项目", "陪玩项目：王者荣耀上分"),
        ("我的角色名", "我的角色名：旅行者"),
        ("联系电话", "联系电话：13800138000"),
        ("总价负数", "总价：-199.50"),
        ("VX", "VX：abc12345"),
        # 上面两条是探针语料；下面两条是同属 spec §13 #2 那一族的另两个标签，
        # 一起登记才是「总价/订单总价、VX/V信」的完整取舍。
        ("订单总价", "订单总价：100"),
        ("V信", "V信：abc12345"),
        # ── ③ C 类：本线新增的识别能力（spec §8），一条都不许丢 ──
        ("代练内容", "代练内容：深渊满星"),
        ("角色", "角色：旅行者"),
        ("价格", "价格：199.50"),
        ("装备等级", "装备等级 500"),
        ("天赋", "天赋：巅峰1"),
        ("来源", "来源：淘宝店"),
        ("店铺", "店铺：旗舰店"),
        ("单号", "单号：TB-8899"),
        ("订单编号", "订单编号：TB-9900"),
        ("旺旺", "旺旺：买家甲"),
        ("淘宝旺旺", "淘宝旺旺：买家乙"),
        ("手机号", "手机号：13900139000"),
        ("通行证", "通行证：pass001"),
        ("战网", "战网：bnet001"),
        ("当前信息1", "当前信息1：500"),
        # ── ④ 紧贴写法（关键词后**没有**分隔符）：`+` 分隔符修法的必然代价，见 §13 #5 ──
        ("服务器紧贴", "服务器X"),
        ("区服紧贴", "区服X"),
        ("角色名紧贴", "角色名X"),
        ("项目紧贴", "项目X"),
        ("QQ紧贴", "QQabc12345"),
    )

    #: **修完之后实测**的差异登记表：`(语料标识, 字段, 旧值, 新值, 归类)`。
    #: 数值一律写字面值（金额用 `Decimal`，与 transform 的输出同型）。
    EXPECTED_DIFFS = (
        # ① 业务方明确接受：严格只抄对标关键词 ⇒ 旧词表里的这几个标签不再认（spec §3-D3）。
        ("总价负数", "total_price", Decimal("-199.50"), ABSENT, ACCEPTED),
        ("订单总价", "total_price", Decimal("100"), ABSENT, ACCEPTED),
        ("VX", "customer_qq", "abc12345", ABSENT, ACCEPTED),
        ("V信", "customer_qq", "abc12345", ABSENT, ACCEPTED),
        # ② 紧贴写法的必然代价：自由文本项的第③备选从「零长分隔符」收紧成「至少一个分隔符」，
        #    否则短关键词会把长标签连同值一起吞进捕获组（那正是 A 类的失败形态）。
        ("服务器紧贴", "server_name", "X", ABSENT, TIGHT),
        ("区服紧贴", "server_name", "X", ABSENT, TIGHT),
        ("角色名紧贴", "game_role_name", "X", ABSENT, TIGHT),
        ("项目紧贴", "project_text", "X", ABSENT, TIGHT),
        ("QQ紧贴", "customer_qq", "abc12345", ABSENT, TIGHT),
        # ③ 新增能力：本线的目的（spec §8），15 条一条不少。
        ("代练内容", "project_text", ABSENT, "深渊满星", NEW),
        ("角色", "game_role_name", ABSENT, "旅行者", NEW),
        ("价格", "total_price", ABSENT, Decimal("199.50"), NEW),
        ("装备等级", "current_info_1", ABSENT, "500", NEW),
        ("天赋", "current_info_2", ABSENT, "巅峰1", NEW),
        ("来源", "source", ABSENT, "淘宝店", NEW),
        ("店铺", "source", ABSENT, "旗舰店", NEW),
        ("单号", "source_order_no", ABSENT, "TB-8899", NEW),
        ("订单编号", "source_order_no", ABSENT, "TB-9900", NEW),
        ("旺旺", "customer_ww", ABSENT, "买家甲", NEW),
        ("淘宝旺旺", "customer_ww", ABSENT, "买家乙", NEW),
        ("手机号", "customer_phone", ABSENT, "13900139000", NEW),
        ("通行证", "game_account", ABSENT, "pass001", NEW),
        ("战网", "game_account", ABSENT, "bnet001", NEW),
        ("当前信息1", "current_info_1", ABSENT, "500", NEW),
    )

    @staticmethod
    def _measure():
        """跑两侧、返回 `(语料标识, 字段, 旧值, 新值)` 的集合。"""
        measured = set()
        for label, text in OldStaticRulesCorpusParityTests.CORPUS:
            old = parse_order_text(text, rules=OLD_STATIC_RULES).fields
            new = parse_order_text(text).fields
            for field in set(old) | set(new):
                old_value = old.get(field, ABSENT)
                new_value = new.get(field, ABSENT)
                if old_value != new_value:
                    measured.add((label, field, old_value, new_value))
        return measured

    def test_corpus_identifiers_are_unique(self):
        """标识不唯一 ⇒ 上面那条判据会把两条语料的差异合并成一个键，**静默**漏掉一条。"""
        labels = [label for label, _text in self.CORPUS]
        self.assertEqual(len(labels), len(set(labels)))

    def test_diff_against_the_old_static_rules_is_exactly_the_registered_list(self):
        measured = self._measure()
        registered = {(label, field, old, new) for label, field, old, new, _kind in self.EXPECTED_DIFFS}
        # 双向：未登记的差异 ⇒ 红；登记了却没出现 ⇒ 也红。
        self.assertEqual(measured - registered, set(), "未登记的差异（新出现的行为变化）")
        self.assertEqual(registered - measured, set(), "登记了却没出现的差异（清单已过期）")
        # 归类本身也是登记的一部分：把一条「新增能力」误标成「接受损失」也要红。
        self.assertEqual(
            len(self.EXPECTED_DIFFS), len(registered), "登记表里有重复条目"
        )

    def test_every_registered_kind_has_at_least_one_row(self):
        """防止某一类被整类删空（删空后上面那条依然绿，等于偷偷取消了这类断言）。"""
        kinds = {kind for *_diff, kind in self.EXPECTED_DIFFS}
        self.assertEqual(kinds, {ACCEPTED, TIGHT, NEW})

    # ── 行为判据（对拍之外，把期望语义**直接**钉字面值）────────────────
    def test_a_short_keyword_no_longer_swallows_a_longer_label(self):
        """A 类回归：`代` 是 `代练/陪玩项目` 的第一个字。

        修前：第③备选的分隔符是零长 ⇒ `代` 在行首命中，`([^\\n]+)` 把
        `练/陪玩项目：满星满命` 整段吃进 `project_text`（**静默写错权威值**）。
        """
        result = parse_order_text("代练/陪玩项目：满星满命")
        self.assertEqual(result.fields["project_text"], "满星满命")
        self.assertNotIn("project_text", result.unmatched)

    def test_keywords_inside_a_longer_label_are_recognised_again(self):
        """B 类里必须恢复的三条：关键词处于更长标签的**中部**，左边界让它们整条不命中。"""
        self.assertEqual(
            parse_order_text("陪玩项目：王者荣耀上分").fields["project_text"],
            "王者荣耀上分",
        )
        self.assertEqual(
            parse_order_text("我的角色名：旅行者").fields["game_role_name"], "旅行者"
        )
        self.assertEqual(
            parse_order_text("联系电话：13800138000").fields["customer_phone"],
            "13800138000",
        )

    def test_the_business_accepted_losses_are_still_losses(self):
        """接受损失不能「顺手修回来」：严格只抄对标关键词（spec §3-D3）下这两族必须仍不认。"""
        for text, field in (
            ("总价：-199.50", "total_price"),
            ("订单总价：100", "total_price"),
            ("VX：abc12345", "customer_qq"),
            ("V信：abc12345", "customer_qq"),
        ):
            with self.subTest(text=text):
                self.assertNotIn(field, parse_order_text(text).fields, text)

    def test_the_new_capabilities_are_all_still_recognised(self):
        """C 类 15 条：这是本线的目的，回归一条就是这一线白做。"""
        expected = {
            ("代练内容：深渊满星", "project_text", "深渊满星"),
            ("角色：旅行者", "game_role_name", "旅行者"),
            ("价格：199.50", "total_price", Decimal("199.50")),
            ("装备等级 500", "current_info_1", "500"),
            ("天赋：巅峰1", "current_info_2", "巅峰1"),
            ("来源：淘宝店", "source", "淘宝店"),
            ("店铺：旗舰店", "source", "旗舰店"),
            ("单号：TB-8899", "source_order_no", "TB-8899"),
            ("订单编号：TB-9900", "source_order_no", "TB-9900"),
            ("旺旺：买家甲", "customer_ww", "买家甲"),
            ("淘宝旺旺：买家乙", "customer_ww", "买家乙"),
            ("手机号：13900139000", "customer_phone", "13900139000"),
            ("通行证：pass001", "game_account", "pass001"),
            ("战网：bnet001", "game_account", "bnet001"),
            ("当前信息1：500", "current_info_1", "500"),
        }
        for text, field, value in sorted(expected):
            with self.subTest(text=text):
                self.assertEqual(parse_order_text(text).fields.get(field), value)

    def test_tight_numeric_writes_are_unaffected_by_the_separator_tightening(self):
        """紧贴的**数字类**写法必须保住（spec 的硬约束）：它们是「值模式自带边界」的那类。"""
        self.assertEqual(
            parse_order_text("电话13800138000").fields["customer_phone"], "13800138000"
        )
        self.assertEqual(
            parse_order_text("价格100").fields["total_price"], Decimal("100")
        )

    def test_sub_account_alone_is_not_parsed_as_the_main_account(self):
        """⛔ 左边界是**承重**的：`账号` ⊂ `子账号`。

        P2 的 `test_sub_account_is_not_parsed_as_the_main_account` 用的是两行语料
        （`子账号：sub\\n账号：main`），那里带左边界的版本先命中 ⇒ 兜底版不会被执行。
        单行 `子账号：sub` 才是「给 `game_account` 也编一条无左边界兜底版」会踩中的形态：
        兜底版会把 `sub` 写进**代练员用来登录的**主账号字段。
        所以 `game_account` **不编兜底版**（`RecognitionItem.loose_fallback=False`）。
        """
        result = parse_order_text("子账号：sub")
        self.assertEqual(result.fields["game_sub_account"], "sub")
        self.assertNotIn("game_account", result.fields)
        self.assertIn("game_account", result.unmatched)

    def test_the_two_line_sample_still_picks_the_main_account(self):
        """P2 原判据的**同一批语料**在这里原样复跑一遍（不是替代，见上一条）。"""
        result = parse_order_text("子账号：sub\n账号：main")
        self.assertEqual(result.fields["game_account"], "main")
        self.assertEqual(result.fields["game_sub_account"], "sub")

    def test_game_name_does_not_swallow_the_account_label(self):
        """`BUILTIN_RULES` 逐字保留，连带这条既有行为也不动。"""
        result = parse_order_text("游戏账号：acc002")
        self.assertEqual(result.fields["game_account"], "acc002")
        self.assertNotIn("game_name", result.fields)
