"""人工核对登录状态，不调用计全付。"""
import getpass
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.settlement_control.locking import LockUnavailable, operation_lock
from apps.settlement_control.models import ManualReview, SecurityEvent, UpstreamSession


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--operator', required=True)
        parser.add_argument('--evidence-file', required=True)
        parser.add_argument('--confirm-no-inflight', required=True, action='store_true')
        parser.add_argument('--confirm-workers-protected', required=True, action='store_true')

    def handle(self, *args, **options):
        if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False) and getattr(connection, 'schema_name', 'public') == 'public':
            raise CommandError('必须通过 tenant_command 指定租户上下文。')
        operator = options['operator'].strip()
        if not operator or len(operator) > 128 or '\n' in operator or '\r' in operator:
            raise CommandError('操作人标识无效。')
        try:
            with Path(options['evidence_file']).open('rb') as f:
                evidence = f.read(10241)
            if not 20 <= len(evidence.strip()) <= 10240:
                raise ValueError
            encrypted = Fernet(settings.SETTLEMENT_FERNET_KEY.encode()).encrypt(evidence)
        except (OSError, ValueError):
            raise CommandError('证据文件或加密密钥无效。') from None
        try:
            with operation_lock(exclusive=True), transaction.atomic():
                row = UpstreamSession.objects.select_for_update().filter(key='agent').first()
                if not row or not row.login_attempt:
                    raise CommandError('没有需要核对的登录；未作修改。')
                ManualReview.objects.create(client='local', request_id=row.login_attempt, operator=operator,
                    process_user=f'{getpass.getuser()}:{os.getuid()}', decision='login_reconciled',
                    evidence_ciphertext=encrypted, evidence_hash=hashlib.sha256(evidence).hexdigest())
                SecurityEvent.objects.create(client='local', action='admin_reconcile_login', result=200)
                row.login_attempt = None
                row.attempted_at = timezone.now()
                row.save(update_fields=['login_attempt', 'attempted_at'])
        except LockUnavailable:
            raise CommandError('无法独占运行锁，请先确认没有请求在执行。') from None
        self.stdout.write('登录核对状态已解除；未调用上游。')
