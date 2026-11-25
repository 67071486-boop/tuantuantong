"""数据修复：给「已完成、但打手绩效没有接手人」的订单补上接手人，并重走「待验收 → 已完成」。 ## 为什么需要它（生产实测的形态）鸡蛋铺下家线（`JidanpuMemberOrderLink`）上有一批订单已经是 `COMPLETED`，但它的 `Performance`（`emp_type=MAIN`）那一行**存在而 `user` 为空**、`temp_amount = 0.00`、 `settle_status = 未结账`、`review_status = NULL`。这种行不会有任何人拿到打手绩效：绩效的钱按「归属人 × 提成」算，没有归属人 ⇒ 算不出、也发不出。业务方规则（2026-10-27 口径；2026-10-29 真值治理改准：源码不再内置任何租户的账号名）： - 走**鸡蛋铺线**的单 ⇒ 接手人挂「鸡蛋铺系统」账号（名字从 `--jidanpu-user` 或环境变量 `FIX_MISSING_TAKER_JIDANPU_USER` 读）； - 走**团团通 peer 线**（`PeerOrderLink`）的单 ⇒ 接手人挂「团团通系统」账号（`--peer-user` / `FIX_MISSING_TAKER_PEER_USER`）； - **两条链路都没有** ⇒ **跳过并打印**（⛔ 不猜接手人）； - 目标账号按**工号 / 名字**查（精确匹配）。⚠️ 该线**有候选**而 CLI 参数与环境变量**都没给名字** ⇒ `CommandError` 响亮报错（不打任何兜底名字）；给了但**查不到**同样响亮失败（`CommandError`），⛔ 不静默跳过。修法**不是**直接改 `Performance.user` / `temp_amount`（那是绕过领域判据），而是照既有领域入口走一遍： 1. `order_writes.assign_order`：指派接手人（它内部把订单迁到「代练中」、写 `user`、
按这个人的提成重算金额）。它自带实名 / 游戏范围 / 销售归属 / 已结账 / 状态机五道闸； 2. 状态机唯一写入口 `order_status.apply_transition`：接着重走「代练中 → 待验收 → 已完成」。最后一步会触发完成钩子（`settlement_hooks.on_order_completed` → `_activate_taker_performance`），把 `bonus_amount` 按 `temp_amount` 生效（「完成即生效」）——这就是「金额不再为 0」的来源，⛔ 不在本命令里手写一遍。 ## 默认 dry-run（标准档：动钱） `--commit` 才写库。默认只逐单打印：订单号 / 来源线 / 目标接手人 / 当前 MAIN 行（user、预计绩效、结账状态）/ 预计动作。dry-run 的预检也走**同一批领域判据** （`assign_order(dry_run=True)` + 两段状态边的 `transition_denial`），因此「预检说能修、真写却失败」的窗口只剩「完成钩子算客服提成时配置不对」这一档（每次 `apply_transition` 都会重新判）。 ## 幂等候选集合 = 「`status=COMPLETED` 且 MAIN 行 `user IS NULL`」。修好之后 `user` 非空 ⇒ 第二次执行**命中 0 条、零写入**（`--limit` 支持小批试跑）。 ## 台账每张成功修复的单落**一条**操作日志：复用 `assign_order` 内部既有的 `performance.assign`（动作码既有，⛔ 不新造）。两次状态迁移走 `apply_transition` （机器路径，不写 `order.transition` 行），因此**一张单恰好一行**台账。 ## 为什么必须保住原完单时间（`completed_at`）
重走状态会**离开「已完成」再回来**：`assign_order` 先把它迁到「代练中」，而 `apply_transition` 里「离开已完成 ⇒ `order.completed_at = None`」那一支正好命中；重走最后一步「待验收 → 已完成」又把它写成 `timezone.now()`。两步合起来 = **完单时间被改成修复时刻**。生产实测：46 张已完成单的完单时间就这样被改成了修复当天，原本是 10-11～10-25（完单时间是客服对账 / 结算口径的时间轴，丢了它就等于改了历史）。 ⇒ `_repair` 在动任何东西**之前**记下 `original_completed_at`，整单流程走完后： - 原来**有值** ⇒ `Order.objects.filter(pk=…).update(completed_at=原值)` 按**单列**写回（⛔ 不 `order.save()`：那是整行覆盖，会冲掉这一轮别的列）； - 原来**为空** ⇒ 保持现状（最后一步写成修复时刻），⛔ 不凭空造一个时间。每单的输出行同时打印「原完单时间=… ⇒ 保持原值 / 写为修复时刻」，便于以后核对。 ## 它**不**做什么 - ⛔ 不碰已结账的 MAIN 行（`Performance.is_settled` 三字段并集）⇒ 打印「跳过」且一行不改； - ⛔ 不处理「MAIN 行整个不存在」的单（那是另一类历史数据，本命令不补行）； - ⛔ 不猜接手人：链路判定只有 `_line_of_order()` 这一处； - ⛔ 不绕过领域闸：目标账号没有游戏权限 / 未实名 / 是销售归属人 / 提成配置不对时，命令按「失败」逐单打印真实原因（先在 dry-run 里看清，再决定是否手工补权限）。 ## 生产建议（先 dry-run，再小批） # 接手人名字给一次（环境变量；或每条命令都带 --jidanpu-user / --peer-user）： export FIX_MISSING_TAKER_JIDANPU_USER=<鸡蛋铺系统账号的工号 / 名字> export FIX_MISSING_TAKER_PEER_USER=<团团通系统账号的工号 / 名字> manage.py fix_missing_taker_performance --schema=<租户> --limit 3          # 先看 3 张 manage.py fix_missing_taker_performance --schema=<租户> --limit 3 --commit # 只写这 3 张 manage.py fix_missing_taker_performance --schema=<租户>                    # 全量预览 manage.py fix_missing_taker_performance --schema=<租户> --commit           # 全量写 ⚠️ 重走状态会给**已有链路**排下行 / 上游同步事件（`apply_transition` 的既有副作用），这是「真的把这张单重新走了一遍」的正常代价；`--limit` 小批试跑可先观察一轮。"""
from __future__ import annotations

import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.accounts.display import user_display_name
from apps.business.commission import CommissionError
from apps.business.models import (
    EmpType,
    JidanpuMemberOrderLink,
    Order,
    OrderStatus,
    PeerOrderLink,
    Performance,
    SettleStatus,
)
from apps.business.order_status import (
    TRANSITIONS,
    IllegalTransition,
    apply_transition,
    transition_denial,
)
from apps.business.order_writes import WriteContext, assign_order
from apps.business.performance_amount import AmountError
from apps.platform.models import Tenant

#: 调用本命令的机器身份（`record()` 需要它；`log_actor_id=None` ⇒ 台账落**系统行**）。
ACTOR = "fix_missing_taker_performance"

#: 目标账号默认名的**环境变量**（真值治理：源码不再内置任何租户的生产账号名—— 改前写死过某租户的名字，且它在生产被人改过（实测被去掉「账号」二字），
#: 每跟一次改名都要动源码）。形态与兄弟命令（`dailiantong_sync` 的 `os.environ.get(ENV_…)`）一致。
#: ⚠️ 不写死主键：每个租户的 `auth_user` 各自一份。 ⛔ 仍按**名字精确匹配**（不写死主键）；名字对不上时由下面的「候选清单」提示接手。
ENV_JIDANPU_USER = "FIX_MISSING_TAKER_JIDANPU_USER"
ENV_PEER_USER = "FIX_MISSING_TAKER_PEER_USER"


def _env_default(name: str) -> str:
    """环境变量里的默认接手人（空白视为未配置——`.env` 里留空键不算给了名字）。"""
    return (os.environ.get(name) or "").strip()

#: 命令输出里的「这次做了什么」说明。
#: ⚠️ `assign_order` 的 `record()` 今天**没有** `reason` 形参 ⇒ 本常量只用于命令输出，
#: ⛔ 不要以为它落进了日志行。
REPAIR_NOTE = "数据修复：补齐缺失接手人并重走待验收→已完成"

#: 来源线（**唯二的取值** + `None` = 没有链路）。判定只在 `_line_of_order()` 一处。
LINE_JIDANPU = "jidanpu"
LINE_PEER = "peer"
LINE_LABELS = {LINE_JIDANPU: "鸡蛋铺", LINE_PEER: "团团通"}

#: 两段重走的顺序（最后一步必须是「待验收 → 已完成」，否则完成钩子不跑）。
REWALK = (
    (OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW),
    (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED),
)


def _edge_nodes(from_status, target) -> frozenset:
    """这一段迁移「持有的节点集」= **这条边自己的节点集**（机器路径的既有口径）。先例与逐字理由见 `xgj_push._nodes_for` / `jidanpu_converge._nodes_for`：
    数据修复命令没有请求身份，取边表里这条边的节点集，语义是「**这条边存在**就允许系统去走它」（⛔ 不是把某个角色的节点搬到后台）。"""
    return frozenset(TRANSITIONS.get((str(from_status), str(target)), frozenset()))


def _ctx_for(from_status, target) -> WriteContext:
    """`assign_order` 的机器上下文：归属人取 `ACTOR`、台账落系统行、节点 = 这条边自己。"""
    return WriteContext(
        actor=ACTOR,
        log_actor=ACTOR,
        log_actor_id=None,
        nodes=_edge_nodes(from_status, target),
    )


def _line_of_order(order_ids) -> dict:
    """**来源线判定的唯一出处**：鸡蛋铺链路优先，其次 peer 链路，都没有 ⇒ `None`。判据就是两张链路表的**存在性**（业务方口径），⛔ 不按绑定 / 状态 / 时间猜：
    - 该单有 `JidanpuMemberOrderLink` ⇒ 鸡蛋铺线； - 否则有 `PeerOrderLink`（上游 / 下游任一侧）⇒ 团团通 peer 线；
    - 都没有 ⇒ `None`（调用方跳过并打印，⛔ 不猜）。一次把整批订单的链路取回来（两条 SQL），避免逐单 N+1。"""
    order_ids = list(order_ids)
    if not order_ids:
        return {}
    jidanpu_ids = set(
        JidanpuMemberOrderLink.objects.filter(order_id__in=order_ids).values_list(
            "order_id", flat=True
        )
    )
    peer_ids = set(
        PeerOrderLink.objects.filter(order_id__in=order_ids).values_list(
            "order_id", flat=True
        )
    )
    result = {}
    for order_id in order_ids:
        if order_id in jidanpu_ids:
            result[order_id] = LINE_JIDANPU
        elif order_id in peer_ids:
            result[order_id] = LINE_PEER
        else:
            result[order_id] = None
    return result


def _status_label(status) -> str:
    """状态码 → 中文标签（唯一出处 `OrderStatus.choices`）。"""
    return dict(OrderStatus.choices).get(str(status), str(status))


def _completed_at_note(original_completed_at, *, committed) -> str:
    """「原完单时间 → 本次怎么处置」的一行输出（核对用；判据与 `_repair` 同一个来源）。
    `committed=False`（dry-run）一律说「将…」——⛔ 别把**计划**打印成**已发生**。"""
    if original_completed_at is None:
        return "原完单时间=空 ⇒ " + (
            "写为修复时刻" if committed else "将写为修复时刻"
        )
    # ⚠️ 库里读出的 aware 值是 UTC，裸 `str()` 会差 8 小时 ⇒ 统一经 `localtime`
    #    （与 `rescue_gone_mirrors._local_text` 同口径）。
    moment = timezone.localtime(original_completed_at).strftime("%Y-%m-%d %H:%M:%S")
    return f"原完单时间={moment} ⇒ " + ("保持原值" if committed else "将保持原值")


class Command(BaseCommand):
    help = (
        "给「已完成但打手绩效没有接手人」的订单补接手人并重走「待验收→已完成」"
        "（默认 dry-run；--commit 才写；已结账行跳过；无链路跳过不猜）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema", default=None,
            help="只处理这一个租户 schema（默认：全部非 public 租户，逐个进 tenant_context）",
        )
        parser.add_argument(
            "--commit", action="store_true",
            help="真的写库（默认 dry-run：只打印逐单计划，不写任何东西）",
        )
        parser.add_argument(
            "--limit", type=int, default=None,
            help="每个租户本轮最多处理多少张（默认不限；小批试跑用）",
        )
        parser.add_argument(
            "--jidanpu-user", default=None,
            help=(
                "鸡蛋铺线的接手人：登录账号或名字，精确匹配"
                f"（缺省时读环境变量 {ENV_JIDANPU_USER}；两处都没给而该线有候选 ⇒ 响亮报错）"
            ),
        )
        parser.add_argument(
            "--peer-user", default=None,
            help=(
                "peer 线的接手人：登录账号或名字，精确匹配"
                f"（缺省时读环境变量 {ENV_PEER_USER}；两处都没给而该线有候选 ⇒ 响亮报错）"
            ),
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit is not None and limit < 1:
            # ⛔ 0 / 负数会让「一轮什么都没干」看起来像「修完了」。
            raise CommandError("--limit 必须 ≥ 1")
        commit = options["commit"]
        # CLI 参数优先，环境变量兜底（都不给而该线有候选 ⇒ `_resolve_account` 响亮报错）。
        jidanpu_spec = options["jidanpu_user"] or _env_default(ENV_JIDANPU_USER)
        peer_spec = options["peer_user"] or _env_default(ENV_PEER_USER)

        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        only = options["schema"]
        if only:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")

        self.stdout.write(
            "写库模式（--commit）" if commit
            else "dry-run（未写库；--commit 才写）"
        )
        totals = {"applied": 0, "skipped": 0, "failed": 0}
        for tenant in tenants.order_by("schema_name"):
            # ⚠️ `apps.business` 是 TENANT_APP，这些表只在各自 schema 里 ⇒ 必须逐个进
            #    `tenant_context`（同 `audit_order_group_qr` / `purge_pending_screenshots`）。
            with tenant_context(tenant):
                counts = self._run_for_tenant(
                    tenant.schema_name,
                    commit=commit,
                    limit=limit,
                    jidanpu_spec=jidanpu_spec,
                    peer_spec=peer_spec,
                )
            for key in totals:
                totals[key] += counts[key]

        if commit:
            self.stdout.write(
                f"汇总（--commit）：成功={totals['applied']} "
                f"跳过={totals['skipped']} 失败={totals['failed']}"
            )
        else:
            # dry-run 的 `applied` 是「预检通过、--commit 后会修」的张数。
            self.stdout.write(
                f"汇总（dry-run，未写库）：成功=0 将修复={totals['applied']} "
                f"跳过={totals['skipped']} 失败={totals['failed']}"
            )
        return None

    # ── 一个租户 ─────────────────────────────────────────────────────────
    def _run_for_tenant(self, schema_name, *, commit, limit, jidanpu_spec, peer_spec):
        perfs = list(
            Performance.objects.filter(
                order__status=OrderStatus.COMPLETED,
                order__deleted_at__isnull=True,
                emp_type=EmpType.MAIN,
                # 候选判据就是「打手绩效没有归属人」——这是这份脏数据的形态。
                user__isnull=True,
                # 已软删的绩效行是**有人删过**的，不在这条修复线里（⛔ 不顺手复活）。
                deleted_at__isnull=True,
            )
            .select_related("order")
            .order_by("order_id")
        )
        if limit is not None:
            perfs = perfs[:limit]

        lines = _line_of_order(perf.order_id for perf in perfs)
        # 先解析「本批真的会用到」的账号：缺号 ⇒ **写任何东西之前**响亮失败。
        users = {}
        needed = {line for line in lines.values() if line is not None}
        if LINE_JIDANPU in needed:
            users[LINE_JIDANPU] = self._resolve_account(
                jidanpu_spec, label="鸡蛋铺系统账号", option="--jidanpu-user",
                env_var=ENV_JIDANPU_USER,
            )
        if LINE_PEER in needed:
            users[LINE_PEER] = self._resolve_account(
                peer_spec, label="团团通系统账号", option="--peer-user",
                env_var=ENV_PEER_USER,
            )

        counts = {"applied": 0, "skipped": 0, "failed": 0}
        self.stdout.write(f"[{schema_name}] 候选 {len(perfs)} 张" + ("" if commit else "（dry-run）"))
        for perf in perfs:
            order = perf.order
            line = lines[order.pk]
            head = (
                f"{order.order_no} 来源="
                f"{LINE_LABELS.get(line, '无链路')}"
            )
            current = (
                f"当前MAIN：user=空 temp_amount={perf.temp_amount} "
                f"bonus_amount={perf.bonus_amount} 结账={perf.settle_status}"
            )
            # ⚠️ **在 `_repair` 动任何东西之前**取副本：修单过程中这一列会被清成 NULL、
            #    再写成修复时刻（`_repair` 末尾才回填原值）⇒ 输出要的是**原来**那一刻。
            completed_at_note = _completed_at_note(
                order.completed_at, committed=commit
            )
            if line is None:
                counts["skipped"] += 1
                self.stdout.write(
                    f"  {head} {current} {completed_at_note} → 跳过："
                    "既没有鸡蛋铺链路也没有团团通 peer 链路（⛔ 不猜接手人）"
                )
                continue
            if perf.is_settled:
                counts["skipped"] += 1
                self.stdout.write(
                    f"  {head} {current} {completed_at_note} → 跳过："
                    "该行已结账（⛔ 不许改）"
                )
                continue

            taker = users[line]
            target = f"目标={user_display_name(taker)}(pk={taker.pk})"
            if commit:
                result, message = self._repair(order, taker)
            else:
                result, message = self._preview(order, taker)
            if result != "applied":
                counts["failed"] += 1
                self.stdout.write(
                    f"  {head} {target} {current} {completed_at_note} → 失败：{message}"
                )
                continue

            counts["applied"] += 1
            if commit:
                perf.refresh_from_db()
                self.stdout.write(
                    f"  {head} {target} {current} {completed_at_note} → 已修复："
                    f"user={user_display_name(perf.user)}(pk={perf.user_id}) "
                    f"temp_amount={perf.temp_amount} bonus_amount={perf.bonus_amount} "
                    f"结账={perf.settle_status}（{REPAIR_NOTE}）"
                )
            else:
                self.stdout.write(
                    f"  {head} {target} {current} {completed_at_note} → 将：指派并重走"
                    f"「{_status_label(OrderStatus.PENDING_REVIEW)} → "
                    f"{_status_label(OrderStatus.COMPLETED)}」"
                )

        if commit:
            self.stdout.write(
                f"[{schema_name}] 成功={counts['applied']} "
                f"跳过={counts['skipped']} 失败={counts['failed']}"
            )
        else:
            self.stdout.write(
                f"[{schema_name}] 将修复={counts['applied']} "
                f"跳过={counts['skipped']} 失败={counts['failed']}"
            )
        return counts

    # ── 两个写路径（预检 / 真写）────────────────────────────────────────
    def _preview(self, order, taker):
        """dry-run：跑**全部逐单判据**、但一行不写。 - 第一段（指派）走 `assign_order(dry_run=True)`：实名 / 游戏范围 / 销售归属 /
        已结账 / 状态机边五道闸都在它里面； - 后两段走纯内存 `transition_denial`（与 `apply_transition` 的第一道闸同源）。
        ⚠️ 这里**不**调 `apply_transition`：它会 `select_for_update` + 写状态。完成钩子里的「客服提成」是否算得出来，只有真写时才由 `apply_transition` 判（预检不预演钩子，⛔ 不假装知道）。"""
        outcome = assign_order(
            order,
            taker=taker,
            ctx=_ctx_for(order.status, OrderStatus.IN_PROGRESS),
            dry_run=True,
        )
        if not outcome.ok:
            return "failed", f"指派预检被拒：{outcome.msg}"
        for from_status, target in REWALK:
            denied = transition_denial(from_status, target, _edge_nodes(from_status, target))
            if denied:
                return "failed", f"状态预检被拒：{denied}"
        return "applied", ""

    def _repair(self, order, taker):
        """真写：`assign_order` + 唯一写入口 `apply_transition` 重走两段。 ⚠️ **整张单一个事务**：任一段失败 ⇒ `transaction.set_rollback(True)` 把前面
        已经写下的（指派 / 金额 / 台账）一起回滚，⛔ 不留「人指派了、状态没走完」的半笔（E-0022：`atomic()` 里不要用裸 `return` 退出补偿路径）。
        ⭐ **保住原完单时间**（见模块 docstring 那一节）：重走状态会把 `completed_at` 清空、最后一步再写成 `now()` ⇒ 进事务**之前**记下原值，整单走完后按**单列** 写回（原来为空就保持现状）。"""
        # ⚠️ 必须在 `assign_order` 之前取值：它内部就会把这张单迁到「代练中」，
        #    那一刻 `apply_transition` 已按「离开已完成」把 `completed_at` 清成 NULL。
        original_completed_at = order.completed_at
        try:
            with transaction.atomic():
                outcome = assign_order(
                    order,
                    taker=taker,
                    ctx=_ctx_for(order.status, OrderStatus.IN_PROGRESS),
                    dry_run=False,
                )
                if not outcome.ok:
                    transaction.set_rollback(True)
                    return "failed", f"指派被拒：{outcome.msg}"
                for target in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED):
                    from_status = order.status
                    try:
                        # ⚠️ 节点集 = 这条边自己的（机器路径口径，见 `_edge_nodes`）。
                        apply_transition(
                            order, target, nodes=_edge_nodes(from_status, target),
                            actor=ACTOR,
                        )
                    except IllegalTransition as exc:
                        # 状态机在写任何东西之前就抛它 ⇒ 但仍要回滚前面那次指派。
                        transaction.set_rollback(True)
                        return (
                            "failed",
                            f"改状态到「{_status_label(target)}」被拒：{exc}",
                        )
                    except (CommissionError, AmountError) as exc:
                        # 完成钩子的**预期**失败（提成配置不对）：整笔回滚 + 原文报出。
                        transaction.set_rollback(True)
                        return (
                            "failed",
                            f"改状态到「{_status_label(target)}」算钱失败：{exc.message}",
                        )
                # ⭐ 整单流程走完（状态 / 金额 / 台账都已落）⇒ 把原完单时间写回。 ⛔ **不用 `order.save()`**：那是整行覆盖，会把这一轮写下的别的列冲掉；
                # `QuerySet.update()` 只写这一列，且按 Django 语义**不碰** `updated_at` （`auto_now` 只在 `save()` 时自动写）⇒ 时间轴上的其他事实不变。
                # 原来为空 ⇒ 保持现状（最后一步的 `now()`，⛔ 不凭空造时间）。
                if original_completed_at is not None:
                    Order.objects.filter(pk=order.pk).update(
                        completed_at=original_completed_at
                    )
                    # 内存对象跟着回正：调用方之后若读 `order.completed_at` 不该看到 `now()`。
                    order.completed_at = original_completed_at
            return "applied", ""
        except Exception as exc:  # noqa: BLE001 —— 逐单失败不拖垮整批
            return "failed", f"{type(exc).__name__}: {exc}"

    # ── 目标账号 ─────────────────────────────────────────────────────────
    def _resolve_account(self, spec, *, label, option, env_var):
        """按**登录账号或名字**精确匹配一个账号；没给 / 0 / 多个 ⇒ `CommandError`（响亮失败）。 ⚠️ 不写死主键（每个租户的 `auth_user` 各自一份）；多个命中时**拒绝猜**
        （用户要的是「查不到就响亮失败」，歧义同样不该静默挑一个）。 ⭐ 2026-10-29 真值治理：`spec` 为空（CLI 参数与环境变量都没给）也在这里响亮失败——
        源码不再内置任何租户的账号名，⛔ 不打合成兜底名（那只会让每次运行都栽在「找不到账号」上，报错信息反而更难懂）。"""
        if not spec:
            raise CommandError(
                f"没有指定{label}：请用 {option} 给出工号 / 名字，"
                f"或设环境变量 {env_var}（源码不内置任何租户的账号名）"
            )
        matches = list(
            User.objects.filter(Q(username=spec) | Q(first_name=spec))
            .order_by("pk")[:5]
        )
        if not matches:
            # ⭐：名字被改过时（生产真踩过），只说「找不到」等于让人瞎猜 ⇒
            #    把**像它的账号**列出来（含 pk / 登录账号 / 名字），运维照着一填就行。
            like = list(
                User.objects.filter(
                    Q(first_name__icontains=spec[:2]) | Q(username__icontains=spec[:4])
                ).order_by("pk")[:8]
            )
            hint = (
                "候选（pk｜登录账号｜名字）："
                + "；".join(
                    f"{user.pk}｜{user.username}｜{user.first_name}" for user in like
                )
                if like
                else "（没有相近账号，请确认租户或账号名）"
            )
            raise CommandError(
                f"找不到{label}：{spec!r}（按登录账号或名字精确匹配）。{hint}；"
                f"请用 {option} 指定正确的工号 / 名字"
            )
        if len(matches) > 1:
            pks = "、".join(str(user.pk) for user in matches)
            raise CommandError(
                f"{label} {spec!r} 命中了多个账号（pk={pks}）⇒ 请用 {option} "
                "指定唯一的登录账号"
            )
        return matches[0]
