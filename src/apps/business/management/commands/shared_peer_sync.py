import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.business import shared_peer_sync
from apps.platform.models import Domain


@contextmanager
def scheduler_lock(filename):
    path = Path(filename)
    if not path.is_absolute():
        raise CommandError("--lock-file 必须是绝对路径")
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise CommandError(f"不能打开共享同步锁（{type(exc).__name__}）") from None
    with os.fdopen(fd, "rb"):
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise CommandError("--lock-file 必须是普通文件")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise CommandError(f"不能取得共享同步锁（{type(exc).__name__}）") from None
        yield


class Command(BaseCommand):
    help = "运行显式配置租户的共享上下游同步调度"
    requires_system_checks = []

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--loop", action="store_true", help="持续运行（默认模式）")
        mode.add_argument("--check-routes", action="store_true", help="只读验证路由和切换条件")
        mode.add_argument("--check-route-identities", action="store_true", help="只读验证数据库、角色和域名")
        parser.add_argument("--admit-tenant", help="只检查指定新接管租户的聊天切换条件")
        parser.add_argument("--routes-file", required=True)
        parser.add_argument("--lock-file")

    def handle(self, *args, **options):
        filename = options["routes_file"]
        if (
            not settings.TT_SHARED_WEB_ROUTES_FILE
            or Path(settings.TT_SHARED_WEB_ROUTES_FILE) != Path(filename)
            or settings.DATABASES["default"]["ENGINE"] != "django.db.backends.dummy"
        ):
            raise CommandError("TT_SHARED_WEB_ROUTES_FILE 必须指向相同清单，并禁用默认数据库")
        try:
            routes = shared_peer_sync.load_tenant_routes(filename)
            admitting = options["admit_tenant"]
            if admitting and (not options["check_routes"] or admitting not in routes):
                raise CommandError("--admit-tenant 需要 --check-routes 且须在路由清单内")
            if options["check_routes"] or options["check_route_identities"]:
                admission = {admitting} if admitting else routes if options["check_routes"] else ()
                self.check_routes(routes, admission)
                return
            if not options["lock_file"]:
                raise CommandError("持续运行需要 --lock-file")
            with scheduler_lock(options["lock_file"]):
                shared_peer_sync.run_scheduler(routes)
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from None
        except (RuntimeError, EOFError, BrokenPipeError) as exc:
            raise CommandError(f"共享同步退出（{type(exc).__name__}）") from None


    def check_routes(self, routes, admission):
        failures = []
        for schema, route in routes.items():
            try:
                with shared_peer_sync.tenant_database(*route, readonly=True):
                    if schema in admission and (blocker := shared_peer_sync.chat_cutover_blocker()):
                        failures.append(f"{schema}（{blocker}）")
            except (ImproperlyConfigured, DatabaseError, Domain.DoesNotExist) as exc:
                failures.append(f"{schema}（{type(exc).__name__}）")
        if failures:
            raise CommandError("共享同步清单校验失败：" + "、".join(failures))
        self.stdout.write(f"共享同步只读检查通过：{len(routes)} 个租户")
