"""打手钱包端点（`/api/app/wallet/*`）。读的是「我自己的钱包」：所有端点都从 `request.user` 取人，⛔ 不接受 `user_id` / 租户标识参数（`AGENTS.md` §4）；钱包表在租户库 ⇒ 跨租户天然不可达。
金额一律以**字符串**出参——这是**实现保证**、不是框架默认：`api_response()` 直接走 DRF 的 JSONEncoder，`Decimal` 会被 `float()` 成二进制浮点（实测 `Decimal("0.10")` → `0.1`）⇒ 本模块显式 `str(...)`；客户端按字符串做精确比较 / 展示，⛔ 不要在 App 里 `parseFloat` 后再相加。
"""
import logging
import secrets
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.http import HttpResponse
from django.utils import timezone

from rest_framework import serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.app_auth_views import AppPlaneMixin, fixed_window_allowed
from apps.accounts.jeepay import (
    JeepayError,
    WAY_WX_LITE,
    create_payment,
    create_wx_lite_payment,
    verify_notify,
)
from apps.accounts.models import RechargeOrder, StaffProfile, WalletTransaction
from apps.accounts.qr import qr_png_base64
from apps.accounts.wallet import (
    RELEASE_HOLD_DAYS,
    WalletError,
    credit_recharge,
    deposit_amounts_for_user,
    get_wallet,
    release_available_at,
    request_release,
    request_withdraw,
    settle_release_if_due,
)
from apps.accounts.wx_mini import WxMiniError, ensure_configured, exchange_code
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.deposit_gate import deposit_enforced_for
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.openapi.authentication import client_ip

logger = logging.getLogger(__name__)

#: `POST /api/app/wallet/wx-openid/` 的 IP 额度（默认 **60 次/小时**）。
#: ⚠️ 与注册那两个端点同一形状：阈值在函数体里 `getattr(settings, …)` 读（否则
#: `override_settings` 不生效），读不到就用这里的默认值（⛔ 不抛：缺配置不该打挂端点）。
DEFAULT_WX_OPENID_MAX_REQUESTS = 60
DEFAULT_WX_OPENID_WINDOW_SECONDS = 3600
#: 超额文案与注册那条逐字一致（同一类拒绝：来源 IP 请求过于频繁）。
WX_OPENID_RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"
#: openid 缺失 / 不属于当前小程序时给客户端的那句话——用户的动作只有「重新进小程序」。
WX_OPENID_MISSING_MSG = "请在小程序内重新进入充值"


class WxOpenidRequestSerializer(serializers.Serializer):
    """`{code}`：`wx.login()` 拿到的一次性 code。"""

    code = serializers.CharField(max_length=128)


class WithdrawRequestSerializer(serializers.Serializer):
    """提现入参。**只收字符串或数字**，解析后必须是有限、最多两位小数的正数。
    ⚠️ 用 `CharField` 而不是 `DecimalField`：DRF 的 `DecimalField` 会把多余精度**四舍五入**（钱不能静默变），本序列化器选择**拒绝**。
    """

    amount = serializers.CharField(max_length=32)

    def validate_amount(self, value):
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise serializers.ValidationError("金额格式不正确")
        if not amount.is_finite():
            raise serializers.ValidationError("金额格式不正确")
        if amount.as_tuple().exponent < -2:
            raise serializers.ValidationError("金额最多两位小数")
        if amount <= 0:
            raise serializers.ValidationError("金额必须大于 0")
        if amount >= Decimal("10000000000"):
            raise serializers.ValidationError("金额超出范围")
        return amount


def _first_error(serializer) -> str:
    for errors in serializer.errors.values():
        if errors:
            return str(errors[0])
    return "参数不正确"


def _money(value) -> str:
    """金额出参的**唯一格式化点**：两位小数、字符串。
    ⚠️ ⛔ 不要退回 `str(value)`：`Decimal("0")` 会变成 `"0"`，前端按固定两位展示时缺角；`f"{value:.2f}"` 对 0 / 整数 / 两位小数都稳定。
    """
    return f"{value:.2f}"


def _wallet_payload(wallet):
    """钱包出参。⚠️ 已有三个键**含义一个字不改**，解冻冷静期只新增三个键：`release_pending`（有没有一笔**未到期**的解冻申请，钱仍在 `frozen` 里）、`release_available_at`（到期时刻；没申请 / 已结算 ⇒ `None`——⚠️ 走 `local_iso` 显示口径，⛔ 不是裸 `isoformat()` 的 UTC 串，App 按串里的月日直接展示「X 月 X 日到账」）、`release_hold_days`（冷静期天数，唯一出处 `wallet.RELEASE_HOLD_DAYS`，⛔ 前端别写死）。
    """
    return {
        "balance": _money(wallet.balance),
        "frozen": _money(wallet.frozen),
        # ⚠️ D-0302：App 截串直显 ⇒ 手拼时刻一律 `local_iso`（+08:00），
        # 与下面的 `release_available_at` 同一条出口。
        "updated_at": local_iso(wallet.updated_at),
        "release_pending": wallet.release_requested_at is not None,
        "release_available_at": local_iso(release_available_at(wallet)),
        "release_hold_days": RELEASE_HOLD_DAYS,
    }


def _deposit_qr_png_base64(pay_data_type, pay_data):
    """`codeUrl` 时把支付链接画成 PNG；其余情况 / 生成失败 ⇒ `None`（**不加**这个字段）。
    `aliapp` / `wxapp` 是给各自 SDK 的**支付串**，不是链接 ⇒ 不出图；`codeUrl`（`ALI_QR` / `WX_NATIVE`）才是可扫的链接。⚠️ **生成失败不许 500**：一张图的失败不能把「已下单成功、`pay_data` 可用」的响应打成错误（客户端有兜底：没 `qr_png_base64` 就当老版本自己画）。⛔ **`pay_data` 不进日志**：它是可付款凭证（带签名 / 订单参数），失败只记异常**类名**、连消息也不记（消息里可能带输入片段）。
    """
    if pay_data_type != "codeUrl" or not isinstance(pay_data, str) or not pay_data:
        return None
    try:
        return qr_png_base64(pay_data)
    except Exception as exc:  # noqa: BLE001 —— 有意吞掉：图失败不能拖垮下单响应
        logger.warning("充值二维码生成失败，已退回无 qr_png_base64 的响应：%s", type(exc).__name__)
        return None


class AppWalletView(AppPlaneMixin, APIView):
    """我的钱包：余额、冻结额、以及每款游戏要求的押金（用来告诉打手「还差多少」）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # ⭐ 解冻冷静期：**先结算再读**（事务 + 行锁）。到期的那笔在这里
        # 从 `frozen` 转回 `balance`，所以截图里的「余额」永远是结算后的真值。
        wallet = settle_release_if_due(request.user)
        profile = StaffProfile.objects.filter(user=request.user).first()
        my_games = (
            list(profile.games.filter(deleted_at__isnull=True).order_by("id"))
            if profile
            else []
        )
        # ⭐ 个人押金额度：批量取本品游戏的要求金额（开关开 ⇒ 个人额度、缺行 0；
        # 开关关 ⇒ 全局），⛔ 不在这里另写一份取值顺序、也不逐款查库。
        amounts = deposit_amounts_for_user(request.user, my_games)
        rules = [
            {
                "game_id": game.id,
                "game_name": game.name,
                "amount": _money(amounts[game.pk]),
            }
            for game in my_games
        ]
        # `deposit_enforced`：**这个客户端**的接单要不要先冻押金（App 专属闸，业务方）。
        # 前端拿它决定「接单前要不要弹冻结确认」——⛔ 不要让 App 自己猜开关状态。
        return api_response(
            {
                **_wallet_payload(wallet),
                "deposit_rules": rules,
                "deposit_enforced": deposit_enforced_for(request),
                # ⛔ **前三单快路已下线**（spec §8.2）：这个键**保留**、恒 `"0.00"`——老版本 App 靠它决定提现弹层那句文案，删键会让旧客户端报「字段缺失」或显示未定义；而它一律为 0 时老客户端自然走普通路（「审核通过后打款」）。⚠️ ⛔ 不要改成「按额度算」：那条算法已整段删除（见 `wallet.py` 的注释）。
                "fast_lane_remaining": "0.00",
            }
        )


class AppWalletReleaseView(AppPlaneMixin, APIView):
    """**申请**解冻押金（现口径是「申请」：钱仍冻着，满 7 天才回余额）。
    业务拒绝（还有进行中的单 / 没有可解冻的钱）走业务码，HTTP 仍 200；已有一笔未到期申请时**幂等成功**（⛔ 不重复写流水、⛔ 不重置计时）。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            wallet, created = request_release(request.user)
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        # ⭐ 审计（D-0269 第二批）：解冻是**动钱**的动作，必须留痕。
        # ⚠️ 只有**新申请**才记一条：重复点击是幂等返回，不是一个新事件（与流水同口径）。
        # ⚠️ 金额取 `frozen`：申请时它一分未动，所以这就是被申请的那笔押金。
        if created:
            record(
                log_type=ACTION_LOG_TYPES["wallet.release"],
                action="wallet.release",
                actor=request.user.username,
                actor_id=request.user.pk,
                target_kind="taker_wallet",
                target_id=str(wallet.pk),
                target_name=request.user.username,
                to_value=_money(wallet.frozen),
            )
        return api_response(_wallet_payload(wallet))


class AppWalletWithdrawView(AppPlaneMixin, APIView):
    """申请提现（人工审核 + 人工打款）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not isinstance(request.data, dict):
            return api_response(None, msg="参数不正确", code=ErrorCode.VALIDATION)
        serializer = WithdrawRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        try:
            order = request_withdraw(request.user, serializer.validated_data["amount"])
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        # ⭐ 审计（D-0269 第二批）：提现申请当场扣余额 ⇒ 必须留痕。
        record(
            log_type=ACTION_LOG_TYPES["wallet.withdraw"],
            action="wallet.withdraw",
            actor=request.user.username,
            actor_id=request.user.pk,
            target_kind="withdraw_order",
            target_id=str(order.pk),
            target_name=request.user.username,
            to_value=_money(order.amount),
            reason=f"W{order.pk}",
        )
        return api_response(
            {
                "id": order.pk,
                "amount": _money(order.amount),
                "state": order.state,
                "alipay_account": order.alipay_account,
                "alipay_name": order.alipay_name,
                # ⭐ 快路：`true` = 已自动通过、跳过冷静期。
                # ⚠️ D-0302：App 截串直显 ⇒ 时刻键一律 `local_iso`（+08:00）。
                "fast_lane": order.fast_lane,
                "payout_after": local_iso(order.payout_after),
            }
        )


class AppWithdrawListView(AppPlaneMixin, APIView):
    """我的提现记录（只读；创建走上面的 `withdraw/`）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = request.user.withdraw_orders.all()
        total = queryset.count()
        rows = list(queryset[:50])
        return api_response(
            paginated(
                [
                    {
                        "id": o.pk,
                        "amount": _money(o.amount),
                        "state": o.state,
                        "remark": o.remark,
                        # ⚠️ D-0302：App 截串直显 ⇒ 一律 `local_iso`（+08:00）。
                        "created_at": local_iso(o.created_at),
                        # ⭐ 快路标记与预计打款时间。
                        "fast_lane": o.fast_lane,
                        "payout_after": local_iso(o.payout_after),
                        # ⭐ 财务中断原因。
                        "interrupt_reason": o.interrupt_reason,
                    }
                    for o in rows
                ],
                total=total,
                page=1,
                page_size=50,
            )
        )


class AppWxOpenidView(AppPlaneMixin, APIView):
    """用 `wx.login()` 的 code 换小程序 openid 并记在当前账号上。
    **只回 `ok` / `bound`**，⛔ 不回显 openid（只用于付款、不认身份，回显没有用处）；微信 `errcode != 0` ⇒ 业务错误（带 errcode，⛔ 不带 errmsg / secret）；`WX_MINI_*` 未配置 ⇒ 「小程序支付未配置」（业务码，⛔ 不 500）；按来源 IP 固定窗口限流（复用 `fixed_window_allowed`，阈值 `APP_WX_OPENID_*`）；**不记操作日志**：这是「换一张付款用的凭证」，不是身份绑定，每次进小程序都会重新取（分类见 `test_operation_log_ledger`）。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        allowed = fixed_window_allowed(
            scope="wx-openid",
            ip=client_ip(request),
            limit=getattr(
                settings, "APP_WX_OPENID_MAX_REQUESTS", DEFAULT_WX_OPENID_MAX_REQUESTS
            ),
            window=getattr(
                settings, "APP_WX_OPENID_WINDOW_SECONDS", DEFAULT_WX_OPENID_WINDOW_SECONDS
            ),
        )
        if not allowed:
            return api_response(
                None,
                msg=WX_OPENID_RATE_LIMITED_MSG,
                code=ErrorCode.APP_REGISTER_RATE_LIMITED,
            )
        if not isinstance(request.data, dict):
            return api_response(None, msg="参数不正确", code=ErrorCode.VALIDATION)
        serializer = WxOpenidRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        try:
            session = exchange_code(serializer.validated_data["code"])
        except WxMiniError as exc:
            return api_response(None, msg=str(exc), code=exc.code)

        profile, _ = StaffProfile.objects.get_or_create(user=request.user)
        profile.wx_openid = session["openid"]
        profile.wx_openid_appid = session["app_id"]
        profile.save(update_fields=["wx_openid", "wx_openid_appid"])
        return api_response({"ok": True, "bound": True})


class AppWalletDepositView(AppPlaneMixin, APIView):
    """创建充值单并拿到 Jeepay 的支付参数。返回的 `payDataType` / `payData` 原样交给客户端，按 `pay_data_type` 分流：`aliapp`（支付宝 APP 支付串）⇒ 唤起支付宝；`codeUrl`（`ALI_QR` 支付宝扫码 / `WX_NATIVE` 微信原生扫码）⇒ 自己画二维码或打开链接；`wxapp`（⭐ `WX_LITE` 小程序支付在开源版 Jeepay 里也回 `wxapp`，`payData` 是 `wx.requestPayment` 的参数包）⇒ 唤起微信（⚠️ `WX_APP` 实测该商户号未开通）。⚠️ **出参形状没变**（共五种，含 `WX_LITE`），客户端只按 `pay_data_type` 分流、不需要为新增方式改解析。
    ⭐ 可选字段 `qr_png_base64`：**只在** `pay_data_type == "codeUrl"` 时出现——把支付链接画成 PNG（约 300×300、白边 2 模块、纠错 M），值是**裸 base64**，⛔ **不带** `data:image/png;base64,` 前缀（契约只有这一种写法，客户端自己拼）；`aliapp` / `wxapp` 没有这个字段（给 SDK 的支付串，不是可扫链接）；**生成失败 ⇒ 没有这个字段**，响应仍是成功（客户端兜底自己画）；⛔ 支付链接**不进日志**（可付款凭证），失败只记异常类名。⚠️ 充值金额**只在这里**落 `RechargeOrder`；钱包余额要等回调入账，⛔ 不能在这里先加钱。
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not isinstance(request.data, dict):
            return api_response(None, msg="参数不正确", code=ErrorCode.VALIDATION)
        serializer = WithdrawRequestSerializer(data={"amount": request.data.get("amount")})
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)

        # 支付方式：`way_code` 是本端点的**既有**字段名（App 在用）；`way` 是追加的等价别名（业务方口径里就叫 `way`）。两个都给时以 `way_code` 为准。⚠️ 合法值不在这里判：`jeepay.SUPPORTED_WAYS` 是它的**唯一出处**，非法值由 `create_payment` 抛 `JeepayError(VALIDATION)` ⇒ 200 + 业务码（⛔ 不 500），并照既有纪律把这张挂单标成 failed。
        raw_way = request.data.get("way_code") or request.data.get("way") or ""
        way_code = str(raw_way)[:16]

        # ⭐ 小程序支付（WX_LITE）：下单前必须已经有**本小程序**的 openid。
        # openid 按 AppID 隔离 ⇒ 记的 appid 与当前 `WX_MINI_APP_ID` 不一致就当没有
        # （换小程序 / 公众号那串 openid 一律不复用），让客户端重新走 `/wx-openid/`。
        wx_openid = ""
        if way_code == WAY_WX_LITE:
            try:
                mini_app_id = ensure_configured()
            except WxMiniError as exc:
                return api_response(None, msg=str(exc), code=exc.code)
            profile = StaffProfile.objects.filter(user=request.user).first()
            wx_openid = (profile.wx_openid or "") if profile else ""
            if not wx_openid or (profile.wx_openid_appid or "") != mini_app_id:
                return api_response(
                    None, msg=WX_OPENID_MISSING_MSG, code=ErrorCode.VALIDATION
                )

        # 订单号：R + 12 位时间 + 8 位随机（≤ 40，且同名不会撞 unique）。
        mch_order_no = "R" + timezone.now().strftime("%y%m%d%H%M%S") + secrets.token_hex(4)
        order = RechargeOrder.objects.create(
            user=request.user,
            amount=serializer.validated_data["amount"],
            mch_order_no=mch_order_no,
            way_code=way_code,
        )
        try:
            if way_code == WAY_WX_LITE:
                data = create_wx_lite_payment(
                    mch_order_no=mch_order_no,
                    amount=order.amount,
                    subject="押金充值",
                    body=f"打手押金充值 {order.amount} 元",
                    openid=wx_openid,
                )
            else:
                data = create_payment(
                    mch_order_no=mch_order_no,
                    amount=order.amount,
                    way_code=order.way_code,
                    subject="押金充值",
                    body=f"打手押金充值 {order.amount} 元",
                )
        except JeepayError as exc:
            # 下单失败：把这张待支付单关掉，别留一条永远不会支付的挂单。
            RechargeOrder.objects.filter(pk=order.pk, state=RechargeOrder.STATE_PENDING).update(
                state=RechargeOrder.STATE_FAILED
            )
            return api_response(None, msg=str(exc), code=exc.code)
        except Exception:
            # 非预期失败（编程错误 / DB / 未预料的异常）同样先把单作废，再让 500 响亮地冒上去
            # （评审 Minor 1：否则会留一条永远 pending 的挂单，且没人知道它为什么没支付）。
            RechargeOrder.objects.filter(pk=order.pk, state=RechargeOrder.STATE_PENDING).update(
                state=RechargeOrder.STATE_FAILED
            )
            raise
        # ⭐ 审计（D-0269 第二批）：记的是**发起充值**（钱还没到账；
        # 到账走 Jeepay 回调、机器对机器不记操作日志）。
        record(
            log_type=ACTION_LOG_TYPES["wallet.deposit"],
            action="wallet.deposit",
            actor=request.user.username,
            actor_id=request.user.pk,
            target_kind="recharge_order",
            target_id=str(order.pk),
            target_name=request.user.username,
            to_value=_money(order.amount),
            reason=mch_order_no,
        )
        payload = {
            "mch_order_no": mch_order_no,
            "amount": _money(order.amount),
            "pay_data_type": data.get("payDataType"),
            "pay_data": data.get("payData"),
        }
        # ⭐ 可选：`codeUrl` 才出二维码（裸 base64，无 `data:` 前缀）。失败 ⇒ 不加这个键，
        # 其余字段照旧（⛔ 不为一张图 500）。
        qr_png = _deposit_qr_png_base64(payload["pay_data_type"], payload["pay_data"])
        if qr_png is not None:
            payload["qr_png_base64"] = qr_png
        return api_response(payload)


class AppPayNotifyView(AppPlaneMixin, APIView):
    """Jeepay 异步回调。**免认证**（它带的是签名，不是会话/令牌）。
    ⚠️ 返回的是**纯文本** `success`（Jeepay 的约定），不是本仓的信封——不经过 `api_response()`，响应形状与其它端点不同是**有意**的（⛔ 别"统一"它）。
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    #: 本视图回**第三方规定的纯文本**（`success` / `fail`），不是本仓信封 ⇒
    #: `tests/test_no_route_500.py` 按这个类属性把它分到「纯文本形状」那一类。
    text_response_contract = True

    def post(self, request):
        from django.conf import settings

        params = {k: v for k, v in request.data.items()} if hasattr(request.data, "items") else {}
        if not verify_notify(params):
            return HttpResponse("fail", status=400)
        order = (
            RechargeOrder.objects.filter(mch_order_no=str(params.get("mchOrderNo") or "")).first()
        )
        if order is None:
            # 找不到单：回 fail 让它重试（可能是主从延迟/漏建），⛔ 不吞成 success。
            return HttpResponse("fail", status=404)

        # ⭐ 金额与商户必须先校验（评审 Major 1 + 第二轮 Minor 1）：签名合法只证明
        # "这消息来自 Jeepay"，不证明"金额没被改小/改大"。**放在状态分支之前**——
        # 否则一个金额/商户不符的 4/6 会先把订单关掉（虽不入账，但状态已被改）。
        expected_cents = str(int(order.amount * 100))
        if str(params.get("amount")) != expected_cents:
            return HttpResponse("fail", status=400)
        # ⚠️ 小程序支付那个应用可能挂在**另一个 Jeepay 商户**下（`JEEPAY_MINI_MCH_NO`）⇒
        # 商户号要接受两个，只认公众号那个会让小程序的钱永远入不了账。
        allowed_mch_nos = {
            str(getattr(settings, "JEEPAY_MCH_NO", "") or ""),
            str(getattr(settings, "JEEPAY_MINI_MCH_NO", "") or ""),
        } - {""}
        if str(params.get("mchNo")) not in allowed_mch_nos:
            return HttpResponse("fail", status=400)
        # ⚠️ 小程序支付是**另一个 Jeepay 应用**：它的回调带 `JEEPAY_MINI_APP_ID`、用
        # `JEEPAY_MINI_APP_KEY` 签（Jeepay 用的是「订单所属应用」的 appSecret）。只认公众号
        # 那一个 appId 会让小程序付的钱永远入不了账。验签在 `verify_notify` 里按 appId 选钥匙。
        allowed_app_ids = {
            str(getattr(settings, "JEEPAY_APP_ID", "") or ""),
            str(getattr(settings, "JEEPAY_MINI_APP_ID", "") or ""),
        } - {""}
        if str(params.get("appId")) not in allowed_app_ids:
            return HttpResponse("fail", status=400)

        state = str(params.get("state") or "")
        # Jeepay 的 `state`：2 = 支付成功；3 = 支付失败（可重试）；4 = 已撤销；6 = 已关闭。
        # ⚠️ `5`（已退款）不在本批范围：它走下面的 `state != "2"` 分支回 success，
        # 订单保持 paid、钱包保留入账——退款对账是下一批的事。
        if state in ("4", "6"):
            # 终态：落到 closed，⛔ 不留一条永远 pending 的单。
            RechargeOrder.objects.filter(
                pk=order.pk, state=RechargeOrder.STATE_PENDING
            ).update(state=RechargeOrder.STATE_CLOSED)
            return HttpResponse("success")
        if state != "2":
            return HttpResponse("success")

        credit_recharge(order, pay_order_id=str(params.get("payOrderId") or ""))
        return HttpResponse("success")


class AppWalletTransactionsView(AppPlaneMixin, APIView):
    """我的钱包流水（只读）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = WalletTransaction.objects.filter(wallet__user=request.user)
        total = queryset.count()
        rows = list(queryset[:50])
        return api_response(
            paginated(
                [
                    {
                        "id": tx.pk,
                        "kind": tx.kind,
                        "amount": _money(tx.amount),
                        "balance_after": _money(tx.balance_after),
                        "frozen_after": _money(tx.frozen_after),
                        "ref_no": tx.ref_no,
                        "note": tx.note,
                        # ⚠️ D-0302：App 按串里的月日时分直显 ⇒ `local_iso`。
                        "created_at": local_iso(tx.created_at),
                    }
                    for tx in rows
                ],
                total=total,
                page=1,
                page_size=50,
            )
        )
