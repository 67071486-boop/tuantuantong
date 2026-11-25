import logging
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlsplit

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from PIL import Image
from qcloud_cos.cos_exception import CosClientError, CosServiceError
from rest_framework.exceptions import PermissionDenied

from apps.accounts.admin_mutations import lock_tenant_admin_mutations
from apps.business import order_qr_fetch
from apps.business.game_scope import game_scope
from apps.business.models import EmpType, OrderScreenshot, OrderStatus, Performance
from apps.business.order_writes import lock_order_row
from apps.business.roles import WORKER
from apps.business.selectors import search_orders
from apps.core.plane import on_public_plane
from apps.core.scoping import scope_queryset
from apps.core.storage import MAX_IMAGE_BYTES as MAX_SOURCE_BYTES
from apps.core.storage import get_storage, key_belongs_to_tenant
from apps.rbac.permissions import has_node_now
from apps.wecom.group_qr import get_or_create_qr
from apps.wecom.models import WecomAppConfig, WecomGroupChat, WecomGroupQr

from . import core_bridge, service, system_notice_settings
from .images import IMAGE_FORMATS, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS
from .matrix_bridge import ChatServiceUnavailable
from .models import OrderGrabQrNotice
from .system_notifications import send_order_notification

logger = logging.getLogger(__name__)
MISSING_QR = "该订单暂无群二维码，请联系通讯录客服获取。"
UNCERTAIN_QR = "该订单的群二维码暂时无法确认，请联系通讯录客服获取。"
QR_PRESENT = "已接单，请查看该订单的群二维码。"
RETRY_INTERVAL = timedelta(minutes=2)
RETRY_DEADLINE = timedelta(minutes=20)
QR_PROVIDER_HOSTS = frozenset({"wework.qpic.cn", "wxwork.qpic.cn", "work.weixin.qq.com"})
STOPPED_STATUSES = frozenset({OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB, OrderStatus.REFUNDED})


class QrUnavailable(Exception):
    pass


RECOVERABLE_IMAGE_ERRORS = (
    QrUnavailable, order_qr_fetch.QrFetchError, OSError, ValueError, SyntaxError,
    Image.DecompressionBombError, CosClientError, CosServiceError,
)


def _enabled(claimed_at=None):
    if on_public_plane() or not settings.TT_CHAT_ORDER_GROUPS_ENABLED:
        return False
    enabled, resumed_at = system_notice_settings.order_grab_qr_window()
    return enabled and (claimed_at is None or resumed_at is None or claimed_at >= resumed_at) and not core_bridge.enabled()


def enqueue(order, *, user_id, claimed_at):
    if claimed_at is None:
        return
    try:
        with transaction.atomic():
            if not _enabled(claimed_at) or not service._active_contacts().filter(pk=user_id, groups__name=WORKER).exists():
                return
            OrderGrabQrNotice.objects.get_or_create(
                order=order, worker_id=user_id, claimed_at=claimed_at,
                defaults={"next_attempt_at": timezone.now()},
            )
    except (ChatServiceUnavailable, DatabaseError) as exc:
        logger.warning("接单群码通知入队失败 order=%s reason=%s", order.pk, type(exc).__name__)


def _current_worker(job, order):
    if order is None or order.deleted_at is not None or order.status in STOPPED_STATUSES:
        return None
    if not Performance.objects.filter(
        order=order, user_id=job.worker_id, claimed_at=job.claimed_at,
        emp_type=EmpType.MAIN, deleted_at__isnull=True,
    ).exists():
        return None
    return service._active_contacts().filter(pk=job.worker_id, groups__name=WORKER).first()


def _order_qrs(order):
    return OrderScreenshot.objects.filter(order=order, img_type=OrderScreenshot.IMG_GROUP_QR)


def _ready_qr(order):
    return _order_qrs(order).filter(status=OrderScreenshot.STATUS_READY).order_by("-pk").first()


def _read_order_qr(row):
    key = row.object_key
    if not key_belongs_to_tenant(key, connection.schema_name, shared_holder=lambda name: name == key):
        raise QrUnavailable("群码对象不属于当前订单")
    with closing(get_storage().open_stream(key)) as stream:
        return stream.read(MAX_SOURCE_BYTES + 1)


def _source_pattern(source):
    # PostgreSQL and Python both recognize these explicit ASCII token boundaries.
    escaped = re.sub(r"([\\.^$*+?{}\[\]|()])", r"\\\1", source)
    return rf"(^|[^A-Za-z0-9]){escaped}($|[^A-Za-z0-9])"


def _wecom_groups(config, pattern):
    return WecomGroupChat.objects.filter(
        identity_revision=config.identity_revision, dismissed=False, name__regex=pattern,
    ).order_by("pk")


@dataclass(frozen=True)
class WecomQrSource:
    config: WecomAppConfig
    source: str
    group_id: int
    join_way: str

    def still_matches(self, order, worker):
        same_config = WecomAppConfig.objects.filter(
            pk=self.config.pk, enabled=True, group_qr_enabled=True, identity_revision=self.config.identity_revision,
            corp_id=self.config.corp_id, agent_id=self.config.agent_id, secret=self.config.secret,
        ).exists()
        if not same_config or (order.source_order_no or "").strip() != self.source:
            return False
        groups = _wecom_groups(self.config, _source_pattern(self.source))
        if list(groups.values_list("pk", "pending_dismissal")[:2]) != [(self.group_id, False)]:
            return False
        return (
            WecomGroupQr.objects.filter(
                group_id=self.group_id, config_id=self.join_way, state__in=("ready", "reading"),
            ).exists()
            and has_node_now(worker, "order.view_detail", "order.view_content")
            and scope_queryset(
                worker, search_orders(include_hall_shadow=True).filter(pk=order.pk), scope_filter=game_scope("game_id"),
            ).exists()
        )


def _read_wecom_qr(job, order, worker):
    config = WecomAppConfig.objects.first()
    source = (order.source_order_no or "").strip()
    if not source or config is None or not config.is_ready() or not config.group_qr_enabled:
        return None, None
    pattern = _source_pattern(source)
    groups = list(_wecom_groups(config, pattern)[:2])
    if not groups:
        return None, None
    if len(groups) != 1 or groups[0].pending_dismissal:
        raise QrUnavailable("企微群码需要客服确认")
    group = groups[0]
    row = WecomGroupQr.objects.filter(group=group).first()
    if row is None or (not row.config_id and row.state == "idle"):
        return None, None
    match = WecomQrSource(config, source, group.pk, row.config_id)
    if not match.still_matches(order, worker):
        raise QrUnavailable("企微群码需要客服确认")

    def order_still_matches():
        current = search_orders(include_hall_shadow=True).filter(pk=order.pk).first()
        return _current_worker(job, current) is not None and match.still_matches(current, worker)

    result = get_or_create_qr(
        group, expected_source=source, order_still_matches=order_still_matches, allow_create=False,
        strict_name=re.compile(pattern).search,
    )
    if result["status"] != "ready":
        raise QrUnavailable("暂时无法读取企微群码")
    url = result["qr_url"]
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in QR_PROVIDER_HOSTS
        or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
    ):
        raise QrUnavailable("无法确认企微群码图片地址")
    return order_qr_fetch._download_image(url), match


def _attachment(content):
    if not content or len(content) > MAX_SOURCE_BYTES:
        raise QrUnavailable("群码图片大小无效")
    supported = {image_format: mime for mime, image_format in IMAGE_FORMATS.items()}
    with Image.open(BytesIO(content), formats=list(supported)) as photo:
        if photo.width * photo.height > MAX_IMAGE_PIXELS or getattr(photo, "is_animated", False):
            raise QrUnavailable("群码图片无效")
        photo.load()
        if len(content) <= MAX_IMAGE_BYTES:
            return {"mime": supported[photo.format], "width": photo.width, "height": photo.height, "data": content}
        photo.thumbnail((900, 900))
        reduced = BytesIO()
        photo.convert("RGB").save(reduced, format="JPEG", quality=90)
        if reduced.tell() > MAX_IMAGE_BYTES:
            raise QrUnavailable("群码图片过大")
        return {"mime": "image/jpeg", "width": photo.width, "height": photo.height, "data": reduced.getvalue()}


def _finish(job, *, text=None, image=None, screenshot=None, wecom=None):
    with transaction.atomic():
        order = lock_order_row(job.order)
        source = job.order.source_order_no
        job = OrderGrabQrNotice.objects.select_for_update().filter(pk=job.pk, finished_at__isnull=True).first()
        if job is None:
            return
        if text is not None:
            lock_tenant_admin_mutations()
        worker = _current_worker(job, order)
        if worker is not None and text is not None and _enabled(job.claimed_at):
            current_qr = _ready_qr(order)
            if _screenshot_stamp(current_qr) != _screenshot_stamp(screenshot):
                raise QrUnavailable("订单群码已更新")
            if wecom is not None and not wecom.still_matches(order, worker):
                raise QrUnavailable("企微群码已更新")
            if text == MISSING_QR and (
                source != order.source_order_no or _order_qrs(order).filter(status=OrderScreenshot.STATUS_PENDING).exists()
            ):
                raise QrUnavailable("订单群码已更新")
            send_order_notification(
                worker, order_no=order.order_no, text=text, image=image, client_id=f"grab-qr:{job.pk}",
            )
        job.finished_at = timezone.now()
        job.save(update_fields=["finished_at"])


def _screenshot_stamp(row):
    return (row.pk, row.object_key) if row is not None else None


def _resolve(job, order, worker, row):
    if row is not None:
        try:
            return _attachment(_read_order_qr(row)), None
        except RECOVERABLE_IMAGE_ERRORS:
            pass
    content, wecom = _read_wecom_qr(job, order, worker)
    if content is not None:
        return _attachment(content), wecom
    if row is not None or _order_qrs(order).filter(status=OrderScreenshot.STATUS_PENDING).exists():
        raise QrUnavailable("暂时无法读取订单群码")
    return None, None


def _deliver(job):
    order = job.order
    worker = _current_worker(job, order)
    if worker is None or not _enabled(job.claimed_at):
        _finish(job)
        return
    row = _ready_qr(order)
    try:
        image, wecom = _resolve(job, order, worker, row)
    except RECOVERABLE_IMAGE_ERRORS:
        if timezone.now() < job.claimed_at + RETRY_DEADLINE:
            return
        _finish(job, text=UNCERTAIN_QR, screenshot=row)
    else:
        if image is None and timezone.now() < job.claimed_at + RETRY_INTERVAL and order_qr_fetch._fetch_gate_passing(order):
            return
        _finish(job, text=QR_PRESENT if image else MISSING_QR, image=image, screenshot=row, wecom=wecom)


def run_due(*, limit=20):
    if not _enabled():
        return 0
    now = timezone.now()
    with transaction.atomic():
        jobs = list(OrderGrabQrNotice.objects.select_for_update(skip_locked=True, of=("self",)).filter(
            finished_at__isnull=True, next_attempt_at__lte=now,
        ).select_related("order").order_by("next_attempt_at", "pk")[:limit])
        OrderGrabQrNotice.objects.filter(pk__in=[job.pk for job in jobs]).update(next_attempt_at=now + RETRY_INTERVAL)
    for job in jobs:
        try:
            _deliver(job)
        except (ChatServiceUnavailable, PermissionDenied, QrUnavailable):
            continue
        except Exception as exc:
            # A retry keeps this notice durable without delaying delivery to other workers.
            logger.warning("接单群码通知处理失败 job=%s reason=%s", job.pk, type(exc).__name__)
    return len(jobs)
