"""Schedule the existing peer work across isolated tenant databases."""
import asyncio
import logging
import multiprocessing
import signal
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

import psycopg
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business import peer_hall, peer_wakeup, peer_worker
from apps.business.models import PeerSyncEvent
from apps.business.peer_client import enforce_total_peer_timeout, new_peer_deadline_loop
from apps.business.peer_sync import CORE_CHAT_TARGET
from apps.internal_chat.models import OrderChatPeerLink, OrderGroup
from apps.platform.models import Domain
from config.shared_web import _bind_database, _load_routes

logger = logging.getLogger(__name__)

THREADS_PER_LANE = 2
POLL_SECONDS = 10
TICK_SECONDS = 0.1
TURN_SECONDS = 5
DELIVERY_BATCH_SIZE = 5
SHUTDOWN_SECONDS = 15
LANES = ("delivery", "maintenance")
LANE_LOCKS = {"delivery": 1, "maintenance": 2}
PEER_SYNC_LOCK_NAMESPACE = 70219137
DATABASE_IDENTITY_SQL = """
    SELECT current_database(), current_user, owner = role.oid,
           NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    FROM pg_roles AS role
    JOIN (SELECT datdba AS owner FROM pg_database WHERE datname = current_database()) AS db ON true
    WHERE rolname = current_user
"""


class TenantLane:
    def __init__(self, tenants):
        self.tenants = tuple(tenants)
        self.waiting = OrderedDict()
        self.running = set()
        self.woken_while_running = set()
        self.delayed = {}

    def poll(self):
        for tenant in self.tenants:
            self.wake(tenant)

    def wake(self, tenant):
        if tenant in self.running:
            self.woken_while_running.add(tenant)
        else:
            self.waiting[tenant] = None

    def take(self):
        if len(self.running) == THREADS_PER_LANE or not self.waiting:
            return None
        tenant, _ = self.waiting.popitem(last=False)
        self.running.add(tenant)
        return tenant

    def complete(self, tenant, *, more, wake_at=None):
        self.running.remove(tenant)
        if more or tenant in self.woken_while_running:
            self.wake(tenant)
        self.woken_while_running.discard(tenant)
        if wake_at is not None:
            self.delayed[tenant] = wake_at

    def wake_due(self, tick):
        for tenant, wake_at in list(self.delayed.items()):
            if wake_at <= tick:
                del self.delayed[tenant]
                self.wake(tenant)


def load_tenant_routes(filename):
    routes = {}
    for hostname, route in _load_routes(filename).items():
        if route.schema == get_public_schema_name() or route.schema in routes:
            raise ImproperlyConfigured("Shared peer sync requires one domain per non-public tenant")
        routes[route.schema] = (hostname, route)
    return routes


@contextmanager
def tenant_database(hostname, route, *, readonly=False):
    database = route.connection()
    database.shared_web_schema = route.schema
    if readonly:
        database.settings_dict["OPTIONS"]["options"] = "-c default_transaction_read_only=on -c statement_timeout=5000"
    with _bind_database(database):
        try:
            database.set_schema_to_public()
            with database.cursor() as cursor:
                cursor.execute(DATABASE_IDENTITY_SQL)
                if cursor.fetchone() != (route.database, route.database, True, True):
                    raise ImproperlyConfigured("Shared peer sync requires the isolated database owner")
                cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", [route.schema])
                if cursor.fetchone() is None:
                    raise ImproperlyConfigured("Shared peer sync tenant schema is absent")
            domain = Domain.objects.select_related("tenant").get(
                domain=hostname, tenant__schema_name=route.schema, tenant__is_active=True,
            )
            with tenant_context(domain.tenant):
                yield database
        finally:
            database.close()


def has_pending_peer_chat():
    return PeerSyncEvent.objects.filter(
        direction="out", target_type__in=("chat", CORE_CHAT_TARGET),
        status=PeerSyncEvent.Status.PENDING,
    ).exists()


def chat_cutover_blocker():
    if has_pending_peer_chat():
        return "有跨工作室聊天待投递"
    if OrderGroup.objects.filter(binding_id__isnull=False, binding_role__in=("upstream", "downstream")).exists():
        return "有仍相连的旧跨工作室订单群"
    if OrderChatPeerLink.objects.filter(detached_at__isnull=True).exists():
        return "有仍相连的聊天微服务订单群"
    return ""


@dataclass
class DeliveryTurn:
    window: peer_worker.DeliveryWindow
    since: datetime
    expires: float


class TenantJobs:
    def __init__(self):
        self.delivery_windows = {}
        self.reconcile_cursors = {}
        self.http_loops = {}
        self.next_chat_warning = {}
        self.hall_retries = {}

    def close_delivery_window(self, schema, *, now):
        current = self.delivery_windows[schema]
        def skipped(role, binding, exc):
            logger.warning(
                "上下游共享投递 %s：跳过本窗连败结算（%s %s，%s）",
                schema, role, binding, type(exc).__name__,
            )

        revoked = current.window.close(now=now, since=current.since, on_error=skipped)
        if revoked:
            logger.warning("上下游共享投递 %s：自动停绑 %d", schema, revoked)

    def finish_delivery(self, routes):
        for schema, turn in self.delivery_windows.items():
            if not turn.window.conn_failed - turn.window.succeeded:
                continue
            try:
                with tenant_database(*routes[schema]) as database:
                    if claim_lane(database, "delivery"):
                        self.close_delivery_window(schema, now=timezone.now())
            except Exception as exc:
                logger.warning("上下游共享关停结算 %s 失败（%s）", schema, type(exc).__name__)

    def deliver(self, schema, stop):
        now = timezone.now()
        tick = monotonic()
        current = self.delivery_windows.get(schema)
        if current is not None and tick >= current.expires:
            self.close_delivery_window(schema, now=now)
            current = None
        if current is None:
            current = DeliveryTurn(peer_worker.DeliveryWindow(), now, tick + POLL_SECONDS)
            self.delivery_windows[schema] = current

        summary = peer_worker.run_delivery_batch(
            now=now, window=current.window, batch_size=DELIVERY_BATCH_SIZE, orders_only=True,
            should_stop=lambda: stop.is_set() or monotonic() >= tick + TURN_SECONDS,
        )
        if summary["claimed"]:
            logger.info(
                "上下游共享投递 %s：认领 %d，已投送 %d，待重试 %d，失败 %d，内部异常 %d",
                schema, summary["claimed"], summary["delivered"], summary["retried"],
                summary["failed"], len(summary["errors"]),
            )
        if tick >= self.next_chat_warning.get(schema, -1):
            self.next_chat_warning[schema] = tick + 60
            if has_pending_peer_chat():
                logger.warning("上下游共享投递 %s：跨工作室聊天事件仍待发送，已原样保留", schema)
        return bool(summary["claimed"])

    def _remember_hall_retry(self, schema, pull):
        retry_at = pull.get("retry_at")
        if retry_at is None:
            self.hall_retries.pop(schema, None)
            return False
        self.hall_retries[schema] = (retry_at, pull["retry_binding_pk"])
        return retry_at

    def maintain(self, schema, stop):
        pending_retry = self.hall_retries.get(schema)
        if pending_retry is not None:
            retry_at, binding_pk = pending_retry
            if timezone.now() < retry_at:
                return retry_at
            pull = peer_worker.run_hall_pull(retry_binding_pk=binding_pk)
            return self._remember_hall_retry(schema, pull)
        deadline = monotonic() + TURN_SECONDS
        reclaimed = peer_hall.reclaim_stuck_claims(
            should_stop=lambda: stop.is_set() or monotonic() >= deadline,
        )
        if stop.is_set():
            return False
        cursor = self.reconcile_cursors.setdefault(schema, peer_worker.ReconcileCursor())
        deadline = monotonic() + TURN_SECONDS
        reconcile = peer_worker.run_reconcile(
            should_stop=lambda: stop.is_set() or monotonic() >= deadline, cursor=cursor, orders_only=True,
        )
        if stop.is_set():
            return False
        pull = peer_worker.run_hall_pull()
        if reclaimed or reconcile["requeued"] or reconcile["errors"] or pull["pages"]:
            logger.info(
                "上下游共享补拉 %s：回收 %d，对账补投 %d，对账失败 %d，拉取 %d 页",
                schema, reclaimed, reconcile["requeued"], reconcile["errors"], pull["pages"],
            )
        return self._remember_hall_retry(schema, pull)

    def run(self, lane, schema, route, stop):
        try:
            with tenant_database(*route) as database:
                if stop.is_set():
                    return False
                if not claim_lane(database, lane):
                    logger.warning("上下游共享 %s 租户 %s 已被另一进程认领", lane, schema)
                    return False
                thread = threading.get_ident()
                loop = self.http_loops.get(thread)
                if loop is None:
                    loop = self.http_loops[thread] = new_peer_deadline_loop()
                with enforce_total_peer_timeout(loop):
                    if lane == "delivery":
                        return self.deliver(schema, stop)
                    return self.maintain(schema, stop)
        except Exception as exc:
            logger.warning("上下游共享 %s 租户 %s 本轮失败（%s）", lane, schema, type(exc).__name__)
            return False


def claim_lane(database, lane):
    with database.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            [PEER_SYNC_LOCK_NAMESPACE, LANE_LOCKS[lane]],
        )
        return cursor.fetchone()[0]


def _executor(lane, routes, pipe, other_pipes):
    for other_pipe in other_pipes:
        other_pipe.close()
    stop = threading.Event()

    def request_stop(_signal, _frame):
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, request_stop)
    jobs = TenantJobs()
    pending = {}
    with pipe, ThreadPoolExecutor(max_workers=THREADS_PER_LANE, thread_name_prefix=lane) as pool:
        while not stop.is_set():
            for schema, job in list(pending.items()):
                if job.done():
                    try:
                        pipe.send((schema, job.result()))
                    except (BrokenPipeError, EOFError):
                        stop.set()
                        break
                    del pending[schema]
            if not stop.is_set() and pipe.poll(TICK_SECONDS):
                try:
                    schema = pipe.recv()
                except EOFError:
                    break
                if schema is None:
                    break
                pending[schema] = pool.submit(jobs.run, lane, schema, routes[schema], stop)
        stop.set()
    for loop in jobs.http_loops.values():
        loop.close()
    if lane == "delivery":
        jobs.finish_delivery(routes)


async def _listen(schema, route, lane):
    reconnect_delay = 1
    while True:
        try:
            db = await psycopg.AsyncConnection.connect(
                dbname=route.database, user=route.database, password=route.password,
                host=route.host, port=route.port, connect_timeout=3, autocommit=True,
            )
            async with db:
                cursor = await db.execute(DATABASE_IDENTITY_SQL)
                if await cursor.fetchone() != (route.database, route.database, True, True):
                    raise ImproperlyConfigured("Shared peer sync listener requires the isolated database owner")
                await db.execute(f"LISTEN {peer_wakeup.CHANNEL}")
                lane.wake(schema)
                reconnect_delay = 1
                async for _ in db.notifies():
                    lane.wake(schema)
        except (psycopg.Error, OSError, ImproperlyConfigured) as exc:
            logger.warning("上下游共享通知 %s 连接失败，继续定时查询（%s）", schema, type(exc).__name__)
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(POLL_SECONDS, reconnect_delay * 2)


async def _schedule(routes, executors):
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    lanes = {name: TenantLane(routes) for name in LANES}
    listeners = [
        asyncio.create_task(_listen(schema, route, lanes["delivery"]))
        for schema, (_, route) in routes.items()
    ]
    next_poll = 0
    try:
        while not stop.is_set():
            for name, (process, pipe) in executors.items():
                if not process.is_alive():
                    raise RuntimeError(f"Shared peer sync {name} process exited ({process.exitcode})")
                lane = lanes[name]
                while pipe.poll():
                    schema, result = pipe.recv()
                    if name == "maintenance" and result:
                        delay = max(0, (result - timezone.now()).total_seconds())
                        lane.complete(schema, more=False, wake_at=monotonic() + delay)
                    else:
                        lane.complete(schema, more=result)
            tick = monotonic()
            if tick >= next_poll:
                for lane in lanes.values():
                    lane.poll()
                next_poll = tick + POLL_SECONDS
            for name, lane in lanes.items():
                lane.wake_due(tick)
                pipe = executors[name][1]
                while (schema := lane.take()) is not None:
                    pipe.send(schema)
            await asyncio.sleep(TICK_SECONDS)
    finally:
        for task in listeners:
            task.cancel()
        await asyncio.gather(*listeners, return_exceptions=True)
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def run_scheduler(routes):
    # Fork before opening tenant connections or starting threads.
    context = multiprocessing.get_context("fork")
    executors = {}
    with ExitStack() as cleanup:
        pipes = {name: context.Pipe() for name in LANES}
        endpoints = [cleanup.enter_context(pipe) for pair in pipes.values() for pipe in pair]
        try:
            for name, (parent_pipe, child_pipe) in pipes.items():
                process = context.Process(
                    target=_executor, name=f"shared-peer-{name}",
                    args=(name, routes, child_pipe, [pipe for pipe in endpoints if pipe is not child_pipe]),
                )
                process.start()
                executors[name] = (process, parent_pipe)
                child_pipe.close()
            asyncio.run(_schedule(routes, executors))
        finally:
            for process, pipe in executors.values():
                if process.is_alive():
                    pipe.close()
            deadline = monotonic() + SHUTDOWN_SECONDS
            for process, _ in executors.values():
                process.join(max(0, deadline - monotonic()))
            for process, _ in executors.values():
                if process.is_alive():
                    process.kill()
                process.join()
