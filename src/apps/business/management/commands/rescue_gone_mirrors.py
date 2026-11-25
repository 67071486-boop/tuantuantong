"""复活「已判 `gone`、之后又在上游大厅重现」的上家镜像单（YF-01 受害者恢复）。 **为什么需要它**：YF-01（夜间审查实锤）曾把「上游实际还在挂」的镜像批量标 `gone`—— 终态不复活（模型口径），而拉取轮对终态再现**只记台账、不改状态** ⇒ 已受害的行不会自愈。本命令把「有再现证据」的行**人工**复活回 `listed`，让下一轮拉取照常刷新投影；复活后若它真已不在上游，下一轮全量扫会照既有判据再判一次（⭐ D-0366 起复核 = 该游戏段一跳新鲜段查，不再是 key1 逐单回查），不会永久错下去。 **复活判据只用拉取轮留下的直接痕迹**（⛔ 不猜），**两通道任一**满足即可（R1，2026-10-20 评审必修）： - ① **再现台账**：`JidanpuSyncEvent(kind="upstream.hall_skipped", target_type="hall",
target_ref="<地址>/<单号>", reason=TERMINAL_REAPPEAR_REASON)`，且 `created_at` **不早于** `gone_at`； - ② **镜像自己的 `last_seen_at`**（不早于 `gone_at`）：终态再现分支每轮无条件 `_touch` 刷新它、**不受台账去重限制**。为什么必须要有第二条（R1）：台账唯一写口 `note_once` 按 `(kind, target_type, target_ref, reason)` 去重**永只一行**——能被救回的行必然已有第 1 幕台账 ⇒ 救回后再 gone、再重现时新台账被吞 ⇒ 只认台账的话，第二幕起永远筛空、行永久冻结且 dry-run 静默显 0 条。两条通道的**时间下界都承重**，不是装饰：同一张单可能「判 gone → 重现 →（救回或再次判）→
又一次 gone」，上一次重现的旧痕迹不能给下一次 gone 当证据（救回后判 gone 之前，拉取轮不会再碰它 ⇒ `last_seen_at < gone_at` 恰好筛掉「救回后再也没见过」的行）。 ⛔ **默认 dry-run**：只逐行打印「会复活谁、凭哪条证据」，一个字不落库；`--execute` 才落写。落写是**条件更新**（`status=gone` 才动）⇒ 并发被改走时拿不到就不算，重跑无害。每张复活的行另记一条 `upstream.hall_rescued` 台账（审计口径照 spec §4.4）。 ⭐ **D-0300（2026-10-24 业务方拍板）**：**循环内自动复活已上线**——`sync_hall` 的行循环对 `gone` 行「重新见到即复活」（同一套语义：条件更新 + 清灰卡 + `upstream.hall_rescued` 台账，常量同出 `jidanpu_common`）。本命令保留为**存量批量兜底 / 审计工具**（打「有再现证据但循环还没轮到」的历史行；dry-run 打印本身就是审计面）。 ⚠️ **必须自己遍历租户**（同 `purge_pending_screenshots` 的 Critical）：`apps.business` 是 TENANT_APP，镜像行与台账都在**租户 schema** 里。`--schema` 限定单租户、`--binding` 限定单份绑定（按绑定 pk，镜像按它的 `jidanpu_base_url` 认领——owner 轮换不影响认领）。"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.business.jidanpu_common import (
    KIND_RESCUED,
    RESCUE_REASON,
    TERMINAL_REAPPEAR_REASON,
    write_event,
)
from apps.business.models import JidanpuSyncEvent, JidanpuUpstreamBinding, JidanpuUpstreamHallOrder
from apps.core.datetime_display import local_iso

_LISTED = JidanpuUpstreamHallOrder.Status.LISTED
_GONE = JidanpuUpstreamHallOrder.Status.GONE


def _local_text(moment) -> str:
    """stdout 人话里的时刻：**项目时区** `Y-m-d H:M:S`（D-0302）。 ⚠️ 库里读出的 aware 时刻是 UTC，旧写法裸 `str()` / f-string 出的是 UTC 墙钟
    （比本地钟点差 8 小时）⇒ 统一经 `localtime`；`None` 原样给 `"None"` （旧写法 f-string 对 `None` 也是这个形状，stdout 口径不变）。"""
    if moment is None:
        return "None"
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M:%S")


class Command(BaseCommand):
    help = (
        "复活「已判 gone 且之后在上游重现（有终态再现台账）」的上家镜像单；"
        "默认 dry-run，--execute 才落写"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute", action="store_true",
            help="真的落写（默认 dry-run：只打印会复活谁，不改任何行）",
        )
        parser.add_argument(
            "--binding", type=int, default=None,
            help="只处理这一份绑定的地址（JidanpuUpstreamBinding 的 pk）",
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
        revived_total = 0
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自的 schema 里（见模块 docstring）
            with tenant_context(tenant):
                revived, seen = self._rescue_schema(
                    tenant.schema_name, binding_pk=binding_pk, execute=execute
                )
                revived_total += revived
                binding_seen = binding_seen or seen
        if not binding_seen:
            raise CommandError(f"没有任何租户里有这个绑定：#{binding_pk}")
        verb = "复活" if execute else "会复活（dry-run，未落写）"
        self.stdout.write(f"合计 {verb} {revived_total} 张")

    def _rescue_schema(self, schema_name: str, *, binding_pk: int | None, execute: bool):
        """一个租户里的「枚举候选 → 逐行计划 →（--execute 才）落写」。回 `(已复活数, 绑定是否见到)`。"""
        binding = None
        if binding_pk is not None:
            binding = JidanpuUpstreamBinding.objects.filter(pk=binding_pk).first()
            if binding is None:
                # 这一租户没有它不算错（命令遍历所有租户）；全部租户都没有才由 handle 报错。
                return 0, False

        mirrors = JidanpuUpstreamHallOrder.objects.filter(status=_GONE).order_by("pk")
        if binding is not None:
            mirrors = mirrors.filter(upstream_base_url=binding.jidanpu_base_url)
        if not mirrors.exists():
            return 0, binding is not None

        #: 一次取回全部候选的再现台账（⛔ 不是逐行一查：候选多时是 N 次查询）。
        refs = {
            f"{m.upstream_base_url}/{m.upstream_order_id}"[:191]: m for m in mirrors
        }
        evidence: dict[str, list[JidanpuSyncEvent]] = {}
        rows = JidanpuSyncEvent.objects.filter(
            kind="upstream.hall_skipped", target_type="hall",
            reason=TERMINAL_REAPPEAR_REASON, target_ref__in=refs,
        ).order_by("target_ref", "created_at", "pk")
        for row in rows:
            evidence.setdefault(row.target_ref, []).append(row)

        revived = 0
        for ref, mirror in refs.items():
            # ⛔ 两通道各带时间下界（≥ gone_at）：上一次 gone 幕的旧痕迹不能给新一次
            #    gone 当证据（见模块 docstring「两通道」一节）。
            gone_at = mirror.gone_at
            valid_events = [
                ev for ev in evidence.get(ref, ())
                if gone_at is None or ev.created_at >= gone_at
            ]
            #: 通道②（R1）：`last_seen_at` 由终态再现分支每轮 `_touch` 刷新、不受台账
            #: 去重限制——台账那条通道在「救回后第二幕」必然断流（R1 的缺口本身）。
            reseen = gone_at is None or (
                mirror.last_seen_at is not None and mirror.last_seen_at >= gone_at
            )
            if not valid_events and not reseen:
                continue
            if valid_events:
                # ⚠️ D-0302：这行 stdout **给人看**（救回前先 dry-run 核证据）；
                #    库里读出的时刻是 UTC，旧写法裸 str() 出 UTC 墙钟（差 8 小时）⇒ localtime。
                evidence_note = (
                    f"台账 {len(valid_events)} 条、最近 {_local_text(valid_events[-1].created_at)}"
                )
            else:
                #: 如实标注证据来源（⛔ 不假装有台账）。
                #: ⚠️ 同上：`last_seen_at` 出**项目时区**形状，不再是裸 str 的 UTC 墙钟。
                evidence_note = (
                    f"无新台账（note_once 去重），last_seen_at={_local_text(mirror.last_seen_at)} 见证"
                )
            self.stdout.write(
                f"[{'execute' if execute else 'dry-run'}] {schema_name} "
                f"#{mirror.pk} {ref} gone_at={_local_text(mirror.gone_at)} 证据 {evidence_note}"
            )
            if not execute:
                continue
            updated = JidanpuUpstreamHallOrder.objects.filter(
                pk=mirror.pk, status=_GONE
            ).update(status=_LISTED, gone_at=None)
            if not updated:
                # 候选读完到落写之间被并发改走 ⇒ 拿不到就不算、也不记台账（那条台账会说假话）。
                continue
            revived += 1
            # ⭐ 大厅离厅快照（D-0296）：镜像**复活回大厅**了 ⇒ 删掉它的灰卡（显式
            # filter 删，回删除行数）。不清的话同一张单会「可接区一张 + 灰区一张」
            # 同时出现（一单双卡）。⛔ 这一步失败要响亮但不回滚复活：快照是读面的辅助数据，漏删只是多一张过期灰卡，复活本身不能陪葬。
            try:
                from apps.business import hall_snapshot

                hall_snapshot.clear_mirror_snapshots(mirror)
            except Exception as exc:  # noqa: BLE001 —— 辅助数据失败不回滚复活（响亮留痕）
                self.stderr.write(
                    f"{schema_name}: 镜像 #{mirror.pk} 复活后的快照清理失败"
                    f"（{type(exc).__name__}），可能残留一张过期灰卡"
                )
            payload = {
                "upstream_binding_id": (
                    binding.pk if binding is not None else mirror.owner_binding_id
                ),
            }
            if valid_events:
                payload["evidence_event_id"] = valid_events[-1].pk
            else:
                # ⚠️ D-0302：这条 payload 进**台账行**（同步事件页可查）；
                #    `local_iso` 出项目时区 +08:00 串，替代裸 `isoformat()` 的 UTC 串。
                payload["evidence_last_seen_at"] = local_iso(mirror.last_seen_at)
            write_event(
                direction="in", kind=KIND_RESCUED, status="done",
                target_type="hall", target_ref=ref, reason=RESCUE_REASON,
                payload=payload,
            )
        return revived, binding is not None
