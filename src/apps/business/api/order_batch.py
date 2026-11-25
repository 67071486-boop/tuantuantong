"""批量端点的**脚手架**（解析行 → 数据权限 → 入参闸 → 两段式 → 统一信封）。设计：`docs/specs/2026-09-26-order-batch-operations-design.md` §D2/D4/D8/D9。
⛔ **判据不在这里**——每个动作「这一笔能不能做」的答案一律来自 `apps.business.order_writes` 的核心函数（`per_order` 就是它）；本模块只管「批量这件事本身」：解析、权限、上限、事务与回滚。
"""
from collections import defaultdict

from django.db import transaction
from django.db.models import Q

from apps.business import split_group
from apps.business.api.serializers import allowed_transitions_for
from apps.business.game_scope import game_scope
from apps.business.models import Order, OrderStatus
from apps.business.order_flags import scope_by_creator
from apps.business.validation import ValidationProblem
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node_now

#: 单批上限（spec §D8 的**纯防呆**值）：页面每页最多 100 笔，正常撞不到。
BATCH_ORDER_LIMIT = 500


def parse_order_nos(raw):
    """把请求里的 `order_nos` 收敛成 `(list[str] | None, 错误文案)`。⚠️ **四种拒绝的顺序固定：空 → 格式不正确 → 重复 → 超限**，且顺序本身有判别力：「先去重还是先判重」这类改法会让某一条用例失去判别力（见 `tests/test_order_batch_actions.py` 的三条入参用例）。
    """
    if not isinstance(raw, list) or not raw:
        return None, "请至少选择一笔订单"
    order_nos = []
    for item in raw:
        if not isinstance(item, str) or not item:
            return None, "订单号格式不正确"
        order_nos.append(item)
    if len(set(order_nos)) != len(order_nos):
        return None, "同一笔订单只能出现一次"
    if len(order_nos) > BATCH_ORDER_LIMIT:
        return None, f"一次最多处理 {BATCH_ORDER_LIMIT} 笔，本次 {len(order_nos)} 笔"
    return order_nos, ""


def resolve_orders(request, order_nos, *, include_deleted=False, list_visibility=False):
    """按请求顺序解析这批单；取不到的进 issue 列表（**不**把整批判成 1000，spec §D9）。"""
    qs = (Order.all_objects if include_deleted else Order.objects).filter(
        order_no__in=order_nos
    )
    # ⭐ 评审阻断 3：隐藏单**只放行 retrieve** ⇒ 批量（改状态 / 指派 / 改价 / 删除 / 退款 /
    #    重算）一律按「取不到这一行」处理（1002 + `记录不存在或已被删除`），
    #    ⛔ 不因为它是 `WAITING_GRAB` 就放行。与单笔 `get_object()` 的失败出口同形。
    qs = qs.exclude(hall_card__isnull=False).exclude(
        upstream_hall_card__isnull=False
    )
    qs = scope_queryset(request.user, qs, scope_filter=game_scope("game_id"))
    if list_visibility:
        qs = scope_by_creator(request.user, qs)
    found = {row.order_no: row for row in qs}
    ordered, issues = [], []
    for order_no in order_nos:
        row = found.get(order_no)
        if row is None:
            issues.append(
                {"order_no": order_no, "source_order_no": "",
                 "code": int(ErrorCode.BUSINESS),
                 "msg": "记录不存在或已被删除"}
            )
        else:
            ordered.append(row)
    return ordered, issues


def _issue(order, outcome):
    """一行不合格回显（⭐ D-0301，2026-10-24：补 `source_order_no` 供结果弹窗做展示回退）。

    ⚠️ `order` 是 `Order` 行（调用点都拿着整行）；取不到行的单号走 `resolve_orders`
    自己拼的那条（那里没有行可读，`source_order_no` 给空串占位——前端回退到 `order_no`）。
    """
    return {
        "order_no": order.order_no,
        "source_order_no": order.source_order_no,
        "code": outcome.code,
        "msg": outcome.msg,
    }


def transition_targets(request, order_nos):
    """`batch/transition/` 的**读取形态**（spec §D10）：这批单可选目标的**并集** + 每笔自己的可选集。⛔ **纯只读**：不开事务、不写任何东西（不写业务列、不写留痕、不建行）——它替换的是「勾 N 笔就发 N 次详情请求」（每页最多 100 笔）。
    ⚠️ 每笔的 `allowed` 与订单详情**同源**：都调 `serializers.allowed_transitions_for`（唯一判据出处，spec §D5）——⛔ 不在这里镜像 `order_status.TRANSITIONS`、也不复制一份节点判据。`targets` 按 `OrderStatus.choices` 的**声明顺序**排序（并集是集合，不排序下拉顺序会随哈希种子漂移）；取不到行的单号进 `ineligible`（复用 `resolve_orders` 的 issue，spec §D9），⛔ 不把整批判成 1000；`eligible` / `applied` 有意恒空（这条请求没提交任何目标）。
    """
    ordered, issues = resolve_orders(request, order_nos)
    per_order, union = [], set()
    for order in ordered:
        allowed = list(allowed_transitions_for(order, request))   # 与订单详情**同源**
        # ⭐ D-0301：回显行补 `source_order_no`（结果弹窗展示回退的取数前提）。
        per_order.append({"order_no": order.order_no,
                          "source_order_no": order.source_order_no,
                          "allowed": allowed})
        union.update(allowed)
    targets = [value for value, _ in OrderStatus.choices if value in union]
    return api_response(data={"dry_run": True, "targets": targets, "per_order": per_order,
                              "eligible": [], "ineligible": issues, "applied": []})


def split_batch_members(members):
    groups = defaultdict(list)
    for member in members:
        if member.split_id:
            groups[member.split_id].append(member)
    return groups


def load_split_batch_members(split_ids):
    return Order.all_objects.filter(split_id__in=split_ids).order_by("id")


def lock_split_batch_scope(ordered):
    split_ids = {row.split_id for row in ordered if row.split_id}
    selected = {row.order_no: row for row in ordered}
    locked = list(
        Order.all_objects.select_for_update()
        .filter(Q(split_id__in=split_ids) | Q(order_no__in=selected))
        .order_by("id")
    )
    direct = {row.order_no: row for row in locked if row.order_no in selected}
    groups = split_batch_members(locked)
    if len(direct) != len(selected) or any(
        current.split_id != selected[no].split_id for no, current in direct.items()
    ):
        return None
    # A transaction that held a group lock may have inserted a row while this read waited.
    current_ids = list(load_split_batch_members(split_ids).values_list("pk", flat=True))
    locked_ids = [row.pk for row in locked if row.split_id]
    if current_ids != locked_ids:
        return None
    return direct, groups


def split_batch_effects(request, rows, groups, *, deleting):
    """按提交顺序预演出组和级联，供展示和写入前核对。"""
    effects = {}
    active_groups = {
        split_id: [member for member in members if member.deleted_at is None]
        for split_id, members in groups.items()
    }

    def add(member, operation):
        entry = effects.setdefault(member.order_no, (member, []))
        if operation not in entry[1]:
            entry[1].append(operation)

    for row in rows:
        active = active_groups.get(row.split_id, [])
        if row.deleted_at is not None or not active:
            continue
        if not deleting:
            anchor = next(
                (member for member in active if member.split_kind in ("", split_group.LEGACY, split_group.PEER)),
                None,
            )
            if anchor is not None and anchor.pk != row.pk:
                add(anchor, "自动调平接单价")
            continue
        if row.pk not in {member.pk for member in active}:
            continue
        anchor, *children = active
        if split_group.cascades_on_delete(row, anchor) and children:
            for child in children:
                add(child, "随拆单主单删除")
            active_pks = {member.pk for member in active}
            for member in groups[row.split_id]:
                if member.pk not in active_pks and member.deleted_at is not None:
                    add(member, "清除已删订单的拆单归组")
            active.clear()
            continue
        if anchor.pk != row.pk and row.total_price != 0:
            add(anchor, "返还被删子单的接单价")
        active[:] = [member for member in active if member.pk != row.pk]
        if len(active) == 1:
            add(active.pop(), "解除拆单归组")

    if not effects:
        return []
    visible, _ = resolve_orders(
        request, list(effects), include_deleted=deleting, list_visibility=True
    )
    required_nodes = {
        "order.recycle" if member.deleted_at is not None else "order.list"
        for member in visible
    }
    if len(visible) != len(effects) or any(
        not has_node_now(request.user, node) for node in required_nodes
    ):
        raise ValidationProblem("拆单联动涉及无权查看的订单，无法确认本批操作范围")
    return [
        {"order_no": member.order_no, "source_order_no": member.source_order_no,
         "operation": "、".join(operations)}
        for member, operations in effects.values()
    ]


def run_batch(request, *, order_nos, dry_run, per_order, ctx, include_deleted=False,
              after_apply=None, before_lock=None, confirmed_effects=None):
    """两段式的公共实现（spec §D2/D4）。⚠️ `dry_run=True` 分支**不开事务**（只读校验，没有要保护的行锁）；`dry_run=False` 分支的 `select_for_update()` **必须在** `with transaction.atomic():` 里面——autocommit 下加锁求值会抛 `TransactionManagementError`（E-0019 ⇒ HTML 500）。
    ⚠️ `apply` 时若 `resolve_orders` 报出 issue（客户端送来了**上次校验就说不合格**的号）⇒ **整批拒**（`set_rollback(True)` + 1002 + `data.conflict`），⛔ 不静默跳过：§D3 的契约是「`apply` 的 `order_nos` = 上一次 `dry_run` 的 `eligible`」，送别的号是客户端违约，猜它想干什么比拒掉更贵。
    ⭐ `after_apply` 可选回调：**整批真实提交成功之后**（事务已退出）才调用，入参是这一批被处理的行（锁内取回、已被 `per_order` 写过），返回值（字符串）作为成功响应的 `msg`；用途 = 批量指派之后逐单撤代练通订单——真实上游写请求**不能进事务**（与单笔 `/assign/`「事务提交后再撤」同一条纪律），失败要能随响应回给客服。`dry_run=True` ⇒ 由**调用方**传 `None`（预检一行没写，不该动平台单）；失败回滚 / 中途被拒 ⇒ 回调**不会被调用**（没有「本地没改成、平台单却撤了」）。
    """
    ordered, issues = resolve_orders(
        request, order_nos, include_deleted=include_deleted,
        list_visibility=confirmed_effects is not None,
    )

    if dry_run:
        # ⚠️ **按请求顺序一次成型**（spec §D2/§D9）：先建两张表（取不到行的 issue、逐笔判据的 outcome），再照 `order_nos` 的顺序走一遍同时生成 `eligible` 与 `ineligible`；⛔ 不要写成「先铺 `list(issues)`、再追加逐笔失败」——解析期 issue 与逐笔不合格同时存在时，`ineligible` 的顺序会偏离请求里的 `order_nos` 顺序。
        issues_by_no = {issue["order_no"]: issue for issue in issues}
        rows_by_no = {order.order_no: order for order in ordered}
        eligible, ineligible = [], []
        for order_no in order_nos:
            issue = issues_by_no.get(order_no)
            if issue is not None:
                ineligible.append(issue)
                continue
            order = rows_by_no[order_no]
            outcome = per_order(order, ctx, True)
            if outcome.ok:
                eligible.append(order.order_no)
            else:
                ineligible.append(_issue(order, outcome))
        data = {"dry_run": True, "eligible": eligible, "ineligible": ineligible, "applied": []}
        if confirmed_effects is not None:
            rows = [rows_by_no[no] for no in eligible]
            split_ids = {row.split_id for row in rows if row.split_id}
            try:
                data["effects"] = split_batch_effects(
                    request, rows, split_batch_members(load_split_batch_members(split_ids)),
                    deleting=include_deleted,
                )
            except ValidationProblem as problem:
                return api_response(code=problem.code, msg=str(problem), data=None)
        return api_response(data=data)

    # ── apply：一个事务；逐笔重校验；任一不合格整批回滚（E-0022）──
    eligible = [order.order_no for order in ordered]
    processed = []
    with transaction.atomic():
        if issues:
            # 客户端送来了**上次校验就说不合格**的单号 ⇒ 不猜、不静默跳过，整批拒。
            first = issues[0]
            transaction.set_rollback(True)
            return api_response(
                code=ErrorCode.BUSINESS,
                msg=f"单号 {first['order_no']} 在提交时已不可处理：{first['msg']}；本批未做任何修改",
                data={"conflict": first, "applied": []},
            )
        if confirmed_effects is not None:
            scope = lock_split_batch_scope(ordered)
            if scope is not None:
                locked, groups = scope
                rows = [locked[no] for no in eligible]
                try:
                    effects = split_batch_effects(request, rows, groups, deleting=include_deleted)
                except ValidationProblem as problem:
                    return api_response(code=problem.code, msg=str(problem), data=None)
            if scope is None or effects != confirmed_effects:
                return api_response(
                    code=ErrorCode.BUSINESS,
                    msg="拆单联动范围已变化，请重新确认。本批未做任何修改",
                    data={"applied": []},
                )
            if not include_deleted:
                locked = {no: row for no, row in locked.items() if row.deleted_at is None}
        else:
            if before_lock is not None:
                before_lock(ordered)
            locked = {
                row.order_no: row
                for row in (
                    (Order.all_objects if include_deleted else Order.objects)
                    .select_for_update()
                    .filter(order_no__in=eligible)
                    .order_by("id")
                )
            }
        for index, order in enumerate(ordered, start=1):
            row = locked.get(order.order_no)
            if row is None:
                transaction.set_rollback(True)
                return api_response(
                    code=ErrorCode.BUSINESS,
                    msg=f"第 {index} 笔（单号 {order.order_no}）在提交时已不存在；本批未做任何修改",
                    data={"conflict": {"order_no": order.order_no,
                                       "source_order_no": order.source_order_no,
                                       "code": int(ErrorCode.BUSINESS),
                                       "msg": "记录不存在或已被删除"}, "applied": []},
                )
            outcome = per_order(row, ctx, False)
            if not outcome.ok:
                transaction.set_rollback(True)
                return api_response(
                    code=ErrorCode.BUSINESS,
                    msg=(f"第 {index} 笔（单号 {order.order_no}）在提交时已不可处理："
                         f"{outcome.msg}；本批未做任何修改"),
                    data={"conflict": _issue(order, outcome), "applied": []},
                )
            processed.append(row)
    # ⚠️ 事务**已提交**才走这里（见 docstring 的 `after_apply`）。
    msg = after_apply(processed) if after_apply is not None else ""
    return api_response(data={"dry_run": False, "eligible": eligible,
                              "ineligible": issues, "applied": eligible},
                        msg=msg or "")
