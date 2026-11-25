"""订单状态迁移白名单。授权轴是 RBAC 节点 code、不是本模块自己的角色词；规则是 (当前状态, 目标状态) -> 允许的节点 code 集合。
唯一写入口 apply_transition 的 nodes 必填；can_transition(nodes=None) 只回答这条边在不在、不算授权；自定义职位与内置职位走同一套 nodes_of。
每条边的节点集 = 能合法到达该目标的各端点节点门的并集、避免端点放行状态机再拒一次。"""
import logging

from django.utils import timezone

from apps.business import jidanpu_assignment_lock
from apps.business.peer_sync import PEER_DIRECT_PENDING_MESSAGE
from apps.business.models import (
    EmpType,
    Order,
    OrderStatus,
    PeerOrderLink,
    Performance,
    SettleStatus,
)
from apps.business.order_status_persistence import persist_order_status

logger = logging.getLogger(__name__)

ISSUE_REASON_REQUIRED_MSG = "请输入问题原因"

#: ⭐ A8 / L-6：上家迟到事件会覆盖、本地用户也会编辑的那几列。盖「我方最后一次本地动作时刻」的写点清单以它为准（穷举网：tests/test_local_action_stamp_net.py）。
#: status 不在列——它由 apply_transition 这条状态机入口盖（既有口径）；game 不在列——本地换游戏是配置对齐、上家事件的 game 覆盖不构成「本地编辑被冲掉」那一档（登记残差 ⛔ 不在这里顺手扩）。
CONFLICT_PRONE_FIELDS: frozenset[str] = frozenset({
    "hall_price", "customer_phone", "customer_qq", "project_text",
    "remark", "content",
})


def stamp_local_action(order) -> None:
    """盖「我方最后一次本地动作时刻」（PeerOrderLink.local_action_at = now）。唯一实现（⭐ A8 / L-6——原来是 apply_transition 尾部的一段内联写法；状态之外还有五个「上家事件会覆盖」的列（备注/项目/电话/QQ/发单价）、它们的本地写点原来不盖这一列 ⇒ 本地改完备注、一条更旧的上家事件照样把它冲掉（L-6 现场）；收口成函数后非状态的本地写点调同一个入口 ⛔ 不各抄一份）。
    ⚠️ 只盖挂着「启用且未软删」上游绑定的链路（既有判据逐字保留）：这张单根本不同步时写这一列没有消费方。
    ⚠️ 只有本地用户动作经本函数：两个同步落库支（peer_inbound.apply_events 与 apply_upstream_events）与鸡蛋铺回拉线（jidanpu_upstream._pull_content）都不调——它们是「对端事实到达」、盖了会把时间线搅浑（apply_transition 尾部的既有注释逐字）；⚠️ 用 timezone.now() ⛔ 不用 order.updated_at：那一列的语义是我方动作的时刻（与状态的既有口径同源）；⚠️ .update() 不经 save() ⇒ 不碰 updated_at、也不触发任何信号。"""
    PeerOrderLink.objects.filter(
        order=order, upstream_binding__deleted_at__isnull=True,
        upstream_binding__enabled=True,
    ).update(local_action_at=timezone.now())


class IllegalTransition(ValueError):
    """非法状态迁移。继承 ValueError 以保持既有调用方兼容。
    ⚠️ API 层必须 except IllegalTransition 而不是 except ValueError：后者会把写库路径冒出的 ValueError 也翻译成业务码、把未预期的错误伪装成业务结果。
    它也表示「调用方手上的是陈旧状态」：apply_transition 写之前会复读该行、库里的状态与内存不一致时抛本异常；对调用方来说语义仍是「这条迁移现在不合法」（只是原因从「边不存在/节点不够」多了一种「这单已经被别人改过」）、所以复用它、不新造类、不新增错误码。"""


# ── 边判据用的节点集合（spec §4.3：每条边的节点集 = 它所有可写端点的节点门之并）──
#: 通用 `/transition/` 端点对**非已完成**目标的节点门（对标 16）。
_CHANGE_STATUS = frozenset({"order.change_status"})
#: 通用 `/transition/` 端点对**已完成**目标的节点门（对标 17）。
_COMPLETE = frozenset({"order.change_status_completed"})
_THROW = frozenset({"order.send_to_grab_hall"})
#: `/clear-taker/` 的类级门（对标 19）。
_CLEAR_TAKER = frozenset({"order.clear_taker"})
#: `/withdraw/` 的类级门（对标 117）。
_WITHDRAW = frozenset({"order.withdraw_from_grab_hall"})
#: `/assign/` 的类级门（对标 18 / 1162）。
_ASSIGN = frozenset({"order.assign_taker", "order.submit_payout_price"})
#: `/submit/` 的类级门（对标 105，code 固定 `order.submit_work`，改 code 会让存量授权丢）。
_SUBMIT = frozenset({"order.submit_work"})
#: `/claim/` 的类级门（对标 50）。
_TAKE_ORDER = frozenset({"grab_hall.take_order"})

# ── 边表：除自环外、每一对 (src, target) 都有边、节点集按 target 定 ──已完成/已退单也能被持有相应节点的人改走、完成钩子因此可能被重复触发；下面两处是刻意收紧、不在「整体放开」里。
# 进入大厅统一使用大厅发单权限，录单入口在建单前另行检查。
# IN_PROGRESS ← 既有三条入边的并集（UNASSIGNED→ 带 _ASSIGN、WAITING_GRAB→ 带 _TAKE_ORDER、PENDING_REVIEW→ 带 _COMPLETE）；PENDING_REVIEW ← 既有 IN_PROGRESS→；COMPLETED ← 只剩 PENDING_REVIEW → 这一条入边（D-0071 的「六个来源态都能到」已被该裁定收紧；见 _COMPLETED_SOURCE）；ISSUE ←（业务方裁定）= 通用 /transition/ 的 16 并 /report-issue/ 的 _SUBMIT（理由与「为什么这不构成提权」见本表下方那段 ISSUE 为什么要有 _SUBMIT 注释）；PAUSED/REFUNDED ← 既有循环自动补的异常态边。
_TARGET_NODES = {
    OrderStatus.UNASSIGNED: _CHANGE_STATUS | _CLEAR_TAKER | _WITHDRAW,
    OrderStatus.WAITING_GRAB: _THROW,
    OrderStatus.IN_PROGRESS: _CHANGE_STATUS | _ASSIGN | _COMPLETE,
    OrderStatus.PENDING_REVIEW: _CHANGE_STATUS | _SUBMIT,
    OrderStatus.COMPLETED: _COMPLETE,
    # ⭐ 业务方裁定：ISSUE 这一档加 _SUBMIT、让「代练中 → 问题单」这条新端点（/report-issue/、节点门 = order.submit_work）在状态机上放行；⛔ 不加的话端点层放行、状态机再拒一次（双轴、正是本模块要消灭的形状）。
    # ⚠️ 本表按 target 铺边 ⇒ 这一改对所有来源态 → ISSUE 的边都加了 _SUBMIT、不只 IN_PROGRESS →；这不是提权、代价与边界见本表下方那段注释。
    OrderStatus.ISSUE: _CHANGE_STATUS | _SUBMIT,
    OrderStatus.PAUSED: _CHANGE_STATUS,
    OrderStatus.REFUNDED: _CHANGE_STATUS,
}


def machine_target_nodes(target) -> frozenset[str]:
    """⭐ 机器路径（外部平台/同步驱动）写某个目标态时用的节点集。取值 = _TARGET_NODES[target]（该目标态在状态机上的合法可达节点并集）。
    为什么要它：外部平台（代练通）把状态改了、本地必须如实跟写——那条写入不是任何一个人类账号的操作、它手里没有「某个角色碰巧持有的节点」；不显式给出节点集就只能传空集 ⇒ fail-closed 拒绝 ⇒「远端已完成、本地还停在待验收」（正是 Rev.9 §2 要消灭的不一致）。
    ⛔ 这是声明「系统代外部事实执行」、不是给人类端点开的旁路：人类端点仍用 nodes_of(user)；今天只允许代练通同步线（dailiantong_push）与它驱动的那几处调用；目标态认不出（不是 OrderStatus 成员）⇒ 空集（fail-closed）；⚠️ 授权判据本体仍在 transition_denial：本函数只回答「这条边需要哪些节点」⛔ 不要在别处再拼一份节点表。"""
    return frozenset(_TARGET_NODES.get(str(target), frozenset()))

# 提交完单和问题单各自使用固定目标态；通用状态修改不使用这个权限。


#: 入 COMPLETED 的边只留这一个来源态（业务方裁定，spec §2.3 / F3）：「任何订单不能直接改已完成，必须从『待验收』改已完成」；整体放开之后刻意收紧的三处：只有待验收能到已完成；已结账不许经回池改钱；已结账拒绝一切状态变更——别把其中一处读成「只有这一处」。
#: ⛔ 读侧不跟着收——OrderSerializer.get_allowed_transitions 仍把「已完成」列出来当会被拒的提示项（业务方明确要求「下拉里已完成也要有」）、那是有意的读/写不对称、判据见 tests/test_complete_requires_review.py。
_COMPLETED_SOURCE = OrderStatus.PENDING_REVIEW

#: ⚠️ 两条必须保持的边界（spec §4.4；漏了就是提权）：
#: 1. _TAKE_ORDER（grab_hall.take_order、代练员自助接单 /claim/）只出现在 (WAITING_GRAB, IN_PROGRESS)——上表按 target 铺边、所以这里补一条 src == WAITING_GRAB 的例外：其它来源态 → IN_PROGRESS 的节点集不含它、否则持该节点的账号能把任意单（含已完成/已退单）拉进代练中；
#: 2. /claim/ 自己的来源态判据（claim.py 条件更新带的 status=WAITING_GRAB）不动——它不读本表、是那条路径自己的闸门。
TRANSITIONS = {
    (src, target): (
        nodes | _TAKE_ORDER
        if target == OrderStatus.IN_PROGRESS and src == OrderStatus.WAITING_GRAB
        else nodes
    )
    for src in OrderStatus
    for target, nodes in _TARGET_NODES.items()
    if src != target
    # ⭐（F3，spec §2.3）：入 COMPLETED 只剩 PENDING_REVIEW → COMPLETED 一条；⛔ 不要给这条规则留「来源态白名单」之类的口子——被删的六条入边
    #（UNASSIGNED / WAITING_GRAB / IN_PROGRESS / ISSUE / PAUSED / REFUNDED）各自有端点级判据钉住（tests/test_complete_requires_review.py）。
    and (target != OrderStatus.COMPLETED or src == _COMPLETED_SOURCE)
}


#: 通用 /transition/ 端点按目标态挑的节点（与 order_views.OrderViewSet 的 ORDER_PERMISSIONS["transition"] + TRANSITION_TARGET_PERMISSIONS 同一口径）。放在这里而不是视图里、是因为读侧（OrderSerializer.get_allowed_transitions）也要用它：两处各写一份必然漂移（E-0021 的形状）。
#: ⚠️ 视图那边的字面量保持不变（结构兜底 B2 走 AST 取类体里的字符串常量、换成引用本映射会让那一条判据失去命中）——两处的一致性由 test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint 拿端点当 oracle 钉住；键取 str(OrderStatus.COMPLETED)：can_transition 也是按 str(...) 查表的、两处口径一致（OrderStatus 是 Django 的 TextChoices、str(成员) 即其值）。
#: ⭐（计划④）WAITING_GRAB 一格：通用 /transition/ 的这个目标已改判 36（见 _TARGET_NODES 与视图的 TRANSITION_TARGET_PERMISSIONS）⇒ 读侧若仍按默认的 16 判「类级门过不过」、会出现只持 36 的账号在端点上能抛、在 allowed_transitions 里却看不到「待抢单」的读/写分叉（本字段的契约是「端点上真会接受的目标集合」）；⚠️ 只加这一格不会把 16 放回来：can_transition 那一半用 TRANSITIONS（本目标已不含 16）先拦住了。
GENERIC_TRANSITION_NODES = {
    str(OrderStatus.COMPLETED): _COMPLETE,
    str(OrderStatus.WAITING_GRAB): _THROW,
}
#: 未登记的目标态一律落回这个（= E1 的类级门 16）。
GENERIC_TRANSITION_DEFAULT_NODES = _CHANGE_STATUS

#: 「不能直接改已完成」的**逐字**文案（业务方给的对标报错原文，spec §2.3）。
#: ⚠️ 只有「目标 == COMPLETED 且来源 != 待验收」这一支用它（见 `transition_denial`）。
COMPLETE_REQUIRES_REVIEW_MESSAGE = "必须先把订单状态改成待验收才能改成已完成"

#: ⭐ T4：「目标状态 == 当前状态」这一档（自环、例如已经「已完成」又点了「已完成」）的原因句——业务方点名换成一句人话；形状：含「已经是」＋具体状态中文名＋「无需修改」。
#: ⚠️ 中文名取自既有唯一出处 OrderStatus.choices（与 order_writes._status_text / jidanpu_converge._status_label 同源）——⛔ 不新抄一张中文表。
SELF_TRANSITION_MESSAGE = "该订单已经是「{status_label}」，无需修改"


def self_transition_message(status) -> str:
    """自环（目标 == 当前）时给用户看的人话理由。
    ⚠️ 中文名走 OrderStatus.choices；认不出的码原样保留（与 order_writes._status_text 的运行时口径一致）；不过自环这一支只在两端都是 OrderStatus 成员时才进（见 transition_denial）⇒ 正常路径上不会印出码本身。"""
    label = dict(OrderStatus.choices).get(str(status), str(status))
    return SELF_TRANSITION_MESSAGE.format(status_label=label)


def can_transition(current, target, nodes=None):
    """能否从 current 迁到 target。
    nodes=None 只回答「这条边是否存在」——本函数不构成授权判断、授权判定请传 nodes（nodes=frozenset() 天然 fail-closed：空集一律拒绝）。
    ⚠️ 为什么不把 nodes=None 收紧成「拒绝」：P2 的 test_order_status.py 里多处正是用它回答「这条边在不在」（纯状态机图查询）、收紧它们会把那些用例变成「测持有全部节点下的边」——等于把授权维度灌进查询维度、状态机图从此不可测；📌 刻意不写具体数字：写进注释的计数会随重构漂移（E-0011）——能被一个反例证伪的绝对句比不写更糟；真正的 fail-open 在唯一写入口 apply_transition：它的 nodes 是必填 keyword-only、漏传直接 TypeError、不会静默放行。"""
    allowed = TRANSITIONS.get((str(current), str(target)))
    if not allowed:
        return False
    if nodes is None:
        return True
    return bool(allowed & {str(n) for n in nodes})


def transition_denial(current, target, nodes):
    """None = 放行；否则返回一句能指认是哪扇门拒绝的理由。
    ⚠️ 两种拒绝必须可分辨（spec §2 A10 / DoD「拒绝侧能指认是哪扇门」）：边不存在（含「显式边被循环覆盖」这类表错）⇒ 只报状态对；边存在但节点不够 ⇒ 追加「（缺少所需权限节点）」；混成一句会让排障的人分不清「业务上不让这么走」与「这个人没这个权限」。
    ⭐（F3，spec §2.3）：「改已完成」那一支回业务方给的逐字文案——目标 == COMPLETED 且来源态不是「待验收」时返回 COMPLETE_REQUIRES_REVIEW_MESSAGE（必须先把订单状态改成待验收才能改成已完成、业务方给的对标报错原文、逐字照抄）；⛔ 别的非法迁移的文案一个字都不改（含自环：它不是「改成已完成」这个动作、仍走通用那句、从而保住「两种拒绝可分辨」这条判据）。⛔ 自环这一档是刻意的偏离（T4）：批量验收勾了几十笔重复点「确定」时、第二发的预检会把这批单报成一串「非法状态迁移：COMPLETED → COMPLETED」、界面毫无反馈 ⇒ 业务方点名要一句人话 ⇒ 自环走 SELF_TRANSITION_MESSAGE（该订单已经是「已完成」，无需修改）、不走上一段的通用拒绝句；其余三档一字未改（①通用「边不存在」②带「（缺少所需权限节点）」③COMPLETE_REQUIRES_REVIEW_MESSAGE）；「两种拒绝可分辨」这条判据继续由「未知/存量状态码」那一档保住（上一段说的「边不存在」今天仍有可达样本：(不是 OrderStatus 的码, 合法目标)）。⚠️ 限定语（别让后来者读错）：Order.status 只是 choices（库层没有 CHECK）⇒ 这一档函数层可达（直接调 transition_denial / apply_transition 造得出来）、但生产路径上今天不可达——所有写状态的落点都对成员集合设了闸：peer_inbound.py 里那道未知状态硬闸（raw_status not in OrderStatus.values）＋两处 peer 接受集闸：apply_events 的 DOWNSTREAM_ACCEPTED_STATUSES、apply_upstream_events 的 _upstream_accepted()（= UPSTREAM_STATUS_MAP 值域）——外加唯一写入口 apply_transition 自己 ⇒ ⛔ 别把这一档读成「用户真的会看到通用那句」（用户看到的是上面三档 + 自环人话）；自环从此是单独一档、不再算进它。"""
    if (
        str(target) == str(OrderStatus.COMPLETED)
        and str(current) not in (str(_COMPLETED_SOURCE), str(OrderStatus.COMPLETED))
    ):
        return COMPLETE_REQUIRES_REVIEW_MESSAGE
    # ⭐ T4：自环（目标 == 当前）给人话理由。**只在两端都是 `OrderStatus` 成员时**
    #    才进这一支 ⇒ 存量 / 未知码仍回下面那句通用文案（「边不存在」那一档因此仍有可达样本，
    #    `test_order_status_axis` 的两种拒绝可分辨判据不失去靶子）。
    if str(current) == str(target) and str(current) in OrderStatus.values:
        return self_transition_message(current)
    edge = TRANSITIONS.get((str(current), str(target)))
    if not edge:
        return f"非法状态迁移：{current} → {target}"
    if nodes is None:
        return None
    if not (edge & {str(n) for n in nodes}):
        return f"非法状态迁移：{current} → {target}（缺少所需权限节点）"
    return None


# ── 迁移副作用：**唯一**的出口（spec §4.1.2；P4 也复用它）────────────────
# 为什么要有这一层：副作用写在视图里，就会有多个调用点，将来新增路径必漏一个——
# P2 的通用 /transition/ 当初就是这样变成后门的（spec §646）。
_TARGET_HOOKS: dict[str, list] = {}

#: 「钱钩子」的函数属性标记名：register_target_hook(..., recomputes_amount=True) 打上它、_run_target_hooks(skip_amount_recalc=True) 据此只跳过带标记的那些钩子。
#: ⚠️ 标记打在函数属性上、注册表 _TARGET_HOOKS 的值形状不变（仍是 dict[str, list[func]]）——既有用例直接拿 [func, …] / len(...) 比 ⛔ 不要把值改成 (flag, func) 元组。
_AMOUNT_HOOK_MARKER = "recomputes_amount"

#: ⭐ 审计日志线：「要操作人主键」的钩子标记名。register_target_hook(..., wants_actor_id=True) 打上它、_run_target_hooks 据此只给这些钩子多传一个 actor_id= 关键字。
#: ⛔ 为什么是「按标记逐个传」而不是「所有钩子都加一个形参」：钩子契约 func(order, from_status) 被 9 个生产钩子 + 若干用例钩子（含测试里现注册的）实现、整体改签名会把「一处留痕」的改动扩散成全仓钩子协议重构（§5-A：不扩大范围）；与 _AMOUNT_HOOK_MARKER 同款：标记打在函数属性上、注册表的值形状不变。
_ACTOR_ID_HOOK_MARKER = "wants_actor_id"
_FINANCE_HOOK_MARKER = "wants_finance_policy"


def register_target_hook(target, func, *, recomputes_amount=False, wants_actor_id=False, wants_finance_policy=False):
    """注册「迁到 target 时」要跑的副作用。func(order, from_status)。
    recomputes_amount=True = 这个钩子做的是「按当前接单人重算金额」——它是 skip_amount_recalc 唯一认的语义（见 apply_transition 的 skip_amount_recalc 段）；⛔ 不要给别的语义的钩子打这个标记：标记的语义精确到「重算金额」这一件事（例如回池钩子是「清钱 + 发撤单事件」、不是「重算」）。
    ⭐ wants_actor_id=True = 这个钩子要知道「这次迁移是谁做的」（认证态主键）⇒ _run_target_hooks 会以 func(order, from_status, actor_id=<pk 或 None>) 调它；今天只有回池钩子 performance._on_pool_entry 打它（它的两条留痕是人为动作、没有主键就只能是机器行，见 refunds.reverse_previous_round_refund）；⛔ 打了标记的钩子必须接受 actor_id 这个 keyword-only 形参（默认 None）。⚠️ 误标今天分两档（复跑实测）：非 IN_PROGRESS 目标态上的误标没有判据（SURVIVED）；把 IN_PROGRESS 上的通知钩子误标会被 tests/test_in_progress_recalc.py 的 E 档用例抓住（KILLED）⇒ 上面那条约定靠逐个枚举维持 ⛔ 不要当成「有判据保证」；⚠️ 注册表是模块级的、测试里注册过必须用 clear_target_hooks 清理。"""
    if recomputes_amount:
        setattr(func, _AMOUNT_HOOK_MARKER, True)
    if wants_actor_id:
        setattr(func, _ACTOR_ID_HOOK_MARKER, True)
    if wants_finance_policy:
        setattr(func, _FINANCE_HOOK_MARKER, True)
    _TARGET_HOOKS.setdefault(str(target), []).append(func)


def clear_target_hooks(target=None):
    """清空注册表（`target=None` 清全部）。测试用。"""
    if target is None:
        _TARGET_HOOKS.clear()
    else:
        _TARGET_HOOKS.pop(str(target), None)


def _run_target_hooks(order, from_status, *, skip_amount_recalc=False, actor_id=None, allow_financial_writes=True):
    """跑 order.status 这个目标态上注册的钩子。
    skip_amount_recalc=True ⇒ 只跳过带 _AMOUNT_HOOK_MARKER 标记的钩子（= 「按当前接单人重算金额」那一类）、其余钩子照跑；⛔ 不要改成「有开关就整段不跑」：IN_PROGRESS 上还注册了通知钩子（notifications._on_in_progress）、它排在钱钩子之后——整段跳过会把通知连同 apply_transition 尾上的副作用一起丢掉（完整理由见 apply_transition）。
    ⭐ actor_id：只传给带 _ACTOR_ID_HOOK_MARKER 的钩子（register_target_hook(..., wants_actor_id=True)）、其余钩子仍是两参调用 ⇒ 既有钩子契约一个字不改；actor_id=None 的含义是「这次迁移没有认证态」（常驻同步/机器路径）、由钩子自己决定怎么如实登记。"""
    for func in _TARGET_HOOKS.get(str(order.status), ()):
        if skip_amount_recalc and getattr(func, _AMOUNT_HOOK_MARKER, False):
            continue
        kwargs = {}
        if getattr(func, _ACTOR_ID_HOOK_MARKER, False):
            kwargs["actor_id"] = actor_id
        if getattr(func, _FINANCE_HOOK_MARKER, False):
            kwargs["allow_financial_writes"] = allow_financial_writes
        func(order, from_status, **kwargs)


#: 「回池」= 迁入这两个目标态。它们的钩子（`performance._on_pool_entry`）会把当前接单人
#: 那条 MAIN 绩效行的 `bonus_amount` / `taker_price` 清掉（spec §4.1.2 的七列回退）。
_POOL_TARGETS = frozenset({str(OrderStatus.UNASSIGNED), str(OrderStatus.WAITING_GRAB)})


def settled_pool_entry_denial(order, target):
    """R-10a 的判据：已结账的绩效行不许经「回池」被改钱。None = 放行。
    ⭐ 公开判据：本函数是「已结账回池」的唯一实现——apply_transition（真写路径）与 order_writes.transition_order 的 dry_run 预检都调它（spec §D5：同一份判据、两处调用）⇒ dry_run 也跑完 §4.2 列出的全部逐笔合格性（此前只跑第一道闸、同一函数的两条分支对同一笔单给出相反答案）；⛔ 不许在核心层再复制一份「能不能回池」的判断。
    为什么收在这里：R8 让 COMPLETED → UNASSIGNED/WAITING_GRAB 一步可达、而回池的钩子会把 MAIN 行的 bonus_amount / taker_price 清掉 ⇒ 只要还有一条回池路径不判结账、「已结账不许动钱」这条保护就被证伪（/clear-taker/、/throw-to-hall/、通用 /transition/ 都曾被绕过）；判据收在唯一写入口 apply_transition ⛔ 不在三个视图里各抄一遍（E-0021 的形状：同一事实两套判据、只用对了一套）。⚠️ 只覆盖池内两态：其余目标态一个字都不拦（/assign/ 的 R-2a 闸门是另一处、位置在视图里、覆盖的是「改派」这件事）；判据是 Performance.is_settled（三字段并集）；存量里有「已结账但没写 settled_at」和「只有 settled_amount」、只看状态列会放行而回退会动钱；统计与结账拒绝文案另有更窄的单列判法、对脏行结论可以相反、回池这一支不要跟着收窄；⚠️ 缺 MAIN 行（P2 遗留单）⇒ 没有行可被改钱 ⇒ 放行（钩子会补建一条初始行）。"""
    if str(target) not in _POOL_TARGETS:
        return None
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if perf is None or not perf.is_settled:
        return None
    return f"非法状态迁移：{order.status} → {target}（该单绩效已结账，不能改回池内）"


#: D4b 的拒绝理由（业务方口径逐字，spec §4.4.3 的 R4 句）。业务方原话：「已结账的订单如果要更改状态必须先改成未结账」⇒ 这句必须指路（「请先撤销结账」）⛔ 不带「非法状态迁移：<当前> → <目标>」前缀（旧稿那套前缀形状已作废）；前端 msg(errText(e)) 原样显示这一整句。
SETTLED_STATUS_CHANGE_MESSAGE = "该订单的绩效已结账，不能改状态；请先撤销结账后再操作"


def settled_status_change_denial(order, target, *, completing_dailiantong_settlement=False):
    """D4b 的判据：该单的 MAIN 绩效行已结账 ⇒ 拒绝状态变更。None = 放行。
    ⚠️ 判据本体复用模型属性 Performance.is_settled（三字段并集）——存量行里有「已结账但没写 settled_at」与「只有 settled_amount」两种形态 ⛔ 不要只看 settle_status 单列（那两种会被误判成「未结账」）；⚠️ 缺 MAIN 行 ⇒ 放行（没有行可被改钱；照 settled_pool_entry_denial 的既有口径）。
    ⚠️ 与 settled_pool_entry_denial 的分工：那条只管「回池两态」并保留自己的措辞、本判据排在它之后覆盖其余目标态（spec §4.1.3 的固定顺序第 4、5 步）；⚠️ 顺序（R4）：本判据先于 taker_present_pool_entry_denial——已结账时连状态都不许改、谈「先清除接手人」无意义。⚠️ ⛔ 不拦撤销结账（POST /api/business/settlements/revoke）：它只改绩效行的结账字段、不改订单状态、不走状态机（performance_writes 里 apply_transition 零命中）⇒ 本闸管不到它 ⛔ 不为它加例外（spec §4.4.2 #1）；⚠️ ⛔ 不拦上下游同步的直写路径（peer_inbound / jidanpu_hall 那几处 queryset .update / order.status = ...）：它们绕过唯一写入口 ⇒ 本闸对它们没有约束力、按 spec §4.4.5(B) / §8-1 如实登记为残差、本线不改它们的行为。"""
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if perf is None or not perf.is_settled:
        return None
    # 平台先结账的存量单可补走同一次验收的两步，保护本地打款和其它状态动作。
    if (
        completing_dailiantong_settlement
        and target in (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED)
        and order.status in (
            OrderStatus.IN_PROGRESS, OrderStatus.PENDING_REVIEW, OrderStatus.ISSUE,
            OrderStatus.PAUSED,
        )
    ):
        from apps.business.dailiantong_push import is_platform_settled_performance

        if is_platform_settled_performance(perf):
            return None
    # ⭐⭐ 例外：自动入账的行允许迁到「已退单」。为什么必须有这一条：钱包作为发薪唯一出口之后、每一张完成的单都会被自动标成「已结账」（accounts.payout.payout_completed_order）⇒ 不放行的话「完成后退款」这条业务动作会被 D4b 整条挡死（tests/test_refund_full.py 的「退单 → 回池 → 重做 → 再退单」链路当场红）；⚠️ 放行是有配套的：refunds.on_order_refunded 对 settled_by == "system" 的 MAIN 行写一条 -settled_amount 的冲账调整（钱包同事务被扣回、允许负）、并把这一行的结账痕迹复位 ⇒ 钱与状态一起回头；⛔ 只放行这一档：判据是「settle_status=已结账 ∧ settled_by="system"」——存量「财务手工结账」的行（settled_by 是财务用户名）照旧拒绝、规格 §3 明写「存量已结算的单不回冲（历史工资是财务手工付的）」。
    # ⭐ 同一条口径的第二个窄例外：自动入账的行允许迁回「待验收」。为什么：/review/ 的幂等用例（tests/test_review_api.py::test_approving_an_already_approved_row_is_idempotent）走的是「已完成 → 待验收 → 再点审核通过」、而自动入账之后每一张完成单都已结账 ⇒ 不放行就把那条既有行为（业务方裁定的「已通过 ⇒ 幂等」）整条打死；⚠️ 这一档不动钱：再点 /review/ 时该行已是「已通过」⇒ 幂等返回、不会再走完成；而万一它真的再次完成、payout_completed_order 的 settle_status 判据会直接返回（不重复加钱）⇒ 放行的代价只是「已完成单的奖金显示值可能与已付快照不同」、那正是规格 §5 已登记的「完成后改价不改绩效历史」残差。
    # ⭐ 同一条口径的第三个窄例外：自动入账的行允许再完成（待验收 → 已完成）。为什么：COMPLETED → PENDING_REVIEW → COMPLETED 是既有流程（改金额后重新验收）、自动入账之后第一次完成就已结账 ⇒ 不放行会让「重开再完成」永久 1002（既有用例 tests/test_performance_completion_activation.py::test_re_entering_completed_overwrites_the_bonus_without_duplicating_rows 红）；⚠️ 配套：payout_completed_order 在「已结账 + settled_by=system」时按新净额补差（只补不退、不重复加钱）；_activate_taker_performance 也放行自动入账的行覆盖 bonus_amount（否则第二次完成读到的还是旧值）——三处缺一不可；⛔ 其余目标态（代练中/池内两态/问题单/已暂停……）照旧拒绝。
    if (
        perf.settle_status == SettleStatus.SETTLED
        and perf.settled_by == "system"
        and str(target)
        in (
            str(OrderStatus.REFUNDED),
            str(OrderStatus.PENDING_REVIEW),
            str(OrderStatus.COMPLETED),
        )
    ):
        return None
    return SETTLED_STATUS_CHANGE_MESSAGE


#: **D2** 的后端 `msg`（业务方逐字，spec §4.1.2 的「文案」段 / R4）。
#: 前端 `order-orderlist.ts` 用 `msg(errText(e))` **原样显示**这一整句 ⇒ 全链路只有一个
#: 文案出处；⛔ 前端不另写一句，后端也**不加** `非法状态迁移：<当前> → <目标>` 前缀。
TAKER_PRESENT_POOL_ENTRY_MESSAGE = (
    "该订单已有接手人，请先清除接手人，再改为「待抢单」或「未分配」"
)


def taker_present_pool_entry_denial(order, target):
    """D2 的判据：订单当前有接手人时不许迁入「回池」两个目标态。None = 放行。
    ⚠️ 「有接手人」的权威判据 = 该单 emp_type=MAIN（打单工资）那一行的 user 非空；「一单最多一条 MAIN 行」由部分唯一索引 uniq_order_main_taker 保证 ⇒ .first() 语义安全（与 claim.py / settled_pool_entry_denial 同一查询形状）；⛔ 不要用 claimed_at 反推有没有接手人：该列可空、且它的 docstring 逐字说明了为什么「有 claimed_at」不等于「有接手人」；⚠️ Order 上没有任何接手人列：本判据只能查 Performance；⚠️ 本判据只覆盖 _POOL_TARGETS 那两个目标态（/transition/ → PAUSED 之类不经它；覆盖一切目标态的那道闸是 D4b（settled_status_change_denial、落在 apply_transition 守卫段的第 5 步）。
    ⚠️ 为什么必须有它：迁入池内两态时、回池钩子（performance._on_pool_entry → reset_taker_performance）会把当前接手人的七列静默回退 ⇒ 库里不会留下「池内状态却有人接」的矛盾、但用户看不到任何提示（spec §3.2 的现状段）；本线把那次静默清人改成「拒绝 + 提示『请先清除接手人』」。⚠️ 例外不在这里、也不按目标态猜：「显式清除当前接单人」这个动作由 apply_transition 的显式开关 allow_taker_release 表达、调用点逐个枚举（恰好两处，见 apply_transition 的 docstring）；本函数不知道调用方是谁。"""
    if str(target) not in _POOL_TARGETS:
        return None
    perf = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if perf is None or perf.user_id is None:
        return None
    return TAKER_PRESENT_POOL_ENTRY_MESSAGE


def apply_transition(order, target, *, nodes, actor="", actor_id=None,
                     hall_publisher=None,
                     hall_leave_reason: str | None = None,
                     suppress_outbound_sync: bool = False,
                     allow_taker_release: bool = False,
                     allow_status_change_while_pushed: bool = False,
                     completing_dailiantong_settlement: bool = False,
                     skip_amount_recalc: bool = False,
                     notify_worker: bool = True,
                     allow_financial_writes: bool = True,
                     sync_personal_orders: bool = True,
                     sync_peer_orders: bool = True,
                     notice_reason: str = ""):
    """改订单状态。非法迁移抛 IllegalTransition（由 API 层转成业务错误码）。 ⭐ actor_id（keyword-only、默认 None）：actor 是用户名（写 order.updated_by 与留痕的 actor 列）、而留痕的 actor_id 列要的是认证态主键——主键只能由调用点从认证态取（⛔ 不用 order.updated_by 反查用户凑 pk、那是"猜操作人"）⇒ 有认证态的调用点传 actor_id=request.user.pk / ctx.log_actor_id、机器/同步路径不传（= None）由接收方（_run_target_hooks → 钩子）如实登记成机器行；它只流进带 _ACTOR_ID_HOOK_MARKER 的钩子（今天只有回池钩子 performance._on_pool_entry）、其余钩子的两参契约不变。⚠️ nodes 是必填 keyword-only：「唯一写入口必须显式给出调用方持有的节点集」这条契约的机械落实——漏传会 TypeError 不会静默放行；匿名/零节点的调用方传空集即可（can_transition 取交集得空 ⇒ 拒绝）；⚠️ 显式传 None 也一律拒绝：None 是合法取值、而 can_transition(..., nodes=None) 的宽松语义是「这条边存在吗」——照单全收就等于在唯一写入口上留了一道 fail-open 门；这里把「没有身份」翻译成空集（= 拒绝）、can_transition 的宽松语义不动（P2 的边表测试靠它回答图论问题）。⚠️ 迁移副作用只在这里跑（_run_target_hooks）——所有改状态的路径都经此函数、所以新增路径不会漏掉副作用；不要在视图里另调一遍（那正是两套机制的来源）；⚠️ 保持 except IllegalTransition（不是 except ValueError）：钩子里冒出的 ValueError 是未预期错误、不该被伪装成「状态非法」（A2 / E-0010）。 ## ⭐ `hall_publisher`（发单人）：漏斗参数（F5，2026-09-23；D-0369，2026-11-03 改准口径）本漏斗**只管「进 `WAITING_GRAB`」（抛大厅）那几条路**：业务方口径「发单人 = 把订单抛到大厅的人」，两条进 `WAITING_GRAB` 的路（`/throw-to-hall/` 与通用 `/transition/`）都传 `request.user`，且**走这一个漏斗**——不在某个视图里另开第二个写入点。 ⚠️ ⭐ **D-0369（2026-11-03）起口径扩成「指派（含改派）也算发单动作」**，但那一半 **不经本漏斗**：`order_writes.assign_order` 在自己的事务里**直写** `order.hall_publisher`（写点与「谁写谁不写」的行为矩阵见它的 docstring）。为什么不经漏斗：漏斗写 `hall_publisher` 时会连带触发 `mark_published`（无 `moment`），抢在 `set_taker` 自己那次带 `moment=now` 的写之前把 `published_at` 写掉，打红「`published_at` 与 `claimed_at` 逐微秒相等」那条既有钉子；且改派支（状态已是 `IN_PROGRESS`）按 R-3 跳过迁移、根本没有漏斗调用可传。 ⚠️ **只有 `hall_publisher is not None` 时**才赋值并 append 进 `update_fields`：「没传」那一档的 `update_fields` 仍是 `["status", "updated_by", "completed_at", "updated_at"]` 四列（`tests/test_order_status.py` 有一条**整体相等**断言钉着它）。
    ⛔ 判据必须用 `is not None`（本项目铁律）：`User` 实例本身不可能是 falsy，但把「传了谁」与「没传」用真值判据区分会让下一个人以为传 `None` 之外还有别的空档。 ⚠️ 事务契约：select_for_update 要求调用方在事务里（transaction.atomic()）；autocommit 下 Django 在读码处（django/db/models/sql/compiler.py 的 SQLCompiler.as_sql）直接 raise TransactionManagementError ⇒ 响亮失败、不会静默降级成一次无锁读；今天 8 处产品码调用点都在 atomic() 内（order_views 6 处、performance_views 1 处、claim.py:215 由 order_views.claim 的 atomic() 提供边界）——但这是契约：新增调用点必须自己开事务、否则线上表现为 500。检查顺序（有意如此）：nodes is None → frozenset() ⇒ transition_denial(内存状态, target, nodes) ⇒ 复读比对 ⇒ 赋值 order.status ⇒ save(update_fields=...) ⇒ _run_target_hooks(...)；①transition_denial 放最前：它是纯内存的「边+节点」判据（最便宜、且是主契约）——让「边不存在/节点不够」的调用不必为一次 SELECT … FOR UPDATE 买单、也不让报错原因被并发状态盖住（那种情况下 order.status 是真是假都不该放行）；②复读比对放 transition_denial 之后、赋值之前：transition_denial 判的是「内存里的状态能不能走这条边」、复读判的是「内存里的状态还是不是真的」——两者缺一不可（只判边不复读 ⇒ 陈旧状态被当成真的写进去丢更新；只复读不判边 ⇒ 绕过状态机的边表）；③行锁（SELECT … FOR UPDATE）才是真正的串行化点：没有它本函数的「复读」与「写入」之间仍可插入另一个事务的提交（读-改-写的经典窗口）。 ⭐ suppress_outbound_sync（来源闸，I-7）：True = 「本次写入是由外部同步驱动的、不许再对外回声」——语义是只关掉本函数尾上那条「给已有下游链路排 member.status_out」的钩子 ⛔ 不跳过任何别的副作用（状态本身照写、_run_target_hooks 照跑、调用方的订单审计照记）；为什么需要它：下游鸡蛋铺线的回拉（pull_statuses）也经本函数写回远端拉来的状态、缺了这道闸每写回一笔就会排一条发往对方的改状态事件、而反向映射非双射（我方 IN_PROGRESS → 对方「代练中」、对方原本可能是「待上号」）⇒ 会把对方刚设的状态改掉；默认 False ⇒ 既有调用方行为一字不变；今天会显式传 True 的只有那条外部同步驱动的回拉（jidanpu_member._pull_statuses）。 ⭐ allow_taker_release（D2 的显式例外开关，R1 / spec §4.1.2）：只有「显式清除当前接单人」这个动作才传 True；本线逐个枚举的调用点恰好两处（⛔ 不写「凡是清人的都算」这类模糊判据）：①POST /orders/<no>/clear-taker/（api/order_views.py 里那次 apply_transition）——业务方点名的那一处、它的实现就是迁入 UNASSIGNED、不加开关会死锁（「要清人，请先清人」）；②绩效垃圾桶 POST /performances/<id>/delete/ 的 taker_cleared 支（api/performance_views.py 里那次 apply_transition）——调用的那一刻该 MAIN 行的 user 仍非空（清人是回池钩子随后做的）⇒ 不加开关这个功能会退化成「要删人先清人」死循环（spec §4.1.2 的「附带风险」段逐字论证）；⛔ 本线枚举到的 11 个真调用点里只有上面那两处传本开关、其余（/transition/ 与批量 batch/transition、/throw-to-hall/、/withdraw/、/assign/ 内部那次、/submit/、/review/、/claim/、同步收敛 jidanpu_converge.py、机器回拉 jidanpu_member.py）都不传 ⇒ 照拒；本开关只跳过 D2 那道判据（taker_present_pool_entry_denial）、其余判据（边/节点/复读比对/已结账）一个字不放过；⛔ 不要按 target == UNASSIGNED 猜（那会把 /transition/ 直接改成未分配也放行——等于没修）；⛔ 不要按端点名/URL 猜（视图层拿不到、且会漏掉别的调用方）。
    ⭐ skip_amount_recalc（钱钩子的显式例外，R10 / R11 / E-0163）：True = 「本次迁移不要跑『按当前接单人重算金额』那一类目标态钩子」——那一类由注册方声明（register_target_hook(..., recomputes_amount=True)）、本仓只标了一个：performance._on_in_progress（迁到「代练中」时按绩效行当前归属人重算 temp_amount）；_run_target_hooks 据此只跳过带标记的钩子——通知钩子与 apply_transition 尾上的副作用照跑（给已有链路排 status_out、peer_dispatch.notify_order_status_changed）；⚠️ 标记打在函数属性上 ⇒ 注册表的值形状不变（仍是 dict[str, list[func]]）。唯一调用点（逐个枚举）：order_writes.assign_order 内部那一次 apply_transition；本线枚举到的其余真调用点都不传（/transition/ 与批量 batch/transition、/throw-to-hall/、/withdraw/、/submit/、/review/、/claim/、同步收敛 jidanpu_converge.py、机器回拉 jidanpu_member.py、以及两处 allow_taker_release 调用点）。为什么 /assign/ 要传：该入口在同一次调用里紧接着就用本次要指派的人 set_taker 重算同一列 ⇒ 钱钩子那一炮的结果立刻被无条件覆盖（白算）、而它读的是马上要被换掉的旧接单人 ⇒ 旧人配置坏时（例如提成模式是「固定金额」）会挡住一次本来合法的改派、异常还会从 assign_order 冒出来变成 HTTP 500（E-0163 有实测）；⛔ R7 没有被放松：真算不出来（取不出基价/新接单人模式不匹配）仍由紧随其后的 set_taker 以同一个异常抛出、落进 assign_order 既有的 except (CommissionError, _AmountError) ⇒ transaction.set_rollback(True) + 原文弹错（对照：/claim/ 是「先条件更新接单人、再重算、最后迁状态」⇒ 钩子读到的就是抢单人、同值幂等、不改它）。 ⛔ 为什么不用另外两种省事的修法（两种都会丢东西、不要退回去）：吞异常（在那个调用点 except (CommissionError, _AmountError): pass）——异常是从 _run_target_hooks 里冒出来的 ⇒ 排在钱钩子之后的通知钩子被跳过、尾上那两条副作用也整段不跑；整段跳过钩子——省了 500、但通知钩子同样被跳过——判别力由 tests/test_in_progress_recalc.py 的 E 档用例钉住（订单从「待抢单」被指派时、抛单人必须收到一条「被抢走」）。⛔ 三条边界（逐个枚举的约定 ⛔ 不声称「标记面被用例守着」）：①标记的语义精确到「按当前接单人重算金额」这一件事——⛔ 不要给回池钩子（_on_pool_entry：回池时清钱 + 发撤单事件、不是「重算」）或 on_order_completed / on_order_refunded 打它——将来某个传本开关的回池调用会静默少发一条事件；②只有 order_writes.assign_order 内部那一次传 skip_amount_recalc=True（见上）；③把标记打错到别的钩子上、今天分两档（T8 指出、复跑实测 ⛔ 不写成「有用例守着」这类绝对句）：非 IN_PROGRESS 目标态的钩子上误标没有判据——实测：额外给 _on_pool_entry 打上标记后整跑本线 12 个相关测试模块得 Ran 346 / OK（0 个 FAIL:/ERROR:）⇒ SURVIVED；把 IN_PROGRESS 上的通知钩子误标则会被 tests/test_in_progress_recalc.py 的 E 档用例抓住——实测 KILLED ⇒ 这是逐个枚举的约定、不是「有判据保证」。 ⭐ allow_status_change_while_pushed（A2 的显式放行开关，计划 Rev.9 §2）：默认 False = 「普通状态变更」——默认这一档会被那道闸拒掉：只要该单有一条 state=sent 的代练通推送行、业务方口径是「状态必须与代练通一致（绝不能不一致）」⇒ 手工改出来的状态会与远端当场分叉；判据本体在 dailiantong_push.local_status_locked_denial（本函数只调用它）。会显式传 True 的调用点（逐个枚举；⛔ 人类端点一个都不许传）：①dailiantong_push.apply_remote_status / _apply_local_status——代练通同步把远端事实写回本地（Rev.9 §2、轮询与回调两条路都经它）；②dailiantong_push.ensure_internal_hall——抛代练通前的补发内部大厅（Rev.9 §3）；③dailiantong_push._assign_placeholder_taker → order_writes.assign_order——被代练通接单后把接手人记成占位账号「代练通打手」（Rev.9 §2）；④claim_order → apply_transition——内部打手先接时本地可以先进入「代练中」（Rev.9 §4 第 2 条：随后由同步把代练通订单撤掉）；⑤order_writes.assign_order 的 allow_status_change_while_pushed 形参：只有机器路径（③）传 True、人工 /assign/ 不传。⚠️ 本开关只跳过这一道闸：边/节点/复读/已结账/D2 一个字都不放过。"""
    if nodes is None:                 # 显式传 None = 没有身份 ⇒ 拒绝，不要当成"不校验"
        nodes = frozenset()
    denial = transition_denial(order.status, target, nodes)
    if denial is not None:
        raise IllegalTransition(denial)
    # ⚠️ 复读回来的数据库真实状态才进异常消息：只用内存里的 order.status 会给用户打印「非法状态迁移：WAITING_GRAB → IN_PROGRESS」、而事实是这单已经被撤回成 UNASSIGNED——消息与事实不符会让排障的人朝错方向找（AGENTS §5「技术文档必须按实现写」的同族）；这一改不动既有用例：它们断的是子串「非法状态迁移」。
    # ⭐ D-0144（A5）：加锁读走默认 manager ⇒ 已软删的单取不到（DoesNotExist）⇒ 改不了状态；⛔ 改成 all_objects 会让「已删单还能流转」重新成立。
    locked = Order.objects.select_for_update().only("status", "peer_direct_pending").get(pk=order.pk)
    if locked.peer_direct_pending is not None:
        raise IllegalTransition(PEER_DIRECT_PENDING_MESSAGE)
    if locked.status != str(order.status):
        raise IllegalTransition(
            f"非法状态迁移：{order.status} → {target}"
            f"（该单当前状态已是 {locked.status}）"
        )
    if not jidanpu_assignment_lock.lock_order_change(order.pk):
        raise IllegalTransition(jidanpu_assignment_lock.ORDER_BUSY)
    # ⭐ R-10a（控制方裁定；spec §4.5）：跑目标钩子之前判「已结账的绩效不许回池」。位置在复读（= 库里的状态是真的）之后、改写状态之前 ⇒ 被拒时一行都不写（连状态列的那次 save 都还没发生）、而不是靠调用方 atomic() 回滚兜底。
    # 各调用方既有的 except IllegalTransition → 业务码 通道因此统一生效 ⛔ 不需要三处各判一遍。
    settled_denial = settled_pool_entry_denial(order, target)
    if settled_denial is not None:
        raise IllegalTransition(settled_denial)
    # ⭐ 本段的检查顺序是 R4 裁定的固定顺序（spec §4.1.3）：先判「已结账 ⇒ 拒绝一切状态变更」（D4b、覆盖一切目标态）、再判「有接手人 ⇒ 不许回池」（D2）。
    # 目标落池内两态时、上面那条池内闸先命中、报的是池内那句（同样含「已结账」）；其余目标态才落到下面这道：已结账拒绝一切状态变更、并且排在「有接手人不许回池」之前。
    # 不要对调这几段：对调之后池内目标的报错不可观测地变了。
    if Performance.objects.filter(
        order=order, online_payroll_batch__isnull=False, settled_at__isnull=True,
    ).exists():
        raise IllegalTransition("订单有正在处理的在线付款，暂时不能更改状态")
    status_change_denial = settled_status_change_denial(
        order, target, completing_dailiantong_settlement=completing_dailiantong_settlement
    )
    if status_change_denial is not None:
        raise IllegalTransition(status_change_denial)
    # ⭐ D2（spec §3.2 / §4.1.2）：有接手人 ⇒ 不许迁入池内两态。位置与上一条同段——都在「复读之后（= 库里的状态是真的）、写任何东西之前」⇒ 被拒时一行都不写（连状态列的那次 save() 都还没发生）、而不是靠调用方 atomic() 回滚兜底；各调用方既有的 except IllegalTransition → 业务码 通道因此统一生效。
    # ⚠️ allow_taker_release=True 只跳过这一条（= 「显式清除当前接单人」那两处枚举出来的调用点）、其余判据一个字不放过。
    # ⚠️ 登记：枚举到的两处开关调用点目标态都是 UNASSIGNED（池内）⇒ 第 4 步既有池内闸必先命中、本条性质结构性不可观测（spec §8-17；⛔ 不是「已有判据覆盖」）。
    if not allow_taker_release:
        taker_denial = taker_present_pool_entry_denial(order, target)
        if taker_denial is not None:
            raise IllegalTransition(taker_denial)
    # ⭐⭐ A2（计划 §2）：已抛到代练通的单、禁止手工改状态。位置：复读之后（= 库里的状态是真的）、写任何东西之前 ⇒ 被拒时一行都不写。
    # 为什么在唯一写入口：所有改状态的路径都经这里（/transition/、批量、/submit/、/review/、/report-issue/、退款……）⇒ 一处拦截覆盖全部 ⛔ 不在各视图各判一遍；⚠️ 判据本体在 dailiantong_push.local_status_locked_denial（⛔ 不在这里抄「什么算已抛单」）；函数内懒导入：本模块是全仓最热的写入口、不能把它拉进顶层导入图。
    # ⚠️ 放行的只有两档（都由显式开关表达、各调用点逐个枚举）：①allow_status_change_while_pushed=True：代练通同步自己回写（Rev.9 §2）与「内部打手先接/指派」（Rev.9 §4——代练通那边还没被接时内部可以先接）；②目标就是当前状态（自环）在更早就被 transition_denial 拒掉、与本闸无关。
    if not allow_status_change_while_pushed:
        from apps.business.dailiantong_push import local_status_locked_denial

        locked_denial = local_status_locked_denial(order)
        if locked_denial is not None:
            raise IllegalTransition(locked_denial)
    from_status = order.status

    if actor:
        order.updated_by = actor
    # 完单时间（spec §6 的写入点表）：进「已完成」写、离开「已完成」清。
    # completed_at 作为本地流转的附加列一起保存，数据库断言见 test_order_column_fields.py。
    # ⚠️ 判据用 str(...)：Order.status 存的是字符串、target 可能是 OrderStatus。
    if str(target) == OrderStatus.COMPLETED:
        order.completed_at = timezone.now()
    elif str(from_status) == OrderStatus.COMPLETED:
        order.completed_at = None
    if hall_publisher is not None:
        order.hall_publisher = hall_publisher
    persist_order_status(
        order, str(target),
        transition_fields=("updated_by", "completed_at"),
        published=hall_publisher is not None, actor_id=actor_id, notify_worker=notify_worker,
        notice_reason=notice_reason,
    )
    # ⭐ 大厅离厅快照（D-0296）：成功离开大厅（from == WAITING_GRAB 且目标不是它）⇒ 存一条「离厅那一刻的在厅视图」灰卡；写入点枚举与覆写理由见 hall_snapshot.py 的模块 docstring；exit_kind 只有两档：目标是「代练中」= 被接走（claimed）、其余一切出边 = 撤回/离厅（withdrawn）。
    # ⚠️ 函数级懒导入（照本文件既有先例）：hall_snapshot 要经 api.serializers 序列化载荷、顶层 import 会把 API 层拉进全仓最热写入口的导入图；⚠️ 快照是读面的辅助数据：它失败不许把一次合法的状态迁移打挂（与上面的群消息推送同一条口径——提示面失败不得回滚业务）、但必须响亮留痕。
    if str(from_status) == str(OrderStatus.WAITING_GRAB) and str(target) != str(OrderStatus.WAITING_GRAB):
        from apps.business import hall_snapshot

        exit_kind = (
            "claimed"
            if str(target) == str(OrderStatus.IN_PROGRESS)
            else "withdrawn"
        )
        try:
            hall_snapshot.record_native_exit(order, exit_kind=exit_kind)
        except Exception:  # noqa: BLE001 —— 辅助数据失败不回滚状态迁移（响亮留痕）
            logger.exception(
                "大厅离厅快照写入失败（不影响状态迁移）order=%s", order.order_no
            )
    # ← 目标态钩子的出口，且在保存**之后**；`skip_amount_recalc` 只跳过带标记的钱钩子。
    # ⚠️ `actor_id` 只给带 `_ACTOR_ID_HOOK_MARKER` 的钩子（`wants_actor_id=True`）。
    if completing_dailiantong_settlement and target == OrderStatus.COMPLETED:
        from apps.business.settlement_hooks import activate_early_dailiantong_taker_cost

        activate_early_dailiantong_taker_cost(order)
    _run_target_hooks(
        order, from_status, skip_amount_recalc=skip_amount_recalc, actor_id=actor_id,
        allow_financial_writes=allow_financial_writes,
    )
    # ⛔⛔ 「验收即入账」已停用（spec docs/specs/2026-10-23-taker-income-and-early-settlement.md §3.1）：这里原先会调 apps.accounts.payout.payout_completed_order(order)、把该单 MAIN 绩效的应付款打进打手钱包余额、并把绩效行标「已结账」（D-0279 收窄成只对 App 注册打手）；业务方口径：钱包只做充值/押金冻结/提现、订单绩效不再走钱包 ⇒ 今天不再调用；订单进 COMPLETED 照旧生成绩效行（_run_target_hooks 里的 on_order_completed 那条链一个字没动）、钱由财务按「结账状态」线下打款（「已结账」= 已打款）。
    # ⚠️ 函数保留（accounts/payout.py、docstring 写明只用于历史/人工场景）：存量 settled_by="system" 的行、以及已结账行上的调整冲账接收器都还要能工作；⛔ 这不是开关：把它加回来 = 重新打开「每张完成单自动发钱」那条业务规则；⚠️ 提现/押金/充值三条链一个字节都没动。
    # ⭐ 订单消息推送（「自动发消息到微信群设置」线的唯一触发点）。位置：状态已 save()、目标态钩子已跑完之后 ⇒ 消息里的状态/金额都是最终值。抛单到大厅 ⇒ hall 事件（hall_publisher is not None 才认、撤回/普通迁移不算）；迁入「重要变化」那 6 个状态 ⇒ important 事件；两者由 order_push.queue_status_change_push 内部互斥判断（WAITING_GRAB 不在重要集合里）。⚠️ 函数内懒导入：与上面几条同款、避免把 order_push 的模型/urllib 拉进本模块的顶层导入图；⚠️ 真正发 HTTP 在 transaction.on_commit 之后、且在线程里 ⇒ 订单状态变化不等外部服务；⚠️ suppress_outbound_sync=True（外部同步驱动的回拉）时跳过：那条路不该再往群里回声。
    if not suppress_outbound_sync:
        from apps.business.order_push import queue_status_change_push

        # ⚠️ **不许让群消息拖垮订单状态**（与 `Notification` 同一口径：提示不是凭据）：
        #    入队要查规则表、拼消息；这里任何异常都只记日志，状态变更照常提交。
        try:
            queue_status_change_push(
                order, from_status=from_status, hall_publisher=hall_publisher
            )
        except Exception:  # noqa: BLE001 - 见上：通知面失败不得回滚业务
            logger.exception(
                "订单消息推送入队失败（不影响订单状态）order=%s", order.order_no
            )
    # ⭐ 下游鸡蛋铺线（spec §5.3 最后一行）：我方状态变了 ⇒ 给已有本线链路的绑定排一条 member.status_out。位置在唯一写入口上 ⇒ 一处覆盖全部状态变更路径（/transition/、/submit/、/withdraw/、/clear-taker/、退款……）。
    # ⚠️ 零绑定即零成本：queue_status_out 自己先做一次取链路的查询、空就立刻返回（这条路径是全仓最热的写入口 ⛔ 不许为每次状态迁移加一堆查询——判据见 tests/test_jidanpu_member_triggers.py::ZeroBindingCostTests）；⚠️ 必须函数内懒导入：performance（本模块的钩子注册方）顶层 import 本模块、而 jidanpu_member → jidanpu_hall → performance ⇒ 顶层 import 会成环；⚠️ suppress_outbound_sync=True 时整段跳过（外部同步驱动的回拉、见上面的签名段）：只关这一条、别的副作用一个字都没动。
    # 来源态取本次迁移之前的状态，供下家同步识别恢复「代练中」。
    # 合并在飞任务时采用最新请求的来源态，消费者不从订单现态反推。
    if not suppress_outbound_sync and sync_personal_orders:
        from apps.business import jidanpu_member

        jidanpu_member.queue_status_out(order, from_status=from_status)
        # ⭐ 回池后重新进大厅（业务方口径）：目标态是 WAITING_GRAB ⇒ 先退掉下家那张旧副本、再按大厅状态重新录单 + 抛厅（⛔ 不能只退不建）；位置在唯一写入口上 ⇒ /throw-to-hall/、录单即抛、通用 /transition/ 三条进厅路径共用同一份判据（幂等：端点自己那次调用由 queue_member_event 的在飞去重归并）；from_status = 本次迁移之前的状态（自环不算进厅）。
        # ⚠️ 与 queue_status_out 同一条懒导入 / 同一个 suppress_outbound_sync 闸（外部同步驱动的回拉不回声）。
        jidanpu_member.queue_hall_return(order, from_status=from_status)
        # ⭐ 上家鸡蛋铺线（spec §6.2 / §6.4 / §15.4 / §15.6）：进「已完成」⇒ 给已有本线链路的绑定排 upstream.complete（只排队）；退款/问题单 ⇒ 零上游写、只留一条台账人话（A12 / A14）。
        # ⚠️ 必须留在 if not suppress_outbound_sync: 的同一个块里（B7 来源闸）：回拉写回（follow_refund / pull_statuses）既不许排事件、也不许写那句「我方退款不同步上游」的人话——那会把「上家退我们的单」记成「我方退的」、指错方向（判据见 tests/test_jidanpu_upstream_triggers.py）。
        # ⚠️ 函数内懒导入：本模块顶层不能 import 它（jidanpu_upstream 顶层 import 了本模块 ⇒ 成环）、与上面 jidanpu_member 同一手法；⚠️ 零链路即零成本：on_order_status_changed 只在进「已完成」那一档查一次链路（queue_complete 的短路）、退款/问题单/其余状态是纯 Python 分支 ⇒ 判据见 tests/test_jidanpu_upstream_triggers.py::CompletionTriggerTests。
        from apps.business import jidanpu_upstream

        jidanpu_upstream.on_order_status_changed(order, from_status)
    # ⭐ P3：状态变化的唯一入队漏斗（上行 + 下行都在这里面判）；⚠️ 函数内 import：peer_dispatch 反向 import 了 order_status 的类型没有、但把这条放在模块顶部会让 business 的导入图变脆（且 P2 已用同一手法回避环）。
    # ⛔ 本段抛的异常不被上面的 except IllegalTransition 捕获（那是 ValueError 的另一支）。
    if sync_peer_orders:
        from apps.business.peer_dispatch import notify_order_status_changed

        notify_order_status_changed(order)
    # ⭐ 团团通之间的大厅镜像（D-0303）：进/出 WAITING_GRAB 的唯一接线点——进厅（抛单到大厅）⇒ hall.throw、出厅（被接走/撤回/指派）⇒ hall.take；判据与写点枚举见 peer_dispatch.notify_hall_membership 的 docstring。
    # ⚠️ 函数内 import：与本文件其余跨线调用同一条防环纪律。
    if sync_peer_orders:
        from apps.business.peer_dispatch import notify_hall_membership

        notify_hall_membership(
            order,
            from_status=from_status,
            target=target,
            hall_publisher=hall_publisher,
            leave_reason=hall_leave_reason,
        )
    if hall_publisher is not None and str(target) == str(OrderStatus.WAITING_GRAB):
        from apps.internal_chat.order_groups import publish

        publish(order, hall_publisher)
    # ⭐ 开放平台 v2（T8，设计文档 §7.2 / §7.2.1 裁决 1）：状态变化 ⇒ 在与本次写入同一个事务里写一行推送 outbox（⛔ 不是 on_commit：那会让业务回滚了、推送行却留下，见 apps/openapi/dispatch.py 的模块 docstring 与 §7.4 的改准块）。
    # ⚠️ 位置在 suppress_outbound_sync 闸之外（就排在上一句 peer 通知的旁边）：那道闸的语义是「本次写入由外部同步驱动、不许再往那个通道回声」（它关的是发回鸡蛋铺的两条线、见上面 if not suppress_outbound_sync: 段）；而第三方应用是另一个当事人、它和上面那条 peer 通知一样必须知道「这张单的状态真的变了」——回拉回来的状态也是真实的本地状态（§7.2.1 裁决 1 逐字）。
    # ⚠️ 函数内懒导入（与上面两句同款）：本模块是全仓最热的写入口、顶层 import apps.openapi 会把它的模型/出站模块一起拉进 business 的导入图；⚠️ 零应用即零成本：notify_* 自己先做一次查应用的查询、空就立刻返回（与 jidanpu_member.queue_status_out 同一条纪律）；⚠️ 时刻显式取（⛔ 不用 order.updated_at 之外的东西、但也不用它的"省事"写法）：这里 order.status 刚刚 save() 过、timezone.now() 就是这一刻；⚠️⭐ spec=OPENAPI_SPEC 是显式的（回调线 T1b）：投递引擎的公开入口从 T1b 起不再有"默认回上家侧"那一格（漏传 = 当场 TypeError）⇒ 本处（上家侧那一族的写入点）显式声明自己是哪一族 ⛔ 不靠默认值省这一句（理由见 dispatch 模块 docstring 第 4 条的改准块）。
    from apps.openapi import dispatch as openapi_dispatch
    from apps.openapi.credentials import OPENAPI_SPEC
    from apps.takerapi.api import callback_recipients as taker_callback

    # ⚠️ 下面两族共用**同一个**时刻对象（`moment`）：同一次状态变化、两个当事人，
    #    时刻必须逐字相同（各取一次 `timezone.now()` 会差几微秒，对端对账时看着像两件事）。
    moment = timezone.now()
    openapi_dispatch.notify_order_status_changed(
        order, spec=OPENAPI_SPEC, status=order.status, occurred_at=moment
    )
    # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 1 行）：紧挨着上面那句、同一个事务、同一个 moment。⛔ 不能拿"已经推给上家应用了"当省掉——两个当事人（上家 = 把单派给我们的第三方；打手 = 在我们这儿接单的外部工作室）。
    # ⚠️ 收件人语义与上面那条不同（打手侧只推"这张单的当前接手人"名下那一个应用，spec §4.2）⇒ 走本族自己的包装 ⛔ 不复用上面那三个（它们内部写死 eligible_apps()）；⚠️ 本函数上方那段"为什么在 suppress_outbound_sync 闸之外"对打手侧同样成立：那道闸关的是"发回鸡蛋铺的两条线"、而打手应用是另一个当事人。
    taker_callback.notify_order_status_changed_for_taker(
        order, status=order.status, occurred_at=moment
    )
    # ⭐ 盖「我方最后一次动作时刻」（业务方口径「以最后更改的为主」）；下家侧收到上家事件时用它挡更旧的事件（peer_inbound.apply_events 的并列判据）。
    # ⭐ A8 / L-6 起这段收口成唯一实现 stamp_local_action（判据与口径逐字保留在那边；本入口之外，备注/项目/电话/QQ/发单价的本地写点也调同一个函数）。
    stamp_local_action(order)
    if target == OrderStatus.WAITING_GRAB:
        from apps.accounts.wxpusher_hall import queue_hall_publication

        queue_hall_publication("local", order, listed_at=moment)
    return order
