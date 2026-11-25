"""问题反馈的四个端点（spec `2026-10-08-feedback-design.md` §4）：`POST /api/business/feedback/media/ticket/`、`GET` / `POST /api/business/feedback/`（门 = 登录 + 节点 `home.feedback`）、`PATCH /api/business/feedback/<int:pk>/`（门 = 登录 + `is_tenant_admin`）。三条口径（照既有两条图片存储线的形状）：① **平面守卫在视图体里**（`on_public_plane()` ⇒ 404）——`public` 平面上没有 `business_feedback` 表，放进去就是 500；⚠️ 对处理端点尤其**承重**：那边的管理员判据在 public 平面对超管恒真，没有这道守卫 public 上的超管会走进视图体、查一张不存在的表。② **节点门写在 `get_permissions()` 的**方法体**里**、参数是模块常量：接线位点表（`apps/rbac/wiring.py` 的 `SITES["home.feedback"]`）的 T2 判据解析的就是那个方法体（含一层模块常量解引用），只写在类级 `permission_classes` 上它解析不到。③ **处理端点的门是 `is_tenant_admin`、不是 RBAC 节点**：业务方原话「只有系统管理员可以看见反馈的内容」，判据出处就是 `apps/core/permissions.py::is_tenant_admin`；⛔ 那个类**不是** `home.feedback` 的拦截位点，别登记成节点门。
为什么校验**不在** `transaction.atomic()` 里：提交端点的补偿路径是「整条反馈不落库 + 删掉这一次涉及的对象」，而删除是**远端 I/O**（生产是 COS）——放进 `atomic()` 会同时踩两条既有教训：块内 `return` 会提交半截写入（E-0022）、远端往返拖长事务（图片存储线同型登记）。⇒ 校验**全部先做完**（校验里一个字节都不写库），`atomic()` 里只剩两张表的 `INSERT`，块内没有任何 `return`。
清理对象时的边界（⛔ 这里是**安全边界**，不是细节）：清理只删**调用者自己的反馈截图**，判据唯一出处 `is_my_feedback_key`（`<本租户>/users/<本人 id>/feedback.<随机>.<ext>`），**校验与清理共用同一份**。⛔ **不要把判据放宽到「本租户」**：那会让这个提交端点变成**跨用户的删除原语**——任何持 `home.feedback` 的人（内置职位都有这个点）都能借它删掉本租户里任意对象：别人的头像、微信收款码、乃至订单截图（`demo/orders/…`），而这些对象名在页面上是**回显**的（订单载荷 `images[].key`、头像预签名链接里就带 key）；红队实测过这组形状、总指挥复跑证实 ⇒ 已按「本人 · 反馈段」收口（spec 里的「⛔ 更正」块是同步留痕）。代价（如实登记）：**误传的别人的 key 不再被删**——宁留一个孤儿对象，也不开一个跨用户的删除口子；跨租户的 key 一个字节都不碰（AGENTS.md §4 / D-0137 同向）。
"""
from django.db import connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.feedback_serializers import (
    FeedbackCreateSerializer,
    FeedbackHandleSerializer,
    FeedbackTicketSerializer,
)
from apps.business.models import Feedback, FeedbackImage, FeedbackStatus
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.permissions import is_tenant_admin
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.core.storage import (
    ALLOWED_EXTS,
    MAX_USER_MEDIA_BYTES,
    USER_KEY_SEGMENT,
    build_user_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_type,
)

#: 三个节点门端点共用的权限节点（spec §1.1：对标 112「问题反馈及建议」本来就有这个点，
#: 这里只是把它接上）。⚠️ 接线位点表指向本模块的 `get_permissions`，T2 判据向下解析**一层**
#: 模块常量 ⇒ 这里改名要同批改 `apps/rbac/wiring.py`。
FEEDBACK_NODE = "home.feedback"

#: 一条反馈最多 3 张截图（业务方裁定，spec §2 第 4 行）。
MAX_FEEDBACK_IMAGES = 3

#: 超限的拒绝文案（前端按同一份契约提示；⛔ 标点与空格都不要动）。
FEEDBACK_LIMIT_MSG = "一条反馈最多上传3张截图"
#: 单张超限的拒绝文案：走**用户媒体**那一条上限（`MAX_USER_MEDIA_BYTES` = 10 MB）。
#: ⚠️ 对象名复用 `<租户>/users/<用户 id>/feedback.<随机>.<ext>`（spec §1.3）⇒
#: `key_media_kind` 判它是 `"user"` 类，本机直传路由的早反馈上限自动取同一个数。
IMAGE_SIZE_LIMIT_MSG = "图片超过 10 MB"
#: 认不出图片魔数时的拒绝文案（与既有两条图片存储线逐字一致）。
IMAGE_TYPE_LIMIT_MSG = "只支持 jpg / jpeg / png"
#: 对象名不属于「本租户 + 本用户」时的 403 文案（同 `MyProfileMediaClaimView` 那句）。
FOREIGN_KEY_MSG = "截图对象名不属于当前用户"


def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**。

    ⛔ 只来自 `connection.schema_name`（请求期已由 django-tenants 的中间件设好），
    **绝不接受任何来自请求参数的租户标识**（`AGENTS.md` §4）。
    """
    return connection.schema_name


def feedback_payload(row: Feedback, storage) -> dict:
    """一行反馈的对外形状（spec §4.2）。

    ⚠️ `images[].url` 是**现签**的短时效链接，⛔ 不落库（URL 会过期）；`key` 一并回给前端
    ——前端提交时要原样回传它。
    """
    return {
        "id": row.pk,
        "content": row.content,
        "status": row.status,
        "status_label": row.get_status_display(),
        "admin_note": row.admin_note,
        "created_by_name": user_display_name(row.created_by),
        "created_at": local_iso(row.created_at),
        "handled_by_name": (
            user_display_name(row.handled_by) if row.handled_by is not None else None
        ),
        "handled_at": local_iso(row.handled_at),
        "images": [
            {"key": image.object_key, "url": storage.presign_get(image.object_key).url}
            for image in row.images.all()
        ],
    }


#: 反馈截图对象名的**种类段**（`build_user_key(..., kind="feedback", ...)` 产出 `feedback.<随机>.<ext>`）。
FEEDBACK_KEY_SEGMENT = "feedback"


def is_my_feedback_key(key: str, user, schema: str) -> bool:
    """这个对象名是不是**本次调用者自己**的反馈截图（校验与清理**共用同一份判据**）。形状：`<租户>/users/<本人 id>/feedback.<随机>.<ext>`，判据两条**缺一不可**：
    ① `key_belongs_to_tenant(key, schema)` —— **归一化**判据（按 `/` 切段、剔除空段与 `.`、任何 `..` 一律拒）。⛔ 不能写成裸 `key.startswith(f"{schema}/")`：`demo/../victim/…` 的字符串前缀确实是 `demo/`，而解析后落在**别人的命名空间**里（`apps/core/storage.py::key_belongs_to_tenant` 的 docstring 记着实测）。② 归一化后**恰好四段**、且逐段等于 `[本租户, "users", 本人 id, "feedback." 开头]`。⚠️ **为什么②必须存在（红队 + 总指挥复跑实测的跨用户删除原语）**：护栏只到①时，本函数被清理路径调用 ⇒ 任何持 `home.feedback` 的人都能借提交端点删掉**本租户里任意对象**——别人的头像（`users/9/avatar.…`）、微信收款码、乃至订单截图（`orders/…`），这些对象名在页面上是回显的（订单载荷 `images[].key`、头像预签名链接里就带 key）；⇒ 判据收到「**本人 · 反馈段**」这一格，代价是**误传的别人 key 不再被删**——宁留一个孤儿对象，也不开一个跨用户的删除口子（spec `2026-10-08-feedback-design.md` 的「⛔ 更正」块）。
    ⚠️ 两处调用点（`_image_keys_error` 的护栏与 `_drop_own_objects`）**必须都走本函数**：各写一份就是同一事实的两套判据（E-0021 族），它们一旦漂移，**清理会比校验更宽**——那正是这次缺陷的形状。
    """
    if not key_belongs_to_tenant(key, schema):
        return False
    parts = [p for p in key.split("/") if p not in ("", ".")]
    return (
        len(parts) == 4
        and parts[0] == schema
        and parts[1] == USER_KEY_SEGMENT
        and parts[2] == str(user.pk)
        and parts[3].startswith(f"{FEEDBACK_KEY_SEGMENT}.")
    )


def _drop_own_objects(storage, keys, user) -> None:
    """把这一次提交涉及的、**属于调用者自己的反馈截图**删掉（spec §4.3 第 6 条的补偿）。⛔ 判据只有一条来源：`is_my_feedback_key`，**降级成「本租户」就是跨用户删除原语**（红队实测：别人的头像 / 收款码 / 订单截图都会被删），详见那个函数的 docstring；跨租户的 key 连①都过不了 ⇒ 一个字节都不碰（AGENTS.md §4 / D-0137 同向）。
    删失败**不抛**——照 `apps/core/profile_views.py::MyProfileMediaClaimView` 的清理口径，对象删不掉不该让这次拒绝变成 500。
    """
    schema = _tenant_schema()
    for key in keys:
        if not is_my_feedback_key(key, user, schema):
            continue
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 —— 清理失败只该留下一个孤儿对象
            pass


def _image_keys_error(storage, keys, user, schema):
    """截图的五条校验（spec §4.3 的第 2–5 条）。

    全部通过回 `None`；任一条不过 ⇒ 清掉**这一次涉及且属于本人**的对象后回**拒绝响应**。
    两步都返回 Response/None，视图里直接 `return`（⛔ 这些校验不在 `atomic()` 里）。
    """
    if len(keys) > MAX_FEEDBACK_IMAGES:
        _drop_own_objects(storage, keys, user)
        return api_response(code=ErrorCode.VALIDATION, msg=FEEDBACK_LIMIT_MSG)
    # 护栏：对象名必须属于**本租户 + 本用户 + 反馈段**（判据的唯一出处是
    # `is_my_feedback_key`，⛔ 不在这里另写一份前缀比较）。
    for key in keys:
        if not key:
            _drop_own_objects(storage, keys, user)
            return api_response(code=ErrorCode.VALIDATION, msg="截图对象名不能为空")
        if not is_my_feedback_key(key, user, schema):
            # ⚠️ **先清理、再 403**：这类拒绝也要把**本人的**对象删掉（spec §4.3 第 6 条），
            #    而 `raise` 会跳过后面的清理代码 ⇒ 顺序不能反。
            #    码与文案照 `MyProfileMediaClaimView` 同一条失败（403 + 同一句话）。
            _drop_own_objects(storage, keys, user)
            raise PermissionDenied(FOREIGN_KEY_MSG)
        meta = storage.head(key)
        if meta is None:
            _drop_own_objects(storage, keys, user)
            return api_response(code=ErrorCode.VALIDATION, msg="截图还没上传成功")
        # ⚠️ 只取**大小**；`head()` 给的内容类型**故意丢弃**——它客户端可控 / 本机按扩展名推，
        #    不可信（真正的类型判据是下面的魔数嗅探）。
        size, _header_content_type = meta
        if size <= 0:
            _drop_own_objects(storage, keys, user)
            return api_response(code=ErrorCode.VALIDATION, msg="图片无效或为空")
        if size > MAX_USER_MEDIA_BYTES:
            _drop_own_objects(storage, keys, user)
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_SIZE_LIMIT_MSG)
        if sniff_image_type(storage, key) is None:
            _drop_own_objects(storage, keys, user)
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)
    return None


class FeedbackPlaneBase(APIView):
    """四个端点的公共装配：登录 + **平面守卫**（`public` 平面一律 404）。"""

    permission_classes = [IsAuthenticated]

    def _plane_guard(self):
        if on_public_plane():
            return plane_not_found_response()
        return None


class FeedbackNodeGatedView(FeedbackPlaneBase):
    """三个节点门端点（签发凭证 / 列表 / 提交）的公共装配：登录 + 节点 `home.feedback`。⚠️ 权限装配写在 `get_permissions()` 的**方法体**里（⛔ 不是类级 `permission_classes`）、且节点 code 取模块常量——接线位点表（`apps/rbac/wiring.py`）的 T2 判据解析的是那个方法体（含一层模块常量解引用）。
    ⚠️ `has_node` **方法内导入**（照 `apps/core/profile_views.py` 的既有形状）；public 平面上**不加**节点门，改由视图体的平面守卫回 404（理由见模块 docstring）。
    """

    NODE_DENIED_MESSAGE = "当前职位没有此操作所需的权限节点"

    def get_permissions(self):
        from apps.rbac.permissions import has_node

        permissions = [IsAuthenticated()]
        if not on_public_plane():
            gate = has_node(FEEDBACK_NODE)
            gate.message = self.NODE_DENIED_MESSAGE
            permissions.append(gate())
        return permissions


class FeedbackTicketView(FeedbackNodeGatedView):
    """`POST /api/business/feedback/media/ticket/` —— 签发一张只能写**本用户**对象的直传凭证。

    ⛔ **只签发、不写库**：签发了但没传完，库里不该出现指向一个不存在对象的行
    （同 `MyProfileMediaTicketView` 的 `del field` 那一手）。
    """

    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        ser = FeedbackTicketSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ext = ser.validated_data["ext"].lower().lstrip(".")
        if ext not in ALLOWED_EXTS:
            return api_response(code=ErrorCode.VALIDATION, msg=IMAGE_TYPE_LIMIT_MSG)
        key = build_user_key(_tenant_schema(), request.user.pk, "feedback", ext)
        signed = get_storage().presign_put(key, cache_control=True)
        return api_response({"key": key, "upload_url": signed.url, "expires_in": signed.expires_in})


class FeedbackCollectionView(FeedbackNodeGatedView):
    """`GET` / `POST /api/business/feedback/`（列表与提交，spec §4.2 / §4.3）。**两个方法住同一个视图**：spec 把它们写成**同一条路径**；拆成两个类会让 `urls.py` 出现两条字面相同的 `path()`，而 Django **只命中第一条** ⇒ 第二条是死路由（同 `ScreenshotCollectionView` 记下的那条教训）。
    ⚠️ 两个方法**同一个**节点门（`home.feedback`）：对标那个点就是「问题反馈及建议」这一个功能点（它的备注原文是「建议全员开放权限」）。
    """

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        rows = Feedback.objects.select_related("created_by", "handled_by").prefetch_related(
            "images"
        )
        if not is_tenant_admin(request.user):
            # ⛔ 在 **queryset 层**收口（不是前端过滤、也不是「先取全部再筛」）：
            #    非管理员只看得到自己提过的那几条（spec §4.2 / §9 判据 1）。
            rows = rows.filter(created_by=request.user)
        storage = get_storage()
        items = [feedback_payload(row, storage) for row in rows]
        return api_response({"items": items, "total": len(items)})

    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        ser = FeedbackCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        content = ser.validated_data["content"]
        # ⚠️ **保持顺序去重**：同一次提交里同一个 key 只落一行——不去重的话 `image_keys: [k, k]` 会落**两行同 key**（`FeedbackImage` 上没有唯一约束），列表载荷把同一张图渲染两遍、页面上看不出是重复数据。⚠️ 去重放在**数量判据之前**：4 个相同的 key 是**一张图**，不该按「4 张」拒。
        keys = list(dict.fromkeys(ser.validated_data["image_keys"]))
        storage = get_storage()
        rejected = _image_keys_error(storage, keys, request.user, _tenant_schema())
        if rejected is not None:
            return rejected
        # ⛔ 块内**没有** `return`：校验全在上面做完了，这里只有两张表的写入
        #    （E-0022：`atomic()` 里的 `return` 会提交半截写入且返回 200）。
        with transaction.atomic():
            row = Feedback.objects.create(content=content, created_by=request.user)
            FeedbackImage.objects.bulk_create(
                [FeedbackImage(feedback=row, object_key=key) for key in keys]
            )
        return api_response({"id": row.pk})


class TenantAdminOnly(BasePermission):
    """只有持「系统管理员」职位的人能过（spec §4.4 的门，判据收口在 `is_tenant_admin`）。"""

    message = "只有系统管理员可以处理反馈"

    def has_permission(self, request, view):
        return is_tenant_admin(request.user)


class FeedbackHandleView(FeedbackPlaneBase):
    """`PATCH /api/business/feedback/<int:pk>/` —— 管理员标记状态 / 写处理备注（spec §4.4）。⚠️ 门是 **`is_tenant_admin`（审批层判据），不是 RBAC 节点**（业务方原话是「只有系统管理员可以看见反馈的内容」）⇒ ⛔ 本类**不是** `home.feedback` 的拦截位点。
    ⛔ 非管理员一律 **403**（不是 404、不是静默成功）；判据在权限层 ⇒ 非管理员连 `get_object_or_404` 都走不到（库里那行一个字都不会变）。
    """

    def get_permissions(self):
        permissions = [IsAuthenticated()]
        if not on_public_plane():
            permissions.append(TenantAdminOnly())
        return permissions

    def patch(self, request, pk):
        guard = self._plane_guard()
        if guard is not None:
            return guard
        ser = FeedbackHandleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        # ⛔ 只查本租户（schema 即边界）：别的租户的 pk 在这里 404，不存在的 pk 也 404。
        row = get_object_or_404(Feedback, pk=pk)
        changed = ser.validated_data
        if "status" in changed:
            row.status = changed["status"]
            if row.status == FeedbackStatus.HANDLED:
                row.handled_by = request.user
                row.handled_at = timezone.now()
            else:
                # 改回「未处理」⇒ 处理人与处理时间一并清空（spec §4.4）。
                row.handled_by = None
                row.handled_at = None
        if "admin_note" in changed:
            row.admin_note = changed["admin_note"]
        row.save()
        # 操作日志（spec §8）：**只有这一个端点**记——管理员对**别人的**记录做处置，
        # 且这会改变提交人看到的状态。提交与签发票据**刻意不记**（理由见
        # `tests/test_operation_log_ledger.py` 的 `DELIBERATELY_NOT_LOGGED`）。
        record(
            log_type=ACTION_LOG_TYPES["feedback.handle"],
            action="feedback.handle",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            target_kind="feedback",
            target_id=str(row.pk),
            to_value=row.get_status_display(),
            reason=row.admin_note,
        )
        return api_response({"id": row.pk, "status": row.status})
