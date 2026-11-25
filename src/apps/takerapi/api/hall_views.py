"""打手侧**对外**端点：拉可接单列表（`GET /api/taker/v1/hall/`）与镜像接单（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5.1/§5.3，判据清单 §9）。
承重：取数不经过内部合并读口、⛔ 不透传内部全量投影——对外原生行恰好 §5.1 的 11 键、镜像行 13 键（判据=`tests/test_takerapi_hall_api.py` 键集恰好断言）；范围收窄主体是 `request.auth.taker`（Bearer 下 `request.user` 是匿名，用它收窄=大厅恒空且判据假绿，`E-0004`）；keyword 可搜值 ⊆ 载荷可见值（可搜不可见=探测信道，见 `HALL_KEYWORD_FIELDS`）。
写侧复用领域层 `claim_upstream_mirror` / `claim_peer_mirror_sync`（⛔ 不抄第二份接单逻辑）；节点门 `grab_hall.take_order`：读=空列表、写=`1002` 同形文案（⛔ 不 403/1012——都是探测信道）。"""
from __future__ import annotations

from django.db import connection
from django.db.models import Q, Value
from django.db.models.functions import Coalesce, NullIf
from rest_framework import serializers
from rest_framework.views import APIView

from apps.business import jidanpu_common, jidanpu_upstream, peer_hall
from apps.business import selectors
from apps.business.api.order_views import _sorted_nonempty_params, order_search_params
from apps.business.api.serializers import HallSearchSerializer
from apps.business.claim import ClaimError
from apps.business.claim_channel import CHANNEL_TAKER_API
from apps.business.commission import CommissionError
from apps.business.game_scope import game_scope
from apps.business.models import JidanpuUpstreamHallOrder, PeerHallMirror
from apps.business.performance_amount import AmountError
from apps.business.selectors import search_hall
from apps.core.datetime_display import local_iso
from apps.core.short_cache import hall_cache_ttl, short_cached_payload
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.openapi.api.order_extra_views import page_window
from apps.openapi.idempotency import idempotent
from apps.openapi.serializers import money
from apps.rbac.permissions import nodes_of
from apps.takerapi.authentication import TakerAppBearerAuthentication
from apps.takerapi.credentials import TAKER_SPEC
from apps.takerapi.throttling import TakerAppFallbackThrottle

#: 对外 `keyword` 可搜值=行「订单号」键显示的那个值（原生=来源号优先展示号 D-0338；镜像=各自展示号）。
#: `HALL_DISPLAY_NO` 是 SQL 侧同值注解列（`NullIf`+`Coalesce` 与 `native_hall_rows` 的展示覆写同一个取值式，两处分叉=搜到的和看到的不一致）。
#: ⛔ 保持字面量、⛔ 不从内部 `KEYWORD_FIELDS` 派生（派生=内部新增可搜字段对外自动跟着可搜且无网会红）；闸=`tests/test_takerapi_hall_api.py::HallKeywordTests` 恰好集合断言。
HALL_DISPLAY_NO = "display_no"
HALL_KEYWORD_FIELDS = frozenset({HALL_DISPLAY_NO})

#: 打手侧大厅看/接要的节点。与原生抢单（`claim_order` 节点门）、鸡蛋铺镜像接单、内部大厅读口**同一颗**
#: `grab_hall.take_order`（spec §9-6：把节点从账号撤掉 ⇒ 抢单立刻不通）——能接真单的人才该看得见/接得了这三路。
TAKER_HALL_NODE = "grab_hall.take_order"
#: 没有上面那颗节点时的答复：与「这张单不存在 / 不在抢单大厅」**逐字同形**
#: （`order_write_views.NOT_IN_HALL_MSG`）。⛔ 不另造一句话：多一句差别就是一条
#: 「这个单号存在吗」的探测信道（spec §5.3-2），也会暴露「你有没有这个节点」。
NO_TAKER_NODE_MSG = "该订单不存在或不在抢单大厅"


def platform_config_claim_msg() -> str:
    """（业务方指令）：takerapi 抢单失败档的 1015 文案换装——平台名（鸡蛋铺/上家）一个字不出对外口，只在本模块两个接单出口换装。
    ⛔ 不改 `jidanpu_common` / `peer_hall` 的常量本体、不改内部网页版端点——内部排障需要那句真话。
    判据打在业务码（`ErrorCode.PLATFORM_NOT_CONFIGURED`）上 ⛔ 不是文案字符串：将来那两支新写配置类文案也自动被换装。"""
    studio = getattr(connection.tenant, "name", "") or "工作室"
    return f"还没配置好{studio}的同步账号，请联系管理员"

#: §5.1 行载荷键集（恰好 11 个）**没有常量**：`hall_row_payload` 的函数体是唯一出处
#: （再定一份同名常量=两处出处，改坏时互不跟随，`E-0024` 自指族）。
#: 判据在用例里**另写一遍字面量**（`tests/test_takerapi_hall_api.py` 的 `HALL_ROW_KEYS`），⛔ 不从本模块派生（自指恒真）。


def hall_row_payload(order) -> dict:
    """一行大厅单 → 对外载荷（唯一装配出口；§5.1 白名单恰好 11 键，逐键来源与禁键清单=本函数体本身）。
    `order_no` **恒填内置单号**：本装配被 `/my/orders/` 族复用（`claimed_order_row`），那边它是寻址键（只认内置号）；大厅原生行的「来源号优先展示号」由 `native_hall_rows` 覆写（D-0338），⛔ 不收回本函数（否则 my-orders 寻址键被改坏）。
    `hall_price`=生效接单价注解（`selectors.EFFECTIVE_TAKER_PRICE`）⛔ 不是原值列；`created_at`=项目时区 `+08:00`（D-0302）；NULL 价如实回 `null` ⛔ 不伪装 `"0.00"`（`D-0015`：null 与 0 是两件事）。"""
    return {
        # ⚠️ 恒填内置单号：`/my/orders/` 族复用本装配，那边 `order_no` 是寻址键（只认内置号）。
        # 大厅原生行的「来源号优先展示号」由 `native_hall_rows` 覆写（D-0338）——覆写只住那一个调用点，
        # ⛔ 不收回本函数（否则 my-orders 的寻址键被改坏）。
        "order_no": order.order_no,
        # ⭐ 业务方要求：把**三方/来源单号**也带上（打手拿它在团团通里对单）；
        # 没有来源号的历史单回空串（⛔ 不回 null：本列 `blank=True, default=""`）。
        "source_order_no": order.source_order_no,
        "game": order.game_id,
        "game_name": order.game.name,
        "server": order.server_id,
        "project_text": order.project_text,
        "content": order.content,
        "time_requirement": order.time_requirement,
        "is_urgent": order.is_urgent,
        "hall_price": money(getattr(order, selectors.EFFECTIVE_TAKER_PRICE)),
        # ⚠️ D-0302：走 `local_iso`（**项目时区** `+08:00`）。本族端点是打手
        # 侧自己（App / TAKER-API 文档）消费的——文档 §5.1 的示例本就是 `+08:00`，
        # 旧写法裸 `.isoformat()` 出 `+00:00` 与文档示例自相矛盾。
        "created_at": local_iso(order.created_at),
    }


def hall_mirror_row_payload(row: JidanpuUpstreamHallOrder) -> dict:
    """一行鸡蛋铺镜像单 → 对外载荷（D-0296）：**同形 11 键 + `origin` + `mirror_pk`**（逐键映射见函数体，形状必须可预测——第三方按这份表解析；`mirror_pk` 是镜像接单端点的路径参数）。
    `order_no`=`orderIdView` 展示号（空回落 `upstream_order_id`）；`server` 恒 `null`（文本区服无 FK）；`content`=区服文案；`time_requirement`/`is_urgent` 恒空/`False`（上家没给）。
    `hall_price`=勾兑展示价（与内部镜像行同一价源）；⛔ 禁键清单与 `hall_row_payload` 同一张（客户联系方式/员工身份/财务/`remote_*`/`sim_pk` 等一个不透传——对外只有这 13 键）。"""
    payload = row.upstream_payload if isinstance(row.upstream_payload, dict) else {}
    view_no = str(payload.get("orderIdView") or "") or row.upstream_order_id
    return {
        "order_no": view_no,
        "source_order_no": view_no,
        "game": row.game_id,
        "game_name": row.game.name,
        "server": None,
        "project_text": row.project_text,
        "content": row.remote_game_server,
        "time_requirement": "",
        "is_urgent": False,
        "hall_price": money(
            jidanpu_upstream.upstream_mirror_hall_price(row)
        ),
        # ⚠️ D-0302：同 `hall_row_payload`——`+08:00`（内部镜像行
        # `serialize_upstream_row` 同一次收口；`remote_created_at` 恒 aware，
        # 见 `_remote_datetime` 的注释）。
        "created_at": local_iso(row.remote_created_at or row.first_seen_at),
        "origin": "upstream",
        "mirror_pk": row.pk,
    }


def hall_peer_row_payload(row) -> dict:
    """一行团团通镜像单 → 对外载荷（D-0303）：与 `hall_mirror_row_payload`（鸡蛋铺）**恰好同键**（前端一张表渲染三路行），`origin="peer"`、`mirror_pk` 是镜像接单端点的路径参数；逐键映射见函数体。
    `order_no`/`source_order_no`=上家单号；`server` 恒 `null`（对端区服主键不消费，R21）；`content`=客户原始需求；`time_requirement`/`is_urgent` 恒空/`False`（载荷没有）。
    `hall_price`=展示价（`peer_hall.display_price`，与内部大厅/抢单闸/转正落库同一算法），无价如实 `null`（抢单闸会拒）；⛔ 禁键清单同另两路——payload 白名单全量一个不透传。"""
    payload = row.payload if isinstance(row.payload, dict) else {}
    return {
        "order_no": row.peer_order_no,
        "source_order_no": row.peer_order_no,
        "game": row.game_id,
        "game_name": row.game.name,
        "server": None,
        "project_text": str(payload.get("project_text") or ""),
        "content": str(payload.get("content") or ""),
        "time_requirement": "",
        "is_urgent": False,
        "hall_price": money(peer_hall.display_price(row)),
        # ⭐ 走 `local_iso`（项目时区 `+08:00`，D-0302）：同一份响应里另两路都是 `+08:00`，
        # 本行裸 `.isoformat()` 出 `+00:00` 是混排、违反 TAKER-API.md §5.1 契约。
        "created_at": local_iso(row.first_seen_at),
        "origin": "peer",
        "mirror_pk": row.pk,
    }


def _keyword_q(keyword) -> Q:
    """对外 `keyword` 的匹配条件：只在 `HALL_KEYWORD_FIELDS`（今天恰好 `{"display_no"}` 注解列）上 `icontains`。
    与内部三处（`search_orders`/`search_hall`/`search_recycle_bin`）唯一差别是字段面（不变式见模块 docstring）；命中判定与它们逐字同款。
    `display_no` 是本模块注解列名（载荷里没有）——调用方 `native_hall_rows` 必须先 `annotate`，否则 `FieldError`（响亮）。"""
    condition = Q()
    for field in HALL_KEYWORD_FIELDS:
        condition |= Q(**{f"{field}__icontains": keyword})
    return condition


class TakerHallSearchSerializer(HallSearchSerializer):
    """打手大厅检索参数 = 管理端那六个 **＋** 三方单号（业务方要求）。
    ⚠️ **不要**把 `source_order_no` 加进共用的 `HallSearchSerializer`：管理端大厅不应用它，
    加了就是「校验通过然后静默忽略」——那正是那份序列化器 docstring 在防的事。"""

    #: ⭐ 按**三方/来源单号**精确定位（⛔ 不做子串：来源号是外部系统的键，模糊匹配会带出别的单）。
    source_order_no = serializers.CharField(required=False, allow_blank=False, trim_whitespace=True)


def _view_no(row: JidanpuUpstreamHallOrder) -> str:
    """镜像行的**展示号**（`hall_mirror_row_payload` 的 `order_no` 键同一口径）。"""
    payload = row.upstream_payload if isinstance(row.upstream_payload, dict) else {}
    return str(payload.get("orderIdView") or "") or row.upstream_order_id


def mirror_pk_for_view_no(taker, view_no) -> int | None:
    """按**展示号**反查镜像行 pk（老接单端点的镜像兜底，业务方裁定）。展示号住原样行 JSON（`orderIdView`，缺回落 `upstream_order_id`）——与读侧 `order_no` 键同口径，⛔ 本表没有展示号列、别造第二份来源。
    **状态不限**（⛔ 不只查 `listed`）：非在挂行也必须查得到，才能让领域层前置把「已转正/已消失」翻成 `1012` 而不是伪装「不存在」；再过绑定账号的游戏权限（与读侧同道判据），多行同号取最新；找不到 ⇒ `None`（调用方翻 `1002` 同形，不泄露存在性）；成功仍只可能出自 `listed`（领域层自己的前置，本函数不复制）。
    ⭐ 归属绑定必须启用中（`owner_binding__enabled=True`，E-0288「可见=可接」延伸到「可解析」——停用绑定名下的 listed 僵尸卡曾由本通路被命中）；⛔ 刻意不连 `status=listed` 一起滤（「非在挂也查得到」是上面承重设计，有用例钉着，加了会把它们打回 1002）。"""
    qs = JidanpuUpstreamHallOrder.objects.filter(
        Q(upstream_payload__orderIdView=view_no) | Q(upstream_order_id=view_no),
        owner_binding__enabled=True,
    )
    qs = scope_queryset(taker, qs, scope_filter=game_scope("game_id"))
    row = qs.order_by("-first_seen_at", "-id").first()
    return row.pk if row else None


def peer_mirror_pk_for_order_no(taker, order_no) -> int | None:
    """按大厅载荷里的上家单号解析唯一可见 peer 镜像行。

    不筛状态：可见但已离厅的行仍交由接单领域层返回现有竞争结果；单号跨上家重复时拒绝猜测。
    """
    qs = PeerHallMirror.objects.filter(
        peer_order_no=order_no,
        peer_upstream_binding__deleted_at__isnull=True,
        peer_upstream_binding__enabled=True,
        peer_upstream_binding__hall_sync_enabled=True,
    )
    qs = scope_queryset(taker, qs, scope_filter=game_scope("game_id"))
    matches = list(qs.order_by("pk")[:2])
    return matches[0].pk if len(matches) == 1 else None


def mirror_claim_response(request, *, mirror_pk):
    """镜像接单的**请求主体**（`HallMirrorClaimView` 与老接单端点的镜像兜底共用；业务方裁定第三方只适配一个端点，⛔ 不抄两份）：范围收窄 fail-closed、领域层 `claim_upstream_mirror`、异常翻译、成功三键全只此一份。
    节点门在本主体里 ⇒ 两个入口自动同一道闸：无 `grab_hall.take_order` ⇒ `1002`+`NO_TAKER_NODE_MSG`（与「单不存在」同形防枚举；⛔ 不是 `1012`——那会告诉调用方单真的存在）。"""
    taker = request.auth.taker
    if TAKER_HALL_NODE not in nodes_of(taker):
        return api_response(
            data=None, msg=NO_TAKER_NODE_MSG, code=ErrorCode.BUSINESS
        )
    visible = scope_queryset(
        taker,
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror_pk),
        scope_filter=game_scope("game_id"),
    ).first()
    if visible is None:
        return api_response(
            data=None,
            msg=jidanpu_common.ALREADY_TAKEN_MESSAGE,
            code=ErrorCode.ORDER_ALREADY_TAKEN,
        )
    # ⚠️ 押金：不显式传 `enforce_deposit`（=回落租户总闸）——与原生抢单（`claim_order` 不传）同口径；
    # `TakerApp` 不是 `AppToken`，App 专属押金闸对这族本来不生效。留痕：转正内部已写审计行，
    # 这里 ⛔ 不再记一条 `order.claim`（同一次动作两行日志=说假话）。
    try:
        order = jidanpu_upstream.claim_upstream_mirror(
            user_id=taker.pk,
            mirror_id=mirror_pk,
            actor=taker.get_username() or "",
            actor_id=taker.pk,
            channel=CHANNEL_TAKER_API,
        )
    except ClaimError as exc:
        # ⭐（业务方报障修法一）：**命中即下架**的拆雷写入（`listed → gone`）必须保留——本端点族带
        # `@idempotent`，失败信封默认连业务写入一起回滚，不声明会让拆雷在这条路上永远落不了库。
        # 判据=异常上的 `defused` 标记（非拆雷的 1012 一行没写，必须保持「同键可重试」——`mark_side_effect` 纪律）。
        if getattr(exc, "defused", False):
            from apps.openapi.idempotency import mark_side_effect

            mark_side_effect(request)
        # ⭐ ：1015（平台没配置好）那一句在对外口换成工作室名——平台名
        # 一个字不出对外口；内部网页版仍走常量原句（`platform_config_claim_msg`）。
        if exc.code == ErrorCode.PLATFORM_NOT_CONFIGURED:
            return api_response(
                data=None, msg=platform_config_claim_msg(), code=exc.code
            )
        return api_response(data=None, msg=str(exc), code=exc.code)
    except (CommissionError, AmountError):
        # ⚠️ **不读异常的 message / code**：这一档的口径是固定文案 + `1002`
        # （照内部镜像接单视图同款——它发生在「上游占住已成功、我方落库失败」
        # 之后，翻成「已被接走」是说假话）。
        return api_response(
            data=None,
            msg=jidanpu_common.RESERVED_PROMOTE_PENDING_MESSAGE,
            code=ErrorCode.BUSINESS,
        )
    return api_response(
        {
            "order_no": order.order_no,
            "status": order.status,
            "status_text": order.get_status_display(),
        }
    )


def native_hall_rows(taker, *, keyword=None, source_order_no=None, native_scope=None, **filters):
    """原生那一路：`search_hall` + 范围收窄 + 对外 keyword / 三方单号 → `(时刻, 载荷)` 列表。收窄主体是 `taker`（绑定账号）⛔ 不是 `request.user`（模块 docstring 同一条）。
    `keyword` **刻意不传给 `search_hall`**（内部字段表含客户联系方式，传=重开「搜得到但看不见」的信道）：先注解 `display_no` 再 `icontains`——「搜到的就是看到的」。
    D-0338 展示覆写也在这里（⛔ 不在 `hall_row_payload` 改，那边被 my-orders 复用）：来源号非空取它、空保留内置号（纯自录单不空屏）。"""
    qs = search_hall(**filters)
    if native_scope is not None:
        qs = native_scope(qs)
    qs = scope_queryset(taker, qs, scope_filter=game_scope("game_id"))
    qs = qs.annotate(
        **{
            HALL_DISPLAY_NO: Coalesce(
                NullIf("source_order_no", Value("")), "order_no"
            )
        }
    )
    if keyword:
        qs = qs.filter(_keyword_q(keyword))
    if source_order_no:
        # ⭐ ：按**三方/来源单号**精确筛（业务方要求；只筛他本来就能看见的大厅单）。
        qs = qs.filter(source_order_no=source_order_no)
    # 整个结果会进短缓存。只从库读取打手载荷需要的字段。
    qs = qs.select_related(None).select_related("game").only(
        "order_no", "source_order_no", "game__name", "server_id", "project_text",
        "content", "time_requirement", "is_urgent", "created_at",
    )
    rows = []
    for order in qs:
        payload = hall_row_payload(order)
        # ⭐ D-0338：大厅原生行的 `order_no` 键=来源号优先展示号，与镜像两路（各自展示号）对齐——
        # 三路这一键都是「给打手看、拼接单 URL 用的那个号」。
        # ⛔ 抢单端点必须认得这个值（`_live_order_in_scope` 来源号兜底与本次是同一次契约，只改读不改写会把第三方全打回 1002）。
        if native_scope is None:
            payload["order_no"] = payload["source_order_no"] or payload["order_no"]
        rows.append((order.created_at, payload))
    return rows


def mirror_hall_rows(taker, *, keyword=None, source_order_no=None, **filters):
    """镜像那一路（D-0296）：`upstream_hall_queryset` **同源圈定**（`listed`+`select_related`，三筛选与内部同一语义）→ `(时刻, 载荷)`；范围收窄同一道 `scope_queryset`+`game_scope`，主体 `taker`。
    ⛔ `keyword`/`source_order_no` 不传给那个 queryset（它的 keyword 会搜上家单号/游戏名/区服/备注——都不在对外载荷里，搜=信道）⇒ 在内存按展示号判（`icontains` / 逐字相等）。
    ⭐ 价格区间在这里过（与内部同一把 `upstream_hall_price_in_range`，按勾兑展示价、两端含；展示价进不了 SQL）⛔ 也不传给 queryset（它收价格参数会 `TypeError`——内部读口刻意形状：静默忽略=放宽筛选）；时刻=`first_seen_at`。"""
    price_min = filters.pop("price_min", None)
    price_max = filters.pop("price_max", None)
    qs = jidanpu_upstream.upstream_hall_queryset(**filters)
    qs = scope_queryset(taker, qs, scope_filter=game_scope("game_id"))
    qs = qs.only(
        "upstream_order_id", "upstream_payload", "game__name", "project_text",
        "remote_game_server", "remote_rec_fee", "hall_price_percent_at_sync",
        "owner_binding__hall_price_percent", "remote_created_at", "first_seen_at",
    )
    has_price_filter = price_min is not None or price_max is not None
    rows = []
    for row in qs:
        if has_price_filter and not jidanpu_upstream.upstream_hall_price_in_range(
            row, price_min=price_min, price_max=price_max
        ):
            continue
        if keyword and keyword not in _view_no(row):
            continue
        if source_order_no and _view_no(row) != source_order_no:
            continue
        rows.append((row.first_seen_at, hall_mirror_row_payload(row)))
    return rows


def peer_mirror_hall_rows(taker, *, keyword=None, source_order_no=None, **filters):
    """团团通镜像那一路（D-0303）：`peer_hall.peer_hall_queryset` **同源圈定**，与 `mirror_hall_rows` 同一条形状与纪律（圈定同源、范围收窄同一道、主体 `taker`）。
    ⛔ `keyword`/`source_order_no` 不传给 queryset（会搜 payload 被投影掉的字段）⇒ 内存按上家单号判（`keyword`=区分大小写子串；`source_order_no`=逐字相等）。
    ⭐ 价格区间在这里过（同一把 `peer_hall_price_in_range`，两端含）⛔ 不传给 queryset（不收价格参数）；时刻=`first_seen_at`。"""
    price_min = filters.pop("price_min", None)
    price_max = filters.pop("price_max", None)
    qs = peer_hall.peer_hall_queryset(**filters)
    qs = scope_queryset(taker, qs, scope_filter=game_scope("game_id"))
    qs = qs.only(
        "peer_order_no", "game__name", "payload", "remote_hall_price",
        "hall_price_percent_at_sync", "peer_upstream_binding__hall_price_percent", "first_seen_at",
    )
    has_price_filter = price_min is not None or price_max is not None
    rows = []
    for row in qs:
        if has_price_filter and not peer_hall.peer_hall_price_in_range(
            row, price_min=price_min, price_max=price_max
        ):
            continue
        if keyword and keyword not in row.peer_order_no:
            continue
        if source_order_no and row.peer_order_no != source_order_no:
            continue
        rows.append((row.first_seen_at, hall_peer_row_payload(row)))
    return rows


def hall_rows_for_taker(taker, *, keyword=None, source_order_no=None, **filters) -> list[dict]:
    """三路并读：原生 ∪ 鸡蛋铺镜像 ∪ 团团通镜像（D-0296 两路 + D-0303 一路）。排序键写进性质：两路各自倒序、相同时间戳 ⇒ 原生 → 鸡蛋铺镜像 → 团团通镜像（⛔ 不依赖构造顺序+稳定排序）。
    ⭐ 节点门在三路之前：无 `grab_hall.take_order` ⇒ 空列表（`count=0`/`results=[]`，与「本租户没有单」同形；⛔ 不 403、不 1002）——能接真单的人才该看见，写侧已拒、读侧还给他看=让他点必然失败的按钮。
    判据读**绑定账号**的节点（入参 `taker`）⛔ 不是 `request.user`（Bearer 下是匿名，模块 docstring 同一条）。"""
    if TAKER_HALL_NODE not in nodes_of(taker):
        return []
    native = native_hall_rows(
        taker, keyword=keyword, source_order_no=source_order_no, **filters
    )
    mirror = mirror_hall_rows(
        taker, keyword=keyword, source_order_no=source_order_no, **filters
    )
    peer = peer_mirror_hall_rows(
        taker, keyword=keyword, source_order_no=source_order_no, **filters
    )
    items = [(moment, 0, payload) for moment, payload in native]
    items.extend((moment, 1, payload) for moment, payload in mirror)
    items.extend((moment, 2, payload) for moment, payload in peer)
    items.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return [payload for _moment, _rank, payload in items]


#: 短缓存键**不吃**的分页参数：`_build`（`hall_rows_for_taker`）不读它们、分页在缓存之后切片
#: ⇒ 每多一种翻页组合就多占一个 `max_entries=64` 槽位，纯浪费。
#: ⛔ `_build` 不得读分页参数——谁让 build 感知 `page`，这里就必须把两键请回键里（否则串页）；判据=`tests/test_hall_short_cache.py::HallTakerCacheHitTests::test_pages_slice_the_same_cached_rows`。
TAKER_CACHE_SKIP_PARAMS = frozenset({"page", "page_size"})


class HallListView(APIView):
    """`GET /api/taker/v1/hall/` —— 拉可接单列表（spec §5 端点表第 1 行）。装配：`TakerAppBearerAuthentication` + ⛔ 显式空 `permission_classes`（不写会吃全局 `IsAuthenticated`、合法令牌反被判 401）+ 兜底闸（装类属性上，`initial()` 顺序保证 401 先于 429、被挡请求不进桶）。
    ⛔ 不写 `get_permissions()` 覆写：对外授权面相同、类体一处就够（去掉它没有任何用例会红的东西不留，AGENTS §5-E）。判据顺序：①入参先过序列化器（非法 ⇒ `1000`+一条查询不发）→ ②`page_window` 自校验（`?page=abc` ⇒ `1000` ⛔ 不是 500）→ ③才取数。
    ⭐ 取数带 1 秒进程内短缓存（`apps/core/short_cache.py`）：键=端点+绑定账号主键+请求参数（空串不计；分页两键不在键里，见 `TAKER_CACHE_SKIP_PARAMS`）；校验在缓存外（缓存不吞错误形状）；代价=新鲜度 ≤ TTL。"""

    authentication_classes = [TakerAppBearerAuthentication]
    #: ⛔ 显式空列表 = **不做权限检查**（不写这个属性会吃全局默认的 `IsAuthenticated`
    #: ⇒ 合法令牌反被判 401；与上家侧对外面同款）。
    permission_classes = []
    #: 兜底闸装在类属性上 ⇒ DRF 的 `initial()` 顺序（认证 → 权限 → 限流）自动保证
    #: 「401 先于 429」且被 401 挡掉的请求**一个字节都不进桶**。
    throttle_classes = [TakerAppFallbackThrottle]

    cache_scope = "hall/taker/v1"

    def rows_for_taker(self, taker, **filters):
        return hall_rows_for_taker(taker, **filters)

    def get(self, request):
        data = order_search_params(request, TakerHallSearchSerializer)

        def _build():
            return self.rows_for_taker(
                request.auth.taker,
                game_id=data.get("game"),
                server_id=data.get("server"),
                project_text=data.get("project_text"),
                price_min=data.get("price_min"),
                price_max=data.get("price_max"),
                keyword=data.get("keyword"),
                source_order_no=data.get("source_order_no"),
            )

        # ⭐ 短缓存：键=端点+绑定账号主键（免押/节点门/押金规则/游戏权限都挂 taker 上 ⇒ 打手身份就是键维度；
        # 规则改动 ≤1 秒生效可接受）+请求参数（空串不计；分页两键剔除理由见 `TAKER_CACHE_SKIP_PARAMS`）。
        # 命中时 `_build`（连节点门）不执行 ⇒ taker.pk 必须在键里；构成与取舍见 `apps/core/short_cache.py`。
        rows = short_cached_payload(
            (
                self.cache_scope,
                request.auth.taker.pk,
                [
                    (k, v)
                    for k, v in _sorted_nonempty_params(request)
                    if k not in TAKER_CACHE_SKIP_PARAMS
                ],
            ),
            ttl=hall_cache_ttl(),
            build=_build,
        )
        # ⭐ 内存分页（D-0296 改准）：窗口校验仍是 `page_window` 那一份（非法 ⇒ 1000，⛔ 不另写一份钳制）；
        # `count`=三路合计（与内部读口同口径），键集 `{count, results, page, page_size}` 一字不变。
        # 短缓存下分页保持在缓存之后：缓存全量 `rows`，窗口校验每次都跑（缓存不吞 1000）；代价=被筛掉的行也进内存，不新增数量级。
        page, page_size = page_window(request)
        offset = (page - 1) * page_size
        return api_response(
            {
                "count": len(rows),
                "results": rows[offset : offset + page_size],
                "page": page,
                "page_size": page_size,
            }
        )


class HallMirrorClaimView(APIView):
    """`POST /api/taker/v1/hall/upstream/<int:pk>/claim/` —— 接一张**镜像单**（D-0296）。主体=`jidanpu_upstream.claim_upstream_mirror`（本地预占→赢租约才打上游→转正建单；⛔ 不抄第二份接单逻辑），镜像优选闸与押金闸在领域层自动生效。
    装配三件与 `HallClaimView` 同款：Bearer + ⛔ 不装权限类 + 兜底闸；收窄主体=`request.auth.taker`（⛔ 不读 `request.user`）。幂等装饰器**带上**：领域层「行必须 `listed`」挡**并发**、这层挡**重放**，两层不互替。
    ⛔ 写侧范围收窄必须有（漏=越权接单+越权计酬：转正会建真订单并写 MAIN 绩效）；取不到行与「这一行不存在」**逐字同形**（fail-closed 不泄露存在性）。"""

    authentication_classes = [TakerAppBearerAuthentication]
    permission_classes = []
    throttle_classes = [TakerAppFallbackThrottle]

    @idempotent(spec=TAKER_SPEC)
    def post(self, request, pk):
        # 主体在模块函数 `mirror_claim_response`：老接单端点的镜像兜底与本视图共用同一份（⛔ 不抄第二份）。
        return mirror_claim_response(request, mirror_pk=pk)


def peer_claim_response(request, *, mirror_pk):
    """团团通镜像接单的**请求主体**（`HallPeerClaimView` 用）。⭐ D-0347 起是**同步抢单**（「能抢就是抢到」）：`peer_hall.claim_peer_mirror_sync` 在请求内直推对端（≤3 秒）——受理 ⇒ 当场转正成交（`status="promoted"`）；回绝 ⇒ 当场 1012；无结论 ⇒ 放回大厅 + 1017。⛔ 不再有「接单中」的成功响应：**成功=已成交**。
    范围收窄 fail-closed（取不到行=「已被接走」，不泄露存在性）；押金显式传 `deposit_enforced_for`（App 令牌恒冻结；⛔ 不传=落回只读租户总闸、App 接镜像单不冻押金）。
    节点门在取行之前：无节点 ⇒ `1002`+`NO_TAKER_NODE_MSG`（与「单不存在」同形防枚举；⛔ 与 1012「已被接走」混——那句会泄露单存在）。"""
    taker = request.auth.taker
    if TAKER_HALL_NODE not in nodes_of(taker):
        return api_response(
            data=None, msg=NO_TAKER_NODE_MSG, code=ErrorCode.BUSINESS
        )
    visible = scope_queryset(
        taker,
        PeerHallMirror.objects.filter(pk=mirror_pk),
        scope_filter=game_scope("game_id"),
    ).first()
    if visible is None:
        return api_response(
            data=None,
            msg=jidanpu_common.ALREADY_TAKEN_MESSAGE,
            code=ErrorCode.ORDER_ALREADY_TAKEN,
        )
    # ⭐ 押金闸（热修）：本端点**恒为 App 令牌** ⇒ 用同一条判据
    # （`deposit_enforced_for` 会认 `AppToken` 并把 `DEPOSIT_ENFORCED_APP` 叠上来）。
    # ⛔ 不传 ⇒ 落回「只读租户总闸」⇒ App 接镜像单不冻结押金。
    from apps.business.deposit_gate import deposit_enforced_for

    try:
        row = peer_hall.claim_peer_mirror_sync(
            user_id=taker.pk, mirror_id=mirror_pk,
            enforce_deposit=deposit_enforced_for(request),
        )
    except ClaimError as exc:
        # ⭐ ：1015（hall_sync 关着那句「还没打开上家大厅同步」）在对外口
        # 换成工作室名——平台名一个字不出对外口；内部网页版仍走常量原句
        # （`platform_config_claim_msg`）。code 1015 原样保留。
        if exc.code == ErrorCode.PLATFORM_NOT_CONFIGURED:
            return api_response(
                data=None, msg=platform_config_claim_msg(), code=exc.code
            )
        return api_response(data=None, msg=str(exc), code=exc.code)
    return api_response(
        data={
            "mirror_pk": row.pk,
            "peer_order_no": row.peer_order_no,
            "status": row.status,
            "status_text": row.get_status_display(),
        }
    )


class HallPeerClaimView(APIView):
    """`POST /api/taker/v1/hall/peer/<int:pk>/claim/` —— 接一张**团团通上家**镜像单（D-0303）。主体=`peer_hall.claim_peer_mirror_sync`（D-0347 同步抢单直推；⛔ 不抄第二份接单逻辑），抢单闸（押金/游戏权限/上限）与优选闸都在领域层自动生效。
    装配三件与 `HallMirrorClaimView` 同款：Bearer + ⛔ 不装权限类 + 兜底闸；收窄主体=`request.auth.taker`。幂等装饰器带上：领域层「行必须 `listed`」挡**并发**、这层挡**重放**，两层不互替。"""

    authentication_classes = [TakerAppBearerAuthentication]
    permission_classes = []
    throttle_classes = [TakerAppFallbackThrottle]

    @idempotent(spec=TAKER_SPEC)
    def post(self, request, pk):
        return peer_claim_response(request, mirror_pk=pk)
