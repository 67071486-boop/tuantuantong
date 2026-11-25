"""上家鸡蛋铺线（客服个人绑定）：**传图三步**（计划 Task 7；spec §6.3 / §2.4）。我方订单**已完成**时，把本单的订单截图**代替我方的截图**传到上家（A6），然后才允许提交完成（A5 的硬闸门：上家规则「提交完成前必须已上传过截图」）；台账 / `last_error` / 要不要提交完成由调用方（Task 8 的 outbox 消费者）按 `PicOutcome` 决定。三步形状**都是真机实测下来的，⛔ 不许走样**（完整表格外移 `docs/code-comment-notes.md`）：① `GET /CommonPermissionAjax/GetAliOssPolicy?bk=1&exsec=240`（走 SDK）取五键策略；② **直传阿里云 OSS**（multipart，字段顺序合同见 `OSS_FORM_FIELD_ORDER`，**`file` 必须最后**；`key = ${sid}-${orderId}-${rand5}-${epoch}.${ext}`，`epoch` 是**毫秒**时间戳且**逐张取时刻**，见 `build_object_key`）；③ `POST /OrderAjax/UploadOrderPic`（走 SDK）登记 `{orderId, remark, imgListStr:"<host>/<key>", gameAccId, taskId, imgType}`（`imgType` 逐张取，见 `IMG_TYPE_TO_UPLOAD`）。
⛔ 第②步**不是鸡蛋铺端点**（spec §13.1-R4，独立故障面：OSS 域名不在鸡蛋铺的凭据体系里、SDK 也**没有**上传器）⇒ 本模块自己用 `httpx` 发这一跳（`post_to_oss`），失败抛 **`OssUploadError`**——它与鸡蛋铺 SDK 的 `ApiError` / `JidanpuError` **不同族、不互相伪装**（判据见 `tests/test_jidanpu_upstream_pics.py` 的失败分档三条用例）；策略 / 桶 / 有效期变化让截图**整体失败**时应当响亮报错 + 台账 + 绑定 `last_error`（调用方落），⛔ 不许静默跳过。**拉回来**（B 段；代练员线复用同一端点）：`fetch_remote_pics` 读列表（认壳函数与 `remote_has_pic` 是同一个，⛔ 不是第二套判据）、`download_pic` 下载（与第②步同一条站姿：短命 `httpx.Client`、不带任何鸡蛋铺 cookie / 认证头，失败翻成 `PicDownloadError`）、`build_inbound_object_key` 用**确定性键**落库 ⇒ 同一张图第二轮**天然不再存一行**（`object_key` 唯一，⛔ 不加列 / 不加迁移）。
三个「不许硬编码」的值（`sid` / `gameAccId` / `taskId`）**全部**来自上传页隐藏域（`jidanpu_upstream.fetch_upload_page_fields`；实测那两个值就是 `0`，但 **`0` 也别写死**）；图种 / 大小闸门**复用既有服务端口径**（唯一出处 `apps.core.storage` 的 `ALLOWED_EXTS` / 18 MiB，⛔ 不写第二份定义；对标前端「先压 ≤5MB」是**对标前端的行为**，我方不做压缩）；短路：`ViewPic` 的 `msg.data` 非空 ⇒ 一张都不传（⛔ **`imgCount` 不是判据**——实测补图前后**恒为 0**，两个方向都不用它；短路可关 `skip_if_remote_has_pic=False`：代练员线要的是「我的图到他那儿」⇒ 对面已有图也照传）；失败口径由调用方定，本模块只如实分成两档返回——「**没图**」（`ViewPic` 正常回空，`PullOutcome` 为空）与「**没拉成**」（读 / 下载 / 落库失败，带 `reason`）⛔ 不许混。隐私（spec §6.3 / §11-18）：截图内容 / `accessid` / `signature` / 对象名 / 源 URL / **对端上传者的名字**一律不进人话文案与异常文本、⛔ 不进 `remark`，只在 `PicOutcome` / `PullOutcome` 的**结构化字段**里。"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import socket
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from jidanpu.client import JidanpuClient
from jidanpu.errors import ApiError, JidanpuError

from apps.business import jidanpu_upstream
from apps.business.media_access import order_shared_origin
from apps.business.models import OrderScreenshot
from apps.business.platform_egress import vendor_transport
from apps.core.storage import (
    ALLOWED_EXTS,
    MAX_IMAGE_BYTES,
    build_shared_order_key,
    get_storage,
    sniff_image_type,
)

# ── 端点常量：⛔ 本模块**不复制字面量**，唯一出处仍是 `jidanpu_upstream`（spec §2.4）──

logger = logging.getLogger(__name__)

VIEW_PIC_PATH = jidanpu_upstream.VIEW_PIC_PATH
UPLOAD_PIC_PATH = jidanpu_upstream.UPLOAD_PIC_PATH
OSS_POLICY_PATH = jidanpu_upstream.OSS_POLICY_PATH

#: 图种 / 大小闸门（spec §6.3）：**既有服务端口径**的别名，⛔ 不是第二份定义。
ALLOWED_IMAGE_EXTS = ALLOWED_EXTS
MAX_SCREENSHOT_BYTES = MAX_IMAGE_BYTES

#: 取策略的两个固定查询参数（spec §2.4 实测：`?bk=1&exsec=240`）。
OSS_POLICY_BK = "1"
OSS_POLICY_EXSEC = "240"

#: 策略 `data` 里**必须**齐全的五个键（缺一个就拼不出一次合法直传）。
REQUIRED_POLICY_FIELDS: tuple[str, ...] = ("accessid", "host", "policy", "signature", "expire")

#: multipart 的**字段顺序合同**（spec §2.4）：`file` **必须最后**（⛔ 后面还有字段会让 OSS 静默丢掉）。
OSS_FORM_FIELD_ORDER: tuple[str, ...] = (
    "key", "policy", "OSSAccessKeyId", "signature", "success_action_status",
)
FILE_FIELD_NAME = "file"

#: `imgType`：`0` = 订单截图、`1` = 群聊二维码（spec §2.4；对标上传页的 radio 原文：
#: `.tools/recon/media-storage/Order__UploadPic.html` 的 `value="0" title="订单截图"` 与 `value="1" title="群聊二维码"`，
#: 同一格值由他们的页面 JS 原样发进 `UploadOrderPic`，见 `assets_JS2019_Order_uploadpicnew.js` 的 `data: { …, imgType }`）。
UPLOAD_IMG_TYPE_ORDER = "0"
UPLOAD_IMG_TYPE_GROUP_QR = "1"

#: 我方图种 → 对标的 `imgType`（**逐张**判；⛔ 不 `.get(...)`：`img_type` 长出第三种取值时该**响亮**（`KeyError`），而不是静默把它当订单截图发过去）。
#: ⚠️ 上家线只选 `IMG_ORDER`（`choose_screenshots` 的默认口径）⇒ 它发出去的那一格**逐字仍是 `"0"`**，既有语义一个字都没变。
IMG_TYPE_TO_UPLOAD: dict[int, str] = {
    OrderScreenshot.IMG_ORDER: UPLOAD_IMG_TYPE_ORDER,
    OrderScreenshot.IMG_GROUP_QR: UPLOAD_IMG_TYPE_GROUP_QR,
}

#: `success_action_status` 的固定值（spec §2.4）：让 OSS 成功时回 **200** 而不是默认的 204。
OSS_SUCCESS_ACTION_STATUS = "200"

#: OSS 那一跳的超时（秒）。⛔ 与鸡蛋铺 SDK 的超时无关（两条不同的链路）。
OSS_TIMEOUT_SECONDS = 60

#: 扩展名 → 上传用的 `Content-Type`。⚠️ ⛔ **不读 `OrderScreenshot.content_type`**：那一列是**客户端可控**的（`apps/core/storage.py` 的 `sniff_image_type` 就是为这件事写的）⇒ 上传头按**扩展名**（已经过闸门）推，两个后端判据一致。
#: ⚠️ 取值一律用 `CONTENT_TYPE_BY_EXT[ext]`（**不是** `.get(...)`）：`ext` 已经在 `ALLOWED_IMAGE_EXTS` 闸门里，两处一旦不同步（白名单加了图种、这里忘了加映射）就该**响亮**（`KeyError`）而不是静默发一个 `application/octet-stream` 上去。
CONTENT_TYPE_BY_EXT: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}

# ── 人话文案（spec §6.3；⛔ 不放对象名 / 凭据）────────────────────────────────

ALREADY_HAD_REASON = "对面这张单已经有图了（只看有没有图、不看图种），不重复传"
#: ⚠️ 上面那句是**方向中立**的：上家线那边是「上家」，
#: 代练员线那边是「下家」——同一句话两处都要读得通，所以不写「上家」。
#: ⛔ 与「跳过」这件事本身无关：它只描述**为什么一张都没传**（`already_had=True`）。
NO_SCREENSHOT_REASON = "我方这张单没有可传的订单截图，未上传"
SHOT_EXT_REASON = "图片格式不是 jpg/jpeg/png，未上传"
SHOT_SIZE_REASON = "图片超过单张 18MB 上限，未上传"
SHOT_UNREADABLE_REASON = "读不到这张截图的字节（对象存储里没有这个对象），未上传"
UPLOAD_FIELDS_MISSING_REASON = (
    "取不到上家的店铺标识，截图功能会失败，请先联系上家确认（上传页缺少：{missing}）"
)
POLICY_FAILED_REASON = "取上传签名失败（第①步·鸡蛋铺）：{detail}"
OSS_FAILED_REASON = "直传阿里云 OSS 失败（第②步·这一跳不经鸡蛋铺，是独立故障面）：{detail}"
REGISTER_FAILED_REASON = "登记图片失败（第③步·鸡蛋铺）：{detail}"
PUSHED_REASON = "已上传到上家（第 {n} 张订单截图）"

#: per-pic 判重（`only_missing=True`，E-0282 / H1）：某一张**历史登记过的 oss_key 已在远端
#: 出现** ⇒ 那一张跳过、不重复上传（进 `PicOutcome.skipped` 的那一档人话；⛔ 不带对象名）。
PIC_ALREADY_THERE_REASON = "这张图对面那张单上已经有了（按历史推送登记判重），不重复传"
#: 全部已存在 ⇒ `pushed=()` 且 `failed` 空 ⇒ 调用方**不写台账行**；这句只落在
#: `PicOutcome.reason` 上，让「对面已有」与「我方没传」（`NO_SCREENSHOT_REASON`）分得开。
ALL_PICS_ALREADY_THERE_REASON = "这些图对面那张单上都有了（按历史推送登记判重），一张都没补传"

#: 失败文案里 `detail` 的截断上限（异常文本可能很长，而台账列是有限的）。
_DETAIL_MAX = 120

#: 失败发生在**哪一步**（`PicOutcome.failed[*].step` 的取值）。
STEP_READ = "read"
STEP_FIELDS = "fields"
STEP_POLICY = "policy"
STEP_OSS = "oss"
STEP_REGISTER = "register"
#: 拉图那一路的三步（`PullOutcome.failed[*].step` 的取值；⛔ 与上面那组不混）。
STEP_VIEW = "view"
STEP_DOWNLOAD = "download"
STEP_STORE = "store"

# ── 拉图（B 段）的固定值与闸门 ────────────────────────────────────────────────

#: 下载那一跳的超时**兜底**（秒）：⛔ 与鸡蛋铺 SDK / OSS 直传那两条链路的超时无关。
#: 唯一读取口是 `pic_timeout_seconds()`（设置 `JIDANPU_IMAGE_TIMEOUT` 可覆盖）。
DEFAULT_PIC_TIMEOUT_SECONDS = 60

#: 图片下载超时的设置键（`config/settings.py` 的同名设置）。
JIDANPU_IMAGE_TIMEOUT_SETTING = "JIDANPU_IMAGE_TIMEOUT"

#: **读图列表**（`ViewPic`）超时兜底（秒）与设置键（业务方拍板）。
#: 某租户线上那条 `读订单图失败（JidanpuTimeoutError）` 就是这一跳超时（SDK 默认 30 秒）。
DEFAULT_VIEWPIC_TIMEOUT_SECONDS = 60
JIDANPU_VIEWPIC_TIMEOUT_SETTING = "JIDANPU_VIEWPIC_TIMEOUT"
#: ViewPic 这一跳的**总尝试次数**（业务方口径：多给一次重试 ⇒ 2 次尝试；其它 API 不动）。⚠️ 这是「次数」不是「重试数」：SDK 的配置项是 `max_retries`（尝试数 = 它 + 1）⇒ 换给 `JidanpuConfig` 的值是 `VIEWPIC_MAX_ATTEMPTS - 1`。
#: ⚠️ 会话失效那一路（401/302 → 清会话、重登、`auth_retry=False` 重发一次）是**既有恢复路径**，不在这 2 次之内，⛔ 也不为它另调次数。
VIEWPIC_MAX_ATTEMPTS = 2


def _timeout_seconds_from(setting_key: str, default: int, label: str) -> int:
    """设置里的一档超时（秒）：缺失 / 非整数 / ≤ 0 ⇒ 兜底并记 warning。照 `apps/accounts/wallet.py::review_timeout_hours` 的**同族写法**：运行期 `getattr(settings, …)`（这样测试的 `override_settings` 才生效）、`int()` 解析、解析失败或 ≤ 0 ⇒ 回落兜底并 `logger.warning`——⛔ **不静默按 0**（0 会让那一跳每次都在本地立刻超时，症状是「什么都拉不回来」而日志只有一个超时）；两个调用方共用这一份判据（下载字节 / 读图列表），⛔ 别各写一份必然漂。"""
    raw = getattr(settings, setting_key, default)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r 不是整数，%s按默认 %s 秒处理", setting_key, raw, label, default,
        )
        return default
    if seconds <= 0:
        logger.warning(
            "%s=%r 不是正数，%s按默认 %s 秒处理", setting_key, raw, label, default,
        )
        return default
    return seconds


def pic_timeout_seconds() -> int:
    """**图片下载**那一跳的超时（秒）：设置缺失 / 非整数 / ≤ 0 ⇒ 兜底 60 并记 warning。业务方口径：鸡蛋铺 SDK 的 `JidanpuConfig.timeout`（30 秒，管 JSON API 调用）**不带**这一跳——从上家把图片字节取回来是另一条链路 ⇒ 单独一档、默认 **60** 秒。"""
    return _timeout_seconds_from(
        JIDANPU_IMAGE_TIMEOUT_SETTING,
        DEFAULT_PIC_TIMEOUT_SECONDS,
        "图片下载超时",
    )


def viewpic_timeout_seconds() -> int:
    """**读图列表**（`ViewPic`）那一跳的超时（秒）：缺失 / 非法 / ≤ 0 ⇒ 兜底 60 + warning。⚠️ 它**只**作用于 `ViewPic`（`fetch_remote_pics` / `remote_has_pic`），其它 JSON API 仍是 `JidanpuConfig.timeout`（30 秒）；实现见 `_viewpic_client`。"""
    return _timeout_seconds_from(
        JIDANPU_VIEWPIC_TIMEOUT_SETTING,
        DEFAULT_VIEWPIC_TIMEOUT_SECONDS,
        "读图列表（ViewPic）超时",
    )


class _ViewPicHttp:
    """把主客户端的 httpx 客户端包一层：这一层上的每个请求都带 ViewPic 那一档超时。为什么需要这一层（⛔ 不是为了绕开 SDK 的设计）：`src/jidanpu/` 是 **vendored** 的 SDK（`VENDORED.md`：⛔ 不改一行）而它**没有** per-call 超时 / 重试覆盖（`Transport.request` / `JidanpuClient.call` 都不收这两个参数）；本仓既有的口径是「要有界只能**按实例**给 `JidanpuConfig` 换值」（见 `jidanpu.open_client` 的 docstring）⇒ 这里用 SDK **公开**的 `http_client=` 注入点建一个**视图专用**客户端：底层仍是**同一个** `httpx.Client`（同一个连接池 / 同一份 cookie jar / 同一套事件钩子 ⇒ **不重登、不丢会话**），只有 ViewPic 那一发多带一个 per-request `timeout`（httpx 支持每请求覆盖）。
    ⚠️ **这个包装只挂给 ViewPic 专用客户端**（`_viewpic_client`）⇒ 包装内不按 URL 判断：它上面本来就只有 ViewPic 一种调用，按路径白名单反而多一处会漂的判据（将来 ViewPic 的路径写法变一下，超时就会**静默**退回 30）。
    ⚠️ 重试次数不在这里：它由 `_viewpic_client` 换 `JidanpuConfig.max_retries` 提供（SDK 的重试循环读的就是那一格）⇒ 退避 / 会话失效重登**仍走既有传输层**。"""

    def __init__(self, inner, *, timeout: int) -> None:
        self._inner = inner
        self._timeout = timeout

    @property
    def cookies(self):
        """cookie jar：`Transport` 用它做脱敏，**必须是同一份**（否则明文判据会漏）。"""
        return self._inner.cookies

    def request(self, method, url, **kwargs):
        """透传一次请求，但把 `timeout` 换成这一档。⚠️ 这里**不按 URL 判断**：这个包装只会被 `_viewpic_client` 拿去做 ViewPic 那一跳的专用客户端（其余调用仍走主客户端）；按 URL 白名单判断反而多一处会漂的判据——将来 ViewPic 的路径写法变一下，超时就会**静默**退回 30。"""
        kwargs["timeout"] = self._timeout
        return self._inner.request(method, url, **kwargs)

    def close(self) -> None:
        """⛔ 不关底层客户端：它是主客户端的那一份（`Transport._owns_client=False` 也不会调）。"""


def _viewpic_client(client):
    """给 ViewPic 这一跳一个**共享会话**的视图专用客户端；拿不到底层 HTTP 客户端 ⇒ 原样返回。共享 `auth`（主客户端的 `SessionAuth`：已登录时 `ensure_login()` **不会重登**）与 `registry`（⛔ 不重新解析 500 KB 清单）；`JidanpuConfig` 只换两格：`timeout` = 这一档、`max_retries` = 总尝试次数 - 1。
    ⚠️ `_tt_http` 是本仓建连处（`apps/business/jidanpu.py` 的两个落点）自己挂的引用，SDK 没有公开访问器；替身客户端（测试里的 stub）没有它 ⇒ **原样回退**，行为与接线前一致。"""
    http = getattr(client, "_tt_http", None)
    if http is None:
        return client
    timeout = viewpic_timeout_seconds()
    config = replace(
        client.config,
        timeout=timeout,
        max_retries=VIEWPIC_MAX_ATTEMPTS - 1,
    )
    return JidanpuClient(
        config,
        registry=client.registry,
        auth=client.auth,
        http_client=_ViewPicHttp(http, timeout=timeout),
    )


#: 源 URL 里「说明」那一段的分隔串（真机 `alt` 的形状：
#: `【机器人01】上传于10-4 21:53，说明：群二维码`；出处 = 计划 §0.2 的实测样本）。
ALT_NOTE_MARKER = "说明："
#: `alt` 的说明里出现它 ⇒ 这张是**群聊二维码**（否则是订单截图）——计划 §1-B 第 2 步的逐字口径。
GROUP_QR_HINT = "群"

#: 确定性对象名里用**源 URL 稳定身份的 sha256 前缀**（十六进制字符数；32 = 128 bit）。
KEY_DIGEST_CHARS = 32

# ── 拉图那一路的人话（⛔ 不含源 URL / 远程对象名 / 对端上传者名字）──────────────

PULL_UNREADABLE_REASON = "读下家订单图失败（{detail}），这一轮没拉图"
PULL_SHAPE_REASON = "下家回包里有 {n} 条图但一条都读不出地址，这一轮没拉图"
PULL_DOWNLOAD_REASON = "下家第 {index} 张图没下载下来（{detail}）"
PULL_EXT_REASON = "下家第 {index} 张图不是 jpg/jpeg/png，没存"
PULL_SIZE_REASON = "下家第 {index} 张图超过单张 18MB 上限，没存"
PULL_EMPTY_REASON = "下家第 {index} 张图是空文件，没存"
PULL_TYPE_REASON = "下家第 {index} 张图的字节不是图片，没存"
PULL_STORE_REASON = "下家第 {index} 张图存不进对象存储（{detail}）"
PULL_KEY_TAKEN_REASON = "下家第 {index} 张图算出来的对象名已被本租户另一张单占用，没存"

#: ⭐ 回环修复：这张图的**远端对象 key** 在「我们推给下家的台账」里
#: （`jidanpu_member.historical_pushed_pic_keys`）⇒ 它是我们推上去的，⛔ 不再拉回来。
#: ⚠️ 「推过的」不是「对面本来就有」：后者是机器人群码之类、不是我们传的。
PULL_PUSHED_OWN_REASON = "下家第 {index} 张图是我们自己推过的（对象 key 在推送台账里），不回收"
#: 图种白名单（`img_types`）挡下的一张：代练员线拉回**只收订单截图**（群码是我们自己推的）。
PULL_IMG_TYPE_SKIPPED_REASON = "下家第 {index} 张图不在本线回收的图种范围内，不回收"
#: 本单已经有 `ready` 群码 ⇒ 远端再来一张也不落第二张。
#: 业务依据（同口径的现成先例）：`apps/business/api/my_orders_views.py` 的
#: 「本单已经有群二维码了，不用再同步」——一张单只需要一张群码。
PULL_QR_ALREADY_EXISTS_REASON = "本单已有群二维码，这一张不再入库"
#: 订单截图已到 20 张上限（与认领端点 `MAX_ORDER_SCREENSHOTS` 同一条业务规则）。
PULL_ORDER_CAP_REASON = "本单订单截图已到 20 张上限，这一张不再入库"


class PicError(Exception):
    """本模块的失败基类（传图三步）。⚠️ ⛔ **刻意不继承** `jidanpu.errors.JidanpuError`：第②步是**独立故障面**（spec §13.1-R4），两档异常**不同族**才能让调用方分开处置、也让用例能把它们分开断言。"""


class OssUploadError(PicError):
    """**直传阿里云 OSS** 那一跳失败（第②步）。触发面：连不上 / 超时 / OSS 回非 200（策略过期、桶变了、签名被拒）；⚠️ 这一跳**不是鸡蛋铺端点**（SDK 没有上传器）⇒ 它失败**不等于**鸡蛋铺那边坏了。"""


class PicDownloadError(PicError):
    """**拉图**时下载那一跳失败（`download_pic`）。触发面：连不上 / 超时 / 源站回非 200 / 回了 0 字节；⚠️ 与 `OssUploadError` 同为「第三方主机、独立故障面」，但**方向相反**（那是我们传上去，这是我们取回来）——两档不同的类名，调用方与用例都能分开处置。"""


# ── 结果 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PushedShot:
    """一张成功传上去的截图。⚠️ `oss_key`（对象名）与 `object_key`（我方存储对象名）只在**结构化字段**里，⛔ 不进 `reason`（spec §6.3 隐私）。"""

    object_key: str
    oss_key: str
    reason: str
    #: 我方图种（`OrderScreenshot.IMG_ORDER` / `IMG_GROUP_QR`）。⚠️ 调用方要报「传上去的是哪几种」
    #: 就得有它；⛔ 它**不是**远程那个 `imgType`（那个在 `IMG_TYPE_TO_UPLOAD` 里逐张映射）。
    #: 默认值只为「既有构造点只有一处、且它一定给值」这一事实兜底，⛔ 不是「可以不传」。
    img_type: int = OrderScreenshot.IMG_ORDER


@dataclass(frozen=True)
class SkippedShot:
    """被闸门挡下、**没有**上传的一张（扩展名 / 大小）。"""

    object_key: str
    reason: str


@dataclass(frozen=True)
class FailedShot:
    """某一步失败。`step` ∈ `read` / `fields` / `policy` / `oss` / `register`；
    `error` 是**原始异常**（OSS 那一档是 `OssUploadError`，鸡蛋铺那两档是 `ApiError` / `JidanpuError`）。
    """

    object_key: str
    step: str
    reason: str
    error: Exception | None = None


@dataclass(frozen=True)
class PicOutcome:
    """一次「把本单截图传到上家」的结果。`already_had=True`：上家**已经有图** ⇒ 一张都没传（`ViewPic` 短路的唯一形态）；`pushed`：成功传上去的（可能为 0 / 多张）；`skipped`：被闸门挡下的（扩展名 / 大小）——**不是失败**，但也不会让上家多出图；`failed`：某一步失败（⛔ 一旦非空就**不许**提交完成，A5 的硬闸门）。"""

    pushed: tuple[PushedShot, ...] = ()
    already_had: bool = False
    skipped: tuple[SkippedShot, ...] = ()
    failed: tuple[FailedShot, ...] = ()
    reason: str = ""

    @property
    def blocks_completion(self) -> bool:
        """A5 闸门的**输入**（spec §6.2 第 ③ 步 / §6.3）：True ⇒ 调用方**不准**提交完成。判据两条（保守方向）：① 有 `failed`（任一步失败）；② **一张都没传成、上家也没有图**（含「全部被闸门 `skipped`」与「我方没有可传的图」）——上家规则是「提交完成前必须已上传过截图」，没图就提交一定被拒。"""
        if self.failed:
            return True
        return not self.already_had and not self.pushed


@dataclass(frozen=True)
class RemotePic:
    """下家那张单上的一条图（`ViewPic` 的 `msg.data[*]`）。⚠️ `src` 是**会过期的签名直链**（`accessid` / `signature` 就在查询串里）⇒ 它**只留在结构化字段**里，⛔ 不进人话台账、⛔ 不落库（落库的是我们自己算出来的对象名）；`alt` 里有**对端上传者的名字**与说明（真机形状见 `ALT_NOTE_MARKER` 的注释）⇒ 它只用来判图种（`img_type_of_alt`），⛔ 不写进 `remark`。回包里的 `pid` / `IsOpenView` / `thumb` 本线一个都不用，故不落字段。"""

    src: str
    alt: str = ""


@dataclass(frozen=True)
class PulledShot:
    """一张**新落库**的图（`object_key` 是我方对象名，⛔ 不是下家的）。"""

    object_key: str
    img_type: int


@dataclass(frozen=True)
class PullOutcome:
    """一次「把下家的图拉回来」的结果（B 段）。`pulled`：这一次**新落库**的行；`existing`：对象名已有行（**幂等**那一轮——`object_key` 是确定性键）；`skipped`：被闸门挡下的（扩展名 / 大小 / 不是图片 / 空文件）；`failed`：读列表 / 下载 / 落库失败（`step` ∈ `view` / `download` / `store`）；`reason`：`failed` 的**第一条**人话，**空串 = 这一轮没有失败**（⚠️ 「没图」也是空串 ⇒ 调用方据此把「没图」与「没拉成」分开，⛔ 不许合成一档）。
    ⚠️ 本结构**不决定要不要挡状态回写**——那是调用方的口径（代练员线是「任何失败都不挡」）。"""

    pulled: tuple[PulledShot, ...] = ()
    existing: tuple[str, ...] = ()
    skipped: tuple[SkippedShot, ...] = ()
    failed: tuple[FailedShot, ...] = ()
    reason: str = ""


# ── 取策略 / 判断已有图 / 登记 ────────────────────────────────────────────────


def _last_envelope(client) -> dict | None:
    """SDK 客户端最近一次响应的**原始业务壳**（脱敏副本）；拿不到回 `None`。"""
    envelope = getattr(client, "last_envelope", None)
    return envelope if isinstance(envelope, dict) else None


def _envelope_code(client) -> Any:
    """最近一次业务壳里的 `code`（壳缺失 ⇒ `None`）。"""
    envelope = _last_envelope(client)
    return envelope.get("code") if envelope is not None else None


def _is_write_success(code: Any) -> bool:
    """写端点成功判据：`code` 是**非 bool 的数字**且 `> 0`（spec §2.3 / GC 7 / E-0087）。⚠️ `bool` 显式排除：`True == 1` 会把畸形壳判成成功（SDK 的同一条口径）。"""
    return not isinstance(code, bool) and isinstance(code, (int, float)) and code > 0


def _envelope_has_pic(envelope: dict | None) -> bool:
    """`msg.data` 非空判据（spec §6.3 / §11-12）：**只看它**，⛔ **绝不用 `imgCount`**。⚠️ 真机形状：页面直接拿 `data.msg` 去喂 `layer.photos` ⇒ 载荷就在 `msg` 里，且 `msg` 是**对象**、图集在 `msg.data` 数组里。"""
    if not isinstance(envelope, dict):
        return False
    message = envelope.get("msg")
    if not isinstance(message, dict):
        return False
    pics = message.get("data")
    return isinstance(pics, list) and len(pics) > 0


def _is_view_pic_envelope(envelope: dict | None) -> bool:
    """这份壳是不是 **ViewPic 的业务壳**（spec §6.3）：`msg` 是**对象 / 数组**。真机「有图」的形状是 `{"code":0,"msg":{"title":"","data":[…]}}`——载荷就在 `msg` 里，而 SDK 的宽松规则对「`code==0` + `msg` 是非空对象 + 顶层没有 `data`/`count`/`total`」**会判失败** ⇒ 光靠 `except` 分不出「上家没有图（正常回包）」与「这次读真失败了」（两者在 SDK 眼里都是同一个 `ApiError`）；⇒ 认壳：`msg` 是 dict/list 就是**本端点的业务壳**（有没有图由 `_envelope_has_pic` 再判），拿不到壳（传输层 / HTTP / 会话失效）才重抛。
    ⚠️ **真机「没图」的确切形状仓里没有原始记录**（`.tools/recon/` 只留了「有图」那一份）⇒ 本判据**按形状**认壳、⛔ **不猜**一种空壳：`data` 是空数组 / 缺 `data` 键 / `msg` 空对象三种候选都落进「返回 `_envelope_has_pic(...)`」这一支，比钉死某一种更稳。
    ⇒ 这条「空图 = False」**尚未与真机对拍**（真机联调时要核一次）。"""
    if not isinstance(envelope, dict):
        return False
    return isinstance(envelope.get("msg"), (dict, list))


def remote_has_pic(client, *, remote_order_id: str) -> bool:
    """上家那张单**是否已有图**（spec §2.4 / §6.3）：`ViewPic` 的 `msg.data` 非空即已有。⚠️ **判据在「壳」上、⛔ 不在 SDK 的解包结果上**：SDK 成功时只交壳里的 `data` 键，而本端点的载荷在 `msg` 里 ⇒ 必须自己读 `client.last_envelope`；由此带出一个真实形态：SDK 的宽松规则对「`code==0` + `msg` 是非空对象 + 没有 `data`/`count`/`total` 键」**会判失败**，而这**恰恰是 ViewPic 的正常回包**——不分有图 / 没图，SDK 都会抛。⛔ 所以这里**不能**只在「有图」方向吞异常：只要 `last_envelope` 能认出是**本端点的业务壳**（`msg` 是对象 / 数组），就把「有没有图」交给 `_envelope_has_pic`（True / False **都返回**），⛔ **不重抛**；⛔ 只有**拿不到业务壳**（传输层 / HTTP / 会话失效）才原样上行——不知道有没有图就不能乱传。
    ⚠️ **这一跳有自己的一档超时 / 次数**（业务方拍板，见 `_viewpic_client`）：实际调用走视图专用客户端（60 秒 + 共 2 次尝试），但**会话 / 连接池是同一份**。"""
    caller = _viewpic_client(client)
    try:
        caller.call("GET", VIEW_PIC_PATH, query={"orderId": remote_order_id})
    except JidanpuError:
        envelope = _last_envelope(caller)
        if _is_view_pic_envelope(envelope):
            return _envelope_has_pic(envelope)
        raise
    return _envelope_has_pic(_last_envelope(caller))


def fetch_oss_policy(client) -> dict:
    """第①步：取阿里云 OSS 直传签名（spec §2.4 实测；**走 SDK**）。判据：`data` 是对象、且 `accessid` / `host` / `policy` / `signature` / `expire` 五个键**都非空**；缺任何一个 ⇒ 响亮抛 `ApiError`（fail-closed，⛔ 不发一次字段残缺的直传）；文案里**只说缺哪些键**，⛔ 不回显任何值（`policy` / `signature` 是凭据面）。"""
    payload = client.call(
        "GET", OSS_POLICY_PATH, query={"bk": OSS_POLICY_BK, "exsec": OSS_POLICY_EXSEC},
    )
    data = payload if isinstance(payload, dict) else None
    if data is None:
        envelope = _last_envelope(client)
        data = envelope.get("data") if envelope is not None else None
    missing = [
        name for name in REQUIRED_POLICY_FIELDS
        if not isinstance(data, dict) or not str(data.get(name) or "").strip()
    ]
    if not isinstance(data, dict) or missing:
        raise ApiError(
            f"{OSS_POLICY_PATH} 的 data 缺字段：{'、'.join(missing) or '（不是对象）'}",
            code=None,
        )
    return data


def register_pic(client, *, remote_order_id: str, host: str, key: str, remark: str,
                 game_acc_id: str, task_id: str,
                 img_type: str = UPLOAD_IMG_TYPE_ORDER) -> None:
    """第③步：把 `<host>/<key>` 登记到上家那张单上（spec §2.4 实测；**走 SDK**）。判据：**我们自己**判壳里的 `code > 0`（GC 7 / E-0087），⛔ 不靠 SDK 的宽松解包——`{"code":0,"msg":"…","data":[]}`（非空 `msg` + 空 `data`）会被宽松规则放行、**不抛异常**；`img_type` 默认 `"0"`（订单截图）= **上家线既有语义**（它的选图口径只有订单截图），代练员线按**逐张**的真实图种传（`IMG_TYPE_TO_UPLOAD`，群二维码 ⇒ `"1"`）。
    ⚠️ **为什么这里显式传 `success_rule="lenient"`**（评审实测的坑）：`UploadOrderPic` 在 SDK 的端点清单里是 `risk="write"` ⇒ SDK 默认按 `positive_code`（`code > 0`）判定，**它自己会在我们的判据之前抛 `ApiError`** ⇒ 下面那段 `_is_write_success` 就成了**不可达的装饰**（把 `_is_write_success` 改成 `return True`，46 条用例**全绿**）。显式传 `lenient` 把「成功语义」的**唯一出口**交回我们自己：两种规则的**合取**与 `positive_code` **逐形状等价**（`lenient` 放行的 `code<=0` 壳一律被下面这段拒掉；两条规则都由壳里的 `success` 字段优先，见 SDK 的 `transport._envelope_succeeded`）——所以这不是放宽，而是**让判据可达、可被用例证伪**。
    ⚠️ 文案里**只带业务码**，⛔ 不带 `signature` / 对象名（那些不进人话）。"""
    client.call("POST", UPLOAD_PIC_PATH, data={
        "orderId": remote_order_id,
        "remark": remark,
        "imgListStr": f"{host}/{key}",
        "gameAccId": game_acc_id,
        "taskId": task_id,
        "imgType": img_type,
    }, success_rule="lenient")
    code = _envelope_code(client)
    if not _is_write_success(code):
        raise ApiError(
            f"{UPLOAD_PIC_PATH} 的业务壳 code={code!r}（写端点成功码必须 > 0）：登记图片失败",
            code=code if not isinstance(code, bool) else None,
        )


# ── `key` 与 multipart ───────────────────────────────────────────────────────


def now_ms() -> int:
    """当前时刻的**毫秒级** Unix 时间戳（对标页面 `new Date().getTime()` 的形状；spec §2.4）。⚠️ 秒级不够：同一次 push 里的多张图若共用同一秒 ⇒ 5 位随机串撞一次就会**同名覆盖**（10 张图约 0.045%），后传的那张会把先传的那张从 OSS 覆盖掉、上家少一张。"""
    return int(timezone.now().timestamp() * 1000)


def build_object_key(*, sid: str, remote_order_id: str, ext: str,
                     rand5: str | None = None, epoch: int | None = None) -> str:
    """OSS 对象名 `${sid}-${orderId}-${rand5}-${epoch}.${ext}`（spec §2.4 的逐字形状）。`sid` 来自上传页隐藏域（⛔ 不硬编码）；`remote_order_id` = 上家 `orderId`（⛔ **不是** `orderIdView`，B4）；`rand5` = **5 位十进制随机串**（对标页面 `(Math.random()+"").substring(2, 7)` 的形状；⛔ 不用十六进制——那会带字母，与实测形状不同），这里用 `secrets` 取、避免对象名可猜；`epoch` = **毫秒级** Unix 时间戳（对标页面 `new Date().getTime()`；⛔ **不是秒**），不给就取**当前时刻**（`now_ms()`）。
    ⚠️ `rand5` / `epoch` 可显式传入只为**可复现的测试**；真实调用一律用默认值。"""
    random_part = rand5 if rand5 is not None else f"{secrets.randbelow(100000):05d}"
    stamp = epoch if epoch is not None else now_ms()
    return f"{sid}-{remote_order_id}-{random_part}-{stamp}.{ext.lower()}"


def _policy_value(fields: Mapping[str, Any], name: str) -> str:
    """策略字段取值；缺 / 空 ⇒ `OssUploadError`（fail-closed，⛔ 不发残缺的 multipart）。"""
    value = fields.get(name)
    if value is None or not str(value).strip():
        raise OssUploadError(f"阿里云 OSS 直传缺少策略字段：{name}")
    return str(value)


def build_oss_multipart(*, key: str, fields: Mapping[str, Any], data: bytes,
                        content_type: str = "application/octet-stream",
                        filename: str = FILE_FIELD_NAME) -> tuple[str, bytes]:
    """把一次 OSS 直传编成 `(Content-Type 头, body 字节)`（**纯函数，不过网**）。⭐ **字段顺序是合同**（spec §2.4）：`key` / `policy` / `OSSAccessKeyId` / `signature` / `success_action_status` / `file`，**`file` 必须最后**（`file` 之后还有字段会让 OSS 静默丢掉它们）；⚠️ `accessid` 在表单里的名字是 `OSSAccessKeyId`（大小写逐字照阿里云）。
    ⚠️ 刻意自己编字节而不是让 `httpx` 从 `data=`/`files=` 推：那样字段顺序由库决定，而**顺序本身是要被断言的结构**（`tests/test_jidanpu_upstream_pics.py` 直接断字节）。"""
    boundary = uuid.uuid4().hex
    form: tuple[tuple[str, str], ...] = (
        ("key", key),
        ("policy", _policy_value(fields, "policy")),
        ("OSSAccessKeyId", _policy_value(fields, "accessid")),
        ("signature", _policy_value(fields, "signature")),
        ("success_action_status", OSS_SUCCESS_ACTION_STATUS),
    )
    out = bytearray()
    for name, value in form:
        out += _text_part(boundary, name, value)
    out += _file_part(boundary, filename, data, content_type)
    out += b"--" + boundary.encode() + b"--\r\n"
    return f"multipart/form-data; boundary={boundary}", bytes(out)


def _text_part(boundary: str, name: str, value: str) -> bytes:
    return (
        b"--" + boundary.encode() + b"\r\n"
        + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        + value.encode() + b"\r\n"
    )


def _file_part(boundary: str, filename: str, data: bytes, content_type: str) -> bytes:
    return (
        b"--" + boundary.encode() + b"\r\n"
        + f'Content-Disposition: form-data; name="{FILE_FIELD_NAME}"; filename="{filename}"\r\n'.encode()
        + f"Content-Type: {content_type}\r\n\r\n".encode()
        + data + b"\r\n"
    )


def post_to_oss(host: str, fields: Mapping[str, Any], key: str, data: bytes,
                content_type: str, *, filename: str = FILE_FIELD_NAME) -> None:
    """第②步**默认实现**：把这次直传发给阿里云 OSS（spec §2.4）。⚠️ ⛔ **这一跳不是鸡蛋铺端点**（SDK 没有 OSS 上传器）⇒ 自己建一个**短命**的 `httpx.Client`：它**不带任何 cookie / 认证头**，⛔ 绝不把鸡蛋铺的会话带去第三方主机；失败一律翻成 `OssUploadError`（独立故障面）：连不上 / 超时 ⇒ `httpx.HTTPError`，OSS 回非 200（策略过期 / 签名被拒 / 桶变了）⇒ 同样；成功判定就是 `success_action_status=200` 那一格（spec §2.4）⇒ 只认 HTTP 200。
    ⚠️ 异常文案只带状态码 / 异常类名，**不带**响应体（里面可能有桶 / 签名信息）；`oss_post` 是**可注入**的（`push_order_screenshots` 的 `oss_post=`）：契约与它逐字相同——`(host, fields, key, data, content_type) -> None`，失败抛 `OssUploadError`。"""
    content_type_header, body = build_oss_multipart(
        key=key, fields=fields, data=data, content_type=content_type, filename=filename,
    )
    try:
        with httpx.Client(
            timeout=OSS_TIMEOUT_SECONDS, follow_redirects=False, transport=vendor_transport(host),
        ) as client:
            response = client.post(
                host, content=body, headers={"Content-Type": content_type_header},
            )
    except httpx.HTTPError as exc:
        raise OssUploadError(f"连接阿里云 OSS 失败（{type(exc).__name__}）") from exc
    if response.status_code != 200:
        raise OssUploadError(f"阿里云 OSS 返回 HTTP {response.status_code}")


#: 注入替身的签名（照 `post_to_oss`；方便调用方与本模块的注解对齐）。
OssPost = Callable[..., None]


# ── 选图 ─────────────────────────────────────────────────────────────────────


#: 默认要传的图种：**上家线的既有口径**（只订单截图，⛔ 不含群聊二维码）。
DEFAULT_PUSH_IMG_TYPES: tuple[int, ...] = (OrderScreenshot.IMG_ORDER,)


def choose_screenshots(order, *, img_types: tuple[int, ...] = DEFAULT_PUSH_IMG_TYPES
                       ) -> tuple[OrderScreenshot, ...]:
    """本单要传的截图：`img_type ∈ img_types` 且 `status = ready`，**按 `pk` 最早在前**。⚠️ 默认 `img_types` = **订单截图**（照 `peer_dispatch._enqueue_order_screenshots` 的同一口径，spec §6.3）：⛔ 默认不含群聊二维码（`img_type=1`）、⛔ 不含未认领（`pending`）的行；代练员线显式传 `(IMG_ORDER, IMG_GROUP_QR)`（业务方口径②「全传」）——**参数化而不是分叉**：选图口径仍然只有这一处。"""
    return tuple(
        OrderScreenshot.objects.filter(
            order=order,
            img_type__in=img_types,
            status=OrderScreenshot.STATUS_READY,
        ).order_by("pk")
    )


# ── 主入口 ───────────────────────────────────────────────────────────────────


def _ext_of(object_key: str) -> str:
    """对象名的扩展名（小写）；没有 `.` ⇒ 空串（会落进「图种不对」那一档）。"""
    _stem, dot, ext = object_key.rpartition(".")
    return ext.lower() if dot else ""


def _brief(exc: Exception) -> str:
    """异常文本的**短**版本（台账 / 人话用）。⛔ 不原样回显长文本。"""
    text = str(exc).strip() or type(exc).__name__
    return text[:_DETAIL_MAX]


def _failure(shot, *, index: int, step: str, template: str, error: Exception | None,
             detail: str = "") -> FailedShot:
    """拼一条失败：**点名是哪一步、哪一张**（spec §6.3）。⚠️ 「哪一张」用**本地 `pk`** 指认：对象名（`object_key` / OSS `key`）⛔ **不进文案**（spec §6.3 隐私）；`object_key` 仍留在结构化字段里，供调用方落本地台账内部字段。"""
    base = template.format(detail=detail) if "{detail}" in template else template
    return FailedShot(
        object_key=shot.object_key,
        step=step,
        reason=f"{base}（第 {index} 张，截图 id={shot.pk}）",
        error=error,
    )


def push_order_screenshots(client, *, order, binding, link, http, now: datetime,
                           oss_post: OssPost | None = None,
                           remote_order_id: str | None = None,
                           img_types: tuple[int, ...] = DEFAULT_PUSH_IMG_TYPES,
                           skip_if_remote_has_pic: bool = True,
                           only_missing: bool = False,
                           on_pushed: Callable[[PushedShot], None] | None = None) -> PicOutcome:
    """把 `order` 的订单截图传到上家（三步；spec §6.3；计划 Task 7 的接口）。`binding` / `link` / `http` 由调用方按真实形态给（`jidanpu.open_client_with_http(binding)` 产出 `client` + `http`，`link` 是本单的本线链路、`link.upstream_order_id` = 上家 `orderId`）；⚠️ `binding` 本函数**不写**（台账 / `last_error` 是调用方 Task 8 的落点——留着它是为了让入口签名与计划的契约一致）；`now` 是本次 push 的**时刻锚点**（注入 ⇒ key 的基准时刻可复现），`key` 的毫秒时间戳在它之上**逐张**叠加真实流逝的毫秒；`oss_post` 是**第②步的可注入替身**（签名照 `post_to_oss`）；⚠️ **失败即停**（fail-fast）：一旦某张失败就返回，`failed` 非空 ⇒ 调用方**不许**提交完成；第①步 / 第③步的鸡蛋铺异常（`ApiError` / `JidanpuError`）会被翻成 `failed` 条目，**预期之外**的异常（比如注入替身自己写错）**原样上行**——那不该被伪装成业务结果（E-0010）；`fetch_upload_page_fields` 抛的 **`ApiError`** 是**可预期的业务失败**（spec §9.2 第 4 项点名的已知形态：上传页 302 到**无权限页**）⇒ 收成 `step="fields"` 的失败 + 不带凭据的人话，⛔ 不冒泡成异常（同一步「隐藏域缺值」走**同一档**，调用方只处理一套错误面），**基础设施类失败**（`TransportError` / `ServerError` / `SessionExpiredError`）**仍原样响亮上行**——那是「检测本身失败」，说成「上家没给店铺标识」会让人去改一个没坏的权限设置（E-0011）。
    ## 三个显式参数（代练员线引入；⭐ D-0367 起**上家线也用**）：`remote_order_id`（**不给**时按上家线老口径取 `link.upstream_order_id`，既有调用点逐字不变；代练员线的链路列叫 `link.jidanpu_order_id` ⇒ 它**显式传**）；`img_types`（默认只订单截图 = 上家线口径，代练员线传两种「全传」）；`skip_if_remote_has_pic`（**对面已经有图时还传不传**：⭐ D-0367 业务方拍板「上家有图也需要传」⇒ **起上家线改传 `False`**——竹屿/团团工作室等上家**每单创建即自带一张参考图**，`True` 会让 A 段**一张都不传且毫无痕迹**（E-0314 同族实测：竹屿 3/3、团团工作室 4/4 样本，上家单上只有它自家那张图；修复轮的对抗性探针读数同形：`ViewPic_calls=1 / UploadOrderPic_calls=0 / 台账只有一句 done`）；代练员线起就传 `False`：业务方要的是「**我的图到他那儿**」；⚠️ 裸传 `False`（不带 `only_missing`）时**一次 `ViewPic` 都不打**——那一跳只服务这个短路判断）。
    `only_missing`（E-0282 / H1）= **逐张判缺**——对面已有图不再整档短路，改为只补缺的那几张。判据三步（⭐ L2：`fetch_remote_pics` **恰好一次** `ViewPic`，⛔ 不再调 `remote_has_pic`、两次读取合一次）：① 远端对象键集合 = `fetch_remote_pics` 的 `src` 逐条过 `remote_object_key`（按**对象 key**、⛔ 不是整条 URL——同一张图会同时出现在两个域名上）；② 该链路历史台账 payload 里登记过的 `{本地 object_key → 对面 oss_key}` 映射（唯一出处 `jidanpu_member.historical_pushed_pic_keys`；本地键是 sha256 入站键、对面 oss_key 是推送时**随机现生成**的——两套**永不相交的值空间**，唯一的桥就是推送成功时写进台账的那份映射）；③ 本地 ready 图中「其历史 oss_key 已在远端出现」的跳过（进 `skipped`、人话 `PIC_ALREADY_THERE_REASON`），**无历史映射的图照传**（缺口期口径：本参数上线前推成功的图没有映射 ⇒ 第一次补推会重传一次，影响面有界、不炸）。⚠️ 全部已存在 ⇒ `pushed=()` 且 `failed` 空（`reason=ALL_PICS_ALREADY_THERE_REASON`）⇒ 调用方**不写台账行**；`skip_if_remote_has_pic` 在 `only_missing=True` 时不再生效（整档短路被 per-pic 判缺取代，也**不会**多打第二次 `ViewPic`）。"""
    if remote_order_id is None:
        remote_order_id = link.upstream_order_id

    shots = choose_screenshots(order, img_types=img_types)

    #: per-pic 判缺（`only_missing=True`，E-0282 / H1）：见 docstring「三个显式参数」那节。
    #: ⭐ 键形状两侧异形（M6）：本地 `object_key` 是 sha256 入站键、对面 oss_key 是推送时随机现生成的 `${sid}-…` 键——**只有台账映射能把它们对上**，⛔ 不存在「键同名命中」那种恒真判据；`remote_object_key` 只做「按对象 key 提取」（丢弃域名与查询串）。
    if only_missing:
        if not shots:
            return PicOutcome(reason=NO_SCREENSHOT_REASON)
        # 函数内 import（环，照 `_push_screenshots_of_line` 那处的同一处置）：
        # `jidanpu_member` 在模块级 import `jidanpu_upstream`，而后者又 import 本模块的
        # 上游；kind / payload 键的唯一出处都在 `jidanpu_member` ⇒ 只能在函数内 import。
        from apps.business import jidanpu_member

        # ① 恰好一次 ViewPic（L2 硬判据）：fetch_remote_pics 一把拿远端对象键集合。
        remote_pics = fetch_remote_pics(client, remote_order_id=remote_order_id)
        remote_keys = {remote_object_key(pic.src) for pic in remote_pics.items}
        # ② 该链路历史台账登记过的 {本地 object_key → {oss_key…}}。
        historical = jidanpu_member.historical_pushed_pic_keys(order, binding)
        # ③ 「其历史 oss_key 已在远端出现」的跳过（无映射的照传 = 缺口期口径）。
        missing: list[OrderScreenshot] = []
        deduped: list[SkippedShot] = []
        for shot in shots:
            if (historical.get(shot.object_key) or set()) & remote_keys:
                deduped.append(SkippedShot(shot.object_key, PIC_ALREADY_THERE_REASON))
                continue
            missing.append(shot)
        if not missing:
            # 全部已存在：`pushed=()` 且 `failed` 空 ⇒ 调用方不写台账行（见 docstring）。
            # `already_had=True`：对面这张单**确实**有我方的图（A5 闸门按「已有图」放行）。
            return PicOutcome(
                already_had=True, skipped=tuple(deduped),
                reason=ALL_PICS_ALREADY_THERE_REASON,
            )
        shots = tuple(missing)
        dedup_skipped = tuple(deduped)
    else:
        dedup_skipped = ()
        if skip_if_remote_has_pic and remote_has_pic(client, remote_order_id=remote_order_id):
            return PicOutcome(already_had=True, reason=ALREADY_HAD_REASON)

    if not shots:
        return PicOutcome(reason=NO_SCREENSHOT_REASON)

    try:
        fields = jidanpu_upstream.fetch_upload_page_fields(http, remote_order_id=remote_order_id)
    except ApiError as exc:
        # ⭐ 可预期的业务失败（`fetch_upload_page_fields` 里**唯一**的 ApiError = 302 到无权限页）
        # ⇒ 与「隐藏域缺值」合成同一档 `step="fields"`，⛔ 不让同一步有两种错误面。
        reason = jidanpu_upstream.UPLOAD_FIELDS_UNREADABLE
        return PicOutcome(
            failed=(FailedShot(object_key="", step=STEP_FIELDS, reason=reason, error=exc),),
            reason=reason,
        )
    missing = [
        name for name in jidanpu_upstream.REQUIRED_UPLOAD_FIELDS
        if not str(fields.get(name) or "").strip()
    ]
    if missing:
        return PicOutcome(
            failed=(FailedShot(
                object_key="", step=STEP_FIELDS,
                reason=UPLOAD_FIELDS_MISSING_REASON.format(missing="、".join(missing)),
            ),),
            reason=UPLOAD_FIELDS_MISSING_REASON.format(missing="、".join(missing)),
        )
    sid = fields["sid"]
    game_acc_id = fields["gameAccId"]
    task_id = fields["taskId"]

    poster = oss_post or post_to_oss
    pushed: list[PushedShot] = []
    skipped: list[SkippedShot] = []
    failed: list[FailedShot] = []
    # `key` 的毫秒时间戳以 `now` 为锚点、**逐张**叠加真实流逝的毫秒：
    # 一批图里每张都有自己的时刻 ⇒ 不会因为「同一秒 + 随机串相撞」而互相覆盖。
    started = time.monotonic()

    for index, shot in enumerate(shots, start=1):
        ext = _ext_of(shot.object_key)
        if ext not in ALLOWED_IMAGE_EXTS:
            skipped.append(SkippedShot(shot.object_key, SHOT_EXT_REASON))
            continue
        try:
            with get_storage().open_stream(shot.object_key) as handle:
                data = handle.read()
        except (OSError, ValueError) as exc:
            failed.append(_failure(
                shot, index=index, step=STEP_READ, template=SHOT_UNREADABLE_REASON,
                error=exc, detail=_brief(exc),
            ))
            break
        if len(data) > MAX_SCREENSHOT_BYTES:
            skipped.append(SkippedShot(shot.object_key, SHOT_SIZE_REASON))
            continue

        key = build_object_key(
            sid=sid, remote_order_id=remote_order_id, ext=ext,
            epoch=int(now.timestamp() * 1000) + int((time.monotonic() - started) * 1000),
        )
        step = STEP_POLICY
        try:
            policy = fetch_oss_policy(client)
            step = STEP_OSS
            poster(
                policy["host"], policy, key, data, CONTENT_TYPE_BY_EXT[ext],
            )
            step = STEP_REGISTER
            register_pic(
                client, remote_order_id=remote_order_id, host=policy["host"], key=key,
                remark=shot.remark, game_acc_id=game_acc_id, task_id=task_id,
                img_type=IMG_TYPE_TO_UPLOAD[shot.img_type],
            )
        except OssUploadError as exc:
            # ⭐ 独立故障面（spec §13.1-R4）：⛔ 不许伪装成鸡蛋铺那一档。
            failed.append(_failure(
                shot, index=index, step=STEP_OSS, template=OSS_FAILED_REASON,
                error=exc, detail=_brief(exc),
            ))
            break
        except JidanpuError as exc:
            template = POLICY_FAILED_REASON if step == STEP_POLICY else REGISTER_FAILED_REASON
            failed.append(_failure(
                shot, index=index, step=step, template=template,
                error=exc, detail=_brief(exc),
            ))
            break
        registered = PushedShot(
            object_key=shot.object_key, oss_key=key,
            reason=PUSHED_REASON.format(n=index), img_type=shot.img_type,
        )
        pushed.append(registered)
        if on_pushed is not None:
            on_pushed(registered)

    return PicOutcome(
        # per-pic 判重跳过的那几张（`only_missing`）也进 `skipped`（它们**不是失败**，
        # 与扩展名 / 大小闸门同一档；判重那一句人话里⛔ 不带对象名）。
        pushed=tuple(pushed), skipped=tuple(dedup_skipped) + tuple(skipped),
        failed=tuple(failed),
        reason=failed[0].reason if failed else "",
    )


# ── 拉回来（B 段：下家 → 我方；代练员线 `jidanpu_member` 复用）────────────────────
#    ⛔ 与上面「传下去」共用同一个端点的判据（`_is_view_pic_envelope`），不是第二套实现。


@dataclass(frozen=True)
class RemotePics:
    """`ViewPic` 的一整份回包：解析出来的图 + `msg.data` 里的**原始条数**。⚠️ 两个字段缺一不可：只有 `items` 的话，「回包里有 3 条但一条都读不出地址」（对端改了字段名 / 换了形状）会与「一张图都没有」同形——那是**静默空**（E-0004 的同族）；`rows > 0 而 items 为空` ⇒ 响亮记一条人话。"""

    items: tuple[RemotePic, ...] = ()
    rows: int = 0

    def __bool__(self) -> bool:
        """「下家这张单有没有图」= `items` 非空（⛔ 只看 `rows`：读不出地址的不算图）。"""
        return bool(self.items)


#: 下载那一跳的替身签名（照 `download_pic`）：`(src) -> bytes`，失败抛 `PicDownloadError`。
PicGetter = Callable[[str], bytes]


def _pic_src(item: Any) -> str:
    """一条 `msg.data[*]` 里的图片直链；形状不对 / 空 ⇒ 空串（调用方按「读不出地址」处置）。"""
    if not isinstance(item, Mapping):
        return ""
    src = item.get("src")
    return src.strip() if isinstance(src, str) else ""


def _remote_pics(envelope: dict | None) -> RemotePics:
    """把 `ViewPic` 的业务壳解成 `RemotePics`（载荷在 `msg.data`，spec §0.2 实测）。⚠️ 只认 `msg` 是对象、`msg.data` 是数组；其余形状 ⇒ 空的 `RemotePics`（壳都在 `_is_view_pic_envelope` 那一关判过了）。"""
    message = envelope.get("msg") if isinstance(envelope, dict) else None
    if not isinstance(message, Mapping):
        return RemotePics()
    rows = message.get("data")
    if not isinstance(rows, list):
        return RemotePics()
    items: list[RemotePic] = []
    for item in rows:
        src = _pic_src(item)
        if not src:
            continue
        alt = item.get("alt") if isinstance(item, Mapping) else ""
        items.append(RemotePic(src=src, alt=alt if isinstance(alt, str) else ""))
    return RemotePics(items=tuple(items), rows=len(rows))


def fetch_remote_pics(client, *, remote_order_id: str) -> RemotePics:
    """下家那张单上的图（`ViewPic` 的 `msg.data`），**两种信封形态都认**。⚠️ 与 `remote_has_pic` **同一个判据、同一个入口形状**（照它写，⛔ 不另造一套）：SDK 的成功判据在这个未登记端点上**很怪**——真机「有图」与「没图」两种回包都会让 `client.call` 抛 `ApiError`（`code == 0` + 载荷在 `msg` 里、顶层没有 `data`/`count`/`total` 时宽松规则判失败）⇒ 只要 `last_envelope` 能认出是**本端点的业务壳**（`msg` 是对象 / 数组）就当**正常回包**解包；只有**拿不到业务壳**（传输层 / HTTP / 会话失效）才原样上诉；⛔ 「读失败」与「没有图」由此**分得开**：前者抛、后者回空的 `RemotePics`（代练员线那一条「拉图失败不挡待验收」的台账话术就靠这个区分）。
    ⚠️ **这一跳有自己的一档超时 / 次数**（业务方拍板）：走 `_viewpic_client`（60 秒 + 共 2 次尝试），会话 / 连接池与主客户端**同一份**；别在别处重写一次这个调用，否则「只影响 ViewPic」这条判据会静默漂掉（`tests` 有两条用例钉住）。"""
    caller = _viewpic_client(client)
    try:
        caller.call("GET", VIEW_PIC_PATH, query={"orderId": remote_order_id})
    except JidanpuError:
        envelope = _last_envelope(caller)
        if _is_view_pic_envelope(envelope):
            return _remote_pics(envelope)
        raise
    return _remote_pics(_last_envelope(caller))


def _pull_detail(exc: Exception) -> str:
    """失败人话里的那一截 detail（⛔ 不带 URL / 凭据 / 远程对象名）。⚠️ 为什么不一律 `_brief(exc)`：下载那一跳的 `httpx` 异常文本**可能带上完整 URL**（查询串里就是 `accessid` / `signature`）⇒ 隐私口径（模块顶部）要求它⛔ 不进人话；`PicDownloadError` 的文案是本模块自己拼的（只有状态码 / 异常类名）⇒ 只有它是安全的。"""
    if isinstance(exc, PicDownloadError):
        return _brief(exc)
    return type(exc).__name__


#: 下载那一跳的**响应上限**：单张图最多 20 MiB（流式累计，⛔ 不信 `Content-Length`）。
#: 比服务端既有的 18 MiB 图片闸（`MAX_IMAGE_BYTES`）宽一点：这一档是**传输层的界**，
#: 落库前仍要过 18 MiB 那条业务闸 ⇒ 超 20 MiB 直接判失败，别把 1 GB 拉进内存。
MAX_PIC_BYTES = 20 * 1024 * 1024
#: 单次流式读的块大小（64 KiB，与回调接收器同一档）。
PIC_READ_CHUNK = 64 * 1024
#: 重定向**最多 3 跳**（第 4 个 3xx 直接判失败）。每一跳都必须重新过 URL 闸。
MAX_PIC_REDIRECTS = 3
#: 只允许的 scheme（⛔ `file:` / `ftp:` / `gopher:` 一律拒）。
ALLOWED_PIC_SCHEMES = ("http", "https")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    """把一个主机名解析成**全部**地址（测试注入点：用例替换它，⛔ 不打真网络）。回的是 `(地址字符串, …)`；解析不出 ⇒ 空元组（调用方按失败处置）。"""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(info[4][0]) for info in infos}))


def _assert_safe_pic_url(src: str, *, resolver: Callable[[str, int], Iterable[str]]) -> str:
    """图片直链的 SSRF 闸（H-2，审计批）：不通过 ⇒ `PicDownloadError`。判据（四条，缺一条就是一条可用的内网打点）：① scheme 只允许 `http` / `https`；② **不许带用户名 / 密码**（`http://user:pass@host/` 是另一类绕过：有些客户端会把凭据当主机解析、或把凭据带进下一跳）；③ 主机必须解析得出至少一个地址；④ **每一个**解析出的地址都必须是公网可路由：`ipaddress.is_global` 为假的一律拒（环回 / 私有 / 链路本地 / 保留段 / 未指定 / CGNAT `100.64/10` 全在这条里），**组播**单独拒（`224.0.0.0/4` 在 Python 里 `is_global` 为真，实测）。
    ⚠️ **逐地址**判而不是「有一个公网就放行」：域名同时解析出公网 + 内网（DNS 污染 / 内网 DNS 视图）时，客户端连哪一个不由我们决定 ⇒ fail-closed；文案只写人话，⛔ **不带 URL / 主机名 / 地址**（图片直链的查询串里是签名）。
    ⚠️ **已知残差（如实登记）**：这里解析一次、真正连接时 httpx 会**再解析一次**（DNS rebinding 的 TOCTOU 窗口）。当前实现只要求「解析后逐地址拒绝」，没有把连接钉死到已校验的那一个 IP 上（那要动传输层 / 自管连接池）；窗口存在但比原状（完全不判）小得多。"""
    try:
        parsed = urlsplit(src)
    except ValueError:
        raise PicDownloadError("下家图片地址形状不合法") from None
    if parsed.scheme.lower() not in ALLOWED_PIC_SCHEMES:
        raise PicDownloadError("下家图片地址只支持 http/https")
    host = (parsed.hostname or "").strip().rstrip(".")
    if not host:
        raise PicDownloadError("下家图片地址没有主机名")
    if parsed.username is not None or parsed.password is not None:
        raise PicDownloadError("下家图片地址不允许带用户名或密码")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError:
        raise PicDownloadError("下家图片地址的端口不合法") from None
    # ⭐ 主机是 **IP 字面量** ⇒ 直接判它，⛔ 不去查 DNS（也不该让 resolver 的答案盖掉字面量：
    #    对端写 `http://169.254.169.254/` 时，查不查 DNS 它都是那个地址）。
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            addresses = tuple(resolver(host, port))
        except OSError as exc:
            raise PicDownloadError(f"解析下家图片地址失败（{type(exc).__name__}）") from exc
        if not addresses:
            raise PicDownloadError("解析下家图片地址失败（没有任何地址）")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(str(raw))
        except ValueError:
            raise PicDownloadError("下家图片地址解析出了不认识的地址") from None
        if address.is_multicast or not address.is_global:
            raise PicDownloadError("下家图片地址指向内网 / 本机 / 保留地址")
    return src


@dataclass
class _Fetched:
    """一次**流式**取回的响应（`download_pic` 只认这四样，⛔ 不认 httpx 的完整响应对象）。"""

    status: int
    headers: Mapping[str, str]
    chunks: Iterator[bytes]
    close: Callable[[], None]


def _fetch_pic(url: str, *, timeout: float) -> _Fetched:
    """真实下载口（测试注入点：用例替换它，⛔ 打真网络）。⚠️ `follow_redirects=False`：重定向由 `download_pic` **自己逐跳走**，每一跳重新过 SSRF 闸（对端可以用 302 把请求带去内网地址；交给 httpx 自动跟就是绕过闸门）；不带任何 cookie / 认证头（与 `post_to_oss` 同一条站姿）。
    ⚠️ `trust_env=False`：⛔ 不读进程环境里的 `HTTP(S)_PROXY`——走代理等于把「按 URL 逐跳过 SSRF 闸」换成代理侧自己解析（闸门被绕过）；也⛔ 不吃 `.netrc` 里的凭据（这个下载口不该带任何认证）。"""
    client = httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)
    try:
        request = client.build_request("GET", url)
        response = client.send(request, stream=True)
    except BaseException:
        client.close()
        raise

    def _close() -> None:
        try:
            response.close()
        finally:
            client.close()

    return _Fetched(
        status=response.status_code,
        headers=response.headers,
        chunks=response.iter_bytes(),
        close=_close,
    )


def _read_pic_capped(fetched: _Fetched) -> bytes:
    """把流式响应读进内存，**超 20 MiB 立即失败**（⛔ 不先读完再看大小）。"""
    kept = bytearray()
    try:
        for chunk in fetched.chunks:
            if not chunk:
                continue
            if len(kept) + len(chunk) > MAX_PIC_BYTES:
                raise PicDownloadError(
                    f"下家图片超过 {MAX_PIC_BYTES // (1024 * 1024)} MiB 上限"
                )
            kept.extend(chunk)
    except httpx.HTTPError as exc:
        raise PicDownloadError(f"连接下家图片地址失败（{type(exc).__name__}）") from exc
    if not kept:
        raise PicDownloadError("下家图片地址回了一个 0 字节的文件")
    return bytes(kept)


def download_pic(
    src: str,
    *,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
    fetch: Callable[..., _Fetched] | None = None,
) -> bytes:
    """下载那一跳的**默认实现**：把一个下家的图片直链取回字节。⚠️ 与 `post_to_oss` 同一条站姿：**短命 `httpx.Client`、不带任何 cookie / 认证头**（那个直链在第三方主机上，⛔ 绝不把鸡蛋铺的会话带过去）；失败一律翻成 `PicDownloadError`（独立故障面）：闸门不过 / 连不上 / 超时 / 非 200 / **0 字节** / **超 20 MiB** 都是失败（空文件永远不合法，照认领端点「图片无效或为空」）；文案只带异常类名 / 状态码，⛔ **不带 URL、不带响应体**（URL 的查询串里是签名）。
    ⭐ **H-2 加固（审计批）**：① scheme 只 http/https、URL **不许带凭据**；② host **解析后逐地址**拒绝环回 / 私有 / 链路本地 / 保留 / 组播（`_assert_safe_pic_url`，`ipaddress` 判）；③ **重定向 ≤ 3 跳且逐跳复检**：`follow_redirects=False`，每一跳的 `Location` 都重新过同一道闸（⛔ 不把 302 当免检通道）；④ **流式读 + 20 MiB 上限**（`MAX_PIC_BYTES`），⛔ 不信 `Content-Length`。
    ⚠️ **注入点（测试用）**：`resolver`（默认 `_resolve_host`）与 `fetch`（默认 `_fetch_pic`）可注入——用例**不打真网络**（DNS 与 HTTP 两层都由替身给），见 `tests/test_jidanpu_upstream_pics.py::DownloadPicTests`。**超时单独一档**（业务方口径）：走 `pic_timeout_seconds()`（设置 `JIDANPU_IMAGE_TIMEOUT`，默认 60 秒），⛔ **不复用**鸡蛋铺 SDK 的 `JidanpuConfig.timeout`（那 30 秒管的是 JSON API 调用）；**如实登记**：线上那条 `读订单图失败（JidanpuTimeoutError）` 是**读图列表**（`ViewPic`，走 SDK 的 30 秒）超时，**不是**这一跳——本档 60 秒是给**下载字节**单独定的，改动它救不了那句报错，也⛔ 不能当成「那句已修」。"""
    resolve = resolver or _resolve_host
    open_stream = fetch or _fetch_pic
    timeout = pic_timeout_seconds()
    current = src
    for hop in range(MAX_PIC_REDIRECTS + 1):
        current = _assert_safe_pic_url(current, resolver=resolve)
        try:
            fetched = open_stream(current, timeout=timeout)
        except httpx.HTTPError as exc:
            raise PicDownloadError(f"连接下家图片地址失败（{type(exc).__name__}）") from exc
        try:
            if fetched.status in _REDIRECT_STATUSES:
                if hop >= MAX_PIC_REDIRECTS:
                    raise PicDownloadError(
                        f"下家图片地址重定向超过 {MAX_PIC_REDIRECTS} 跳"
                    )
                location = str(fetched.headers.get("location") or "").strip()
                if not location:
                    raise PicDownloadError(
                        f"下家图片地址回 HTTP {fetched.status} 但没有 Location"
                    )
                current = urljoin(current, location)
                continue
            if fetched.status != 200:
                raise PicDownloadError(f"下家图片地址回 HTTP {fetched.status}")
            return _read_pic_capped(fetched)
        finally:
            fetched.close()
    # 循环体每支都 return / raise；到不了这里（防御性，别写死断言）。
    raise PicDownloadError("下家图片地址重定向次数过多")


def img_type_of_alt(alt: Any) -> int:
    """`alt` → 我方图种（计划 §1-B 第 2 步的逐字口径：含「群」⇒ 二维码，否则订单截图）。⚠️ 判据只看 `alt` 里 **`说明：` 之后**那一段（真机形状：`【机器人01】上传于10-4 21:53，说明：群二维码`）：`alt` 前半截是**对端上传者的名字**，名字里带「群」（如「群主小张」）会把订单截图误判成二维码；没有 `说明：` 标记时**整串回退**（判不了就按整串判，⛔ 不凭空当成二维码）。"""
    text = alt if isinstance(alt, str) else ""
    _head, marker, note = text.rpartition(ALT_NOTE_MARKER)
    scope = note if marker else text
    if GROUP_QR_HINT in scope:
        return OrderScreenshot.IMG_GROUP_QR
    return OrderScreenshot.IMG_ORDER


def stable_pic_identity(src: str) -> str:
    """源 URL 的**稳定身份**：`scheme://host/path`（⛔ 丢掉查询串与片段）。⚠️ 为什么必须丢掉查询串（证据）：对标页面渲染出来的直链带**变换参数**（形状：`…/<对象名>.png?x-oss-process=style/geshi`——出处见 `.tools/recon/media-storage/Order__PicMange.html`，**原始记录不进 git**），而签名直链的 `accessid` / `signature` 也在查询串里 ⇒ 把它算进哈希会让**同一张图因参数变化而再存一行**，而确定性键是这一路唯一的重跑保护（幂等）。
    ⚠️ 上面那个对象名是**同形状的合成值**（真机值⛔ 不进 `src/**`，E-0077）。"""
    parts = urlsplit(src if isinstance(src, str) else "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def remote_object_key(src: str) -> str:
    """源 URL 的**远端对象 key**（⛔ 与 `stable_pic_identity` 刻意不同：**丢掉域名**，只留路径）。为什么必须有它：同一张进度图会**同时**出现在两个域名上（真机实测）——回调报文里的 `imgUrl` 是 **OSS 直连**（`<桶>.<region>.aliyuncs.com/<对象名>`），查询接口（`GetOrderProcessImgs` / `ViewPic`）给的 `src` 是 `oss.jidanpu.com/<对象名>`；**这两个域名指向同一个对象**（同一个桶的两个访问入口）⇒ 按「域名 + 路径」判重会把**同一张图存成两行**，判据只能落在**对象 key**（路径）上。
    ⚠️ **①式那条 `ViewPic` 的路照旧用 `stable_pic_identity`**（域名 + 路径），⛔ **一个字都不许改**：它的确定性键**已经在生产上落过图**，换口径等于把老图全当成新图再存一遍 ⇒ 这里是**新增**一个身份函数，而不是改那一个；查询串照旧丢掉（签名 `accessid` / `signature`、以及 `x-oss-process` 那类变换参数都在里面），前导 `/` 也丢掉（两种域名给的前导斜杠形态可能不同，而对象名本身不带它）。"""
    parts = urlsplit(src if isinstance(src, str) else "")
    return parts.path.lstrip("/")


#: 「源 URL → 稳定身份」的那个函数的形状（`build_inbound_object_key` 的 `identity` 形参）。
PicIdentity = Callable[[str], str]


def build_inbound_object_key(*, order, img_type: int, src: str, ext: str,
                             identity: PicIdentity = stable_pic_identity) -> str:
    """拉回来的那张图在我们对象存储里的名字（**确定性** ⇒ 天然幂等；⛔ 不加列 / 不加迁移）。形状与认领端点**同一套 builder**（⛔ 不新造命名法）：**订单截图** ⇒ `build_shared_order_key(<链上最初单号>, ext, random_part=<哈希>)` = `shared-orders/<origin>/<哈希>.<ext>`（P4 裁决 R-P4-12：订单截图走共享前缀）；**群聊二维码** ⇒ **同一套共享段**：`build_shared_order_key(<链上最初单号>, ext, random_part=<哈希>, kind="qr")` = `shared-orders/<origin>/qr.<哈希>.<ext>`（⭐ D-0316：群码随订单同步 ⇒ 留在本租户前缀的老口径 R-P4-7 已废除，不改这条的话**从鸡蛋铺拉回的群码永远不同步**）。
    哈希 = `sha256(<identity>(src))` 的前 `KEY_DIGEST_CHARS` 个十六进制字符 ⇒ 仍是**确定性去重身份**（`object_key` 唯一约束照旧）；群码改走共享段之后，本函数**不再产出带租户段的 key**（未知 `img_type` 也走共享段）。
    ⚠️ **`identity` 的默认值就是老口径**（`stable_pic_identity` = 域名 + 路径）⇒ ①式那条 `ViewPic` 的路（不传它）**逐字不变**；②式那两条路显式传 `remote_object_key`（按**对象 key** 判重，理由见那个函数的 docstring）。"""
    digest = hashlib.sha256(identity(src).encode()).hexdigest()[:KEY_DIGEST_CHARS]
    if img_type == OrderScreenshot.IMG_GROUP_QR:
        return build_shared_order_key(
            order_shared_origin(order), ext, random_part=digest, kind="qr",
        )
    return build_shared_order_key(order_shared_origin(order), ext, random_part=digest)


def remote_pics_from_openapi_rows(rows) -> RemotePics:
    """官方 API 的进度图行（`GetOrderProcessImgs` 的 `{url, uploadTime, name, note}`）→ `RemotePics`。⚠️ 与 `_remote_pics`（`ViewPic` 那一份）**不是重复实现**：两边读的字段名不同（那一边是 `msg.data[*].src` / `alt`，这一边是 `data[*].url` / `note`），而**共同的那一半**（`RemotePics` 的两个字段、「有 N 条却一条都读不出地址」那条判据、下载 / 落库 / 幂等）都落在 `RemotePics` 与 `store_remote_pics` 上，两边共用。
    ⚠️ `name` 是**对端上传者的名字**（别的租户的人名，与 `ViewPic` 的 `【机器人01】` 同族）⇒ ⛔ **不进** `alt`（`alt` 只吃 `note` 那一句说明）；`alt` 的唯一用途是判图种（`img_type_of_alt`），而 `remark` 一律留空（隐私口径见模块顶部）。
    ⚠️ `rows` 取**收到的原始条数**（含读不出地址的那些）：`rows > 0` 而 `items` 为空正是「对端换了形状」那条响亮判据的输入（⛔ 不许静默当成「没图」）。"""
    items: list[RemotePic] = []
    count = 0
    for row in rows or ():
        count += 1
        if not isinstance(row, Mapping):
            continue
        src = row.get("url")
        if not isinstance(src, str) or not src.strip():
            continue
        note = row.get("note")
        items.append(RemotePic(src=src.strip(), alt=note if isinstance(note, str) else ""))
    return RemotePics(items=tuple(items), rows=count)


class _ScreenshotTargetChanged(Exception):
    pass


def store_remote_pics(pics: RemotePics, *, order, uploaded_by, now: datetime,
                      download: PicGetter | None = None,
                      identity: PicIdentity = stable_pic_identity,
                      img_types: tuple[int, ...] | None = None,
                      skip_remote_keys: set[str] | None = None,
                      allow_store: Callable[[], bool] | None = None) -> PullOutcome:
    """把**已经读到的**那批远端图下载 + 存进我方订单截图（⭐ 三条路**共用这一份**）。谁调它（⛔ 只有这三处，别再多一处）：①式 / 慢路（`pull_order_screenshots`）图从 `ViewPic` 的 `msg.data` 来、`identity` 用默认（`stable_pic_identity`，域名 + 路径）**与改动前逐字相同**；②式回调 `action=30`（`jidanpu_member_openapi`）图从报文里**带着**的 `imgUrl`（⛔ 不另打上游读接口）、`identity` = `remote_object_key`（**对象 key**）；②式「已完成 ⇒ 待验收」那一步（`jidanpu_member._pull_openapi_screenshots`）图从官方 API `GetOrderProcessImgs` 来、`identity` = `remote_object_key`。⚠️ **为什么②式那两条路要按对象 key 判重**：同一张图会同时出现在「回调给的 OSS 直连」与「查询接口给的 `oss.jidanpu.com`」两个域名上（实测重复送达 0.5%）⇒ 按域名判重会存两行，详见 `remote_object_key` 的 docstring。
    ## 两个可选闸门（默认都**不**启用 ⇒ 其它调用方逐字不变）：`img_types` 只收这些图种（`None` = 全收；代练员线传 `MEMBER_PULL_IMG_TYPES`，只收订单截图——群码是我们自己推给下家的，⛔ 不需要回收）；`skip_remote_keys` 是**远端对象 key** 的黑名单（`None` = 不跳过），命中 ⇒ 不下载、不入库——它补的是推送侧 `only_missing` 已有、而拉回侧原来**缺掉**的那半份判重（回环事故：我们推上去的图又被当成下家新图拉回来，见计划 Rev.2）；判据落在**对象 key** 上：推送台账登记的是对面 OSS 对象名，与 `src` 用哪个域名无关。
    ⛔ 本函数**不抛业务失败**：单张下载 / 单张落库的任何失败都收进 `PullOutcome.failed`——「要不要挡状态回写」是**调用方的口径**（代练员线是「任何失败都不挡『待验收』」，业务方口径③）；**「没图」≠「没拉成」**：正常回空 ⇒ 空的 `PullOutcome`（`reason` 也是空、一条台账都不写），读不出地址 ⇒ `failed` 里 `step="view"` 的一条人话——两种都不抛但**话术完全不同**，⛔ 不许把异常静默当成「没图」；「回包里有 N 条图但一条都读不出地址」记 `step="view"` 的失败（对端换形状的信号，⛔ 不许静默当成没图）；预期之外的异常（我们自己代码的错，如 `TypeError`）照旧裸逃：`failed` 只收「下载那一跳」与「单张落库」这两类**已预期**的失败面，其余由调用方兜（E-0010）。**幂等**：`object_key` 是**确定性键**（`build_inbound_object_key` + 传进来的 `identity`）⇒ 同一张图第二轮直接识别成 `existing`（⛔ 不更新、不重复存）；并发插同一张撞唯一约束 ⇒ 当良性幂等（照 `peer_inbound._apply_screenshot` 的同一条口径，内层 savepoint 只回滚这一笔）。**别的口径**：`uploaded_by` 由调用方给（那一列非空 + `PROTECT`），⛔ 本函数不猜「记在谁名下」；`remark` 一律留空：`alt` 里是**对端上传者的名字**（`【机器人01】…`），那是别的租户的人名，⛔ 不进我方任何一列；`original_name` 也留空（远程对象名⛔ 不落进我方库）；`content_type` 取**魔数嗅探**的结果（照认领端点：`Content-Type` 是客户端可控的），`size_bytes` 取真实字节数（这一路我们自己拿着字节，比 `peer_inbound` 那一路（只有 key）能填得更实）。"""
    if pics.rows and not pics.items:
        reason = PULL_SHAPE_REASON.format(n=pics.rows)
        return PullOutcome(
            failed=(FailedShot(object_key="", step=STEP_VIEW, reason=reason),),
            reason=reason,
        )

    fetcher = download or download_pic
    storage = get_storage()
    pulled: list[PulledShot] = []
    existing: list[str] = []
    skipped: list[SkippedShot] = []
    failed: list[FailedShot] = []

    # ⭐：本单已有 `ready` 群码 ⇒ 远端再来一张也不落第二张。
    #    业务依据：一张单只需要一张群码（同口径先例 = `api/my_orders_views.py` 的「本单已经有群二维码了，不用再同步」）。⛔ 这是**共享拉图**的行为变更：上家线同样受用（它本来就有「本单已有群码不再拉」的请求侧短路，这里是第二道兜底）。
    has_ready_qr = OrderScreenshot.objects.filter(
        order=order, img_type=OrderScreenshot.IMG_GROUP_QR,
        status=OrderScreenshot.STATUS_READY,
    ).exists()
    # 订单截图 20 张闸：只对 img_type=0；函数内 import 避免 api 层成环。
    from apps.business.api.screenshot_views import MAX_ORDER_SCREENSHOTS

    for index, pic in enumerate(pics.items, start=1):
        ext = _ext_of(urlsplit(pic.src).path)
        img_type = img_type_of_alt(pic.alt)
        # 闸门⓪：我们**自己推上去的**那张（推送台账按远端对象 key 登记）⇒ 不回收。
        #    ⚠️ 这是 那次回环的断点：推送侧 `only_missing` 已有这半份判重、
        #    拉回侧原来没有 ⇒ 「推上去 → 又拉回来 → 再补推」无限增殖。
        if skip_remote_keys and remote_object_key(pic.src) in skip_remote_keys:
            skipped.append(SkippedShot("", PULL_PUSHED_OWN_REASON.format(index=index)))
            continue
        # 闸门⓪'：图种白名单（代练员线只收订单截图）。
        if img_types is not None and img_type not in img_types:
            skipped.append(SkippedShot("", PULL_IMG_TYPE_SKIPPED_REASON.format(index=index)))
            continue
        # 闸门⓪''：单内上限（群码一张 / 订单截图 20 张）——都在下载之前判。
        # ⚠️ **与 D-0316「后到覆盖」的已知不对称（如实登记）**：peer 同步事件里的群码是"后到覆盖"（新图淘汰旧图）；而**拉图**这条路上，本单一旦有 `ready` 群码就**整档跳过**远端的群码（含对方更新的那张）——沿用既定业务口径「本单已经有群二维码了，不用再同步」。
        #    群码改走共享段**没有**改变这条行为，刻意不改（残差见 `D-0316`）。
        if img_type == OrderScreenshot.IMG_GROUP_QR and has_ready_qr:
            skipped.append(SkippedShot("", PULL_QR_ALREADY_EXISTS_REASON))
            continue
        if img_type == OrderScreenshot.IMG_ORDER:
            count = OrderScreenshot.objects.filter(
                order=order, img_type=OrderScreenshot.IMG_ORDER,
                status__in=(OrderScreenshot.STATUS_PENDING, OrderScreenshot.STATUS_READY),
            ).count()
            if count >= MAX_ORDER_SCREENSHOTS:
                skipped.append(SkippedShot("", PULL_ORDER_CAP_REASON))
                continue
        # 闸门①：扩展名（**在下载之前**判：省掉一次注定用不上的下载）。
        if ext not in ALLOWED_IMAGE_EXTS:
            skipped.append(SkippedShot("", PULL_EXT_REASON.format(index=index)))
            continue
        # 闸门②：下载（第三方主机那一跳；注入替身只为可复现的测试）。
        try:
            data = fetcher(pic.src)
        except Exception as exc:  # noqa: BLE001 —— 口径③：单张下载失败不挡状态回写
            failed.append(FailedShot(
                object_key="", step=STEP_DOWNLOAD, error=exc,
                reason=PULL_DOWNLOAD_REASON.format(index=index, detail=_pull_detail(exc)),
            ))
            continue
        # 闸门③：字节（空 / 超限）。
        if not data:
            skipped.append(SkippedShot("", PULL_EMPTY_REASON.format(index=index)))
            continue
        if len(data) > MAX_SCREENSHOT_BYTES:
            skipped.append(SkippedShot("", PULL_SIZE_REASON.format(index=index)))
            continue
        try:
            key = build_inbound_object_key(
                order=order, img_type=img_type, src=pic.src, ext=ext, identity=identity,
            )
            held = OrderScreenshot.objects.filter(object_key=key).first()
            if held is not None:
                if held.order_id == order.pk:
                    existing.append(key)          # ⭐ 幂等：同一张图第二轮不再存一行
                else:
                    failed.append(FailedShot(
                        object_key=key, step=STEP_STORE,
                        reason=PULL_KEY_TAKEN_REASON.format(index=index),
                    ))
                continue
            # 字节先进对象存储，再落行（照认领端点的顺序：行是 `ready` 就必须真有字节）。
            storage.put_bytes(key, data, content_type=CONTENT_TYPE_BY_EXT[ext])
            sniffed = sniff_image_type(storage, key)
            if sniffed is None:
                # ⛔ 不是图片的字节立刻删掉：留着就是一个「行都没有」的永久孤儿（A3 永久保留）。
                storage.delete(key)
                skipped.append(SkippedShot(key, PULL_TYPE_REASON.format(index=index)))
                continue
            try:
                with transaction.atomic():        # savepoint：并发插同一张只回滚这一笔
                    if allow_store is not None and not allow_store():
                        raise _ScreenshotTargetChanged
                    OrderScreenshot.objects.create(
                        order=order,
                        object_key=key,
                        img_type=img_type,
                        uploaded_by=uploaded_by,
                        remark="",
                        status=OrderScreenshot.STATUS_READY,
                        size_bytes=len(data),
                        content_type=sniffed,
                        ready_at=now,
                    )
                    # ⭐ 开放平台 v2（T8，设计文档 §7.2.1 裁决 3）：进度图变 `ready` 的**三个**写入点之三（从上家鸡蛋铺拉图）——⚠️ 这一处今天**不走**既有的入队函数（`peer_dispatch.enqueue_screenshot_event` 是 peer 链路的广播）⇒ 开放平台的推送必须在这里**显式**接一句；位置在 savepoint 块**内**、`create()` 之后：这一行真的 ready 与推送行同生共死，而上面那条 `IntegrityError` 幂等分支**不经过这里**（它 `continue` 了）⇒ 并发重复插不会重复推。
                    #    ⚠️ 时刻用本函数**已经在用的** `now`（同一次拉图里所有行的时刻口径一致），⛔ 不另取一次 `timezone.now()`。
                    #    ⚠️⭐ `spec=OPENAPI_SPEC` 是显式的（回调线 T1b）：投递引擎的公开入口从 T1b 起**不再有**「默认回上家侧」那一格（漏传 = 当场 `TypeError`）⇒ 本处（上家侧那一族的写入点）显式声明自己是哪一族。
                    from apps.openapi import dispatch as openapi_dispatch
                    from apps.openapi.credentials import OPENAPI_SPEC
                    from apps.takerapi.api import callback_recipients as taker_callback

                    openapi_dispatch.notify_order_image_added(
                        order,
                        spec=OPENAPI_SPEC,
                        object_key=key,
                        img_type=img_type,
                        remark="",
                        occurred_at=now,
                    )
                    # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 5 行后半）：**紧挨着**上面那句，同一个 savepoint、同一个 `now`（本函数已经在用的那个时刻，⛔ 不另取一次 `timezone.now()`——与上一句同一条口径）。
                    #    ⚠️ 位置在 `create()` 之后、并发 `IntegrityError` 幂等分支**之外** ⇒ 重复事件不重复推（与上一句同一条纪律）。
                    taker_callback.notify_order_image_added_for_taker(
                        order,
                        object_key=key,
                        img_type=img_type,
                        remark="",
                        occurred_at=now,
                    )
            except _ScreenshotTargetChanged:
                if not OrderScreenshot.objects.filter(object_key=key).exists():
                    storage.delete(key)
                break
            except IntegrityError:
                # 同 key 的并发 INSERT ⇒ 良性幂等（同 key 真的有一行，照 `peer_inbound._apply_screenshot`）；否则让这个 `IntegrityError` 落到下面的单张失败边界上（口径③：**单张落库失败不挡状态回写**，但必须留一条人话，⛔ 不许连人话都没有地静默过去）。
                if OrderScreenshot.objects.filter(object_key=key).exists():
                    existing.append(key)
                    continue
                # ⭐：另一条并发路径撞的是「每单一张 ready 群码」的唯一索引（`uniq_order_ready_group_qr`）——不是同 key。
                #    字节已经传上去了，而这一行落不下来 ⇒ **删掉刚传的对象**（否则就是「行都没有」的永久孤儿），并按「本单已有群码」记跳过（⛔ 不记失败：这不是故障）。
                if img_type == OrderScreenshot.IMG_GROUP_QR and OrderScreenshot.objects.filter(
                    order=order, img_type=OrderScreenshot.IMG_GROUP_QR,
                    status=OrderScreenshot.STATUS_READY,
                ).exists():
                    storage.delete(key)
                    skipped.append(SkippedShot(key, PULL_QR_ALREADY_EXISTS_REASON))
                    continue
                raise
        except Exception as exc:  # noqa: BLE001 —— 口径③：单张落库失败不挡状态回写
            failed.append(FailedShot(
                object_key="", step=STEP_STORE, error=exc,
                reason=PULL_STORE_REASON.format(index=index, detail=_pull_detail(exc)),
            ))
            continue
        pulled.append(PulledShot(object_key=key, img_type=img_type))

    if pulled:
        # ⭐ E-0282 / B2：新图落库 ⇒ 给该单已建链的①式 member 绑定排补推（`queue_pic_repush_for_ready_screenshots`：①式启用绑定 × 已建链 × 本单有图，无候选即刻返回；幂等/并发重复那一轮 `pulled` 为空 ⇒ 不触发）。
        #    ⚠️ **M4：本调用链刻意不在事务里**（网络下载在事务外是有意设计）⇒ 这一跳如实登记为**非事务**：「图已落库、补推事件没排上」的窗口由 `repush_member_pics` 命令兜底（台账判据可查），⛔ 不为它把拉图包进事务。
        #    函数内 import（环，照 `_push_screenshots_of_line` 那处的同一处置）：`jidanpu_member` 在模块级 import `jidanpu_upstream`，而后者又 import 本模块。
        from apps.business import jidanpu_member

        jidanpu_member.queue_pic_repush_for_ready_screenshots(order)

    return PullOutcome(
        pulled=tuple(pulled), existing=tuple(existing), skipped=tuple(skipped),
        failed=tuple(failed), reason=failed[0].reason if failed else "",
    )


def pull_order_screenshots(client, *, order, remote_order_id: str, uploaded_by,
                           now: datetime, download: PicGetter | None = None,
                           img_types: tuple[int, ...] | None = None,
                           skip_remote_keys: set[str] | None = None,
                           allow_store: Callable[[], bool] | None = None) -> PullOutcome:
    """把 `remote_order_id` 那张下家单上的图**拉回来、存进我方订单截图**（①式的 B 段）。⚠️ **本函数 = 「读列表」+ `store_remote_pics`**（抽出共用那一段）：读列表 / 单张下载 / 单张落库的失败面与幂等口径全在 `store_remote_pics` 的 docstring 里，⛔ 这里一个字都不重复；抽出的唯一目的是让②式那两条路（回调 `action=30` / 官方 API 全量）落库走**同一份**代码，而不是各写一份必然漂的落库循环。
    ⚠️ **①式那条路的行为一个字都没变**：读列表还是 `ViewPic`、`identity` 还是默认的 `stable_pic_identity`（域名 + 路径）、失败分档与人话也逐字是原来那一份；`img_types` / `skip_remote_keys` 只**透传**给 `store_remote_pics`（默认 `None` = 两道闸门都不启用 ⇒ 既有调用方逐字不变），口径见那边的 docstring。"""
    try:
        pics = fetch_remote_pics(client, remote_order_id=remote_order_id)
    except JidanpuError as exc:
        # ⭐ 「没拉成」的第一档：读列表失败（传输 / 会话 / 业务壳判不了）⇒ 人话 + 不抛。
        reason = PULL_UNREADABLE_REASON.format(detail=_pull_detail(exc))
        return PullOutcome(
            failed=(FailedShot(object_key="", step=STEP_VIEW, reason=reason, error=exc),),
            reason=reason,
        )
    return store_remote_pics(
        pics, order=order, uploaded_by=uploaded_by, now=now, download=download,
        img_types=img_types, skip_remote_keys=skip_remote_keys, allow_store=allow_store,
    )
