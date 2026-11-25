"""通讯录 / 客户联系 / 台账 proxy 端点的判据（计划 §3：`test_wecom_proxy.py`）。

## 判据打在哪

- **参数从调用方载荷取**（E-0176）：序列化器校验 query 参数，企微侧收到的参数值
  就是调用方传的那个（stub 记录的 URL 逐字断言）；
- **企微错误码翻译**（E-0010 / E-0087）：非零 errcode ⇒ 业务码 1040 + 人话，⛔ 裸 500；
- **未配置 ⇒ 业务码 1039**（HTTP 200，页面按码分支）；
- **客户联系面走 contacts 客户端**：mock 打在被查找模块（`apps.wecom.sdk.get_contacts_client`，
  E-0029），断言主客户端**没被**用在客户联系端点上；contacts_secret **选填**
  （为空 ⇒ 门不拦、照样走 contacts 客户端，secret 的回退在 sdk 层）；
- **台账读**：倒序分页 50/页（终态 = 返回的行序与 total）。

零真实网络：`WecomApiStub` 拦 `requests.Session`（token 轮与业务轮按序排队）。
"""
from __future__ import annotations

import json
from unittest import mock

from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.wecom.models import WecomAppConfig, WecomInboundMessage
from tests._wecom_helpers import (
    AES_KEY,
    AGENT_ID,
    CORP_ID,
    CONTACTS_SECRET,
    SECRET,
    TOKEN,
    WecomApiStub,
)
from tests.p5_auth_helpers import make_logged_in_client

NODE = "system.wecom_settings"
DEPARTMENTS = "/api/wecom/contacts/departments/"
USERS = "/api/wecom/contacts/users/"
MESSAGES = "/api/wecom/messages/"
FOLLOW_USERS = "/api/wecom/external/follow_users/"
CUSTOMERS = "/api/wecom/external/customers/"
GROUPCHATS = "/api/wecom/external/groupchats/"


class _StubBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="wecom-proxy", groups=("客服",), nodes=(NODE,)
        )
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name,
            corp_id=CORP_ID,
            agent_id=AGENT_ID,
            secret=SECRET,
            contacts_secret=CONTACTS_SECRET,
            callback_token=TOKEN,
            encoding_aes_key=AES_KEY,
            enabled=True,
        )
        self.stub = WecomApiStub().install()
        self.addCleanup(self.stub.stop)
        # 每个 stub 用例先回一轮 token（wechatpy 取 token 再打业务端点）。
        self.stub.enqueue({"access_token": "stub-token-p", "expires_in": 7200})


class DepartmentsProxyTests(_StubBase):
    def test_department_list_proxies_the_caller_department_id(self):
        self.stub.enqueue({"errcode": 0, "department": [{"id": 2, "name": "客服部"}]})
        body = self.client.get(DEPARTMENTS, {"department_id": 2}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["departments"][0]["name"], "客服部")
        # E-0176：企微收到的参数 = 调用方载荷里的那个值。
        self.assertIn("department/list?id=2", self.stub.last_url())

    def test_department_list_without_param_calls_full_list(self):
        self.stub.enqueue({"errcode": 0, "department": []})
        body = self.client.get(DEPARTMENTS).json()
        self.assertEqual(body["code"], 0)
        self.assertIn("department/list", self.stub.last_url())
        self.assertNotIn("department_id=", self.stub.last_url())

    def test_remote_errcode_maps_to_1040_with_plain_message(self):
        self.stub.enqueue({"errcode": 60011, "errmsg": "no privilege"})
        body = self.client.get(DEPARTMENTS).json()
        self.assertEqual(body["code"], 1040)
        self.assertIn("60011", body["msg"])
        self.assertIn("权限", body["msg"])


class UsersProxyTests(_StubBase):
    def test_user_simplelist_proxies_the_caller_department_id(self):
        self.stub.enqueue(
            {"errcode": 0, "userlist": [{"userid": "u1", "name": "成员甲"}]}
        )
        body = self.client.get(USERS, {"department_id": 5}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["users"][0]["name"], "成员甲")
        self.assertIn("user/simplelist?department_id=5", self.stub.last_url())

    def test_missing_department_id_is_validation_error_not_500(self):
        body = self.client.get(USERS).json()
        self.assertEqual(body["code"], 1000)

    def test_non_numeric_department_id_is_validation_error_not_500(self):
        body = self.client.get(USERS, {"department_id": "abc"}).json()
        self.assertEqual(body["code"], 1000)


class NotConfiguredTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="wecom-proxy-nc", groups=("客服",), nodes=(NODE,)
        )

    def test_proxies_without_config_are_1039_not_500(self):
        # ⚠️ USERS 的 department_id 是必填（E-0010：参数校验先于业务闸）⇒ 带上合法参数
        # 才能走到「未配置」那一档。
        bodies = [
            self.client.get(DEPARTMENTS).json(),
            self.client.get(USERS, {"department_id": 1}).json(),
            self.client.get(FOLLOW_USERS).json(),
            self.client.get(CUSTOMERS, {"userid": "u1"}).json(),
            self.client.get(GROUPCHATS).json(),
        ]
        for body in bodies:
            self.assertEqual(body["code"], 1039)
        # 台账是本地表：没配置也照读（空页）。
        body = self.client.get(MESSAGES).json()
        self.assertEqual(body["code"], 0)


class _FakeContactsClient:
    """假客户联系客户端：只带 `external_contact.get_follow_user_list` 一个方法。"""

    class external_contact:  # noqa: N801 —— wechatpy 的属性名就是蛇形
        @staticmethod
        def get_follow_user_list():
            return {"errcode": 0, "follow_user": ["follow-user-synthetic-a"]}


class ContactsSecretIsolationTests(_StubBase):
    def test_contacts_endpoints_use_the_contacts_client_not_the_main_one(self):
        contacts = _FakeContactsClient()
        main = object()  # 主客户端被碰到任何方法都会 AttributeError ⇒ 1040，测试当场红
        with (
            mock.patch(
                "apps.wecom.sdk.get_contacts_client", return_value=contacts
            ) as m_contacts,
            mock.patch("apps.wecom.sdk.get_client", return_value=main) as m_main,
        ):
            body = self.client.get(FOLLOW_USERS).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["follow_users"], ["follow-user-synthetic-a"])
        # ⭐ 客户联系面走 contacts 客户端；主客户端没被碰到。
        m_contacts.assert_called_once()
        m_main.assert_not_called()


class ContactsSecretFallbackTests(TenantTestCase):
    """contacts_secret 选填：为空 ⇒ 客户联系面的门不再 1039，照样走 contacts 客户端
    （生效 secret 的回退判据在 sdk 层 = `test_wecom_config_api.ContactsClientSecretFallbackTests`）。"""

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(
            username="wecom-proxy-fallback", groups=("客服",), nodes=(NODE,)
        )
        WecomAppConfig.objects.create(
            tenant_schema=connection.schema_name,
            corp_id=CORP_ID,
            agent_id=AGENT_ID,
            secret=SECRET,
            contacts_secret="",  # ⭐ 空 = sdk 层回退主 secret
            callback_token=TOKEN,
            encoding_aes_key=AES_KEY,
            enabled=True,
        )

    def test_follow_users_with_blank_contacts_secret_still_probes(self):
        contacts = _FakeContactsClient()
        main = object()  # 主客户端被碰到任何方法都会 AttributeError ⇒ 1040，测试当场红
        with (
            mock.patch(
                "apps.wecom.sdk.get_contacts_client", return_value=contacts
            ) as m_contacts,
            mock.patch("apps.wecom.sdk.get_client", return_value=main) as m_main,
        ):
            body = self.client.get(FOLLOW_USERS).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["follow_users"], ["follow-user-synthetic-a"])
        m_contacts.assert_called_once()
        m_main.assert_not_called()
        # 传给 sdk 的就是「contacts_secret 为空」的那行配置（回退发生在 sdk 层）。
        self.assertEqual(m_contacts.call_args[0][0].contacts_secret, "")


class GroupChatsProxyTests(_StubBase):
    def test_groupchat_list_with_cursor(self):
        contacts = _FakeGroupChatStub()
        with mock.patch("apps.wecom.sdk.get_contacts_client", return_value=contacts):
            body = self.client.get(
                GROUPCHATS, {"cursor": "cursor-synthetic-1"}
            ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["next_cursor"], "cursor-synthetic-2")
        self.assertEqual(len(body["data"]["group_chat_list"]), 1)

    def test_groupchat_info_by_chat_id(self):
        contacts = _FakeGroupChatStub()
        with mock.patch("apps.wecom.sdk.get_contacts_client", return_value=contacts):
            body = self.client.get(GROUPCHATS, {"chat_id": "wr synthetic-chat"}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["group_chat"]["chat_id"], "wr synthetic-chat")


class _FakeGroupChatStub:
    """假客户联系客户端：list / info 两个方法的形状照 wechatpy 返回。"""

    class external_contact:  # noqa: N801 —— wechatpy 的属性名就是蛇形
        @staticmethod
        def get_group_chat_list(limit, status_filter=0, owner_filter=None, cursor=None):
            assert limit == 100
            return {
                "errcode": 0,
                "group_chat_list": [{"chat_id": "wr synthetic-chat"}],
                "next_cursor": "cursor-synthetic-2",
            }

        @staticmethod
        def get_group_chat_info(chat_id):
            return {"errcode": 0, "group_chat": {"chat_id": chat_id, "name": "客户群甲"}}


class CustomersProxyTests(_StubBase):
    def test_customers_list_then_batch_details(self):
        contacts = _FakeCustomersStub()
        with mock.patch("apps.wecom.sdk.get_contacts_client", return_value=contacts):
            body = self.client.get(CUSTOMERS, {"userid": "follow-user-synthetic-a"}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["external_userids"], ["external-synthetic-1"])
        self.assertEqual(body["data"]["contacts"][0]["external_contact"]["name"], "客户甲")

    def test_customers_requires_userid(self):
        body = self.client.get(CUSTOMERS).json()
        self.assertEqual(body["code"], 1000)


class _FakeCustomersStub:
    class external_contact:  # noqa: N801 —— wechatpy 的属性名就是蛇形
        @staticmethod
        def list(userid):
            assert userid == "follow-user-synthetic-a"
            return {"errcode": 0, "external_userid": ["external-synthetic-1"]}

        @staticmethod
        def batch_get_by_user(userid, cursor="", limit=50):
            return {
                "errcode": 0,
                "external_contact_list": [
                    {
                        "external_contact": {"name": "客户甲"},
                        "follow_info": {"userid": userid},
                    }
                ],
            }


class MessagesLedgerTests(_StubBase):
    def _insert_rows(self, count: int) -> None:
        for i in range(count):
            WecomInboundMessage.objects.create(
                msg_id=f"msg-ledger-{i:04d}",
                from_user_name=f"user-{i}",
                msg_type="text",
                content=f"内容{i}",
            )

    def test_ledger_reads_newest_first_with_pagination(self):
        self._insert_rows(3)
        body = self.client.get(MESSAGES, {"page": 1}).json()
        self.assertEqual(body["code"], 0)
        data = body["data"]
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["page_size"], 50)
        ids = [row["msg_id"] for row in data["items"]]
        self.assertEqual(ids, ["msg-ledger-0002", "msg-ledger-0001", "msg-ledger-0000"])
        # 台账行值逐条钉（E-0024）：字段值不变形。
        self.assertEqual(data["items"][0]["content"], "内容2")
        self.assertEqual(data["items"][0]["from_user_name"], "user-2")

    def test_ledger_page_beyond_total_is_empty_not_500(self):
        self._insert_rows(1)
        body = self.client.get(MESSAGES, {"page": 9}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total"], 1)

    def test_ledger_page_param_is_validated(self):
        body = self.client.get(MESSAGES, {"page": "zero"}).json()
        self.assertEqual(body["code"], 1000)
