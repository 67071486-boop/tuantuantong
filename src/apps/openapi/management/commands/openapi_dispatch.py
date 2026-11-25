"""`openapi_dispatch`：开放平台主动回调推送的唯一调度入口（设计文档 §7.4/§13.4-13）。`--loop`=常驻轮询（生产形态：systemd 单元 `tuantuantong-openapi-dispatch` 随发版装/重启，按 `SYNC_UNIT_ENABLE` 只在一台 enable；⛔ 别手工挂 cron、别叠第二个调度器），`--once`=跑一轮（与 `--loop` 互斥、同给响亮报错），`--tenant <schema>`=只跑一个租户（排障）。形状照 `peer_sync` 抄（同一条线上跑着的先例）：一条常驻进程是「共用的闹钟」、数据是各自的；遍历租户 → `tenant_context` → 逐租户独立异常边界。
⛔ 与 `peer_sync` 的有意差异——投递语义更松：出站 HTTP 是 at-least-once（§7.4 逐字），对方超时但实际收到 ⇒ 可能重复 ⇒ 对端应按 `delivery_id` 去重（这条必须写进对外文档）；⛔ 不要读成"两台同时跑不会重复"——取活只有 `SELECT … FOR UPDATE SKIP LOCKED` + 300 秒租约，进程崩/卡过租约 ⇒ 同一行会再投一次，幂等是兜底、不是"可以多开一份"的理由。
退出码契约（照 `peer_sync`/`jidanpu_sync` 先例）：`--once` 有租户未预期失败 ⇒ `CommandError(returncode=1)`，预期内投递失败（连不上/对方 4xx/5xx/退避等待）是正常噪音、⛔ 不会非零；`--loop` 崩了由 systemd 拉起（`Restart=always`）⇒ 常驻形态的告警判据是台账 `failed` 行的增长与页面「最近推送记录」（`reason` 列就是给人看的那句话）。⚠️ 管理命令而非 HTTP 端点：`--tenant` 只是运维排障入口，⛔ 不接受请求来源的租户标识（`AGENTS.md` §4）；`app_secret` 一个字不进日志（D-0201 不放松的那格；worker 失败原因也不含密钥，签名只进请求头）。"""
import time

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.integration_sync.delivery import publish_and_enqueue
from apps.openapi import dispatch
from apps.openapi.credentials import OPENAPI_SPEC
from apps.platform.models import Tenant

#: `--loop` 的睡眠间隔（秒）：与 `peer_sync` 同量级（一轮的节奏由退避表决定，不由它决定）。
#: §7.4 逐字要求 `LOOP_SLEEP_SECONDS = 10`（推送延迟 ≈ 轮询间隔，代价已登记）。
LOOP_SLEEP_SECONDS = 10


class Command(BaseCommand):
    help = "开放平台主动回调推送（取活 + HTTPS 投递 + 退避重试 + 机会式清理）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="跑一轮就退（排障 / 手工；⛔ 不是生产调度——生产是下面的 --loop 常驻单元）",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help="常驻轮询（生产形态：由发版装的 systemd 单元 tuantuantong-openapi-dispatch 跑它）",
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        # ⛔ `--once` 与 `--loop` 不能同时给：那会变成「静默常驻」（`--once` 被忽略）——
        # 宁可响亮报错（与 `peer_sync` 同一条纪律）。
        if options.get("once") and options.get("loop"):
            raise CommandError("--once 与 --loop 不能同时给（一个跑一趟、一个常驻）")
        if options.get("loop"):
            while True:
                self._run_once(schema)
                time.sleep(LOOP_SLEEP_SECONDS)
        if self._run_once(schema):
            raise CommandError("有租户的本轮出现未预期失败，详见上方汇总", returncode=1)

    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。⚠️ `--tenant` 是排障闸门。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    def _run_once(self, schema) -> bool:
        """跑一趟所有（或指定的）租户，返回「是否出现过未预期失败」（退出码契约用）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            # ⚠️ 逐租户独立异常边界：一个租户失败**不影响**别的租户。
            # ⛔ 只写异常类型名：`str(exc)` 可能带对方回包 / 凭据片段（与 `peer_sync` 同口径）。
            try:
                with tenant_context(tenant):
                    if self._run_tenant(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界
                # 能逃到这一层的异常**按定义**是未预期的（`run_round` 已接住逐行的异常）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 本轮失败（{type(exc).__name__}），下一个租户继续"
                )
        return unexpected

    def _run_tenant(self, tenant) -> bool:
        """在一个租户里跑一轮（投递内核在 `apps/openapi/dispatch.py`）。回「是否有未预期失败」。
        ⚠️ `spec=OPENAPI_SPEC` 显式写死：`run_round` 的 `spec` 必填 keyword-only、漏传当场 `TypeError`（打手侧一族有它自己的命令收 `spec=TAKER_SPEC`），
        ⛔ 不会静默按上家侧的台账/文案办事（理由见 `apps/openapi/dispatch.py` 模块 docstring）。"""
        publication_failed = False
        try:
            publish_and_enqueue()
        except Exception as exc:  # 保留旧回调的投递机会。
            publication_failed = True
            self.stderr.write(f"{tenant.schema_name}: 变更发布失败（{type(exc).__name__}）")
        summary = dispatch.run_round(spec=OPENAPI_SPEC)
        self.stdout.write(
            f"{tenant.schema_name}: 已投递 {summary['delivered']}"
            f" · 重试 {summary['retried']} · 终态失败 {summary['failed']}"
            f" · 不再投递 {summary['dropped']} · 清理 {summary['pruned']}"
        )
        errors = summary.get("errors") or []
        if errors:
            # ⛔ 不静默：未预期失败必须让运维在终端上也看得见（台账另有留痕）。
            # ⚠️ 转抄的是**异常类型名**；**完整栈**在 `run_round` 的 `logger.exception` 里。
            self.stderr.write(
                f"{tenant.schema_name}: 本轮有 {len(errors)} 条推送未预期失败："
                + "；".join(errors)
            )
            return True
        return publication_failed
