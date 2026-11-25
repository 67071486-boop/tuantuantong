"""QQ 机器人设置页 REST 端点判据（批次B）。

七组必钉（任务书）+ 配套行为钉：

1. **权限门**：无 `system.qqbot_settings` 节点 → 每个端点家族至少一条 403；
   有节点 → 200（信封 `code` 是否为 0 由各组行为用例管，这里只管「进得来」）。
2. **secret 只写不读**：GET 响应体**完整字符串**不含 secret 值；PATCH 带 secret 后只见
   `app_secret_set: true`；PATCH 不带 / 带空串都不清空旧值。
3. **跨租户否定断言**（`AGENTS.md` §5-G：判据用租户内唯一、两租户取值不同的字段
   `group_openid`）：B 的用户打 A 的群主键 → 业务拒绝信封且 A 的行分毫未动；
   B 的列表 / 发件箱只见自己的行。配**正向对照**（B 改自己的行成功）——没有它，
   「拒绝」可能由「端点对谁都不工作」凑出来（`test_account_isolation.py` 同一条纪律）。
4. **验真路径**：PATCH 配齐凭据 → mock `TokenManager`：成功 → `last_error` 清空、
   `verified=true`；失败 → 配置仍保存、`last_error` 有码、`verified=false`。
   单字段改动（只改 enabled）**不触发**验真（mock 断言未被调用）。
5. **菜单服务端校验**：11 个一级 / link 非 https / 二级非法类型 → 业务校验错误
   且**不调 QQ**（mock 断言未被调用）；合法载荷 → `put_menu` 被调、版本回写。
6. **未配置代理降级**：binding 无凭据时 menu / mutes / join-requests / share-link
   → 业务码 1030 信封，绝不 500。
7. **test-push → 行落库**（kind=test_push、status=pending）——断言打在**数据库行**上
   （`AGENTS.md` §5-G：断言副作用打在真实出口上）。

⚠️ mock 落点：视图统一经 `apps.qqbot.qqbot_api` 模块对象取 `TokenManager` /
`put_menu` / `approve_join_request` 等 ⇒ `mock.patch` 打在**被查找的那个模块**
`apps.qqbot.qqbot_api` 的属性上（E-0029）。
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.platform.provisioning import provision_tenant, seed_tenant
from apps.qqbot.models import (
    QQBotBinding,
    QQBotGroup,
    QQBotGroupPushConfig,
    QQBotJoinRequestLog,
    QQBotOutboundMessage,
)
from tests.p5_auth_helpers import make_logged_in_client

#: 本批的自有节点（权限门用例与夹具都点名它，不从 catalog import——E-0024 同族：
#: 门写错节点时这条不跟着变绿）。
NODE = "system.qqbot_settings"

DOMAIN_B = "qqbot-iso-b.test.local"
SCHEMA_B = "qqbot_iso_b"

#: 两租户的群 openid——**取值必须不同**（§5-G：跨租户判据的锚点）。
OPENID_A = "AAAA-cross-tenant-group"
OPENID_B = "BBBB-cross-tenant-group"


def _node_client(**kw):
    """持 `system.qqbot_settings` 节点的登录 client（默认给全部游戏，D-0136）。

    ⚠️ 必须**带一个组**：`_grant_nodes` 只把节点挂到「用户所在组」对应的 `Role` 上，
    不带组的账号循环体一次都不跑、静默拿到 0 个节点（症状是 with-node 用例全 403）。
    组名 ≤5 字（`Role.name` 列宽 5），取一个本文件私有的名字、不蹭内置职位。
    """
    kw.setdefault("groups", ("机管",))
    return make_logged_in_client(nodes=(NODE,), **kw)


def _plain_client(**kw):
    """**不持任何节点**的登录 client（fail-closed ⇒ 打本页端点必 403）。"""
    return make_logged_in_client(username=kw.pop("username", "qqbot-plain-user"), **kw)


def _group(openid, **kw):
    return QQBotGroup.objects.create(group_openid=openid, **kw)


def _binding_configured():
    """配好凭据的单例（不启用；启用与否与端点判据无关）。"""
    row = QQBotBinding.get_solo()
    row.app_id = "APP-1"
    row.app_secret = "SECRET-VALUE-1"
    row.save()
    return row


class PermissionGateTests(TenantTestCase):
    """无节点 → 403；有节点 → 200。每个端点家族至少各一条。"""

    def setUp(self):
        self.group = _group("gate-group-openid")
        #: (方法, URL, body)——每条路由一条；body 给**合法**形状（有节点的对照腿要 200）。
        self.cases = [
            ("get", "/api/qqbot/binding/", None),
            ("patch", "/api/qqbot/binding/", {"enabled": False}),
            ("get", "/api/qqbot/groups/", None),
            ("patch", f"/api/qqbot/groups/{self.group.pk}/", {"remark": "改备注"}),
            (
                "put",
                f"/api/qqbot/groups/{self.group.pk}/push-config/",
                {"is_active": True, "msg_type": "text", "game_ids": []},
            ),
            ("post", f"/api/qqbot/groups/{self.group.pk}/test-push/", {}),
            ("get", f"/api/qqbot/groups/{self.group.pk}/mutes/", None),
            ("get", f"/api/qqbot/groups/{self.group.pk}/join-requests/", None),
            (
                "post",
                f"/api/qqbot/groups/{self.group.pk}/join-requests/approve/",
                {"op": "approve", "join_request_id": "jr-1", "member_openid": "mem-1"},
            ),
            (
                "patch",
                f"/api/qqbot/groups/{self.group.pk}/join-policy/",
                {"mode": "off"},
            ),
            ("get", f"/api/qqbot/menu/?group={self.group.pk}", None),
            (
                "put",
                "/api/qqbot/menu/",
                {"group": self.group.pk, "menu": {"buttons": [{"name": "菜单"}]}},
            ),
            ("post", "/api/qqbot/share-link/", {"group": self.group.pk}),
            ("get", "/api/qqbot/messages/", None),
        ]

    def _call(self, client, method, url, body):
        if method == "get":
            return client.get(url)
        return getattr(client, method)(url, body, content_type="application/json")

    def test_without_the_node_every_family_is_rejected_with_403(self):
        client = _plain_client()
        for method, url, body in self.cases:
            with self.subTest(method=method, url=url):
                response = self._call(client, method, url, body)
                self.assertEqual(response.status_code, 403, response.content[:200])
                self.assertEqual(response.json()["code"], 403)

    def test_with_the_node_every_family_passes_the_gate(self):
        client = _node_client()
        with mock.patch("apps.qqbot.qqbot_api.TokenManager") as manager:
            manager.return_value.get_token.return_value = "tok"
            for method, url, body in self.cases:
                with self.subTest(method=method, url=url):
                    response = self._call(client, method, url, body)
                    self.assertEqual(response.status_code, 200, response.content[:200])

    def test_anonymous_request_is_401_not_403(self):
        """匿名（没有任何认证）→ 401；与 openapi 的 `ApiAppAdminView` 同一口径。"""
        from django.test import Client

        response = Client(SERVER_NAME="tenant.test.com").get("/api/qqbot/binding/")
        self.assertEqual(response.status_code, 401, response.content[:200])


class SecretWriteOnlyTests(TenantTestCase):
    """secret 只写不读：响应体连键都不出现；留空 / 不带 = 不改。"""

    def setUp(self):
        self.client = _node_client(username="secret-user")
        with mock.patch("apps.qqbot.qqbot_api.TokenManager") as manager:
            manager.return_value.get_token.return_value = "tok"
            response = self.client.patch(
                "/api/qqbot/binding/",
                {"app_id": "APP-1", "app_secret": "SECRET-VALUE-1"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertTrue(response.json()["data"]["app_secret_set"])

    def test_get_response_never_contains_the_secret_value(self):
        response = self.client.get("/api/qqbot/binding/")
        body = response.content.decode()
        # **完整字符串**断言（钉值，不钉形状）：secret 的值一个片段都不许出现。
        self.assertNotIn("SECRET-VALUE-1", body)
        data = response.json()["data"]
        self.assertNotIn("app_secret", data)
        self.assertTrue(data["app_secret_set"])

    def test_patch_without_secret_keeps_the_old_value(self):
        response = self.client.patch(
            "/api/qqbot/binding/", {"enabled": True}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertTrue(response.json()["data"]["app_secret_set"])
        # 判据打在**数据库行**上（E-0009）：旧 secret 原样还在。
        self.assertEqual(QQBotBinding.get_solo().app_secret, "SECRET-VALUE-1")

    def test_patch_with_blank_secret_keeps_the_old_value(self):
        """「清空输入框再保存」也解释成不动它（_BLANK_MEANS_KEEP 的语义）。"""
        response = self.client.patch(
            "/api/qqbot/binding/", {"app_secret": ""}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(QQBotBinding.get_solo().app_secret, "SECRET-VALUE-1")


class BindingPatchAuditLogTests(TenantTestCase):
    """⭐ 评审 P2-4：PATCH binding 记**动作**留痕（照 rotate-secret「只记动作不记值」）。

    批次B 时这条端点在 `DELIBERATELY_NOT_LOGGED`（当时的理由「secret 只写不读 ⇒ 日志
    写不出变更值」）被评审推翻：secret 恰恰是**低频、不可回读、出事要追**的那一格——
    「谁在什么时候换过」必须能回答；值的纪律不变（⛔ 一个片段都不进日志）。
    """

    def setUp(self):
        self.client = _node_client(username="binding-audit-user")
        self.operator = get_user_model().objects.get(username="binding-audit-user")

    def _patch(self, payload):
        with mock.patch("apps.qqbot.qqbot_api.TokenManager") as manager:
            manager.return_value.get_token.return_value = "tok"
            return self.client.patch(
                "/api/qqbot/binding/", payload, content_type="application/json"
            )

    @staticmethod
    def _rows():
        return list(OperationLog.objects.filter(action="system.qqbot_binding_update"))

    def test_patch_with_changes_writes_exactly_one_action_row_without_the_secret(self):
        response = self._patch(
            {"app_id": "APP-1", "app_secret": "SECRET-VALUE-1", "enabled": True}
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        rows = self._rows()
        self.assertEqual(len(rows), 1, "一次有实际变更的 PATCH 恰好一条动作留痕")
        row = rows[0]
        self.assertEqual(row.actor_id, self.operator.pk, "操作人 = 发请求的登录用户")
        self.assertEqual(row.target_kind, "qqbot_binding")
        self.assertIn("app_secret", row.field_name, "字段名进 field_name（只记名字）")
        # ⛔ secret 的值一个片段都不进留痕：整行所有文本列逐一查（`_clip` 之外的
        #   自由文本出口就这几列，钉满才叫「不含」）。
        for text in (
            row.summary, row.field_name, row.from_value,
            row.to_value, row.reason, row.target_name, row.actor,
        ):
            self.assertNotIn("SECRET-VALUE-1", text)

    def test_noop_patch_writes_no_log_row(self):
        """无实际变更（原样的值 / secret 留空）⇒ 不写留痕（R26 全仓口径）。"""
        self._patch({"app_id": "APP-1", "app_secret": "SECRET-VALUE-1", "enabled": True})
        self.assertEqual(len(self._rows()), 1)
        response = self._patch({"app_id": "APP-1", "enabled": True, "app_secret": ""})
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(len(self._rows()), 1, "纯 no-op 不追加留痕行")
        # 「留空 = 不改」的旧行为不被留痕改造破坏（E-0027）。
        self.assertEqual(QQBotBinding.get_solo().app_secret, "SECRET-VALUE-1")


class VerifyPathTests(TenantTestCase):
    """PATCH binding 的验真：成功清错、失败照存配置只记 last_error；单字段不触发。"""

    def setUp(self):
        self.client = _node_client(username="verify-user")

    def test_successful_token_fetch_clears_last_error_and_reports_verified(self):
        with mock.patch("apps.qqbot.qqbot_api.TokenManager") as manager:
            manager.return_value.get_token.return_value = "tok-1"
            response = self.client.patch(
                "/api/qqbot/binding/",
                {"app_id": "APP-1", "app_secret": "SECRET-VALUE-1"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertTrue(data["verified"])
        # TokenManager 用的是**请求之后**的凭据对（不是库里的旧值）。
        manager.assert_called_once_with("APP-1", "SECRET-VALUE-1")
        row = QQBotBinding.get_solo()
        self.assertEqual(row.last_error, "")
        # `connection_state` 不动：保存凭据 ≠ 连接已建好（ws 进程才是它的写入方）。
        self.assertEqual(row.connection_state, "")
        self.assertEqual(row.app_id, "APP-1")
        self.assertEqual(row.app_secret, "SECRET-VALUE-1")

    def test_failed_verification_still_saves_config_and_records_last_error(self):
        from apps.qqbot.qqbot_api import TokenError

        with mock.patch("apps.qqbot.qqbot_api.TokenManager") as manager:
            manager.return_value.get_token.side_effect = TokenError(
                "换取 access_token 失败：HTTP400 Invalid appId"
            )
            response = self.client.patch(
                "/api/qqbot/binding/",
                {"app_id": "APP-BAD", "app_secret": "SECRET-BAD-VALUE"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertFalse(data["verified"])
        # 配置**仍保存**（用户可能离线填好再连），last_error 带得出来路的错误文案。
        row = QQBotBinding.get_solo()
        self.assertEqual(row.app_id, "APP-BAD")
        self.assertEqual(row.app_secret, "SECRET-BAD-VALUE")
        self.assertIn("HTTP400", row.last_error)
        self.assertIn("TOKEN", row.last_error)

    def test_single_field_change_does_not_trigger_verification(self):
        """只改 enabled ⇒ 一次网络往返都不该有（mock 断言未被构造）。"""
        with mock.patch("apps.qqbot.qqbot_api.TokenManager") as manager:
            response = self.client.patch(
                "/api/qqbot/binding/", {"enabled": True}, content_type="application/json"
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        manager.assert_not_called()
        self.assertNotIn("verified", response.json()["data"])


class MenuValidationTests(TenantTestCase):
    """群菜单的服务端校验：不合法的载荷在**调 QQ 之前**就被业务校验错误挡下。"""

    def setUp(self):
        self.client = _node_client(username="menu-user")
        _binding_configured()
        self.group = _group("menu-group-openid")

    @staticmethod
    def _buttons(n):
        return [{"name": f"菜单{i}"} for i in range(n)]

    def _put(self, menu):
        return self.client.put(
            "/api/qqbot/menu/",
            {"group": self.group.pk, "menu": menu},
            content_type="application/json",
        )

    def test_more_than_ten_top_level_buttons_is_rejected_without_calling_qq(self):
        with mock.patch("apps.qqbot.qqbot_api.put_menu") as put_menu:
            response = self._put({"buttons": self._buttons(11)})
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000)
        put_menu.assert_not_called()

    def test_link_button_with_non_https_url_is_rejected(self):
        menu = {"buttons": [{"name": "官网", "type": "link", "link": "http://evil.example"}]}
        with mock.patch("apps.qqbot.qqbot_api.put_menu") as put_menu:
            response = self._put(menu)
        self.assertEqual(response.json()["code"], 1000)
        put_menu.assert_not_called()

    def test_sub_button_with_unknown_type_is_rejected(self):
        menu = {
            "buttons": [
                {"name": "服务", "sub_button": [{"name": "子项", "type": "miniprogram"}]},
            ],
        }
        with mock.patch("apps.qqbot.qqbot_api.put_menu") as put_menu:
            response = self._put(menu)
        self.assertEqual(response.json()["code"], 1000)
        put_menu.assert_not_called()

    def test_valid_payload_reaches_put_menu_and_records_version(self):
        menu = {
            "buttons": [
                {
                    "name": "服务",
                    "sub_button": [{"name": "联系客服", "type": "send_message"}],
                },
                {"name": "官网", "type": "link", "link": "https://example.com"},
            ],
        }
        with mock.patch(
            "apps.qqbot.qqbot_api.put_menu",
            return_value=(True, {"version": 3}, "0", ""),
        ) as put_menu:
            response = self._put(menu)
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], 3)
        put_menu.assert_called_once()
        # 第三个位置参数（`menu`）是**原样**透传的载荷（我们不改写官方信封）。
        self.assertEqual(put_menu.call_args.args[2], menu)
        self.assertEqual(QQBotBinding.get_solo().menu_version, 3)


class UnconfiguredProxyDegradationTests(TenantTestCase):
    """binding 无凭据时代理端点统一回 1030 业务信封，绝不 500。"""

    def setUp(self):
        self.client = _node_client(username="degrade-user")
        # get_solo() 建出来的就是空凭据行——什么都不填。
        QQBotBinding.get_solo()
        self.group = _group("degrade-group-openid")

    def test_menu_get_degrades_to_the_business_code(self):
        response = self.client.get(f"/api/qqbot/menu/?group={self.group.pk}")
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1030)
        self.assertIn("机器人未配置", response.json()["msg"])

    def test_mutes_get_degrades_with_remote_error_field(self):
        response = self.client.get(f"/api/qqbot/groups/{self.group.pk}/mutes/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["remote_error"]["code"], "1030")

    def test_join_requests_local_rows_still_come_out(self):
        """本地留痕照出、远端降级——失败的信息只进 `remote_error`。"""
        operator = get_user_model().objects.get(username="degrade-user")
        QQBotJoinRequestLog.objects.create(
            group=self.group,
            join_request_id="jr-local-1",
            member_openid="member-1",
            decision=QQBotJoinRequestLog.Decision.MANUAL_PENDING,
        )
        QQBotJoinRequestLog.objects.create(
            group=self.group,
            join_request_id="jr-local-2",
            member_openid="member-2",
            decision=QQBotJoinRequestLog.Decision.MANUAL_APPROVED,
            acted_at=timezone.now(),
            acted_by=operator,  # ⭐ 评审 P2-3：人工处置的行带操作人。
        )
        response = self.client.get(f"/api/qqbot/groups/{self.group.pk}/join-requests/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertEqual(len(data["items"]), 2)
        by_jr = {row["join_request_id"]: row for row in data["items"]}
        # ⭐ 评审 P2-3：人工行带操作人 id + 显示名；机器行两格都是 null（前端显示「自动」）。
        self.assertEqual(by_jr["jr-local-2"]["acted_by"], operator.pk)
        self.assertEqual(by_jr["jr-local-2"]["acted_by_name"], operator.get_username())
        self.assertIsNone(by_jr["jr-local-1"]["acted_by"])
        self.assertIsNone(by_jr["jr-local-1"]["acted_by_name"])
        # 远端降级形态不变：失败只进 `remote_error`，本地行照出。
        self.assertEqual(data["remote_error"]["code"], "1030")

    def test_the_join_request_rows_stamp_moments_in_the_project_timezone(self):
        """D-0302（2026-10-24）：入群申请台账行的三个时刻键是**项目时区**（+08:00）串。

        QQ 机器人设置页截串直显；旧写法裸 datetime 经信封渲染成 UTC 串、差 8 小时。
        夹具把时刻钉在 UTC 04:30 ⇒ 北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        operator = get_user_model().objects.get(username="degrade-user")
        QQBotJoinRequestLog.objects.create(
            group=self.group,
            join_request_id="jr-tz-1",
            member_openid="member-tz",
            decision=QQBotJoinRequestLog.Decision.MANUAL_APPROVED,
            apply_at=fixed,
            acted_at=fixed,
            acted_by=operator,
        )
        row = {
            r["join_request_id"]: r
            for r in self.client.get(
                f"/api/qqbot/groups/{self.group.pk}/join-requests/"
            ).json()["data"]["items"]
        }["jr-tz-1"]
        self.assertEqual(row["apply_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(row["acted_at"], "2026-10-24T12:30:00+08:00")
        # `created_at` 是 auto_now_add（不可固定）⇒ 形状钉死即可。
        self.assertTrue(row["created_at"].endswith("+08:00"), row["created_at"])

    def test_share_link_degrades_to_the_business_code(self):
        response = self.client.post(
            "/api/qqbot/share-link/",
            {"group": self.group.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1030)


class GroupConfigTests(TenantTestCase):
    """群侧配置端点的行为钉：备注、推送配置、审批策略、人工审批、测试推送。"""

    def setUp(self):
        self.client = _node_client(username="group-config-user")
        _binding_configured()
        self.game = self._game("原神")
        self.group = _group("config-group-openid")

    @staticmethod
    def _game(name):
        from apps.business.models import Game

        return Game.objects.create(name=name)

    def test_patch_group_only_changes_remark(self):
        response = self.client.patch(
            f"/api/qqbot/groups/{self.group.pk}/",
            {"remark": "一群", "group_name": "想改官方名"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.group.refresh_from_db()
        self.assertEqual(self.group.remark, "一群")
        # 官方投影列**不许**被页面改掉（写入方是事件流）。
        self.assertEqual(self.group.group_name, "")

    def test_put_push_config_replaces_the_whole_row(self):
        response = self.client.put(
            f"/api/qqbot/groups/{self.group.pk}/push-config/",
            {"is_active": True, "msg_type": "markdown", "game_ids": [self.game.pk]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        config = self.group.push_config
        self.assertTrue(config.is_active)
        self.assertEqual(config.msg_type, "markdown")
        self.assertEqual(list(config.games.values_list("id", flat=True)), [self.game.pk])
        # 再 PUT 一份（整份替换语义：games 清空）。
        response = self.client.put(
            f"/api/qqbot/groups/{self.group.pk}/push-config/",
            {"is_active": False, "msg_type": "text", "game_ids": []},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        config.refresh_from_db()
        self.assertFalse(config.is_active)
        self.assertEqual(config.msg_type, "text")
        self.assertEqual(config.games.count(), 0)

    def test_put_push_config_with_unknown_game_is_a_validation_error(self):
        response = self.client.put(
            f"/api/qqbot/groups/{self.group.pk}/push-config/",
            {"is_active": True, "msg_type": "text", "game_ids": [999999]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000)
        self.assertFalse(
            QQBotGroupPushConfig.objects.filter(group=self.group).exists()
        )

    def test_patch_join_policy_stores_mode_and_whitelist(self):
        response = self.client.patch(
            f"/api/qqbot/groups/{self.group.pk}/join-policy/",
            {"mode": "whitelist", "whitelist": ["member-openid-1"]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.group.refresh_from_db()
        self.assertEqual(self.group.join_policy_mode, "whitelist")
        self.assertEqual(self.group.join_policy_whitelist, ["member-openid-1"])
        # 非 whitelist 模式照存空表（不留幽灵名单）。
        response = self.client.patch(
            f"/api/qqbot/groups/{self.group.pk}/join-policy/",
            {"mode": "approve_all"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.group.refresh_from_db()
        self.assertEqual(self.group.join_policy_whitelist, [])

    def test_groups_list_carries_the_saved_whitelist(self):
        """⭐ 评审 P1-2：群列表行带回已存白名单——此前只有 PATCH join-policy 的响应
        带名单 ⇒ 页面刷新后拿不回、编辑区被清成空（一条数据丢失路径）。"""
        self.group.join_policy_mode = QQBotGroup.JoinPolicyMode.WHITELIST
        self.group.join_policy_whitelist = ["member-openid-kept"]
        self.group.save(update_fields=["join_policy_mode", "join_policy_whitelist"])
        response = self.client.get("/api/qqbot/groups/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        row = next(
            r for r in response.json()["data"]["items"] if r["id"] == self.group.pk
        )
        # 判据钉**值**：响应里的名单 == 库里的名单。
        self.assertEqual(row["join_policy_whitelist"], ["member-openid-kept"])
        self.assertEqual(row["join_policy_mode"], "whitelist")

    def test_approve_success_writes_a_manual_approved_log_row(self):
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            return_value=(True, {}, "0", ""),
        ) as approve:
            response = self.client.post(
                f"/api/qqbot/groups/{self.group.pk}/join-requests/approve/",
                {"op": "approve", "join_request_id": "jr-9", "member_openid": "mem-9"},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertTrue(response.json()["data"]["ok"])
        row = QQBotJoinRequestLog.objects.get(join_request_id="jr-9")
        self.assertEqual(row.decision, QQBotJoinRequestLog.Decision.MANUAL_APPROVED)
        self.assertIsNotNone(row.acted_at)
        # ⭐ 评审 P2-3：人工审批的留痕行带**操作人**（= 发请求的登录用户）。
        operator = get_user_model().objects.get(username="group-config-user")
        self.assertEqual(row.acted_by_id, operator.pk)
        approve.assert_called_once()

    def test_approve_failure_still_writes_a_failed_log_row(self):
        with mock.patch(
            "apps.qqbot.qqbot_api.approve_join_request",
            return_value=(False, None, "11253", "无权限"),
        ):
            response = self.client.post(
                f"/api/qqbot/groups/{self.group.pk}/join-requests/approve/",
                {
                    "op": "decline",
                    "join_request_id": "jr-10",
                    "member_openid": "mem-10",
                    "reject_reason": "人满",
                },
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertFalse(data["ok"])
        row = QQBotJoinRequestLog.objects.get(join_request_id="jr-10")
        self.assertEqual(row.decision, QQBotJoinRequestLog.Decision.FAILED)
        self.assertEqual(row.error_code, "11253")
        self.assertEqual(row.reject_reason, "人满")

    def test_refresh_info_degrades_on_remote_failure(self):
        """「刷新群信息」：白名单接口未批（11253）⇒ info_error，绝不 500。"""
        with mock.patch(
            "apps.qqbot.qqbot_api.get_group_info",
            return_value=(False, None, "11253", "无权限"),
        ):
            response = self.client.get(
                f"/api/qqbot/groups/?refresh_info={self.group.pk}"
            )
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertEqual(data["info_error"]["code"], "11253")
        # 列表本体照出。
        self.assertEqual(len(data["items"]), 1)


class TestPushTests(TenantTestCase):
    """test-push → 发件箱行落库（断言打在数据库行上）。"""

    def test_test_push_creates_a_pending_row(self):
        client = _node_client(username="push-user")
        group = _group("push-group-openid")
        response = client.post(
            f"/api/qqbot/groups/{group.pk}/test-push/", {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        row_id = response.json()["data"]["id"]
        row = QQBotOutboundMessage.objects.get(pk=row_id)
        self.assertEqual(row.kind, QQBotOutboundMessage.Kind.TEST_PUSH)
        self.assertEqual(row.status, QQBotOutboundMessage.Status.PENDING)
        self.assertEqual(row.group_id, group.pk)

    def test_messages_list_returns_rows_with_truncated_payload(self):
        client = _node_client(username="msg-list-user")
        group = _group("msg-list-group-openid")
        client.post(
            f"/api/qqbot/groups/{group.pk}/test-push/", {}, content_type="application/json"
        )
        response = client.get(f"/api/qqbot/messages/?group={group.pk}")
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertEqual(data["total"], 1)
        row = data["items"][0]
        self.assertEqual(row["group_openid"], group.group_openid)
        self.assertEqual(row["kind"], "test_push")
        self.assertLessEqual(len(row["payload_text"]), 200)


class GroupIsolationTests(TenantTestCase):
    """跨租户红线（§4 / §5-G）：判据锚在 `group_openid`（租户内唯一、两租户取值不同）。

    ⚠️ 坑（`test_account_isolation.py` 的现场）：两个 schema 的主键各自从 1 起，
    「按 A 的 pk 取必 404」要靠**B 里造出 1..N-1、让 A 的靶子 pk=N 在 B 不存在**；
    跨域名调用会把连接留在那个租户上 ⇒ 每次 B 侧操作都 `try/finally` 钉回来。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="QQ 机器人隔离租户 B", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    #: 显式指定的高位主键：两个 schema 的 id 序列各自从 1 起且**跨用例前进**（实测：
    #: 序列不随事务回滚），靠「A 的靶子 pk 在 B 不存在」时不能用自增值拼运气——
    #: 两边都用测试自选的固定大号，靶子 pk 在 B 永远缺位。
    A_TARGET_PK = 900001
    B_OWN_PK = 900002

    def setUp(self):
        connection.set_tenant(self.tenant)  # ⚠️ 第一行，别删
        # A 的靶子（openid 与 B 的取值**不同**——这是跨租户判据的锚点，§5-G）。
        self.target_a = _group(OPENID_A, remark="A 的群", pk=self.A_TARGET_PK)
        # ⚠️ B 的用户必须建在 **B 的 schema** 里（账号表按租户分）；B 自己的群同理。
        with tenant_context(self.tenant_b):
            self.own_b = _group(OPENID_B, remark="B 自己的群", pk=self.B_OWN_PK)
            self.client_b = _node_client(
                username="qqbot-node-user-b", domain=DOMAIN_B
            )
        self.client = _node_client(username="qqbot-node-user-a")
        connection.set_tenant(self.tenant)

    def _b_call(self, method, url, body=None):
        try:
            if method == "get":
                return self.client_b.get(url)
            return getattr(self.client_b, method)(
                url, body, content_type="application/json"
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_b_list_only_contains_its_own_group(self):
        response = self._b_call("get", "/api/qqbot/groups/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        openids = [row["group_openid"] for row in response.json()["data"]["items"]]
        # 判据：**取值不同**的 openid——B 只见 BBBB…，不见 AAAA…。
        self.assertEqual(openids, [OPENID_B])
        self.assertNotIn(OPENID_A, openids)

    def test_b_patch_on_a_pk_missing_in_b_is_a_business_rejection_and_a_row_is_untouched(self):
        response = self._b_call(
            "patch", f"/api/qqbot/groups/{self.A_TARGET_PK}/", {"remark": "越权改"}
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 1000)
        # A 的那一行分毫未动（判据打在数据库行上，E-0009）。
        self.target_a.refresh_from_db()
        self.assertEqual(self.target_a.remark, "A 的群")

    def test_positive_control_b_can_patch_its_own_group(self):
        """正向对照：没有它，「拒绝」可能由「端点对谁都不工作」凑出来。"""
        response = self._b_call(
            "patch", f"/api/qqbot/groups/{self.B_OWN_PK}/", {"remark": "B 改自己的"}
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(response.json()["code"], 0)
        with tenant_context(self.tenant_b):
            self.assertEqual(
                QQBotGroup.objects.get(pk=self.B_OWN_PK).remark, "B 改自己的"
            )
        self.target_a.refresh_from_db()
        self.assertEqual(self.target_a.remark, "A 的群")

    def test_b_messages_list_never_contains_a_rows(self):
        # A 里排一条测试消息；B 的发件箱必须一行都没有。
        from apps.qqbot.push import queue_test_push

        queue_test_push(self.target_a)
        response = self._b_call("get", "/api/qqbot/messages/")
        self.assertEqual(response.status_code, 200, response.content[:200])
        data = response.json()["data"]
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["items"], [])
        # 正向对照：A 自己能看到这一行。
        response = self.client.get("/api/qqbot/messages/")
        self.assertEqual(response.json()["data"]["total"], 1)
        # 负向再钉一层数据库行：那一行还好好地在 A 的 schema 里。
        self.assertEqual(QQBotOutboundMessage.objects.count(), 1)
