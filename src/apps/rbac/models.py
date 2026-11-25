"""角色权限的载体（spec §3，决策 D1 / D2 / D4 / D5）。

**为什么角色要单独建表**：对标物的角色是**数据**（可新增 / 复制 / 改名 / 删除）。
账号仍挂在 `auth.Group` 上，`Role.group` 与同名 Group 一对一——这样
`apps/core/permissions.py::HasRole`、`apps/accounts/permissions.py::is_account_admin`
与账号表单的写库路径**一行不改**（spec §6.2）。
"""
from django.contrib.auth.models import Group
from django.db import models

from apps.rbac.choices import NodeKind


class PermissionNode(models.Model):
    """一个权限点。**两级**：`level=0` 是栏目，`level=1` 是功能点。"""

    # 对标 Menu_ID。**只用于溯源与审计，不参与任何判据**——对标记号变了不该影响我们的行为。
    benchmark_id = models.PositiveIntegerField("对标 Menu_ID", unique=True, null=True, blank=True)
    # 我们的稳定标识。**改名不改码**（spec §4.3）。
    code = models.CharField("权限码", max_length=64, unique=True)
    # 以下三个字段**原文照抄对标**：`note` 承载反义语义（"勾选后……就不能……"），
    # 改写等于改语义（E-0005 / E-0006 的同类风险）。
    name = models.CharField("权限名", max_length=50)
    note = models.CharField("权限说明", max_length=255)
    url = models.CharField("对标 URL", max_length=255, blank=True, default="")
    kind = models.CharField("形态", max_length=8, choices=NodeKind.CHOICES)
    level = models.SmallIntegerField("层级")          # 0 栏目 / 1 功能点
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    order_by = models.IntegerField("排序", default=0)
    # ⚠️ 合取语义：True ⇔ 我们已有该功能 **且** 该点的判据已经接上（spec §4.4）。
    available = models.BooleanField("已实现", default=False)

    class Meta:
        # 按对标 Menu_ID 排序 = 对标页面的渲染顺序（§3 的取数实测）。
        ordering = ["benchmark_id"]

    def __str__(self):
        return self.code


class Role(models.Model):
    """角色职位。`name` / `remark` 的 max_length **照抄对标的表单 maxlength**（spec §3.3）。"""

    name = models.CharField("角色职位名称", max_length=5, unique=True)
    remark = models.CharField("角色说明", max_length=50, blank=True, default="")
    #: 「这个职位的人**可以作为销售客服**」——录单页「接单销售客服」那一格的候选口径
    #: （`apps/business/selectors.py::kefu_options`）。业务方裁定：候选按**角色标记**取，
    #: 不再写死两个角色名，租户自建的角色在角色编辑页自己勾。
    #: ⚠️ **默认 `False`**：靠种子 / 数据迁移把内置「客服」「客服主管」置 `True`
    #: ⇒ 存量租户的候选名单与改动前**逐条相同**（不静默放大、也不静默缩小）。
    can_be_sales_agent = models.BooleanField("可作为销售客服", default=False)
    dashboard_type = models.CharField(
        "首页看板", max_length=16, blank=True, default="", db_default="",
        choices=[("", "默认"), ("worker", "打手"), ("work", "工作")],
    )
    group = models.OneToOneField(Group, on_delete=models.CASCADE, related_name="role")
    permissions = models.ManyToManyField(PermissionNode, related_name="roles")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name
