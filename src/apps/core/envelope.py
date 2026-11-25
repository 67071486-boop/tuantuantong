"""统一响应体：{code, msg, data}。所有业务接口必须经此构造，view 不手写信封。"""
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from apps.core.error_codes import ErrorCode


def api_response(data=None, *, msg="", code=ErrorCode.OK, status=200):
    response = Response({"code": int(code), "msg": msg, "data": data}, status=status)
    # 标记「已封口」：EnvelopeModelViewSet.finalize_response 见到它就不再包一层。
    # 放在这个**唯一的信封构造点**，意味着任何用 api_response() 的地方都自动免疫二次包装——
    # 这是结构保证；若改成要求调用方记得调某个 helper，那就是纪律，迟早会漏。
    response._enveloped = True
    return response


def paginated(items, *, total, page, page_size, summary=None):
    """分页载荷，放进 api_response 的 data 参数。

    ⭐ **可选关键字 `summary`**（2026-09-22，绩效管理页 `/Emp/emplist` 的汇总行）：
    不传 ⇒ **键不进 payload**（既有调用者的响应逐字节不变）；传了 ⇒ 以 `data["summary"]` 出现。
    它的**口径**由调用方负责（绩效列表用 `performance_views.summary_from_totals`），
    本函数只管信封形状——信封形状只允许在这一处定义（模块 docstring / spec D4）。
    """
    payload = {
        "items": list(items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }
    if summary is not None:
        payload["summary"] = summary
    return payload


def error_response(code, msg, status, headers=None):
    """传输/认证层失败的统一信封——**唯一**构造入口。
    中间件（不经 DRF 异常处理链）与 `envelope_exception_handler` 都必须用它，否则同一个状态码会出
    现两种响应形状——这是 **spec D4 明文要求的代价**（"信封构造必须抽成共用函数"）；它不是任何一条 E 编号错题的教训，别再往这里挂 `E-0007` / `E-0014` 之类对不上的引用。"""
    return Response({"code": code, "msg": msg, "data": None}, status=status, headers=headers)


def rendered_response(response):
    """把 DRF Response 渲染好再交出去（不经 DRF 请求周期的场景：中间件短路、Django 层视图）。**为什么必须共用这一个 helper**：DRF 的 `Response` 平时是在 `finalize_response`里补上「渲染三件套」再交给 `render()` 的（`rest_framework/views.py`）；
    一旦响应由**中间件短路**或**Django 层视图**（如 `CSRF_FAILURE_VIEW`）产出，那条路根本不会跑，不在这里补，响应就停在未渲染态——外层`CommonMiddleware.process_response` 每个响应都要读 `len(response.content)`（`django/middleware/common.py:113`），于是一个本意 401/403 
    的响应对外变成 500。**为什么不能各写一份**：这份三件套决定的是**字节形状**（渲染器、媒体类型）。手抄一份就又开了一条绕过唯一信封来源的路——实测过一次背离：`JsonResponse`走 `json.dumps` 默认参数（带空格），而 DRF 的 `COMPACT_JSON` 用`SHORT_SEPARATORS=(",", ":")`，同一个 403 的信封**逐字节不同**，且将来换信封时这条路会被静默漏掉。"""
    response.accepted_renderer = JSONRenderer()
    response.accepted_media_type = "application/json"
    response.renderer_context = {}
    response.render()
    return response