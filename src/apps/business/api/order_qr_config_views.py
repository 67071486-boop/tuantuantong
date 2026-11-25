"""马上来查码配置的租户面端点（D-0348）：`GET` / `PATCH /api/business/order-qr/config/`（持 `system.pinduoduo_settings` 节点）。读配置**api_key 只回掩码**（⛔ 原文不回显）；保存时 `api_key` **留空 = 不改**、**启用态基址必填**（合并请求后的终态判，缺失即拒）、任何成功保存**清 401 闩**。
**权限节点**：复用现成的 `system.pinduoduo_settings`（D-0128 冻结 ⛔ 不新增节点；微信公众号设置先例——同属「第三方对接设置」的可见性承载点）。**平面守卫**：本表只在租户 schema ⇒ `public` 平面请求一律 404（照 `apps/wechatmp/views.py::_WechatMpBaseView` 的形态；不守就是 `ProgrammingError` ⇒ 500）。**台账**：PATCH 记 `system.order_qr_config_update`（改的是**凭据与启停**——照 QQ 机器人设置页的口径，这类动作必须记台账），GET 是读、不记（spec §10.3）。**信封**：`apps.core.envelope.api_response`。
"""
from __future__ import annotations

from django.db import IntegrityError
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.models import OrderQrFetchConfig
from apps.business.order_push import validate_webhook_url
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.plane import on_public_plane, plane_not_found_response
from apps.rbac.permissions import has_node

#: 与「微信公众号设置」等第三方对接设置页同一颗节点（⛔ 不新增 RBAC 节点）。
ORDER_QR_CONFIG_NODE = "system.pinduoduo_settings"

#: 台账动作码（`apps/audit/actions.py` 同批登记）。
CONFIG_UPDATE_ACTION = "system.order_qr_config_update"


def mask_secret(value: str) -> str:
    """密钥掩码（照 `wechatmp.views.mask_secret`：≤8 位全星，否则前 4 后 4）。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _tenant_schema() -> str:
    """租户标识只来自 `connection.schema_name`（⛔ 不收请求参数，`AGENTS.md` §4）。"""
    from django.db import connection

    return connection.schema_name


def get_config() -> OrderQrFetchConfig | None:
    """本租户的配置行；没有 = 未配置（触发点零动作，不预置行）。"""
    return OrderQrFetchConfig.objects.filter(tenant_schema=_tenant_schema()).first()


class OrderQrConfigSerializer(serializers.Serializer):
    """保存配置的入参（E-0010：请求来源的值一律过序列化器）。三个字段都可缺省（PATCH 语义：缺省 / 空串 = 保持不变——`api_key` 是密钥「留空 = 不改」，`base_url` 空串**另有语义**：清除基址；⚠️ 启用态下清空会被视图拒绝（F-1②）——「基址空 = 未配置」，只在停用态允许保存）。
    """

    enabled = serializers.BooleanField(required=False)
    base_url = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=None
    )
    api_key = serializers.CharField(
        max_length=128, required=False, allow_blank=True, default=""
    )

    def validate_base_url(self, value: str) -> str:
        text = (value or "").strip()
        if not text:
            return ""
        try:
            return validate_webhook_url(text)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc


class OrderQrConfigView(APIView):
    """读 / 存马上来查码配置（单端点两方法；信封同 wechatmp 设置页）。"""

    permission_classes = [has_node(ORDER_QR_CONFIG_NODE)]

    def get(self, request):
        if on_public_plane():
            return plane_not_found_response()
        config = get_config()
        return api_response(
            {
                "configured": config is not None,
                "enabled": bool(config and config.enabled),
                "base_url": config.base_url if config else "",
                # ⛔ 密钥是全页最贵的字符串：只回掩码（WechatMpConfig.appsecret 同一口径）。
                "api_key_masked": mask_secret(config.api_key) if config else "",
                "auth_failed_at": (
                    config.auth_failed_at.isoformat() if config and config.auth_failed_at else None
                ),
                "updated_at": (
                    config.updated_at.isoformat() if config and config.updated_at else None
                ),
            }
        )

    def patch(self, request):
        if on_public_plane():
            return plane_not_found_response()
        serializer = OrderQrConfigSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attrs = serializer.validated_data

        config = get_config()
        created = config is None
        if config is None:
            config = OrderQrFetchConfig(tenant_schema=_tenant_schema())
        changed: list[str] = []
        if "enabled" in request.data and attrs.get("enabled") is not None:
            if created or config.enabled != attrs["enabled"]:
                changed.append("启停")
                config.enabled = attrs["enabled"]
        if "base_url" in request.data and attrs.get("base_url") is not None:
            new_url = attrs["base_url"].strip()
            if created or config.base_url != new_url:
                changed.append("基址")
                config.base_url = new_url
        if (attrs.get("api_key") or "").strip():
            changed.append("密钥")
            config.api_key = attrs["api_key"].strip()
        # ⭐ 任何一次成功保存都清 401 闩（「停触发直到配置变更」的落地）：
        # 改密钥当然要清；只改启停 / 基址也清——那是运营者确认过配置的显式动作。
        if config.auth_failed_at is not None:
            changed.append("清除密钥拒绝标记")
            config.auth_failed_at = None
        # ⭐ F-1②：启用态（按**合并请求后**的终态判）基址必须非空——漏填基址在保存当场报错，而不是落一个「开着开关却无处可拉」的配置（那会让每个触发点都过闸、worker 每单每 10 分钟刷一行必败台账）。
        # 放在「没有要保存的变更」之前：残存的坏配置连无变更请求都会被指到病根。
        if config.enabled and not (config.base_url or "").strip():
            return api_response(
                code=ErrorCode.VALIDATION, msg="启用马上来查码必须先填写对端基址"
            )
        if not changed:
            return api_response(code=ErrorCode.VALIDATION, msg="没有要保存的变更")
        try:
            config.save()
        except IntegrityError:
            # 并发首保存撞 `tenant_schema` 唯一约束（WechatMpConfig 同款 DB 兜底）⇒ 业务码，不是 500。
            return api_response(code=ErrorCode.VALIDATION, msg="配置行创建冲突，请刷新后重试")
        record(
            log_type=ACTION_LOG_TYPES[CONFIG_UPDATE_ACTION],
            action=CONFIG_UPDATE_ACTION,
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            target_kind="order_qr_config",
            target_id=_tenant_schema(),
            reason="、".join(changed),
        )
        return api_response(
            {
                "configured": True,
                "enabled": config.enabled,
                "base_url": config.base_url,
                "api_key_masked": mask_secret(config.api_key),
                "auth_failed_at": None,
                "updated_at": timezone.now().isoformat(),
            }
        )
