"""车队清单：**运维库存清单**（哪个租户在哪个库），**不是**权威租户目录。

本模块只做一件事：把手写的 `fleet.json` 解析成 `FleetEntry` 元组，供
`provision_tenant_db`（按清单供给）与 `fleet_status`（巡检）遍历。它**不连数据库**。

⚠️ **定位（改代码前先读）**：清单回答「我们打算/曾经供给哪些租户、库与域名叫什么」，
它是**运维库存**，**不是**权威租户目录。权威目录始终是**每个租户库自己 `public` 里的
`platform_tenant` / `platform_domain` 行**——那才是 `provision_tenant` 真正写进去、
请求路径真正读的东西。两者是否一致由 `fleet_status` 巡检**显式对账**（不一致报 `DRIFT`）；
**本模块不做合并、不回写**。所以：不要把「清单里有这一条」当成「这个租户存在」的判据，
也不要把清单当成租户目录的替身。

⚠️ **派生值不进清单**：条目只有三个键 `schema_name` / `name` / `domain`；database 与 role
由 `{TENANT_DB_PREFIX}{schema_name}` 在**读取时**派生（`FleetEntry.database` / `.role` 是属性、
不是字段）。写进 JSON 就会与派生规则漂移，所以一律由本模块算。

清单形状（**封闭形状**，多一个键都算错）::

    {"tenants": [{"schema_name": "dev", "name": "开发租户", "domain": "dev.test.local"}]}

**未知键一律报错**，顶层只允许 `tenants`、条目只允许上面三个键。理由：多余键最可能是
① 拼写错误（`domian`）——静默吞掉会让运维以为配置生效了；② 想存派生值（`database`）
——与上面的「派生值不进清单」直接冲突。两种都该响亮地失败，而不是猜。

闸门：`schema_name` 走 Task 1 的 `validate_schema_name`（在通用白名单之上再挡 `public`
与 `pg_` 前缀），并且**在读取时就把 database / role 派生一遍**——前缀会把长度顶过 63
（`tt_` + 61 = 64），而 `schema_name` 自己看是合法的，不在构造时算就会把错误推迟到
访问 `.database` 时才炸。
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from django.conf import settings

from apps.platform.db_provisioning import tenant_database_name, tenant_role_name
from apps.platform.identifiers import InvalidIdentifierError, validate_schema_name

# 清单条目**只**允许这三个键（封闭形状，见模块 docstring）。
ENTRY_KEYS = ("schema_name", "name", "domain")
# 顶层**只**允许这一个键。
TOP_LEVEL_KEYS = ("tenants",)


class FleetError(ValueError):
    """清单读不了 / 不合形状 / 不合闸门。

    继承 `ValueError`，与 `InvalidIdentifierError` 同族：管理命令既可以按本类型精确捕获，
    也可以按 `ValueError` 统一处理。**所有**错误信息都同时点名「出错的值」与「位置」
    （文件路径，条目问题再带第几条）——只报「清单不合法」排障时等于没报。
    """


def _preview(value: Any, *, limit: int = 120) -> str:
    """错误信息里给出错值的短预览（太大时截断，避免把整份清单打进日志）。"""
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _duplicated(values: Sequence[str]) -> list[str]:
    """返回重复出现的值（按首次出现顺序，每个值一次）；没有重复时返回空列表。

    检测判据是**集合大小比对**（集合变小 ⇔ 有重复），而错误信息必须**点名**是哪个值
    重复了——「清单里有重复」这种句子对排障没有信息量。
    """
    if len(set(values)) == len(values):
        return []
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return [value for value, count in counts.items() if count > 1]


@dataclass(frozen=True)
class FleetEntry:
    """清单里的一行：一个租户 + 它的域名。

    自校验：构造时就过闸门（`schema_name` 走 Task 1 的白名单 + 派生长度），所以
    「拿在手上的 `FleetEntry` 一定是合法的」这条不变量由类型自己保证——调用方
    （`provision_tenant_db` 从命令行参数造条目时也一样）不必再各自校验一遍。

    `.database` / `.role` 是**派生属性**，不是字段：它们恒等于
    `{TENANT_DB_PREFIX}{schema_name}`，存起来就会漂移。
    """

    schema_name: str
    name: str
    domain: str

    def __post_init__(self) -> None:
        for field_name in ENTRY_KEYS:
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise FleetError(
                    f"{field_name} 必须是字符串，收到 {type(value).__name__}：{value!r}"
                )
        if not self.name:
            raise FleetError(f"name 不能为空（清单条目要有人看得懂的名字）：{self.name!r}")
        if not self.domain:
            raise FleetError(
                f"domain 不能为空（域名是 Host 路由的判据）：{self.domain!r}"
            )
        try:
            validate_schema_name(self.schema_name)
            # 立刻派生一遍（结果丢掉）：`tt_` + 61 字符 = 64 > 63 只有**派生名**非法，
            # schema_name 单看是合法的 ⇒ 不在这里算，错误会推迟到访问 `.database` 时才炸。
            tenant_database_name(self.schema_name)
            tenant_role_name(self.schema_name)
        except InvalidIdentifierError as exc:
            raise FleetError(str(exc)) from exc

    @property
    def database(self) -> str:
        """租户库名（派生，见模块 docstring）。"""
        return tenant_database_name(self.schema_name)

    @property
    def role(self) -> str:
        """租户角色名（派生；与库名同值，PG 里两者是独立命名空间）。"""
        return tenant_role_name(self.schema_name)


def _parse_entry(raw: Any, *, index: int, manifest_path: Path) -> FleetEntry:
    """解析第 `index` 条（从 1 数），任何问题都带**路径 + 第几条 + 出错的值**（R3）。"""
    location = f"{manifest_path} 第 {index} 条"
    if not isinstance(raw, dict):
        raise FleetError(
            f"{location}：条目必须是 JSON 对象，收到 {type(raw).__name__}：{_preview(raw)}"
        )
    missing = [key for key in ENTRY_KEYS if key not in raw]
    if missing:
        raise FleetError(
            f"{location}：缺少字段 {missing!r}；条目只允许 {list(ENTRY_KEYS)!r}，"
            f"实际收到 {sorted(raw)!r}"
        )
    unknown = [key for key in raw if key not in ENTRY_KEYS]
    if unknown:
        raise FleetError(
            f"{location}：出现未知键 {unknown!r}（条目只允许 {list(ENTRY_KEYS)!r}）；"
            f"清单是封闭形状，派生值（database / role）不许写进 JSON"
        )
    try:
        return FleetEntry(
            schema_name=raw["schema_name"],
            name=raw["name"],
            domain=raw["domain"],
        )
    except FleetError as exc:
        raise FleetError(f"{location}：{exc}") from exc


def load_fleet(path: Path | None = None) -> tuple[FleetEntry, ...]:
    """读清单文件并返回条目元组；`path=None` 时读 `settings.TENANT_FLEET_FILE`。

    任何问题都抛 `FleetError`（继承 `ValueError`），信息里带上「哪个文件、第几条、
    什么值」。空的 `tenants` 数组是合法的（还没有租户），返回空元组。
    """
    manifest_path = Path(path) if path is not None else Path(settings.TENANT_FLEET_FILE)
    try:
        raw_text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FleetError(f"车队清单读不到：{manifest_path}（{exc}）") from exc
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise FleetError(
            f"车队清单不是合法 JSON：{manifest_path}"
            f"（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"
        ) from exc
    if not isinstance(document, dict):
        raise FleetError(
            f"车队清单顶层必须是 JSON 对象（形如 {{\"tenants\": [...]}}）：{manifest_path}；"
            f"收到 {type(document).__name__}：{_preview(document)}"
        )
    missing = [key for key in TOP_LEVEL_KEYS if key not in document]
    if missing:
        raise FleetError(
            f"车队清单缺少键 {missing!r}（顶层只允许 {list(TOP_LEVEL_KEYS)!r}）：{manifest_path}"
        )
    unknown = [key for key in document if key not in TOP_LEVEL_KEYS]
    if unknown:
        raise FleetError(
            f"车队清单顶层出现未知键 {unknown!r}（只允许 {list(TOP_LEVEL_KEYS)!r}）："
            f"{manifest_path}"
        )

    tenants = document["tenants"]
    if not isinstance(tenants, list):
        raise FleetError(
            f"tenants 必须是数组：{manifest_path}；"
            f"收到 {type(tenants).__name__}：{_preview(tenants)}"
        )

    entries = tuple(
        _parse_entry(raw_entry, index=index, manifest_path=manifest_path)
        for index, raw_entry in enumerate(tenants, start=1)
    )

    # 重复检查放在所有条目都解析成功之后：先让人把条目本身写对，再谈重复。
    duplicated_schemas = _duplicated([entry.schema_name for entry in entries])
    if duplicated_schemas:
        raise FleetError(
            f"车队清单出现重复的 schema_name {duplicated_schemas!r}"
            f"（一个 schema 只能对应一个租户）：{manifest_path}"
        )
    duplicated_domains = _duplicated([entry.domain for entry in entries])
    if duplicated_domains:
        raise FleetError(
            f"车队清单出现重复的 domain {duplicated_domains!r}"
            f"（一个域名只能路由到一个租户）：{manifest_path}"
        )
    return entries


def entry_for_schema(entries: Sequence[FleetEntry], schema_name: str) -> FleetEntry:
    """在 `entries` 里按 `schema_name` 找条目；找不到抛 `FleetError`。

    错误信息同时给出**要找的值**与**清单里现有的值**——排障时第一件事就是看拼错了没有。
    """
    for entry in entries:
        if entry.schema_name == schema_name:
            return entry
    known = [entry.schema_name for entry in entries]
    raise FleetError(
        f"清单里没有 schema_name={schema_name!r} 的租户；清单里有：{known!r}"
    )
