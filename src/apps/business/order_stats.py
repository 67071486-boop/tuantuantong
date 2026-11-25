"""接单统计的读取层（对标账号页 `.recTotalDiv`「接单统计」卡片 / `/UserAjax/GetRecOrderTotalList`）。口径（业务方的四条裁定 C1–C4 见本轮计划的「执行期修订 1」；逐条写明依据）： 1. **行集合 = 该租户全部「有效状态正常」的子账号**（`User.is_active=True`）—— 复用账号线列表的查询入口 `apps/accounts/selectors.list_accounts`（**不另写一套「谁是子账号」**）；窗口只改**数值**、**不改名单** ⇒ 没接单的人也在表里（`orderCount=0` / `lastRecOrderDate="无"`），禁用的人排除（与对标橙字那半句一致）。 2. **窗口** = `Performance.claimed_at` 的**本地日期**落在 `[stime, etime]`（**含两端**）。 ⚠️ `claimed_at` 是 P3e 新加的列 ⇒ **存量行没有这个时刻**（只能为空）⇒ 历史统计会偏少，如实登记为代价（见 `docs/DECISIONS.md` 的 D-0092）。 3. **完单率** = (窗口内接单里「**待验收 + 已完成**」的条数) ÷ 窗口内接单数，**百分数** 两位小数。依据：对标 `cols` 的 templet 原文「订单完成率(只计算待验收+已完成的)」且用 `res.complateRate + '%'` 拼接 ⇒ 值本身是百分数（不是 0~1 的比率）。状态映射 = `OrderStatus.PENDING_REVIEW`（label「待验收」）/ `OrderStatus.COMPLETED` （label「已完成」），**除这两个之外的 `OrderStatus` 取值一律不计**
（代练中 / 问题单 / 已暂停 / 已退单 / 池内两态——判据是上面那个二元组，不是这份名单：新增状态时名单会过期，判据不会）。 4. **还未结算** = Σ（窗口内 `settle_status=未结账` 的绩效行的 `settlement_amount.payable_net(行, 该行的未结账调整记录)`）——**复用既有结算读取层**，不另造第二套定义：算式来自 `settlement_amount.payable_net`，「哪些调整记录算未结账」来自 `settlement.unsettled_adjustments_prefetch()`（唯一构造处）。已结账的行**整行不计**：它的 `payable_net` 里仍带着已经付过的 `bonus_amount`，加进来就是把同一笔钱数两次。 5. **排序** = `orderCount` 降序 → `uid` 升序（对标**同分顺序不稳定**：每窗落在页外的 uid 不是同一批 ⇒ 我方加稳定 tie-break）；`idx` = **全量**行序 + 1（不是「本页行序」）。 6. **`userName`** = 对标物 `userName`（`apps/accounts/serializers.py` 的映射表把它对到 `display_name`），为空时回落**登录账号**（列标题原文是「姓名/名称」）。 7. **`gameName`**（可选，中文游戏名文本）= **按「这个人的游戏权限」筛行集合** （`StaffProfile.games` M2M，`apps/accounts/models.py`），**不是**把某些人的数字缩小到某个游戏：没有该款游戏权限的人**整行消失**，**剩下的人每个数值字段一个都不变**。依据是**对标实测**（同样的口径与 102→101 的数字逐字抄在**仓库内**的计划
`docs/plans/2026-09-22-p3e-self-service-prohibit-and-doc-debt.md` 的 Task 3 Interfaces；原始响应的调研台账**不在 git**、指针会烂，故这里只指仓库内那一份）： `gameName=原神` 时 `count` **102→101**（少一个人），而两边**共同 uid 的 `orderCount` 差异 = 0**（逐 uid 对比），且卡片那个下拉的 i18n 标签原文就叫「游戏权限」（`userlist.js` 的 `selectGameTotal:'游戏权限'`）。 ⚠️ **形态后果（如实）**：没有 `StaffProfile`（因而没有游戏权限）的账号在带 `gameName` 时**整行消失**——对标侧子账号建档强制「至少一款游戏权限」，所以我方这种账号在那边不存在对等物；不带 `gameName` 时他们照旧在表里。 - 游戏名在**本租户里没有「有效存在」**（不存在，**或存在但已被软删除**——D-0085 之后游戏可软删，见 `Game.deleted_at`）⇒ 抛 `OrderStatsError`（`1000` + 说真话的 msg， **刻意改掉**对标的「静默空表」）。判据 = `filter(name=…, deleted_at__isnull=True).exists()`：软删的游戏已从各页下拉消失，再把它当合法筛选项就会把挂着该权限的人照旧带出来（那是自相矛盾的成功）； - 游戏**有效存在**、但**没人有该权限** ⇒ **正常成功 + 空列表**（合法空结果，**不是**错误）。 8. **按「看统计的人」的游戏权限收窄被统计的行**：某个人的 `orderCount` / `noSettAmount` / `complateRate` / `lastRecOrderDate` 只统计 **查看者有权限的那些游戏**的行（`Performance → order__game_id`）。⚠️ **这与第 7 条的 `gameName` 是两件事**：第 7 条按**被统计的人**的游戏权限决定**表里出现哪些人**（一个请求参数），第 8 条按**看统计的人**的游戏权限决定**每一行数哪些单**（数据权限，与请求参数无关）。两套叠加时：先由 `gameName` 决定名单，再由第 8 条决定每一行的数字。没有权限的游戏的单**不进任何人的数字**（那人还在表里、`orderCount` 为 0）。 **严格口径**：查看者一款游戏都没有 ⇒ **整张表都不给**（`items` 空、`total` 0），而不是回一张「全 0 的同事名单」——依据是业务方 的裁定（「空游戏列表 ⇒ 什么都看不到」）。 ⚠️ 管理员不受影响（两处都过 `scope_queryset`，它的管理员短路判据只有一份）。 ⚠️ 本模块**只读**、不碰 request；查询只落在**当前连接所在的 schema** 上， **不接受任何请求参数传入的租户线索**（AGENTS.md §4）。"""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from apps.accounts.display import user_display_name
from apps.accounts.selectors import list_accounts
from apps.business.game_scope import game_scope, games_of
from apps.business.models import (
    Game,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.business.performance_amount import CENT, AmountError
from apps.business.settlement import unsettled_adjustments_prefetch
from apps.business.settlement_amount import payable_net
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset

#: 算作「完单」的两个状态 —— 判据是对标 templet 的「待验收+已完成」逐字映射到
#: `OrderStatus` 的 label（`PENDING_REVIEW`=待验收 / `COMPLETED`=已完成）。
COMPLETED_LIKE_STATUSES = (OrderStatus.PENDING_REVIEW, OrderStatus.COMPLETED)

#: 「窗口内没有接单」在 `lastRecOrderDate` 上的字面值（照对标：字符串「无」，**不是** `null`）。
NO_LAST_ORDER_DATE = "无"


class OrderStatsError(Exception):
    """接单统计的**预期**失败（请求参数指向不存在的东西 / 某一行金额算不出来）。
    形状与 `ClaimError` / `SettleError` 同构：`code` 直接进信封当业务码、`message` 当 `msg`。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


def _nothing_without_games(queryset, user):
    """严格口径的闸门：**一款游戏权限都没有 ⇒ 空表**，否则原样返回。 ⚠️ 这是 `scope_queryset` 的 `scope_filter`（交给卡点调用），不是可直接调用的判据：
    管理员由卡点自己短路（`apps/core/permissions.is_tenant_admin`），所以这里**不**再判一遍
    ——两处判据各写一份会漂移（见 `apps/business/game_scope.py` 的模块 docstring）。"""
    return queryset if games_of(user).exists() else queryset.none()


def _window_rows(*, stime, etime, viewer):
    """窗口内的绩效行，按归属人分组（`{user_id: [行, ...]}`）。 ⭐ **一次查询**取全部人的窗口行（不是逐账号查）——行集合可能有几百个账号，逐人查就是 N+1。未结账调整记录由共用的 `unsettled_adjustments_prefetch()` 预取
    （与结算列表同一份筛法，`payable_net` 只对给定序列求和）。 ⚠️ **不在这里按 `gameName` 过滤**：`gameName` 筛的是**人**（行集合），不是订单 ——见模块 docstring 第 7 条与 `order_stats_rows` 里的账号集过滤。
    按看统计的人的游戏权限收窄被统计的行 ——`Performance → order__game_id`。两者叠加时：先由 `gameName` 决定名单，再由查看者的权限决定每一行**数哪些单**。"""
    # ⭐ D-0144（C9）：窗口内**已软删单**的绩效行不计入任何人的金额（跨关系 JOIN 不经过
    #    `Order` 的默认 manager ⇒ 必须显式补，spec §4.3-C2）。
    # ⭐ R-6：**单条被软删**的绩效行同判（统计是金额读路径 ⇒ 被删的行不该再计入任何人）。
    qs = Performance.objects.filter(
        user_id__isnull=False,
        order__deleted_at__isnull=True,
        deleted_at__isnull=True,
    ).select_related("order")
    if stime == date.min or etime == date.max:
        # 保留日历极限筛选，避开偏移或取次日时的溢出。
        qs = qs.filter(claimed_at__date__gte=stime, claimed_at__date__lte=etime)
    else:
        # 按当前时区切天，让接单时刻的索引直接参与范围查询。
        start = timezone.make_aware(datetime.combine(stime, time.min))
        end = timezone.make_aware(datetime.combine(etime + timedelta(days=1), time.min))
        qs = qs.filter(claimed_at__gte=start, claimed_at__lt=end)
    # ⭐ ：隐藏单（镜像卡载体）不是任何人的接单业绩 ⇒ 不算进统计。 ⚠️ 写成 `exclude(...=False)`（⛔ 不是 `filter(...=True)`）：结构网
    # `tests/test_order_read_path_net.py::HallShadowExclusionNetTests` 同时校验「方法必须是 exclude、比较值必须是 False」——`filter(isnull=True)` 语义相同、
    # 但换个写法就把「必须排除」这条口径的形状漂成任意一种等价的布尔翻转， AST 网就再也分不出「排除」与「误写成不排除」。
    qs = qs.exclude(order__hall_card__isnull=False).exclude(
        order__upstream_hall_card__isnull=False
    )
    # 数据权限的**唯一卡点**（spec §6.1）：读侧的收窄规则在业务层
    # （`apps/business/game_scope.py`），这里只把 `scope_filter` 传进去。
    # ⚠️ 管理员由卡点短路 ⇒ 不受影响（DoD #2）。
    qs = scope_queryset(viewer, qs, scope_filter=game_scope("order__game_id"))
    # 长窗口的每行都要汇总，只取统计、算钱和报错用到的字段。
    qs = qs.only(
        "user_id", "claimed_at", "settle_status", "temp_amount", "bonus_amount",
        "deduct_mode", "deduct_value", "order__status", "order__order_no",
    )
    grouped = defaultdict(list)
    for performance in qs.prefetch_related(unsettled_adjustments_prefetch()):
        grouped[performance.user_id].append(performance)
    return grouped


def _unsettled_amount(user_rows):
    """窗口内**未结账**绩效行的应付净额之和（`Decimal`，两位小数）。 ⚠️ **已结账的行整行跳过**（见模块 docstring 第 4 条）。
    ⚠️ 某一行的 `payable_net` 算不出来（固定扣点 > 预计绩效等）⇒ 抛 `OrderStatsError`： **整表拒绝**而不是把那一行按 0 算（静默错账），也不是 HTML 500——与结算列表
    （`settlement_views.SettlementViewSet.list`）同一条口径，并补上「是哪一行」。"""
    total = Decimal("0.00")
    for performance in user_rows:
        if performance.settle_status != SettleStatus.UNSETTLED:
            continue
        try:
            total += payable_net(performance, performance.unsettled_adjustments)
        except AmountError as exc:
            raise OrderStatsError(
                f"{exc.message}（绩效 {performance.pk} / 订单 "
                f"{performance.order.order_no}）",
                exc.code,
            ) from exc
    return total.quantize(CENT)


def order_stats_rows(*, stime, etime, viewer, game_name=None):
    """返回**完整**（未分页）的统计行列表，顺序与 `idx` 都已算好。行的 8 个键是**逐字契约**（前端按它取值）：`idx` / `uid` / `userName` / `orderCount` / `noSettAmount` / `complateRate` / `createUserDate` / `lastRecOrderDate`。
    ⚠️ **`viewer` 是必填 keyword-only**（与 `settle_performances(..., user=)` / `revoke_settlements(..., user=)` 同一先例）：数据权限的主体**不许有默认值**。为什么不是「`None` ⇒ 空表」了事：`games_of(None)` 是空集 ⇒ `scope_queryset` 的
    `scope_filter` 会把行收成空 ⇒ 静默回空表。今天唯一的生产调用方总传 `viewer=request.user` ⇒ 无现网影响；但将来漏传会把统计**静默**清空（无异常、无日志、页面只是全 0）。做成必填 ⇒ 漏传在**调用点**就是 `TypeError`，用例见 `tests/test_order_stats.py::OrderStatsRowsSignatureTests`。"""
    if game_name is not None and not Game.objects.filter(
        name=game_name, deleted_at__isnull=True
    ).exists():
        # 刻意改掉对标的「静默返回空表」：这个名字在库里不存在是**请求的问题**，要说出来。
        raise OrderStatsError(f"游戏「{game_name}」不存在", ErrorCode.VALIDATION)

    accounts = list_accounts(is_active=True)
    # ⭐ 严格口径（业务方）：**没有游戏权限 ⇒ 什么都看不到**。
    #    只收窄数字会让这种账号看到一张「全 0 的同事名单」；整张表都不给才是「什么都看不到」。
    #    走同一个卡点 ⇒ 管理员的短路判据与 `_window_rows` 那处同源（不在这里另判一次）。
    accounts = scope_queryset(viewer, accounts, scope_filter=_nothing_without_games)
    if game_name is not None:
        # ⭐ `gameName` 筛的是**人**不是数字（模块 docstring 第 7 条）：
        # 按「这个人的游戏权限」把没有该款游戏的人**整行**去掉，剩下的人数值不变。
        # 游戏对象已在上面确认**有效存在**（未软删）⇒ 这里不会因 None 静默筛空。
        accounts = accounts.filter(staff_profile__games__name=game_name)

    grouped = _window_rows(stime=stime, etime=etime, viewer=viewer)

    rows = []
    for account in accounts:
        claims = grouped.get(account.pk, ())
        order_count = len(claims)
        completed = sum(
            1 for claim in claims if claim.order.status in COMPLETED_LIKE_STATUSES
        )
        # 0 条接单时不给 0/0 的除法：`0.0` 是「没有可完成的单」，不是 100%。
        complate_rate = (
            0.0 if order_count == 0 else round(completed * 100 / order_count, 2)
        )
        claim_times = [claim.claimed_at for claim in claims if claim.claimed_at]
        profile = getattr(account, "staff_profile", None)
        rows.append(
            {
                # 对标物 `userName` = 我们的 `display_name`；档案/昵称为空时回落登录账号（列标题「姓名/名称」）——`display_name` 的映射依据见
                # `apps/accounts/serializers.py` 的 `CREATE_REQUIRED_FIELDS` 注释。 ⚠️ 口径走共用 helper（唯一出处 `apps.accounts.display.user_display_name`），
                # ⛔ 不要在这里再写一遍 `first_name or get_username()`。
                "uid": account.pk,
                "userName": user_display_name(account),
                "orderCount": order_count,
                "noSettAmount": float(_unsettled_amount(claims)),
                "complateRate": complate_rate,
                # 显示日期与筛选窗口使用同一时区，避免跨午夜时错一天。
                "createUserDate": (
                    timezone.localtime(profile.created_at).date().isoformat()
                    if profile is not None
                    else ""
                ),
                "lastRecOrderDate": (
                    timezone.localtime(max(claim_times)).date().isoformat()
                    if claim_times
                    else NO_LAST_ORDER_DATE
                ),
            }
        )
    # 稳定排序：`orderCount` 降序 → `uid` 升序（对标同分顺序不稳定，见模块 docstring 第 5 条）。
    rows.sort(key=lambda row: (-row["orderCount"], row["uid"]))
    for index, row in enumerate(rows, start=1):
        # ⚠️ `idx` 是**全量**名次（不是「本页第几行」）：翻到第 2 页时第 2 名的 `idx` 仍是 2。
        row["idx"] = index
    return rows
