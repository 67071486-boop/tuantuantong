"""代练通抛单的接口：`POST /api/business/orders/<order_no>/push-to-platform/`（把一张单**真发**到代练通）、`GET …/platform-push/`（读这张单的全部推送链路行）、`GET …/platform-push/preview/`（发单页默认值预览 B8；与真发单同源，⛔ 不查余额）、`GET /api/business/platform-integrations/zones/?q=&game_id=`（代练通区服字典，本地 TTL 缓存；`game_id` = 按我方游戏取全部区服）、`GET …/zones/map/?game_id=&server_id=`（读「上次选过的区服」映射 T10a）、`GET …/zones/default/?game_id=`（读**默认区服**：映射优先 / 游戏名首匹配 / none，B5）、`GET …/summary/`（右面板账户摘要：余额实时 + 30s 缓存，B6）。
权限（⭐ M5 审计：写端点有专属节点）：**读**接口（推送链路列表 / 预览 / 区服 / 摘要 / 余额告警）沿用订单页既有节点 `order.list`；**写**接口（`POST …/push-to-platform/` = 真钱冻结 + 建平台单）换到 `order.platform_write`（交易平台涉钱 / 写动作）；节点 code 写成**模块常量字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的字符串常量，用变量拼出来的 code 不会被它看见。
失败分层（⛔ 不要 500）：**前置闸**（`DailiantongPushError`）⇒ HTTP 200 + `code=1002` + 中文 `msg`（文案带「下一步」：去配置 / 改状态 / 充值 / 人工核对）；**上游业务失败 / 超时** ⇒ 本端点**不**把它当接口失败：`POST` 返回 `code=0` 的推送行（`state=failed` / `unknown` + `last_error`），调用方据行判断；**余额检查本身就是上游调用**：它回业务失败 / 传输失败时同样是 `code=1002` 的中文 `msg`；入参形状不对 ⇒ 序列化器回 DRF 400 信封（`code=1000`），⛔ 不进服务层。凭据红线（AGENTS.md §4）：响应里**只有推送行**（单号 / 金额 / 状态 / `last_error`），⛔ 没有 `app_secret`、`pay_password_md5` / `pay_pass`；`last_error` 的来源见服务层（上游原文或本线中文）。
"""

from decimal import Decimal

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.dailiantong_actions import withdraw_defaults_for_push
from apps.business.dailiantong_client import DailiantongError
from apps.business.dailiantong_push import (
    GROUPS_MAX_LENGTH,
    LABEL_MAX_LENGTH,
    LIMIT_ACCEPT_MAX,
    MEMO_MAX_LENGTH,
    OFF_SELL_DAYS_MAX,
    PUSH_DEFAULT_TIME_LIMIT_HOURS,
    REQUIRE_TEMPLATE_MAX_LENGTH,
    DailiantongPushError,
    agree_cancel_availability,
    arbitration_availability,
    balance_alert,
    default_zone_for_game,
    display_status_for_remote,
    settled_amounts_for_push,
    list_zones,
    list_zones_for_game,
    platform_account_summary,
)
from apps.business.platform_adapters import get_adapter
from apps.business.game_scope import game_scope
from apps.business.models import (
    DailiantongPush,
    DailiantongZoneMap,
    GameServer,
    Order,
    PlatformPushSettings,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node

#: **读**接口共用的 RBAC 节点（订单管理列表；能看订单的人能看平台推送）。
ORDER_PUSH_NODE = "order.list"
#: ⭐ M5：**写**接口的专属节点（交易平台涉钱 / 写动作）。
#: 抛单 = 真钱冻结 + 建平台单 ⇒ 从 `order.list` 换到本点；读接口仍用 `ORDER_PUSH_NODE`。
PLATFORM_WRITE_NODE = "order.platform_write"

#: 本版唯一能真发单的平台（代练通）。入参白名单：其它平台 id 一律 400，
#: ⛔ 不要扩成 `PlatformIntegration.Platform.choices` 的 13 个（那 12 个没有客户端）。
SUPPORTED_PLATFORM = 10

#: 成功 / 失败 / 状态未知三种信封文案。⚠️ 上游失败也是 `code=0`：推送行本身就是结果。
_SUCCESS_MESSAGES = {
    DailiantongPush.State.SENT: "发单成功",
    DailiantongPush.State.FAILED: "发单失败，原因见推送行",
    DailiantongPush.State.UNKNOWN: "发单状态未知，请人工核对代练通后台",
}


class DailiantongPushSerializer(serializers.ModelSerializer):
    """推送链路行的对外形状（⛔ 不含任何凭据）。"""

    state_display = serializers.CharField(source="get_state_display", read_only=True)
    #: ⭐：没有上游单号时对外统一空串（与列表摘要 `platform_push.serial_no`
    #: 同形；否则详情 `null` / 列表 `""` 会「同一条行两套形状」）。
    serial_no = serializers.SerializerMethodField()
    #: ⭐ fx-D：来源单号 / 接单价 / 接单时间（详情页也会用这三格）。
    source_order_no = serializers.CharField(
        source="order.source_order_no", read_only=True
    )
    accept_price = serializers.DecimalField(
        source="order.total_price",
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )
    accepted_at = serializers.DateTimeField(
        source="remote_accepted_at", read_only=True
    )
    #: ⭐ 真机定案：申请客服介入的**可用性**（前端据此置灰 / 显示原因）。
    #: 判据 = `CancelStatus=11` 且转入满 1 小时；实现唯一来源
    #: `dailiantong_push.arbitration_availability`（⛔ 不在这里再写一份）。
    arbitration_available = serializers.SerializerMethodField()
    arbitration_blocked_reason = serializers.SerializerMethodField()
    #: 撤销协商发起方（`taker` / `us` / `""`）；判定唯一来源 `dailiantong_push.cancel_initiator_from_detail`（回读时落库）。
    #: `agree_cancel_available` = `CancelStatus==11` **且**发起方是打手（唯一来源 `dailiantong_push.agree_cancel_availability`）——前端据此决定「同意撤销」按钮；⛔ 上游**没有**「拒绝协商」这一档，所以**不给**拒绝按钮的数据。
    cancel_initiator = serializers.CharField(read_only=True)
    agree_cancel_available = serializers.SerializerMethodField()
    #: ⭐ 真机（业务方）：撤销要求原文（`CancelCmt`）/ 撤销原因（`CancelReason`）。
    #: 客服点「同意撤销」时要看到上游拼好的整段要求（含支出金额），⛔ 不改写。
    #: `cancel_cmt` 在详情无 `CancelCmt` 键时退回 `CancelReason`（落库时已处理）。
    cancel_cmt = serializers.CharField(source="remote_cancel_cmt", read_only=True)
    cancel_reason = serializers.CharField(source="remote_cancel_reason", read_only=True)
    #: ⭐（业务方）：详情页「已结代练费 / 已赔保证金」。上游没有独立「已结」
    #: 字段 ⇒ 取详情 `PayLevelBal` / `RepEnsureBal` 原值，**只在 Status 16/17 下发**，
    #: 其它状态 / 解析不出 ⇒ `null`（前端显示「—」，⛔ 不写 0 冒充）。
    settled_pay_level_bal = serializers.SerializerMethodField()
    settled_rep_ensure_bal = serializers.SerializerMethodField()
    #: ⭐ h1（真机 bug）：详情「代练通」页签的**综合状态**（`Status` +
    #: `CancelStatus` 一起判；实现唯一来源 `dailiantong_push.display_status_for_remote`）。
    #: 前端优先显示 `display_status_label`（⛔ 不许只看 `remote_status`）。
    display_status = serializers.SerializerMethodField()
    display_status_label = serializers.SerializerMethodField()

    class Meta:
        model = DailiantongPush
        fields = [
            "id",
            "order",
            "platform",
            "serial_no",
            "state",
            "state_display",
            # ⭐：列表摘要与详情页要能逐字对同一条；详情也下发 is_active。
            "is_active",
            "price",
            "ensure1",
            "ensure2",
            "time_limit_hours",
            "ex_order_no",
            "remote_status",
            "remote_cancel_status",
            "display_status",
            "display_status_label",
            # ⭐ dlt-d：接单打手 UID（拉黑打手要用；空串 = 还不知道）。
            "accept_uid",
            "last_synced_at",
            "last_error",
            "attempts",
            "created_at",
            "updated_at",
            # ⭐ fx-D：来源单号 / 接单价 / 接单时间。
            "source_order_no",
            "accept_price",
            "accepted_at",
            # ⭐ 仲裁可用性。
            "arbitration_available",
            "arbitration_blocked_reason",
            # 撤销协商发起方 + 「同意撤销」可用性。
            "cancel_initiator",
            "agree_cancel_available",
            # ⭐ 真机：撤销要求原文 / 撤销原因（客服同意前要看的内容）。
            "cancel_cmt",
            "cancel_reason",
            # ⭐：详情页「已结代练费 / 已赔保证金」（只在 Status 16/17 有值）。
            "settled_pay_level_bal",
            "settled_rep_ensure_bal",
        ]
        read_only_fields = fields

    def get_serial_no(self, obj) -> str:
        return obj.serial_no or ""

    def get_arbitration_available(self, obj) -> bool:
        return arbitration_availability(obj)[0]

    def get_arbitration_blocked_reason(self, obj) -> str:
        return arbitration_availability(obj)[1]

    def get_agree_cancel_available(self, obj) -> bool:
        return agree_cancel_availability(obj)

    def get_settled_pay_level_bal(self, obj) -> str | None:
        return settled_amounts_for_push(obj)[0]

    def get_settled_rep_ensure_bal(self, obj) -> str | None:
        return settled_amounts_for_push(obj)[1]

    def get_display_status(self, obj) -> str | None:
        return display_status_for_remote(
            obj.remote_status, obj.remote_cancel_status
        )[0]

    def get_display_status_label(self, obj) -> str:
        return display_status_for_remote(
            obj.remote_status, obj.remote_cancel_status
        )[1]



class PushToPlatformSerializer(serializers.Serializer):
    """`POST orders/<no>/push-to-platform/` 的入参。

    ⚠️ 所有长度 / 数值边界都在这里挡（⛔ 不在服务层靠数据库报错）：
    `zone_server_id` 列宽 32、`price` 两位小数、`time_limit_hours` 至少 1。
    """

    platform = serializers.ChoiceField(
        choices=[(SUPPORTED_PLATFORM, "代练通")],
        required=False,
        default=SUPPORTED_PLATFORM,
        error_messages={
            "invalid_choice": "目前只有代练通支持抛单",
            "required": "请选择平台",
        },
    )
    price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        error_messages={
            "required": "请填写抛单价格",
            "invalid": "抛单价格格式不对（最多两位小数）",
            "max_digits": "抛单价格太大",
            "max_decimal_places": "抛单价格最多两位小数",
        },
    )
    ensure1 = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal("0.00"),
    )
    ensure2 = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        default=Decimal("0.00"),
    )
    time_limit_hours = serializers.IntegerField(
        min_value=1,
        max_value=720,
        required=False,
        # ⭐ 一键抛单计划 §2.3：默认 30 天（720 小时）。与 preview 的
        # `PUSH_DEFAULT_TIME_LIMIT_HOURS` 同值（那一个是唯一出处，这里不能各写一份）。
        default=PUSH_DEFAULT_TIME_LIMIT_HOURS,
        error_messages={
            "min_value": "时限必须至少 1 小时",
            "max_value": "时限不能超过 720 小时",
            "invalid": "时限必须是整数小时",
        },
    )
    zone_server_id = serializers.CharField(
        max_length=32,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "required": "请选择代练通区服",
            "blank": "请选择代练通区服",
            "max_length": "代练通区服编码不合法（超过 32 个字符）",
        },
    )
    # ⭐（一键抛单计划 §2.4 / §4）：门槛两项为**可选**入参。
    # `allow_null=True`：显式传 `null` = 本单「不设置」（⛔ 与「没传 → 用全局默认」不同）。
    power_level = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        error_messages={
            "min_value": "要求最低完单数不能小于 0",
            "invalid": "要求最低完单数必须是整数",
        },
    )
    limit_accept = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=LIMIT_ACCEPT_MAX,
        error_messages={
            "min_value": "拒绝新手和介入率不能小于 0",
            "max_value": f"拒绝新手和介入率不能大于 {LIMIT_ACCEPT_MAX}（按百分比填写）",
            "invalid": "拒绝新手和介入率必须是整数（留空表示不设置）",
        },
    )
    # ⭐ I3：页面「代练/陪练要求」那一格。**空 ⇒ 后端用全局模板**
    # （计划 §2.4 的唯一模板）；超长按 `REQUIRE_TEMPLATE_MAX_LENGTH` 截断——
    # 这里先挡一道（⛔ 让数据库 / 上游去截）。
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
    # ⭐ I3：自动下架天数（上游 `OffSellDays`）。页面「0 = 不设置」那一档
    # ⇒ **前端不上送这个键**（`0` 不是上游的有效天数，默认 7 由上游自己兜）。
    off_sell_days = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        max_value=OFF_SELL_DAYS_MAX,
        error_messages={
            "min_value": "自动下架天数必须至少 1 天（不设置请不要提交这个字段）",
            "max_value": f"自动下架天数不能超过 {OFF_SELL_DAYS_MAX} 天",
            "invalid": "自动下架天数必须是整数",
        },
    )
    # ⭐ P8 / P9：页面「当前订单说明」——**传了就一字不改发给代练通**；
    # 没传才回落「订单内容 + 重要备注」的拼装。长度上限与上游列宽同一个出处。
    memo = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=MEMO_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={
            "max_length": f"发到平台的订单说明不能超过 {MEMO_MAX_LENGTH} 个字符",
        },
    )
    # ⭐：发单频道（`PUBLIC` / `GOODCHANNEL` / 内部分组 id）与两个标签，透传上游。
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
    # ⭐：**同发内部抢单价**（⚠️ 不是代练通参数）——抛单前补发内部大厅时用它。
    # 没传 ⇒ 大厅价 = 发单价（既有口径）。0 是合法金额（D-0015）⇒ `min_value=0`。
    hall_price_same = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0.00"),
        error_messages={
            "invalid": "同发内部抢单价格式不对（最多两位小数）",
            "min_value": "同发内部抢单价不能是负数",
        },
    )


def _visible_order(request, order_no):
    """按订单号取**本租户 + 本账号游戏可见范围**内的订单（⛔ 已软删的取不到）。"""
    return scope_queryset(
        request.user,
        Order.objects.filter(order_no=order_no),
        scope_filter=game_scope("game_id"),
    ).first()


def _not_found():
    return api_response(data=None, msg="订单不存在或不可见", code=ErrorCode.VALIDATION)


def _service_failure(exc: Exception):
    """服务层 / 上游失败 ⇒ HTTP 200 + 业务码（⛔ 不做成 500）。"""
    return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)


class DailiantongPushView(APIView):
    """`POST /api/business/orders/<order_no>/push-to-platform/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()

        serializer = PushToPlatformSerializer(data=request.data)
        # 入参形状不对 ⇒ 400 信封（DRF 的统一异常处理会包成 `{code,msg,data}`）。
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # ⭐ 门槛默认值：**没传** ⇒ 用全局设置（页面默认带出；服务端再兜一层，保证
        # 「保存为全局默认且真生效」不依赖前端每次记得带）；显式传 `null` = 本单不设置。
        settings_row = PlatformPushSettings.get_solo()
        power_level = (
            data["power_level"]
            if "power_level" in data
            else settings_row.default_power_level
        )
        limit_accept = (
            data["limit_accept"]
            if "limit_accept" in data
            else settings_row.default_limit_accept
        )

        try:
            # 多平台骨架：抛单按**入参平台**取适配器（本版只有代练通；未接入平台 ⇒ 中文错误、
            # 零上游请求）。代练通适配器是纯转调，行为与改前逐字一致。
            adapter = get_adapter(data["platform"])
            push = adapter.push_order(
                order,
                price=data["price"],
                ensure1=data["ensure1"],
                ensure2=data["ensure2"],
                time_limit_hours=data["time_limit_hours"],
                zone_server_id=data["zone_server_id"],
                operator=request.user,
                power_level=power_level,
                limit_accept=limit_accept,
                # ⭐ I3：页面填的两格也真发出去（空 / 不传 ⇒ 用全局默认）。
                requirement=data.get("requirement"),
                off_sell_days=data.get("off_sell_days"),
                # ⭐ P8/P9：页面「当前订单说明」原样上送（没传才回落拼装值）。
                memo=data.get("memo"),
                # ⭐：频道 / 标签透传 + 同发内部抢单价。
                groups=data.get("groups"),
                label1=data.get("label1"),
                label2=data.get("label2"),
                hall_price_same=data.get("hall_price_same"),
            )
        except DailiantongPushError as exc:
            # 前置闸：中文业务错误（没调写上游）。
            return _service_failure(exc)
        except DailiantongError as exc:
            # 余额检查（唯一会打上游的前置闸）失败：`DailiantongTransportError` 是它的子类。
            return _service_failure(exc)

        # ⭐ A3（计划 Rev.9 §3）：大厅补发那一句是**内存属性**（不落库）——
        # 它属于「这一次请求的结果说明」，与推送行本身的状态是两件事
        # （大厅没发成⛔ 不改推送行、也⛔ 不判整体失败）。
        message = _SUCCESS_MESSAGES.get(push.state, "")
        hall_note = getattr(push, "hall_note", "")
        if hall_note:
            message = f"{message}；{hall_note}" if message else hall_note

        return api_response(
            data=DailiantongPushSerializer(push).data,
            msg=message,
        )


class DailiantongPushPreviewQuerySerializer(serializers.Serializer):
    """`GET orders/<no>/platform-push/preview/` 的查询参数。⚠️ `zone_server_id` 走序列化器（E-0010）：超长 / 类型不对的入参必须变成 200 + 业务码，⛔ 不许进服务层拼字符串；空串 = 没选（按默认区服走）。
    ⭐ 多平台骨架：`platform` 是**可选**入参（默认代练通 10）。预览发生在抛单之前、还没有推送行可看，只能按入参分派：未接入平台 ⇒ `code=1002` + 中文「该交易平台尚未接入…」，⛔ 零上游请求。
    """

    platform = serializers.IntegerField(
        required=False, default=SUPPORTED_PLATFORM, min_value=1
    )
    zone_server_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=32,
        trim_whitespace=True,
        error_messages={"max_length": "代练通区服编码不合法（超过 32 个字符）"},
    )
    # ⭐ P8/P9：页面里正在编辑的「当前订单说明」——传了就用它回显（一字不改），
    # 没传才回落到拼装值。这样「客服改完再点预览」看到的就是真会发出去的那份。
    memo = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MEMO_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={
            "max_length": f"发到平台的订单说明不能超过 {MEMO_MAX_LENGTH} 个字符",
        },
    )


class DailiantongPushPreviewView(APIView):
    """`GET /api/business/orders/<order_no>/platform-push/preview/?zone_server_id=`。发单页的**默认值唯一来源**：页面打开时把这里回的值填进表单，客服只改价格即可提交；返回字段与「与真发单同源」的判据见 `dailiantong_push.preview_push` 的 docstring。
    订单不可见 ⇒ `code=1000` 中文（与同族读端点一致）；入参畸形 ⇒ 200 + `code=1000`（⛔ 不 500）；**不查余额**（B6 的 `summary` 负责，30 秒缓存），`can_push=false` 时 `reason` 说清为什么；只读：不写库、不发单、不进审计。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        params = DailiantongPushPreviewQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        try:
            # 多平台骨架：按入参平台取适配器（未接入平台 ⇒ 中文错误、零上游请求）。
            # ⭐ dl-A：`memo` 用 `"memo" in ...` 区分「没传」与「传了空串」——前者才回落拼装。
            adapter = get_adapter(params.validated_data["platform"])
            data = adapter.preview_push(
                order,
                zone_server_id=params.validated_data.get("zone_server_id", ""),
                memo=(
                    params.validated_data.get("memo")
                    if "memo" in params.validated_data
                    else None
                ),
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(data=data)


class DailiantongPushListView(APIView):
    """`GET /api/business/orders/<order_no>/platform-push/`：该单的**全部**推送行。⭐（前端契约，申请撤销弹窗）：**每个平台最新一条有效推送行**额外下发 `pay_level_bal` / `rep_ensure_bal` / `withdraw_defaults_error` 三个键（服务层 = `dailiantong_actions.withdraw_defaults_for_push`，详情现值 + 30 秒缓存）——前端用它回填撤销弹窗的两个金额；读不到时 `error` 是中文原因、两格 `null`（页面留空要求手填，⛔ 不猜）。
    ⚠️ 只对**最新一条有效推送**去上游回读一次（历史行 / 已结束行不额外打上游）；上游失败只影响这三个键，⛔ 不让整个读面 500。列表顺序**显式** `-created_at, -id`（最新在前，⛔ 不依赖模型默认序）：这与订单列表 `platform_push` 摘要、第三方页聚合统一取**最新一笔**。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        pushes = list(
            DailiantongPush.objects.filter(order=order).order_by("-created_at", "-id")
        )
        rows = list(DailiantongPushSerializer(pushes, many=True).data)
        # 每个平台只给**最新一条有效推送**填默认金额（历史 / 结束行给空 + 说明）。
        latest_active: dict[int, int] = {}
        for push in pushes:
            if push.state in DailiantongPush.ACTIVE_STATES:
                latest_active.setdefault(int(push.platform), push.pk)
        for row, push in zip(rows, pushes):
            if latest_active.get(int(push.platform)) == push.pk:
                row.update(withdraw_defaults_for_push(push))
            else:
                row.update(
                    {
                        "pay_level_bal": None,
                        "rep_ensure_bal": None,
                        "withdraw_defaults_error": (
                            "这条不是该平台最新的有效推送，申请撤销的两个金额只能手填。"
                        ),
                    }
                )
        return api_response(data=rows)


class DailiantongZonesQuerySerializer(serializers.Serializer):
    """`GET platform-integrations/zones/` 的查询参数。`q`：关键字（匹配 `ServerName` 或区服编码；空 = 全部）；`game_id`：**按我方游戏**取全部区服（发单页「更换区服」下拉用；见 `list_zones_for_game`）。⚠️ 两个都走序列化器（E-0010）：`?game_id=abc` 不许 500。
    """

    q = serializers.CharField(required=False, allow_blank=True, trim_whitespace=False)
    game_id = serializers.IntegerField(required=False, min_value=1)


class DailiantongZonesView(APIView):
    """`GET /api/business/platform-integrations/zones/?q=&game_id=`：代练通区服字典。代理上游 `gamezoneserver`（4327 条，无分页参数）+ 模块级 10 分钟缓存。只给 `q` ⇒ 关键字过滤整个字典（既有行为）；给 `game_id` ⇒ **该游戏名在字典里的全部区服**（字典原序），供发单页下拉选（⚠️ 前端契约：**给了 `game_id` 时 `q` 被忽略**，不在子集里再过滤）。
    游戏不存在 / 匹配不到 ⇒ `code=0` + 空数组（⛔ 不是错误）；字典本身拉不到 ⇒ `code=1002` + 中文原因。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request):
        params = DailiantongZonesQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        query = params.validated_data.get("q", "")
        game_id = params.validated_data.get("game_id")
        try:
            if game_id is not None:
                # ⚠️ 前端契约：给了 `game_id` 时 `q` 忽略（下拉自己在前端过滤）。
                zones = list_zones_for_game(game_id)
            else:
                zones = list_zones(query)
        except DailiantongPushError as exc:
            return _service_failure(exc)
        except DailiantongError as exc:
            return _service_failure(exc)
        return api_response(data=zones)


class DailiantongZoneMapQuerySerializer(serializers.Serializer):
    """`GET platform-integrations/zones/map/` 的查询参数。

    ⚠️ 两个 id 都走序列化器（E-0010）：`?game_id=abc` 直接进 `.filter()` 会抛
    `ValueError` → HTML 500，而本仓判据是「这一类请求一律 200 + 业务码」。
    """

    game_id = serializers.IntegerField(min_value=1)
    server_id = serializers.IntegerField(min_value=1)


class DailiantongZoneMapView(APIView):
    """`GET /api/business/platform-integrations/zones/map/?game_id=&server_id=`。发单页「上次选过的区服自动带出」的读口：回我们**记住的**代练通区服编码（`DailiantongZoneMap`，由发单成功时 `_remember_zone` 写入）＋ 我方区服名。命中：`{"zone_server_id": "…", "server_name": "安卓QQ区"}`；没记过（或该区服不属于这个游戏）：`zone_server_id` 为 `null`、`server_name` 仍是我方区服名（查不到那个区服才两格都 `null`）——页面据此判断「要不要自动带出」。
    ⚠️ 只读本租户库的表（AGENTS.md §4）：`DailiantongZoneMap` 落在租户 schema，`queryset` 不接受任何 tenant 参数。权限沿用订单页节点 `order.list`。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request):
        params = DailiantongZoneMapQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        game_id = params.validated_data["game_id"]
        server_id = params.validated_data["server_id"]

        # 区服必须**属于**这个游戏：`(game, server)` 是本表唯一键，跨游戏拼出来的组合
        # 不该被当成一条有效记忆。
        server = GameServer.objects.filter(pk=server_id, game_id=game_id).first()
        mapping = DailiantongZoneMap.objects.filter(
            game_id=game_id, server_id=server_id
        ).first()
        return api_response(
            data={
                "zone_server_id": mapping.zone_server_id if mapping is not None else None,
                "server_name": server.name if server is not None else None,
            }
        )


class DailiantongDefaultZoneQuerySerializer(serializers.Serializer):
    """`GET platform-integrations/zones/default/` 的查询参数（`?game_id=`）。

    ⚠️ `game_id` 走序列化器（E-0010）：`?game_id=abc` 直接进 `.filter()` 会抛
    `ValueError` → 500；本仓判据是「这一类请求一律 200 + 业务码」。
    """

    game_id = serializers.IntegerField(min_value=1)


class DailiantongDefaultZoneView(APIView):
    """`GET /api/business/platform-integrations/zones/default/?game_id=`（计划 §4 / B5）。发单页的**默认区服**：`{zone_server_id, server_name, source}`，`source ∈ {"map", "first", "none"}`（取值顺序与「为什么取第一条」见 `dailiantong_push.default_zone_for_game` 的 docstring）。
    ⚠️ 匹配不到 = `source="none"`（HTTP 200 + 业务码 0）——那是**业务结果**、⛔ 不是错误：页面据此显示「请手动选择区服」。⚠️ 字典拉不到（上游不可用）⇒ `code=1002` + 中文原因（与「匹配不到」不是一回事）。⚠️ 权限沿用订单页节点 `order.list`（已裁定：能看订单的人能发单）。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request):
        params = DailiantongDefaultZoneQuerySerializer(data=request.query_params)
        params.is_valid(raise_exception=True)
        try:
            data = default_zone_for_game(params.validated_data["game_id"])
        except DailiantongPushError as exc:
            return _service_failure(exc)
        except DailiantongError as exc:
            return _service_failure(exc)
        return api_response(data=data)


class DailiantongBalanceAlertView(APIView):
    """`GET /api/business/platform-integrations/balance-alert/`（运营配套）。**余额不足预警**：可用余额 < 未来 24h 待发单所需 ⇒ `is_low=true` + 中文 `message`。口径（本函数的选择，见 `dailiantong_push.balance_alert` 的 docstring）：「待发单所需」= 当前 `未分配 / 待抢单`、且**还没有有效推送**的订单，按各自发单价逐张加总（没定价的单单独计数、不计入金额）。
    **只读**：⛔ 不写库、不发单、不进审计；唯一上游调用是 `UserMoney`（30 秒缓存）。无配置 / 上游失败 ⇒ `error` 中文、金额 `null`（⛔ 不说「余额充足」）；不 500（服务层把可预期失败都收进 `error`）。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request):
        return api_response(data=balance_alert())


class DailiantongAccountSummaryView(APIView):
    """`GET /api/business/platform-integrations/summary/`（计划 §4 / B6）。发单页右面板：平台 / 平台账号 / 可用余额 / 冻结余额（+ 中文原因）。余额走 `UserMoney` 实时查、**30 秒模块级缓存**；无配置 / 查询失败都给**中文原因**（字段口径见 `dailiantong_push.platform_account_summary`）。
    ⚠️ 这个端点**不会 500**：服务层把「无配置 / 上游业务失败 / 传输失败 / 金额解析不了」全部翻成 `error` 文案（计划 §4 的验收判据：「无配置与失败给中文说明」）。⚠️ 权限沿用订单页节点 `order.list`；凭据不进响应（只有账号标识与金额）。
    """

    permission_classes = [has_node(ORDER_PUSH_NODE)]

    def get(self, request):
        return api_response(data=platform_account_summary())
