"""闲管家店铺对接的端点（spec §5）。两个消费方，分开写： | 路径 | 方法 | 干什么 | |---|---|---| | `/api/business/xgj-bindings/…` | GET/POST/PATCH/DELETE | 租户维护自己的 AppKey/AppSecret（**保存后自动同步一次店铺**，R5） | | `/api/business/xgj-bindings/{id}/test-connection/` | POST | 调 `user/authorize/list` 列出这个应用能操作的店铺 |
| `/api/business/xgj-bindings/{id}/sync-shops/` | POST | 手动重同步店铺 → 来源（R5/R7） | | `/api/business/xgj-bindings/{id}/shops/` | GET | 该应用下的店铺（含对应来源、订购状态）——对接页那张表的数据源 | | `/api/business/xgj-shops/` | GET | **跨应用**的店铺列表（按来源 / 公司店铺分组筛）——「订单来源管理」页「店铺归属」区块 | | `/api/business/xgj/order-detail/` | POST | 按订单号查详情（**上游原样返回**，见下） | 形状照 `shop_binding_views.py`：单独文件、少冲突面。 ⚠️ **`order-detail` 回的是上游原样**（spec §5 的注）：订单字段四十来个，逐个映射成中文标签是
**前端**的事；后端再转译一层只会多一份会漂移的对照表。**录单读单**那条路（`read-platform-order`）才做字段映射，因为那是给录单表单用的固定六字段形状。 ⚠️ **自动建的来源是普通来源**（spec per-source §3-D9）：本模块不锁、不标记；租户删了它，下次同步/录单会**再建一个**（已登记代价）。
"""
from django.conf import settings
from django.db import transaction
from rest_framework import serializers, viewsets
from rest_framework.decorators import action

from apps.business.api.base import (
    EnvelopeModelViewSet,
    EnvelopePagination,
    EnvelopeReadOnlyModelViewSet,
)
from apps.business.api.config_views import ConfigAuditMixin
from apps.business.api.serializers import XgjShopBindingSerializer
from apps.business.models import XgjShop, XgjShopBinding
from apps.business.source_groups import (
    group_rows_for_source,
    legacy_group_for_source,
    source_ids_in_group,
)
from apps.business.xgj import AUTH_ERROR_CODE, DEFAULT_BASE_URL, XgjError, order_detail, shop_list
from apps.business.xgj_shop import subscription_warning, sync_shops
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode


def credentials_of(binding: XgjShopBinding) -> tuple[str, str] | None:
    """取一条绑定的上游凭据；不完整就回 `None`（调用方一律按 `1015` 处理）。 ⚠️ 与阿奇索那条路的 `binding.access_token.strip()` 同一条理由：租户是**复制粘贴**进来的，首尾的空格与换行很常见，而带换行的 `appid` 会让上游回 `100001`（看起来像"密钥错了"）。
    序列化器的 `validate_*` 已经 strip 过一次，这里**再 strip 一次**是为了覆盖「历史数据 / 直接从 shell 写库」这类绕过序列化器的路径——与 `read-platform-order` 同款防御。
    """
    if binding is None:
        return None
    app_key = (binding.app_key or "").strip()
    app_secret = (binding.app_secret or "").strip()
    if not app_key or not app_secret:
        return None
    return app_key, app_secret


def _upstream_error_code(exc: XgjError) -> int:
    """上游失败 → 业务码。判据是「**等于 `AUTH_ERROR_CODE`**」，不枚举其它码（spec §7）。"""
    if exc.upstream_code == AUTH_ERROR_CODE:
        return ErrorCode.PLATFORM_TOKEN_INVALID
    return ErrorCode.PLATFORM_QUERY_FAILED


class XgjShopBindingViewSet(ConfigAuditMixin, EnvelopeModelViewSet):
    """`/api/business/xgj-bindings/`：列表 / 新增 / 编辑 / 删除 / 测试连接。 **不声明 `permission_classes`**：吃 fail-closed 的默认权限类（`IsAuthenticated`），与既有配置类 CRUD 同形。租户边界由 schema 本身保证——`queryset` **不带任何租户参数** （租户上下文只能来自认证态/域名，绝不接受请求参数传入，`AGENTS.md` §4）。 ⚠️ **关掉 PUT**（同 `ShopBindingViewSet`）：spec §5 的端点表只有 PATCH， PUT 是 router 默认多给的能力面，本资源没有 PUT 消费方。
    ## 留痕：三个写端点由 `ConfigAuditMixin` 的三个 DRF 钩子埋点（与业务写入**同事务**； **纯 no-op 更新零行**）——与阿奇索那条路（`ShopBindingViewSet`，M3b）**同款处置**：覆盖率网对新写端点当场会红（`test_every_enumerated_write_endpoint_is_classified`），按「不许放宽判据」在这里补埋点。 ⚠️ **凭据纪律**：`XgjShopBinding.app_secret` 是租户在闲管家建的自研应用密钥，
    **一个字符都不进日志**（模板见 `apps/audit/actions.py` 的 `SYSTEM_XGJ_BINDING_*`，只渲染 店铺名 / 备注，且 `_audit_columns` 只回 `reason`）。 ⚠️ `test-connection` 与 `xgj/order-detail` **不埋点**：它们只读上游、**不落库** （与 `read-platform-order` 同类），已显式登记进 `DELIBERATELY_NOT_LOGGED`。
    """

    audit_actions = (
        "system.xgj_binding_create",
        "system.xgj_binding_update",
        "system.xgj_binding_destroy",
    )
    #: spec §4.2 的词表 + Task L 新增的 `xgj_binding`（见 spec §4.2 的 Task L 补记）。
    audit_target_kind = "xgj_binding"
    #: 这张表的主词是 `shop_name`（不是配置域那四个的 `name`）。
    audit_name_field = "shop_name"

    queryset = XgjShopBinding.objects.all()
    serializer_class = XgjShopBindingSerializer
    pagination_class = EnvelopePagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    # ── 保存后自动同步一次店铺（R5）────────────────────────────

    def finalize_response(self, request, response, *args, **kwargs):
        """把「保存后自动同步一次店铺（R5）」的结果挂到**保存响应**上（`data.sync`）。 ⚠️ **为什么既不覆写 `create`/`update`、也不覆写 `perform_create`/`perform_update`** （2026-09-22 合并 `main` 时实测，两条路都试过、都会红）：审计日志线的覆盖率网 `test_instrumented_endpoints_really_call_record` 是按「**该方法的钩子由哪个类定义**」解析埋点的——只要本类自己定义了 `perform_create` / `perform_update`，它就**只在本类的函数体里**找 `record(...)`，找不到就判
        「声称埋点却找不到」；覆写 `create`/`update` 会走上另一条同样会红的解析支路。所以那两个钩子的**定义者必须保持 `ConfigAuditMixin`**（它负责 `serializer.save()` 与 `record(...)`，那是合并进来的既有行为，不能被这一层盖掉）。本方法只**读响应**：拿 `id` 反查那一行，不碰任何钩子。 **怎么认出「这是一次保存」**：`self.action`（DRF 在路由分发时写好的动作名）只认 `create` / `update` / `partial_update` —— 本类另外三个动作
        （`test-connection` / `shops` / `sync-shops`）也会走 POST/GET，绝不能顺手给它们也挂一个 `sync`。⚠️ 判据用 `self.action` 而不是请求方法，正因为 `sync-shops` 也是 POST。 **为什么同步失败不回滚保存**：这一行的价值是"租户把凭据记在这里"；凭据写错了，他要的是把错的改对，而不是"连记录都不让存"。所以保存照常成功，同步结果单独报（前端据此提示「已保存，但店铺同步失败」）。凭据不完整时**跳过**，不发上游请求。
        """
        response = super().finalize_response(request, response, *args, **kwargs)
        if getattr(self, "action", "") not in ("create", "update", "partial_update"):
            return response
        if not (200 <= response.status_code < 300):
            return response
        body = response.data
        # 成功路径已经被 `EnvelopeModelViewSet.finalize_response` 包成信封 ⇒ 塞进 `data` 里。
        if not (isinstance(body, dict) and isinstance(body.get("data"), dict)):
            return response
        pk = body["data"].get("id") or kwargs.get("pk")
        binding = XgjShopBinding.objects.filter(pk=pk).first() if pk else None
        if binding is not None:
            body["data"]["sync"] = self._sync_or_reason(binding)
        return response


    def _sync_or_reason(self, binding) -> dict:
        """同步一次并把结果翻译成**可展示的结构**（不回上游原文，spec §9-S1）。"""
        if credentials_of(binding) is None:
            return {"skipped": "AppKey / AppSecret 还没填完整"}
        try:
            return sync_shops(binding)
        except XgjError as exc:
            return {"error": "店铺同步失败", "code": _upstream_error_code(exc)}

    # ── 系统操作日志的列映射（审计日志线）──────

    def _audit_columns(self, instance):
        """`备注` → `reason`（spec §5.9：备注进 `reason`，且只放**纯值**）。 ⛔ **绝不返回 `app_key` / `app_secret` / 两个推送 URL**：前两个是租户凭据。
        本条由 `tests/test_xgj_shop_binding.py` 的凭据断言钉住（与阿奇索那条路的 `test_no_credential_reaches_any_log_column` 同款）。
        """
        return {"reason": instance.remark}

    # ── 测试连接 ───────────────────────────────────────────────

    @action(detail=True, methods=["post"], url_path="test-connection")
    def test_connection(self, request, pk=None):
        """「测试连接」：拿这一行的凭据调 `user/authorize/list`。 **为什么值得单开一个动作**：业务方 R4 的原话是「该应用**仅能调用当前闲管家账号绑定的店铺**」——「这个应用到底能操作哪些店」必须**看得见**，否则租户要等录单失败才知道密钥配错了。
        ⚠️ 凭据**只从这一行取**（`get_object()` 走的是本租户的 queryset）：绝不接受请求里传 `app_key`/`app_secret`，否则这就是一条拿我们当跳板去打闲管家接口的通道（spec §9-S3）。失败一律回业务码，不回上游原文（原文可能含上游内部信息，且对租户无用）。
        """
        binding = self.get_object()
        credentials = credentials_of(binding)
        if credentials is None:
            return api_response(
                data=None,
                msg="这一行还没填完整的 AppKey / AppSecret",
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        app_key, app_secret = credentials
        try:
            shops = shop_list(
                app_key=app_key,
                app_secret=app_secret,
                # ⚠️ 基址**只由平台配置提供**；空值回落实测基址（`XGJ_API_BASE=` 是合法配置，
                #    所以判据用 `or` 而不是 `getattr(..., DEFAULT_BASE_URL)` 的默认参数）。
                base_url=getattr(settings, "XGJ_API_BASE", "") or DEFAULT_BASE_URL,
            )
        except XgjError as exc:
            # ⚠️ **本模块不把上游原文落日志**（只回业务码与一句固定文案）——这是密钥不进日志的第一道保证。真要记上游原文，**必须**先过 `platform_order_views._redact_secret`： `app_secret` 我方**从不发出**（参与签名的只是它的 MD5 派生值，`xgj.sign`），
            # 但上游回包是**第三方文本**，它回显收到的 query/header 是完全可能的，而我们 **无法枚举**它不会 ⇒ 判据不能是"我们没发它就一定安全"（这是关于第三方的绝对句，按 AGENTS.md §5 不能当依据）。
            return api_response(
                data=None,
                msg="连接失败：AppKey / AppSecret 无效，或应用未通过审核",
                code=_upstream_error_code(exc),
            )
        return api_response(data={"shops": shops})

    # ── 店铺同步（R5/R7）与店铺列表（对接页那张表）──────────────

    @action(detail=True, methods=["post"], url_path="sync-shops")
    def sync_shops(self, request, pk=None):
        """手动重同步：拉一次店铺列表，幂等地落成本地表 + 来源表（spec §5）。 **为什么要一个手动入口**：① 绑定之后新开的店（R7）在录单前就能先建好来源；
        ② `XgjShopBinding.order_source` 被拆掉之后，升级的租户要靠**点一次这里** （或重存那一行）把「店 ↔ 来源」重建出来（per-source spec §7 的升级步骤）。
        """
        binding = self.get_object()
        # ⚠️ 拉上游**在事务外**（可能 10s 超时，不该占着事务）；落库 + 留痕在**同一个
        #    `atomic()` 里**（spec §7.4：留痕写不进去时业务行要跟着回滚）。
        result = self._sync_or_reason(binding)
        if "error" in result:
            return api_response(
                data=None, msg=result["error"], code=result.get("code", ErrorCode.PLATFORM_QUERY_FAILED)
            )
        if "skipped" in result:
            # 什么都没发生（凭据不全）⇒ **不留痕**：写一条「同步了」是假陈述。
            return api_response(
                data=None, msg=result["skipped"], code=ErrorCode.PLATFORM_NOT_CONFIGURED
            )
        # ⚠️ 「同步店铺」是一个**写端点**（落 `XgjShop` 行 + 自动生成的 `OrderSource`），    所以它有**自己的**动作 `system.xgj_binding_sync_shops`，而不是借
        #    `system.xgj_binding_update`（那会是一句假陈述：这一行本身没被改）。    保存时那次**自动**同步**不另记**——那条路自己的 create/update 已经记了一行。
        with transaction.atomic():
            self._record_config(
                "system.xgj_binding_sync_shops",
                target_id=binding.pk,
                target_name=binding.shop_name or f"#{binding.pk}",
                reason=(
                    f"新增店铺 {result.get('created_shops', 0)} 家 / "
                    f"更新 {result.get('updated_shops', 0)} 家 / "
                    f"新建来源 {result.get('created_sources', 0)} 个"
                ),
            )
        return api_response(data=result)

    @action(detail=True, methods=["get"], url_path="shops")
    def shops(self, request, pk=None):
        """这个应用下的店铺（含对应来源 id/名字 + 订购状态）——对接页下方那张表的数据源。 ⚠️ 走 `get_object()`（本租户的 queryset）：传别家的 id 落 404 ⇒ 拿不到任何数据。
        ⚠️ `subscription_warning` 是**订购状态**的提醒，**不是版本判定**（上游不返回版本名）：详见 `xgj_shop.subscription_warning` 的 docstring 与 spec per-source §3-D11。
        """
        binding = self.get_object()
        items = [
            {
                "id": shop.pk,
                "shop_name": shop.shop_name,
                "user_name": shop.user_name,
                "order_source": shop.order_source_id,
                "order_source_name": shop.order_source.name if shop.order_source_id else "",
                "is_valid": shop.is_valid,
                "valid_end_time": shop.valid_end_time,
                "subscription_warning": subscription_warning(shop),
                "binding": binding.pk,
                "binding_name": binding.shop_name,
            }
            for shop in binding.shops.select_related("order_source").order_by("id")
        ]
        return api_response(data={"items": items})


class XgjShopSearchSerializer(serializers.Serializer):
    """`GET /api/business/xgj-shops/` 的查询参数（**唯一**校验口，E-0010）。两个都是 id：`min_value=1` 挡掉 `0` / 负数；`abc` / 非整数字符串由 `IntegerField`
    回 200 + `1000`（⛔ 不直接丢进 `filter()`，那会抛 `ValueError` ⇒ HTML 500）。缺席 = 不筛（**不设 `default`**，与 `OrderSearchSerializer` 的 id 类参数同口径）。
    """

    order_source = serializers.IntegerField(required=False, min_value=1)
    source_group = serializers.IntegerField(required=False, min_value=1)


class XgjShopRowSerializer(serializers.ModelSerializer):
    binding = serializers.IntegerField(source="binding_id", read_only=True)
    binding_name = serializers.SerializerMethodField()
    order_source = serializers.IntegerField(source="order_source_id", read_only=True)
    order_source_name = serializers.SerializerMethodField()
    group_id = serializers.SerializerMethodField()
    group_name = serializers.SerializerMethodField()
    groups = serializers.SerializerMethodField()
    subscription_warning = serializers.SerializerMethodField()

    class Meta:
        model = XgjShop
        fields = [
            "id", "shop_name", "user_name",
            "order_source", "order_source_name", "group_id", "group_name", "groups",
            "is_valid", "valid_end_time", "subscription_warning",
            "binding", "binding_name",
        ]

    def get_binding_name(self, obj) -> str:
        return obj.binding.shop_name if obj.binding_id else ""

    def get_order_source_name(self, obj) -> str:
        return obj.order_source.name if obj.order_source_id else ""

    def get_group_id(self, obj):
        group = legacy_group_for_source(obj.order_source) if obj.order_source_id else None
        return group.pk if group is not None else None

    def get_group_name(self, obj) -> str:
        group = legacy_group_for_source(obj.order_source) if obj.order_source_id else None
        return group.name if group is not None else ""

    def get_groups(self, obj):
        return group_rows_for_source(obj.order_source) if obj.order_source_id else []

    def get_subscription_warning(self, obj) -> str:
        """与对接页子表**同一个**判据（`xgj_shop.subscription_warning`），⛔ 不另写一份。"""
        return subscription_warning(obj)


class XgjShopListViewSet(EnvelopeReadOnlyModelViewSet):
    """按来源或分组筛选跨应用店铺。"""

    serializer_class = XgjShopRowSerializer
    pagination_class = EnvelopePagination

    def get_queryset(self):
        """按入参过滤（唯一校验口在 `XgjShopSearchSerializer`）。

        ⚠️ `get_queryset` 里读 `self.request.query_params` 是本仓既有的只读筛选形态
        （`OrderViewSet.get_queryset` 同款）；不传就不过滤。
        """
        params = XgjShopSearchSerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        data = params.validated_data
        qs = (
            XgjShop.objects.select_related("binding", "order_source__group")
            .prefetch_related("order_source__groups")
            .order_by("id")
        )
        if data.get("order_source") is not None:
            qs = qs.filter(order_source_id=data["order_source"])
        if data.get("source_group") is not None:
            qs = qs.filter(order_source_id__in=source_ids_in_group(data["source_group"]))
        return qs


class XgjOrderDetailInputSerializer(serializers.Serializer):
    """查详情的入参判据。**请求来源的值一律过序列化器**（AGENTS.md §5 / E-0010）。 `order_no` 只允许数字（闲鱼订单号是 19 位纯数字，官方示例 `2226688164543566229`）。 ⚠️ 用 `[0-9]` 而**不是** `\\d`：Python 的 `\\d` 默认匹配 **Unicode 十进制数字**（如「١٢٣」），
    那些值不是闲管家要的订单号 ⇒ 会白跑一趟上游拿 `1017`，而不是在入参处就 `1000`。 ⚠️ 上限 32 位是**保守**取值（真实订单号 19 位；上游对超长 `order_no` 的行为没实测）。
    """

    binding = serializers.IntegerField()
    order_no = serializers.RegexField(
        r"^[0-9]{1,32}$", error_messages={"invalid": "订单号只能是数字（≤32 位）"}
    )


class XgjOrderDetailView(viewsets.GenericViewSet):
    """只暴露一个动作：`POST /api/business/xgj/order-detail/`。形状照 `ReadPlatformOrderView`（本仓「显式 `path()` + `GenericViewSet.as_view({...})`」的既有手法）——**不用** `@action`：那会额外生成一条别名路径，同一件事两个入口。
    ⚠️ `binding` 走的是**本租户的 queryset**（`XgjShopBinding.objects`）：传别的租户的 id 会落 `Http404` ⇒ 4004，拿不到任何数据（spec §9-S2）。
    """

    queryset = XgjShopBinding.objects.all()

    def order_detail(self, request):
        payload = XgjOrderDetailInputSerializer(data=request.data)
        payload.is_valid(raise_exception=True)   # 失败 ⇒ 统一异常处理器包成 1000
        binding = self.get_object_or_none(payload.validated_data["binding"])
        credentials = credentials_of(binding)
        if credentials is None:
            return api_response(
                data=None,
                msg="这家店铺还没配置好闲管家对接，请先到「闲管家店铺对接」完成配置",
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        app_key, app_secret = credentials
        try:
            detail = order_detail(
                payload.validated_data["order_no"],
                app_key=app_key,
                app_secret=app_secret,
                base_url=getattr(settings, "XGJ_API_BASE", "") or DEFAULT_BASE_URL,
            )
        except XgjError as exc:
            # 同 `test_connection`：不记 `app_secret`，不把上游原文弹给租户。
            return api_response(
                data=None, msg="闲管家订单读取失败", code=_upstream_error_code(exc)
            )
        return api_response(data={"detail": detail})

    def get_object_or_none(self, pk):
        """按主键在**本租户**的 queryset 里取一行；取不到回 `None`。 ⚠️ 用 `filter(pk=...).first()` 而不是 `self.get_object()`：后者的 404 会让「配置不存在」与「配置不完整」走出两种不同的错误形状，而这两件事对租户是同一件事
        （「这家店没配好」）⇒ 一律 `1015`。租户边界不靠参数：`queryset` 已经是本 schema 的全部行。
        """
        return self.get_queryset().filter(pk=pk).first()
