"""Process authorized and persisted online payroll batches in this tenant database."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business.models import OnlinePayrollTransfer
from apps.business.online_payroll_worker import run_polling_round
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = '推进已确认的在线结算批次并核对逐笔到账结果'

    def add_arguments(self, parser):
        parser.add_argument('--tenant')
        parser.add_argument('--check-rollback', action='store_true')

    def handle(self, *args, **options):
        check_rollback = options['check_rollback']
        if check_rollback and settings.ONLINE_PAYROLL_ENABLED:
            raise CommandError('请先关闭在线结算制单并确认所有进程已读取开关，再回滚到旧版')
        if not check_rollback and not settings.ALIPAY_AUTH_LOCAL_ENABLED:
            return
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if options.get('tenant'):
            tenants = tenants.filter(schema_name=options['tenant'])
            if not tenants.exists():
                raise CommandError('找不到指定工作室')
        for tenant in list(tenants.order_by('schema_name')):
            with tenant_context(tenant):
                if check_rollback:
                    released = (OnlinePayrollTransfer.Status.FAILED, OnlinePayrollTransfer.Status.CLOSED)
                    if OnlinePayrollTransfer.objects.exclude(status__in=released).exists():
                        raise CommandError('有进行中或已付款的在线结算，目标版本缺少防止重复结账的保护，无法回滚')
                else:
                    run_polling_round(lambda batch, reason: self.stderr.write(f'{tenant.schema_name} 批次 {batch}：{reason}'))
