"""开放平台限流的**唯一计数入口**：DB 固定窗口桶（v2 设计文档 §10 / §4-V4；T3）。

## 这里是什么、⛔ 不是什么

本模块只做一件事：**把「这一次调用」记进一张表，并当场回答「还放不放行」**。⛔ 它**不是配额**
（没有按套餐分级、没有"用超了要提额"），阈值由**调用方**给（`limit` / `window_seconds`），
本模块一个数字都不自带 —— 于是「哪个端点多少额度」这条事实只有**一处**出处（各自的模块常量）。

⭐ **唯一的计数出口是 `consume()`**：全仓**不允许**再有第二份"读计数 → 加一 → 写回"的实现
（`AGENTS.md` §5-E：同一个事实两套判据）。本模块另导出 `current_count()`，那是**只读**的
（`SELECT`，⛔ 不建行、⛔ 不加一）—— 给「闸门在放行之前先看一眼要不要直接拒」那一档用，
见下面「为什么闸门需要一次只读」。

## ⭐ 为什么「记」与「判」必须是**一条**语句（⛔ 不是先读再写）

`INSERT … ON CONFLICT (…) DO UPDATE SET count = <表>.count + 1 RETURNING count` 是**原子**的：
PostgreSQL 在冲突行上加行锁、读出**当时**的计数再写回，两条并发连接不会各读到同一个旧值。
换成「先 `SELECT` 再 `UPDATE`」（`update_or_create()` / `get_or_create()` + `save()` 都是这个形状）
在并发下会**少记**：两条连接都读到 5、都写 6 ⇒ 实际发生 7 次调用却只记了 6 次
⇒ 额度被放宽，而**没有任何症状**指向它（多 worker 下正是这个形状）。

⚠️ **表名与约束名从模型取，⛔ 不硬编码**（`_bucket_table_and_constraint()`，模型经
`spec.bucket_model` 给）：
硬编码的字符串会在模型改名 / 换 app 时**静默失效**（SQL 照跑、报的是"表不存在"或更糟：
插进了另一张同名的表）。⚠️⭐ **T2**：连"**哪张表**"本身也成了 spec 的一件东西
—— 改造前它硬编码 `OpenApiRateBucket`，那样打手侧那一族会把计数写进**上家侧**的桶表
（spec §4 第 5 条）。⚠️ 租户 schema 由**当前连接**的 `search_path` 决定
（`TENANT_APPS` 里每个租户 schema 各一张表）⇒ ⛔ 不把 schema 名拼进 SQL、
⛔ 也不接受任何来自请求参数的租户标识（`AGENTS.md` §4 的红线）。

## 窗口口径 = **固定窗口**（⛔ 不是滑动窗口）

`window_start = (now // window_seconds) * window_seconds` 的 UTC 时刻：同一窗口内所有调用落在
**同一行**，跨过边界自然换行 ⇒ 不需要任何"过期"机制，旧行由**机会式清理**删掉（见下）。
⚠️ 固定窗口的代价（**有意接受**）：跨边界时相邻两个窗口各自都能放满，最坏约两倍。
沿用旧实现的口径（`throttling.py` 的 `bucket_key` 也是 `int(now // 窗口秒数)`），⛔ 本次不改窗口形态。

## ⭐ 边界语义（这一条是判据，用例钉着它）

**`count <= limit` ⇒ 放行**，即**放行总数恰好 = limit**：第 `limit` 次放行、第 `limit + 1` 次被挡。
⚠️ 这与旧实现「先判后记」（`used >= MAX` 就挡）**结果相同**，但**实现顺序变了**
（旧：先读、判、再写；新：一次写读回、再判）⇒ 两种写法的等价性由既有用例
`tests/test_openapi_throttling.py` 的边界用例守着（⛔ 不许为了好写把它放宽成 `>=` / `<=`）。

⚠️ **行里那个 `count` 是本窗口的「尝试数」，⛔ 不是「放行数」**（被挡的那几次**也加一**，
因为「记」与「判」是同一条语句、判决在写回之后才拿到）。可观察行为**没有差别**
（`count <= limit` 一票否决 ⇒ 进了这个窗口就一直挡）；要改成"只数放行的"就得**先判定再写回**
⇒ 退回读-改-写，正是本模块开头否掉的形状（并发下少记）。判据（两条连接的 `count` 等于
两次尝试之和）见 `tests/test_openapi_rate_limit.py::MultiConnectionEquivalenceTests`。

## 机会式清理（⭐ T1 没有 TTL ⇒ 这一半必须是本模块的责任）

DB 行**不会自己过期**（不像 `cache.set(..., timeout)`）。清理点只有一处：**本窗口第一次命中**
（`RETURNING count == 1`）时，删掉 `window_start` 早于「当前窗口左端 − `RETENTION_WINDOWS` 个窗口」
的行。为什么挂在这个时机：它天然是「每 (维度, 窗口) 恰好一次」的，⛔ 不需要常驻任务、
⛔ 也不需要给每次调用都加一条 `DELETE`。

- ⛔ 清理条件里**没有**「时间兜底 / 按大小 / 按前缀」这类模糊条件：条件就是
  `window_start < 明确算出的时刻`（`AGENTS.md` §5-A 对破坏性动作的要求）；
- ⚠️ **为什么要给 `window_start` 加一条单列索引**（迁移 `0004`）：`DELETE … WHERE window_start < %s`
  的取数面只有 `window_start` 一列，而既有那条唯一索引是 `(scope, ident, window_start)`
  ⇒ 前缀是 `scope` 不是 `window_start`，用不上它（⛔ 不是"顺手加的索引"）。
"""

from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models

# ⚠️⭐ T2：桶模型**不再从这里 import** —— 一律经 `spec.bucket_model` 取
# （`apps/openapi/credentials.py` 的模块 docstring 写着"为什么必须集中成一束"）。
# ⚠️⭐ T2b：三个公开函数的 `spec` 改成**必填 keyword-only** ⇒ 本模块
# **不再需要** `OPENAPI_SPEC`（原先只为那三处默认值而 import）；import 只剩类型注解用的
# `CredentialSpec`。⛔ 别为了"留个默认值方便"把它加回来 —— 那正是本任务要收掉的形状。
from apps.openapi.credentials import CredentialSpec

#: ⭐ 来源串**解析不成合法 IP** 时，计数桶那一行用的固定 `ident`（见 `ip_ident`）。
#: ⚠️ 它是**显式命名**的一个桶，⛔ 不是"截断 / 压缩"出来的值（截断会把两个不同的伪造值
#: 并成一行，而"到底归并了什么"没人说得清）。
#: ⛔⭐ **现行口径（R1）：这个值「不再作为桶被使用」** —— 来源解析不了时
#: 各入口**直接拒**（`is_unparsable`），⛔ 不读也不写桶。保留常量与 `ip_ident` 的
#: `except` 分支是「只加不删」的纪律：**删它属收口任务**（⛔ 现在别删）。
UNPARSABLE_IDENT = "unparsable"

#: 机会式清理保留几个窗口：`window_start` 早于「当前窗口左端 − 3 个窗口」的行会被删。
#: ⚠️ 取 3 而不是 0 的理由：留一点余量，免得**刚跨过边界**的那次清理把上一个窗口的行删掉
#: ——上一个窗口的行对"这一分钟到底放行了多少"的排障还有用，而保留三个窗口的代价是每行多活两分钟。
RETENTION_WINDOWS = 3

#: ⭐ 三个 `scope` 的分工（桶模型的 `Scope` 枚举，**经 spec 的 `*_scope` 字段**取）——**每一条额度各占一行**：
#:
#: | `scope` | `ident` | 谁用 | 阈值（出处） |
#: |---|---|---|---|
#: | `app` | **纯 `app_key`** | 对外业务端点的兜底闸 | 6000 / 60 秒（`throttling.OPENAPI_FALLBACK_MAX_REQUESTS`） |
#: | `ip` | **纯来源 IP** | 按来源 IP 的失败闸（签名 / Bearer / 取令牌**共用这一行**） | 20 / 60 秒（`authentication.IP_FAILURE_MAX`） |
#: | `ip_token` | **纯来源 IP** | 取令牌端点自己的兜底额度 | 600 / 60 秒（`token_views.TOKEN_FALLBACK_MAX_REQUESTS`） |
#:
#: ⚠️ **`ident` 永远是"一维取值"，⛔ 不许拼前缀 / 复合键**：同一张表里按来源 IP 有**两个**独立阈值
#: （失败闸与取令牌额度），它们靠**不同的 `scope`** 分成两行 —— ⛔ 不是靠 `ident` 上的用途前缀。
#: 理由：`ident` 的语义是「由 `scope` 决定的一维取值」（纯 `app_key` / 纯 IP），拼前缀会让它变成
#: 复合键，而"拼"与"解"的规则迟早会在第二处被复制（`AGENTS.md` §5-E）。见模型 docstring 的改准块。
#:
#: ⚠️ 长度：`ident` 列宽 64；三档里最长的取值是 IPv6 文本形态（≤ 45 字符）⇒ 不会截断。
#: ⛔ 本模块**不**替调用方截断 / 归一 `ident`（那是各调用点自己的口径：`app_key` 是列宽 64 的
#: `CharField`、来源 IP 由 `authentication.client_ip()` 给）。


def _bucket_table_and_constraint(*, spec: CredentialSpec) -> tuple[str, str]:
    """`(表名, 唯一约束名)`，**从模型取**（⛔ 不在这份 SQL 里写死任何一个）。

    ⚠️ 唯一的唯一约束就是 `(scope, ident, window_start)` 那条；这里按**字段元组**认它，
    ⛔ 不按"`constraints` 里的第一个"认 —— 将来有人在模型上加第二条 `UniqueConstraint`
    时，"第一个"会**静默**变成另一条，而 `ON CONFLICT ON CONSTRAINT` 会去撞一个语意不同的
    约束（例如只按 `ident` 唯一 ⇒ 两个维度互相撞）。认不出来时**响亮报错**，不静默兜底。

    ⚠️⭐ **T2**：**哪张表**由 `spec.bucket_model` 决定（字段元组由
    `spec.bucket_unique_fields` 决定）—— 改造前这两个模型名 / 字段名都是这里硬编码的
    ⇒ 打手侧复用时计数会落进**上家侧**的桶表（spec §4 第 5 条）。
    判据（能红）：`tests/test_openapi_rate_limit.py::CredentialSpecDrivenBucketTests`
    —— 拿一份指向**打手侧那张桶表**（表名与约束名都不同）的 spec 调 `consume`，
    行必须落在**那张**表里、且一次都不碰原来那张。
    """
    meta = spec.bucket_model._meta
    expected = tuple(spec.bucket_unique_fields)
    for constraint in meta.constraints:
        if isinstance(constraint, models.UniqueConstraint) and tuple(constraint.fields) == expected:
            return meta.db_table, constraint.name
    raise ImproperlyConfigured(
        f"{spec.bucket_model.__name__} 上找不到字段为 {expected} 的唯一约束："
        "限流计数桶的 UPSERT 依赖它（见 apps/openapi/rate_limit.py 的模块 docstring）"
    )


def _window_start(now: float, window_seconds: int) -> datetime:
    """当前固定窗口的**左端**（UTC 的 aware `datetime`；⛔ 不是该时刻本身）。

    ⚠️ 返回 aware datetime 是必须的：`USE_TZ = True`（`config/settings.py`），
    塞一个 naive datetime 进 `DateTimeField` 会按本地时区解释 ⇒ 窗口边界在 UTC 与
    Asia/Shanghai 之间漂 8 小时，而症状是"限额忽然松/紧了一档"（很难查）。
    """
    epoch_seconds = int(now // window_seconds) * window_seconds
    return datetime.fromtimestamp(epoch_seconds, tz=dt_timezone.utc)


def _retry_after(now: float, window_seconds: int) -> int:
    """还要等几秒才跨过当前窗口（`Retry-After` 用；⛔ 必须是 ≥ 1 的整数）。

    ⚠️ `max(1, …)` 挡的是"刚好落在窗口边界上算出 0"：`Retry-After: 0` 等于叫对方立刻重试，
    那正是要避免的形状（同族理由见 `throttling._seconds_until_next_window` 的 docstring）。
    """
    remaining = window_seconds - (now % window_seconds)
    return max(1, int(remaining))


def _prune(cursor, *, table: str, cutoff: datetime) -> None:
    """删掉 `window_start` 早于 `cutoff` 的行（**同一条游标**里跑，条件只有一个明确时刻）。

    ⚠️ 这是本模块里**唯一**的 `DELETE`；调用点只有 `consume()` 的「本窗口第一次命中」那一支。
    """
    cursor.execute(f'DELETE FROM "{table}" WHERE "window_start" < %s', (cutoff,))


def _parsed_ip(source_ip: str):
    """把来源串解析成 `ipaddress` 对象；解析不了 ⇒ `None`（本模块里**唯一**的解析点）。

    ⚠️ 单独抽出来是为了让 `ip_ident()`（规范化）与 `is_unparsable()`（fail-closed 判据）
    **用同一个解析**：两处各写一遍的话，改了其中一处（例如放宽成"允许端口后缀"）
    另一处就静默地不同口径（`AGENTS.md` §5-E）。
    ⚠️ 只捕 `ValueError`：入参**恒为 `str`**（`authentication.client_ip()` 经 `_metadata_str`
    归一，非字符串一律回空串）⇒ 为不可达的 `TypeError` 写一条分支只会让读者以为它承重。
    """
    try:
        return ipaddress.ip_address(source_ip)
    except ValueError:
        return None


def is_unparsable(source_ip: str) -> bool:
    """来源串**解析不成合法 IP** ⇒ `True`（这就是 fail-closed 那条判据本身）。

    ## ⭐ 为什么要判它、判了之后做什么（R1 的裁决）

    来源串解析不了 ⇒ **各入口直接拒**（`authentication` 的失败闸三个入口、
    取令牌端点的兜底额度那一档），⛔ **既不读也不写任何计数桶**。依据是仓库**既有**口径：
    `authentication._ip_allowed()` 对不可解析来源本来就是 **fail-closed**（回"不在白名单内"）
    ⇒ 计数这一层不该给出一个更宽的口径。

    ## ⛔ 为什么不能"归并到一个占位桶"（R1 之前那半天的做法，已改准）

    归并出来的那个桶是**共享毒桶**：任何能伪造来源头的路径（伪造 `X-Real-IP` / XFF 最右段）
    只要往同一个 `ident` 上灌满阈值，就能让**所有同类**（同样不可解析的）来源的请求
    整片被挡 —— 而受害者与攻击者之间**没有任何关系**。那是"互相伤害"，
    ⛔ 不是限流。fail-closed 之后：**每一次**不可解析的请求都当场被拒（不受别人灌量影响），
    且计数表里**一行都不产生**。

    ## 判据（两条都要能红，见 `tests/test_openapi_rate_limit.py::UnparsableSourceIpTests`）

    ① 垃圾来源 ⇒ 被拒（按各路径既有契约的形状）；
    ② 计数表里**没有**任何 `ident == UNPARSABLE_IDENT` 的行（那条路径根本没写桶）；
    ③ ⭐ 对照：先用一个垃圾来源灌 N 条，**另一个不同**的垃圾来源的答复**与灌之前逐字相同**
    （旧实现下这条红：共享桶被灌满后它从"正常拒绝"变成"被闸挡住"）。
    """
    return _parsed_ip(source_ip) is None


def ip_ident(source_ip: str) -> str:
    """把来源串规范成计数桶的 `ident`：**合法 IP 的文本形态**；解析不了 ⇒ `UNPARSABLE_IDENT`。

    ## 为什么必须有它（⛔ 不是"防御性写法当承重逻辑"）

    判据 = `tests/test_openapi_rate_limit.py::UnparsableSourceIpTests` **能红**（弄红读数见 T3 报告），
    而且这条 500 是**本次改动自己引入的回归**：旧实现把来源串拼进 `LocMemCache` 的**键**
    （键没有长度限制），T3 之后它进的是 `OpenApiRateBucket.ident`（**`varchar(64)`**）
    ⇒ 一个 500 字符的伪造 `X-Real-IP` 会让 `INSERT` 抛 `DataError` ⇒ **500**。
    而 `authentication.client_ip()` 的 docstring **自己写着**它「只回答"用哪个 IP 判"，
    ⛔ **不管**它能不能解析成合法 IP」⇒ 未解析的任意长度串确实会走到这里
    （`AGENTS.md` §5-E：请求来源的值，进类型化的列之前一律过一遍；判据是
    「**这类请求**还会不会 500」，⛔ 不是"这个字段"）。

    ## 口径（两步，都能被用例证伪）

    1. `ipaddress.ip_address(...)` 解析成功 ⇒ 用 `str(parsed)`
       ⇒ ⭐ 顺带把**同一地址的多种写法**归成**一个桶**（`::1` 与 `0:0:0:0:0:0:0:1`，
       以及 IPv4-mapped 那类形态，由标准库统一）。
    2. 解析失败（空串 / 超长 / 垃圾）⇒ `UNPARSABLE_IDENT`（⛔ **不截断**，理由见那个常量）。
       ⚠️ 只捕 `ValueError`：入参**恒为 `str`**（`authentication.client_ip()` 经 `_metadata_str`
       归一，非字符串一律回空串）⇒ 为不可达的 `TypeError` 写一条分支只会让读者以为它承重
       （`AGENTS.md` §5-E）。

    ## ⚠️ 它只作用于**计数桶**，⛔ 不动任何准入判据

    `client_ip()`（取哪个 IP 判）与 `_ip_allowed()`（白名单放行）**一行都没改** ——
    "来源解析不了 ⇒ 不在白名单内"（fail-closed）那条判据仍由 `_ip_allowed` 自己负责
    （它有自己的一次解析）。两处**各判各的**是**有意**的：改白名单那条要单独裁决。

    ## ⚠️ 为什么"解析不了的来源共用一个桶"不算帮攻击者归并

    这个 `ident` 只决定**计数落在哪一行**，⛔ 不参与任何"放不放行"的判定：两个解析不了的来源
    共用一行，只会让它们的**失败次数相加**（更早触发那条闸），⛔ 不会让任何一个来源绕过
    认证 / 白名单 / 限额 —— 那些判据一个字都没读这个值。

    ## ⛔⭐ 现行口径（R1）：上面那段「不算帮攻击者归并」的结论**已被推翻**
    ## （原句逐字保留在上面），第 2 步那条 `UNPARSABLE_IDENT` 分支**自 R1 起不再被用作桶**

    实测复核的结论：共用一行**就是**帮攻击者归并 —— 任何能伪造来源头的路径往同一个 `ident`
    灌满阈值，就能把**所有**同样不可解析的来源一起挡掉（受害者与攻击者毫无关系）。
    ⇒ 现在**每个入口在解析失败时直接拒**（`is_unparsable()`，⛔ 不读也不写桶），
    本函数那条 `except` 分支与 `UNPARSABLE_IDENT` 因此**不再是任何请求的落点**
    （保留原文与常量是「只加不删」的纪律：删它属收口任务）。
    ⚠️ 本函数的**第 1 步**（合法 IP 的规范化）仍然承重：三个计数入口与取令牌端点都用它。
    """
    parsed = _parsed_ip(source_ip)
    return str(parsed) if parsed is not None else UNPARSABLE_IDENT


def consume(
    *,
    scope: str,
    ident: str,
    limit: int,
    window_seconds: int,
    now: float | None = None,
    spec: CredentialSpec,
) -> tuple[bool, int]:
    """**记一次**并回答「放不放行」：`(是否放行, 还要等几秒)`。

    | 参数 | 含义 |
    |---|---|
    | `scope` | 计数维度（桶模型 `Scope` 的取值：`app` / `ip` / `ip_token`） |
    | `ident` | 该维度的取值（`app` 档 = `app_key`；`ip` 档 = 来源 IP） |
    | `limit` | 本窗口放行多少次（⭐ `count <= limit` ⇒ 放行，见模块 docstring 的边界语义） |
    | `window_seconds` | 固定窗口长度（秒） |
    | `now` | **epoch 秒**（`time.time()` 的形状）；`None` ⇒ 现取。⚠️ 给测试与 `timer` 留的口子 |
    | `spec` | 哪一族（决定**写哪张桶表**，见 `_bucket_table_and_constraint`）。⭐⭐ **T2b：必填 keyword-only**（⛔ 默认值已去掉） |

    ⛔ 第二个返回值**只**给 `Retry-After` 用：它是"还要等几秒"，≥ 1 的整数，**即使放行时也算**
    （调用方按需忽略）。⚠️ 被挡时它**不是**"限流窗口还剩多久"之外的东西 —— 本模块不区分
    "第几次超限"，也⛔ 不因此给出越来越长的等待（对齐旧实现 `wait()` 的形状）。

    ⚠️⭐ **T2**：`spec` 决定桶表（表名 + 唯一约束名，见
    `_bucket_table_and_constraint`）。
    ⭐⭐ **T2b：`spec` 改成必填 keyword-only，⛔ 不再有默认值** ——
    漏传 = **调用期就 `TypeError`**。改之前默认值是 `OPENAPI_SPEC`，于是"打手侧漏传"
    会**静默**把计数写进**上家侧**那张桶表（spec §4 第 5 条 / §4.1-1 的"静默串台面"：
    两族共用一行计数 ⇒ 一边打满把另一边同一 `ident` 一起挡掉）。
    ⚠️ **行为逐字不变**：生产调用点本来就显式传 `spec`，而那就是 `OPENAPI_SPEC`
    ⇒ 桶表 / scope / 阈值 / 窗口与改前同一个值；既有用例只是"把原先由默认值兜住的
    那件事写出来"（⛔ 断言一条未改）。
    """
    moment = time.time() if now is None else now
    window_start = _window_start(moment, window_seconds)
    table, constraint = _bucket_table_and_constraint(spec=spec)
    # ⚠️ `ON CONFLICT ON CONSTRAINT` 用的是**约束名**（从模型取），不是列列表：
    # 列列表与约束漂了也能跑（撞的是另一条索引），而按名字撞 ⇒ 名字不在就**报错**。
    sql = (
        f'INSERT INTO "{table}" ("scope", "ident", "window_start", "count") '
        f"VALUES (%s, %s, %s, 1) "
        f'ON CONFLICT ON CONSTRAINT "{constraint}" DO UPDATE '
        f'SET "count" = "{table}"."count" + 1 '
        f'RETURNING "count"'
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, (scope, ident, window_start))
        row = cursor.fetchone()
        count = int(row[0])
        if count == 1:
            # 本窗口第一次命中 ⇒ 顺手清理过期行（理由与索引见模块 docstring）。
            cutoff = window_start - timedelta(seconds=RETENTION_WINDOWS * window_seconds)
            _prune(cursor, table=table, cutoff=cutoff)
    return count <= limit, _retry_after(moment, window_seconds)


def current_count(
    *,
    scope: str,
    ident: str,
    window_seconds: int,
    now: float | None = None,
    spec: CredentialSpec,
) -> int:
    """**只读**：当前窗口已累计的计数（⛔ 不计数、⛔ 不建行、⛔ 不清理）。没有行 ⇒ `0`。

    ## ⭐ 为什么闸门需要它，而⛔ 不能拿 `consume()` 顶替

    "先看一眼要不要直接拒"这件事在**两个**地方要（`authentication._failure_count` 与取令牌
    端点），它们的语义都是**按失败次数**分桶：只有**失败**才加一。
    若把这一次"看一眼"也写成 `consume()`，那么**每一次请求**（包括完全合法、成功的那些）
    都会把失败计数加一 ⇒ 闸门从"猜了多少次"变成"来了多少次"：一个正常调用方连打 21 次
    就会被拉黑一分钟。那是**行为口径变了**，而任务书对旧路径的要求恰恰是
    「文案 / 401 / 阈值一个都不许变」⇒ 读与写在这里必须分开。
    ⚠️ 代价（如实登记）：多一次 `SELECT`（被挡的那次请求会先读一次、再返回）。
    它换掉的是"合法调用方被自己的成功请求拖黑"这条更贵的错。

    ⚠️⭐ **T2**：`spec` 决定**读哪张桶表**（与 `consume` 同一个口径；
    两边必须看同一张表，否则"先看一眼"读到的计数与实际写入的计数不是一件事）。
    ⭐⭐ **T2b：`spec` 必填 keyword-only**（⛔ 默认值已去掉；理由与 `consume`
    那条逐字相同：漏传 ⇒ 静默读**上家侧**那张表，闸门会按别人的计数判决）。
    """
    moment = time.time() if now is None else now
    window_start = _window_start(moment, window_seconds)
    table, _constraint = _bucket_table_and_constraint(spec=spec)
    with connection.cursor() as cursor:
        cursor.execute(
            f'SELECT "count" FROM "{table}" '
            f'WHERE "scope" = %s AND "ident" = %s AND "window_start" = %s',
            (scope, ident, window_start),
        )
        row = cursor.fetchone()
    return int(row[0]) if row is not None else 0
