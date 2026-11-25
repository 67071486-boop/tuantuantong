r"""短信发送与验证码（打手 App 线）。## 为什么是一个独立模块发送通道（服务商）与验证码业务（生成 / 限流 / 校验）是两件事：前者会随商务选择变，后者是业务判据。本模块把前者收成一个函数，按 `settings.SMS_BACKEND` 分发；后者在 `apps/accounts/app_sms.py`，只依赖这里——换服务商不动业务代码。## 后端- `console`（默认）：把验证码打印到日志。**开发 / 测试用**，不花钱、不需要凭证。- `tencent`：
腾讯云短信 `SendSms`（版本 2021-01-11）。签名用云 API 3.0 的**TC3-HMAC-SHA256** 自己算（不引 SDK：`tencentcloud-sdk-python` 是全集包，为了一个`SendSms` 引一大坨依赖不值；算法见腾讯云文档「签名方法 v3」，本文件逐行对应）。⚠️ 它要 6 项 env（见 `settings.SMS_TENCENT_*`）；
缺任何一项都**显式报错**，⛔ 不静默成功——静默会让"短信没到"伪装成"验证码错了"。## 手机号格式统一按 11 位大陆手机号校验（`^1[3-9]\d{9}$`）。号码本身**不是**密钥，但它是个人敏感信息：日志里只打后四位（`138****1234`），避免日志变成号码库。## 与 `mashanglai-thirdparty` 的关系那边的 `HrSmsService`（Java）是同一家服务商、同一套参数的参考实现；本文件是它在Django 里的等价物——口径一致（签名内容而非签名 ID、模板变量个数必须与模板一致），但不共用代码（语言不同）。"""
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

#: 大陆手机号（11 位，1 开头，第二位 3–9）。⛔ 不接海外号——本产品只服务国内打手。
MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

#: 验证码长度与有效期（秒）。业务方口径见计划 §3.2。
CODE_LENGTH = 6
CODE_TTL_SECONDS = 300

#: 限流窗口与额度（计划 §3.2）。
SEND_COOLDOWN_SECONDS = 60
SEND_DAILY_LIMIT_PER_MOBILE = 10
SEND_DAILY_LIMIT_PER_IP = 50

#: 腾讯云短信 API（云 API 3.0）。
TENCENT_HOST = "sms.tencentcloudapi.com"
TENCENT_SERVICE = "sms"
TENCENT_ACTION = "SendSms"
TENCENT_VERSION = "2021-01-11"
TENCENT_DEFAULT_REGION = "ap-guangzhou"


def normalize_mobile(raw) -> str:
    """去空白与 `+86` 前缀；不合法返回空串（调用方据此回校验失败，⛔ 不抛异常）。"""
    text = str(raw or "").strip().replace(" ", "").replace("-", "")
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("86") and len(text) == 13:
        text = text[2:]
    return text if MOBILE_RE.match(text) else ""


def mask_mobile(mobile: str) -> str:
    """日志用：只留前三位与后四位。"""
    return f"{mobile[:3]}****{mobile[-4:]}" if len(mobile) == 11 else "***"


def send_verification_code(mobile: str, code: str) -> None:
    """发一条验证码短信。**失败要抛**——调用方据此回业务错误，⛔ 不许吞掉后假装成功。"""
    backend = getattr(settings, "SMS_BACKEND", "console")
    if backend == "console":
        # ⚠️ 生产配成 console = 打手永远收不到验证码，而接口看起来一切正常（评审 Blocking 1）。
        # 所以 DEBUG=False 时**直接失败**，⛔ 不许静默降级。
        if not settings.DEBUG:
            raise ImproperlyConfigured(
                "生产环境不允许 SMS_BACKEND=console：验证码不会真正发出。请设 SMS_BACKEND=tencent。"
            )
        # `mobile` 已脱敏：日志里的号码不该成为一份可导出的号码库（与 PII 守卫同一条纪律）。
        logger.info("[sms:console] to=%s code=%s", mask_mobile(mobile), code)
        return
    if backend == "tencent":
        _tencent_send_code(mobile, code)
        return
    raise ImproperlyConfigured(f"未知的 SMS_BACKEND：{backend!r}（只认 console / tencent）")


# ── 腾讯云 ────────────────────────────────────────────────────────
def _tencent_config():
    """读取并校验六项配置。缺项 = 配置错误（响亮），⛔ 不是「发不出去就跳过」。"""
    conf = getattr(settings, "SMS_TENCENT", None) or {}
    missing = [k for k in ("secret_id", "secret_key", "sdk_app_id", "sign_name", "template_id") if not conf.get(k)]
    if missing:
        raise ImproperlyConfigured(
            "腾讯云短信配置不完整，缺：" + "、".join(missing) + "（见 settings.SMS_TENCENT_*）"
        )
    return conf


def _tencent_send_code(mobile: str, code: str) -> None:
    conf = _tencent_config()
    payload = {
        "PhoneNumberSet": [f"+86{mobile}"],
        "SmsSdkAppId": str(conf["sdk_app_id"]),
        # ⚠️ 腾讯云这里要的是**签名内容**（如「代代通」），⛔ 不是控制台里的签名 ID。
        "SignName": str(conf["sign_name"]),
        "TemplateId": str(conf["template_id"]),
        # 验证码模板只有一个变量；个数与模板不一致会回 FailedOperation.TemplateParamSetNotMatchApprovedTemplate。
        "TemplateParamSet": [code],
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = _tencent_headers(
        body, conf["secret_id"], conf["secret_key"], conf.get("region") or TENCENT_DEFAULT_REGION
    )
    try:
        response = httpx.post(
            f"https://{TENCENT_HOST}",
            content=body.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
    except httpx.HTTPError as exc:  # 网络层：超时 / DNS / 连接被拒
        raise RuntimeError(f"腾讯云短信请求失败：{exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"腾讯云短信返回非 JSON（HTTP {response.status_code}）") from exc

    result = data.get("Response") or {}
    if result.get("Error"):
        err = result["Error"]
        raise RuntimeError(
            f"腾讯云短信失败：{err.get('Code')} {err.get('Message')}"
            f"（RequestId={result.get('RequestId')}）"
        )
    statuses = result.get("SendStatusSet") or []
    if not statuses:
        raise RuntimeError(f"腾讯云短信没有返回发送状态（RequestId={result.get('RequestId')}）")
    first = statuses[0]
    # ⚠️ 判定按**这一行自己的 Code**，不看 HTTP 状态：腾讯云失败也回 200
    # （与 E-0087 同一条纪律——写接口不能拿"没抛异常"当成功）。
    if first.get("Code") != "Ok":
        raise RuntimeError(
            f"腾讯云短信失败：{first.get('Code')} {first.get('Message')}"
            f"（RequestId={result.get('RequestId')}）"
        )


def _tencent_headers(body: str, secret_id: str, secret_key: str, region: str) -> dict:
    """云 API 3.0 的 TC3-HMAC-SHA256 签名（腾讯云文档「签名方法 v3」的逐行实现）。"""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, dt_timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"

    # 1) 规范请求串。`host` 必须与请求头一致；排序后的 `content-type;host` 是参与签名的头。
    canonical_headers = f"content-type:{content_type}\nhost:{TENCENT_HOST}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
    # ⚠️ `canonical_headers` **自带结尾 `\n`**，而腾讯云的规范是 `CanonicalHeaders + '\n' + SignedHeaders`（文档「签名方法 v3」）⇒ 这两段之间**确实是两个换行**，`"\n".join` 正好得到这个形状。
    # ⛔ 别「修」成手工拼接的单换行：实测那样会 `AuthFailure.SignatureFailure`（用官方 SDK 对照才定位到）。
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, hashed_payload]
    )

    # 2) 待签字符串。
    credential_scope = f"{date}/{TENCENT_SERVICE}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    # 3) 逐级派生签名密钥。
    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    secret_date = _hmac(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac(secret_date, TENCENT_SERVICE)
    secret_signing = _hmac(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": TENCENT_HOST,
        "X-TC-Action": TENCENT_ACTION,
        "X-TC-Version": TENCENT_VERSION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Region": region,
    }