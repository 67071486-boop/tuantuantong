"""短信验证码业务（打手 App 线）。

## 落点

模型 `apps.accounts.models.SmsCode` 在租户库（`TENANT_APPS`）⇒ 每个工作室的验证码
各自独立，⛔ 不跨租户。

## 限流为什么数库而不是用 cache

`django.core.cache` 的默认后端是**单进程内存**（locmem）；生产是 gunicorn 多 worker，
每个 worker 一份 cache ⇒ 限流形同虚设（同一号码能连发 N 次，N = worker 数）。
本模块直接数 `SmsCode` 表的行——准确、跨进程一致，代价是每次发送多两条 COUNT
（发送本身要调外部 API，这点开销不值得优化）。

## 对外文案纪律

「验证码错误 / 已过期 / 已使用」三种情况**对外同一个文案与同一个码**
（`APP_SMS_CODE_INVALID`）：区分它们等于告诉攻击者「这个号码确实收过码」。
"""
import secrets
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from apps.accounts.models import SmsCode
from apps.business.peer_sync import hash_token, token_matches
from apps.core.error_codes import ErrorCode
from apps.core.sms import (
    CODE_LENGTH,
    CODE_TTL_SECONDS,
    SEND_COOLDOWN_SECONDS,
    SEND_DAILY_LIMIT_PER_IP,
    SEND_DAILY_LIMIT_PER_MOBILE,
    send_verification_code,
)


#: 同一条码允许校验失败的次数。到上限即作废该条码——6 位码在 5 分钟
#: 窗口里可被枚举，不设上限等于把验证码当公网密码用。
MAX_VERIFY_ATTEMPTS = 5


class SmsError(Exception):
    """短信业务的预期失败。`code` 直接给 API 层当业务码用（与 `ClaimError` 同形）。"""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def _issue_code(*, mobile: str, purpose: str, request_ip: str | None):
    now = timezone.now()
    day_ago = now - timedelta(hours=24)

    if SmsCode.objects.filter(
        mobile=mobile,
        created_at__gte=now - timedelta(seconds=SEND_COOLDOWN_SECONDS),
    ).exists():
        raise SmsError(
            f"发送太频繁，请 {SEND_COOLDOWN_SECONDS} 秒后再试",
            ErrorCode.APP_SMS_RATE_LIMITED,
        )
    if (
        SmsCode.objects.filter(mobile=mobile, created_at__gte=day_ago).count()
        >= SEND_DAILY_LIMIT_PER_MOBILE
    ):
        raise SmsError("该手机号今日发送次数已用完", ErrorCode.APP_SMS_RATE_LIMITED)
    if request_ip and (
        SmsCode.objects.filter(request_ip=request_ip, created_at__gte=day_ago).count()
        >= SEND_DAILY_LIMIT_PER_IP
    ):
        raise SmsError("发送太频繁，请稍后再试", ErrorCode.APP_SMS_RATE_LIMITED)

    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    record = SmsCode.objects.create(
        mobile=mobile,
        code_hash=hash_token(code),
        purpose=purpose,
        expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
        request_ip=request_ip,
    )
    try:
        send_verification_code(mobile, code)
    except Exception:
        # 发送失败 ⇒ 删掉这条记录再抛：否则用户收不到码，却还要被 60 秒冷却挡住。
        SmsCode.objects.filter(pk=record.pk).delete()
        raise
    return code


def issue_login_code(*, mobile: str, request_ip: str | None = None) -> str:
    """发登录验证码。返回明文**只给测试用**（生产调用方不落日志）。"""
    return _issue_code(mobile=mobile, purpose=SmsCode.PURPOSE_LOGIN, request_ip=request_ip)


def consume_login_code(*, mobile: str, code: str) -> None:
    """校验登录验证码。**成功才标记已用**——失败不消耗记录，允许用户重试。"""
    now = timezone.now()
    record = (
        SmsCode.objects.filter(
            mobile=mobile,
            purpose=SmsCode.PURPOSE_LOGIN,
            used_at__isnull=True,
            expires_at__gt=now,
        )
        .order_by("-created_at")
        .first()
    )
    if record is None or not token_matches(str(code or ""), record.code_hash):
        if record is not None:
            # 失败计数（并发下用 F 表达式，不读改写）。到上限顺手作废这条码。
            SmsCode.objects.filter(pk=record.pk).update(attempts=F("attempts") + 1)
            if record.attempts + 1 >= MAX_VERIFY_ATTEMPTS:
                SmsCode.objects.filter(pk=record.pk, used_at__isnull=True).update(used_at=now)
        raise SmsError("验证码错误或已过期", ErrorCode.APP_SMS_CODE_INVALID)
    if record.attempts >= MAX_VERIFY_ATTEMPTS:
        # 已到失败上限的码，即使这一枪猜对了也不再放行（否则「先错 4 次再猜」照样能过）。
        raise SmsError("验证码错误或已过期", ErrorCode.APP_SMS_CODE_INVALID)
    # ⭐ 一次性必须是**原子**的：读后 `save` 下两个并发请求会都成功。
    updated = SmsCode.objects.filter(pk=record.pk, used_at__isnull=True).update(used_at=now)
    if not updated:
        raise SmsError("验证码错误或已过期", ErrorCode.APP_SMS_CODE_INVALID)
