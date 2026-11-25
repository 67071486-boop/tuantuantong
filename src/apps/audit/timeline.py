"""订单时间线聚合（D-0360；纯读面、零迁移、零写行为）：排查一单「为什么没同步 / 中间发生了什么」时，把散在各表的留痕按单号拼成带人话短名的时间线。聚合口径与查询形状的权威出处是实施计划 `docs/plans/2026-11-01-order-audit.md` §1.2 / §1.6（本文件只按它实现，改口径先改计划）。
覆盖面四个盲区如实声明（⛔ 不是「全量」）：① binding 维度行（`target_type="binding"`）没有单号可指认，由 `bindings_health` 健康条呈现；② 上家镜像引用行（`target_type="hall"`）单号只在 `payload` JSON 里，一期不查（镜像行本身已由镜像节呈现）；③ 对端的卡挂在我方大厅、还没被领成我方订单——我方库里没有这张 `Order`，时间线入口不存在（镜像行呈现）；④ 读不出对端单号的 callback 失败行（sha / 计次 / `:unsigned` / `:unreadable` 等复合键形）不带可指认的远端单号。另：查码拉取只在「查到」时有行（D-0361 的首查 / 放弃两个台账 kind 已停写——每分钟全量轮询下是纯噪声，排障走 journald 与逐轮摘要），qr_fetch 节无行时保留如实提示语。
查询形状钉死：归一 `Q(order_no=key) | Q(source_order_no=key)` 走 `Order.all_objects`（读点登记在 `test_order_read_path_net.ALL_OBJECTS_SITES`）；`OperationLog` source 臂与 `XgjReceiptRecord` 收款节是仅有的两个非索引点查（仅当来源号非空才发 / 量级=收款表体量），⛔ 不为此加迁移；`JidanpuSyncEvent` callback 臂第二段是**对端单号**（经链路表 `jidanpu_*_order_link` 匹配，写形实证见计划 §1.2），⛔ 不做裸 `target_ref=` 查（全表扫）；`JIDANPU_KIND_LABELS` / `PEER_KIND_LABELS` 由 `tests/test_sync_event_kind_registry.py` 恰好集合网钉住，未登记 kind 运行时 fail-open 显示原始 kind（不炸、网会红）。单请求 ≈ 12 个查询。
"""
from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from apps.audit.display import action_label, summary_for_display
from apps.audit.models import OperationLog
from apps.business.jidanpu_member import readback_breaker_health
from apps.business.models import (
    DailiantongPush,
    EmpType,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    PeerHallMirror,
    PeerOrderLink,
    PeerSyncEvent,
    XgjReceiptRecord,
)
# 派单方广播臂枚举下游绑定用（跨模块引私有名是设计点名的做法——「生效 + 开了大厅推送」
# 两道闸的唯一出处就在那里，⛔ 不抄第二份挑选逻辑）。
from apps.business.peer_dispatch import _hall_targets
# 查码节的「查到行」过滤常量（写面与读面共用，⛔ 不许这里抄第二句人话）。
from apps.business.order_qr_fetch import LEDGER_ACTION, LEDGER_OK_REASON
# 收款节的「匹配结果人话」唯一出处（`xgj_receipt_stats` 口径 5，⛔ 前端/这里都别再拼一份）。
from apps.business.xgj_receipt_stats import match_summary_text

#: 节的固定名与人话标题（§1.6：节名固定、空节照出）。
SECTION_LABELS: dict[str, str] = {
    "operation_log": "操作留痕",
    "jidanpu_sync": "鸡蛋铺同步台账",
    "peer_sync": "上下游同步台账",
    "dailiantong": "代练通链路",
    "mirror": "镜像与链路行",
    "qr_fetch": "查码拉取",
    "receipt": "收款记录",
}
#: 节名固定顺序（响应里的输出顺序；前端按它竖排）。
SECTION_ORDER: tuple[str, ...] = (
    "operation_log", "jidanpu_sync", "peer_sync", "dailiantong", "mirror", "qr_fetch",
    "receipt",
)
#: 每节上限（§1.6：防 E-0304 病态单无界输出；operation_log 200、其余 100）。
OPERATION_LOG_CAP = 200
DEFAULT_SECTION_CAP = 100

#: 订单 8 态的人话（`OrderStatus.choices` 的镜像；取自模型 choices 本身，不另写一份）。
_ORDER_STATUS_LABELS = dict(OrderStatus.choices)


@dataclass(frozen=True)
class TimelineItem:
    """一节里的一条事件（§1.6 统一形状 + 原始字段随行）。"""

    at: str | None
    kind: str
    kind_label: str
    status: str
    summary: str
    raw: dict

    def as_payload(self) -> dict:
        return {
            "at": self.at,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "status": self.status,
            "summary": self.summary,
            **self.raw,
        }


# ── kind 人话登记表（被 `test_sync_event_kind_registry` 恰好集合钉住）────────────
# 取值口径：一句话说清「这件事是什么」，写不出处就写事实本身；⛔ 不编「听起来合理」
# 的解释。kind 全集的枚举出处见该网文件的 docstring（四族写口 + 常量解引用）。

#: `JidanpuSyncEvent.kind` 的人话短名。fail-open：不在这张表里的 kind 原样显示。
JIDANPU_KIND_LABELS: dict[str, str] = {
    # ── 下家鸡蛋铺线（`jidanpu_member`；出向 outbox 七类 + 台账）──
    "member.launch": "向鸡蛋铺抛单",
    "member.assign": "指派接单人给鸡蛋铺",
    "member.direct_assign": "指派鸡蛋铺下家打手",
    "member.withdraw": "向鸡蛋铺撤单",
    "member.withdraw_summary": "其他下家撤单汇总",
    "member.unlist": "把鸡蛋铺那张单撤出大厅",
    "member.status_out": "推送状态给鸡蛋铺",
    "member.price": "同步发单价给鸡蛋铺",
    "member.project": "同步接手项目给鸡蛋铺",
    "member.source_order_no": "同步来源单号给鸡蛋铺",
    "member.remote_taken": "鸡蛋铺侧已被接手（自动接手）",
    "member.status_in": "从鸡蛋铺回拉状态",
    "member.launch_breaker_open": "抛单读回熔断挂起",
    "member.launch_blocked": "发单被熔断暂缓投递（挂起期挡单）",
    # ⭐ D-0362：「明确查无」终态化两条（`jidanpu_member`）。
    "member.no_order_recreated": "对端明确查无此单，已按拍板自动新建（每单限一次）",
    "member.no_order_handoff": "已自动新建仍查无，转人工处置（停止自动新建与重排）",
    "binding.identity_healed": "下家身份自愈补链",
    # ── 下家鸡蛋铺 ②式回调（`jidanpu_member_openapi` + 共用引擎）──
    "member.openapi_unsigned": "②式回调验签未通过",
    "member.openapi_unreadable": "②式回调报文读不出",
    "member.openapi_unclaimed": "②式回调码无人认领",
    "member.openapi_unknown_order": "②式回调找不到我方链路",
    "member.openapi_other_action": "②式回调不是状态通知",
    "member.openapi_query_failed": "②式回调查权威状态失败",
    "member.openapi_unknown_status": "②式回调状态认不出",
    "member.openapi_foreign_path": "事件文件里有非本线路径的行",
    "member.openapi_stale_skipped": "②式回调乱序跳过",
    "member.openapi_reconcile_cap": "②式兜底对账撞本轮上限",
    "member.openapi_reconcile_stuck": "②式兜底对账跳过坏单",
    "member.openapi_legacy_unknown": "老回调地址收到的非本租户单",
    "member.openapi_healed": "②式回调补上了链路单号",
    "member.openapi_heal_blocked": "②式回调自愈补不上",
    # ── 上家鸡蛋铺官方 API 回调线（`jidanpu_openapi_consume`）──
    "openapi.status_in": "从鸡蛋铺回调回拉状态",
    "openapi.callback_unsigned": "回调验签未通过",
    "openapi.callback_unreadable": "回调报文读不出",
    "openapi.callback_unclaimed_code": "回调码无人认领",
    "openapi.callback_unknown_order": "回调找不到我方链路",
    "openapi.callback_not_tracked": "回调对已停止跟踪的单",
    "openapi.callback_other_action": "回调不是状态通知",
    "openapi.callback_query_failed": "回调查权威状态失败",
    "openapi.callback_foreign_path": "事件文件里有非本线路径的行",
    "openapi.callback_stale_skipped": "回调乱序跳过",
    "openapi.callback_retry": "回调事件反复失败计次",
    "openapi.reconcile_cap": "兜底对账撞本轮上限",
    "openapi.reconcile_stuck": "兜底对账跳过坏单",
    "openapi.unmapped_status": "回调权威状态认不出",
    "openapi.outbox_not_supported": "①式通道不支持该动作",
    # ── 上家鸡蛋铺登录式线（`jidanpu_upstream` / `upstream_hall_shadow`）──
    "upstream.status_in": "从上家回拉状态",
    "upstream.content_in": "从上家回拉客户需求",
    "upstream.pull_pics_in": "从上家拉订单截图",
    "upstream.hall_claim": "在上家大厅接单",
    "upstream.complete": "向上家提交完成",
    "upstream.hall_owner_mismatch": "镜像行归属与绑定不一致",
    "upstream.refund_skipped": "退款不同步上家（无权限）",
    "upstream.delete_skipped": "删除不同步上家（无权限）",
    "upstream.issue_skipped": "问题单不推上家（全局禁用）",
    "upstream.hall_claim_released": "绑定停用，接单退回上家",
    "upstream.hall_loop_intercept": "上家大厅回环拦截",
    "upstream.hall_skipped": "上家大厅卡被跳过",
    "upstream.hall_gone": "上家大厅卡已消失",
    "upstream.hall_game_ambiguous": "上家卡游戏名匹配歧义",
    "upstream.record": "上家大厅卡记入镜像",
    "upstream.record_skipped": "上家大厅卡记入被跳过",
    "upstream.reassign": "上家转正单重新指派",
    "upstream.hall_rescued": "上家消失卡重现复活",
    "upstream.hall_throw_no_price": "上家镜像卡无价不抛下家",
    "upstream.hall_shadow_refunded": "上家隐藏单随卡退款",
    "upstream.hall_manual_refund": "上家镜像卡人工退款优先",
    "upstream.promoted_source_rewrite": "转正单来源号重写",
}

#: `PeerSyncEvent.kind` 的人话短名。fail-open 同上。
PEER_KIND_LABELS: dict[str, str] = {
    "chat.order_roster": "同步订单群成员",
    "chat.order_message": "同步订单群消息",
    "chat.order_album": "同步订单群相册",
    "chat.order_pin": "同步订单群置顶",
    "chat.order_detached": "关闭跨工作室订单群同步",
    # ── 团团通租户之间：订单与截图 ──
    "order.assign": "派单给对方租户",
    "order.direct_assign": "跨工作室直派打手",
    "order.grab": "对方打手接单",
    "order.withdraw": "撤回对方的单",
    "order.update": "订单变更同步",
    "order.screenshot.add": "订单截图共享",
    "order.screenshot.delete": "订单截图删除",
    # ── 大厅镜像（协议事件六类 + 拉取证据）──
    "hall.throw": "大厅抛卡给对方",
    "hall.take": "从对方大厅撤卡",
    "hall.reprice": "大厅卡改价",
    "hall.claim": "对方在卡上接单",
    "hall.claim_ok": "接单应答：成交",
    "hall.claim_refused": "接单应答：拒绝",
    "hall.pull": "对方拉取大厅卡（证据）",
    # ── 本仓自己的决策留痕 ──
    "hall.claim_recycle": "卡死的接单按证据收口",
    "hall.claim_release": "绑定停用，接单退回大厅",
    "peer.hall_hop_limit": "大厅转发超跳数上限丢弃",
}

#: 台账方向的人话（两张表同款取值 `out` / `in`）。
_DIRECTION_LABELS = {
    "out": "我方 → 对方",
    "in": "对方 → 我方",
}


def _iso(value):
    """时刻 → 本时区 ISO 串；`None` 原样回（前端显示「—」）。"""
    if value is None:
        return None
    return timezone.localtime(value).isoformat()


def _jidanpu_kind_label(kind: str) -> str:
    return JIDANPU_KIND_LABELS.get(kind, kind)


def _peer_kind_label(kind: str) -> str:
    return PEER_KIND_LABELS.get(kind, kind)


def _sync_summary(reason: str, fallback: str) -> str:
    """台账行的 summary = 行上自己的原因人话（写点已要求人话）；空则回落 kind 短名。"""
    return reason or fallback


# ── 归一查询（§1.2 钉死的双号规则）────────────────────────────────────────────


def resolve_order_by_key(key: str):
    """`key` → **唯一订单**；查不中回 `None`（视图把它翻成与跨租户同形的业务信封）。规则（§1.2 钉死）：两臂（`order_no` / `source_order_no`）都能命中，**order_no 精确命中优先**（唯一索引 ⇒ 至多一条）；仅 source 臂命中多条时取 `created_at` 最新（查询已按它倒序取第一条，`-id` 做同刻 tie-break 保确定性）。走 `all_objects`（审计语义：退单/撤单的软删单恰是排查常态对象），读点登记在 `tests/test_order_read_path_net.py::ALL_OBJECTS_SITES`。"""
    rows = list(
        Order.all_objects.filter(Q(order_no=key) | Q(source_order_no=key)).order_by(
            "-created_at", "-id"
        )
    )
    if not rows:
        return None
    for row in rows:
        if row.order_no == key:
            return row
    return rows[0]


def list_split_group_members(split_id: str) -> list[str]:
    """拆单组**全部**成员单号（只列号、不展开；含已软删——组账要完整）。

    ⚠️ 与 `resolve_order_by_key` 分作用域各用一次 `all_objects`（读点网按作用域计数）。
    """
    return list(
        Order.all_objects.filter(split_id=split_id)
        .order_by("created_at", "id")
        .values_list("order_no", flat=True)
    )


# ── 各节的行集合（查询形状见模块 docstring，⛔ 不偏离）────────────────────────


def _operation_log_rows(order) -> list:
    """系统订单号优先。仅缺系统单号的旧记录允许以来源号关联。"""
    rows = list(OperationLog.objects.filter(order_no=order.order_no))
    if order.source_order_no:
        rows.extend(
            OperationLog.objects.filter(source_order_no=order.source_order_no, order_no="")
        )
    seen: set[int] = set()
    deduped: list = []
    for row in rows:
        if row.pk in seen:
            continue
        seen.add(row.pk)
        deduped.append(row)
    return deduped


def _jidanpu_rows(order) -> list:
    """`target_type="order"` 精确 + 逐绑定 callback 复合键精确（复合键第二段 = **链路行上的对端单号**：member 臂 `jidanpu_order_id`〔可空，录单后才回填 ⇒ 只取非空行〕、upstream 臂 `upstream_order_id`；写形实证见模块 docstring）。
    ⚠️ member / upstream 两张绑定表的 pk **共用**同一个 `target_ref` 字符串空间（`f"{pk}:{远端号}"` 前缀不区分两张表）——误命中需要「pk 撞号 **且** 远端单号同串」双条件同时成立（对端单号是两边各自系统发的号），如实登记这个边界。
    ⛔ binding 维度行（`target_type="binding"`）不在这里（盲区 1，见模块 docstring）。
    """
    # 链路行 → (绑定 pk, 对端单号)；member 臂只收 `jidanpu_order_id` 非空的行
    # （空串 = 还没录成单，那一段对端系统也还不认识这张单、不可能有 callback 行）。
    member_refs = list(
        JidanpuMemberOrderLink.objects.filter(order=order)
        .exclude(jidanpu_order_id="")
        .values_list("binding_id", "jidanpu_order_id")
    )
    upstream_refs = list(
        JidanpuUpstreamOrderLink.objects.filter(order=order).values_list(
            "binding_id", "upstream_order_id"
        )
    )
    rows = list(
        JidanpuSyncEvent.objects.filter(
            target_type="order", target_ref=order.order_no
        )
    )
    cb_refs = [
        f"{pk}:{remote_id}" for pk, remote_id in member_refs + upstream_refs
    ]
    if cb_refs:
        rows.extend(
            JidanpuSyncEvent.objects.filter(
                target_type="callback",
                target_ref__in=cb_refs,
            )
        )
    return rows


def _peer_rows(order) -> tuple[list, list]:
    """回 `(台账行, 链路行投影)`：两臂合并（按 pk 去重，否则病态双号同串时同一条台账在时间线上出现两次），链路行原样给 `related`。
    ① **收单方 link 臂**：链路行的唯一写点在收单方（`peer_inbound`），先经 link 取对端单号，再按 `(binding_role, binding_id, target_ref=对端单号)` 收窄查台账（⛔ 只按绑定不按对端单号收窄会把整条绑定历史拉来）。
    ② **派单方广播臂**（业务方拿真实单验证出的盲区）：派单方抛给下家的大厅台账没有 link 可依 ⇒ 复用 `_hall_targets()`（「绑定生效 + 开了大厅推送」两道闸，⛔ 不抄第二份挑选逻辑）枚举下游绑定，逐绑定按 `(binding_role="downstream", binding_id, target_ref__in=双号)` 查；双号 = `[source_order_no, order_no]` 去空去重（写形依据 `peer_dispatch.enqueue_hall_event`：`target_ref=effective_peer_ref`——来源单号优先、无来源号=内置号）。绑定数是个位数、每绑定吃 `peer_event_evidence_idx` 前缀，量级无害。
    """
    links = list(PeerOrderLink.objects.filter(order=order).order_by("id"))
    rows: list = []
    for link in links:
        if link.upstream_binding_id:
            role, bid = "upstream", str(link.upstream_binding_id)
        else:
            role, bid = "downstream", str(link.downstream_binding_id)
        rows.extend(
            PeerSyncEvent.objects.filter(
                binding_role=role, binding_id=bid, target_ref=link.peer_order_no
            )
        )
    # ② 派单方广播臂（见 docstring）：双号去空去重后，逐启用大厅推送的下游绑定查。
    refs = list(dict.fromkeys(k for k in (order.source_order_no, order.order_no) if k))
    if refs:
        for binding in _hall_targets():
            rows.extend(
                PeerSyncEvent.objects.filter(
                    binding_role="downstream",
                    binding_id=str(binding.pk),
                    target_ref__in=refs,
                )
            )
    seen: set[int] = set()
    deduped: list = []
    for row in rows:
        if row.pk in seen:
            continue
        seen.add(row.pk)
        deduped.append(row)
    deduped.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    return deduped, links


def _mirror_items(order) -> list[TimelineItem]:
    """镜像行与链路行（FK / OneToOne 直取；查隐藏单带出转正正主、查正主带出镜像行）。"""
    items: list[TimelineItem] = []

    # 上家镜像行：`upstream_hall_card`（本单是隐藏单）与 `jidanpu_upstream_hall_origin`
    # （本单是转正正主）两个方向都要；OneToOne 反向查无会抛，按无行处理。
    try:
        card = order.upstream_hall_card
    except JidanpuUpstreamHallOrder.DoesNotExist:
        card = None
    if card is not None:
        items.append(
            TimelineItem(
                at=_iso(card.first_seen_at),
                kind="mirror.upstream_hall_card",
                kind_label="上家大厅镜像卡（本单由它派生）",
                status=card.status,
                summary=f"上家单号 {card.upstream_order_id}（{card.upstream_base_url}）",
                raw={"mirror_id": card.pk, "upstream_order_id": card.upstream_order_id},
            )
        )
    try:
        origin = order.jidanpu_upstream_hall_origin
    except JidanpuUpstreamHallOrder.DoesNotExist:
        origin = None
    if origin is not None:
        items.append(
            TimelineItem(
                at=_iso(origin.first_seen_at),
                kind="mirror.upstream_hall_origin",
                kind_label="本单由上家镜像卡转正",
                status=origin.status,
                summary=f"来自上家镜像行 {origin.upstream_order_id}",
                raw={"mirror_id": origin.pk, "upstream_order_id": origin.upstream_order_id},
            )
        )
    try:
        peer_card = order.hall_card
    except PeerHallMirror.DoesNotExist:
        peer_card = None
    if peer_card is not None:
        items.append(
            TimelineItem(
                at=_iso(peer_card.created_at),
                kind="mirror.peer_hall_card",
                kind_label="团团通大厅镜像卡（本单由它派生）",
                status=peer_card.status,
                summary=f"上家单号 {peer_card.peer_order_no}（{peer_card.peer_base_url}）",
                raw={"mirror_id": peer_card.pk, "peer_order_no": peer_card.peer_order_no},
            )
        )

    for link in order.jidanpu_member_links.all().order_by("id"):
        items.append(
            TimelineItem(
                # 该表没有 created_at；「首次上大厅」缺席时退「上次推送」，再退 None。
                at=_iso(link.listed_at or link.last_out_at),
                kind="link.jidanpu_member",
                kind_label="鸡蛋铺下家链路行",
                status="linked",
                summary=(
                    f"绑定 {link.binding_id}；远端单 {link.jidanpu_order_id}"
                    + ("；在对方大厅挂着" if link.in_hall else "")
                ),
                raw={
                    "link_id": link.pk,
                    "binding_id": link.binding_id,
                    "jidanpu_order_id": link.jidanpu_order_id,
                    "in_hall": link.in_hall,
                    "withdrawn_at": _iso(link.withdrawn_at),
                },
            )
        )
    for link in JidanpuUpstreamOrderLink.all_objects.filter(order=order).order_by("id"):
        items.append(
            TimelineItem(
                # 该表没有 created_at；「上次推送 / 上次拉取」都缺席（从没动过）⇒ None。
                at=_iso(link.last_out_at or link.last_in_at),
                kind="link.jidanpu_upstream",
                kind_label="鸡蛋铺上家链路行",
                status=("retired" if link.retired_at is not None
                        else "terminated" if link.is_terminated else "linked"),
                summary=f"绑定 {link.binding_id}；上家单 {link.upstream_order_id}",
                raw={
                    "link_id": link.pk,
                    "binding_id": link.binding_id,
                    "upstream_order_id": link.upstream_order_id,
                    "terminated_at": _iso(link.terminated_at),
                },
            )
        )
    if order.split_id:
        members = list_split_group_members(order.split_id)
        items.append(
            TimelineItem(
                at=None,
                kind="mirror.split_group",
                kind_label="拆单组（只列同组单号）",
                status="grouped",
                summary="、".join(members),
                raw={"split_id": order.split_id, "member_order_nos": members},
            )
        )
    return items


# ── 载荷组装 ─────────────────────────────────────────────────────────────────


def _section(key: str, items: list[TimelineItem], cap: int) -> dict:
    """§1.6 节形状 `{key, label, truncated, items}`；超上限置 `truncated=true`。"""
    return {
        "key": key,
        "label": SECTION_LABELS[key],
        "truncated": len(items) > cap,
        "items": [item.as_payload() for item in items[:cap]],
    }


def _operation_log_items(order) -> list[TimelineItem]:
    rows = _operation_log_rows(order)
    rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    return [
        TimelineItem(
            at=_iso(row.created_at),
            kind=row.action,
            kind_label=action_label(row.action),
            status="",
            summary=summary_for_display(row, source_order_no=order.source_order_no),
            raw={
                "actor": row.actor,
                "actor_name": row.actor_name,
                "log_type": row.log_type,
                "target_name": row.target_name,
                "field_name": row.field_name,
                "from_value": row.from_value,
                "to_value": row.to_value,
                "reason": row.reason,
                "source_order_no": row.source_order_no,
            },
        )
        for row in rows
    ]


def _jidanpu_items(order) -> list[TimelineItem]:
    rows = _jidanpu_rows(order)
    rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    return [
        TimelineItem(
            at=_iso(row.created_at),
            kind=row.kind,
            kind_label=_jidanpu_kind_label(row.kind),
            status=row.status,
            summary=_sync_summary(row.reason, _jidanpu_kind_label(row.kind)),
            raw={
                "direction": row.direction,
                "direction_label": _DIRECTION_LABELS.get(row.direction, row.direction),
                "target_type": row.target_type,
                "target_ref": row.target_ref,
                "attempts": row.attempts,
                "processed_at": _iso(row.processed_at),
            },
        )
        for row in rows
    ]


#: `DailiantongPush.State` 的人话（`choices` 镜像）。
_DAILIANTONG_STATE_LABELS = {
    "pending": "发送中",
    "sent": "已发出",
    "failed": "发送失败",
    "unknown": "状态未知（需人工对账）",
    "closed": "已关闭",
}


def _dailiantong_items(order) -> list[TimelineItem]:
    return [
        TimelineItem(
            at=_iso(push.created_at),
            kind="dailiantong.push",
            kind_label="代练通抛单",
            status=push.state,
            summary=_DAILIANTONG_STATE_LABELS.get(push.state, push.state),
            raw={
                "serial_no": push.serial_no,
                "ex_order_no": push.ex_order_no,
                "remote_status": push.remote_status,
                "remote_cancel_status": push.remote_cancel_status,
                "last_error": push.last_error,
            },
        )
        for push in DailiantongPush.objects.filter(order=order).order_by(
            "-created_at", "-id"
        )
    ]


def _qr_fetch_items(order) -> list[TimelineItem]:
    """查码节（省库批口径）：**只显示查到行**——即既有成功路径写的 `order.malailai_qr_fetch` ok 行（reason = `LEDGER_OK_REASON`，写读共用一个常量）。
    ⚠️ D-0361 的首查 / 放弃两个台账 kind 已停写（每分钟全量轮询下是纯噪声）⇒「中间轮次与失败零台账」是拍板口径、不是留痕缺失：排障走 journald（逐轮摘要 + 失败明细）与本节查到行。
    ⚠️ 只查 `order_no=` 臂（有索引）：查码成功行写面恒带本单号（`order_qr_fetch._record_ledger`），不存在「只有来源号」的行，⛔ 不做第二臂；过滤用 reason 精确等值（失败行/闩沿行 reason 都带「失败」字样，天然落不进等值匹配，⛔ 不是前缀剔除）。
    """
    rows = list(
        OperationLog.objects.filter(
            action=LEDGER_ACTION, order_no=order.order_no, reason=LEDGER_OK_REASON
        ).order_by("-created_at", "-id")[:DEFAULT_SECTION_CAP]
    )
    if not rows:
        return [
            TimelineItem(
                at=None,
                kind="qr_fetch.note",
                kind_label="查码拉取",
                status="",
                summary="暂无（查码只在查到时留行；中间轮次与失败不进台账，排障查服务器日志）",
                raw={"suppress_until": _iso(order.qr_fetch_suppress_until)},
            )
        ]
    return [
        TimelineItem(
            at=_iso(row.created_at),
            kind="qr_fetch.hit",
            kind_label="查码拉取",
            status="ok",
            summary=row.reason,
            raw={"actor": row.actor},
        )
        for row in rows
    ]


def _receipt_rows(order) -> list:
    """收款记录（闲管家「交易成功」记账行）按双号臂查（计划 §九 C）。串单键（§九 C 已核实）：`XgjReceiptRecord.order_no` = 推送的闲鱼单号、与 `Order.source_order_no` 同源 ⇒ 两键 = `[source_order_no, order_no]` 去空去重，结果按 pk 去重（病态同串两键同命中只出一次）。
    ⚠️ 该列**单列无索引** ⇒ 全时间线第二个非索引点查（第一个是 OperationLog source 臂）；⛔ 不为此加迁移（量级 = 收款表体量，诊断频度可接受，照 OperationLog source 臂先例）。
    """
    keys = list(dict.fromkeys(k for k in (order.source_order_no, order.order_no) if k))
    if not keys:
        return []
    rows = list(XgjReceiptRecord.objects.filter(order_no__in=keys))
    seen: set[int] = set()
    deduped: list = []
    for row in rows:
        if row.pk in seen:
            continue
        seen.add(row.pk)
        deduped.append(row)
    # 交易成功时间在前的排前；`success_time` 可空（推送的 modify_time 认不出时留空），
    # 空时刻的行沉底（同刻按 id 稳定）。
    deduped.sort(
        key=lambda row: (row.success_time is None, row.success_time, row.id),
    )
    return deduped


def _receipt_summary(row) -> str:
    """收款行的人话：金额 + 店铺（快照 / 标识取非空者）+ 匹配结果（唯一出处的人话）。

    ⚠️ `amount` 可空 = 待回查 / 回查失败 / 详情认不出（模型 docstring 口径）⇒ 如实写
    「待回查」，不补 0（E-0021）；`raw_push` 不外泄（⛔ 推送原文不进时间线，只进归纳）。
    """
    if row.amount is None:
        amount_text = "实收金额待回查"
    else:
        amount_text = f"实收 {row.amount} 元"
    shop = row.shop_name_snapshot or row.shop_identity or "未识别店铺"
    return f"{amount_text}；店铺 {shop}；{match_summary_text(row.matched_count, row.match_result)}"


def _receipt_items(order) -> list[TimelineItem]:
    return [
        TimelineItem(
            at=_iso(row.success_time),
            kind="xgj.receipt",
            kind_label="闲管家收款",
            status="",
            summary=_receipt_summary(row),
            raw={
                "app_key": row.app_key,
                "amount": "" if row.amount is None else str(row.amount),
                "shop_name_snapshot": row.shop_name_snapshot,
                "shop_identity": row.shop_identity or "",
                "matched_count": row.matched_count,
            },
        )
        for row in _receipt_rows(order)
    ]


def _related_payload(order, peer_links: list) -> dict:
    """`related`：链路与拆组的结构化投影（相邻跳；不在 mirror 节重复展开）。"""
    return {
        "peer_links": [
            {
                "binding_role": (
                    "upstream" if link.upstream_binding_id else "downstream"
                ),
                "binding_id": (
                    link.upstream_binding_id or link.downstream_binding_id
                ),
                "peer_order_no": link.peer_order_no,
                "origin_order_no": link.origin_order_no,
            }
            for link in peer_links
        ],
        "split_group": (
            {"split_id": order.split_id, "member_order_nos": list_split_group_members(order.split_id)}
            if order.split_id
            else None
        ),
    }


def _order_payload(order, taker_display: str | None) -> dict:
    return {
        "order_no": order.order_no,
        "source_order_no": order.source_order_no,
        "status": order.status,
        "status_label": _ORDER_STATUS_LABELS.get(order.status, order.status),
        "game_name": order.game.name if order.game_id else "",
        "project_text": order.project_text,
        "is_deleted": order.deleted_at is not None,
        "deleted_at": _iso(order.deleted_at),
        "created_at": _iso(order.created_at),
        "updated_at": _iso(order.updated_at),
        "taker_display": taker_display,
    }


def _order_taker_display(order) -> str | None:
    """接手人 = `Performance` 打单工资行（FK 直取；无行回 `None`，⛔ 不编人名）。"""
    perf = (
        order.performances.filter(emp_type=EmpType.MAIN).select_related("user").first()
    )
    if perf is None or perf.user_id is None:
        return None
    return perf.user.get_full_name() or perf.user.get_username()


def _bindings_health(order, now) -> list[dict]:
    """时间线内嵌健康条：**只**看该单涉及的 member 绑定（量级天然有界）。"""
    binding_pks = set(
        JidanpuMemberOrderLink.objects.filter(order=order).values_list(
            "binding_id", flat=True
        )
    )
    entries = []
    for binding in JidanpuMemberBinding.objects.filter(pk__in=binding_pks).order_by(
        "pk"
    ):
        suspended, detail = readback_breaker_health(binding, now=now)
        entries.append(
            {
                "binding_id": binding.pk,
                "store": binding.account or binding.jidanpu_base_url,
                "enabled": binding.enabled,
                "suspended": suspended,
                "detail": detail,
            }
        )
    return entries


def build_order_timeline(order) -> dict:
    """§1.6 的 `data` 形状：`{order, related, sections, bindings_health}`。"""
    peer_rows, peer_links = _peer_rows(order)
    items_by_section = {
        "operation_log": _operation_log_items(order),
        "jidanpu_sync": _jidanpu_items(order),
        # peer 台账行与链路行投影**同一次**查出（⛔ 不为 related 再查一遍 link）。
        "peer_sync": [
            TimelineItem(
                at=_iso(row.created_at),
                kind=row.kind,
                kind_label=_peer_kind_label(row.kind),
                status=row.status,
                summary=_sync_summary(row.reason, _peer_kind_label(row.kind)),
                raw={
                    "direction": row.direction,
                    "direction_label": _DIRECTION_LABELS.get(row.direction, row.direction),
                    "binding_role": row.binding_role,
                    "target_ref": row.target_ref,
                    "attempts": row.attempts,
                    "processed_at": _iso(row.processed_at),
                },
            )
            for row in peer_rows
        ],
        "dailiantong": _dailiantong_items(order),
        "mirror": _mirror_items(order),
        "qr_fetch": _qr_fetch_items(order),
        "receipt": _receipt_items(order),
    }
    sections = [
        _section(
            key,
            items_by_section[key],
            OPERATION_LOG_CAP if key == "operation_log" else DEFAULT_SECTION_CAP,
        )
        for key in SECTION_ORDER
    ]
    return {
        "order": _order_payload(order, _order_taker_display(order)),
        "related": _related_payload(order, peer_links),
        "sections": sections,
        "bindings_health": _bindings_health(order, timezone.now()),
    }


def list_member_breaker_health() -> list[dict]:
    """`GET /api/audit/jidanpu-breakers/` 的载荷：全部**启用**绑定的读回熔断态。成本口径（§1.4，两级查询）：先按熔断台账 note 的 12h 窗区间扫判「有没有挂起迹象」（健康店一次区间扫即见底、毫秒级），仅挂起中的绑定才算完整 state——薄包装 `readback_breaker_health` 内部就是这个两级，⛔ 不在这里再判一次。健康条显示的是闸门视野内的挂起（12h 窗内 note 行为准），不是逐时刻真值。
    """
    now = timezone.now()
    entries = []
    for binding in JidanpuMemberBinding.objects.filter(enabled=True).order_by("pk"):
        suspended, detail = readback_breaker_health(binding, now=now)
        entries.append(
            {
                "binding_id": binding.pk,
                "store": binding.account or binding.jidanpu_base_url,
                "enabled": binding.enabled,
                "suspended": suspended,
                "detail": detail,
            }
        )
    return entries
