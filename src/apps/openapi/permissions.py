"""锁定应用（`ApiAppKind.WECOM_GROUP_QR`）的**端点白名单**（设计文档 §6，D3 / D4 / D5）。

本模块只做一件事：回答「**这个（HTTP 方法, `url_name`）允不允许锁定应用打**」。
⛔ 它**不碰**通用应用（`ApiAppKind.GENERAL`）—— 通用应用的授权面一字不变
（§13.10 的裁定：一个通用应用凭据 = 本租户全部业务数据）。

## 这道闸经 `spec` 判，不要改回读 `app.kind`

调用方是两个凭据族共用的认证基类（打手侧子类只覆盖 `spec = TAKER_SPEC`）。
每一族的请求都会路过，所以「要不要判」必须由 `spec` 回答。`TakerApp` 没有 `kind` 列，
读 `app.kind` 会 `AttributeError`，打手侧每个请求都 500。

⚠️ 所以「锁定应用是哪一类行」这件事和其余可参数化面一样**住在 `CredentialSpec`**
（`locked_app_kind`，`apps/openapi/credentials.py`）：开放平台那一份写 `WECOM_GROUP_QR`、
打手侧那一份写 `None`（= 这一族没有"锁定应用"这个概念 ⇒ **第一行就返回**）。
它的后果是**结构性**的：打手侧那条路**连一次属性读都不会发生**，⛔ 不是"多一层 try"。

## ⭐ 这是 **deny-by-default**（本模块存在的全部理由）

判据是**恰好集合**：只有 `WECOM_GROUP_QR_ALLOWED_ENDPOINTS` 里逐字列出的
`(HTTP 方法, url_name)` 才放行，**其余一律拒**。⇒ **新增一条对外端点不会自动放行**，
它要显式进这张表才能被锁定应用看到（fail-closed）。

## ⛔ 为什么判据不住在视图上（不挂权限类）

对外视图**逐条**写 `permission_classes = []`（`api/order_query_views.py` /
`api/order_extra_views.py` / `api/order_write_views.py`，v2 §13.10 的裁定）。
若把这条判据做成一个权限类、要求**每条**对外视图记得挂上，那么：

- **漏挂一次 = 静默放行** —— 那条端点对锁定应用完全敞开，而**没有任何地方会报错**：
  测试绿、页面正常、日志干净，直到有人去读那条端点的代码；
- 而认证层的放行表**漏加一条**的后果是**响亮 403**（合规调用方当场被打回、立刻有人来查）。

⇒ 同一件事的两种失败形态里，这一种是**会被发现**的那种。判据因此放在**认证层的最后一步**
（`ApiAppBearerAuthentication.authenticate()`），⛔ 不是放在视图上。

## ⛔ 判据为什么是 `url_name`，不是路径前缀

按路径前缀放行（例如 `path.startswith("/api/open/v1/orders/")`）会被**路径变形**绕过：
多一个斜杠、大小写、`%2f`、`..`、查询串写法都会改变字符串比较的结果，而**路由匹配用的是
Django 自己解析出来的那条路由**。「(方法, `resolver_match.url_name`)」是**路由表上的身份**
（D3），它由 Django 在调用视图之前解析好，请求方**改不了**（改了就落到别的 `url_name`）。

## ⚠️ 代价（如实登记）

- 这条判据**看不见**「同一个视图类被两条路由挂载」这种情况：两条路由各有自己的 `name`
  ⇒ 两条都要进表。漏一条的症状是**响亮 403**（见上），不是静默放行；
- `url_name` 为空（路由没写 `name=`）⇒ **拒**（fail-closed，D3 明文）。
  ⛔ 别为了"让某条新端点能过"去给它放宽成"空名也放行"——那会让**所有**没写 `name=` 的路由
  （含将来新增的）一起对锁定应用敞开。
"""
from __future__ import annotations

from rest_framework import exceptions

from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec
from apps.openapi.models import ApiAppKind

#: 锁定应用**只**允许这些 `(HTTP 方法, url_name)`（设计文档 §6.1，**唯一出处**）。
#:
#: ⚠️ 判据是**恰好集合**：这张表 == spec D4 逐字列出的六条，多一条少一条都必须有判据解释
#: （`tests/test_openapi_wecom_qr_scope.py` 的两向断言就是干这个的）。
#:
#: ⚠️⭐ **同名挂多个方法的共四组**（D4 明文点名）：判据**必须按方法收窄**，只看 `url_name`
#: 等于把写权限一起放出去：
#:
#: | `url_name` | 本表放行 | 同一条路由上**刻意不放行**的第二个方法 |
#: |---|---|---|
#: | `open-order-query` | `GET`（按时间增量拉单） | **`POST` 录单** |
#: | `open-order-detail` | `GET` | **`PATCH` 改单** |
#: | `open-order-status` | `GET` | **`POST` 改状态** |
#: | `open-order-images` | `GET` **与** `POST`（认领） | —— 两个都放行（这组是唯一的例外） |
#: | `open-order-image-ticket` | `POST`（取票，只收 `img_type=1`，见 D6） | 它只有这一个方法 |
#:
#: ⛔ 别把 `("POST", "open-order-images")` 与 `("POST", "open-order-image-ticket")` 混成一条：
#: 它们是**两条不同的路由**（两条 `path()`，两个 `name`），合并会漏掉其中一条。
WECOM_GROUP_QR_ALLOWED_ENDPOINTS = frozenset({
    ("GET", "open-order-query"),         # 按时间增量拉单（本线新增的用法）
    ("GET", "open-order-detail"),
    ("GET", "open-order-status"),
    ("GET", "open-order-images"),
    ("POST", "open-order-images"),       # 认领
    ("POST", "open-order-image-ticket"),  # 取票（只收 `img_type=1`，见 D6）
})

#: 越权那一档的 `msg`（D5；**唯一的出处**，⛔ 别在调用点再抄一遍字面量）。
#:
#: ⚠️ 口径与大白话一致：**凭据是对的**（它已经过了验签 / `state` / IP 白名单三道闸），
#: 只是**这次操作不归它**。⛔ 它**不是** 401 那一族（401 = 凭据问题）——
#: 401 的文案若在这里复用，调用方会把「我的密钥配错了」当成原因去改一个**没错**的地方。
WECOM_GROUP_QR_ENDPOINT_NOT_ALLOWED_MSG = "该应用无权访问本端点"


class EndpointOutOfScope(exceptions.PermissionDenied):
    """锁定应用打了白名单外的端点（**授权失败**：凭据对、但这次操作不归它）。

    ## ⛔ 为什么它继承 `PermissionDenied`（**既有 403 口径的唯一出口**）

    本仓**已经**有一条把"已认证但没权限"翻成 403 的路：`PermissionDenied`
    → `apps/core/exceptions.py` 里那个分支 → **HTTP 403 + `{code: 403, msg, data: null}`**
    （`api/order_image_views.py` 的「对象名不属于本租户」走的就是它；`docs/OPEN-API.md`
    的码表第 403 行逐字同一形状）。

    拒绝走既有口径：HTTP 403 + 业务码 403，与现行 D5 一致。
    信封出口对没枚举的异常让 HTTP 码和业务码取同一个数，所以做不出
    「HTTP 200 + 业务码 403」。改那条分叉要动 `apps/core/exceptions.py`。
    「凭据对、操作不归你」由上面那句 `msg` 承担，不要复用 401 的文案。

    ## 它为什么是一个异常，而不是一个返回值

    DRF 的 `BaseAuthentication.authenticate()` 的契约只有两种出口：**返回 `(user, auth)`**
    或**抛异常**（`rest_framework/authentication.py` 的 `BaseAuthentication` 与
    `APIView.perform_authentication`）。认证层**拿不到一个「请回这个响应」的出口**
    ⇒ 「拒」这件事只能靠抛。

    ⚠️ 但它必须是一个**能被本仓异常处理器认识**的类型：抛一个框架不认识的异常
    （`ValueError` 之类）会掉进 `envelope_exception_handler` 的 `response is None` 那一支
    ⇒ Django 的 **500** —— 那正是本任务明令不许出现的形状（「拒」是**预期**结果，不是缺陷）。

    ## 与它继承来的那一档的关系

    行为上与裸 `PermissionDenied` **逐字相同**（同一个分支、同一个信封形状）；
    单独成一个类只是为了让**判据能指认它**（用例断的是那条路由 + 那个 `msg`，
    而这个 `msg` 只有本线这一处会产生）—— ⛔ 不是"加一层没用的子类"：
    去掉它，判据就只能断"某个 403"，分不出是本线这道闸还是别的 403 来源。
    """

    #: `msg` 的默认值就是本线那句话（⛔ 唯一出处是本文件上面那个常量）。
    default_detail = WECOM_GROUP_QR_ENDPOINT_NOT_ALLOWED_MSG


def is_endpoint_allowed(*, method: str, url_name: str | None) -> bool:
    """锁定应用能不能打这个 `(方法, url_name)`（**判据的唯一实现**）。

    ## 口径（逐条都能被用例证伪）

    - `url_name` 为 `None` / 空串 ⇒ **拒**（D3：路由没写 `name=` ⇒ fail-closed）；
    - `method` 原样参与比较（`request.method` 是**大写**的，路由表里的键也是大写）：
      ⛔ 这里**不做** `upper()` 之外的花样归一（大小写敏感是**Django 的事实**，
      它自己也是按大写比较的）；
    - **恰好集合**：不在表里 ⇒ 拒（deny-by-default）。
    """
    if not url_name:
        return False
    return (method.upper(), url_name) in WECOM_GROUP_QR_ALLOWED_ENDPOINTS


def enforce_app_endpoint_scope(app, request, *, spec: CredentialSpec) -> None:
    """**锁定应用**的端点闸（§6.2：认证七步之后的**第八步**）。

    ## 本函数做的三件事

    1. ⛔ **本族没有"锁定应用"这个概念 ⇒ 直接返回**（`spec.locked_app_kind is None`；
       通用应用与**打手侧整族**都走这一支，一个字节都不受影响）——
       本线的授权面收窄**只针对锁定应用**；
    2. 取 `request.resolver_match.url_name`（DRF 在 `APIView.initial()` 里
       `self.resolver_match = request.resolver_match`，而 `initial()` **早于**认证完成赋值；
       ⚠️ 本函数**不依赖**那个副作用，它读的是 Django 挂在请求上的那一个 —— 更靠前、更硬）；
    3. 取不到 `resolver_match` / `url_name` 为空 / 不在白名单 ⇒ 抛 `EndpointOutOfScope`
       （HTTP 403 + 业务码 403）。不放行。

    ## 为什么第一行判的是 `spec.locked_app_kind`，不是 `app.kind`

    调用方是**两个凭据族共用**的认证基类 ⇒ 打手侧的 `TakerApp` 也路过这里，而它
    **刻意没有** `kind` 这一列 ⇒ 读 `app.kind` 会当场 `AttributeError` ⇒ 打手侧**每个请求 500**。
    判据改从 `spec` 取（口径与本仓其余可参数化面一致：同一件事只住一处；`AGENTS.md` §5-E）
    ⇒ 打手侧那一族**连一次属性读都不会发生**（`TAKER_SPEC.locked_app_kind is None`）。
    ⛔ 别为了"少传一个参数"把它改回 `getattr(app, "kind", None)`：那是**静默**的
    —— 凭据模型**将来**再加一列同名不同义的东西时，这条闸会**悄悄按新列的语义**判。

    ## 为什么取不到 `resolver_match` 也要拒（而不是"测试环境就放行"）

    它在生产里**不该发生**（Django 在调用视图前就解析好了路由）。真发生了只有两种可能：
    路由表被换掉了，或者有人绕过 URLconf 直接把请求打进视图 —— 两种情况下"这个请求是打给
    哪个端点的"都**说不清**，而**说不清就不放行**（fail-closed）。
    ⛔「取不到就放行」会把这条判据变成一个**能被构造出来的旁路**。

    ## ⚠️ 为什么它不在认证类里内联（本模块存在的意义）

    判据与文案都只有**一份**实现：认证类那边只调这一个函数。将来若第二条认证路径也要这条闸
    （例如又出现一种对外凭据），它调的是**同一个**函数，⛔ 不是抄一份
    （`AGENTS.md` §5-E：同一个事实两套判据）。

    ## ⚠️ `spec` 是**必填** keyword-only（⛔ 不给默认值）

    口径与 `apps/openapi/authentication.py` 那几个 `_failure_count` / `_mark_used` 逐字相同：
    漏传的后果是**静默**按上家侧处理，而这里漏传的后果更贵 —— 它决定「这道闸到底开不开」。
    必填 ⇒ 漏传 = **构造期（调用期）就 `TypeError`**。
    """
    if spec is OPENAPI_SPEC and app.kind == ApiAppKind.BUSINESS_INTEGRATION:
        match = request.resolver_match
        if match is None or match.namespace != "business_integration":
            raise EndpointOutOfScope("业务应用无权访问本接口")
        return
    if spec.locked_app_kind is None:
        # ⛔ 这一族没有"锁定应用"：不看 `resolver_match`、不读白名单、不产生任何新失败面。
        # ⚠️ 通用应用在本族里也走这一支（`locked_app_kind` 说的是"这一类行存不存在"，
        #    而"这一行是不是锁定应用"由下面那一句判）。
        return
    if getattr(app, "kind", None) != spec.locked_app_kind:
        # ⛔ 通用应用到这里就结束（同一个 `ApiApp` 表里 `kind=general` 的那些行）。
        return
    resolver_match = getattr(request, "resolver_match", None)
    url_name = getattr(resolver_match, "url_name", None)
    if not is_endpoint_allowed(method=request.method, url_name=url_name):
        raise EndpointOutOfScope(WECOM_GROUP_QR_ENDPOINT_NOT_ALLOWED_MSG)
