"""某条绩效的**调整记录链**：`GET` + `POST /api/business/performances/{performance_id}/adjustments`（spec §7 端点表两行：`GET` 财务/客服读链、含 `settle_status`——P4-8；`POST` 财务人工修正、新增一条 `kind=MANUAL` 记录——P4-11）。单独成模块：`settlement_views.py` 是结算域的**列表**（绩效 + 待归属），本端点的主语是**绩效**（挂在 `/performances/{id}/` 下）；也不塞进 `performance_views.py`——那个 ViewSet 是「只读 + `/adjust` + `/review`」，再加一条与结算域耦合的动作会让两个域在同一处生长。
GET 与 POST 住同一个 `AdjustmentListView`（裁定 W）：spec §7 把它们画成**同一条路径**，全仓先例是「同一条路径一个 view 两个方法」（`RefundView`）；⛔ 不为同一条路径再放第二个 `path()`——两条同串 `path()` 在方法不匹配时的回落语义**未经核实**，不引入需要依赖该语义才能工作的设计（AGENTS §5）。闸门是两个不同的 RBAC 节点（spec §6 的表 / 裁定 W）：读 = `finance.payroll`（对标 `80 打手与客服工资`），人工修正 = **同结账** = `finance.change_settlement_status`（对标 `84 更改结算状态`）；分派走 `get_permissions()` 按 `self.action`，**403 的 `msg` 也分派**（spec §17.4 #4：`msg` 必须可辨）——`denied_message` 写成 **property**，基类 `FinanceReadPermissionMixin.permission_denied` 读的就是它。
只读三条口径：闸门 `FinanceReadPermissionMixin`（`has_node("finance.payroll")`）；数据权限过 `scope_queryset`（spec §6.1 第三行；写路径取绩效时同样过一次）；分页 `EnvelopePagination`。⚠️ `GET` 的 `performance_id` **只当筛选键**、不另查「绩效是否存在」——查不到就是空链，与「这条绩效没有任何调整记录」同一形状（口径实施期拟定、spec §7 没写；理由见 `tests/test_settlement_api.py::AdjustmentChainTests` 用例的 docstring）；⚠️ `POST` 的 `performance_id` **则要判存在**：不存在（或不可见）⇒ `VALIDATION(1000)`，照 `refund_views.py:97-99` 的形状。⚠️ 序列化器：只读那个在本模块（照 `notification_views.py` / `my_orders_views.py` 先例）；人工修正的**入参**序列化器在 `api/serializers.py::ManualAdjustmentSerializer`（与 `RefundRequestSerializer` 同一处——它是「请求形状」的规格，不是本模块出参）。
"""
from django.db import IntegrityError, transaction
from rest_framework import serializers, viewsets

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.business.api.serializers import ManualAdjustmentSerializer
from apps.business.api.settlement_views import FinanceReadPermissionMixin
from apps.business.game_scope import game_scope
from apps.business.models import Performance, PerformanceAdjustment, SettleStatus
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node

#: 人工修正的闸门节点 code：**同结账**（spec §6 的表「人工修正调整记录（`MANUAL`）」
#: 一行 + 裁定 W）。`apps/rbac/catalog.py` 里 `benchmark_id = 84` / `code = "finance.change_settlement_status"`
#: 那一行（⚠️ **不写行号**：裁剪会让它漂，按 id/code 指认），对标 **`84 更改结算状态`**。
MANUAL_ADJUSTMENT_NODE = "finance.change_settlement_status"
#: `PerformanceAdjustment` 上那条具名唯一约束（裁定 V）——`1010` 的**唯一**来源。
DUPLICATE_REQUEST_CONSTRAINT = "uniq_perf_adjustment_request_id"


class PerformanceAdjustmentSerializer(serializers.ModelSerializer):
    """调整记录的一行（spec §3.3 的字段 + `settle_status`）。⚠️ `performance_id` 显式声明：模型上它是 Django 的隐式 `attname`，**不在** `ModelSerializer` 自动生成的集合里，不显式声明会在 `fields` 里报 `ImproperlyConfigured`；⚠️ `request_id` 只读（裁定 V）：退款联动生成的行为 `None`，人工修正行为客户端给的值。"""

    performance_id = serializers.IntegerField(read_only=True)
    request_id = serializers.CharField(read_only=True)

    class Meta:
        model = PerformanceAdjustment
        fields = [
            "id",
            "performance_id",
            "kind",
            "amount",
            "reason",
            "operator",
            "settle_status",
            "settled_at",
            "created_at",
            "request_id",
        ]


class AdjustmentListView(FinanceReadPermissionMixin, viewsets.GenericViewSet):
    """调整记录链（spec §7）：`GET` 读链（财务 / 客服），`POST` 人工修正（财务，同结账）。"""

    pagination_class = EnvelopePagination
    serializer_class = PerformanceAdjustmentSerializer
    http_method_names = ["get", "post", "head", "options"]

    @property
    def denied_message(self):
        """两个方法**两句话**（spec §17.4 #4：403 有多个来源，`msg` 必须能分辨方向）。⚠️ 写成 **property** 而不是类属性：`self.action` 要到 `initialize_request()` 之后才有值，而 `FinanceReadPermissionMixin.permission_denied` 读的是 `self.denied_message`——property 会被优先取到，类属性会在两个方法上给出同一句（前端分不出「读被拒」还是「写被拒」）。"""
        if self.action == "create":
            return "你没有人工修正绩效调整记录的权限"
        return "你没有查看绩效调整记录的权限"

    def get_permissions(self):
        """`POST` 用**另一个**节点（同结账），`GET` 用读节点（spec §6 的表）。⚠️ 两个方法**两个权限节点**是这条用例的判据（`tests/test_adjustment_api.py` 的 M9）：两边都用同一个节点的话，「持写节点者不能读」那条会红。"""
        if self.action == "create":
            return [has_node(MANUAL_ADJUSTMENT_NODE)()]
        return super().get_permissions()

    def get_queryset(self):
        # ⭐ D-0144（「未结账调整记录列表」那一半）+ R-8a：已软删单的绩效行、以及**父绩效行自己**被软删（订单还活着）时，调整记录都不出现在列表里——跨关系 JOIN 不经过 `Order` 的默认 manager，且此前只判 `performance__order__deleted_at`、漏判 `performance__deleted_at` ⇒ 打一个已软删绩效 id 仍能读到它的调整链。
        # 判据：`tests/test_performance_row_soft_delete_read_paths.py::AdjustmentChainExclusionTests`（删前可见 / 删后不可见 + 活行对照）。
        qs = PerformanceAdjustment.objects.filter(
            performance_id=self.kwargs["performance_id"],
            performance__order__deleted_at__isnull=True,
            performance__deleted_at__isnull=True,
        )
        # 数据权限的**唯一卡点**（spec §6.1）：基线 pass-through，但接线必须在。
        # ⭐「按游戏权限收窄」：`PerformanceAdjustment` 经 `performance.order` 指到游戏 ⇒ `performance__order__game_id`（与下面取绩效那处**两条不同的路径**，按各自的模型选；规则本体在 `apps/business/game_scope.py`）。
        return scope_queryset(
            self.request.user, qs, scope_filter=game_scope("performance__order__game_id")
        )

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    def create(self, request, *args, **kwargs):
        """人工修正（spec §7 的 POST 行）：新增一条 `kind=MANUAL` 的调整记录。**服务端决定的事，请求一个字都改不了**（裁定 W / M4）：`kind` 恒为 `MANUAL`、`refund` 恒为 `None`（人工修正不挂任何退款事件）、`operator` = `request.user.username`；`settle_status` **强制 `未结账`**——修正必须**重新走结账**，**即使绩效行已结账**（§3.3 的设计：否则已结账绩效上的修正额**永远不会被结账**，人工修正的语义就是「这笔钱还没付」）。
        ⭐ 但「创建时写未结账」**不等于**「它永远停在未结账」（D-0280）：`apps/accounts/payout.py::payout_adjustment` 的**冲账**在同一次请求的同一事务里紧随创建之后跑，只要这条调整挂的是 **MAIN 行 ∧ 有归属人 ∧ `amount != 0` ∧ 库里此刻 `settle_status=已结账` ∧ `settled_by == "system"`**，钱就在那一刻**真的进了钱包** ⇒ 它会把这条调整改判成「已结账」并把 `Performance.settled_amount` 同步加上这笔金额。⛔ 本端点这一行**不改**（仍在创建时写 `未结账`）：那一支的判据在 `payout_adjustment` 里**只有一处**，这里跟着再判一次就是两处判据、必漂移（E-0021 同族）；⚠️ 其余各支（SALES 行 / 未结账行 / 手工结账行 / `amount=0`）**照旧停在未结账**等下一次结账——那才是「修正必须重新走结账」的原意。
        **幂等**（裁定 X）：唯一约束是 `1010` 的**唯一**来源——⛔ 不做 `exists()` 预查（有预查则「去掉唯一约束」的判别力实验不会红）；`IntegrityError` 一律**先认约束名**，别的完整性错误**原样 re-raise**（未预期错误仍要响亮地 500，E-0010）。⚠️ **范围是本表**：同一个 `request_id` 已用于一次退款（`OrderRefund`）后**仍可**用于一次人工修正——两张表各自唯一，**不是**全局幂等键。⚠️ **不调 `settlement_amount.payable_net`**（裁定 Y）：它会因该绩效 `deduct_mode/value` 坏掉抛 `AmountError`，而 MANUAL 调整恰恰是「负应付 / 坏账的唯一出路」——**兜底不能被被兜底的东西卡死**；审计只记「发生了什么」，净额是导出值。⚠️ 预期失败（重复 `request_id`）让 `IntegrityError` **冒出** `atomic()` 块（E-0022：块内不许裸 `return` 退出补偿路径），`return` 在事务之外。
        """
        # 第 1 步：入参校验（非数字 / 超列宽 / 缺字段 / 超长 ⇒ 200 + `1000`）。
        serializer = ManualAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 前置校验：绩效存在且在本租户 schema 内 ⇒ 否则 `1000`。⚠️ 租户上下文来自**连接**（当前 schema），绝不接受请求参数传入（AGENTS.md §4）。
        # ⭐「按游戏权限收窄」：`Performance` 经 `order` 指到游戏 ⇒ `order__game_id`。看不见的绩效行与"不存在"在这里**同一个出口**（`1000` + 「绩效不存在或不可见」），与 `refund_views.py` 的处置同形。
        perf = (
            scope_queryset(
                request.user,
                Performance.objects.all(),
                scope_filter=game_scope("order__game_id"),
            )
            .select_related("order", "user")
            # ⭐ D-0144（「锚点 `Performance.objects.all()`」那一半）+ R-6：已软删单的绩效行、以及**单条被软删**的绩效行同判「不存在或不可见」——否则会给一条已删的行挂上调整记录，而结算侧已把它排除 ⇒ 那条金额永远结不掉（沉在库里）。
            .filter(
                pk=kwargs["performance_id"],
                order__deleted_at__isnull=True,
                deleted_at__isnull=True,
            )
            .first()
        )
        if perf is None:
            return api_response(
                data=None, msg="绩效不存在或不可见", code=ErrorCode.VALIDATION
            )

        actor = getattr(request.user, "username", "") or ""
        # ⚠️ `try` 在 `atomic()` **外面**：`IntegrityError` 冒出块 ⇒ 事务已回滚，
        #    这里只是**翻译**它（E-0022）。
        try:
            with transaction.atomic():
                perf = Performance.objects.select_for_update(of=("self",)).get(pk=perf.pk)
                if perf.online_payroll_batch_id and not perf.is_settled:
                    return api_response(msg="报酬已在在线付款单中，暂时不能调整",
                                        code=ErrorCode.PERFORMANCE_STATE_INVALID)
                row = PerformanceAdjustment.objects.create(
                    performance=perf,
                    refund=None,
                    kind=PerformanceAdjustment.Kind.MANUAL,
                    amount=data["amount"],
                    reason=data["reason"],
                    operator=actor,
                    settle_status=SettleStatus.UNSETTLED,
                    request_id=data["request_id"],
                )
                # 裁定 Y：`from_value=""`（新增没有变更前值）、`to_value=str(amount)`。⭐ M3a：旧出口 `log_manual_adjustment` 已迁到唯一写入出口 `record(...)`；动作 = `performance.adjust_manual`（**不是** `performance.adjust`：本端点新增的是一条 `kind=MANUAL` 的 `PerformanceAdjustment` 记录、不是改绩效上的既有字段；Ruling N 明确两者不得合并/改名成 `log_adjust`）；身份按 R11 落到绩效归属人（`OperationLog` 没有 `performance_id` 列）。
                record(
                    log_type=ACTION_LOG_TYPES["performance.adjust_manual"],
                    action="performance.adjust_manual",
                    actor=actor,
                    actor_id=request.user.pk,
                    target_kind="user",
                    target_id=perf.user_id,
                    # ⭐（spec §3-A2）：人名落**账号名称**（`user_display_name`）；归属关系为空 ⇒ 保持空串（⛔ 不拿 `None` 去调、也不渲染 `None`）。
                    target_name=(
                        user_display_name(perf.user) if perf.user_id else ""
                    ),
                    order_no=perf.order.order_no,
                    from_value="",
                    to_value=str(data["amount"]),
                    reason=data["reason"],
                )
        except IntegrityError as exc:
            if DUPLICATE_REQUEST_CONSTRAINT in str(exc):
                return api_response(
                    data=None,
                    msg="该 request_id 已用于一次人工修正",
                    code=ErrorCode.REFUND_DUPLICATE_REQUEST,
                )
            # 别的完整性错误**不**伪装成「你重复提交了」（E-0010）。
            raise

        return api_response(data=PerformanceAdjustmentSerializer(row).data)
