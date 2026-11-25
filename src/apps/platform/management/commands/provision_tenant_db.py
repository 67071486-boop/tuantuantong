"""`provision_tenant_db`：端到端供给一个租户（建库建角色 → 库内安装 → 产出凭据）。顺序固定：解析条目（`--fleet` 或 `--schema/--name/--domain`）→ 打印 SQL（密码脱敏成 `***`）→ `--dry-run` 到此为止 →
`--emit-env` 两道前置闸门（顺序承重：a. 角色已存在且没给 `--rotate-password` ⇒ 拒绝——`provision_database` 对已有角色是跳过，本次生成的密码没写进库，写出去就是「库里不认」的凭据（D-0080）；b. 目标文件已存在且没给 `--force` ⇒ 拒绝；`--force` 不绕过 a）→ `provision_database` 建角色/建库/收 PUBLIC `CONNECT` → 子进程跑 `install_tenant` → 写 0600 凭据。
⚠️ 用子进程而不是换 Django 连接（有意取舍，⛔ 别顺手优化）：子进程正是租户服务器将来真实的运行方式，且命令序列可被三个纯函数断言。⚠️ 残差：重跑已存在角色又没给 `--rotate-password` 时子进程仍用本次生成的密码连 ⇒ 密码认证集群上会以认证失败响亮停下（不是静默装错库），要重跑请显式 `--rotate-password`。「建库建角色 → 子进程 install_tenant → 写凭据」已抽到 `apps/platform/tenant_provisioning.py`（两个消费者一条链路，行为与输出一字不变）；四个纯函数从本模块再导出——`tests/test_provision_tenant_db.py` 从本模块导入，删掉再导出会以 ImportError 红。"""
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.platform.db_provisioning import (
    build_provision_statements,
    generate_password,
    role_exists,
)
from apps.platform.fleet import FleetEntry, FleetError, entry_for_schema, load_fleet
from apps.platform.tenant_provisioning import (
    TenantInstallError,
    build_emitted_env,
    build_install_argv,  # 再导出：既有回归网从本模块导入（本模块自己不再调用）
    build_install_command,  # 同上
    build_install_env,  # 同上
    create_tenant_database,
    install_tenant_in_database,
    write_credentials_file,
)

#: 打印 SQL 时替换密码的占位串。stdout 会进终端 / CI 日志，而真实凭据只该落在 0600 的
#: `--emit-env` 文件里；形状（`PASSWORD '...'`）保留，读的人仍能看懂这条 SQL 在干什么。
REDACTED_PASSWORD = "***"

#: `--emit-env` 写出的文件头。`DJANGO_SECRET_KEY` 刻意不在文件里：每个租户服务器必须有自己的密钥（部署口径见 RUNBOOK）。
#: ⚠️ 爆炸半径：这个文件会被复制到租户服务器上，而 `POSTGRES_ADMIN_*` 是能连每一个租户库的身份——一旦跟着文件过去，隔离承诺即告失效。
ENV_FILE_HEADER = (
    "# 由 provision_tenant_db 生成的租户服务器数据库凭据（本文件权限 0600）。\n"
    "# DJANGO_SECRET_KEY 不在这里：每个租户服务器必须有自己的密钥，请在该服务器上自行生成。\n"
    "# ⛔ 本文件只放**该租户自己**的 POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD；\n"
    "#    租户服务器的 .env 里**绝不能**出现 POSTGRES_ADMIN_USER / POSTGRES_ADMIN_PASSWORD /\n"
    "#    POSTGRES_ADMIN_DB 这三行——那个运维身份能连每一个租户库（RUNBOOK §2.5.11）。\n"
)


class Command(BaseCommand):
    help = "端到端供给一个租户：建库建角色 → 在库内跑 install_tenant → 按需产出 .env 片段"

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            required=True,
            help="租户 schema 名（--fleet 模式下用它从清单里选条目）",
        )
        parser.add_argument("--name", help="租户名称（非 --fleet 模式必填）")
        parser.add_argument("--domain", help="租户的独享域名（非 --fleet 模式必填）")
        parser.add_argument(
            "--fleet",
            metavar="PATH",
            help="从该车队清单文件里取名字与域名（此时不许再给 --name/--domain）",
        )
        parser.add_argument(
            "--emit-env",
            dest="emit_env",
            metavar="PATH",
            help=(
                "把该租户服务器的数据库凭据写成权限 0600 的 .env 片段"
                "（主机/端口用 --emit-host/--emit-port 覆盖）"
            ),
        )
        parser.add_argument(
            "--emit-host",
            dest="emit_host",
            metavar="HOST",
            help=(
                "写进 --emit-env 文件的主机（应用机视角）。不给则用 POSTGRES_HOST；"
                "从隧道后面跑供给时必须显式给，否则文件里会是隧道地址"
            ),
        )
        parser.add_argument(
            "--emit-port",
            dest="emit_port",
            metavar="PORT",
            help="写进 --emit-env 文件的端口（应用机视角）。不给则用 POSTGRES_PORT",
        )
        parser.add_argument(
            "--dry-run",
            dest="dry_run",
            action="store_true",
            help="只打印将要执行的 SQL，不建库、不建角色、不写文件",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="允许覆盖已存在的 --emit-env 目标（不能绕过 --rotate-password 的凭据闸门）",
        )
        parser.add_argument(
            "--rotate-password",
            dest="rotate_password",
            action="store_true",
            help="对已存在的角色执行 ALTER ROLE ... PASSWORD（重跑并要产出凭据时必须显式给）",
        )

    def handle(self, *args, **options):
        entry = self._resolve_entry(options)
        password = generate_password()
        emit_env = options["emit_env"]
        # 形状闸门：在任何输出 / DDL / dry-run 退出**之前**（见 _resolve_emit_address 的 docstring）。
        emit_host, emit_port = self._resolve_emit_address(options)

        self.stdout.write(
            f"将供给租户 schema={entry.schema_name}（库 {entry.database}，角色 {entry.role}，"
            f"域名 {entry.domain}，名称 {entry.name}）"
        )
        self.stdout.write("将要执行：")
        for statement in build_provision_statements(
            role=entry.role,
            database=entry.database,
            password=REDACTED_PASSWORD,
            # 与实际供给同源：dry-run 打印的 `GRANT … TO` 必须就是真跑时会授权的那一句。
            operator=settings.POSTGRES_ADMIN_USER,
        ):
            self.stdout.write(f"  {statement.as_string(None)}")

        if options["dry_run"]:
            self.stdout.write("dry-run：没有建库、没有建角色、没有写文件")
            if emit_env:
                # ⛔ dry-run **不评估** --emit-env 的两道前置闸门（角色已存在 / 目标文件已存在），
                # 所以「dry-run 退出 0」不等于「真实运行也会过」。明说，别让运维拿它当前置检查。
                self.stdout.write(
                    "⚠️ 预检未评估：--emit-env 的前置闸门（角色是否已存在 / 目标文件是否已存在）"
                    "本次没有检查（dry-run 不连库、不碰文件）；真实运行会在任何 DDL 之前先拒。"
                )
                self.stdout.write(
                    "⚠️ dry-run 不产出凭据文件；--emit-host/--emit-port 只做**形状**校验"
                    "（空串会拒），不连库、也不校验那个地址连不连得通。"
                )
            return

        if emit_env:
            self._check_emit_env_preconditions(
                entry=entry,
                emit_env=emit_env,
                rotate=options["rotate_password"],
                force=options["force"],
            )

        # 步骤 5–6（T6 起在 `tenant_provisioning` 里，语义与打印顺序一字未变）：
        # 建库建角色（+ 按 `--rotate-password` 轮换口令）→ 子进程在目标库里跑 install_tenant。
        create_tenant_database(
            schema_name=entry.schema_name,
            password=password,
            rotate_password=options["rotate_password"],
        )

        self._run_install_tenant(entry=entry, password=password)

        if emit_env:
            self._write_env_file(
                emit_env,
                build_emitted_env(
                    database=entry.database,
                    role=entry.role,
                    password=password,
                    host=emit_host,
                    port=emit_port,
                ),
                force=options["force"],
            )
            self.stdout.write(
                self.style.SUCCESS(f"凭据已写入 {emit_env}（权限 0600）")
            )

        if options["rotate_password"]:
            self.stdout.write(
                self.style.WARNING(f"已轮换角色 {entry.role} 的密码")
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"已供给租户 schema={entry.schema_name} 库={entry.database} 域名={entry.domain}"
            )
        )

    def _resolve_emit_address(self, options) -> tuple[str, str]:
        """解析写进凭据文件的主机/端口（应用机视角），返回 `(host, port)`。完全不传（`None`）⇒ 回退 `settings.DATABASES["default"]`，与今天逐字相同；
        ⛔ 显式给了空串 ⇒ 响亮报错、不许静默回退：`--emit-host "$APP_HOST"` 而变量没设是最常见的形态，静默回退写出的正是要消除的那个（Mac/隧道侧）地址、且无任何痕迹。
        这是参数形状闸门（与 `--emit-env` 的角色/文件两道闸不是一回事）⇒ 连 `--dry-run` 也必须响亮报错，且在任何输出与任何 DDL 之前。"""
        application_database = settings.DATABASES["default"]
        resolved = []
        for flag, value, fallback in (
            ("--emit-host", options["emit_host"], application_database["HOST"]),
            ("--emit-port", options["emit_port"], application_database["PORT"]),
        ):
            if value is None:
                resolved.append(str(fallback))
            elif not str(value).strip():
                raise CommandError(
                    f"{flag} 给的是空串：显式给了却没有值会**静默回退**成 Mac 侧 settings 的地址，"
                    "那正是从隧道后面供给时要消除的那个（租户机器拿到连不上）。"
                    "要么给一个真实值，要么整条参数都不给。"
                )
            else:
                # ⚠️ 落盘也必须用 `strip()` 后的值：上面那道闸门按 `strip()` 判空，若这里写原值，
                # 从控制台复制来的地址（常带尾随空格）会**过闸门**却把带空格的地址写进 0600
                # 凭据文件 ⇒ 租户机器连不上，且肉眼看不出来（地址看起来是对的）。
                resolved.append(str(value).strip())
        return resolved[0], resolved[1]

    def _resolve_entry(self, options) -> FleetEntry:
        """按 `--fleet` 与否解析出唯一一个 `FleetEntry`（清单问题翻成 `CommandError`）。"""
        schema = options["schema"]
        fleet_path = options["fleet"]

        if fleet_path:
            if options["name"] or options["domain"]:
                raise CommandError(
                    "--fleet 模式不许再给 --name/--domain：名字与域名以清单为唯一出处，"
                    "两个出处不许静默取一个"
                )
            try:
                return entry_for_schema(load_fleet(Path(fleet_path)), schema)
            except FleetError as exc:
                raise CommandError(str(exc)) from exc

        if not options["name"] or not options["domain"]:
            raise CommandError("非 --fleet 模式必须同时给 --schema / --name / --domain")
        try:
            # `FleetEntry` 构造时自己过闸门（派生名长度 + 严格白名单），命令行这一路也走它。
            return FleetEntry(
                schema_name=schema, name=options["name"], domain=options["domain"]
            )
        except FleetError as exc:
            raise CommandError(str(exc)) from exc

    def _check_emit_env_preconditions(self, *, entry, emit_env, rotate, force):
        """写凭据文件之前的两道闸门，顺序承重（见模块 docstring 第 4 条）。"""
        if not rotate and role_exists(entry.role):
            raise CommandError(
                f"角色 {entry.role} 已存在：provision_database 不会重置已存在角色的密码，"
                "本次生成的密码没有写进库里 ⇒ 拒绝写出 --emit-env 凭据文件"
                "（那会是一份库里不认的密码）。要真的轮换请加 --rotate-password；"
                "--force 不能绕过这一条（它只覆盖文件）。"
            )
        if os.path.exists(emit_env) and not force:
            raise CommandError(
                f"--emit-env 目标已存在：{emit_env}（拒绝覆盖；要覆盖请加 --force）"
            )

    def _run_install_tenant(self, *, entry, password):
        """子进程在目标库里跑 `install_tenant`；失败时把 stderr 原文带进 `CommandError`。
        argv/环境/错误措辞都在 `tenant_provisioning.install_tenant_in_database`；本方法只把那条预期失败翻成 `CommandError`——措辞与抽出前逐字相同（既有用例钉着）。"""
        try:
            install_stdout = install_tenant_in_database(
                schema_name=entry.schema_name,
                name=entry.name,
                domain=entry.domain,
                password=password,
            )
        except TenantInstallError as exc:
            raise CommandError(str(exc)) from exc
        if install_stdout:
            self.stdout.write(install_stdout)

    def _write_env_file(self, path, env, *, force):
        """写 0600 的 `.env` 片段（实现与平台控制台共用，见 `tenant_provisioning`）。
        ⚠️ 文件头用本命令自己的 `ENV_FILE_HEADER`：读文件的人要能看出它是命令行生成的。"""
        write_credentials_file(
            path=path, env=env, header=ENV_FILE_HEADER, force=force
        )
