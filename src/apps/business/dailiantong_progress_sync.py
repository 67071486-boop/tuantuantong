"""把代练通**打手上传的进度图**同步进本仓「订单截图」。 ## 背景详情页「代练通」页签此前只读上游 `LevelOrderProgressList`（不落本地截图管理）；业务方要求打手传的图也进本仓「订单截图」。 ## 口径 - 只处理**有效推送**（`state ∈ ACTIVE_STATES`、`is_active=True` 且有 `serial_no`）； ⛔ 不扫全历史 / 不再拉终态行； - 每张推送一次拉 `LevelOrderProgressList(Max=100)`； - **只要 `Source != 11`** 的图（`11` = 发单者，即我们自己发的自动群码等）；
`Source` 缺失时按「非我方」处理（宁可同步）； - `Img` 必须是 **http(s) 直链**：下载 → **字节魔数**判图种（⛔ 不信 Content-Type / 扩展名） → `get_storage().put_bytes` 存进**既有媒体存储**（`shared-orders/<origin>/…`，与订单截图同一套前缀）→ 建 `OrderScreenshot` 行； - `img_type = OrderScreenshot.IMG_ORDER`（「订单截图」档：这是代练进度图，不是群二维码）；
- 备注 = `NickName` + `Comment` + 时间（截到模型列宽 50）；⛔ 不把 URL 写进备注 / 审计。 ## 幂等 `OrderScreenshot.external_id = dailiantong:<push_id>:<progress_id>`（缺 ID 时用 URL 摘要）；同一单同一 external_id 只建一行；重复同步只补新增。**不重复下载**已存在的行。 ## 失败单张下载 / 类型 / 大小 / 写库失败 ⇒ 只记可读原因并继续其它图；不抛异常、不中断整轮同步。单条推送最多 100 张、每张 ≤ `MAX_IMAGE_BYTES`（18 MiB）。 ## 审计只有**新增 > 0** 时写一条机器审计（`order.dailiantong_progress`）：「从代练通同步了 N 张打手进度图（来源单号 …；失败 M）」；⛔ 不记图片 URL 全文。"""

from __future__ import annotations

import hashlib
import logging
import time
from urllib.parse import urlparse

import httpx
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import dailiantong_push as push_module
from apps.business.media_access import order_shared_origin
from apps.business.models import DailiantongPush, OrderScreenshot
from apps.core.storage import (
    IMAGE_EXT_BY_CONTENT_TYPE,
    MAX_IMAGE_BYTES,
    build_shared_order_key,
    get_storage,
    sniff_image_bytes,
)

logger = logging.getLogger(__name__)

#: 一次拉多少条进度（与读面 `PROGRESS_LIST_MAX` 同值；上游实测可回 100）。
PROGRESS_SYNC_MAX = 100
#: 下载超时（秒）：直链下载，不重试。
DOWNLOAD_TIMEOUT_SECONDS = 15.0
#: 发单者（我们）上传的 `Source` 值；这一档不同步进本地截图。
SOURCE_PUBLISHER = 11
#: ⭐ M1：只允许代练通自己的图床域名（SSRF 边界）；子域也必须落在这个后缀下。
ALLOWED_IMAGE_HOST_SUFFIX = ".dailiantong.com"
ALLOWED_IMAGE_HOSTS = frozenset({"dailiantong.com"})
#: 备注列宽（唯一出处 = 模型列定义；⛔ 不手写 50）。
REMARK_MAX_LENGTH = OrderScreenshot._meta.get_field("remark").max_length

# ⭐ M7三个可靠性常量 ──────────────────────────────────
#: 进度图是**代练通**的能力 ⇒ 候选查询必须按平台收窄（⛔ 拿代练通凭据打别的平台）。
PROGRESS_SYNC_PLATFORM = push_module.DEFAULT_PLATFORM
#: 失败图的**负缓存**时长（小时）：同一张图（同一推送行 + 同一上游进度 ID）在这个窗口内不再重试。用途 = 死链图每轮都要 15 秒超时，不记负缓存会把一轮拖成几十分钟。
#: ⚠️ 用 Django cache（进程内 LocMem 也够：常驻的 `dailiantong_sync --loop` 是单个进程，
#: 且有进程锁）；进程重启后最多重新试一次，代价有限。
FAILED_IMAGE_RETRY_HOURS = 6
#: **单轮总耗时上限**（秒）：到点就停，剩下的留给下一轮。回调为主 ⇒ 一轮拖太久会推迟
#: 新到回调的消费（`_loop` 同一时刻只跑一轮）。判据在每个推送 / 每张图**开始之前**检查，
#: 所以最坏超时 = 上限 + 一次下载超时（15 秒）。
MAX_ROUND_SECONDS = 120


def _failed_image_cache_key(push, external_id: str) -> str:
    """失败图负缓存的键：**租户 + 推送行 + 上游进度 ID**（⛔ 不同租户互相串）。"""
    schema = (getattr(connection, "schema_name", "") or "").strip() or "public"
    return f"dailiantong:progress-failed:{schema}:{push.pk}:{external_id}"


def _failed_image_is_cached(push, external_id: str) -> bool:
    return cache.get(_failed_image_cache_key(push, external_id)) is not None


def _remember_failed_image(push, external_id: str, reason: str) -> None:
    """失败图记负缓存（窗口见 `FAILED_IMAGE_RETRY_HOURS`）；原因也存进去供排障。"""
    cache.set(
        _failed_image_cache_key(push, external_id),
        (reason or "同步失败")[:200],
        FAILED_IMAGE_RETRY_HOURS * 3600,
    )


class ProgressImageError(RuntimeError):
    """单张图下载 / 校验失败（可读中文；调用方转为失败计数）。"""


def _row_value(row, *names):
    """进度行取值（上游键大小写不保证）。"""
    if not isinstance(row, dict):
        return None
    for name in names:
        if name in row:
            return row[name]
    folded = {str(key).strip().lower(): value for key, value in row.items()}
    for name in names:
        key = name.strip().lower()
        if key in folded:
            return folded[key]
    return None


def _row_int(row, *names) -> int | None:
    return push_module._as_int(_row_value(row, *names))


def _fetch_image(url: str) -> bytes:
    """下载一张进度图；非 http(s) / 超限 / HTTP 失败 ⇒ `ProgressImageError`。"""
    text = (url or "").strip()
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ProgressImageError("进度图不是 https 直链，无法下载")
    if host not in ALLOWED_IMAGE_HOSTS and not host.endswith(
        ALLOWED_IMAGE_HOST_SUFFIX
    ):
        raise ProgressImageError("进度图域名不在代练通图床白名单内，拒绝下载")
    chunks: list[bytes] = []
    total = 0
    try:
        with httpx.stream(
            "GET",
            text,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            # ⭐ M1：不跟重定向（否则白名单域名可以 302 到任意地址）。
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ProgressImageError(
                    f"下载进度图失败（HTTP {response.status_code}）"
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ProgressImageError(
                        f"进度图超过本仓上限（{MAX_IMAGE_BYTES} 字节）"
                    )
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ProgressImageError(
            f"下载进度图失败（{type(exc).__name__}）"
        ) from exc
    data = b"".join(chunks)
    if not data:
        raise ProgressImageError("进度图是空文件")
    return data


def _progress_external_id(push, row) -> str:
    """幂等键：`dailiantong:<push_id>:<progress_id>`（缺 ID 时用 URL 摘要）。"""
    progress_id = _row_value(row, "ID", "Id", "id")
    if progress_id is not None and str(progress_id).strip():
        suffix = str(progress_id).strip()
    else:
        url = str(_row_value(row, "Img", "IMG", "img") or "").strip()
        suffix = "url:" + hashlib.sha1(
            url.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:16]
    return f"dailiantong:{push.pk}:{suffix}"[:64]


def _remark_for(row) -> str:
    parts = [
        str(_row_value(row, "NickName", "Nickname", "nickname") or "").strip(),
        str(_row_value(row, "Comment", "comment", "Msg") or "").strip(),
        str(_row_value(row, "CreateDate", "create_date") or "").strip(),
    ]
    return " ".join(part for part in parts if part)[:REMARK_MAX_LENGTH]


def _screenshot_uploader(order):
    """同步行的 `uploaded_by`（NOT NULL + PROTECT）：优先本单客服，否则占位账号「代练通」。"""
    if order.sales_user_id:
        return order.sales_user
    return push_module.placeholder_taker_user()


def sync_progress_screenshots_for_push(
    push: DailiantongPush, *, client=None, deadline: float | None = None
) -> tuple[int, int, str]:
    """同步一条推送的打手进度图；返回 `(新增, 失败, 可读说明)`（⛔ 不抛预期失败）。 ⭐ M7： - **平台收窄**：本函数只服务代练通（`PROGRESS_SYNC_PLATFORM`）；别的平台的行直接跳过
    （⛔ 别拿代练通的凭据 / 接口去打别的平台）； - **负缓存**：同一个（推送行 + 上游进度 ID）在 `FAILED_IMAGE_RETRY_HOURS` 小时内
    失败过 ⇒ 本节不再重试（死链图否则每轮 15 秒超时）； - **单轮总耗时上限**：`deadline`（`time.monotonic()` 绝对值）到了就停，剩下的留给下一轮。"""
    if push.platform != PROGRESS_SYNC_PLATFORM:
        # 防御性：候选查询已经按平台过滤，这里挡住手工命令 / 直接调用拿错行的情形。
        return 0, 0, ""
    serial = (push.serial_no or "").strip()
    if not serial:
        return 0, 0, ""

    owns_client = client is None
    if owns_client:
        integration = push_module._active_integration(push.platform)
        client = push_module._open_client(integration)
    try:
        try:
            page = client.progress_list(serial, max_count=PROGRESS_SYNC_MAX)
        except Exception as exc:  # noqa: BLE001 —— 单条推送读失败不影响整轮
            logger.warning(
                "代练通进度图读取失败 push=%s serial=%s（%s）",
                push.pk,
                serial,
                type(exc).__name__,
            )
            return 0, 0, "读取代练通进度图失败（上游暂时不可用）"
    finally:
        if owns_client:
            push_module._close(client)

    order = push.order
    # 业务方裁定：每单「订单截图」最多 20 张（常量唯一出处 = 截图 API 模块）。
    from apps.business.api.screenshot_views import MAX_ORDER_SCREENSHOTS

    room = MAX_ORDER_SCREENSHOTS - OrderScreenshot.objects.filter(
        order=order, img_type=OrderScreenshot.IMG_ORDER
    ).count()
    added = 0
    failed = 0
    first_error = ""
    capped = False
    timed_out = False
    for row in page.rows:
        if _row_int(row, "Source", "source") == SOURCE_PUBLISHER:
            continue
        url = str(_row_value(row, "Img", "IMG", "img") or "").strip()
        if not url:
            continue
        if room <= 0:
            capped = True
            break
        if deadline is not None and time.monotonic() >= deadline:
            # ⭐ M7：本轮总耗时到点 ⇒ 这张及后面的留给下一轮（⛔ 不阻塞回调消费）。
            timed_out = True
            break
        external_id = _progress_external_id(push, row)
        if OrderScreenshot.objects.filter(
            order=order, external_id=external_id
        ).exists():
            continue
        if _failed_image_is_cached(push, external_id):
            # ⭐ M7 负缓存：这张图最近失败过 ⇒ 本轮不重试（不计失败、不刷日志）。
            continue

        object_key = ""
        try:
            data = _fetch_image(url)
            content_type = sniff_image_bytes(data)
            if content_type is None:
                raise ProgressImageError("图片类型无法识别（只支持 jpg / png）")
            ext = IMAGE_EXT_BY_CONTENT_TYPE[content_type]
            object_key = build_shared_order_key(order_shared_origin(order), ext)
            get_storage().put_bytes(object_key, data, content_type=content_type)
            # ⭐ 先例口径（`jidanpu_upstream_pics.store_remote_pics`）：落库与派发共用
            #    同一个备注 / 同一个时刻对象 ⇒ 同一张图的「ready 时刻」两族读数一致。
            remark = _remark_for(row)
            ready_at = timezone.now()
            with transaction.atomic():
                OrderScreenshot.objects.create(
                    order=order,
                    img_type=OrderScreenshot.IMG_ORDER,
                    object_key=object_key,
                    external_id=external_id,
                    original_name=f"dailiantong-progress-{external_id.rsplit(':', 1)[-1]}.{ext}",
                    size_bytes=len(data),
                    content_type=content_type,
                    remark=remark,
                    uploaded_by=_screenshot_uploader(order),
                    status=OrderScreenshot.STATUS_READY,
                    ready_at=ready_at,
                )
                # ⭐ 开放平台 v2 + 打手回调（设计文档 §7.2.1 裁决 3 的同族；业务方业务方拍板补推）：进度图变 `ready` 的写入点之四（代练通打手进度图拉回入库）——照 `peer_inbound._apply_screenshot`（②）与 `jidanpu_upstream_pics.store_remote_pics`（③）两个同款先例：
                # 这里**不走** peer 链路的广播（`peer_dispatch.enqueue_screenshot_event`） ⇒ 两族推送必须各自**显式**接一句。 ⚠️ 位置在 savepoint 块**内**、`create()` 之后：这一行真的 ready 与推送行同生共死；下面那条 `IntegrityError` 幂等分支**不经过这里**
                # （它 `continue` 了）⇒ 并发重复插不会重复推；循环开头 `exists()` 预查那一支同样不经过（没有 `create` 就没有派发）⇒ 重复同步不重复推。 ⚠️ 时刻用上面落库的那个 `ready_at`（先例口径：两族共用同一个时刻对象，⛔ 不另取一次 `timezone.now()`）。 ⚠️⭐ `spec=OPENAPI_SPEC` 是显式的（先例同款：投递引擎的公开入口没有"默认族"那一格，漏传 = 当场 `TypeError`）。
                from apps.openapi import dispatch as openapi_dispatch
                from apps.openapi.credentials import OPENAPI_SPEC
                from apps.takerapi.api import callback_recipients as taker_callback

                openapi_dispatch.notify_order_image_added(
                    order,
                    spec=OPENAPI_SPEC,
                    object_key=object_key,
                    img_type=OrderScreenshot.IMG_ORDER,
                    remark=remark,
                    occurred_at=ready_at,
                )
                # ⭐ 打手侧那一族紧挨着上面那句：同一个 savepoint、同一个 `ready_at`
                #    （收件人 = **当前接手人**名下的应用：代练通单通常无本地接手人 ⇒
                #    收件人函数自己算出空列表、零行——⛔ 不是漏接）。
                taker_callback.notify_order_image_added_for_taker(
                    order,
                    object_key=object_key,
                    img_type=OrderScreenshot.IMG_ORDER,
                    remark=remark,
                    occurred_at=ready_at,
                )
        except IntegrityError:
            # 并发 / 重复同步：行已经存在 ⇒ 良性跳过，并清掉这次多写的字节。
            if object_key:
                try:
                    get_storage().delete(object_key)
                except Exception:  # noqa: BLE001 —— 清理失败不影响结果
                    pass
            continue
        except ProgressImageError as exc:
            failed += 1
            _remember_failed_image(push, external_id, str(exc))
            if not first_error:
                first_error = str(exc)
            logger.warning(
                "代练通进度图同步失败 push=%s progress=%s（%s）",
                push.pk,
                external_id,
                exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 —— 单张失败不中断其它图
            failed += 1
            _remember_failed_image(push, external_id, str(exc) or type(exc).__name__)
            if not first_error:
                first_error = "写入本地截图失败"
            if object_key:
                try:
                    get_storage().delete(object_key)
                except Exception:  # noqa: BLE001
                    pass
            logger.warning(
                "代练通进度图入库失败 push=%s progress=%s（%s）",
                push.pk,
                external_id,
                type(exc).__name__,
            )
            continue
        added += 1
        room -= 1

    if added:
        record(
            log_type=ACTION_LOG_TYPES["order.dailiantong_progress"],
            action="order.dailiantong_progress",
            actor=push_module.SYNC_ACTOR,
            actor_id=None,
            system=True,
            target_kind="dailiantong_push",
            target_id=str(push.pk),
            target_name=order.order_no,
            order_no=order.order_no,
            game_name=(order.game.name or "") if order.game_id else "",
            reason=(
                f"从代练通同步了 {added} 张打手进度图"
                f"（来源单号 {serial}；失败 {failed}）"
            ),
        )

    note = ""
    if failed:
        note = f"有 {failed} 张进度图同步失败（第一张：{first_error}）"
    if capped:
        note = (note + "；" if note else "") + (
            f"已达每单 {MAX_ORDER_SCREENSHOTS} 张订单截图上限，剩余未同步"
        )
    if timed_out:
        note = (note + "；" if note else "") + (
            f"本轮总耗时已达上限（{MAX_ROUND_SECONDS} 秒），剩余进度图留到下一轮"
        )
    return added, failed, note


def sync_active_progress_screenshots(*, max_seconds: float | None = MAX_ROUND_SECONDS) -> tuple[int, int]:
    """本租户内所有**有效推送**同步一轮进度图；返回 `(新增, 失败)`。 ⚠️ 逐条推送独立异常边界；单条失败只记日志，不影响其它推送 / 整轮同步。
    ⭐ M7： - 候选查询**按平台收窄**（只代练通）——将来有别的平台行时，⛔ 不能拿代练通凭据去拉； - **单轮总耗时上限** `max_seconds`（默认 `MAX_ROUND_SECONDS`）：到点就停（含正在处理
    的那条推送里的每张图），剩下的留给下一轮。`None` / `<= 0` = 不限（只在排障 / 测试用）。"""
    deadline = (
        time.monotonic() + float(max_seconds)
        if max_seconds is not None and max_seconds > 0
        else None
    )
    added_total = 0
    failed_total = 0
    qs = (
        DailiantongPush.objects.filter(
            platform=PROGRESS_SYNC_PLATFORM,
            state__in=DailiantongPush.ACTIVE_STATES,
            # ⭐ 只同步**还在轮询**的有效推送：终态（is_active=False）的行不再拉进度，
            # 避免「下一轮零上游调用」那条判据被进度图同步破坏。
            is_active=True,
        )
        .exclude(serial_no__isnull=True)
        .exclude(serial_no="")
        .order_by("id")
    )
    for push in qs.iterator():
        if deadline is not None and time.monotonic() >= deadline:
            logger.info(
                "代练通进度图同步达到本轮耗时上限（%s 秒），剩余推送留到下一轮",
                max_seconds,
            )
            break
        try:
            added, failed, _note = sync_progress_screenshots_for_push(
                push, deadline=deadline
            )
        except Exception as exc:  # noqa: BLE001 —— 一条推送失败不影响其它
            # ⚠️ 读列表阶段的失败不算「某张图失败」（没有 external_id 可记）⇒ 只落日志 / 计数。
            logger.warning(
                "代练通进度图同步异常 push=%s（%s）", push.pk, type(exc).__name__
            )
            failed_total += 1
            continue
        added_total += added
        failed_total += failed
    return added_total, failed_total
