"""镜像卡派生的**隐藏单**（`Order.hall_card`，业务方拍板）。 ## 为什么要有这张单团团通大厅里摆的是「上家推来的镜像卡」（`PeerHallMirror`，**不是**我方 `Order`）。而「抛到下家大厅」那条同步（`jidanpu_member.queue_hall_launch`）只认 `Order` ⇒ 卡在厅里时，开了 `JidanpuMemberBinding.hall_sync_enabled` 的下家鸡蛋铺**收不到**它。业务方的口径是「**只要团团通大厅有单（含镜像卡），所有开启了大厅同步的工作室都要收到**」 ⇒ 给每张落成 `listed` 的卡派生一张 `Order`（`hall_card` 指回那张卡），建单后立刻走 **既有** `queue_hall_launch(order)`（它自己按 `hall_sync_enabled` 过滤，⛔ 本模块不另写一套）。
## 它为什么必须**隐藏** 这张单只是「把卡片同步给下家大厅」的载体，**不是**本租户的一笔生意：它没有客户、没有绩效、金额也不是本租户的收入。让它出现在订单管理列表 / 统计 / 导出里会把报表撑大、还会让人去点一张本该由大厅处理掉的单。⇒ 所有面向人的订单读面统一 `exclude(hall_card__isnull=False)`（读点登记：`tests/test_order_read_path_net.py` 与各读点自身的注释）；**详情按单号仍可打开**（客服排障要看得到，⛔ 不是 404）。 ## 生命周期（三条，各有唯一入口） | 时刻 | 谁调 | 做什么 | |---|---|---| | 卡落成 `listed`（推送支 / 拉取支共用 `peer_inbound._land_hall_throw`） | `ensure_for_card` | 建（`WAITING_GRAB`）或**复活**同一张（卡 `gone` 后再见到）；建 / 复活后 `queue_hall_launch` |
| 本租户打手把卡抢走、卡转正 | `peer_inbound._promote_claiming_mirror` | **同一张单接着用**：挂打手 / 写展示价 / 建绩效行 / `IN_PROGRESS`，并清空 `hall_card`（转正后它就是我方真单，必须回到列表） | | 卡 `gone`（摘牌 / 关开关 / 解绑 / 卡死回收按证据下架） | `refund_for_card` | 落 `REFUNDED`（**不清 `hall_card`**：退掉的派生单仍在隐藏面），并走既有 `jidanpu_member.queue_withdraw` 把下家大厅里的副本退掉 | ⚠️ **1:1（`Order.hall_card` 是 OneToOne）**：同一张卡只能有一张隐藏单；重复推送 / 拉取刷新都走 `get_or_create`，⛔ 不会建出第二张。 ⚠️ **卡是 `gone` / 转正后的终态 ⇒ 不建**（`ensure_for_card` 第一道闸）：`gone` 的卡不在厅里，给它派生一张待抢单等于凭空造一张没人能接的单。 ⚠️ **⛔ 不在这里建绩效行**：隐藏单无人接手（绩效行是「接单」的事实，转正那一刻才建）。"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from apps.business.models import Order, OrderStatus, PeerHallMirror

logger = logging.getLogger(__name__)


def _payload(card: PeerHallMirror) -> dict:
    """卡的上家原始行（`PeerHallMirror.payload` 是 JSONB，可能为空 / 不是 dict）。"""
    payload = card.payload
    return payload if isinstance(payload, dict) else {}


def _total_price(payload: dict) -> Decimal:
    """隐藏单的「客户价」= 对端发单价（与 `peer_inbound.apply_events` 建单支同一口径）。 ⚠️ 它**不是**本次业务关心的那一个价：抛给下家大厅用的是 `hall_price`（展示价）。
    这里照建单支的形状落一列，是为了转正时这张单的字段与「新建一张」逐字同形。 ⚠️ 无价 / 坏形状 ⇒ `0.00`（`total_price` 是非空列；「没带价」这个事实由
    `hall_price IS NULL` 表达，与真单同一条口径）。"""
    value = payload.get("hall_price")
    if value is None:
        return Decimal("0.00")
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def _text(payload: dict, key: str) -> str:
    """`remark` / `content` 的类型闸——复用 `peer_inbound` 的**同一份**判据（A1 / H-1）。"""
    from apps.business.peer_inbound import _peer_text_or_empty

    return _peer_text_or_empty(payload, key)


def _clipped(value: str, model, field_name: str) -> str:
    """按列宽截断（裁决 R19）——复用 `peer_inbound._clip_to_column` 的同一份口径。"""
    from apps.business.peer_inbound import _clip_to_column

    return _clip_to_column(value, model, field_name)


def ensure_for_card(card: PeerHallMirror, *, now=None) -> Order | None:
    """卡**在厅里** ⇒ 取/建它的隐藏单；建了或复活了就往开启同步的工作室抛一次。回隐藏单（拿不到前置时回 `None`）： - `card.status != listed` ⇒ `None`（`gone` / `claiming` / `promoted` 都不该派生待抢单）； - `card.game is None` ⇒ `None`（`Order.game` 非空；正常落地路径 `resolve_game` 恒给行，
    这一档只防数据被手改成畸形，⛔ 不为它建单）； - 已有隐藏单且仍 `WAITING_GRAB` ⇒ 原样返回（重复推送 / 拉取刷新天然幂等）； - 已有隐藏单但已 `REFUNDED`（卡 `gone` 后又复活）⇒ **同一张**单回 `WAITING_GRAB`，重新 `queue_hall_launch`（⛔ 不新建第二张）。 ⚠️ **价取 `display_price(card)`**（大厅里看到的那个价：上家价 × 本绑定百分比），
    无价则 `hall_price = None`（⛔ 不补 0，与真单的无价口径一致）。 ⚠️ 游戏 / 项目 / 展示字段按**卡的载荷**落；区服 / 项目 FK **刻意留空**：载荷里的 `server_id` / `project_id` 是**对端**的主键（R21 之下不消费）。 ⚠️ `queue_hall_launch` 的闸门（`hall_sync_enabled` / 启用 / 范围 / 同地址去重） **一处都不在这里复制**——那是它的既有判据（D-0299）。"""
    from apps.business import peer_hall

    if card is None or card.status != PeerHallMirror.Status.LISTED:
        return None
    if card.game_id is None:
        logger.warning(
            "镜像卡没有游戏，跳过隐藏单派生：binding=%s card=%s",
            card.peer_upstream_binding_id, card.peer_order_no,
        )
        return None
    payload = _payload(card)
    price = peer_hall.display_price(card)
    defaults = {
        "order_no": _new_order_no(),
        "status": OrderStatus.WAITING_GRAB,
        "game": card.game,
        # R21：区服 / 项目是**对端**的主键，不落我方 FK；项目名按自由文本落（列宽截断）。
        "server": None,
        "category": None,
        "project": None,
        # ⭐ N4（R3-M5）：`project_text` 与联系方式都是**请求来源值**——先过与
        #    `remark` / `content` 同一份类型闸（`_text`），再按列宽裁剪；
        #    容器（dict / list）按「没带该字段」落空串，⛔ 不 `str(dict)` 硬塞。
        "project_text": _clipped(_text(payload, "project_text"), Order, "project_text"),
        "total_price": _total_price(payload),
        "hall_price": price,
        # 展示字段照真单的建单支落（转正后这张单就是客户看到的那张单）。
        "remark": _text(payload, "remark"),
        "content": _text(payload, "content"),
        "customer_phone": _text(payload, "customer_phone"),
        "customer_qq": _text(payload, "customer_qq"),
        "source_order_no": card.peer_order_no,
    }
    # ⚠️ **必须走 `all_objects`**：默认 manager 看不见已软删的行 ⇒
    # `get_or_create` 会去**新建**一张，而 `Order.hall_card` 是 OneToOne（0099 的唯一索引）
    # ⇒ 撞库 500。软删之后同一张卡再落厅是**可能发生**的（历史删除 / 人工清理），修法 = 查回同一行并**复活**它（1:1 是承重口径，⛔ 不新建第二张）。
    order, created = Order.all_objects.get_or_create(hall_card=card, defaults=defaults)
    if created:
        _queue_launch(order)
        return order
    revivable = order.deleted_at is not None or order.status == OrderStatus.REFUNDED
    if not revivable:
        return order
    # 复活：卡 `gone` 后又回到大厅 / 隐藏单被软删后卡又落厅 ⇒ **同一张**单回待抢。
    # ⚠️ 条件更新用 `all_objects`（默认 manager 的软删过滤对 `.update()` 的**匹配集**生效：
    #    用 `Order.objects` 会让已软删那一行匹配 0 行，复活静默失败）。
    updated = Order.all_objects.filter(pk=order.pk).filter(
        Q(status=OrderStatus.REFUNDED) | Q(deleted_at__isnull=False)
    ).update(
        status=OrderStatus.WAITING_GRAB,
        # ⭐ N4（R3-M5）：复活不是「只把状态翻回待抢」——卡这次带来的**内容**也要一起刷，
        #    否则消费端 `_consume_launch`（按单号现查 DB）会把首建时的旧游戏 / 旧备注 /
        #    旧项目 / 旧联系方式重新抛给下家。取值与建单支**同一份** `defaults`（⛔ 不抄第二份）。
        game=card.game,
        project_text=defaults["project_text"],
        total_price=defaults["total_price"],
        hall_price=price,
        remark=defaults["remark"],
        content=defaults["content"],
        customer_phone=defaults["customer_phone"],
        customer_qq=defaults["customer_qq"],
        source_order_no=card.peer_order_no,
        deleted_at=None,
        deleted_by="",
        updated_at=now or timezone.now(),
    )
    if not updated:
        # 读到落写之间被别人改了（并发复活 / 人工处置）⇒ 尊重那一次。
        return order
    from apps.business.order_writes import queue_externalpay_recheck
    queue_externalpay_recheck(order)
    # 实例回填：同一事务里的后续代码 / 返回值可能读这个实例（⚠️ 重抛内容的**判据仍是
    # `.update()` 落的那一行**——消费端按单号现查 DB，⛔ 别把回填当成"内容已刷新"的证明）。
    order.status = OrderStatus.WAITING_GRAB
    order.game = card.game
    order.project_text = defaults["project_text"]
    order.total_price = defaults["total_price"]
    order.hall_price = price
    order.remark = defaults["remark"]
    order.content = defaults["content"]
    order.customer_phone = defaults["customer_phone"]
    order.customer_qq = defaults["customer_qq"]
    order.source_order_no = card.peer_order_no
    order.deleted_at = None
    order.deleted_by = ""
    _queue_launch(order)
    return order


def _new_order_no() -> str:
    """订单号走**既有**生成器（⛔ 不在这里另拼一段数字）。"""
    from apps.business.order_no import generate_order_no

    return generate_order_no()


def _queue_launch(order: Order) -> int:
    """把这张单抛给所有「开启了大厅同步」的下家鸡蛋铺绑定（既有钩子，⛔ 不新造过滤）。"""
    from apps.business import jidanpu_member

    return jidanpu_member.queue_hall_launch(order)


def sync_hidden_order_price(card: PeerHallMirror, *, now) -> bool:
    """把**大厅展示价**同步到这张卡派生出的隐藏单上（`hall.reprice` / 改百分比的唯一落价口）。回「这次真的写了吗」——`False` 覆盖三种 no-op（无卡 / 算不出价 / 单不在待抢 / 同值）。 ## 为什么只动 `WAITING_GRAB` 隐藏单只有**还在大厅里**（`WAITING_GRAB`）时才是「下家大厅那张卡的本地载体」；一旦转正成 `IN_PROGRESS` / `COMPLETED` / `REFUNDED`，它的价就是那张真单的生意价，
    ⛔ 不能被上家后续的改价（或本层百分比调整）改写。 ## 价从哪来 / 什么时候不写 - 唯一算法出处 `peer_hall.display_price(card)`（上家价 × 本绑定百分比），⛔ 不另算百分比； - `display_price` 回 `None`（上家没给价）⇒ **no-op**：⛔ 不把已有的价擦成 NULL（E-0021 同族）； - 与 `order.hall_price` 相等 ⇒ **no-op**：不刷 `updated_at`、不排下家改价事件。
    ## 落库与配套 - `qs.update(hall_price=price, updated_at=now)`：`update()` **不触发 `auto_now`**， `updated_at` 必须显式带（漏了时间会停在旧值）； - 再把新价回填到内存实例，拿**同一实例**调既有 `jidanpu_member.queue_price_update` （鸡蛋铺下家副本；它只读 pk / 单号，价由消费端现查 DB）。 ⚠️ 判据以 **DB 行**为准；实例回填是同一事务内后续代码的配套，⛔ 别把它当成落库证明。"""
    from apps.business import jidanpu_member, peer_hall

    if card is None:
        return False
    price = peer_hall.display_price(card)
    if price is None:
        return False
    qs = Order.objects.filter(
        hall_card=card, status=OrderStatus.WAITING_GRAB
    )
    order = qs.first()
    if order is None:
        return False
    if order.hall_price == price:
        return False
    qs.update(hall_price=price, updated_at=now)
    order.hall_price = price
    order.updated_at = now
    jidanpu_member.queue_price_update(order)
    return True


def refund_for_card(card: PeerHallMirror, *, now=None) -> int:
    """卡**离开大厅**（`gone`）⇒ 隐藏单跟着 `REFUNDED`，并让下家大厅撤下它的副本。回「这次真的退掉了几张」（0 / 1）——逐条收口让调用方能把「真的翻了」与「并发已处理」分开（照本线既有的逐行条件更新口径）。 - **只有 `WAITING_GRAB` 会翻**：`REFUNDED` 是幂等（重复摘牌天然无害）；其余状态
    （`IN_PROGRESS` / `COMPLETED` / `PENDING_REVIEW` / `ISSUE` / `PAUSED` …）说明这张单已经转正成我方真单（`hall_card` 已清空，走到这里说明数据被手改）或被人动过 ⇒ ⛔ **不覆盖、也不报错**（返回 0 是「这次没有翻任何一张」的诚实答复；
    硬翻会把一笔正在进行的真单退掉，那是钱的事）。⚠️ 评审点名要写清的就是这一档： **调用方拿到 0 不代表出错**，别把 0 当异常上抛 / 重试。 - **`hall_card` 不清**：退掉的派生单仍属隐藏面（它没有真实客户与退款额，⛔ 不许在列表里冒充一笔真退单）。 - 下家大厅的副本走**既有** `jidanpu_member.queue_withdraw`（「已经推出去的东西的清理」；它会按推送证据找绑定、并排除接手人自己那家店——⛔ 这里不另写一份）。"""
    from apps.business import jidanpu_member

    if card is None:
        return 0
    order = getattr(card, "shadow_order", None)
    if order is None or order.deleted_at is not None:
        return 0
    updated = Order.objects.filter(
        pk=order.pk, status=OrderStatus.WAITING_GRAB, deleted_at__isnull=True
    ).update(status=OrderStatus.REFUNDED, updated_at=now or timezone.now())
    if not updated:
        return 0
    # ⚠️ 这里**不回写内存里的 `order.status`**：`queue_withdraw` 只读单号 / 主键与推送证据，
    #    读状态没有意义 ⇒ 少一处「内存状态与库不一致」的假话（扫描口径也更窄）。
    jidanpu_member.queue_withdraw(order)
    return 1
