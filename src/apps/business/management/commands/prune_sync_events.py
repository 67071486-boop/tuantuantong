"""`prune_sync_events`：同步台账（`PeerSyncEvent` / `JidanpuSyncEvent`）的**保留期清理**。 python manage.py prune_sync_events                 # **dry-run**（默认）：只报数，一个字节都不删 python manage.py prune_sync_events --commit        # 真删（仍逐租户、仍只删命中的行、**分批**删） python manage.py prune_sync_events --tenant <schema> [--commit] python manage.py prune_sync_events --days 180 --commit   # 显式保留期（两张表同用；排障口径） python manage.py prune_sync_events --dry-run       # 显式声明只报数（与 --commit 同给时 dry-run 赢） ## 保留期（两张表**各有出处**，⛔ 不是拍脑袋） - **`PeerSyncEvent` 默认 90 天**（审计 M-11）：终态（`done` / `failed`）超期的 **非证据行**才清； - **`JidanpuSyncEvent` 默认 30 天**（业务方拍板，**D-0349**）：终态（`done` / `failed` / `skipped`）超期即清。此前随 M-11 用 90 天；拍板改 30 天的动因是
这张台账「只增不减」（生产 ~10.3 万行）长期膨胀拖累库 CPU，30 天足够覆盖「同一条人话台账重写一次」的去重窗口（`jidanpu_upstream._note_game_ambiguity`）； - `pending` **永不删**（两张表同一条，候选集 WHERE 只含终态，没有任何时间 / 大小兜底）； - `--days N` **显式给出时两张表同用 N**（排障口径）；缺省各表用各表的默认。 ## ⛔ 证据行**不删**（承重；清单从代码常量枚举，不是拍脑袋） - **`hall.pull`（全部）**：上家大厅拉取端点为真正返回的每张卡写的**抢单证据** （`peer_sync.HALL_PULL_KIND`）。抢单判据是「推送过**或**拉取过」——删了它，老卡被 claim 时会因「没有证据」被误拒（Z20 的闸）。 - **`hall.throw` 且 `status=done` 且 `reason=''`**：推送证据（`peer_sync.HALL_THROW_KIND`；空原因才是「真送达」，`done` + 非空原因是 take 作废、`failed` 是没送达——那两类**可以**删）。 - **`peer.hall_hop_limit`**（`peer_inbound.HOP_LIMIT_LEDGER_KIND`）：转发跳数超限的台账，人工排查「链上为什么没出现这张卡」唯一依据（计划点名的证据行）。 ## 调度（D-0349 起，本命令随 `tuantuantong-ledger-prune.timer` 自动跑）
每台租户机每天 03:30±5min 触发一次 `prune_sync_events --commit`（**不带 `--days`** ⇒ 用上面各表自己的默认），逐租户清**本机**的台账（命令自己枚举本机租户，与备份「own tenant DB」同一口径）。本机跑不跑由 `LEDGER_PRUNE_UNIT_ENABLE` （默认 1，`$R/shared/ledger-prune-unit.env`）决定；随发版自动装 / 回滚随回滚重启，四处接线由 `src/tests/test_deploy_sync_units.py::LedgerPruneTimerUnitTests` 守。 ## 纪律（`AGENTS.md` §5-A 破坏性动作三条） 1. **默认 dry-run 只报数**：不 `--commit` 就什么都不删——先看命中数再决定； `--dry-run` 与 `--commit` 同给时**以 dry-run 为准**（零写入优先，矛盾时不猜）； 2. **删除前同 `WHERE` 打命中数**：count 与 delete 用**同一个 queryset**（`_peer_candidates` / `_jidanpu_candidates`），⛔ 不许"报数一套条件、删除另一套"； 3. **按显式条件删**：终态 + `created_at < now - days` + 非证据行，⛔ 没有任何「时间 / 大小 / 前缀兜底」的第二把刷子。 ## 运行形态 - **逐 schema 循环**（照 `jidanpu_sync` 的跨 schema 枚举模式——台账在**租户库**里，平台 public schema 没有这两张表）：`Tenant` 排除 public → 逐个 `tenant_context`； - 逐租户独立异常边界：一个租户失败不挡别的租户；有未预期失败 ⇒ 退出码 1（照 `peer_sync` / `jidanpu_sync` 的退出码契约）； - **分批删除**（D-0349）：每批 `BATCH_SIZE`（5000）条、循环到删完，批与批各自提交—— ⛔ 不许一把巨量 DELETE（10 万行的删除 = 长事务 + 长锁）。每一批的 DELETE 都带 **同一把候选 WHERE** 再 `pk__in` 收口 ⇒ SELECT 与 DELETE 之间翻回 `pending` 的行 **不会被这一批误删**（「pending 永不删」的闸打在 DELETE 自己的 WHERE 上）。"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Min
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business import peer_inbound, peer_sync
from apps.business.models import JidanpuSyncEvent, PeerSyncEvent
from apps.platform.models import Tenant

#: `PeerSyncEvent` 的默认保留期（天）。90 天是审计 M-11的口径，原样保留；
#: 要改走 `--days` 或另立决策，⛔ 不改这里。
DEFAULT_RETENTION_DAYS = 90

#: `JidanpuSyncEvent` 的默认保留期（天）。**30 天 = 业务方拍板（D-0349）**；
#: timer 不带 `--days` 跑的就是它。要改保留期先改拍板，⛔ 不改这里。
JIDANPU_DEFAULT_RETENTION_DAYS = 30

#: `--days` 的下限：0 天 = 「终态即删」会把**正在被抢单判据引用**的窗口外行也扫掉，
#: 而且与「保留期」语义矛盾。给个保守地板。
MIN_RETENTION_DAYS = 1

#: **分批删除**的批大小（D-0349）：每批至多这么多条、循环到删完。
#: ⛔ 不是「一把删」的性能调优参数——它承重的是「不长事务 / 不长锁」这条拍板判据
#: （测试 `PruneSyncEventsBatchingTests` 把它调小来钉「循环真的在循环」）。
BATCH_SIZE = 5000


def _fmt_time(value):
    """候选集时间窗的展示值（运维读的；空集 / 空值 → "—"）。"""
    if value is None:
        return "—"
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M")


class Command(BaseCommand):
    help = (
        "清理两张同步台账（PeerSyncEvent / JidanpuSyncEvent）：默认 dry-run 只报数，"
        "--commit 才删；终态且超过保留期（缺省 peer 90 天 / jidanpu 30 天，--days 显式覆盖两表）；"
        "分批删除；证据行（hall.pull / hall.throw done 空原因 / peer.hall_hop_limit / order.direct_assign）与 "
        "pending 行不删"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="真删（⛔ 缺省是 dry-run：只报命中数，一个字节都不删）",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="强制只报数（缺省本就是 dry-run；与 --commit 同给时以 dry-run 为准——零写入优先）",
        )
        parser.add_argument(
            "--days", type=int, default=None,
            help="保留期（天）：显式给出时两张表同用（排障口径）；"
                 f"缺省 peer {DEFAULT_RETENTION_DAYS} 天 / jidanpu {JIDANPU_DEFAULT_RETENTION_DAYS} 天",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days is not None and days < MIN_RETENTION_DAYS:
            raise CommandError(f"--days 至少 {MIN_RETENTION_DAYS}（收到 {days}）")
        schema = (options.get("tenant") or "").strip() or None
        commit = bool(options.get("commit")) and not bool(options.get("dry_run"))
        now = timezone.now()
        peer_days = days if days is not None else DEFAULT_RETENTION_DAYS
        jidanpu_days = days if days is not None else JIDANPU_DEFAULT_RETENTION_DAYS
        peer_cutoff = now - timedelta(days=peer_days)
        jidanpu_cutoff = now - timedelta(days=jidanpu_days)

        unexpected = False
        deleted_peer_total = deleted_jidanpu_total = 0
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    peer_candidates = self._peer_candidates(peer_cutoff)
                    jidanpu_candidates = self._jidanpu_candidates(jidanpu_cutoff)
                    peer_count = peer_candidates.count()
                    jidanpu_count = jidanpu_candidates.count()
                    peer_window = self._oldest_newest(peer_candidates)
                    jidanpu_window = self._oldest_newest(jidanpu_candidates)
                    if commit:
                        # ⚠️ count 与 delete 用**同一个 queryset**（同一把 WHERE），
                        #    ⛔ 不许报数一套、删除另一套（§5-A 破坏性动作纪律）。
                        deleted_peer = self._delete_in_batches(peer_candidates)
                        deleted_jidanpu = self._delete_in_batches(jidanpu_candidates)
                    else:
                        deleted_peer = deleted_jidanpu = 0
                    deleted_peer_total += deleted_peer
                    deleted_jidanpu_total += deleted_jidanpu
                    kept = self._peer_evidence_kept(peer_cutoff)
                self.stdout.write(
                    f"{tenant.schema_name}: peer 台账命中 {peer_count} 条"
                    f"（最老 {_fmt_time(peer_window[0])} · 最新 {_fmt_time(peer_window[1])}）"
                    f" · jidanpu 台账命中 {jidanpu_count} 条"
                    f"（最老 {_fmt_time(jidanpu_window[0])} · 最新 {_fmt_time(jidanpu_window[1])}）"
                    f" · 证据行保留 {kept} 条（不删）"
                    + (
                        f" ⇒ 已删 peer {deleted_peer} / jidanpu {deleted_jidanpu}"
                        if commit else "（dry-run，未删除）"
                    )
                )
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（照 peer_sync）
                unexpected = True
                # ⛔ 只写异常类型名：`str(exc)` 可能带载荷片段（spec §11-S1 同族）。
                self.stderr.write(
                    f"{tenant.schema_name}: 本租户清理失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        mode = "已删除" if commit else "dry-run 未删除"
        if days is not None:
            retention_note = f"保留期 {days} 天（--days 显式指定，两张表同用）"
        else:
            retention_note = (
                f"保留期 peer {DEFAULT_RETENTION_DAYS} 天 / "
                f"jidanpu {JIDANPU_DEFAULT_RETENTION_DAYS} 天（各表默认）"
            )
        self.stdout.write(
            f"合计（{mode}）：peer {deleted_peer_total} 条 · jidanpu "
            f"{deleted_jidanpu_total} 条 · {retention_note} · 分批 {BATCH_SIZE} 条/批"
        )
        if unexpected:
            raise CommandError("有租户的清理出现未预期失败，详见上方", returncode=1)

    # ── 跨 schema 枚举（照 jidanpu_sync / peer_sync 的既有模式）──────────────
    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。 ⚠️ `--tenant` 是**排障闸门**，⛔ 不提供 `--schema` 别名（两个开关会变成
        "一个没有文档的别名"）。timer 每台机跑的就是本机 `Tenant` 表里的租户（与备份「own tenant DB」同一口径：本机的平台面只登记本机服务的租户）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    # ── 候选集（count 与 delete 共用；⛔ 别在调用方再写一份 WHERE）─────────────
    @staticmethod
    def _peer_candidates(cutoff):
        """`PeerSyncEvent` 的**可删**候选：终态 + 超保留期 + **非证据行**。证据行的排除清单与理由见模块 docstring（常量出处：`peer_sync.HALL_PULL_KIND` /
        `peer_sync.HALL_THROW_KIND` / `peer_inbound.HOP_LIMIT_LEDGER_KIND`）。 ⚠️ `hall.throw` 只保留「`done` 且空 `reason`」那部分证据（推送真送达）；
        `done`+非空（take 作废）与 `failed`（没送达）**不是**证据、照删。"""
        return PeerSyncEvent.objects.filter(
            status__in=[PeerSyncEvent.Status.DONE, PeerSyncEvent.Status.FAILED],
            created_at__lt=cutoff,
        ).exclude(
            kind__in=[peer_sync.HALL_PULL_KIND, peer_sync.DIRECT_TAKER_ASSIGN_KIND],
        ).exclude(
            kind=peer_sync.HALL_THROW_KIND,
            status=PeerSyncEvent.Status.DONE,
            reason="",
        ).exclude(
            kind=peer_inbound.HOP_LIMIT_LEDGER_KIND,
        )

    @staticmethod
    def _peer_evidence_kept(cutoff):
        """「本来在保留窗里、但因为它是证据行而不删」的行数（dry-run 报给人看的数）。"""
        return PeerSyncEvent.objects.filter(
            status__in=[PeerSyncEvent.Status.DONE, PeerSyncEvent.Status.FAILED],
            created_at__lt=cutoff,
        ).filter(
            kind__in=[peer_sync.HALL_PULL_KIND, peer_sync.DIRECT_TAKER_ASSIGN_KIND],
        ).count() + PeerSyncEvent.objects.filter(
            status=PeerSyncEvent.Status.DONE, reason="",
            created_at__lt=cutoff,
        ).filter(
            kind=peer_sync.HALL_THROW_KIND,
        ).count() + PeerSyncEvent.objects.filter(
            status__in=[PeerSyncEvent.Status.DONE, PeerSyncEvent.Status.FAILED],
            created_at__lt=cutoff,
        ).filter(
            kind=peer_inbound.HOP_LIMIT_LEDGER_KIND,
        ).count()

    @staticmethod
    def _jidanpu_candidates(cutoff):
        """`JidanpuSyncEvent` 的可删候选：终态 + 超保留期。 ⚠️ 无证据行豁免（实查过：全仓对这张表只有一个「存在性」读点——
        `jidanpu_upstream._note_game_ambiguity` 的**写前去重**，删了旧行最多让同一条
        人话台账重写一次，不是判据）。`pending` 不在候选状态清单里 ⇒ **永不删**。"""
        return JidanpuSyncEvent.objects.filter(
            status__in=[
                JidanpuSyncEvent.Status.DONE,
                JidanpuSyncEvent.Status.FAILED,
                JidanpuSyncEvent.Status.SKIPPED,
            ],
            created_at__lt=cutoff,
        )

    @staticmethod
    def _oldest_newest(queryset):
        """候选集的（最老, 最新）`created_at`（报告用；空集两个都是 None）。"""
        window = queryset.aggregate(oldest=Min("created_at"), newest=Max("created_at"))
        return window["oldest"], window["newest"]

    @staticmethod
    def _delete_in_batches(queryset):
        """**分批**删除候选集（每批 `BATCH_SIZE` 条、循环到删完），返回删除总数。 - ⛔ 不许一把 DELETE：十万行的删除 = 长事务 + 长锁（D-0349 拍板点名）；
        - 每一批 = 先按候选集 WHERE 取一批 pk，再**同一把 WHERE + `pk__in`** 删： SELECT 与 DELETE 之间翻回 `pending` 的行不会被误删
        （「pending 永不删」的闸打在 DELETE 自己的 WHERE 上，不是只在 SELECT 上）； - 某一批删了 0 行（整批都被并发翻走 / 已被别的执行者删掉）⇒ 收口返回，防病态循环。"""
        total = 0
        while True:
            batch_ids = list(
                queryset.order_by("pk").values_list("pk", flat=True)[:BATCH_SIZE]
            )
            if not batch_ids:
                return total
            deleted = queryset.filter(pk__in=batch_ids).delete()[0]
            if deleted == 0:
                return total
            total += deleted
