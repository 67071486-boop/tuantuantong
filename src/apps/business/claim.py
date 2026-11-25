"""抢单：限制校验 + 条件更新（spec §7 / §7.2 / §7.4）。 **纯逻辑，不碰 request**，也不收 ORM 对象——收 **ID**。原因：并发测试要在多个线程里调用它，而线程各有独立连接、各自的 schema 上下文；传 ID 让函数自己在**正确的 schema 下** 重新取行，比跨线程传对象安全。 📌 价格区间与持单上限**按人读取**——取值统一走 `apps/business/grab_limits.py::limits_for_order(user_id, candidate)`（缺档案 ⇒ 回落全局常量；
`order_type` 决定取 `dl` 还是 `pw` 上限）。本模块**不自己读 `StaffProfile`**，也不自己定这三个常量（同一事实两处判据必然漂移，E-0021 的形状）。 📌 「禁自助接单」同样走这一层（`limits.forbid_self_service`）——本模块 **仍然不查库**；那一项的缺档案语义是「不禁止」而非回落，见 `grab_limits.py` 口径 2。 📌 **实名闸（D-0245）**：「未实名不允许接单」也走这一层——判定单一出处
`apps/realname/services.py::ensure_can_claim`（开关 + 代练员 + 未实名），本模块只做异常翻译（`RealNameClaimBlocked` → `ClaimError`）；机器路径不进本函数，见裁定 8。 📌 **裁定 8**：这些**按人**闸门（整层 `check_grab_limits`：四支限制 + D-0245 的实名闸）在 `claim_order(..., enforce_limits=False)` 下**整层跳过**——只服务**机器自动接手** （下游鸡蛋铺线的日志快路）；人类入口一律默认 `True`，对它们闸门一个字不动。"""
from django.contrib.auth.models import User
from django.utils import timezone

from apps.business import order_time_fields
from apps.business.grab_limits import limits_for_order
from apps.business.models import EmpType, Order, OrderStatus, Performance
from apps.business.order_status import apply_transition
from apps.business.performance import recompute_temp_amount
from apps.core.error_codes import ErrorCode
from apps.realname.services import RealNameClaimBlocked, ensure_can_claim

# ⚠️ 持单上限与价格区间**不在本模块定义**，也不在本模块读全局值：三个常量已收口到 `apps/business/grab_limits.py`，**按人读取**也已接上——本模块的判据用
# `limits_for_order(user_id, candidate)` 取**生效值**（缺档案时才由读取层回落全局常量），
# 避免同一事实两处定义（`AGENTS.md` §4）。

# 订单未完成 = 除「已完成 / 已退单」之外都算占着额度。池内两个状态（未分配 / 待抢单）**不在这里**：D-0032 之下它们保证 MAIN 行的 `user` 为空，
# 而这里数的正是 `user_id = 自己` 的行——所以两条口径对「被持有的行」是同一个集合（见决策记录 `.agents/notes/implemented/architecture/2026-09-20-pool-states-mean-no-taker.md`）。
_ACTIVE_STATUSES = (
    OrderStatus.IN_PROGRESS,
    OrderStatus.PENDING_REVIEW,
    OrderStatus.ISSUE,
    OrderStatus.PAUSED,
)


class ClaimError(Exception):
    """抢单失败。`code` 直接给 API 层当业务码用。 ⭐ `defused`（业务方报障修法一）：**仅**在「拒绝的同时完成了要保留的业务写入」时由抛出方置 `True`（今天唯一一例 = `claim_upstream_mirror` 命中停用 /
    缺失绑定的僵尸卡 ⇒ `listed → gone` 拆雷）。带幂等装饰的对外端点（`@idempotent` 对失败信封默认连业务写入一起回滚）据此调 `idempotency.mark_side_effect` 保住那笔
    写入。⛔ 判据是「真的落库了」，不是「非 0 码就标记」（见 `mark_side_effect` 的 docstring：一行都没写的拒绝必须保持「同键可重试」）。"""

    def __init__(self, message, code, *, defused: bool = False):
        super().__init__(message)
        self.code = code
        self.defused = defused


def _has_taker(order_id):
    """这条订单的 MAIN 绩效行是否已经归属某人（D-0032 之下它同时是「池内 ⇔ 无人」的判据）。"""
    return Performance.objects.filter(
        order_id=order_id, emp_type=EmpType.MAIN, user_id__isnull=False
    ).exists()


def check_grab_limits(*, user_id, order=None, candidate=None):
    """抢单前置校验（`order=` 老调用点 / `candidate=` 镜像单，二者必居其一）。**必须在条件更新之前**做。五支的顺序（前四支按 spec §7.2 的表，实名闸（D-0245）排在「禁自助接单」之前（spec §2.3 指定））： **NULL 接单价 → 实名闸 → 禁自助接单 → 接单价区间 → 接单上限**。 ⚠️ 五支里只有「NULL 接单价」那一支**在按人读取之前**、不查库；「禁自助接单」那一支复用紧接着的同一次 `StaffProfile` 读取 ⇒ 不额外查库。
    ⚠️ **这五支共用一个码** `GRAB_LIMIT_EXCEEDED`（同一个用户可见结果：「你现在抢不了」），所以每支的 msg 必须能独立说明**是哪一支**——测试与前端都靠它区分，只断码区分不出来。 ⚠️ **支数不是契约**：判据的权威清单就是本函数（`apps/core/error_codes.py` 的同一句话同步写着「五件事」），别在别处把数量写死。（`rowcount == 0` 那一支走 `ORDER_ALREADY_TAKEN`，不在这里，见 `claim_order`。）
    📌 价格区间与接单上限的**取值来源是这个人**（`limits_for_order`）：缺档案 ⇒ 由读取层回落全局常量；`order_type == COMPANION` ⇒ 上限取 `grab_limit_pw`，其余取值 ⇒ `grab_limit_dl`。 📌 「禁自助接单」也来自同一次读取：`limits.forbid_self_service` 为真 ⇒ 拒。缺档案 ⇒ 读取层给 `False`（**不禁止**）——与上面两项的「回落」语义相反，见 `grab_limits.py`。 📌 实名闸（D-0245）的判定**不在本模块**：`ensure_can_claim`（单一出处）查「开关 + 代练员 Group + 未实名」，本模块只把 `RealNameClaimBlocked` 翻成同码的 `ClaimError`。"""
    candidate = candidate if candidate is not None else GrabCandidate.from_order(order)
    # ⚠️ **NULL 接单价 ⇒ 不可抢**（Ruling 49）：`hall_price` 是 `null=True`，抛单不传价时
    # 它就是 NULL ⇒ 大厅里真的会有「没有接单价」的单。修之前这里整支跳过，那张单反而**不受任何价格判据**被抢走——代练员在接一个**不知道多少钱**的活。
    # 这是业务限制，**不是**给金额列加约束（D-0015 只管列约束）。
    if candidate.hall_price is None:
        raise ClaimError(
            "该单没有接单价，请联系客服", ErrorCode.GRAB_LIMIT_EXCEEDED
        )
    # 📌 按人限制的读取放在**这一支之后**：上面那一支不查库，最便宜的拒绝
    #    不该为一次 `StaffProfile` 查询买单。
    limits = limits_for_order(user_id, candidate)
    # 📌 实名闸（D-0245，spec `docs/specs/2026-10-16-realname-claim-gate-design.md` §2.3）：位置在「禁自助接单」判定**之前**（spec 指定）。判定单一出处 `apps/realname/services.py::ensure_can_claim`（开关 + 代练员 Group + 未实名，
    # ⛔ 本模块不抄第二份）；这里只做异常翻译：`ClaimError` + 同码 `GRAB_LIMIT_EXCEEDED`，文案逐字（E-0024）且必须与同码其它支可区分—— 这一支说的是「先去实名」，不是「你被配置禁了」。 ⚠️ 本函数收 **ID** 不收实体（模块 docstring）⇒ 按 ID 取人；人已不存在（陈旧 ID）
    # 取到 `None` ⇒ 闸侧放行（后续的条件更新 / 状态机仍在）。 ⚠️ 机器自动接手（裁定 8）`enforce_limits=False` 根本不进本函数 ⇒ 闸天然不拦机器路径，⛔ 不许把闸塞进 `machine_claim_order`。
    try:
        ensure_can_claim(user=User.objects.filter(pk=user_id).first())
    except RealNameClaimBlocked:
        raise ClaimError("请先完成实名认证后再接单", ErrorCode.GRAB_LIMIT_EXCEEDED) from None
    # ⚠️ 「禁自助接单」：这是「你根本不该在大厅接单」的**硬开关**，比价格/上限更该先报；它复用上面**同一次** `StaffProfile` 读取 ⇒ 不额外查库。
    # 口径（业务方裁定）：**大厅照常能看、只在抢单时被拒**；`/assign` **不禁**；缺档案 = **不禁止**（读取层给 `False`）。msg 必须与另外三支可区分（同码四支）。
    if limits.forbid_self_service:
        raise ClaimError("你已被设置为禁止自助接单", ErrorCode.GRAB_LIMIT_EXCEEDED)
    # ⚠️ 上面那一支已经把 `None` 挡住了，所以这里直接比较；**不要**改成真值判断（`if price:`）：D-0015 之下 `0` 是合法金额，真值写法会把它当成「没传价」。
    # 这个差异**是可观察的**：某人把下限配成 `100` 时，`price = 0` 必须被这一支拦下（`0 < 100`），真值写法会静默放过去（AGENTS.md §5 的 E-0021 形状）。
    # 真正要防的是把「接单价必须 > 0」这类下限顺手加回来。
    if candidate.hall_price < limits.price_min or candidate.hall_price > limits.price_max:
        raise ClaimError(
            f"接单价必须在 {limits.price_min}~{limits.price_max} 之间",
            ErrorCode.GRAB_LIMIT_EXCEEDED,
        )

    # ⭐ D-0144（C18）：已软删订单上的 MAIN 行**不算持有量**。跨关系 JOIN 不经过 `Order`
    #    的默认 manager ⇒ 必须在这里显式补（spec §4.3-C2）。
    held = Performance.objects.filter(
        user_id=user_id, emp_type=EmpType.MAIN,
        order__status__in=_ACTIVE_STATUSES,
        order__deleted_at__isnull=True,
    ).count()
    # ⚠️ `limits.max_active is None` ⇒ **不限制**（档案里配的是 `0`）⇒ 上限这一支**整支跳过**， **不要**拿 `None` 去比大小。口径：`None` = 不限制，与 `0` 不同——`0` 在读取层（`apps/business/grab_limits.py`）就被翻成 `None`；而「未配」不是 `None`，它由缺档案
    # 触发、回落到正整数 `10`（读取层的 `GLOBAL_MAX_ACTIVE`）。判据用 `is not None`、不用真值（E-0021）。 ⚠️ 本处用真值写法**恰好**等价（读取层已把 `0` 翻成 `None`，所以 `max_active` 只可能
    # 是 `None` 或 ≥1）；但这个等价**不是**「本支可以不判空」的理由——注意本支对 `0` 的正确处理**同样依赖**读取层那条归一化（`0` 若流到这里，`is not None` 会走成 `held >= 0` 恒真）。真正要守的是「`None` ⇒ 跳过」这个意图本身，规则仍是 `is not None`。
    if limits.max_active is not None and held >= limits.max_active:
        raise ClaimError(
            f"你手上未完成的订单已达 {limits.max_active} 单",
            ErrorCode.GRAB_LIMIT_EXCEEDED,
        )


def claim_order(*, user_id, order_id, nodes, enforce_limits: bool = True,
                suppress_outbound_sync: bool = False,
                sync_personal_orders: bool = True,
                sync_peer_orders: bool = True,
                enforce_deposit: bool | None = None):
    """抢单：**条件更新**，靠数据库保证原子性（spec §7.1）。 `rowcount == 1` = 抢到；`0` = 已被别人接走。 ⛔ **锁序承重（Z2）**：「`UPDATE ... WHERE user_id IS NULL` 本身是原子语句」这一半 **仍然成立**（抢单胜负的裁决还是它）；本函数在**首次碰钱包之前**会对 `Order` 行本函数在**首次碰钱包之前**会对 `Order` 行 `select_for_update()`，统一「**先锁 Order、再锁钱包**」——此前是先由 `cancel_release_on_claim` / `ensure_deposit_for_claim` 拿钱包行锁、再由 `apply_transition` 拿 Order 行锁，与改派 / 退款钩子的「Order → 钱包」正好相反 ⇒ 两个并发入口可以互锁到死（一方 500）。修法见下面第一次碰钱包之前的 Z2 注释。 ⚠️ `enforce_deposit`：None = 回落租户总闸；True / False = 调用方显式覆盖。它**不受** `enforce_limits` 影响——机器接手跳的是「按人闸门」，押金要由那条路径显式传 `False`（见 `machine_claim_order`）；留在 `enforce_limits` 里会把显式 `True` 静默吃掉。 ⭐ **唯一例外**（D-0296 优选强制押金，H-1）：**优选单**上，人类路径（`enforce_limits=True`）即使显式传 `False` 也会被覆盖成强制——判「是不是人类路径」正是用 `enforce_limits`（机器路径两者都 False，天然排除）；显式 `True` 恒不降级。 ⚠️ **本函数不是授权入口，也不自己做授权**：它收的是 **ID**，**不认身份**—— `nodes` 必须由调用方从**已认证的主体**算出来（`apps/rbac/permissions.py::nodes_of`）并如实传入，随后原样交给 `apply_transition`，由状态机再判一次边（纵深）。 **每个调用点都必须自己先过闸门**：`/claim` 在视图入口按节点 `grab_hall.take_order` 拒绝（HTTP 403），状态机这一层只是第二道，**不能**当作授权来依赖。
    ⚠️ `nodes` 是**必填 keyword-only**（Ruling 47），与 `apply_transition` 同形：漏传 = `TypeError`（签名层 fail-closed）。修之前这里写的是**常量** `{Role.TAKER}`（旧角色轴）⇒ 状态机的身份维度对本路径**恒真**，谁来调、替谁抢，边都会过 ——那正是本分支连续几个任务在消灭的 fail-open 形状。 ## ⭐ `enforce_limits`（**裁定 8**）：机器接手跳过**按人**的抢单闸门 `enforce_limits`（keyword-only，**默认 `True`**）= 「要不要过**按人**的那五道闸门」（`check_grab_limits`：没有接单价 / **未实名**（D-0245）/ 禁自助接单 / 接单价区间 / 持单上限）。 - **人类入口一律用默认值**（`/claim` 等视图）：那四道闸门对它们**一个字都不放宽**； - **机器自动接手**（下游鸡蛋铺线的 `poll_log`，spec A6：「对方打手接了 ⇒ 我方自动接手」是 **无条件**的；那时单已经在**对方**手里、外部事实已定）传 `False` ⇒ 只跳过这四道按人闸门。 - ⛔ **其余判据全部保留**：来源态守卫（不在大厅）、`rowcount == 0`（已被别人接走）、状态机的节点门、`apply_transition` 的复读比对与「已结账不许回池」。 ⛔ **不许**为机器路径另抄一份接手逻辑（钱路径只许一份实现）：本条开关是**唯一**的分叉点。 ## ⭐ `suppress_outbound_sync`（D-0227 / E-0236：**镜像接手**不回声）与 `apply_transition` 既有参数**同名同语义**（「本次写入由外部同步驱动 ⇒ 不许再对外回声」）， keyword-only、默认 `False` ⇒ **人类入口与①式快路行为一字不变**。`True` 时： - 透传给内部 `apply_transition`（盖掉 `queue_status_out` / 上家线钩子——既有那道闸）； - 尾上那句 `queue_claimed` 带同一开关 ⇒ 跳过接手人自己绑定的 `member.assign` / `member.unlist`（见 `jidanpu_member.queue_claimed`；「其他绑定排 `member.withdraw`」
    那一半**保留**——那是冗余副本清理，不是回声）。 ⚠️ **唯一传 `True` 的调用点**是②式的「因为对方接走而触发的自动接手」（`jidanpu_member.machine_claim_order(..., suppress_outbound_sync=True)`，由 `jidanpu_member_openapi._apply_member_status` 触发）：那次接手是**对方动作的镜像**，对方副本按定义不该被动（E-0236 现场：镜像接手连带把对方已接走的单推成已退单）。 ⚠️ 状态机判定发生在**绩效条件更新之后**（`apply_transition` 是唯一漏斗，不能绕过它提前判）：所以传了不含本条边所需节点的节点集时，调用方拿到的是「`IllegalTransition` + 那半截已写入的绩效行」。**事务边界在端点**（spec §7.4），端点必须**显式回滚**（见 `order_views.claim` 的 `transaction.set_rollback(True)`）。 ⚠️ **用 ORM 而不是裸 SQL**（与计划片段不同）： `QuerySet.update()` 生成的**就是**这一条 `UPDATE ... WHERE ...`，返回值是匹配行数（读码：`django/db/models/sql/compiler.py` 的 `SQLUpdateCompiler.execute_sql` 返回 `cursor.rowcount`），而表名（`_meta.db_table`）、列名、`EmpType.MAIN` 都由 Django 拼—— 手抄的那份一旦写错就是指到**另一张表**上去了（django-tenants 的租户隔离靠连接的 search_path，ORM 与裸 SQL 走的是同一条连接，但裸 SQL 少了 Django 的这层拼接）。 ⚠️ 条件更新里**必须显式**写 `updated_at`：`auto_now=True` 只挂在 `save()` 上， `QuerySet.update()` **不**会替调用方带上它（同一处读码：`as_sql` 只写调用方给的字段）。漏了它，这一行的时间戳会**停在创建时刻**——而「这行最近一次被改过是什么时候」就靠它（有专门的用例钉住，见 `test_claim.py::test_claim_refreshes_updated_at`）。 ⚠️ 这条口径**与「谁读这个时间戳」无关**，也与「别的写点带不带」无关： `QuerySet.update()`（只写调用方给的列）与 `bulk_update()`（读码： `django/db/models/query.py` 的 `bulk_update` 调的是 `_prepare_related_fields_for_save`、 **不走 `pre_save`**，再用调用方给的 `fields` 拼 `update()`，见该函数 `:891-894` / `:906-919` / `:924`） **都不会**替调用方带 `auto_now` ⇒ **每一次直写绩效行都得自己决定带不带 `updated_at`**。本行带上它，是因为**行确实被改过**；同类写点里带它的例如 `performance.py` 的重算（`save(update_fields=[…, "updated_at"])`）与 `settlement_hooks.py` 的结账钩子， **不带它的也存在**（例如 `refunds.py` 冲销时的 `.update(temp_amount=…, bonus_amount=…)`、 `settlement.py` 结账回填的 `bulk_update(settled_rows, ["settled_amount"])`）—— 上面只是**举例**，不是一份「例外清单」。 ⚠️ `WHERE` 里**必须**有 `emp_type = '打单工资'`——否则客服那条「销售提成」行也可能被"抢"（它的 `user` 可以是空的）。去掉后一条 UPDATE 会命中两行。"""
    # ⭐ D-0144（A4）：走默认 manager ⇒ **已软删的单取不到**（抛 `DoesNotExist`）⇒ 抢不到。
    #    视图层更早一步：`get_object()` 走同一个默认 queryset ⇒ 已删单先落 1002。
    order = Order.objects.get(pk=order_id)

    # ⭐⭐ **A4（计划 §4 第 1 条）：代练通优先**。「已有一条有效推送 **且** 远端已经有打手接手」⇒ 内部大厅抢单**直接拒绝**。 ⚠️ 判据本体在 `dailiantong_push.claim_blocked_by_platform_denial`（⛔ 不在本模块
    # 再写一份「什么叫已接手」）；函数内懒导入：`dailiantong_push` 会拉起审计 / 客户端，而 `claim` 是被 `jidanpu_member` 顶层 import 的热模块。 ⚠️ 顺序在**来源态守卫之上**：本地状态可能还停在「待抢单」（同步还没跑到），
    # 这时先给的文案必须是「代练通已被接单」而不是含糊的「已被别人接走」。 ⚠️ 只抛在「远端已接手」这一档；**只抛到代练通、还没人接**（10 临时 / 11 未接手 / 18 已下架）时内部照旧可以抢（抢成功之后由同步把代练通订单撤掉 —— Rev.9 §4 第 2 条）。 ⭐ H7：18 已下架并入「未接手族」（`REMOTE_STATUS_NOT_ACCEPTED`）。
    from apps.business.dailiantong_push import claim_blocked_by_platform_denial

    platform_denial = claim_blocked_by_platform_denial(order)
    if platform_denial is not None:
        raise ClaimError(platform_denial, ErrorCode.ORDER_ALREADY_TAKEN)

    if order.status != OrderStatus.WAITING_GRAB:
        # 「不在大厅」有两种成因，spec §5 的码表专门为后者留了独立的值，**不能**混成一个码： ① 那条 MAIN 行已经有人 ⇒ 就是「已被接走」（顺序双击、被指派、刚被抢走都算）
        # ② 行还空着 ⇒ 这单根本不在大厅（未分配 / 已暂停 / 待验收……） ⚠️ ① 用的**不是**第二套「谁接了」的定义，就是那条 MAIN 行的归属本身
        # （与 `search_hall` 的入选条件同源）；真正的**胜负**仍然只由下面的条件更新裁决， ① 只是把「已经有人」提前翻译成正确的码，免得双击的人看到「不在抢单大厅」。
        if _has_taker(order_id):
            raise ClaimError("该订单已被别人接走", ErrorCode.ORDER_ALREADY_TAKEN)
        raise ClaimError("该订单不在抢单大厅", ErrorCode.BUSINESS)

    # ⚠️ **按人的五道闸门**（NULL 价 / 未实名 D-0245 / 禁自助接单 / 价格区间 / 持单上限）：⛔ 默认一律要过；
    # 只有**机器自动接手**（裁定 8）显式传 `enforce_limits=False` 才跳过它们——
    # 那时外部事实已定（单已经在对方打手手里），被这几道按人闸门挡住会造出「我方没接手、对方在做」的错位 ⇒ 绩效 / 结算漏发。**其余判据一道都不跳。**
    if enforce_limits:
        check_grab_limits(user_id=user_id, order=order)
        # ⭐ 优选闸（D-0296）：个人打手接**优选单**的两档门槛（没完成过单 / 完成数异常多）。⚠️ 排在 `check_grab_limits` **之后**、⛔ 不动它的五支契约；
        # 与它同在 `enforce_limits` 分支里 ⇒ 机器接手（裁定 8）天然不进这道闸。 ⚠️ **函数内懒导入**：`hall_preferred` 顶层 import 本模块的 `ClaimError`。
        from apps.business import hall_preferred

        hall_preferred.check_preferred_claim(
            user=User.objects.get(pk=user_id), order=order
        )

    # ⭐ Z2：**统一锁序 = 先锁 Order、再锁钱包**。下面 `cancel_release_on_claim` / `ensure_deposit_for_claim` 会拿**钱包行锁**，而尾上的 `apply_transition` 会拿**订单行锁**（`Order.objects.select_for_update()`）
    # ⇒ 不先锁 Order 的话，本函数与「改派 / 退款钩子」（它们先锁 Order 再碰钱包）就是相反的锁序，并发时可以互锁到死（一方 `deadlock detected` ⇒ 500，终态虽然自洽）。所以这里**在第一次碰钱包之前**先把 Order 行锁住。⚠️ 位置必须在 `check_grab_limits` 之后（那是并发测试的栅栏同步点，挪到它前面会让所有抢单线程串行、把栅栏卡死）。
    # ⚠️ 事务边界在调用方（与函数里其它写入同形：`order_views.claim` / takerapi / `machine_claim_order` / `peer_inbound` 四处都在 `transaction.atomic()` 里）。 ⚠️ 这把锁**不改判据**：抢单胜负仍由下面那条条件更新裁决；它只回答「谁先谁后」。 ⚠️ 这是本函数**第二个** `Order.objects` 读点（第一个是开头的状态守卫）⇒ `tests/test_order_read_path_net.py` 的重数已登记为 2。
    Order.objects.select_for_update().filter(pk=order_id).first()

    # ── 解冻冷静期：接单 ⇒ 申请作废（业务方选项 A）────────────
    # 口径：冷静期内又接单的人**不是要离场**（他还要接着干活）⇒ 解冻申请作废、押金照旧冻着；到期（已满 7 天）的那笔则先结算回余额，再由下面的补冻从余额重新扣。 ⚠️ **必须排在押金补冻之前**：反过来会先看到「旧 frozen 够用」而跳过补冻、随后才结算 ⇒ 留下「有单、零冻结」的窟窿（`cancel_release_on_claim` 内部先结算就是这么设计的）。
    # ⚠️ 位置在按人闸门之后：被持单上限 / 价格区间拒掉的人**不算接单**，不该丢申请。 ⚠️ 事务边界在端点（与本函数其它写入同形）：抢单失败时端点回滚，作废也一并撤销。 ⚠️ **函数内 import**：`apps.accounts.wallet` 反向 import 本模块的 `_ACTIVE_STATUSES`，顶部互相 import 会成环。
    from apps.accounts.wallet import cancel_release_on_claim

    release_request_cancelled = cancel_release_on_claim(user_id)

    # ── 押金冻结（打手 App 线，D-0269）────────────────────
    # 口径：押金是**账号级**的（`wallet.frozen >= 该游戏押金` 即可接该游戏任意多单），不够时从 `balance` 补冻结差额；余额也不够 ⇒ 与抢单限制同码拒绝。 ⚠️ **不在 `enforce_limits` 分支内**：机器自动接手（`enforce_limits=False`）不涉及我方打手的钱包，那条路径必须**显式**传 `enforce_deposit=False`（见 `machine_claim_order`）。
    # 留在 `enforce_limits` 里会让「显式 True」被静默忽略。 ⚠️ **函数内 import**：`apps.accounts.wallet` 反向 import 本模块的 `_ACTIVE_STATUSES`，顶部互相 import 会成环。 ⚠️ `select_for_update` 要求调用方已在事务里（现有三个人类入口都满足： `order_views.claim` / `takerapi` 的 claim / 未来的 App 端点）。 ⚠️ 受 `settings.DEPOSIT_ENFORCED` 控制（默认关，逐租户打开）：这条闸会改变「谁能接单」，默认开会让存量打手在充值前突然接不了单——上线是显式动作。
    from django.conf import settings

    # `enforce_deposit` 由**调用方**给（None = 回落租户总闸）：App 令牌单独控制的判据在
    # `apps/business/deposit_gate.py::deposit_enforced_for`，⛔ 别在本函数里再认一次请求类型。
    deposit_gate = settings.DEPOSIT_ENFORCED if enforce_deposit is None else enforce_deposit
    # ⭐ 优选单强制押金（D-0296）：租户总闸关着也要冻。 ⚠️ **人类路径判据是 `enforce_limits`，⛔ 不是「`enforce_deposit is None`」** （H-1）：web/H5 大厅两条人类接单（`order_views.claim` 与内部镜像接单视图）传的是 `deposit_enforced_for(request)`——**具体的 bool**，租户总闸关时它是 `False` ⇒ 旧的「`is None` 才覆盖」条件在网页大厅（正是优选
    # toggle 所在的面）**永不命中**、优选强制押金整条失效。本函数里人类/机器的分界就是 `enforce_limits`：唯一的机器路径 `machine_claim_order` 传 `False` 且显式 `enforce_deposit=False`（`jidanpu_member.py`），人类入口一律默认 `True`。 ⚠️ 机器路径不覆盖：它显式传 `False` 的既有契约不动（押金段本就不受 `enforce_limits`
    # 控制——覆盖仍只发生在押金段内，本函数 docstring 那条口径不破坏）。 ⚠️ 非优选单仍按原逻辑回落租户总闸（语义一字不变）。 ⚠️ 判价用**抛单价**（`hall_preferred.is_preferred_order`），判据 `is not None`。 ⭐ 优选租户开关（D-0328）：开关关（N）⇒ 本覆盖整支跳过—— 高价单回归普通接单规则，押金只按租户总闸（开着时照拦，业务方知情）。 ⚠️ 这里**自带一次懒导入**：上一处（优选闸）在 `enforce_limits` 分支里，机器路径不会执行那个分支 ⇒ 名字绑定不可依赖，本处各自导入。
    from apps.business import hall_preferred

    if (
        enforce_limits
        and not deposit_gate
        and hall_preferred.preferred_enabled()
        and hall_preferred.is_preferred_order(order)
    ):
        deposit_gate = True
    if deposit_gate:
        from apps.accounts.wallet import WalletError, ensure_deposit_for_claim

        try:
            ensure_deposit_for_claim(
                User.objects.get(pk=user_id), order.game, ref_no=order.order_no
            )
        except WalletError as exc:
            raise ClaimError(str(exc), exc.code) from exc
    # ⚠️ **`claimed_at`（接单时刻）与 `updated_at` 取同一个 `now`**：两个时刻差几微秒没有业务含义，而「同一次接手写下的两个时间戳」应当一致。这一列是账号页「接单统计」的取数来源（窗口按 `claimed_at__date` 收窄）—— ⚠️ 它**只在「接手」时写**（本处与 `performance.set_taker`），改价 / 审核 / 结账都不动它；清除接单人时由 `reset_taker_performance` 与 `user` 一起清。
    # ⭐ **R-7（spec §4.5）——同一步复活被软删的 MAIN 行**：垃圾桶软删那条行时会把 `user` 清空（`reset_taker_performance`），于是它**满足**下面条件更新的谓词（`user_id__isnull=True`）⇒ 不复活的话，接手人会被写进一条仍是「已删除」的行，留下「行还是已删、却带着一个接单人」的隐形状态（读路径看不见它）。与 `/assign/` 的复活同口径（`order_views.py` 的 `perf.deleted_at is not None` 分支），
    # 差别只在**落点**：那条路是「先取出实例再 `save`」，这条路的原子性来自条件更新本身 ⇒ 复活必须挂进**同一次** `UPDATE`，不能另起一次写入（分两次会让「写了人」与「复活」之间出现一个可被别的请求观察到的中间态）。 ⚠️ 条件更新里的 `WHERE` **一个字不改**（尤其不能加 `deleted_at__isnull=True`：那会把「有人接了」说成「已被别人接走」，`rowcount` 变 0）。
    now = timezone.now()
    claimed = Performance.objects.filter(
        order_id=order_id,
        emp_type=EmpType.MAIN,
        user_id__isnull=True,
    ).update(user_id=user_id, claimed_at=now, updated_at=now, deleted_at=None, deleted_by="")

    # `claimed` 只可能是 0 或 1：约束② `uniq_order_main_taker` 保证一条订单最多一条 MAIN 行。 ⚠️ 若这条订单**一条 MAIN 行都没有**（孤儿订单），这里也会是 0，与「被别人抢走」不可区分——
    # 这正是 spec §7.4 把「录单与建绩效同生共死」定为 `/claim` 前置条件的原因。这里**刻意不加** `exists()` 兜底把它翻译成别的结果：不变量被破坏时该响亮，
    # 而伪装成「已被接走」只是把脏数据藏起来（Ruling 15 / E-0010）。
    if claimed != 1:
        raise ClaimError("该订单已被别人接走", ErrorCode.ORDER_ALREADY_TAKEN)

    # ⭐ 线 E：**接手 = 这张单第一次被发出去给打手**（业务方拍板选 A） ⇒ 写 `Order.published_at`（报酬页「发单时间」那一档筛的就是它）。 ⚠️ 实现收在**唯一一处** `apps/business/order_time_fields.py::mark_published`
    # （E-0021：`/assign` 的 `set_taker` 与这里写的是**同一个事实**，两处各写一遍必然漂移）；它内部用 `WHERE published_at IS NULL` 落实「只写第一次」（重抛 / 二次接手不覆盖）、
    # 并且**不清空**——两列都是筛选维度，与 `Order.completed_at` 离开已完成会清**有意不同**。 ⚠️ **传的就是上行那个 `now` 对象**：这两列都是「接手那一刻」，分两次取 `timezone.now()` 会差几微秒，对账时看着像两件事。
    order_time_fields.mark_published(order, moment=now)

    # ⚠️ **抢到之后按「这个人」重算预计绩效**（spec §6.1；§16.4 #17）。这里**不能**改成调 `set_taker`：条件更新（`UPDATE … WHERE user_id IS NULL`）是抢单
    # 原子性的来源，不能被「先读再写」替换。所以重算在本模块是**第二个**调用点—— `tests/test_temp_amount_recalc.py` 同时覆盖 `/assign` 与 `/claim` 两个入口，
    # 任一个的重算被去掉都会红（与 P3a 的 A4「副作用别散在调用点」是同一类风险的**已知代价**）。
    performance = recompute_temp_amount(
        Performance.objects.get(order_id=order_id, emp_type=EmpType.MAIN)
    )

    # ⚠️ 抢到之后改状态也走 `apply_transition`（**唯一的漏斗**）—— 这样将来给 IN_PROGRESS 加迁移副作用时，抢单这条路径不会漏。
    # `nodes` 是**调用方的**节点集，**不要**写回常量 `{grab_hall.take_order}`：那会让状态机的身份维度对本路径恒真（Ruling 47）。
    # 两笔写入的**事务边界在端点**上（spec §7.4 的那张表写的是 `/hall/{order_no}/claim`），与 `/assign` 等三个动作同形：视图用 `transaction.atomic()` 包住。
    apply_transition(
        order, OrderStatus.IN_PROGRESS, nodes=nodes,
        # ⭐ D-0227（E-0236）：镜像接手把「不回声」这一开关递进来（缺省 `False` ⇒ 既有
        #    调用点行为一字不变）；它只盖 `queue_status_out` / 上家线钩子（既有语义不扩不缩）。
        suppress_outbound_sync=suppress_outbound_sync,
        sync_personal_orders=sync_personal_orders,
        sync_peer_orders=sync_peer_orders,
        # ⭐⭐ A4（计划 Rev.9 §4 第 2 条）：**内部打手先接**是业务允许的动作（随后同步会把
        # 代练通订单撤掉）⇒ 显式放行「已抛代练通禁止手工改状态」那道闸。 ⚠️ 能走到这里的前提是上面那道「远端已接手 ⇒ 直接拒绝」没命中；
        # 这一句⛔ 不是绕过它，只是在「代练通还没人接」时允许本地先接。
        allow_status_change_while_pushed=True,
    )
    # ⭐ 下游鸡蛋铺线（spec §5.3 第 2、3 行）：抢单成功之后按「**谁**接了」分流——接单人**就是**某个本线绑定的主人 ⇒ 给他排 `member.assign`（对方只录单）； **别的**打手接了 ⇒ 给已推出去的绑定排 `member.withdraw`。
    # ⚠️ 位置在 `apply_transition` **之后**：它上面每一条 `ClaimError` / 状态机失败都走不到这里 ⇒ 不会留下「没抢到、事件却写了」。同事务由**调用方**（`order_views.claim` 的 `transaction.atomic()`）提供（本函数自己不开事务，见模块 docstring 的口径）。
    # ⚠️ **必须函数内懒导入**：`jidanpu_hall` 顶层 import 本模块，而 `jidanpu_member` 顶层 import `jidanpu_hall` ⇒ 在本模块顶层 import `jidanpu_member` 会成环（claim → jidanpu_member → jidanpu_hall → claim）。
    from apps.business import jidanpu_member

    if sync_personal_orders:
        jidanpu_member.queue_claimed(
            order, taker_id=user_id, suppress_outbound_sync=suppress_outbound_sync
        )
    if enforce_limits and settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        from apps.internal_chat.order_groups import assignment_changed
        from apps.internal_chat.order_grab_qr_notices import enqueue

        assignment_changed(order, performance.user, jidanpu_only=True)
        enqueue(order, user_id=user_id, claimed_at=now)
    # ⭐ D-0348：**接单触发**——单刚被接下，趁人还没去点开，异步拉一次「马上来」查码把群码备好（提前量；此时群多半还没建 ⇒ `not_found` 静默冷却，
    # 等打手/客服第一次点开详情再中）。⚠️ 触发模块自己带五道闸（未配置 ⇒ 零动作）； spawn 挂 `transaction.on_commit`，与上面的入队同生共死（回滚不 spawn）。
    # ⚠️ 函数内懒导入（与上面 `jidanpu_member` 同一条防环理由）。
    from apps.business import order_qr_fetch

    order_qr_fetch.maybe_enqueue_fetch(order)
    # ⭐ 接口可见性：把「这次接单作废了一笔解冻申请 / 结算了一笔到期解冻」的**区别**带回去给调用方（`order_views.claim` 响应里的 `release_request_cancelled`）。
    # ⚠️ 这是**内存属性**、不是模型列（⛔ 不落库：它是这一次请求的事件，不是订单状态；落库会与订单状态机抢同一个事实）。
    order.release_request_cancelled = release_request_cancelled
    return order


# ── 镜像单接单的限制校验输入面（spec §6.1）──────────────────────────────────────
# ⚠️ **本段追加在文件尾、且自带下面两个导入，是刻意的**：`tests/test_doc_line_references.py` 的 `_DECLARED_ANCHORS` 钉着本文件**上面**那三条行号锚点（来源态守卫 / 条件更新 /
# `apply_transition`）⇒ 在它们之前插入任何一行都会把那张门禁整体推红。值对象放在锚点之后，锚点一行不动。位置不影响使用：Python 的模块级名字在**调用时** 才解析，`check_grab_limits` 到那一刻一定能看到 `GrabCandidate`。
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class GrabCandidate:
    """抢单限制校验的**最小输入面**（spec §6.1 的实现要点）。 ⚠️ 存在的理由：镜像单**不是** `Order`（它没有订单行），而四支限制校验**只**依赖
    「接单价 + 订单类型」两件事 ⇒ 把输入面收敛成这个值对象，⛔ **不许把四支逻辑抄第二份**
    （同一事实两处实现必然漂移，E-0021 的形状）。"""

    hall_price: Decimal | None
    order_type: str

    @classmethod
    def from_order(cls, order) -> "GrabCandidate":
        return cls(hall_price=order.hall_price, order_type=order.order_type)
