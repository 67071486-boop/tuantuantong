from rest_framework import serializers

from chat_service.image_limits import MAX_CHAT_ALBUM_BYTES as MAX_ALBUM_BYTES

from .images import ImageInput

MAX_BIGINT = 2**63 - 1
MAX_ALBUM_IMAGES = 10
MAX_TIMESTAMP_SECONDS = 253402300799


class ContactsQuery(serializers.Serializer):
    q = serializers.CharField(required=False, allow_blank=True, max_length=100, default="")


class ContactDirectoryUpdate(serializers.Serializer):
    primary_contact_id = serializers.IntegerField(min_value=1, max_value=MAX_BIGINT, allow_null=True)
    backup_contact_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=MAX_BIGINT),
    )

    def validate(self, attrs):
        primary = attrs["primary_contact_id"]
        backups = attrs["backup_contact_ids"]
        if primary in backups or len(set(backups)) != len(backups):
            raise serializers.ValidationError({"backup_contact_ids": "客服不能重复选择"})
        return attrs


class ConversationQuery(serializers.Serializer):
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100)
    offset = serializers.IntegerField(required=False, min_value=0, max_value=MAX_BIGINT)

    def to_internal_value(self, data):
        if (set(data) - set(self.fields) or
                hasattr(data, 'getlist') and any(len(data.getlist(key)) != 1 for key in data)):
            raise serializers.ValidationError({'non_field_errors': ['会话分页参数有误']})
        return super().to_internal_value(data)


class ConversationStart(serializers.Serializer):
    peer_id = serializers.IntegerField(min_value=1, max_value=MAX_BIGINT)


class MessageQuery(serializers.Serializer):
    before = serializers.IntegerField(required=False, min_value=1, max_value=MAX_BIGINT)
    around = serializers.IntegerField(required=False, min_value=1, max_value=MAX_BIGINT)

    def validate(self, attrs):
        if "before" in attrs and "around" in attrs:
            raise serializers.ValidationError("只能指定一个历史消息定位方式")
        return attrs


class MessageSearchQuery(serializers.Serializer):
    q = serializers.CharField(max_length=100, allow_blank=True, default="")
    before = serializers.IntegerField(required=False, min_value=1, max_value=MAX_BIGINT)
    sender_id = serializers.IntegerField(required=False, min_value=1, max_value=MAX_BIGINT)
    date_from = serializers.IntegerField(required=False, min_value=0, max_value=MAX_TIMESTAMP_SECONDS)
    date_to = serializers.IntegerField(required=False, min_value=0, max_value=MAX_TIMESTAMP_SECONDS)
    image_only = serializers.BooleanField(default=False)

    def validate(self, attrs):
        date_from, date_to = attrs.get("date_from"), attrs.get("date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise serializers.ValidationError({"date_to": "结束时间不能早于开始时间"})
        if not attrs["q"] and not attrs["image_only"] and not {"sender_id", "date_from", "date_to"} & attrs.keys():
            raise serializers.ValidationError({"q": "请输入关键词或选择筛选条件"})
        return attrs


class MessageSend(serializers.Serializer):
    text = serializers.CharField(max_length=4000, allow_blank=True, default="")
    client_id = serializers.CharField(max_length=100, trim_whitespace=False)
    reply_to_id = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=MAX_BIGINT)
    image = ImageInput(required=False)
    images = ImageInput(many=True, required=False, min_length=2, max_length=MAX_ALBUM_IMAGES)
    mention_member_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=MAX_BIGINT),
        max_length=50, required=False,
    )

    def validate(self, attrs):
        images = attrs.get("images")
        if images is not None:
            if "image" in attrs:
                raise serializers.ValidationError("单图和相册只能选择一种")
            if sum(len(image["data"]) for image in images) > MAX_ALBUM_BYTES:
                raise serializers.ValidationError("多张图片合计不能超过 5.25 MiB")
        if not attrs["text"] and "image" not in attrs and images is None:
            raise serializers.ValidationError("请输入消息或选择图片")
        return attrs


class ConversationPreferenceUpdate(serializers.Serializer):
    pinned = serializers.BooleanField(required=False)
    remark = serializers.CharField(max_length=30, allow_blank=True, required=False)
    marked_unread = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("请选择要修改的会话设置")
        return attrs


class MarkRead(serializers.Serializer):
    seq = serializers.IntegerField(min_value=0, max_value=MAX_BIGINT)


class OrderMessagePin(serializers.Serializer):
    message_id = serializers.IntegerField(allow_null=True, min_value=1, max_value=MAX_BIGINT)


class SyncQuery(serializers.Serializer):
    after = serializers.IntegerField(default=0, min_value=0, max_value=MAX_BIGINT)
