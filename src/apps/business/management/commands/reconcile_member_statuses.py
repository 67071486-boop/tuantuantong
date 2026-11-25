"""**一次性补数**：逐单重放「查权威 → 判 → 写」（下家②式 / 2026-10-22 停滞事故的收尾，计划 G4）。 ## 为什么需要它（⛔ 不是「再写一套对账」） 2026-10-22 的事故里，在途 4767 条有 4749 条**从未回拉**（`last_in_at IS NULL`），其中 526 张停在「待抢单」。修好窗口之后（G1–G3）这些单**会**在接下来的一圈里被轮到，但那需要几小时（在途 4700+ 条时旧单周期约 6.8 小时）。本命令是**人工加速**：对指定的一个租户（必要时再指定一份绑定）**立刻**把这些单查一遍。 - `--schema` **必填**：绑定主键在每个租户库里都从 1 开始（`models.py` 的绑定表），
不带 schema 会在**每个租户**各命中一条同号绑定 —— 那是跨租户误操作。 - **默认 dry-run**：只打印「单号 / 上游状态 / 会不会触发机器接手、接手给谁」，**一个字都不落库**； `--execute` 才真的写。 - **逐单 try**：一张单查不动只影响它自己（打印 + 继续下一张），⛔ 不套整批事务（一张失败不该把整批的写入一起回滚掉，那会让人误以为「一张都没写」）。 - **限速**：每批 ≤ `--batch`（默认 50）+ 批间 `--sleep` 秒；**每批之前**看一次 `jidanpu_openapi.credential_backoff_reason` —— 这份凭据刚被①档拒过就**整份停下**
（⛔ 不拿一个必被拒的接口硬打：那正是 spec §9.5 ② 要防的形态）。 ## 为什么 dry-run 也要真查上游「会不会被机器接手」不是从回包里推出来的：它取决于**上游此刻的状态** × **我方此刻的状态** （`wants_machine_claim`，与写入那一段**同一个**判据）。⇒ dry-run 必须真查一次 `GetOrderStatus`（只读端点），否则打印出来的只能是猜的。⚠️ 这意味着 dry-run **也会消耗上游只读配额** （每单一次 `GetOrderStatus`）⇒ 已按 `--batch` / `--sleep` 限速，⛔ 别拿它当「零成本预览」。 ## 它**不**做什么 - ⛔ 不自己判「该写什么」：整段仍是 `jidanpu_member_openapi._apply_member_status` （与回调 / 兜底对账**同一份**）。 - ⛔ 不改候选判据：候选仍是 `jidanpu_member.pullable_links`（下家线自己那条「处理集合」）。 - ⛔ 不碰 ①式绑定、⛔ 不碰上家线。"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone as django_timezone
from django_tenants.utils import tenant_context

from apps.business import (
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_openapi,
    jidanpu_openapi_consume,
)
from apps.business.jidanpu_openapi_consume import (
    OUTCOME_APPLIED,
    OUTCOME_NOTED,
    OUTCOME_SKIPPED,
)
from apps.business.models import JidanpuMemberBinding, OrderStatus
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = (
        "逐单补拉下家②式绑定的权威状态（默认 dry-run：打印单号/上游状态/会不会机器接手；"
        "--execute 才真的写）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema", required=True,
            help="租户 schema 名（**必填**：绑定主键在每个租户库里都从 1 开始）",
        )
        parser.add_argument(
            "--binding", type=int, default=None,
            help="只处理这一份绑定（JidanpuMemberBinding 的 pk；默认：本租户全部②式绑定）",
        )
        parser.add_argument(
            "--only-waiting", action="store_true",
            help="只处理我方还停在「待抢单」的单（2026-10-22 事故里那 526 张的形态）",
        )
        parser.add_argument(
            "--batch", type=int, default=50,
            help="每批最多查几张（默认 50；每批之前看一次凭据退避窗口）",
        )
        parser.add_argument(
            "--sleep", type=float, default=1.0,
            help="批间停几秒（默认 1；0 = 不停）",
        )
        parser.add_argument(
            "--execute", action="store_true",
            help="真的写（默认 dry-run：只打印计划，不落库）",
        )

    def handle(self, *args, **options):
        schema = options["schema"]
        if options["batch"] <= 0:
            raise CommandError("--batch 必须是正整数")
        if options["sleep"] < 0:
            raise CommandError("--sleep 不能是负数")
        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(f"没有这个 schema：{schema}")
        with tenant_context(tenant):
            self._run_in_tenant(**options)

    # ── 一个租户 ─────────────────────────────────────────────────────────────

    def _run_in_tenant(self, *, schema, binding, only_waiting, batch, sleep, execute,
                       **_unused):
        bindings = JidanpuMemberBinding.objects.all().order_by("pk")
        if binding is not None:
            bindings = bindings.filter(pk=binding)
            if not bindings.exists():
                raise CommandError(f"租户 {schema} 里没有这份绑定：#{binding}")
        rows = [row for row in bindings if jidanpu_openapi.is_openapi_binding(row)]
        if binding is not None and not rows:
            raise CommandError(f"绑定 #{binding} 不是②式（官方API）绑定 ⇒ 本命令不处理")
        verb = "补拉" if execute else "会补拉（dry-run，未落写）"
        for row in rows:
            self._run_binding(
                row, only_waiting=only_waiting, batch=batch, sleep=sleep, execute=execute,
                verb=verb,
            )

    # ── 一份绑定 ─────────────────────────────────────────────────────────────

    def _run_binding(self, binding, *, only_waiting, batch, sleep, execute, verb):
        head = f"绑定 #{binding.pk}（{binding.user.username}）"
        if not binding.enabled:
            # ⚠️ 停用的绑定在调度里根本不会跑 ⇒ 这里也⛔ 不偷偷替它补（那等于绕过用户的开关）。
            self.stdout.write(f"{head}：已停用 ⇒ 跳过")
            return
        cache_key = jidanpu_openapi.openapi_cache_key(binding)
        if cache_key is None:
            self.stdout.write(f"{head}：凭据不全 ⇒ 跳过（⛔ 一次上游都不打）")
            return

        links = jidanpu_member.pullable_links(binding).select_related("order").order_by("pk")
        if only_waiting:
            links = links.filter(order__status=OrderStatus.WAITING_GRAB)
        total = links.count()
        self.stdout.write(f"{head}：候选 {total} 张，{verb}")
        if not total:
            return

        queried = applied = skipped = failed = 0
        for offset in range(0, total, batch):
            # ⭐ **每批之前**看一次退避窗口（spec §9.5 ②）：刚被①档拒过就整份停下，
            # ⛔ 不拿一个必被拒的接口硬打。
            blocked = jidanpu_openapi.credential_backoff_reason(cache_key)
            if blocked:
                self.stdout.write(f"{head}：停止 —— {blocked}")
                break
            chunk = list(links[offset:offset + batch])
            now = django_timezone.now()
            with jidanpu_openapi_consume.status_queries(binding, max_orders=batch) as queries:
                for link in chunk:
                    outcome = self._one(binding, link, queries, now=now, execute=execute)
                    if outcome is None:
                        # 查不动 / 未预期（`_one` 已经打印了那一行）——只影响这一张。
                        failed += 1
                        continue
                    queried += 1
                    if outcome == OUTCOME_APPLIED:
                        applied += 1
                    elif outcome == OUTCOME_SKIPPED:
                        skipped += 1
                    elif outcome == OUTCOME_NOTED:
                        failed += 1
            if sleep and offset + batch < total:
                time.sleep(sleep)
        self.stdout.write(
            f"{head}：查 {queried}/写 {applied}/跳过 {skipped}/要人看 {failed}"
        )

    # ── 一张单 ───────────────────────────────────────────────────────────────

    def _one(self, binding, link, queries, *, now, execute) -> str:
        """这一张单：dry-run 只打印，`--execute` 走**与回调同一条**写入面。逐单 try。
        回 `OUTCOME_*`（有结论）；`None` = 这一张查不动 / 未预期（调用方只算它一张失败）。"""
        order_no = link.jidanpu_order_id
        order = link.order
        if execute:
            try:
                outcome = jidanpu_member_openapi._apply_member_status(
                    binding=binding, order=order, link=link, order_no=order_no,
                    queries=queries, now=now, findings=[],
                    trigger=jidanpu_member.TRIGGER_RECONCILE,
                )
            except Exception as exc:  # noqa: BLE001 —— **逐单**边界（一张坏单不许带走整批）
                self._stderr(
                    f"  {order.order_no}（远端 {order_no}）未预期失败："
                    f"{jidanpu_openapi_consume._failure_detail(exc)}"
                )
                return None
            order.refresh_from_db(fields=["status"])
            self.stdout.write(f"  {order.order_no}（远端 {order_no}）⇒ {outcome}，我方={order.status}")
            return outcome

        # ── dry-run：只读一次权威状态，然后按**同一个**判据算「会不会机器接手」 ──
        try:
            token = queries.status_of(order_no)
        except Exception as exc:  # noqa: BLE001 —— 逐单边界
            self._stderr(
                f"  {order.order_no}（远端 {order_no}）查不动："
                f"{jidanpu_openapi_consume._failure_detail(exc)}"
            )
            return None
        if token is None:
            # 撞了本批的查询上限（正常不会：每批都新建一个 queries，且 batch == 本批张数）。
            self._stderr(f"  {order.order_no}（远端 {order_no}）撞上批上限，本轮没查")
            return None
        token_text = str(token).strip()
        mapped = jidanpu_member.map_remote_status(token_text)
        will_claim = jidanpu_member_openapi.wants_machine_claim(
            mapped=mapped, token=token_text, order_status=order.status,
        )
        takeover = f"会 → {binding.user.username}" if will_claim else "不会"
        self.stdout.write(
            f"  {order.order_no}（远端 {order_no}）上游={token_text or '（空）'}"
            f" 我方={order.status} 机器接手={takeover}"
        )
        return "queried"

    def _stderr(self, text: str) -> None:
        self.stderr.write(text)
