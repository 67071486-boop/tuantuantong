"""鸡蛋铺**官方 Open Platform API** 的客户端（spec §3/§7.1/§9；本线 T2）。本模块与 `apps/business/jidanpu.py` 是**两套系统**（⛔ 别把两边的口径互相搬）——①登录式：地址+账号+密码、先登录（SDK 内部）、成功判据交给 vendor SDK（读端点 `lenient`）、端点 `/OrderAjax/*` 等；②官方 API（本模块）：地址+`client_id`+`client_secret`、先 `POST /oauth/token`（`client_credentials`）、⭐ 成功判据 **`code > 0`**（spec §3.2 F5/F6）、端点 `/api/OpenPlatform/*`。⛔ **不共用参数模板、也不共用判成败口径**（`AGENTS.md` §5-E 的 E-0007/E-0087）：同一个产品里两条路的端点、字段名、码制都不一样——实测「只读回 `1`、**录单回 `605764`**」就是最直接的反例。
本模块边界（T2）：⭐ 读+写封装，但**仍然只有封装**——谁在什么时候打写端点由**分派层**决定（`jidanpu_member_openapi_outbox`，整条②式出向分派**默认关着** `JIDANPU_MEMBER_OPENAPI_OUTBOX`）；写端点成功判据与只读**同一个出处**（`is_success_code` ⇒ `code > 0`），⛔ 不拿「没抛异常」当成功（E-0087）；⛔ **凭据只从「绑定那一行」取**（租户边界）、⛔ **密钥与 token 不进日志/异常/返回值**（失败只记「哪一步、哪一档、HTTP 与 code」，与 `jidanpu.py` 既有口径一致）；⭐ 属于本模块：T3 回调地址拼装+「码 → 密钥」0600 文件的**写入侧**（读侧在 `apps/callback/` ⛔ 不改那条线）、T7 状态映射表+「该不该写/认不出来怎么办」判据（**只出判据、不写任何东西**，「该不该写」复用 `jidanpu_upstream.should_write_status`）、T11 ②式「测试连接」=**逐条只读探测**（⛔ 一个写接口都不碰、⛔ 不落库）；证据等级（实测/文档明写/我们自定）逐项出处见各函数 docstring 与外移登记，超时与 token 缓存口径是**我们自定**（对方没给超时口径，限流确切数值没有实测，登记为推断，spec §8.3）。
⭐ T3 写入侧（spec §5.1/§5.3/§14-T3）：**回调地址**形状 `https://<该租户主域名>/api/jidanpu/callback/<22 位码>`，域名取 `platform.Domain` 的 `is_primary` 行（⚠️ 与 spec §5 字面写法有出入——一租户一库下 `PlatformTenant` 在租户库里是空表、照字面会永远回空串，**已登记为对 spec 的偏离**，理由见 `tenant_primary_domain`）；**密钥文件**=`$R/shared/callback-keys.env`、**0600**、一行一条 `<码>\t<client_secret>`，格式的另一半（读侧）在 `apps/callback/config.py`（已冻结）⇒ 本模块只**写**：同目录临时文件+`os.replace` 原子替换、**只增不减**、密钥不进日志/异常/返回值；⛔ **唯一写盘点=「保存绑定」那一次**（跟着它的 `transaction.atomic()` 走）：**写不进文件 ⇒ 整个保存回滚**（⛔ 不留「库里说配好了、文件里没有」的半态——那种半态下回调全部 401/503 而页面与库里一切正常）。"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import httpx
from django.conf import settings
from django.db import connection
from jidanpu.errors import ConfigError

#: ⭐ 回调那条线的**读侧**常量（T4，已冻结）：⛔ 本模块**不改它**，只**复用**它的两个定义 —— 路径前缀 / 定长码与分隔符各写一遍必然漂移（一处改了另一处不改 ⇒ 地址拼得出来而接收器
#: 认不出，或反过来：文件里写的是 `=` 而读侧按 TAB 切）。⚠️ `apps.callback.wsgi` / `apps.callback.config` **都不 import Django**（那条线故意的）⇒ 从业务层 import 它们是安全的。
from apps.callback import wsgi as callback_wsgi
from apps.callback.config import KEYS_FILE_SEPARATOR, callback_keys_path

#: ⭐ T7 的**唯一判据出处**：状态写不写由**既有那一个函数**判（spec §6.3 明令复用）。
#: ⛔ 别在本模块再写一套 —— 两处判据必然漂（spec §6 的原话），而且它是**登录式上家线**已经
#: 验收过的安全闸（它挡的正是「已完成单被改回待验收 + `completed_at` 静默清空」那一幕）。
from apps.business import jidanpu_upstream
#: 我方订单状态枚举。⚠️ **必须起别名**：本模块里 `OrderStatus` 这个名字已经被
#: `GetOrderStatus` 那个**回包形状**的 dataclass 占了（见下），两个东西重名会让
#: 「远端状态」与「我方状态」在代码里长得一模一样（本仓踩过同形不同义的坑）。
from apps.business.jidanpu_proxy import proxy_url
from apps.business.models import OrderStatus as OurOrderStatus
from apps.business.platform_egress import vendor_transport

logger = logging.getLogger(__name__)

# ── 端点路径（⛔ 逐字：拼错一个就是一次静默失败）────────────────────────────────
#: 取 token（spec §3.1 F2；文档：`grant_type=client_credentials` + `client_id` + `client_secret`）。
TOKEN_PATH = "/oauth/token"
#: 查订单状态：`orderId` / `sourceId` **二选一**（spec §3.3 F8 实测两个入口都通）。
ORDER_STATUS_PATH = "/api/OpenPlatform/GetOrderStatus"
#: 查订单进度图列表：`orderId`（spec §3.3 F9 实测取回某单 15 张进度图）。
ORDER_PROCESS_IMGS_PATH = "/api/OpenPlatform/GetOrderProcessImgs"
#: 取游戏列表（spec §3.3 F10 实测：带 `id` / `name`）。
GAMES_PATH = "/api/OpenPlatform/GetGames"
#: 取常用项目：⚠️ **必须带 `gameId`**（spec §3.3 F11 实测：**不带会 404**）。
PROJECT_PATH = "/api/OpenPlatform/GetProject"

# ── 写端点路径（二期步骤 1；形状照上面那四条只读常量，⛔ 拼错一个就是一次静默没写进去）──────
#: 录单。⚠️ **「我方发单价」走 `assignPrice`（⛔ 不是 `price`）**，见 `create_order`。
CREATE_ORDER_PATH = "/api/OpenPlatform/CreateOrder"
#: 改订单状态（⭐ `status` 收**中文标签**，⛔ 不按文档那份不全的枚举校验）。
CHANGE_ORDER_STATUS_PATH = "/api/OpenPlatform/ChangeOrderStatus"
#: 改价。⚠️ 实测它写的是**大厅/抛单价 `recFee`**、不是接单价 ⇒ 见 `modify_order_price`。
MODIFY_ORDER_PRICE_PATH = "/api/OpenPlatform/ModifyOrderPrice"
#: 改单。⚠️ 它的系统单号那一格叫 **`orderID`**（大写 `ID`），与别的端点的 `orderId` **不是一个拼写**。
MODIFY_ORDER_PATH = "/api/OpenPlatform/ModifyOrder"
#: 上传截图（派单传我方截图那条路；二期步骤 6 才接线）。
UPLOAD_IMG_PATH = "/api/OpenPlatform/UploadImg"

# ── 「哪一步失败」的名字（异常与日志都用它，⛔ 不用行号 / 不用地址）──────────────
STEP_TOKEN = "token"
STEP_ORDER_STATUS = "GetOrderStatus"
STEP_ORDER_PROCESS_IMGS = "GetOrderProcessImgs"
STEP_GAMES = "GetGames"
STEP_PROJECT = "GetProject"
#: 写端点那五步（二期步骤 1）：⚠️ 它们**只进失败文案与日志**，⛔ 不进 `PROBE_ORDER`
#: （探测那一趟一个写接口都不许打，见 `probe_read_endpoints`）。
STEP_CREATE_ORDER = "CreateOrder"
STEP_CHANGE_ORDER_STATUS = "ChangeOrderStatus"
STEP_MODIFY_ORDER_PRICE = "ModifyOrderPrice"
STEP_MODIFY_ORDER = "ModifyOrder"
STEP_UPLOAD_IMG = "UploadImg"

#: 步骤 → 人话（页面上的「逐项点名」用它；⛔ 别在别处再写一份）。
STEP_LABELS: dict[str, str] = {
    STEP_TOKEN: "取 token（/oauth/token）",
    STEP_ORDER_STATUS: "查订单状态（GetOrderStatus）",
    STEP_ORDER_PROCESS_IMGS: "查订单进度图（GetOrderProcessImgs）",
    STEP_GAMES: "取游戏列表（GetGames）",
    STEP_PROJECT: "取常用项目（GetProject）",
    STEP_CREATE_ORDER: "录单（CreateOrder）",
    STEP_CHANGE_ORDER_STATUS: "改订单状态（ChangeOrderStatus）",
    #: ⚠️ 刻意**不写「发单价」**：实测它改的是大厅/抛单价 `recFee`、不是接单价 ⇒ 标签只报端点、
    #: 不替那一格下结论（见 `modify_order_price`）。
    STEP_MODIFY_ORDER_PRICE: "改价（ModifyOrderPrice）",
    STEP_MODIFY_ORDER: "改单（ModifyOrder）",
    STEP_UPLOAD_IMG: "传图（UploadImg）",
}

#: `mode` 这一列里「官方 API 方式」的取值（spec §2.1：它是**唯一**判据）。
#: ⛔ 判据一律写显式相等，⛔ **不许**写 `if binding.client_id:` 那种真值判据 ——
#: 切回登录式之后 `client_id` 还在，那一句会**静默走官方 API**（E-0021 家族）。
OPENAPI_MODE = "openapi"

#: `grant_type` 的固定值（文档原文）。
GRANT_TYPE_CLIENT_CREDENTIALS = "client_credentials"

#: 单次请求的超时（秒）。⚠️ **我们自定**：对方没给超时口径，而这条线的调用会进**常驻循环**
#: ⇒ 取比登录式 SDK 默认（30 秒）更短的值，别让「上游卡住」把一轮拖到它自己的墙钟上限。
DEFAULT_TIMEOUT = 15.0

#: token 提前续期的余量（秒）：在 `expires_in` 到期前这么久就重新取，
#: 免得「刚拿到手就过期」。⚠️ 我们自定（文档只说 `expires_in` 是有效期）。
TOKEN_EXPIRY_SKEW_SECONDS = 60.0

#: token 缓存的条数上限（防常驻进程里无界增长：键含密钥指纹，换密钥会产生新键）。
TOKEN_CACHE_MAX_ENTRIES = 64

#: ⭐ **凭据被拒之后的退避窗口**（秒）——spec §9.5 ②「⛔ 不每轮反复打一个必拒的接口」的落地。 ②式消费者不设间隔闸（每一拍都跑）、取 token 是每轮第一跳 ⇒ 权限被收回后那一发会每 3 秒打一次。
#: 取 10 分钟 = 与兜底对账默认间隔同量级（这条路上本来最慢的节拍）；⛔ **只对 `CREDENTIALS`（①档）生效** （③档下一轮很可能就好了，退避它 = 把「抖了一下」变成 10 分钟静默停摆）。代价：窗口内权限开回来了
#: 也要等窗口过完才自愈（页面「测试连接」不走退避）⇒ 判据是「比人手去开权限快」，⛔ 不是「尽快重试」。
CREDENTIAL_BACKOFF_SECONDS = 600.0

#: 退避窗口内的**人话**（进台账 / 绑定 `last_error`：⛔ 不许静默跳过，那会让人以为这条路没坏）。
#: ⚠️ 末段一定要带上「为什么」：窗口本身不是原因，**被拒**才是（照 §9.3 纪律 1）。
CREDENTIAL_BACKOFF_REASON = (
    "上一步刚被鸡蛋铺拒过凭据（多半是「开放平台」没开通 / 权限被收回）⇒ "
    "这一轮**不再重复打**那个必被拒的接口（等待窗口 {seconds:.0f} 秒，到点自动重试）：{detail}"
)
#: `User-Agent`。⚠️ 不带任何租户 / 账号信息（那是身份，⛔ 不许外泄给上游）。 ⚠️ **风控降险线如实登记（有意未改）**：本线仍自报产品名，与①式已换**浏览器 UA** 的做法不一致
#: （`docs/plans/2026-10-20-upstream-waf-risk-reduction.md` §2.2 只点名 `jidanpu.py` 两个构造点，
#: 本行不在范围）；若对端以后按 UA 拦②式，改法一样：换浏览器串（与 `jidanpu.BROWSER_USER_AGENT` 共用）。
USER_AGENT = "tuantuantong-jidanpu-openapi/0.1"

#: 判定「内层权限不足」时用的**公共子串**：spec §3.3 F15 实测两处文案是 `没有权限` 与
#: `你没有权限`，⇒ ⛔ **不许**按某一个具体整句写死（下一个人会以为那两处文案是一样的）。
PERMISSION_NEEDLE = "权限"

#: ⭐ 判定「**外层门槛**没开通」用的两个特征词。实测原文是 `您没有开放平台接入权限`（spec §9.1-A）； ⛔ 同样**不按整句相等**判 —— F15 已经证明同一件事的文案会有多种写法。
#: ⚠️ **命中宽严按步不同（§23.2 的裁定，别把两处合成一条）**： · **取 token 那一步用 `any`**（任一命中即 ①：「权限」二字在那一步本身就是强信号）；
#: · **只读端点那一格用 `all`**（两个字都命中才是 ①；只回「没有权限」是**内层**含义 ⇒ ②）。
OUTER_GATE_WORDS: tuple[str, ...] = ("开放平台", PERMISSION_NEEDLE)

#: 脱敏后的占位串（⛔ 别改成空串：空串会让「抹掉了」与「本来就没有」分不清）。
REDACTED = "***"

# 三段**逐字**的用户文案（templates）。⛔ 别在这里改写：它们是 spec 明文要页面上用的那几句。
#: A 档（没开通）：spec §9.3 逐字。⚠️ 末句的「左边那个选项」是对标弹窗里的**位置描述**，
#: 本模块照抄不改（spec 要求逐字）；页面那一侧若布局不同，由页面决定怎么摆（⛔ 不在客户端里改文案）。
NO_OPENAPI_ACCESS_TEXT = (
    "这个账号在鸡蛋铺那边还没开通「开放平台」接口（对方回：{peer}）。\n"
    "请联系鸡蛋铺给你这个账号开通开放平台，拿到 client_id 与 client_secret；\n"
    "或者改用「账号 + 密码」方式对接（左边那个选项）。"
)
#: B 档（权限不全）：spec §11.5 逐字（`{items}` = 逐项点名）。
PERMISSION_INCOMPLETE_TEXT = (
    "开放平台的权限还没配全：{items}。这几项我们暂时用不了，不影响订单状态同步。"
)
#: ⭐ **「闸那一行」专用的说法**（总指挥裁定；收尾 §25.2 的那句话）。判别维度是「**这一行是不是闸那一行**」（`ENABLE_GATE_STEPS` 里 `kind=permission` 的行，今天只有
#: `GetOrderStatus`），⛔ 不是「哪个界面/哪条路径」：那句话对闸那一行**在任何地方都是假的** （拒启用恰恰是因为这一跳缺了、状态同步跑不起来）⇒ PUT「启用被拒」与「测试连接」逐条表里
#: 那一行的 `message` **都用这一句**（表里说「不影响」就是说假话）。 ⚠️ 非闸行照旧用 `PERMISSION_INCOMPLETE_TEXT`（那里「不影响」是真的）；`token` 被挡走 ①/③，不套这句。
GATE_BLOCKED_PERMISSION_TEXT = (
    "开放平台的权限还没配全：{items}。这一项缺了，订单状态同步就跑不起来，所以还不能启用。"
)
#: C 档（我们这边 / 网络）：spec §11.5 逐字（`{kind}` = 异常类型 / HTTP 状态）。
CONNECT_FAILED_TEXT = "我们这边连不上鸡蛋铺的开放平台（{kind}），请把这条反馈给管理员"


class FailureKind(StrEnum):
    """失败的三档（spec §9.1 的 A / B / C）。
    ⛔ **不许把这三档合并成一个**：它们的**用户指引完全不同**。"""

    #: ① 凭据 / 开通问题（A 档）：账号在鸡蛋铺那边**还没开通开放平台**，或者取 token 这一步
    #: 就被拒了（`client_id` / `client_secret` 不对也归这一档）。⇒ 只能找鸡蛋铺，或改用登录式。
    CREDENTIALS = "credentials"
    #: ② 权限问题（B 档）：门开了，但**这个应用**对**这一项**没有权限
    #: （实测 `GetEmpList` ⇒ `没有权限`、`GetRecOrderRankList` ⇒ `你没有权限`）。
    #: ⇒ 用户在鸡蛋铺那边把这一项开出来即可（spec §9.1-B 的「不影响订单状态同步」照写进文案）。
    PERMISSION = "permission"
    #: ③ 其它业务 / 网络失败（C 档）：网络异常、5xx、响应不是 JSON、404、上游改版……
    #: ⛔ **绝不许**把它说成「你没有权限」（spec §9.1-C：那会把排查方向带偏）。
    OTHER = "other"


#: 各档的「下一步怎么办」——⚠️ **①②的指引完全不同**，这是分档**存在的理由**（spec §9.1）： ① 是「外层门槛没开通」⇒ 只能找鸡蛋铺；② 是「门开了、里面某一项没配」⇒ **用户自己就能开**
#: （spec §20.1 内层 = API 子账户在**他自己那套**系统里的权限）。②那句措辞照 spec §20.2-2（T11）：末尾不带「或联系鸡蛋铺」、点明去哪里开 + 给谁开。⛔ 这两句一个字都不许合并
#: （合并 = 把「求官方开通」与「自助授权」混成一件事，用户会去求一个不需要求的人）。
USER_ACTION_BY_KIND: dict[FailureKind, str] = {
    FailureKind.CREDENTIALS: "让鸡蛋铺给这个账号开通「开放平台」（拿到 client_id / client_secret），"
                             "或改用「账号 + 密码」方式对接。",
    FailureKind.PERMISSION: "去那套鸡蛋铺系统的「权限设置」里，给这个 API 子账户把这一项权限打开"
                            "（这一步你自己就能做，不用找鸡蛋铺）。",
    FailureKind.OTHER: "把这条反馈给管理员：这是我们这边（或网络）的问题，不是鸡蛋铺拒绝了权限。",
}

#: 三档在日志 / 异常文本里的人话（⛔ 页面上用的那几句在 `NO_OPENAPI_ACCESS_TEXT` 等常量里）。
KIND_TEXT: dict[FailureKind, str] = {
    FailureKind.CREDENTIALS: "账号/凭据这一层被拒（可能还没开通「开放平台」）",
    FailureKind.PERMISSION: "开放平台的权限没配全（这一项没有权限）",
    FailureKind.OTHER: "其它失败（网络 / 协议 / 上游改版）",
}


def step_label(step: str) -> str:
    """步骤 → 人话；认不出来的步骤**原样回**（⛔ 不吞、也不编一个更好听的名字）。"""
    return STEP_LABELS.get(step, step)


def _redact(text, secrets=()) -> str:
    """把文本里出现的**每一把密钥 / token**换成 `***`（用完即弃，⛔ 不保存 `secrets`）。
    ⚠️ 为什么需要它：失败文案要把**对方的原文**带给用户（spec §9.3 纪律 1），而对方有可能把
    我们发过去的请求体（含 `client_secret`）回显在 `msg` 里 —— 那是最容易被忽略的一条泄漏路径。"""
    if text is None:
        return ""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), REDACTED)
    return out


def is_success_code(code) -> bool:
    """⭐ **成功判据 = `code > 0`**（spec §3.2 F5/F6 实测；文档原话「大于0代表成功」）。实测读数（判据存在的全部理由）：只读成功回 `code = 1`；⭐ 录单成功回 `code = 605764`；
    失败回 `code = 0` + 一句 `msg`。⇒ ⛔ 绝不许写 `code == 1`（会把录单成功判成失败）、 ⛔ 也绝不许写 `code == 0`（那是 vendor SDK 登录式读端点的 `lenient` 口径，会把全部成功判失败）；
    判据只有这一处出处，⛔ 调用点不许再写比较式。 ⚠️ `code` 非整数一律判失败（fail-closed：这里没有源码只有实测读数）；`bool` 是 `int` 子类而 `code: true` 不是成功码 ⇒ 显式排掉。"""
    if isinstance(code, bool) or not isinstance(code, int):
        return False
    return code > 0


def classify_failure(*, step, http_status=None, code=None, msg=None) -> FailureKind:
    """把一次失败分到 §9.1 的 A / B / C 三档（**纯函数** ⇒ 判据能单独钉）。两个**互相独立**的「外层门槛」信号（⛔ ①②能分开的承重设计）： 1. **结构性**：失败发生在 `step="token"`（HTTP 200 且 `code` 不是成功码）——取 token 本身就是那道外层门槛；⭐ 但**也要看报文**：对方用自己的故障码 + 「系统繁忙」表达它自己
    出故障时，按结构判 ① 会把用户支使去找鸡蛋铺开权限（指引错方向）。 2. **文案**：HTTP 200 + 失败码 + `msg` 命中特征词 ⇒ ①。宽严两处刻意不同（§23.2 裁定）：取 token 那步用 `any`（「权限」二字在那一步是强信号）；只读端点那格用 `all` （只回「没有权限」是**内层**含义 ⇒ 判 ②，用户可自助开那一项，spec §20 / §9.1-B）。
    3. 两处没命中 ⇒ `msg` 含「权限」⇒ ②（只读端点格）；取 token 那步没有 ② 可言 ⇒ ③ 并带对方原文。 ⛔ C 档两条成对判据（spec §9.1-C / §13.3-C9）：网络异常 / 5xx / 非 JSON / 其它 HTTP 状态 **一律 ③**；只有 HTTP 200 + `code` 存在且非成功码才谈「权限」二字 ⇒ 「连接失败」在结构上不可能被判成「没权限」。⚠️ HTTP 401/403 归 ①（实测内层权限不足走的是 200 + `code=0` + msg， F12/F13，所以 HTTP 层被拒只可能是门没开 / 凭据不对）。"""
    rejected = http_status == 200 and code is not None and not is_success_code(code)
    if rejected:
        text = msg or ""
        #: ⭐ **取 token 那一步的判据 = 任一命中**（§23.2 的总指挥裁定）：
        #: 在**取 token**这一步，「权限」二字本身就是强信号（那时还谈不上「某个接口的权限」）
        #: ⇒ `any`。反例（对方用自己的故障码 + 一句「系统繁忙」）两个词都不含 ⇒ 仍走 ③ ✓。
        if step == STEP_TOKEN:
            return (
                FailureKind.CREDENTIALS
                if any(word in text for word in OUTER_GATE_WORDS)
                else FailureKind.OTHER
            )
        #: 只读端点那一格：**两个字都命中**才归 ①（那里判「外层门槛」要比取 token 那一步**严**：
        #: 实测原文 `您没有开放平台接入权限` 两个词都在；而只读端点回一句「没有权限」是**内层**
        #: 含义（某一项权限没配）⇒ 判 ②，与 ① 的用户指引完全不同（§20 / §9.1-B）。 ⛔ 这一格一个字都不许跟着上面那个 `any` 变。
        if all(word in text for word in OUTER_GATE_WORDS):
            return FailureKind.CREDENTIALS
        if PERMISSION_NEEDLE in text:
            return FailureKind.PERMISSION
        return FailureKind.OTHER
    if http_status in (401, 403):
        return FailureKind.CREDENTIALS
    return FailureKind.OTHER


class OpenApiError(Exception):
    """官方 API 的一次失败，**带类型**（§9.1 的 A/B/C 三档）。 ⚠️ 四个字段各自承重，⛔ 别合并： - `kind`：A/B/C 三档（`FailureKind`）—— 用户指引完全不同； - `step`：**哪一步**失败（取 token / 哪个端点）—— 文案与排查都用它；
    - `http_status` / `upstream_code` / `transport`：取证用（⛔ 都不是密钥）； - `origin`（⇒ `binding_id` / `tenant_schema`）：**哪个租户的哪一份绑定**打出来的（批量失败的归因）。⚠️ 两者都⛔ 不是凭据；缺省 `None` 时文本里不出现它。
    - `upstream_msg`：**对方原样给的文字**（spec §9.3 纪律 1 要求把它带给用户） —— 它是唯一可能含外部文本的字段，所以**必须**过一遍密钥抹除。 ⛔ **不含密钥 / token**：构造时会把 `secrets` 里的每一把从**所有**文本字段里抹掉，然后**丢弃** `secrets` 本身（⛔ 不留引用、不进 `repr`）。"""

    def __init__(self, *, step, kind, http_status=None, upstream_code=None,
                 upstream_msg=None, transport=None, detail=None, secrets=(), origin=None,
                 empty_data=False):
        self.step = step
        self.kind = FailureKind(kind)
        self.http_status = http_status
        self.upstream_code = upstream_code
        self.empty_data = empty_data
        self.transport = transport
        self.upstream_msg = _redact(upstream_msg, secrets)
        self.detail = _redact(detail, secrets)
        #: ⭐ **归因**：这一批失败是**哪个租户的哪一份绑定**打出来的。批量调用（逐单查状态 / 兜底对账）失败时，「哪一份绑定坏了」是最先要回答的问题，
        #: 而光看 `step` 答不出来。⚠️ 两者都**不是凭据**（schema 名与行主键在绑定页面上
        #: 本来就看得到），所以可以进文本；凭据仍然只走 `secrets` 那一条路。
        self.binding_id, self.tenant_schema = origin if origin else (None, None)
        super().__init__(self._text())

    def _text(self) -> str:
        """一句话说清**哪一步、哪一档**（⛔ 不带密钥值；对方原文脱敏后才带上）。"""
        text = f"鸡蛋铺官方API「{step_label(self.step)}」失败：{KIND_TEXT[self.kind]}"
        bits = []
        if self.http_status is not None:
            bits.append(f"HTTP {self.http_status}")
        if self.upstream_code is not None:
            bits.append(f"code={self.upstream_code}")
        if self.transport:
            bits.append(self.transport)
        origin = "／".join(part for part in (
            f"租户 {self.tenant_schema}" if self.tenant_schema else "",
            f"绑定 {self.binding_id}" if self.binding_id is not None else "",
        ) if part)
        if origin:
            bits.append(origin)
        if bits:
            text += "（" + "，".join(bits) + "）"
        if self.detail:
            text += f"（{self.detail}）"
        if self.upstream_msg:
            text += f"：{self.upstream_msg}"
        return text

    @property
    def user_text(self) -> str:
        """**页面上要显示的那一段**（逐字取自 spec §9.3 / §11.5，⛔ 别改写、⛔ 别省掉对方原文）。"""
        if self.kind is FailureKind.CREDENTIALS:
            return NO_OPENAPI_ACCESS_TEXT.format(peer=self.upstream_msg or "（对方没有给文字说明）")
        if self.kind is FailureKind.PERMISSION:
            return PERMISSION_INCOMPLETE_TEXT.format(items=step_label(self.step))
        text = CONNECT_FAILED_TEXT.format(kind=self.transport or self.detail or "未知")
        if self.upstream_msg:
            #: ⭐ ③ 的口径**不止「连不上」**：对方也可能用**业务失败**表达它自己那边出故障（自己的故障码 + 一句「系统繁忙」）⇒ ③ 这一档**照样**要求
            #: 把**对方原文**如实带给用户（spec §9.3 纪律 1），⛔ 不许把它说成「你没开通开放平台」
            #: （那是 ① 的话，会让用户去开一个不需要开的权限）。
            text += f"（对方原话：{self.upstream_msg}）"
        return text

    @property
    def user_action(self) -> str:
        """这一档「接下来该做什么」——⚠️ **①与②是两句不同的话**（这就是分档的意义）。"""
        return USER_ACTION_BY_KIND[self.kind]


def _answered_despite_business_failure(exc: OpenApiError) -> bool:
    """这次失败是不是「**对方应答了、只是业务上失败**」（T11 探测用，spec §9.2「只要有响应、不是权限错就算通」）。判据三条**合取**（⛔ 一条都不能少）：`http_status == 200`（传输层没出事）； `upstream_code is not None`（挡「HTTP 200 但报文里没有 `code`」——把「对方改版了」说成
    「接口可用」是说假话）；`kind is OTHER`（①②都不算「可用」）。 ⚠️⚠️ **它分不出「查不到这张单」与「参数不合法」**（真机实测）：`GetOrderStatus` 两档回同一个 `code=0`，只有 `msg` 能分辨，而本判据不看 `msg` ⇒ 调用方 `find_order_status` 读成「对方没有
    这张单」的结论里**也包含**「我们没传单号」那一档。⛔ 本判据本身一个字都不改（三条已钉死档位）；兜底与代价见 `find_order_status` 的 docstring（录单会被对方来源单号查重响亮拒掉，⛔ 不静默）。"""
    return (
        exc.http_status == 200
        and exc.upstream_code is not None
        and exc.kind is FailureKind.OTHER
    )


def absent_order_error(exc: OpenApiError) -> bool:
    """识别查单的普通业务失败，供旧调用方沿用宽松的查询回退。"""
    return exc.step == STEP_ORDER_STATUS and _answered_despite_business_failure(exc)


def confirmed_absent_order_error(exc: OpenApiError) -> bool:
    """要求明确的无单回执，供会据此终止任务或允许录单的调用方使用。"""
    return (
        absent_order_error(exc)
        and str(exc.upstream_code) == "0"
        and exc.upstream_msg == "没有相关订单信息"
        and exc.empty_data
    )


@dataclass(frozen=True)
class OrderStatus:
    """`GetOrderStatus` 一页的结果（字段名与语义见模块 docstring 的证据表）。 ⚠️ 四个时刻**原样保留字符串**（⛔ 不解析成 `datetime`）：实测量到 `"2025-06-23 14:54:18"` 这种形状，多解析一层就多一个「我以为它是什么格式」的假设（`null` 也合法：`operTime` 实测就是）。 ## ⭐⭐ 回包里**恰好**这几格——真机实测（⛔ 别再往里加字段）。实测两次（`?orderId=` 与 `?sourceId=` 各一次），`data` 键集合**恰好 5 个**：`status` / `creatTime` /
    `operTime` / `doneTime` / `verifyTime`。⇒ ⛔ `orderId` / `price` / `recFee` 都不在里面（按文档"容错"加的那三格，真机一次都没回过）。两条承重后果： 1. **对方的系统订单号只能从建单接口的 `data` 取**（真机 `{"code":606917,"msg":"录单成功", "data":"20261009044818722526"}`）；「录单后按来源单号回读取号」这条路**不存在** ⇒
    `sourceId` 回读**只能确认「这张单查得到」**，"比号"判据恒假（症状 = 每次录单都判失败）。 2. **改价的"回读钉值"在②式做不到**（回读里没有 `recFee`）⇒ 只能以写端点成功码为准（口径见 `jidanpu_member_openapi_outbox.write_price`）。 `tests/test_jidanpu_openapi.py::test_the_order_status_shape_is_exactly_the_five_real_keys` 是**恰好集合**网（同时钉字段集合与假上游回的键集合）⇒ 谁再往任一侧加一格当场红。"""

    status: str
    creat_time: str | None = None
    oper_time: str | None = None
    done_time: str | None = None
    verify_time: str | None = None


@dataclass(frozen=True)
class _Token:
    """一个 token 与它的到期时刻（单调钟）。 ⚠️ `value` 用 `repr=False`：`repr(_Token(...))` 里**不许**出现 token
    （⛔ 返回值 / 日志 / 异常里都不许有它）。"""

    value: str = field(repr=False)
    expires_at: float


#: token 缓存：键 = （租户 schema、绑定 pk、地址、client_id、密钥指纹）。
#: ⚠️ `OrderedDict` 只为「超上限时按最早插入的淘汰」，⛔ 不是 LRU 语义。
_token_cache: "OrderedDict[tuple, _Token]" = OrderedDict()
_token_lock = threading.Lock()

#: ⭐ **凭据被拒的退避标记**（spec §9.5 ②）：`缓存键 → (单调钟, 那一档的人话, http_status, upstream_code, upstream_msg)`。
#: ⚠️ 键与 token 缓存**同一个**（含 schema / 绑定 / 地址 / `client_id` / 密钥指纹）⇒ **换了凭据或地址，标记自然失效**（⛔ 不用手工清，也不会把旧系统的结论套到新系统上）。
#: ⚠️ 它是**进程内**的观测（与 token 缓存同一条口径）：重启即忘 —— 那正是我们要的「重启一次就再试一发」（人改完配置重启服务是最常见的恢复动作）。
_credential_backoff: dict[tuple, tuple] = {}


def reset_token_cache() -> None:
    """清空 token 缓存 + **凭据退避标记**（用例之间隔离；换密钥 / 换地址后也可以显式清一次）。
    ⚠️ 正常路径**不需要**它：两个结构的键里都含密钥指纹与地址 ⇒ 换了凭据自然取新的，
    ⛔ 不会把旧系统的 token / 旧系统的失败结论拿去打新系统。"""
    with _token_lock:
        _token_cache.clear()
        _credential_backoff.clear()


def note_credential_rejection(cache_key, exc: OpenApiError, *, now: float | None = None) -> None:
    """记下「这个凭据刚被①档拒过」（spec §9.5 ②：⛔ 不每轮反复打一个必拒的接口）。 ⚠️ **只记 `FailureKind.CREDENTIALS`**：③档（网络抖动 / 5xx）**不记** —— 那一种下一轮很可能
    就好了，退避它等于把「我们这边抖了一下」变成一段时间的不打（说假话）。 ⚠️ 判据只看 `exc.kind`（分档的唯一出处是 `classify_failure`），⛔ 不在这里再判一次文案。"""
    if exc.kind is not FailureKind.CREDENTIALS:
        return
    with _token_lock:
        _credential_backoff[cache_key] = (
            time.monotonic() if now is None else now,
            str(exc), exc.http_status, exc.upstream_code, exc.upstream_msg,
        )
        # ⚠️ 与 token 缓存同一条口径的上限（防常驻进程里无界增长）。
        while len(_credential_backoff) > TOKEN_CACHE_MAX_ENTRIES:
            _credential_backoff.pop(next(iter(_credential_backoff)))


def credential_backoff_reason(cache_key, *, now: float | None = None) -> str:
    """这一份凭据此刻在**退避窗口**里吗：在 ⇒ 那句人话；不在 / 没记过 ⇒ **空串**。 ⚠️ 窗口内的**读数**与 `_token_for` 用的是**同一个**函数（`_backoff_start`）⇒ 调度层
    （`jidanpu_sync`）判「这一轮要不要跳过这一段」与取 token 那一跳**不可能**给出两种结论（那是本仓最恨的「同一件事两处判」）。⇒ ⛔ 别在调用点自己拿 `time.monotonic()` 再算一遍。"""
    with _token_lock:
        start = _backoff_start(cache_key, now=now)
        if start is None:
            return ""
        entry = _credential_backoff.get(cache_key)
        detail = (entry[4] or entry[1] or "").strip() if entry else ""
        moment = time.monotonic() if now is None else now
        return CREDENTIAL_BACKOFF_REASON.format(
            seconds=CREDENTIAL_BACKOFF_SECONDS - (moment - start), detail=detail[:120],
        )


def _backoff_start(cache_key, *, now: float | None = None) -> float | None:
    """窗口内那条标记的**起点**（单调钟）；`None` = 不在窗口里。 ⚠️ 判据只有这一处：`credential_backoff_reason`（调度层读）与 `_token_for`（取 token 那一跳）
    都调它 ⇒ 两处不可能给出两种结论。 ⚠️ **调用方必须已经持有 `_token_lock`**（`_token_for` 在锁内判它，而 `Lock` **不可重入**
    ⇒ 这里绝不能再自己加锁，否则常驻进程会**当场死锁**）。公开读口是下面那个 `credential_backoff_reason`，它自己加锁。"""
    entry = _credential_backoff.get(cache_key)
    if entry is None:
        return None
    moment = time.monotonic() if now is None else now
    if moment - entry[0] >= CREDENTIAL_BACKOFF_SECONDS:
        # 窗口过了 ⇒ 摘掉标记（下一发真的打出去，⛔ 不让它变成永久黑名单）。
        _credential_backoff.pop(cache_key, None)
        return None
    return entry[0]


def _token_cache_key(*, schema, pk, base_url, client_id, secret) -> tuple:
    """缓存键。⚠️ 故意**不带密钥明文**（指纹进键，⛔ 明文不进任何长期结构）。三个维度的理由（少一个都会出错）：
    - **schema**：两个租户的绑定 pk 可以都是 `1` ⇒ 少了它，A 租户的 token 会被 B 租户用上；
    - **地址**：spec §3.1 F1 实测「两个不同的地址背后是**两套系统**」⇒ 换地址必须重新取 token； - **密钥指纹**：换密钥自动失效。"""
    fingerprint = hashlib.sha256(str(secret).encode("utf-8")).hexdigest()
    return (schema, pk, base_url, client_id, fingerprint)


def _origin_of(cache_key) -> tuple:
    """缓存键 → `(绑定 id, 租户 schema)`（`OpenApiError` 的归因用）。 ⚠️ **这里是全模块唯一的解包点**：键的形状由 `_token_cache_key` 定义
    ⇒ 那边改了顺序，只改这一处（⛔ 别在 `_get` / `_token_for` 里各摊一次）。 ⚠️ 认不出的形状一律回 `(None, None)`（⛔ 不让归因字段把一条失败变成另一条异常）。"""
    try:
        schema, pk = cache_key[0], cache_key[1]
    except (TypeError, IndexError, KeyError):
        return (None, None)
    return (pk, schema)


def _decode_json(response, *, step, secrets, origin=None) -> dict:
    """解析响应体；**不是 JSON 对象** ⇒ ③（协议问题，响亮地失败，⛔ 不静默当空）。"""
    try:
        payload = response.json()
    except ValueError:
        raise OpenApiError(
            step=step, kind=FailureKind.OTHER, http_status=response.status_code,
            transport="响应不是 JSON", secrets=secrets, origin=origin,
        ) from None
    if not isinstance(payload, dict):
        raise OpenApiError(
            step=step, kind=FailureKind.OTHER, http_status=response.status_code,
            transport=f"响应不是对象（是 {type(payload).__name__}）", secrets=secrets, origin=origin,
        ) from None
    return payload


def _raise_for_envelope(*, step, response, payload, secrets, code_required=True, origin=None) -> None:
    """信封层的判据：HTTP ＋ `code > 0`。**不通过就抛 `OpenApiError`**（带三档类型）。 ⚠️ `code_required=False` **只给取 token 那一步**（文档明写的形状差别）：`/api/OpenPlatform/*`
    的成功体一定带 `code`，而 `/oauth/token` 的成功体是 **OAuth2 平铺体**（`access_token` / `token_type` / `expires_in`）**根本没有 `code` 格**，失败时才回 `{"code": 0, …}` ⇒ 不放宽的话
    每次成功取 token 都被判成失败；⛔ 别把这个放宽搬到只读端点上（那几个的 `code` 判据一点不能松）。 ⚠️ 日志**不带** `msg`（对方可能回显请求体）⇒ 只记「哪一步、哪一档、HTTP、code」； ③ 的文案括号里业务级失败补一句「上游业务失败」（⛔ 不落成「未知」，对方原文由 `upstream_msg` 带）。"""
    code = payload.get("code")
    msg = payload.get("msg")
    if response.status_code == 200 and (
        is_success_code(code) or (not code_required and code is None)
    ):
        return
    kind = classify_failure(
        step=step, http_status=response.status_code, code=code, msg=msg,
    )
    # ⚠️ 日志**不带** `msg`（对方有可能把请求体回显在里面）⇒ 只记「哪一步、哪一档、HTTP、code」。
    logger.warning(
        "鸡蛋铺官方API「%s」失败：kind=%s http=%s code=%s",
        step_label(step), kind, response.status_code, code,
    )
    #: ③ 的文案里那个括号：网络 / 协议那一档由 `transport` 填；**业务级** ③（对方自己的故障码
    #: + 一句人话）没有 `transport` ⇒ 这里补一句
    #: 「上游业务失败」，⛔ 不让它落成「（未知）」（对方原文由 `upstream_msg` 带上，见 `user_text`）。
    business_level_other = kind is FailureKind.OTHER and response.status_code == 200
    raise OpenApiError(
        step=step, kind=kind, http_status=response.status_code, upstream_code=code,
        upstream_msg=msg, detail="上游业务失败" if business_level_other else None,
        secrets=secrets, origin=origin, empty_data=payload.get("data") == "",
    )


def _request_token(http, *, base_url, client_id, secret, origin=None) -> _Token:
    """`POST /oauth/token`（`client_credentials`）⇒ 一个带到期时刻的 token。 ⚠️ 失败分档：**网络 / 5xx / 非 JSON ⇒ ③**；**业务级拒绝（含 401/403）**⇒ 看报文
    （`classify_failure` 的第 1 / 第 2 条信号）：报文说是外层门槛 ⇒ **①**，否则 ⇒ **③**。 ⚠️ ⛔ **不是「这一步永远是 ①」**（「这一步永远是 ①」那句旧口径已被推翻）：
    对方也可能用自己的故障码 + 一句「系统繁忙」表达**它自己那边出故障**，那与「你没开通开放平台」是两句**指引完全不同**的话 —— 前者要我们把原文反馈给管理员，后者要用户去找鸡蛋铺。"""
    url = base_url.rstrip("/") + TOKEN_PATH
    data = {
        "grant_type": GRANT_TYPE_CLIENT_CREDENTIALS,
        "client_id": client_id,
        "client_secret": secret,
    }
    secrets = (secret,)
    try:
        response = http.post(url, data=data)
    except httpx.HTTPError as exc:
        # ⛔ `from None`：传输层的原始异常文本可能带请求信息，这里只保留**类型名**（够排查）。
        logger.warning(
            "鸡蛋铺官方API「%s」失败：kind=%s %s",
            step_label(STEP_TOKEN), FailureKind.OTHER, type(exc).__name__,
        )
        raise OpenApiError(
            step=STEP_TOKEN, kind=FailureKind.OTHER, transport=type(exc).__name__,
            secrets=secrets, origin=origin,
        ) from None

    payload = _decode_json(response, step=STEP_TOKEN, secrets=secrets, origin=origin)
    _raise_for_envelope(
        step=STEP_TOKEN, response=response, payload=payload, secrets=secrets, code_required=False,
        origin=origin,
    )

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise OpenApiError(
            step=STEP_TOKEN, kind=FailureKind.OTHER, http_status=response.status_code,
            detail="响应里没有 access_token", secrets=secrets, origin=origin,
        )
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
        # 有效期读不出来 = 协议问题（⛔ 不默认一个「大概一小时」的猜测值：
        # 猜长了会被上游拒、猜短了会白打请求，两种都静默）。
        raise OpenApiError(
            step=STEP_TOKEN, kind=FailureKind.OTHER, http_status=response.status_code,
            detail=f"expires_in 不是正数（{type(expires_in).__name__}）", secrets=secrets,
            origin=origin,
        )

    ttl = float(expires_in) - TOKEN_EXPIRY_SKEW_SECONDS
    if ttl <= 0:
        # `expires_in` 比余量还短 ⇒ 按一半算，⛔ 绝不让 TTL 变成 0 或负数
        # （那会退化成「每次调用都取一遍 token」，而对方**有限流**）。
        ttl = float(expires_in) / 2
    return _Token(value=token, expires_at=time.monotonic() + ttl)


def _token_for(http, *, cache_key, base_url, client_id, secret, allow_backoff=True) -> str:
    """拿一个**仍有效**的 token；缓存没有 / 已过期才真的去打一次 `/oauth/token`。 ⭐ 缓存的两条承重理由：① 对方**有限流**（spec §8.3 登记为推断）而调用会进**常驻循环**，每次调用都取 token = 请求数翻倍纯白给；② 有效期由对方 `expires_in` 决定，提前
    `TOKEN_EXPIRY_SKEW_SECONDS` 续期。⚠️ 取 token 在**锁里**做（宁可等，不对上游连打几次）；缓存**不返回也不记录** token 值。 ⭐ **`allow_backoff`**（spec §9.5 ②）：默认 `True`——刚被①档拒过的凭据在退避窗口内**不再真的打**，
    直接抛重建的同一个 `OpenApiError`（带上一次对方原文）；**探测那两颗传 `False`**——那是人主动点的一次，必须真的打上游（否则拿到窗口内的旧结论 = 说假话）。 ⚠️ 成功取到 token ⇒ 顺手清掉退避标记（权限被重新开通这一种只能靠这一句清）。"""
    now = time.monotonic()
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            _token_cache.move_to_end(cache_key)
            return cached.value
        if allow_backoff and _backoff_start(cache_key, now=now) is not None:
            # ⚠️ **这个函数在锁内被调**（`Lock` 不可重入 ⇒ 它自己绝不能再加锁，见它的 docstring）。
            raise _backoff_error(cache_key)
    try:
        token = _request_token(
            http, base_url=base_url, client_id=client_id, secret=secret,
            origin=_origin_of(cache_key),
        )
    except OpenApiError as exc:
        # ⭐ spec §9.5 ②：**只对①档**记退避（③档记了会把一次抖动变成一段时间的静默停摆）。
        note_credential_rejection(cache_key, exc, now=now)
        raise
    with _token_lock:
        _token_cache[cache_key] = token
        _token_cache.move_to_end(cache_key)
        while len(_token_cache) > TOKEN_CACHE_MAX_ENTRIES:
            _token_cache.popitem(last=False)
        _credential_backoff.pop(cache_key, None)
        return token.value


def _backoff_error(cache_key) -> OpenApiError:
    """退避窗口内**代替真请求**抛出的那个异常（照上一次那一档原样重建）。 ⚠️ **重建而不是存一个异常实例**：实例带着构造时的调用栈（存下来会一直指着旧 traceback，
    排查时把人带偏）。⚠️ `secrets` 传空元组——`upstream_msg` 在记标记时**已经脱敏**，再抹一遍会把它抹成 `***` 反而看不出原文。
    ⚠️ **调用方必须已持有 `_token_lock`**（`Lock` **不可重入** ⇒ 这里⛔ 绝不能再 `with _token_lock` ——那会在常驻进程里**当场死锁**，症状：不打上游、也不写日志；实测踩过一次）。"""
    entry = _credential_backoff.get(cache_key)
    if entry is None:  # pragma: no cover - 调用点刚刚判过它在窗口里（并发下理论上会到）
        entry = (0.0, CREDENTIAL_BACKOFF_REASON.format(seconds=0, detail=""), None, None, None)
    _, _text, http_status, upstream_code, upstream_msg = entry
    return OpenApiError(
        step=STEP_TOKEN, kind=FailureKind.CREDENTIALS, http_status=http_status,
        upstream_code=upstream_code, upstream_msg=upstream_msg,
        detail=f"这一轮没有真的打上游（退避窗口内，{CREDENTIAL_BACKOFF_SECONDS:.0f} 秒）",
        origin=_origin_of(cache_key),
    )


def _as_rows(data, *, step, secrets) -> tuple[dict, ...]:
    """形状防御（照 `jidanpu._as_rows` 的口径）：载荷必须是「**全是对象**的列表」。
    ⛔ **不静默当成空列表**（那会把「上游改版 / 我们调用错」说成「远端没有数据」， `AGENTS.md` §5-E 的 E-0004），⛔ **也不静默丢掉畸形的行**（一行不是对象就抛）。
    合法空列表**照常回空**（不是畸形）。"""
    if not isinstance(data, list):
        raise OpenApiError(
            step=step, kind=FailureKind.OTHER,
            transport=f"data 不是列表（是 {type(data).__name__}）", secrets=secrets,
        )
    rows = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise OpenApiError(
                step=step, kind=FailureKind.OTHER,
                transport=f"data[{index}] 不是对象（是 {type(row).__name__}）", secrets=secrets,
            )
        rows.append(row)
    return tuple(rows)


# ══════════════════════════════════════════════════════════════════════════════
# 写封装的两个小配件（二期步骤 1；**只给写方法用**，⛔ 不碰只读那条路）
# ══════════════════════════════════════════════════════════════════════════════

#: 改这两个状态时 `remark` **必需**（文档原文：「备注说明（改问题单、已暂停时必需要）」）。
#: ⚠️ 这是**文档明写**（不是实测）：只用来把一次**本地就能判出来**的调用错误当场说清，
#: ⛔ 不用它去校验 `status` 本身（那份枚举不全，见 `change_order_status`）。
CHANGE_STATUS_REMARK_REQUIRED = frozenset({"问题单", "已暂停"})


def _exactly_one_ref(*, step, order_id, source_id) -> dict:
    """`orderId` / `sourceId` **二选一** ⇒ `{参数名: 值}`（恰好一项）；否则 `ValueError`。 ⚠️ 判「给了」用 `is not None`（`"0"` / `0` 都是合法单号，E-0021 家族）；两个都不给 / 都给是
    **调用方编程错误** ⇒ `ValueError`，⛔ 不翻译成业务码（那会让监控失明，E-0010）。判据只有这一处（`get_order_status` / `change_order_status` / `upload_img` 共用）。"""
    given = [(name, value) for name, value in (("orderId", order_id), ("sourceId", source_id))
             if value is not None]
    if len(given) != 1:
        raise ValueError(f"{step} 必须且只能给 orderId / sourceId 之一")
    return dict(given)


class OpenApiClient:
    """按**一行绑定**打官方 API 的客户端（四个只读端点 + 五个写封装）。 ⚠️ 写方法的**唯一生产调用方 = 分派层 `jidanpu_member_openapi_outbox`**（默认关着；传图 `upload_img` 今天仍无调用方）⇒ ⛔ 别以为「有写方法」=「系统会写上游」；加写封装后，
    在跑的调用方走过的路径**一字未变**。写端点成功判据与只读**同一个出处**（`is_success_code`）， ⛔ 不拿「没抛异常」当成功（E-0087）。⚠️ 实例上**没有** token 列（token 经模块级缓存取，
    ⛔ 不可能出现在 `repr(client)` 或任何返回结构里）；实例上**有** `client_secret` ⇒ 只放私有属性，`__repr__` 与日志都不带它。"""

    __slots__ = ("_http", "_base_url", "_client_id", "_secret", "_cache_key")

    def __init__(self, http, *, base_url, client_id, secret, cache_key):
        self._http = http
        self._base_url = base_url
        self._client_id = client_id
        self._secret = secret
        self._cache_key = cache_key

    @property
    def _origin(self) -> tuple:
        """这一份绑定的**归因** `(绑定 id, 租户 schema)`（进 `OpenApiError` 的失败文本）。"""
        return _origin_of(self._cache_key)

    @property
    def cache_key(self) -> tuple:
        """这一份凭据的**缓存键**（含 schema / 绑定 / 地址 / `client_id` / 密钥指纹）。 ⭐ 公开它只为**一件事**（T12）：调度层要拿它去问「这一份凭据是不是刚被①档拒过」
        （`credential_backoff_reason`）—— 那是**同一个**键，⛔ 不许在调度层自己拼一个（拼出来的键少一个维度就会变成「A 租户的结论套到 B 租户头上」，而且**静默**）。
        ⚠️ 键里⛔ **没有密钥明文**（只有指纹，见 `_token_cache_key`）。"""
        return self._cache_key

    def __repr__(self) -> str:
        # ⛔ 只回「身份 + 地址」，密钥与 token 一律 `***`。
        return (f"<OpenApiClient base_url={self._base_url!r} "
                f"client_id={self._client_id!r} secret={REDACTED}>")

    # ── 内部：取 token + 打一个只读端点 ─────────────────────────────────────────
    def _request(self, path, *, params, step, allow_backoff=True):
        """打一次**只读 GET**，回 `(response, payload, secrets)`。 ⭐ 「只读请求」的形状只有这一处（URL 拼接 + `Authorization` 头 + JSON 解码 + 传输层档位）：
        `_get` 与 `probe_path` 都只经它发请求，⛔ 不各写一份（否则探测会与取数漂成两种请求形状）。 ⚠️ 返回的 `secrets` 是「要抹掉的值」——⛔ 只往 `OpenApiError(secrets=…)` 里传，
        不拼文案、不存下来；`allow_backoff` 见 `_token_for`（探测传 `False`）。"""
        origin = self._origin
        token = _token_for(
            self._http, cache_key=self._cache_key, base_url=self._base_url,
            client_id=self._client_id, secret=self._secret, allow_backoff=allow_backoff,
        )
        url = self._base_url.rstrip("/") + path
        secrets = (self._secret, token)
        try:
            response = self._http.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "鸡蛋铺官方API「%s」失败：kind=%s %s",
                step_label(step), FailureKind.OTHER, type(exc).__name__,
            )
            raise OpenApiError(
                step=step, kind=FailureKind.OTHER, transport=type(exc).__name__,
                secrets=secrets, origin=origin,
            ) from None

        payload = _decode_json(response, step=step, secrets=secrets, origin=origin)
        return response, payload, secrets

    def probe_token(self) -> None:
        """探测的**第一跳**：取一次 token；拿到即通过，失败抛 `OpenApiError`（带三档）。
        ⚠️ 只调 `_token_for`（取法与缓存口径只有那一处，⛔ 不为探测重写）；⭐ **`allow_backoff=False`**
        （T12）：人主动点的一次必须真的打上游，⛔ 不许拿退避窗口里的旧结论回给用户（说假话，且会让人以为「点了也没用」）。"""
        _token_for(
            self._http, cache_key=self._cache_key, base_url=self._base_url,
            client_id=self._client_id, secret=self._secret, allow_backoff=False,
        )

    def probe_path(self, path, *, params, step, order_id_probe=False) -> str:
        """**只判信封层**的只读探测（T11）：这个接口此刻能不能用。返回 `""` = 可用；否则抛 `OpenApiError`（三档由 `classify_failure` 定）；可达但对方回业务失败 ⇒ 回**对方原话**。
        ⚠️ **不判 `data` 的形状**（`_get` 才判）：探测问「能不能用」而非「数据长什么样」，形状判据会把「上游改版」和「没权限」混成一条。⚠️ `order_id_probe=True` 只给「带一个不存在的单号」的那两项（spec §9.2 ③ 探测法）：那种请求本来就该被业务失败回掉 ⇒ 只要它是
        HTTP 200 + 失败码 + 非权限档，就说明「接口应答了、也没拒我们的权限」= 可用； ⛔ 这一放宽**不许**给 `GetGames` / `GetProject`（它们收到业务失败就是真的不能用）。 ⭐ **`allow_backoff=False`**（理由同 `probe_token`）。"""
        response, payload, secrets = self._request(
            path, params=params, step=step, allow_backoff=False,
        )
        try:
            _raise_for_envelope(
                step=step, response=response, payload=payload, secrets=secrets,
                origin=self._origin,
            )
        except OpenApiError as exc:
            if order_id_probe and _answered_despite_business_failure(exc):
                return str(exc.upstream_msg or "")
            raise
        return ""

    def _get(self, path, *, params, step):
        """打一个**只读**端点并回 `(data, secrets)`（信封判据在 `_raise_for_envelope`，只有一处）。
        ⚠️ 请求那一跳在 `_request` 里（与 `probe_path` 共用同一份形状）。"""
        response, payload, secrets = self._request(path, params=params, step=step)
        _raise_for_envelope(
            step=step, response=response, payload=payload, secrets=secrets, origin=self._origin,
        )
        if "data" not in payload:
            raise OpenApiError(
                step=step, kind=FailureKind.OTHER, http_status=response.status_code,
                detail="成功响应里没有 data", secrets=secrets, origin=origin,
            )
        return payload["data"], secrets

    # ── 四个只读端点 ───────────────────────────────────────────────────────────
    def get_order_status(self, *, order_id=None, source_id=None) -> OrderStatus:
        """`GetOrderStatus`：按**远端单号**或**我们的来源单号**查权威状态（二选一）。
        ⚠️ 两个都不给 / 都给一律 `ValueError`（调用方编程错误，⛔ 不翻译成业务码——那会让监控失明，
        E-0010）；判据在 `_exactly_one_ref`（写端点共用）。"""
        params = _exactly_one_ref(step=STEP_ORDER_STATUS, order_id=order_id, source_id=source_id)
        data, secrets = self._get(ORDER_STATUS_PATH, params=params, step=STEP_ORDER_STATUS)
        if not isinstance(data, dict):
            #: ⭐ **`http_status=200` 是承重的**（停滞修复，G2）：兜底对账要按「失败的指纹」区分「**这一张单**的形状坏」（⇒ 跳过它继续下一张）与「传输层 /
            #: 5xx」（⇒ 整轮停住）。**这里必然是 HTTP 200（`_raise_for_envelope` 已放行），写 200 是事实不是兜底**；少了它，这一档的指纹会与 `ConnectError` 一模一样
            #: （两者都是 `OTHER` + 没有 `code`）⇒ 一张坏单会把整轮钉死。
            raise OpenApiError(
                step=STEP_ORDER_STATUS, kind=FailureKind.OTHER, http_status=200,
                transport=f"data 不是对象（是 {type(data).__name__}）", secrets=secrets,
                origin=self._origin,
            )
        status = data.get("status")
        if not isinstance(status, str) or not status:
            # ⛔ 不静默回一个空状态：那会让上层把「认不出来」当成「某个状态」（状态映射那条线对「认不出来的值」有专门口径，前提是它**拿得到原值**）。
            # ⚠️ `http_status=200` 的理由同上：**这里必然是 HTTP 200（`_raise_for_envelope`
            # 已放行），写 200 是事实不是兜底** —— 这一档必须能与传输层失败分开。
            raise OpenApiError(
                step=STEP_ORDER_STATUS, kind=FailureKind.OTHER, http_status=200,
                detail="data.status 不是非空字符串", secrets=secrets, origin=self._origin,
            )
        return OrderStatus(
            status=status,
            creat_time=data.get("creatTime"),
            oper_time=data.get("operTime"),
            done_time=data.get("doneTime"),
            verify_time=data.get("verifyTime"),
        )

    def find_order_status(self, *, order_id=None, source_id=None) -> OrderStatus | None:
        """沿用旧查询回退，查单的普通业务失败也返回空结果。"""
        status, _ = self.find_order_status_with_absence(order_id=order_id, source_id=source_id)
        return status

    def find_order_status_with_absence(
        self, *, order_id=None, source_id=None, require_confirmation=False,
    ) -> tuple[OrderStatus | None, bool]:
        """报告下家是否明确返回无单，严格模式下其他失败照常抛出。"""
        try:
            return self.get_order_status(order_id=order_id, source_id=source_id), False
        except OpenApiError as exc:
            if confirmed_absent_order_error(exc):
                return None, True
            if not require_confirmation and absent_order_error(exc):
                return None, False
            raise

    def fetch_order_process_imgs(self, *, order_id) -> tuple[dict, ...]:
        """`GetOrderProcessImgs`：一单的进度图行（实测形状 `{url, uploadTime, name, note}`）。
        ⚠️ 行**原样返回**（本模块不裁字段）：去重按**对象 key** 那是消费者那条线的事
        （spec §3.5 F21），本模块只保证「形状是对象列表、畸形就抛」。"""
        data, secrets = self._get(
            ORDER_PROCESS_IMGS_PATH, params={"orderId": order_id}, step=STEP_ORDER_PROCESS_IMGS,
        )
        return _as_rows(data, step=STEP_ORDER_PROCESS_IMGS, secrets=secrets)

    def fetch_games(self) -> tuple[dict, ...]:
        """`GetGames`：游戏行（实测带 `id` / `name`）。
        ⚠️ 只该在**绑定检测**与**页面下拉**时打（spec §7.1 末段）：⛔ 不进每一轮轮询
        —— 那会给上游添没必要的负担。"""
        data, secrets = self._get(GAMES_PATH, params={}, step=STEP_GAMES)
        return _as_rows(data, step=STEP_GAMES, secrets=secrets)

    def fetch_projects(self, *, game_id) -> tuple[str, ...]:
        """`GetProject?gameId=`：某一款游戏的常用项目名（文档：`array[string]`）。 ⚠️⚠️ **`gameId` 必填**（spec §3.3 F11 **实测**：不带会 404，而 404 在调用方看来像「网络问题」）
        ⇒ 做成**必填关键字参数** + 发请求前再挡一道空值（直接把「我们漏传了参数」说清楚）。 ⛔ 判空**不看真值**（`game_id=0` 会被 `if not game_id` 当成没传，E-0021 家族）⇒
        判的是 `str(game_id).strip() == ""`。"""
        if str(game_id).strip() == "":
            raise ValueError("GetProject 必须带 gameId（实测不带会 404）")
        data, secrets = self._get(PROJECT_PATH, params={"gameId": game_id}, step=STEP_PROJECT)
        if not isinstance(data, list):
            raise OpenApiError(
                step=STEP_PROJECT, kind=FailureKind.OTHER,
                transport=f"data 不是列表（是 {type(data).__name__}）", secrets=secrets,
                origin=self._origin,
            )
        names = []
        for index, item in enumerate(data):
            if not isinstance(item, str):
                raise OpenApiError(
                    step=STEP_PROJECT, kind=FailureKind.OTHER,
                    transport=f"data[{index}] 不是字符串（是 {type(item).__name__}）",
                    secrets=secrets, origin=self._origin,
                )
            names.append(item)
        return tuple(names)

    # ══ 二期步骤 1：五个写端点（⭐ 只加封装；生产调用方只有分派层 outbox，传图未接线）════════════════
    # ⛔ 五条共同口径（⛔ 别在某个方法里单独改一条）： · 成功判据 = `code > 0`，唯一出处 `is_success_code`（⛔ 不拿「没抛异常」当成功，E-0087）； · 失败一律抛 `OpenApiError`（带 `upstream_code` 与对方 `msg`）；
    # · 参数名**逐字**照文档（副本在不进 git 的 `.tools/recon/jidanpu-openapi/raw-doc.txt`）； · 返回值 = 上游 `data` **原样**（⛔ 不判形状，理由见 `_write`）； · 请求形状（JSON 体 + 鉴权头 + 基址）只有一处：`_post`。接线：录单 / 推状态 / 改价 / 退单 / 改项目已由 `jidanpu_member_openapi_outbox` 办（默认关着），传图（二期计划步骤 6）仍未接线。

    def _post(self, path, *, body, step):
        """打一次**写 POST**（`application/json`），回 `(response, payload, secrets)`。 ⭐ 「写请求」的形状只有这一处（与 `_request` 那份**只读**形状并列，⛔ 不改那条）：
        取 token 口径、基址拼接、鉴权头、JSON 解码、传输层档位——五个写方法只经它发请求。 ⚠️ **请求体走 `json=`**：文档对这五个端点逐条写的是 `Body 参数 / application/json`
        ⇒ ⛔ 不写成表单、不塞进查询串（读用 query、写用 JSON，两套形状不许互搬，E-0007）。 ⚠️ `secrets` 口径与 `_request` 逐字相同；这里**不判信封**（判据在 `_write` 一处）。"""
        origin = self._origin
        token = _token_for(
            self._http, cache_key=self._cache_key, base_url=self._base_url,
            client_id=self._client_id, secret=self._secret,
        )
        url = self._base_url.rstrip("/") + path
        secrets = (self._secret, token)
        try:
            response = self._http.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "鸡蛋铺官方API「%s」失败：kind=%s %s",
                step_label(step), FailureKind.OTHER, type(exc).__name__,
            )
            raise OpenApiError(
                step=step, kind=FailureKind.OTHER, transport=type(exc).__name__,
                secrets=secrets, origin=origin,
            ) from None

        payload = _decode_json(response, step=step, secrets=secrets, origin=origin)
        return response, payload, secrets

    def _write(self, path, *, body, step):
        """打一个**写**端点并回上游 `data` **原样**（信封判据只有一处：`_raise_for_envelope`）。 ⭐ 成功判据 = `is_success_code(code)`（`code > 0`），与只读**同一个出处**（⛔ 不另写比较式，
        第二个真相源迟早漂）。⚠️ 这是**写**这件事上最贵的坑（**E-0087**）：对方可能回「`code = 0` + 一句非空 `msg`」的**假成功**信封，宽松解包会放行它 ⇒ 在这一行判失败抛错， ⛔ 绝不拿「没抛异常」当成功。
        ⚠️ **`data` 的形状不判**：写请求只有两种结局——成功（`code > 0`）或抛错；成功之后再补形状判据 = 把一次**已落地的写入**报成失败 ⇒ 调用方重试 ⇒ 在对家建出第二张单 / 重复推状态（比「读到怪形状」贵得多）。形状由调用方按各自用途判（录单要的是那串系统订单号）。"""
        response, payload, secrets = self._post(path, body=body, step=step)
        _raise_for_envelope(
            step=step, response=response, payload=payload, secrets=secrets, origin=self._origin,
        )
        return payload.get("data")

    def create_order(self, *, game_name, game_area, project, hours, order_soures, price,
                     customer_id, rec_user, assign_price, source_order_id=None, remark=""):
        """`CreateOrder`：**录单**（`POST`，JSON）。回上游 `data` = **系统订单号**（拆单时逗号分隔）。 ⭐ 参数名**逐字照文档**（逐格表见外移登记；`orderSoures` 原文就这个拼写，⛔ 别把①式字段名搬过来）。 ⭐ **`price`（订单总价）与 `assign_price`（我方发单价）是两格，⛔ 绝不照字面把「给打手的价」写进 `price`**（已实测：传 `price=20` + `assignPrice=10` ⇒ 对方存 `price=20` + `recFee=10`）。 ## ⛔ 禁用牌（spec §12.1-D5）：调用方**必须已裁字节**，本方法不裁
        `project` / `remark` 原样过线，⛔ 这里不做字节裁剪——对端那一格有**字节级**上限 ≈732 （E-0242 实测），裁剪**唯一出处**是 `jidanpu_common._clip_project`（700 字节，①式表单与 ②式出向共用同一把剪刀）。现役**唯一**生产调用点是 `jidanpu_member_openapi_outbox._create_body`；新增调用点 ⛔ 不许直接调本方法——先让值过那把剪刀。这条约束有 **AST 网**钉着（`tests/test_jidanpu_create_order_call_net.py`：全仓 `src/apps/**` 里 `.create_order(` 调用点
        只许出现在 outbox 模块；⛔ 别把 `order_intake.create_order`（我方建单内部函数，只是重名）混淆进来）。 ⚠️ 不把裁剪下沉进本方法：原语保持「原样过线」，谁在出向边界裁是**分派层**口径；在原语里再裁一刀 = 「同一个值两把剪刀」（两个选项，本仓选立 AST 网）。 ⚠️ 文档明写 `assignPrice > 0` 时对方会自动发它内部的抢单大厅（那是**对方**的大厅，与「撤下大厅」不是一件事）。⚠️ `customer_id` 传空串触发对方服务端异常（spec §7.1 F18）⇒ 调用方自己填合法值， ⛔ 本方法不替你编占位值；其余可选字段**暂不暴露**（⛔ 不铺没验过的字段）。⭐ 回包那格是调用方的事：本方法**原样回** `data`（可能 `None` / 空串 / 逗号分隔多段），「取哪一段」由调用方定口径（②式取第一段 + 留痕，见 `jidanpu_member_openapi_outbox._remote_order_ids`）。"""
        body = {
            "gameName": game_name,
            "gameArea": game_area,
            "project": project,
            "hours": hours,
            #: ⚠️ `orderSoures`（来源名称）与 `souresOrderID`（来源**单号**）是两格，别混。
            "orderSoures": order_soures,
            "price": price,
            "customerId": customer_id,
            "recUser": rec_user,
            "assignPrice": assign_price,
            "remark": remark,
        }
        if source_order_id is not None:
            body["souresOrderID"] = source_order_id
        return self._write(CREATE_ORDER_PATH, body=body, step=STEP_CREATE_ORDER)

    def change_order_status(self, *, status, order_id=None, source_id=None, remark=""):
        """`ChangeOrderStatus`：**改订单状态**（`POST`，JSON）。回上游 `data`（通常没有 ⇒ `None`）。 ⭐ **`status` 收中文标签**，且**文档那份枚举不全**（实测 `未分配` / `待上号` / `已退单` 也都收，二期计划 §2.3 / §2.4）⇒ ⛔ **本方法不按枚举校验**（会把实测能用的值挡在门外，判据出处是对方）；
        值**逐字**转发，⛔ 不做映射 / 加工。⚠️ `remark` 只挡「问题单 / 已暂停」+ 空白（文档明写必需；本地就能判出的调用错误 ⇒ `ValueError`）——它判的是**我们有没有给够参数**， ⛔ 与「对方收不收这个状态值」不是一回事。参数表见外移登记。 ## ⭐ `remark` 的**三档取值**（⭐ 唯一出处，⛔ 别在调用方各判一次）
        非空文本 = 这一格发那段文本（`问题单` / `已暂停` 必需）；**空串**（缺省）= 键在、值是空串—— ⛔ 照①式 `jidanpu.set_order_status` **恒发** `remark` 的已上线报文形态，改它 = 另一次改动； ⭐ **`None`** = ⛔ **整格不发**——退单 / 撤下大厅那一档（`refund_remote`）的口径是「`已退单` 不带 `remark`」且请求体被逐字钉成恰好两格。为什么给 `None` 单开一档：把「空串」也省掉会连**推状态**那一档的报文一起改掉（与①式逐字等价的形态，没在真机量过）—— 本线纪律是「先加能力、⛔ 不动已经上线的东西」。"""
        if str(status).strip() in CHANGE_STATUS_REMARK_REQUIRED and not str(remark).strip():
            raise ValueError(
                f"ChangeOrderStatus 改「{status}」时必须给 remark（文档：改问题单、已暂停时必需）"
            )
        body = _exactly_one_ref(
            step=STEP_CHANGE_ORDER_STATUS, order_id=order_id, source_id=source_id,
        )
        body["status"] = status
        # ⭐ `remark is None` ⇒ **整格不发**（见 docstring 那张表）；空串照旧发一个空的这一格。
        if remark is not None:
            body["remark"] = remark
        return self._write(CHANGE_ORDER_STATUS_PATH, body=body, step=STEP_CHANGE_ORDER_STATUS)

    def modify_order_price(self, *, order_id, price):
        """`ModifyOrderPrice`：**改价**（`POST`，JSON）。回上游 `data`（文档标为可选 ⇒ 常常 `None`）。 ⭐ **改的是哪一格：实测已核（二期步骤 0 / 步骤 3）**——`price` ⇒ 改对方的 **`recFee`** （建单 `assignPrice` 落的那一格），对方 `price`（订单总价）**不变** ⇒ ②式这**一个**改价端点
        改的就是「我方发单价」；**没有**第二个能改的价格（`recUser` / `assignPrice` 只在录单那一刻可传）。本方法只把 `price` **逐字**发出去（⛔ 不替调用方判「谁该来改这一格」）。 ⚠️⚠️ **「回读钉值」在②式做不到**（真机实测：`GetOrderStatus` 回包**没有 `recFee` / `price`**）
        ⇒ 调用方**只能以本端点成功码为准**（口径登记在 `jidanpu_member_openapi_outbox.PRICE_CONFIRM_WRITE_ONLY_REASON`）；⛔ 谁"按文档"写「改完回读 `recFee` 钉值」——那条判据**恒假**，症状是**每一次改价都停在「写后未能确认」**。"""
        return self._write(
            MODIFY_ORDER_PRICE_PATH, body={"orderId": order_id, "price": price},
            step=STEP_MODIFY_ORDER_PRICE,
        )

    def modify_order(self, *, game_name, game_area, project, order_id=None,
                     source_order_id=None, extra=None):
        """`ModifyOrder`：**改单**（`POST`，JSON）。回上游 `data`（通常没有 ⇒ `None`）。 ⛔ **这张表按实测写，⛔ 不按文档写**（E-0227 族：文档比实现宽松）——三发逐格读数、「文档 vs 实际」逐格表、参数表与第二现场读数见外移登记（取证原话须到不进 git 的 `.tools/recon/` 复现，⛔ 真值不进 `src/**`）。两条与文档不符的硬结论： ① `orderID` / `souresOrderID` / `gameArea` **实际三者都必填**（文档写「至少一项」/「可选」） ⇒ `game_area` 是**没有缺省值**的必填形参（谁顺手省掉会在调用处当场 `TypeError`）； ⛔ 不在这里给它编缺省值——合法取值来自**订单上的大区**（取值口
        `jidanpu_member_openapi_outbox._game_area`，带「永不为空」回落口径）； ② 文档「至少必填一项」只保留为**本地编程错误**闸（两个都不给 ⇒ `ValueError`，不发这一发）， ⛔ 与 `change_order_status` / `upload_img` 的「恰好一个」不是同一条规则。 ⚠️ `recUser` / `assignPrice` 不在本端点参数表（文档逐条核过）⇒ ②式**没有**「改抛单价 / 撤下大厅」接口；⭐⭐ 第二现场实测**发出去也无效、不会把单推回大厅** ⇒ 调用方**不发这两格** （撤下大厅走 `change_order_status` 改 `status=已退单`，二期计划 §4.2）。 ## ⭐⭐ **整单覆盖**（真机第二现场，本方法最贵的一条）
        ⛔ **这个端点是「发什么就留什么、没发的就用默认值」**（真机实测：只发那 5 格 ⇒ 没发的格被清掉，如 `orderSoure` → `'其它'`、`hours` → `720`、`originPrice` → `0.0`）⇒ `extra` 不是可选装饰：调用方**必须**把 `jidanpu_member_openapi_outbox.MODIFY_ORDER_REQUIRED_CELLS` 发全（哪几格、取值、证据的唯一出处 = `MODIFY_ORDER_PAYLOAD_CELLS`，⛔ 不在本方法再列一遍；共用取值口 = `_shared_body_cells`）。本方法只做「把 `extra` 并进 body」+ **撞键响亮抛** （那 5 格各有各的判据，⛔ 不许从 extra 覆盖）。⚠️ 只发**给出的**那一格单号（`is not None` 判）—— 今天唯一调用方两格都给，这条是留给别的用途的形状，⛔ 别读成「一个就够」。 ## ⭐ 其余文档「可选」格实测都不需要（③ 那发恰好 5 格就 `保存成功`） ⚠️ 边界如实登记：只证明「**这一发**上没有别的必填格」，⛔ 推不出「任何单上都没有」—— 带值的分支本线没量过；真机第一次遇到别的必填提示时，按**对方原话**回来补。"""
        body: dict = {}
        if order_id is not None:
            body["orderID"] = order_id
        if source_order_id is not None:
            body["souresOrderID"] = source_order_id
        if not body:
            raise ValueError(
                "ModifyOrder 必须至少给 orderID / souresOrderID 之一（文档：至少必填一项）"
            )
        body["gameName"] = game_name
        #: ⭐⭐ 实测必填（文档标「可选」）⇒ 与上面那两格单号同级、**无条件发**（见 docstring）。
        body["gameArea"] = game_area
        body["project"] = project
        if extra:
            clash = sorted(set(extra) & set(body))
            if clash:
                raise ValueError(
                    f"ModifyOrder 的 extra 与那 5 格撞键：{clash}（那 5 格的取值各有出处，"
                    "⛔ 不许从 extra 覆盖）"
                )
            body.update(extra)
        return self._write(MODIFY_ORDER_PATH, body=body, step=STEP_MODIFY_ORDER)

    def upload_img(self, *, img_url, note, order_id=None, source_id=None, img_type=0):
        """`UploadImg`：**传一张图**到那张单上（`POST`，JSON）。回上游 `data`（通常没有 ⇒ `None`）。 | 本方法 | 文档参数 | 必需 | 是什么 | |---|---|---|---| | `img_url` | `imgUrl` | ✅ | 图片 URL |
        | `note` | `note` | ✅ | 图片说明 | | `order_id` / `source_id` | `orderId` / `sourceId` | **二选一** | 同 `change_order_status` | | `img_type` | `ImgType` | — | ⚠️ 原文就是**大驼峰 `ImgType`**（对方那几张表里唯一一个这样写的）；`0`=进度图，`1`=进群二维码，默认 `0` |
        ⚠️ `img_type` **总是发出去**（默认 `0` = 文档的默认值）⇒ ⛔ 不用 `if img_type:` 那种真值判据（`0` 是**有意义的取值**，`AGENTS.md` §5-E 的 E-0021 家族）。 ⚠️ 本步骤**只有封装**：接线是二期计划的步骤 6（①式的「派单传我方截图」走会话与隐藏域， ②式只能用这个端点）。"""
        body = _exactly_one_ref(step=STEP_UPLOAD_IMG, order_id=order_id, source_id=source_id)
        body["imgUrl"] = img_url
        body["note"] = note
        body["ImgType"] = img_type
        return self._write(UPLOAD_IMG_PATH, body=body, step=STEP_UPLOAD_IMG)


def _is_openapi(binding) -> bool:
    """`mode` 是**唯一**判据（spec §2.1）：⛔ 不看 `client_id` 是不是空的。"""
    return binding is not None and (getattr(binding, "mode", "") or "") == OPENAPI_MODE


def is_openapi_binding(binding) -> bool:
    """`mode` 是**唯一**判据（spec §2.1）：这一行是不是②式。 ⭐ **给模块外的调用方用**（视图按方式分流那一处）：⛔ 别在调用方写
    `if binding.client_id:` 那种真值判据 —— 切回登录式之后 `client_id` 还在，
    那一句会**静默走官方 API**（E-0021 家族）。"""
    return _is_openapi(binding)


def _base_url_of(binding) -> str:
    """一行绑定的鸡蛋铺地址（⭐ **两条线通用**的唯一取址出处；取不到 ⇒ 空串）。取法顺序承重： ① 先认真实列 `jidanpu_base_url`（**两张绑定表都有**，⛔ 不是上家专有）；② 再回落 `base_url`（上家表上的只读别名；下家 `JidanpuMemberBinding` **没有**这个属性）。
    ⚠️ 为什么需要它（实测）：原来读 `base_url` 时对下家行永远回空串 ⇒ `openapi_credentials_of` 回 `None` ⇒ 每处调用 `ConfigError`（表面症状「地址不完整」，库里地址好好的——静默形态）。
    上家别名实现就是 `return self.jidanpu_base_url` ⇒ 两条路取同一个值（上游行为逐字不变）。顺手 `strip()`（复制粘贴的首尾空白很常见）；⛔ 只此一处，别处不许再写取址逻辑。"""
    for name in ("jidanpu_base_url", "base_url"):
        value = (getattr(binding, name, "") or "").strip()
        if value:
            return value
    return ""


def openapi_credentials_of(binding) -> tuple[str, str, str] | None:
    """取一行绑定的官方 API 凭据；**不是②式 / 不完整** ⇒ `None`（调用方按 `1015` 处理）。与 `jidanpu.credentials_of` 同口径的三条：
    1. ⛔ 只读**这一行**的三个字段（地址那一个走 `_base_url_of`，**两条线通用**）； 2. 再 `strip()` 一次（覆盖历史数据 / 直接从 shell 写库这类绕过序列化器的路径）；
    3. ⛔ **本函数不记日志、不回文案里的值** —— 密钥只以元组形式交回调用方。"""
    if not _is_openapi(binding):
        return None
    base_url = _base_url_of(binding)
    client_id = (getattr(binding, "client_id", "") or "").strip()
    client_secret = (getattr(binding, "client_secret", "") or "").strip()
    if not base_url or not client_id or not client_secret:
        return None
    return base_url, client_id, client_secret


def openapi_cache_key(binding) -> tuple | None:
    """这一行绑定的**缓存键**（token 缓存与凭据退避标记共用同一个键）；凭据不全 ⇒ `None`。 ⭐ 存在的理由（T12）：调度层要问「这一份凭据是不是刚被①档拒过」
    （`credential_backoff_reason`）—— 它**不该**为了拿一个键去建一个 HTTP 客户端（那既白开一次连接池，又把「读一个进程内标记」这件事绑到网络库的构造上）。
    ⇒ 键的拼法只有一处（`_token_cache_key`），本函数与 `openapi_client` 都用它。 ⚠️ 键里⛔ **没有密钥明文**（只有指纹）。"""
    credentials = openapi_credentials_of(binding)
    if credentials is None:
        return None
    base_url, client_id, client_secret = credentials
    return _token_cache_key(
        schema=_schema_name(), pk=getattr(binding, "pk", None), base_url=base_url,
        client_id=client_id, secret=client_secret,
    )


@contextmanager
def openapi_client(binding, *, timeout=DEFAULT_TIMEOUT):
    """按**一行绑定**建一个官方 API 客户端（凭据只从这一行取）。 ⚠️ 与 `jidanpu.open_client` 同口径三条：凭据只从这一行取（⛔ 不接受请求来源的地址 / 账号）；
    凭据不全 ⇒ `ConfigError` 且**文案不带任何值**（既有调用方按 `1015` 处理，复用同一异常类型）；连接的建与关由本函数负责。⚠️ 一条**实质差别**：这条路**不登录**，按需取 `client_credentials`
    的 token，且 token 由**模块级缓存**持有 ⇒ 连建两次客户端只取一次 token。 ⚠️ `follow_redirects=False`：这条路的 3xx 是**协议意外**，不该被静默跟随（登录式同理）。"""
    if not _is_openapi(binding):
        raise ConfigError("这行绑定不是「官方开放平台 API」方式")
    credentials = openapi_credentials_of(binding)
    if credentials is None:
        raise ConfigError("鸡蛋铺地址 / client_id / client_secret 不完整")
    base_url, client_id, client_secret = credentials
    cache_key = _token_cache_key(
        schema=_schema_name(), pk=getattr(binding, "pk", None), base_url=base_url,
        client_id=client_id, secret=client_secret,
    )
    transport = vendor_transport(base_url)
    network = {"transport": transport} if transport is not None else {"proxy": proxy_url()}
    with httpx.Client(
        timeout=timeout, follow_redirects=False, headers={"User-Agent": USER_AGENT},
        **network,
    ) as http:
        yield OpenApiClient(
            http, base_url=base_url, client_id=client_id, secret=client_secret,
            cache_key=cache_key,
        )


def _schema_name() -> str:
    """当前租户 schema 名（进缓存键 ⇒ 租户之间不串 token）。
    ⚠️ 延迟 import：本模块只在**真的建客户端**时才需要 Django 的连接对象，
    而 `manage.py check` / 纯函数用例不该被 ORM 牵扯。"""
    from django.db import connection

    return getattr(connection, "schema_name", "") or ""


# ══ T3：回调地址 +「码 → 密钥」文件的写入侧（spec §5.1 / §5.3）════════════════════
# ⛔ 本段只有两件事：**拼地址**（纯函数，读一行绑定 + 那个租户的主域名）与**写密钥文件**
#    （唯一一处写盘点，调用方是保存绑定的那个视图）。读侧在 `apps/callback/`（T4）。

#: 回调地址的协议。⚠️ 照 spec §5.1 的形状**写死 `https`**（⛔ 不用 `request.scheme`）：
#: 接收器挂在 nginx 上、只经 HTTPS 转进来；用请求的 scheme 会让「本机 http 调一下」拼出一个
#: 对方永远打不通的地址，而用户会照样把它填进鸡蛋铺那侧（那比不给地址更糟）。
CALLBACK_URL_SCHEME = "https"

#: 密钥文件的**首行注释**。⛔ **逐字**取自 spec §5.3 与 `apps/callback/config.py` 的模块
#: docstring（T4 那份是契约的另一半）。⚠️ 两处文本由
#: `tests/test_jidanpu_callback_keys.py` 直接对拍 ⇒ 任意一侧漂了就红（⛔ 别只改一处）。
CALLBACK_KEYS_FILE_HEADER = (
    "# 团团通 · 鸡蛋铺官方 API 回调密钥（0600；由「保存绑定」自动维护；⛔ 人工改要重启接收器）"
)

#: 同目录临时文件的前缀（⚠️ 必须**同目录**：`os.replace` 只在同一文件系统内原子）。
_TMP_PREFIX = ".callback-keys-"

#: 地址算不出来时的**人话原因**（spec §13.3-C10 要求「回空串 + 原因」；界面照它显示）。
CALLBACK_URL_REASON_NOT_OPENAPI = "这条绑定用的是「账号 + 密码」方式，没有回调地址"
CALLBACK_URL_REASON_NO_CODE = "这条绑定还没有回调码（保存一次就会生成）"
CALLBACK_URL_REASON_BAD_CODE = "这串回调码的形状接收器不认（只能是被人工改过库），地址暂时不给"
CALLBACK_URL_REASON_NO_DOMAIN = "查不到这个租户的主域名（平台租户域名表里没有可用的一行）"

# ── ⛔ 上家②式行不跑 outbox（T8 收口裁定，spec §15-N3；下家②式 outbox 在 `jidanpu_member_openapi_outbox`）────────────
#: 「待发的写动作发不出去」那条台账的 kind（≤ 32 字符）。
OPENAPI_OUTBOX_NOT_SUPPORTED_KIND = "openapi.outbox_not_supported"
#: 那条台账的 `target_type`（⛔ 与 `reconcile` / `callback` 分开：三类事实各自可查）。
OPENAPI_OUTBOX_TARGET_TYPE = "openapi_outbox"
#: 人话（⛔ **不含会变动的计数**：台账幂等键是 `(kind, target_type, target_ref, reason)` 四元组
#: ⇒ 计数一进 reason 就会「张数每变一次多一条台账」；计数只进返回值与 `payload`）。
OPENAPI_OUTBOX_NOT_SUPPORTED_REASON = (
    "本行是官方API（②式）：**写接口第一期不做**（spec §15-N3）⇒ 这条待发动作不会自动发出去，"
    "请人工处理或先切回①式；⛔ 本行不去建登录式连接（登录凭据本来也没有）"
)


def note_openapi_outbox_not_supported(binding) -> str:
    """②式绑定上还挂着**待发的写动作** ⇒ 留一条**幂等**台账 + 回一句人话。 ⛔ 当前只有「上家视角」在用：唯一调用方是 `jidanpu_sync._run_upstream_binding`（判 `payload.upstream_binding_id`），下家那一轮根本不跑 outbox ⇒ **这里没有下家的对应物**；
    保留本函数是下家后面可能要用（⛔ 不要删）。⭐ 存在的理由：outbox 的 handler 都要经①式登录客户端打写接口，而②式写接口第一期不做（spec §15-N3）⇒ 跳过后**必须**留痕（否则事件变成「静默发不出去」）。⚠️ 附带后果（如实登记）：上家②式路已撤（界面 / 接口不再接受 `mode="openapi"`）⇒ 本函数今天在正常路径上**不可达**（只有手写库行会走到）。
    回**空串** = 这一行没有待发事件（⇒ 调用方不打噪音）；否则回一句**带条数**的人话，计数进台账 `payload`。⚠️ 判据（哪些算「待发」）与 `jidanpu_upstream.run_outbox` 的取活条件**逐字相同** ——两处必须同一份口径，否则「outbox 认为待发、提示说没有」自相矛盾。本函数**不建连接、不读凭据值**。"""
    from apps.business.models import JidanpuSyncEvent

    pending = JidanpuSyncEvent.objects.filter(
        direction="out", kind__in=jidanpu_upstream.OUTBOX_KINDS,
        payload__upstream_binding_id=binding.pk,
        status__in=("pending", "failed"),
    ).count()
    if not pending:
        return ""
    jidanpu_upstream._note_once_ref(
        target_type=OPENAPI_OUTBOX_TARGET_TYPE, target_ref=f"{binding.pk}",
        kind=OPENAPI_OUTBOX_NOT_SUPPORTED_KIND, status="failed",
        reason=OPENAPI_OUTBOX_NOT_SUPPORTED_REASON,
        #: ⚠️ `direction="in"`：这一行是**我们登记的已知事实**（不是待发事件本身）
        #: ——照 `note_once` 的默认口径；⛔ 写成 `out` 会让它自己进 outbox 的取活集合。
        direction=JidanpuSyncEvent.Direction.IN,
        payload={"upstream_binding_id": binding.pk, "pending": pending},
    )
    return f"{OPENAPI_OUTBOX_NOT_SUPPORTED_REASON}（这一行现在有 {pending} 条）"


class CallbackKeysError(RuntimeError):
    """写不进 / 读不到「码 → 密钥」文件（父目录不在、权限、IO 错）。 ⚠️ 消息里**只有路径与异常类型**，⛔ 没有密钥值（照 `apps/callback/config.py` 那套哨兵口径）。
    ⚠️ 这是**未预期**的 I/O 失败 ⇒ **响亮地**冒出来（调用方 `jidanpu_upstream_views.put` 由此回滚
    整个保存），⛔ 不吞成「保存成功」——那会留下「库里说配好了、文件里没有」的半态，那种半态下回调全部 401/503 而页面与库里一切正常（本线最防的那类静默）。"""


def callback_path_of_code(code: str) -> str:
    """码 ⇒ 接收器**认**的那条路径；形状不合 ⇒ 空串。 ⚠️ 判据**借接收器自己的解析函数**（`apps.callback.wsgi.callback_code_of_path`）：这条路径
    的形状（前缀 + 定长 22 + 字母表）在 nginx 的 location 正则、接收器的路径解析两处已经钉死 ⇒ 在这里再写一遍正则就是第三个真相源。⇒ 判据从构造上就是「**接收器会不会认这条地址**」，
    而不是「我们觉得它像不像」。"""
    path = f"{callback_wsgi.CALLBACK_PATH_PREFIX}{code or ''}"
    return path if callback_wsgi.callback_code_of_path(path) == code else ""


def tenant_primary_domain() -> str:
    """**当前租户**的主域名（取不到 ⇒ 空串）。平台平面（`public`）回空串（那里的「租户行」是平台自己）。 ## ⚠️ 与 spec §5 / §13.3-C10 字面写法**有出入**，以本函数为准（已登记为偏离）。稿子写「取 `PlatformTenant.primary_domain`」，但一租户一库下那条路取不到值：`PlatformTenant`
    在租户库里只有一张同名**空表**（留壳保迁移历史，⛔ 不在租户侧读写；平台侧已迁去独立项目） ⇒ 照字面实现会永远回空串、页面静默不给地址。实际权威来源是 **`platform.Domain`** （django-tenants Host 路由行，`is_primary=True` 即主域名）——它还保证拼出的域名**就是这台机器
    真的会路由的域名**（地址要给用户复制到鸡蛋铺那侧，拼一个路由不到的域名比不给更糟）。 ⚠️ 多域名时**顺序确定**：主域名优先、其次按域名字典序（⛔ 不许「随便挑一个」—— 那会让同一绑定每次保存显示不同地址）。"""
    from django.db import connection
    from django_tenants.utils import get_public_schema_name, get_tenant_domain_model

    tenant = getattr(connection, "tenant", None)
    if tenant is None or not getattr(tenant, "pk", None):
        return ""
    if getattr(connection, "schema_name", "") == get_public_schema_name():
        return ""
    domains = get_tenant_domain_model().objects.filter(tenant=tenant)
    # `-is_primary`：PostgreSQL 上布尔降序把 True 排在前面（主域名优先）。
    return (
        domains.order_by("-is_primary", "domain").values_list("domain", flat=True).first() or ""
    )


@dataclass(frozen=True)
class CallbackAddress:
    """回调地址的结果：`url` 与（算不出来时的）人话原因 —— ⚠️ **恰好一个非空**。"""

    url: str
    reason: str


def _external_callback_host() -> str:
    return settings.JIDANPU_CALLBACK_HOSTS.get(connection.schema_name, "")


def callback_address(binding) -> CallbackAddress:
    """拼出一行绑定的回调地址（spec §5.1 的形状：`https://<租户主域名>/api/jidanpu/callback/<22 位码>`）。三条「为什么这么定」照 spec §5.1 **写进代码**（⛔ 别只留在文档里）：⭐ 用**随机码**、不用租户名
    （租户名路径可枚举，能被试探「这个域名属于哪家、在不在跑」）；⛔ 不用两级子域名（不在通配符证书覆盖范围内且要多配 DNS）⇒ 地址永远挂在**该租户自己的域名**上（来源见 `tenant_primary_domain`）； ⭐ 地址由我们生成、用户不用自己拼（spec §11.3）。
    ⛔ 算不出来 ⇒ **回空串 + 原因**（spec §13.3-C10）：绝不回落 `example.com`、⛔ 不拼半截地址（会被用户复制进鸡蛋铺那侧然后静默收不到回调）；⛔ 地址里**不含** client_id / client_secret / 租户名 / schema 名（spec §13.3-C11）——本函数用到的值里**结构上**就没有这四样，⛔ 不是靠一句「记得别拼进去」。"""
    if not _is_openapi(binding):
        return CallbackAddress(url="", reason=CALLBACK_URL_REASON_NOT_OPENAPI)
    code = getattr(binding, "callback_code", "") or ""
    if not code:
        return CallbackAddress(url="", reason=CALLBACK_URL_REASON_NO_CODE)
    path = callback_path_of_code(code)
    if not path:
        return CallbackAddress(url="", reason=CALLBACK_URL_REASON_BAD_CODE)
    domain = _external_callback_host() or tenant_primary_domain()
    if not domain:
        return CallbackAddress(url="", reason=CALLBACK_URL_REASON_NO_DOMAIN)
    return CallbackAddress(url=f"{CALLBACK_URL_SCHEME}://{domain}{path}", reason="")


def callback_url(binding) -> str:
    """只要那一串地址（页面 / 序列化器用）；算不出来 ⇒ **空串**（原因问 `callback_address`）。"""
    return callback_address(binding).url


def callback_key_of(binding) -> tuple[str, str] | None:
    """这一行此刻该写进密钥文件的 `(码, 密钥)`；**不该写** ⇒ `None`（判据唯一出处，⛔ 别在调用方再判一遍）。三条判据：① `mode == "openapi"`（⛔ 不是 `client_id` 真值判据）；② 有**非空**回调码
    （码由模型层生成；shell / 迁移直写库的行可能没有）；③ `client_secret` **非空**——⛔ **绝不写空值** （空值把「这个码用哪把密钥」从可用变成必然验签失败，读侧对空密钥回 401，而页面与库里一切正常）。
    两者都顺手 `strip()`（复制粘贴的首尾空白很常见）。⚠️ **不看 `enabled`**：地址要先给用户看（页面与开关无关，用户常「先配好再启用」）；这份映射说的是「哪个码用哪把密钥」，与同步开关无关。"""
    if not _is_openapi(binding):
        return None
    code = (getattr(binding, "callback_code", "") or "").strip()
    secret = (getattr(binding, "client_secret", "") or "").strip()
    if not code or not secret:
        return None
    return code, secret


@dataclass(frozen=True)
class CallbackKeyWrite:
    """一次 `ensure_callback_key` 的结果。⚠️ 四个字段**都不含密钥值**（`repr` 因此是安全的）。
    - `created`：文件此前**不存在或没有可用内容** ⇒ 这一次把它建出来了（**带首行注释**）； - `changed`：那一**行**内容真的变了（新增了这一行 / 换了密钥）；`False` = 文件里本来就有
    一模一样的一行，这一次只是**幂等重写**（重写的理由见 `ensure_callback_key`）。"""

    path: Path
    code: str
    created: bool
    changed: bool


def _split_keys_lines(text: str) -> list[str]:
    """按 `\n` 切并丢掉尾部空串（⛔ **不用** `str.splitlines()`）。 ⚠️ 与读侧 `apps/callback/config.py::parse_callback_keys` **同一套切法**：`splitlines()` 还会在 `\\x0b` / `\\u2028` 这些字符上切——密钥里若有这类字符，两处按不同的方式切行就会得出不同的映射（写进去的和读出来的不是一个东西）。"""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _plan_keys_write(existing: str, code: str, secret: str) -> tuple[str, bool, bool]:
    """算出新文本 ⇒ `(文本, changed, created)`。**只增不减**：除那一个码那一行，其余逐字保留。 ⚠️ 判「这一行是那个码」的口径**与读侧逐字一致**（`#` 注释与空行跳过、必须含 TAB、取行首字段全等比对、行尾 `\r` 容掉）——口径不一致的后果是「我们更新了 A 行、读侧认的是 B 行」。 ⚠️ 末行缺换行符时追加新行**必须先补 `\n`**：否则新行粘在旧行尾巴上，把那行的密钥拼成一个谁也用不了的字符串（静默）。"""
    line = f"{code}{KEYS_FILE_SEPARATOR}{secret}"
    if not existing.strip():
        # 文件不存在 / 空文件：建出来（**带首行注释**，spec §5.3 的形状）。
        return f"{CALLBACK_KEYS_FILE_HEADER}\n{line}\n", True, True
    lines = _split_keys_lines(existing)
    for index, raw in enumerate(lines):
        text = raw[:-1] if raw.endswith("\r") else raw
        if not text or text.lstrip().startswith("#") or KEYS_FILE_SEPARATOR not in text:
            continue
        head, _, tail = text.partition(KEYS_FILE_SEPARATOR)
        if head != code:
            continue
        if tail == secret:
            return existing, False, False  # 已有同样的一行 ⇒ 内容一字不变
        lines[index] = line  # 只换这一行
        return "\n".join(lines) + "\n", True, False
    return "\n".join(lines + [line]) + "\n", True, False  # ⛔ 不动别的行，只在末尾追加


def _write_all(fd: int, data: bytes) -> None:
    """把 `data` **全部**写进 `fd`（`os.write` 允许短写 ⇒ 循环写完）。
    ⚠️ 这是**写盘字节的唯一出口**：`tests/test_jidanpu_callback_keys.py` 在它上面打桩
    （写一半就抛 `OSError`）来验证「写失败时原文件一字未变」（spec §13.1-A9 的注入点）。"""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - 正常内核不会这么返回
            raise OSError("os.write 一个字节都没写进去")
        view = view[written:]


def _unlink_quietly(path: Path) -> None:
    """删临时文件（失败就算了：丢的是**没写完的**临时文件，⛔ 不是目标文件）。"""
    try:
        os.unlink(path)
    except OSError:  # pragma: no cover - 临时文件已经不在了 / 权限
        pass


def _write_fresh_0600(path: Path, text: str) -> None:
    """**新建**一个文件写满再关（`O_EXCL` ⇒ ⛔ 绝不覆盖已有文件；`fchmod` 的理由见 `_atomic_write`）。
    ⚠️ `os.close` 放在 `finally` 里：写盘中途抛异常时描述符也必须关掉，否则临时文件既删不掉
    （Windows 语义）又漏 fd。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件（0600 + 显式 `os.fchmod`）+ `os.replace` 原子替换（spec §5.3 第 2/3 条）。三条各自承重：① 临时文件必须**同目录**（`os.replace` 只在同一文件系统内原子，放 `/tmp` 会退化成
    「复制 + 删」）；② ⛔ **绝不截断重写**（`open(path, "w")` 那一瞬间文件是空的 ⇒ 全部回调 503； `os.replace` 之下读侧只会读到旧文件或新文件，两份都完整）；③ **0600 给两遍**（`os.open` 的 mode
    受 umask 影响、`fchmod` 不受——这个文件里放的是密钥，权限不能靠 umask 凑）。 ⚠️ **父目录不新建**（`shared/` 由发版建）⇒ 目录不在就响亮报错（⛔ `mkdir` 会在错的地方悄悄造目录）； ⚠️ **任何失败都要清临时文件**（`except BaseException` 兜底）——留在 `shared/` 里的会是一份 **写了一半的密钥**（0600 也还是密钥）。"""
    directory = path.parent
    if not directory.is_dir():
        raise CallbackKeysError(
            f"回调密钥文件的父目录不存在：{directory}"
            "（发版会建 shared/，本函数不新建目录）"
        )
    tmp = directory / f"{_TMP_PREFIX}{os.getpid()}-{secrets.token_hex(8)}"
    try:
        _write_fresh_0600(tmp, text)
        os.replace(tmp, path)
    except OSError as exc:
        _unlink_quietly(tmp)
        # ⚠️ 文案里**只有路径与异常类型名**（⛔ 没有密钥、也⛔ 没有文件内容）。
        raise CallbackKeysError(
            f"写不进回调密钥文件 {path}（{exc.__class__.__name__}）"
        ) from exc
    except BaseException:  # pragma: no cover - 控制流不在这里，兜的是"任何别的失败"
        _unlink_quietly(tmp)
        raise


def ensure_callback_key(binding) -> CallbackKeyWrite | None:
    """把这一行的 `(码, 密钥)` 写进 `$R/shared/callback-keys.env`（spec §5.3 / §13.1-A9）。什么时候写 / 不写（判据唯一出处 = `callback_key_of`）：①式绑定 ⛔ 不写（没有回调地址与 `client_secret` 语义）；没有回调码 ⛔ 不写；`client_secret` 为空 ⛔ **不写**（空值 = 必然验签失败
    而页面一切正常）；`enabled=False` ✅ **照写**（地址与开关无关）；解绑 / 换方式 / 重新生成码 ⛔ **一行都不删**（**只增不减**：删行会断掉「别的绑定还在用」的情况）。两条实现纪律：**原子** （`_atomic_write`）+ **只增不减**（`_plan_keys_write`，其余行含注释逐字保留）。
    ⚠️ 返回 `None` = 不该写；返回 `CallbackKeyWrite` = 写了（⛔ 返回值里没有密钥）。该写时**每次都重写一遍**（哪怕一个字节没变）：行为只有一条路径，且顺手把权限修回 0600；代价 = 人手低频动作多一次几行的写盘。⚠️ **残差（如实登记，未加锁）**：读 → 改 → 替换之间没有文件锁 ⇒ 同一瞬间两人各存一次理论上会丢一行密钥；本机单文件、绑定与人手级别低频 ⇒ 先如实挂着（⛔ 不假装已解决；真要加锁时同一次改这里与 `docs/RUNBOOK.md` 的文件面）。"""
    if _external_callback_host():
        return None
    pair = callback_key_of(binding)
    if pair is None:
        return None
    code, secret = pair
    path = callback_keys_path()
    try:
        existing = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise CallbackKeysError(
            f"读不到回调密钥文件 {path}（{exc.__class__.__name__}）"
        ) from exc
    text, changed, created = _plan_keys_write(existing, code, secret)
    _atomic_write(path, text)
    return CallbackKeyWrite(path=path, code=code, created=created, changed=changed)


# ═══════════════════════════════════════════════════════════════════════════════
# T7：官方 API 状态 → 团团通订单状态（映射表 + 未识别口径；spec §6 全节）。本段**只出判据、不写任何东西**（消费者是 T6）：进几个字符串、出一个 `StatusPlan`（纯数据）。三条硬口径：① ⭐ 回调里的 `status` **不决定**写成什么（spec §6.1-B4：只决定「要不要去查」）
# ⇒ `plan_openapi_status` 的入参只有权威值（回调会漏 / 会重复（F20 实测 0.5%）、其 `timestamp` 偏 +8 小时（F19），都⛔ 不参与判断）；② ⭐ 「该不该写」判据只有一处 = `jidanpu_upstream.should_write_status`（spec §6.3 明令复用，本模块没有第二套同义判断）； ③ ⭐ 认不出来的值（spec §6.4）：⛔ 不静默丢、不猜、不映射成问题单 ⇒ `mapped=None` + `write=False` + `unknown=True` + 一条必须登记的台账 kind。

#: 官方 API 实测下发的状态字符串 → 团团通 `OrderStatus` 码（spec §6.2，**逐行出处写在行上**）。 ⚠️ 键是**实测的中文字符串**（⛔ 不照文档写死：F17 的 `未分配` 不在文档枚举里）；`None` 是
#: **有意义的取值**（≠「键不在」：None = 明确不映射，键不在 = 认不出来，出口不同）。 ⚠️ ⛔ **不复用也不改** `jidanpu_member.REMOTE_STATUS_MAP` 那一族（服务登录式的 13 个**码**，
#: 且把认不出来的值兜底成 `ISSUE`——本线不沿用那条兜底，§6.4-④）；也⛔ 不并入 `jidanpu_upstream` 那张收窄版（那是登录式那一格的口径）。每一行都有出处；⛔ 认不出来的不许猜。
OPENAPI_STATUS_MAP: dict[str, str | None] = {
    # 实测：spec §3.4-F17 —— 新建的单就是它，而**文档的状态枚举里没有这个值**。
    # ⚠️ 写不写**不在这里定**：spec §6.2 这一格写的是「⛔ 不写（**走 §6.3**）」⇒
    #    交给下面那一个判据判（它在我方「代练中」时会给 `True`，见用例里钉住的那一格矩阵）。
    "未分配": OurOrderStatus.UNASSIGNED,
    # 实测：spec §3.4-F16 —— 端到端把状态改成「待上号」成功、回查已变、回调验签通过。
    "待上号": OurOrderStatus.IN_PROGRESS,
    # 读码：`jidanpu_member.REMOTE_STATUS_LABELS["代练中"]`（登录式那一格的口径）。
    "代练中": OurOrderStatus.IN_PROGRESS,
    # 读码：`jidanpu_upstream.REMOTE_STATUS_LABELS["待验收"] = None`（`PENDING_REVIEW_REMOTE_TOKENS`）
    # —— 它是我方**自己提交完成之后的回音**：写回去会把已完成单改成待验收，
    #     而 `apply_transition` 离开 `COMPLETED` 会**静默清空** `completed_at`（spec §6.2 那一行）。
    "待验收": None,
    # 读码：`jidanpu_member.REMOTE_STATUS_LABELS["已暂停"]`。
    "已暂停": OurOrderStatus.PAUSED,
    # 读码：`jidanpu_member.REMOTE_STATUS_LABELS["问题单"]`。
    "问题单": OurOrderStatus.ISSUE,
    # 读码：`jidanpu_member.REMOTE_STATUS_LABELS["已完成"]`。 ⚠️ 能不能写还要过状态机的**边存在性**：入 `COMPLETED` 只剩「待验收 →」这一条
    # （`order_status._COMPLETED_SOURCE`）⇒ 我方还在「代练中」时对方报已完成 ⇒ 不写 + 人话台账
    # （由下面那**同一个**判据的 ③ 给出，spec §6.2 那一格的 ⚠️）。
    "已完成": OurOrderStatus.COMPLETED,
    # 读码：`jidanpu_member.REMOTE_STATUS_LABELS["已退单"]` ⇒ `REFUNDED`；⚠️ 但本线第一期
    # **不直接写**：它要走**跟退**那条路（钱一起回退，spec §6.2 / §15-N4）⇒ 见 `OPENAPI_REFUND_TOKENS`。
    "已退单": OurOrderStatus.REFUNDED,
    # 读码：`jidanpu_member` 的既有注释 —— 「待抢单」是**显示层**对 `orderSta` 的覆盖，
    # 数据里没有这个值（spec §6.2 那一格；列在这里只为「万一对方真下发」时不把它当成认不出来的值）。
    "待抢单": None,
}
#: ⚠️ **`已撤销` / `撤销中` / `仲裁中` / `已仲裁` / `异常中` 刻意不在上表里**（spec §6.2 ⚠️ 段）：
#: 登录式底表把这几个映射成 `ISSUE`，但官方 API **从没下发过**这些值（Q4 枚举未与对方核清）
#: ⇒ 照抄会让「我们以为它下发过」变成事实。口径 = 不在上表里的一律走 §6.4（登记 + 人工核对）。

#: 「跟退」那一档（spec §6.2「已退单」行 + §15-N4）：**不直接写**——这一档走**跟退**那条路（钱一起回退）。⚠️ 这是**路由**（这一档走哪条路），⛔ **不是**第二个「该不该写」判据——
#: 既有登录式那条线也是这么切的（`jidanpu_upstream._pull_row` 先路由到 `follow_refund`，再才轮到 `should_write_status`）。⭐ 消费者把这一档路由到既有跟退原语（D3：
#: `jidanpu_openapi_consume._follow_refund` 调 `follow_refund`）；本函数仍然只出**路由**。
OPENAPI_REFUND_TOKENS = frozenset({"已退单"})

#: 这一档**走哪条路**（`StatusPlan.route` 的两个取值）。
ROUTE_STATUS = "status"
ROUTE_REFUND = "refund"

#: ⭐ 认不出来的状态**必须**登记的那条台账的 kind（spec §6.4-②：用**新**的 kind）。
#: ⚠️ ⛔ 别把它塞进累计计数（`callback_count` 那类数字看不出「出事了」，spec §6.4-③）。
OPENAPI_UNMAPPED_STATUS_KIND = "openapi.unmapped_status"

#: 权威值缺失（`status` 为空）时的人话（照 `jidanpu_upstream.REMOTE_STATUS_MISSING_REASON`
#: 的既有口径：⛔ 判不了就什么都不写，⛔ 不猜一个状态）。
OPENAPI_STATUS_MISSING_REASON = (
    "鸡蛋铺官方API 这一次没有给出可读的状态（status 为空）⇒ 这一轮什么都不写"
)
#: 认不出来的值（spec §6.4-② 的人话；`{token}` 是**远端原值逐字**）。
OPENAPI_UNMAPPED_STATUS_REASON = (
    "鸡蛋铺官方API 返回了一个本线认不出来的状态「{token}」⇒ ⛔ 一个字节都不写订单，"
    "登记一条待人工核对（⛔ 不猜、⛔ 不映射成问题单、⛔ 不静默丢）"
)
#: 表里**明确写着不映射**的那两档（`None`：待验收 / 待抢单）的人话。 ⚠️ 不再细分「预期回音 / 异常」那一刀：那要读**链路的提交记录**（`link.completed_submitted_at`），
#: 是消费者（T6）手上才有的输入 ⇒ ⛔ 不在这里编一个（登录式那条线的 `_unmapped_reason` 分得开，
#: 是因为它手上**有**链路）。
OPENAPI_NOT_MAPPED_REASON = "鸡蛋铺官方API 的这一档（{token}）在本线明确不映射 ⇒ 只登记，不改我方状态"
#: 跟退那一档的人话（⭐ D3：这一档不走通用写入格、要走跟退那条路——消费者把这一档
#: **真的**路由到跟退（`_follow_refund`），这句人话只描述**裁决本身**）。
OPENAPI_REFUND_ROUTE_REASON = (
    "鸡蛋铺官方API 说这一单「{token}」⇒ 它要走**跟退**那条路（钱一起回退），"
    "⛔ 不走本线通用的状态写入格（跟退由消费者调既有原语 `follow_refund` 办；"
    "被已结账等闸拦住的那几档由它登记 + 人工核对）"
)


@dataclass(frozen=True)
class StatusPlan:
    """一次「远端状态 → 我方该怎么办」的**纯数据**结论（spec §6）。 ⚠️ **不写任何东西**（消费者是 T6）；`write` 的**唯一出处**是 `jidanpu_upstream.should_write_status` （spec §6.3）——本类只把那个布尔值搬出来，⛔ 没有第二套判据（唯一例外 `route=refund` 的**路由**，
    显式枚举、用例逐条钉住）。字段：`remote_status` 权威值逐字原值（认不出来也原样带着，台账要它）； `mapped` 我方码（`None` = 不写，含「明确不映射」与「认不出来」）；`route` = `ROUTE_STATUS` /
    `ROUTE_REFUND`（后者由消费者路由到跟退原语，⛔ 不进通用写入格）；`write` 该不该写（唯一出处）； `reason` 人话（`write=True` 时是空串）；`unknown` 认不出来（⇒ 必须按 `kind` 登记）；`kind` 必须用的台账 kind（`None` = 调用方默认）。"""

    remote_status: str
    mapped: str | None
    route: str
    write: bool
    reason: str
    unknown: bool
    kind: str | None = None


def _status_token(value) -> str:
    """状态格的归一化（`None` ⇒ 空串；其余 `strip`）。判据一律用空串 / `is None`，⛔ 不用真值。"""
    return "" if value is None else str(value).strip()


def _unmapped_reason(token: str) -> str:
    """`mapped is None` 那一档的**细分人话**（判据**不在这里** —— 它仍是 `should_write_status` 的 ①）。
    ⚠️ 只换措辞、不参与裁决：本函数拿不到 `order`，也⛔ 不该在这里重判一次「该不该写」。"""
    if token in OPENAPI_STATUS_MAP:
        return OPENAPI_NOT_MAPPED_REASON.format(token=token)
    return OPENAPI_UNMAPPED_STATUS_REASON.format(token=token)


def plan_openapi_status(order, *, remote_status) -> StatusPlan:
    """官方 API 的**权威**状态 → 「写成什么 / 该不该写 / 认不出来怎么办」（spec §6；本线 T7）。入参只有两个（⛔ 都没有第三个）：`order` 只读 `.status` 一格（照 `should_write_status` 既有签名，用例传 `SimpleNamespace` 就够，本函数不碰库）；`remote_status` = **权威值**。⛔ **回调里的
    `status` 不是入参**（spec §6.1-B4 承重顺序：回调只决定「要不要去查」）；⛔ 也没有**时间**入参（回调 `timestamp` 偏 +8 小时，F19）。三条出路（顺序固定）：① 认不出来（不在 `OPENAPI_STATUS_MAP`）⇒ `mapped=None`、`write=False`、
    `unknown=True`、`kind=OPENAPI_UNMAPPED_STATUS_KIND` ⇒ 调用方必须登记待人工核对； ② 跟退档（`OPENAPI_REFUND_TOKENS`）⇒ `route=ROUTE_REFUND`、`write=False`（⛔ 不进通用写入格）； ③ 其余 ⇒ `mapped` 照表，`write` 交 `should_write_status` 判（唯一判据）。"""
    token = _status_token(remote_status)
    if not token:
        #: ⛔ 判不了就什么都不写（照登录式那条线的既有口径），也⛔ 不当成「认不出来的值」
        #: —— 「没有值」与「有个怪值」是两件事，台账措辞与后续动作都不一样。
        return StatusPlan(
            remote_status=token, mapped=None, route=ROUTE_STATUS, write=False,
            reason=OPENAPI_STATUS_MISSING_REASON, unknown=False,
        )
    if token in OPENAPI_REFUND_TOKENS:
        return StatusPlan(
            remote_status=token, mapped=OPENAPI_STATUS_MAP.get(token), route=ROUTE_REFUND,
            write=False, reason=OPENAPI_REFUND_ROUTE_REASON.format(token=token), unknown=False,
        )
    mapped = OPENAPI_STATUS_MAP.get(token)
    unknown = token not in OPENAPI_STATUS_MAP
    write, reason = jidanpu_upstream.should_write_status(order, mapped=mapped)
    if mapped is None:
        #: 判据已经给出「不写」（它的 ①：`mapped is None`）⇒ 这里**只**把措辞换成更具体的一句
        #: （认不出来 / 本线明确不映射）。
        reason = _unmapped_reason(token)
    return StatusPlan(
        remote_status=token, mapped=mapped, route=ROUTE_STATUS, write=write, reason=reason,
        unknown=unknown, kind=OPENAPI_UNMAPPED_STATUS_KIND if unknown else None,
    )


def unmapped_status_payload(*, remote_status, order_no, binding_id, queried_at) -> dict:
    """「认不出来的状态」那条台账的 `payload`（spec §6.4-② 要求它带上这四件事实）。 ⛔ **不放凭据**（照 `jidanpu_upstream._note_order` 的既有纪律）：远端原值、我方订单号、
    绑定 id、查单时刻 —— 四件都是**定位用的事实**，没有一件是凭据。 ⚠️ `queried_at` 是**我们自己**查单的时刻（⛔ 不是回调里那个偏 +8 小时的 `timestamp`，F19）。"""
    return {
        "remote_status": _status_token(remote_status),
        "order_no": order_no,
        "upstream_binding_id": binding_id,
        "queried_at": queried_at.isoformat() if hasattr(queried_at, "isoformat") else queried_at,
    }


# ══════════════════════════════════════════════════════════════════════════════
# T11：②式的「测试连接」= **逐条**只读探测（spec §9.2 / §11.4 / §20.2）。全部职责：进一行②式绑定，出一张逐条表（可用 / 不可用 + 原因 + 该做什么）。 **不打写接口**、**不落库**、**不写任何配置**。三条硬口径：
# ① ⭐ 只探测只读端点（§20.2-1 + §11.4）：⛔ 绝不许为了探测打写接口—— 页面上点一次「测试连接」就打一次写接口 = 给用户一个「点一下就可能改线上单」的按钮； ② ⭐ 两层权限分得开（spec §20.1）：外层 = 官方发放 ⇒ 只能找鸡蛋铺；内层 = API 子账户在他自己那套系统里的权限 ⇒ 用户可自助。两档指引是两句不同的话（`USER_ACTION_BY_KIND`）； ③ ⭐ 省着打（对方有限流，spec §8.3）：见 `PROBE_ORDER` 的顺序理由。

#: 探测的顺序（**有序**）：1. `token` 排第一且**失败即停**（唯一的一处）——取 token 是前置，拿不到时四个端点会以同一理由各失败一次，连打 4 发白给；2. `GetGames` 在 `GetProject` 之前
#: （后者的 `gameId` 从前者第一行 `id` 取，实测不带会 404）⇒ 省一发；3. 两个带单号的探测排最后（用不存在的单号探「接口能不能用」，见 `probe_path` 的 `order_id_probe`）。
#: ⚠️ token 通了之后是「继续探」：四项权限互相独立 ⇒ 一次点「测试连接」要把缺哪几项一次说清（否则修一项点一次）。上限 = 1 次 token + 4 次只读 = 5 次。
PROBE_ORDER: tuple[str, ...] = (
    STEP_TOKEN, STEP_GAMES, STEP_PROJECT, STEP_ORDER_STATUS, STEP_ORDER_PROCESS_IMGS,
)

#: ⭐ **那把闸**（「能不能让人启用」）**只看这两跳**（spec §25.2 裁定，该节优先于 §11.4 末段的字面读法）：`token` **且** `GetOrderStatus`——缺任何一跳，订单状态同步就是不可能的 ⇒ 挡启用；
#: ⛔ 另外三项**不挡启用**（信息性）：`GetGames` / `GetProject` 是录单那条路要用的，进度图是展示。 §25.1 的张力：§11.4 末段「任何一项不过都不许启用」与 §20.2 / §9.1-B「内层权限不影响状态同步」
#: 字面冲突 ⇒ 字面照做会让「只缺 `GetGames` 权限、状态同步跑得起来」的人连启用都不行。 ⚠️ 五项照样全探、照样进 `checks` 表 ⇒ 用户仍看得到「缺哪项、去哪儿开」，只是不缺的那几项不挡启用。
ENABLE_GATE_STEPS: tuple[str, ...] = (STEP_TOKEN, STEP_ORDER_STATUS)

#: 探单号：带单号的那两项用它去探。⛔ **刻意用一个不可能属于真单的值**
#: （`"0"`：对方那边的单号是它自己的长数字串）⇒ 我们**绝不**拿用户的真单去点「测试连接」，
#: 也就⛔ 不会因为点这一下而读到任何一张真单的内容（它是页面上的按钮，不是数据入口）。
PROBE_ORDER_ID = "0"

#: 未探测那一档的原因（token 失败之后那四行用它）：说清**为什么没探**，⛔ 不假装探过了。
PROBE_NOT_PROBED_REASON = (
    "未探测：「{step}」这一步没通过，后面的探测不再打（对方有限流，⛔ 不白打四发）"
)

#: `GetProject` 没探的那一档：上一步没给出可用的 `gameId` ⇒ **不发这一发**。
PROBE_NO_GAME_ID_REASON = (
    "未探测：上一步（取游戏列表）没给出可用的 gameId，"
    "而实测不带 gameId 会 404 ⇒ 这一发不打（打了也只是白打）"
)

#: 带探单号的那两项「可达」时给的原因（把**对方原话**带上：它说的正是「这单不存在」一类）。
PROBE_REACHABLE_REASON = "接口可达（我们探的是一个不存在的单号，对方回：{peer}）"


def _dedup(texts) -> tuple[str, ...]:
    """去掉空串与重复项、**保持先后顺序**（表里那一段指引只给一次，⛔ 不在四行里各贴一遍）。"""
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class EndpointProbe:
    """逐条表里的**一行**（⛔ 不含任何凭据：`error` 里的文本已由 `OpenApiError` 脱敏）。字段：`step` 哪一跳（取值同 `STEP_*`）；`probed` **真的打了这一发没有**（`False` = 前置没通过，
    由 `reason` 说清）；`ok` 可用吗（⚠️ 与 `probed` 不是一回事：没探过的行 `ok=False` 但原因不是「不可用」）；`kind` 不可用档位（可用或未探测 ⇒ 空串）；`reason` 人话原因（对方原话优先）；
    `message` 页面显示段（只有「探到了且不可用」才有；⚠️ **闸那一行**的权限问题换一句真话 `GATE_BLOCKED_PERMISSION_TEXT`）；`action` 该做什么（直接来自 `OpenApiError.user_action`）。"""

    step: str
    probed: bool
    ok: bool
    error: OpenApiError | None = None
    note: str = ""
    skip_reason: str = ""
    inherit_action: str = ""

    @property
    def label(self) -> str:
        return step_label(self.step)

    @property
    def kind(self) -> str:
        if self.ok or self.error is None:
            return ""
        return str(self.error.kind)

    @property
    def reason(self) -> str:
        if not self.probed:
            return self.skip_reason
        if self.ok:
            return PROBE_REACHABLE_REASON.format(peer=self.note) if self.note else ""
        exc = self.error
        #: 对方原话优先（spec §9.3 纪律 1）；没有原话才回落到我们自己的那一档说明。
        return exc.upstream_msg or exc.detail or exc.transport or KIND_TEXT[exc.kind]

    @property
    def message(self) -> str:
        """页面上要显示的那一段（**只有「探到了且不可用」才有**）。 ⭐ **闸那一行的权限问题换一句真话**：判别维度是「是不是闸那一行」，⛔ 不是「哪个界面」——
        §11.5 那句文案的末句「不影响订单状态同步」对闸那一行在任何地方都是**假的** ⇒ 表里那一行与 PUT 那段话**共用** `GATE_BLOCKED_PERMISSION_TEXT`；非闸行照旧走
        `error.user_text`（那里「不影响」是真的）；`token` 被挡走 ①/③ ⇒ 也照旧。"""
        if self.probed and not self.ok and self.error is not None:
            if self.step in ENABLE_GATE_STEPS and self.error.kind is FailureKind.PERMISSION:
                return GATE_BLOCKED_PERMISSION_TEXT.format(items=self.label)
            return self.error.user_text
        return ""

    @property
    def action(self) -> str:
        if not self.probed:
            #: 未探测的那几行**继承**把它挡住的那一跳的指引：用户在**同一行**上就看到该做什么，
            #: ⛔ 不必自己去别处找（而那段长文案只由 `ProbeReport.messages` 给一次）。
            return self.inherit_action
        if self.ok or self.error is None:
            return ""
        return self.error.user_action

    def as_dict(self) -> dict:
        """给页面 / 前端渲染的形状（**字段名就是契约**，T10 照它渲染）。"""
        return {
            "step": self.step,
            "label": self.label,
            "probed": self.probed,
            "ok": self.ok,
            "kind": self.kind,
            "reason": self.reason,
            "message": self.message,
            "action": self.action,
        }


@dataclass(frozen=True)
class ProbeReport:
    """②式探测的**全部结论**：一张逐条表 + 去重后的「话」与「该做什么」。 ⚠️ 三条纪律：`ok` = **每一行都探到了且都可用**（信息口径，页面照它渲染 + 说清缺哪项去哪儿开）；
    `gate_ok` = 「能不能让人启用」那把闸（spec §25.2 只看 `ENABLE_GATE_STEPS` 两跳）——⚠️ 与 `ok` **故意不是一回事**：只缺 `GetGames` 权限时 `ok=False` 而 `gate_ok=True`（⛔ 不该挡着人启用）；
    `messages` / `actions` **去重**（同一句指引不贴四遍），顺序 = `PROBE_ORDER`； `as_data()` 是**页面拿到的那个 dict**（⛔ 视图不再拼一遍：形状只允许一处出处）。"""

    checks: tuple[EndpointProbe, ...]

    @property
    def ok(self) -> bool:
        return all(row.probed and row.ok for row in self.checks)

    @property
    def gate_ok(self) -> bool:
        """⭐ **「能不能让人启用」那把闸**（spec §25.2）：只看 `ENABLE_GATE_STEPS` 那两跳。 ⚠️ **与 `ok` 不是一回事**（合并 = §25.1 那个张力）：`ok` 问「五项全绿吗」（信息口径）；
        这里问「**状态同步跑得起来吗**」⇒「只缺 `GetGames` 权限」时两条故意不同。⚠️ 逐项按 `ENABLE_GATE_STEPS` 取行、**取不到就当没过**（fail-closed：闸依赖的行不在表里不许静默放行）。"""
        by_step = {row.step: row for row in self.checks}
        return all(
            (row := by_step.get(step)) is not None and row.probed and row.ok
            for step in ENABLE_GATE_STEPS
        )

    @property
    def messages(self) -> tuple[str, ...]:
        """要显示的那几段话（**不可用的那几档**的 `user_text`；全绿 ⇒ 空元组）。"""
        return _dedup(row.message for row in self.checks)

    @property
    def actions(self) -> tuple[str, ...]:
        """「接下来做什么」（去重；含未探测那几行继承来的那一句）。"""
        return _dedup(row.action for row in self.checks)

    @property
    def advice(self) -> tuple[str, ...]:
        """「**发生了什么** + **该做什么**」合成一段（**PUT 的启用被拒**时当 `msg`）。 ⚠️ 顺序固定：先结论（`messages`）再该做什么（`actions`）——倒过来读者会先看到「去做 X」
        而不知道为什么；两段都去重过。结论段**不是另写一份**：就是各行的 `message` ⇒ 「闸那一行」那句真话在表里与这里是**同一份**（判别维度是「是不是闸那一行」，⛔ 不是「哪个界面」）。"""
        return _dedup(tuple(self.messages) + tuple(self.actions))

    def as_data(self) -> dict:
        return {
            "ok": self.ok,
            "messages": list(self.messages),
            "actions": list(self.actions),
            "checks": [row.as_dict() for row in self.checks],
        }


def probe_read_endpoints(binding) -> ProbeReport:
    """按一行**②式**绑定逐条探测只读端点（spec §9.2 / §11.4 / §20.2）。 ⚠️ 凭据只从**这一行**取（走 `openapi_client`，租户边界与 T2 同一处）；
    ⚠️ **不落库**：本函数只发 GET 与 `/oauth/token`（POST，但那是**取 token**、不改对方任何数据）。
    ⚠️ 只读端点那一发失败**不抛**：三档都收进表里（那是结论，不是异常）；非 `OpenApiError` 的错误（我们自己的 bug）照旧**响亮上行**（E-0010）。"""
    with openapi_client(binding) as client:
        return _probe_all(client)


def _probe_all(client) -> ProbeReport:
    """`PROBE_ORDER` 那一趟（顺序、失败即停、省着打的判据全在这里，⛔ 别在调用方再判一遍）。"""
    checks: list[EndpointProbe] = []
    blocker: OpenApiError | None = None
    games: tuple[dict, ...] = ()

    for step in PROBE_ORDER:
        if blocker is not None:
            checks.append(EndpointProbe(
                step=step, probed=False, ok=False,
                skip_reason=PROBE_NOT_PROBED_REASON.format(step=step_label(blocker.step)),
                #: 未探测的那几行**继承**把它挡住的那一跳的指引：用户在**同一行**上就看到该做什么，
                #: ⛔ 不必自己去别处找（那段长文案只由 `ProbeReport.messages` 给一次）。
                inherit_action=blocker.user_action,
            ))
            continue

        note = ""
        try:
            if step == STEP_TOKEN:
                client.probe_token()
            elif step == STEP_GAMES:
                #: ⚠️ **本趟唯一的例外**：其余三项只判信封层（`probe_path`），而这一项走 `fetch_games`（**连 `data` 的形状一起判**）。两条理由：
                #: ① 它的返回值我们要**用**（第一行的 `id` 就是 `GetProject` 的 `gameId`） ⇒ 形状不对 = 「这一项我们用不了」，那正是这张表要报的结论；
                #: ② 一次调用同时给出「通不通」与「`gameId` 是什么」⇒ **省一发**（对方有限流）。
                games = client.fetch_games()
            elif step == STEP_PROJECT:
                game_id = games[0].get("id") if games else None
                if game_id is None or str(game_id).strip() == "":
                    # ⛔ **不发这一发**：没有 gameId 时打了也只会得到 404（实测），
                    # 而 404 在用户眼里像「网络问题」——那种说不清的形态不要造。
                    checks.append(EndpointProbe(
                        step=step, probed=False, ok=False,
                        skip_reason=PROBE_NO_GAME_ID_REASON,
                    ))
                    continue
                client.probe_path(PROJECT_PATH, params={"gameId": game_id}, step=step)
            elif step == STEP_ORDER_STATUS:
                note = client.probe_path(
                    ORDER_STATUS_PATH, params={"orderId": PROBE_ORDER_ID}, step=step,
                    order_id_probe=True,
                )
            else:  # STEP_ORDER_PROCESS_IMGS（`PROBE_ORDER` 的最后一项）
                note = client.probe_path(
                    ORDER_PROCESS_IMGS_PATH, params={"orderId": PROBE_ORDER_ID}, step=step,
                    order_id_probe=True,
                )
        except OpenApiError as exc:
            checks.append(EndpointProbe(step=step, probed=True, ok=False, error=exc))
            if step == STEP_TOKEN:
                #: ⭐ **失败即停**唯一的一处（理由见 `PROBE_ORDER` 第 1 条）。
                blocker = exc
            continue
        checks.append(EndpointProbe(step=step, probed=True, ok=True, note=note))

    return ProbeReport(checks=tuple(checks))
