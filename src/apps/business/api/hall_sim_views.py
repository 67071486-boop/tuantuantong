"""接单大厅**模拟单**的「假接手」端点（D-0296，业务方）：`POST /api/business/hall/sim/<pk>/claim/` —— 押金闸过了就把模拟单按下架（active → retired）回「已被接手」；押金不够回**与真单逐字相同**的押金提示且卡片不下架。这是**演示用**假接手：模拟单不是真单（不建 `Order` 行、不进状态机 / 结算），点「接手」的业务效果就是**这张演示卡被人拿走了**——回 `1012`（已被接走）+ 一句人话，为的是与真单抢输的**交互形状完全一致**（前端不用写第二套失败处理）。⚠️ **幂等**：对已 `retired` 的卡再点，条件更新命中 0 行、响应不变——「再点一次」与「点一张别人已拿走的卡」业务上是同一件事。
⚠️ **可见性闸（X-01，业务方两次裁定）**：读侧大厅看不见模拟卡的三类人（PC / H5 会话、开了个人免押的老打手、钱包里还冻着押金的打手）**写侧同样点不动**——判据是 `hall_simulated.viewer_sees_simulated`（**与读侧同一份函数**，⛔ 不另抄三条件）；不满足 ⇒ 回**与「已被接走」逐字相同**的 `1012` 信封且**卡片不下架**：看不见的人不该能从「有 / 没有 / 押金不够」的差别里猜出这张卡存不存在（与读侧「整族隐藏」同口径，顺带关掉 `1013`/`1012` 的弱存在性 oracle）；闸先于押金闸跑（隐形人连押金判定都不进）。
⚠️ **押金闸（业务方口径「和正常订单一样的流程」）**：闸开着时，点模拟单也要先过 `ensure_deposit_for_claim` 的同一套判定——不够 ⇒ 回**与真单逐字相同**的 `WalletError` 人话 + `1013` 且**卡片不下架**（真单抢输也不下架）；够 ⇒ 校验全程在一个**回滚的**事务里（结算 / 冻结当场撤销，一分钱不动），出来才按下架；闸关着 ⇒ 完全不碰押金，与真单同口径。
"""
from django.db import transaction
from django.utils import timezone
from rest_framework.views import APIView

from apps.accounts.wallet import WalletError, ensure_deposit_for_claim
from apps.business.deposit_gate import deposit_enforced_for
from apps.business.hall_simulated import viewer_sees_simulated
from apps.business.models import HallSimulatedOrder
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 假接手的固定人话（**逐字**契约：前端按它渲染提示，测试按它钉文案）。
SIM_ALREADY_TAKEN_MSG = "此单已被人接手，请刷新"


class HallSimClaimView(APIView):
    """`POST /api/business/hall/sim/<int:pk>/claim/` —— 模拟单假接手。⚠️ 节点门与真单抢单**同一颗**（`grab_hall.take_order`）：能接真单的人才点得动演示卡（照 `JidanpuUpstreamHallClaimView` 的同一道门）。⚠️ **可见性闸**（X-01）：`post()` 第一件事就是读侧同一道三条件判据（`hall_simulated.viewer_sees_simulated`，模块 docstring 有为什么）——读面看不见演示卡的人，这里也只拿得到「已被接走」那封信封。
    ⚠️ **不做游戏权限 scope**：模拟卡是演示数据、零客户信息，拿走它不产生任何数据 / 资金可见性（与真单 claim 的越权面**不同级**）；节点门 + 可见性闸即全部准入。
    """

    permission_classes = [has_node("grab_hall.take_order")]

    def post(self, request, pk):
        # ⭐ 可见性闸（X-01）：读侧看不见模拟卡的三类人（PC 会话 / 个人免押 / 有冻结押金）写侧同样点不动。判据与读侧**同一份函数**（`hall_simulated.viewer_sees_simulated`，⛔ 不另抄三条件）；不满足 ⇒ 回**与「已被接走」逐字相同**的 `1012` 信封、卡片不动——不给「这个 pk 有没有行 / 押金够不够」的差别留缝（读侧整族隐藏，写侧同口径是「拒绝也长得像别人先拿走了」）。
        # 闸排在押金闸**之前**：隐形人连 `ensure_deposit_for_claim` 都不该进（否则 `1013`/`1012` 可区分「卡存在」）。fail-closed：函数对任何异常形状都按「不可见」走。
        if not viewer_sees_simulated(request):
            return api_response(
                data=None, msg=SIM_ALREADY_TAKEN_MSG, code=ErrorCode.ORDER_ALREADY_TAKEN
            )
        now = timezone.now()
        # ⭐ 押金闸（业务方口径「和正常订单一样的流程」）：闸开着时用 `ensure_deposit_for_claim` 的**同一套判定与同一句文案**（⛔ 不另抄一份金额比较 / 文案模板）。整个校验包在一个**回滚的**事务里：真流程里的「结算到期解冻 + 补冻结」当场撤销、净写入为零；不够 ⇒ 逐字透传人话 + `1013` 且**不下架**（真单抢输不下架，同一形状）。闸关着 ⇒ 不进这段，与真单同口径。
        if deposit_enforced_for(request):
            card = HallSimulatedOrder.objects.filter(pk=pk).first()
            if card is not None:
                with transaction.atomic():
                    try:
                        ensure_deposit_for_claim(
                            request.user, card.game, ref_no=card.display_order_no
                        )
                        transaction.set_rollback(True)
                    except WalletError as exc:
                        transaction.set_rollback(True)
                        return api_response(data=None, msg=str(exc), code=exc.code)
        HallSimulatedOrder.objects.filter(
            pk=pk, status=HallSimulatedOrder.Status.ACTIVE
        ).update(
            status=HallSimulatedOrder.Status.RETIRED, retired_at=now
        )
        # ⛔ **无论 rowcount** 都回同一封信封（模块 docstring 的「为什么」）：
        # 命中 1 = 本次拿走；命中 0 = 别人已拿走（或它根本不在）——对调用方是同一句话。
        return api_response(
            data=None, msg=SIM_ALREADY_TAKEN_MSG, code=ErrorCode.ORDER_ALREADY_TAKEN
        )
