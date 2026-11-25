"""仅供持独立中控数据库凭据的运维人员查看待核对项。"""
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Q

from apps.settlement_control.models import Authorization, SecurityEvent


class Command(BaseCommand):
    help = '只读列出需要人工核对的授权记录，不重试或修改上游。'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True, help='精确的受控客户端标识')

    def handle(self, *args, **options):
        client = options['client']
        if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False) and (getattr(connection, 'schema_name', 'public') == 'public' or client != 'local'):
            raise CommandError('本地模式必须指定当前租户上下文与 local 客户端。')
        review = Q(phase__in=['unknown', 'creating', 'authorizing', 'refreshing', 'identity_review', 'identity_authorizing', 'identity_refreshing', 'revoking'])
        if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False):
            from apps.business.models import AlipaySettlementAuthorization
            processing = AlipaySettlementAuthorization.objects.filter(requires_platform_review=True, last_error='操作处理中，请稍后重试').values('request_id')
            review |= Q(request_id__in=processing)
        records = Authorization.objects.filter(review, client=client).order_by('updated_at')
        for row in records.iterator():
            self.stdout.write(json.dumps({'request_id': str(row.request_id), 'phase': row.phase, 'unknown_action': row.unknown_action, 'review_code': row.review_code, 'app_id': row.app_id, 'app_name': row.app_name, 'next_action': row.next_action, 'updated_at': row.updated_at.isoformat()}, ensure_ascii=False))
        SecurityEvent.objects.create(client=client, action='admin_list_unknown', result=200)
