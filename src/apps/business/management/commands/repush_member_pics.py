"""存量补推：给「图比指派晚到」的下游链路一次性排 `member.assign` 补推（E-0282 / B3）。 **为什么需要它**：A 段传图（`member.assign` 的消费路径）只在**指派那一刻**传当时 ready 的截图；上家拉回截图是 300 秒一轮、指派是分钟级动作 ⇒ 「指派早于截图到达」是常态竞态。到图触发（B2，`queue_pic_repush_for_ready_screenshots`）从 起接上 ⇒ **新**到图会自动补推；**本命令补存量**——上线前已经缺图的那批链路（2026-10-24 某租户实测 9 条链路 / 6 张单）没有「新到图」这个边沿可触发。 **圈面（L3 二选一，本处选「按台账判据收窄」）**：一条链路进圈 ⇔ 同时满足
1. 绑定 `enabled=True` 且**①式**（登录式；②式传图二期未接线 ⇒ 不排、不写噪声行）； 2. `JidanpuMemberOrderLink` 已存在（没录到过对方 ⇒ 对面没有那张单，先录单再谈图），且**不在对方大厅**（`in_hall=False`）、**没退成过**（`withdrawn_at` 为空）—— ⭐ 2026-10-24 修复轮 H1 / M2：与 B2 到图触发同两个过滤（大厅档补图口径待拍板；已退成的链路不该再烧额度）； 3. 该单确有 `ready` 截图（订单截图 + 群码，「全传」口径——`MEMBER_PUSH_IMG_TYPES`）； 4. 台账里**没有**「订单截图已传到下家」的成功句（`_note_order_once` 那条 `done`；
已传成功的链路不进圈——重跑消费也是零上游写的空转，没必要排队）。 **幂等口径（M5 改准）**：在飞窗口内重跑 = 零排队（`queue_member_event` 在飞归并）；事件 `done` 后重跑 ⇒ **会**重新排队，但消费是**零上游写**的无害空转（B1 per-pic 判重兜底）——⛔ 不许写「跑两遍第二遍零排队」这种过期口径。 ⛔ **默认 dry-run**（只逐行打印「单号 × 绑定」，一个字不落库）；`--execute` 才排事件。 ⚠️ 实跑是**生产写操作**：发版后、经业务方批准再跑（跑前把 dry-run 清单贴出来）； `--schema` 限定单租户。排的事件与 B2 到图触发**同一形状**（`KIND_ASSIGN` + `rearm=True` + `payload PIC_RE_PUSH_PAYLOAD`，判据唯一出处都在 `jidanpu_member`）。 ⚠️ **必须自己遍历租户**（同 `rescue_gone_mirrors` 的 Critical）：`apps.business` 是 TENANT_APP，链路与台账都在租户 schema 里。"""
from django.core.management.base import BaseCommand, CommandError

from apps.business import jidanpu_member
from apps.business.jidanpu_member import KIND_ASSIGN, PUSH_PIC_DONE_REASON
from apps.business.models import JidanpuMemberOrderLink, JidanpuSyncEvent, OrderScreenshot

#: 「已传到下家」成功句的**稳定前缀**（从唯一出处的文案模板派生：格式占位都在「（」之后，
#: ⛔ 不在这里重抄整句字面量——文案改了前缀会跟着漂）。
_DONE_PREFIX = PUSH_PIC_DONE_REASON.split("（", 1)[0]


class Command(BaseCommand):
    help = (
        "存量补推：给「已建链的①式启用绑定 × 单上有 ready 截图 × 台账无『已传到下家』成功句」"
        "的链路排 member.assign 补推（E-0282）；默认 dry-run，--execute 才落写"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute", action="store_true",
            help="真的排事件（默认 dry-run：只打印会补推谁，不改任何行）",
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

        execute = options["execute"]
        queued_total = 0
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）。
            with tenant_context(tenant):
                queued_total += self._repush_schema(
                    tenant.schema_name, execute=execute,
                )
        verb = "已排补推" if execute else "会补推（dry-run，未落写）"
        self.stdout.write(f"合计 {verb} {queued_total} 条链路")

    def _repush_schema(self, schema_name: str, *, execute: bool) -> int:
        """一个租户里的「枚举候选 →（--execute 才）排事件」。回本轮排掉的链路数。"""
        # ⚠️ `jidanpu_openapi` 只能在函数内 import（环，见 `outbox_line_for` 的说明）；
        #    ⭐ 挪到**循环外**——每次循环体里 import 一遍是纯开销。
        from apps.business import jidanpu_openapi

        links = (
            JidanpuMemberOrderLink.objects.filter(
                binding__enabled=True,
                # ⭐ 还挂在对方大厅（in_hall）与已退成（withdrawn_at 非空）的链路都**不进圈**——与 B2 到图触发的
                # `queue_pic_repush_for_ready_screenshots` 同两个过滤（理由见那边的
                # docstring 第 ③④ 格：大厅档补图口径待拍板、已退成不再烧额度）。
                in_hall=False, withdrawn_at__isnull=True,
            )
            .select_related("binding", "order")
            .order_by("order_id", "binding_id")
        )
        queued = 0
        for link in links:
            order = link.order
            if not OrderScreenshot.objects.filter(
                order=order,
                img_type__in=jidanpu_member.MEMBER_PUSH_IMG_TYPES,
                status=OrderScreenshot.STATUS_READY,
            ).exists():
                continue  # 圈面第 3 格：没有 ready 截图（指派时就没图的另一档由对账兜）
            if JidanpuSyncEvent.objects.filter(
                direction="in", kind=KIND_ASSIGN, target_ref=order.order_no,
                status="done", reason__startswith=_DONE_PREFIX,
                payload__member_binding_id=link.binding.pk,
            ).exists():
                continue  # 圈面第 4 格：已传成功（L3 收窄；不进圈 ⇒ 少一条空转）
            # ⚠️ ①式判据在 helper 里同一条（`mode` 唯一判据）——这里提前跳过②式，
            #    免得 dry-run 清单里出现一条永远排不出去的噪声行。
            if jidanpu_openapi.is_openapi_binding(link.binding):
                continue
            self.stdout.write(
                f"[{'execute' if execute else 'dry-run'}] {schema_name} "
                f"单 {order.order_no} × 绑定 #{link.binding.pk}"
                f"（{link.binding.jidanpu_base_url} / {link.binding.account}）"
            )
            if not execute:
                continue
            jidanpu_member.queue_member_event(
                order, link.binding, kind=KIND_ASSIGN, rearm=True,
                payload=dict(jidanpu_member.PIC_RE_PUSH_PAYLOAD),
            )
            queued += 1
        return queued
