"""账号端点：#1 列表、#3 详情（只读部分）、#8 职位下拉。按 brief「Task 4」的测试清单实现，并补上三条同类要求：R7——`mask_pii` 那一层是列表脱敏 / **不持限制型 flag 时**详情全量，**两个方向都要钉**（只测一个方向会放过「实现把详情也脱敏了」）；本类的账号**不持** flag，所以它钉的是 `mask_pii` 这一档；限制型 flag 是**另一层**、逐个读面生效，见 `apps/accounts/payloads.py::account_payload(viewer=)`。
§5.2 的 PII 表里三个字段（`id_card_no` / `bank_card_no` / `alipay_account`）**都要**覆盖（只测一个的话，另外两个的裁剪分支漏掉也照样全绿）；`roles` 动作的取值域是**当前租户的全部角色**（内置 + 自建），不是 `DEFAULT_ROLES` 常量——⚠️ **别在这里写死内置角色的个数**（原文是「内置 5 + 自建」）：内置清单的权威出处是 `apps/rbac/seed.py::BUILTIN_ROLE_MODULES`，与供给时的 `apps/business/roles.py::DEFAULT_ROLES` 不是同一份清单（前者多一个内置「人事」）。
夹具口径见 `tests/p5_auth_helpers.py`：登录必须走真实登录端点，`force_login()` 产出的会话没有 `identity_plane`，会被连接中间件按 fail-closed 拒掉。"""
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.db import IntegrityError, connection
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model

from apps.accounts import views as accounts_views
from apps.accounts.choices import BusinessType, MemberType
from apps.accounts.models import StaffProfile
from apps.accounts.serializers import AccountFilterSerializer
from apps.business.models import Game
from apps.business.roles import DEFAULT_ROLES, SERVICE, SUPERVISOR, TENANT_ADMIN, WORKER
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.rbac.models import Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()
DOMAIN = "tenant.test.com"

#: 前端「人员管理」岛页源码（前后端角色串格式对拍用）。
_FRONTEND_USER_LIST_TS = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "layui-island"
    / "pages"
    / "system-user-list.ts"
)

#: 前端 `buildUserListWhere` 把多选职位**原样透传**成 `role` 的那一行。
#: 容忍空白，但**不允许**出现 `split` / 取下标 / 取首个——那三种写法都是「丢一半职位」。
_ROLE_PASSTHROUGH_RE = re.compile(
    r"if\s*\(values\.jobRole\)\s*where\.role\s*=\s*values\.jobRole"
)

#: 非默认职位：用来证明 `roles` 动作只吐 DEFAULT_ROLES，不吐租户自建的 Group。
#: 非默认（自建）职位名：`roles` 下拉已列出它；本条**不建** `Role` 行 ⇒ 筛选仍报 1000。
CUSTOM_ROLE = "自建职位-账号线"

#: R13 / spec §3.2 / §4.2：新增载荷必须带齐的六个「必填，照抄」字段的合法取值。
#: 每个「新增成功」的载荷都从这里展开，避免六个字段在十几处各抄一份、漂移了也不知道。
CREATE_REQUIRED_PROFILE_FIELDS = {
    "display_name": "小王",
    "commission_value": "10.00",
    "grab_limit_dl": 0,
    "grab_limit_pw": 0,
    "hired_on": "2026-01-02",
    "deposit_amount": "0.00",
}


def make_account(username, *, role="客服", game=None, **profile_kwargs):
    """建一个带档案的账号。`profile_kwargs` 直接透传给 `StaffProfile`。"""
    user = User.objects.create_user(username=username, password="pw-123456")
    if role:
        user.groups.add(Group.objects.get(name=role))
    profile = StaffProfile.objects.create(user=user, **profile_kwargs)
    if game is not None:
        profile.games.add(game)
    return user


class SeededTenantTestCase(TenantTestCase):
    """带基线数据的租户测试基类（默认 Group 与 `admin`）。
    ⚠️ `seed_tenant` **不会**被 `TenantTestCase` 自动调用——`setUpClass` 只建 schema跑迁移（`django_tenants/test/cases.py`），默认 Group 与 `admin` 都不存在。不显式灌一次，`Group.objects.get(...)` 与 `User.objects.get(username="admin")`会以 `DoesNotExist` 报错。既有先例见 `tests/test_account_admin_predicate.py`。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        # 游戏放在类级：它与用例数量无关，逐条重建只是白跑写入。
        cls.game = Game.objects.create(name="王者荣耀")


class AccountApiTestCase(SeededTenantTestCase):
    """列表 / 详情 / 职位下拉用例的公共夹具。"""

    def setUp(self):
        self.admin = User.objects.get(username="admin")
        # ⚠️ `games=None`（夹具默认翻面后的显式 opt-out）：本账号只用来**发请求**，
        # 它此前**没有** `StaffProfile` ⇒ 有「按游戏筛账号」的用例（`AccountNewFilterTests`）
        # 依赖这一点：夹具新默认会给它建一份全量档案、让它混进 `?game=` 的结果里。
        self.client = make_logged_in_client(username="acct-admin", games=None)
        self.client_user = User.objects.get(username="acct-admin")
        # 夹具账号默认**不是**管理员；不给它「系统管理员」Group，下面每个用例都会 403。
        # 这同时证明了 spec §7.6 第 3 行（持 Group 者可管账号）在**端点层**成立。
        self.client_user.groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.kefu = make_account("kefu-1", role="客服", game=self.game)


class AccountFilterSerializationTests(SeededTenantTestCase):
    """筛选参数的**入口形状**：`QueryDict` 与普通 dict 在 DRF 里不等价。
    DRF 的 `Field.get_value` 对 `QueryDict` 走 `html.parse_html_dict`，把请求当HTML 表单解析——缺席的布尔字段被当成「未勾选的 checkbox」，于是`BooleanField` 收到 `{}` 而非 `empty`，`run_validation` 返回 `False`。表现：`AccountFilterSerializer(data=QueryDict("")).validated_data`凭空多出 `{'is_active': False}`，列表被静默过滤成「只看已停用」。
    ⚠️ 本类不是风格洁癖，而是**给后来者的判据**：本线第一次接 `AccountFilterSerializer`时就是这样把列表打成空集的（`views.py::AccountViewSet.list` 因此显式转 `dict()`）。新增查询参数序列化器时，把下面两条断言照搬到那个序列化器上。"""

    def test_empty_dict_yields_no_filters(self):
        """普通 dict 空输入 → 没有任何筛选条件（这是**调用方应当传的形状**）。"""
        params = AccountFilterSerializer(data={})
        self.assertTrue(params.is_valid())
        self.assertEqual(dict(params.validated_data), {})

    def test_querydict_is_parsed_as_an_html_form(self):
        """钉住 DRF 的既有行为：空 `QueryDict` → 凭空多出**五个**布尔键 `False`。
        （`is_active` / `settle_forbidden` / `deleted` / `is_studio` / `is_worker`：都不是「发送者没传」，而是 HTML 表单解析把缺席的 checkbox 当成未勾选。Task 3 新增 `settle_forbidden` 后，`views.list()` 里那句 `dict()` 转换**依然是必需的**——这条断言同时守住它。Task 8 加 `deleted`、D-0228 加 `is_studio` 时这个集合逐次 +1，**正因为**它们是同一台机器的产物。）
        这条断言**故意描述一个陷阱**，不是期望的功能：它把「为什么 views 必须先转`dict()`」变成一条可执行的说明。DRF 将来改了 `Field.get_value` 的 HTML 分支，本用例会失败——那时应连同 `views.py` 里那句转 dict 一起重新评估。"""
        from django.http import QueryDict

        params = AccountFilterSerializer(data=QueryDict(""))
        self.assertTrue(params.is_valid())
        self.assertEqual(
            dict(params.validated_data),
            {"is_active": False, "settle_forbidden": False, "deleted": False,
             "is_studio": False, "is_worker": False},
        )

    def test_absent_is_active_does_not_filter_the_list(self):
        """端到端出口断言：不带任何参数请求列表，两个**已启用**账号都必须在结果里。
        若 `views.py` 退回 `data=request.query_params`，本用例会拿到空列表（总条数 0），而不是只让上面那条序列化器用例失败——这正是AGENTS.md「断言打在真实出口上」的意思。"""
        # ⚠️ `games=None`（同上）：这个探针账号不在本用例的期望列表里，也不该有档案。
        client = make_logged_in_client(username="filter-probe", games=None)
        probe = User.objects.get(username="filter-probe")
        probe.groups.add(Group.objects.get(name=TENANT_ADMIN))
        body = client.get("/api/accounts/").json()
        # ⛔ spec §2.3：列表按 `-id` 最新在前——probe（后建）在 admin 之前。
        # 本条的本意（两个启用账号都在）不变，只把期望序列按新排序改写。
        self.assertEqual(
            [i["username"] for i in body["data"]["items"]], ["filter-probe", "admin"]
        )


class AccountListTests(AccountApiTestCase):
    def test_list_returns_envelope_with_pagination(self):
        """正向对照：管理员能读到列表，且形状是 {code,msg,data:{items,total,page,page_size}}。"""
        response = self.client.get("/api/accounts/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertIn("items", body["data"])
        self.assertIn("total", body["data"])
        # admin（seed 的超级用户）+ acct-admin（夹具）+ kefu-1
        self.assertGreaterEqual(body["data"]["total"], 3)

    def test_list_is_newest_first(self):
        """T2（spec §2.3，业务方 2026-10-16 拍板「子账户排序按照最新的在上面」）：
        新建两个账号 ⇒ 列表首页第一条是**新的**（`-id`）。"""
        make_account("older-1", role="客服", game=self.game)
        make_account("newer-1", role="客服", game=self.game)
        usernames = [
            i["username"] for i in self.client.get("/api/accounts/").json()["data"]["items"]
        ]
        # 夹具里 acct-admin / kefu-1 比这两个都早 ⇒ 新账号必须压在它们前面，
        # 且两者之间也是新的在前（对 `-id` 的两段断言，缺一不可）。
        self.assertLess(usernames.index("newer-1"), usernames.index("older-1"))
        self.assertLess(usernames.index("older-1"), usernames.index("acct-admin"))

    def test_every_item_has_no_password_field(self):
        items = self.client.get("/api/accounts/").json()["data"]["items"]
        self.assertTrue(items, "列表为空——测试自身失效")
        for item in items:
            self.assertNotIn("password", item)
            self.assertNotIn("initial_password", item)

    def test_list_carries_the_sort_settle_and_delete_keys(self):
        """列表响应带三个新键：排序值 / 禁止结算 / 删除时间（键名字面写死，E-0024）。"""
        make_account(
            "sorted-1", role="客服", game=self.game, order_by=7, settle_forbidden=True
        )
        items = self.client.get("/api/accounts/").json()["data"]["items"]
        row = next(i for i in items if i["username"] == "sorted-1")
        self.assertEqual(row["order_by"], 7)
        self.assertIs(row["settle_forbidden"], True)
        self.assertIsNone(row["deleted_at"])          # 没删 ⇒ null
        # 边界（E-0021）：默认值 0 / False 必须原样出现，不能被省略或写成 None。
        plain = next(i for i in items if i["username"] == "kefu-1")
        self.assertEqual(plain["order_by"], 0)
        self.assertIs(plain["settle_forbidden"], False)
        self.assertIsNone(plain["deleted_at"])

    def test_filter_by_role(self):
        make_account("worker-1", role=WORKER, game=self.game)
        body = self.client.get("/api/accounts/", {"role": WORKER}).json()
        usernames = [i["username"] for i in body["data"]["items"]]
        self.assertEqual(usernames, ["worker-1"])

    def test_filter_by_is_active(self):
        make_account("worker-1", role=WORKER, game=self.game)
        User.objects.filter(username="kefu-1").update(is_active=False)
        body = self.client.get("/api/accounts/", {"is_active": "false"}).json()
        usernames = [i["username"] for i in body["data"]["items"]]
        self.assertIn("kefu-1", usernames)
        self.assertNotIn("worker-1", usernames)

    def test_is_active_true_excludes_the_deactivated_account(self):
        """`is_active=false` 的正向对照：把 `true` 也测了，才排除「筛选被整个忽略」。
        只测 `false` 时，一个「永远返回全集」的实现也能过——因为断言是
        `assertIn` + `assertNotIn` 两种，但集合恰好只剩被停用的那个也能凑出来。"""
        User.objects.filter(username="kefu-1").update(is_active=False)
        body = self.client.get("/api/accounts/", {"is_active": "true"}).json()
        usernames = [i["username"] for i in body["data"]["items"]]
        self.assertIn("acct-admin", usernames)
        self.assertNotIn("kefu-1", usernames)

    def test_filter_by_key_matches_username_and_contact(self):
        StaffProfile.objects.filter(user__username="kefu-1").update(qq="998877")
        body = self.client.get("/api/accounts/", {"key": "998877"}).json()
        self.assertEqual([i["username"] for i in body["data"]["items"]], ["kefu-1"])

    def test_filter_by_key_matches_username_too(self):
        """`key` 的另一条分支（`username__icontains`）——只测 QQ 会漏掉它。"""
        body = self.client.get("/api/accounts/", {"key": "kefu-1"}).json()
        self.assertEqual([i["username"] for i in body["data"]["items"]], ["kefu-1"])

    def test_filter_by_role_does_not_duplicate_rows(self):
        """`groups__name` 是一对多 join：不 `distinct()` 时同一账号会出现多行。
        ⚠️ 判别力说明：本用例里每个账号只持一个 Group，`join` 恰好不产生重复行，所以它**不能**单独证明 `distinct()` 生效；`distinct()` 的判别力用例（持两个 Group 的账号）见 `test_two_group_account_appears_once`。"""
        make_account("worker-1", role=WORKER, game=self.game)
        body = self.client.get("/api/accounts/", {"role": "客服"}).json()
        usernames = [i["username"] for i in body["data"]["items"]]
        self.assertEqual(usernames, ["kefu-1"])

    def test_two_group_account_appears_once(self):
        """持两个 Group 的账号在按职位筛选时只出现一行。
        `groups__name=` 是等值条件，join 已经滤掉不匹配的 Group，一个账号只剩一行。
        本条是回归钉子，不是 `distinct()` 的变异判据。"""
        self.kefu.groups.add(Group.objects.get(name=WORKER))
        body = self.client.get("/api/accounts/", {"role": "客服"}).json()
        usernames = [i["username"] for i in body["data"]["items"]]
        self.assertEqual(usernames.count("kefu-1"), 1)

    # ── 「选择职位」是多选：`role` 接受逗号分隔的多个职位────────────
    def test_filter_by_two_roles_returns_both_and_the_correct_total(self):
        """`选择职位` 是多选：`role=客服,代练员` 必须按 **OR** 返回两个职位的账号。
        ⚠️ 修复前这条是红的：`AccountFilterSerializer.role` 当时是**单选** `ChoiceField`，逗号串被拒成 200 + 业务码 1000——用户勾两个职位就是「筛选一用就报错」。`total` 一并断言：分页层读的是 `count()`，重复行（见`test_account_holding_two_filtered_roles_appears_once`）会让它虚高。"""
        make_account("worker-1", role=WORKER, game=self.game)
        body = self.client.get("/api/accounts/", {"role": f"客服,{WORKER}"}).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        self.assertEqual(
            sorted(i["username"] for i in body["data"]["items"]), ["kefu-1", "worker-1"]
        )
        self.assertEqual(body["data"]["total"], 2)

    def test_one_invalid_role_in_a_comma_list_is_rejected_whole(self):
        """逗号串里**任何一个**职位非法 ⇒ 整条请求 200 + 1000（不放行另外半个）。
        两层判据：① 业务码 `1000`；② `data` 是 `None`——不能「只按合法的那一个筛、
        还回 200 + code 0」（那正是「静默放行半个」的形状）。"""
        make_account("worker-1", role=WORKER, game=self.game)
        response = self.client.get("/api/accounts/", {"role": f"{WORKER},不存在的职位"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertIsNone(response.json()["data"])

    def test_blank_items_in_a_comma_list_are_rejected(self):
        """空白项不放行：`"客服,"` / `",客服"` / `"客服,,代练员"` / `"客服, "` ⇒ 200 + 1000。
        选定口径（Task 3b brief 要求二选一后写死）：**先 strip 每一段，strip 后为空即整条拒绝**。不静默丢弃空项——丢弃会让 `"客服,"` 表现得与 `"客服"` 一样，调用方看不出自己发错了。值两侧的空白**容忍**（正向对照见下一条）。"""
        make_account("worker-1", role=WORKER, game=self.game)
        for raw in ("客服,", ",客服", f"客服,,{WORKER}", "客服, "):
            with self.subTest(role=raw):
                response = self.client.get("/api/accounts/", {"role": raw})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)

    def test_whitespace_around_a_role_name_is_tolerated(self):
        """正向对照：`"客服, 代练员"`（逗号后有空格）**不是**非法值。
        与上一条合起来口径唯一：先 strip，strip 后为空才拒。"""
        make_account("worker-1", role=WORKER, game=self.game)
        body = self.client.get("/api/accounts/", {"role": f"客服, {WORKER}"}).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        self.assertEqual(
            sorted(i["username"] for i in body["data"]["items"]), ["kefu-1", "worker-1"]
        )

    def test_account_holding_two_filtered_roles_appears_once(self):
        """一个账号**同时持两个被筛的职位** ⇒ 按这两个职位筛时只出现**一行**。
        ⚠️ 这条是 `distinct()` 的**变异判据**（回应 T3 自报的「distinct 未证明承重」）：`groups__name__in=[…]` 是**多值**条件，SQL 的 join 会为匹配到的每个 Group各产出一行 ⇒ 去掉 `qs.distinct()` 后本用例必须变红（`kefu-1` 出现两次、`total` 从 1 变 2）。上面那条**单值** `role=` 的用例做不到这一点（等值 join 只留一行），它是回归钉子而非变异判据。"""
        self.kefu.groups.add(Group.objects.get(name=WORKER))
        body = self.client.get("/api/accounts/", {"role": f"客服,{WORKER}"}).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        usernames = [i["username"] for i in body["data"]["items"]]
        self.assertEqual(usernames.count("kefu-1"), 1)
        self.assertEqual(body["data"]["total"], 1)

    def test_malformed_filter_returns_1000_not_500(self):
        """E-0010：请求来源的值（含查询参数）先过序列化器，`?is_active=maybe` 不是 500。"""
        response = self.client.get("/api/accounts/", {"is_active": "maybe"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_unknown_role_filter_returns_1000_not_500(self):
        """取值域 = 当前租户全部角色 + 请求期存在性校验：`?role=不存在的职位` 应被序列化器挡成 1000，而不是静默返回空列表或 500。"""
        response = self.client.get("/api/accounts/", {"role": CUSTOM_ROLE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_filter_accepts_a_custom_role(self):
        """筛选与表单**共用同一处校验** ⇒ 下拉里能选的，筛选也必须认。"""
        Role.objects.create(name="夜班客服", group=Group.objects.create(name="夜班客服"))
        body = self.client.get("/api/accounts/", {"role": "夜班客服"}).json()
        self.assertEqual(body["code"], 0)

    def test_filter_rejects_an_unknown_role(self):
        body = self.client.get("/api/accounts/", {"role": "根本没有这个职位"}).json()
        self.assertEqual(body["code"], 1000)

    def test_blank_is_active_is_treated_as_absent(self):
        """R8：`?is_active=` 按「没传」处理，不是非法值。前端清空「状态」下拉时会送 `?is_active=`（空串）。照直过序列化器的话`BooleanField.to_internal_value("")` 会拒 → 200 + code 1000，用户看到的是错误提示而不是（本该出现的）完整列表。
        **正向对照**：同一条用例末尾用非空的 `?is_active=false` 证明这个筛选**没被整个忽略**——所以「返回全集」只能来自空串被丢弃，而不是来自筛选条件失效。"""
        make_account("worker-1", role=WORKER, game=self.game)
        User.objects.filter(username="kefu-1").update(is_active=False)

        blank = self.client.get("/api/accounts/", {"is_active": ""})
        self.assertEqual(blank.status_code, 200)
        self.assertEqual(blank.json()["code"], 0)
        self.assertEqual(
            sorted(i["username"] for i in blank.json()["data"]["items"]),
            ["acct-admin", "admin", "kefu-1", "worker-1"],
            "空串没有被当成「没传」",
        )

        # 正向对照：同一个参数的**非空**值确实会筛。
        self.assertEqual(
            [i["username"] for i in
             self.client.get("/api/accounts/", {"is_active": "false"}).json()["data"]["items"]],
            ["kefu-1"],
        )

    def test_blank_role_is_treated_as_absent(self):
        """R8：`?role=`（清空「职位」下拉）按「没传」处理。
        正向对照同上：`?role=代练员` 仍必须只返回代练员账号。"""
        make_account("worker-1", role=WORKER, game=self.game)

        blank = self.client.get("/api/accounts/", {"role": ""})
        self.assertEqual(blank.status_code, 200)
        self.assertEqual(blank.json()["code"], 0)
        self.assertEqual(
            sorted(i["username"] for i in blank.json()["data"]["items"]),
            ["acct-admin", "admin", "kefu-1", "worker-1"],
            "空串没有被当成「没传」",
        )

        self.assertEqual(
            [i["username"] for i in
             self.client.get("/api/accounts/", {"role": WORKER}).json()["data"]["items"]],
            ["worker-1"],
        )

    def test_whitespace_only_role_is_rejected_not_treated_as_absent(self):
        """T3b 评审移交的**空白回归**（Task 8 收口）：`?role=%20` ⇒ **200 + 1000**。⛔ 改动前的行为是**静默返回全量**（`?role=%20` 与不传 `role` 结果一模一样）——那正是本仓最忌的「静默丢掉筛选条件」（`E-0004` / `E-0021` 同族）：用户以为按职位筛了，其实拿到的是全部账号。
        根因（读码 `rest_framework/fields.py:745-753` + 探针实测）：`CharField.run_validation` 在 `to_internal_value` **之前**就把「`data == ''` 或strip 后为空」短路成 `''` 返回 ⇒ 子类那个 `raw == ""` 分支**根本收不到全空白输入**。修法是把判据提到 `run_validation`。
        ⚠️ **与 `?role=` 的分工**：空串仍是「没传」（清空下拉是正常操作，`test_blank_role_is_treated_as_absent` 钉着）；**只有空白**才是错误。"""
        make_account("worker-1", role=WORKER, game=self.game)

        response = self.client.get("/api/accounts/", {"role": " "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["code"],
            1000,
            "只发空白等于发了个没意义的筛选——必须拒，不能静默返回全量",
        )

        # 判别力对照：同一个参数**非空**时确实会筛（证明上面不是「什么都拒」）。
        self.assertEqual(
            [i["username"] for i in
             self.client.get("/api/accounts/", {"role": WORKER}).json()["data"]["items"]],
            ["worker-1"],
        )

    def test_blank_param_does_not_clear_the_other_filters(self):
        """空串只丢它自己：`?is_active=false&role=` 里 `role` 空、`is_active` 仍然生效。
        没有这条，「把空串参数一律丢掉」与「一见到空串就清空所有筛选」两种实现
        长得一样——后者会让「清一个下拉」顺手把别的筛选也重置了。"""
        make_account("worker-1", role=WORKER, game=self.game)
        User.objects.filter(username="kefu-1").update(is_active=False)
        body = self.client.get(
            "/api/accounts/", {"is_active": "false", "role": ""}
        ).json()
        self.assertEqual(
            [i["username"] for i in body["data"]["items"]], ["kefu-1"]
        )

    def test_blank_key_is_accepted(self):
        """`?key=` 空串本来就被 `allow_blank=True` 放行——把既有行为钉住，防止两套口径。"""
        make_account("worker-1", role=WORKER, game=self.game)
        body = self.client.get("/api/accounts/", {"key": ""}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(len(body["data"]["items"]), 4)

    def test_malformed_pagination_params_do_not_500(self):
        """`page` / `page_size`（`EnvelopePagination` 的入参）也属于请求来源的值。
        判据同 E-0010：「**这类**请求还会不会 500」。`page` 走 `Paginator.page()`，非数字会被 DRF 翻成 `NotFound` → 200 + **1002**；`page_size` 走 `_positive_int`，非法值被 `contextlib.suppress(ValueError)` 吞掉，退回默认值 → 200 + **0**。两处**都不是** 500，但错误码不同——所以这里只断言「JSON 信封 + 非 500」，不替分页层规定码值。"""
        make_account("worker-1", role=WORKER, game=self.game)
        for query in ("page=abc", "page=0", "page=99999", "page=²",
                      "page_size=abc", "page_size=-1", "page_size=0", "page_size=²"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/accounts/?{query}")
                self.assertEqual(response.status_code, 200)
                # 正向对照：响应确实是本端点的 JSON 信封，而不是「路由没匹配上」的 HTML 404。
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertIn(response.json()["code"], (0, 1002))

    def test_non_admin_gets_403(self):
        """越权：客服不得读人员列表（§6.3）。正向对照见 `test_list_returns_envelope_with_pagination`。"""
        client = make_logged_in_client(username="kefu-2")
        kefu = User.objects.get(username="kefu-2")
        kefu.groups.add(Group.objects.get(name="客服"))
        response = client.get("/api/accounts/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)

    def test_anonymous_gets_401(self):
        """匿名读列表被拒。正向对照见 `test_list_returns_envelope_with_pagination`。"""
        from django.test import Client

        response = Client(SERVER_NAME=DOMAIN).get("/api/accounts/")
        self.assertEqual(response.status_code, 401)

    def test_delete_is_not_exposed(self):
        """D3：不暴露硬删 → 405（不是 200、不是 500）。
        ViewSet 用 `GenericViewSet` 而非 `EnvelopeModelViewSet`：后者带 `DestroyModelMixin`，router 会注册 `DELETE`，本断言就会变成 200。正向对照见列表/详情用例（同一ViewSet 的 GET 是通的，说明 405 来自「没有注册 DELETE」而非整个 ViewSet 不可用）。"""
        response = self.client.delete(f"/api/accounts/{self.kefu.pk}/")
        self.assertEqual(response.status_code, 405)

    def test_delete_does_not_remove_the_row(self):
        """405 之外再钉一次**落库事实**：被拒的 DELETE 不得改动数据库（E-0009 同一条纪律）。"""
        self.client.delete(f"/api/accounts/{self.kefu.pk}/")
        self.assertTrue(User.objects.filter(pk=self.kefu.pk).exists())


class FrontendBackendRoleContractTests(AccountApiTestCase):
    """前后端对拍：`选择职位` 的**前端拼串格式**与**后端解析格式**钉在同一条用例里。T3 评审指出「前端发什么、后端收什么」此前只有靠人读——没有一条测试同时对得上。这条「拼串」有两半，两半都必须被钉住：
    1. 分隔符是 `,`：vendored `public/vendor/formselects-v4.js` 的 `valStr` 分支就是`values.map(取 value).join(',')`，而页面 `form.val('sform')` 读的正是同一个hidden input 的值 ⇒ 前端发出去的就是 `"客服,代练员"` 这种**无空格逗号串**。这一半由本用例的 `ROLE_QUERY` **字面值**代表（前端侧另有一条 vitest 断言用同一个字面值执行 `buildUserListWhere`，见 `system-user-list.spec.ts`）。2. 页面 `buildUserListWhere` 把 `jobRole` **原样透传**成 `role`：不 split、不取第一个、不截断——本用例读前端源码确认这一点（`split` / 取下标都会变红）。
    两半合起来 + 把那个字面串喂给**真实端点**，才是「同时对得上两侧」的判据。"""

    #: 与前端 vitest 断言同一份字面串（`客服` = Group 名 `SERVICE`，`代练员` = `WORKER`）。
    ROLE_QUERY = "客服,代练员"

    def test_frontend_role_string_is_split_by_the_backend(self):
        source = _FRONTEND_USER_LIST_TS.read_text(encoding="utf-8")
        match = _ROLE_PASSTHROUGH_RE.search(source)
        self.assertIsNotNone(
            match,
            "前端 `buildUserListWhere` 不再把 `jobRole` 原样透传给 `role`"
            "（改成 split / 取第一个 / 截断都会打破本对拍）"
            f"，见 {_FRONTEND_USER_LIST_TS.name}",
        )
        make_account("worker-1", role=WORKER, game=self.game)
        body = self.client.get("/api/accounts/", {"role": self.ROLE_QUERY}).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        self.assertEqual(
            sorted(i["username"] for i in body["data"]["items"]), ["kefu-1", "worker-1"]
        )


class AccountNewFilterTests(AccountApiTestCase):
    """brief Task 3 的 7 个新筛选（游戏权限 / 业务类型 / 成员类型 / 是否实名 /禁止结算 / 搜索2 / 搜索3）。
    夹具口径：`make_account()` 建的账号**总带档案**，`StaffProfile` 的默认值是`business_type=1`（专做代练）、`member_type=0`（内部成员）、`settle_forbidden=False`、`id_card_no=""`。所以每个用例只需造「命中」的那一个，基类那几个默认账号就是反例。
    判定写在**真实出口**上：断言响应里返回的 `username`，不是 `list_accounts()` 的queryset（后者绕过序列化器与分页，E-0009）。"""

    def usernames(self, **params):
        body = self.client.get("/api/accounts/", params).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        return [i["username"] for i in body["data"]["items"]]

    # ── 游戏权限（`game` → `staff_profile__games__id`）──────────────────
    def test_filter_by_game(self):
        other = Game.objects.create(name="英雄联盟-按游戏筛")
        make_account("game-hit", role="客服", game=other)
        # 正向对照：`kefu-1` 持的是基类的 `self.game`（王者荣耀）⇒ 不命中。
        self.assertEqual(self.usernames(game=other.pk), ["game-hit"])

    def test_filter_by_game_does_not_duplicate_rows(self):
        """`games` 是多对多：一个账号持两款游戏、按其中一款筛时**只能出现一次**。
        判别力说明（如实）：`game__id=<值>` 是**等值**条件，SQL 的 join 本来就把不匹配的游戏行滤掉了，所以本条对当前过滤集**未必**能杀掉「去掉 `distinct()`」这一变异（见 `task-3-report.md` 的自查第 3 条实测）。它是一条回归钉子 + brief 要求的「一个账号有多款游戏时只出现一次」的出口断言，不是 `distinct()` 的变异判据。"""
        other = Game.objects.create(name="英雄联盟-去重用例")
        hit = make_account("game-two", role="客服", game=self.game)
        hit.staff_profile.games.add(other)
        self.assertEqual(self.usernames(game=other.pk), ["game-two"])
        names = self.usernames(game=self.game.pk)
        self.assertEqual(names.count("game-two"), 1)
        # 正向对照：这一款游戏确实还有另一个账号（否则上面那行可能只是因为结果为空）。
        self.assertEqual(sorted(names), ["game-two", "kefu-1"])

    # ── 业务类型（`business_type`）────────────────────────────────────
    def test_filter_by_business_type(self):
        make_account("bt-hit", role="客服", game=self.game,
                     business_type=BusinessType.COMPANION)
        self.assertEqual(self.usernames(business_type=BusinessType.COMPANION), ["bt-hit"])

    def test_business_type_zero_is_a_legal_value(self):
        """边界（E-0021）：`0` = 代练+陪玩 是合法枚举值，不能被当成「没传」丢掉。
        ⚠️ 两侧**都写字面值 `0`**（不引 `BusinessType.BOTH`，E-0024）：用常量的话，常量一旦不再等于 0，这条用例会**静默停止测边界**（评审 Minor 2）。正向对照：`0` 若被当成「没传」，`?business_type=1` 的结果里会混进 `bt-both`。"""
        make_account("bt-both", role="客服", game=self.game, business_type=0)
        self.assertEqual(self.usernames(business_type=0), ["bt-both"])
        self.assertNotIn("bt-both", self.usernames(business_type=1))

    # ── 成员类型（`member_type`）──────────────────────────────────────
    def test_filter_by_member_type(self):
        make_account("mt-hit", role="客服", game=self.game,
                     member_type=MemberType.PARTNER)
        self.assertEqual(self.usernames(member_type=MemberType.PARTNER), ["mt-hit"])

    def test_member_type_zero_is_a_legal_value(self):
        """边界（E-0021）：`0` = 内部成员 是合法枚举值，不能被当成「没传」丢掉。
        前端会真的发 `member_type:'0'`（`MEMBER_TYPE_FILTER_OPTIONS` 的第二项），所以这条与 `business_type=0` 是同一个**可达**形状（评审 Minor 3）。判据写字面值 `0`（不引 `MemberType.INTERNAL`，E-0024）：常量改名 / 改值后本用例必须自己变红。判别力：`mt-partner`（合作用户）是反例——真值写法 `if member_type:` 会把它一起返回。"""
        make_account("mt-zero", role="客服", game=self.game, member_type=0)
        make_account("mt-partner", role="客服", game=self.game,
                     member_type=MemberType.PARTNER)
        names = self.usernames(member_type=0)
        self.assertIn("mt-zero", names)
        self.assertNotIn("mt-partner", names)

    # ── 身份三分类（D-0228 口径：打手=持「代练员」职位且非工作室；工作室=勾 is_studio）──
    def test_filter_by_is_studio_true_returns_only_studios(self):
        """工作室档 = 显式勾了 is_studio（**职位与成员类型都不看**——显式标记优先）。"""
        make_account("studio-hit", role="代练员", game=self.game, is_studio=True)
        make_account("worker-plain", role="代练员", game=self.game)
        self.assertEqual(self.usernames(is_studio="true"), ["studio-hit"])

    def test_filter_by_is_studio_false_is_a_legal_value(self):
        """边界（E-0021，「打手」那一档的组成部分）：false = 只看**未**标工作室的账号，
        是显式取值——真值写法会把这一档静默丢成「不过滤」。
        判别力：`studio-hit` 在场且不被返回。"""
        make_account("studio-hit", role="代练员", game=self.game, is_studio=True)
        make_account("worker-plain", role="代练员", game=self.game)
        self.assertEqual(
            sorted(self.usernames(is_studio="false")), ["kefu-1", "worker-plain"]
        )

    def test_identity_tabs_split_by_worker_role_and_studio_flag(self):
        """三分类端到端（改准后的口径，业务方 2026-10-12 报「打手/工作室都 0」的修复判据）：- 打手 = 持「代练员」职位 且 is_studio=False——**成员类型是默认的内部成员也算**
        （真实形态：demo 库的代练员账号 member_type=0，此前按成员类型切档导致两档全 0）；- 工作室 = is_studio=True（显式标记优先，勾了就从打手档挪到工作室档）；- 内部员工 = 不持代练员职位 且 is_studio=False（kefu-1/客服/系统管理员）。"""
        # 真实形态复刻：代练员职位 + member_type 默认 0 + 未标工作室 ⇒ 打手档。
        make_account("who-taker", role="代练员", game=self.game)
        # 工作室：同一形态但勾了 is_studio ⇒ 工作室档（不在打手档）。
        make_account("who-studio", role="代练员", game=self.game, is_studio=True)
        # 内部员工：客服职位。
        make_account("who-kefu", role="客服", game=self.game)
        self.assertEqual(
            sorted(self.usernames(is_worker="true", is_studio="false")),
            ["who-taker"],
        )
        self.assertEqual(self.usernames(is_studio="true"), ["who-studio"])
        self.assertEqual(
            sorted(set(self.usernames(is_worker="false", is_studio="false"))),
            ["kefu-1", "who-kefu"],
        )

    def test_a_user_holding_worker_role_and_others_counts_as_worker(self):
        """边界：一人多职位时，「持代练员」即算打手（groups 是 OR 集合，不因兼任而消失）；
        `distinct()` 保证他只出现一次（先例：role 多选过滤的判据用例）。"""
        multi = make_account("multi-role", role=None, game=self.game)
        from django.contrib.auth.models import Group
        multi.groups.add(Group.objects.get(name="客服"), Group.objects.get(name="代练员"))
        names = self.usernames(is_worker="true", is_studio="false")
        self.assertEqual(names.count("multi-role"), 1)

    def test_list_payload_carries_is_studio(self):
        make_account("studio-payload", role="代练员", game=self.game, is_studio=True)
        item = next(
            i for i in self.client.get("/api/accounts/").json()["data"]["items"]
            if i["username"] == "studio-payload"
        )
        self.assertIs(item["is_studio"], True)

    # ── 是否实名（`is_cert`）──────────────────────────────────────────
    def test_filter_by_is_cert_y_returns_only_real_name_accounts(self):
        make_account("cert-hit", role="客服", game=self.game,
                     id_card_no="110101199001011234")
        self.assertEqual(self.usernames(is_cert="Y"), ["cert-hit"])

    def test_filter_by_is_cert_n_includes_accounts_without_a_profile(self):
        """`N` 的定义是「身份证号为空**或没有档案行**」——没有档案的人显然没实名。
        只测「有档案且号码为空」那一支会漏掉 `staff_profile__isnull=True` 那半边，
        而列表默认就把没有档案的账号一起吐出来（`admin` / `acct-admin` 都没有档案）。"""
        make_account("cert-hit", role="客服", game=self.game,
                     id_card_no="110101199001011234")
        User.objects.create_user(username="no-profile-1", password="pw-123456")
        names = self.usernames(is_cert="N")
        self.assertIn("no-profile-1", names)   # 没有档案行 ⇒ 未实名
        self.assertIn("kefu-1", names)         # 有档案但身份证号为空 ⇒ 未实名
        self.assertNotIn("cert-hit", names)    # 有身份证号 ⇒ 不是 N
        # 互补方向：同一个筛选值域的另一端也必须真的生效。
        self.assertNotIn("no-profile-1", self.usernames(is_cert="Y"))

    # ── 禁止结算（`settle_forbidden`）─────────────────────────────────
    def test_filter_by_settle_forbidden_true(self):
        make_account("frozen-1", role="客服", game=self.game, settle_forbidden=True)
        self.assertEqual(self.usernames(settle_forbidden="true"), ["frozen-1"])

    def test_filter_by_settle_forbidden_false_really_filters(self):
        """E-0021 形状：`false` 是**合法且有意义**的取值（只看未禁止结算的人）。
        真值写法（`if settle_forbidden:`）会把它当成「没传」⇒ `frozen-1` 也出现在
        结果里。变异证明（改真值 → 本用例变红 → 还原）见 `task-3-report.md`。"""
        make_account("frozen-1", role="客服", game=self.game, settle_forbidden=True)
        names = self.usernames(settle_forbidden="false")
        self.assertIn("kefu-1", names)
        self.assertNotIn("frozen-1", names)
        # 正向对照：同一条支路的另一端结果是互补的。
        self.assertEqual(self.usernames(settle_forbidden="true"), ["frozen-1"])

    # ── 搜索2（`key2`：支付宝账号 或 支付宝姓名）──────────────────────
    def test_filter_by_key2_matches_alipay_account(self):
        make_account("ali-acct", role="客服", game=self.game,
                     alipay_account="ali-888-account")
        self.assertEqual(self.usernames(key2="ali-888"), ["ali-acct"])

    def test_filter_by_key2_matches_alipay_name(self):
        make_account("ali-name", role="客服", game=self.game,
                     alipay_name="支付宝姓名-888")
        self.assertEqual(self.usernames(key2="姓名-888"), ["ali-name"])

    # ── 搜索3（`key3`：备注 或 标记）─────────────────────────────────
    def test_filter_by_key3_matches_remark(self):
        make_account("rm-hit", role="客服", game=self.game, remark="备注-关键字-ZZ")
        self.assertEqual(self.usernames(key3="关键字-ZZ"), ["rm-hit"])

    def test_filter_by_key3_matches_tag(self):
        make_account("tag-hit", role="客服", game=self.game, tag="标记-关键字-YY")
        self.assertEqual(self.usernames(key3="关键字-YY"), ["tag-hit"])

    # ── 非法值（E-0010：请求来源的查询参数先过序列化器）──────────────
    def test_new_filter_malformed_values_return_1000_not_500(self):
        """`?game=abc` / `?business_type=9` / `?settle_forbidden=maybe` / `?is_cert=X`
        一律 **200 + 业务码 1000**，不是 HTML 500，也不是静默返回全集。
        `game=0` 也在内：序号从 1 开始，`0` 不可能是合法主键（`min_value=1`）。"""
        for query in ("game=abc", "game=0", "business_type=9", "business_type=abc",
                      "settle_forbidden=maybe", "is_cert=X"):
            with self.subTest(query=query):
                response = self.client.get(f"/api/accounts/?{query}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(response.json()["code"], 1000)


class AccountDetailTests(AccountApiTestCase):
    def test_detail_returns_one_account(self):
        body = self.client.get(f"/api/accounts/{self.kefu.pk}/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["username"], "kefu-1")
        self.assertEqual(body["data"]["role"], "客服")
        self.assertEqual(body["data"]["games"], [self.game.pk])

    def test_detail_carries_the_sort_settle_and_delete_keys(self):
        """详情响应带三个新键，**未删行**的 `deleted_at` 是 `None`。
        默认详情排除已删的人。这里断言未删行的 `deleted_at is None`。
        软删和恢复的库行判据在 `tests/test_account_soft_delete.py`。"""
        data = self.client.get(f"/api/accounts/{self.kefu.pk}/").json()["data"]
        self.assertEqual(data["order_by"], 0)
        self.assertIs(data["settle_forbidden"], False)
        self.assertIsNone(data["deleted_at"])

    def test_detail_of_unknown_id_returns_1002_not_500(self):
        """不存在的 id → 业务码 1002（`NotFound` → `ErrorCode.BUSINESS`），HTTP 200。"""
        response = self.client.get("/api/accounts/999999/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)

    def test_detail_malformed_id_returns_1000_not_500(self):
        """E-0010 的同类：路径参数 id 不是**十进制数字**时不落到 ORM。
        `abc` 只是对照组。判别力在本组的后三个：`"²".isdigit()`、`"①".isdigit()` 都是 `True`（`str.isdigit` 认上标与带圈数字），而 `int()` 对它们抛 `ValueError`——所以 `isdigit()` 版本的守卫会**放它们过去**，落到 DRF 的 `get_object_or_404` 上得到 1002。这正是本用例要钉住的差异：同一个「非十进制 id」必须走**同一条** 1000 分支，而不是看字符碰巧属于哪一类。
        ⚠️ 如实口径：`isdigit()` 版本**不会**产生 HTML 500——`get_object_or_404` 显式把`TypeError` / `ValueError` / `ValidationError` 翻成 `Http404`（`rest_framework/generics.py:13-21`），再变成 200 + 1002。所以本用例断言的坏处是「错误码按字符类别分叉」，不是「500」。"""
        for pk in ("abc", "²", "①", "Ⅰ", "-1", "+1"):
            with self.subTest(pk=pk):
                response = self.client.get(f"/api/accounts/{pk}/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/json")
                self.assertEqual(response.json()["code"], 1000)

    def test_decimal_digits_from_other_scripts_are_still_looked_up(self):
        """`isdecimal()` 的边界：它认全部 Unicode 十进制数字，`int()` 也认——不得过度拒绝。
        `"١"`（阿拉伯-印度数字一）`isdecimal()` 为 `True`，`int("١") == 1`。本租户恰好有 1 号账号（`seed_tenant` 的 `admin`），所以它必须**取到那行**，而不是被守卫当成非法 id 拒掉。没有这条，一个「只认 ASCII `[0-9]`」的实现（例如 `pk.isascii() and pk.isdecimal()`）也能让上一条全绿。"""
        body = self.client.get("/api/accounts/١/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["username"], "admin")

    def test_detail_route_has_no_post(self):
        """详情路由只有 `GET` / `PUT` / `PATCH`：`POST` 不注册在这一层 → 405。
        ⚠️ 本用例原名 `test_detail_is_read_only_no_write_methods`，断言 `PUT` / `PATCH` 也是405。Task 6 交付 #4 编辑后它们是**已注册**的方法，那条断言随之作废（口径见`task-6-report.md`）——`PUT` / `PATCH` 能通由 `AccountUpdateTests` 正向钉住。`DELETE` 的 405 属于 D3，由 `test_delete_is_not_exposed` 单独钉。
        正向对照：同一个 URL 的 `GET` 是 200——所以 405 来自「这一层没有 POST」，而不是 URL 没匹配上（那会是 404）。"""
        response = self.client.post(f"/api/accounts/{self.kefu.pk}/", data={})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.client.get(f"/api/accounts/{self.kefu.pk}/").status_code, 200)

    def _role_of(self, username):
        user = User.objects.get(username=username)
        return self.client.get(f"/api/accounts/{user.pk}/").json()["data"]["role"]

    def test_role_is_deterministic_when_account_holds_several_groups(self):
        """一个账号意外持多个 Group 时，`role` 必须有确定的取值。
        `user.groups` 的取值顺序不受 `auth.Group.Meta` 约束（它没有 `ordering`），裸 `.first()` 于是依赖数据库返回顺序。这里给同一账号加两个 Group，反复取详情，断言取值稳定（5 次相同）——不稳定时本用例会看到两个值的集合。"""
        self.kefu.groups.add(Group.objects.get(name=WORKER))
        observed = {self._role_of("kefu-1") for _ in range(5)}
        self.assertEqual(len(observed), 1, f"role 取值不稳定：{observed}")

    def test_role_with_several_groups_matches_the_single_group_control(self):
        """多 Group 账号的 `role` 必须与「只持胜出职位」的对照账号取值一致。
        实现定的口径是**名称升序取首个**（见 `payloads._first_role_name`）：这里不预设中文字典序的结论，而是用两个单 Group 对照账号**量出**实现给每个职位的取值，再要求多 Group 账号等于其中升序在前的那一个。"""
        only_service = make_account("only-service", role=SERVICE, game=self.game)
        only_worker = make_account("only-worker", role=WORKER, game=self.game)
        by_role = {
            self.client.get(f"/api/accounts/{only_service.pk}/").json()["data"]["role"]: SERVICE,
            self.client.get(f"/api/accounts/{only_worker.pk}/").json()["data"]["role"]: WORKER,
        }
        self.assertEqual(set(by_role.values()), {SERVICE, WORKER})
        expected = min(by_role.values())          # 名称升序的首个

        self.kefu.groups.add(Group.objects.get(name=WORKER))
        self.assertEqual(self._role_of("kefu-1"), expected)

    def test_role_derivation_actually_discriminates_orderings(self):
        """判别力用例：夹具让**名称顺序**与 **Group pk 顺序**指向不同的职位。本用例存在的理由：`test_role_is_deterministic_when_account_holds_several_groups`与 `test_role_with_several_groups_matches_the_single_group_control` 都能被「按 group pk 取首个」的实现通过——`seed_tenant` 按 `DEFAULT_ROLES` 建 Group，`客服`(pk=1) 在 `客服主管`(pk=2) 之前，名称升序却先 `代练员`，两边不一致。
        ⚠️ 实测（别凭直觉挑夹具）：`.first()` / `user.groups.all()` **没有 `order_by`**（`auth.Group.Meta` 不带 `ordering`），顺序由数据库返回顺序决定。本环境实测到的返回顺序是 **group pk 升序**（`auth_group` 主键索引）：`客服`(1) → `客服主管`(2) →`代练员`(3)。这只是「本环境实测」，不是可依赖的契约——正因如此实现才不依赖它。本租户的默认职位 pk 为 `客服=1 客服主管=2 代练员=3 系统管理员=4 系统开发员=5`，而名称升序按 Unicode 码点，实测为 `代练员 < 客服 < 客服主管 < 系统开发员 < 系统管理员`——注意它**不是**拼音序，也不是 `DEFAULT_ROLES` 的顺序，所以这条不能靠推理，期望值一律从单 Group 对照账号的响应里**量**出来。
        做法：给一个空账号按 `客服主管 → 代练员 → 客服` 的顺序加三个 Group。两种口径分叉：- 数据库返回顺序（实测 pk 升序）→ `客服`（pk=1）；- 名称升序 → `代练员`。两者不同（用例里显式断言这一点），于是断言才有判别力。"""
        # 夹具前提：`seed_tenant` 按 `DEFAULT_ROLES` 的顺序建 Group，pk 随之递增。
        # 把这一句写出来，下面 `min(..., key=pk)` 才是可核对的（否则是隐式假设）。
        pk_by_name = {
            name: Group.objects.get(name=name).pk for name in DEFAULT_ROLES
        }
        self.assertEqual(
            [pk_by_name[name] for name in DEFAULT_ROLES],
            sorted(pk_by_name.values()),
            "默认职位的 pk 不再按 DEFAULT_ROLES 的顺序——本用例的夹具前提失效",
        )

        probes = {}
        for username, role in (("only-zhuguan", SUPERVISOR), ("only-dailian", WORKER),
                               ("only-kefu", SERVICE)):
            make_account(username, role=role, game=self.game)
            probes[role] = self._role_of(username)
        self.assertEqual(set(probes.values()), set(probes), "单 Group 对照账号取值串了")
        by_name = min(probes.values())                       # 名称升序
        by_pk = min(probes, key=lambda name: pk_by_name[name])  # group pk 升序
        self.assertNotEqual(
            by_name, by_pk,
            "夹具坏了：本夹具必须让名称序与 pk 序指向不同的职位，否则本用例没有判别力",
        )

        target = make_account("three-groups", role=None, game=self.game)
        for role in (SUPERVISOR, WORKER, SERVICE):
            target.groups.add(Group.objects.get(name=role))

        self.assertEqual(
            self._role_of("three-groups"), by_name,
            f"未定序的 `.first()` 口径会取到 {by_pk}；名称升序应取到 {by_name}",
        )


class AccountPiiMaskingTests(AccountApiTestCase):
    """R7：`mask_pii` 那一层——列表脱敏（spec §5.3），**不持限制型 flag 时**详情全量。**两个方向都要钉。**⚠️ 本类的账号**不持** `system_user.restrict_view_id_card` / `restrict_view_commission_params`⇒ 上面那句在这里成立；持 flag 的读取点是**另一层**（见 `payloads.py::account_payload(viewer=)`），不要把它读成"详情永远不脱敏"。
    §5.3 的表列了三个字段，所以三个字段逐个断言：只测 `id_card_no` 的话，`bank_card_no` / `alipay_account` 的裁剪漏做也照样全绿。"""

    #: 长度 10 → 脱敏后应是 前3 + "***" + 后2。
    CARD = "1234567890"
    ALIPAY = "alipay-acc-01"

    def setUp(self):
        super().setUp()
        StaffProfile.objects.filter(user=self.kefu).update(
            id_card_no=self.CARD, bank_card_no=self.CARD, alipay_account=self.ALIPAY
        )

    def _list_item(self, username="kefu-1"):
        items = self.client.get("/api/accounts/").json()["data"]["items"]
        return next(i for i in items if i["username"] == username)

    def test_list_masks_all_three_pii_fields(self):
        item = self._list_item()
        self.assertEqual(item["id_card_no"], "123***90")
        self.assertEqual(item["bank_card_no"], "123***90")
        self.assertEqual(item["alipay_account"], "ali***01")

    def test_list_masked_value_is_not_the_original(self):
        """脱敏的**反向**断言：列表里不得出现原值（只比自己拼的期望串会漏掉漏改造的情形）。"""
        item = self._list_item()
        for field, original in (
            ("id_card_no", self.CARD),
            ("bank_card_no", self.CARD),
            ("alipay_account", self.ALIPAY),
        ):
            with self.subTest(field=field):
                self.assertNotEqual(item[field], original)

    def test_detail_returns_pii_in_full(self):
        """R7 的另一半：编辑回填需要真值，**不持限制型 flag 时**详情**不得**脱敏（本类的账号不持 flag）。"""
        data = self.client.get(f"/api/accounts/{self.kefu.pk}/").json()["data"]
        self.assertEqual(data["id_card_no"], self.CARD)
        self.assertEqual(data["bank_card_no"], self.CARD)
        self.assertEqual(data["alipay_account"], self.ALIPAY)

    def test_empty_pii_stays_empty_in_list_and_detail(self):
        """空值仍为空串——不得变成 `"***"` 或 `"***"` 之类的假值。"""
        other = make_account("empty-pii", role="客服", game=self.game)
        item = self._list_item("empty-pii")
        self.assertEqual(item["id_card_no"], "")
        self.assertEqual(item["bank_card_no"], "")
        self.assertEqual(item["alipay_account"], "")
        detail = self.client.get(f"/api/accounts/{other.pk}/").json()["data"]
        self.assertEqual(detail["id_card_no"], "")
        self.assertEqual(detail["bank_card_no"], "")
        self.assertEqual(detail["alipay_account"], "")

    def test_short_pii_is_masked_whole(self):
        """短值（≤5）不能「保留前 3 后 2」——那会把 4 位的值整个原样吐出来。
        本用例把该口径钉死：短值整串掩成 `***`；同样的值在**不持限制型 flag 时**的详情里**仍然**是全量。"""
        short = make_account(
            "short-pii", role="客服", game=self.game,
            id_card_no="1234", bank_card_no="5678", alipay_account="ab",
        )
        item = self._list_item("short-pii")
        self.assertEqual(item["id_card_no"], "***")
        self.assertEqual(item["bank_card_no"], "***")
        self.assertEqual(item["alipay_account"], "***")
        # 详情不受 `mask_pii` 影响（同一条流水线的两个出口用不同口径）；
        # 限制型 flag 是**另一层**、连详情也会遮，见 `payloads.py::account_payload(viewer=)`。
        detail = self.client.get(f"/api/accounts/{short.pk}/").json()["data"]
        self.assertEqual(detail["id_card_no"], "1234")
        self.assertEqual(detail["bank_card_no"], "5678")
        self.assertEqual(detail["alipay_account"], "ab")

    def test_account_without_profile_has_no_profile_keys(self):
        """`auth.User` 没有档案行时（如 seed 的 `admin`），档案键**整个不出现**。
        这不是脱敏路径：`account_payload` 对 `profile is None` 返回 `{}`。
        列表与详情两处都必须如此，否则消费方拿到的「空档案」形状会随端点分叉。"""
        self.assertIsNone(getattr(self.admin, "staff_profile", None))
        item = self._list_item("admin")
        for key in ("id_card_no", "bank_card_no", "alipay_account"):
            self.assertNotIn(key, item)
        detail = self.client.get(f"/api/accounts/{self.admin.pk}/").json()["data"]
        for key in ("id_card_no", "bank_card_no", "alipay_account"):
            self.assertNotIn(key, detail)


class AccountRolesTests(AccountApiTestCase):
    def test_roles_endpoint_lists_tenant_roles(self):
        """2026-09-23（本线 D1）：取值域 = **当前租户的全部角色**（内置 + 自建）。
        ⚠️ 原文写「内置 5 + 自建」——线 D给内置清单加了「人事」之后那个数过期，
        所以这里去掉数：内置清单见 `apps/rbac/seed.py::BUILTIN_ROLE_MODULES`。"""
        Role.objects.create(name="夜班客服", group=Group.objects.create(name="夜班客服"))
        body = self.client.get("/api/accounts/roles/").json()
        self.assertEqual(body["code"], 0)
        self.assertIn("客服", body["data"])
        self.assertIn("夜班客服", body["data"])

    def test_roles_endpoint_includes_custom_roles(self):
        """原用例名是 `test_roles_endpoint_returns_exactly_the_five_default_roles`——它钉的是**被本线反转的旧口径**（D8 的范围裁剪）。反转后：**内置角色仍在**
        （今天几条以内置清单 `BUILTIN_ROLE_MODULES` 为准，⛔ 别在这里写死个数），自建的**必须也在**；且不得漏掉任何一个 `Role` 行。"""
        Role.objects.create(name="夜班客服", group=Group.objects.create(name="夜班客服"))
        data = self.client.get("/api/accounts/roles/").json()["data"]
        self.assertEqual(sorted(data), sorted(Role.objects.values_list("name", flat=True)))

    # ⭐ 原 `test_roles_endpoint_does_not_leak_custom_groups` 已删除 ——
    # 本线把「租户自建职位不得漏出」的口径**反转**为「必须列出」（spec D1 / §3.1）。

    def test_roles_requires_admin(self):
        """下拉也是管理员端点：客服取职位列表应 403。"""
        client = make_logged_in_client(username="kefu-3")
        User.objects.get(username="kefu-3").groups.add(Group.objects.get(name="客服"))
        response = client.get("/api/accounts/roles/")
        self.assertEqual(response.status_code, 403)


class AccountListQueryCountTests(AccountApiTestCase):
    def test_list_query_count_does_not_grow_with_rows(self):
        """列表不得有 N+1（brief 的硬约束：`staff_profile` / `groups` / `games` 都要预取）。
        做法：同一份夹具下把一个账号扩到 5 个，再比查询数——若 `groups` 或 `games`是逐行查询，查询数会随行数增长。这里用 `assertNumQueries` 的**相对**口径：先量 1 个账号，再量 6 个账号，两次必须相等。"""
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        with CaptureQueriesContext(connection) as baseline:
            response = self.client.get("/api/accounts/")
            self.assertEqual(response.json()["code"], 0)
        baseline_count = len(baseline)

        for index in range(5):
            make_account(f"bulk-{index}", role="客服", game=self.game)

        with CaptureQueriesContext(connection) as grown:
            response = self.client.get("/api/accounts/")
            self.assertEqual(response.json()["code"], 0)
        grown_count = len(grown)

        self.assertEqual(
            grown_count,
            baseline_count,
            f"列表查询数随行数增长（{baseline_count} → {grown_count}），说明存在 N+1",
        )


class AccountOpenerFilterTests(AccountApiTestCase):
    def test_opener_filter_rejects_invalid_id(self):
        body = self.client.get('/api/accounts/', {'opened_by': 'bad'}).json()
        self.assertNotEqual(body['code'], 0)

    def test_openers_requires_account_list_permission(self):
        client = make_logged_in_client(username='opener-no-permission', games=None)
        self.assertEqual(client.get('/api/accounts/openers/').status_code, 403)

    def test_opener_search_reports_limit_and_can_find_username(self):
        users = [User(username=f'bulk_opener_{n:03}', first_name='同名开户人') for n in range(101)]
        User.objects.bulk_create(users)
        users = list(User.objects.filter(username__startswith='bulk_opener_'))
        StaffProfile.objects.bulk_create([StaffProfile(user=u, opened_by=u) for u in users])
        data = self.client.get('/api/accounts/openers/', {'q': '同名开户人'}).json()['data']
        self.assertEqual(data['total'], 101)
        self.assertEqual(len(data['items']), 100)
        second = self.client.get('/api/accounts/openers/', {'q': '同名开户人', 'page': 2}).json()['data']
        self.assertEqual(len(second['items']), 1)
        self.assertEqual(second['items'][0]['username'], 'bulk_opener_100')
        specific = self.client.get('/api/accounts/openers/', {'q': 'bulk_opener_100'}).json()['data']
        self.assertEqual(specific['total'], 1)
        self.assertEqual(specific['items'][0]['username'], 'bulk_opener_100')


class AccountCrossTenantTests(AccountApiTestCase):
    """跨租户判别力用例（AGENTS.md §4 要求的那条）。
    ⚠️ 本类要证的不是「B 的 pk 在 A 里取不到」——那件事**不成立**：两个 schema 的 `auth_user` 自增序列各自从 1 开始，而 `seed_tenant` 会给**每个**租户灌一个 `admin`（`apps/platform/provisioning.py:54-57`），所以 id 天然重叠。要证的是：**同号的两个 id 各取各的 schema**——A 的 `pk=1` 永远是 A 的 `admin`，不会是 B 的任何一行。"""

    #: B 租户里的标记账号。名字带租户后缀，断言里直接找它，避免与 A 的同名账号混淆。
    B_USERNAME = "tenant-b-only-account"

    @classmethod
    def setUpClass(cls):
        # 第二个租户必须在 `setUpClass` 建（在一个用例内部建 schema 会与外层事务相冲），
        # 与 `tests/test_account_serializer.py::AccountWriteSerializerTests` 同一手法。
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="acct_api_b", name="账号接口租户 B", domain="acct-api-b.test.local"
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据，所以这里显式再调一次：
        # 不变量「B 也有 admin」是本类断言的前提，不能依赖 schema 是本次运行刚建的。
        seed_tenant(cls.tenant_b)

    def setUp(self):
        super().setUp()
        from django_tenants.utils import tenant_context

        # `auth_user` 的自增序列与 `seed_tenant` 无关地各自推进（事务回滚**不会**回退Postgres 序列），所以「B 的新账号恰好是 pk=2」不可依赖。这里**显式指派**同一个 pk：A 取一个空号，B 也把账号造成那个号——重叠由夹具制造，不由巧合凑出。
        # ⚠️ pk 必须在 `create_user` 时给：建成后再 `update(id=…)` 会把`auth_user_groups.user_id` 的外键悬空（实测 IntegrityError）。
        self.overlap_pk = (
            User.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
        ) + 1
        with tenant_context(self.tenant_b):
            # B 的 admin（`seed_tenant` 灌的基线）是本 schema 的第一行：pk=1。
            b_admin = User.objects.get(username="admin")
            self.assertEqual(b_admin.pk, 1)
            User.objects.filter(pk=self.overlap_pk).delete()
            User.objects.create_user(
                pk=self.overlap_pk, username=self.B_USERNAME, password="pw-123456"
            )
            self.assertTrue(
                User.objects.filter(pk=self.overlap_pk, username=self.B_USERNAME).exists(),
                "B 的标记账号没落到重叠 pk 上——本类的前提坏了",
            )
        User.objects.create_user(
            pk=self.overlap_pk, username="a-only-marker", password="pw-123456"
        )
        self.a_username_at_overlap = User.objects.get(pk=self.overlap_pk).username
        self.assertEqual(self.a_username_at_overlap, "a-only-marker")
        self.assertNotEqual(self.a_username_at_overlap, self.B_USERNAME)
        self.assertEqual(b_admin.pk, 1)
        # A 侧的反向前提：B 的标记账号在 A 的 schema 里**查不到**。
        # 没有这一条，「列表里没有它」也可能只是因为夹具根本没建出来。
        self.assertFalse(User.objects.filter(username=self.B_USERNAME).exists())
        self.assertTrue(User.objects.filter(pk=self.overlap_pk).exists(),
                        "A 里同号行不存在——重叠 pk 这条用例失去判别力")

    def test_list_does_not_contain_the_other_tenants_account(self):
        """(a) B 的账号不得出现在 A 的列表里。"""
        body = self.client.get("/api/accounts/").json()
        usernames = [i["username"] for i in body["data"]["items"]]
        # 正向对照：A 自己的账号确实在列表里（否则「没有 B 的账号」由空列表凑出来）。
        self.assertIn("admin", usernames)
        self.assertNotIn(self.B_USERNAME, usernames)

    def test_opener_filter_and_choices_stay_within_tenant(self):
        from django_tenants.utils import tenant_context

        opener_a = User.objects.get(pk=self.overlap_pk)
        StaffProfile.objects.create(user=self.client_user, opened_by=opener_a)
        with tenant_context(self.tenant_b):
            opener_b = User.objects.get(pk=self.overlap_pk)
            b_admin = User.objects.get(username="admin")
            b_profile, _ = StaffProfile.objects.get_or_create(user=b_admin)
            b_profile.opened_by = opener_b
            b_profile.save(update_fields=["opened_by"])
            self.assertEqual(b_profile.opened_by.username, self.B_USERNAME)

        choices = self.client.get('/api/accounts/openers/').json()
        self.assertEqual(choices['code'], 0)
        names = [row['name'] for row in choices['data']['items']]
        self.assertIn(self.a_username_at_overlap, names)
        self.assertNotIn(self.B_USERNAME, names)
        search = self.client.get('/api/accounts/openers/', {'q': 'a-only-marker'}).json()
        self.assertEqual([row['name'] for row in search['data']['items']], ['a-only-marker'])
        self.assertEqual(search['data']['total'], 1)
        result = self.client.get('/api/accounts/', {'opened_by': self.overlap_pk}).json()
        usernames = [row['username'] for row in result['data']['items']]
        self.assertIn('acct-admin', usernames)
        self.assertNotIn('admin', usernames)

    def test_detail_of_an_overlapping_pk_returns_this_tenants_row(self):
        """(b) 重叠 pk 取到的是**本租户**那一行——断言在值上，不是「恰好 404」。
        该 pk 在 B 里是 `tenant-b-only-account`、在 A 里是另一行。串号的话
        这里读到的 username 会是 `tenant-b-only-account`。"""
        detail = self.client.get(f"/api/accounts/{self.overlap_pk}/").json()
        self.assertEqual(detail["code"], 0, "重叠 pk 在 A 里必须能取到 A 自己的行")
        self.assertEqual(detail["data"]["username"], self.a_username_at_overlap)
        self.assertNotEqual(detail["data"]["username"], self.B_USERNAME)
        self.assertEqual(detail["data"]["id"], self.overlap_pk)

    def test_detail_of_the_other_tenants_admin_pk_returns_this_tenants_admin(self):
        """`admin` 在**每个**租户都存在（`seed_tenant`），pk 又都是 1——最容易串号的一格。
        A 的 `pk=1` 必须是 A 的 `admin`：超管标志与 `系统管理员` Group 都来自 A 的基线。"""
        detail = self.client.get("/api/accounts/1/").json()
        self.assertEqual(detail["code"], 0)
        self.assertEqual(detail["data"]["username"], "admin")
        self.assertTrue(detail["data"]["is_tenant_admin"])
        self.assertEqual(detail["data"]["role"], TENANT_ADMIN)

    def test_batch_status_cannot_reach_the_other_tenants_row(self):
        """(c) **写路径**的跨租户判别力（AGENTS.md §4）：批量启停打不到 B 的同号行。
        ⚠️ 用**重叠 pk**：那个 id 在两个 schema 里各有一行，所以「A 把 B 的账号停用了」在这一格上**可观测**——若 `status_batch` 的查询不绑定当前 schema，它会停用 B 的那一行，而 A 自己的那一行也会被停用（B 侧断言同时变红）。不重叠的 pk 让这条用例失去判别力（B 里压根没有那个号，怎么查都查不到）。"""
        from django_tenants.utils import tenant_context

        body = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.overlap_pk], "is_active": False},
            content_type="application/json",
        ).json()
        # 批量载荷只认**本租户**的行：重叠 pk 在这里取到的是 A 的 `a-only-marker`，
        # 于是整条请求成功——这正是「租户上下文只来自连接」的形状。
        self.assertEqual(body["code"], 0)
        self.assertFalse(User.objects.get(pk=self.overlap_pk).is_active)
        # B 侧：同号行**必须没被动过**。这是一条独立于 A 侧的环境断言。
        with tenant_context(self.tenant_b):
            b_row = User.objects.get(pk=self.overlap_pk)
            self.assertEqual(b_row.username, self.B_USERNAME)
            self.assertTrue(
                b_row.is_active,
                "A 的批量启停把 B 的同号行也改掉了——tenant 上下文没绑在连接上",
            )

    def usernames(self, **params):
        """本页用户名；**同时**断言 `total` 等于页内条数（评审 Minor 5）。
        本类夹具总共只有个位数账号，一页装得下 ⇒ `total == len(items)` 在这里是恒等式。它挡的是「`items` 没漏、但 `total`（即 `count()`）把 B 的行数也算进去」这种形状：跨租户泄漏在**计数**上和在**页内容**上是两个出口，只断一个会漏。"""
        body = self.client.get("/api/accounts/", params).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        names = [i["username"] for i in body["data"]["items"]]
        self.assertEqual(body["data"]["total"], len(names), "total 与页内条数不一致")
        return names

    def test_new_profile_filters_do_not_reach_the_other_tenants_rows(self):
        """brief Task 3 新增的 7 个筛选引入了 `staff_profile` 这一层的**新 join**（含多对多 `games`）⇒ 补一条跨租户判别力用例（AGENTS.md §4）。
        判据用**用户名**（租户内唯一、两租户取值不同），不用主键：两个 schema 的`auth_user` 自增序列各自从 1 开始，主键在两边天然重叠（见本类 docstring）。`game` 那一格用**显式同号**制造重叠：B 的游戏故意建成与 A 的同一个 pk，于是「查询没绑 schema」会让 B 的账号出现在 A 的筛选结果里。"""
        from django_tenants.utils import tenant_context

        with tenant_context(self.tenant_b):
            b_user = User.objects.get(username=self.B_USERNAME)
            b_game = Game.objects.create(pk=self.game.pk, name="B 的游戏-同号")
            b_profile = StaffProfile.objects.create(
                user=b_user,
                alipay_account="b-only-alipay",
                business_type=BusinessType.COMPANION,
                settle_forbidden=True,
                id_card_no="110101199001011234",
            )
            b_profile.games.add(b_game)

        # A 侧**复用 `setUp` 造的重叠 pk 行**（`a-only-marker`），不再新建账号：
        # 那个 pk 是显式指派进 `auth_user` 的，自增序列没跟着推进，再 `create_user`
        # 会撞 `auth_user_pkey`（实测 UniqueViolation）。给它挂上档案即可。
        a_user = User.objects.get(pk=self.overlap_pk)
        a_profile = StaffProfile.objects.create(
            user=a_user,
            alipay_account="a-only-alipay",
            business_type=BusinessType.COMPANION,
            settle_forbidden=True,
            id_card_no="110101199001011234",
        )
        a_profile.games.add(self.game)

        # 正向对照：A 自己那一行确实被这组筛选命中（否则「没有 B 的账号」可能由空集凑出）。
        self.assertEqual(self.usernames(key2="a-only-alipay"), [self.a_username_at_overlap])

        # 每个筛选值都同时命中 A 与 B 的账号（`b-only-alipay` 除外，它只命中 B）：
        # 查询若没绑当前 schema，`B_USERNAME` 就会出现在结果里。
        cases = (
            {"game": self.game.pk},
            {"business_type": BusinessType.COMPANION, "settle_forbidden": "true"},
            {"is_cert": "Y"},
            {"key2": "alipay"},
        )
        for params in cases:
            with self.subTest(params=params):
                names = self.usernames(**params)
                self.assertNotIn(self.B_USERNAME, names)
        self.assertEqual(self.usernames(key2="b-only-alipay"), [])
        # `key3=备注`：A / B 两边都没填 ⇒ 结果**为空**（不是「恰好不含 B」）。
        # ⚠️ 原注释写「结果为空」而断言只是 `assertNotIn(B)`——两者不符且恒真；
        # 现在注释与断言一致：列表为空 + 助手同时钉住 `total == 0`。
        self.assertEqual(self.usernames(key3="备注"), [])


class AccountUpdateTests(SeededTenantTestCase):
    """#4 编辑：`PUT/PATCH /api/accounts/<id>/`。
    ⚠️ 对 brief 的三处偏离（都按 binding 裁决 / R5，逐条见 `task-6-report.md`）：1. **继承 `SeededTenantTestCase`，不是 `TenantTestCase`**（R5）：后者不跑 `seed_tenant`，brief 夹具里的 `Group.objects.get(TENANT_ADMIN)` 会 `DoesNotExist`；`game` 也改从基类取（类级建一次，不必逐条用例重建）。2. `test_cannot_take_another_users_username` 断言 **1001**（裁决 R6 与 spec §5.1/§7.4：重名是一条**可区分**的错误，create 与 update 不能两套口径）；brief 那里写的 1000 作废。同时补了「改成自己的 username 必须成功」的正向对照。3. 夹具的 `target` 带几个**有值**的档案字段（`qq` / `commission_value` / `deposit_amount`）——没有它们，「未发的键保持库里的值」就没有可断言的差异（默认值恰好等于「被重置」的值）。
    本类同时覆盖 Task 6 的 A 项：每个 PATCH 用例都断言**未发的键在库里没变**，而不只是断言响应（AGENTS.md「断言打在真实出口上」）。"""

    #: 夹具的档案值（一个 CharField + 两个 DecimalField）。都**不是**模型默认值
    #: ——否则「未被改动的键保持了库里的值」与「被重置成默认值」在断言里长得一样。
    QQ = "13500000000"
    COMMISSION = Decimal("7.50")
    DEPOSIT = Decimal("3.00")

    def setUp(self):
        self.client = make_logged_in_client(username="upd-admin")
        User.objects.get(username="upd-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.target = make_account(
            "edit-target", role="客服", game=self.game,
            qq=self.QQ, commission_value=self.COMMISSION, deposit_amount=self.DEPOSIT,
        )

    def _patch(self, payload):
        return self.client.patch(
            f"/api/accounts/{self.target.pk}/", data=payload, content_type="application/json"
        )

    def _put(self, payload):
        return self.client.put(
            f"/api/accounts/{self.target.pk}/", data=payload, content_type="application/json"
        )

    def _profile(self):
        return StaffProfile.objects.get(user=self.target)

    def test_partial_update_changes_only_given_fields(self):
        body = self._patch({"display_name": "改名了"}).json()
        self.assertEqual(body["code"], 0)
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "改名了")
        self.assertEqual(self.target.groups.first().name, "客服")   # 职位未被清掉
        # ↓ A 项：只发了 `display_name`，其余每一个字段都必须还是库里的值。
        #   逐字段打在**数据库行**上（响应可能只回一部分键）。
        profile = self._profile()
        self.assertEqual(profile.qq, self.QQ)
        self.assertEqual(profile.commission_value, self.COMMISSION)
        self.assertEqual(profile.deposit_amount, self.DEPOSIT)
        self.assertEqual(list(profile.games.values_list("pk", flat=True)), [self.game.pk])
        self.assertEqual(list(self.target.groups.values_list("name", flat=True)), ["客服"])
        self.assertTrue(self.target.is_active)
        self.assertTrue(self.target.check_password("pw-123456"))
        # 编辑响应不回显任何口令形态的键（spec §6.1：唯一出口是**新增**响应）。
        self.assertNotIn("initial_password", body["data"])
        self.assertNotIn("password", body["data"])

    def test_partial_update_turns_forbid_self_service_on_and_keeps_it_when_absent(self):
        """写入 + 回读：PATCH `grab_forbid_self_service=True` ⇒ 库与响应载荷都是 `True`；
        随后只改别的字段（不传这个键）⇒ **保持 `True`**（`required=False` + partial 语义）。"""
        self.assertIs(self._profile().grab_forbid_self_service, False)   # 夹具前提
        body = self._patch({"grab_forbid_self_service": True}).json()
        self.assertEqual(body["code"], 0)
        self.assertIs(body["data"]["grab_forbid_self_service"], True)
        self.assertIs(self._profile().grab_forbid_self_service, True)

        again = self._patch({"display_name": "改别的"}).json()
        self.assertEqual(again["code"], 0)
        self.assertIs(again["data"]["grab_forbid_self_service"], True)
        self.assertIs(self._profile().grab_forbid_self_service, True)

    def test_partial_update_turns_settle_forbidden_on_and_off(self):
        """钱⑤（2026-09-23 业务方裁定）：账号接口能**读**能**写** `settle_forbidden`。
        接法照 `grab_forbid_self_service`（P3e 的落点）：序列化器`BooleanField(required=False)`（写入口）+ `payloads._profile_fields`（读出）。**两个方向都钉字面布尔**——只测 `True` 的话，「写死 `True`」也能绿；只测 `False`（默认值）的话，一个恒 `False` 的实现也能绿。"""
        self.assertIs(self._profile().settle_forbidden, False)   # 夹具前提（模型默认值）
        body = self._patch({"settle_forbidden": True}).json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["settle_forbidden"], True)
        self.assertIs(self._profile().settle_forbidden, True)

        again = self._patch({"settle_forbidden": False}).json()
        self.assertEqual(again["code"], 0, again)
        self.assertIs(again["data"]["settle_forbidden"], False)
        self.assertIs(self._profile().settle_forbidden, False)

        # 与同族字段同一条 partial 语义：没发这个键的 PATCH 不得把它重置。
        self._patch({"settle_forbidden": True})
        third = self._patch({"display_name": "又改别的"}).json()
        self.assertEqual(third["code"], 0, third)
        self.assertIs(third["data"]["settle_forbidden"], True)
        self.assertIs(self._profile().settle_forbidden, True)

    def test_partial_update_of_an_account_without_a_profile(self):
        """目标账号**没有** `StaffProfile` 行时的编辑路径（`seed_tenant` 的 `admin` 就是这样）。
        覆盖 `_persist_account` 里 `get_or_create` 的那一支：编辑一个没有档案的账号必须**补出**档案行，而不是 `AttributeError` / HTML 500，且 `display_name` 与 `role` 照旧生效。判据是「**这类**请求还会不会 500」，不是「这个字段」（E-0010）。"""
        admin = User.objects.get(username="admin")
        self.assertIsNone(getattr(admin, "staff_profile", None))   # 夹具前提
        body = self.client.patch(
            f"/api/accounts/{admin.pk}/",
            data={"display_name": "总管", "role": WORKER},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        admin = User.objects.get(username="admin")
        self.assertEqual(admin.first_name, "总管")
        self.assertEqual(list(admin.groups.values_list("name", flat=True)), [WORKER])
        # 正向对照：档案是**这次**补出来的（不是夹具里本来就有）。
        self.assertIsNotNone(getattr(admin, "staff_profile", None))

    def test_patch_writes_the_sort_value_and_settle_switch(self):
        """PATCH 改「排序值」与「禁止结算」后**重新读库**确认落库（不看响应）。"""
        body = self._patch({"order_by": 7, "settle_forbidden": True}).json()
        self.assertEqual(body["code"], 0)
        profile = self._profile()                     # 重新查一次，不读响应里的回显
        self.assertEqual(profile.order_by, 7)
        self.assertIs(profile.settle_forbidden, True)
        # A 项的一半：只发这两个键不得顺手清掉别的列。
        self.assertEqual(profile.qq, self.QQ)

    def test_patch_resets_the_sort_value_and_settle_switch_to_zero_and_false(self):
        """E-0021 的边界：`0` / `False` 是**合法值**，不能被真值判断当成没传而丢掉。
        夹具先把两列改成非默认值再 PATCH 回 `0` / `False`——否则「没写进去」与
        「写的就是默认值」在断言里长得一样，这条用例就失去判别力。"""
        StaffProfile.objects.filter(user=self.target).update(
            order_by=9, settle_forbidden=True
        )
        body = self._patch({"order_by": 0, "settle_forbidden": False}).json()
        self.assertEqual(body["code"], 0)
        profile = self._profile()
        self.assertEqual(profile.order_by, 0)
        self.assertIs(profile.settle_forbidden, False)

    def test_update_role_replaces_the_single_group(self):
        self._patch({"role": WORKER})
        names = list(self.target.groups.values_list("name", flat=True))
        self.assertEqual(names, [WORKER])
        # A 项的另一半：只发 `role` 不得顺手重置档案 / 游戏 / 口令。
        profile = self._profile()
        self.assertEqual(profile.qq, self.QQ)
        self.assertEqual(profile.commission_value, self.COMMISSION)
        self.assertEqual(list(profile.games.values_list("pk", flat=True)), [self.game.pk])
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("pw-123456"))

    def test_update_games_replaces_the_set(self):
        other = Game.objects.create(name="英雄联盟")
        self._patch({"games": [other.pk]})
        self.assertEqual(
            list(self.target.staff_profile.games.values_list("pk", flat=True)), [other.pk]
        )
        # A 项的另一半：只发 `games` 不得顺手重置职位 / 档案 / 口令。
        self.assertEqual(list(self.target.groups.values_list("name", flat=True)), ["客服"])
        profile = self._profile()
        self.assertEqual(profile.qq, self.QQ)
        self.assertEqual(profile.commission_value, self.COMMISSION)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("pw-123456"))

    def test_password_is_rejected_not_ignored(self):
        body = self._patch({"password": "hunter2"}).json()
        self.assertEqual(body["code"], 1000)
        self.target.refresh_from_db()
        self.assertTrue(self.target.check_password("pw-123456"))   # 行未变
        # 正向对照：同一个 client 改一个**允许**的字段是 0——
        # 所以上面的 1000 来自「password 这个字段被拒」，不是端点整体不可用。
        self.assertEqual(self._patch({"display_name": "还是能改"}).json()["code"], 0)

    def test_is_superuser_and_is_staff_are_rejected(self):
        for field in ("is_superuser", "is_staff"):
            with self.subTest(field=field):
                body = self._patch({field: True}).json()
                self.assertEqual(body["code"], 1000)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_superuser)
        self.assertFalse(self.target.is_staff)

    def test_is_active_is_rejected_on_the_edit_endpoint(self):
        """R6 四个字段里的第四个：`is_active` 只走 #5 `status` 端点。"""
        body = self._patch({"is_active": False}).json()
        self.assertEqual(body["code"], 1000)
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        # 正向对照：允许的字段照样能改（1000 不是端点坏了）。
        self.assertEqual(self._patch({"display_name": "还是能改"}).json()["code"], 0)

    def test_cannot_take_another_users_username(self):
        make_account("taken", role="客服", game=self.game)
        body = self._patch({"username": "taken"}).json()
        # 裁决 R6 / spec §5.1：重名是**可区分**的一条 → 1001（不是 1000，也不是 HTML 500）。
        self.assertEqual(body["code"], 1001)
        self.target.refresh_from_db()
        self.assertEqual(self.target.username, "edit-target")   # 行未变
        # 正向对照：把自己的 username 原样再发一次必须成功——
        # 否则「拒」可能只是来自「username 一律不许改」这条更宽的规则。
        self.assertEqual(self._patch({"username": "edit-target"}).json()["code"], 0)

    def test_missing_target_returns_business_code_not_500(self):
        response = self.client.get("/api/accounts/99999999/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)

    def test_patch_of_a_missing_target_changes_nothing(self):
        """上一条是 `GET`；这一条是**编辑路径**：不存在的 id → 1002，且不产生任何行。"""
        before = User.objects.count()
        response = self.client.patch(
            "/api/accounts/99999999/", data={"display_name": "x"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1002)
        self.assertEqual(User.objects.count(), before)
        # 正向对照：同一个载荷打在**存在**的目标上是 0。
        self.assertEqual(self._patch({"display_name": "存在就改"}).json()["code"], 0)

    def test_update_malformed_id_returns_1000_not_500(self):
        """E-0010 同类：编辑路径的 `id` 非十进制时不落到 ORM，且与 `GET` **同一条分支**。
        `abc` 只是对照组。判别力在 `"²"`：`str.isdigit()` 为 `True` 而 `int()` 抛 `ValueError`——`isdigit()` 版本的守卫会放它过去，落到 DRF 的 `get_object_or_404` 上得到 1002。本端点必须与 `retrieve` 口径一致：同一个畸形 id 不能 `GET` 回 1000、`PATCH` 回 1002。"""
        for method in ("patch", "put"):
            for pk in ("abc", "²", "①", "Ⅰ", "-1", "+1"):
                with self.subTest(method=method, pk=pk):
                    response = getattr(self.client, method)(
                        f"/api/accounts/{pk}/", data={"display_name": "x"},
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response["Content-Type"], "application/json")
                    self.assertEqual(response.json()["code"], 1000)

    def test_update_response_carries_the_new_values(self):
        """响应里的字段必须是**改完之后**的值（三段：档案标量 / games / role）。
        ⚠️ `get_object()` 走 `base_queryset()` 的 `select_related("staff_profile")`——`user.staff_profile` 在那一刻就被**缓存**进 `user._state.fields_cache`，`profile.games`也在 `prefetch_related("staff_profile__games")` 的缓存里。写路径若用`StaffProfile.objects.get_or_create()` 拿到的**另一个** profile 对象去改，`account_payload(user)` 读到的仍是那份陈旧缓存：库里改了、响应回的是旧值，前端拿它回填表单就会把刚改掉的值显示回去（静默的，没人会报错）。
        判别力：三段必须一起看——`role` 走 `user.groups.set()`，它自己会清掉 `user` 上的预取缓存（`django/db/models/fields/related_descriptors.py:1253` 的`_remove_prefetched_objects()`），所以只钉 `role` 会漏掉这条；档案与 `games`是**另一个**对象上的缓存，清不到。"""
        other = Game.objects.create(name="英雄联盟")
        body = self._patch(
            {"qq": "13900000000", "games": [other.pk], "role": WORKER}
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["qq"], "13900000000")
        self.assertEqual(body["data"]["games"], [other.pk])
        self.assertEqual(body["data"]["role"], WORKER)

    def test_patch_is_studio_persists_and_response_carries_it(self):
        """D-0228：编辑勾/退「工作室」——库里与响应都变（响应是编辑表单的回填来源）；
        不发该键 = PATCH 语义「缺席保持原值」（把工作室账号改 QQ 不会顺手退掉勾选）。
        """
        body = self._patch({"is_studio": True, "member_type": 1}).json()
        self.assertEqual(body["code"], 0)
        self.assertIs(body["data"]["is_studio"], True)
        profile = StaffProfile.objects.get(user=self.target)
        self.assertIs(profile.is_studio, True)
        # 缺席 = 不动：只改 QQ，工作室标记保持 True。
        body2 = self._patch({"qq": "13911112222"}).json()
        self.assertEqual(body2["code"], 0)
        self.assertIs(body2["data"]["is_studio"], True)
        self._patch({"is_studio": False})
        self.assertIs(
            StaffProfile.objects.get(user=self.target).is_studio, False
        )

    def test_put_requires_the_full_body(self):
        """`PUT` 是**整单**替换（serializer `partial=False`）：缺必填项 → 1000，且一行都不许改。
        正向对照是同文件的 `test_put_replaces_the_whole_record`。"""
        response = self._put({"display_name": "半单"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "")     # 载荷里那个字段也没落库
        self.assertEqual(list(self.target.groups.values_list("name", flat=True)), ["客服"])

    def test_put_rejects_server_managed_fields_too(self):
        """`PUT` 与 `PATCH` 在四个服务端字段上的拒绝不能分叉（fix round 1 / finding 5）。
        载荷带齐 `PUT` 的必填项，唯独多出被禁字段——这样 1000 只可能来自那条拒绝规则，
        而不是「缺必填项」；正向对照是同一份载荷去掉被禁字段必须回 0。"""
        required = {"username": "edit-target", "role": "客服", "games": [self.game.pk]}
        self.assertEqual(self._put(dict(required)).json()["code"], 0)      # 正向对照
        for field, value in (
            ("password", "hunter2"),
            ("is_superuser", True),
            ("is_staff", True),
            ("is_active", False),
        ):
            with self.subTest(field=field):
                response = self._put({**required, field: value})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)
                self.target.refresh_from_db()          # 行未变（四个列逐项钉）
                self.assertTrue(self.target.check_password("pw-123456"))
                self.assertTrue(self.target.is_active)
                self.assertFalse(self.target.is_staff)
                self.assertFalse(self.target.is_superuser)

    def test_empty_patch_has_no_side_effects(self):
        """空 PATCH 是**纯 no-op**：不得凭空建出一条空白档案行（fix round 1 / finding 4）。
        断言打在**数据库行**上。账号行那一半（空载荷不写 `auth_user`）在这里**没有**可观测差异——把同样的值写回去看不出来；它由 `_persist_account` 的提前返回保证，「陈旧整行写回」的判别力在 `test_edit_does_not_revert_a_concurrent_write_to_another_column`。夹具用 `admin`：`seed_tenant` 只建账号、不建档案，正好是「没有档案」那一支。"""
        admin = User.objects.get(username="admin")
        self.assertIsNone(getattr(admin, "staff_profile", None))     # 夹具前提：没有档案
        response = self.client.patch(
            f"/api/accounts/{admin.pk}/", data={}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        # 断言打在**数据库行**上：空编辑不得顺手 `get_or_create` 出一条空白档案。
        self.assertFalse(StaffProfile.objects.filter(user=admin).exists())
        admin.refresh_from_db()
        self.assertEqual(admin.first_name, "")
        # 正向对照：同一目标、同一 verb，**非空** PATCH 仍然补出档案行。
        self.assertEqual(
            self.client.patch(
                f"/api/accounts/{admin.pk}/", data={"display_name": "总管"},
                content_type="application/json",
            ).json()["code"],
            0,
        )
        self.assertTrue(StaffProfile.objects.filter(user=admin).exists())

    def test_missing_role_group_returns_1000_not_500(self):
        """`Role` 行在、同名 `Group` 行不在（两张表漂移）时：1000 信封，不是 HTML 500（finding 3）。
        `role` 现在过序列化器的**请求期存在性校验**（`validate_role_exists` 查 `Role` 表），所以这里构造的是「`Role.name` 与 `Group.name` 不一致」那种漂移——`apps/rbac/services.py`说明这两张表之间**没有**数据库级约束，直接写库可以留下不同名的行。
        ⚠️ **不要退回「直接删 `Group`」的旧写法**（Task 2 实测）：`Role.group` 是`on_delete=CASCADE`，删 `Group` 会把 `Role` 一起删掉 ⇒ 请求在**序列化器**那层就被拒，`_persist_account` 里这段 `Group.DoesNotExist` 翻译根本走不到，本用例会退化成「又测了一遍不存在性校验」。"""
        group = Group.objects.get(name=WORKER)
        group.name = f"{WORKER}-漂移"          # 只改 Group 名，`Role` 行仍在（两张表漂移）
        group.save(update_fields=["name"])
        # 夹具自证：漂移真的成立（Role 在、同名 Group 不在）——否则下面验的是别的东西。
        self.assertTrue(Role.objects.filter(name=WORKER).exists())
        self.assertFalse(Group.objects.filter(name=WORKER).exists())
        response = self._patch({"role": WORKER})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        body = response.json()
        self.assertEqual(body["code"], 1000)
        self.assertIn(WORKER, body["msg"])       # 缺失的职位要点名，才知道去修哪一行
        self.assertEqual(
            list(self.target.groups.values_list("name", flat=True)), ["客服"]   # 行未变
        )
        # 正向对照：同一 verb、同一目标，换成**存在**的 Group → 0。
        self.assertEqual(self._patch({"role": "客服"}).json()["code"], 0)

    def test_edit_does_not_revert_a_concurrent_write_to_another_column(self):
        """编辑只写本次真正改动的列：并发写入的 `password` / `last_login` 必须存活（finding 1）。
        判别力：在视图 `get_object()` **读完之后**、`_persist_account` **写之前**，用另一条语句改库——`last_login` 每次成功登录都会写，`password` 本端点明确拒收。不带`update_fields=` 的 `save()` 会把视图内存里的旧值整行写回，下面两条断言就失败。"""
        stale = timezone.now() - timedelta(days=1)
        concurrent = timezone.now()
        User.objects.filter(pk=self.target.pk).update(last_login=stale)
        real_validate = accounts_views._validate_write_serializer

        def race(serializer):
            User.objects.filter(pk=self.target.pk).update(
                last_login=concurrent, password=make_password("race-pw")
            )
            return real_validate(serializer)

        with mock.patch("apps.accounts.views._validate_write_serializer", side_effect=race):
            body = self._patch({"display_name": "改名了"}).json()
        self.assertEqual(body["code"], 0)
        row = User.objects.get(pk=self.target.pk)
        self.assertEqual(row.first_name, "改名了")          # 本次改动生效
        self.assertEqual(row.last_login, concurrent)        # 并发写入未被盖回
        self.assertTrue(row.check_password("race-pw"))      # 口令列未被盖回旧哈希

    def test_put_replaces_the_whole_record(self):
        other = Game.objects.create(name="英雄联盟")
        body = self._put({
            "username": "edit-target", "display_name": "整单改名", "role": WORKER,
            "games": [other.pk], "qq": "13600000000",
        }).json()
        self.assertEqual(body["code"], 0)
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "整单改名")
        self.assertEqual(list(self.target.groups.values_list("name", flat=True)), [WORKER])
        profile = self._profile()
        self.assertEqual(list(profile.games.values_list("pk", flat=True)), [other.pk])
        self.assertEqual(profile.qq, "13600000000")
        self.assertTrue(self.target.check_password("pw-123456"))

    def test_update_is_admin_only(self):
        """写路径继承 `IsAccountAdmin`（R6 / spec D6）。正向对照在同一用例内。"""
        outsider = make_logged_in_client(username="upd-outsider")
        response = outsider.patch(
            f"/api/accounts/{self.target.pk}/", data={"display_name": "越权"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.target.refresh_from_db()
        self.assertEqual(self.target.first_name, "")
        self.assertEqual(self._patch({"display_name": "有权限"}).json()["code"], 0)

    def test_integrity_error_path_returns_1001_not_500(self):
        """并发兜底：`_persist_account` 抛 `IntegrityError` → 1001 信封，且整单回滚。
        patch 的是 **view 里引用的那个模块级名字**（与 `create` 路径既有测试同手法）——
        序列化器的存在性检查挡不住竞态，没有这条 except 就是 HTML 500。"""
        with mock.patch("apps.accounts.views._persist_account", side_effect=IntegrityError):
            response = self._patch({"username": "race-loser"})
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 1001)
        self.assertTrue(body["msg"], "1001 分支必须带可读 msg")
        # 结构性断言：信封就是这三个键、`data` 为空（不看某一个键名在不在）。
        self.assertEqual(set(body), {"code", "msg", "data"})
        self.assertIsNone(body["data"])
        self.target.refresh_from_db()
        self.assertEqual(self.target.username, "edit-target")   # 回滚：行未变


class AccountCreateTests(SeededTenantTestCase):
    """新增路径的两面：出口载荷的形状，与必填项仍然必填（Task 6 的 A 项）。⚠️ 「必填项仍然必填」是 A 项的**另一半**：改 `_persist_account` 让缺席的键不赋值之后，必须同时证明**新增**时缺席的键仍然被拒——否则一个「缺席就写默认值」的实现能让所有PATCH 用例全绿，却建出一个没有职位、没有游戏的账号。
    ⚠️ 载荷形状那条与 A 项也同源：`_persist_account` 改成了「先`getattr(user, "staff_profile", None)`，没有再 `get_or_create`」。档案对象现在可能来自**反向缓存**（`get_object()` 的 `select_related` 塞的那种），而 `account_payload(user)`读的就是这个缓存——取档案这一步被改坏时，新增响应里的**整个档案键组**会消失，而新增响应正是口令唯一一次出口的时刻。
    ⚠️ 判别力口径（如实）：载荷形状那条断言的是**出口契约**。实测：删掉 `get_or_create`之后那次手动回写缓存，该用例**仍绿**——因为正向一对一描述符赋值时已经写了反向缓存（`related_descriptors.py:353-356`）。所以它证明的是「新增响应带档案字段」，**不是**「某一行的写法必需」；编辑路径那个真实的缓存 bug 由`AccountUpdateTests.test_update_response_carries_the_new_values` 用判别力钉住。"""

    def setUp(self):
        self.client = make_logged_in_client(username="create-probe")
        User.objects.get(username="create-probe").groups.add(
            Group.objects.get(name=TENANT_ADMIN)
        )

    def test_create_response_carries_the_profile_keys(self):
        body = self.client.post(
            "/api/accounts/",
            data={
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "profiled", "role": "客服",
                "games": [self.game.pk], "qq": "13500000000",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        account = body["data"]["account"]
        self.assertEqual(account["display_name"], "小王")
        self.assertEqual(account["role"], "客服")
        self.assertEqual(account["games"], [self.game.pk])
        # 判别力在这两行：档案对象从缓存里读成 `None` 时，下面两个键会整个消失。
        self.assertEqual(account["qq"], "13500000000")
        self.assertIn("member_type", account)

    def test_create_still_rejects_a_missing_required_field(self):
        """新增时缺 `role` / `games` → 1000，且**不落行**（A 项的另一半）。
        与 `AccountUpdateTests` 的 PATCH 用例配成一对：那边证明「PATCH 缺席 = 保持库里的值」，这边证明「新增缺席 = 拒 + 不落行」。缺了这条，一个把缺席键当默认值写的实现（PATCH 用例照样全绿）会建出一个没有职位或没有游戏的账号。"""
        before = User.objects.count()
        for payload in (
            {**CREATE_REQUIRED_PROFILE_FIELDS, "username": "no-role", "games": [self.game.pk]},
            {**CREATE_REQUIRED_PROFILE_FIELDS, "username": "no-games", "role": "客服"},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/api/accounts/", data=payload, content_type="application/json"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(User.objects.count(), before)

        # 正向对照：同一批夹具里把缺的那个字段补上就能建出来——
        # 所以上面的 1000 来自「缺必填项」，不是这个载荷本身建不出来。
        ok = self.client.post(
            "/api/accounts/",
            data={
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "no-role", "role": "客服", "games": [self.game.pk],
            },
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0)
        self.assertTrue(User.objects.filter(username="no-role").exists())

    def test_create_requires_the_six_spec_required_fields(self):
        """R13 / spec §3.2 / §4.2：六个「必填，照抄」字段在**新增**上缺一不可。
        做法：每个子用例从一份完整载荷里**只删一个**字段，断言 1000 **且不落行**（断在数据库行上，E-0009）。逐个删是必需的——只测其中一个的话，另外五个的必填性没有承重物。末尾补正向对照：同一份完整载荷必须真的建出账号。
        ⚠️ 错误**归属**（是哪个字段缺了）由 `tests/test_account_serializer.py` 的`test_create_marks_each_missing_spec_required_field` 钉住：统一信封的 `msg`只带一句可读文本、不带字段名，在这里断言字段名会假红。"""
        base = {
            **CREATE_REQUIRED_PROFILE_FIELDS,
            "username": "six-required",
            "role": "客服",
            "games": [self.game.pk],
        }
        before = User.objects.count()
        for field in CREATE_REQUIRED_PROFILE_FIELDS:
            payload = {key: value for key, value in base.items() if key != field}
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/accounts/", data=payload, content_type="application/json"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000, response.json())
        # 断言落在数据库行数上：六次被拒的请求一行都不许落库。
        self.assertEqual(User.objects.count(), before)
        self.assertFalse(User.objects.filter(username="six-required").exists())

        # 正向对照：六项齐全时同一条路径必须成功，且值真的落到档案行上。
        ok = self.client.post(
            "/api/accounts/", data=base, content_type="application/json"
        ).json()
        self.assertEqual(ok["code"], 0, ok)
        created = User.objects.get(username="six-required")
        self.assertEqual(created.staff_profile.grab_limit_dl, 0)
        self.assertEqual(created.staff_profile.grab_limit_pw, 0)
        self.assertEqual(str(created.staff_profile.hired_on), "2026-01-02")
        self.assertEqual(str(created.staff_profile.deposit_amount), "0.00")
        self.assertEqual(created.first_name, "小王")

    def test_create_records_the_operator_as_opened_by(self):
        """开户人（自助注册线 2026-10-16，spec §7-14）：手工新增 = 当时的操作者。
        断言打在档案行的 `opened_by` 列上（E-0009）：它是 `opened_by_id` 与操作者主键的**逐值对拍**，操作者是本类夹具登录的 `create-probe`。自助注册那条写路径（`opened_by = invite.created_by`）由`tests/test_self_registration.py` 断言——两条写路径都断言（spec §7-15）。"""
        body = self.client.post(
            "/api/accounts/",
            data={
                **CREATE_REQUIRED_PROFILE_FIELDS,
                "username": "opened-marker", "role": "客服",
                "games": [self.game.pk],
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        created = User.objects.get(username="opened-marker")
        self.assertEqual(
            created.staff_profile.opened_by_id,
            User.objects.get(username="create-probe").pk,
        )


class AccountStatusTests(TenantTestCase):
    """#5 单个启停 / #6 批量启停（幂等 `set`）。⚠️ **刻意偏离对标物**：`/UserAjax/UpUserSta` 是 **toggle**（重复请求会翻回去），本线按 spec §3.3 的注改成**幂等 set**——请求体带 `{"is_active": bool}`，重复发同一条请求是 no-op。`test_disable_single_is_idempotent_set` 钉住这个方向。
    R5：`TenantTestCase` 不跑 `seed_tenant`，而本类要 `Group.objects.get(TENANT_ADMIN)`，所以 `setUpClass` 必须显式灌一次（否则 `DoesNotExist`）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)

    def setUp(self):
        self.game = Game.objects.create(name="王者荣耀")
        self.client = make_logged_in_client(username="sta-admin")
        User.objects.get(username="sta-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        self.a = make_account("sta-a", role="客服", game=self.game)
        self.b = make_account("sta-b", role="客服", game=self.game)

    def test_disable_single_is_idempotent_set(self):
        """⚠️ 我们**偏离**对标物的 toggle，改成幂等 set（spec §3.3 注）。"""
        first = self.client.post(f"/api/accounts/{self.a.pk}/status/",
                                 {"is_active": False}, content_type="application/json").json()
        self.assertEqual(first["code"], 0)
        second = self.client.post(f"/api/accounts/{self.a.pk}/status/",
                                  {"is_active": False}, content_type="application/json").json()
        self.assertEqual(second["code"], 0)
        self.a.refresh_from_db()
        self.assertFalse(self.a.is_active)      # 两次都是 False，没有翻回去

    def test_batch_disable_sets_all(self):
        body = self.client.post("/api/accounts/status/",
                                {"ids": [self.a.pk, self.b.pk], "is_active": False},
                                content_type="application/json").json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(User.objects.get(pk=self.a.pk).is_active)
        self.assertFalse(User.objects.get(pk=self.b.pk).is_active)

    def test_disabled_account_can_no_longer_log_in(self):
        # 正向对照先跑：同一个口令**在停用之前**必须能登录。没有它，下面那个
        # `assertIsNone` 可能由「这个夹具的口令压根对不上」凑出来，判别力为零。
        self.assertIsNotNone(authenticate(username="sta-a", password="pw-123456"))
        self.client.post(f"/api/accounts/{self.a.pk}/status/",
                         {"is_active": False}, content_type="application/json")
        self.assertIsNone(authenticate(username="sta-a", password="pw-123456"))

    def test_malformed_ids_returns_1000_not_500(self):
        """E-0010 的判据是「**这类请求**还会不会 500」：四个畸形载荷都必须是干净的 1000。
        `{"ids": "not-a-list"}` / `{"ids": [99999999]}` / `{"ids": []}` 走**批量**入口（`status_batch`），`{"is_active": "maybe"}` 刻意**不带 `ids`**、走**单个**入口（`/api/accounts/<pk>/status/`）——否则它就只是上面三条之外的第四个批量载荷，`AccountStatusSerializer`（单个入口的校验器）压根没被这条用例摸到。"""
        for payload in ({"ids": "not-a-list", "is_active": False},
                        {"ids": [99999999], "is_active": False},
                        {"ids": [], "is_active": False},
                        {"is_active": "maybe"}):
            with self.subTest(payload=payload):
                if "ids" in payload:
                    url = "/api/accounts/status/"
                else:
                    url = f"/api/accounts/{self.a.pk}/status/"
                response = self.client.post(url, payload, content_type="application/json")
                self.assertNotEqual(response.status_code, 500)
                self.assertEqual(response.json()["code"], 1000)

    def test_unknown_id_batch_writes_nothing(self):
        """「先解析、后应用」：批量载荷里有一个不存在的 id 时，**整批不落库**。
        只断言返回 1000 是不够的——一个「先逐个写、遇到缺失再报错」的实现照样回 1000，
        却已经把前面那些账号停用了（半个批次生效）。所以断言打在**数据库行**上（E-0009）。"""
        body = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.a.pk, 99999999], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertTrue(User.objects.get(pk=self.a.pk).is_active)

        # 正向对照：同一个载荷去掉那个不存在的 id 就真的生效——
        # 所以上面的「还是 True」不是「这个端点压根不工作」凑出来的。
        ok = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.a.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0)
        self.assertFalse(User.objects.get(pk=self.a.pk).is_active)

    def test_batch_with_a_soft_deleted_id_is_rejected_whole_and_writes_nothing(self):
        """E-0250：批量启停的载荷里混入一个**软删**的 pk ⇒ 1000 整批拒绝、零写入。
        软删的人必须和「不存在的 id」走**同一句话**（「存在不存在的账号」）：批量取行与单条 `get_object()` 同源（都过 `base_queryset()` 的软删过滤）之后，软删行在这条路径上就是「取不到」。否则批量启用会把 `deleted_at` 非空、`is_active=False`的那个人翻回启用——被解雇的人拿旧密码重新登录（单条路径对同一目标回 1002）。恢复的正道是回收站的 `restore`（`tests/test_account_soft_delete.py`）。
        断言打在**数据库行**上（E-0009）：只看 1000 不够，「先逐个写、遇到缺失再报错」的实现照样回 1000 却已把活账号停用了；这里钉「批内**活**账号一行不改 +软删行**没有被复活**（`is_active` 与 `deleted_at` 都停在软删那一刻）」。正向对照：同一载荷去掉软删 pk 真的生效——1000 不是「端点坏了」凑出来的。"""
        # 夹具自证：a 确实被软删了（走它自己的端点，不手写列）。
        deleted = self.client.post(
            f"/api/accounts/{self.a.pk}/delete/", content_type="application/json"
        ).json()
        self.assertEqual(deleted["code"], 0, deleted)
        self.assertIsNotNone(StaffProfile.objects.get(user=self.a).deleted_at)
        # ⚠️ 内存对象还是软删**前**的旧值：先 refresh 再自证（上一版漏了这步，
        # 红在了自检行上——那是夹具的 bug，不是被测端的）。
        self.a.refresh_from_db()
        self.assertIs(self.a.is_active, False)

        for is_active in (True, False):
            with self.subTest(is_active=is_active):
                body = self.client.post(
                    "/api/accounts/status/",
                    {"ids": [self.a.pk, self.b.pk], "is_active": is_active},
                    content_type="application/json",
                ).json()
                self.assertEqual(body["code"], 1000)
                # 与「不存在的 id」同一句通用文案：不给「这个 pk 在别的租户/回收站里
                # 还存在」留可探测面（与 status_batch 既有注释同口径）。
                self.assertEqual(body["msg"], "存在不存在的账号")
        # 整批零写入（批内的活账号 b 没被停用也没被启用）。
        self.assertTrue(User.objects.get(pk=self.b.pk).is_active)
        # 软删行停在软删那一刻：既没有被批量「启用」复活，也没有被第二次改写。
        self.a.refresh_from_db()
        self.assertIs(self.a.is_active, False)
        self.assertIsNotNone(StaffProfile.objects.get(user=self.a).deleted_at)

        # 正向对照：去掉软删 pk，同一载荷对 b 生效。
        ok = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.b.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0)
        self.assertFalse(User.objects.get(pk=self.b.pk).is_active)

    def test_delete_is_still_not_exposed_on_the_new_routes(self):
        """任务要求 2：新 `@action` 不得把 ViewSet 拓宽成可删（D3 的 405 边界）。本任务新增的两条路由（详情层 `.../status/`、列表层 `status/`）都只注册了 `POST`。
        `DELETE` 到这两条上必须仍是 **405**——正向对照是同一批 URL 的 `POST` 可用（不是「整个 ViewSet 不可达」凑出的 405）。
        `reset-password` 那条路由属于 Task 9，它的 405 由`tests/test_account_password.py` 里同名的用例钉住（本任务跑时那个 URL 还不存在，在这里断言会得到 `404` 而不是 `405`——实测踩到过）。"""
        for url in (f"/api/accounts/{self.a.pk}/status/",
                    "/api/accounts/status/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.delete(url).status_code, 405)
        # 这条 `deleted_at is None` 是**终态快照**（如实口径，别读过头）：· 它守的是「被 405 挡掉的 DELETE 最终没在库里留下软删痕迹」这个**终态**；· 它**分辨不了守卫的「位置」**——**变异 F**（把守卫挪到 `_lookup_id_error` 之后）实测：
        # 带**数字 pk** 的这条（`<pk>/status/`）**仍然回 405**（`_lookup_id_error` 放行之后守卫照样拦下）⇒ 就「相对 `_lookup_id_error` 的前 / 后」这**两个被测位置**而言，这条路由都不会软删；变异 F 下**变红的是上面循环里 `url='/api/accounts/status/'`那一条**（`pk=None` ⇒ 被 `_lookup_id_error` 先短路成 1000 信封 / HTTP 200），**不是它**。· 但它**不是恒真**：**变异 G**（把守卫**整个删掉**）实测它**真的会红**
        # （`line 1867` 的 `assertIsNone` 失败，见 `task-8-mutation-G.txt`）⇒ 它观察得到「守卫不存在」那条反事实。· ⚠️ 它确实会被执行到：上面那两条 405 断言写在 `with self.subTest(...)` 里，而 **`subTest` 的失败是「记录后吞掉」的、不会中断本用例**（我另跑探针验过：子测试失败后 `with` 之后的语句照样执行）⇒ 这里**不能**写「只有 405 先通过才执行」。⛔ 这里原先写「守卫排在 `_lookup_id_error` 之后会**放行**然后**直接软删那个账号**」——那句**是错的**（被变异 F 证伪），而且与 `views.py` 同一次改准的注释直接矛盾。
        self.assertIsNone(
            StaffProfile.objects.get(user=self.a).deleted_at,
            "被 405 挡掉的 DELETE 不得在库里留下软删痕迹（守卫被删掉时这条会红）",
        )
        self.assertEqual(
            self.client.post(
                f"/api/accounts/{self.a.pk}/status/",
                {"is_active": True},
                content_type="application/json",
            ).status_code,
            200,
        )

    def test_requires_an_account_admin(self):
        """R6：启停走**同一个** `IsAccountAdmin` 门（不是新开一个权限名）。
        正向对照在同一用例内：同一载荷换成管理员发就成功——所以 403 是权限挡的，
        不是载荷/路由的问题。"""
        outsider = make_logged_in_client(username="sta-outsider")
        response = outsider.post(
            f"/api/accounts/{self.a.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertTrue(User.objects.get(pk=self.a.pk).is_active)
        self.assertEqual(
            self.client.post(
                f"/api/accounts/{self.a.pk}/status/",
                {"is_active": False},
                content_type="application/json",
            ).json()["code"],
            0,
        )

    def test_batch_response_lists_the_updated_ids(self):
        """批量成功响应带 `updated`：调用方要知道**这批请求实际改了谁**。
        只断言 `code == 0` 时，一个「返回空 `updated`」的实现也能全绿；`updated` 是
        前端刷新选中行的唯一依据（spec §5.1 的载荷口径）。"""
        body = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.b.pk, self.a.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertCountEqual(body["data"]["updated"], [self.a.pk, self.b.pk])

    def test_batch_ids_over_the_spec_cap_returns_1000_without_writing(self):
        """spec §9-8 的端到端接线：201 个 id 的批量请求回 1000，且一行都不落库。
        ⚠️ 判别力口径（如实）：本载荷里的 201 个号都**不存在**，就算上限没生效，存在性核对也会回 1000 —— 所以这条钉的是「接线 + 不落行」；上限本身（200 放行 / 201 拒绝）由 `tests/test_account_serializer.py` 的`AccountBatchStatusSerializerTests` 直打序列化器钉住。"""
        self.assertTrue(User.objects.get(pk=self.a.pk).is_active)   # 夹具前提
        response = self.client.post(
            "/api/accounts/status/",
            {"ids": list(range(900001, 900202)), "is_active": False},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertTrue(User.objects.get(pk=self.a.pk).is_active)   # 断言落在数据库行上

        # 正向对照：合法的小批量仍然生效——否则上面的 1000 可能由「批量端点坏了」凑出来。
        ok = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.a.pk], "is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0)
        self.assertFalse(User.objects.get(pk=self.a.pk).is_active)

    def test_batch_disable_is_idempotent_set_on_repeat(self):
        """**批量**入口也是幂等 `set`：同一条请求发两次，两次都成功且结果不变。
        单条入口有同样的重复请求用例（`test_disable_single_is_idempotent_set`），批量入口原先却每条用例只发一次、而夹具初始 `is_active=True`——于是「**批量路径单独**退回对标物的 toggle」的实现在旧测试下**全绿**（第一次 `True → False` 已经对上了）。本用例是那条缺口的判别力所在。
        末尾再发一次 `is_active=True` 作正向对照：它证明这个端点**会**跟着载荷改状态，所以「第二次 False 之后仍是 False」是幂等，不是「端点第二次就没反应了」。"""
        payload = {"ids": [self.a.pk, self.b.pk], "is_active": False}
        first = self.client.post(
            "/api/accounts/status/", payload, content_type="application/json"
        ).json()
        self.assertEqual(first["code"], 0)
        second = self.client.post(
            "/api/accounts/status/", payload, content_type="application/json"
        ).json()
        self.assertEqual(second["code"], 0)
        # 断言落在数据库行上：两次都是 False，第二次没有把它们翻回 True。
        self.assertFalse(User.objects.get(pk=self.a.pk).is_active)
        self.assertFalse(User.objects.get(pk=self.b.pk).is_active)

        # 正向对照：同一条路径改发 True 必须能把两行都改回来。
        back = self.client.post(
            "/api/accounts/status/",
            {"ids": [self.a.pk, self.b.pk], "is_active": True},
            content_type="application/json",
        ).json()
        self.assertEqual(back["code"], 0)
        self.assertTrue(User.objects.get(pk=self.a.pk).is_active)
        self.assertTrue(User.objects.get(pk=self.b.pk).is_active)

    def test_malformed_path_id_matches_the_detail_endpoint_code(self):
        """同一个畸形 pk 在 **GET 详情**与**两个启停入口**上必须回同一条分支。`_lookup_id_error` 的存在理由就是这个：不能「看方法决定码值」。没有它时实测分叉——`GET /api/accounts/abc/` 回 1000，而启停/重置落到 `get_object()` 上回 1002。
        正对照在同一用例内：同一个 URL 换成合法 pk 就必须回 0，所以上面的 1000 不是「路由压根不存在」（那会是 404 或 405）。"""
        for pk in ("abc", "²", "①", "Ⅰ", "-1", "+1"):
            with self.subTest(pk=pk):
                detail = self.client.get(f"/api/accounts/{pk}/")
                status = self.client.post(
                    f"/api/accounts/{pk}/status/",
                    {"is_active": False},
                    content_type="application/json",
                )
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(status.status_code, 200)
                self.assertEqual(detail.json()["code"], 1000)
                self.assertEqual(
                    status.json()["code"],
                    detail.json()["code"],
                    f"pk={pk!r}：启停入口与详情入口回的不是同一个码",
                )
        ok = self.client.post(
            f"/api/accounts/{self.a.pk}/status/",
            {"is_active": False},
            content_type="application/json",
        ).json()
        self.assertEqual(ok["code"], 0)
        self.assertFalse(User.objects.get(pk=self.a.pk).is_active)


class AccountBatchStatusAutocommitTests(TransactionTestCase):
    """**生产形态**下的批量启停：autocommit 打开时也必须成功。
    ⚠️ 本类为什么必须是 `TransactionTestCase`（而不是同文件其余用例的 `TenantTestCase`）：`TenantTestCase` 继承 `django.test.TestCase`，它把每条用例包在一个 atomic 块里（`django/test/testcases.py` 的 `TestCase._fixture_setup` → `_enter_atomics`），于是测试里的 `connection.get_autocommit()` 恒为 **False**。请求跑的却是**另一个**形态：本仓库设置里没有 `ATOMIC_REQUESTS`，`MIDDLEWARE` 里也没有开事务的中间件 ⇒ autocommit（下面第一行断言就是这条前提的自证）。两者的差别正是 `select_for_update()` 合不合法（`django/db/models/sql/compiler.py:839-848`）——所以只有跳出 `TestCase` 的事务包裹，`status_batch` 少了那层 `transaction.atomic()` 时的 `TransactionManagementError`才会露出来。**别把这个类挪回 `TenantTestCase`：那样它会退化成一条恒真的假通过。**
    `django_tenants` 只提供 `TestCase` 版的 `TenantTestCase`，所以这里自己搭一遍它的租户建/拆（等价于 `TenantTestCase.setUpClass` / `tearDownClass`）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=DOMAIN)
        cls.domain.save()
        # `TenantTestCase.add_allowed_test_domain()` 的等价物：Host 路由要放行这个域名。
        if DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [DOMAIN]
        connection.set_tenant(cls.tenant)
        seed_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(DOMAIN)
        super().tearDownClass()

    def test_batch_status_succeeds_while_autocommit_is_on(self):
        # 前提自证：这条用例确实跑在 autocommit 上（否则它测的还是 `TestCase` 那个形态）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上，测不到生产形态——检查基类是否被换掉了",
        )
        game = Game.objects.create(name="王者荣耀")
        client = make_logged_in_client(username="auto-admin")
        User.objects.get(username="auto-admin").groups.add(Group.objects.get(name=TENANT_ADMIN))
        target = make_account("auto-a", role="客服", game=game)

        response = client.post(
            "/api/accounts/status/",
            {"ids": [target.pk], "is_active": False},
            content_type="application/json",
        )
        # 修复前这里不是这个形状：`status_batch` 的 queryset 在 autocommit 下就地求值，
        # 直接抛 `TransactionManagementError` 冒到测试客户端外（实测见 task-7-9-report.md）。
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0)
        # 断言打在数据库行上：只看返回码无法区分「写了」与「回了个成功信封」。
        self.assertFalse(User.objects.get(pk=target.pk).is_active)


class AccountIdCardWhitespaceNormalizationTests(AccountApiTestCase):
    """E-0252 写入侧收口：管理员手填的身份证号 **strip**、纯空白**归空串**落库。
    「已实名」判据（`realname.services.is_real_name`，strip 后判非空）与人员列表的「实名状态」筛选项（`accounts/selectors.py` 的 `id_card_no__gt=""`，DB 字节级判据）都假设档案里**不会**躺纯空格——写入侧同一次收口（E-0232：穷举过全部写入点后的结论：生产写入点只有 verify pass 落档 / 档案手填 / 重置清空三处，verify 与重置本来就是干净值）。
    ⚠️ **如实登记（先红后绿的口径）**：DRF `CharField` 默认 `trim_whitespace=True`今天已经在 strip（实测：`rest_framework/fields.py` 的 `run_validation` 对 strip 后为空的 `allow_blank` 值提前回 `''`）⇒ 本类**现状天生绿**；它的「能红」证明 = 变异（把字段的 `trim_whitespace` 关掉 ⇒ 两条用例红，读数见本线报告）。序列化器里的显式`validate_id_card_no` 是**不依赖第三方默认值**的项目级保证——`RealNameVerifySerializer`就为了自己的理由把 `trim_whitespace` 关掉过，今天照抄那个形态进来时本网必须拦住。"""

    def test_whitespace_only_id_card_is_stored_as_empty(self):
        res = self.client.patch(
            f"/api/accounts/{self.kefu.pk}/",
            data={"id_card_no": "   "},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], 0)
        self.assertEqual(
            StaffProfile.objects.get(user=self.kefu).id_card_no,
            "",
            "纯空格落了库 ⇒ 判据与筛选项都会把 ' ' 当已实名（E-0252 写入侧没收口）",
        )

    def test_surrounding_whitespace_is_stripped(self):
        res = self.client.patch(
            f"/api/accounts/{self.kefu.pk}/",
            data={"id_card_no": " 110101199001011234 "},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["code"], 0)
        self.assertEqual(
            StaffProfile.objects.get(user=self.kefu).id_card_no,
            "110101199001011234",
            "前后空白没有剥掉就落库",
        )
