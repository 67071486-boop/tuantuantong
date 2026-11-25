"""交易平台对接设置接口（计划 `2026-10-26-platform-integration-settings.md` T2）。

覆盖计划 T2 验收里的五类：CRUD、凭据不回显、默认唯一 / 换默认、平台白名单、
全局设置读写、租户隔离、未授权 403。

## 判据口径（照本仓既有约定）

- **写操作断言数据库终态**（`AGENTS.md` §5-G）：不看前端提示、不看响应里的对象，
  改完直接读 `PlatformIntegration` / `PlatformPushSettings` 的行。
- **凭据不回显全句搜**：把响应 JSON 序列化成一整句话，明文 / md5 任何一个出现即失败；
  同时递归收集所有键，断言 `app_secret` / `pay_password` / `pay_password_md5` **一个都不在**。
- **跨租户的否定断言用业务值、不用主键**（`tests/test_feedback_isolation.py` 的口径）：
  两个租户的账号字面量不同；主键只当入参，且夹具自证两边错开。
- 夹具值一律**合成值**（PII 守卫扫 `src/**`）。
"""
import hashlib
import json
from unittest import mock

from django.db import IntegrityError, connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.api.platform_integration_views import PlatformIntegrationSerializer
from apps.business.models import PlatformIntegration, PlatformPushSettings
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

NODE = "system.platform_integration_settings"
URL_LIST = "/api/business/platform-integrations/"
URL_SETTINGS = "/api/business/platform-integrations/settings/"
#: ⭐ 评审 B1：「一键发单」入口可见性探针（只要登录 + `order.list`）。
URL_PUSH_AVAILABILITY = "/api/business/platform-integrations/push-availability/"


def detail_url(pk) -> str:
    return f"{URL_LIST}{pk}/"


#: 租户 A = `TenantTestCase` 的 `test` schema（与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名：后缀 `.test.local` 在 `.env` 的 `DJANGO_ALLOWED_HOSTS` 白名单里。
DOMAIN_B = "platform-integration-iso-b.test.local"
SCHEMA_B = "platform_integration_iso_b"

#: 证据件 `:68-87` 的 7 个平台 id（与 `platform_integration_views.AVAILABLE_PLATFORMS` 同值，
#: 但这里**写死字面量**：从被测代码 import 集合会让「白名单被改宽」这条用例失去判别力）。
PAGE_PLATFORM_IDS = (10, 30, 80, 90, 93, 50, 60)

#: 合成凭据（⛔ 不是任何真实密钥；PII / 真值守卫扫 `src/**`）。
APP_SECRET = "synthetic-app-secret-3f9a"
PAY_PASSWORD = "synthetic-pay-pw-71c"


def _md5(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def _all_keys(node) -> set:
    """递归收集 JSON 里出现过的所有键（用来断言凭据键一个都没下发）。"""
    if isinstance(node, dict):
        keys = set(node)
        for value in node.values():
            keys |= _all_keys(value)
        return keys
    if isinstance(node, list):
        keys = set()
        for value in node:
            keys |= _all_keys(value)
        return keys
    return set()


class PlatformIntegrationApiTests(TenantTestCase):
    def setUp(self):
        # 跨域名请求不会在响应结束时还原 schema（同 `tests/test_feedback_isolation.py` 的说明），
        # 所以每条用例开头把连接显式钉回租户 A。
        connection.set_tenant(self.tenant)
        self.client = make_logged_in_client(
            username="pi-user", groups=("客服",), nodes=(NODE,)
        )

    # ── 小工具 ──────────────────────────────────────────────────────
    def _get(self, url=URL_LIST, client=None):
        return (client or self.client).get(url)

    def _post(self, payload, client=None):
        return (client or self.client).post(
            URL_LIST, payload, content_type="application/json"
        )

    def _patch(self, pk, payload, client=None):
        return (client or self.client).patch(
            detail_url(pk), payload, content_type="application/json"
        )

    def _delete(self, pk, client=None):
        return (client or self.client).delete(detail_url(pk))

    def _put_settings(self, payload):
        return self.client.put(
            URL_SETTINGS, payload, content_type="application/json"
        )

    def _payload(self, **overrides):
        payload = {
            "platform": 10,
            "account": "USR-SYNTH-0001",
            "app_id": "app-synth-01",
            "app_secret": APP_SECRET,
            "pay_password": PAY_PASSWORD,
            "is_enabled": True,
        }
        payload.update(overrides)
        return payload

    # ── 1. CRUD：建 → 读列表 → 改 → 删，断言数据库终态 ─────────────────
    def test_crud_round_trip_checks_database_final_state(self):
        created = self._post(self._payload())
        self.assertEqual(created.data["code"], 0, created.data)
        pk = created.data["data"]["id"]
        self.assertEqual(created.data["data"]["account"], "USR-SYNTH-0001")

        row = PlatformIntegration.objects.get(pk=pk)
        self.assertEqual(row.platform, 10)
        self.assertEqual(row.account, "USR-SYNTH-0001")
        self.assertEqual(row.app_id, "app-synth-01")
        self.assertEqual(row.app_secret, APP_SECRET)  # 明文原样存（口径见任务 §2）
        self.assertEqual(row.pay_password_md5, _md5(PAY_PASSWORD))
        self.assertTrue(row.is_enabled)

        listed = self._get()
        self.assertEqual(listed.data["code"], 0, listed.data)
        self.assertIsInstance(listed.data["data"], list, "列表按约定直接回数组")
        self.assertEqual(
            [item["account"] for item in listed.data["data"]], ["USR-SYNTH-0001"]
        )

        patched = self._patch(
            pk, {"account": "USR-SYNTH-0002", "remark": "改过的备注", "is_enabled": False}
        )
        self.assertEqual(patched.data["code"], 0, patched.data)
        row.refresh_from_db()
        self.assertEqual(row.account, "USR-SYNTH-0002")
        self.assertEqual(row.remark, "改过的备注")
        self.assertFalse(row.is_enabled)

        deleted = self._delete(pk)
        self.assertEqual(deleted.data["code"], 0, deleted.data)
        self.assertFalse(PlatformIntegration.objects.filter(pk=pk).exists())

    # ── 2. 凭据不回显（整句搜 + 递归键集）─────────────────────────────
    def test_credentials_never_echoed_in_list_detail_or_write_responses(self):
        created = self._post(self._payload())
        self.assertEqual(created.data["code"], 0, created.data)
        pk = created.data["data"]["id"]

        responses = {
            "post": created,
            "list": self._get(),
            "detail": self._get(detail_url(pk)),
            "patch": self._patch(pk, {"remark": "只改备注"}),
        }
        for where, response in responses.items():
            with self.subTest(where=where):
                self.assertIn(response.status_code, (200,), response.content[:300])
                body = response.json()
                text = json.dumps(body, ensure_ascii=False)
                # ⛔ 明文与 md5 都不许出现在**任何位置**（包括信封 msg / 其它字段）。
                self.assertNotIn(PAY_PASSWORD, text, f"{where} 下发了支付密码明文")
                self.assertNotIn(_md5(PAY_PASSWORD), text, f"{where} 下发了支付密码 md5")
                self.assertNotIn(APP_SECRET, text, f"{where} 下发了 AppSecret")
                keys = _all_keys(body)
                self.assertNotIn("app_secret", keys, f"{where} 下发了 app_secret 键")
                self.assertNotIn("pay_password", keys, f"{where} 下发了 pay_password 键")
                self.assertNotIn(
                    "pay_password_md5", keys, f"{where} 下发了 pay_password_md5 键"
                )
                # 只回答「配没配」。
                item = body["data"]
                if isinstance(item, list):
                    item = item[0]
                self.assertIs(item["has_secret"], True)
                self.assertIs(item["has_password"], True)

    # ── 3. 落库值 = md5(密码).hexdigest()；不传凭据 = 保持原值 ─────────
    def test_pay_password_is_stored_as_md5_and_omitted_credentials_are_kept(self):
        created = self._post(self._payload())
        pk = created.data["data"]["id"]
        expected = _md5(PAY_PASSWORD)
        row = PlatformIntegration.objects.get(pk=pk)
        self.assertEqual(row.pay_password_md5, expected)
        self.assertEqual(len(row.pay_password_md5), 32)
        self.assertEqual(row.pay_password_md5, row.pay_password_md5.lower())
        self.assertNotEqual(row.pay_password_md5, PAY_PASSWORD)

        # 不传凭据 ⇒ 原值不被清空（不能变成空串）。
        patched = self._patch(pk, {"account": "USR-SYNTH-KEEP"})
        self.assertEqual(patched.data["code"], 0, patched.data)
        row.refresh_from_db()
        self.assertEqual(row.app_secret, APP_SECRET)
        self.assertEqual(row.pay_password_md5, expected)

        # 编辑弹窗里 AppSecret 留空 = 不改（照 XgjShopBinding 的先例）。
        blank_secret = self._patch(pk, {"app_secret": ""})
        self.assertEqual(blank_secret.data["code"], 0, blank_secret.data)
        row.refresh_from_db()
        self.assertEqual(row.app_secret, APP_SECRET)

        # 明确给新值才改；支付密码同样按新明文重算。
        new_pw = "another-synthetic-pw-55a"
        replaced = self._patch(pk, {"app_secret": "new-synthetic-secret-9c", "pay_password": new_pw})
        self.assertEqual(replaced.data["code"], 0, replaced.data)
        row.refresh_from_db()
        self.assertEqual(row.app_secret, "new-synthetic-secret-9c")
        self.assertEqual(row.pay_password_md5, _md5(new_pw))

        # 传空串 / 超长 = 明确拒绝（不是静默 no-op），库里一个字不变。
        for bad in ("", "x" * 65):
            with self.subTest(length=len(bad)):
                before = PlatformIntegration.objects.get(pk=pk).pay_password_md5
                response = self._patch(pk, {"pay_password": bad})
                self.assertEqual(response.data["code"], 1000, response.data)
                self.assertIn("支付密码", response.data["msg"])
                self.assertEqual(
                    PlatformIntegration.objects.get(pk=pk).pay_password_md5, before
                )

        # AppSecret **原样存**：⛔ 不 trim（首尾空白由平台方决定，本仓不替它裁）。
        padded = self._post(
            self._payload(account="PADDED-SECRET", app_secret="  padded-secret  ")
        )
        self.assertEqual(padded.data["code"], 0, padded.data)
        self.assertEqual(
            PlatformIntegration.objects.get(
                pk=padded.data["data"]["id"]
            ).app_secret,
            "  padded-secret  ",
        )

    # ── 4. 默认唯一 / 换默认 ─────────────────────────────────────────
    def test_default_is_unique_per_platform_and_switching_clears_the_old_one(self):
        first = self._post(self._payload(account="DEFAULT-A", is_default=True))
        self.assertEqual(first.data["code"], 0, first.data)
        first_pk = first.data["data"]["id"]

        # 同平台再建一个默认 ⇒ 不是「两个默认并存」，而是**换默认**：
        # 旧默认必须先被清掉、新默认再置上（同一个事务内）。
        second = self._post(self._payload(account="DEFAULT-B", is_default=True))
        self.assertEqual(second.data["code"], 0, second.data)
        second_pk = second.data["data"]["id"]

        defaults = PlatformIntegration.objects.filter(platform=10, is_default=True)
        self.assertEqual(defaults.count(), 1)
        self.assertEqual(defaults.get().pk, second_pk)
        self.assertFalse(PlatformIntegration.objects.get(pk=first_pk).is_default)

        # PATCH 把默认切回第一行：同样先清后置。
        switched = self._patch(first_pk, {"is_default": True})
        self.assertEqual(switched.data["code"], 0, switched.data)
        self.assertEqual(
            list(
                PlatformIntegration.objects.filter(platform=10, is_default=True)
                .values_list("pk", flat=True)
            ),
            [first_pk],
        )

        # 不同平台各有一个默认是合法的（约束是「同平台」唯一，不是全局唯一）。
        other_platform = self._post(
            self._payload(platform=30, account="DEFAULT-30", is_default=True)
        )
        self.assertEqual(other_platform.data["code"], 0, other_platform.data)
        self.assertEqual(
            PlatformIntegration.objects.filter(is_default=True).count(), 2
        )

    def test_default_conflict_integrity_error_is_translated_to_business_error(self):
        """并发窗口的兜底：`IntegrityError` ⇒ 带字段名的中文 1000，⛔ 不是 500。

        ⚠️ 正常路径不会撞约束（换默认在先清后置里做完了）。这里把「清旧默认」这一步
        打桩成 no-op，**强制**制造一次约束冲突——被测的是 `perform_create` 的翻译分支，
        不是并发本身（并发用例会脆，且不在这条任务的判据里）。
        ⚠️ 桩打在**被查找的类属性**上（`PlatformIntegrationSerializer._demote_existing_default`），
        并且下面先自证「桩真的没清旧默认」——否则这条用例可能因为别的原因此绿。
        """
        self._post(self._payload(account="ORIGINAL-DEFAULT", is_default=True))

        with mock.patch.object(
            PlatformIntegrationSerializer,
            "_demote_existing_default",
            autospec=True,
        ) as demote:
            response = self._post(self._payload(account="CONFLICTING-DEFAULT", is_default=True))

        self.assertEqual(response.data["code"], 1000, response.data)
        self.assertIn("默认账号", response.data["msg"])
        self.assertTrue(demote.called, "桩没接上：清旧默认那一步根本没被调用")
        # 库里仍然只有原来那一行默认；冲突行**没有落库**（事务回滚）。
        self.assertEqual(
            list(
                PlatformIntegration.objects.filter(platform=10, is_default=True)
                .values_list("account", flat=True)
            ),
            ["ORIGINAL-DEFAULT"],
        )
        self.assertFalse(
            PlatformIntegration.objects.filter(account="CONFLICTING-DEFAULT").exists()
        )

    def test_default_switch_rolls_back_when_create_fails_in_serializer_transaction(self):
        """「先清旧默认、再置新默认」必须与失败回滚在**同一个事务**里。

        ⚠️ 判据必须**绕过视图的 `perform_create` atomic**、直接走序列化器的
        `create()`：视图那层 atomic 会兜住失败，即使序列化器那层被删掉，用例仍会绿
        ⇒ 那样就测不到 `PlatformIntegrationSerializer.create` 的 `transaction.atomic()`。
        打桩让 `super().create` 在**清旧默认之后**抛 `IntegrityError`；若没有同一事务，
        清掉的那一步会留在库里 ⇒ 旧默认变 False。
        """
        first = self._post(self._payload(account="ROLLBACK-CREATE-OLD", is_default=True))
        first_pk = first.data["data"]["id"]

        serializer = PlatformIntegrationSerializer(
            data=self._payload(account="ROLLBACK-CREATE-NEW", is_default=True)
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with mock.patch(
            "rest_framework.serializers.ModelSerializer.create",
            side_effect=IntegrityError("forced-failure-after-demote"),
        ):
            with self.assertRaises(IntegrityError):
                serializer.save()

        self.assertTrue(PlatformIntegration.objects.get(pk=first_pk).is_default)
        self.assertFalse(
            PlatformIntegration.objects.filter(
                account="ROLLBACK-CREATE-NEW"
            ).exists()
        )

    def test_default_switch_rolls_back_when_update_fails_in_serializer_transaction(self):
        """同上一支的更新版：PATCH 换默认时 `super().update` 失败，旧默认要回来。"""
        first = self._post(self._payload(account="ROLLBACK-UPDATE-OLD", is_default=True))
        first_pk = first.data["data"]["id"]
        second = self._post(self._payload(account="ROLLBACK-UPDATE-NEW"))
        second_pk = second.data["data"]["id"]

        serializer = PlatformIntegrationSerializer(
            PlatformIntegration.objects.get(pk=second_pk),
            data={"is_default": True},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with mock.patch(
            "rest_framework.serializers.ModelSerializer.update",
            side_effect=IntegrityError("forced-failure-after-demote"),
        ):
            with self.assertRaises(IntegrityError):
                serializer.save()

        self.assertTrue(PlatformIntegration.objects.get(pk=first_pk).is_default)
        self.assertFalse(PlatformIntegration.objects.get(pk=second_pk).is_default)

    def test_other_integrity_error_is_not_masked_as_default_conflict(self):
        """非本约束的 `IntegrityError` 必须原样冒出（500），⛔ 不能被伪装成「默认账号冲突」。

        ⚠️ 没有这条，把 `_save_translating_default_conflict` 的 `if` 改成无条件翻译
        也不会有任何用例变红——那会让真正的库结构错误被一句假业务文案盖住。
        """
        self.client.raise_request_exception = False
        try:
            with mock.patch.object(
                PlatformIntegrationSerializer,
                "create",
                side_effect=IntegrityError("some_other_constraint"),
            ):
                response = self._post(self._payload(account="OTHER-CONSTRAINT"))
        finally:
            self.client.raise_request_exception = True

        self.assertEqual(response.status_code, 500, response.content[:300])
        self.assertNotIn(
            "默认账号设置失败",
            response.content.decode("utf-8", "replace"),
            "非本约束的 IntegrityError 被伪装成了默认账号冲突",
        )

    def test_options_and_head_are_allowed(self):
        """`OPTIONS` / `HEAD` 不该回 405（补 `http_method_names` 的判据）。

        先例：`xgj_views.py` 的 `XgjShopBindingViewSet` 把这两个动词显式列进
        `http_method_names`；本视图原先漏了 ⇒ 实测两者都是 405。
        """
        self.assertEqual(self.client.options(URL_LIST).status_code, 200)
        self.assertEqual(self.client.head(URL_LIST).status_code, 200)

    def test_deleting_the_current_default_is_allowed_and_leaves_no_default(self):
        created = self._post(self._payload(account="ONLY-DEFAULT", is_default=True))
        pk = created.data["data"]["id"]
        deleted = self._delete(pk)
        self.assertEqual(deleted.data["code"], 0, deleted.data)
        self.assertEqual(PlatformIntegration.objects.filter(platform=10).count(), 0)
        # ⛔ 不自动挑一个顶上（没有别的行可挑；这里钉住「删完就是没有默认」）。
        self.assertEqual(
            PlatformIntegration.objects.filter(platform=10, is_default=True).count(), 0
        )

    # ── 5. 平台白名单 ────────────────────────────────────────────────
    def test_only_the_seven_page_platforms_are_accepted(self):
        for platform in PAGE_PLATFORM_IDS:
            with self.subTest(platform=platform):
                response = self._post(
                    self._payload(platform=platform, account=f"PAGE-{platform}")
                )
                self.assertEqual(response.data["code"], 0, response.data)
        self.assertEqual(PlatformIntegration.objects.count(), 7)

        # 模型 choices 里另外 6 个（8/70/91/92/94/95）页面选不出来 ⇒ 一律拒。
        for platform in (8, 70, 91, 92, 94, 95):
            with self.subTest(platform=platform):
                response = self._post(
                    self._payload(platform=platform, account=f"GHOST-{platform}")
                )
                self.assertEqual(response.data["code"], 1000, response.data)
                self.assertIn("平台", response.data["msg"])
                self.assertIn("可对接范围", response.data["msg"])
        # 连模型 choices 都不存在的值也同样拒（不是 500）。
        for platform in (999, "abc"):
            with self.subTest(platform=platform):
                response = self._post(
                    self._payload(platform=platform, account="BAD-PLATFORM")
                )
                self.assertEqual(response.data["code"], 1000, response.data)
                self.assertIn("平台", response.data["msg"])

        self.assertEqual(PlatformIntegration.objects.count(), 7, "幽灵平台落库了")

    def test_is_available_is_true_only_for_dailiantong(self):
        ids = {}
        for platform in PAGE_PLATFORM_IDS:
            response = self._post(
                self._payload(platform=platform, account=f"AVAIL-{platform}")
            )
            ids[platform] = response.data["data"]["id"]

        items = self._get().data["data"]
        by_platform = {item["platform"]: item for item in items}
        self.assertEqual(set(by_platform), set(PAGE_PLATFORM_IDS))
        for platform, item in by_platform.items():
            with self.subTest(platform=platform):
                self.assertIs(
                    item["is_available"], platform == 10,
                    "只有代练通（10）有客户端；其余 6 个能存但不可用",
                )

    # ── 6. 全局设置：读写 + 非法值 ───────────────────────────────────
    def test_settings_read_write_and_invalid_values_are_rejected(self):
        initial = self._get(URL_SETTINGS)
        self.assertEqual(initial.data["code"], 0, initial.data)
        self.assertEqual(len(initial.data["data"]), 12, initial.data)
        self.assertEqual(
            set(initial.data["data"]),
            {
                "quick_push_enabled",
                "legacy_simple_mode",
                "check_game_region",
                "forbid_push_when_double_gold_zero",
                "duplicate_push_check",
                "select_jidanpu_center_by_default",
                "auto_zero_refund_on_conflict",
                "block_push_completed_orders",
                "auto_send_group_qr_after_accept",
                # ⭐ 一键抛单 B4：门槛默认值两份 + 代练要求模板。
                "default_power_level",
                "default_limit_accept",
                "require_template",
            },
        )
        # 三列的首读默认值（⛔ `default_limit_accept` 是 null，不是 0）。
        self.assertEqual(initial.data["data"]["default_power_level"], 1)
        self.assertIsNone(initial.data["data"]["default_limit_accept"])
        self.assertEqual(initial.data["data"]["require_template"], "")
        # 懒建单行：GET 之后库里恰好一行 `pk=1`。
        self.assertEqual(PlatformPushSettings.objects.count(), 1)
        self.assertEqual(PlatformPushSettings.objects.get().pk, 1)

        # PUT 只给一个键也接受（全量 / 部分都收）。
        partial = self._put_settings({"legacy_simple_mode": 1})
        self.assertEqual(partial.data["code"], 0, partial.data)
        self.assertEqual(
            PlatformPushSettings.objects.get(pk=1).legacy_simple_mode, 1
        )
        self.assertEqual(self._get(URL_SETTINGS).data["data"]["legacy_simple_mode"], 1)

        # 全量保存。
        full = {
            "quick_push_enabled": False,
            "legacy_simple_mode": 0,
            "check_game_region": 1,
            "forbid_push_when_double_gold_zero": True,
            "duplicate_push_check": 1,
            "select_jidanpu_center_by_default": True,
            "auto_zero_refund_on_conflict": False,
            "block_push_completed_orders": 1,
            "auto_send_group_qr_after_accept": 1,
            "default_power_level": 5,
            "default_limit_accept": 20,
            "require_template": "合成模板：接单后联系客服",
        }
        saved = self._put_settings(full)
        self.assertEqual(saved.data["code"], 0, saved.data)
        row = PlatformPushSettings.objects.get(pk=1)
        for field, value in full.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(row, field), value)
        # 存进去的是 `0/1` 整数，不是布尔（两套编码别强行归一）。
        self.assertIs(type(row.legacy_simple_mode), int)
        self.assertEqual(row.legacy_simple_mode, 0)

        # 非法整数（不在 choices 里）⇒ 带字段名的中文 1000，且库值不变。
        before = PlatformPushSettings.objects.get(pk=1).legacy_simple_mode
        bad_choice = self._put_settings({"legacy_simple_mode": 2})
        self.assertEqual(bad_choice.data["code"], 1000, bad_choice.data)
        self.assertIn("传统简易模式", bad_choice.data["msg"])
        self.assertEqual(
            PlatformPushSettings.objects.get(pk=1).legacy_simple_mode, before
        )
        bad_choice_2 = self._put_settings({"duplicate_push_check": 9})
        self.assertEqual(bad_choice_2.data["code"], 1000, bad_choice_2.data)
        self.assertIn("检测重复发单", bad_choice_2.data["msg"])

        # 三列各自的边界（⛔ 不 500；带字段名中文）。
        for payload, needle in (
            ({"default_power_level": -1}, "要求最低完单数"),
            ({"default_power_level": "abc"}, "要求最低完单数"),
            ({"default_limit_accept": -1}, "拒绝新手和介入率"),
            ({"default_limit_accept": 101}, "拒绝新手和介入率"),
            ({"default_limit_accept": "abc"}, "拒绝新手和介入率"),
            ({"require_template": "话" * 501}, "代练要求模板"),
        ):
            with self.subTest(payload=payload):
                bad = self._put_settings(payload)
                self.assertEqual(bad.data["code"], 1000, bad.data)
                self.assertIn(needle, bad.data["msg"])
        # 显式 `null` = 不设置（不是「没传」）：能落库成 null。
        cleared = self._put_settings({"default_limit_accept": None})
        self.assertEqual(cleared.data["code"], 0, cleared.data)
        self.assertIsNone(PlatformPushSettings.objects.get(pk=1).default_limit_accept)

        # 未知字段 ⇒ 拒绝（⛔ 不能静默忽略：拼错的键会「保存成功」但什么都没改）。
        unknown = self._put_settings({"legacy_simple_mod": 1})
        self.assertEqual(unknown.data["code"], 1000, unknown.data)
        self.assertIn("不支持的设置项", unknown.data["msg"])
        self.assertIn("legacy_simple_mod", unknown.data["msg"])

        # 非对象 body 也是 1000，不是 500。
        # ⚠️ 必须自己 `json.dumps`：Django 测试 client 只对 dict / list 自动编码，
        # 裸字符串会被当成原始 body 发出去（`abc` 不是合法 JSON ⇒ 400 解析错误）。
        for body in ([], "abc", 3):
            with self.subTest(body=body):
                response = self.client.put(
                    URL_SETTINGS, json.dumps(body), content_type="application/json"
                )
                self.assertEqual(response.data["code"], 1000, response.data)
                self.assertIn("请求体必须是对象", response.data["msg"])

    # ── 6b. 写操作留痕（纯 no-op 不写；凭据不进日志）────────────────────
    def test_write_actions_are_audited_and_noop_is_not_logged(self):
        created = self._post(self._payload(account="AUDIT-ACCOUNT"))
        pk = created.data["data"]["id"]

        create_log = OperationLog.objects.get(
            action="system.platform_integration_create"
        )
        self.assertEqual(create_log.target_kind, "platform_account")
        self.assertEqual(create_log.target_id, str(pk))
        self.assertEqual(create_log.target_name, "AUDIT-ACCOUNT")
        self.assertEqual(create_log.log_type, 40)  # LogType.SYSTEM

        # 纯 no-op 的 PATCH 不写留痕（remark 的当前值就是空串）。
        before = OperationLog.objects.count()
        noop = self._patch(pk, {"remark": ""})
        self.assertEqual(noop.data["code"], 0, noop.data)
        self.assertEqual(OperationLog.objects.count(), before)

        # 真改一个字段 ⇒ 恰好一条 update。
        self._patch(pk, {"remark": "改过"})
        self.assertEqual(
            OperationLog.objects.filter(
                action="system.platform_integration_update"
            ).count(),
            1,
        )

        # 删除：`target_id` 在删除前取 ⇒ 删完仍指得回那一行。
        self._delete(pk)
        destroy_log = OperationLog.objects.get(
            action="system.platform_integration_destroy"
        )
        self.assertEqual(destroy_log.target_id, str(pk))
        self.assertEqual(destroy_log.target_name, "AUDIT-ACCOUNT")

        # 全局设置：先 GET（懒建、不写日志），再 PUT 同值（no-op）、再真改。
        self._get(URL_SETTINGS)
        before = OperationLog.objects.count()
        noop_put = self._put_settings({"quick_push_enabled": True})
        self.assertEqual(noop_put.data["code"], 0, noop_put.data)
        self.assertEqual(OperationLog.objects.count(), before)
        self._put_settings({"quick_push_enabled": False})
        self.assertEqual(
            OperationLog.objects.filter(
                action="system.platform_settings_update"
            ).count(),
            1,
        )

        # 凭据红线：日志的任何文本列里都不许出现明文 / md5。
        logged = json.dumps(
            list(
                OperationLog.objects.values_list(
                    "summary",
                    "target_name",
                    "target_id",
                    "field_name",
                    "from_value",
                    "to_value",
                    "reason",
                )
            ),
            ensure_ascii=False,
        )
        self.assertNotIn(APP_SECRET, logged)
        self.assertNotIn(PAY_PASSWORD, logged)
        self.assertNotIn(_md5(PAY_PASSWORD), logged)

    # ── 7. 未授权 403 / 未登录 401 ───────────────────────────────────
    def test_unauthorized_requests_are_403_and_anonymous_is_401(self):
        # ⚠️ 用**不加入任何组**的账号：`setUp` 那个 client 已经把本点挂到「客服」组上了，
        # 再用同一个组会让这个"无权限"账号**继承**那个节点（403 变成 200，用例假红）。
        no_node = make_logged_in_client(username="pi-no-node")
        self.assertEqual(self._get(client=no_node).status_code, 403)
        self.assertEqual(
            self._post(self._payload(), client=no_node).status_code, 403
        )
        self.assertEqual(
            no_node.get(URL_SETTINGS).status_code, 403
        )
        self.assertEqual(
            no_node.put(
                URL_SETTINGS, {"quick_push_enabled": True},
                content_type="application/json",
            ).status_code,
            403,
        )

        anonymous = Client(SERVER_NAME=DOMAIN_A)
        self.assertEqual(anonymous.get(URL_LIST).status_code, 401)
        self.assertEqual(anonymous.get(URL_SETTINGS).status_code, 401)

        # 对照：持点客户端的同样请求是通的（否则上面的 403 可能只是「端点坏了」）。
        self.assertEqual(self._get().status_code, 200)
        self.assertEqual(self._get(URL_SETTINGS).status_code, 200)


class PushAvailabilityProbeTests(TenantTestCase):
    """⭐ 评审 B1（2026-10-27）：「一键发单」入口探针的权限面。

    现场：生产上 `客服 / 客服主管 / 售后客服 / 人事客服 / 发单客服` **全部没有**
    `system.platform_integration_settings` 节点 ⇒ 前端读账号列表必 403 ⇒ 按钮一直不画。
    业务口径：**能看到订单列表（`order.list`）就该看到这颗按钮**；真正的发单动作仍由
    `POST /orders/<no>/push-to-platform/` 自己校验（⭐ M5 起写动作节点 =
    `order.platform_write`，外加配置 + 状态机；本探针只回答「看得到入口」）。

    ⚠️ 用例里**刻意不造任何 `PlatformIntegration` 行**：探针回答的是「这个人该不该看到入口」，
    不是「本租户配好了没有」——把它写成配置门会让这个已确认的生产缺陷原样回来。
    ⚠️ 对照腿（同账号读配置列表仍 403）必须有：否则 200 可能只是因为它偷偷有那个页面节点。
    """

    def setUp(self):
        connection.set_tenant(self.tenant)

    def test_an_order_list_only_account_gets_available_true(self):
        kefu = make_logged_in_client(
            username="pi-push-kefu", groups=("客服",), nodes=("order.list",)
        )
        res = kefu.get(URL_PUSH_AVAILABILITY)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["code"], 0)
        self.assertEqual(res.data["data"], {"available": True})
        # 对照：同一个账号**读不到**配置列表（页面节点没给）⇒ 探针的 200 不是继承来的。
        self.assertEqual(kefu.get(URL_LIST).status_code, 403)

    def test_anonymous_is_401(self):
        anonymous = Client(SERVER_NAME=DOMAIN_A)
        self.assertEqual(anonymous.get(URL_PUSH_AVAILABILITY).status_code, 401)

    def test_without_order_list_is_403(self):
        no_node = make_logged_in_client(username="pi-push-no-node")
        self.assertEqual(no_node.get(URL_PUSH_AVAILABILITY).status_code, 403)


class PlatformIntegrationIsolationTests(TenantTestCase):
    """以租户 A 的凭据访问 B 的数据必须失败（`AGENTS.md` §4 的强制测试）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="平台对接隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)

    def _seed(self):
        """两个租户各造自己的行 + 各自的登录 client，返回 `(pk_a, pk_b, client_a, client_b)`。

        A 造两行、B 造一行 ⇒ A 的目标主键必然大于 B 的最大主键；用例自己断言这件事
        （⛔ 不靠推测——两个 schema 的主键序列各自从 1 开始）。
        """
        with tenant_context(self.tenant):
            PlatformIntegration.objects.create(
                platform=10, account="A-ACCOUNT-ONLY-1"
            )
            row_a = PlatformIntegration.objects.create(
                platform=10, account="A-ACCOUNT-ONLY-2", remark="A 原备注"
            )
            client_a = make_logged_in_client(
                domain=DOMAIN_A, username="pi-iso-a", groups=("客服",), nodes=(NODE,)
            )
        with tenant_context(self.tenant_b):
            row_b = PlatformIntegration.objects.create(
                platform=10, account="B-ACCOUNT-ONLY-1"
            )
            client_b = make_logged_in_client(
                domain=DOMAIN_B, username="pi-iso-b", groups=("客服",), nodes=(NODE,)
            )
        self.assertNotEqual(
            row_a.pk, row_b.pk,
            "夹具没造对：两租户的主键撞了 ⇒ 下面的跨租户用例测不到隔离",
        )
        return row_a.pk, row_b.pk, client_a, client_b

    def _rows_in(self, tenant):
        with tenant_context(tenant):
            return sorted(
                PlatformIntegration.objects.values_list("account", "remark")
            )

    def test_each_tenant_lists_only_its_own_rows(self):
        _pk_a, _pk_b, client_a, client_b = self._seed()

        accounts_a = [
            item["account"] for item in client_a.get(URL_LIST).data["data"]
        ]
        accounts_b = [
            item["account"] for item in client_b.get(URL_LIST).data["data"]
        ]
        self.assertEqual(
            accounts_a, ["A-ACCOUNT-ONLY-1", "A-ACCOUNT-ONLY-2"]
        )
        self.assertEqual(accounts_b, ["B-ACCOUNT-ONLY-1"])

    def test_tenant_b_cannot_patch_or_delete_tenant_a_rows(self):
        pk_a, _pk_b, _client_a, client_b = self._seed()

        try:
            patched = client_b.patch(
                detail_url(pk_a),
                {"remark": "跨租户改", "account": "B-STOLE-A"},
                content_type="application/json",
                SERVER_NAME=DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(patched.data["code"], 1002, patched.data)
        self.assertEqual(patched.data["msg"], "记录不存在或已被删除")

        try:
            deleted = client_b.delete(detail_url(pk_a), SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(deleted.status_code, 200, deleted.content[:300])
        self.assertEqual(deleted.data["code"], 1002, deleted.data)

        # 判据打在**业务值**上（不是主键）：A 的两行原样还在，B 那边也没多出行。
        self.assertEqual(
            self._rows_in(self.tenant),
            [("A-ACCOUNT-ONLY-1", ""), ("A-ACCOUNT-ONLY-2", "A 原备注")],
        )
        self.assertEqual(
            self._rows_in(self.tenant_b), [("B-ACCOUNT-ONLY-1", "")]
        )

    def test_positive_control_each_tenant_can_edit_its_own_row(self):
        """非空哨兵：两个租户各自改自己那行都能成功（否则上面的 1002 可能只是接口坏了）。"""
        pk_a, pk_b, client_a, client_b = self._seed()

        try:
            resp_a = client_a.patch(
                detail_url(pk_a),
                {"remark": "A 自己改"},
                content_type="application/json",
                SERVER_NAME=DOMAIN_A,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp_a.data["code"], 0, resp_a.data)

        try:
            resp_b = client_b.patch(
                detail_url(pk_b),
                {"remark": "B 自己改"},
                content_type="application/json",
                SERVER_NAME=DOMAIN_B,
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(resp_b.data["code"], 0, resp_b.data)

        self.assertEqual(
            self._rows_in(self.tenant),
            [("A-ACCOUNT-ONLY-1", ""), ("A-ACCOUNT-ONLY-2", "A 自己改")],
        )
        self.assertEqual(
            self._rows_in(self.tenant_b), [("B-ACCOUNT-ONLY-1", "B 自己改")]
        )
