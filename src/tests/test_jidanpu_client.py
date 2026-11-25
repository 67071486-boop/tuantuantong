"""`apps.business.jidanpu` 的**调用点**（打桩 `JidanpuClient`，零网络）。

⚠️ 桩打在**被查找的那个模块**上（`AGENTS.md` §5-E / E-0029）：`apps.business.jidanpu` 里
`from jidanpu import JidanpuClient` ⇒ patch `apps.business.jidanpu.JidanpuClient`。
⚠️ 本文件**不重写 SDK 自己的测试**（spec §10 第一节：登录表单字段 / `code==1` 严格判定 /
会话失效重登那些归上游）。这里只回答四件事：**传什么配置、调哪个端点、带什么参数、
以及我们没有自己再判一遍成败**。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：条数用 `42` / `7` 这类圆整数，
⛔ **不抄真实租户的实测计数**——计划里的示例值会被原样搬进 `src/**`。
"""
from __future__ import annotations

import types
from unittest import mock

from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError, ConfigError

from apps.business.jidanpu import fetch_users

BASE_URL = "https://jidanpu-a.test.invalid"
ACCOUNT = "bot-account-a"
PASSWORD = "pw-canary-a"
#: 合成总人数（⛔ 不是任何真实租户的实测值）。
COUNT = 42
#: 另一个合成总人数：`code=200` 的桩用它，好让「读的是哪个壳」也看得出来。
COUNT_WITH_LEGACY_SUCCESS_CODE = 7
ROWS = (
    {"loginAcc": "staff-1", "userName": "测试员工甲", "sta": "Y"},
    {"loginAcc": "staff-2", "userName": "测试员工乙", "sta": "N"},
)


def _binding(**overrides):
    """一份**鸭子类型的绑定行**替身（**零数据库**）。

    ⚠️ **2026-10-08 改准（只换夹具，断言一个字未改）**：本文件原来用
    `JidanpuBinding.objects.create(...)` 造那一行，而那张租户级鸡蛋铺线的绑定表已随该线
    整条拆除**删除**（`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md` §2.2 / §10.4）。
    ⚠️ 换得掉的理由是 `apps/business/jidanpu.py` 本来就是**鸭子类型**的：`credentials_of`
    只读 `base_url` / `account` / `password` 三个属性、⛔ 一行库都不查（那三个名字的取数
    口径与 strip 行为**一个字未改**）。
    """
    fields = {
        "base_url": BASE_URL,
        "account": ACCOUNT,
        "password": PASSWORD,
        "source_name": "",
        "account_uid": "",
        "account_name": "",
    }
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class _FakeClient:
    """`JidanpuClient` 的可控替身：只记「被怎么构造、被怎么调用」（零网络）。"""

    instances: list["_FakeClient"] = []

    def __init__(self, config, **kwargs):
        self.config = config
        self.kwargs = kwargs
        self.last_envelope = {"code": 0, "msg": "success", "data": list(ROWS), "count": COUNT}
        self.calls: list[tuple] = []
        _FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return list(ROWS)


class _LegacySuccessCode(_FakeClient):
    """壳里回 **`code=200`** 的**合法成功**（不是失败）。

    读端点的成功码实测有 `0` / `1` / `200` 三种（SDK `_endpoint_success_rule` 的
    docstring 逐字记着这三个），`code=200` 正是「自造的 `code != 0` 即失败」会误杀的形状。
    """

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.last_envelope = {
            "code": 200,
            "msg": "OK",
            "data": list(ROWS),
            "count": COUNT_WITH_LEGACY_SUCCESS_CODE,
        }


class _NoCount(_FakeClient):
    """壳里没有 `count` 键。"""

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.last_envelope = {"code": 0, "msg": "success", "data": list(ROWS)}


class _BadShape(_FakeClient):
    """`call()` 回一个**不是列表**的载荷（模拟未预期的上游形状变更）。"""

    def call(self, method, path, **kwargs):
        return {"not": "a list"}


class _Boom(_FakeClient):
    """`call()` 抛一个**具体实例**，供 `assertIs` 钉「原样上行、没有换一个新异常」。"""

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.boom = ApiError("业务失败：系统要求来源单号不能为空", code=0)

    def call(self, method, path, **kwargs):
        raise self.boom


class JidanpuFetchUsersTests(TenantTestCase):
    def setUp(self):
        _FakeClient.instances = []
        self.binding = _binding()

    def test_it_logs_in_with_this_rows_credentials_and_reads_the_user_list(self):
        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            page = fetch_users(self.binding)

        client = _FakeClient.instances[0]
        self.assertEqual(client.config.base_url, BASE_URL)
        self.assertEqual(client.config.account, ACCOUNT)
        self.assertEqual(client.config.password, PASSWORD)
        self.assertEqual(
            client.calls,
            [("GET", "/UserAjax/GetUserList", {"query": {"page": 1, "limit": 100}})],
        )
        self.assertEqual(page.count, COUNT)
        self.assertEqual(page.rows, ROWS)

    def test_it_does_not_hand_roll_a_second_verdict(self):
        """spec §5.7 / D15：**不传** `success_rule=`，也不自己再看一遍信封 `code`。"""
        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            fetch_users(self.binding)

        kwargs = _FakeClient.instances[0].calls[0][2]
        self.assertNotIn("success_rule", kwargs)

    def test_a_legitimate_read_success_code_is_passed_through(self):
        """**正面钉子**：判据在 SDK，我们只是把它放行。

        `code=200` 是读端点的**合法成功**（SDK 记的三个成功码 `0` / `1` / `200` 之一）
        ⇒ 任何自造的「`code != 0` 即失败」判据都会在这里红；用 `code=0` 的桩做不到
        ——它会把自造判据一起放行（这正是上一版 8 条用例的共同盲区）。
        """
        with mock.patch("apps.business.jidanpu.JidanpuClient", _LegacySuccessCode):
            page = fetch_users(self.binding)

        self.assertEqual(page.rows, ROWS)
        self.assertEqual(page.count, COUNT_WITH_LEGACY_SUCCESS_CODE)

    def test_the_password_never_appears_in_the_config_error_we_build(self):
        """密码不进**我们构造的文案**（spec §8-S1）。

        判据打在真实出口 `str(异常)` 上。此处绑定的 `password` 是**非空**的（只是
        `base_url` 空）⇒「文案里没有它」不是恒真：把密码拼进 `ConfigError` 的改动会红。
        """
        row = _binding(base_url="")

        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            with self.assertRaises(ConfigError) as ctx:
                fetch_users(row)

        self.assertNotIn(PASSWORD, str(ctx.exception))
        self.assertEqual(_FakeClient.instances, [])

    def test_the_password_never_appears_in_the_shape_error_we_build(self):
        """`data` 形状不对时我们抛的 `ApiError` 里也不许出现密码（spec §8-S1）。"""
        with mock.patch("apps.business.jidanpu.JidanpuClient", _BadShape):
            with self.assertRaises(ApiError) as ctx:
                fetch_users(self.binding)

        self.assertNotIn(PASSWORD, str(ctx.exception))

    def test_count_falls_back_to_the_row_count_when_the_envelope_has_none(self):
        with mock.patch("apps.business.jidanpu.JidanpuClient", _NoCount):
            page = fetch_users(self.binding)

        self.assertEqual(page.count, len(ROWS))

    def test_a_count_that_is_not_a_row_count_falls_back_to_the_row_count(self):
        """壳里的 `count` 不是「可用的条数」时回落到本页行数（低报，但绝不编一个数）。

        两个分支各自承重：
        - 非 `int`（`"42"`）⇒ `isinstance(count, int)` 这一半；
        - `True` ⇒ `not isinstance(count, bool)` 这一半（`bool` 是 `int` 的子类，
          但 `count: true` 不是条数）。
        """
        for bad_count in ("42", True):
            with self.subTest(bad_count=bad_count):

                class _BadCount(_FakeClient):
                    def __init__(self, config, **kwargs):
                        super().__init__(config, **kwargs)
                        self.last_envelope = {
                            "code": 0,
                            "msg": "success",
                            "data": list(ROWS),
                            "count": bad_count,
                        }

                with mock.patch("apps.business.jidanpu.JidanpuClient", _BadCount):
                    page = fetch_users(self.binding)

                self.assertEqual(page.count, len(ROWS))

    def test_sdk_failures_are_not_swallowed(self):
        """SDK 抛 `ApiError`（写/读的业务失败）⇒ **同一个实例**原样上行：不吞、不改判、不换新的。"""
        with mock.patch("apps.business.jidanpu.JidanpuClient", _Boom):
            with self.assertRaises(ApiError) as ctx:
                fetch_users(self.binding)

        self.assertIs(ctx.exception, _Boom.instances[0].boom)

    def test_a_non_list_payload_is_a_loud_failure(self):
        with mock.patch("apps.business.jidanpu.JidanpuClient", _BadShape):
            with self.assertRaises(ApiError) as ctx:
                fetch_users(self.binding)

        # `code=None`：这是**我们**报的形状错，不是上游给的业务码——别伪造一个上游码。
        self.assertIsNone(ctx.exception.code)

    def test_incomplete_credentials_never_reach_the_sdk(self):
        row = _binding(password="")
        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            with self.assertRaises(ConfigError):
                fetch_users(row)
        self.assertEqual(_FakeClient.instances, [])

    def test_a_none_binding_raises_the_same_config_error(self):
        """没有绑定行（`None`）与「字段不全」走**同一条**路（调用方一律按 `1015` 处理）。"""
        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            with self.assertRaises(ConfigError):
                fetch_users(None)
        self.assertEqual(_FakeClient.instances, [])

    def test_the_account_is_stripped_before_it_reaches_the_sdk(self):
        """**判别性**的一半：SDK 的 `JidanpuConfig.__post_init__` 只 strip `base_url`、
        **不** strip `account` ⇒ 去掉 `credentials_of` 的 account strip，这条就红。"""
        row = _binding(account="  " + ACCOUNT + "\n")

        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            fetch_users(row)

        self.assertEqual(_FakeClient.instances[0].config.account, ACCOUNT)

    def test_a_trailing_slash_on_the_base_url_is_tolerated(self):
        """**非判别性**的一半（如实标注）：SDK 自己也 `strip().rstrip("/")`，
        所以去掉我们这层 strip 这条照样绿。它记录的是「页面上粘进来的带尾斜杠地址
        能用」这个输入契约，**不是**「我们 strip 了 base_url」的证据。"""
        row = _binding(base_url=BASE_URL + "/")

        with mock.patch("apps.business.jidanpu.JidanpuClient", _FakeClient):
            fetch_users(row)

        self.assertEqual(_FakeClient.instances[0].config.base_url, BASE_URL)
