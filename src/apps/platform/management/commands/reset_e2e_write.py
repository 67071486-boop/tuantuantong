"""`reset_e2e_write`：把一次性写租户「推倒重建 + 灌夹具」（spec `docs/specs/2026-09-25-write-mode-audit-design.md` §4）。唯一职责：清空重建专用一次性写租户，供写模式审计每次跑前调用（写模式真改数据，不重置则后一个职位面对被改烂的租户）。
⛔ 本命令是本线唯一会 DROP 租户库的地方，三道闸门 fail-closed：① 库名闸门——目标必须恰好是常量 `tt_e2e_write`（`_assert_write_db` 纯函数、可单测），⛔ 不接受任何指向别的库的参数（`--schema` 之类都没有，参数化目标正是把闸门变松的形态）；② DEBUG 闸门（形状同 `seed_e2e`）；③ DROP 原语自己的确认串——`drop_tenant_database` 要求确认串逐字等于库名，本命令传另一来源的常量 `WRITE_DATABASE` ⇒ 两个来源一致才会真执行 DROP。
步骤（顺序固定）：`drop_tenant_database`（运维身份，`DROP DATABASE … WITH (FORCE)` → `DROP ROLE`）→ `provision_tenant_db`（建库建角色 → 库内 `install_tenant` → 写 0600 凭据）→ 起子进程带租户凭据跑 `seed_e2e --schema e2e_write` → 再跑 `seed_e2e_write_orders`。⚠️ 灌夹具必须起子进程、带刚写出的凭据（执行期实测踩到，⛔ 别"顺手优化"成 `call_command`）：租户目录（`Tenant`/`Domain`）住在租户自己那个库的 `public` 里，而 `seed_e2e` 走进程内 default 连接（应用库）⇒ 进程内直调会报 `schema 不存在`（首次真实运行就这么失败的）；凭据来源 = 本命令刚让 `provision_tenant_db` 写出的 0600 文件。"""
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from apps.platform.db_provisioning import drop_tenant_database, tenant_database_name

# ── 一次性写租户的常量（**单一出处**；spec §2 的表就是本命令的事实来源）──────────────
#: 租户 schema 名。库 / 角色名由 `{TENANT_DB_PREFIX}{schema}` 派生 ⇒ 与下面的库名常量互为交叉校验。
WRITE_SCHEMA = "e2e_write"
#: 库名（= 角色名，PG 里两者是独立命名空间）。闸门 `_assert_write_db` 认的就是这个字面值。
WRITE_DATABASE = "tt_e2e_write"
#: 租户独享域名。
WRITE_DOMAIN = "e2e-write.test.local"
#: 租户名称（只写进 `Tenant.name`，给人看；不是判据）。
WRITE_TENANT_NAME = "E2E 写模式一次性租户"
#: 凭据落点。`provision_tenant_db` 以 0600 写出（本命令只负责把父目录建出来）。
WRITE_ENV_FILE = Path(settings.REPO_ROOT) / ".tools/e2e/tenant-write.env"


def _assert_write_db(name: str) -> None:
    """闸门（纯函数）：目标库名必须恰好是 `WRITE_DATABASE`，否则响亮拒绝。
    ⛔ 只认字面值、不做任何派生/前缀/大小写宽松（`tt_e2e_write_extra`、`TT_E2E_WRITE` 一律拒）——对只读租户库 `tt_e2e` 误跑一次就会清掉 E2E 基线数据；抽成纯函数为了被 `tests/test_reset_e2e_write.py` 直接钉住。
    🔎 已知边界：本闸门只判库名、⛔ 不判 `POSTGRES_HOST`/`POSTGRES_PORT`（DROP 走环境里的运维连接）——手工把凭据指向另一台服务器上同名的库时不会察觉；现实风险低（凭据由 `provision_tenant_db` 本机写出、0600、从不手工编辑）⇒ 刻意不为此拆闸门架构，登记于 spec §7。"""
    if name != WRITE_DATABASE:
        raise CommandError(
            f"reset_e2e_write 只认一次性写租户库 {WRITE_DATABASE!r}，收到的是 {name!r}"
            "（拒绝对任何别的库执行 DROP —— 尤其别把它指到只读租户库 / 开发库上）。"
        )


def _read_credentials(path: Path) -> dict[str, str]:
    """解析凭据文件（`KEY=VALUE` 行，`#` 与空行跳过）——形状与 `e2e/config.mjs::readTenantEnv` 一致。
    缺文件/缺键时响亮失败：静默拿到空 dict 会让子进程回落到应用库凭据，那正是"灌错库"的形态（`apps/platform/db_provisioning.py` 同一条教训）。"""
    required = (
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
    )
    if not path.is_file():
        raise CommandError(f"凭据文件不存在：{path}（供给本该已经写出它）")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        separator = line.find("=")
        if separator == -1:
            continue
        values[line[:separator]] = line[separator + 1 :]
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise CommandError(f"凭据文件 {path} 缺少键 {missing!r}（拒绝让子进程回落到应用库）")
    return values


class Command(BaseCommand):
    help = (
        "把一次性写租户推倒重建并灌 E2E 夹具"
        f"（固定目标 库={WRITE_DATABASE} schema={WRITE_SCHEMA}；DEBUG=false 时拒绝；不接受目标参数）"
    )

    # ⛔ 刻意**不**实现 `add_arguments`：任何"选择目标"的参数都是闸门的松动面。
    # 目标库名由模块常量给出，改动只能改代码、改完要过 `_assert_write_db` 与测试。

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "reset_e2e_write 只在 DEBUG=true 下运行——它会 DROP 一个租户库，"
                "绝不能碰非开发环境。"
            )

        # 两道库名闸门、两个**不同来源**：字面常量、以及由 schema 派生出来的库名。
        # 前者挡「常量被改成别的库」，后者挡「schema 常量漂移导致派生名不再是写租户库」。
        _assert_write_db(WRITE_DATABASE)
        derived_database = tenant_database_name(WRITE_SCHEMA)
        _assert_write_db(derived_database)

        self.stdout.write(
            f"将推倒重建一次性写租户：库={derived_database} schema={WRITE_SCHEMA} 域名={WRITE_DOMAIN}"
        )

        # 运维身份 + `DROP DATABASE … WITH (FORCE)` + `DROP ROLE` 全在既有原语里
        # （`apps/platform/db_provisioning.py`）。`confirm` 传**字面常量**而不是 `derived_database`：
        # 让原语自己再比一次两个独立来源，值不一致时它先于任何 SQL 拒绝。
        drop_tenant_database(schema_name=WRITE_SCHEMA, confirm=WRITE_DATABASE)
        self.stdout.write(f"已 DROP 库与角色 {derived_database}")

        # `provision_tenant_db` 写凭据时用 `O_EXCL`（目标已存在即拒）⇒ 第二次跑必须给 `--force`：
        # 本命令的契约就是"每次都重建"，凭据文件每次都会被覆盖成新的密码。
        WRITE_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        call_command(
            "provision_tenant_db",
            schema=WRITE_SCHEMA,
            name=WRITE_TENANT_NAME,
            domain=WRITE_DOMAIN,
            emit_env=str(WRITE_ENV_FILE),
            force=True,
        )
        # 灌夹具走既有命令（它自己的 schema 白名单里含 e2e_write，见 seed_e2e.E2E_SCHEMAS）。
        # ⚠️ 必须**带着刚写出的租户凭据起子进程**：理由见模块 docstring（目录行在租户库里，
        #    进程内的 default 连接指着应用库）。
        self._run_seed_e2e()
        # 再补**订单夹具**（T2b）：`seed_e2e` 一笔订单都不造 ⇒ 订单页行内控件全渲染不出来，
        # 而订单域正是写模式存在的理由。⚠️ 顺序**必须**在 `seed_e2e` 之后——本命令只造订单，
        # 用的游戏 / 分类 / 项目 / 账号全是 `seed_e2e` 建的（缺一个它会响亮拒绝）。
        self._run_seed_write_orders()

        # 收尾复核权限：`provision_tenant_db` 用 `fchmod(0600)` 写出，但这里**再验一次**——
        # 凭据文件一旦可被同组/其他用户读到，等价于口令泄漏，而它不会自己发出任何声音。
        # ⚠️ 这一句排在成功文案**之前**：验不过就不该先说"✅ 已重建"。
        mode = os.stat(WRITE_ENV_FILE).st_mode & 0o777
        if mode != 0o600:
            raise CommandError(
                f"凭据文件 {WRITE_ENV_FILE} 的权限是 {oct(mode)}，不是 0600 —— "
                "它含数据库口令，绝不能让同组/其他用户读到。"
            )

        # ⛔ 不打印口令：口令只落在 0600 的凭据文件里，屏幕上只报**落点**。
        self.stdout.write(
            self.style.SUCCESS(
                f"✅ 一次性写租户已重建：库={derived_database} 角色={derived_database} "
                f"schema={WRITE_SCHEMA} 域名={WRITE_DOMAIN}；"
                f"凭据已写入 {WRITE_ENV_FILE}（权限 0600）"
            )
        )

    def _run_seed_e2e(self):
        """用租户自己的凭据起子进程跑 `seed_e2e --schema e2e_write`（理由见模块 docstring）。
        argv 与 `provision_tenant_db` 起 `install_tenant` 同构（`sys.executable` + `manage.py`，cwd=`BASE_DIR`）：本仓已有的"换库跑命令"标准姿势，不另立一套。"""
        self._run_tenant_command("seed_e2e")

    def _run_seed_write_orders(self):
        """在 `seed_e2e` 之后再起一次子进程跑 `seed_e2e_write_orders`（订单夹具）。
        ⚠️ 与 `_run_seed_e2e` 同一姿势、同一份凭据（进程内 default 连接指着应用库，两条都必须打到租户库）。
        ⚠️ 顺序承重：订单夹具要挂 `seed_e2e` 建的游戏/分类/项目/账号，缺一个它自己会响亮拒绝（⛔ 不会静默造一笔挂空的订单）。"""
        self._run_tenant_command("seed_e2e_write_orders")

    def _run_tenant_command(self, command_name: str):
        """用租户自己的凭据起子进程跑一条租户库内的管理命令。两条命令（`seed_e2e`/`seed_e2e_write_orders`）共用这一处 argv 与错误翻译
        （⛔ 不各写一份——漂移的后果是"灌错库"）；失败时把子进程 stderr 原文带进 `CommandError`（排障要靠它）。"""
        credentials = _read_credentials(WRITE_ENV_FILE)
        argv = [
            sys.executable,
            str(settings.BASE_DIR / "manage.py"),
            command_name,
            "--schema",
            WRITE_SCHEMA,
        ]
        try:
            result = subprocess.run(
                argv,
                env={**os.environ, **credentials},
                cwd=str(settings.BASE_DIR),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise CommandError(
                f"{command_name} 在库 {credentials['POSTGRES_DB']!r} 内执行失败"
                f"（退出码 {exc.returncode}）；stderr 原文：\n{(exc.stderr or '').strip()}"
            ) from exc
        if result.stdout.strip():
            self.stdout.write(result.stdout.strip())
