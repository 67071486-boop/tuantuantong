"""录单识别设置的两个端点（spec §5）：`GET /api/business/recognition-settings/`（RBAC 节点 89 `system.auto_discern_settings`）与 `PATCH /api/business/recognition-settings/<item_code>/`（节点 96 `system.edit_auto_discern_keywords`）。⚠️ **不是 `EnvelopeModelViewSet`**：这一页只有「看列表 / 改一行」，没有新增与删除（对标也只能改关键词，列表是服务端固定行）⇒ 两个显式 `path()` 比挂一个只实现一半动作的 ViewSet 更诚实，也少两条 405 路由。
⚠️ **租户边界**（AGENTS.md §4）：查询只在当前连接 / 租户 schema 上跑、**不接受任何 tenant 参数**，`item_code` 只来自 URL。⚠️ **写留痕与业务写入同事务**，且**纯 no-op 不写日志行**（spec §5.2）；块内**不 `return`** 退出补偿路径（E-0022）。
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.intake_rules import RECOGNITION_ITEMS
from apps.business.models import RecognitionKeyword
from apps.business.recognition import recognition_rows
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node

#: 识别项的 code / 名称词表（从注册表派生，**不另抄一份**）。
_ITEM_NAMES = {item.code: item.name for item in RECOGNITION_ITEMS}


class RecognitionSettingListView(APIView):
    """14 行的关键词列表（默认值 ∪ 覆盖值），顺序 = 页面顺序。"""

    permission_classes = [has_node("system.auto_discern_settings")]

    def get(self, request):
        return api_response(data={"items": recognition_rows()})


class RecognitionSettingDetailView(APIView):
    """改一行的关键词串；空串 = **合法的"清空该行"**（不是"没填"）。"""

    permission_classes = [has_node("system.edit_auto_discern_keywords")]

    def patch(self, request, item_code):
        # 请求体**必须是 JSON 对象**：DRF 的 `JSONParser` 只 `json.load` 顶层、不校验类型（`rest_framework/parsers.py::JSONParser.parse`），数组 / 字符串 / 数字都会原样成为 `request.data` ⇒ 下一行 `.get(...)` 抛 `AttributeError` ⇒ **HTTP 500**。
        # 这是「非法输入一律 `code=1000`」（spec §5.2 / E-0010 同族）的形状之一；⚠️ 本路由带 `<str:item_code>` 转换器 ⇒ `tests/test_no_route_500.py` 会跳过它，兜底只有 `tests/test_recognition_settings_api.py` 里那三条体用例。
        if not isinstance(request.data, dict):
            raise ValidationError("请求体必须是对象")
        name = _ITEM_NAMES.get(item_code)
        if name is None:
            # 未知识别项 ⇒ 业务码 1000（不是 404、更不是 500）：请求形状合法但值不在词表里。
            raise ValidationError("没有这个识别项")
        keywords = request.data.get("keywords", "")
        if not isinstance(keywords, str):
            raise ValidationError("keywords 必须是字符串")
        # 纯空白按空串处理（spec §5.2）：清空是合法行为，不是"没填"。
        if not keywords.strip():
            keywords = ""
        width = RecognitionKeyword._meta.get_field("keywords").max_length
        if len(keywords) > width:
            raise ValidationError("关键词过长")
        with transaction.atomic():
            row = RecognitionKeyword.objects.filter(item_code=item_code).first()
            changed = row is None or row.keywords != keywords
            if changed:
                # ⚠️ `updated_at` 是 `auto_now` ⇒ 必须走 `save()`（`update_or_create` 会调）。
                RecognitionKeyword.objects.update_or_create(
                    item_code=item_code, defaults={"keywords": keywords}
                )
                record(
                    log_type=ACTION_LOG_TYPES["system.recognition_keywords_update"],
                    action="system.recognition_keywords_update",
                    actor=request.user.get_username(),
                    actor_id=request.user.pk,
                    target_kind="recognition_item",
                    target_id=item_code,
                    target_name=name,
                    to_value=keywords,
                )
        return api_response(
            data={"item_code": item_code, "name": name, "keywords": keywords}
        )
