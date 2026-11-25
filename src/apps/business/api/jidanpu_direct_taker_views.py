from rest_framework import serializers


class JidanpuNumberField(serializers.RegexField):
    def __init__(self, **kwargs):
        super().__init__(r"\A[1-9][0-9]{0,63}\Z", **kwargs)

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class _JidanpuStudioSerializer(serializers.Serializer):
    studio_id = serializers.IntegerField(min_value=1, max_value=2**63 - 1, required=False)


class JidanpuFirstOptionsSerializer(_JidanpuStudioSerializer):
    refresh = serializers.BooleanField(default=False)
    operation_id = serializers.UUIDField(required=False)


class JidanpuTakerSearchSerializer(_JidanpuStudioSerializer):
    query = serializers.CharField(max_length=100)


class JidanpuTakerAssignmentSerializer(_JidanpuStudioSerializer):
    operation_id = serializers.UUIDField(required=False)
    number = JidanpuNumberField()
    name = serializers.CharField(max_length=100, error_messages={"required": "请重新搜索并选择鸡蛋铺打手。"})
    expected_number = JidanpuNumberField(allow_blank=True)
    expected_fee = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    fee = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    expected_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True, required=False)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    studio_token = serializers.RegexField(r"\A[0-9a-f]{64}\Z", required=False)

    def validate(self, attrs):
        first_fields = {"studio_id", "expected_price", "studio_token"}
        given = first_fields.intersection(attrs)
        if (given and given != first_fields) or ("price" in attrs and not given):
            raise serializers.ValidationError("请重新选定鸡蛋铺工作室，并核对发单价。")
        return attrs


class JidanpuTakerVerifySerializer(serializers.Serializer):
    pending_ref = JidanpuNumberField()
    close_unconfirmed = serializers.BooleanField(default=False)
