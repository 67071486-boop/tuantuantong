import re
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers

from apps.accounts.display import display_names_for_usernames, user_display_name
from apps.accounts.serializers import INT4_MAX
from apps.business import settlement_amount, split_group
from apps.business.game_scope import game_scope, games_of
from apps.business.models import (
    MONEY_FIELDS,
    CustomerKind,
    DailiantongPush,
    EmpType,
    Game,
    GameLibrary,
    GameServer,
    JidanpuSyncEvent,
    Order,
    OrderRefund,
    OrderSource,
    OrderSourceGroup,
    OrderStatus,
    OrderType,
    PaymentStatus,
    PeerUpstreamBinding,
    Performance,
    PerformanceSettleRequest,
    PlatformIntegration,
    Project,
    ProjectCategory,
    ReviewStatus,
    SettleStatus,
    ShopBinding,
    ShopType,
    XgjShopBinding,
)
from apps.business.order_flags import (
    can_view_offer_price,
    can_view_taker,
    can_view_taker_price,
)
from apps.business.order_status import (
    GENERIC_TRANSITION_DEFAULT_NODES,
    GENERIC_TRANSITION_NODES,
    can_transition,
)
from apps.business.performance_amount import (
    DEDUCT_FIXED,
    DEDUCT_PERCENT,
    AmountError,
    commission_amount,
    deduct_amount,
)
from apps.accounts.choices import BusinessType
from apps.accounts.serializers import MultiRoleField
from apps.business.source_groups import group_rows_for_source, legacy_group_for_source
from apps.business.selectors import (
    EFFECTIVE_TAKER_PRICE,
    LATEST_PLATFORM_PUSHES,
    SPLIT_COMPLETED_MEMBERS,
    SPLIT_IS_MAIN,
    TAKER_IS_PLATFORM,
    TAKER_NAME,
    TAKER_SETTLED,
    search_orders,
)
from apps.business.settlement_amount import payable_net
from apps.core.datetime_display import local_iso
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node_now, nodes_of


#: 只能由**服务端动作**写入的订单列（`OrderSerializer` 与 `OrderUpdateSerializer` 都不收）：
#: `completed_at`（apply_transition 按「进出已完成」算出的派生事实）、`hall_publisher`（最后一次发单动作的人这条认证态事实：throw_to_hall 写 `request.user` + 指派 / 改派直写（`assign_order` 写操作人，D-0369；照 `models.py` 该字段注释的口径））、`split_id`（服务端生成的组标识——录单与详情页两条入口各自生成/复用，请求体不是权威来源：客户端能指定=谁都能把自己挂进别人的拆单组，而组是钱与展示的共同单位；判据 tests/test_order_split.py 的 SplitIntakeTests::test_the_model_column_cannot_be_forced_from_the_intake_payload 与 SplitEndpointTests::test_patch_cannot_move_an_order_into_or_out_of_a_group，两条都带阳性对照）、
#: `published_at` / `submitted_at`（「第一次被发出去 / 第一次进待验收」那一刻，值由服务端动作推出，唯一实现 order_time_fields._mark 带一条不变式 WHERE <列> IS NULL：只写第一次、且不清空）。⚠️ 不列只读的后果（本批实测）：两个写序列化器都是 exclude=[] ⇒ 内网只持 order.add_edit 的客服一个 PATCH 就能把这两列改成任意值（_mark 那条 WHERE 管不到 PATCH）、对外 openapi 改单端点共用 OrderUpdateSerializer ⇒ 外部系统可伪造我方订单的这两个事件时刻（由 tests/test_openapi_order_update.py 的「最终对外字段表」当场报出）。判据：tests/test_order_column_fields.py::ServerWrittenTimeColumnsAreClosedTests 四条（三条只读+阳性对照）。为什么进 read_only_fields：请求体不是这几列值的权威来源（⚠️ 不是防伪造——防伪造靠认证态；这里挡的是写入点跑偏）；两处共用本常量，避免各自手抄漂移。
SERVER_WRITTEN_ORDER_FIELDS = (
    "wecom_notice_journal", "wecom_notice_pending",
    "completed_at", "hall_publisher", "split_id", "split_kind",
    "published_at", "submitted_at",
    # ⭐ 业务方拍板：镜像卡派生的隐藏单 OneToOne（Order.hall_card）由服务端在卡落成/转正时写，⛔ 请求体不是权威来源
    #（不列只读 = POST /orders/ 带 hall_card 就能凭空造一张「隐藏单」，或把正常单挂到别人的镜卡上 ⇒ 从列表里消失）。
    # 判据：tests/test_order_column_fields.py 同族用例（录单 / PATCH 各一条 + 阳性对照）。
    "hall_card",
    # ⭐ 上家镜像卡派生隐藏单的 OneToOne（Order.upstream_hall_card，D-0325 真缺陷）。与 hall_card 同一条理由：
    # 只由 upstream_hall_shadow 的钩子/转正复用支写；不列只读 = 录单/PATCH 能把任意一张单挂到（或摘出）上家镜卡 ⇒ 单凭空消失/出现。
    # 判据：tests/test_openapi_order_update.py::ReadOnlyFieldTests 的「最终对外字段表」。
    "upstream_hall_card",
    # ⭐ D-0359：receipt_sync_notfound_count（「上家查无」放弃计数）只由 upstream_receipt_sync._bump_notfound（原子 UPDATE）写，
    # ⛔ 请求体不是权威来源（不列只读 = PATCH 可清零绕开夜间收敛 / 第三方可改小计数）。
    # 判据：tests/test_openapi_order_update.py::ReadOnlyFieldTests 的「最终对外字段表」+ test_the_receipt_notfound_counter_is_refused_and_changes_nothing（阳性对照）。
    "receipt_sync_notfound_count",
    # ⭐ 2026-11-10 客户线：Order.customer 只由领域核心按可选 `buyer_eid` 键（W1）与回填命令（W2）写。
    # ⛔ 请求体不是权威来源（不列只读 = 录单/PATCH 能把任意单挂到别人的客户档案上）。
    # 判据：tests/test_customer_members.py 的写入点契约网（含「PATCH/openapi 不是写入者」两条）。
    "customer",
    # ⭐ 新老客标签：只由录单的领域核心写（自动判定或 `customer_kind_override` 手动指定）。
    # ⛔ 请求体不是权威来源（不列只读 = 录单/PATCH 能直接伪造或绕过手动标记）。
    # 判据：tests/test_customer_kind.py 的两条写入面用例（录单 / PATCH 各一条 + 阳性对照）。
    "customer_kind",
    "customer_kind_is_manual",
)

#: 只能走**专用端点**写入的订单列。与 SERVER_WRITTEN_ORDER_FIELDS 刻意分开：那两条的理由是「值由服务端动作/认证态推出」（写入点跑偏），
#: 本条的理由是「两个写序列化器都是 exclude=[] ⇒ 不列只读的话，只持 order.add_edit 的客服一个 PATCH 就能改收款状态、
#: 绕过权限节点 order.change_payment_status 与留痕」（C12）。两条理由不同 ⇒ 不揉成一个常量；判据分别由 tests/test_order_column_fields.py 与tests/test_order_payment_status_field.py 的三个写路径用例钉住（录单落默认档 / 录单忽略请求体值 / PATCH 忽略请求体值，后两条各带阳性对照）。
DEDICATED_ENDPOINT_ONLY_ORDER_FIELDS = ("payment_status",)

#: 只能走**软删/恢复端点**（/orders/<单号>/delete/、/restore/）写入的订单列。与上面两条刻意分开：本条属于「绕过专用端点的权限节点」一族，
#: 但权限节点不同（order.delete / order.recycle）⇒ 各占一个常量。两个写序列化器都是 exclude=[] ⇒ 不列只读的话，只持 order.add_edit 的客服：
#: ① PATCH {"deleted_at": …} 就能把一张在库单改成已删（deleted_by 也能随便填），不经过 order.delete 的节点门、也不留 order.delete 审计行（只留 order.update）；② POST 带这两列 ⇒ 新单出生即已删（在库看不见、却仍占着单号）。两条都让 apps/business/recycle.py 开头那份「写入点枚举」变成假话（spec §6.1）。判据由 tests/test_order_soft_delete_write_guard.py 的四条用例钉住（两条写路径各一条+阳性对照+「没有 order.delete 审计行」）。
SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS = ("deleted_at", "deleted_by")


class PriceRedactionMixin:
    """价格字段的读取脱敏：flag 被施加时，值读出来是 None（spec §4.2/§5.2）。机制只有一种：get_fields() 在「请求存在且该限制被施加」时把目标字段替换成 SerializerMethodField（读出来恒为 None），其余一切照旧——不是声明式写死 SerializerMethodField、也不是在 get_fields() 里删键。
    ⚠️ 为什么经 get_fields() 条件替换：OrderSerializer 同时是录单的入参序列化器（create 的 OrderSerializer(data=request.data)，不传 context），而它今天接受请求体的 hall_price（tests/test_roles_and_intake_perf.py 的 _create 就送这个键并断言它决定了 temp_amount）；声明式的 SerializerMethodField 是只读的 ⇒ 录单会静默丢掉这个键。条件替换把「只读」限制在带 request 的构造上。
    ⚠️ 缺 request 时一律不脱敏（context.get("request") is None，例如内部构造/录单入参）：给非请求路径丢字段是静默的功能损失，而它的表现（「价格是 None」）与「flag 生效了」不可区分。⚠️ 字段仍在响应里、值为 None（前端契约是「键恒在」，本 mixin 只替换字段实例、不删键；None 的语义是「被隐藏」不是「价格为空」）。⚠️ 两条 flag 互不蕴含：REDACTED_PRICE_FIELDS 里每一项自带自己的判据（can_view_offer_price / can_view_taker_price），不共享总开关。"""

    #: (字段名, 看得见的判据)：判据返回 False（=该限制被施加）时该字段脱敏。字段名不在声明它的那个序列化器的 fields 里时是 no-op
    #: ⇒ 每一项都声明在真正暴露该字段的那个类上（OrderSerializer / OrderListSerializer 的 hall_price、PerformanceListSerializer 的 taker_price），基类保持空元组。
    #: 大厅列表不自己声明脱敏：它继承 OrderListSerializer.REDACTED_PRICE_FIELDS，同时把 hall_price 的 source 覆盖成生效接单价 EFFECTIVE_TAKER_PRICE。
    REDACTED_PRICE_FIELDS = ()

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is None:
            return fields
        for name, can_view in self.REDACTED_PRICE_FIELDS:
            if name in fields and not can_view(request.user):
                # `method_name` 显式给同一个方法：本 mixin 的每个字段都读成 `None`，
                # 不需要 per-field 的 `get_<field>`（也就不会与业务方法名相撞）。
                fields[name] = serializers.SerializerMethodField(
                    method_name="_redacted_price"
                )
        return fields

    def _redacted_price(self, obj):
        """脱敏占位字段的取值：**恒为 `None`**。"""
        return None


class ActorSerializer(serializers.Serializer):
    """操作人。P2 无认证、操作人来自请求体——所以它更需要长度校验。Order.updated_by 是 varchar(150)，审计落点 OperationLog 的 actor 列 M2 起也是 varchar(150)（迁移 0005_alter_operationlog_actor_target_name；分支合并 main 前它曾是 64；旧表 AuditLog.actor 同宽 150，但自本线起写入落点是 OperationLog，长度判据按它取）。两处同宽，这里的 150 就是最终口径、没有待办（与 reason 那次是同一个 bug、当时只修了一处）。
    ⚠️ 上限 150 = User.username 的 max_length（Ruling 8）：这几列装的是用户名，username 上限是 150 ⇒ 两处必须同顶。原来写 64 是 username 之外的另一个上限（实测过：64 < username 上限时，认证态用户名进这些列会 HTML 500）。
    ⚠️ 本字段仍是业务字段（Order.created_by/updated_by），按 P2 契约从请求体取；审计行那条腿自 R20 起取认证态（request.user.get_username()）——两者刻意不同源（spec §5.9）。不要把这里的校验当成防伪造（它只挡长度）。"""

    actor = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


#: ⭐ 订单状态多选（业务方指令「把这个管理订单页面的多选状态实装起来」；订单列表 status 与绩效列表 order_status 同一次改）：
#: 逗号串一次最多收多少个码。OrderStatus 总共只有 8 个码 ⇒ 50 沿用「多值参数」既有惯例上限（与 MULTI_VALUE_MAX 同值；
#: 枚举多值照 validate_order_nos 的形态做，不是 CommaSeparatedListField——那是纯字符串族、不校验枚举）。
ORDER_STATUS_MAX_TOKENS = 50

#: 逗号串的**整串长度**上限（与 `MULTI_VALUE_MAX_LENGTH` 同值：50 个最长 13 字符的码
#: 加分隔符 ≈ 700，1500 留足余量，同时挡住「一个几 KB 的查询串」不经校验直进查询）。
ORDER_STATUS_MAX_LENGTH = 1500


def _order_status_tokens(value: str) -> list[str]:
    """「逗号串枚举码」的逐 token 校验（status / order_status 两个序列化器共用）。⭐ 实装多选：形状与对标的 orderSta 单参数逗号串一致（orderlist.js 把多选 value 用逗号拼串）；形态照 validate_order_nos（手写 split + 逐 token 校验），但 token 是枚举码（OrderStatus.values）。判据（任一不满足 ⇒ 业务码 1000）：个数 ≤ ORDER_STATUS_MAX_TOKENS（50）；每个 token 非空（,, / IN_PROGRESS, 都拒，与 order_nos 的「不能有空项」同一判据）；每个 token 都是 OrderStatus 的合法码、非法的点名——⛔ 不静默丢掉那个 token（E-0021 同族）。
    ⚠️ 出参是 list[str]（已校验的码）：调用方（search_orders 的 status__in / performance_queryset 的 order__status__in）直接吃列表，⛔ 不在查询侧再 split 一次（「校验口」与「拼查询」两处各拆一次必然漂移，见 CommaSeparatedListField 的 docstring）。⚠️ 单值是逗号串的特例：?status=IN_PROGRESS 校验成 ["IN_PROGRESS"]，status__in=[x] 与旧 status=x 逐字等价 ⇒ 既有单值调用方行为不变。
    ⚠️ 整串空串按「没传」处理：两个入口（order_search_params / performance_views.search_params）都先丢整串空串 ⇒ 到这里的串至少有内容；但「有内容」不保证「每个 token 都非空」——,, / IN_PROGRESS, 这类含空 token 的串会到达这里、由「不能有空项」拒成 1000（判据：订单侧 StatusMultiSelectTests::test_separator_only_string_is_1000 与绩效侧 FilterTests::test_malformed_new_filters_are_business_codes_not_500 的 ,, 一例）。"""
    tokens = value.split(",")
    if len(tokens) > ORDER_STATUS_MAX_TOKENS:
        raise serializers.ValidationError(
            f"订单状态一次最多筛 {ORDER_STATUS_MAX_TOKENS} 个"
        )
    for token in tokens:
        if token == "":
            raise serializers.ValidationError("订单状态不能有空项")
        if token not in OrderStatus.values:
            raise serializers.ValidationError(f"订单状态里的 {token} 不是有效的状态码")
    return tokens


class OrderSearchBaseSerializer(serializers.Serializer):
    """订单类检索端点共用的查询参数（/orders/ 与大厅都吃这四个）。抽成基类的理由有两个、方向相反、都不能省：
    只定义一处：这四条各自的判据（game/server 是 IntegerField(min_value=1)）若每个端点各抄一份，将来加参数或改判据必漏一个——漏掉的那份不是「少筛一个」而是「少校验一个」：未校验的值直进 filter() 就是 HTML 500（E-0010）。
    不要靠「继承一整坨」：大厅不应用 status/is_urgent/tag/date_from/date_to——若 HallSearchSerializer 从 OrderSearchSerializer 继承，这五个会被校验通过然后静默忽略（Ruling 40②，客户端以为在筛、实际筛了个寂寞）；所以两条子类各自只声明自己真的会用的字段，共用部分从这个基类来。"""

    game = serializers.IntegerField(required=False, min_value=1)
    server = serializers.IntegerField(required=False, min_value=1)
    project_text = serializers.CharField(required=False, allow_blank=True)
    keyword = serializers.CharField(required=False, allow_blank=True)


class OrderSearchSerializer(OrderSearchBaseSerializer):
    """订单检索参数。校验必须在入口做：?game=abc 直接进 IntegerField 会抛 ValueError → HTML 500；?status=BOGUS 又会静默返回空——前者违反「业务结果一律 200+code」，后者让客户端分不清「没有匹配」和「参数写错」；走 DRF 的 ValidationError 才会得到 200+code=1000。
    ⚠️ 检索参数共 26 个（基类 4 + 本类 22）：演进口径——D-0144（D13 裁定回填）新增 taker_user / hall_publisher_user；公司店铺分组线 +1（source_group）；价格口径线 +2 ⇒ hall_price_min / hall_price_max（3 接发组合档的发单价区间，show_price_type 的 choices 同时扩到 0..4、choices 变化不改参数个数）；拆单 +1 ⇒ split_id（按拆单组取全部成员，同时是列表读面「本次不折叠」的开关，见 selectors.fold_split_members）；公司店铺分组再 +1 ⇒ source_group；新老客标签 +1 ⇒ customer_kind。改个数时这份口径要跟着改，别让它与签名脱钩。
    ⭐ 订单状态多选：参数个数不变（仍 26），status 从单个 ChoiceField 改成逗号串枚举码（逐 token 校验 OrderStatus.values，见 _order_status_tokens；单值是逗号串的特例、旧行为不变）。形状与对标 orderSta 的单参数逗号串一致 ⇒ 前端透传层零改动（api/orders.ts 的 status?: string 原样成立）。"""

    #: ⭐ 实装多选：收逗号串枚举码（IN_PROGRESS,COMPLETED）；逐 token 校验与出参形状见 _order_status_tokens（本类 validate_status 只是转调）。
    #: ⚠️ 空串仍按「没传」处理（order_search_params 统一丢空串，status 不在 BLANK_IS_A_VALUE 里）⇒ allow_blank 保持默认 False 不影响正常客户端。
    status = serializers.CharField(
        max_length=ORDER_STATUS_MAX_LENGTH, required=False
    )

    def validate_status(self, value):
        """逗号串 → 已校验的码列表（判据与文案的唯一出处 = `_order_status_tokens`）。"""
        return _order_status_tokens(value)

    is_urgent = serializers.BooleanField(required=False)
    tag = serializers.CharField(required=False, allow_blank=True)
    date_from = serializers.DateTimeField(required=False)
    date_to = serializers.DateTimeField(required=False)
    # 本批：收款状态（码、单选）。参数名用我们的枚举风格，与对标的 setSta（中文值）不同——差异登记在 spec §9。
    # ⚠️ allow_blank 保持默认 False：空串不是「没选」而是非法取值（?payment_status= 必须回 1000，见 OrderViewSet._search_params 的空串例外）。
    payment_status = serializers.ChoiceField(choices=PaymentStatus.choices, required=False)
    #: ⭐ 新老客筛选（三档 `全部/新客/老客`；「全部」= 不传这个参数）。按**存储的**标签筛，
    #: ⛔ 不按当前历史重新判定（标签是录单时快照）。非法取值由 DRF 回 1000。
    customer_kind = serializers.ChoiceField(choices=CustomerKind.choices, required=False)

    # ── ⭐ D-0144：控件裁剪与筛选实装（spec §5.4 / D11）────────────────────────────
    # ⚠️ 判据一律 `is not None`（E-0021）；`price_min`/`price_max` **不加** `min_value`
    # （D-0015：金额 0 与负数都是合法且有意义的边界）。
    order_source = serializers.IntegerField(required=False, min_value=1)
    #: ⭐ 公司店铺分组（计划 §4 #2）：只留「其来源属于该分组」的单。
    #: 语义同其它 id 类参数：`min_value=1` 挡掉 `0`/负数；不传 = 不过滤。
    source_group = serializers.IntegerField(required=False, min_value=1)
    order_type = serializers.ChoiceField(choices=OrderType.choices, required=False)
    sales_user = serializers.IntegerField(required=False, min_value=1)
    #: kefu_type 决定「拿哪个列去比 sales_user 这个值」：0⇒sales_user_id、5⇒created_by（文本列，id 解析成用户名再等值匹配）、
    #: 10⇒refunds__operator（文本列，同样 id→用户名）+ distinct()（refunds 是多值关系，会放大行）。
    #: ⚠️ 降级如实登记（spec §3-D11 半可做②）：5/10 是名字文本匹配 ⇒ 账号改名后按新名筛筛不到旧单（created_by 是录入时的快照）。
    kefu_type = serializers.ChoiceField(choices=("0", "5", "10"), required=False, default="0")
    #: 未安排：语义**逐字**取对标 `lay-tips`「勾选后将筛选出状态为未分配或者没有指派过打手陪玩的单子」。
    is_leak = serializers.BooleanField(required=False)
    #: 价格口径。五档全收：0 接单价格 / 1 发单价格 / 2 退款金额 / 3 接发组合 / 4 搜亏本单；0..4 之外仍是非法取值（业务码 1000）。
    #: 3 用两对区间：接单价走 price_min/price_max，发单价走 hall_price_min/hall_price_max；4 的算式 = hall_price > total_price（业务方当场裁定，严格大于），
    #: 且它不使用任何金额区间 ⇒ 同时给区间会被下面的 validate 响亮拒绝（原登记「⛔ 3/4 不接受」已作废——本线实装了两档）。
    show_price_type = serializers.ChoiceField(
        choices=("0", "1", "2", "3", "4"), required=False, default="0"
    )
    price_min = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    price_max = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    #: 3 接发组合档里发单价那一对区间。⛔ 与 price_min/price_max 同款不加 min_value（D-0015：金额 0 与负数都是合法且有意义的边界）。
    #: ⚠️ hall_price 列本身可空 ⇒ 这两条条件对 hall_price IS NULL 的单不命中（SQL 三值逻辑）。
    #: ⚠️ 只在档 3 有效：show_price_type != "3" 却带上它们会被下面的 validate ② 响亮拒绝（⛔ 不是默默丢掉——selectors.search_orders 只在 3 档读这两个参数）。
    hall_price_min = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    hall_price_max = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    order_nos = serializers.CharField(max_length=25799, required=False)
    batch_lookup_nos = serializers.CharField(max_length=25799, required=False)

    def _validate_lookup_tokens(self, value):
        tokens = value.split(",")
        if len(tokens) > 200:
            raise serializers.ValidationError("批量单号一次最多 200 个")
        if "" in tokens:
            raise serializers.ValidationError("批量单号不能有空项")
        return tokens

    def validate_order_nos(self, value):
        if any(len(token) > 128 for token in self._validate_lookup_tokens(value)):
            raise serializers.ValidationError("批量单号里每项最长 128 个字符")
        return value

    def validate_batch_lookup_nos(self, value):
        return self._validate_lookup_tokens(value)

    #: ⭐ D13：打手（走 Performance(emp_type=MAIN).user）与发单人（走 Order.hall_publisher FK）。
    #: ⛔ 两档按用户 id 精确筛、不做名字文本匹配。⚠️ min_value=1 是这两条真正的闸（search_orders() 里的 is not None 只是口径一致，见 selectors.search_orders 注释与 spec §5.4 补③）。
    taker_user = serializers.IntegerField(required=False, min_value=1)
    hall_publisher_user = serializers.IntegerField(required=False, min_value=1)

    #: ⭐ 拆单：按拆单组 ID 取该组的全部成员。空串按「没传」处理（与缺键同义）；它同时是列表读面「本次不做折叠」的开关：
    #: 显式给值 ⇒ 返回该组全部成员（含主单），没有它 ⇒ 列表按 split_id 折叠（每组只出 id 最小者）。
    #: 长度沿用 Order.split_id 的列宽（32），非法值由 DRF 回业务码 1000（⛔ 不是 500）；⚠️ max_length 与列宽同一个事实，改列宽要同时改这里（列宽唯一出处是模型）。
    split_id = serializers.CharField(required=False, max_length=32)

    def validate(self, attrs):
        """两条跨字段闸（命中 ⇒ 业务码 1000），⛔ 都不静默取一个 / 静默丢一个。
        ① ⭐ D-0311：「代练打手」（taker_user）与「发单人」（hall_publisher_user）可共存、语义是 AND——同一张单要同时满足「接手人=A」与「抛单人=B」。原「两档互斥 ⇒ 1000」（spec §5.4 补①）已废除：管理订单页新增了独立的「发单人」下拉、两个筛选可以叠加；旧组合控件（recType + #popgameName）仍只产生其中一个参数。⛔ 这里没有跨字段闸，两个参数各自过自己的 min_value=1。
        ② 发单价那一对区间只在「3 接发组合」档有效：show_price_type != "3" 却带了 hall_price_min / hall_price_max 任一个 ⇒ 响亮拒绝。⚠️ 不补这一条的后果是静默：selectors.search_orders 只在 3 档读这两个参数 ⇒ 档 0/1/2（含缺省，它落 default="0"）或档 4 带它们会被默默丢掉（E-0021 同族：静默丢条件）；判据逐个用 is not None（0 是「给了这个条件」）；排在③前面是有意的——档 4+发单价区间同时命中两条时给用户的诊断是「这一档不该有发单价区间」（更具体）；前端只在 3 档发这两个参数 ⇒ 正常用户走不到这条，它是给直接打接口的人响亮地拒绝。③「4 搜亏本单」档不使用金额区间（业务方裁定）⇒ 同时给 price_min / price_max 任一个 ⇒ 响亮拒绝。⛔ 不许收下再静默丢掉（E-0021 同族）；这一档的算式只比 hall_price 与 total_price 两个列、端点里没有「区间」这个自由度：若默默忽略，客户端会以为「按我给的区间+亏本」筛、实际拿到另一批数据。⚠️ 本支只查主栏那一对：hall_price_min/hall_price_max 到不了这里（②已把「档≠3」的它们全部拒掉）⇒ ⛔ 不在这里再写一遍那种到不了的条件（「防御性写法不要写成承重逻辑」，E-0021 同族说明）；前端在 4 档压根不发区间参数 ⇒ 正常用户走不到这条。"""
        if attrs.get("show_price_type") != "3" and (
            attrs.get("hall_price_min") is not None
            or attrs.get("hall_price_max") is not None
        ):
            raise serializers.ValidationError(
                "「发单价区间」只在价格口径的「接发组合」那一档使用，请先清空或改选档位。"
            )
        if attrs.get("show_price_type") == "4" and (
            attrs.get("price_min") is not None or attrs.get("price_max") is not None
        ):
            raise serializers.ValidationError(
                "「搜亏本单」不使用金额区间，请先清空金额区间。"
            )
        return attrs


class PenaltyGroupSearchSerializer(serializers.Serializer):
    group_penalty_children = serializers.BooleanField(required=False)
    include_page_relations = serializers.BooleanField(required=False)
    include_unmatched_split_members = serializers.BooleanField(required=False)
    penalty_parent_order_no = serializers.RegexField(r"\A[0-9]{20}\Z", required=False)


class FilterOptionsSerializer(serializers.Serializer):
    """候选名单端点的查询参数（spec §5.6）。⚠️ limit 对两份名单各自生效；默认 200、上限 500（我方自定，U14 未取到对标的返回形状）。
    ⚠️ `search` 是显示名 icase 包含（2026-11-09 业务方拍板「下拉改远程关键词搜索」）；前后空白剥掉（CharField 默认 trim）、空串在 `order_search_params` 就被丢成「没传」⇒ 不传时行为与改造前逐字一致。max_length=64 与 AssigneeOptionsSerializer.key 同值（同族参数同一上限）。"""

    game = serializers.IntegerField(required=False, min_value=1)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500, default=200)
    search = serializers.CharField(required=False, allow_blank=True, max_length=64)


class KefuOptionsSerializer(serializers.Serializer):
    """「销售客服」候选名单端点（GET /api/business/orders/kefu-options/）的查询参数。⚠️ 只声明一个 limit，与 FilterOptionsSerializer.limit 同值（默认 200 / 上限 500）：前端的搜索是在已取回的 option 里本地过滤（formSelects 的 search，与 #popgameName 那一格同一处置）、没有远程关键字搜索 ⇒ 本端点没有 key 参数。
    ⛔ 声明一个前端不传、后端也不读的参数等于「校验通过然后静默忽略」（Ruling 40②，与 AssigneeOptionsSerializer 的 docstring 同一条纪律）。⛔ 不接受任何租户标识：租户上下文只来自连接的 search_path（AGENTS.md §4）。"""

    limit = serializers.IntegerField(required=False, min_value=1, max_value=500, default=200)


class AssigneeOptionsSerializer(serializers.Serializer):
    """指派弹层候选名单端点（GET /api/business/orders/assignee-options/，D-0146 方案②）的查询参数。⚠️ 只声明弹层真的会传的四个参数（逐字读 order-orderlist.ts::fetchAssignCandidates：role / business_type / key / page_size）——多声明的字段会被校验通过然后静默忽略（Ruling 40②，与 HallSearchSerializer 同一条纪律）。
    ⚠️ role 与 business_type 复用账号列表那两个字段类（MultiRoleField 的逗号多选+请求期职位存在性校验 / 同一份 BusinessType 枚举）：这是「新端点的数据范围与 GET /api/accounts/ 一致」在入口校验这一层的兑现——自造第二套职位解析会让「同一串 ?role= 在两个端点上筛出不同的人」（E-0021 族：别在第二个地方重写同一判据）。
    ⚠️ limit 与 FilterOptionsSerializer.limit 同值（默认 200 / 上限 500）：弹层今天送 page_size=200，换端点只换数据来源、不放宽候选面。"""

    role = MultiRoleField(required=False)
    business_type = serializers.ChoiceField(choices=BusinessType.choices, required=False)
    key = serializers.CharField(required=False, allow_blank=True, max_length=64)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=500, default=200)


class FavoriteAssigneeSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(min_value=1)
    favorite = serializers.BooleanField()


class PeerSplitOrderItemSerializer(serializers.Serializer):
    project_text = serializers.CharField(max_length=900)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_project_text(self, value):
        if not value.strip():
            raise serializers.ValidationError("拆出订单的项目内容不能为空")
        return value


class CreateOrderAssignSerializer(serializers.Serializer):
    """POST /orders/ 上「建单+后续动作」那一段的可选入参（录单页快速指派 D-0179；本线新增「内部抢单」支，业务方裁定）。为什么单独一个序列化器、不塞进 OrderSerializer：这些键都不是订单列，而 OrderSerializer 是 exclude=[] 的模型序列化器，进去的键会被 serializer.save() 原样喂给 Order.objects.create(...) ⇒ TypeError。形状与既有 /assign/ 的 AssignSerializer 同源（同一个 user_id 语义），只是键名按这次动作取名（assignee=建单后要指派给谁）。
    assignee：接手人的用户主键。⚠️ 值域与 /assign/ 不是同一档（Minor-5）：AssignSerializer.user_id 是裸 IntegerField()，这里加了 min_value=1（本仓主键从 1 起，0/负数只可能是拼错的载荷）；差异只在拒绝发生在哪一层——这里被 DRF 字段校验拒（1000 字段级文案），/assign/ 那边走到视图的「指定的用户不存在」（同样 1000），用户可见行为等价（都不写一行）⛔ 但别把两处说成同一档。
    remember_assignee 四态："Y"=把这个人记成操作人自己的默认接手人（必须同时给 assignee——没有人的「记住此人」是自相矛盾的载荷，由视图回 1000 不静默忽略）、"N"=清除、"hall"=记下「默认抛内部抢单」（StaffProfile.default_throw_to_hall=True、同时清掉 default_assignee；两列互斥由 order_writes.set_default_assign_preference 一处保证 ⇒ 下次录单默认选中「内部抢单」档，价格每次自己填 ⛔ 不记价、⛔ 不碰任何金额列）、缺键=不动（「没勾且本来就没设」和「取消勾选」必须能分开）。⚠️ "hall" 必须本次 throw_to_hall=true（「记着默认抛单」而这一单偏偏没抛是自相矛盾的载荷 ⇒ 视图在建单之前回 1000，msg 定稿「勾选『默认抛内部抢单』时必须选择内部抢单」）；落库时机与 "Y" 同一形状：只在抛单成功之后落（抛单失败那条 1023 天然不落）；"N" 仍是无条件清除（无后续动作/指派失败 1022/抛单失败 1023 三条路径都清）——⚠️ "N" 的「无条件」是「在已经建单之后」；建单之前那几道闸（1000）一行都不写 ⇒ 也不清偏好（完整判据与用例名见 order_views.OrderViewSet.create 的 docstring 第 3 条末）。throw_to_hall：true=建单之后在同一次请求里把这张单按本请求的 hall_price 抛进内部抢单大厅（业务方裁定；feat/hall-throw-autorefresh）。⚠️ 与 assignee 互斥（同一个单选组的两档，同时给 ⇒ 视图回 1000）；为真时 hall_price 解析后的数值必须 >0，否则同样 1000（订单一行都不写）；⚠️ 后台录单即抛同时要求 order.add_edit 与 order.send_to_grab_hall；缺省 False：不传就是「只建单、不抛」；⛔ 它不是订单列，⛔ 不落库、⛔ 不写进任何订单字段。⚠️ 只影响「建单之后做不做后续动作」的键都没有默认值（assignee / remember_assignee：缺键就该是「不做这件事」，不是「清除」）；throw_to_hall 是唯一有默认值（False）的键：布尔开关不是三态，缺键与显式 false 是同一件事。⭐ 本线（业务方拍板「换销售客服=提成也换人」）新增第四个键 sales_user：值=目标用户的 auth.User 主键（与专用端点 POST …/sales-agent/ 的 user 同义）⛔ 不是名字（名字由服务端从这个人算出来，见 order_writes.change_sales_agent）；缺键=维持现状（归属=认证态 request.user，既有行为一个字不改）；带了键：归属=该人、按同一套校验（候选范围那一份判据；「已有绩效行」那条对新单天然不成立）。⚠️ 不持 order.allow_change_sales_agent 却带了这个键 ⇒ 视图回 HTTP 403（PermissionDenied，与专用端点同一道门）——⛔ 不是静默忽略：静默忽略会让「界面显示甲是销售客服、提成却归录单人」这个分叉看起来保存成功；⛔ 它不是订单列、不进 OrderSerializer，视图读的是这个序列化器的 validated_data（OrderSerializer 的 sales_user 仍是 read_only）；min_value=1 同 assignee。"""

    assignee = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    remember_assignee = serializers.ChoiceField(choices=("Y", "N", "hall"), required=False)
    throw_to_hall = serializers.BooleanField(required=False, default=False)
    # ⭐ 本线：录单时显式指定**提成归属人**（缺键 = 维持「归属 = 认证态」）。
    sales_user = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    #: ⭐ 拆单：录单页那颗勾选框「按代练项目拆分订单(项目之间用/号分隔)」的接线口。值 = 真 bool（⛔ 不复刻对标的 chaifen=Y 字符串形态）；
    #: 缺键 / false 同义 = 不拆（布尔开关没有三态）；与 assignee / throw_to_hall 不互斥：拆出来的第一笔（显示层主单）照常参与指派/抛单，其余各笔只落单、不做后续动作。
    #: ⛔ 它不是订单列（拆单只写 Order.split_id，那个列的值由服务端生成、客户端给不了）。
    split = serializers.BooleanField(required=False, default=False)
    split_orders = PeerSplitOrderItemSerializer(many=True, required=False, allow_empty=False)
    # ⭐ 2026-11-10 客户线（W1）：「读平台订单」回填的买家身份（闲管家 `buyer_eid`）。
    #   非空 ⇒ 领域核心 upsert 客户档案并连单；缺键 / 空串 = 不建不连（手工单没有买家标识）。
    #   ⚠️ 两个键都不是订单列、⛔ 塞不进 OrderSerializer（exclude=[] 会把它们喂给
    #   Order.objects.create ⇒ TypeError）；`eid` 超宽在这里显式拒（身份键不裁不兜），
    #   `buyer_nick` 不设上限由 upsert 裁到列宽（展示快照不得挡单，判据见 customers.upsert_customer）。
    buyer_eid = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=64, trim_whitespace=True
    )
    buyer_nick = serializers.CharField(
        required=False, allow_blank=True, default="", trim_whitespace=False
    )
    #: ⭐ 新老客标签的手动覆盖（`new` / `old`；空串或缺键 = 不覆盖）：自动单也可手动指定，
    #: 无买家 ID 的手工单不填名称也能带标签。⛔ 它不是订单列——订单上的两个标签列仍只读，
    #: 由领域核心按这个键（或自动历史判定）写；判据 tests/test_customer_kind.py。
    customer_kind_override = serializers.ChoiceField(
        choices=CustomerKind.choices, required=False, allow_blank=True
    )

    def validate(self, attrs):
        if "split_orders" in attrs and not attrs["split"]:
            raise serializers.ValidationError("添加拆出订单时必须开启拆单")
        return attrs


class DeleteOrderSerializer(serializers.Serializer):
    """软删的入参（spec §5.1）：isDelEmp 两档、默认 "N"。⚠️ 参数名逐字沿用对标（D6-④）：本仓已有同名同义先例（/clear-taker/，D-0142）。"""

    isDelEmp = serializers.ChoiceField(choices=("Y", "N"), default="N")


class SplitOrderSerializer(serializers.Serializer):
    """POST /orders/<单号>/split/ 的入参（拆单）。⚠️ 这是推断级设计（对标在那个入口零抓件：.tools/recon 下既没有那个弹层的抓件、docs/jidanpu-endpoints.json 的 406 条端点里 path 含 split 的也是 0 条）⇒ 入参形状是本线自行定义的、⛔ 不是复刻；取「字符串列表」而不是「project_text 含 /」是为了不再造一层切分规则（切分口径只有一处：order_intake.split_project_text，那是录单那条路的输入形态）。
    projects：长度 ≥2、每项非空的字符串列表（服务端按它逐笔落单）。拒绝路径全部回业务码 1000+一句人话（⛔ 不是 500，也 ⛔ 不借用 DRF 内置英文文案）：项目数<2 / 有空项（含纯空白项，trim_whitespace 之后是空串）。「订单已软删」「订单已完成」两条不在这里判：它们要读订单状态、落在视图里（OrderViewSet.split），同样回业务码+人话。⛔ 每项宽度沿用 Order.project_text 的列宽（900）——这一列是它的落点。"""

    #: ⚠️ `allow_blank=True` 是**刻意的**：空项的判据由下面的 `validate_projects` 给
    #: **我们自己那句人话**；留默认（`False`）会让它先被 DRF 的字段级文案拒掉。
    projects = serializers.ListField(
        child=serializers.CharField(
            max_length=900, allow_blank=True, trim_whitespace=True
        ),
        required=True,
    )

    def validate_projects(self, value):
        """两条业务判据（各自一句人话，`msg` 由 DRF 异常处理器取首条）。"""
        if len(value) < 2:
            raise serializers.ValidationError("拆分订单至少需要 2 个代练项目")
        if any(item == "" for item in value):
            raise serializers.ValidationError("拆分的代练项目不能有空项")
        return value


class PeerSplitExistingOrderSerializer(serializers.Serializer):
    order_no = serializers.CharField(min_length=20, max_length=20)
    project_text = serializers.CharField(max_length=900, required=False)
    total_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )

    def validate_project_text(self, value):
        if not value.strip():
            raise serializers.ValidationError("订单项目内容不能为空")
        return value

    def validate(self, attrs):
        if "project_text" not in attrs and "total_price" not in attrs:
            raise serializers.ValidationError("请填写订单项目内容或接单价")
        return attrs


class PeerSplitOrderSerializer(serializers.Serializer):
    remaining_project_text = serializers.CharField(max_length=900, allow_blank=True)
    source_total_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    new_orders = serializers.ListField(
        child=PeerSplitOrderItemSerializer(), required=False, default=list
    )
    existing_orders = serializers.ListField(
        child=PeerSplitExistingOrderSerializer(), required=False, default=list
    )

    def validate_remaining_project_text(self, value):
        if not value.strip():
            raise serializers.ValidationError("原订单剩余项目不能为空")
        return value

    def validate(self, attrs):
        order_nos = [row["order_no"] for row in attrs["existing_orders"]]
        if len(order_nos) != len(set(order_nos)):
            raise serializers.ValidationError("已有订单不能重复提交")
        return attrs


class BatchOrderEffectSerializer(serializers.Serializer):
    order_no = serializers.CharField(max_length=20, trim_whitespace=False)
    source_order_no = serializers.CharField(max_length=128, allow_blank=True, trim_whitespace=False)
    operation = serializers.CharField(max_length=120)


class BatchDeleteSerializer(DeleteOrderSerializer):
    """POST /orders/batch/delete/ 的 body：继承单笔的字段与校验、只多两个键。⛔ 不要复制 DeleteOrderSerializer 的字段定义（E-0021 的形状：同一件事两套判据）。
    ⚠️ isDelEmp 的缺省 "N" 照旧（继承来的 default），批量与单笔同口径；allow_empty=True / 不设 max_length 的两条理由与 BatchPriceSerializer 逐字相同（空数组与上限的文案一律由 order_batch.parse_order_nos 给）。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()
    confirmed_effects = BatchOrderEffectSerializer(many=True, required=False)


class V2BatchDeleteSerializer(BatchDeleteSerializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)


class HallSearchSerializer(OrderSearchBaseSerializer):
    """抢单大厅的检索参数（spec §5：筛游戏 / 价格区间）。⚠️ 只声明大厅真的会应用的字段（Ruling 40②）：hall 用到的就是 game/server/project_text/keyword 与下面两个价格端——不多不少；多声明的字段会被校验通过、然后被大厅静默忽略（修前正是如此：?status=IN_PROGRESS 看起来在筛、实际没筛，?status=BOGUS 还回一个让人以为「参数生效了」的 code=1000）。共用那四个的单一定义见基类（不靠「继承一整坨」，理由写在 OrderSearchBaseSerializer）。
    ⚠️ 不给金额加下界（min_value）——D-0015：金额可以为负（退款/冲抵/调整）。"""

    price_min = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    price_max = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    #: ⭐ D-0296：灰卡开关——1/true 时响应在可接区之后追加灰区（离厅快照 ∪ 已下架的模拟单）。⚠️ 不传时行为一字不变（缺省 False，纯加法）；
    #: 灰区只吃 game 筛选与游戏权限 scope（keyword/价格区间不作用灰区，理由见大厅读口的注释）。
    include_exited = serializers.BooleanField(required=False, default=False)
    #: ⭐ D-0296：优选开关——只留**优选**的可接行（原生按原始价 > 200、
    #: 镜像按勾兑展示价 > 200、模拟单恒优选）。⚠️ 只过滤**可接区**，灰区不受它影响。
    preferred_only = serializers.BooleanField(required=False, default=False)


class RecycleBinSearchSerializer(serializers.Serializer):
    """回收站列表的查询参数（spec §5.2 的 7 个+分页）。⚠️ 只声明回收站真的会应用的字段（与 HallSearchSerializer 同一条理由，Ruling 40②）：多声明的字段会被校验通过然后静默忽略。
    ⚠️ page_size 的允许集逐字取对标 orderrecycle.js 的 limits: [10,20,30]；默认 20（同 limit: 20）。"""

    game = serializers.IntegerField(required=False, min_value=1)
    status = serializers.ChoiceField(choices=OrderStatus.choices, required=False)
    deleted_from = serializers.DateTimeField(required=False)
    deleted_to = serializers.DateTimeField(required=False)
    order_source = serializers.IntegerField(required=False, min_value=1)
    keyword = serializers.CharField(required=False, allow_blank=True)
    game_account = serializers.CharField(required=False, allow_blank=True)
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.ChoiceField(choices=(10, 20, 30), required=False, default=20)


class ConfigParentFilterSerializer(serializers.Serializer):
    """配置资源的「按父级过滤」参数。只有三个带父级的配置 ViewSet 用它校验（servers / categories / projects）。GameViewSet 是链顶、没有父级 ⇒ 既不校验也不过滤——它上面的 ?game= 与其它未知参数一样被忽略（200+code 0）。不要为「统一」而给它加校验：Game 上没有 game 字段，game 在 /games/ 这个资源上根本没有意义，校验它只会多出一个无处可用的约束。
    ⚠️ 别把理由说反（初版这句话是错的）：?game=abc 在 /servers/、/categories/、/projects/、/orders/ 上都是会校验的（回 code 1000，有测试钉住）；唯一静默忽略它的端点，恰恰就是 /games/ 自己。校验必须在入口做——把未经校验的参数直接拿去 filter() 才会 ValueError → HTML 500（与订单检索同一个坑，见 test_no_route_500.py）。"""

    game = serializers.IntegerField(required=False, min_value=1)
    category = serializers.IntegerField(required=False, min_value=1)


class ConfigReferenceCountField(serializers.Field):
    """「被引用了多少次」的只读字段——删除前那句警告的数据源。⚠️ 默认口径是「只数还活着的下级」；order_count 是刻意的例外（算上已软删的订单，因为 Order 那三个 FK 是 PROTECT、引用是物理的）：见三个序列化器各自就地写的那句说明（spec D4）。
    两条取值路径、形状都是 int：①列表：读 ViewSet 的 annotate（一次查询算完，且带 distinct 防笛卡尔积）；②创建/更新响应：那时实例上没有注解属性 ⇒ 走 alive_count(value) 那次 COUNT。不回落的话 to_representation 会因取不到属性而报错；回落成「恒 0」又会说假话（恢复一行时它其实有 5 个下级），所以这里要求调用方给一个真会算的 lambda。
    ⚠️ source="*" 不能省：默认 source 是字段名，而实例上并没有 order_count 这个属性；DRF 对 source=="*" 的处理是把整个实例交给 to_representation（rest_framework/fields.py:373-374，source_attrs=[]）——SerializerMethodField 用的就是这一条，这里只是不走它那套 get_<field> 命名约定。"""

    def __init__(self, alive_count, **kwargs):
        self.alive_count = alive_count
        kwargs["source"] = "*"
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def to_representation(self, value):
        annotated = getattr(value, self.field_name, None)
        if annotated is not None:
            return annotated
        return self.alive_count(value)


class GameSerializer(serializers.ModelSerializer):
    """游戏。四个对标开关可写——它们是「游戏管理」页的行内开关（D-0032）。deleted_at 可写：置时间戳=软删除（DELETE 端点做的事，D-0085）、置回 null=恢复（前端「已删除」视图的「恢复」按钮）。
    把恢复做成一次 PATCH 而不是新端点，是为了复用既有的 *.update 埋点与端点契约（不新增写端点，也就不会牵动「每个写端点都要分类」的覆盖率网）。"""

    # ⚠️ order_count 刻意算上已软删的订单（与下面两个下级计数口径不同）：已删订单行仍物理引用着这个游戏（Order.game 是 PROTECT）、历史单要一直读得到
    # ⇒ 用反向外键 manager（g.orders 走 OrderManager、默认排除已删）计数会漏掉它们，删除警告说的就不是完整的历史账。⚠️ 这不是「不算就撞 ProtectedError」——
    # 本域的删除自 D-0085 起是软删除（游戏行永远不真删），ProtectedError 补偿路径不会触发，计数只服务「删除警告」。⛔ 不能写 g.all_objects：反向关系访问器只有 g.orders 一个名字（实测 AttributeError）。
    order_count = ConfigReferenceCountField(
        # ⭐ 隐藏单（镜像卡派生的载体）不是本租户的真订单 ⇒ 删除警告里不算它（它没有真实客户/绩效，算进去会让人以为删游戏会带走一笔生意）；
        # ⭐ R-4：同一道闸罩到上家镜像隐藏单（upstream_hall_card，D-0325）——同理，且与 GameViewSet 注解那条路的口径必须一致。
        # ⚠️ 代价：游戏名下只有隐藏单时这句警告会是 0——只是数字不全，不是操作死路（删除是软删、不会因它失败）。
        lambda g: Order.all_objects.filter(game=g)
        .exclude(hall_card__isnull=False)
        .exclude(upstream_hall_card__isnull=False)
        .count()
    )
    category_count = ConfigReferenceCountField(
        lambda g: g.categories.filter(deleted_at__isnull=True).count()
    )
    # 游戏下面的项目要跨一层（`categories__projects`）——删游戏时会级联下架它们，
    # 所以警告里得说清「会一起下架几个项目」。
    project_count = ConfigReferenceCountField(
        lambda g: Project.objects.filter(
            category__game=g,
            category__deleted_at__isnull=True,
            deleted_at__isnull=True,
        ).count()
    )
    #: 游戏 logo 的短时效读链接（feat/game-logo）。⚠️ 只回 URL、⛔ 不回 logo_key（key 是内部标识，前端不需要也不该拼）；
    #: ⚠️ 空 key ⇒ 空串（⛔ 不是 None）：网页端与打手 App 按空串回落到「首字+色块」。
    #: 本机存储的链接是相对路径（/api/media/blob/<key>，要登录态），生产 COS 是绝对签名 URL——照现状，⛔ 不为它改存储层。
    logo_url = serializers.SerializerMethodField()

    class Meta:
        model = Game
        fields = [
            "id", "name", "sort_order", "is_active",
            "is_default", "is_mobile", "is_companion", "disable_sms",
            "created_at", "deleted_at",
            "order_count", "category_count", "project_count",
            "logo_url",
        ]
        read_only_fields = [
            "created_at", "order_count", "category_count", "project_count", "logo_url",
        ]

    def get_logo_url(self, obj):
        """`logo_key` → 现签的读链接；没有 / 签不出来一律回空串（回落色块）。"""
        key = obj.logo_key or ""
        if not key:
            return ""
        try:
            from apps.core.storage import append_game_logo_thumbnail_query, get_storage

            return append_game_logo_thumbnail_query(get_storage().presign_get(key, cache_preview=True).url)
        except Exception:  # noqa: BLE001 —— 存储后端异常只降级这一格，不该让整页 500
            return ""


class GameLibrarySerializer(serializers.ModelSerializer):
    class Meta:
        model = GameLibrary
        fields = ["id", "name", "first_key", "sort_order"]
        read_only_fields = fields


class GameServerSerializer(serializers.ModelSerializer):
    class Meta:
        model = GameServer
        fields = ["id", "game", "name", "sort_order", "is_active", "created_at"]
        read_only_fields = ["created_at"]


class ProjectCategorySerializer(serializers.ModelSerializer):
    """项目分类。三个对标字段可写（D-0032）。assigned_platforms 是逗号拼接的平台 id 字符串（如 "8,30,90"）；这里只按长度校验（max_length=128），id 是否属于那 13 个平台由前端保证（对标的复选框也只在客户端约束）。
    不要在序列化器里加平台白名单校验：平台清单会随 M6 变化，硬编码在服务端会让加平台变成一次后端发布。deleted_at 可写：软删除/恢复，见 GameSerializer。"""

    # ⚠️ `order_count` 刻意算上已软删的订单（`PROTECT` 引用是物理的，理由见 `GameSerializer`）。
    order_count = ConfigReferenceCountField(
        lambda c: Order.all_objects.filter(category=c).count()
    )
    project_count = ConfigReferenceCountField(
        lambda c: c.projects.filter(deleted_at__isnull=True).count()
    )

    class Meta:
        model = ProjectCategory
        fields = [
            "id", "game", "name", "sort_order", "is_active",
            "assigned_platforms", "title_prefix", "title_suffix",
            "created_at", "deleted_at", "order_count", "project_count",
        ]
        read_only_fields = ["created_at", "order_count", "project_count"]


class ProjectSerializer(serializers.ModelSerializer):
    """代练项目。`deleted_at` 可写：软删除 / 恢复，见 `GameSerializer`。"""

    # ⚠️ `order_count` 刻意算上已软删的订单（`PROTECT` 引用是物理的，理由见 `GameSerializer`）。
    order_count = ConfigReferenceCountField(
        lambda p: Order.all_objects.filter(project=p).count()
    )

    class Meta:
        model = Project
        fields = [
            "id", "category", "name", "base_price",
            "sort_order", "is_active", "created_at", "deleted_at",
            "order_count",
        ]
        read_only_fields = ["created_at", "order_count"]


class OrderSourceSerializer(serializers.ModelSerializer):
    """旧版写单选归属，新版用 group_ids 替换完整归属。"""

    order_by = serializers.IntegerField(min_value=0, max_value=999, required=False, default=0)
    group = serializers.PrimaryKeyRelatedField(
        queryset=OrderSourceGroup.objects.all(), allow_null=True, required=False
    )
    group_name = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()
    group_ids = serializers.PrimaryKeyRelatedField(
        source="groups", queryset=OrderSourceGroup.objects.all(),
        many=True, required=False, write_only=True,
    )

    class Meta:
        model = OrderSource
        fields = [
            "id", "name", "remark", "order_by", "shop_type",
            "group", "group_name", "groups", "group_ids", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        if "group" in attrs and "groups" in attrs:
            raise serializers.ValidationError({"group_ids": "请使用一个分组字段提交"})
        return attrs

    def create(self, validated_data):
        if "groups" in validated_data:
            validated_data["group"] = next(iter(validated_data["groups"]), None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "groups" in validated_data:
            groups = validated_data["groups"]
            if instance.group not in groups:
                validated_data["group"] = next(iter(groups), None)
            return super().update(instance, validated_data)

        if "group" not in validated_data:
            return super().update(instance, validated_data)

        old_group_id = instance.group_id
        instance = super().update(instance, validated_data)
        if old_group_id is not None and instance.group_id != old_group_id:
            instance.groups.remove(old_group_id)
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if data["group"] is None:
            group = legacy_group_for_source(instance)
            data["group"] = group.pk if group is not None else None
        return data

    def get_group_name(self, instance) -> str:
        group = legacy_group_for_source(instance)
        return group.name if group is not None else ""

    def get_groups(self, instance):
        return group_rows_for_source(instance)


class OrderSourceGroupSerializer(serializers.ModelSerializer):
    source_count = serializers.IntegerField(read_only=True)
    source_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, write_only=True,
    )

    class Meta:
        model = OrderSourceGroup
        fields = ["id", "name", "order_by", "source_count", "source_ids", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_source_ids(self, ids):
        if self.instance is not None:
            raise serializers.ValidationError("只可在新增分组时添加来源")
        return sorted(set(ids))

    def create(self, validated_data):
        ids = validated_data.pop("source_ids", [])
        sources = list(
            OrderSource.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        ) if ids else []
        if len(sources) != len(ids):
            raise serializers.ValidationError({"source_ids": "部分订单来源已不存在，请刷新后重试"})
        group = super().create(validated_data)
        group.member_sources.add(*sources)
        return group


# 金额字段：**只有 /price/ 能改**（spec §6）。创建时可以写——录单页本来就是一次填完。
# 定义在 models.py（领域知识），这里只导入使用，避免两处定义漂移。


def allowed_transitions_for(order, request):
    """当前状态下该调用方真的能走的目标状态，供前端在流转弹窗里只列可选项。为什么由后端给、而不是让前端镜像 order_status.TRANSITIONS：镜像会漂移、漂移的表现是少一个选项——用户看不到任何错误、只会发现某个操作做不到；放在这里就等于白名单只有一处定义。
    ⚠️ 节点判据与 /transition/ 端点同源：都用 nodes_of(request.user)（apps/rbac/permissions.py）。此前这里用 roles=None（只判「从→到」），于是详情会列出该调用方其实做不到的迁移——前端据此渲染按钮、点了必然被拒、用户看不到任何错误说明（AGENTS.md §5 禁止这种「看起来一致、实际分叉」的断言）。⚠️ 调用方必须传真的 request（读 request.user 算节点）：传 None ⇒ 节点空集 ⇒ allowed_transitions 对所有人变空（静默丢功能）。
    ⚠️ 本函数从 OrderSerializer.get_allowed_transitions 的方法体原样抽出（模块级）：批量端点 batch/transition 的读取形态（spec §D10）也要拿这批单的可选目标——那是 API 层的事（要 request/nodes_of），所以落点是这里，⛔ 不是 order_status.py、⛔ 更不是把判据复制进 order_batch.py；抽取时行为一字未改（obj→order、self.context.get("request")→形参 request，其余逐字相同），get_allowed_transitions 现在是它的薄包装；对拍网是 tests/test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint（拿端点当 oracle）⛔ 改这里必须跑它。⚠️ REFUNDED 不再是空数组（业务方裁定第 3 条）：退单后由操作人显式指定恢复去向，order_status.TRANSITIONS 有两条回池出边（REFUNDED→UNASSIGNED / REFUNDED→WAITING_GRAB）；原写「REFUNDED 是终态，因此它的结果是空数组」随该裁定作废。⚠️ COMPLETED 的判据（D-0071）：不再无条件被排除，而是「调用方持节点 order.change_status_completed(17) 且状态机允许」时列出（业务方裁定原文「有权限的职位都可以用」，「有权限」=持节点 17——对标把「更改订单状态」拆成 16 非已完成/17 已完成）；对 COMPLETED 这一个目标，本函数与端点用的是同一处节点判据（OrderViewSet.get_permissions() 的 TRANSITION_TARGET_PERMISSIONS）；只改一侧会被对拍网当场抓到。⚠️ 本字段的契约是「通用 /transition/ 端点会接受的目标集合」（收紧，收掉 P3a 起的既存缺口）：每个目标都要同时过两关——①状态机边允许（can_transition(order.status, target, nodes=…)）②调用方持该目标在通用端点上的类级门（非 COMPLETED/WAITING_GRAB=16、COMPLETED=17、⭐ WAITING_GRAB=36（计划④），映射的唯一出处是 order_status.GENERIC_TRANSITION_NODES / _DEFAULT_NODES）。只判①会把「点下去必然 403」的目标也列出来（AGENTS.md §5 禁止「看起来一致、实际分叉」）；⚠️ 可观察的收紧：一个持 grab_hall.take_order(50) 但没有 16 的代练员，在 WAITING_GRAB 单上不再看到 IN_PROGRESS（那是 /claim/ 的边，不是通用端点的），该行为由对拍网的两条用例钉住；⚠️ 此前的实现（P3b B1-6）写的是「COMPLETED 只能由 /review/ 触发」并把 17 当作 /review/ 的专属节点门——那段口径随 D-0071 作废（那道闸门是我们自加的、对标没有的）。"""
    nodes = nodes_of(request.user) if request is not None else frozenset()
    # ⚠️ 本字段的契约（按实现写准）：主体 =「通用 /transition/ 端点会接受的目标集合」（不是「状态机允许的目标集合」）⇒ 必须把类级节点门也算进来：
    # 非 COMPLETED 目标要 order.change_status、COMPLETED 要 order.change_status_completed（与 OrderViewSet.get_permissions 的 TRANSITION_TARGET_PERMISSIONS 同一口径）；
    # 只判状态机会把「调用方点下去必然 403」的目标也列出来（本线之前只判角色不查节点 16 就存在这个既存缺口，此处一并收掉）。⭐ 本批（F3，spec §2.3）——有意的读/写不对称：状态机现在只有 (PENDING_REVIEW, COMPLETED) 一条入边，但业务方明确要求「下拉里『已完成』也要有」、选中时由前端弹提示、后端再拒一次 ⇒ 当前不是 COMPLETED 时只要调用方持节点 17，本字段照旧列出 COMPLETED（是否会被拒由 completed_requires_review 标出）。⛔ 不许为了让「列表 == 端点接受集合」那条用例变绿而把这一项删掉或放宽那条用例：用例已按新语义把「已完成」写成显式枚举的例外（tests/test_roles_and_intake_perf.py::test_listing_agrees_with_the_transition_endpoint）。
    completed_hint_nodes = GENERIC_TRANSITION_NODES[str(OrderStatus.COMPLETED)]
    could_be_told_to_review = bool(nodes & completed_hint_nodes)

    return [
        target
        for target in OrderStatus.values
        if (
            can_transition(order.status, target, nodes=nodes)
            and bool(
                nodes
                & GENERIC_TRANSITION_NODES.get(
                    str(target), GENERIC_TRANSITION_DEFAULT_NODES
                )
            )
        )
        or (
            # 提示项那一支：不是「这条边现在能走」，而是「业务方要求把它摆在选项里」
            str(target) == str(OrderStatus.COMPLETED)
            and str(order.status) != str(OrderStatus.COMPLETED)
            and could_be_told_to_review
        )
    ]


def order_source_display_name(order) -> str:
    """「订单来源」的显示值：挂字典 ⇒ 字典项的当前名；否则 ⇒ source 快照（D4 / R7）。⚠️ 本函数是唯一实现：订单列表（OrderSplitListSerializer.source_name）与详情（OrderSerializer.source_name）都调它，两处各抄一份必然分叉——「同一事实只有一处实现」（E-0011 同族；本文件里已有 net_received 那条同款教训）。
    ⚠️ 这里一个字都不写库：Order.source 是历史快照——字典项改名不重拍（D4）、字典项被删也不重写（R7/D5）；本函数只决定读的时候显示哪一个，数据库里那一列不动。
    ⚠️ 取数侧必须 select_related("order_source")（OrderViewSet.get_queryset 已加）：这是 FK 解引用，不预取就是一页 N 行 N 条字典查询（N+1）。⚠️ 判据写 is not None 而不是真值（E-0021 同族）：这条边界的语义是「有没有挂字典」，OrderSource 今天没有 falsy 形态，但不该赌将来没人给它加 __bool__。"""
    source = order.order_source
    if source is not None:
        return source.name
    return order.source


class OrderSerializer(PriceRedactionMixin, serializers.ModelSerializer):
    """订单的读/建序列化器（创建、详情、流转与改价的响应）。⚠️ hall_price（抛单价）按 order.restrict_view_offer_price 脱敏：本类用 exclude=[]、模型字段全在、其中就含 hall_price；
    脱敏只作用于带 request 的构造（详情与各动作的响应），录单入参（OrderSerializer(data=...)，无 context）的 hall_price 仍可写——理由与判据见 PriceRedactionMixin。"""

    #: 抛单价：被 `order.restrict_view_offer_price` 限制时读成 `None`。
    REDACTED_PRICE_FIELDS = (("hall_price", can_view_offer_price),)

    allowed_transitions = serializers.SerializerMethodField()
    split_kind = serializers.CharField(read_only=True)
    #: ⭐ 本批（F3，spec §2.3）新增的读侧标记，形态=一个独立的只读布尔键：True ⇔ allowed_transitions 里那个 COMPLETED 是会被拒的提示项
    #:（当前状态不是「待验收」，写下去会回业务码+逐字文案）。为什么另起一个键而不是只多回一个标记字段：allowed_transitions 的既有消费方
    #: 把它当字符串数组用（前端直接铺下拉），往数组里塞对象会破契约；另起一个键让「照旧铺下拉」与「选中时弹提示」两件事各读各的。
    completed_requires_review = serializers.SerializerMethodField()

    #: ⭐（来源字典当前名）：管理订单页「订单来源」列的显示值。挂字典 ⇒ 字典项的当前名（改名后立即跟随）；order_source 为空（自由文本/字典已删）⇒ 回落 source 快照。
    #: 取值口是模块级 order_source_display_name——与订单列表同源，⛔ 别在这里另写一份。
    #: ⚠️ source（快照）键保留不动：旧前端与导出仍读它，且历史不重写（D4 / R7）。
    source_name = serializers.SerializerMethodField()
    split_is_balance_anchor = serializers.SerializerMethodField()

    class Meta:
        model = Order
        exclude = ["split_ship_main_order_no", "wecom_notice_journal", "wecom_notice_pending", "penalty_approval", "peer_direct_pending"]
        read_only_fields = [
            "order_no",
            "status",
            "split_kind",
            # ⚠️ sales_user 决定客服提成的唯一归属：客服行建行即写死 user；sales_agent 是可手填的展示快照，不要和它一起只读。归属人不能由这个序列化器写，受治理的写入路在别处：
            # 录单默认按认证态写；持 order.allow_change_sales_agent 时 POST /orders/ 用 CreateOrderAssignSerializer 收非订单列的 sales_user（不持却带了 ⇒ 403，不是静默忽略）；
            # 换已有订单走 POST /orders/<单号>/sales-agent/。sales_user 留在这里是承重的：去掉它，请求体里的值会被 save() 写进订单。
            "sales_user",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            # 服务端动作专属列（见模块顶部的 `SERVER_WRITTEN_ORDER_FIELDS` 说明）
            *SERVER_WRITTEN_ORDER_FIELDS,
            # 专用端点专属列（见模块顶部的 `DEDICATED_ENDPOINT_ONLY_ORDER_FIELDS` 说明）
            *DEDICATED_ENDPOINT_ONLY_ORDER_FIELDS,
            # 软删 / 恢复端点专属列（见模块顶部的 `SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS` 说明）
            *SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS,
        ]

    def get_allowed_transitions(self, obj):
        """allowed_transitions 字段的取值口——薄包装、判据在模块级函数里。⚠️ 判据整段抽到 allowed_transitions_for(order, request)（模块级，定义在本类之前）：批量端点 batch/transition 的读取形态（spec §D10）也要「与订单详情同源」的可选目标集合；抽取行为一字未改——本方法只把 self.context.get("request") 原样传下去；⛔ 不许在这里「顺手」加任何判据（那会让两处再次分叉，正是 D5 要挡的）。
        ⚠️ 视图构造本序列化器时必须传 context={"request": request}：不传则 request 为 None、节点落到空集、allowed_transitions 会对所有人变空（静默丢功能）。⚠️ 判据、契约与例外那一整段说明逐字搬进了模块级函数的 docstring（含 COMPLETED 的提示项、REFUNDED 的出边、对拍网的名字）——⛔ 别在这里再抄一份，两处重复的说明必然漂移。"""
        return allowed_transitions_for(obj, self.context.get("request"))

    def get_completed_requires_review(self, obj):
        """allowed_transitions 里那一项「已完成」是否会被拒（true ⇒ 前端弹提示窗）。判据 = 它是被当提示项列出来的那一支：当前状态不是 PENDING_REVIEW（入 COMPLETED 的合法来源态按 order_status._COMPLETED_SOURCE 只有这一个；其余六态到 COMPLETED 的边都被 F3 删掉了）且 COMPLETED 确实出现在列表里。
        ⚠️ 订单已经是 COMPLETED 时列表里没有它（没有自环）⇒ 这里回 False。"""
        if str(obj.status) == str(OrderStatus.PENDING_REVIEW):
            return False
        if str(obj.status) == str(OrderStatus.COMPLETED):
            return False
        return str(OrderStatus.COMPLETED) in self.get_allowed_transitions(obj)

    def get_source_name(self, obj):
        """「订单来源」的显示值——薄包装，判据在模块级 `order_source_display_name`。"""
        return order_source_display_name(obj)

    def get_split_is_balance_anchor(self, obj):
        from apps.business import split_group

        return split_group.is_balance_anchor(obj)


class OrderDetailSerializer(OrderSerializer):
    penalty_parent_order_no = serializers.SerializerMethodField()
    penalty_children = serializers.SerializerMethodField()
    has_dailiantong_history = serializers.SerializerMethodField()
    can_continue_split = serializers.SerializerMethodField()

    def get_can_continue_split(self, obj):
        if obj.hall_card_id is not None or obj.upstream_hall_card_id is not None:
            return False
        if obj.status != OrderStatus.COMPLETED:
            return True
        legacy_anchor = None
        if obj.split_id:
            if obj.split_kind not in ("", split_group.LEGACY, split_group.PEER):
                return False
            members = split_group.active_members(obj.split_id)
            visible_members = self._visible_orders().filter(split_id=obj.split_id)
            if members.exclude(pk__in=visible_members.values("pk")).exists():
                return False
            anchor = members.first()
            if anchor is None:
                return False
            shipping_main = split_group.shipping_main_order(anchor)
            if shipping_main is not None:
                if shipping_main.hall_card_id is not None or shipping_main.upstream_hall_card_id is not None:
                    return False
                user = self.context["request"].user
                if shipping_main.game_id != obj.game_id and not games_of(user).filter(pk=shipping_main.game_id).exists():
                    return False
            if split_group.is_legacy_member(obj):
                legacy_anchor = anchor
        return not split_group.has_split_payment_lock([obj], legacy_anchor=legacy_anchor)

    def get_has_dailiantong_history(self, obj):
        return obj.dailiantong_pushes.filter(
            platform=PlatformIntegration.Platform.DAILIANTONG,
            state__in=(DailiantongPush.State.SENT, DailiantongPush.State.CLOSED),
            serial_no__isnull=False,
        ).exclude(serial_no="").exists()

    def _visible_orders(self):
        return scope_queryset(
            self.context["request"].user, search_orders(), scope_filter=game_scope("game_id"),
        )

    def get_penalty_parent_order_no(self, obj):
        if obj.penalty_approval_id is None:
            return None
        return self._visible_orders().filter(pk=obj.penalty_approval.original_order_id).values_list(
            "order_no", flat=True,
        ).first()

    def get_penalty_children(self, obj):
        return list(self._visible_orders().filter(penalty_approval__original_order_id=obj.pk).values(
            "order_no", "status",
        ))


class OrderUpdateSerializer(serializers.ModelSerializer):
    """编辑订单（PATCH）专用。金额、订单号/状态与归属人一律只读。见 spec §6：改价必须走 /price/ 这个能单独授权、单独留痕的动作——如果 PATCH 也能写金额，「改价留痕」在结构上就是假的。审计列（created_by/updated_by）不接受通用 PATCH 直接设置。
    ⚠️ 但这不是防伪造：P2 没有认证，视图里的操作人取自请求体的 actor（spec §10.1 #8 的既定做法），客户端本来就能自称任何人；只读的意义是「操作人只能由视图按一条明确规则写入」，而不是「不可伪造」；可信的操作人要等 P5 接上认证后从 request.user 取。"""

    class Meta:
        model = Order
        exclude = ["split_ship_main_order_no", "wecom_notice_journal", "wecom_notice_pending", "penalty_approval", "peer_direct_pending"]
        read_only_fields = [
            "order_no",
            "status",
            # ⚠️ 归属人通用 PATCH 一律不收：sales_user 决定客服提成的唯一归属（spec §3.8），通用 PATCH 不改归属：载荷里的 sales_user 是只读、不进 validated_data。
            # 录单默认按认证态写；持 order.allow_change_sales_agent 时用 CreateOrderAssignSerializer 收 sales_user。换已有订单走
            # POST /orders/<单号>/sales-agent/（同事务搬 SALES 绩效行、重算、留痕）。
            "sales_user",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            *MONEY_FIELDS,
            # 服务端动作专属列（见模块顶部的 `SERVER_WRITTEN_ORDER_FIELDS` 说明）
            *SERVER_WRITTEN_ORDER_FIELDS,
            # 专用端点专属列（见模块顶部的 `DEDICATED_ENDPOINT_ONLY_ORDER_FIELDS` 说明）
            *DEDICATED_ENDPOINT_ONLY_ORDER_FIELDS,
            # 软删 / 恢复端点专属列（见模块顶部的 `SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS` 说明）
            *SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS,
            # ⭐ ：`Order.order_label` 是**本地**标签（第三方平台订单页「更改标签」写它），
        # 不对外开放写入 ⇒ 放进只读，保持改单端点的 25 字段契约不变。
        "order_label",
        # ⭐（D-0356 查码队列）：qr_fetch_suppress_until 是查码通道的系统内部退避列（只由拉取 worker 按「任何非 ok 结局写 max(600s, retry_after)」的规则写），
        # ⛔ 第三方 PATCH 不许碰（放它进来 = 第三方能改小退避把队列打爆）——本序列化器是 exclude=[] + 只读白名单形状，每加一列都必须显式补这里
        #（漏补即默认可写；本条正是全量套件 test_openapi_order_update 契约网抓出来的）。
        "qr_fetch_suppress_until",
    ]


class OrderBalanceUpdateSerializer(OrderUpdateSerializer):
    """内部 PATCH 入参；领域层仅为拆分组非锚点开放真实改价。"""

    total_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        error_messages={
            "invalid": "金额格式不正确",
            "max_decimal_places": "金额最多两位小数",
        },
    )


class PriceChangeSerializer(serializers.Serializer):
    """改价入参。必须走序列化器——否则非法金额会变成 HTTP 500（见 Step 5）。金额可以为负（退款/冲抵/调整，业务方确认，见 D-0015）：这里不设 min_value（也不设模型校验器/CHECK），即没有符号约束。
    ⚠️ 判据不止「是不是合法的两位小数」——那句话曾写在这里、是过度声明、已更正（「点名一处、留下同类」正是 E-0010 的形状）。实际判据是四条：Decimal 可解析（invalid）、最多两位小数、总位数 ≤12、整数位 ≤10（DecimalField(max_digits=12, decimal_places=2)）；再叠上本字段的 required=True/allow_null=False（空串、省略、null 三种都是拒绝）与 DRF 的 MAX_STRING_LENGTH=1000。前端镜像在 src/frontend/src/config/money.ts（含逐路径空值口径：录单用的 OrderSerializer 的 total_price 由模型非空+default=Decimal("0.00") 生成、实测 required=False, allow_null=False——省略→由模型默认落 0.00，显式 null/空串/纯空白串都是拒绝，业务码 1000）。
    ⚠️ 别把「省略合法」读成「一律可空」：total_price 非空这件事只让空串/null 从「落 None」变成「被拒」、省略仍然合法；同一 OrderSerializer 上其余金额字段（received_price/platform_price/hall_price/fee）仍是 null=True, blank=True，对它们「省略/空串/null」才全部落 None（以上口径已对运行中的序列化器逐字段核过）。"""

    total_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        error_messages={
            "required": "缺少 total_price",
            "invalid": "金额格式不正确",
            "max_decimal_places": "金额最多两位小数",
        },
    )
    # ⚠️ max_length 必须与**审计落点** `OperationLog.reason` 的列宽一致：不设上限的话，
    # 超长 reason 会让审计行插入失败——而那时价格**已经落库**，于是变成
    # 「只改字段、没有留痕」，正是 spec §6 禁止的情形（且比不修更糟：原本 200 的请求会变 500）。
    reason = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=255
    )


class BatchPriceSerializer(PriceChangeSerializer):
    """POST /orders/batch/price/ 的 body：继承单笔的字段与校验、只多两个键。⛔ 不要复制 PriceChangeSerializer 的字段定义（E-0021 的形状：同一件事两套判据）。
    ⚠️ allow_empty=True 是刻意的：空数组的拒绝由 order_batch.parse_order_nos 给文案（spec 要求那句「请至少选择一笔订单」），⛔ 不要让 DRF 先回它自己的内置文案；同理不设 max_length（上限文案由 parse_order_nos 给）。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()
    confirmed_effects = BatchOrderEffectSerializer(many=True, required=False)


class PaymentStatusChangeSerializer(serializers.Serializer):
    """改收款状态入参（spec §5.1）。只有这一个字段：对标那个弹窗没有备注输入 ⇒ 审计行的 reason 留空。
    ⚠️ 与本端点同族的 /price/ 有 reason，那是既有契约（PriceChangeSerializer），别顺手对齐。"""

    payment_status = serializers.ChoiceField(
        choices=PaymentStatus.choices,
        error_messages={
            "required": "缺少 payment_status",
            "invalid_choice": "收款状态取值不正确",
        },
    )


class XgjReceiptSyncSerializer(serializers.Serializer):
    """POST /orders/xgj-receipt-sync/ 的 body：录单时间范围（两个日期）。计划 docs/plans/2026-10-26-receipt-sync-button.md §1.4/§2-3/§5：
    上限 31 天在前后端各算一遍：前端提交前算 (date_to - date_from).days + 1 <= 31，后端这里再算一遍（前端可绕过 ⇒ 后端是判据）。范围是 [date_from 00:00, date_to+1 00:00) 的半开区间，由领域模块 xgj_receipt_sync.window_bounds 算——⛔ 序列化器不碰时区。失败一律 ValidationError ⇒ 统一异常处理器包成 HTTP 200 + code=1000。"""

    #: 前后端共用的天数上限（含首尾两天）。
    MAX_DAYS = 31

    date_from = serializers.DateField(
        error_messages={
            "required": "缺少开始日期",
            "invalid": "开始日期格式不正确（应为 YYYY-MM-DD）",
        },
    )
    date_to = serializers.DateField(
        error_messages={
            "required": "缺少结束日期",
            "invalid": "结束日期格式不正确（应为 YYYY-MM-DD）",
        },
    )

    def validate(self, attrs):
        date_from = attrs["date_from"]
        date_to = attrs["date_to"]
        if date_from > date_to:
            raise serializers.ValidationError("开始日期不能晚于结束日期")
        days = (date_to - date_from).days + 1
        if days > self.MAX_DAYS:
            raise serializers.ValidationError(
                f"时间范围不能超过 {self.MAX_DAYS} 天（当前 {days} 天），请缩小时间范围"
            )
        return attrs


class SalesAgentChangeSerializer(serializers.Serializer):
    """换接单销售客服归属的入参（业务方拍板的新能力）。只有一个键、而且是目标用户的「主键」：
    user=接单销售客服换成谁（auth.User 主键）。⛔ 字段名照做、⛔ 不用 sales_user：后者是订单列名，而本端点收的是「目标用户」，且刻意与订单列同名会让「请求体里那个键能不能写订单列」这类问题反复出现（OrderSerializer 的 sales_user 仍只读）。⛔ 不收「名字」（没有 sales_agent 这个键）：名字由服务端从这个人算出来（order_writes.change_sales_agent 用与候选/录单兜底同源的显示名表达式），否则请求体可以送「甲的名字+乙的主键」⇒ Order.sales_agent（展示）与 Order.sales_user（提成归属）分叉，而两列都被同一页读着。
    min_value=1：本仓主键从 1 起、0/负数只可能是拼错的载荷；⚠️ 值域与 AssignSerializer.user_id（裸 IntegerField）不是同一档——差异只在拒绝发生在哪一层（这里由 DRF 字段校验回 1000），与 CreateOrderAssignSerializer.assignee 的说明逐字同源。"""

    user = serializers.IntegerField(
        min_value=1,
        error_messages={
            "required": "缺少 user",
            "invalid": "目标用户取值不正确",
            "min_value": "目标用户取值不正确",
        },
    )


#: 发单价「按接单价比例」的**百分比上界（含）**（spec §5.2 / C8）。
#: ⛔ **不要复用** `performance_amount.PERCENT_MAX`（= 100）：那是**扣点**比例的上限，
#: 借用会把 1000% 静默截成 100%（`tests/test_order_hall_price_api.py` 有一条专门的判别用例）。
HALL_PRICE_PERCENT_MAX = Decimal("1000")

#: `%` 档的语法：数字 + 可选小数 + **一个 `%` 收尾**。
#: 正负号 / `%` 不在收尾 / 多个 `%` 都不匹配 ⇒ 落金额档 ⇒ 解析失败 ⇒ 业务码 1000（spec §5.2 第 3 条）。
_HALL_PRICE_PERCENT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)%$")

#: 金额闸：与 `PriceChangeSerializer.total_price` 同形的四条判据
#: （可解析、≤2 位小数、总位数 ≤12、整数位 ≤10），**不设 `min_value`**（D-0015：0 与负数都合法）。
#: 借 DRF 的字段做判据而不是手抄位宽运算——手抄就是第二套口径。
_HALL_PRICE_AMOUNT_FIELD = serializers.DecimalField(max_digits=12, decimal_places=2)


def _hall_price_amount(raw):
    """金额档：过金额闸并量化到两位小数；不合法 ⇒ DRF `ValidationError`（业务码 1000）。"""
    try:
        return _HALL_PRICE_AMOUNT_FIELD.run_validation(raw)
    except serializers.ValidationError:
        raise serializers.ValidationError({"price": "发单价格式不正确"})


class HallPriceChangeSerializer(serializers.Serializer):
    """改发单价入参（spec §5.2）。刻意用 CharField：% 形态不是数字、用 DecimalField 连表达都表达不了。换算与解析（C8）：①空串和纯空白由字段层拒掉：price 是 CharField(allow_blank=False)，默认先去掉两端空白、两种输入都是 blank → 1000（不要再在 validate() 里加一道空串判断）；②匹配正则 ⇒ 比例档：pct > HALL_PRICE_PERCENT_MAX ⇒ 1000，否则 hall_price = commission_amount(Decimal(order.total_price), pct)——换算与舍入复用既有算式、不在视图里再写一遍 ×pct÷100（第二套算式会分叉）；③否则金额档：过 _hall_price_amount；④第②③步的结果再过同一道金额闸（换算结果撑爆 12 位时回 1000，不让数据库 numeric 溢出变 500）。
    ⚠️ 比例的分母是 Order.total_price（页面上显示为「接单价」的那一列）、不是 hall_price——「接单价/发单价」最容易抄反的地方（spec §5.2）；必须传 context={"order": order}：本序列化器要读分母才能折算。
    ⚠️ validate() 里覆盖 attrs["price"]（原样字符串→解析后的 Decimal）并额外塞一个 attrs["reason"]（% 档=服务端填的「按接单价 X% 折算」、金额档=""）是有意的：price 换成 Decimal 供视图直接落库、reason 供视图写留痕（% 的原始输入由服务端写进审计行的 reason，请求体不提供）；它们不是漏声明的字段——别在下一次「补全字段声明」时把它们改成真字段（reason 若成为真字段，请求体就能伪造「这个价是折算出来的」这条事实）。"""

    price = serializers.CharField(
        max_length=32,
        required=True,
        allow_blank=False,
        error_messages={"required": "缺少 price", "blank": "发单价不能为空"},
    )

    def validate(self, attrs):
        raw = attrs["price"].strip()
        order = self.context["order"]
        matched = _HALL_PRICE_PERCENT_RE.match(raw)
        if matched is not None:
            pct = Decimal(matched.group(1))
            if pct > HALL_PRICE_PERCENT_MAX:
                raise serializers.ValidationError({"price": "发单价比例不能超过1000%"})
            amount = commission_amount(Decimal(order.total_price), pct)
            # `%` 的原始输入由**服务端**写进审计行的 `reason`（请求体不提供，spec §5.2）。
            attrs["reason"] = f"按接单价 {raw} 折算"
        else:
            amount = _hall_price_amount(raw)
            attrs["reason"] = ""
        # 第 4 步：最终值再同一道闸（换算结果与直写金额走同一条判据）。
        attrs["price"] = _hall_price_amount(str(amount))
        return attrs


# ── 各批量 body 序列化器（spec §4.1；与 BatchPriceSerializer 同形）──
# ⚠️ 本节定义在各自单笔序列化器之后（继承需要基类已存在，Python 类体在 import 期求值）；serializers.py 里不能把它们集中到文件中部
#（PaymentStatusChangeSerializer 在 BatchPriceSerializer 之后、HallPriceChangeSerializer 更靠后）。⛔ 不写「本节共 N 个」这类计数：加一个就过期（E-0011）。其中 BatchPriceSerializer / BatchDeleteSerializer 紧跟各自的单笔基类就地定义，其余（本节的几个）基类都已在上面 ⇒ 能集中在这里。


class BatchPaymentStatusSerializer(PaymentStatusChangeSerializer):
    """POST /orders/batch/payment-status/ 的 body：继承单笔的字段与校验、只多两个键。⛔ 不要复制 PaymentStatusChangeSerializer 的字段定义（E-0021 的形状：同一件事两套判据）。
    ⚠️ allow_empty=True / 不设 max_length 的两条理由与 BatchPriceSerializer 逐字相同（空数组与上限的文案一律由 order_batch.parse_order_nos 给）。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()


class BatchHallPriceSerializer(HallPriceChangeSerializer):
    """POST /orders/batch/hall-price/ 的 body：继承单笔的字段与校验、只多两个键。⚠️ % 档的折算分母是每一笔自己的 Order.total_price（与单笔同口径）⇒ 本序列化器的 validate() 不能只跑一次：批量端点会对每一笔各跑一次（context={"order": 该笔}），见 OrderViewSet.batch_hall_price。
    ⛔ 不要在批量端点里另写一套 ×pct÷100（第二套算式会分叉）。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()


class BatchOrderTypeSerializer(serializers.Serializer):
    """POST /orders/batch/types/ 的 body。⛔ 只放 order_type（spec §9-R7）：批量不得成为「改订单别的字段」的第二条写通道。
    ⚠️ 取值域必须取真实枚举 OrderType.choices（与 OrderUpdateSerializer 的 order_type 字段同源）——写死 BOOST/COMPANION 会在枚举扩值时静默过期。
    ⚠️ 独立 Serializer（不继承 OrderUpdateSerializer）：后者有二十来个可写字段，在它上面「剔字段」要维护一份与模型同长的排除表、加一个模型字段就漏一个；这里只有三个键 ⇒ 「只放 order_type」是结构性保证。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()
    order_type = serializers.ChoiceField(choices=OrderType.choices)


def transition_reason_field() -> serializers.CharField:
    """状态流转「说明原因」字段的唯一声明出处（D-0337）。做成工厂而不是共享一个字段实例：DRF 字段在 bind() 时会往实例上写 field_name/root 等绑定状态，同一个实例被两个序列化器类同时声明会互相覆盖；每次调用返回新实例，上限/可空/trim 的口径仍只有这一处（⛔ 别改回 OrderTransitionReasonSerializer.declared_fields[...] 那种引用——DRF 3.18 的 declared_fields 不再挂在类上，本仓实测 AttributeError）。两个消费方：单笔 /transition/（OrderTransitionReasonSerializer）与批量 BatchTransitionSerializer.reason——上限写两份必然漂移，所以收进这一个函数。
    可空（required=False + allow_blank=True + 默认空串）：必填闸在前端（弹窗选中问题单/已暂停/已退单时拦「请输入原因」）；后端对任何目标态都宽松可选——与开放平台那侧的 remark（apps/openapi/api/order_write_views.py，「⛔ 不给任何目标态加必须给 remark 的分支」）同一裁决：同一个端点不能对「带与不带原因」的调用方在同一条边上给两种结果。
    ≤200：业务方的口径（⚠️ 不是对标的 50——对标 SetOrderSta 子页 input#remark 是 maxlength="50"，业务方给的本仓上限是 200）；200 < 列宽 OperationLog.reason 的 255 ⇒ 「请求里能填、库里静默截断」的组合不存在（同 openapi 那条的列宽同源判据）。trim：DRF CharField 默认 trim_whitespace=True 在 max_length 之前 strip()（含全角空白）⇒ 「trim 后 ≤200」自动成立、不需要自定义 validator；纯空白的 reason 经 trim 变空串 ⇒ 与「没传」同一个值，留痕行 reason 列为空串。"""
    return serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
        error_messages={"max_length": "说明原因最多 200 个字符"},
    )


class OrderTransitionReasonSerializer(serializers.Serializer):
    """状态流转「说明原因」的入参形状（单笔 /transition/，D-0337）。只有 reason 一个键：单笔 transition 的 target 仍由动作体裸读（它有自己「缺少 target ⇒ 1000+中文 msg」的既有契约、有测试钉着；挪进序列化器会换掉那句话与那个分支），
    本类只负责新加的 reason 的格式校验（口径见 transition_reason_field()）。"""

    reason = transition_reason_field()


class BatchTransitionSerializer(serializers.Serializer):
    """POST /orders/batch/transition/ 的 body：独立序列化器（spec §4.1 第 2 行/§D10）。⛔ 不继承任何东西：单笔 transition 今天没有序列化器（它裸读 request.data["target"]，缺 target 回 1000 由动作体自己给）⇒ 没有可继承的基类，也不许为了「统一」去继承一个有二十来个可写字段的编辑序列化器（那会开出第二条写通道）⇒ 本类的键恰好四个：order_nos / dry_run / target + ⭐ D-0337 起的 reason（见字段注释），此外不得再多。
    ⚠️ target 可选且 allow_blank=True：target 缺省（或被显式送成空串）= spec §D10 的读取形态（只回这批单可选目标的并集 ⛔ 一行不写）⇒ 那条请求的 dry_run 不参与判断：读取形态恒报 dry_run: True、applied: []，送 False 也一样（dry_run 仍是必填布尔、按 §D2 的统一契约校验格式）；判据是动作体里的 if not target:（真值判据在这里是对的："" 与「没传」同义、都是「读取形态」——与 E-0021 管的「0 与负数也是合法边界」不是同一类）。
    ⚠️ 取值域必须取真实枚举 OrderStatus.choices（与状态机表同源）：写死几个状态名会在枚举扩值时静默过期，而失效的表现是合法目标被拒（用户看不到原因）；⚠️ 非法 target 是请求级 1000（spec §4.2 的 transition 行）⛔ 不是逐笔 ineligible。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()
    target = serializers.ChoiceField(
        choices=OrderStatus.choices, required=False, allow_blank=True
    )
    # ⭐ D-0337：「更改订单状态」弹窗选「问题单 / 已暂停 / 已退单」时要求填「说明原因」，原因随两段式批量一路透传进每笔的留痕行（transition_order(remark=…)）。
    # ⚠️ 与单笔 /transition/ 用同一个字段口径（transition_reason_field()，每次返回新实例）：两处各写一份上限必然漂移，但本类 ⛔ 不继承任何序列化器
    #（上方 docstring 的结构性「恰好四个键」会被继承破坏）⇒ 字段声明经工厂收口在唯一出处。
    reason = transition_reason_field()


class BatchRecomputeSerializer(serializers.Serializer):
    """POST /orders/batch/recompute-commission/ 的 body（spec §4.1 第 9 行/§D15）。⛔ 独立 Serializer、不继承任何单笔序列化器：本端点是新能力、⛔ 没有单笔对应物（spec §D15 明写「本仓没有这个端点，也没有这个函数」）⇒ 没有可继承的基类，本类只有 order_nos / dry_run 两个键是结构性保证。
    ⚠️ order_nos 的 allow_empty=True / 不设 max_length 两条理由与 BatchPriceSerializer 逐字相同（空数组与上限的文案一律由 order_batch.parse_order_nos 给）。
    ⚠️ 没有动作参数：这颗按钮不带任何业务值（确认文案见 spec §5.5）⇒ 多出来的键由 DRF 静默忽略，不会造成「以为设上了」的错账。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()


class OrderNoteSerializer(serializers.Serializer):
    """「添加订单日志」的入参（POST /api/business/orders/<单号>/notes/）。⚠️ 必须过序列化器（E-0010：请求来源的值进类型化的列之前一律过序列化器），三条判据都不是风格问题：
    content 必填且非空白：空串/纯空白/省略 ⇒ 业务码 1000 + 中文 msg「订单日志内容不能为空」。⚠️ 空白判据走显式的 validate_content、不走 DRF 默认的 trim_whitespace（M-5 的二选一里选了「贴近对标」那条）：对标把 remark 原样入库——POST /OrderAjax/SaveOrderPublieMark {orderId, remark}（.tools/recon/order-view/sections/order-view.md §7），而 DRF CharField 默认 trim_whitespace=True 会静默 strip 前导/尾随空白、内容就不是"原样"了；故显式 trim_whitespace=False + validate_content：value.strip() 为空 ⇒ 同一句中文 1000、非空 ⇒ 逐字保留调用方给的串（含首尾空白）；error_messages 仍要显式给中文——前端「原样弹后端 msg」时 DRF 的英文 blank 文案会直接出现在中文界面上。
    content 的 max_length=255 = 审计落点 OperationLog.reason 的列宽：不设上限的话超长内容会在留痕落库时 DataError ⇒ HTML 500（E-0010 同族）。⚠️ 代价（如实登记，M-6 确认在位的兜底）：summary 是「添加订单日志：」+content，255 字的内容会让 summary 超过它自己的 255 列宽 ⇒ record() 的 _clip 会截断 summary（apps/audit/log.py）——这是刻意的兜底（截断总好过 500）；要「整句不被截断」就得把 content 上限压到 247，而那会偏离「上限按 reason 列宽取」这条口径（T1 任务的明文要求）故不改；判据：tests/test_order_detail_log_contract.py::test_content_exactly_at_the_column_width_is_accepted（断言 len(row.summary)==255 且以「添加订单日志：」开头）。actor：与既有写订单端点同口径（ActorSerializer 的 150 = User.username 上限）、只为挡住超长值 ⇒ 1000 而不是 500；⚠️ 它不落任何列：审计行的操作人一律取认证态（spec §5.9 的硬规矩「操作人一律取认证态…从不取请求体的 actor」，actor_id 同步取 request.user.pk），与 /transition/、录单的「请求体身份一律忽略」同一条规则（R20）。"""

    content = serializers.CharField(
        max_length=255,
        trim_whitespace=False,
        error_messages={
            "required": "订单日志内容不能为空",
            "blank": "订单日志内容不能为空",
            "max_length": "订单日志内容最多 255 个字符",
        },
    )
    actor = serializers.CharField(
        max_length=150, required=False, allow_blank=True, default=""
    )

    def validate_content(self, value):
        """纯空白也算空（对标前端 layer.msg('备注内容不能为空！') 的同义判据）；非空则原样返回。⚠️ 与 trim_whitespace=False 是一对：只判「去掉空白后是不是空」、判完不改写入参——对标把 remark 原样入库（.tools/recon/order-view/sections/order-view.md §7）。"""
        if not value.strip():
            raise serializers.ValidationError("订单日志内容不能为空")
        return value


class ThrowToHallSerializer(serializers.Serializer):
    """抛单入参。price 可不传（不传就用订单已有的默认接单价，spec §5）。金额可以为负（D-0015）：这里同样不设 min_value。"""

    price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )


class AssignSerializer(serializers.Serializer):
    """指派人入参。price 是可选的接单价覆盖；本批的三个新键见 spec §5.1。
    ⚠️ fee 与 price 不是同一件事（C7/C8）：price 写 Order.hall_price（接单价，是计算基数的输入）、fee 写 Performance.temp_amount（这个人的预计绩效）。⛔ 两个金额键 price/fee 不设 min_value（D-0015：0 与负数合法）；hours 设 min_value=1（对标的「时间要求(小时)」下界，见本类里 hours 那一行——原写「三个键都不设 min_value」与实现不符（E-0011；原先还带了一个行号引用，行号会随本文件增删而失效，故改为按字段名指认）；project 由 allow_blank=False + validate_project 拒空/纯空白。
    ⚠️ fee/hours/project 的 null 与「不传」同义（都表示「不写这一列」）——判据在视图里一律 is not None（E-0021）。"""

    user_id = serializers.IntegerField()
    chat_follow_up = serializers.BooleanField(required=False)
    price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    #: 覆盖 `Performance.temp_amount`（对标 `assignation.js` 的 `fee`，见 C7）。
    fee = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    #: `Performance.hours`（对标「时间要求(小时)」，下界 1 由这里保证、列上不设约束）。
    hours = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    #: Performance.project（对标「接手项目」）：空串由 allow_blank=False 拒，纯空白由 validate_project 拒（对标编辑期文案逐字）。
    #: ⚠️ 同名不同义：本键 = Performance.project（自由文本「接手项目」，每人一份）；录单页 OrderSerializer.project = Order.project，那是到 Project 的 FK（「代练/陪玩项目」）
    #: ⇒ 两个端点各写各的列、⛔ 不能互相搬值（E-0068 同族）。读侧的避碰见 C13 的 taker_project。
    project = serializers.CharField(
        # 900（E-0239）：与 `Performance.project` 列同宽；预填来自 `Order.project_text`（900）。
        max_length=900, required=False, allow_blank=False, allow_null=True
    )

    def validate_project(self, value):
        if not value.strip():
            raise serializers.ValidationError("项目要求不能为空，请输入")
        return value


class BatchAssignSerializer(AssignSerializer):
    """POST /orders/batch/assign/ 的 body：继承单笔的字段与校验、只多两个键。⛔ 不要复制 AssignSerializer 的字段定义（spec §4.1 第 1 行；同一件事两套判据必然分叉）；⚠️ allow_empty=True / 不设 max_length 的两条理由与 BatchPriceSerializer 逐字相同（空数组与上限的文案一律由 order_batch.parse_order_nos 给）。
    ⚠️ 请求级的 taker 存在性/停用与带 price/fee 时的两道节点闸（403）不在这里：它们在视图里、run_batch 之前各做一次（spec §D5）——⛔ 不进逐单循环，那会把授权语义（403）错译成数据不合格。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()


class ClearTakerSerializer(serializers.Serializer):
    """清除接单人入参（spec §5.2）：isDelEmp 只决定金额那一步。Y ⇒ 在既有回退之外把 temp_amount / bonus_amount 都归零（zero_performance_amounts）；
    N（或缺省）⇒ 只回退；其它取值 ⇒ 1000（ChoiceField 自己拒）。⚠️ 两档都把 hours / project 清空——那是 reset_taker_performance 的活，与 isDelEmp 无关。"""

    isDelEmp = serializers.ChoiceField(choices=("Y", "N"), required=False, default="N")


class OrderListSerializer(PriceRedactionMixin, serializers.ModelSerializer):
    """列表页只需要少量字段——不要用全字段序列化器拖垮列表。以下键演进全部是加法（详情/其它端点的字段语义一个没改；HallListSerializer 继承本类 fields，因此大厅列表跟着多键——口径一致）：
    ⚠️ source_order_no 必须在列表里（业务方）：管理订单页「订单号」列规则是「来源单号非空就显示来源单号，否则显示系统订单号」——列表拿不到它前端就只能恒显示 order_no（修前的形状：模型有、详情有、列表没有）。⚠️ source 也必须在列表里（业务方反馈「订单来源」列没正常显示）：管理订单页那一列读的就是它（模型上的名字快照）；修前形状同上，前端那列还被 templet 钉死。⚠️ taker_name / refund_fee 是 spec §5.3 的加法：taker_name 取 get_queryset() 注解的 TAKER_NAME（emp_type=MAIN 绩效行上的用户名）；refund_fee 是模型自己的两位小数金额。
    ⚠️ 第二批列（spec §5）给列表补了 9 个只读键（管理订单页那批列的取数出口）：game_role_name / customer_ww / hall_price（脱敏，见下）/ sales_agent / created_by / remark / completed_at 是模型列，net_received 与 hall_publisher_name 是派生键（各一个 SerializerMethodField，都在内存里算）。⚠️ 第 10 个键 time_requirement 是执行期补的（同一批）：spec §5 的表漏列了它，而 §7.3/决策 C5 要求管理订单页的「时间」列渲染它（录单页的「时间要求」自由文本；对标那列读的是服务端算的时长描述，我们没有）；契约清单的权威出处是 tests/test_order_list_columns.py 的 TEN_KEYS。⚠️ 本批再补两个键：payment_status（三档码）与 payment_method（自由文本，blank=True ⇒ 后端恒回字符串）——收款状态标记列要读前者、金额提示的「，支付方式：X」要读后者；都在价格脱敏面之外（脱敏只作用于 hall_price）。⚠️ hall_price 现在在本类的 fields 里（spec §5/C6：管理订单页的「发单价」列要读它）⇒ 脱敏项声明在本类的 REDACTED_PRICE_FIELDS 上——PriceRedactionMixin 只对「字段名在自己 fields 里」的项生效，声明在别处是空转；本类取的是 Order.hall_price（录单默认发单价），大厅那份值由 HallListSerializer 的声明字段覆盖成生效接单价（Ruling 44）。⭐ 钱包发薪线补 taker_settled——「发单价」列在金额前那个绿色「结」标记（打手绩效是否已结账）；取值 = get_queryset() 注解的 TAKER_SETTLED（相关子查询，无 N+1）、没有 MAIN 行 ⇒ False；原先 spec 决策 C6/§9-3 明确不做（载荷里没有结账状态），钱包发薪上线后（验收即自动结账）业务方要求补上 ⇒ C6 那一段作废；抛单次数与 recFee > price 转红仍未做（仍缺对应字段）。"""

    #: 发单价：被 `order.restrict_view_offer_price` 限制时读成 `None`（键仍在）。
    #: 大厅（子类）继承本元组即可——它的 `hall_price` 字段名同样在自己的 `fields` 里。
    REDACTED_PRICE_FIELDS = (("hall_price", can_view_offer_price),)

    #: 接单人用户名。取值优先级：没有 taker_name 注解（大厅）⇒ 空串；有注解且持 order.restrict_view_taker ⇒ 常量「无权查看」（照对标同一档的显示值，spec §9）；
    #: 有注解且未受限 ⇒ 注解值。大厅里的单本来就没有接单人，空串是真话、不是兜底，所以旗帜在那一档不改写它。
    #: ⚠️ 缺 request 时不脱敏：非请求路径（内部构造）丢字段是静默的功能损失，与 PriceRedactionMixin 的既有判据同形。
    taker_name = serializers.SerializerMethodField()

    #: 实收款（净实收额）= settlement_amount.net_received(order) 的两位小数字符串。⚠️ 必须调那个函数（spec §5 / C2「同一事实只有一处实现」）：
    #: 在这里重写一遍算式就是第二套算式，两处会静默分叉、且分叉的表现是算错钱。它是纯内存读——只读实例上已有的 total_price / refund_fee（算式不再读
    #: received_price，见 settlement_amount.net_received 的 docstring），不构造查询 ⇒ 没有 N+1。判别力见 tests/test_order_list_columns.py::NetReceivedPayloadTests::test_payload_delegates_to_the_shared_function（把函数换成桩，载荷必须跟着变）。
    net_received = serializers.SerializerMethodField()

    #: 发单人（最后一次发单动作的人：抛大厅或指派，D-0369，2026-11-03）。
    #: 没人发单过（既没抛过大厅、也没被指派过）/ 历史行 ⇒ 空串；⚠️ 读的是 FK 的**显示名** ⇒ 取数侧必须 `select_related("hall_publisher")`
    #:（`selectors.search_orders` / `search_hall` 都已加），否则一页 N 行就是 N 条查询。
    hall_publisher_name = serializers.SerializerMethodField()

    #: 该单打手绩效（emp_type=MAIN）是否已结账（钱包发薪线上线后补的「结」标记）。取值 = get_queryset() 注解的 TAKER_SETTLED（selectors.taker_settled_expression()，一次相关子查询、无 N+1）。
    #: ⚠️ 没有 MAIN 行 ⇒ False（大厅行 / 未接单 / P2 遗留单）：语义是「这张单没有可结账的打手绩效 ⇒ 未结账」（与 jidanpu_common 那句「缺 MAIN 行 ⇒ 没有已结账可言」同口径）。
    #: ⚠️ 取数侧没带注解时（大厅 search_hall / 回收站 / 财务「待归属」的 queryset）也回 False：那三处不消费这个键，但 HallListSerializer / RecycleBinListSerializer 继承本类 fields ⇒ 键必须恒在（缺键会让前端行映射读到 undefined，见回收站 RECYCLE_ROW_KEYS 那条教训）；判「注解在不在」而不是判值——与 get_taker_name 同一处置。
    taker_settled = serializers.SerializerMethodField()

    #: ⭐（平台推送读面）：「这单已抛到平台」的徽标 + 平台单号。null = 该单没有代练通（platform=10）推送行；否则是最近一条推送行的六个只读字段。
    #: 取值 = get_queryset() 预取的 LATEST_PLATFORM_PUSHES（selectors.latest_platform_push_prefetch，一次 Prefetch 取回整页 ⇒ 无 N+1）。
    #: ⚠️ 取数侧没挂预取时（大厅 / 回收站 / 待归属 / 详情）回 null：那几处不消费这个键，但子类继承本类 fields ⇒ 键必须恒在（与 taker_settled 同一条处置：判「预取在不在」而不是判值）。
    platform_push = serializers.SerializerMethodField()

    def get_fields(self):
        fields = super().get_fields()
        # ⚠️ 旗帜在每个序列化器实例上只解一次，不要在 get_taker_name 里逐行调 can_view_taker：那条判据落到一个没有缓存的节点查询上
        #（apps/rbac/permissions.py::has_node_now）⇒ 一页 20 行就是 20 条多余查询，?page_size=200 时 200 条。与 PriceRedactionMixin.get_fields() 同一处置。
        request = self.context.get("request")
        self._can_view_taker = request is None or can_view_taker(request.user)
        return fields

    def get_taker_name(self, obj):
        # 先判**注解在不在**（而不是判值）：大厅的 queryset 没有这个注解，而池内的单
        # 本来就没有接单人 ⇒ 空串是真话。
        if not hasattr(obj, TAKER_NAME):
            return ""
        if not self._can_view_taker:
            return "无权查看"
        return getattr(obj, TAKER_NAME) or ""

    def get_taker_settled(self, obj):
        """True = 该单的打手绩效行（emp_type=MAIN）已结账；其余一律 False。⚠️ 判注解在不在（不是判值）：大厅/回收站/待归属的 queryset 没有这个注解、而那三处本来就不消费这个键 ⇒ False 是真话、不是兜底；
        Exists 恒回布尔、不会 NULL，所以这里不需要区分「没注解」与「注解为假」之外的第三种形态。"""
        return bool(getattr(obj, TAKER_SETTLED, False))

    def get_platform_push(self, obj):
        """该单有效代练通推送行的只读摘要；没有推送行 ⇒ None。口径（业务方统一）：platform=10 的最新一笔（预取 queryset 已按 -created_at,-id 排好，rows[0] 就是最新）；⛔ 不再「活跃优先」。这与详情页 GET …/platform-push/（显式 -created_at,-id 的第一条）以及第三方页聚合的选行口径一致——父行状态/发单价/交易状态必须来自同一条。
        ⚠️ 判预取在不在（不是判值）：大厅/回收站/待归属/详情的取数侧不挂预取 ⇒ 那几处回 None（它们不消费这个键），⛔ 不在这里逐单查库（那会变成 N+1）。
        ⚠️ serial_no 在 failed/pending/unknown 档可能是 NULL（模型列 null=True）；对外形状按契约统一成字符串（没有单号 ⇒ 空串），⛔ 不要把它伪造成一个像单号的值。⚠️ price 是发平台价格（推送行自己的列，与「发单价」列同源）：管理订单页的「发单价」列在有推送时显示它（业务方：「发到代练通 3.00、列表却显示 0.00」）；它必须与 hall_price 吃同一道脱敏（order.restrict_view_offer_price）——否则受限角色能从这颗摘要读到被隐藏的报价 ⇒ None（键仍在，前端回退本地发单价/—）。缺 request 时不脱敏（与 PriceRedactionMixin 同口径）。"""
        rows = getattr(obj, LATEST_PLATFORM_PUSHES, None)
        if not rows:
            return None
        # ⭐ 统一口径：**最新一笔**（⛔ 不再活跃优先）。
        push = rows[0]
        # ⚠️ 脱敏判据**按请求算一次**再缓存：`can_view_offer_price` 会查 RBAC 节点，
        # 逐行调用就是 N+1（实测 1 单 → 5 单时 SQL 88 → 120，`test_query_count_does_not_grow…` 红）。
        if not hasattr(self, "_platform_push_price_visible"):
            request = self.context.get("request")
            self._platform_push_price_visible = (
                request is None or can_view_offer_price(request.user)
            )
        price_visible = self._platform_push_price_visible
        return {
            "platform": push.platform,
            "serial_no": push.serial_no or "",
            "state": push.state,
            "remote_status": push.remote_status,
            "remote_cancel_status": push.remote_cancel_status,
            "is_active": push.is_active,
            # 两位小数字符串（与 `PlatformPushRowSerializer.price` / 其余金额键同形）。
            "price": None if not price_visible else f"{push.price:.2f}",
        }

    def get_net_received(self, obj):
        """出参形状与 selectors.order_list_summary 的金额一致：两位小数字符串。net_received() 返回的是 Decimal（已 quantize 到分），这里只做格式化、
        不做任何算术语义（算式不许在本文件出现第二遍）。"""
        return f"{settlement_amount.net_received(obj):.2f}"

    def get_hall_publisher_name(self, obj):
        """FK 为空（没抛过单/历史行）⇒ 空串；null FK 读取本身不查库。⚠️ 取显示名（业务方要求「发单人展示账号名称而不是账号本身」）：昵称优先、空回落登录账号，
        单一口径 apps/accounts/display.py::user_display_name——与接手人那格的表达同族，⛔ 不在这里写第二套「怎么显示一个人」。
        判据是 is not None（与本文件的金额边界口径一致）：User 没有 falsy 形态，但这条边界写成真值就是在赌「将来不会有人给它加 __bool__」。"""
        publisher = obj.hall_publisher
        # 判据是 `is not None`（与本文件的金额边界口径一致）：`User` 没有 falsy 形态，
        # 但这条边界写成真值就是在赌「将来不会有人给它加 `__bool__`」。
        return user_display_name(publisher) if publisher is not None else ""

    class Meta:
        model = Order
        fields = [
            "order_no", "source_order_no", "source", "status", "game", "server",
            "project_text", "total_price", "customer_phone", "is_urgent",
            "created_at", "refund_fee", "taker_name",
            # 第二批列（spec §5）：7 个模型列 + 2 个派生键（派生键也要列进来，
            # 否则 DRF 会静默丢掉声明字段）。
            "game_role_name", "customer_ww", "hall_price", "sales_agent",
            "created_by", "remark", "completed_at",
            # 第 10 个（ 执行期补，spec §5 原表漏列）：管理订单页「时间」列要读它。
            "time_requirement",
            # 本批（价格与收款状态）：收款状态码 + 支付方式（自由文本）。
            # 都是**加法**：`HallListSerializer` 继承本类 `fields`、财务「待归属」清单用本类
            # ⇒ 大厅与待归属跟着多这两个键（与 `source` / `taker_name` 同族的已登记加法）。
            "payment_status", "payment_method",
            "net_received", "hall_publisher_name",
            # ⭐ （钱包发薪线）：「发单价」列的绿色「结」标记（打手绩效是否已结账）。
            # **加法**：大厅 / 回收站 / 待归属跟着多这一个键（与 `source` / `taker_name` 同族），
            # 那三处取数侧没有注解 ⇒ 恒 `False`（见 `get_taker_settled`）。
            "taker_settled",
            # ⭐ ：这单已抛到平台（代练通）的只读摘要；没有推送行 ⇒ `null`。
            # **加法**：大厅 / 回收站 / 待归属跟着多这一个键（与 `taker_settled` 同族），
            # 那几处取数侧没有预取 ⇒ 恒 `None`（见 `get_platform_push`）。
            "platform_push",
        ]


class OrderSplitListSerializer(OrderListSerializer):

    """订单列表（GET /orders/）的载荷：在基类那串键之后追加拆单两键+来源显示名+徽标键。
    ⚠️ 为什么另起一个子类、而不是直接往 OrderListSerializer 加键——这是本线对 spec §2.4「OrderListSerializer 追加两个键」的执行期口径，理由是可重跑的：基类那份 fields 被三个别的读面继承/复用，它们都不消费这些键，而其中两处各有一条恰好相等的键表判据（逐条按事实写，⛔ 别把两处的形状混起来）：回收站 RecycleBinListSerializer ⇒ tests/test_order_recycle.py 的 RECYCLE_ROW_KEYS（载荷自身的「恰好集合」网，多一个键就红）；抢单大厅 HallListSerializer ⇒ 加进基类会同时改原生大厅载荷的键集合（那一页不读这些键），并且必须同批给 apps/business/jidanpu_common.REMOTE_ROW_BASE（上游鸡蛋铺镜像行的键表——那些行是普通 dict、根本不过序列化器）也加上这些键，不加就红的两条判据是 tests/test_jidanpu_upstream_hall.py::test_the_two_streams_share_exactly_one_row_shape 与 tests/test_jidanpu_upstream_pull.py（拿 set(HallListSerializer.Meta.fields) 与 set(REMOTE_ROW_BASE) | MERGE_ADDITION_KEYS 对拍）；⚠️ 那两条恰好相等量的是「镜像行↔序列化器字段集」、大厅载荷自身并没有「恰好集合」网（tests/test_order_list_columns.py 对大厅是「缺键集合为空」那种包含式）⛔ 别顺手把它写成「大厅也是恰好集合」；财务「待归属」清单（UnattributedView）用的是同一个基类（那一侧同样没有恰好集合网）。业务方那句「尽量少动原有结构」在这三处就是：不给它们加它们不读的键 ⇒ 拆单两键只出现在订单列表这一条读面上（落点 = OrderViewSet.get_serializer_class 的 list 分支，与取数层的折叠同一条路）。
    ⚠️ 两键一律追加在末尾（既有前端与「键集合」网的纪律）；split_id = 空串表示不是拆单，split_members = 组内成员总数（非拆单 = 0，见 selectors.split_members_expression()——唯一取值口、本类不自己算）。⭐（来源字典当前名）再追加第 3 个键 source_name——管理订单页「订单来源」列的显示值（挂字典 ⇒ 当前名，否则 ⇒ source 快照）；它同样只属于订单列表这一条读面：上面那三个消费方都不显示这一列 ⇒ 加进基类会改它们的载荷契约、逼着同批改两处「恰好集合」键表（RECYCLE_ROW_KEYS / REMOTE_ROW_BASE），而那正是「尽量少动原有结构」要避免的；⚠️ 键序摆在 split_id 之前：拆单两键仍在末尾（tests/test_order_list_columns.py::SplitKeysAreOrderListOnlyTests 那条「两键在末尾」用例钉着）。⭐（业务方）追加 order_type（陪玩单徽标）与 tag（标记徽标）：两者都是 Order 的真列（无派生、无额外查询），同样只加在订单列表这一条读面上（大厅/回收站/待归属各有自己的「恰好集合」键表，它们不显示这两个徽标 ⇒ ⛔ 不动基类、也不动它们）；键序同样摆在 split_id 之前（拆单两键必须仍在末尾）；列表载荷的键集合从本线起由 tests/test_order_list_columns.py::OrderListExactKeysTests 按恰好集合钉住（此前只有「缺键集合为空」那半边）。⭐（业务方）再追加 taker_is_platform——该单接手人是不是代练通的占位账号（前端「接手人」列据此把文字转紫）；判据按固定 username（取值口 selectors.taker_is_platform_expression()），⛔ 不按 taker_name 的串相等判；同样只加在订单列表这一条读面 ⇒ 键序摆在 split_id 之前（拆单两键仍在末尾）。"""

    #: 组内成员总数：取值口是取数层那条 SQL 注解（`SPLIT_MEMBERS`，`selectors.py`），
    #: 这里只声明它。⚠️ 只在**订单列表**的取数链上存在该注解（本类也只服务那一条读面）。
    split_members = serializers.IntegerField(read_only=True)
    split_kind = serializers.CharField(read_only=True)

    #: ⭐ D-0357（G16）：组内已完成数（v2 组卡「拆单组 · N 单 · 完成 X/N」的 X）。取值口 = SPLIT_COMPLETED_MEMBERS 注解
    #:（selectors.split_completed_members_expression()，与 split_members 同形）；非拆单 = 0。同样只加在订单列表这一条读面，摆在既有拆单键之后。
    split_completed_members = serializers.IntegerField(read_only=True)

    #: ⭐ D-0357（G16）：**本行是否组内主单**（组内未软删成员里 `id` 最小者；
    #: 非拆单行**恒 `False`**，S7）。取值口 = `SPLIT_IS_MAIN` 注解
    #: （`selectors.split_is_main_expression()`）。给 v2 组卡的折叠代表行判据。
    split_is_main = serializers.BooleanField(read_only=True)
    split_is_balance_anchor = serializers.BooleanField(read_only=True)

    #: ⭐ 「订单来源」的显示值（挂字典 ⇒ 当前名，否则 ⇒ `source` 快照）。
    #: 取值口是模块级 `order_source_display_name`（**与详情同源**）；FK 由
    #: `OrderViewSet.get_queryset` 的 `select_related("order_source")` 预取（挡 N+1）。
    source_name = serializers.SerializerMethodField()

    def get_source_name(self, obj):
        """薄包装，判据在模块级 `order_source_display_name`（⛔ 别在这里另写一份）。"""
        return order_source_display_name(obj)

    #: 录单人的显示名（账号快照 → 昵称；查不到人时回落账号本身）。
    #: ⭐（业务方）：「录单人」列要显示账号名称（如「小九」），不是登录账号（手机号 / xr138…）。created_by 是用户名快照、不是外键 ⇒ 用
    #: display_names_for_usernames() 一页一次批量解析（⛔ 不逐行查库；口径单一出处 apps/accounts/display.py）。⚠️ 只在订单列表这一条读面（与下面拆单两键同一处置）：基类那份 fields 被大厅 / 回收站 / 待归属继承，加进基类会破它们的「恰好键集」网（见类 docstring 的理由）。
    created_by_name = serializers.SerializerMethodField()

    #: ⭐（业务方）：该单接手人是不是代练通的占位账号（订单列表「接手人」列靠它决定文字转紫；占位账号的显示名从「代练通打手」改成「代练通」）。
    #: 取值 = get_queryset() 注解的 TAKER_IS_PLATFORM（selectors.taker_is_platform_expression()：Exists(MAIN 行 ∧ user.username == 固定占位用户名)）——按 username 判，
    #: ⛔ 不按 taker_name 的串相等判（显示名可改、旧名有存量，串判会静默漏）。⚠️ 只在订单列表这一条读面（与 source_name / 拆单两键同一处置）：大厅 / 回收站 / 待归属不显示这一列，加进基类会破它们的「恰好键集」网。
    taker_is_platform = serializers.SerializerMethodField()

    def get_taker_is_platform(self, obj) -> bool:
        """True = 接手人是代练通占位账号；其余（含没有 MAIN 行）一律 False。⚠️ 与 get_taker_name 同一道权限闸：查看者持 order.restrict_view_taker 时名字已被脱敏成常量「无权查看」
        ⇒ 这里也必须回 False，否则前端会给「无权查看」三个字上紫色、并泄露「这单是平台占位账号接的」；⚠️ 判注解在不在（不是判值）：取数侧没有这个注解时回 False，与 taker_settled 同一处置。"""
        if not self._can_view_taker:
            return False
        return bool(getattr(obj, TAKER_IS_PLATFORM, False))

    def get_created_by_name(self, obj) -> str:
        context = self.context
        # ⚠️ 判**键在不在**，⛔ 不判 `if not cache`：查不到的账号不进返回值
        # （`display_names_for_usernames` 的既定口径）⇒ 整页都是「开放平台应用 X」这类
        # 非 username 快照时，`not cache` 恒真、每行重跑一次批量查询（N+1）。
        if "_created_by_name_cache" not in context:
            page = getattr(getattr(self, "parent", None), "instance", None) or [obj]
            names: dict[str, str] = {}
            if page:
                names = display_names_for_usernames(
                    {getattr(row, "created_by", "") for row in page}
                )
            context["_created_by_name_cache"] = names
        return context["_created_by_name_cache"].get(obj.created_by or "", "") or (obj.created_by or "")

    class Meta(OrderListSerializer.Meta):
        fields = [
            "created_by_name",
            *OrderListSerializer.Meta.fields,
            "source_name",
            # ⭐ 陪玩单 / 标记两个徽标要读的真列（只加在订单列表这一条读面）。
            "order_type",
            "tag",
            # ⭐ 新老客标签（录单快照）与「是否手动指定」——「旺旺/来源」列最左侧的「新/老」标签读它。
            # 只加在订单列表这一条读面（与 order_type/tag 同一处置：基类被大厅/回收站/待归属复用）。
            "customer_kind",
            "customer_kind_is_manual",
            # ⭐ 接手人是不是代练通占位账号（「接手人」列转紫的判据）。
            "taker_is_platform",
            "split_kind",
            "split_id",
            "split_members",
            # ⭐ D-0357（G16）：组内已完成数 + 本行是否主单（v2 组卡两键）。
            # 仍摆在**末尾**（既有拆单键的「键序在末尾」纪律照旧）。
            "split_completed_members",
            "split_is_main",
            "split_is_balance_anchor",
        ]
        #: 八个都是**输出**键（列表不接请求体）⇒ 与基类的形状声明同一口径。
        read_only_fields = [
            "source_name", "order_type", "tag", "taker_is_platform",
            "customer_kind", "customer_kind_is_manual",
            "split_kind", "split_id", "split_members", "split_completed_members", "split_is_main",
            "split_is_balance_anchor",
        ]


class OrderPenaltyListSerializer(OrderSplitListSerializer):
    is_penalty_child = serializers.BooleanField(read_only=True)
    penalty_child_count = serializers.IntegerField(read_only=True)
    penalty_parent_order_no = serializers.CharField(read_only=True, allow_null=True)

    class Meta(OrderSplitListSerializer.Meta):
        fields = [
            *OrderSplitListSerializer.Meta.fields, "is_penalty_child", "penalty_child_count", "penalty_parent_order_no",
        ]


class RecycleBinListSerializer(OrderListSerializer):
    """回收站列表：订单列表字段 + 删除时间/删除人（spec §5.2/§7.3 第 10、11 列）。⚠️ 为什么另起一个类、而不是给 OrderListSerializer 加两列：那会让订单列表（GET /orders/）也多出这两个键、改变既有载荷契约（前端已按既有键集写；多出来的键在那一页没有任何语义）；回收站是唯一需要这两列的读端点、所以专用序列化器只服务它。
    ⚠️ 两列都只读：本类只用于列表输出（many=True，不接请求体），read_only_fields 在这里是形状声明——它同时挡住「将来有人拿它当入参序列化器」这条尚未存在的路径，与 SOFT_DELETE_ENDPOINT_ONLY_ORDER_FIELDS 的口径一致。⚠️ deleted_at 由 DRF 的 DateTimeField 按 iso-8601 输出（settings 未设 DATETIME_FORMAT）；deleted_by 是 CharField 原样输出；判据钉逐字相等（tests/test_order_recycle.py::RecycleBinListTests），不是「键在不在」。
    ⚠️ tag / game_account 只加在本子类的 fields 上（⛔ 不动 OrderListSerializer.Meta.fields）：回收站页的「订单号」徽标读标记（Order.tag）、「角色信息」列读游戏账号（Order.game_account），而订单列表/大厅列表两份载荷都不读它们 ⇒ 加进基类会改变那两处既有的载荷契约（与上面「为什么另起一个类」同一条理由的反面）；⚠️ 这两个键曾经漏加过：前端行映射照读它们而载荷不回 ⇒ escapeHtml(undefined) 在 layui 的 parseData 里抛、整张表一行都渲染不出来（T6 抓到的致命缺陷）；本类的键集合由 tests/test_order_recycle.py::RecycleBinListTests::test_the_bin_row_key_set_is_exactly_the_promise 按恰好集合钉住——那一端才是能挡住「前端多读一个键」的形状。⭐ 新增 deleted_by_name（删除人的账号名称）：取值 context["deleted_by_names"]（{登录账号: 显示名}）里查得到就用它、查不到 ⇒ 回落行上的 deleted_by（账号快照本身）；⛔ 本方法一次库都不查：映射由 OrderViewSet.recycle_bin 对本页批量解析后塞进 context（一页一次查询），在 SerializerMethodField 里 User.objects.get(...) 是 N+1（一页 200 行就打 200 次库，见 apps/accounts/display.py::display_names_for_usernames）；deleted_by 键保留（加法，旧前端不受影响）：一个用于显示、一个用于身份追溯。"""

    #: 删除人的**账号名称**（昵称优先、空串回落登录账号）；查不到 ⇒ 回落 `deleted_by`。
    #: ⚠️ 只读、且**不查库**：映射来自 `context["deleted_by_names"]`（见类 docstring）。
    deleted_by_name = serializers.SerializerMethodField()

    def get_deleted_by_name(self, obj):
        """`context` 里有映射就用它，否则回落 `obj.deleted_by`（⛔ 这里一次库都不查）。"""
        names = self.context.get("deleted_by_names") or {}
        return names.get(obj.deleted_by, obj.deleted_by)

    class Meta(OrderListSerializer.Meta):
        fields = [
            *OrderListSerializer.Meta.fields,
            "deleted_at",
            "deleted_by",
            "deleted_by_name",
            "tag",
            "game_account",
        ]
        # ⚠️ 声明要与 `fields` 的形状一致（本类只输出）：`tag` / `game_account` /
        # `deleted_by_name` 与 `deleted_at` / `deleted_by` 一样都是**只读**列，
        # ⛔ 不能让它们只出现在 `fields` 里。
        read_only_fields = [
            "deleted_at", "deleted_by", "deleted_by_name", "tag", "game_account",
        ]


class HallListSerializer(OrderListSerializer):
    """抢单大厅的列表序列化器：订单列表字段 + 接单价 hall_price（Ruling 34）。⚠️ 本类不再往 fields 里追加 hall_price：spec §5/C6（第二批列）起基类的 fields 已经有这个键（管理订单页的「发单价」列要读它）；本类的职责只剩改它的取值来源——下面那个声明字段覆盖基类的模型字段，再 append 一次会让 fields 出现重复项。而大厅确实必须看见接单价——代练员在大厅挑单、那是他唯一关心的数，大厅又支持按价格区间筛，「能筛却看不见」是残缺。
    ⚠️ hall_price 取的是「生效接单价」、不是 Order.hall_price（Ruling 44）：值来自 search_hall 注解的 effective_taker_price（=绩效覆盖价优先，为空才用订单默认价，spec §6.3）；口径必须与价格区间筛选同源，否则「筛出来的价」与「看到的价」会分叉；对外 key 仍是 hall_price（spec §5 承诺的字段名，前端契约不变）——改的只是它的取值来源。
    ⚠️ hall_price 按 order.restrict_view_offer_price 脱敏：脱敏项由基类 OrderListSerializer.REDACTED_PRICE_FIELDS 继承而来（hall_price 现在在基类的 fields 里 ⇒ 那个声明真的生效）、被限制时读成 None；因此大厅的构造点必须带 context={"request": request}（order_views.hall 已带）——不带就等于没脱敏。"""

    #: 抛单价：被 `order.restrict_view_offer_price` 限制时读成 `None`。
    #: ⚠️ **不在这里重复声明**：基类已声明同一个键（`hall_price` 在基类 `fields` 里 ⇒
    #: 生效），两处各写一份是「同一事实两处实现」。本类只覆盖**取值来源**（下面那个字段）。
    hall_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True,
        source=EFFECTIVE_TAKER_PRICE,
    )

    #: ── 两源合并的加法键（spec §4.6）──⚠️ 原生单的既有键与取值一个都不许改：这 7 个键在原生行上是「有键、空值/缺省值」，老前端不读它们 ⇒ 行为不变。
    #: 镜像行走 jidanpu_hall.serialize_remote_row，它的键表与下面这 7 个 + 基类 fields 恰好相等（ 的用例钉住）。
    origin = serializers.SerializerMethodField()
    source_system = serializers.SerializerMethodField()
    remote_order_id = serializers.SerializerMethodField()
    remote_order_view = serializers.SerializerMethodField()
    remote_game_area = serializers.SerializerMethodField()
    remote_game_server = serializers.SerializerMethodField()
    remote_hours_desc = serializers.SerializerMethodField()

    def get_origin(self, obj) -> str:
        return "native"

    def get_source_system(self, obj) -> str:
        return ""

    def get_remote_order_id(self, obj) -> str:
        return ""

    def get_remote_order_view(self, obj) -> str:
        return ""

    def get_remote_game_area(self, obj) -> str:
        return ""

    def get_remote_game_server(self, obj) -> str:
        return ""

    def get_remote_hours_desc(self, obj) -> str:
        return ""

    #: ⭐ 上家鸡蛋铺线（客服个人绑定）第三路的镜像行主键（ 的集成补丁）。取值：原生行恒 None；P2 镜像行恒 None；本线镜像行 = JidanpuUpstreamHallOrder.pk（由 jidanpu_upstream.serialize_upstream_row 填）。
    #: ⚠️ 为什么必须在共享键表里：本线接单端点是 POST /api/business/hall/upstream/<int:pk>/claim/（<int:pk> = 本线镜像表主键），而大厅行原先没有任何主键 ⇒ 前端拿不到那个 <pk>、那颗「接手此单」结构上点不动（ 的用例直接拿 self.mirror.pk 拼 URL，所以这个缺口单侧全绿、静默）。
    #: 业务方/编排方裁定：加这一个加法键；⛔ 只给本线路加是错的——那会破坏「三路恰好同键」，所以三路都给：另两路回 None。⚠️ 老前端不读它 ⇒ 行为不变；但它确实改了已上线两路的载荷（新增一个 null 键），登记在 的交付说明里。
    mirror_pk = serializers.SerializerMethodField()

    def get_mirror_pk(self, obj) -> None:
        """原生行不是镜像行 ⇒ 恒 `None`（**不是 0、不是空串**：那两种值会被前端当成一个真主键）。"""
        return None

    #: ⭐⭐ D-0296 的四个加法键——原生行这一份的取值（镜像/模拟单/灰行各按自己的来源填，键表仍恰好同形，对拍网 tests/test_jidanpu_upstream_hall.py::test_the_two_streams_share_exactly_one_row_shape）：
    #: is_preferred：这行是不是优选。原生行 = 注解 effective_taker_price 的原始值（⛔ 脱敏前——脱敏只改读出的值、不改价本身）> 200（阈值出处 hall_preferred.PREFERRED_PRICE_THRESHOLD）；
    #: is_exited / exit_kind：原生可接行恒 False / None（灰卡来自快照，不经过本序列化器，见大厅读口）；sim_pk：模拟单行主键（HallSimulatedOrder.pk），照 mirror_pk 同一条「行身份键、非本路恒 None」的先例，原生行恒 None。
    is_preferred = serializers.SerializerMethodField()
    is_exited = serializers.SerializerMethodField()
    exit_kind = serializers.SerializerMethodField()
    sim_pk = serializers.SerializerMethodField()

    def get_is_preferred(self, obj) -> bool:
        from apps.business import hall_preferred

        return hall_preferred.is_preferred_price(
            getattr(obj, EFFECTIVE_TAKER_PRICE, None)
        )

    def get_is_exited(self, obj) -> bool:
        """原生**可接行**必然还在厅（大厅入选条件）⇒ 恒 `False`。"""
        return False

    def get_exit_kind(self, obj) -> None:
        return None

    def get_sim_pk(self, obj) -> None:
        """原生行不是模拟单 ⇒ 恒 `None`（同 `get_mirror_pk` 的口径）。"""
        return None

    class Meta(OrderListSerializer.Meta):
        # `hall_price` 已在基类 `fields` 里；这里保持一份**拷贝**（而不是再 append），
        # 避免 `fields` 出现重复项。
        # 7 个合并键摆在最后、基类那段一个字不动（两源合并是**纯加法**）。
        fields = [
            *OrderListSerializer.Meta.fields,
            "origin", "source_system", "remote_order_id", "remote_order_view",
            "remote_game_area", "remote_game_server", "remote_hours_desc",
            # ⭐ 第三路接单要用的镜像行主键（ 集成补丁；三路同键 ⇒ 另两路回 `None`）。
            "mirror_pk",
            # ⭐⭐ D-0296的四个加法键（取值见上面各字段的注释）。
            "sim_pk", "is_preferred", "is_exited", "exit_kind",
        ]


class PerformanceSearchSerializer(serializers.Serializer):
    """绩效域查询参数的基类（照 P2 _search_params 的纪律：校验必须在入口做）。⚠️ 别直接把新参数加在这里：SettlementSearchSerializer 从本类继承，而结算列表不应用绩效列表的对标筛选（P4FU-C 起，绩效列表/导出用下面那个子类 PerformanceListSearchSerializer）；加在这里会让 /settlements 静默忽略它们（OrderSearchBaseSerializer 的 docstring 记了同一形状的教训）。
    约定（与 P2/P3a 一致）：空串按「没传」（前端「没选」常送 ?emp_type=）——由调用方在入口过滤掉空串；非法值 ⇒ DRF ValidationError ⇒ 200+code 1000（不是 HTML 500，E-0010）；user 用 min_value=1：?user=0 这种「看起来像 id 的边界」在入口就拒、不要让它变成长度为 0 的结果集（P3a 对 ?game=0 的同类处置）。"""

    user = serializers.IntegerField(min_value=1, required=False)
    emp_type = serializers.ChoiceField(choices=EmpType.choices, required=False)
    review_status = serializers.ChoiceField(choices=ReviewStatus.choices, required=False)
    settle_status = serializers.ChoiceField(choices=SettleStatus.choices, required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)


#: 逗号串参数一次最多接受多少个值（customer / username 两个「批量搜人」的参数共用）。依据：绩效列表一页 20 行、EnvelopePagination.max_page_size 是 200
#: ⇒ 一次筛指向 50 个人已经远超「看着列表挑人」的量级；同时把 URL 长度与 IN (...) 的绑定参数规模挡在安全区（同类「超长列表⇒绑定参数上限⇒HTML 500」的风险见 SettleRequestSerializer）。
#: 超限一律 200 + code 1000：⛔ 不静默截断——截断会让「搜了 80 个人」表现得像「只找到 50 个」，与导出那条「绝不静默截断」的教训同族。
MULTI_VALUE_MAX = 50

#: 逗号串参数的整串长度上限。50 个用户名 × 平均约 10 字符 + 分隔符 ≈ 550；1500 留足余量，同时挡住「一个几 KB 的查询串」（不经校验的请求来源值不直进查询，E-0010）。
#: ⚠️ 这个判据必须在 CommaSeparatedListField.to_internal_value 里判原始字符串，⛔ 不能靠 CharField(max_length=...) / MaxLengthValidator（理由见那个字段的 docstring）。
MULTI_VALUE_MAX_LENGTH = 1500


class CommaSeparatedListField(serializers.CharField):
    """逗号分隔的多值查询参数：出参是去空白、去空项后的 list[str]。⚠️ 空串/只有分隔符的串 ⇒ []（调用方按「没传」处理）：前端「没选」常送 ?customer=，与既有「空串=没传」的口径一致（search_params 已在入口剔除整串空串，这里兜的是 ?customer=,, 这种）；⚠️ 超个数 ⇒ 1000（MULTI_VALUE_MAX 的依据见那里的注释）；⚠️ 超整串长度 ⇒ 1000（MULTI_VALUE_MAX_LENGTH）。
    ⚠️ 为什么长度判据写在 to_internal_value 里、而不是 max_length= / MaxLengthValidator（评现实测：写 max_length= 时 ?username= + 1600 个 x 仍然 200+code 0）：DRF Field.run_validation 的次序是「先 to_internal_value(data)、后 run_validators(value)」，而 CharField.__init__ 把 max_length 装成的是 MaxLengthValidator（进 run_validators）；本字段的 to_internal_value 返回的是 list ⇒ 那个校验器数的是列表元素个数（0…50，早被 too_many 挡死）、原始字符串长度从头到尾没被检查过；所以长度只能在这一步、对 raw 字符串判；⛔ 不要「顺手」把 max_length= 加回来：它只会重新制造一条「看起来在挡、实际没挡」的假保证（E-0011 家族：能被一个反例证伪的绝对句/死配置比不写更糟）。
    ⚠️ 出参类型是 list 而不是字符串：过滤表达式直接吃 __in=，不在视图里再 split 一次（「校验口」与「拼查询」两处各拆一次必然漂移）。"""

    default_error_messages = {
        "too_many": f"一次最多筛选 {MULTI_VALUE_MAX} 个值",
        "too_long": f"该参数的整串长度最多 {MULTI_VALUE_MAX_LENGTH} 个字符",
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("allow_blank", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        raw = super().to_internal_value(data)
        # ⚠️ 先判**原始字符串**长度，再切分（理由见类 docstring 的「为什么不是 max_length」）。
        if len(raw) > MULTI_VALUE_MAX_LENGTH:
            self.fail("too_long")
        items = [item.strip() for item in raw.split(",")]
        items = [item for item in items if item]
        if len(items) > MULTI_VALUE_MAX:
            self.fail("too_many")
        return items


class PerformanceListSearchSerializer(PerformanceSearchSerializer):
    """绩效列表/导出记录在基类六条之外新增的查询参数（P4FU-C，brief §2 B 的参数表）。⚠️ 为什么另开一个子类、而不是直接往 PerformanceSearchSerializer 里加：SettlementSearchSerializer 从基类继承，而结算列表（SettlementViewSet.get_queryset）不应用这些新参数；加在基类里会让 /settlements 把它们校验通过然后静默忽略——正是 OrderSearchBaseSerializer 的 docstring 明写要避免的形状（Ruling 40②：「未声明的参数被忽略」是安全的、「声明了却不筛」才是骗人）；基类因此保持不动；列表与导出共用本子类（performance_views.search_params 用它），所以导出自动跟着走。
    ⚠️ 只声明我们有数据、且真的会进查询的字段：对标证据件里 22 个控件中的淘宝状态、是否实名、申请结算、排团团号、机位这五类没有数据源、本类一个都不声明（页面照样画出来、点了提示「尚未开放」）；声明了却不筛、比没声明更糟。收款状态有数据源：看订单的接单实收款、和报酬页色块同一句话——本类声明 net_receipts_state 并且真的进查询。
    值的口径（brief 要求逐条写清）：order_status / order_type 吃的是我们的存储值（OrderStatus 是枚举名 IN_PROGRESS/COMPLETED…，OrderType 是 BOOST/COMPANION）——不是对标的中文文本、也不是证据件里那 14 个静态 option；⭐ 订单状态多选线：order_status 是逗号串枚举码（COMPLETED,UNASSIGNED 这种，逐 token 校验见 _order_status_tokens；与订单列表 status 同一次改、共用同一份判据），单值是逗号串的特例、旧行为不变；order_type 的 -1（对标的「全部订单」）由前端不传处理：后端不做 -1 特判（传了它 = 非法值 ⇒ 1000）；customer_type 缺省 = 0（销售人）：0 是合法值、判据一律 is not None（E-0021：if customer_type: 会把 0 当成「没传」而走错分支），它只在 customer 有值时才有意义（单独传 customer_type 不过滤）。"""

    game = serializers.IntegerField(required=False, min_value=1)
    include_settlement_summary = serializers.BooleanField(required=False)
    #: ⭐ D-0368（报酬页「收款状态」筛选）：两档 =「未收款」/「已结款」，判据 = 订单的收款状态 order.payment_status（上家收款同步那个字段）——received ⇒ SETTLED；unreceived ⇒ 非 SETTLED 的补集。
    #: 不接 order.received_price：生产上那一列没有值，「已收款」会结构性恒空。⚠️ 对标的「结算中」一档：PaymentStatus.SETTLING 档在生产零行（纯人工标记、无人使用）⇒ 仍不进 choices，前端对那档不发参、走「尚未开放」点名提示（同 orderSta 未映射档位的既有通道）。
    #: ⚠️ 参数名用语义英文（order_status 先例），⛔ 不照抄对标的 furlSta。
    net_receipts_state = serializers.ChoiceField(
        choices=("received", "unreceived"),
        required=False,
    )
    #: ⭐ 实装多选：收逗号串枚举码（判据与文案的唯一出处 = `_order_status_tokens`；
    #: 枚举多值照 `order_nos` 形态做，⛔ 不是 `CommaSeparatedListField`——那是纯字符串族）。
    order_status = serializers.CharField(
        max_length=ORDER_STATUS_MAX_LENGTH, required=False
    )
    order_type = serializers.ChoiceField(choices=OrderType.choices, required=False)
    order_source = serializers.IntegerField(required=False, min_value=1)
    #: ⭐ 公司店铺分组（计划 §4 #5）：两账单共用同一个值（页签只切 `emp_type`）。
    source_group = serializers.IntegerField(required=False, min_value=1)
    #: 对标的「销售人 / 发单人」二选一。`default=0` 让缺省语义显式（缺省 = 销售人）。
    customer_type = serializers.ChoiceField(
        choices=[(0, "销售人"), (10, "发单人")], required=False, default=0
    )
    customer = CommaSeparatedListField(required=False)
    username = CommaSeparatedListField(required=False)
    #: 订单号：**精确**（与 `SettlementSearchSerializer` 同一口径，两处不要各自发明）。
    order_no = serializers.CharField(max_length=20, required=False)
    #: 模糊关键词：命中「订单号片段」或「角色名片段」（对标 `key1` 的 placeholder 原文）。
    keyword = serializers.CharField(max_length=128, required=False, allow_blank=True)
    #: 项目筛选值（喂 performance_views.py 的 order__project_text__icontains）。
    #: ⚠️ 这是手写字面量、不从模型派生 ⇒ 列宽必须与 Order.project_text 手动对齐（D-0218 起 900；D-0140 起 300；更早 128 与列宽同值，模型一改它就脱钩）。
    #: 代价只在「整串粘贴」：子串筛选走 icontains、不受此限，只是整串（>900 字）会被判 1000。
    project = serializers.CharField(max_length=900, required=False, allow_blank=True)
    customer_ww = serializers.CharField(max_length=64, required=False, allow_blank=True)
    #: ⭐ 线 C：date_from / date_to 判哪一列——自描述的字符串，⛔ 不是对标证据件里那五个数字档（10 发单 / 20 交单 / 25 录单 / 30 结账 / 40 验收，见 emp-emplist.ts::DATE_TYPE_MAP）。默认 created（录单时间）⇒ 不传它时行为与改动前逐字相同（既有用例一条不改也绿）。
    #: ⚠️ 为什么加在本子类、而不是基类 PerformanceSearchSerializer：基类的 docstring 明写「别直接把新参数加在这里」——SettlementSearchSerializer 从基类继承，而结算列表的时间窗判的永远是 settled_at（settlement_views.settlement_rows）、不认这一档 ⇒ 加在基类会让 /settlements 与对外 bills/ 校验通过然后静默忽略，正是本文件反复点名要避免的形状（Ruling 40②）。设计/计划里写的类名是基类，按本仓「声明了却不筛才是骗人」的硬规则落在本子类；列表/导出共用本子类（performance_views.search_params 用它），所以两处自动同时生效。
    #: ⚠⭐ 时间档共五档；reviewed 一档已改名换列成 completed。业务方现场是「报酬页少了交单时间和验收时间」，而根因不是没接线、是接错了列：原 reviewed 打的是 Performance.reviewed_at，而那一列在生产上全空（它唯一的写入点是 /review/ 绩效审核，这个租户从不走）⇒ 那一档在线上必然筛出空集（设计 §1 的实测读数）。真实验收发生在订单变成 COMPLETED 那一刻（Order.completed_at）⇒ 改接它、并改名成 completed（名字与真值一致）；⚠️ reviewed 这个取值不被接受（⛔ 不留两套契约：改名的代价 = 一处前端映射 + 若干用例）；另外两档（published 发单 / submitted 交单）是本批新增的两个订单列。取值→列：created（默认）→ Performance.created_at（非空）；published → Order.published_at；submitted → Order.submitted_at；completed → Order.completed_at；settled → Performance.settled_at（后四列可空）。⚠️ 五列里四列可空 ⇒ 按它们筛时空值行自然被排除（SQL 三值逻辑），与结算记录页 settled_at__date 的既有口径一致——这是预期、不是漏行（每档各有一条「空值行不出现」的用例）。⚠️ 新增三处 order__ join ⇒ 同一次登记 tests/test_order_read_path_net.py（AGENTS.md §5-G / E-0175）；⚠️ 一档「有列可打」不等于线上筛得出东西：这一列得有人写、且生产上写过——判据见 tests/test_performance_api.py::DateTypeColumnWriterContractTests（为「接了生产零写入列」那类缺陷落的回流网）。
    date_type = serializers.ChoiceField(
        choices=[
            ("created", "录单时间"),
            ("published", "发单时间"),
            ("submitted", "交单时间"),
            ("completed", "验收时间"),
            ("settled", "结账时间"),
        ],
        required=False,
        default="created",
    )
    #: ⭐（2026-11-07 交单时间筛选修复）重声明为**原始串**：基类 DateField 只收 `YYYY-MM-DD`，
    #: 而本页 laydate 是 datetime 控件、选了时分秒会被整段判非法。⛔ 不落基类：
    #: SettlementSearchSerializer 与对外 GET /api/open/v1/bills/ 消费同名字段，改基类会泄漏契约
    #: （`__date` filter 吃垃圾串 = 500，E-0010；先例 = 上面的 date_type）。换算唯一在
    #: performance_views._parse_time_bound——validated_data 只放原始串、时区/窗口不出本文件。
    date_from = serializers.CharField(required=False, max_length=32, trim_whitespace=False)
    date_to = serializers.CharField(required=False, max_length=32, trim_whitespace=False)
    #: ⭐（2026-11-07）「排除禁止结算的」单档（业务方拍板；原三态控件的 `all`/`only` 档已撤，
    #: 前端改挂打手多选下拉的虚拟分组哨兵，见 `exclude_net_negative`）：只认 `excluded`。
    #: 本类只做白名单校验；判据唯一出处 =
    #: `apps.accounts.selectors.settle_forbidden_user_ids`（D-0109：⛔ 不另写第二份
    #: `settle_forbidden` 比较），queryset 分支落在 `performance_queryset` 末段（列表/导出/summary
    #: 共用同一函数，三处自动同源）。
    settle_forbidden_scope = serializers.CharField(required=False)
    #: ⭐（2026-11-07）「排除总绩效为负的」哨兵（打手多选下拉虚拟分组）：只认 `'1'`。
    #: ⛔ 必须显式声明——不声明则 DRF 把整键从 validated_data 丢弃、功能静默永不激活。
    #: 缺省不发参 ⇒ validate 不跑 ⇒ 无此键 ⇒ queryset 分支跳过（默认 = 不排除）。
    exclude_net_negative = serializers.CharField(required=False)

    #: 两格式（唯一判据，正则锚定全串 + `strptime` 真解析拒 2026-13-45）；date-only 维持
    #: 「整天含端」旧口径、带时间按时刻生效。文案沿用现状 DRF 译文、扩成双格式。
    TIME_FILTER_FORMATS = (
        (r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S"),
    )
    TIME_FILTER_INVALID_MSG = (
        "日期格式错误。请从这些格式中选择：YYYY-MM-DD 或 YYYY-MM-DD HH:mm:ss。"
    )

    def _validate_time_filter(self, value):
        """过两格式即放行原始串；两种都不匹配（或 strptime 拒）⇒ 1000。"""
        for pattern, fmt in self.TIME_FILTER_FORMATS:
            if re.fullmatch(pattern, value):
                try:
                    datetime.strptime(value, fmt)
                except ValueError:
                    break
                return value
        raise serializers.ValidationError(self.TIME_FILTER_INVALID_MSG)

    def validate_date_from(self, value):
        return self._validate_time_filter(value)

    def validate_date_to(self, value):
        return self._validate_time_filter(value)

    def validate_order_status(self, value):
        """逗号串 → 已校验的码列表（与 OrderSearchSerializer.validate_status 共用一份判据）。⭐ 实装多选：本类只做转调、⛔ 不另写第二份逐 token 判据
        （两处各写一遍必然漂移——文案/上限/空项判据都只有 _order_status_tokens 一处）。"""
        return _order_status_tokens(value)

    def validate_settle_forbidden_scope(self, value):
        """单档白名单；`only`/`all`/表外值 ⇒ 1000（文案照同文件既有非法 choice 风格）。"""
        if value != "excluded":
            raise serializers.ValidationError("settle_forbidden_scope 只能是 excluded")
        return value

    def validate_exclude_net_negative(self, value):
        """哨兵开关白名单：只认 `'1'`（表外值 ⇒ 1000）；缺省不发参 = 不排除（S-1）。"""
        if value != "1":
            raise serializers.ValidationError("exclude_net_negative 只能是 1")
        return value


class PerformanceAdjustSerializer(serializers.Serializer):
    """ /adjust 的入参（spec §5；P4 的 B3/B4）。⚠️ 一律不设 min_value（D-0015：金额可以为负/零，负值 = 退款/冲抵/调整）；有专门用例钉住「0 与负数都能改」，谁把 min_value 加回来谁红。
    ⚠️ 每个业务字段都 required=False：「只改扣点」「只改接单价」都是合法请求；空载荷 ⇒ 无副作用（与账号线的「编辑空载荷无副作用」同一口径）。
    ⚠️ taker_price 允许 null = 清空覆盖、回落到订单默认价（spec §6.3）；⚠️ reason 上限 200（列宽 255、留出字段名前缀的位置）：超长的请求来源值不能 500（E-0010）。"""

    taker_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    #: ⭐ 本批（D-0142）：直写预计绩效 / 生效绩效（不经过提成算式）。用途有两个（同一端点、同一载荷形状）：绩效弹层的行内编辑/铅笔，以及垃圾桶的归零
    #:（{"temp_amount":"0.00","bonus_amount":"0.00","reason":"清空该接手绩效金额…"}）。⛔ 不加 min_value（D-0015：0 与负数合法）；⛔ allow_null=False：这两列 null=False，null 是非法载荷 ⇒ 由 DRF 直接回 1000（与既有 deduct_value 同形）。
    #: ⚠️ 与 taker_price 的差别：后者 null 是有意义的（清空覆盖、回落订单价）所以 allow_null=True；这两个键没有「清空」这个语义（要清零就显式送 "0.00"）。
    temp_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    bonus_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    deduct_mode = serializers.ChoiceField(
        choices=[("", "未设置"), (DEDUCT_PERCENT, "按比例扣除"), (DEDUCT_FIXED, "扣除固定金额")],
        required=False,
    )
    deduct_value = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    reason = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )


#: /adjust-amount/ 的金额闸（D-0147 批 E 任务 2）。判据与 _HALL_PRICE_AMOUNT_FIELD 同源：Decimal 可解析、≤2 位小数、总位数 ≤12、整数位 ≤10
#:（目标列 Performance.temp_amount / taker_price / Order.hall_price 都是 max_digits=12, decimal_places=2）。⛔ 不设 min_value（D-0015：0 与负数都是合法金额）。
_ADJUST_AMOUNT_GATE = serializers.DecimalField(max_digits=12, decimal_places=2)


class PerformanceAdjustAmountSerializer(serializers.Serializer):
    """POST /performances/<id>/adjust-amount/ 的入参（spec §4.1/§4.6；D-0147 批 E）。载荷形状：{amount_kind: "temp"|"bonus", amount: "<十进制串>", reason?: str}。
    ⚠️ amount 声明成 CharField 而不是 DecimalField（spec §4.6 明写「用字符串」）：沿用本仓金额一律走十进制串的口径（形状照 HallPriceChangeSerializer）、避免 JSON float 直接进 Decimal 的精度漂移；实际解析仍复用同一道金额闸（_ADJUST_AMOUNT_GATE.run_validation）、所以位宽与小数位数不另写一套判据。
    ⚠️ 不设 min_value（D-0015）：0 与负数都合法（退款/冲抵/调整）。⚠️ reason 上限 200（审计落点 OperationLog.reason 列宽 255、留出前缀位置）：请求来源的值不能因为超长在写库时 500（E-0010）。"""

    amount_kind = serializers.ChoiceField(
        choices=("temp", "bonus"),
        error_messages={
            "required": "缺少 amount_kind",
            "invalid_choice": "amount_kind 只能是 temp 或 bonus",
            "null": "amount_kind 只能是 temp 或 bonus",
        },
    )
    amount = serializers.CharField(
        max_length=32,
        required=True,
        allow_blank=False,
        error_messages={"required": "缺少 amount", "blank": "金额不能为空"},
    )
    reason = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )

    def validate_amount(self, raw):
        """反推基价要用的是 `Decimal`，这里只把字符串收口成金额（失败 ⇒ 业务码 1000）。"""
        try:
            return _ADJUST_AMOUNT_GATE.run_validation(raw)
        except serializers.ValidationError:
            raise serializers.ValidationError("金额格式不正确")


class PerformanceReviewSerializer(serializers.Serializer):
    """ /review 的入参（spec §5：通过/驳回（带意见））。⚠️ approved 必填：缺了它就无法判断是「通过」还是「驳回」——不要给它默认值，否则一次漏传会静默驳回一张单（发钱方向的事故、看不出错）。
    ⚠️ comment 上限 255（=审计落点 OperationLog.reason 的列宽）：超长不能在写库时 500（E-0010），留痕那边还有一道截断兜底。"""

    approved = serializers.BooleanField()
    comment = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


#: 绩效列表最早一批的 14 列。⚠️ 抽成模块常量是为了让 SettlementListSerializer 用同一份基础集拼自己的字段（⛔ 不重抄），
#: 同时显式地不继承下面那两组新列（理由见那个类的 docstring）。
#: ⭐ 追加第 15 项 source_order_no（来源单号）：业务方要两个列表页的「关联订单」列都显示来源单号，而绩效列表与结算列表共用本常量 ⇒ 加在这里一次、两个列表同时出参（⛔ 不在两个 Meta.fields 里各写一份——两处必然漂移）；⚠️ 追加在末尾（既有 14 项位置不动，改动面最小）；字段本身的声明、实测依据与「为什么不是系统单号」见 PerformanceListSerializer.source_order_no。
_PERFORMANCE_LIST_CORE_FIELDS = (
    "id", "order_no", "order_status", "user", "username", "emp_type",
    "temp_amount", "bonus_amount", "deduct_mode", "deduct_value",
    "taker_price", "review_status", "settle_status", "created_at",
    "source_order_no",
)

#: ⭐ 绩效管理页 `/Emp/emplist` 新增的 9 列（补完计划 §2.1）。
#: ⛔ **只进绩效列表**——结算列表本轮不动（见 `SettlementListSerializer`）。
_EMPLIST_FIELDS = (
    "order_price", "net_receipts", "refund_fee", "game_role", "dl_project",
    "wangwang", "kefu", "log_user_name", "deduct_fee",
)

#: 结账留痕三列（`settled_at` / `settled_by` / `settled_amount`）：绩效列表与结算列表
#: **都要**，所以声明在父类上、两边的 `Meta.fields` 各列一次（位置由各自决定）。
_SETTLED_SNAPSHOT_FIELDS = ("settled_at", "settled_by", "settled_amount")


class DeductFeeField(serializers.DecimalField):
    """列表行里的扣点额（金额，不是扣点参数值）——值来自 performance_amount.deduct_amount。⚠️ 继承 DecimalField、不是 SerializerMethodField：金额的渲染格式（COERCE_DECIMAL_TO_STRING ⇒ "10.00"）只允许有一处定义、与 temp_amount / bonus_amount 同源；SerializerMethodField 会把 Decimal 交给 JSON 编码器变成 10.0 这种 float 形状（PayableNetField 的 docstring 记了同一处）；实现手法也与它同形：覆写 get_attribute 自算值、字段类型仍是 DecimalField。
    ⚠️ 算式不在本类里（emp-emplist 计划 §2.1 的硬判据）：一律调 performance_amount.deduct_amount(mode=..., value=..., base=...)，base 是 temp_amount（「预计绩效总计」，不是 bonus_amount——见那个函数的 docstring）；mode/value 未设置（""/None）或 value==0 ⇒ Decimal("0.00")，这一条口径也由那个函数给出、本类不重写。
    ⭐ 越界 ⇒ 这一格读成 null、⛔ 不让整张列表打不开（出处：docs/plans/2026-09-22-emp-emplist-completion.md 文末「执行记录」节——⚠️ 不在 §2.1——+业务方口径「接不了就告诉我、别让页面坏掉」）：读要容忍——deduct_amount 抛 AmountError（比例 > 100，或固定额 > temp_amount）是预期失败、不是 500；这个状态常见且可达——退单会把 MAIN 行的 temp_amount 清 0.00（refunds.py 第 2 步）而 deduct_mode/deduct_value 留着，/adjust 只改接单价时也会让 temp_amount 变小；列表是逐行调用、又是财务诊断坏行的唯一入口——T1 原先照结算列表先例整表回 1000（把整页打不开）比某格没数更糟 ⇒ 该行 deduct_fee 返回 None（JSON null）：行照常渲染、其它行的值不受影响，⛔ 不给 0.00 之类假数、⛔ 不把该行藏掉（假数会说假话，藏行会让财务看不见坏行）。⚠️ 写仍然 fail-closed：扣点越界不因本字段被吞掉——/settlements/settle 走的是 settlement_amount.payable_net（settlement.py::_row_nets 把 AmountError 翻成 SettleError）整批拒绝，/adjust 也仍在写库之前校验（performance_views.py::adjust）；本类只服务读（绩效列表和 xlsx 导出）、写路径一个字没动：读要能看见、写仍然整批拒绝；对照用例见 tests/test_emp_emplist_backend.py 里相邻的两条（列表渲染 null / 同一批数据 settle 仍拒绝）。⚠️ 只吞 AmountError（预期失败）：其它异常照旧冒泡响亮地 500（E-0010：不要把未预期异常伪装成业务结果）。"""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)

    def get_attribute(self, performance):
        try:
            return deduct_amount(
                mode=performance.deduct_mode,
                value=performance.deduct_value,
                # ⚠️ 收口成 `Decimal`：未从库里刷新的实例上它可能是 `str`，
                # 而 `commission_amount` 会 `str * Decimal` ⇒ TypeError
                # （同一坑见 `settlement_amount.py` 第 3 条纪律）。
                base=Decimal(performance.temp_amount),
            )
        except AmountError:
            # 扣点越界 = **读**上的预期失败 ⇒ 这一格 `null`，行留在列表里（见类 docstring
            # 的「越界 ⇒ 这一格读成 null」）。⛔ 不要在这里改成 `Decimal("0.00")`
            # （假数）或让异常继续冒泡（整页打不开）；写路径的 fail-closed 不在这里。
            return None


class PerformanceListSerializer(PriceRedactionMixin, serializers.ModelSerializer):
    """绩效列表的一行（spec §5）。⚠️ order_no/username 是跨表读：Performance 自己只有 FK；用 source= 让它们在列表页一次 select_related 拿到、而不是每行再查一次库。⭐ source_order_no 也是同一族的跨表读（source="order.source_order_no"，业务方报「关联订单列应取来源单号」；依据与「为什么不是系统单号」见下面它的声明处）。⚠️ user 可能为 NULL（还没人接）——source="user.username" 在中间对象为 None 时返回 None（DRF get_attribute 对 None 短路）、不会抛。
    接单价按 order.restrict_view_taker_price 脱敏：本类是绩效列表、改价和审核三个响应的形状，构造时必须带 context={"request": request}、不带就等于没脱敏；订单的读序列化器不暴露接单价，所以这条限制的落点就在这里。
    ⭐（绩效管理页 /Emp/emplist 补完计划 §2.1）新增 12 个只读字段，全部取自 order.*（同一次 JOIN）或本表列（无查询），deduct_fee 是唯一的计算列：order_price / net_receipts / refund_fee / game_role / dl_project / wangwang / kefu / log_user_name / deduct_fee / settled_at / settled_by / settled_amount；⛔ 不加任何 ≥0 校验（D-0015：退款/冲抵/调整都可以是负数）。order_price / net_receipts / refund_fee 按节点脱敏：不持 order.list 就读成空——和接单价那条限制是两套判据（节点和 flag）互不蕴含；不持节点的人曾经能在「我的绩效」里看到订单金额，所以这三列不能再照订单侧「不脱敏」放行。"""

    #: 接单价：被 `order.restrict_view_taker_price` 限制时读成 `None`。
    REDACTED_PRICE_FIELDS = (("taker_price", can_view_taker_price),)

    #: 订单金额族的三列：不持 order.list 时读成 None。⚠️ 判据不在这里：本类只读 context["can_view_order_money"]（由 performance_views.can_view_order_money()
    #: 判好、PerformanceViewSet._row_payload() 带进来），⛔ 不自己查节点——那会是同一事实的第二套判据（E-0021 的形状）。
    #: ⚠️ 缺这个键 ⇒ 按「不能看」处理（fail-closed）：常量 None 也走同一支，所以内部构造（没有请求上下文）不会静默把订单金额漏出去。
    MASKED_ORDER_MONEY_FIELDS = ("order_price", "net_receipts", "refund_fee")

    def get_fields(self):
        """在 PriceRedactionMixin 的 flag 脱敏之外，再按视图给出的决定遮掉订单金额族。⚠️ 复用 mixin 的 _redacted_price（恒 None）⇒ 值的形状与 taker_price 脱敏时逐字节相同（键恒在、值为 null）；
        ⛔ 不在这里删键：前端契约是「键恒在」。"""
        fields = super().get_fields()
        if not self.context.get("can_view_order_money"):
            for name in self.MASKED_ORDER_MONEY_FIELDS:
                if name in fields:
                    fields[name] = serializers.SerializerMethodField(
                        method_name="_redacted_price"
                    )
        return fields

    #: 微信收款码的短时效读链接（图片存储线；spec §7 末行「财务看某人的收款码」）。⚠️ 只读、服务端签名（⛔ 前端不拼 URL、⛔ 库里不存 URL）；没档案 / 没传过 ⇒ None；
    #: 签名失败也回 None——⛔ 不能让某一行的存储故障把整页绩效列表打成 500。取数走 user__staff_profile（视图的 select_related 已预取，避免逐行查库）。
    wechat_qr_url = serializers.SerializerMethodField()

    #: 本字段的准入节点：finance.payroll（apps/rbac/catalog.py 的 id 80「打手与客服工资」）。依据 = A8 裁定「这个入口复用 finance.payroll」——前端路由守卫
    #:（src/frontend/src/config/routePermissions.ts 的 agents-performance）用的就是它，而路由守卫只挡页面、挡不住接口（列表端点的门是 my_performance.export，见 performance_views.py::PERFORMANCE_PERMISSIONS）⇒ 判据必须落在服务端这一格上。
    #: ⚠️ 真正的洞是「持 order.list 但不持 finance.payroll」：order.list 是 performance_views.py::PERFORMANCE_MANAGEMENT_NODE，持它 ⇒ 数据范围放宽到全工作室，而本字段若没有独立判据，别人的收款码签名链接就会进载荷；内置角色里「客服 / 客服主管」正好满足这个组合（apps/rbac/seed.py::BUILTIN_ROLE_MODULES 按 order 栏目批发、不给 finance 栏目）。⛔ 别把「代练员」写成这个洞的例子（旧注释就是那么写的，已被实测证伪）：代练员的内置模块表不含 order ⇒ 不持 order.list ⇒ performance_queryset 在函数末尾把它收窄成只看自己 ⇒ 它拿不到别人的码。⚠️ 字面值与 settlement_views.FINANCE_READ_NODE / performance_write_views.ALIPAY_DUPLICATES_NODE 同源（都指目录 80）；⛔ 不从 settlement_views 反向 import——那个模块 import 本模块，会成环。
    WECHAT_QR_NODE = "finance.payroll"

    #: `get_wechat_qr_url` 的**逐实例**判据缓存（`None` = 还没解过；说明见那个方法）。
    _wechat_qr_allowed = None

    #: ⭐（2026-11-07 R3）「挂负账」行标记：`行.user_id ∈ context["negative_debt_user_ids"]`
    #: （挂负账打手集——2026-11-08 撤强制展示后只剩红签用途）。恒在键；缺键（/adjust、/review 等
    #: `_row_payload` 不传）⇒ fail-closed `False`。判据唯一出处 =
    #: `apps/business/performance_negative_debt.py`。范围 = 仅打手域：销售提成行恒 False
    #: （业务方 2026-11-08 拍板——挂负账打手的客服提成行不亮红签）。
    negative_debt = serializers.SerializerMethodField()

    def get_negative_debt(self, obj):
        debt_ids = self.context.get("negative_debt_user_ids")
        if not debt_ids:
            return False
        return obj.emp_type == EmpType.MAIN and obj.user_id in debt_ids

    def get_wechat_qr_url(self, obj):
        """持 finance.payroll（或租户管理员短路——has_node_now 的既定行为、全仓一致）的请求者拿得到链接；其余一律 None。
        ⚠️ 本字段比设计 §9.3 更严（已裁定的行为、不要改）：绩效列表对每一行——含请求者自己那一行——都要求 finance.payroll ⇒ 本人看自己那一行同样是 None；本人看自己那一份不在这条链上：走 GET /api/auth/profile（apps/core/profile_views.py::MyProfileView，门只要 IsAuthenticated、它自己的档案上那格 wechat_qr_url 就是签名链接）⇒ 读 docs/specs/2026-09-25-media-storage-design.md §9.3 那句「只给本人+持 finance.payroll 的人看」时按该节末尾的回填更正读，⛔ 不要据此把它判成本字段漏了「本人」、也 ⛔ 不要「顺手放行本人」。
        ⭐ 判据就地判、不靠视图经 context 传：本类是 /performances/、/adjust/、/review/ 三处响应的形状，把判据放在视图里就得三处各传一次（漏一处就是静默泄漏）；⛔ 不抛异常：本字段只是载荷里的一格，缺权限不该把整页打成 403/500（与「签名失败回 None」同一条口径）。⚠️ 判据按序列化器实例缓存一次：本方法逐行调用，而 has_node_now 对非管理员会查一次库 ⇒ 一页 20 行就是 20 条多余查询（同 OrderListSerializer.get_fields 对 can_view_taker 的处置）；⚠️ 缺 request 时 fail-closed（回 None）：读取路径按契约必带 context（见本类 docstring），真缺了就说明构造点漏了——宁可不给链接，也不给一个没判过权限的链接。"""
        allowed = self._wechat_qr_allowed
        if allowed is None:
            request = self.context.get("request")
            allowed = request is not None and has_node_now(request.user, self.WECHAT_QR_NODE)
            self._wechat_qr_allowed = allowed
        if not allowed:
            return None
        profile = getattr(obj.user, "staff_profile", None) if obj.user_id else None
        key = getattr(profile, "wechat_qr_key", "") or ""
        if not key:
            return None
        try:
            from apps.core.storage import get_storage

            return get_storage().presign_get(key).url
        except Exception:  # noqa: BLE001
            return None

    order_no = serializers.CharField(source="order.order_no", read_only=True)
    #: ⭐ 新增：来源单号（Order.source_order_no = 来源/交易平台那一侧的单号）。为什么要它：业务方报「打手与客服报酬页 / 结算记录页的『关联订单』列取的是系统内置单号，这里应该取来源单号」⇒ 列表载荷必须先把这一格交出来，前端才可能渲染它。
    #: 实测依据（对标侧的字段语义，不是推断）：.tools/recon/order-view/_SOURCE.txt:14-19 记着同一次真机取数里 data[0].orderId 是 20 位本系统订单号、同行 jionOrder 是 19 位关联/交易平台单号（⚠️ 两个真值不在本文件回显——原始记录只在那份不进 git 的勘察台账里，写法照 AGENTS.md §5-C / E-0077「要引真值当证据就占位」：真值进 src/** 会命中 pii-guard 的「19 位数字串」形状判据）；对标那一列的 templet 正文取 res.jionOrder、data-id 取 res.orderId（.tools/recon/suqing1/assets/JS2019/Emp/emplist.js:314-323）⇒ 正文 = 来源单号。
    #: ⚠️ 我们原先判成「对标的拼写错误」并把正文取成系统单号——那个判断已作废（更正落在 emp-emplist.ts 第 35 条与 emp-billsummary.ts 的两处登记）。📌 同口径先例：apps/business/performance_export.py::detail_values 的「关联订单」列（导出明细第 7 列）从 2026-09 起就一直取 order.source_order_no，而「系统内置单号」是同表第 10 列（order.order_no）——本次是把列表页对齐到导出早已采用的口径。⚠️ blank=True（非 NULL）的文本列 ⇒ 没填回空串、不是 null（与 game_role / dl_project 同形状），前端遇到空串渲染空 ⛔ 不回落成系统单号（那会造成「看起来有来源单号」的假象）；跨表读、与上面的 order_no 同一次 select_related("order") 取出，不引入 N+1。
    source_order_no = serializers.CharField(source="order.source_order_no", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True, default=None)
    #: ⭐ P4FU-C（D-0071 ③ 的后端一半）：绩效页那一列显示的是**订单状态**，不是审核状态。
    #: 只读、跨表读 `order.status`；`get_queryset` 已 `select_related("order")`，不引入 N+1。
    #: ⚠️ **不动 `review_status`**：取消审核闸门后它仍被 `/review` 与既有页面读（D-0071 ④）。
    order_status = serializers.CharField(source="order.status", read_only=True)

    # ── 订单维度的 8 列（全部走 `order` 那一次 `select_related`，不新增查询）──
    #: 订单金额。`Order.total_price` 是 `null=False default=0.00` ⇒ 恒有值（不是 `null`）。
    order_price = serializers.DecimalField(
        source="order.total_price", max_digits=12, decimal_places=2, read_only=True
    )
    #: 实际收款。`Order.received_price` 可空 ⇒ 没填时是 `null`（**不是** `0`）。
    net_receipts = serializers.DecimalField(
        source="order.received_price", max_digits=12, decimal_places=2, read_only=True
    )
    #: 退款。`Order.refund_fee` 是 `null=False default=0.00` 的累计退款额（可为负）。
    refund_fee = serializers.DecimalField(
        source="order.refund_fee", max_digits=12, decimal_places=2, read_only=True
    )
    #: 角色名。`game_role_name` 是 `blank=True`（非 NULL）⇒ 没填回**空串**，不是 `null`。
    game_role = serializers.CharField(source="order.game_role_name", read_only=True)
    game_name = serializers.CharField(source="order.game.name", read_only=True)
    #: 订单项目（权威值是文本快照 `project_text`，不是 FK 的 `project.name`，见 D9）。
    dl_project = serializers.CharField(source="order.project_text", read_only=True)
    #: 旺旺 / YY（列名即 `customer_ww`）。
    wangwang = serializers.CharField(source="order.customer_ww", read_only=True)
    #: 销售客服（`sales_agent` 是可手填的文本快照）。
    kefu = serializers.CharField(source="order.sales_agent", read_only=True)
    #: 记录者（`created_by` 是用户名快照，上限 150）。
    log_user_name = serializers.CharField(source="order.created_by", read_only=True)
    log_user_display = serializers.SerializerMethodField()

    def get_log_user_display(self, obj):
        name = obj.order.created_by
        return self.context.get("recorder_names", {}).get(name, name)

    #: 扣点额（**计算**，见 `DeductFeeField`；⛔ 算式只在 `performance_amount.deduct_amount`）。
    deduct_fee = DeductFeeField()

    # ── 本表的三列（结账留痕；与 `SettlementListSerializer` 同一个来源）──
    #: `settled_at` / `settled_by` 直接读模型字段（与 `reviewed_at`/`reviewed_by` 同形）。
    #: 未结账时 `settled_at` 是 `null`、`settled_by` 是空串（模型默认值）。
    settled_at = serializers.DateTimeField(read_only=True)
    settled_by = serializers.CharField(read_only=True)
    settlement_method = serializers.SerializerMethodField()
    online_payroll_batch_id = serializers.UUIDField(read_only=True)
    online_payroll_occupied = serializers.SerializerMethodField()

    def get_settlement_method(self, instance):
        return instance.settlement_method or ("manual" if instance.settled_at else None)

    def get_online_payroll_occupied(self, instance):
        return bool(instance.online_payroll_batch_id and not instance.is_settled)

    #: ⭐ P4-FE-4：结账时的实结金额快照。未结账 ⇒ null（不是 0、也不是空串）。⚠️ 声明成朴素的只读 DecimalField 就够：null 的形状由 DRF 在 Serializer.to_representation
    #: 里保证——属性为 None 时它短路口、根本不调本字段的 to_representation（rest_framework/serializers.py:543-552，实测本仓库安装版：if check_for_none is None: ret[name] = None）。
    #: ⛔ 不要为此覆写 to_representation：那条分支在列表出参路径上永远不会被调用（第一版写过一个覆写版本，M4 变异实测「把 None 覆写成 0」用例仍然全绿 ⇒ 死代码；带死代码的错误注释比不写更糟，同 E-0011）。⚠️ 有值的那一支仍走 COERCE_DECIMAL_TO_STRING ⇒ "-5.00" 这种字符串，与 bonus_amount / payable_net 同源（SerializerMethodField 会变成 -5.0）；⛔ 不加 min_value（D-0015）：单行净额可以为负，快照同样。
    settled_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, read_only=True
    )

    #: ⭐ 本批（D-0142）：接手绩效弹层要显示昵称、小时数、接手项目（spec §5.5）。⚠️ display_name 用 SerializerMethodField 而不是 source="user.first_name"：
    #: 口径是「昵称优先、空串回落登录账号」，一个 source 表达不了回落。user 在 _base_queryset / performance_queryset 都已 select_related ⇒ 不加查询。
    display_name = serializers.SerializerMethodField()
    #: `Performance.hours`（可空列原值；`null` ⇒ JSON `null`）。
    hours = serializers.IntegerField(read_only=True, allow_null=True)
    #: ⛔ **刻意不叫 `project`**：同一个载荷里 `dl_project` 已经是**订单**项目（本类上方），
    #: 两个「项目」用同一个键名会把「标签一样、语义不同」的坑再挖一次。
    taker_project = serializers.CharField(source="project", read_only=True)

    #: ⭐ 提前结算申请的申请时刻（spec §3.4 第 5 条：财务可见）。⚠️ 没申请 ⇒ null（⛔ 不是空串、也不是 False）：前端判的是「有没有这个时刻」，二值化之后「申请时间读不出来」与「没申请」就分不出来了（E-0021 同族）；值是 PerformanceSettleRequest.created_at（ISO 8601），⛔ 不是 settled_at。
    #: ⚠️ 读的是反向一对一（PerformanceSettleRequest.performance 是 OneToOneField ⇒ Django 生成 ReverseOneToOneDescriptor）：列表 queryset 必须 select_related("settle_request")（performance_queryset / _base_queryset 都已加）⇒ 不产生 N+1；漏了那一句只会多一条逐行查询、不会改变答案。
    #: ⚠️ 没有申请时反向描述符抛 RelatedObjectDoesNotExist（Django 让它同时继承 ObjectDoesNotExist 与 AttributeError，见 related_descriptors.py 的 ReverseOneToOneDescriptor.RelatedObjectDoesNotExist）⇒ 这里按 ObjectDoesNotExist 接。
    settle_requested_at = serializers.SerializerMethodField()

    def get_settle_requested_at(self, obj):
        """该绩效的提前结算申请时刻；没申请 ⇒ None（口径见字段声明处的注释）。⚠️ 走 local_iso（项目时区 +08:00）：SerializerMethodField 的返回值不过 DRF 的 DateTimeField、裸 datetime 会被 JSON 编码器按 UTC 序列化成 +00:00——同一个载荷里 settled_at 是 +08:00、本格却是 +00:00、页面显示会差 8 小时（E-0238 家族；口径见 apps/core/datetime_display.py 的模块 docstring）。
        ⭐ cancelled 的申请回 None——作废的申请不再是「这一行申请过」的证据（打手可以重申）、财务看板上也不该再挂着「已申请」的标记；判据只排 cancelled（pending / settled 照旧给出时刻）、与配额口径 settle_requests.QUOTA_STATES 同一条边界：占额度的那两条 = 会显示标记的那两条。"""
        try:
            request_row = obj.settle_request
        except ObjectDoesNotExist:
            return None
        if request_row.state == PerformanceSettleRequest.State.CANCELLED:
            return None
        return local_iso(request_row.created_at)

    def get_display_name(self, obj):
        user = obj.user
        if user is None:
            return ""
        # 口径的**唯一出处**在 `apps.accounts.display.user_display_name`（昵称优先、空串回落账号）：
        # ⛔ 不要在这里再写一遍 `first_name or get_username()`（抄第二份就会各自漂移）。
        return user_display_name(user)

    class Meta:
        model = Performance
        fields = [
            *_PERFORMANCE_LIST_CORE_FIELDS,
            # ── 追加（emp-emplist 计划 §2.1；**追加在末尾**，既有列的位置不动）──
            *_EMPLIST_FIELDS,
            # 本批（D-0142）三个只读键：声明和列进 `Meta.fields` 两件都要做
            # （只声明不列进去，DRF 会静默丢掉）。这三列只进列表、不进导出。
            "display_name", "hours", "taker_project",
            "log_user_display",
            *_SETTLED_SNAPSHOT_FIELDS,
            # ⭐ 图片存储线：财务看结算对象的收款码（只读、服务端签名）。
            # ⚠️ 只声明不加进 Meta.fields 会被 DRF **静默丢掉**（本文件既有教训）。
            "wechat_qr_url",
            # ⭐ 提前结算申请时刻（spec §3.4 第 5 条）：财务在「打手与客服报酬」列表能看到「这一行申请过提前结算」。⚠️ 声明了必须列进来，否则 DRF 静默丢键（本文件既有教训）；
            # ⛔ 只进绩效列表（本类）——结算列表 SettlementListSerializer 的 Meta.fields 是另一份，本批没有要求所以没加（加了等于顺手扩那个页面的契约）。
            "settle_requested_at",
            # ⭐（2026-11-07 R3）「挂负账」行标记（恒在布尔键；声明不列进来会被 DRF 静默丢掉）。
            "negative_debt",
            "settlement_method", "online_payroll_batch_id", "online_payroll_occupied",
            "game_name",
        ]


class SettleRequestSerializer(serializers.Serializer):
    """结账入参（spec §4.3 第 1 步/§7.1）。必须过序列化器、否则畸形值会变 HTML 500。三条判据都不是风格问题：
    ⚠️ allow_empty=False 是必需的：第一版用 updated == len(set(ids)) 做 fail-closed 判据，而 0 == len(set([])) 恒成立 ⇒ 空列表会被判成「成功结账 0 笔」并返回 OK（spec §17.1 #9）；空列表必须在这里就被拒成 1000。
    ⚠️ max_length 防的是「未校验的值直进 filter()」这一类（spec §7.1 的「超长列表」）：没有上界时一个几十万元素的列表会撞 PostgreSQL 的绑定参数上限 → HTML 500（E-0010）；1000 是实施期拟定的批次上界（spec 只点名了这类风险、没给数字）。⚠️ reason 上限 255 = OperationLog.reason 的列宽：超长会在绩效已经落库之后写审计行时 500、留下「只改字段、没有留痕」的脏行——P2 的 PriceChangeSerializer 注释记录过同一个坑（spec §7.1 的 reason 行）。⛔ 不给 performance_ids 的元素加下界（min_value）：主键不是金额，但 D-0015 的「边界判据不靠猜」同向——这里只需要「是整数」，0/负数查不到就是「不存在」，由 1006 的逐 id 判据解释，而不是在入口造一个 spec 没写过的约束。"""

    performance_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        max_length=1000,
        error_messages={
            "required": "缺少 performance_ids",
            "empty": "performance_ids 不能为空",
            "not_a_list": "performance_ids 必须是列表",
            "max_length": "一次结账最多 1000 条绩效",
        },
    )
    reason = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


# ── P4-8：结算列表（spec §7 / §4.4 ③）────────────────────────────────


class CommaSeparatedIntegerListField(serializers.ListField):
    """?performance_ids=1,2,3 这类逗号串入参（查询串里没有「列表」这个类型）。⚠️ 为什么必须自己拆：DRF 的 ListField 只接受 list、收到字符串直接判 not_a_list（业务码 1000）；
    而「清算绩效」弹窗拿到的 id 就是这样一串（对标子页的 URL 也是 ?ids=1,2,3）⇒ 拆分是端点的入参契约、不能推给调用方改成重复参数（那会与对标形状不一致，且对标的 ids 就是一个参数）。"""

    def to_internal_value(self, data):
        if isinstance(data, str):
            data = data.split(",")
        return super().to_internal_value(data)


class SettlementPreviewQuerySerializer(serializers.Serializer):
    """GET /api/business/settlements/preview 的查询参数（计划 §3 T2.1）。⚠️ 与 SettleRequestSerializer 同一套边界（allow_empty=False / max_length=1000 / 元素只要求是整数）：同一条 filter(id__in=…) 上的两个入口、
    边界各写一套就会漂移——而未校验/超长的列表直进 filter() 会撞 PostgreSQL 的绑定参数上限 ⇒ HTML 500（E-0010 的「超长列表」那一格）。
    ⛔ 不给元素加下界（min_value）：与结账入口同口径——0/负数查不到就是「不存在」，由端点按「命中 0 行」如实返回（count: 0）、不在入口造一条契约没写过的约束。"""

    performance_ids = CommaSeparatedIntegerListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        max_length=1000,
        error_messages={
            "required": "缺少 performance_ids",
            "empty": "performance_ids 不能为空",
            "not_a_list": "performance_ids 必须是逗号分隔的整数",
            "max_length": "一次最多预览 1000 条绩效",
        },
    )


class SettlementSearchSerializer(PerformanceSearchSerializer):
    """结算列表的查询参数（spec §7）：绩效列表那六条 + 订单号 + 游戏 + 订单来源。⚠️ 从 PerformanceSearchSerializer 继承而不是重抄一份：前六条（人/收入类型/审核状态/结账状态/时间窗）是同一件事，重抄会让「加一条筛选」必然漏一处——而漏掉的那份不是「少筛一个」而是「少校验一个」：未校验的值直进 filter() 就是 HTML 500（E-0010，与 OrderSearchBaseSerializer 抽基类同一个理由）。
    ⚠️ order_no 按精确匹配（视图里 order__order_no= | order__source_order_no= 双列 OR，D-0301）：spec §7 只写了「订单号」这一格、没有给模糊语义；icontains 会让「20 位单号的任意片段」命中多条、那不是 spec 给的判据、不该由实现者发明（E-0006 的同族风险）；⚠️ max_length=128 = 两列列宽的较大者（Order.source_order_no 128 / order_no 20，D-0301 放宽「来源单号也能精确查」的取数前提）：超长值在入口就被拒，不要指望数据库（它抛的是 DataError ⇒ HTML 500）。
    ⭐ order_source（D-0323）是两态参数（给结算记录页的来源分解行「查看明细」用）：正整数字串（?order_source=5）⇒ validate_order_source 转成 int、视图按 order__order_source_id=<id> 筛（指定来源）；字面 'none'（?order_source=none）⇒ 原样通过、视图按 order__order_source__isnull=True 筛（未指定来源——Order.order_source 是可空 FK，来源被删/录单没选都会落进这一档）；其余（0/负数/非数字/别的词）⇒ ValidationError ⇒ 200+code 1000（E-0010）；不传/空串（空串在视图入口按「没传」剔除）⇒ 不筛来源、全量。⚠️ settlement_rows 与本序列化器被开放平台 GET /api/open/v1/bills/ 共用同一个入口（SettlementViewSet._params）：本参数随之成为对外端点加法的新筛选项，不送它的对外调用方行为不变（同一事实只有一处定义、不顺手在这里分裂口径）。"""

    game = serializers.IntegerField(required=False, min_value=1)
    order_no = serializers.CharField(max_length=128, required=False)
    #: 两态：正整数字串 / 字面 `'none'`（判据与两态语义见类 docstring；长度只挡
    #: 「一个几 KB 的查询串」，真正的值域判据在 `validate_order_source`）。
    order_source = serializers.CharField(max_length=32, required=False)

    def validate_order_source(self, value):
        """两态的唯一校验口（D-0323）：'none' 原样、正整数字串转 int、其余拒成 1000。⚠️ 正整数下界与 user / game 同一条纪律（min_value=1 的理由）：?order_source=0 这种「看起来像 id 的边界」在入口就拒、不让它变成「筛出一个空集」的静默形状。
        ⚠️ E-0299：⛔ 不用 str.isdigit() 判「是不是数字串」——'²'.isdigit() 是 True（Unicode 上标也算 digit）而 int('²') 抛 ValueError、会穿出 is_valid() 与 envelope_exception_handler 变 HTML 500（E-0010 红线，M1）；直接 try/except int()、让「int() 吃不吃得下」当唯一判据（全角数字 '１' 这类 int() 吃得下 ⇒ 照旧算合法数字串、不收紧）。"""
        if value == "none":
            return value
        try:
            parsed = int(value)
        except ValueError:
            raise serializers.ValidationError(
                "order_source 只接受正整数或字面 'none'"
            )
        if parsed < 1:
            raise serializers.ValidationError(
                "order_source 只接受正整数或字面 'none'"
            )
        return parsed


class SettlementPeopleSearchSerializer(serializers.Serializer):
    """GET /settlements/people 的查询参数（D-0089）。⚠️ 只声明 game 一个：本端点只按游戏收窄。⛔ 不复用 SettlementSearchSerializer——那会让「声明的参数被静默忽略」，
    正是 PerformanceListSearchSerializer 的 docstring 明写要避免的形状（Ruling 40②）。"""

    game = serializers.IntegerField(required=False, min_value=1)


class SettlementDailySummarySerializer(serializers.Serializer):
    """按结账日期与收入类型筛选每日汇总。"""

    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    emp_type = serializers.ChoiceField(choices=EmpType.choices, required=False)


class SettlementDailySourcesSerializer(serializers.Serializer):
    """按结账日期与收入类型筛选来源分解。"""

    date = serializers.DateField(required=True)
    emp_type = serializers.ChoiceField(choices=EmpType.choices, required=False)


class PayableNetField(serializers.DecimalField):
    """列表行里的应付净额（spec §4.4 ③）——值来自 settlement_amount.payable_net。⚠️ 它读 instance.unsettled_adjustments：那是视图用 Prefetch(..., to_attr=...) 挂上去的、已筛好的「未结账调整记录」列表（payable_net 的入参契约就是「调用方筛好再传」）；⛔ 故意不做回落查询（例如找不到属性就 instance.adjustments.filter(...)）：静默回落会把每条记录变成一次额外 SQL（N+1），而且「哪些调整记录算未结账」会有两处定义。
    ⚠️ AmountError 不由这里吞（settlement_amount.payable_net 的 docstring 明写会透传）：列表逐行调用时必须预期这个失败形态、由视图按业务码翻译（settlement_views.SettlementViewSet.list）——翻译成「净额 0」是一笔静默错账；⭐ 但本字段会把它补上「是哪一行」（P4-8 Minor 1）：整表拒绝的形状不变（同一个 AmountError 类型、同一个 code）、只在 msg 末尾补 performance_id / order_no——否则财务只知道「有一行坏」、定位不到坏行（列表可能几百行）；补在这里而不是视图里：视图的 except 拿到异常时已经不知道是哪一行了（get_serializer(page, many=True) 的迭代在 DRF 内部）。
    ⚠️ 继承 DecimalField 而不是 SerializerMethodField：金额的渲染格式（COERCE_DECIMAL_TO_STRING ⇒ "-15.00"）只允许有一处定义、与 bonus_amount 等既有金额字段同源；SerializerMethodField 会把 Decimal 交给 JSON 编码器变成 -15.0。"""

    def __init__(self, **kwargs):
        kwargs.setdefault("max_digits", 12)
        kwargs.setdefault("decimal_places", 2)
        kwargs.setdefault("read_only", True)
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        try:
            return payable_net(instance, instance.unsettled_adjustments)
        except AmountError as exc:
            # ⭐ **只补行标识，不改形状**（清单 9 / P4-8 Minor 1）：
            # 类型、`code`、整表拒绝的语义一个字不变；`msg` 末尾补上出错的绩效与订单。
            # `instance.order` 已由视图 `select_related("order", "user")` 取好。
            raise AmountError(
                f"{exc.message}（绩效 {instance.pk} / 订单 {instance.order.order_no}）",
                exc.code,
            ) from exc


class SettlementListSerializer(PerformanceListSerializer):
    """结算列表的一行 = 绩效列表的一行 + 应付净额（spec §7）+ 项目。⚠️ 继承而不是重抄：结算列表就是「财务视角的绩效列表」（同一批行、同一套筛选）、唯一多的数是 payable_net；重抄一份会让两个列表的字段集各自漂移。
    ⭐ P4-FE-0 追加三列（对标 /Emp/billsummary「结算记录」页）：project（项目）/ settled_at（结算时间）/ settled_by（结算人）；⭐ P4-FE-4 再追加一列 settled_amount（实结金额快照，只读；未结账 ⇒ null）——它是结账那一刻的应付净额、与活值 payable_net 会不同（结账后调整被翻成已结账、活值不再计入它们；那正是这一列存在的理由）；⛔ 原写「只加在这里，不加在 PerformanceListSerializer」——那句已作废（绩效管理页 /Emp/emplist 补完计划 §2.1）：绩效页也要展示这三列、它们已上移到父类 PerformanceListSerializer（连同 settled_amount 的 null 形状文档）；本类因此不再重复声明、Meta.fields 也去掉它们（父类已含）——否则同一列会有两处声明、而两处必然漂移；⭐ 本类自己的新增只有 payable_net（应付净额）与 project（项目）；⭐（display-fixes 线）本类把父类已声明的 display_name 列进 Meta.fields（结算记录页要显示昵称）——那不是本类新增的字段、只是把父类已有的那个键放进出参。
    不把父类那 9 列 _EMPLIST_FIELDS 放进结算列表（那是绩效页的列）；出参多了 source_order_no：结算记录的关联订单列要显示来源单号，而本类与绩效列表共用 _PERFORMANCE_LIST_CORE_FIELDS；搜索改动只在绩效列表、结算页的搜索没动；DeductFeeField 越界返回 None、不再抛，所以不继承它不是为了避免死代码；父类再加列时本类不会自动跟上、要跟进就显式加进 fields。⚠️ project 取自 Order.project_text（权威值，D9：可空 FK 只是辅助）——所以 source="order.project_text" 而不是 order.project.name；⚠️ 不引入 N+1：视图的 get_queryset 已经 select_related("order", "user")，所以这一列与既有的 order_no 同一次 JOIN 取出（实测 SQL 条数见任务报告）；⚠️ project_text 列 blank=True（非 NULL）⇒ 无名项目回空串、不是 null（与「结算人空串原样返回」同口径：出参不把空串翻译成 null）。"""

    #: ⚠️ 声明式 `CharField` 而不是 `SerializerMethodField`：空串照原样出参。
    project = serializers.CharField(source="order.project_text", read_only=True)

    payable_net = PayableNetField()

    class Meta(PerformanceListSerializer.Meta):
        #: 基础 14 列沿用父类的**同一份常量**（不重抄）；结算列表自己的两列 + 结账留痕三列
        #: + ⭐ （display-fixes 线）追加的 `display_name`。
        fields = [
            *_PERFORMANCE_LIST_CORE_FIELDS,
            "payable_net",
            "project",
            *_SETTLED_SNAPSHOT_FIELDS,
            "settlement_method", "online_payroll_batch_id", "online_payroll_occupied",
            # ⭐ 结算记录页「结算对象人员」显示的是昵称（不是登录账号）。⚠️ 字段本身已在父类声明（display_name = SerializerMethodField()，
            # 口径 = apps/accounts/display.py::user_display_name）⇒ 这里只把它列进 Meta.fields：⛔ 不重复声明、⛔ 不重写口径
            #（DRF 只声明不列进 fields 会静默丢掉——本文件既有教训）。
            "display_name",
        ]


# ── P4-9：部分退款（spec §5.1）──────────────────────────────────────


class RefundRequestSerializer(serializers.Serializer):
    """设置累计退款额入参（spec §5.1）：{refund_total, reason, request_id}。必须过它再进领域层。⭐ 改口径（D-0312）：refund_total 是累计退款总额（绝对值）——填多少就是累计退了多少、可以改小、也可以填 0 清空；⛔ 不沿用旧键 amount 改语义（旧前端发 50 会被静默当成「累计 = 50」）：改名后旧前端缺必填键 ⇒ 1000（响亮），error_messages["required"] 写明要填什么。
    三条判据都不是风格问题（E-0010：请求来源的值进类型化的列或查询之前一律过序列化器）：
    ⚠️ refund_total 不给 min_value：refund_total >= 0 是 §5.1 的前置校验 #3、它的失败码是业务码 1008（REFUND_AMOUNT_INVALID）而不是 1000；在序列化器上写 min_value 会把它变成 1000、把「金额不对」和「格式不对」混成同一个码——判据只在领域层有一处（refunds.refund_total_denial；A5 起单笔与批量都从它取这几条判据 ⛔ 不在这里再判一遍）；它也不与 D-0015 冲突：D-0015 保护的是金额列不得加下界、这里判的是「新累计值不能为负」、0 是合法输入（=清空）。⚠️ refund_total 的位数上限是列宽（max_digits=12, decimal_places=2）：不校验的话超宽值会在 OrderRefund 落库时 DataError → HTML 500。⚠️ reason / request_id 的 max_length 也是列宽（255/64）：同一条理由；reason 同时是 OperationLog.reason 的写入来源（留痕那边还有一道截断兜底）。"""

    refund_total = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        error_messages={"required": "请填写累计退款总额"},
    )
    reason = serializers.CharField(max_length=255)
    request_id = serializers.CharField(max_length=64)


class BatchRefundSerializer(RefundRequestSerializer):
    """POST /orders/batch/refunds/ 的 body：继承单笔的字段与校验、只多三个键。⛔ 不要复制 RefundRequestSerializer 的字段定义（spec §4.1 第 7 行）；⚠️ order_nos 的两条理由与 BatchPriceSerializer 逐字相同（空数组/上限的文案由 order_batch.parse_order_nos 给）；refund_total / reason 仍是整批一份（spec §D13——「每笔各设置成这个累计额（可能调高也可能调低）」、弹窗里必须写明，见 §5.4）；⚠️ 三条逐笔判据（COMPLETED/金额/提成率）不在这里：它们在领域层（refunds.refund_total_denial / assert_sales_commissions_configured，经 order_writes.refund_total_write）⇒ 批量与单笔共用同一份、⛔ 这里不重写。
    ⭐ request_ids：幂等键逐笔（spec §D13）——长度必须与 order_nos 相等、且按下标对齐（第 k 个键配第 k 笔单，spec §D9）；执行时前端按上一次 dry_run 的合格子集过滤后一并送出（§D3）⇒ 两个列表是同一份子集的两个投影。
    ⚠️ validate() 跑在视图调 parse_order_nos 之前 ⇒ 这里只能读 self.initial_data["order_nos"] 的原始值（字段层只保证它是个列表、视图那一步的「空/重复/超限」收敛尚未发生）；⛔ 不要依赖视图的处理顺序、也不要假设它已经被收敛过。"""

    order_nos = serializers.ListField(child=serializers.CharField(), allow_empty=True)
    dry_run = serializers.BooleanField()
    #: 每笔一个幂等键（`max_length` 与单笔 `request_id` 同宽）。
    request_ids = serializers.ListField(
        child=serializers.CharField(max_length=64),
        error_messages={"required": "缺少 request_ids"},
    )

    def get_fields(self):
        """摘掉继承来的单笔 request_id（spec §4.1 第 7 行：批量只收 request_ids）。⚠️ 必须摘掉、不能只是「改成不必填」：继承来的 request_id 是必填的 ⇒ 批量请求（只送 request_ids）会被它一句「缺少 request_id」整批挡掉。
        ⛔ 不要给这段写「摘掉它能防止请求体里的 request_id 被静默忽略」——那条收益没有兑现：DRF 对未声明的键一律静默忽略，POST {"request_id": "x", "request_ids": [...]} 今天照样被接受、request_id 被丢掉（E-0011：一句能被反例证伪的理由比不写更糟）；真实收益只有两条：①必需的 request_id 不再挡掉批量请求；②该键不再出现在契约（OPTIONS 元数据）里。
        ⚠️ 用 get_fields() 摘、而不是在类体里写 request_id = None：后者能生效靠的是 SerializerMetaclass 把类体里任意同名属性都记进 known 从而跳过基类字段（rest_framework/serializers.py 的 _get_declared_fields）——那是一处实现副作用、读代码的人看不出「这一行是在删字段」。"""
        fields = super().get_fields()
        fields.pop("request_id", None)
        return fields

    def validate(self, attrs):
        # ⚠️ **基类今天 `validate()` 是 no-op**（`Serializer.validate` 直接 `return attrs`）
        # ⇒ 补这一句**行为不变**；留它是因为将来基类加校验时，少了这一句会被**静默跳过**
        # （子类覆写而不转调 = 基类判据在这个子类上失效，E-0021 的同族）。
        attrs = super().validate(attrs)
        raw_order_nos = self.initial_data.get("order_nos")
        # ⚠️ 只判「原始值确实是列表」的那一支：非列表（字符串 / 数字 / 缺键）由 `order_nos`
        # 的字段层或视图的 `parse_order_nos` 拒（各自有自己的文案）；在这里对非列表调
        # `len()` 会 TypeError ⇒ HTML 500（E-0010）。
        if isinstance(raw_order_nos, list) and len(attrs["request_ids"]) != len(raw_order_nos):
            raise serializers.ValidationError(
                {"request_ids": "退款请求键的数量必须与订单数量一致"}
            )
        return attrs


class RefundSerializer(serializers.ModelSerializer):
    """退款记录的一行（spec §7 的 GET /orders/{order_no}/refunds，每次退款一行）。⚠️ amount 是带符号的：kind=SET（D-0312）记的是差额（新累计−旧累计，可能为负数）、历史 PARTIAL/FULL/REVERSAL 行照旧；本序列化器原样上屏、⛔ 不取绝对值/不格式化（负数是一条真实资金事件）。
    ⚠️ order_no 是跨表读（OrderRefund 自己只有 FK）：用 source= 让列表页 select_related("order") 一次拿到、而不是每行再查一次库；与 PerformanceListSerializer.order_no 同一手法。"""

    order_no = serializers.CharField(source="order.order_no", read_only=True)
    # ⭐ 来源单号（D-0301）：退款列表「订单号」展示回退的取数前提
    # （前端 `source_order_no || order_no`）。跨表读同 `order_no` 的手法：
    # `select_related("order")` 已在列表 queryset 上，不产生 N+1。
    source_order_no = serializers.CharField(
        source="order.source_order_no", read_only=True
    )

    class Meta:
        model = OrderRefund
        fields = [
            "id",
            "order_no",
            # ⭐ （D-0301）追加在 `order_no` 之后，既有键的位置一字不动。
            "source_order_no",
            "kind",
            "amount",
            "reason",
            "operator",
            "request_id",
            "created_at",
        ]


# ── P4-11：人工修正（spec §7 的 POST 行）────────────────────────────


class ManualAdjustmentSerializer(serializers.Serializer):
    """人工修正入参（spec §7）：{amount, reason, request_id}。必须过它再写库。三条判据都不是风格问题（E-0010：请求来源的值进类型化的列之前一律过序列化器）：
    ⚠️ amount 只拒 0、负值是正常输入（spec §7 的「带符号」/D-0015）：amount 是冲减为负、补正为正的带符号调整量，「修正走新增负数调整记录」正是 §3.3 的业务方原话；⛔ 判据只在 validate_amount、且绝不写成 <= 0 / min_value——那会把「负应付的唯一出路」挡死；⛔ 不做模型层校验：spec §5.2 的将来预案需要 amount=0 的 MANUAL 行存在（「退单清空前接单人」那条）、所以「不得为 0」是本端点这一次请求的形状规则；📌 为什么不用 1008（退款额非法）：1008 的语义是「退款额非法」、拿它答「人工修正金额为 0」会让前端「退款金额不对」的分支误触发 ⇒ 用 1000。
    ⚠️ amount 的位数上限是列宽（max_digits=12, decimal_places=2）：不校验的话超宽值会在落库时 DataError → HTML 500。⚠️ reason / request_id 的 max_length 也是列宽（255/64）：同一条理由；reason 同时是 OperationLog.reason 的写入来源（留痕那边还有一道截断兜底）；request_id 是本表的幂等键（裁定 X：范围不是全局——同一个值已用于一次退款后仍可用于一次人工修正、两张表各自唯一）。"""

    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    reason = serializers.CharField(max_length=255)
    request_id = serializers.CharField(max_length=64)

    def validate_amount(self, value):
        """**只拒绝 0**（`if value == 0`）——⛔ 不要写成 `value <= 0`（D-0015）。"""
        if value == 0:
            raise serializers.ValidationError("调整金额不能为 0")
        return value


# ── 阿奇索店铺对接（spec §6.1）──────────────────────────────────────


class ShopBindingSerializer(serializers.ModelSerializer):
    r"""店铺对接（spec §6.1）。⚠️ 值域判据在序列化器、不在库层（OrderSourceSerializer 同一条纪律）：platform 是 SmallIntegerField、库层允许 999；本序列化器上有两道判据、别混成一句：①值域（ShopType 的九个取值，见 models.py）由 DRF 从模型 choices 自动生成的 ChoiceField 拦住——不是这里手写的 choices；②「本轮只接受闲鱼」是下面显式的 validate_platform（spec D1）：其它平台被拒（1000+中文 msg）、不静默存下一条永不被使用的配置。
    ⚠️ access_token 的「空 = 不改」语义只在 PATCH 生效（见 update()）：视图用 http_method_names 关掉了 PUT——spec §6.1 的端点表没有它、那是 router 默认多给的能力面、本资源没有 PUT 消费方（不是因为 PUT 会清空凭据：update() 对缺省键同样保留旧值）；POST 时空串合法（允许「先建行、后拿授权码」）。"""

    platform_label = serializers.SerializerMethodField()
    #: 平台级 AppId（**只读**，给前端弹层的 `#appId` 回填用）。⚠️ **不是密钥**——对标本来就把它
    #: 预填给租户看（`defAppid`）。**绝不要**照这个模式把 `AGISO_APP_SECRET` 也加进来
    #: （spec §9-S3：平台密钥一次都不进浏览器）。
    platform_app_id = serializers.SerializerMethodField()
    #: ⭐ E-0253：access_token 只写不读——它回到接口就是把租户在阿奇索侧的授权码明文下发给浏览器（修前列表逐行回显，照对标的「已知风险」登记不再成立：能不发的凭据就不发）。
    #: 读侧只有下面这个布尔；写侧语义逐字不变（PATCH 空/缺省 = 不改，见 update()）。形态照 PeerUpstreamBindingSerializer.password_set / XgjShopBindingSerializer.app_secret
    #:（本仓凭据字段的既有做法，本表是当初唯一漏网）。
    access_token_set = serializers.SerializerMethodField()
    access_token = serializers.CharField(
        max_length=512, write_only=True, required=False, allow_blank=True,
        help_text="阿奇索授权码（只写不读；响应只有 access_token_set 布尔）",
    )

    class Meta:
        model = ShopBinding
        fields = [
            "id", "platform", "platform_label", "platform_app_id", "shop_name", "shop_id",
            "order_source", "access_token", "access_token_set", "remark", "created_at", "updated_at",
        ]
        read_only_fields = ["shop_id", "created_at", "updated_at"]

    def get_platform_label(self, obj) -> str:
        return obj.get_platform_display()

    def get_platform_app_id(self, obj) -> str:
        """平台级 AppId（未配置时为空串）。"""
        return getattr(settings, "AGISO_APP_ID", "")

    def get_access_token_set(self, obj) -> bool:
        """只回答「配没配」，不回答「配的是什么」。"""
        return bool(obj.access_token)

    def validate_platform(self, value):
        if value != ShopType.XIANYU:
            raise serializers.ValidationError("本轮只支持闲鱼店铺对接")
        return value

    def update(self, instance, validated_data):
        # 「空 = 不改」：PATCH 里没带 access_token、或带了空串 ⇒ 保留原值。
        # 判据用 `not validated_data.pop("access_token", None)`（空串与缺省都算「不改」）——
        # 这是**刻意**的：授权码只能由租户重新授权时替换，不该被"只改备注"顺手清掉。
        token = validated_data.pop("access_token", None)
        if token:
            instance.access_token = token
        return super().update(instance, validated_data)


# ── 闲管家店铺对接（spec §5）──────────────────────────────────────

#: 推送给闲管家的两个回调地址的**路径**（域名由请求的 host 决定）。
#: ⚠️ 这两个路径**没有对应的视图**（spec §3-D5 / §12-1）：接收端是本轮之后的事，
#: 现在把它们登记下来只是为了「记录在案 + 让租户能填进闲管家」。
XGJ_ORDER_PUSH_PATH = "/api/business/xgj/push/order/"
XGJ_PRODUCT_PUSH_PATH = "/api/business/xgj/push/product/"


class XgjShopBindingSerializer(serializers.ModelSerializer):
    """闲管家店铺对接（spec §5）。三件事写清楚、免得后来者按「阿奇索那张表」的习惯改坏它：
    ①app_secret 是 write_only（spec §9-S1）：接口永不回显它、页面只看得到 app_secret_set（布尔）；⚠️ 不要为了"方便租户核对"把它改成可读——D-0061 对阿奇索平台密钥立的就是这条规矩、租户自己的密钥同理（业务方二选一确认）。
    ②app_secret 的「空 = 不改」只在 PATCH 生效（update()，spec §9-S4）；视图关掉了 PUT（同 ShopBindingViewSet 的理由）、所以没有"非 partial 覆盖"这条歧义路径。③两个推送 URL 是服务端算出来的、只读（spec §3-D5）：前端传什么都不看；它们随请求的 host 变——换租户域名后旧值不会自动更新、要重新保存一次那一行（spec §12-2）。"""

    #: `app_secret` 的**只读替身**：只回答「配没配」，不回答「配的是什么」。
    app_secret_set = serializers.SerializerMethodField()
    app_secret = serializers.CharField(
        max_length=128, write_only=True, required=False, allow_blank=True,
        help_text="闲管家应用的 AppSecret（只写不读）",
    )

    class Meta:
        model = XgjShopBinding
        fields = [
            "id", "shop_name", "app_key", "app_secret", "app_secret_set",
            "order_push_url", "product_push_url", "remark", "created_at", "updated_at",
        ]
        read_only_fields = [
            "order_push_url", "product_push_url", "created_at", "updated_at",
        ]

    def get_app_secret_set(self, obj) -> bool:
        return bool(obj.app_secret)

    def validate_app_key(self, value):
        """AppKey 不能是空白（只有空白的 AppKey 一定调不通上游、存下来只会得到一个 1015）。⚠️ 用 strip() 之后判空：租户是从闲管家控制台复制粘贴的、首尾空格与换行很常见，
        而带换行的 appid 会让上游回 100001（看起来像"密钥错了"、实际是粘贴带了换行）；这里 strip 之后存进去的也是 strip 过的值——validate_* 的返回值才会被写入
        （DRF serializers.py 的 run_validation 把校验后值放回 validated_data）。"""
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("AppKey 不能为空")
        return cleaned

    def validate_app_secret(self, value):
        """同样 strip（理由见 `validate_app_key`）；空串的语义由 `update()` 决定。"""
        return (value or "").strip()

    def _apply_push_urls(self, instance) -> None:
        """按本次请求的 host 生成两个推送 URL（spec §3-D5）。⚠️ 取不到 request（例如 shell/迁移里直接保存）时留空而不是抛异常：
        序列化器的 create/update 不该因为「没有 HTTP 请求」就炸掉；context["request"] 由 EnvelopeModelViewSet 的 get_serializer_context() 提供。"""
        request = self.context.get("request")
        if request is None:
            return
        instance.order_push_url = request.build_absolute_uri(XGJ_ORDER_PUSH_PATH)
        instance.product_push_url = request.build_absolute_uri(XGJ_PRODUCT_PUSH_PATH)

    def create(self, validated_data):
        """一次 save() 落库（先算好两个 URL 再存）。⚠️ 早先的写法是「super().create() 建行 → 补 URL → 第二次 save(update_fields=…)」、注释还写着"不留半行"——那句是假的：本工程没设 ATOMIC_REQUESTS（config/settings.py 里没有这一项）、super().create() 已经把行提交了、第二次 save() 若失败就真的留下一条没有推送 URL 的半行；现在把 URL 在 save() 之前算好（build_absolute_uri 不需要 pk）、所以「一次 save、不留半行」这句话才是真的。
        ⚠️ 代价（如实登记）：这里不走 ModelSerializer.create()，所以它自带的 raise_errors_on_nested_writes 与 m2m 写入处理都不在这条路上；本表现在没有 m2m、也没有可写的嵌套字段（order_source 传 dict 会在入参处被挡成 1000、有测试）、所以无现状影响；将来给这张表加 m2m 或嵌套可写字段时，必须回到 super().create()（tests/test_xgj_shop_binding.py 的畸形写入体用例是这条的兜底）。"""
        instance = XgjShopBinding(**validated_data)
        self._apply_push_urls(instance)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        """同样是一次 save()：先改内存里的字段（含两个 URL）、再交给 DRF 落库。⚠️ super().update() 只 setattr + save()，而两个 URL 在 read_only_fields 里、永远不会出现在 validated_data 中
        ⇒ 先算好的值不会被覆盖。"""
        # 「空 = 不改」（spec §9-S4）：PATCH 里没带 app_secret、或带了空串 ⇒ 保留原值。判据是 if secret: 而不是 if secret is not None:——这是刻意的：
        # 页面上的密码框留空表示「不动它」，而它回显不了，所以「清空密钥」这个操作在界面上根本没法表达（要换密钥就直接填新的）。
        secret = validated_data.pop("app_secret", None)
        if secret:
            instance.app_secret = secret
        # ⚠️ 必须先把两个 URL 从 validated_data 里摘掉：DRF 的 ModelSerializer.update() 是「对 validated_data 里每个键 setattr 然后 save()」
        #（rest_framework/serializers.py:1058-1064），所以只要这两个键混进来，它们就会把下面 _apply_push_urls() 刚算好的值覆盖掉再落库。
        # 真跑过的反例（M8）：把 read_only_fields 里这两个键删掉之后，PATCH 能存进 https://evil.example/x（而 POST 仍然安全——create() 是构造对象后覆写，顺序正好相反）。今天 read_only_fields 挡着、所以不可利用；但「承重的那道防线」必须自己站得住，不能靠另一个字段声明兜着（E-0011 同族：没有依据的兜底比不写更糟）。
        validated_data.pop("order_push_url", None)
        validated_data.pop("product_push_url", None)
        self._apply_push_urls(instance)
        return super().update(instance, validated_data)


# ── P4FU-B：撤销结账 / 删除记录（D-0072）────────────────────────────
# ⚠️ 本段**只追加**在文件尾部（另一条线正在改 `get_allowed_transitions` 那一段），
# 不要为此重排上面的任何既有代码。


class RevokeSettleRequestSerializer(SettleRequestSerializer):
    """撤销结账入参（D-0072 ②）：结账的 performance_ids + 必填的 reason。⚠️ 继承 SettleRequestSerializer 而不是重抄：performance_ids 的三条判据（allow_empty=False / max_length=1000 / 元素是整数）与正向结账是同一件事、重抄一份会让「加一条入参约束」必然漏一处。
    ⚠️ reason 从「可选」变成「必填且非空」是 D-0072 的明文（「撤销结账必须填原因，并写进订单日志」）——所以覆盖父类的 required=False, allow_blank=True, default=""；allow_blank 默认 False ⇒ 前端弹层里「什么都没输入」送出的空串会被拒成 1000、而不是当成「没填原因」静默通过。
    ⚠️ max_length=255 = AuditLog.reason 的列宽：超长会在留痕落库时 500（E-0010）。"""

    reason = serializers.CharField(max_length=255)


class PerformanceIdsSerializer(serializers.Serializer):
    """删除记录的入参（D-0072 ①）：只有 performance_ids。三条判据逐条对着 SettleRequestSerializer.performance_ids 写（它们不是风格问题）：
    ⚠️ allow_empty=False 必需：空列表在领域层是「一批都不删」的空操作，而命令式端点收到空列表更像客户端的 bug（前端在勾 0 行时就该提示）⇒ 入口拒成 1000。
    ⚠️ max_length=1000 防「未校验的值直进 filter()」：没有上界时几十万元素的列表会撞 PostgreSQL 的绑定参数上限 ⇒ HTML 500（E-0010）。⛔ 不给元素加 min_value：主键不是金额、0/负数查不到就是「不存在」，由领域层逐 id 说明（同 SettleRequestSerializer 的口径）。"""

    performance_ids = serializers.ListField(
        child=serializers.IntegerField(),
        allow_empty=False,
        max_length=1000,
        error_messages={
            "required": "缺少 performance_ids",
            "empty": "performance_ids 不能为空",
            "not_a_list": "performance_ids 必须是列表",
            "max_length": "一次最多删除 1000 条绩效",
        },
    )


class JidanpuSyncEventSerializer(serializers.ModelSerializer):
    """台账行（只读；spec §9.2 第 3/4 块的数据源）。⚠️ payload 故意不进列表响应（spec §6「events 出参不含 payload」、spec §8-S1）：它是 spec §4.4 里唯一无上界的 JSON 兜底字段（推送内容/拉到的字段摘要），而 spec §9.2 的两个表一个字段都用不到它；无条件回给任何已登录用户会把 §8-S1 的「密码不进浏览器」从结构性保证降级成「对将来每个写入方的契约」——下一期任一写入方把上游请求体摘要塞进 payload、浏览器就会收到它、而列表用例不会红；要看明细就另开详情端点（按主键单取、带自己的鉴权与出参白名单）。
    守这条的是 tests/test_jidanpu_sync_lists.py 的 test_the_events_row_exposes_exactly_the_contracted_keys（恰好 10 个键）。"""

    class Meta:
        model = JidanpuSyncEvent
        fields = [
            "id", "direction", "kind", "target_type", "target_ref",
            "status", "attempts", "reason", "created_at", "processed_at",
        ]
        read_only_fields = fields

#: 台账的两个词表（spec §4.4）：**唯一出处**都是模型自己的 `TextChoices`，这里只派生、不重抄。
#: `status` 与 `direction` 都走这条口径——重抄一份字面值的话，模型加一个取值后接口会
#: **静默只认旧值**（回 `1000「X」不是合法选项`，看着像拼写错、实际是词表漂移）。
JIDANPU_EVENT_STATUSES = tuple(JidanpuSyncEvent.Status.values)
JIDANPU_EVENT_DIRECTIONS = tuple(JidanpuSyncEvent.Direction.values)


class JidanpuSyncEventQuerySerializer(serializers.Serializer):
    """台账列表的查询参数（spec §6）。请求来源的值一律过序列化器（AGENTS.md §5-E）。⚠️ status 支持逗号分隔的多值：页面「待处理」表要一次拿 failed,skipped 两种（spec §9.2 第 4 块点名的就是这一对）；未知值 ⇒ 1000（不静默返回空列表——「空结果不等于没有数据」，AGENTS.md §5-E）。
    ⚠️ 空串按「没传」处理（照 settlement_views._params 的既有口径：前端清空下拉会送 ?status=）。"""

    status = serializers.CharField(max_length=64, required=False, allow_blank=True)
    direction = serializers.ChoiceField(
        choices=JIDANPU_EVENT_DIRECTIONS, required=False, allow_blank=True
    )
    kind = serializers.CharField(max_length=32, required=False, allow_blank=True)

    def validate_status(self, value):
        parts = [part for part in (value or "").split(",") if part]
        unknown = [part for part in parts if part not in JIDANPU_EVENT_STATUSES]
        if unknown:
            raise serializers.ValidationError(f"未知的同步状态：{','.join(unknown)}")
        return parts


# ── 上下游订单互通（spec `docs/specs/2026-09-25-upstream-downstream-order-sync-design.md` §8）──
class PeerBindRequestSerializer(serializers.Serializer):
    """下家发来的握手请求（spec §5.1 第 3 步）。⚠️ token_out 是下家生成、给我（上家）用的；我存它的哈希（spec §9-S3 / 裁决 R15）。"""

    account = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, write_only=True)
    peer_base_url = serializers.CharField(max_length=255)
    token_out = serializers.CharField(max_length=128)


class PeerUpstreamBindingSerializer(serializers.ModelSerializer):
    """页面列表的一行（spec §6.1）。⚠️ password / token_in_hash / token_out 永不回显（spec §9-S4）：只发布尔 password_set、页面靠它显示"要不要重填"；⭐ C1（M-1）：password_set 改读模型布尔列（迁移 0095 把存量明文回填成布尔并清空）——明文列已无任何读者、序列化器是最后一个被改掉的读点。
    ⭐ 多两键 hall_sync_enabled / hall_price_percent——绑定页编辑弹窗的回填出口（老行读出来就是模型默认的 False / 100，⛔ 不是 null：两列非空带默认值）；它们只读：写入走下面那个 PeerBindingSettingsSerializer（⛔ 不让本序列化器变成万能写口——凭据/绑定关系一个都不许从这里改）。
    ⭐ C2（Z22）：只读布尔 uses_insecure_http——存量 http:// 绑定的风险展示（新绑定已在视图里被拒；老行不打断、但要让人看得见它还在明文跑）。"""

    #: `password_set` 现在直接映射模型同名列（`ModelSerializer` 自动取列），
    #: ⛔ 不再要 `SerializerMethodField`（那是读明文列的旧口）。
    uses_insecure_http = serializers.SerializerMethodField()

    #: ⭐ 复评修 1：对标第 4 列是「绑定操作人」（一个人），而 bound_by 由 DRF 按 FK 的 PK 序列化 ⇒ 页面只能渲染出 3 这样的数字。这里补一列显示名，
    #: ⛔ 不替换 bound_by（它同时是「销售提成归属人」的来源，别的消费方按 id 取）。命名照本文件的 FK-取人先例（hall_publisher_name / taker_name / log_user_name）：
    #: 本行不是 User 行 ⇒ 用 <FK 字段名>_name，而不是裸 display_name（那个先例里指的是本行那个人自己，ActorSerializer / PerformanceListSerializer 同形）。取值口径 = apps.accounts.display.user_display_name（「怎么显示一个人」的共用出处：昵称优先、空串回落登录账号），⛔ 只把空串当没有（不另发明 strip 判据）。复评：本列与「订单 sales_agent 快照」曾经各写一遍这个表达式 ⇒ 同一个人两个名字（页面「测试客服甲」/ 订单「kefu-a」）；现在两处都走这个 helper。⚠️ 取数侧必须 select_related("bound_by")（PeerBindingListView.get 已加），否则一页 N 行就是 N 条查询（与 hall_publisher_name 同一条纪律）。
    bound_by_name = serializers.SerializerMethodField()

    class Meta:
        model = PeerUpstreamBinding
        fields = [
            "id", "peer_name", "peer_base_url", "peer_account",
            "password_set", "uses_insecure_http",
            # ⭐ D-0359：收款查询密码的「已配」布尔（⛔ 明文列 `receipt_query_password`
            # 本身永不进 fields——write_only 语义由「根本不在序列化器里」保证，
            # 与 `password` / `token_*` 同一套口径）。
            "receipt_query_password_set",
            "bound_by", "bound_by_name", "bound_at",
            "last_ok_at", "last_error", "last_error_at", "consecutive_failures",
            "hall_sync_enabled", "hall_price_percent",
        ]
        read_only_fields = fields

    def get_uses_insecure_http(self, obj) -> bool:
        """存量风险标志：域名仍是 `http://` 明文（新绑定已拒 `http://`，老行如实展示）。"""
        return obj.peer_base_url.startswith("http://")

    def get_bound_by_name(self, obj) -> str:
        """绑定操作人的显示名（first_name 空则回落登录账号）。bound_by 是非空 FK（PROTECT、无 null=True）⇒ 这里刻意不写「没有操作人」的分支：那会是永不执行的死代码（E-0024 同族、别让注释说假话）。
        ⚠️ 口径走共用 helper apps.accounts.display.user_display_name——同一事实只有一处实现（⛔ 不要在这里再写一遍 first_name or get_username()）。"""
        return user_display_name(obj.bound_by)


class PeerBindCreateSerializer(serializers.Serializer):
    """页面弹窗那三个字段（照对标，spec §2.2/§11.2）。⚠️ peer_name 的 max_length=20 照对标（homeCompany）；本仓 OrderSource.name 只有 10 字 ⇒ 截断发生在落单时（spec A11）、⛔ 不在这一层截。
    ⭐ C7（L-15）：可选布尔 replace——同一上家域名已有另一条活绑定时的显式顶替开关（缺省 False = 拒绝并指向旧绑定；同 (域名, 子账号) 的幂等重绑不需要它、那条路不走同 host 闸）。"""

    peer_name = serializers.CharField(max_length=20)
    peer_base_url = serializers.CharField(max_length=255)
    peer_account = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, write_only=True)
    replace = serializers.BooleanField(required=False, default=False)


class PeerUpstreamBindingDetailSerializer(PeerUpstreamBindingSerializer):
    class Meta(PeerUpstreamBindingSerializer.Meta):
        fields = [*PeerUpstreamBindingSerializer.Meta.fields, "game_rules", "direct_taker_assignment_enabled"]
        read_only_fields = fields


class _GameRuleInteger(serializers.IntegerField):
    def to_internal_value(self, data):
        if type(data) is not int:
            self.fail("invalid")
        return super().to_internal_value(data)


class _UpstreamGameRuleSerializer(serializers.Serializer):
    game_id = _GameRuleInteger(min_value=1, max_value=2**63 - 1)
    hall_price_percent = _GameRuleInteger(min_value=1, max_value=100, allow_null=True)


class UpstreamGameRulesSerializer(serializers.ListSerializer):
    def __init__(self, **kwargs):
        super().__init__(child=_UpstreamGameRuleSerializer(), allow_empty=False, **kwargs)

    def validate(self, rules):
        game_ids = {rule["game_id"] for rule in rules}
        if len(game_ids) != len(rules):
            raise serializers.ValidationError("同一个游戏只能配置一次")
        existing = set(
            Game.objects.filter(pk__in=game_ids, deleted_at__isnull=True)
            .values_list("pk", flat=True)
        )
        if game_ids != existing:
            raise serializers.ValidationError("所选游戏不存在或已删除，请刷新游戏列表")
        return rules


class PeerBindingSettingsSerializer(serializers.Serializer):
    """省略的设置保持原值，空收款查询密码也保持原值。"""

    hall_sync_enabled = serializers.BooleanField(required=False)
    hall_price_percent = serializers.IntegerField(
        required=False, min_value=1, max_value=100
    )
    game_rules = UpstreamGameRulesSerializer(required=False, allow_null=True)
    direct_taker_assignment_enabled = serializers.BooleanField(required=False)
    receipt_query_password = serializers.CharField(
        required=False, write_only=True, allow_blank=True, max_length=255
    )
    receipt_query_precheck_ok = serializers.BooleanField(required=False)
