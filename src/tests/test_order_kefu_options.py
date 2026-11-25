"""「销售客服」候选端点 `GET /api/business/orders/kefu-options/`（2026-10-04 线，spec §2.1 / §3-1、2）。

**它回答什么**：订单管理页「销售客服」那一格的候选源。证据件里那一格是**空壳**
（`<select name="inputOrderKefu" …><option value="">选择或输入</option></select>`），
对标由运行期 JS 灌候选，本仓此前**从来没填过** ⇒ 下拉恒空。

判据（逐条对应 spec §3）：
① `items` 每行**恰好** `{id, name}`（恰好集合，⛔ 不是 `assertIn`）；
② 候选 = **带「可作为销售客服」标记（`Role.can_be_sales_agent=True`）的职位下的未软删账号
   ∪ 当前登录人**——用**恰好集合**钉住，并用「没有标记的职位下的人**不在**候选里」
   「软删账号**不在**候选里」两侧反证；
③ `name` = **昵称**（夹具的昵称与登录账号**不同形**，否则本判据会假绿）；
④ 截断：`limit` 给 N ⇒ 回 N 条且 `truncated=True`；
⑤ 权限门 = **本页自己的** `order.list`（对照半：同一 client 打 `GET /api/accounts/` 是 403）；
⑥ 同一个人**同时**持两个带标记的职位时，`items` 里那个 `pk` **只出现一次**
   （`groups__role__…` 是多值 join，去重是承重的）。

⚠️ **2026-10-08 本文件的夹具改过一份**（业务方当天裁定「候选按角色标记取」）：原来只要建出
`Group` 就够（口径写死两个角色名），现在**必须同时建出带标记的 `Role`**
（`Role.group` 是 OneToOne、反查路径 `groups__role__can_be_sales_agent`）——
只建 Group 的人**不进候选**。`_role(flag=…)` 是那件事的唯一实现。

⚠️ **「两处同源」怎么写进判据**：本端点（`GET /api/business/orders/kefu-options/`）**就是**
订单管理页筛选那一格与录单 / 编辑订单页那一格的**同一份候选**（两处的前端取数都是它，
不是各写一份）⇒ 「改一处两处一起变」的判据落在**端点载荷**上：候选里**有**带标记的自建角色
（阳性）与**没有**标记的角色（阴性）这两条，就是列表筛选那边会跟着看到的集合。
⚠️ 如实说清这条判据的**证明力边界**：它证明的是「**这一个端点**的名单按标记取」；
「前端两处都调这个端点」这半边今天没有判据守着（本仓的前端页测试不含这一条）。

⚠️ 夹具一律**合成值**（E-0077）；跨租户的否定断言用**用户名**指认，⛔ 不用主键（§5-G）。
⚠️ 不断言候选的**中文排序**（那取决于库的 collation，不是本仓的口径）——只钉集合、条数与
`truncated`；排序的确定性由「同一个查询里 `order_by("name", "pk")`」这一处实现保证。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business.models import Game, Order  # noqa: F401  （租户夹具的基线数据）
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.rbac.models import Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

URL = "/api/business/orders/kefu-options/"
ACCOUNTS_URL = "/api/accounts/"

#: 跨租户隔离用例的租户 B 域名 / schema（后缀 `.test.local` 在 `ALLOWED_HOSTS` 白名单里）。
DOMAIN_B = "order-kefu-options-b.test.local"
SCHEMA_B = "order_kefu_options_b"

# ── 合成夹具（昵称与登录账号**不同形**，spec §3-1③ 的判别力来源）──────────────────
KEFU_A_USERNAME = "kefu-zhang"
KEFU_A_NICKNAME = "测试昵称张"
LEAD_A_USERNAME = "kefu-lead"
LEAD_A_NICKNAME = "测试昵称主管"
TAKER_A_USERNAME = "dailian-wang"
TAKER_A_NICKNAME = "测试昵称打手"
DELETED_A_USERNAME = "kefu-deleted"
DELETED_A_NICKNAME = "测试昵称已删"
VIEWER_A_USERNAME = "dailian-viewer"
VIEWER_A_NICKNAME = "测试昵称浏览者"
#: 租户 B 的客服（用户名与 A 的全部不同形，N13 的否定断言才有效）。
KEFU_B_USERNAME = "kefu-b-chen"
KEFU_B_NICKNAME = "测试昵称B客服"
#: **租户自建**的客服角色（不在内置五职位里；`Role.name` 上限 5 字）。
CUSTOM_ROLE_NAME = "夜班客服"
CUSTOM_STAFF_USERNAME = "kefu-night"
CUSTOM_STAFF_NICKNAME = "测试昵称夜班"


def _role(name, *, flag):
    """建 / 取职位 `name` 并把它的人**能不能当销售客服**设成 `flag`（幂等）。

    **本文件 2026-10-08 之后的夹具唯一入口**：候选口径读的是 `Role.can_be_sales_agent`
    （`groups__role__can_be_sales_agent=True`），只建 `Group` 不给 `Role` 的人**不进候选**。
    ⚠️ `flag` 是**必填关键字参数**（⛔ 不给默认值）：本文件里「忘了设标记」与「故意不设」
    是两件相反的事，默认值会让后者静默变成前者（阳性用例照样绿、阴性用例变成恒真）。
    """
    group, _ = Group.objects.get_or_create(name=name)
    role, _was_created = Role.objects.get_or_create(
        name=name, defaults={"group": group}
    )
    Role.objects.filter(pk=role.pk).update(can_be_sales_agent=flag)
    return role


def _staff(username, nickname, *, group=None, role_flag=None, deleted=False):
    """建一个「当前租户里的」账号（合成值；可按需加职位 / 标记软删）。

    ⚠️ `group` 与 `role_flag` **成对使用**：给了 `group` 就必须显式声明这个职位的标记
    （见 `_role` 的必填理由）。`group` 为 `None` 时 `role_flag` 必须也是 `None`
    ——那种账号（当前登录人）走「∪ 当前登录人」那一半，与角色标记无关。
    """
    assert (group is None) == (role_flag is None), (
        "group 与 role_flag 必须成对给：候选口径读的是角色标记，只挂 Group 的账号不进候选"
    )
    user = User.objects.create_user(username=username)
    if nickname:
        user.first_name = nickname
        user.save(update_fields=["first_name"])
    if group is not None:
        _role(group, flag=role_flag)
        user.groups.add(Group.objects.get(name=group))
    StaffProfile.objects.create(
        user=user, deleted_at=timezone.now() if deleted else None
    )
    return user


class KefuOptionsTests(TenantTestCase):
    """① 契约 / ② 候选口径 / ③ 显示名 / ④ 截断 / ⑤ 权限门。"""

    def setUp(self):
        # 客服（内置角色「客服」）与客服主管（内置角色「客服主管」）：**带标记**、昵称与账号不同形。
        self.kefu = _staff(
            KEFU_A_USERNAME, KEFU_A_NICKNAME, group="客服", role_flag=True
        )
        self.lead = _staff(
            LEAD_A_USERNAME, LEAD_A_NICKNAME, group="客服主管", role_flag=True
        )
        # 反证半①：代练员**不带标记**（他既不是销售客服，也不是当前登录人）。
        self.taker = _staff(
            TAKER_A_USERNAME, TAKER_A_NICKNAME, group="代练员", role_flag=False
        )
        # 反证半②：软删掉的客服**不在**候选里。
        self.deleted = _staff(
            DELETED_A_USERNAME,
            DELETED_A_NICKNAME,
            group="客服",
            role_flag=True,
            deleted=True,
        )
        # 当前登录人：**不带标记**的职位（持 `order.list` 的是他）⇒ 命中「∪ 当前登录人」那一半。
        self.client = make_logged_in_client(
            username=VIEWER_A_USERNAME, groups=("代练员",), nodes=("order.list",)
        )
        self.viewer = User.objects.get(username=VIEWER_A_USERNAME)
        self.viewer.first_name = VIEWER_A_NICKNAME
        self.viewer.save(update_fields=["first_name"])

    def _data(self, url=URL):
        body = self.client.get(url).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return body["data"]

    # ── ① 契约形状 ────────────────────────────────────────────────────────────
    def test_rows_are_exactly_id_and_name(self):
        data = self._data()
        self.assertEqual(set(data), {"items", "truncated"})
        self.assertTrue(data["items"], "候选是空的：本用例的夹具没生效")
        for row in data["items"]:
            # **恰好集合**（⛔ 不是 `assertIn`）：多一个键（例如把内部字段漏出去）也要红。
            self.assertEqual(set(row), {"id", "name"})

    # ── ② 候选口径（恰好集合 + 两侧反证）──────────────────────────────────────
    def test_candidates_are_the_roles_marked_as_kefu_plus_the_current_user(self):
        data = self._data()
        names = {row["name"] for row in data["items"]}
        ids = {row["id"] for row in data["items"]}
        # 恰好集合：带标记的「客服」+「客服主管」+ 当前登录人（去重后**只有**这三个）。
        self.assertEqual(
            names, {KEFU_A_NICKNAME, LEAD_A_NICKNAME, VIEWER_A_NICKNAME}
        )
        self.assertNotIn(TAKER_A_NICKNAME, names)          # 代练员（无标记）不在
        self.assertNotIn(TAKER_A_USERNAME, names)
        self.assertNotIn(DELETED_A_NICKNAME, names)        # 软删账号不在
        self.assertNotIn(self.taker.pk, ids)
        self.assertNotIn(self.deleted.pk, ids)

    def test_a_role_marked_as_kefu_puts_its_people_in_the_candidates(self):
        """**阳性对照**：一个**租户自建**的角色（不在内置五职位里）勾上标记 ⇒ 它的人进候选。

        ⚠️ **判别力来自配对的那条阴性用例**（`test_a_role_without_the_mark_is_excluded…`）：
        只有两侧都在，「按标记取」才不是靠「候选集本来就大」假通过。
        ⚠️ 自建角色名（`CUSTOM_ROLE_NAME`）**不是**内置角色 ⇒ 本用例也顺带证明了
        「候选不再写死内置角色名」这件事（改动前的实现下，这条用例**红**）。
        """
        night = _staff(
            CUSTOM_STAFF_USERNAME,
            CUSTOM_STAFF_NICKNAME,
            group=CUSTOM_ROLE_NAME,
            role_flag=True,
        )
        by_id = {row["id"]: row["name"] for row in self._data()["items"]}
        self.assertEqual(by_id.get(night.pk), CUSTOM_STAFF_NICKNAME)

    def test_a_role_without_the_mark_is_excluded_even_if_it_is_named_kefu(self):
        """**阴性对照**：一个名字里带「客服」、但**没有标记**的自建角色 ⇒ 它的人**不在**候选。

        ⚠️ 这条同时挡两种坏实现：
        ① 把过滤条件换成「按角色名模糊 / 按名字里的关键词」——`夜班客服` 会被错误地放进来；
        ② 把 `Role.can_be_sales_agent` 的默认值改成 `True`——那时这个角色自己就带标记了，
           本条红（阴性那半失效）。
        ⚠️ 夹具是**同一个人**（`night`）在两个用例里进 / 出候选：`_role` 把它翻成 `False`
        之后必须**确实不在**——若它还在，说明候选读的不是标记（或读了别的宽松条件）。
        """
        night = _staff(
            CUSTOM_STAFF_USERNAME,
            CUSTOM_STAFF_NICKNAME,
            group=CUSTOM_ROLE_NAME,
            role_flag=False,
        )
        data = self._data()
        ids = {row["id"] for row in data["items"]}
        names = {row["name"] for row in data["items"]}
        self.assertNotIn(night.pk, ids)
        self.assertNotIn(CUSTOM_STAFF_NICKNAME, names)
        # 非空哨兵：带标记的那两个人**确实在**，否则上面两条会因「候选集本来就空」假绿。
        self.assertIn(KEFU_A_NICKNAME, names)
        self.assertIn(LEAD_A_NICKNAME, names)

    def test_the_logged_in_viewer_is_always_a_candidate_without_any_mark(self):
        """**当前登录人恒在**：他的职位没有标记（「代练员」= `False`）也照样进候选。

        ⚠️ 先自证夹具的**前提**（这个职位的标记确实是 `False`、他也不属于任何带标记的职位）：
        少了这两条，本用例在「把所有人的职位都置 `True`」的坏实现下也会绿。
        """
        self.assertFalse(
            Role.objects.get(name="代练员").can_be_sales_agent,
            "夹具前提不成立：当前登录人的职位带着标记，本条就证明不了「∪ 当前登录人」那一半",
        )
        self.assertEqual(
            set(self.viewer.groups.values_list("name", flat=True)), {"代练员"}
        )
        by_id = {row["id"]: row["name"] for row in self._data()["items"]}
        self.assertEqual(by_id.get(self.viewer.pk), VIEWER_A_NICKNAME)

    def test_a_soft_deleted_kefu_is_excluded_while_its_live_peer_is_not(self):
        """④ 的**对照半**：软删那个人不出现，而**没被删**的客服照旧出现。

        只有两侧一起断言，「软删排除」才不是靠「整个候选集本来就是空」假通过。
        """
        data = self._data()
        ids = {row["id"] for row in data["items"]}
        self.assertIn(self.kefu.pk, ids)
        self.assertNotIn(self.deleted.pk, ids)

    def test_a_person_holding_both_kefu_roles_appears_once(self):
        """一个人**同时**持两个带标记的职位 ⇒ `items` 里那个 `pk` **只出现一次**。

        这是今天补齐的钉子：`list_accounts` 的按职位筛是**多值 join**
        （见它 docstring 里「一个账号同时持两个被筛的职位时会重复出现」那条，靠末尾 `distinct()`
        收口），而 `kefu_options` 又在那上面叠了一个 `| Q(pk=viewer.pk)`
        ⇒ 两条路都可能在出口上产出重复行。判据打在**本端点的出口**（`id` 逐条唯一），
        而不是 `list_accounts` 自己那一层。
        ⚠️ **2026-10-08 改按角色标记之后这条仍然承重**：反查路径由 `groups__name__in` 变成
        `groups__role__can_be_sales_agent`（同样是多值 join），重复行的形态一字未变。
        """
        dual = _staff("kefu-dual", "测试昵称双职", group="客服", role_flag=True)
        dual.groups.add(Group.objects.get(name="客服主管"))
        ids = [row["id"] for row in self._data()["items"]]
        # 恰好集合的**多重集**半边：重复一次就红（`len(ids) == len(set(ids))`）。
        self.assertEqual(len(ids), len(set(ids)), f"同一个 pk 出现了多次：{ids}")
        # 同一个人的另一半：这个双职位的人**确实在**候选里（否则上面那条会因「他压根没出现」假绿）。
        self.assertEqual(ids.count(dual.pk), 1)

    # ── ③ 显示名 = 昵称（昵称与账号不同形）────────────────────────────────────
    def test_name_is_the_nickname_not_the_login_account(self):
        data = self._data()
        by_id = {row["id"]: row["name"] for row in data["items"]}
        self.assertEqual(by_id[self.kefu.pk], KEFU_A_NICKNAME)
        self.assertEqual(by_id[self.lead.pk], LEAD_A_NICKNAME)
        # 当前登录人也走同一口径（他的昵称同样与账号不同形）。
        self.assertEqual(by_id[self.viewer.pk], VIEWER_A_NICKNAME)
        # 反证：昵称**绝不是**登录账号（夹具不同形 ⇒ 这一条才判得动）。
        self.assertNotIn(KEFU_A_USERNAME, set(by_id.values()))
        self.assertNotIn(VIEWER_A_USERNAME, set(by_id.values()))

    def test_an_account_without_a_nickname_falls_back_to_the_login_name(self):
        """空昵称回落登录账号（`display_name_expression` 的 `NullIf(..., "")` 那一半）。"""
        no_nick = _staff("kefu-no-nick", "", group="客服", role_flag=True)
        by_id = {row["id"]: row["name"] for row in self._data()["items"]}
        self.assertEqual(by_id[no_nick.pk], "kefu-no-nick")

    # ── ④ 截断 ────────────────────────────────────────────────────────────────
    def test_limit_truncates_and_reports_it_both_ways(self):
        tight = self._data(f"{URL}?limit=1")
        self.assertEqual(len(tight["items"]), 1)
        self.assertTrue(tight["truncated"])
        roomy = self._data(f"{URL}?limit=500")
        self.assertEqual(len(roomy["items"]), 3)      # 客服 + 客服主管 + 当前登录人
        self.assertFalse(roomy["truncated"])
        # 上界照同族端点（`FilterOptionsSerializer.limit`）：超上限是**业务码 1000**，不是 500。
        over = self.client.get(f"{URL}?limit=501").json()
        self.assertEqual(over["code"], 1000)

    # ── ⑤ 权限门 ──────────────────────────────────────────────────────────────
    def test_the_gate_is_order_list_not_system_user_list(self):
        """权限门 = **本页自己的** `order.list`（spec §2.1 / §3-2）。

        ⚠️ **判别力来自对照半**：同一个「客服」client 打 `GET /api/accounts/`（判
        `system_user.list`）是 **403** ⇒ 它在 `kefu-options` 上拿到 200 **不可能**是因为
        「它本来就能读账号列表」。把门换成 `system_user.list`、或复用 `assignee-options`
        的并集（`order.assign_taker` / `order.submit_payout_price` / `order.clear_taker`）
        ⇒ 本条红。
        ⚠️ 那个 probe client 走 `make_logged_in_client`（它 `get_or_create` 同名 `Role`，
        **不碰标记**）⇒ 它复用 `setUp` 里带标记的那个「客服」角色，
        `Role.can_be_sales_agent` 保持 `True`（本用例的 200 与那份候选无关，
        这里只是不要让它因标记缺失而变成另一个坑）。
        """
        kefu_client = make_logged_in_client(
            username="kefu-gate-probe", groups=("客服",), nodes=("order.list",)
        )
        self.assertTrue(Role.objects.get(name="客服").can_be_sales_agent)
        self.assertEqual(kefu_client.get(URL).status_code, 200)
        # 对照半①：`assignee-options` 的门是指派动作的并集（`order.assign_taker` /
        # `order.submit_payout_price` / `order.clear_taker`）⇒ 这个「客服」拿到**真实 403**。
        # 复用那道门做本端点的门 ⇒ 本条红。
        self.assertEqual(
            kefu_client.get("/api/business/orders/assignee-options/").status_code, 403
        )
        # 对照半②：`GET /api/accounts/`（判 `system_user.list`，叠加 `IsAccountAdmin`）
        # 也读不到列表。⚠️ 本夹具的租户**零活跃管理员**（`TenantTestCase` 不跑 `seed_tenant`）
        # ⇒ 那次 403 被 `AccountViewSet.handle_exception` 翻成 **200 + 业务码 1003**
        # （已登记的既定行为）⇒ 判据钉**业务码不为 0**，⛔ 不钉 HTTP 码。
        self.assertNotEqual(kefu_client.get(ACCOUNTS_URL).json()["code"], 0)

    def test_the_endpoint_is_401_when_anonymous_and_403_without_the_node(self):
        from rest_framework.test import APIClient

        anon = APIClient(SERVER_NAME="tenant.test.com")
        self.assertEqual(anon.get(URL).status_code, 401)
        no_perm = make_logged_in_client(username="kefu-no-node", nodes=())
        self.assertEqual(no_perm.get(URL).status_code, 403)


class KefuOptionsCrossTenantIsolationTests(TenantTestCase):
    """跨租户：A 的候选集里**不出现** B 的客服，而 B 自己的候选集**非空**（两侧都要证）。

    ⚠️ **不用主键指认「另一个租户的那一行」**（`AGENTS.md` §5-G）：两个 schema 的主键序列各自
    从 1 开始 ⇒ B 的用户 id 在 A 里会撞上 A 自己的行。判据一律用**用户名 / 昵称**这类
    租户内唯一、且本例两租户取值不同的字段。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 第二个租户必须在 `setUpClass` 里建（用例内部的 schema DDL 与外层事务相冲，
        #    与 `tests/test_order_filter_options.py` 同一配方）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="客服候选隔离租户 B", domain=DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据 ⇒ 不能假定本次一定是新建。
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（schema 上下文会跨用例残留）。
        connection.set_tenant(self.tenant)
        # ⚠️ 租户 A 这边**不用** `seed_tenant`（`TenantTestCase` 只建空 schema）
        #    ⇒ 连内置角色的标记都得夹具自己给（`role_flag=True`）。
        self.kefu_a = _staff(KEFU_A_USERNAME, KEFU_A_NICKNAME, group="客服", role_flag=True)
        self.client_a = make_logged_in_client(
            username=VIEWER_A_USERNAME, groups=("代练员",), nodes=("order.list",)
        )
        with tenant_context(self.tenant_b):
            # ⚠️ 租户 B 不一样：它由 `provision_tenant` + `seed_tenant` 建 ⇒ 内置角色由
            #    `seed_builtin_roles()` 带着标记建出来（本线新建租户那条路径）。
            #    这里仍显式写 `role_flag=True`（幂等）：判据不依赖「种子已经跑过」这个前提。
            self.kefu_b = _staff(KEFU_B_USERNAME, KEFU_B_NICKNAME, group="客服", role_flag=True)
            self.client_b = make_logged_in_client(
                domain=DOMAIN_B,
                username="kefu-b-viewer",
                groups=("客服",),
                nodes=("order.list",),
            )

    def test_a_does_not_see_b_and_b_itself_is_not_empty(self):
        data_a = self.client_a.get(URL).json()["data"]
        names_a = {row["name"] for row in data_a["items"]}
        # 非空哨兵：A 自己那份候选**确实有人**，否则下面的否定断言会因「两边都空」假通过。
        self.assertIn(KEFU_A_NICKNAME, names_a)
        self.assertNotIn(KEFU_B_NICKNAME, names_a)
        self.assertNotIn(KEFU_B_USERNAME, names_a)

        # 另一侧自证：B 的候选集里**有** B 自己那个客服（他确实存在于 B）。
        data_b = self.client_b.get(URL).json()["data"]
        names_b = {row["name"] for row in data_b["items"]}
        self.assertIn(KEFU_B_NICKNAME, names_b)
        self.assertNotIn(KEFU_A_NICKNAME, names_b)
