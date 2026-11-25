from contextlib import contextmanager, nullcontext
from hashlib import blake2b

from django.db import connection


ORDER_BUSY = "这张订单正在向鸡蛋铺发送指派，请稍后重试。"
BINDING_BUSY = "这个鸡蛋铺绑定有指派正在发送，请稍后再修改。"


def _key(scope, pk):
    data = f"{connection.schema_name}:jidanpu-direct-assignment:{scope}:{pk}".encode()
    return int.from_bytes(blake2b(data, digest_size=8).digest(), "big", signed=True)


def _try_lock(statement, key):
    with connection.cursor() as cursor:
        cursor.execute(statement, [key])
        return bool(cursor.fetchone()[0])


@contextmanager
def _session_lock(key, *, shared=False):
    take = "SELECT pg_try_advisory_lock_shared(%s)" if shared else "SELECT pg_try_advisory_lock(%s)"
    release = "SELECT pg_advisory_unlock_shared(%s)" if shared else "SELECT pg_advisory_unlock(%s)"
    acquired = _try_lock(take, key)
    try:
        yield acquired
    finally:
        if acquired:
            _try_lock(release, key)


@contextmanager
def sending(*, binding_id, order_id, source_binding_id=None):
    # Other orders can use the same binding while edits to its credentials wait for sending to end.
    with _session_lock(_key("binding", binding_id), shared=True) as binding_available:
        if not binding_available:
            yield BINDING_BUSY
            return
        source_lock = (
            _session_lock(_key("binding", source_binding_id), shared=True)
            if source_binding_id is not None and source_binding_id != binding_id
            else nullcontext(True)
        )
        with source_lock as source_available:
            if not source_available:
                yield BINDING_BUSY
                return
            with _session_lock(_key("order", order_id)) as order_available:
                yield "" if order_available else ORDER_BUSY


def binding_change_available(binding_id):
    with _session_lock(_key("binding", binding_id)) as acquired:
        return acquired


def lock_binding_change(binding_id):
    return _try_lock("SELECT pg_try_advisory_xact_lock(%s)", _key("binding", binding_id))


def lock_order_change(order_id):
    return _try_lock("SELECT pg_try_advisory_xact_lock(%s)", _key("order", order_id))


def lock_remote_copy(store_url, remote_id):
    """Serialize ownership of one remote copy inside the caller's transaction."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_key("remote-copy", (store_url, remote_id))])
