"""系统内部公告（2026-10-17 实装）的接口与派生逻辑判据。

关键口径：
- 标题**由服务端从内容派生**（首行截 50），请求体只收 `content`；
- 读列表 = 登录即可（主页「通知和公告」要读）；写 = `system.publish_notice`；
- 删除是硬删，没有软删 / 已读态（与对标一致）。
"""
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business.api.notice_views import derive_title
from apps.business.models import SystemNotice
from tests.p5_auth_helpers import make_logged_in_client


class DeriveTitleTests(TenantTestCase):
    def test_first_line_truncated_to_50(self):
        self.assertEqual(derive_title("今晚维护\n预计一小时"), "今晚维护")
        self.assertEqual(derive_title("  前后空格  \n第二行"), "前后空格")
        self.assertEqual(derive_title("x" * 80), "x" * 50)
        self.assertEqual(derive_title("   "), "（无标题公告）")


class SystemNoticeApiTests(TenantTestCase):
    def setUp(self):
        self.editor = make_logged_in_client(
            username="notice-editor", groups=("客服",), nodes=("system.publish_notice",)
        )
        self.viewer = make_logged_in_client(username="notice-viewer")

    def test_create_derives_title_and_uses_authenticated_user(self):
        body = self.editor.post(
            "/api/business/system-notices/",
            {"content": "今晚 23:00 维护\n预计一小时"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["title"], "今晚 23:00 维护")
        self.assertEqual(body["data"]["created_by"], "notice-editor")
        row = SystemNotice.objects.get()
        self.assertEqual(row.content, "今晚 23:00 维护\n预计一小时")

    def test_read_is_open_to_any_logged_in_user(self):
        SystemNotice.objects.create(title="已有公告", content="已有公告\n正文")
        body = self.viewer.get("/api/business/system-notices/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["title"], "已有公告")

    def test_write_without_node_is_403(self):
        response = self.viewer.post(
            "/api/business/system-notices/",
            {"content": "偷偷发一条"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(SystemNotice.objects.exists())

    def test_edit_only_changes_content_and_rederives_title(self):
        row = SystemNotice.objects.create(title="旧标题", content="旧标题\n旧正文")
        body = self.editor.patch(
            f"/api/business/system-notices/{row.id}/",
            {"content": "新标题\n新正文"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.title, "新标题")
        self.assertEqual(row.content, "新标题\n新正文")

    def test_empty_content_is_1000(self):
        body = self.editor.post(
            "/api/business/system-notices/",
            {"content": "   "},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("请输入公告内容", body["msg"])

    def test_delete_is_hard_delete(self):
        row = SystemNotice.objects.create(title="要删", content="要删")
        body = self.editor.delete(f"/api/business/system-notices/{row.id}/").json()
        self.assertEqual(body["code"], 0)
        self.assertFalse(SystemNotice.objects.filter(pk=row.pk).exists())

    def test_list_ordered_newest_first(self):
        SystemNotice.objects.create(title="第一条", content="第一条")
        SystemNotice.objects.create(title="第二条", content="第二条")
        items = self.viewer.get("/api/business/system-notices/").json()["data"]["items"]
        self.assertEqual([i["title"] for i in items], ["第二条", "第一条"])

    def test_created_by_survives_editor_deletion(self):
        """`created_by` 是用户名快照、不是 FK：删账号不带走公告。"""
        User.objects.create_user(username="离职员工", password="pw")
        row = SystemNotice.objects.create(
            title="历史公告", content="历史公告", created_by="离职员工"
        )
        User.objects.filter(username="离职员工").delete()
        row.refresh_from_db()
        self.assertEqual(row.created_by, "离职员工")
