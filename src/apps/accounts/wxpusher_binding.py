import re
import secrets
from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.accounts.models import (
    AppToken, WxPusherBinding, WxPusherLinkAttempt, WxPusherLinkRecipientThrottle, WxPusherNotice,
    WxPusherPreference,
)
from apps.accounts.wxpusher_config import APP_ID_PATTERN, load_wxpusher_config
from apps.accounts.wxpusher_staff import is_wxpusher_staff
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.roles import WORKER
from apps.core.wxpusher import WxPusherClient, WxPusherError, is_wxpusher_qr, is_wxpusher_subscription_link
from apps.core.wxpusher_app_links import DAIDAITONG_HOME_SCHEME, DAIDAITONG_ORIGIN
from apps.rbac.permissions import has_node_now

_VALID_SECONDS = 300
_LINK_VALID_SECONDS = 30 * 60
_LINK_CODE_INTERVAL = timedelta(seconds=60)
_LINK_RESTART = "订阅链接已失效，请重新生成。"
_LINK_EXTRA_PATTERN = re.compile(r"(?P<challenge>[a-f0-9]{32})\.[a-f0-9]{24}")
_PROVIDER_TIMEOUT = 6
_START_INTERVAL = timedelta(seconds=15)
_SCAN_INTERVAL = timedelta(seconds=10)
_TEST_INTERVAL = timedelta(seconds=60)
_MAX_CODE_FAILURES = 5
_RESTART = "绑定已失效，请重新生成二维码再次绑定。"


class BindingError(Exception):
    """可以直接显示给打手的提示。"""


def _locked_binding(user, config, *, session_hash=None, staff=False, app_token_id=None):
    # 用账号行锁覆盖尚未创建绑定行的情况，远程请求在锁外执行。
    try:
        locked_user = get_user_model().objects.select_for_update().only("pk", "password").get(pk=user.pk, is_active=True)
    except get_user_model().DoesNotExist:
        raise BindingError("账号已停用，请联系工作室。") from None
    _check_binding_user(locked_user, session_hash=session_hash, staff=staff, app_token_id=app_token_id)
    _check_config(config)
    return WxPusherBinding.objects.get_or_create(user_id=user.pk, app_id=config.app_id)[0]


def _check_binding_user(locked_user, *, session_hash=None, staff=False, app_token_id=None):
    if session_hash is not None and not constant_time_compare(session_hash, locked_user.get_session_auth_hash()):
        raise BindingError("登录已失效，请重新登录后绑定。")
    if app_token_id is not None and not AppToken.objects.select_for_update().filter(
        pk=app_token_id, user_id=locked_user.pk, revoked_at__isnull=True, expires_at__gt=timezone.now(),
    ).exists():
        raise BindingError("登录已失效，请重新登录后绑定。")
    if staff:
        if not is_wxpusher_staff(locked_user):
            raise BindingError("此账号不能设置员工通知。")
    elif not locked_user.groups.filter(name=WORKER).exists():
        raise BindingError("此账号没有打手权限。")


def _check_config(config):
    current = load_wxpusher_config()
    if current.source == "none" or (current.app_id, current.app_token) != (config.app_id, config.app_token):
        raise BindingError("WxPusher 配置已更新，请刷新页面后再试。")


def _active_challenge(binding, now):
    if binding.effective_uid:
        raise BindingError("当前账号已绑定，请先解除绑定。")
    if not binding.challenge or binding.expires_at is None or binding.expires_at <= now:
        raise BindingError(_RESTART)


def _code_digest(challenge, code):
    return salted_hmac("accounts.wxpusher-binding", f"{challenge}:{code}", algorithm="sha256").hexdigest()


def _uid_taken(binding, uid):
    return WxPusherBinding.objects.filter(
        Q(uid=uid) | Q(scan_bound_uid=uid), app_id=binding.app_id,
    ).exclude(pk=binding.pk).exists()


def start_binding(user, config, *, staff=False):
    challenge = secrets.token_hex(16)
    with transaction.atomic():
        binding = _locked_binding(user, config, staff=staff)
        if binding.effective_uid:
            raise BindingError("当前账号已绑定，请先解除绑定。")
        now = timezone.now()
        if binding.last_start_at and now - binding.last_start_at < _START_INTERVAL:
            raise BindingError("生成二维码太频繁，请等待 15 秒再试。")
        binding.clear_challenge()
        binding.challenge = challenge
        binding.last_start_at = now
        binding.expires_at = now + timedelta(seconds=_VALID_SECONDS)
        binding.save()

    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            qr = client.create_qrcode(app_token=config.app_token, extra=challenge, valid_time=_VALID_SECONDS)
    except WxPusherError:
        raise BindingError("暂时无法生成二维码，请稍后重试。") from None
    if not is_wxpusher_qr(qr.url):
        raise BindingError("WxPusher 返回的二维码无效，请稍后重试。")
    _check_config(config)
    saved = WxPusherBinding.objects.filter(
        pk=binding.pk, challenge=challenge, expires_at__gt=timezone.now(),
    ).update(qr_code=qr.code)
    if not saved:
        raise BindingError(_RESTART)
    return {"qr_url": qr.url, "expires_at": binding.expires_at, "challenge": challenge}


def _check_auto_challenge(binding, challenge, now):
    _active_challenge(binding, now)
    if binding.challenge != challenge or not binding.qr_code:
        raise BindingError(_RESTART)


def check_auto_binding(user, config, *, challenge, session_hash, staff=False, app_token_id=None):
    with transaction.atomic():
        binding = _locked_binding(user, config, session_hash=session_hash, staff=staff, app_token_id=app_token_id)
        now = timezone.now()
        _check_auto_challenge(binding, challenge, now)
        if binding.last_scan_at and now - binding.last_scan_at < _SCAN_INTERVAL:
            return {"bound": False}
        binding.last_scan_at = now
        binding.save(update_fields=["last_scan_at"])

    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            uid = client.verify_scanned_uid(
                app_token=config.app_token, code=binding.qr_code, app_id=binding.app_id,
            )
    except WxPusherError:
        return {"bound": False}
    if not uid or len(uid) > WxPusherBinding._meta.get_field("uid").max_length:
        return {"bound": False}

    with transaction.atomic():
        binding = _locked_binding(user, config, session_hash=session_hash, staff=staff, app_token_id=app_token_id)
        _check_auto_challenge(binding, challenge, timezone.now())
        if binding.last_scan_at != now:
            return {"bound": False}
        _confirm_uid(binding, uid, scanned_only=True)
    return {"bound": True}


def scan_binding(user, config):
    with transaction.atomic():
        binding = _locked_binding(user, config)
        now = timezone.now()
        _active_challenge(binding, now)
        if binding.code_digest:
            return _scan_result(binding)
        if not binding.qr_code:
            raise BindingError("二维码尚未生成，请稍后重新生成二维码。")
        if binding.last_scan_at and now - binding.last_scan_at < _SCAN_INTERVAL:
            raise BindingError("查询太频繁，请等待 10 秒再点「我已扫码」。")
        binding.last_scan_at = now
        binding.save(update_fields=["last_scan_at"])
        challenge = binding.challenge

    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            uid = client.verify_scanned_uid(
                app_token=config.app_token, code=binding.qr_code, app_id=binding.app_id,
            )
    except WxPusherError:
        raise BindingError("暂未查到扫码结果，请完成订阅，稍后再点「我已扫码」。") from None
    if not uid or len(uid) > WxPusherBinding._meta.get_field("uid").max_length:
        raise BindingError("暂未确认订阅，请在 WxPusher 完成订阅后再试。")

    code = f"{secrets.randbelow(1_000_000):06d}"
    with transaction.atomic():
        binding = _locked_binding(user, config)
        _active_challenge(binding, timezone.now())
        if binding.challenge != challenge or binding.last_scan_at != now or binding.code_digest:
            raise BindingError("绑定流程已更新，请返回当前页面继续。")
        if _uid_taken(binding, uid):
            raise BindingError("此 WxPusher 账号已在本工作室绑定，请先解除旧绑定。")
        binding.pending_uid = uid
        binding.code_digest = _code_digest(challenge, code)
        binding.expires_at = timezone.now() + timedelta(seconds=_VALID_SECONDS)
        binding.save(update_fields=["pending_uid", "code_digest", "expires_at"])

    delivery_confirmed = True
    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            client.send(
                app_token=config.app_token,
                uid=uid,
                summary="团团通绑定验证码",
                content=f"团团通绑定验证码：{code}。5 分钟内有效，请勿转发。",
            )
    except WxPusherError:
        # 请求超时也可能送达。收到即可确认，本挑战不会重复发送。
        delivery_confirmed = False

    updated = WxPusherBinding.objects.filter(
        pk=binding.pk, challenge=challenge, code_digest=binding.code_digest,
    ).update(delivery_confirmed=delivery_confirmed)
    if not updated:
        raise BindingError("绑定流程已更新，请返回当前页面继续。")
    binding.delivery_confirmed = delivery_confirmed
    return _scan_result(binding)


def _scan_result(binding):
    return {"expires_at": binding.expires_at, "delivery_unconfirmed": not binding.delivery_confirmed}


def confirm_binding(user, config, code):
    with transaction.atomic():
        binding = _locked_binding(user, config)
        _active_challenge(binding, timezone.now())
        if not binding.code_digest:
            raise BindingError("请先扫码获取验证码。")
        if constant_time_compare(binding.code_digest, _code_digest(binding.challenge, code)):
            _confirm_uid(binding, binding.pending_uid)
            return
        binding.code_failures += 1
        remaining = _MAX_CODE_FAILURES - binding.code_failures
        if remaining <= 0:
            binding.clear_challenge()
        binding.save()

    message = f"验证码不正确，还可尝试 {remaining} 次。" if remaining > 0 else "验证码尝试次数已用完，请重新绑定。"
    raise BindingError(message)


def _confirm_uid(binding, uid, *, scanned_only=False):
    if _uid_taken(binding, uid):
        raise BindingError("此 WxPusher 账号已在本工作室绑定，请先解除旧绑定。")
    # 旧版本只用 uid 做免密登录；扫码只绑通知时不写入它。
    if scanned_only:
        binding.scan_bound_uid = uid
    else:
        binding.uid = uid
    binding.clear_challenge()
    try:
        with transaction.atomic():
            binding.save()
    except IntegrityError:
        if _uid_taken(binding, uid):
            raise BindingError("此 WxPusher 账号已在本工作室绑定，请先解除旧绑定。") from None
        raise
    WxPusherLinkAttempt.objects.filter(binding=binding).delete()


def send_test_notice(user, config, *, staff=False, open_daidaitong=False, session_hash=None, app_token_id=None):
    with transaction.atomic():
        binding = _locked_binding(user, config, staff=staff, session_hash=session_hash, app_token_id=app_token_id)
        uid = binding.effective_uid
        if not uid:
            raise BindingError("请先绑定 WxPusher。")
        now = timezone.now()
        if binding.last_test_at and now - binding.last_test_at < _TEST_INTERVAL:
            raise BindingError("请间隔 60 秒再发送测试消息。")
        binding.last_test_at = now
        binding.save(update_fields=["last_test_at"])
        notice = WxPusherNotice.objects.create(
            user_id=user.pk, binding=binding, binding_started_at=binding.last_start_at,
            kind="test", reference=uuid4().hex, status=WxPusherNotice.Status.ATTEMPTED, attempted_at=now,
        )

    content = "这是一条团团通测试通知。看到这条消息，说明你的手机可以收到来自团团通的通知。"
    link = {}
    if open_daidaitong:
        content = f'<p>{content}</p><p><a href="{DAIDAITONG_ORIGIN}/m/?entry=wxpusher">打开团团通网页版</a></p>'
        link = {"url": DAIDAITONG_HOME_SCHEME, "html": True}
    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            client.send(
                app_token=config.app_token, uid=uid, summary="团团通测试通知",
                content=content, **link,
            )
    except WxPusherError:
        notice.status = WxPusherNotice.Status.UNCONFIRMED
        notice.save(update_fields=["status"])
        raise BindingError("暂时无法确认测试通知是否发送，请稍后再试。") from None
    notice.status = WxPusherNotice.Status.SENT
    notice.save(update_fields=["status"])
    return {"accepted": True}


def unlink_binding(user, config, *, staff=False):
    with transaction.atomic():
        binding = _locked_binding(user, config, staff=staff)
        binding.uid = None
        binding.scan_bound_uid = None
        binding.clear_challenge()
        binding.save()
        WxPusherLinkAttempt.objects.filter(binding=binding).delete()


def _link_extra(challenge, config):
    # The opaque provider link never returns this proof to the browser.
    scope = f"{connection.schema_name}:{config.app_id}:{config.app_token}:{challenge}"
    proof = salted_hmac("wxpusher-link-callback", scope, algorithm="sha256").hexdigest()[:24]
    return f"{challenge}.{proof}"


def _link_attempts(user, config, challenge):
    return WxPusherLinkAttempt.objects.filter(
        binding__user_id=user.pk, binding__app_id=config.app_id, challenge=challenge,
        expires_at__gt=timezone.now(),
    )


def link_attempt_expiry(user, config, challenge):
    return _link_attempts(user, config, challenge).values_list("expires_at", flat=True).first()


def start_link_binding(user, config, *, session_hash):
    if not re.fullmatch(APP_ID_PATTERN, config.app_id):
        raise BindingError("本工作室尚未配置有效的 WxPusher 应用编号。")
    challenge = secrets.token_hex(16)
    with transaction.atomic():
        binding = _locked_binding(user, config, session_hash=session_hash)
        now = timezone.now()
        if binding.last_link_start_at and binding.last_link_start_at > now - _START_INTERVAL:
            raise BindingError("生成订阅链接太频繁，请等待 15 秒再试。")
        binding.last_link_start_at = now
        binding.save(update_fields=["last_link_start_at"])
        expires = now + timedelta(seconds=_LINK_VALID_SECONDS)
        WxPusherLinkAttempt.objects.update_or_create(binding=binding, defaults={
            "challenge": challenge, "expires_at": expires, "verified_uid": "", "checked_at": None,
            "code_uid": "", "code_digest": "", "code_expires_at": None, "code_failures": 0,
        })

    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            qr = client.create_qrcode(
                app_token=config.app_token, extra=_link_extra(challenge, config), valid_time=_LINK_VALID_SECONDS,
            )
    except WxPusherError:
        raise BindingError("暂时无法生成订阅链接，请稍后重试。") from None
    if not isinstance(qr.link, str) or not is_wxpusher_subscription_link(qr.link):
        raise BindingError("WxPusher 暂未返回可用的订阅链接，请稍后重试。")
    _check_config(config)
    if not _link_attempts(user, config, challenge).exists():
        raise BindingError(_LINK_RESTART)
    return {"mode": "link", "subscribe_url": qr.link, "expires_at": expires, "attempt_id": challenge}


def record_link_subscription(config, extra, uid):
    match = _LINK_EXTRA_PATTERN.fullmatch(extra)
    if not match:
        return
    challenge = match.group("challenge")
    if not constant_time_compare(extra, _link_extra(challenge, config)):
        return
    WxPusherLinkAttempt.objects.filter(
        challenge=challenge, binding__app_id=config.app_id, expires_at__gt=timezone.now(), verified_uid="",
    ).update(verified_uid=uid, checked_at=None)


def cancel_link_binding(user, config, *, challenge, session_hash):
    with transaction.atomic():
        _locked_binding(user, config, session_hash=session_hash)
        deleted, _ = WxPusherLinkAttempt.objects.filter(
            binding__user_id=user.pk, binding__app_id=config.app_id, challenge=challenge,
        ).delete()
        if not deleted:
            raise BindingError("这次订阅绑定已结束，请刷新查看当前状态。")
    return {"canceled": True}


def _link_donors(user_id, app_id, uid):
    return WxPusherBinding.objects.filter(Q(uid=uid) | Q(scan_bound_uid=uid), app_id=app_id).exclude(user_id=user_id)


def _locked_link_recipients(user, config, uid, session_hash):
    donor_query = _link_donors(user.pk, config.app_id, uid)
    original_donor = donor_query.values_list("pk", "user_id", "last_start_at").first()
    ids = [user.pk] if original_donor is None else [user.pk, original_donor[1]]
    # Provider login and legacy binding use the same account locks before reading or changing a recipient.
    accounts = {account.pk: account for account in get_user_model().objects.select_for_update().filter(
        pk__in=ids,
    ).only("pk", "password", "is_active", "username").order_by("pk")}
    actor = accounts.get(user.pk)
    if actor is None or not actor.is_active:
        raise BindingError("账号已停用，请联系工作室。")
    _check_binding_user(actor, session_hash=session_hash)
    if not has_node_now(actor, "my_profile"):
        raise BindingError("此账号已不能设置个人通知，请联系工作室。")
    _check_config(config)

    current = WxPusherBinding.objects.filter(user_id=actor.pk, app_id=config.app_id).first()
    donor = donor_query.first()
    actual_donor = (donor.pk, donor.user_id, donor.last_start_at) if donor is not None else None
    if actual_donor != original_donor or (donor is not None and donor.user_id not in accounts):
        raise BindingError("绑定刚刚发生变化，请重新生成订阅链接再试。")
    if current is None:
        raise BindingError(_LINK_RESTART)
    return current, donor, actor, accounts.get(donor.user_id) if donor is not None else None


def check_link_binding(user, config, *, challenge, session_hash):
    attempts = _link_attempts(user, config, challenge)
    attempt = attempts.first()
    if attempt is None:
        raise BindingError(_LINK_RESTART)
    uid = attempt.verified_uid
    if not uid or attempt.code_uid:
        return {"bound": False}

    now = timezone.now()
    proof = attempts.filter(verified_uid=uid, code_uid="")
    if not proof.filter(Q(checked_at__isnull=True) | Q(checked_at__lte=now - _SCAN_INTERVAL)).update(checked_at=now):
        return {"bound": False}
    try:
        with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
            subscribed = client.is_subscribed(app_token=config.app_token, uid=uid, app_id=config.app_id)
    except WxPusherError:
        return {"bound": False}
    if not subscribed:
        return {"bound": False}

    with transaction.atomic():
        recipients = _locked_link_recipients(user, config, uid, session_hash)
        pending = proof.select_for_update(of=("self",)).filter(checked_at=now, expires_at__gt=timezone.now()).first()
        if pending is None:
            raise BindingError(_LINK_RESTART)
        _move_link_uid(*recipients, uid)
        pending.delete()
    return {"bound": True}


def _reserve_link_code_recipient(config, uid, now):
    scope = f"{connection.schema_name}:{config.app_id}:{uid}"
    digest = salted_hmac("wxpusher-link-recipient-limit", scope, algorithm="sha256").hexdigest()
    until = now + _LINK_CODE_INTERVAL
    limits = WxPusherLinkRecipientThrottle.objects
    _, created = limits.get_or_create(digest=digest, defaults={"blocked_until": until})
    if not created and not limits.filter(digest=digest, blocked_until__lte=now).update(blocked_until=until):
        raise BindingError("此 WxPusher 账号请求验证码太频繁，请等待 60 秒再试。")
    expired = limits.filter(blocked_until__lte=now).select_for_update(skip_locked=True).order_by("blocked_until")[:32]
    limits.filter(pk__in=list(expired.values_list("pk", flat=True))).delete()


def send_link_code(user, config, *, challenge, session_hash, uid):
    with transaction.atomic():
        binding = _locked_binding(user, config, session_hash=session_hash)
        attempt = _link_attempts(user, config, challenge).first()
        if attempt is None:
            raise BindingError(_LINK_RESTART)
        if attempt.code_failures >= _MAX_CODE_FAILURES:
            raise BindingError("验证码尝试次数已用完，请重新生成订阅链接。")
        now = timezone.now()
        if binding.last_link_code_at and binding.last_link_code_at > now - _LINK_CODE_INTERVAL:
            raise BindingError("请求验证码太频繁，请等待 60 秒再试。")
        _reserve_link_code_recipient(config, uid, now)
        binding.last_link_code_at = now
        binding.save(update_fields=["last_link_code_at"])
        attempt.code_uid = uid
        attempt.code_digest = ""
        attempt.code_expires_at = None
        attempt.save(update_fields=["code_uid", "code_digest", "code_expires_at"])

    reservation = _link_attempts(user, config, challenge).filter(
        code_uid=uid, code_digest="", binding__last_link_code_at=now,
    )
    with WxPusherClient(timeout=_PROVIDER_TIMEOUT) as client:
        try:
            if not client.is_subscribed(app_token=config.app_token, uid=uid, app_id=config.app_id):
                raise BindingError("这个 WxPusher 账号尚未订阅，请先完成订阅。")
            _check_config(config)
        except WxPusherError:
            reservation.update(code_uid="")
            raise BindingError("暂时无法确认 WxPusher 订阅，请稍后再试。") from None
        except BindingError:
            reservation.update(code_uid="")
            raise
        now = timezone.now()
        expires = min(attempt.expires_at, now + timedelta(seconds=_VALID_SECONDS))
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not reservation.filter(expires_at__gt=now).update(
            code_digest=_code_digest(challenge, code), code_expires_at=expires,
        ):
            raise BindingError(_LINK_RESTART)
        try:
            client.send(
                app_token=config.app_token, uid=uid, summary="团团通绑定验证码",
                content=f"团团通绑定验证码：{code}。请返回团团通确认，请勿转发。",
            )
        except WxPusherError:
            return {"sent": False, "delivery_unconfirmed": True, "expires_at": expires}
    return {"sent": True, "expires_at": expires}


def confirm_link_code(user, config, *, challenge, session_hash, code):
    attempts = _link_attempts(user, config, challenge)
    uid = attempts.values_list("code_uid", flat=True).first()
    if not uid:
        raise BindingError("请先获取 WxPusher 绑定验证码。")

    remaining = None
    with transaction.atomic():
        recipients = _locked_link_recipients(user, config, uid, session_hash)
        now = timezone.now()
        attempt = attempts.select_for_update(of=("self",)).filter(code_uid=uid, expires_at__gt=now).first()
        if attempt is None:
            raise BindingError(_LINK_RESTART)
        if attempt.code_failures >= _MAX_CODE_FAILURES:
            raise BindingError("验证码尝试次数已用完，请重新生成订阅链接。")
        if not attempt.code_digest or attempt.code_expires_at is None or attempt.code_expires_at <= now:
            raise BindingError("验证码已失效，请重新获取。")
        if not constant_time_compare(attempt.code_digest, _code_digest(challenge, code)):
            attempt.code_failures += 1
            remaining = _MAX_CODE_FAILURES - attempt.code_failures
            attempt.save(update_fields=["code_failures"])
        else:
            _move_link_uid(*recipients, uid)
            attempt.delete()
    if remaining is not None:
        msg = f"验证码不正确，还可尝试 {remaining} 次。" if remaining else "验证码尝试次数已用完，请重新生成订阅链接。"
        raise BindingError(msg)
    return {"bound": True}


def _move_link_uid(binding, donor, actor, donor_user, uid):
    if binding.effective_uid == uid:
        return
    if donor is not None and not (
        donor_user.groups.filter(name=WORKER).exists()
        and not WxPusherPreference.objects.filter(user_id=donor.user_id, staff_enabled=True).exists()
    ):
        raise BindingError("这个 WxPusher 账号关联了本工作室的其他通知，请联系工作室处理。")
    try:
        with transaction.atomic():
            if donor is not None:
                donor.uid = donor.scan_bound_uid = None
                donor.clear_challenge()
                donor.save()
                WxPusherLinkAttempt.objects.filter(binding=donor).delete()
                record(
                    log_type=ACTION_LOG_TYPES["user.update"], action="user.update",
                    actor=actor.get_username(), actor_id=actor.pk,
                    target_kind="user", target_id=donor_user.pk, target_name=donor_user.get_username(),
                    field_name="wxpusher_binding", reason="WxPusher 通知换绑",
                )
            binding.uid = None
            binding.scan_bound_uid = uid
            binding.last_start_at = timezone.now()
            binding.clear_challenge()
            binding.save()
    except IntegrityError:
        if _uid_taken(binding, uid):
            raise BindingError("绑定刚刚发生变化，请重新生成订阅链接再试。") from None
        raise
