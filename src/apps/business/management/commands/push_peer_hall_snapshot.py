"""把当前**全部在挂单**（`WAITING_GRAB`）逐条铺底推给下家的大厅镜像（D-0303，spec §2.3）。 **为什么需要它**：大厅镜像的触发点只挂在「状态**变化**」上（进厅 / 出厅 / 改价）—— 开关打开（或本功能发版）那一刻**已经在厅**的单，没有任何事件会再为它们发一次 `hall.throw` ⇒ 下家的大厅缺存量。本命令把「当下在厅」的全集逐条排一次 `hall.throw`
补齐；去重照入队函数的 pending 形状 ⇒ **重跑无害**（已排过且未投的不会重复）。 ⛔ **默认 dry-run**：只逐行打印「会给哪个绑定排哪些单」，一个字不落库；`--execute` 才落写。落写只写 `PeerSyncEvent` 的 `pending` 行（真投递照旧由常驻 `peer_sync` worker 做）——⚠️ 这是**写操作**，按发版纪律**先批后跑**（spec §2.3：发版后由运维
人工跑一次）。 ⚠️ **必须自己遍历租户**（照 `rescue_gone_mirrors` / `purge_pending_screenshots` 的先例）： `apps.business` 是 TENANT_APP，订单与绑定都在**租户 schema** 里。`--schema` 限定单租户、 `--binding` 限定单份绑定（`PeerDownstreamBinding` 的 pk；绑定 pk 各租户各自从 1 起， ⛔ 不给 `--schema` 时同一个数字会在多个租户里各认各的）。 ⚠️ 只推 `state=ACTIVE` 且 `hall_push_enabled=True` 的绑定（入队函数自己**再判一次**，命令传单个绑定也绕不过开关，spec §5）。"""
from django.core.management.base import BaseCommand, CommandError

from apps.business.models import Order, OrderStatus, PeerDownstreamBinding
from apps.business.peer_sync import HALL_THROW_KIND


class Command(BaseCommand):
    help = (
        "把当前全部「待抢单」铺底推给下家的大厅镜像（排 hall.throw）；"
        "默认 dry-run，--execute 才落写"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute", action="store_true",
            help="真的落写（默认 dry-run：只打印会推什么，不写任何行）",
        )
        parser.add_argument(
            "--binding", type=int, default=None,
            help="只推给这一份下游绑定（PeerDownstreamBinding 的 pk）；缺省 = 全部生效且开了大厅推送的绑定",
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
        execute = options["execute"]
        binding_seen = binding_pk is None
        pushed_total = 0
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）。
            with tenant_context(tenant):
                pushed, seen = self._push_schema(
                    tenant.schema_name, binding_pk=binding_pk, execute=execute
                )
                pushed_total += pushed
                binding_seen = binding_seen or seen
        if not binding_seen:
            raise CommandError(f"没有任何租户里有这个绑定：#{binding_pk}")
        verb = "已排" if execute else "会排（dry-run，未落写）"
        self.stdout.write(f"合计 {verb} {pushed_total} 条 hall.throw")

    def _push_schema(self, schema_name: str, *, binding_pk: int | None, execute: bool):
        """一个租户里的「枚举绑定 → 枚举在挂单 →（--execute 才）逐条排」。回 `(已排数, 绑定是否见到)`。"""
        from apps.business.peer_dispatch import enqueue_hall_event

        bindings = PeerDownstreamBinding.objects.filter(
            state=PeerDownstreamBinding.State.ACTIVE,
            hall_push_enabled=True,
        ).order_by("pk")
        if binding_pk is not None:
            # ⚠️ 先**不带**开关过滤查一次：「绑定不存在」与「绑定存在但没开推送」
            # 是两句话（把后者报成前者会让运维去查拼写）。点名一个没开推送 /
            # 已解除的绑定 ⇒ 响亮报错（⛔ 不是静默 0 条——那会让"为什么没铺上" 变成一场排障）。
            named = PeerDownstreamBinding.objects.filter(pk=binding_pk).first()
            if named is None:
                # 这一租户没有它不算错（命令遍历所有租户）；全部租户都没有才由 handle 报错。
                return 0, False
            if (
                named.state != PeerDownstreamBinding.State.ACTIVE
                or not named.hall_push_enabled
            ):
                raise CommandError(
                    f"绑定 #{binding_pk}（{named.peer_base_url}）未开启大厅推送"
                    "或已解除，不推（spec §5 的开关不绕过）"
                )
            bindings = bindings.filter(pk=binding_pk)
        if not bindings.exists():
            return 0, binding_pk is None

        # 在挂单全集（spec §2.3 逐字：「当前全部 WAITING_GRAB 单」）。默认 manager 天然排除已软删（D-0144）；`order_by` 给确定顺序，dry-run 打印可复核。
        # ⭐（评审阻断 2）：镜像卡派生的**隐藏单**（`Order.hall_card`）不是本租户的原生在厅单——它在卡落厅那一刻已经由 `hall_shadow.ensure_for_card` 走过一次
        # `queue_hall_launch`；铺底若按裸 `WAITING_GRAB` 再抛一次，下家大厅会收到两张同内容的卡（`PeerDownstreamBinding` 那一路与 `JidanpuMemberBinding` 那一路都会重复）。 ⇒ 显式排除，⛔ 不靠「隐藏单没有 MAIN 行」这类隐式形状。
        orders = Order.objects.filter(
            status=OrderStatus.WAITING_GRAB
        ).exclude(hall_card__isnull=False).exclude(
            upstream_hall_card__isnull=False
        ).order_by("pk")
        if not orders.exists():
            return 0, True

        pushed = 0
        for binding in bindings:
            for order in orders:
                self.stdout.write(
                    f"[{'execute' if execute else 'dry-run'}] {schema_name} "
                    f"绑定 #{binding.pk}（{binding.peer_base_url}）⇐ "
                    f"{order.order_no}（{order.game.name if order.game else '无游戏'}）"
                )
                if not execute:
                    continue
                made = enqueue_hall_event(
                    order, kind=HALL_THROW_KIND, bindings=[binding]
                )
                pushed += len(made)
        return pushed, True
