import signal
from time import monotonic, sleep

import psycopg
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django_tenants.utils import get_public_schema_name, schema_context, tenant_context

from apps.accounts.wxpusher_config import legacy_rollback_would_restore_old_settings, load_wxpusher_config
from apps.accounts.wxpusher_hall import has_hall_work
from apps.accounts.wxpusher_notifications import InvalidNoticeDomain, QueuedSendPacer, send_notification_batch
from apps.accounts.wxpusher_wakeup import open_listener, wait_for_hall_wakeup
from apps.platform.models import Tenant

_REGULAR_INTERVAL = 3
_HALL_RECOVERY_INTERVAL = 30
_LISTENER_RETRY_INTERVAL = 5
_STOP_CHECK_INTERVAL = 0.25
_WATCH_HALL_LIMIT = 5


class Command(BaseCommand):
    help = "发送已排队的打手手机通知"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50, help="每轮处理上限，业务通知按条计，大厅和群聊按批计")
        parser.add_argument("--watch", action="store_true", help="监听抢单提醒并处理已到期通知，按 Ctrl+C 停止")

        parser.add_argument("--check-rollback", action="store_true", help="检查切回旧版是否会恢复旧的推送设置")

    def handle(self, *args, limit, watch, check_rollback, **options):
        if check_rollback:
            return self._check_rollback()
        if limit < 1:
            raise CommandError("每轮至少处理一条通知。")
        public_schema = get_public_schema_name()
        stopping = False
        pacer = QueuedSendPacer()

        def request_stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        def should_stop():
            return stopping

        previous_handler = signal.signal(signal.SIGTERM, request_stop) if watch else None
        try:
            if watch:
                self._watch(public_schema, limit, pacer, should_stop)
            else:
                self._run_round(public_schema, limit, pacer, mode="all")
                return
        except KeyboardInterrupt:
            pass
        finally:
            if watch:
                signal.signal(signal.SIGTERM, previous_handler)
        self.stdout.write("已停止处理手机推送。")

    def _run_round(self, public_schema, limit, pacer, *, mode, should_stop=None):
        watching = should_stop is not None
        if watching:
            close_old_connections()
        with schema_context(public_schema):
            tenants = list(Tenant.objects.filter(is_active=True).exclude(schema_name=public_schema))
        configured = more_work = False
        for tenant in tenants:
            if watching and should_stop():
                break
            with tenant_context(tenant):
                if not load_wxpusher_config().configured:
                    continue
                configured = True
                with schema_context(public_schema):
                    domain = tenant.get_primary_domain()
                if domain is None:
                    raise CommandError("找不到已启用工作室的主域名。")
                try:
                    counts = send_notification_batch(
                        domain.domain, limit=limit, pacer=pacer, should_stop=should_stop, mode=mode,
                    )
                except InvalidNoticeDomain as error:
                    raise CommandError(str(error)) from error
                if watching and not more_work and not should_stop():
                    more_work = has_hall_work() if mode == "hall" else bool(counts)
            if counts or not watching:
                self.stdout.write(
                    f"已受理 {counts['sent']}，已跳过 {counts['skipped']}，受理未确认 {counts['unconfirmed']}。"
                )
        if not configured and not watching:
            self.stdout.write("当前未配置打手手机推送。")
        return more_work

    def _watch(self, public_schema, limit, pacer, should_stop):
        listener = None
        next_regular = next_recovery = next_connect = next_hall = monotonic()
        try:
            while not should_stop():
                now = monotonic()
                if listener is None and now >= next_connect:
                    try:
                        listener = open_listener()
                        # Register first, then process publications from before registration.
                        next_hall = next_recovery = now
                    except psycopg.Error as exc:
                        self.stderr.write(f"抢单提醒监听连接失败，定时补捞继续（{type(exc).__name__}）")
                        next_connect = monotonic() + _LISTENER_RETRY_INTERVAL

                recovery_due = now >= next_recovery
                if now >= next_hall or recovery_due:
                    more_hall_work = self._run_round(
                        public_schema, min(limit, _WATCH_HALL_LIMIT), pacer, mode="hall", should_stop=should_stop,
                    )
                    finished = monotonic()
                    if recovery_due:
                        next_recovery = finished + (_HALL_RECOVERY_INTERVAL if listener else _REGULAR_INTERVAL)
                    if more_hall_work and finished >= next_regular and not should_stop():
                        # Give due personal notifications one slot between sustained hall batches.
                        more_regular_work = self._run_round(
                            public_schema, 1, pacer, mode="regular", should_stop=should_stop,
                        )
                        finished = monotonic()
                        next_regular = finished + (0 if more_regular_work else _REGULAR_INTERVAL)
                    next_hall = finished + _STOP_CHECK_INTERVAL if more_hall_work else float("inf")
                    continue

                regular_delay = max(next_regular - now, pacer.remaining())
                timeout = min(_STOP_CHECK_INTERVAL, regular_delay, next_recovery - now, next_hall - now)
                if listener is not None:
                    try:
                        if wait_for_hall_wakeup(listener, timeout):
                            next_hall = monotonic()
                            continue
                    except psycopg.Error as exc:
                        self.stderr.write(f"抢单提醒监听断开，正在重连（{type(exc).__name__}）")
                        listener.close()
                        listener = None
                        next_connect = next_recovery = monotonic()
                        continue
                elif timeout > 0:
                    sleep(min(timeout, max(0, next_connect - now)))

                if regular_delay == 0 and not should_stop():
                    # Finish at most one lower-priority request before checking for a hall signal again.
                    more_work = self._run_round(
                        public_schema, 1, pacer, mode="regular", should_stop=should_stop,
                    )
                    next_regular = monotonic() + (0 if more_work else _REGULAR_INTERVAL)
        finally:
            if listener is not None:
                listener.close()


    def _check_rollback(self):
        schema = settings.WXPUSHER_SUBSCRIBE_TENANT_SCHEMA.strip()
        public_schema = get_public_schema_name()
        if not schema or schema == public_schema:
            return
        with schema_context(public_schema):
            tenant = Tenant.objects.filter(schema_name=schema, is_active=True).first()
        if tenant is not None:
            with tenant_context(tenant):
                unsafe = legacy_rollback_would_restore_old_settings()
            if unsafe:
                raise CommandError(
                    "旧版会恢复旧的 WxPusher 推送或一键登录配置。"
                    "请先清空 shared/.env 的 WXPUSHER_SUBSCRIBE_TENANT_SCHEMA，再回滚。"
                )
