"""账号的输出形状：列表与详情共用，输出里不含密码形态的键。列表脱敏、详情全量，用 `mask_pii=True` 切换——默认全量是刻意的：漏传时偏松、看得见，默认脱敏会让详情静默少字段；列表脱敏不是安全边界，不持限制 flag 的人仍能从详情读到全量。
限制型 flag 是另一层：`viewer` 持 `restrict_view_id_card` / `restrict_view_commission_params` 时，详情和写响应也会被遮；两层判据各自独立。
"""
from apps.accounts import account_flags
from apps.accounts.display import user_display_name
from apps.accounts.models import StaffProfile
from apps.core.permissions import is_tenant_admin

#: 在列表响应里需要裁剪的字段（spec §5.3 那张表的三个字段）。
#: 用常量而不是在三处硬编码，是为了让「新增 PII 字段」这件事在 diff 里看得见。
_PII_FIELDS = ("id_card_no", "bank_card_no", "alipay_account")

#: 提成数值参数三列（对标 1281 的脱敏对象）。它们是 `Decimal` / 小整数枚举，
#: 置 `None` 而不是字符串 `"***"`——塞字符串会破前端类型（与第 1 批价格脱敏同口径）。
COMMISSION_PARAM_FIELDS = ("base_salary", "commission_mode", "commission_value")

#: 在 `viewer` 对象上缓存「这次要不要遮那两个字段」的属性名（见 `_viewer_masks`）。
_MASK_CACHE_ATTR = "_account_flag_masks"


def _viewer_masks(viewer) -> tuple[bool, bool]:
    """`(遮身份证?, 遮提成参数?)`，按 `viewer` 对象缓存。
    列表逐行调用、两个可见性判定各走一条查询，不缓存时列表查询计数随行数涨。缓存只活在这个对象上：会话认证每次请求重新取用户，下一次请求不会复用；跨请求复用同一个 user 对象时中途改授权不会立刻反映（调用点都传 `request.user`）。
    """
    cached = getattr(viewer, _MASK_CACHE_ATTR, None)
    if cached is None:
        cached = (
            not account_flags.id_card_visible(viewer),
            not account_flags.commission_params_visible(viewer),
        )
        try:
            setattr(viewer, _MASK_CACHE_ATTR, cached)
        except AttributeError:
            # `viewer` 不接受自定义属性时退化成「每次重算」：多查询，结论不变。
            pass
    return cached


def _mask_pii(value: str) -> str:
    """保留前 3 后 2，中间用 `*` 遮住。空串原样返回（脱敏不得把「没填」变成「填了个怪东西」）；长度 ≥ 6 返回 `前3 + "***" + 后2`；长度 ≤ 5 **整串**遮成 `"***"`。
    中间固定三个 `*`、不按原长度展开：从 `*` 的个数反推出被遮住的位数，等于把「卡号多长」漏出去；短值整串遮是同一条理由的延伸——「保留前 3 后 2」在 4 位值上会退化成原样输出，那比不脱敏更糟（看着像脱敏了）。
    """
    if not value:
        return value
    if len(value) <= 5:
        return "***"
    return f"{value[:3]}***{value[-2:]}"


def account_payload(user, *, mask_pii: bool = False, viewer=None) -> dict:
    """账号的对外形状。`mask_pii=True` 时裁剪列表上的证件号和账号。
    `viewer` 是谁在看；两个限制 flag 的失败形态是字段被遮，列表、详情、写响应都走这里，不给「改完自己回读」开例外。默认 `viewer=None` 不施加限制：漏传时偏松、看得见。判定经 `_viewer_masks` 按 viewer 缓存，避免列表每行再打两条查询。
    """
    profile = getattr(user, "staff_profile", None)
    if viewer is None:
        hide_id_card = hide_commission = False
    else:
        hide_id_card, hide_commission = _viewer_masks(viewer)
    return {
        "id": user.pk,
        "username": user.get_username(),
        "display_name": user.first_name or "",
        "is_active": user.is_active,
        # 统一判据：租户平面认「系统管理员」职位，平台平面仍看超管开关。
        # 不要改回 `bool(user.is_superuser)`，菜单门和准入会各说各话。
        "is_tenant_admin": is_tenant_admin(user),
        # 名称升序取首个，而不是 `groups.values_list(...).first()`：后者的顺序由数据库返回顺序决定（`auth.Group.Meta` 不带 `ordering`），同一账号持多个 Group 时详情里的 `role` 会随查询计划变化，定成名称升序让取值可复现。同时 `groups.all()` 走列表的 `prefetch_related("groups")` 缓存，`values_list()` 不会——后者会给列表页**每行**多打一条查询。
        "role": _first_role_name(user),
        # 走 `profile.games.all()` 而不是 `games.values_list("id")`：后者**不会**用 `prefetch_related("staff_profile__games")` 的缓存，而是每行再打一条查询（`values_list` 是新的 QuerySet，不读 `_prefetched_objects_cache`）。实测：6 行列表从 18 条查询涨到 28 条，其中 5 条是这里的逐行 games 查询。
        "games": [game.pk for game in profile.games.all()] if profile else [],
        "last_login": user.last_login,
        "date_joined": user.date_joined,
        # 档案字段。⚠️ `auth.User` 没有档案行时（例如 `seed_tenant` 灌的 `admin`）
        # 下面这批键**整个不出现**，不是「给出模型默认值」——消费方要按可选处理。
        **(_profile_fields(
            profile,
            mask_pii=mask_pii,
            hide_id_card=hide_id_card,
            hide_commission=hide_commission,
        )),
    }


def _first_role_name(user):
    """账号的职位名；持多个 Group 时取名称升序的第一个（口径见 `account_payload`）。"""
    names = sorted(group.name for group in user.groups.all())
    return names[0] if names else None


def _profile_fields(
    profile: StaffProfile | None, *, mask_pii: bool, hide_id_card=False, hide_commission=False
) -> dict:
    if profile is None:
        return {}
    fields = {
        "id_card_no": profile.id_card_no,
        "business_type": profile.business_type,
        "member_type": profile.member_type,
        "is_studio": profile.is_studio,
        # 开户人显示名（只读；写入点在 views / selfreg_views，PATCH 盖不到）。
        # `opened_by` 是 SET_NULL：开户人被硬删后该列为 NULL，回空串。
        "createUserName": (
            user_display_name(profile.opened_by) if profile.opened_by_id is not None else ""
        ),
        "base_salary": profile.base_salary,
        "commission_mode": profile.commission_mode,
        "commission_value": profile.commission_value,
        "alipay_account": profile.alipay_account,
        "alipay_name": profile.alipay_name,
        "bank_name": profile.bank_name,
        "bank_card_no": profile.bank_card_no,
        "grab_limit_dl": profile.grab_limit_dl,
        "grab_limit_pw": profile.grab_limit_pw,
        "grab_price_min": profile.grab_price_min,
        "grab_price_max": profile.grab_price_max,
        # 下面这批新键（`order_by` / `settle_forbidden` / `grab_forbid_self_service`
        # 与末尾的 `deleted_at`）**刻意不进** `_PII_FIELDS` / `COMMISSION_PARAM_FIELDS`：
        # 它们既不是 PII、也不是提成参数，不受那两套脱敏开关影响。
        "order_by": profile.order_by,
        "settle_forbidden": profile.settle_forbidden,
        "grab_forbid_self_service": profile.grab_forbid_self_service,
        # 钱⑤（业务方裁定）：读面与上一行同族——档案缺行时整批键不出现，
        # 有档案时一律回真 `bool`（模型 `BooleanField` 的归一化输出）。
        "settle_forbidden": profile.settle_forbidden,
        "live_url": profile.live_url,
        "qq": profile.qq,
        "mobile": profile.mobile,
        "hired_on": profile.hired_on,
        "tag": profile.tag,
        "remark": profile.remark,
        "deposit_amount": profile.deposit_amount,
        # ⭐ 个人押金额度总闸：人员管理页那一列的行内开关读它。只读——写入点只有 `PUT /api/business/takers/<id>/deposit-quota/`（⛔ 不挂进 `AccountWriteSerializer`：那条路的 `PATCH` 会把「开关 + 额度一起存」的口径拆成两套，钱相关的写入点越少越可审）。
        "deposit_quota_enabled": profile.deposit_quota_enabled,
        "deleted_at": profile.deleted_at,
    }
    if mask_pii:
        for field in _PII_FIELDS:
            fields[field] = _mask_pii(fields[field])
    if hide_id_card:
        # 与列表同一套脱敏函数（一处可审的策略）；短值走它自己的整串 `"***"` 分支。
        fields["id_card_no"] = _mask_pii(fields["id_card_no"])
    if hide_commission:
        for field in COMMISSION_PARAM_FIELDS:
            fields[field] = None
    return fields
