"""第三方平台订单管理页的读面：`GET /api/business/platform-pushes/`（本租户的平台推送行，分页 + 筛选 + 关键字）与 `GET …/?group_by=order`（⭐ 同上但**按订单聚合**、分页按订单）。这个端点为什么存在：「第三方平台订单管理」页（对标 25 列）此前是空表，本模块只做**读**——页面**今天真有数据**的列才给键（单号 / 平台 / 金额 / 状态 / 时间 / 错误），接单价、利润、剩余时间、撤销时间这些**没有数据源**的列**不给键**（⛔ 不造 `0` / `null` 假值）。
权限（⛔ 不新增节点）：复用订单列表节点 `order.list`（「能看订单的人能看平台推送行」，与抛单端点同口径）；节点 code 是**模块常量字面量**（`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的字符串常量，变量拼出来的 code 不会被它看见）。租户边界：`queryset` 只跑在当前连接 / 租户 schema 上，**不接受任何 tenant 参数**（AGENTS.md §4）；`public` 平面上 `has_node` 恒假 ⇒ 回 403、不会走到查表那一步。⭐ 多平台骨架：本端点**只读本地推送行**（不调任何平台能力），「按平台分派」体现在**每一行**——`PlatformPushRowSerializer.to_representation` 按该行的 `platform` 取适配器，只有具备「接手后发送 / 自动回复」能力的平台才下发 `qr_*` / `auto_reply_last_error` 三列（未接入平台回空值，⛔ 键集不变、⛔ 不额外请求）。⚠️ 平台筛选（`?platform=`）本身**不过适配器**：未接入平台的行也是本租户的真实数据、照常列出（既有契约 `test_platform_filter_can_ask_for_another_platform`）；「未接入平台 ⇒ 中文错误」只发生在**动作 / 预览 / 抛单**这些真的会打上游的调用点。
入参形状（E-0010：请求来源的值先过序列化器）：`platform` 平台 id 默认代练通（10）；`status` 上游状态码（`remote_status`）可空 = 不筛；`keyword` 匹配**我方订单号或平台单号**（不区分大小写）；`page` / `page_size` 默认 30 / 上限 100（超出按 100 截断）；⭐ `group_by=order` 按订单聚合（不给 = 旧的扁平逐行口径）。坏形状（`?status=abc` 等）由序列化器翻成 `1000`（HTTP 200 + 业务码），⛔ 不做成 500（`tests/test_no_route_500.py` 的显式路由腿会拿畸形参数打本端点）。⭐ **按订单聚合**：同一订单可能有多条推送行（失败重发 / 撤销后重发），扁平分页会把同一张单的两行分到两页 ⇒ 前端无法正确聚合，分组模式下**分页单位是订单**、`total` = 订单数，载荷 = `{"items": [{"order_no", "source_order_no", "order_status", "game", "server", "pushes": [<扁平模式同一序列化器的一行>]}], "total", "page", "page_size"}`。判据（**逐条写死，避免两种口径漂移**）：组内 `pushes` = 该单在「当前平台」口径下的全部推送行（`platform` 仍生效），⛔ **不再按 `status` / `keyword` 收窄**——收窄会让「按状态筛出来的单」看不到自己其它的推送；`status` 该单**任一**推送行命中 ⇒ 整组返回（组内仍给全部行）；`keyword` 命中**订单号 / 平台单号 / 来源单号**任一 ⇒ 整组返回（来源单号这条**只在分组模式有**，扁平模式既有行为只搜订单号 + 平台单号，⛔ 不改它）；**组的排序键 = 该单最新一条推送的 `created_at`**（不受 `status` / `keyword` 影响）倒序、同刻按 `order_id` 倒序兜底（翻页稳定），组内 `pushes` 按 `-created_at, -id`（与扁平模式同一口径）；`group_by` 不给 / 空串 ⇒ 旧扁平分支、行为逐字不变。实现上只有 **3 条 SQL**（分组计数 / 当页分组 / 当页推送行），⛔ 不随订单数增长（无 N+1）。
"""


from django.db.models import Max, Q
from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.dailiantong_push import (
    agree_cancel_availability,
    arbitration_availability,
    display_status_for_remote,
    remaining_seconds_for_push,
    settled_amounts_for_push,
    verify_deadline_for_push,
)
from apps.business.models import DailiantongPush, PlatformIntegration
from apps.business.platform_adapters import adapter_or_none
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 本端点沿用的 RBAC 节点（订单管理列表；⛔ 本线不新增节点）。
ORDER_LIST_NODE = "order.list"

#: 默认平台：代练通（本版唯一有真实客户端 / 唯一有推送数据的平台）。
DEFAULT_PLATFORM = PlatformIntegration.Platform.DAILIANTONG

#: 分页默认值与上限（业务方 的口径：默认 30、上限 100）。
DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


def _push_rows_queryset(platform):
    """列表两个口径（扁平 / 按订单聚合）**共用**的底集：同一平台 + 订单未软删。⚠️ 两条口径必须同源：D-0144 的「已软删的单在常规读面上不存在」是从推送行起查时**显式**补的（跨关系 JOIN 不经过 `Order` 的默认 manager）⇒ 分成两份写必然漂移；`select_related` 也放在这里——一次 JOIN 取订单 / 游戏 / 区服，⛔ 不许 N+1。
    """
    return DailiantongPush.objects.select_related(
        "order", "order__game", "order__server", "order__order_source"
    ).filter(
        platform=platform,
        order__deleted_at__isnull=True,
    )


class PlatformPushQuerySerializer(serializers.Serializer):
    """`GET /api/business/platform-pushes/` 的查询参数。

    ⚠️ 所有值都在这里挡（⛔ 不在视图里直接 `int(...)`）：`?status=abc` / `?page=abc`
    会抛 `ValueError` → HTML 500，而本仓的判据是「这一类请求一律 200 + 业务码」。
    """

    platform = serializers.IntegerField(
        required=False, default=DEFAULT_PLATFORM, min_value=1
    )
    #: 上游 `Status` 码（`DailiantongPush.remote_status`）。可空 = 「还没回读过」或「不筛」。
    status = serializers.IntegerField(required=False)
    keyword = serializers.CharField(
        required=False, allow_blank=True, max_length=64, trim_whitespace=True
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    #: ⚠️ **不设 `max_value`**：超过上限按 100 截断（前端传大值不该收到一个错误）。
    page_size = serializers.IntegerField(
        required=False, default=DEFAULT_PAGE_SIZE, min_value=1
    )
    #: ⭐ 业务方：`group_by=order` ⇒ 按订单聚合、分页按订单。
    #: 不给 / 空串 = 旧的扁平逐行口径（⛔ 行为一字不变）。
    group_by = serializers.ChoiceField(
        choices=("order",),
        required=False,
        error_messages={
            "invalid_choice": "group_by 只支持 order（按订单聚合）；不传 = 逐行分页",
        },
    )


class PlatformPushRowSerializer(serializers.ModelSerializer):
    """页面一行（**只给今天真有数据源的列**，⛔ 不造假值键）。字段来源：推送行本身：`id` / `platform` / `serial_no` / `price` / `state` / `remote_status` / `remote_cancel_status` / `created_at` / `last_synced_at` / `last_error`；群二维码状态 `qr_sent_at` / `qr_last_error`——⚠️ 这两个键是**必须**的：`last_error` 每 30 秒会被状态回读清空，群二维码的失败原因只能靠 `qr_last_error`（否则页面上永远看不到「为什么没发出去」）；自动回复状态 `auto_reply_last_error` 理由同上（失败原因不被 `refresh_push_status()` 清空，才能排查「为什么没回」）。⭐ 远端接单人（计划 §2）：`remote_taker_uid` / `remote_taker_name` / `remote_accepted_at` / `remote_cancel_reason` / `remote_note`——前四格是**代练通侧的事实快照**（详情回读 / 回调留证），`remote_note` 是「本地状态按远端回写成什么 / 并发抢单怎么收口的」一句人话（客服要能直接看到「谁接的、什么时候接的、系统做了什么」）。⭐ 自动仲裁：`auto_arbitrate` / `cancel_requested_at` / `auto_arbitrated_at` / `auto_arbitrate_note`——协商撤销满 24 小时后是否自动申请了客服介入，失败 / 停止重试的原因在最后那一列（不被回读清空）。关联订单（`select_related("order", "order__game", "order__server")`，一次 JOIN、无 N+1）：`order_no` / `game` / `server` / `order_status`。⭐ 申请客服介入的可用性（真机定案）：`arbitration_available`（bool）/ `arbitration_blocked_reason`（中文原因，未满 1 小时含「还需 X 分钟」）——⚠️ 按**本地快照**（`remote_cancel_status` + `cancel_requested_at`）算、只作按钮提示，真正放不放行以动作端点的**当次详情回读**为准（唯一实现 `dailiantong_push.arbitration_availability`）。⭐ `is_active`（远端到终态后置 `False`，= 还值不值得轮询）：聚合行的父行要显示「**最新活跃推送**」的交易状态 ⇒ 前端靠它选行，没有它只能退化成「取最新一条」。⭐ `cancel_initiator`（`taker` / `us` / `""`）+ `agree_cancel_available`（bool）——「同意撤销」按钮，判据 = `remote_cancel_status==11` **且**发起方是打手，上游**没有拒绝档** ⇒ ⛔ 没有「拒绝协商」的键（唯一实现 `dailiantong_push.agree_cancel_availability`，只按本地快照，真放行仍以动作端点回读为准）。⭐ 真机（业务方同意撤销前要看）：`cancel_cmt`（上游拼好的撤销要求原文，含支出金额）/ `cancel_reason`，两者**原样展示、⛔ 不改写**；详情没有 `CancelCmt` 键 ⇒ `cancel_cmt` 退回 `CancelReason`。
    """

    #: 我方订单号（页面「订单号」列）。
    order_no = serializers.CharField(source="order.order_no", read_only=True)
    #: 游戏名（`Order.game` 是必填 FK ⇒ 恒有值）。
    game = serializers.SerializerMethodField()
    #: 区服名；订单没填区服 ⇒ `null`（⛔ 不拿空串冒充一个区服名）。
    server = serializers.SerializerMethodField()
    #: 我们**本地**订单状态（与 `state` = 推送状态是两件事）。
    order_status = serializers.CharField(source="order.status", read_only=True)
    #: ⭐ fx-D：来源单号（可能空串 ⇒ 前端回退内置单号）。
    source_order_no = serializers.CharField(
        source="order.source_order_no", read_only=True
    )
    #: ⭐ fx-D：接单价 = **本地订单的接单价** `Order.total_price`。对标 `GetPlatformOrderList` 的 `price` 一格（title「该笔订单的接单(销售)金额」）；平台详情 `LevelOrderDetail` 里**没有**单独的接单价键——`Price` 是平台订单价（= 我方发单价，已由 `price` 下发）。见 `docs/DAILIANTONG-API.md` §6.5。
    accept_price = serializers.DecimalField(
        source="order.total_price",
        max_digits=12,
        decimal_places=2,
        read_only=True,
    )
    #: ⭐（业务方）：A 组上游打手字段——UID / 显示名 / 完成单数 / 投诉取消次数。`accept_uid` 优先取详情 `AcceptUID` 的专列、历史行退回 `remote_taker_uid`；`accept_user_name` = 详情 `AcceptUserName`（落库 `remote_taker_name`）。
    #: 两格缺失 ⇒ 空串；两个计数缺失 / 解析不出 ⇒ `null`（⛔ 不写 0）。
    accept_uid = serializers.SerializerMethodField()
    accept_user_name = serializers.SerializerMethodField()
    accept_over_count = serializers.IntegerField(read_only=True)
    cust_cancel_count = serializers.IntegerField(read_only=True)
    #: ⭐（业务方）：本地计算字段——利润 = 接单价 - 推送价（缺一边 ⇒ null）。
    profit = serializers.SerializerMethodField()
    #: ⭐（业务方）：「剩余时间」列——`remote_accepted_at + time_limit_hours`
    #: 的剩余秒数；没接单时间 / 没时限 ⇒ `null`；已过期 ⇒ 0（不画负时间）。
    remaining_seconds = serializers.SerializerMethodField()
    #: ⭐（业务方）：本地订单来源展示名（`Order.source` 快照优先，空则用来源字典 `order_source.name`；都没有 ⇒ 空串）。
    order_source = serializers.SerializerMethodField()
    #: ⭐（业务方）：验收倒计时（本地算，不同步上游）。起点 = 远端**首次进入 13** 的时刻（`remote_verify_started_at`），终点 = 起点 + **48h**（业务方口径；本单/租户配置优先，但今天两者都没有 ⇒ 实际 48h）。
    #: 没有起点 ⇒ 三格都空；已过期 ⇒ 剩余秒数为**负数** + `verify_overdue=true`。
    verify_deadline_at = serializers.SerializerMethodField()
    verify_remaining_seconds = serializers.SerializerMethodField()
    verify_overdue = serializers.SerializerMethodField()
    #: ⭐ fx-D：平台打手接单时间（上游详情 `StartDate`，落库 `remote_accepted_at`）。
    accepted_at = serializers.DateTimeField(
        source="remote_accepted_at", read_only=True
    )
    #: ⭐ 真机定案：申请客服介入的可用性（见类 docstring 最后一段）。
    arbitration_available = serializers.SerializerMethodField()
    arbitration_blocked_reason = serializers.SerializerMethodField()
    #: 撤销协商发起方（`taker` / `us` / `""`）+「同意撤销」可用性：`agree_cancel_available` = `CancelStatus==11` **且**发起方是打手（唯一实现 `dailiantong_push.agree_cancel_availability`）。
    #: ⛔ 上游**没有**「拒绝协商」这一档 ⇒ 这里**不给**任何拒绝/驳回的键或按钮数据。
    cancel_initiator = serializers.CharField(read_only=True)
    agree_cancel_available = serializers.SerializerMethodField()
    #: ⭐ 真机（业务方）：撤销要求原文（`CancelCmt`）/ 撤销原因（`CancelReason`）。
    #: 客服点「同意撤销」前要看到上游拼好的整段要求（含支出金额）；⛔ 不改写。
    cancel_cmt = serializers.CharField(source="remote_cancel_cmt", read_only=True)
    cancel_reason = serializers.CharField(source="remote_cancel_reason", read_only=True)
    #: ⭐（业务方）：「已结代练费 / 已赔保证金」——详情 `PayLevelBal` /
    #: `RepEnsureBal` 原值；**只在 Status 16/17 下发**，其它状态 / 解析不出 ⇒ `null`。
    settled_pay_level_bal = serializers.SerializerMethodField()
    settled_rep_ensure_bal = serializers.SerializerMethodField()
    #: ⭐ h1（真机 bug）：「交易状态」列的**综合状态**——`Status` 与 `CancelStatus` 一起判（实现唯一来源 `dailiantong_push.display_status_for_remote`）。
    #: 12/15/16 ⇒ 已撤销 / 已强制撤销；11 ⇒ 撤销中；14 ⇒ 客服介入中；13 ⇒ 按 `Status`。前端**优先**用 `display_status_label`（⛔ 不许只看 `remote_status`）。
    display_status = serializers.SerializerMethodField()
    display_status_label = serializers.SerializerMethodField()

    class Meta:
        model = DailiantongPush
        fields = [
            "id",
            "platform",
            "serial_no",
            "order_no",
            "game",
            "server",
            "price",
            "state",
            "remote_status",
            "remote_cancel_status",
            "display_status",
            "display_status_label",
            "order_status",
            "created_at",
            "last_synced_at",
            "last_error",
            # ⭐ T12：群二维码发没发 / 为什么没发（页面 tooltip 靠这两格）。
            "qr_sent_at",
            "qr_last_error",
            # ⭐ T14：自动回复最近一次失败原因（成功 / 没配规则 / 还没到那一档时为空）。
            "auto_reply_last_error",
            # ⭐ A2（计划 §2）：远端接单人 / 接单时间 / 撤销原因
            # 与「最近一次同步结论」（本地状态按远端回写成什么 / 并发抢单怎么收口的）。
            # 客户在代练通接单后，客服要能在这里直接看到「谁接的、什么时候接的」。
            "remote_taker_uid",
            "remote_taker_name",
            "remote_accepted_at",
            "remote_cancel_reason",
            "remote_note",
            # ⭐ fx-D：来源单号 / 接单价 / 接单时间（页面三个格）。
            "source_order_no",
            "accept_price",
            "accepted_at",
            # ⭐（业务方）：打手 UID / 显示名 / 完成单数 / 投诉取消次数。
            "accept_uid",
            "accept_user_name",
            "accept_over_count",
            "cust_cancel_count",
            # ⭐（业务方）：本地算的利润 / 订单来源 / 验收倒计时。
            "profit",
            "remaining_seconds",
            "order_source",
            "verify_deadline_at",
            "verify_remaining_seconds",
            "verify_overdue",
            # ⭐ 真机定案：申请客服介入的可用性（按钮提示用）。
            "arbitration_available",
            "arbitration_blocked_reason",
            # 撤销协商发起方 + 「同意撤销」可用性（按钮提示用）。
            "cancel_initiator",
            "agree_cancel_available",
            # ⭐ 真机：撤销要求原文 / 撤销原因（同意撤销前要展示的内容）。
            "cancel_cmt",
            "cancel_reason",
            # ⭐ 已结代练费 / 已赔保证金（只在 Status 16/17 有值）。
            "settled_pay_level_bal",
            "settled_rep_ensure_bal",
            # ⭐ 协商撤销满 24 小时的自动申请客服介入——`auto_arbitrate` 是开关（客服发起撤销时可取消勾选）、`cancel_requested_at` 是计时起点、`auto_arbitrated_at` / `auto_arbitrate_note` 是「做了没有 / 为什么没做」。⚠️ 必须单独下发 `auto_arbitrate_note`：`last_error` 每 30 秒会被回读清空。
            "auto_arbitrate",
            "cancel_requested_at",
            "auto_arbitrated_at",
            "auto_arbitrate_note",
            # ⭐（业务方）：**还值不值得轮询**（远端到终态后置 `False`）。
            # 第三方平台订单管理页按订单聚合时，父行「交易状态」要显示**最新活跃推送**
            # 的状态 ⇒ 前端必须有这一列才判得出「哪条还活跃」；没有它只能退化成「取最新一条」。
            "is_active",
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        """⭐ 多平台骨架：**按这一行自己的 `platform`** 取适配器再决定平台能力列。`qr_sent_at` / `qr_last_error` / `auto_reply_last_error` 是「接手后发送」「自动回复」两套能力的产物——未接入平台没有这两套能力，这两组列对它们没有意义（留着历史值只会让页面显示别家的失败原因）⇒ 没有对应能力的平台一律回空值，⛔ 不改键集（页面契约不动）：页面照样拿到同样的键，只是值为空。
        """
        data = super().to_representation(instance)
        adapter = adapter_or_none(instance.platform)
        if adapter is None or not adapter.supports_accept_send:
            data["qr_sent_at"] = None
            data["qr_last_error"] = ""
        if adapter is None or not adapter.supports_auto_reply:
            data["auto_reply_last_error"] = ""
        return data

    def get_game(self, obj) -> str:
        return obj.order.game.name

    def get_server(self, obj):
        server = obj.order.server
        return server.name if server is not None else None

    def get_accept_uid(self, obj) -> str:
        return (obj.accept_uid or "").strip() or (obj.remote_taker_uid or "").strip()

    def get_accept_user_name(self, obj) -> str:
        return (obj.remote_taker_name or "").strip()

    def get_remaining_seconds(self, obj) -> int | None:
        return remaining_seconds_for_push(obj)

    def get_profit(self, obj) -> str | None:
        accept_price = obj.order.total_price
        push_price = obj.price
        if accept_price is None or push_price is None:
            return None
        return f"{accept_price - push_price:.2f}"

    def get_order_source(self, obj) -> str:
        """可直接展示的来源中文名；⛔ 永不返回裸 id / 数字。快照 `Order.source` 正常就是名字；但历史 / 导入数据里可能被写成了字典 id 的数字串（前端按规矩不会拿数字当名字）⇒ 纯数字快照**跳过**、改取 `order_source.name`；两边都取不到 ⇒ 空串（前端画「—」）。
        """
        snapshot = (obj.order.source or "").strip()
        if snapshot and not snapshot.isdigit():
            return snapshot
        source_row = obj.order.order_source
        name = (source_row.name or "").strip() if source_row is not None else ""
        return name

    def get_verify_deadline_at(self, obj):
        return verify_deadline_for_push(obj)[0]

    def get_verify_remaining_seconds(self, obj) -> int | None:
        return verify_deadline_for_push(obj)[1]

    def get_verify_overdue(self, obj) -> bool:
        return verify_deadline_for_push(obj)[2]

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


class PlatformPushListView(APIView):
    """`GET /api/business/platform-pushes/`：本租户的平台推送行（分页）。

    ⭐ 业务方：多带一个 `?group_by=order` ⇒ **按订单聚合**（分页按订单），
    其余参数语义不变；不给 ⇒ 旧的扁平逐行口径（⛔ 一字不改）。
    """

    permission_classes = [has_node(ORDER_LIST_NODE)]

    def get(self, request):
        # 空串 = 前端「没选」而不是非法值（与 `order_search_params` 的既有口径一致）：
        # 先丢掉空串，剩下的坏形状才由序列化器翻成 1000。
        raw = {key: value for key, value in request.query_params.items() if value != ""}
        params = PlatformPushQuerySerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data

        page = data["page"]
        page_size = min(data["page_size"], MAX_PAGE_SIZE)

        if data.get("group_by") == "order":
            return self._grouped_page(data, page=page, page_size=page_size)

        queryset = _push_rows_queryset(data["platform"])
        if data.get("status") is not None:
            queryset = queryset.filter(remote_status=data["status"])
        keyword = data.get("keyword") or ""
        if keyword:
            # 页面搜索框的口径：我方订单号**或**平台单号（`ex_order_no` 就是我们的单号，
            # 但它可能为空 —— `serial_no` 才是平台单号，两者都要能搜到）。
            queryset = queryset.filter(
                Q(order__order_no__icontains=keyword)
                | Q(serial_no__icontains=keyword)
            )

        queryset = queryset.order_by("-created_at", "-id")
        total = queryset.count()
        offset = (page - 1) * page_size
        rows = queryset[offset : offset + page_size]
        return api_response(
            data={
                "items": PlatformPushRowSerializer(rows, many=True).data,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )

    def _grouped_page(self, data, *, page, page_size):
        """⭐ 按订单聚合的一页（业务方）。口径（与模块 docstring「按订单聚合」一节逐条对应）：底集 = `_push_rows_queryset`（平台 + 订单未软删）⇒ **软删 / 租户边界不放松**；组 = 底集里**至少有一条**推送行命中 `status` / `keyword` 的订单；组内 `pushes` = 该单在底集里的**全部**行（⛔ 不按 status / keyword 收窄）；排序 = 组按「该单最新一条推送」的 `created_at` 倒序（`order_id` 兜底）、组内按 `-created_at, -id`；`total` = 订单数（分页按订单）。
        只有 3 条 SQL：`count()` / 本页分组 / 本页推送行（⛔ 不随订单数增长）。
        """
        platform = data["platform"]
        scoped = _push_rows_queryset(platform)

        matched = scoped
        if data.get("status") is not None:
            matched = matched.filter(remote_status=data["status"])
        keyword = data.get("keyword") or ""
        if keyword:
            # ⚠️ 分组模式的关键字比扁平模式多认**来源单号**（业务方：
            # 「命中订单号 / 平台单号 / 来源单号即整组返回」）。扁平模式不动。
            matched = matched.filter(
                Q(order__order_no__icontains=keyword)
                | Q(serial_no__icontains=keyword)
                | Q(order__source_order_no__icontains=keyword)
            )

        groups = (
            scoped.filter(order_id__in=matched.order_by().values("order_id"))
            .values("order_id")
            .annotate(latest_created_at=Max("created_at"))
            # ⚠️ 显式 `order_by` 覆盖模型的默认排序（`DailiantongPush.Meta.ordering`
            # 若漏进 GROUP BY 的查询会变成「列必须出现在 GROUP BY」的 500）。
            .order_by("-latest_created_at", "-order_id")
        )
        total = groups.count()
        offset = (page - 1) * page_size
        page_groups = list(groups[offset : offset + page_size])
        order_ids = [group["order_id"] for group in page_groups]

        # 一次取回本页订单的全部推送行（含订单 / 游戏 / 区服，无 N+1），按组归位。
        pushes_by_order: dict[int, list] = {order_id: [] for order_id in order_ids}
        if order_ids:
            for push in scoped.filter(order_id__in=order_ids).order_by(
                "-created_at", "-id"
            ):
                pushes_by_order[push.order_id].append(push)

        items = []
        for order_id in order_ids:
            rows = pushes_by_order[order_id]
            # 组由 `matched`（`scoped` 的子集）产生 ⇒ 本页每个组至少有一行；
            # 这里仍按「没有就跳过」防御，⛔ 不让一行的意外把整页打成 500。
            if not rows:
                continue
            # 同一组的行指向同一张订单（组键就是 order_id）⇒ 表头字段取组里最新那行。
            order = rows[0].order
            items.append(
                {
                    "order_no": order.order_no,
                    "source_order_no": order.source_order_no,
                    "order_status": order.status,
                    "game": order.game.name,
                    "server": order.server.name if order.server_id else None,
                    "pushes": PlatformPushRowSerializer(rows, many=True).data,
                }
            )
        return api_response(
            data={
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        )


# ── 页签批量计数（fx-D）─────────────────────────────────────


class PlatformPushCountsQuerySerializer(serializers.Serializer):
    """`GET /api/business/platform-pushes/counts/` 的查询参数。

    与列表同一套形状（`platform` / `keyword`）；⛔ 不收 `status`（本端点按状态分桶）。
    """

    platform = serializers.IntegerField(
        required=False,
        default=DEFAULT_PLATFORM,
        min_value=1,
        error_messages={"invalid": "平台参数必须是整数", "min_value": "平台参数不合法"},
    )
    keyword = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
        trim_whitespace=True,
        error_messages={"max_length": "搜索关键字不能超过 64 个字符"},
    )


class PlatformPushCountsView(APIView):
    """`GET /api/business/platform-pushes/counts/`：14 个页签的括号数字**一次**取回。载荷（两套口径并列）：`total` = 范围内**全部**推送行（已软删的订单不算，与列表同口径）；`counts` = 按上游 `remote_status` 分桶（`"10"`–`"18"` 恒在、0 也回；`"none"` = 还没回读过）；`states` = 按本地推送状态分桶（`pending` / `sent` / `failed` / `unknown` / `closed`）；⭐ `order_total` = 范围内**订单数**（同一张单的多条推送只算 1）；⭐ `order_counts` = 按 `remote_status` 分桶的**订单数**（键形状与 `counts` 相同）。
    ⚠️ 口径与页签映射一致：页签按 `remote_status` 筛（`?status=11` 等），因此 `counts` 与「那一档列表的 `total`」同源；`states` 单独回——`closed`（已在代练通删除的历史行）不会被混进状态总数里看不见。⭐ `counts` 与 `order_counts` 的差异（业务方）：`counts` / `total` 数**推送行**（一张单 2 条推送 ⇒ 计 2），`order_counts` / `order_total` 数**订单**（同一张单只计 1）；`order_counts[X]` 的判据 = 「该单**任一**推送行的 `remote_status` 是 X」⇒ 一张单先在 11 后重发到 12 会**同时**落在两个桶里 ⇒ **各桶之和可能大于 `order_total`**（不是算错，是「点每个页签能筛出多少单」；它与列表 `?group_by=order&status=X` 的 `total` **同源**）；`states` 只有行口径（本地推送状态是行属性，订单没有单一取值），按订单聚合后页面要显示本地状态时看组内 `pushes` 即可。
    ⛔ 只读：不调用上游、不写审计。坏入参（`?platform=abc` / 超长关键字）由序列化器翻成 `1000`（HTTP 200 + 业务码），⛔ 不 500。
    """

    permission_classes = [has_node(ORDER_LIST_NODE)]

    #: 页签里**有远端码**的那几档；`10`/`17`/`18` 也回（0 就回 0），页面可自行取用。
    #: ⭐ H7：`18` = 已下架（真机；上游码表没列这一档）——「已下架」页签
    #: 接上读面后需要它**恒在**，否则 0 条时会显示成「—」而不是「(0)」。
    STATUS_BUCKETS = tuple(str(code) for code in range(10, 19))
    #: 「还没回读过」的桶（`remote_status is None`）。
    NONE_BUCKET = "none"

    def get(self, request):
        raw = {key: value for key, value in request.query_params.items() if value != ""}
        params = PlatformPushCountsQuerySerializer(data=raw)
        params.is_valid(raise_exception=True)
        data = params.validated_data

        queryset = DailiantongPush.objects.filter(
            platform=data["platform"],
            order__deleted_at__isnull=True,
        )
        keyword = data.get("keyword") or ""
        if keyword:
            queryset = queryset.filter(
                Q(order__order_no__icontains=keyword)
                | Q(serial_no__icontains=keyword)
            )

        counts = {bucket: 0 for bucket in self.STATUS_BUCKETS}
        counts[self.NONE_BUCKET] = 0
        states = {value: 0 for value, _label in DailiantongPush.State.choices}
        total = 0
        # 订单口径：桶 → 订单 id 集合（**一次**遍历同一份行集算出两套数字，⛔ 不重复查库）。
        order_buckets: dict[str, set[int]] = {
            bucket: set() for bucket in counts
        }
        order_ids: set[int] = set()
        for order_id, remote_status, state in queryset.values_list(
            "order_id", "remote_status", "state"
        ).iterator():
            total += 1
            order_ids.add(order_id)
            if remote_status is None:
                counts[self.NONE_BUCKET] += 1
                bucket = self.NONE_BUCKET
            else:
                # 上游将来新增码：按实际码回，⛔ 不把它静默并进别的桶。
                bucket = str(remote_status)
                counts[bucket] = counts.get(bucket, 0) + 1
            order_buckets.setdefault(bucket, set()).add(order_id)
            states[state] = states.get(state, 0) + 1

        return api_response(
            data={
                "total": total,
                "counts": counts,
                "states": states,
                "order_total": len(order_ids),
                "order_counts": {
                    bucket: len(ids) for bucket, ids in order_buckets.items()
                },
            }
        )
