"""I4（终审修复轮，2026-10-25）锁序用例的**共用判据**：抓 SQL，钉「先锁 Order、再锁 Performance」。

## 为什么用 SQL 当判据（而不是「双线程跑一次看它死不死」）

死锁不保证复现（调度 / 行数 / 机器负载都能让它溜过去），绿了的并发用例等于没测。这里直接看
这次调用发出的 `SELECT … FOR UPDATE` **顺序**：订单行锁必须早于绩效行锁——与 `/assign/`
（`apply_transition` 先锁 Order、随后 `change_taker` 锁绩效行）同一条方向。反过来的形状
（先绩效、后订单）就是 AB-BA 成环的那一半。

## 怎么认「这条语句锁的是哪张表」

不能只看 SQL 里出没出现表名：绩效行那条查询是 `Performance.objects.select_for_update(
of=("self",))`，它的 SQL 里**也**会出现 `business_order`（`select_related` 的 JOIN 或
`order=…` 的 WHERE 子查询）。所以先切出 `FOR UPDATE OF …` 的**锁定目标段**再比对：

- `… FOR UPDATE OF "business_performance"` ⇒ 只锁绩效行；
- `… FOR UPDATE OF "business_order"` ⇒ 只锁订单行；
- 不带 `OF` 的 `… FOR UPDATE`（`change_sales_agent` 里那一句）⇒ 按查询里出现的表认
  （它没有 join，出现哪张就是哪张）。

⚠️ **判据要能失败**：把 `lock_order_row` 那句删掉（或挪到绩效行锁之后），本判据会以
「没有锁订单行 / 锁序反了」当场红——四处用例各钉一条边。
"""
from __future__ import annotations

from apps.business.models import Order, Performance


def _locking_targets(sql: str) -> str:
    """`FOR UPDATE [OF …]` 的**锁定目标段**（没有 `OF` ⇒ 空串，调用方按整条 SQL 认表）。"""
    upper = sql.upper()
    marker = " FOR UPDATE OF "
    if marker not in upper:
        return ""
    return upper.split(marker, 1)[1]


def _locks_table(sql: str, table: str) -> bool:
    """这条语句锁的是不是 `table` 那一行（口径见模块 docstring）。"""
    upper = sql.upper()
    if "FOR UPDATE" not in upper:
        return False
    quoted = f'"{table}"'.upper()
    targets = _locking_targets(sql)
    if targets:
        return quoted in targets
    return quoted in upper


def locking_statements(captured):
    """`CaptureQueriesContext.captured_queries` 里带 `FOR UPDATE` 的 `(序号, SQL)` 列表。"""
    return [
        (index, query["sql"])
        for index, query in enumerate(captured.captured_queries)
        if "FOR UPDATE" in query["sql"].upper()
    ]


def assert_order_locked_before_performance(case, captured):
    """断言这次调用**先锁订单行、后锁绩效行**（两把锁都必须真的被拿到）。"""
    order_table = Order._meta.db_table
    perf_table = Performance._meta.db_table
    locking = locking_statements(captured)
    order_locks = [i for i, sql in locking if _locks_table(sql, order_table)]
    perf_locks = [i for i, sql in locking if _locks_table(sql, perf_table)]
    case.assertTrue(
        order_locks,
        "本次调用没有锁订单行（I4 的 Order 行锁被删了？）：%r" % (locking,),
    )
    case.assertTrue(
        perf_locks,
        "本次调用没有锁绩效行 —— 用例没走到要测的那条边：%r" % (locking,),
    )
    case.assertLess(
        order_locks[0],
        perf_locks[0],
        "锁序反了：绩效行锁（query #%d）早于订单行锁（query #%d）—— I4 要消掉的 "
        "AB-BA 形状回来了：%r" % (perf_locks[0], order_locks[0], locking),
    )
