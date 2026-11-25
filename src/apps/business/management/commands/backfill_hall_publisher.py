"""存量回填：给「被人指派过、但发单人是空的」订单从操作日志补回发单人（D-0369）。 ## 为什么需要它（D-0369，2026-11-03 业务方口径） D-0369 起「指派（含改派）也算发单动作」⇒ `order_writes.assign_order` 会直写 `Order.hall_publisher`；但**存量单**在那之前被人指派过、发单人却还空着—— 那些单按新口径应当有发单人。数据源只有操作日志：`assign_order` 对每次指派 / 改派都留一行（动作码 `performance.assign` / `performance.reassign`）。 **人工行的机械判据**：`actor_id__isnull=False`。`record()` 是 fail-closed 的（没有操作人必须显式 `system=True`）⇒ 机器行（代练通占位接手、修复命令等）的 `actor_id` 必为 NULL，被这个过滤**天然排除**——⛔ 不需要（也不该）按 `actor`
文本猜「这行是不是人写的」。 ## 「只补空」是唯一安全口径（有意如此，⛔ 别"顺手"扩成覆盖写）目标行**只取** `hall_publisher IS NULL` 的活单（默认 manager 排除软删单）； **已有发单人的一律不动**——哪怕它的最后一次指派日志比「现有发单人的写入」更新。理由：现有发单人可能来自一次**比日志更晚**的抛大厅动作，而「发单人这一列是什么时候、被哪个动作写成现在这个值」在日志侧**无法判先后**（抛大厅的留痕与指派的留痕是两类动作行，列值本身不带写入时刻）⇒ 拿日志去覆盖非空值就可能把**更新的** 事实改回**更旧的**。只补空则两类错误都不会发生，且天然幂等：回填完 `hall_publisher` 非空 ⇒ 第二次执行命中 0 条、零写入。 ## 写法纪律 - ⛔ **只动 `hall_publisher` 一列**：`QuerySet.update(hall_publisher=…)`，且 `WHERE`
里再带一次 `hall_publisher__isnull=True`（并发下也不覆盖新值）。`updated_by` / `updated_at` / `published_at` 一律不碰——回填是**运维动作**不是业务动作， ⛔ 不许伪造「最后修改人 / 最后修改时刻」。 - ⛔ **不写审计行**（本命令不调 `record()`）⇒ 不牵动操作日志两网的计数钉子；干跑 / 实跑的明细清单由调用方落 `.tools/recon/`（不进 git），stdout 只出统计 + 最多 10 条样例。 - actor 解析：优先按 `actor_id` 精确查 `User`；查不到再按 `actor` 用户名精确匹配；仍查不到（用户已真删）⇒ **跳过**并在统计里单列计数（⛔ 不猜人）。 ## 用法（默认干跑） manage.py backfill_hall_publisher --schema=<租户>            # 干跑：统计 + 样例 manage.py backfill_hall_publisher --schema=<租户> --apply    # 真写 ⚠️ `apps.business` / `apps.audit` 都是 TENANT_APP，订单与日志都在**租户 schema** 里 ⇒ 必须逐个进 `tenant_context`（同 `fix_missing_taker_performance` / `recheck_terminated_links` 的口径）。"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.audit.models import OperationLog
from apps.business.models import Order
from apps.platform.models import Tenant

#: 人工指派的两条动作码（`order_writes.assign_order` 写下的恰好那两个）。
ASSIGN_ACTIONS = ("performance.assign", "performance.reassign")

#: stdout 最多打印多少条样例（明细由调用方落 `.tools/recon/`，不进 git）。
SAMPLE_LIMIT = 10


class Command(BaseCommand):
    help = (
        "给「被人指派过但发单人是空的」存量订单从操作日志补回发单人"
        "（D-0369；默认干跑，--apply 才写；只补空、只动 hall_publisher 一列）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="真写库（默认干跑：只打印统计与样例，不写任何东西）",
        )
        parser.add_argument(
            "--schema", default=None,
            help="只处理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )

    def handle(self, *args, **options):
        apply_write = options["apply"]
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        only = options["schema"]
        if only:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")

        self.stdout.write(
            "回填模式（--apply）" if apply_write
            else "干跑（未写库；--apply 才写）"
        )
        totals = {
            "empty_total": 0, "with_log": 0, "applied": 0,
            "unresolved": 0, "no_log": 0,
        }
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 订单与日志都在租户 schema 里 ⇒ 必须逐个进 tenant_context。
            with tenant_context(tenant):
                counts = self._run_for_tenant(tenant.schema_name, apply=apply_write)
            for key in totals:
                totals[key] += counts[key]

        self.stdout.write(
            f"汇总：空发单人活单={totals['empty_total']} "
            f"其中有指派日志={totals['with_log']} "
            f"{'回填' if apply_write else '可回填'}={totals['applied']} "
            f"解析不到用户跳过={totals['unresolved']} "
            f"无任何指派日志={totals['no_log']}"
        )
        return None

    def _run_for_tenant(self, schema_name, *, apply):
        """一个租户里的「找候选 → 取最新指派日志 → 解析人 → 只补空」。回统计 dict。"""
        # 目标行 = 发单人为空的**活单**（默认 manager 已排除软删单；⛔ 不碰已删单）。
        empty_orders = list(
            Order.objects.filter(hall_publisher__isnull=True)
            .only("pk", "order_no")
            .order_by("pk")
        )
        stats = {
            "empty_total": len(empty_orders),
            "with_log": 0, "applied": 0, "unresolved": 0, "no_log": 0,
        }
        if not empty_orders:
            self.stdout.write(f"[{schema_name}] 空发单人活单=0，无事可做")
            return stats

        # 数据源 = 人工指派日志（`actor_id__isnull=False` 的机械判据，见模块 docstring）。
        # 按 (created_at, id) 升序遍历、逐键覆盖 ⇒ 循环结束时每个单号对应**最新**一条。
        latest: dict[str, OperationLog] = {}
        logs = (
            OperationLog.objects.filter(
                action__in=ASSIGN_ACTIONS,
                actor_id__isnull=False,
                order_no__in=[order.order_no for order in empty_orders],
            )
            .order_by("created_at", "id")
            .only("order_no", "actor_id", "actor", "action", "created_at")
        )
        for log in logs:
            latest[log.order_no] = log

        samples: list[str] = []
        for order in empty_orders:
            log = latest.get(order.order_no)
            if log is None:
                # 从未被（人）指派过（或日志已被清理；只有机器行的也落在这里——
                # 机器行被上面的过滤排除，按口径**不算**「有人指派过」）。
                stats["no_log"] += 1
                continue
            stats["with_log"] += 1
            user = (
                User.objects.filter(pk=log.actor_id).first()
                or User.objects.filter(username=log.actor).first()
            )
            if user is None:
                stats["unresolved"] += 1
                samples.append(
                    f"{order.order_no} → 跳过：日志 actor={log.actor!r}"
                    f"(actor_id={log.actor_id}) 解析不到用户（用户已真删？）"
                )
                continue
            if apply:
                # ⛔ 只动这一列；WHERE 再带一次 isnull ⇒ 并发下也不覆盖新值。
                #    `QuerySet.update()` 不碰 `auto_now` ⇒ `updated_at` 天然不动。
                updated = Order.objects.filter(
                    pk=order.pk, hall_publisher__isnull=True
                ).update(hall_publisher=user.pk)
                if not updated:
                    # 并发把这一单填上了 ⇒ 本轮没写，如实不计入「已回填」。
                    continue
            stats["applied"] += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append(
                    f"{order.order_no} → {'已回填' if apply else '将回填'} "
                    f"{user.username}(pk={user.pk})"
                    f"（依据：{log.action}，日志时刻 {log.created_at:%Y-%m-%d %H:%M:%S}）"
                )

        self.stdout.write(
            f"[{schema_name}] 空发单人活单={stats['empty_total']} "
            f"有指派日志={stats['with_log']} "
            f"{'回填' if apply else '可回填'}={stats['applied']} "
            f"解析不到用户跳过={stats['unresolved']} "
            f"无任何指派日志={stats['no_log']}"
        )
        for line in samples:
            self.stdout.write(f"  {line}")
        return stats
