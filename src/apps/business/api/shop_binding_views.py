"""店铺对接的 CRUD 端点（spec §6.1）。形状照 `source_views.py`：单独文件、少冲突面。⭐ 三个写端点原先没被覆盖率网分类 ⇒ 现在挂 `ConfigAuditMixin` 埋点，**来源 = 新拟**（对标 logType=40 只有游戏 / 角色权限 / 订单来源三类、无店铺对接动作；两路否定证据见 spec §5.4 补记）。
⚠️ **凭据纪律**：`ShopBinding.access_token` 是租户在阿奇索侧的授权码，**一个字符都不进日志**（模板见 `apps/audit/actions.py` 的 `SYSTEM_SHOP_BINDING_*`，只渲染 名 / 平台 / 备注），且**不进 API 响应**（只写不读，读侧只有 `access_token_set` 布尔——见 `ShopBindingSerializer`）；平台级 `AGISO_APP_SECRET` 只住在 `.env`、连模型都没有（spec §9-S3）。
"""
from django.conf import settings
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.api.config_views import ConfigAuditMixin
from apps.business.api.serializers import ShopBindingSerializer
from apps.business.models import ShopBinding
from apps.core.envelope import api_response
from apps.rbac.permissions import has_node


class ShopBindingViewSet(ConfigAuditMixin, EnvelopeModelViewSet):
    """`/api/business/shop-bindings/`：列表 / 新增 / 编辑 / 删除。⭐ **E-0253**：全家端点（含 `platform-config`）补**逐 action 节点门** `system.pinduoduo_settings`——与前端门同一个点（页面/端点同宽；该节点被 4 个页面共用，本次只收**本端点**），修前全套 CRUD 只吃默认权限类（「前端藏了、后端没闸」）。
    **不声明 `permission_classes`**：吃 fail-closed 的默认权限类（`IsAuthenticated`），节点判据由 `get_permissions()` **与默认类取并集**地叠上去（形状照 `config_views.py` 的 `GAME_PERMISSIONS` / `GAME_SERVER_PERMISSIONS`）。租户边界由 schema 本身保证——`queryset` **不带任何租户参数**（租户上下文只能来自认证态/域名，绝不接受请求参数传入，AGENTS.md §4）。⚠️ **`http_method_names` 关掉 PUT**：spec §6.1 端点表只有 `GET/POST/PATCH/DELETE`，PUT 是 router/`ModelViewSet` 默认多给的能力面、本资源没有消费方 ⇒ 没有 spec 依据的能力面不暴露；理由**不是**「PUT 会清空授权码」（`ShopBindingSerializer.update()` 对缺省 `access_token` 保留旧值、DRF 非 partial 的 update 只 setattr 请求里出现的键）；一条 `PUT → 405` 的用例钉住它。
    留痕（spec §5.4）：三个写端点由 `ConfigAuditMixin` 的三个 DRF 钩子埋点（与业务写入**同事务**、**纯 no-op 更新零行**）；这里只声明动作 code / `target_kind` 并给出句式要渲染的额外列：`平台` → `to_value`、`备注` → `reason`，`{名}` 走 `target_name`（店铺名，空则退回平台文案）。
    """


    #: 逐 action 的节点 code 表：本资源的**全部**动作都收进同一个节点——前端页面（拼多多对接设置）整页挂在它后面，端点面与页面同宽，不留「列表要节点、单行不要」的窄门。形状照 `config_views.py::GameViewSet.GAME_PERMISSIONS`。
    #: ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的字符串常量，变量拼出来的 code 不会被它看见（该判据回答「`available=True` 的点有没有消费方」）。
    #: ⚠️ 未登记在表里的 action（DRF 以后新增的默认动作）一律走默认权限类（fail-closed：仍要登录），**不是**默认放行。
    SHOP_BINDING_PERMISSIONS = {
        "list": ("system.pinduoduo_settings",),
        "retrieve": ("system.pinduoduo_settings",),
        "create": ("system.pinduoduo_settings",),
        "update": ("system.pinduoduo_settings",),
        "partial_update": ("system.pinduoduo_settings",),
        "destroy": ("system.pinduoduo_settings",),
        "platform_config": ("system.pinduoduo_settings",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。⚠️ **为什么是并集**：默认类表达的是「这一层的最低门槛」（今天 = `IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`），节点判据表达的是「这个动作还要哪个 code」——两者是**合取**不是二选一，替换掉默认类会让「接口需要登录」这条全局不变式只在本视图里失效。
        ⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），**不是**「默认放行」。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.SHOP_BINDING_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`：
        #    `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    audit_actions = (
        "system.shop_binding_create",
        "system.shop_binding_update",
        "system.shop_binding_destroy",
    )
    #: spec §4.2 的 `target_kind` 词表 + M3b 新增的 `shop_binding`（见 spec §4.2 的 M3b 补记）。
    audit_target_kind = "shop_binding"

    queryset = ShopBinding.objects.all()
    serializer_class = ShopBindingSerializer
    pagination_class = EnvelopePagination
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def _name(self, instance):
        """句子里显示的「名」：店铺名 / 昵称；空则退回平台文案（否则句子会缺主词）。

        ⚠️ **不用 `shop_id`**：它不写入（spec §5.2），空列不配当主词。
        """
        return instance.shop_name or instance.get_platform_display()

    def _audit_columns(self, instance):
        """`平台` → `to_value`、`备注` → `reason`（spec §5.9）。⛔ **绝不返回 `access_token` / `shop_id`**：授权码是租户凭据，一旦进日志列（`reason` / `to_value` …）就等于把凭据抄进了可查询的明细表，且 `?actor=` 那类检索会把它带出来——本条由 `ShopBindingWritePathLogTests.test_no_credential_reaches_any_log_column` 钉住。
        """
        return {
            "to_value": instance.get_platform_display(),
            "reason": instance.remark,
        }

    @action(detail=False, methods=["get"], url_path="platform-config")
    def platform_config(self, request):
        """平台级 AppId（`settings.AGISO_APP_ID`）——**与任何一行店铺对接都无关**。三件事写在这里，省得后来者重新猜一遍：
        ① **它是平台级配置**：同一份部署里**每个租户读到同一个值**（由 `.env` 的 `AGISO_APP_ID` 决定），不随租户变、更不随某一行 `ShopBinding` 变；② **它刻意下发给租户浏览器**：租户要拿它去阿奇索授权，必须看得见——它**不是密钥**、只是应用标识，平台 `AGISO_APP_SECRET` 才是密钥且**一次都不进浏览器**（spec §9-S3 / §9-S4），别把两者混为一谈；③ **为什么需要独立出口**：逐行的 `platform_app_id`（`ShopBindingSerializer` 的 `SerializerMethodField`，表格 `AppId` 列仍在用）在**一条对接记录都没有**时给不出值——列表为空 ⇒ 新租户在弹层里看不到自己要拿去授权的 AppId；本端点与「行」解耦、**零绑定**时也能显示，这是它存在的唯一理由（没有这个理由就不该多开一条端点）。
        ⚠️ `detail=False` 的 action 由 DRF 注册在 `{pk}` 详情路由**之前**（`rest_framework/routers.py` 的 `SimpleRouter.routes` 顺序：list → `detail=False` 动态路由 → detail），所以 `platform-config` 不会被 `lookup_value_regex` 当成主键抢走（实测见 `tests/test_shop_binding_api.py::test_platform_config_route_is_not_swallowed_by_the_detail_route`）。
        """

        return api_response(data={"platform_app_id": getattr(settings, "AGISO_APP_ID", "")})
