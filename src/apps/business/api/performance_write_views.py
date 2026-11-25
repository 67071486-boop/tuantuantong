"""绩效页的按钮端点：撤销结账、删除、导出、支付宝查重。闸门一律是节点 code，不要新增 Django codename 权限类；撤销与正向结账共用 `finance.change_settlement_status`（同一颗按钮的两个方向），支付宝查重与结算读接口共用 `finance.payroll`。
业务失败是 200 + 业务码；未认证 401、无权限 403；`permission_denied` 必须先转调 `super()`，否则未登录会被当成 403。
"""
from decimal import Decimal
from urllib.parse import quote

from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.views import APIView

from apps.accounts.display import display_names_for_usernames, user_display_name
from apps.accounts.models import StaffProfile
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.performance_views import (
    can_view_order_money,
    performance_queryset,
    search_params,
)
from apps.business.api.serializers import (
    PerformanceIdsSerializer,
    RevokeSettleRequestSerializer,
)
from apps.business.order_flags import can_view_offer_price
from apps.business.performance_export import (
    build_workbook,
    detail_values,
    summary_values,
)
from apps.business.performance_writes import (
    DeleteError,
    RevokeError,
    delete_performances,
    revoke_settlements,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 一次导出的行数上限。导出是把当前筛选整批拿走，没有异步导出。
#: 超限必须报错，绝不静默截断：截断会让财务拿着少一截的数据对账，而且没有报错。
EXPORT_MAX_ROWS = 10000

#: 导出列集不从列表序列化器派生。顺序和表头的唯一出处是
#: `performance_export.py` 的 `DETAIL_HEADERS` / `SUMMARY_HEADERS`（明细 29 列 + 汇总 10 列）。
#: 列表上多出来的字段不进导出，也不要复活已经删掉的列标签表。


class SettlementRevokeView(APIView):
    """撤销结账（D-0072 ②）：已结账 → 未结账，必填原因，逐 id fail-closed。"""

    def get_permissions(self):
        """闸门 = `has_node("finance.change_settlement_status")`（与 `SettleView` **同一个**节点）。⚠️ 写成**方法**而不是类级 `permission_classes`：接线位点表的 T2 判据解析的是方法体里的 `has_node(...)` 调用（与 `SettleView.get_permissions` 同形）。
    理由（免得后来者以为是复制粘贴错了）：对标物的「更改结算状态」是一颗按钮，D-0072 把它拆成「正向 = `/settle`」「反向 = `/revoke`」两个端点，权限点因此**共用**——新开一个点会要求业务方在权限树上维护两个永远同进同退的勾。
    """
        return [has_node("finance.change_settlement_status")()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（必须转调 `super()`：它先判认证并抛 401）。"""
        super().permission_denied(request, message="你没有撤销结账的权限", code=code)

    def post(self, request):
        serializer = RevokeSettleRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)          # 缺原因 / 空原因 / 超长 ⇒ 1000
        data = serializer.validated_data
        actor = getattr(request.user, "username", "") or ""
        try:
            result = revoke_settlements(
                data["performance_ids"],
                actor=actor,
                user=request.user,
                reason=data["reason"],
            )
        except RevokeError as exc:
            # 预期失败：业务码 + 整批已回滚（异常冒出 `atomic()` 块）。
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(
            data={
                "revoked_performances": result.revoked_performances,
                "revoked_adjustments": result.revoked_adjustments,
                # ⭐ 批量里**已经是「未结账」目标状态、因此被跳过**的 id（成功路径）。
                # ⚠️ 与「查不到 / 越权 ⇒ 1006」是两件事，见 `performance_writes.RevokeResult`。
                "skipped_performances": result.skipped_performances,
            }
        )


class PerformanceDeleteView(APIView):
    """删除绩效记录（D-0072 ①）：已结账 / MAIN 行 / 有调整记录三类都不许删。"""

    def get_permissions(self):
        """闸门 = `has_node("finance.delete_performance")`（对标 `85 删除效绩数据`）。"""
        return [has_node("finance.delete_performance")()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有删除绩效记录的权限", code=code
        )

    def post(self, request):
        serializer = PerformanceIdsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        actor = getattr(request.user, "username", "") or ""
        try:
            result = delete_performances(
                serializer.validated_data["performance_ids"],
                actor=actor,
                user=request.user,
            )
        except DeleteError as exc:
            return api_response(data=None, msg=exc.message, code=exc.code)
        return api_response(data={"deleted": result.deleted})


class PerformanceExportView(APIView):
    """导出绩效记录（GET），成功时是 xlsx 文件流，故意不走统一信封——成功没有 `{code,msg,data}`，前端必须按非 JSON 处理；参数非法或超上限仍回信封，所以同一个 URL 有两种形状。GET 靠会话 cookie，不触发写方法那道 CSRF。
    筛选和数据范围与列表同一对函数：持导出节点但不持 `order.list` 的人只导得出自己的行。列集看附件（明细 29 列 + 汇总 10 列），不从列表序列化器派生；发单价按 `can_view_offer_price` 脱敏，明细不带支付宝账号、汇总表按附件带结算账号四列。
    人名列（明细第 1 / 25 列、汇总与跳过名单第 1 列）一律出**显示名**（昵称优先、空串回落账号；2026-11-05 业务方）——「记录人」的账号快照存值不动，导出层按一次批量映射换显示名。
    只给成功的导出记一条操作日志（失败提前返回信封、没有导出任何数据，不能记成「导出了」）；不写目标类型和目标 id：身份由操作人承载。
    """

    def get_permissions(self):
        """闸门 = `has_node("finance.export_performance")`（对标 `133 导出效绩记录`）。"""
        return [has_node("finance.export_performance")()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(request, message="你没有导出绩效记录的权限", code=code)

    def get(self, request):
        # 入参（畸形值 ⇒ 200 + `code 1000` 的**信封**，不是 xlsx）。
        data = search_params(request.query_params)
        # ⚠️ `select_related("order__game", "order__order_source")` **链在这里**（⛔ 不改
        # `performance_queryset` 本身）：明细逐行读这两个关联，不预取就是 N+1。
        queryset = performance_queryset(request.user, data).select_related(
            "order__game", "order__order_source"
        )

        total = queryset.count()
        if total > EXPORT_MAX_ROWS:
            # ⛔ **报错，不截断**（见 `EXPORT_MAX_ROWS` 的注释）。
            return api_response(
                data=None,
                msg=(
                    f"本次导出 {total} 行，超过上限 {EXPORT_MAX_ROWS} 行，"
                    "请先收窄筛选条件"
                ),
                code=ErrorCode.VALIDATION,
            )

        # ⚠️ 脱敏判据按**当前请求的 user** 算一次（不是逐行算）：flag 的施加是请求级的。
        can_see_offer_price = can_view_offer_price(request.user)

        # ⭐ **明细与汇总同源**：queryset 只物化一次（`list`），汇总就在这次遍历里按用户名累计。⛔ 不要在明细之外再查一次库算汇总——两处分别取数会让「汇总与明细对不上」成为可能，而那种偏差在导出文件里看不出来（本项目对「同源」的既有要求）。
        # 口径 = **明细行按用户名分组**：接单数 = 行数，后四项 = Σ（与附件逐字对拍过）。
        #: ⭐：打款表（第 3 张副表）的取数——**就在这次遍历里**收集
        #: `(用户名, 实计额)`，⛔ 不为它再查一次库（本仓对「同一事实两处取数」有成文教训）。
        #: 分组 / 求和 / 跳过判定都在 `performance_export.alipay_tables`（唯一一份实现）。
        bonus_rows = []
        totals_by_user = {}
        #: ⭐ **D-0255**：订单价与退款**按订单去重**（与
        #: `performance_views.performance_totals` 同一条口径、同一次裁定，E-0021）：
        #: 同一张单在一个用户的桶里只计一次价与退款，提成（预计/实计）与行数仍按**行**累计。
        #: ⚠️ 今天 `(order, user)` 唯一约束让每个桶里一张单最多一行 ⇒ 本去重是**结构性保险**；
        #: 但口径不能靠「约束碰巧在」——约束一旦放宽，两行单的价/退款就会在桶里翻倍。
        seen_orders_by_user = {}
        # ⚠️ 明细行必须等**全量**用户名收齐才能映射（下面那次批量查询），所以先物化再两段走：
        # `list(queryset)` 上限受 `EXPORT_MAX_ROWS` 约束（与 `order_export_views` 同形）。
        performances = list(queryset)
        for performance in performances:
            bonus_rows.append((performance.user.username, performance.bonus_amount))
            bucket = totals_by_user.setdefault(
                performance.user.username,
                {
                    "count": 0,
                    "temp_amount": Decimal("0.00"),
                    "bonus_amount": Decimal("0.00"),
                    "order_price": Decimal("0.00"),
                    "refund_fee": Decimal("0.00"),
                },
            )
            seen_orders = seen_orders_by_user.setdefault(performance.user.username, set())
            bucket["count"] += 1
            bucket["temp_amount"] += performance.temp_amount
            bucket["bonus_amount"] += performance.bonus_amount
            if performance.order_id not in seen_orders:
                seen_orders.add(performance.order_id)
                bucket["order_price"] += performance.order.total_price
                bucket["refund_fee"] += performance.order.refund_fee

        # ⭐ 人名列全改显示名（2026-11-05 业务方）：三处消费点共用**一次**批量映射——
        # 明细第 25 列「记录人」（`order.created_by` 账号快照）、汇总第 1 列与跳过名单第 1 列
        # （登录账号）。来源两路：`performance.user.username` 与非空 `order.created_by`
        # （去重 / 剔空由 `display_names_for_usernames` 内部做）；⛔ 不逐行查库。
        display_names = display_names_for_usernames(
            [performance.user.username for performance in performances]
            + [performance.order.created_by for performance in performances]
        )
        detail_rows = [
            detail_values(
                performance,
                can_view_offer_price=can_see_offer_price,
                created_by_names=display_names,
            )
            for performance in performances
        ]

        # 结算四列取该用户的 `StaffProfile`（没有档案 ⇒ 空）：**一次查完**，⛔ 不逐行查。
        profiles = {
            profile.user.username: profile
            for profile in StaffProfile.objects.filter(
                user__username__in=list(totals_by_user)
            ).select_related("user")
        }
        # 行序 = 用户名**升序**（附件实测如此）；⛔ 不按插入序 / 主键序。
        summary_rows = [
            summary_values(
                username,
                totals_by_user[username],
                profiles.get(username),
                display_names.get(username, username),
            )
            for username in sorted(totals_by_user)
        ]

        # ⚠️ 打款表与跳过名单**吃同一份 `profiles`**（就是上面为汇总四列查的那一次）：⛔ 不要在导出模块里再查一次库（那会变成两处取数）。
        # ⭐（业务方拍板）：导出与列表/手机页用**同一个判据**——不持 `order.list` 也不持 `finance.payroll` ⇒ 「订单价格 / 退款」两列整列不出现在文件里；判据只在 `performance_views.can_view_order_money()` 一处实现，本端点只执行。
        payload = build_workbook(
            detail_rows,
            summary_rows,
            bonus_rows,
            profiles,
            can_view_order_money=can_view_order_money(request.user),
            display_names=display_names,
        )

        # 成功的导出才留痕。建文件失败会 500，不能提前写一条「导出了」。⚠️ 操作人取**认证态**（与全项目同一条 R20 口径），不取请求参数。
        # ⚠️ 读操作**没有业务事务**（spec §6.2：写日志失败只丢一条导出记录），所以刻意不包 `transaction.atomic()`（与 `OrderViewSet.retrieve` 同形）。⚠️ 顺序：**先把文件建出来、再留痕**——建文件失败属未预期错误（会响亮地 500），不该提前写一条「导出了」的假记录。
        record(
            log_type=ACTION_LOG_TYPES["performance.export"],
            action="performance.export",
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
        # 同时给 ASCII 兜底名和 RFC 5987 的 `filename*`：中文名只有后者能被浏览器可靠采用。
        chinese_name = f"效绩提成记录{now:%m%d%H%M%S}.xlsx"
        response["Content-Disposition"] = (
            f'attachment; filename="performances-{now:%Y%m%d}.xlsx"; '
            f"filename*=UTF-8''{quote(chinese_name)}"
        )
        return response


# ── P4FU-C：检测重复收款支付宝（只读）────────────────────────────────────

#: 支付宝查重的节点 code = `finance.payroll`（80「打手与客服工资」），与结算三个读接口
#: **同一个** code（`settlement_views.FINANCE_READ_NODE`）。理由：这是绩效页上的只读检测，
#: 受众与结算记录页同一批（财务 / 客服主管），不该在权限树上多一个永远同进同退的勾。
ALIPAY_DUPLICATES_NODE = "finance.payroll"


class AlipayDuplicatesView(APIView):
    """检测重复收款支付宝（只读；对标 `/UserAjax/GetCheckIdenticalAliPayAcc`）。**语义（照对标）**：**租户内全量**（不看列表筛选）、只找**重复**——按非空 `StaffProfile.alipay_account` 分组、组内 **≥ 2 个账号**才输出；空串 / 未填**不参与分组**（它不是一个「收款账户」，不能被当成一组重复）。
    **出参形状**（前端照它做对标那张 6 列子表）：`{"duplicates": [{"alipay_account", "count", "users": [{"username", "display_name", "job", "is_active"}...]}...]}`。`count` = 用这个账户收款的人数（≥ 2），与 `users` 长度同源（都来自同一次读取）、不是「重复次数」的减法结果；`display_name` = **显示名**（昵称优先、空串回落登录账号，唯一出处 `apps/accounts/display.py::user_display_name`——「显示人统一成昵称」线**追加**的键，`username` 同时保留，前端按 `display_name || username` 回落）；`job` = 该用户的 **Group 名（职位）**、多个按名称升序「、」连接，**没有职位 ⇒ 空串**（不编造、也不回落成用户名）；排序先 `alipay_account` 再 `username` ⇒ 响应可复现（前端表格与测试都好钉）。
    **PII 取舍（有意例外）**：`alipay_account` 在账号**列表**里是脱敏字段（`accounts/payloads.py` 的 `_PII_FIELDS` → 保留前 3 后 2），本端点**返回完整账号**——它的**目的就是让人看到「重复的那个账号」**，脱敏后两条记录看起来一样、端点即失效。代价与边界三条：① 受众被 `finance.payroll` 节点限制，与本项目「**列表脱敏、详情全量**」的既有 PII 口径一致，本端点属于**详情档**（对标就是全量展示的子表）；② 本端点**只返回** `username` + `display_name` + 职位 + `is_active`，**不返回**支付宝姓名 / 银行卡 / 身份证 / 手机号——PII 面收窄到一个**必须**可见的字段；③ 只读、不改任何数据，也不做「只显示后 4 位 + 完整值」那种折中（对「核对是不是同一个账户」同样没用，还会把完整值再暴露一遍）。
    """

    def get_permissions(self):
        """闸门 = `has_node(ALIPAY_DUPLICATES_NODE)`（= `finance.payroll`）。

    写成方法而不是类级 `permission_classes`：接线位点表解析的是方法体。
    不复用 mixin 里继承来的 `get_permissions`：位点锚在本类自己声明的那一处，继承的方法体对不上。
"""
        return [has_node(ALIPAY_DUPLICATES_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（必须转调 `super()`：它先判认证并抛 401）。"""
        super().permission_denied(
            request, message="你没有查看重复收款支付宝的权限", code=code
        )

    def get(self, request):
        # 第一步：让数据库只回「真的有 ≥ 2 个账号共用」的那些支付宝账号（HAVING COUNT ≥ 2）。
        accounts = list(
            StaffProfile.objects.exclude(alipay_account="")
            .values("alipay_account")
            .annotate(count=Count("id"))
            .filter(count__gte=2)
            .order_by("alipay_account")
            .values_list("alipay_account", flat=True)
        )
        duplicates = []
        if accounts:
            # ⚠️ `prefetch_related("user__groups")`：`job` 逐行读 Group 名，不回落到每行一条查询。
            profiles = (
                StaffProfile.objects.filter(alipay_account__in=accounts)
                .select_related("user")
                .prefetch_related("user__groups")
                .order_by("alipay_account", "user__username")
            )
            grouped = {}
            for profile in profiles:
                grouped.setdefault(profile.alipay_account, []).append(
                    {
                        "username": profile.user.username,
                        # ⭐（「显示人统一成昵称」线）：**追加**显示名键，口径唯一出处 `apps/accounts/display.py::user_display_name`（昵称优先、空串回落登录账号）。⚠️ `username` 是**加法前的既有键**、原样保留（它是「账号」这个事实；前端按 `display_name || username` 回落）。
                        "display_name": user_display_name(profile.user),
                        "job": "、".join(
                            sorted(group.name for group in profile.user.groups.all())
                        ),
                        "is_active": profile.user.is_active,
                    }
                )
            duplicates = [
                {"alipay_account": account, "count": len(users), "users": users}
                for account, users in grouped.items()
            ]
        return api_response(data={"duplicates": duplicates})
