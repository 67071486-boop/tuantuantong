"""T8：实名认证**管理页**端点（列表 / 设置读写 / 余量 / 重置）的判据。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` **§8**（权限与留痕）、
**§9.1**（端点清单）、**§10.1**（对标那一页的逐项口径）、**§13-2-3** / **§14-P11**
（「只存开关、不实装拦截」）与任务书 T8 的 9 条测试要求。

## 判据一律打在**终态**上（`AGENTS.md` §5-E / E-0009）

主判据 = 响应里的**载荷值**与库里的**行**（`RealNameCertRecord` 的字段、
`StaffProfile.id_card_no`、`RealNameSetting` 的列、`OperationLog` 的行）。
⛔ **不把 `code == 0` 当判据**——那是恒真写法里最常见的一种（需求 3 明写「断言返回的行数 /
内容，不只 `code == 0`」）。

## ⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）

身份证号用 `110101…` 的**合成**号（校验位按 GB 11643 算过 ⇒ 本地校验能过，**不指向任何真人**）；
姓名 / 账号名都是合成串。⛔ 一个真值都没有。

## ⚠️ `status` 三档与 `key1/2/3` 的判别力

`status=0`（全部）与「不传 `status`」都返回全部行 ⇒ 只断言行数**分不出**这两者，
所以用例 3 的三档**各自断言具体是哪几行**（按 `user_name` 认，⛔ 不按 pk —— 每个 schema 的
自增序列各自从 1 开始，拿 pk 指认跨用例的行是恒真/恒假的温床）。
"""
from __future__ import annotations

import warnings
from datetime import UTC, date, datetime

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.platform.models import RealNameGrant, RealNamePackage
from apps.rbac.seed import seed_catalog
from apps.realname.models import CertMethod, CertResult, RealNameCertRecord, RealNameSetting
from apps.business.roles import SERVICE, TENANT_ADMIN, WORKER
from apps.rbac.permissions import user_node_codes
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 平面 / 域名（与既有实名测试同口径）────────────────────────────────────────
TENANT_DOMAIN = "tenant.test.com"

# ── 合成夹具 ───────────────────────────────────────────────────────────────────
ID_CARD_A = "110101199003070011"
ID_CARD_B = "110101198505120037"
NAME_A = "测试姓名甲"
NAME_B = "测试姓名乙"
FAKE_KEY_ID = "AKIDFAKE0000T8SYNTH"
FAKE_KEY_SECRET = "SYNTH-T8-0000-FAKE-SECRET"

# ── 权限节点（⛔ 字面量，不 import catalog：那样节点被改名/删掉时用例看不见）──────
NODE_RECORDS = "system_user.realname_records"
NODE_RESET = "system_user.reset_realname"

# ── 业务码字面值（⛔ 不写 `ErrorCode.…`，同 `test_realname_verify_api.py`）────────
OK = 0
VALIDATION = 1000
FORBIDDEN = 403

# ── 端点 ───────────────────────────────────────────────────────────────────────
LOGS_URL = "/api/accounts/realname-logs/"
SETTING_URL = "/api/accounts/realname-setting/"
QUOTA_URL = "/api/accounts/realname-quota/"


def reset_url(pk) -> str:
    return f"/api/accounts/{pk}/reset-realname/"


# ══════════════════════════════════════════════════════════════════════════════
# 夹具构造器
# ══════════════════════════════════════════════════════════════════════════════
def make_package(**overrides) -> RealNamePackage:
    """合成密钥包（默认 10 次、未消耗、启用、不过期）。

    ⚠️ `key_id` / `key_secret` 用合成值：用例 6 钉的就是「它们**不出现在**响应里」（按**值**判）。
    """
    fields = {
        "key_id": FAKE_KEY_ID,
        "key_secret": FAKE_KEY_SECRET,
        "remark": "合成包-T8",
        "total_quota": 10,
        "used_quota": 0,
        "is_active": True,
        "expires_at": None,
    }
    fields.update(overrides)
    return RealNamePackage.objects.create(**fields)


def make_record(user, *, name, id_card_no, result, applied_at=None, **overrides):
    """一条核验流水（默认 `basic` / 申请时间 = now）。"""
    fields = {
        "user": user,
        "user_name_snapshot": user.username,
        "name": name,
        "id_card_no": id_card_no,
        "gender": "男",
        "birthday": "19900307",
        "age": 30,
        "cert_method": CertMethod.BASIC,
        "result": result,
        "applied_at": applied_at or timezone.now(),
        "certified_at": timezone.now(),
    }
    fields.update(overrides)
    return RealNameCertRecord.objects.create(**fields)


class _AdminCase(TenantTestCase):
    """公共夹具：三个账号（管理员持 1263 / 管理员持 1264 / 管理员一个节点都没有）。

    ⚠️ **起点由用例自己声明**：本文件一半判据是「某个东西**没变**」，起点若由 `setUp`
    悄悄造出来，用例就看不出它本来是多少（恒真判据的温床，与 `test_realname_verify_api.py`
    同一条口径）。
    """

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)  # 挡住上一条用例残留的 schema 上下文
        seed_catalog()
        self.schema = self.tenant.schema_name
        # 列表 / 设置 / 余量：持 1263。
        self.client_records = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="t8-records-admin",
            groups=(TENANT_ADMIN,),
            nodes=(NODE_RECORDS,),
            games=None,
        )
        # 重置：持 1264。
        self.client_reset = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="t8-reset-admin",
            groups=(TENANT_ADMIN,),
            nodes=(NODE_RESET,),
            games=None,
        )
        # 无节点：**显式 opt-out**（`nodes` 默认空集就是这一支，写出来是为了可读）。
        self.client_no_node = make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="t8-no-node-admin",
            groups=(TENANT_ADMIN,),
            nodes=(),
            games=None,
        )

    # ── 便捷断言 ──────────────────────────────────────────────────────────────
    def body(self, response):
        assert response.status_code == 200, (response.status_code, response.content[:300])
        return response.json()

    def rows_of(self, response):
        """列表响应的 `data.items`（形状由 `core.envelope.paginated` 定义）。"""
        return self.body(response)["data"]["items"]

    def usernames_of(self, response):
        """列表响应里那一页的 `user_name` 有序表（按返回顺序）。"""
        return [row["user_name"] for row in self.rows_of(response)]


# ══════════════════════════════════════════════════════════════════════════════
# 1. 权限：无 1263 ⇒ 三个端点全被拒；有 1263 ⇒ 200
# ══════════════════════════════════════════════════════════════════════════════
class RecordsNodeGateTests(_AdminCase):
    """1263 / 1264 这两扇门到底拦住了谁。

    ⚠️ **先说清本类测得出什么、测不出什么**（`AGENTS.md` §5-E 的「自指/恒真」一族）：
    ⭐ 2026-09-23「判据统一」之后，`nodes_of()` / `has_node_now()` 对**租户管理员**
    （持「系统管理员」职位）**短路为全目录**（`apps/rbac/permissions.py`），而
    `IsAccountAdmin` 认的正是同一个谓词 ⇒ 管理员**无论如何**都过节点门。
    所以「同一个管理员 client 摘掉节点 ⇒ 403」这种断言**在本项目里不可能成立**，
    拿它当判据只会得到一个恒绿的假保证（先例与原文见 `test_rbac_batch2_system_user.py`
    的 `_assert_pair` docstring）。

    ⇒ 本类钉的是三件**真的能失败**的事：
    ① 非管理员**不持**该节点 ⇒ 403（文案来自**节点门**）；**持**该节点 ⇒ 200
       （⛔ **线 D更新**：账号端点的准入已从 `IsAccountAdmin` 换成
       `IsAccountManager`——持任一账号管理节点即可，**不要求是管理员**。原文写的是
       「非管理员即便持该节点也被第一道门拒 403」，那句随线 D 作废）；
    ② 管理员持节点 ⇒ 200 + `code=0`（端点真的通，不是「被别的原因挡住也算过」）；
    ③ **action → 节点 code** 的映射：由 `AccountViewSet.ACCOUNT_PERMISSIONS` 的字面表
       在 `test_rbac_batch2_system_user.py::test_the_per_action_node_mapping_is_exactly_this_literal_table`
       里钉着（那里同一次补了本线的四条），本类只做一次**这里也能读到**的正向复核。
    """

    def test_00_the_clients_really_work(self):
        """⭐ 夹具自证：三条端点真的通（否则下面所有「有节点 ⇒ 200」都不可信）。"""
        self.assertEqual(self.body(self.client_records.get(LOGS_URL))["code"], OK)
        self.assertEqual(self.body(self.client_records.get(SETTING_URL))["code"], OK)
        self.assertEqual(self.body(self.client_records.get(QUOTA_URL))["code"], OK)

    def _non_admin_client(self, username, node):
        """建一个**非管理员**、**真的持着** `node` 的账号，返回它的已登录 client。

        ⚠️ **必须给一个非空的 group**（本 helper 用 `SERVICE`）：
        `make_logged_in_client(groups=())` 一个组都不建，而 `grant_nodes` 是往
        **该用户当前所在每个 Group 对应的 `Role`** 上挂节点 ⇒ 零组时**一个节点都挂不上**，
        「持节点」那句话变成假的（本文件第一版就踩了这个，表现为"闸门来源"断言红）。
        ⚠️ 用户名每次都不同（同一条用例里可能调多次）。
        """
        from tests.p5_auth_helpers import grant_nodes

        client = make_logged_in_client(
            domain=TENANT_DOMAIN, username=username, groups=(SERVICE,), games=None
        )
        user = User.objects.get(username=username)
        grant_nodes(user, node)
        # 夹具自证：不查这一句的话，"持节点"只是我嘴上说的。
        self.assertIn(node, user_node_codes(user), "节点没挂上（夹具坏了）")
        return client

    def test_01_a_non_admin_is_denied_without_the_node_and_admitted_with_it(self):
        """① 非管理员打三个 1263 端点：**不持节点 ⇒ 403（节点门）；持节点 ⇒ 200**。

        ⚠️ **口径变更（线 D）**：原文断言「持节点仍 403，且文案不是节点门的
        （= 被 `IsAccountAdmin` 拒）」。线 D 把账号端点的准入换成 `IsAccountManager`
        （持任一账号管理节点即可，**不要求是管理员**）⇒ 持 1263 的**非管理员现在 200**
        （原文那句会红）。判据没有放宽，只是换了半边：
        **不持**节点的那一半仍 403，且文案**恰好是节点门那句**（读得出是哪一扇门拒的）。
        """
        from apps.accounts.views import AccountViewSet

        # ⚠️ **两个变体必须用两个不同的 Group**：`_non_admin_client` 把节点挂在
        # **该用户所在每个 Group 对应的 `Role`** 上，而 `grant_nodes` 是**并集**。
        # 两个变体同用 `SERVICE` 时，给 holder 挂节点**顺带也挂给了 plain**
        # ⇒「不持节点」那句话当场变成假的（线 D 之前两边都被管理员门拒，所以这个洞看不出来；
        # 线 D 之后它表现成「plain 也能 200」）。`WORKER` 的默认模块里没有任何账号节点。
        plain = make_logged_in_client(
            domain=TENANT_DOMAIN, username="t8-plain-1263", groups=(WORKER,), games=None
        )
        holder = self._non_admin_client("t8-holder-1263", NODE_RECORDS)

        for method, url in (
            ("get", LOGS_URL),
            ("get", SETTING_URL),
            ("get", QUOTA_URL),
            ("patch", SETTING_URL),
        ):
            payload = {"enabled": True} if method == "patch" else None
            with self.subTest(method=method, url=url, variant="不持节点"):
                call = getattr(plain, method)
                response = (
                    call(url, payload, content_type="application/json")
                    if payload is not None
                    else call(url)
                )
                self.assertEqual(
                    response.status_code, FORBIDDEN, response.content[:200]
                )
                self.assertEqual(
                    response.json()["msg"],
                    AccountViewSet.NODE_DENIED_MESSAGE,
                    "这条 403 应当来自**节点门**（准入那一扇他本来也过不去，"
                    "但节点门排在前面）",
                )
            with self.subTest(method=method, url=url, variant="持节点"):
                call = getattr(holder, method)
                response = (
                    call(url, payload, content_type="application/json")
                    if payload is not None
                    else call(url)
                )
                self.assertEqual(response.status_code, 200, response.content[:200])
                self.assertEqual(response.json()["code"], OK, response.content[:300])

    def test_02_a_non_admin_is_denied_without_the_node_and_admitted_with_it(self):
        """① 的同一形状，打在重置那条端点上（1264 的持 / 不持两个变体）。

        ⚠️ 与 `test_01` 同一条线 D 口径变更：持 1264 的**非管理员现在能重置** ⇒
        「终态不动」只能断言在**不持节点**那一半上；持节点那一半反过来要断言
        **终态真的变了**（否则「200 + code 0」可能由别的原因凑出来）。
        """
        from apps.accounts.views import AccountViewSet

        target = User.objects.create_user(username="t8-plain-reset-target")
        StaffProfile.objects.create(user=target, id_card_no=ID_CARD_A)
        # ⚠️ 与 `test_01` 同一条理由：两个变体必须分处两个 Group（`grant_nodes` 是并集，
        # 同组会把 holder 的节点顺带挂给 plain）。
        plain = make_logged_in_client(
            domain=TENANT_DOMAIN, username="t8-plain-1264", groups=(WORKER,), games=None
        )
        holder = self._non_admin_client("t8-holder-1264", NODE_RESET)

        # 不持节点 ⇒ 403（节点门），且**终态一行都没动**。
        denied = plain.post(reset_url(target.pk))
        self.assertEqual(denied.status_code, FORBIDDEN, denied.content[:200])
        self.assertEqual(denied.json()["msg"], AccountViewSet.NODE_DENIED_MESSAGE)
        self.assertEqual(StaffProfile.objects.get(user=target).id_card_no, ID_CARD_A)
        self.assertEqual(RealNameCertRecord.objects.filter(user=target).count(), 0)

        # 持节点 ⇒ 200 + code 0，且**终态真的变了**（档案清空 + 一条 `fail` 流水）。
        allowed = holder.post(reset_url(target.pk))
        self.assertEqual(allowed.status_code, 200, allowed.content[:200])
        self.assertEqual(allowed.json()["code"], OK, allowed.content[:300])
        self.assertEqual(StaffProfile.objects.get(user=target).id_card_no, "")
        self.assertEqual(
            RealNameCertRecord.objects.filter(
                user=target, result=CertResult.FAIL
            ).count(),
            1,
        )

    def test_03_the_mapping_is_the_literal_table_this_task_added(self):
        """③ 正向复核本线那四条登记（字面量，⛔ 不拿 `ACCOUNT_PERMISSIONS` 自己当期望值）。

        ⚠️ 真正钉死映射的是 `test_rbac_batch2_system_user.py` 的那张字面表
        （本条只是让「本任务加了哪四条」在**本文件**里可读、且改错了在这里也会红）。
        """
        from apps.accounts.views import AccountViewSet

        table = dict(AccountViewSet.ACCOUNT_PERMISSIONS)
        self.assertEqual(table["realname_logs"], ("system_user.realname_records",))
        self.assertEqual(table["realname_setting"], ("system_user.realname_records",))
        self.assertEqual(table["realname_quota"], ("system_user.realname_records",))
        self.assertEqual(table["reset_realname"], ("system_user.reset_realname",))
        # 反面控制：这两个 code 必须是**目录里真有的**（拼错时上面四条照样能通过）。
        from apps.rbac.catalog import ALL_CODES

        self.assertIn(NODE_RECORDS, ALL_CODES)
        self.assertIn(NODE_RESET, ALL_CODES)

    def test_04_an_admin_with_the_nodes_gets_the_full_path(self):
        """② 管理员持节点：四条端点全部 200 + `code=0`（含写那条端点的真实终态）。"""
        target = User.objects.create_user(username="t8-admin-reset-target")
        StaffProfile.objects.create(user=target, id_card_no=ID_CARD_A)

        self.assertEqual(self.body(self.client_records.get(LOGS_URL))["code"], OK)
        self.assertEqual(self.body(self.client_records.get(SETTING_URL))["code"], OK)
        self.assertEqual(self.body(self.client_records.get(QUOTA_URL))["code"], OK)
        # 重置：持 1264 的管理员 ⇒ 200，且**终态**变了（不是只看信封）。
        self.assertEqual(
            self.body(self.client_reset.post(reset_url(target.pk)))["code"], OK
        )
        self.assertEqual(StaffProfile.objects.get(user=target).id_card_no, "")
        self.assertEqual(
            RealNameCertRecord.objects.filter(
                user=target, result=CertResult.FAIL
            ).count(),
            1,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 2. 列表：`status` 三档（断言**行内容**，⛔ 不只看 code）
# ══════════════════════════════════════════════════════════════════════════════
class LogsStatusFilterTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.pass_user = User.objects.create_user(username="t8-status-pass")
        self.fail_user = User.objects.create_user(username="t8-status-fail")
        self.other_user = User.objects.create_user(username="t8-status-other")
        make_record(self.pass_user, name=NAME_A, id_card_no=ID_CARD_A, result=CertResult.PASS)
        make_record(self.fail_user, name=NAME_B, id_card_no=ID_CARD_B, result=CertResult.FAIL)
        # ⭐ 第二条 pass：用来证明 `status=2` 不是「碰巧只剩一行」。
        make_record(self.other_user, name=NAME_A, id_card_no=ID_CARD_A, result=CertResult.PASS)

    def test_10_status_0_returns_every_row(self):
        """`status=0` = **全部**（三行都在）——⚠️ `0` 是取值、不是「没筛选」的同义词。"""
        rows = self.rows_of(self.client_records.get(LOGS_URL, {"status": "0"}))
        self.assertEqual(len(rows), 3, rows)
        self.assertEqual(
            sorted(row["result"] for row in rows), ["fail", "pass", "pass"]
        )

    def test_11_status_1_returns_only_the_failed_row(self):
        """`status=1` = 实名失败 ⇒ **恰好**那一行（按 `user_name` 认，⛔ 不按 pk）。"""
        rows = self.rows_of(self.client_records.get(LOGS_URL, {"status": "1"}))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["user_name"], "t8-status-fail")
        self.assertEqual(rows[0]["result"], CertResult.FAIL)

    def test_12_status_2_returns_only_the_passed_rows(self):
        """`status=2` = 实名通过 ⇒ **两行**（那两条 pass），且都不是 fail 那条。"""
        rows = self.rows_of(self.client_records.get(LOGS_URL, {"status": "2"}))
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(
            sorted(row["user_name"] for row in rows),
            ["t8-status-other", "t8-status-pass"],
        )
        for row in rows:
            self.assertEqual(row["result"], CertResult.PASS)

    def test_13_a_bad_status_is_rejected_at_the_serializer(self):
        """`status=9` ⇒ 200 + code 1000（⛔ **不是**静默返回全部）。

        ⚠️ 这一条是「`0` 是全部」那条的**反面控制**：如果实现把未知取值也当"不过滤"，
        本用例会以「拿到 3 行、code=0」的形式红。
        """
        response = self.client_records.get(LOGS_URL, {"status": "9"})
        body = self.body(response)
        self.assertEqual(body["code"], VALIDATION)
        self.assertIsNone(body["data"])


# ══════════════════════════════════════════════════════════════════════════════
# 3. 列表：三个关键词
# ══════════════════════════════════════════════════════════════════════════════
class LogsKeywordFilterTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.u1 = User.objects.create_user(username="t8-key-alpha")
        self.u2 = User.objects.create_user(username="t8-key-beta")
        self.u3 = User.objects.create_user(username="t8-key-gamma")
        make_record(self.u1, name=NAME_A, id_card_no=ID_CARD_A, result=CertResult.PASS)
        make_record(self.u2, name=NAME_B, id_card_no=ID_CARD_B, result=CertResult.FAIL)
        make_record(self.u3, name=NAME_B, id_card_no=ID_CARD_A, result=CertResult.FAIL)

    def test_20_key1_matches_the_login_account_snapshot(self):
        """`key1` 摸 `user_name_snapshot`（系统用户名称）。"""
        rows = self.rows_of(self.client_records.get(LOGS_URL, {"key1": "t8-key-alpha"}))
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["user_name"], "t8-key-alpha")

    def test_21_key2_matches_the_real_name(self):
        """`key2` 摸 `name`（真实姓名）：`NAME_B` 那两行。"""
        rows = self.rows_of(self.client_records.get(LOGS_URL, {"key2": NAME_B}))
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual(sorted(row["name"] for row in rows), [NAME_B, NAME_B])

    def test_22_key3_matches_the_id_card(self):
        """`key3` 摸 `id_card_no`（身份证号）：`ID_CARD_A` 那两行。"""
        rows = self.rows_of(self.client_records.get(LOGS_URL, {"key3": ID_CARD_A}))
        self.assertEqual(len(rows), 2, rows)
        for row in rows:
            # ⚠️ 响应里是**脱敏号** ⇒ 按脱敏形状比，⛔ 不拿完整号比（那会让本用例恒假）。
            self.assertEqual(row["id_card_no"], f"{ID_CARD_A[:3]}***{ID_CARD_A[-2:]}")


# ══════════════════════════════════════════════════════════════════════════════
# 3b. ⭐ 列表：`sdate` / `edate` 的日期区间（R2-1 补的零覆盖）
#
# 独立实测：T8 交付时 `grep -rn "sdate|edate" src/tests/` **零命中**
# ⇒ 「少一天」/「`<= edate 00:00`」/ naive-datetime 三种回归**一条用例都不会红**。
# 下面四条各自对着一种写法，且**每条都能被弄红**（证明见 R2 报告里的变异读数）。
#
# ⭐ 时区口径：`config/settings.py:157-159` 是 `TIME_ZONE = "Asia/Shanghai"` + `USE_TZ = True`。
# 本仓全部 `applied_at` 都是**带时区**的（写入侧 `timezone.now()`），列也是 `timestamptz`
# ⇒ 比较的是**绝对时刻**。所以下面一律用 `dt(…, tzinfo=datetime.UTC)` **写死 UTC 偏移**
# 造记录（⛔ 不依赖 `timezone.get_current_timezone()` 去凑，那会让"在哪个时区"变成环境变量）。
#
# ⚠️ **A22 分辨什么**：视图那侧若把裸 `date` 直接喂进 ORM（`datetime.fromisoformat("2026-06-13")`
# 这条写法），Django 不报错、但**按 UTC 解释**（与写入侧差 8 小时）⇒ 「某天的记录莫名跑到前一天」。
# A22 造的那条记录在当地 8 时、UTC 却是**前一天** ⇒ 它专打这个 8 小时错位，⛔ 不是只比日期串。
# ══════════════════════════════════════════════════════════════════════════════
class LogsDateRangeFilterTests(_AdminCase):
    def test_a20_the_edate_day_itself_is_included(self):
        """⭐ **杀手判据**：`applied_at` 落在 `edate` 当天 **23:59:59** ⇒ 必须被返回。

        三件事一起分辨（各自都是一条真实写法的回归）：

        ① 半开区间的上界写成 `<= edate 00:00`（把 `+ timedelta(days=1)` 删掉）
           ⇒ 上界正好退到**当天 00:00** ⇒ 那条 23:59:59 落在边界之外 ⇒ 本用例红
           （⭐ 这条是 R2 变异的**目标判据**，原始读数见 R2 报告）；
        ② 拿 `date` 与 `DateTimeField` 比（值被截断成"某天 00:00"）⇒ 同 ① 一样把它排掉；
        ③ 用 `DATE(applied_at)` 之类**丢掉时刻**的写法 ⇒ 「当天 00:00 之后」的行一起消失。

        区间用「前 / **边界** / 后」三条记录：只断言行数**分不出**是"少一天"还是"全没返回"
        ⇒ 同时钉住返回的那一行是**哪一行**（按 `applied_at` 的值认，见下面 assertEqual）。
        ⚠️ 记录一律按 **UTC 绝对时刻**写（本仓 `USE_TZ=True` ⇒ 落库是 `timestamptz`），
        ⛔ 不拿 `timezone.localtime()` 去凑"当地 23:59"——那会让本用例依赖机器时区。
        """
        user = User.objects.create_user(username="t8-date-kept")
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 15, 15, 59, 59, tzinfo=UTC),
        )
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 16, 15, 59, 59, tzinfo=UTC),
        )
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 16, 16, 0, 1, tzinfo=UTC),
        )

        rows = self.rows_of(
            self.client_records.get(
                LOGS_URL, {"sdate": "2026-06-16", "edate": "2026-06-16"}
            )
        )

        self.assertEqual(len(rows), 1, rows)
        # ⚠️ 钉**值**而不是常量：这一行就是 6-16 那条。
        # ⚠️ 响应里的 `applied_at` 是**实测读数**：D-0302起行装配走 `local_iso`
        # （**项目时区 +08:00** 的墙钟——管理端截串直显，旧写法 UTC `…Z` 差 8 小时），
        # 而"这条记录属于本地 6-16"由本行左边的区间（本地 6-16 当天）证明，⛔ 不靠这个字符串。
        self.assertEqual(rows[0]["applied_at"], "2026-06-16T23:59:59+08:00")

    def test_a21_the_day_after_edate_is_excluded(self):
        """`edate + 1 天 00:00`（**本地**）是**排他**上界 ⇒ 正好落在那一刻的记录**不许**返回。

        对 `<= edate 00:00` 那条写法，这条 00:00 也恰好在边界上（同样被排掉）⇒ 它守的是
        **上界的开闭**本身：把上界写成 `__lte` + `edate+1d` ⇒ 本用例多收这一行 ⇒ 红。

        ⚠️ 那一刻用 `make_aware(当天 00:00, 当前时区)` 造（与实现**同一个换算**）：
        ⛔ 不手算"本地 00:00 等于几个 UTC 点"——手算出来的数会在机器时区一变就悄悄错 8 小时
        （本文件第一版就踩了：把 23:59:59 写成 UTC 时刻，实际落在本地 6-16 07:59:59）。
        """
        user = User.objects.create_user(username="t8-date-next-midnight")
        make_record(
            user,
            name=NAME_B,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 16, 4, 0, 0, tzinfo=UTC),
        )
        make_record(
            user,
            name=NAME_B,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            # ⭐ `edate + 1d` 的**本地 00:00**（与实现同一个换算，⛔ 不手算 UTC 偏移）。
            applied_at=timezone.make_aware(
                datetime.combine(date(2026, 6, 17), datetime.min.time()),
                timezone.get_current_timezone(),
            ),
        )

        rows = self.rows_of(
            self.client_records.get(
                LOGS_URL, {"sdate": "2026-06-16", "edate": "2026-06-16"}
            )
        )

        self.assertEqual(len(rows), 1, rows)
        # D-0302：UTC 04:00 ⇒ 载荷是北京墙钟 12:00（+08:00）。
        self.assertEqual(rows[0]["applied_at"], "2026-06-16T12:00:00+08:00")

    def test_a22_the_range_is_aligned_to_local_dates_not_utc(self):
        """⭐ 区间按**本仓时区**（`Asia/Shanghai`，UTC+8）解释，⛔ 不是按 UTC。

        分辨点**不在**「两边界的绝对时刻」上：`applied_at` 是 `timestamptz`，比的是**绝对时刻**，
        而「本地 6-13 00:00」与「UTC 6-13 00:00」这两个**窗口**在 16 小时的错位里**互相错开**
        ⇒ 可分辨的记录是「本地日期与 UTC 日期**不是同一天**」的那两条：

        | 记录 | 写入（UTC 绝对时刻） | 本地 | 正确实现 | 按 UTC 解释 |
        |---|---|---|---|---|
        | ① | `2026-06-12T16:00:00Z` | **6-13** 00:00 | ✅ 在内 | ⛔ 被排掉（它的 UTC 日期是 6-12） |
        | ② | `2026-06-13T20:00:00Z` | **6-14** 04:00 | ⛔ 排掉 | ✅ 被误收（UTC 日期是 6-13） |
        | ③ 对照 | `2026-06-13T12:00:00Z` | 6-13 20:00 | ✅ 在内 | ✅ 在内 |

        请求 `sdate=edate=2026-06-13`：

        - 正确（`make_aware(当天 00:00, 当前时区)`）⇒ **2 行**：① 与 ③；
        - `make_aware(…, ZoneInfo("UTC"))` 这一族 ⇒ **另一个集合**：② 与 ③
          ⇒ 行数一样是 2，但**成员不同** ⇒ 本用例红（**实测已杀**，读数见 R2 报告）。

        ⚠️ 所以下面**同时**钉「哪两行」（按 `applied_at` 的值排序断言），⛔ 不只断行数
        ——只断行数的话这一族错误会**恰好**是 2 行而漏过（这就是"看起来对了"的假绿）。
        ⚠️ ③ 是对照：两种解释都在区间内 ⇒ 它保证"有返回"不是靠没筛东西凑出来的。

        ⭐ **裸 `datetime` 那一族由另一条判据钉**（`test_a22b` 的 helper）：实测
        把 `_day_start` 改成**返回裸 `datetime`**（不做 `make_aware`）时，Django 的
        `DateTimeField.get_prep_value` 会把它**按当前时区**（`Asia/Shanghai`）补上 tzinfo
        ——**不是按 UTC**（这与 `views.py` 那条注释原先的说法相反，已按实测改准）
        ⇒ **行集断言抓不到它**（变异 `M-C` 就是这样 SURVIVED 的）。
        """
        user = User.objects.create_user(username="t8-date-local-tz")
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.FAIL,
            applied_at=datetime(2026, 6, 12, 16, 0, 0, tzinfo=UTC),
        )
        make_record(
            user,
            name=NAME_B,
            id_card_no=ID_CARD_B,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 13, 20, 0, 0, tzinfo=UTC),
        )
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC),
        )

        rows = self.rows_of(
            self.client_records.get(
                LOGS_URL, {"sdate": "2026-06-13", "edate": "2026-06-13"}
            )
        )

        self.assertEqual(len(rows), 2, rows)
        # D-0302：两条 UTC 时刻的北京墙钟分别是 6-13 00:00 与 6-13 20:00（+08:00）。
        self.assertEqual(
            sorted(row["applied_at"] for row in rows),
            ["2026-06-13T00:00:00+08:00", "2026-06-13T20:00:00+08:00"],
        )

    def _get_rows_without_naive_warning(self, params):
        """请求一次并把「ORM 收到 naive datetime」那条 `RuntimeWarning` 一并抓出来。

        ⚠️ **为什么单独有这条**（同族：`AGENTS.md` §5-E「断言副作用要打在真实出口上」）：
        行集断言**分辨不出**"传了裸 `datetime`"这一族错误 —— Django 会把裸值
        **按当前时区**补齐（实测），而当前时区恰好就是正确实现要的那个 ⇒ 行集一模一样、全绿。
        实测读数：变异 `M-C`（`_day_start` 直接 `return datetime.combine(...)`）
        ⇒ `Ran 33 tests / OK`（**存活**）⇒ 所以钉它只能靠这条警告。

        ⚠️ 这里不是"把警告当中间层断言"：警告**就是**「传了个无时区的值」唯一的信号，
        而它的后果（按当前时区解释）在生产里会随 `TIME_ZONE` 变化而**静默**改变筛选结果。
        ⚠️ `simplefilter("always")` 是必须的：Python 默认的 warning 过滤会**去重**
        （同一条只报一次）⇒ 别的用例先触发过之后这里就再也看不见它了。
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rows = self.rows_of(self.client_records.get(LOGS_URL, params))
        naive = [
            str(w.message)
            for w in caught
            if issubclass(w.category, RuntimeWarning) and "naive datetime" in str(w.message)
        ]
        return rows, naive

    def test_a22b_the_filter_value_is_timezone_aware_not_naive(self):
        """⛔ 进 ORM 的筛选值**必须是带时区**的（裸值会触发 Django 的 naive-datetime 警告）。

        ⚠️ 这条是 `test_a22` 的**必要补充**，不是重复：那一族的行集与正确实现**一模一样**
        （见 helper 的注释与实测读数），所以只有警告能分辨它。
        """
        user = User.objects.create_user(username="t8-date-naive-guard")
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 13, 4, 0, 0, tzinfo=UTC),
        )

        rows, naive = self._get_rows_without_naive_warning(
            {"sdate": "2026-06-13", "edate": "2026-06-13"}
        )

        self.assertEqual(naive, [], f"筛选用的日期被当成 naive 值传进了 ORM：{naive}")
        # 夹具自证：这一次筛选**确实生效**（否则"没有警告"可能只是"压根没查"）。
        self.assertEqual(len(rows), 1, rows)
        # D-0302：UTC 04:00 ⇒ 载荷是北京墙钟 12:00（+08:00）。
        self.assertEqual(rows[0]["applied_at"], "2026-06-13T12:00:00+08:00")

    def test_a23_the_range_filters_on_applied_at_not_created_at(self):
        """筛选口径是 **`applied_at`**（申请时间，列表显示的那一列），⛔ 不是 `created_at`。

        两条记录的 `created_at` **都是 now**（`auto_now_add`，造不出过去值），而 `applied_at`
        一条在今年 1 月、一条在 6 月 ⇒ 查 6 月那个区间：

        - 正确实现：只剩 6 月那条 ⇒ 1 行；
        - 写成 `created_at__range`（两者在"正常用例"里往往同一天，**页面看不出来**）：
          两条 `created_at` 都是今天 ⇒ **2 行** ⇒ 红。
        """
        user = User.objects.create_user(username="t8-date-column")
        make_record(
            user,
            name=NAME_A,
            id_card_no=ID_CARD_A,
            result=CertResult.PASS,
            applied_at=datetime(2026, 1, 1, 4, 0, 0, tzinfo=UTC),
        )
        make_record(
            user,
            name=NAME_B,
            id_card_no=ID_CARD_B,
            result=CertResult.PASS,
            applied_at=datetime(2026, 6, 16, 4, 0, 0, tzinfo=UTC),
        )
        # 夹具自证：两条都在库里（否则「只剩 1 行」可能只是"库里就 1 行"）。
        self.assertEqual(RealNameCertRecord.objects.count(), 2)

        rows = self.rows_of(
            self.client_records.get(
                LOGS_URL, {"sdate": "2026-06-16", "edate": "2026-06-16"}
            )
        )

        self.assertEqual(len(rows), 1, rows)
        # D-0302：UTC 04:00 ⇒ 载荷是北京墙钟 12:00（+08:00）。
        self.assertEqual(rows[0]["applied_at"], "2026-06-16T12:00:00+08:00")


# ══════════════════════════════════════════════════════════════════════════════
# 4. ⭐ 脱敏 / ⭐ 不含密钥 / 默认值
# ══════════════════════════════════════════════════════════════════════════════
class LogsPayloadTests(_AdminCase):
    def test_30_id_card_is_masked_with_the_shared_helper(self):
        """⭐ 响应里的 `id_card_no` = `payloads._mask_pii` 的结果，且**不等于**完整号。

        ⚠️ 断言的是**算法**（import 那个函数来算期望值）+ **不等式**（不等于完整号）。
        只写「等于某个固定串」在脱敏算法被换成"原样返回"时**不会红**（那串照样对得上）。
        """
        from apps.accounts.payloads import _mask_pii

        user = User.objects.create_user(username="t8-mask-user")
        make_record(user, name=NAME_A, id_card_no=ID_CARD_A, result=CertResult.PASS)

        rows = self.rows_of(self.client_records.get(LOGS_URL))
        self.assertEqual(len(rows), 1, rows)
        masked = rows[0]["id_card_no"]
        self.assertEqual(masked, _mask_pii(ID_CARD_A))
        self.assertNotEqual(masked, ID_CARD_A, "身份证号被原样返回了")
        self.assertNotIn(ID_CARD_A, str(rows[0]), "响应里还带着完整号码")

    def test_31_no_key_material_in_the_list_or_setting_response(self):
        """⭐ 列表与设置响应里**都没有** `key_id` / `key_secret` 的**值**（按值判，⛔ 不按键判）。

        ⚠️ 「键不在」是弱判据（改个名字就绕过去了）⇒ 本用例同时按**值**搜整个响应体。
        """
        make_package()  # 库里真的有一份带密钥的包（否则本用例是空转）
        user = User.objects.create_user(username="t8-nokey-user")
        make_record(user, name=NAME_A, id_card_no=ID_CARD_A, result=CertResult.PASS)

        for url in (LOGS_URL, SETTING_URL, QUOTA_URL):
            with self.subTest(url=url):
                raw = self.client_records.get(url).content.decode("utf-8")
                self.assertNotIn(FAKE_KEY_SECRET, raw, f"{url} 的响应里出现了 key_secret")
                self.assertNotIn(FAKE_KEY_ID, raw, f"{url} 的响应里出现了 key_id")

    def test_32_first_read_of_a_fresh_tenant_returns_the_documented_defaults(self):
        """⭐ 新租户首次读 setting ⇒ 三个布尔 `False`、两个年龄 `0`（对标 `checked` 位置）。

        ⚠️ 起点断言**前置**：库里一开始**一行都没有**（`assertEqual(count, 0)`），
        否则"读出来是默认值"可能只是"读到了别处建好的行"，判据退化。
        """
        self.assertEqual(RealNameSetting.objects.count(), 0, "起点不是空表")

        data = self.body(self.client_records.get(SETTING_URL))["data"]

        self.assertIs(data["enabled"], False)
        self.assertIs(data["block_claim_without_cert"], False)
        self.assertIs(data["require_cert_for_settlement"], False)
        self.assertEqual(data["min_age"], 0)
        self.assertEqual(data["max_age"], 0)
        # 首读会建一行（`get_or_create_setting` 的语义）⇒ 终态恰好一行。
        self.assertEqual(RealNameSetting.objects.count(), 1)
        self.assertEqual(RealNameSetting.objects.get().tenant_schema, self.schema)


# ══════════════════════════════════════════════════════════════════════════════
# 5. ⭐ PATCH 不改未传的键 / 年龄校验 / 开关两态（结算仍只存不拦）
# ══════════════════════════════════════════════════════════════════════════════
class SettingPatchTests(_AdminCase):
    def patch(self, payload):
        return self.client_records.patch(
            SETTING_URL, payload, content_type="application/json"
        )

    def test_40_patch_leaves_the_keys_it_did_not_receive_alone(self):
        """⭐ 先设 `enabled=True`，再只 PATCH `min_age` ⇒ `enabled` 仍 `True`。

        ⚠️ 这一条正是 `data.get("enabled", False)` 那款写法的**唯一**杀手用例
        （变异验证 ①：把它改成 `data.get` 必须让本用例红）。
        """
        first = self.body(self.patch({"enabled": True}))["data"]
        self.assertIs(first["enabled"], True, "第一步没写进去，后面的判据不成立")

        second = self.body(self.patch({"min_age": 18}))["data"]
        self.assertEqual(second["min_age"], 18)
        self.assertIs(second["enabled"], True, "未传的 enabled 被改成了 False")

        # 终态：库里的值也一样（⛔ 不只看响应）。
        setting = RealNameSetting.objects.get()
        self.assertIs(setting.enabled, True)
        self.assertEqual(setting.min_age, 18)
        # 其余三个未传的键仍是各自的默认值。
        self.assertIs(setting.block_claim_without_cert, False)
        self.assertIs(setting.require_cert_for_settlement, False)
        self.assertEqual(setting.max_age, 0)

    def test_41_patch_can_turn_a_boolean_back_off(self):
        """反面控制：`false` 是**合法取值**，必须能写进去（`if "enabled" in data` 的两向都通）。"""
        self.patch({"enabled": True})
        data = self.body(self.patch({"enabled": False}))["data"]
        self.assertIs(data["enabled"], False)
        self.assertIs(RealNameSetting.objects.get().enabled, False)

    def test_42_all_four_values_round_trip(self):
        """四个值一次写全 ⇒ 库里与响应都逐项一致（不只看 code）。"""
        payload = {
            "enabled": True,
            "block_claim_without_cert": True,
            "require_cert_for_settlement": True,
            "min_age": 18,
            "max_age": 45,
        }
        data = self.body(self.patch(payload))["data"]
        for key, value in payload.items():
            with self.subTest(key=key):
                self.assertEqual(data[key], value)
        setting = RealNameSetting.objects.get()
        for key, value in payload.items():
            with self.subTest(key=key):
                self.assertEqual(getattr(setting, key), value)

    def test_43_max_smaller_than_min_is_rejected_with_a_chinese_message(self):
        """两个年龄都非 0 且 `max < min` ⇒ 200 + code 1000，且 `msg` 里有人话。"""
        response = self.patch({"min_age": 30, "max_age": 18})
        body = self.body(response)
        self.assertEqual(body["code"], VALIDATION)
        self.assertIn("最大年龄", body["msg"])
        # 终态：一行都没写（不是"校验失败但仍落库"）。
        self.assertEqual(RealNameSetting.objects.count(), 0)

    def test_44_zero_on_either_side_means_no_limit_and_is_not_a_conflict(self):
        """⭐ `0` = 不限制 ⇒ 任一侧为 `0` 时 `max < min` **不成立**（⚠️ 边界值判据，E-0021）。

        ⚠️ 拿 `0` 去比会造出一条**假的**校验失败（`min_age=30, max_age=0` 会被误判成 0 < 30）；
        本用例的两组都是"应当放行"的形状。
        """
        data = self.body(self.patch({"min_age": 30, "max_age": 0}))["data"]
        self.assertEqual(data["min_age"], 30)
        self.assertEqual(data["max_age"], 0)

        data = self.body(self.patch({"min_age": 0, "max_age": 18}))["data"]
        self.assertEqual(data["min_age"], 0)
        self.assertEqual(data["max_age"], 18)

    def test_45_a_partial_patch_is_checked_against_the_stored_value(self):
        """⭐ 跨字段校验看的是**合并后**的值：先存 `min_age=30`，再只 PATCH `max_age=18` ⇒ 拒。

        ⚠️ 只看本次载荷的写法会漏判这一档（`min_age` 不在 `attrs` 里）——
        症状是"分区间的两个值可以存成互相矛盾的组合"。
        """
        self.patch({"min_age": 30})
        response = self.patch({"max_age": 18})
        self.assertEqual(self.body(response)["code"], VALIDATION)
        self.assertEqual(RealNameSetting.objects.get().max_age, 0, "被拒的那次仍然写进去了")

    def test_46_the_settlement_switch_is_stored_but_still_enforces_nothing(self):
        """⭐ 两态如实（2026-10-16 起改准，E-0175）：`require_cert_for_settlement` **仍只存不拦**；
        `block_claim_without_cert` **已由接单/指派闸消费**（D-0245，判据见
        `tests/test_realname_claim_gate.py`）——本用例不再声称"两个开关都没有消费方"。

        判据打在**别的写路径**上：记着「结算要求实名」（`require_cert_for_settlement=True`）
        之后，一个**没有实名**的账号仍然照常登录、照常拿到自己的信息 ⇒ 说明结算开关
        今天没有任何消费方。⛔ 本用例**不**断言"将来也不会拦"（那是产品决策），
        只断言"**现在**它拦不住"——而那正是页面上那行**两态**提示要说的事
        （「未实名不允许接单」已生效；「结算要求实名」暂仅保存、尚未启用）。
        """
        self.patch({"block_claim_without_cert": True, "require_cert_for_settlement": True})
        setting = RealNameSetting.objects.get()
        self.assertIs(setting.block_claim_without_cert, True)
        self.assertIs(setting.require_cert_for_settlement, True)

        # 一个没有身份证号、也没有任何核验流水的账号 —— 「未实名」的定义。
        # ⚠️ 起点判据必须先立住（拿一个已实名的人测"没被拦"是恒真的）。
        staff_client = make_logged_in_client(
            domain=TENANT_DOMAIN, username="t8-not-realname-staff", games=None
        )
        staff = User.objects.get(username="t8-not-realname-staff")
        # ⚠️ 读档案要走 `StaffProfile.objects`（⛔ 不用 `staff.staff_profile`）：
        #    后者在没档案时抛 `RelatedObjectDoesNotExist`，而它是
        #    `ObjectDoesNotExist` 的子类、**不是** `AttributeError` ⇒ `getattr(…, 默认值)`
        #    **接不住它**（实测：那一行直接把用例变成 ERROR）。
        profile = StaffProfile.objects.filter(user=staff).first()
        self.assertFalse(
            bool(profile and profile.id_card_no),
            "起点不是未实名，本用例不成立",
        )
        self.assertEqual(
            RealNameCertRecord.objects.filter(user=staff).count(), 0, "起点有核验流水"
        )

        # 真实读写路径：登录（已经发生了，停在上面那个夹具里）+ 读自己的资料
        # ——**没被拦**就是"结算开关只存不拦"的证据（实名闸消费的是另一个开关，
        # 它的判据在 `tests/test_realname_claim_gate.py`，不在这里重复）。
        # ⛔ 本用例不断言接单 / 结算路径本身；取的是"一个未实名的子账号还能正常用系统"
        # 这个最小的可观察事实。
        profile = self.body(staff_client.get("/api/auth/profile"))
        self.assertEqual(profile["code"], OK)

    def test_47_a_bad_boolean_is_rejected_at_the_serializer(self):
        """`enabled="maybe"` ⇒ 1000（请求来源的值先过序列化器，E-0010）。"""
        response = self.patch({"enabled": "maybe"})
        self.assertEqual(self.body(response)["code"], VALIDATION)
        self.assertEqual(RealNameSetting.objects.count(), 0)


# ══════════════════════════════════════════════════════════════════════════════
# 6. 余量
# ══════════════════════════════════════════════════════════════════════════════
class QuotaTests(_AdminCase):
    def test_50_zero_when_there_is_no_grant_row(self):
        """没买过 ⇒ `0`（⛔ 不是 `None`：`None` 专表"读不到"，两者不许混）。"""
        data = self.body(self.client_records.get(QUOTA_URL))["data"]
        self.assertEqual(data["remaining"], 0)

    def test_51_remaining_is_granted_minus_used(self):
        """`granted=5, used=3` ⇒ `2`（整数减法；起点刻意非 0/非满，否则读数与两端重合）。"""
        RealNameGrant.objects.create(tenant=self.tenant, granted=5, used=3)
        data = self.body(self.client_records.get(QUOTA_URL))["data"]
        self.assertEqual(data["remaining"], 2)


# ══════════════════════════════════════════════════════════════════════════════
# 7. 重置：终态 = 新流水 + 档案被清空 + 一条留痕
# ══════════════════════════════════════════════════════════════════════════════
class ResetRealnameTests(_AdminCase):
    def setUp(self):
        super().setUp()
        self.target = User.objects.create_user(username="t8-reset-target")
        StaffProfile.objects.create(user=self.target, id_card_no=ID_CARD_A)
        self.pass_row = make_record(
            self.target, name=NAME_A, id_card_no=ID_CARD_A, result=CertResult.PASS
        )

    def test_60_reset_writes_a_new_fail_row_and_clears_the_profile(self):
        """⭐ 有 1264 ⇒ 200 且落一条新流水 + `StaffProfile.id_card_no` 被清空。

        四条判据（缺一不可）：
        ① 那条 `pass` 流水**原样还在**（流水只增不改 —— 它是核验历史，不是当前状态）；
        ② **新增**恰好一条 `fail` 流水，`vendor_message="管理员重置"`；
        ③ 档案上的号码被清成 `""`（那才让"未实名"重新成立）；
        ④ 留痕一行 `user.realname_reset`，正文含**被重置的人**。
        """
        response = self.client_reset.post(reset_url(self.target.pk))
        self.assertEqual(self.body(response)["code"], OK)

        # ① 旧流水没被动过（逐字段比，⛔ 不只看行数）。
        self.pass_row.refresh_from_db()
        self.assertEqual(self.pass_row.result, CertResult.PASS)
        self.assertEqual(self.pass_row.id_card_no, ID_CARD_A)
        self.assertEqual(self.pass_row.name, NAME_A)
        self.assertEqual(self.pass_row.vendor_message, "")  # 起点就是空串
        self.assertEqual(
            RealNameCertRecord.objects.filter(user=self.target).count(), 2,
            "流水行数不是 2（旧的那条被删了，或新的一条没写）",
        )

        # ② 新增的那条。
        added = RealNameCertRecord.objects.filter(
            user=self.target, result=CertResult.FAIL
        ).first()
        self.assertIsNotNone(added, "没有新增 result=fail 的重置流水")
        self.assertEqual(added.vendor_message, "管理员重置")
        self.assertEqual(added.cert_method, CertMethod.BASIC)
        self.assertEqual(added.id_card_no, ID_CARD_A, "重置流水应当记着被重置的那个号")
        self.assertIsNotNone(added.applied_at)
        self.assertIsNotNone(added.certified_at)

        # ③ 档案被清空（终态判据：重读库里那一行）。
        self.assertEqual(
            StaffProfile.objects.get(user=self.target).id_card_no, "",
            "档案上的身份证号没被清空 ⇒ 页面上仍然显示「已实名」，重置等于没做",
        )

        # ④ 留痕。
        rows = list(OperationLog.objects.filter(action="user.realname_reset"))
        self.assertEqual(len(rows), 1, [r.action for r in OperationLog.objects.all()])
        self.assertEqual(rows[0].log_type, 30)
        self.assertEqual(rows[0].target_id, str(self.target.pk))
        self.assertIn("t8-reset-target", rows[0].summary)
        self.assertIn("管理员重置", rows[0].summary)
        # ⛔ 完整身份证号**不进**日志（PII 纪律）。
        self.assertNotIn(ID_CARD_A, rows[0].summary)
        self.assertNotIn(ID_CARD_A, rows[0].reason)

    def test_61_reset_of_a_user_who_never_verified_still_returns_ok(self):
        """末实名过的人也允许重置：200 + 一条 `fail` 流水（终态幂等，如实登记）。"""
        fresh = User.objects.create_user(username="t8-never-verified")

        response = self.client_reset.post(reset_url(fresh.pk))

        self.assertEqual(self.body(response)["code"], OK)
        rows = RealNameCertRecord.objects.filter(user=fresh)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().result, CertResult.FAIL)
        self.assertEqual(rows.first().vendor_message, "管理员重置")

    def test_62_a_malformed_pk_is_a_business_error_not_a_500(self):
        """畸形 pk ⇒ 200 + code 1000（与 `retrieve` / `reset-password` 同一条守卫）。"""
        response = self.body(self.client_reset.post(reset_url("abc")))
        self.assertEqual(response["code"], VALIDATION)

    def test_63_an_unknown_pk_is_not_found(self):
        """不存在的 pk ⇒ 业务码（⛔ 不是 HTML 500）。"""
        response = self.client_reset.post(reset_url(987654321))
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], OK)
