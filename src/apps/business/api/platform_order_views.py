"""录单页的「读平台订单」（spec §6.2；**起按订单来源分流到两个平台**）。 | 路径 | 方法 | 入参 | 出参 | |---|---|---|---| | `/api/business/read-platform-order/` | POST | `{order_source?: int, tid: str}` | `_to_form_fields(...)` 或 `_xgj_to_form_fields(...)` + `order_source`/`order_source_name`/`platform` | **独立一个 ViewSet**（而不是塞进 `order_views.py`）的理由与 `source_views.py` 同：少一处冲突面，且这条链路的依赖（阿奇索客户端 / 闲管家客户端 / 两个店铺对接配置 / 平台级密钥）都自成一组。 **分流规则（per-source spec D6/D7）：** - **给了 `order_source`** ⇒ 手动覆盖，走原来那条路：`ShopBinding` 可用 ⇒ 阿奇索（优先）；否则按「来源 → `XgjShop` → `binding`」找闲管家应用；两边都没有 ⇒ `1015`。
手动给了来源就**不再**自动判定——否则「我明明选了 A 店，系统却按别家店读回来」。 - **没给 `order_source`** ⇒ **自动判定**：按 id 序逐个试已配置的闲管家应用，`order/detail` 成功即**命中即停**；用订单里的 `seller_eid` 找 `XgjShop`（找不到就按需同步一次，R7；仍找不到就用 `seller_name` 兜底建来源，D8）；都不命中 ⇒ `1015` 并提示「请手动选择订单来源」。 ⚠️ **自动判定不遍历阿奇索**（业务方 四问 ④）：它按次收费，且手选来源才能确定用哪套平台级密钥。代价已登记在 spec §3-D7：没手选来源时，纯阿奇索的单读不出来。 **读单不落库**：只回给前端填表（与 `POST /api/business/orders/parse/` 完全同形）。 **闲管家那条腿多一道闸**（线 F，业务方）：上游读单**成功**、但订单是 `11`（待付款）且系统开关 `reject_unpaid_xgj_order` 开着 ⇒ **拒绝识别**（业务码 `1028` + 一句人话，**不返回**表单字段）。
上游**没给**状态码 / 给了**未知**值一律**放行**（fail-open：只拦明确的待付款）； ⛔ **阿奇索那条腿不受这个开关管**（两家的状态码语义不同、不可比，本仓有明文登记）。 ⚠️ **只捕两个客户端的自有异常**（`AgisoError` / `XgjError`）：两条路的客户端契约都是「成功必然回 dict、任何失败必然抛自己的异常」——网络层、截断响应、非 UTF-8、非数字上游码、非 dict 响应体、非 dict 业务对象六条路径都已翻译（`apps/business/agiso.py` / `xgj.py`）。 **不要**再补 `except Exception`：那会把编程错误也伪装成上游故障，监控就此失明（AGENTS.md §5 / E-0010）。也**不要**把两个异常合成一个 except：它们是**并列**的兄弟，共用一个基类会诱使后来者按"上游都一样"去写跨平台的状态码分支（两个平台的码完全不可比）。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from rest_framework import serializers, viewsets

from apps.business.agiso import DEFAULT_BASE_URL, AgisoError, order_detail
from apps.business.api.xgj_views import credentials_of
from apps.business.models import OrderSource, ShopBinding, XgjShop, XgjShopBinding
from apps.business.system_switches import switch_enabled
from apps.business.xgj import AUTH_ERROR_CODE as XGJ_AUTH_ERROR_CODE
from apps.business.xgj import DEFAULT_BASE_URL as XGJ_DEFAULT_BASE_URL
from apps.business.xgj import XgjError
from apps.business.xgj import order_detail as xgj_order_detail
from apps.business.xgj_shop import source_for_order
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode

logger = logging.getLogger(__name__)

#: 上游鉴权类错误的码（实测：`1` = "Invalid access token"）。
#: 其余一切失败（含网络层）一律 `1017` —— 判据是「**不等于 1** 的失败」，不枚举上游码（spec §8）。
_UPSTREAM_AUTH_ERROR = 1

#: 闲管家的「待付款」状态码。**权威出处**：`docs/XGJ-INTEGRATION.md` 与 `docs/specs/2026-09-22-xgj-shop-integration-design.md` 的状态码表
#: （**11 待付款 · 12 待发货 · 21 已发货 · 22 已完成 · 23 已退款 · 24 已关闭**）。 ⛔ 阿奇索那条腿的码**语义不同、不可比**（本仓有明文登记）⇒ 本常量只用于闲管家那条腿。
XGJ_ORDER_STATUS_UNPAID = 11

#: 控制本档拒绝的系统开关 key（注册表见 `apps/business/system_switches.py`，默认 `"Y"`）。
REJECT_UNPAID_XGJ_ORDER_SWITCH = "reject_unpaid_xgj_order"


def _refuse_unpaid_xgj_order(fields: dict) -> dict | None:
    """闲管家读单的「待付款」闸：命中 ⇒ 返回拒绝响应，否则 `None`（放行）。判据**两条都要**（`AGENTS.md` §5-E：别把防御性写法当承重逻辑，两条各自有用例）： 1. `fields["order_status"]` **恰等于 11**——⚠️ 这里是**fail-open**：上游**没给**这个键时 `_xgj_int` 回 `0`、给了未知值就是那个未知值，两者都不等于 11 ⇒ **放行**。 ⛔ 不要写成「不是已知的 12/21/22/23/24 就拦」：那会在上游少给一个字段时把**正常单**拦住，比漏拦一张待付款单严重得多（录单页会变成"读不出任何单"）。
    2. 开关开着（`switch_enabled`；无覆盖行 = 注册表默认 `"Y"` = 拒）。 ⚠️ 入参是 `_xgj_to_form_fields` 的**产物**（已取整的 int），不是上游原始 `detail` ——这样「状态码不是整数」那条异常路径仍然只由 `_xgj_to_form_fields` 抛 `XgjError`，本函数不引入第二个 500 的逃逸口。 ⚠️ 业务码是 `PLATFORM_ORDER_STATUS_REJECTED`（不是 `1002`）：这一档是**上游订单状态**导致的确定拒绝，与「没配对对接」「凭据错」「上游故障」三档要能分开（见 `error_codes.py` 那段注释）。
    """
    if fields["order_status"] != XGJ_ORDER_STATUS_UNPAID:
        return None
    if not switch_enabled(REJECT_UNPAID_XGJ_ORDER_SWITCH):
        return None
    logger.info(
        "闲管家读单被拒：订单是待付款状态 order_status=%s switch=%s",
        fields["order_status"], REJECT_UNPAID_XGJ_ORDER_SWITCH,
    )
    return api_response(
        data=None,
        # 一句人话，且**读到它的人做得到**：这条 msg 会被录单页原样弹给**录单的客服** （该页把本码放进了 `SPEAKING_READ_ORDER_CODES`）——他的门是 `order-new`，而「闲管家店铺对接」页要 `system.pinduoduo_settings`、那个控件还要
        # `system.settings` ∧ `system.edit_settings` ⇒ 把"去那页关掉"写成**给他的指引** 等于让他去做一件多半做不到的事（评审 P2-1）。所以：主指引是"买家付款后再读一次"，关开关写成**交给管理员**的条件句。
        msg=(
            "这张闲管家订单还是「待付款」，已拒绝识别；买家付款后再读一次。"
            "若长期不需要这个限制，请联系管理员到「闲管家店铺对接」页关掉"
        ),
        code=ErrorCode.PLATFORM_ORDER_STATUS_REJECTED,
    )


class ReadPlatformOrderInputSerializer(serializers.Serializer):
    """入参判据。**请求来源的值一律过序列化器**（AGENTS.md §5 / E-0010）。 `tid` 只允许数字：阿奇索要的是 `Long`。非数字在这里就挡掉，**不发上游请求**。 ⚠️ 64 位上限是**保守**取值：上游对超长 `tid` 的真实行为还没实测（spec §11-T5）。
    ⚠️ `order_source` **可选**（per-source spec D6）：省略（或显式 `null`）⇒ 自动判定；给了 ⇒ 手动覆盖。判据用 `is None` 而不是真值：来源 id `0` 是一个**给了的值**（只是查不到配置 ⇒ `1015`），不该被当成"没给"而滑进自动判定
    （AGENTS.md §5 / E-0021）。
    """

    order_source = serializers.IntegerField(required=False, allow_null=True)
    # ⚠️ 用 `[0-9]` 而**不是** `\d`：Python 的 `\d` 默认匹配 **Unicode 十进制数字**（如「١٢٣」），
    # 那些值不是阿奇索要的 `Long` ⇒ 会白跑一趟上游拿 `1017`，而不是在入参处就 `1000`。
    tid = serializers.RegexField(
        r"^[0-9]{1,64}$", error_messages={"invalid": "订单号只能是数字（≤64 位）"}
    )


class ReadPlatformOrderView(viewsets.GenericViewSet):
    """只暴露一个动作：`POST /api/business/read-platform-order/`。形状照 `RefundView`（本仓「显式 `path()` + `GenericViewSet.as_view({...})`」的既有手法）—— **不用** `@action`：那会额外生成一条别名路径，同一件事两个入口
    （`urls.py` 里 `hall/` 那条注释正是这个教训）。 **不声明 `permission_classes`**：吃 fail-closed 的默认权限类（`IsAuthenticated`），与既有业务端点同形。租户边界由 schema 本身保证——查询**不带任何租户参数**
    （租户上下文只能来自认证态/域名，绝不接受请求参数传入，AGENTS.md §4）。
    """

    def read_platform_order(self, request):
        """读单入口：给了来源就**手动覆盖**，没给就**自动判定**（per-source spec D6）。

        分支的判据是 `is not None`（**不是真值**）：`order_source=0` 是一个给了的值，
        只是查不到配置 ⇒ 老老实实回 `1015`，不要滑进自动判定（AGENTS.md §5 / E-0021）。
        """
        payload = ReadPlatformOrderInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)   # 失败 ⇒ 统一异常处理器包成 1000
        source_id = payload.validated_data.get("order_source")
        tid = payload.validated_data["tid"]

        if source_id is None:
            return self._read_by_auto_detect(tid=tid)
        return self._read_by_source(tid=tid, source_id=source_id)

    def _read_by_source(self, *, tid, source_id):
        """手动覆盖那条路（**优先级：阿奇索优先，闲管家兜底**）。一个订单来源**同时**绑了两边时，阿奇索赢、闲管家那一行**静默失效**——这是有意选的（阿奇索那条路已上线，先改它的行为风险更大），代价已登记在 spec §12-4，并由测试钉住。
        ⚠️ 判据是「阿奇索那边**可用**（有绑定 **且** 授权码与平台密钥都非空）」而不是「有绑定」：否则一个建了行但还没填授权码的来源会把闲管家那条**能用的**配置也一起挡掉。 ⚠️ 手动给来源**不回落自动判定**：租户明确选了某家店，系统却按别家店读回来，
        比"读不到"更糟（那是**静默读错**）。所以这条路上没有配置就是 `1015`。
        """
        source = OrderSource.objects.filter(pk=source_id).first()

        binding = (
            ShopBinding.objects.filter(order_source_id=source_id).order_by("id").first()
        )
        app_id = getattr(settings, "AGISO_APP_ID", "") or ""
        app_secret = getattr(settings, "AGISO_APP_SECRET", "") or ""
        # ⚠️ 授权码是客服从阿奇索页面**复制粘贴**的，可能带换行/空格：`strip()` 之后才用。    token **中间**带裸 `\n` / `\r` 时 `http.client.putheader` 抛 `ValueError`，被    `agiso._NETWORK_ERRORS` 收成 `AgisoError` ⇒ 本端点回 `1017`（**不是** 500），
        #    但这一单仍然白跑；strip 后为空串 ⇒ 与「这个来源没有绑定」同样按 `1015` 处理    （**不发**上游请求）。
        token = (binding.access_token or "").strip() if binding is not None else ""
        if binding is not None and token and app_id and app_secret:
            return self._read_from_agiso(
                tid=tid, binding=binding, token=token, app_id=app_id, app_secret=app_secret,
                source=source,
            )

        xgj_binding, xgj_credentials = self._binding_for_source(source_id)
        if xgj_binding is not None and xgj_credentials is not None:
            return self._read_from_xgj(
                tid=tid, binding=xgj_binding, credentials=xgj_credentials, source=source,
            )

        # 两边都没有可用的配置 ⇒ `1015`。msg 把**两个**页面都点出来：
        # 只说「其它平台店铺对接」会让填了闲管家的人去错地方。
        return api_response(
            data=None,
            msg="这家店铺还没配置好平台对接，请先到「其它平台店铺对接」或「闲管家店铺对接」完成配置",
            code=ErrorCode.PLATFORM_NOT_CONFIGURED,
        )

    @staticmethod
    def _binding_for_source(source_id):
        """按 `order_source` 找**闲管家应用**：`来源 → XgjShop → binding`（per-source spec §5）。 ⚠️ `@staticmethod`（订单管理页「同步收款」线）：函数体不读 `self`，而那个领域模块（`apps/business/xgj_receipt_sync.py`）要**复用同一份判据** （⛔ 不抄第二份「来源 → 店 → 应用」的解析）。改成静态方法后可以直接 `ReadPlatformOrderView._binding_for_source(source_id)` 调用，**行为一字未变**。 ⚠️ 起 `XgjShopBinding` 上**没有** `order_source` 字段了：一个应用最多绑 30 家店，
        「一个应用 ↔ 一个来源」正是这条线要修的错模型 ⇒ 来源归**店铺**，应用从店铺反查。 ⚠️ 取**第一条凭据完整**的行，而不是第一条行：同一个来源理论上可以挂在多家店上（表上没有唯一约束、页面上也没有编辑入口），第一行凭据不全时不该让后面能用的那一行失效——那正是"配了却点了没反应"的形状。判据是 **id 序**，所以"用了哪一行"是确定的，失败日志里也会带 `binding=<pk>`。（阿奇索那一支仍是 `.first()`：那是**已上线**的行为，本轮不为对称去改它。）
        """
        for shop in (
            XgjShop.objects.filter(order_source_id=source_id)
            .select_related("binding")
            .order_by("id")
        ):
            found = credentials_of(shop.binding)
            if found is not None:
                return shop.binding, found
        return None, None

    def _read_by_auto_detect(self, *, tid):
        """没给来源时的**自动判定**（per-source spec D6/R2）。按 id 序逐个试**已配置**（凭据完整）的闲管家应用，`order/detail` 成功即**命中即停**： - 命中 ⇒ 用订单里的 `seller_eid` 认店（找不到就按需同步一次、再用 `seller_name` 兜底），
        必要时建来源（`xgj_shop.source_for_order`）； - 这个应用读不到 ⇒ 记一条日志再试下一个； - 一个都没命中 ⇒ `1015`，msg 明确提示**手动选择订单来源**（这是租户唯一的出路：
        自动判定不遍历阿奇索，spec §3-D7）。 ⚠️ **命中即停**不只是省钱（闲管家按次计费）：订单号属于哪家店是**唯一**的，多试出来的"第二个命中"只可能是上游的模糊匹配 ⇒ 取第一个成功的反而是更确定的判据。
        """
        for binding in XgjShopBinding.objects.order_by("id"):
            credentials = credentials_of(binding)
            if credentials is None:
                continue
            app_key, app_secret = credentials
            try:
                detail = xgj_order_detail(
                    tid,
                    app_key=app_key,
                    app_secret=app_secret,
                    base_url=getattr(settings, "XGJ_API_BASE", "") or XGJ_DEFAULT_BASE_URL,
                )
            except XgjError as exc:
                # ⚠️ 自动判定下"这个应用读不到"是**正常**的（订单不属于这个应用）⇒ 只记上游码，
                #    不记上游原文（它可能回显凭据，spec §9-S1），也不弹给租户。
                logger.info(
                    "闲管家自动判定：这个应用读不到订单，试下一个 order_no=%s binding=%s upstream_code=%s",
                    tid, binding.pk, exc.upstream_code,
                )
                continue
            # 命中即停（下面这一步之后不再试任何应用）
            fields = _xgj_to_form_fields(detail)
            refusal = _refuse_unpaid_xgj_order(fields)
            if refusal is not None:
                # ⚠️ 拒绝也是「命中即停」：这张单**属于**这个应用（只是状态不允许录单），    继续试下一个应用只会白花钱、并且可能试出别的店的同号单（静默读错）。
                # ⚠️ 闸排在 `source_for_order` **之前**：被拒的这次读单一个字节都不落库    （订单来源字典也别为一张不录的单建行）。
                logger.info(
                    "闲管家自动判定命中但被拒 order_no=%s binding=%s", tid, binding.pk
                )
                return refusal
            source = source_for_order(binding, detail)
            logger.info(
                "闲管家自动判定命中 order_no=%s binding=%s order_source=%s", tid, binding.pk, source.pk
            )
            return _with_source(fields, source=source, platform="xgj")

        return api_response(
            data=None,
            msg="没能识别这个订单号属于哪家店铺，请手动选择订单来源",
            code=ErrorCode.PLATFORM_NOT_CONFIGURED,
        )

    def _read_from_agiso(self, *, tid, binding, token, app_id, app_secret, source=None):
        """阿奇索那条路（**逐字保留原实现**，只把它搬进独立方法）。"""
        try:
            data = order_detail(
                tid,
                token,
                app_id=app_id,
                app_secret=app_secret,
                # ⚠️ 基址**只由平台配置提供**；空值回落实测基址（`AGISO_API_BASE=` 是合法配置，
                #    所以判据用 `or` 而不是 `getattr(..., DEFAULT_BASE_URL)` 的默认参数）。
                base_url=getattr(settings, "AGISO_API_BASE", "") or DEFAULT_BASE_URL,
            )
            # 映射也放进 try：上游 `Data` **里面**的形状不在上游契约内，
            # 映射失败时 `_to_form_fields` 抛的也是 `AgisoError` ⇒ 落到下面同一个出口（1017），
            # 而不是 `AttributeError`/`ValueError` ⇒ 500（与「`Data` 不是 dict」那道闸同族）。
            fields = _to_form_fields(data)
        except AgisoError as exc:
            # 上游失败必须记日志（含 `RequestId`：排障时它是对着客服说的唯一凭据），但**不**把上游原文弹给客服（前端按 spec §2.5 静默降级为手工填价），也**不**记授权码 / `AGISO_APP_SECRET`（spec §9-S3）——
            # `request_id` 与 `upstream_msg` 都是**第三方文本且同源**（上游把 `Authorization` 回显进任一处都可能）⇒ 两个字段都先过 `_redact_token` 再落日志。
            logger.warning(
                "阿奇索订单查询失败 tid=%s binding=%s upstream_code=%s request_id=%s msg=%s",
                tid, binding.pk, exc.upstream_code,
                _redact_token(exc.request_id, token),
                _redact_token(exc.upstream_msg, token),
            )
            code = (
                ErrorCode.PLATFORM_TOKEN_INVALID
                if exc.upstream_code == _UPSTREAM_AUTH_ERROR
                else ErrorCode.PLATFORM_QUERY_FAILED
            )
            return api_response(data=None, msg="平台订单读取失败", code=code)

        return _with_source(fields, source=source, platform="agiso")

    def _read_from_xgj(self, *, tid, binding, credentials, source=None):
        """闲管家那条路（spec §6 的映射表）。 ⚠️ **不把上游原文弹给客服**（与阿奇索同款静默降级，spec §2.5）；失败只记日志。 ⚠️ **日志里的上游原文一律先脱敏**（spec §9-S1）：`app_secret` 我方**从不发出**
        （参与签名的只是它的 MD5 派生值，`xgj.sign`），但上游回包是**第三方文本**——它回显收到的 query/header 是完全可能的，而我们**无法枚举**它不会。所以判据不是"我们没发它就一定安全"，而是"**落了日志的第三方文本先抹掉密钥**"
        （与阿奇索那条路的 `_redact_token` 对称；那边的凭据是放在请求头里的原文，风险更直白）。
        """
        app_key, app_secret = credentials
        try:
            detail = xgj_order_detail(
                tid,
                app_key=app_key,
                app_secret=app_secret,
                base_url=getattr(settings, "XGJ_API_BASE", "") or XGJ_DEFAULT_BASE_URL,
            )
            fields = _xgj_to_form_fields(detail)
        except XgjError as exc:
            logger.warning(
                "闲管家订单查询失败 order_no=%s binding=%s upstream_code=%s msg=%s",
                tid, binding.pk, exc.upstream_code, _redact_secret(exc.upstream_msg, app_secret),
            )
            code = (
                ErrorCode.PLATFORM_TOKEN_INVALID
                if exc.upstream_code == XGJ_AUTH_ERROR_CODE
                else ErrorCode.PLATFORM_QUERY_FAILED
            )
            return api_response(data=None, msg="平台订单读取失败", code=code)

        # 「待付款」闸（线 F，业务方）：上游读单**成功**、但状态不允许录单 ⇒ 拒绝识别。
        # ⚠️ 闸在 `try` **之外**：它返回的是正常业务失败信封，不是上游异常（别让它走进 `except`）。
        refusal = _refuse_unpaid_xgj_order(fields)
        if refusal is not None:
            return refusal

        return _with_source(fields, source=source, platform="xgj")


def _with_source(fields: dict, *, source, platform: str) -> dict:
    """成功载荷上补三个出参：`order_source` / `order_source_name` / `platform`（per-source spec §5）。 **为什么后端回来源 id**：录单页可能在**没选来源**的情况下发请求（自动判定），回填下拉需要知道后端判定成了哪一家店。`platform` 是给排障与前端分流用的
    （`"agiso"` / `"xgj"`）。 ⚠️ `source` 可能为 `None`（理论上不该发生：三条成功路径都会带来源），那时 `order_source` 回 `null` —— 前端据它**不动**下拉（spec §5 的注）。
    """
    fields["order_source"] = source.pk if source is not None else None
    fields["order_source_name"] = source.name if source is not None else ""
    fields["platform"] = platform
    return api_response(data=fields)


def _redact_secret(msg: str, secret: str) -> str:
    """把上游失败文案里可能夹带的 **AppSecret** 抹掉（spec §9-S1：密钥不得进日志）。比 `_redact_token` 简单：闲管家的密钥**没有**"被我们自己放进请求头"这条路径
    （它只以 MD5 派生值参与签名），所以不需要处理换行转义那一族形态——只需要防**上游回显**这一条：第三方文本里逐字出现密钥就替换掉。
    """
    if not secret:
        return msg
    return msg.replace(secret, "***")


def _redact_token(msg: str, token: str) -> str:
    """把上游失败文案里可能夹带的**授权码**抹掉（spec §9-S3：授权码不得进日志）。 ⚠️ 调用点有**两处**：`upstream_msg` 与 `request_id`——两者都是第三方文本且同源（上游把 `Authorization` 回显进任一处都可能），所以共用这一个脱敏器。两条已知路径（**本机都实测过**；两条的原始报错形态见下）： ① **上游回显**：上游把收到的 `Authorization` 头值原样回进 `Error_Msg` / `RequestId` ⇒ token **逐字**出现。
    ② **本地 `putheader` 的转义形态**：token **中间**含裸 `\\n` / `\\r` 时（首尾的已被 `strip()` 去掉），`http.client.putheader` 抛 `ValueError('Invalid header value %r')`，而 `%r` 对 bytes 会把换行**转义**成 `\\n` / `\\r` ⇒ **整个 token 不是逐字出现**，但换行两侧的片段逐字出现（`b'Bearer CANARY-A\\nCANARY-B'` 里 `CANARY-A` / `CANARY-B` 都在）。 ⚠️ 只 `msg.replace(token, "***")` 对这条路径是**空转**（实测 `token in msg` 为 `False`），
    日志里照样能还原授权码 ⇒ 必须连 `Bearer <token>` 的 `repr` 转义形态一起替换。（`repr(...)` 与 `%r` 是同一个函数，所以下面拼出来的转义串与 msg 里的逐字相同；例如 token 含单引号时 `repr` 会改用双引号包裹，`[2:-1]` 一样剥得对。）
    """
    if not token:
        return msg
    redacted = msg.replace(token, "***")
    try:
        # 与 `http.client.putheader` 抛错时 `%r` 渲染的是同一串（它先 `.encode("latin-1")`）。
        escaped_bearer = repr(f"Bearer {token}".encode("latin-1"))[2:-1]
    except UnicodeEncodeError:
        # 非 latin-1 的 token 走不到 `%r` 那条路（`encode` 先抛，文案里不含 token）⇒ 只需逐字替换。
        return redacted
    return redacted.replace(escaped_bearer, "Bearer ***")


def _int_field(data: dict, key: str) -> int:
    """上游**整数**标量 → `int`。缺省 ⇒ `0`；给了但转不成整数 ⇒ `AgisoError`（不 500）。 ⚠️ Task 1 的契约只保证 `Data` 是 dict，**不保证里面每个字段的类型**（第三方数据）。直接 `int(...)` 遇到字符串 / 对象会抛 `ValueError` / `TypeError` 逃逸成 500——
    与 Task 1 堵掉的「`Data` 不是 dict」是同一类逃逸，只是深了一层。 ⚠️ 也**不静默兜成 0**：那会给录单页填一个错的金额 / 时间（两个方向都是静默的）。
    """
    raw = data.get(key)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        # ⚠️ `OverflowError`：`json.loads` 默认接受 `Infinity` / `-Infinity`（非标准 JSON），
        #    而 `int(float("inf"))` 抛的是 `OverflowError`（**不是** `ValueError`）——
        #    漏掉它就是一个 500 的逃逸口（`payment: Infinity` 可达）。
        raise AgisoError(
            upstream_code=-1, upstream_msg=f"上游 {key} 不是整数：{raw!r}"
        ) from exc


def _to_form_fields(data: dict) -> dict:
    """把上游 `Data` 映射成录单页要填的形状（spec §6.2）。 ⚠️ `payment` 单位是**分**：`Decimal(分) / 100`，**不用浮点**（算错钱的方向），格式化两位小数字符串（与既有 `MONEY_FIELDS` 的序列化形状一致）。 **0 是合法值**：缺省判据用 `is not None`，不要用真值（`bool(0) is False`）。 ⚠️ 空串 `""` 表示「上游**没给**价格」（`raw_payment is None`），**不是**「价格 = 0」（0 分会照实回 `"0.00"`）。前端据这个空串**不覆盖**客服已填的价格（`order-addorder.ts` 的 `if (fields.payment !== '')`）——谁把那条守卫去掉或改成无条件写，空串就会把 `#price` 清空，而**空价不会报错**：`formToPayload` 把空串键整个丢掉（`views/orders/orderFormFields.ts:285`），`total_price` 又是 `required=False` +
    模型 `default=Decimal("0.00")`（`api/serializers.py:294-295`）⇒ **新建录单时**订单被**静默记成 0 元**。 ⚠️ **起录单页不再能静默录出空总价单**（业务方裁定 **D-0082**）：录单页**新建态**的 `totalPriceRequiredError` 会把空总价拦在提交前。上面那条链**在别处仍成立**（`formToPayload` 仍丢空串键；接口层 / 外部脚本 / 平台拉单省略该字段仍落 `0.00`）——本注描述的是**接口层**口径。 ⚠️ **别写成「必然退不掉」**（2026-09-21 复评反例；2026-09-23 按 D-0142 更新）：退单闸门 `refunds.py::_gross_received` **只读订单总价 `total_price`** （D-0142 推翻了 D-0081 的「接单实收优先」；更早回落的是接单价 `hall_price`） ⇒ 总价 0 元、接单实收有钱的单**毛收款就是 `0.00`**；而**总价也是 0** 时毛收款同样是 `0.00` （可退额为 0，正数退款仍被前置校验 #4 拒）——**「两处价都空」这个失败形态已不存在**
    （`total_price` 是 `null=False default=0.00`）。 ⚠️ `buyer_nick` 只能当**显示值**：阿奇索自己标注它不唯一且买家可改——**不要**把它当客户身份（归属 / 对账一律走订单号）。 ⚠️ `customer_phone` 恒空：阿奇索订单查询没有电话字段，保留键只为贴合对标的回填形状。
    """
    raw_payment = data.get("payment")
    payment = "" if raw_payment is None else f"{Decimal(_int_field(data, 'payment')) / 100:.2f}"
    item = data.get("item")
    if not isinstance(item, dict):
        item = {}
    return {
        "payment": payment,                       # 元，字符串
        "buyer_nick": str(data.get("buyer_nick") or ""),
        "customer_phone": "",
        "remark": str(item.get("title") or ""),
        "order_status": _int_field(data, "order_status"),
        "pay_time": _int_field(data, "pay_time"),
    }


def _xgj_int(data: dict, key: str) -> int:
    """闲管家上游**整数**标量 → `int`。缺省 ⇒ `0`；给了但转不成整数 ⇒ `XgjError`（不 500）。与 `_int_field`（阿奇索那条路）同形，但抛的是 `XgjError`——两条路的异常出口不同，
    混用会让 `except` 抓不住（`AgisoError` 与 `XgjError` 没有继承关系）。 ⚠️ 也**不静默兜成 0**：那会给录单页填一个错的金额 / 时间（两个方向都是静默的）。
    """
    raw = data.get(key)
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        # ⚠️ `OverflowError`：`json.loads` 默认接受 `Infinity`（非标准 JSON），
        #    而 `int(float("inf"))` 抛的是它（**不是** `ValueError`）——漏掉就是一个 500 的逃逸口。
        raise XgjError(
            upstream_code=-1, upstream_msg=f"上游 {key} 不是整数：{raw!r}"
        ) from exc


def _xgj_to_form_fields(detail: dict) -> dict:
    """闲管家订单详情 → 录单页要填的形状（spec §6 的映射表）。 **字段名全部出自官方接口文档**（spec §2.2，`order_detail` 的 schema），不是猜的： | 表单字段 | 上游字段 | 说明 | |---|---|---| | `payment` | **`pay_amount`（实付，分）** | `Decimal(分) / 100`，两位小数字符串；**不用浮点**（算错钱的方向） | | `buyer_nick` | `buyer_nick` | 直接取 | | `buyer_eid` | `buyer_eid` | 原样透传（2026-11-10 客户线：买家身份键，录单时随载荷回传建客户档案） | | `customer_phone` | `receiver_mobile` | ⚠️ **仅待发货状态返回**（实测：其它状态的单里这个键**整个不在**）⇒ 那些单恒空 | | `remark` | `goods.title` | 与阿奇索取 `item.title` 同形 | | `order_status` / `pay_time` | 同名 | 取整后透传（缺省回 `0`，**不是**原值），见下 |
    ⚠️ **`payment` 取 `pay_amount`（实付金额）而不是 `total_amount`（下单金额）**（业务方 定）：录单记的是「**客户实际付了多少钱**」——录单金额是拿来算绩效 / 结算的，以真实付款为准更不容易算错钱。 **真实数据证明两者真的会不同**：实测一张已退款单 `total_amount=1000` / `pay_amount=900`（分） ⇒ 取错会录进差 10% 的价格。改回去只需把下面 `detail.get("pay_amount")` 改一处 + `tests/test_xgj_shop_binding.py` 里三条期望值（spec §11-T2 已闭合）。 ⚠️ 空串 `""` 表示「上游**没给**这个金额」（`raw_amount is None`），**不是**「金额 = 0」（0 分会照实回 `"0.00"`）。前端据这个空串**不覆盖**客服已填的价格（`order-addorder.ts` 的 `if (fields.payment !== '')`）——去掉那条守卫会让空价单被**静默记成 0 元** （`_to_form_fields` 的注释里有完整的后果链）。
    ⚠️ **起录单页新建态会拦空总价**（业务方裁定 **D-0082**）——本句描述的是**接口层**口径（省略该字段仍落 `0.00`）；录单页那条路不再成立。 ⚠️ `order_status` / `pay_time` 是**取整后透传**（`_xgj_int`；上游缺这个键时回 `0`）：闲管家的码是 11/12/21/22/23/24，与阿奇索的码**语义不同、不可比**。前端今天**不消费**这两个字段（`order-addorder.ts` 只用 payment / buyer_nick / customer_phone / remark）；将来要用，先做一张跨平台状态码对照表（spec §11-T5）。
    """
    raw_amount = detail.get("pay_amount")
    payment = "" if raw_amount is None else f"{Decimal(_xgj_int(detail, 'pay_amount')) / 100:.2f}"
    goods = detail.get("goods")
    if not isinstance(goods, dict):
        # 与 `_to_form_fields` 对 `item` 的处理同形：嵌套字段形状不对时当空处理，
        # 只影响 `remark`（一个展示用字符串），不影响金额。
        goods = {}
    return {
        "payment": payment,
        "buyer_nick": str(detail.get("buyer_nick") or ""),
        # ⭐ 2026-11-10 客户线：买家身份键，原样透传（不裁剪不改名——上游字段出口纪律）。
        # ⛔ 阿奇索那条路（`_to_form_fields`）没有这个键：那边没有买家唯一标识。
        "buyer_eid": str(detail.get("buyer_eid") or ""),
        "customer_phone": str(detail.get("receiver_mobile") or ""),
        "remark": str(goods.get("title") or ""),
        "order_status": _xgj_int(detail, "order_status"),
        "pay_time": _xgj_int(detail, "pay_time"),
    }
