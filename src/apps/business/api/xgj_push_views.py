"""闲管家推送接收端点（spec `2026-10-04-xgj-push-inbound-design.md` §4 / §D1 / §D8）。两条路由（`apps/business/urls.py` 各一条**只收 POST 的字面 `path()`**）：`/api/business/xgj/push/order/`（订单推送：验签 + 按状态自动退单，领域逻辑在 `apps/business/xgj_push.py`）与 `/api/business/xgj/push/product/`（商品推送：只验签 + ack，一个字节都不写，spec §D9）。三条装配口径（spec §D1 / §D8）：
① **免登录**：`authentication_classes = []` + `permission_classes = [AllowAny]`——调用方是闲管家的服务器，没有会话也没有令牌（先例 = `PeerBindView`）；⛔ `permission_classes` 不写成空列表（那要靠「空 = 放行」的隐式约定，而全局默认是 `IsAuthenticated`），`authentication_classes` 反过来**必须**是空列表（空列表在 DRF 里就是匿名）。② **免 CSRF**：不用自己加 `csrf_exempt`——DRF 的 `APIView.as_view()` 自套它（`rest_framework/views.py` 的 `as_view` 尾上 `return csrf_exempt(view)`）⇒ 安全边界**完全**由领域层的验签承担（spec §D2 的代价栏如实登记）。③ **响应形状是第三方规定的** `{"result": …, "msg": …}`，**不是**本仓统一信封（`api_response` 在这一处**有意不用**）；一律 HTTP 200——包括「处理期间冒出未预期异常」那一档：厂商只读 `result`，做成 500 只会让对方拿到 HTML 响应体、照样判失败，而我们失去一次如实报 `fail` 的机会（spec §4 的 ⚠️ 段）；该例外**显式登记**在 `tests/test_no_route_500.py` 的 `VENDOR_SHAPED_ROUTES`，⛔ 没有放宽那张网的通用断言。请求来源的值一律过序列化器（E-0010）：body 用 `XgjOrderPushPayloadSerializer` 收口、query 三个签名参数用 `XgjPushQuerySerializer` 收口——它们**都要进查询**（`order_no` → `source_order_no` 的过滤条件、`appid` → `app_key` 的过滤条件）⇒ 类型与长度必须在进库之前定死；序列化不过一律折成 `fail` + 那一档的 `msg`（§4 的表），HTTP 仍是 200。
"""

from __future__ import annotations

import json
import logging

from django.http import JsonResponse
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from apps.business.xgj_push import (
    MSG_BAD_BODY,
    MSG_MISSING_SIGN,
    MSG_PROCESS_FAILED,
    RESULT_FAIL,
    handle_order_push,
    handle_product_push,
)

logger = logging.getLogger(__name__)


class XgjPushQuerySerializer(serializers.Serializer):
    """query 的三个签名参数（spec §2.1 / §5 第 2 步）。
    ⚠️ 文档把 `timestamp` 标成 `required=false`，而**本仓**按 spec §4 的表把它当必填：
    缺 `appid` / `sign` / `timestamp` **任一** ⇒ `fail 缺少签名参数`（业务方选定的严格档 R8）。⚠️ 长度上限是为了不让一个超长参数进查询 / 进日志；三条都对不上时回的是同一句`缺少签名参数`（§4 的表里这一档只有那一句，⛔ 不为「格式不对」另造 msg）。"""

    appid = serializers.CharField(max_length=64)
    timestamp = serializers.CharField(max_length=32)
    sign = serializers.CharField(max_length=128)


class XgjOrderPushPayloadSerializer(serializers.Serializer):
    """订单推送 body 里**本线会用到**的五个键（spec §2.1 的字段清单）：`order_no` / `order_status`（退款线收的）+ `seller_id` / `user_name` / `modify_time`（收款线补收）——**带默认值、都可选** ⇒ 退款路径（23/24）在「报文缺后三个键」时行为逐字节不变；其余键本线一个都不用，未知键在 DRF 里**静默忽略**（不报错、不进任何查询）。
    ⚠️ `order_status` / `seller_id` / `modify_time` 用 `CharField`：推送方可能发 JSON 数字（`23` / `1234567`），DRF 的 `CharField` 会把 `int` 转成字符串（⛔ 不要按 `int` / `str` 两种形状各判一次；秒级时间戳的数值解析在领域层的 `_success_time_from_modify` 里做、同一份判据）。⚠️ `max_length=128` 与 `Order.source_order_no` 列宽同源（模型是那一列宽度的唯一出处，这里写的是**推送值**的上限；超长值在进库之前就被挡下）。
    """

    order_no = serializers.CharField(
        max_length=128, required=False, allow_blank=True, allow_null=True, default=""
    )
    order_status = serializers.CharField(
        max_length=16, required=False, allow_blank=True, allow_null=True, default=""
    )
    # 文档：`seller_id` int64 商家ID / `user_name` 闲鱼会员名（64 列宽与 `XgjShop.user_name` 同源）。
    seller_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, allow_null=True, default=""
    )
    user_name = serializers.CharField(
        max_length=64, required=False, allow_blank=True, allow_null=True, default=""
    )
    # 文档：`modify_time` int32 秒级时间戳（10 位 ⇒ 32 列宽上限绰绰有余）。
    modify_time = serializers.CharField(
        max_length=32, required=False, allow_blank=True, allow_null=True, default=""
    )


class XgjPushViewBase(APIView):
    """两个推送端点的共同装配（免登录 / 免 CSRF / 非信封响应）。⚠️ **子类必须显式声明 `vendor_response_contract = True`**：
    那是 `tests/test_no_route_500.py`的**框架事实**判据（`_is_vendor_shaped` 按类属性取值），用来把「第三方形状」的端点与「本仓统一信
    封」的端点在结构上分开。基类**不**预置这个属性 ⇒ 新加的子类忘了声明时，它会落进那张网的通用断言（要求响应里有 `code` 键）而当场变红，⛔ 不要把它挪到基类上当成「沉默的默认值」。"""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    @staticmethod
    def _reply(result: str, msg: str) -> JsonResponse:
        """第三方形状的响应体：**键集恰好**是 `{"result", "msg"}`（spec §D8）。"""
        return JsonResponse({"result": result, "msg": msg})

    @staticmethod
    def _signed_query(request):
        """query 三个参数过序列化器；不合法回 `None`（调用方回 `msg = 缺少签名参数`）。"""
        serializer = XgjPushQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return None
        return serializer.validated_data


class XgjOrderPushView(XgjPushViewBase):
    """`POST /api/business/xgj/push/order/`（spec §4 / §5）。"""

    #: 见基类 docstring：本端点回第三方形状，登记进 `test_no_route_500.VENDOR_SHAPED_ROUTES`。
    vendor_response_contract = True

    def post(self, request):
        # 原文字节**先取下来**：验签用它（spec §2.2），解析也用它 ⇒ 两者是同一份字节，
        # ⛔ 不是「解析出来再 dumps 回去」。
        raw_body = request.body
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._reply(RESULT_FAIL, MSG_BAD_BODY)
        payload_serializer = XgjOrderPushPayloadSerializer(data=payload)
        if not payload_serializer.is_valid():
            return self._reply(RESULT_FAIL, MSG_BAD_BODY)
        signed = self._signed_query(request)
        if signed is None:
            return self._reply(RESULT_FAIL, MSG_MISSING_SIGN)
        try:
            result, msg = handle_order_push(
                raw_body=raw_body,
                appid=signed["appid"],
                timestamp=signed["timestamp"],
                sign=signed["sign"],
                order_no=payload_serializer.validated_data.get("order_no") or "",
                order_status=payload_serializer.validated_data.get("order_status") or "",
                # 收款线补收的三个键：缺省即退款线的旧签名（23/24 零影响）。
                seller_id=payload_serializer.validated_data.get("seller_id") or "",
                user_name=payload_serializer.validated_data.get("user_name") or "",
                modify_time=payload_serializer.validated_data.get("modify_time") or "",
            )
        except Exception:
            # 未预期异常仍然**响亮**（服务器日志全栈）＋ 如实回 `fail`（spec §4 的第 4 行）。
            logger.exception("闲管家订单推送处理失败")
            return self._reply(RESULT_FAIL, MSG_PROCESS_FAILED)
        return self._reply(result, msg)


class XgjProductPushView(XgjPushViewBase):
    """`POST /api/business/xgj/push/product/`（spec §D9）。"""

    #: 见基类 docstring（同 `XgjOrderPushView`）。
    vendor_response_contract = True

    def post(self, request):
        signed = self._signed_query(request)
        if signed is None:
            return self._reply(RESULT_FAIL, MSG_MISSING_SIGN)
        try:
            result, msg = handle_product_push(
                raw_body=request.body,
                appid=signed["appid"],
                timestamp=signed["timestamp"],
                sign=signed["sign"],
            )
        except Exception:
            logger.exception("闲管家商品推送处理失败")
            return self._reply(RESULT_FAIL, MSG_PROCESS_FAILED)
        return self._reply(result, msg)