"""T8：`callback_url` / `callback_state` 的**后端读写面**（设计文档 §7.1 / §8）。

## 本文件证什么

⛔ **前端页面改造是另一个任务（T9）** ⇒ 本文件只钉后端：

1. **读**：`GET /api/open/apps/` 与创建 / `PATCH` 的响应载荷里回这两列
   （`app_row_payload` 是**唯一**装配出口，⛔ 不另开第二个）；
2. **写**：`POST /api/open/apps/` 可以**顺带**填；`PATCH /api/open/apps/<pk>/` 可以改
   （含把地址**清空**——空串是**合法值**：清掉回调 ⇒ 不再产生投递，§7.1）；
3. ⭐ **校验只有一份实现**：保存时拒收的文案与
   `apps/openapi/outbound.py::validate_callback_url` 抛的**逐字相同**
   （判据把两处放在**同一个输入**上对拍 ⇒ 「两处各写一份判据」这条会当场红）；
4. ⛔ **拒收零写入**：非 https / 内网地址 / 解析不出的域名 ⇒ 业务码 `1000`，
   而且**库里的值一个字都没变、一行留痕都不写**；
5. **留痕**：真改了才写一行（纯 no-op 不写），且 `field_name` 是**真实列名**、中文由
   `FIELD_LABELS` 派生（⛔ 句子里不出现裸列名）。

## 夹具纪律

- 凭据与地址一律**合成值**（`AGENTS.md` §5-C）；公网那一档用地址**字面量**
  （`93.184.216.34`，⛔ 不是任何真实第三方，也⛔ 不打真实网络——校验只做 `getaddrinfo`）。
- `cache.clear()`：本仓 §6.3 的按来源 IP 计数曾落 `LocMemCache`（T3 已换 DB 行），
  保留它是"只加不删"的纪律。
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.openapi import outbound
from apps.openapi.api.app_views import APP_MANAGE_NODES
from apps.openapi.models import ApiApp, ApiAppState, CallbackState
from apps.rbac.seed import seed_catalog
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 应用管理的节点门（复用现成的 `system.pinduoduo_settings`；⛔ 本任务不新增节点）。
APP_MANAGE_NODE = APP_MANAGE_NODES[0]

#: 应用管理端点（与 `apps/openapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）。
LIST_PATH = "/api/open/apps/"
DETAIL_TEMPLATE = "/api/open/apps/{pk}/"

#: 业务码字面值（E-0024：写数字）。
OK = 0
VALIDATION = 1000

#: 合成地址与凭据（⛔ 与任何真实第三方无关）。
PUBLIC_HOST = "93.184.216.34"
CALLBACK_URL = f"https://{PUBLIC_HOST}/openapi/hook"
OTHER_CALLBACK_URL = f"https://{PUBLIC_HOST}/openapi/hook2"
APP_NAME = "回调夹具应用甲"
APP_KEY = "t8callbackkey0001"
APP_SECRET = "t8-callback-secret-0000000000000001"

#: 一个**必须被拒**的地址（内网 / 回环 / 云元数据）：判据是任选一个都拒，这里取元数据地址。
PRIVATE_URL = "https://169.254.169.254/latest/meta-data"


class _CallbackFixture(TenantTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        seed_catalog()
        self.admin = make_logged_in_client(
            username="t8-callback-admin", groups=("客服",), nodes=(APP_MANAGE_NODE,)
        )
        self.anonymous = Client(SERVER_NAME=self.domain.domain)
        self.creator = User.objects.get(username="t8-callback-admin")

    # ── 造物 / 发请求 ──────────────────────────────────────────────────
    def make_app(self, **overrides) -> ApiApp:
        fields = {
            "name": APP_NAME,
            "app_key": APP_KEY,
            "app_secret": APP_SECRET,
            "created_by": self.creator,
        }
        fields.update(overrides)
        return ApiApp.objects.create(**fields)

    def post_json(self, url, payload):
        return self.admin.post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def patch_json(self, url, payload):
        return self.admin.patch(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def get(self, url):
        return self.admin.get(url)

    def body(self, response):
        return json.loads(response.content)

    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def assert_rejected(self, response, *, message: str):
        """业务失败（HTTP 200 + `1000`）+ 文案：本节所有拒收共用的判据。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], VALIDATION, payload)
        self.assertIn(message, payload["msg"], payload)
        return payload


class CallbackReadTests(_CallbackFixture):
    """读面：两列必须出现在**行载荷**里（页面 T9 靠它们渲染输入框与开关）。"""

    def test_the_list_row_payload_carries_both_callback_fields(self):
        app = self.make_app(callback_url=CALLBACK_URL, callback_state=CallbackState.DISABLED)
        row = self.assert_ok(self.get(LIST_PATH))[0]
        self.assertEqual(row["callback_url"], CALLBACK_URL)
        self.assertEqual(row["callback_state"], CallbackState.DISABLED)
        self.assertEqual(row["id"], app.pk)

    def test_the_defaults_are_an_empty_address_and_an_enabled_switch(self):
        """模型默认值就是「没配」+「开关启用」（§7.1：地址空本来就不投递）。"""
        self.make_app()
        row = self.assert_ok(self.get(LIST_PATH))[0]
        self.assertEqual(row["callback_url"], "")
        self.assertEqual(row["callback_state"], CallbackState.ENABLED)

    def test_the_create_response_carries_them_too(self):
        data = self.assert_ok(
            self.post_json(
                LIST_PATH, {"name": "回调夹具应用乙", "callback_url": CALLBACK_URL}
            )
        )
        self.assertEqual(data["callback_url"], CALLBACK_URL)
        self.assertEqual(data["callback_state"], CallbackState.ENABLED)

    def test_the_patch_response_carries_them(self):
        app = self.make_app()
        data = self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"callback_url": CALLBACK_URL, "callback_state": "disabled"},
            )
        )
        self.assertEqual(data["callback_url"], CALLBACK_URL)
        self.assertEqual(data["callback_state"], CallbackState.DISABLED)


class CallbackWriteTests(_CallbackFixture):
    """写面：建时可选、改时真正变了才写 + 留痕（两列都要能改）。"""

    def test_create_accepts_both_fields(self):
        data = self.assert_ok(
            self.post_json(
                LIST_PATH,
                {
                    "name": "回调夹具应用丙",
                    "callback_url": f"{CALLBACK_URL}/",
                    "callback_state": "disabled",
                },
            )
        )
        app = ApiApp.objects.get(pk=data["id"])
        self.assertEqual(app.callback_url, f"{CALLBACK_URL}/")
        self.assertEqual(app.callback_state, CallbackState.DISABLED)

    def test_patch_writes_both_fields_and_leaves_two_audit_rows(self):
        app = self.make_app()
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"callback_url": CALLBACK_URL, "callback_state": "disabled"},
            )
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_url, CALLBACK_URL)
        self.assertEqual(app.callback_state, CallbackState.DISABLED)
        rows = list(OperationLog.objects.filter(action="system.open_api_app_update"))
        self.assertEqual(
            sorted(row.field_name for row in rows), ["callback_state", "callback_url"]
        )
        by_field = {row.field_name: row for row in rows}
        self.assertEqual(by_field["callback_url"].from_value, "空")
        self.assertEqual(by_field["callback_url"].to_value, CALLBACK_URL)
        self.assertEqual(by_field["callback_state"].from_value, "启用")
        self.assertEqual(by_field["callback_state"].to_value, "停用")
        # ⚠️ 中文标签由 `FIELD_LABELS` 派生（R8/R9）⇒ 句子里不许出现裸列名。
        self.assertIn("回调地址：空 → " + CALLBACK_URL, by_field["callback_url"].summary)
        self.assertIn("回调开关：启用 → 停用", by_field["callback_state"].summary)

    def test_clearing_the_address_is_a_legal_value(self):
        """⭐ 清空回调地址（`""`）是**合法值**（= 不再产生投递）⇒ 必须真的生效。

        失败方向：调用方用真值判据（`if data.get("callback_url"):`）⇒ 本用例红
        （"清空"被当成"没传"，地址**静默留着**，推送继续发）。
        """
        app = self.make_app(callback_url=CALLBACK_URL)
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"callback_url": ""})
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_url, "")
        row = OperationLog.objects.get(action="system.open_api_app_update")
        self.assertEqual(row.field_name, "callback_url")
        self.assertEqual(row.to_value, "空")

    def test_resending_the_same_values_writes_nothing(self):
        """纯 no-op（重发同值）⇒ **不写库、不写留痕**（R26 口径）。"""
        app = self.make_app(
            callback_url=CALLBACK_URL, callback_state=CallbackState.DISABLED
        )
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"callback_url": CALLBACK_URL, "callback_state": "disabled"},
            )
        )
        self.assertEqual(
            OperationLog.objects.filter(action="system.open_api_app_update").count(), 0
        )

    def test_callback_state_can_be_switched_back_on(self):
        app = self.make_app(callback_state=CallbackState.DISABLED)
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"callback_state": "enabled"})
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_state, CallbackState.ENABLED)

    def test_a_bad_callback_state_value_is_a_business_error(self):
        app = self.make_app()
        self.assert_rejected(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"callback_state": "maybe"}),
            message="",
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_state, CallbackState.ENABLED)


class CallbackValidationTests(_CallbackFixture):
    """⭐ 校验**只有一份实现** + 拒收**零写入**（§7.1 的「保存时」那一半）。"""

    #: 每一种"必须被拒"的形状（与 `outbound` 那一层的用例同源，这里证**端点这一层**也拒）。
    REJECTED = (
        "http://93.184.216.34/hook",
        "https://127.0.0.1/hook",
        "https://10.1.2.3/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pw@93.184.216.34/hook",
        "https://t8-no-such-host.invalid/hook",
    )

    def _expected_message(self, url: str) -> str:
        """期望文案**取自被复用的那个函数**（对拍的是"同一份实现"，⛔ 不是抄一句中文）。"""
        try:
            outbound.validate_callback_url(url)
        except outbound.CallbackUrlError as exc:
            return str(exc)
        raise AssertionError(f"夹具地址 {url} 竟然通过了 outbound 的校验（夹具坏了）")

    def test_every_rejected_shape_is_refused_by_the_endpoint_with_the_same_message(self):
        app = self.make_app(callback_url=CALLBACK_URL)
        for url in self.REJECTED:
            with self.subTest(url=url):
                self.assert_rejected(
                    self.patch_json(
                        DETAIL_TEMPLATE.format(pk=app.pk), {"callback_url": url}
                    ),
                    message=self._expected_message(url),
                )
                app.refresh_from_db()
                self.assertEqual(
                    app.callback_url, CALLBACK_URL,
                    f"{url} 被拒了，但库里的地址被改坏了",
                )
        self.assertEqual(
            OperationLog.objects.filter(action="system.open_api_app_update").count(), 0,
            "被拒的请求**一行留痕都不许写**",
        )

    def test_a_rejected_url_is_also_refused_on_create(self):
        self.assert_rejected(
            self.post_json(LIST_PATH, {"name": "回调夹具应用丁", "callback_url": PRIVATE_URL}),
            message=self._expected_message(PRIVATE_URL),
        )
        self.assertFalse(ApiApp.objects.filter(name="回调夹具应用丁").exists())

    def test_a_valid_https_address_is_accepted(self):
        """正对照：合法的 https 公网地址放行（没有它，"全拒"也能让上面几条绿）。"""
        app = self.make_app()
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk), {"callback_url": OTHER_CALLBACK_URL}
            )
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_url, OTHER_CALLBACK_URL)

    def test_surrounding_whitespace_is_trimmed_on_save(self):
        """归一化：首尾空白被去掉（投递时用的就是这个形态，⛔ 两处形态必须一致）。"""
        app = self.make_app()
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=app.pk),
                {"callback_url": f"  {OTHER_CALLBACK_URL}  "},
            )
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_url, OTHER_CALLBACK_URL)

    def test_an_over_long_address_is_refused_by_the_column_width_boundary(self):
        """列宽闸：超过 `ApiApp.callback_url`（200）⇒ `1000`，⛔ 不让它进库被静默截断。"""
        app = self.make_app()
        too_long = f"https://{PUBLIC_HOST}/" + "a" * 200
        self.assert_rejected(
            self.patch_json(DETAIL_TEMPLATE.format(pk=app.pk), {"callback_url": too_long}),
            message="",
        )
        app.refresh_from_db()
        self.assertEqual(app.callback_url, "")
