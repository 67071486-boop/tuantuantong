"""账号端点（spec §5）。准入 = `IsAccountManager`（系统管理员 **或** 持账号管理节点）+ 逐动作节点门。
基类用 `GenericViewSet` 而非 `EnvelopeModelViewSet`：后者带 `DestroyModelMixin` 会把 `DELETE` 注册进 router，而 D3 不暴露硬删（要加回来先改 D3）；响应经 `api_response()` 封口，异常由 `envelope_exception_handler` 负责，不手写信封。
"""
import secrets
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import exceptions as drf_exceptions
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.accounts.admin_mutations import admin_mutation_transaction
from apps.accounts.choices import AccountSource
from apps.accounts.guards import (
    active_admin_count,
    apply_status,
    check_admin_invariant,
    check_admin_target_invariant,
    check_delete_invariant,
    check_role_grant_invariant,
)
from apps.accounts.models import StaffProfile, WxPusherBinding
from apps.accounts.display import user_display_name
from apps.accounts.payloads import _first_role_name, _mask_pii, account_payload
from apps.accounts.permissions import IsAccountManager, is_account_admin
from apps.accounts.selectors import base_queryset, list_accounts
from apps.accounts.serializers import (
    DUPLICATE_USERNAME,
    AccountBatchStatusSerializer,
    AccountFilterSerializer,
    AccountStatusSerializer,
    AccountWriteSerializer,
    RealNameLogFilterSerializer,
    RealNamePackageCreateSerializer,
    RealNameSettingWriteSerializer,
)
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.business.models import Game
from apps.business.roles import TENANT_ADMIN
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane
from apps.rbac.permissions import has_node
from apps.rbac.selectors import role_names
from apps.realname import services
from apps.realname.models import (
    CertMethod,
    CertResult,
    RealNameCertRecord,
    RealNameSetting,
)
from apps.realname.package_admin import (
    PackageAdminError,
    add_package,
    list_packages,
    package_payload,
)
#: 余量读数的**单一出处**（`granted - used`；没有额度行 ⇒ `0`；跨 schema 读不到 ⇒ `None` + 一句人话 `msg`）。
#: ⛔ 不在这里重算一遍——两份实现必然漂移，而「`0` 与 读不到 是两件事」正是那个模块钉死的边界（E-0021）。
#: ⚠️ 直接 import 它的**函数**而不是再包一层：本模块与 `apps.realname.views` 之间没有任何反向 import。
from apps.realname.views import remaining_quota

User = get_user_model()

#: 对标物 `/UserAjax/RefChangePwd` 的固定重置口令（**实测**：按钮 title 原文「重置该用户的登录密码为123456」）。
#: 它是**常量**、不是可配置项：本线的重置语义就是「回到那个已知口令」，与「管理员自定义新口令」
#: （另一条线的工作）区分开。见 spec D5 的代价。
RESET_PASSWORD = "123456"

#: `user.status` / `user.status_batch` 模板里 `{有效}` / `{禁用}` 的字面值。
#: 出处：`docs/RECON-operation-log.md:120`「用户状态（有效↔禁用）」。它是**显示标签**，
#: 不是 `auth_user.is_active` 的布尔值本身（`record()` 只吃字符串列）。
_STATUS_LABELS = {True: "有效", False: "禁用"}

# ── 实名认证线：管理页那几条端点的口径常量 ──────────────────────
#: 对标「人员实名记录与配置」页的分页：`limits: [20]`（spec §10.1）⇒ 参数名是 **`limit`**
#: （⛔ 不是本仓通用的 `page_size`），默认与**上限**都是 20。
REALNAME_LOG_PAGE_SIZE = 20

#: 列表筛选 `status` 的**取值 → `result` 列**映射（对标 `certStatus` 三档文案：`0 全部` /
#: `1 实名失败` / `2 实名通过`；spec §9.1）。⚠️ **`0` 是「全部」这个真实取值** ⇒ 显式映射 +
#: `is not None` 判「传没传」（⛔ 不用真值判断，E-0021）；`None` 只是「这张表里没有这一档」的哨兵。
REALNAME_STATUS_TO_RESULT = {
    "0": None,                    # 全部
    "1": CertResult.FAIL,         # 实名失败
    "2": CertResult.PASS,         # 实名通过
}


class RealNameLogPagination(EnvelopePagination):
    """实名记录列表的分页：`page` / `limit`，默认与上限都是 20（对标 `limits: [20]`）；响应形状仍由
    `core.envelope.paginated` 一处定义。⚠️ 只挂在 `AccountViewSet.realname_pagination_class` 上按 `self.action` 逐请求选用，⛔ 不能换掉 `AccountViewSet.pagination_class`（那会静默改掉账号列表的契约）。
    """

    page_size = REALNAME_LOG_PAGE_SIZE
    page_size_query_param = "limit"
    max_page_size = REALNAME_LOG_PAGE_SIZE


class AccountViewSet(viewsets.GenericViewSet):
    """账号端点：`list` / `retrieve` / `roles`、新增、编辑、启停（`status` / `status_batch`）、
    重置密码、软删 / 恢复与实名管理页那几条。⚠️ 新增的 `@action` **只注册 `POST`**：本类的 405 边界（D3：不暴露硬删）由「基类没有 `DestroyModelMixin`」保证，新动作不得顺手把别的动词带进来。
    """

    permission_classes = [IsAccountManager]

    #: 逐 action 的节点 code 表（③线 spec §4.2；逐条对标出处：`list` 58 / `add_edit` 61 /
    #: `status`+`status_batch` 62 / `reset_password` 102 / `roles` 两页共用 ⇒ any）。⚠️ 字符串必须是
    #: **字面量**（`test_rbac_structure.py` 的 B2 走 AST 取字符串常量）；未登记动作走默认权限类（fail-closed）。
    ACCOUNT_PERMISSIONS = {
        "list": ("system_user.list",),
        "retrieve": ("system_user.add_edit",),
        "create": ("system_user.add_edit",),
        "update": ("system_user.add_edit",),
        "partial_update": ("system_user.add_edit",),
        "roles": ("system_user.list", "system_user.add_edit"),
        "openers": ("system_user.list",),
        "status": ("system_user.change_status",),
        "status_batch": ("system_user.change_status",),
        # 软删 / 恢复 = 一次用户状态变更（与「改有效状态」同类），挂 `system_user.change_status`（62）。
        # ⛔ 不新增权限节点：`apps/rbac/catalog.py` 是对标权限树声明式全表、有 `EXPECTED_NODE_COUNT`
        # 等计数钉子；对标 210 点里本没有「删除系统用户」这个点，挂现有节点已比对标更严。
        "delete": ("system_user.change_status",),
        "restore": ("system_user.change_status",),
        "reset_password": ("system_user.reset_password",),
        # 实名管理页四个动作 + 资源包配置面共用 `system_user.realname_records`(1263)：对标那一页
        # （`/User/RnameCertLogAndSetting`）本来就只有一个权限点。
        # ⛔ 不新增权限节点（`catalog.py` 有 `EXPECTED_NODE_COUNT` 计数钉子）。
        "realname_logs": ("system_user.realname_records",),
        "realname_setting": ("system_user.realname_records",),
        "realname_quota": ("system_user.realname_records",),
        "realname_packages": ("system_user.realname_records",),
        "reset_realname": ("system_user.reset_realname",),
    }

    #: 节点门拒人时 403 信封的 `msg`：让「是哪扇门拒的」可指认——`IsAccountManager` 没定义 `message`
    #: （走 DRF 默认文案），本条是自有字面量，缺节点的已认证非管理员拿到的是本条文案（E-0011：不写绝对句）。
    #: 判据 = `test_rbac_batch2_system_user.py` 对本值的字面量钉子 + 摘节点后两处 `assertEqual(msg, NODE_DENIED_MESSAGE)`。
    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（合取，不替换）：`IsAccountManager` 是「必须管账号」的最低
        门槛，替换会让「不是管账号的人不能碰账号」失效；未登记 action 返回默认类（fail-closed）。顺序沿 `order_views.py::OrderViewSet.get_permissions`（判等用 `type(p) is not`，⛔ `isinstance` 会丢掉其子类）。
        """
        base = [p() for p in self.permission_classes]
        codes = self.ACCOUNT_PERMISSIONS.get(self.action)
        if not codes:
            return base
        gate = has_node(*codes)
        # ⚠️ 只在**本视图自己造出来的那一个类**上设 message（`has_node()` 每次都返回新类）
        gate.message = self.NODE_DENIED_MESSAGE
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), gate(), *others]

    pagination_class = EnvelopePagination

    #: 实名记录列表那一档的分页（`page` / `limit`，默认 20）；按 `self.action` 逐请求选用（见
    #: `get_pagination`）。⚠️ 直接换类属性 `pagination_class` 会静默改掉账号列表的契约
    #: （`api/accounts.ts` 按 `page` / `page_size` 发）。
    realname_pagination_class = RealNameLogPagination

    def get_pagination(self):
        """本请求该用哪份分页实现。⚠️ 必须同一个实例贯穿 `paginate_queryset` 与 `get_paginated_response`：
        DRF 把切好的那一页挂在**实例**的 `self.page` 上，响应阶段另 `new` 一个 ⇒ `AttributeError`（本类实测踩过这一版）。
        """
        if self.action == "realname_logs":
            return self.realname_pagination_class()
        return self.pagination_class()

    def paginate_queryset(self, queryset):
        """用 `get_pagination()` 选中的那份实现切页（其余动作走 DRF 的原路径）。⚠️ 不覆写方法、改在请求
        中途换类属性 `self.pagination_class = …` 也不可行：那会影响同一进程里并发的别的请求。
        """
        paginator = self.get_pagination()
        if paginator is None:
            return None
        self._paginator = paginator
        page = paginator.paginate_queryset(queryset, self.request, view=self)
        if page is None:
            # 该分页实现决定不分页 ⇒ 响应阶段回到 `super()`（DRF 会回裸列表）。
            self._paginator = None
        return page

    def get_paginated_response(self, data):
        """用同一个分页实例构造响应（形状仍由 `core.envelope.paginated` 一处定义）。"""
        paginator = getattr(self, "_paginator", None)
        if paginator is None:
            return super().get_paginated_response(data)
        return paginator.get_paginated_response(data)

    def get_queryset(self):
        return base_queryset()

    def handle_exception(self, exc):
        """把「本租户零活跃管理员」的 403 翻成**可区分**的 1003（spec §6.2.3；业务失败 ⛔ 不是放宽权限）。落点必须在
        钩子：零管理员时 `permission_denied()` 在任何视图方法之前就抛、权限类又不能返回响应；平台平面必须先挡（public 无租户 ⇒ `active_admin_count()` 恒 0，会误翻干净 403）。判据 = `tests/test_account_admin_predicate.py::test_account_endpoint_on_the_public_plane_is_a_clean_403`。
        """
        if (
            isinstance(exc, drf_exceptions.PermissionDenied)
            and getattr(self.request.user, "is_authenticated", False)
            and not on_public_plane()
            and not is_account_admin(self.request.user)
            and active_admin_count() == 0
        ):
            response = api_response(
                data=None,
                msg="本租户当前没有可用的管理员，请联系平台处理",
                code=ErrorCode.LAST_ADMIN,
            )
            # 与 DRF 自己的 `handle_exception` 同形状：这条响应代表一个异常。
            response.exception = True
            return response
        return super().handle_exception(exc)

    # ── #1 列表 ────────────────────────────────────────────────────
    def list(self, request):
        # ⚠️ 必须先转**普通 dict** 再过序列化器：`QueryDict` 会让 DRF 按 HTML 表单解析，缺席布尔字段按「未勾选
        # checkbox」处理 ⇒ 没传 `?is_active` 时凭空多出 `is_active=False`、列表被静默滤成只看停用（实测 total 0）。
        # ⚠️ 空串按「没传」：前端清空下拉会送 `?is_active=` / `?role=`，不过滤会被字段校验拒成 1000（同订单列表口径）。
        raw = {k: v for k, v in request.query_params.items() if v != ""}
        params = AccountFilterSerializer(data=raw)
        # `raise_exception=True` 不是「响亮地 500」：DRF 的 `ValidationError` 由
        # `envelope_exception_handler` 翻成 **200 + code 1000**（E-0010 的口径）。
        # 不写这一步，`?is_active=maybe` 会带着字符串进 `filter()` 抛 `ValueError` → 500。
        params.is_valid(raise_exception=True)
        qs = list_accounts(**params.validated_data)
        # 人员管理列表按 `-id` 最新在前（业务方拍板「子账户排序按照最新的在上面」；分页稳定性随 pk）。
        # ⚠️ 只改这一条取数路径：`base_queryset` 的 `order_by("id")` 是大厅候选 / 统计等别的调用方
        # 也在用的确定性给定，⛔ 不在那里改。
        qs = qs.order_by("-id")
        page = self.paginate_queryset(qs)
        # R7 / spec §5.3：**列表**脱敏（挡批量采集），详情**在不持限制型 flag 时**返回全量
        # （编辑回填要真值）；持 flag 者见 `payloads.py::account_payload(viewer=)`。
        payload = [account_payload(u, mask_pii=True, viewer=request.user) for u in page]
        return self.get_paginated_response(payload)

    # ── #2 新增 ────────────────────────────────────────────────────
    def create(self, request):
        serializer = AccountWriteSerializer(data=request.data)
        # 非法 → 200 + code 1000；重名 → 1001（spec §5.1 要求它是**可区分**的一条）。
        # 与 `update()` 共用同一个入口，两条写路径的重名码值才不会分叉。
        conflict = _validate_write_serializer(serializer)
        if conflict is not None:
            return conflict
        # ⭐ F1（线 D）：**非系统管理员不得授予「系统管理员」职位**
        # （业务规则，见 `guards.check_role_grant_invariant` 的口径与为什么是 1002）。
        # ⚠️ 判定排在下面那层 `atomic()` **之前** ⇒ 被拒时一行都不落库（连口令都不生成）。
        blocked = check_role_grant_invariant(
            request.user, serializer.validated_data.get("role")
        )
        if blocked is not None:
            return blocked
        try:
            with transaction.atomic():
                # ⭐ T6（spec §2.7，业务方拍板「新增撞上软删账号 = 恢复」）：
                # 同名行是**软删**的 ⇒ 不再新建，改走恢复路径（历史订单 / 绩效的
                # `PROTECT` 外键天然接回同一行）。在用的同名行在序列化器里已被 1001 挡下。
                revive_user = _soft_deleted_user_by_username(
                    serializer.validated_data.get("username")
                )
                if revive_user is not None:
                    # 并发兜底（判据 11-f）：校验（无锁）与这里（行锁）之间，另一条 create 可能已把同一行
                    # 复活——行锁把并发 create 串行化，后到者在锁内复查发现「在用」⇒ 1001（可接受：重复录入）。
                    # ⛔ 不做「第二次也放行」：那会让两条请求的表单值互相覆盖。
                    user = User.objects.select_for_update().get(pk=revive_user.pk)
                    revive_profile = StaffProfile.objects.select_for_update().get(user=user)
                    if revive_profile.deleted_at is None:
                        return _conflict_response()
                    revive_profile.deleted_at = None
                    revive_profile.save(update_fields=["deleted_at", "updated_at"])
                    # 删的时候把人停用了（`delete` 的口径）；恢复即重新入职 ⇒ 一并启用，
                    # 口令在下面重新生成（旧口令作废）。
                    user.is_active = True
                    user.save(update_fields=["is_active"])
                    # 审计（择形回报，spec §2.7）：先补一条 `user.restore`（回收站语义，动作码已有），
                    # 再照常走下面的 `_record_account_created`（本次请求确实是「按表单新增了一个人」）。
                    _record_account(
                        request,
                        "user.restore",
                        target_id=user.pk,
                        target_name=user.username,
                        reason=user.get_username(),
                    )
                    # 表单值覆盖：`_persist_account` 在**既有行**上应用本次 `validated_data`。语义如实登记（spec §2.7
                    # 业务方拍板「重新入职 = 以本次表单为准」）：序列化器**非 partial**，带 `default=` 的字段缺席时**也进**
                    # `validated_data` ⇒ 恢复按**新增默认值**覆盖旧值（有意为之，判据 = `SoftDeleteRecreateReviveTests`）。
                    user = _persist_account(serializer.validated_data, user=user)
                else:
                    user = _persist_account(serializer.validated_data)
                # 开户人（自助注册线，spec §2.1 / D-0237）：手工新增 = 当时的操作者；紧跟档案写、
                # 在**同一个** `atomic()` 里落（⛔ 不开第二个事务）；审计留痕仍由 `_record_account_created`
                # 按「谁在操作」记，与本列互不替代。刚建的档案行已进 `user` 反向缓存，直接取不回查。
                profile = user.staff_profile
                profile.opened_by = request.user
                profile.save(update_fields=["opened_by"])
                # ⚠️ 口令的生成与落库和上面那次写同在**一个** `atomic()`，且生成放在账号行写成功之后：生成挪前 / 出块
                # ⇒ `IntegrityError` 分支白生成一次（`test_integrity_error_path_*`）；口令 `save()` 另开事务 ⇒ 写失败时
                # 库里留下**没有可用口令**的账号（`test_failed_password_write_leaves_no_account_row`）。口令只在成功路径生成。
                password = _generate_initial_password()
                # `set_password()` 只改内存（`django/contrib/auth/base_user.py:96-98`），
                # 必须再 `save()`；漏掉它是静默失效：接口回 200 但账号登不进去。
                user.set_password(password)
                user.save(update_fields=["password"])
                # spec §5.3：新增写 `user.create`，并按选中的游戏**逐条**写
                # `user.game_permission_add`（评审 M4 —— 新增与编辑共用同一个 `games.set()`）。
                # 与上面两次 `save()` 同事务：留痕写失败 ⇒ 整单回滚（spec §7.4）。
                _record_account_created(
                    request, user, serializer.validated_data.get("games")
                )
        except IntegrityError:
            # 并发下唯一约束的兜底（序列化器的存在性检查挡不住竞态）：不接住就是未预期异常 → HTML 500，
            # 宁可给 1001（竞态下别的约束落到这里时文案并不精确，是有意的代价）。
            # ⚠️ `except` 必须在 `atomic()` **外面**：Django 要求先离开被污染的事务再处理它（`Atomic.__exit__`）。
            return _conflict_response()
        return api_response(
            {
                "account": account_payload(user, viewer=request.user),
                # ⚠️ **密码的唯一出口**：只在这里出现一次。详情/列表永不返回它，
                # 也不写日志、不写文件、不存明文列（spec §6.1）。
                "initial_password": password,
            }
        )

    # ── #3 详情 ────────────────────────────────────────────────────
    def retrieve(self, request, pk=None):
        # 路径参数也是请求来源的值（E-0010，与列表筛选参数同口径）：非十进制的 pk 在这里挡下。
        # 守卫的详细口径（为什么是 `isdecimal()` 而不是 `isdigit()`）见 `_lookup_id_error`。
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        # `get_object()` 走 `get_queryset()` → 当前租户 schema 内的 `User`。⚠️ 如实口径（别读成跨租户防线）：两个 schema 的
        # `auth_user` 自增序列**各自从 1 开始**，「B 的 pk」在 A 里很可能有同号行；本端点取到的只是**当前 schema** 那行
        # （queryset 由连接 `search_path` 绑定、不接受请求参数）。跨租户拒绝由 schema 隔离本身保证（`AccountCrossTenantTests`）。
        user = self.get_object()
        return api_response(account_payload(user, viewer=request.user))

    # ── #4 编辑（**不含密码**，见 serializers.SERVER_MANAGED_FIELDS）──
    def update(self, request, pk=None):
        # 与 `retrieve` 同一条守卫：同一个畸形 id 不能 `GET` 回 1000、`PATCH` 回 1002。
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        try:
            # 先取锁，再读目标，后来的请求才能读到前一笔已提交的档案。
            with admin_mutation_transaction():
                user = self.get_object()
                serializer = AccountWriteSerializer(
                    data=request.data,
                    partial=request.method == "PATCH",
                    context={"instance": user},
                )
                # 校验入口与 `create` 共用 ⇒ 重名在这里同样是 1001（裁决 R6 / spec §5.1）；
                # `password` / `is_superuser` / `is_staff` / `is_active` 由序列化器拒 → 1000。
                # `ValidationError` 直接抛出 `atomic()`（回滚，无写）→ 由统一信封翻成 1000。
                conflict = _validate_write_serializer(serializer)
                if conflict is not None:
                    return conflict
                target_role = serializer.validated_data.get("role")
                # ⭐ F1：**非系统管理员不得授予「系统管理员」职位**（口径与 1002 见 `guards.check_role_grant_invariant`）。
                # ⚠️ 排在 `check_admin_invariant` 之前：本规则管「谁有资格动这一格」，那条管「动完还剩几个管理员」，
                # 互不替代；判定在任何写入之前 ⇒ 被拒时零副作用。
                blocked = check_role_grant_invariant(request.user, target_role)
                if blocked is not None:
                    return blocked
                # 「降级」在本线只有「摘掉『系统管理员』Group」一种形态（`is_superuser` 不可经 API 变更）。判据用**变更后**状态
                # （spec §6.2 / R12-②）：没发 `role` ⇒ 职位不变；发了 ⇒ 是否仍选中「系统管理员」。⚠️ 判据统一后**不再有**
                # `or bool(user.is_superuser)`：摘掉 Group 的超管不再是管理员（留着会把真实降级误判成「没变」而放行）。
                will_be_admin = (
                    is_account_admin(user)
                    if target_role is None
                    else target_role == TENANT_ADMIN
                )
                blocked = check_admin_invariant(
                    request.user,
                    user,
                    # 本端点拒收 `is_active`（`SERVER_MANAGED_FIELDS`），所以活跃状态不变。
                    will_be_active=user.is_active,
                    will_be_admin=will_be_admin,
                )
                if blocked is not None:
                    # 判定在写入之前 ⇒ 这次请求一行都不落库（`atomic()` 里没有任何写）。
                    return blocked
                # ⭐ P0-1：**管理员账号只有管理员能动**——只在本次把 `role` 改成**非**管理员时判（=「拿掉身份」那支）；排在
                # `check_admin_invariant` 之后：对最后一个管理员那条已回 1003，既有用例不退化。⛔ 不带 `role` 的编辑不走这里
                # （本规则不是「人事不能碰管理员这一行」，用例 = `AdminTargetInvariantTests::test_an_hr_can_still_edit_a_non_position_field_of_an_admin`）。
                if target_role is not None and not will_be_admin:
                    blocked = check_admin_target_invariant(
                        request.user, user, action="修改其职位"
                    )
                    if blocked is not None:
                        return blocked
                # 派生行（改名 / 职位 / 游戏权限）的差集必须在**写之前**取：
                # `_persist_account` 会就地改 `user`，写完再比就只能比出「没变」。
                before = _account_write_snapshot(user)
                user = _persist_account(serializer.validated_data, user=user)
                _record_account_updated(
                    request, user, serializer.validated_data, before
                )
        except IntegrityError:
            return _conflict_response()
        return api_response(account_payload(user, viewer=request.user))

    def partial_update(self, request, pk=None):
        """`PATCH` 的**路由入口**：router 把 `patch` 映射到 `partial_update` 而非 `update`（`rest_framework/routers.py` 的 `Route.mapping`），只定义 `update` 时 `PATCH` 未注册 ⇒ **405**；是否部分更新由 `update()` 按 `request.method` 判断。
        ⚠️ 本方法有**两条路由**（改账号资料 / 改实名配置），分派判据是 `self.action`（`url_path` 决定路由、它决定权限门，两扇门不会串）；两支**共用**动作码 `user.update`（改的都是子账号资料列；⛔ 新增第三条码会让 `ACTION_TEMPLATES` 条数与 `EXPECTED_ACTIONS` / `EXPECTED_LOG_TYPES` 两张手写清单一起变）。
        """
        if self.action == "realname_setting":
            return self.realname_setting(request)
        return self.update(request, pk=pk)

    # ── #8 职位下拉 ────────────────────────────────────────────────
    @action(detail=False, methods=["get"], url_path="openers")
    def openers(self, request):
        query = serializers.CharField(required=False, allow_blank=True, max_length=64).run_validation(
            request.query_params.get("q", "")
        )
        page = serializers.IntegerField(min_value=1, max_value=92233720368547758).run_validation(request.query_params.get("page", 1))
        ids = StaffProfile.objects.exclude(opened_by_id=None).values_list("opened_by_id", flat=True).distinct()
        users = User.objects.filter(pk__in=ids).order_by("id")
        if query:
            users = users.filter(Q(first_name__icontains=query) | Q(username__icontains=query))
        count = users.count()
        start = (page - 1) * 100
        return api_response(data={"items": [{"id": u.pk, "name": user_display_name(u), "username": u.get_username()} for u in users[start:start + 100]], "total": count, "page": page})

    @action(detail=False, methods=["get"], url_path="roles")
    def roles(self, request):
        """职位下拉 = **当前租户的全部角色**（内置 + 自建；业务方裁定「列全部角色」）——取值域是租户 `Role` 表，`DEFAULT_ROLES` 只用于租户供给；⛔ 别在这里写死内置角色数（判据 = `test_account_admin_predicate.py::AccountManagerPredicateTests::test_the_roles_dropdown_offers_the_hr_role_from_the_data`）。权限判据不变：`(system_user.list, system_user.add_edit)` 的 any（D-0064）；响应形状仍为 `string[]`，前端零结构改动。
        """
        return api_response(role_names())

    # ── #5 单个启停（幂等 set）────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="status")
    def status(self, request, pk=None):
        """按请求体设置单个账号的启停状态。"""
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        payload = AccountStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = self.get_object()
        with admin_mutation_transaction():
            return self._change_status(
                request, [user], payload.validated_data["is_active"], batch=False
            )

    # ── #6 批量启停（幂等 set）────────────────────────────────────
    @action(detail=False, methods=["post"], url_path="status")
    def status_batch(self, request):
        payload = AccountBatchStatusSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        ids = set(payload.validated_data["ids"])
        with admin_mutation_transaction():
            # 共用排除软删的查询，只锁账号行，避开外连接的可空档案行。
            users = list(
                base_queryset().select_for_update(of=("self",)).filter(pk__in=ids)
            )
            if len(users) != len(ids):
                return api_response(data=None, msg="存在不存在的账号", code=ErrorCode.VALIDATION)
            return self._change_status(
                request, users, payload.validated_data["is_active"], batch=True
            )

    @staticmethod
    def _change_status(request, users, is_active, *, batch):
        """在同一事务中写状态与审计，审计只记录确实变化的账号。"""
        changed = [user for user in users if user.is_active != is_active]
        response = apply_status(request.user, users, is_active)
        if response.data["code"] != ErrorCode.OK or not changed:
            return response
        if batch:
            _record_account(
                request,
                "user.status_batch",
                from_value=len(changed),
                to_value=_STATUS_LABELS[is_active],
            )
        else:
            user = changed[0]
            _record_account(
                request,
                "user.status",
                target_id=user.pk,
                target_name=user.username,
                from_value=_STATUS_LABELS[not is_active],
                to_value=_STATUS_LABELS[is_active],
            )
        return response

    # ── #7 重置密码 ────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """把目标账号的口令重置为固定常量 `RESET_PASSWORD`。⚠️ **刻意不调 `validate_password`**（spec §6.1）：固定口令必然过不了强度校验器、这里的语义就是「回到那个已知口令」，补进来会让本端点恒 1000；管理命令 `set_tenant_password.py` 收的是自定义口令、走另一条规则（它调校验器），别互相「对齐」。⚠️ `set_password()` 只改内存（`django/contrib/auth/base_user.py:96-98`），漏掉 `save()` 是**静默失效**（回 200 但旧口令登得进）；已登录会话随口令哈希（`_auth_user_hash`）比对失效而 flush ⇒ 重置**本身就是**踢登录态的落地机制，⛔ 不手工删 `django_session` 行（`test_reset_invalidates_the_targets_existing_session` 断言的也只是「登录态没了」）。
        响应不回显口令（spec §6.1：它只出现在新增端点那一次）；路径参数走与 `retrieve` / `update` 同一条守卫（否则畸形 pk 分叉：1000 vs 1002）。
        """
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        user = self.get_object()
        # ⭐ P0-1：**管理员账号只有管理员能动**。这条路径上没有既有不变式可依赖（重置口令不影响
        # 「还剩几个管理员」），它是唯一的门：不接的话，持 `system_user.reset_password` 的人事能把
        # 管理员口令改成固定 `123456` 再登录（评审实测的绕过路径）。判定在任何写入之前 ⇒ 零副作用。
        blocked = check_admin_target_invariant(
            request.user, user, action="重置其登录密码"
        )
        if blocked is not None:
            return blocked
        with transaction.atomic():
            user.set_password(RESET_PASSWORD)
            user.save(update_fields=["password"])
            # spec §5.3 `user.reset_password`：模板无变量，只记「谁、对谁、做了什么」。
            # 与口令写同事务（spec §7.4）：留痕失败 ⇒ 口令也不落库。
            _record_account(
                request,
                "user.reset_password",
                target_id=user.pk,
                target_name=user_display_name(user),
            )
        return api_response({"id": user.pk})

    # ── #9 软删 / 恢复（业务方要「删除用户」能用，而硬删做不了）────
    @action(detail=True, methods=["post"], url_path="delete")
    def delete(self, request, pk=None):
        """把账号**软删**：写 `staff_profile.deleted_at` + 置 `is_active=False`。不是硬删因为 `Performance.user` / `Order.sales_user` 都是 `on_delete=PROTECT`（删用户不得带走历史订单），业务方要这颗按钮能用 ⇒ 可恢复的软删；**D3「不暴露硬删」仍成立**（仍无 `DestroyModelMixin`，`DELETE` 405）。
        两笔写入同一 `atomic()`（只写一半 =「删了还能登录」的矛盾行）；幂等：已删再删回成功但 `deleted_at` 不改写、不留痕。守卫（advisory 锁后判定）：删自己 1004、删到零活跃管理员 1003、不存在 1002、非管理员删管理员 1002（P0-1，排在最后 ⇒ 最后一个管理员仍是 1003）。
        """
        # ⛔ **必须是本方法的第一条语句**（实测踩出来的，别往下挪）：`@action(methods=["post"])` 只决定注册映射，而 `APIView.dispatch` 按 `getattr(self, request.method.lower())` 找处理器、本方法恰好叫 `delete` ⇒ 每条 accounts 路由的 `DELETE` 都命中它；该路径 `self.action` 为 `None` ⇒ 取不到节点门、只剩 `IsAccountManager`（绕过 `system_user.change_status`）。
        # 守卫的位置只决定「pk 缺失 / 畸形的那几条路由回 405 还是回 1000+200」（变异 F/G 证据登记外移）；restore 没这个坑（不是 HTTP 动词）。
        # 判据 = `test_account_soft_delete.py::test_delete_and_restore_are_post_only` + `test_account_api.py::AccountStatusTests::test_delete_is_still_not_exposed_on_the_new_routes`。
        if request.method != "POST":
            return self.http_method_not_allowed(request)
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        # ⚠️ 取对象必须**包含已删**：默认 `get_queryset()` 排除已删 ⇒ `get_object()` 对已删的人
        # 回 404，那样就分不出「已删（幂等成功）」与「不存在（1002）」——两者语义完全不同。
        user = self._object_including_deleted(pk)
        if _deleted_at(user) is not None:
            return api_response({"id": user.pk})  # 幂等：什么都没改，也不留痕
        with admin_mutation_transaction():
            blocked = check_delete_invariant(request.user, user)
            if blocked is not None:
                # 判定在任何写入之前 ⇒ 这里正常 return 不会留下「半个事务」（E-0022）。
                return blocked
            # ⭐ P0-1（线 D）：**管理员账号只有管理员能动**。判定排在上面那条
            # 更具体的规则之后 ⇒ 「删掉最后一个管理员」仍是 1003（既有用例不退化），
            # 本规则只管还有别的管理员兜底的情况。
            blocked = check_admin_target_invariant(
                request.user, user, action="删除其账号"
            )
            if blocked is not None:
                return blocked
            # 没有档案行的人（如 `seed_tenant` 灌的 `admin`）也要能删：给它补一行空档案，
            # 否则 `deleted_at` 无处可写，「删除」对这类账号静默失效。同一个事务内完成。
            profile = StaffProfile.objects.get_or_create(user=user)[0]
            profile.deleted_at = timezone.now()
            profile.save(update_fields=["deleted_at"])
            # 软删的人不该还能登录：两笔写入同事务（brief 改动 3 第 1 条）。
            user.is_active = False
            user.save(update_fields=["is_active"])
            # 软删账号可以按用户名复用，旧的个人通知不能随账号恢复。
            WxPusherBinding.objects.filter(user_id=user.pk).delete()
            _record_account(
                request,
                "user.delete",
                target_id=user.pk,
                target_name=user.username,
                reason=_delete_snapshot(user),
            )
        return api_response({"id": user.pk})

    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, pk=None):
        """把**已软删**的账号恢复：把 `deleted_at` 清成 `None`。⚠️ **「恢复」≠「启用」：碰都不碰 `is_active`**
        ——删时顺手停用了、恢复时不顺手启用，恢复后仍登录不了，这是**有意**的：「恢复」只回答「这条记录还在不在回收站里」。幂等：没删过的再恢复 ⇒ 返回成功、不写不留痕。
        """
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        user = self._object_including_deleted(pk)
        if _deleted_at(user) is None:
            return api_response({"id": user.pk})  # 幂等：没删过，什么都不改
        with transaction.atomic():
            profile = user.staff_profile
            profile.deleted_at = None
            profile.save(update_fields=["deleted_at"])
            _record_account(
                request,
                "user.restore",
                target_id=user.pk,
                target_name=user.username,
                reason=user.get_username(),
            )
        return api_response({"id": user.pk})

    # ── #10 实名认证管理页（T8；spec §9.1 的后三条 + 重置）──────────
    @action(detail=False, methods=["get"], url_path="realname-logs")
    def realname_logs(self, request):
        """人员实名记录列表（对标「人员实名记录与配置」页表格；持 1263）。入参 `sdate`/`edate`、`status`（0 全部 / 1 实名失败 / 2 实名通过）、`key1`/`key2`/`key3`（用户名 / 姓名 / 身份证号包含）+ `page`/`limit`。⚠️ 参数名是 `status`：对标的**取数参数**如此、控件名才是 `certStatus`（证据边界见 `docs/JIDANPU-REFERENCE.md:1078`，⛔ 别写成 `cert_status`）；`status=0` 是「全部」⇒ `is not None` + 显式映射（E-0021）。
        `id_card_no` 不完整展示（对标列原话）：脱敏复用 `_mask_pii`（前 3 后 2，⛔ 不另写一份）；⛔ 响应不含 `key_id`/`key_secret`（spec §6-3）。
        """
        raw = {k: v for k, v in request.query_params.items() if v != ""}
        params = RealNameLogFilterSerializer(data=raw)
        # `raise_exception=True` ⇒ DRF 的 `ValidationError` 由 `envelope_exception_handler`
        # 翻成 **HTTP 200 + code 1000**（本仓口径，⛔ 不是 400；与 `list` 同一条）。
        params.is_valid(raise_exception=True)
        qs = _realname_log_queryset(params.validated_data)
        page = self.paginate_queryset(qs)
        payload = [_realname_log_payload(row) for row in page]
        return self.get_paginated_response(payload)

    @action(detail=False, methods=["get", "patch"], url_path="realname-setting")
    def realname_setting(self, request):
        """实名四项配置的读 / 写（对标「实名认证相关设置」弹窗；持 1263）。GET 回填（每租户一行）；PATCH 是部分更新：**未传的键一律保持原值**（`if "enabled" in data`，⛔ 不是 `data.get("enabled", False)`——那会把「没传」当「传了 False」，只改 `min_age` 的保存会静默关掉开关，`D-0208` 同款坑）；`min_age`/`max_age` 的 0 = 不限制（判据 `is not None`，E-0021）。
        两开关两态（按实写）：`block_claim_without_cert` **已实装**（D-0245，接单闸 `realname.services.ensure_can_claim` 与指派闸 `order_writes.assign_order` 在读）；`require_cert_for_settlement` **仍只存不拦**（业务方拍板不做，⛔ 不许顺手实装），`enabled` 只被核验端点读。⛔ 响应绝不含 `key_id`/`key_secret`（那两个字段压根不在这张表上）。
        """
        if request.method == "PATCH":
            return self._realname_setting_write(request)
        setting = services.get_or_create_setting()
        return api_response(_realname_setting_payload(setting))

    def _realname_setting_row(self):
        """本租户那一行设置，**没有就回 `None`（⛔ 不建行）**。与 `services.get_or_create_setting()` 的分工：
        那个会**建**行（读路径要它），本方法**只读**——写路径拿它当「库里现值」喂跨字段校验，被拒请求零副作用。
        """
        return RealNameSetting.objects.filter(tenant_schema=connection.schema_name).first()

    def _realname_setting_write(self, request):
        """`PATCH /api/accounts/realname-setting/` 的实现（口径见 `realname_setting`）。PATCH 部分更新 =
        `partial=True` + 逐键 `in validated_data`（未传的键原值不动）；四个写 + 一次留痕在同一个 `transaction.atomic()` 里（spec §7.4：留痕写失败 ⇒ 配置也不落库）。
        """
        # ⚠️ **校验必须在「取 / 建那一行」之前全部走完**（本方法最容易踩的坑）：`get_or_create_setting()` 是一次写，而跨字段
        # 判据（`max < min`）要看合并后的值。解法 = 给序列化器一个**未保存的**原型行（列默认值就在模型那一处）当 `instance`
        # ⇒ 校验读到「库里现值或默认值」但不碰数据库 ⇒ 一次被拒的 PATCH **不留任何行**（否则是悄悄建了一行默认配置）。
        existing = self._realname_setting_row()
        serializer = RealNameSettingWriteSerializer(
            data=request.data,
            partial=True,
            context={"instance": existing or RealNameSetting()},
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        with transaction.atomic():
            setting = services.get_or_create_setting()
            if "enabled" in data:
                setting.enabled = data["enabled"]
            if "block_claim_without_cert" in data:
                setting.block_claim_without_cert = data["block_claim_without_cert"]
            if "require_cert_for_settlement" in data:
                setting.require_cert_for_settlement = data["require_cert_for_settlement"]
            if "min_age" in data:
                setting.min_age = data["min_age"]
            if "max_age" in data:
                setting.max_age = data["max_age"]
            setting.save()
            # 留痕：改的是**本租户那一行配置**（没有具体子账号）⇒ `target_kind="user"` 下
            # 不带 `target_id` / `target_name`（与 `user.update` 那些逐账号的动作区分开）。
            # ⚠️ 动作码与「改账号资料」共用 `user.update`，理由见 `partial_update` 的 docstring。
            _record_account(request, "user.update")
        return api_response(_realname_setting_payload(setting))

    @action(detail=False, methods=["get"], url_path="realname-quota")
    def realname_quota(self, request):
        """本租户剩余核验次数（供「购买实名认证套餐包(当前余量 N 次)」按钮文案；持 1263）。读数唯一出处 = `apps/realname/views.py::remaining_quota`（`granted - used`；没有额度行 ⇒ `0`；读不到 ⇒ `None` + 人话 `msg`）；⛔ 不返回任何密钥字段（`key_id` / `key_secret` 一个字节都不出）。
        """
        remaining, msg = remaining_quota()
        return api_response({"remaining": remaining}, msg=msg)

    @action(detail=False, methods=["get", "post"], url_path="realname-packages")
    def realname_packages(self, request):
        """实名**资源包**的配置面（spec §2.4；持 1263）。GET = 掩码列表（`list_packages()` 形状原样出）；POST = 录入 `{key_id, key_secret, total_quota, expires_at?, remark?}`，校验**照命令**（`package_admin.add_package` 单一出处 ⛔ 两份实现），失败信封 = 200 + code 1000 + 命令既有文案。⛔ **`key_secret` 一个字节不出**：写进库那一次是它唯一的存在方式（判据 = `tests/test_realname_package_admin_api.py` 对照**库里那一行**断言）。
        写的是**平台表**（public schema）；留痕口径 = **刻意不记操作日志**（登记见 `tests/test_operation_log_ledger.py::DELIBERATELY_NOT_LOGGED`：平台资源可停用可重录、模板体系无对标出处）。
        """
        if request.method == "POST":
            serializer = RealNamePackageCreateSerializer(data=request.data)
            # 非法 → 200 + code 1000（E-0010 的口径；信封异常处理器翻的）。
            serializer.is_valid(raise_exception=True)
            data = serializer.validated_data
            try:
                pkg = add_package(
                    key_id=data["key_id"],
                    key_secret=data["key_secret"],
                    total_quota=data["total_quota"],
                    expires_at=data.get("expires_at"),
                    remark=data.get("remark", ""),
                )
            except PackageAdminError as exc:
                # 领域层的**预期**失败 → 1000（E-0010）；文案 = 命令的既有输出（判据 8：
                # 「重复 key-id ⇒ 既有错误文案」，两边同源才不会漂移）。
                raise serializers.ValidationError(str(exc))
            return api_response(_package_row_local(package_payload(pkg)))
        return api_response([_package_row_local(row) for row in list_packages()])

    @action(detail=True, methods=["post"], url_path="reset-realname")
    def reset_realname(self, request, pk=None):
        """管理员重置某个子账号的实名（持 1264）。口径：**不动**任何 `result="pass"` 流水行（**流水只增不改**，改它等于篡改对账凭据），而是 ① 新写一条 `basic` / `fail` / `"管理员重置"` 的流水；② 清空 `StaffProfile.id_card_no`（「未实名」重新成立的判据是「档案上有没有号」，见 `apps/realname/views.py`；只写流水不改档案页面仍显示已实名）。
        两笔写入 + 留痕同一 `atomic()`（spec §7.4）；幂等性如实登记：没实名过也允许重置（「重置是幂等的**终态**」）；⛔ 不删任何流水行。
        """
        invalid = _lookup_id_error(pk)
        if invalid is not None:
            return invalid
        user = self.get_object()
        # 号码必须在**清空之前**取（清了之后就没有可写进流水的号了）。
        # ⚠️ 用 `StaffProfile.objects` 而非 `user.staff_profile`：后者在没档案时抛
        # `RelatedObjectDoesNotExist`，而它**不是** `AttributeError` ⇒ `getattr(…, 默认值)` 接不住。
        profile = StaffProfile.objects.filter(user=user).first()
        id_card_no = (profile.id_card_no if profile is not None else "") or ""
        now = timezone.now()
        with transaction.atomic():
            RealNameCertRecord.objects.create(
                user=user,
                user_name_snapshot=user.username,
                # ⚠️ 姓名列照填 `''`：本动作**不掌握**用户真实姓名（那是核验时用户自己填的，
                # 重置不该替他把姓名也删/编一个）⇒ 留空是如实值，⛔ 不用用户名冒充姓名。
                name="",
                id_card_no=id_card_no,
                cert_method=CertMethod.BASIC,
                result=CertResult.FAIL,
                vendor_message="管理员重置",
                applied_at=now,
                certified_at=now,
            )
            if profile is not None:
                profile.id_card_no = ""
                profile.save(update_fields=["id_card_no"])
            _record_account(
                request,
                "user.realname_reset",
                target_id=user.pk,
                target_name=user.username,
                reason="管理员重置",
            )
        return api_response({"id": user.pk})

    def _object_including_deleted(self, pk):
        """按 pk 取账号，**包含已软删的人**（两个软删动作自己的取对象路径）。与 `get_object()` 的唯一差别：
        后者走 `base_queryset()` **默认排除已删** ⇒ 已删的人会变 404，分不出「已删（幂等成功）」与「不存在（1002）」。
        """
        user = base_queryset(include_deleted=True).filter(pk=pk).first()
        if user is None:
            raise drf_exceptions.NotFound()
        return user


def _realname_log_queryset(params: dict):
    """实名记录列表的筛选（spec §9.1 入参表）。**租户上下文只来自连接**（`AGENTS.md` §4）。`sdate`/`edate` 按 **`applied_at`**（列表「申请时间」列，⛔ 不按 `created_at`）取半开区间 `[sdate 00:00, edate+1d 00:00)`（写成 `<= edate 00:00` 会漏掉当天 00:00 之后的记录）；`status` 走 `REALNAME_STATUS_TO_RESULT` 三档映射（`0` = 全部 ⇒ 不过滤，判 `is not None`）；`key1`/`key2`/`key3` = `user_name_snapshot`/`name`/`id_card_no` 的**包含**匹配。
    """
    qs = RealNameCertRecord.objects.all()
    sdate = params.get("sdate")
    edate = params.get("edate")
    if sdate:
        qs = qs.filter(applied_at__gte=_day_start(sdate))
    if edate:
        qs = qs.filter(applied_at__lt=_day_start(edate) + timedelta(days=1))
    status = params.get("status")
    if status is not None:
        result = REALNAME_STATUS_TO_RESULT[status]
        # ⚠️ `result is None` ⇒ `status="0"`（全部）⇒ **不过滤**。
        # 写成 `if result:` 在这条上等价（两个取值都是非空字符串），但那是真值判断的形状
        # ⇒ 按 §5-E / E-0021 的口径一律用 `is not None`。
        if result is not None:
            qs = qs.filter(result=result)
    if params.get("key1"):
        qs = qs.filter(user_name_snapshot__icontains=params["key1"])
    if params.get("key2"):
        qs = qs.filter(name__icontains=params["key2"])
    if params.get("key3"):
        qs = qs.filter(id_card_no__icontains=params["key3"])
    return qs


def _day_start(value):
    """`YYYY-MM-DD`（字符串**或** `date`）→ 当天 00:00 的**带时区** `datetime`（当前时区）。入参实际已是 `date`（DRF `DateField` 解析后回 `date` 对象，实测），两种都收以稳住类型契约。⚠️ 先 `date` 再 `make_aware`：⛔ 不把裸 `datetime` 直接喂进 ORM——它的解释依赖进程 `TIME_ZONE`，跨时区部署会静默筛出不同的一天、Django 也会为 naive datetime 报警（实测读数与判据 `test_a22b` 登记外移）。
    畸形日期到这里**已过序列化器**（E-0010）⇒ 可假定格式合法；只判 `date` 不判 `datetime`（`.date()` 丢时刻，时区换算在这里不参与运算，不做多余的 `astimezone(...).date()`）。
    """
    if isinstance(value, date):
        parsed = value.date() if isinstance(value, datetime) else value
    else:
        parsed = date.fromisoformat(value)
    return timezone.make_aware(
        datetime.combine(parsed, datetime.min.time()), timezone.get_current_timezone()
    )


def _realname_log_payload(row) -> dict:
    """一条实名记录对外形状（对标那 10 列）。⭐ `id_card_no` 不完整展示：脱敏**复用** `payloads._mask_pii`（前 3 后 2，⛔
    不另写一份——两份实现必然漂移；对标列 `title` 原话「不予完整展示，完整证件号请进编辑子账号资料里查看」）。⛔ 本载荷不含 `key_id` / `key_secret`（那两个字段压根不在这张表上；口径声明）。
    """
    return {
        "id": row.pk,
        "user_name": row.user_name_snapshot,
        "name": row.name,
        "id_card_no": _mask_pii(row.id_card_no),
        "gender": row.gender,
        "age": row.age,
        "cert_method": row.cert_method,
        "result": row.result,
        # ⚠️ D-0302：手拼时刻一律 `local_iso`（+08:00）——管理端实名记录页
        # 截串直显，裸 datetime 经信封渲染成 UTC 串时「申请/认证时间」差 8 小时。
        "applied_at": local_iso(row.applied_at),
        "certified_at": local_iso(row.certified_at),
        "cert_image": row.cert_image,
    }


def _realname_setting_payload(setting) -> dict:
    """那四项配置的对外形状（弹窗回填用）。四个值**照库里的真值**回（⛔ 不在这里套默认值 / 归一化：
    默认值只在 `RealNameSetting` 的列默认值那一处定义）；⛔ 不含 `key_id` / `key_secret`（同 `_realname_log_payload`）。
    """
    return {
        "enabled": bool(setting.enabled),
        "block_claim_without_cert": bool(setting.block_claim_without_cert),
        "require_cert_for_settlement": bool(setting.require_cert_for_settlement),
        "min_age": int(setting.min_age),
        "max_age": int(setting.max_age),
    }


def _package_row_local(row: dict) -> dict:
    """实名资源包行 → 时刻键出**项目时区**（`local_iso` +08:00；D-0302 兜底扫描收口点）。`package_payload` 与平台 CLI
    **共用**、那边按 datetime 处理 ⇒ 显示形状只能在本端点行装配处换，⛔ 不动 service 本身。
    """
    localized = dict(row)
    for key in ("expires_at", "created_at"):
        if key in localized:
            localized[key] = local_iso(localized[key])
    return localized


def _lookup_id_error(pk):
    """路径参数 `pk` 不是**十进制数字**时返回 1000 信封，否则 `None`。`retrieve` 与 `update` 共用：同一个畸形 id 必须走**同一条**分支，不能「看方法决定码值」。⚠️ `isdigit()` 不能用（`"²".isdigit()` 是 `True` 而 `int("²")` 抛错），用 `isdecimal()`——它比 `int()` **更严**（`int("1_0")`、`int(" 1")` 过而它拒），多挡的都是畸形 id。
    如实口径：这不是堵 HTML 500（`get_object_or_404` 已把 `TypeError`/`ValueError` 翻成 404 → 1002），作用是让非十进制 id 一律走同一条 1000 分支。
    """
    if str(pk).isdecimal():
        return None
    return api_response(data=None, msg="账号 id 必须是数字", code=ErrorCode.VALIDATION)


def _conflict_response():
    """「登录账号已存在」的 1001 信封（spec §5.1）——两条写路径共用同一份文案。"""
    return api_response(data=None, msg="登录账号已存在", code=ErrorCode.CONFLICT)


def _validate_write_serializer(serializer):
    """写路径（新增 / 编辑）共用的校验入口；返回非 `None` 时调用方直接当响应返回。重名 → 1001，其余非法值抛给 DRF →
    200 + 1000（E-0010）。**两条写路径必须共用**：各写一份的话重名的码值迟早分叉，而 spec §5.1/§7.4 要求重名在两条路径上都是**可区分**的 1001。
    """
    try:
        serializer.is_valid(raise_exception=True)
    except serializers.ValidationError as exc:
        if _is_duplicate_username(exc.detail):
            return _conflict_response()
        raise
    return None


def _is_duplicate_username(detail) -> bool:
    """`detail` 里 `username` 报的是**重名**而不是空 / 超长等别的字段错误。只认 `code`、不认「有没有 `username`
    这个键」：`""` 与 151 字符的 username 也把错误挂在同一键下，按键判断会把「该字段不能为空」回成「登录账号已存在」（spec §5.1 要的是**可区分**）。
    """
    errors = detail.get("username") if hasattr(detail, "get") else None
    if errors is None:
        return False
    if not isinstance(errors, (list, tuple)):
        errors = [errors]
    return any(getattr(error, "code", None) == DUPLICATE_USERNAME for error in errors)


def _generate_initial_password() -> str:
    """6 位数字（允许前导零），空间 `10^6`。见 spec D4 的代价。"""
    return f"{secrets.randbelow(10**6):06d}"


# ── spec §5.3：账号域留痕（`apps/audit/log.py::record` 的唯一调用形状）────
def _deleted_at(user):
    """该账号的 `deleted_at`；**没有档案行**时返回 `None`（= 没删过）。⚠️ 用 `getattr` 而不是 `user.staff_profile`：
    `seed_tenant` 灌的 `admin` **按设计没有**档案行，直接取属性抛 `RelatedObjectDoesNotExist`（那是 500，不是「没删」）。
    """
    profile = getattr(user, "staff_profile", None)
    return None if profile is None else profile.deleted_at


def _soft_deleted_user_by_username(username):
    """按用户名找**软删**的账号行（spec §2.7）：没有 / 用户名空 ⇒ `None`。只认「有档案行且 `deleted_at` 非空」的行——与 `serializers.validate_username` 的放行判据**同一形态**：序列化器在**校验层**（无锁）、本函数在**写路径**，找不到时两处必须给同一答案，否则出现「校验放行、写路径却新建」的分叉。`username` 有唯一约束 ⇒ 至多一行；`order_by("pk")` 只为查询确定性可读。
    """
    if not username:
        return None
    return (
        User.objects.filter(username=username, staff_profile__deleted_at__isnull=False)
        .order_by("pk")
        .first()
    )


def _delete_snapshot(user) -> str:
    """软删留痕的 `reason`：**登录账号 + 职位 + 标记 + 备注**（4 项）。对标原文的 `user.delete` 是 13 值完整快照（含手机号 /
    身份证号 / 结算支付宝 / 保证金 / 未结账单总额 / 操作IP），只写 4 项的理由逐条见 `apps/audit/actions.py::USER_DELETE` 的注释（前三类是 PII，后三类无列或要额外查结算表）。⛔ 别把这段读成「完整快照」——写了那几个字就是假话（E-0011）。
    """
    profile = getattr(user, "staff_profile", None)
    if profile is None:
        return user.get_username()
    return (
        f"{user.get_username()}，职位：{_first_role_name(user) or ''}，"
        f"标记：{profile.tag}，备注：{profile.remark}"
    )


def _record_account(request, action, *, target_id="", target_name="", **columns):
    """账号域的统一埋点形状（spec §5.3 / §5.9）。`log_type` 一律查登记表 `ACTION_LOG_TYPES`，不写死数字；
    操作人一律取**认证态**、从不取请求体里的身份（R20：请求体的身份可伪造，留痕的全部价值就是「谁做的」为真）；`target_id` 放账号 pk 的**数字**字符串（对标「相关ID」列），要显示的名字走 `target_name`（R11），两者不互兼。
    """
    return record(
        log_type=ACTION_LOG_TYPES[action],
        action=action,
        actor=request.user.get_username(),
        actor_id=request.user.pk,
        target_kind="user",
        target_id=target_id,
        target_name=target_name,
        **columns,
    )


def _game_names(game_ids):
    """请求里的 game id（已过 `AccountWriteSerializer.validate_games`）→ 游戏名，按 id 升序（差集是 `set`，升序让
    同一份差集每次渲染行序稳定）。查不到的 id 跳过：校验与写之间存在「游戏被删」的竞态窗口，留痕宁缺一行，也不能让这个窗口变成 HTML 500（E-0010 的判据是「**这类**请求还会不会 500」）。
    """
    ordered = sorted(set(game_ids or ()))
    if not ordered:
        return []
    names = dict(Game.objects.filter(pk__in=ordered).values_list("pk", "name"))
    return [names[pk] for pk in ordered if pk in names]


def _record_game_permissions(request, action, *, user, game_names):
    """逐游戏一行 `user.game_permission_add` / `user.game_permission_remove`。新增与编辑共用这一个循环
    （**M4**：`_persist_account` 用同一个 `games.set()` 服务两条路径，所以「新增用户资料」下面同样要逐游戏写）。调用方负责算好要写的那批游戏名：新增 = 全部选中项（before 集合为空），编辑 = 差集（见 `_record_account_updated`）。
    """
    for name in game_names:
        _record_account(
            request, action,
            target_id=user.pk, target_name=user.username, game_name=name,
        )


def _record_account_created(request, user, game_ids):
    """新增账号：一条 `user.create` + 每个选中的游戏一条 `user.game_permission_add`。直接用**选中的全部游戏**
    （不像编辑算差集）是**等价**的，不是两套口径：新增的 before 集合恒为空——档案行刚建，还没有任何游戏关联。
    """
    _record_account(request, "user.create", target_id=user.pk, target_name=user.username)
    _record_game_permissions(
        request, "user.game_permission_add", user=user, game_names=_game_names(game_ids)
    )


def _account_write_snapshot(user):
    """编辑**之前**的可比较快照：用户名 / 职位 / 游戏集合。派生行必须按**真实差集**写，不能按请求载荷：
    把没变的游戏写成「新增了游戏权限」是审计日志里的假话（与 `order.update` 同值不留噪音行同纪律）。
    """
    profile = getattr(user, "staff_profile", None)
    return {
        "username": user.username,
        # 职位名走 `payloads._first_role_name`（**不在这里再抄一份排序规则**）：
        # 它与 `account_payload(user)["role"]` 是同一口径，抄成两份必然漂移。
        "role": _first_role_name(user),
        "game_ids": set(profile.games.values_list("pk", flat=True)) if profile else set(),
    }


def _record_account_updated(request, user, validated, before):
    """编辑账号：一条 `user.update` + 按**实际变化**算出的派生行；`validated` 为空与 `_persist_account` 的 no-op 早返回是**同一条判据**（PATCH `{}`）⇒ 那种请求一行都不写。
    ⚠️ **M7（评审登记，不要照它实现）**：`user.rename` 模板第二句（替换订单接手人名称）描述的是**对标自己的**行为，我方不执行任何替换、只改 `auth_user.username`（在 `_persist_account` 里）；文案按 A11 **照抄**，本条显式登记「不做」，免得后来读者照着句子实现一个不存在的需求。
    """
    target_id, target_name = user.pk, user.username
    if not validated:
        return
    _record_account(request, "user.update", target_id=target_id, target_name=target_name)

    if before["username"] != target_name:
        _record_account(
            request, "user.rename",
            target_id=target_id, target_name=target_name,
            from_value=before["username"], to_value=target_name,
        )

    role_name = validated.get("role")
    if role_name is not None and role_name != before["role"]:
        _record_account(
            request, "user.role_change",
            target_id=target_id, target_name=target_name, to_value=role_name,
        )

    game_ids = validated.get("games")
    if game_ids is None:
        return
    new_ids = set(game_ids)
    _record_game_permissions(
        request, "user.game_permission_add",
        user=user, game_names=_game_names(new_ids - before["game_ids"]),
    )
    _record_game_permissions(
        request, "user.game_permission_remove",
        user=user, game_names=_game_names(before["game_ids"] - new_ids),
    )


def _persist_account(validated: dict, *, user=None, password: str | None = None):
    """写 `auth.User` + `StaffProfile` + 职位（Group）+ 游戏权限。⚠️ `set_password()` 只改内存、**必须 `save()`**（漏掉静默失效：回 200 但登不进）。**只在「请求里出现过的键」上赋值**：PATCH 缺席键一律**保持库里的值**（不是模型默认值）；新增时 `username`/`role`/`games` 由 `required=True` 保证，真缺了 `raise ValueError`（编程错误，按口径**响亮地**失败）。
    `password is None` 时不碰口令列，调用方负责另落哈希并自己开 `atomic()` 把这里的写与口令 `save()` 圈在一起。还会抛 DRF `ValidationError`（→ 200 + 1000）：`role` 校验只保证 `Role` 表里有名字，`Role` 与 `Group` 之间没有数据库级约束（`apps/rbac/services.py`）⇒ 两表可能漂移，同名 `Group` 查不到（E-0010）。
    """
    data = dict(validated)
    # 用 `None` 当「键缺席」的哨兵：这四类字段在序列化器里都不接受 `null`
    # （`CharField` / `ChoiceField` / `ListField` 都没有 `allow_null=True`），所以
    # 一个**通过校验**的 `validated_data` 里出现 `None` 只可能是「键不在」。
    username = data.pop("username", None)
    display_name = data.pop("display_name", None)
    role_name = data.pop("role", None)
    game_ids = data.pop("games", None)

    # ⚠️ 空载荷（PATCH `{}`）是**纯 no-op**：不碰账号行，也不得顺手 `get_or_create` 出一条空档案行
    # ——那是「一次查看式请求改了数据」。判据是「有效空」而不是「`validated` 为空」：
    # `partial=False`（PUT）时序列化器会把默认值填进来，那不是空载荷。
    if (
        user is not None
        and not data
        and username is None
        and display_name is None
        and role_name is None
        and game_ids is None
        and password is None
    ):
        return user

    changed = []                      # 本次真正改过的**列名**（编辑路径的 update_fields）
    if user is None:
        if username is None or role_name is None or game_ids is None:
            raise ValueError(
                "新增账号必须带 username / role / games（序列化器 required=True 保证）"
            )
        user = User(username=username, is_staff=False, is_superuser=False)
    elif username is not None:
        user.username = username
        changed.append("username")

    if display_name is not None:
        user.first_name = display_name
        changed.append("first_name")
    if password is not None:
        user.set_password(password)
        changed.append("password")
    # ⚠️ 编辑路径**必须**带 `update_fields=`：不带时 Django 把内存里**每一列**都写回（`update_fields` 为空则不过滤列），本请求读到的陈旧 `password` / `is_active` / `is_staff` / `is_superuser` 会盖掉并发写入——包括本端点明确拒收的 `is_active`（`test_edit_does_not_revert_a_concurrent_write_to_another_column` 钉住）。新增不能传：实例还没有 pk，Django 直接抛 `ValueError`（INSERT 本来就写整行）。
    if user._state.adding:
        user.save()
    elif changed:
        user.save(update_fields=changed)

    # ⚠️ 用**缓存里那个** profile 对象：`select_related("staff_profile")` 已把它塞进 `user._state.fields_cache`，而 `account_payload(user)` 读的就是它；换成表里新查出来的**另一个**对象去改，库里改了、响应回旧值，前端回填表单就把刚改掉的值显示回去（`AccountUpdateTests.test_update_response_carries_the_new_values` 钉住）。`RelatedObjectDoesNotExist` 同时继承 `AttributeError` ⇒ `getattr(..., None)` 可安全表达「没有档案」。
    profile = getattr(user, "staff_profile", None)
    # 新档案行只能整行 INSERT；既存行必须**按本次真正改过的列**写回（理由同上面 `auth_user` 那次 `update_fields=`：不带它时陈旧档案快照会盖掉并发写入——两个管理员改不同档案列时一笔静默丢失）。⚠️ `updated_at` 必须显式列进 `update_fields`：`auto_now` 只在 `pre_save` 里赋值、Django 按 `update_fields` 过滤列，不写它本次就不刷新。
    created_profile = profile is None
    if created_profile:
        # 新建的那行会自己回到 `user` 的反向缓存上：`StaffProfile(user=user)` 走正向一对一描述符、
        # 赋值时顺手写反向缓存，覆盖上面 `getattr` 留下的 `None` ⇒ 不需要手动回写（实测：加不加都一样）。
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        # ⭐ 来源 = 网页端/后台新增（D-0277）。⚠️ **只在这一支写**：编辑不碰来源（它记「当初从哪来」
        # 不是「这次从哪建」）；软删恢复也走 `created_profile=False`（档案行还在）⇒ 恢复旧 App 注册账号
        # **不会**被洗成 console、仍不能登网页端（来源记的是最初从哪进来；换口径需业务方另行拍板）。
        profile.account_source = AccountSource.CONSOLE
    changed_profile = []
    for field, value in data.items():
        setattr(profile, field, value)
        changed_profile.append(field)
    if created_profile:
        profile.save()
    elif changed_profile:
        profile.save(update_fields=[*changed_profile, "updated_at"])
    if game_ids is not None:
        profile.games.set(game_ids)   # 新增时至少一款由序列化器保证

    if role_name is not None:
        try:
            group = Group.objects.get(name=role_name)
        except Group.DoesNotExist:
            # 请求期的 `role` 校验查的是 **`Role` 表**，而账号挂 `Group`：两张表的名字之间没有数据库级约束（`apps/rbac/services.py`），「Role 行在、同名 Group 行不在」是真会发生的**数据**问题。`DoesNotExist` 不是 DRF 异常：不翻译就是 HTML 500（E-0010）；翻成 1000 并写明缺失职位，调用方才知道要修租户基础数据。
            raise serializers.ValidationError({"role": f"职位「{role_name}」不存在"})
        user.groups.set([group])      # D8：**单选**
    return user
