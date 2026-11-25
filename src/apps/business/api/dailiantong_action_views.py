"""代练通**平台动作**的四个接口：`POST …/platform-push/withdraw/`（撤回：未接手删除 / 已接手协商撤销）、`POST …/platform-push/price/`（改代练通价格，只有未接手可改）、`POST …/platform-push/time/`（补时，增量 1–720 小时）、`POST …/platform-push/message/`（发留言，≤512 字）。服务层在 `apps/business/dailiantong_actions.py`（前置闸 / 上游调用 / 回读 / 审计都在那里），本文件只做「取可见订单 → 校验入参形状 → 调服务 → 翻信封」。⭐ 多平台骨架：四条都先按**这张单最新一条有效推送行的 `platform`** 取适配器（`platform_adapters.adapter_for_order`）再调同名方法；代练通适配器是**纯转调**（行为与改前逐字一致），未接入平台 ⇒ 中文错误、零上游请求。
权限（⭐ M5 审计：写端点有专属节点）：**读**动作（留言列表 / 进度图）沿用订单页既有节点 `order.list`；**写**动作（撤回 / 改价 / 补时 / 留言 / 全部撤回 / 确认完单 / 重发）换到 `order.platform_write`（交易平台涉钱 / 写动作）——原先这批写端点全只挂 `order.list`，「能看订单」就能对平台单做真钱写动作，与本地退款 / 改接单价的专属节点口径不一致。节点 code 写成**模块常量字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的字符串常量，用变量拼出来的 code 不会被它看见。
失败分层（⛔ 不要 500）：前置闸 / 上游业务失败 / 回读失败 / 结果未知（服务层统一抛 `DailiantongPushError`）⇒ HTTP 200 + `code=1002` + 中文 `msg`；入参形状不对 ⇒ 序列化器回 DRF 400 信封（统一处理器包成 HTTP 200 + `code=1000`），⛔ 不进服务层、⛔ 不碰上游；成功 ⇒ `code=0` + 推送行（与 `GET platform-push/` 的行同形状）。每个 `post` 都把服务调用写在**自己**的方法体里：`tests/test_operation_log_coverage.py` 按「HTTP 方法的入口方法体」跟 `record(...)`、只认 `record` / 登记助手 / `self.<DRF 入口名>` 这几条链，藏进非 DRF 命名的公共 `perform_*` 会让覆盖网**报不出**埋点 ⇒ 四个 `post` 各自直呼服务函数（重复的是 8 行信封代码，不是业务判据）。凭据红线（AGENTS.md §4）：响应里**只有推送行**，⛔ 没有 `app_secret`、⛔ 没有 `pay_password_md5` / `pay_pass`，上游详情里的明文 `GamePass` 也不会出现（服务层只取几个标量）。
"""

from decimal import Decimal

from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.api.dailiantong_push_views import (
    DailiantongPushSerializer,
    _not_found,
    _service_failure,
    _visible_order,
)
from apps.business.dailiantong_actions import (
    MESSAGE_MAX_LENGTH,
    TIME_MAX_HOURS,
    TIME_MIN_HOURS,
    WITHDRAW_COMMENT_MAX_LENGTH,
    accept_push,
    add_push_message,
    adjust_push_time,
    modify_push_price,
    read_push_messages,
    read_push_progress,
    repush_failed_push,
    withdraw_all_platforms,
    withdraw_push,
)
from apps.business.dailiantong_push import (
    DEFAULT_PLATFORM,
    GROUPS_MAX_LENGTH,
    LABEL_MAX_LENGTH,
    MEMO_MAX_LENGTH,
    REQUIRE_TEMPLATE_MAX_LENGTH,
    DailiantongPushError,
)
from apps.business.models import DailiantongPush
from apps.business.platform_adapters import adapter_for_order
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 四条接口共用的 RBAC 节点（订单管理列表；⛔ 不新增节点）。
PLATFORM_ACTION_NODE = "order.list"
#: ⭐ M5：写动作的专属节点（涉钱 / 写上游）。**读**动作
#: （留言列表 / 进度图）仍用 `PLATFORM_ACTION_NODE`；写动作全部换到本点。
PLATFORM_WRITE_NODE = "order.platform_write"


class WithdrawSerializer(serializers.Serializer):
    """`POST …/platform-push/withdraw/` 的入参（三格都可选，P7 起两个金额也能填）。`comment` = 撤销理由（空 ⇒ 服务层默认事由）；`pay_level_bal` / `rep_ensure_bal` = **支付代练费 / 赔保证金**（业务方口径：对标的撤销弹窗三格）——不传 ⇒ 用代练通详情里的现值（旧行为），传了就一字不改交给上游；金额 0 合法（D-0015）⇒ `min_value=0`；支付密码由客户端现算（⛔ 不收这个字段）。
    """

    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=WITHDRAW_COMMENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"撤回事由不能超过 {WITHDRAW_COMMENT_MAX_LENGTH} 个字符",
            "invalid": "撤回事由格式不对",
        },
    )
    pay_level_bal = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0.00"),
        error_messages={
            "invalid": "支付代练费格式不对（最多两位小数）",
            "min_value": "支付代练费不能是负数",
            "max_digits": "支付代练费太大",
        },
    )
    rep_ensure_bal = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0.00"),
        error_messages={
            "invalid": "赔偿保证金格式不对（最多两位小数）",
            "min_value": "赔偿保证金不能是负数",
            "max_digits": "赔偿保证金太大",
        },
    )
    # ⭐（前端契约）：**逐平台**撤回时带 `{platform}`。
    # 旧实现固定 10 ⇒ 多平台时「点哪个平台都去撤代练通」。
    platform = serializers.IntegerField(
        required=False,
        default=DEFAULT_PLATFORM,
        min_value=1,
        error_messages={
            "invalid": "平台 id 必须是整数",
            "min_value": "平台 id 不合法",
        },
    )
    # ⭐：协商撤销后**是否自动申请客服介入**（**默认不勾选**，业务方
    # 改准）。客服在撤销弹窗里主动勾选才代发；不传 = 业务默认 `False`。
    # 业务理由：不勾选时代练通侧 24 小时自己会转客服介入，我们再去申请就是重复申请。
    auto_arbitrate = serializers.BooleanField(
        required=False,
        default=False,
        error_messages={"invalid": "自动申请仲裁必须是真 / 假"},
    )


class PriceSerializer(serializers.Serializer):
    """`POST …/platform-push/price/` 的入参：`price` 必填、最多两位小数。"""

    price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        error_messages={
            "required": "请填写新的代练通价格",
            "invalid": "改价金额格式不对（最多两位小数）",
            "max_digits": "改价金额太大",
            "max_decimal_places": "改价金额最多两位小数",
        },
    )


class TimeSerializer(serializers.Serializer):
    """`POST …/platform-push/time/` 的入参：`hours` = **增量**（1–720）。"""

    hours = serializers.IntegerField(
        min_value=TIME_MIN_HOURS,
        max_value=TIME_MAX_HOURS,
        error_messages={
            "required": "请填写要补的小时数",
            "min_value": f"补时小时数必须至少 {TIME_MIN_HOURS} 小时",
            "max_value": f"补时小时数不能超过 {TIME_MAX_HOURS} 小时",
            "invalid": "补时小时数必须是整数",
        },
    )


class MessageSerializer(serializers.Serializer):
    """`POST …/platform-push/message/` 的入参：`msg` 非空、≤512。"""

    msg = serializers.CharField(
        allow_blank=False,
        max_length=MESSAGE_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "required": "请填写留言内容",
            "blank": "留言内容不能为空",
            "max_length": f"留言内容不能超过 {MESSAGE_MAX_LENGTH} 个字符",
        },
    )


class DailiantongWithdrawView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/withdraw/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = WithdrawSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # ⭐ 多平台骨架：按**推送行上的 platform** 取适配器（未接入平台 ⇒ 中文错误、零上游请求）。
            adapter = adapter_for_order(order)
            push = adapter.withdraw_push(
                order,
                operator=request.user,
                comment=serializer.validated_data.get("comment") or None,
                # ⭐ P7：页面填的两个金额（没填 ⇒ 服务层用详情现值）。
                pay_level_bal=serializer.validated_data.get("pay_level_bal"),
                rep_ensure_bal=serializer.validated_data.get("rep_ensure_bal"),
                # ⭐（前端契约）：逐平台撤回——⛔ 不再固定 10。
                platform=serializer.validated_data.get("platform", DEFAULT_PLATFORM),
                # ⭐：撤销弹窗的「自动申请仲裁」勾选（**默认 False**）。
                auto_arbitrate=serializer.validated_data.get("auto_arbitrate", False),
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        # ⭐ P2-10：不能一律说「撤回已完成」——未接手单是「删除并即时退款」，
        # 已接手单只是「提交协商撤销，等对方处理」。按服务层实际走到了哪一支回消息。
        if push.state == DailiantongPush.State.CLOSED:
            message = "撤回已完成：订单已删除并即时退款"
        else:
            message = "已提交协商撤销，等待打手 / 客服处理"
        return api_response(
            data=DailiantongPushSerializer(push).data, msg=message
        )


class DailiantongPriceView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/price/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = PriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # ⭐ 多平台骨架：按推送行上的 platform 分派（代练通 = 转调既有服务，行为逐字不变）。
            adapter = adapter_for_order(order)
            push = adapter.modify_push_price(
                order,
                operator=request.user,
                price=serializer.validated_data["price"],
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="改价已完成"
        )


class DailiantongTimeView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/time/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = TimeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # ⭐ 多平台骨架：按推送行上的 platform 分派（未接入平台 ⇒ 中文错误、零上游请求）。
            adapter = adapter_for_order(order)
            push = adapter.adjust_push_time(
                order,
                operator=request.user,
                hours=serializer.validated_data["hours"],
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="补时已完成"
        )


class DailiantongMessageView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/message/`。"""

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = MessageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            # ⭐ 多平台骨架：按推送行上的 platform 分派（未接入平台 ⇒ 中文错误、零上游请求）。
            adapter = adapter_for_order(order)
            push = adapter.add_push_message(
                order,
                operator=request.user,
                msg=serializer.validated_data["msg"],
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="留言已提交"
        )


# ── P3：从所有平台撤回（管理订单页行内按钮）───────────────────


class WithdrawAllSerializer(serializers.Serializer):
    """`POST …/platform-pushes/withdraw-all/` 的入参（三格都可选，与单平台撤回同口径）。"""

    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=WITHDRAW_COMMENT_MAX_LENGTH,
        trim_whitespace=True,
        error_messages={
            "max_length": f"撤回事由不能超过 {WITHDRAW_COMMENT_MAX_LENGTH} 个字符",
            "invalid": "撤回事由格式不对",
        },
    )
    pay_level_bal = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0.00"),
        error_messages={
            "invalid": "支付代练费格式不对（最多两位小数）",
            "min_value": "支付代练费不能是负数",
        },
    )
    rep_ensure_bal = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal("0.00"),
        error_messages={
            "invalid": "赔偿保证金格式不对（最多两位小数）",
            "min_value": "赔偿保证金不能是负数",
        },
    )


class DailiantongWithdrawAllView(APIView):
    """`POST /api/business/orders/<order_no>/platform-pushes/withdraw-all/`：一次性把这张单在**所有平台**的推送撤掉、**逐平台回结果**（载荷形如 `{"code": 0, "data": {"results": [{platform, platform_label, serial_no, ok, state, message}...], "succeeded", "failed", "total"}}`）。
    **没有推送** ⇒ `code=1002` + 「这张单没有抛到任何平台」（⛔ 不假装成功）；**部分成功** ⇒ `code=0`（`succeeded/failed` 如实计数、`msg` 里说清）；每个平台的分支与单平台撤回完全一致（未接手 ⇒ 删除退款；已接手 ⇒ 申请协商撤销）。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = WithdrawAllSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = withdraw_all_platforms(
                order,
                operator=request.user,
                comment=serializer.validated_data.get("comment") or None,
                pay_level_bal=serializer.validated_data.get("pay_level_bal"),
                rep_ensure_bal=serializer.validated_data.get("rep_ensure_bal"),
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)

        succeeded = result["succeeded"]
        failed = result["failed"]
        if failed == 0:
            message = f"已从 {succeeded} 个平台撤回"
        elif succeeded == 0:
            message = f"{failed} 个平台撤回失败，原因见逐平台结果"
        else:
            message = f"部分平台撤回成功（成功 {succeeded} / 失败 {failed}）"
        return api_response(data=result, msg=message)


# ── 确认完单（`PATCH`：补路由，C1 的服务层已有）────────────────────────────


class DailiantongFinishView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/finish/`：确认完单（`LevelOrderOver`）。服务层 = `dailiantong_actions.accept_push`（与第三方平台订单管理页的「批量验收」**同一个**实现：写前回读到 `Status=13`、写后回读到 `Status=17`、会扣平台余额）；本视图只做「取可见订单 → 调服务 → 翻信封」，⛔ 不走第二条判据。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        try:
            push = accept_push(order, operator=request.user)
        except DailiantongPushError as exc:
            return _service_failure(exc)
        return api_response(
            data=DailiantongPushSerializer(push).data, msg="确认完单已完成"
        )


# ── 只读：平台留言 / 进度图（详情页）────────────────────────────


class DailiantongMessagesView(APIView):
    """`GET /api/business/orders/<order_no>/platform-push/messages/`（只读）。返回 `{messages: [{time, text, is_system, sender, uid, nickname}], images: [{time, caption, img, url}], images_error, error}`：留言列表按上游原序；`is_system` = 上游 `MsgType=10` 或正文里带我们自己的系统标记（接手后发送 / 自动回复）；`sender` = 发布方原文（接单者 / 发单者 / 客服，取自 `OperationID`，缺失回退 `NickName` / `UID`）；`images` = 同一单的进度图（上游留言 API **没有**图片字段 ⇒ 取 `LevelOrderProgressList`）；`images_error` 非空 = 图片读失败（留言本身仍然返回）。
    ⛔ 只读：不写库、不发写上游；**失败给中文 `error`、不 500**。
    """

    permission_classes = [has_node(PLATFORM_ACTION_NODE)]

    def get(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        return api_response(data=read_push_messages(order))


class DailiantongProgressView(APIView):
    """`GET /api/business/orders/<order_no>/platform-push/progress/`（只读）。返回 `{progress: [{time, caption, img, url}], error}`；`img` 是上游原值（对象名或 URL），`url` 只在**已经是 http(s)** 时给（我们没有该 OSS 桶的下载签名）。⛔ 只读 + 失败中文、不 500（口径同留言）。
    """

    permission_classes = [has_node(PLATFORM_ACTION_NODE)]

    def get(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        return api_response(data=read_push_progress(order))


# ── P7-详情：重发失败 / 已关闭的推送────────────────────────


class RepushSerializer(serializers.Serializer):
    """`POST …/platform-push/repush/` 的入参：**全部可选**（缺省取上一条推送行的值）。

    页面通常什么都不用填（重发 = 原样再抛一次）；要改价 / 改时限 / 换区服时才带上对应键。
    """

    price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, error_messages={
            "invalid": "发单价格格式不对（最多两位小数）",
        },
    )
    ensure1 = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    ensure2 = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False
    )
    time_limit_hours = serializers.IntegerField(
        required=False, min_value=1, max_value=720,
        error_messages={"min_value": "时限必须至少 1 小时", "invalid": "时限必须是整数小时"},
    )
    zone_server_id = serializers.CharField(
        required=False, allow_blank=True, max_length=32, trim_whitespace=True,
        error_messages={"max_length": "代练通区服编码不合法（超过 32 个字符）"},
    )
    memo = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        max_length=MEMO_MAX_LENGTH, trim_whitespace=False,
        error_messages={
            "max_length": f"发到平台的订单说明不能超过 {MEMO_MAX_LENGTH} 个字符",
        },
    )
    groups = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=GROUPS_MAX_LENGTH,
    )
    label1 = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=LABEL_MAX_LENGTH,
    )
    label2 = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=LABEL_MAX_LENGTH,
    )
    requirement = serializers.CharField(
        required=False, allow_blank=True, allow_null=True,
        max_length=REQUIRE_TEMPLATE_MAX_LENGTH, trim_whitespace=False,
        error_messages={
            "max_length": f"代练要求不能超过 {REQUIRE_TEMPLATE_MAX_LENGTH} 个字符",
        },
    )
    hall_price_same = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
        min_value=Decimal("0.00"),
    )


class DailiantongRepushView(APIView):
    """`POST /api/business/orders/<order_no>/platform-push/repush/`：把一条**失败**（`failed`）或**已关闭**（`closed`）的推送重新抛一次；⛔ **不绕开五道闸**（服务层最后就是调 `push_order`），已有有效推送 ⇒ 中文拒绝。页面不填任何字段也可以（价格 / 保证金 / 时限取上一条推送行，区服取默认区服）。
    失败分层与 `push-to-platform/` 一致：前置闸 / 上游失败 ⇒ `code=1002` 中文；入参畸形 ⇒ 400 信封；成功 ⇒ `code=0` + **新的**推送行。
    """

    permission_classes = [has_node(PLATFORM_WRITE_NODE)]

    def post(self, request, order_no):
        order = _visible_order(request, order_no)
        if order is None:
            return _not_found()
        serializer = RepushSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            push = repush_failed_push(
                order,
                operator=request.user,
                price=data.get("price"),
                ensure1=data.get("ensure1"),
                ensure2=data.get("ensure2"),
                time_limit_hours=data.get("time_limit_hours"),
                zone_server_id=data.get("zone_server_id"),
                memo=(data.get("memo") if "memo" in data else None),
                groups=data.get("groups"),
                label1=data.get("label1"),
                label2=data.get("label2"),
                requirement=data.get("requirement"),
                hall_price_same=data.get("hall_price_same"),
            )
        except DailiantongPushError as exc:
            return _service_failure(exc)
        message = {
            DailiantongPush.State.SENT: "重发成功",
            DailiantongPush.State.FAILED: "重发失败，原因见推送行",
            DailiantongPush.State.UNKNOWN: "重发状态未知，请人工核对代练通后台",
        }.get(push.state, "已提交重发")
        return api_response(data=DailiantongPushSerializer(push).data, msg=message)
