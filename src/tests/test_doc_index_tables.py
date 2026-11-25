"""`docs/DECISIONS.md` / `docs/ERRORS.md` 索引表的**结构网**（P4 挂账清账线 Task 2）。

## 这个模块为什么存在

`AGENTS.md` §5-F 明写：**git 干净合并不等于编号没撞**——实测 `git merge-tree` 报零冲突，
却在 `DECISIONS.md` 里留下**两行 `D-0023`**。而此前唯一的检查是合并后**人工**跑一句
`grep -o '^| [DE]-0[0-9][0-9][0-9]' docs/DECISIONS.md docs/ERRORS.md | sort | uniq -d`
（`AGENTS.md` §5-F 把这条写成合并流程的一步）。人工步骤会漏 ⇒ 在这里落成自动网。

## 它断言什么（六条，各自都能失败）

1. **解析层的条数下界**：每个文件解析到的索引行数 ≥ `_MIN_ROW_COUNTS`
   ——只回答「有没有整段被跳过」（⛔ 不是「恰好等于」）。
2. **解析层的哨兵**：两表首行 + 两个已知畸形行（`_SENTINEL_IDS`）必须被解析到
   ——证明解析到的确实是索引区。
3. **尾部锚（单调下界）**：解析到的**最大**编号 ≥ `_TAIL_ANCHOR_IDS` 里那个硬编码锚
   ——补下界的盲区：索引只增 ⇒ 只砍掉**尾部**未被引用、也非哨兵的几行时，条数可能仍在下界
   之上而悄悄通过；锚是**单调下界，不需要维护**（新号只会更高）。
4. **无重号**：同一文件内四位 `D-xxxx` / `E-xxxx` 索引行不得重复（与人工 grep 同为四位形态）。
5. **列数一致**：索引区里每一条表格行的**结构列数** == 它所属表头的列数
   （合并事故常表现为某行多 / 少一根竖线）。
6. **引用可解析**：正文（非引用注记行）里引用的 `D-xxxx` / `E-xxxx`，必须能在**对应**
   索引表里找到对应行——「引用了却从未登记」是真实缺口。⚠️ **两个方向都查**：
   `DECISIONS.md` 里引用的 `E-xxxx` 去 `ERRORS.md` 的索引里找，反向同理（只查同前缀
   会漏掉一整类悬空引用）。

## 它**不**声称什么（⛔ 别把这里的绿读成超过这六条的东西）

- ⚠️ **「`docs/**` 只增不改」不是本网能验的属性**：它是 **git 历史属性**（要回答「历史里有没有
  删过」得比对一个旧版本、或看 `git diff --numstat` 的删除列），单元测试读不到。
  ⇒ 本网只证「**索引表结构没被某次合并搞坏**」，⛔ **不许**把它写成"已有自动守卫"。
  它的前身是一条**人工** grep；本网把那条人工步骤自动化，这不等于给「只增不改」建了守卫。
- 第 6 条有**一条被有意排除的面**（都是历史记录，不是现行引用）：
  **行首是 `>` 的取号 / 顺延 / 让号注记块**——它们按设计就在记**预留号与顺延前后的旧号**
  （`E-0143`→`E-0063` 这类换算、以及从未落地的预留号），其中一批本来就**没有也不该有**
  索引行。把它们纳入本网，只会得到一张会一直变长的历史白名单：实测「本前缀未解析引用」
  会从 **2 条**涨到 **27 条**（`DECISIONS.md` 8 + `ERRORS.md` 19；即多出 **25 条**），
  而**新增**的取号注记又天然会带预留号 ⇒ 与其把「历史预留号」写成一长串，
  不如把这类行整体排除、并在下面写清边界。
- 第 5 条对**存量**里四行已知的畸形行做了**显式钉死**（`_KNOWN_MALFORMED_ROWS`），
  ⛔ 不是静默跳过：集合必须**恰好**相等——新增畸形行会红，这四行被改好（或再多一行）也红，
  都要求同步更新钉子。为什么不在本线就地修：`docs/**` 只增不改，改动一行会让删除列非 0。
- 第 1–3 条（解析层）用**下界 + 哨兵 + 尾部锚**，⛔ 不用「恰好等于」：`AGENTS.md` §5-B 要求每条线
  都追加索引行，写死总数会把「新加一条决策」变成别人从没碰过的测试上的红（那正是 §5-F 拒绝过的
  「写死必过期」的税）。下界保证「没有整段被跳过」，哨兵（两表首行 + 两个已知畸形行）
  保证「解析到的是**该**区域」；尾部锚（最大编号只增）补的是「**尾部**几行被砍掉而条数仍在下界
  之上」这个盲区，它是单调下界、⛔ 不需要随新号维护。
- 第 5 条只判**行结构**，⛔ 不判「渲染器会怎么渲染」：本网对代码段里裸 `|` 的口径
  **有意不跟随 GFM**（那类竖线是作者写的内容，不是合并事故；渲染器怎么切是另一回事），
  要判的是「**合并有没有弄坏行结构**」。

## 解析口径（纯文本，零新依赖）

- 索引行 = 以 `| ` 开头、编号列恰好是四位 `D-xxxx` / `E-xxxx` 的那一行；
- 表头 = 编号列是 `编号`（本仓主表）或 `#`（`ERRORS.md` 末尾那张追加表）的行，
  其后（直到下一个表头）的每条 `|` 行都拿它的列数当基准；遇到**认不出的**表头
  （首格不是 `编号` / `#`）就清空基准，⛔ 不拿上一张表的列数去量下一张表的行；
- **结构列数 = 结构竖线数 − 1**，其中 `\\|`（转义竖线）与**单反引号代码段内**的 `|`
  不算结构竖线——它们是正文内容，不是分隔符；
- ⛔ 不 `continue` 跳过任何一条解析得到的索引行：行数另有下界 + 哨兵守着
  （`_MIN_ROW_COUNTS` / `_SENTINEL_IDS`），最大编号另有尾部锚守着（`_TAIL_ANCHOR_IDS`）；
  解析器匹配到 0 行（或整段被跳过）会让前两条自己红，只砍尾部会让锚红。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from unittest import TestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
DECISIONS_MD = REPO_ROOT / "docs" / "DECISIONS.md"
ERRORS_MD = REPO_ROOT / "docs" / "ERRORS.md"

#: 索引行的编号列（四位形态；与 `AGENTS.md` §5-F 的人工 grep 同口径）。
_INDEX_ROW_RE = re.compile(r"^\| ([DE]-\d{4}) \|")
#: 表头行：编号列是 `编号`（本仓主表）或 `#`（`ERRORS.md` 末尾那张追加表）。
_HEADER_RE = re.compile(r"^\|\s*(?:编号|#)\s*\|")
#: 只由 `|` / `-` / `:` / 空格组成的行是表头下的分隔行，没有「列数」可判。
_SEPARATOR_RE = re.compile(r"^\|[\s|:-]*\|$")

#: 每个文件索引行数的**下界**（⛔ 不是「恰好等于」）：下界只回答「有没有整段被跳过」，
#: 而 `AGENTS.md` §5-B 要求每条线都追加索引行 ⇒ 写死总数会把「新加一条决策」变成别人的红。
_MIN_ROW_COUNTS = {"DECISIONS.md": 157, "ERRORS.md": 109}

#: **哨兵**：必须在解析结果里出现的索引行（两表首行 + 两个已知畸形行）。
#: 下界只能证明「条数没少」，哨兵才能证明「解析到的确实是索引区、没把别处的 `|` 行算进来」。
_SENTINEL_IDS = {
    "DECISIONS.md": ("D-0001", "D-0154"),
    "ERRORS.md": ("E-0001", "E-0150"),
}

#: **尾部锚**（单调下界）：每个文件里解析到的**最大**编号必须 ≥ 这里的值。
#: 与 `_MIN_ROW_COUNTS` 互补——下界只回答「条数没少」，而索引是**只增**的 ⇒ 只砍掉**尾部**
#: （最新）的若干条未被引用、也非哨兵的行时，条数仍可能停在下界之上而悄悄通过。
#: ⚠️ 这两个字面量是**单调下界，⛔ 不需要维护**：索引只增 ⇒ 后续新号只会更高，锚自然仍成立
#: （除非有人把索引**改小**，那正是本锚要抓的）。取锚时实测的最大编号：`DECISIONS.md` = `D-0163`、
#: `ERRORS.md` = `E-0156`。
_TAIL_ANCHOR_IDS = {"DECISIONS.md": "D-0163", "ERRORS.md": "E-0156"}

#: 存量畸形行：结构列数与表头不一致，但按「只增不改」⛔ 不能在本线就地修。
#: key = 编号（稳定身份，⛔ 不用行号），value = (实际结构列数, 原因)。
#: ⚠️ 这张表必须**恰好**等于实测的畸形集合：多一行（新增畸形）或这一行被改好
#: （实际列数变了）都会让 `test_index_rows_match_their_header_column_count` 红。
_KNOWN_MALFORMED_ROWS = {
    "D-0154": (6, "整行**多写了一列「现象（实测）」**（六列形态；表头是五列）"),
    "E-0051": (5, "整行按 `DECISIONS` 的五列形态写（多带了一列状态「已修复」）"),
    "E-0148": (3, "缺最后一列「详情」（行尾直接收在正文上）"),
    "E-0150": (3, "缺最后一列「详情」（行尾直接收在正文上）"),
}

#: 正文里引用、但本仓明确登记为**历史预留 / 顺延前旧号**的编号（⛔ 不是现行号，
#: 也没有也不该有索引行）。逐条都能在原句里读到「分支期预留 / 合并时顺延」这类标注
#: （见 `DECISIONS.md` 的 `D-0149` 行、`ERRORS.md` 的 `E-0088` 行）。
#: ⚠️ 这张表是**两向钉子**：实测集合必须恰好等于它——新增一条解析不到的引用会红，
#: 这一条被改成合法引用也会红。⛔ 不许用「原因文本含某个词」之类的正则后门放行。
_KNOWN_HISTORICAL_REFS = {
    "D-0129",
    "E-0129",
    #: `D-0190` 是分支期预留号，正文还引用它，但索引行在合并时顺延成了 `D-0193`。
    #: 给 `D-0190` 补上索引行，这里会红。
    "D-0190",
}


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _index_row_ids(lines: list[str]) -> list[str]:
    """按**原样出现顺序**取出索引行编号（重复的也逐条保留，供重号判据计数）。"""
    ids: list[str] = []
    for line in lines:
        match = _INDEX_ROW_RE.match(line)
        if match is not None:
            ids.append(match.group(1))
    return ids


def _structural_pipe_count(line: str) -> int:
    """数**结构竖线**：`\\|` 与单反引号代码段内的 `|` 不算（它们是正文内容）。

    ⚠️ 刻意**不**读 markdown 渲染器：本网判的是「合并有没有弄坏行结构」，
    代码段内的裸 `|` 是作者有意写的内容，不是合并事故。
    """
    count = 0
    in_code = False
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            index += 2          # 转义竖线（`\|`）：下一字符不参与结构计数
            continue
        if char == "`":
            in_code = not in_code
        elif char == "|" and not in_code:
            count += 1
        index += 1
    return count


def _structural_column_count(line: str) -> int:
    """一行的结构列数 = 结构竖线数 − 1（一条 `| a | b |` 有 3 根竖线、2 列）。"""
    return _structural_pipe_count(line) - 1


def _column_mismatches(path: Path) -> dict[str, tuple[int, int]]:
    """返回 `{行的编号列: (实际列数, 表头列数)}`，只收列数对不上的行。

    ⚠️ 逐行判、**不跳过**：表头 / 分隔行以外的每一条 `|` 行都要跟当前表头比。
    ⚠️ 表头靠「**下一行是分隔行**」这个 markdown 表格形状来认；认不出的表头
    （首格不是 `编号` / `#`）会把基准**清空**（`header_columns = None`），
    ⛔ 不拿上一张表的列数去量下一张表的行。
    """
    lines = _read_lines(path)
    header_columns: int | None = None
    mismatches: dict[str, tuple[int, int]] = {}
    for index, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        next_is_separator = (
            index + 1 < len(lines) and _SEPARATOR_RE.match(lines[index + 1]) is not None
        )
        if next_is_separator:
            header_columns = (
                _structural_column_count(line) if _HEADER_RE.match(line) else None
            )
            continue
        if header_columns is None or _SEPARATOR_RE.match(line):
            continue
        actual = _structural_column_count(line)
        if actual != header_columns:
            key = line.split("|", 2)[1].strip()
            mismatches[key] = (actual, header_columns)
    return mismatches


def _cited_ids(path: Path, prefix: str) -> set[str]:
    """正文（⛔ 不含行首是 `>` 的取号 / 顺延注记块）里引用的**某前缀**编号。"""
    cited: set[str] = set()
    for line in _read_lines(path):
        if line.lstrip().startswith(">"):
            continue
        cited.update(re.findall(rf"({re.escape(prefix)}\d{{4}})", line))
    return cited


class DocIndexTableTests(TestCase):
    """这些网各管一件事；每条都能被一次定向变异弄红（证明见计划台账）。"""

    def test_index_row_counts_do_not_regress(self):
        """解析器**真的解析到了东西**，且没有整段被跳过（**下界**，⛔ 不是恰好相等）。"""
        for path in (DECISIONS_MD, ERRORS_MD):
            ids = _index_row_ids(_read_lines(path))
            self.assertGreaterEqual(
                len(ids),
                _MIN_ROW_COUNTS[path.name],
                msg=(
                    f"{path.name} 只解析到 {len(ids)} 条索引行（下界 {_MIN_ROW_COUNTS[path.name]}）："
                    "解析器可能匹配不到行、或整段被跳过（见 `_INDEX_ROW_RE` / `_read_lines`）。"
                    "⚠️ 用下界是**故意**的：按 `AGENTS.md` §5-B 新加一条索引行**不该**让这个测试红。"
                ),
            )

    def test_key_index_rows_are_still_parsed(self):
        """哨兵：下界之外再钉几个**必须**被解析到的行（两表首行 + 两个已知畸形行）。"""
        for path in (DECISIONS_MD, ERRORS_MD):
            ids = set(_index_row_ids(_read_lines(path)))
            missing = [
                sentinel for sentinel in _SENTINEL_IDS[path.name] if sentinel not in ids
            ]
            self.assertEqual(
                missing,
                [],
                msg=(
                    f"{path.name} 的哨兵索引行没被解析到：{missing}。"
                    "哨兵保护的是「解析到的是**该**区域」——把 `_INDEX_ROW_RE` 收窄、"
                    "或让 `_read_lines` 只读前半段，都会让它们缺席。"
                ),
            )

    def test_index_tail_anchor_is_monotonic(self):
        """尾部锚：解析到的**最大**编号 ≥ `_TAIL_ANCHOR_IDS`（单调下界，⛔ 不需要维护）。"""
        for path in (DECISIONS_MD, ERRORS_MD):
            ids = _index_row_ids(_read_lines(path))
            highest = max(ids) if ids else ""
            anchor = _TAIL_ANCHOR_IDS[path.name]
            self.assertGreaterEqual(
                highest,
                anchor,
                msg=(
                    f"{path.name} 解析到的最大索引编号是 {highest!r}，低于尾部锚 {anchor!r}："
                    "索引是**只增**的 ⇒ 这几乎一定是**尾部被截断**（最新的若干行被删或没被解析到），"
                    "而不是正常演进。请先确认索引尾部完整；若确为有意改号 / 顺延，"
                    "把 `_TAIL_ANCHOR_IDS` 的锚一起改成**实测的新最大号**并写明原因"
                    "（下界 `_MIN_ROW_COUNTS` 抓不到这一类，因为砍掉几行后条数仍可能在下界之上）。"
                ),
            )

    def test_no_duplicate_index_numbers_within_a_file(self):
        """④ 同一文件内四位编号不得重号（与人工 grep 同口径）。"""
        for path in (DECISIONS_MD, ERRORS_MD):
            ids = _index_row_ids(_read_lines(path))
            duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
            self.assertEqual(
                duplicates,
                [],
                msg=(
                    f"{path.name} 的索引表出现重号 {duplicates}："
                    "`git` 干净合并可能留下两行同号（`AGENTS.md` §5-F 实测）。"
                    "处置：按 §5-F 给后并入者顺延，并在同一个提交里改完全部引用。"
                ),
            )

    def test_index_rows_match_their_header_column_count(self):
        """⑤ 每行结构列数 == 表头列数；存量四行畸形显式钉死，⛔ 不是静默跳过。"""
        for path, prefix in ((DECISIONS_MD, "D-"), (ERRORS_MD, "E-")):
            actual = {
                key: columns
                for key, (columns, _header) in _column_mismatches(path).items()
            }
            pinned = {
                key: columns
                for key, (columns, _reason) in _KNOWN_MALFORMED_ROWS.items()
                if key.startswith(prefix)
            }
            # ⚠️ 两向的 `assertEqual`：新畸形行会红；钉住的那几行被改好（键消失 / 列数变了）也红。
            self.assertEqual(
                actual,
                pinned,
                msg=(
                    f"{path.name} 的索引行结构列数与表头对不上：实测 {actual}、钉住 {pinned}。"
                    "若这是新出现的畸形行（合并多 / 少了一根竖线），先修行；"
                    "若是存量且按「只增不改」不能就地修，把它连同列数与原因加进 `_KNOWN_MALFORMED_ROWS`；"
                    "若原畸形已被修好，请从 `_KNOWN_MALFORMED_ROWS` 里删掉它。"
                ),
            )

    def test_every_cited_number_resolves_to_an_index_row(self):
        """⑥ 正文引用的编号必须能在对应索引表里找到；同前缀与**跨前缀**两个方向都查。"""
        pairings = (
            (DECISIONS_MD, "D-", ERRORS_MD, "E-"),
            (ERRORS_MD, "E-", DECISIONS_MD, "D-"),
        )
        for path, prefix, other_path, other_prefix in pairings:
            own_index = set(_index_row_ids(_read_lines(path)))
            other_index = set(_index_row_ids(_read_lines(other_path)))
            # --- 同前缀：本文件的编号必须登记在本文件的索引里 ---
            own_unresolved = sorted(_cited_ids(path, prefix) - own_index)
            pinned = sorted(ref for ref in _KNOWN_HISTORICAL_REFS if ref.startswith(prefix))
            # ⚠️ 两向的 `assertEqual`：新引用解析不到会红；钉住的历史号被补上索引行也红。
            self.assertEqual(
                own_unresolved,
                pinned,
                msg=(
                    f"{path.name} 里引用、却在本文件索引表里找不到对应行的编号：{own_unresolved}"
                    f"（钉住的历史预留号是 {pinned}）。"
                    "「引用了却从未登记」是真实缺口：要么补索引行，要么确认它是历史预留号"
                    "（原句有「分支期预留 / 顺延」这类标注），再加进 `_KNOWN_HISTORICAL_REFS`；"
                    "若历史号已被补成正式索引行，请把它从钉子里删掉。"
                ),
            )
            # --- 跨前缀：本文件引用的 `E-` / `D-` 必须登记在**另一个文件**的索引里 ---
            cross_unresolved = sorted(_cited_ids(path, other_prefix) - other_index)
            self.assertEqual(
                cross_unresolved,
                [],
                msg=(
                    f"{path.name} 里引用的 {other_prefix}xxxx 在 {other_path.name} 的索引里"
                    f"找不到对应行：{cross_unresolved}。"
                    "只查同前缀会漏掉这一整类悬空引用；处置（按顺序核）："
                    f"① 若 {other_path.name} 只是漏登记，就在那边补索引行；"
                    "② 若是引用写错了号，就改引用；"
                    "③ 若这个号是**未合并分支预留 / 合并顺延后的旧号**（原句有「分支期预留 / 顺延」"
                    "这类标注），按 `_KNOWN_HISTORICAL_REFS` 的形态给跨前缀另加一条**带注释的显式钉子**"
                    "（⛔ 不许加 allowlist、⛔ 不许用「原因文本含某个词」之类的正则后门放行）。"
                ),
            )
