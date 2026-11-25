"""实名核验的**按人限流**（D-0251）：cache 固定窗口桶，桶键只含服务端来源的值。

## 口径（`docs/DECISIONS.md` 的 D-0251 行，⛔ 台账不改，这里只引用）

- **按用户计**（D-0251 ①）：桶键 = 租户 schema + user pk，**全部服务端来源**
  （M17 教训：⛔ 不用请求方可控的值当桶键）。⚠️ schema 必须进键：cache 是**进程级**的、
  跨租户共享，而 user pk 在各租户库里各自自增——不带 schema，两个租户的 5 号用户会
  共用一个桶（互相误伤）。
- **计数对象**（D-0251 ② + 实施细化①）：核验提交（**被 1026/1027 配额拒掉的也计数**
  ——递增点在配额事务**之前**，被限流者不必排队等行锁；「拿错误信息反复提交」正是要拦的形状，
  是②字面定义「会出网」的超集）。递增点在 `services.verify_for_user` 的幂等短路**之后**、
  厂商调用**之前** ⇒ 本地格式失败（②步抛 `RealNameInvalidInput`）与 PASS 短路（③步返回）
  两条「零出网」路径**天然到不了这里**。
- **阈值 / 窗口**（D-0251 ④ 定的骨架，数值已改）：默认 **3 次 / 86400 秒**
  （**业务方 改成「每人每天最多 3 次」**，取代 D-0251 原来的「5 次 / 3600 秒」——
  防的是拿错误信息反复刷厂商接口，一天 3 次够正常用户纠错）。⚠️ 这是**从本窗口首次提交起算的
  24 小时固定窗口**，⛔ 不是自然日 0 点清零：当天第 1 次提交后 24 小时内最多 3 次，
  窗口滚过自动恢复。
  settings 可配：`REALNAME_VERIFY_RATE_LIMIT` / `REALNAME_VERIFY_RATE_WINDOW_SECONDS`。
  ⚠️ 两个值都在**函数体里** `getattr(settings, …)`、⛔ 不在 import 期读——
  不然 `override_settings`（与运行时改配置）都不生效。读不到就回默认值
  （⛔ 不抛：缺配置不该把核验端点整个打挂）。
- **超额**（D-0251 ⑤）⇒ `RealNameRateLimited`（业务码 `1029` +
  「尝试过于频繁，请稍后再试」）；**不锁定**——窗口滚过（cache TTL 到期）自动恢复。
- ⚠️ **已知软限**（D-0251 ④ 如实登记）：默认 `LocMemCache` 在多 worker（`--workers 2`）下
  各计各的 ⇒ 实际上限最坏 = worker 数 × 阈值。

## 计数语义（照 `apps/openapi/rate_limit.py` 的既有先例）

**count 是「尝试数」不是「放行数」**：被拒的那几次也 +1（「记一笔」与「判超没超」是同一次
`add`/`incr` 完成的，判在读数之后）⇒ 进了超限窗口就一直拒到 TTL 到期，可观察行为与
「先判后记」完全一致。

## ⛔ 为什么是 `add` + `incr`（不是 `accounts/selfreg_views.py` 的 `get` + `set`）

`cache.add(key, 1, timeout)` 是原子的：并发首访只有一家建桶成功，失败的一方落 `incr`；
TTL 只在**建桶那一刻**设一次（`incr` 保留原 TTL，locmem 与 redis 同款语义）⇒ **固定窗口**。
selfreg 的 `get` + `set` 每次失败都**重设 TTL** ⇒ 那是滑动窗口（持续失败就永远不过期），
与 D-0251 的「窗口滚过自动恢复」直接冲突——⛔ 别照它抄。
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.db import connection

from apps.core.error_codes import ErrorCode

#: 默认阈值 / 窗口（业务方 ：每人每天最多 3 次 ⇒ 3 次 / 86400 秒；
#: 取代 D-0251 原本的 5 次 / 3600 秒。窗口是首次提交起算的 24 小时固定窗口）。
DEFAULT_LIMIT = 3
DEFAULT_WINDOW_SECONDS = 86400

#: 超额那一档的业务码与文案（D-0251 原话；`1029` 的登记出处见 `error_codes.py`）。
RATE_LIMITED_CODE = int(ErrorCode.REALNAME_RATE_LIMITED)
RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"

#: 桶键前缀（防与 cache 里其它使用者撞键；schema 与 user pk 由 `bucket_key` 拼）。
_KEY_PREFIX = "realname-verify"


class RealNameRateLimited(Exception):
    """按人限流的**预期**业务拒绝（D-0251 ⑤）——调用方（核验视图）翻成 `1029`。

    ⚠️ 与本 app 其余异常同一口径：这是**预期**的业务拒绝，不是异常路径的兜底；
    未预期的异常照旧响亮 500（`AGENTS.md` §5-E / E-0010）。
    码与文案是**类属性**：这一档只有一种形态，没有参数化的必要。
    """

    code = RATE_LIMITED_CODE
    message = RATE_LIMITED_MSG


def bucket_key(*, user_pk: int) -> str:
    """限流桶键：`realname-verify:<租户 schema>:<user pk>`——**全部服务端来源**（D-0251 ①）。

    ⛔ 任何调用点都不许把请求参数（IP、号码、姓名……）拼进键：那正是 M17 的形状。
    """
    return f"{_KEY_PREFIX}:{connection.schema_name}:{user_pk}"


def register_attempt(*, user) -> None:
    """给这次核验提交**记一笔**；超额就抛 `RealNameRateLimited`（`1029`）。

    调用点 = `services.verify_for_user`：幂等短路**之后**、`transaction.atomic()`（厂商
    调用）**之前**——检查与递增都在厂商调用之前（D-0251 ③），被限流的那一次
    **零出网、零扣减、零流水**。

    ⚠️ 阈值语义：第 `limit` 次放行、第 `limit + 1` 次起被拒（`count > limit` ⇒ 拒），
    默认 3 次 / 24 小时窗口（业务方 改；D-0251 原口径是 5 次/小时）。
    """
    limit = getattr(settings, "REALNAME_VERIFY_RATE_LIMIT", DEFAULT_LIMIT)
    window = getattr(settings, "REALNAME_VERIFY_RATE_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS)
    key = bucket_key(user_pk=user.pk)
    if cache.add(key, 1, timeout=window):
        return  # 本窗口第一次：TTL 在这一刻设上，窗口从这里起算
    try:
        count = cache.incr(key)
    except ValueError:
        # `add` 判负到 `incr` 之间键恰好滚过窗口（TTL 到期的边界竞态）⇒ 重建桶。
        # ⚠️ `set` 会覆盖并发新建的桶 ⇒ 极端竞态下少记一次：与 D-0251 ④ 登记的软限同族，如实接受。
        cache.set(key, 1, timeout=window)
        return
    if count > limit:
        raise RealNameRateLimited()
