"""`manage.py import_project_tag_dict` 的判据（批次 1 计划 Task 6）。

设计稿：`docs/plans/2026-10-11-price-board-center.md` §2.2；命令自己的模块 docstring
写了数据源形状、归并口径与幂等定义。

## 这份网钉什么

1. **归并口径**：标准名取 `name2`（没有才退回 `name`）、别名是"被并到**别的**标准名上"
   的那些 `name`（⛔ **不含标准名自己**）、类别取 `cat`；
2. ⭐ **幂等**：同一份源跑第二次 ⇒ 行数不变、`created=0 / updated=0`、
   **`updated_at` 也不动**（"值没变就不 save"）；
3. ⭐ **`--dry-run` 一行都不写**（判据打在数据库行数上，⛔ 不打输出文本）；
4. **游戏不在库里 ⇒ 跳过并告警**（不静默成功），`--create-games` 才顺手建；
5. ⭐ **PII 自检**：要落库的值命中就**拦下**且**一行都不写**；`--allow-pii` 才放行；
   并且**打印扫描过的值个数**（扫 0 个值的"没命中"没有判别力）；
6. **列宽超限在导入前拦下**（⛔ 不靠库层 `DataError` 半途炸，那会留下半截数据）；
7. `--schema` 必填、且写的是**指定那个租户**。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C）：源文件里的游戏 / 项目名 / 类别全是编的，
与任何真实租户数据无关；真实语料只在**不进 git** 的 `.tools/recon/`。
⚠️ 断言打在**数据库行**与 `ProjectTagDict` 的列值上（E-0009），字段名写**字面量**（E-0024）。
"""
import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django_tenants.test.cases import TenantTestCase

from apps.business.models import Game, ProjectTagDict

SCHEMA = "test"  # django_tenants 的 TenantTestCase 固定用这个 schema（与既有用例同口径）

#: 合成的一支"离线归纳产物"：三行订单、两个游戏、一个被并过的标准名。
SYNTHETIC = {
    "900000000000000001": {
        "game": "合成游戏甲", "content": "合成内容一",
        "name": "合成项目A", "cat": "任务代肝",
        "total_price": "90.00", "hall_price": "80.00", "name2": "合成项目A",
    },
    "900000000000000002": {
        "game": "合成游戏甲", "content": "合成内容二",
        "name": "合成项目A的另一种写法", "cat": "任务代肝",
        "total_price": "100.00", "hall_price": "85.00", "name2": "合成项目A",
    },
    "900000000000000003": {
        "game": "合成游戏乙", "content": "合成内容三",
        # ⚠️ 这一行**没有 `name2`**：标准名必须退回 `name`。
        "name": "合成项目B", "cat": "地图收集",
        "total_price": "50.00", "hall_price": "40.00",
    },
}


def run_command(*args, **options):
    """跑一次命令，返回 `(stdout, stderr)` 原文。

    ⚠️ 命令输出是判据的载体（「扫了几个值」这一条只能打在输出上）
    ⇒ 必须把 stdout / stderr 都收回来，⛔ 不让它们漏进测试日志。
    """
    out, err = StringIO(), StringIO()
    call_command("import_project_tag_dict", *args, stdout=out, stderr=err, **options)
    return out.getvalue(), err.getvalue()


class ImportCommandTestCase(TenantTestCase):
    """公共夹具：一个临时源文件 + 一个合成游戏。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tt-ptag-import-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.game = Game.objects.create(name="合成游戏甲")

    def write_source(self, payload, name="merged.json") -> str:
        path = self.tmp / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def create_game_b(self):
        """把合成源里第二支游戏也建出来。

        ⚠️ 只有需要「两支游戏都在库里」的用例才调它：另有两支用例**专门**要它缺失
        （游戏不在库里 ⇒ 跳过并告警 / `--create-games` 才建），基础夹具里建了就把那两条弄瞎了。
        """
        return Game.objects.create(name="合成游戏乙")

    def rows(self):
        return {
            (row.game.name, row.name): (row.aliases, row.category, row.is_active)
            for row in ProjectTagDict.objects.select_related("game")
        }


class MergeSemanticsTests(ImportCommandTestCase):
    def test_standard_name_alias_and_category(self):
        self.create_game_b()
        out, _ = run_command(self.write_source(SYNTHETIC), schema=SCHEMA)
        rows = self.rows()
        self.assertEqual(
            set(rows), {("合成游戏甲", "合成项目A"), ("合成游戏乙", "合成项目B")}
        )
        aliases, category, is_active = rows[("合成游戏甲", "合成项目A")]
        # ⭐ 别名只收**被并到别的标准名上**的那一个；标准名自己**不在**里面。
        self.assertEqual(aliases, "合成项目A的另一种写法")
        self.assertEqual(category, "任务代肝")
        self.assertIs(is_active, True)
        # `name2` 缺失那行退回 `name`。
        self.assertEqual(rows[("合成游戏乙", "合成项目B")][1], "地图收集")
        self.assertIn("PII 自检", out)

    def test_standard_name_is_never_its_own_alias(self):
        """⭐ 判据用**构造出来的**反例：一行 `name == name2` ⇒ 别名必须是空串。

        ⚠️ 这条对着的是"照字面把所有 `name` 都收进别名"那种实现——
        实测真实语料里 1458 个标准名有 **1271 个**会因此写出一列与标准名逐字相同的"别名"。
        """
        run_command(
            self.write_source({
                "900000000000000009": {
                    "game": "合成游戏甲", "name": "合成项目C", "cat": "其他",
                    "name2": "合成项目C", "content": "x",
                }
            }),
            schema=SCHEMA,
        )
        self.assertEqual(self.rows()[("合成游戏甲", "合成项目C")][0], "")

    def test_alias_order_is_first_seen_and_deduped(self):
        """别名的顺序 = 源里**首次出现**的顺序，且去重 ⇒ 重跑结果逐字相同（幂等的前提）。"""
        payload = {
            "900000000000000011": {"game": "合成游戏甲", "name": "写法乙", "cat": "其他",
                                   "name2": "合成项目D", "content": "x"},
            "900000000000000012": {"game": "合成游戏甲", "name": "写法甲", "cat": "其他",
                                   "name2": "合成项目D", "content": "x"},
            "900000000000000013": {"game": "合成游戏甲", "name": "写法乙", "cat": "其他",
                                   "name2": "合成项目D", "content": "x"},
        }
        run_command(self.write_source(payload), schema=SCHEMA)
        self.assertEqual(self.rows()[("合成游戏甲", "合成项目D")][0], "写法乙,写法甲")


class IdempotencyTests(ImportCommandTestCase):
    def test_second_run_creates_and_updates_nothing(self):
        self.create_game_b()
        path = self.write_source(SYNTHETIC)

        out1, _ = run_command(path, schema=SCHEMA)
        self.assertIn("新建 2 行", out1)
        first = {
            (row.game.name, row.name): (row.aliases, row.category, row.updated_at)
            for row in ProjectTagDict.objects.select_related("game")
        }

        out2, _ = run_command(path, schema=SCHEMA)
        # ⭐ `updated=0` 才是幂等：只断言"行数没变"的话，每次重写一遍也照样绿。
        self.assertIn("新建 0 行、更新 0 行、保持不变 2 行", out2)
        second = {
            (row.game.name, row.name): (row.aliases, row.category, row.updated_at)
            for row in ProjectTagDict.objects.select_related("game")
        }
        self.assertEqual(first, second)
        self.assertEqual(ProjectTagDict.objects.count(), 2)

    def test_is_active_is_not_reset_by_a_rerun(self):
        """⛔ 人工停用的行，重跑**不许**把它打开（`is_active` 是人工开关，不是导入字段）。"""
        path = self.write_source(SYNTHETIC)
        run_command(path, schema=SCHEMA)
        row = ProjectTagDict.objects.get(game=self.game, name="合成项目A")
        row.is_active = False
        row.save()

        run_command(path, schema=SCHEMA)
        self.assertIs(ProjectTagDict.objects.get(pk=row.pk).is_active, False)

    def test_default_replaces_aliases_and_merge_aliases_unions(self):
        path = self.write_source(SYNTHETIC)
        run_command(path, schema=SCHEMA)
        row = ProjectTagDict.objects.get(game=self.game, name="合成项目A")
        row.aliases = "人工加的别名"
        row.save()

        # 默认 = 源是权威：覆盖掉人工加的那条。
        run_command(path, schema=SCHEMA)
        self.assertEqual(
            ProjectTagDict.objects.get(pk=row.pk).aliases, "合成项目A的另一种写法"
        )

        row = ProjectTagDict.objects.get(pk=row.pk)
        row.aliases = "人工加的别名"
        row.save()
        # `--merge-aliases` = 并集：人工那条**保住**，源那条也在。
        run_command(path, schema=SCHEMA, merge_aliases=True)
        self.assertEqual(
            ProjectTagDict.objects.get(pk=row.pk).aliases,
            "人工加的别名,合成项目A的另一种写法",
        )
        # 并集也是幂等的：再跑一次不变。
        run_command(path, schema=SCHEMA, merge_aliases=True)
        self.assertEqual(
            ProjectTagDict.objects.get(pk=row.pk).aliases,
            "人工加的别名,合成项目A的另一种写法",
        )


class DryRunTests(ImportCommandTestCase):
    def test_dry_run_writes_nothing(self):
        """⭐ 判据打在**数据库行数**上，⛔ 不打输出文本（输出写着 dry-run 但真写了也很常见）。"""
        self.create_game_b()
        out, _ = run_command(self.write_source(SYNTHETIC), schema=SCHEMA, dry_run=True)
        self.assertEqual(ProjectTagDict.objects.count(), 0)
        self.assertIn("[dry-run]", out)
        self.assertIn("新建 2 行", out)

    def test_dry_run_on_an_existing_row_changes_nothing(self):
        path = self.write_source(SYNTHETIC)
        run_command(path, schema=SCHEMA)
        before = {
            (row.pk, row.aliases, row.category, row.updated_at)
            for row in ProjectTagDict.objects.all()
        }
        run_command(path, schema=SCHEMA, dry_run=True)
        after = {
            (row.pk, row.aliases, row.category, row.updated_at)
            for row in ProjectTagDict.objects.all()
        }
        self.assertEqual(before, after)


class GameResolutionTests(ImportCommandTestCase):
    def test_unknown_game_is_skipped_loudly_and_not_created(self):
        out, err = run_command(self.write_source(SYNTHETIC), schema=SCHEMA)
        self.assertEqual(set(self.rows()), {("合成游戏甲", "合成项目A")})
        self.assertEqual(Game.objects.filter(name="合成游戏乙").count(), 0)
        # ⚠️ 不许静默：缺失的游戏必须出现在 stderr 里，且要有"这次不完整"的话。
        self.assertIn("合成游戏乙", err)
        self.assertIn("部分", err)
        self.assertIn("跳过 1 组", out)

    def test_create_games_flag_creates_the_missing_game(self):
        run_command(self.write_source(SYNTHETIC), schema=SCHEMA, create_games=True)
        self.assertIn("合成游戏乙", {row.game.name for row in ProjectTagDict.objects.all()})
        self.assertIs(Game.objects.get(name="合成游戏乙").is_active, True)


class PiiGateTests(ImportCommandTestCase):
    """⭐ PII 自检：**要落库的值**命中就拦下，且一行都不写。"""

    def _payload(self, name):
        return {
            "900000000000000021": {
                "game": "合成游戏甲", "name": name, "cat": "其他",
                "name2": name, "content": "合成的订单正文",
            }
        }

    def test_phone_number_in_a_written_value_blocks_the_whole_run(self):
        path = self.write_source(self._payload("联系13800138000"))
        with self.assertRaises(CommandError) as caught:
            run_command(path, schema=SCHEMA)
        # ⛔ 一个字都不许写进去。
        self.assertEqual(ProjectTagDict.objects.count(), 0)
        # ⛔ 报错文本里不许回显原文（终端会进日志 / 截图）。
        self.assertNotIn("13800138000", str(caught.exception))

    def test_a_hit_in_content_only_is_a_warning_not_a_block(self):
        """`content` **不落库** ⇒ 它命中只告警；对照上面那条，说明拦的是"要落库的值"。"""
        payload = {
            "900000000000000022": {
                "game": "合成游戏甲", "name": "合成项目E", "name2": "合成项目E",
                "cat": "其他", "content": "电话 13800138000",
            }
        }
        _, err = run_command(self.write_source(payload), schema=SCHEMA)
        self.assertEqual(ProjectTagDict.objects.count(), 1)
        self.assertIn("content", err)

    def test_allow_pii_lets_it_through_when_a_human_says_so(self):
        path = self.write_source(self._payload("联系13800138000"))
        run_command(path, schema=SCHEMA, allow_pii=True)
        self.assertEqual(
            ProjectTagDict.objects.get().name, "联系13800138000"
        )

    def test_the_scan_reports_how_many_values_it_looked_at(self):
        """⭐ 扫 0 个值的"没命中"没有判别力（`AGENTS.md` §5-E）⇒ 个数必须打印出来。

        合成源里将落库的值 = 2 个游戏名 + 2 个标准名 ＋ 1 个别名 ＋ 2 个类别。
        ⚠️ 这里**只断言"个数 > 0 且是个数字"**：把具体数字写死会让每次改夹具都红，
        而这条要守的是"有没有打印"，不是"打印了几"。
        """
        out, _ = run_command(self.write_source(SYNTHETIC), schema=SCHEMA)
        line = [row for row in out.splitlines() if row.startswith("PII 自检：")][0]
        self.assertRegex(line, r"扫描 [1-9]\d* 个将落库的值，命中 0 处")


class GuardrailTests(ImportCommandTestCase):
    def test_schema_is_required(self):
        with self.assertRaises(CommandError):
            run_command(self.write_source(SYNTHETIC))

    def test_unknown_schema_is_refused(self):
        with self.assertRaises(CommandError):
            run_command(self.write_source(SYNTHETIC), schema="并不存在的schema")

    def test_missing_source_file_is_refused(self):
        with self.assertRaises(CommandError):
            run_command(str(self.tmp / "并不存在.json"), schema=SCHEMA)

    def test_source_that_is_not_an_object_is_refused(self):
        path = self.tmp / "list.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(CommandError):
            run_command(str(path), schema=SCHEMA)

    def test_source_with_no_usable_row_is_refused_not_reported_as_success(self):
        """⭐ 一条都归不出组却报"导入完成"是最坏的假绿（E-0162）⇒ 必须响亮地失败。"""
        path = self.write_source({"900000000000000031": {"game": "", "name": ""}})
        with self.assertRaises(CommandError):
            run_command(path, schema=SCHEMA)
        self.assertEqual(ProjectTagDict.objects.count(), 0)

    def test_overlong_alias_string_is_refused_before_any_write(self):
        """别名串超过列宽（255）⇒ **导入前**拦下，⛔ 不留半截数据。"""
        long_name = "合" * 60
        payload = {
            f"9000000000000001{index:02d}": {
                "game": "合成游戏甲", "name": f"{long_name}{index}", "cat": "其他",
                "name2": "合成项目F", "content": "x",
            }
            for index in range(6)
        }
        with self.assertRaises(CommandError):
            run_command(self.write_source(payload), schema=SCHEMA)
        self.assertEqual(ProjectTagDict.objects.count(), 0)

    def test_overlong_game_name_is_refused_before_any_write(self):
        """⚠️ 游戏名超过列宽（`Game.name` = 64）⇒ 也必须在**导入前**拦下。

        独立评审 Nit #3：原本只预检标准名（128）与别名串（255），
        `--create-games` 建游戏时超长会在库层 `DataError` 炸 —— 虽然
        `transaction.atomic` 会整体回滚不留半截数据，但报错不友好且不像业务错误。
        """
        payload = {
            "90000000000000000051": {
                "game": "游" * 65,
                "name": "合成项目G",
                "cat": "其他",
                "name2": "合成项目G",
                "content": "x",
            },
        }
        with self.assertRaises(CommandError):
            run_command(self.write_source(payload), schema=SCHEMA)
        self.assertEqual(ProjectTagDict.objects.count(), 0)

    def test_records_missing_name_or_game_are_counted_and_reported(self):
        self.create_game_b()
        payload = dict(SYNTHETIC)
        payload["900000000000000041"] = {"game": "合成游戏甲", "content": "没有 name"}
        out, err = run_command(self.write_source(payload), schema=SCHEMA)
        self.assertIn("跳过一条源记录", err)
        self.assertIn("源记录不完整而跳过的：1", out)
        # 其余三行照常导入（⛔ 不是"一条坏就全不导"）。
        self.assertEqual(ProjectTagDict.objects.count(), 2)
