"""注入预算守卫：`AGENTS.md` 必须装得进宿主的 64 KB 指令预算，否则它**整份不会被注入**。

## 为什么要这条网（本线实测）

宿主（DSH）把工作区里的指令文件拼成**一块**注入，**总预算 65536 字节**；装不下时的做法是
**从最宽泛的文件开始丢**——文件顺序是 `AGENTS.md` → `AGENTS.local.md`（`.local` 最具体、排最后）。
本仓 2026-09-24 之前 `AGENTS.md` 已涨到 **73,493 字节**（其中 §2 当前阶段 35 KB、§5 工作流约定
30 KB，两节占全文 **90%**）⇒ 宿主**把它整份丢弃**，agent 每次会话只看到 `AGENTS.local.md`：
§0 的白话要求、§4 的禁区、§5 的工作流与编号纪律**全部「文件里写了、但读不到」**，而且是**静默**的
（只在注入头部一行小字里说 `omitted AGENTS.md`）。⚠️ 同一根因的另一种症状：在 `.worktrees/**`
这类**没有 `.local` 覆盖**的检出里，表现为**截断**（实测 73,668 → 65,108 字节）——丢的是后半截。

## 判据（阈值留了余量，不是「越小越好」）

- `AGENTS.md` 单份 **≤ 40,000 字节**：它必须**自己**就能装下（`.local` 可能更大，且本文件在丢弃
  顺序里第一个被丢 ⇒ 「只剩它」是真实情形）。
- `AGENTS.md` + `AGENTS.local.md`（存在时）**≤ 60,000 字节**：给宿主框架文本（约 200 字节）与
  `.local` 的正常增长留 4 KB。

超限时的正确处置**不是**删规则，而是把**会一直涨**的内容外移到 `docs/`——先例见
`docs/STATUS.md`（进度流水）与 `docs/WORKFLOW.md`（工作流长解释），两份都是**逐字外移、未删**。
"""

import unittest
from pathlib import Path
from unittest import TestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
AGENTS_LOCAL_MD = REPO_ROOT / "AGENTS.local.md"

MAX_AGENTS_BYTES = 40_000
MAX_COMBINED_BYTES = 60_000


def _size(path: Path) -> int:
    return path.stat().st_size


_HAS_INTERNAL_FILES = (Path(__file__).resolve().parents[2] / "AGENTS.md").is_file()

@unittest.skipUnless(_HAS_INTERNAL_FILES, "本仓库只开源网页前后端源码，不含内部规则文件 AGENTS.md")
class AgentsMdBudgetTests(TestCase):
    def test_agents_md_alone_fits_the_injection_budget(self):
        size = _size(AGENTS_MD)
        self.assertLessEqual(
            size,
            MAX_AGENTS_BYTES,
            msg=(
                f"AGENTS.md 已经 {size:,} 字节（上限 {MAX_AGENTS_BYTES:,}）：再涨就会像 2026-09-24 那样"
                "被宿主整份丢弃，团队规则对 agent 静默失效。"
                "处置：把会一直涨的内容外移到 docs/（先例 docs/STATUS.md、docs/WORKFLOW.md），"
                "不要在 AGENTS.md 里删规则。"
            ),
        )

    def test_combined_instruction_files_fit_the_injection_budget(self):
        parts = [AGENTS_MD] + ([AGENTS_LOCAL_MD] if AGENTS_LOCAL_MD.exists() else [])
        total = sum(_size(path) for path in parts)
        detail = " + ".join(f"{path.name}={_size(path):,}" for path in parts)
        self.assertLessEqual(
            total,
            MAX_COMBINED_BYTES,
            msg=(
                f"指令文件合计 {total:,} 字节（{detail}；上限 {MAX_COMBINED_BYTES:,}）："
                "宿主的 64 KB 预算是**所有指令文件加起来的总额**，超限时先丢 AGENTS.md。"
                "处置：先瘦 AGENTS.md（外移内容），再看 AGENTS.local.md 是否该搬走历史内容。"
            ),
        )
