"""交易平台对接设置：账号 CRUD + 全局发单设置的读写（计划 T2）。端点：`GET`/`POST /api/business/platform-integrations/`（列表数组不分页 / 新增账号）、`GET`/`PATCH`/`DELETE …/<id>/`（单行 / 修改账号 / 删除账号）、`GET`/`PUT …/settings/`（读 / 保存 9 组全局设置 + 门槛/模板三列，全量 / 部分都收）、`GET …/push-availability/`（「一键发单」入口可见性探针，只要登录 + `order.list`）。
「一键发单」入口探针：订单列表那颗地球按钮原来拿**账号列表**端点当数据门，而它要页面节点 `system.platform_integration_settings` ⇒ 生产上客服 / 客服主管 / 售后客服 / 人事客服 / 发单客服**全部没有**这个节点 ⇒ 403 ⇒ 所有客服都看不到按钮。`push-availability/` 是**只读探针**：只要登录 + `order.list`（与真正的发单端点同一个节点）就回 `{"available": true}`；真正的发单动作仍由 `dailiantong_push_views` 自己校验（节点 + 平台账号配置 + 状态机 + 余额，一个字未改）。⛔ 本探针不读配置、不写行、不记日志。凭据红线（AGENTS.md §4）：`app_secret` / `pay_password` 是 **`write_only`**——响应里没有这两个键，只有 `has_secret` / `has_password` 布尔回答「配没配」；`pay_password` 服务端算成 `MD5(密码)` 32 位小写十六进制落 `pay_password_md5`，明文不落库、不进 response、不进任何日志 / 异常文案；更新时不传凭据字段 ⇒ 保持原值（`app_secret` 传空串也按「不改」，照 `XgjShopBindingSerializer` 先例：编辑弹窗留空 = 不改）。
默认账号（评审专门交代）：同平台 `is_default=True` 至多一行由库层部分唯一索引保证（`uniq_platform_integration_default`）；切默认**必须先清旧默认再置新默认**、两步收在同一个 `transaction.atomic()` 里（见 `PlatformIntegrationSerializer.update`）——⛔ 不能先 `save(is_default=True)` 再清旧的，那一步会当场撞唯一约束。并发窗口仍撞上 `IntegrityError` 时，本模块**只拿约束名**判断是不是「默认账号冲突」、转成带字段名中文 `ValidationError`（业务码 `1000`），其它完整性错误原样 re-raise（响亮的 500，E-0010）；⛔ 异常原文（实测 PG 的 `DETAIL: Failing row contains (...)` 会带整行值）与整行值**一个字都不进日志 / 响应**。平台白名单（收窄写入面）：证据件 `system-tripartiteplatformsetting.html:68-87` 的「要对接的平台」radio 只渲染 **7 个** id（10 / 30 / 80 / 90 / 93 / 50 / 60），接口只接受这 7 个、其它 id（包括模型 choices 里另外 6 个）一律拒绝，避免库里出现「页面选不出来的幽灵行」；其中**只有代练通（10）**有真实客户端 ⇒ `is_available` 只对 10 为 `True`，其余 6 个**能存**（本期配置先落库）但**暂不可用**（T3 弹窗据此提示「该平台暂未开通」）。租户边界：`queryset` 只跑在当前连接 / 租户 schema 上，**不接受任何 tenant 参数**（AGENTS.md §4）。
"""

import hashlib

from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopeModelViewSet
from apps.business.dailiantong_push import REQUIRE_TEMPLATE_MAX_LENGTH
from apps.business.models import PlatformIntegration, PlatformPushSettings
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 本模块两个视图共用的 RBAC 节点（页面点 = 1189「交易平台对接设置」）。⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取字符串常量，用变量拼出来的 code 不会被它看见；`apps/rbac/wiring.py` 的位点登记本模块的 `*ViewSet.permission_classes` / `*View.permission_classes`。
PLATFORM_INTEGRATION_NODE = "system.platform_integration_settings"

#: 「一键发单」入口可见性探针的 RBAC 节点：**与真正的发单端点同一个节点**
#: （`dailiantong_push_views.ORDER_PUSH_NODE`，两处都写字符串字面量——B2 走 AST 取字面量，
#: 用变量拼出来的 code 不会被它看见）。⛔ 不新增节点、⛔ 不改发单端点自己的权限。
PUSH_AVAILABILITY_NODE = "order.list"

#: 证据件 `:68-87` 的 7 个平台 radio（顺序照证据件；代练通排第一）。
#: ⛔ 不要直接放开成模型 choices 的 13 个：多出来的 6 个页面选不出来，存进去就是幽灵行。
AVAILABLE_PLATFORMS = (10, 30, 80, 90, 93, 50, 60)

#: 页面「能存」的 7 个里，今天**真的能发单**的只有代练通（10）。
#: 其余 6 个本期只落配置、没有客户端 ⇒ `is_available` 为 `False`。T3 据此提示
#: 「该平台暂未开通」；等对应客户端落地时，把它加进本元组即可（**响应字段不用改**）。
PLATFORMS_WITH_CLIENT = (10,)

#: 库层默认账号唯一约束的名字（`PlatformIntegration.Meta.constraints`）。
#: ⚠️ 只用来**认约束**；异常原文不进日志 / 响应（PG 的 DETAIL 会带整行值）。
DEFAULT_CONSTRAINT = "uniq_platform_integration_default"

#: 支付密码明文长度上限（业务口径：1–64；超长直接拒，不做截断）。
PAY_PASSWORD_MAX_LENGTH = 64

#: 操作日志的 `target_kind`（`OperationLog.target_kind` 是 `CharField(16)`）。
#: ⚠️ 两个值都**恰好 16 字符**（= 列宽）：多一个字符会被 `record()` **静默截断**成
#: 谁也不匹配的 kind，按类型检索的人会漏掉每一行。
PLATFORM_INTEGRATION_TARGET_KIND = "platform_account"
PLATFORM_SETTINGS_TARGET_KIND = "platform_setting"


def _md5_hex(password: str) -> str:
    """`MD5(支付密码)` 的 32 位小写十六进制。`hashlib.md5(...).hexdigest()` 的契约就是 32 位小写十六进制（CPython `hashlib` 文档：`hexdigest()` 返回「double length ... lowercase hexadecimal digits」）⇒ 不需要再 `assert len(...) == 32`（防御性代码别写成承重逻辑）。
    """
    return hashlib.md5(password.encode("utf-8")).hexdigest()


class PlatformIntegrationSerializer(serializers.ModelSerializer):
    """一行平台对接账号的对外形状 + 凭据写入口径。

    ⛔ **不要**把 `app_secret` / `pay_password_md5` 加进 `fields` 的可读列：
    凭据只写不读（见模块 docstring 的凭据红线）。
    """

    #: 平台由白名单收窄（模型 choices 是 13 个，但页面只提供 7 个）。
    #: `invalid_choice` 同时覆盖「不在 13 个 choices 里」与「类型不是整数」两类坏输入。
    platform = serializers.ChoiceField(
        choices=PlatformIntegration.Platform.choices,
        required=True,
        error_messages={
            "required": "请选择平台",
            "blank": "请选择平台",
            "null": "请选择平台",
            "invalid_choice": "平台不在可对接范围内",
        },
    )
    account = serializers.CharField(
        max_length=64,
        error_messages={
            "required": "平台账号不能为空",
            "blank": "平台账号不能为空",
            "null": "平台账号不能为空",
        },
    )
    #: 明文写入、原样存库（⛔ 不 strip：密钥里的首尾空格由平台方决定，本仓不替它裁）。
    #: 空串在**更新**时按「不改」处理（见 `update()`）；新增时存空串 = 还没配置。
    app_secret = serializers.CharField(
        max_length=1000, write_only=True, required=False, allow_blank=True,
        trim_whitespace=False,
        help_text="平台 AppSecret（只写不读；改时留空 = 不改）",
    )
    #: 明文支付密码：服务端算成 `MD5(密码)` 落库，明文不落库、不回显。
    #: ⛔ 不设 `max_length` 字段参数：超长要走本字段的**中文**校验文案，而不是 DRF 的英文默认文案。
    pay_password = serializers.CharField(
        write_only=True, required=False, allow_blank=True, trim_whitespace=False,
        help_text="支付密码明文（只写不读；服务端只存 MD5，改时留空 = 不改）",
    )
    has_secret = serializers.SerializerMethodField()
    has_password = serializers.SerializerMethodField()
    platform_label = serializers.CharField(source="get_platform_display", read_only=True)
    is_available = serializers.SerializerMethodField()

    class Meta:
        model = PlatformIntegration
        fields = [
            "id",
            "platform",
            "platform_label",
            "account",
            "app_id",
            # 只写不读：这两个键**不在**响应里（有测试逐个 JSON 位置搜明文 / md5）。
            "app_secret",
            "pay_password",
            "has_secret",
            "has_password",
            "is_enabled",
            "is_default",
            "sync_accept",
            "sms_notify",
            "include_push",
            "contact_qq",
            "contact_phone",
            "remark",
            "is_available",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    # ── 只读替身 ────────────────────────────────────────────────────
    def get_has_secret(self, obj) -> bool:
        return bool(obj.app_secret)

    def get_has_password(self, obj) -> bool:
        return bool(obj.pay_password_md5)

    def get_is_available(self, obj) -> bool:
        """该平台今天**有没有客户端**（≠ 能不能存；7 个都能存）。"""
        return obj.platform in PLATFORMS_WITH_CLIENT

    # ── 字段校验 ────────────────────────────────────────────────────
    def validate_platform(self, value):
        """白名单收窄：模型 choices 里另外 6 个平台**页面选不出来**，⛔ 不收。"""
        if value not in AVAILABLE_PLATFORMS:
            raise serializers.ValidationError(
                "平台不在可对接范围内（本页只支持代练通、代练妈妈、代练丸子、"
                "趣代练、天玩代练、咕噜代练、易代练）"
            )
        return value

    def validate_account(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("平台账号不能为空")
        return cleaned

    def validate_pay_password(self, value):
        """写支付密码时的两条硬校验：非空、长度 1–64。

        ⚠️ **空串 = 不改**只在「字段没传」时成立；传了空串是**明确的写操作** ⇒ 拒绝，
        否则「把密码清空」会变成一个静默 no-op，调用方以为改成功了。
        """
        if value is None:
            return value
        if value == "":
            raise serializers.ValidationError("支付密码不能为空")
        if len(value) > PAY_PASSWORD_MAX_LENGTH:
            raise serializers.ValidationError(
                f"支付密码不能超过 {PAY_PASSWORD_MAX_LENGTH} 个字符"
            )
        return value

    # ── 默认账号的唯一性（先清后置）────────────────────────────────
    def _demote_existing_default(self, platform, *, exclude_pk=None) -> None:
        """把同平台**其它**默认行清掉（只在 `is_default=True` 的写路径上调）。

        ⚠️ 这是「换默认」的前半步：必须在置新默认**同一个事务内**先跑，
        否则新默认的 INSERT / UPDATE 会撞部分唯一索引。
        """
        qs = PlatformIntegration.objects.filter(platform=platform, is_default=True)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        # `auto_now` 的 `updated_at` 不参与：这里只改 `is_default`，被降级的行不该被记成
        # 「刚刚被编辑过」（页面按 `updated_at` 排序时会看到无关的跳动）。
        qs.update(is_default=False)

    def create(self, validated_data):
        pay_password = validated_data.pop("pay_password", None)
        if pay_password is not None:
            validated_data["pay_password_md5"] = _md5_hex(pay_password)
        with transaction.atomic():
            if validated_data.get("is_default"):
                # 新增即默认 = 一次「换默认」（清旧 + 置新在同一事务里）。
                self._demote_existing_default(validated_data["platform"])
            return super().create(validated_data)

    def update(self, instance, validated_data):
        pay_password = validated_data.pop("pay_password", None)
        if pay_password is not None:
            validated_data["pay_password_md5"] = _md5_hex(pay_password)
        # 编辑弹窗留空 = 不改（照 `XgjShopBindingSerializer` 的既有先例）。
        if "app_secret" in validated_data and validated_data["app_secret"] == "":
            validated_data.pop("app_secret")
        with transaction.atomic():
            platform = validated_data.get("platform", instance.platform)
            is_default = validated_data.get("is_default", instance.is_default)
            if is_default:
                self._demote_existing_default(platform, exclude_pk=instance.pk)
            return super().update(instance, validated_data)


class PlatformIntegrationViewSet(EnvelopeModelViewSet):
    """`/api/business/platform-integrations/` 的列表 / 新增 / 单行 / 修改 / 删除。⚠️ **关闭 PUT**：本页只用 `PATCH`，`PUT` 是 `ModelViewSet` 默认多给的能力面，没有消费方就不暴露（同 `ShopBindingViewSet` 先例）；`lookup_value_regex` 只认数字——`settings/` 是一条**字面**路径，不能让 router 的 detail 模式把 `settings` 当主键吃进去（否则查整数主键会抛 `ValueError` → 500）。
    ⚠️ `DELETE` 当前默认行**允许**：删完之后该平台没有默认账号（⛔ 不自动挑一个顶上，那会让「谁默认」变成服务端替用户做的决定）。⚠️ 整个集合（读 + 写）共用页面节点 `system.platform_integration_settings`：端点面与页面同宽，不留「列表要节点、单行不要」的窄门。
    """

    queryset = PlatformIntegration.objects.all()
    serializer_class = PlatformIntegrationSerializer
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    lookup_value_regex = r"[0-9]+"
    permission_classes = [
        IsAuthenticated,
        has_node(PLATFORM_INTEGRATION_NODE),
    ]

    def perform_create(self, serializer):
        # 留痕与业务写入同一个事务（spec §7.4）：日志写不进去时账号行要跟着回滚。
        with transaction.atomic():
            self._save_translating_default_conflict(serializer)
            user = self.request.user
            record(
                log_type=ACTION_LOG_TYPES["system.platform_integration_create"],
                action="system.platform_integration_create",
                actor=user.get_username(),
                actor_id=user.pk,
                target_kind=PLATFORM_INTEGRATION_TARGET_KIND,
                target_id=serializer.instance.pk,
                target_name=serializer.instance.account,
            )

    def perform_update(self, serializer):
        with transaction.atomic():
            # no-op 判据必须在**写之前**取：`save()` 之后实例上就是新值了。
            changed = self._changes_anything(serializer)
            self._save_translating_default_conflict(serializer)
            if changed:
                user = self.request.user
                record(
                    log_type=ACTION_LOG_TYPES["system.platform_integration_update"],
                    action="system.platform_integration_update",
                    actor=user.get_username(),
                    actor_id=user.pk,
                    target_kind=PLATFORM_INTEGRATION_TARGET_KIND,
                    target_id=serializer.instance.pk,
                    target_name=serializer.instance.account,
                )

    def perform_destroy(self, instance):
        # `delete()` 会把实例 pk 置 `None` ⇒ 目标两列必须在**删除前**取。
        target_id = instance.pk
        target_name = instance.account
        with transaction.atomic():
            super().perform_destroy(instance)
            user = self.request.user
            record(
                log_type=ACTION_LOG_TYPES["system.platform_integration_destroy"],
                action="system.platform_integration_destroy",
                actor=user.get_username(),
                actor_id=user.pk,
                target_kind=PLATFORM_INTEGRATION_TARGET_KIND,
                target_id=target_id,
                target_name=target_name,
            )

    @staticmethod
    def _changes_anything(serializer) -> bool:
        """`PATCH` 的载荷里有没有任何一处与实例**当前值**不同（纯 no-op 不写留痕）。⚠️ `pay_password` 不是一个模型列（序列化器层才换成 `pay_password_md5`）⇒ 必须单独比摘要，⛔ 不能 `getattr(instance, "pay_password")`（会抛 `FieldDoesNotExist` / `AttributeError`）；`app_secret` 传空串 = 「不改」（`update()` 会把它 pop 掉）⇒ 这里同样不算变化。
        """
        instance = serializer.instance
        for field, value in serializer.validated_data.items():
            if field == "pay_password":
                if _md5_hex(value) != instance.pay_password_md5:
                    return True
                continue
            if field == "app_secret" and value == "":
                continue
            if value != getattr(instance, field):
                return True
        return False

    @staticmethod
    def _save_translating_default_conflict(serializer) -> None:
        """`save()` 撞默认账号唯一约束 ⇒ 转成带字段名的中文业务错误（`1000`）。⚠️ 正常路径下**不该**走到这里（换默认已经在 `transaction.atomic()` 里先清旧再置新），接住它只处理并发窗口（两个请求同时抢同一个平台的默认位）。
        ⚠️ 只拿**约束名**判断；⛔ 不记 `str(exc)`——PG 的 `DETAIL: Failing row contains (...)` 会带整行的明文凭据，连 `logger` 都不碰（E-0010：未预期错误仍要响亮 500）。
        """
        try:
            serializer.save()
        except IntegrityError as exc:
            if DEFAULT_CONSTRAINT not in str(exc):
                raise
            # `from None`：不把原始异常挂到链上（它可能带整行值）。
            raise serializers.ValidationError(
                {"is_default": "默认账号设置失败：该平台已有默认账号，请稍后重试"}
            ) from None


def _choice_field(choices, *, label: str, options: str):
    """9 组里 `0/1` 那几组的公共字段工厂（`invalid_choice` 文案带字段名）。"""
    return serializers.ChoiceField(
        choices=choices,
        required=False,
        error_messages={"invalid_choice": f"{label}只能是 {options}"},
    )


class PlatformPushSettingsSerializer(serializers.ModelSerializer):
    """9 组全局设置（`GET` / `PUT /api/business/platform-integrations/settings/`）。**未知字段拒绝**：`Serializer` 默认静默忽略多给的键 ⇒ 本类在 `to_internal_value` 里显式拒绝（否则前端把 `legacy_simple_mode` 拼错成 `legacy_simple_mod` 时，保存会「成功」但什么都没改）。
    **全量 / 部分都收**：视图固定 `partial=True`，PUT 只给一两个键也合法；`0/1` 那几组用显式 `ChoiceField`，非法值给**带字段名**的中文文案（⛔ 不是 DRF 的英文 `"… is not a valid choice."`）。
    """

    legacy_simple_mode = _choice_field(
        PlatformPushSettings.YesNo.choices, label="传统简易模式", options="0（否）或 1（是）"
    )
    check_game_region = _choice_field(
        PlatformPushSettings.YesNo.choices, label="检测游戏大区准确性", options="0（否）或 1（是）"
    )
    duplicate_push_check = _choice_field(
        PlatformPushSettings.DuplicateCheck.choices,
        label="检测重复发单",
        options="0（检测）或 1（不做检测）",
    )
    block_push_completed_orders = _choice_field(
        PlatformPushSettings.CompletedPushLimit.choices,
        label="已完成订单不允许再发",
        options="0（不限制）或 1（限制）",
    )
    auto_send_group_qr_after_accept = _choice_field(
        PlatformPushSettings.GroupQrSend.choices,
        label="平台接手后自动发群二维码",
        options="0（不发送）或 1（自动发）",
    )
    # ⭐（一键抛单计划 §2.4 / §4）：新增三列，接口读写。
    # 「要求最低完单数」：上游语义 = 接单者完单结算数 ≥ 此值；默认 1。
    default_power_level = serializers.IntegerField(
        required=False,
        min_value=0,
        error_messages={
            "min_value": "要求最低完单数不能小于 0",
            "invalid": "要求最低完单数必须是整数",
        },
    )
    # 「拒绝新手和介入率」：留空 = 不设置（不发上游参数）。
    # 语义照代练通文档：拒绝客服介入**百分比**大于此值的人接单 ⇒ 0–100。
    default_limit_accept = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        max_value=100,
        error_messages={
            "min_value": "拒绝新手和介入率不能小于 0",
            "max_value": "拒绝新手和介入率不能大于 100（按百分比填写）",
            "invalid": "拒绝新手和介入率必须是整数（留空表示不设置）",
        },
    )
    # 「代练/陪玩要求」全局模板：空串 = 不配置（发单时用系统默认占位文案）。
    require_template = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=REQUIRE_TEMPLATE_MAX_LENGTH,
        trim_whitespace=False,
        error_messages={
            "max_length": f"代练要求模板不能超过 {REQUIRE_TEMPLATE_MAX_LENGTH} 个字符",
        },
    )

    class Meta:
        model = PlatformPushSettings
        fields = [
            "quick_push_enabled",
            "legacy_simple_mode",
            "check_game_region",
            "forbid_push_when_double_gold_zero",
            "duplicate_push_check",
            "select_jidanpu_center_by_default",
            "auto_zero_refund_on_conflict",
            "block_push_completed_orders",
            "auto_send_group_qr_after_accept",
            "default_power_level",
            "default_limit_accept",
            "require_template",
        ]

    def to_internal_value(self, data):
        # E-0010：请求来源的值一律先过序列化器；非对象（数组 / 字符串 / 数字）在这里就变成 200 + `1000`，不让它走到 `.get(...)` / 字段访问那一步变成 500。
        # ⚠️ 必须用**带键的** `ValidationError`：DRF 的 `Serializer.run_validation` 把字符串 detail 直接塞进 `_errors`，而 `errors` 属性按 dict 组装 ReturnDict ⇒ 裸字符串会抛 `ValueError`（测试实测 `body='abc'` 那一条）；`non_field_errors` 是 DRF 自己的非字段错误键。
        if not isinstance(data, dict):
            raise serializers.ValidationError({"non_field_errors": "请求体必须是对象"})
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError(
                {unknown[0]: f"不支持的设置项：{unknown[0]}"}
            )
        return super().to_internal_value(data)


class PlatformIntegrationSettingsView(APIView):
    """`/api/business/platform-integrations/settings/`：9 组全局设置的读 / 保存。⚠️ 单行表：读侧一律 `PlatformPushSettings.get_solo()`（`get_or_create(pk=1)`，首次 GET 会懒建默认行）；⛔ 不手工建第二行。`PUT` 也接受**部分**字段（`partial=True`）：页面可能只提交改过的那几组，未知字段一律拒（见 `PlatformPushSettingsSerializer.to_internal_value`）。
    ⚠️ 门与账号集合同一个页面节点：本页是「交易平台对接设置」的一整页。
    """

    permission_classes = [
        IsAuthenticated,
        has_node(PLATFORM_INTEGRATION_NODE),
    ]

    def get(self, request):
        row = PlatformPushSettings.get_solo()
        return api_response(data=PlatformPushSettingsSerializer(row).data)

    def put(self, request):
        with transaction.atomic():
            row = PlatformPushSettings.get_solo()
            serializer = PlatformPushSettingsSerializer(
                row, data=request.data, partial=True
            )
            serializer.is_valid(raise_exception=True)
            # 纯 no-op 不写留痕：值没变 = 什么都没发生（与配置域 R27.2 同口径）。
            changed = any(
                getattr(row, field) != value
                for field, value in serializer.validated_data.items()
            )
            serializer.save()
            if changed:
                user = request.user
                record(
                    log_type=ACTION_LOG_TYPES["system.platform_settings_update"],
                    action="system.platform_settings_update",
                    actor=user.get_username(),
                    actor_id=user.pk,
                    target_kind=PLATFORM_SETTINGS_TARGET_KIND,
                    target_id=row.pk,
                )
        # E-0022：不在 `atomic()` 里 `return`（成功路径也统一放到块外）。
        return api_response(data=serializer.data)


class PushAvailabilityView(APIView):
    """`GET /api/business/platform-integrations/push-availability/`：发单入口可见性探针。为什么需要它：订单列表那颗「一键发单到交易平台」按钮原来读 `GET /api/business/platform-integrations/` 判断「本租户有没有启用的平台账号」，而那个端点要**页面节点** `system.platform_integration_settings`——生产上客服 / 客服主管 / 售后客服 / 人事客服 / 发单客服都没有该节点 ⇒ 403 ⇒ 所有客服都看不到按钮；业务口径：**能看到订单列表（`order.list`）的人就该看到这颗按钮**，真正的发单动作由发单端点自己校验（同一个 `order.list` 节点 + 平台账号配置 + 状态机 + 余额）。
    返回 `{"available": true}`：只要请求通过本端点的节点门，就允许前端画入口。⚠️ 这里**不查** `PlatformIntegration`：它回答的是「这个人该不该看到入口」，不是「本租户配好了没有」——租户没配时点进发单页会得到发单端点自己的 1002 人话（带「去配置」的下一步），比在列表页静默藏掉入口更容易让人找到出路。
    边界：⛔ 不读配置、不写任何行、不产生操作日志（纯只读探针）；⛔ 不改发单端点自身的权限与校验（`dailiantong_push_views` 一个字未动）；⛔ 不新增 RBAC 节点（沿用既有 `order.list`）。
    """

    permission_classes = [IsAuthenticated, has_node(PUSH_AVAILABILITY_NODE)]

    def get(self, request):
        return api_response(data={"available": True})
