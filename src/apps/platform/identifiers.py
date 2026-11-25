"""标识符闸门：把会被拼进 SQL、或用来派生库名与角色名的字符串卡在严格白名单内。

    不用 django-tenants 自带的 schema 名校验：它只挡 `pg_` 前缀和长度，
    `'x"; DROP DATABASE postgres; --'` 这种值也能通过，而它自己用字符串拼接建 schema。
    新代码一律走本模块。
"""
import re

# 严格白名单：只允许小写字母 / 数字 / 下划线，首字符不能是数字，总长 ≤ 63
# （63 是 PostgreSQL 标识符 `NAMEDATALEN - 1` 的上限；因为只放行 ASCII，字节数 == 字符数）。
# ⚠️ 用 `fullmatch`，**不要**改回 `match`：`$` 锚点在结尾换行**之前**就收尾，
# `IDENTIFIER_RE.match('dev\n')` 会放行（实测）——上面那句「只允许…」会被这一个反例证伪。
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# 平台平面的 schema 名。它必须在这里被挡住：`public` 会通过上面的白名单。
PUBLIC_SCHEMA_NAME = "public"

# 平台库自己的保留 schema 名。`platform` 能通过上面的白名单，不挡就会出现同名租户。
# `validate_schema_name` 会被库名派生调用，所以平台库的库名 / 角色名不从这里派生，
# 改用独立设置 `PLATFORM_DB_NAME`。
PLATFORM_SCHEMA_NAME = "platform"


class InvalidIdentifierError(ValueError):
    """值不在严格白名单内。

    继承 `ValueError`：调用方（管理命令）既可以按本类型精确捕获，也可以按 `ValueError`
    统一处理——本模块**只**用这一种错误类型表达「这个名字不能用在 SQL 里」。
"""


def validate_identifier(value: str, *, what: str) -> str:
    """校验一个即将进入 SQL 的标识符，合法则原样返回。

    `what` 是给人看的字段名（如 `"schema name"` / `"database name"`），只进错误信息。
"""
    # 非 str 交给 `re.fullmatch` 会抛 `TypeError`（不是本模块的异常），必须显式挡下。
    if not isinstance(value, str):
        raise InvalidIdentifierError(
            f"{what} 必须是字符串，收到 {type(value).__name__}：{value!r}"
        )
    # `fullmatch` 而非 `match`——理由见上面 `IDENTIFIER_RE` 那条注释（结尾换行会被 `$` 放过）。
    if not IDENTIFIER_RE.fullmatch(value):
        raise InvalidIdentifierError(
            f"{what} 不是合法标识符（只允许小写字母/数字/下划线，"
            f"首字符不能是数字，长度 1–63）：{value!r}"
        )
    return value


def validate_schema_name(value: str) -> str:
    """在通用白名单之上，再挡住三个 PostgreSQL 特殊名 / 保留名。

    - `public`：平台平面。放行它会把租户数据写进共享 schema。
    - `pg_` 前缀：PostgreSQL 自己的保留前缀。
    - `platform`：**平台库自己**的保留名（`PLATFORM_SCHEMA_NAME`，spec §7-Q2）。它是
    「平台库」的 schema 名，不是租户名——放行它会让平台库与某个租户争同一个派生名。
    ⚠️ 本函数从此**只服务租户 schema 名**；平台库的名字不再从这里派生（见常量上方那条注释）。
"""
    validate_identifier(value, what="schema name")
    if value == PUBLIC_SCHEMA_NAME:
        raise InvalidIdentifierError(
            f"schema name 不能是 {value!r}（那是平台平面，不是租户 schema）"
        )
    if value == PLATFORM_SCHEMA_NAME:
        raise InvalidIdentifierError(
            f"schema name 不能是 {value!r}"
            f"（那是平台库自己的保留名，不是租户 schema）"
        )
    if value.startswith("pg_"):
        raise InvalidIdentifierError(
            f"schema name 不能以 'pg_' 开头（PostgreSQL 保留前缀）：{value!r}"
        )
    return value
