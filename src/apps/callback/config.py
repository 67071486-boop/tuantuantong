"""回调接收器的运行时配置：**路径与密钥从哪里取**。

⛔ **本模块不 import Django**（也不读 `settings`）：接收器由 gunicorn 以
`apps.callback.wsgi:application` 直接起，**没有** `DJANGO_SETTINGS_MODULE`、也不该为了一个
回调去初始化整个 Django 应用（那条路会带上 `django-tenants` 的按 Host 认租户中间件，
而 `callback.suqing.chat` 不是任何租户的域名）。同一份常量给管理命令复用 —— 两处各写一遍
必然会漂移（一个读 `shared/callback/events.jsonl`、另一个读别处，排查时"看不到数据"）。

## 四个取值（都可以用环境变量覆盖，默认值 = 生产形态）

| 用途 | 环境变量 | 默认值 |
|---|---|---|
| 部署根 `$R` | `CALLBACK_ROOT` | `/home/ubuntu/projects/tuantuantong` |
| 事件文件 | `CALLBACK_EVENTS_PATH` | `$R/shared/callback/events.jsonl` |
| 密钥文件（0600，**旧路径**那一把） | `CALLBACK_ENV_FILE` | `$R/shared/callback.env` |
| 「码 → 密钥」文件（0600，**新路径**，见下节） | `CALLBACK_KEYS_FILE` | `$R/shared/callback-keys.env` |

⚠️ **环境变量在每次调用时读**（⛔ 不做模块级缓存）：测试要能在同一个进程里换路径，
而"缓存过一次就再也不看"是这类模块最常见的静默坑（改了 `CALLBACK_EVENTS_PATH` 却还写老地方）。

## ⭐ 新增：按**回调码**取密钥（`shared/callback-keys.env`）

回调地址（spec §5.1）是 `https://<该租户自己的域名>/api/jidanpu/callback/<每绑定一串随机码>`
⇒ 接收器要能**从路径里取出那串码**、据此找到**该绑定**对应的那把 `client_secret`。
这是**另一份文件**，⛔ **不动** `callback.env` 一个字节：

> 为什么不复用 `callback.env`（spec §5.3 明写）：那里是**一把**密钥，而这条路要的是
> 「**哪个码用哪把密钥**」的**映射**；塞进同一个文件会把接收器线那个**已被验收过的**
> 单一密钥形状改掉（那条线的判据与运维手册都按它写的）⇒ 另起一个文件。

**文件形状（⛔ T3「保存绑定时写进去」那一侧必须逐字对齐这一份）**：

```
# 团团通 · 鸡蛋铺官方 API 回调密钥（0600；由「保存绑定」自动维护；⛔ 人工改要重启接收器）
<22 位回调码>\t<该绑定的 client_secret>
```

**格式与查找方式（这就是「码 → 密钥」的契约）**：

1. **一行一条**，码与密钥之间是**一个制表符 `TAB`**（`<码>\t<密钥>`）：
   ⛔ 不是空格、⛔ 不是 `=`（那会把"码里有没有空格"变成一个要猜的问题）；
2. 以 `#` 开头的行 = 注释（首行那句是**固定**的，见上面那段文本）；**空行忽略**；
3. 码 = **恰好 22 个** `A-Za-z0-9-_` 字符（`secrets.token_urlsafe(16)`，spec §5.1）；
4. **查找 = 与路径里取出的那串码「全等」比对**（对**行首字段**做全等，⛔ 不做前缀 / 模糊 /
   大小写归一 —— 码是定长的，归一化只会把两个不同的码混成一个）；
5. 同一个码出现多次 ⇒ **第一条胜出**。T3 的写入是「读 + 改 + 原子替换」⇒ 正常不会有重复行；
   人工编辑造出来的重复行按**第一行**算，⛔ 不去猜哪一行"更新"；
6. 某行**没有 TAB**、或**密钥部分是空的** ⇒ 这一条**不是**可用密钥
   （调用方按「这个码取不到密钥」处理，见 `load_callback_secret`）；
7. 行尾的 `\r` 容掉（Windows 上编辑过的文件）；⛔ 其余空白**不**做归一化（密钥里若有空白，原样保留）。

⚠️ **不做模块级缓存**（每次调用重读整份文件）：T3 写完立刻生效 ⇒ **⛔ 不需要重启接收器**
（首行注释里那句"人工改要重启"是照 spec §5.3 的形状抄的，对**人工改**只是保守说法）。
⚠️ 读侧在 T3 做 `os.replace` 那一瞬间会读到**旧文件或新文件**（两者都完整，替换是原子的）
⇒ 最坏是"刚保存的绑定头几秒验签失败回 401"（spec §5.3 第 2 条；F23「对方会不会重试」**未定**，如实挂着）。

## 密钥（`clientSecret`）的两条硬口径

1. **只从那个 0600 文件读**，⛔ **不从进程环境读**：systemd 的 `Environment=` / `EnvironmentFile=`
   会把值落进 `systemctl show` 与 `/proc/<pid>/environ`（同机器上任何用户都读得到；
   `AGENTS.md` §4 的「不提交真实密钥」在这里的等价物是「不让它出现在任何可被读到的第二处」）。
2. **值绝不进日志、绝不进错误消息、绝不进落盘记录**：本模块只回一个 `str`，
   抛的异常只带**文件路径**，连"文件里有几行"都不说。判据在 `src/tests/test_callback_receiver.py`
   （它拿一个哨兵密钥跑一遍，再断言事件文件与命令输出里都搜不到那个哨兵）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: 部署根。⚠️ 与 `deploy/*.sh` 里的 `R=/home/ubuntu/projects/tuantuantong` 是同一个东西。
DEFAULT_ROOT = "/home/ubuntu/projects/tuantuantong"

ENV_ROOT = "CALLBACK_ROOT"
ENV_EVENTS_PATH = "CALLBACK_EVENTS_PATH"
ENV_ENV_FILE = "CALLBACK_ENV_FILE"
ENV_KEYS_FILE = "CALLBACK_KEYS_FILE"

#: 密钥文件里的变量名（`deploy/activate.sh` 那套 `*.env` 同形：一行 `K=V`）。
SECRET_VAR_NAME = "CALLBACK_JIDANPU_CLIENT_SECRET"

#: 事件文件相对 `$R` 的路径（业务方 定，**不许改**——它同时是"整块搬走"的抓手）。
EVENTS_RELATIVE = ("shared", "callback", "events.jsonl")

#: 密钥文件相对 `$R` 的路径。
ENV_FILE_RELATIVE = ("shared", "callback.env")

#: ⭐「码 → 密钥」文件相对 `$R` 的路径（spec §5.3）：**另一份**文件，⛔ 不塞进 `callback.env`。
KEYS_FILE_RELATIVE = ("shared", "callback-keys.env")

#: 码与密钥之间的分隔符（spec §5.3 的 `<码><TAB><密钥>`）。⚠️ 唯一定义，⛔ 别在别处再写一个字面量。
KEYS_FILE_SEPARATOR = "\t"


class SecretUnavailable(RuntimeError):
    """读不到 `clientSecret`（文件不在 / 不可读 / 没有那一行）。

    ⚠️ 调用方必须**当成"验不了签"处理**（fail-closed：非 2xx + 记录里标 `sign_ok=false`），
    ⛔ 不许降级成"跳过验签、照样回 200"——那是本仓最防的「假成功」。
    """


class CallbackCodeUnknown(RuntimeError):
    """路径里的那串码**取不到可用密钥**（文件里没有这个码 / 那一行没有密钥）。

    ⚠️ 与 `SecretUnavailable` **必须分得清**（spec §5.4 第 2 条、§13.1 的 A3 对 A4；两个类
    **互不为子类**，⛔ 别让一个继承另一个 —— 那会让 `except` 的先后顺序变成承重逻辑）：

    - 本异常 = 这条回调**归不到任何能验签的绑定**上 ⇒ 回 **401**（与验签失败同一档：
      对第三方来说两件事没有区别，都是"这条报文验不了签"）；
    - `SecretUnavailable` = **我们的**文件不在 / 读不了 ⇒ 回 **503**（那是我们的配置问题，
      回 401 等于对第三方说"你的签名错了" —— 那是**说假话**，会把排查方向带偏）。

    ⛔ 不许把两者合成一个码（接收器 spec §4.1 的既有裁决），也⛔ 不许回 200。
    """


def callback_root() -> Path:
    """部署根 `$R`（每次调用都重新读环境变量）。"""
    return Path(os.environ.get(ENV_ROOT) or DEFAULT_ROOT)


def events_path() -> Path:
    """事件文件（一行一条 JSON）的绝对路径。"""
    override = os.environ.get(ENV_EVENTS_PATH)
    if override:
        return Path(override)
    return callback_root().joinpath(*EVENTS_RELATIVE)


def env_file_path() -> Path:
    """密钥文件（0600）的绝对路径。"""
    override = os.environ.get(ENV_ENV_FILE)
    if override:
        return Path(override)
    return callback_root().joinpath(*ENV_FILE_RELATIVE)


def callback_keys_path() -> Path:
    """⭐「码 → 密钥」文件（0600）的绝对路径；形状与格式见模块 docstring。"""
    override = os.environ.get(ENV_KEYS_FILE)
    if override:
        return Path(override)
    return callback_root().joinpath(*KEYS_FILE_RELATIVE)


def _parse_env_file(text: str) -> dict[str, str]:
    """把 `K=V` 形状的文本解析成 dict（容忍 `export ` 前缀、引号、CRLF、`#` 注释）。"""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'").strip()
    return values


def _warn_if_world_readable(path: Path) -> None:
    """密钥文件除了属主之外还可读时，**响亮**提醒一句（⛔ 不回显值、也不因此拒服务）。

    ⚠️ 只是提醒、不是判据：文件模式取决于运维怎么放文件，而**拒服务**会让回调全变成 5xx。
    """
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:  # pragma: no cover - 文件刚被删掉的竞态
        return
    if mode & 0o077:
        logger.warning(
            "回调密钥文件 %s 的权限是 %o（属主之外也可读）⇒ 建议 chmod 600", path, mode
        )


def load_client_secret() -> str:
    """读出 `clientSecret`。读不到 ⇒ 抛 `SecretUnavailable`（消息里**只有路径**）。

    ⛔ 返回的这个字符串**只许**在内存里参与签名计算：不许写日志、不许进异常文本、
    不许进事件记录（`src/tests/test_callback_receiver.py` 有哨兵用例守着这三条）。
    """
    path = env_file_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretUnavailable(
            f"读不到回调密钥文件 {path}（{exc.__class__.__name__}）⇒ 验不了签，"
            "本机应先把 0600 的密钥文件放好再启用接收器"
        ) from exc
    _warn_if_world_readable(path)
    secret = _parse_env_file(text).get(SECRET_VAR_NAME, "")
    if not secret:
        raise SecretUnavailable(f"回调密钥文件 {path} 里没有 {SECRET_VAR_NAME}=…（值为空或整行缺失）")
    return secret


def events_dir() -> Path:
    """事件文件所在目录（`shared/callback/`）。"""
    return events_path().parent


def parse_callback_keys(text: str) -> dict[str, str]:
    """把「码 → 密钥」文件的**文本**解析成 `{码: 密钥}`（格式见模块 docstring，⛔ 别在这里放宽）。

    规则就三条（其余一律**不当成条目**，⛔ 不猜）：

    - `#` 开头的行与空行 ⇒ 忽略；
    - 必须含 `TAB`：`码<TAB>密钥` ⇒ 取**第一段**为码、**其余全部**为密钥
      （⚠️ `partition` 而不是 `split`：密钥里万一有 TAB 也不会被切掉）；
    - 同一个码出现多次 ⇒ **第一条胜出**（⛔ 不覆盖，理由见 docstring 第 5 条）。

    ⚠️ **不做任何归一化**（不去空格、不转大小写）：码是定长的 22 字符，归一化只会把两个不同的码
    混成一个 —— 那种"宽容"在本仓属于**假绿**（把不该匹配的匹配上）。

    ⚠️ 本函数**不判断码的格式是否合法**：格式判断在路径解析那一侧（`apps/callback/wsgi.py`）。
    这里宽松一格的理由是：T3 写进去的码就来自我们自己的生成函数，而"文件里有一个形状怪的码"
    最坏的后果只是它在路径里永远取不到（路径那一侧会 404）。
    """
    mapping: dict[str, str] = {}
    # ⚠️ 按 `\n` 切、单独容掉 `\r`（⛔ 不用 `str.splitlines()`：它还会在 `\x0b` / `\u2028`
    # 这些字符上切 —— 密钥里若有这类字符，用 `splitlines` 会把一行切成两行）。
    for raw_line in text.split("\n"):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line or line.lstrip().startswith("#"):
            continue
        if KEYS_FILE_SEPARATOR not in line:
            continue
        code, _, secret = line.partition(KEYS_FILE_SEPARATOR)
        if not code or code in mapping:
            continue
        mapping[code] = secret
    return mapping


def load_callback_secret(code: str) -> str:
    """⭐ 按**回调码**取出那把 `client_secret`（spec §5.4 第 2 条）。

    - 文件读不到 ⇒ 抛 `SecretUnavailable`（**503** 那一档：我们的配置问题）；
    - 文件里没有这个码、或那一行没有密钥 ⇒ 抛 `CallbackCodeUnknown`（**401** 那一档）；
    - 两个异常类的边界**不许合**（理由见 `CallbackCodeUnknown` 的 docstring）。

    ⛔ 返回的这个字符串**只许**在内存里参与签名计算：不许写日志、不许进异常文本、不许进事件记录
    （`src/tests/test_callback_receiver.py` 有哨兵用例守着；异常消息里**只有路径与码**，⛔ 没有密钥）。
    """
    path = callback_keys_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SecretUnavailable(
            f"读不到回调密钥文件 {path}（{exc.__class__.__name__}）⇒ 验不了签，"
            "本机应先把 0600 的密钥文件放好再启用接收器"
        ) from exc
    _warn_if_world_readable(path)
    mapping = parse_callback_keys(text)
    secret = mapping.get(code, "")
    if not secret:
        # 两种情形回**同一个码**（对第三方来说没区别：这条回调归不到任何能验签的绑定上），
        # 但**消息**分开写 —— 排查时要能一眼分辨"压根没这个码"与"有这一行但密钥是空的"
        # （后者通常意味着 T3 写文件那一侧出了错）。
        if code in mapping:
            raise CallbackCodeUnknown(
                f"回调密钥文件 {path} 里有 {code} 这一行、但**没有密钥值** ⇒ 这个绑定的回调验不了签"
            )
        raise CallbackCodeUnknown(f"回调密钥文件 {path} 里没有这个回调码 ⇒ 这条回调不认识")
    return secret
