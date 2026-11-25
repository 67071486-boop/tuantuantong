"""上下游订单互通的**纯函数**（spec §9-S3/S7/S9、§10.1、A11）。
本模块只有纯函数与常量：不碰数据库、不发请求——本模块是这套闸门与白名单的实现落点。
⚠️ 出站 HTTP 在 `apps/business/peer_client.py`，⛔ 不要合并进本模块（那样就没法单独测闸门了）。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlsplit

from django.core.serializers.json import DjangoJSONEncoder

#: 令牌长度。`token_urlsafe(32)` ≈ 43 字符；列宽 128 留足余量。
TOKEN_BYTES = 32

#: `OrderSource.name` 的列宽（`apps/business/models.py` 的 `OrderSource`）。
#: ⚠️ 这个 10 与绑定名的 20（对标 `homeCompany` 的 `maxlength=20`）**不是同一个约束体系**
#: ——照对标抄字段规格时必须回查本仓既有列宽（spec A11 / §14-R10）。
ORDER_SOURCE_NAME_MAX = 10

JIDANPU_DIRECT_PENDING_PLATFORM = "jidanpu"
PEER_DIRECT_PENDING_MESSAGE = "下家指派正在确认，请从指派弹窗用原工作室和账号名称重试"
DIRECT_TAKER_ASSIGN_KIND = "order.direct_assign"
CORE_CHAT_TARGET = "chat_core"

#: 撤单原因（事件级 `meta.withdraw_reason` 的取值）→ 下家订单日志的**逐字中文** （spec §7.3 第 1/2 行 + §16.2 的 `deleted` 行）。 ⚠️ 协议里传的是**左边那个枚举**，中文只在本仓内部用（⛔ 不把中文当协议传）。
#: 上家入队（`peer_dispatch.enqueue_order_event`）与下家落库写日志都要读它 ⇒ 放**纯函数模块**，放 `peer_dispatch` 会引出 `dispatch ↔ inbound` 的循环风险（预检 Ruling 2）。
#: ⚠️ 改这里的字面值 = 改对客可见的订单日志文案；逐字值由 `tests/test_peer_order_push.py::EnqueueOrderEventTests::test_withdraw_reason_text_is_the_spec_wording` 钉住。
WITHDRAW_REASON_TEXT: dict[str, str] = {
    "refunded": "发单方在发单系统里已将该笔订单改成已退单",
    "taker_removed": "发单方将订单接手人移除",
    "deleted": "发单方在发单系统里已将该笔订单删除",
}

#: 外发字段白名单（spec §10.1；游戏那一项按**裁决 R21** 改过）。 ⛔ **白名单，不是排除法**：`Order` 有 60+ 列，排除法迟早漏（`AGENTS.md` §5-E / E-0010）。 ⛔ 绝不外传租户内数据（用户 / 绩效 / 审计 / 内部备注）。 ⚠️ **本清单已按 `Order` 的**真实列**校准**（开工扫描实测）： spec §10.1 散文里举的 `customer_name` 与 `deadline` **在本仓不存在**；
#: 客户电话列叫 `customer_phone`、客户原始需求列叫 `content`、QQ/微信列叫 `customer_qq`。将来加字段必须回查 `src/apps/business/models.py` 的 `Order`，⛔ 别照 spec 的散文抄。 ⭐ **裁决 R21：游戏按名字传，⛔ 不传 `game_id`**。每个租户的 `Game.id` 都**从 1 开始**
#: ⇒ 对端的 id 在本租户**永远能解析到**、却几乎总是解析到**另一款游戏**：同步过来的单会静默挂到错的游戏下（订单读路径按游戏作用域 ⇒ 下家客服可能根本看不到），而接口回 HTTP 200 + `applied: 1`——**丢/错单被报成成功**。名字是两端唯一有意义的对齐键。 ⚠️ `server_id` / `project_id` 仍按 **id** 传：本期接收侧**不消费**它们（对齐是 P2），如实登记；`project_text` 是**自由文本**（D9「以 project_text 为准」），接收侧原样落。
PEER_ORDER_FIELDS: tuple[str, ...] = (
    "order_no",
    "status",
    # ⛔ **业务方裁定：客户价（= 上家的接单价）绝不下发**—— 下家一律按**发单价**建单（`hall_price`），客户价只留在上家。
    # 删掉这一格是**契约变更**：接收侧不再收到 `total_price`，它把 `hall_price` 写进自己的 `total_price`（见 `peer_inbound._DOWNSTREAM_BOUNDED_FIELDS`）。
    # P3 起随状态同步一起外发（业务方裁定）：下家按发单价建单。
    "hall_price",
    "remark",
    "game_name",
    "server_id",
    "project_id",
    "project_text",
    "customer_phone",
    "customer_qq",
    "content",
    "source_order_no",
)

#: 一轮对账**最多问多少个单号**（P3 Task 8，spec §16.8 / 业务方「查到丢单就直接补」）。
#: ⚠️ 两侧共用同一个数：上家侧切片后**只问这些**（`peer_worker.run_reconcile`），
#: 下家侧收到更长的清单也**只答这些**（`peer_views.PeerReconcileView`）——对端不该能用一份超长清单逼我们跑无界查询。超过的部分**有意不理**（不是错误）。
RECONCILE_MAX_ORDERS = 200

# ── 入站事件批量上限（⭐ C4 / L-2，审计批）────────────────────────
#: 发送端**一次 HTTP 实际投递的最大事件条数**（证据行：`peer_worker.deliver_one` 调 `peer_client.push_events` 时只包**一条**事件——「一次只投一条，P1 现状」）。
#: ⛔ 改发送端的批量形状时必须回头核这个数（它承重：接收端的上限按它放大）。
PEER_EVENTS_MAX_PER_PUSH = 1
#: 接收端 `events` 列表的**硬上限**：`max(2 × 发送端单批, 100)`。
#: 给 2× 余量是允许发送端将来把单批扩一倍而不必先改接收端；`100` 是地板（防止发送端
#: 退化成 0 时上限跟着归零）。超限 ⇒ 接收端回业务信封（`1000` + 人话），发送侧把整批记成业务失败——它该去分批，而不是把对端拖进巨型事务。
PEER_EVENTS_MAX_BATCH = max(2 * PEER_EVENTS_MAX_PER_PUSH, 100)

# ── 团团通之间的大厅镜像事件（D-0303，spec `docs/specs/2026-10-25-peer-hall-mirror.md`）──
#: ⚠️ 与 `WITHDRAW_REASON_TEXT` 同一条放纯函数模块的理由：上家侧入队（`peer_dispatch`）与下家侧接收（`peer_inbound`）都要读它，放任何一侧都会引出 `dispatch ↔ inbound` 的循环风险。
#: ⛔ 字面值是**双方约定的机器契约**（随事件外发），改它 = 改协议，两侧必须同一次改。上家（我方有生效下游绑定）向**全部**启用大厅推送的下游绑定广播前三类；后三类是 `hall.claim` 的应答与请求本身（下家 → 上家一条、上家 → 下家一条）。
HALL_THROW_KIND = "hall.throw"
HALL_TAKE_KIND = "hall.take"
#: `hall.take` 顶层 wire 键 `leave_reason` 的取值：指派族（`order_writes.assign_order`
#: 唯一覆盖点）发带因 take，抢单受理 / 自抢 / 撤回 / 清人 / 软删发裸 take；
#: 接收侧只认带这个值的 take 才尝试收单（缺失 / 不认识 ⇒ 零动作，兼容老端）。
HALL_TAKE_LEAVE_ASSIGNED = "assigned"
HALL_REPRICE_KIND = "hall.reprice"
HALL_CLAIM_KIND = "hall.claim"
HALL_CLAIM_OK_KIND = "hall.claim_ok"
HALL_CLAIM_REFUSED_KIND = "hall.claim_refused"
#: ⭐ Z20 的**第二类证据**（计划 `2026-10-25-peer-hall-pull` §2）：上家大厅拉取端点为真正返回给这条下家绑定的每一张卡写一行 `direction="in" / kind="hall.pull"`。
#: 抢单判据 = 「推送过（`hall.throw` done + 空 reason）**或** 拉取过（本 kind）」。 ⚠️ 它**不是**协议事件：⛔ 不进 `HALL_INBOUND_KINDS`、⛔ 不进 `peer_worker.claim_due_events`
#: 的取活面（`direction="in"`），也⛔ 不从本仓发出去。 ⚠️ 将来加 `PeerSyncEvent` 清理命令时，**必须排除** `hall.throw`（done）与本 kind 两类证据行（计划 §2 的保留期口径；今天全仓没有清理命令）。
HALL_PULL_KIND = "hall.pull"

#: 大厅拉取端点：`limit` 上限 / 默认值（计划 §1.3）。
HALL_PULL_MAX_LIMIT = 200
HALL_PULL_DEFAULT_LIMIT = 100
#: 游标是**不透明串**：只校验长度与字符集，⛔ 不当整数解析（计划 §1.3 / 三审 M6）。
HALL_PULL_CURSOR_MAX_LENGTH = 512
_HALL_PULL_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
#: 上家**广播**的三类（触发点接线见 `peer_dispatch.enqueue_hall_event` 的调用点）。
HALL_BROADCAST_KINDS: tuple[str, ...] = (
    HALL_THROW_KIND,
    HALL_TAKE_KIND,
    HALL_REPRICE_KIND,
)
#: 下家接收侧认的全部大厅事件（`peer_inbound.apply_events` 的分派表）。
HALL_INBOUND_KINDS: frozenset[str] = frozenset({
    HALL_THROW_KIND,
    HALL_TAKE_KIND,
    HALL_REPRICE_KIND,
    HALL_CLAIM_OK_KIND,
    HALL_CLAIM_REFUSED_KIND,
})

#: 下家**只接受**上家这 4 种状态（业务方裁定；落点在 `peer_inbound.apply_events`）。
DOWNSTREAM_ACCEPTED_STATUSES: frozenset[str] = frozenset(
    {"IN_PROGRESS", "ISSUE", "PAUSED", "REFUNDED"}
)

#: 下家这 4 种状态**才回传**给上家（业务方裁定）。
UPSTREAM_TRIGGER_STATUSES: frozenset[str] = frozenset(
    {"PENDING_REVIEW", "COMPLETED", "ISSUE", "PAUSED"}
)

#: 下家状态 → 上家目标状态。⭐ `COMPLETED → PENDING_REVIEW` 是**有意的**（原话：
#: 「已完成只能由本系统自己更改——订单需要验收，不能说你下家已完成我也已完成了」）。
UPSTREAM_STATUS_MAP: dict[str, str] = {
    "PENDING_REVIEW": "PENDING_REVIEW",
    "COMPLETED": "PENDING_REVIEW",
    "ISSUE": "ISSUE",
    "PAUSED": "PAUSED",
}

#: 白名单里**不是 `Order` 的直接列**的那几项（`order_payload` 现取）。
#: 键 = 白名单里的名字，值 = 取值路径。⚠️ 模型守卫用例按这张表把派生项排除掉，
#: ⛔ 别把派生项直接从白名单里删掉（那样出站载荷会静默少一个字段）。
DERIVED_ORDER_FIELDS: dict[str, str] = {
    "game_name": "order.game.name",
}

#: 拒绝的主机形状（spec §9-S9 的 SSRF 闸门）。
_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}

#: RFC 6598 共享地址空间（CGNAT / Tailscale 节点 / 运营商 NAT 内部地址）。 ⚠️ `ipaddress` 的 `is_private` **不认**这一段——实测 Python 3.13：`100.64.0.1` 的
#: `is_private` / `is_reserved` / `is_loopback` / `is_link_local` / `is_multicast` 全为 `False`，只有 `is_global` 为 `False`。`IPv4Address.is_global` 的源码注释写明
#: 「`is_global` 与 `is_private` 相反，唯一例外是 `100.64.0.0/10`（两者都为 False）」 ⇒ 控制方裁定 R9：spec §9-S9 的「内网地址」包含这一段，必须显式判。
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")

#: **纯数字 / 点**形态的主机：不可能是真正的域名（DNS 没有全数字顶层域名），也**不是**
#: 规范 IP 写法 ⇒ 一律拒。⚠️ 这一条**不管**「libc 能不能解析」（那是 `_parses_as_ipv4` 的事）：
#: `256.1.1.1`、`1.2.3.4.5` 这类串 `inet_aton` 与 `getaddrinfo` 都不认，但放它们过闸门没有收益。
_NUMERIC_HOST_PATTERNS = (re.compile(r"^[0-9.]+$"),)


def _parses_as_ipv4(host: str) -> bool:
    """这个主机串会不会被 libc 当成 **IPv4** 解析（`inet_aton` 的口径）。 ⚠️ 只在 `ipaddress.ip_address()` **已经失败**之后调用：`1.2.3.4` 这种规范写法
    在那一支之前就处理完了（否则会把合法公网 IP 也拒掉）。 ⚠️ 用 `inet_aton` 一次判**整个类别**，不再逐条补正则——正则只能「补一个漏一个」（`AGENTS.md` §5-E / E-0010）：`0x7f.0.0.1` / `127.0.0.0x1` / `0x7f.0x0.0x0.0x1` 这类
    **混合十六进制标签** `ipaddress` 不认，本机 libc 的 `getaddrinfo` 却解析成 `127.0.0.1` （实测输出见 Task 2 报告）。 ⛔ 这不是 DNS 解析（本期明确不做，那是已知残差）：只判「串本身是不是数字编码的 IPv4」。"""
    try:
        socket.inet_aton(host)
    except OSError:      # 实测：非 ASCII 主机名也走 OSError（不是 UnicodeEncodeError）
        return False
    return True


def new_token() -> str:
    """生成一套新令牌（明文只出现一次：要么存下来外发，要么立刻交给对方）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(raw: str) -> str:
    """验证侧只存哈希（spec §9-S3）。 ⚠️ 用 **sha256 而不是 Django 的密码哈希**：这是 32 字节随机串、不是人选的口令，
    没有字典攻击面，也不需要 salt 与慢哈希（每次同步都要比对，慢哈希是纯浪费）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def token_matches(raw: str, hashed: str) -> bool:
    """定时安全比较（spec §9-S7）：⛔ 不用 `==`。"""
    return hmac.compare_digest(hash_token(raw), hashed)


#: URL 闸门自己那句「格式不对」文案（spec §9-S9 / §14-R7）。 ⛔ **必须是自有中文**：`urlsplit()` 与 `SplitResult.port` 抛的是**裸 `ValueError` + Python 英文原文**，
#: 而任何把本函数的异常文案直接当 `msg` 回给外部的调用方都会把它漏出去（`bind/` 已改成回固定文案；Task 5 的页面端点会回显 `msg`）。实测三条会漏出去（本机 Python 3.13）： `http://[::1` ⇒ "Invalid IPv6 URL"、
#: `http://example.com:abc` ⇒ "Port could not be cast to integer value as 'abc'"、 `http://example.com:99999` ⇒ "Port out of range 0-65535"。本闸门一律把它们翻成下面这一句（`tests/test_peer_sync_pure.py` 钉住它）。
BASE_URL_MALFORMED_MSG = "网址格式不对（域名或端口写法有误）"


def normalize_base_url(raw: str) -> str:
    """URL 闸门（spec §9-S9）：只允许 http/https、必须带主机、⛔ 拒绝内网地址。非法一律抛 `ValueError` —— 调用方把它翻成**业务码**（不是 500），因为「用户填了个内网地址」是**预期**的失败（`AGENTS.md` §5-E / E-0010）。
    只接受「**主机（域名或公网 IP 字面量，可含端口）**」这一个**基址**：路径 / 查询 / 锚点 / 用户名密码一律**报错**，⛔ 不静默丢掉——Task 5/7 会往这个基址后面拼同步端点路径，静默改写就是「没报错、结果是错的」（`AGENTS.md` §5-E / E-0010）。
    ⚠️ 公网 IP 字面量（如 `https://203.0.113.10:8443`）**是合法输入**（本闸门只拒内网/组播/ 数字编码那几类）⇒ 上面这句别读成"只收域名"。 ⚠️ **本函数抛出的 `ValueError` 文案全部是自有中文**（含下面两处把 stdlib 英文原文翻成 `BASE_URL_MALFORMED_MSG` 的地方）⇒ 调用方可以安全地把它当 `msg` 用， ⛔ 但**别**往这里加会抛英文原文的调用而不翻译。"""
    text = (raw or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        # `urlsplit` 自己会抛英文 `ValueError`（实测 `http://[::1` ⇒ "Invalid IPv6 URL"）
        # ⇒ 在闸门内翻成自有中文，⛔ 别让调用方把框架原文回给外部（spec §14-R7 同族）。
        raise ValueError(BASE_URL_MALFORMED_MSG) from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("网址必须以 http:// 或 https:// 开头")
    # E-0261：`hostname` 可能带尾点（`localhost.` / `127.0.0.1.` 是合法 DNS 写法——
    # FQDN 的根域显式形式，libc 解析结果与不带尾点**相同**、都指向本机），精确比较
    # 黑名单会漏 ⇒ 在归一化处剥掉。剥尾点不改合法输入的语义，只让两种写法汇成同一个比较键（普通域名带尾点照旧放行）。
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("网址里没有主机名")
    if parts.username is not None or parts.password is not None:
        raise ValueError("网址里不允许带用户名或密码")
    if parts.path not in ("", "/"):
        raise ValueError("网址里不允许带路径，只填到域名（含端口）为止")
    if parts.query:
        raise ValueError("网址里不允许带查询参数（? 后面的部分）")
    if parts.fragment:
        raise ValueError("网址里不允许带锚点（# 后面的部分）")
    if host in _BLOCKED_HOSTS:
        raise ValueError("不允许指向本机地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 不是规范 IP 字面量：可能是域名（放行），也可能是**数字编码**的绕过写法（拒）。
        if _parses_as_ipv4(host) or any(p.match(host) for p in _NUMERIC_HOST_PATTERNS):
            raise ValueError("不允许使用数字编码形式的主机地址") from None
    else:
        if ip.is_multicast:      # IPv4 224.0.0.0/4、IPv6 ff00::/8（裁定 R9 归入本闸门）
            raise ValueError("不允许指向组播地址")
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip in _CGNAT_V4   # RFC 6598：`is_private` 不认它，见 `_CGNAT_V4` 注释
        ):
            raise ValueError("不允许指向内网地址")
        if isinstance(ip, ipaddress.IPv6Address):
            # `urlsplit().hostname` 已把方括号剥掉；重建 URL 时必须补回，
            # 否则 `http://2606:4700:4700::1111` 这种串根本不是合法 URL（存库/出站都挂）。
            host = f"[{host}]"
    try:
        port_value = parts.port
    except ValueError:
        # `SplitResult.port` 自己会抛英文 `ValueError`（实测 `http://example.com:abc` ⇒
        # "Port could not be cast to integer value as 'abc'"、`:99999` ⇒ "Port out of range 0-65535"）
        # ⇒ 同样在闸门内翻成自有中文。⚠️ 保留原来的**检查顺序**（端口判在最后），免得 `ftp://host:abc` 这类输入换一个报错理由。
        raise ValueError(BASE_URL_MALFORMED_MSG) from None
    port = f":{port_value}" if port_value is not None else ""
    return f"{parts.scheme.lower()}://{host}{port}"


def is_negative_amount(value) -> bool:
    """金额值是不是**负数**（纯值域判据，⛔ 不管形状，⛔ 不量化）。 ⭐（D-0306 / D-0307）：**订单流**的负价是合法边界（照落 `total_price`）， **大厅镜像行**永不负价 ⇒ 「这个价要不要进大厅 / 要不要往外传播」的判据只有这一处，
    由 `peer_dispatch`（入队侧）与 `peer_inbound`（落地侧）共用，⛔ 别各抄一份 sign 判断。 - `None` ⇒ `False`（「没带价」不是负数，另有一档判它）；
    - 认不出（非数值 / 空串 / 容器）⇒ `False`：形状错由既有的形状闸负责， ⛔ 不把形状错伪装成值域错（两类跳过的机器码与发送侧处置不同）； - `"-0.00"` 归一化后等于 0 ⇒ `False`（D-0015：0 是合法边界，不是负数）。"""
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) < 0
    except (ArithmeticError, TypeError, ValueError):
        return False


def effective_peer_ref(order) -> str:
    """peer 同步的**身份键**（`PeerSyncEvent.target_ref` / 大厅卡号 / 对账键）： ⭐ **唯一判据 = `jidanpu_member.effective_source_order_no(order)`**（业务方：跨系统一律用来源单号做身份，内置单号只在本系统内排重用）。本函数**只做转发**， ⛔ 不在这里另写一份「`source_order_no or order_no`」——同一事实两处判据必然漂移
    （`AGENTS.md` §5-E）。为什么本模块要这个薄壳、而不是让每个调用方各自 import：出向有 5 处生产写入点（`peer_dispatch` 的四类订单事件 / 截图 / 大厅广播，`peer_worker` 的撤单投递前复核），收向的解析口也在 `peer_inbound`（`_local_order_for_peer_ref`）——**一个个 import 会把
    「唯一出处」抄成 5 份字面**；留一格转发点，改口径时只有这一处。 ⚠️ **jidanpu_member 只在调用时 import**：`peer_sync` 会被 `apps/core/authentication.py` 在模块级 import（纯函数区），模块级拉进 `jidanpu_member` 会把一整条租户业务依赖链带进基座层的导入期；且两边在导入顺序上互为环（`peer_inbound` 的同款处置）。 ⚠️ 返回值可能是**空串**（`Order.order_no` 为空只有夹具才会出现）——调用方按列宽 / 形状闸自己判；本函数如实返回（⛔ 不补一个假单号）。"""
    from apps.business import jidanpu_member

    return jidanpu_member.effective_source_order_no(order)


def order_payload(order, *, origin_order_no: str = "") -> dict:
    """按白名单取一单的载荷（spec §10.1）。 ⭐ **游戏传的是名字**（`order.game.name`，**裁决 R21**）：⛔ 不传对端 `game_id`—— 每个租户的 `Game.id` 都从 1 开始 ⇒ 对端的 id 在本租户永远能解析到、却几乎总是另一款游戏（静默错单：接口回 200 + `applied:1`，单子却挂错了游戏、客服可能看不到）。接收侧按名字在本地 `Game` 里对齐，解析不到就**自动建一款同名游戏** （A14 起；落点 `apps/business/peer_inbound.py::resolve_game`）。 `server_id` / `project_id` 仍按 id 传、接收侧本期不消费（对齐是 P2，如实登记）； `project_text` 是自由文本，接收侧原样落（D9：可空 FK + 自由文本，**以 project_text 为准**）。 ⚠️ **返回值必须是 JSON 安全的**（2026-09-25 Task 6 实测补上）：`Order.total_price` 是
    `DecimalField` ⇒ 原样放进 `PeerSyncEvent.payload`（`models.JSONField`）时， psycopg 的 json dumper 会用内置 `json.dumps`，直接抛 `TypeError: Object of type Decimal is not JSON serializable`（Task 6 的 RED 证据）。对端那条出站 HTTP 请求体同理。 ⛔ **整类处理，不要逐字段补**（`AGENTS.md` §5-E / E-0010）：走 `DjangoJSONEncoder` （Django 给 `JsonResponse` 用的同一个），把 `Decimal` / `datetime` / `UUID` 一类统一翻成 JSON 能装的形状；`int` / `str` / `None` **原样不变** （所以 `test_peer_sync_pure.py` 里那几条 `SimpleNamespace` 值断言逐字不动仍绿）。 ⚠️ 金额仍**原样传、不做换算也不四舍五入**（spec §10.3）：`Decimal("100.00")` 变
    `"100.00"` 是同一个值的 JSON 表示，不是口径变化——与 DRF 在本仓把金额序列化成字符串的既有口径一致（`COERCE_DECIMAL_TO_STRING` 默认 True）。 ⭐ **`origin_order_no`（P4 多跳链身份，spec `2026-10-25-peer-hall-share-design.md` §5.3.1 第 6 条）**：非空时才多带这一个键，值是「这张单在**链上最初那一跳**的单号」（取值见 `peer_dispatch._origin_order_no`）。它是**单号、不是 PII**，与 `order_no` / `source_order_no` 同一族。为什么必须有它（多跳时「卡的单号」与「单的单号」不是同一个）：二次转发沿链**不换卡上的单号**（`peer_dispatch.mirror_hall_payload` 送的是镜像行的 `peer_order_no` = 源头单号），而真单的 `order_no` 是**每一跳自己生成**的 ⇒ 第三层（C）拿到 B 推来的 `order.assign` 时，拿 `order_no` 去对大厅里那张卡**永远对不上**（C 那张卡记的是 A 的号）。接收侧按 `payload.get("origin_order_no") or payload["order_no"]` 认卡 ⇒ **单跳时这个键不出现、行为逐字不变**（本租户原生单 / 对端是链头）。 ⚠️ **⛔ 不要把它塞进 `PEER_ORDER_FIELDS`**：那张表是「`Order` 上存在的列」的清单，而本键是**链身份**（来自 `PeerOrderLink`，不是本单的列）；`test_peer_sync_pure.py` 的 `EXPECTED_PEER_ORDER_FIELDS` 是外部契约值，两张清单不是同一个东西。"""
    raw: dict = {}
    for field in PEER_ORDER_FIELDS:
        if field == "game_name":
            # 游戏名走 FK（`Order` 上没有 `game_name` 这个列）；FK 取不到时给 `None`，
            # 由接收侧按「解析不到 ⇒ 跳过」处理，⛔ 不在这里猜一个名字。
            game = getattr(order, "game", None)
            raw[field] = getattr(game, "name", None)
            continue
        raw[field] = getattr(order, field, None)
    # ⭐ P4 多跳链身份：**只在有值时出现**（单跳 / 原生单 ⇒ 键不出现 ⇒ 接收侧逐字按旧口径
    #    用 `order_no` 认卡）。
    if isinstance(origin_order_no, str) and origin_order_no:
        raw["origin_order_no"] = origin_order_no
    # `json.loads(json.dumps(...))`：编码器把非 JSON 类型翻成 JSON 类型，解码回
    # `dict` / `list` / 基本类型——**唯一**的「整类」收口点。
    return json.loads(json.dumps(raw, cls=DjangoJSONEncoder))


def source_name_for(peer_name: str) -> str:
    """把上家工作室名截成 `OrderSource.name` 装得下的长度（spec A11）。
    ⚠️ **有意截断、有意不报错**（业务方裁定）：代价是前 10 字相同的两个上家
    **共用同一个来源**（spec §14-R10）。⛔ 不要"顺手加个后缀"或改成抛错。"""
    return (peer_name or "").strip()[:ORDER_SOURCE_NAME_MAX]


# ── 大厅拉取：游标 / 分页形状（纯函数，计划 `2026-10-25-peer-hall-pull` §1.3）──────────
def encode_hall_cursor(hall_entered_at: datetime, source_rank: int, pk: int) -> str:
    """把 `(进厅时刻, 来源档位, 主键)` 编成**不透明**游标串（`urlsafe-base64`，无填充）。 - 来源档位：原生 `0` / 镜像 `1`（同一时刻的并列次序见端点排序键）；
    - ⚠️ 游标只是**分页键**：`created_at` / `first_seen_at` 不读作业务时刻（计划 §11.1
    的残差：翻页期间旧单重新进厅可能被本轮漏见，下一轮见到即复活）； - ⚠️ ⛔ 不把游标做成整数偏移：见 `decode_hall_cursor` 的形状闸。"""
    raw = f"{hall_entered_at.isoformat()}|{int(source_rank)}|{int(pk)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_hall_cursor(raw) -> tuple[datetime, int, int]:
    """解开 `encode_hall_cursor` 的产物；形状不对一律抛 `ValueError`（调用方翻 `1000`）。
    ⚠️ **只校验长度 / 字符集 / 三段结构**，⛔ 不把游标当整数或偏移量解析——
    游标是**不透明串**（计划 §1.3 / 三审 M6 的改准点）。"""
    if not isinstance(raw, str) or not raw:
        raise ValueError("cursor 必须是一个字符串")
    if len(raw) > HALL_PULL_CURSOR_MAX_LENGTH:
        raise ValueError("cursor 太长")
    if not _HALL_PULL_CURSOR_RE.match(raw):
        raise ValueError("cursor 的字符集不对")
    padding = "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(f"{raw}{padding}".encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValueError("cursor 解不开") from None
    parts = decoded.split("|")
    if len(parts) != 3:
        raise ValueError("cursor 的结构不对")
    try:
        moment = datetime.fromisoformat(parts[0])
        rank = int(parts[1])
        pk = int(parts[2])
    except (TypeError, ValueError):
        raise ValueError("cursor 的结构不对") from None
    if rank not in (0, 1) or pk <= 0 or moment.tzinfo is None:
        raise ValueError("cursor 的结构不对")
    return moment, rank, pk


def hall_pull_limit(raw) -> int:
    """`limit` 的形状闸 + 上限：缺省 100、上限 200；非整数 / 负数 / `bool` ⇒ `ValueError`。
    ⚠️ **`bool` 必须单独拒**：`True` 是 `int` 的子类，放过去会被当成 1（同 `_incoming_hop`
    的既有口径）。⚠️ 超过上限是**夹紧**、不是报错（计划 §1.3 只把形状错归 `1000`）。"""
    if raw is None:
        return HALL_PULL_DEFAULT_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError("limit 必须是整数")
    if raw <= 0:
        raise ValueError("limit 必须是正整数")
    return min(raw, HALL_PULL_MAX_LIMIT)
