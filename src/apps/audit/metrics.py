"""`RequestMetric` 的写入原语（spec `docs/plans/2026-10-04-request-metrics.md` §2 的 UPSERT 口径）。不用 ORM 的 `bulk_create(update_conflicts=True, update_fields=[…])`（spec §2 给的另一条路）：Django 5.2 的 Postgres 后端只会生成 `"<field>" = EXCLUDED."<field>"`（`django/db/backends/postgresql/operations.py:394-406`，实测读源），**表达不了** `count = count + 1` / `max_ms = GREATEST(max_ms, EXCLUDED.max_ms)` 这类**数据库侧算术**；而 spec §2 明确禁止「先读再写」（并发下会丢更新）⇒ 选 `INSERT … ON CONFLICT … DO UPDATE` 原语：算术与 `GREATEST` 全写在 SQL 里、由数据库求值，没有读-改-写窗口。
写在这里而不是中间件里：表在 `apps/audit`（`TENANT_APPS`），写入原语跟着模型走；`apps/core/request_metrics.py` 只负责「什么时候写、写什么」。
⚠️ 这个分层还有一个**判据上的用途**（E-0029）：`mock.patch` 必须打在**被查找的那个模块**——中间件用 `from apps.audit.metrics import upsert_request_metric`，所以有效的桩是 `apps.core.request_metrics.upsert_request_metric`，不是 `apps.audit.metrics.*`。
"""
from django.db import connection, transaction

from apps.audit.models import RequestMetric

#: 唯一键的列（与 `RequestMetric.Meta.constraints` 那条 `UniqueConstraint` **同一组字面量**）。
#: `ON CONFLICT` 的推断目标就是它；两处漂移会让 UPSERT 静默退化成「每条请求都插一行」或报错。
UNIQUE_FIELDS = ("kind", "bucket_start", "route", "method", "status_code", "user_id")

#: 直方图分档**上界**（毫秒）：`d < 上界` 落进该档；比最后一个上界还大的落进 `>=3000` 档。
HIST_BOUNDS_MS = (100, 200, 500, 1000, 3000)

#: 直方图列名，**顺序与 `HIST_BOUNDS_MS` 一一对应**，末尾多一个兜底档（`>=3000`）。
#: ⚠️ 必须与 `RequestMetric` 的 `hist_*` 字段逐字一致（`tests/test_request_metrics.py` 钉住）。
HIST_FIELDS = (
    "hist_lt100",
    "hist_lt200",
    "hist_lt500",
    "hist_lt1000",
    "hist_lt3000",
    "hist_gte3000",
)

#: 目标表名（**从模型取**，⛔ 不手抄字符串：`db_table` 改了这里会跟着改）。
_TABLE = f'"{RequestMetric._meta.db_table}"'
_CONFLICT_COLUMNS = ", ".join(f'"{name}"' for name in UNIQUE_FIELDS)
_HIST_COLUMNS = ", ".join(f'"{name}"' for name in HIST_FIELDS)
_HIST_PLACEHOLDERS = ", ".join(["%s"] * len(HIST_FIELDS))
_HIST_UPDATES = ", ".join(
    f'"{name}" = {_TABLE}."{name}" + EXCLUDED."{name}"' for name in HIST_FIELDS
)

#: 一次 UPSERT：插入形状 = 「次数 1 / 总耗时=最大耗时=这一次的耗时 / 对应直方图桶 1」。
#: 冲突时把**增量**累加上去 —— 这正是「每请求一行」被折成有界汇总行的那一步。
_UPSERT_SQL = f"""
INSERT INTO {_TABLE} (
    "kind", "bucket_start", "route", "method", "status_code", "user_id", "username",
    "count", "total_ms", "max_ms",
    {_HIST_COLUMNS},
    "created_at", "updated_at"
) VALUES (
    %s, %s, %s, %s, %s, %s, %s,
    1, %s, %s,
    {_HIST_PLACEHOLDERS},
    NOW(), NOW()
)
ON CONFLICT ({_CONFLICT_COLUMNS}) DO UPDATE SET
    "count" = {_TABLE}."count" + EXCLUDED."count",
    "total_ms" = {_TABLE}."total_ms" + EXCLUDED."total_ms",
    "max_ms" = GREATEST({_TABLE}."max_ms", EXCLUDED."max_ms"),
    {_HIST_UPDATES},
    "username" = EXCLUDED."username",
    "updated_at" = NOW()
"""


def hist_index(duration_ms: int) -> int:
    """耗时落在第几个直方图档（`HIST_FIELDS` 的下标）。边界是**左闭右开**。

    `d == 3000` ⇒ 落 `hist_gte3000`（因为 `d < bound` 不成立）；`d == 100` ⇒ `hist_lt200`。
    """
    for index, bound in enumerate(HIST_BOUNDS_MS):
        if duration_ms < bound:
            return index
    return len(HIST_BOUNDS_MS)


def upsert_request_metric(
    *,
    kind: str,
    bucket_start,
    route: str,
    method: str,
    status_code: int,
    user_id: int,
    username: str,
    duration_ms: int,
) -> None:
    """把**一次**请求折进 `RequestMetric`（spec §2）：命中唯一键就累加，否则插一行。`kind="agg"`：`bucket_start` 传整点、`duration_ms` 是这一次的耗时（作为增量累加）；`kind="slow"`：`bucket_start` 传那一次请求的时刻，插入形状同样是 `count=1 / total_ms=max_ms=这一次的耗时 / 只置对应那一个直方图桶`（spec §3）。
    ⚠️ **如实登记**：唯一键含 `bucket_start`，而 `slow` 的 `bucket_start` 是**请求时刻** ⇒ 两条 `bucket_start` **完全相同**的同类慢请求会**当场合并**成一行（`count` 累加、`total_ms` 相加、`max_ms` 取大者、直方图桶各 +1）——⛔ 不是「同一微秒实际不会发生」（能被一个反例证伪的绝对句）；判据：`tests/test_request_metrics.py` 里「同一时刻两条慢请求合并」的用例。
    ⛔ 本函数**不吞异常**：调用方（中间件）负责「绝不让用户请求失败」（spec §5），那是中间件的承重口径，不在这里做（免得同一件事两处各写一半）。⚠️ `transaction.atomic()` 在这里是**承重**的，不是防御性写法：包一层保存点后写库失败只回滚到保存点；不包的话，在已有事务里（`ATOMIC_REQUESTS`、或测试的 `TestCase`）一条失败的 SQL 会把**整个**事务标脏、之后任何查询都抛 `TransactionManagementError` ⇒ 埋点失败会连带把用户请求弄成 500，正好违反 spec §5。
    """
    histogram = [0] * len(HIST_FIELDS)
    histogram[hist_index(duration_ms)] = 1
    params = (
        kind,
        bucket_start,
        route,
        method,
        status_code,
        user_id,
        username,
        int(duration_ms),
        int(duration_ms),
        *histogram,
    )
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(_UPSERT_SQL, params)
