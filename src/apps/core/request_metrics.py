"""请求耗时埋点中间件（spec `docs/plans/2026-10-04-request-metrics.md` §2/§3/§5）。**它做什么**：每个请求记一条 `RequestMetric`（「谁、什么时候、哪个接口、多久」），同一「整点 + 接口 + 方法 + 状态码 + 人」只留**一行**（`count` / `total_ms` / `max_ms` / 六个直方图桶在数据库侧累加）；超过阈值的请求**额外**再写一条 `kind="slow"` 行（存那一次的精确耗时，供排障看个例）。**它量的是全链路**：本中间件排在 `MIDDLEWARE` **最后**（最靠近视图）⇒ `get_response()` 里已包含租户解析、会话、CSRF、认证这些**外层**中间件的耗时；`time.perf_counter()` 是单调时钟、不受系统时间调整影响。
**失败口径（承重，spec §5）**：埋点本身的**任何**异常都**不许**让用户请求失败——整段写库包 `try/except` + `logger.exception`（进 journald）后照常返回响应。⚠️ **这不是「静默失败」**：是**刻意**拿可观测性换「不影响业务」，代价**如实登记**；判据：`tests/test_request_metrics.py::WriteFailureIsFailOpenTests`。
**如实登记的代价（spec §8）**：① 每个被记录的请求多一次索引 UPSERT（~1–3 ms）且在用户请求的同一条路上（⛔ 不同步、⛔ 不引后台线程 / 队列——那会带来生命周期与测试上的麻烦）；② `slow` 行在慢请求多时会明显增长（由保留期清理兜住，见 `purge_request_metrics`）；③ 记的是**员工维度**的耗时（敏感数据），只对**账号管理员**开放（`IsAccountAdmin` 门），⛔ 不提供导出、⛔ 不做跨租户汇总；④ 它**不替代** APM：没有调用链、没有 SQL 级分解，只有接口级耗时与分档分布。**隐私面**：只到「谁、什么时候、哪个接口、多久」，⛔ 中间件**不解析请求体**。
"""

import logging
from time import perf_counter

from django.conf import settings
from django.utils import timezone

# ⚠️ `from apps.audit.metrics import upsert_request_metric`（不是 `import apps.audit.metrics`）：这条 `from … import` 让「被查找的模块」变成**本模块**，所以有效的 `mock.patch` 目标是 `apps.core.request_metrics.upsert_request_metric`（E-0029）；判据见 `tests/test_request_metrics.py` 里那条「打在定义模块上不生效」的对照。
from apps.audit.metrics import upsert_request_metric
from apps.core.plane import on_public_plane

logger = logging.getLogger(__name__)

#: **不记**的路径前缀（spec §2）：探活与静态资源会把表写满、没有分析价值。⚠️ 匹配是**前缀**语义（`str.startswith(tuple)`）⇒ `"/healthz-anything"` 同样免记——这是**有意**的取舍（拿「少记几条以这些前缀开头的路径」换「探活 / 静态资源不把表写满」），后果如实登记：任何以这些前缀开头的路径——包括攻击者构造的——都不进表。
#: ⚠️ 与 `NonDirectUploadBodyLimitMiddleware` 同口径用 `request.path_info`（去掉 `SCRIPT_NAME`），⛔ 不用 `request.path`：部署在子路径下后者会带上前缀、白名单整条失效。
EXEMPT_PATH_PREFIXES = ("/healthz", "/static/", "/media/", "/favicon.ico")

#: **解析不到路由**时写进 `route` 列的固定哨兵（spec §3 的 `route` 列）。⛔ **绝不**回落 `request.path`：那是**请求方可控**的字符串（404 路径），会把表的基数从「接口数」顶成「任意路径数」（实测：50 个互不相同的 404 路径 ⇒ 表里 50 行），且路径里可能带令牌 / 订单号之类的东西、一存就是 90 天。判据：`tests/test_request_metrics.py` 的 `test_1b` / `test_1c` / `test_1d`。
UNRESOLVED_ROUTE = "<unresolved>"


class RequestMetricsMiddleware:
    """把每个请求折成 `RequestMetric` 的一行（口径见本模块 docstring）。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = perf_counter()
        response = self.get_response(request)
        try:
            _record(request, response, started)
        except Exception:
            # 埋点自身的任何异常都在这里被吃掉：**用户请求照常返回**（spec §5 的失败口径）。
            # `logger.exception` 带栈，运维能从 journald 看见"埋点坏了"；⛔ 不要降级成
            # 静默 `pass`——那会让埋点坏掉而没有任何信号。
            logger.exception("请求耗时埋点写入失败（已忽略，不影响本次请求）")
        return response


def _record(request, response, started):
    """写一次埋点。**允许抛**：调用方（`__call__`）负责兜住并记日志。"""
    # 耗时**先量**：下面读 `request.user` / `resolver_match` 与写库都不该被算进"用户等了多久"。
    duration_ms = max(0, round((perf_counter() - started) * 1000))
    if not settings.REQUEST_METRICS_ENABLED:
        return
    path = request.path_info
    if path.startswith(EXEMPT_PATH_PREFIXES):
        return
    # 平台平面（`public`）没有这张表：`RequestMetric` 属 `apps.audit`、只建在租户 schema 里（`TENANT_APPS`）。⛔ 不守卫的话，平台平面的**每个**请求都会走一次「失败 + logger.exception」——那不是新功能，只是给平台面添噪音；口径与 `make_logged_in_client` 在 public 平面跳过 `StaffProfile` 同源（spec §0：数据天然按租户分开）。
    if on_public_plane():
        return
    # ⚠️ `request.user` **在响应之后再读**（spec §5）：`AuthenticationMiddleware` 已经跑过；
    # 未登录时是 `AnonymousUser`（`id` 是 None、`username` 是 ""）。
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        user_id = int(user.pk)
        # 列宽 150 = `auth_user.username` 的列宽。⚠️ 这里的切片是**承重**的：写入走的是
        # 原始 SQL（绕过 ORM 的字段 max_length），超长值会让 Postgres 抛
        # `value too long for type character varying(150)`、整条埋点被丢掉。
        username = user.get_username()[:150]
    else:
        # `0` 而不是 `NULL`：唯一约束里 `NULL` 互不相等，匿名行永远不会合并（spec §2）。
        user_id, username = 0, ""
    resolver_match = getattr(request, "resolver_match", None)
    # 接口**模板**（如 `api/business/orders/(?P<pk>[^/.]+)/`）；解析不到（404 / 中间件短路）一律归一成固定哨兵 `UNRESOLVED_ROUTE`（理由见常量定义处），⛔ **绝不**落 `request.path`。
    # ⚠️ 末尾切成 255 仍是**承重**的：列宽就是 255，写入走原始 SQL（绕过 ORM 的字段校验），已解析的模板理论上也可能超长（长正则），超长会让整条埋点以 `value too long` 失败。
    route = (getattr(resolver_match, "route", None) or UNRESOLVED_ROUTE)[:255]
    now = timezone.now()
    # `agg` 的桶是**整点**。`USE_TZ=True` ⇒ `now` 是 UTC；本仓 `TIME_ZONE` 是 Asia/Shanghai
    # （整小时偏移）⇒ UTC 整点与本地整点是同一个瞬间，两种截法等价。
    bucket_start = now.replace(minute=0, second=0, microsecond=0)
    common = {
        "route": route,
        "method": request.method[:8],
        "status_code": response.status_code,
        "user_id": user_id,
        "username": username,
        "duration_ms": duration_ms,
    }
    upsert_request_metric(kind="agg", bucket_start=bucket_start, **common)
    # spec §2：「耗时 **>** 阈值」的请求额外再写一条 slow 行（阈值取自 settings，⛔ 不硬编码）。
    if duration_ms > settings.REQUEST_METRICS_SLOW_MS:
        upsert_request_metric(kind="slow", bucket_start=now, **common)
