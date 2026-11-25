"""`migrate_member_link_to_survivor`：A 族占链老单的**一次性迁链**命令（D-0370，2026-11-03）。 python manage.py migrate_member_link_to_survivor                     # **干跑**（默认）：只打印清单，一个字节都不改 python manage.py migrate_member_link_to_survivor --apply             # 真迁（仍逐行单事务、仍只改命中的行） python manage.py migrate_member_link_to_survivor --schema <schema> [--apply] ## 要治的病（E-0315 / D-0370）同店一张**老单**（多为已退款）占着 `(binding, jidanpu_order_id)` 链行；同来源的**存活新单**认领建链必撞唯一约束（`uniq_jdp_member_link_remote`）⇒ 新单永远进不了链路
（截图回传 / 状态回传断线），旧实现还把每次认领撞成未预期异常烧满重试。 ## 迁什么（⛔ 判据逐条，宁可漏迁不可错迁） - **只看「链行的现行 order 已死」的那一行**：老单已软删、或状态 ∈ {已退单, 已完成} （`SURVIVOR_EXCLUDED_STATUSES`）。老单还活着 ⇒ 无需迁移（幂等：迁完之后链行指向存活单，重跑同一行自然落在这一档）； - **存活候选** = 全租户内**有效来源单号相同**（`effective_source_order_no`，唯一出处：来源单号优先、空白回落内置单号——与认领预读查的是**同一把钥匙**）的**恰好一张** 存活单（未软删、状态不在排除集、不是老单自己）； **0 张或 ≥2 张（歧义）⇒ 只进「需人工核对」清单，⛔ 不自动迁**； - **存活单在本绑定上已有别的链行**（`uniq_jdp_member_link_order` 会拒）⇒ 同样只进人工清单（⛔ 不删任何既有行来腾位置）。
## 迁的时候写哪几列（--apply） `order_id` 指向存活单，同时把**生命周期三列重置**（`in_hall=False`、`listed_at=None`、 `withdrawn_at=None`）：老单那一段「在厅 / 已退成」的历史不属于新单；链行就位后由对账排出「补抛」、消费侧**先回读远端再行动**（D-0318 前置回读）——迁移本身不打一次上游。⚠️ `jidanpu_order_id` / `binding` 不动（远端单号就是两边共用的那把钥匙）。写库用「链行 pk + 现行 order_id」双条件更新：没打中（并发下刚被人改过）⇒ 不计迁移数、照实转人工核对（⛔ 不静默计数）。 ## ⚠️ 业务方已知的代价（拍板记录，D-0370）老单的时间线查码 / 同步节**不再显示其链路行**（`audit/timeline.py` 按 order 读链） ——老单已退款，业务方已知并接受。 ## 纪律（AGENTS.md §5-A 破坏性动作三条） 1. **默认干跑只打印**：不 `--apply` 就一行都不改； 2. 打印的清单与真改的**是同一批命中行**（同一次枚举出计划，⛔ 不许两套判据）； 3. 只按显式标识（链行 pk + 现行 order 双条件）改，⛔ 没有「时间 / 大小 / 前缀兜底」的第二把刷子。 ## 运行形态 - **逐租户循环**（链行表在租户 schema，public 没有这张表）：`Tenant` 排除 public → 逐个 `tenant_context`；`--schema` 是排障闸门；逐租户独立异常边界，有失败 ⇒ 退出码 1； - **幂等**：重复跑 / 补跑，第二次全落在「跳过」那一档，零写库。"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business.jidanpu_member import effective_source_order_no
from apps.business.models import JidanpuMemberOrderLink, Order, OrderStatus
from apps.platform.models import Tenant

#: 「存活单」不许落的**我方终态**：已退单 / 已完成——链行要迁去的是**还在做**的那张单，
#: ⛔ 不许把一条活链行指到另一张死单上（那是把病换个房间）。
SURVIVOR_EXCLUDED_STATUSES = (
    str(OrderStatus.REFUNDED),
    str(OrderStatus.COMPLETED),
)

#: 输出行三档（人话前缀；排查日志按方括号词筛）。
TAG_MIGRATED = "迁移"
TAG_MANUAL = "人工核对"
TAG_SKIP = "跳过"


@dataclass
class MigratePlan:
    """一行链行的处置计划（干跑打印的清单与 --apply 真改的**同一批**）。"""

    action: str
    link: JidanpuMemberOrderLink
    summary: str
    survivor: Order | None = None


def _fmt_order(order) -> str:
    status = getattr(order, "status", "") or "?"
    deleted = "·已软删" if getattr(order, "deleted_at", None) is not None else ""
    return f"#{order.pk} {order.order_no}（{status}{deleted}）"


def survivor_candidates_for(old_order) -> list:
    """同**有效来源单号**的存活候选（⛔ 判据见模块 docstring「迁什么」一节）。 ⚠️ 先用两键粗查（`source_order_no` / `order_no`）收窄，再在 Python 里按
    `effective_source_order_no` 精筛——「来源单号为空白时回落内置单号」这条口径
    没法写成单个 SQL 条件，精筛是唯一忠实写法。"""
    key = effective_source_order_no(old_order)
    if not key:
        return []
    rows = Order.all_objects.filter(Q(source_order_no=key) | Q(order_no=key))
    return [
        row
        for row in rows
        if row.pk != old_order.pk
        and effective_source_order_no(row) == key
        and row.deleted_at is None
        and str(row.status) not in SURVIVOR_EXCLUDED_STATUSES
    ]


def plan_tenant() -> list[MigratePlan]:
    """本租户全部链行的处置计划（干跑与真改共用这一份，⛔ 两套判据）。"""
    plans: list[MigratePlan] = []
    links = list(JidanpuMemberOrderLink.objects.all().order_by("id"))
    # 一次批量取齐链行引用的单（⛔ 不逐行查）；用 all_objects：软删单正是本命令的
    # 处置对象，默认 manager 会把它们藏掉。
    orders = {
        row.pk: row
        for row in Order.all_objects.filter(pk__in=[link.order_id for link in links])
    }
    for link in links:
        old = orders.get(link.order_id)
        base = (
            f"链行 #{link.pk}：老单 {_fmt_order(old) if old else '#' + str(link.order_id) + ' 不存在'}"
            f" · 远端单号 {link.jidanpu_order_id} · 绑定 #{link.binding_id}"
        )
        if old is None:
            plans.append(MigratePlan(
                action=TAG_MANUAL, link=link,
                summary=f"{base} ⇒ 指向的单不存在（数据残行），不自动迁",
            ))
            continue
        alive = old.deleted_at is None and str(old.status) not in SURVIVOR_EXCLUDED_STATUSES
        if alive:
            plans.append(MigratePlan(
                action=TAG_SKIP, link=link,
                summary=f"{base} ⇒ 老单仍是存活单，无需迁移",
            ))
            continue
        candidates = survivor_candidates_for(old)
        if len(candidates) != 1:
            detail = (
                "同来源存活候选 0 张"
                if not candidates
                else "同来源存活候选 "
                + f"{len(candidates)} 张（{' , '.join(_fmt_order(c) for c in candidates)}）歧义"
            )
            plans.append(MigratePlan(
                action=TAG_MANUAL, link=link,
                summary=f"{base} ⇒ {detail}，不自动迁",
            ))
            continue
        survivor = candidates[0]
        if JidanpuMemberOrderLink.objects.filter(
            binding_id=link.binding_id, order_id=survivor.pk
        ).exists():
            plans.append(MigratePlan(
                action=TAG_MANUAL, link=link,
                summary=(
                    f"{base} ⇒ 候选 {_fmt_order(survivor)} 在本绑定上已有链行"
                    "（唯一约束会拒），不自动迁"
                ),
            ))
            continue
        plans.append(MigratePlan(
            action=TAG_MIGRATED, link=link, survivor=survivor,
            summary=(
                f"{base} ⇒ 存活单 {_fmt_order(survivor)}"
                "（同时重置 in_hall/listed_at/withdrawn_at，不打上游）"
            ),
        ))
    return plans


class Command(BaseCommand):
    help = (
        "A 族占链老单的一次性迁链（D-0370）：链行的现行单已退单/已完成/已软删，"
        "且全租户同有效来源单号的存活候选恰好一张 ⇒ 把链行迁指到存活单（同时重置"
        " in_hall/listed_at/withdrawn_at，不打上游）；候选 0 张或歧义 ⇒ 只列「需人工核对」。"
        "默认干跑只打印清单，--apply 才改；逐租户跑、逐行单事务、幂等。"
        "已知代价：老单的时间线查码/同步节不再显示其链路行（业务方拍板接受）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="真迁（⛔ 缺省是干跑：只打印清单，一个字节都不改）",
        )
        parser.add_argument(
            "--schema", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )

    def handle(self, *args, **options):
        schema = (options.get("schema") or "").strip() or None
        apply_changes = bool(options.get("apply"))
        migrated_total = manual_total = skipped_total = 0
        failed = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    plans = plan_tenant()
                    migrated = manual = skipped = 0
                    for plan in plans:
                        self.stdout.write(f"  [{plan.action}] {plan.summary}")
                        if plan.action == TAG_SKIP:
                            skipped += 1
                            continue
                        if plan.action != TAG_MIGRATED:
                            manual += 1
                            continue
                        if not apply_changes:
                            migrated += 1
                            continue
                        # ⛔ 逐行单事务 + 双条件显式更新（pk + 现行 order）：
                        #    一行迁移一行提交；没打中 = 并发刚改过 ⇒ 转人工（如实计数）。
                        with transaction.atomic():
                            hit = JidanpuMemberOrderLink.objects.filter(
                                pk=plan.link.pk, order_id=plan.link.order_id,
                            ).update(
                                order=plan.survivor,
                                in_hall=False, listed_at=None, withdrawn_at=None,
                            )
                        if hit:
                            migrated += 1
                        else:
                            manual += 1
                            self.stdout.write(
                                "      （写库未打中：链行刚被并发改动，转人工核对）"
                            )
                    mode = "已写库" if apply_changes else "dry-run 未写库"
                    self.stdout.write(
                        f"{tenant.schema_name}: 命中 {len(plans)} 行"
                        f"（跳过 {skipped} / 迁移 {migrated} / 人工核对 {manual}；{mode}）"
                    )
                migrated_total += migrated
                manual_total += manual
                skipped_total += skipped
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（照 prune_sync_events）
                failed = True
                # ⛔ 只写异常类型名：str(exc) 可能带载荷片段（spec §11-S1 同族）。
                self.stderr.write(
                    f"{tenant.schema_name}: 本租户迁链失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        mode = "已写库" if apply_changes else "dry-run 未写库"
        self.stdout.write(
            f"合计（{mode}）：跳过 {skipped_total} / 迁移 {migrated_total} / "
            f"人工核对 {manual_total}"
            + ("" if apply_changes else "（--apply 才真迁；先抽查本清单再决定）")
        )
        if failed:
            raise CommandError("有租户的迁链出现未预期失败，详见上方", returncode=1)

    # ── 跨 schema 枚举（照 prune_sync_events 的既有模式）────────────────────
    def _iter_tenants(self, schema):
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))
