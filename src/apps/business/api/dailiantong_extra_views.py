"""代练通「**有、我们没用上**」那批动作的接口：`POST …/platform-push/up-price/`（未接手单**加价**）、`POST …/platform-push/arbitration/`（申请客服介入）、`POST …/platform-push/agree-cancel/`（**同意撤销**，打手申请撤销后）、`POST …/platform-push/cancel-request/`（**取消撤销**，仅我方发起 `Flag=1`）、`POST …/platform-push/value/`（评价打手）、`POST …/platform-push/off-sell/`（上架 / 下架 `flag` 1/0）、`POST …/platform-push/lock/`（锁定 / 解锁 `locked` 1/0）、`POST …/platform-push/blacklist/`（拉黑 / 解拉黑打手 `flag` 0/1）、`GET /api/business/platform-integrations/stats/`（按日统计，代理 `LevelOrderStat`、带缓存）、`GET`/`POST /api/business/platform-integrations/order-templates/`（代练通侧订单要求模板列表 / 新增）、`PATCH`/`DELETE …/order-templates/<int:temp_id>/`（改 / 删模板）、`POST …/order-templates/<int:temp_id>/default/`（设默认模板）。
⚠️ **命名**：这里的「订单要求模板」是**代练通侧的模板**（`OrderRequireTemp*`），与本仓自己的「代练要求模板」（发单页那份全局模板 / `PlatformPushSettings`）**是两回事**，⛔ 不要互相引用。服务层在 `apps/business/dailiantong_extra_actions.py`（前置闸 / 上游调用 / 回读 / 审计都在那里），本文件只做「取可见订单 → 校验入参形状 → 调服务 → 翻信封」。
权限 / 失败分层 / 覆盖网 / 凭据红线（与 `dailiantong_action_views.py` 同一套）：**读**动作沿用 `order.list`，**写**动作（加价 / 仲裁 / 同意撤销 / 取消撤销 / 评价 / 上下架 / 锁定 / 拉黑 / 模板新增改删设默认 / 加款 / 编辑 / 截图）换到 `order.platform_write`，`GET`/`POST` 同类的模板列表按**方法**分派节点；节点 code 写成**模块常量字面量**（`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的字符串常量，变量拼出来的 code 不会被它看见）。失败分层（⛔ 不要 500）：前置闸 / 上游业务失败 / 回读失败 / 结果未知 ⇒ HTTP 200 + `code=1002` + 中文 `msg`（服务层统一抛 `DailiantongPushError`）；入参形状不对 ⇒ 400 信封包成 HTTP 200 + `code=1000`，⛔ 不进服务层、⛔ 不碰上游；成功 ⇒ `code=0` + 订单动作回**推送行**、模板 / 统计回各自数据对象。每个 `post` 都把服务调用写在**自己**的方法体里：`tests/test_operation_log_coverage.py` 按「HTTP 方法的入口方法体」跟 `record(...)`，藏进非 DRF 命名的公共 `perform_*` 会让覆盖网**报不出**埋点。凭据红线（AGENTS.md §4）：响应里**只有推送行 / 模板 / 统计**，⛔ 没有 `app_secret`、`pay_password_md5` / `pay_pass` / 明文 `GamePass`（服务层只取几个标量）。
"""

from datetime import timedelta
from decimal import Decimal

from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.business.api.dailiantong_push_views import (
    DailiantongPushSerializer,
    _not_found,
    _service_failure,
    _visible_order,
)
from apps.business.dailiantong_extra_actions import (
    LOCK_REASON_MAX_LENGTH,
    ORDER_TEMPLATE_CONTENT_MAX_LENGTH,
    ORDER_TEMPLATE_TITLE_MAX_LENGTH,
    PUSH_SCREENSHOT_CAPTION_MAX_LENGTH,
    STATS_DEFAULT_DAYS,
    STATS_SIDE_ACCEPT,
    STATS_SIDE_PUBLISH,
    VALUE_COMMENT_MAX_LENGTH,
    add_push_amount,
    add_push_screenshot,
    blacklist_taker,
    create_order_template,
    default_stats_window,
    delete_order_template,
    edit_push_order,
    # ⭐ 打手申请撤销后我方**同意撤销**（`LevelOrderCancel(Flag=2)`）。
    agree_cancel,
    cancel_withdraw_request,
    list_order_templates,
    lock_push_order,
    off_sell_push_order,
    platform_order_stats,
    request_push_arbitration,
    set_default_order_template,
    update_order_template,
    up_price_push,
    value_push_order,
)
from apps.business.dailiantong_push import (
    GROUPS_MAX_LENGTH,
    LABEL_MAX_LENGTH,
    MEMO_MAX_LENGTH,
    REQUIRE_TEMPLATE_MAX_LENGTH,
    TITLE_MAX_LENGTH,
    TITLE_MIN_LENGTH,
    DailiantongPushError,
)
from apps.core.storage import MAX_IMAGE_BYTES, sniff_image_bytes
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 本组接口共用的 RBAC 节点（订单管理列表；⛔ 不新增节点）。
EXTRA_ACTION_NODE = "order.list"
#: ⭐ M5：写动作的专属节点（涉钱 / 写上游）。**读**动作
#: （按日统计 / 模板列表 GET）仍用 `EXTRA_ACTION_NODE`。
PLATFORM_WRITE_NODE = "order.platform_write"

#: 满意度四档（`LevelOrderValue` 的 `EvalType`；值与中文都照文档）。
EVAL_TYPE_CHOICES = [
    (10, "非常满意"),
    (11, "满意"),
    (12, "一般"),
    (13, "很差"),
]

#: 上架 / 下架方向（`LevelOrderOffSell` 的 `Flag`）。
OFF_SELL_CHOICES = [(1, "上架"), (0, "下架")]
#: 锁定 / 解锁方向（`LevelOrderLock` 的 `Locked`）。
LOCK_CHOICES = [(1, "锁定"), (0, "解锁")]
#: 拉黑 / 解拉黑方向（`AddBlackList` 的 `Flag`）。
BLACKLIST_CHOICES = [(0, "拉黑"), (1, "解除拉黑")]


class UpPriceSerializer(serializers.Serializer):
    """`POST …/platform-push/up-price/` 的入参：加价**增量**，最多两位小数。"""

    up_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        error_messages={
            "required": "请填写要加的金额",
            "invalid": "加价金额格式不对（最多两位小数）",
            "max_digits": "加价金额太大",
            "max_decimal_places": "加价金额最多两位小数",
        },
    )


class ValueSerializer(serializers.Serializer):
    """`POST …/platform-push/value/` 的入参：`eval_type` 四档 + 可选 `comment`。"""

    eval_type = serializers.ChoiceField(
        choices=EVAL_TYPE_CHOICES,
        error_messages={
            "required": "请选择满意度",
            "invalid_choice": "满意度只能是非常满意 / 满意 / 一般 / 很差",
        },
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=VALUE_COMMENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"评价内容不能超过 {VALUE_COMMENT_MAX_LENGTH} 个字符",
            "invalid": "评价内容格式不对",
        },
    )


class OffSellSerializer(serializers.Serializer):
    """`POST …/platform-push/off-sell/` 的入参：`flag` 1 上架 / 0 下架。"""

    flag = serializers.ChoiceField(
        choices=OFF_SELL_CHOICES,
        error_messages={
            "required": "请选择上架还是下架",
            "invalid_choice": "上下架方向只能是 1（上架）或 0（下架）",
        },
    )


class LockSerializer(serializers.Serializer):
    """`POST …/platform-push/lock/` 的入参：`locked` 1/0 + 可选原因。"""

    locked = serializers.ChoiceField(
        choices=LOCK_CHOICES,
        error_messages={
            "required": "请选择锁定还是解锁",
            "invalid_choice": "锁定方向只能是 1（锁定）或 0（解锁）",
        },
    )
    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=LOCK_REASON_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"锁定原因不能超过 {LOCK_REASON_MAX_LENGTH} 个字符",
            "invalid": "锁定原因格式不对",
        },
    )


class BlacklistSerializer(serializers.Serializer):
    """`POST …/platform-push/blacklist/` 的入参：`flag` 0 拉黑 / 1 解除。"""

    flag = serializers.ChoiceField(
        choices=BLACKLIST_CHOICES,
        required=False,
        default=0,
        error_messages={
            "invalid_choice": "拉黑方向只能是 0（拉黑）或 1（解除拉黑）",
        },
    )


class StatsQuerySerializer(serializers.Serializer):
    """`GET platform-integrations/stats/` 的查询参数。`start` / `end`：`YYYY-MM-DD`，都不传 ⇒ 最近 7 天（含今天），只传一个 ⇒ 另一个按默认窗口补齐（`start` 缺 = `end - 6 天`；`end` 缺 = 今天）；`is_pub`：1 发单（默认）/ 0 接单；`page_index` / `page_size`：上游分页（默认 1 / 50）。⚠️ 全部过序列化器（E-0010）：`?start=abc` 不许 500。
    """

    start = serializers.DateField(
        required=False,
        error_messages={"invalid": "开始日期格式不对（要 YYYY-MM-DD）"},
    )
    end = serializers.DateField(
        required=False,
        error_messages={"invalid": "结束日期格式不对（要 YYYY-MM-DD）"},
    )
    is_pub = serializers.ChoiceField(
        choices=[(STATS_SIDE_PUBLISH, "发单"), (STATS_SIDE_ACCEPT, "接单")],
        required=False,
        default=STATS_SIDE_PUBLISH,
        error_messages={"invalid_choice": "统计方向只能是 1（发单）或 0（接单）"},
    )
    page_index = serializers.IntegerField(
        required=False,
        min_value=1,
        default=1,
        error_messages={"invalid": "页码必须是正整数"},
    )
    page_size = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=200,
        default=50,
        error_messages={
            "invalid": "每页条数必须是正整数",
            "max_value": "每页条数不能超过 200",
        },
    )

    def validate(self, attrs):
        start = attrs.get("start")
        end = attrs.get("end")
        if start is None and end is None:
            start, end = default_stats_window()
        elif start is None:
            start = end - timedelta(days=STATS_DEFAULT_DAYS - 1)
        elif end is None:
            today = default_stats_window()[1]
            end = max(today, start)
        if start > end:
            raise ValidationError("开始日期不能晚于结束日期")
        attrs["start"] = start
        attrs["end"] = end
        return attrs


class OrderTemplateCreateSerializer(serializers.Serializer):
    """`POST platform-integrations/order-templates/` 的入参。"""

    title = serializers.CharField(
        allow_blank=False,
        max_length=ORDER_TEMPLATE_TITLE_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "required": "请填写模板标题",
            "blank": "模板标题不能为空",
            "max_length": f"模板标题不能超过 {ORDER_TEMPLATE_TITLE_MAX_LENGTH} 个字符",
        },
    )
    temp = serializers.CharField(
        allow_blank=False,
        max_length=ORDER_TEMPLATE_CONTENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "required": "请填写模板内容",
            "blank": "模板内容不能为空",
            "max_length": f"模板内容不能超过 {ORDER_TEMPLATE_CONTENT_MAX_LENGTH} 个字符",
        },
    )
    is_default = serializers.BooleanField(
        required=False,
        default=False,
        error_messages={"invalid": "「是否默认」必须是布尔值"},
    )


class OrderTemplateUpdateSerializer(serializers.Serializer):
    """`PATCH …/order-templates/<id>/` 的入参；至少给一项。"""

    title = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=ORDER_TEMPLATE_TITLE_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "blank": "模板标题不能为空",
            "max_length": f"模板标题不能超过 {ORDER_TEMPLATE_TITLE_MAX_LENGTH} 个字符",
        },
    )
    temp = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=ORDER_TEMPLATE_CONTENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "blank": "模板内容不能为空",
            "max_length": f"模板内容不能超过 {ORDER_TEMPLATE_CONTENT_MAX_LENGTH} 个字符",
        },
    )
    is_default = serializers.BooleanField(
        required=False,
        error_messages={"invalid": "「是否默认」必须是布尔值"},
    )

    def validate(self, attrs):
        if not attrs:
            raise ValidationError("没有要修改的内容（标题 / 内容 / 是否默认至少给一个）")
        return attrs


# ── 订单动作 ───────────────────────────────────────────────────────────


class DailiantongUpPriceView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/up-price/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = UpPriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            push = up_price_push(
                order,
                operator=request.user,
                up_price=serializer.validated_data["up_price"],
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="加价已完成"
        )


class DailiantongArbitrationView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/arbitration/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        try:
            push = request_push_arbitration(order, operator=request.user)
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="已提交客服介入申请"
        )


class DailiantongAgreeCancelView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/agree-cancel/`：同意**打手发起**的协商撤销（上游 `LevelOrderCancel(Flag=2)`，要支付密码 + 两个金额）。前置：推送行 `remote_cancel_status == 11`（服务层还会当次回读复核）；成败：写后回读 `CancelStatus ∈ {12,15,16}` 才算成功（上游回 `Result=1` 不算）；⛔ 不提供「拒绝协商」：上游没有拒绝档，口径见 `docs/DAILIANTONG-API.md` §6.13。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        try:
            push = agree_cancel(order, operator=request.user)
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="已同意撤销"
        )


class DailiantongCancelWithdrawView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/cancel-request/`：取消**我方发起**的协商撤销（上游 `LevelOrderCancel(Flag=1)`，**无金额 / 无支付密码**）。打手发起的撤销 ⇒ 中文提示改用「同意撤销」/ 满 1 小时后申请客服介入（⛔ 不代取消）。成败 = 写后回读 `CancelStatus ∈ {10,13}`；成功清 `cancel_requested_at` 与自动仲裁待办。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        try:
            push = cancel_withdraw_request(order, operator=request.user)
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="已取消撤销"
        )


class DailiantongValueView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/value/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = ValueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            push = value_push_order(
                order,
                operator=request.user,
                eval_type=serializer.validated_data["eval_type"],
                comment=serializer.validated_data.get("comment") or None,
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="评价已提交"
        )


class DailiantongOffSellView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/off-sell/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = OffSellSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        flag = serializer.validated_data["flag"]
        try:
            push = off_sell_push_order(
                order, operator=request.user, flag=flag
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data,
            msg="上架已完成" if flag == 1 else "下架已完成",
        )


class DailiantongLockView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/lock/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = LockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        locked = serializer.validated_data["locked"]
        try:
            push = lock_push_order(
                order,
                operator=request.user,
                locked=locked,
                reason=serializer.validated_data.get("reason") or None,
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data,
            msg="已锁定" if locked == 1 else "已解锁",
        )


class DailiantongBlacklistView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/blacklist/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = BlacklistSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        flag = serializer.validated_data["flag"]
        try:
            push = blacklist_taker(
                order, operator=request.user, flag=flag
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data,
            msg="已拉黑该打手" if flag == 0 else "已解除拉黑",
        )


# ── 按日统计（只读）─────────────────────────────────────────────────────


class DailiantongStatsView(APIView):
    """`GET /api/business/platform-integrations/stats/?start=&end=&is_pub=`。"""

    permission_classes = [has_node(EXTRA_ACTION_NODE)]

    def get(self, request):
        params = StatsQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        try:
            stats = platform_order_stats(
                start=data["start"],
                end=data["end"],
                is_pub=data["is_pub"],
                page_index=data["page_index"],
                page_size=data["page_size"],
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=stats)


# ── 代练通侧订单要求模板（⚠️ 不是本仓的「代练要求模板」）──────────────


class DailiantongOrderTemplateListView(APIView):
    """`GET` / `POST /api/business/platform-integrations/order-templates/`。⭐ M5：同一个类两种语义 ⇒ **按方法分派节点**——`GET` 是读（沿用 `order.list`），`POST` 是在上游账户下建模板（账户级写）⇒ 要 `order.platform_write`。⛔ 不要为了省事整类挂写节点（那会把模板下拉变成「没写权限就看不了」），也⛔ 不要整类挂读节点（写动作会绕过涉钱门）。
    """

    def get_permissions(self):
        node = (
            PLATFORM_WRITE_NODE
            if self.request.method == "POST"
            else EXTRA_ACTION_NODE
        )
        return [has_node(node)()]

    def get(self, request):
        try:
            data = list_order_templates()
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=data)

    def post(self, request):
        serializer = OrderTemplateCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            template = create_order_template(
                operator=request.user,
                title=data["title"],
                temp=data["temp"],
                is_default=data["is_default"],
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=template, msg="模板已创建")


class DailiantongOrderTemplateDetailView(APIView):
    """`PATCH` / `DELETE /api/business/platform-integrations/order-templates/<id>/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def patch(self, request, temp_id):
        serializer = OrderTemplateUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            template = update_order_template(
                operator=request.user,
                temp_id=temp_id,
                title=data.get("title"),
                temp=data.get("temp"),
                is_default=data.get("is_default"),
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=template, msg="模板已更新")

    def delete(self, request, temp_id):
        try:
            result = delete_order_template(operator=request.user, temp_id=temp_id)
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=result, msg="模板已删除")


class DailiantongOrderTemplateDefaultView(APIView):
    """`POST /api/business/platform-integrations/order-templates/<id>/default/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, temp_id):
        try:
            template = set_default_order_template(
                operator=request.user, temp_id=temp_id
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=template, msg="已设为默认模板")


# ── 详情页三个补充动作──────────────────────────────────────


class AmountSerializer(serializers.Serializer):
    """`POST …/platform-push/amount/` 的入参：**补款金额**（整数元、≥ 1）。⭐ 真机：`LevelOrderModify_Special` 的 `Kind=0` 只收整数元——1.5 元回 `-5 补款金额不能含有小数！`、0 元回 `-3 补款金额不能低于1元.`；所以这里保留两位小数的输入形状（前端可能送 `"1.00"`），但**校验必须是整数值**，服务层再统一翻成 `Val="1"` 的字面量。
    """

    amount = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        error_messages={
            "required": "请填写补款金额",
            "invalid": "补款金额格式不对（最多两位小数）",
            "max_digits": "补款金额太大",
            "max_decimal_places": "补款金额最多两位小数",
        },
    )

    def validate_amount(self, value):
        if value != value.to_integral_value():
            raise ValidationError("补款金额必须是整数元（代练通不接受小数）")
        if value < Decimal("1"):
            raise ValidationError("补款金额不能低于 1 元")
        return value



class EditPushSerializer(serializers.Serializer):
    """`POST …/platform-push/edit/` 的入参：只收**非金额**字段（金额走改价）。

    键全部可选，但至少要有一项（服务层会挡「什么都没改」并给中文原因）。
    """

    title = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=TITLE_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"标题不能超过 {TITLE_MAX_LENGTH} 个字符",
        },
    )
    memo = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=MEMO_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={
            "max_length": f"订单说明不能超过 {MEMO_MAX_LENGTH} 个字符",
        },
    )
    requirement = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=REQUIRE_TEMPLATE_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={
            "max_length": f"代练要求不能超过 {REQUIRE_TEMPLATE_MAX_LENGTH} 个字符",
        },
    )
    zone_server_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=32,
        trim_whitespace=True,
        error_messages={"max_length": "代练通区服编码不合法（超过 32 个字符）"},
    )
    groups = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=GROUPS_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"发单频道不合法（超过 {GROUPS_MAX_LENGTH} 个字符）",
        },
    )
    label1 = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=LABEL_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={"max_length": f"标签1不能超过 {LABEL_MAX_LENGTH} 个字符"},
    )
    label2 = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=LABEL_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={"max_length": f"标签2不能超过 {LABEL_MAX_LENGTH} 个字符"},
    )

    def validate_title(self, value):
        text = (value or "").strip()
        if text and len(text) < TITLE_MIN_LENGTH:
            raise serializers.ValidationError(
                f"标题至少 {TITLE_MIN_LENGTH} 个字符"
            )
        return value


class ScreenshotSerializer(serializers.Serializer):
    """`POST …/platform-push/screenshot/` 的入参（`multipart/form-data`）。`image`（必填）：jpg / png，**按字节魔数判图种**（⛔ 不信 `Content-Type`），上限与订单截图一致（`apps.core.storage.MAX_IMAGE_BYTES`）；`caption`（可选）：进度正文，空 ⇒ 服务层默认「客服补充的进度截图」。
    """

    image = serializers.FileField(
        error_messages={"required": "请选择要上传的图片文件", "invalid": "图片文件不合法"}
    )
    caption = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=PUSH_SCREENSHOT_CAPTION_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"进度说明不能超过 {PUSH_SCREENSHOT_CAPTION_MAX_LENGTH} 个字符",
        },
    )

    def validate_image(self, value):
        if value.size > MAX_IMAGE_BYTES:
            raise serializers.ValidationError(
                f"图片不能超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB"
            )
        return value


class DailiantongAmountView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/amount/`：给代练通订单**补款**。服务层 = `dailiantong_extra_actions.add_push_amount`（`LevelOrderModify_Special` 的 `Kind=0`：写前过终态闸 + 余额闸，写后回读 `Price == 原价 + 补款额`）；与「加价」不同：补款用于已接手的单，会真的增加我方冻结额。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = AmountSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            push = add_push_amount(
                order, operator=request.user, amount=serializer.validated_data["amount"]
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="补款已完成"
        )


class DailiantongEditView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/edit/`：编辑平台订单（非金额字段）。

    服务层 = `dailiantong_extra_actions.edit_push_order`（写前只有「未接手」可改；
    写后回读 `Title` / `Require`；金额字段原样回传、⛔ 不在这个入口改钱）。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = EditPushSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            push = edit_push_order(
                order,
                operator=request.user,
                title=data.get("title"),
                memo=(data.get("memo") if "memo" in data else None),
                requirement=(
                    data.get("requirement") if "requirement" in data else None
                ),
                zone_server_id=data.get("zone_server_id"),
                groups=data.get("groups"),
                label1=data.get("label1"),
                label2=data.get("label2"),
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="编辑已提交"
        )


class DailiantongScreenshotView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/screenshot/`：给平台单**追加截图**。服务层 = `dailiantong_extra_actions.add_push_screenshot`：`GetSTS` → `put_object`（**租户前缀**的对象名）→ `progress_add` → **回读**进度列表看到这个对象名才算成功（上游这个动作会「假成功」，⛔ 不能只看 `Result`）。
    成功返回 `{img, caption, url}`：`img` = 对象名（排障用），`url` 当前是空串（我们没有该 OSS 桶的下载签名能力，⛔ 不编一个点不开的链接）。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = ScreenshotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        upload = serializer.validated_data["image"]
        data = upload.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise ValidationError({"image": ["图片太大"]})
        if sniff_image_bytes(data) is None:
            raise ValidationError({"image": ["只支持 jpg / png 图片"]})
        try:
            result = add_push_screenshot(
                order,
                operator=request.user,
                data=data,
                declared_content_type=getattr(upload, "content_type", "") or "",
                caption=serializer.validated_data.get("caption") or "",
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=result, msg="截图已追加")
