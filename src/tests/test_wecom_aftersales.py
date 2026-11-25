from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.internal_chat.models import Message
from apps.platform.provisioning import provision_tenant
from apps.wecom import group_sync, refund_notices
from apps.wecom.archive import ArchiveError
from apps.wecom.models import (
    WecomAppConfig, WecomArchiveMessage, WecomArchiveOrderGroup, WecomGroupChat, WecomRefundNotice, WecomZoneKeywordHit,
)
from tests.p5_auth_helpers import make_logged_in_client


class AftersalesSettingsTests(TenantTestCase):
    settings_url = "/api/wecom/aftersales/settings/"
    options_url = "/api/wecom/aftersales/options/"

    @classmethod
    def get_test_schema_name(cls):
        return "kexing"

    def setUp(self):
        self.enterContext(self.settings(
            WECOM_ARCHIVE_TENANT_SCHEMA="kexing", WECOM_ARCHIVE_PROGRAM_ID="synth-aftersales-program",
            WECOM_ZONE_ENABLED=True, TT_CHAT_ORDER_GROUPS_ENABLED=True, TT_CHAT_CORE_BACKEND="legacy",
            WECOM_REFUND_NOTICE_RULE_ID="", WECOM_REFUND_NOTICE_CHAT_ID="", WECOM_REFUND_NOTICE_START_AT="",
        ))
        self.config = WecomAppConfig.objects.create(
            tenant_schema="kexing", enabled=True, corp_id="synth-aftersales-corp", agent_id=19,
            secret="synth-aftersales-secret", identity_revision=7,
        )
        self.admin = make_logged_in_client(
            username="synth-aftersales-admin", groups=("企微管理",), nodes=("system.wecom_settings",),
        )
        self.staff = make_logged_in_client(username="synth-aftersales-other")
        self.user = get_user_model().objects.create(username="synth-aftersales-user", first_name="合成售后员工")
        self.group = WecomGroupChat.objects.create(chat_id="synth-aftersales-group", name="合成客户群", identity_revision=7)
        self.values = {
            "enabled": True, "keywords": ["退款", "换打手"], "group_ids": [self.group.chat_id],
            "order_recipient": "publisher", "fixed_recipient_ids": [self.user.pk],
        }

    def _put(self, client=None, **values):
        return (client or self.admin).put(self.settings_url, {**self.values, **values}, content_type="application/json")

    def test_defaults_are_disabled_and_never_call_wecom(self):
        with patch("apps.wecom.zone_client.call_zone_operation") as remote:
            response = self.admin.get(self.settings_url)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["enabled"])
        self.assertEqual(data["status"], "disabled")
        self.assertEqual(data["keywords"], data["default_keywords"])
        self.assertTrue({"退款", "退单", "换打手", "不想打了"} <= set(data["keywords"]))
        self.assertEqual(len(data["keywords"]), 16)
        self.assertTrue({"打错了", "掉段了"}.isdisjoint(data["keywords"]))
        remote.assert_not_called()

    def test_all_groups_is_default_without_selecting_group_ids(self):
        draft = self.admin.get(self.settings_url).json()["data"]
        self.assertEqual(draft["group_ids"], [])
        missing_scope = self.admin.put(self.settings_url, {
            "enabled": True, "keywords": draft["keywords"], "order_recipient": draft["order_recipient"],
            "fixed_recipient_ids": draft["fixed_recipient_ids"],
        }, content_type="application/json")
        self.assertIsNone(missing_scope.json()["data"])
        response = self._put(group_ids=draft["group_ids"], keywords=draft["keywords"])
        self.assertEqual(response.json()["code"], 0)
        saved = response.json()["data"]
        self.assertEqual(saved["group_ids"], [])
        self.assertEqual(saved["status"], "pending")
        self.assertIs(draft["legacy_pilot"], False)

    def test_legacy_pilot_is_labelled_while_the_settings_draft_covers_all_groups(self):
        with self.settings(
            WECOM_REFUND_NOTICE_RULE_ID="synth-legacy-rule", WECOM_REFUND_NOTICE_CHAT_ID=self.group.chat_id,
            WECOM_REFUND_NOTICE_START_AT=timezone.now().isoformat(),
        ), patch("apps.wecom.zone_client.call_zone_operation") as remote:
            data = self.admin.get(self.settings_url).json()["data"]
            self.assertTrue(data["legacy_pilot"])
            self.assertTrue(data["enabled"])
            self.assertEqual(data["status"], "synced")
            self.assertEqual(data["keywords"], data["default_keywords"])
            self.assertEqual(data["group_ids"], [])
            self.assertEqual(data["group_options"], [])
            saved = self._put(group_ids=[], keywords=data["keywords"]).json()["data"]
            self.assertFalse(saved["legacy_pilot"])
            self.assertEqual(saved["status"], "pending")
        remote.assert_not_called()

    def test_settings_persist_and_return_selected_names(self):
        response = self._put(keywords=[" 退款 ", "退款", "换打手"], fixed_recipient_ids=[self.user.pk] * 2)
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["keywords"], ["退款", "换打手"])
        self.assertEqual(data["fixed_recipient_ids"], [self.user.pk])
        self.assertEqual(data["group_options"], [{
            "id": self.group.chat_id, "name": self.group.name, "detail": self.group.chat_id,
        }])
        self.assertEqual(data["fixed_recipient_options"], [{
            "id": self.user.pk, "name": self.user.first_name, "detail": self.user.username,
        }])
        self.assertEqual(data["status"], "pending")
        self.assertEqual(self.admin.get(self.settings_url).json()["data"], data)
        audit = OperationLog.objects.get(action="system.wecom_config_update", field_name="售后纠纷通知")
        self.assertEqual(audit.actor_id, int(self.admin.session["_auth_user_id"]))
        self.assertEqual(audit.target_id, str(self.config.pk))

    def test_scope_and_permissions_reject_invalid_settings(self):
        self.assertEqual(self._put(self.staff).status_code, 403)
        self.assertEqual(self.staff.get(self.options_url, {"kind": "user"}).status_code, 403)
        old_group = WecomGroupChat.objects.create(chat_id="synth-old-identity", name="合成旧群", identity_revision=2)
        self.assertIsNone(self._put(group_ids=[old_group.chat_id]).json()["data"])
        self.assertIsNone(self._put(group_ids=[""]).json()["data"])
        self.assertIsNone(self._put(keywords=["超过长度" * 10]).json()["data"])
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertIsNone(self._put().json()["data"])
        data = self.admin.get(self.options_url, {"kind": "group", "q": "合成"}).json()["data"]
        self.assertEqual(data, {"items": [{
            "id": self.group.chat_id, "name": self.group.name, "detail": self.group.chat_id,
        }], "has_more": False})

    def test_option_search_excludes_new_placeholder_and_distinguishes_equal_names(self):
        placeholder = WecomGroupChat.objects.create(chat_id="synth-not-synced", identity_revision=7, pending=True)
        known = WecomGroupChat.objects.create(
            chat_id="synth-aftersales-reload", name=self.group.name, details={"name": self.group.name},
            identity_revision=7, pending=True,
        )
        one = self.admin.get(self.options_url, {"kind": "group"}).json()["data"]["items"]
        self.assertEqual({row["detail"] for row in one}, {self.group.chat_id, known.chat_id})
        self.assertIsNone(self._put(group_ids=[placeholder.chat_id]).json()["data"])
        self.assertEqual(self._put(group_ids=[known.chat_id]).json()["data"]["status"], "pending")
        same_name = get_user_model().objects.create(username="synth-other-nickname", first_name=self.user.first_name)
        two = self.admin.get(self.options_url, {"kind": "user", "q": self.user.first_name}).json()["data"]["items"]
        self.assertEqual({row["detail"] for row in two}, {self.user.username, same_name.username})

    def test_settings_cannot_be_enabled_from_a_different_tenant(self):
        self._put()
        other, domain = provision_tenant(schema_name="synth_aftersales_b", name="合成未试点店", domain="aftersales-b.test.com")
        with self.settings(ALLOWED_HOSTS=["tenant.test.com", domain.domain]), tenant_context(other):
            admin = make_logged_in_client(domain=domain.domain, username="synth-aftersales-admin-b", is_superuser=True)
            settings = admin.get(self.settings_url).json()["data"]
            self.assertFalse(settings["available"])
            self.assertFalse(settings["enabled"])
            self.assertEqual(settings["group_ids"], [])
            self.assertIsNone(self._put(admin, group_ids=[]).json()["data"])
        self.assertEqual(self.admin.get(self.settings_url).json()["data"]["group_ids"], [self.group.chat_id])

    def test_sync_uses_a_dedicated_rule_and_recovers_a_lost_create_result(self):
        rules = {"synth-manual-rule": {"name": "合成原测试规则"}}
        now = timezone.now()
        self._put()

        def wecom(config, operation, params, **kwargs):
            if operation == "get_rule_list":
                return {"rule_list": [{"rule_id": key, "name": rule["name"]} for key, rule in rules.items()], "has_more": False}
            if operation == "create_rule":
                rules["synth-managed-rule"] = params
                raise ArchiveError("合成结果丢失")
            if operation == "get_rule_detail":
                return rules[params["rule_id"]]
            self.fail(operation)

        with patch("apps.wecom.zone_client.call_zone_operation", side_effect=wecom) as remote:
            refund_notices.run_due(now=now)
            self.assertEqual(self.admin.get(self.settings_url).json()["data"]["status"], "error")
            refund_notices.run_due(now=now + timedelta(minutes=6))
        self.assertEqual(self.admin.get(self.settings_url).json()["data"]["status"], "synced")
        self.assertEqual([call.args[1] for call in remote.call_args_list], [
            "get_rule_list", "create_rule", "get_rule_list", "get_rule_detail",
        ])
        managed = rules["synth-managed-rule"]
        self.assertNotEqual(managed["name"], rules["synth-manual-rule"]["name"])
        self.assertEqual(managed["keyword"], {"word_list": ["退款", "换打手"], "match_rule": 0})
        self.assertEqual(managed["applicable_range"], {
            "chat": {"id_list": [self.group.chat_id]}, "chat_type": {"type_list": [2]},
            "session_type": {"session_type_list": [2]}, "msg_scope": 3,
        })

    def test_sync_scope_drift_stays_off_and_settings_can_be_disabled(self):
        self._put()
        now = timezone.now()
        with patch("apps.wecom.zone_client.call_zone_operation", side_effect=[
            {"rule_list": [], "has_more": False}, {"rule_id": "synth-managed-rule"},
            {"name": "合成错误规则", "keyword": {"word_list": ["退款"]}, "applicable_range": {}},
        ]):
            refund_notices.run_due(now=now)
        self.assertEqual(self.admin.get(self.settings_url).json()["data"]["status"], "error")
        with patch("apps.wecom.zone_client.call_zone_operation") as remote:
            self.assertEqual(self._put(enabled=False).json()["data"]["status"], "disabled")
            refund_notices.run_due(now=now + timedelta(minutes=6))
            remote.assert_not_called()

    def test_updates_only_the_managed_rule_and_confirms_new_scope(self):
        old_scope = self.group.chat_id
        new_scope = WecomGroupChat.objects.create(
            chat_id="synth-new-scope", name="合成新范围群", identity_revision=7,
        ).chat_id
        rules = {"synth-manual-rule": {"name": "合成原试用规则"}}
        now = timezone.now()
        self._put(group_ids=[old_scope])

        def wecom(config, operation, params, **kwargs):
            if operation == "get_rule_list":
                return {"rule_list": [{"rule_id": key, "name": rule["name"]} for key, rule in rules.items()], "has_more": False}
            if operation == "create_rule":
                rules["synth-managed-rule"] = params
                return {"rule_id": "synth-managed-rule"}
            if operation == "get_rule_detail":
                return rules[params["rule_id"]]
            if operation == "update_rule":
                rules[params["rule_id"]] = params
                return {}
            self.fail(operation)

        with patch("apps.wecom.zone_client.call_zone_operation", side_effect=wecom) as remote:
            refund_notices.run_due(now=now)
            self.assertEqual(self._put(keywords=["退单"], group_ids=[new_scope]).json()["data"]["status"], "pending")
            refund_notices.run_due(now=now + timedelta(minutes=1))
            self.assertEqual(self._put(keywords=["退单"], group_ids=[new_scope], order_recipient="sales").json()["data"][
                "status"
            ], "synced")
            refund_notices.run_due(now=now + timedelta(minutes=2))
        self.assertEqual([call.args[1] for call in remote.call_args_list].count("create_rule"), 1)
        self.assertEqual([call.args[2]["rule_id"] for call in remote.call_args_list if call.args[1] == "update_rule"],
                         ["synth-managed-rule"])
        self.assertEqual(rules["synth-managed-rule"]["applicable_range"]["chat"]["id_list"], [new_scope])
        self.assertEqual(rules["synth-managed-rule"]["keyword"]["word_list"], ["退单"])
        self.assertEqual(rules["synth-manual-rule"], {"name": "合成原试用规则"})

    def test_all_groups_sync_clears_the_old_group_list_and_rejects_remaining_restrictions(self):
        self._put()
        now = timezone.now()
        rule = {}
        updates = []

        def wecom(config, operation, params, **kwargs):
            if operation == "get_rule_list":
                entries = [{"rule_id": "synth-managed-rule", "name": rule["name"]}] if rule else []
                return {"rule_list": entries, "has_more": False}
            if operation == "create_rule":
                rule.update(params)
                return {"rule_id": "synth-managed-rule"}
            if operation == "get_rule_detail":
                return rule
            if operation == "update_rule":
                updates.append(params["applicable_range"])
                if len(updates) == 2:
                    rule["applicable_range"] = {**updates[-1], "department": {"id_list": [19]}}
                elif len(updates) == 3:
                    rule["applicable_range"] = {key: value for key, value in updates[-1].items() if key != "chat"}
                return {}
            self.fail(operation)

        with patch("apps.wecom.zone_client.call_zone_operation", side_effect=wecom):
            refund_notices.run_due(now=now)
            self.assertEqual(self.admin.get(self.settings_url).json()["data"]["status"], "synced")
            self.assertEqual(self._put(group_ids=[]).json()["data"]["status"], "pending")
            for minutes, status in ((1, "error"), (7, "error"), (13, "synced")):
                refund_notices.run_due(now=now + timedelta(minutes=minutes))
                self.assertEqual(self.admin.get(self.settings_url).json()["data"]["status"], status)
        self.assertEqual(updates, [{
            "chat": {"id_list": []}, "chat_type": {"type_list": [2]},
            "session_type": {"session_type_list": [2]}, "msg_scope": 3,
        }] * 3)

    def _sync_policy(self, now, **values):
        self.assertEqual(self._put(**values).json()["data"]["status"], "pending")
        rule = {}

        def wecom(config, operation, params, **kwargs):
            if operation == "get_rule_list":
                return {"rule_list": [], "has_more": False}
            if operation == "create_rule":
                rule.update(params)
                return {"rule_id": "synth-managed-rule"}
            if operation == "get_rule_detail":
                return {**rule, "keyword": {"word_list": rule["keyword"]["word_list"]}}
            self.fail(operation)

        with patch("apps.wecom.zone_client.call_zone_operation", side_effect=wecom):
            refund_notices.run_due(now=now)
        self.assertEqual(self.admin.get(self.settings_url).json()["data"]["status"], "synced")
        return rule

    def _order_staff(self, username, *, games):
        client = make_logged_in_client(username=username, groups=("测试客服",), nodes=("order.view_detail",), games=games)
        return get_user_model().objects.get(pk=client.session["_auth_user_id"])

    def _hit(self, message_id, group, sent_at, rule_id="synth-managed-rule"):
        WecomArchiveMessage.objects.create(
            identity_revision=7, msg_id=message_id, chat_id=group.chat_id, sent_at=sent_at,
            encrypted_secret_key="synth-aftersales-key", public_key_version=1,
        )
        return WecomZoneKeywordHit.objects.create(
            identity_revision=7, program_id="synth-aftersales-program", msg_id=message_id,
            rule_id=rule_id, has_hit_keyword=True,
        )

    def _record_order_creation(self, order, creator, *, action="order.create", target_id=None, order_no=None):
        record(
            log_type=ACTION_LOG_TYPES[action], action=action,
            actor=creator.get_username(), actor_id=creator.pk, target_kind="order",
            target_id=target_id or order.order_no, order_no=order_no or order.order_no,
        )

    def test_all_groups_notifies_existing_and_new_customer_groups(self):
        game = Game.objects.create(name="合成全群游戏")
        publisher = self._order_staff("synth-all-groups-publisher", games=[game])
        old_order, new_order, archive_order = [
            Order.objects.create(game=game, order_no=no, hall_publisher=publisher)
            for no in ("20261122000000003907", "20261122000000003908", "20261122000000003909")
        ]
        self.group.name = old_order.order_no
        self.group.save(update_fields=["name"])
        rule = self._sync_policy(timezone.now(), group_ids=[], fixed_recipient_ids=[])
        self.assertEqual(rule["applicable_range"], {
            "chat_type": {"type_list": [2]}, "session_type": {"session_type_list": [2]}, "msg_scope": 3,
        })
        new_group_id = "synth-after-enabling"
        group_sync.queue_event({
            "MsgType": "event", "Event": "change_external_chat", "ChangeType": "create", "ChatId": new_group_id,
        }, identity_revision=self.config.identity_revision)
        new_group = WecomGroupChat.objects.get(chat_id=new_group_id)
        archive_only = WecomGroupChat(chat_id="synth-authorized-without-group-record")
        for group, order in ((new_group, new_order), (archive_only, archive_order)):
            WecomArchiveOrderGroup.objects.create(
                config=self.config, identity_revision=7, program_id="synth-aftersales-program", chat_id=group.chat_id,
                order=order, matched_order_no=order.order_no,
            )
        now = timezone.now()
        old_hit = self._hit("synth-all-groups-old-hit", self.group, now)
        new_hit = self._hit("synth-all-groups-new-hit", new_group, now)
        archive_hit = self._hit("synth-all-groups-archive-hit", archive_only, now)
        old_identity = WecomGroupChat.objects.create(
            chat_id="synth-previous-enterprise", name=old_order.order_no, identity_revision=2,
        )
        unmatched = self._hit("synth-stale-name-hit", old_identity, now)
        self._hit("synth-other-rule-hit", self.group, now, rule_id="synth-legacy-rule")
        forbidden_groups = [
            WecomGroupChat(chat_id=""),
            WecomGroupChat.objects.create(chat_id="synth-dismissed", name=old_order.order_no, identity_revision=7, dismissed=True),
            WecomGroupChat.objects.create(chat_id="synth-dismiss-pending", name=old_order.order_no,
                                          identity_revision=7, pending_dismissal=True),
        ]
        for index, group in enumerate(forbidden_groups):
            self._hit(f"synth-outside-eligible-{index}", group, now)

        def summarize(config, operation, params, **kwargs):
            if operation == "create_summary_task":
                return {"jobid": params["msg_list"][-1]["msgid"]}
            if operation == "get_summary_result":
                return {"status": 1, "response_data": "合成全群摘要。"}
            self.fail(operation)

        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=summarize),
            patch("apps.wecom.zone_client.call_zone_operation") as remote,
        ):
            refund_notices.run_due(now=now)
            self.assertFalse(Message.objects.exists())
            refund_notices.run_due(now=now + timedelta(minutes=1))
            remote.assert_not_called()
        self.assertCountEqual(WecomRefundNotice.objects.values_list("hit_id", flat=True),
                              [old_hit.pk, new_hit.pk, archive_hit.pk, unmatched.pk])
        self.assertEqual(WecomRefundNotice.objects.get(hit=unmatched).problem, "unmatched")
        self.assertCountEqual(Message.objects.values_list("system_order_no", flat=True),
                              [old_order.order_no, new_order.order_no, archive_order.order_no])

    def test_sends_one_order_notice_to_role_and_each_permitted_fixed_recipient(self):
        game = Game.objects.create(name="合成售后游戏")
        creator = self._order_staff("synth-aftersales-creator", games=[game])
        publisher = self._order_staff("synth-aftersales-publisher", games=[game])
        fixed = self._order_staff("synth-aftersales-fixed", games=[game])
        no_game = self._order_staff("synth-aftersales-no-game", games=[])
        order = Order.objects.create(
            game=game, order_no="20261122000000003901", created_by=creator.username, hall_publisher=publisher,
        )
        self._record_order_creation(order, creator)
        self.group.name = order.order_no
        self.group.save(update_fields=["name"])
        group_two = WecomGroupChat.objects.create(chat_id="synth-aftersales-group-b", name="另一个配置群", identity_revision=7)
        WecomGroupChat.objects.create(chat_id="synth-aftersales-group-c", name="非配置群", identity_revision=7)
        now = timezone.now()
        self._sync_policy(now, group_ids=[self.group.chat_id, group_two.chat_id], order_recipient="creator",
                          fixed_recipient_ids=[creator.pk, fixed.pk, no_game.pk])
        now = timezone.now()
        hit = self._hit("synth-aftersales-hit", self.group, now)
        self._hit("synth-aftersales-manual", self.group, now, "synth-manual-rule")
        self._hit("synth-aftersales-before", self.group, now - timedelta(minutes=10))
        self._hit("synth-aftersales-outside", WecomGroupChat.objects.get(chat_id="synth-aftersales-group-c"), now)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-summary-job"}, {"status": 1, "response_data": "合成售后摘要。"},
            ]),
        ):
            refund_notices.run_due(now=now)
            self.assertFalse(Message.objects.exists())
            refund_notices.run_due(now=now + timedelta(minutes=1))
            refund_notices.run_due(now=now + timedelta(minutes=2))
        delivered = list(Message.objects.order_by("conversation__system_recipient_id"))
        self.assertEqual([message.conversation.system_recipient_id for message in delivered], [creator.pk, fixed.pk])
        self.assertTrue(all(message.system_order_no == order.order_no for message in delivered))
        self.assertTrue(all("售后纠纷通知规则" in message.text and "合成售后摘要。" in message.text for message in delivered))
        notice = WecomRefundNotice.objects.get()
        self.assertEqual(notice.hit, hit)
        self.assertEqual(notice.delivered_recipient_ids, [creator.pk, fixed.pk])

    def test_creator_remains_the_audited_account_after_its_old_username_is_reused(self):
        game = Game.objects.create(name="合成售后游戏")
        creator = self._order_staff("synth-original-name", games=[game])
        old_username = creator.username
        order = Order.objects.create(game=game, order_no="20261122000000003904", created_by=old_username)
        self._record_order_creation(order, creator, action="order.copy")
        creator.username = "synth-renamed-account"
        creator.save(update_fields=["username"])
        reused = self._order_staff(old_username, games=[game])
        self.assertNotEqual(reused.pk, creator.pk)
        self.group.name = order.order_no
        self.group.save(update_fields=["name"])
        self._sync_policy(timezone.now(), order_recipient="creator", fixed_recipient_ids=[])
        now = timezone.now()
        self._hit("synth-reused-name-hit", self.group, now)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-job"}, {"status": 1, "response_data": "合成改名摘要。"},
            ]),
        ):
            refund_notices.run_due(now=now)
            refund_notices.run_due(now=now + timedelta(minutes=1))
        self.assertEqual(Message.objects.get().conversation.system_recipient_id, creator.pk)

    def test_accounts_without_a_matching_creation_audit_do_not_receive_the_creator_notice(self):
        game = Game.objects.create(name="合成售后游戏")
        username_match = self._order_staff("synth-unaudited-creator", games=[game])
        fixed = self._order_staff("synth-unaudited-fixed", games=[game])
        order = Order.objects.create(game=game, order_no="20261122000000003905", created_by=username_match.username)
        self._record_order_creation(order, username_match, action="order.update")
        self._record_order_creation(order, username_match, target_id="20261122000000003999")
        self._record_order_creation(order, username_match, order_no="20261122000000003999")
        self.group.name = order.order_no
        self.group.save(update_fields=["name"])
        self._sync_policy(timezone.now(), order_recipient="creator", fixed_recipient_ids=[fixed.pk])
        now = timezone.now()
        self._hit("synth-no-creator-audit-hit", self.group, now)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-job"}, {"status": 1, "response_data": "合成未确认录单人摘要。"},
            ]),
        ):
            refund_notices.run_due(now=now)
            refund_notices.run_due(now=now + timedelta(minutes=1))
        self.assertEqual(Message.objects.get().conversation.system_recipient_id, fixed.pk)

    def test_declining_scope_blocks_a_notice_that_is_waiting_for_its_summary(self):
        game = Game.objects.create(name="合成售后游戏")
        publisher = self._order_staff("synth-inflight-publisher", games=[game])
        order = Order.objects.create(game=game, order_no="20261122000000003902", hall_publisher=publisher)
        self.group.name = order.order_no
        self.group.save(update_fields=["name"])
        now = timezone.now()
        self._sync_policy(now, fixed_recipient_ids=[])
        now = timezone.now()
        hit = self._hit("synth-inflight", self.group, now)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", return_value={"jobid": "synth-waiting-job"}) as model,
        ):
            refund_notices.run_due(now=now)
        model.assert_called_once()
        receipt = WecomRefundNotice.objects.get(hit=hit)
        self.assertEqual(receipt.summary_job_id, "synth-waiting-job")
        self._put(enabled=False, fixed_recipient_ids=[])
        with (
            patch.object(refund_notices, "call_zone_operation") as model,
            self.settings(
                WECOM_REFUND_NOTICE_RULE_ID="synth-legacy-rule", WECOM_REFUND_NOTICE_CHAT_ID=self.group.chat_id,
                WECOM_REFUND_NOTICE_START_AT=(now - timedelta(minutes=5)).isoformat(),
            ),
        ):
            self._hit("synth-legacy-hit", self.group, now, "synth-legacy-rule")
            refund_notices.run_due(now=now + timedelta(minutes=1))
            expiry = now + timedelta(hours=2, minutes=1)
            refund_notices.run_due(now=expiry)
        model.assert_not_called()
        self.assertFalse(Message.objects.exists())
        self.assertEqual(WecomRefundNotice.objects.count(), 1)
        receipt.refresh_from_db()
        self.assertEqual(receipt.closed_at, expiry)
        self.assertEqual(receipt.problem, "expired")

    def test_publisher_role_uses_the_current_publisher_when_the_summary_returns(self):
        game = Game.objects.create(name="合成售后游戏")
        before = self._order_staff("synth-previous-publisher", games=[game])
        current = self._order_staff("synth-current-publisher", games=[game])
        order = Order.objects.create(game=game, order_no="20261122000000003903", hall_publisher=before)
        self.group.name = order.order_no
        self.group.save(update_fields=["name"])
        self._sync_policy(timezone.now(), fixed_recipient_ids=[])
        now = timezone.now()
        hit = self._hit("synth-publisher-hit", self.group, now)
        with (
            patch.object(refund_notices, "decrypt_message_key", return_value="synth-key"),
            patch.object(refund_notices, "call_zone_operation", side_effect=[
                {"jobid": "synth-job"}, {"status": 1, "response_data": "合成发单人摘要。"},
            ]),
        ):
            refund_notices.run_due(now=now)
            order.hall_publisher = current
            order.save(update_fields=["hall_publisher"])
            refund_notices.run_due(now=now + timedelta(minutes=1))
        self.assertEqual(Message.objects.get().conversation.system_recipient_id, current.pk)
        self.assertEqual(WecomRefundNotice.objects.get(hit=hit).delivered_recipient_ids, [current.pk])
