"""`fetch_order_qr_queue`：马上来查码队列——每分钟定时补拉缺群码的订单（D-0356）。 python manage.py fetch_order_qr_queue                 # 干活（缺省就是干活，本命令不删任何东西） python manage.py fetch_order_qr_queue --dry-run       # 只报各租户候选数，零出站 python manage.py fetch_order_qr_queue --tenant <schema> python manage.py fetch_order_qr_queue --limit 2       # 只消费前 2 张（排障口径；缺省=全部候选） ## 一件事订单缺群二维码时，除「人带动」的两个触发点（打开群码区块 / 接单成功）外，系统**自己** 每分钟启动一轮：把候选**全部**逐个问一遍对端（⭐ 2026-11-01 业务方口径「直接全部扫一次，进入队列一个一个去问，频率在限流以内」），查到落图，没查到下一轮再来。24 小时内录入的单只要对端能出码，群码自动到位。业务方拍板原话：「只要是录入系统的订单就进入队列，队列每隔一段时间按顺序用订单号去访问端点查码，查到了就落入截图，没查到就留到下一次，加一个超时限制，超过 24 小时还没有群码的订单废弃查询」。 ## 「队列」是一条候选查询（⛔ 不做入队/出队/重排的状态维护）未软删 + 最近 24h 录入 + 不在对端 429 退避期 + 没有任何群码截图行，老单优先。 - **退避（仅 429）**：`Order.qr_fetch_suppress_until`（持久列，跨进程可见）——⭐
2026-11-01 业务方拍板：普通失败**不写**退避（每轮全部候选都问到，下一轮自然重试）；唯一例外是对端 429 明确给的「过 N 秒再来」⇒ 写 `max(60s, retry_after)`。入队时铺的进程内 cache 冷却只服务 web 快路径，对每分钟新起的本进程无效（两层并存）。 - **查到即出队**：ok 落 ready 群码行 ⇒ 候选查询的 exclude 不再选中。 - **24h 废弃**：窗口自然滚出、不再查；台账面已简化——⭐ 停写 `qr.fetch_started` / `qr.fetch_gaveup`（D-0361 的两个关键时刻 kind：每分钟全量轮询下它们是纯噪声），查码只在**查到**时留既有成功行、失败只进 journald；排障走本命令的逐轮摘要 + 失败明细行（stdout 必达）与订单时间线的查到行。 - **分层间隔**：候选按**单龄**分档轮询——<1h 每 1 分钟、 1~6h 每 5 分钟、6~24h 每 15 分钟（常量集中在 `POLL_TIER_*` / `POLL_INTERVAL_*`，业务方可调）。判定是**纯函数**（`qr_poll_due`：按 `created_at` 对齐的轮询块，无任何跨进程状态——timer 每分钟新起进程，进程内存的到期表活不过一轮，故不用）；「老单优先」顺序与 24h 窗语义不变。 - **预查折叠（同批）**：候选主查询已批量核过「未软删 + 窗内 + 不在退避期 + 无群码行」、配置闸整租户一次 ⇒ 逐单消费时把已核过的 `(order, config)` 经 `preverified` 传给
`_run_fetch_careful`，折掉每单 4 处复核查询（每问一张的额外库往返趋近于零）。如实登记的等价性边界：本轮开始**之后**的变化本轮看不见——软删由落库签发那跳兜成 skipped、人工传码由落库管道淘汰 + 唯一部分索引兜底（最坏多一次无害覆盖）、配置被关由内存对象判（闩沿等下一轮收口）、其余下一轮候选查询自然出队。 - **存量不收**：窗口只覆盖最近 24h（上线首日会带进「上线前 24h 内录入」的活单，属预期）。 ## 限速（对端预算是生命线，按我方 IP 计） **在役租户机三台**，各扫**本机**租户（租户库物理分离，天然无并发冲突；三台 = 三个 IP，对端限速各自独立计）：候选**全量逐个问**，相邻两单之间垫 `QUERY_INTERVAL_SECONDS`（0.5s） ⇒ 每台 ≤120 查询/min，恰好压着对端 120/min 的限速线（单次请求耗时只会拉长间隔 ⇒ 不会超）。 ⚠️ 对端两侧限速须 ≥120/min（查询与触发建码同调——队列扫的无码单查询会触发对端建码，只抬查询侧的话冷启动吞吐会卡在建码预算上；业务方拍板同步上调）。候选特别多时一轮超过 60s：systemd timer 对在跑中的 oneshot 合并跳过后续触发，下一轮照常（速率不变，只是这轮拖长）；对端口径再变先改 `QUERY_INTERVAL_SECONDS`。（历史口径：批曾为 6/30、对端建码曾为 ≤30/min——D-0356 原始登记，2026-11-01 调整见 STATUS。） ## 运行形态（照 `prune_sync_events` 的既有契约） - **逐租户循环**：`Tenant` 排除 public → 逐个 `tenant_context`（「只拿到本机租户」是部署不变量——本机平台面只登记本机服务的租户）；逐租户独立异常边界，一个租户失败不挡别的； **单笔未预期异常记账跳过、继续本轮**（⭐ 2026-11-01 毒单不再中断整租户）；本轮只要出现过未预期失败 ⇒ 退出码 1（`CommandError`），全绿 0。 - **配置闸一次判整租户**：`OrderQrFetchConfig` 无行 / `enabled=False` / 密钥空 / 基址空 / 401 闩 ⇒ 整租户跳过（零出站、零台账）。 - **timer 通道口径**：逐单调 `order_qr_fetch._run_fetch_careful(..., record_failures=False)` ——失败不进操作日志页（只 logger，journald 可查）；`ok` 与 401 闩沿照记台账。"""
from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business import order_qr_fetch
from apps.platform.models import Tenant

#: 相邻两次查码请求的最小间隔（秒）：0.5s ⇒ 每台 ≤120 查询/min（对端限速按我方 IP 计，三台各是独立 IP、各自限，不叠加）。候选**全量逐个问**（业务方「直接全部扫一次，
#: 进入队列一个一个去问，频率在限流以内」）。单次请求耗时只会拉长实际间隔 ⇒ 速率不会超。 ⚠️ 间隔垫在**租户内**逐单循环里：一机多租户时跨租户边界（A 末单→B 首单）不垫，
#: 机器级速率 = 120/min + 每个租户边界 1 次——当前一机一租户，恰好恒等；多租户同机时重审。
QUERY_INTERVAL_SECONDS = 0.5

#: 候选窗口（小时）：超过 24 小时还没有群码的订单废弃查询（业务方拍板，零仪式滚出）。
QUEUE_WINDOW_HOURS = 24

# ── 分层轮询间隔（常量集中可调，业务方可改数值）────────
#: 单龄分档的边界：<1h 视为「新单」、1~6h 视为「中期」、6~24h 视为「老单」。
POLL_TIER_FRESH_UNTIL = timedelta(hours=1)
POLL_TIER_MID_UNTIL = timedelta(hours=6)
#: 各档的轮询间隔（秒）：新单每 1 分钟（timer 就是每分钟一轮 ⇒ 每轮必问）、
#: 中期每 5 分钟、老单每 15 分钟。
POLL_INTERVAL_FRESH_SECONDS = 60
POLL_INTERVAL_MID_SECONDS = 300
POLL_INTERVAL_OLD_SECONDS = 900
#: timer 触发周期（秒）：分层判定按「对齐 `created_at` 的轮询块」判——到期 = 单龄落进
#: 本档间隔块的**前一个触发周期**里（`age % interval < 轮询周期`）。间隔是周期的整数倍
#: 时等价于「每 interval 轮问一次」；间隔比周期还短 ⇒ 每轮必问。纯函数、零跨进程状态。
POLL_ROUND_SECONDS = 60


def qr_poll_due(order, now) -> bool:
    """本单**这一轮**该不该问（按单龄分层的纯函数判据；⛔ 无跨进程状态）。 ⚠️ 块对齐锚是 `created_at`：同一张单的轮询时刻在不同轮次间是**确定的**
    （单龄 mod 间隔），进程重启 / 补跑都不漂。方向安全：错过一个块顶多等下一个间隔
    （少问一次），⛔ 不会多问。"""
    age = now - order.created_at
    if age < POLL_TIER_FRESH_UNTIL:
        interval = POLL_INTERVAL_FRESH_SECONDS
    elif age < POLL_TIER_MID_UNTIL:
        interval = POLL_INTERVAL_MID_SECONDS
    else:
        interval = POLL_INTERVAL_OLD_SECONDS
    if interval <= POLL_ROUND_SECONDS:
        return True
    return (age % timedelta(seconds=interval)) < timedelta(seconds=POLL_ROUND_SECONDS)


def queue_candidates(now):
    """查码队列的候选集（**未切片**的 queryset；count 与切片都从这里出，⛔ 别在调用方另写 WHERE）。 - 未软删 + 最近 `QUEUE_WINDOW_HOURS` 小时录入（24h 废弃的落法）；
    - 不在对端 429 的持久退避期（⭐：普通失败不写退避——每分钟一轮扫描本身就是节流，下一轮自然重试；`qr_fetch_suppress_until` 仅对端 429 才写）；
    - **没有任何群码截图行**（ready 或 pending 都算有——pending 行说明签发凭证已在途）； - 老单优先（业务方「按顺序」）。"""
    from apps.business.models import Order, OrderScreenshot

    return (
        Order.objects.filter(
            deleted_at__isnull=True,
            created_at__gte=now - timedelta(hours=QUEUE_WINDOW_HOURS),
        )
        .filter(
            Q(qr_fetch_suppress_until__isnull=True)
            | Q(qr_fetch_suppress_until__lte=now),
        )
        .exclude(
            screenshots__img_type=OrderScreenshot.IMG_GROUP_QR,
        )
        .order_by("created_at")
    )


class Command(BaseCommand):
    help = (
        "马上来查码队列：扫最近 24 小时录入、缺群码、不在对端 429 退避期的订单，"
        "老单优先把候选全部逐个问一遍（间隔 0.5s ⇒ 每台 ≤120 次/分钟）；"
        "普通失败与落库失败（land_failed）下一轮自然重试（仅 429 写持久退避）、"
        "timer 通道失败不进操作日志页；单笔未预期异常记账跳过、继续本轮，"
        "本轮结束仍有未预期失败则退出码 1；--dry-run 只报候选数零出站"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="只报各租户候选数（零出站、零台账）",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="本轮每租户最多消费的候选数（缺省=全部候选；排障口径，≥1）",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError(f"--limit 至少 1（收到 {limit}）")
        schema = (options.get("tenant") or "").strip() or None
        dry_run = bool(options.get("dry_run"))
        now = timezone.now()

        unexpected = False
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    stats = self._run_tenant(now, limit=limit, dry_run=dry_run)
                self.stdout.write(
                    f"{tenant.schema_name}: 候选 {stats['candidates']} / "
                    f"消费 {stats['consumed']} / ok {stats['ok']} / "
                    f"跳过 {stats['skipped']} / 分层等 {stats['tier_wait']}"
                    + (
                        "（dry-run，零出站）"
                        if dry_run
                        else (
                            "（配置未启用或闩 ⇒ 整租户零出站）"
                            if not stats["gate"]
                            else ""
                        )
                    )
                )
                # 失败明细逐行跟上（journald 排障用；成功单只在上面的 ok 计数里）。
                # ⚠️ 走 stdout 而不是 logger.info：本项目没配 LOGGING，非 django 命名空间
                #    的 INFO 到不了 journald，stdout 必达。
                for line in stats["failures"]:
                    self.stdout.write(f"  - {line}")
                if stats["unexpected"]:
                    # 单笔未预期失败（已记账跳过、本轮照常走完）：本轮结束退出码 1，
                    # ⛔ 不再中断——毒单没有资格打断整租户的队列（M6）。
                    unexpected = True
                    self.stderr.write(
                        f"{tenant.schema_name}: {stats['unexpected']} 单未预期失败"
                        "（已记账跳过、继续本轮；下一轮自然重试）"
                    )
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界（照 prune_sync_events）
                unexpected = True
                # ⛔ 只写异常类型名：`str(exc)` 可能带载荷片段（spec §11-S1 同族）。
                self.stderr.write(
                    f"{tenant.schema_name}: 本租户查码队列失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        mode = "dry-run" if dry_run else "已执行"
        self.stdout.write(
            f"合计（{mode}）：全量逐个问（间隔 {QUERY_INTERVAL_SECONDS}s）· "
            f"窗口 {QUEUE_WINDOW_HOURS}h"
            + (f" · --limit {limit}" if limit is not None else "")
        )
        if unexpected:
            raise CommandError("有租户的查码队列出现未预期失败，详见上方", returncode=1)

    # ── 跨 schema 枚举（照 prune_sync_events 的既有模式）──────────────────────
    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。 ⚠️ `--tenant` 是**排障闸门**。timer 每台机跑的就是本机 `Tenant` 表里的租户
        （「只拿到本机租户」是部署不变量：本机的平台面只登记本机服务的租户）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    def _run_tenant(self, now, *, limit, dry_run) -> dict:
        """单个租户的一轮：配置闸 → 候选 → 逐单消费。返回摘要计数（给 journald 一行）。"""
        from apps.business.models import OrderQrFetchConfig

        # 配置闸一次判整租户（判据与 worker 复核同源：`_config_gate_passing`）⇒ 判不过零出站。
        config = OrderQrFetchConfig.objects.filter(
            tenant_schema=connection.schema_name
        ).first()
        gate = order_qr_fetch._config_gate_passing(config)

        candidates = list(queue_candidates(now))
        stats = {
            "candidates": len(candidates),
            "consumed": 0,
            "ok": 0,
            "skipped": len(candidates),
            "gate": gate,
            "failures": [],
            "unexpected": 0,
            "tier_wait": 0,
        }
        if dry_run or not gate:
            return stats

        schema = connection.schema_name
        selected = candidates[:limit] if limit is not None else candidates
        served = 0
        for order in selected:
            # ⭐ 分层间隔：单龄不到本档到期块 ⇒ 这一轮不问（候选顺序不动，
            # 「老单优先」与 24h 窗语义原样）。⚠️ 与 `--limit` 叠加的饿档残差（如实
            # 登记）：切片被「层等到期」的单占满时，切片外的更老单要等它们到期才轮到 ——最多饿一个间隔档（15 分钟），下一轮自然轮到、自愈。
            if not qr_poll_due(order, now):
                stats["tier_wait"] += 1
                continue
            if served:
                # 相邻两单之间的最小间隔（首单前不垫）：把查询频率压在对端限速内——
                # 单次请求耗时只会拉长实际间隔 ⇒ 速率不会超（全量逐个问口径）。
                time.sleep(QUERY_INTERVAL_SECONDS)
            served += 1
            outcome = order_qr_fetch._run_fetch_careful(
                schema=schema,
                order_no=order.order_no,
                source_order_no=order.source_order_no or "",
                order_pk=order.pk,
                record_failures=False,
                # ⭐ 预查折叠：候选主查询 + 整租户配置闸已在本轮批量核过
                #   ⇒ 把 `(order, config)` 传下去折掉每单 4 处复核查询。
                preverified=(order, config),
            )
            stats["consumed"] += 1
            stats["skipped"] -= 1
            if outcome == order_qr_fetch.OUTCOME_UNEXPECTED:
                # 未预期异常：worker 已记日志（不写退避，下一轮自然重试）。⭐ M6 ：记账跳过、**继续消费剩余候选**——毒单（候选老单优先，反复异常的现实路径：复核后窗口内单被软删 ⇒ 签发抛 Http404 /
                # PermissionDenied、或同名真人账号冲突 RuntimeError）不再每轮把整租户队列打断；本轮结束后按计数置退出码 1（handle 里）。 ⚠️ 2026-11-03 预查折叠起「单被软删」不再是这一档：签发那跳的
                # Http404 在 worker 快路径里就地折成 skipped（与复核挡下同结局）；剩下的 unexpected = PermissionDenied / 同名真人账号冲突 RuntimeError 等真接线问题。
                stats["failures"].append(f"{order.order_no}: unexpected")
                stats["unexpected"] += 1
                continue
            if outcome == "ok":
                stats["ok"] += 1
            elif outcome == "skipped":
                # 消费前竞态（软删 / 人工传了码 / 配置刚被关）——不是失败，
                # 单列回 skipped 计数，⛔ 不混进失败明细（S1）。
                stats["skipped"] += 1
            else:
                # 其余结局（not_found / generating / peer_error / network /
                # land_failed——对端回了 ok 但落库失败，H1）逐行列明细，不计 ok。
                stats["failures"].append(f"{order.order_no}: {outcome}")
        return stats
