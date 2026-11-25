"""端点注册表：spec/endpoints.json 的内存视图与检索。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jidanpu.errors import ConfigError

logger = logging.getLogger(__name__)

_PATH_PARAM_RE = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True, slots=True)
class Param:
    name: str
    location: str
    type: str
    required: bool
    description: str = ""


@dataclass(frozen=True, slots=True)
class Endpoint:
    operation_id: str
    method: str
    path: str
    name: str
    tags: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    #: **参数名**，来自扒租户前端 JS 的静态读码（`spec/write_params.json`）。
    #: 只有名字：位置、类型、必填性都看不出来，所以**不**参与必填校验，
    #: 也不放进 `params`（那三个字段就只能编值）。用途是「这个接口要传哪些参数名」。
    param_names: tuple[str, ...] = ()
    #: 响应字段名。实测得来时是只读扫描的真实响应键（`spec/response_shapes.json` 经
    #: `tools/import_endpoints.py --response-shapes` 合并）；没有实测的端点回退到权威索引的残缺快照。
    #: 只给名字，类型见 `spec/response_shapes.json` 与 `jidanpu.models.generated`。
    response_fields: tuple[str, ...] = ()
    risk: str = "read"
    evidence: str = "unverified"
    #: 端点显式声明的成功语义，取值见 `jidanpu.transport.SUCCESS_RULES`。`None`=清单没声明，
    #: 此时由 `client.call()` 按 `risk` 取默认（write→positive_code，其余→lenient）。显式声明来自
    #: `spec/success_codes.json`（`tools/import_endpoints.py` 合并），压过 `risk` 默认——既可登记实测写接口，也可显式写 `"lenient"` 作例外。
    success_rule: str | None = None
    #: 端点的分页口径（实测），来自 `spec/pagination.json`（`tools/import_endpoints.py --pagination` 合并）。`None`=清单没登记。
    #: ⚠️ 这是描述性元数据：`iter_all()` 目前不会自动按它翻页——例如 `limit_effective=False` 的端点每页固定行数，
    #: 用默认 `page_size=100` 调 `iter_all()` 会在第一页就判「不满页」而提前结束。
    pagination: dict[str, Any] | None = None
    #: 清单里的分组域名（如 `OrderAjax`），载入的端点都带。
    domain: str = ""
    #: 调用该端点时应带的 `Referer`（清单里的扁平 `referer` 键）；可为空串。
    referer: str = ""

    @property
    def path_params(self) -> tuple[str, ...]:
        """路径模板里的参数名，如 /config/{type} → ("type",)。"""
        return tuple(_PATH_PARAM_RE.findall(self.path))

    def required_names(self, location: str) -> tuple[str, ...]:
        """某一位置上的必填参数名。"""
        return tuple(p.name for p in self.params if p.required and p.location == location)

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass(frozen=True, slots=True)
class Registry:
    endpoints: tuple[Endpoint, ...] = field(default_factory=tuple)

    def __len__(self) -> int:
        return len(self.endpoints)

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Registry:
        items = spec.get("endpoints")
        if not isinstance(items, list):
            raise ConfigError("spec 缺少 endpoints 数组")

        endpoints: list[Endpoint] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            params = tuple(
                Param(
                    name=str(p.get("name", "")),
                    location=str(p.get("location", "")),
                    type=str(p.get("type", "string")),
                    required=bool(p.get("required")),
                    description=str(p.get("description", "")),
                )
                for p in (raw.get("params") or [])
                if isinstance(p, dict) and p.get("name")
            )
            endpoints.append(
                Endpoint(
                    operation_id=str(raw.get("operation_id", "")),
                    method=str(raw.get("method", "")).lower(),
                    path=str(raw.get("path", "")),
                    name=str(raw.get("name", "")),
                    tags=tuple(str(t) for t in (raw.get("tags") or [])),
                    params=params,
                    param_names=tuple(
                        str(n) for n in (raw.get("param_names") or []) if isinstance(n, str) and n
                    ),
                    response_fields=tuple(
                        str(n)
                        for n in (raw.get("response_fields") or [])
                        if isinstance(n, str) and n
                    ),
                    risk=str(raw.get("risk", "read")),
                    evidence=str(raw.get("evidence", "unverified")),
                    success_rule=(
                        str(raw["success_rule"]) if raw.get("success_rule") else None
                    ),
                    pagination=(
                        dict(raw["pagination"])
                        if isinstance(raw.get("pagination"), dict)
                        else None
                    ),
                    domain=str(raw.get("domain") or ""),
                    referer=str(raw.get("referer") or ""),
                )
            )
        return cls(endpoints=tuple(endpoints))

    def find(self, pattern: str) -> list[Endpoint]:
        """大小写不敏感检索，匹配 path / name / tags / operation_id / 页面路由（`referer`）/ 参数名——为了回答
        用用户的话问出来的问题（如 `find("/Order/orderlist")`、`find("gameName")`、`find("黑名单")`）。
        清单里 349/359 个端点带页面路由、覆盖 76 个前端页面，是从前端功能定位接口最直接的路；参数名只覆盖有记录的端点（见 `param_names`）。"""
        needle = pattern.lower()
        return [
            e
            for e in self.endpoints
            if needle in e.path.lower()
            or needle in e.name.lower()
            or needle in e.operation_id.lower()
            or needle in e.referer.lower()
            or any(needle in t.lower() for t in e.tags)
            or any(needle in n.lower() for n in e.param_names)
        ]

    def by_operation_id(self, operation_id: str) -> Endpoint | None:
        for e in self.endpoints:
            if e.operation_id == operation_id:
                return e
        return None

    def duplicate_keys(self) -> dict[tuple[str, str], tuple[Endpoint, ...]]:
        """返回 (method, path) 重复的组。真实清单里有 16 组重复（32 个端点），多是「分页版 / 普通版」
        两条记录或同一 `operation_id` 语义重复；不是抽取错误，但会让按 path 的检索产生歧义。"""
        grouped: dict[tuple[str, str], list[Endpoint]] = {}
        for endpoint in self.endpoints:
            grouped.setdefault((endpoint.method, endpoint.path), []).append(endpoint)
        return {key: tuple(value) for key, value in grouped.items() if len(value) > 1}

    def match(self, method: str, path: str) -> Endpoint | None:
        """先精确匹配 method+path，再按 {param} 模板匹配。
        ⚠️ (method, path) 在清单里重复时返回第一条并记 warning——精确指定用 `by_operation_id`；
        多个路径模板同时命中（如 `/excel/download/{id}` 与 `/{type}`）同样返回文件顺序第一条并记 warning 点名竞争者。"""
        method = method.lower()
        grouped: dict[tuple[str, str], list[Endpoint]] = {}
        for endpoint in self.endpoints:
            grouped.setdefault((endpoint.method, endpoint.path), []).append(endpoint)

        candidates = grouped.get((method, path))
        if candidates:
            if len(candidates) > 1:
                logger.warning(
                    "%s %s 在清单里有 %d 条重复记录，返回第一条 %s；"
                    "如需精确指定请改用 by_operation_id。",
                    method.upper(),
                    path,
                    len(candidates),
                    candidates[0].operation_id,
                )
            return candidates[0]

        matched = [
            endpoint
            for endpoint in self.endpoints
            if endpoint.method == method
            and "{" in endpoint.path
            and _template_matches(endpoint.path, path)
        ]
        if not matched:
            return None
        if len(matched) > 1:
            logger.warning(
                "%s %s 同时匹配 %d 个路径模板（%s），返回第一个 %s；"
                "如需精确指定请改用 by_operation_id。",
                method.upper(),
                path,
                len(matched),
                "、".join(f"{e.operation_id}（{e.path}）" for e in matched),
                matched[0].operation_id,
            )
        return matched[0]


def _template_matches(template: str, path: str) -> bool:
    """模板匹配：每个 {param} 恰好吃掉一个路径段，字面量部分按字面处理。
    字面量必须 `re.escape`——否则 `/v1.0/{id}` 会把 `.` 当通配符而命中 `/v1X0/9`。"""
    parts: list[str] = []
    cursor = 0
    for param in _PATH_PARAM_RE.finditer(template):
        parts.append(re.escape(template[cursor : param.start()]))
        parts.append("[^/]+")
        cursor = param.end()
    parts.append(re.escape(template[cursor:]))
    return re.match("^" + "".join(parts) + "$", path) is not None


def load_registry(path: Path) -> Registry:
    """从 spec/endpoints.json 载入注册表。"""
    if not path.exists():
        raise ConfigError(
            f"找不到端点清单 {path}。清单是运行期数据，随 jidanpu 包一起安装，"
            f"所以这条错误通常意味着安装不完整（例如 wheel 缺数据文件），"
            f"而不是「需要先生成点什么」。可以重装 jidanpu-sdk，"
            f"或用 JidanpuClient(spec_path=...) 显式指定清单。"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"端点清单 {path} 不是合法的 UTF-8 文本：{exc}") from exc
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"端点清单 {path} 不是合法 JSON：{exc}") from exc
    if not isinstance(spec, dict):
        raise ConfigError(
            f"端点清单 {path} 的顶层必须是 JSON 对象（含 endpoints 数组），"
            f"实际是 {type(spec).__name__}。清单由仓库里的 tools/import_endpoints.py "
            f"生成，且随包发布，请确认它没被手改或截断。"
        )
    return Registry.from_spec(spec)
