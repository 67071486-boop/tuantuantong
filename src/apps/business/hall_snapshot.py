"""接单大厅**离厅快照**的写入点收拢口（D-0296，业务方）。 ## 为什么要有这个模块一张单离开大厅（被接走 / 被撤回 / 被软删）的那一刻，它在大厅里呈现的那一行要原样存成一条 `HallExitSnapshot`（灰卡）。写入点散在**多个**路径上（状态机 / 软删 / 镜像同步 / 救回命令），但**构造载荷与落行的逻辑只有这一份**——⛔ 调用点只管「什么时候、以哪种离厅方式」调这里的函数，不许在调用点自己拼 payload （同一件事两处实现必然漂移，`AGENTS.md` §5-E）。 ## 写入点清单（枚举即契约；漏一个 = 大厅少一张灰卡） | 调用方 | 调什么 | exit_kind | |---|---|---| | `order_status.apply_transition`（成功转换，`from == WAITING_GRAB` 且目标不是它） | `record_native_exit` | `claimed`（目标 = 代练中）/ `withdrawn`（其余一切出边） |
| `recycle.soft_delete_order`（首次删除分支；软删不经状态机） | `record_native_exit` | `withdrawn` | | `jidanpu_upstream.claim_upstream_mirror`（`listed → claiming` 条件更新成功） | `record_mirror_exit` | `claimed` | | `jidanpu_upstream.claim_upstream_mirror`（归属绑定停用 / 缺失的 `listed` 僵尸卡，命中即下架 `listed → gone`；2026-10-29 业务方报障修法） | `record_mirror_exit` | `withdrawn` | | `jidanpu_upstream._mark_gone`（仅当原状态是 `listed`，条件更新保证） | `record_mirror_exit` | `withdrawn` |
| `jidanpu_upstream.promote_upstream_mirror`（`claiming → promoted`） | ⛔ **不写**（离厅那一刻 = claiming 那一次） | | `jidanpu_upstream._claim_refused`（`claiming → gone`） | ⛔ **不写**（原状态不是 `listed`） | | `peer_hall.claim_peer_mirror`（peer 绑定停用 / 软删 / 缺失支，2026-10-29 只对齐文案回 `1012`） | ⛔ **不写**（peer 行生命周期由 `hall.claim_refused` 回包驱动，与鸡蛋铺拉取侧不同；生产零命中） | | `rescue_gone_mirrors` 与 `sync_hall` 行循环（gone 复活回 `listed`，⭐ D-0300 起两处） | `clear_mirror_snapshots`（删，防一单双卡） |"""
import logging

from django.db import DataError, IntegrityError, transaction
from django.utils import timezone

from apps.business.models import HallExitSnapshot, Order, OrderStatus
from apps.core.datetime_display import local_iso

logger = logging.getLogger(__name__)

#: 「在厅视图」的固定值（覆写用）：快照存的是「它还挂在大厅里时长什么样」。 ⚠️ 镜像行的基线键表（`jidanpu_common.REMOTE_ROW_BASE`）里 `taker_name` 的空值是
#: **空串**，原生序列化器给出的空值是 **`None`**——两条路各自按自己的在厅基线覆写， ⛔ 不要在这里「统一」成同一个值（两路行形状对拍的网各自钉着各自的口径）。
IN_HALL_STATUS = str(OrderStatus.WAITING_GRAB)


def native_payload(order) -> dict:
    """原生单 → **离厅那一刻的大厅行载荷**（键集 = `HallListSerializer` 输出）。 ⚠️ **函数级 import**（照 `order_status.py` 现有先例）：`apps.business.api.serializers` 顶层拉着状态机 / 结算一串 business 模块，而本模块被 `order_status`（全仓最热的写入口）调用 ⇒ 顶层 import 会在导入图上多一条易碎的环。
    ⚠️ **单行 re-annotate**：大厅行的大厅价键取的是注解 `effective_taker_price` （`selectors.search_hall` 注的），这里按同一表达式给**这一行**补上，序列化器才读得到它（漏了它 `hall_price` 会序列化成 `None`）。 ## 三处覆写——为什么（⛔ 逐条看，别当成装饰）序列化发生在**转换之后**：`order` 实例此刻已经是转换后的状态（被接走的单
    `status=IN_PROGRESS`、MAIN 行已挂接单人）。但快照存的是「在厅视图」，所以： - `status` 覆写成 `WAITING_GRAB`——大厅行永远挂着「待抢单」； - `taker_name` 覆写成 `None`——在厅的单没有接单人（接管发生后序列化器会从 MAIN 行读出人名，那不是在厅视图）； - `hall_price` 覆写成 `order.hall_price`（**原始抛单价**）——接单之后注解会读到接管绩效行上的覆盖价，那同样不是在厅视图；原生单在大厅的展示价就是抛单价。"""
    from apps.business.api.serializers import HallListSerializer
    from apps.business.selectors import EFFECTIVE_TAKER_PRICE, effective_taker_price_expression

    # ⚠️ 走 **`all_objects`**：软删那个写点是在「刚把 `deleted_at` 写下去」之后调的，
    #    默认 manager 会把这一行滤掉（D-0144）⇒ 用默认 manager 快照会静默丢一张灰卡。
    annotated = (
        Order.all_objects.filter(pk=order.pk)
        .select_related("game", "server", "category", "project", "hall_publisher")
        .annotate(**{EFFECTIVE_TAKER_PRICE: effective_taker_price_expression()})
        .first()
    )
    if annotated is None:
        # 取不到行（理论上不可达：写点都长在「这行刚被改过」的事务里）⇒ 响亮失败，
        # ⛔ 不静默降级成空 payload——那张灰卡会是一行空壳。
        raise LookupError(f"离厅快照取不到订单行：pk={order.pk}")
    data = HallListSerializer(annotated).data
    data["status"] = IN_HALL_STATUS
    data["taker_name"] = None
    # ⚠️ 覆写值要过 JSON 序列化：`hall_price` 按镜像行同款格式化成两位小数字符串
    #    （`Decimal` 进 JSONField 会当场 TypeError——事务里抛了会被调用方的
    #    「响亮留痕」接住，灰卡丢一张）。`None` 原样（没抛价的在厅单是合法状态）。
    data["hall_price"] = (
        None if order.hall_price is None else f"{order.hall_price:.2f}"
    )
    return data


def mirror_payload(mirror) -> dict:
    """上家镜像行 → 离厅那一刻的大厅行载荷（键集 = `serialize_upstream_row` 输出）。 ⚠️ **函数级 import**：`jidanpu_upstream` 顶层拉着 claim / member 一串模块，
    本模块又被它调用（写点在那边）⇒ 与 `native_payload` 同一条防环纪律。 ## 覆写——为什么 `serialize_upstream_row` 是「在厅投影」，它对 `status` / `taker_name` /
    `hall_price` 的取值本来就与镜像行的接单状态无关（镜像行不是订单，没有接管人）；但**显式覆写一遍**把「快照 = 在厅视图」这条契约写进代码——投影将来若改成会反映接单状态，快照这边不会跟着变。"""
    from apps.business import jidanpu_upstream

    data = jidanpu_upstream.serialize_upstream_row(mirror)
    data["status"] = IN_HALL_STATUS
    data["taker_name"] = ""
    data["hall_price"] = jidanpu_upstream._money_text(
        jidanpu_upstream.upstream_mirror_hall_price(mirror)
    )
    # ⚠️ payload 要过 JSONField（无自定义 encoder ⇒ datetime 不 serializable）：时刻键统一 ISO 字符串（与 `HallListSerializer` 输出的同一形状）。
    # ⚠️ D-0302：走 `local_iso`（项目时区 +08:00，与 `HallListSerializer` 的 DRF `DateTimeField` 同一条出口语义）——旧写法裸 `.isoformat()` 出 UTC 串，
    # 灰卡行透传给大厅时与原生行的 `+08:00` 混屏、差 8 小时。 ⚠️ **存量混合窗口**：修复前写入的快照行里已是 UTC 串，读侧（`snapshot_row`）原样透出、不做转换 ⇒ 新旧行都不炸；旧行随 7 天保留期自愈。
    moment = data.get("created_at")
    if hasattr(moment, "isoformat"):
        data["created_at"] = local_iso(moment)
    return data


def record_native_exit(order, *, exit_kind: str, at=None) -> HallExitSnapshot:
    """原生单离厅 → 一条快照。**调用方先判「这单是不是在大厅」**（本函数不判状态）。"""
    return HallExitSnapshot.objects.create(
        order=order, mirror=None, source=HallExitSnapshot.Source.NATIVE,
        exit_kind=exit_kind, game=order.game, payload=native_payload(order),
        exited_at=at or timezone.now(),
    )


def record_mirror_exit(mirror, *, exit_kind: str, at=None) -> HallExitSnapshot:
    """上家镜像离厅 → 一条快照。**调用方先用条件更新判「原状态是 listed」**。
    ⚠️ 镜像行的 `game` 在 `listed` 行上恒非空（游戏对不上的行直接落 `skipped`、
    不进大厅）⇒ 这里不兜空 `game`：兜了会把「listed ⇒ 有游戏」这条不变式藏进防御分支。"""
    snapshot = _mirror_exit_snapshot(mirror, exit_kind=exit_kind, at=at or timezone.now())
    snapshot.save(force_insert=True)
    return snapshot


def _mirror_exit_snapshot(mirror, *, exit_kind, at):
    return HallExitSnapshot(
        mirror=mirror, source=HallExitSnapshot.Source.UPSTREAM,
        exit_kind=exit_kind, game_id=mirror.game_id, payload=mirror_payload(mirror),
        exited_at=at,
    )


def record_mirror_exits(mirrors, *, exit_kind, at) -> None:
    """批量留灰卡。个别快照失败时仍可下架并保留其余快照。"""
    snapshots = []
    for mirror in mirrors:
        try:
            if mirror.game_id is None:
                raise ValueError("离厅镜像卡缺少游戏")
            snapshots.append(_mirror_exit_snapshot(mirror, exit_kind=exit_kind, at=at))
        except Exception:  # noqa: BLE001
            logger.exception("批量下架的灰卡快照构造失败 mirror=%s", mirror.pk)
    try:
        with transaction.atomic():
            HallExitSnapshot.objects.bulk_create(snapshots, batch_size=250)
    except (DataError, IntegrityError):
        # 整批已回滚，逐张隔离失败的快照。
        for snapshot in snapshots:
            try:
                with transaction.atomic():
                    snapshot.save(force_insert=True)
            except (DataError, IntegrityError):
                logger.exception("批量下架的灰卡快照写入失败 mirror=%s", snapshot.mirror_id)
            except Exception:  # noqa: BLE001
                logger.exception("批量下架的灰卡快照补写中止 mirror=%s", snapshot.mirror_id)
                break
    except Exception:  # noqa: BLE001
        logger.exception("批量下架时无法写入 %d 张灰卡快照", len(snapshots))


def clear_mirror_snapshots(mirror) -> int:
    """救回一张镜像 ⇒ 删掉它的全部快照（显式 filter 删；回删除行数）。为什么是删而不是留：镜像**复活回大厅**了，旧灰卡还在的话，同一张单会
    「可接区一张 + 灰区一张」同时出现（一单双卡）。 ⭐ **D-0300 口径（`gone → listed` 不只发生在 `rescue_gone_mirrors`
    那条人工救回路上，别处不复用本函数」已不成立）**：`sync_hall` 行循环的**自动复活** （`gone` 重新见到即复活）也调本函数——两个写点共用同一份清理逻辑，⛔ 不在循环里自拼第二份删除。"""
    deleted, _ = HallExitSnapshot.objects.filter(mirror=mirror).delete()
    if deleted:
        logger.info("镜像救回，已清快照 mirror=%s deleted=%s", mirror.pk, deleted)
    return deleted
