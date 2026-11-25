"""把一份**离线跑出来的项目词典**灌进 `ProjectTagDict`（计划 Task 6）。设计稿：`docs/plans/2026-10-11-price-board-center.md` §2.2。 ## 数据源（⛔ 不进 git）离线归纳的产物（形如 `.tools/recon/merged.json`）：`{订单号: {game, content, name, cat, total_price, hall_price, name2}}`。⚠️ **它含真实业务内容**（游戏名 / 项目名 / 类别），按 `AGENTS.md` §5-C 只在**不进 git** 的 `.tools/recon/` 里放着 ⇒ 本命令**必须显式传路径**， ⛔ 不提供默认路径、也⛔ 不把它拷进仓库。 ## 归并口径（设计稿与会话裁决） - **标准名** = `name2`（有的话）否则 `name`；
- **别名** = 被**归并到同一个标准名**的那些 `name`（逗号分隔）。 ⚠️ **标准名本身不进别名**：实测 1458 个标准名里有 **1271 个**本身就出现在自己的 `name` 集合里（`name2 == name` 的单名项目）⇒ 照字面「所有 name」会把它们全写成一列 **与标准名逐字相同的「别名」**，而设计稿 §1 的交付口径是「1447 个项目 ＋ **565 条别名映射**」 ——「映射」只可能是「名字被并到了**别的**标准名上」那些（本次数据实测 **319** 条，与 565 的差异是两次归纳的快照漂移）； - **类别** = `cat`。 ## 幂等（设计稿任务书的硬要求）同一份源跑第二次：`created=0 / updated=0 / unchanged=N`，**不产生重复行** （判据打在 `(game, name)` 的 `UniqueConstraint` ＋ 上面那句「值没变就不 save」上）。
⚠️ 默认**覆盖** `aliases`（源是权威、改了源要能纠错）；要保住人工在配置页里加的别名，用 `--merge-aliases`（并集，仍然幂等）。 ⚠️ `is_active` **更新时一律不动**——它是人工停用开关，⛔ 重跑不该把它打开。 ## 写在哪个租户 ⛔ **`--schema` 必填**：`ProjectTagDict` 是**每个租户自己订单里长出来的**（设计稿 §9.6 「一租户一套词典」）⇒ 把 A 租户的词典灌进 B 租户等于串业务数据。设计稿与 `AGENTS.md` §4 都不允许，所以这里**不做「默认全租户」**（本仓其它租户级命令如 `purge_pending_screenshots` 是全租户的，那是**清理**、不是**灌业务数据**，两者不能类推）。 ## PII 自检（批次 1 计划 Task 6 的硬要求）「已知抽样里 0 手机号 / 0 身份证」**不能当依据**（`AGENTS.md` §5-E：空结果不等于没有数据） ⇒ 每次导入都现扫一遍： - **要写进库的值**（游戏名 / 标准名 / 别名 / 类别）命中 ⇒ **拦下**（`CommandError`），可用 `--allow-pii` 显式放行； - **只在 `content` 等不落库字段里**命中 ⇒ **只告警**（它们本来就不进库）； - ⚠️ 输出里**打印扫描过的值个数**：扫了 0 个值的「没命中」是没有判别力的假绿（同 `AGENTS.md` §5-E 的「空结果不等于没有数据」）。"""
import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import tenant_context

from apps.business.models import Game, ProjectTagDict
from apps.platform.models import Tenant

#: 形状级 PII 判据（与 `pii-guard.spec.ts` 的内建形状面同口径）。
#: ⚠️ 两边都加了数字边界：项目名里合法地存在 `4.6` / `11-15号` 这类数字，
#: 没有边界会误报成一串手机号。
PII_PATTERNS = (
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
)

#: `ProjectTagDict` 两列的库层上限（模型上的 `max_length`）。⛔ 不要从模型上取——
#: 那样「模型改成 1024」这条守卫会跟着静默放宽（E-0024：钉值，不钉常量）。
NAME_MAX = 128
ALIASES_MAX = 255
#: `Game.name` 的库层上限（`apps/business/models.py` 的 `CharField(..., max_length=64)`）。
#: ⚠️ `--create-games` 时游戏名超长原本会在库层 `DataError` 炸，
#: 虽然 `transaction.atomic` 会整体回滚不留半截数据，但报错不友好 ⇒ 一并预检。
GAME_NAME_MAX = 64


def mask(value: str) -> str:
    """把命中 PII 的值打码后再进日志（前 3 后 2，与 `realname.views.mask_id_card` 同形）。
    ⛔ 原文一个字都不打印：操作员的终端会进日志 / 截图。"""
    if len(value) <= 5:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 5)}{value[-2:]}"


def find_pii(value: str):
    """返回 `(类型, 命中的原文)`；没有则 `None`。"""
    for label, pattern in PII_PATTERNS:
        found = pattern.search(value or "")
        if found:
            return label, found.group(0)
    return None


def split_aliases(text: str):
    """把逗号分隔的别名字符串拆开（空串 ⇒ 空列表）。"""
    return [part for part in (text or "").split(",") if part]


def join_aliases(names) -> str:
    """并集式拼接，**保持首次出现的顺序**（顺序稳定 = 重跑结果逐字相同 = 幂等）。"""
    seen = []
    for name in names:
        if name and name not in seen:
            seen.append(name)
    return ",".join(seen)


class Command(BaseCommand):
    help = "把离线归纳出来的项目词典灌进某个租户的 ProjectTagDict（幂等；路径必须显式给）"

    def add_arguments(self, parser):
        parser.add_argument(
            "source",
            help="离线词典 JSON 的路径（⛔ 不进 git，如 .tools/recon/merged.json）",
        )
        parser.add_argument(
            "--schema", required=True,
            help="写进哪个租户 schema（⛔ 必填：一租户一套词典，不许跨租户灌）",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="只算不写：打出会发生什么，一行不改"
        )
        parser.add_argument(
            "--merge-aliases", action="store_true",
            help="别名与库里已有值取并集（默认：用源里的值覆盖）",
        )
        parser.add_argument(
            "--create-games", action="store_true",
            help="源里有、租户库里没有的游戏：顺手建出来（默认跳过并告警）",
        )
        parser.add_argument(
            "--allow-pii", action="store_true",
            help="即使要落库的值命中 PII 形状也继续（⛔ 只在人工确认过之后用）",
        )

    # ── 入口 ────────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        source = Path(options["source"])
        records = self._load(source)
        groups, skipped_rows, bad_source = self._group(records)

        for line in bad_source:
            self.stderr.write(f"跳过一条源记录：{line}")
        self.stdout.write(
            f"源 {source}：{len(records)} 条订单 → {len(groups)} 个"
            f"（游戏 × 标准名）；源记录不完整而跳过的：{skipped_rows}"
        )
        if not groups:
            # ⛔ 不静默成功：一条都没归出组却报「导入完成」是最坏的假绿（E-0162）。
            raise CommandError(f"源里没有可导入的 (游戏, 标准名) 组合：{source}")

        self._check_lengths(groups)
        self._check_pii(groups, records, allow=options["allow_pii"])

        tenant = Tenant.objects.filter(schema_name=options["schema"]).first()
        if tenant is None:
            raise CommandError(f"没有这个租户 schema：{options['schema']}")

        with tenant_context(tenant):
            self._write(tenant.schema_name, groups, options)

    # ── 读源 + 归并 ─────────────────────────────────────────────────────────

    def _load(self, source: Path) -> dict:
        if not source.is_file():
            raise CommandError(f"找不到数据源：{source}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CommandError(
                f"读不了 / 解析不了数据源 {source}：{type(exc).__name__}: {exc}"
            )
        if not isinstance(raw, dict) or not raw:
            raise CommandError(f"数据源不是「订单号 → 记录」的非空对象：{source}")
        return raw

    def _group(self, records: dict):
        """`{(game, standard): {"aliases": [...], "category": str}}` ＋ 不完整的记录。
        ⚠️ **别名不含标准名**，理由见模块 docstring。"""
        groups = {}
        skipped = 0
        bad = []
        for order_no, row in records.items():
            if not isinstance(row, dict):
                skipped += 1
                bad.append(f"{order_no}: 不是对象（{type(row).__name__}）")
                continue
            game_name = (row.get("game") or "").strip()
            name = (row.get("name") or "").strip()
            standard = (row.get("name2") or "").strip() or name
            if not game_name or not standard:
                skipped += 1
                bad.append(f"{order_no}: 缺 game 或 name/name2")
                continue
            slot = groups.setdefault((game_name, standard), {"aliases": [], "category": ""})
            if name and name != standard and name not in slot["aliases"]:
                slot["aliases"].append(name)
            category = (row.get("cat") or "").strip()
            if category:
                # 同一标准名在不同行上类别不一致时：**首次出现的那条赢**（顺序稳定 ⇒ 幂等）。
                slot["category"] = slot["category"] or category
        return groups, skipped, bad

    # ── 两道路闸 ────────────────────────────────────────────────────────────

    def _check_lengths(self, groups) -> None:
        """列宽超限必须在**导入前**响亮地拦下，⛔ 不靠库层 `DataError` 半途炸。"""
        too_long = []
        for (game_name, standard), slot in groups.items():
            aliases = join_aliases(slot["aliases"])
            if len(game_name) > GAME_NAME_MAX:
                too_long.append(
                    f"游戏名 {len(game_name)} 字（上限 {GAME_NAME_MAX}）：{mask(game_name)}"
                )
            if len(standard) > NAME_MAX:
                too_long.append(f"标准名 {len(standard)} 字（上限 {NAME_MAX}）：{mask(standard)}")
            if len(aliases) > ALIASES_MAX:
                too_long.append(
                    f"{game_name} / {mask(standard)} 的别名串 {len(aliases)} 字"
                    f"（上限 {ALIASES_MAX}）"
                )
        if too_long:
            for line in too_long[:20]:
                self.stderr.write(line)
            raise CommandError(
                f"{len(too_long)} 处超列宽，未写任何一行；请先回源侧归并或放宽列宽"
            )

    def _check_pii(self, groups, records, allow: bool) -> None:
        """扫一遍 PII：**要落库的值**命中就拦，只在 `content` 里命中就告警。
        ⚠️ **打印扫描个数**：扫了 0 个值的「没命中」不构成保证（`AGENTS.md` §5-E）。"""
        blocking = []
        scanned = 0
        for (game_name, standard), slot in groups.items():
            for value in (game_name, standard, slot["category"], *slot["aliases"]):
                if not value:
                    continue
                scanned += 1
                hit = find_pii(value)
                if hit:
                    blocking.append((hit[0], mask(value), f"{mask(game_name)}/{mask(standard)}"))

        self.stdout.write(f"PII 自检：扫描 {scanned} 个将落库的值，命中 {len(blocking)} 处")
        if scanned == 0:
            self.stderr.write("⚠️ 一个值都没扫到——这次「没命中」不构成任何保证")

        for label, masked, where in blocking[:20]:
            self.stderr.write(f"将落库的值命中 {label}：{masked}（来自 {where}）")
        if blocking and not allow:
            raise CommandError(
                f"{len(blocking)} 个将落库的值命中 PII，已拦下（确认无风险后用 --allow-pii 放行）"
            )

        # ⛔ 不落库的字段（`content` / 价格）只告警：它们本来就不进库，
        #    但它们出现 PII 说明源可能被别的下游用错，值得让操作员看见。
        soft = 0
        for order_no, row in records.items():
            if not isinstance(row, dict):
                continue
            for key in ("content",):
                if find_pii(str(row.get(key) or "")):
                    soft += 1
                    break
        if soft:
            self.stderr.write(
                f"⚠️ 另有 {soft} 条订单的 content（**不落库**）命中 PII 形状，仅告警"
            )

    # ── 写库 ────────────────────────────────────────────────────────────────

    def _write(self, schema_name: str, groups, options) -> None:
        dry_run = options["dry_run"]
        create_games = options["create_games"]

        game_names = sorted({game_name for game_name, _ in groups})
        known = set(Game.objects.filter(name__in=game_names).values_list("name", flat=True))
        missing = [name for name in game_names if name not in known]

        if missing and create_games:
            if not dry_run:
                for name in missing:
                    Game.objects.get_or_create(name=name)
            known.update(missing)
            self.stdout.write(f"新建游戏 {len(missing)} 个：{'、'.join(missing)}")
        elif missing:
            # ⚠️ 不用 `Game.all_objects`：那是 `Order` 上的列名白名单逃生口，
            # 使用点由 `tests/test_order_read_path_net.py` 钉着（多一处就红）。
            self.stderr.write(
                f"⚠️ 租户 {schema_name} 里没有这 {len(missing)} 个游戏，它们的项目全部跳过："
                f"{'、'.join(missing)}（要顺手建出来加 --create-games）"
            )

        games = {game.name: game for game in Game.objects.filter(name__in=known)}

        created = updated = unchanged = 0
        skipped_games = 0
        with transaction.atomic():
            for (game_name, standard), slot in sorted(groups.items()):
                game = games.get(game_name)
                if game is None:
                    skipped_games += 1
                    continue
                aliases = join_aliases(slot["aliases"])
                category = slot["category"]
                existing = ProjectTagDict.objects.filter(game=game, name=standard).first()
                if existing is None:
                    created += 1
                    if not dry_run:
                        ProjectTagDict.objects.create(
                            game=game, name=standard, aliases=aliases, category=category
                        )
                    continue
                if options["merge_aliases"]:
                    aliases = join_aliases(split_aliases(existing.aliases) + split_aliases(aliases))
                if existing.aliases == aliases and existing.category == category:
                    unchanged += 1
                    continue
                updated += 1
                if not dry_run:
                    # ⚠️ 整对象 save()（不是 update_fields）：`updated_at` 是 auto_now，
                    #    显式列字段反而容易把它落下。`is_active` 用库里的现值，⛔ 不重置。
                    existing.aliases = aliases
                    existing.category = category
                    existing.save()
            if dry_run:
                transaction.set_rollback(True)

        verb = "将" if dry_run else ""
        self.stdout.write(
            f"{'[dry-run] ' if dry_run else ''}租户 {schema_name}："
            f"{verb}新建 {created} 行、{verb}更新 {updated} 行、保持不变 {unchanged} 行"
            f"（游戏不存在而跳过 {skipped_games} 组）"
        )
        if skipped_games and not create_games:
            self.stderr.write(
                "⚠️ 有组的游戏不在库里 ⇒ 这次导入是**部分**的，别当成完整成功"
            )
