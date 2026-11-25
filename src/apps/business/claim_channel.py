"""按认证客户端记录接单渠道，兼容既有移动端统计。"""
from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.deposit_gate import is_app_request

CHANNEL_WEB = "网页端大厅"
CHANNEL_APP = "App 端大厅"
CHANNEL_MINI_PROGRAM = "微信小程序端大厅"
CHANNEL_MOBILE = "打手移动端大厅"
CHANNEL_TAKER_API = "打手对接接口"
LEGACY_MOBILE_CHANNEL = "打手App"
DOWNSTREAM_CLAIM_FIELD = "downstream_claim"

MOBILE_CHANNELS = frozenset({CHANNEL_APP, CHANNEL_MINI_PROGRAM, CHANNEL_MOBILE, LEGACY_MOBILE_CHANNEL})


def channel_for(request) -> str:
    if not is_app_request(request):
        return CHANNEL_WEB
    client = (request.auth.client or "").strip().lower()
    if client == "mp-weixin":
        return CHANNEL_MINI_PROGRAM
    if client in {"app-android", "app-ios"}:
        return CHANNEL_APP
    return CHANNEL_MOBILE


def record_successful_claim(order, taker, *, price, channel="", downstream="", sync_actor=None):
    """确认已接手时记一次；下家事件仍保留真实的系统操作身份。"""
    record(
        log_type=ACTION_LOG_TYPES["order.claim"], action="order.claim",
        actor=sync_actor or taker.get_username(), actor_id=None if sync_actor else taker.pk,
        system=bool(sync_actor), target_kind="user", target_id=taker.pk,
        target_name=user_display_name(taker), order_no=order.order_no,
        source_order_no=order.source_order_no, to_value=price,
        field_name=DOWNSTREAM_CLAIM_FIELD if sync_actor else "",
        reason=downstream if sync_actor else channel,
    )
