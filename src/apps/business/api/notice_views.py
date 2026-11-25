"""系统内部公告（对标 `/System/systemnotice`）。端点（走 router，`/api/business/system-notices/`）：`GET` 登录即可（主页「通知和公告」要读）；`POST` / `PATCH /api/business/system-notices/<pk>/` / `DELETE …/<pk>/` 都要 `system.publish_notice`。与对标的三处对应：对标列表列是 `title` / `content` / `createTime`，本模块把后两者映射成 `content` / `created_at`；对标弹窗**只有「公告内容」一个输入框**（`systemnotice.js` 的 `add(id, content)`）、标题是服务端派生的 ⇒ `title` 在这里由 `derive_title()` 生成、序列化器把它标成只读。
对标删除是硬删（没有二次确认以外的软删除），这里同样硬删（公告不是业务凭据）。⚠️ **不要**把公告做进 `Notification` 表：那是**按收件人**的三类订单事件（被指派/被抢走/待审核），而公告是**整租户可见、没有已读态**的另一种东西——混表会让「我的消息」多出没有收件人的行。
"""
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError

from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.models import SystemNotice
from apps.rbac.permissions import has_node

#: 公告内容上限（逐字照抄对标 textarea 的 `maxlength="1000"`）。
MAX_CONTENT_LENGTH = 1000
#: 派生标题的上限（对标列表用 `substring(0, 50)` 展示；这里存同样的长度）。
MAX_TITLE_LENGTH = 50


def derive_title(content: str) -> str:
    """从公告内容派生标题：**第一行**去空白后截 50 字。

    只有一处实现：序列化器写 `title` 时调它。⛔ 不要让前端或模型 `save()` 再拼一次
    （那样「标题怎么来的」会变成两份会漂移的判据）。
    """
    first_line = (content or "").strip().splitlines()[0] if (content or "").strip() else ""
    first_line = first_line.strip()
    return first_line[:MAX_TITLE_LENGTH] or "（无标题公告）"


class SystemNoticeSerializer(serializers.ModelSerializer):
    """公告的对外形状。`title` / `created_by` 只读——标题派生、发布人取认证态。"""

    content = serializers.CharField(
        max_length=MAX_CONTENT_LENGTH,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "blank": "请输入公告内容",
            "max_length": f"公告内容不能超过 {MAX_CONTENT_LENGTH} 个字",
        },
    )

    class Meta:
        model = SystemNotice
        fields = ["id", "title", "content", "created_by", "created_at", "updated_at"]
        read_only_fields = ["title", "created_by", "created_at", "updated_at"]

    def validate_content(self, value: str) -> str:
        # `CharField(trim_whitespace=True)` 已把纯空白变成空串并触发 `blank`；这里再兜一道，
        # 防止全角空格之类的「看起来非空」内容。
        if not (value or "").strip():
            raise ValidationError("请输入公告内容")
        return value

    def create(self, validated_data):
        request = self.context.get("request")
        validated_data["title"] = derive_title(validated_data["content"])
        validated_data["created_by"] = (
            getattr(request.user, "get_username", lambda: "")() if request else ""
        )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        if "content" in validated_data:
            validated_data["title"] = derive_title(validated_data["content"])
        return super().update(instance, validated_data)


class SystemNoticeViewSet(EnvelopeModelViewSet):
    """公告 CRUD。读 = 登录即可；写 = `system.publish_notice`（含删除）。"""

    queryset = SystemNotice.objects.all()
    serializer_class = SystemNoticeSerializer
    pagination_class = EnvelopePagination

    #: 逐 action 的节点 code 表。⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py`
    #: 的 B2 走 AST 取非 docstring 的字符串常量。
    SYSTEM_NOTICE_PERMISSIONS = {
        "create": ("system.publish_notice",),
        "update": ("system.publish_notice",),
        "partial_update": ("system.publish_notice",),
        "destroy": ("system.publish_notice",),
    }

    def get_permissions(self):
        """与默认权限类取并集（形状照 `ProjectCategoryViewSet.get_permissions`）。"""
        base = [permission() for permission in self.permission_classes]
        codes = self.SYSTEM_NOTICE_PERMISSIONS.get(self.action)
        if not codes:
            return base
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]
