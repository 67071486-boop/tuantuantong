"""接单统计端点：`GET /api/business/order-stats`。对标 = 账号页 `.recTotalDiv`「接单统计」卡片 / `/UserAjax/GetRecOrderTotalList`；取数与口径全在 `apps/business/order_stats.py`（**只读**、可脱离 HTTP 直接测），本模块只做三件事：**校验查询参数** → **授权** → **分页**。三处刻意改进（对标的行为照抄，错误表达**不照抄**）：① **错误绝不静默**——对标用「HTTP 200 + `code:0` + `count:0` + `data:[]`」表达「缺 `stime`/`etime` / 日期带时分秒 / `gameName` 不存在」三种错误，我方一律显式业务码 **`1000` + 说真话且可区分的 msg**；② **`limit` / `page` 的三种坏值分开处理、都不静默**——DRF 分页对 `limit=abc` 会**静默回落**默认页大小 ⇒ `limit` 必须在序列化器里先拒（**参数校验放在分页之前**，这也是 `test_no_route_500` 的显式路由腿能拿到 200 的原因），`page` 非整数 / `<1` 同样拒成 `1000`；`page` 是**合法整数但超末页**时 DRF 抛 `NotFound`、本仓信封会翻成 `1002「记录不存在或已被删除」`——**那是假话**（没有任何记录不存在，只是这一页没有内容），参数校验**管不到**这一支（`page=99999` 是合法整数）⇒ 由 `get()` **接住 `NotFound` 并按成功形状回**：`code 0` + `items: []` + **真实 `total`**（「选游戏重取、不回第 1 页」正依赖这一点：页数变少后若拿到 1002，layui 会把它当失败处理）；本端点 `limit` 上限 = 2000（`ORDER_STATS_MAX_LIMIT`，照对标 `limits` 最大档），`limit>2000` / `<1` / 非数字一律 `1000` + msg，**绝不静默截断**；③ **不做缓存、不写假话**——对标的橙字「数据有 2 分钟缓存」我方**不写**（我们没有缓存）。
总数：对标放在**响应顶层** `count`，我方沿用**统一信封**、总数在 **`data.total`**（`core.envelope.paginated()` 既有形状，全仓分页端点一致）。**结论：不改顶层 `count`**——为一个端点破信封形状会让前端按「哪个端点」分支取值；layui 在 `table.render` 里用 `parseData` 适配一次即可：`parseData: (res) => ({code: res.code, msg: res.msg, count: res.data.total, data: res.data.items})`，行数据本身在 `data.items`。
⚠️ **授权判据是 RBAC 节点 code**（`has_node`，Ruling T / D-0028）：**不要**新增 `HasPermission`（Django codename，已冻结为 legacy）。⚠️ 业务失败一律 **200 + 业务 `code`**——**本端点只产生 `1000`**：`OrderStatsError` 只有「未知游戏名」那一支（`ErrorCode.VALIDATION`），`AmountError` 四处**全是** `VALIDATION`，越界 `page` 已被 `get()` 接住按**成功空页**回 ⇒ **`1002` 本端点不再产生**（别拿它当排障依据）。未认证 **401**，无权限 **403**。
"""
from rest_framework import serializers
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from apps.business.api.base import EnvelopePagination
from apps.business.order_stats import OrderStatsError, order_stats_rows
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 准入节点（`apps/rbac/catalog.py` 的 1253「查看接单统计」）。
ORDER_STATS_NODE = "system_user.view_order_stats"

#: 本端点的 `limit` **上限**（= 对标 `limits:[20,30,50,100,500,1000,2000]` 的最大档）。
#: ⚠️ **专用**于本端点：全局 `EnvelopePagination.max_page_size = 300` **不动**（别的端点照旧）。
#: 超限 / 非法值一律在上面被序列化器拒掉（`1000`），**不允许**分页层静默截断。
ORDER_STATS_MAX_LIMIT = 2000


class OrderStatsSearchSerializer(serializers.Serializer):
    """查询参数的**唯一**校验口（照 `settlement_views.SettlementViewSet._params` 的写法）。⚠️ 请求来源的值进类型化查询之前一律过序列化器（E-0010）：`?stime=garbage` / `?limit=abc` 直接进 `filter()` / 分页会抛 `ValueError`（HTML 500）或被静默忽略，两种都不是这里要的行为。
    ⚠️ `stime` / `etime` **刻意 `required=False`**：缺参由本视图自己判，才能给出「缺少必填参数：stime、etime」这句**指出缺了哪一个**的话（DRF 的默认 required 文案分不出是两者都缺还是缺一个）；格式错误用 `error_messages` 说清是哪个参数、该长什么样。
    """

    stime = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        # 只吃 `yyyy-MM-dd`（`strptime`）：`"2026-09-01 00:00:00"` 与 `"20260901"` 都会被拒。
        error_messages={"invalid": "stime 格式必须是 yyyy-MM-dd"},
    )
    etime = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "etime 格式必须是 yyyy-MM-dd"},
    )
    # 「中文游戏名文本」（对标那个下拉的 option value 就是游戏名，不是 id）。
    gameName = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,          # = `Game.name` 的列宽（超长值在这里就拒，别指望数据库）
        error_messages={"max_length": "gameName 太长（游戏名最长 64 字符）"},
    )
    page = serializers.IntegerField(
        required=False,
        min_value=1,
        error_messages={
            "invalid": "page 必须是正整数",
            "min_value": "page 必须是 ≥ 1 的整数",
        },
    )
    limit = serializers.IntegerField(
        required=False,
        min_value=1,
        # 上限 = `ORDER_STATS_MAX_LIMIT`（2000）。**必须在这里拒**，不能只靠分页层：DRF 的 `_positive_int(..., cutoff=self.max_page_size)` 对超限值**返回 cutoff**（`rest_framework/pagination.py`），也就是把 3000 **静默截成 2000 / 或更小的 300**——那正是本端点不能接受的行为（对标下拉给到 2000，选了就该真给 2000）。
        max_value=ORDER_STATS_MAX_LIMIT,
        error_messages={
            "invalid": "limit 必须是正整数",
            "min_value": "limit 必须是 ≥ 1 的整数",
            "max_value": f"limit 不能超过 {ORDER_STATS_MAX_LIMIT}",
        },
    )


class OrderStatsPagination(EnvelopePagination):
    """对标的页大小参数名是 **`limit`**（不是本仓默认的 `page_size`），默认 **100**。⚠️ **只改这三个取值，不复制一份分页实现**：信封形状（`{items,total,page,page_size}`）仍由 `EnvelopePagination` 唯一给出。
    ⚠️ **`max_page_size` 本端点专用 = 2000**（对标 `limits` 最大档），**不再沿用**全局的 300——全局 300 会让用户选 1000/2000 时被 DRF **静默截断（成 300）**（`_positive_int` 的 `cutoff`）；**超限值由 `OrderStatsSearchSerializer.limit` 的 `max_value` 先拒**（业务码 `1000` + msg），分页层只负责把合法值原样用上。⛔ **全局 `EnvelopePagination` 不要动**（别的端点行为不变）。
    """

    page_size = 100
    page_size_query_param = "limit"
    max_page_size = ORDER_STATS_MAX_LIMIT


class OrderStatsView(APIView):
    """`GET /api/business/order-stats` —— 按人统计「窗口内接单量 / 还未结算 / 完单率」等。"""

    def get_permissions(self):
        """闸门 = `has_node(ORDER_STATS_NODE)`（写成方法而不是类属性的理由见
        `settlement_views.FinanceReadPermissionMixin.get_permissions`：接线位点表按方法体判据）。
        """
        return [has_node(ORDER_STATS_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一个**可辨的权限语义** `msg`（403 有多个来源，只断状态码分不出来源）。

        ⚠️ **必须转调 `super()`**：DRF 在那里先判「有没有认证成功」并抛 `NotAuthenticated`
        （401），自己直接 `raise PermissionDenied` 会把匿名也变成 403。
        """
        super().permission_denied(request, message="你没有查看接单统计的权限", code=code)

    def _params(self):
        """校验并返回查询参数（非法值 ⇒ `ValidationError` ⇒ 200 + `code 1000`）。"""
        # 空串按「没传」处理：前端清空下拉时会送 `?gameName=`（那是「没选」而不是非法值）。
        raw = {key: value for key, value in self.request.query_params.items() if value != ""}
        params = OrderStatsSearchSerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        missing = [name for name in ("stime", "etime") if name not in data]
        if missing:
            raise serializers.ValidationError("缺少必填参数：" + "、".join(missing))
        return data

    def get(self, request):
        data = self._params()
        try:
            rows = order_stats_rows(
                stime=data["stime"],
                etime=data["etime"],
                game_name=data.get("gameName"),
                viewer=request.user,
            )
        except OrderStatsError as exc:
            # 预期失败：未知游戏名 / 某一行金额算不出来 ⇒ 业务码 + 说真话的 msg。
            # ⛔ 不吞成「空表」（那正是对标那 39 字节的坑），也不伪装成 500。
            return api_response(data=None, msg=exc.message, code=exc.code)
        paginator = OrderStatsPagination()
        try:
            page = paginator.paginate_queryset(rows, request, view=self)
        except NotFound:
            # `page` 是**合法整数但超末页**（不是参数错，参数校验管不到它）：DRF 的分页在这里
            # 抛 `NotFound`，本仓信封会翻成 `1002「记录不存在或已被删除」`——**假话**：
            # 没有任何记录不存在，只是这一页没有内容。按对标的成功形状回空页 + **真实** `total`。
            return api_response(
                paginated(
                    [],
                    total=len(rows),
                    page=data.get("page", 1),
                    page_size=paginator.get_page_size(request),
                )
            )
        return paginator.get_paginated_response(page)
