"""开放平台对外 v2 的**录单**端点（`POST /api/open/v1/orders/`；spec §6.1/§6.2/§6.3/§13.14-3）。契约：Bearer ⛔ 不收会话 cookie、幂等必带 `Idempotency-Key`（缺失 ⇒ `1000`）、成功回这张单的**查单载荷**（`order_payload`，与 `GET v1/orders/<单号>/` 同形状）；领域=⛔ 不重写建单逻辑，调 `order_intake.create_order`（内部录单同一个函数）。
承重：字段白名单 `CREATE_FIELDS`（唯一出处；取证自内部 `OrderSerializer` 可写字段 ⛔ 不照抄对标拼写）——表外的键一律**拒**（fail-closed：静默丢字段=缺陷；代价=照对标文档写的调用方直接失败，解法是文档写差异对照表）；对外比内部严的两项 `EXTRA_REQUIRED_FIELDS`（`total_price` 缺=静默读错钱；`source_order_no` 缺=所有空号单互相去重）；来源单号去重用合成幂等键（⛔ 不拿 `Order.source_order_no` 当事实源——不分应用、会跨应用误判把内部单回放出去；窗口=保留期 30 天，超窗同号会再建单，代价要写进对外文档）；`1022`/`1023` 单已落库 ⇒ 判据是 `IntakeOutcome.order is not None` ⛔ 不是信封 `code`，且必须 `mark_side_effect`（否则订单被回滚、响应说「单已建好」=假话，推导见 `apps/openapi/idempotency.py` docstring）。
装配由宿主声明 ⛔ mixin 不自带（宿主 GET/POST 授权面不同：`GET` 走 v1 节点门、`POST` ⛔ 不装权限类）；⛔ 不做游戏范围过滤（`assert_game=None`）；租户上下文只来自连接 ⛔ 不读请求参数；`request.user` 是匿名 ⇒ `IntakeActor.user=None`。过程教训（可复用，AGENTS §5-G 同族）：改一条**路径**的语义 ⇒ 受影响网清单必须含「任何拿这条路径当样本/夹具的网」（grep 路径字符串在 `src/tests/**` 的借用点）。"""
from __future__ import annotations

from apps.business.api.serializers import CreateOrderAssignSerializer, OrderSerializer
from apps.business.order_intake import IntakeActor, create_order
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.order_write_views import (
    OPEN_API_ACTOR_TEMPLATE,
    OPEN_API_FULL_NODES,
    _write_context,
)
from apps.openapi.credentials import OPENAPI_SPEC, spec_of
from apps.openapi.idempotency import (
    idempotent,
    mark_side_effect,
    remember_source,
    seen_source_response,
)
from apps.openapi.serializers import order_payload

#: ⭐ 对外录单的**字段白名单**（唯一出处）。与 `OrderSerializer` 可写字段的对拍由用例钉住
#: （`tests/test_openapi_order_create.py::CreateShapeTests::test_the_whitelist_only_names_writable_order_fields`）
#: ——名字拼错 / 内部改字段名这里没跟 ⇒ 那条用例红（否则表外名字被静默忽略）。
CREATE_FIELDS = frozenset(
    {
        # ── 订单列（内部 `OrderSerializer` 可写）──────────────────────────────
        "game",  # ← 草案 `game_id`
        "project_text",
        "total_price",  # ← 草案 `price`（金额**字符串两位小数**；形状判据在内部序列化器里）
        "source_order_no",  # 必填且非空（去重键，见模块 docstring 的 ⭐ 那一节）
        "server",  # ← 草案 `game_area` / `game_server`（本系统只有"区服"这一个 FK）
        "game_account",
        "game_role_name",  # ← 草案 `game_role`
        "content",
        "remark",  # ⚠️ 写得到、查单**读不回**（D 组：内部痕迹）——有意的写读不对称
        "hall_price",  # ← 草案 `assign_price`（发单价）
        # ── **不是订单列**的两个键（各自有独立序列化器，见 `CreateOrderAssignSerializer`）──
        "assignee",  # 建单后指派给这个内部用户
        "throw_to_hall",  # 建单后抛进内部抢单大厅（与 `assignee` 互斥）
    }
)

#: 对外**比内部额外**要求的两项的**人话文案**（§6.3 / 模块 docstring 的 ⭐ 那一节）。
#: ⚠️ 顺序 = 报文里的列举顺序（判据不看顺序，只钉"缺了哪些"）。
EXTRA_REQUIRED_FIELDS = ("total_price", "source_order_no")

MISSING_EXTRA_FIELDS_MSG = "缺少必填参数：{}"
NOT_A_JSON_OBJECT_MSG = "请求体必须是一个 JSON 对象"
#: 表外的键（本 API **不接受**的字段）那条文案。⚠️ 它会**点名**是哪些键 ——
#: 第三方照对标文档写的调用方因此能在**第一次调用**就看到"这个字段我们不收"，
#: 而不是以为写进去了（对照表在 `docs/OPEN-API.md` §4.6）。
UNKNOWN_FIELDS_MSG = "请求里有本 API 不接受的字段：{}"


def _is_blank(value) -> bool:
    """「没给」的判据：`None` 或全是空白的字符串（⛔ 不用真值——E-0021 族）。
    `0` / `"0.00"` / `False` 都**不是**"没给"（金额 0 是合法值 D-0015、`throw_to_hall=false` 也是显式给了值）。只服务上面两个键的**必填**。"""
    if value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _whitelisted_payload(request, allowed_fields=CREATE_FIELDS):
    """请求体 ⇒ `(字典, 1000 信封或 None)`：**白名单之外出现任何键就拒**（fail-closed）。首版按白名单过滤（表外键静默不生效）被裁定为**静默丢字段**（调用方以为写进去了、库里没有、无任何信号）⇒ 现在：表外任何键（含未知键、含 `""`/`null` 空值——⛔ 不特判，否则多一条没有判据的分支）⇒ `1000`+一行不落库（幂等占位行一起回滚 ⇒ 同键可重试）；表内可选字段缺失照旧（⛔ 别误伤成全必填）。代价如实：照对标文档写的调用方直接失败——解法是文档写差异（「对标有、我们没有」对照表），第一次调用就暴露。
    判据顺序（顺序本身是判据）：①非对象体（JSON 数组/标量 ⇒ `.items()` 抛 `AttributeError` ⇒ 500；AGENTS §5-E 判"这类请求还会不会 500"；⛔ 不交给序列化器去炸——本仓对参数不合法的既定答复是 200+`1000`）→ ②未知键 ⇒ 拒 → ③缺 `EXTRA_REQUIRED_FIELDS` ⇒ 拒 → ④才交给两个序列化器（落库的值一律过序列化器）。"""
    raw = request.data
    if not isinstance(raw, dict):
        return None, api_response(
            data=None, msg=NOT_A_JSON_OBJECT_MSG, code=ErrorCode.VALIDATION
        )
    # ⚠️ `sorted()` 只为**文案稳定**（两次同样的报文给出同一句 msg ⇒ 判据可写死那条 msg）。
    unknown = sorted(set(raw) - allowed_fields)
    if unknown:
        return None, api_response(
            data=None,
            msg=UNKNOWN_FIELDS_MSG.format("、".join(unknown)),
            code=ErrorCode.VALIDATION,
        )
    return dict(raw), None


def _missing_extra_fields(payload) -> list:
    """`EXTRA_REQUIRED_FIELDS` 里缺了 / 空了的那些（⛔ 不判内部那两项：那在领域核心里）。"""
    return [name for name in EXTRA_REQUIRED_FIELDS if _is_blank(payload.get(name))]


def _intake_actor(app) -> IntakeActor:
    """把「哪个应用在录单」装成领域核心的 `IntakeActor`（逐字段理由见那个模块 docstring）。三个 `None`/空串**都不是"忘了传"**：
    `user=None`（Bearer 下没有 `User`，`sales_user`/`hall_publisher` 留空）、`log_actor_id=None`（留痕落系统行，与既有两条写端点同款）、`assert_game=None`（§13.10 在游戏准入层的表达 ⛔ 不是漏闸）、`sales_agent_fallback=""`（这条路上没有"客服"可兜底）。"""
    label = OPEN_API_ACTOR_TEMPLATE.format(app.name)
    return IntakeActor(
        user=None,
        log_actor=label,
        log_actor_id=None,
        nodes=OPEN_API_FULL_NODES,
        write_ctx=_write_context(app),
        sales_agent_fallback="",
        assert_game=None,
    )


class OrderCreateMixin:
    """`POST /api/open/v1/orders/` 的处理器（**录单**）。必须是 mixin（框架事实）：`GET` 已挂 `order_query_views.OrderSourceQueryView`、契约要求 `POST` 同一条路径——同一路径第二条路由永远走不到 ⇒ 两方法同住一个视图类（同族端点实测过同一个坑）。
    ⚠️ 装配由宿主声明 ⛔ 本 mixin 不自带：宿主 GET/POST 授权面不同（`GET` 仍走 v1 节点门、`POST` ⛔ 不装权限类 §13.10）⇒ 挪进 mixin 会让那条 `GET` **静默失去节点门**。"""

    allowed_create_fields = CREATE_FIELDS
    create_payload_for_order = staticmethod(order_payload)

    def intake_actor(self, request):
        return _intake_actor(request.auth)

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request):
        """录单：白名单过滤 → 必填闸 → 两个序列化器 → 去重 → 领域核心 → 台账。
        成功回这张单的查单载荷（`order_payload`，与 `GET` 同一装配）⛔ 不是内部录单的 `OrderSerializer(...).data`（含员工身份/`sales_user` 主键等 D 组内容=对内形状）；失败/中间态（`1000`/`1001`/`1022`/`1023`）信封与内部录单逐字相同（领域核心给的）。"""
        payload, error = _whitelisted_payload(request, self.allowed_create_fields)
        if error is not None:
            return error
        missing = _missing_extra_fields(payload)
        if missing:
            return api_response(
                data=None,
                msg=MISSING_EXTRA_FIELDS_MSG.format("、".join(missing)),
                code=ErrorCode.VALIDATION,
            )
        serializer = OrderSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        # 「建单 + 指派 / 抛单」那两个**不是订单列**的键：独立序列化器（与内部录单同一个）。
        assign_input = CreateOrderAssignSerializer(data=payload)
        assign_input.is_valid(raise_exception=True)
        source_order_no = serializer.validated_data["source_order_no"]

        # ── ⭐ 来源单号去重（§6.3）：同一应用+同号已录 ⇒ 回放那一单（⛔ 不建第二单）；排在领域核心之前（命中就零副作用），它自己判"快照非空"、占位行不会被当成结果发出。
        # ⭐ `spec` 显式传且必填 keyword-only（⛔ 不给默认值：漏传 ⇒ 静默查/写**另一族**台账，症状=一族录过的单在另一族被回放）；
        # 这里经认证态取（`spec_of(request)`）；幂等入口那一侧由装饰器显式声明（漏传=构造期 `TypeError`）。
        spec = spec_of(request)
        replay = seen_source_response(
            app=request.auth, source_order_no=source_order_no, spec=spec
        )
        if replay is not None:
            return replay

        outcome = create_order(
            serializer=serializer,
            assign_input=assign_input,
            actor=self.intake_actor(request),
            payload_for_order=self.create_payload_for_order,
        )
        if outcome.order is None:
            # 前置闸 / 校验把这一单拒了（一行都没写）⇒ 占位行照旧由幂等入口回滚
            # （同一个键**下次可以重试**），⛔ 不声明副作用、⛔ 不记来源单号。
            return outcome.response
        # ⭐ 到这一行：**订单已经落库**（`1022` / `1023` 也算）⇒ 幂等入口绝不能回滚它。
        mark_side_effect(request)
        # ⭐ 与上面同一笔事务里记下「这个来源单号已经录过」（理由与代价见模块 docstring）。
        remember_source(
            app=request.auth,
            source_order_no=source_order_no,
            request=request,
            response=outcome.response,
            spec=spec,
        )
        return outcome.response
