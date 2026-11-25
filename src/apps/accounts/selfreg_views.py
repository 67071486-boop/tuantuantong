"""自助注册链接的三个端点（spec §3，决策 D-0237）：配置 / 链接台账 / 公开注册。⚠️ 前两个（config / invites）是**页面端点**：登录 + 节点门（复用账号管理已有节点 `system_user.add_edit`，⛔ 不新增节点——`catalog.py` 有计数钉子）；`SelfRegisterPublicView` 是**免登录**端点：`authentication_classes=[]` + `AllowAny`（PeerBindView 先例——不发会话所以不需要 CSRF；匿名请求可穿过 `TenantSessionGuardMiddleware`）。全局默认权限类是 fail-closed 的 `IsAuthenticated` ⇒ 公开视图必须**显式** `AllowAny`。
令牌生成 / 哈希 import 复用 `apps/business/peer_sync` 的 `new_token` / `hash_token`（⛔ 不抄第二份实现）；明文只在生成响应 / 注册响应里出现一次，库里只有 sha256。
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.account_numbering import generate_display_name as _generate_display_name
from apps.accounts.choices import AccountSource
from apps.accounts.display import user_display_name
from apps.accounts.models import SelfRegistrationConfig, SelfRegistrationInvite, StaffProfile
from apps.accounts.views import RESET_PASSWORD, _game_names, AccountViewSet
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import roles
from apps.business.models import Game
from apps.business.peer_sync import hash_token, new_token
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

User = get_user_model()

#: 配置 / 链接两个端点的节点门（spec §3.1 / §3.2）。**复用**账号管理那个已有节点
#: ——整页（桌面「自助注册」）的路由门也是它，接口与页面同门。
SELF_REGISTRATION_NODE = "system_user.add_edit"

#: 链接有效期（天）。业务方拍板「过期 7 天」。
SELF_REGISTER_LINK_TTL_DAYS = 7

#: 自助注册的初始密码。值与 `views.RESET_PASSWORD`（重置口令，对标实测「123456」）
#: 同源，按 spec §3.3 的口径在本模块给这个**语义**一个名字；哈希落库在注册事务里。
SELF_REGISTER_INITIAL_PASSWORD = RESET_PASSWORD

#: 自助注册新人的**代练持单上限**（业务方拍定「新注册用户接单上限 3」）。
#: ⚠️ 语义出处 `apps/business/grab_limits.py` 口径 4：`0` = 不限制，所以这里必须是正数；
#: 改这个常量 = 改「新人能同时接几单」，不是展示文案。
SELF_REGISTER_GRAB_LIMIT_DL = 3

#: 公开注册的失败限流（T5：**按链接分桶**，spec §2.6）。窗口 3600 秒；阈值从 settings 读（默认 20），测试用 `override_settings` 调低。桶键 = `self-register-fail:{hash_token(tk)}`，形状坏到拿不出令牌的进共享字面桶 `_MALFORMED_BUCKET`。
#: ⚠️ 与 peer 绑定限流同一条已知边界：计数落在进程内 LocMemCache，多 worker 各算一份 ⇒ 生产实际上限 ≈ 阈值 × worker 数（缓解，不是控制项）。
SELF_REGISTER_WINDOW_SECONDS = 3600

#: 大陆手机号形状（spec §8-2 的取舍：业务方未细说，取常见口径；放宽是一行事）。
SELF_REGISTER_MOBILE_RE = r"^1[3-9]\d{9}$"

#: 失败文案（自有中文、带字段名，E-0074）。逐字即外部契约：测试里写字面副本比对。
SELF_REGISTER_LINK_INVALID_MSG = "链接已失效或已被使用，请联系人事重新获取"
SELF_REGISTER_MOBILE_TAKEN_MSG = "该手机号已注册，请直接登录或联系人事"
SELF_REGISTER_RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"
SELF_REGISTER_EMPTY_POOL_MSG = "请先在上方配置至少一款游戏再生成链接"
SELF_REGISTER_GAME_NOT_IN_POOL_MSG = "game_ids 只能从链接给定的游戏里勾选"

#: 编号前缀的取值形状（spec §2.1：只允许字母；中文 / 数字 / 符号拒）。
SELF_REGISTER_PREFIX_RE = r"^[A-Za-z]{1,5}$"
SELF_REGISTER_PREFIX_INVALID_MSG = "编号前缀只能是 1~5 个英文字母"
SELF_REGISTER_START_INVALID_MSG = "起始编号必须是 ≥ 1 的整数"

#: 「作废」（D-0253）的业务文案（逐字即外部契约：测试里写字面副本比对）。
#: ⛔ 已消费 / 已过期（含已作废）都必须是**明确的一句**，不 500、也不是静默成功。
SELF_REG_REVOKE_USED_MSG = "该链接已被使用，无法作废"
SELF_REG_REVOKE_EXPIRED_MSG = "该链接已过期，无需作废"
SELF_REG_REVOKE_NOT_FOUND_MSG = "链接不存在或已失效"


def _config_payload(config: SelfRegistrationConfig) -> dict:
    """配置端点 GET/PUT 共用的出口形状（一处组装，两条路不漂移）。"""
    return {
        # 按 pk 升序：出口形状确定性，调用方对拍不靠数据库的返回顺序。
        "game_ids": list(config.games.order_by("pk").values_list("pk", flat=True)),
        "forbid_orders_default": config.forbid_orders_default,
        # 编号两列（spec §2.1）：`number_start` 没配时是 `null`（JSON null），前端照实显示。
        "number_prefix": config.number_prefix,
        "number_start": config.number_start,
    }


def _self_register_failure_key(token: str) -> str:
    """限流键（T5，业务方拍板「按链接分桶」，spec §2.6）。
    ⚠️ 维度是**请求者出示的那串令牌的哈希**（`hash_token` 复用 peer_sync 那一份）：不管存在与否 / 是否已用 / 是否过期，桶都按它分——瞎编的令牌有自己的桶，被滥用的链接不拖累同租户其它链接（判据：跨链接隔离）。⛔ 不是 `connection.schema_name`（旧整租户桶已删——一条被滥用的链接会挡住全租户注册）；也⛔ 不是 `request.get_host()`（保留端口，阈值会被端口后缀乘出来）。⚠️ 请求体形状坏到**拿不出令牌** ⇒ 计入共享字面桶 `_MALFORMED_BUCKET`（spec §2.6 第 2 条）。
    """
    if not token:
        return _MALFORMED_BUCKET
    return f"self-register-fail:{hash_token(token)}"


#: 载荷形状错误（tk 缺失 / 非字符串）共用的字面桶（spec §2.6 第 2 条）。
_MALFORMED_BUCKET = "self-register-fail:malformed"


def _count_self_register_failure(key: str) -> None:
    """失败计数（`cache.incr` 在键不存在时会抛，所以 `get` + `set`，与 peer 同写法）。"""
    current = cache.get(key) or 0
    cache.set(key, current + 1, SELF_REGISTER_WINDOW_SECONDS)


def _invite_status(invite: SelfRegistrationInvite, now) -> str:
    """台账行的状态推导（spec §2.3：**不落列**，推导式只住这一处）。⚠️ 判据是 `is not None`，不是真值（E-0021）。
    """
    if invite.consumed_at is not None:
        return "used"
    if now > invite.expires_at:
        return "expired"
    return "unused"


#: 配置 / 链接两端共用的节点门（`has_node()` 每次调用都返回**新类**，所以在模块级
#: 造一次、把拒人文案钉成账号域那句——与 `AccountViewSet.get_permissions` 同一句话，
#: ⛔ 不抄第二份文案）。`IsAuthenticated` 排在最前 ⇒ 匿名请求拿到 401 而不是 403。
_SelfRegGate = has_node(SELF_REGISTRATION_NODE)
_SelfRegGate.message = AccountViewSet.NODE_DENIED_MESSAGE


class _SelfRegNodeGateMixin:
    """配置 / 链接两端共用的准入装配：先登录（匿名 401），再节点门（403）。"""

    permission_classes = [IsAuthenticated, _SelfRegGate]


class SelfRegistrationConfigSerializer(serializers.Serializer):
    """`PUT self-registration/config/` 的载荷（spec §3.1 / §2.1）。
    游戏池 ≥1 且都在本租户；编号两列**可选**（不传 = 按没配存），前缀只允许 `^[A-Za-z]{1,5}$`（中文 / 数字 / 符号拒）、起始编号 ≥ 1。⚠️ 「两列都非空才算配置了编号」**不在这里挡**：只填一列是**合法存态**（判据 3：按未配置处理），注册侧 `_generate_display_name` 统一判。
    """

    game_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        error_messages={"empty": "game_ids 至少要选一款游戏"},
    )
    forbid_orders_default = serializers.BooleanField()
    # `allow_blank=True` 先于正则短路（空串 = 没配前缀，放行）；RegexField(max_length=5)
    # 挡超长，正则挡中文 / 数字 / 符号。
    number_prefix = serializers.RegexField(
        regex=SELF_REGISTER_PREFIX_RE,
        required=False,
        allow_blank=True,
        max_length=5,
        error_messages={
            "invalid": SELF_REGISTER_PREFIX_INVALID_MSG,
            "max_length": SELF_REGISTER_PREFIX_INVALID_MSG,
        },
    )
    number_start = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        error_messages={"min_value": SELF_REGISTER_START_INVALID_MSG},
    )

    def validate_game_ids(self, value):
        # 与 `AccountWriteSerializer.validate_games` 同口径：请求里的 game id 按
        # **当前租户 schema** 解析（连接由 django-tenants 切好），别的租户的游戏查不到 ⇒ 拒。
        found = set(Game.objects.filter(pk__in=value).values_list("pk", flat=True))
        missing = sorted(set(value) - found)
        if missing:
            raise serializers.ValidationError(f"游戏不存在：{missing}")
        return value

    def validate_number_prefix(self, value):
        return value.strip()


class SelfRegisterSerializer(serializers.Serializer):
    """公开注册的字段校验（spec §3.3 第 3 步 / §2.1）：mobile 形状 / 支付宝两栏必填 / 游戏 ≥1。
    ⚠️ **没有 `display_name`**（spec §2.1 删了姓名栏）：显示名由服务端算（编号或支付宝姓名，`_generate_display_name`），⛔ 不接受请求传入。「game_ids ⊆ 链接快照池」**不在这里**：那一步需要先锁到邀请行（spec 第 2 步在第 3 步之前），在视图里判。
    """

    mobile = serializers.RegexField(
        regex=SELF_REGISTER_MOBILE_RE,
        error_messages={"invalid": "手机号格式不对：请填 11 位大陆手机号"},
    )
    alipay_account = serializers.CharField(
        max_length=64,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "required": "alipay_account 不能为空",
            "blank": "alipay_account 不能为空",
        },
    )
    alipay_name = serializers.CharField(
        max_length=64,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "required": "alipay_name 不能为空",
            "blank": "alipay_name 不能为空",
        },
    )
    game_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        error_messages={"empty": "game_ids 至少要选一款游戏"},
    )


class SelfRegistrationConfigView(_SelfRegNodeGateMixin, APIView):
    """`GET/PUT self-registration/config/`（spec §3.1）。"""

    def get(self, request):
        config, _ = SelfRegistrationConfig.objects.get_or_create(pk=1)
        return api_response(data=_config_payload(config))

    def put(self, request):
        serializer = SelfRegistrationConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        config, _ = SelfRegistrationConfig.objects.get_or_create(pk=1)
        # ⚠️ 两笔写入同一个 `transaction.atomic()`：M2M 的 `.set()` 自己开独立事务，
        # 不圈在一起的话，两步之间进程死掉会留下「游戏池换了、默认开关没换」的
        # 半新半旧行（views.py `_soft_delete` 同一条纪律）。
        with transaction.atomic():
            config.games.set(payload["game_ids"])
            config.forbid_orders_default = payload["forbid_orders_default"]
            # 编号两列（spec §2.1）：**可选**字段——缺席 = 按没配存（`get(key, 默认)`，
            # ⛔ 不是 `payload[key]` 直取，PUT 不带这两键的旧调用方不能被炸掉）。
            config.number_prefix = payload.get("number_prefix", "")
            config.number_start = payload.get("number_start", None)
            config.save(
                update_fields=[
                    "forbid_orders_default",
                    "number_prefix",
                    "number_start",
                    "updated_at",
                ]
            )
        return api_response(data=_config_payload(config))


class InviteListCreateView(_SelfRegNodeGateMixin, APIView):
    """`GET/POST self-registration/invites/`（spec §3.2）。
    `POST` = 生成一条一次性链接：明文令牌**只在这里回一次**，库里只有哈希；游戏 / 开关是**生成那一刻**的快照。`GET` = 最新 200 条平铺（有意不分页，spec §8-4）：`data` 就是**行数组本身**，不套 `{items,total}` 分页信封（前端 `api/selfRegistration.ts` 按此形状类型化）；行字段恰好 8 个键，契约值钉在测试里。
    """

    def get(self, request):
        rows = (
            SelfRegistrationInvite.objects
            .select_related("created_by", "consumed_by")
            .prefetch_related("chosen_games")
            .order_by("-id")[:200]
        )
        now = timezone.now()
        items = []
        for invite in rows:
            items.append({
                "id": invite.pk,
                # ⚠️ D-0302：三个时刻键手拼 ⇒ 一律 `local_iso`（+08:00）；
                # 管理端自助注册页截串直显，裸 datetime 经信封渲染成 UTC 串时整列差 8 小时。
                "created_at": local_iso(invite.created_at),
                # 显示名走 `apps/accounts/display` 的口径（昵称优先、空串回落账号），
                # ⛔ 不另抄一份回落逻辑。
                "created_by_name": user_display_name(invite.created_by),
                "status": _invite_status(invite, now),
                "expires_at": local_iso(invite.expires_at),
                "consumed_at": local_iso(invite.consumed_at),
                "consumed_by_name": (
                    user_display_name(invite.consumed_by)
                    if invite.consumed_by is not None else None
                ),
                "chosen_game_names": [g.name for g in invite.chosen_games.all()],
            })
        return api_response(data=items)

    def post(self, request):
        config, _ = SelfRegistrationConfig.objects.get_or_create(pk=1)
        pool = list(config.games.all())
        if not pool:
            raise serializers.ValidationError({"games": SELF_REGISTER_EMPTY_POOL_MSG})
        token = new_token()
        invite = SelfRegistrationInvite.objects.create(
            token_hash=hash_token(token),
            created_by=request.user,
            forbid_orders_default=config.forbid_orders_default,
            expires_at=timezone.now() + timedelta(days=SELF_REGISTER_LINK_TTL_DAYS),
        )
        invite.games.set(pool)
        # ⚠️ D-0302：`local_iso`（+08:00）；生成弹窗按它显示「有效期至」。
        return api_response(data={"token": token, "expires_at": local_iso(invite.expires_at)})


class InviteRevokeView(_SelfRegNodeGateMixin, APIView):
    """`POST self-registration/invites/<pk>/revoke/`（D-0253）：作废一条链接。效果 = **立即过期**：`expires_at` 置为当前时刻——**零迁移**（不新增列 / 状态值，「已作废」在台账上就是「已过期」的一种，`_invite_status` 的推导式一个字不改）。只对**未消费且未过期**的行可作废；已消费 / 已过期（含已作废）⇒ 明确业务文案，不 500、也**不许**把 `expires_at` 二次改写（重复作废读到的是「无需作废」，不是一次假成功——作废时间因此不可漂移）。
    ⚠️ `select_for_update` 圈进 `transaction.atomic()`（E-0022 同族纪律）：公开注册那条路对同一行也是「锁行 → 判有效 → 消费」，必须走同一把行锁，「判未消费 → 置过期」两步才不会插进注册的两步之间（两边谁先提交谁赢，输家按各自文案拒绝）。⚠️ 三档拒绝都是**写前** return（事务里没有可回滚的东西），与 `status_batch` 的 1000 同一形状；留痕只在**成功**的那一跳写。
    """

    def post(self, request, pk):
        now = timezone.now()
        with transaction.atomic():
            # 租户上下文由连接的 `search_path` 提供：别的租户的同号行在这里查不到，
            # 落在下面「链接不存在」那一句上（⛔ 不接受请求参数传租户，AGENTS.md §4）。
            invite = SelfRegistrationInvite.objects.select_for_update().filter(pk=pk).first()
            if invite is None:
                return api_response(
                    data=None, msg=SELF_REG_REVOKE_NOT_FOUND_MSG, code=ErrorCode.VALIDATION
                )
            if invite.consumed_at is not None:
                return api_response(
                    data=None, msg=SELF_REG_REVOKE_USED_MSG, code=ErrorCode.VALIDATION
                )
            if now > invite.expires_at:
                # 已过期（含已作废——「已作废」就是 `expires_at` 已过去的一种）⇒ 无需作废。
                return api_response(
                    data=None, msg=SELF_REG_REVOKE_EXPIRED_MSG, code=ErrorCode.VALIDATION
                )
            invite.expires_at = now
            invite.save(update_fields=["expires_at"])
            # 留痕（D-0253 ④：新写端点必须进 INSTRUMENTED 钉子，动作码 `user.selfreg_revoke`，模板见 `apps/audit/actions.py::USER_SELFREG_REVOKE`）。「是哪一条链接」由 `target_kind` / `target_id` 承载（日志页「相关ID」列）；`reason` 给「生成于」那个时刻——人事在台账上认行用的就是它（纯值，R10）。
            # ⛔ `target_name` 留空：链接行没有「名字」，把生成人 / 生成时间塞进「目标名称」列都是假陈述（FEEDBACK_HANDLE 同一条注释纪律）。
            record(
                log_type=ACTION_LOG_TYPES["user.selfreg_revoke"],
                action="user.selfreg_revoke",
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind="selfreg_invite",
                target_id=invite.pk,
                # ⚠️ D-0302：这句进**审计台账**给人看；`created_at` 从库里读出
                # 是 UTC 时刻，旧写法裸 strftime 出 UTC 墙钟（差 8 小时）⇒ 先 localtime。
                reason=timezone.localtime(invite.created_at).strftime("%Y-%m-%d %H:%M"),
            )
        return api_response(data={
            "id": invite.pk,
            # ⚠️ 刻意**不回** `status`：此刻 `expires_at == now`，`_invite_status` 的 `now > expires_at` 判不出「已过期」（要等下一微秒）——回一个 "unused" 是假陈述，前端也不消费它（状态以台账重取时的推导为准）。⚠️ D-0302：`local_iso`（+08:00）。
            "expires_at": local_iso(invite.expires_at),
        })


class SelfRegisterPublicView(APIView):
    """`GET/POST self-registration/register/`（spec §3.3）：免登录的公开注册端点。
    ⚠️ `authentication_classes` **必须**是空列表（PeerBindView 先例）：装配任何会话认证类都会把 CSRF 与会话语义带进来，而本端点**不发会话**；`AllowAny` 显式声明——全局默认是 fail-closed 的 `IsAuthenticated`，漏写即 401。⚠️ 主流程严格按 spec §3.3 的 1–7 步：**全检查通过后才写**（写前零写、写后无检查），`select_for_update` 锁邀请行，`except IntegrityError` 在 `atomic()` **外面**（E-0022：可预期失败不进补偿路径——Django 要求先离开被污染的事务再处理它）。
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request):
        """注册页首屏：有效给池，无效给 `{valid: false}`（200 信封，页面据此渲染失效态）。
        ⚠️ GET 不计数、不限流：它不写任何数据，失败也只泄露「这串不是有效令牌」——与瞎编 token 的响应无法区分。
        """
        token = request.query_params.get("tk") or ""
        invite = SelfRegistrationInvite.objects.filter(token_hash=hash_token(token)).first()
        if (
            invite is None
            or invite.consumed_at is not None
            or timezone.now() > invite.expires_at
        ):
            return api_response(data={"valid": False})
        tenant = getattr(request, "tenant", None)
        return api_response(data={
            "valid": True,
            "tenant_name": getattr(tenant, "name", "") or "",
            "games": [{"id": g.pk, "name": g.name} for g in invite.games.all()],
            # ⚠️ D-0302：`local_iso`（+08:00）——公开注册页（H5）按串里的
            # 月日时分直显「链接有效期」；旧写法裸 datetime 经信封渲染成 UTC 串，差 8 小时。
            "expires_at": local_iso(invite.expires_at),
        })

    def post(self, request):
        # ── T5：限流**按链接分桶**（spec §2.6）。桶由「请求者出示的 tk」决定，所以
        # 先从请求体里把 tk 拿出来——形状坏到拿不出令牌（非 dict / tk 缺失 / 非字符串）
        # 计入共享字面桶 `_MALFORMED_BUCKET`。
        if not isinstance(request.data, dict):
            key = _MALFORMED_BUCKET
            if (cache.get(key) or 0) >= getattr(settings, "SELF_REGISTER_MAX_FAILURES", 20):
                return api_response(data=None, msg=SELF_REGISTER_RATE_LIMITED_MSG,
                                    code=ErrorCode.VALIDATION)
            _count_self_register_failure(key)
            raise serializers.ValidationError({"tk": SELF_REGISTER_LINK_INVALID_MSG})
        token = request.data.get("tk")
        if not isinstance(token, str) or not token:
            key = _MALFORMED_BUCKET
            if (cache.get(key) or 0) >= getattr(settings, "SELF_REGISTER_MAX_FAILURES", 20):
                return api_response(data=None, msg=SELF_REGISTER_RATE_LIMITED_MSG,
                                    code=ErrorCode.VALIDATION)
            _count_self_register_failure(key)
            raise serializers.ValidationError({"tk": SELF_REGISTER_LINK_INVALID_MSG})
        key = _self_register_failure_key(token)
        if (cache.get(key) or 0) >= getattr(settings, "SELF_REGISTER_MAX_FAILURES", 20):
            return api_response(data=None, msg=SELF_REGISTER_RATE_LIMITED_MSG,
                                code=ErrorCode.VALIDATION)

        try:
            with transaction.atomic():
                invite = (
                    SelfRegistrationInvite.objects
                    .select_for_update()
                    .filter(token_hash=hash_token(token))
                    .first()
                )
                # spec 第 2 步：行不存在 / 已用 / 已过期 ⇒ 同一句失效文案（不给枚举面）。
                if (
                    invite is None
                    or invite.consumed_at is not None
                    or timezone.now() > invite.expires_at
                ):
                    _count_self_register_failure(key)
                    raise serializers.ValidationError({"tk": SELF_REGISTER_LINK_INVALID_MSG})
                # spec 第 3 步：字段校验（放在令牌判定**之后**，与 spec 的步骤顺序一致）。
                serializer = SelfRegisterSerializer(data=request.data)
                if not serializer.is_valid():
                    _count_self_register_failure(key)
                    raise serializers.ValidationError(serializer.errors)
                payload = serializer.validated_data
                mobile = payload["mobile"]
                game_ids = payload["game_ids"]
                # 挑子集（业务方拍板的语义）：注册完的游戏权限 ⊆ 生成链接时的配置池。
                pool_ids = set(invite.games.values_list("pk", flat=True))
                if not set(game_ids) <= pool_ids:
                    _count_self_register_failure(key)
                    raise serializers.ValidationError(
                        {"game_ids": SELF_REGISTER_GAME_NOT_IN_POOL_MSG}
                    )
                # spec 第 4 步：手机号未被占用（唯一约束是并发兜底，见外面的 IntegrityError）。
                if User.objects.filter(username=mobile).exists():
                    _count_self_register_failure(key)
                    raise serializers.ValidationError({"mobile": SELF_REGISTER_MOBILE_TAKEN_MSG})

                # ── spec 第 5 步：全检查通过后才写（写前零写、写后无检查）──
                # 显示名 = 编号或支付宝姓名（spec §2.1）。`_generate_display_name`
                # **锁配置行**：同租户并发注册在这里排队，编号不重号（判据 5）。
                display_name = _generate_display_name(payload["alipay_name"])
                user = User(
                    username=mobile,
                    first_name=display_name,
                    is_staff=False,
                    is_superuser=False,
                )
                user.set_password(SELF_REGISTER_INITIAL_PASSWORD)
                user.save()
                profile = StaffProfile.objects.create(
                    user=user,
                    mobile=mobile,
                    # 开户人 = 生成链接的人事（spec §2.1 的自助注册写路径）。
                    opened_by=invite.created_by,
                    # 记录链接自助注册来源，开户人是发链接的人事。
                    account_source=AccountSource.LINK,
                    grab_forbid_self_service=invite.forbid_orders_default,
                    # 新人的代练持单上限（业务方拍定「新注册用户接单上限 3」）。
                    # ⚠️ `grab_limit_dl=0` 的语义是**不限制**（`grab_limits.py` 口径 4），
                    # 所以「新人 3 单」必须写成字面量 3，⛔ 不能留 0 靠默认值。
                    grab_limit_dl=SELF_REGISTER_GRAB_LIMIT_DL,
                    # 收款支付宝两栏（spec §2.1：都必填）。
                    alipay_account=payload["alipay_account"],
                    alipay_name=payload["alipay_name"],
                )
                profile.games.set(game_ids)
                # 查无同名 Group ⇒ 不翻译的 500：那是租户基础数据坏了，
                # 与 `_persist_account` 的 Group 分支同口径（不是请求错误）。
                group = Group.objects.get(name=roles.WORKER)
                user.groups.set([group])
                invite.consumed_at = timezone.now()
                invite.consumed_by = user
                invite.save(update_fields=["consumed_at", "consumed_by"])
                invite.chosen_games.set(game_ids)
                _record_self_registered(invite, user, game_ids)
        except IntegrityError:
            # 并发同手机号撞 `auth_user.username` 唯一约束的兜底（spec 第 6 步）：
            # code 1001；同事务已整体回滚 ⇒ 令牌未被消费，链接仍可用。
            _count_self_register_failure(key)
            return api_response(data=None, msg="该手机号已注册", code=ErrorCode.CONFLICT)
        # 初始密码只在成功路径上生成（其实是常量），唯一出口是这里的响应体（spec 第 7 步）。
        # `display_name`（账号名称）也只在响应里出现一次：/m/ 成功页的第二行文案靠它
        # （spec §2.1），库里的落点是 `auth_user.first_name`。
        return api_response(data={
            "username": mobile,
            "display_name": display_name,
            "initial_password": SELF_REGISTER_INITIAL_PASSWORD,
        })


def _record_self_registered(invite: SelfRegistrationInvite, user, game_ids) -> None:
    """自助注册的审计留痕：照 `_record_account_created` 的形状（user.create + 逐游戏一行）。
    ⚠️ actor 落法（spec §3.3 第 5 步的如实登记）：`views._record_account` 一律取 `request.user`——匿名请求下那是 `AnonymousUser`，`actor_id` 为 `None`，`record()` 的 fail-closed 判据（取向 5）会当场抛 `ValueError` ⇒ 它**强依赖登录用户**；按 spec 的预案改用 **`invite.created_by`（生成链接的人事）**当操作人：这条审计回答「这个账号是谁开进来的」，与 `opened_by` 同源。⚠️ `_game_names`（请求里的 id → 游戏名，查不到的跳过）复用 `views` 那一份，不抄第二份。
    """
    created_by = invite.created_by
    actor = created_by.get_username()
    record(
        log_type=ACTION_LOG_TYPES["user.create"],
        action="user.create",
        actor=actor,
        actor_id=created_by.pk,
        target_kind="user",
        target_id=user.pk,
        target_name=user.username,
    )
    for name in _game_names(game_ids):
        record(
            log_type=ACTION_LOG_TYPES["user.game_permission_add"],
            action="user.game_permission_add",
            actor=actor,
            actor_id=created_by.pk,
            target_kind="user",
            target_id=user.pk,
            target_name=user.username,
            game_name=name,
        )
