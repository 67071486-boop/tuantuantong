"""闲管家店铺同步：把「一个应用下的 N 家店」落成「N 个订单来源」（spec §3 R5/R6）。业务方 的反馈是整条改动的起点：**一个闲管家应用最多绑 30 家店**，而旧模型是「一个应用 ↔ 一个订单来源」⇒ 30 家店的单挤进同一个来源。这里负责把店铺列表
同步成本地表 + 来源表，做到「**一个来源 = 一家店**」。四条不变量（每条都有测试钉住）： 1. **按 `user_identity` 认店**（唯一键，官方文档说它不变）——所以**店铺改名不新增来源**（R6）； 2. **幂等**：同一份店铺列表同步两次，不会多出店铺行、也不会多出来源；
3. **只创建、不改名**：店铺已有来源时只更新名称快照，**不动来源的名字** （租户在「订单来源管理」里手改过的名字不该被一次同步抹掉）； 4. **同名不共用**：来源名冲突时加 `(2)`/`(3)` 后缀——因为我们按**标识**认店，不按名字，共用一个同名来源会把两家店的归属混在一起。 ⚠️ **来源名只在首次创建时取当时的店铺名称**：之后店名变了，来源名保持不动（R6 要的就是这个）。"""
from __future__ import annotations

import logging
import time

from apps.business.models import OrderSource, ShopType, XgjShop, XgjShopBinding
from apps.business.xgj import XgjError, shop_list

logger = logging.getLogger(__name__)

#: 同名后缀的上限。用完就退回「店名 + 标识前 6 位」——保证一定能建出来，且仍然可辨认。
_MAX_SUFFIX = 50

#: 自动生成的来源打在 `remark` 上的标记。⚠️ 它同时是一条**判据**：
#: 只有带这个标记的来源才会被「订单里没有标识」那条兜底路径复用（见 `source_for_order`），
#: 免得自动判定把**租户手工**建的同名来源占掉。
AUTO_SOURCE_REMARK = "闲管家店铺对接自动生成"


def _clip_source_name(core: str, suffix: str = "") -> str:
    """把来源名的**主体**裁进 `OrderSource.name` 的列宽，再接上 `suffix`。 ⚠️ `suffix`（`(2)` / `#3`）**不参与截断**——它承担的是唯一性，裁掉会让「同名不共用」静默失效。
    ⚠️ 列宽从**模型元数据**取、不抄数字（先例 `apps/business/peer_inbound.py::_clip_to_column`，裁决 R19）。
    ⚠️ **匹配与创建必须用同一把尺子**：`source_for_order` 的「没有标识」那条兜底路径先按名字查重、再建——只裁创建不裁查重的话，第二次读单会「找不到第一次那条」而**每读一次再建一个来源**。"""
    width = OrderSource._meta.get_field("name").max_length
    text = (core or "").strip() or "闲管家店铺"
    if width is None:
        return f"{text}{suffix}"
    return f"{text[: max(width - len(suffix), 1)]}{suffix}"


def unique_source_name(base: str, *, exclude_pk: int | None = None) -> str:
    """给一家店找一个**当前没被占用**的来源名（spec D5）。 `exclude_pk` 是给「已经属于这家店的那个来源」留的口子——它自己占着这个名字不算冲突（虽然本模块**不会**改已有来源的名字，但导出这个参数让判据在别处也能复用）。 ⚠️ **返回值必须塞得进 `OrderSource.name` 那一列**（2026-10-03 修）：`base` 来自**店铺名**
    （`XgjShop.shop_name`，上限 64）或**卖家会员名**（`seller_name`，上限 64），而 `OrderSource.name` 是 `varchar(10)`。不裁的后果是 `DataError: value too long for type character varying(10)` ⇒ 整个「同步店铺」/「读单」请求 **500**。线上实测：某租户的**第二个**
    闲管家应用里有家店名 **14 个字**，点「同步店铺」当场 500，而同一批里前几家店（名字短） **已经落库** ⇒ 留下「一半店有来源、一家没有」的半成品状态；第一个应用的店名全是 4~7 个字，所以这个坑一直没露出来。截断规则与「后缀不裁」的理由见 `_clip_source_name`。"""
    core = (base or "").strip() or "闲管家店铺"
    taken = set(
        OrderSource.objects.exclude(pk=exclude_pk).values_list("name", flat=True)
    ) if exclude_pk else set(OrderSource.objects.values_list("name", flat=True))

    first = _clip_source_name(core)
    if first not in taken:
        return first
    for n in range(2, _MAX_SUFFIX + 2):
        candidate = _clip_source_name(core, f"({n})")
        if candidate not in taken:
            return candidate
    # 极端情况：同名多于 50 家 ⇒ 退化成带标识后缀（仍然唯一、仍然可读）
    suffix = 1
    while True:
        candidate = _clip_source_name(core, f"#{suffix}")
        if candidate not in taken:
            return candidate
        suffix += 1


def _create_source(shop_name: str) -> OrderSource:
    """给一家店建一个普通来源（**不加锁定标记**：业务方要求它能改能删，spec D9）。"""
    return OrderSource.objects.create(
        name=unique_source_name(shop_name),
        shop_type=ShopType.XIANYU,
        remark=AUTO_SOURCE_REMARK,
    )


def ensure_source(shop: XgjShop, *, fallback_name: str = "") -> OrderSource:
    """保证这家店有一个来源；没有就**当场建一个**并回填（spec R4/R7/D8）。 `fallback_name` 用在「店铺列表里查不到这家店」的兜底路径上——那时只有订单里的
    **卖家会员名**可用，所以来源名会是会员名而不是店名（spec D8 如实登记的代价）。 ⚠️ 判据是 `shop.order_source_id is None`（**不**用真值判断对象：来源被删后 FK 会置空，
    这里要做的是"补一个"，而不是"改一个"）。"""
    if shop.order_source_id is not None:
        return shop.order_source  # type: ignore[return-value]
    name = shop.shop_name or fallback_name or shop.user_name or "闲管家店铺"
    source = _create_source(name)
    shop.order_source = source
    shop.save(update_fields=["order_source", "updated_at"])
    return source


def source_for_order(binding: XgjShopBinding, detail: dict) -> OrderSource:
    """按**订单里的店铺标识**找到（或补建）这家店的来源（spec R7 / D8）。自动判定命中某个应用之后，订单里能用来认店的只有两个字段： `seller_eid`（卖家标识，与店铺列表的 `user_identity` 同源）与 `seller_name`（会员名）。
    步骤： 1. 先按标识在本地表里找；**找不到就按需同步一次**这家应用（R7：绑定之后新开的店）； 2. 同步后仍找不到 ⇒ 用订单里的 `seller_name` 兜底建来源（D8——那家店可能已不在授权列表里），并**把这条路径记进日志**。 ⚠️ 兜底时若订单带标识，会补一行 `XgjShop`（店名快照留空，等下次同步补上）：
    不补的话，同一家店的下一张单会**再建一个来源**，「一个来源 = 一家店」当场失效。 3. 订单里连标识都没有 ⇒ 认不出店，只能按会员名建一个来源（同样记日志）。 ⚠️ 按需同步失败（`XgjError`）**不吞掉整单**：订单已经读到了，同步只是"能不能顺便认店"；失败就按兜底路径继续，并只把**上游码**记进日志（不记上游原文——它可能回显凭据， spec §9-S1；本模块不是密钥脱敏器所在的那一层）。"""
    identity = str(detail.get("seller_eid") or "").strip()
    seller_name = str(detail.get("seller_name") or "").strip()
    shop = XgjShop.objects.filter(user_identity=identity).first() if identity else None
    if shop is None and identity:
        try:
            sync_shops(binding)
        except XgjError as exc:
            # 按需同步失败**不吞掉整单**：订单已经读到了，同步只是"能不能顺便认店"。
            logger.warning(
                "闲管家按需同步失败（继续按订单里的会员名兜底）binding=%s upstream_code=%s",
                binding.pk, exc.upstream_code,
            )
        shop = XgjShop.objects.filter(user_identity=identity).first()
    if shop is None and identity:
        # 上游授权列表里没有这家店（可能已解约 / 已下线）⇒ 用订单里的会员名兜底建来源，
        # 并**补一行 `XgjShop`**：不补的话同一家店的下一张单会**再建一个来源**，
        # 「一个来源 = 一家店」当场失效。
        logger.warning(
            "闲管家自动判定：店铺不在授权列表里，用订单里的会员名兜底建来源 binding=%s identity=%s",
            binding.pk, identity,
        )
        shop, _ = XgjShop.objects.get_or_create(
            user_identity=identity,
            defaults={"binding": binding, "shop_name": "", "user_name": seller_name},
        )
    if shop is not None:
        # 认出了是哪家店（直接找到 / 按需同步后找到 / 兜底补建）⇒ 用它的来源（没有就建）。 ⚠️ **孤儿店行先接回当前应用**（独立评审复核后补，2 行、无迁移）：`binding` 是
        # `SET_NULL` ⇒ 租户删掉对接那一行之后，店行会带着 `binding IS NULL` 留着。不接回的话： ① 「手动选了这个来源」会回 `1015`（`_binding_for_source` 拿不到应用），
        # 而「自动判定」却能读到**同一张单**——同一个来源两种结果； ② 它只能等下一次「同步店铺」且上游列表里**仍有**这家店才会被接回，那份"短暂"可能是**无限期**。
        if shop.binding_id is None:
            shop.binding = binding
            shop.save(update_fields=["binding", "updated_at"])
        return ensure_source(shop, fallback_name=seller_name)
    # 订单里**连标识都没有** ⇒ 认不出店，只能按会员名建来源。 ⚠️ **同名复用**（独立评审的 Important）：这一支若每次都 `_create_source`，
    # 同一张单读两次就会得到「只有会员名」与「只有会员名(2)」两个来源——来源字典无界增长，而录单页还会自动选中新建的那个（正是本线要消灭的"同一家店散在多个来源"）。
    # ⚠️ 代价如实登记：这是**唯一一处按名字当键**的地方（别处一律按 `user_identity`）—— 这里没有标识可用，只能拿名字兜底；两个不同店铺恰好用同一个会员名时会**共用**来源，但我们本来就分不出它们（上游没给标识），共用一个总好过每次再建一个。
    logger.warning(
        "闲管家自动判定：订单里没有店铺标识，只能按卖家会员名兜底建来源 binding=%s", binding.pk
    )
    # ⚠️ **先裁再查**（与 `_create_source` 同一把尺子）：不裁的话，
    # 长会员名这条路上「查重用的名字」与「建出来的名字」不是同一个 ⇒ 第二次读同一张单查不到第一次那条，于是**每读一次再建一个来源**（来源字典无界增长），
    # 而症状只是"来源列表里多了一堆同名项"，不会报错。
    name = _clip_source_name(seller_name or "闲管家店铺")
    # ⚠️ 只复用**我们自己为这条兜底路径建的、而且还没挂到任何店铺上的**来源（独立评审复核后收紧，两条判据缺一不可）： ① `xgj_shops__isnull=True`——排除"已经挂在某家店上"的来源。只按名字查会**劫持**
    # 另一家已知店的来源（"来源已挂在谁身上"是**可判定**的，而那条路与 spec D5 直接冲突：按标识认店、不按名字，共用一个来源会把两家店的归属混在一起）。 ② `remark=AUTO_SOURCE_REMARK`——排除租户**手工**建的同名来源（那是租户的东西，
    # 不该被自动判定占用）。残留（如实登记）：租户若把这条兜底来源改了名或改了备注，下次会**再建一个** ——方向是"宁可多一个，也不劫持别人的"。
    existing = OrderSource.objects.filter(
        name=name, xgj_shops__isnull=True, remark=AUTO_SOURCE_REMARK
    ).first()
    if existing is not None:
        return existing
    return _create_source(name)


def subscription_warning(shop: XgjShop, *, now: int | None = None) -> bool:
    """这家店的**订购状态**看起来是否无效（spec per-source §3-D11 的标黄判据）。 ⚠️ 这**不是版本判定**，页面上的标黄**不等于"不是铂金版"**，不标黄也**不保证**能识别订单（最终以上游 `order/detail` 的返回为准）。上游的店铺字段里**没有版本名**，硬猜就是编。
    判据只有两条，都出自官方文档（`user/authorize/list` 的 schema）： - `is_valid is False`——文档原话「是否有效订购中」明确为否； - `valid_end_time` 有值且 `<= now`——文档原话「订购有效结束时间」（秒级时间戳；
    文档示例里无效订购给 `0`，`0 <= now` 自然成立）。 `None`（还没同步过 / 上游这次没给这个键）**不标黄**：缺数据不等于异常。 ⚠️ 判据用 `is False` 而不是真值：`is_valid` 是**三态**（`True`/`False`/`None`）， `None` 走的是"不标黄"这一支（AGENTS.md §5 / E-0021 的同族）。"""
    if shop.is_valid is False:
        return True
    if shop.valid_end_time is not None:
        return shop.valid_end_time <= (int(time.time()) if now is None else now)
    return False


def _xgj_bool(raw) -> bool | None:
    """上游的布尔标量 → `True`/`False`/`None`（**认不出就回 `None`，不猜**）。官方文档把 `is_valid` / `is_trial` 标成 boolean（doc 的示例把它们写成 `'true'` 这种带引号的形态，
    那是 Apifox 的展示格式）；真实回包我们**只观测过 JSON 布尔**（集成线实测 `is_valid: true/false`）。这里额外容错 `0`/`1` 与 `"true"`/`"false"` 字符串——**不认识的取值一律 `None`**：
    编一个 `False` 会让页面上凭空多出一行「订购无效」的黄条（那是关于钱的错提示）。"""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return None
    if isinstance(raw, int):
        return bool(raw)
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "1"):
            return True
        if low in ("false", "0"):
            return False
    return None


def _xgj_seconds(raw) -> int | None:
    """上游的**秒级时间戳**标量 → `int`；缺省 / 转不成 / 负数 ⇒ `None`（不猜）。官方文档：`valid_end_time` 是 int32 的 Unix 秒（示例 `1689782400`，无效订购时给 `0`）。
    `None` 与 `0` 的差别在消费方那边很重要：`None` = **没数据**（不标黄）， `0` = **上游明确说没有有效订购**（标黄）。"""
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def sync_shops(binding: XgjShopBinding) -> dict:
    """拉一次店铺列表，幂等地同步成本地表 + 来源表（spec R5）。 **契约**：上游失败（`XgjError`）**原样抛出去**，由调用方翻译成业务码——
    本函数**不**吞异常（吞掉会让"同步失败"看起来像"同步成功但一家店都没有"）。返回计数：`{"total", "created_shops", "updated_shops", "created_sources"}`。
    ⚠️ `app_key`/`app_secret` 为空时调用方要先挡（本函数假定凭据完整—— 与 `xgj_views.credentials_of` 的分工一致）。"""
    rows = shop_list(
        app_key=(binding.app_key or "").strip(),
        app_secret=(binding.app_secret or "").strip(),
    )
    created_shops = updated_shops = created_sources = 0
    for row in rows:
        identity = str(row.get("user_identity") or "").strip()
        if not identity:
            # 没有标识就没法认店（改名/同名都判不出来）⇒ **跳过而不是猜**。
            # 上游接口文档把 user_identity 列为必返字段，出现缺失说明契约变了。
            continue
        shop_name = str(row.get("shop_name") or "").strip()
        user_name = str(row.get("user_name") or "").strip()
        is_valid = _xgj_bool(row.get("is_valid"))
        valid_end_time = _xgj_seconds(row.get("valid_end_time"))
        shop = XgjShop.objects.filter(user_identity=identity).first()
        if shop is None:
            shop = XgjShop.objects.create(
                binding=binding, user_identity=identity,
                shop_name=shop_name, user_name=user_name,
                is_valid=is_valid, valid_end_time=valid_end_time,
            )
            created_shops += 1
        else:
            # ⚠️ 只更新**快照**，不碰 `order_source`（那可能是租户手改过的）
            shop.binding = binding
            shop.shop_name = shop_name or shop.shop_name
            shop.user_name = user_name or shop.user_name
            # ⚠️ 订购状态**照实覆盖**（上游这次没给这个键时保留旧值：`None` 是"没数据"，
            #    不是"订购无效"——用 `is not None` 判，不拿真值判，E-0021）。
            if is_valid is not None:
                shop.is_valid = is_valid
            if valid_end_time is not None:
                shop.valid_end_time = valid_end_time
            shop.save(update_fields=[
                "binding", "shop_name", "user_name",
                "is_valid", "valid_end_time", "updated_at",
            ])
            updated_shops += 1
        if shop.order_source_id is None:
            ensure_source(shop)
            created_sources += 1
    return {
        "total": len(rows),
        "created_shops": created_shops,
        "updated_shops": updated_shops,
        "created_sources": created_sources,
    }
