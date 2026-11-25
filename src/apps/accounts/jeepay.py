"""Jeepay 聚合支付对接（充值，D-0269 P2 第二批）。自己实现而不引 SDK：官方 SDK 是 Java 的，协议只有**MD5 签名**与 `POST /api/pay/unifiedOrder`，`httpx` + `hashlib` 不到 100 行（不为一个接口引大依赖）。签名（Jeepay 文档逐字）：取非空参数按参数名 ASCII 升序拼 `k1=v1&k2=v2…`、末尾拼 `&key=<商户私钥>`、MD5 后**转大写**；`sign` 自身不参与签名（回调验签同样先去掉）；⛔ 不要用真值判断金额：`amount` 单位是**分**，`0` 不合法但判据走显式校验。
失败判定（E-0087）：`code == 0` 才是成功，HTTP 200 但 `code != 0` 必须判失败；⚠️ **`code == 0` 也可能是失败**——上游「商户号该产品权限未开通」这类拒绝以 `data.errCode=NO_AUTH` / `errMsg` 回来（真商户号实测 `WX_APP` / `WX_H5` 即如此）⇒ 没有 `payDataType` 时把 `errCode/errMsg` 原样拼进错误消息，⛔ 不只报「没有返回支付参数」（那会让运维去查代码，真正的动作在商户平台）。代付（`create_transfer` / `query_transfer`）复用 `sign_params` / `_config`；⚠️⭐ **代付成功判据分两层**：`code == 0` 只表示**发起成功**，`data.state == 2` 才是转账成功（1 转账中 / 3 失败 / 4 关闭）⇒ state 判据在钱包层，不在本模块。
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal

import httpx
from django.conf import settings

from apps.core.error_codes import ErrorCode

#: 支付方式（Jeepay 的 `wayCode`）。⭐ 业务方拿真商户号逐条实测过中文含义与客户端拿到的 `payDataType`，⛔ 别只按英文码猜：`ALI_APP`（支付宝 **APP 支付** ⇒ `aliapp`，`payData` 是唤起支付宝 App 的支付串）、`ALI_QR`（支付宝**扫码 / 当面付** ⇒ `codeUrl`，`https://qr.alipay.com/...`）、`WX_NATIVE`（微信**原生扫码** ⇒ `codeUrl`，`weixin://wxpay/bizpayurl?pr=...`）——后两者的码 / 链接都由客户端自己画 / 打开；
#: `WX_APP`（微信 **APP 支付** ⇒ `wxapp`）⚠️ 实测这个商户号**该产品权限未开通**（`errCode=NO_AUTH`），仍留在支持列表里（客户端可以选、上游会拒），失败按 `data.errCode/errMsg` 原样报给客户端；`WX_H5` 同样未开通、本批**不接**（没有对应的 App 使用场景）；
#: ⭐ `WX_LITE`（微信**小程序**支付）⚠️ 与 `WX_JSAPI`（公众号）**不是**同一种：openid 按 AppID 隔离，小程序那一串只能由小程序的 `code2session` 换到；它走**另一个 Jeepay 应用**（`JEEPAY_MINI_APP_ID` / `JEEPAY_MINI_APP_KEY`），`channelExtra` 必须是 JSON 字符串 `{"openid":"..."}`（Jeepay 用 `JSONObject.parseObject(channelExtra, WxLiteOrderRQ.class)` 解，⛔ 传 dict 会签成 repr）。
WAY_WX_APP = "WX_APP"
WAY_ALI_APP = "ALI_APP"
WAY_ALI_QR = "ALI_QR"
WAY_WX_NATIVE = "WX_NATIVE"
WAY_WX_LITE = "WX_LITE"
SUPPORTED_WAYS = {WAY_ALI_APP, WAY_ALI_QR, WAY_WX_NATIVE, WAY_WX_APP, WAY_WX_LITE}


class JeepayError(Exception):
    """Jeepay 的预期失败（未配置 / 上游拒绝 / 网络）。调用方翻成业务码，⛔ 不 500。"""

    def __init__(self, message, code=ErrorCode.BUSINESS):
        super().__init__(message)
        self.code = code


def _config(*, require_notify: bool = True) -> dict:
    """读配置并校验缺项。
    ⚠️ `require_notify=False` 是给**代付**用的：代付不依赖回调（一律以查单为准）⇒ 只开提现的机器不该因为没配 `JEEPAY_NOTIFY_URL` 就调不通；充值（`create_payment` / 回调）仍走默认值 `True`，一个字不变。
    """
    conf = {
        "gateway": (getattr(settings, "JEEPAY_GATEWAY", "") or "").rstrip("/"),
        "mch_no": getattr(settings, "JEEPAY_MCH_NO", "") or "",
        "app_id": getattr(settings, "JEEPAY_APP_ID", "") or "",
        "key": getattr(settings, "JEEPAY_KEY", "") or "",
        "notify_url": getattr(settings, "JEEPAY_NOTIFY_URL", "") or "",
    }
    required = ("gateway", "mch_no", "app_id", "key")
    if require_notify:
        required = required + ("notify_url",)
    missing = [k for k in required if not conf[k]]
    if missing:
        raise JeepayError(
            "Jeepay 未配置，缺：" + "、".join(missing) + "（见 settings.JEEPAY_*）",
            ErrorCode.BUSINESS,
        )
    return conf


def ensure_configured() -> None:
    """配置自检（缺项抛 `JeepayError`）。
    ⚠️ 抽成公开函数是给 `wallet.start_transfer` 用的：必须在**改状态之前**知道「这台机器根本调不通 Jeepay」，否则一张单会被推进 `paying` 却从来没发出去（只能人工查单 / 登记打款收场）。本函数不做网络请求；走 `require_notify=False`（代付不依赖回调，缺 `JEEPAY_NOTIFY_URL` 不该挡住「只开提现」的机器）。
    """
    _config(require_notify=False)


def sign_params(params: dict, key: str) -> str:
    """按 Jeepay 规则算签名（参数里已有的 `sign` 先剔除）。"""
    items = sorted(
        (k, v)
        for k, v in params.items()
        if k != "sign" and v is not None and str(v) != ""
    )
    raw = "&".join(f"{k}={v}" for k, v in items) + f"&key={key}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _notify_app_keys() -> dict:
    """`appId → 签名密钥`。⚠️ 小程序支付是**另一个 Jeepay 应用**（独立 appId / appKey）。
    Jeepay 给商户的回调是用**该订单所属应用**的 appSecret 签的（`PayMchNotifyService.createNotifyUrl` → `queryMchApp(mchNo, payOrder.appId).getAppSecret()`，`payOrder.appId` 即下单时传的 `mchApp.getAppId()`）⇒ WX_LITE 的单回调带的是 `JEEPAY_MINI_APP_ID`、用 `JEEPAY_MINI_APP_KEY` 签。⛔ 只认公众号那一把钥匙会让小程序付的钱永远入不了账（回调全判签名失败）。
    """
    pairs = {}
    for app_id_name, key_name in (
        ("JEEPAY_APP_ID", "JEEPAY_KEY"),
        ("JEEPAY_MINI_APP_ID", "JEEPAY_MINI_APP_KEY"),
    ):
        app_id = str(getattr(settings, app_id_name, "") or "")
        key = str(getattr(settings, key_name, "") or "")
        if app_id and key:
            pairs.setdefault(app_id, key)
    return pairs


def verify_notify(params: dict) -> bool:
    """回调验签。⛔ 不用 `==`（常量时间比较）。
    ⚠️ 按回调里的 `appId` 选**那一把**密钥（公众号 / 小程序是两个 Jeepay 应用）；`appId` 缺失时退回公众号那把——上线时的既有行为，⛔ 不要顺手改掉。
    """
    provided = str(params.get("sign") or "")
    if not provided:
        return False
    keys = _notify_app_keys()
    app_id = str(params.get("appId") or "")
    if not app_id:
        app_id = str(getattr(settings, "JEEPAY_APP_ID", "") or "")
    key = keys.get(app_id)
    if not key:
        return False
    return hmac.compare_digest(sign_params(params, key), provided)


def create_payment(
    *,
    mch_order_no: str,
    amount: Decimal,
    way_code: str,
    subject: str,
    body: str,
    channel_extra: str | None = None,
    app_id: str | None = None,
    app_key: str | None = None,
    mch_no: str | None = None,
) -> dict:
    """统一下单。返回 Jeepay 的 `data`（含 `payDataType` / `payData`）。`amount` 是**元**，这里换成**分**（Jeepay 只收整数分）。
    ⚠️ `app_id` / `app_key` / `mch_no` 是给**小程序支付**用的：它在 Jeepay 里是另一个应用（甚至另一个商户），网关 / 回调地址仍沿用 `JEEPAY_*`。⚠️ `channel_extra` **必须是 JSON 字符串**：Jeepay 侧按字符串解析（`UnifiedOrderRQ` → `JSONObject.parseObject(channelExtra, WxLiteOrderRQ.class)`），而 `sign_params` 对值走 `str(v)` ⇒ 传 dict 会签成 Python repr、验签失败。
    """
    conf = _config()
    if app_id is not None:
        conf["app_id"] = app_id
    if app_key is not None:
        conf["key"] = app_key
    if mch_no:
        # 小程序那个应用可能挂在**另一个 Jeepay 商户**下 ⇒ 商户号要能单独给。
        conf["mch_no"] = mch_no
    if channel_extra is not None and not isinstance(channel_extra, str):
        raise JeepayError("channelExtra 必须是 JSON 字符串", ErrorCode.VALIDATION)
    if way_code not in SUPPORTED_WAYS:
        raise JeepayError(
            f"不支持的支付方式：{way_code}"
            f"（只支持 {'/'.join(sorted(SUPPORTED_WAYS))}）",
            ErrorCode.VALIDATION,
        )
    cents = int((amount * 100).to_integral_value())
    if cents <= 0:
        raise JeepayError("充值金额必须大于 0", ErrorCode.VALIDATION)

    payload = {
        "mchNo": conf["mch_no"],
        "appId": conf["app_id"],
        "mchOrderNo": mch_order_no,
        "wayCode": way_code,
        "amount": cents,
        "currency": "cny",
        "subject": subject[:64],
        "body": body[:256],
        "notifyUrl": conf["notify_url"],
        "reqTime": str(int(time.time())),
        "version": "1.0",
        "signType": "MD5",
    }
    if channel_extra is not None:
        payload["channelExtra"] = channel_extra
    payload["sign"] = sign_params(payload, conf["key"])

    try:
        response = httpx.post(
            f"{conf['gateway']}/api/pay/unifiedOrder",
            json=payload,
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise JeepayError(f"Jeepay 请求失败：{exc}") from exc

    try:
        result = response.json()
    except ValueError as exc:
        raise JeepayError(f"Jeepay 返回非 JSON（HTTP {response.status_code}）") from exc

    # ⚠️ 成功判定只认 `code == 0`（E-0087：HTTP 200 不代表成功）。
    # ⚠️ 上游可能回 `0` 或 `"0"`（Java 侧类型不同）⇒ 统一按字符串比，⛔ 别用 `!= 0`。
    if str(result.get("code")) != "0":
        raise JeepayError(
            f"Jeepay 下单失败：{result.get('msg') or '未知错误'}"
            f"（code={result.get('code')}）"
        )
    data = result.get("data")
    if isinstance(data, str):
        # Jeepay 文档里 `data` 有时是 JSON 字符串（示例如此），两种都接住。
        try:
            data = json.loads(data)
        except ValueError:
            pass
    if not isinstance(data, dict) or not data.get("payDataType"):
        # ⚠️ 上游 `code == 0` 但没给支付参数：真实原因在 `data.errCode` / `data.errMsg` 里
        # （实测 微信 APP 支付未开通 ⇒ `errCode=NO_AUTH`「商户号该产品权限未开通…」）。
        # ⛔ 别只报「没有返回支付参数」——那句话让运维去查代码，而真正的动作在商户平台。
        detail = ""
        if isinstance(data, dict):
            code = data.get("errCode") or ""
            msg = data.get("errMsg") or ""
            if code or msg:
                detail = f"（上游 {code}：{msg}）"
        raise JeepayError(f"Jeepay 下单成功但没有返回支付参数{detail}")
    return data


def create_wx_lite_payment(
    *, mch_order_no: str, amount: Decimal, subject: str, body: str, openid: str
) -> dict:
    """微信**小程序**下单（`wayCode=WX_LITE`）。返回 Jeepay 的 `data`。
    ⚠️ 用「小程序支付」那个 Jeepay 应用的 `appId` / `appKey`（`JEEPAY_MINI_*`）；`channelExtra` 逐字是 `{"openid":"..."}` 的 **JSON 字符串**（见模块常量注释）。⚠️ 出参的 `payDataType` **原样透传**：开源版 Jeepay 的 `WxLiteOrderRS.buildPayDataType()` 返回 `CS.PAY_DATA_TYPE.WX_APP`（值 `"wxapp"`），客户端按它 + `payData` 唤起 `wx.requestPayment`；⛔ 本模块不替上游改写这个字符串。
    """
    openid = (openid or "").strip()
    if not openid:
        raise JeepayError("缺少微信 openid，请在小程序内重新进入充值", ErrorCode.VALIDATION)
    app_id = str(getattr(settings, "JEEPAY_MINI_APP_ID", "") or "")
    app_key = str(getattr(settings, "JEEPAY_MINI_APP_KEY", "") or "")
    missing = [
        name
        for name, value in (
            ("JEEPAY_MINI_APP_ID", app_id),
            ("JEEPAY_MINI_APP_KEY", app_key),
        )
        if not value
    ]
    if missing:
        raise JeepayError(
            "小程序支付未配置，缺：" + "、".join(missing) + "（见 settings.JEEPAY_MINI_*）",
            ErrorCode.BUSINESS,
        )
    channel_extra = json.dumps({"openid": openid}, separators=(",", ":"))
    return create_payment(
        mch_order_no=mch_order_no,
        amount=amount,
        way_code=WAY_WX_LITE,
        subject=subject,
        body=body,
        channel_extra=channel_extra,
        app_id=app_id,
        mch_no=str(getattr(settings, "JEEPAY_MINI_MCH_NO", "") or "") or None,
        app_key=app_key,
    )


# ── 支付宝代付（打手提现审核线）─────────────────────────────
# ⚠️ 上游有**两条不同的转账 API**（计划 §2）：A. `POST /api/transferOrder` = 商户 → 外部支付宝/银行卡账户 ← 本模块用这条；B. `POST /api/wallet/transfer` = 计全付钱包用户互转 ← 不适用（我们没有钱包账户）。
# ⭐⭐ **成功判据分两层，⛔ 别混成一层**（计划 §2 / E-0087 同族）：① 本模块只认 `code == 0`（= 发起成功），非 0 一律 `JeepayError`；② `code == 0` **不等于转账成功**——上游文档写明 `data.state`：0 生成 / 1 转账中 / **2 成功** / 3 失败 / 4 关闭，`state=1` 必须再调查单确认 ⇒ 本模块**只把 `data` 原样交出去**，`state` 判据在钱包层（`wallet.py`）；把 `code == 0` 当打款成功的实现要能被判红（计划 §7 第 1 条）。


def _transfer_if_code() -> str:
    """上游代付通道码。
    ⚠️ 它属于「那台 Jeepay 实例配了什么通道」⇒ 落 settings（`JEEPAY_TRANSFER_IF_CODE`），换环境改 `.env` 即可，⛔ 不改代码（计划 §10 步骤 0 的实测项之一）。
    """
    return (getattr(settings, "JEEPAY_TRANSFER_IF_CODE", "") or "alipay").strip()


def _transfer_entry_type() -> str:
    """支付宝代付的入账类型（settings `JEEPAY_TRANSFER_ENTRY_TYPE`，默认 `ALIPAY_CASH`）。"""
    return (getattr(settings, "JEEPAY_TRANSFER_ENTRY_TYPE", "") or "ALIPAY_CASH").strip()


def _post_transfer_api(path: str, payload: dict, conf: dict) -> dict:
    """代付两个端点共用的「签名 → POST → 判 `code` → 解 `data`」那一段。
    ⚠️ 与 `create_payment` 的后半段**同一条判据**（只认 `code == 0`），抽出来只为一件事：两个转账端点不许各写一份成功判定（两处各写一遍必然漂，E-0021 同族）。
    """
    payload = dict(payload)
    payload["sign"] = sign_params(payload, conf["key"])
    try:
        response = httpx.post(f"{conf['gateway']}{path}", json=payload, timeout=10)
    except httpx.HTTPError as exc:
        raise JeepayError(f"Jeepay 请求失败：{exc}") from exc

    try:
        result = response.json()
    except ValueError as exc:
        # 非 JSON 一律**不采信**（计划 §7 第 7 条）：调用方据此不落 paid。
        raise JeepayError(f"Jeepay 返回非 JSON（HTTP {response.status_code}）") from exc

    if not isinstance(result, dict):
        raise JeepayError("Jeepay 返回的结构不是对象")
    if str(result.get("code")) != "0":
        raise JeepayError(
            f"Jeepay 代付失败：{result.get('msg') or '未知错误'}"
            f"（code={result.get('code')}）"
        )
    data = result.get("data")
    if isinstance(data, str):
        # 与 `create_payment` 同款：`data` 有时是 JSON 字符串。
        try:
            data = json.loads(data)
        except ValueError:
            pass
    if not isinstance(data, dict):
        # `code == 0` 却没有 `data`：连 `state` 都读不到 ⇒ 不能当成功（E-0087）。
        raise JeepayError("Jeepay 代付返回成功但缺少 data")
    return data


def create_transfer(
    *, mch_order_no: str, amount: Decimal, account_no: str, account_name: str,
    desc: str, client_ip: str,
) -> dict:
    """发起支付宝代付。返回 Jeepay 的 `data`（含 `state` / `transferId`）。`amount` 是**元**，这里换成**分**（Jeepay 只收整数分）。
    ⚠️ 本函数的返回**只证明「发起成功」**（`code == 0`）；`data["state"]` 才是转账状态，由 `wallet.start_transfer` 判（见模块「成功判据分两层」）。
    """
    conf = _config(require_notify=False)
    cents = int((amount * 100).to_integral_value())
    if cents <= 0:
        raise JeepayError("代付金额必须大于 0", ErrorCode.VALIDATION)

    payload = {
        "mchNo": conf["mch_no"],
        "appId": conf["app_id"],
        "mchOrderNo": mch_order_no,
        "amount": cents,
        "currency": "cny",
        "ifCode": _transfer_if_code(),
        "entryType": _transfer_entry_type(),
        "accountNo": account_no,
        "accountName": account_name,
        "transferDesc": desc[:128],
        # ⚠️ 本批**不依赖回调**（SDK 示例没有传 notifyUrl）⇒ 一律以查单为准（计划 §2）。
        "clientIp": client_ip,
        "reqTime": str(int(time.time())),
        "version": "1.0",
        "signType": "MD5",
    }
    return _post_transfer_api("/api/transferOrder", payload, conf)


def query_transfer(mch_order_no: str) -> dict:
    """查代付单（`mchOrderNo` 或 `transferId` 二选一；本批只用 `mchOrderNo`）。"""
    conf = _config(require_notify=False)
    payload = {
        "mchNo": conf["mch_no"],
        "appId": conf["app_id"],
        "mchOrderNo": mch_order_no,
        "reqTime": str(int(time.time())),
        "version": "1.0",
        "signType": "MD5",
    }
    return _post_transfer_api("/api/transfer/query", payload, conf)
