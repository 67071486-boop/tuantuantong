"""订单截图四端点的入参序列化器（spec §7）。

⚠️ 请求来源的值**一律先过序列化器**再进类型化的列或查询（`AGENTS.md` §5-E / `E-0010`）：
判据是「**这类请求**还会不会 500」，不是「这个字段还会不会 500」。
"""
from rest_framework import serializers


class ScreenshotTicketSerializer(serializers.Serializer):
    """要一张上传凭证。`ext` 由前端给（后端再按 `ALLOWED_EXTS` 校一次）。"""

    ext = serializers.CharField(max_length=8)
    #: 0=订单截图 / 1=群聊二维码（spec §3.2 的「图片类型」单选）
    img_type = serializers.IntegerField(required=False, default=0, min_value=0, max_value=1)


class ScreenshotClaimSerializer(serializers.Serializer):
    """认领（落库）刚直传上去的那一个对象。"""

    screenshot_id = serializers.IntegerField(min_value=1)
    original_name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    #: 对标那张表单的 `maxlength=50`（spec §3.2），⛔ 别放宽
    remark = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
