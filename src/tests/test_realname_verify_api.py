"""T6：实名核验端点（`POST /api/auth/realname/verify/`、`GET /api/auth/realname/`）的判据。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` **§5**（端到端流程）、
**§6-2b**（计费口径与每一档的证据等级）、**§6-7**（事务边界）、**§9.1**（端点清单）；
任务书 T6 的 12 条硬要求。

## 本文件怎么钉（⛔ 判据一律打在**终态**上）

判据 = 库里的行：`RealNameCertRecord` 的行数与字段值、`RealNameGrant.used`、
`RealNamePackage.used_quota`、`StaffProfile.id_card_no`。
⛔ **不**把「调用了厂商」当主判据（`AGENTS.md` §5-E / E-0009：中间层会被测试框架改写）；
唯一的例外是**负向**用例里的 `stub.call_count == 0`（"根本没出网"这个事实**只能**在这里看），
它另有 `test_00` 的自证（桩真的接上了、且一次正常核验确实产生 1 次调用）。

## ⭐ 三处最容易写成**恒真**判据的地方（照 spec §12 的评审 A7，本文件刻意避开）

1. **幂等短路**：断言 `code == 0` 是**恒真**的（不短路、真调厂商也会回 `pass`）
   ⇒ 用例 04 的判据是「第二次提交前后 `used` 与流水行数**都不变**」（起点 `used=1`，⛔ 不是 0）。
2. **「不扣次数」**：起点 `used=0` 时断言 `used == 0` 恒真
   ⇒ 用例 01 的起点是 `used=3`，断言它**仍然是 3**。
3. **「没放行」**：只断言回 1026 不够（放行了也可能因为别的原因回 1026？那就反过来了——
   真正的恒真风险是"断言没扣但起点本来就是 0"）⇒ 用例 02 的起点是 `granted=1, used=1`。

## 厂商一律打桩（⛔ 绝不打真网）

照 `tests/test_realname_vendor.py` 的 `_VendorStub` 手法：把 `httpx.Client` 换成
「走 `httpx.MockTransport` 的真客户端」——**只换 socket 那一层**，URL / query / 签名头
都是被测代码自己拼的。`test_00` 是这条打桩的**自证**（没有它，"桩没接上"会以
"跑去打真网"的形式出现，而不是一条明确的红）。

⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：`*-T6SYNTH*` 的凭据、`110101…` 的
**合成**身份证号（校验位按 GB 11643 算出来的、不指向任何真人）、`测试姓名甲` 这类合成姓名。
"""
from __future__ import annotations

import threading
from unittest import mock

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import ProgrammingError, close_old_connections, connection
from django.db.models import QuerySet
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
    tenant_context,
)

from apps.accounts.models import StaffProfile
from apps.platform.bootstrap import PLATFORM_DOMAIN, ensure_public_tenant
from apps.platform.models import RealNameGrant, RealNamePackage, Tenant
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.realname import vendor
from apps.realname.models import RealNameCertRecord, RealNameSetting
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 平面 / 域名 ────────────────────────────────────────────────────────────────
#: 租户 A = `TenantTestCase` 自带的那个 schema（`test`），域名与既有测试同口径。
TENANT_DOMAIN = "tenant.test.com"
#: 租户 B（跨租户用例）。schema 名与域名都刻意不同（`Domain` 上有唯一约束）。
SCHEMA_B = "realname_iso_b"
DOMAIN_B = "realname-iso-b.test.local"

# ── 合成夹具（⛔ 一个真值都没有）────────────────────────────────────────────────
NAME_OK = "测试姓名甲"
NAME_OTHER = "测试姓名乙"
#: 合成的 18 位号（校验位按 GB 11643 算过 ⇒ 本地校验能过；**不指向任何真人**）。
ID_CARD_MALE = "110101199003070011"  # 男（第 17 位奇数）、生日 1990-03-07
ID_CARD_FEMALE = "11010119900307002X"  # 女（第 17 位偶数）
ID_CARD_MISMATCH = "110101198505120037"  # 厂商回「不一致」（result=2）
ID_CARD_NOT_FOUND = "110101200002290042"  # 厂商回「库中无此号」（result=3）
ID_CARD_OLD_MAN = "110101197001010067"  # 另一条流水用（跨租户用例）
FAKE_KEY_ID = "AKIDFAKE0000T6SYNTH"
FAKE_KEY_SECRET = "SYNTH-T6-0000-FAKE-SECRET"
FAKE_REQUEST_ID = "req-synthetic-t6-0001"

# ── 业务码的**字面值**（⛔ 不写 `ErrorCode.…`：那样码值被改掉时用例看不见，同 `test_realname_quota.py`）
OK = 0
VALIDATION = 1000
DISABLED = 1025
QUOTA_EXHAUSTED = 1026
NO_PACKAGE = 1027

#: 真 `httpx.Client`：模块级取好 ⇒ 换掉 `httpx.Client` 之后仍造得出**真的**客户端。
_REAL_HTTPX_CLIENT = httpx.Client

#: 并发用例的两个超时（⛔ 不是"容错"：它们是判据的一部分，见那里的注释）。
BARRIER_TIMEOUT = 3.0
THREAD_JOIN_TIMEOUT = 4 * BARRIER_TIMEOUT + 10


# ══════════════════════════════════════════════════════════════════════════════
# 假厂商（零网络）
# ══════════════════════════════════════════════════════════════════════════════
class _VendorStub:
    """零网络假厂商：`httpx.MockTransport` 只决定"对方回什么"，请求本身是真的。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._reply: tuple[int, str, object] | None = None
        self._error: Exception | None = None

    def json_reply(self, payload, *, status: int = 200) -> "_VendorStub":
        self._reply = (status, "json", payload)
        self._error = None
        return self

    def boom(self, exc: Exception) -> "_VendorStub":
        self._error = exc
        return self

    @property
    def call_count(self) -> int:
        """厂商被调用了几次（⚠️ 只在**负向**用例里用：`0` = 根本没出网）。"""
        return len(self.requests)

    def factory(self, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self.handler), **kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        status, kind, payload = self._reply
        return httpx.Response(status, json=payload)


# ══════════════════════════════════════════════════════════════════════════════
# 夹具构造器（模块级：几个用例类共用）
# ══════════════════════════════════════════════════════════════════════════════
def make_package(**overrides) -> RealNamePackage:
    """建一个合成密钥包（默认 10 次、未消耗、启用、不过期）。

    ⚠️ `key_secret` 一律用合成值：用例 12 钉的就是「它**不出现在**响应里」（按**值**判）。
    """
    fields = {
        "key_id": FAKE_KEY_ID,
        "key_secret": FAKE_KEY_SECRET,
        "remark": "合成包-T6",
        "total_quota": 10,
        "used_quota": 0,
        "is_active": True,
        "expires_at": None,
    }
    fields.update(overrides)
    return RealNamePackage.objects.create(**fields)


def make_grant(tenant, *, granted: int, used: int = 0) -> RealNameGrant:
    return RealNameGrant.objects.create(tenant=tenant, granted=granted, used=used)


def enable_setting(tenant_schema: str, **overrides) -> RealNameSetting:
    """建本租户的实名设置（默认**启用**；要测"未启用"就显式传 `enabled=False`）。"""
    fields = {"tenant_schema": tenant_schema, "enabled": True}
    fields.update(overrides)
    return RealNameSetting.objects.create(**fields)


def used_of(grant: RealNameGrant) -> int:
    """从库里**重读** `used`。

    ⚠️ 必须重读：`charge()` 把对象上的属性就地换成了 `F()` 表达式（"还没算出来的值"），
    拿内存里那个对象读会得到表达式对象而不是数字。
    """
    return RealNameGrant.objects.get(pk=grant.pk).used


def used_quota_of(package: RealNamePackage) -> int:
    return RealNamePackage.objects.get(pk=package.pk).used_quota


# ══════════════════════════════════════════════════════════════════════════════
# 核验端点（POST）
# ══════════════════════════════════════════════════════════════════════════════
class _VerifyApiCase(TenantTestCase):
    """公共夹具：登录用户 + 假厂商（**不**预建设置 / 额度行 / 包 —— 每个用例自己声明起点）。

    ⚠️ **不预建起点是有意的**：本文件一半的判据是"某个东西**没变**"，
    起点若由 `setUp` 悄悄造出来，用例就看不出"它本来是多少"（那正是恒真判据的温床）。
    """

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)  # 挡住上一条用例残留的 schema 上下文
        # ⚠️ D-0251 限流走进程级 locmem cache：桶**跨用例存活**（事务回滚
        #    回不去），不清的话前面用例的计数会把本用例的核验误判成"超频"。夹具卫生，
        #    ⛔ 不是判据改动（各用例的断言一个字没动）。
        cache.clear()
        self.addCleanup(cache.clear)
        self.schema = self.tenant.schema_name
        self.client_user = make_logged_in_client(domain=TENANT_DOMAIN, username="realname-user-a")
        self.user = User.objects.get(username="realname-user-a")
        self.stub = _VendorStub()
        patcher = mock.patch.object(vendor.httpx, "Client", self.stub.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 默认厂商回「一致」（result=1）—— 需要别的档的用例自己改。
        self.stub.json_reply(
            {
                "msg": "查询成功",
                "requestid": FAKE_REQUEST_ID,
                "code": 200,
                "data": {"result": "1", "message": "一致"},
            }
        )

    # ── 夹具自证 ──────────────────────────────────────────────────────────────
    def test_00_the_vendor_stub_is_actually_wired(self):
        """⭐ **桩接没接上本身要有判据**（`AGENTS.md` §5-E / E-0029）。

        ⚠️ 没有这一条时，"桩没接上"的表现是**跑去打真网**（或挂到超时），
        而不是一条指名道姓的红 —— 那会让下面所有用例的读数都不可信。
        """
        self.assertIsNot(
            vendor.httpx.Client, _REAL_HTTPX_CLIENT, "桩没接上：被测代码会去打真网"
        )
        result = vendor.verify(
            NAME_OTHER, ID_CARD_MALE, key_id=FAKE_KEY_ID, key_secret=FAKE_KEY_SECRET
        )
        self.assertEqual(self.stub.call_count, 1, "桩接上了但没被走到")
        self.assertIs(result.ok, True)
        self.assertEqual(result.vendor_result, "1")

    # ── 便捷动作 ─────────────────────────────────────────────────────────────
    def post_verify(self, *, name=NAME_OK, id_card_no=ID_CARD_MALE, client=None):
        return (client or self.client_user).post(
            "/api/auth/realname/verify/",
            {"name": name, "id_card_no": id_card_no},
            content_type="application/json",
        )

    def get_status(self, client=None):
        return (client or self.client_user).get("/api/auth/realname/")

    def body(self, response) -> dict:
        return response.json()

    def records(self):
        return RealNameCertRecord.objects.all()


class RealNameVerifyBillingTests(_VerifyApiCase):
    """用例 01 / 02 / 02b / 03 / 06 / 07：**分档计费**（spec §6-2b 那张表逐行钉）。"""

    def test_01_code_400_records_a_fail_and_charges_nothing(self):
        """`code:400`（⚠️ **HTTP 状态码仍是 200**）⇒ 流水 1 行 + `result=fail` + 两层计数**都不动**。

        判据（三条缺一不可）：
        ① `vendor_code == 400`（证明我们读的是**响应体里的码**，不是 HTTP 状态码 ——
           看状态码会把这一档读成"核验成功"）；
        ② 流水**有且只有 1 行**（spec §5 第 ⑦ 步：不扣次数 ≠ 不写记录）；
        ③ `grant.used` / `package.used_quota` **仍是起点值**（起点刻意取 `3` / `2`，
           ⛔ 不是 0 —— 起点为 0 时"没扣"是恒真的）。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=3)
        package = make_package(total_quota=10, used_quota=2)
        self.stub.json_reply(
            {"msg": "身份证号不正确", "requestid": "", "code": 400, "data": {}}
        )

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(res.status_code, 200)  # ⚠️ 失败信封挂在 HTTP 200 上
        self.assertEqual(body["code"], OK)
        self.assertEqual(body["data"]["result"], "fail")
        # ① + ②
        self.assertEqual(self.stub.call_count, 1, "这一档必须**真的出网**过（否则下面的'没扣'是假的）")
        rows = list(self.records())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].vendor_code, 400)
        self.assertEqual(rows[0].result, "fail")
        self.assertEqual(rows[0].vendor_result, "")
        # ③
        self.assertEqual(used_of(grant), 3, "code:400 这一档不许扣次数")
        self.assertEqual(used_quota_of(package), 2, "code:400 这一档不许扣平台包")

    def test_02_exhausted_quota_is_1026_and_changes_nothing(self):
        """本租户余量为 0（`granted=1, used=1`）⇒ **不放行也不扣**。

        ⚠️ 起点刻意造成"刚好用尽"（⛔ 不是 `granted=0`）：`granted=0` 会同时被
        「没买过」那条判据拦住，分辨不出是"边界写对了"还是"根本读不到行"。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=1, used=1)
        package = make_package(total_quota=10, used_quota=2)

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], QUOTA_EXHAUSTED)
        self.assertIn("余量不足", body["msg"])
        self.assertEqual(self.records().count(), 0, "没放行就不该留下流水")
        self.assertEqual(self.stub.call_count, 0, "余量不足时⛔ 不许出网（先查余量、后调厂商）")
        self.assertEqual(used_of(grant), 1)
        self.assertEqual(used_quota_of(package), 2)

    def test_02b_no_usable_package_is_1027_not_1026(self):
        """平台侧没有可用的包 ⇒ **1027**（⛔ 不是 1026）。

        为什么要单独钉：`1026` 让管理员去给**这个租户**加额度，`1027` 才是"平台要补包"
        ⇒ 混成一个码会让人去改一个**没错**的地方（`apps/core/error_codes.py` 那段注释的口径）。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=1, used_quota=1)  # 已耗尽 ⇒ 选不出可用包

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], NO_PACKAGE)
        self.assertIn("密钥包", body["msg"])
        self.assertEqual(self.records().count(), 0)
        self.assertEqual(used_of(grant), 0)

    def test_03_disabled_is_1025_and_never_calls_the_vendor(self):
        """未启用 ⇒ `1025`，且**一次厂商都不调**（桩计数 = 0）。

        ⚠️ 这条计数是**负向判据**：它证明"这个开关真的发生在出网之前"。
        """
        enable_setting(self.schema, enabled=False)  # 显式写死：默认值就是 False（对标 `CertSwicth`）
        grant = make_grant(self.tenant, granted=5, used=0)
        package = make_package(total_quota=10, used_quota=0)

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], DISABLED)
        self.assertIn("未启用", body["msg"])
        self.assertEqual(self.stub.call_count, 0)
        self.assertEqual(self.records().count(), 0)
        self.assertEqual(used_of(grant), 0)
        self.assertEqual(used_quota_of(package), 0)

    def test_06_usable_false_still_charges_once_and_records_fail(self):
        """`code:200` 但 `data` 里没有可判定的 `result` ⇒ **照扣 1 次** + `result=fail`。

        ⚠️ 这是评审 Blocking #3 点名的那一档：它**已查得、已被计费**，只是结果值形状变了。
        把它并进 `vendor_error`（= 不扣）会**静默少记一次我们花钱买的次数**。
        判据：起点 `used=1` ⇒ 终态**必须**是 2（⛔ 不是"不变"）。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=1)
        package = make_package(total_quota=10, used_quota=0)
        # `data` 里**没有** `result`（厂商形状变了），但 code 仍是 200 ⇒ ok=True / result_usable=False。
        self.stub.json_reply(
            {"msg": "查询成功", "requestid": FAKE_REQUEST_ID, "code": 200, "data": {}}
        )

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], OK)
        self.assertEqual(body["data"]["result"], "fail")
        rows = list(self.records())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].vendor_code, 200)
        self.assertEqual(rows[0].vendor_result, "", "拿不到可判定的结果码 ⇒ 归一化成空串")
        self.assertEqual(rows[0].result, "fail")
        self.assertEqual(used_of(grant), 2, "已查得、已被计费 ⇒ 必须照扣 1 次")
        self.assertEqual(used_quota_of(package), 1)

    def test_07_network_timeout_records_fail_code_0_and_charges_once(self):
        """网络超时 ⇒ 流水 `result=fail`、`vendor_code=0`；按**保守规则**照扣 1 次（spec §6-2b）。

        ⚠️ 证据等级如实登记：这一档"照扣"**没有实测依据**，是有意选的保守方向
        （"宁可多扣租户一次，不可少记我们一笔"）；判据即本用例的 `used` +1。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=1)
        package = make_package(total_quota=10, used_quota=0)
        self.stub.boom(httpx.TimeoutException("synthetic timeout"))

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], OK)
        self.assertEqual(body["data"]["result"], "fail")
        rows = list(self.records())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].vendor_code, 0, "连信封都没拿到 ⇒ 码归一化成 0")
        self.assertEqual(rows[0].vendor_result, "")
        self.assertIsNotNone(rows[0].certified_at)
        self.assertEqual(used_of(grant), 2)
        self.assertEqual(used_quota_of(package), 1)


class RealNameVerifyResultTests(_VerifyApiCase):
    """用例 04 / 05 / 08 + 输入与截断：结果映射、幂等短路、落库的副作用。"""

    def test_04_idempotent_short_circuit_charges_once_and_writes_one_row(self):
        """同号重复提交两次 ⇒ **只扣一次**、流水**只有一行**。

        ⭐ 判据刻意**不是** `code == 0`（那是恒真的：不短路、真调厂商也会回 `pass`）：
        起点由**第一次**提交造成（`used=1`、流水 1 行），第二次提交后**两者都不变**。
        ⚠️ 负向判据配一条正向对照：第一次提交必须**真的**扣了 1 次、真的写了 1 行
        （否则"第二次没变"可能由"两次都没生效"凑出来）。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=0)
        package = make_package(total_quota=10, used_quota=0)

        first = self.post_verify()
        self.assertEqual(self.body(first)["data"]["result"], "pass")
        # 正向对照（起点必须是真的被改变过）：
        self.assertEqual(used_of(grant), 1)
        self.assertEqual(used_quota_of(package), 1)
        self.assertEqual(self.records().count(), 1)
        self.assertEqual(self.stub.call_count, 1)
        first_record_pk = self.records().get().pk

        second = self.post_verify()

        self.assertEqual(self.body(second)["code"], OK)
        self.assertEqual(self.body(second)["data"]["result"], "pass")
        # ⭐ 判据：口径上"短路"= 这三样一个都不许动。
        self.assertEqual(used_of(grant), 1, "短路又扣了一次")
        self.assertEqual(used_quota_of(package), 1, "短路又扣了一次平台包")
        self.assertEqual(self.records().count(), 1, "短路又写了一条流水")
        self.assertEqual(self.stub.call_count, 1, "短路还调了厂商")
        self.assertEqual(self.records().get().pk, first_record_pk)

    def test_05_result_2_and_3_are_fail_and_do_not_write_staff_profile(self):
        """厂商回 `2`（不一致）/ `3`（库中无此号）⇒ `result=fail`，且**不落** `StaffProfile.id_card_no`。

        判据打在**档案列的值**上（⛔ 不是响应文案）：spec §14-P2 业务方口径
        「没核过就不该落库」。⚠️ 夹具的档案是 `make_logged_in_client` 建的、`id_card_no` 为空
        ⇒ 断言它**仍然是空串**（起点非空才能证明"没被写"吗？这里相反：这一列唯一的合法终态就是空
        ⇒ 只要它变成我们提交的那个号，就是被写了。用例 08 是它的正向对照。）
        """
        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)

        for vendor_result, id_card in (("2", ID_CARD_MISMATCH), ("3", ID_CARD_NOT_FOUND)):
            with self.subTest(vendor_result=vendor_result):
                self.stub.json_reply(
                    {
                        "msg": "查询成功",
                        "requestid": FAKE_REQUEST_ID,
                        "code": 200,
                        "data": {"result": vendor_result, "message": "结果描述"},
                    }
                )
                res = self.post_verify(id_card_no=id_card)
                self.assertEqual(self.body(res)["data"]["result"], "fail")
                row = self.records().order_by("-id").first()
                self.assertEqual(row.result, "fail")
                self.assertEqual(row.vendor_result, vendor_result)
                self.assertEqual(
                    StaffProfile.objects.get(user=self.user).id_card_no,
                    "",
                    "核验失败也把号写进了档案（spec §14-P2：⛔ 不许写）",
                )

    def test_08_pass_writes_staff_profile_id_card_no(self):
        """`result=1` ⇒ `result=pass`，且**把号落进** `StaffProfile.id_card_no`（用例 05 的正向对照）。

        顺带钉：本地解析出来的性别 / 生日 / 年龄**真的进了流水**（厂商这条接口不回它们，
        只能本地算 —— spec §4.2 / §5 第 ③ 步）。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=0)
        package = make_package(total_quota=10, used_quota=0)

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], OK)
        self.assertEqual(body["data"]["result"], "pass")
        # ⛔ 响应里只许有脱敏号（对标页也不完整展示）。
        self.assertEqual(body["data"]["id_card_masked"], "110***11")
        self.assertNotIn(ID_CARD_MALE, res.content.decode())
        self.assertEqual(used_of(grant), 1)
        self.assertEqual(used_quota_of(package), 1)

        row = self.records().get()
        self.assertEqual(row.result, "pass")
        self.assertEqual(row.gender, "男")
        self.assertEqual(row.birthday, "19900307")
        # 周岁按**今天**算（在测试里独立算一遍，⛔ 不拿被测函数当期望值来源）。
        today = timezone.localdate()
        expected_age = today.year - 1990 - ((today.month, today.day) < (3, 7))
        self.assertEqual(row.age, expected_age)
        self.assertEqual(row.cert_method, "basic", "我们只有基础认证（二要素）")
        self.assertEqual(row.cert_image, "", "二要素没有认证图片 ⇒ 恒空")
        self.assertIsNotNone(row.applied_at)
        self.assertIsNotNone(row.certified_at)
        self.assertLessEqual(row.applied_at, row.certified_at)
        self.assertEqual(row.user_name_snapshot, "realname-user-a", "提交那一刻的账号快照")
        self.assertEqual(row.package_pk, package.pk)
        self.assertEqual(row.grant_pk, grant.pk)
        self.assertEqual(row.package_remark, "合成包-T6", "对账要看得懂，不能只有一个裸 id")
        self.assertEqual(row.vendor_request_id, FAKE_REQUEST_ID)

        self.assertEqual(StaffProfile.objects.get(user=self.user).id_card_no, ID_CARD_MALE)

    def test_08b_numeric_result_1_is_pass_and_charges_once(self):
        """⭐⭐ **端到端**：厂商把 `data.result` 回成 **JSON 数字 `1`** ⇒ `result=pass` 且**扣 1 次**。

        ⭐ 这是生产原始回包的形状（生产机实测：`{"code":200,"data":{"result":2,...}}`
        —— `result` 是 **int**，不是字符串；交接单 P0 / 决策 D-0215 修复轮的现场）。
        ⚠️ 为什么必须有这一条：P0 修复前的判据网全是**字符串夹具**（`"1"`）⇒ 那次「真实核验
        永远判失败」在端到端层面**零判据**（交接单 R2）。vendor 层的 int 分支另有单测
        （`tests.test_realname_vendor`），本条钉的是**全链路**：int 夹具 ⇒ 记录 `pass` + 扣次。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=0)
        package = make_package(total_quota=10, used_quota=0)
        #: ⭐ 与 setUp 默认夹具的唯一差别 = `result` 是 **int**（不是 `"1"`）：
        self.stub.json_reply(
            {
                "msg": "成功",
                "requestid": FAKE_REQUEST_ID,
                "code": 200,
                "data": {"result": 1, "message": "一致"},
            }
        )

        res = self.post_verify()

        body = self.body(res)
        self.assertEqual(body["code"], OK)
        self.assertEqual(body["data"]["result"], "pass")
        self.assertEqual(used_of(grant), 1, "int 1 是一致 ⇒ 照常扣 1 次")
        self.assertEqual(used_quota_of(package), 1)
        row = self.records().get()
        self.assertEqual(row.result, "pass")
        self.assertEqual(row.vendor_result, "1", "归一化后以字符串口径落库（与字符串夹具同形）")
        self.assertEqual(
            StaffProfile.objects.get(user=self.user).id_card_no,
            ID_CARD_MALE,
            "int 1 ⇒ 照常落档案列（修复前这里永远判 fail、不落）",
        )

    def test_13a_blank_after_strip_is_1000_and_writes_nothing(self):
        """姓名字段**去掉空白后为空** ⇒ `1000`，且**不写流水、不出网**。

        ⚠️ 这一条是 `RealNameVerifySerializer.validate_name` 的**唯一判据**
        （`name="   "` + 一个**合法**号码：如果那段校验被删掉，请求会照常走完并 `pass`
        ⇒ 用例红）。没有它，那两段 `strip()+判空` 就是装饰性代码（`AGENTS.md` §5-E）。
        """
        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)

        res = self.post_verify(name="   ")

        self.assertEqual(res.status_code, 200)  # 校验失败在本仓也是 HTTP 200 + code=1000
        self.assertEqual(self.body(res)["code"], VALIDATION)
        self.assertEqual(self.records().count(), 0)
        self.assertEqual(self.stub.call_count, 0)

    def test_13b_surrounding_whitespace_is_stripped(self):
        """`name` / `id_card_no` 前后带空白 ⇒ **strip 之后**照常核验，且落库的值**不带空白**。

        ⚠️ 这一条同时钉住「长度校验发生在 strip **之后**」：`" 号 "` 比 18 位长，
        若长度校验用的是未 strip 的原串（DRF `max_length` 的默认位置），
        这次请求会以 `data=None`（校验失败）收场 —— 实测第一版就是这样红的。
        顺带钉：解析出来的性别来自**号码本身**（这份合成号是女性）。
        """
        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)

        res = self.post_verify(name=f"  {NAME_OK}  ", id_card_no=f" {ID_CARD_FEMALE} ")

        self.assertEqual(self.body(res)["data"]["result"], "pass")
        row = self.records().get()
        self.assertEqual(row.name, NAME_OK)
        self.assertEqual(row.id_card_no, ID_CARD_FEMALE)
        self.assertEqual(row.gender, "女")
        self.assertEqual(StaffProfile.objects.get(user=self.user).id_card_no, ID_CARD_FEMALE)

    def test_15_overlong_vendor_message_is_truncated_instead_of_500(self):
        """厂商 `message` 超长 ⇒ **裁到列宽**照常落库（⛔ 不是 `DataError` ⇒ 500）。

        ⚠️ 这条钉的是 `services._truncate` —— 厂商原文长度**没有任何承诺**，
        而 `vendor_message` 列只有 191 字符：不裁的话这次核验对用户就是"服务器错误"，
        我们白花一次次数（`docs/specs/…-design.md` §4.2）。
        """
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)
        long_message = "超长描述" + "描" * 400
        self.stub.json_reply(
            {
                "msg": "查询成功",
                "requestid": FAKE_REQUEST_ID,
                "code": 200,
                "data": {"result": "1", "message": long_message},
            }
        )

        res = self.post_verify()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.body(res)["code"], OK)
        row = self.records().get()
        self.assertEqual(len(row.vendor_message), 191)
        self.assertEqual(row.vendor_message, long_message[:191])
        self.assertEqual(used_of(grant), 1)


class RealNameStatusApiTests(_VerifyApiCase):
    """`GET /api/auth/realname/`：状态 + 余量，且**绝不含密钥**（用例 12）。"""

    def test_12_status_response_carries_no_key_material(self):
        """⭐ 响应里**不含任何密钥字段** —— 判据钉在**值**上（⛔ 不是钉字段名的存在与否）。

        ⭐ 做法：库里那一行的 `key_secret` / `key_id` 用**合成哨兵值**，然后断言
        **响应体原始字节里找不到它们**。这样"哪天有人顺手把包对象序列化进响应"会被当场抓住
        （⛔ 只断言"没有 `key_secret` 这个键名"则挡不住换名 / 嵌套 / 拼进 msg 的写法）。
        """
        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=1)

        res = self.get_status()

        raw = res.content.decode()
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(FAKE_KEY_SECRET, raw, "响应里出现了 key_secret 的值")
        self.assertNotIn(FAKE_KEY_ID, raw, "响应里出现了 key_id 的值")
        body = self.body(res)
        self.assertEqual(body["code"], OK)
        # 载荷**恰好**是这四键（多一个键就是新开了一个出口面）。
        self.assertEqual(
            sorted(body["data"]),
            ["enabled", "is_real_name", "remaining", "verified_at"],
        )

    def test_12b_status_reports_enabled_realname_and_remaining(self):
        """`enabled` / `is_real_name` / `verified_at` / `remaining` 四个值都要**从库里算对**。

        ⚠️ `remaining` 是**整数减法**（`granted - used`）：起点刻意取 `5 / 2` ⇒ 期望 **3**
        （⛔ 不用真值判断的实现会把 `used=0` 那种情形算对、把这一种算错）。
        """
        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=2)
        make_package(total_quota=10, used_quota=0)

        before = self.body(self.get_status())["data"]
        self.assertIs(before["enabled"], True)
        self.assertIs(before["is_real_name"], False)  # 还没核过
        self.assertIsNone(before["verified_at"])
        self.assertEqual(before["remaining"], 3)

        # 核验通过之后：三样都要跟着变（判据打在**库行**与**重读的响应**上）。
        self.post_verify()
        after = self.body(self.get_status())["data"]
        self.assertIs(after["is_real_name"], True)
        self.assertIsNotNone(after["verified_at"])
        self.assertEqual(after["remaining"], 2)

    def test_12d_verified_at_is_a_project_timezone_string(self):
        """D-0302（2026-10-24）：App 实名页「认证时间」是**项目时区**（+08:00）的 ISO 串。

        App 按串截显；旧写法裸 datetime 经信封渲染成 UTC 串、差 8 小时。夹具把流水行的
        `certified_at` 钉在 UTC 2026-10-24 04:30 ⇒ 响应是北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)
        self.post_verify()
        RealNameCertRecord.objects.update(
            certified_at=dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc),
        )
        data = self.body(self.get_status())["data"]
        self.assertEqual(data["verified_at"], "2026-10-24T12:30:00+08:00")

    def test_12c_remaining_is_zero_when_there_is_no_grant_row(self):
        """没有额度行 ⇒ `remaining` 是 **0**（⛔ **不是** `null`）。

        ⚠️ 这一条与下一条（读失败 ⇒ `null`）是一对：两种情形必须能被客户端区分开，
        否则"我没买过"与"我们读不到"会显示成同一句话（spec §6-7 ① 的口径）。
        """
        enable_setting(self.schema)
        # 刻意**不建** RealNameGrant 行。

        data = self.body(self.get_status())["data"]

        self.assertEqual(data["remaining"], 0)
        self.assertEqual(self.body(self.get_status())["msg"], "")

    def test_14_remaining_is_null_and_explained_when_the_read_fails(self):
        """跨 schema 读余量**失败** ⇒ `remaining: null` + `msg` 里说明（⛔ 不许静默当成 0）。

        ⚠️ 判据的判别力：这一档若被写成 `0`，用户会以为"我没次数了"去催客服购买，
        而其实是我们这边读不到 —— 所以本用例既钉 `None`，也钉"msg 非空"。
        """
        enable_setting(self.schema)
        make_grant(self.tenant, granted=5, used=1)

        with mock.patch(
            "apps.realname.views._read_grant_row",
            side_effect=ProgrammingError("synthetic: relation does not exist"),
        ):
            res = self.get_status()

        body = self.body(res)
        self.assertEqual(body["code"], OK, "读不到余量不该让整个状态端点变成失败")
        self.assertIsNone(body["data"]["remaining"], "读不到余量⛔ 不许静默当成 0")
        self.assertNotEqual(body["msg"], "", "降级必须在 msg 里说明（否则用户以为没次数了）")


# ══════════════════════════════════════════════════════════════════════════════
# 用例 09：平面守卫（public 平面 ⇒ 404）
# ══════════════════════════════════════════════════════════════════════════════
class RealNameOnThePublicPlaneTests(TenantTestCase):
    """`public` 平台平面没有本 app 的表 ⇒ **已认证回 404**（⛔ 不是 500），匿名回 401。

    ⚠️ 口径与骨架照 `tests/test_my_profile_api.py::MyProfileOnThePublicPlaneTests`：
    认证（401）排在视图体里的平面判据（404）**之前**，是本仓固定次序；
    `ensure_public_tenant` 放 `setUp`（测试事务内）而**不是** `setUpClass`
    ——后者会被提交并活过整个进程。
    """

    def setUp(self):
        super().setUp()
        ensure_public_tenant(PLATFORM_DOMAIN)
        with schema_context("public"):
            User.objects.create_superuser(
                username="realname-pub-super", email="", password="pw-realname-pub"
            )
        self.client = Client(SERVER_NAME=PLATFORM_DOMAIN)
        login = self.client.post(
            "/api/auth/login",
            {"username": "realname-pub-super", "password": "pw-realname-pub"},
            content_type="application/json",
        )
        self.assertEqual(login.status_code, 200, login.content[:200])

    def tearDown(self):
        connection.set_tenant(self.tenant)
        super().tearDown()

    def test_09_both_endpoints_are_404_when_authenticated(self):
        """已认证：两个端点都回 **404 + code 404**（⛔ 不是 500）。

        ⚠️ 漏了守卫时的症状：`public` 平面上 `realname_realnamecertrecord` 表**不存在**
        ⇒ `ProgrammingError` ⇒ 500（用户看到"服务器错误"，而这条路径压根不该存在）。
        ⚠️ msg 钉**字面量**：它与 `/api/auth/profile` / `/api/auth/password` 共用
        `apps/core/plane.py::plane_not_found_response` 这一个出处（漂移时三条一起红）。
        """
        res = self.client.get("/api/auth/realname/")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)
        self.assertEqual(res.json()["msg"], "当前平面没有此功能")

        res = self.client.post(
            "/api/auth/realname/verify/",
            {"name": NAME_OK, "id_card_no": ID_CARD_MALE},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["code"], 404)
        self.assertEqual(res.json()["msg"], "当前平面没有此功能")

    def test_09b_anonymous_is_401_not_404(self):
        """匿名：401（⛔ 不许把匿名请求伪装成"这个平面没有此功能"）。"""
        anon = Client(SERVER_NAME=PLATFORM_DOMAIN)
        self.assertEqual(anon.get("/api/auth/realname/").status_code, 401)
        self.assertEqual(
            anon.post(
                "/api/auth/realname/verify/",
                {"name": NAME_OK, "id_card_no": ID_CARD_MALE},
                content_type="application/json",
            ).status_code,
            401,
        )


# ══════════════════════════════════════════════════════════════════════════════
# 用例 10：设置的唯一性（并发首访 ⇒ 全表恰好一行）
# ══════════════════════════════════════════════════════════════════════════════
#: 并发用例的租户（与既有并发用例 `tests/test_realname_quota.py` 同口径）。
SETTING_SCHEMA = "test"
SETTING_DOMAIN = "tenant.test.com"


class _BarrierBeforeSettingInsert:
    """上下文管理器：在 `RealNameSetting` 的 **INSERT 之前**把两条线程对齐。

    ⚠️ **为什么必须打在这个位置**（⛔ 不是随便挑一个同步点）：`get_or_create` 是
    「先 `get`（查不到）→ 再 `create`」两步，那条缝就是竞态本身。只有在对齐到"两边都查空了、
      都准备 INSERT"时，才有可能逼出两行。对齐在调用**之前**的话，先到的那条可能已经提交完，
      后到的直接 `get` 到现成行 ⇒ 用例退化成"两次串行调用"，判别力归零。

    ⚠️ 本包装器不改变任何判据：它只加一个同步点，⛔ 不吞异常、⛔ 不改返回值。
    """

    def __init__(self, *, timeout: float):
        self.barrier = threading.Barrier(2, timeout=timeout)
        self._original = QuerySet.create
        self.arrived = 0
        self.timed_out = 0
        self._counter_lock = threading.Lock()

    def _wrapper(self, queryset, **kwargs):
        if queryset.model is RealNameSetting:
            with self._counter_lock:
                self.arrived += 1
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                # 等到超时（= 另一条没能同时到达）⇒ 计数在这里做，见 `aligned()`。
                with self._counter_lock:
                    self.timed_out += 1
        return self._original(queryset, **kwargs)

    def __enter__(self):
        # ⚠️ 塞的是**普通函数**（⛔ 不是绑定方法）：绑定方法进类字典后不会再被描述符重新绑定，
        #    实例调用时会少一个实参（T5 实测踩到过，见 `test_realname_quota.py` 的注释）。
        def _patched(queryset, **kwargs):
            return self._wrapper(queryset, **kwargs)

        self._patch = mock.patch.object(QuerySet, "create", _patched)
        self._patch.start()
        return self

    def __exit__(self, *exc_info):
        self._patch.stop()
        return False

    def aligned(self) -> bool:
        """两条线程是否**都**在 INSERT 之前到达了同步点、且都没等到超时。"""
        return self.arrived == 2 and self.timed_out == 0


class RealNameSettingUniquenessTests(TransactionTestCase):
    """用例 10：`RealNameSetting` 每租户恰好一行 —— **靠唯一约束**，⛔ 不靠 `get_or_create` 的运气。

    ⚠️ 用 `TransactionTestCase`（⛔ 不是 `TenantTestCase`）：并发用例要的是**真并发**
    （两条独立连接、无外层事务），而 `TenantTestCase` 把用例包在事务里。
    夹具形态照 `tests/test_realname_quota.py`（`django-tenants` 没有 `TenantTransactionTestCase`）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name=SETTING_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=SETTING_DOMAIN)
        cls.domain.save()
        if SETTING_DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [SETTING_DOMAIN]
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if SETTING_DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(SETTING_DOMAIN)
        super().tearDownClass()

    def setUp(self):
        # 前提自证：确实跑在**无外层事务**的形态上（否则并发态用例没有判别力）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        connection.set_tenant(self.tenant)
        RealNameSetting.objects.all().delete()

    def _run_two_first_visits(self) -> dict:
        """两条**独立连接**各自做一次「首访」（`services.get_or_create_setting`）。"""
        from apps.realname import services

        results: dict[str, tuple[str, object]] = {}

        def worker(key: str) -> None:
            try:
                close_old_connections()
                # 租户上下文只从连接态来（`AGENTS.md` §4：⛔ 不接受任何请求参数传租户）。
                connection.set_tenant(Tenant.objects.get(schema_name=SETTING_SCHEMA))
                setting = services.get_or_create_setting()
                results[key] = ("ok", setting.pk)
            except Exception as exc:  # noqa: BLE001 —— 结果要回主线程断言（⛔ 不许静默）
                results[key] = ("error", f"{type(exc).__name__}: {exc}")
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(key,)) for key in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=THREAD_JOIN_TIMEOUT)
        self.assertFalse(any(t.is_alive() for t in threads), "并发用例挂住了（死锁？）")
        return results

    def test_10_two_concurrent_first_visits_leave_exactly_one_row(self):
        """⭐ 并发首访 ⇒ **恰好一行**，且两条调用都拿到它（`IntegrityError` 不许漏给调用方）。

        ⚠️ 判别力从哪来：把模型上的 `unique=True` **删掉**（且同一次改迁移）之后，
        两条线程各建一行 ⇒ `count() == 1` 这条读数是 **2** ⇒ 本用例红。
        ⚠️ 同步点自证：两条都必须在 INSERT 之前到达（`aligned()`）——
        只有一条到达时，另一条是等到超时才往下走的，那说明这个夹具没有真的制造出竞态。
        """
        with _BarrierBeforeSettingInsert(timeout=BARRIER_TIMEOUT) as gate:
            results = self._run_two_first_visits()

        self.assertTrue(
            gate.aligned(),
            f"同步点没对齐（到达 {gate.arrived} 条、超时 {gate.timed_out} 条）——夹具没制造出竞态",
        )
        self.assertEqual(
            {key: value[0] for key, value in results.items()},
            {"alpha": "ok", "beta": "ok"},
            results,
        )
        self.assertEqual(
            RealNameSetting.objects.count(), 1, "并发首访留下了不止一行（唯一约束没生效？）"
        )
        self.assertEqual(
            {value[1] for value in results.values()},
            {RealNameSetting.objects.get().pk},
            "两条调用拿到的不是同一行",
        )

    def test_10b_repeated_first_visit_does_not_create_a_second_row(self):
        """重复首访（串行）同样只留一行 —— 两条调用拿到同一个 `pk`。"""
        from apps.realname import services

        first = services.get_or_create_setting()
        second = services.get_or_create_setting()

        self.assertEqual(RealNameSetting.objects.count(), 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(RealNameSetting.objects.get().tenant_schema, SETTING_SCHEMA)
        # 默认值口径（spec §4.2 的 `checked` 位置）：三开关全 False、年龄全 0。
        row = RealNameSetting.objects.get()
        self.assertIs(row.enabled, False)
        self.assertIs(row.block_claim_without_cert, False)
        self.assertIs(row.require_cert_for_settlement, False)
        self.assertEqual(row.min_age, 0)
        self.assertEqual(row.max_age, 0)


# ══════════════════════════════════════════════════════════════════════════════
# 用例 11：跨租户
# ══════════════════════════════════════════════════════════════════════════════
class RealNameCrossTenantTests(TenantTestCase):
    """用例 11：租户 A 的额度行**不被** B 用到（`AGENTS.md` §4 / §5-G）。

    ⚠️ **不能用主键指认"另一个租户的那一行"**：两个 schema 的序列各自从 1 开始 ⇒
    `filter(pk=…)` 在另一边命中的是**它自己**的行（断言恒真或恒假）。
    本用例的指认字段是 `RealNameGrant.tenant__schema_name` —— 它**租户内唯一**、
    且本例两个租户取值**不同**（`test` vs `realname_iso_b`）。

    ⚠️ 顺带如实登记一条实测：平台表（`RealNameGrant` / `RealNamePackage` / `Tenant`）
    **只在 `public` 里有一份**（`SHARED_APPS` 的模型不建到租户 schema 里，
    见 `TenantSyncRouter` 的 `allow_migrate`）⇒ 这两行额度**在同一个物理表里**，
    靠 `tenant` 列区分。所以"跨租户"这条判据的真实含义是：
    **核验路径必须按租户过滤**，⛔ 不是"两张表互相看不见"。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        # 第二个租户**必须**在 `setUpClass` 建（在一个用例内部建 schema 会与外层事务相冲：
        # schema DDL 不能在 `TestCase` 的事务里回滚）—— 与 `tests/test_account_isolation.py` 同手法。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="实名隔离租户 B", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A（跨域名请求**不会**在响应结束时还原 schema）。
        connection.set_tenant(self.tenant)
        self.schema = self.tenant.schema_name
        # ⚠️ D-0251 限流的桶在进程级 locmem 里跨用例存活 ⇒ 夹具卫生（同上）。
        cache.clear()
        self.addCleanup(cache.clear)
        self.stub = _VendorStub()
        patcher = mock.patch.object(vendor.httpx, "Client", self.stub.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stub.json_reply(
            {
                "msg": "查询成功",
                "requestid": FAKE_REQUEST_ID,
                "code": 200,
                "data": {"result": "1", "message": "一致"},
            }
        )
        # A 侧起点：启用 + 5 次额度；B 侧起点在用例里建（要的是"两边**取值不同**"）。
        enable_setting(self.schema)
        self.grant_a = make_grant(self.tenant, granted=5, used=0)
        self.package = make_package(total_quota=10, used_quota=0)

    def _b_call(self, function):
        """在 B 的 schema 上下文里执行 `function()`，结束后把连接**钉回租户 A**。"""
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def test_11_tenant_b_verification_does_not_touch_tenant_a_grant(self):
        """B 核验一次 ⇒ **B 的**额度行 +1、**A 的**纹丝不动；两边的流水各归各的 schema。"""

        def _b_setup():
            enable_setting(SCHEMA_B)
            make_grant(self.tenant_b, granted=2, used=0)
            return make_logged_in_client(domain=DOMAIN_B, username="realname-user-b")

        client_b = self._b_call(_b_setup)
        res = client_b.post(
            "/api/auth/realname/verify/",
            {"name": NAME_OK, "id_card_no": ID_CARD_OLD_MAN},
            content_type="application/json",
        )
        self.assertEqual(res.json()["code"], OK)
        self.assertEqual(res.json()["data"]["result"], "pass")

        # ① 两块额度行都在**同一个物理表**（public）里 ⇒ 按 schema 名指认，⛔ 不用主键。
        used_by_schema = {
            row.tenant.schema_name: row.used for row in RealNameGrant.objects.all()
        }
        self.assertEqual(used_by_schema.get(SCHEMA_B), 1, "B 自己那次没记上")
        self.assertEqual(used_by_schema.get(self.schema), 0, "B 的核验动了 A 的额度行")

        # ② 流水是**每个 schema 各一份**的表 ⇒ 必须在各自的上下文里读。
        self.assertEqual(
            self._b_call(lambda: RealNameCertRecord.objects.count()), 1, "B 的流水没落"
        )
        self.assertEqual(
            RealNameCertRecord.objects.count(), 0, "B 的核验在 A 的 schema 里落了流水"
        )
        # ③ 平台包的消耗是**全局共用**的（两层余量里的"批发层"）：B 用了一次就少一次。
        self.assertEqual(used_quota_of(self.package), 1)


# ══════════════════════════════════════════════════════════════════════════════
# E-0252 回归钉：管理员重置 → 同号重验 ⇒ 档案必须写回、接单闸放行
# ══════════════════════════════════════════════════════════════════════════════
class RealNameResetReverifyTests(_VerifyApiCase):
    """E-0252：重置实名后，同一号码重验命中**幂等短路**时必须补写档案。

    现场（闲时审查 H4）：`reset_realname` 只清档案、**保留** pass 流水
    （口径明文不动流水）⇒ 重验同一号码命中幂等短路，旧行为直接 return 旧行——
    档案永远空着，D-0245 接单闸把这个人**永久卡死**。
    修后：短路命中时补写 `StaffProfile.id_card_no`——幂等（档案已是同一个号就一行不写）、
    零出网（`stub.call_count` 不变）、零额度（`used` 不变）、零新流水。
    """

    RESET_NODE = "system_user.reset_realname"  # ⛔ 字面量（E-0024：节点改名时用例要看得见）

    def _reset_admin_client(self):
        from apps.business.roles import TENANT_ADMIN

        return make_logged_in_client(
            domain=TENANT_DOMAIN,
            username="realname-reset-admin",
            groups=(TENANT_ADMIN,),
            nodes=(self.RESET_NODE,),
            games=None,
        )

    def _turn_on_claim_gate(self):
        """打开「未实名不允许接单」并把本人挂成「代练员」（D-0245 的判据面）。"""
        from apps.business.roles import WORKER
        from apps.realname.services import get_or_create_setting

        setting_row = get_or_create_setting()
        setting_row.block_claim_without_cert = True
        setting_row.save(update_fields=["block_claim_without_cert", "updated_at"])
        group, _ = Group.objects.get_or_create(name=WORKER)
        self.user.groups.add(group)

    def test_reset_then_reverify_same_number_backfills_profile_and_reopens_the_gate(self):
        from apps.realname.services import claim_gate_blocks

        # ⚠️ 先 enable_setting（create 一行）再开闸（get_or_create **拿到同一行**）；
        #    反过来会撞 `tenant_schema` 的唯一约束（两行设置不存在）。
        enable_setting(self.schema)
        self._turn_on_claim_gate()
        grant = make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)

        # ① 首验通过（正向对照：这条路径**本来**就会把号写进档案——用例 08 已钉）。
        first = self.post_verify()
        self.assertEqual(self.body(first)["data"]["result"], "pass")
        self.assertEqual(StaffProfile.objects.get(user=self.user).id_card_no, ID_CARD_MALE)

        # ② 管理员重置 ⇒ 档案被清空（流水**保留**：reset 写的是一条新 fail 行）。
        res = self._reset_admin_client().post(
            f"/api/accounts/{self.user.pk}/reset-realname/"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.body(res)["code"], OK)
        self.assertEqual(
            StaffProfile.objects.get(user=self.user).id_card_no,
            "",
            "起点自证失败：重置没把档案清掉（E-0009：起点错了判据失真）",
        )
        self.assertEqual(self.records().count(), 2, "重置 = 原 pass 流水 + 一条新 fail 流水")
        # 「卡死」状态本身成立：闸在此刻**确实**拦着（否则下面"放行"的读数是假的）。
        self.assertTrue(claim_gate_blocks(user=self.user), "起点自证失败：重置后闸没拦")

        # ③ 同号重验 ⇒ 短路命中：零出网、零额度、零新流水，但档案**必须**写回、闸放行。
        second = self.post_verify()
        self.assertEqual(self.body(second)["code"], OK)
        self.assertEqual(self.body(second)["data"]["result"], "pass")
        self.assertEqual(self.stub.call_count, 1, "短路补写不许出网")
        self.assertEqual(used_of(grant), 1, "短路补写不许扣次数")
        self.assertEqual(self.records().count(), 2, "短路补写不许写新流水")
        self.assertEqual(
            StaffProfile.objects.get(user=self.user).id_card_no,
            ID_CARD_MALE,
            "短路命中后档案仍空 ⇒ E-0252 的洞还在（用户被接单闸卡死）",
        )
        self.assertFalse(claim_gate_blocks(user=self.user), "档案写回了，闸还拦着")


class RealNameShortCircuitMustNotOverwriteFilledProfileTests(_VerifyApiCase):
    """E-0260：幂等短路的档案补写**仅限档案为空**——管理员手填的号不得被静默盖掉。

    现场（夜间二审 R2-N3）：档案里已有管理员手填的号 Y，用户重提旧验过审的 X
    ⇒ 旧行为「档案号 ≠ 提交号就写回」把 Y 无痕迹盖成 X（留痕只记「提交过」）。
    修后：档案**为空**才补写（E-0252 的死锁修复照旧，上面那个类钉着）；档案已有号
    （哪怕与本次提交不同）一律不动。
    """

    def test_short_circuit_does_not_overwrite_admin_filled_profile(self):
        enable_setting(self.schema)
        grant = make_grant(self.tenant, granted=5, used=0)
        make_package(total_quota=10, used_quota=0)

        # ① 首验过审 ⇒ 档案 = 提交号（这条路径本来就会写，用例 08 已钉）。
        first = self.post_verify()
        self.assertEqual(self.body(first)["data"]["result"], "pass")

        # ② 管理员手填另一个号（合成号；直接落库 = 手填的形状：没有任何流水/留痕）。
        profile = StaffProfile.objects.get(user=self.user)
        profile.id_card_no = ID_CARD_FEMALE
        profile.save(update_fields=["id_card_no"])

        # ③ 用户重提旧验过审的号 ⇒ 短路命中（零出网、零额度、零新流水）。
        second = self.post_verify()
        self.assertEqual(self.body(second)["data"]["result"], "pass")
        self.assertEqual(self.stub.call_count, 1, "短路不许出网")
        self.assertEqual(used_of(grant), 1, "短路不许扣次数")
        self.assertEqual(self.records().count(), 1, "短路不许写新流水")

        # ④ ⭐ 判据：手填的号**原样保留**（修前红：被短路补写盖成 ID_CARD_MALE）。
        self.assertEqual(
            StaffProfile.objects.get(user=self.user).id_card_no,
            ID_CARD_FEMALE,
            "管理员手填的档案号被幂等短路的补写盖掉了（E-0260）",
        )
