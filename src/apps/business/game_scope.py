"""可见范围的**规则本体**：按账号的游戏权限收窄（业务方裁定）。 **为什么规则在这里、而不在 `apps/core/scoping.py`**：那个模块是 P5 留的**卡点** （`scope_queryset(user, queryset, scope_filter=None)`），它的 docstring 明写「本模块**不内置任何业务规则**」（依据 `docs/ERRORS.md` **E-0006**：P5 曾把一条猜出来的可见范围规则当成默认行为）。所以本线把规则放在**业务层**，只把 `scope_filter` 传给卡点： scope_queryset(user, Order.objects.all(), scope_filter=game_scope("game_id")) **口径**（业务方逐条裁定）：可见范围 = **该账号自己的游戏权限列表** （`StaffProfile.games`）。「全部游戏」不是额外权限——它只是「不按单个游戏收窄」，所以本模块**不给「全部游戏」加任何载体**（不加字段、不加节点、不加虚拟游戏行）。 ⚠️ **管理员的短路：判据只有一处**（`apps.core.permissions.is_tenant_admin`，租户平面 = 持「系统管理员」职位）。它现在出现在**三个入口**，但**同源**： 1. `games_of`（本模块，**收口评审 A-1 补的**）⇒ 管理员拿到**全部游戏**；
2. `apps/core/scoping.py::scope_queryset` 的短路； 3. `assert_game_allowed` 的早返回。 ⚠️ **承重关系（别把它读成「三处缺一不可」——按 E-0065 的口径问「去掉它哪条用例会红」）**：因为 `games_of` 自己带短路，`game_scope` 的过滤器对管理员**等价于不过滤** ⇒ 在**游戏权限这一路**上，第 2 处**不再承担**「管理员不被收窄」这件事（去掉它，管理员照样看得全）；第 3 处同理不再是承重的（去掉它，`games_of(admin)` 仍含那只游戏 ⇒ 照样写得到）。两处都**保留为纵深**，价值是让「读得到就能写」在每个入口**代码里可见**，而不是靠读者去追 `games_of` 的内部。 ⚠️ 但第 2 处对**别的** `scope_filter`（非 `game_scope` 的那些）**仍然是承重的**—— 那是卡点的通用契约（`scope_queryset` 的 docstring 第 2 条不变量），本线没有改它。 ⛔ 判据**只此一处**（`is_tenant_admin`），不另写第二份、也不用 `is_superuser`。 ⚠️ **空集语义**：`__in=[]` 在 Django 里生成的是**空结果**（不是「不过滤」）。「没有档案 / 没勾任何游戏 ⇒ 空集」是业务方选的**严格**口径：那种账号什么都看不到。
⚠️ **两条判据（过滤 / 拒绝）在本模块定义，调用点都经它们**（2026-10-07 抽出，spec §17.4； 2026-10-07 按独立复审**改准措辞**）：**过滤规则**的写法收在 `game_ids_q` 一处（`Q(**{f"{field}__in": …})`，queryset 形状 `filter_by_game_ids` 只是把它接到 `filter()` 上）； **拒绝规则**的写法收在 `assert_game_id_allowed`。本模块的三个出口（`games_of` / `game_scope` / `assert_game_allowed`）与**开放平台的应用侧**（`apps.openapi`，那里没有 `User`，允许集合来自 `ApiApp.games`）都调这几处 ⇒ 用户侧与应用侧的差异**在「允许集合从哪来」** （`games_of(user)` vs `ApiApp.games`），规则本体不另写。核对办法就是读本模块： `__in` 那行在 `game_ids_q` 里、调用点写的是函数名，不是自己拼过滤条件。 ⚠️ **本条刻意不写「全仓只有一份实现」那种绝对句**（`AGENTS.md` §5-C / E-0011）：2026-10-07 的独立复审正是拿一个反例把前一版的绝对句证伪的——`apps.business.jidanpu_member.orders_of` 当时 **手写**了一份 `game_id__in=games_of(binding.user).values("pk")`，因为它嵌在 `Q(...)` 的 `|` 分支里，而原先的原语只吐 queryset、递不进 `Q()`。那一处**已收编**（现在调 `game_ids_q`，实测编译出的 SQL 与收编前**逐字节相同**）。⇒ 规矩：**新增的「按游戏集合收窄 / 不在集合里就拒」一律走这两条判据的原语**（收窄 ⇒ `filter_by_game_ids`；要嵌进 `Q()` ⇒ `game_ids_q`；拒绝 ⇒ `assert_game_id_allowed`）， ⛔ 别在别的模块里再写一份 `filter(game__in=…)` 或「不在集合里就拒」。 ⚠️ **如实边界**：同模块 `expected_shape` 里的 `games_of(...).filter(pk=…).exists()` 是**单点成员判定**（要一个 bool、不抛错、不吃 queryset），不属于上面两条判据的收窄面，未动它。"""

from django.db.models import Q


def game_ids_q(field: str, game_ids) -> Q:
    """**过滤规则的写法**：一个 `Q`，把 `field` 指向的游戏收窄到 `game_ids`。 ⚠️ **那个 `Q(**{f"{field}__in": game_ids})` 就是「按游戏集合过滤」在本仓的书写处**： `filter_by_game_ids`（queryset 形状）实现上就是 `queryset.filter(game_ids_q(...))`。拆出 `Q` 形状是为了**能嵌进调用方自己的 `Q()`**——`queryset` 递不进 `Q()`，所以
    `apps.business.jidanpu_member.orders_of` 原先只能手写 `game_id__in=…`（2026-10-07 独立复审抓到的反例；那条已收编）。 ⚠️ **嵌进 `Q()` 时注意 `&` 的操作数顺序**：Django 的 `Q(**kwargs)` 在构造时对 kwarg 名做了 `sorted()`，所以 `Q(status=…, game_id__in=…)` 编译出来是 `game_id IN (…) AND status = …`
    ——要**逐字节**复现它的 SQL，`&` 的左边得是 `game_ids_q(...)`： `game_ids_q("game_id", ids) & Q(status=…)`。反过来写（`Q(status=…) & game_ids_q(...)`） **语义完全相同**（AND 可交换）、只是编译出的文本里两个操作数换了位置 ⇒ 对「SQL 语义逐字不变」这种判据会红。⛔ 别据此以为反过来写会算错。 `game_ids` 允许的形状与空集语义见 `filter_by_game_ids`。"""
    return Q(**{f"{field}__in": game_ids})


def filter_by_game_ids(queryset, field: str, game_ids):
    """**过滤规则本体**：把 `queryset` 收窄到「`field` 指向的游戏 ∈ `game_ids`」。过滤规则的**写法**在 `game_ids_q`（本函数只是把它接到 `filter()` 上）：用户侧经 `game_scope(field)` 用它，开放平台的应用侧（`apps.openapi` 的查单端点）直接用它，
    需要嵌进调用方自己的 `Q()` 时用 `game_ids_q`。 `field` 取 `"game_id"` / `"order__game_id"` / `"performance__order__game_id"` ——由调用方按**它自己的模型**选（字段路径写错会 `FieldError` 响亮报错，不会静默不过滤）。
    `game_ids` = **允许的游戏 id 集合**，两种形状都直接交给 `__in`（Django 原生支持）： - 用户侧给 `games_of(user).values("pk")`（子查询 ⇒ 一条 SQL，不把 id 拉进内存）； - 应用侧给 `ApiApp.games` 的 id 查询 / id 列表。 ⚠️ **`game_ids` 为空集 ⇒ 空结果**（`__in=[]` 不是「不过滤」）：这条严格口径由 Django 的 `__in` 语义保证，不需要在这里再加分支（加了反而会出现「空集 ⇒ 全放行」的第二种口径）。"""
    return queryset.filter(game_ids_q(field, game_ids))


def assert_game_id_allowed(game_ids, game) -> None:
    """**拒绝规则本体**：`game` 不在允许的游戏集合里 ⇒ `ValidationProblem`（业务码 1000）。「不在授权游戏集合里就拒」这条判据的写法收在这里：用户侧经 `assert_game_allowed` 用它（写侧准入），开放平台的应用侧用的是**同一条规则的另一面**——查单端点用
    `filter_by_game_ids` 把不在范围内的单**过滤掉**（那一侧要的是「与不存在同形」，不是抛 1000）。 `game_ids` = **允许的游戏集合**，形状是「**能按 `pk` 过滤**的游戏查询」：用户侧给 `games_of(user)`（`Game` 的 QuerySet），应用侧给 `app.games.values("pk")`
    ——两者都支持 `.filter(pk=…)`，所以同一份判据能吃两种来源。 ⚠️ **`game is None` ⇒ 直接返回**：「游戏必填」由既有校验管（`apps.business.validation.validate_required` / `validate_order_chain`），这里再抛一次会让同一件事有两个出处（与 `assert_game_allowed` 的既有口径一致）。"""
    from apps.business.validation import ValidationProblem

    if game is None:
        return
    if not game_ids.filter(pk=game.pk).exists():
        raise ValidationProblem("你没有该游戏的权限")


def games_of(user):
    """该账号有权访问的游戏（`QuerySet[Game]`）。 **没有档案 / 没勾任何游戏 ⇒ 空集**（不是 `None`，也不是"不过滤"）——业务方 2026-09-23 裁定。**唯一的例外是租户管理员 ⇒ 全部游戏**（见下面那条）。 ⚠️ **管理员短路**：`is_tenant_admin(user)` ⇒ 返回 **全部游戏**（`Game.objects.all()`）。判据**复用** `apps.core.permissions.is_tenant_admin`——**与 `apps/core/scoping.py::scope_queryset` 的短路、`assert_game_allowed` 的早返回是同一个函数**，所以三处同源； ⛔ 不另写第二份判据、⛔ 不用 `is_superuser`。 **为什么必须有**（实测的后果，不是防御性写法）：`apps/platform/provisioning.py` 供给出来的租户 `admin` **按构造没有 `StaffProfile`** ⇒ 没有这条短路时 `_user_payload`（`apps/core/auth_views.py`）拿到的 `game_ids` 是 `[]` ⇒ 前端 `fetchGames()`（全量 ∩ `game_ids`）拿到空下拉 ⇒ **管理员从前端录不了单**；
    列表页的游戏列也只能回落显示主键。读侧（`scope_queryset`）与写侧（`assert_game_allowed`）本来就有这条短路 ⇒ 它同时也让「读得到就能写」在 `games_of` 这个出处上成立（三处口径一致）。 **为什么取 `Game.objects.all()` 而不是「排除软删游戏」**：软删（`Game.deleted_at`， D-0085）是**列表/界面**层的口径——`GET /api/business/games/` 的 queryset 才按它过滤，前端的 `fetchGames()` 又拿那份全量与 `game_ids` 求交 ⇒ 软删游戏自然从下拉消失；而**权限集本身**不看 `deleted_at`：普通账号那一支（`profile.games.all()`）也不排除软删 ⇒ 只给管理员排除会变成**两种口径**（同一个软删游戏，普通持权账号写得到、管理员写不到），与「三处同源」互相矛盾。这里保持与普通账号完全同形：**授予集该有什么就回什么**。 ⚠️ **缺档案时 Django 抛的异常能被子类的默认值兜住**：反向 OneToOne （`StaffProfile.user` 的 `related_name="staff_profile"`）在缺行时抛
    `RelatedObjectDoesNotExist`。实测它的 MRO 是 `RelatedObjectDoesNotExist → DoesNotExist → ObjectDoesNotExist → AttributeError → Exception → BaseException → object` ⇒ 是 `AttributeError` 的**子类** （`issubclass(..., AttributeError) is True`，探针见本线报告）⇒ `getattr(user, "staff_profile", None)` 的 `None` 默认值能兜住它，**不需要** `try/except`。 ⚠️ **`public` 平面单独早返回**（实测踩到，不是防御性写法）：`StaffProfile` 是 `TENANT_APPS` 成员 ⇒ `accounts_staffprofile` 这张表**只在租户 schema 里**。在平台平面上访问反向描述符时，Django 发出的是对**不存在的表**的查询 ⇒ 抛的是 `django.db.utils.ProgrammingError: relation "accounts_staffprofile" does not exist` ——它**不是** `AttributeError` 的子类，`getattr` 的默认值兜不住。（实测：`ensure_public_tenant` 之后在 public 里建平台账号，`games_of(user)` 抛上面那条 `ProgrammingError`；而 `/api/auth/me` 在平台平面也要能跑 ⇒ 不早返回会让运营后台的 `/api/auth/me` 直接 500。）平台账号在本模块的口径里没有游戏权限 ⇒ 空集。 ⚠️ **`public` 早返回必须排在管理员短路之前**：`is_tenant_admin` 在 `public` 平面回的是 `is_superuser`，而 `Game` 是租户表 ⇒ 先判管理员会让平台超管去查**不存在的表**（同上一条的 `ProgrammingError`）。平台账号一律空集（既有用例钉着）。"""
    from apps.business.models import Game
    from apps.core.permissions import is_tenant_admin
    from apps.core.plane import on_public_plane

    if on_public_plane():
        return Game.objects.none()
    if is_tenant_admin(user):
        return Game.objects.all()
    profile = getattr(user, "staff_profile", None)
    if profile is None:
        return Game.objects.none()
    return profile.games.all()


def game_scope(field: str):
    """`scope_queryset` 的 `scope_filter` **工厂**：按 `field` 指向的游戏列收窄。 `field` 取 `"game_id"` / `"order__game_id"` / `"performance__order__game_id"`
    ——由调用方按**它自己的模型**选（同一个字段路径写错会 `FieldError` 响亮报错，不会静默不过滤）。返回的函数签名是 `(queryset, user) -> QuerySet`，直接交给
    `scope_queryset(..., scope_filter=...)`。 ⚠️ **本工厂只负责「允许集合从账号来」，过滤规则本身在 `filter_by_game_ids`** ——⛔ 不要把 `filter(...)` 写回这里（那就成了第二份判据）。"""

    def _filter(queryset, user):
        return filter_by_game_ids(queryset, field, games_of(user).values("pk"))

    return _filter


def accounts_with_game(accounts, game):
    """Bulk form of the tenant-side game membership check in games_of."""
    from apps.business.roles import TENANT_ADMIN
    from apps.core.plane import on_public_plane

    if on_public_plane():
        return accounts.none()
    return accounts.filter(
        game_ids_q("staff_profile__games", [game.pk]) | Q(groups__name=TENANT_ADMIN),
    ).distinct()


def assert_game_allowed(user, game) -> None:
    """写侧准入：`game` 不在 `games_of(user)` 里 ⇒ `ValidationProblem`（业务码 1000）。 `game is None` ⇒ **直接返回**：「游戏必填」由既有校验管（`apps.business.validation.validate_required` / `validate_order_chain`），这里再抛一次会让同一件事有两个出处。
    ⚠️ **管理员早返回（纵深，不是承重的）**：判据**复用** `apps.core.permissions.is_tenant_admin`，⛔ 不另写第二份、⛔ 不用 `is_superuser` （租户平面早就不看它了）。它当初是**必须的**（2026-09-23 执行期修订 2 实测）：供给出来的租户管理员 **按构造没有 `StaffProfile`**，而那时 `games_of(admin)` 是空集 ⇒ 录单 / 改单 / 复制全被
    `1000` 拒，与读侧（`scope_queryset` 短路）不对称。 `games_of` 已有**同一条**短路，本函数的早返回**不再承重** ——去掉它，`games_of(admin)` 仍含那只游戏 ⇒ 管理员照样写得到。保留它是让「写侧也有准入」在代码里**可见**（去掉会静默依赖 `games_of` 的内部）； ⛔ 不要因此以为「删了会红」：见模块 docstring 的「承重关系」那段。 ⚠️ **本函数只是「允许集合从账号来」那一层**：拒绝规则本体在 `assert_game_id_allowed`（⛔ 不要把 `filter(pk=…).exists()` 写回这里）。"""
    from apps.core.permissions import is_tenant_admin

    if game is None:
        return
    if is_tenant_admin(user):
        return
    assert_game_id_allowed(games_of(user), game)
