"""账号接口请求值的校验入口（E-0010）：判据是「**这类请求**还会不会 500」——请求来源的值先过
这里再进 ORM，**包括查询参数**（`?is_active=maybe` 直接进 `filter()` 会 `ValueError` → HTML 500）。
覆盖写路径、列表筛选与启停载荷（启停走 `AccountStatusSerializer` / `AccountBatchStatusSerializer`）。"""
from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework.fields import empty

from apps.accounts.choices import BusinessType, CommissionMode, MemberType
from apps.business.models import Game
from apps.rbac.models import Role

User = get_user_model()

#: 只能由服务端管理的字段。请求里出现即**拒绝**（不是忽略）——静默忽略会让调用方以为改密成功
#: （改密只走 reset-password 端点，spec §7.4）。⚠️ `deleted_at` 刻意不在字段声明里、但在本元组里：
#: 删除时间只能由 `delete/` / `restore/` 端点写，PATCH 带它即拒（否则静默 no-op 还回 code 0）。
SERVER_MANAGED_FIELDS = ("password", "is_superuser", "is_staff", "is_active", "deleted_at")

#: PostgreSQL `integer`（int4）的上界。`grab_limit_dl` / `grab_limit_pw` 在模型上是
#: `PositiveIntegerField`（自带 `MaxValueValidator(2147483647)`），但该校验器只在 `full_clean()` 里跑，
#: 而写路径是 `setattr` + `save()`——上界不在序列化器钉住，越界值落到 int4 列会抛 `DataError`(22003) → HTML 500。
INT4_MAX = 2147483647

#: 「登录账号已存在」的错误 `code`。视图（`AccountViewSet.create`）据此把它映射成
#: `ErrorCode.CONFLICT`（1001，spec §5.1 要求重名**可区分**于其它校验失败）。
#: 判别不能用「`username` 这个键在不在」——空串与超长 username 也挂在同一个键下。
DUPLICATE_USERNAME = "duplicate_username"

#: spec §3.2 / §4.2 标「必填，照抄」的六个字段（对标名 `userName` / `brokerageAmount` / `recOrderMaxCount(2)` /
#: `inputDate` / `insureAmount`）。⚠️ **只在新增（create）上强制必填**（裁决 R13），编辑走「缺席即保持库里的值」；
#: 实现上声明 `required=False`（与 `default=` 不能并存，`rest_framework/fields.py:308`），`__init__` 按是否新增翻 `True`。
CREATE_REQUIRED_FIELDS = (
    "display_name",
    "commission_value",
    "grab_limit_dl",
    "grab_limit_pw",
    "hired_on",
    "deposit_amount",
)


class MultiRoleField(serializers.CharField):
    """`role` 查询参数：逗号分隔**多选**——formselects 多选拼 `"客服,财务"`（`formselects-v4.js` 的
    `valStr` = `join(',')`），单选 `ChoiceField` 会整串拒掉 ⇒ 勾两个职位筛选就报错；出参 `list[str]` 直供
    `groups__name__in=`；每段都必须在**当前租户** `Role` 表存在（**请求期**查库，choices 类定义时无租户上下文），任一非法**整条拒**。"""

    default_error_messages = {
        "invalid_choice": "“{input}” 不是合法选项。",
        "blank_item": "职位不能为空（逗号之间 / 结尾不得出现空项）。",
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("allow_blank", True)
        super().__init__(**kwargs)

    def run_validation(self, data=empty):
        """先接管「整串空白」再交给 `CharField`：`CharField.run_validation` 会把空白输入短路掉、
        `to_internal_value` 收不到——实测（`AccountFilterSerializer(data={"role": " "})`）拦住它的
        是 `run_validation`。空串 ⇒ `[]`（按「没传」），只有空白 ⇒ 1000。"""
        if isinstance(data, str):
            if data.strip() == "":
                if data == "":
                    # `?role=`（清空下拉）⇒ 按「没传」处理（与 `views.list()` 同口径）。
                    return []
                # `?role=%20` ⇒ **拒**（不是返回全量）：只有空白等于「发了个没意义的筛选」。
                self.fail("blank_item")
        return super().run_validation(data)

    def to_internal_value(self, data):
        raw = super().to_internal_value(data)
        if raw == "":
            return []
        roles = [item.strip() for item in raw.split(",")]
        for role in roles:
            # 空段 ⇒ 拒（不静默丢）：否则 `"客服,"` 与 `"客服"` 同形，调用方看不出自己发错了。
            if role == "":
                self.fail("blank_item")
            # 请求期存在性校验：不在当前租户 `Role` 表里 ⇒ 整条拒。
            validate_role_exists(role)
        return roles


def validate_role_exists(value):
    """职位名的**唯一**校验出口：必须能在当前租户的 `Role` 表里找到。⚠️ 必须是**请求期**校验
    （choices 在类定义时求值、那时没有租户上下文）；write 与 filter **共用本函数**——两份各写一次会让
    「下拉里能选、筛选却 1000」复发。失败形态 DRF `ValidationError` ⇒ 200 + code 1000（E-0010）。"""
    if not Role.objects.filter(name=value).exists():
        raise serializers.ValidationError("该职位不存在", code="role")
    return value


class AccountFilterSerializer(serializers.Serializer):
    """列表筛选参数。非法值 → DRF `ValidationError` → 200 + `code 1000`。七个字段都按 E-0010：
    `?game=abc` / `?business_type=9` / `?settle_forbidden=maybe` / `?is_cert=X` 必须在入口被拒成 1000，
    不能带着字符串进 `filter()` 抛 `ValueError` → HTML 500（空串已由视图滤掉，本层只管「非空但非法」）。"""

    # 「选择职位」是**多选**（逗号分隔）；取值域 = **当前租户的全部角色**（内置 + 自建，
    # `/api/accounts/roles/` 的 `roles` 动作同一来源）。⛔ 不能退回单值
    # `CharField(validators=[validate_role_exists])`：formselects 多选发 `"客服,财务"`，单值整串拒成 1000。
    role = MultiRoleField(required=False)
    is_active = serializers.BooleanField(required=False)
    key = serializers.CharField(required=False, allow_blank=True, max_length=64)
    # 「游戏权限」按 **Game 的主键 id** 取值（不是游戏名）：前端下拉的 `<option value>` 就是 id。
    # `min_value=1`：主键从 1 开始，`?game=0` 不可能命中任何行，在入口拒掉。
    game = serializers.IntegerField(min_value=1, required=False)
    opened_by = serializers.IntegerField(min_value=1, required=False)
    # `business_type` / `member_type` 的取值域与写路径**共用同一份枚举**（`choices.py`）：
    # 两处各抄一份必然漂移（同 `role` 与 `roles` 动作共用 `DEFAULT_ROLES` 的口径）。
    business_type = serializers.ChoiceField(choices=BusinessType.choices, required=False)
    member_type = serializers.ChoiceField(choices=MemberType.choices, required=False)
    # ⭐ 工作室筛选（D-0228）：缺席 = 不过滤；**false 是合法且有意义的值**（打手那一档，
    # E-0021——真值判断会把「只看非工作室」丢掉）。视图已把空串滤成「没传」。
    is_studio = serializers.BooleanField(required=False)
    # ⭐ 打手身份筛选（D-0228 改准：按「代练员」职位，不按成员类型）。false = 内部员工档。
    is_worker = serializers.BooleanField(required=False)
    # 「是否实名」用 `Y`/`N`（对标原文的取值），**不是**布尔：
    # `Y` = 有身份证号；`N` = 号码为空**或没有档案行**。
    is_cert = serializers.ChoiceField(choices=("Y", "N"), required=False)
    # 「禁止结算」是布尔，`false` 是**合法且有意义**的取值（只看未禁止的人）——
    # 下游判据必须用 `is not None`，不能用真值（E-0021）。
    settle_forbidden = serializers.BooleanField(required=False)
    # 「搜索2 / 搜索3」分别摸支付宝（账号或姓名）与备注 / 标记，都是模糊匹配。
    key2 = serializers.CharField(required=False, allow_blank=True, max_length=64)
    key3 = serializers.CharField(required=False, allow_blank=True, max_length=64)
    # 「已删除」：`true` ⇒ **只看已删的人**（回收站入口）；不传 / `false`
    # ⇒ 默认口径（排除已删）。⚠️ 它**不动** `is_active` 的语义——「禁用」与「已删除」
    # 是两件事，`?is_active=false` 仍然只看「禁用但没删」的人。
    deleted = serializers.BooleanField(required=False)


class AccountStatusSerializer(serializers.Serializer):
    """#5 单个启停的请求体：`{"is_active": bool}`——**幂等 `set` 的目标值**，不是 toggle 标记（spec §3.3 注）。
    `BooleanField` 是硬要求：`{"is_active": "maybe"}` 直接 `save()` 会被 Django 当真值写库（非空字符串），
    接口回 200 而库里变「启用」——正是 E-0010 要挡的形态。"""

    is_active = serializers.BooleanField()


class AccountBatchStatusSerializer(serializers.Serializer):
    """#6 批量启停的请求体。`allow_empty=False`：空 `ids` 是**没有意义的请求**而非 no-op 成功
    （回 1000，别回空 `updated` 让人以为改成功）；`min_value=1`：非正整数不可能是 `auth_user.pk`，入口拒；
    `max_length=200`（spec §9-8 拟定值，与 `max_page_size` 同量级、可调）：挡「一次请求锁全租户账号行」的形状。"""

    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=200,
    )
    is_active = serializers.BooleanField()


class AccountWriteSerializer(serializers.Serializer):
    """新增（`partial=False`）与编辑（`partial=True`）共用。⚠️ 下面五个字段都是 `>= 0`（`min_value=0`）：
    `base_salary` / `commission_value` / `deposit_amount` / `grab_price_min` / `grab_price_max`——它们是
    薪酬与限额、**不是订单金额**，不适用 D-0015（那管的是订单的退款 / 冲抵 / 调整），⛔ 别把这条推广到订单去。"""

    creation_required_fields = CREATE_REQUIRED_FIELDS

    username = serializers.CharField(max_length=150)
    display_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    role = serializers.CharField(validators=[validate_role_exists])
    games = serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=False
    )

    id_card_no = serializers.CharField(max_length=18, required=False, allow_blank=True, default="")

    def validate_id_card_no(self, value: str) -> str:
        """手填身份证号：**strip**；去空白后为空 ⇒ 落库为**空串**（E-0252 写入侧收口）——「已实名」判据与
        列表实名筛选都假设档案不躺纯空格。与 DRF 默认 trim 是重复防线，显式写出是因 E-0252 点名、且
        `RealNameVerifySerializer` 关掉过 trim——照抄进来时判据网必须红（`AccountIdCardWhitespaceNormalizationTests`）。"""
        return value.strip()

    business_type = serializers.ChoiceField(choices=BusinessType.choices, required=False,
                                            default=BusinessType.BOOSTING)
    member_type = serializers.ChoiceField(choices=MemberType.choices, required=False,
                                          default=MemberType.INTERNAL)
    # ⭐ 工作室标记（D-0228）：新开/编辑表单的勾选；新增缺省 False。
    is_studio = serializers.BooleanField(required=False, default=False)
    base_salary = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                           required=False, allow_null=True, default=None)
    commission_mode = serializers.ChoiceField(choices=CommissionMode.choices, required=False,
                                              default=CommissionMode.ORDER_PERCENT)
    # ⚠️ 下面六个字段**不给 `default=`**：`required=True` 与 `default` 不能并存
    # （`rest_framework/fields.py:308`），而必填性要按「新增 / 编辑」在 `__init__` 里翻。
    commission_value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                                required=False)
    alipay_account = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    alipay_name = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    bank_name = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    bank_card_no = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    grab_limit_dl = serializers.IntegerField(min_value=0, max_value=INT4_MAX, required=False)
    grab_limit_pw = serializers.IntegerField(min_value=0, max_value=INT4_MAX, required=False)
    grab_price_min = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                              required=False, allow_null=True, default=None)
    grab_price_max = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                              required=False, allow_null=True, default=None)
    # 「排序值」：0 是**合法值**（不是「没传」），上界 9999 来自对标输入框 `maxlength=4`；「禁止结算」
    # 的 `False` 同样是合法值（E-0021）。两个都不给 `default=`，缺席交给模型默认值，不在这一层造值。
    order_by = serializers.IntegerField(min_value=0, max_value=9999, required=False)
    settle_forbidden = serializers.BooleanField(required=False)
    # 「禁自助接单」（P3e）：布尔开关**不给 `default=`**——
    # `partial=True`（PATCH）下，带默认值会让每个只改别的字段的请求都顺手把它重置一遍
    # （静默关掉别人的开关）。缺席 = 保持库里的值。
    grab_forbid_self_service = serializers.BooleanField(required=False)
    # 钱⑤（业务方裁定）：账户级「禁止结算」，接法与「禁自助接单」同一个写法——布尔开关、不给
    # `default=`，写入口 `_persist_account` 按「请求里出现过的键」逐列赋值。⚠️ 上面三个新键**不进**
    # `CREATE_REQUIRED_FIELDS`；`deleted_at` 刻意不进字段声明（软删走专门端点，见 `SERVER_MANAGED_FIELDS`）。
    live_url = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    qq = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    mobile = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    hired_on = serializers.DateField(required=False, allow_null=True)
    # ⚠️ `max_length=100`（**不是 64**）：对标的「设置标记」输入框是 `maxlength: 100`，模型列原先是
    # 64 ⇒ 65~100 字的标记「前端放行、后端拒」。加宽的是后端（⛔ 不收窄前端 maxlength——那砍掉对标能力），
    # 模型列与序列化器是同一个数字，两处必须一起改。
    tag = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    remark = serializers.CharField(required=False, allow_blank=True, default="")
    deposit_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0,
                                              required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # R13：新增（无既存实例且非部分更新）时，六个「必填，照抄」字段缺一不可。判据用
        # `_current_user()`（认 `instance=` 与 `context["instance"]`）而非 `self.instance`：
        # 本仓 `update()` 走 `context={"instance": user}`，编辑路径上 `self.instance` 其实是 None。
        if self._current_user() is None and not self.partial:
            for name in self.creation_required_fields:
                self.fields[name].required = True

    def validate(self, attrs):
        sent = [f for f in SERVER_MANAGED_FIELDS if f in self.initial_data]
        if sent:
            raise serializers.ValidationError(
                {f: "该字段不能通过本接口修改" for f in sent}
            )
        # 价格区间：只有两端都有值时才要求有序（与模型 CHECK 同口径，但先在这里给出可读错误）
        low, high = self._merged_price_bounds(attrs)
        if low is not None and high is not None and high < low:
            raise serializers.ValidationError({"grab_price_max": "上限不能小于下限"})
        return attrs

    def _current_user(self):
        """本次编辑的目标账号；新增时为 `None`。调用方把实例放在 `context["instance"]`（计划的
        `update()` 一节），DRF 标准的 `instance=` 也认——两种都支持，免得同一件事有两套入口。"""
        return self.instance if self.instance is not None else self.context.get("instance")

    def _merged_price_bounds(self, attrs):
        """把本次请求的值与**已存在档案**的值合并后再比大小。`partial=True` 只发一端时另一端沿用
        库里的值：只看本次请求会放过「新上限 < 库里的下限」，那条请求落到模型 CHECK 上、调用方
        拿到的就不是「上限不能小于下限」这条可读错误。"""
        profile = getattr(self._current_user(), "staff_profile", None)
        low = attrs.get("grab_price_min", getattr(profile, "grab_price_min", None))
        high = attrs.get("grab_price_max", getattr(profile, "grab_price_max", None))
        return low, high

    def validate_username(self, value):
        qs = User.objects.filter(username=value)
        instance = self._current_user()
        if instance is not None:
            qs = qs.exclude(pk=instance.pk)
        if not qs.exists():
            return value
        # ⭐ T6（spec §2.7，业务方拍板）：同名行**全部**是软删的 ⇒ **放行**（create 走恢复路径、外键接回
        # 同一行），有**在用的**才 1001。判据是数量比较（E-0021 反面）：没档案行的账号（`seed_tenant` 的
        # `admin`）不可能被软删、必须落「在用」档；⛔ 别写 `staff_profile__deleted_at__isnull=True`（LEFT JOIN 会放掉没删过的人）。
        if qs.count() != qs.filter(staff_profile__deleted_at__isnull=False).count():
            raise serializers.ValidationError("登录账号已存在", code=DUPLICATE_USERNAME)
        return value

    def validate_games(self, value):
        # 请求里的 game id 在这里按**当前租户 schema** 解析（连接由 django-tenants 切好）：
        # 别的租户的 game 在这个 schema 里查不到 → 拒绝。
        found = set(Game.objects.filter(pk__in=value).values_list("pk", flat=True))
        missing = sorted(set(value) - found)
        if missing:
            raise serializers.ValidationError(f"游戏不存在：{missing}")
        return value


# ── ⭐ T8（实名认证线）：管理页那两个端点的请求值入口 ──────────────────
class RealNameLogFilterSerializer(serializers.Serializer):
    """实名记录列表的筛选参数（spec §9.1）。⚠️ `status` 是**字符枚举** `"0"` / `"1"` / `"2"`（对标取数参数原样，
    证据 `docs/JIDANPU-REFERENCE.md:1078`；控件名 `certStatus` ⛔ 不照抄成 `cert_status`、⛔ 不写 `IntegerField`）；
    `"0"` =「全部」是真实取值 ⇒ 视图用 `is not None` 判（E-0021）；`sdate` / `edate` 用 `DateField` 入口拒畸形（E-0010）。"""

    sdate = serializers.DateField(required=False)
    edate = serializers.DateField(required=False)
    #: `ChoiceField` ⇒ `status=9` / `status=abc` 在入口被拒（1000），不会静默返回全部。
    status = serializers.ChoiceField(choices=("0", "1", "2"), required=False)
    #: 三个搜索框（对标 `key1` 系统用户名称 / `key2` 姓名 / `key3` 身份证号）。
    #: `max_length` 取各自列的宽度：超长值不进 `icontains`（那会白扫一遍全表）。
    key1 = serializers.CharField(required=False, allow_blank=True, max_length=64)
    key2 = serializers.CharField(required=False, allow_blank=True, max_length=64)
    key3 = serializers.CharField(required=False, allow_blank=True, max_length=18)


class RealNameSettingWriteSerializer(serializers.Serializer):
    """实名四项配置的 `PATCH` 载荷（spec §9.1 / §10.1）。全部 `required=False` + 视图 `partial=True` ⇒ 未传**不改**；
    「没传」与「传了假值」只能靠键在不在 `validated_data` 里分（⛔ `data.get(键, 默认值)` 把未传当传了默认——
    D-0208 同款坑：静默关掉实名开关）；年龄下界 0（= 不限制，E-0021）、上界 150 兜底；`max < min` 只在两值都非 0 时判。"""

    enabled = serializers.BooleanField(required=False)
    block_claim_without_cert = serializers.BooleanField(required=False)
    require_cert_for_settlement = serializers.BooleanField(required=False)
    min_age = serializers.IntegerField(required=False, min_value=0, max_value=150)
    max_age = serializers.IntegerField(required=False, min_value=0, max_value=150)

    def validate(self, attrs):
        """跨字段判据：两个年龄**都非 0** 且 `max < min` ⇒ 中文人话（spec §10.1 硬约束）。判据必须看
        **合并后**的值（本次传的 ∪ 库里现值）——只传 `max_age` 的 PATCH 拿 `attrs` 直接比会漏判；
        实例由视图经 `context["instance"]` 传入，没有实例时只判本次传的键。"""
        instance = self.context.get("instance")
        min_age = attrs.get("min_age", getattr(instance, "min_age", 0))
        max_age = attrs.get("max_age", getattr(instance, "max_age", 0))
        # ⚠️ `!= 0` 是**取值判据**（`0` = 不限制），⛔ 不是真值判断的偷懒写法：
        # 两侧都要显式判「非 0」才构成一个真实区间。
        if min_age != 0 and max_age != 0 and max_age < min_age:
            raise serializers.ValidationError(
                {"max_age": "最大年龄不能小于最小年龄（填 0 表示不限制）"}
            )
        return attrs


class RealNamePackageCreateSerializer(serializers.Serializer):
    """实名**资源包**录入的 `POST` 载荷（T3，spec §2.4）。本序列化器只做**类型 / 形状**层（E-0010）；
    领域判据（total ≥ 1、key 非空白、expires_at 解析、key_id 唯一）**全部**在 `realname.package_admin.add_package`
    （与命令同一出处），端点把 `PackageAdminError` 翻成 200 + code 1000。⛔ `key_secret` 只进不出。"""

    key_id = serializers.CharField(max_length=64)
    key_secret = serializers.CharField(max_length=128)
    total_quota = serializers.IntegerField()
    expires_at = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=64
    )
    remark = serializers.CharField(required=False, allow_blank=True, max_length=120)

    def validate_expires_at(self, value):
        # ⛔ **空串不在这里归一成 `None`**：显式给了却没值要**响亮拒绝**（service 的
        # `parse_expires_at` 对空串报错）——静默当「不过期」正是命令 T2 挡的那类事故。
        # 「没给」这一档由 `required=False` 表达（键不在 validated_data 里）。
        return value
