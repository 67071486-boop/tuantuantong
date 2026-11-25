"""代练员视角的「我接的单」：`GET /api/business/my-orders`（spec §4.4 / §5）与打手受限详情 `GET /api/business/my-orders/<order_no>/detail/` （booster-h5 spec `2026-10-10-booster-h5-m1-design.md` §6.2，2026-10-10 新增）。 **为什么单独一个模块**（控制方裁定 R3）：Part 1 的分解把这个端点归到 `api/performance_views.py`，但那是 B1-5/B1-6 的**在飞落点**，本分支不动它的既有行。偏离已登记进 `docs/plans/2026-09-20-p3b-performance-review.md` 的 Part 2。 **它是「绩效 = 订单 × 人」的**代练员侧**读法**（D-0011）：同一条 `Performance` 行，客服/客服主管从 `/performances` 看（全租户），代练员从 `/my-orders` 看（只有自己的）。所以本视图**不是** `PerformanceViewSet` 的一个筛选分支——两者的**角色闸门相反**，
数据范围也相反（一个是全量 + 筛选，一个是 `user == request.user` 的窄读）。三条口径： - **谁**：持有 `my_orders.send_to_my_system` 的人（对标「我的订单」入口的节点）——超管 / 持「系统管理员」角色的账号经 `nodes_of` 拿全目录 ⇒ 放行。 **不再按职位**（2026-09-23 单轴化删掉了原先那道 `Role.TAKER` 职位闸门）； - **哪几行**：`Performance` 里 `user == request.user` **且** `emp_type == 打单工资`。 ⚠️ 前半句是 spec §4.4 说的「会在 view 内再叠加 `user == request.user`」——那是**功能语义**（这个端点的定义就是"我的"），**不是**数据权限。 ⚠️ 后半句**不是**冗余，它过滤的是**接单人身份**（"我**接**的单"），不是「我在这单上有绩效」：
**同一个人可以在不同订单上分别是 `打单工资` 与 `销售提成`** （`uniq_performance_order_user` 只约束**同一** `(order, user)`，跨单不约束）。只用 `user=me` 会把「单是别人接的、我只是拿销售提成」的那些单也列出来。判别力测试：`tests/test_my_orders_api.py::test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on`。 - **数据权限**：仍然必须过一次 `scope_queryset`（Global Constraint #7 / spec §4.4），且**不发明任何可见范围规则**（E-0006）——它今天是 pass-through， `tests/test_my_orders_api.py` 有判别力测试钉住「接线真的被用上」。 ⚠️ **序列化器定义在本模块里**：不经 `api/serializers.py`（两边各管各的，改动互不牵连）。
"""
from urllib.parse import quote

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

# ⭐ 跨 app 复用「提前结算申请」的领域层（D-0365）：网页端批量申请与 App 端    单条申请走**同一条**判据链（`create_settle_request`）与同一组出参形状    （`settle_request_payload` / `early_settlement_payload`）⇒ 口径只有一份。
#    ⚠️ 跨 app import API 层符号有先例（`api/jidanpu_member_views.py` import    `takerapi.api.order_scope.claimed_perf_of`）；这里的名字都是**公开名**    （无下划线前缀）。⛔ 不把这些函数复制一份到本模块——两份判据必然漂移。
from apps.accounts.app_income_views import (
    EarlySettleError,
    create_settle_request,
    early_settlement_payload,
    settle_request_payload,
)
from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu, jidanpu_openapi, jidanpu_upstream, order_qr_fetch
from apps.business.api.base import EnvelopePagination
# ⭐ 导出行数上限与绩效导出**同一份判据**（同 app 内 import 常量，⛔ 不再抄一个数）。
from apps.business.api.performance_write_views import EXPORT_MAX_ROWS
from apps.business.models import (
    EmpType,
    JidanpuUpstreamOrderLink,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
    PerformanceSettleRequest,
    SettleStatus,
)
from apps.business.my_orders_export import build_workbook, row_values
from apps.business.performance_amount import MissingPriceError, effective_taker_price
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.core.storage import get_storage
from apps.openapi.serializers import money
from apps.rbac.permissions import has_node

# ── 「我的收入」页的四档筛选（spec §3.3）──────────────────────── 四档：`all`（默认，全部我接的单）/ `unsettled`（已完成·未结账）/ `settled`（已完成·已结账）/ `completed`（全部已完成）。
# ⚠️ 后三档的**共同前提都是「订单已完成」**——「未结 / 已结」在 spec §2 的定义就写在「我接的**已完成**单里」；只按 `settle_status` 筛会把「代练中、未结账」的单也塞进「未结」那一档（那会让打手以为这单马上要结账了）。
INCOME_ALL = "all"
INCOME_UNSETTLED = "unsettled"
INCOME_SETTLED = "settled"
INCOME_COMPLETED = "completed"
INCOME_FILTER_CHOICES = (
    INCOME_ALL,
    INCOME_UNSETTLED,
    INCOME_SETTLED,
    INCOME_COMPLETED,
)


class MyOrdersIncomeSerializer(serializers.Serializer):
    """`GET /my-orders?income=…` 的**唯一**入参校验口（spec §3.3）。 ⚠️ 值先进类型化查询之前必须过它：`?income=<任意串>` 直接进 `filter()` 不会抛错，而是**静默返回空列表**（`filter(settle_status="不存在的值")` 完全合法）——那会让调用方以为「这个筛选下没有单」，而不是「你参数写错了」。⇒ 这里按 `choices` 拒成 `1000`，与 `search_params`（绩效列表）同一条口径。 ⚠️ 空串按「没传」处理（前端常送 `?income=`），在 `list()` 里先被过滤掉。
    ⭐ D-0365：日期三参数**同口进本类**（「唯一入参校验口」的地位不变）： `date_type` 四档 + `date_from` / `date_to`（两端都含，判**哪一列**见 `MY_ORDER_DATE_FIELDS`）。⚠️ 三个参数**要么都有意义地组合**：`date_type` 缺省时 `date_from` / `date_to` **不生效**（`apply_date_filter` 直接原样返回）—— ⛔ 不替用户猜一个默认列（对标发的是选中档原值；「只给日期不给档」由前端负责提示，后端按「没传日期筛选」处理）。
    """

    income = serializers.ChoiceField(
        choices=INCOME_FILTER_CHOICES,
        required=False,
        default=INCOME_ALL,
    )
    #: ⭐ D-0365：四档自描述取值（接手 / 交单 / 录单 / 结算），列映射见
    #: `MY_ORDER_DATE_FIELDS`（形状照 `performance_views.PERFORMANCE_DATE_FIELDS`）。
    date_type = serializers.ChoiceField(
        choices=("claimed", "submitted", "created", "settled"),
        required=False,
    )
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)


#: `list()` 从查询串里**捞走**的键全集（其余键不进序列化器）。
#: ⚠️ 捞取判据 = 「键在集合里**且**值非空串」——空串按「没传」（前端常送 `?income=`）。
MY_ORDER_PARAM_KEYS = frozenset({"income", "date_type", "date_from", "date_to"})

#: ⭐ D-0365：`date_type` 的取值 → 施加 `date_from` / `date_to` 的**列名** （本线唯一的映射处；形状照 `performance_views.PERFORMANCE_DATE_FIELDS` 同款）。接手时间 / 结算时间是 `Performance` **自己的**列，交单 / 录单走 `order__` 关系打**订单**的列。
#: ⚠️ `claimed_at` / `order__submitted_at` / `settled_at` 三列**可空** ⇒ 按那一档筛时空值行**自然被排除**（SQL 三值逻辑，`PERFORMANCE_DATE_FIELDS` 同款先例）——这是**预期**、不是漏行，由 `tests/test_my_orders_income_functions.py` 逐档钉住。
MY_ORDER_DATE_FIELDS = {
    "claimed": "claimed_at",             # 接手时间（绩效行）
    "submitted": "order__submitted_at",  # 交单时间（订单）
    "created": "order__created_at",      # 录单时间（订单）
    "settled": "settled_at",             # 结算时间（绩效行）
}


def apply_date_filter(queryset, params):
    """把日期三参数接到「我接的单」查询集上（**唯一的日期筛选实现**）。 ⚠️ **只加 `filter()`，不动数据范围**：调用方传进来的已经是 `my_orders_queryset()`（= user + MAIN + 两层未软删 + `scope_queryset`）之后的集合。
    ⚠️ `date_type` 缺省 ⇒ **原样返回**（`date_from` / `date_to` 不生效——不猜默认列，见 `MyOrdersIncomeSerializer` 的注释）。区间按**本地日期**收窄（`__date` 用当前时区切天），两端都是**含**的（`PERFORMANCE_DATE_FIELDS` 的消费形状同款）。
    ⚠️ 边界判据用 `is not None`（E-0021）：「传了 / 没传」才是这里要区分的。
    """
    date_type = params.get("date_type")
    if date_type is None:
        return queryset
    date_field = MY_ORDER_DATE_FIELDS[date_type]
    if params.get("date_from") is not None:
        queryset = queryset.filter(**{f"{date_field}__date__gte": params["date_from"]})
    if params.get("date_to") is not None:
        queryset = queryset.filter(**{f"{date_field}__date__lte": params["date_to"]})
    return queryset


def apply_income_filter(queryset, income):
    """把 `income` 这一档接到「我接的单」查询集上（**唯一的筛选实现**）。 ⚠️ **只加 `filter()`，不动数据范围**：调用方传进来的已经是 `user=request.user` + `emp_type=打单工资` + `scope_queryset` 之后的集合
    （`MyOrdersViewSet.get_queryset()`）⇒ 这里不碰 `scope_queryset`，也不改分页。 ⚠️ `settle_status` 的取值是**中文**（`SettleStatus` 的两个 label 就是库里的值）， ⛔ 不要按 `SETTLED` / `UNSETTLED` 这种枚举**名**去记——那是另一个词表。
    """
    if income == INCOME_COMPLETED:
        return queryset.filter(order__status=OrderStatus.COMPLETED)
    if income == INCOME_UNSETTLED:
        return queryset.filter(
            order__status=OrderStatus.COMPLETED,
            settle_status=SettleStatus.UNSETTLED,
        )
    if income == INCOME_SETTLED:
        return queryset.filter(
            order__status=OrderStatus.COMPLETED,
            settle_status=SettleStatus.SETTLED,
        )
    # `all`（含未知档，虽然序列化器已经挡掉了）⇒ 原样返回。
    return queryset


#: 批量申请一次最多多少条。**依据**：列表一页上限 200（`EnvelopePagination.max_page_size`），「把一整页勾上申请」就是它的自然上限；同时把 `IN (...)` 与逐条事务的规模挡在安全区
#: （同类「超长列表 ⇒ 绑定参数上限 ⇒ HTML 500」的风险见 `serializers.MULTI_VALUE_MAX`）。超限 ⇒ `200 + code 1000`，⛔ 不静默截断。
MY_SETTLE_REQUEST_BATCH_MAX = 200


class SettleRequestsBatchSerializer(serializers.Serializer):
    """`POST /my-orders/settle-requests/` 的入参（D-0365）。 ⚠️ 元素用 `IntegerField`（不是 `CharField`）：主键是整数列，让畸形值在入口就是 `1000`（`?` 那类「字符串直进 filter()」的 500 见 E-0010）。 ⛔ 元素**不加 `min_value`**：`0` / 负数查不到就是「不存在」，由逐条的 1035「该绩效不存在或不属于你」解释（App 端单条入参同一条理由，E-0010）。 ⚠️ `allow_empty=False`：空列表是调用方的 bug（前端空选时自己提示、不发请求），
    到了后端就是 `1000`。 ⚠️ **上限 200**（`MY_SETTLE_REQUEST_BATCH_MAX`）登记在此；超限 ⇒ `1000`，不截断。 ⚠️ 重复 id **不特判**：逐条结果自然呈现（第二条撞「已申请过」的预期拒绝）。 ⚠️ **不接收其它字段**：DRF 的 `Serializer` 默认忽略多余键——归属永远取认证态（`AGENTS.md` §4：⛔ 不接受租户标识 / 用户标识参数）。
    """

    performance_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        max_length=MY_SETTLE_REQUEST_BATCH_MAX,
    )


class MyOrderSerializer(serializers.ModelSerializer):
    """「我接的单」的对外形状（brief §2 的字段清单）。

    `order` 是 `PROTECT` 非空 FK（`Performance.order`），所以 `source="order.xxx"`
    不会取到 `None`——不需要 `SerializerMethodField` 那层空判。
    """

    order_no = serializers.CharField(source="order.order_no", read_only=True)
    # ⭐ 来源单号（D-0301）：打手 H5 / App 的展示口径 =「来源单号非空取它、    空回退 `order_no`」，回退渲染在前端做（载荷给**两个原值**、不在这里拼展示值，
    #    与 `OrderListSerializer.source_order_no` 的既有口径一致）。    `Order.source_order_no` 是 `blank=True` 的非 NULL 文本列 ⇒ 没填就是空串。
    source_order_no = serializers.CharField(
        source="order.source_order_no", read_only=True
    )
    # ⚠️ `status` 是**订单**状态（代练员看的是「我这单到哪一步了」），
    #    不是绩效的 `review_status`——后者是审核口径，客服/客服主管从 `/performances` 看。
    status = serializers.CharField(source="order.status", read_only=True)
    game_name = serializers.CharField(source="order.game.name", read_only=True)
    game_role_name = serializers.CharField(source="order.game_role_name", read_only=True)
    project_text = serializers.CharField(source="order.project_text", read_only=True)
    # 「指派给我的订单」第 1 列的加急徽标要它（`order-myorderlist.ts` 的 `CELL_TEMPLETS[1]`）。
    # `Order.is_urgent` 是 `BooleanField(default=False)`（`src/apps/business/models.py` 的 `Order`）⇒ 恒有值，
    # 不需要 `SerializerMethodField` 那层空判（同本类 docstring 对 `order` 非空的说明）。
    is_urgent = serializers.BooleanField(source="order.is_urgent", read_only=True)
    # 「我的收入」页（`/emp/myemp`）第 2 列要的「用户名称」。 ⚠️ `Performance.user` 是 **null=True** 的 FK（`models.py` 的 `user` 字段，还没归属人的行）    ⇒ 空关系的嵌套取值会抛 `AttributeError`：DRF 的 `Field.get_attribute` 接得住它，    而它内部 `get_attribute(instance, attrs)` 那个逐段取值的循环**只**把    `ObjectDoesNotExist` 吞成 `None`，接不住 `getattr(None, "username")`。    **实测两种构造的差别**（DRF 3.18.1：两个只差 `allow_null` 的序列化器，各序列化一条
    #    `user_id=None` 的 `Performance`）：    - 不写 `allow_null`：`data = {"id": 4242}`。`read_only` 让 `required=False`，于是落到      `Field.get_attribute` 里 `raise SkipField()` 的分支，再被 `Serializer.to_representation`      里「捕获 `SkipField` 后 `continue`」的分支跳过 ⇒ **不报错，但 `username` 这个键      从载荷里消失**（静默丢字段，比 500 更难发现）；    - 写 `allow_null=True`：`data = {"id": 4242, "username": null}`。走
    #      `Field.get_attribute` 的 `if self.allow_null: return None` 分支，      再走 `Serializer.to_representation` 的「值为 `None` 就写 `null`」分支      （`CharField.to_representation` 就是 `str(value)`，所以 `None` 靠的是序列化器      那条短路，不能只靠字段自己兜）。判别力用例：`tests/test_my_orders_api.py::test_null_user_row_serializes_username_as_null`。
    username = serializers.CharField(source="user.username", read_only=True, allow_null=True)
    # 「指派给我的订单」第 12 列「接手人」（`order-myorderlist.ts` 的 `CELL_TEMPLETS[12]`； ⚠️ 下标是 **12**——`COLS` 第 0 项是勾选列，11 是隐藏的「旺旺/来源」）。显示名的**唯一出处**是 `apps.accounts.display.user_display_name`（昵称优先、空串回落登录账号）
    # ⇒ ⛔ 不在这里另写一份 `first_name or username`（两套判据必然漂）。 ⚠️ `user` 是 **null=True** 的 FK（还没归属人的绩效行）⇒ 用 SerializerMethodField 显式判空，    ⛔ 不让 DRF 在空关系上把整个键**静默丢掉**（同 `username` 那条实测，见上面的长注释）。
    taker_name = serializers.SerializerMethodField()
    # 「指派给我的订单」第 13 列「接手价」（`CELL_TEMPLETS[13]`；下标是 **13**，同上），口径见计划 §1：**覆盖价优先**（`Performance.taker_price`），否则回落**大厅接单价** （`Order.hall_price`）。算式**只调** `performance_amount.effective_taker_price`：与 `/claim`、指派重算、退款重算共用同一份口径（⛔ 不在这里另写一遍 `if taker_price is not None`）。写入方（判「这一列生产上有没有写过」时要看这些；复评 Minor-2 逐条核实）： · `Order.hall_price` —— **指派带价**那一支（`order_writes.assign_order` 里
    #   `order.hall_price = price`，`apps/business/order_writes.py:880` 一带）与   `POST /hall-price/`（`order_writes.change_hall_price`）写； · `Performance.taker_price` —— `POST /performances/<pk>/adjust/`   （`apps/business/api/performance_views.py:777` 一带）与   `performance.apply_amount_to_effective_base`（`apps/business/performance.py:269` 一带，   服务 `/adjust-amount/`）写；`performance.reset_taker_performance` 清（清除接单人）。
    # ⚠️ `performance.set_taker` **不写这两列**：它只写 `user` / `claimed_at`（「接手那一刻」）。    ⛔ 别把上面两列的写入方挂到它名下（本行上一版就是这么挂错的，复评当场抓到）。 ⛔ 不动 `temp_amount` / `bonus_amount`（预计 / 生效绩效）：那是"这单我能挣多少"，    与"接手价"不是一件事——拿它冒充接手价就是台账上的一句假话。
    taker_price = serializers.SerializerMethodField()
    # 本表字段（不是跨表）：取值直接来自 `Performance` 行——
    # `emp_type` = 打单工资 / 销售提成，`settle_status` = 未结账 / 已结账。
    emp_type = serializers.CharField(read_only=True)
    settle_status = serializers.CharField(read_only=True)
    #: ⭐ 这一行**自己的**提前结算申请时刻（spec §3.4）：有申请 ⇒ 项目时区的 ISO 串；没有 ⇒ `null`。App 据此把「申请提前结算」按钮换成「已申请」。 ⚠️ **只按该行自己的 `performance` 取**（反向一对一 `settle_request`），
    #: ⛔ 不按「这个人有没有申请过」判——那会把别人的 / 别的单的申请显示到这一行上。 ⚠️ 值的时区口径同 `PerformanceListSerializer.settle_requested_at`（`local_iso`， E-0238 家族：`SerializerMethodField` 不过 DRF 的 `DateTimeField`，裸 datetime 会出 UTC 串）。
    settle_requested_at = serializers.SerializerMethodField()

    class Meta:
        model = Performance
        fields = [
            "id",
            "username",
            "taker_name",
            "order_no",
            # ⭐（D-0301）：来源单号（打手端展示回退的取数前提）。追加在
            #    `order_no` 之后，既有键的位置一字不动。
            "source_order_no",
            "status",
            "game_name",
            "project_text",
            "is_urgent",
            "temp_amount",
            "bonus_amount",
            "taker_price",
            "emp_type",
            "settle_status",
            "review_status",
            "created_at",
            # ⭐：这一键排**末尾**（既有 15 键的位置一字不动）。
            "settle_requested_at",
            "game_role_name",
        ]

    def get_settle_requested_at(self, obj):
        """该行的提前结算申请时刻；没有申请 ⇒ `None`（口径见字段声明处）。 ⭐ **2026-10-23（spec §8.1）：`cancelled` 的申请回 `None`** —— 作废之后打手可以重申，
        App 必须把按钮换回「申请提前结算」；否则这一行会永远显示「已申请」、按钮点不动（后端接受重申，前端却把它藏了）。与配额口径 `settle_requests.QUOTA_STATES` 同边界。
        """
        try:
            request_row = obj.settle_request
        except ObjectDoesNotExist:
            return None
        if request_row.state == PerformanceSettleRequest.State.CANCELLED:
            return None
        return local_iso(request_row.created_at)

    def get_taker_name(self, obj) -> str | None:
        """接手人的**显示名**；绩效行还没归属人 ⇒ `None`。

        ⚠️ 显示名的唯一出处仍是 `user_display_name`（昵称优先、空串回落登录账号）。
        ⛔ 不在序列化器里重写那份回落判据：两处各写一份，改了一处就漂。
        """
        if obj.user is None:
            return None
        return user_display_name(obj.user)

    def get_taker_price(self, obj):
        """**接手价**（两位小数字符串；两边都没有价 ⇒ `None`）。算式**只调** `performance_amount.effective_taker_price`（2026-10-23 评审 Important-4）：它是「覆盖价优先、否则订单默认价」的**唯一实现**（`/claim`、指派重算、退款重算都走它）
        ⇒ ⛔ 不在这里再写一遍 `obj.taker_price if ... else obj.order.hall_price`（第二套判据必然漂）。 - **只吞 `MissingPriceError`**（两边都没价，语义明确 = 没有接手价 ⇒ `None`）；其它 `AmountError` **照抛**（那是金额口径出了问题，不是「没价」——E-0063 同族：
        按类型分档，⛔ 不按「当时的价是不是 None」判）。 - 金额出口仍走 `apps.openapi.serializers.money`（两位小数字符串 / `None`），与对外查单**同一口径**（⛔ 不在这里另写一套量化规则）。
        """
        try:
            value = effective_taker_price(obj)
        except MissingPriceError:
            # ⚠️ 这是**唯一**该回 `None` 的一档：覆盖价与大厅接单价都没有 ⇒ 页面那一格空着，
            #    ⛔ 不回落 `0.00`（那等于说「这单不要钱」）。
            return None
        return money(value)


class MyOrderDetailSerializer(serializers.ModelSerializer):
    """打手受限订单详情的对外形状（booster-h5 spec §6.2 的键集，⛔ 不多给）。 ⚠️ 键数：spec §6.2 当年是 16 键；**2026-10-24（D-0301）补 `source_order_no` ⇒ 17 键** （恰好键集网 `tests/test_my_order_detail_api.py::DETAIL_KEYS` 同一次改）。 ⛔ 仍然**不含**客户联系方式（R1 裁定不给）与其它金额列、审核 / 结算字段。 **为什么不用桌面详情那两个端点**（spec §2.4）：`GET /orders/<单号>/` 的门是 `order.view_detail` / `view_content`，内建「代练员」的模块集不含 `order` 栏目 ⇒ 打手默认 403。这里是**打手手机端专用的受限读**：字段集不含客户联系方式（R1 裁定不给）、不含其它金额列与审核 / 结算字段。 - `claimed_at` = **绩效行的 `created_at`**（与列表口径同轴：桌面「接手时间」
    渲染用的就是它，spec §2.3；不是 `Performance.claimed_at`——那列是接单**时刻**，存量行可空，列语义见 `models.py` 的长注释）。 - `temp_amount` 走 `ModelSerializer` 的 `DecimalField` ⇒ 与 `MyOrderSerializer` 同一序列化口径（字符串）。 - `server_name`：`Order.server` 是 `null=True` 的 FK ⇒ 嵌套取值要 `allow_null=True` （同本模块 `username` 那条实测结论：不写它时空关系会让键从载荷里**静默消失**）。 - `group_qr_url`：该单 `img_type=1`（群聊二维码）**最新一张已认领**行的短时签名 URL， ⛔ 绝不返回 `img_type=0` 的行（那是进度截图，M2 截图线的事）。签名 URL 复用 **截图读路径的同一件生成物**（`get_storage().presign_get`，
    `ScreenshotCollectionView.list_items` 的同款调用），⛔ 不自造第二套。「每单一张」由企微群码线（D-0213）保证；多张取最新属防御。
    """

    order_no = serializers.CharField(source="order.order_no", read_only=True)
    # ⭐ 来源单号（D-0301）：同 `MyOrderSerializer.source_order_no` 的口径——
    #    载荷给原值，展示回退（`source_order_no || order_no`）由 H5 / App 前端做。
    source_order_no = serializers.CharField(
        source="order.source_order_no", read_only=True
    )
    status = serializers.CharField(source="order.status", read_only=True)
    game_name = serializers.CharField(source="order.game.name", read_only=True)
    server_name = serializers.CharField(
        source="order.server.name", read_only=True, allow_null=True
    )
    project_text = serializers.CharField(source="order.project_text", read_only=True)
    content = serializers.CharField(source="order.content", read_only=True)
    demand = serializers.CharField(source="order.demand", read_only=True)
    time_requirement = serializers.CharField(
        source="order.time_requirement", read_only=True
    )
    game_account = serializers.CharField(source="order.game_account", read_only=True)
    game_sub_account = serializers.CharField(
        source="order.game_sub_account", read_only=True
    )
    game_role_name = serializers.CharField(source="order.game_role_name", read_only=True)
    is_urgent = serializers.BooleanField(source="order.is_urgent", read_only=True)
    remark = serializers.CharField(source="order.remark", read_only=True)
    # ⚠️ 键名与来源**刻意不同名**：对外契约叫 `claimed_at`（spec §6.2），数据源是
    #    绩效行的 `created_at`（同轴说明见类 docstring）。
    claimed_at = serializers.DateTimeField(source="created_at", read_only=True)
    group_qr_url = serializers.SerializerMethodField()

    class Meta:
        model = Performance
        fields = [
            "order_no",
            # ⭐（D-0301）：来源单号。详情的键数从 16 → 17；
            #    「恰好键集」网（`tests/test_my_order_detail_api.py::DETAIL_KEYS`）同一次改。
            "source_order_no",
            "status",
            "game_name",
            "server_name",
            "project_text",
            "content",
            "demand",
            "time_requirement",
            "game_account",
            "game_sub_account",
            "game_role_name",
            "is_urgent",
            "remark",
            "claimed_at",
            "temp_amount",
            "group_qr_url",
        ]

    def get_group_qr_url(self, obj) -> str | None:
        row = (
            OrderScreenshot.objects.filter(
                order_id=obj.order_id,
                img_type=OrderScreenshot.IMG_GROUP_QR,
                status=OrderScreenshot.STATUS_READY,
            )
            .order_by("-pk")
            .first()
        )
        if row is None:
            return None
        return get_storage().presign_get(row.object_key).url


def my_orders_queryset(user):
    """「我接的单」的**基线**查询集（列表与导出**共用**这一段，D-0365 抽成模块函数）。 ⚠️ 抽出来是为了让**导出端点**与列表端点共用同一组数据范围条件（`MyOrdersViewSet .get_queryset()` 与 `MyOrdersExportView` 都调它）——「导出的是另一批数据」这种 bug 只可能在两处各写一套时出现。**行为零变化**：四条条件与 `scope_queryset` 收口逐字未动。四条口径（原 `get_queryset()` 的注释，逐字保留在此）： - `user=user`：**功能语义**（这个端点的定义就是「我的」，spec §4.4），不是数据权限；
    - `emp_type=EmpType.MAIN`：过滤的是**接单人身份**（「我**接**的单」），不是「我在这单上有绩效」——同一个人跨订单可以分别是 `打单工资` 与 `销售提成`（`uniq_performance_order_user` 只约束**同一** `(order, user)`），只用 `user=me` 会把「单是别人接的、我只拿销售提成」的单也列出来。判别力测试： `tests/test_my_orders_api.py::test_my_orders_covers_orders_i_claimed_not_orders_i_only_earn_sales_on`； - `order__deleted_at__isnull=True`（⭐ D-0144 C1）与 `deleted_at__isnull=True`
    （⭐ 2026-10-23 评审 Important-1）：跨关系 JOIN 不经过默认 manager ⇒ 两层都要显式补（与 `apps/accounts/app_income_views.py::my_performances` 同一条口径）； - `scope_queryset`：数据权限的**唯一卡点**（Global Constraint #7 / spec §4.4），基线 pass-through，但接线必须在（E-0006：不发明可见范围规则）。
    """
    # `select_related` 覆盖序列化器读的**跨表**字段：`order`（含 `order.game`，给 `game_name`）与 `user`（给「我的收入」页的 `username = source="user.username"`），避免按行回查。
    # 判别力用例：`tests/test_my_orders_api.py::test_query_count_does_not_grow_with_the_number_of_rows` （比较 1 行 / 4 行两次请求的 SQL 条数，少了 `"user"` 时每多一行多 2 条）。
    qs = Performance.objects.select_related(
        "order", "order__game", "user", "settle_request"
    ).filter(
        user=user,
        emp_type=EmpType.MAIN,
        order__deleted_at__isnull=True,
        deleted_at__isnull=True,
    )
    return scope_queryset(user, qs)


def get_own_order_detail_performance(user, order_no):
    order = get_object_or_404(Order, order_no=order_no)
    performance = (
        Performance.objects.select_related("order", "order__game", "order__server")
        .filter(order=order, emp_type=EmpType.MAIN, user=user)
        .first()
    )
    if performance is None:
        raise PermissionDenied("该单不是你接的，不能查看详情")
    return performance


class MyOrdersViewSet(viewsets.GenericViewSet):
    """我接的单（spec §5）。**只读**：本 ViewSet 没有写方法（代练员交单走 `/submit`）。"""

    pagination_class = EnvelopePagination
    serializer_class = MyOrderSerializer

    #: 逐 action 的节点 code 表（形状与 `OrderViewSet.ORDER_PERMISSIONS`、 `PerformanceViewSet.PERFORMANCE_PERMISSIONS` 同）。 ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的
    #: 字符串常量，用变量拼出来的 code 不会被它看见。 ⚠️ 未登记在表里的 action 一律走默认权限类（fail-closed：仍要登录），**不是**默认放行。
    MY_ORDERS_PERMISSIONS = {
        "list": ("my_orders.send_to_my_system",),
        # ⭐ booster-h5（spec §6.2）：打手受限详情**复用同一颗节点**——「我的订单」入口的读门。⛔ 不新增 RBAC 节点（D-0128 冻结）。 ⚠️ **动作名刻意叫 `order_detail` 而不是 `detail`**：`detail` 是 DRF ViewSet 的
        # 保留 initkwarg（`ViewSetMixin.as_view` 里那句 `cls.detail = None` 会在 urls.py 加载时把类上的同名**方法**覆盖成 `None` ⇒ 请求期 `handler` 为 None、每个请求都是 `TypeError`——本线实测踩到）。URL 形状不受影响（仍是 `…/detail/`，
        # 由 `urls.py` 的显式 `path()` 决定）。
        "order_detail": ("my_orders.send_to_my_system",),
        # ⭐ 群二维码按需同步：**复用同一颗节点**（「我的订单」入口的读门）。
        # ⛔ 不新增 RBAC 节点（D-0128 冻结）；它只往鸡蛋铺同步台账排一条拉图请求，
        # 真正的权限收窄在动作体里（只有**本单接单人**能触发，见 `sync_screenshots`）。
        "sync_screenshots": ("my_orders.send_to_my_system",),
        # ⭐ 批量申请提前结算（D-0365）：**复用同一颗节点**（「我的订单」入口的门）。⛔ 不新增 RBAC 节点（D-0128 冻结；`sync_screenshots` 同款先例）——
        # 它只服务「我自己的行」的申请动作，权限收窄在 `create_settle_request` 的归属判据里（别人的绩效 id ⇒ 同句「该绩效不存在或不属于你」，不给越权探测面）。
        "settle_requests": ("my_orders.send_to_my_system",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。 ⚠️ **为什么是并集**：默认类表达的是"这一层的最低门槛"（今天 = `IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`），节点判据表达的是"这个动作还要哪个 code"。两者是**合取**，不是二选一——替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。 ⚠️ 认证（401）与授权（403）的分流点、以及"为什么还要显式保留 `IsAuthenticated`"
        的完整理由（含 `rest_framework/views.py` 的行号）写在 `apps/business/api/order_views.py::OrderViewSet.get_permissions` 的 docstring 里，本处照抄形状、不重复整段论证。 ⚠️ 判等必须用 `type(p) is not IsAuthenticated`（**不要** `isinstance`）： `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉，而那正是"其余默认类一条都不丢"要防的事。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.MY_ORDERS_PERMISSIONS.get(self.action)
        if not codes:
            return base
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    def get_queryset(self):
        """列表的行集 = `my_orders_queryset(request.user)`（口径见那个函数的 docstring）。"""
        return my_orders_queryset(self.request.user)

    def list(self, request, *args, **kwargs):
        """我接的单。谁可以看：**持有 `my_orders.send_to_my_system` 的调用方**（spec §4.3 / §5；由 `get_permissions()` 的类级节点门判）。没有代练员职位闸。节点门回答能不能进这个入口；查询集已按当前用户收窄。 ⚠️ 授权判据放在**解析查询参数之前**（与 `/hall`、`/assign`、绩效列表同形）：未授权者不该从错误差异里探测端点行为。⚠️ 授权之后**只解析**业务查询参数 `income`（「我的收入」页四档筛选，spec §3.3）与日期三参数
        `date_type` / `date_from` / `date_to`（⭐ D-0365，2026-11-02，映射见 `MY_ORDER_DATE_FIELDS`）；它们同样先过序列化器（`MyOrdersIncomeSerializer`）再进 `filter()`，非法值 ⇒ `200 + code 1000`（⛔ 不是 500，E-0010）。 ⚠️ 分页与 `scope_queryset` 一个字没改：这两组参数只是**在已授权的可见行集上** 再加 `filter()`，不改变数据范围，也不改变分页器。
        """
        params = MyOrdersIncomeSerializer(
            data={
                key: value
                for key, value in request.query_params.items()
                if key in MY_ORDER_PARAM_KEYS and value != ""
            }
        )
        params.is_valid(raise_exception=True)
        queryset = apply_date_filter(
            apply_income_filter(
                self.get_queryset(), params.validated_data.get("income", INCOME_ALL)
            ),
            params.validated_data,
        )
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def settle_requests(self, request):
        """批量申请提前结算（⭐ D-0365，2026-11-02；`POST /my-orders/settle-requests/`）。网页端「我的收入」页工具栏「申请结算」的落点。语义 = **已有的「提前结算申请」** （2026-10-23 spec §3.4，App 已在用）：勾选行批量提交，**逐条**调 `create_settle_request(request.user, pid)`（它自带锁人 + 事务 + 五步判据链， ⛔ 本动作不另写第二套判据、不改任何金额与状态）。 - 单条预期失败（`EarlySettleError`，业务码 1035/1036）**不中断整批**：
        按条记 `{performance_id, ok, code, msg}`；成功条再附 `settle_request` 载荷（复用 `settle_request_payload` 的同一形状）。 - 重复 id **不特判**：第二条会撞「已申请过」的预期拒绝，逐条结果如实呈现。 - 额度**当场重算**（`early_settlement_payload`，申请已落库 ⇒ `used` 已含成功条）； ⛔ 不拿「申请前的 used + N」去拼（两条数字来源必然漂）。 - 闸门：`MY_ORDERS_PERMISSIONS["settle_requests"]` = 与列表同一颗
        `my_orders.send_to_my_system`（⛔ 不新增 RBAC 节点，D-0128 冻结； `sync_screenshots` 同款先例）。 - 留痕：**刻意不记操作日志**——申请只写一行 `PerformanceSettleRequest` （申请人 / 时刻 / 状态都在那一行上，行本身就是留痕）、不动钱；与 App 端 `POST /api/app/income/settle-requests/` 的登记**同一档豁免**（登记面见 `tests/test_operation_log_ledger.py` 的 `DELIBERATELY_NOT_LOGGED`）。
        """
        serializer = SettleRequestsBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        items = []
        for performance_id in serializer.validated_data["performance_ids"]:
            try:
                row = create_settle_request(request.user, performance_id)
            except EarlySettleError as exc:
                items.append(
                    {
                        "performance_id": performance_id,
                        "ok": False,
                        "code": exc.code,
                        "msg": exc.message,
                    }
                )
                continue
            items.append(
                {
                    "performance_id": performance_id,
                    "ok": True,
                    "code": ErrorCode.OK,
                    "msg": "",
                    "settle_request": settle_request_payload(row),
                }
            )
        return api_response(
            {
                "items": items,
                "early_settlement": early_settlement_payload(request.user),
            }
        )

    def order_detail(self, request, order_no=None):
        """打手受限订单详情（booster-h5 spec §6.2；`GET /my-orders/<order_no>/detail/`）。 ⚠️ **动作名叫 `order_detail`、URL 仍叫 `…/detail/`**：`detail` 是 DRF ViewSet 的保留 initkwarg（见 `MY_ORDERS_PERMISSIONS` 那条注释里的实测）。三道判据，顺序固定： 1. **类级节点门**（`get_permissions()` 按 `MY_ORDERS_PERMISSIONS["order_detail"]` 装配，与 list 同一颗 `my_orders.send_to_my_system`）——在动作体之前执行。 2. **单存在且未软删**：`get_object_or_404(Order, order_no=…)` 走默认 manager ⇒ 已软删单取不到（D-0144 的常规读面口径）。取不到时按本仓信封约定是 **HTTP 200 + `1002`「记录不存在或已被删除」**（`envelope_exception_handler`
        把 `Http404` 归一化成业务结果，与 `/submit`、截图端点同一出口）。 3. **接单人是谁**：存在 `Performance(order=该单, emp_type=MAIN, user=request.user)`（与列表同轴——`user` + `MAIN` + 单未软删）才放行；否则 **403**（资源级授权是传输层语义，msg 参照 `/submit` 的「该单不是你接的…」句式）。⚠️ 「没有 MAIN 行」与「MAIN 行不是我的」在这条路径上是**同一个业务含义**（你不是这张单的接单人）⇒ 共用同一出口。 ⛔ **不写** `order.view_credentials` 审计（spec §6.2）：载荷不含游戏密码——`Order` 模型本就没有密码列（D6，`models.py` 的「玩家账号」段）。 ⚠️ 动作内**不过** `scope_queryset`：那条卡点管的是「列表读面按数据范围收窄」，
        而这里的第一判据就是 `user == request.user`（功能语义，行集天然只含自己的）；租户边界由 schema 隔离保证（跨租户否定用例见 `tests/test_my_order_detail_api.py`）。
        """
        perf = get_own_order_detail_performance(request.user, order_no)
        order = perf.order
        payload = MyOrderDetailSerializer(perf).data
        # ⭐ D-0348：群码区块是「暂无群码」（`group_qr_url` 为 None）时，
        #    在这个自然触发点异步拉一次「马上来」查码（不阻塞本响应；五道闸在触发模块里，
        #    未配置 ⇒ 第一道闸就返回，零动作）。
        if payload.get("group_qr_url") is None:
            order_qr_fetch.maybe_enqueue_fetch(order)
        return api_response(payload)

    def sync_screenshots(self, request, order_no=None):
        """按需同步本单的**群二维码**（2026-10-23；`POST /my-orders/<order_no>/screenshots/sync/`）。打手在 App 里点开详情、本地**没有**群二维码时调它一次。上游那一步（读图 + 逐张下载）是分钟级的最坏耗时 ⇒ **请求里一次上游都不打**：只往鸡蛋铺同步台账排一条 `upstream.pull_pics_in` 请求行（`payload.trigger = app_detail`），由 `jidanpu_sync` 的下一拍（`LOOP_SLEEP_SECONDS = 3`）用**现有**的 `pull_order_screenshots` 消费（⛔ 不是第二条拉图实现，见 `jidanpu_upstream.run_pull_pics_requests`）。判据顺序固定（前面的都不满足才轮到下一道）： 1. **类级节点门**：`MY_ORDERS_PERMISSIONS["sync_screenshots"]`（与列表同一颗）。 2. **单存在且未软删**：`get_object_or_404` 走默认 manager（D-0144）⇒ 取不到时
        按本仓信封约定是 **200 + `1002`**（与 `order_detail` 同一出口）。 3. **本单接单人本人**：`Performance(order, emp_type=MAIN, user=request.user)` ——与 `order_detail` **逐字同口径**（不是本人的单 ⇒ **403** + 同一句式； ⛔ 与「还是不是接单人」无关，别的账号既看不到详情也不该能触发）。 4. **本地已有可用群码**（`img_type=1` + `status=ready`）⇒ `triggered=false` + 原因，**不入队**（已有图不重复打上游）。 5. **没有上家链路**（`JidanpuUpstreamOrderLink`）⇒ `triggered=false` + 原因，不入队。 6. **这条上家线不可用**（停用 / ②式官方API / 凭据不全）⇒ `triggered=false` + 原因：排了也永远没人消费（消费段只跑①式启用中的绑定）⇒ 不如如实说清。
        7. 其余 ⇒ 入队；**冷却内重复调用不再入队**（判据 = 数据库里最近一条本标记请求行的 `created_at`，见 `queue_pull_pics_request`）⇒ `triggered=false` + `retry_after_seconds`（另一条请求可能正在拉）。响应（前端只读这三个键）：`{"triggered": bool, "reason": str, "retry_after_seconds": int}`。
        """
        order = get_object_or_404(Order, order_no=order_no)
        perf = Performance.objects.filter(
            order=order, emp_type=EmpType.MAIN, user=request.user,
        ).first()
        if perf is None:
            raise PermissionDenied("该单不是你接的，不能同步群二维码")
        if OrderScreenshot.objects.filter(
            order=order,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        ).exists():
            return api_response({
                "triggered": False,
                "reason": "本单已经有群二维码了，不用再同步",
            })
        link = (
            JidanpuUpstreamOrderLink.objects.filter(order=order)
            .select_related("binding")
            .order_by("-pk")
            .first()
        )
        if link is None:
            return api_response({
                "triggered": False,
                "reason": "本单不是从上家鸡蛋铺来的，没有可同步的群二维码",
            })
        binding = link.binding
        if not binding.enabled:
            return api_response({
                "triggered": False,
                "reason": "上家这条同步线已停用，同步不了，请联系客服",
            })
        if jidanpu_openapi.is_openapi_binding(binding):
            return api_response({
                "triggered": False,
                "reason": "上家这条线走的是官方API方式，暂不支持从 App 同步截图，请联系客服",
            })
        if jidanpu.credentials_of(binding) is None:
            return api_response({
                "triggered": False,
                "reason": "上家同步账号还没配好，同步不了，请联系客服",
            })
        event, retry_after = jidanpu_upstream.queue_pull_pics_request(
            order=order, link=link, requested_by=request.user,
        )
        if event is None:
            return api_response({
                "triggered": False,
                "reason": jidanpu_upstream.PULL_PICS_REQUEST_GONE_REASON,
            })
        if retry_after > 0:
            return api_response({
                "triggered": False,
                "reason": "刚刚已经同步过一次了，请稍等片刻",
                "retry_after_seconds": retry_after,
            })
        return api_response({
            "triggered": True,
            "reason": "已开始从上家同步群二维码，请稍等几秒",
        })


# ── 「我的订单收入」导出（⭐ D-0365；照 `PerformanceExportView` 的形状）──


#: 订单状态逗号串一次最多多少个 token（与 `serializers.ORDER_STATUS_MAX_TOKENS` 同一惯例
#: 上限；本模块**自写逐 token 校验**、不 import 那边的私有函数 `_order_status_tokens`
#: ——同形状各写一份时，注释点名出处，见 `MyOrdersExportSerializer.validate_order_status`）。
EXPORT_ORDER_STATUS_MAX_TOKENS = 50


class MyOrdersExportSerializer(serializers.Serializer):
    """`GET /my-orders/export` 的入参（D-0365）。 **筛选与页面同源**：这里收的是页面 5 个客户端筛选的**服务端镜像**（否则「导出的不是看到的」）+ 日期三参数（与 `GET /my-orders/` 同名同契约）。逐键判据与前端 `applyIncomeFilters` 的对齐关系： - `game`：游戏**名**精确匹配（`order__game__name`；前端按 option 文案对 `game_name`）； - `settle_status`：后端 `SettleStatus` 的**中文值**（前端下拉的 value 就是它）；
    - `order_status`：逗号串枚举码，**照 `PerformanceListSearchSerializer._order_status_tokens` 的逐 token 校验形状本模块自写**（⛔ 不 import 那个私有名；同名注释点名出处）； - `key1` / `key2`：`icontains`（SQL 侧即大小写折叠，与前端「trim + toLowerCase includes」同判据；`CharField` 默认 `trim_whitespace=True` ⇒ 前端 trim 后端也 trim，这条同源性登记在案）； - `date_type` / `date_from` / `date_to`：与 `MyOrdersIncomeSerializer` 同名同契约
    （判哪一列见 `MY_ORDER_DATE_FIELDS`；⚠️ 校验口是**两个类各一份**——列表的入参类 docstring 记了「唯一入参校验口」的地位，导出这里是它自己的入口校验，两份的 choices 逐字相同，改一处要两处一起改）。 ⚠️ 非法值一律 `200 + code 1000` 的**信封**（不是 xlsx、也不是 500，E-0010）——本端点「同一个 URL 两种形状」的约定与 `PerformanceExportView` 逐字同款。
    """

    game = serializers.CharField(required=False, max_length=64)
    settle_status = serializers.ChoiceField(
        choices=(SettleStatus.UNSETTLED, SettleStatus.SETTLED),
        required=False,
    )
    order_status = serializers.CharField(required=False, max_length=1500)
    key1 = serializers.CharField(required=False, max_length=128, allow_blank=True)
    key2 = serializers.CharField(required=False, max_length=900, allow_blank=True)
    date_type = serializers.ChoiceField(
        choices=("claimed", "submitted", "created", "settled"),
        required=False,
    )
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate_order_status(self, value):
        """逗号串 → 已校验的码列表（**同形状自写**，出处 = `serializers._order_status_tokens`）。判据逐条对齐那边（任一不满足 ⇒ 业务码 `1000`）：每个 token 非空（`,,` / `X,` 拒）、
        每个 token 都是 `OrderStatus` 的合法码（非法的**点名**，⛔ 不静默丢）。 ⚠️ 整串空串按「没传」：视图入口先丢整串空串（本端点照 `search_params` 的同一处理）。
        """
        tokens = value.split(",")
        if len(tokens) > EXPORT_ORDER_STATUS_MAX_TOKENS:
            raise serializers.ValidationError(
                f"订单状态一次最多筛 {EXPORT_ORDER_STATUS_MAX_TOKENS} 个"
            )
        for token in tokens:
            if token == "":
                raise serializers.ValidationError("订单状态不能有空项")
            if token not in OrderStatus.values:
                raise serializers.ValidationError(
                    f"订单状态里的 {token} 不是有效的状态码"
                )
        return tokens


class MyOrdersExportView(APIView):
    """导出「我的订单收入」记录（GET；D-0365）。 ⭐ **形状照 `PerformanceExportView` 逐条**（本仓导出端点的既定约定，本类不发明第二套）： - 成功时返回 `.xlsx` 的**文件字节**（单工作表「收入记录」，构造唯一出处是 `apps/business/my_orders_export.py`），**没有** `{code,msg,data}` 信封；失败时（参数非法 / 超上限）仍回**信封 + HTTP 200** ⇒ 同一个 URL 两种形状，前端必须按「非 JSON」分支处理（`downloadMyOrderExport` 的注释）。
    - **筛选与列表同源**：行集 = `my_orders_queryset(request.user)`（列表那组数据范围条件：user=me + MAIN + 两层未软删 + `scope_queryset`）+ 本序列化器校验过的镜像筛选 + `apply_date_filter`（列表同一个日期实现）。⛔ 不在导出侧另写一套取数。 - **上限**：`EXPORT_MAX_ROWS` 与绩效导出**同一份常量**（同 app import，不抄数）；超上限 ⇒ 信封「本次导出 N 行，超过上限 M 行，请先收窄筛选条件」HTTP 200， ⛔ **报错，不截断**（静默截断会让打手拿着少一截的账去对账）。
    - **留痕**：照 `PerformanceExportView` 先例——**只记成功的导出**（超上限 / 参数畸形那两条路都提前 `return` 了信封，没有导出任何数据，记一条「导出了」就是假话）； **不写 `target_kind` / `target_id`**（动作码 `my_orders.export` 的模板与分类见 `apps/audit/actions.py`，**新拟**：对标没有这个动作的日志原文可照抄）。
    """

    def get_permissions(self):
        """闸门 = `has_node("my_orders.send_to_my_system")`（「我的订单」入口的那颗节点）。 ⛔ 不新增 RBAC 节点（D-0128 冻结）：导出与列表是同一批受众（看得到列表的人才
        导得出列表），权限点共用。⚠️ 写成**方法**而不是类级 `permission_classes`：照 `PerformanceExportView.get_permissions` 的形状（该形状的理由见它的 docstring）。
        """
        return [has_node("my_orders.send_to_my_system")()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（必须转调 `super()`：它先判认证并抛 401）。"""
        super().permission_denied(
            request, message="你没有导出我的订单收入的权限", code=code
        )

    def get(self, request):
        # 入参（畸形值 ⇒ 200 + `code 1000` 的**信封**，不是 xlsx）。
        raw = {
            key: value
            for key, value in request.query_params.items()
            if key in {"game", "settle_status", "order_status", "key1", "key2",
                       "date_type", "date_from", "date_to"} and value != ""
        }
        params = MyOrdersExportSerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data

        queryset = my_orders_queryset(request.user)
        # ⭐ 镜像筛选与前端 `applyIncomeFilters` 逐键同判据（见序列化器的对齐表）。
        if data.get("game"):
            queryset = queryset.filter(order__game__name=data["game"])
        if data.get("settle_status"):
            queryset = queryset.filter(settle_status=data["settle_status"])
        if data.get("order_status"):
            # 到这里的值一定是非空列表（空串被入口丢成「没传」、空 token 被校验拒掉）。
            queryset = queryset.filter(order__status__in=data["order_status"])
        if data.get("key1"):
            queryset = queryset.filter(order__order_no__icontains=data["key1"])
        if data.get("key2"):
            queryset = queryset.filter(order__project_text__icontains=data["key2"])
        # 日期三参数与列表**同一个实现**（`apply_date_filter`，含「date_type 缺省 ⇒ 不筛」）。
        queryset = apply_date_filter(queryset, data)

        total = queryset.count()
        if total > EXPORT_MAX_ROWS:
            # ⛔ **报错，不截断**（理由见 `EXPORT_MAX_ROWS` 的注释与类 docstring）。
            return api_response(
                data=None,
                msg=(
                    f"本次导出 {total} 行，超过上限 {EXPORT_MAX_ROWS} 行，"
                    "请先收窄筛选条件"
                ),
                code=ErrorCode.VALIDATION,
            )

        # 行集转格子（纯映射在 `my_orders_export`，⛔ 视图不自己拼单元格）。
        rows = [row_values(performance) for performance in queryset]
        payload = build_workbook(rows)

        # ⭐ 只记成功的导出（理由见类 docstring 的「留痕」段）。 ⚠️ 操作人取**认证态**（与全项目同一条 R20 口径），不取请求参数。 ⚠️ 读操作**没有业务事务**（spec §6.2：写日志失败只丢一条导出记录）⇒
        #    不包 `transaction.atomic()`（与 `PerformanceExportView.get` 同形）。 ⚠️ 顺序：**先把文件建出来、再留痕**——建文件失败属未预期错误（响亮 500），    不该提前写一条「导出了」的假记录。
        record(
            log_type=ACTION_LOG_TYPES["my_orders.export"],
            action="my_orders.export",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
        )

        now = timezone.localtime()
        response = HttpResponse(
            payload,
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
        # ⚠️ 同时给 ASCII 兜底名与 RFC 5987 的 `filename*`（与绩效导出同款：中文名只有
        #    后者能被浏览器可靠采用，挑剔的客户端只认前者）。
        chinese_name = f"收入记录{now:%m%d%H%M%S}.xlsx"
        response["Content-Disposition"] = (
            f'attachment; filename="my-orders-{now:%Y%m%d}.xlsx"; '
            f"filename*=UTF-8''{quote(chinese_name)}"
        )
        return response
