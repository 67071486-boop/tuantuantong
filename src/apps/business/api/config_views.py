from django.db import models, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopeModelViewSet, EnvelopePagination
from apps.business.api.serializers import (
    ConfigParentFilterSerializer,
    GameSerializer,
    GameServerSerializer,
    ProjectCategorySerializer,
    ProjectSerializer,
)
from apps.business.models import Game, GameServer, Order, Project, ProjectCategory
from apps.rbac.permissions import has_node


class ParentFilteredQuerysetMixin:
    """按父级过滤配置资源的公共实现。三个 ViewSet 各写一遍这段守卫就是三份拷贝，改一处漏两处是迟早的事（E-0010 记的正是这个形状）。所以：
    - 过滤字段由一个类属性声明（`parent_filter_field`） - 空串按「没传」处理（前端清空下拉会送 `?game=`） - 校验走 `ConfigParentFilterSerializer`，非法值 → DRF `ValidationError` → 200 + `code 1000`
    `GameViewSet` **不用**本 mixin：它是链顶、没有父级。
    """

    # ⚠️ `parent_filter_field` 是**开发者常量**，不是请求数据：漏声明时必须在**导入期** 响亮失败，而不是等第一个请求打进来、在 `get_queryset` 里以 `FieldError` → HTML 500 的形式暴露（ 的 self-concern；`FieldError` 是未预期的程序员错误，本就该 500）。
    # 注意：只写这行**注解**达不到效果——注解不产生类属性，`self.parent_filter_field` 照样在运行期才炸。真正的闸门是下面的 `__init_subclass__`。
    parent_filter_field: str

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not getattr(cls, "parent_filter_field", ""):
            raise TypeError(
                f"{cls.__name__} 继承 ParentFilteredQuerysetMixin "
                "但没有声明 parent_filter_field（要过滤的父级字段名）"
            )

    def get_queryset(self):
        raw = {k: v for k, v in self.request.query_params.items() if v != ""}
        params = ConfigParentFilterSerializer(data=raw)
        params.is_valid(raise_exception=True)
        queryset = super().get_queryset()
        parent_id = params.validated_data.get(self.parent_filter_field)
        if parent_id is not None:
            queryset = queryset.filter(**{f"{self.parent_filter_field}_id": parent_id})
        return queryset


class ConfigAuditMixin:
    """配置域四个 ViewSet 的写路径留痕 + 事务边界（spec §5.4 / §5.5 / §7.4；R27）。 ## 为什么埋在 DRF 的 `perform_*` 钩子上，而不是覆写 `create` / `update` / `destroy` 这四个 ViewSet **没有**自己的 `create` / `update` / `destroy`（它们来自 `EnvelopeModelViewSet` → `ModelViewSet` 的 mixin）。在子类覆写就必须把 DRF 的 `get_serializer` / `get_success_headers` / 状态码那一套**再抄四遍**；而 `perform_create` / `perform_update` / `perform_destroy` 正是 DRF 给「插入一次副作用」留的钩子（`rest_framework/mixins.py` 的 `CreateModelMixin` / `UpdateModelMixin`）——本 mixin 只做「写这一次、记这一次留痕」。 ## `partial_update` 不会记两次
    `PATCH` 由 router 映射到 `partial_update`（`rest_framework/routers.py:121` 的 `'patch': 'partial_update'`），而 `UpdateModelMixin.partial_update` **只是** `kwargs['partial'] = True` 后 `return self.update(...)`（`rest_framework/mixins.py:80-82`）， `update` 再调**一次** `perform_update`（同文件 `:63-68`）⇒ 一次 `PATCH` 恰好一行（有单测钉住）。**不要**在 `partial_update` 上再埋一次。 ## 事务（R27.1 / spec §7.4）这四个 ViewSet 此前**没有任何** `transaction.atomic()`（评审 B6 实测）⇒ 业务写入与 `record(...)` 必须收在同一个 `atomic()` 里：留痕写不进去时业务行要跟着回滚，不允许「配置改了、日志没有」的黑洞。**不在块内 `return` 退出**（E-0022）。
    `PROTECT` 挡住删除时 `instance.delete()` 抛的 `ProtectedError` 从块里冒出 ⇒ 本块回滚，再由 `EnvelopeModelViewSet.destroy` 翻成 `ErrorCode.CONFLICT`（既有行为，未改）；留痕在删除**成功之后**才写 ⇒ 被拒的删除不写行。 ## 纯 no-op 不写留痕（R27.2，与账号域 / RBAC 同口径） `PUT` / `PATCH` 把当前值原样重发一次 = **什么都没发生**，留下一条「编辑…」的留痕是假话。判据是**写之前**把 `serializer.validated_data` 与实例的**当前值逐个比**（值语义： FK 比主键、序列比集合），有任何一处不同才 `record`。
    """

    #: `(create, update, destroy)` 三个动作 code，由各 ViewSet 声明（spec §5.4 / §5.5）。
    audit_actions: tuple[str, str, str]
    #: 留痕的 `target_kind`（对标「相关类型」列），由各 ViewSet 声明。 ⚠️ R28：取值**只能是** spec §4.2 的枚举词表（`order` / `user` / `role` / `game` / `server` / `project` / `category` / `operator`）
    #: ——该列是自由 `CharField(16)`、没有 DB 级枚举，词表由 `ConfigTargetKindVocabularyTests` 字面钉住（自造词会让「按目标检索」漏行）。
    audit_target_kind: str
    #: 句子里要显示的名字取实例的哪个字段（R11：名字走 `target_name`）。四个模型都是 `name`。
    audit_name_field = "name"

    def __init_subclass__(cls, **kwargs):
        """漏声明 / 写错动作 code 时在**导入期**响亮失败，而不是等第一个请求打进来。 `record(...)` 用 `ACTION_LOG_TYPES[action]` 取分类，未登记的 code 会在请求路径上抛 `KeyError` → HTML 500（未预期异常，E-0010）。这与
        `ParentFilteredQuerysetMixin.__init_subclass__` 是同一个取向：开发者常量写错属于导入期错误。
        """
        super().__init_subclass__(**kwargs)
        actions = getattr(cls, "audit_actions", ())
        if len(actions) != 3:
            raise TypeError(
                f"{cls.__name__} 继承 ConfigAuditMixin 但没有声明 3 个 audit_actions"
                "（依次为 create / update / destroy 的 action code）"
            )
        if not getattr(cls, "audit_target_kind", ""):
            raise TypeError(
                f"{cls.__name__} 继承 ConfigAuditMixin 但没有声明 audit_target_kind"
            )
        unknown = [code for code in actions if code not in ACTION_LOG_TYPES]
        if unknown:
            raise TypeError(
                f"{cls.__name__} 的 audit_actions 未登记进 ACTION_LOG_TYPES：{unknown}"
            )

    # ── DRF 钩子（唯一的三个埋点）────────────────────────────────
    def perform_create(self, serializer):
        with transaction.atomic():
            super().perform_create(serializer)
            self._record_saved(self.audit_actions[0], serializer.instance)

    def perform_update(self, serializer):
        # ⚠️ R26 同口径（**对称性**，不是并发修复）：no-op 快照取在**写事务之内**，与 RBAC 的 R26 实现同形，且与真正落库的 `save()` 共用同一个事务 / 连接。 ⚠️ **不要把这句话读成「并发窗口已关闭」——它没有**（限定范围复审实测）：
        # `serializer.instance` 是 DRF 在 `get_object()` 里加载的（早于本方法、也早于 `atomic()`），而 `_changes_anything` 只比对内存里的 `validated_data` 与实例属性、**不发任何查询** （配置模型无 m2m，故也不会触发重读）；本仓库没有 `ATOMIC_REQUESTS`、
        # 这里也没有 `select_for_update` ⇒ READ COMMITTED 下另一个事务仍可在 `get_object()` 与本行 `UPDATE` 之间提交，守卫按**陈旧值**判「没变」而静默跳过留痕行。真要点掉这个窗口得加行锁或乐观并发判据（属行为改动，未做，见 spec §16.2 残差）。
        with transaction.atomic():
            # ⚠️ no-op 判据仍必须在**写之前**取：`serializer.save()` 之后实例上就是新值了。
            changed = self._changes_anything(serializer)
            super().perform_update(serializer)
            if changed:
                self._record_saved(self.audit_actions[1], serializer.instance)

    def perform_destroy(self, instance):
        # ⚠️ `delete()` 会把实例的 pk 置 `None`（Django `Model.delete()` 的既有行为）
        # ⇒ 目标两列必须在**删除前**取，否则「相关ID」列恒空、事后指认不出删的是哪一个。
        target_id = instance.pk
        target_name = self._name(instance)
        # ⭐ M3b：句子里要渲染的额外列（如订单来源的 `排序` / `备注`）同样必须**删除前**取
        # （删完实例就不在库里了，`_audit_columns` 的默认实现读实例属性，删后仍可读，
        # 但「读实例」这件事在语义上属于删除前——这里显式提前，避免将来改成回查库）。
        columns = self._audit_columns(instance)
        with transaction.atomic():
            super().perform_destroy(instance)
            self._record_config(
                self.audit_actions[2], target_id=target_id, target_name=target_name, **columns
            )

    # ── 内部 ─────────────────────────────────────────────────────
    def _record_saved(self, action, instance):
        self._record_config(
            action, target_id=instance.pk, target_name=self._name(instance),
            **self._audit_columns(instance),
        )

    def _audit_columns(self, instance):
        """句子要渲染的**额外列**（除 `target_id` / `target_name` 之外的模板变量）。四个配置域只渲染名字 ⇒ 默认返回空。⭐ M3b 新增 `OrderSource` / `ShopBinding` 两个域，它们的对标句式还带 `排序` / `平台` / `备注` ⇒ 各自覆写本钩子，把值放进
        `to_value` / `reason`（spec §5.9：值进 `to_value`、备注进 `reason`）。 ⚠️ **返回值直接透传给 `record(...)`**：键必须是 `record` 的形参名（`to_value` / `from_value` / `reason` …），写错会在请求路径上 `TypeError` → 500（未预期异常）。
        本钩子**只组装列、不开事务**——事务由调用方（`perform_*`）负责。
        """
        return {}

    def _record_config(self, action, *, target_id="", target_name="", **columns):
        """配置域的统一埋点形状（spec §5.4 / §5.5 / §5.9）。 - `log_type` 一律查登记表 `ACTION_LOG_TYPES`，**不写死数字**； - 操作人一律取**认证态**（`self.request.user`），从不取请求体里的身份（R20：
        请求体的身份可伪造，而留痕的价值就是「谁做的」为真）； - `target_id` 放数字 id（对标「相关ID」列），句子里要显示的名字走 `target_name`（R11）； - `columns` 是 `_audit_columns` 的额外列（M3b 的订单来源 / 店铺对接用它带
        `排序`/`平台`/`备注`）。 ⚠️ **调用方负责把它放进与业务写入同一个 `transaction.atomic()`**（spec §7.4）：本助手只组装列，不开事务。
        """
        user = self.request.user
        return record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=user.get_username(),
            actor_id=user.pk,
            target_kind=self.audit_target_kind,
            target_id=target_id,
            target_name=target_name,
            **columns,
        )

    def _name(self, instance):
        return getattr(instance, self.audit_name_field, "")

    def _changes_anything(self, serializer):
        """`validated_data` 里有没有任何一处与实例**当前值**不同（R27.2）。"""
        instance = serializer.instance
        for field, value in serializer.validated_data.items():
            if self._comparable(value) != self._comparable(self._current(instance, field)):
                return True
        return False

    @staticmethod
    def _current(instance, field):
        """实例上的当前值。多对多取集合——`getattr` 只会给一个管理器。"""
        if instance._meta.get_field(field).many_to_many:
            return list(getattr(instance, field).all())
        return getattr(instance, field)

    @staticmethod
    def _comparable(value):
        """把比较的两侧归一成**值语义**：模型实例 → 主键；序列 → 归一后的集合。

    ⚠️ 不能按**身份**比：同一行、同一批权限在两侧是不同实例，身份比较会把「没变」
    判成「变了」（RBAC 的权限 code 集合踩的正是这个坑）。
"""
        if isinstance(value, models.Model):
            return (value._meta.label, value.pk)
        if isinstance(value, (list, tuple, set, frozenset)):
            return {ConfigAuditMixin._comparable(item) for item in value}
        return value


#: **软删除的级联规则**（模块级、按模型声明；D-0085）。依据：父级一旦下架，各页的下拉里就再也选不到它 ⇒ 留着还活着的子项只会得到一种「半死不活」的状态——项目页是按**游戏**过滤的，所以某个分类被删后，它下面的项目**仍然
#: 显示**、而「所属分类」列却空着（分类字典里没有它了）。与其让用户看到这种状态，不如一起下架：删游戏连带它的分类与项目，删分类连带它的项目（递归）。 ⚠️ **恢复只恢复你点的那一行**（不自动把子项拉回来）：子项在各自的「已删除」视图里逐个恢复。
#: 顺序上先恢复父级（游戏/分类）才能在下拉里选到它、进而看到它的子项。 ⚠️ **级联下架的子项不各写一条操作日志**（日志里只有你点的那一条 `*.destroy`）——这是本轮有意的取舍，登记在 spec 里；将来要逐条留痕得给每个子项也调一次 `record(...)`。
SOFT_DELETE_CASCADE: dict[type, tuple[str, ...]] = {
    Game: ("categories",),
    ProjectCategory: ("projects",),
}


class SoftDeleteConfigMixin:
    """配置域的**软删除**公共实现（业务方裁定，D-0085）。四件事： 1. `DELETE` **不真删**：只把 `deleted_at` 打上时间戳，行留在库里 ⇒ 历史订单上的 `Order.game` / `category` / `project`（都是 `PROTECT`）原样有效，「被引用的配置删不掉」这条死路从此不存在（**历史数据一个字都不动**）； 2. **级联下架**：按 `SOFT_DELETE_CASCADE` 把还活着的下级一起打时间戳（见那里的注释）；
    3. **列表**默认排除已删除的行；`?deleted=1` **只看**已删除的行（前端「已删除」视图）； `?deleted=abc` 这类非白名单取值回 `code 1000`——**不静默当成 0**（同 E-0021 的口径：把「传了别的值」当「没传」会静默给出错的结果）； 4. **详情 / 更新 / 删除路径仍能看到已删除的行** ⇒ **恢复就是 `PATCH {"deleted_at": null}`**：复用既有的 `*.update` 埋点与端点契约， **不新增写端点、不新增审计动作**（也就不牵动「每个写端点都要分类」的覆盖率网）。
    另：`count_annotations` 给列表行注解「被引用了几次」，供前端**删除前的警告**显示（如「有 3 笔订单在用」）——引用计数是「一句有用的警告」与「一句废话」的区别。
    """

    #: `{"<序列化器字段名>": <聚合表达式>}`；空字典 = 不注解。三个 ViewSet 各自声明。 ⚠️ **每条 `Count` 都要 `distinct=True`**：同一个查询里对**两个及以上**多值关联做聚合时，几个 JOIN 会互相相乘（笛卡尔积），`Count("orders")` 会变成「订单数 × 分类数」——数字看着像对的、其实是错的。`distinct=True` 把每个计数都收成 DISTINCT 主键。有测试专门造「2 笔订单 + 3 个分类」来钉住这一点。 ⚠️ 计数还要**排除已下架的下级**（`filter=Q(...deleted_at__isnull=True)`）：警告里
    #: 「还有 2 个项目」说的是**页面上还看得到的**那 2 个，不是历史累计。 ⚠️ **但 `order_count` 是刻意的例外：它算上已软删的订单**——`Order.game` / `category` / `project` 都是 `PROTECT`，**已删订单行仍然物理引用着**这三者，用「还活着」的口径会**少报** ⇒ 删除前的警告不出现、点下去撞 `ProtectedError`（1001，spec D4）。 ⚠️ 机制上**不需要**在这里改写法：`models.Count("orders", …)` 走的是 SQL JOIN，而 **JOIN 不经过 `Order` 的默认 manager** ⇒ 它**本来就把已软删的行算进去**
    #: （实测：1 张在库 + 1 张已软删 ⇒ `Count("orders")` = 2）。⛔ 别按「反向名要换 `all_objects`」去改：反向关系的查询名只有 `orders` 一个，`Count("all_objects")` 实测 `FieldError`。三个 ViewSet 各自就地留了这句。
    count_annotations: dict[str, models.Count] = {}
    #: `?deleted=` 的**白名单**取值。不在里面的（含 `abc`）回业务码 1000，不静默当 0。
    DELETED_TRUE = ("1", "true")
    DELETED_FALSE = ("0", "false")

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action == "list":
            # ⚠️ **只有列表**区分已删除。详情 / 更新 / 删除必须看得见已删除的行，
            # 否则恢复（`PATCH {deleted_at: null}`）会 404——那是唯一一条恢复路径。
            queryset = queryset.filter(deleted_at__isnull=not self._wants_deleted())
        if self.action == "list" and self.count_annotations:
            queryset = queryset.annotate(**self.count_annotations)
            # ⚠️ **必须把默认排序补回来**（实测踩到）：`annotate()` 会打开 `group_by`，而 Django 在 group_by 的查询上**不再自动补 `Meta.ordering`** ——生成的 SQL 里 ORDER BY 整个消失（`Game.objects.annotate(...).query` 实测无 ORDER BY），
            # 整页顺序变成数据库的返回顺序。`test_game_config_replica` 的排序用例当场红。三个模型都有 `Meta.ordering`，显式写上即可（顺序语义与不加注解时逐字一致）。
            queryset = queryset.order_by(*queryset.model._meta.ordering)
        return queryset

    def _wants_deleted(self) -> bool:
        raw = self.request.query_params.get("deleted")
        if raw is None or raw == "" or raw in self.DELETED_FALSE:
            return False
        if raw in self.DELETED_TRUE:
            return True
        raise ValidationError({"deleted": ["只能是 1 / 0"]})

    def perform_destroy(self, instance):
        """软删除：**只写 `deleted_at` 一列**（`update_fields` 保证不会顺手把整行写回去）。

    不真删 ⇒ `ProtectedError` 那条补偿路径（`EnvelopeModelViewSet.destroy` 接住它回 1001）
    在本域的删除上不再会被触发；那条兜底**保留不动**（同一基类的订单来源 / 店铺对接还在用）。
"""
        now = timezone.now()
        instance.deleted_at = now
        instance.save(update_fields=["deleted_at"])
        self._cascade_soft_delete(instance, now)

    @classmethod
    def _cascade_soft_delete(cls, instance, now) -> None:
        """按 `SOFT_DELETE_CASCADE` 把下级**还活着的**行一起打上同一个时间戳（递归）。"""
        for related in SOFT_DELETE_CASCADE.get(type(instance), ()):
            for child in getattr(instance, related).filter(deleted_at__isnull=True):
                child.deleted_at = now
                child.save(update_fields=["deleted_at"])
                cls._cascade_soft_delete(child, now)


class GameViewSet(ConfigAuditMixin, SoftDeleteConfigMixin, EnvelopeModelViewSet):
    # 游戏 / 区服 ⇒ `40 系统相关`；项目 / 分类 ⇒ `70 其它`（spec §5.4/§5.5）。
    # log_type 一律由 `ACTION_LOG_TYPES` 派生，**不在这里写死数字**。
    audit_actions = ("system.game_create", "system.game_update", "system.game_destroy")
    audit_target_kind = "game"

    #: 逐 action 的节点 code 表。形状照 `order_views.py::OrderViewSet.ORDER_PERMISSIONS`。 ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的
    #: 字符串常量，用变量拼出来的 code 不会被它看见（该判据回答"`available=True` 的点有没有消费方"）。 ⚠️ **未登记在表里的 action（`list` / `retrieve` / …）一律走默认权限类**（fail-closed：仍要登录），**不是**默认放行——这些 action 不在映射判据网内，行为不变。
    GAME_PERMISSIONS = {
        "create": ("game.add_edit",),
        "update": ("game.add_edit",),
        "partial_update": ("game.add_edit",),
        "destroy": ("game.delete",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。 ⚠️ **为什么是并集**：默认类表达的是"这一层的最低门槛"（今天 = `IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`），
        节点判据表达的是"这个动作还要哪个 code"。两者是**合取**，不是二选一——替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。 ⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），**不是**"默认放行"。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.GAME_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`：
        # `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]

    # ⚠️ `order_count` 走 SQL JOIN ⇒ **不经 `Order` 的默认 manager** ⇒ 已软删的订单 **照样被算进去**。⚠️ ****：这**不是**为了避开 `ProtectedError`——本域的 `DELETE` 自 D-0085 起是**软删除**（`SoftDeleteConfigMixin` 只写 `deleted_at`），游戏行**永远不真删**，`PROTECT` 那条补偿路径在本域不会触发。算上已软删订单是因为它们**仍然物理引用**这个游戏（历史单的 `game` 外键要一直有效），删除警告说「有 N 笔订单在用」时把历史单一起算进去才是完整的历史账。
    # （旧 Count+JOIN 写法换 `all_objects` 会 FieldError——那条禁令只管 JOIN 形状； 2026-10-31 改成下面的子查询形状后，走 `Order.all_objects` 就能表达同一口径。） ⚠️ 形状（生产实测：订单量大的租户该列表 301ms、小租户 24ms）：三个多值关联在同一查询里 LEFT JOIN 会互相相乘、再靠 `distinct` 收敛，订单一多中间结果巨大；改成「按本游戏分组的小子查询」后行数永远 = 游戏数，值与旧写法逐一相等（`tests.test_config_soft_delete` 的计数与笛卡尔积用例原样钉着 ⇒ 无需 distinct）。
    def _grouped_count(queryset, group_by):
        # ⚠️ `output_field` 必须显式给：子查询推断 IntegerField、`Count` 是 BigIntegerField，
        # 不给会 `FieldError: mixed types`（实测当场红 10 条用例）。
        return Coalesce(
            models.Subquery(
                queryset.values(group_by).annotate(c=models.Count("pk")).values("c")[:1]
            ),
            models.Value(0),
            output_field=models.BigIntegerField(),
        )

    count_annotations = {
        # ⭐ 评审 Minor：隐藏单（镜像卡派生的载体）不是本租户的真订单 ⇒ 删除警告里不算它。 ⭐（评审 R-4）：同一道闸罩到**上家镜像**隐藏单（D-0325）——它同样没有客户 / 绩效；⚠️ 两族各一轴（`hall_card` / `upstream_hall_card`），一个 Q 里
        # AND 起来，与 `GameSerializer` 回落那条路的口径一致。 ⚠️ 走 `filter=` 而不是改反向关系：上面那条「算上已软删订单」的口径不变，这里只把「隐藏面」那一类挑掉（它没有客户 / 绩效，算进去会让人以为删游戏会
        # 带走一笔生意）。⚠️ 代价：游戏名下**只有隐藏单**时这句警告会显示 0——那只是数字不全，**不是操作死路**（删除是软删、不会因它失败）。
        "order_count": _grouped_count(
            Order.all_objects.filter(
                game=models.OuterRef("pk"),
                hall_card__isnull=True,
                upstream_hall_card__isnull=True,
            ),
            "game",
        ),
        "category_count": _grouped_count(
            ProjectCategory.objects.filter(
                game=models.OuterRef("pk"), deleted_at__isnull=True
            ),
            "game",
        ),
        "project_count": _grouped_count(
            Project.objects.filter(
                category__game=models.OuterRef("pk"),
                category__deleted_at__isnull=True,
                deleted_at__isnull=True,
            ),
            "category__game",
        ),
    }
    queryset = Game.objects.all()
    serializer_class = GameSerializer
    pagination_class = EnvelopePagination


class GameServerViewSet(ConfigAuditMixin, ParentFilteredQuerysetMixin, EnvelopeModelViewSet):
    audit_actions = (
        "system.server_create", "system.server_update", "system.server_destroy",
    )
    # ⚠️ R28：`target_kind` 一律取 spec §4.2 的**枚举词表** （order / user / role / game / server / project / category / operator），
    # 不照模型名自造——`game_server` 不在词表里，按目标检索的人会漏掉每一行；且 `project_category` 恰好 16 字符（= 列宽），一旦变长会被 `record()` 静默截断。
    audit_target_kind = "server"
    parent_filter_field = "game"
    queryset = GameServer.objects.select_related("game").all()
    serializer_class = GameServerSerializer
    pagination_class = EnvelopePagination

    #: 逐 action 的节点 code 表。形状照 `order_views.py::OrderViewSet.ORDER_PERMISSIONS`。 ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的
    #: 字符串常量，用变量拼出来的 code 不会被它看见（该判据回答"`available=True` 的点有没有消费方"）。 ⚠️ **未登记在表里的 action（`list` / `retrieve` / …）一律走默认权限类**（fail-closed：仍要登录），**不是**默认放行——这些 action 不在映射判据网内，行为不变。
    GAME_SERVER_PERMISSIONS = {
        "create": ("game.server",),
        "update": ("game.server",),
        "partial_update": ("game.server",),
        "destroy": ("game.server",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。 ⚠️ **为什么是并集**：默认类表达的是"这一层的最低门槛"（今天 = `IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`），
        节点判据表达的是"这个动作还要哪个 code"。两者是**合取**，不是二选一——替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。 ⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），**不是**"默认放行"。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.GAME_SERVER_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`：
        # `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]


class ProjectCategoryViewSet(
    ConfigAuditMixin, SoftDeleteConfigMixin, ParentFilteredQuerysetMixin, EnvelopeModelViewSet
):
    audit_actions = ("category.create", "category.update", "category.destroy")
    audit_target_kind = "category"
    parent_filter_field = "game"
    # ⚠️ `order_count` 与 `GameViewSet` 同一口径（JOIN 不经默认 manager ⇒ 已软删订单也算，让删除警告说出完整的历史账）。⚠️ ****：这**不是**为了避开
    # `ProtectedError`——分类的删除自 D-0085 起是软删除，不会真删行（见 `GameViewSet.count_annotations` 那条更正）。
    count_annotations = {
        "order_count": models.Count(
            "orders",
            filter=models.Q(orders__hall_card__isnull=True),
            distinct=True,
        ),
        "project_count": models.Count(
            "projects",
            filter=models.Q(projects__deleted_at__isnull=True),
            distinct=True,
        ),
    }
    queryset = ProjectCategory.objects.select_related("game").all()
    serializer_class = ProjectCategorySerializer
    pagination_class = EnvelopePagination

    #: 逐 action 的节点 code 表。形状照 `order_views.py::OrderViewSet.ORDER_PERMISSIONS`。 ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的
    #: 字符串常量，用变量拼出来的 code 不会被它看见（该判据回答"`available=True` 的点有没有消费方"）。 ⚠️ **未登记在表里的 action（`list` / `retrieve` / …）一律走默认权限类**（fail-closed：仍要登录），**不是**默认放行——这些 action 不在映射判据网内，行为不变。
    PROJECT_CATEGORY_PERMISSIONS = {
        "create": ("game.order_type_categories",),
        "update": ("game.order_type_categories",),
        "partial_update": ("game.order_type_categories",),
        "destroy": ("game.order_type_categories",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。 ⚠️ **为什么是并集**：默认类表达的是"这一层的最低门槛"（今天 = `IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`），
        节点判据表达的是"这个动作还要哪个 code"。两者是**合取**，不是二选一——替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。 ⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），**不是**"默认放行"。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.PROJECT_CATEGORY_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`：
        # `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]


class ProjectViewSet(
    ConfigAuditMixin, SoftDeleteConfigMixin, ParentFilteredQuerysetMixin, EnvelopeModelViewSet
):
    """项目。**两种过滤都能用**：`?category=`（模型上的直接父级）与 `?game=`（翻译成 `category__game`）。为什么要 `?game=`：对标物的「常用代练项目管理」页是**按游戏**过滤的（`GetProjectList?gameId=`，实测 `gameId` 有效），而这一页的 UI 是「游戏下拉 + 单表」——
    它**不选分类**（决策 D-0023 / spec §3.3）。我们的模型是 项目→分类→游戏 三级，所以这里必须把 `game` 翻译成跨一级的 `category__game`，否则前端只能对每个分类各发一次请求再在浏览器里合并（N 次请求、且分页/排序会变难）。
    """

    audit_actions = ("project.create", "project.update", "project.destroy")
    audit_target_kind = "project"
    parent_filter_field = "category"
    # 删除前警告要用的引用计数：`Project.orders`（同样必须 `distinct=True`，见 mixin 的说明）。 ⚠️ 它**刻意算上已软删的订单**：JOIN 不经 `Order` 的默认 manager ⇒ 物理引用被算进去，让警告说出完整的历史账。⚠️ ****：这**不是**为了避开
    # `ProtectedError`——删除自 D-0085 起是软删除，不会真删项目行（见 `GameViewSet.count_annotations` 同一条更正）。
    count_annotations = {
        "order_count": models.Count(
            "orders",
            filter=models.Q(orders__hall_card__isnull=True),
            distinct=True,
        )
    }
    queryset = Project.objects.select_related("category__game").all()
    serializer_class = ProjectSerializer
    pagination_class = EnvelopePagination

    def get_queryset(self):
        queryset = super().get_queryset()
        # 与 mixin 同一套校验（`ConfigParentFilterSerializer`）：`?game=abc` 要回 `code 1000`，
        # 不能把未经校验的值直接拿去 filter（那会 `ValueError` → HTML 500，见 E-0010）。
        raw = {k: v for k, v in self.request.query_params.items() if v != ""}
        params = ConfigParentFilterSerializer(data=raw)
        params.is_valid(raise_exception=True)
        game_id = params.validated_data.get("game")
        if game_id is not None:
            queryset = queryset.filter(category__game_id=game_id)
        return queryset

    #: 逐 action 的节点 code 表。形状照 `order_views.py::OrderViewSet.ORDER_PERMISSIONS`。 ⚠️ 字符串必须是**字面量**：`tests/test_rbac_structure.py` 的 B2 走 AST 取非 docstring 的
    #: 字符串常量，用变量拼出来的 code 不会被它看见（该判据回答"`available=True` 的点有没有消费方"）。 ⚠️ **未登记在表里的 action（`list` / `retrieve` / …）一律走默认权限类**（fail-closed：仍要登录），**不是**默认放行——这些 action 不在映射判据网内，行为不变。
    PROJECT_PERMISSIONS = {
        "create": ("game.common_projects",),
        "update": ("game.common_projects",),
        "partial_update": ("game.common_projects",),
        "destroy": ("game.common_projects",),
    }

    def get_permissions(self):
        """逐 action 装配节点判据；**与默认权限类取并集**（不替换）。 ⚠️ **为什么是并集**：默认类表达的是"这一层的最低门槛"（今天 = `IsAuthenticated`，`config/settings.py` 的 `DEFAULT_PERMISSION_CLASSES`），
        节点判据表达的是"这个动作还要哪个 code"。两者是**合取**，不是二选一——替换掉默认类会让"接口需要登录"这条全局不变式只在本视图里失效。 ⚠️ 未登记在表里的 action 一律走默认类（fail-closed：仍要登录），**不是**"默认放行"。
        """
        base = [permission() for permission in self.permission_classes]
        codes = self.PROJECT_PERMISSIONS.get(self.action)
        if not codes:
            return base
        # `IsAuthenticated` 提到最前并保证**恰好一个**；其余默认类**一条都不丢**。
        # ⚠️ 判等必须用 `type(p) is not IsAuthenticated`，**不要**用 `isinstance`：
        # `isinstance` 会把 `IsAuthenticated` 的**子类**当成同一类一并丢掉。
        others = [p for p in base if type(p) is not IsAuthenticated]
        return [IsAuthenticated(), has_node(*codes)(), *others]
