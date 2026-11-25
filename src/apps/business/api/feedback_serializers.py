"""问题反馈端点的入参序列化器（spec `2026-10-08-feedback-design.md` §4）。⚠️ 请求来源的值**一律先过序列化器**再进类型化的列或查询（AGENTS.md §5-E：判据是「**这类请求**还会不会 500」，不是「这个字段还会不会 500」）；本模块只做**形状**校验（类型 / 长度 / 取值域），**业务**校验（截图数量、对象归属、大小、魔数）在视图里做——那里才拿得到存储层与当前用户。
⚠️ 校验失败的口径：本仓是 **HTTP 200 + `code=ErrorCode.VALIDATION`**（`apps/core/exceptions.py::envelope_exception_handler`），⛔ 不是 400；文案是给中文界面直接显示的 ⇒ `error_messages` 一律显式给中文（默认是 DRF 的英文）。
"""
from rest_framework import serializers

from apps.business.models import FeedbackStatus


class ObjectKeyField(serializers.CharField):
    """只收**字符串**的对象名。⚠️ 为什么自己判类型：DRF 的 `CharField` 会把 `int` / `float` **静默强转成字符串**（`str(data)`）⇒ `image_keys=[1, 2]` 会变成两个形如 `"1"` 的对象名、落进「对象名不属于当前用户」那条 **403** 分支，而不是「入参形状错」的 `1000`；两条分支语义不同（一个是越权形状、一个是格式错），混起来的代价是用例说不清自己测的是哪一条。
    """

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class FeedbackTicketSerializer(serializers.Serializer):
    """要一张上传凭证。`ext` 由前端给（后端再按 `storage.ALLOWED_EXTS` 校一次）。⚠️ `allow_blank=True`：空串**不在这里拒**，留给视图的 `ALLOWED_EXTS` 判据回「只支持 jpg / jpeg / png」——与既有的头像 / 收款码签发端点**同一句话**（`apps/core/profile_views.py::MyProfileMediaTicketView`）。
    """

    ext = serializers.CharField(
        max_length=8,
        allow_blank=True,
        error_messages={
            "required": "缺少图片格式",
            "null": "缺少图片格式",
            # ⚠️ 扩展名不合法**一律同一句话**（与视图里的 `ALLOWED_EXTS` 判据、以及既有的 `apps/core/profile_views.py::MyProfileMediaTicketView` 逐字相同）：⛔ 不新增第二种说法——「太长」与「不在白名单」对调用方是同一件事。
            "max_length": "只支持 jpg / jpeg / png",
        },
    )


class FeedbackCreateSerializer(serializers.Serializer):
    """提交一条反馈。⚠️ `image_keys` 只校**形状**（是数组、元素是 ≤255 的字符串）：元素上限 255 与 `FeedbackImage.object_key` 的列宽同源；数量上限（3 张）与对象归属/大小/类型都在视图里判（那里才能在做不到时**清理对象**，spec §4.3 第 6 条）。
    """

    content = serializers.CharField(
        error_messages={
            "required": "请填写反馈内容",
            "blank": "请填写反馈内容",
            "null": "请填写反馈内容",
        },
    )
    image_keys = serializers.ListField(
        child=ObjectKeyField(
            max_length=255,
            allow_blank=True,
            error_messages={
                "invalid": "截图对象名格式不正确",
                "max_length": "截图对象名太长",
                "null": "截图对象名不能为空",
            },
        ),
        required=False,
        default=list,
        allow_empty=True,
        error_messages={
            "not_a_list": "截图对象名必须是数组",
            "invalid": "截图对象名格式不正确",
            "null": "截图对象名必须是数组",
        },
    )


class FeedbackHandleSerializer(serializers.Serializer):
    """管理员处置一条反馈（spec §4.4）。⚠️ 两个字段都可选，但**至少要给一个**——两个都不给的请求是空操作，静默回成功会让调用方以为自己改了什么（同族：`apps/openapi` 的纯 no-op PATCH 口径）。
    """

    status = serializers.ChoiceField(
        choices=FeedbackStatus.choices,
        required=False,
        error_messages={
            "invalid_choice": "状态只能是未处理或已处理",
            "null": "状态不能为空",
        },
    )
    admin_note = serializers.CharField(
        required=False,
        allow_blank=True,
        error_messages={"null": "备注不能为空"},
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("请至少给出 status 或 admin_note")
        return attrs
