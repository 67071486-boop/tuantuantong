"""Order-page WeCom display login, separate from standalone archive access."""

from django.db import connection
from django.utils import timezone


ORDER_ARCHIVE_VIEWER_SESSION_KEY = "wecom_order_archive_viewer"


def order_archive_viewer_userid(request, config):
    identity = request.session.get(ORDER_ARCHIVE_VIEWER_SESSION_KEY)
    if not isinstance(identity, dict):
        return ""
    expires_at = identity.get("expires_at")
    if (
        identity.get("tenant_schema") != connection.schema_name
        or identity.get("user_id") != str(request.user.pk)
        or identity.get("corp_id") != config.corp_id
        or identity.get("agent_id") != config.agent_id
        or identity.get("identity_revision") != config.identity_revision
        or type(expires_at) is not int
        or expires_at <= timezone.now().timestamp()
    ):
        return ""
    userid = identity.get("wecom_userid")
    return userid if isinstance(userid, str) else ""
