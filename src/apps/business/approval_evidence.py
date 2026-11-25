"""Issue upload tickets and serve immutable, participant-only approval evidence."""

import re
from contextlib import contextmanager

from django.core import signing
from django.db import connection, transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.urls import reverse
from rest_framework.exceptions import PermissionDenied

from apps.business.models import Approval, ApprovalKind
from apps.core.storage import (
    ALLOWED_EXTS,
    APPROVAL_EVIDENCE_KIND,
    MAX_APPROVAL_EVIDENCE_BYTES,
    USER_KEY_SEGMENT,
    build_user_key,
    get_storage,
    sniff_image_bytes,
)

UPLOAD_TICKET_SECONDS = 600
_UPLOAD_TICKET_SALT = "business.approval-evidence.upload"
_IMAGE_NAME = re.compile(rf"{APPROVAL_EVIDENCE_KIND}\.[0-9a-f]{{12}}\.(?:{'|'.join(ALLOWED_EXTS)})\Z")


def image_belongs_to_user(key, user_id):
    prefix = f"{connection.schema_name}/{USER_KEY_SEGMENT}/{user_id}/"
    name = key.removeprefix(prefix)
    return name != key and _IMAGE_NAME.fullmatch(name) is not None


@contextmanager
def lock_evidence(key):
    # Upload and attachment wait on the same object, including with multiple web workers.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [key])
        yield


def issue_image_ticket(user, ext):
    key = build_user_key(connection.schema_name, user.pk, APPROVAL_EVIDENCE_KIND, ext)
    ticket = signing.dumps(key, salt=_UPLOAD_TICKET_SALT)
    return {
        "key": key,
        "upload_url": reverse("approval-image-upload", kwargs={"ticket": ticket}),
        "expires_in": UPLOAD_TICKET_SECONDS,
    }


def _image_content_type(key):
    return "image/png" if key.endswith(".png") else "image/jpeg"


def upload_evidence(request, ticket):
    try:
        key = signing.loads(ticket, salt=_UPLOAD_TICKET_SALT, max_age=UPLOAD_TICKET_SECONDS)
    except signing.BadSignature as exc:
        raise PermissionDenied("截图上传凭证无效或已过期") from exc
    if not image_belongs_to_user(key, request.user.pk):
        raise PermissionDenied("截图不属于当前账号")

    stream = request.stream
    data = stream.read(MAX_APPROVAL_EVIDENCE_BYTES + 1) if stream is not None else b""
    if not 0 < len(data) <= MAX_APPROVAL_EVIDENCE_BYTES:
        return HttpResponse(status=413)
    content_type = _image_content_type(key)
    if sniff_image_bytes(data) != content_type:
        return HttpResponse(status=400)

    storage = get_storage()
    with lock_evidence(key):
        if storage.head(key) is not None:
            return HttpResponse(status=409)
        storage.put_bytes(key, data, content_type=content_type)
    return HttpResponse(status=204)


def evidence_image_url(approval_id, index):
    return reverse("approval-image-read", kwargs={"pk": approval_id, "index": index})


def read_evidence(user, pk, index):
    participants = Q(initiator=user) | Q(approver=user) | Q(cc=user)
    approval = Approval.objects.only("initiator_id", "image_keys").filter(
        participants, pk=pk, kind=ApprovalKind.PENALTY,
    ).first()
    if approval is None or index >= len(approval.image_keys):
        return HttpResponse(status=404)
    key = approval.image_keys[index]
    if not image_belongs_to_user(key, approval.initiator_id):
        return HttpResponse(status=404)

    storage = get_storage()
    if storage.head(key) is None:
        return HttpResponse(status=404)
    response = FileResponse(storage.open_stream(key), content_type=_image_content_type(key))
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
