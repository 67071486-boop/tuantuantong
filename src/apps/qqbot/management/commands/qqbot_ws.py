"""`qqbot_ws`：QQ 机器人的调度入口。

    python manage.py qqbot_ws --once # 对所有租户各抽一轮发件箱（不连 WS）就退
    python manage.py qqbot_ws --tenant <schema> # 只跑一个租户（排障）
    python manage.py qqbot_ws --loop # 常驻（生产形态：systemd 单元拉起）

骨架照 `apps/business/management/commands/jidanpu_sync.py`：

- 遍历租户 → 每个租户在自己的 `tenant_context` 里跑 → **逐租户独立异常边界**
  （一个租户失败不影响别的租户，失败写 stderr、⛔ 不静默）；
- `--once` 与 `--loop` 互斥（宁可响亮报错，不做「静默常驻」）；
- ⛔ 不接受任何请求来源的租户标识：`--tenant` 是运维排障入口，跟 jidanpu_sync 同口径。

**无 binding 时空转不报错**：发版到还没配置机器人的租户机就是这种状态——
`--once` 一行不抽、`--loop` 起零个线程，都算正常。
"""
from __future__ import annotations

import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.platform.models import Tenant
from apps.qqbot.models import QQBotBinding
from apps.qqbot.worker import BotWorker, GroupSender

logger = logging.getLogger(__name__)

#: --loop 的重扫周期（秒）：新 binding 起线程、disabled 停线程。
RESCAN_SECONDS = 30.0


class Command(BaseCommand):
    help = "QQ 机器人：--once 抽一轮发件箱（不连 WS）；--loop 常驻（reader+sender 线程对）。"

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--once",
            action="store_true",
            help="对所有（或指定的）租户各抽一轮发件箱就退出（不连 WS 网关）。",
        )
        group.add_argument(
            "--loop",
            action="store_true",
            help="常驻：按租户扫描启用的 binding，每个起一对线程，每 30 秒重扫。",
        )
        parser.add_argument(
            "--tenant",
            default=None,
            help="只跑这个租户 schema（排障闸门；⛔ 不提供别名）。",
        )

    def handle(self, *args, **options):
        schema = options.get("tenant")
        if options.get("once") and options.get("loop"):
            # argparse 的互斥组已经拦了；这里再显式说一遍（照 jidanpu_sync 的口径）。
            raise CommandError("--once 与 --loop 不能同时给（一个跑一趟、一个常驻）")
        if options.get("loop"):
            self._loop(schema)
            return
        self._once(schema)

    # ── 租户清单（照 jidanpu_sync._iter_tenants）───────────────────────────

    def _iter_tenants(self, schema):
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    # ── --once：各租户抽一轮发件箱 ─────────────────────────────────────────

    def _once(self, schema) -> None:
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    self._drain_tenant_once()
            except Exception as exc:  # noqa: BLE001 - 逐租户异常边界
                # ⛔ 只写异常类型名：str(exc) 可能带上游回包 / 凭据片段。
                self.stderr.write(
                    f"{tenant.schema_name}: 本轮失败（{type(exc).__name__}）"
                )

    def _drain_tenant_once(self) -> None:
        """本租户所有启用 binding 各抽一轮（不连 WS）。无 binding ⇒ 什么都不做。"""
        bindings = QQBotBinding.objects.filter(enabled=True)
        for binding in bindings:
            GroupSender(binding).drain()

    # ── --loop：常驻，按周期重扫 ───────────────────────────────────────────

    def _loop(self, schema) -> None:
        workers: dict = {}
        while True:
            try:
                workers = self._reconcile_workers(schema, workers)
            except Exception:  # noqa: BLE001 - 主循环自身不断
                logger.exception("qqbot_ws 重扫失败（30 秒后重试）")
            time.sleep(RESCAN_SECONDS)

    def _reconcile_workers(self, schema, workers: dict) -> dict:
        """让线程集合收敛到「所有启用 binding」：新的起、没了的停。"""
        desired: dict = {}
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    for binding_id in QQBotBinding.objects.filter(
                        enabled=True
                    ).values_list("pk", flat=True):
                        desired[(tenant.schema_name, binding_id)] = tenant
            except Exception as exc:  # noqa: BLE001 - 逐租户异常边界
                self.stderr.write(
                    f"{tenant.schema_name}: 扫描失败（{type(exc).__name__}）"
                )
        for key, worker in list(workers.items()):
            if key not in desired:
                worker.stop()
                del workers[key]
        for key, tenant in desired.items():
            if key not in workers:
                worker = BotWorker(tenant, key[1])
                worker.start()
                workers[key] = worker
        return workers
