"""代练通开放接口（V3.4）客户端——**只做协议层**：能可靠调通、能被单测钉住。本模块**不查库、不落库、不判业务语义**（不猜状态、不校验单号格式、不决定该不该发单）。调用方拿到的就是上游回包解析后的结构；成败只看本模块抛不抛 `DailiantongError`。 ## 协议真相的出处 `docs/DAILIANTONG-API.md`（我们自己的实测笔记，比厂商文档可信）： - **签名**（§2.1）：`Sign = MD5(各业务参数值按发送顺序拼接 + AppID + TimeStamp + Ver + AppSecret)`， 32 位小写、**不含参数名**；`Action` 的值排在最前面（实测脚本与厂商自证向量一致）。 ⚠️ 2026-10-26 实测有一半动作**根本不验签**，但本客户端**每个请求都按文档签** ——不依赖「他们不验」这个事实做任何设计（他们哪天收紧，不签的就全挂）。 - **传输**（§2）：`POST` + `application/x-www-form-urlencoded`。⛔ 凭据只在 body 里， **URL 永远不带查询串**（`base_url` 默认 HTTPS，见 R10）。
- **三种响应信封**（§3）： ① `{"Result": <int 或 str>, "Err": "..."}`——`Result` 类型不固定，**一律 `str()` 归一再比**； ② 裸对象（如 `UserMoney` 的 `{"SumBal": …}`）； ③ 列表信封 `{"<Action>": <数组 或 空字符串>, "RecordCount": N}`——空结果是**空字符串**、非空是数组；且**部分字段是二次转义的 Unicode**（实测 `LevelOrderPubLishList` 的 `Actor` 回的是字面量 `\\u5f85\\u5b9a`，同一行的 `Title` 却正常）⇒ 解析后对字符串值做一次兜底解码。 - **业务成功码**：`"1"` 是唯一成功码；其余（含 `-101`…`-106`）一律抛 `DailiantongError(code, message, action, raw)`，`message` 用上游 `Err` **原文**， ⛔ 本模块不编第二套错误文案（文档码表与实测码表会打架，§3 第 5 条）。 ## ⛔ 重试红线（真钱）
只有**只读动作**在**连接失败 / 超时**时**至多重试 1 次**（白名单 = `READ_ONLY_ACTIONS`）。写动作（尤其 `LevelOrderAdd` 发单）**永不自动重试**：实测外部单号 `ExOrderNo` 在详情 / 列表里都查不回来（§6.1 第 4 条），超时后重试 = **真钱重复建单**。 HTTP 非 2xx 也不重试——那不是「连接没建立」，而是「服务端明确回了个半截答复」。 ## ⚠️ 已知「假成功」 `LevelOrderProgressAdd`（发进度）与 `ShareOrder`（生成分享链接，本模块未封装）对**不存在的单号** 也回 `Result=1`。本客户端**照实返回成功**，由调用方**回读**确认（§3 第 3 条 / R6）。 `progress_add` 的 docstring 里点了这两条。 ## 阿里云 OSS（传图三步的第 ② 步，T12） `get_sts()` 取临时三件套（裸对象信封）；`put_object()` 按 **OSS V1 签名**把图 PUT 到 `dltfile01.oss-cn-hangzhou.aliyuncs.com`。⛔ 不引入阿里云 SDK（只用标准库 + `httpx`）。临时凭据只活在调用栈里：`OssStsCredentials.__repr__` 三个值全部打码， `OssUploadError.message` 只有 HTTP 状态 + OSS 短错误码 + 异常类型名。 ## 凭据红线 `DailiantongConfig` 只收 `MD5(支付密码)`（32 位小写），**明文永不入库**； `PayPass = MD5(MD5(支付密码) + 用户ID)` 由 `pay_pass` 现算。`app_secret` 与 `pay_password_md5` **不进 `repr()`、不进日志、不进异常文案**（`AGENTS.md` §4）。"""
from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from apps.business.platform_egress import vendor_transport

logger = logging.getLogger(__name__)

#: 默认入口。⚠️ 只走 HTTPS（文档只给 http，实测 https 同路径可用，见 R10）。
DEFAULT_BASE_URL = "https://open.dailiantong.com/API/OpenService.ashx"
#: 协议版本号；请求里叫 `Ver`，也参与签名。
API_VERSION = "1.0"
#: 唯一的业务成功码（`Result` 归一成字符串后比较）。
SUCCESS_CODE = "1"


def _is_success_code(code: str) -> bool:
    """成功码判据：`"1"` **或数值上等于 1 的写法**（⭐ P3-15：上游可能回 `1.0`）。
    ⚠️ 不能简单 `str(1.0)` 后比较（那是 `"1.0"` ≠ `"1"`）⇒ 失败信封会被误判成
    传输 / 协议失败，把「发单成功」当「未知」处理（真钱链路上最坏的一类误判）。"""
    text = (code or "").strip()
    if text == SUCCESS_CODE:
        return True
    try:
        return Decimal(text) == 1
    except (InvalidOperation, ValueError):
        return False

# ── Action 名（每个字面量只在这里出现一次）────────────────────────────────────
ACTION_ADD_ORDER = "LevelOrderAdd"
ACTION_MODIFY_ORDER = "LevelOrderModify"
ACTION_MODIFY_SPECIAL = "LevelOrderModify_Special"
ACTION_DELETE_ORDER = "LevelOrderDelSelf"
ACTION_ORDER_DETAIL = "LevelOrderDetail"
ACTION_ORDER_STATUS_LIST = "LevelOrderPubLishList_Status"
ACTION_PUBLISH_LIST = "LevelOrderPubLishList"
ACTION_CANCEL_ORDER = "LevelOrderCancel"
ACTION_FINISH_ORDER = "LevelOrderOver"
ACTION_UP_PRICE = "LevelOrderUpPrice"
ACTION_MODIFY_PRICE = "LevelOrderModify_Price"
ACTION_MESSAGE_ADD = "LevelOrderMessageAdd"
ACTION_MESSAGE_LIST = "LevelOrderMessageList"
ACTION_PROGRESS_ADD = "LevelOrderProgressAdd"
ACTION_PROGRESS_LIST = "LevelOrderProgressList"
ACTION_USER_MONEY = "UserMoney"
ACTION_MONEY_CHANGE_LIST = "UserMoneyChangeList"
ACTION_ZONES = "gamezoneserver"
ACTION_RECOMMEND_PRICE = "RecommendPrice"
#: 阿里云 OSS 临时凭证（传图三步的第 ① 步，`docs/DAILIANTONG-API.md` §4 #15 /
#: `docs/references/dailiantong/open-api-v3.4.md`「获取OSS临时凭证」）。
ACTION_GET_STS = "GetSTS"
# ⭐ 代练通「有、我们没用上」的这批动作（`docs/DAILIANTONG-API.md` §4 #11/#12/#14/#19/#21/#27–#32）。
#: 评价打手（`EvalType` 10 非常满意 / 11 满意 / 12 一般 / 13 很差）。
ACTION_ORDER_VALUE = "LevelOrderValue"
#: 锁单 / 解锁（`Locked` 1 加锁、0 解锁）。
ACTION_ORDER_LOCK = "LevelOrderLock"
#: 申请客服介入（只能「申请」，结果靠回调 / 回读 `CancelStatus`）。
ACTION_REQUEST_ARBITRATION = "LevelOrderRequestArbitration"
#: 上架 / 下架（`Flag` 1 上架、0 下架）。
ACTION_OFF_SELL = "LevelOrderOffSell"
#: 拉黑 / 解拉黑打手（`UID` 取详情 `AcceptUID`；`Flag` 0 添加、1 移除）。
ACTION_ADD_BLACKLIST = "AddBlackList"
#: 按日统计（发单量 / 金额 / 完单率…）。
ACTION_ORDER_STAT = "LevelOrderStat"
#: 订单要求模板（**代练通侧的模板**，⚠️ 与我们自己的「代练要求模板」是两回事）。
ACTION_REQUIRE_TEMP_LIST = "OrderRequireTempList"
ACTION_REQUIRE_TEMP_SET_DEFAULT = "OrderRequireTempSetDefault"
ACTION_REQUIRE_TEMP_DELETE = "OrderRequireTempDelete"
ACTION_REQUIRE_TEMP_ADD = "OrderRequireTempAdd"
ACTION_REQUIRE_TEMP_UPDATE = "OrderRequireTempUpdate"

#: ⭐ **可自动重试的唯一白名单**（显式枚举，⛔ 不用「不是写动作就算读」这种反推）：
#: 只有这里列出的动作，才允许在**连接失败 / 超时**时**至多重试 1 次**。
#: 加新动作前先问：它会不会改远端状态 / 花钱？拿不准就**不加**。
READ_ONLY_ACTIONS = frozenset(
    {
        ACTION_ORDER_DETAIL,
        ACTION_ORDER_STATUS_LIST,
        ACTION_PUBLISH_LIST,
        ACTION_MESSAGE_LIST,
        ACTION_PROGRESS_LIST,
        ACTION_USER_MONEY,
        ACTION_MONEY_CHANGE_LIST,
        ACTION_ZONES,
        ACTION_RECOMMEND_PRICE,
        # ⭐ P3-15（审计修复）：`GetSTS` 也是只读（取临时凭证，不改远端状态、
        # 不花钱）⇒ 连接失败 / 超时允许至多重试一次。⛔ 它不在 `WRITE_ACTIONS` 里。
        ACTION_GET_STS,
        # ⭐（dlt-d）：按日统计 / 订单要求模板列表都是**只读**（不改远端状态、
        # 不花钱）⇒ 连接失败 / 超时允许至多重试一次。
        ACTION_ORDER_STAT,
        ACTION_REQUIRE_TEMP_LIST,
    }
)

#: **永不自动重试**的写动作（本模块封装的 19 个）。这张表用来把红线钉进测试：
#: 每条用例都会让它们遇到传输失败，并断言**只调用了一次**。
#: ⛔ `LevelOrderAdd` 超时后重试 = 真钱重复建单（§6.1 / R11）。
WRITE_ACTIONS = frozenset(
    {
        ACTION_ADD_ORDER,
        ACTION_MODIFY_ORDER,
        ACTION_MODIFY_SPECIAL,
        ACTION_DELETE_ORDER,
        ACTION_CANCEL_ORDER,
        ACTION_FINISH_ORDER,
        ACTION_UP_PRICE,
        ACTION_MODIFY_PRICE,
        ACTION_MESSAGE_ADD,
        ACTION_PROGRESS_ADD,
        # ⭐（dlt-d）：这批「有、我们没用上」的动作**全是写动作**
        # （评价 / 锁单 / 申请客服介入 / 上下架 / 拉黑 / 四个模板写动作）⇒ 一律不重试。
        ACTION_ORDER_VALUE,
        ACTION_ORDER_LOCK,
        ACTION_REQUEST_ARBITRATION,
        ACTION_OFF_SELL,
        ACTION_ADD_BLACKLIST,
        ACTION_REQUIRE_TEMP_SET_DEFAULT,
        ACTION_REQUIRE_TEMP_DELETE,
        ACTION_REQUIRE_TEMP_ADD,
        ACTION_REQUIRE_TEMP_UPDATE,
    }
)

#: `LevelOrderValue` 的 `EvalType` 取值（文档 §「发布订单评价」）。
EVAL_TYPE_VERY_SATISFIED = 10  # 非常满意
EVAL_TYPE_SATISFIED = 11  # 满意
EVAL_TYPE_NORMAL = 12  # 一般
EVAL_TYPE_BAD = 13  # 很差
#: 允许的满意度集合（`is not None` 的显式枚举，⛔ 不用区间猜）。
EVAL_TYPES = frozenset(
    {EVAL_TYPE_VERY_SATISFIED, EVAL_TYPE_SATISFIED, EVAL_TYPE_NORMAL, EVAL_TYPE_BAD}
)

#: `LevelOrderOffSell` 的 `Flag`：1 上架、0 下架（文档原文）。
OFF_SELL_FLAG_ON = 1
OFF_SELL_FLAG_OFF = 0

#: `LevelOrderLock` 的 `Locked`：1 加锁、0 解锁（文档原文）。
LOCK_FLAG_LOCKED = 1
LOCK_FLAG_UNLOCKED = 0

#: `AddBlackList` 的 `Flag`：0 默认添加、1 移除黑名单（文档原文）。
BLACKLIST_FLAG_ADD = 0
BLACKLIST_FLAG_REMOVE = 1

#: `OrderRequireTempAdd` / `OrderRequireTempUpdate` 的 `IsDefault`。
TEMP_IS_DEFAULT = 1
TEMP_IS_NOT_DEFAULT = 0

#: `LevelOrderModify_Special` 的 `Kind` 取值（文档 §「订单特殊修改」）。
SPECIAL_KIND_AMOUNT = 0  # 补款（要支付密码）
SPECIAL_KIND_TIME = 1  # 补时

#: `LevelOrderCancel` 的 `Flag` 取值（文档 §「订单撤销」）。
CANCEL_FLAG_APPLY = 0  # 申请撤销（要支付密码 + 两项金额）
CANCEL_FLAG_CANCEL = 1  # 取消撤销
CANCEL_FLAG_AGREE = 2  # 同意撤销（协商已处理；⚠️ 实测也要支付密码）

#: `pay_password_md5` 只接受 **MD5(支付密码)** 的 32 位小写十六进制。
#: ⛔ 收到明文（长度 / 字符集都不对）时在配置期就拒掉：明文一旦被当 md5 用，
#: 发单会回 `-3 支付密码错误`，而错误路径里根本看不出「传错了哪一层」。
_PAY_PASSWORD_MD5_RE = re.compile(r"[0-9a-f]{32}")

#: 二次转义 Unicode 的最小形态：字面量 `\uXXXX`（至少一位十六进制）。
_UNICODE_ESCAPE_LEN = 6
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _md5_hex(text: str) -> str:
    """32 位小写 MD5（`usedforsecurity=False` 只为兼容 FIPS 环境的显式声明）。"""
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def build_sign(
    values: Sequence[object],
    *,
    app_id: str,
    app_secret: str,
    timestamp: int | str,
    ver: str = API_VERSION,
) -> str:
    """按代练通的规则算 `Sign`（32 位小写）。

    `values` = **按发送顺序**排列的业务参数**值**（含 `Action` 的值，见模块 docstring）：
    `MD5("".join(values) + app_id + timestamp + ver + app_secret)`。
    ⛔ 参数名不参与签名；⛔ 顺序敏感（我们照自己实际发送的顺序算，不自作主张排序）。

    自证向量（厂商文档里的公开示例，⛔ 不是我们的凭据）：

    >>> build_sign(
    ...     ["gamezoneserver"],
    ...     app_id="17ab8be89b8c",
    ...     app_secret="fe1008bdffaa485887e042faa798c628",
    ...     timestamp=1450774491,
    ... ) == "9a97d04b4b7f65529c0ef4b6200e68b4"
    True
    """
    joined = "".join(str(value) for value in values)
    return _md5_hex(f"{joined}{app_id}{timestamp}{ver}{app_secret}")


# ── 阿里云 OSS（传图三步的第 ② 步）─────────────────────────────────────────
# 依据 = 厂商文档 `docs/references/dailiantong/open-api-v3.4.md`「上传截图流程」： endpoint `oss-cn-hangzhou.aliyuncs.com`、bucket `dltfile01`；`GetSTS` 拿临时三件套，
# 上传后把**OSS 目标文件名**（可带路径、不带 http）填进 `LevelOrderProgressAdd` 的 `Img`。 ⚠️ 本仓**不引入**阿里云 SDK（项目纪律：不擅自加运行时依赖）⇒ 签名 / 上传都用标准库 + `httpx` 手写。唯一的凭据面是 `GetSTS` 回来的三件套，⛔ 不进日志 / 异常文案。

#: OSS endpoint（厂商文档写死的值；⛔ 不是我们猜的）。
OSS_ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"
#: OSS bucket（同上；`Img` 里的对象名相对它）。
OSS_BUCKET = "dltfile01"

#: 上传超时（秒）。二维码是小图（我方存储侧的图上限定为 18 MiB），15 秒足够；⛔ 写动作不重试。
OSS_UPLOAD_TIMEOUT_SECONDS = 15.0

#: OSS 错误 XML 里的 `<Code>…</Code>`（只取这个短码进异常文案，⛔ 不搬整个 body：
#: body 里可能有回显的请求头 / 凭据，那是上游的错误面，不该由我们搬运）。
_OSS_ERROR_CODE_RE = re.compile(r"<Code>\s*([A-Za-z0-9_-]+)\s*</Code>")


class OssUploadError(Exception):
    """上传到代练通 OSS 失败（**独立故障面**：不是代练通 API 的业务失败）。 ⚠️ 与 `DailiantongError` **不同族、不互相伪装**（先例 = 鸡蛋铺传图线的
    `OssUploadError`）：前者是「代练通回了业务失败」，本类是「阿里云 OSS 这一跳没成」。 `message` 里**只有** HTTP 状态 + OSS 短错误码 + 异常类型名，⛔ 不含 STS 三件套、
    ⛔ 不含对象名。"""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:  # pragma: no cover - 稳定出口
        return self.message


@dataclass(frozen=True)
class OssStsCredentials:
    """`GetSTS` 回来的阿里云 OSS 临时凭证三件套。 ⚠️ 有失效时间、**每次上传前重新取**（厂商文档原话「每次上传需要重新获取」）⇒
    调用方⛔ 不要缓存它、⛔ 不要落库。 `repr()` **三个值全部打码**：`access_key_id` 虽然不是密钥，但它与 token 一样属于
    「不该出现在日志里的临时凭据」（`AGENTS.md` §4 的口径是凭据面整体不留痕）。"""

    security_token: str
    access_key_id: str
    access_key_secret: str

    def __repr__(self) -> str:
        return (
            "OssStsCredentials(security_token='***', access_key_id='***', "
            "access_key_secret='***')"
        )


def _canonicalized_oss_headers(
    oss_headers: Mapping[str, str] | Sequence[tuple[str, str]],
) -> str:
    """`x-oss-*` 头 → `key:value\\n`（**键小写 + 排序**；阿里云 OSS V1 签名规则）。
    ⚠️ **值不做 URL 编码**、键比较不区分大小写；同一键大小写重复时以排序后的稳定顺序为准
    （我们的调用点只有 `x-oss-security-token` 一个键，这条只是形状兜底）。"""
    items = oss_headers.items() if isinstance(oss_headers, Mapping) else oss_headers
    pairs = sorted((str(name).lower(), str(value)) for name, value in items)
    return "".join(f"{name}:{value}\n" for name, value in pairs)


def oss_string_to_sign(
    *,
    verb: str,
    date: str,
    resource: str,
    content_md5: str = "",
    content_type: str = "",
    oss_headers: Mapping[str, str] | Sequence[tuple[str, str]] = (),
) -> str:
    """阿里云 OSS V1 的 `StringToSign`（**可逐行对拍**，单测钉住字面形状）。 ``` VERB\\n Content-MD5\\n Content-Type\\n Date\\n CanonicalizedOSSHeaders（每行 key:value\\n，可空）
    CanonicalizedResource（/bucket/object_key） ``` ⚠️ `Content-MD5` 我们**不发**（`httpx` 已在 HTTPS 上传完整 body；少一个可算错的槽位），所以那一行恒为空串——但**槽位必须保留**（少一行会让签名整条错位）。
    ⚠️ `CanonicalizedResource` 用**原始对象名**（⛔ 不 URL 编码）：本仓的对象名形如 `tt/<租户 schema>/<单号>/<qr|ar>-…-<毫秒时间戳>.<jpg|png>`，只含安全字符 ⇒ 两种写法同值； `put_object` 里对对象名做了白名单校验，杜绝「编码前后不一致」这类歧义。"""
    return (
        f"{verb}\n"
        f"{content_md5}\n"
        f"{content_type}\n"
        f"{date}\n"
        f"{_canonicalized_oss_headers(oss_headers)}"
        f"{resource}"
    )


def sign_oss_v1(
    *,
    access_key_secret: str,
    verb: str,
    date: str,
    resource: str,
    content_md5: str = "",
    content_type: str = "",
    oss_headers: Mapping[str, str] | Sequence[tuple[str, str]] = (),
) -> str:
    """算 OSS V1 签名：`base64(HMAC-SHA1(AccessKeySecret, StringToSign))`。"""
    string_to_sign = oss_string_to_sign(
        verb=verb,
        date=date,
        resource=resource,
        content_md5=content_md5,
        content_type=content_type,
        oss_headers=oss_headers,
    )
    digest = hmac.new(
        access_key_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


#: 对象名的合法形状（服务层自己拼的：`tt/<租户 schema>/<单号>/<qr|ar>-…-<毫秒时间戳>.<扩展名>`）。
#: `put_object` 只接受这个白名单 ⇒ 签名里的「原始对象名」与 URL 里的编码形态**不会分叉**，
#: 也不会把 `..` / 查询串 / 绝对 URL 带进 OSS 请求。
_OSS_OBJECT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


def _validate_object_key(object_key: str) -> str:
    key = (object_key or "").strip()
    if not key or key.startswith("/") or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError("OSS 对象名不合法（必须是非空相对路径，且不含空段 / . / ..）")
    if not _OSS_OBJECT_KEY_RE.fullmatch(key):
        raise ValueError("OSS 对象名不合法（只允许字母 / 数字 / . _ - /）")
    return key


def put_object(
    sts: OssStsCredentials,
    object_key: str,
    data: bytes,
    content_type: str,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = OSS_UPLOAD_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> None:
    """把一张图 PUT 到代练通的阿里云 OSS（传图三步的第 ② 步）。 - `sts`：刚 `GetSTS` 回来的临时三件套（⛔ 不缓存；本函数不记日志）； - `object_key`：对象名（形如 `tt/<租户 schema>/<单号>/qr-<毫秒>.<ext>`），⛔ 不带 http；
    - 请求头：`Date`（RFC 1123 GMT）、`Content-Type`、`x-oss-security-token`； - 签名：`Authorization: OSS <AccessKeyId>:<base64(HMAC-SHA1(secret, StringToSign))>`。
    `transport` / `now` **只给测试**（`httpx.MockTransport` + 固定 `Date`），生产不传。 ⛔ 失败**不重试**（写动作；重试只会多压一次上游），失败翻成 `OssUploadError`。"""
    key = _validate_object_key(object_key)
    if not isinstance(data, bytes) or not data:
        raise ValueError("OSS 上传内容不能为空（且必须是 bytes）")
    for name, value in (
        ("security_token", sts.security_token),
        ("access_key_id", sts.access_key_id),
        ("access_key_secret", sts.access_key_secret),
    ):
        if not (value or "").strip():
            # ⛔ 消息里只有字段名，不带值。
            raise ValueError(f"OSS 临时凭证缺少 {name}")

    content_type = (content_type or "").strip() or "application/octet-stream"
    date = email.utils.formatdate(
        (now or datetime.now(timezone.utc)).timestamp(), usegmt=True
    )
    resource = f"/{OSS_BUCKET}/{key}"
    oss_headers = (("x-oss-security-token", sts.security_token),)
    signature = sign_oss_v1(
        access_key_secret=sts.access_key_secret,
        verb="PUT",
        date=date,
        resource=resource,
        content_type=content_type,
        oss_headers=oss_headers,
    )
    headers = {
        "Date": date,
        "Content-Type": content_type,
        "x-oss-security-token": sts.security_token,
        "Authorization": f"OSS {sts.access_key_id}:{signature}",
    }
    url = f"https://{OSS_BUCKET}.{OSS_ENDPOINT}/{quote(key, safe='/')}"

    if transport is None:
        transport = vendor_transport(url)
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.put(url, content=data, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        # ⛔ 只记异常类型名：异常文本可能带 URL / 请求头（凭据面）。
        raise OssUploadError(
            f"上传到代练通 OSS 失败（{type(exc).__name__}）"
        ) from None

    if not response.is_success:
        code_match = _OSS_ERROR_CODE_RE.search(response.text or "")
        suffix = f"，Code={code_match.group(1)}" if code_match else ""
        raise OssUploadError(
            f"上传到代练通 OSS 失败（HTTP {response.status_code}{suffix}）"
        )


def decode_escaped_unicode(value: str) -> str:
    """把「看起来是二次转义的 Unicode」还原一层（只认 `\\uXXXX`，别的一字不动）。实测动机：`LevelOrderPubLishList` 里 `Actor` 回的是**字面量** `\\u5f85\\u5b9a`
    （不还原的话，页面上会看到一堆斜杠和字母），而同列表的 `Title` 是正常中文。只在字符串里**真的出现** `\\u` 且后面跟 4 位十六进制时才动，因此正常中文、
    含 `\\n` 的文本都不会被误伤。代理对（`\\uD83D\\uDE00`）会合成一个字符。"""
    if "\\u" not in value:
        return value
    out: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        if value.startswith("\\u", index) and index + _UNICODE_ESCAPE_LEN <= length:
            digits = value[index + 2 : index + _UNICODE_ESCAPE_LEN]
            if all(char in _HEX_DIGITS for char in digits):
                code = int(digits, 16)
                index += _UNICODE_ESCAPE_LEN
                # 高代理 + 紧接着的低代理 ⇒ 合成一个补充平面字符。
                if 0xD800 <= code <= 0xDBFF and value.startswith("\\u", index):
                    low_digits = value[index + 2 : index + _UNICODE_ESCAPE_LEN]
                    if (
                        len(low_digits) == 4
                        and all(char in _HEX_DIGITS for char in low_digits)
                        and 0xDC00 <= int(low_digits, 16) <= 0xDFFF
                    ):
                        low = int(low_digits, 16)
                        code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                        index += _UNICODE_ESCAPE_LEN
                # ⭐ P3-15：畸形代理对（孤立高代理 / 孤立低代理）⛔ 不能 `chr()` 出一个
                # 孤立代理字符（会污染下游字符串、写日志/JSON 时也可能炸）；用 U+FFFD。
                if 0xD800 <= code <= 0xDFFF:
                    out.append("\ufffd")
                    continue
                out.append(chr(code))
                continue
        out.append(value[index])
        index += 1
    return "".join(out)


def _decode_payload(value: Any) -> Any:
    """对解析后的回包**递归**做一次 Unicode 兜底解码（只动字符串值，不动键）。"""
    if isinstance(value, str):
        return decode_escaped_unicode(value)
    if isinstance(value, list):
        return [_decode_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_payload(item) for key, item in value.items()}
    return value


class DailiantongError(Exception):
    """上游**业务失败**（拿到了回包，但 `Result` 不是 `"1"`）。字段与调用约定： - `code`：`Result` 归一成字符串后的值（`"-2"` / `"-101"` …），可能为 `None`
    （仅传输 / 协议级失败时）； - `message`：**上游 `Err` 原文**（⛔ 不做映射、不编文案）； - `action`：这次调用的是哪个动作（排障用）；
    - `raw`：解析后的上游回包（`dict`），便于调用方留证。 ⛔ `__str__` 只拼上面三个标量，**不碰请求参数**——因此 `app_secret` / `pay_password_md5` 不会出现在异常字符串里。"""

    def __init__(
        self,
        code: str | None,
        message: str,
        action: str,
        raw: Any = None,
    ) -> None:
        self.code = code
        self.message = message
        self.action = action
        self.raw = raw
        super().__init__(self._describe())

    def _describe(self) -> str:
        if self.code is None:
            return f"{self.action} 调用失败：{self.message}"
        return f"{self.action} 失败（code={self.code}）：{self.message}"

    def __str__(self) -> str:  # pragma: no cover - 只是 _describe 的稳定出口
        return self._describe()


class DailiantongTransportError(DailiantongError):
    """**没拿到可用的业务答复**：连接失败 / 超时 / HTTP 非 2xx / 回包不是 JSON / 形状不对。
    `code` 恒为 `None`。⚠️ 它回答的是「这次调用没拿到上游答复」，**不是**「业务失败」——
    业务失败看父类的 `code`（`AGENTS.md` §5-E 的同一条边界）。"""


@dataclass(frozen=True)
class ListPage:
    """列表信封（第三种形态）解析后的一页。 - `rows`：行字典的元组。上游**空结果是空字符串 `""`**，这里统一成空元组；
    - `record_count`：信封里的 `RecordCount`（总条数）；**缺失或不是整数时为 `None`**
    （实测有的动作连这个键都不回，⛔ 别拿 0 冒充「查过了、没有数据」）。"""

    rows: tuple[dict[str, Any], ...]
    record_count: int | None = None


@dataclass(frozen=True)
class DailiantongConfig:
    """一个代练通商户的凭据 + 传输参数。 ⚠️ `pay_password_md5` 是 **MD5(支付密码)** 这一层，**不是明文**：
    我们只需要它就能现算 `PayPass`，明文永不入库 / 永不进日志（§1 / R3）。"""

    app_id: str
    app_secret: str
    user_id: str
    pay_password_md5: str
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 10.0

    def __post_init__(self) -> None:
        for name in ("app_id", "app_secret", "user_id", "pay_password_md5"):
            if not getattr(self, name):
                # ⛔ 消息里只有字段名，不带值。
                raise ValueError(f"DailiantongConfig.{name} 不能为空")
        if not _PAY_PASSWORD_MD5_RE.fullmatch(self.pay_password_md5):
            raise ValueError(
                "DailiantongConfig.pay_password_md5 必须是 MD5(支付密码) 的 32 位小写十六进制"
                "（⛔ 不是支付密码明文）"
            )
        if not self.base_url:
            raise ValueError("DailiantongConfig.base_url 不能为空")
        if not self.base_url.startswith("https://"):
            # ⛔ 请求 body 里有 AppSecret 派生值 PayPass（以及订单账号资料），
            # 明文链路不可接受（文档 R10：只走 HTTPS，实测 https 同路径可用）。
            raise ValueError(
                "DailiantongConfig.base_url 必须以 https:// 开头"
                "（⛔ 凭据走 body，明文链路不可接受）"
            )

    @property
    def pay_pass(self) -> str:
        """`PayPass = MD5(MD5(支付密码) + 用户ID)`（支付密码的**第二次** MD5）。"""
        return _md5_hex(self.pay_password_md5 + self.user_id)

    def __repr__(self) -> str:
        # ⛔ 凭据红线：app_secret / pay_password_md5 一律打码（日志里只会看到 ***）。
        return (
            f"DailiantongConfig(app_id={self.app_id!r}, app_secret='***', "
            f"user_id={self.user_id!r}, pay_password_md5='***', "
            f"base_url={self.base_url!r}, timeout={self.timeout!r})"
        )


class DailiantongClient:
    """线程安全的代练通客户端（内部一个 `httpx.Client`，可复用、可用 `with` 管生命周期）。典型用法:: with DailiantongClient(config) as client:
    serial_no = client.add_order(...)   # ⛔ 这一跳绝不重试 detail = client.order_detail(serial_no)
    `transport` / `clock` 只给测试用（`httpx.MockTransport` + 固定时间戳），生产不传。"""

    def __init__(
        self,
        config: DailiantongConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or time.time
        # 显式超时（连接 / 读 / 写 / 池各用同一个值；重试最多让最坏耗时翻倍）。
        if transport is None:
            transport = vendor_transport(config.base_url)
        self._http = httpx.Client(timeout=config.timeout, transport=transport)

    @property
    def config(self) -> DailiantongConfig:
        return self._config

    def __repr__(self) -> str:
        # config 自己的 repr 已经打码，这里直接借用。
        return f"DailiantongClient({self._config!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> DailiantongClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── 传输与解析（内部）───────────────────────────────────────────────────

    def _request(self, action: str, params: Sequence[tuple[str, object]]) -> Any:
        """发一次请求并解析出**原始业务载荷**；只对白名单里的只读动作重试一次。"""
        timestamp = int(self._clock())
        values = [action] + [value for _, value in params]
        sign = build_sign(
            values,
            app_id=self._config.app_id,
            app_secret=self._config.app_secret,
            timestamp=timestamp,
        )
        # 业务参数**保持调用方给的顺序**：签名按这个顺序算，发送也用这个顺序。
        form: list[tuple[str, str]] = [("Action", action)]
        form.extend((name, str(value)) for name, value in params)
        form.extend(
            [
                ("AppID", self._config.app_id),
                ("TimeStamp", str(timestamp)),
                ("Ver", API_VERSION),
                ("Sign", sign),
            ]
        )

        # ⭐ P3-15：发送前断言参数名唯一。重复的参数名会被 `dict(form)` 静默吞掉一个
        # （签名按两个值算、实际只发一个 ⇒ 上游看到的是「签名错误」，排障极难）。
        names = [name for name, _value in form]
        if len(set(names)) != len(names):
            duplicated = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(
                "代练通请求参数名重复：" + "、".join(duplicated) + "（拒绝发送）"
            )

        max_attempts = 2 if action in READ_ONLY_ACTIONS else 1
        for attempt in range(1, max_attempts + 1):
            try:
                # ⛔ 用 `data=`（表单），⛔ 不用 `params=`：凭据 / 单号不进 URL。
                # ⚠️ httpx 的 `data=` 只认 dict / bytes（⛔ 不认 list of tuples，
                #    否则会被当成流、在 MockTransport 里炸 `TypeError`）⇒ 转成保序 dict。
                response = self._http.post(self._config.base_url, data=dict(form))
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < max_attempts:
                    # ⚠️ 只记动作名与异常类型，不记请求体（里面有 PayPass）。
                    logger.warning(
                        "代练通只读动作 %s 传输失败（%s），按白名单重试一次",
                        action,
                        type(exc).__name__,
                    )
                    continue
                raise DailiantongTransportError(
                    code=None,
                    message=f"请求失败（{type(exc).__name__}）",
                    action=action,
                ) from exc

            if not response.is_success:
                # 非 2xx 不重试：这是服务端明确回了个非业务答复（WAF 页 / 网关错），
                # 再打一次也不会变好；而且任务红线只允许「连接失败 / 超时」重试。
                raise DailiantongTransportError(
                    code=None,
                    message=f"返回 HTTP {response.status_code}（不是业务回包）",
                    action=action,
                )
            return self._parse_payload(action, response)

        raise AssertionError("重试循环不可能走到这里")  # pragma: no cover

    def _parse_payload(self, action: str, response: httpx.Response) -> Any:
        text = response.text
        if not text.strip():
            raise DailiantongTransportError(
                code=None, message="回包为空", action=action
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DailiantongTransportError(
                code=None, message="回包不是 JSON", action=action
            ) from exc
        # 实测记录里出现过「整个回包再被 JSON 编码一层」的形态（§3 第 3 条末句），
        # 解两次是幂等的：正常对象解一次就停。
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise DailiantongTransportError(
                    code=None, message="回包里嵌套的字符串不是 JSON", action=action
                ) from exc
        return _decode_payload(payload)

    def _call(self, action: str, params: Sequence[tuple[str, object]]) -> Any:
        """发请求 + 判业务成败；成功时返回 `_ResultValue`（信封①）或原始载荷（②③）。"""
        payload = self._request(action, params)
        if isinstance(payload, dict) and "Result" in payload:
            # ⚠️ `Result` 类型不固定（文档字符串、实测发单是数字）⇒ 一律 str() 归一，
            # 再按「数值上等于 1」判成功（兼容 `1.0`，P3-15）。
            code = str(payload.get("Result")).strip()
            message = str(payload.get("Err") or "")
            if not _is_success_code(code):
                raise DailiantongError(
                    code=code, message=message, action=action, raw=payload
                )
            return _ResultValue(message=message)
        return payload

    def _call_message(self, action: str, params: Sequence[tuple[str, object]]) -> str:
        """给「成功回 `Result` 信封」的写动作用：返回上游 `Err` 原文。
        在 `add_order` 上 `Err` 装的是**新订单号**（实测 20 位纯数字），
        其它动作通常是「修改成功.」这类提示；⛔ 本模块不猜、不改写。"""
        result = self._call(action, params)
        if not isinstance(result, _ResultValue):
            raise _protocol_error(action, "预期 Result 信封，拿到裸对象 / 列表")
        return result.message

    def _call_object(self, action: str, params: Sequence[tuple[str, object]]) -> dict:
        """给裸对象形态（形态②）用。"""
        result = self._call(action, params)
        if not isinstance(result, dict):
            raise _protocol_error(action, "预期裸对象，拿到列表或 Result 信封")
        return result

    def _call_list(
        self,
        action: str,
        params: Sequence[tuple[str, object]],
    ) -> ListPage:
        """给列表信封（形态③）用：空字符串 ⇒ 空页；数组 ⇒ 行；`RecordCount` 缺失 ⇒ `None`。"""
        result = self._call(action, params)
        return _list_page(action, result)

    # ── 发单 / 改单 ─────────────────────────────────────────────────────────

    def add_order(
        self,
        *,
        zone_server_id: str,
        title: str,
        time_limit: int,
        price: object,
        ensure1: object,
        ensure2: object,
        actors: str,
        contact_name: str | None = None,
        mobile: str | None = None,
        qq: str | None = None,
        limit_accept: int | None = None,
        base_price: object | None = None,
        label1: str | None = None,
        label2: str | None = None,
        memo: str | None = None,
        groups: str | None = None,
        off_sell_days: int | None = None,
        power_level: int | None = None,
        ex_order_no: str | None = None,
        min_power_level: int | None = None,
    ) -> str:
        """发单（`LevelOrderAdd`）。成功时返回**新订单号**。实测（§6.1）：成功回 `{"Result":1,"Err":"<订单号>"}`——订单号在 `Err` 里， **20 位纯数字**（⛔ 不是文档示例的 `ORD…` 格式，⛔ 本模块不按前缀 / 长度校验）。
        `PayPass` 每次都会带上；`Ensure1` / `Ensure2` 必须显式给（0 是合法值，见 D-0015）。 `actors` 的五段格式是 `账号|*|密码|*|角色名|*|当前游戏信息|*|代练要求`（已裁定填占位文本）。
        ⛔ **这一跳绝不自动重试**：超时后外部单号查不回来，重试 = 真钱重复建单（R11）。 ⚠️ 返回空串是上游异常（实测没有出现过）；本模块照实返回，由调用方决定要不要当失败。"""
        params: list[tuple[str, object]] = [
            ("ZoneServerID", zone_server_id),
            ("Title", title),
            ("TimeLimit", time_limit),
            ("Price", price),
            ("Ensure1", ensure1),
            ("Ensure2", ensure2),
            ("ContactName", contact_name),
            ("Mobile", mobile),
            ("QQ", qq),
            ("LimitAccept", limit_accept),
            ("BasePrice", base_price),
            ("Label1", label1),
            ("Label2", label2),
            ("Memo", memo),
            ("Actors", actors),
            ("Groups", groups),
            ("PayPass", self._config.pay_pass),
            ("OffSellDays", off_sell_days),
            ("PowerLevel", power_level),
            ("ExOrderNo", ex_order_no),
            ("MinPowerLevel", min_power_level),
        ]
        return self._call_message(ACTION_ADD_ORDER, _drop_none(params))

    def modify_order(
        self,
        serial_no: str,
        *,
        title: str,
        time_limit: int,
        price: object,
        ensure1: object,
        ensure2: object,
        zone_server_id: str | None = None,
        contact_name: str | None = None,
        mobile: str | None = None,
        qq: str | None = None,
        limit_accept: int | None = None,
        base_price: object | None = None,
        label1: str | None = None,
        label2: str | None = None,
        memo: str | None = None,
        actors: str | None = None,
        groups: str | None = None,
    ) -> str:
        """改单（`LevelOrderModify`）：标题 / 时限 / 价格 / 保证金 / 账号资料等。
        ⚠️ 实测**只有未接手或已下架的订单**能改（否则 `-1`），且这个动作**不验签**（§4 #5）。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("ZoneServerID", zone_server_id),
            ("Title", title),
            ("TimeLimit", time_limit),
            ("Price", price),
            ("Ensure1", ensure1),
            ("Ensure2", ensure2),
            ("ContactName", contact_name),
            ("Mobile", mobile),
            ("QQ", qq),
            ("LimitAccept", limit_accept),
            ("BasePrice", base_price),
            ("Label1", label1),
            ("Label2", label2),
            ("Memo", memo),
            ("Actors", actors),
            ("Groups", groups),
            ("PayPass", self._config.pay_pass),
        ]
        return self._call_message(ACTION_MODIFY_ORDER, _drop_none(params))

    def modify_special(
        self,
        serial_no: str,
        *,
        kind: int,
        val: object,
    ) -> str:
        """订单特殊修改（`LevelOrderModify_Special`）：补款 / 补时 / 改游戏密码。 `kind` 取 `SPECIAL_KIND_AMOUNT`（0 补款）/ `SPECIAL_KIND_TIME`（1 补时）/
        2（改游戏密码）。文档写明**只有补款要 `PayPass`**，所以只在 `kind == 0` 时带。 ⚠️ 实测这类动作**不推回调**（§6.2 结论 1）⇒ 调用方必须回读详情。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("Kind", kind),
            ("Val", val),
        ]
        if kind == SPECIAL_KIND_AMOUNT:
            params.append(("PayPass", self._config.pay_pass))
        return self._call_message(ACTION_MODIFY_SPECIAL, params)

    def delete_order(self, serial_no: str) -> str:
        """删除订单（`LevelOrderDelSelf`）。
        ⭐ 实测（R13）：**未接手**的单删除 = **即时退款**（冻结归零、余额复原）；
        一旦有打手接手就删不掉，只能走协商撤销。"""
        return self._call_message(
            ACTION_DELETE_ORDER, [("ODSerialNo", serial_no)]
        )

    # ── 查询 ────────────────────────────────────────────────────────────────

    def order_detail(self, serial_no: str) -> dict[str, Any]:
        """订单详情（`LevelOrderDetail`，裸对象）。 ⚠️ 实测（§6.1 第 4 条）：详情里**没有 `ExOrderNo`** ⇒ 别指望按外部单号反查；
        必须存下发单返回的订单号。⚠️ `GamePass` 会被**明文回吐**，因此只能放占位文本。"""
        return self._call_object(
            ACTION_ORDER_DETAIL, [("ODSerialNo", serial_no)]
        )

    def order_status_list(
        self,
        *,
        s_date: str,
        e_date: str,
        serial_no: str | None = None,
        page_index: int = 1,
        page_size: int = 5000,
    ) -> ListPage:
        """只查状态（`LevelOrderPubLishList_Status`）——回调不稳时的兜底轮询。
        每页**最大 5000 条**（文档 + §7「兜底轮询」），只回 `SerialNo` / `Status` / `CancelStatus`。`serial_no` 为空 = 查时间范围内全部。"""
        params: list[tuple[str, object]] = [
            ("sDate", s_date),
            ("eDate", e_date),
            ("SerialNo", serial_no),
            ("PageIndex", page_index),
            ("PageSize", page_size),
        ]
        return self._call_list(ACTION_ORDER_STATUS_LIST, _drop_none(params))

    def publish_list(
        self,
        *,
        s_date: str,
        e_date: str,
        status: int | None = None,
        cancel_status: int | None = None,
        price_area: str | None = None,
        search_type: str | None = None,
        search_str: str | None = None,
        page_index: int = 1,
        page_size: int = 20,
    ) -> ListPage:
        """我发布的订单列表（`LevelOrderPubLishList`）。 ⚠️ 实测（§6.1 第 4 条）：列表里**没有 `ExOrderNo`**；`Actor` 是二次转义
        Unicode（本模块已还原）。筛选参数不传 = 不筛（与厂商文档的「可不传递」一致）。"""
        params: list[tuple[str, object]] = [
            ("Status", status),
            ("CancelStatus", cancel_status),
            ("PriceArea", price_area),
            ("SearchType", search_type),
            ("SearchStr", search_str),
            ("sDate", s_date),
            ("eDate", e_date),
            ("PageIndex", page_index),
            ("PageSize", page_size),
        ]
        return self._call_list(ACTION_PUBLISH_LIST, _drop_none(params))

    def message_list(self, serial_no: str, *, max_count: int | None = None) -> ListPage:
        """订单留言（`LevelOrderMessageList`）。`max_count` 文档上限 500。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("Max", max_count),
        ]
        return self._call_list(ACTION_MESSAGE_LIST, _drop_none(params))

    def progress_list(self, serial_no: str, *, max_count: int | None = None) -> ListPage:
        """订单进度 / 截图（`LevelOrderProgressList`）。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("Max", max_count),
        ]
        return self._call_list(ACTION_PROGRESS_LIST, _drop_none(params))

    def user_money(self) -> dict[str, Any]:
        """账户余额（`UserMoney`，裸对象）：`SumBal` / `FreezeBal` / `ActiveBal`。
        ⚠️ 本动作**严格验签**（§2.1）⇒ 签名错会回 `-102`，这里会抛 `DailiantongError`。"""
        return self._call_object(ACTION_USER_MONEY, [])

    def money_change_list(
        self,
        *,
        s_date: str,
        e_date: str,
        change_type: int | None = None,
        rela_serial_no: str | None = None,
        page_index: int = 1,
        page_size: int = 50,
    ) -> ListPage:
        """资金流水（`UserMoneyChangeList`）。`page_size` 文档上限 50。"""
        params: list[tuple[str, object]] = [
            ("sDate", s_date),
            ("eDate", e_date),
            ("ChangeType", change_type),
            ("RelaSerialNo", rela_serial_no),
            ("PageIndex", page_index),
            ("PageSize", page_size),
        ]
        return self._call_list(ACTION_MONEY_CHANGE_LIST, _drop_none(params))

    def zones(self) -> ListPage:
        """全区服字典（`gamezoneserver`，列表信封但**没有** `RecordCount`）。
        返回值是 `ZoneServerID` + `ServerName`（例如「英雄联盟/电信/艾欧尼亚」；
        ⛔ 别把 `ServerName` 当成我们自己的区服名，映射是人工的事）。"""
        return self._call_list(ACTION_ZONES, [])

    def recommend_price(
        self,
        *,
        title: str,
        zone_server_id: str,
        price_type: int = 0,
    ) -> dict[str, Any]:
        """推荐价格（`RecommendPrice`，裸对象）：`BasePrice` / `Ensure1` / `Ensure2` / `TimeLimit`。
        ⚠️ 它只按标题 + 区服给建议价，**不懂我们的定价规则**（§8）。"""
        params: list[tuple[str, object]] = [
            ("Title", title),
            ("ZoneServerID", zone_server_id),
            ("PriceType", price_type),
        ]
        return self._call_object(ACTION_RECOMMEND_PRICE, params)

    # ── 传图：OSS 临时凭证 ──────────────────────────────────────────────────

    def get_sts(self) -> OssStsCredentials:
        """取阿里云 OSS 临时凭证（`GetSTS`，**裸对象信封**，不是 `Result` 信封）。返回 `{SecurityToken, AccessKeyId, AccessKeySecret}` 三件套；⚠️ 有失效时间，
        厂商文档写明「每次上传需要重新获取」⇒ 调用方⛔ 不缓存、⛔ 不落库。形状不对（缺字段 / 空值）⇒ `DailiantongTransportError`，消息里**只有字段名**，
        ⛔ 不带任何已经拿到的值（凭据红线）。"""
        payload = self._call_object(ACTION_GET_STS, [])
        return _parse_sts(ACTION_GET_STS, payload)

    # ── 撤销 / 完单 / 改价 ─────────────────────────────────────────────────

    def cancel_order(
        self,
        serial_no: str,
        flag: int,
        *,
        pay_level_bal: object | None = None,
        rep_ensure_bal: object | None = None,
        comment: str | None = None,
    ) -> str:
        """撤销（`LevelOrderCancel`）：`flag` 取 0 申请撤销 / 1 取消撤销 / 2 同意撤销（0 / 1 / 2 分别有 `CANCEL_FLAG_APPLY` / `CANCEL_FLAG_CANCEL` / `CANCEL_FLAG_AGREE` 常量）。
        ⚠️ 这是**协商制**撤销，不是「撤销即完」：打手有权不同意，之后走 `CancelStatus=11..16` 与客服介入（§6）。文档：`flag=0/2` 必须给 `PayLevelBal` / `RepEnsureBal`；`flag=0/2` 还要 `PayPass`（自动带）。
        ⭐ `flag=2`（同意撤销）也要 `PayPass` **不是文档写的**——真机实测：不带回 `{"Result":-2,"Err":"支付密码错误."}`，带上才 `{"Result":1,"Err":"撤销成功."}` 并把 `CancelStatus` 11 → 12；`flag=1`（取消撤销）实测不需要。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("Flag", flag),
            ("PayLevelBal", pay_level_bal),
            ("RepEnsureBal", rep_ensure_bal),
            ("Comment", comment),
        ]
        # ⚠️ flag=2 也要，真机实测（文档只写了 flag=0 要）。
        if flag in (CANCEL_FLAG_APPLY, CANCEL_FLAG_AGREE):
            params.append(("PayPass", self._config.pay_pass))
        return self._call_message(ACTION_CANCEL_ORDER, _drop_none(params))

    def finish_order(self, serial_no: str) -> str:
        """确认完单（`LevelOrderOver`，要 `PayPass`）。
        ⚠️ 实测（§6.2）：完单后扣的正好是订单价，**没看到额外手续费**。"""
        return self._call_message(
            ACTION_FINISH_ORDER,
            [("ODSerialNo", serial_no), ("PayPass", self._config.pay_pass)],
        )

    def up_price(self, serial_no: str, up_price: object) -> str:
        """未接手订单加价（`LevelOrderUpPrice`，要 `PayPass`）。"""
        return self._call_message(
            ACTION_UP_PRICE,
            [
                ("ODSerialNo", serial_no),
                ("UpPrice", up_price),
                ("PayPass", self._config.pay_pass),
            ],
        )

    def modify_price(self, serial_no: str, price: object) -> str:
        """未接手订单改价（`LevelOrderModify_Price`，要 `PayPass`）。"""
        return self._call_message(
            ACTION_MODIFY_PRICE,
            [
                ("ODSerialNo", serial_no),
                ("Price", price),
                ("PayPass", self._config.pay_pass),
            ],
        )

    # ── 评价 / 锁单 / 客服介入 / 上下架 / 拉黑（dlt-d）──────────

    def value_order(
        self,
        serial_no: str,
        *,
        other_user_id: str,
        eval_type: int,
        comment: str | None = None,
    ) -> str:
        """评价打手（`LevelOrderValue`）。实测（§4 #11）：不存在的单回 `-2 订单未达最终状态`，已评价过回
        `-1 已经评价过了,不能重复评价.`。`eval_type` 取 `EVAL_TYPE_*` 四个值之一（10 非常满意 / 11 满意 / 12 一般 / 13 很差）。
        ⚠️ 评价结果**没有可回读的接口**（详情 / 列表都不回吐评价）⇒ 调用方只能按上游成功码算数；⛔ 本模块不做任何「假回读」。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("OtherUserID", other_user_id),
            ("EvalType", eval_type),
            ("Comment", comment),
        ]
        return self._call_message(ACTION_ORDER_VALUE, _drop_none(params))

    def lock_order(
        self,
        serial_no: str,
        *,
        locked: int,
        reason: str | None = None,
    ) -> str:
        """锁单 / 解锁（`LevelOrderLock`）。`locked` 取 `LOCK_FLAG_LOCKED` / `LOCK_FLAG_UNLOCKED`。
        实测（§6.2）：只有「正在代练」（`Status=12`）能锁；上锁后 `Status=15`、
        解锁后回 `Status=12`，两次都会推回调 ⇒ 回读判据 = 状态码变化。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("Locked", locked),
            ("Reason", reason),
        ]
        return self._call_message(ACTION_ORDER_LOCK, _drop_none(params))

    def request_arbitration(self, serial_no: str) -> str:
        """申请客服介入（`LevelOrderRequestArbitration`）。
        ⚠️ 只能「申请」，结果靠对方处理（§8「纠纷仲裁」）；本模块只发一次、⛔ 不重试。"""
        return self._call_message(
            ACTION_REQUEST_ARBITRATION, [("ODSerialNo", serial_no)]
        )

    def off_sell(self, serial_no: str, *, flag: int) -> str:
        """上架 / 下架（`LevelOrderOffSell`）。`flag` 取 `OFF_SELL_FLAG_ON`（上架）/
        `OFF_SELL_FLAG_OFF`（下架）。实测（§4 #19）：不存在的单回 `-1 订单异常`；文档另列
        `-2 订单状态已变化` / `-3 可操作资金不足（上架）`。"""
        return self._call_message(
            ACTION_OFF_SELL,
            [("ODSerialNo", serial_no), ("Flag", flag)],
        )

    def add_blacklist(self, uid: str, *, flag: int = BLACKLIST_FLAG_ADD) -> str:
        """拉黑 / 解拉黑打手（`AddBlackList`）。 `uid` 必须是**打手的用户 ID**（详情里的 `AcceptUID`，不是我们的单号 / 账号）；
        `flag` 取 `BLACKLIST_FLAG_ADD`（0 添加）/ `BLACKLIST_FLAG_REMOVE`（1 移除）。
        ⚠️ 该动作**不验签**（§2.1 表），但本客户端照样按文档签。"""
        return self._call_message(
            ACTION_ADD_BLACKLIST,
            [("UID", uid), ("Flag", flag)],
        )

    # ── 按日统计（只读，带缓存的那条由服务层做）────────────────────────────

    def order_stat(
        self,
        *,
        is_pub: int,
        s_date: str,
        e_date: str,
        page_index: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """按日统计（`LevelOrderStat`，**裸对象**：`LevelOrderStatList` + `LevelOrderStatTotal`）。
        `is_pub` = 0 接单 / 1 发单（文档原文）。日期格式 `YYYY-MM-DD HH:MM:SS`。 ⚠️ 实测回包不是「`<Action>` 列表信封」而是两个固定键的对象（`docs/DAILIANTONG-API.md`
        §4 #27）⇒ 本方法把**原始对象**交给服务层归一化，⛔ 不在协议层猜行形状。"""
        params: list[tuple[str, object]] = [
            ("IsPub", is_pub),
            ("sDate", s_date),
            ("eDate", e_date),
            ("PageIndex", page_index),
            ("PageSize", page_size),
        ]
        return self._call_object(ACTION_ORDER_STAT, params)

    # ── 订单要求模板（⚠️ 代练通侧模板，与我们自己的「代练要求模板」无关）────

    def require_temp_list(
        self,
        *,
        page_index: int | None = 1,
        page_size: int | None = 50,
    ) -> ListPage:
        """代练通订单要求模板列表（`OrderRequireTempList`，列表信封）。 ⚠️ **推断级（未真机复核）**：厂商文档只列了 `Action` 一个参数，但我们的实测
        笔记（`docs/DAILIANTONG-API.md` §4 #28）记着「空参数回 `-101 参数错误`」。这里按同族列表动作的通用形状补 `PageIndex` / `PageSize`（不传 = 不发送），
        真机若仍回 `-101`，把上游原文展示给用户即可（⛔ 不在本地编第二套码表）。"""
        params: list[tuple[str, object]] = [
            ("PageIndex", page_index),
            ("PageSize", page_size),
        ]
        return self._call_list(ACTION_REQUIRE_TEMP_LIST, _drop_none(params))

    def add_require_temp(self, *, title: str, temp: str, is_default: int) -> str:
        """新增订单要求模板（`OrderRequireTempAdd`）。 ⭐ 成功时 `Result=1`、**`Err` 里是模板 ID**（§3 第 2 条 / 文档注）⇒ 返回值就是
        上游 `Err` 原文（调用方**不要**把它当"成功"文案，它是 ID）。上限 10 个：超过时上游回「添加失败，每人最多添加10个模板。」（原样上抛）。"""
        return self._call_message(
            ACTION_REQUIRE_TEMP_ADD,
            [("Title", title), ("Temp", temp), ("IsDefault", is_default)],
        )

    def update_require_temp(
        self,
        temp_id: int,
        *,
        title: str | None = None,
        temp: str | None = None,
        is_default: int | None = None,
    ) -> str:
        """修改订单要求模板（`OrderRequireTempUpdate`）。未给的字段不发送（= 不改）。"""
        params: list[tuple[str, object]] = [
            ("TempID", temp_id),
            ("Title", title),
            ("Temp", temp),
            ("IsDefault", is_default),
        ]
        return self._call_message(ACTION_REQUIRE_TEMP_UPDATE, _drop_none(params))

    def delete_require_temp(self, temp_id: int) -> str:
        """删除订单要求模板（`OrderRequireTempDelete`）。"""
        return self._call_message(
            ACTION_REQUIRE_TEMP_DELETE, [("TempID", temp_id)]
        )

    def set_default_require_temp(self, temp_id: int) -> str:
        """设默认订单要求模板（`OrderRequireTempSetDefault`）。"""
        return self._call_message(
            ACTION_REQUIRE_TEMP_SET_DEFAULT, [("TempID", temp_id)]
        )

    # ── 留言 / 进度 ────────────────────────────────────────────────────────

    def message_add(self, serial_no: str, msg: str) -> str:
        """发留言（`LevelOrderMessageAdd`）。实测会推回调，但回调 `Status=0`、不带状态。"""
        return self._call_message(
            ACTION_MESSAGE_ADD,
            [("ODSerialNo", serial_no), ("Msg", msg)],
        )

    def progress_add(
        self, serial_no: str, msg: str, *, img: str | None = None
    ) -> str:
        """发订单进度（`LevelOrderProgressAdd`，可带图）。 ⛔ **已知假成功**：本动作（以及未封装的 `ShareOrder`）对**不存在的单号**
        也回 `{"Result":1,"Err":"添加完成."}`（§3 第 3 条 / R6）。本客户端**照实返回成功**——⛔ 调用方不能把这里的返回值当成「进度真的写上了」，
        必须**回读详情 / 进度列表**确认。实测本动作**不推回调**（§6.2）。"""
        params: list[tuple[str, object]] = [
            ("ODSerialNo", serial_no),
            ("Msg", msg),
            ("Img", img),
        ]
        return self._call_message(ACTION_PROGRESS_ADD, _drop_none(params))


#: `_call` 成功时对「信封①」的包装（只为区分「Result 信封的 Err」与裸对象）。
@dataclass(frozen=True)
class _ResultValue:
    """信封①成功时的 `Err`（上游把返回值也装在这个字段里）。"""

    message: str


def _drop_none(params: Sequence[tuple[str, object]]) -> list[tuple[str, object]]:
    """丢掉值为 `None` 的参数（= 不传），**保留空串与 0**（两者都是合法值，D-0015）。"""
    return [(name, value) for name, value in params if value is not None]


def _protocol_error(action: str, detail: str) -> DailiantongTransportError:
    """回包形状不符合预期——⚠️ 不是业务失败，是「没拿到可用答复」。"""
    return DailiantongTransportError(
        code=None, message=f"回包形状不符合预期：{detail}", action=action
    )


#: `GetSTS` 回包的字段名（大小写不保证；按小写键查）。
_STS_FIELD_NAMES = {
    "security_token": "SecurityToken",
    "access_key_id": "AccessKeyId",
    "access_key_secret": "AccessKeySecret",
}


def _parse_sts(action: str, payload: Any) -> OssStsCredentials:
    """`GetSTS` 载荷 → `OssStsCredentials`；缺字段只说字段名（⛔ 不回显已拿到的值）。"""
    if not isinstance(payload, dict):
        raise _protocol_error(action, "预期裸对象（临时凭证），拿到列表或 Result 信封")

    lowered = {str(key).lower(): value for key, value in payload.items()}
    values: dict[str, str] = {}
    for attr, wire_name in _STS_FIELD_NAMES.items():
        raw = lowered.get(wire_name.lower())
        if raw is None or not str(raw).strip():
            # ⛔ 只报缺哪个字段。
            raise _protocol_error(action, f"临时凭证缺少 {wire_name}")
        values[attr] = str(raw).strip()
    return OssStsCredentials(**values)


def _record_count(value: object) -> int | None:
    """解析 `RecordCount`；缺失 / 布尔 / 非整数 ⇒ `None`（⛔ 不拿 0 冒充「查过且为空」）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _list_page(action: str, result: Any) -> ListPage:
    """把列表信封（形态③）变成 `ListPage`；形状不对就响亮抛协议错。"""
    if not isinstance(result, dict):
        raise _protocol_error(action, "预期列表信封，拿到裸对象或 Result 信封")
    envelope_key = action
    if envelope_key not in result:
        raise _protocol_error(action, f"回包里没有 {envelope_key} 键")
    raw_rows = result[envelope_key]

    if isinstance(raw_rows, str):
        if raw_rows.strip() == "":
            # ⭐ 实测：空结果是**空字符串**，不是空数组。
            raw_rows = []
        else:
            # 兜底：整个列表值被再编码成 JSON 字符串的形态（§3 第 3 条末句）。
            try:
                raw_rows = json.loads(raw_rows)
            except json.JSONDecodeError as exc:
                raise _protocol_error(
                    action, f"{envelope_key} 是字符串但不是 JSON"
                ) from exc
            raw_rows = _decode_payload(raw_rows)

    if not isinstance(raw_rows, list):
        raise _protocol_error(action, f"{envelope_key} 既不是数组也不是空字符串")

    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            raise _protocol_error(action, f"{envelope_key} 的某一行不是对象")
        rows.append(item)
    return ListPage(rows=tuple(rows), record_count=_record_count(result.get("RecordCount")))
