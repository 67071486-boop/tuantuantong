"""批 O-RULES（Task 3 / D2）的测试专用帮手。

⚠️ 只服务本线的测试：`allow_taker_release` 是**产品动作**（显式清除当前接单人）在
状态机上的开关，⛔ 不是「凡是清人的都算」的通用豁免（R1：例外只给**恰好两处**调用点，
见 `docs/specs/2026-09-28-order-page-rules-design.md` §4.1.2 的枚举表）。
本模块把「测试里要造一次产品动作」这件事收成一处，避免各用例各写一份开关
（2026-09-28 修复轮 1 当场 `grep` 实测：**11 处调用点**）。
"""
from apps.business.models import OrderStatus
from apps.business.order_status import apply_transition


def release_taker(order, *, nodes, actor=""):
    """以**产品动作**的口径回池（= `/clear-taker/` 走的那条路，R1 的例外开关）。

    ⚠️ 用了它 = 测试里在做「显式清除当前接单人」这件事；⛔ 不是「为了绕过新闸」。
    要测「新闸真的拒」就**不要**用它（见 `test_dispatch_api.TakerPresentPoolEntryGuardTests`
    里那条反向钉子）。
    """
    return apply_transition(
        order, OrderStatus.UNASSIGNED, nodes=nodes, actor=actor,
        allow_taker_release=True,
    )
