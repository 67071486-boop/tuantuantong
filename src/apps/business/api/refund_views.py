"""部分退款的两个端点（spec §5.1 / §7；P4-9）：`POST /api/business/orders/{order_no}/refunds`（设置累计退款额，`order_writes.refund_total_write`；判据在 `refunds.py`）与 `GET` 同路径（该订单的退款记录，每次一行）——门都是 `has_node("order.online_refund")`。**为什么两个方法住同一个 `RefundView`**：spec §7 的端点表把它们写成**同一条路径**，按「一事一个入口」拆成两个视图只会让 URL 里出现两条字面相同的 `path()`（Django 也能跑，但「同一资源」的事实就从代码里消失了）；`ViewSet.as_view({"get": ..., "post": ...})` 是本仓显式 `path()` 手法的既有形状。
⭐ **闸门是 RBAC 节点 code**（Ruling T / AGENTS.md §5 硬规则 1）：`has_node("order.online_refund")`——对标目录 **`1205 在线订单退款管理界面及操作`**（`apps/rbac/catalog.py` 里 `benchmark_id = 1205` 那一行；⚠️ **不写行号**——裁剪后会漂，按 id/code 指认；`/Order/OnlineRefundOrder`）。spec §10 #10 业务方裁定「**财务 + 客服都能退**」现在**靠权限点表达**：把该节点授予哪些角色是**权限配置**的事（D-0030 之后没有「财务」这个角色）；⛔ **不写 `has_role(...)`**，也不新增 `HasPermission`（Django codename，已冻结为 legacy，D-0028）。⚠️ **闸门天然排在「解析参数」之前**：DRF 的 `initial()`（认证 → 权限）先于 `get()`/`refund()`，未授权者不会因为「参数畸形」拿到 `1000`，也无法从错误差异里探测端点行为。⚠️ **业务失败一律 200 + 业务 `code`**（`1000` / `1007` / `1008` / `1009` / `1010`），未认证 **401**，已认证无权限 **403**。
⚠️ **`GET` 与 `POST` 都过 `scope_queryset`**（spec §6.1 表格逐条列明为 ✅），两者都带**按游戏权限**的 `scope_filter`（业务方裁定的可见范围口径，规则本体在 `apps/business/game_scope.py`）——⚠️ **两条的字段路径不同**（`game_scope(field)` 的 `field` 按**各自的模型**选）：退款记录是 `OrderRefund`、它**没有** `game` 列 ⇒ 走 `order__game_id`；取订单那一次是 `Order` 本身 ⇒ 走 `game_id`。写错字段路径会 `FieldError` 响亮报错（不会静默不过滤），但方向仍要选对。
"""
from decimal import Decimal

from rest_framework import viewsets

from apps.business.api.base import EnvelopePagination
from apps.business.api.serializers import (
    RefundRequestSerializer,
    RefundSerializer,
)
from apps.business.game_scope import game_scope
from apps.business.models import Order, OrderRefund
from apps.business.order_writes import WriteContext, refund_total_write
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node, nodes_of


#: 闸门节点 code。⚠️ 写成**模块常量**而不是直接嵌字面量：接线位点表（RBAC Wiring Map，
#: `apps/rbac/wiring.py`）的 T2 判据会解析锚点方法体、并向下解析**一层模块常量**来找这个 code
#: （见 `docs/specs/2026-09-21-rbac-wiring-map-design.md` 的维护说明）。
REFUND_NODE = "order.online_refund"


class RefundView(viewsets.GenericViewSet):
    """订单退款：`GET`（记录列表）/ `POST`（部分退款）。"""

    pagination_class = EnvelopePagination
    serializer_class = RefundSerializer
    denied_message = "你没有订单退款权限"

    def get_permissions(self):
        """闸门 = `has_node(REFUND_NODE)`（spec §6）。⚠️ 写成**方法**而不是类级 `permission_classes`：接线位点表的 T2 判据只解析**方法体**（方法里出现 `has_node(...)` 调用或 `.permission_classes` 才算「权限装配」），全仓既有锚点（`RoleAuthorityViewSet.get_permissions` 等）都是这个方法名；行为与 `permission_classes = [has_node(REFUND_NODE)]` **一致**——DRF 默认的 `get_permissions()` 本身就是把 `permission_classes` 实例化。
        """
        return [has_node(REFUND_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（spec §17.4 #4：403 有多个来源，断言必须能区分「无权限」与「CSRF 失败」等其他 403）。⚠️ **必须转调 `super()`**：DRF 的 `permission_denied` 在抛 `PermissionDenied` 之前先判「有没有认证成功」——未认证时它抛 `NotAuthenticated`（401），自己直接 `raise PermissionDenied` 会把未认证也变成 403、破坏 spec §7 首段的分层要求（与 P4-7 的 `SettleView` / P4-8 的 `FinanceReadPermissionMixin` 同一写法）。
        """
        super().permission_denied(request, message=self.denied_message, code=code)

    def get_queryset(self):
        """该订单的退款记录。**按订单号筛**，再过一个数据权限卡点。⚠️ 用 `order__order_no` 而不是主键：订单号是全线对外的标识符（与大厅 / 抢单 / 通知同一口径），路径参数里给的也是它；`select_related("order")` 是 `RefundSerializer.order_no` 的取数路径（跨表读，不 `select_related` 就是每行一次额外 SQL）。
        """
        qs = OrderRefund.objects.select_related("order").filter(
            order__order_no=self.kwargs["order_no"],
            # ⭐ D-0144：已软删单的退款记录不出现在这里（跨关系 JOIN 不经过 `Order`
            #    的默认 manager ⇒ 必须显式补，spec §4.3-C2）。
            order__deleted_at__isnull=True,
        )
        # 数据权限的**唯一卡点**（spec §6.1 的「退款记录列表」一行 + 业务方的按游戏权限口径）：`OrderRefund` 没有 `game` 列 ⇒ 字段路径走 `order__game_id`。
        return scope_queryset(
            self.request.user, qs, scope_filter=game_scope("order__game_id")
        )

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def refund(self, request, *args, **kwargs):
        """部分退款（spec §5.1）。入参先过序列化器，再进领域核心 `refund_total_write`。⭐ A5（spec §D5）：判据与写入搬进 `apps.business.order_writes`（单笔与批量共用一份），本方法只做「取对象 → 构造 `WriteContext` → 调核心 → 包信封」；响应形状与搬迁前**逐字相同**（成功 `RefundSerializer(record).data`、失败 200 + 业务码）。
        """
        # 第 1 步：入参校验（格式 / 位数 / 列宽 ⇒ 200 + `1000`）。
        serializer = RefundRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 前置校验 #1：订单存在、在本租户 schema 内、且在自己的**游戏可见范围**内 ⇒ `1000`。⚠️ 租户上下文来自**连接**（当前 schema），绝不接受请求参数传入（AGENTS.md §4）；游戏可见范围来自认证态（`games_of(user)`），同样不接受请求参数。⭐ D-0144：走默认 manager ⇒ **已软删的单取不到** ⇒ 已删单不能再退款。
        order = scope_queryset(
            request.user,
            Order.objects.filter(order_no=kwargs["order_no"]),
            scope_filter=game_scope("game_id"),
        ).first()
        if order is None:
            return api_response(
                data=None, msg="订单不存在或不可见", code=ErrorCode.VALIDATION
            )

        # ⚠️ 退款**不读、也不校验**请求体的 `actor`（`RefundRequestSerializer` 里没有这个字段）⇒ `ctx.actor` 与 `ctx.log_actor` **同源**、都取认证态；留痕的 `actor_id` 取认证态主键（spec §5.9）。
        # `nodes` 一并装进去（本动作今天不读它，但 `WriteContext` 的契约是「调用方把需要的值一次装齐」）。
        actor = getattr(request.user, "username", "") or ""
        ctx = WriteContext(
            actor=actor,
            log_actor=actor,
            log_actor_id=request.user.pk,
            nodes=nodes_of(request.user),
            user=request.user,
        )
        outcome = refund_total_write(
            order,
            refund_total=data["refund_total"],
            reason=data["reason"],
            request_id=data["request_id"],
            ctx=ctx,
            dry_run=False,
        )
        if not outcome.ok:
            # 预期失败（1007/1008/1009/1010）：整笔已回滚（见 `refunds._write_refund_total`
            # 的事务说明），这里只包信封——与搬迁前 `except RefundError` 那支逐字同形。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)

        # ⭐ 差额 = 0 = 全空操作：`record` 是 `None`，⛔ 不能无条件过
        #    `RefundSerializer`（那会 KeyError/序列化崩 = 500）。显式分支只回三个键，
        #    前端按「成功 + 关层 + reload」处置（`changed:false` 只是如实说明没改动）。
        record = outcome.detail.get("record")
        if record is None:
            row = outcome.detail["order"]
            return api_response(
                data={
                    "order_no": row.order_no,
                    "refund_fee": f"{Decimal(row.refund_fee):.2f}",
                    "changed": False,
                }
            )
        return api_response(data=RefundSerializer(record).data)
