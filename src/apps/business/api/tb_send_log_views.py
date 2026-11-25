"""`/api/business/tb-send-logs/`：淘宝发货记录的**列表**与**重试发货**（spec §3.7）。| 方法 | 路径 | 用途 ||---|---|---|| GET | `/api/business/tb-send-logs/` | 列表：`sDate` / `eDate` / `result` / `key` + 分页 || POST | `/api/business/tb-send-logs/retry/` | 重试发货：入参 = 勾选行的**记录 id 列表** `{"ids": [...]}`，逐条重发 |**单独一个文件**（形状照 `xgj_views.py` / `source_views.py`）：与在飞的其它线并行施工时不抢 `order_views.py` 那个 2600 行的热文件，冲突面少一处。## 与对标的关系（spec §2 的实测列，逐条）- 4 个筛选：
发货时间 `sDate` / `eDate`（对标是**日期**，`lay-verify="datetime"`）、发货结果 `result`（`-1` 全部 / `1` 成功 / `0` 失败，与对标同值）、搜索 `key`（placeholder「系统单号/淘宝单号」⇒ 两个字段的模糊命中**取并集**，与`performance_views` 的 `keyword` 同口径）。- 分页：对标的 `limit: 50` / `limits: [50]`（只有一项）⇒ 本端点的默认页大小取 **50**。⚠️ 参数名用本仓统一的 `page` / `page_size`（⛔ 不是对标的 `limit`）：layui 传 `limit`时它被忽略、页大小落回默认的 50 ⇒ **行为与对标一致**，这是有意的取舍。
- 9 列表格的字段来源 = `TaobaoOrderSendLog` 的模型字段（行载荷是 `ModelSerializer`逐字输出的模型字段名，⛔ 不改名、⛔ 不转驼峰——前端单元格按这些键取，改名会**静默空白**）。## 「重试发货」的入参为什么是**行 id**同一张单（同一个 `tb_order_no`）可能对应**多行**记录（先说失败、重试后成功）⇒ 按单号传会有歧义；按**记录行 id** 才一一对应（spec §3.7 的第一种形态）。后端拿到 id 后**自己回查那一行**取它的 `tb_order_no` 快照去重发（spec §3.5：重试取勾选行的 `TbOrderNo`），⛔ 不接受前端传单号。## 权限（spec §3.7 的 ⚠️：新端点默认 fail-closed）两个动作都挂**对标那一页自己的节点** `order.taobao_ship_records`（1227，`/Order/TBSendRecordList`，note 原文「进入淘宝发货记录页面并且可以导入淘宝单号批量发货」）——⛔ 不借 `order.list`：那会让「只看订单列表」的角色获得发货权。判据装配照 `OrderViewSet.get_permissions` 的先例（并集形式）。"""
from django.contrib.auth.models import User
from django.db.models import Q
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.accounts.display import user_display_name
from apps.business import xgj_ship
from apps.business.api.base import EnvelopePagination
from apps.business.models import SendResult, TaobaoOrderSendLog
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 一次「重试发货」最多接受多少行。依据 = **对标的页大小**（`limits: [50]`，spec §2 实测）：勾选只能在本页内进行 ⇒ 50 已覆盖真实用法；它同时是**上游调用次数**的上限（逐条同步调，最坏 50 × `xgj_ship.SHIP_TIMEOUT`），⛔ 不要为了「多选一点」放大它而不评估响应时间（代价如实登记在 spec §7 第一节）。
RETRY_MAX_ITEMS = 50


class TbSendLogRowSerializer(serializers.ModelSerializer):
    """列表 / 逐条结果的行形状：**模型字段名逐字输出**（⛔ 不改名、⛔ 不转驼峰）。
    ⚠️ 前端那张表的单元格按这些键取值（`id` 还是勾选行回传的那个 id）⇒ 改一个键名就是**静默空白**（页面不报
    错、列里什么都没有）。所以这里**显式枚举** `fields`而不是 `__all__`：将来给模型加列时，是否要暴露给页面的决定必须**写下来**。"""

    class Meta:
        model = TaobaoOrderSendLog
        fields = (
            "id", "order_no", "tb_order_no", "send_params", "store_name",
            "result", "sent_at", "operator", "remark", "created_at",
        )


class TbSendLogSearchSerializer(serializers.Serializer):
    """4 个筛选（spec §3.7 的第一行）。**只声明本端点真的会应用的字段**。⚠️ 空串按「没传」处理（前端清空筛选栏会送 `?key=`）：
    由视图在入口过滤掉空串，与 `order_views` / `audit` 两处既有先例同口径。
    ⚠️ 非法值 ⇒ DRF `ValidationError` ⇒ **200 + `code 1000`**（⛔ 不是 HTML 500，E-0010）。⚠️ `result` 的 `-1` = 全部（对标下拉的默认项）⇒ 它在 `choices` 里，由视图解释成「不筛」。⚠️ 日期用 `DateField`（对标那两格是**日期**、值是 `YYYY-MM-DD`）⇒ 过滤按`sent_at__date`（当前时区切天），两端**都含**——与绩效列表的时间区间同口径。"""

    sDate = serializers.DateField(required=False)
    eDate = serializers.DateField(required=False)
    result = serializers.ChoiceField(choices=(-1, 0, 1), required=False)
    key = serializers.CharField(required=False, max_length=128)
    exclude_missing = serializers.BooleanField(required=False, default=False)


class TbSendLogRetrySerializer(serializers.Serializer):
    """重试入参：`{"ids": [<记录行 id>, ...]}`（正整数列表，⛔ 不是单号列表）。
    ⚠️ `max_length` 落在**查询/循环之前**：
    超过 `RETRY_MAX_ITEMS` 当场 `1000`，⛔ 不静默截断——截断会让「勾了 60 条」表现得像「只发了 50 条」（同族教训：`MULTI_VALUE_MAX` 的注释）。"""

    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=RETRY_MAX_ITEMS,
    )


class TbSendLogPagination(EnvelopePagination):
    """对标的 `limit: 50` / `limits: [50]`（只有一项）⇒ 本端点的**默认**页大小 = 50。

    `page_size` 仍可被 `?page_size=` 覆盖（上限沿用基类的 `max_page_size`），
    所以「传 `limit`」与「不传」都得到 50 行——对标那页只有 50 这一档。
    """

    page_size = 50


class TaobaoOrderSendLogViewSet(viewsets.GenericViewSet):
    """`/api/business/tb-send-logs/`：列表（GET）+ 重试发货（POST `/retry/`）。
    ⚠️ **只开这两个动作**：
    不继承 `ModelViewSet` ⇒ router 不会生成`POST /tb-send-logs/`（新增）/ `PATCH` / `DELETE` 这些**没有业务含义**的写入口（那几个端点每一个都要补埋点分类，且「谁能改一条已经发出去的发货记录」没有答案）。"""

    serializer_class = TbSendLogRowSerializer
    pagination_class = TbSendLogPagination
    #: 只有列表与重试两条路（`retry` 是 POST 动作 ⇒ "post" 必须在内，否则 405）。
    http_method_names = ["get", "post", "head", "options"]

    #: 逐 action 的节点 code 表（形状照 `order_views.py::OrderViewSet.ORDER_PERMISSIONS`：元组 = any，字符串必须是**字面量**——`tests/test_rbac_structure.py` 的 B2 走 AST 取字符串常量，变量拼出来的 code 它看不见）。两个动作都判**这一页自己的节点**（见模块 docstring）。
    #: ⚠️ 未登记在表里的 action 一律走默认权限类（fail-closed：仍要登录），**不是**默认放行。
    TB_SEND_LOG_PERMISSIONS = {
        "list": ("order.taobao_ship_records",),
        "retry": ("order.taobao_ship_records",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。⚠️ **为什么是并集**：
        默认类表达的是"这一层的最低门槛"（今天 = `IsAuthenticated`），节点判据表达的是"这个动作还要哪个 code"。
        替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。形状与判据同 `OrderViewSet.get_permissions`（先例），⚠️ 判等用 `type(p) is not IsAuthenticated`（⛔ 不用 `isinstance`：它会把子类一起丢掉）。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.TB_SEND_LOG_PERMISSIONS.get(self.action)
        if not codes:
            return base
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    # ── 查询参数（唯一校验口）────────────────────────────────────────────
    def _search_params(self):
        """查询参数**唯一**的校验口（E-0010）。空串按「没传」（见序列化器 docstring）。"""
        raw = {
            name: value
            for name, value in self.request.query_params.items()
            if value != ""
        }
        params = TbSendLogSearchSerializer(data=raw)
        # 非法值 → DRF `ValidationError` → 异常处理器翻成 200 + `code=1000`。
        params.is_valid(raise_exception=True)
        return params.validated_data

    def _filtered_queryset(self, data):
        """把 4 个筛选落到查询上（⛔ 判据一律 `is not None`，E-0021）。"""
        qs = TaobaoOrderSendLog.objects.all()
        s_date = data.get("sDate")
        if s_date is not None:
            qs = qs.filter(sent_at__date__gte=s_date)
        e_date = data.get("eDate")
        if e_date is not None:
            qs = qs.filter(sent_at__date__lte=e_date)
        result = data.get("result")
        # `-1` = 全部（对标下拉的默认项）⇒ 显式排掉它，而不是拿真值判断
        # （`result=0`（失败）是合法筛选值，`if result:` 会把它当成「没传」静默丢掉）。
        if result is not None and result != -1:
            qs = qs.filter(result=result)
        key = data.get("key")
        if key:
            # 对标的 placeholder 是「系统单号/淘宝单号」⇒ 两个字段模糊命中取并集。
            qs = qs.filter(Q(order_no__icontains=key) | Q(tb_order_no__icontains=key))
        if data.get("exclude_missing"):
            qs = qs.exclude(remark__contains="订单不存在")
        # 排序保持 `Meta.ordering = ["-sent_at", "-id"]`（⛔ 不在这里再写一份 order_by：
        # 同一事实两套判据 ⇒ 改一处漏一处）。
        return qs

    def list(self, request):
        data = self._search_params()
        page = self.paginate_queryset(self._filtered_queryset(data))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    # ── 重试发货（spec §3.7 第二行）──────────────────────────────────────
    @action(detail=False, methods=["post"], url_path="retry")
    def retry(self, request):
        """逐条重发勾选的那些记录，返回 `{"success": n, "fail": m, "items": [...]}`。- 逐条**互相独立**：
        一条失败不影响其余（每条自己的结果写在 `items` 里，`success` / `fail` 是两个计数，页面拿它们拼「成功X条，失败Y条」）。- ⛔ **本动作不抛业务异常**：
        每条的失败（上游拒绝 / 没绑定 / 我们自己的代码出错）都由 `xgj_ship` 翻译成「记一条失败」，不会让整批 500（spec §3.4 硬要求 1 同口径）。- 传了**不存在**的记录 id ⇒ 当场 `1000` 且**零写入**（⛔ 不静默跳过：静默跳过会让「勾了 5 条只发了 3 条」看起来像全发过了）。这是**预期失败**，不是 500。- ⚠️ `operator` = 当前登录人的**显示名**（spec §3.1；自动触发那一路是「系统」）。与其余写端点同源取认证态，⛔ 不接受请求体里传操作人（R20）。"""
        payload = TbSendLogRetrySerializer(data=request.data)
        # 失败 ⇒ 统一异常处理器包成 200 + `code 1000`（E-0010）。
        payload.is_valid(raise_exception=True)
        # 去重但**保序**：同一个 id 传两次不该把同一单发两遍（失败档会多留一行）。
        ids = list(dict.fromkeys(payload.validated_data["ids"]))
        rows = {
            row.pk: row for row in TaobaoOrderSendLog.objects.filter(pk__in=ids)
        }
        missing = [pk for pk in ids if pk not in rows]
        if missing:
            return api_response(
                data=None,
                msg=f"这些发货记录不存在（可能已被删除）：{missing}",
                code=ErrorCode.VALIDATION,
            )
        operator, actor_id = self._operator()
        items, success, fail = [], 0, 0
        for pk in ids:
            # ⚠️ 逐条调**领域层**（判据只有一份：`xgj_ship`，⛔ 不在视图里重写发货逻辑）。
            item = xgj_ship.retry_row(rows[pk], operator=operator, actor_id=actor_id)
            items.append(item)
            if item["result"] == int(SendResult.SUCCESS):
                success += 1
            else:
                fail += 1
        return api_response(data={"success": success, "fail": fail, "items": items})

    def _operator(self) -> tuple[str, int | None]:
        """操作人**显示名** + 其主键（口径的唯一出处 `apps/accounts/display.user_display_name`）。
        ⚠️ 显示名（`first_name`）上限 150、与 `TaobaoOrderSendLog.operator` 的列宽一致；
        真正落库时仍走 `xgj_ship._clip_to_column` 按模型元数据裁（⛔ 不在这里抄列宽）。⚠️ 认证态理论上可能是 `AnonymousUser`（本端点有节点门，匿名到不了这里），取 `pk` 前用 `isinstance(user, User)` 兜一下：拿不到 id 就写 `None`（`record()` 的 `actor_id` 是可空列），⛔ 不让它变成 500。"""
        user = self.request.user
        actor_id = user.pk if isinstance(user, User) else None
        return user_display_name(user), actor_id
