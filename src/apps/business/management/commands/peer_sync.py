"""`peer_sync`：上下游投递的**唯一调度入口**（spec §16.6 第 2 条）。 python manage.py peer_sync --loop             # 常驻轮询（**生产就是这个形态**） python manage.py peer_sync --once             # 跑一轮（排障 / 手工） python manage.py peer_sync --tenant <schema>  # 只跑一个租户（排障） ⚠️ **生产调度由发版自动安装**（改准，决策 `D-0157`）：`--loop` 由 systemd 常驻单元 `deploy/systemd/tuantuantong-peer-sync.service`（与鸡蛋铺线同形）拉起，**随发版安装 / 重启** （`deploy/activate.sh` 与 `deploy/rollback.sh` 的 `SYNC_SERVICES` 清单；按本机 `SYNC_UNIT_ENABLE` 决定哪台跑）。⛔ **不要再手工挂 cron**（与常驻**真并发**）；⛔ **不要再叠第二个调度器**。 `--once` 与 `--loop` 互斥（同时给会响亮报错，不是静默常驻）。
⚠️ **一条常驻进程是「共用的闹钟」，数据是各自的**：遍历租户 → 每个租户在自己的 `tenant_context` 里跑 → **逐租户独立异常边界**（一个租户失败不影响别的租户）。 ⚠️ **多台应用服务器时只在一台上 enable**（单机约束由 `SYNC_UNIT_ENABLE` 显式声明）。 ⚠️ **投递语义是 at-least-once，不是"不会重复"** （改准：原句"真要两台同时跑也不会重复投递"是**假的**）：取活用 `SELECT … FOR UPDATE SKIP LOCKED` + **300 秒租约**（spec §7.7 / §16.6）只能挡住"同一瞬间"的抢活 ——**进程崩 / 投递卡过租约** ⇒ 下一轮会把**同一台账行再投一次**；两台同时跑时同一张单的事件也可能被先后投出、**顺序不由本机决定**。⇒ **事件必须幂等**（接收侧按 `occurred_at` 与 `PeerOrderLink.last_applied_at` 裁决新旧、相等 = 重复，spec §16.3），顺序差异也由同一条规则吸收。 ⚠️ 运维口径仍是**只在一台上跑**（幂等是兜底，⛔ 不是"可以多开一份"的理由）。 ⚠️ **每轮在投递之后还跑一次对账**（P3 Task 8，spec §16.8）：问下家"我推给你的单你应用到哪了"，
把落后的重排成 `pending` 让下一轮补投。⚠️ 对账的失败（对端不可达 / 拒收）是**预期内**的， ⛔ **不设退出码**（只进汇总行的「对账失败 N」）；退出码契约仍只看 `run_round` 的未预期失败。 ⭐ **每轮在投递之前先跑一次纯本地清理**（本线的 P3 增量：「大厅镜像接单卡超时回收」，⭐ D-0335 起 60 秒、原 300 秒）： `peer_hall.reclaim_stuck_claims()` —— `claiming` 超过回收阈值（⭐ D-0335 起 60 秒，原 300）还没等到正式订单的镜像行放回 `listed`。它**零网络、零出站事件**（只改本地行 + 记一条 `direction="in"` 台账），所以挂在 **循环的最前面**：先让卡死的行回到大厅，再投递（下一拍 `hall.throw` / 铺底就能把它挂回去）。 ⚠️ 这一支**不按租户筛**也**不按绑定开关筛**，理由见 `peer_hall.reclaim_stuck_claims` 的 docstring。⚠️ 它失败（库错之类）属于**未预期** ⇒ 按既有逐租户异常边界冒到 `_run_once` 的 `except`、计一个未预期失败，⛔ 不在这里吞掉。 ⚠️ **退出码契约**（照 `jidanpu_sync` 的先例）：`--once` 在**有租户出现未预期失败**时抛 `CommandError(returncode=1)`，否则退出 0 ⇒ **手工跑这一趟**时非零就是"我们这边坏了"； **预期内的投递失败**（连不上 / 对端拒收 / 退避等待）是正常噪音，⛔ 不会让它非零。 ⚠️ `--loop` 崩了由 systemd 拉起（`Restart=always`），⛔ **没有"退出码发信"这回事**—— 常驻形态的告警判据是绑定上的 `last_ok_at`。 ⚠️ 本命令是**管理命令**而非 HTTP 端点：它按命令行给的 `--tenant` 选租户是**运维排障入口**， ⛔ 不接受任何请求来源的租户标识（`AGENTS.md` §4）。"""
import signal
import threading
import time
from contextlib import closing

import psycopg

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business import peer_hall, peer_wakeup, peer_worker
from apps.platform.models import Tenant

#: `--loop` 的睡眠间隔（秒）：与鸡蛋铺线同量级（一轮的节奏由退避表决定，不由它决定）。
LOOP_SLEEP_SECONDS = 10
LISTENER_POLL_SECONDS = 0.25

#: ⭐ M-12（C9）：优雅停机的**停止位**。 `SIGTERM` / `SIGINT` 只把它置位，⛔ 不直接退出：正在投递的那一轮会**跑完当前租户
#: 当前轮**再收口，睡眠从 `time.sleep` 换成 `Event.wait`（信号一到立刻醒来退出，不用陪睡满 10 秒）。systemd 的 `TimeoutStopSec=30` 因此只覆盖「一轮」的时长，
#: 不再出现「发版 SIGKILL 打断在途上游写请求」的常态（事件状态只在成功后落库 ⇒ 被打断 = 「上游已收到、我方没记账」⇒ 重发非幂等写）。
_STOP = threading.Event()


def _request_stop(signum, _frame) -> None:
    """信号处理器：只置位。⛔ 在 handler 里做任何重活（信号上下文不可重入）。"""
    _STOP.set()


class Command(BaseCommand):
    help = "上下游订单事件投递（取活 + 退避 + 投递前复核 + 自动停绑 + 对账补丢单）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true",
            help="跑一轮就退（排障 / 手工；⛔ 不是生产调度——生产是下面的 --loop 常驻单元）",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help="常驻轮询（生产形态：由发版装的 systemd 单元 tuantuantong-peer-sync 跑它；本地也可用）",
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip() or None
        # ⛔ `--once` 与 `--loop` 不能同时给：那会变成「静默常驻」（`--once` 被忽略）——
        # 宁可响亮报错。
        if options.get("once") and options.get("loop"):
            raise CommandError("--once 与 --loop 不能同时给（一个跑一趟、一个常驻）")
        if options.get("loop"):
            # ⭐ M-12：注册 SIGTERM / SIGINT → 停止位（重复注册只是覆盖 handler，幂等）。
            #    ⚠️ `--once` 不注册：跑一趟就退的形态没有"当前轮"可言，保持默认行为。
            signal.signal(signal.SIGTERM, _request_stop)
            signal.signal(signal.SIGINT, _request_stop)
            self._loop(schema)
            return
        if self._run_once(schema):
            # 退出码契约：未预期失败 ⇒ 非零，**手工跑这一趟**的人才会看到（⛔ 不再有 cron 发信——
            # 常驻形态由发版装的 systemd 单元 `Restart=always` 拉起、告警看 `last_ok_at`）；
            # 预期内的投递失败**不**走这里。
            raise CommandError(
                "有租户的本轮出现未预期失败，详见上方汇总",
                returncode=1,
            )

    def _loop(self, schema) -> None:
        """Keep the sole sender responsive while periodic maintenance runs separately."""
        if _STOP.is_set():
            return
        try:
            if schema is not None:
                self._iter_tenants(schema)
            listener = self._open_delivery_listener()
        except BaseException:
            connection.close()
            raise
        maintenance = threading.Thread(
            target=self._maintenance_loop, args=(schema,), name="peer-maintenance",
        )
        try:
            maintenance.start()
        except BaseException:
            if listener is not None:
                listener.close()
            connection.close()
            raise
        try:
            self._delivery_loop(schema, listener=listener)
        finally:
            _STOP.set()
            maintenance.join()
            connection.close()

    def _open_delivery_listener(self):
        try:
            return peer_wakeup.open_listener()
        except psycopg.Error as exc:
            self.stderr.write(f"大厅摘牌监听连接失败，定时投递继续（{type(exc).__name__}）")
            return None

    def _delivery_loop(self, schema, *, listener):
        next_scan = time.monotonic()
        reconnect_at = next_scan + 1
        reconnect_delay = 1
        windows = {}
        window_since = timezone.now()
        should_scan = True
        try:
            while not _STOP.is_set():
                tick = time.monotonic()
                if tick >= next_scan:
                    self._close_delivery_windows(windows, since=window_since)
                    windows.clear()
                    window_since = timezone.now()
                    next_scan = tick + LOOP_SLEEP_SECONDS
                    should_scan = True
                if should_scan:
                    try:
                        should_scan = self._scan_delivery(schema, windows)
                    except Exception as exc:  # noqa: BLE001
                        self.stderr.write(f"上下游投递轮失败（{type(exc).__name__}）")
                        connection.close()
                        should_scan = False
                    if _STOP.is_set() or should_scan or time.monotonic() >= next_scan:
                        continue
                if listener is None and time.monotonic() >= reconnect_at:
                    listener = self._open_delivery_listener()
                    if listener is not None:
                        should_scan = True
                        continue
                    reconnect_at = time.monotonic() + reconnect_delay
                    reconnect_delay = min(LOOP_SLEEP_SECONDS, reconnect_delay * 2)
                timeout = max(0, min(LISTENER_POLL_SECONDS, next_scan - time.monotonic()))
                if listener is None:
                    _STOP.wait(timeout)
                    continue
                try:
                    with closing(listener.notifies(timeout=timeout, stop_after=1)) as notifications:
                        should_scan = next(notifications, None) is not None
                    reconnect_delay = 1
                except psycopg.Error as exc:
                    self.stderr.write(f"大厅摘牌监听断开，定时投递继续（{type(exc).__name__}）")
                    listener.close()
                    listener = None
                    reconnect_at = time.monotonic() + reconnect_delay
                    reconnect_delay = min(LOOP_SLEEP_SECONDS, reconnect_delay * 2)
        finally:
            if listener is not None:
                listener.close()
            self._close_delivery_windows(windows, since=window_since)

    def _scan_delivery(self, schema, windows):
        close_old_connections()
        claimed = False
        for tenant in self._iter_tenants(schema):
            if _STOP.is_set():
                break
            try:
                with tenant_context(tenant):
                    if tenant.schema_name not in windows:
                        windows[tenant.schema_name] = (tenant, peer_worker.DeliveryWindow())
                    _tenant, window = windows[tenant.schema_name]
                    summary = peer_worker.run_delivery_batch(
                        now=timezone.now(), window=window, should_stop=_STOP.is_set,
                    )
                    claimed |= bool(summary["claimed"])
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"{tenant.schema_name}: 投递轮失败（{type(exc).__name__}）")
                continue
            if summary["claimed"]:
                self.stdout.write(
                    f"{tenant.schema_name}: 投递 {summary['delivered']}"
                    f" · 重试 {summary['retried']} · 终态失败 {summary['failed']}"
                    f" · 撤单作废 {summary['voided']} · 丢弃 {summary['dropped']}"
                )
            self._write_delivery_errors(tenant, summary)
        return claimed

    def _close_delivery_windows(self, windows, *, since):
        for tenant, window in windows.values():
            try:
                with tenant_context(tenant):
                    revoked = window.close(now=timezone.now(), since=since)
                    if revoked:
                        self.stdout.write(f"{tenant.schema_name}: 自动停绑 {revoked}")
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"{tenant.schema_name}: 投递计数失败（{type(exc).__name__}）")

    def _maintenance_loop(self, schema):
        try:
            while not _STOP.is_set():
                close_old_connections()
                try:
                    self._run_maintenance(schema)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"上下游维护轮失败（{type(exc).__name__}）")
                _STOP.wait(LOOP_SLEEP_SECONDS)
        finally:
            connection.close()

    def _run_maintenance(self, schema):
        for tenant in self._iter_tenants(schema):
            try:
                with tenant_context(tenant):
                    if _STOP.is_set():
                        return
                    reclaimed = peer_hall.reclaim_stuck_claims()
                    if _STOP.is_set():
                        return
                    reconcile = peer_worker.run_reconcile(should_stop=_STOP.is_set)
                    if _STOP.is_set():
                        return
                    pull = peer_worker.run_hall_pull()
                    self.stdout.write(
                        f"{tenant.schema_name}: 对账补 {reconcile['requeued']}"
                        f" · 对账失败 {reconcile['errors']} · 回收卡死 {reclaimed}"
                        f" · 大厅拉取 {pull['pages']} 页 / {pull['rows']} 卡"
                        f"（{'完整' if pull['complete'] else '未完成'} · 判消失 {pull['gone']}）"
                    )
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"{tenant.schema_name}: 维护轮失败（{type(exc).__name__}）")

    def _iter_tenants(self, schema):
        """本命令这一趟要跑的租户（只读平台面的 `Tenant`）。
        ⚠️ `--tenant` 是**排障闸门**，⛔ 不提供 `--schema` 别名（两个开关会变成
        "一个没有文档的别名"）。"""
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    def _run_once(self, schema) -> bool:
        """跑一趟所有（或指定的）租户，返回「是否出现过未预期失败」（退出码契约用）。"""
        unexpected = False
        for tenant in self._iter_tenants(schema):
            # ⚠️ 逐租户独立异常边界：一个租户失败**不影响**别的租户。
            # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段（spec §11-S1）。
            try:
                with tenant_context(tenant):
                    if self._run_tenant(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界
                # 能逃到这一层的异常**按定义**是未预期的（`run_round` 已接住逐事件的异常）。
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 本轮失败（{type(exc).__name__}），"
                    "下一个租户继续"
                )
        return unexpected

    def _run_tenant(self, tenant) -> bool:
        """在一个租户里跑一轮（投递内核在 `apps/business/peer_worker.py`）。顺序：① 纯本地清理（大厅镜像接单卡 60 秒回收，零网络）→ ② `run_round()` 正常投递 → ③ `run_reconcile()` 对账补丢单（P3 Task 8）→ ④ `run_hall_pull()` 大厅**慢路拉取**
        （计划 `2026-10-25-peer-hall-pull` §5：拉取排在投递之后，⛔ 不抢投递的预算）。 ⚠️ **① 挂在循环最前面**：回收让卡死的行先回到大厅，本拍 / 下一拍的出站投递才能把它重新挂回对端（见模块 docstring）。
        ⚠️ **对账 / 拉取的 `errors` 是预期内噪音**（对端不可达 / 拒收 / 老对端无端点）， ⛔ **不设退出码**——退出码契约只看 `run_round` 的未预期失败（见 `handle`）。返回这一轮**是否出现未预期失败**（退出码契约用）。"""
        reclaimed = peer_hall.reclaim_stuck_claims()
        summary = peer_worker.run_round()
        reconcile = peer_worker.run_reconcile()
        pull = peer_worker.run_hall_pull()
        self.stdout.write(
            f"{tenant.schema_name}: 投递 {summary['delivered']}"
            f" · 重试 {summary['retried']} · 终态失败 {summary['failed']}"
            f" · 撤单作废 {summary['voided']} · 自动停绑 {summary['revoked']}"
            f" · 对账补 {reconcile['requeued']} · 对账失败 {reconcile['errors']}"
            f" · 回收卡死 {reclaimed}"
            f" · 大厅拉取 {pull['pages']} 页 / {pull['rows']} 卡"
            f"（{'完整' if pull['complete'] else '未完成'} · 判消失 {pull['gone']}）"
        )
        return self._write_delivery_errors(tenant, summary)

    def _write_delivery_errors(self, tenant, summary) -> bool:
        errors = summary.get("errors") or []
        if errors:
            # ⛔ 不静默：未预期失败必须让运维在终端上也看得见（台账 / 操作日志另有留痕）。
            # ⚠️ 这里转抄的是 `run_round` 汇总里的**异常类型名**；**完整栈**在 worker 的
            # `logger.exception` 里（排障需要）——两条通道分工不同，⛔ 不是同一句话的两种说法。
            self.stderr.write(
                f"{tenant.schema_name}: 本轮有 {len(errors)} 条事件未预期失败："
                + "；".join(errors)
            )
            return True
        return False
