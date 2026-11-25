"""业务路由的路径转换器（`register_converter` 的唯一登记处）。 ## 为什么需要它：路径参数里的 NUL 字节会 500 P4-9 评审实测：`POST /api/business/orders/%00/refunds` → `DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes` → **HTML 500**。同一形态在既有 `hall/<str:order_no>/claim/` 上也存在（评审同样实测到 `DataError`）。机制（**读码**，`django/test/client.py:460-465` 与 `django/core/handlers/wsgi.py:147-151`）：
测试客户端把 `parsed.path` 交给 `unquote_to_bytes()`，于是 `%00` 变成**真的 NUL 字符** （`\x00`，iso-8859-1 解码进 `PATH_INFO`）；Django 的 `<str:...>` 转换器正则 `[^/]+` **匹配 NUL** ⇒ 订单号以 `\x00` 进入 `filter(order_no=...)` ⇒ PostgreSQL 拒绝 ⇒ 500。 ⚠️ **为什么是「共享」修复而不是给某个端点打补丁**：这是**平台级 shape**， `orders/` 与 `hall/` 两条路径参数是**同一个** `order_no` 输入面。两个 `path()` 共用下面这一个转换器 ⇒ `git grep "order_no:order_no"` 就是全部使用点。
## 选定的形状：路由不匹配 ⇒ 404 转换器的正则**排除 ASCII 控制字符**（`\x00`–`\x1f` 与 `\x7f`），畸形路径因此**不匹配**任何路由 ⇒ Django 返回 **404**（不是 500，也不进视图）。选 404 而不是「200 + 业务码」的理由：这个输入**连一条合法订单号都不是**，它没有「业务结果」可言；让它在路由层就落空，比在每个视图里各写一遍「订单号里有没有控制字符」更少一处可漏。 ## 边界（如实登记） - **只覆盖显式 `path()` 里用 `<order_no:...>` 的那两条路由**。router 生成的订单详情路由（`orders/{order_no}/...`）**本来就安全**：`OrderViewSet.lookup_value_regex = r"\d{20}"` （`order_views.py:281`）只匹配 20 位数字 ⇒ NUL 早已被挡在路由外（实测 404）。 - `performances/<int:performance_id>/...` 用的是 `int` 转换器（`[0-9]+`），同样天然安全。 - ⛔ 本模块**不改任何业务逻辑**（不加 `≥ 0` 校验、不动退款/结账的形状），只收窄路由匹配。"""
from django.urls import register_converter

#: `order_no` 路径段允许的字符：**排除 ASCII 控制字符**（含 NUL）。
#: 写成 `[^/...]`（而不是「只允许数字」）是为了不把「订单号的字符集」定义到这里——
#: 订单号的形状由 `apps/business/order_no.py` 的生成器与列宽决定，本处只挡控制字符。
ORDER_NO_PATTERN = r"[^/\x00-\x1f\x7f]"


class OrderNoConverter:
    """`<order_no:...>`：挡住含控制字符的路径段（NUL 是其中一种）。
    ⚠️ `to_python` / `to_url` 都**原样返回**：本转换器的职责只是**收窄匹配**，
    不做任何规范化（改写订单号会让 `ORDER_NO_PATTERN` 悄悄改变业务值）。"""

    regex = ORDER_NO_PATTERN + "+"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(OrderNoConverter, "order_no")


class RemoteRefConverter:
    """`<remote_ref:...>`：远端来源 / 远端单号的路径段——**挡住控制字符**。理由与 `OrderNoConverter` **完全同源**（同一份实测：`%00` ⇒ `DataError` ⇒ HTML 500）：
    这两个段会直接进 `filter(source_system=..., remote_order_id=...)`。 ⚠️ 两个段**共用这一个**转换器（`remote_order_id` 在方向一是 20 位数字、方向二是我方
    自造的来源单号，字符集不由这里定义）。"""

    regex = ORDER_NO_PATTERN + "+"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(RemoteRefConverter, "remote_ref")
