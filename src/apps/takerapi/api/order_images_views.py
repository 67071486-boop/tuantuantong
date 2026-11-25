"""打手侧**对外**端点：**进度图三条**（列表 `GET …/images/` + 取票 `POST …/images/ticket/` + 认领 `POST …/images/`；spec `docs/specs/2026-10-08-taker-open-api-design.md` §5.3-2/§5.4/§9）。两条 POST 的三段式直传形态与上家侧逐字同款（票→第三方 PUT→认领）；`POST …/images/` 与 `GET` 同住一个视图类（同路径第二条路由走不到）。
承重：范围判据=**「他是这张单的当前接手人」**（`order_scope.claimed_order_of` 唯一出处，三条进领域函数前逐条过；取不到 ⇒ `1002` 同形 ⛔ 不是空列表；⛔ 本模块不 import 抢单取单口那套 `scope_queryset`/`game_scope`）；领域口**复用**内部 `issue_screenshot_ticket`/`claim_screenshot`（⛔ 一个闸都不重写：白名单/20 张/行锁/0 字节/18 MiB/魔数/淘汰/两笔广播全在那两份里）。
`uploaded_by`=**绑定账号 `request.auth.taker`**（语义更准：图是他在传；上家侧取 `created_by` 是因为那边没有绑定账号；⛔ 不置空、⛔ 不造假用户；取票与认领必须同一值，否则"同一上传人"闸让每次认领**静默** `1000`）；留痕刻意不写（`DELIBERATELY_NOT_LOGGED`，可追溯靠幂等台账）；三条如实登记：终态单不关图闸（⛔ 不补第二道闸=分叉，拦不拦属业务裁定）、幂等回放不重查范围判据（不写库不泄信息，事实以查单为准）、取票极窄竞态回 404（⛔ 不修共享领域函数、⛔ 不为竞态加翻译路径；认领那一跳同一瞬落 `1000`，两形状不一致同为已登记边界）。"""
from __future__ import annotations

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.api.screenshot_views import claim_screenshot, issue_screenshot_ticket
from apps.business.models import OrderScreenshot
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.order_extra_views import _paginated
from apps.openapi.idempotency import idempotent, mark_side_effect
from apps.openapi.serializers import image_row
from apps.takerapi.api.order_scope import TAKER_ORDER_NOT_FOUND_MSG, claimed_order_of
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.throttling import TakerAppFallbackThrottle

#: 取票成功 `data` 的键集（恰好这三个；与上家侧同一份契约）。⛔ 不回内部主键 `screenshot_id`
#: （认领入口是对象名 `key`，第三方不需要行号）；⛔ 不回 `img_type`（本端点只收 `0`，回恒为 `0` 的键是废话）。
TICKET_DATA_KEYS = ("key", "upload_url", "expires_in")

#: 取票端点收 `img_type=1`（群聊二维码）时的那句话（**对外契约的一部分**，与上家侧逐字相同）。
#: ⚠️ 它是"参数不合法"那一档 ⇒ 码取 `1000`（⛔ 不静默按 `0` 处理、也⛔ 不落那一行）。
IMAGE_IMG_TYPE_MSG = "本接口只支持订单进度图（img_type=0）"

#: 三条端点的路径模板（供用例 import；⛔ 不放进 `urls.py`——那里只管路由。
#: 先例 = `apps/openapi/api/order_image_views.py` 的同名常量）。
LIST_PATH_TEMPLATE = "/api/taker/v1/orders/{order_no}/images/"
TICKET_PATH_TEMPLATE = "/api/taker/v1/orders/{order_no}/images/ticket/"


class TakerOrderImageTicketSerializer(serializers.Serializer):
    """`POST .../images/ticket/` 的入参（唯一校验口；与上家侧逐字同款）。`ext` 必填 ≤8，取值由领域函数按 `ALLOWED_EXTS` 判（表外 ⇒ `1000`）。
    `img_type` 可选默认 `0`、本端点**只放行 `0`**（判据在视图；⛔ 不写 `ChoiceField`——文案会变 DRF 内置）。"""

    ext = serializers.CharField(
        max_length=8, error_messages={"required": "缺少 ext", "blank": "ext 不能为空"}
    )
    img_type = serializers.IntegerField(required=False, default=0)


class TakerOrderImageClaimSerializer(serializers.Serializer):
    """`POST .../images/` 的入参（唯一校验口；与上家侧逐字同款）。`key` 必填=取票回的对象名，长度界=`OrderScreenshot.object_key` 列宽 255（超长在这里 `1000`，⛔ 不让它撞 PostgreSQL `value too long` ⇒ 500，E-0010）。
    `original_name` 可选 ≤255（与内网同界）；`remark` 可选 ≤**50**（落列宽 50，⛔ 别放宽成 255：那会让「请求能填、库里静默截断」）。"""

    key = serializers.CharField(
        max_length=255, error_messages={"required": "缺少 key", "blank": "key 不能为空"}
    )
    original_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    remark = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )


class TakerOrderImagesAssemblyMixin:
    """`…/images/…` 三条的共同装配（认证/无权限类/兜底闸）。⛔ 不各写一份：三条授权面逐字相同
    （"一个应用凭据=绑定的那个打手账号，且只对该账号接手的单"）⇒ 装配写一处（多一份覆写会让
    "这条路径提前切过装配"在读代码时看不见，AGENTS §5-E）。"""

    authentication_classes = [TakerAppBearerAuthentication]
    #: ⛔ 显式空列表 = **不做权限检查**（不写它会吃全局默认的 `IsAuthenticated`
    #: ⇒ 合法令牌反被判 401；与抢单对外面同款）。
    permission_classes = []
    #: 兜底闸（DRF 的 `initial()` 顺序 = 认证 → 权限 → 限流 ⇒ 401 先于 429，且不进桶）。
    throttle_classes = [TakerAppFallbackThrottle]

    @staticmethod
    def _claimed_or_failure(request, order_no):
        """范围判据（三条共用的唯一出口）；不是他的单 ⇒ `(None, 失败响应)`。
        返回 `(order, response)` 二元组（仓内既有"取或失败"形状）：调用方一句 `if order is None: return response`，⛔ 不重复那句文案。"""
        order = claimed_order_of(request.auth.taker, order_no)
        if order is None:
            # 不存在 / 不是他接的 / 已软删：**同一个出口**（spec §5.3-2 的"同形"）。
            return None, api_response(
                data=None, msg=TAKER_ORDER_NOT_FOUND_MSG, code=ErrorCode.BUSINESS
            )
        return order, None


class TakerOrderImageTicketView(TakerOrderImagesAssemblyMixin, APIView):
    """`POST /api/taker/v1/orders/<order_no>/images/ticket/` —— **取上传票**。"""

    @idempotent(spec=TAKER_SPEC)
    def post(self, request, order_no):
        serializer = TakerOrderImageTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # ⭐ 范围判据**先过**（⛔ 不进领域函数 ⇒ 不是他的单时一行都不写）。
        order, failure = self._claimed_or_failure(request, order_no)
        if order is None:
            return failure
        img_type = serializer.validated_data["img_type"]
        # ⭐ 对外**只允许 `0`**（差异登记：对标 `UploadImg` 的 `ImgType=1` 是群聊二维码=内部业务动作，第三方没有它的语义）。
        # 业务参数不合法 ⇒ `1000`；码非 0 ⇒ 幂等占位行回滚 ⇒ 同一颗键下次可重试（⛔ 不会永久卡"正在处理中"）。
        if img_type != OrderScreenshot.IMG_ORDER:
            return api_response(
                data=None, msg=IMAGE_IMG_TYPE_MSG, code=ErrorCode.VALIDATION
            )
        outcome = issue_screenshot_ticket(
            order_no=order_no,
            ext=serializer.validated_data["ext"],
            img_type=img_type,
            # ⭐ **绑定账号**（理由见模块 docstring 的「`uploaded_by`」那一节）。
            uploaded_by=request.auth.taker,
        )
        if not outcome.ok:
            # 领域层的可预期失败（后缀不在白名单 `1000` / 超过 20 张 `1000`）原样出去。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        # ⚠️ **按白名单挑键**（⛔ 不是 `pop("screenshot_id")`）：白名单让"将来领域结果
        # 多出一个内部字段"**不会**静默出现在对外契约上（多一个键就是一次契约变更）。
        return api_response({name: outcome.data[name] for name in TICKET_DATA_KEYS})


class TakerOrderImagesView(TakerOrderImagesAssemblyMixin, APIView):
    """`/api/taker/v1/orders/<order_no>/images/` —— 列表（`GET`）+ 认领（`POST`）。
    ⚠️ 两个方法必须同住这个类（框架事实 ⛔ 别再注册一条路由）：同一条路径第二条 `path()` 永远走不到（先例：上家侧 `OrderImagesView`、`status/`）。"""

    #: 每项的键集=**上家侧那一份**（`apps/openapi/serializers.py::image_row`）——⛔ 本模块不拼第二份装配：
    #: 两族对外面的"一张进度图"是同一件事（同一模型同一套列），形状分叉=两边第三方各读一套字段名。
    #: 该函数内部预签名读链接（生产 COS 为整点对齐的 1–2 小时）并读一次 `uploaded_by`；本类实现体只有 get/post 各一行判定，没有可抽的共享逻辑。

    def get(self, request, order_no):
        """该单的进度图列表（**分页**；只给**已认领完成**的图）。取行口径与上家侧同一（三条一起）：
        `status=ready` + 软删单排除（D-0144）+ `order_by("pk")` 最旧在前（`OrderScreenshot` 刻意无 `Meta.ordering`，顺序由读路径自己声明）。
        ⚠️ 先过范围判据：取不到/不是他接的 ⇒ `1002`（⛔ 不是"空列表"——那会让第三方分不清"这单没有图"与"这单不在你这里"）。"""
        order, failure = self._claimed_or_failure(request, order_no)
        if order is None:
            return failure
        rows = (
            OrderScreenshot.objects.filter(
                order__order_no=order_no,
                order__deleted_at__isnull=True,
                status=OrderScreenshot.STATUS_READY,
            )
            # ⚠️ 预取上传人：`image_row` 的显示名要读 `uploaded_by` 那一行
            # （不预取 = 每行一条查询）。
            .select_related("uploaded_by")
            .order_by("pk")
        )
        return api_response(_paginated(rows, request, image_row))

    @idempotent(spec=TAKER_SPEC)
    def post(self, request, order_no):
        """**认领**：把刚直传上去的对象落成 `ready`（与上家侧认领端点调**同一个**函数）。"""
        serializer = TakerOrderImageClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order, failure = self._claimed_or_failure(request, order_no)
        if order is None:
            return failure
        outcome = claim_screenshot(
            order_no=order_no,
            # ⚠️ 对外按**对象名**选那一行（第三方手里只有 `key`；⛔ 我们没有把行号给出去）。
            object_key=serializer.validated_data["key"],
            original_name=serializer.validated_data["original_name"],
            remark=serializer.validated_data["remark"],
            # ⭐ **与取票那一跳同一个值**（绑定账号）：`claim_screenshot` 的"同一上传人"闸
            # 就是靠它成立的（理由见模块 docstring 的「`uploaded_by`」那一节）。
            uploaded_by=request.auth.taker,
        )
        if not outcome.ok:
            # ⭐⭐ **拒收即清理四档（超限/0 字节/超 18 MiB/魔数认不出）必须声明「副作用已落库」**（`writes_kept=True` ⇒ `mark_side_effect`，上家侧变异实测的必要一步，本线逐字沿用）：
            # 它们删行+删对象，幂等入口对「码 ≠ 0」默认回滚，而 `storage.delete` 非事务 ⇒ 不声明就留下指向已删对象的 `pending` 行（张数永久多占、答复漂移）。
            # 口径 ⛔ 不是"失败了就标记"：不写库的失败档（没有待认领的行/文件没传上来）不声明，占位行照旧回滚（同键可重试）。
            if outcome.writes_kept:
                mark_side_effect(request)
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        # ⚠️ 载荷走**列表项那一个出口**（`image_row`）⇒ 「认领回来的那一项」与
        # 「列表里那一项」逐键同形（⛔ 不拼第二份）。
        return api_response(image_row(outcome.row))
