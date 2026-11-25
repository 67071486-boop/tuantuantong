"""角色与同名 `auth.Group` 的同步（spec §6.2）。

**为什么必须有这一层**：`Role` 与 `Group` 是两张表，而账号挂在 `Group` 上。
两者漂移的后果是"界面上改了角色，人的权限没变"。所有写路径只走本模块的四个函数，
每个都在**一个事务**里同时写两张表——这是 D2 的代价栏要求的结构保证，
不能靠"记得两边都改"的纪律。
"""
from django.contrib.auth.models import Group
from django.db import transaction

from apps.rbac.models import PermissionNode, Role


class RoleServiceError(Exception):
    """领域层的**预期**失败（AGENTS.md §4 / E-0010）。

    视图捕获它并翻译成业务错误码；**未预期**异常仍应响亮地 500。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_node_codes(permission_codes, *, allow_empty, empty_message="请至少选择一个权限"):
    """把 code 列表解析成节点。**「code 必须存在」这条规则只有这一份实现**。

    两条路只差一个 `allow_empty`（spec §17.5 / §17.7 的裁定）：

    - **角色路**（`create_role` / `update_role_permissions`）⇒ `allow_empty=False`：
      空集合一律拒绝，理由见下（**别为了省事把它放宽**：那会放开一个有其自身安全论证的既有约束）。
    - **开放平台应用路**（`apps.openapi.api.app_views`）⇒ `allow_empty=True`：
      空集合**是合法状态**（= 什么都不授权 ⇒ 调什么都 403，fail-closed）。
      应用侧**没有「全选」这个语义**，所以不会出现角色路那条「漏传 ⇒ 静默变成超级权限」。

    ⚠️ **判据只此一份**（E-0021 / spec §17.7）：⛔ 别在 `apps/openapi/**` 里再写一遍
    「遍历 code、收集未知项」的逻辑；那句「权限点不存在：X」的文案也只有这里一份。
    """
    codes = list(permission_codes or [])
    if not codes:
        if allow_empty:
            return []  # 应用路：空集 = 什么都不授权（fail-closed），⛔ 不是「全选」
        raise RoleServiceError("permission_codes_empty", empty_message)
    nodes = list(PermissionNode.objects.filter(code__in=codes))
    unknown = sorted(set(codes) - {n.code for n in nodes})
    if unknown:
        raise RoleServiceError(
            "permission_code_unknown", f"权限点不存在：{'、'.join(unknown)}"
        )
    return nodes


def _resolve_nodes(permission_codes):
    """角色路的薄包装：**两个既有调用点（`create_role` / `update_role_permissions`）一字不改**。

    空集合用 `if not codes:` 判——`None` 与 `[]` 是同一个失败面，
    不做"没传就当全选"的兜底（那会让一次漏传静默变成超级权限）。
    """
    return resolve_node_codes(permission_codes, allow_empty=False)


def _raise_name_taken(name):
    """`Role.name` 与 `Group.name` 都是唯一键，**两张表各自都能撞**。

    对用户而言这是**同一个**概念（"这个名字不可用"），所以共用一个 code / 文案：
    两个 code 会把"两张表"的实现细节漏进接口契约，逼 Task 7 的调用方为同一个字段错误分两支。
    """
    raise RoleServiceError("role_name_taken", f"角色职位名称已存在：{name}")


@transaction.atomic
def create_role(*, name, remark, permission_codes, can_be_sales_agent=False, dashboard_type=""):
    """建角色 + 同名 Group。同名 Group 已存在时**复用**它（对标不禁止重名 Group）。

    ⚠️ `can_be_sales_agent` 的默认值 `False` 是**承重**的（业务方 2026-10-08 裁定，决策 `D-0208`）：
    它有两个来源——**新建租户**的种子（`seed.py` 显式传 `True` 给「客服」「客服主管」）与
    **管理员的角色编辑页**（本线新增的那颗勾选框）。⛔ 别把它改成 `True`：那会让**每个**角色
    （含管理员临时建的）都进销售客服候选，`apps/business/selectors.py::kefu_options`
    的「按标记取」就退化成「按有没有职位取」。

    ⚠️ `Role` 重名与 `Group` 重名是**两回事**，判据不同：
    - 同名 `Group` ⇒ 复用（`Group.name` 在账号线里还有别的用途，可能是先建组后配角色）；
    - 同名 `Role` ⇒ **预期失败**，抛 `RoleServiceError`。
      不先查的话 `Role.objects.create` 会撞 `name` 的唯一约束抛 `IntegrityError`，
      在视图层变成 HTML 500；而这是用户输入造成的可预期失败（E-0010），
      必须给业务码。Task 7 的「复制」动作就走这条路径。

    这里**只查 `Role` 是对的**，不是漏查 `Group`：同名的裸 Group 会被 `get_or_create`
    **采纳**（不给它建第二行，也就不会撞 `auth_group_name_key`）——这是特性，由
    `test_create_role_reuses_an_existing_same_named_group` 钉住；`rename_role` 的判据则相反，
    见那里的说明。两处校验都在**写第一行之前**完成，所以失败路径根本不会产生 `Role` / `Group` 行；
    即便将来有人在写之后加逻辑，`transaction.atomic` 也保证异常冒出时整块回滚。
    """
    nodes = _resolve_nodes(permission_codes)
    if Role.objects.filter(name=name).exists():
        _raise_name_taken(name)
    group, _ = Group.objects.get_or_create(name=name)
    role = Role.objects.create(
        name=name,
        remark=remark or "",
        group=group,
        can_be_sales_agent=can_be_sales_agent,
        dashboard_type=dashboard_type,
    )
    role.permissions.set(nodes)
    return role


@transaction.atomic
def rename_role(role, *, name, remark):
    """改名 = 角色与 Group **一起**改名。账号挂的是 Group 行，所以成员不丢。

    顺序是**先 Group 后 Role**：两者在一个事务里，顺序不改终态；但反过来先写
    `role.name` 而 `group.save()` 失败时，库内会短暂存在"角色名与组名不一致"的中间态，
    照 Group→Role 写则中间态方向与调用方看到的名字一致（更不容易被误读）。

    ⚠️ 改名前要查**两张表**（各自排除自己），不能只查 `Role`——这与 `create_role` 的判据不同，
    因为改名**不能"采纳"别人已有的组**：把 `role.group` 指向已存在的组、再删原组，
    会连带丢掉原组成员的职位（账号挂的是组）。所以撞上就拒绝，并**在写第一行之前**拒绝。
    两条现实路径各自会造成一种 500：
    - **裸 `Group`**（只有组、没有 `Role`）：`seed.py` 先建组再建角色、
      `apps/platform/provisioning.py` 直接灌 `DEFAULT_ROLES`；这些组在角色列表里**看不见**，
      管理员会把它们的名字当"未占用" ⇒ `group.save()` 撞 `auth_group_name_key`。
    - **`Role` 与 `Group` 不同名的行**：`seed_builtin_roles` 的 `get_or_create` 与直接写库都可能
      留下这种行（`Role.name` 与 `Group.name` 之间**没有**数据库级约束）⇒ 只查 `Group` 会漏，
      `group.save()` 先成功、`role.save()` 再撞 `rbac_role_name_key`。
    """
    old_name = role.name
    if name != old_name:
        taken = (
            Role.objects.filter(name=name).exclude(pk=role.pk).exists()
            or Group.objects.filter(name=name).exclude(pk=role.group_id).exists()
        )
        if taken:
            _raise_name_taken(name)
        group = role.group
        group.name = name
        group.save(update_fields=["name"])
        role.name = name
    role.remark = remark or ""
    role.save(update_fields=["name", "remark"])
    return role


@transaction.atomic
def update_role_permissions(role, *, permission_codes):
    """**整体替换**权限集（对标是勾选框提交全量，不是增量）。

    先解析再写：`_resolve_nodes` 抛错时一行都不碰，已有分配保持原样——
    半套权限比旧权限更危险（界面上看着成功了，实际少给/多给了点）。
    """
    nodes = _resolve_nodes(permission_codes)
    role.permissions.set(nodes)
    return role


@transaction.atomic
def update_role_sales_agent_flag(role, *, can_be_sales_agent):
    """「可作为销售客服」标记（`Role.can_be_sales_agent`）的**唯一**写口（本线 2026-10-08，`D-0208`）。

    **为什么单开一个函数，而不是塞进 `rename_role`**：`rename_role` 的契约是「角色改名 / 改说明」，
    它的两个入参都是**必填** keyword-only；把标记塞进去要么给它一个 `None` 哨兵（把「不改」与
    「改成 False」两种意思压进一个参数），要么让所有调用点都必须表态——前者是 E-0021 那一族
    （把「没传」当成一个值），后者会改掉一个**与标记无关**的既有函数的签名。
    这里照**同一个文件里既有的分工**办：`update_role_permissions` 管权限、本函数管标记，
    「哪些键真的出现了」由视图判断（同 `permission_codes` 的 `in data` 判据）。

    ⚠️ 只写这一列（`update_fields`）：本函数**不碰** `name` / `remark` / 权限集
    ⇒ 管理员改过的授权不会被它回滚（与 `rename_role` 那条纪律同口径）。
    """
    role.can_be_sales_agent = can_be_sales_agent
    role.save(update_fields=["can_be_sales_agent"])
    return role


@transaction.atomic
def delete_role(role):
    """删角色 = 删**同名 Group**。调用方负责先过删除保护（Task 8）。

    `Role.group` 的外键在 `Role` 一侧，删 `Role` **不会**连带删 `Group`；
    而账号挂在 `Group` 上，只删 `Role` 会留下一个仍然授权、但在界面上已消失的组。
    所以两张表都要删，且必须同一个事务。
    """
    group = role.group
    role.delete()
    group.delete()
