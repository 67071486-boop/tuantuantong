"""读查询（spec §7）。**所有查询都在当前租户 schema 内**（AGENTS.md §4）。"""
from apps.rbac.models import PermissionNode, Role


def list_roles():
    """列表用。**不分页**（对标 `page:false`），所以这里也不接分页参数。

    ⚠️ 2026-09-23（本线 D4）：原来的 `annotate(permission_count=Count("permissions"))` 已删——
    它对每个角色多一次 JOIN 计数，而**前端从来没有消费过它**（实测 `grep -rn permission_count
    src/frontend/src` 只有类型定义一处）。要恢复显示必须同时改 spec。
    """
    return Role.objects.order_by("id")


def role_names():
    """当前租户的**全部角色名**（内置 + 自建），按 `Role.id` 升序。

    ⚠️ 读的是**当前 schema** 的 `Role` ⇒ 租户隔离天然成立（AGENTS.md §4）：
    本函数**不接受任何租户参数**，租户上下文只来自连接。
    顺序取 `Role.Meta.ordering`（`["id"]`）；内置角色由种子先建 ⇒ 排在自建之前。
    ⚠️ **别在这里写死内置角色数**（原文写「内置 5 + 自建」）：线 D（2026-10-12）加了
    内置「人事」之后那个数就过期了。它以 `rbac.seed.BUILTIN_ROLE_MODULES` 为准。
    """
    return list(Role.objects.order_by("id").values_list("name", flat=True))


def role_permission_rows(role):
    """权限清单预览的 5 列（对标 `GetRoleAuthorityViewList` 的实测形状）。

    ⚠️ 顶级节点的"所属栏目"是字符串 **`无`**，不是 `null`（暂停 spec §3.6 实测）。
    """
    rows = []
    for node in role.permissions.select_related("parent").order_by("benchmark_id"):
        rows.append({
            "name": node.name,
            "level": node.level,
            "parent_name": node.parent.name if node.parent else "无",
            "note": node.note,
            "order_by": node.order_by,
        })
    return rows


def node_tree():
    """编辑权限弹层的两级树。**只回消费方用到的字段**（spec §11-5）：对标还回了
    `CreateTime` / `IsAllowDel` / `FatherMenu_ID` 等，页面一个都没用过，我们不暴露。

    栏目顺序不另写 `order_by`：`PermissionNode.Meta.ordering = ["benchmark_id"]`
    就是对标 `GetUserRole` 的渲染顺序（= 对标 Menu_ID 升序）。
    两级结构是模型上的事实（`level` 0/1），**不写递归**——多一级就要多一次查询，
    而数据里没有三级（YAGNI）。
    """
    modules = PermissionNode.objects.filter(level=0).prefetch_related("children")
    return [
        {
            "code": m.code, "name": m.name, "note": m.note, "level": m.level,
            "available": m.available,
            "children": [
                {"code": c.code, "name": c.name, "note": c.note, "level": c.level,
                 "available": c.available}
                for c in m.children.all()
            ],
        }
        for m in modules
    ]
