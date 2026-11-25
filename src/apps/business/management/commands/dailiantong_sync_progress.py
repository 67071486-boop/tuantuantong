"""`dailiantong_sync_progress`：手工把**一张单**的代练通进度图同步进本地「订单截图」。 python manage.py dailiantong_sync_progress --tenant <schema> --order <订单号>
- 只跑指定租户 / 指定订单（排障 / 补数据；常态由 `dailiantong_sync` 每轮自动跑）； - 只处理该单**最新一条有上游单号的推送**；重复执行不会重复建行（幂等键 =
推送行 + 上游进度 ID）； - ⛔ 不写业务状态、⛔ 不改推送行；只新增本地 `OrderScreenshot` 行 + 一条机器审计（有新增时才写）。"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import tenant_context

from apps.business.dailiantong_progress_sync import (
    sync_progress_screenshots_for_push,
)
from apps.business.models import DailiantongPush, Order
from apps.business.process_lock import ProcessLock
from apps.platform.models import Tenant

#: 手工补数据命令的进程锁（M7）：与常驻 `dailiantong_sync` **不同一把**
#: （两者可以并存：常驻那轮跑的是全租户，手工这条只补一张单；但要防两个手工进程同时跑）。
#: 路径口径与 `dailiantong_sync` 一致（同一部署根 `shared/` 下），可用环境变量覆盖。
ENV_PROGRESS_LOCK_PATH = "DAILIANTONG_PROGRESS_LOCK_PATH"
ENV_CALLBACK_ROOT = "DAILIANTONG_CALLBACK_ROOT"
DEFAULT_CALLBACK_ROOT = "/home/ubuntu/projects/tuantuantong"
LOCK_RELATIVE = ("shared", "dailiantong-sync-progress.lock")


def progress_lock_path() -> Path:
    override = os.environ.get(ENV_PROGRESS_LOCK_PATH)
    if override:
        return Path(override)
    root = Path(os.environ.get(ENV_CALLBACK_ROOT) or DEFAULT_CALLBACK_ROOT)
    return root.joinpath(*LOCK_RELATIVE)


class Command(BaseCommand):
    help = "手工同步一张订单的代练通进度图到本地「订单截图」（幂等）"

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="租户 schema（必填）")
        parser.add_argument("--order", required=True, help="我方订单号")

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip()
        order_no = (options.get("order") or "").strip()
        if not schema or not order_no:
            raise CommandError("必须给 --tenant 与 --order")

        # ⭐ M7：进程锁。第二个手工进程立刻退出（⛔ 不并发对同一批推送重复下载 / 重复写行）。
        lock = ProcessLock(progress_lock_path())
        if not lock.acquire():
            raise CommandError(
                "另一个 dailiantong_sync_progress 进程正在运行"
                f"（锁文件：{lock.path}）。请等它结束或确认没有进程在跑后重试。",
                returncode=1,
            )
        try:
            self._run(schema, order_no)
        finally:
            lock.release()

    def _run(self, schema: str, order_no: str) -> None:
        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(f"没有这个租户 schema：{schema}")

        with tenant_context(tenant):
            order = Order.objects.filter(order_no=order_no).first()
            if order is None:
                raise CommandError(f"这个租户里没有订单号 {order_no}")
            push = (
                DailiantongPush.objects.filter(
                    order=order,
                    state__in=DailiantongPush.ACTIVE_STATES,
                )
                .exclude(serial_no__isnull=True)
                .exclude(serial_no="")
                .order_by("-id")
                .first()
            )
            if push is None:
                raise CommandError("这张单没有带上游单号的有效推送，无法同步进度图")
            added, failed, note = sync_progress_screenshots_for_push(push)

        self.stdout.write(
            f"进度图同步：新增 {added} 张，失败 {failed} 张"
            + (f"（{note}）" if note else "")
        )
