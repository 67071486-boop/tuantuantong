"""Short hall cache keyed by schema, request parameters and each viewer's visibility.
Dedicated finance workers can share it through the private runtime directory.
Other entry points retain their process-local cache and the same TTL.
"""
from __future__ import annotations

import json

from django.conf import settings
from django.core.cache import caches
from django.db import connection

from apps.core.order_finance_runtime import current_hall_runtime

#: 键前缀（防与 locmem 里其它使用者撞键；schema 与取值由 `_cache_key` 拼）。
_KEY_PREFIX = "hallcache"

#: 大厅载荷专用的 cache alias（为什么独立，见模块 docstring「独立 alias」节）。
_CACHE_ALIAS = "hall_list"


def hall_cache_ttl() -> int:
    """TTL（秒）的出处：`settings.HALL_LIST_CACHE_TTL_S`（`0` = 紧急关闭）。

    运行期读 `settings`（不是 import 时常量）⇒ `.env` 改值与测试的
    `override_settings` 都即时生效。
    """
    return settings.HALL_LIST_CACHE_TTL_S


def _cache_key(key_parts) -> str:
    """`"hallcache:" + schema + ":" + json(key_parts)`（构成与理由见模块 docstring）。`default=str` 兜住 `key_parts` 里的非 JSON 类型（如 `Decimal`）；
    `sort_keys`让相同内容的不同构造序得到同一个键。⚠️ `separators=(",", ":")` 必须保留：
    Django 默认分隔符带空格，而`memcache_key_warnings` 的正则（`django/core/cache/backends/base.py:393`，`[\x00-\x20\x7f]`）把空格判为非法字符 ⇒ 每个新键都在日志添一行`CacheKeyWarning`（钉子：`tests/test_hall_short_cache.py::HallCacheKeyTests`）。"""
    return (
        _KEY_PREFIX
        + ":"
        + connection.schema_name
        + ":"
        + json.dumps(
            key_parts, sort_keys=True, ensure_ascii=False, default=str,
            separators=(",", ":"),
        )
    )


def short_cached_payload(key_parts, *, ttl, build):
    """按 `key_parts` 缓存 `build()` 的结果 `ttl` 秒；`ttl <= 0` ⇒ 直接透传（紧急开关路径）。⚠️ `ttl <= 0`（E-0021：
    `0` 是合法值 = 关闭）时**零缓存读写**：
    既不 `get` 也不 `set`，`build()` 每次照常执行——`.env` 把 `HALL_LIST_CACHE_TTL_S` 写成 0 即等于本功能不存在。⚠️ 命中返回的是反 pickle 出来的**新副本**（模块 docstring「副本语义」节），调用方对它的原地修改不会影响缓存里的那份。"""
    if ttl <= 0:
        return build()
    key = _cache_key(key_parts)
    if runtime := current_hall_runtime(connection.schema_name):
        return runtime.hall(key, ttl=ttl, build=build, fallback=lambda: _locally_cached(key, ttl, build))
    return _locally_cached(key, ttl, build)


def _locally_cached(key, ttl, build):
    cached = caches[_CACHE_ALIAS].get(key)
    if cached is not None:
        return cached
    payload = build()
    caches[_CACHE_ALIAS].set(key, payload, ttl)
    return payload