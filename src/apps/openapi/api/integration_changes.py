from rest_framework import serializers, status

from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.integration_sync.feed import ExpiredCursor, InvalidCursor, read_page
from apps.openapi.api.integration_auth import IntegrationView


class ChangeQuery(serializers.Serializer):
    cursor = serializers.CharField(required=False, max_length=512)
    page_size = serializers.IntegerField(required=False, default=50, min_value=1, max_value=100)
    start = serializers.ChoiceField(required=False, choices=["now"])

    def validate(self, attrs):
        if "cursor" in attrs and "start" in attrs:
            raise serializers.ValidationError("cursor 和 start 请只传其中一个")
        return attrs


class ChangeListView(IntegrationView):
    def get(self, request):
        query = ChangeQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        values = query.validated_data
        try:
            page = read_page(
                app_key=request.auth.app_key,
                cursor=values.get("cursor"),
                page_size=values["page_size"],
                start_now=values.get("start") == "now",
            )
        except InvalidCursor:
            return api_response(code=ErrorCode.VALIDATION, msg="变更进度无效")
        except ExpiredCursor:
            return api_response(
                code=status.HTTP_410_GONE, status=status.HTTP_410_GONE,
                msg="变更进度已过期，请重新读取全量数据",
            )
        return api_response(page)
