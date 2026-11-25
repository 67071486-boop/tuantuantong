"""实名认证的租户侧端点（spec §9.1 的前两条）。

| 方法 | 路径 | 谁 | 干什么 |
|---|---|---|---|
| POST | `/api/auth/realname/verify/` | 登录用户自己 | 提交 `{name, id_card_no}` 核验一次 |
| GET | `/api/auth/realname/` | 登录用户自己 | 看自己的实名状态 + 本租户剩余次数 + 是否启用 |

**权限口径**：`IsAuthenticated` —— ⛔ **不给额外权限点**。业务方 的原话是
「这个是**给用户自己实名**用的，现在**不做硬要求必须实名**」（spec §2-B4 / §8）⇒
它就是"登录即可"的自助动作，挂节点会让普通子账号永远核不了。

**平面守卫**（⭐ 照 `apps/core/profile_views.py` 的既有形态，评审 Important #8）：
`public` 平台平面**没有** `apps.realname` 的那些表（它们只在租户 schema 里）
⇒ 不守的话，平台面上这两个端点会以 `ProgrammingError: relation … does not exist`
的形式变成 **500**。守卫写在**视图体里**（⛔ 不是 `get_permissions`），
理由与 profile 那条一样：认证（401）要排在业务判据之前，而 404 要能被测到。

**信封**：一律走 `apps/core/envelope.py::api_response`，⛔ 不手写（spec D4）。
**异常翻译**：只翻**预期**的四类（未启用 1025 / 余量 1026-1027 / 号码非法 1000 /
按人限流 1029-D-0251）；未预期的异常⛔ **不接**，让它响亮地 500（`AGENTS.md` §5-E / E-0010）。
**密钥**：`key_secret` 是**全平台最贵的字符串** ⇒ 本模块**一个字节都不回**（spec §6-3）；
`GET` 的响应里连 `key_id` 都没有（判据钉"值不在"，见测试的密钥用例）。
"""
from django.db import DatabaseError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.realname import rate_limit, services
from apps.realname.quota import QuotaError
from apps.realname.serializers import RealNameVerifySerializer


def mask_id_card(id_card_no: str) -> str:
    """身份证号的**脱敏展示**（前 3 后 2，如 `110***00`）。

    ⛔ 本端点的任何响应都**不许**回完整号码（对标页的身份证列也写着"不予完整展示"，
    spec §4.2）。⚠️ 与对标一致，这里只做**展示**脱敏：库里仍存明文（spec §7 如实登记）。
    """
    if len(id_card_no) < 6:
        return "***"
    return f"{id_card_no[:3]}***{id_card_no[-2:]}"


def _read_grant_row():
    """读本租户的额度行（没有 ⇒ `None`）。**只读**，⛔ 不改任何计数（那是 `quota.py` 的唯一职责）。

    ⚠️ 租户上下文**只来自连接**（`tenant__schema_name` 与当前 schema 同名），
    ⛔ 不接受任何请求参数传进来的租户标识（`AGENTS.md` §4 铁律）。
    ⚠️ **不走 `apps/platform/models.py` 之外的捷径**：`RealNameGrant` 是平台表（`public`），
    而本模块是租户侧 —— 读它是**既有形态**（`apps/realname/quota.py` 同款：
    平台配额与租户流水在同一个事务/同一个连接里，实测详见 spec §4.2 的跨 schema 一节）。
    """
    from apps.platform.models import RealNameGrant

    return RealNameGrant.objects.filter(tenant__schema_name=_tenant_schema()).first()


def _tenant_schema() -> str:
    """当前租户 schema 名（租户上下文的**唯一出处**是连接态）。"""
    from django.db import connection

    return connection.schema_name


def remaining_quota() -> tuple[int | None, str]:
    """本租户剩余次数 `granted - used`，返回 `(remaining, msg)`。

    三种读数（⛔ 三者**不许混**）：

    | 情形 | 读数 | 为什么 |
    |---|---|---|
    | 有额度行 | `granted - used`（**整数减法**，⛔ 不用真值判断：`0` 是合法读数） | 正常 |
    | 没有额度行 | `0` | "没买过"就是没次数（spec §6-7 ①：⛔ 不 auto-create 一个 0 额度的行） |
    | **跨 schema 读失败**（`DatabaseError`） | `None` + 一句人话 `msg` | ⛔ **不许静默当成 0** —— 那会让用户以为"我没次数了"，去催客服购买，而其实是我们读不到 |

    ⚠️ 只接 `DatabaseError` 这一档（**预期**的基础设施失败）。其它异常照旧往上冒 ⇒ 响亮 500
    （`AGENTS.md` §5-E / E-0010）。
    """
    try:
        row = _read_grant_row()
    except DatabaseError:
        return None, "暂时读不到本租户的实名认证剩余次数，请稍后重试或联系管理员"
    if row is None:
        return 0, ""
    # ⚠️ 整数减法 + `int()`（`PositiveIntegerField` 回来就是 int）：⛔ 不写成
    # `if row.used:` 之类的真值判断 —— `used=0` 是合法读数。
    return int(row.granted) - int(row.used), ""


class _RealNameBaseView(APIView):
    """两个端点的公共装配：登录即可 + 平台平面守卫（404）。"""

    permission_classes = [IsAuthenticated]

    def _plane_guard(self):
        """平台平面没有本 app 的表 ⇒ 明确 404（⛔ 不是 500）。

        ⚠️ 响应走 `apps/core/plane.py::plane_not_found_response`（**单一出处**，
        与 `/api/auth/profile` / `/api/auth/password` 共用同一句 msg 与同一个状态码）。
        """
        if on_public_plane():
            return plane_not_found_response()
        return None


class RealNameVerifyView(_RealNameBaseView):
    """`POST /api/auth/realname/verify/` —— 用户给自己核验一次（spec §5 的全流程）。"""

    def post(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard

        # 请求来源的值一律过序列化器（E-0010）：`{name, id_card_no}` 都要 strip 后非空。
        # ⚠️ `raise_exception=True` ⇒ DRF 的 `ValidationError` 由 `envelope_exception_handler`
        # 翻成 **HTTP 200 + code=1000**（本仓口径，⛔ 不是 400）。
        serializer = RealNameVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["name"]
        id_card_no = serializer.validated_data["id_card_no"]

        # 只翻**预期**的四类失败（各自的码不同 ⇒ 管理员/用户能分清该找谁）。
        # ⚠️ 局部变量叫 `cert_record`（**⛔ 不叫 `record`**）：下面的留痕要调
        # `apps/audit/log.py::record`，而同名局部变量会把它遮掉
        # （语句作用域是**整个函数**，不是从赋值那一行开始 ⇒ 报
        # `TypeError: 'RealNameCertRecord' object is not callable`）。
        try:
            cert_record = services.verify_for_user(
                user=request.user, name=name, id_card_no=id_card_no
            )
        except services.RealNameDisabled as exc:
            # 1025：本租户没启用（去找管理员开总开关）。
            return api_response(code=ErrorCode.REALNAME_DISABLED, msg=str(exc))
        except services.RealNameInvalidInput as exc:
            # 1000：号码本地就不合法（用户自己核对）。
            return api_response(code=ErrorCode.VALIDATION, msg=str(exc))
        except rate_limit.RealNameRateLimited as exc:
            # 1029（D-0251）：同一用户提交过于频繁。⛔ 不锁定——窗口滚过自动恢复；
            # 这一次零出网、零扣减、零流水（递增点在厂商调用之前，见 services ③b）。
            return api_response(code=exc.code, msg=exc.message)
        except QuotaError as exc:
            # 1026 / 1027：用它**自己的**码（本租户没余量 vs 平台没可用包 —— 混成一个码
            # 会让管理员去改一个没错的地方，见 `error_codes.py` 那段注释）。
            return api_response(code=exc.code, msg=exc.message)

        # ⭐ T8：留痕（用户**自己**发起的一次核验）。
        # ⚠️ 一条流水 = 一次核验事件；幂等短路（`services.verify_for_user` 直接返回旧行）
        # 也走这里 ⇒ **重复提交同一号码会多写一条日志**（如实登记：日志记的是"用户提交过"，
        # 而"有没有扣次数"由流水与 `RealNameGrant.used` 判，两者不是一回事）。
        # ⚠️ 与业务写入**不在同一个 `atomic()`** 里：核验的写（扣次数 + 落流水）在
        # `services.verify_for_user` 内部、事务边界就在那里；把 `record` 塞进那里面
        # 会改 T6 已交付的代码。代价如实登记：**留痕写失败不会回滚那次核验**——
        # 与「留痕失败 ⇒ 整单回滚」（spec §7.4）在这一点上**有意偏离**，
        # 因为核验已经花掉了一次厂商额度、回滚会造出"扣了钱没有流水"的形态。
        # ⛔ `reason` 只放**脱敏**号码（PII 纪律，见 `apps/audit/actions.py` 的
        # `USER_REALNAME_VERIFY`）；⛔ 真实姓名一个字都不进日志。
        record(
            log_type=ACTION_LOG_TYPES["user.realname_verify"],
            action="user.realname_verify",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            target_kind="user",
            target_id=request.user.pk,
            target_name=cert_record.user_name_snapshot,
            reason=mask_id_card(cert_record.id_card_no),
        )

        remaining, _msg = remaining_quota()
        return api_response(
            {
                "result": cert_record.result,
                # ⛔ **只回脱敏号**（对标页也不完整展示）。
                "id_card_masked": mask_id_card(cert_record.id_card_no),
                # ⚠️ 整数减法（`None` = 读不到，见 `remaining_quota` 的表）。
                "remaining": remaining,
            }
        )


class RealNameStatusView(_RealNameBaseView):
    """`GET /api/auth/realname/` —— 我的实名状态 + 本租户是否启用 + 剩余次数。

    ⛔ **绝不返回任何密钥字段**（`key_secret` / `key_id` / `package_pk` 都不出现在响应里）。
    """

    def get(self, request):
        guard = self._plane_guard()
        if guard is not None:
            return guard

        setting = services.get_or_create_setting()
        profile = getattr(request.user, "staff_profile", None)
        passed = services.latest_pass_record(request.user)
        remaining, msg = remaining_quota()

        # `is_real_name` 的判据走**单一出处** `services.is_real_name`（D-0245 §2.1 收编；
        # 原先这里与 `profile_views.py` 各有一份内联）——spec §6-6 的"不阻断"语义一字没改
        # （管理员手填的号码也算"已实名"）。
        payload = {
            "enabled": bool(setting.enabled),
            "is_real_name": services.is_real_name(profile),
            # 认证时间取自**流水**（本任务不新增 `StaffProfile` 列）；没核过就是 `None`。
            # ⚠️ D-0302：App 实名页「认证时间」按串截显 ⇒ `local_iso`（+08:00）；
            # 旧写法裸 datetime 经信封渲染成 UTC 串、差 8 小时。
            "verified_at": local_iso(
                (passed.certified_at or passed.applied_at) if passed else None
            ),
            "remaining": remaining,
        }
        # ⚠️ `remaining is None`（跨 schema 读失败）时把原因放进 `msg`：⛔ 不许静默当成 0。
        return api_response(payload, msg=msg)
