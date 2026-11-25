"""租户内的职位名（= django.contrib.auth 的 Group 名）词表。 **本模块只定义 Group 名常量**：`SERVICE` / `SUPERVISOR` / `WORKER` / `SYSTEM_ADMIN` / `DEVELOPER` / `DEFAULT_ROLES` / `TENANT_ADMIN`。租户供给时按 `DEFAULT_ROLES` 种子 Group（`apps/platform/provisioning.py`）；
`apps/core/permissions.py` 的 `HasRole` 按这些词取值。 **2026-09-20 换词**（spec §6.3 / D-0030）：从我们自造的五个改为一对一对标物的五个。 **业务代码不该按角色名分支**（角色是数据）。 📌 **2026-09-23 单轴化**：状态机的授权轴已改为 **RBAC 节点 code** （spec `2026-09-23-rbac-single-axis-design.md`；判据出口 `apps/rbac/permissions.py::nodes_of`）。原先那一层「当前用户 → 状态机 `Role` 常量」的
适配层（`ALL_ROLES` / `GROUP_TO_ROLES` / `roles_of`）随之**整个删除**—— 自定义职位与内置职位从此走同一套节点判据，不再经本模块转译。 `order_status.Role` 这个状态机角色词表同一批删除。 ⚠️ `TENANT_ADMIN`（= `SYSTEM_ADMIN` = `"系统管理员"`）是 `is_account_admin` 的承载（spec D8 / RBAC spec §6.3），改词要连带改账号线的判据注释与测试。 ⚠️ 本模块的常量是 **Group 名**，与 `apps/rbac/models.py::Role`（RBAC 角色行，读 `Role.permissions` 里的节点 code）**不是一回事**，别混。"""

SERVICE = "客服"
SUPERVISOR = "客服主管"
WORKER = "代练员"
SYSTEM_ADMIN = "系统管理员"
DEVELOPER = "系统开发员"

DEFAULT_ROLES = (SERVICE, SUPERVISOR, WORKER, SYSTEM_ADMIN, DEVELOPER)

#: 具备"管理本租户"含义的角色名。**不是** `is_superuser` 的别名：
#: `is_account_admin = is_superuser OR 持有本 Group`（账号线 spec §7.6）。
TENANT_ADMIN = SYSTEM_ADMIN
