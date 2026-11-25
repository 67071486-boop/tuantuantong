"""P2 接口测试的**真实登录**夹具（测试专用，**不是**生产代码）。

**为什么需要它**：fail-closed 的默认权限类（spec D11）让 P2 的业务端点（`apps/business/**`
的 5 个 ViewSet，全部没有声明 `permission_classes`）从"匿名可访问"变成"需登录"。
`test_config_api.py` / `test_order_api.py` / `test_no_route_500.py` 三处都要在发请求前
准备一个登录态。同一个流程抄三份一定会漂移（改了一处漏了另两处），所以收口在这里。

**为什么走真实登录端点**（`POST /api/auth/login`，Task 5 的产物）而不是别的捷径：

- `force_login()` **不可用**：它产出的会话里**没有 `identity_plane`**，会被
  `TenantSessionGuardMiddleware` 与 `TenantBoundAuthentication` 按 fail-closed 不变式拒掉
  （spec §5.1-3 有专门的红线测试钉这一点）——用它的结果是每个业务请求都 401。
- `materialize_session()`（`tests/test_p5_credential_plane.py`）是**红线测试的攻击构造物**
  （手工拼一个声明了别的平面的会话），拿它当日常夹具会把"凭据层被绕过"的形状带进普通接口
  测试。真实登录是唯一同时满足"会话合法"与"测的就是真实路径"的方式。
- 用户由本函数**当场创建**：`seed_tenant` 建出来的 `admin` 是 `set_unusable_password()`
  （D12），拿它登不进去。

**为什么这个 client 刻意不带 `enforce_csrf_checks=True`**（与 P5 自己的 CSRF 测试相反）：
Django 测试 `Client` 默认 `enforce_csrf_checks=False`，只把 `request._dont_enforce_csrf_checks`
置 True（`django/test/client.py:183`），于是 `LoginView` 上的 `csrf_protect`
（即 `CsrfViewMiddleware.process_view`，读同一个 flag，`django/middleware/csrf.py:427`）
在这一路不生效。**这是有意为之，不是疏漏**：本夹具的用途只是给业务接口测试准备登录态，
而这些测试**不断言任何 CSRF 行为**——它们的断言全部关于业务信封与业务码，CSRF 只会是噪音。
CSRF 的判据（裸 POST 必须确定性 403、带合法 token 才能登录、跨站登出不得生效）由 P5 自己的
测试用 `enforce_csrf_checks=True` 的 client 独立钉住（`test_p5_envelope_csrf.py` 的
`LoginEndpointTests`），那里的口径**不因为本夹具而改变**。
"""
from django.contrib.auth import get_user_model
from django.test import Client

# P2 接口测试的登录账号。同一个测试方法内只会建一次（`TestCase` 每个测试回滚），
# 所以跨测试重名不会撞唯一约束。
P2_TEST_USERNAME = "p2-api-user"
P2_TEST_PASSWORD = "pw-p2-api"


class _AllGames:
    """`make_logged_in_client(games=…)` **不传**时的哨兵（不是 `None`、不是 `[]`）。

    为什么需要它（而不是用 `"all"` 字符串）：`None` 与 `[]` 已经是**两个有语义的取值**
    （分别是「没有档案」与「零游戏」），第三种「不传」必须有第三个可区分的值——
    字符串哨兵会和「游戏 code 就叫 all」混，所以用这个私有类型。`repr` 写成人话，
    失败信息里能一眼看出是默认分支。
    """

    def __repr__(self):  # pragma: no cover - 只在断言/日志里出现
        return "<全部游戏（默认）>"


#: 默认给「当前租户的全部游戏」的哨兵（模块私有；调用方不需要它）。
_ALL_GAMES = _AllGames()


def make_logged_in_client(
    domain="tenant.test.com",
    *,
    username=P2_TEST_USERNAME,
    password=P2_TEST_PASSWORD,
    groups=(),
    nodes=(),
    is_superuser=False,
    games=_ALL_GAMES,
):
    """在当前租户 schema 里建一个可用用户，走真实登录端点，返回**已登录**的 client。

    返回的 client 与 `Client(SERVER_NAME=domain)` 形状一致，调用方直接替换即可。
    登录失败就抛 `AssertionError`（夹具坏了必须响亮，不能静默返回匿名 client ——
    那会让每个业务测试以"401 → data 是 None"的形式炸在别处，归因困难）。

    ⚠️ **`nodes` 默认空 ⇒ 默认不挂任何节点**：`groups` 只建 `Group`，
    而节点判据读的是 `Group → Role → permissions`。默认空集意味着**调用方必须自己声明
    判据**——没声明就打受 RBAC 节点门保护的端点会拿到 403（fail-closed，不是放行）。
    既有调用方一个都不用改（默认值让它们的行为逐字不变）；需要放行的测试显式写
    `nodes=("order.list",)`。

    ⚠️ **`is_superuser=True` 会顺手把账号加进「系统管理员」职位**（2026-09-23 判据统一，
    本夹具同步）：本夹具模拟的是 `seed_tenant` 建的那个管理员——它**既**是超管**也**在
    「系统管理员」组里（`apps/platform/provisioning.py` 的 `admin.groups.add(...)`）。
    **只置 `is_superuser` 而不给组，在新判据下不再是管理员**
    （`apps/core/permissions.py::is_tenant_admin` 的租户平面只认职位）——
    27 个既有调用点要的是"一个管理员"，不是"测超管短路"，所以这里把组补上。
    专门测「超管短路已失效」的用例在 `tests/test_admin_predicate_unified.py`。

    ⚠️ **`games` 的四条语义（2026-09-23 业务方裁定，逐条写死，别自行改动）**：

    1. **不传（默认）** ⇒ 给这个账号建/取 `StaffProfile`，并把**当前租户的全部游戏**
       都勾上（`Game.objects.all()`）。这是**新默认**——绝大多数调用点只是要"发个请求"、
       并不关心游戏范围，所以它们**不需要改**；本仓为此把默认从"不给游戏"翻了过来
       （背景：`docs/plans/2026-09-23-shared-fixture-all-games.md`）。
    2. **`games=None`** ⇒ **不建 `StaffProfile`**（「没有档案」那条形状）。要给「没有档案
       ⇒ `games_of` 空 ⇒ 什么都看不到」的用例用这一支。
    3. **`games=[]`** ⇒ 建档案、**一款游戏都不勾**（「有档案但零游戏」）。
    4. **`games=[g1, …]`** ⇒ 只勾这几款（**并集**语义：不抹掉已勾的）。部分授权的用例
       （「只有 A 权限 ⇒ 看不到 B 的行」）必须显式传这一支——**默认全量会静默把这类
       钉子变成恒真**，那是本夹具最危险的失效形态。

    ⛔ **要测「没有权限」就显式 opt-out**（上面 2 / 3 / 4）；⛔ 别把默认值当成"已经声明了
    范围"。既有的 `grant_games(user, *games)` 调用**一处都不要删**：它们是显式声明，
    其中不少是**部分授权**，删了会改语义（并集语义下与默认共存是安全的）。

    ⚠️ **平台平面（`public`）上默认**不建档案**（实测踩过，不是防御性写法）**：
    `StaffProfile` 属 `TENANT_APPS` ⇒ `accounts_staffprofile` **只在租户 schema 里**；
    平台平面上建档案会抛
    `ProgrammingError: relation "accounts_staffprofile" does not exist`。
    所以默认这一支在 `on_public_plane()` 上**整个跳过**（平台账号在本项目口径里没有游戏
    权限，见 `apps/business/game_scope.py::games_of` 的平台早返回）。
    ⛔ 显式传 `games=None` 之外的值（`[]` / `[g…]`）在平台平面上没有意义，
    本夹具**响亮地抛 `AssertionError`**——静默忽略会让调用方以为自己授权成功了。
    """
    # ⚠️ 当前 schema 必须是该用户所在 schema（E-0013）：否则 `login()` 里
    # `update_last_login` 的 `save(update_fields=["last_login"])` 会更新 0 行并抛
    # `DatabaseError`。调用点都在 `TenantTestCase.setUp`（连接已 `set_tenant`），
    # 与同处创建的 `Game` / `Order` 用的是同一个 schema。
    from django.contrib.auth.models import Group

    from apps.business.roles import TENANT_ADMIN

    user_model = get_user_model()
    if is_superuser:
        user = user_model.objects.create_superuser(username=username, password=password)
        # 「系统管理员」Group 由 `get_or_create` 建（`TenantTestCase` 不跑 `seed_tenant`，
        # 这个组不一定存在）。⚠️ 这里**不建同名 `Role`**：管理职位的人拿到的是
        # `has_node_now` 的管理员短路，不依赖目录；需要具体节点的测试仍要显式传 `nodes=`。
        group, _ = Group.objects.get_or_create(name=TENANT_ADMIN)
        user.groups.add(group)
    else:
        user = user_model.objects.create_user(username=username, password=password)
    if groups:
        for name in groups:
            group, _ = Group.objects.get_or_create(name=name)
            user.groups.add(group)

    _apply_games(user, games)

    if nodes:
        _grant_nodes(user, nodes)

    client = Client(SERVER_NAME=domain)
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
        content_type="application/json",
    )
    if response.status_code != 200:
        raise AssertionError(
            f"登录夹具失败：HTTP {response.status_code}，body={response.content[:200]!r}"
        )
    return client


def _apply_games(user, games):
    """`games` 四值语义的**唯一实现**（见 `make_logged_in_client` 的 docstring）。

    单独抽出来只为一件事：让"哪一支建档案、哪一支不建"在一个地方读完。
    """
    from apps.core.plane import on_public_plane

    if games is None:
        # 语义 2：不建档案。
        return
    if on_public_plane():
        # 平台平面没有 `accounts_staffprofile` 表：默认这一支跳过；显式要授权则响亮地报。
        if games is not _ALL_GAMES:
            raise AssertionError(
                "平台平面（public）没有 accounts_staffprofile 表，"
                "make_logged_in_client(..., games=…) 在平台平面上没有意义；"
                "默认之外只接受 games=None（其语义正是「不建档案」）。"
            )
        return

    from apps.accounts.models import StaffProfile
    from apps.business.models import Game

    profile, _ = StaffProfile.objects.get_or_create(user=user)
    if games is _ALL_GAMES:
        # 语义 1：当前租户的全部游戏。
        profile.games.add(*Game.objects.all())
    elif games:
        # 语义 4：只勾这几款（`add` 是并集，不抹已勾的）。
        profile.games.add(*games)
    # 语义 3（`games=[]`）：档案已建，什么都不勾。


def _grant_nodes(user, codes):
    """`grant_nodes` 的**唯一实现**（Task 1 已交付公开入口；Task 2 只是把它抽出来）。

    夹具（`make_logged_in_client`）与测试都要挂节点，两处各写一份必然漂移
    （踩过的同型：P5 的登录流程三处重复 → 收口成 `make_logged_in_client`）。

    ⚠️ **本函数自己 `seed_catalog()`**（幂等）：不这么做的话，忘了先灌目录的调用方会
    静默拿到"一个节点都没挂上"的组 —— 症状是 403，看起来像判据写错了。
    ⚠️ `TenantTestCase` **不会** `seed_tenant`（它只建空 schema），所以目录必须由调用方
    或本函数灌；本函数选后者。
    ⚠️ 组没有同名 `Role` 时**建一个空的**（`Role.name` 的 `max_length=5` ⇒ 组名超 5 字会
    `DataError`；测试里用的组名都是 ≤5 字的内置五职位，所以这里不必截断）。
    """
    from apps.rbac.models import PermissionNode, Role
    from apps.rbac.seed import seed_catalog

    seed_catalog()
    nodes = list(PermissionNode.objects.filter(code__in=codes))
    for name in list(user.groups.values_list("name", flat=True)):
        group = user.groups.get(name=name)
        role, _ = Role.objects.get_or_create(name=name, defaults={"group": group})
        role.permissions.add(*nodes)


def grant_nodes(user, *codes):
    """把节点 code 挂到该用户当前所在的所有 Group 对应的 `Role` 上（幂等）。

    ⚠️ **判据读的是 `user.groups → Role → permissions`**（`apps/rbac/permissions.py`），
    只挂 `Group` 不给节点**不会**放行——所以已有测试里那些 `groups=("客服",)` 的夹具
    在本线之后必须显式调用本函数（或给 `make_logged_in_client` 传 `nodes=`），
    否则会以 403 的形式红。

    实现与夹具共用 `_grant_nodes`；`seed_catalog()` / 组名长度 / 空 `Role` 的处理
    见那里的 docstring。
    """
    _grant_nodes(user, codes)


def grant_games(user, *games):
    """给账号授**游戏权限**（`StaffProfile.games`），缺档案就补一个（幂等）。

    2026-09-23「订单域按游戏权限收窄」之后，读路径过
    `scope_queryset(..., scope_filter=game_scope(...))`、写路径过 `assert_game_allowed`
    ⇒ 既有测试里那些「真实登录 + 造几张单 + 断言看得到/改得动」的夹具，必须**显式**
    声明它持有哪些游戏。否则缺档案 ⇒ `games_of` 空集 ⇒ 读侧空结果 / 写侧业务码 `1000`
    （那不是回归，是新品钉按新契约生效）。

    ⚠️ **与 `make_logged_in_client(games=…)` 的分工（2026-09-23 默认值翻面之后）**：
    夹具**不传 `games`** 时已经自动给当前租户的全部游戏，所以本函数今天主要用于
    ① 对**已经拿到的 `user` 对象**补授权（夹具只返回 client，不返回 user）；
    ② 在夹具之外**再叠加**（`add` 是并集，叠加是安全的）。
    ⛔ **既有调用一处都不要删**：它们是**显式声明**，其中相当一部分是**部分授权**
    （逐款游戏点名），删掉会让"看不到别的游戏"那类断言静默失效。
    ⛔ 也**不要**把"要测没有权限"的用例改成默认值：那样会让「没有档案 / 零游戏 ⇒
    什么都看不到」的钉子变成**恒真**（本夹具最危险的失效形态，见
    `make_logged_in_client` 的 docstring 四条语义）——那种用例应当显式传
    `games=None` / `games=[]` / `games=[…]`（opt-out）。

    ⚠️ **`get_or_create` + `add`，不是 `create` + `set`**：
    ① 调用方可能已在 `setUp` 里建了带别的字段（提成率 / 抢单限制）的档案，
       这里不能因此撞唯一约束；
    ② `add` 是**并集**语义，不抹掉调用方已勾过的游戏（`set` 会）。
    """
    from apps.accounts.models import StaffProfile

    profile, _ = StaffProfile.objects.get_or_create(user=user)
    if games:
        profile.games.add(*games)
    return profile


def permission_select_counts(queries):
    """Count tenant-admin membership and RBAC node selects in a captured request."""
    from apps.business.roles import TENANT_ADMIN

    selects = [query["sql"] for query in queries if query["sql"].lstrip().upper().startswith("SELECT")]
    return {
        "admin_group": sum('FROM "auth_group"' in sql and TENANT_ADMIN in sql for sql in selects),
        "permission_nodes": sum('FROM "rbac_permissionnode"' in sql for sql in selects),
    }

