"""开放平台**写端点的幂等入口**（唯一；设计文档 `docs/specs/2026-10-08-open-platform-v2-parity-design.md` §6.3）。

## 这里是什么

一个**装饰器**（`idempotent`），把它套在写端点的方法上（`post` / `put`）：

1. 读请求头 `Idempotency-Key`（契约长度 **8–128**）；**缺失或不合规 ⇒ 业务码 `1000`**
   （⛔ 不"默默放行"——放行就等于同一笔业务被执行两次）；
2. 键在我方台账里的**四态**（逐条判据见 `_run` 的 docstring）：
   新键 ⇒ 执行副作用 + 落响应快照；同键同请求 + 快照非空 ⇒ **回放**；同键异请求 ⇒ `1000`；
   同键但快照为空（上一次还在执行 / 崩了）⇒ `1001`（CONFLICT）+ 一句人话。

## ⭐ 存储与事务：占位行与业务写入**同一个事务**

新键那一条先建一行**占位行**（`response_snapshot` 为空），再在**同一个** `transaction.atomic()`
里调业务处理器：

- 业务**抛异常**（含 DRF 的 `NotFound` / `ValidationError`）⇒ 整个事务回滚 ⇒ **占位行跟着消失**；
- 业务**正常返回但信封 `code != 0`**（可预期的业务失败，如非法状态流转 / 已结账拒绝改价）⇒
  本模块 `transaction.set_rollback(True)` **再** return（`AGENTS.md` §5-E / E-0022：
  ⛔ 不许用裸 `return` 退出补偿路径——那会把占位行提交掉，让同一个键**永久**卡在
  「正在处理中」）。
  ⇒ 两条都是「业务失败 ⇒ 这个键**下次可以重试**」，判据打在**库里的行数**上
  （`tests/test_openapi_order_write.py`）。
- ⚠️ 只有**成功**（信封 `code == 0`）才写快照 ⇒ 「有快照」与「这一笔真的做成了」是同一件事，
  回放**不可能**把一次失败当成结果发出去。

## ⚠️ 并发：第二笔撞唯一约束 ⇒ **必须用嵌套 atomic 拿 savepoint**

`OpenApiIdempotency` 的唯一性是 `uniq_openapi_idem_app_key = (app, key)`（具名约束）。
两个请求同时用一个新键时，后到的 `INSERT` 会抛 `IntegrityError`；而**在 `atomic()` 块里裸捕它**
会让整个事务进入「已损坏」状态（PostgreSQL 侧的当前事务被中止）⇒ 后面任何查询都抛
`TransactionManagementError`。做法照既有先例（`apps/business/jidanpu_upstream_pics.py`
那处「savepoint + `IntegrityError` 幂等」）：**内层再套一个 `atomic()` = savepoint**，
捕到 `IntegrityError` 只回滚这笔 `INSERT`，外层事务仍然可用。
⇒ 判据：并发第二笔回 `1001`（⛔ 不是 500，也不是 `TransactionManagementError`）。

## ⚠️ 快照里**不许**出现凭据类字段（D-0201 不放松的那一格）

`app_secret` 可以出现在响应体里（业务方 的裁定），⛔ 但**不许**落进任何**长期留存**
的地方——幂等快照就是一处长期留存。`_assert_no_credentials` 是这条纪律的**可执行**形式
（递归查键名，命中就**响亮**抛 `ValueError`，⛔ 不静默脱敏：那会掩盖一个真的设计错误）。

## 老行会累积 ⇒ 机会式清理

本表**没有 TTL**（DB 行不会自己过期）⇒ 每次**建一行新行**时顺手清一次：
删掉 `created_at` 早于「**明确算出的**保留期左端」的行，**一次最多 500 行**。
- ⛔ 清理条件里**只有**这一个时间判据（没有"按大小 / 按前缀 / 再兜一层时间"这类模糊条件，
  `AGENTS.md` §5-A 对破坏性动作的要求）；
- ⚠️ **为什么要给 `created_at` 加一条单列索引**（迁移 `0005`）：这条 `DELETE` 的取数面只有
  `created_at` 一列，而唯一约束那条索引的前缀是 `app`，替不了它。

## ⭐⭐ 现行口径（T6）—— 「业务码 ≠ 0 ⇒ 回滚占位行」这条对**录单**是错的

原口径（上面「存储与事务」那一节，原文逐字保留）把「可预期的业务失败」一律当成
「**一行都没写**」⇒ `transaction.set_rollback(True)` 再 return。T5 的两条端点确实如此
（改状态 / 改发单价被拒时领域核心一行都没写）。

⚠️ **录单不是**：`1022`（单已建好、指派没成）与 `1023`（单已建好、抛单没成）这两个码
**非 0 而订单已经落库**（那是业务方明裁的形状：指派/抛单失败**不许**带走订单）。
在外面套一层 `_run` 的事务时，那两档如果照样 `set_rollback(True)`，会把**已建的单一起抹掉**，
而响应仍然写着「单已建好（单号 X）」——一句**假话**（E-0063 同族），且调用方重试会**再建一张单**。
（实测 `django/db/transaction.py::Atomic.__exit__`：带 savepoint 的块回滚**不会**把
`needs_rollback` 留给外层 ⇒ 污染只可能来自 `_run` 这一句，也就是本条正是要修的东西。）

**落地**：`_run` 只在「非 0 码 **且** handler 没有声明『副作用已落库』」时回滚；
声明方式是显式调用 `mark_side_effect(request)`（见它的 docstring）。
- ⛔ **不是放宽**：T5 的两条端点**一个字未改**（它们不调那个函数 ⇒ 判据与读数逐字不变）；
- ⛔ **不是"谁都能声明"**：`mark_side_effect` 只在**真的写成了业务对象之后**调
  （今天只有录单那一个调用点，且它拿 `IntakeOutcome.order is not None` 当判据）。

## ⭐⭐ T2—— 本模块**收 spec 当参数**（⛔ 行为逐字节不变）

改造前：台账模型 `OpenApiIdempotency` 在四处被**硬编码**（`_find_existing` / `_prune` /
`MAX_PATH_CHARS` 与那一句文案 / `source_key` 前缀那一族）。今天一律**经 `CredentialSpec` 取**
（`apps/openapi/credentials.py`）：

| 收 spec 的地方 | 取什么 |
|---|---|
| `idempotent(spec=…)` → `_run(request, handler, spec=…)` | **写端点显式声明**自己哪一族（⛔ 必填 keyword-only，不在运行期猜） |
| `_find_existing` / `_prune` / `seen_source_response` / `remember_source` | `spec.idem_model`（⛔ 必填，不给默认值：漏传的后果是**静默**去读 / 写另一族的台账） |
| `source_key` | `spec.source_key_prefix` |
| `_overlong_path_response` | `spec.max_path_chars` / `spec.path_too_long_msg` |

⭐⭐ **T2：`@idempotent` 的 `spec` 由写端点显式声明（不运行期猜）**

改之前这一格是 `_run(request, handler)` + `spec_of(request)`（从认证态推）。问题：那是**同一类
错误**（忘了说自己是哪一族）在两个地方的两种待遇 —— `_find_existing` 那几处是**必填**
（漏传 ⇒ `TypeError`），而 `spec_of` 那一处**静默回退**成上家侧。⇒ 现在 6 个既有调用点
一律写成 `@idempotent(spec=OPENAPI_SPEC)`，⛔ 漏传是构造期的 `TypeError`
（判据：`tests/test_openapi_order_write.py::CredentialSpecDrivenLedgerTests::
test_the_idempotent_decorator_requires_an_explicit_spec`）。
⚠️ **`spec_of` 仍然在职**（`order_create_views` 那条"同一来源单号"去重按请求取），
它自己那条静默回退**也已改成响亮失败**（见 `apps/openapi/credentials.py`）。

⚠️ **模块级别名保留**（`MAX_PATH_CHARS` / `PATH_TOO_LONG_MSG` / `SOURCE_KEY_PREFIX`）：
它们是既有用例 import 的名字（`tests/test_openapi_order_images.py` 那条"闸与列宽对拍"的
用例按 `MAX_PATH_CHARS` 比对），值是 **spec 派生**的 ⇒ ⛔ 判据一条未放宽、数字只有一处出处。
"""
from __future__ import annotations

import hashlib
from datetime import timedelta
from functools import wraps

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.response import Response

from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
# ⚠️⭐ T2：台账模型**不再从这里 import** —— 一律经 `spec.idem_model` 取
# （理由与判据见 `apps/openapi/credentials.py` 的模块 docstring：漏一处的症状是
# 「既有网全绿、线上把另一族的台账当自己的用」）。
from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec

#: 幂等键的请求头名（契约逐字，设计文档 §6.3）。
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: 键的长度契约（**含两端**）。⚠️ 与模型那一列（`max_length=191`）**不是**同一个口径：
#: 列宽是"装得下"，这两个数字是**对外契约**（改它们要同一次改文档与用例）。
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 128

#: 幂等台账的保留期（天）。超过它的行由 `_prune()` 删掉。
RETENTION_DAYS = 30

#: `_prune()` 一次最多删多少行（⛔ 不许无上限地删：这个清理挂在**请求路径**上，
#: 无上限的话积压很多时一次请求就会长时间持锁）。
MAX_PRUNE_ROWS = 500

#: 占位行写进 `status_code` 列的那个值。⚠️ 它是**占位值、不是结果**：可回放的行必须
#: `response_snapshot` **非空**（见 `_run` 的第四态），所以这个值**永远**不会被回放出去。
PLACEHOLDER_STATUS_CODE = 200

#: 四态里各档的文案（对外契约的一部分；T10 的接入文档逐条列出）。
#: ⚠️ 缺失那一句是设计文档 §6.3 的**逐字**要求（「缺少幂等键」）。
MISSING_KEY_MSG = "缺少幂等键"
KEY_LENGTH_MSG = f"幂等键长度必须是 {MIN_KEY_LENGTH}–{MAX_KEY_LENGTH} 个字符"
DIFFERENT_REQUEST_MSG = "同一个幂等键配了不同的请求体"
IN_PROGRESS_MSG = "同一个幂等键的请求正在处理中"

#: handler 用来声明「副作用**已经落库**」的那个请求属性名（**唯一出处**：⛔ 别在别处再拼一次）。
#: 见 `mark_side_effect` 与模块 docstring 末尾的 T6 改准块。
SIDE_EFFECT_FLAG = "_openapi_idempotent_side_effect_done"

#: 「同一应用 + 同一来源单号」去重那颗**合成幂等键**的前缀（设计文档 §6.3）。
#: ⚠️ **唯一出处**：拼这颗键的地方只有 `source_key` 一处（⛔ 别在视图里再拼一次——
#: 那种"两处拼同一颗键"的写法迟早漂移，而漂移的后果是去重静默失效）。
#: 长度：`source_order_no` 的列宽是 128 ⇒ 最长 15 + 128 = 143 ≤ `key` 列的 191 ✓。
#: ⚠️⭐ T2：**值取自 spec**（`OPENAPI_SPEC.source_key_prefix`）——
#: 它是"凭据族"的一件东西：两族共用同一颗键的话，一族的来源单号会在另一族那边**命中回放**。
SOURCE_KEY_PREFIX = OPENAPI_SPEC.source_key_prefix

#: 幂等台账 `path` 那一列的**容量**（字符数）。
#: ⛔ **从模型自己的 `_meta` 取**（⛔ 不硬编码 191）—— 闸与列宽**同源**：
#: 列宽改了（或模型换了列类型 / 长度）这条闸**跟着改**，不会留下"注释说 191、列已经 255"那种
#: 静默错位（先例：`_is_shared_screenshot_key` 的列宽也是从 `_meta` 取的）。
#: ⚠️⭐ T2：取法从「直接读 `OpenApiIdempotency._meta`」改成
#: 「读 **spec** 的 `max_path_chars`」（spec 再按 `idem_model` + `idem_path_field` 从
#: 模型 `_meta` 取）—— ⛔ **读数不变**（同一列、同一个 `max_length`），
#: 变的只是"这一族用哪张表 / 哪一列"由 spec 决定。
MAX_PATH_CHARS = OPENAPI_SPEC.max_path_chars

#: 请求路径超过 `path` 列宽时的那句话（**对外契约的一部分**，T11 的接入文档逐条列出）。
#: ⚠️ 数字**拼自** `MAX_PATH_CHARS`（⛔ 不在文案里再写一次字面量：两处数字会漂）。
PATH_TOO_LONG_MSG = OPENAPI_SPEC.path_too_long_msg


def source_key(source_order_no: str, *, spec: CredentialSpec) -> str:
    """来源单号去重的**合成幂等键**（唯一出处，见 `SOURCE_KEY_PREFIX` 的注释）。

    ⚠️ T2：前缀**经 spec 取**（⛔ 不再是这一族的私有字面量）。
    ⭐⭐ **T2b：`spec` 必填 keyword-only**（⛔ 默认值已去掉）——
    漏传会让一族的来源单号**用另一族的前缀**去查台账（spec §4 第 4 条
    "两套来源单号去重键串台"），而症状是**静默回放别人的响应**。
    既有调用点本来就显式传 `spec`（`seen_source_response` / `remember_source`）
    ⇒ 行为逐字不变，⛔ 断言一条未改。
    """
    return f"{spec.source_key_prefix}{source_order_no}"


def mark_side_effect(request) -> None:
    """由 handler 在**副作用真的落库之后**调用：告诉幂等入口「这一笔做成了」。

    ## ⭐ 什么时候该标记（判据**一条**，⛔ 不是"失败了就标记"）

    **业务写入已经落库、且这一次请求不打算把它回滚** ⇒ 标记。
    逐条说清它**不是**什么：

    - ⛔ **不是「信封 `code != 0` 就标记」**：一次**什么都没写**的拒绝
      （缺参 / 闸门 / 非法流转）也回非 0 码，那一档**必须**让占位行跟着回滚
      （否则同一个键**永远重放那个失败**、再也不能重试——T5 的既有契约就破了）。
    - ⛔ **不是「handler 想保住占位行就标记」**：占位行不是业务对象；
      标记的对象是**业务写入**（这里就是那张订单）。
    - ⛔ **不许按信封反推**：录单那条路上 `code == 0` 与"写成了"**不是**同一件事
      ——`1022` / `1023` 是「单已建好、后续动作没成」。判据取**领域核心给的业务对象**
      （`apps/business/order_intake.IntakeOutcome.order is not None`）。

    ## 为什么需要它（完整推导见模块 docstring 末尾的 T6 改准块）

    `_run` 的既有口径是「业务码非 0 ⇒ 连占位行一起回滚」。录单的 `1022` / `1023`
    **非 0 而订单已经落库** ⇒ 照那条办会把那张单一起回滚掉，而响应还宣称
    「单已建好（单号 X）」——一句**假话**，且调用方重试会**再建一张单**。

    ## ⛔⭐ 调用点（评审改准：实为**四个**；
    ## 原「调用点只有一个」的更早原句在此一并留痕）

    ① `apps/openapi/api/order_create_views.py::OrderCreateMixin.post`（T6 落）；
    ② ⭐ `apps/openapi/api/order_image_views.py::OrderImageClaimMixin.post`（T10a 落）：
       进度图**认领**的"拒收即清理"四档（超限 / 0 字节 / 超 18 MiB / 魔数认不出）会
       **删行 + 删对象**，那两笔删除是**要保留的业务写入**（留着那一行会让订单张数永久停在 21）
       ⇒ 必须声明，否则 `_run` 会把它们回滚，而 `storage.delete` 不是事务性的
       ⇒ 库里留下一条指着已不存在对象的 `pending` 行。
       ⚠️ 判据是领域结果上的 `writes_kept`（`ScreenshotOutcome`），⛔ 不是"失败了就标记"：
       同一条端点上**不写库**的两档（没有待认领的行 / 文件还没传上来）**不**声明。
    ③ ⭐ `apps/takerapi/api/order_images_views.py` 的进度图认领 `post`（打手侧同族端点，
       落地时漏登、评审补登）：与 ② 同一条"拒收即清理"判据
       （领域结果上的 `writes_kept`），⛔ 同样不是"失败了就标记"。
    ④ ⭐ `apps/takerapi/api/hall_views.py::mirror_claim_response`（已落，
       业务方报障修法一）：鸡蛋铺镜像接单命中停用 / 缺失绑定的僵尸卡 ⇒ 领域层
       `listed → gone` **拆雷**（要保留的业务写入）后回 `1012`。判据 = 领域层在异常上
       带的 `defused` 标记（`ClaimError(message, code, defused=True)`，仅拆雷真的落库时
       置位），⛔ 不是"1012 就标记"——非拆雷的 1012 一行都没写，必须保持同键可重试。
    """
    setattr(request, SIDE_EFFECT_FLAG, True)


def _side_effect_done(request) -> bool:
    """handler 有没有声明过「副作用已落库」（见 `mark_side_effect`）。"""
    return bool(getattr(request, SIDE_EFFECT_FLAG, False))


def request_digest(request) -> str:
    """请求体**字节**的 sha256 hex（64 字符）——`request_hash` 列的**唯一**计算处。

    ⚠️ 抽出来给两个调用方用：`_run`（占位行那一笔）与 `remember_source`（合成键那一笔）。
    两份各写一遍就会在"改口径"时只改一处（`AGENTS.md` §5-E）。
    """
    return hashlib.sha256(request.body).hexdigest()

#: 快照里**不许**出现的键名（小写比较，递归到任意深度）。
#: ⚠️ 这是一份**显式枚举**的清单：⛔ 不用"键名里含某个词"那种模糊判据
#: （`AGENTS.md` §5-G：豁免 / 拦截都必须显式枚举）。本 app 今天的凭据只有 `app_secret`
#: 一个（`app_key` 按设计**不是**机密，见 `apps/openapi/models.py` 的类 docstring），
#: 其余几个是同类东西的通用名字，留给将来。
_CREDENTIAL_KEYS = frozenset(
    {"app_secret", "secret", "client_secret", "password", "access_token", "refresh_token"}
)


def _assert_no_credentials(body, *, where="快照") -> None:
    """递归检查 `body` 的**键名**：命中凭据类字段 ⇒ 抛 `ValueError`（响亮，⛔ 不静默脱敏）。

    ⚠️ 为什么是"抛"而不是"删掉那个键再存"：这一档只可能来自**我们自己的设计错误**
    （某个端点把凭据装配进了响应体）⇒ 那是**未预期**的错误，就该响亮地炸
    （`AGENTS.md` §5-E：把未预期错误伪装成业务结果会让监控失明）。
    """
    if isinstance(body, dict):
        for name, value in body.items():
            if str(name).lower() in _CREDENTIAL_KEYS:
                raise ValueError(
                    f"幂等{where}里出现了凭据类字段 {name!r}："
                    "密钥类字段不许落进任何长期留存（见 apps/openapi/models.py 的纪律）"
                )
            _assert_no_credentials(value, where=where)
    elif isinstance(body, (list, tuple)):
        for item in body:
            _assert_no_credentials(item, where=where)


def _find_existing(*, app, key, spec: CredentialSpec):
    """取这条键的现有台账行（没有 ⇒ `None`）。

    ⚠️ 单独抽一个函数**不是为了好看**：它让"并发那一支"能被用例**打桩**
    （把这次查找打桩成"没找到"，就能真的走到 `INSERT` 撞唯一约束那条路上，
    见 `tests/test_openapi_order_write.py` 的并发用例）。`mock.patch` 要打在**被查找的模块**
    （本模块）上，`AGENTS.md` §5-E / E-0029。

    ⚠️⭐ T2：**查哪张台账由 `spec.idem_model` 决定**（`spec` 是必填的
    keyword-only 参数，⛔ **故意不给默认值** —— 这一处漏传的后果是 spec §4 第 1 条那条
    「按 pk 命中**另一族**的同一号行 ⇒ 回放别人的响应」，那是**静默**的
    ⇒ 宁可响亮地 `TypeError`）。
    """
    return spec.idem_model.objects.filter(app=app, key=key).first()


def _prune(*, spec: CredentialSpec, now=None) -> int:
    """机会式清理：删掉 `created_at` 早于「现在 − `RETENTION_DAYS` 天」的行，**最多 500 行**。

    返回真正删掉的行数（给用例当读数用；⛔ 生产调用方不读它）。

    ## 判据（两条，都能被用例独立证伪）

    1. **时间判据只有一个**：`created_at < 明确算出的时刻`（`now - RETENTION_DAYS 天`）；
    2. **一次最多 `MAX_PRUNE_ROWS` 行**：先按 `created_at` 升序取最多 500 个主键，再按主键删
       ⇒ 积压时不会一次删掉整表（⚠️ `QuerySet.delete()` **不接受**切片，
       Django 会抛 `TypeError: Cannot use 'limit' or 'offset' with delete.`
       ⇒ 分两步：取主键 → 按主键删）。

    ⚠️⭐ T2：**清哪张台账由 `spec.idem_model` 决定**（`spec` 必填，
    ⛔ 不给默认值的理由同 `_find_existing`：漏传 ⇒ 静默去删**另一族**的老行）。
    """
    cutoff = (timezone.now() if now is None else now) - timedelta(days=RETENTION_DAYS)
    idem_model = spec.idem_model
    stale_ids = list(
        idem_model.objects.filter(created_at__lt=cutoff)
        .order_by("created_at")
        .values_list("pk", flat=True)[:MAX_PRUNE_ROWS]
    )
    if not stale_ids:
        return 0
    deleted, _detail = idem_model.objects.filter(pk__in=stale_ids).delete()
    return deleted


def _invalid_key_response(request) -> Response | None:
    """键合规 ⇒ `None`；缺失 / 超短 / 超长 ⇒ 一个 `1000` 信封。

    ⚠️ 只有**长度**这一个判据（设计文档 §6.3 的契约就是长度）：字符集由调用方自己定，
    ⛔ 不在这里加"必须像 UUID"之类契约里没有的规则。
    """
    key = request.headers.get(IDEMPOTENCY_HEADER, "")
    if key == "":
        return api_response(data=None, msg=MISSING_KEY_MSG, code=ErrorCode.VALIDATION)
    if not MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH:
        return api_response(data=None, msg=KEY_LENGTH_MSG, code=ErrorCode.VALIDATION)
    return None


def _overlong_path_response(request, spec: CredentialSpec) -> Response | None:
    """路径装得进 `path` 那一列 ⇒ `None`；装不进 ⇒ 一个 `1000` 信封。

    ## 为什么必须有一道这样的闸（T10a 修复；**既有缺陷**，⛔ 不是 T10a 引入的）

    本入口把 `request.path` **原样**写进幂等台账的 `path` 列（`varchar`，列宽见
    `spec.max_path_chars`），而路径里那一段 `order_no` 是**请求给的、没有长度界**
    （共享转换器只排控制字符）⇒ 超长路径会在**占位行 `INSERT`** 那一步撞
    `StringDataRightTruncation (value too long for type character varying(191))`
    ⇒ **HTTP 500**（值来自请求、进了类型化的列 —— `AGENTS.md` §5-E / E-0010 那一类，
    判据是「**这类请求**还会不会 500」；同族先例 = 本仓先前那次「超长 `X-Real-IP`
    撞限流计数列宽」，当时定的口径是**"生产走不到"不是不挡的理由**）。

    ⚠️ **实测（T10a 定值）**：修之前，在 **T5 既有**的
    `POST /api/open/v1/orders/<order_no>/status/` 上，`order_no` 长 100 / 150 字符 ⇒
    `200 + 1002`；**170 / 200 / 500 字符 ⇒ HTTP 500**（阈值 = `len(path)` > 列宽）。
    ⇒ 它当时影响 **T5 / T6 / T7 三批的全部 `@idempotent` 端点**（同一个入口）。

    ## 位置与口径

    - 排在 `_run` 的**第一道**（键的长度判据之后、**任何库读写之前**）
      ⇒ 超长路径连一次查询都不发（也就不会有半截状态）；
    - 答复是**业务码 `1000`** + 一句人话（它是"参数不合法"这一档，⛔ 不是 5xx、
      ⛔ 不是"默默截断后放行"——截断会让幂等键的 `path` 与实际请求不是同一件事）；
    - ⛔ **不新增模型列、不改列宽**（那是迁移；这里只是让越界的请求在**进库之前**被拒）。
    """
    if len(request.path) <= spec.max_path_chars:
        return None
    return api_response(
        data=None, msg=spec.path_too_long_msg, code=ErrorCode.VALIDATION
    )


def _replay(row) -> Response:
    """把台账里的那一笔**逐字**重发（⛔ 不重跑业务处理器）。

    ⚠️ 回放**不直接**返回存下来的那个 dict，而是经**唯一的信封构造点**（`api_response`）
    用快照里的三个字段重建——信封形状只允许有一处定义（`apps/core/envelope.py` 的模块 docstring）。
    两者在今天**逐字等价**：快照就是 `api_response()` 产出的那三个键。

    ⚠️ 证明力边界（实测登记）：`response_snapshot` 是 **JSONB** 列，
    而 PostgreSQL 的 JSONB **不保留键的插入顺序** ⇒ 两次响应的**字节序可能不同**
    （键序被库归一化）。⇒ 「逐字相同」的判据打在**解析后的载荷**上（`code` / `msg` / `data`
    三个键的值逐字相同），⛔ 不是"两次 `response.content` 字节相等"。
    """
    body = row.response_snapshot
    return api_response(
        data=body.get("data"),
        msg=body.get("msg", ""),
        code=body.get("code", ErrorCode.OK),
        status=row.status_code,
    )


def _existing_outcome(row, *, method, path, digest) -> Response:
    """已经有这一行时的三个出口（判据逐条写在 `_run` 的 docstring 里）。"""
    # ⚠️ 方法与路径也进这一条判据：唯一键是 `(app, key)`，**不含**端点
    # ⇒ 同一个键被用到**另一个端点**上时，快照与这次请求问的根本不是一件事。
    # ⛔ 不许照字面只看 `request_hash`：那会在两个端点的请求体**碰巧逐字相同**时
    # 把上一个端点的响应回放给这一个（＝静默给出一个错误答案）。
    if row.method != method or row.path != path or row.request_hash != digest:
        return api_response(data=None, msg=DIFFERENT_REQUEST_MSG, code=ErrorCode.VALIDATION)
    # ⚠️ 这里**用真值判断是对的**（E-0021 的例外形状）：空快照（`{}`）在本模块里
    # **就是**「上一次还没写成」这个语义的载体（`JSONField(default=dict)` 的占位值，
    # 见 `PLACEHOLDER_STATUS_CODE` 的注释），不是"传了 0 被当成没传"那一类边界。
    if not row.response_snapshot:
        return api_response(data=None, msg=IN_PROGRESS_MSG, code=ErrorCode.CONFLICT)
    return _replay(row)


def _run(request, handler, *, spec: CredentialSpec) -> Response:
    """幂等入口的全部流程（`idempotent` 装饰器只是它的一层壳）。

    ## 四态的判据（逐条）

    | 态 | 判据 | 结果 |
    |---|---|---|
    | ① 新键 | `(app, key)` 在台账里查不到 | 建占位行 → **同事务**调业务 → 成功则写快照（`status_code` + 响应体 dict） |
    | ② 回放 | 同键 **且** 同 `method` / `path` / `request_hash` **且** 快照非空 | `api_response` 按快照重建（⛔ 业务处理器一次都不跑） |
    | ③ 拒绝 | 同键 **但** `method` / `path` / `request_hash` 有任一不同 | `1000`（⛔ 不静默返回上一个响应） |
    | ④ 冲突 | 同键 **且** 请求相同 **但** 快照为空（上一笔还在执行 / 崩了） | `1001`（CONFLICT）+「同一个幂等键的请求正在处理中」 |

    ⚠️ `request_hash` = **请求体字节的 sha256**（模型 docstring 的逐字口径）。
    用**字节**而不是"解析后的 `request.data`"：DRF 对表单编码的请求体给出的是 `QueryDict`、
    对畸形 JSON 会抛 `ParseError`，拿它算摘要会平白多出一条路径
    （`AGENTS.md` §5-E：判据是"**这类请求**还会不会 500"）。⇒ "同体"= **同样的字节**，
    这条边界如实登记在这里。

    ⭐ **T6追加第五档出口**（详见模块 docstring 末尾的改准块）：
    信封非 0 **但** handler 声明过「副作用已落库」（`mark_side_effect`）⇒ **不回滚**、
    照常落快照（回放的仍是那次真实响应）。判据逐条：
    `tests/test_openapi_order_create.py::IdempotencyTests`（其中 `1022`/`1023` 两条）。

    ⭐ **T2：本入口收哪一份 spec** —— ⚠️ 当时是 `spec_of(request)`
    （从认证态推）；⭐⭐ **T2 改成必填 keyword-only 参数**：谁调用谁声明
    （`@idempotent(spec=…)` 的 6 个调用点，⛔ 漏传 = 构造期 `TypeError`）。
    理由见模块 docstring 的「T2」那一节（"运行期猜"与"必填"对同一种错两种待遇）。
    """
    invalid = _invalid_key_response(request)
    if invalid is not None:
        return invalid
    # ⭐ T10a：路径装不进 `path` 那一列 ⇒ 在**任何库读写之前**拒（否则占位行 `INSERT`
    # 会撞 `StringDataRightTruncation` ⇒ 500；逐条理由与实测读数见 `_overlong_path_response`）。
    overlong = _overlong_path_response(request, spec)
    if overlong is not None:
        return overlong

    key = request.headers[IDEMPOTENCY_HEADER]
    app = request.auth
    method = request.method
    path = request.path
    digest = request_digest(request)

    existing = _find_existing(app=app, key=key, spec=spec)
    if existing is not None:
        return _existing_outcome(existing, method=method, path=path, digest=digest)

    with transaction.atomic():
        try:
            # ⚠️ 内层 `atomic()` = **savepoint**：并发下第二笔的 `INSERT` 撞
            # `uniq_openapi_idem_app_key` 时只回滚这一笔，外层事务仍然可用
            # （模块 docstring 有理由；先例见 apps/business/jidanpu_upstream_pics.py）。
            with transaction.atomic():
                row = spec.idem_model.objects.create(
                    app=app,
                    key=key,
                    method=method,
                    path=path,
                    request_hash=digest,
                    response_snapshot={},
                    status_code=PLACEHOLDER_STATUS_CODE,
                )
        except IntegrityError:
            # 另一个请求刚刚用同一个键建了行 ⇒ 它正在执行（第四态）。
            # ⚠️ 先 `set_rollback(True)` 再 return（E-0022）：这个 `except` 把异常吞在
            # `with` **内部**，不显式回滚就会把外层事务当成正常路径提交掉。
            transaction.set_rollback(True)
            return api_response(data=None, msg=IN_PROGRESS_MSG, code=ErrorCode.CONFLICT)

        response = handler()
        body = getattr(response, "data", None)
        # ⚠️ 判"这一笔成没成"看**业务码**，⛔ 不看 `response.data is not None`
        # （`api_response()` 永远返回信封 dict ⇒ 那是恒真的判据，E-0063）。
        ok = isinstance(body, dict) and int(body.get("code", -1)) == int(ErrorCode.OK)
        # ⭐ T6：非 0 码但 handler 声明过「副作用已落库」⇒ **照常落快照**（见模块 docstring
        # 末尾的改准块与 `mark_side_effect` 的 docstring）。⛔ 别把这一条简化成
        # "非 0 一律保留"：那会让一次**什么都没写**的拒绝（缺参 / 闸门）留下快照，
        # 于是同一个键**永远重放那个失败**、再也不能重试（T5 的既有契约就破了）。
        if not ok and not _side_effect_done(request):
            # 可预期的业务失败 ⇒ 占位行跟着回滚（这个键**下次可重试**），响应原样发出去。
            transaction.set_rollback(True)
            return response

        _assert_no_credentials(body)
        row.response_snapshot = body
        row.status_code = response.status_code
        row.save(update_fields=["response_snapshot", "status_code"])
        # 本表没有 TTL ⇒ 建新行时顺手清一次老行（条件与上限见 `_prune`）。
        _prune(spec=spec)
    return response


def idempotent(*, spec: CredentialSpec):
    """把写端点方法包成幂等的（**全仓唯一的幂等入口**，`AGENTS.md` §5-E）。

    用法：`@idempotent(spec=OPENAPI_SPEC)` 套在 `APIView` 的 `post` / `put` / `patch`
    方法上；被包装的方法返回**统一信封**（`api_response(...)`）——成功（`code == 0`）
    才会落快照，其余按可预期失败回滚占位行。

    ## ⭐⭐ T2：`spec` 是**必填** keyword-only（⛔ 不给默认值、⛔ 不靠请求推断）

    改之前是裸 `@idempotent`（spec 由 `_run` 在运行期从认证态推）⇒ 同一种错
    （"忘了说自己是哪一族"）在这里**静默按上家侧处理**，而在 `_find_existing` 那几处
    是必填（当场 `TypeError`）。现在**写端点自己声明**自己哪一族：
    漏传 = 装饰那一刻就 `TypeError`（`TypeError: idempotent() missing 1 required
    keyword-only argument: 'spec'`），⛔ 不是等第一次请求按上家侧静默落库。
    ⚠️ **判据**：`tests/test_openapi_order_write.py::CredentialSpecDrivenLedgerTests::
    test_the_idempotent_decorator_requires_an_explicit_spec`。

    ⚠️ `functools.wraps` 必须留着：`test_drf_view_method_names` 之类的结构网按**方法名**
    认东西，把名字换成 `wrapper` 会让它们看不见这个处理器。
    """

    def decorator(view_method):
        @wraps(view_method)
        def wrapper(self, request, *args, **kwargs):
            return _run(
                request, lambda: view_method(self, request, *args, **kwargs), spec=spec
            )

        return wrapper

    return decorator


# ═════════════════════════════════════════════════════════════════════════════
# 录单的**额外**去重：同一应用 + 同一来源单号（设计文档 §6.3；T6 新增）
# ═════════════════════════════════════════════════════════════════════════════
# ⛔ **不新增模型列、不加唯一约束**：`Order.source_order_no` 没有唯一约束是**既有裁决**
# （业务方口径 + `AGENTS.md` §4「只增不删」），而复用它当去重事实源会**跨应用**生效
# （内部录单 / 上下游同步 / 鸡蛋铺回拉都写那一列）——契约要的是「**同一应用** + 同一来源单号」。
# ⇒ 合成一颗幂等键写进**本表**（`app` 那一列天然把它限死在应用内），下次同号再录就命中它。


def seen_source_response(*, app, source_order_no, spec: CredentialSpec):
    """同一应用 + 同一来源单号**已经录过** ⇒ 回放那一次的响应；没有 ⇒ `None`。

    ⚠️ 判据**只有** `(app, 合成键)` 两条（§6.3 的「同一应用 + 同一来源单号」逐字）：
    ⛔ **不比对** `method` / `path` / 请求体摘要 —— 那是 `Idempotency-Key` 那一套的判据。
    来源单号去重按契约就是「**同号即同单**」：同一个单号配一份改了别的字段的载荷来重发，
    回的仍然是**原来那一单**（⛔ 不是"再建一张"、也⛔ 不是 `1000`）。

    ⚠️ **快照为空 ⇒ 当没命中**（那一行是"正在建"的占位；此时**不能**把 `None` 的 data 当结果发出去）。

    ⚠️⭐ T2：查**哪张台账**、拼**哪个前缀**都经 `spec`（`spec` 必填，
    ⛔ 不给默认值：漏传 ⇒ 去查另一族的台账，而症状是"一族录过的单在另一族那边**回放**"）。
    """
    row = (
        spec.idem_model.objects.filter(app=app, key=source_key(source_order_no, spec=spec))
        .order_by("-id")
        .first()
    )
    if row is None or not row.response_snapshot:
        return None
    return _replay(row)


def remember_source(
    *, app, source_order_no, request, response, spec: CredentialSpec
) -> None:
    """建单成功之后**在同一个事务里**记下「这个来源单号已经录过」这一行。

    ⚠️ **必须在与订单同一个事务里调**（调用点就是录单 handler，它在 `_run` 的外层事务里）：
    业务回滚时它跟着回滚，否则会留下「单没建成、来源单号却已被记下」的半截状态
    ⇒ 下一次合法录单会被"回放"成一张**不存在的单**。

    ⚠️ **记的是"这一单"，不是"这一次响应的成败"**：调用方在**订单真的落库**之后调它
    （判据是 `IntakeOutcome.order is not None`），所以 `1022` / `1023`（单已建好、后续动作
    没成）那两档**也要记** —— 不记的话，调用方换个幂等键重发同一个来源单号会**再建一张单**。
    代价如实登记：那两档的快照就是那个非 0 信封 ⇒ 之后同号再来会**重放那一句失败**
    （而不是重试后续动作）。⚠️ 它**不改变事实**：那一单确实存在，查单端点看得到它。

    ⚠️ `method` / `path` / `request_hash` 照录**发起那次建单的请求**：这样万一有调用方
    直接拿这颗合成键当自己的 `Idempotency-Key` 用，`_run` 的通用那四态判据仍然成立
    （同体 ⇒ 回放、异体 ⇒ `1000`）。

    ⚠️⭐ T2：写**哪张台账**与拼**哪个前缀**都经 `spec`（必填，理由同
    `seen_source_response`：漏传 ⇒ 记到另一族的台账里，两边各自静默失效）。
    """
    body = getattr(response, "data", None)
    _assert_no_credentials(body, where="来源单号去重的快照")
    spec.idem_model.objects.create(
        app=app,
        key=source_key(source_order_no, spec=spec),
        method=request.method,
        path=request.path,
        request_hash=request_digest(request),
        response_snapshot=body,
        status_code=response.status_code,
    )
