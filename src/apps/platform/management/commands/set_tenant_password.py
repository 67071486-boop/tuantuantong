"""`set_tenant_password`：为某个租户的初始管理员设置密码。必须有它：`seed_tenant` 刻意把初始 `admin` 建成无可用密码（`set_unusable_password()`）——
同一份 password 哈希灌给多个租户正是跨租户冒充的触发条件（spec §3.4）⇒「新建租户 → 能登录」之间缺的那一环就是本命令。
密码只走交互式 stdin、绝不做成命令行参数（`--password xxx` 会进 shell history、`ps` 进程表、CI 日志）；`getpass` 输入不回显，本命令不打印/不落日志/不写文件，除写进库的哈希外不留任何副本。"""
import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django_tenants.postgresql_backend.base import is_valid_schema_name
from django_tenants.utils import get_public_schema_name, schema_context, schema_exists


class Command(BaseCommand):
    help = "交互式设置某个租户（schema）里某个用户的密码（默认 admin）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            required=True,
            help="目标租户的 schema 名（必须是已存在的租户 schema，不能是 public）",
        )
        parser.add_argument(
            "--username",
            default="admin",
            help="目标用户名（默认 admin，即 seed_tenant 建的初始管理员）",
        )

    def handle(self, *args, **options):
        schema = options["schema"]
        username = options["username"]
        public_schema = get_public_schema_name()

        # public 平面没有租户用户（`seed_tenant` 只在租户 schema 里建 admin）：
        # 放行它会把密码设到一个谁也登不进去的地方，还把"平台管理员"与"租户的管理员"
        # 的语义搅在一起——本命令只管后者。
        if schema == public_schema:
            raise CommandError(f"schema {public_schema!r} 是平台平面，没有租户用户可设密码")

        # `--schema` 由使用者提供、会被送进 `SET search_path`（django-tenants 会加引号，不是注入口）。
        # 这里挡的是另一件事：让本命令接受的名字与 `provision_tenant` 用同一把尺子（`is_valid_schema_name`，
        # 它另外挡住 Postgres 保留的 `pg_` 前缀），避免出现"命令认、供给不认"的名字。
        if not is_valid_schema_name(schema):
            raise CommandError(f"schema {schema!r} 不是合法的 Postgres schema 名")

        # 先校验 schema 存在：否则 `schema_context` 会建出/切进一个空 schema，
        # 然后以一句含糊的 "user does not exist" 报错，把人引向错误的方向。
        if not schema_exists(schema):
            raise CommandError(f"schema {schema!r} 不存在")

        # 两次输入必须一致，避免不可见输入打错一位就把管理员锁在门外。
        first = getpass.getpass(f"为 {schema}/{username} 设置新密码：")
        second = getpass.getpass("再输入一次确认：")
        if first != second:
            raise CommandError("两次输入的密码不一致，未做任何修改")
        if not first:
            raise CommandError("密码不能为空")

        try:
            validate_password(first)
        except ValidationError as exc:
            # `exc.messages` 只说规则（如"密码太短"），**不含密码本身**，可以安全回显。
            raise CommandError("密码不符合要求：" + "；".join(exc.messages)) from None

        with schema_context(schema):
            user_model = get_user_model()
            try:
                user = user_model.objects.get(username=username)
            except user_model.DoesNotExist:
                # 报错里带上用户名与 schema，便于排障；**不含**密码。
                raise CommandError(
                    f"schema {schema!r} 下没有用户 {username!r}"
                ) from None
            user.set_password(first)
            # update_fields：只动 password，不碰 is_staff/groups 等既有状态。
            user.save(update_fields=["password"])

        # 只回显"谁、在哪儿"，绝不含密码本身。
        self.stdout.write(
            self.style.SUCCESS(f"已为 {schema}/{username} 设置密码")
        )
