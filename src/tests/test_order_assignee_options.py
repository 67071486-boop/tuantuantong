"""指派弹层候选名单端点 `GET /api/business/orders/assignee-options/`（D-0146 方案②）。

⭐ **本线追加（业务方裁定 R1/R2，`feat/default-assign-hall`）**：`data` 顶层再多一个键
`defAssIsHall`（**五 → 六**）——默认档位是不是「内部抢单」。三条新用例在
`DefaultAssigneeHallReadTests`；契约与理由见 `docs/specs/2026-10-08-default-assign-hall-design.md`。

**为什么有这条线**：指派弹层原走 `GET /api/accounts/`，而那个端点的 `list` 判 `system_user.list`
（`apps/accounts/views.py` 的 `ACCOUNT_PERMISSIONS`）⇒ 持 `order.assign_taker` / `order.clear_taker`
但无该节点的角色（如「客服」）拉名单回 **403**、弹层里点不到人（现场实测见
`docs/specs/2026-09-24-e2e-golden-path-design.md` 的残差段；业务方裁定走方案②、
本次实施见 `docs/DECISIONS.md` 的 D-0146）。

⚠️ 夹具一律合成值（E-0077）；判据一律用**用户名 / 昵称 / 标记**这类可指认的字段。
⚠️ 本文件的核心不变式：**新端点的数据范围与 `GET /api/accounts/` 完全一致**——
那条判据（`test_the_person_set_matches_the_accounts_endpoint`）是「不许因为换了端点就放宽
『谁能被指派』的面」的承重判据。
"""
from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.order_writes import set_default_assign_preference
from apps.rbac.permissions import has_node_now
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

URL = "/api/business/orders/assignee-options/"
ACCOUNTS_URL = "/api/accounts/"
#: 职位下拉的数据源端点（`apps/accounts/views.py::AccountViewSet.roles`）——
#: 本批新增的 `roles` 键必须与它**同集合**（spec §4.6）。
ROLES_URL = "/api/accounts/roles/"

#: 前端弹层默认档（`order-orderlist.ts` 的 `ASSIGN_ROLE_DEFAULT`）。
ROLE_DEFAULT = "代练员"


def _account(username, *, role=None, first_name="", business_type=None, tag="", remark=""):
    """造一个候选账号：真实登录一次（让 `Role` 行按需建出来），再回填昵称与档案。

    ⚠️ `role` 传了才会建同名 `Group` + `Role`（`_grant_nodes` 的既有形状）——
    `MultiRoleField` 要求职位能**在租户 `Role` 表里查到**，所以拿 `role=` 筛的用例
    必须让候选真的持有那个职位。
    """
    make_logged_in_client(
        username=username,
        groups=(role,) if role else (),
        nodes=("order.list",),
    )
    user = User.objects.get(username=username)
    if first_name:
        user.first_name = first_name
        user.save(update_fields=["first_name"])
    if business_type is not None or tag or remark:
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        if business_type is not None:
            profile.business_type = business_type
        profile.tag = tag
        profile.remark = remark
        profile.save()
    return user


class AssigneeOptionsPermissionTests(TenantTestCase):
    """权限门：要的是**指派那一侧的节点**，不是 `system_user.list`。"""

    def setUp(self):
        self.taker = _account("测试打手甲", role=ROLE_DEFAULT, first_name="测试昵称甲")

    def test_an_assign_node_without_system_user_list_can_read_the_list(self):
        """① 持 `order.assign_taker`、**没有** `system_user.list` 的「客服」也能拿到名单。

        前置判据（⛔ 不许省）：这个账号**确实**没有 `system_user.list`——
        否则本用例可能在「它本来就能读账号列表」的情况下假通过。
        """
        client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=("order.assign_taker",)
        )
        viewer = User.objects.get(username="测试客服甲")
        self.assertFalse(has_node_now(viewer, "system_user.list"))
        response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertIn(self.taker.pk, [row["id"] for row in body["data"]["items"]])

    def test_a_clear_taker_only_role_can_read_the_list(self):
        """同一层弹层里还有「清除接单人」（`order.clear_taker`）⇒ 只持它也放行。"""
        client = make_logged_in_client(
            username="测试客服乙", groups=("客服",), nodes=("order.clear_taker",)
        )
        self.assertEqual(client.get(URL).status_code, 200)

    def test_a_role_without_any_assign_node_is_403(self):
        """② 没有任何指派节点的角色 ⇒ 403（权限是传输层的事，不是 200 + 业务码）。"""
        no_perm = make_logged_in_client(username="测试无权限甲", nodes=())
        self.assertEqual(no_perm.get(URL).status_code, 403)

    def test_anonymous_is_401(self):
        from rest_framework.test import APIClient

        anon = APIClient(SERVER_NAME="tenant.test.com")
        self.assertEqual(anon.get(URL).status_code, 401)


class AssigneeOptionsShapeTests(TenantTestCase):
    """③ 响应键**恰好**：`data` = `{items, truncated, roles, defAssRecManUid, defAssRecMan, defAssIsHall}`，
    每项 = `{id,name,role,tag,remark}`。"""

    def setUp(self):
        self.nick = _account(
            "测试打手甲",
            role=ROLE_DEFAULT,
            first_name="测试昵称甲",
            business_type=1,
            tag="标记甲",
            remark="备注甲",
        )
        self.no_nick = _account("测试打手乙", role=ROLE_DEFAULT)
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=("order.assign_taker",)
        )

    def test_the_response_keys_are_exactly_items_truncated_and_roles(self):
        """⭐ **契约变更（D-0146 → D-0147 批 E 任务 3 → D-0179 → 本线 D-0203）**。

        - `roles`（第三个键）= 指派弹窗的「用户职位」一组（D-0147 批 E 任务 3）；
        - `defAssRecManUid` / `defAssRecMan`（第四、五个键）= **录单页快速指派**
          的「默认指派此人」（D-0179，业务方裁定）——形状照对标
          `GetAllUserByGameName` 的同名键；
        - ⭐ `defAssIsHall`（第六个键）= **默认档位是不是「内部抢单」**（本线，业务方
          2026-10-08 裁定 R1/R2）：真 ⇒ 下次录单默认选中那一档（此时 `defAssRecManUid` 为 `0`）。
          ⚠️ **有意不抄对标的魔法值 `999999`**（它把「内部抢单」塞进 `defAssRecManUid` 本身）
          ⇒ 我们用一个语义化的布尔键说同一件事。

        ⛔ 这条**不是**放宽：它仍断言**恰好**六个顶层键（不许用 `toContain`），
        且候选项的行形状**仍恰好五个键**（新键只加在顶层——那条「不许把提成参数塞进行里」
        的口径见 `orders.ts::AssigneeOption` 与实施报告 §③）。
        """
        data = self.client.get(URL).json()["data"]
        self.assertEqual(
            set(data),
            {
                "items",
                "truncated",
                "roles",
                "defAssRecManUid",
                "defAssRecMan",
                "defAssIsHall",
            },
        )
        self.assertIsInstance(data["truncated"], bool)
        self.assertGreater(len(data["items"]), 0)
        for row in data["items"]:
            self.assertEqual(set(row), {"id", "name", "role", "tag", "remark"})

    def test_name_prefers_the_nickname_and_falls_back_to_the_login_name(self):
        by_id = {row["id"]: row for row in self.client.get(URL).json()["data"]["items"]}
        self.assertEqual(by_id[self.nick.pk]["name"], "测试昵称甲")
        self.assertEqual(by_id[self.no_nick.pk]["name"], "测试打手乙")

    def test_role_tag_and_remark_are_the_accounts_own_values(self):
        by_id = {row["id"]: row for row in self.client.get(URL).json()["data"]["items"]}
        row = by_id[self.nick.pk]
        self.assertEqual(row["role"], ROLE_DEFAULT)
        self.assertEqual(row["tag"], "标记甲")
        self.assertEqual(row["remark"], "备注甲")
        # 没有档案行的人：`tag` / `remark` 是空串（不是缺键），`role` 仍是他的职位名。
        blank = by_id[self.no_nick.pk]
        self.assertEqual(blank["tag"], "")
        self.assertEqual(blank["remark"], "")
        self.assertEqual(blank["role"], ROLE_DEFAULT)


class AssigneeOptionsRolesTests(TenantTestCase):
    """⑥ `roles` = 本租户**全部角色名**（内置 + 自建），与 `GET /api/accounts/roles/` 同源。

    观察者用租户管理员：他两侧都读得到，于是「两侧集合相等」不会因为夹具权限差异而变成假象。
    ⚠️ 两侧都要断言**非空**（否则「两边都空」也会让相等断言通过）。
    """

    def setUp(self):
        _account("测试打手甲", role=ROLE_DEFAULT)
        _account("测试陪玩乙", role="自建职")
        self.admin = make_logged_in_client(username="测试管理员甲", is_superuser=True)

    def test_the_roles_key_matches_the_accounts_roles_endpoint(self):
        mine = self.admin.get(URL).json()["data"]["roles"]
        theirs = self.admin.get(ROLES_URL).json()["data"]
        self.assertIsInstance(mine, list)
        self.assertTrue(all(isinstance(name, str) for name in mine))
        self.assertGreater(len(mine), 0, "assignee-options 的 roles 是空的")
        self.assertGreater(len(theirs), 0, "/api/accounts/roles/ 是空的")
        self.assertEqual(set(mine), set(theirs))
        # 内置角色与自建角色都在里面（不是只回了内置五个常量）。
        self.assertIn(ROLE_DEFAULT, mine)
        self.assertIn("自建职", mine)


class AssigneeOptionsMatchesAccountsTests(TenantTestCase):
    """④ 与老端点**同一个人集合**——「数据范围没变」的承重判据。"""

    def setUp(self):
        _account("测试代练甲", role=ROLE_DEFAULT, first_name="测试昵称甲", business_type=1)
        _account("测试代练乙", role=ROLE_DEFAULT, business_type=2)
        _account("测试陪玩丙", role="陪玩", business_type=1)
        # 老端点还要过**账号管理准入**（线 D是 `IsAccountManager`，不再是
        # `IsAccountAdmin`；= 系统管理员 **或** 持任一账号管理节点）+ 它自己的节点门
        # `system_user.list` ⇒ 用租户管理员当共同观察者：他两侧都能读，
        # 于是「两侧相等」不再是夹具权限差异造成的假象。
        self.admin = make_logged_in_client(username="测试管理员甲", is_superuser=True)

    def _ids(self, url):
        body = self.admin.get(url).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return {row["id"] for row in body["data"]["items"]}

    def test_the_person_set_matches_the_accounts_endpoint(self):
        for query in (
            f"?role={ROLE_DEFAULT}",
            "?key=测试昵称",
            "?business_type=1",
            f"?role={ROLE_DEFAULT}&key=测试代练乙&business_type=2",
        ):
            old_ids = self._ids(f"{ACCOUNTS_URL}{query}&page_size=200")
            new_ids = self._ids(f"{URL}{query}&limit=200")
            # 两侧都非空（防「两边都空」的假通过），且**相等**。
            self.assertGreater(len(old_ids), 0, query)
            self.assertEqual(old_ids, new_ids, query)


class AssigneeOptionsLimitTests(TenantTestCase):
    """⑤ `limit`：缺省 200、上界 500（超界 1000）、截断如实上报。"""

    def setUp(self):
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",), nodes=("order.assign_taker",)
        )

    def test_the_default_limit_is_200_and_truncation_is_reported(self):
        # 造出**多于 200** 个候选 ⇒ 「缺 limit 时恰好回 200 条 + truncated」才有判别力
        # （只有 3 个账号时，默认 200 与默认 500 的回包一模一样）。
        User.objects.bulk_create(
            [User(username=f"测试候选{i:03d}", password="!") for i in range(205)]
        )
        default = self.client.get(URL).json()["data"]
        self.assertEqual(len(default["items"]), 200)
        self.assertTrue(default["truncated"])
        explicit = self.client.get(f"{URL}?limit=200").json()["data"]
        self.assertEqual(
            [row["id"] for row in default["items"]],
            [row["id"] for row in explicit["items"]],
        )

    def test_a_tight_limit_truncates_and_a_roomy_one_does_not(self):
        User.objects.bulk_create(
            [User(username=f"测试候选{i:03d}", password="!") for i in range(3)]
        )
        tight = self.client.get(f"{URL}?limit=1").json()["data"]
        self.assertEqual(len(tight["items"]), 1)
        self.assertTrue(tight["truncated"])
        roomy = self.client.get(f"{URL}?limit=500").json()["data"]
        self.assertFalse(roomy["truncated"])

    def test_a_limit_over_the_cap_is_1000(self):
        self.assertEqual(self.client.get(f"{URL}?limit=501").json()["code"], 1000)


class DefaultAssigneeHallReadTests(TenantTestCase):
    """⭐ 本线新增（业务方 2026-10-08 裁定 R1/R2）：`defAssIsHall` 的三档读面。

    `defAssIsHall` = **操作人自己**的默认档位是不是「内部抢单」。三档各一条：
    有大厅发单权限并记着 hall ⇒ `True`（且 `defAssRecManUid` / `defAssRecMan` 归零，两列互斥）；
    记着某个人 ⇒ `False`；都没设 / 没有档案行 ⇒ `False`。
    ⚠️ 它**不是**「本租户的公共设置」——换一个客服登录读到的是**他自己**那一份。
    """

    def setUp(self):
        self.client = make_logged_in_client(
            username="测试客服甲", groups=("客服",),
            nodes=("order.assign_taker", "order.send_to_grab_hall"),
        )
        self.cs_user = User.objects.get(username="测试客服甲")
        self.taker = _account("测试打手甲", role=ROLE_DEFAULT, first_name="测试昵称甲")

    def _data(self, client=None):
        body = (client or self.client).get(URL).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    def test_the_hall_default_is_reported_as_true_with_a_zero_uid(self):
        set_default_assign_preference(self.cs_user, mode="hall")
        data = self._data()
        self.assertIs(data["defAssIsHall"], True)
        # 两列互斥 ⇒ 记着 hall 时「那个人」那两份必须是 0 / ''（⛔ 不是对标的 999999）
        self.assertEqual(data["defAssRecManUid"], 0)
        self.assertEqual(data["defAssRecMan"], "")

    def test_a_remembered_person_is_reported_as_false(self):
        set_default_assign_preference(self.cs_user, mode="person", taker=self.taker)
        data = self._data()
        self.assertIs(data["defAssIsHall"], False)
        self.assertEqual(data["defAssRecManUid"], self.taker.pk)
        self.assertEqual(data["defAssRecMan"], "测试昵称甲")

    def test_nothing_remembered_is_reported_as_false(self):
        data = self._data()
        self.assertIs(data["defAssIsHall"], False)
        self.assertEqual(data["defAssRecManUid"], 0)
        self.assertEqual(data["defAssRecMan"], "")

    def test_an_operator_without_a_profile_gets_false_instead_of_a_crash(self):
        """没有档案行（`seed_tenant` 的 `admin` 形态）⇒ `False`，不是 500、也不是缺键。

        ⚠️ 显式 `games=None` = **不建档案**（夹具的四条语义之一）。
        """
        maybe_no_profile = make_logged_in_client(
            username="测试无档案客服甲",
            groups=("客服",),
            nodes=("order.assign_taker",),
            games=None,
        )
        self.assertFalse(
            StaffProfile.objects.filter(user__username="测试无档案客服甲").exists()
        )
        data = self._data(maybe_no_profile)
        self.assertIs(data["defAssIsHall"], False)
        self.assertEqual(data["defAssRecManUid"], 0)

    def test_the_hall_default_belongs_to_the_operator_not_the_tenant(self):
        """⚠️ 主语是**录单的那个客服**：别人记着 hall，不该出现在我的读面里。"""
        other = make_logged_in_client(
            username="测试客服乙", groups=("客服",),
            nodes=("order.assign_taker", "order.send_to_grab_hall"),
        )
        set_default_assign_preference(User.objects.get(username="测试客服乙"), mode="hall")
        # 正向前置：那个账号自己读得到（否则下面那句 False 可能只是夹具没建出来）
        self.assertIs(self._data(other)["defAssIsHall"], True)
        self.assertIs(self._data()["defAssIsHall"], False)
