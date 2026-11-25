"""角色权限接口（spec §7 / §10-A2）。"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django_tenants.test.cases import TenantTestCase

from apps.rbac.models import PermissionNode, Role
from apps.rbac.seed import seed_catalog
from apps.rbac.serializers import RoleCreateSerializer, RoleUpdateSerializer
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/role-authorities/"


class RoleAuthorityApiTests(TenantTestCase):
    def setUp(self):
        seed_catalog()
        # 用 superuser 做夹具：判据的判别力由探针单独证，
        # 本文件测的是**接口契约**，不该被权限拒绝干扰。
        # ⚠️ **不要用 `self.client.force_login(...)`**：它产出的会话里没有 `identity_plane`，
        # 会被 `TenantSessionGuardMiddleware` 与 `TenantBoundAuthentication` 按 fail-closed 不变式
        # 拒成 **401**（`src/tests/p5_auth_helpers.py` 的 docstring 有实测，spec §5.1-3 有红线）。
        # 一律走真实登录夹具 `make_logged_in_client`；它**当场创建**用户（`seed_tenant` 的 admin
        # 是 `set_unusable_password()`，登不进去）。
        # ⚠️ 判据统一后**不能**再"登录后手工置 `is_superuser`"：管理员判据是
        # 「持『系统管理员』职位」，裸超管过不了任何节点门。夹具的 `is_superuser=True`
        # 会顺手把账号加进该职位（见 `p5_auth_helpers` 的 docstring），这里改用它。
        self.client = make_logged_in_client(
            username="api-admin", password="pw-123456", is_superuser=True
        )

    def _create(self, name="客服", codes=("order.list",)):
        return self.client.post(
            BASE, {"name": name, "remark": "r", "permission_codes": list(codes)},
            content_type="application/json",
        )

    def test_list_is_not_paginated_and_has_no_permission_count(self):
        self._create()
        body = self.client.get(BASE).json()
        self.assertNotIn("items", body["data"], "本端点不分页（对标 page:false）")
        row = body["data"][0]
        self.assertEqual(row["name"], "客服")
        # （本线 D4）：该字段的前端消费方为零，后端为每行多付一次 `Count` JOIN
        # ⇒ 已删。它若回来，这里必须红。
        self.assertNotIn("permission_count", row)

    def test_create_writes_role_and_group(self):
        response = self._create()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertTrue(Group.objects.filter(name="客服").exists())

    def test_name_longer_than_five_is_a_business_error_not_500(self):
        # ⚠️ brief 原文这里用的是「客服主管长」——**恰好 5 个字**，也就是上限本身，
        # 所以它不是"超长"用例（实测：那一版拿到 code 0）。上限 5 来自模型
        # （`Role.name` max_length=5，spec §3.3 照抄对标表单），是要保住的一侧；
        # 采样值改成**真正的 6 个字**，让本用例的判别力落在"超长"上。
        response = self._create(name="超级客服主管")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_empty_permission_codes_is_rejected(self):
        response = self.client.post(
            BASE, {"name": "客服", "remark": "", "permission_codes": []},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 1000)

    def test_unknown_permission_code_is_a_business_error(self):
        response = self._create(codes=("nope.nope",))
        self.assertEqual(response.json()["code"], 1000)

    def test_detail_returns_permission_codes_for_the_dialog(self):
        role_id = self._create().json()["data"]["id"]
        body = self.client.get(f"{BASE}{role_id}/").json()
        self.assertEqual(body["data"]["permission_codes"], ["order.list"])

    def test_patch_updates_name_remark_and_permissions_atomically(self):
        role_id = self._create().json()["data"]["id"]
        response = self.client.patch(
            f"{BASE}{role_id}/",
            {"name": "客服主管", "remark": "x", "permission_codes": ["order.list", "order.delete"]},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        role = Role.objects.get(pk=role_id)
        self.assertEqual(role.name, "客服主管")
        self.assertEqual(role.group.name, "客服主管")
        self.assertEqual(role.permissions.count(), 2)

    def test_copy_clones_permissions_under_a_new_name(self):
        role_id = self._create().json()["data"]["id"]
        response = self.client.post(
            f"{BASE}{role_id}/copy/", {"name": "客服2", "remark": ""},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        clone = Role.objects.get(name="客服2")
        self.assertEqual(list(clone.permissions.values_list("code", flat=True)), ["order.list"])

    def test_patch_rolls_back_the_rename_when_permissions_are_rejected(self):
        """`update` 里改名与改权限**同一个事务**（本任务的裁决 ②，brief 已写）。

        **补的**（brief 原文没有这条用例）：只写"包在 `transaction.atomic()` 里"是断言不了的，
        得让它真的抛一次。构造：新名字合法（序列化器放行）、权限里塞一个未知 code
        （只有服务层的 `_resolve_nodes` 会发现）。`rename_role` 先跑且成功，
        `update_role_permissions` 再抛 —— 没有那层事务，库里就留下"改了一半"。
        """
        role_id = self._create().json()["data"]["id"]
        response = self.client.patch(
            f"{BASE}{role_id}/",
            {"name": "客服主管", "remark": "x", "permission_codes": ["nope.nope"]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        role = Role.objects.get(pk=role_id)
        self.assertEqual(role.name, "客服", "改名被提交了——事务没盖住两条写路径")
        self.assertEqual(role.group.name, "客服", "Group 改名被提交了——两张表都不许半改")
        self.assertEqual(role.remark, "r", "remark 也被提交了")
        self.assertEqual(list(role.permissions.values_list("code", flat=True)), ["order.list"])

    def test_create_without_remark_is_not_a_500(self):
        """**补的**（独立评审 Critical）：`remark` 在序列化器上是 `required=False` 且**没有** `default=`，
        而服务层 `create_role` 的 `remark` 是**必填 keyword-only** ⇒ `views.create` 的
        `**serializer.validated_data` 里**根本没有这个键**（DRF 对"可选且无默认值"的缺席字段抛
        `SkipField`）⇒ `TypeError` ⇒ **HTML 500**。

        `copy` / `update` 侥幸没中招（它们写的是 `.get("remark", "")`），而此前所有夹具都带 remark，
        所以这条路径无人覆盖。修法与账号线同形状：`remark` 给 `default=""`。
        """
        response = self.client.post(
            BASE, {"name": "客服", "permission_codes": ["order.list"]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertEqual(Role.objects.get(name="客服").remark, "")

    def test_patch_with_only_remark_keeps_name_and_permissions(self):
        """**补的**（独立评审裁决 ③：对齐账号线的 `partial` 语义）。

        `PATCH` 是**部分更新**：缺席的键一律保持库里的值。修复前 `RoleUpdateSerializer` 没有
        `partial=True` ⇒ 只发 `remark` 直接回 1000；而即使发了 name + permission_codes、
        只是**漏了 remark**，`views.update` 的 `remark=data.get("remark", "")` 会把备注**清成空**
        ——那条路径不是"语义张力"，是**静默数据丢失**。
        """
        role_id = self._create().json()["data"]["id"]
        response = self.client.patch(
            f"{BASE}{role_id}/", {"remark": "只改备注"}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0)
        role = Role.objects.get(pk=role_id)
        self.assertEqual(role.remark, "只改备注")
        self.assertEqual(role.name, "客服", "缺席的 name 必须保持库里的值")
        self.assertEqual(role.group.name, "客服", "Group 也不得跟着动")
        self.assertEqual(
            list(role.permissions.values_list("code", flat=True)), ["order.list"],
            "缺席的 permission_codes 必须保持原样——它不能被当成空集合",
        )

    def test_patch_without_remark_does_not_clear_it(self):
        """评审点名的**静默数据丢失**路径（裁决 ③ 的原始证据）：`PATCH` 带了 `name` +
        `permission_codes`、只漏了 `remark`。修复前 `remark=data.get("remark", "")` 会把备注
        清成空，而调用方拿到的是 `code 0`——"成功"地把一条数据弄丢了。
        """
        role_id = self._create().json()["data"]["id"]
        response = self.client.patch(
            f"{BASE}{role_id}/",
            {"name": "客服主管", "permission_codes": ["order.list", "order.delete"]},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0)
        role = Role.objects.get(pk=role_id)
        self.assertEqual(role.remark, "r", "缺席的 remark 被静默清空了")
        self.assertEqual(role.name, "客服主管")
        self.assertEqual(role.permissions.count(), 2)

    def test_empty_patch_is_a_noop(self):
        """空 `{}` PATCH 是**纯 no-op**（账号线的既有口径：没有要改的键就什么都不改）。"""
        role_id = self._create().json()["data"]["id"]
        response = self.client.patch(f"{BASE}{role_id}/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        role = Role.objects.get(pk=role_id)
        self.assertEqual((role.name, role.remark), ("客服", "r"))
        self.assertEqual(role.group.name, "客服")
        self.assertEqual(list(role.permissions.values_list("code", flat=True)), ["order.list"])

    def test_patch_with_explicit_empty_permission_codes_is_still_rejected(self):
        """**显式**给 `[]` 是**非法值**（不是"缺席"）：服务层的 `_resolve_nodes` 把它当
        "一个权限都没选"拒绝（`allow_empty=False` 在序列化器上先挡一次）⇒ 1000，且一行不改。
        """
        role_id = self._create().json()["data"]["id"]
        response = self.client.patch(
            f"{BASE}{role_id}/", {"permission_codes": []}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        role = Role.objects.get(pk=role_id)
        self.assertEqual(list(role.permissions.values_list("code", flat=True)), ["order.list"])

    def test_permission_rows_use_the_five_reference_columns(self):
        role_id = self._create(codes=("order", "order.list")).json()["data"]["id"]
        rows = self.client.get(f"{BASE}{role_id}/permissions/").json()["data"]
        self.assertEqual(set(rows[0]), {"name", "level", "parent_name", "note", "order_by"})
        module_row = next(r for r in rows if r["level"] == 0)
        self.assertEqual(module_row["parent_name"], "无", "顶级节点的 parent_name 是字符串『无』")

    def test_delete_removes_role_and_group(self):
        role_id = self._create().json()["data"]["id"]
        self.assertEqual(self.client.delete(f"{BASE}{role_id}/").status_code, 200)
        self.assertFalse(Role.objects.filter(pk=role_id).exists())
        self.assertFalse(Group.objects.filter(name="客服").exists())

    def test_unknown_role_id_is_a_business_error(self):
        response = self.client.get(f"{BASE}999999/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_non_numeric_role_id_is_a_business_error_not_500(self):
        """**补的**（brief 原文没有）：`[^/.]+` 让 `abc` 也能匹配上路由，而它会带着
        `ValueError: Field 'id' expected a number` 进 ORM ⇒ HTML 500（实测）。
        E-0010 的判据是"这类请求"，不是"这一个 id"，所以在这里钉住它。
        五个走 `_get_object` 的动作共用一个出口，逐条过一遍。
        """
        probes = [
            ("get", f"{BASE}abc/", None),
            ("patch", f"{BASE}abc/", {"name": "客服", "permission_codes": ["order.list"]}),
            ("delete", f"{BASE}abc/", None),
            ("post", f"{BASE}abc/copy/", {"name": "客服2"}),
            ("get", f"{BASE}abc/permissions/", None),
        ]
        for method, url, payload in probes:
            with self.subTest(method=method, url=url):
                if payload is None:
                    response = getattr(self.client, method)(url)
                else:
                    response = getattr(self.client, method)(
                        url, payload, content_type="application/json"
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)


class RoleSerializerFieldSetTests(TenantTestCase):
    """⭐ **2026-10-08 修复轮（F3.2）**：两个角色序列化器的可写字段集合**恰好**是那几个。

    ## 为什么必须有这一类（它守的判据此前**零网覆盖**）

    「这个端点只能改哪几个字段」原本由两条互补的东西守着：
    ① `apps/rbac/views.py::update` 的 R26 注释里那条指向 `RoleUpdateSerializer` 的
       `路径:行号` 引用（`tests/test_doc_line_references.py` 的第一层只钉「**没指飞**」）；
    ② 改写面时的**读码**。
    本线给那两个序列化器各加了一个字段（`can_be_sales_agent`）时，那条引用被改成**按类名指认**
    ⇒ 它**整体退出了那个网的语料**。
    而「字段集合恰恰好」这件事，**任何**网都没在钉：多一个第五字段、或少一个，
    只有人读码才发现。⇒ 这里补上真断言（⛔ **不是** `>=` / `issuperset`：那对"多一个字段"恒真）。

    ## 为什么断言打在**序列化器实例**上而不是端点响应上

    端点响应只回读面字段（`list` / `_detail` 的那几个键），它**看不见**「序列化器多收了一个
    可写字段」这件事——而后者才是「PATCH 能改到不该改的列」的入口。写入面的判据要打在写入面上。
    """

    def test_role_update_serializer_exposes_exactly_the_four_writable_fields(self):
        """`RoleUpdateSerializer`（`PATCH` / `PUT` 的写面）：**恰好**这四个，没有第五个。

        ⚠️ 期望值写成**字面量集合**（⛔ 不是 `set(RoleUpdateSerializer().fields)`，
        那是自指断言——E-0024）。
        """
        self.assertEqual(
            set(RoleUpdateSerializer().fields),
            {"name", "remark", "permission_codes", "can_be_sales_agent", "dashboard_type"},
        )

    def test_role_create_serializer_exposes_exactly_the_same_four_fields(self):
        """`RoleCreateSerializer`（`POST` 的写面）：**恰好**同样四个。

        两个序列化器**各自声明**同一组字段（本仓不用 Mixin 藏字段，见
        `apps/rbac/serializers.py` 的注释）⇒ 两条用例分开写：合并成一条会让
        「其中一个多收了一个字段」逃过去。
        """
        self.assertEqual(
            set(RoleCreateSerializer().fields),
            {"name", "remark", "permission_codes", "can_be_sales_agent", "dashboard_type"},
        )


class RoleSalesAgentFlagApiTests(TenantTestCase):
    """「可作为销售客服」标记（`Role.can_be_sales_agent`）的三条写入路 + 两个读面（2026-10-08 本线）。

    **为什么单独一类**：这个字段是本线**新增**的，而它的判据各自会红在不同地方——
    ① **新建**带它（`RoleCreateSerializer` + `create_role`）；
    ② `PATCH` 改它（`views.update` 的 `"can_be_sales_agent" in data` 判据）；
    ③ **复制**从**源角色**原样抄（`views.copy`）；
    ④ **读面**回它（`list` 行与 `_detail`）——前端编辑页的**回填**就靠 ④：少了它，
       编辑态每次保存都会按 payload 里的值把已勾上的标记改掉（静默改数据）。

    ⚠️ 「**没传**」与「**显式传 `False`**」是**两条**用例、⛔ 不合并（E-0021 那一族）：
    前者靠序列化器的 `required=False`（DRF 在 `partial=True` 下先抛 `SkipField`），
    后者是一个**值**。合并成一条时，把 `in data` 判据写成 `data.get(..., False)` 照样绿。
    """

    def setUp(self):
        seed_catalog()
        # 同 `RoleAuthorityApiTests`：管理员 = 持「系统管理员」职位（夹具的 `is_superuser=True` 顺手给组）。
        self.client = make_logged_in_client(
            username="flag-admin", password="pw-123456", is_superuser=True
        )

    def _create(self, name="标记角色", **extra):
        payload = {"name": name, "remark": "r", "permission_codes": ["order.list"]}
        payload.update(extra)
        response = self.client.post(BASE, payload, content_type="application/json")
        self.assertEqual(response.json()["code"], 0, response.content)
        return response.json()["data"]

    # ── ① 新建（含默认值那一对） ──────────────────────────────────
    def test_create_without_the_flag_stores_false(self):
        detail = self._create()
        self.assertIs(Role.objects.get(pk=detail["id"]).can_be_sales_agent, False)

    def test_create_with_the_flag_true_stores_true(self):
        detail = self._create("标记甲", can_be_sales_agent=True)
        self.assertIs(Role.objects.get(pk=detail["id"]).can_be_sales_agent, True)

    def test_create_with_the_flag_explicitly_false_stores_false(self):
        """与上一条**成对**：显式 `false` 是一个值，不是"没传"（⛔ 两条别合并成一条）。"""
        detail = self._create("标记乙", can_be_sales_agent=False)
        self.assertIs(Role.objects.get(pk=detail["id"]).can_be_sales_agent, False)

    # ── ② 更新 ────────────────────────────────────────────────────
    def test_patch_sets_and_clears_the_flag(self):
        role_id = self._create("标记丙")["id"]
        for value in (True, False):
            with self.subTest(value=value):
                response = self.client.patch(
                    f"{BASE}{role_id}/", {"can_be_sales_agent": value},
                    content_type="application/json",
                )
                self.assertEqual(response.json()["code"], 0, response.content)
                self.assertIs(Role.objects.get(pk=role_id).can_be_sales_agent, value)

    def test_patch_without_the_flag_keeps_the_stored_value(self):
        """**缺席即保持**（PATCH 语义）：一次与标记无关的 PATCH 不得把它清掉。

        判别力：把 `views.update` 的 `"can_be_sales_agent" in data` 改成
        `data.get("can_be_sales_agent", False)`（或无条件写一次）⇒ 本用例红——
        那正是「改个备注顺手把标记弄没」的静默数据丢失。
        """
        role_id = self._create("标记丁", can_be_sales_agent=True)["id"]
        response = self.client.patch(
            f"{BASE}{role_id}/", {"remark": "只改备注"}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertIs(Role.objects.get(pk=role_id).can_be_sales_agent, True)

    def test_put_without_the_flag_resets_it(self):
        """`PUT` = **全量替换** ⇒ 缺这个键落 `False`（与 `remark` 缺键落 `""` 是**同一个**既有口径）。

        ⚠️ 这是**登记过的**语义、不是意外：前端一律 `PATCH` 且每次都带这个键
        （`api/roleAuthority.ts` 的 `updateRoleAuthority`）。在这里钉住它，是为了让
        「哪天有人把 `PUT` 改成"缺席即保持"」变成一次**可见的**语义改动
        （那时本用例红，要求同一次改注释与前端），而不是悄悄发生的第六种语义。
        """
        role_id = self._create("标记戊", can_be_sales_agent=True)["id"]
        response = self.client.put(
            f"{BASE}{role_id}/", {"name": "标记戊", "permission_codes": ["order.list"]},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        self.assertIs(Role.objects.get(pk=role_id).can_be_sales_agent, False)

    # ── ③ 复制（从源角色抄） ──────────────────────────────────────
    def test_copy_clones_the_flag_from_the_source(self):
        """复制把标记**从源角色原样抄**（与"权限整份抄"同口径）；**两侧都要有**。

        ⚠️ **载荷里塞这个键不生效**（`RoleCopySerializer` 刻意不收它）：语义只有
        「从源角色抄」这一份判据，⛔ 别在这里补一个"载荷优先"的分支——那会让两侧中的
        一侧被**静默忽略**（`AGENTS.md` §5-E 的静默丢字段一族）。
        """
        marked_id = self._create("标记己", can_be_sales_agent=True)["id"]
        plain_id = self._create("标记庚")["id"]

        def _copy(source_id, name, **extra):
            payload = {"name": name, "remark": ""}
            payload.update(extra)
            response = self.client.post(
                f"{BASE}{source_id}/copy/", payload, content_type="application/json"
            )
            self.assertEqual(response.json()["code"], 0, response.content)
            return Role.objects.get(name=name)

        # 阳性：源带标记 ⇒ 副本带标记。
        self.assertIs(_copy(marked_id, "标记辛").can_be_sales_agent, True)
        # 阴性对照：源没有标记 ⇒ 副本也没有（否则"恒 True"的实现照样绿）。
        self.assertIs(_copy(plain_id, "标记壬").can_be_sales_agent, False)
        # 载荷里硬塞 `true` 也**不改**结果（源没有标记 ⇒ 副本仍没有）。
        self.assertIs(
            _copy(plain_id, "标记癸", can_be_sales_agent=True).can_be_sales_agent, False
        )

    # ── ④ 读面（形状恰好） ────────────────────────────────────────
    def test_detail_and_list_row_carry_the_flag(self):
        """两个读面各**恰好**多一个键，值就是库里那一列（两个方向都钉）。"""
        detail = self._create("标记子", can_be_sales_agent=True)
        role_id = detail["id"]
        self.assertIs(detail["can_be_sales_agent"], True)
        # 形状恰好：加键要**同一次**把读面契约改准，别让"多了一个没人知道的键"溜过去。
        self.assertEqual(
            set(detail), {"id", "name", "remark", "permission_codes", "can_be_sales_agent", "dashboard_type"}
        )
        self.assertTrue(
            self.client.get(f"{BASE}{role_id}/").json()["data"]["can_be_sales_agent"]
        )

        # 阴性对照：另一个**没**标记的角色在同一个读面上回 `False`
        # （否则上面那几条对"恒 True"的实现照样绿）。
        plain_id = self._create("标记丑")["id"]
        rows = self.client.get(BASE).json()["data"]
        row = next(r for r in rows if r["id"] == plain_id)
        self.assertIs(row["can_be_sales_agent"], False)
        self.assertEqual(set(row), {"id", "name", "remark", "can_be_sales_agent"})
        marked_row = next(r for r in rows if r["id"] == role_id)
        self.assertIs(marked_row["can_be_sales_agent"], True)


class RoleAuthorityCrossTenantTests(TenantTestCase):
    """AGENTS.md §4：本任务新增了对 `Role` 的查询（`selectors.list_roles` /
    `role_permission_rows` / 视图的 `_get_object`），必须补一条
    「以租户 A 的凭据访问 B 的数据必须失败」。

    ⚠️ **否定断言不靠主键指认"另一个租户的那一行"**（AGENTS.md §5：两个 schema 的主键
    各自从 1 开始，`filter(pk=1)` 在每个租户里都命中**它自己**的 1 号角色）：判据用
    **租户内唯一、且两租户取值不同**的 `Role.name`——A 自建的「甲角色」与 B 的内置「客服」。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 建 schema 是 DDL ⇒ 必须在 `setUpClass`（`TenantTestCase` 把每个测试包在事务里）。
        # **只写 `cls.tenant_b`**：`cls.tenant` / `cls.domain` 是基类 `tearDownClass`
        # 用来删它自己那个共享租户的，覆盖它们是 E-0015（`tests/test_rbac_me_payload.py` 同形）。
        from apps.platform.provisioning import provision_tenant

        cls.tenant_b, _ = provision_tenant(
            schema_name="rbac_api_b", name="租户 B", domain="rbac-api-b.test.local"
        )

    def setUp(self):
        seed_catalog()
        # 同 `RoleAuthorityApiTests`：管理员 = 持「系统管理员」职位（夹具的 `is_superuser=True`
        # 顺手给组，见 `p5_auth_helpers`）。
        self.client = make_logged_in_client(
            username="api-admin", password="pw-123456", is_superuser=True
        )

    def test_a_credential_cannot_see_b_roles(self):
        from django_tenants.utils import tenant_context

        # A 侧阳性对照：A 自己建的角色必须在 A 的列表里。
        created = self.client.post(
            BASE, {"name": "甲角色", "remark": "", "permission_codes": ["order.list"]},
            content_type="application/json",
        )
        self.assertEqual(created.json()["code"], 0)
        names_a = [row["name"] for row in self.client.get(BASE).json()["data"]]
        self.assertIn("甲角色", names_a)

        with tenant_context(self.tenant_b):
            # 先证明 B 里**确实**有角色：否则下面的"看不见"可能只是"B 是空的"（假通过）。
            # B 由 `provision_tenant` 建，内置角色由 `seed_tenant` 灌入。
            b_roles = set(Role.objects.values_list("name", flat=True))
            self.assertIn("客服", b_roles, "B 的内置角色没灌进来，否定断言会假通过")
            self.assertNotIn("甲角色", b_roles)
            # B 侧用户（在 B 的 schema 里创建，见夹具 docstring 的 E-0013）。
            # ⚠️ 夹具的 `is_superuser=True` 顺手把账号加进 B 的「系统管理员」职位
            # （判据统一后"管理员"就是它）。
            b_client = make_logged_in_client(
                domain="rbac-api-b.test.local",
                username="api-admin-b",
                password="pw-123456",
                is_superuser=True,
            )

        # B 的凭据 + B 的 Host 打到同一个端点：正向对照，证明连接真的切到了 B。
        names_b = [row["name"] for row in b_client.get(BASE).json()["data"]]
        self.assertIn("客服", names_b)
        self.assertNotIn("甲角色", names_b)
        # HTTP 层的否定断言：A 的列表里不得出现 B 的内置角色。
        # 这一条是活的判别器——A 从头到尾没灌内置角色，若查询跨了 schema，它必然出现。
        self.assertNotIn("客服", names_a, "A 的列表里出现了 B 的内置角色——跨租户泄漏")


class RoleAuthorityPermissionGateTests(TenantTestCase):
    """`ACTION_NODES` 的**可执行**归属核对：§7 的 8 个动作各自挂在自己的权限点上。

    **为什么需要它**（**补的**，brief 原文没有）：本文件其余用例全部用 superuser
    （`has_node_now` 对 `is_superuser` 短路为真），于是 `ACTION_NODES` 的映射
    **零判别力**——把任意一个动作从表里删掉、让它回落到 `DEFAULT_NODE`，其余用例照样全绿。
    这是本项目 E-0009 的同型形状（"测了动作，没测动作的落地机制"）。

    ⚠️ **独立评审 Important：初版只覆盖了 4 个动作的拒绝面**（create / PATCH / copy / destroy）；
    `list` / `retrieve` / `permissions` 只断言"持有节点时 200"（**这些动作即使不要求任何权限
    也照样绿**），`PUT` **一次都没被打过** ⇒ `ACTION_NODES["update"]` 零覆盖。
    本版按"**持有 ⇒ 放行** 且 **不持有 ⇒ 403**"两个方向把 8 个动作逐条钉住，
    并让 `_edit` 与 `_delete` 的**区分**可判（同一个用户打一个点放行、打另一个点 403）。

    夹具是**非** superuser（超管会被判据短路），每个动作一个只持有相关节点的角色。
    """

    #: 本类的角色与它们的权限点。名字都 ≤ 5（`Role.name` 的上限）。
    ROLE_SPECS = (
        ("入口角色", ["system.role_authority"]),
        ("预览角色", ["system.role_authority", "system.role_authority_view"]),
        ("编辑角色", ["system.role_authority", "system.role_authority_edit"]),
        ("删除角色", ["system.role_authority", "system.role_authority_delete"]),
        ("无关角色", ["order.list"]),
    )

    def setUp(self):
        seed_catalog()
        # 建靶子角色的人必须是管理员（判据 = 持「系统管理员」职位，见 `p5_auth_helpers`）。
        admin_client = make_logged_in_client(
            username="gate-admin", password="pw-123456", is_superuser=True
        )

        self.roles = {}
        for role_name, codes in self.ROLE_SPECS:
            response = admin_client.post(
                BASE,
                {"name": role_name, "remark": "", "permission_codes": codes},
                content_type="application/json",
            )
            self.assertEqual(response.json()["code"], 0, response.content)
            self.roles[role_name] = Role.objects.get(name=role_name)

        # 所有写动作的靶子。每个用例独立回滚，改名互不干扰。
        response = admin_client.post(
            BASE,
            {"name": "目标角色", "remark": "原备注", "permission_codes": ["order.list"]},
            content_type="application/json",
        )
        self.assertEqual(response.json()["code"], 0, response.content)
        self.target = Role.objects.get(name="目标角色")

    def _client_for(self, role_name, username):
        """建一个**非**超管并挂到 `role_name` 上（判据会被超管短路，所以这条断言不能省）。"""
        client = make_logged_in_client(username=username, password="pw-123456")
        user = get_user_model().objects.get(username=username)
        self.assertFalse(user.is_superuser, "夹具必须是**非**超管，否则判据被短路")
        user.groups.add(self.roles[role_name].group)
        return client

    def _assert_denied(self, response, label):
        # 无权限是**传输层**失败：HTTP 403 + code 403（`apps/core/exceptions.py`）。
        self.assertEqual(response.status_code, 403, label)
        self.assertEqual(response.json()["code"], 403, label)

    # ── 读动作 ─────────────────────────────────────────────────────
    def test_entry_node_holder_can_list_and_retrieve(self):
        # `list` / `retrieve` 都不在 ACTION_NODES 里 ⇒ 走 DEFAULT_NODE
        # `system.role_authority`，本夹具的角色**持有**它。
        client = self._client_for("入口角色", "gate-entry")
        self.assertEqual(client.get(BASE).status_code, 200)
        self.assertEqual(client.get(f"{BASE}{self.target.id}/").status_code, 200)

    def test_unrelated_node_holder_is_denied_on_every_read_action(self):
        """**补的**（评审 Important）：不持有入口节点 ⇒ 三个读动作**全部** 403。

        修复前只有"持有 ⇒ 200"这一侧：把 `get_permissions` 换成"无条件放行"，
        那些断言照样绿——它们测不到门。
        """
        client = self._client_for("无关角色", "gate-unrelated")
        self._assert_denied(client.get(BASE), "list")
        self._assert_denied(client.get(f"{BASE}{self.target.id}/"), "retrieve")
        self._assert_denied(client.get(f"{BASE}{self.target.id}/permissions/"), "permissions")

    def test_permissions_action_is_gated_by_the_view_node(self):
        # 持有 `_view` ⇒ 放行
        client = self._client_for("预览角色", "gate-view")
        self.assertEqual(client.get(f"{BASE}{self.target.id}/permissions/").status_code, 200)
        # 只持入口、**不**持 `_view` ⇒ 403（证明这个动作确实各挂各的点）
        entry = self._client_for("入口角色", "gate-entry-perm")
        self._assert_denied(entry.get(f"{BASE}{self.target.id}/permissions/"), "permissions")

    # ── 写动作 ─────────────────────────────────────────────────────
    def test_create_and_copy_are_gated_by_the_edit_node(self):
        # 不持有 `_edit`（只持入口 + 预览）⇒ 403
        denied = self._client_for("预览角色", "gate-view-write")
        self._assert_denied(
            denied.post(BASE, {"name": "别的角色", "permission_codes": ["order.list"]},
                        content_type="application/json"), "create")
        self._assert_denied(
            denied.post(f"{BASE}{self.target.id}/copy/", {"name": "别的角色"},
                        content_type="application/json"), "copy")
        # 持有 `_edit` ⇒ 两个动作都放行
        allowed = self._client_for("编辑角色", "gate-edit-create")
        self.assertEqual(
            allowed.post(BASE, {"name": "新建角色", "permission_codes": ["order.list"]},
                         content_type="application/json").json()["code"], 0)
        self.assertEqual(
            allowed.post(f"{BASE}{self.target.id}/copy/", {"name": "复制角色"},
                         content_type="application/json").json()["code"], 0)

    def test_put_and_patch_are_gated_by_the_edit_node(self):
        """**补的**（评审 Important）：`PUT` 此前**一次都没被打过** ⇒
        `ACTION_NODES["update"]` 零覆盖；删掉那一行就会退回 `DEFAULT_NODE`
        （"只持入口权限的人也能改名"）。
        """
        # 持有 `_edit` ⇒ PUT（全量）与 PATCH（部分）都放行
        allowed = self._client_for("编辑角色", "gate-edit-update")
        self.assertEqual(
            allowed.put(f"{BASE}{self.target.id}/",
                        {"name": "改名目标", "permission_codes": ["order.list"]},
                        content_type="application/json").json()["code"], 0)
        self.assertEqual(
            allowed.patch(f"{BASE}{self.target.id}/", {"name": "二次改名"},
                          content_type="application/json").json()["code"], 0)

        # 只持入口 ⇒ PUT / PATCH 都 403
        entry = self._client_for("入口角色", "gate-entry-update")
        self._assert_denied(
            entry.put(f"{BASE}{self.target.id}/",
                      {"name": "改名目标", "permission_codes": ["order.list"]},
                      content_type="application/json"), "PUT")
        self._assert_denied(
            entry.patch(f"{BASE}{self.target.id}/", {"name": "改名目标"},
                        content_type="application/json"), "PATCH")

    def test_delete_is_gated_by_the_delete_node(self):
        # 只持入口 + 预览 ⇒ 403，且一行不动（判据在 `get_permissions` 里，早于任何写路径）
        denied = self._client_for("预览角色", "gate-view-delete")
        self._assert_denied(denied.delete(f"{BASE}{self.target.id}/"), "destroy")
        self.assertTrue(Role.objects.filter(pk=self.target.pk).exists())
        # 持有 `_edit` 但**不**持 `_delete` ⇒ 仍 403（两个点不互相顶替）
        edit = self._client_for("编辑角色", "gate-edit-delete")
        self._assert_denied(edit.delete(f"{BASE}{self.target.id}/"), "destroy")
        # 持有 `_delete` ⇒ 放行，且角色与同名 Group 都真的没了
        allowed = self._client_for("删除角色", "gate-delete")
        response = allowed.delete(f"{BASE}{self.target.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        self.assertFalse(Role.objects.filter(pk=self.target.pk).exists())
        self.assertFalse(Group.objects.filter(name="目标角色").exists())

    def test_edit_and_delete_nodes_are_not_interchangeable(self):
        """`_edit` 与 `_delete` 的**区分**度（评审要求）：同一批用户，两个动作的答案**相反**。"""
        edit = self._client_for("编辑角色", "gate-distinct-edit")
        delete = self._client_for("删除角色", "gate-distinct-delete")
        # 持 `_edit`：改得了名，删不掉
        self.assertEqual(
            edit.patch(f"{BASE}{self.target.id}/", {"name": "改名目标"},
                       content_type="application/json").json()["code"], 0)
        self._assert_denied(edit.delete(f"{BASE}{self.target.id}/"), "edit→destroy")
        # 持 `_delete`：删得掉，改不了名
        self._assert_denied(
            delete.patch(f"{BASE}{self.target.id}/", {"name": "别的名字"},
                         content_type="application/json"), "delete→PATCH")
        self.assertEqual(delete.delete(f"{BASE}{self.target.id}/").status_code, 200)
