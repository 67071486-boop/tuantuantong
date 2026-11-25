"""`takerapi_dispatch`：打手侧主动回调推送的唯一调度入口（设计文档 §6/§9-11）。`--loop`=常驻轮询（生产形态：systemd 单元 `tuantuantong-takerapi-dispatch` 随发版装/重启，按 `SYNC_UNIT_ENABLE` 只在一台 enable；⛔ 别手工挂 cron、别叠第二个调度器），`--once`=跑一轮（与 `--loop` 互斥、同给响亮报错），`--tenant <schema>`=只跑一个租户（排障）。形状照 `openapi_dispatch`（更早先例 `peer_sync`）抄，数据与台账各自一份；遍历租户 → `tenant_context` → 逐租户独立异常边界。
⛔ 本线没有「与对端水位比较」那回事（别照 `peer_sync` 找不存在的判据、⛔ 别为对齐加水位列）：唯一记录是投递台账 `TakerApiDelivery`（行在=没投出，`done`/`failed`=已结束），重试节奏由本行 `attempts`+`next_attempt_at` 决定（退避 10s/60s/5m/15m/1h/6h，第 7 次失败才终态），没有 `last_ok_at` 健康列、也没有对端回执（回执只有那一次 HTTP 2xx）；告警判据=台账 `failed` 行增长 + 内部端点「最近推送记录」。`spec=TAKER_SPEC` 写死在调用点：`run_round` 是两族共用引擎、一行具体模型都不 import（台账模型/列名/状态值全经 `CredentialSpec` 现取），`spec` 必填 keyword-only、漏传当场 `TypeError`——默认值曾把"传错族"从当场报错降级成静默走错台账（**T1b** 收掉了那个"默认回上家侧"的值），⛔ 不加默认参数、⛔ 不做兜底。
退出码契约（照 `peer_sync`/`jidanpu_sync` 先例）：`--once` 有租户未预期失败 ⇒ `CommandError(returncode=1)`，预期内投递失败（连不上/对方 4xx/5xx/退避等待）是正常噪音、⛔ 不会非零；`--loop` 崩了由 systemd 拉起（`Restart=always`）。⚠️ 管理命令而非 HTTP 端点：`--tenant` 只是运维排障入口，⛔ 不接受请求来源的租户标识（`AGENTS.md` §4）；`app_secret` 一个字不进日志（worker 失败原因也不含密钥，签名只进请求头）。`tuantuantong-callback.service` 是鸡蛋铺回调接收器（`CALLBACK_UNIT_ENABLE`，另一件事），与本线单元不是同一个东西。"""
import time

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.openapi import dispatch
from apps.platform.models import Tenant
from apps.takerapi.credentials import TAKER_SPEC

#: `--loop` 的睡眠间隔（秒）：与上家侧 `openapi_dispatch` / `peer_sync` 同量级
#: （一轮的节奏由退避表决定，不由它决定）。设计文档 §6 逐字要求 10 秒
#: （推送延迟 ≈ 轮询间隔，代价登记在第二期设计文档 §10-4）。
LOOP_SLEEP_SECONDS = 10

#: 汇总行的前缀。⭐ 打手侧自己的（⛔ 不照抄上家侧那句 `{schema}: 已投递 …`）：两条线并排跑在同一台机器上，
#: 运维看 `journalctl` 要一眼分清是哪条线在跑——两句话逐字相同就分不清了。
#: ⚠️ 判据（`tests/test_takerapi_dispatch_command.py::SummaryPrefixTests`）钉的是这个值，改这里那条用例就红（`E-0024`：钉值不钉常量）。
SUMMARY_PREFIX = "打手侧回调投递"


class Command(BaseCommand):
    help = "打手侧主动回调推送（取活 + HTTPS 投递 + 退避重试 + 机会式清理）"

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
            help="常驻轮询（生产形态：由发版装的 systemd 单元 tuantuantong-takerapi-dispatch 跑它）",
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        # ⛔ `--once` 与 `--loop` 不能同时给：那会变成「静默常驻」（`--once` 被忽略）——
        # 宁可响亮报错（与 `peer_sync` / `openapi_dispatch` 同一条纪律）。
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
            # ⛔ 只写异常类型名：`str(exc)` 可能带对方回包 / 凭据片段（与 `openapi_dispatch` 同口径）。
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
        ⚠️ `spec=TAKER_SPEC` 显式写死（`run_round` 的 `spec` 必填 keyword-only，漏传当场 `TypeError`）：本命令是打手侧一族的
        常驻入口，取活/建行/状态/文案都落 `TakerApiDelivery`，⛔ 不会静默按上家侧那张台账办事（理由见模块 docstring）。"""
        summary = dispatch.run_round(spec=TAKER_SPEC)
        self.stdout.write(
            f"{tenant.schema_name}: {SUMMARY_PREFIX} 已投递 {summary['delivered']}"
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
        return False
