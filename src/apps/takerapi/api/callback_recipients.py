"""打手侧回调推送的**收件人**与四个 `notify_*` 包装（spec `docs/specs/2026-10-09-taker-callback-design.md` §4.2 收件人判据 / §4.1 四类事件，本模块是唯一出处）。
承重：收件人=**只有「绑定账号==这张单当前接手人」的应用**（⛔ 不许退化成"推给全租户"=上家侧语义）；四个包装每个只做两件事（算收件人+调 `enqueue_deliveries`），⛔ 没有第二份引擎（收件人/去重/投递/退避/签名各只有一份实现）；`order.reassigned` 收件人是**前任**、`former_taker_id` 由入队点显式传（⛔ 不在本函数查库猜前任——那时库里已没有前任这个事实）；这四个**不加**族守卫（`spec=TAKER_SPEC` 写死、收件人只取打手侧 ⇒ 不存在"传错族"路径，按形状不需要，AGENTS §5-E）。
⛔ 不排 `on_commit`、⛔ 不发 HTTP、⛔ 不自己开事务；入队点清单=spec §4.3（紧挨既有 `notify_*` / `enqueue_order_withdrawn` 调用、同一事务同一 `occurred_at`）；事件名只从 `TakerApiDelivery.Event` 取；`order.reassigned` 今天不去重（引擎 `_identity_of` 对第 4 类回空串——每次"不再是你的"都是新事件，⛔ 本模块不补判据、⛔ 不改共用引擎）。"""
from __future__ import annotations

from apps.business.models import EmpType, Performance
from apps.openapi.dispatch import enqueue_deliveries
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.models import TakerApiDelivery, TakerApp


def current_taker_id(order) -> int | None:
    """这张单的**当前接手人**（MAIN 绩效行的 `user_id`），取不到 ⇒ `None`（判据逐字=spec §4.2）。接手人身份只有 `EmpType.MAIN` 行表达；一条查询只拿主键（调用方只要"归谁"，且在全仓最热写入口旁跑）。
    `None` 两种成因（孤儿单 / MAIN 行 `user` 为空——抢单指派前合法为空）映射同一结果 ⛔ 都不许退化成"推给全租户"。与 `order_scope.claimed_perf_of` 取数形状同源但判据不同（这里**不**判软删：软删那一刻恰恰要推给"刚刚不再是他的"人）⇒ ⛔ 不拿它当本模块判据、也⛔ 再写一份授权判据。
    `order=<实例>` ⛔ 不是按单号再查（入队点现场就有实例）；本条在 `test_order_read_path_net` 账上不计数（`order=<实例>` 两者都不是）。"""
    return (
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN)
        .values_list("user_id", flat=True)
        .first()
    )


def taker_apps_for(taker_id: int | None) -> list[TakerApp]:
    """这个打手名下**会收推送**的应用（spec §4.2 三项判据，**与**关系），按 `id` 升序。取值全取自 `TAKER_SPEC`（族数据 ⛔ 不写字面量）：`state`=启用（停应用停整条通道）、`callback_state`=启用（停回调只停推送）、`callback_url` 非空。这三项是 spec §4.2 的唯一实现 ⛔ 包装里不许再看一眼应用状态（第二份判据必漂）。
    `taker_id is None` ⇒ 空列表（孤儿单=零行，spec §9-1 反例）；零应用即零成本立刻返回（引擎的空判是第二道 ⛔ 不靠它兜）。
    `taker_id`/`state` 列名是**字面量**（打手侧自己的列，上家侧表没有——不属于"两族逐字相同"那几列，所以不走 spec）。"""
    if taker_id is None:
        # ⭐ 孤儿单 / 没人接 ⇒ **没有收件人**（spec §4.2 的 `if taker_id is None: return []`）。
        #    ⛔ 不许在这里"顺手"回退成"本租户全部启用应用"——那是上家侧的语义。
        return []
    return list(
        TakerApp.objects.filter(
            taker_id=taker_id,
            state=TAKER_SPEC.enabled_state_value,
            **{TAKER_SPEC.callback_state_field: TAKER_SPEC.callback_enabled_value},
        )
        .exclude(**{TAKER_SPEC.callback_url_field: ""})
        .order_by("id")
    )


def notify_order_status_changed_for_taker(order, *, status, occurred_at) -> list:
    """`order.status_changed`（spec §4.1）：**他接的单**状态变了 ⇒ 推给他名下应用。只做两件事：算收件人（当前接手人）⇒ 调 `enqueue_deliveries`；⛔ 不排 `on_commit`、⛔ 不发 HTTP、⛔ 不自己开事务（模块 docstring）。
    `status` **必填** ⛔ 不从 `order.status` 取（有入队点是跨行 `.update()`、实例是旧的，错得很安静）；`occurred_at` **必填**（至少软删那处不改 `updated_at`，默认取它会给出假时刻）；载荷 `status`/`note` 两键与上家侧逐字同形（对外契约 spec §8-2，⛔ 不是抄）。"""
    return enqueue_deliveries(
        spec=TAKER_SPEC,
        apps=taker_apps_for(current_taker_id(order)),
        event=TakerApiDelivery.Event.ORDER_STATUS_CHANGED,
        order=order,
        occurred_at=occurred_at,
        identity=str(status),
        extra={"status": str(status), "note": ""},
    )


def notify_order_deleted_for_taker(order, *, occurred_at) -> list:
    """`order.deleted`（spec §4.1）：单被**软删** ⇒ 推给**当前**接手人名下应用。收件人判据删前删后都成立：`Performance` 是普通 manager（`deleted_at` 只是标记）⇒ 软删时一起被标记的 MAIN 行照样查得到（不需要"删前先存"特殊处理）。
    `occurred_at` **必填**（软删的 `update_fields` 不刷 `updated_at`，从它取=更早的假时刻）；载荷无额外键；去重看"最近一条行仍在飞"（引擎档）⇒ 「删→恢复→再删」的第二次删排得出去。"""
    return enqueue_deliveries(
        spec=TAKER_SPEC,
        apps=taker_apps_for(current_taker_id(order)),
        event=TakerApiDelivery.Event.ORDER_DELETED,
        order=order,
        occurred_at=occurred_at,
        identity="",
    )


def notify_order_image_added_for_taker(
    order, *, object_key, img_type=0, remark="", occurred_at
) -> list:
    """`order.image_added`（spec §4.1）：进度图认领成功 ⇒ 推给**当前**接手人名下应用。⭐ 载荷形状照上家侧那个包装——是**引擎的承重依赖**：投递时 `_wire_payload` 按 `payload["images"][0]["object_key"]` 现取预签名 URL ⇒ 形状少一格=所有图片推送带空 url 且台账显示"投递成功"。
    库存稳定身份（`object_key`）、报文给预签名 URL（生产 COS 为整点对齐的 1–2 小时）——有意不同（理由见上家侧包装 docstring，⛔ 不重抄）；去重比 `images[0].object_key`（同一张图只推一次）；`occurred_at` 必填。"""
    return enqueue_deliveries(
        spec=TAKER_SPEC,
        apps=taker_apps_for(current_taker_id(order)),
        event=TakerApiDelivery.Event.ORDER_IMAGE_ADDED,
        order=order,
        occurred_at=occurred_at,
        identity=str(object_key),
        extra={
            "images": [
                {"object_key": str(object_key), "img_type": img_type, "remark": remark}
            ]
        },
    )


def notify_order_reassigned_for_taker(
    order, *, former_taker_id, reason, occurred_at
) -> list:
    """⭐ `order.reassigned`（spec §4.1/§4.2 末段）：**该单不再属于原来那位接手人**。收件人是**前任** ⛔ 不是当前接手人（四类里唯一）——入队那一刻 `Performance.user` 已变成别人或被清空，要通知的正是"你不再是这单的人"那个人，现任拿到会以为自己刚被撤单。
    `former_taker_id` 由入队点显式传入 ⛔ 不在本函数查库猜前任（库里已没有"前任"事实，历史值只能由入队点自带；入队点本来就握着前任局部变量）；`None` ⇒ 零行（入队点已有 `is not None` 挡着，这里再兜一次是同一条判据的第二道 ⛔ 不是新增判据）。
    `reason` 三取值（`taker_removed`/`refunded`/`deleted`，机器可读）：由入队点沿用既有 peer 撤单事件的取值，⛔ 本模块不校验、不在两处各写枚举；本类事件今天不去重（引擎 `_identity_of` 对第 4 类回空串，模块 docstring）⇒ `reason` 是给对端读的语义 ⛔ 不是去重键。"""
    return enqueue_deliveries(
        spec=TAKER_SPEC,
        apps=taker_apps_for(former_taker_id),
        event=TakerApiDelivery.Event.ORDER_REASSIGNED,
        order=order,
        occurred_at=occurred_at,
        identity=str(reason),
        extra={"reason": str(reason)},
    )
