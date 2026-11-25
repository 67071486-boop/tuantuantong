"""Summarize the order-finance runtime's recent local observations."""
from collections import defaultdict
import json
from math import ceil

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.order_finance_runtime import FinanceRuntime


class Command(BaseCommand):
    help = "Read recent local order-finance timing, errors and admission refusals."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--seconds", type=int, default=300)

    def handle(self, *, seconds, **options):
        if not 1 <= seconds <= 300:
            raise CommandError("--seconds must be between 1 and 300")
        if not settings.TT_ORDER_FINANCE_RUNTIME_DIR:
            raise CommandError("TT_ORDER_FINANCE_RUNTIME_DIR is not configured")
        grouped = defaultdict(list)
        try:
            runtime = FinanceRuntime(settings.TT_ORDER_FINANCE_RUNTIME_DIR)
            for tenant, route, status, duration, refused, business_error in runtime.recent(seconds):
                grouped[(tenant, route)].append((status >= 400 or business_error, duration, refused))
        except (OSError, ValueError) as exc:
            raise CommandError("Cannot read the private order-finance runtime directory") from exc

        groups = []
        for (tenant, route), observations in sorted(grouped.items()):
            durations = sorted(value for _, value, _ in observations)
            groups.append({
                "tenant": tenant,
                "route": route,
                "requests": len(observations),
                "errors": sum(failed for failed, _, _ in observations),
                "rejected": sum(refused for _, _, refused in observations),
                "p95_ms": durations[ceil(len(durations) * .95) - 1],
                "max_ms": durations[-1],
            })
        self.stdout.write(json.dumps({"seconds": seconds, "groups": groups}, ensure_ascii=False))
