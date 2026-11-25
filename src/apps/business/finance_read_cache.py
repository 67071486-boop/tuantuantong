"""Small per-process caches backed by transaction-visible tenant revisions."""
import hashlib
import json

from django.core.cache.backends.locmem import LocMemCache
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, ProgrammingError
from django.utils import timezone

from apps.business.game_scope import games_of
from apps.core.permissions import is_tenant_admin


_cache = LocMemCache("business-finance-reads", {"OPTIONS": {"MAX_ENTRIES": 128}})
_MAX_VALUE_BYTES = 64 * 1024
_TTL = {"catalog": 30, "settlement": 120}


def _revision(domain):
    if connection.vendor != "postgresql" or connection.in_atomic_block or not connection.get_autocommit():
        return None
    schema = connection.ops.quote_name(connection.schema_name)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT epoch, {domain}_version FROM {schema}.business_finance_read_revision WHERE singleton"
            )
            return cursor.fetchone()
    except ProgrammingError as exc:
        if getattr(exc.__cause__, "sqlstate", None) != "42P01":
            raise
        return None  # Application can safely serve a tenant during an additive migration rollout.


def _cached(domain, signature, build):
    revision = _revision(domain)
    if revision is None:
        return build()
    database = connection.settings_dict
    identity = tuple(database[key] for key in ("HOST", "PORT", "NAME")) + (connection.schema_name,)
    encoded = json.dumps((identity, domain, revision, signature), cls=DjangoJSONEncoder)
    key = hashlib.sha256(encoded.encode()).hexdigest()
    saved = _cache.get(key)
    if saved is not None:
        return json.loads(saved)
    value = build()
    packed = json.dumps(value, cls=DjangoJSONEncoder, ensure_ascii=False).encode()
    if len(packed) <= _MAX_VALUE_BYTES and revision == _revision(domain):
        _cache.set(key, packed, _TTL[domain])
    return value


def catalog_options(build):
    return _cached("catalog", (), build)


def historical_settlement(user, through, parameters, build):
    if through is None or through >= timezone.localdate() or connection.in_atomic_block:
        return build(game_ids=None, apply_game_scope=True)
    # Resolve and pass exactly the same visible game set into the query and the cache key.
    game_ids = None if is_tenant_admin(user) else tuple(
        games_of(user).order_by("pk").values_list("pk", flat=True)
    )
    signature = (timezone.get_current_timezone_name(), game_ids, parameters)
    return _cached("settlement", signature, lambda: build(game_ids=game_ids, apply_game_scope=False))
