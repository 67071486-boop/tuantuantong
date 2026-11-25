"""`GET/PATCH /api/auth/profile` —— 「我的资料」自助读写（spec §5.1 / §5.2）。与 `apps/accounts/views.py::AccountViewSet` **刻意分开**：那一条链是**管账号的人改别人**（准入 `IsAccountManager` + `system_user.*` 节点；
线 D起准入不再要求是「系统管理员」，见 `apps/accounts/permissions.py`），本模块是**改自己**（登录 + `my_profile.edit`）。两者受众不同，合在一起会让两套判据互相污染。
⚠️ `StaffProfile` / `record` / `has_node` 都是**租户层**的东西，而本模块在 `apps/core`（共享层）⇒ 一律**函数/方法内导入**（与 `auth_views.py` 既有做法同款）。⚠️ 校验失败的口径：本仓是 **HTTP 200 + `code=ErrorCode.VALIDATION`**（`apps/core/exceptions.py::envelope_exception_handler`），⛔ 不是 400。"""
from django.db import transaction
from django.http import Http404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.core.storage import (
    ALLOWED_EXTS,
    MAX_USER_MEDIA_BYTES,
    build_user_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_type,
)

#: 自助可改的 7 列（spec §3-D2）。⛔ 加一列 = 放行一个新的自助写面，必须回改 spec。
EDITABLE_FIELDS = (
    "alipay_account", "alipay_name", "bank_name", "bank_card_no",
    "qq", "mobile", "live_url",
)


class MyProfileWriteSerializer(serializers.Serializer):
    """只声明那 7 列。列宽**逐条抄** `apps/accounts/models.py::StaffProfile`；
    `tests/test_my_profile_api.py::SerializerWidthTests` 钉住两处不漂移。"""

    alipay_account = serializers.CharField(max_length=64, required=False, allow_blank=True)
    alipay_name = serializers.CharField(max_length=64, required=False, allow_blank=True)
    bank_name = serializers.CharField(max_length=64, required=False, allow_blank=True)
    bank_card_no = serializers.CharField(max_length=32, required=False, allow_blank=True)
    qq = serializers.CharField(max_length=64, required=False, allow_blank=True)
    mobile = serializers.CharField(max_length=32, required=False, allow_blank=True)
    live_url = serializers.CharField(max_length=255, required=False, allow_blank=True)


def my_profile_payload(user, profile) -> dict:
    """输出形状（spec §5.1）。`profile is None` = 还没有档案行（`seed_tenant` 灌的账号）。"""
    from apps.accounts.payloads import _first_role_name
    # ⚠️ 函数内导入（本模块在 `apps/core` 共享层，租户层模块一律函数/方法内导入，见文件头）。
    # D-0245 §2.1：`is_real_name` 的判据收编为**单一出处** `apps/realname/services.py`，
    # 本处原先那份 `bool(profile and profile.id_card_no)` 内联已改调它（语义一字没改）。
    from apps.realname.services import is_real_name

    return {
        "username": user.get_username(),
        "display_name": user.first_name or "",
        "role": _first_role_name(user) or "",
        "id_card_no": profile.id_card_no if profile else "",
        # 派生字段：前端不再自己判空（一处判据）。
        "is_real_name": is_real_name(profile),
        # ⭐ 实名（spec §10.2）：本租户**是否启用** + **还剩几次**；两个事实都在 `apps/realname` 里判（见 `_realname_summary`），本模块只搬运。
        # ⚠️ 与上面那行 `is_real_name` 是**两件事**：那个只看**档案上有没有号**（管理员手填也算），与「核验过没有 / 功能开没开」无关——spec §2-B4 的「不阻断任何既有操作」靠的就是这条语义一字不改。
        "realname": _realname_summary(),
        # `Decimal` 交给 DRF 编码（与 `/api/accounts/` 同口径）；前端按 number|string 兼容。
        "base_salary": profile.base_salary if profile else None,
        **{name: (getattr(profile, name) if profile else "") for name in EDITABLE_FIELDS},
        "games": [game.pk for game in profile.games.all()] if profile else [],
        # 图片存储线：头像 / 微信收款码的**短时效读链接**。
        # ⚠️ 只给 URL、⛔ 不回对象名（key 是内部标识，前端不需要也不该拼）。
        # ⚠️ 取链接要读存储层 ⇒ 失败时**回落 None**（资料页不该因为对象存储抽风而整页 500）。
        "avatar_url": _media_url(profile, "avatar_key"),
        "wechat_qr_url": _media_url(profile, "wechat_qr_key"),
    }


def _realname_summary() -> dict:
    """本租户的实名开关 + 剩余次数（spec §10.2 的「我的信息」载荷）。⛔ **两个事实都不在本模块算**（`AGENTS.md` §5-E：同一个事实不许两套判据）：- **开关** ⇒ 走实名模块自己的读法 `services.get_or_create_setting()`。「本租户那一行怎么找 / 没有行算不算启用」这两条判据只在它一处（列默认值 `False`由 `apps/realname/models.py::RealNameSetting` 定义）。
    ⛔ 不在本模块另写`RealNameSetting.objects.filter(...)`：那是第二份判据，模型以后加了限定条件时两边会漂。⚠️ 代价如实登记：那是 `get_or_create` ⇒ **首次 `GET /api/auth/profile` 会按默认值建那一行**（与 `GET /api/auth/realname/` 同款行为，本线既有口径）。- **余量** ⇒ 走 `apps/realname/views.py::remaining_quota()`：
    它自己区分「没买过 ⇒ `0`」与「跨 schema 读不到 ⇒ `None`」，并且**只读**（⛔ 不碰计数，改计数器是 `apps/realname/quota.py` 的唯一职责）。⛔ 本函数**不回任何密钥字段**（`key_id` / `key_secret` 一个都不出现，spec §6-3）。⚠️ 两个模块都在**函数内**导入：`apps.realname` 是租户层 app，而本模块在`apps/core`（共享层）——与模块 docstring 里那条约定同款。"""
    from apps.realname import services
    from apps.realname.views import remaining_quota

    setting = services.get_or_create_setting()
    remaining, _msg = remaining_quota()
    return {"enabled": bool(setting.enabled), "remaining": remaining}


def _media_url(profile, field: str):
    """把档案上的对象名签成一个短时效 URL；没有 / 签不出来一律回 `None`。"""
    key = getattr(profile, field, "") if profile else ""
    if not key:
        return None
    try:
        from apps.core.storage import get_storage

        return get_storage().presign_get(key).url
    except Exception:  # noqa: BLE001 —— 存储后端异常不该让「我的资料」整页失败
        return None


class MyProfileView(APIView):
    """我自己的资料。GET = 登录即可；PATCH = 登录 + `my_profile.edit`（spec §5.1/§5.2）。"""

    permission_classes = [IsAuthenticated]

    #: 节点门拒人时的 403 msg。⚠️ **必须显式设**：不给的话 DRF 回的是英文默认句
    #: （`You do not have permission…`），中文界面直接显示它。机制与
    #: `AccountViewSet.NODE_DENIED_MESSAGE` 同款（`gate.message = …`）。
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        """PATCH 另加节点门。
        ⚠️ 平面守卫**放在视图体里**（见 `_plane_guard`），所以这里在 `public` 平面上**不加节点门**——否则 `has_node` 在 public 恒假，请求会先被 403
         挡掉，「public 平面回 404」这条判据就永远测不到（spec §5.6）。⚠️ `has_node` **在方法内导入**：`apps.rbac` 是租户 app，而本模块在 urlconf 载入期就被 import（`apps.core.urls`）——放模块级会让平台层在导入期加载租户层模块（与 `auth_views.py` 的各处函数内导入同一条理由）。"""
        permissions = [IsAuthenticated()]
        if self.request.method == "PATCH" and not on_public_plane():
            from apps.rbac.permissions import has_node

            gate = has_node("my_profile.edit")
            # ⚠️ 只在本视图自己造出来的那个类上设 message（`has_node()` 每次返回新类）。
            gate.message = self.NODE_DENIED_MESSAGE
            permissions.append(gate())
        return permissions

    def _plane_guard(self):
        """平台平面没有 `StaffProfile` / 角色目录 ⇒ 明确 404（而不是 500，spec §5.6）。

        ⚠️ 响应信封走 `apps/core/plane.py::plane_not_found_response`（与
        `ChangePasswordView.post` 同一出处）：msg / 状态码只在一处定义，两处不会漂移。
        """
        if on_public_plane():
            return plane_not_found_response()
        return None

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        profile = getattr(request.user, "staff_profile", None)
        return api_response({"profile": my_profile_payload(request.user, profile)})

    def _validate_only_editable(self, data):
        """请求体里出现**任何**非 7 列键 ⇒ 一句可读的校验失败（含全部越界键名）。为什么不用 DRF 的「忽略未知键」默认行为：那会把 `{"username": "x"}` **静默丢弃**、回成功——调用方以为改成功了。判据是「**这类请求**不再静默」，不是「这个字段不再报错」。⚠️ 报成**一句**而不是字段级 dict：
        错误信封只带 `_first_message` 那一条（`apps/core/exceptions.py`），字段级 dict 会让调用方只看到第一个键。⚠️ 进入本函数前先核**请求体类型**：
        DRF 的 `JSONParser` 只 `json.load` 顶层、不校验类型（`rest_framework/parsers.py::JSONParser.parse`），数组 / 字符串 / 数字都会原样成为 `request.data` ⇒ `.keys()` 抛 `AttributeError` ⇒ **HTTP 500**。这是「非法输入一律 `code=1000`」的形状之一（E-0010 同族），本仓已有同款先例（`apps/business/api/recognition_views.py` 的「请求体必须是对象」）。⛔ 本路由不在 `tests/test_no_route_500.py` 的枚举来源里（它只收`apps.business.urls` / `apps.audit.urls`）⇒ 这条兜底**没有**结构网替它守着。"""
        if not isinstance(data, dict):
            raise serializers.ValidationError("请求体必须是对象")
        extra = sorted(set(data.keys()) - set(EDITABLE_FIELDS))
        if extra:
            raise serializers.ValidationError(
                "这些字段不能通过本接口修改：" + "、".join(extra)
            )

    def patch(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        from apps.accounts.models import StaffProfile
        from apps.audit.actions import ACTION_LOG_TYPES
        from apps.audit.log import record

        self._validate_only_editable(request.data)
        serializer = MyProfileWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        profile, _created = StaffProfile.objects.get_or_create(user=request.user)
        changed = {
            name: value
            for name, value in serializer.validated_data.items()
            if getattr(profile, name) != value
        }
        with transaction.atomic():
            if changed:
                for name, value in changed.items():
                    setattr(profile, name, value)
                profile.save(update_fields=[*changed.keys(), "updated_at"])
                # 操作日志：**一行**、只记「渠道」（对标模板唯一槽位）。
                # ⛔ 不逐字段写多行（模板里没有字段槽位，多行文案完全相同 = 噪声）；
                # ⛔ 不把字段值写进日志（这里有人家的收款账号与卡号）。
                record(
                    log_type=ACTION_LOG_TYPES["user.self_update"],
                    action="user.self_update",
                    actor=request.user.get_username(),
                    actor_id=request.user.pk,
                    target_kind="user",
                    target_id=request.user.pk,
                    target_name=request.user.get_username(),
                    reason="系统网页",
                )
        return api_response({"profile": my_profile_payload(request.user, profile)})


# ── 头像 / 微信收款码（图片存储线，spec §5.2 / §7）──────────────────────────
def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**（spec §6.2）。

    ⛔ 只来自 `connection.schema_name`，**绝不接受请求参数传入的租户标识**（`AGENTS.md` §4）。
    """
    from django.db import connection

    return connection.schema_name


#: 允许的两种「用户媒体」。`kind` 是**路径段**，必须白名单（⛔ 不接受任意字符串）。
MEDIA_KINDS = {
    "avatar": "avatar_key",
    "wechat-qr": "wechat_qr_key",
}


class _MyProfileMediaBase(APIView):
    """两个写端点的公共装配（平面守卫 + `my_profile.edit` 节点门）。
    ⚠️ 平面守卫**在视图体里**、节点门**在 `get_permissions()` 方法体里**——与`MyProfileView` 同款（理由见
    那里的 docstring：public 平面上先放行、再由视图体回 404）。⚠️ `has_node` 方法内导入（`apps.rbac` 是租户 app，本模块在 urlconf 载入期就被 import）。"""

    permission_classes = [IsAuthenticated]
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        permissions = [IsAuthenticated()]
        if not on_public_plane():
            from apps.rbac.permissions import has_node

            gate = has_node("my_profile.edit")
            gate.message = self.NODE_DENIED_MESSAGE
            permissions.append(gate())
        return permissions

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None

    @staticmethod
    def _field(kind: str) -> str:
        field = MEDIA_KINDS.get(kind)
        if field is None:
            raise Http404
        return field


class MyProfileMediaTicketView(_MyProfileMediaBase):
    """`POST /api/auth/profile/media/<kind>/ticket` —— 签发一张只能写**本用户**对象的直传凭证。"""

    def post(self, request, kind):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        field = self._field(kind)
        ext = (request.data.get("ext") or "").lower().lstrip(".")
        if ext not in ALLOWED_EXTS:
            return api_response(code=ErrorCode.VALIDATION, msg="只支持 jpg / jpeg / png")
        key = build_user_key(_tenant_schema(), request.user.pk, kind, ext)
        signed = get_storage().presign_put(key)
        del field  # 只在 claim 时写列：签发了但没传完，列上不该指向一个不存在的对象
        return api_response({"key": key, "upload_url": signed.url, "expires_in": signed.expires_in})


class MyProfileMediaClaimView(_MyProfileMediaBase):
    """`POST /api/auth/profile/media/<kind>` —— HEAD 校验 + 前缀校验 + 魔数校验，然后写列并删旧对象。"""

    def post(self, request, kind):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        field = self._field(kind)
        key = (request.data.get("key") or "").strip()
        if not key:
            return api_response(code=ErrorCode.VALIDATION, msg="缺少 key")
        # 护栏②：对象名必须属于**本租户 + 本用户**（归一化判据；⛔ 不是字符串 startswith）
        expected_prefix = f"{_tenant_schema()}/users/{request.user.pk}/"
        if not key_belongs_to_tenant(key, _tenant_schema()) or not key.startswith(expected_prefix):
            raise PermissionDenied("对象名不属于当前用户")

        storage = get_storage()
        meta = storage.head(key)
        if meta is None:
            return api_response(code=ErrorCode.VALIDATION, msg="文件还没上传成功")
        size, _header_content_type = meta
        # ⚠️ **0 字节对象一律拒收**：`media_blob_views` 的模块 docstring 写着「空图片永远不合法」，但这一跳原先只判 `size > MAX` ⇒ 口径没落到实现上。码与清理路径**照抄**下面那条超限分支（同一个 `ErrorCode.VALIDATION`、同样先删对象）；文案与超限**分开**——0 字节不是「超过 10 MB」，沿用那句会说假话。
        # ⚠️ 它**先于**下面的魔数嗅探：嗅探也能让 0 字节落不了库（读不到魔数 ⇒ 拒），但那条会报「只支持 jpg / jpeg / png」——对空文件是误导。
        if size <= 0:
            storage.delete(key)
            return api_response(code=ErrorCode.VALIDATION, msg="图片无效或为空")
        # ⚠️ 头像 / 收款码的上限是 **10 MB**（业务方裁定），与订单截图的
        # 18 MiB **不是**同一条 ⇒ ⛔ 别把这里改回 `MAX_IMAGE_BYTES`。
        # 这是**权威**那一跳：浏览器直传不经过我们服务器，签发端点拿不到大小。
        if size > MAX_USER_MEDIA_BYTES:
            storage.delete(key)
            return api_response(code=ErrorCode.VALIDATION, msg="图片超过 10 MB")
        sniffed = sniff_image_type(storage, key)
        if sniffed is None:
            storage.delete(key)
            return api_response(code=ErrorCode.VALIDATION, msg="只支持 jpg / jpeg / png")

        from apps.accounts.models import StaffProfile

        profile, _ = StaffProfile.objects.get_or_create(user=request.user)
        old_key = getattr(profile, field, "") or ""
        setattr(profile, field, key)
        profile.save(update_fields=[field])
        # ⚠️ 旧对象在**写库成功之后**才删（反过来的话，写库失败会留下"列指向已删对象"的坏状态）
        if old_key and old_key != key:
            try:
                storage.delete(old_key)
            except Exception:  # noqa: BLE001 —— 旧对象删不掉不该让本次上传失败
                pass
        return api_response({"key": key})