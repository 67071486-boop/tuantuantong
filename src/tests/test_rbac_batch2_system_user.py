"""`system_user` 逐点挂节点门，外加两个限制型 flag 的脱敏矩阵。

管理员认「系统管理员」职位。限制型 flag 对管理员旁路，脱敏断言必须用非管理员当 viewer。
账号端点准入是「管理员，或持任一账号管理节点」：人事持对应节点就能改账号。
配对：管理员有没有节点都 200；非管理员持该节点 200；摘掉该节点 403，文案来自排在前面的节点门。
action → node 用字面表钉，不从被测常量反推。
"""
import itertools

from django.contrib.auth.models import User
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.views import AccountViewSet
from apps.business.roles import SERVICE, TENANT_ADMIN
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.rbac.models import PermissionNode, Role
from apps.rbac.permissions import user_node_codes
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client


class AccountNodeGateTests(TenantTestCase):
    """逐 action：管理员无条件放行 / 非管理员**持该节点**仍被管理员门拒。

    ⚠️ **2026-09-23 判据统一后重写**：原文是「同一个 client，持节点 200 / 摘掉节点 403」，
    但那依赖"职位持有者被节点门拦得住"——现在职位持有者**就是**管理员，`has_node_now`
    对它无条件短路 ⇒ 那一对区分不出来。新的配对见 `_assert_pair` 的 docstring。
    """

    #: `_non_admin_holding` 的用户名序号（见那里的说明）。
    _plain_seq = itertools.count()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase` 只建空 schema：Group / Role / 权限目录都要 `seed_tenant` 灌
        # （它内部调 `seed_rbac()` ⇒ 「系统管理员」Role 拿到**全部非 flag 节点**）。
        seed_tenant(cls.tenant)

    def setUp(self):
        # ⚠️ 目标账号**不是**请求者自己：
        # ① `reset_password` 会把**自己**的会话踢掉（口令哈希变了 ⇒ 会话 flush ⇒ 后续请求 401
        #    而不是 403），拿自己当目标会让"拒绝侧"断不出来；
        # ② 停用自己会触发 1004 自锁保护，混进另一条分支。
        self.victim = User.objects.create_user(username="gate-victim", password="pw-123456")
        # ⚠️ **游戏必须自己建**：`seed_tenant` 的 `DEFAULT_GAMES = ()`
        # （`apps/platform/provisioning.py:23`，**实测**）⇒ 它一行游戏都不灌，
        # `Game.objects.first()` 会是 `None`、`"games": [None]` 直接被序列化器拒成 1000。
        from apps.business.models import Game

        self.game = Game.objects.create(name="原神")
        # 请求者：持「系统管理员」Group（= 过账号端点准入，线 D 起准入类是
        # `IsAccountManager`；管理员是它的第一支），节点来自该 Group 的 Role。
        self.client = make_logged_in_client(username="gate-admin", groups=(TENANT_ADMIN,))

    # ── 夹具 ───────────────────────────────────────────────────────
    def _grant(self, *codes):
        Role.objects.get(name=TENANT_ADMIN).permissions.add(
            *PermissionNode.objects.filter(code__in=codes)
        )

    def _drop(self, *codes):
        """把节点从「系统管理员」Role 上摘掉（本用例事务内，测试结束自动回滚）。"""
        Role.objects.get(name=TENANT_ADMIN).permissions.remove(
            *PermissionNode.objects.filter(code__in=codes)
        )

    def _assert_pair(self, request_call, node):
        """配对判据（**线 D 2026-10-12 第三次重写**；上一版见 git 历史）。

        ① **管理员**（持「系统管理员」职位）在同一条请求上：持该节点 ⇒ 200 + code 0，
           **摘掉该节点 ⇒ 仍然 200 + code 0**（管理员短路就取自 `is_tenant_admin`，
           管理员门因此蕴含节点门）——这一半**逐字未变**；
        ② 一个**非管理员、持该节点**的账号打同一条请求 ⇒ **200 + code 0**
           （线 D 的新准入：`IsAccountManager` 认账号管理节点，这就是「人事能开子账户」）。
           **摘掉该节点之后再打一次 ⇒ 403，且文案是 `NODE_DENIED_MESSAGE`**
           ——节点门真的在拦，放宽的只是准入那一扇。

        ⚠️ **本方法分辨不出"某个 action 挂的是哪条 code"**：每条用例都把自己的 code
        当参数传进来，所以把 `ACCOUNT_PERMISSIONS["retrieve"]` 改成别的 code，本系列
        照样全绿。**action → code 的映射**由
        `test_the_per_action_node_mapping_is_exactly_this_literal_table` 的字面表钉住
        （评审 B1）。

        ⚠️ **必须同时断 `code == 0`，不能只断 HTTP 200**：本项目信封把**业务失败**也回成
        HTTP 200（校验失败 → 200 + code 1000、重名 → 200 + 1001、找不到 → 200 + 1002）。
        只断 `status_code == 200` 时，一个字段没填全的 POST 也会"通过"——等于没测
        （E-0009 / E-0024 族：断言没有判别力）。

        ⚠️ **2026-09-23 那版**（非管理员持节点 ⇒ 被 `IsAccountAdmin` 拒 403）随线 D 作废：
        那条断言今天会**红**（准入放宽了，它拿到 200）。
        """
        self._grant(node)
        ok = request_call()
        self.assertEqual(ok.status_code, 200, f"管理员持 {node} 应当放行：{ok.content[:200]!r}")
        self.assertEqual(
            ok.json()["code"], 0,
            f"持 {node} 的业务码应当是 0（200 + 非 0 = 业务失败）：{ok.content[:300]!r}",
        )
        self._drop(node)
        still_ok = request_call()
        self.assertEqual(
            still_ok.status_code, 200,
            f"摘掉 {node} 之后管理员仍应放行（管理员短路蕴含节点门）：{still_ok.content[:200]!r}",
        )
        self.assertEqual(still_ok.json()["code"], 0, still_ok.content[:300])
        # ② 非管理员持该节点 ⇒ 线 D 之后**放行**（新能力）；摘掉节点 ⇒ 节点门拒 403。
        non_admin = self._non_admin_holding(node)
        self.client, original = non_admin, self.client
        try:
            allowed = request_call()
            # 摘掉他唯一的账号管理节点（挂在 `SERVICE` 的 `Role` 上，`grant_nodes` 的落点）
            Role.objects.get(name=SERVICE).permissions.remove(
                PermissionNode.objects.get(code=node)
            )
            denied = request_call()
        finally:
            self.client = original
        self.assertEqual(
            allowed.status_code, 200,
            f"非管理员持 {node} 应当放行（线 D 的准入）：{allowed.content[:300]!r}",
        )
        self.assertEqual(allowed.json()["code"], 0, allowed.content[:300])
        self.assertEqual(
            denied.status_code, 403,
            f"摘掉 {node} 之后必须 403（节点门）：{denied.content[:300]!r}",
        )
        self.assertEqual(
            denied.json()["msg"], AccountViewSet.NODE_DENIED_MESSAGE,
            "这条 403 应当来自**节点门**（装配顺序 `[IsAuthenticated, gate, IsAccountManager]`）",
        )
        self._grant(node)

    def _non_admin_holding(self, node):
        """建一个**非管理员**、但持 `node` 的账号，返回它的已登录 client。

        ⚠️ 用户名每次不同：`_assert_pair` 一条用例里可能被调多次（同一条测试里三次请求
        加多次构造），复用会撞 `auth_user_username_key`。
        """
        username = f"gate-plain-{next(self._plain_seq)}"
        client = make_logged_in_client(username=username, groups=(SERVICE,))
        grant_nodes(User.objects.get(username=username), node)
        return client

    def _game_pk(self):
        return self.game.pk

    # ── 逐 action ─────────────────────────────────────────────────
    def test_list_requires_system_user_list(self):
        self._assert_pair(lambda: self.client.get("/api/accounts/"), "system_user.list")

    def test_create_and_patch_require_system_user_add_edit(self):
        import itertools

        counter = itertools.count()

        def create():
            # 每次调用换一个用户名：同一条用例里要 POST 三次（有节点 / 无节点 / 还原），
            # 复用同一个用户名会让后两次落进 1001 重名分支（HTTP 也是 200 ⇒ 假通过）。
            return self.client.post(
                "/api/accounts/",
                {
                    "username": f"created-by-gate-{next(counter)}",
                    "role": "客服",
                    "games": [self._game_pk()],
                    # ⚠️ 新增路径上这**六个**是必填（`serializers.py` 的
                    # `CREATE_REQUIRED_FIELDS`，**实测**）；少一个就被序列化器拒成
                    # 200 + code 1000 —— 只断 HTTP 200 会假通过，所以 `_assert_pair`
                    # 必须同时断 `code == 0`。取值抄 `tests/test_account_api.py` 的
                    # `CREATE_REQUIRED_PROFILE_FIELDS`（同一份口径，别自己另编）。
                    "display_name": "小王",
                    "commission_value": "10.00",
                    "grab_limit_dl": 0,
                    "grab_limit_pw": 0,
                    "hired_on": "2026-01-02",
                    "deposit_amount": "0.00",
                },
                content_type="application/json",
            )

        self._assert_pair(create, "system_user.add_edit")
        # 同一个 code 的另一条写路径（PATCH）
        self._assert_pair(
            lambda: self.client.patch(
                f"/api/accounts/{self.victim.pk}/",
                {"display_name": "改名"},
                content_type="application/json",
            ),
            "system_user.add_edit",
        )

    def test_put_requires_add_edit_too(self):
        """`update`（**PUT**）与 `partial_update`（PATCH）共用 `system_user.add_edit`。

        ⚠️ **为什么单独一条**：PUT 落到的是 `update` 这个 action 名，在 `ACCOUNT_PERMISSIONS`
        里是**独立一行**；没有用例打 PUT，那一行被改成别的 code（哪怕换成一个该账号也持有的
        节点）都不会红（Task 2 评审的 Minor 3 实测指出）。
        ⚠️ PUT 载荷的必填项只有 `username` / `role` / `games` 三个——编辑路径上
        `CREATE_REQUIRED_FIELDS` 那条翻必填的分支**不生效**（判据是
        `self._current_user() is None and not self.partial`，编辑时 `context={"instance": user}`
        让前者为假）。取值口径与既有 `tests/test_account_api.py:1010` 的 `required` 一致。
        """
        payload = {
            "username": "gate-victim",
            "role": "客服",
            "games": [self._game_pk()],
        }
        self._assert_pair(
            lambda: self.client.put(
                f"/api/accounts/{self.victim.pk}/", payload, content_type="application/json"
            ),
            "system_user.add_edit",
        )

    def test_retrieve_requires_add_edit_not_list(self):
        """`retrieve` 归 `add_edit`（§4 偏离 1）。

        ⚠️ **2026-09-23 口径变更**：原文在这里补了一刀"只持 `list` 打不开详情 ⇒ 403"。
        判据统一后能过 `IsAccountAdmin` 的账号**就是**管理员，管理员无条件过节点门
        ⇒ 那一刀**对任何能过管理员门的调用方**都观察不到（实测 200）。
        节点表的区分度**由**
        `test_the_per_action_node_mapping_is_exactly_this_literal_table`（字面映射表）承重
        ——⛔ 不要写成 `AccountPermissionTableCoverageTests`：那条只钉**键集合**（每个 action
        都登记过），**不钉映射**（评审 B1 指出的正是这处说假话的注释）。
        本用例保留 `_assert_pair` 的配对判据（管理员无节点也 200 / 非管理员持节点被管理员门拒）。
        """
        self._assert_pair(
            lambda: self.client.get(f"/api/accounts/{self.victim.pk}/"), "system_user.add_edit"
        )

    def test_roles_dropdown_declares_either_page_node(self):
        """`roles` 是两个页面共用的下拉（§4 偏离 2）⇒ any 语义。

        ⚠️ **2026-09-23 口径变更**：原文逐段摘节点断言 200/403 的区分——管理员无条件过
        节点门，那条区分**对任何能过管理员门的调用方**都观察不到（实测全 200）。本用例改成钉**声明**
        （两个 code 的字面集合，any 语义）+ 管理员放行（完整映射表见
        `test_the_per_action_node_mapping_is_exactly_this_literal_table`）。
        """
        self.assertEqual(
            set(AccountViewSet.ACCOUNT_PERMISSIONS["roles"]),
            {"system_user.add_edit", "system_user.list"},
        )
        self.assertEqual(self.client.get("/api/accounts/roles/").status_code, 200)

    def test_the_per_action_node_mapping_is_exactly_this_literal_table(self):
        """**逐 action → 节点 code 的字面映射钉子**（评审 B1 回补）。

        ⚠️ 为什么必须有它：判据统一后 `_assert_pair` 钉的是"管理员被短路 / 非管理员被
        **管理员门**拒"，**分辨不出**某个 action 挂的是哪条 code；而
        `AccountPermissionTableCoverageTests` 只断言"每个 action 都在表里"（**键集合**），
        **不钉映射**。少了本条，把 `ACCOUNT_PERMISSIONS["retrieve"]` 改成任何别的 code
        （甚至 `("system_user.reset_password",)`）都**没有任何断言会红**——而它正是
        `get_permissions()` 真正读的那张表。

        ⚠️ 期望值写**字面量**（E-0024）：⛔ 不要把 `AccountViewSet.ACCOUNT_PERMISSIONS`
        自己当期望值，那是自指断言（表被改坏时不会红）。
        ⚠️ `roles` 是**两个 code 的 any 语义**，这里连元组顺序一起钉（顺序不影响语义，
        但改了顺序在 diff 里应当可见）。
        """
        self.assertEqual(
            dict(AccountViewSet.ACCOUNT_PERMISSIONS),
            {
                "list": ("system_user.list",),
                "retrieve": ("system_user.add_edit",),
                "create": ("system_user.add_edit",),
                "update": ("system_user.add_edit",),
                "partial_update": ("system_user.add_edit",),
                "roles": ("system_user.list", "system_user.add_edit"),
                "openers": ("system_user.list",),
                "status": ("system_user.change_status",),
                "status_batch": ("system_user.change_status",),
                "reset_password": ("system_user.reset_password",),
                # ⭐ `feat/system-user-list-completion` 的两项：
                # 软删/恢复（`POST api/accounts/<pk>/delete/` 与 `.../restore/`）**复用既有的
                # `system_user.change_status` 节点、不新增节点**（决策 **D-0102**）。
                # ⚠️ 本表是 `main` 的 RBAC 线（提交 `05136e8`）在本线那两个 `@action` **之前**写下的，
                # 两边合起来才第一次同时出现 ⇒ **这是「无文本冲突但语义过期」的合并失败**，
                # 由全量套件抓到（`Ran 2079 / FAILED (failures=1)`），**不是**文本冲突。
                # ⛔ 期望值仍是**字面量**（E-0024），不许改成读 `ACCOUNT_PERMISSIONS` 自己（自指）。
                "delete": ("system_user.change_status",),
                "restore": ("system_user.change_status",),
                # ⭐ T8（实名认证线）：管理页那四个 action。
                #    三个读动作 + 一个写动作共用 **同一个** 对标权限点
                #    `system_user.realname_records`(1263)（对标那一页就是一个点）；
                #    重置单独挂 `system_user.reset_realname`(1264)。
                #    ⛐ 不新增权限节点（catalog 是对标权限树的声明式全表）。
                "realname_logs": ("system_user.realname_records",),
                "realname_setting": ("system_user.realname_records",),
                "realname_quota": ("system_user.realname_records",),
                # ⭐ T3（spec §2.4）：资源包配置面（GET/POST
                #    `api/accounts/realname-packages/`）**复用同一个** 1263 节点
                #    （对标那一页就是一个点，⛔ 不新增节点——同上面四条的口径）。
                "realname_packages": ("system_user.realname_records",),
                "reset_realname": ("system_user.reset_realname",),
            },
        )

    def test_openers_requires_list_node(self):
        self._assert_pair(lambda: self.client.get('/api/accounts/openers/'), 'system_user.list')

    def test_status_endpoints_require_change_status(self):
        url = f"/api/accounts/{self.victim.pk}/status/"
        self._assert_pair(
            lambda: self.client.post(url, {"is_active": True}, content_type="application/json"),
            "system_user.change_status",
        )
        self._assert_pair(
            lambda: self.client.post(
                "/api/accounts/status/",
                {"ids": [self.victim.pk], "is_active": True},
                content_type="application/json",
            ),
            "system_user.change_status",
        )

    def test_reset_password_requires_its_own_node(self):
        self._assert_pair(
            lambda: self.client.post(
                f"/api/accounts/{self.victim.pk}/reset-password/",
                {},
                content_type="application/json",
            ),
            "system_user.reset_password",
        )

    def test_an_unrelated_node_does_not_open_the_door(self):
        """持**别的**账号管理节点不能代替：`system_user.reset_password` 打不开 `list`。

        ⚠️ **线 D之后这条依然成立、但拒人的那扇门变了**：原文写「对非管理员，
        第一道门（管理员门）就过不了」——准入换成 `IsAccountManager` 之后，持
        `system_user.reset_password` 的**能过准入**（它在账号管理节点集合里），
        但 `list` 要的是 `system_user.list` ⇒ **节点门**先拒，403 的文案是
        `NODE_DENIED_MESSAGE`。这正是「放宽准入 ≠ 放宽动作」的判据。
        """
        plain = make_logged_in_client(username="gate-unrelated", groups=(SERVICE,))
        grant_nodes(User.objects.get(username="gate-unrelated"), "system_user.reset_password")
        resp = plain.get("/api/accounts/")
        self.assertEqual(resp.status_code, 403)
        # ⚠️ 这一条来自**节点门**（装配顺序是 `[IsAuthenticated, gate, IsAccountManager]`）：
        # 准入那一扇他过得去（持的是账号管理节点），是节点门按 `system_user.list` 拒的。
        self.assertEqual(resp.json()["msg"], AccountViewSet.NODE_DENIED_MESSAGE)
        # 管理员摘掉 list 仍然放行（管理员短路蕴含节点门）。
        self._drop("system_user.list")
        allowed = self.client.get("/api/accounts/")
        self.assertEqual(allowed.status_code, 200, allowed.content[:200])
        self.assertEqual(allowed.json()["code"], 0)

    def test_a_non_admin_holding_the_node_is_admitted_by_the_manager_gate(self):
        """**线 D（2026-10-12）口径变更**：非管理员持账号节点 ⇒ 放行；摘掉 ⇒ 节点门拒。

        原文（版，见 git 历史）断言「非管理员持 `system_user.list` 仍 403
        （管理员门先拒）」，并拿它当「管理员门蕴含节点门」的另一半。线 D 把账号端点的准入
        换成 `IsAccountManager`（持任一账号管理节点即可，**不要求是管理员**）之后，
        那条断言**会红**（实测 200）⇒ 本用例钉新的关系：准入与节点门**各管一件事**。
        """
        plain = make_logged_in_client(username="gate-plain", groups=(SERVICE,))
        grant_nodes(User.objects.get(username="gate-plain"), "system_user.list")
        allowed = plain.get("/api/accounts/")
        self.assertEqual(allowed.status_code, 200, allowed.content[:200])
        self.assertEqual(allowed.json()["code"], 0)

        # 摘掉节点 ⇒ 节点门拒（准入那一扇同时也不成立，但轮不到它）
        Role.objects.get(name=SERVICE).permissions.remove(
            PermissionNode.objects.get(code="system_user.list")
        )
        denied = plain.get("/api/accounts/")
        self.assertEqual(denied.status_code, 403, denied.content[:200])
        self.assertEqual(denied.json()["msg"], AccountViewSet.NODE_DENIED_MESSAGE)

        # 管理员摘掉 list 仍然放行（那一半逐字未变）。
        self._drop("system_user.list")
        still_allowed = self.client.get("/api/accounts/")
        self.assertEqual(still_allowed.status_code, 200, still_allowed.content[:200])
        self.assertEqual(still_allowed.json()["code"], 0)

    def test_node_denied_message_is_this_exact_literal(self):
        """⚠️ **值钉子**：期望值写**字面量**，不引用被测常量（E-0024：自指断言在常量被改坏时不会红）。

        `_assert_pair` 断的是「响应体的 `msg` == 该常量」——那条证明"响应带了这个文案"，
        但常量本身换成**另一个非空串**它照样绿。本条把**文案的值**钉住。

        ⚠️ **"403 可指认来源"的依据是代码事实，不是任何翻译文案**（译文随 locale 变）：
        `IsAccountManager`（`apps/accounts/permissions.py`）**没有定义 `message`**
        ⇒ `rest_framework/views.py:340` 取到 `None` ⇒ `permission_denied`（同文件 180 行）
        抛 `PermissionDenied(detail=None)` ⇒ 走 DRF 的 `PermissionDenied.default_detail`
        （`rest_framework/exceptions.py:184`）⇒ 与 `NODE_DENIED_MESSAGE` 这条本项目自有字面量
        **来源不同**（一个来自 DRF 的翻译目录、一个是本项目写死的字面量）。
        ⚠️ **刻意不写"必定不同"**：那需要枚举全部 locale 才成立（E-0011）。
        本判据真正的保险是下面本条用例的 `assertEqual` 加上
        `_assert_pair` ② 摘掉节点那一刀、以及
        `test_a_non_admin_holding_the_node_is_admitted_by_the_manager_gate` 里对
        **节点门文案**的 `assertEqual`——那两处把「403 是哪扇门给的」钉在文案上，
        文案被改坏时会**响亮地红**，而不是静默假通过。
        ⚠️ `apps/core/exceptions.py:67` 的 `"无权限"` 是 **detail 为空串时的兜底**
        （把 `NODE_DENIED_MESSAGE` 变异成 `""` 时会看到它），**不是任何一扇门**的文案
        ——把两者混起来会让本判据的依据说错。
        """
        self.assertEqual(
            AccountViewSet.NODE_DENIED_MESSAGE,
            "当前职位没有此操作所需的权限节点",
        )

    def test_anonymous_is_401_not_403(self):
        from django.test import Client

        resp = Client(SERVER_NAME="tenant.test.com").get("/api/accounts/")
        self.assertEqual(resp.status_code, 401)


class AccountFlagMaskingTests(TenantTestCase):
    """两个 `flag` 的脱敏矩阵（**2026-09-23 判据统一后拆成两半**）。

    ⚠️ **口径变更（原文断言"HTTP 各读面被遮"，见 git 历史）**：能过 `IsAccountAdmin` 的
    账号 = 持「系统管理员」职位的人，而**同一个职位也是这两条限制型 flag 的旁路判据**
    （`flag_enabled` 的短路从 `is_superuser` 换成了 `is_tenant_admin`）⇒ 账号端点上
    **没有任何 HTTP 调用方会被这两条 flag 限制**（脱敏在那条路径上不可达）。
    所以本类拆成：

    - **HTTP 读面**（列表 / 详情 / 编辑响应 / 新增响应）：钉**管理员不被限制**
      （「管理员不该变成最受限的人」，DoD #4 的承重面）；
    - **读取层**（`account_payload(viewer=…)`）：用一个**非管理员、持该 flag 的账号**当
      viewer，钉**脱敏本身仍然生效**（判据仍落在节点上，没坏）。

    ⚠️ **刻意不写"共 N 个读面"**（措辞清扫·脆性计数类）：加一个读面（例如导出）就会
    静默变假。读面清单以本类的 `def test_` 方法为准。

    ⚠️ `flag` 的失败形态是**字段被遮**，不是 403（spec §5.2）——本类的断言全部打在
    **响应体里的值**上；`status_code == 200` 只是**前置守卫**（保证读到的是成功信封而不是
    403/500 的别的形状），判别力全在值上。

    ⚠️ **数值过 HTTP 之后是 `float`，不是 `Decimal`**（读源码核实：`account_payload` 返回的是
    普通 dict、不经序列化器，而 `api_response` 用 DRF 的 `JSONRenderer`，它的
    `encoders.JSONEncoder` 对 `Decimal` 走 `float(obj)`——`rest_framework/utils/encoders.py:42-44`）。
    所以这里断 `30.0` / `8000.0`。**直接调 `account_payload()` 时才拿到 `Decimal`**
    （既有用例 `tests/test_account_serializer.py:364` 就是这么断的）。
    """

    CARD = "110101199001011234"

    #: `_non_admin_flag_viewer` 的用户名序号（同一条用例可能建多个）。
    _viewer_seq = itertools.count()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        from apps.accounts.models import StaffProfile
        from apps.business.models import Game

        self.victim = User.objects.create_user(username="mask-victim", password="pw-123456")
        profile = StaffProfile.objects.create(
            user=self.victim,
            id_card_no=self.CARD,
            base_salary="8000.00",
            commission_mode=20,
            commission_value="30.00",
        )
        # ⚠️ 游戏必须自己建：`seed_tenant` 的 `DEFAULT_GAMES = ()` ⇒ 一行都不灌（**实测**）。
        self.game = Game.objects.create(name="原神")
        profile.games.set([self.game])
        self.admin = make_logged_in_client(username="mask-admin", groups=(TENANT_ADMIN,))

    def _non_admin_flag_viewer(self, *codes):
        """一个**非管理员**、但持 `codes` 的账号——脱敏判据在它身上仍可观察。"""
        username = f"mask-plain-{next(self._viewer_seq)}"
        make_logged_in_client(username=username, groups=(SERVICE,))
        viewer = User.objects.get(username=username)
        grant_nodes(viewer, *codes)
        return viewer

    # ── 夹具 ───────────────────────────────────────────────────────
    def _grant(self, *codes):
        Role.objects.get(name=TENANT_ADMIN).permissions.add(
            *PermissionNode.objects.filter(code__in=codes)
        )

    def _game_pk(self):
        return self.game.pk

    def _detail(self):
        resp = self.admin.get(f"/api/accounts/{self.victim.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        return resp.json()["data"]

    def _list_row(self):
        rows = self.admin.get("/api/accounts/").json()["data"]["items"]
        return next(r for r in rows if r["id"] == self.victim.pk)

    # ── 用例 ───────────────────────────────────────────────────────
    def test_without_any_flag_the_admin_sees_the_real_values_in_detail(self):
        """前置对照：不持 flag 时详情是**全量**——否则下面的"被遮住"可能是别的原因凑出来的。"""
        fields = self._detail()
        self.assertEqual(fields["id_card_no"], self.CARD)
        self.assertEqual(fields["commission_value"], 30.0)
        self.assertEqual(fields["base_salary"], 8000.0)
        self.assertEqual(fields["commission_mode"], 20)

    def test_id_card_flag_does_not_touch_the_admin_but_still_masks_a_non_admin_reader(self):
        """`restrict_view_id_card`：**管理员不被限制**；**非管理员持它时读取层仍脱敏**。

        ⚠️ **2026-09-23 口径变更**：原文断言 HTTP 各读面被遮——能过 `IsAccountAdmin` 的
        账号就是管理员，而管理员走 `flag_enabled` 的旁路 ⇒ HTTP 那条路不再脱敏。
        判据没被放宽：读取层（`account_payload(viewer=…)`）用非管理员 viewer 仍然脱敏。
        """
        from apps.accounts.payloads import account_payload

        self._grant("system_user.restrict_view_id_card")
        fields = self._detail()
        self.assertEqual(fields["id_card_no"], self.CARD)        # 管理员：原样
        self.assertEqual(fields["commission_value"], 30.0)      # 另一条 flag 也不受影响
        # 列表那一档本来就脱敏 PII（`mask_pii=True`，与 flag 无关）⇒ 值仍是遮的。
        self.assertEqual(self._list_row()["id_card_no"], "110***34")
        # 读取层：非管理员持同一条 flag ⇒ 详情也脱敏（前 3 后 2）。
        viewer = self._non_admin_flag_viewer("system_user.restrict_view_id_card")
        self.assertEqual(account_payload(self.victim, viewer=viewer)["id_card_no"], "110***34")
        self.assertEqual(
            account_payload(self.victim, mask_pii=True, viewer=viewer)["id_card_no"],
            "110***34",
        )

    def test_commission_flag_does_not_touch_the_admin_but_still_nulls_for_a_reader(self):
        """`restrict_view_commission_params`：同上——管理员原样、非管理员 viewer 读不到三个参数列。"""
        from apps.accounts.payloads import account_payload

        self._grant("system_user.restrict_view_commission_params")
        detail = self._detail()
        row = self._list_row()
        for surface in (detail, row):
            self.assertEqual(surface["base_salary"], 8000.0)    # 管理员：原样（旁路）
            self.assertEqual(surface["commission_mode"], 20)
            self.assertEqual(surface["commission_value"], 30.0)
        # ⚠️ 逐面各自的口径：详情**不受** §5.3 的列表脱敏影响（只有持那条 flag 才被遮），
        # 列表本来就对所有管理员脱敏 PII。两处不能写成一个期望值。
        self.assertEqual(detail["id_card_no"], self.CARD)
        self.assertEqual(row["id_card_no"], "110***34")
        # 读取层：非管理员持该 flag ⇒ 三个参数列置 None（不是字符串）。
        viewer = self._non_admin_flag_viewer("system_user.restrict_view_commission_params")
        detail_view = account_payload(self.victim, viewer=viewer)
        list_view = account_payload(self.victim, mask_pii=True, viewer=viewer)
        for surface in (detail_view, list_view):
            self.assertIsNone(surface["base_salary"])
            self.assertIsNone(surface["commission_mode"])
            self.assertIsNone(surface["commission_value"])
        # 逐面各自的口径（与上面那半同源）：详情不受列表脱敏影响、列表本来就脱敏 PII。
        self.assertEqual(detail_view["id_card_no"], self.CARD)
        self.assertEqual(list_view["id_card_no"], "110***34")

    def test_both_flags_are_bypassed_for_the_admin_and_apply_to_a_reader(self):
        """两条 flag 同时授予：管理员两个字段都原样；非管理员 viewer 两个都被遮。"""
        from apps.accounts.payloads import account_payload

        self._grant(
            "system_user.restrict_view_id_card",
            "system_user.restrict_view_commission_params",
        )
        fields = self._detail()
        self.assertEqual(fields["id_card_no"], self.CARD)
        self.assertEqual(fields["commission_value"], 30.0)
        viewer = self._non_admin_flag_viewer(
            "system_user.restrict_view_id_card",
            "system_user.restrict_view_commission_params",
        )
        reader = account_payload(self.victim, viewer=viewer)
        self.assertEqual(reader["id_card_no"], "110***34")
        self.assertIsNone(reader["commission_value"])

    def test_write_response_is_not_masked_for_the_admin(self):
        """写响应也是读面——但**管理员**经旁路看到的是原值（2026-09-23 口径变更）。

        ⚠️ 原文（`test_write_response_is_masked_too`）断言写响应被遮；那个断言要求
        "持 flag 的人能过 `IsAccountAdmin`"，而新口径下这两件事互斥 ⇒ 写响应上的脱敏
        **没有能过管理员门的调用方**（非管理员被管理员门先拒）。这里钉新口径，
        读取层的脱敏见上两条。
        """
        self._grant("system_user.restrict_view_id_card")
        resp = self.admin.patch(
            f"/api/accounts/{self.victim.pk}/",
            {"display_name": "改个名"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["data"]["id_card_no"], self.CARD)

    def test_create_response_is_not_masked_for_the_admin(self):
        """`create` 的回执也是读面——但**管理员**经旁路看到的是原值（2026-09-23 口径变更）。

        ⚠️ 原文（`test_create_response_is_masked_too`）断言回执被遮；那条要求"持 flag 的人
        能过 `IsAccountAdmin`"，新口径下两者互斥 ⇒ 该读面上的脱敏**没有能过管理员门的调用方**。
        载荷必填项照 `tests/test_account_api.py` 的 `CREATE_REQUIRED_PROFILE_FIELDS`
        （六个）+ `username` / `role` / `games`；只断 HTTP 200 会假通过，所以同时断 `code == 0`。
        """
        self._grant("system_user.restrict_view_id_card")
        resp = self.admin.post(
            "/api/accounts/",
            {
                "username": "mask-created",
                "role": "客服",
                "games": [self._game_pk()],
                "id_card_no": self.CARD,
                "display_name": "小王",
                "commission_value": "10.00",
                "grab_limit_dl": 0,
                "grab_limit_pw": 0,
                "hired_on": "2026-01-02",
                "deposit_amount": "0.00",
            },
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        self.assertEqual(resp.json()["data"]["account"]["id_card_no"], self.CARD)

    def test_the_admin_position_still_sees_the_real_values(self):
        """**管理员旁路**：职位持有者不该被限制型 flag 锁住（`D-0054`；2026-09-23 判据换职位）。

        ⚠️ **前置条件读库里 Role 的授权，不用 `has_node_now`**：后者对管理员**恒短路为真**，
        拿它当前置等于没有前置（E-0024 族）。
        """
        super_user = make_logged_in_client(
            username="mask-super", is_superuser=True, groups=(TENANT_ADMIN,)
        )
        self._grant(
            "system_user.restrict_view_id_card",
            "system_user.restrict_view_commission_params",
        )
        self.assertEqual(
            set(
                PermissionNode.objects.filter(
                    roles__name=TENANT_ADMIN,
                    code__in=[
                        "system_user.restrict_view_id_card",
                        "system_user.restrict_view_commission_params",
                    ],
                ).values_list("code", flat=True)
            ),
            {"system_user.restrict_view_id_card", "system_user.restrict_view_commission_params"},
            "前置条件：两条 flag 确实挂在「系统管理员」Role 上",
        )
        self.admin = super_user
        fields = self._detail()
        self.assertEqual(fields["id_card_no"], self.CARD)
        self.assertEqual(fields["commission_value"], 30.0)
        self.assertEqual(fields["base_salary"], 8000.0)

    def test_flag_revocation_takes_effect_for_a_freshly_loaded_viewer(self):
        """判定缓存挂在 `viewer` **对象**上：重新取一行 `auth_user` 后必须重算。

        ⚠️ **2026-09-23 口径变更**：原文走**两个真实 HTTP 请求**（管理员）来证明"缓存不跨请求"
        ——管理员现在走旁路 ⇒ 那条路观察不到脱敏。改成读取层：同一个账号，
        第二次用**重新取行**的 viewer（`_viewer_masks` 的缓存属性随对象走，
        HTTP 路径上每次请求正是这样重新取行的）。
        末段钉另一半：**同一个对象**仍带缓存（缓存确实挂在对象上）。
        """
        from apps.accounts.payloads import account_payload

        self._grant("system_user.restrict_view_id_card")
        viewer = self._non_admin_flag_viewer("system_user.restrict_view_id_card")
        self.assertEqual(account_payload(self.victim, viewer=viewer)["id_card_no"], "110***34")
        Role.objects.get(name=SERVICE).permissions.remove(
            *PermissionNode.objects.filter(code="system_user.restrict_view_id_card")
        )
        fresh = User.objects.get(pk=viewer.pk)
        self.assertEqual(account_payload(self.victim, viewer=fresh)["id_card_no"], self.CARD)
        self.assertEqual(
            account_payload(self.victim, viewer=viewer)["id_card_no"],
            "110***34",
            "缓存挂在 viewer 对象上——同一对象的第二次读取不该重算",
        )

    def test_list_path_stacking_list_masking_and_the_flag_is_idempotent(self):
        """列表路径上两层同时命中（`mask_pii=True` 且持 `restrict_view_id_card`）时，值不变。

        ⚠️ `_profile_fields` 会各调一次 `_mask_pii`——叠加不是漏洞：它对已脱敏值**幂等**。
        列表用例只**隐含**覆盖这条叠加路径，这里直调取值把它单独钉住（不经 HTTP）。
        正向对照用同一份载荷里的 `commission_mode`：证明拿到的确实是一份真载荷。
        ⚠️ **2026-09-23 口径变更**：viewer 改成**非管理员**持 flag 的账号——管理员走旁路，
        两层不会同时命中（原文用 `mask-admin`，那层 flag 已失效）。
        """
        from apps.accounts.payloads import account_payload

        self._grant("system_user.restrict_view_id_card")
        viewer = self._non_admin_flag_viewer("system_user.restrict_view_id_card")
        payload = account_payload(self.victim, mask_pii=True, viewer=viewer)
        self.assertEqual(payload["id_card_no"], "110***34")
        self.assertEqual(payload["commission_mode"], 20)


#: 跨租户用例的两个域（与 `tests/test_account_isolation.py` 同手法）。
DOMAIN_A = "tenant.test.com"
DOMAIN_B = "tenant-b.test.com"
B_SCHEMA = "tenantb"


class AccountNodeCrossTenantTests(TenantTestCase):
    """节点判据读的是**连接所在 schema**（`AGENTS.md` §4）：B 的授权不会漏到 A。

    ⚠️ **第二个租户必须在 `setUpClass` 建**：在一个用例内部建 schema 会与外层事务相冲
    （Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）——这是既有跨租户用例
    （`tests/test_account_isolation.py:96-111`）已经付过的学费，照抄它的形状。

    ⚠️ 判据用**租户内唯一、且两租户取值不同**的字段（用户名），不用主键——
    两个 schema 的主键序列各自从 1 开始（`AGENTS.md` §5 的踩坑条）。

    ⚠️ **判别力从哪来**：只断"在 B 里挂节点后 A 仍 403"会被"这个动作对谁都不放行"凑出来，
    所以末段配一条**正向对照**（把节点挂回 A ⇒ 必须 200）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.tenant_b, _ = provision_tenant(
            schema_name=B_SCHEMA, name="第三批跨租户租户 B", domain=DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线 ⇒ 不能依赖"恰好是本次刚建的"。
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # ⚠️ 第一行把连接显式钉回租户 A：schema 上下文会跨用例残留。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(username="cross-admin", groups=(TENANT_ADMIN,))
        self.role_a = Role.objects.get(name=TENANT_ADMIN)
        self.node_code = "system_user.list"

    def _grant_in_b(self, code):
        """在**租户 B 的 schema 内**给同名 Role 挂节点。

        ⚠️ 两条语句必须**一起**待在 `tenant_context(self.tenant_b)` 里：只把 `Role.objects.get`
        放进去、把 `PermissionNode.objects.get` 留在块外，读到的是 **A 的**节点行，
        挂到 B 的 Role 上是**跨 schema 关联**——那条用例本身就制造了它要证明"不存在"的跨租户写入。
        """
        with tenant_context(self.tenant_b):
            Role.objects.get(name=TENANT_ADMIN).permissions.add(
                PermissionNode.objects.get(code=code)
            )

    def test_grants_in_another_schema_do_not_open_this_tenants_door(self):
        """B 的授权不漏到 A——在**判据层**观察（2026-09-23 口径变更后 HTTP 层观察不到）。

        ⚠️ 原文用管理员 client 打 `/api/accounts/`：摘掉 A 的节点 ⇒ 403、在 B 里挂上后仍 403。
        判据统一后管理员**无条件**过节点门 ⇒ HTTP 层那对区分不出来（实测恒 200）。
        改钉判据本体：`user_node_codes` 读的是**连接所在 schema** 的 `Role`；对同一个账号
        在 A 上摘掉、再在 B 上挂上，A 的答案必须**不变**（末段配 A 侧挂回的正向对照）。
        """
        try:
            user = User.objects.get(username="cross-admin")
            self.role_a.permissions.remove(PermissionNode.objects.get(code=self.node_code))
            self.assertNotIn(self.node_code, user_node_codes(user))
            # 在**另一个 schema** 里（用 B 自己的节点行）给同名 Role 挂上该节点 ⇒ A 侧必须仍没有
            self._grant_in_b(self.node_code)
            self.assertNotIn(
                self.node_code, user_node_codes(user), "另一个租户的授权漏进了本租户"
            )
            # **正向对照**：把节点挂回 A ⇒ 必须出现（否则上面的"没有"可能是别的原因凑出来的）
            self.role_a.permissions.add(PermissionNode.objects.get(code=self.node_code))
            self.assertIn(self.node_code, user_node_codes(user))
        finally:
            # ⚠️ 跨 schema 的上下文切换不会自动还原到本用例的租户时，显式钉回 A，
            # 避免污染后续用例（`tests/test_account_isolation.py` 模块说明同源）。
            connection.set_tenant(self.tenant)


class AccountPermissionTableCoverageTests(TenantTestCase):
    """结构兜底：**路由上的每个 action 都必须在 `ACCOUNT_PERMISSIONS` 里有节点**。

    ⚠️ 为什么需要：`get_permissions` 对未登记的 action 回退到默认类（只有 `IsAccountManager`）
    ⇒ 将来给本视图加一个 `@action` 而忘了登记，那个动作就**静默绕开节点门**（fail-open 的形状）。

    ⚠️ **本条只钉"键集合"（每个 action 都登记过），⛔ 不钉"某个 action 挂的是哪条 code"**
    ——映射由 `AccountNodeGateTests.test_the_per_action_node_mapping_is_exactly_this_literal_table`
    的字面表钉住（评审 B1）。
    """

    def test_every_routed_action_has_a_node_entry(self):
        standard = ("list", "create", "retrieve", "update", "partial_update", "destroy")
        defined = {name for name in standard if hasattr(AccountViewSet, name)}
        extra = {fn.__name__ for fn in AccountViewSet.get_extra_actions()}
        self.assertEqual(
            set(AccountViewSet.ACCOUNT_PERMISSIONS), defined | extra,
            "节点表与视图实际提供的 action 不一致——新增 action 必须同时登记节点（否则它只要求管理员）",
        )
