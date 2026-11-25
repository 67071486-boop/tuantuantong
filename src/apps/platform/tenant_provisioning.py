"""租户开通的**可复用服务层**：建库建角色 → 库内安装 → 凭据落盘（spec §3.5 的步骤 ②③）。

## 为什么要有这一层（⛔ 不许把这里的逻辑抄回调用方）

`provision_tenant_db`（命令行）与平台控制台（独立项目 `../ttt-platform` 的 `console`
开通视图，D-0196；它自己有一份 `console/tenant_provisioning.py`）是两个消费者，
要的是**同一条链路**：`provision_database` → 子进程 `install_tenant`。抄两遍的后果是**静默漂移**
（一处补了轮换语义、另一处没补），而两边的失败表现都是「装出来的东西不一样」——与
`AGENTS.md` §5-E 的 E-0027 同族。
⇒ 两个消费者都只调本模块；**CLI 形状留在命令里**：`--emit-env` 的路径打印与前置闸门、`--fleet`
遍历、`--rotate-password` / `--dry-run` 开关、退出码，一律不进这一层。

## 本模块导出什么

- **纯函数**（不碰数据库）：`build_install_command` / `build_install_argv` / `build_install_env` /
  `build_emitted_env` / `render_env_file`。它们原先住在 `provision_tenant_db` 里，T6 搬到这里；
  命令模块**同名再导出** ⇒ 既有回归网（`tests/test_provision_tenant_db.py` 从命令模块导入它们）
  一行都不用改。⚠️ 那个再导出不是「顺手留着」：删掉它那条网会以 ImportError 红。
- **步骤 ②**：`create_tenant_database(...)`——建库建角色 + 补成员资格 + 收掉 PUBLIC 的 `CONNECT`
  （幂等语义全在 `db_provisioning._apply_provision_statements` 里，这里**只是**加了一层
  「可选轮换口令」）。
- **步骤 ③**：`install_tenant_in_database(...)`——**子进程**在目标库里跑 `install_tenant`。
- **凭据落盘**：`write_credentials_file(...)`（0600）。命令的 `--emit-env` 与控制台的交付**共用**
  它：`O_EXCL`/`O_TRUNC`、`fchmod(0o600)` 那套写两遍必漂移。
- **控制台交付**：`default_tenant_credentials_path()` / `write_tenant_credentials()`。

## 步骤 ③ 为什么还是**子进程**（⛔ 不要「顺手优化」）

理由与 `provision_tenant_db` 模块 docstring 的 R1 条一字不变：换 Django 连接别名要动
`connections` 注册表与 `migrate_schemas --database` 的交互；而子进程**正是租户服务器将来真实的
运行方式**，并且命令序列可以被 `build_install_argv` / `build_install_env` 两个纯函数断言。

## `rotate_password` 是显式开关：两条消费者的语义**不同**，⛔ 别合并

- **命令**（不给 `--rotate-password`）⇒ **不轮换**。命令可能只是在重跑一个已存在角色的租户，
  而「本次生成的口令到底有没有写进库里」由它自己的 `--emit-env` 两道前置闸门管着。
- **控制台** ⇒ 传 `True`。交付是**必做**的（spec §7-Q3：每次都写出 0600 凭据文件），而角色若在
  上一次尝试里已经建出来，`provision_database` **不会**重置口令（`CREATE ROLE` 被跳过）⇒ 不轮换
  就会写出一份「库里不认」的凭据。传 `True` 让「本次生成的口令 == 库里的口令」成为不变量，
  这也是「同一个表单再交一次要能接着干完」能成立的原因。
  ⚠️ 代价（如实登记）：重交一次会**轮换**该租户的口令，上一份凭据立即失效——但**同一路径**的
  凭据文件会被本次重写，所以「文件与库」始终一致（文件是唯一被交付的那份）。
"""
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings

from apps.platform.db_provisioning import (
    provision_database,
    rotate_role_password,
    tenant_database_name,
    tenant_role_name,
)
from apps.platform.identifiers import validate_schema_name


class TenantInstallError(RuntimeError):
    """步骤 ③ 失败（子进程非 0 退出）。

    **预期**失败的一种（域名已被占用、库名不对、租户迁移报错…），带子进程 stderr **原文**。
    调用方（命令 / 控制台）各自决定怎么呈现它：命令翻成 `CommandError`，控制台把原文显示给
    操作员（spec §3.5：失败要给原始错误，⛔ 不吞成业务错误码）。
    """


def build_install_env(*, database: str, role: str, password: str) -> dict[str, str]:
    """子进程要覆盖的环境变量：让 `install_tenant` 打到**这个租户的库**上。

    主机/端口跟应用库同一台（供给的本就是同一集群里的另一个库）。返回值**不含**
    `DJANGO_SECRET_KEY`——命令不替租户服务器编密钥，也不把平台那把带进去。
    """
    application_database = settings.DATABASES["default"]
    return {
        "POSTGRES_DB": database,
        "POSTGRES_USER": role,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_HOST": str(application_database["HOST"]),
        "POSTGRES_PORT": str(application_database["PORT"]),
    }


def build_emitted_env(
    *, database: str, role: str, password: str, host: str, port: str
) -> dict[str, str]:
    """凭据文件的那五个键（**应用机视角**）。

    与 `build_install_env` 的区别只在主机/端口：那个给**本机的** `install_tenant` 子进程用（走隧道），
    这个要给**租户机器**用（走云联网内网）。两个地址混用是本线最容易踩的一处静默错误。
    """
    return {
        "POSTGRES_DB": database,
        "POSTGRES_USER": role,
        "POSTGRES_PASSWORD": password,
        "POSTGRES_HOST": host,
        "POSTGRES_PORT": port,
    }


def build_install_command() -> list[str]:
    """跑 `install_tenant` 的基础 argv（以命令名结尾；参数由 `build_install_argv` 追加）。"""
    return [sys.executable, str(settings.BASE_DIR / "manage.py"), "install_tenant"]


def build_install_argv(*, schema_name: str, name: str, domain: str) -> list[str]:
    """完整 argv = 基础 argv + `--schema` / `--name` / `--domain`。"""
    return build_install_command() + [
        "--schema",
        schema_name,
        "--name",
        name,
        "--domain",
        domain,
    ]


def render_env_file(env: dict[str, str], *, header: str) -> str:
    """把 env 片段渲染成 `.env` 文本（键顺序 = dict 顺序，稳定、可断言）。

    `header` 由调用方给：命令行那份与平台控制台那份要各自说清**是谁生成的**、
    ⛔ 不许把 `POSTGRES_ADMIN_*` 复制过去（两处 header 文本见各自的常量）。
    """
    return header + "".join(f"{key}={value}\n" for key, value in env.items())


#: 平台控制台写出的租户凭据文件头。与 `provision_tenant_db` 的 `ENV_FILE_HEADER` **同口径、
#: 不同出处**：读文件的人要能一眼看出它是**控制台**生成的（⛔ 不要照抄命令那份的抬头）。
CONSOLE_ENV_FILE_HEADER = (
    "# 由平台控制台（开通新租户）生成的租户服务器数据库凭据（本文件权限 0600）。\n"
    "# DJANGO_SECRET_KEY 不在这里：每个租户服务器必须有自己的密钥，请在该服务器上自行生成。\n"
    "# ⛔ 本文件只放**该租户自己**的 POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD；\n"
    "#    租户服务器的 .env 里**绝不能**出现 POSTGRES_ADMIN_USER / POSTGRES_ADMIN_PASSWORD /\n"
    "#    POSTGRES_ADMIN_DB 这三行——那个运维身份能连每一个租户库（RUNBOOK §2.5.11）。\n"
)


def default_tenant_credentials_path(schema_name: str) -> Path:
    """控制台交付这个租户凭据的默认落点：`<仓库根>/secrets/tenant-<schema>.env`。

    选 `secrets/`：它已进 `.gitignore`（真实凭据不进仓库）。
    ⚠️ **平台库自己那份凭据现在由独立项目写**：路径口径在 `../ttt-platform` 的
    `provision_platform_db`（本仓那份引导命令已于 随平台侧迁出，见 `D-0196`）。

    ⚠️ **文件名里的 `schema_name` 不是注入面**：它到得了这里就说明已经过了
    `validate_schema_name` 的严格白名单（小写字母/数字/下划线），不含 `/` 与 `..`
    ⇒ 派生出的路径不可能逃出 `secrets/`。⛔ 不要为了「支持别的名字」把白名单放松。
    """
    return Path(settings.REPO_ROOT) / "secrets" / f"tenant-{schema_name}.env"


def write_credentials_file(
    *, path: str | Path, env: dict[str, str], header: str, force: bool
) -> None:
    """写 0600 的凭据文件（命令行 `--emit-env` 与控制台交付**共用**这一份实现）。

    `O_EXCL`（不给 force）/ `O_TRUNC`（给 force）之外还**显式** `fchmod(0o600)`：
    目标已存在且旧权限是 0644 时，`os.open` 的 mode 参数不生效（它只在创建时用），
    覆盖后就会留下一份「同组可读」的凭据文件。
    """
    text = render_env_file(env, header=header)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        remaining = text.encode("utf-8")
        while remaining:
            remaining = remaining[os.write(fd, remaining) :]
    finally:
        os.close(fd)


def write_tenant_credentials(
    *,
    schema_name: str,
    database: str,
    role: str,
    password: str,
    host: str,
    port: str,
) -> str:
    """控制台的凭据交付（spec §7-Q3）：写 0600 文件，**返回路径**（页面只显示这个路径）。

    - 父目录 `secrets/` 不存在时按 0700 建出来（与平台侧那条引导命令同规格）；
    - `force=True`：控制台每次开通/重交都已经把口令轮换成本次这份（见模块 docstring），
      ⛔ 若这里不覆盖，文件里留下的就是**上一份**口令 ⇒ 交付出去的凭据是错的。
      「不许覆盖别人写的文件」这件事由**落点固定为 `<仓库根>/secrets/tenant-<schema>.env`**
      保证：那是本功能自己的文件，不是用户给的路径。
    - ⚠️ **进函数第一件事是再跑一次 `validate_schema_name`**（M-11，T6）：这个参数
      **直接决定文件落点**（`<仓库根>/secrets/tenant-<schema>.env`）⇒ 它是**路径注入面**，
      ⛔ 不能靠「调用方一定先过了闸门」这种约定——`../` 或绝对路径形状能让文件写到 `secrets/`
      之外。`default_tenant_credentials_path` 的 docstring 里的那条论证（「到得了这里就说明
      已经过了白名单」）**只对控制台那条调用链成立**，本函数是公开的可复用层，自己再挡一次。
    """
    # 白名单闸门（小写字母 / 数字 / 下划线，1–63；挡 `public` / `platform` / `pg_` 前缀）。
    # 放在最前：名字不合法时**一个目录都不建、一个字节都不写**（异常原样冒泡，
    # 它是 `ValueError` 子类、属**预期**失败，调用方自己决定怎么呈现）。
    validate_schema_name(schema_name)

    path = default_tenant_credentials_path(schema_name)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_credentials_file(
        path=path,
        env=build_emitted_env(
            database=database,
            role=role,
            password=password,
            host=host,
            port=port,
        ),
        header=CONSOLE_ENV_FILE_HEADER,
        force=True,
    )
    return str(path)


def create_tenant_database(
    *, schema_name: str, password: str, rotate_password: bool = False
) -> tuple[str, str]:
    """spec §3.5 的**步骤 ②**：建库建角色（幂等），可选把口令轮换成本次这份。

    返回 `(database, role)`（两个派生值，调用方不必自己再算一遍——算两遍就会漂移）。

    ⚠️ `create_role` 被跳过的语义在 `provision_database` 里（角色已存在 ⇒ 口令**不**被重置）；
    这也是 `rotate_password=True` 存在的唯一理由（控制台必须交付一份库里认的口令）。
    `ALTER ROLE`（轮换）只能排在 `provision_database` **之后**：角色不存在时它会直接报错，
    而 `provision_database` 正是那个保证角色存在的步骤。
    """
    provision_database(schema_name=schema_name, password=password)
    if rotate_password:
        rotate_role_password(schema_name=schema_name, password=password)
    return tenant_database_name(schema_name), tenant_role_name(schema_name)


def install_tenant_in_database(
    *, schema_name: str, name: str, domain: str, password: str
) -> str:
    """spec §3.5 的**步骤 ③**：用**子进程**在目标库里跑 `install_tenant`，返回子进程 stdout。

    失败（子进程非 0 退出）⇒ 抛 `TenantInstallError`，信息里带**退出码 + stderr 原文**
    （⛔ 不带 stdout：`install_tenant` 的成功文案里有 schema/域名，那些不是机密，
    但把两股输出混在一起会让排错时看不出哪句是错误）。

    ⚠️ 这里**不接** `except Exception`：只有「子进程跑完了但退出码非 0」是本函数能判定的失败。
    其它异常（`os.environ` 缺键、argv 构造失败…）照原样冒泡（`AGENTS.md` §5-E：未预期异常
    不许伪装成业务结果）。
    """
    database = tenant_database_name(schema_name)
    role = tenant_role_name(schema_name)
    argv = build_install_argv(schema_name=schema_name, name=name, domain=domain)
    env = {
        **os.environ,
        **build_install_env(database=database, role=role, password=password),
    }
    try:
        result = subprocess.run(
            argv,
            env=env,
            cwd=str(settings.BASE_DIR),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise TenantInstallError(
            f"install_tenant 在库 {database!r} 内执行失败（退出码 {exc.returncode}）；"
            f"stderr 原文：\n{(exc.stderr or '').strip()}"
        ) from exc
    return result.stdout.strip()
