"""事件的落盘：**追加写的一行一条 JSON**（`shared/callback/events.jsonl`）。

## 为什么是文件、不是数据库（三条理由，业务方 定的形态）

1. **这一版的语义还没定**（回调失败要不要重试、响应超时算不算失败，第三方文档都没写）
   ⇒ 先不设计表结构：**原样留档最不会丢信息**。表一旦建了，字段选错就等于把信息丢在门外。
2. **要能整块搬走**：以后搬去独立服务器时，整个"搬家"就是 `cp` 一个文件（外加一次 DNS 改动）。
3. **租户机器上没有运维身份**（`AGENTS.md` §4：`POSTGRES_ADMIN_USER` 绝不能出现在租户服务器上）
   ⇒ 在这台机器上**建不了新库**。⛔ 不要为了"顺手建张表"去试。

## 并发安全怎么做的（两件一起上，⛔ 别只留一件）

- `O_APPEND` 打开：偏移由内核放到文件末尾（Linux 上对普通文件的 `write` 还持 inode 锁）；
- **外加 `fcntl.flock(LOCK_EX)`** 把"一条记录的多次 `write`"整体罩住：
  `write` 理论上可以短写（返回小于请求的字节数），那时必须续写第二笔 —— 若没有锁，
  另一台 worker 的字节就会插进这一行的中间，产出**坏行**（那正是要防的）。
  ⚠️ `flock` 是**承重**的那一半；`O_APPEND` 是"即使有人把锁删了也不至于写到文件中间"的兜底。

## ⭐ 按大小轮转（分段），且**偏移是全文件连续的**（H-1）

生产上 `events.jsonl` 已到 **198MB / 十几万行**。无轮转有两个后果：① 单文件无限膨胀
（攻击者写盘放大后尤其危险，H-1）；② `callback_events` 这类工具越读越慢。

**设计承重：轮转不能破坏消费侧的字节偏移读**（`jidanpu_openapi_consume` 把
`callback_file_offset` 存在绑定上，按 `open("rb")+seek` 增量读）。所以：

- 轮转 = **把当前文件整块改名成归档段** `<事件文件名>.<全局起始字节>`，再建一个空的新
  `events.jsonl`；归档段里每一行的**全局字节位置**不变（文件名里那个数字就是该段第一字节
  的全局偏移）；
- `read_records_from(path, offset)` 把「归档段 + 现役文件」看成一整条**逻辑流**：先按各段
  的 `(base, size)` 定位 `offset` 落在哪一段、从段内对应位置读起，读到段尾**无缝续到下一
  段**，`records` / `line_ends` / `new_offset` 全部是**全局偏移**。消费侧**一个字都不用改**
  就能跨轮转继续读（它只认偏移在变长）——**三件不变量**：①已验签的行一行不丢（归档段还在，
  任何落在旧偏移之后的行都读得到）；②不崩（段边界只在完整行处，轮转在接收器锁内改名）；
  ③不重复消费（偏移单调、只往后读）。
- 轮转**只由接收器在写锁内做**（见下），所以「改名那一刻有没有别的 worker 正拿着旧文件
  句柄往归档段里写」这个问题不存在（所有 append 都排在同一个 `flock` 后面）。
- ⭐ **归档段按保留期自动删**（业务方拍板：**保留 24 小时**，处理完的旧分段删掉；
  实现见 `SEGMENT_RETENTION_SECONDS` / `prune_segments`）。三条闸一起上：
  ① 只删**已完全消费**的段（段尾 ≤ 消费侧当前全局偏移；段尾 = **下一段的起始字节**）；
  ② 只删**段龄 ≥ 保留期**的段（段龄 = 该段最后一笔写入的 `mtime`）；
  ③ **最后一段永远不删**（「最后一段」= **最新那个归档段**）。⚠️ 这一条是**承重**的：
  `live_base`（现役文件段首 = 高水位的地基）由**现存段**算出来 ⇒ 把归档段删光会让
  `high_water_bytes` 塌回现役文件大小 ⇒ 消费侧误判「文件被搬动」并清水位。留一段就守住了坐标。
  删除由**消费侧**在每轮推进完偏移之后触发（`jidanpu_openapi_consume`），⛔ 不在接收器的
  写热路径上；拿 `LOCK_EX`（与轮转 / append 同一把锁）⇒ 不会与轮转或写并发；删失败只
  `logger.warning`、⛔ 不抛（清理是旁路，不许把这一轮消费弄挂）。
- ⚠️ **删段会让 `snapshot().total_bytes` 变小**（现存字节），所以消费侧判「文件被搬动过 /
  变短了」**只许认 `snapshot().high_water_bytes`**（累计写入字节：轮转与清理都不会让它变小）。
  ⛔ 不许把那条判据换回 `total_bytes` —— 换回去 = 每次清理都触发「水位清零 + 重读现存段」。
- ⚠️ **被删掉的偏移再被要**（理论上不该发生：删的前提是已消费；真实来源 = 同一台机器上另一份
  绑定的 `callback_file_offset` 落后超过保留期）：`read_records_from` 遇到
  `0 < offset < 现存最早段的段首` ⇒ **响亮抛 `OffsetOutOfRetention`**，⛔ 不夹到段首、⛔ 不静默
  从头读（那两种都会让「有人要读已经删掉的范围」从日志里消失 = 静默丢）。⚠️ 从**现存最早段**
  起读（`snapshot().earliest_base`）仍可回溯：保留期窗口内的行该读得到就读得到。

## 写入持久性

旧单机接收器写入页缓存后回响应。独立接收器验签成功后调用 `fsync`，
只在文件和目录均同步后回响应。主动回读在两种部署中仍然是订单状态的依据。

## 记录形状

一行一条 JSON（`ensure_ascii=False` ⇒ 中文原样落盘，文件能直接 `grep`；
JSON 会把字符串里的换行转义成 `\\n` ⇒ **一条记录一定只占一行**，这是 JSONL 在这里成立的前提）。
形状的定义与字段含义见 spec §4；`apps/callback/wsgi.py` 是唯一的写入方。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.callback import config

logger = logging.getLogger(__name__)

#: 事件文件与目录的权限：目录 0700、文件 0600（报文可能有业务信息，别给同机其他用户读）。
#: ⚠️ **两处都要**（`DIR_MODE` 给 `mkdir`、`FILE_MODE` 给 `os.open` **和** `os.fchmod`）：
#: `os.open(..., mode)` / `Path.mkdir(mode=...)` 里的 mode 会**被进程 `umask` 削**，
#: 而 `os.fchmod` 不受 umask 影响（照本仓既有做法：`jidanpu_openapi._atomic_write` /
#: `platform.tenant_provisioning._write_credentials` 都是「open 给一遍 + fchmod 给一遍」）。
DIR_MODE = 0o700
FILE_MODE = 0o600

#: 现役文件的写锁（`events.jsonl.lock`）：**轮转与 append 共用这一把**。
#: ⚠️ 为什么不再拿数据文件自己的 `flock`：轮转会把数据文件**改名**，而另一个 worker 可能
#: 早先打开了同一个 inode（改名后它的 fd 指向归档段）⇒ 那笔 append 会写进归档段（消费侧
#: 永远读不到 = 丢行）。锁一把**不随轮转改名**的锁文件，这个窗口才真正关掉。
LOCK_SUFFIX = ".lock"

#: 现役文件到这么大就轮转（字节）。默认 256 MiB：生产当前 198MB，正常流量下轮转很罕见，
#: 而攻击面已经被「验签前不落原文 + 按 IP 限流」收掉了大半。
#: 轮转阈值（⚠️ 业务方拍板 256 MiB → **64 MiB**：现网事件文件已到 198MB，
#: 若沿用 256 MiB 则**永远不会轮转** ⇒ 24 小时保留期清理也永不生效、磁盘只增不减。
#: 取 64 MiB 的代价：归档段变多（每 64MB 一个），但消费侧按全局偏移跨段读、与段数无关；
#: 保留期清理按段删，段越小越平滑。⛔ 别调回 GB 级——那等于关掉保留期。
ROTATE_BYTES = 64 * 1024 * 1024

#: 归档段的**保留期**（秒）：业务方拍板「保留 24 小时，处理完的旧分段删掉」。
#: ⚠️ 这是「段龄」的上限，不是「什么时候删」的闹钟：删除由消费侧每轮顺手触发一次，
#: 是否真删由 `prune_segments` 的三条闸决定（已消费 + 超期 + 不是最后一段）。
SEGMENT_RETENTION_SECONDS = 24 * 3600

#: 保留期的环境变量覆盖（排查 / 演练时临时缩短；⛔ 不用它当常态开关）。
#: 非法值（空串 / 非整数 / ≤ 0）⇒ 回落到 `SEGMENT_RETENTION_SECONDS` 并记一条 warning：
#: ⚠️ **≤ 0 也当非法**（0 = 「一消费完立刻删」，写错一个数字就把留档删光的风险不该存在；
#: 真想关掉清理就把它设成一个很大的数）。
ENV_SEGMENT_RETENTION = "CALLBACK_SEGMENT_RETENTION_SECONDS"

#: 归档段文件名里那个全局起始偏移的解析（`events.jsonl.<数字>`）。
#: ⚠️ 只认**纯数字**后缀：锁文件（`.lock`）与运维手工放的备份（`.bak`）不会被当段读。
_SEGMENT_RE = re.compile(r"^(\d+)$")


class OffsetOutOfRetention(RuntimeError):
    """要读的**起点早于现存最早的归档段**（那一段已被保留期清理删掉）。

    ⚠️ **必须响亮**（⛔ 不夹到段首、⛔ 不静默从头读）：夹到段首会把「有人要读已经删掉的范围」
    变成一条容易被忽略的 warning；静默从头读则可能把还留着的行**重复消费**一遍。
    能走到这里只有两种情形：① 同一台机器上另一份绑定的 `callback_file_offset` 落后超过保留期
    （多份绑定共用一个事件文件）；② 有人手工删了段 / 手工改过偏移。两种都要人看见。
    """


def segment_retention_seconds() -> int:
    """归档段保留期（秒）：`SEGMENT_RETENTION_SECONDS`，可被 `CALLBACK_SEGMENT_RETENTION_SECONDS` 覆盖。

    每次调用都重新读环境变量（⛔ 不做模块级缓存：用例要在同一个进程里换它）。
    """
    raw = os.environ.get(ENV_SEGMENT_RETENTION)
    if raw is None:
        return SEGMENT_RETENTION_SECONDS
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        logger.warning(
            "回调归档段保留期的环境变量 %s=%r 不可用（要正整数秒）⇒ 回落到默认的 %s 秒",
            ENV_SEGMENT_RETENTION, raw, SEGMENT_RETENTION_SECONDS,
        )
        return SEGMENT_RETENTION_SECONDS
    return value


def ensure_events_dir(path: Path) -> Path:
    """确保事件文件所在目录存在（新建时就用 0700；**已存在的目录不动**）。"""
    directory = path.parent
    directory.mkdir(parents=True, mode=DIR_MODE, exist_ok=True)
    return directory


def segment_files(target: Path | str | None = None) -> list[tuple[int, Path]]:
    """已归档的段：`[(全局起始偏移, 路径), …]`，按偏移升序（文件不存在 ⇒ 空列表）。

    ⚠️ 偏移来自**文件名**（轮转时写进去的那个数字），⛔ 不靠「按 mtime 排序再累加」：
    后者在有人手工搬动 / 复制文件之后会给出错的偏移，而消费侧会拿它去 `seek`。
    """
    path = Path(target) if target is not None else config.events_path()
    try:
        entries = list(path.parent.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    found: list[tuple[int, Path]] = []
    prefix = path.name + "."
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix):]
        if _SEGMENT_RE.match(suffix) is None:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:  # pragma: no cover - 目录项刚被删的竞态
            continue
        found.append((int(suffix), entry))
    found.sort(key=lambda item: item[0])
    return found


def live_base(target: Path | str | None = None) -> int:
    """现役文件第一字节的**全局偏移**（= 所有归档段里最远的那个段尾；没有段 ⇒ 0）。"""
    base = 0
    for start, entry in segment_files(target):
        try:
            end = start + entry.stat().st_size
        except OSError:  # pragma: no cover - 段刚被删的竞态
            continue
        base = max(base, end)
    return base


@dataclass(frozen=True)
class FileSnapshot:
    """事件文件的两套尺寸（消费侧用**哪一套**是承重的，⛔ 别混）。

    - `high_water_bytes` = **累计写入字节**（逻辑流的总长度 = 现役文件段首 + 现役文件长）：
      **轮转与保留期清理都不会让它变小** ⇒ 消费侧判「文件被搬动过 / 变短了」**只许用它**
      （见 `jidanpu_openapi_consume.consume_callbacks`）；
    - `total_bytes` = **现存**归档段总长 + 现役文件长：⚠️ **保留期清理会把它变小**，
      它只回答「现在磁盘上还有多少」，⛔ 不许当水位 / 偏移的判据（那会让清理触发水位清零）；
    - `live_bytes` / `live_mtime` = 现役文件自己的（mtime 仍是「最近一次写入」的近似）；
    - `live_base` = 现役文件第一字节的全局偏移；
    - `earliest_base` = **现存最早**一段的起始偏移（没有归档段 ⇒ 0）。它之前的偏移**读不到了**
      （`read_records_from` 遇到它们响亮抛 `OffsetOutOfRetention`）；水位清零那一支从它读起。
    """

    total_bytes: int
    live_bytes: int
    live_mtime: float | None
    live_base: int
    #: 累计写入字节（= `live_base + live_bytes`）。⚠️ 判「文件变短了吗」只认它。
    high_water_bytes: int
    #: 现存最早一段的起始偏移（没有归档段 ⇒ 0）。
    earliest_base: int = 0


def snapshot(target: Path | str | None = None) -> FileSnapshot:
    """读一次尺寸快照（与轮转 / append / 清理用**同一把锁**的共享侧，见 `_read_lock`）。"""
    path = Path(target) if target is not None else config.events_path()
    lock_fd = _read_lock(path)
    try:
        segments = segment_files(path)
        archive_total = 0
        base = 0
        #: ⚠️ 现存最早段 = 列出来的第一个（`segment_files` 按偏移升序），⛔ 不另算 `min()`。
        earliest = segments[0][0] if segments else 0
        for start, entry in segments:
            size = _size_of(entry)
            archive_total += size
            base = max(base, start + size)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return FileSnapshot(
                total_bytes=archive_total, live_bytes=0, live_mtime=None,
                live_base=base, high_water_bytes=base, earliest_base=earliest,
            )
        return FileSnapshot(
            total_bytes=archive_total + stat.st_size,
            live_bytes=stat.st_size,
            live_mtime=stat.st_mtime,
            live_base=base,
            high_water_bytes=base + stat.st_size,
            earliest_base=earliest,
        )
    finally:
        _release_read_lock(lock_fd)


def _lock_path(target: Path) -> Path:
    return target.with_name(target.name + LOCK_SUFFIX)


def _open_lock(target: Path) -> int:
    """打开（必要时新建）写锁文件，权限 0600（同样 open 给一遍 + fchmod 给一遍）。"""
    fd = os.open(_lock_path(target), os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    except OSError:
        os.close(fd)
        raise
    return fd


def _read_lock(target: Path) -> int | None:
    """拿**共享锁**（`LOCK_SH`）读：轮转改名的窗口关在这里（见模块 docstring）。

    ⚠️ 为什么读侧也要锁：轮转 = **改名**，而读侧是「先列段、再按偏移打开」两步。
    没有锁时这两步之间可能发生轮转 ⇒ 列到的段清单与读到的活文件**不是同一个代**
    （活文件已经换成空的新文件，而那个代的行还在刚改名出来的段里）⇒ 读侧可能从
    一个错的位置开始读，把还没读的行**跳过去**（静默丢）。共享锁把「列段 + 读」与
    「改名」互斥；写侧已有的 `LOCK_EX` 与它是同一把锁文件。
    ⚠️ 锁只罩住**读的那一段**（增量读通常只有几行 / 几十 KB）；`iter_lines`（排查命令）
    不拿锁（它可能整份读 198MB，不该把回调写卡住；代价见那个函数）。
    ⚠️ 拿不到锁文件（目录只读 / 权限异常）⇒ 回 `None` 并**照旧读**：这里不是 fail-closed
    的判据（读不锁最多是极窄的竞态，拒读会让消费整条停摆）。
    """
    lock = _lock_path(target)
    try:
        fd = os.open(lock, os.O_RDONLY)
    except FileNotFoundError:
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, FILE_MODE)
            os.fchmod(fd, FILE_MODE)
        except OSError:  # pragma: no cover - 目录不可写
            return None
    except OSError:  # pragma: no cover - 权限异常
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    except OSError:  # pragma: no cover - 极少数文件系统不支持
        os.close(fd)
        return None
    return fd


def _release_read_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _rotate(target: Path) -> Path | None:
    """把现役文件整块改名成归档段，再建一个空的新现役文件。回归档段路径（没转 ⇒ `None`）。

    ⚠️ **只在调用方持有写锁时调**（`append_record` 的 `flock` 内）：所有 append 都排在
    同一把锁后面 ⇒ 改名那一刻不存在「别人的旧 fd 还在写它」的窗口。
    ⚠️ 段名里的偏移 = 现役文件第一字节的全局偏移（`live_base`）；这个数字是消费侧把
    逻辑流拼起来的**唯一**依据 ⇒ ⛔ 别改成时间戳之类的非偏移命名。
    ⚠️ 目标段名已存在（手工放了一份同名文件 / 时钟与状态不一致）⇒ **不覆盖**，响亮抛：
    覆盖归档段 = 直接丢掉消费侧还没读的行（假成功比失败更糟）。
    """
    base = live_base(target)
    archive = target.with_name(f"{target.name}.{base}")
    if archive.exists():
        raise FileExistsError(
            f"轮转目标已存在，拒绝覆盖：{archive.name}（先人工确认这份归档的来源）"
        )
    os.rename(target, archive)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    finally:
        os.close(fd)
    return archive


def append_record(record: Mapping[str, Any], path: Path | str | None = None, *, durable=False) -> Path:
    """把一条记录追加写进事件文件，返回写入的绝对路径。

    ⚠️ **失败要响亮**：⛔ 不许"写不进去也当成功"。调用方（`wsgi.py`）拿到异常就回 500 —— 
    本仓口径是**假成功比失败更糟**。

    ## 权限 0600 给两遍（⭐ 第二遍是后来补的，别删）

    这个文件里长期留着 `source_ip` 与订单号原文（本线新增的长期 PII 面），所以它必须
    **只有属主可读**。两遍各自挡一种形态：

    1. `os.open(..., FILE_MODE)`：**新建**时定模式。⚠️ 它的 mode 会被进程 `umask` 削
       （`umask 0o777` ⇒ 文件是 `0o000`；本仓 `_atomic_write` 的注释写着同一条）；
    2. `os.fchmod(fd, FILE_MODE)`：**不受 umask 影响**，而且对**已存在**的文件也生效
       —— `os.open` 的 mode **只在新建那一次**有效 ⇒ 更早的版本 / 别的工具建出来的
       `0644` 文件会**一直留着旧权限**（那正是这次要收的那一档）。

    ⚠️ **为什么 `fchmod` 失败时让它抛**（而不是吞掉）：吞掉就是「权限没收到，而调用方以为收到了」
    —— 本仓口径是假成功比失败更糟。⚠️ 这条**不会**把已上线的接收器弄坏：这个文件由**接收器进程
    自己**第一次写时建出来（`shared/callback/` 目录也是它建的，发版脚本不碰这两样）⇒ 属主就是它，
    `fchmod` 必然成功（同一台机器上 file owner 才有意义）。

    ## 轮转（H-1）

    写完这一条之后若现役文件达到 `ROTATE_BYTES`，**在同一个写锁里**把它改成归档段并新建空
    现役文件。刚写进去的那一条落在归档段里（全局偏移连续 ⇒ 消费侧照样读得到），⛔ 不丢行。
    """
    target = Path(path) if path is not None else config.events_path()
    ensure_events_dir(target)
    line = json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")) + "\n"
    payload = line.encode("utf-8")
    lock_fd = _open_lock(target)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
            try:
                #: ⚠️ **无条件设一次**（⛔ 不写成"只有更松时才设"：那等于在这里再写一份 umask 判据，
                #: 而它是进程级、随时会变的东西）。读一次 `fstat` 再决定对**权限本来就对**的常见情形
                #: 毫无收益，反倒多一次系统调用。
                os.fchmod(fd, FILE_MODE)
                view = memoryview(payload)
                while view:
                    # ⛔ 短写必须续写（`write` 允许只写一部分）；有锁在，续写不会与别人交错。
                    view = view[os.write(fd, view):]
                if durable:
                    os.fsync(fd)
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
            if size >= ROTATE_BYTES:
                archived = _rotate(target)
                if archived is not None:
                    logger.info(
                        "回调事件文件已轮转：%s（%s 字节）⇒ %s；新现役文件为空，"
                        "消费侧的全局字节偏移保持连续（不丢行、不重复消费）",
                        target.name, size, archived.name,
                    )
            if durable:
                directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
    return target


# ══ 保留期清理（保留 24 小时，处理完的旧分段删掉）════════════════════
def prune_segments(
    consumed_offset: int, *, path: Path | str | None = None, now: float | None = None,
) -> list[Path]:
    """按保留期删掉**已完全消费**的归档段，返回删掉的那些路径（没删 ⇒ 空列表）。

    三条闸**一起**上（少一条都会丢行 / 删早）：

    1. **已完全消费**：该段的**段尾** ≤ `consumed_offset`。段尾 = **下一段的起始字节**
       （段与段首尾相接 ⇒ 也就是这一段的全局结束位置）；⛔ 不用「段长累加」算，
       那在有人手工搬过文件之后会给出错的边界。
    2. **段龄 ≥ 保留期**（`segment_retention_seconds()`）：段龄 = 该段的 `mtime` 到现在。
       ⚠️ `mtime` = 这个段**最后一笔写入**的时刻（段只在轮转之前被写）⇒ 语义是
       「最后一笔写入之后再过 24 小时才允许删」。
    3. **最新那个归档段永远不删**（现役文件本来就不在候选里）。⚠️ 这一条**不只是保守**：
       `live_base` = 现存段的「段首 + 段长」的最大值 = 高水位的地基，把归档段删光会让
       高水位塌回现役文件大小 ⇒ 消费侧误判「文件被搬动」并清水位。留一段才守住那个坐标
       （另外消费侧也可能正读着它的尾巴）。

    ## 由谁触发（⛔ 别在接收器里调它）

    只有**消费侧**（`jidanpu_openapi_consume.consume_callbacks`）在每轮**推进完偏移之后**
    调一次：那里才知道「已消费到哪」。⛔ 不在接收器的写热路径上（那是 gunicorn 的每个请求）。

    ## 并发与失败

    - 拿**写锁** `LOCK_EX`（与轮转 / append 同一把锁文件）⇒ 与轮转 / 写 / 读（`LOCK_SH`）互斥：
      不会出现「刚判完已消费、正删的时候另一台 worker 在写它」的窗口；
    - ⚠️ **删失败只 `logger.warning`、⛔ 不抛**：清理是旁路，没删掉最坏的后果是「下一轮再试」，
      而把这一轮消费弄挂的后果是「水位不前进」——前者便宜得多；
    - 目录 / 段都不存在 ⇒ no-op（⛔ 不为清理去建目录、⛔ 不碰现役文件）。

    ⚠️ **多份绑定共用一份事件文件**：判据里的 `consumed_offset` 只是**触发这次清理的那一份绑定**
    的已消费偏移。同一台机器上若有另一份绑定落后超过保留期，它下一轮会撞上
    `OffsetOutOfRetention`（响亮，不静默）⇒ 那正是「谁在拖后腿」要人看见的地方。
    """
    target = Path(path) if path is not None else config.events_path()
    #: 目录都还没有（接收器一次都没写过）⇒ 直接 no-op：⛔ 不为了清理去 `mkdir`。
    if not target.parent.is_dir():
        return []
    #: 便宜的先判：没有归档段（只有一个现役文件）⇒ 连锁都不用拿。
    if len(segment_files(target)) < 2:
        return []
    retention = segment_retention_seconds()
    moment = time.time() if now is None else float(now)
    try:
        lock_fd = _open_lock(target)
    except OSError as exc:  # pragma: no cover - 目录不可写 / 权限异常
        logger.warning("回调归档段清理：拿不到写锁（%s）⇒ 本轮跳过，下一轮再试", exc)
        return []
    deleted: list[Path] = []
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            #: ⚠️ 拿锁**之后**重新列一遍：上面那次判断在拿锁之前，中间可能刚轮转出新段。
            segments = segment_files(target)
            #: ⚠️ `zip(segments, segments[1:])` = **最后一段天然不在里面**（第 3 条闸）。
            for (_start, entry), (segment_end, _next) in zip(segments, segments[1:]):
                if segment_end > consumed_offset:
                    continue  # 还没消费完（可能正读到它的尾巴）
                try:
                    age = moment - entry.stat().st_mtime
                except OSError:  # pragma: no cover - 段刚被删的竞态
                    continue
                if age < retention:
                    continue  # 还没到保留期
                try:
                    os.unlink(entry)
                except OSError as exc:
                    logger.warning(
                        "回调归档段删除失败（保留期清理）：%s（%s）⇒ 本轮跳过，下一轮再试",
                        entry.name, exc,
                    )
                    continue
                deleted.append(entry)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
    if deleted:
        logger.info(
            "回调归档段已按保留期删除 %s 个（已消费偏移 %s、保留期 %s 秒）：%s",
            len(deleted), consumed_offset, retention,
            ", ".join(entry.name for entry in deleted),
        )
    return deleted


def iter_lines(path: Path | str | None = None) -> Iterator[str]:
    """逐行读**全部**事件（归档段按偏移顺序在前 + 现役文件在后）：读原文、不解析。

    文件不存在时产出 0 行，⛔ 不抛。⚠️ 本函数**没有**增量语义（排查命令用），
    大数据量下会整份读一遍（那是 `callback_events` 既有的代价，spec 有登记）。
    """
    target = Path(path) if path is not None else config.events_path()
    for _start, entry in segment_files(target):
        yield from _iter_file_lines(entry)
    yield from _iter_file_lines(target)


def _iter_file_lines(target: Path) -> Iterator[str]:
    try:
        handle = target.open("r", encoding="utf-8")
    except FileNotFoundError:
        return
    with handle:
        for line in handle:
            yield line.rstrip("\n")


# ══ 按**字节偏移**增量读（kexing 现网 198MB / 十几万行 ⇒ ⛔ 不再每轮全扫）════
#: ⚠️ 自 H-1 起，这里的「字节偏移」是**全局偏移**：跨归档段连续，由
#: `segment_files` / `live_base` 把「归档段 + 现役文件」拼成一条逻辑流。
@dataclass(frozen=True)
class ScanChunk:
    """从某个字节偏移往后读到的一份（⭐ 只读这一段，⛔ 不把整份文件材料化）。

    - `records`：**解析成功的前缀**（遇到第一行读不出来就停）；
    - `new_offset`：最后一条**完整**行的末尾字节位置（绝对偏移；半行 / 坏行 ⇒ 停在那一行
      **之前**，下一轮从同一位置重读）；
    - `broken_line`：那一行在**本段**里的 1-based 序号（`None` = 这一段全都读得出来）；
    - `line_ends`：与 `records` 一一对应、每条记录那一行**末尾**（含换行）的绝对字节位置。
      ⚠️ 引擎「中途停下」时要靠它把偏移停回**第一条没处理的记录之前**（⛔ 不能推到段尾，
      否则那一条下一轮就读不到了 = 静默丢）；
    - `start_offset`：这一段**实际开始读**的位置（绝对偏移）。通常等于传进来的 `offset`；
      `skip_partial=True` 时是「丢掉那半行之后」的行首 —— 中途停下且第一条就没处理时，
      偏移要写回**这里**（⛔ 不能写回段中间，否则下一轮会从半行中间读起）。
    """

    records: tuple[dict, ...]
    new_offset: int
    broken_line: int | None = None
    line_ends: tuple[int, ...] = ()
    start_offset: int = 0


def _segments_with_live(target: Path) -> list[tuple[int, Path, int]]:
    """逻辑流的段清单 `[(全局起始偏移, 路径, 段长), …]`（归档段在前、现役文件最后）。

    ⚠️ 现役文件长度取**读的那一刻**：读到一半它又长了是正常的（下一轮再读尾巴）。

    ⚠️ **段间连续性自查（评审 M-5）**：逻辑流能成立的前提是「每一段的段首
    = 前一段的段尾」。运维**只许删最老的前缀段**（删了它不破这个前提，只是更早的偏移
    被夹到新段首、属于有意的保留期窗口）；一旦有人删了**中间段**或手工改名，这个前提
    就断了 —— 消费侧按全局偏移 `seek` 到空洞里会**静默跳过**一段，而单看「文件变短」
    这一条判据抓不到（删中间段时 `total_bytes` 可能一点没变）。⇒ 这里响亮留一条 warning，
    但**不抛**：读侧仍按现有段尽力读（拒绝读会让整条消费停摆，损失更大）。
    """
    segments = [(start, entry, _size_of(entry)) for start, entry in segment_files(target)]
    try:
        live_size = target.stat().st_size
    except FileNotFoundError:
        live_size = 0
    segments.append((live_base(target), target, live_size))
    for (prev_base, prev_entry, prev_size), (base, entry, _size) in zip(segments, segments[1:]):
        if base != prev_base + prev_size:
            logger.warning(
                "回调事件文件：段不连续（%s 的段尾 %s ≠ %s 的段首 %s）⇒ 有人删了中间段 / "
                "手工改过名；消费侧可能跳过一段（清旧段只许删最老的前缀段，见 RUNBOOK §6）",
                prev_entry.name, prev_base + prev_size, entry.name, base,
            )
    return segments


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:  # pragma: no cover - 段刚被删的竞态
        return 0


def read_records_from(
    path: Path | str | None = None, offset: int = 0, *, skip_partial: bool = False,
) -> ScanChunk:
    """从**全局**字节偏移 `offset` 起逐行读 **并解析**（⛔ 不从头扫：`open("rb")` + `seek`）。

    ⚠️ **为什么按字节而不是行号**：这个文件由**另一个进程**（接收器）追加写，行号要数就得
    从头数 ⇒ 那正是这次要修的「每轮全扫」。字节偏移由内核 `seek` 一步到位。

    ⚠️ **`offset` 必须落在行首**（它是上一个 `ScanChunk.new_offset`，见它的口径）。
    `skip_partial=True` 只给「**从文件中间**起读」那一支用（首轮大文件只读末尾 16MB）：
    先丢掉第一段不完整的行，下一行起才算数。

    ⚠️ **半行不算完整**：最后一行没有换行结尾 ⇒ 视为「接收器正在写这一行」⇒ 偏移停在它之前、
    下一轮重读（⛔ 不把它当一条记录，也⛔ 不把偏移推过它）。
    ⚠️ **坏行 / 解码不了的字节 / 不是 JSON 对象** ⇒ 同上：停住、回 `broken_line`（⛔ 不跳过它
    继续往后读：那会在「文件被整块换成一份旧副本」时**静默吞掉**中间一段）。
    ⚠️ 空行容掉（正常不会有；写坏了也不会产生空行 ⇒ 它不是「坏行」那一档），但它**是完整行**
    ⇒ 偏移照常越过它。

    文件 / 归档段都不存在 ⇒ 回一个**偏移归零**的空段（⛔ 不抛）：调用方按「文件没了」处置。

    ## ⭐ 轮转：跨段无缝续读

    逻辑流 = 归档段（按文件名里的起始偏移排序）＋现役文件。`offset` 落在哪一段就从那一段
    的段内对应位置读起，读到段尾**自动续到下一段**（段边界永远在行边界上：接收器只在
    完整写完一条之后、持锁轮转）⇒ `records` / `line_ends` / `new_offset` 全是**全局偏移**，
    消费侧只认「偏移在变长」，轮转对它是透明的（不丢行、不崩溃、不重复消费）。

    ⚠️ **起点早于现存最早的归档段 ⇒ 响亮抛 `OffsetOutOfRetention`**（⛔ 不夹到段首、
    ⛔ 不静默从头读）：那些行已被保留期清理删掉（`prune_segments`），能走到这里说明
    「有人要读已经删掉的范围」（另一份绑定落后超过保留期 / 有人手工动过文件或偏移）。
    ⚠️ **保留期窗口内仍可回溯**：从 `snapshot().earliest_base` 起读是合法入口
    （水位清零那两支就是这么读的），⛔ 别把「从现存最早段读」也拦掉。
    """
    target = Path(path) if path is not None else config.events_path()
    start = max(0, int(offset))
    lock_fd = _read_lock(target)
    try:
        return _read_records_locked(target, start=start, skip_partial=skip_partial)
    finally:
        _release_read_lock(lock_fd)


def _read_records_locked(
    target: Path, *, start: int, skip_partial: bool,
) -> ScanChunk:
    """`read_records_from` 的**持共享锁**主体（拆出来只为让锁的 finally 一眼可见）。"""
    segments = _segments_with_live(target)
    total = segments[-1][0] + segments[-1][2]
    if all(size == 0 for _base, _entry, size in segments) and not target.exists():
        return ScanChunk((), 0)

    # 起点已被保留期清理删掉 ⇒ **响亮报错**（见 docstring 末段；⛔ 不夹、⛔ 不从头读）。
    first_base = segments[0][0]
    if start < first_base:
        raise OffsetOutOfRetention(
            f"回调事件文件的消费偏移 {start} 早于现存最早归档段（段首 {first_base}）："
            f"它落在已被保留期清理删掉的范围内（{first_base - start} 字节读不到）。"
            "**不静默从头读**，请人工确认：① 这个绑定的「已消费偏移 / 水位」是否该清零；"
            "② 同一台机器上是否有别的绑定落后超过保留期（它们共用一个事件文件）"
        )
    # 偏移越过逻辑流末尾 ⇒ 回空段（`new_offset` 原样带回，与旧口径一致）。
    if start > total:
        return ScanChunk((), start)

    records: list[dict] = []
    line_ends: list[int] = []
    broken: int | None = None
    index = 0
    new_offset = start
    read_from: int | None = None
    first_segment = True
    for base, entry, size in segments:
        segment_end = base + size
        if segment_end <= start:
            continue  # 整段在起点之前（已消费完）
        local_start = max(0, start - base) if first_segment else 0
        try:
            handle = entry.open("rb")
        except FileNotFoundError:  # pragma: no cover - 段刚被删的竞态
            continue
        with handle:
            handle.seek(local_start)
            if first_segment and skip_partial:
                handle.readline()
            actual = base + handle.tell()
            if read_from is None:
                read_from = actual
            new_offset = actual
            while True:
                line_start = base + handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                index += 1
                if not raw.endswith(b"\n"):
                    # 半行（接收器写这一行的同时我们在读）⇒ 偏移停在这一行之前，下一轮重读。
                    broken = index
                    new_offset = line_start
                    break
                line_end = base + handle.tell()
                if raw.strip():
                    try:
                        text = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        broken = index
                        new_offset = line_start
                        break
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        broken = index
                        new_offset = line_start
                        break
                    if not isinstance(parsed, dict):
                        broken = index
                        new_offset = line_start
                        break
                    records.append(parsed)
                    line_ends.append(line_end)
                new_offset = line_end
            first_segment = False
            if broken is not None:
                break
    return ScanChunk(
        tuple(records), new_offset, broken, tuple(line_ends), read_from if read_from is not None else start
    )
