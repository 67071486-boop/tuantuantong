"""鸡蛋铺出站用的**正向代理**配置（业务方选 B：只让鸡蛋铺走代理）。 ## 为什么单独一个中性模块本模块只处理出口配置和计数⇒ 登录式（`jidanpu.py`）与官方 API （`jidanpu_openapi.py`）两条线都能直接 import，不会绕回 `jidanpu_common`（它 import 了 `jidanpu`，模块级反向 import 会成环）。 ## 只给鸡蛋铺用，⛔ 不做全局 `HTTPS_PROXY` 这台机还要连 COS / 支付 / 微信 / 客服栈，整机绕代理会把无关流量也带走（业务方明确选 B）。因此只有**建鸡蛋铺 httpx 客户端**的那几处显式传 `proxy=proxy_url()`。 ## 出口策略：**粘性** + 连续拦截才切换 + 隔离期（出口池线 T2）此前是「每建一次连换一个出口」（`itertools.count` 取模轮询）——那是**反的**：同一个账号的会话
每换一次出口就换一个来源 IP，对端 WAF 更容易把它当异常。现在的口径： - **粘性**：`current_proxy()` 恒回**当前**那个出口（默认下标 0 = 列表第一个），直到发生了切换。 - **连续拦截才切**：`note_blocked()` 把当前出口的「连续拦截计数」+1；攒到 `JIDANPU_EGRESS_FAIL_THRESHOLD`（默认 **3**）⇒ 把这个出口**隔离** `JIDANPU_EGRESS_QUARANTINE_SECONDS`（默认 **900** 秒），下标推进到**下一个未隔离**的出口。单次抖动（一次 502 / 一次超时）不会换出口，只有**连续**被拒才换。 - **成功一次即复位**：`note_ok()` 把当前出口的连续计数清零（⛔ 不是累计失败）。 - **全隔离 ⇒ `None`（= 直连）**：全部出口都在隔离期时**不抛异常**，回落到本机出口 ——「同步停摆」比「用一个可能已被封的 IP」更糟，而且直连本来就是池里的合法一员。
- **可观测**：`current_label()` 给日志用的 `<下标>:<host>`。⛔ **绝不含账号 / 密码 / 查询串** （日志会进 journald 与运维终端）。 ⚠️ **两档状态**： - 设了 `JIDANPU_EGRESS_STATE_FILE` ⇒ 同一台机的多个常驻单元（上家 / 快路 / 回拉 / outbox …） **共用一份**「当前出口 + 连续计数 + 隔离表」（文件锁串行、原子写、**0600**；键是 `_state_key()` = host + sha256 前缀，⛔ 不含 userinfo）； - 不设 ⇒ 退回**进程内**状态（模块级全局，**一个进程一份**：各单元各自选出口、各自计数、各自隔离，谁先攒满阈值谁切，其余单元仍可能挂在被封的 IP 上）。 ⛔ 不做跨机协调（P1 明确不做，租户 <5 不需要）。⚠️ 隔离状态按**出口**记（键 = 内存里的出口 URL / 落盘时的 `_state_key()`，⛔ 不是按下标）⇒ 池子内容变了也不会把「隔离」错配到另一个出口上；改池子（只在测试里会）仍建议配 `reset_counter_for_tests()`。 ## 取值 `JIDANPU_PROXIES`（逗号分隔，可混 `direct`）优先；否则回落单个 `JIDANPU_PROXY`；形如 `http://user:pass@1.2.3.4:8080` 或 `socks5://…`（httpx 支持的 scheme）。空串 / 全是空白 ⇒ `None`（= 直连）：`.env` 里留一行空值就是「没配代理」。"""
from __future__ import annotations

from contextlib import contextmanager

import fcntl
import hashlib
import json
import os
import threading
import time
from urllib.parse import urlsplit

from apps.business.platform_egress import uses_platform_egress

#: 列表里代表「直连（本机出口）」的词（大小写不敏感）。
DIRECT_TOKENS = frozenset({"direct", "local", "直连", ""})

#: 连续拦截多少次才换出口。 **依据（政策值，业务方）**：单次 468 可能只是抖动 / 一次巧合的限流，
#: 连续 **3** 次才当「这个出口真的坏了」——门槛再低会把健康出口踢掉（一次误判就少一个 IP），
#: 再高则要等太久才切换（阈值 × 轮询间隔 = 发现故障的时延）。
DEFAULT_FAIL_THRESHOLD = 3

#: 切走的出口隔离多久（秒）。 **依据（实测 recon 读数）**：对端 WAF 按**来源 IP** 封禁 **5–15 分钟**
#: （惹得越狠越久、且延迟生效）⇒ 取**上限 900 秒**：隔离短了会拿着同一个被封 IP 反复试，
#: 反而把封禁拖长；隔离长了只是晚一点回用一个本来已经能用的出口（池里通常还有别的可用）。
DEFAULT_QUARANTINE_SECONDS = 900.0

#: 出口标签里代表「直连」的 host 词（与 `DIRECT_TOKENS` 同一口径）。
_DIRECT_HOST = "direct"

#: 全部出口都在隔离期时，标签的「下标」位（那一刻**没有**可用下标）。
_NO_OUTLET_INDEX = "-"

#: `note_blocked()` 的**三种结局**（调用方按它决定「丢不丢会话 / 日志写哪一句」）。
#: ⚠️ **⛔ 别并成一个 bool**：`exhausted` 的那一刻**没有**「下一个出口」，
#: 写「已切换到下一个出口」是**假话**（那一句会把人引去查一个不存在的出口）。
BLOCK_COUNTED = "counted"
BLOCK_SWITCHED = "switched"
BLOCK_EXHAUSTED = "exhausted"

_lock = threading.Lock()

#: 当前出口在 `proxies()` 列表里的下标。
_index = 0

#: 每个出口的**连续**拦截计数（成功一次 / 切换之后清零）。键 = 出口 URL（`None` = 直连）。
_blocked_counts: dict[str | None, int] = {}

#: 每个出口的隔离截止时刻（**墙钟** `time.time()`：要跨进程 / 跨重启活着）。键同上。
_quarantined_until: dict[str | None, float] = {}

#: ⭐ 下家出向的**冷却截止时刻**（键 = `<schema>:<binding.pk>`，由 `jidanpu_member` 写）。
#: 与出口池共用同一份状态文件（文件锁 / 原子写 / 0600 同一套），⛔ 不含任何凭据。
_cooldowns: dict[str, float] = {}

#: 现在是不是「全部出口都在隔离期 ⇒ 已回落直连」那一档。
#: ⚠️ 只为让 `note_blocked()` 把「**第一次**切到直连」与「直连上又挨了一发」分开（⑤）：
#: 前者要丢会话 + 告警，后者只计数（⛔ 不然每拍一条「已回落直连」的告警）。
_exhausted = False


def _parse_list(raw: str) -> list[str | None]:
    out: list[str | None] = []
    for item in (raw or "").split(","):
        token = item.strip()
        out.append(None if token.lower() in DIRECT_TOKENS else token)
    return out


def proxies() -> list[str | None]:
    """本进程可用的出口列表（`None` = 直连），**顺序 = 优先级**。
    - `JIDANPU_PROXIES`（逗号分隔，可混 `direct`）优先；
    - 否则回落单个 `JIDANPU_PROXY`； - 都没有 ⇒ `[None]`（直连，与接线前行为一致）。"""
    raw = os.environ.get("JIDANPU_PROXIES")
    if raw is not None and raw.strip():
        parsed = _parse_list(raw)
        if parsed:
            return parsed
    single = (os.environ.get("JIDANPU_PROXY") or "").strip()
    return [single or None]


def _env_number(name: str, default: float) -> float:
    """读一个**正数**环境变量：空 / 写坏 / ≤0 一律回落 `default`（⛔ 不许把坏配置当 0）。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _fail_threshold() -> int:
    """连续拦截阈值（默认 3；每次现读，运维改完 `.env` 重启即生效）。"""
    return int(_env_number("JIDANPU_EGRESS_FAIL_THRESHOLD", DEFAULT_FAIL_THRESHOLD))


def _quarantine_seconds() -> float:
    """出口隔离时长（默认 900 秒）。"""
    return _env_number("JIDANPU_EGRESS_QUARANTINE_SECONDS", DEFAULT_QUARANTINE_SECONDS)


#: 共享状态文件（可选）：设了它 ⇒ **同一台机的多个常驻单元共用一份出口决策**。
#: 不设 ⇒ 退回进程内状态（与接线前一致，用例默认走这条）。
_STATE_FILE_ENV = "JIDANPU_EGRESS_STATE_FILE"

#: 状态文件里代表「直连」的键（JSON 的键不能是 `null` ⇒ 用一个不可能的 URL 占位）。
_STATE_DIRECT_KEY = "direct"


def _state_key(outlet: str | None) -> str:
    """落盘用的键：**⛔ 不含 userinfo**（出口 URL 里可能带账号 / 密码）。形状 `<host>#<sha256 前 12 位>`： - 只留 host 给运维看（不含账号 / 密码 / 端口 / 查询串）；
    - 哈希覆盖**完整 URL**（含端口与凭据）⇒ 同一 host 上的两个出口不会撞键； - 直连用固定词 `direct`（与 `DIRECT_TOKENS` 同一口径，且不可能与任何真 URL 的
    `host#digest` 形状相同）。 ⚠️ **承重**：旧键就是完整出口 URL ⇒ `shared/egress-state.json` 里明文躺着 `http://user:pass@host`（同目录 `.env` 是 0600，这个文件却是 0644）。"""
    if outlet is None:
        return _STATE_DIRECT_KEY
    digest = hashlib.sha256(outlet.encode("utf-8")).hexdigest()[:12]
    return f"{_host_of(outlet)}#{digest}"


def _outlets_by_state_key() -> dict[str, str | None]:
    """当前池子的「落盘键 → 出口」反查表（`_restore` 只认这些键，旧明文键直接丢弃）。"""
    return {_state_key(outlet): outlet for outlet in proxies()}


def _state_file() -> str | None:
    """共享状态文件路径（空 = 不共享）。每次现读，运维改完 `.env` 重启即生效。"""
    raw = (os.environ.get(_STATE_FILE_ENV) or "").strip()
    return raw or None


def _snapshot() -> dict:
    """把内存状态拍成可落盘的 JSON（键：`_state_key`；直连用 `_STATE_DIRECT_KEY`）。"""
    return {
        "index": _index,
        "exhausted": _exhausted,
        "blocked": {
            _state_key(key): value for key, value in _blocked_counts.items()
        },
        # ⚠️ 隔离截止用**墙钟**（`time.time()`）：要跨进程 / 跨重启活下来，
        #    `monotonic()` 的读数在另一个进程里没有意义。
        "quarantine": {
            _state_key(key): value for key, value in _quarantined_until.items()
        },
        # ⭐ 下家出向的冷却（键是 `<schema>:<binding.pk>`，不是出口；值 = 墙钟截止时刻）。
        "cooldowns": dict(_cooldowns),
    }


def _restore(data: dict) -> None:
    """用落盘状态覆盖内存（⛔ 不认识的形状就当空状态，别让坏文件把同步打挂）。 ⚠️ 键是 `_state_key`（⛔ 不是出口 URL）：只认**当前池子里还算得出来**的键
    （旧版本写下的明文 URL 键、或池子里已经删掉的出口 ⇒ 直接丢弃；下一次落盘会把
    整份文件重写成新形状，旧明文也一并消失）。"""
    global _index, _exhausted
    if not isinstance(data, dict):
        data = {}
    index = data.get("index")
    _index = index if isinstance(index, int) and index >= 0 else 0
    _exhausted = bool(data.get("exhausted"))
    known = _outlets_by_state_key()
    _blocked_counts.clear()
    for key, value in (data.get("blocked") or {}).items():
        if isinstance(value, int) and key in known:
            _blocked_counts[known[key]] = value
    _quarantined_until.clear()
    for key, value in (data.get("quarantine") or {}).items():
        if isinstance(value, (int, float)) and key in known:
            _quarantined_until[known[key]] = float(value)
    _cooldowns.clear()
    for key, value in (data.get("cooldowns") or {}).items():
        if isinstance(key, str) and isinstance(value, (int, float)):
            _cooldowns[key] = float(value)


@contextmanager
def _state():
    """读-改-写出口状态：文件模式**加排他锁**（同一台机的多个单元串行），内存模式只加线程锁。
    ⚠️ 文件写用 `tmp` + `os.replace`（原子），⛔ 不原地截断——半个 JSON 会让所有单元一起读坏。
    ⚠️ 读失败 / 目录不可写 ⇒ **当内存模式跑**（宁可各进程各自决策，也不让同步停摆）。"""
    path = _state_file()
    if path is None:
        with _lock:
            yield
        return
    lock_path = path + ".lock"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handle = open(lock_path, "a+")
    except OSError:
        with _lock:
            yield
        return
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                with open(path, encoding="utf-8") as fh:
                    _restore(json.load(fh))
            except (OSError, ValueError):
                _restore({})
            try:
                yield
            finally:
                payload = json.dumps(_snapshot(), ensure_ascii=False)
                tmp = f"{path}.tmp.{os.getpid()}"
                try:
                    with open(tmp, "w", encoding="utf-8") as fh:
                        fh.write(payload)
                    # ⚠️ 承重：状态里有出口的隔离决策，
                    #    权限按同目录 `.env` 的口径（0600）——默认 umask 会给出 0644。
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, path)
                except OSError:
                    pass
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _host_of(proxy: str | None) -> str:
    """出口 URL 的 host（⛔ 丢掉账号 / 密码 / 端口 / 查询串 —— 只留日志能看的那一格）。"""
    if proxy is None:
        return _DIRECT_HOST
    return urlsplit(proxy).hostname or "?"


def _is_quarantined(outlet: str | None, now: float) -> bool:
    """这个**出口**此刻在隔离期里吗（键是出口 URL，⛔ 不是下标）。"""
    until = _quarantined_until.get(outlet)
    return until is not None and now < until


def _next_usable(pool: list[str | None], *, after: int, now: float) -> int | None:
    """`after` **之后**（环形）第一个未隔离的下标；全都在隔离期 ⇒ `None`。"""
    for step in range(1, len(pool) + 1):
        index = (after + step) % len(pool)
        if not _is_quarantined(pool[index], now):
            return index
    return None


def _usable_index(pool: list[str | None], now: float) -> int | None:
    """当前下标**此刻**可用就回它；它被隔离了就回下一个未隔离的；全隔离回 `None`。"""
    if not pool:
        return None
    index = _index if 0 <= _index < len(pool) else 0
    if not _is_quarantined(pool[index], now):
        return index
    return _next_usable(pool, after=index, now=now)


def current_proxy() -> str | None:
    """**当前**出口（粘性：同一出口一直用到发生切换为止）；全隔离 ⇒ `None`（= 直连）。"""
    pool = proxies()
    global _index, _exhausted
    with _state():
        index = _usable_index(pool, time.time())
        if index is None:
            return None
        _index = index
        # 又有可用出口了（隔离期到点）⇒ 退出「已回落直连」那一档。
        _exhausted = False
        return pool[index]


def note_blocked(outlet: str | None = None, *, force: bool = False) -> str:
    """当前出口刚被对端拦截一次（WAF/风控）。回**三种结局**之一（见下面三个常量）。 | 返回值 | 含义 | 调用方该做什么 | |---|---|---| | `BLOCK_COUNTED` | 只计数：没到阈值，**或者已回落直连期间又挨了一发**（那一刻没有池内出口可换） | 什么都不用做 | | `BLOCK_SWITCHED` | 到阈值且**真的换到了池内另一个出口** | 标记整池作废（下一次建连才换，⛔ 不当场关）+ 一条告警 |
    | `BLOCK_EXHAUSTED` | 到阈值，但**全部出口都在隔离期** ⇒ 回落直连 | 同上，但告警要写「已回落直连」 | ⚠️ 上表 `BLOCK_SWITCHED` 那格按实现写：出口变了只**标记整池作废**、⛔ 不当场关客户端（当场关会打断同一轮后面的段，撞 `client has been closed` 的假故障）。 ⚠️ 连续计数 +1；到 `JIDANPU_EGRESS_FAIL_THRESHOLD` ⇒ 隔离**被拦的那个**出口并推进到下一个
    未隔离的。 ⭐ **`outlet`**（2026-10-25 评审 ③）：**发出那个请求的客户端建连时用的出口**。给了就按它记账（⛔ 不是「记账那一刻的当前出口」）——同一轮里 A 出口被切走之后，仍在用 A 客户端的那几段迟到的 468 **不许**把新出口 B 的计数推高（那会把健康出口误隔离）；它若已经不在当前位，这一次只记在它自己头上、⛔ 不报切换（没有新的出口可换）。 `None` ⇒ 按当前出口记（老口径；下家那几段自己建连、拿不到客户端时就是这样）。 ⚠️ `EXHAUSTED` 也**不抛异常**：那一刻没有别的出口可试，硬抛只会把同步打挂（`current_proxy` 会回 `None` = 直连，同步照跑）。"""
    if uses_platform_egress():
        return BLOCK_COUNTED
    pool = proxies()
    global _index, _exhausted
    with _state():
        now = time.time()
        index = _usable_index(pool, now)
        target_index = index
        if outlet is not None:
            # ⭐ 按**发这个请求的那个出口**记账（③）。
            target_index = next(
                (i for i, item in enumerate(pool) if item == outlet), None
            )
            if target_index is None:
                # 这个出口已经不在池里（运维改过配置）⇒ 无处可切，只当一次没有下文的计数。
                return BLOCK_COUNTED
        if target_index is None:
            # 全在隔离期（已回落直连）。⭐ **只有第一次**切到直连回 `EXHAUSTED` （调用方那时要标记整池作废 + 一条「已回落直连」的告警）；此后在直连上再挨一发
            # ⇒ 只计数。 ⚠️ **如实登记**（评审 ⑤）：直连期这一格是往 `None` 键上**一直累加**的，而 `note_ok()` 只清「当前池内出口」那一格 ⇒ 这个数只增不减。
            # ⚠️ 无害（直连是回落档，不参与阈值 / 下标判定），但⛔ 别把它读成「直连的连续拦截次数」；它的用途只是让「第一次回落」与「直连上又挨一发」分开。
            if _exhausted:
                _blocked_counts[None] = _blocked_counts.get(None, 0) + 1
                return BLOCK_COUNTED
            _exhausted = True
            return BLOCK_EXHAUSTED
        target = pool[target_index]
        count = _blocked_counts.get(target, 0) + 1
        # ⭐ `force=True`（下家出向的 468）：**一次就切**，⛔ 不等「连续 3 次」
        #    （业务方口径：收到 468 先换下一个出口重试；连续 3 次是读路的阈值）。
        if count < _fail_threshold() and not force:
            _blocked_counts[target] = count
            return BLOCK_COUNTED
        # 到阈值 ⇒ 隔离它。
        _blocked_counts[target] = 0
        _quarantined_until[target] = now + _quarantine_seconds()
        if target_index != index:
            # ⭐ 报拦截的那个出口**已经不在当前位**（同一轮里 A 被切走后、A 客户端迟到的 468）
            #    ⇒ 只隔离它：⛔ 不动当前下标、⛔ 不报切换（没有新出口要换，也不该刷告警）。
            return BLOCK_COUNTED
        _exhausted = False
        nxt = _next_usable(pool, after=target_index, now=now)
        if nxt is None:
            _index = target_index
            _exhausted = True
            return BLOCK_EXHAUSTED
        _index = nxt
        return BLOCK_SWITCHED


def record_cooldown(key: str, until: float) -> None:
    """记一条**出向冷却**（`jidanpu_member` 的 468 冷却用它；键 = `<schema>:<binding.pk>`）。
    ⚠️ 与出口池共用同一份状态文件（同一把锁 / 原子写 / 0600）⇒ 设了 `JIDANPU_EGRESS_STATE_FILE` 时，冷却对同一台机的其它单元也可见；
    不设 ⇒ 只活在本进程（与出口池同一条代价）。"""
    with _state():
        _cooldowns[str(key)] = float(until)


def cooldown_until(key: str) -> float | None:
    """读一条出向冷却的截止时刻（墙钟秒；`None` = 没记过）。"""
    with _state():
        return _cooldowns.get(str(key))


def clear_cooldown(key: str) -> None:
    """清掉一条出向冷却（成功一次后由 `jidanpu_member` 调）。"""
    with _state():
        _cooldowns.pop(str(key), None)


def note_ok() -> None:
    """当前**池内**出口刚成功过一次 ⇒ 它的连续拦截计数清零（一次成功即复位，⛔ 不是累计失败）。 ⚠️ **它清的是「当前出口」那一格，不认「这次成功的是哪个出口」**（2026-10-27 评审 Minor）。
    同一轮里旧出口客户端迟到的成功会清掉**新**出口的计数（而不是旧出口自己的）—— 影响有限：计数只用于「连续 N 次拦截才隔离」，多清一次只会**延后**隔离、不会误隔离；
    真要精确归属得让每个调用点带上建连出口，本轮不动（见 `docs/STATUS.md` 的残差）。 ⚠️ 已回落直连时没有「当前池内出口」⇒ 这里**什么都不清**（`None` 键的累计见 `note_blocked()` 里那段如实登记）。"""
    if uses_platform_egress():
        return
    pool = proxies()
    with _state():
        index = _usable_index(pool, time.time())
        if index is not None:
            _blocked_counts.pop(pool[index], None)


def current_label() -> str:
    """给日志用的出口标签 `<下标>:<host>`（⛔ 不含账号 / 密码 / 查询串）。
    全部出口都在隔离期（回落直连）时下标位是 `-`：那一刻**没有**「当前池内出口」。"""
    pool = proxies()
    with _state():
        index = _usable_index(pool, time.time())
        if index is None:
            return f"{_NO_OUTLET_INDEX}:{_DIRECT_HOST}"
        return f"{index}:{_host_of(pool[index])}"


def proxy_url() -> str | None:
    """= `current_proxy()`（**保留旧名**给既有调用点：`jidanpu` / `jidanpu_openapi` 的建连）。"""
    return current_proxy()


def reset_counter_for_tests() -> None:
    """只给用例用：把下标与**全部**出口状态（连续计数 / 隔离截止）清回初始值。"""
    global _index, _exhausted
    with _state():
        _index = 0
        _exhausted = False
        _blocked_counts.clear()
        _quarantined_until.clear()
        _cooldowns.clear()
