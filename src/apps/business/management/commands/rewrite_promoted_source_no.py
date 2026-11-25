"""存量补写：把「已转正的 D-0325 隐藏单」的 `source_order_no` 改写为上家展示号（D-0342）。 **为什么需要它**：D-0325 拍板④让隐藏单创建期 `source_order_no`＝上家**内部单号**（防拉取侧回环判据误杀上游原卡——那段论证成立、照旧）；但转正前（2026-10-30 之前）转正支**不改写**这一列 ⇒ 已转正的真实订单按 D-0272 全端口径（搜索 / 列表 / 导出）对不上人在下游看到的展示号（业务方报障：涉事租户两张 IN_PROGRESS 单「搜不到」）。转正点的改写已随 D-0342 上线； **存量**由本命令一次性补写，执行需业务方批准。 **候选判据（六条全满足才改；逐条打印，dry-run 即审计面）**： 1. 镜像行 `status=promoted` 且 `promoted_order` 非空（D-0325 转正的唯一落点形状）；
2. 订单未软删（软删单不可见，改了没人看，跳过）； 3. 镜像报文 `orderIdView` 非空且 ≠ 当前来源号（已合规的跳过——转正点改写上线后转正的单）； 4. **指纹闸**：当前来源号 ∈ {内部号 `upstream_order_id`, 空白}——人工改过的值一律尊重（⛔ 不覆盖客服手填的来源号）； 5. **查重闸**：目标展示号不被本租户**另一张**未删单占用（对齐 `forbid_duplicate_source_order_no` 开关的语义；`orderIdView` 实测会重号（B4），撞了跳过）； 6. **peer 闸**：订单没有 `PeerOrderLink` 行——peer 回传按 `effective_source_order_no` 解析（`peer_inbound._local_order_for_peer_ref`），改写**前**推给 peer 下家的单，对端存的是旧值，
改写会让回传对不上（转正点改写无此风险：peer 推送只发生在转正后）。 **落写**：条件更新（`filter(pk=…, source_order_no=旧值)`）⇒ 重复执行天然幂等（第二次起候选判据 3 就筛掉了）；每改一张记一条台账（`direction="in"`、kind= `upstream.promoted_source_no_rewrite`、`target_ref=order_no`，payload 带 `{internal, view, mirror_pk}`，reason 人话）——台账去重靠判据 3 的幂等，不靠 `note_once`。 ⚠️ **必须自己遍历租户**（同 `rescue_gone_mirrors`）：镜像行与订单都在**租户 schema** 里。 `--schema` 限定单租户、`--binding` 限定单份上家绑定（按绑定 pk）。 ⚠️ **默认实跑**（与 `rescue_gone_mirrors` 的默认 dry-run 相反——本命令按任务书口径带 `--dry-run`；执行本身留业务方批准，先跑一遍 `--dry-run` 核对逐行计划再实跑）。"""
from django.core.management.base import BaseCommand, CommandError

from apps.business.jidanpu_common import write_event
from apps.business.models import (
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    PeerOrderLink,
)

PROMOTED = JidanpuUpstreamHallOrder.Status.PROMOTED

#: 台账 kind（`upstream.*` 族：direction=in、只登记不驱动动作）。⚠️ 列宽 32，本值恰满
#: （⛔ 改长会 `DataError`）；`JidanpuSyncEvent.kind` 是自由 CharField ⇒ 新 kind 零迁移。
KIND_REWRITE = "upstream.promoted_source_rewrite"
REWRITE_REASON = (
    "D-0342 存量补写：转正隐藏单的来源单号由上家内部号改写为上家展示号"
    "（D-0272 口径；转正点改写上线前的存量）"
)


def _payload_view(mirror) -> str:
    """镜像报文里的展示号（`orderIdView`），strip 后的值；缺 / 空回空串。"""
    payload = mirror.upstream_payload if isinstance(mirror.upstream_payload, dict) else {}
    return str(payload.get("orderIdView") or "").strip()


def plan_rewrites(*, binding_pk: int | None = None):
    """枚举一个租户里的改写计划：回 `(候选行列表, 跳过行列表)`，不落任何写。每行是 `(mirror, order, internal, view, skip_reason | "")`——`skip_reason` 空串＝候选、
    非空＝跳过原因（人话，dry-run 一起打印：审计面要看「为什么不动它」）。"""
    mirrors = JidanpuUpstreamHallOrder.objects.filter(
        status=PROMOTED, promoted_order__isnull=False,
    ).select_related("promoted_order").order_by("pk")
    if binding_pk is not None:
        mirrors = mirrors.filter(owner_binding_id=binding_pk)

    candidates: list[tuple] = []
    skipped: list[tuple] = []
    for mirror in mirrors:
        order = mirror.promoted_order
        internal = str(mirror.upstream_order_id or "")
        view = _payload_view(mirror)
        if order.deleted_at is not None:
            skipped.append((mirror, order, internal, view, "订单已软删"))
            continue
        current = str(order.source_order_no or "").strip()
        if not view:
            skipped.append((mirror, order, internal, view, "镜像报文没有展示号，无可改写目标"))
            continue
        if current == view:
            skipped.append((mirror, order, internal, view, "来源号已是展示号（无需改写）"))
            continue
        if current and current != internal:
            skipped.append(
                (mirror, order, internal, view,
                 "来源号已被人工改为其他值，尊重不覆盖（当前值非内部号）")
            )
            continue
        #: ⚠️ 走默认 manager（软删单天然不命中），⛔ 不再叠 `deleted_at__isnull=True`
        #: （与默认 manager 重复、没有用例能弄红——`AGENTS.md` §5-E / E-0021 口径）。
        clash = (
            Order.objects.filter(source_order_no=view)
            .exclude(pk=order.pk).exists()
        )
        if clash:
            skipped.append(
                (mirror, order, internal, view,
                 "目标展示号已被本租户另一张未删单占用（B4 重号档，跳过防撞）")
            )
            continue
        if PeerOrderLink.objects.filter(order=order).exists():
            skipped.append(
                (mirror, order, internal, view,
                 "订单有 peer 链路（对端存的是旧值，改写会让回传对不上），跳过")
            )
            continue
        candidates.append((mirror, order, internal, view, ""))
    return candidates, skipped


class Command(BaseCommand):
    help = (
        "存量补写：把已转正的 D-0325 隐藏单的来源单号改写为上家展示号"
        "（D-0342；--dry-run 只打印不改任何行）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只逐行打印「会改谁、从什么改成什么」，一个字不落库",
        )
        parser.add_argument(
            "--binding", type=int, default=None,
            help="只处理这一份上家绑定（JidanpuUpstreamBinding 的 pk）",
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
        dry_run = options["dry_run"]
        #: ⚠️ 绑定存在性只能**逐租户**看（租户 schema 的表在 public 查不到）；全租户都没见才报错。
        binding_seen = binding_pk is None

        total = 0
        for tenant in tenants.order_by("schema_name"):
            with tenant_context(tenant):
                count, seen = self._rewrite_schema(
                    tenant.schema_name, binding_pk=binding_pk, dry_run=dry_run,
                )
                total += count
                binding_seen = binding_seen or seen
        if not binding_seen:
            raise CommandError(f"没有任何租户里有这个绑定：#{binding_pk}")
        #: dry-run 报**候选数**（那是审计面要的数）；实跑报真实落写数（并发改走的不算）。
        noun = "会改写（dry-run，未落库）" if dry_run else "已改写"
        self.stdout.write(f"合计 {noun} {total} 张")

    def _rewrite_schema(self, schema_name: str, *, binding_pk: int | None,
                        dry_run: bool) -> tuple[int, bool]:
        """一个租户里的「枚举候选 → 逐行计划 →（非 dry-run 才）落写」。
        回 `(计数, 绑定是否见到)`：dry-run 计数 = **候选数**；实跑 = 真正落写的张数。"""
        if binding_pk is not None and not JidanpuUpstreamBinding.objects.filter(
            pk=binding_pk
        ).exists():
            # 这一租户没有它不算错（命令遍历所有租户）；全部租户都没有才由 handle 报错。
            return 0, False
        candidates, skipped = plan_rewrites(binding_pk=binding_pk)
        for mirror, order, internal, view, reason in skipped:
            self.stdout.write(
                f"[skip] {schema_name} order#{order.pk} internal={internal} view={view}：{reason}"
            )
        count = 0
        for mirror, order, internal, view, _reason in candidates:
            mode = "dry-run" if dry_run else "execute"
            self.stdout.write(
                f"[{mode}] {schema_name} order#{order.pk} {order.order_no} "
                f"{order.status} source={internal} -> {view}（镜像 #{mirror.pk}）"
            )
            if dry_run:
                count += 1
                continue
            #: ⚠️ 条件更新走默认 manager（软删单天然不匹配 ⇒ 拿不到就不算），
            #: ⛔ 不再叠 `deleted_at__isnull=True`（同上，E-0021 口径）。
            updated = Order.objects.filter(
                pk=order.pk, source_order_no=str(order.source_order_no or ""),
            ).update(source_order_no=view)
            if not updated:
                # 枚举到落写之间被并发改走 ⇒ 拿不到就不算、也不记台账（那条台账会说假话）。
                self.stderr.write(
                    f"{schema_name}: order#{order.pk} 落写前来源号已变化，本轮跳过"
                )
                continue
            count += 1
            write_event(
                direction="in", kind=KIND_REWRITE, status="done",
                target_type="order", target_ref=order.order_no,
                reason=REWRITE_REASON,
                payload={"internal": internal, "view": view, "mirror_pk": mirror.pk},
            )
        return count, True
