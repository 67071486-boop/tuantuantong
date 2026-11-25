"""Private, bounded runtime state for verified order-finance browser requests."""
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import date, datetime, time as local_time, timedelta, timezone
from decimal import Decimal
import fcntl
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import pickle
import re
import stat
from struct import Struct
from time import monotonic, monotonic_ns, sleep, time

from django.conf import settings

from apps.core.envelope import error_response, rendered_response

_CACHE_SLOTS = 256
_CACHE_ENTRY_BYTES = 512 * 1024
_CACHE_HEADER = Struct("!32sQ")
_CACHE_WAIT_S = .15
_CACHE_RETRY_S = .005
_DIAGNOSTIC_MINUTES = 6
_DIAGNOSTIC_BYTES = 512 * 1024
_MAX_HEAVY_READS = 64
_MISS = object()
_HALL_RUNTIME = ContextVar("finance_hall_runtime", default=None)
_HALL = "/api/business/hall"
_DETAIL = re.compile(r"/api/business/orders/[0-9]{20}(?:\.[a-z0-9]+)?")
_LIST = re.compile(r"/api/business/(orders|performances)(?:\.[a-z0-9]+)?")
_HEAVY_READS = {
    "/api/business/business-report": "business-report",
    "/api/business/order-stats": "order-stats",
    "/api/business/support-stats": "support-stats",
    "/api/business/my-orders/export": "my-orders.export",
    "/api/business/orders/export": "orders.export",
    "/api/business/performances/export": "performances.export",
    "/api/business/settlements": "settlements.list",
    "/api/business/settlements/export": "settlements.export",
    "/api/business/settlements/unattributed": "settlements.unattributed",
    "/api/business/settlements/daily-summary": "settlements.daily-summary",
    "/api/business/settlements/daily-summary/sources": "settlements.daily-summary.sources",
}
_CACHE_VALUE_TYPES = {
    (cls.__module__, cls.__name__): cls
    for cls in (OrderedDict, date, datetime, local_time, timedelta, timezone, Decimal)
}


class _HallUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        value = _CACHE_VALUE_TYPES.get((module, name))
        if value is not None:
            return value
        if (module, name) == ("apps.business.models", "OrderStatus"):
            # Mirrored hall rows carry this exact enum in their serialized payload.
            from apps.business.models import OrderStatus

            return OrderStatus
        raise pickle.UnpicklingError("unsupported hall cache value type")


def _route(method, path):
    if method not in {"GET", "HEAD"}:
        return "write", False
    path = path.removesuffix("/")
    if path == _HALL:
        return "hall", False
    if match := _LIST.fullmatch(path):
        return match[1] + ".list", True
    if label := _HEAVY_READS.get(path):
        return label, True
    if _DETAIL.fullmatch(path):
        return "orders.detail", False
    return "other-read", False


class FinanceRuntime:
    """One private directory shared by both workers of the same service."""

    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_absolute():
            raise ValueError("order-finance runtime directory must be absolute")
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("order-finance runtime directory must be private and service-owned")

    def _open(self, name, *, create=True):
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(self.directory / name, flags | (os.O_CREAT if create else 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError("invalid order-finance runtime file")
            return os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise

    def _open_slot(self, prefix, limit):
        for index in range(limit):
            slot = self._open(f"{prefix}-{index}")
            try:
                fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                slot.close()
            except OSError:
                slot.close()
                raise
            else:
                return slot
        return None

    @contextmanager
    def admit(self, schema, *, global_limit, tenant_limit):
        if global_limit == 0:
            yield None
            return
        if not 1 <= tenant_limit < global_limit <= _MAX_HEAVY_READS:
            yield "unavailable"
            return
        tenant_id = sha256(schema.encode()).hexdigest()
        with ExitStack() as slots:
            refusal = "busy"
            try:
                tenant = self._open_slot("admit-tenant-" + tenant_id, tenant_limit)
                if tenant is not None:
                    slots.enter_context(tenant)
                    global_slot = self._open_slot("admit-global", global_limit)
                    if global_slot is not None:
                        slots.enter_context(global_slot)
                        refusal = None
            except OSError:
                refusal = "unavailable"
            yield refusal

    @staticmethod
    def _read_cache(slot, digest):
        slot.seek(0)
        header = slot.read(_CACHE_HEADER.size)
        if len(header) != _CACHE_HEADER.size:
            return _MISS
        saved_digest, expires = _CACHE_HEADER.unpack(header)
        if saved_digest != digest or monotonic_ns() >= expires:
            return _MISS
        data = slot.read(_CACHE_ENTRY_BYTES + 1)
        if len(data) > _CACHE_ENTRY_BYTES:
            return _MISS
        try:
            return _HallUnpickler(BytesIO(data)).load()
        except (AttributeError, EOFError, ImportError, IndexError, OverflowError, ValueError, pickle.UnpicklingError):
            return _MISS

    @staticmethod
    def _write_cache(slot, digest, ttl, value):
        data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        if len(data) > _CACHE_ENTRY_BYTES:
            return
        slot.seek(0)
        slot.truncate()
        header = _CACHE_HEADER.pack(digest, monotonic_ns() + int(ttl * 1e9))
        slot.write(header + data)

    def _hall_slot(self, digest):
        slot = self._open(f"hall-v1-{int.from_bytes(digest[:2]) % _CACHE_SLOTS:03}")
        deadline = monotonic() + _CACHE_WAIT_S
        try:
            while True:
                try:
                    fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return slot
                except BlockingIOError:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        slot.close()
                        return None
                    sleep(min(_CACHE_RETRY_S, remaining))
        except BaseException:
            slot.close()
            raise

    def hall(self, key, *, ttl, build, fallback):
        digest = sha256(key.encode()).digest()
        try:
            slot = self._hall_slot(digest)
        except OSError:
            return fallback()
        if slot is None:
            return fallback()
        with slot:
            try:
                cached = self._read_cache(slot, digest)
            except OSError:
                cached = _MISS
            if cached is not _MISS:
                return cached
            value = build()
            try:
                self._write_cache(slot, digest, ttl, value)
            except (OSError, TypeError, pickle.PicklingError):
                pass
            return value

    def record_diagnostic(self, *, tenant, route, duration_ms, status, refused, business_error=False):
        now = time()
        minute = int(now // 60)
        header = f"{minute}\n".encode()
        name = f"diagnostics-v1-{minute % _DIAGNOSTIC_MINUTES}"
        event = json.dumps([now, tenant, route, status, duration_ms, refused, business_error], separators=(",", ":"))
        record = (event + "\n").encode()
        try:
            with self._open(name) as log:
                fcntl.flock(log, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if log.readline(32) != header:
                    log.seek(0)
                    log.truncate()
                    log.write(header)
                log.seek(0, os.SEEK_END)
                if log.tell() + len(record) <= _DIAGNOSTIC_BYTES:
                    log.write(record)
        except OSError:
            pass

    def recent(self, seconds):
        now = time()
        cutoff = now - seconds
        for index in range(_DIAGNOSTIC_MINUTES):
            try:
                with self._open(f"diagnostics-v1-{index}", create=False) as log:
                    fcntl.flock(log, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    data = log.read(_DIAGNOSTIC_BYTES)
            except (FileNotFoundError, BlockingIOError):
                continue
            for line in data.splitlines()[1:]:
                try:
                    timestamp, tenant, route, status, duration, refused, business_error = json.loads(line)
                except (TypeError, UnicodeDecodeError, ValueError):
                    continue
                if (
                    type(timestamp) in (int, float) and cutoff <= timestamp <= now
                    and isinstance(tenant, str) and isinstance(route, str)
                    and type(status) is int and type(duration) is int
                    and type(refused) is bool and type(business_error) is bool
                ):
                    yield tenant, route, status, duration, refused, business_error


@contextmanager
def active_hall_runtime(schema, runtime=None):
    runtime = runtime or FinanceRuntime(settings.TT_ORDER_FINANCE_RUNTIME_DIR)
    token = _HALL_RUNTIME.set((schema, runtime))
    try:
        yield
    finally:
        _HALL_RUNTIME.reset(token)


def current_hall_runtime(schema):
    active = _HALL_RUNTIME.get()
    return active[1] if active and active[0] == schema else None


def _refused_response(refusal):
    if refusal == "busy":
        return rendered_response(error_response(429, "订单财务查询繁忙，请稍后重试", 429, {"Retry-After": "1"}))
    return rendered_response(error_response(503, "订单财务查询暂不可用，请稍后重试", 503, {"Retry-After": "1"}))


def serve_verified_finance_request(request, get_response, schema):
    directory = settings.TT_ORDER_FINANCE_RUNTIME_DIR
    if not directory:
        return get_response(request)
    route, heavy = _route(request.method, request.path_info)
    global_limit = settings.TT_ORDER_FINANCE_GLOBAL_HEAVY_READS
    try:
        runtime = FinanceRuntime(directory)
    except (OSError, ValueError):
        if heavy and global_limit:
            return _refused_response("unavailable")
        return get_response(request)

    started = monotonic()
    status = 500
    business_error = False
    refusal = None
    with ExitStack() as stack:
        try:
            if heavy:
                refusal = stack.enter_context(runtime.admit(
                    schema, global_limit=global_limit, tenant_limit=settings.TT_ORDER_FINANCE_TENANT_HEAVY_READS,
                ))
            if refusal:
                response = _refused_response(refusal)
            else:
                if route == "hall" and request.method == "GET":
                    stack.enter_context(active_hall_runtime(schema, runtime))
                response = get_response(request)
            status = response.status_code
            if settings.TT_ORDER_FINANCE_DIAGNOSTICS_ENABLED:
                data = getattr(response, "data", None)
                code = data.get("code") if isinstance(data, dict) else None
                business_error = type(code) is int and code != 0
            return response
        finally:
            if settings.TT_ORDER_FINANCE_DIAGNOSTICS_ENABLED:
                runtime.record_diagnostic(
                    tenant=schema, route=route, duration_ms=round((monotonic() - started) * 1000),
                    status=status, refused=refusal is not None, business_error=business_error,
                )
