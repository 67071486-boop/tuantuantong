from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import WxPusherPreference
from apps.business.game_scope import games_of
from apps.core.envelope import api_response
from apps.core.wxpusher_views import WxPusherView
from apps.rbac.permissions import has_node


def _keywords_field():
    return serializers.ListField(child=serializers.CharField(max_length=50), max_length=20, default=list)


class HallGameRuleSerializer(serializers.Serializer):
    game_id = serializers.IntegerField(min_value=1, max_value=2**31 - 1)
    price_min = serializers.DecimalField(12, 2, min_value=Decimal(0), allow_null=True, default=None)
    price_max = serializers.DecimalField(12, 2, min_value=Decimal(0), allow_null=True, default=None)
    include_keywords = _keywords_field()
    exclude_keywords = _keywords_field()

    def validate(self, attrs):
        low, high = attrs['price_min'], attrs['price_max']
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError({'price_max': '最高价不能低于最低价。'})
        return attrs


class HallSubscriptionSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    games = HallGameRuleSerializer(many=True, max_length=200)

    def validate_games(self, rules):
        ids = {rule['game_id'] for rule in rules}
        if len(ids) != len(rules):
            raise serializers.ValidationError('同一款游戏只能保存一套订阅规则。')
        games = games_of(self.context['user']).filter(deleted_at__isnull=True, is_active=True, pk__in=ids)
        if ids and games.count() != len(ids):
            raise serializers.ValidationError('只能订阅你有权限接单的游戏。')
        return rules


def _settings(user, preference):
    if preference is None:
        return {'enabled': False, 'games': []}
    allowed_games = games_of(user).filter(deleted_at__isnull=True, is_active=True).values('pk')
    rules = preference.hall_games.filter(game_id__in=allowed_games).order_by('game_id')
    return {
        'enabled': preference.hall_enabled_since is not None,
        'games': HallGameRuleSerializer(rules, many=True).data,
    }


class WxPusherHallSubscriptionView(WxPusherView):
    permission_classes = [*WxPusherView.permission_classes, has_node('grab_hall.take_order')]

    def get(self, request):
        return api_response(_settings(request.user, WxPusherPreference.objects.filter(pk=request.user.pk).first()))

    @transaction.atomic
    def patch(self, request):
        serializer = HallSubscriptionSerializer(data=request.data, context={'user': request.user})
        serializer.is_valid(raise_exception=True)
        preference, _ = WxPusherPreference.objects.select_for_update().get_or_create(user=request.user)
        now = timezone.now()
        if serializer.validated_data['enabled']:
            preference.hall_enabled_since = preference.hall_enabled_since or now
        else:
            preference.hall_enabled_since = None
        preference.save(update_fields=['hall_enabled_since'])
        rules = serializer.validated_data['games']
        preference.hall_games.exclude(game_id__in=[rule['game_id'] for rule in rules]).delete()
        current = {rule.game_id: rule for rule in preference.hall_games.all()}
        for rule in rules:
            previous = current.get(rule['game_id'])
            fields = {key: value for key, value in rule.items() if key != 'game_id'}
            if previous is None or any(getattr(previous, key) != value for key, value in fields.items()):
                preference.hall_games.update_or_create(
                    game_id=rule['game_id'], defaults={**fields, 'selected_at': now},
                )
        return api_response(_settings(request.user, preference))
