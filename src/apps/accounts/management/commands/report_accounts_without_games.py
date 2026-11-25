"""上线前巡检：哪些**活跃**账号会看不到任何数据（只读，一个字节都不写库）。

**为什么需要它**：业务方 2026-09-23 选了**严格**口径——「没有档案 / 没勾任何游戏 ⇒
什么都看不到」。这是本线唯一**会让人看不到数据**的变更，所以上线前要先知道面有多大：
把「有档案但零游戏」与「没有档案」的活跃账号**先补上游戏权限**，否则他们会看不到任何单。

**只读**：本命令只 `SELECT`（账号、档案、档案—游戏关联）+ `print`，没有
`save()` / `update()` / `delete()` / `create()`，也不把连接留在别人的 schema 上
（每个租户用 `tenant_context` 进出，退出即还原——两条都有用例钉住，
见 `tests/test_game_scope.py` 的 `test_command_is_read_only` 与
`test_context_does_not_leak_after_the_command`）。

**范围**：`public` 里每个**非 public** 的租户行（`apps.platform.models.Tenant` 属
SHARED_APPS，从当前连接所在的 schema 也读得到它）。
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.platform.models import Tenant

#: 「有档案但零游戏」：档案在、但该档案的游戏关联一行都没有（存量里可能有这种行）。
WITH_PROFILE_NO_GAMES_LABEL = "有档案但零游戏"
#: 「没有档案」：`StaffProfile` 的反向行不存在。
NO_PROFILE_LABEL = "没有档案"


class Command(BaseCommand):
    help = "只读巡检：打印每个租户里「有档案但零游戏」「没有档案」的活跃账号数与用户名清单。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            default=None,
            help="只巡检这一个 schema（默认巡检所有租户）。",
        )

    def handle(self, *args, **options):
        only = options["schema"]
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if only is not None:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")
        for tenant in tenants.order_by("schema_name"):
            with tenant_context(tenant):
                self._report_schema(tenant.schema_name)

    def _report_schema(self, schema_name):
        active = get_user_model().objects.filter(is_active=True)
        # ⚠️ 「没有游戏」= 关联表里一行都没有：`games__isnull=True` 是 LEFT JOIN 上的判据
        # ——对「有档案但零游戏」为真、对健康账号为假（写「档案存在」会把健康账号也数进来）。
        with_profile_no_games = active.filter(
            staff_profile__isnull=False, staff_profile__games__isnull=True
        )
        no_profile = active.filter(staff_profile__isnull=True)

        self.stdout.write(schema_name)
        self._print_bucket(WITH_PROFILE_NO_GAMES_LABEL, with_profile_no_games)
        self._print_bucket(NO_PROFILE_LABEL, no_profile)

    def _print_bucket(self, label, users):
        names = sorted(users.values_list("username", flat=True))
        self.stdout.write(f"  {label}: {len(names)}")
        for name in names:
            self.stdout.write(f"    - {name}")
