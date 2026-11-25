"""接单大厅**模拟单**的生成与回收（D-0296，业务方）。 ## 它给大厅什么每租户每轮（默认 2 小时一拍，由 `jidanpu_sync` 的进程内锚控制）： ① **回收**（**恒跑，不受开关控制**——评审 M-1）——在厅超 2 小时的卡下架（进灰区）、灰卡超 7 天的删除（业务方口径：撤下后**像正常灰卡一样** 留在大厅「已被抢」区，保留期与快照同一条 7 天线，⛔ 不再 2 小时即删）、
快照超 7 天的删除； ② **补充**（受租户开关 `hall_simulated_orders` 控制，默认关）——对每个有历史订单的游戏，若在厅卡少于目标（每游戏每轮随机 2~3 取一次），从该游戏的历史订单里**随机抽源**复刻出新的模拟卡。 ## ⛔ 边界（漏看一条就是资金面事故） - **不建 `Order` 行**：模拟单只有 `HallSimulatedOrder` 一行 + 一个 `payload`；
⛔ 不进状态机 / 结算 / 对账，任何统计 / 资金口径都不读它； - **零 PII**：`payload` 里的客户联系方式（`customer_phone` / `customer_ww` 等）一律空串 ——复刻的只有项目形态（project_text / 区服 / 加急 / 时间要求），⛔ 不带源单的客户面； - **租户边界**：本模块只在**已建立的租户上下文**里被调（`jidanpu_sync._run_tenant`）， ⛔ 不接受任何租户标识参数（`AGENTS.md` §4）。"""
import logging
import random
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models.expressions import RawSQL
from django.utils import timezone

from apps.business.deposit_gate import is_app_request
from apps.business.jidanpu_common import REMOTE_ROW_BASE
from apps.business.models import Game, HallExitSnapshot, HallSimulatedOrder, Order, OrderStatus
from apps.business.order_no import generate_order_no
from apps.business.system_switches import switch_enabled
from apps.core.datetime_display import local_iso

logger = logging.getLogger(__name__)

#: 租户开关（`SystemSwitch`，默认 **N**——生成是显式动作，不是默认行为）。
SWITCH_KEY = "hall_simulated_orders"

#: 一轮的节奏（秒）：`jidanpu_sync` 的进程内锚按它判到点。
ROUND_PERIOD_SECONDS = 7200

#: 在厅窗口：active 满 2 小时下架（active → retired，进「已被抢」灰区）。
#: ⚠️ 业务方口径：2 小时只是**可接时长**；下架后的灰卡与正常灰卡一样
#:    保留 `SNAPSHOT_KEEP_DAYS`（7 天）才删，⛔ 不再「再挂 2 小时即删」。
GRAY_KEEP_SECONDS = 7200

#: 离厅快照的保留期（天）：超期删除（回收时**先 count 打日志再按显式 WHERE 删**）。
SNAPSHOT_KEEP_DAYS = 7

#: 模拟卡价格下限（源价 0 / `None` 时也按它兜底——D-0015：0 是合法金额，这里下限
#: 是业务口径「免费卡不打进大厅」，与 `LAUNCH_NO_PRICE_REASON` 同族的方向）。
MIN_PRICE = Decimal("10")

#: 加价区间（源价 × uniform(1.3, 1.5)，两位小数、四舍五入）。
PRICE_FACTOR_MIN = Decimal("1.3")
PRICE_FACTOR_MAX = Decimal("1.5")

#: 每游戏每轮的目标张数（含端点；**每游戏每轮随机取一次**，不是每张一抽）。
TARGET_MIN = 2
TARGET_MAX = 3

#: 源单的复用冷却：近 4 小时被抽过的源单不再抽（含已下架的卡）。
SOURCE_COOLDOWN = timedelta(hours=4)

#: 仿真单号的尝试次数： exhausting 后放弃这一张（日志响亮，⛔ 不死循环）。
_ORDER_NO_ATTEMPTS = 5


def _money_text(value: Decimal) -> str:
    """金额 → 两位小数字符串（与镜像行 `hall_price` 键同形）。"""
    return f"{Decimal(value):.2f}"


def price_for_source(source_hall_price) -> Decimal:
    """源价 → 模拟卡展示价：`max(下限, 源价 × uniform(1.3, 1.5))`，两位小数。 ⚠️ 源价 `None` **按 0 算**（业务方口径，与 `upstream_hall_price` 的缺价同方向）
    ⇒ 结果就是下限 `10`。⛔ 判据 `is not None`（真值写法会把合法的 `0` 源价也吞成
    `None` 档——虽然两档结果相同，但那是巧合不是语义）。"""
    base = Decimal("0") if source_hall_price is None else Decimal(source_hall_price)
    factor = Decimal(random.uniform(
        float(PRICE_FACTOR_MIN), float(PRICE_FACTOR_MAX)
    )).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    raw = (base * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(MIN_PRICE, raw)


def _unique_display_order_no():
    """20 位仿真号（形状照 `Order.order_no` 的生成规则）：重试保唯一。 ⚠️ **不得存在于 `Order.order_no`**：唯一性判据查 `all_objects`——软删单仍占号
    （列上就是全表唯一约束），查默认 manager 会把「撞了已删单」漏放成假唯一。取号复用 `generate_order_no()`（同一租户序列 ⇒ 与将来真实订单天然错开）。
    尝试用尽 ⇒ `None`（调用方放弃这一张，日志响亮）。"""
    for _ in range(_ORDER_NO_ATTEMPTS):
        candidate = generate_order_no()
        if HallSimulatedOrder.objects.filter(display_order_no=candidate).exists():
            continue
        if Order.all_objects.filter(order_no=candidate).exists():
            continue
        return candidate
    return None


def _payload_for(instance) -> dict:
    """模拟卡的大厅行载荷：键集与 `HallListSerializer` 输出**恰好同形**。基底复用 `REMOTE_ROW_BASE`（镜像行的基线键表，⛔ 不抄一份字面量——键表漂移
    会让「一张表渲染多源行」的那条对拍网红）。⚠️ 客户隐私键（`customer_phone` / `customer_ww` / `game_role_name`）保持基线的**空串**，⛔ 不从源单复制。"""
    data = dict(REMOTE_ROW_BASE)
    data.update(
        {
            "order_no": instance.display_order_no,
            "status": str(OrderStatus.WAITING_GRAB),
            "game": instance.game_id,
            "server": instance.server_id,
            "project_text": instance.project_text,
            "is_urgent": instance.is_urgent,
            "time_requirement": instance.time_requirement,
            "remark": instance.remark,
            "hall_price": _money_text(instance.price),
            # ⚠️ JSONField（无自定义 encoder）吃不下 datetime ⇒ 时刻键 ISO 字符串（与 `HallListSerializer` 输出的同一形状）。
            # ⚠️ D-0302：走 `local_iso`（项目时区 +08:00）。⚠️ 本 payload 是 **生成时固化**进 JSONField 的（读侧 `simulated_row` 原样透出、不转换）⇒
            # 修复前写入的行里已是 UTC 串，读侧照常渲染（新旧两形状都不炸），旧行随在厅 2 小时 + 灰区 7 天的保留期自愈。
            "created_at": local_iso(instance.generated_at),
            # 三路合并键：模拟单自己的 origin；remote_* 全空（它不是任何远端的镜像）
            # ——键表与 `serialize_upstream_row` 的那份**恰好同形**（对拍网钉着）。
            "origin": "simulated",
            "source_system": "",
            "remote_order_id": "",
            "remote_order_view": "",
            "remote_game_area": "",
            "remote_game_server": "",
            "remote_hours_desc": "",
            "sim_pk": instance.pk,
            # 模拟卡在大厅恒优选（业务方口径：演示卡永远亮着那颗标）。
            "is_preferred": True,
            "is_exited": False,
            "exit_kind": None,
        }
    )
    return data


def _create_from_source(source, *, game_id: int, now) -> HallSimulatedOrder | None:
    """从一张历史单复刻一张模拟卡；取号失败 ⇒ `None`（调用方跳过）。"""
    display_no = _unique_display_order_no()
    if display_no is None:
        logger.warning("模拟单取号失败（尝试 %s 次），本轮跳过一张", _ORDER_NO_ATTEMPTS)
        return None
    return HallSimulatedOrder.objects.create(
        game_id=game_id,
        display_order_no=display_no,
        server=source.server,
        project_text=source.project_text or "",
        price=price_for_source(source.hall_price),
        is_urgent=source.is_urgent,
        time_requirement=source.time_requirement or "",
        remark="",
        source_order=source,
        status=HallSimulatedOrder.Status.ACTIVE,
        generated_at=now,
        payload={},
    )


def _recycle(now) -> dict:
    """① 回收：超时下架 / 删卡 / 删快照。**先 count 打日志再按显式 WHERE 删**。 ⚠️ 下架那条 UPDATE 刻意**从模型名直链**（不经过中间变量）：写点枚举网
    （`test_openapi_event_sites_net`）按「queryset 根名 = 模型名」对拍——
    「先 count 再写」的判据不受影响（count 仍在前，WHERE 逐字相同）。"""
    stale_active = HallSimulatedOrder.objects.filter(
        status=HallSimulatedOrder.Status.ACTIVE,
        generated_at__lt=now - timedelta(seconds=GRAY_KEEP_SECONDS),
    )
    retired_n = stale_active.count()
    if retired_n:
        logger.info("模拟单回收：在厅超时下架 %s 张", retired_n)
        HallSimulatedOrder.objects.filter(
            status=HallSimulatedOrder.Status.ACTIVE,
            generated_at__lt=now - timedelta(seconds=GRAY_KEEP_SECONDS),
        ).update(
            status=HallSimulatedOrder.Status.RETIRED, retired_at=now
        )

    stale_retired = HallSimulatedOrder.objects.filter(
        status=HallSimulatedOrder.Status.RETIRED,
        retired_at__lt=now - timedelta(days=SNAPSHOT_KEEP_DAYS),
    )
    deleted_n = stale_retired.count()
    if deleted_n:
        logger.info("模拟单回收：灰卡超 %s 天删除 %s 张", SNAPSHOT_KEEP_DAYS, deleted_n)
        stale_retired.delete()

    stale_snapshots = HallExitSnapshot.objects.filter(
        exited_at__lt=now - timedelta(days=SNAPSHOT_KEEP_DAYS)
    )
    snapshot_n = stale_snapshots.count()
    if snapshot_n:
        logger.info("大厅快照回收：超 %s 天删除 %s 条", SNAPSHOT_KEEP_DAYS, snapshot_n)
        stale_snapshots.delete()
    return {"retired": retired_n, "deleted": deleted_n, "snapshots": snapshot_n}


def _top_up(now) -> int:
    """② 补充：有历史订单（未软删）的游戏，在厅卡不足目标就补差额。"""
    created = 0
    game_ids = list(
        Order.objects.values_list("game_id", flat=True).distinct().order_by("game_id")
    )
    for game_id in game_ids:
        if not Game.objects.filter(pk=game_id).exists():
            # 历史单的游戏行被硬删（PROTECT 之下不应发生）⇒ 跳过这只游戏，⛔ 不炸整轮。
            continue
        active_n = HallSimulatedOrder.objects.filter(
            game_id=game_id, status=HallSimulatedOrder.Status.ACTIVE
        ).count()
        target = random.randint(TARGET_MIN, TARGET_MAX)  # 每游戏每轮随机取一次
        missing = target - active_n
        if missing <= 0:
            continue
        # 排除集：当前在厅卡的源单 + 近 4 小时被抽过的源单（含已下架的卡——
        # 「用过」与「还挂着」是两件事，两个条件都要）。
        excluded = set(
            HallSimulatedOrder.objects.filter(
                game_id=game_id,
                status=HallSimulatedOrder.Status.ACTIVE,
                source_order_id__isnull=False,
            ).values_list("source_order_id", flat=True)
        )
        excluded |= set(
            HallSimulatedOrder.objects.filter(
                game_id=game_id,
                source_order_id__isnull=False,
                generated_at__gte=now - SOURCE_COOLDOWN,
            ).values_list("source_order_id", flat=True)
        )
        # SQL 端随机取样（ORDER BY random() + LIMIT）：候选不再全量拉 pk 入内存
        # （订单表大了以后每游戏每轮一次全表扫是本段的瓶颈）。
        candidates = list(
            Order.objects.filter(game_id=game_id)
            .exclude(pk__in=excluded)
            .order_by(RawSQL("random()", []))
            .values_list("pk", flat=True)[:missing]
        )
        if len(candidates) < missing:
            logger.info(
                "模拟单补充：游戏 %s 的可用源单用尽（目标 %s / 在厅 %s）",
                game_id, target, active_n,
            )
        for source_pk in candidates:
            source = Order.objects.filter(pk=source_pk).first()
            if source is None:
                continue
            instance = _create_from_source(source, game_id=game_id, now=now)
            if instance is None:
                continue
            instance.payload = _payload_for(instance)
            instance.save(update_fields=["payload"])
            created += 1
    return created


def run_tenant_round(*, now=None) -> dict:
    """一个租户里的一轮（**必须已在租户上下文里**）。 ⚠️ **回收段不受开关控制（恒跑）**：快照 7 天清理是离厅快照（原生 / 镜像 / 模拟
    三路共用一张表）的**唯一**回收点，而模拟单开关默认全关——若被开关短路，不开模拟单的租户快照**只增不删**、7 天保留承诺对它们不成立；开关关掉后存量 active
    模拟单也会永远留在厅里。开关**只包「生成新单」段** ——它关的是「制造新模拟卡」，不是「清理」。"""
    now = now or timezone.now()
    result = _recycle(now)
    if switch_enabled(SWITCH_KEY):
        result["created"] = _top_up(now)
    else:
        result["created"] = 0
        result["switched_off"] = True
    return result


def viewer_sees_simulated(request) -> bool:
    """模拟单对**这个查看者**可不可见（2026-10-24 业务方裁定；⭐ 2026-10-29 补第三道）。模拟单**读侧**（`order_views.OrderViewSet` 的大厅流）与**写侧**（假接手端点 `hall_sim_views.HallSimClaimView`）共用的唯一判据——⛔ 两面不许各抄一份（X-01，第五轮审计：写侧漏闸 ⇒ 看不见演示卡的人可以把卡下架）。
    ① **只在移动端**（2026-10-24 晚间追加）：模拟单只给打手 App / 小程序（AppToken 令牌请求）看——PC / H5 网页会话一概不给（业务方实测 PC 内部抢单大厅不该出现演示卡）。判据复用 `is_app_request`（deposit_gate 的唯一出处，⛔ 不另写 isinstance）。 ② **没开个人免押**：开了个人免押（`StaffProfile.deposit_quota_enabled`）
    的老打手不再看它们。没档案 / 开关没开 ⇒ 可见。 ③ **当前没有任何冻结押金**（2026-10-29 业务方确认补上；D-0296 当时漏登记）：钱包里还冻着押金（`TakerWallet.frozen > 0`）的打手已经过了新人期，不再看演示卡。判据读 `TakerWallet.frozen`（押金是**账号级**冻结的唯一去处， `apps/accounts/wallet.py` 的口径；没有钱包行 = 冻结 0 ⇒ 可见，所以本腿只**收窄**「交了押金但没开免押」的人群，两旧条件覆盖的新人照旧可见）。三道**全满足**才可见；在厅段与灰区的 retired 段**同一道判据**（读侧调用方传同一个布尔）。"""
    if not is_app_request(request):
        return False
    from apps.accounts.models import StaffProfile

    enabled = (
        StaffProfile.objects.filter(user=request.user)
        .values_list("deposit_quota_enabled", flat=True)
        .first()
    )
    if enabled is True:
        return False
    from apps.accounts.models import TakerWallet

    frozen = (
        TakerWallet.objects.filter(user=request.user)
        .values_list("frozen", flat=True)
        .first()
    )
    # 没有钱包行（`frozen is None`）⇒ 冻结 0 ⇒ 可见；有行按金额判（`<= 0`
    # 与押金闸「`frozen >= need` 才够」同一方向：非正数都当「没有冻着的钱」，
    # `Decimal(0)` 的合法边界按 D-0015 显式比较，不走真值）。
    return frozen is None or frozen <= 0
