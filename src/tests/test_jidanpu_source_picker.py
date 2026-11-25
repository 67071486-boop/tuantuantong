"""绑定页「订单来源名称」改成**先连上再选**（D-0177；spec §13 / §15）。

本文件钉住那份契约**留下的那一个作用域**：

- 客服个人 `POST /api/business/member/jidanpu-binding/test-connection/`（`JidanpuMemberBinding`）。
  ⛔ **2026-10-08**：原还有一条**租户级** `POST /api/business/jidanpu-binding/test-connection/`
  （`JidanpuBinding`），它随租户级鸡蛋铺线整条拆除 ⇒ 租户级那一组用例一并退场
  （`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`）。

四件事逐条钉：

1. **全量来源字典**随「测试连接」回给页面（`source_options`：`label` = 要保存 / 要显示的名字，
   `name` = 上游原始名；⛔ 与既有的 `source_candidates` 是两件事——后者只是**匹配上的**那些行）；
2. **页面此刻填的密码**用于本次建连（未保存也能查），但⛔ **一个字都不落库**、⛔ 不回显；
3. **只读**：来源字典这一路⛔ 不写上游（只有 GET）、⛔ 不落库（用户配置列与健康列一个不动）；
4. 两种坏情况：**同名多条**两条都在出参里；**老绑定失效**时已存的名字仍在（页面据此标红）。

⚠️ 桩打在**被查找的那个模块**上（`AGENTS.md` §5-E / E-0029）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `member-a`、
密码 `pw-canary-*`、来源名一律「合成…」。⛔ 零真实网络。
⚠️ 期望文案是**测试里的字面量**，⛔ 不从被测模块的常量派生（自指断言不会红，E-0024）。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import AuthError

from apps.business import jidanpu
from apps.business.models import JidanpuMemberBinding
from tests.p5_auth_helpers import make_logged_in_client

MEMBER_TEST_URL = "/api/business/member/jidanpu-binding/test-connection/"

BASE_URL = "https://jidanpu-a.test.invalid"
MEMBER_ACCOUNT = "member-a"
MEMBER_PASSWORD_SAVED = "pw-canary-member-saved"
MEMBER_PASSWORD_TYPED = "pw-canary-member-typed"

#: 合成来源字典（形状照真机实测：`name` = 前缀字母 + `-` + 名字；`value` 与 label 同值）。
SYNTH_SOURCES = (
    {"name": "t-合成来源甲", "value": "合成来源甲"},
    {"name": "h-合成来源乙", "value": "合成来源乙"},
)

#: 已绑定但对方**已经没有**的那个名字（老绑定失效那一档）。
STALE_SOURCE = "合成已失效来源"

#: `AuthError` 那句人话（字面量，⛔ 不从 `jidanpu_views` 派生）。
AUTH_MESSAGE_HINT = "账号或密码"



class _MemberStub:
    """客服个人那条线的替身：`ensure_login()` + 来源字典那一跳 + 身份那一跳 + 探页面。

    ⚠️ `login_error` 一旦给了，**每一次**调用都抛它（真实形态：会话没建立起来时
    `GetUserInfo` 也一起失败）——只让 `ensure_login()` 抛会在身份那一跳上留一个假缝。
    """

    def __init__(self, *, sources=SYNTH_SOURCES, login_error=None):
        self._sources = tuple(sources)
        self._login_error = login_error
        self.last_envelope: dict = {}
        self.calls: list[tuple[str, str]] = []

    def ensure_login(self):
        if self._login_error is not None:
            raise self._login_error

    def call(self, method, path, **kwargs):
        if self._login_error is not None:
            raise self._login_error
        self.calls.append((method, path))
        if path == jidanpu.SOURE_LIST_PATH:
            self.last_envelope = {"code": 0, "msg": "success"}
            return [dict(row) for row in self._sources]
        if path == jidanpu.LOGIN_USER_INFO_PATH:
            self.last_envelope = {"code": 1, "msg": "success"}
            return {"Id": "100001", "Account": MEMBER_ACCOUNT, "IsSuper": True}
        if path == jidanpu.ASSIGNABLE_USERS_PATH:
            # 身份那一跳会顺着它找显示名（`resolve_login_identity`）——照真机的形状给一行。
            self.last_envelope = {"code": 0, "msg": "success"}
            return [{"id": "100001", "userName": "合成员工甲", "isValid": "Y"}]
        raise AssertionError(f"未预期的端点：{method} {path}")

    def get(self, path, **kwargs):
        if self._login_error is not None:
            raise self._login_error
        # 探页面一律「有权限」（本文件不验权限那一档，另有既有文件钉）。
        return httpx.Response(200, text="<html>合成页面</html>")


@contextmanager
def _ctx_pair(obj):
    """本线的建连接缝产出的是 `(sdk, http)` **一对**（注入态下同一个对象兼任两者）。"""
    yield obj, obj


class SourcePickerBackendTests(TenantTestCase):
    """两个作用域共用的契约。"""

    def setUp(self):
        self.client = make_logged_in_client(username="source-picker")

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _post_member(self, stub, **body):
        captured: list[JidanpuMemberBinding] = []

        def _open(binding):
            captured.append(binding)
            return _ctx_pair(stub)

        with mock.patch("apps.business.jidanpu.member_session", side_effect=_open):
            response = self.client.post(
                MEMBER_TEST_URL, json.dumps(body), content_type="application/json"
            )
        return response, captured

    def test_only_the_post_verb_is_exposed(self):
        """这条端点上⛔ 没有别的动词（少一层「将来谁加了写方法」）。

        ⚠️ **2026-10-08 改准**：本用例原先打在**租户级**那条 `jidanpu-binding/test-connection/`
        上；那条端点已随租户级鸡蛋铺线拆除 ⇒ 改打在**留下**的个人绑定端点上
        （判据与形状一个字没改：只映射 POST）。
        """
        for method in ("get", "put", "patch", "delete"):
            response = getattr(self.client, method)(MEMBER_TEST_URL)
            self.assertIn(response.status_code, (404, 405), (method, response.status_code))


    # ── 4. 客服个人那一份：同一份契约 ──────────────────────────────────────

    def _make_member_binding(self, **overrides):
        user = User.objects.get(username="source-picker")
        payload = {
            "user": user,
            "jidanpu_base_url": BASE_URL,
            "account": MEMBER_ACCOUNT,
            "password": MEMBER_PASSWORD_SAVED,
            "source_name": "合成来源甲",
        }
        payload.update(overrides)
        return JidanpuMemberBinding.objects.create(**payload)

    def test_the_member_endpoint_returns_the_same_dictionary_shape(self):
        """客服个人那条线的出参**同形**（同一个前端渲染器、同一套判据）。"""
        self._make_member_binding()
        response, _captured = self._post_member(_MemberStub(), source_name="合成来源甲")
        data = response.json()["data"]
        self.assertIs(data["source_options_ok"], True)
        self.assertEqual(
            data["source_options"],
            [
                {"name": "t-合成来源甲", "label": "合成来源甲"},
                {"name": "h-合成来源乙", "label": "合成来源乙"},
            ],
        )
        self.assertIs(data["source_ok"], True)
        self.assertEqual(data["messages"], [])

    def test_the_member_login_failure_does_not_claim_the_source_is_gone(self):
        """⭐⭐ 登录就没成 ⇒ `source_options_ok=False` + 空字典。

        ⚠️ 这一条是**防说假话**的：字典没读到与「对方真的没有这个来源」是两件事
        （`AGENTS.md` §5-E：空结果不等于没有数据）。页面据 `source_options_ok` 决定
        要不要打那个红标记——少了它，一次登录失败会让页面喊「对方已无此来源」。
        """
        self._make_member_binding(source_name=STALE_SOURCE)
        stub = _MemberStub(
            login_error=AuthError("登录失败", endpoint="POST /Login/Login")
        )
        response, _captured = self._post_member(stub)
        data = response.json()["data"]
        self.assertIs(data["ok"], False)
        self.assertIs(data["source_options_ok"], False)
        self.assertEqual(data["source_options"], [])
        self.assertEqual(data["messages"], ["登录失败：账号或密码错误"])

    def test_the_member_typed_password_is_used_and_never_saved(self):
        self._make_member_binding()
        response, captured = self._post_member(_MemberStub(), password=MEMBER_PASSWORD_TYPED)
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual([b.password for b in captured], [MEMBER_PASSWORD_TYPED])
        self.assertEqual(
            JidanpuMemberBinding.objects.get().password, MEMBER_PASSWORD_SAVED,
            "查询过程不得落库",
        )

    def test_the_member_wrong_password_is_not_echoed(self):
        self._make_member_binding()
        stub = _MemberStub(login_error=AuthError("登录失败", endpoint="POST /Login/Login"))
        response, _captured = self._post_member(stub, password=MEMBER_PASSWORD_TYPED)
        raw = response.content.decode()
        self.assertNotIn(MEMBER_PASSWORD_TYPED, raw)
        self.assertNotIn(MEMBER_PASSWORD_SAVED, raw)

    def test_the_member_source_pull_is_read_only(self):
        """客服个人那条线也只发 GET（⛔ 一个写方法都不许有）。"""
        self._make_member_binding()
        before = list(
            JidanpuMemberBinding.objects.values(
                "jidanpu_base_url", "account", "password", "source_name", "enabled"
            )
        )
        stub = _MemberStub()
        response, _captured = self._post_member(stub, source_name="合成来源乙")
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(sorted({method for method, _path in stub.calls}), ["GET"])
        after = list(
            JidanpuMemberBinding.objects.values(
                "jidanpu_base_url", "account", "password", "source_name", "enabled"
            )
        )
        self.assertEqual(after, before)


class SourceLabelTests(TenantTestCase):
    """`source_label` 是「要保存的那个名字」的**唯一出处**（前后端都靠它）。"""

    def test_the_label_is_the_part_after_the_first_dash(self):
        self.assertEqual(jidanpu.source_label({"name": "t-合成来源甲"}), "合成来源甲")

    def test_no_dash_means_the_whole_name_is_the_label(self):
        """将来可能的手工数据（没有前缀）按整串比——照 `match_sources` 的既有口径。"""
        self.assertEqual(jidanpu.source_label({"name": "合成来源甲"}), "合成来源甲")

    def test_a_name_with_several_dashes_keeps_the_rest_verbatim(self):
        self.assertEqual(jidanpu.source_label({"name": "t-合成-甲"}), "合成-甲")

    def test_matching_uses_that_same_label(self):
        """对照：`match_sources` 用的就是它（两处口径不允许各写一份）。"""
        rows = [{"name": "t-合成来源甲", "value": "x"}]
        self.assertEqual(len(jidanpu.match_sources(rows, "合成来源甲")), 1)

    def test_the_exported_helper_is_the_one_the_view_serializes(self):
        """页面拿到的 `label` 与 `match_sources` 认为是同一个名字（⛔ 不许各切一份）。"""
        rows = ({"name": "t-合成来源甲", "value": "合成来源甲"},)
        self.assertEqual(
            jidanpu.source_label(rows[0]),
            jidanpu.source_label(jidanpu.match_sources(rows, "合成来源甲")[0]),
        )
