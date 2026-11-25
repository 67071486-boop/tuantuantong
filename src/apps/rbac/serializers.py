"""角色接口的请求校验入口（AGENTS.md §4 / E-0010）。

判据是「**这类请求**还会不会 500」：`name` 超长、`permission_codes` 里塞未知 code、
`remark` 超 50 —— 全部要在这里变成业务错误码，而不是落到数据库上抛 `DataError`。
"""
from django.contrib.auth.models import Group
from rest_framework import serializers

from apps.rbac.models import PermissionNode, Role

MAX_NAME = 5
MAX_REMARK = 50


class _NameMixin(serializers.Serializer):
    name = serializers.CharField(max_length=MAX_NAME, allow_blank=False)
    # ⚠️ `default=""` 是**必需的**，不是装饰：服务层 `create_role(*, name, remark, permission_codes)`
    # 的 `remark` 是必填 keyword-only，而 `views.create` 写的是 `**serializer.validated_data`。
    # DRF 对「`required=False` 且没有 `default=`」的缺席字段直接抛 `SkipField`
    # （`rest_framework/fields.py::validate_empty_values` 的 `data is empty` 分支），
    # 于是 `validated_data` 里**根本没有 `remark` 键** ⇒ `TypeError` ⇒ **HTML 500**
    # （实测：`POST {"name","permission_codes"}` 不带 remark 正是这条）。
    # 与 `apps/accounts/serializers.py` 的同名字段同形状。
    # ⚠️ 它**不会**破坏 PATCH 的"缺席即保持"：同一个函数在 `self.root.partial` 为真时
    # **先**抛 `SkipField`（同文件 501-506 行，`default` 还没轮到），所以 `partial=True` 下
    # 缺 `remark` 仍然是"不在 validated_data 里"，由视图用库里的值补。
    remark = serializers.CharField(
        max_length=MAX_REMARK, required=False, allow_blank=True, default=""
    )

    def validate_name(self, value):
        """名字占用检查——**必须与服务层逐操作对齐**（二次裁决）。

        ⚠️ **两次裁决，第一次是错的，别改回去**：
        - 一版（错误）：一律同时查 `Role` 与 `Group`。它会把"建一个与裸 Group 同名的角色"误判成非法——
          而服务层 `create_role` 用的是 `Group.objects.get_or_create`，**会收编那个裸 Group**，
          这是**有意的**（`seed_builtin_roles` 先建 Group 再建 Role，靠的就是它）。
        - 二版（本版，正确）：对齐"这一层能不能复用"的语义——
          - **新增**：服务层能收编裸 Group ⇒ 只查 `Role` 维度（重名 `Role` 仍要拒）；
          - **改名**：不能收编（把 `role.group` 指过去再删旧 Group，会**静默丢掉那些账号的职位**）
            ⇒ `Role`（排除自己）与 `Group`（排除自己的 `group_id`）**两个维度都查**。
        """
        roles = Role.objects.filter(name=value)
        if self.instance is None:
            if roles.exists():
                raise serializers.ValidationError("该角色职位名称已存在")
            return value
        groups = Group.objects.filter(name=value)
        roles = roles.exclude(pk=self.instance.pk)
        groups = groups.exclude(pk=self.instance.group_id)
        if roles.exists() or groups.exists():
            raise serializers.ValidationError("该角色职位名称已存在")
        return value


class RoleCreateSerializer(_NameMixin):
    dashboard_type = serializers.ChoiceField(
        choices=("", "worker", "work"), required=False, default="", allow_blank=True
    )
    permission_codes = serializers.ListField(
        child=serializers.CharField(max_length=64), allow_empty=False
    )
    #: 「可作为销售客服」（`Role.can_be_sales_agent`，业务方裁定；决策 `D-0208`）。
    #:
    #: ⚠️ `required=False, default=False` 的两半**各有用途**，别删任何一半：
    #: - `default=False` 是**新建**那条路必需的：`views.create` 写的是 `**serializer.validated_data`，
    #: 而 `create_role` 的 `can_be_sales_agent` 有默认值 ⇒ 少了这一半虽不至于 500，
    #: 但「显式传 `false`」与「没传」会在序列化器这一层就被合并（`remark` 那条注释记的是同族的 500 形态）；
    #: - `required=False` 让 `PATCH` 保持"缺席即不动"：DRF 在 `partial=True` 下**先**抛 `SkipField`
    #: （`rest_framework/fields.py::validate_empty_values`），`default` 还没轮到
    #: ⇒ 视图能靠 `"can_be_sales_agent" in data` 区分「没传」与「显式传 False」。
    #: ⚠️ **`PUT`（全量替换）下缺这个键 ⇒ 落 `False`**，与 `remark` 缺键落 `""` 是同一个既有口径
    #: （前端一律 `PATCH`；由 `tests/test_rbac_api.py` 的用例钉住，⛔ 不是意外）。
    can_be_sales_agent = serializers.BooleanField(required=False, default=False)


class RoleUpdateSerializer(_NameMixin):
    dashboard_type = serializers.ChoiceField(
        choices=("", "worker", "work"), required=False, default="", allow_blank=True
    )
    permission_codes = serializers.ListField(
        child=serializers.CharField(max_length=64), allow_empty=False
    )
    #: 同 `RoleCreateSerializer` 的说明（两个序列化器**各自声明**：本仓不用 Mixin 藏字段，
    #: 否则「哪条写入路带哪个字段」在文件里读不出来）。
    can_be_sales_agent = serializers.BooleanField(required=False, default=False)


class RoleCopySerializer(_NameMixin):
    """复制弹层。

    ⚠️ **刻意偏离对标**：对标复制弹层的 `maxlength` 是 **6**，而模型/编辑表单的上限是 **5**
    （它自己的口径不一致，提示还写"2-6个字符"）。我们**取 5**——照抄 6 会让 6 个字符的名字
    必然写不进数据库，是一个注定失败的输入路径。已登记进 spec §11。

    ⚠️ **`can_be_sales_agent` 刻意**不在**本序列化器上**（本线裁定）：复制动作的语义
    是「新角色 = 源角色的整份拷贝」，权限从**源角色**整份抄（`views.copy` 读
    `source.permissions`，见本文件顶部的载荷注释），标记按**同一口径**从源角色抄
    （`views.copy` 传 `can_be_sales_agent=source.can_be_sales_agent`）。
    若在这里收一个同名字段，就会出现「载荷说了算还是源角色说了算」的第二份判据，
    且它**只能**二选一 ⇒ 另一侧被静默忽略（`AGENTS.md` §5-E 的静默丢字段一族）。
    先例：`permission_codes` 同样是**不收**的（复制载荷只有 `{name, remark}`）。
    """


class NodeTreeQuerySerializer(serializers.Serializer):
    """目录树接口的查询参数（当前无参数，占位以便将来加筛选时不必改签名）。"""

    available = serializers.BooleanField(required=False)
