"""订单检索的查询构造。供 API 与测试共用——不要在 view 里手写过滤器。"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db.models import (
    BooleanField,
    Case,
    Count,
    DecimalField,
    Exists,
    ExpressionWrapper,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, NullIf

from apps.accounts.models import FavoriteAssignee, StaffProfile
from apps.accounts.payloads import account_payload
from apps.accounts.selectors import list_accounts
from apps.business.game_scope import game_scope
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Order,
    OrderStatus,
    Performance,
    PlatformIntegration,
    SettleStatus,
)
from apps.business.order_flags import can_view_total_stats
from apps.business.source_groups import source_ids_in_group
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node_now
#: ⭐ D-0147 批 E 任务 3：`assignee-options` 的 `roles` 必须与 `GET /api/accounts/roles/`
#: **同源**（spec §4.6）⇒ 直接复用 `apps.rbac.selectors.role_names`（那正是
#: `apps/accounts/views.py::AccountViewSet.roles` 调的那一层）。⛔ 不在这里另写一份查询。
from apps.rbac.selectors import role_names

# 关键词模糊匹配的字段（对标物 `#key1` 的 **`title`**：「可以输入系统单号、淘宝单号、角色名、电话」）。 ⚠️ 必须覆盖**证据件承诺的全部字段**（原先这张表只按页面的 **`placeholder`**（「系统订单号/角色名/电话」）抄，**漏了 `title` 里的
# 「淘宝单号」** ⇒ 界面对用户的提示**早就承诺**能按来源单号搜，而后端一条都搜不出来 ——**静默**：不报错、就是返回空集（所以只有人真去搜才会发现）。 ⚠️ 证据件原文（`src/frontend/public/replica/order-orderlist.html` 的 `#key1`）：
# `placeholder="系统订单号/角色名/电话"` + `title="可以输入系统单号、淘宝单号、角色名、电话"`。 ⚠️ **三个搜索口共用本表**：`search_orders`（管理订单页）/ `search_hall`（抢单大厅）/ `search_recycle_bin`（订单回收站）⇒ 改这里三处一致。⛔ 不要为了「只让某一页生效」在这里加条件分支（那会让同一个「关键词」出现两套判据，必然漂移）。
KEYWORD_FIELDS = (
    "order_no",
    "source_order_no",
    "game_role_name",
    "customer_phone",
    "customer_qq",
)

#: `search_hall` 注解出来的列名：**生效接单价**。大厅的展示与价格筛选**同源**取它。
EFFECTIVE_TAKER_PRICE = "effective_taker_price"

#: 订单列表注解出来的列名：**接单人用户名**。`OrderListSerializer` 读它。
TAKER_NAME = "taker_name"

#: 订单列表注解出来的列名：**拆单组内成员总数**。
#: `OrderSplitListSerializer.split_members` 读它；取值口 = `split_members_expression()`。
SPLIT_MEMBERS = "split_members"

#: 订单列表注解出来的列名：**拆单组内已完成数**（D-0357 G16，给 v2 的「完成 X/N」）。
#: `OrderSplitListSerializer.split_completed_members` 读它；
#: 取值口 = `split_completed_members_expression()`。
SPLIT_COMPLETED_MEMBERS = "split_completed_members"

#: 订单列表注解出来的列名：**本行是否组内主单**（D-0357 G16；非拆单行恒 `False`）。
#: `OrderSplitListSerializer.split_is_main` 读它；取值口 = `split_is_main_expression()`。
SPLIT_IS_MAIN = "split_is_main"

#: 订单组接单价调平的锚点（PEER/LEGACY 均可；不替代旧 `split_is_main` 语义）。
SPLIT_IS_BALANCE_ANCHOR = "split_is_balance_anchor"

#: 订单列表注解出来的列名：**该单打手绩效（`emp_type=MAIN`）是否已结账**。
#: `OrderListSerializer.taker_settled` 读它；取值口 = `taker_settled_expression()`。
TAKER_SETTLED = "taker_settled"

#: 订单列表注解出来的列名：**该单接手人是不是代练通的占位账号**。
#: `OrderSplitListSerializer.taker_is_platform` 读它；取值口 = `taker_is_platform_expression()`。
TAKER_IS_PLATFORM = "taker_is_platform"

#: 订单列表**预取**出来的属性名：本单「代练通（platform=10）最近一条推送行」的列表（`Prefetch(to_attr=…)` 塞进来的，最多一条）。`OrderListSerializer.platform_push` 读它。
#: ⚠️ **必须走预取**：管理订单页一页 N 单，逐单查推送行就是 N+1（`tests/test_dailiantong_platform_data.py`
#: 有一条 SQL 条数不随行数增长的判据）。
LATEST_PLATFORM_PUSHES = "latest_platform_pushes"


def latest_platform_push_prefetch(
    *, platform: int = PlatformIntegration.Platform.DAILIANTONG
) -> Prefetch:
    """把本页订单的**代练通推送行**一次取回、按单挂到 `LATEST_PLATFORM_PUSHES` 属性上。口径（业务方）：取该单**代练通（platform=10）**按 `created_at` 倒序的 **第一条**；没有推送行 ⇒ 序列化器回 `null`。`-id` 是同 `created_at` 时的稳定次序
    （与 `DailiantongPush.Meta.ordering` 逐字同形）。 ⚠️ **一次查询取回整页**（`Prefetch` 的 queryset 不带单张单的条件）：调用方把它挂在列表 queryset 上即可，⛔ 不要在序列化器里逐行 `DailiantongPush.objects.filter(order=…)`。
    ⚠️ 预取的是**全部 `platform=10` 行**（含 `failed` 历史行）：本键要回「最近一条」，只预取有效状态会漏掉「最近一次发送失败」那一档。"""
    return Prefetch(
        "dailiantong_pushes",
        queryset=DailiantongPush.objects.filter(platform=platform).order_by(
            "-created_at", "-id"
        ),
        to_attr=LATEST_PLATFORM_PUSHES,
    )


def effective_taker_price_expression():
    """生效接单价 = 绩效上的覆盖价（若有）否则订单的默认价（spec §6.3）。 ⚠️ **两处必须同源**（Task 5 复审 M5 / Ruling 44）：大厅的 payload 与价格区间筛选都读这一个注解。只改 payload 会让「看到的价」与「筛出来的价」分叉；
    只改筛选则相反。两者都按 `Order.hall_price` 则会在覆盖价生效后**静默**不一致（代练员看到/筛到默认价，实际结算按覆盖价）。 ⚠️ 用**相关子查询**而不是 `Coalesce("performances__taker_price", "hall_price")`：
    后者依赖「`filter()` 建的那个 join 会被 `annotate()` 复用」这一未经本项目实测的行为，而 `performances` 是多值关系（客服的 SALES 行可以并存）——一旦复用的不是同一个 join，SALES 行会把结果放大。子查询自己带 `emp_type=MAIN` 条件，语义不依赖 join 复用。"""
    return Coalesce(
        Subquery(
            Performance.objects.filter(
                order=OuterRef("pk"), emp_type=EmpType.MAIN
            ).values("taker_price")[:1],
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
        "hall_price",
    )


def display_name_expression(*, prefix: str = "user"):
    """「怎么显示一个人」的 **SQL 注解侧唯一出处**：昵称优先、**空串**回落登录账号。 Python 侧的对应物是 `apps/accounts/display.py::user_display_name`（同一口径， ⛔ 两套不许各自漂移）。⚠️ 只把**空串**当没有（`NullIf(..., Value(""))`），
    ⛔ 不把空白串也算空。 `prefix` = 那条用户外键在当前查询里的路径前缀： - `Performance` 查询上是 `"user"`（⇒ `user__first_name` / `user__username`）； - `User` 自身的查询上是 `""`（⇒ `first_name` / `username`）。
    ⚠️ 本函数是给 SQL 注解用的（表达式必须留在查询里）。⛔ 不要在任何 view / serializer 里再抄一遍这个 `Coalesce(NullIf(...))`——加一处就多一份会各自漂移的实现（起 `settlement_views.SettlementPeopleView` 也调它）。"""
    user_path = f"{prefix}__" if prefix else ""
    return Coalesce(
        NullIf(f"{user_path}first_name", Value("")), f"{user_path}username"
    )


def taker_name_expression():
    """接单人显示名 = `emp_type=MAIN` 绩效行上的**昵称优先、空串回落登录账号**（C11）。口径 = `user.first_name or user.username`（与 `order_stats.py` 的 `userName` 同）： **只把空串当没有**，⛔ 不在这里发明「空白串也算空」的第二套口径。表达式的**唯一出处**是 `display_name_expression()`（本模块上方）。
    ⚠️ **别把 `accounts/payloads.py` 列进"同口径"**：那里是 `"display_name": user.first_name or ""`——**没有回落**（空昵称就回空串），因为它序列化的是账号接口上**可编辑的昵称字段本身**，不是"怎么显示一个人"。两条口径的差异在 `apps/accounts/display.py` 的模块 docstring 里写明（那一份才是
    Python 侧显示名的单一出处；本函数是 SQL 注解侧的对应物）。 ⚠️ **必须仍是「一条 SQL 语句」**（`tests/test_taker_name_nickname.py` 的查询数用例钉住）：在**同一个相关子查询**里 `annotate` 出 `name`，而不是两个子查询拼（两个子查询也是「一条 SQL 语句」，但每行多扫一次绩效表）。 ⚠️ 用**相关子查询**而不是 `performances__user__first_name` 的 join：`performances` 是多值关系（客服的 SALES 行可以并存）⇒ join 会把订单行放大成多行。理由与 `effective_taker_price_expression()` 完全相同。"""
    return Subquery(
        Performance.objects.filter(
            order=OuterRef("pk"), emp_type=EmpType.MAIN
        )
        .annotate(name=display_name_expression())
        .values("name")[:1]
    )


def taker_settled_expression():
    """该单**打手绩效行（`emp_type=MAIN`）是否已结账**（管理订单页「发单价」列的绿色「结」标记）。口径 = `Exists(MAIN 行 ∧ Performance.is_settled 三字段并集)`——**照抄模型属性** `models.Performance.is_settled` 的判据（`settle_status == 已结账` **或** `settled_at is not None` **或** `settled_amount is not None`）。⛔ 不在这里另立一套
    「只看 `settle_status`」的窄口径：存量里有「已结账但没写 `settled_at`」与「状态列被改回未结账却留着快照」的脏行，两套判据会得出相反的结论（该属性 docstring 已登记这条边界）。 ⚠️ **没有 MAIN 行 ⇒ `False`**（大厅行 / 未接单 / P2 遗留单）。语义是「这张单没有可结账的打手绩效」⇒「未结账」，与 `jidanpu_common._b1_status_rollback_blocked` 的「缺 MAIN 行
    ⇒ 没有已结账可言」同口径。⛔ 不返回 `null`：那会让前端多出「未知」一档，而这一列在业务上没有第三态。 ⚠️ 用 `Exists` 而不是 `Subquery(...values("settled")[:1])`：`Exists` 在 SQL 层恒回 `TRUE` / `FALSE`、**永不 NULL**，也不需要外层再判「取到没有」；每行仍然只是**一次** 相关子查询（不产生额外查询 ⇒ 无 N+1）。 ⚠️ 不加 `user__isnull=False`：结账本身要求有主（`settlement.settle_performances` 的第 2 步），但存量脏行（无主却带结账痕迹）按 `is_settled` 的口径**就是已结账**；这里要与那个属性说同一件事，⛔ 不靠「加固」把它变成第三套判据（E-0021 同族）。"""
    return Exists(
        Performance.objects.filter(order=OuterRef("pk"), emp_type=EmpType.MAIN).filter(
            Q(settle_status=SettleStatus.SETTLED)
            | Q(settled_at__isnull=False)
            | Q(settled_amount__isnull=False)
        )
    )


def taker_is_platform_expression():
    """该单的**接手人是不是代练通的占位账号**（订单列表「接手人」列转紫的判据）。口径 = `Exists(MAIN 行 ∧ user.username == 固定占位用户名)`： - **判 username（FK 派生）**，⛔ **不判显示名相等**——显示名是「代练通」/ 旧名
    「代练通打手」这种会随业务口径变、而且用户可编辑的串，拿它判身份必然漂移； - `Exists` 在 SQL 层恒回 `TRUE` / `FALSE`、**永不 NULL** ⇒ 没有 MAIN 行（大厅行 / 未接单）就是 `False`，前端拿到的是真布尔；
    - 每行仍只是**一次**相关子查询（批次与 `TAKER_SETTLED` 同一形状 ⇒ 不产生额外查询）。 ⚠️ 占位用户名从业务模块取（`dailiantong_push.VIRTUAL_TAKER_USERNAME` 是**唯一出处**），在函数内 import 以免 selectors 与 push 模块在加载期互相牵连。"""
    from apps.business.dailiantong_push import VIRTUAL_TAKER_USERNAME

    return Exists(
        Performance.objects.filter(
            order=OuterRef("pk"),
            emp_type=EmpType.MAIN,
            user__username=VIRTUAL_TAKER_USERNAME,
        )
    )


def search_orders(
    *,
    status=None,
    payment_status=None,
    customer_kind=None,
    game_id=None,
    server_id=None,
    project_text=None,
    keyword=None,
    is_urgent=None,
    tag=None,
    date_from=None,
    date_to=None,
    order_source=None,
    source_group=None,
    order_type=None,
    sales_user=None,
    kefu_type=None,
    is_leak=None,
    show_price_type=None,
    price_min=None,
    price_max=None,
    hall_price_min=None,
    hall_price_max=None,
    order_nos=None,
    batch_lookup_nos=None,
    taker_user=None,
    hall_publisher_user=None,
    split_id=None,
    include_hall_shadow=False,
):
    """按条件筛订单。所有参数都是可选的；不传就是不过滤。 ⭐ **`include_hall_shadow`（业务方拍板）**：镜像卡派生的**隐藏单** （`Order.hall_card` 非空）默认**不出现**在订单读面上——列表 / 头部统计 / 导出都从本函数取数，所以默认排除一处覆盖三面（⛔ 不在各视图里各写一遍）。详情按单号打开那一档由 `OrderViewSet.get_queryset()` 显式传 `True`（隐藏 ≠ 不存在，客服排障要看得到）。
    ⚠️ **本函数内部**只有这一处判据：`exclude(hall_card__isnull=False)`；⛔ 不要在它的调用方（列表 / 统计 / 导出）再叠第二份。⭐ 其它**不走本函数**的读面（大厅 / 回收站 / 待归属 / 对账候选 / 我的单 / 对外查单 / 铺底）各自有**一处**显式排除，由结构网 `tests/test_order_read_path_net.py::HallShadowExclusionNetTests` 逐面钉住。
    ⚠️ **签名共 28 个 keyword-only 参数**（逐个见签名本身；两档价格区间 `hall_price_min` / `hall_price_max` 供 `3 接发组合` 档；`split_id`=拆单组；`include_hall_shadow`=隐藏单； `source_group`=按「来源所属分组」筛（计划 §4 #1）；`customer_kind`=按存储的新老客标签筛）。 ⚠️ 这个数字**改列参数时要跟着改**（有断言钉着，漏改会让网红）。 ⚠️ `select_related("hall_publisher")` 是「发单人」列（`hall_publisher_name`， spec §5）的**取数前提**：那列读 FK 的显示名（昵称优先，`user_display_name`），不预取就是一页 N 行 N 条查询。 FK 是单值关系 ⇒ 这个 join 不会放大订单行。"""
    # ⭐ D-0144：底集走 `Order.objects`（自定义默认 manager）⇒ **已软删的单天然不在这里**
    #    （A1）。这一行**不改**，也不加 `deleted_at` 条件：默认 manager 已经带着它。
    qs = Order.objects.select_related(
        "game", "server", "category", "project", "hall_publisher"
    )
    if not include_hall_shadow:
        # ⭐（业务方拍板）：镜像卡派生的隐藏单**不进面向人的订单读面**。
        #    列表 / 头部统计 / 导出共用本函数 ⇒ 一处判据管三面（⛔ 不各写一份）。
        qs = qs.exclude(hall_card__isnull=False).exclude(
            upstream_hall_card__isnull=False
        )
    if status is not None:
        # ⭐ **多选实装**：`status` 是「已校验的枚举码**列表**」（`OrderSearchSerializer.validate_status` 的产物；单值 = 只有一个元素的列表）
        # ⇒ `status__in`。单值等价：`status__in=["IN_PROGRESS"]` ≡ `status="IN_PROGRESS"` （既有单值用例 `test_order_api` / `test_order_export_api` 一条不改也绿，就是判据）。
        # ⚠️ 判据写 `is not None` 而不是真值（E-0021；与下方 `payment_status` 同款口径）。
        qs = qs.filter(status__in=status)
    # ⚠️ 判据写 `is not None` 而不是真值：字符串枚举今天没有 falsy 合法值，
    #    但写 `is not None` 不依赖这个巧合（本项目边界铁律，E-0021）。
    if payment_status is not None:
        qs = qs.filter(payment_status=payment_status)
    if customer_kind is not None:
        # 按**存储的**标签筛（录单时快照），⛔ 不按当前历史重算。空串在
        # `order_search_params` 就被丢成「没传」⇒ 到这里一定是 new/old。
        qs = qs.filter(customer_kind=customer_kind)
    # ⚠️ 判据是 `is not None` 而不是真值：`0` 是**携带了这个条件**，不是「没传」。
    #    `float`/`Decimal` 侧的同类形状（`bool(Decimal("0.00")) is False`）在
    #    `search_hall` 的金额两端咬过一次 —— 见那里的注释与 `tests/test_selectors.py`。
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    if server_id is not None:
        qs = qs.filter(server_id=server_id)
    if project_text:
        qs = qs.filter(project_text__icontains=project_text)
    if is_urgent is not None:
        # 注意判据是 `is not None` 而不是真值：`?is_urgent=false` 必须能筛出「不加急」。
        qs = qs.filter(is_urgent=is_urgent)
    if tag:
        qs = qs.filter(tag__icontains=tag)
    if date_from:
        qs = qs.filter(created_at__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__lte=date_to)
    if keyword:
        condition = Q()
        for field in KEYWORD_FIELDS:
            condition |= Q(**{f"{field}__icontains": keyword})
        qs = qs.filter(condition)
    # ── ⭐ D-0144：六条筛选 + 销售客服三档 + 价格口径三档 ──────────────
    if order_source is not None:
        # 来源是字典 FK ⇒ 按 id 等值（`min_value=1` 已在序列化器挡掉 0/负数）。
        qs = qs.filter(order_source_id=order_source)
    if source_group is not None:
        qs = qs.filter(order_source_id__in=source_ids_in_group(source_group))
    if order_type:
        # 字符串枚举今天没有 falsy 合法值；沿用本文件既有口径写真值
        # （`status` 那一行同款），`order_type=` 空串在 `_search_params` 就被丢掉。
        qs = qs.filter(order_type=order_type)
    if is_leak is not None:
        # 语义逐字取对标 `lay-tips`（spec §5.4 实装 4）：
        # `Q(status=UNASSIGNED) | ~Exists(MAIN 行且 user 非空)`。
        # ⚠️ 用 `Exists` **子查询**（与 `effective_taker_price_expression()` 同形）：join 会把行放大。
        assigned = Performance.objects.filter(
            order=OuterRef("pk"), emp_type=EmpType.MAIN, user__isnull=False
        )
        condition = Q(status=OrderStatus.UNASSIGNED) | ~Exists(assigned)
        qs = qs.filter(condition) if is_leak else qs.exclude(condition)
    if sales_user is not None:
        # `kefu_type` 决定「拿哪个列去比 `sales_user` 这个值」（spec §5.4）。
        # ⚠️ **`kefu_type` 单独给、不给 `sales_user` ⇒ 不产生任何筛选**（下面这一整块
        #    挂在 `sales_user is not None` 里，正是这条的实现）——⛔ 不许静默按某个默认人筛。
        if kefu_type == "5":
            # ⚠️ **降级**（§3-D11 半可做 ②）：`created_by` 是**文本列**（录入时的用户名快照），
            #    把请求里的 id 解析成用户名再等值匹配 ⇒ 账号改名后按新名筛**筛不到旧单**。
            name = User.objects.filter(pk=sales_user).values_list("username", flat=True).first()
            qs = qs.filter(created_by=name) if name else qs.none()
        elif kefu_type == "10":
            name = User.objects.filter(pk=sales_user).values_list("username", flat=True).first()
            # ⚠️ `refunds` 是**多值关系** ⇒ 必须 `distinct()`，否则一单多条退款会把它放大成多行
            #    （与 `taker_user` 走 `Exists` 是同一个「不许放大行」的关注点，这里用不上子查询）。
            qs = qs.filter(refunds__operator=name).distinct() if name else qs.none()
        else:                      # "0"：销售客服（FK）
            qs = qs.filter(sales_user_id=sales_user)
    if show_price_type == "4":
        # ── ⭐ `4 搜亏本单`（算式由业务方当场裁定）──────────
        # 口径 = **`hall_price > total_price`**（**严格大于**：发单价高于接单价 ⇒ 差额为负）： - `hall_price == total_price` **不是**亏本（所以是 `gt` 不是 `gte`）； - `hall_price IS NULL` 的单**不算**（它没有发单价）——SQL 三值逻辑下 `NULL > x` 不是真，天然被排除，不需要额外的 `isnull=False`；
        # - 金额为 `0` / 负数**照常参与**：这里**不**加任何「必须为正」的条件。 ⛔ 这一档**不使用**金额区间（业务方明确选「不参与」）⇒ 序列化器对「档 4 + 任一区间参数」响亮拒绝（`code 1000`），到这里那 4 个值一定是 `None`；这里**也不**去读它们（读了就是把「静默丢条件」写进承重逻辑）。 ⚠️ 用 `F()` 走**同一个 WHERE**、不产生 join、不放大行（与 `hall_publisher_id` 那条同款）。
        qs = qs.filter(hall_price__gt=F("total_price"))
    elif show_price_type == "3":
        # ── ⭐ `3 接发组合`──────────────────────────────
        # 语义 = **同时**满足：接单价落在 `[price_min, price_max]` **且** 发单价落在 `[hall_price_min, hall_price_max]`。两对区间**各自单侧可缺**（只给一侧 ⇒ 只应用
        # 那一侧，与 `0/1/2` 档的单侧行为一字不变）。 ⚠️ 判据一律 `is not None`（E-0021）：`Decimal("0.00")` 是真值判据下会被当成「没传」的那个边界，而金额 0 在这里是**有意义**的边界（D-0015 之下负数同理）。 ⚠️ 四条条件都在**同一条 SQL** 上（无 N+1、无 join）。
        if price_min is not None:
            qs = qs.filter(total_price__gte=price_min)
        if price_max is not None:
            qs = qs.filter(total_price__lte=price_max)
        if hall_price_min is not None:
            qs = qs.filter(hall_price__gte=hall_price_min)
        if hall_price_max is not None:
            qs = qs.filter(hall_price__lte=hall_price_max)
    elif price_min is not None or price_max is not None:
        # 口径列映射**逐字**取 spec §5.4：`0`⇒接单价 / `1`⇒发单价 / `2`⇒累计退款额。 `show_price_type` 缺省（未传）时按 `"0"`——与序列化器的 `default="0"` 同一个口径
        # （照对标：那个控件的默认档就是「接单价格」）。 ⚠️ **本支一字未改**（`0/1/2` 三档行为与实装两档之前逐字相同）：`3`/`4` 已由上面两支
        # 接走，落到这里的只可能是 `0`/`1`/`2` 或缺省。
        column = {"0": "total_price", "1": "hall_price", "2": "refund_fee"}[
            show_price_type or "0"
        ]
        # ⚠️ 判据必须是 `is not None`：`bool(Decimal("0.00")) is False` ⇒ 写 `if price_min:`
        #    会把「传了 0」当成「没传」、**静默丢掉筛选条件**（E-0021）。
        if price_min is not None:
            qs = qs.filter(**{f"{column}__gte": price_min})
        if price_max is not None:
            qs = qs.filter(**{f"{column}__lte": price_max})
    if order_nos:
        tokens = order_nos.split(",")
        system_nos = [t for t in tokens if len(t) == 20 and t.isdigit()]
        source_nos = [t for t in tokens if not (len(t) == 20 and t.isdigit())]
        qs = qs.filter(Q(order_no__in=system_nos) | Q(source_order_no__in=source_nos))
    if batch_lookup_nos is not None:
        qs = qs.filter(Q(order_no__in=batch_lookup_nos) | Q(source_order_no__in=batch_lookup_nos))
    if taker_user is not None:
        # ⚠️ 用 `Exists` **相关子查询**而不是 `performances__...` join + `distinct()`： `Exists` 只作布尔条件参与 WHERE，**不产生 join、不放大行** ⇒「一单多绩效行只回一行」天然满足，且 `count` / 分页的 `total` 不会被 join 放大（§5.4 补②）。
        # ⛔ 判据 `is not None` 在这里是**口径一致**、不是承重逻辑（id 没有合法的 falsy 值）—— 真正的承重闸是 `IntegerField(min_value=1)`（spec §5.4 补③ / E-0021 的同族说明）。 📌 补充实测：本仓的 `uniq_order_main_taker` 本来就不允许一单两行 MAIN
        # （`Performance.Meta.constraints`，`fields=("order",)`）⇒「MAIN 维度上 join 会放大」在当前 schema 下不可能发生；选 `Exists` 仍是对的做法（不赌未来约束不放松），但别再把它说成「唯一的承重理由」。判据见 `tests/test_order_filter_options.py` 那两条。
        qs = qs.filter(
            Exists(
                Performance.objects.filter(
                    order=OuterRef("pk"), emp_type=EmpType.MAIN, user_id=taker_user
                )
            )
        )
    if hall_publisher_user is not None:
        # ⛔ **按用户 id 精确筛，不做名字文本匹配**（业务方裁定，D13）：
        #    对标送的是名字串（`operatorName`），我方送 id ⇒ 没有「账号改名后筛不到」的降级。
        qs = qs.filter(hall_publisher_id=hall_publisher_user)
    if split_id is not None:
        # ⭐ **拆单**：按**拆单组 ID** 取该组的全部成员（含显示层主单）。 ⚠️ 判据 `is not None`（不是真值）：空串在 `order_search_params` 就被丢成「没传」
        # ⇒ 到这里的非 None 值一定是非空串；写成真值判断会把「空串」这一档与「没传」混起来，而两者在这里语义相同、但那条依赖没必要写进承重逻辑。
        qs = qs.filter(split_id=split_id)
    return qs


def split_members_expression():
    """⭐ **拆单**：列表载荷 `split_members` 的 SQL 注解（组内成员总数）。 - **非拆单**（`split_id` 空串）⇒ `0`（**不是** `NULL`：载荷契约说这一格是 int）； - **拆单** ⇒ 同组的**未软删**订单行数（含主单自己）。 ⚠️ **为什么用 `Case` 兜住空串那一支**（承重，不是防御性写法）：`split_id=''` 的行占全表大多数，而 `filter(split_id=OuterRef("split_id"))` 对它们会去数**所有非拆单行** ⇒ 既算错（几万）又白扫一遍表。PostgreSQL 的 `CASE` 不评估用不到的分支
    （`docs/current/functions-conditional.html`：does not evaluate any subexpressions that are not needed）⇒ 空串那一档根本不会跑那个相关子查询。 ⚠️ 走**默认 manager**（`Order.objects`）⇒ 已软删的成员**不算**进这一格：回收站里的子单不该让列表上的主单显示成「还能展开」。这与「已软删单在常规读面上不存在」（D-0144）同一条口径。 ⚠️ **代价（如实登记：这是 spec §2.4 字面之外的一处取舍）**：口径既然是「**未软删**成员数」，
    那么**组内别的成员全被软删**时这一格会回到 `1` ⇒ 那片主单在列表上**既不画「拆」徽标、也没有展开开关**（页面判据是 `split_members > 1`），看起来就像它不是拆单的一员；而**详情页**仍能看到它自己的 `split_id`（详情走 `OrderSerializer`，不吃这个注解）。这一档是**刻意**的（软删 = 那一笔在常规读面上不存在；把一个只剩自己的组画成「可展开」会给出一个点了没有内容的开关），代价是**列表上丢掉了「它曾经被拆过」这条历史信息**—— 要保住那条信息得再加一个不吃软删过滤的计数（不在本线范围）。行为判据（软删一个成员 ⇒ 这一格 2 → 1，而组 ID 原样保留）： `tests/test_order_split.py::SplitCopyAndRecycleTests::test_soft_deleting_a_member_keeps_the_group_but_shrinks_the_count`。"""
    counts = (
        Order.objects.filter(split_id=OuterRef("split_id"))
        .values("split_id")
        .annotate(total=Count("id"))
        .values("total")
    )
    return Case(
        When(split_id="", then=Value(0)),
        default=Subquery(counts, output_field=IntegerField()),
        output_field=IntegerField(),
    )


def split_completed_members_expression():
    """⭐ **D-0357（G16）**：列表载荷 `split_completed_members` 的 SQL 注解（组内已完成数）。 - **非拆单**（`split_id` 空串）⇒ `0`（与 `split_members_expression` 同一口径：这一格是 int，不是 `NULL`）； - **拆单** ⇒ 同组**未软删**成员里 `status=COMPLETED` 的行数（含主单自己）。
    ⚠️ 与 `split_members_expression` **同形**（`Case` 兜空串 + 相关子查询计数， `CASE` 不评估用不到的分支 ⇒ 空串那一档不跑子查询）；走**默认 manager** ⇒ 已软删的成员不算（G9 删除时 `split_id` 已清，双保险同一口径）。 ⚠️ **`Coalesce` 是承重的**（与 `split_members` 的差别所在）：子查询带
    `status=COMPLETED` 条件，「组内一个完成的都没有」时 `COUNT ... GROUP BY` 返回**零行**（带 GROUP BY 的聚合对空集不回一行）⇒ 标量子查询得 `NULL` ——而这一格的契约是 int（非拆单恒 0、组存在时至少本单自己可能已完成）。给 v2 组卡「拆单组 · N 单 · 完成 X/N」的 `X`（`N` = 既有 `split_members`），组进度由前端现算，后端不加接口（spec §3.5）。"""
    counts = (
        Order.objects.filter(
            split_id=OuterRef("split_id"), status=OrderStatus.COMPLETED
        )
        .values("split_id")
        .annotate(total=Count("id"))
        .values("total")
    )
    return Case(
        When(split_id="", then=Value(0)),
        default=Coalesce(Subquery(counts, output_field=IntegerField()), Value(0)),
        output_field=IntegerField(),
    )


def split_is_main_expression():
    """⭐ **D-0357（G16）**：列表载荷 `split_is_main` 的 SQL 注解（本行是否组内主单）。 - **非拆单**（`split_id` 空串）⇒ **恒 `False`**； - **拆单** ⇒ 本行是不是组内**未软删**成员里 `id` 最小者——与行为侧判据
    `split_group.is_main_member` **同一口径**（那条用 `EXISTS` 判「有没有更小 id」，这里是 SQL 注解形态，⛔ 不在 Python 侧再判一遍）。 ⚠️ `ExpressionWrapper` 是**承重**的：`~Exists(...)` 裸放在 `Case` 的 `default`
    上没有 `output_field`，Django 会在编译期报错（不是防御性写法）。给 v2 组卡折叠行 = 主单行 + 徽章（`split_is_main` 为真那行当组代表）。"""
    inner = Order.objects.filter(
        split_id=OuterRef("split_id"), split_kind__in=("", "LEGACY"), id__lt=OuterRef("id")
    )
    return Case(
        When(split_id="", then=Value(False)),
        When(
            Q(split_kind="LEGACY") | Q(split_kind="", split_id__gt=""),
            then=ExpressionWrapper(~Exists(inner), output_field=BooleanField()),
        ),
        default=Value(False),
        output_field=BooleanField(),
    )


def split_is_balance_anchor_expression():
    """订单组中最早的活跃 PEER/LEGACY 成员；普通订单恒 False。"""
    earlier = Order.objects.filter(
        split_id=OuterRef("split_id"),
        split_kind__in=("", "LEGACY", "PEER"),
        id__lt=OuterRef("id"),
    )
    return Case(
        When(split_id="", then=Value(False)),
        When(
            split_kind__in=("", "LEGACY", "PEER"),
            then=ExpressionWrapper(~Exists(earlier), output_field=BooleanField()),
        ),
        default=Value(False),
        output_field=BooleanField(),
    )


def fold_split_members(queryset):
    """⭐ **拆单**：把「同一拆单组」折叠成**只留组内 `id` 最小者**。 **判据 = 在「当次筛选结果集」内做**（⛔ 不是拿全表最小 id 去比）：一条行被折掉 ⇔ 存在**另一条同样出现在本次结果集里**、`split_id` 相同、`id` 更小的行。 ⇒ 任何一行都不会**凭空消失**：主单被筛掉、子单命中时，那张子单自己站出来当那一组的代表
    （这正是业务方那句「拆出来的订单必须和原订单在一块」在**筛选**下的兜底）。 ⚠️ **`split_id` 为空串的行一律原样保留、行为零变化**（`Q(split_id="")` 那一支）：非拆单行既不是别人的「更小 id」，也不会被别人折掉。 ⚠️ **条件全在 SQL 里**（一条 `NOT EXISTS` 相关子查询）：折叠与 `order_list_summary` /
    分页 `count` **吃同一份 queryset** ⇒ 统计值与列表条数同源、一起变（⛔ 不许在视图里另起一套、也不许把子单算进 `total` 再在前端滤）。 ⚠️ 显式给 `?split_id=` 的**展开**请求不走这里（视图按参数分流，见 `OrderViewSet.filter_queryset`）：那一档要的就是**该组全部成员**。"""
    inner = queryset.filter(split_id=OuterRef("split_id"), id__lt=OuterRef("id"))
    return queryset.filter(Q(split_id="") | ~Exists(inner))


def penalty_list_visibility(exact_matches):
    visible_parent = exact_matches.filter(pk=OuterRef("penalty_approval__original_order_id"))
    return Q(split_id__gt="") | ~Exists(visible_parent)


def penalty_order_rows(rows, related_scope, visible_scope, *, exact_split_matches=None):
    """Annotate visible orders for the grouped list and its expansions."""
    linked_parent = visible_scope.filter(pk=OuterRef("penalty_approval__original_order_id")).order_by()
    counts = (
        related_scope.filter(penalty_approval__original_order_id=OuterRef("pk"))
        .order_by()
        .values("penalty_approval__original_order_id")
        .annotate(total=Count("pk", distinct=True))
        .values("total")
    )
    count_expression = Coalesce(Subquery(counts), 0)
    if exact_split_matches is not None:
        # 展开可显示未命中的组员，但这些成员的罚款单仍按主表筛选结果单列。
        count_expression = Case(
            When(Exists(exact_split_matches.filter(pk=OuterRef("pk"))), then=count_expression),
            default=Value(0),
        )
    return rows.annotate(
        is_penalty_child=Case(
            When(penalty_approval__original_order_id__isnull=False, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
        penalty_child_count=count_expression,
        penalty_parent_order_no=Subquery(linked_parent.values("order_no")[:1]),
    )


def penalty_children_for_parents(related_scope, visible_scope, parent_order_nos, *, exact_split_matches=None):
    parents = related_scope.filter(order_no__in=parent_order_nos).order_by().values("pk")
    rows = related_scope.filter(penalty_approval__original_order_id__in=parents)
    return penalty_order_rows(rows, related_scope, visible_scope, exact_split_matches=exact_split_matches)


def taker_options(*, viewer, game_id=None, limit=200, search=None):
    """候选**打手**名单（spec §5.6 / D13）。 **口径（我方自定，U14 ⇒ 登记为差异）**：在「**未软删**订单的 `emp_type=MAIN` 绩效行」上出现过的用户（`user_id` 非空、**去重**），按 `name` 升序、`id` 兜底（**确定性**排序，避免截断时结果抖动）。⛔ 不是「租户里全部账号」——那会把从来没接过单的人列进去，选了必然 0 行（E-0004 同族：让用户以为系统里没有这个人）。 ⚠️ `order__deleted_at__isnull=True` 是**必须**的（这是 spec §2.2.2-C27 登记的读点）： `Performance` 侧是跨关系 JOIN，**订单的默认 manager 对 JOIN 不生效**（spec §2.2.2 的机制段）。 ⚠️ **为什么不额外加 `Performance.deleted_at__isnull=True`**（D-0147）：本查询只取 `user__isnull=False` 的 MAIN 行。
    `Performance.deleted_at` 有两个写入者（`recycle.soft_delete_order` 与回收站垃圾桶端点），逐个看它们在这条查询上产不产生漏网行： - `recycle.soft_delete_order` 与订单列在同一个 `transaction.atomic()` 里同向写（删：`recycle.py:78-81`、绩效 `:82-83`；恢复：`:110-115`）⇒ 这条路径**单独** 不会留下「绩效已标删而订单未标删」的行； - 垃圾桶端点会造出「绩效已标删而订单未标删」这种形状，但它在软删 MAIN 行的**同一步**把 `user` 清空（`_on_pool_entry` 的七列回退） ⇒ 被它软删的 MAIN 行不满足 `user__isnull=False`。 ⇒ 就**这两个已登记的写入者**而言，本查询的结果集与「只取未软删行」一致（这是枚举，
    不是「任何来源都不会漏」的保证——将来出现第三个写入者要回来重判）。 ⚠️ `name` 口径与 `taker_name_expression()` **同一口径**（`first_name or username`，只把空串当没有）——⛔ 不在这里发明第二套。 ⚠️ **数据权限卡点在这里收**（`scope_queryset` + `game_scope("order__game_id")`—— `Performance` 侧按 `order__game_id` 选字段路径，与 `settlement.py` 的两处既有口径同源）：这样「候选名单里出现的人」与「列表里能筛到的人」在这道卡点上同源。 ⚠️ **列表独有的例外**：`order.restrict_to_self_created` 只由 `order_views.filter_queryset()` 施加在 `list` 上（`order_flags.scope_by_creator`），候选名单不过它 ⇒ 持该旗标的账号可能出现「候选里有这个人、按他筛列表却 0 行」。 ⛔ **不要**在视图里对名单逐人再查一次（那是 N+1）。返回 `(rows, truncated)`；`rows` 每项恰好 `{"id", "name"}`。 ⚠️ `viewer` 是**必填关键字参数**（⛔ 不给默认值）：漏传时响亮地 `TypeError`，而不是静默返回全租户名单（那会是一条越权面）。 ⚠️ `search` 非 None/非空 ⇒ 显示名 icase 包含过滤（2026-11-09 业务方拍板「下拉改远程关键词搜索」；None/空串时行为与无此参数逐字一致）。"""
    qs = Performance.objects.filter(
        emp_type=EmpType.MAIN,
        user__isnull=False,
        order__deleted_at__isnull=True,
    ).annotate(name=display_name_expression())
    # ⚠️ 数据权限必须先收（在 `game_id` 之前/之后都不改结果集，但两处都要有）。
    qs = scope_queryset(viewer, qs, scope_filter=game_scope("order__game_id"))
    if game_id is not None:
        qs = qs.filter(order__game_id=game_id)
    # ⚠️ `search` 是显示名 icase 包含（2026-11-09，业务方拍板「下拉改远程关键词搜索」）；
    #    None / 空串 = 没给 ⇒ 结果与改造前逐字一致（判据在 test_order_filter_options）。
    if search:
        qs = qs.filter(name__icontains=search)
    # ⚠️ **截断判定取 `limit + 1` 行**（不是先 `COUNT(*)` 再取 `limit` 行）：少一次查询，
    #    且两半都能判（`len > limit` ⇒ `True`；否则 `False`）。
    rows = list(
        qs.values("user_id", "name").distinct().order_by("name", "user_id")[: limit + 1]
    )
    truncated = len(rows) > limit
    return [{"id": row["user_id"], "name": row["name"]} for row in rows[:limit]], truncated


def publisher_options(*, viewer, game_id=None, limit=200, search=None):
    """候选**发单人**名单（spec §5.6 / D13）。 **口径（我方自定，U14 ⇒ 登记为差异）**：在「**未软删**订单的 `hall_publisher` 非空」上
    出现过的用户（**去重**）。同上：按 `name` 升序、`id` 兜底。 ⚠️ 底集用**默认 manager**（`Order.objects`）⇒ 已软删的订单**天然不贡献**候选人
    （A 组 / spec §2.2.2-A7）。 ⚠️ 数据权限同样在**这里**收（`scope_queryset(viewer, …, scope_filter=game_scope("game_id"))`）。 ⚠️ `viewer` 必填关键字参数（同 `taker_options`）。 ⚠️ `search` 非 None/非空 ⇒ 显示名 icase 包含过滤（与 `taker_options` 同一条口径、同一次拍板）。"""
    qs = Order.objects.filter(hall_publisher__isnull=False)
    qs = scope_queryset(viewer, qs, scope_filter=game_scope("game_id"))
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    ids = qs.values_list("hall_publisher_id", flat=True).distinct()
    users = (
        User.objects.filter(pk__in=ids)
        .annotate(name=display_name_expression(prefix=""))
        .order_by("name", "pk")
    )
    # `search` 与 `taker_options` 同一口径（显示名 icase 包含；None / 空串 = 没给）。
    if search:
        users = users.filter(name__icontains=search)
    rows = list(users[: limit + 1])
    truncated = len(rows) > limit
    return [{"id": user.pk, "name": user.name} for user in rows[:limit]], truncated


def kefu_candidates_qs(*, viewer):
    """「谁可以当这张单的销售客服」——候选集合的**唯一判据出处**（返回 `User` queryset）。 ## 为什么单独抽出来（业务方拍板「换销售客服 = 提成也换人」）这个口径今天有**两个消费方**： 1. `kefu_options()`（下面那个，喂订单页 / 录单页那一格的候选 radio）； 2. **换归属那个专用端点**（`OrderViewSet.sales_agent`，写 `Order.sales_user`
    + 搬 SALES 绩效行）——它必须**拒绝**一个不在候选里的人，而「不在候选里」的判据只能有**一份**（E-0021：同一事实两套判据，必然只用对了一套）。 ⛔ **不要**在写侧另写一套过滤条件（例如再抄一遍 `Q(groups__role__can_be_sales_agent=True) | Q(pk=viewer.pk)`）：那样「候选里选得到、提交却 1002 被拒」或反过来（选不到却收得下）两条都会**静默**成立。
    ⚠️ **只回 queryset、不回行**：两个消费方要的形状不同——`kefu_options` 要 `(id, name)` 两列的 `values()`（见它自己的注释：`.values()` 让 Django 跳过 prefetch），写侧要拿 `User` 实例去落 `sales_user` 外键。给 queryset 让它们各取所需， ⛔ 不要为了「少写一行」把行序列化在这里。 ⚠️ `viewer` 是**必填关键字参数**（⛔ 不给默认值）：漏传时响亮 `TypeError`，而不是静默丢掉「∪ 当前登录人」那一半（那会静默少一个候选）。"""
    return list_accounts().filter(
        Q(groups__role__can_be_sales_agent=True) | Q(pk=viewer.pk)
    )


def kefu_options(*, viewer, limit=200):
    """候选**销售客服**名单（管理订单页「销售客服」那一格 `#inputOrderKefu` 的候选源）。 **为什么要有它**（spec §1-A）：证据件里那一格是**空壳**（`<select …><option value="">选择或输入 </option></select>`），对标由运行期 JS 灌候选；本页此前**从来没填过** option ⇒ 下拉恒空。 **口径（业务方定，按角色标记）**：**持「可作为销售客服」标记（`apps/rbac/models.py::Role.can_be_sales_agent=True`）的职位下的**未软删**账号 **∪ 当前登录人**（去重）。⚠️ 它与同页 `takers` / `publishers` **不是同一个语义** （那两个是「**未软删订单里出现过的人**」）⇒ 视图的载荷用**不同的键**（`items`）， ⛔ 不要把两种语义并成一个名字（§5-C）。 ⚠️ **为什么从「写死两个角色名」改成按标记**：原口径是 `Q(groups__name__in=("客服", "客服主管"))`——**内置角色名被抄进了取数代码** ⇒ 租户自建的角色即使干的是客服的活也永远进不了候选，只能改代码。改成读角色上的标记之后，租户在「角色职位权限」页自己勾一下就纳入（存量那两个内置角色由数据迁移置 `True`，新建租户由 `apps/rbac/seed.py::seed_builtin_roles` 带上 ⇒ 名单与改动前逐条相同）。 ⚠️ **「未软删」这条口径不在这里重写**：底集直接调 `apps.accounts.selectors.list_accounts()`
    （它已带上「本租户 + 默认排除已软删」那一套），本函数只追加一个「**或是我自己**」的 OR ——那是 `list_accounts` 表达不了的并集（它的 `role=` 是**与**语义）。 ⚠️ **反查路径是 `groups__role__…`**（`Role.group` 是 **OneToOne**、`related_name="role"`）：没有同名 `Role` 的裸 `Group`（测试夹具与 `migrate_rbac_roles` 都可能造出来）**不带任何标记** ⇒ 它的人**不进**候选。这是**要的**（标记是唯一判据），但它是本函数最容易踩空的一处。 ⚠️ **两份消费者共用这一份名单（一处改、两处变，这是要的）**： ① 录单 / 编辑订单页那一格（`order-addorder.ts`，由本端点灌 radio）； ② **订单管理页的筛选那一格**（`order-orderlist.ts` 的 `fillKefuSelect`，起也走 `GET /api/business/orders/kefu-options/`）。两处调的是**同一个端点**（不是两份取数），所以本函数的口径一改，**列表筛选的候选跟着一起改**——`tests/test_order_kefu_options.py` 把这件事写在模块 docstring 里，但**前端那两处都调本端点**这半边今天**没有自动化判据**守着（本仓的 island 页测试不含这一条）⇒ 改前端取数时请人工核这两处。 ⭐ **起还有第三个消费方**（同一份判据、不是同一份名单）：**写侧**那个换销售客服
    归属的专用端点调上面抽出来的 `kefu_candidates_qs(viewer=…)` 来判「这个人在不在候选里」。 ⚠️ 它**不走本函数**：本函数有 `limit` 截断，拿截断后的名单当准入判据会把「候选里排在第 201 位的人」误判成「不在候选里」。 ⚠️ **两条今天如实存在的口径**（⛔ 不要去「修」它们）： ① 候选**不按 `is_active` 过滤**——`list_accounts()` 这次没传 `is_active` ⇒ **停用的客服仍在候选里**（本端点答的是「这个人是不是客服」，不是「他还上不上班」）； ② **已软删的账号即使持 `order.list` 登录，「∪ 当前登录人」那一半也不生效**—— 底集 `list_accounts()` 默认排除已软删的人，`Q(pk=viewer.pk)` 是在**那个底集之上**再 OR ⇒ 软删的登录人既进不了角色那一半、也进不了「我自己」那一半。口径自洽（候选里永远不出现已删账号），但没人写过 ⇒ 这里写明，免得后来者当成 bug 去改。 ⚠️ 显示名口径 = **昵称优先、空串回落登录账号**，表达式取 `display_name_expression(prefix="")`。它与 `account_payload()` 的 `payload["display_name"] or payload["username"]` **语义等值**（可达值域逐档实测相等：「有昵称给昵称、没昵称回落登录账号」——5 档实测相等； `payload["display_name"]` 自身是 `first_name or ""`、**不回落**，所以要显式 OR 那一半） ——两处是同一条口径的两种表达，⛔ 不许各自漂移。这里用 SQL 注解，是为了让「按 `name` 升序 + 取 `limit + 1` 行」在**一条查询**里完成（与 `publisher_options` 同形）。返回 `(rows, truncated)`；`rows` 每项**恰好** `{"id", "name"}`。 ⚠️ `viewer` 是**必填关键字参数**（⛔ 不给默认值）：漏传时响亮 `TypeError`，而不是静默丢掉「∪ 当前登录人」那一半（那会静默少一个候选）。 ⚠️ 截断判定取 `limit + 1` 行（与 `taker_options` 同形）：少一次 `COUNT`、两半都能判。 ⭐ 候选集合的判据抽到了上面的 `kefu_candidates_qs(viewer=…)` （同一份过滤条件现在也服务写侧那个专用端点）⇒ 本函数只负责「排序 + 截断 + 取两列」。 ⛔ 不要把过滤条件抄回这里：抄回来就是第二套判据（见那个函数的 docstring）。"""
    qs = kefu_candidates_qs(viewer=viewer)
    # ⚠️ `.values(...)` 让 Django **跳过** `list_accounts` 带的 prefetch（`query.py` 的
    #    `good_objects = False` 那一支：结果不是 Model 实例 ⇒ prefetch 无意义）——
    #    本端点只回 id / name，不需要 groups / games 那两个 prefetch 的查询。
    rows = list(
        qs.annotate(name=display_name_expression(prefix=""))
        .order_by("name", "pk")
        .values("pk", "name")[: limit + 1]
    )
    truncated = len(rows) > limit
    return (
        [{"id": row["pk"], "name": row["name"]} for row in rows[:limit]],
        truncated,
    )


def assignee_options(*, role=None, business_type=None, key=None, limit=200):
    """指派弹层「可被指派的候选人」名单（D-0146 方案②）。 **数据范围与 `GET /api/accounts/` 完全一致**（本函数存在的全部理由就是这一条）： ⛔ **不在这里重写任何一条过滤条件**——取数直接复用账号列表那条端点的同一层 `apps.accounts.selectors.list_accounts`（`AccountViewSet.list` 今天调的也是它，见 `apps/accounts/views.py`）。于是「同一租户 / 同样默认排除已软删的人 / 同一套 `role`(`groups__name__in`) + `business_type`(`staff_profile__business_type`) +
    `key`(`username`/`first_name`/`qq`/`mobile` 模糊) 口径」逐条同源。 ⚠️ 账号列表在**游戏范围**上没有隐式收窄（`?game=` 是显式参数、弹层不传）⇒ 本名单同样是全租户口径：⛔ 别在这里加 `scope_queryset`，那会把「谁能被指派」的面**收窄**成「只看得到自己那几款游戏的人」（与本端点换数据来源的意图相反，也会让两侧集合不再相等）。每行的 `name` / `role` 读 `apps.accounts.payloads.account_payload`——与账号列表同一个 `role`（名称升序首个 `Group`、无组为 `None`）与显示名口径（昵称优先、空串回落登录账号）。
    返回 `(rows, truncated, roles)`；`rows` 每项**恰好** `{"id","name","role","tag","remark"}` （弹层用这五个字段渲染候选行与 `lay-tips`；⚠️ 账号没有档案行时 `tag` / `remark` 是 **空串**、不是缺键，与弹层里 `a.tag ?? ''` 的兜底同向）。 ⚠️ 截断判定取 `limit + 1` 行（与 `taker_options` 同形）：少一次 `COUNT`，两半都能判。 ⭐ D-0147（批 E 任务 3）：第三项 `roles` = 本租户**全部角色名**（内置 + 自建），供指派弹窗的「用户职位」一组渲染。取值**直接委托** `apps.rbac.selectors.role_names` ——与 `GET /api/accounts/roles/` 是同一个函数（同源，不是"同一个口径的两份实现"）。"""
    qs = list_accounts(role=role, business_type=business_type, key=key)
    users = list(qs[: limit + 1])
    truncated = len(users) > limit
    return [_assignee_option(user) for user in users[:limit]], truncated, role_names()


def _assignee_option(user):
    payload = account_payload(user)
    return {
        "id": payload["id"],
        "name": payload["display_name"] or payload["username"],
        "role": payload["role"],
        "tag": payload.get("tag", ""),
        "remark": payload.get("remark", ""),
    }


def available_favorite_assignees():
    return list_accounts(is_active=True)


def favorite_assignees(operator):
    saved_ids = list(
        FavoriteAssignee.objects.filter(operator=operator).order_by("-pk").values_list("assignee_id", flat=True)
    )
    users = available_favorite_assignees().in_bulk(saved_ids)
    return [_assignee_option(users[pk]) for pk in saved_ids if pk in users]


def default_assignee_payload(operator):
    """返回操作人自己的默认指派。保存过的大厅选项在当前权限失效时隐藏。"""
    profile = (
        StaffProfile.objects.filter(user=operator).select_related("default_assignee").first()
    )
    # 「没有档案行」与「有档案但没设默认」在这里本就同义（都回 0 / ''），不另做区分。
    # FK 是 `SET_NULL` ⇒ 默认接手人被硬删时它自己就变回 `None`，不会指向空气。
    if profile is None:
        return {"defAssRecManUid": 0, "defAssRecMan": "", "defAssIsHall": False}
    if profile.default_throw_to_hall:
        return {
            "defAssRecManUid": 0,
            "defAssRecMan": "",
            "defAssIsHall": has_node_now(operator, "order.send_to_grab_hall"),
        }
    if profile.default_assignee is None:
        return {"defAssRecManUid": 0, "defAssRecMan": "", "defAssIsHall": False}
    user = profile.default_assignee
    return {
        "defAssRecManUid": user.pk,
        "defAssRecMan": user.first_name or user.get_username(),
        "defAssIsHall": False,
    }


def search_hall(
    *,
    game_id=None,
    server_id=None,
    project_text=None,
    price_min=None,
    price_max=None,
    keyword=None,
):
    """抢单大厅的查询：**已抛单、且还没人接**。 ⚠️ 两个条件各管一件事，**实测的归属**（判别力对照见 Task 5 报告）： - `performances__emp_type=EmpType.MAIN` 的内连接：**孤儿订单**（一条绩效行都没有）根本 join 不上，因此不入大厅——不是 `user__isnull` 挡住它的；
    - `performances__user__isnull=True`：有 MAIN 行但已挂接单人的单不入大厅。这是**正确性条件**而不是"筛选条件"——大厅里的单必须真的没人接，否则抢单会表现为 `rowcount == 0`，与「已被别人接走」无法区分（spec §7.4）。不需要 `.distinct()`：`uniq_order_main_taker`（部分唯一索引，条件只有
    `Q(emp_type=MAIN)`、**没有** `user IS NULL` 之类的逃逸口）保证一条订单最多一条打单工资绩效，所以这个反向 join 不会产生重复行。 ⚠️ **价格一律按「生效接单价」**（`effective_taker_price_expression()`， spec §6.3 / Ruling 44）：筛选与 payload 同源，见该函数的说明。"""
    # ⭐ D-0144：同 A1——`Order.objects` 已排除已软删的单（A2），这里不为它再加条件。
    qs = (
        Order.objects.filter(status=OrderStatus.WAITING_GRAB)
        # ⭐ ：隐藏单（镜像卡派生的载体）**不进面向人的抢单大厅**。
        #    生产上它没有 MAIN 行（下面那条 join 本来就接不住），这里是**显式**判据
        #    —— ⛔ 不靠「没有 MAIN 行」这种隐式形状（形状一变就会漏）。
        .exclude(hall_card__isnull=False)
        .exclude(upstream_hall_card__isnull=False)
        .filter(performances__emp_type=EmpType.MAIN, performances__user__isnull=True)
        # ⚠️ `hall_publisher` 同理：大厅载荷继承了「发单人」列（spec §5 的纯加法），
        # 不预取就是逐行查用户（`hall_publisher_name` 读 FK 的 `username`）。
        .select_related("game", "server", "category", "project", "hall_publisher")
        .annotate(**{EFFECTIVE_TAKER_PRICE: effective_taker_price_expression()})
    )
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    if server_id is not None:
        qs = qs.filter(server_id=server_id)
    if project_text:
        qs = qs.filter(project_text__icontains=project_text)
    # ⚠️ **判据必须是 `is not None`，不能用真值**（Ruling 39）：`bool(Decimal("0.00"))` 是 `False`，而 `_search_params` 只把**空串**当「没传」⇒ `?price_max=0.00`
    # 会**静默**不过滤、把 300.00 的单一起返回。D-0015 之下金额可以为负， `0` 是**有意义的边界**而不是「空」（实测红→绿对照见 `tests/test_hall_api.py::test_hall_price_max_zero_is_not_treated_as_missing`）。
    # ⚠️ 筛的是**生效价**而不是 `hall_price`（Ruling 44）：与 payload 同源。
    if price_min is not None:
        qs = qs.filter(**{f"{EFFECTIVE_TAKER_PRICE}__gte": price_min})
    if price_max is not None:
        qs = qs.filter(**{f"{EFFECTIVE_TAKER_PRICE}__lte": price_max})
    if keyword:
        condition = Q()
        for field in KEYWORD_FIELDS:
            condition |= Q(**{f"{field}__icontains": keyword})
        qs = qs.filter(condition)
    return qs


def search_recycle_bin(
    *,
    game_id=None,
    status=None,
    deleted_from=None,
    deleted_to=None,
    order_source=None,
    keyword=None,
    game_account=None,
):
    """回收站列表的查询（spec §5.2；7 个筛选参数）。 ⚠️ 底集是 `Order.all_objects.filter(deleted_at__isnull=False)` —— 默认 manager 排除已删行
    ⇒ 回收站取不到底集，这一处是 `all_objects` 的读路径（白名单口径见 spec §4.3-C4）。 ⚠️ 日期参数是 `deleted_from` / `deleted_to`（**自描述名**）：对标在这页复用了订单列表的
    `stime`/`etime` 参数名、语义却变成**删除时间**（D6-②）⇒ 我方不复用那个同名不同义的拼写。 ⚠️ 判据一律 `is not None`（`deleted_from` / `deleted_to` 用 `is not None`，其余字符串参数按本仓既有口径用真值——它们没有 falsy 合法值）。"""
    qs = Order.all_objects.filter(deleted_at__isnull=False).select_related(
        "game", "server", "category", "project", "hall_publisher"
    )
    # ⭐ 回收站是**一笔真退单**的清单 ⇒ 隐藏单（镜像卡载体，没有真实
    #    客户 / 退款额）不进。⚠️ 这里走 `all_objects`，默认 manager 的排除**不成立**
    #    ⇒ 必须显式排除（与列表 / 统计 / 导出同一条口径）。
    qs = qs.exclude(hall_card__isnull=False).exclude(
        upstream_hall_card__isnull=False
    )
    if game_id is not None:
        qs = qs.filter(game_id=game_id)
    if status is not None:
        qs = qs.filter(status=status)
    if deleted_from is not None:
        qs = qs.filter(deleted_at__gte=deleted_from)
    if deleted_to is not None:
        qs = qs.filter(deleted_at__lte=deleted_to)
    if order_source is not None:
        qs = qs.filter(order_source_id=order_source)
    if keyword:
        condition = Q()
        for field in KEYWORD_FIELDS:
            condition |= Q(**{f"{field}__icontains": keyword})
        qs = qs.filter(condition)
    if game_account:
        qs = qs.filter(game_account__icontains=game_account)
    # 显式排序：避免分页跨页重复（对标按删除时间倒序）。
    return qs.order_by("-deleted_at", "-id")


def order_list_summary(queryset, *, user, page_filter=None, related_scope=None):
    """计算筛选全集汇总及分页总数；罚款折叠时两个总数分别计算。

    `related_scope` 给出数据权限内可见订单；命中罚款父子单任意一边时，
    金额把同一父单下的罚款子单一起算（笔数/状态仍按命中单）；只展开一层罚款关系。
    """
    if not can_view_total_stats(user):
        return None, None

    zero = Decimal("0.00")
    refunded = Q(status=OrderStatus.REFUNDED)
    status_aggregates = {
        f"status_{state}": Count("pk", distinct=True, filter=Q(status=state))
        for state in OrderStatus.values
    }
    sums = {
        "incoming": Sum("total_price", default=zero),
        "outgoing": Sum("hall_price", default=zero),
        "refund_fees": Sum("refund_fee", default=zero),
        "refunded_incoming": Sum("total_price", filter=refunded, default=zero),
        "refunded_outgoing": Sum("hall_price", filter=refunded, default=zero),
    }
    # 退款操作人筛选使用 DISTINCT；只投影参与汇总的列，避免关联退款时重复累计。
    fields = ["pk", "status", "total_price", "hall_price", "refund_fee"]
    aggregates = {"order_count": Count("pk"), **status_aggregates, **sums}
    if page_filter is not None:
        queryset = queryset.annotate(
            in_list_page=Case(When(page_filter, then=True), default=False, output_field=BooleanField())
        )
        fields.append("in_list_page")
        aggregates["page_count"] = Count("pk", filter=Q(in_list_page=True))
    aggregate = queryset.order_by().values(*fields).aggregate(**aggregates)

    counts = {state: aggregate[f"status_{state}"] for state in OrderStatus.values}
    order_count = aggregate["order_count"]
    if sum(counts.values()) < order_count:
        # 保留可能存在的历史状态，仍按订单去重。
        legacy = (
            queryset.exclude(status__in=OrderStatus.values).order_by().values("status")
            .annotate(count=Count("pk", distinct=True))
        )
        counts.update((row["status"], row["count"]) for row in legacy)

    amounts = {name: aggregate[name] if aggregate[name] is not None else zero for name in sums}
    if related_scope is not None:
        matched = queryset.order_by().values("pk")
        parents = queryset.exclude(penalty_approval__isnull=True).order_by().values(
            "penalty_approval__original_order_id",
        )
        related = related_scope.filter(
            Q(penalty_approval__original_order_id__in=matched)
            | Q(penalty_approval__original_order_id__in=parents)
            | Q(pk__in=parents),
        ).exclude(pk__in=matched)
        related_amounts = related.order_by().aggregate(**sums)
        for name in sums:
            amounts[name] += related_amounts[name] if related_amounts[name] is not None else zero
    summary = {
        "count": order_count,
        "total_hall_price": f"{amounts['outgoing']:.2f}",
        "total_price": f"{amounts['incoming']:.2f}",
        "total_refund_fee": f"{amounts['refund_fees']:.2f}",
        "diff": f"{amounts['incoming'] - amounts['outgoing']:.2f}",
        "refunded_diff": f"{amounts['refunded_incoming'] - amounts['refunded_outgoing']:.2f}",
        "status_counts": counts,
    }
    return summary, aggregate.get("page_count", order_count)
