import threading
from datetime import timedelta
from unittest import mock

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.wecom.group_qr import cleanup_due, get_or_create_qr
from apps.wecom.models import WecomAppConfig, WecomGroupChat, WecomGroupQr


class GroupQrConcurrencyTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="qr_concurrency")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain="qr-concurrency.test.com")
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        self.assertTrue(connection.get_autocommit())
        WecomAppConfig.objects.create(tenant_schema=self.tenant.schema_name, enabled=True,
                                      corp_id="synth-corp", agent_id=1, secret="synth-secret")
        self.group = WecomGroupChat.objects.create(chat_id="synth-chat", name="合成群 123456")

    def launch(self, work):
        result = {}
        def wrapped():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    with connection.cursor() as cur:
                        cur.execute("SELECT pg_backend_pid()")
                        result["pid"] = cur.fetchone()[0]
                    result["value"] = work()
            except Exception as exc:
                result["error"] = exc
            finally:
                connections.close_all()
        thread = threading.Thread(target=wrapped)
        thread.start()
        return thread, result

    def finish(self, thread, result):
        thread.join(10)
        self.assertFalse(thread.is_alive(), "并发线程没有结束")
        if "error" in result:
            raise result["error"]
        return result["value"]

    def test_two_connections_only_create_once(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def remote(config, endpoint, data):
            calls.append(endpoint)
            if endpoint == "add_join_way":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("并发窗口未释放")
                return {"errcode": 0, "config_id": "cfg"}
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/qr"}}
        with mock.patch("apps.wecom.group_qr._verify_group", return_value=True), mock.patch("apps.wecom.group_qr._call", side_effect=remote):
            a, ar = self.launch(lambda: get_or_create_qr(WecomGroupChat.objects.get(pk=self.group.pk)))
            self.assertTrue(entered.wait(10), "第一条创建没有抵达企微调用")
            b, br = self.launch(lambda: get_or_create_qr(WecomGroupChat.objects.get(pk=self.group.pk)))
            try:
                self.assertEqual(self.finish(b, br)["status"], "pending")
            finally:
                release.set()
            self.assertEqual(self.finish(a, ar)["status"], "ready")
        self.assertNotEqual(ar["pid"], br["pid"])
        self.assertEqual(calls.count("add_join_way"), 1)
        self.assertEqual(WecomGroupQr.objects.get().config_id, "cfg")

    def test_view_during_remote_cleanup_waits_then_generates_new_code(self):
        WecomGroupQr.objects.create(group=self.group, config_id="old", corp_id="synth-corp", agent_id=1,
                                    state="ready", last_viewed_at=timezone.now() - timedelta(days=2))
        entered, release = threading.Event(), threading.Event()
        def remote(config, endpoint, data):
            if endpoint == "del_join_way":
                entered.set()
                if not release.wait(10):
                    raise AssertionError("并发窗口未释放")
                return {"errcode": 0}
            if endpoint == "add_join_way":
                return {"errcode": 0, "config_id": "new"}
            return {"errcode": 0, "join_way": {"qr_code": "https://example.invalid/new"}}
        with mock.patch("apps.wecom.group_qr._verify_group", return_value=True), mock.patch("apps.wecom.group_qr._call", side_effect=remote):
            a, ar = self.launch(cleanup_due)
            self.assertTrue(entered.wait(10), "清理没有抵达企微调用")
            b, br = self.launch(lambda: get_or_create_qr(WecomGroupChat.objects.get(pk=self.group.pk)))
            try:
                self.assertEqual(self.finish(b, br)["status"], "pending")
            finally:
                release.set()
            self.finish(a, ar)
            self.assertEqual(get_or_create_qr(self.group)["qr_url"], "https://example.invalid/new")
        self.assertNotEqual(ar["pid"], br["pid"])
        self.assertEqual(WecomGroupQr.objects.get().config_id, "new")
