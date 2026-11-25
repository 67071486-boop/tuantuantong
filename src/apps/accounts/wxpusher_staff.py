"""Employee opt-in and approval recency for WxPusher."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Exists

from apps.accounts.choices import AccountSource
from apps.accounts.models import WxPusherNotice, WxPusherPreference
from apps.accounts.wxpusher_notice_templates import STAFF_KINDS
from apps.business.models import Approval, ApprovalKind, ApprovalStatus
from apps.core.plane import on_public_plane
from apps.rbac.permissions import has_node_now


def is_wxpusher_staff(user):
    if on_public_plane() or not user.is_authenticated or not user.is_active:
        return False
    profile = getattr(user, "staff_profile", None)
    return (
        (profile is None or (profile.deleted_at is None and profile.account_source != AccountSource.APP))
        and has_node_now(user, "my_profile")
    )


def staff_notice_is_pending(notice):
    approval_id = notice.details.get("approval_id")
    if type(approval_id) is not int or not 1 <= approval_id <= (1 << 63) - 1:
        return False
    kind = ApprovalKind.REFUND if notice.kind == "refund_assigned" else ApprovalKind.PENALTY
    newer = WxPusherNotice.objects.filter(
        user_id=notice.user_id, kind=notice.kind, id__gt=notice.pk,
        reference__startswith=f"approval:{approval_id}:",
    )
    # Check the current assignment and its queued replacements in a single snapshot.
    return Approval.objects.filter(
        ~Exists(newer),
        pk=approval_id, approver_id=notice.user_id, status=ApprovalStatus.PENDING, kind=kind,
    ).exists()


def _preferences_payload(preference):
    return {
        "enabled": preference.staff_enabled,
        "group_chat_enabled": True,
        "events": {kind: kind not in preference.disabled_staff_events for kind in STAFF_KINDS},
    }


def get_staff_preferences(user_id):
    preference = WxPusherPreference.objects.filter(user_id=user_id).first()
    return _preferences_payload(preference or WxPusherPreference(user_id=user_id))


@transaction.atomic
def update_staff_preferences(user_id, changes):
    if "enabled" in changes:
        # Serialize with link transfers without blocking the preference's foreign-key check.
        get_user_model().objects.select_for_update(no_key=True).values_list("pk", flat=True).get(pk=user_id)
    preference, _ = WxPusherPreference.objects.select_for_update().get_or_create(user_id=user_id)
    if "enabled" in changes:
        preference.staff_enabled = changes["enabled"]
    disabled = set(preference.disabled_staff_events)
    for kind, enabled in changes.get("events", {}).items():
        if enabled:
            disabled.discard(kind)
        else:
            disabled.add(kind)
    preference.disabled_staff_events = sorted(disabled)
    preference.save(update_fields=["staff_enabled", "disabled_staff_events"])
    return _preferences_payload(preference)
