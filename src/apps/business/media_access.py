"""订单截图**共享前缀**的业务侧判据（P4 Task 4，裁决 R-P4-12 / R-P4-13；D-0316 起含群码）。护栏本身在 `apps.core.storage.key_belongs_to_tenant`（基座层，纯函数）；本模块放**它需要的业务知识**： 1. `order_screenshot_key_is_held(key)` —— 「**本租户**库里有没有一行订单截图指着这个对象名」。这是那条窄例外唯一的判据（`shared_holder`）。 2. `order_shared_origin(order)` —— 「这张单在**链上最初那一跳**的单号」，共享 key 的分段依据。 3. `is_shared_object_key(key)` —— 「这个对象名是不是在共享前缀下」的**唯一实现**（D-0316）：
发送侧的前缀筛（`peer_dispatch`）、接收侧的形状闸（`peer_inbound`）、群码的字节保留判据（`group_qr`）都委托它 ⇒ ⛔ 别再在别处抄一份同形实现（三份判据漂移过就是这样来的）。 ## 为什么单独一个模块、为什么模型 import 放在函数里 `apps.core` **不在模块顶层** import `apps.business` 的**模型**（那会引出反向依赖：`apps.business` 是 `TENANT_APPS`-only 的租户层，基座层不该在导入期带上它的模型）。所以判据住在这里，由**业务侧**的调用方传给护栏；`apps/core` 那侧（`media_blob_views`）要用时**在函数体内延迟 import 本模块**。
⚠️ 这里限定的是**模型**与**顶层**：`apps/core/authentication.py` 在模块级 import 过 `apps.business.peer_sync` 的纯函数（不引模型）⇒ ⛔ 别把这句读成「两个 app 之间零 import」。 ## 判据的证明力边界（如实登记，⛔ 别读成「跨租户已被完全证明」） - 它是**应用层**判据：同桶同凭据下，这是唯一那道门 ⇒ 必须排在**任何签发 / 读取之前**，且只用于订单截图这条路径（用户媒体**不给**判据，见 `apps/core/profile_views.py`）。 - 查询跑在**当前租户的库**上（租户上下文由调用方所在请求 / `set_tenant` 承载）—— ⛔ 本函数**不接受**租户参数（`AGENTS.md` §4：租户标识只能来自认证态）。 - 平台面（`public`）**没有** `business_*` 表，而两个平面共用同一份 urlconf ⇒ 不先挡这一下，平台面上的查询会 `relation does not exist` ⇒ 500。所以这里对平台面 **fail-closed**。"""
from __future__ import annotations

from apps.core.plane import on_public_plane
from apps.core.storage import SHARED_ORDER_PREFIX


def is_shared_object_key(key) -> bool:
    """这个对象名是否**在共享前缀下**（D-0316：全仓**唯一**一份同形实现）。归一化与护栏 `apps.core.storage.key_belongs_to_tenant` **同一套**：按 `/` 切段、剔除空段与
    `.`、任何 `..` 一律拒，再要求首段 == `SHARED_ORDER_PREFIX`（⛔ 不另抄字面量）。 ⚠️ **它只判形状，不判「本租户持不持有这张单」**。跨租户读的那道门是 `key_belongs_to_tenant`
    ＋ `order_screenshot_key_is_held` 的组合；本函数服务于三处**不需要**持有判据的地方：发送侧补发（只发本租户库里的行）、接收侧形状闸（随后还要查本单 / 别的单持有关系）、群码字节保留判据（只决定"要不要删自己那份字节"）。"""
    if not isinstance(key, str) or not key:
        return False
    parts = [p for p in key.split("/") if p not in ("", ".")]
    return bool(parts) and ".." not in parts and parts[0] == SHARED_ORDER_PREFIX


def order_screenshot_key_is_held(key: str) -> bool:
    """本租户库里有没有一行 `OrderScreenshot` 指着这个对象名。 ⚠️ 只在**当前**租户的库上查（⛔ 无租户参数）：调用方负责把连接放在正确的租户上下文里。
    ⚠️ 平台面上**直接回 False**（见模块 docstring：那里没有业务表）。 ⚠️ 模型 import **函数内**（同 `apps.core.plane` 的做法）：本模块会被 `apps/core` 侧延迟
    import，模块级 import 租户层模型会把「基座层不依赖租户层」这条约定反过来。"""
    if on_public_plane():
        return False
    from apps.business.models import OrderScreenshot

    return OrderScreenshot.objects.filter(object_key=key).exists()


def _safe_origin_segment(value) -> str:
    """这个候选值能不能当共享前缀的**分段**（M-c；⛔ 不合法就回落下一级）。 ⚠️ **候选值是对端可控的**（`PeerOrderLink.origin_order_no` 来自载荷的 `source_order_no`，
    兜底用的 `peer_order_no` 来自 `target_ref`）⇒ 必须过闸： - 含 `/`：会凭空多出一段，把图放进**别的命名空间**； - 含 `..`：对象名**永远**过不了护栏（`key_belongs_to_tenant` 对 `..` 段一字不让）
    ⇒ 这条链的截图从此再也传不上去（跨租户 DoS，而且本地看不出原因）； - 空 / 纯空白：`shared-orders//abc.jpg` 会把所有没有链身份的单挤进**同一段**。非 `str` 一律拒（列表 / 数字被 `str()` 成 `"[1, 2]"` 一类是静默错身份）。"""
    if not isinstance(value, str) or not value.strip():
        return ""
    if "/" in value or ".." in value:
        return ""
    return value


def order_shared_origin(order) -> str:
    """这张单在共享前缀里用的**分段值**（裁决 R-P4-12；起末级改来源单号）： 1. 有 `PeerOrderLink` 且 `origin_order_no` **可用** ⇒ 用它（链身份）； 2. 有链行但该列不可用（为空 / 对端塞了 `/` 或 `..`，或 P1–P3 建的**老行**——迁移不回填，裁决 R-P4-14）⇒ 用 `peer_order_no`：对老行来说**对端单号就是链的起点**
    （起新行这一列的语义是**对端的来源单号**）； 3. 根本没有链行（**链头**或不上链的本地单）⇒ 用**来源单号** （`jidanpu_member.effective_source_order_no(order)`，经 `peer_sync.effective_peer_ref` 转发；为空才回落 `order.order_no`）——业务方：跨系统一律用来源单号做身份。为什么末级也要改：同一条链的下游那一行 `origin_order_no` 取的正是在上家那里算出的
    来源号（收向建行时写 `target_ref`）⇒ 链头自己仍用内置号的话，同一张单的图会分在两个段下。 ⚠️ 链头**没有** `PeerOrderLink` 行（全仓唯一的建行点在接收侧 `apps/business/peer_inbound.py`），所以不能对它分叉。 ⛔ **绝不产出空串 / 带 `/` 或 `..` 的段**（`shared-orders//abc.jpg` 会把所有没有链身份的单挤进同一段；`..` 会让这条链的图**永远**过不了护栏）：每一级都过 `_safe_origin_segment`，最后一级 `Order.order_no` 是系统生成的非空列（来源号是**用户填的**，所以它也要过闸）。"""
    from apps.business import jidanpu_member
    from apps.business.models import PeerOrderLink

    peer_order_no_fallback = ""
    for link in PeerOrderLink.objects.filter(order=order).order_by("pk"):
        origin = _safe_origin_segment(link.origin_order_no)
        if origin:
            return origin
        if not peer_order_no_fallback:
            peer_order_no_fallback = _safe_origin_segment(link.peer_order_no)
    if peer_order_no_fallback:
        return peer_order_no_fallback
    # ⚠️ 函数内 import：本模块会被 `apps/core` 侧延迟 import（见模块 docstring 的
    #    「模型 import 放在函数里」），模块级拉 `jidanpu_member` 会破坏那条边界。
    origin = _safe_origin_segment(jidanpu_member.effective_source_order_no(order))
    if origin:
        return origin
    return order.order_no
