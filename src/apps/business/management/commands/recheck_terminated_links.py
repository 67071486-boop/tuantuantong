"""兜底重核**已终止**的上家链路：上家把同一张单重新指派（复活）时重录新单（D-0324）。 **为什么需要它（生产实锤，2026-10-29 诊断）**：上家把已退单的订单**重新指派**给我方账号 ——复用同一张上家单、同一 `orderId`、状态回「待上号 / 代练中」——而「停跟踪 = 往后不为它写任何东西」把这条路堵死；且①段的 24h 窗按**上家录单时间**收窄、③ `_links_due_for_check` 又显式排除 terminated 链路 ⇒ 复活发生在窗外时**每轮的路都到不了它**（窗口洞）。循环内已修的那一半（`sync_my_orders` 的重录分支）只覆盖**窗内**再现的行；本命令按 `key1` 单查（不带窗）逐单核已终止链路，把窗外的那一半也收口——**实现就是**
`jidanpu_upstream.recheck_terminated_links`（⛔ 本命令不另写第二份）。 **为什么做成命令而不是并入每轮③**（登记理由见该函数的 docstring）：terminated 链路随完单量无限增长，并入每轮轮转 = 永久抬高对上游的发数（WAF 风险，2026-10-20 压发数纪律）且稀释在途链路的轮转 ⇒ 照 `jidanpu_member.reconcile_and_queue` / `rescue_gone_mirrors` 的先例做**低频 / 一次性兜底**。需要常态化时再由运维挂低频调度（⛔ 不进常驻同步循环）。 **用法**： - 全量兜底（每绑定每次最多 `--limit`（默认 20）条，`terminated_at` 最旧优先）：
`manage.py recheck_terminated_links` - 定向回补生产两笔卡死单（发布后先跑这个）： `manage.py recheck_terminated_links --schema <schema> --upstream-order-id <上家单号>` `manage.py recheck_terminated_links --schema <schema> --upstream-order-id <上家单号>` - `--binding <pk>` 限定单份绑定；`--schema` 限定单租户。幂等：重录后链路不再终止 ⇒ 重跑不会建第二张单（并发由 `_reassign_resurrected_order` 的 `select_for_update` 兜）；没复活的单**一个字不写**。⭐ 只处理**①式（账号+密码）且启用** 的绑定——`key1` 单查要①式的登录凭据；②式（openapi）绑定不走这一路（stdout 点名跳过）。 ⚠️ **必须自己遍历租户**（同 `rescue_gone_mirrors` 的 Critical）：`apps.business` 是 TENANT_APP，链路 / 订单 / 台账都在**租户 schema**里。"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.business import jidanpu, jidanpu_upstream
from apps.business.models import JidanpuUpstreamBinding

_PASSWORD = JidanpuUpstreamBinding.Mode.PASSWORD


class Command(BaseCommand):
    help = (
        "兜底重核已终止的上家链路：上家重新指派（复活同一张单、状态回在途）⇒ 重录新单"
        "并重置链路（D-0324）；没复活的单一个字不写"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--binding", type=int, default=None,
            help="只处理这一份绑定（JidanpuUpstreamBinding 的 pk）",
        )
        parser.add_argument(
            "--upstream-order-id", default=None,
            help="只核这一张上家单（生产卡死单的定向回补口）",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help=f"每份绑定每次最多核多少条（默认 {jidanpu_upstream.TERMINATED_RECHECK_MAX}，"
                 "最旧优先；这是对上游的发数闸，调大前先想 WAF）",
        )
        parser.add_argument(
            "--schema", default=None,
            help="只处理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )

    def handle(self, *args, **options):
        from django_tenants.utils import get_public_schema_name, tenant_context

        from apps.platform.models import Tenant

        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        only = options["schema"]
        if only is not None:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")

        binding_pk = options["binding"]
        limit = (
            jidanpu_upstream.TERMINATED_RECHECK_MAX
            if options["limit"] is None else max(0, options["limit"])
        )
        binding_seen = binding_pk is None
        reassigned_total = 0
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）。
            with tenant_context(tenant):
                reassigned, seen = self._recheck_schema(
                    tenant.schema_name,
                    binding_pk=binding_pk,
                    upstream_order_id=options["upstream_order_id"],
                    limit=limit,
                )
                reassigned_total += reassigned
                binding_seen = binding_seen or seen
        if not binding_seen:
            raise CommandError(f"没有任何租户里有这个绑定：#{binding_pk}")
        self.stdout.write(f"合计重录 {reassigned_total} 张")

    def _recheck_schema(self, schema_name, *, binding_pk, upstream_order_id, limit):
        """一个租户里的「枚举绑定 → 逐份建连 → 调同一份实现」。回 `(重录数, 绑定是否见到)`。"""
        bindings = JidanpuUpstreamBinding.objects.filter(enabled=True).order_by("pk")
        if binding_pk is not None:
            binding = JidanpuUpstreamBinding.objects.filter(pk=binding_pk).first()
            if binding is None:
                # 这一租户没有它不算错（命令遍历所有租户）；全部租户都没有才由 handle 报错。
                return 0, False
            if not binding.enabled:
                raise CommandError(
                    f"绑定 #{binding_pk} 未启用（spec §8.4 停用即停）⇒ 不打上游、不重录"
                )
            if binding.mode != _PASSWORD:
                raise CommandError(
                    f"绑定 #{binding_pk} 是②式（openapi）绑定，不走这一路"
                    "（`key1` 单查要①式的登录凭据）"
                )
            bindings = [binding]
        reassigned = 0
        seen_any = False
        for binding in bindings:
            if binding.mode != _PASSWORD:
                self.stdout.write(
                    f"{schema_name}: 绑定 #{binding.pk} 是②式（openapi）⇒ 跳过"
                    "（`key1` 单查要①式的登录凭据）"
                )
                continue
            seen_any = True
            # ⚠️ 一份绑定**一次建连**跑完整批（照 `consume_event` 的形态），
            #    ⛔ 不在链路循环里逐单建连——那正是对端 WAF 封 IP 的主因。
            with jidanpu.open_client_with_http(binding) as (sdk, _http):
                outcome = jidanpu_upstream.recheck_terminated_links(
                    binding, client=sdk, now=timezone.now(), limit=limit,
                    upstream_order_id=upstream_order_id, probe_clock=timezone.now,
                )
            for line in outcome.lines:
                self.stdout.write(f"{schema_name}: 绑定 #{binding.pk} · {line}")
            self.stdout.write(
                f"{schema_name}: 绑定 #{binding.pk} · 重录 {outcome.reassigned}、"
                f"仍在终态 {outcome.still_terminal}、读不到 {outcome.missing}、"
                f"游戏对不上 {outcome.skipped}"
            )
            reassigned += outcome.reassigned
        return reassigned, seen_any
