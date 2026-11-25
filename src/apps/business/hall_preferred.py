"""接单大厅的**优选单**：价格阈值、接单闸、读侧行标记与归并（D-0296）。 ## 三件事（同一模块收拢，⛔ 不散在调用点） 0. **租户开关**（D-0328，业务方「把优选关掉」）：`preferred_enabled()` 读 `hall_preferred_orders`（默认 `"Y"` = 存量行为一字不变）。关（N）⇒ 读口所有行 `is_preferred` 恒 False（读口在 `order_views` 大厅视图整批抹）、接单侧跳过本模块的完成数闸与三条 claim 链的「优选强制押金」——高价单回归普通接单规则（普通押金闸
照旧、模拟单生成不受影响）。挂闸位置见各函数 docstring，⛔ 不另设第二套判据。 1. **判价**：`is_preferred_price` / `is_preferred_order` / `is_preferred_mirror_row` ——「优选」只由**原始展示价**回答（原生 = 抛单价 `Order.hall_price`；镜像 = 勾兑展示价 `upstream_hall_price`；模拟单恒 True）。⚠️ 判据打在**脱敏前** 的原始值上：脱敏是**读面**对**受限用户**的事，价还是那个价（`hall_price` 被遮成
`None` 的行对不受限用户仍是优选）。 2. **接单闸**（T4）：`check_preferred_claim` —— 个人打手接优选单的两档门槛（没完成过单不许接；完成数异常多按「可能已被接手」处理）。⚠️ **只接在 claim 链**： `/assign` 指派路径不受限（业务方口径），机器接手（`enforce_limits=False`）同样不进。 3. **读面**：`merge_hall_rows_with_simulated` / `merge_exited_rows` —— 内部大厅可接区（原生 ∪ 镜像 ∪ 模拟单）与灰区（快照 ∪ retired 模拟单）的归并。 ⚠️ **新函数**：`jidanpu_upstream.merge_hall_rows_two` 原样不动（有既有网钉着它）。"""
from decimal import Decimal

from apps.business.models import EmpType, OrderStatus, Performance
from apps.business.roles import WORKER

#: 优选单价阈值：大厅展示价 **严格大于** 它才算优选（`> 200`，`200.00` 本身不是优选）。
PREFERRED_PRICE_THRESHOLD = Decimal("200")

#: ⭐ 优选的**租户开关**（D-0328，业务方「把优选关掉」）：注册表唯一出处是
#: `system_switches.py`（默认 `"Y"` = 存量行为一字不变）。key 在两个模块各持一份字面量
#: 会漂移 ⇒ 本模块从 `system_switches` 的 `SWITCHES` 注册表引（同源）。
PREFERRED_SWITCH_KEY = "hall_preferred_orders"


def preferred_enabled() -> bool:
    """这个租户现在开不开「优选订单」（`system_switches.switch_enabled`，默认开）。 ⚠️ 每次调用查一次当前连接（租户 schema）⇒ **调用点必须自己控制频次**：
    读口的四路行装配在**视图层**一次读开关、整批 strip（`order_views` 大厅读口）， ⛔ 不要把它塞进 `is_preferred_price` 这类**逐行**纯判据里——那会让大厅页每一行
    多一条开关查询（N+1）；接单链每次调用天然只走一遍（一次抢单一次查询），在 `check_preferred_claim` 与三处「优选强制押金」覆盖点上判是合算的。"""
    from apps.business.system_switches import switch_enabled

    return switch_enabled(PREFERRED_SWITCH_KEY)

#: 个人打手接优选单的**完成单数下界**：一次都没完成过 ⇒ 拒（业务方）。
PREFERRED_MIN_COMPLETED = 0
#: ⛔ **业务方拍板：取消上限**。原规则「完成单数 > 10 ⇒ 拒（此订单可能已被接手）」
#: 已删除——口径改为「**只要完成过订单就能接优选单**」。
#: 留这个名字只是为了让旧引用/旧文档不至于不明所以；**不要再拿它做闸**（现恒不触发）。
PREFERRED_MAX_COMPLETED = None


def is_preferred_price(price) -> bool:
    """一个**原始**展示价是不是优选价。判据 `is not None`（金额边界铁律，E-0021）。"""
    return price is not None and price > PREFERRED_PRICE_THRESHOLD


def is_preferred_order(order) -> bool:
    """原生单的优选判定：**抛单价**（`Order.hall_price`，可空）大于阈值。
    ⚠️ 不是「生效接单价」：覆盖价（`Performance.taker_price`）是结算口径，
    「这张单优不优选」看的是抛单时的定价。"""
    return is_preferred_price(order.hall_price)


def completed_count_for(user_id: int) -> int:
    """这个打手**已完成**的订单数（MAIN 绩效行视角）。四条判据（缺一不可）： - MAIN 绩效行（`emp_type=MAIN`）挂在**这个人**名下；
    - 订单状态 = 已完成； - 订单**未软删**（D-0144：已删单上的 MAIN 行不算持有量，同款口径）；
    - 绩效行**未软删**（垃圾桶删掉的行不算数）。"""
    return Performance.objects.filter(
        user_id=user_id, emp_type=EmpType.MAIN,
        order__status=str(OrderStatus.COMPLETED),
        order__deleted_at__isnull=True,
        deleted_at__isnull=True,
    ).count()


def is_personal_booster(user) -> bool:
    """「个人打手」判定（优选闸**只**拦这一档；工作室 / 管理员豁免）。三条同时成立才是个人打手： 1. 持「代练员」Group（Group 判定的形状照 `apps/realname/services.py` 的实名闸先例）；
    2. `StaffProfile.is_studio` **非 True**（工作室账号豁免；没有档案 ≠ 工作室）； 3. 非租户管理员（判据**收口**在 `apps.core.permissions.is_tenant_admin`，⛔ 不另写）。"""
    from apps.core.permissions import is_tenant_admin

    if user is None or getattr(user, "pk", None) is None:
        return False
    if not user.groups.filter(name=WORKER).exists():
        return False
    if is_tenant_admin(user):
        return False
    from apps.accounts.models import StaffProfile

    profile = StaffProfile.objects.filter(user=user).first()
    return profile is None or profile.is_studio is not True


def check_preferred_claim(*, user, order=None, mirror=None, price=None) -> None:
    """接**优选单**的前置闸（`ClaimError` + `1013`，与抢单限制共用同一个码）。 - 目标**非优选** ⇒ 放行（原生按抛单价、镜像按勾兑展示价； ⭐ D-0303 的团团通镜像行**没有自己的镜像模型闸**——它已把展示价算好，直接传 `price=`，本函数照同一个阈值判，⛔ 调用方不许在外面另写一份阈值比较）； - ⭐ **租户开关关（D-0328，`hall_preferred_orders` = N）⇒ 整道闸跳过**（三条 claim
    链的调用点——原生 / 鸡蛋铺镜像 / 团团通镜像——都经本函数，闸在函数口收一次就全覆盖；高价单回归普通接单规则，业务方 知情）； - 调用方**不是个人打手**（工作室 / 管理员 / 非代练员）⇒ 放行； - 完成单数 `== 0` ⇒ 拒：`优选订单需要先完成过订单才能接：你还没有已完成的订单`； - ⭐ **2026-10-28 业务方拍板**：**取消**原来「完成单数 > 10 ⇒ 拒」的上限
    ⇒ 现在是「**完成过 1 单起就能接**」。⛔ 不要再把它加回来。 ⚠️ 本函数**不重复**「抢单限制五支」的任何判据（那是 `check_grab_limits` 的契约，一支都不动）；调用方把它排在 `check_grab_limits` **之后**。 ⚠️ `order` / `mirror` / `price` 至多给一个（原生单一条路、鸡蛋铺镜像一条路、团团通镜像一条路）；都不给 ⇒ 放行。"""
    from apps.business.claim import ClaimError
    from apps.core.error_codes import ErrorCode

    if not preferred_enabled():
        return
    if order is not None:
        preferred = is_preferred_order(order)
    elif mirror is not None:
        from apps.business import jidanpu_upstream

        preferred = is_preferred_price(
            jidanpu_upstream.upstream_mirror_hall_price(mirror)
        )
    elif price is not None:
        preferred = is_preferred_price(price)
    else:
        preferred = False
    if not preferred:
        return
    if not is_personal_booster(user):
        return
    count = completed_count_for(user.pk)
    if count == PREFERRED_MIN_COMPLETED:
        raise ClaimError(
            "优选订单需要先完成过订单才能接：你还没有已完成的订单",
            ErrorCode.GRAB_LIMIT_EXCEEDED,
        )


# ── 读面：可接区 / 灰区的归并（⛔ 不动 jidanpu_upstream.merge_hall_rows_two）────


def merge_hall_rows_with_simulated(native, upstream, simulated) -> list[dict]:
    """可接区归并：原生 ∪ 镜像 ∪ **模拟单**，按进厅时刻倒序。 ⛔ **已被 `peer_hall.merge_hall_rows_peer` 取代、生产代码不再调用**（D-0303 起
    内部大厅读口归并为四路；本函数仅存给它的三条既有用例当形状对照）—— 删它必须同一次迁走那批用例（登记在案）。三条**各自已按各自时刻倒序**的流（原生 = `created_at`、镜像 = `first_seen_at`、
    模拟单 = `generated_at`——三者同义：「进厅时刻」）。排序键与并列次序照 `merge_hall_rows_two` 的同一条性质：**相同时间戳 ⇒ 原生 → 镜像 → 模拟单** （写进排序键自己的性质，⛔ 不依赖构造顺序 + 稳定排序这两个间接前提）。"""
    items = [(moment, 0, payload) for moment, payload in native]
    items.extend((moment, 1, payload) for moment, payload in upstream)
    items.extend((moment, 2, payload) for moment, payload in simulated)
    items.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [payload for _moment, _rank, payload in items]


def merge_exited_rows(snapshots, simulated):
    """灰区归并：快照 ∪ retired 模拟单，按**离厅时刻**倒序（快照 → 模拟单 并列优先）。"""
    items = [(moment, 0, payload) for moment, payload in snapshots]
    items.extend((moment, 1, payload) for moment, payload in simulated)
    items.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [payload for _moment, _rank, payload in items]


def simulated_row(instance, *, exited: bool) -> dict:
    """模拟单 → 大厅行（在 `payload` 之上重申**本次读面**的四个键）。 payload 是生成那一刻的快照，`is_exited` / `exit_kind` / `retired` 与否是**现在**
    的事态 ⇒ 读侧重申，不信旧载荷。`is_preferred` 恒 `True`（业务方口径：演示卡
    永远亮着优选标）。"""
    from apps.business.models import HallSimulatedOrder

    row = dict(instance.payload) if isinstance(instance.payload, dict) else {}
    row["sim_pk"] = instance.pk
    row["origin"] = "simulated"
    row["is_preferred"] = True
    row["is_exited"] = exited
    row["exit_kind"] = None
    if exited and instance.status != HallSimulatedOrder.Status.RETIRED:
        # 灰行必须真已下架（⛔ 不给「还在厅」的卡打灰标——那是说假话）。
        raise ValueError(f"模拟单 {instance.pk} 未下架却按灰行读出")
    return row


def snapshot_row(snapshot, *, can_view_price: bool) -> dict:
    """离厅快照 → 灰区行（**按请求者脱敏** + 重申灰标的三个键）。 - `is_preferred` 按 **payload 里的价**算（`payload["hall_price"]` 是写点覆写过的
    「原始在厅价」，见 `hall_snapshot.native_payload` / `mirror_payload`）；价缺失（`None`）⇒ 非优选——`is not None` 判据与价闸同一条铁律。
    - `can_view_price=False`（`order.restrict_view_offer_price` 被施加）⇒ `hall_price` 读成 `None`（键仍在）——灰行同样吃读面的价格脱敏，脱敏 **不改变** `is_preferred` 的判定（判的是写点存下的原始价）。"""
    row = dict(snapshot.payload) if isinstance(snapshot.payload, dict) else {}
    price = row.get("hall_price")
    if not can_view_price:
        row["hall_price"] = None
    if price is not None and not isinstance(price, Decimal):
        try:
            price = Decimal(str(price))
        except Exception:  # noqa: BLE001 —— 脏 payload 按非优选处理，不打挂整页
            price = None
    row["is_preferred"] = is_preferred_price(price)
    row["is_exited"] = True
    row["exit_kind"] = snapshot.exit_kind
    return row
