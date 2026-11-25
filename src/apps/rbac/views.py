"""角色权限端点（spec §7）。

判据**逐个动作**不同（spec §5.2）：本页自己的权限点就是这几个，
所以本文件同时是"`available=True` 必须有消费方"这条不变式的第一个证据（有测试在核）。
"""
from django.db import transaction
from rest_framework import viewsets
from rest_framework.decorators import action

from apps.accounts.admin_mutations import admin_mutation_transaction
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.roles import TENANT_ADMIN
from apps.rbac.guards import ensure_not_last_admin_role, ensure_role_unused
from apps.rbac.models import Role
from apps.rbac.permissions import has_node
from apps.rbac.selectors import list_roles, node_tree, role_permission_rows
from apps.rbac.serializers import (
    RoleCopySerializer, RoleCreateSerializer, RoleUpdateSerializer,
)
from apps.rbac.services import (
    RoleServiceError, create_role, delete_role, rename_role,
    update_role_permissions, update_role_sales_agent_flag,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode

#: 每个动作对应的权限点（spec §5.2）。§7 的 8 个端点在这里全部有归属。
ACTION_NODES = {
    "create": "system.role_authority_edit",
    "update": "system.role_authority_edit",
    "partial_update": "system.role_authority_edit",
    "copy": "system.role_authority_edit",
    "destroy": "system.role_authority_delete",
    "permissions": "system.role_authority_view",
}
DEFAULT_NODE = "system.role_authority"


class RoleAuthorityViewSet(viewsets.GenericViewSet):
    """角色职位权限的全部端点。

    ⚠️ 基类用 `GenericViewSet` **不是** `EnvelopeModelViewSet`：本线的 `list` 要自定义
    （**不分页**，对标 `page:false`），继承 `ModelViewSet` 会把不需要的 mixin
    与路由动词一起带进来（账号线的同型处理见 `apps/accounts/views.py` 的模块 docstring）。
    """

    def get_permissions(self):
        node = ACTION_NODES.get(self.action, DEFAULT_NODE)
        return [has_node(node)()]

    def list(self, request):
        rows = [
            {
                "id": r.id,
                "name": r.name,
                "remark": r.remark,
                # 「可作为销售客服」（`D-0208`）：列表页**不消费**它，
                # 但读面与写面成对（`_detail` 有、列表没有会让"这个标记在哪能看见"变得不可判）。
                "can_be_sales_agent": r.can_be_sales_agent,
            }
            for r in list_roles()
        ]
        return api_response(rows)

    def retrieve(self, request, pk=None):
        return api_response(self._detail(self._get_object(pk)))

    def create(self, request):
        serializer = RoleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # ⚠️ R25：本方法原先**没有**事务边界（`create_role` 自己的 `@atomic` 只护住它内部两行），
        # 而留痕必须与业务写入**同事务**（spec §7.4）⇒ 这里补一层，把角色写与 `record(...)` 收在一起。
        # 拒绝路径（如重名）由 `_run` 抛 DRF `ValidationError` 冒出块 ⇒ 整块回滚（E-0022 家族）。
        with transaction.atomic():
            role = self._run(create_role, **serializer.validated_data)
            self._record_role(
                request, "system.role_create",
                target_id=role.pk, target_name=role.name,
            )
        return api_response(self._detail(role))

    def update(self, request, pk=None):
        """`PUT` = 全量替换；`PATCH` = **部分更新**（对齐账号线，独立评审裁决 ③）。

        ⚠️ **`partial=(request.method == "PATCH")` 不是可选项**：不传它时 `PATCH` 也按全量校验
        ⇒ 只发 `remark` 直接回 1000；更糟的是**另一条**路径——发了 `name` + `permission_codes`
        但漏了 `remark` 时，`remark=data.get("remark", "")` 会把备注**静默清成空**
        （账号线的 `_persist_account` 对同一形状的口径是"缺席的键一律保持库里的值"，
        连空 `{}` PATCH 都是纯 no-op）。
        ⚠️ **`permission_codes` 缺席时必须整个跳过**：`update_role_permissions` 是**整体替换**
        语义，拿"缺席"去补空集合等于清空授权，还会被 `_resolve_nodes` 拒成 1000
        ——"没传"与"传了空列表"是两回事，后者是非法值，仍要 1000。
        ⚠️ **`can_be_sales_agent` 同一个形状**（`D-0208`）：它也是
        「**显式给了才写**」（`"can_be_sales_agent" in data`），⛔ 不能写成
        `data.get("can_be_sales_agent", False)` —— 那会让一次与标记无关的 PATCH
        （例如只改备注）把已勾上的标记**静默清掉**（E-0021 那一族）。

        ⚠️ **R26：纯 no-op 不写留痕**（与账号域「同值零行」同口径）。`system.role_update` 的模板
        「编辑角色权限：{角色名}，原职位名称：{旧名}」断言了一次改名，对 `PATCH {}` 或"重发当前值"
        这种没发生任何事的请求写出「原职位名称：X」是假话。判据见块内的 `changed`。
        """
        role = self._get_object(pk)
        serializer = RoleUpdateSerializer(
            data=request.data, instance=role, partial=request.method == "PATCH"
        )
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        new_name = data.get("name", role.name)
        # 先读取留痕快照，涉及管理员职位时由守卫取锁。
        with admin_mutation_transaction(lock_immediately=False):
            previous_name = role.name
            previous_remark = role.remark
            previous_codes = set(role.permissions.values_list("code", flat=True))
            # 「可作为销售客服」标记的写前值（`D-0208`）。
            previous_can_be_sales_agent = role.can_be_sales_agent
            previous_dashboard_type = role.dashboard_type
            if new_name != role.name and TENANT_ADMIN in (role.name, new_name):
                self._run(ensure_not_last_admin_role, role, new_name=new_name)
            role = self._run(
                rename_role, role,
                name=new_name,
                remark=data.get("remark", role.remark),
            )
            if "permission_codes" in data:
                role = self._run(
                    update_role_permissions, role, permission_codes=data["permission_codes"]
                )
            # 「可作为销售客服」标记（`D-0208`）：**缺席即不动**，
            # 与 `permission_codes` 同一形状（理由见方法 docstring 的那个 ⚠️）。
            if "can_be_sales_agent" in data:
                role = self._run(
                    update_role_sales_agent_flag, role,
                    can_be_sales_agent=data["can_be_sales_agent"],
                )
            if "dashboard_type" in data:
                role.dashboard_type = data["dashboard_type"]
                role.save(update_fields=["dashboard_type"])
            # 所有字段都未变化时不写留痕。
            # 通例：缺席的键表示"保持库里的值"（PATCH 语义），只有**显式给了**才参与比较；
            # `permission_codes` 按**集合**比（`update_role_permissions` 的 `set()` 会去重，
            # 换个顺序/带重复 code 不是变化）。
            new_remark = data.get("remark", previous_remark)
            changed = (
                new_name != previous_name
                or new_remark != previous_remark
                or ("dashboard_type" in data and data["dashboard_type"] != previous_dashboard_type)
                or (
                    "permission_codes" in data
                    and set(data["permission_codes"]) != previous_codes
                )
                # 标记：显式给了、且与写前值不同才算变化（只勾一下这个标记**也是真的改了**
                # ⇒ 不能被 no-op 守卫吞掉；由 `test_operation_log_write_paths.py` 的用例钉住）。
                or (
                    "can_be_sales_agent" in data
                    and data["can_be_sales_agent"] != previous_can_be_sales_agent
                )
            )
            if changed:
                # 留痕在**同一个 `atomic()` 内**（spec §7.4）：本次改名 + 改权限 + 这一行要么全成、要么全回滚。
                # `target_name` = 新名、`from_value` = 写前旧名（spec §5.9：名字进 `target_name`、旧值进 `from_value`）。
                self._record_role(
                    request, "system.role_update",
                    target_id=role.pk, target_name=role.name, from_value=previous_name,
                )
        return api_response(self._detail(role))

    def partial_update(self, request, pk=None):
        """`ViewSet` 的 router 把 `patch` 映射到 `partial_update`，不是 `update`
        （`rest_framework/routers.py` 的 `Route.mapping`）。转调 `update`，不复制逻辑。"""
        return self.update(request, pk=pk)

    def destroy(self, request, pk=None):
        """删除角色、权限组和写审计共用一个事务。"""
        role = self._get_object(pk)
        # 删除会清空对象的主键，先留下审计所需的标识。
        target_id, target_name = role.pk, role.name
        with admin_mutation_transaction(lock_immediately=False):
            # 两道保护同时命中时，优先报告最后管理员的原因。
            self._run(ensure_not_last_admin_role, role)
            self._run(ensure_role_unused, role)
            delete_role(role)
            # `system.role_destroy` 的模板不含任何变量（spec §5.4），但目标列照填——
            # 不填的话「相关ID」列在这条动作上恒空，事后无法指认删的是哪个角色。
            self._record_role(
                request, "system.role_destroy",
                target_id=target_id, target_name=target_name,
            )
        return api_response(None)

    @action(detail=True, methods=["post"], url_path="copy")
    def copy(self, request, pk=None):
        source = self._get_object(pk)
        serializer = RoleCopySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # ⚠️ R25：本方法原先**没有**事务边界（同 `create`）⇒ 补一层，把新角色写与 `record(...)`
        # 收在一起。`source.permissions` 在块内读：复制的是**当前**授权，与写入落在同一事务快照里。
        with transaction.atomic():
            role = self._run(
                create_role,
                name=serializer.validated_data["name"],
                remark=serializer.validated_data.get("remark", ""),
                permission_codes=list(source.permissions.values_list("code", flat=True)),
                # 「可作为销售客服」标记**从源角色原样抄**（`D-0208`）——
                # 与"权限整份抄"同口径：复制出来的职位是新角色，但它"能不能当销售客服"
                # 这件事与源角色是同一件事。⚠️ 载荷里**没有**这个键（`RoleCopySerializer` 不收它，
                # 理由见那个类的 docstring）⇒ 这里的取值只可能是源角色的当前值，没有第二份判据。
                can_be_sales_agent=source.can_be_sales_agent,
                dashboard_type=source.dashboard_type,
            )
            # `system.role_copy` 的两个名字进 `from_value` / `to_value`（spec §5.9）：
            # 源角色在前、新角色在后，与模板「复制角色职位：{源} → {新}」同序。
            self._record_role(
                request, "system.role_copy",
                target_id=role.pk, target_name=role.name,
                from_value=source.name, to_value=role.name,
            )
        return api_response(self._detail(role))

    @action(detail=True, methods=["get"], url_path="permissions")
    def permissions(self, request, pk=None):
        return api_response(role_permission_rows(self._get_object(pk)))

    # ── 内部 ───────────────────────────────────────────────────────
    def _get_object(self, pk):
        """取不到时抛 DRF 的 `ValidationError`（→ 200 + `code 1000`）。

        ⚠️ **不能抛 `RoleServiceError`**：它不在 DRF 的异常链上，会变成 HTML 500。
        这里的形状与 `apps/accounts/serializers.py` 的既有口径一致
        （"非法值 → DRF `ValidationError` → 200 + `code 1000`"）。

        ⚠️ **非十进制的 pk 必须先挡**（E-0010 的"这类请求"判据，与账号线
        `apps/accounts/views.py::_lookup_id_error` 同口径、同一个理由）：DRF 的默认
        lookup 正则 `[^/.]+`（`rest_framework/routers.py`）**接受** `abc`，而
        `filter(pk="abc")` 会抛 `ValueError: Field 'id' expected a number but got 'abc'`
        ——未预期异常 ⇒ HTML 500（实测：修复前 `GET /api/role-authorities/abc/` 正是这条）。
        挡在这里 ⇒ **8 个动作共用同一个出口**，畸形 id 与"查不到"得到同一个 200 + 1000。
        """
        from rest_framework.exceptions import ValidationError

        if not str(pk).isdecimal():
            raise ValidationError({"role": "角色职位不存在"})
        role = Role.objects.filter(pk=pk).select_related("group").first()
        if role is None:
            raise ValidationError({"role": "角色职位不存在"})
        return role

    def _detail(self, role):
        return {
            "id": role.id,
            "name": role.name,
            "remark": role.remark,
            "permission_codes": sorted(role.permissions.values_list("code", flat=True)),
            # 「可作为销售客服」（`D-0208`）：编辑页靠它**回填那颗勾选框**
            # （没有它，编辑态每次保存都会把已勾上的标记按 payload 的默认值改掉）。
            "can_be_sales_agent": role.can_be_sales_agent,
            "dashboard_type": role.dashboard_type,
        }

    def _run(self, func, *args, **kwargs):
        """把领域层的**预期**失败翻成业务错误码（E-0010）；未预期异常照旧 500。"""
        try:
            return func(*args, **kwargs)
        except RoleServiceError as exc:
            from rest_framework.exceptions import ValidationError

            raise ValidationError({exc.code: exc.message}) from exc

    def _record_role(self, request, action, *, target_id="", target_name="", **columns):
        """角色域的统一埋点形状（spec §5.4 / §5.9）。

        - `log_type` 一律查登记表 `ACTION_LOG_TYPES`，**不写死数字**；
        - 操作人一律取**认证态**（`request.user.get_username()` / `request.user.pk`），
          从不取请求体里的身份（R20：请求体的身份可伪造，而留痕的全部价值就是「谁做的」为真）；
        - `target_id` 放角色 pk 的**数字**字符串（对标「相关ID」列），句子里要显示的名字走
          `target_name`（R11）——两者不能互兼。

        ⚠️ **调用方负责把它放进与业务写入同一个 `transaction.atomic()`**（spec §7.4）：
        本助手只组装列，不开事务。
        """
        return record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            target_kind="role",
            target_id=target_id,
            target_name=target_name,
            **columns,
        )


class PermissionNodeViewSet(viewsets.GenericViewSet):
    """只读的目录树。判据用 `system.role_authority`——它是编辑弹层的数据源。"""

    def get_permissions(self):
        return [has_node(DEFAULT_NODE)()]

    def list(self, request):
        return api_response(node_tree())
