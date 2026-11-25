"""渲染权限接线位点表的**生成物** `docs/RBAC-WIRING-MAP.md`。

设计：`docs/specs/2026-09-21-rbac-wiring-map-design.md` §4。
**单一出处是 `apps/rbac/wiring.py`**——本文件只是它的视图，改文档请改登记表再重跑：

    uv run python manage.py render_rbac_wiring_map          # 写文件
    uv run python manage.py render_rbac_wiring_map --check  # 只比对（不一致非零退出）

`--check` 的用途与 `makemigrations --check` 同：让"文档过期/被手改"在门禁里响亮地失败
（`tests/test_rbac_wiring.py` 的 T4 也走同一个函数，两处不会各写一份渲染逻辑）。
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.rbac import wiring
from apps.rbac.catalog import MODULES, NODES
from apps.rbac.choices import NodeKind

#: 生成物的仓库相对路径（写盘与 `--check` 共用）。
DOC_REL = "docs/RBAC-WIRING-MAP.md"

_KIND_LABEL = dict(NodeKind.CHOICES)
_LAYER_LABEL = {wiring.INTERCEPT: "拦截", wiring.EXPERIENCE: "体验"}


def _cell(text) -> str:
    """表格单元格转义：`|` 会破坏 Markdown 表结构，换行统一走 `<br>`。"""
    return str(text).replace("|", "\\|").replace("\n", "<br>").strip()


def _layer_cell(code: str) -> str:
    if code not in wiring.SITES:
        return "—"
    layers = []
    for site in wiring.SITES[code]:
        label = _LAYER_LABEL[site.layer]
        if label not in layers:
            layers.append(label)
    return "+".join(layers)


def _site_or_reason_cell(code: str) -> str:
    """位点与**拦截层**待定原因**并存渲染**（2026-09-23 口径修订）。

    `EXPERIENCE` 位点与 `PENDING` 可以同时存在：菜单确实声明了 permissions 的点，
    不因为它还没有服务端判据就把「前端可见性已接」这条真实信息抹掉。所以两段都渲染，
    顺序 = 位点在前、待定原因在后（读者先看到「已接了什么」，再看到「拦截层为什么还没有」）。
    """
    parts = []
    for site in wiring.SITES.get(code, ()):
        note = f"（{site.note}）" if site.note else ""
        parts.append(f"[{_LAYER_LABEL[site.layer]}] `{site.path}`{note}")
    if code in wiring.PENDING:
        reason = _cell(wiring.PENDING[code])
        parts.append(f"**拦截层待定**：{reason}" if code in wiring.SITES else reason)
    return "<br>".join(parts)


def _has_intercept(code: str) -> bool:
    return any(s.layer == wiring.INTERCEPT for s in wiring.SITES.get(code, ()))


def _owner_cell(code: str) -> str:
    return "③线（rbac-3）" if code in wiring.IN_FLIGHT else ""


def render_wiring_map() -> str:
    """渲染整份文档。**纯函数**：不碰文件系统，T4 直接拿它与盘上内容比对。"""
    module_name = {code: name for code, _bid, name, _note, _url in MODULES}
    lines = [
        "# 权限接线位点表（RBAC Wiring Map）",
        "",
        "> ⚠️ **生成物，勿手改。** 单一出处是 `src/apps/rbac/wiring.py`；改完登记表后重跑",
        "> `uv run python manage.py render_rbac_wiring_map`（`--check` 只比对）。",
        "> 设计与口径见 `docs/specs/2026-09-21-rbac-wiring-map-design.md`。",
        "",
        "**两态**：`拦截已定` = 有 `INTERCEPT` 位点；`拦截待定` = 在 `PENDING` 里写明阻塞原因。",
        "**层**：`拦截` = 服务端拦得住（`available` 的口径就是它）；`体验` = 前端菜单/路由可见性，",
        "**不等于**访问控制。`available` 只算拦截层。",
        "⚠️ **2026-09-23 口径修订**：`INTERCEPT` 位点与 `PENDING` **互斥**；`EXPERIENCE` 位点"
        "可与 `PENDING` **并存**",
        "（菜单确实声明了 permissions、服务端还没判据的点，其「前端可见性已接」不再被抹掉——"
        "见 `apps/rbac/wiring.py` 的模块 docstring）。",
        f"**③线在飞**（`IN_FLIGHT`，{len(wiring.IN_FLIGHT)} 点）：`available` 与位点允许暂时不一致，",
        "③线合并时它的收口任务负责回填并清空该集合。",
        "",
        f"合计 **{len(NODES)}** 个功能点：有拦截位点 **{sum(1 for n in NODES if _has_intercept(n[0]))}** / "
        f"只有体验位点 **{sum(1 for n in NODES if n[0] in wiring.SITES and not _has_intercept(n[0]))}** / "
        f"完全无位点 **{sum(1 for n in NODES if n[0] not in wiring.SITES)}**；",
        f"**拦截层待定（`PENDING`）{len(wiring.PENDING)}**（含与体验位点并存的 "
        f"{sum(1 for n in NODES if n[0] in wiring.PENDING and n[0] in wiring.SITES)} 条）、"
        f"`available=True` **{sum(1 for n in NODES if n[6])}** 个。",
        "",
        "## 一、按栏目汇总",
        "",
        "| 栏目 | 功能点 | 有拦截位点 | 只有体验位点 | 拦截层待定 | 其中③线在飞 |",
        "|---|---|---|---|---|---|",
    ]
    for module, _bid, name, _note, _url in MODULES:
        mine = [n for n in NODES if n[0].split(".")[0] == module]
        if not mine:
            continue
        lines.append(
            f"| {_cell(name)} | {len(mine)} | "
            f"{sum(1 for n in mine if _has_intercept(n[0]))} | "
            f"{sum(1 for n in mine if n[0] in wiring.SITES and not _has_intercept(n[0]))} | "
            f"{sum(1 for n in mine if n[0] in wiring.PENDING)} | "
            f"{sum(1 for n in mine if n[0] in wiring.IN_FLIGHT)} |"
        )
    lines += [
        f"| **合计** | **{len(NODES)}** | "
        f"**{sum(1 for n in NODES if _has_intercept(n[0]))}** | "
        f"**{sum(1 for n in NODES if n[0] in wiring.SITES and not _has_intercept(n[0]))}** | "
        f"**{len(wiring.PENDING)}** | **{len(wiring.IN_FLIGHT)}** |",
        "",
        "## 二、逐点登记（按 `benchmark_id` 顺序，与权限树渲染同序）",
        "",
        "| 栏目 | code | 形态 | 对标名称 | available | 层 | 位点 / 拦截层待定原因 | 归属 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for code, _bid, name, _note, _url, kind, available in NODES:
        lines.append(
            f"| {_cell(module_name.get(code.split('.')[0], code.split('.')[0]))} | `{code}` | {_KIND_LABEL[kind]} | "
            f"{_cell(name)} | {'是' if available else '否'} | {_layer_cell(code)} | "
            f"{_site_or_reason_cell(code)} | {_owner_cell(code)} |"
        )
    lines.append("")
    return "\n".join(lines)


class Command(BaseCommand):
    help = "渲染 docs/RBAC-WIRING-MAP.md（生成物；--check 只比对）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="只比对盘上内容与渲染结果，不一致则非零退出（不写文件）",
        )

    def handle(self, *args, **options):
        path = Path(settings.REPO_ROOT) / DOC_REL
        rendered = render_wiring_map()
        if options["check"]:
            actual = path.read_text(encoding="utf-8") if path.exists() else ""
            if actual != rendered:
                self.stderr.write(
                    f"{DOC_REL} 与 apps/rbac/wiring.py 不一致（生成物过期或被手改）："
                    "跑 `manage.py render_rbac_wiring_map` 重新生成"
                )
                raise SystemExit(1)
            self.stdout.write(f"{DOC_REL} 一致（{len(NODES)} 行）")
            return
        path.write_text(rendered, encoding="utf-8")
        self.stdout.write(f"已写入 {DOC_REL}：{len(NODES)} 行")
