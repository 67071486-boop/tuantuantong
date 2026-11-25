"""QQ 机器人设置页的 REST 端点。全部视图继承 `QQBotSettingsGateView`：权限门只在基类类体上声明（`permission_classes = [has_node("system.qqbot_settings")]`，自有节点位点见 `apps/rbac/wiring.py` 的 `SITES`；
照 `apps/openapi/api/app_views.py::ApiAppAdminView` 同一手法——一个基类全部端点共用，不各写一份）。两条统一纪律：① 代理类端点（菜单/禁言/入群申请/分享链接）绑定未配置（无 `app_id` 或无 `app_secret`）时统一回业务码
`QQBOT_NOT_CONFIGURED`(1030)「机器人未配置」——HTTP 仍是 200、绝不 500；远端失败也不 500，错误码/文案放响应 `data` 的 `remote_error`（或 `info_error`）字段、页面自己决定怎么提示（E-0087：判成败只看 `qqbot_api` 返回的 `ok`，「没抛异常」不构成成功）；② 验真（PATCH binding）配置照存（用户可能离线填好再连），验真失败只记 `last_error` 并回 `verified: false`。"""
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import Game
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.qqbot import qqbot_api
from apps.qqbot.api.serializers import (
    QQBotBindingSerializer,
    QQBotGroupRemarkSerializer,
    QQBotGroupSerializer,
    QQBotJoinPolicySerializer,
    QQBotJoinRequestActionSerializer,
    QQBotMenuSerializer,
    QQBotPushConfigSerializer,
)
from apps.qqbot.models import (
    QQBotBinding,
    QQBotGroup,
    QQBotGroupPushConfig,
    QQBotJoinRequestLog,
    QQBotOutboundMessage,
)
from apps.qqbot.push import queue_test_push
from apps.rbac.permissions import has_node

#: 发件箱列表的固定页大小（页面是「最近发送记录」的滚动列表，不做可调页）。
MESSAGE_PAGE_SIZE = 20

#: 载荷截断（列表不回全文，详情走 worker 的日志面）。
PAYLOAD_PREVIEW_CHARS = 200

#: 「机器人未配置」的一句人话（四个代理端点共用同一句，页面按 code 分支）。
NOT_CONFIGURED_MSG = "机器人未配置：请先在 QQ 机器人设置页填写 AppID / AppSecret"

#: PATCH binding 的操作留痕动作（登记见 `tests/test_operation_log_ledger.py` 的 `INSTRUMENTED["apps/qqbot/api/views.py"]`）。
#: ⛔ 只记动作不记值：`app_secret` 只写不读（models 的红线），照 `system.open_api_app_rotate_secret`
#: 「只记动作、新旧值都不进留痕」的同一条纪律。
QQBOT_BINDING_LOG_ACTION = "system.qqbot_binding_update"

#: PATCH binding 可改的字段（留痕「改了哪几格」只记**字段名**，值一概不进日志）。
BINDING_PATCHABLE_FIELDS = ("app_id", "app_secret", "enabled", "auth_verified")


class QQBotSettingsGateView(APIView):
    """全部 QQ 机器人设置端点的共同装配（权限门只在这一个类体上声明）。
    ⚠️ 子类不要再声明 `permission_classes`——那会把基类的门整条覆盖（DRF 取的是 `get_permissions()` 解析后的 `self.permission_classes`）。"""

    permission_classes = [has_node("system.qqbot_settings")]


def _get_binding() -> QQBotBinding:
    return QQBotBinding.get_solo()


def _token_manager(binding: QQBotBinding) -> qqbot_api.TokenManager | None:
    """凭据齐（`app_id` 与 `app_secret` 都非空）才造得出客户端；否则 `None`。"""
    if not binding.app_id or not binding.app_secret:
        return None
    return qqbot_api.TokenManager(binding.app_id, binding.app_secret)


def _require_token_manager() -> qqbot_api.TokenManager:
    """代理端点的共同前置：没配凭据 ⇒ 业务码 1030（HTTP 200，不 500）。"""
    binding = _get_binding()
    manager = _token_manager(binding)
    if manager is None:
        # ⛔ 不 raise：这一档是**业务结果**（配置没填），不是请求错误——HTTP 200 + code。
        return None
    return manager


def _remote_error(code: str, message: str) -> dict:
    """远端失败放进 `data` 的形状（`qqbot_api` 的 `(ok, data, code, message)` 后两格）。"""
    return {"code": code, "message": message}


def _remote_unconfigured_data() -> dict:
    return {"remote_error": {"code": str(int(ErrorCode.QQBOT_NOT_CONFIGURED)),
                             "message": NOT_CONFIGURED_MSG}}


class QQBotBindingView(QQBotSettingsGateView):
    """`GET /api/qqbot/binding/`（只读面）+ `PATCH /api/qqbot/binding/`（可改凭据与开关）。"""

    def get(self, request):
        binding = _get_binding()
        return api_response(data=QQBotBindingSerializer(binding).data)

    def patch(self, request):
        # 请求体必须是 JSON 对象（DRF 的 JSONParser 不校验顶层类型 ⇒ 数组原样进来会让
        # `.get` 抛 AttributeError ⇒ 500，违反 E-0010「这类请求不许 500」）。
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        binding = _get_binding()
        serializer = QQBotBindingSerializer(binding, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        # 验真的触发判据：**这次请求带了凭据格**（app_id / app_secret 至少一格），
        # 且按「这次请求之后」的值凭据是齐的。只改 enabled / auth_verified 不触发
        # （单字段改动不该付一次网络往返的代价）。
        touched_credential = ("app_id" in request.data) or ("app_secret" in request.data)
        # 留痕（评审 P2-4）：先记「这次真的改了哪几格」——只记**字段名**，
        # ⛔ 值（尤其 app_secret）一个字都不进日志；secret 留空 = 不改（序列化器同一
        # 语义），不当成变更；无实际变更不写留痕（R26 全仓口径）。
        changed_fields = []
        for field in BINDING_PATCHABLE_FIELDS:
            if field not in request.data:
                continue
            value = request.data[field]
            if field == "app_secret":
                if value != "":
                    changed_fields.append(field)
            elif value != getattr(binding, field):
                changed_fields.append(field)
        serializer.save()
        binding.refresh_from_db()
        if changed_fields:
            record(
                log_type=ACTION_LOG_TYPES[QQBOT_BINDING_LOG_ACTION],
                action=QQBOT_BINDING_LOG_ACTION,
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind="qqbot_binding",
                target_id=str(binding.pk),
                target_name=binding.app_id,
                field_name=",".join(changed_fields),
            )

        verified = None
        if touched_credential and binding.app_id and binding.app_secret:
            verified = self._verify(binding)
        data = QQBotBindingSerializer(binding).data
        if verified is not None:
            data["verified"] = verified
        return api_response(data=data)

    @staticmethod
    def _verify(binding: QQBotBinding) -> bool:
        """同步试取一次 token 验真。成功：清 `last_error`、`connection_state` 不动（那是 ws 进程的写入方，保存凭据不等于连接已建好）；
        失败：配置仍保存（用户可能离线填好再连），`last_error` 记错误码 + 文案（`TokenError` 的 message 出自 `qqbot_api` 的脱敏路径，secret 不进文案）。"""
        manager = qqbot_api.TokenManager(binding.app_id, binding.app_secret)
        try:
            manager.get_token()
        except qqbot_api.TokenError as exc:
            binding.last_error = f"TOKEN: {exc}"
            binding.save(update_fields=["last_error"])
            return False
        binding.last_error = ""
        binding.save(update_fields=["last_error"])
        return True


class QQBotGroupListView(QQBotSettingsGateView):
    """`GET /api/qqbot/groups/`：群列表；可选 `?refresh_info=<pk>` 顺手刷一个群的官方资料。"""

    def get(self, request):
        info_error = None
        refresh_id = request.query_params.get("refresh_info")
        if refresh_id:
            info_error = self._refresh_info(refresh_id)
        groups = QQBotGroup.objects.all().prefetch_related("push_config__games")
        rows = QQBotGroupSerializer(groups, many=True).data
        data = {"items": rows}
        if info_error is not None:
            # 「刷新群信息」按钮的后端：白名单接口未批时官方回 11253——
            # 优雅降级（放进 info_error），⛔ 不许 500。
            data["info_error"] = info_error
        return api_response(data=data)

    def _refresh_info(self, raw_id: str):
        """拉一个群的官方资料回填投影列；任何失败都只进 `info_error`。"""
        try:
            group = QQBotGroup.objects.get(pk=int(raw_id))
        except (TypeError, ValueError):
            raise ValidationError("refresh_info 必须是群 id")
        except QQBotGroup.DoesNotExist:
            raise ValidationError("没有这个群")
        manager = _require_token_manager()
        if manager is None:
            return _remote_unconfigured_data()["remote_error"]
        ok, result, code, message = qqbot_api.get_group_info(manager, group.group_openid)
        if ok and isinstance(result, dict):
            # 投影列只回填认识的三格（其余列的写入方是官方事件流，别在这里抢）。
            group_name = result.get("group_name", result.get("name"))
            if group_name:
                group.group_name = str(group_name)
            # ⚠️ 人数用 `is not None` 判（E-0021：0 是合法边界，不是缺失）。
            member_num = result.get("member_count", result.get("member_num"))
            if member_num is not None:
                group.member_num = int(member_num)
            if result.get("recv_msg_setting"):
                group.recv_msg_setting = str(result.get("recv_msg_setting"))
            group.save(
                update_fields=["group_name", "member_num", "recv_msg_setting"]
            )
            return None
        return _remote_error(code, message)


class QQBotGroupDetailView(QQBotSettingsGateView):
    """`PATCH /api/qqbot/groups/<pk>/`：只许改 `remark`。"""

    def patch(self, request, pk):
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        serializer = QQBotGroupRemarkSerializer(group, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return api_response(data=QQBotGroupSerializer(group).data)


class QQBotGroupPushConfigView(QQBotSettingsGateView):
    """`PUT /api/qqbot/groups/<pk>/push-config/`：整份替换 {is_active, msg_type, game_ids}。"""

    def put(self, request, pk):
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        serializer = QQBotPushConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        game_ids = serializer.validated_data["game_ids"]
        found = Game.objects.filter(id__in=set(game_ids))
        if found.count() != len(set(game_ids)):
            raise ValidationError("有游戏不存在（game_ids 里混进了当前租户没有的游戏）")
        config, _ = QQBotGroupPushConfig.objects.update_or_create(
            group=group,
            defaults={
                "is_active": serializer.validated_data["is_active"],
                "msg_type": serializer.validated_data["msg_type"],
            },
        )
        config.games.set(found)
        return api_response(
            data={
                "group_id": group.pk,
                "is_active": config.is_active,
                "msg_type": config.msg_type,
                "game_ids": list(config.games.values_list("id", flat=True)),
            }
        )


class QQBotGroupTestPushView(QQBotSettingsGateView):
    """`POST /api/qqbot/groups/<pk>/test-push/`：建一条测试消息（不碰网络）。
    ⚠️ 不要求机器人已配置：发不发得出去是 worker 与凭据的事，这一步只落发件箱行（用户可以先排测试再去填凭据）；行落库即成功（响应回行 id）。"""

    def post(self, request, pk):
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        row = queue_test_push(group)
        return api_response(data={"id": row.pk, "status": row.status})


class QQBotGroupMutesView(QQBotSettingsGateView):
    """`GET /api/qqbot/groups/<pk>/mutes/`：代理 `get_mutes`；失败降级进 `remote_error`。"""

    def get(self, request, pk):
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        manager = _require_token_manager()
        if manager is None:
            return api_response(data=_remote_unconfigured_data())
        ok, result, code, message = qqbot_api.get_mutes(manager, group.group_openid)
        if ok:
            return api_response(data={"items": result})
        return api_response(data={"items": None, "remote_error": _remote_error(code, message)})


class QQBotGroupJoinRequestsView(QQBotSettingsGateView):
    """`GET /api/qqbot/groups/<pk>/join-requests/`：本地留痕 + 远端拉取，两块并列。
    本地 `QQBotJoinRequestLog` 行永远先出（审批策略的唯一证据面）；远端 `get_join_requests` 成功就原样附在 `remote`（不做复杂合并）、失败只带 `remote_error`，本地照出。"""

    def get(self, request, pk):
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        # `select_related("acted_by")`：每行要显示操作人显示名（⛔ 不逐行查库）。
        local = QQBotJoinRequestLog.objects.filter(group=group).select_related("acted_by")
        rows = [
            {
                "id": row.pk,
                "join_request_id": row.join_request_id,
                "member_openid": row.member_openid,
                "username": row.username,
                # ⚠️ D-0302：三个时刻键手拼 ⇒ `local_iso`（+08:00）；
                # QQ 机器人设置页截串直显，裸 datetime 经信封渲染成 UTC 串时差 8 小时。
                "apply_at": local_iso(row.apply_at),
                "apply_source": row.apply_source,
                "risk_tips": row.risk_tips,
                "verify_info": row.verify_info,
                "decision": row.decision,
                "reject_reason": row.reject_reason,
                "error_code": row.error_code,
                "acted_at": local_iso(row.acted_at),
                # ⭐ 评审 P2-3：操作人（id + 显示名）；`None` = 自动处置（机器路径）。
                # 显示名走全仓唯一口径 `apps.accounts.display`（照 feedback 的
                # `handled_by_name` 同一惯例）。
                "acted_by": row.acted_by_id,
                "acted_by_name": (
                    user_display_name(row.acted_by) if row.acted_by is not None else None
                ),
                "created_at": local_iso(row.created_at),
            }
            for row in local
        ]
        data = {"items": rows}
        manager = _require_token_manager()
        if manager is None:
            data.update(_remote_unconfigured_data())
            return api_response(data=data)
        ok, result, code, message = qqbot_api.get_join_requests(
            manager, group.group_openid
        )
        if ok:
            data["remote"] = result
        else:
            data["remote_error"] = _remote_error(code, message)
        return api_response(data=data)


class QQBotGroupJoinRequestApproveView(QQBotSettingsGateView):
    """`POST /api/qqbot/groups/<pk>/join-requests/approve/`：人工审批。
    ⚠️ 无论成败都写 `QQBotJoinRequestLog`（manual_approved / manual_declined / failed 三种结局各一行）——留痕表是审批策略的唯一证据面，审批接口报错的一次也要留下「谁想进来、我们试过、结果如何」。"""

    def post(self, request, pk):
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        serializer = QQBotJoinRequestActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        fields = serializer.validated_data

        manager = _require_token_manager()
        now = timezone.now()
        if manager is None:
            return api_response(
                code=ErrorCode.QQBOT_NOT_CONFIGURED, msg=NOT_CONFIGURED_MSG
            )
        ok, _result, code, message = qqbot_api.approve_join_request(
            manager,
            group.group_openid,
            fields["member_openid"],
            op=fields["op"],
            join_request_id=fields["join_request_id"],
            reject_reason=fields.get("reject_reason") or None,
        )
        if ok:
            decision = (
                QQBotJoinRequestLog.Decision.MANUAL_APPROVED
                if fields["op"] == "approve"
                else QQBotJoinRequestLog.Decision.MANUAL_DECLINED
            )
        else:
            decision = QQBotJoinRequestLog.Decision.FAILED
        QQBotJoinRequestLog.objects.create(
            group=group,
            join_request_id=fields["join_request_id"],
            member_openid=fields["member_openid"],
            decision=decision,
            reject_reason=fields.get("reject_reason") or "",
            error_code="" if ok else str(code),
            acted_at=now,
            acted_by=request.user,  # ⭐ 评审 P2-3：人工审批要能回答「谁点的按钮」。
        )
        return api_response(
            data={
                "ok": ok,
                "decision": decision,
                "error": None if ok else _remote_error(code, message),
            }
        )


class QQBotGroupJoinPolicyView(QQBotSettingsGateView):
    """`PATCH /api/qqbot/groups/<pk>/join-policy/`：{mode, whitelist}。"""

    def patch(self, request, pk):
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        group = QQBotGroup.objects.filter(pk=pk).first()
        if group is None:
            raise ValidationError("没有这个群")
        serializer = QQBotJoinPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # whitelist 仅 whitelist 模式有意义；其余模式照存空表（清掉旧名单，别留幽灵数据）。
        whitelist = (
            serializer.validated_data["whitelist"]
            if serializer.validated_data["mode"] == QQBotGroup.JoinPolicyMode.WHITELIST
            else []
        )
        group.join_policy_mode = serializer.validated_data["mode"]
        group.join_policy_whitelist = whitelist
        group.save(update_fields=["join_policy_mode", "join_policy_whitelist"])
        return api_response(
            data={
                "group_id": group.pk,
                "join_policy_mode": group.join_policy_mode,
                "join_policy_whitelist": group.join_policy_whitelist,
            }
        )


class QQBotMenuView(QQBotSettingsGateView):
    """`GET|PUT /api/qqbot/menu/`：群菜单代理（`?group=<pk>` / body `group` 键指定群）。
    PUT 先服务端校验（`QQBotMenuSerializer`：数量/类型/名称长度/https），校验失败是业务校验错误（`code 1000`）、不调 QQ；校验过了才 `put_menu`。"""

    def get(self, request):
        group = self._group_from_query(request)
        manager = _require_token_manager()
        if manager is None:
            return api_response(code=ErrorCode.QQBOT_NOT_CONFIGURED, msg=NOT_CONFIGURED_MSG)
        ok, result, code, message = qqbot_api.get_menu(manager, group.group_openid)
        if not ok:
            return api_response(
                data={"menu": None, "version": None,
                      "remote_error": _remote_error(code, message)}
            )
        version = None
        if isinstance(result, dict):
            for key in ("menu_version", "version"):
                value = result.get(key)
                if isinstance(value, int):
                    version = value
                    break
        return api_response(data={"menu": result, "version": version})

    def put(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        group = self._group_from_payload(request)
        serializer = QQBotMenuSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        menu = serializer.validated_data["menu"]
        manager = _require_token_manager()
        if manager is None:
            return api_response(code=ErrorCode.QQBOT_NOT_CONFIGURED, msg=NOT_CONFIGURED_MSG)
        ok, result, code, message = qqbot_api.put_menu(manager, group.group_openid, menu)
        if not ok:
            return api_response(
                data={"ok": False, "remote_error": _remote_error(code, message)}
            )
        version = None
        if isinstance(result, dict) and isinstance(result.get("version"), int):
            version = result["version"]
            binding = _get_binding()
            binding.menu_version = version
            binding.save(update_fields=["menu_version"])
        return api_response(data={"ok": True, "version": version})

    @staticmethod
    def _group_from_query(request) -> QQBotGroup:
        raw = request.query_params.get("group")
        try:
            return QQBotGroup.objects.get(pk=int(raw))
        except (TypeError, ValueError):
            raise ValidationError("group 必须是群 id")
        except QQBotGroup.DoesNotExist:
            raise ValidationError("没有这个群")

    @staticmethod
    def _group_from_payload(request) -> QQBotGroup:
        raw = request.data.get("group")
        try:
            return QQBotGroup.objects.get(pk=int(raw))
        except (TypeError, ValueError):
            raise ValidationError("group 必须是群 id")
        except QQBotGroup.DoesNotExist:
            raise ValidationError("没有这个群")


class QQBotShareLinkView(QQBotSettingsGateView):
    """`POST /api/qqbot/share-link/`：代理 `generate_url_link`，返回 `{url}`（body 带 `group`）。"""

    def post(self, request):
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        try:
            group = QQBotGroup.objects.get(pk=int(request.data.get("group")))
        except (TypeError, ValueError):
            raise ValidationError("group 必须是群 id")
        except QQBotGroup.DoesNotExist:
            raise ValidationError("没有这个群")
        manager = _require_token_manager()
        if manager is None:
            return api_response(code=ErrorCode.QQBOT_NOT_CONFIGURED, msg=NOT_CONFIGURED_MSG)
        ok, result, code, message = qqbot_api.generate_url_link(manager, group.group_openid)
        if not ok:
            return api_response(
                data={"url": None, "remote_error": _remote_error(code, message)}
            )
        return api_response(data={"url": result.get("url")})


class QQBotMessageListView(QQBotSettingsGateView):
    """`GET /api/qqbot/messages/?group=&status=&page=`：发件箱分页列表（按 -created_at）。"""

    def get(self, request):
        messages = QQBotOutboundMessage.objects.select_related("group").all()
        group_raw = request.query_params.get("group")
        if group_raw not in (None, ""):
            # 请求来源的值进类型化查询前先过 int（E-0010：`?group=abc` 不许 500）。
            try:
                messages = messages.filter(group__pk=int(group_raw))
            except (TypeError, ValueError):
                raise ValidationError("group 必须是群 id")
        status_raw = request.query_params.get("status")
        if status_raw:
            if status_raw in QQBotOutboundMessage.Status.values:
                messages = messages.filter(status=status_raw)
            else:
                raise ValidationError("status 取值不合法")
        try:
            page = max(1, int(request.query_params.get("page") or 1))
        except (TypeError, ValueError):
            raise ValidationError("page 必须是整数")

        total = messages.count()
        start = (page - 1) * MESSAGE_PAGE_SIZE
        rows = [
            {
                "id": row.pk,
                "group_id": row.group_id,
                "group_openid": row.group.group_openid,
                "kind": row.kind,
                "order_id": row.order_id,
                "msg_type_eff": row.msg_type_eff,
                "payload_text": (row.payload_text or "")[:PAYLOAD_PREVIEW_CHARS],
                "status": row.status,
                "attempts": row.attempts,
                "error_code": row.error_code,
                "error_text": row.error_text,
                # ⚠️ D-0302：时刻键手拼 ⇒ `local_iso`（+08:00），
                # 同上面入群申请台账那一张表的口径。
                "scheduled_at": local_iso(row.scheduled_at),
                "sent_at": local_iso(row.sent_at),
                "created_at": local_iso(row.created_at),
            }
            for row in messages.order_by("-created_at")[start:start + MESSAGE_PAGE_SIZE]
        ]
        return api_response(
            data=paginated(rows, total=total, page=page, page_size=MESSAGE_PAGE_SIZE)
        )
