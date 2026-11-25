"""打手侧**对外**端点：**抢单**（`POST /api/taker/v1/hall/<order_no>/claim/`，spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 2 行 / §5.3-3 / §5.4 / §9 判据清单）。装配=Bearer+⛔ 空 `permission_classes`+兜底闸；幂等必带 `Idempotency-Key`（`@idempotent(spec=TAKER_SPEC)`）；无 body（多余 body 既不 500 也不误判 `1000`）。
承重：原生接单仅调用 `claim.claim_order`；两类镜像各调用自己的领域主体；⛔ 本模块不复制接单闸门；`1002` 同形文案承重（查不到、不在范围或不在大厅共用一句，防枚举单号）。
⛔ 不许调 `mark_side_effect`（抢单失败必须能同键重试**真抢到**；判据=`tests/test_takerapi_claim_api.py` 反例）；幂等回放不重查范围判据=如实登记的边界（旧键重发拿到旧信封，不写库不泄漏新信息；事实以查单/我的单为准）；事务边界照内部 `/claim`：可预期失败一律 `set_rollback(True)` 再 return。"""
from __future__ import annotations

from django.db import transaction
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.claim import ClaimError, claim_order
# ⚠️ `_has_taker` 是私有名，但它是"这条 MAIN 绩效行是否已归属某人"在**领域层的那一份**判据
# （`claim_order` 用它把"已被接走"翻成 `1012`）。⛔ 不在这里另写一份 `Performance` 查询
# （第二份判据，漂移侧="该回 1002 还是 1012"这条对外契约）。
from apps.business.claim import _has_taker
from apps.business.claim_channel import CHANNEL_TAKER_API
from apps.business.game_scope import game_scope
from apps.business.models import Order, OrderStatus, PeerHallMirror
from apps.business.order_status import IllegalTransition
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.openapi.idempotency import idempotent
from apps.rbac.permissions import nodes_of
from apps.takerapi.api.hall_views import (
    mirror_claim_response,
    mirror_pk_for_view_no,
    peer_claim_response,
    peer_mirror_pk_for_order_no,
)
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.throttling import TakerAppFallbackThrottle

#: 留痕操作人（形如「打手侧应用 测试应用甲」）。与上家侧那句（`开放平台应用 {}`）**刻意不同**：
#: 读日志的人要一眼看出是**哪一族**凭据干的（与两个 app 动作码分开是同一条理由）；
#: `OperationLog.actor` varchar(150)、应用名上限 50 ⇒ 最长 56，撞不到列宽。
TAKER_API_ACTOR_TEMPLATE = "打手侧应用 {}"

#: 留痕 `reason` 列的值 = **渠道**（`order.claim` 的模板里就是 `{渠道}` 那个槽位，
#: 内部那条路写的是「系统网页」）。⚠️ 这不是"自造对标的第三种渠道"：本仓**自己的**对外通道
#: 就是新的一条渠道；照抄「系统网页」会是一句**假话**（这一次不是网页点的）。


#: `1002` 那一句。⭐ **同形**是承重的（spec §5.3-2 的"与查不到同形"）：不存在 / 不在他的游戏范围 /
#: 不在大厅 / 孤儿订单**四种成因共用这一句** ⇒ 调用方无法拿文案差别把"本租户有没有这个单号"枚举出来。
NOT_IN_HALL_MSG = "该订单不存在或不在抢单大厅"

#: `1012` 那一句。⚠️ spec §5.4 要求写清"**包含你自己刚才已经接过**"——
#: 顺序双击 / 重发（换键）时调用方拿到的是**同一个码**，那句话是他唯一的解释。
ALREADY_TAKEN_MSG = "该订单已被接走（包含你自己刚才已经接过）"


def _live_order_in_scope(taker, order_no):
    """按单号取**当前租户的那张活单**并按**绑定账号的游戏范围**收窄；取不到 ⇒ `None`。租户上下文只来自连接 ⛔ 不读请求参数；默认 manager ⇒ 已软删取不到（D-0144，「取不到」与「不存在」同形）；范围用 `scope_queryset`+`game_scope`（与大厅读口同一份原语），主体是 **`taker`** ⛔ 不是 `request.user`（Bearer 下是匿名、游戏集空 ⇒ 每次抢单都 1002）。
    ⭐ D-0338 来源号兜底：内置号优先；来源号只认唯一的正式订单（未转正的镜像影子排除，随后由对应镜像回退接手）；镜像回退仍排在原生单之后。
    本函数是**本模块唯一**的 `Order.objects` 落点=抢单的取单口；⛔ 别在别的端点再手写一句 `Order.objects.filter(...)`（第二个判据，AGENTS §5-E）。"""
    qs = Order.objects.filter(order_no=order_no)
    qs = scope_queryset(taker, qs, scope_filter=game_scope("game_id"))
    order = qs.first()
    if order is not None:
        return order
    source_qs = Order.objects.filter(source_order_no=order_no)
    source_qs = source_qs.exclude(hall_card__isnull=False).exclude(
        upstream_hall_card__isnull=False
    )
    source_qs = scope_queryset(taker, source_qs, scope_filter=game_scope("game_id"))
    # ⚠️ `source_order_no` 不唯一：取至多两行，**恰好一行**才认（fail-closed）。
    matches = list(source_qs.order_by("pk")[:2])
    return matches[0] if len(matches) == 1 else None


class HallClaimView(APIView):
    """`POST /api/taker/v1/hall/<order_no>/claim/` —— 原生、来源号与镜像单接手入口（spec §5 端点表第 2 行）。装配三件与 `HallListView` 逐字相同（两条路径授权面相同，⛔ 不需要 `get_permissions` 按方法分装配）。
    **独立视图类**（⛔ 不与大厅那两条方法同住一个类）：两条契约**路径不同**、各自能匹配到自己的 pattern——「同路径两个方法只能同住一个类」那个约束在这里不存在（上家侧 `status/`、`images/` 是被那个约束逼成 mixin 的）。"""

    authentication_classes = [TakerAppBearerAuthentication]
    #: ⛔ 显式空列表 = **不做权限检查**（不写它会吃全局默认的 `IsAuthenticated` ⇒ 合法令牌反被判 401）。
    permission_classes = []
    #: 兜底闸（DRF 的 `initial()` 顺序 = 认证 → 权限 → 限流 ⇒ 401 先于 429，且不进桶）。
    throttle_classes = [TakerAppFallbackThrottle]

    allow_mirror_claim = True
    sync_personal_orders = True
    sync_peer_orders = True

    def get_native_order(self, taker, order_no):
        return _live_order_in_scope(taker, order_no)

    def prepare_native_order(self, order, *, taker):
        return order

    @idempotent(spec=TAKER_SPEC)
    def post(self, request, order_no):
        """抢单：无 body；成功 ⇒ `{order_no, status, status_text}`。
        `request.auth` 是 `TakerApp` 凭据（认证成功返回的 `request.user` 是匿名）⇒ 绑定账号只能从 `request.auth.taker` 取。"""
        app = request.auth
        taker = app.taker
        order = self.get_native_order(taker, order_no)
        if order is None:
            if not self.allow_mirror_claim:
                return api_response(msg=NOT_IN_HALL_MSG, code=ErrorCode.BUSINESS)
            # ⭐ 镜像回退顺序承重：原生优先后，先鸡蛋铺、再 peer；两路都复用各自专用端点的同一请求主体。
            # 列表里的展示号就是客户端拼接的 order_no；找不到或 peer 单号有歧义仍落同一个 1002 出口。
            mirror_pk = mirror_pk_for_view_no(taker, order_no)
            if mirror_pk is not None:
                return mirror_claim_response(request, mirror_pk=mirror_pk)
            peer_pk = peer_mirror_pk_for_order_no(taker, order_no)
            if peer_pk is not None:
                peer_response = peer_claim_response(request, mirror_pk=peer_pk)
                if peer_response.data["code"] != ErrorCode.OK:
                    return peer_response
                mirror = PeerHallMirror.objects.select_related("promoted_order").get(
                    pk=peer_pk
                )
                order = mirror.promoted_order
                return api_response(
                    data={
                        "order_no": order.order_no,
                        "status": order.status,
                        "status_text": order.get_status_display(),
                    }
                )
            # 不存在 / 不在他的游戏范围 / 已软删：**同一个出口**（spec §5.3-2 的"同形"）。
            return api_response(data=None, msg=NOT_IN_HALL_MSG, code=ErrorCode.BUSINESS)
        with transaction.atomic():
            order = self.prepare_native_order(order, taker=taker)
            if order is None:
                return api_response(msg=NOT_IN_HALL_MSG, code=ErrorCode.BUSINESS)
            try:
                claim_order(
                    user_id=taker.pk,
                    order_id=order.pk,
                    # ⭐ `nodes` 来自**绑定账号**（⛔ 不是常量集）：spec §9-6 的判据就是它。
                    nodes=nodes_of(taker),
                    # ⚠️ 显式写默认值：本路径是**人类入口**，按人那四道闸门一个字都不放宽
                    # （`claim.py` 的裁定 8：只有机器接手才传 `False`）。
                    enforce_limits=True,
                    sync_personal_orders=self.sync_personal_orders,
                    sync_peer_orders=self.sync_peer_orders,
                )
            except ClaimError as exc:
                # ⭐ **判据（含查库）必须在 `set_rollback(True)` 之前算完**：标记回滚后同一个 `atomic()` 块里
                # 再发查询抛 `TransactionManagementError`（执行期实测：先标记后查库 ⇒ 孤儿订单用例 ERROR）。
                # 这是这条支路唯一能跑通的顺序，不是"顺序好看"。
                orphan_order = (
                    exc.code == ErrorCode.ORDER_ALREADY_TAKEN
                    and order.status == OrderStatus.WAITING_GRAB
                    and not _has_taker(order.pk)
                )
                if orphan_order or exc.code == ErrorCode.BUSINESS:
                    # ⭐ **孤儿订单**：状态待抢单却一条 MAIN 绩效行都没有 ⇒ join 不上 `search_hall`、本来就不在厅；
                    # `claim.py` 把这种成因与"被别人抢走"不可区分（`rowcount==0`）⇒ 对外侧翻回 `1002`（spec §9-14 ⛔ 不是 500/1012），
                    # 判据用领域层那一份 `_has_taker` ⛔ 不另写查询；并发支不受影响（输家的 `order` 是陈旧读，`_has_taker` 当下再查 ⇒ 赢家已提交时 `True` ⇒ 照旧 1012）。「不在抢单大厅」走同一句 `NOT_IN_HALL_MSG`——同形才承重（spec §5.3-2）：两句不同=可探测信道。
                    code, msg = ErrorCode.BUSINESS, NOT_IN_HALL_MSG
                elif exc.code == ErrorCode.ORDER_ALREADY_TAKEN:
                    # ⚠️ 文案**换成本模块这一句**（⛔ 不是原样透传领域层那句）：spec §5.4 要求
                    # 对外写清"包含你自己刚才已经接过"。
                    code, msg = exc.code, ALREADY_TAKEN_MSG
                else:
                    # 其余是 `1013` 那五支（没有接单价 / 未实名 D-0245 / 禁自助接单 / 价格区间 /
                    # 持单上限）——
                    # 五支**共用一个码** ⇒ 文案必须能独立说明是哪一支，本模块原样透传（⛔ 不吞掉差别）。
                    code, msg = exc.code, str(exc)
                # 可预期失败 ⇒ 显式回滚（照内部 `/claim` 的形状）。⚠️ 如实登记（变异自证实测读数=`SURVIVED`）：这一行今天不是承重的——外层 `idempotency._run` 的 `atomic()` 在「码≠0 且未声明副作用」时整笔回滚，今天每条 `ClaimError` 都早于任何写入。
                # 保留三条理由（⛔ 不写成"缺它必红"）：① 与内部 `/claim` 同形（那里它是承重的：块内裸 return 会提交半截写入，E-0022 真现场）；
                # ② 将来声明 `mark_side_effect` 后 `_run` 不再回滚，这一行是唯一防线（变异实测）；③ 一行成本换掉"依赖外层回滚策略"的隐藏前提。
                transaction.set_rollback(True)
                return api_response(data=None, msg=msg, code=code)
            except IllegalTransition as exc:
                # 状态机拒绝 ⇒ `1002`（spec §5.4）。⚠️ 走到这里时绩效条件更新**已经写过行**，那半截写入必须不留下。
                # ⚠️ 如实登记（变异自证实测读数=`SURVIVED`）：今天也不是承重的（外层 `_run` 整笔回滚），但这是唯一真的"先写后拒"的支路——有人调 `mark_side_effect` 或改 `_run` 回滚策略 ⇒ 它立刻是唯一防线；
                # 判据是硬的能红的：`tests/test_takerapi_claim_api.py` 那条同键重试必须**真抢到**的反例（变异实测把它弄红）。
                transaction.set_rollback(True)
                return api_response(data=None, msg=str(exc), code=ErrorCode.BUSINESS)
            order.refresh_from_db()
            # ⚠️ 留痕与两笔业务写入**同一个事务**（本 `with` 块）：失败路径走不到这里 ⇒ 不会留下"没抢到、日志却写了"。
            # ⚠️ `actor_id=None`+`system=True` **必须成对**：这条路上没有"人"这个操作人，`record()` 对
            # "没操作人却没声明 system"是 fail-closed 抛错（⛔ 不是静默落成机器行）。
            record(
                log_type=ACTION_LOG_TYPES["order.claim"],
                action="order.claim",
                actor=TAKER_API_ACTOR_TEMPLATE.format(app.name),
                actor_id=None,
                system=True,
                # ⚠️ `target` 是**接单人**（= 绑定账号）：`order.claim` 的模板里 `{actor_name}` 就是
                # 接单人（对标那句"【某人】通过接单大厅自助接手此单"），本条路上"接单人"与
                # "操作人"不是同一个人（操作人是应用）⇒ 名字进 `target_name`（R11）。
                target_kind="user",
                target_id=taker.pk,
                target_name=user_display_name(taker),
                order_no=order.order_no,
                source_order_no=order.source_order_no,
                to_value=order.hall_price,
                reason=CHANNEL_TAKER_API,
            )
        return api_response(
            {
                "order_no": order.order_no,
                "status": order.status,
                "status_text": order.get_status_display(),
            }
        )
