"""把 DRF 的成功响应包成项目统一信封 {code,msg,data}。

异常路径由 P1 的 `envelope_exception_handler` 负责（见 src/apps/core/exceptions.py），
这里只管成功路径。两者合起来保证「所有接口的响应形状一致」。
"""
from django.db.models import ProtectedError
from rest_framework import viewsets
from rest_framework.pagination import PageNumberPagination

from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode


class EnvelopePagination(PageNumberPagination):
    """P1 约定的分页形状：{items,total,page,page_size}，**放在 `data` 内**（DRF 默认 {count,next,previous,results} 形状不符）。用 P1 的 `api_response()` + `paginated()` 构造而不是手搓信封：`_enveloped` 标记自动打上、`finalize_response` 不会再包一层——分页载荷的形状只在 `core.envelope` 定义一次。
    ⚠️ 它住在 `base.py` 而不是某个 views 模块：它是与 `EnvelopeModelViewSet` 同层的基础设施、被所有业务 ViewSet 共享，放在某个 views 模块里会让 ViewSet 互相 import。
    """

    page_size = 20
    page_size_query_param = "page_size"
    #: 每页上限（业务方 2026-11-07 拍板 200→300：报酬/结算页一次勾 300 条一起结）。
    max_page_size = 300

    def get_paginated_response(self, data, *, summary=None):
        """`summary` 是**可选**的额外载荷（绩效列表的汇总行；见 `core.envelope.paginated`）。

        不传 ⇒ 与加这个关键字之前逐字节相同（其余 ViewSet 一个都不用改）。
        """
        return api_response(
            paginated(
                data,
                total=self.page.paginator.count,
                page=self.page.number,
                page_size=self.get_page_size(self.request),
                summary=summary,
            )
        )


#: 「这个分页器不负责统计块」的哨兵。**不能用 `None`**——订单列表在「不允许看统计值」时也要显式回 `summary: null`（前端据此隐藏那句话），而大厅（`hall`）走的是**同一个分页器**（`OrderViewSet.pagination_class` 是类级属性，`hall` 也调 `self.get_paginated_response`）、载荷**不该多出这个键**。两者只能靠哨兵区分。
_SUMMARY_NOT_APPLICABLE = object()


class OrderListPagination(EnvelopePagination):
    """订单列表可复用汇总算出的分页总数；大厅沿用自己的分页结果。"""

    summary = _SUMMARY_NOT_APPLICABLE
    known_total = None
    page_relations = None

    def django_paginator_class(self, queryset, page_size):
        paginator = super().django_paginator_class(queryset, page_size)
        if self.known_total is not None:
            paginator.count = self.known_total
        return paginator

    def get_paginated_response(self, data):
        payload = paginated(
            data,
            total=self.page.paginator.count,
            page=self.page.number,
            page_size=self.get_page_size(self.request),
        )
        if self.summary is not _SUMMARY_NOT_APPLICABLE:
            payload["summary"] = self.summary
        if self.page_relations is not None:
            payload["page_relations"] = self.page_relations
        return api_response(payload)


class EnvelopeResponseMixin:
    """成功响应包成项目统一信封 `{code,msg,data}` 的唯一实现。住在 mixin 里是为了让**只读集合**与读写集合共用同一份「包不包、包几层」的判据——抄第二份就会漂移，漂移的表现是「某个端点偶尔不带信封」（客户端读 `code` 读不到）。判据与注释见 `finalize_response`。
    """

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # 只包成功路径；异常路径已由 EXCEPTION_HANDLER 包过，避免双重包装。判据不能只有 `status_code < 400`：P1 把**业务失败**也定成 HTTP 200（docs/ARCHITECTURE.md §8：HTTP 状态码只表达传输与认证层），此时 status_code 是 200、内容却已是信封，再包一次就变成 `{"code":0,...,"data":{"code":1002,...}}`——客户端读到的 code 永远是 0。
        # DRF 的 `APIView.handle_exception` 会给所有经 EXCEPTION_HANDLER 产出的响应打上 `response.exception = True`（finalize_response 在其之后调用），据此与成功路径区分即可，**不需要**另建一条错误路径。
        if (
            response.status_code < 400
            and not getattr(response, "exception", False)
            and not getattr(response, "_enveloped", False)
        ):
            response.data = {"code": 0, "msg": "", "data": response.data}
            # 成功也一律回 HTTP 200：状态码只表达传输与认证层、业务结果由 code 表达；DRF 默认的 201（create）/ 204（destroy）会让客户端不得不同时按 status 和 code 两条线分支，与「业务结果只读 code」的约定冲突。
            response.status_code = 200
            response._enveloped = True
        return response


class EnvelopeModelViewSet(EnvelopeResponseMixin, viewsets.ModelViewSet):
    """所有业务 ViewSet 的基类。成功响应统一包一层信封。"""

    def destroy(self, request, *args, **kwargs):
        """删除。被 PROTECT 的关联挡住时回业务错误码，而不是 HTML 500。`on_delete=PROTECT` 抛的 `ProtectedError` 既不是 `Http404` 也不是 DRF `APIException`——P1 的异常处理器**故意**把这类未知异常交回 Django（变成 500），因为「未预期的错误」不该被伪装成业务结果。
        但这一条是**预期的业务结果**：配置被订单/项目引用时不允许删除，客户端需要能读到原因并给出提示，所以在这里显式接住、用 `ErrorCode.CONFLICT`（1001）——这也是 1001 这个码第一次被真正用上。
        """
        try:
            return super().destroy(request, *args, **kwargs)
        except ProtectedError:
            return api_response(
                data=None,
                msg="该记录已被其它数据引用，不能删除",
                code=ErrorCode.CONFLICT,
            )


class EnvelopeReadOnlyModelViewSet(EnvelopeResponseMixin, viewsets.ReadOnlyModelViewSet):
    """只读集合的基类（公司店铺分组的「店铺归属」列表）。为什么单开一个类、而不是给 `EnvelopeModelViewSet` 设 `http_method_names`：`router.register` 按**视图真实具备的 mixin** 生成路由——`ModelViewSet` 即使把写方法关掉，路由表里仍有 POST/PUT/PATCH/DELETE（只是回 405），于是「写端点覆盖率网」与「对外 API 面」都要多背四条假端点；`ReadOnlyModelViewSet` 只生成 list / retrieve ⇒ 路由表与能力面一致。
    ⚠️ 本类的**唯一**读者是 `/api/business/xgj-shops/`；它的权限是「只登录」（与同模块写动作等价，见 `xgj_views.XgjShopListViewSet`）。
    """

