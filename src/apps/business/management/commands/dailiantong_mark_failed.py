"""`dailiantong_mark_failed`：人工把一条 `unknown` 推送标成 `failed`（审计修复 P1-5）。 python manage.py dailiantong_mark_failed --tenant <schema> --push-id 12 \ --operator <用户名> --evidence "已登录代练通后台按单号和发布时间核对，没有这张单"
## 为什么需要它 `unknown` = 「超时 / 没拿到上游单号，不知道到底建没建单」。按真钱红线，这一档 **永不自动重试**（`docs/DAILIANTONG-API.md` §6.1 / R11：外部单号查不回来，重发 = 重复建单）。但如果人工核对上游**确实没有**这张单，旧实现没有任何出口：这一行永远挡着下一次抛单，页面也不会自己变。本命令是**唯一**的出口，不是自动重试：
- 只接受 `state=unknown`（改其它状态一律中文业务错误）； - `--evidence`（核对依据）**必填**且至少 5 个字； - 成功后 `state=failed` + `is_active=False`，本地订单可以重新抛单； - 写一条审计（动作人 = `--operator` 指定的租户内用户），审计里带核对依据。 ## 边界 - `--tenant` 必填：这是排障 / 人工命令，⛔ 不接受请求来源的租户标识（`AGENTS.md` §4）； - `--push-id` 只在那个租户库里查（走 `tenant_context`）； - ⛔ 不访问网络、⛔ 不自动重试、⛔ 不改本地订单状态。"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import tenant_context

from apps.business.dailiantong_push import (
    MANUAL_FAILURE_EVIDENCE_MIN_LENGTH,
    DailiantongPushError,
    mark_unknown_push_failed,
)
from apps.business.models import DailiantongPush
from apps.platform.models import Tenant


class Command(BaseCommand):
    help = "人工把一条「状态未知」的代练通推送标记为失败（受审计；之后可重新抛单）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", required=True, help="租户 schema（必填；⛔ 不跑全部租户）"
        )
        parser.add_argument(
            "--push-id", required=True, type=int, help="要标记的推送行 id"
        )
        parser.add_argument(
            "--operator", required=True, help="执行人工核对的操作员用户名（写进审计）"
        )
        parser.add_argument(
            "--evidence",
            required=True,
            help=(
                "核对依据（至少 "
                f"{MANUAL_FAILURE_EVIDENCE_MIN_LENGTH} 个字）：说明怎么确认上游没有建单"
            ),
        )

    def handle(self, *args, **options):
        schema = (options.get("tenant") or "").strip()
        evidence = (options.get("evidence") or "").strip()
        if not schema:
            raise CommandError("必须指定 --tenant（本命令不跑全部租户）")

        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(f"没有这个租户 schema：{schema}")

        with tenant_context(tenant):
            operator = (
                get_user_model()
                .objects.filter(username=(options.get("operator") or "").strip())
                .first()
            )
            if operator is None:
                raise CommandError(
                    "这个租户里没有该用户名的操作员：" + (options.get("operator") or "")
                )
            push = DailiantongPush.objects.filter(pk=options["push_id"]).first()
            if push is None:
                raise CommandError(
                    f"这个租户里没有推送行 id={options['push_id']}"
                )
            try:
                mark_unknown_push_failed(push, operator=operator, evidence=evidence)
            except DailiantongPushError as exc:
                raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"推送行 {push.pk} 已标记为失败（操作员：{operator.username}）；"
            "本地订单现在可以重新抛单。"
        )
