"""「绑定上家系统设置」页的后端端点（计划 `2026-10-04-upstream-binding-list-rework.md` §3.1 + 行级写端点，E-0255 / D-0256）。端点：`GET /api/business/upstream-bindings/`（**统一列表**：团团通上家 `PeerUpstreamBinding` 与鸡蛋铺上家 `JidanpuUpstreamBinding` **混在一张表**）；`POST …/jidanpu/`（**行级创建**一条鸡蛋铺上家绑定，D-0256：`user` = 操作者＝录入人，`enabled=True` 走与既有 PUT 同一份检测闸）；`GET …/jidanpu/<int:pk>/`（**行级读**，编辑弹窗回填出口，D-0261 起含 `order_source` = `{id, 名字}`）；`PUT` 同路径（**行级编辑**只动目标行，收窄写入 / 换绑收敛与既有 PUT 同一份实现；检测闸照 `_row_put_needs_detection`——⭐ D-0261：仅改来源不碰凭据 ⇒ 免检测、免盖 `checked_at`）；`DELETE` 同路径（**真删**，级联口径由模型 `on_delete` 决定）。
⛔ **为什么要有行级端点（E-0255 / D-0256）**：绑定原是「按登录用户一行」的单行资源，同一客服第二次绑定会**静默覆盖**本人已有行（生产实锤 suqing）；模型层本就支持多行（`user` 列现语义 = 「录入人」，`(地址, 账号)` 仍由表级唯一约束挡重复），单行只是旧视图层行为 ⇒ 页面弹窗改走这里的行级端点；按用户的 member PUT（`jidanpu_upstream_views.py`）**为兼容保留**、语义一字不改。两张表合到一条端点（计划 §0 裁定 2/3）：业务方要把那一页收口成「一张表 + 一列系统类型」，团团通行 = `deleted_at is null` 的全部行（既有页面口径），鸡蛋铺行 = **本租户全部行**（含 `enabled=False`，裁定 3「这里要显示全部」）。⛔ 范围只由**认证态与 Host**决定（AGENTS.md §4）：不接受 `?user=` / `?tenant=` 参数，本模块连 `request.GET` 都不读。
两条红线（改这个模块之前先读）：① **⛔ 统一列表一个字节都不写**（`get` 分支纯读：不查上游、不落库），行级三支的写入全部经 `jidanpu_upstream_views._persist_binding_with_audit`（业务写入与留痕**同一个事务**）；② **⛔ 凭据不进响应**：行里**没有** `password` / `password_set` / `token_*` / OSS 凭证 / cookie——⚠️ 统一列表**有意扩大**一处：鸡蛋铺行的**上家子账号**（`account`，登录名）可以出现在这张跨人的混排表里，但密码、是否已设密码、令牌都不进；**行级 GET** 回 `JidanpuUpstreamBindingSerializer` 的形状（**有** `password_set` 布尔）——那是编辑弹窗回填**自己的资源**（与 member GET 同一形状、同一份序列化器），密码本体仍**只写不读**、连键都不出现；⛔ 统一列表照旧一行都不给。
"""

from __future__ import annotations

from django.db import transaction
from rest_framework import serializers
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.jidanpu_upstream_views import (
    _detect_gate_response,
    _persist_binding_with_audit,
    _row_put_needs_detection,
    _upstream_binding_name,
)
from apps.business.api.jidanpu_upstream_views import JidanpuUpstreamBindingSerializer
from apps.business.api.serializers import PeerUpstreamBindingSerializer
from apps.business.jidanpu_upstream import batch_unlist_orphan_hall_rows
from apps.business.models import (
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    PeerUpstreamBinding,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 本页两条端点共用的**节点门**（与本页菜单 `system-source-system` 同一个点，fail-closed）。
#: 不复用 system.pinduoduo_settings：那是「谁能管开放平台应用」的门，
#: 和本页「谁能看/删上家绑定」不是同一批人。
UPSTREAM_BINDING_NODE = "system.bind_upstream_system"

#: 行里两个「系统类型」的字面值（**前端依赖的契约**，计划 §2.1）。
SYSTEM_TYPE_TUANTUANTONG = "tuantuantong"
SYSTEM_TYPE_JIDANPU = "jidanpu"

#: 取不到行（不存在 / 已被并发删除）时的人话（**外部契约值**：DELETE 的既有原话；
#: 行级 GET / PUT 与它**逐字同句**——同一页里「这条绑定没了」只有一种说法，⛔ 不另造）。
MISSING_BINDING_MESSAGE = "这个绑定不存在"

#: 行契约的**恰好 13 个键**（计划 §2.1 的表 + 大厅镜像的两格；⛔ 一个都不要多加 / 改名）。⚠️ 它**只作本模块的自文档**：真正的形状由下面两个构造函数逐字产出，用例**刻意另存一份字面副本**（`tests/test_upstream_binding_list.py`，E-0024 防自指断言）⇒ ⛔ 不要写「用例从这里派生」那种话（会被这条注释自己证伪）。
#: ⚠️ **两类行键集相同**（这是本页「一张表 + 一列系统类型」的落点）：`hall_sync_enabled` 在鸡蛋铺行上是 **`null`**（那张模型**没有**这一列，见 `_jidanpu_row`），⛔ 不是 `False`——`null` = 「这一行没有这个概念」，`False` = 「有、且关着」。
ROW_KEYS = (
    "id", "system_type", "system_name", "base_url", "account",
    "bound_by_name", "bound_at", "last_ok_at", "last_error", "last_error_at",
    "consecutive_failures",
    "hall_sync_enabled", "hall_price_percent",
)

#: 鸡蛋铺行的三个时刻列走它与 `PeerUpstreamBindingSerializer` **同一套** DRF 渲染
#: （后者是 `ModelSerializer` 自动生成的 `DateTimeField`，同样是全局 `ISO_8601` 口径）
#: ⇒ 两类行的时刻字符串形状一致，前端一套 `formatDateTime` 就够。
_DATETIME = serializers.DateTimeField()


def _peer_row(row) -> dict:
    """团团通行 → 本线的 13 键（字段名映射；取值全部复用既有序列化器）。"""
    payload = PeerUpstreamBindingSerializer(row).data
    return {
        "id": payload["id"],
        "system_type": SYSTEM_TYPE_TUANTUANTONG,
        "system_name": payload["peer_name"],
        "base_url": payload["peer_base_url"],
        "account": payload["peer_account"],
        "bound_by_name": payload["bound_by_name"],
        "bound_at": payload["bound_at"],
        "last_ok_at": payload["last_ok_at"],
        "last_error": payload["last_error"],
        "last_error_at": payload["last_error_at"],
        "consecutive_failures": payload["consecutive_failures"],
        # ⭐ 大厅镜像 P1：两格都取自**同一个**序列化器（⛔ 不在这里另写取值口径）。
        # 模型两列非空带默认值 ⇒ 老行读出来就是 `False` / `100`（⛔ 不是 `null`）。
        "hall_sync_enabled": payload["hall_sync_enabled"],
        "hall_price_percent": payload["hall_price_percent"],
    }


def _jidanpu_row(row) -> dict:
    """鸡蛋铺行 → 本线的 13 键。- ⭐ `system_name` = `row.name or row.account_name`（业务方指令）：**人填的「上家系统名称」优先**（同一租户绑多家上家鸡蛋铺时靠它区分），没填 ⇒ 回落同步引擎缓存的 `account_name`；
    两者都空 ⇒ **空串就回空串**，⛔ 仍不用地址编一个名字（计划 §2.1 的括注 / 残差第 5 条那条契约不变——地址编出来的名字看起来像同步引擎认证的显示名，页面照旧显示 `—`，行与行靠 `base_url` 那一列区分）。- `bound_at` = `created_at`（这张模型没有「绑定时刻」列，`created_at` 就是它）。
    - `consecutive_failures` = **恒 `0`**（模型没有这一列，见模块 docstring）。- ⭐ `hall_sync_enabled` = **`None`**：那张模型**没有**「同步上家抢单大厅」这一列（它只有 `enabled` = 启用订单同步，是**另一个概念**，⛔ 不要拿它顶替——那会让页面显示一个不存在的开关，而「订单同步开着」被读成「大厅共享开着」）。`None` 在这里是**诚实的三值逻辑**：`null` = 没有这个概念，`False` = 有、且关着。- ⭐ `hall_price_percent` = `row.hall_price_percent`（那张模型**有**这一列，是真值）；它与大堂共享线的百分比是**两件事**（那一个是「往里接」时我方接单价的勾兑）。"""
    return {
        "id": row.pk,
        "system_type": SYSTEM_TYPE_JIDANPU,
        # ⭐：人填名（`name`）优先，回落 `account_name`；口径见 docstring 首条。
        "system_name": row.name or row.account_name,
        "base_url": row.jidanpu_base_url,
        "account": row.account,
        "bound_by_name": user_display_name(row.user),
        "bound_at": _DATETIME.to_representation(row.created_at),
        "last_ok_at": _DATETIME.to_representation(row.last_ok_at),
        "last_error": row.last_error,
        "last_error_at": _DATETIME.to_representation(row.last_error_at),
        "consecutive_failures": 0,
        # ⭐ 大厅镜像 P1：取值口径见上面 docstring 的两条。
        "hall_sync_enabled": None,
        "hall_price_percent": row.hall_price_percent,
    }


class UpstreamBindingListView(APIView):
    """`GET /api/business/upstream-bindings/`（计划 §2.1）。⚠️ 只有 `get` ⇒ **有本节点的人**用其余方法会被框架挡成 **405**；**没有**本节点的人拿到的是 **403**（DRF 的 `dispatch` 先 `check_permissions`、再按方法取 handler ⇒ 权限那道门排在 405 之前）——两种都不是 500，但**别把它们说成同一件事**（本页新增/编辑走 D-0256 的行级端点）。
    ⚠️ **不分页**（与 `peer-bindings/` 同形）：响应是 `{"items": [...], "total": N}`。两段查询各**一次**（`select_related` 拿显示名，⛔ 不做 N+1），在 Python 里合并 + 排序（⛔ 不 UNION 拼字符串，计划 §3.1 第 3 条）。排序：按 `bound_at` **降序**（新的在前），两类**混排**；相同时用 `id` 降序兜底（判据可重跑，计划 §2.1）。

    ⛔ 本页新增/编辑已改走下面的**行级**端点（D-0256；旧写法是弹窗打按用户的 member PUT——那会把同账号的第二条绑定静默覆盖掉，E-0255）。
    """

    permission_classes = [has_node(UPSTREAM_BINDING_NODE)]

    def get(self, request):
        # 软删（解绑）过的团团通行不再出现（既有页面口径）。
        peer_rows = PeerUpstreamBinding.objects.filter(
            deleted_at__isnull=True
        ).select_related("bound_by")
        # 鸡蛋铺行 = 本租户**全部**行（含 `enabled=False`，计划 §0 裁定 3）。
        jidanpu_rows = JidanpuUpstreamBinding.objects.all().select_related("user")

        # 先按真实时刻排好序、再序列化：两类行的时刻列名不同（`bound_at` / `created_at`），
        # 拿序列化后的字符串回排会多一次解析、也会把 13 键契约搅进来。
        entries = [
            (row.bound_at, row.pk, _peer_row(row)) for row in peer_rows
        ] + [
            (row.created_at, row.pk, _jidanpu_row(row)) for row in jidanpu_rows
        ]
        # `reverse=True` ⇒ `bound_at` 降序、`id` 降序兜底（计划 §2.1 的顺序判据）。
        entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        items = [entry[2] for entry in entries]
        return api_response(data={"items": items, "total": len(items)})


class JidanpuUpstreamBindingCreateView(APIView):
    """`POST /api/business/upstream-bindings/jidanpu/`：**行级创建**一条鸡蛋铺上家绑定（E-0255 / D-0256）。⭐ 为什么是行级：绑定是**租户多行**资源——同一客服（或不同客服）各绑各的上家账号，一人可有多行；旧的按用户 member PUT 会把本人已有行**静默覆盖**（E-0255 的生产实锤）。## 口径（每条都有出处）- **节点门** = 与统一列表 / 删除**同一个** `UPSTREAM_BINDING_NODE`（D-0256：
    页面本来就是持节点人操作的，比「任意登录者可自助建绑定」更 fail-closed）。- **`user` = 操作者**（D-0256：该列语义 = 「录入人」）。⛔ 请求体里塞 `user` 会被序列化器**静默忽略**（它不在 `fields` 里），行主只能来自认证态（`AGENTS.md` §4）。- **检测闸** = 既有 PUT 的**同一份**（`_detect_gate_response`，⛔ 不抄第二份判据）：
    `enabled=True` 时先同步跑一次 `jidanpu_upstream.detect_binding`，不过 ⇒ **一行都不落**（那时还没进事务，E-0022 的边界不在场）、一条留痕都没有。- **留痕**：动作码沿用 `system.jidanpu_upstream_binding_create`，与业务写入**同一个事务**（`_persist_binding_with_audit`），留痕 `target` 指到**这一行**。⚠️ 只定义 `post` ⇒ 其余方法由框架挡成 **405**（列表住在上面的 `upstream-bindings/`，⛔ 不在这里再开一个 GET 口）。"""

    permission_classes = [has_node(UPSTREAM_BINDING_NODE)]

    def post(self, request):
        serializer = JidanpuUpstreamBindingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)  # 失败 ⇒ 统一处理器包成 1000
        # 「落库后会是什么」——一个**未落库**的实例（`user` 只能是操作者）。
        binding = serializer.pending_instance(request.user)
        if binding.enabled:
            gate = _detect_gate_response(binding)
            if gate is not None:
                return gate
        _persist_binding_with_audit(
            binding=binding,
            action="system.jidanpu_upstream_binding_create",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            # 创建照旧「enabled 即检测」⇒ 盖戳条件与 D-0261 之前一字不差。
            detection_ran=binding.enabled,
        )
        return api_response(data=JidanpuUpstreamBindingSerializer(binding).data)


class JidanpuUpstreamBindingDetailView(APIView):
    """`/api/business/upstream-bindings/jidanpu/<int:pk>/` 的**行级**三支（D-0256）：**GET** 读一条（编辑弹窗的回填出口——统一列表的行里**没有** `enabled`，弹窗要按库里现值预填就只能按主键读一次；⛔ 凭据红线照旧：密码只回 `password_set` 布尔，本体连键都不出现）；**PUT** 行级编辑、**只动目标行**（E-0255 的反义：不再有「覆盖本人行」这回事）；**DELETE** 真删（计划 §2.2 的既有行为）＋ 删号生效点：删后对本地址 `owner=NULL` 的无主 `listed` 镜像行跑一次批量下架（同地址接管保护照旧；僵尸窗治理⑤，修复轮 M1）。口径（每条都有出处）：节点门 = `UPSTREAM_BINDING_NODE`（与列表 / 删除同门）；**PUT 的收窄写入 / 换绑收敛** = 既有 PUT 的**同一份**实现（`_persist_binding_with_audit`，⛔ 不抄第二份判据）：只写 `_PUT_WRITE_FIELDS`（健康列不被打回旧值），改了 `(地址, 账号)` ⇒ 清 `checked_at` + 把**这一行**的在飞 outbox 事件收敛成 `skipped`；⭐ **检测闸**：改了凭据 / `enabled` 翻真 ⇒ 照旧先检测、不过一行不落；**仅改「订单来源」**这类不碰凭据、不翻开关的编辑 ⇒ 免检测、免盖 `checked_at`（判据唯一出处 `_row_put_needs_detection`）；member PUT 与行级创建不在本条范围内（照旧 `enabled` 即检测）。
    **PUT 不改 `user`**：该列语义 = 「录入人」（D-0256），序列化器里也没有 `user` 字段 ⇒ 请求体塞 `user` 被静默忽略、编辑不改变行的录入人。取不到行（不存在 / 已被并发删除）⇒ 与 DELETE **逐字同一句**「这个绑定不存在」+ `1002`（fail-closed、同页同一句人话）。⚠️ `atomic()` 块内⛔ **不许 `return`**（E-0022）：块外的出口只有上面那句「不存在」，块内只有「写 + 留痕」（`_persist_binding_with_audit` 自己管理事务）。
    """


    permission_classes = [has_node(UPSTREAM_BINDING_NODE)]

    def _row_or_none(self, pk):
        return JidanpuUpstreamBinding.objects.filter(pk=pk).first()

    def get(self, request, pk):
        row = self._row_or_none(pk)
        if row is None:
            return api_response(data=None, msg=MISSING_BINDING_MESSAGE, code=ErrorCode.BUSINESS)
        return api_response(data=JidanpuUpstreamBindingSerializer(row).data)

    def put(self, request, pk):
        row = self._row_or_none(pk)
        if row is None:
            return api_response(data=None, msg=MISSING_BINDING_MESSAGE, code=ErrorCode.BUSINESS)
        # ⚠️ 两个前置快照都必须在序列化器**改内存之前**取（同 member PUT 的注释口径）：
        #    `pending_instance()` 之后 `row` 与合并结果是同一个对象，「旧值」读不到。
        was_enabled = row.enabled
        previous_credentials = (row.jidanpu_base_url, row.account)
        serializer = JidanpuUpstreamBindingSerializer(row, data=request.data)
        serializer.is_valid(raise_exception=True)
        binding = serializer.pending_instance(row.user)
        # ⭐ D-0261（审阅 B）：仅改「订单来源」这类**不碰凭据、不翻开关**的编辑免检测，
        #    也免盖 `checked_at`（闸与盖戳同开同关，判据唯一出处 `_row_put_needs_detection`，
        #    ⛔ 不在这里抄第二份）；改了凭据 / 启用了 ⇒ 照旧先检测。
        needs_detection = _row_put_needs_detection(
            serializer.validated_data,
            previous_credentials=previous_credentials,
            was_enabled=was_enabled,
            now_enabled=binding.enabled,
        )
        if needs_detection:
            gate = _detect_gate_response(binding)
            if gate is not None:
                return gate
        _persist_binding_with_audit(
            binding=binding,
            action="system.jidanpu_upstream_binding_update",
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            detection_ran=needs_detection,
            price_fields=serializer.validated_data.keys() & {"hall_price_percent", "game_rules"},
        )
        return api_response(data=JidanpuUpstreamBindingSerializer(binding).data)

    def delete(self, request, pk):
        row = self._row_or_none(pk)
        if row is None:
            # 与 peer 那句人话**同一个出口**（`ErrorCode.BUSINESS` = 1002，计划 §2.2）。
            return api_response(data=None, msg=MISSING_BINDING_MESSAGE, code=ErrorCode.BUSINESS)
        # ⚠️ 审计句子里要用的名字与主键必须在 `delete()` **之前**取出来：
        #    `Model.delete()` 会把实例的主键置 `None`（Django 的既有行为）。
        target_pk = str(row.pk)
        target_name = _upstream_binding_name(row)
        with transaction.atomic():
            row.delete()
            # ⭐ 删号生效点（僵尸窗治理⑤，修复轮 M1）：SET_NULL 留下的无主 `listed` 行
            #    在本地址无人接管 ⇒ 批量下架；必须在 delete **之后**跑——删前绑定还
            #    enabled，会把自己挡在接管保护外。与删除+留痕同一个事务。
            batch_unlist_orphan_hall_rows(
                JidanpuUpstreamHallOrder.objects.filter(
                    upstream_base_url=row.jidanpu_base_url,
                    owner_binding__isnull=True,
                    status=JidanpuUpstreamHallOrder.Status.LISTED,
                )
            )
            record(
                log_type=ACTION_LOG_TYPES["system.jidanpu_upstream_binding_delete"],
                action="system.jidanpu_upstream_binding_delete",
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="jidanpu_upstream", target_id=target_pk,
                target_name=target_name,
            )
        return api_response(data=None)