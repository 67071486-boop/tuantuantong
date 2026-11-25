"""人工核对后开放原请求重试；不调用上游或绑定已有应用。"""
import getpass
import hashlib
import os
from pathlib import Path
import uuid

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import F

from apps.settlement_control.locking import LockUnavailable, operation_lock
from apps.settlement_control.models import Authorization, ManualReview, SecurityEvent


class Command(BaseCommand):
    help = '凭上游结果及无在途证据，仅解锁符合条件的原请求。'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True)
        parser.add_argument('--request-id', required=True, type=uuid.UUID)
        parser.add_argument('--operator', required=True)
        parser.add_argument('--evidence-file', required=True)
        parser.add_argument('--resolution', required=True, choices=['app_absent', 'authorization_unavailable', 'refresh_reconciled', 'identity_reconciled', 'revocation_reconciled', 'write_not_sent'], help='按上游实际证据核对操作结果')
        parser.add_argument('--confirm-upstream-conclusion', required=True, action='store_true', help='确认已核实所选上游结论')
        parser.add_argument('--confirm-no-inflight', required=True, action='store_true', help='确认上游和本机均无在途请求')
        parser.add_argument('--confirm-workers-protected', required=True, action='store_true', help='确认所有工作进程在同机使用同一锁文件')

    def handle(self, *args, **options):
        if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False) and getattr(connection, 'schema_name', 'public') == 'public':
            raise CommandError('必须通过 tenant_command 指定租户上下文。')
        if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False) and options['client'] != 'local':
            raise CommandError('本地模式只允许核对本租户记录。')
        if not all(options[name] for name in ('confirm_upstream_conclusion', 'confirm_no_inflight', 'confirm_workers_protected')):
            raise CommandError('必须确认上游结论、无在途请求、所有工作进程使用同一把锁。')
        operator = options['operator'].strip()
        if not operator or len(operator) > 128 or '\n' in operator or '\r' in operator:
            raise CommandError('操作人标识无效。')
        try:
            with Path(options['evidence_file']).open('rb') as file:
                evidence = file.read(10241)
            if not 20 <= len(evidence.strip()) <= 10240:
                raise ValueError
            encrypted = Fernet(settings.SETTLEMENT_FERNET_KEY.encode()).encrypt(evidence)
        except (OSError, ValueError):
            raise CommandError('证据文件或中控密钥无效。') from None
        try:
            with operation_lock(exclusive=True), transaction.atomic():
                row = Authorization.objects.select_for_update().filter(client=options['client'], request_id=options['request_id']).first()
                if not row:
                    raise CommandError('记录不存在；未作修改。')
                processing = False
                if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False):
                    from apps.business.models import AlipaySettlementAuthorization
                    processing = AlipaySettlementAuthorization.objects.filter(request_id=row.request_id, requires_platform_review=True, last_error='操作处理中，请稍后重试').exists()
                if options['resolution'] == 'app_absent':
                    valid = not row.app_id and ((row.phase == 'unknown' and row.unknown_action == 'create_app') or (row.phase == 'creating' and not row.unknown_action))
                    next_phase = 'new'
                elif options['resolution'] == 'authorization_unavailable':
                    valid = bool(row.app_id) and ((row.phase == 'unknown' and row.unknown_action == 'agreement_authorize') or (row.phase == 'authorizing' and not row.unknown_action))
                    next_phase = 'app_created'
                elif options['resolution'] == 'refresh_reconciled':
                    valid = bool(row.app_id) and ((row.phase == 'unknown' and row.unknown_action == 'refresh') or (row.phase == 'refreshing' and not row.unknown_action))
                    next_phase = 'identity_review' if row.review_code else (row.status if row.status in {'authorized', 'signed'} else 'pending')
                elif options['resolution'] == 'identity_reconciled':
                    valid = bool(row.app_id) and ((row.phase == 'unknown' and row.unknown_action in {'identity', 'identity_refresh'}) or (row.phase in {'identity_authorizing', 'identity_refreshing'} and not row.unknown_action) or (processing and row.phase == 'authorized'))
                    next_phase = 'signed'
                elif options['resolution'] == 'revocation_reconciled':
                    valid = bool(row.app_id) and ((row.phase == 'unknown' and row.unknown_action == 'revoke') or (row.phase == 'revoking' and not row.unknown_action) or (processing and row.phase == 'revoked'))
                    next_phase = 'revoked'
                else:
                    valid = processing and row.phase in {'signed', 'authorized'} and AlipaySettlementAuthorization.objects.filter(request_id=row.request_id, status=row.status).exists()
                    next_phase = row.phase
                if not valid:
                    raise CommandError('记录状态与结论不符；未作修改。')
                ManualReview.objects.create(client=row.client, request_id=row.request_id, operator=operator, process_user=f'{getpass.getuser()}:{os.getuid()}', decision=options['resolution'], evidence_ciphertext=encrypted, evidence_hash=hashlib.sha256(evidence).hexdigest())
                SecurityEvent.objects.create(client=row.client, action='admin_reconcile_attempt', result=200)
                row.phase, row.unknown_action = next_phase, ''
                if options['resolution'] == 'identity_reconciled':
                    row.status, row.configured, row.next_action = 'signed', False, 'identity'
                elif options['resolution'] == 'revocation_reconciled':
                    row.status, row.configured, row.next_action = 'revoked', False, 'agreement'
                elif options['resolution'] not in {'refresh_reconciled', 'write_not_sent'}:
                    row.status, row.configured = 'pending', False
                row.save(update_fields=['phase', 'status', 'unknown_action', 'configured', 'next_action', 'updated_at'])
                if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False):
                    from apps.business.models import AlipaySettlementAuthorization

                    AlipaySettlementAuthorization.objects.filter(request_id=row.request_id).update(revision=F('revision') + 1)
                    AlipaySettlementAuthorization.objects.filter(
                        request_id=row.request_id, requires_platform_review=True,
                        last_error='操作结果待平台核对，请勿重复操作',
                    ).update(requires_platform_review=False, last_error='')
                    if options['resolution'] in {'identity_reconciled', 'revocation_reconciled'}:
                        AlipaySettlementAuthorization.objects.filter(request_id=row.request_id).update(status=row.status, next_action='identity' if row.status == 'signed' else '', requires_platform_review=False, last_error='', authorized_at=None)
                    if options['resolution'] == 'write_not_sent':
                        AlipaySettlementAuthorization.objects.filter(request_id=row.request_id, requires_platform_review=True, last_error='操作处理中，请稍后重试').update(requires_platform_review=False, last_error='')
        except LockUnavailable:
            raise CommandError('无法独占运行锁；可能有调用进行中或锁未配置。') from None
        self.stdout.write('已解除原请求的核对锁；未调用上游。')
