"""闲管家店铺对接的接口测试（spec §5 / §8 / §9 / §10）。

三类分开写，别混着读：

- `XgjShopBindingApiTests`：CRUD、**密钥只写不读**、推送 URL 由 host 生成、测试连接、查详情；
- `XgjReadPlatformOrderDispatchTests`：录单读单的**分流**（spec §3-D6 的优先级）；
- `XgjShopBindingIsolationTests`：跨租户隔离（`AGENTS.md` §4 的红线在本文件闭合）。

**桩打在哪里**（E-0029：打在「被查找的那个模块」上）：
- `apps.business.api.xgj_views.order_detail` / `.shop_list`（视图 `from ... import` 进来的名字）；
- `apps.business.api.platform_order_views.order_detail`（阿奇索）与 `.xgj_order_detail`（闲管家）。
"""
from __future__ import annotations

import json
from unittest import mock

from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, ShopBinding, SystemSwitch, XgjShop, XgjShopBinding
from apps.business.xgj import XgjError
from apps.audit.models import OperationLog
from apps.platform.models import Domain
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/xgj-bindings/"
ORDER_DETAIL_URL = "/api/business/xgj/order-detail/"
READ_URL = "/api/business/read-platform-order/"

DOMAIN_A = "tenant.test.com"
DOMAIN_B = "xgj-iso-b.test.local"
SCHEMA_B = "xgj_iso_b"

#: 判据用的业务值：**租户内唯一、且两租户取值不同**（`AGENTS.md` §5：不用主键指认跨租户的行）。
SHOP_A = "A店-只应出现在A"
SHOP_B = "B店"
SECRET_A = "SK-only-in-a"
SECRET_B = "SK-only-in-b"


class XgjShopBindingApiTests(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(username="xgj-admin")
        self.source = OrderSource.objects.create(name="闲鱼A", shop_type=20)
        # ⚠️ 保存（POST/PATCH）会**自动同步一次店铺**（R5）⇒ 默认把上游打成空列表。
        # 不打桩的话每条 CRUD 用例都会真发一次网络请求（本仓单元测试一律不打真上游）。
        # 桩打在 `apps.business.xgj_shop.shop_list`——**被查找的那个模块**（E-0029）。
        patcher = mock.patch("apps.business.xgj_shop.shop_list", return_value=[])
        self.shop_list = patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, payload):
        return self.client.post(URL, json.dumps(payload), content_type="application/json")

    def _patch(self, pk, payload):
        return self.client.patch(
            URL + f"{pk}/", json.dumps(payload), content_type="application/json"
        )

    def _create(self, **extra):
        payload = {"shop_name": SHOP_A, "app_key": "AK-1", "app_secret": SECRET_A}
        payload.update(extra)
        res = self._post(payload)
        self.assertEqual(res.json()["code"], 0, res.content)
        return res

    # ── CRUD ────────────────────────────────────────────────────

    def test_create_and_list(self):
        body = self._create().json()
        self.assertEqual(body["data"]["shop_name"], SHOP_A)
        self.assertEqual(body["data"]["app_key"], "AK-1")
        self.assertIs(body["data"]["app_secret_set"], True)
        # ⚠️ 这一行**没有** `order_source` 了（per-source spec D3）：来源归**店铺**（`XgjShop`），
        #    一个应用最多管 30 家店，"一个应用 ↔ 一个来源"就是要修掉的错模型。
        self.assertNotIn("order_source", body["data"])
        # 落库了（不是只回显）
        row = XgjShopBinding.objects.get()
        self.assertEqual(row.app_secret, SECRET_A)

        listed = self.client.get(URL).json()
        self.assertEqual(listed["code"], 0)
        self.assertEqual([i["shop_name"] for i in listed["data"]["items"]], [SHOP_A])
        self.assertNotIn("order_source", listed["data"]["items"][0])

    def test_put_is_not_exposed(self):
        """`spec §5` 的端点表只有 GET/POST/PATCH/DELETE；PUT 是 router 默认多给的能力面。"""
        pk = XgjShopBinding.objects.create(shop_name=SHOP_A).pk
        res = self.client.put(
            URL + f"{pk}/", json.dumps({"shop_name": "x"}), content_type="application/json"
        )
        self.assertEqual(res.status_code, 405)

    def test_delete_removes_the_row(self):
        pk = XgjShopBinding.objects.create(shop_name=SHOP_A).pk
        self.assertEqual(self.client.delete(URL + f"{pk}/").json()["code"], 0)
        self.assertFalse(XgjShopBinding.objects.filter(pk=pk).exists())

    def test_blank_app_key_is_rejected_with_1000(self):
        res = self._post({"shop_name": SHOP_A, "app_key": "   ", "app_secret": SECRET_A})
        self.assertEqual(res.json()["code"], 1000)
        self.assertEqual(XgjShopBinding.objects.count(), 0)

    def test_app_key_and_secret_are_stripped(self):
        """租户是从控制台**复制粘贴**的；带换行的 appid 会让上游回 `100001`（看着像密钥错）。"""
        self._create(app_key="  AK-1\n", app_secret=" " + SECRET_A + "\n")
        row = XgjShopBinding.objects.get()
        self.assertEqual(row.app_key, "AK-1")
        self.assertEqual(row.app_secret, SECRET_A)

    def test_malformed_write_bodies_are_1000_not_500(self):
        """畸形**写入体**一律 `1000`，不是 500（AGENTS.md §5 / E-0010）。

        ⚠️ 这是**补位测试**：`test_no_route_500` 的两条腿（registry 腿与 explicit-routes 腿）
        **只发 GET**，所以 POST/PATCH 的畸形体它**盖不到**（判据见那条腿自己的 `accepts_get`）。
        本仓的结构兜底网从来只覆盖"畸形**查询参数**"，写入体是每个资源自己的责任。

        判据是「**这类**请求还会不会 500」而不是"某个字段会不会"：所以下面按**字段种类**取值
        （字符串字段给对象/数组、超长值、整个 body 给数组），
        而不是只挑一个字段试一次。
        ⚠️ 表上**没有** `order_source` 了（per-source spec D3），所以那几条外键用例已删；
        传未知字段 DRF **不报错**（静默忽略），拿它当"畸形体"会得到一条**假**用例。
        """
        pk = XgjShopBinding.objects.create(shop_name=SHOP_A).pk
        post_cases = {
            "app_secret 给对象": {"app_key": "AK-1", "app_secret": {"a": 1}},
            "shop_name 超长": {"app_key": "AK-1", "shop_name": "x" * 65},
            "app_key 超长": {"app_key": "x" * 65},
            "app_key 给数组": {"app_key": ["AK-1"]},
            "shop_name 给对象": {"app_key": "AK-1", "shop_name": {"a": 1}},
            "remark 给对象": {"app_key": "AK-1", "remark": {"a": 1}},
        }
        for label, payload in post_cases.items():
            with self.subTest(case=label):
                self.assertEqual(self._post(payload).json()["code"], 1000, label)

        # 整个 body 不是对象（真实客户端也可能发错）
        res = self.client.post(URL, json.dumps([1, 2]), content_type="application/json")
        self.assertEqual(res.json()["code"], 1000)

        patch_cases = {
            "app_secret 给数组": {"app_secret": ["x"]},
            "shop_name 给数组": {"shop_name": [1, 2]},
            "remark 超长": {"remark": "x" * 201},
        }
        for label, payload in patch_cases.items():
            with self.subTest(case=f"PATCH {label}"):
                res = self.client.patch(
                    URL + f"{pk}/", json.dumps(payload), content_type="application/json"
                )
                self.assertEqual(res.json()["code"], 1000, label)

    # ── 密钥只写不读（spec §9-S1 / §9-S4）───────────────────────

    def test_secret_is_never_echoed(self):
        """**这条是红线**：`app_secret` 一次都不进浏览器（业务方 2026-09-22 二选一确认）。"""
        created = self._create().content.decode()
        self.assertNotIn(SECRET_A, created)
        self.assertNotIn('"app_secret"', created)
        # 只回答「配没配」（`app_secret_set` 的名字里含 `app_secret`，所以上面判据要带引号）
        self.assertIn('"app_secret_set"', created)

        listed = self.client.get(URL).content.decode()
        self.assertNotIn(SECRET_A, listed)
        self.assertNotIn('"app_secret"', listed)

        patched = self._patch(XgjShopBinding.objects.get().pk, {"remark": "只改备注"})
        self.assertNotIn(SECRET_A, patched.content.decode())

    def test_patch_with_empty_secret_keeps_the_old_one(self):
        """「空 = 不改」（spec §9-S4）：一次「只改备注」的编辑**不得**把密钥清掉。

        这类"无声丢数据"只有测试能拦——页面上**显示不出**密钥（回显关着），
        租户要等到录单失败（`1016`）才会发现。
        """
        self._create()
        row = XgjShopBinding.objects.get()
        res = self._patch(row.pk, {"remark": "改备注", "app_secret": ""})
        self.assertEqual(res.json()["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.app_secret, SECRET_A)
        self.assertEqual(row.remark, "改备注")

    def test_patch_with_set_secret_replaces_it(self):
        self._create()
        row = XgjShopBinding.objects.get()
        self._patch(row.pk, {"app_secret": SECRET_B})
        row.refresh_from_db()
        self.assertEqual(row.app_secret, SECRET_B)

    # ── 推送 URL（spec §3-D5）──────────────────────────────────

    def test_push_urls_are_generated_from_the_request_host(self):
        """两个推送地址是**我们**给闲管家的回调地址（不是租户填的），由请求的 host 生成。"""
        body = self._create().json()["data"]
        self.assertTrue(body["order_push_url"].endswith("/api/business/xgj/push/order/"))
        self.assertTrue(body["product_push_url"].endswith("/api/business/xgj/push/product/"))
        self.assertIn(DOMAIN_A, body["order_push_url"])
        self.assertIn(DOMAIN_A, body["product_push_url"])
        # 入库（「记录在案」）
        row = XgjShopBinding.objects.get()
        self.assertEqual(row.order_push_url, body["order_push_url"])
        self.assertEqual(row.product_push_url, body["product_push_url"])

    def test_client_cannot_forge_push_urls(self):
        """前端传什么都不看：租户不能把回调地址指向别处，而我们以为那是自己的回调。

        ⚠️ **POST 与 PATCH 都要测**（第二轮独立评审的 Important）：修复轮把
        `_apply_push_urls()` 挪到了 `super().update()` **之前**，而 DRF 的
        `ModelSerializer.update()` 是「先 `setattr` 再 `save()`」⇒ **对 PATCH 来说，
        "无条件覆写"这道防线失效了**，只剩 `read_only_fields` 一层。
        评审实测（M8）：删掉 `read_only_fields` 里那两个键后，**PATCH 能把
        `https://evil.example/x` 存进库**，而当时这条用例只发 POST ⇒ **仍然绿**。
        现在两道都补齐：`update()` 里显式 `pop` 掉这两个键（见 `serializers.py` 的注释），
        且本用例对**两个动词**各断言一次——任一层被拆掉都会红。
        """
        # ① POST
        body = self._create(order_push_url="https://evil.example/x").json()["data"]
        self.assertIn(DOMAIN_A, body["order_push_url"])
        self.assertNotIn("evil.example", body["order_push_url"])
        # ② PATCH（同一条不变量，另一个动词）
        row = XgjShopBinding.objects.get()
        patched = self.client.patch(
            URL + f"{row.pk}/",
            json.dumps({"order_push_url": "https://evil.example/patch"}),
            content_type="application/json",
        ).json()["data"]
        self.assertNotIn("evil.example", patched["order_push_url"])
        self.assertIn(DOMAIN_A, patched["order_push_url"])
        row.refresh_from_db()
        self.assertNotIn("evil.example", row.order_push_url, "库里也不该有伪造值")

    def test_patch_refreshes_push_urls(self):
        """改了 host 的部署（换域名）重新保存一次即可刷新（spec §12-2 的残差）。

        ⚠️ **PATCH 必须用另一个 host**，否则这条没有判别力：建行与 PATCH 都在 `DOMAIN_A` 上时，
        「PATCH 完全不刷新 URL」也会让 `DOMAIN_A in url` 成立。
        变异实测（独立评审 M4）：删掉 `update()` 里的 `_apply_push_urls` 调用，旧版本**全绿**；
        现在会红——断言的是「URL 跟着本次请求的 host 变成 B」。
        """
        self._create()
        row = XgjShopBinding.objects.get()
        self.assertIn(DOMAIN_A, row.order_push_url)   # 前置：建行那一份是 A

        # 给**同一个租户**再挂一个域名（"换域名部署"的最小仿真）。
        # ⚠️ 必须真建这一行：Host 路由按 `Domain` 表解析，随便传一个不存在的 host 会被
        #    中间件当成未知租户拒掉（那就测不到"同一租户、不同 host"这件事了）。
        alt_domain = "xgj-alt.test.local"
        Domain.objects.create(domain=alt_domain, tenant=self.tenant, is_primary=False)

        res = self.client.patch(
            URL + f"{row.pk}/",
            json.dumps({"remark": "换域名后再存一次"}),
            content_type="application/json",
            SERVER_NAME=alt_domain,
        )
        body = res.json()["data"]
        self.assertIn(alt_domain, body["order_push_url"])
        self.assertNotIn(DOMAIN_A, body["order_push_url"])
        self.assertIn(alt_domain, body["product_push_url"])
        row.refresh_from_db()
        self.assertIn(alt_domain, row.order_push_url, "PATCH 之后库里也该跟着刷新")

    # ── 测试连接 ────────────────────────────────────────────────

    def test_test_connection_lists_the_shops_of_this_app(self):
        self._create()
        row = XgjShopBinding.objects.get()
        shops = [{"shop_name": SHOP_A, "is_valid": True}]
        with mock.patch(
            "apps.business.api.xgj_views.shop_list", return_value=shops
        ) as called:
            body = self.client.post(URL + f"{row.pk}/test-connection/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["shops"], shops)
        # 凭据**只从那一行取**（不接受请求里传 app_key / app_secret）
        self.assertEqual(called.call_args.kwargs["app_key"], "AK-1")
        self.assertEqual(called.call_args.kwargs["app_secret"], SECRET_A)

    def test_test_connection_without_credentials_is_1015(self):
        pk = XgjShopBinding.objects.create(shop_name=SHOP_A, app_key="AK-1").pk
        # ⚠️ `return_value` **必须给**：不给的话，一旦"凭据不全也去调上游"这个不变量被破，
        # 桩会返回一个 MagicMock，它进 JSON 响应后让 DRF 编码器**无限递归**
        # （第二轮评审判定为 Minor：测试会**挂死**而不是红，CI 里比红更糟）。
        with mock.patch(
            "apps.business.api.xgj_views.shop_list", return_value=[{"shop_name": "不该被调到"}]
        ) as called:
            body = self.client.post(URL + f"{pk}/test-connection/").json()
        self.assertEqual(body["code"], 1015)
        self.assertFalse(called.called, "凭据不全时不得发上游请求")

    def test_test_connection_maps_the_auth_error_to_1016(self):
        from apps.business.xgj import XgjError

        self._create()
        row = XgjShopBinding.objects.get()
        with mock.patch(
            "apps.business.api.xgj_views.shop_list",
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            body = self.client.post(URL + f"{row.pk}/test-connection/").json()
        self.assertEqual(body["code"], 1016)

    # ── 查订单详情 ──────────────────────────────────────────────

    def test_order_detail_returns_upstream_verbatim(self):
        self._create()
        row = XgjShopBinding.objects.get()
        detail = {"order_no": "2226688164543566229", "total_amount": 8000, "goods": {"title": "标题"}}
        with mock.patch(
            "apps.business.api.xgj_views.order_detail", return_value=detail
        ) as called:
            body = self.client.post(
                ORDER_DETAIL_URL,
                json.dumps({"binding": row.pk, "order_no": "2226688164543566229"}),
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0)
        # **原样**返回（不裁剪、不改名）：映射成中文标签是前端的事
        self.assertEqual(body["data"]["detail"], detail)
        self.assertEqual(called.call_args.kwargs["app_key"], "AK-1")

    def test_order_detail_requires_a_numeric_order_no(self):
        self._create()
        row = XgjShopBinding.objects.get()
        for bad in ("abc", "١٢٣", "", "12 34"):
            with self.subTest(order_no=bad):
                res = self.client.post(
                    ORDER_DETAIL_URL,
                    json.dumps({"binding": row.pk, "order_no": bad}),
                    content_type="application/json",
                )
                self.assertEqual(res.json()["code"], 1000)

    def test_order_detail_with_unknown_binding_is_1015(self):
        with mock.patch("apps.business.api.xgj_views.order_detail") as called:
            body = self.client.post(
                ORDER_DETAIL_URL,
                json.dumps({"binding": 999999, "order_no": "123"}),
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 1015)
        self.assertFalse(called.called)

    def test_order_detail_maps_upstream_failures(self):
        from apps.business.xgj import XgjError

        #: 哨兵串刻意**不含「失败」二字**：视图自己的文案是「闲管家订单读取失败」，
        #: 拿「失败」当判据会恒真（自测过：第一版就栽在这里）。
        canary = "CANARY-上游原文不该出现"
        self._create()
        row = XgjShopBinding.objects.get()
        cases = ((100001, 1016), (100004, 1017), (-1, 1017))
        for upstream, expected in cases:
            with self.subTest(upstream=upstream):
                with mock.patch(
                    "apps.business.api.xgj_views.order_detail",
                    side_effect=XgjError(upstream_code=upstream, upstream_msg=canary),
                ):
                    body = self.client.post(
                        ORDER_DETAIL_URL,
                        json.dumps({"binding": row.pk, "order_no": "123"}),
                        content_type="application/json",
                    ).json()
                self.assertEqual(body["code"], expected)
                # 上游原文**不**弹给租户
                self.assertNotIn(canary, body["msg"])


class XgjReadPlatformOrderDispatchTests(TenantTestCase):
    """录单读单的分流（per-source spec D6/D7）：给了来源 ⇒ **阿奇索优先，闲管家兜底**；
    没给来源 ⇒ **自动判定**（只遍历闲管家，命中即停）。

    ⚠️ 阿奇索的平台级凭证必须显式给：本机 `.env` 里**有** AGISO 凭证，
    但那是开发者本机的值、测试不该依赖它（`test_read_platform_order.py` 同一条口径）。
    ⚠️ **不能用类级 `@override_settings`**：`TenantTestCase.setUpClass` 没有转调
    `SimpleTestCase.setUpClass`（那条覆盖是在那里 `enable()` 的），所以这里显式
    `enable()` + `addCleanup(disable)`，写法**字面量**、不读环境。
    """

    def setUp(self):
        creds = override_settings(AGISO_APP_ID="app", AGISO_APP_SECRET="sec")
        creds.enable()
        self.addCleanup(creds.disable)
        self.client = make_logged_in_client(username="xgj-reader")
        self.source = OrderSource.objects.create(name="闲鱼A", shop_type=20)

    def _post(self, tid="2226688164543566229", **extra):
        body = {"tid": tid}
        body.update(extra)
        return self.client.post(READ_URL, json.dumps(body), content_type="application/json")

    def _xgj(self, *, order_source=None, identity="ID-A", shop_name=SHOP_A,
             app_key="AK-1", app_secret=SECRET_A):
        """建一条闲管家应用 + 它名下的一家店；店挂到给定来源（`None` = 还没同步出店名/来源）。

        ⚠️ `XgjShopBinding` **没有** `order_source` 字段（per-source spec D3）：
        「来源 → 应用」这条链改成「来源 → `XgjShop` → `binding`」，所以夹具必须先建店行。
        """
        binding = XgjShopBinding.objects.create(
            shop_name=f"应用-{shop_name}", app_key=app_key, app_secret=app_secret
        )
        XgjShop.objects.create(
            binding=binding, user_identity=identity, shop_name=shop_name,
            user_name=f"member-{identity}", order_source=order_source,
        )
        return binding

    def test_source_with_only_an_xgj_binding_reads_from_xgj(self):
        self._xgj(order_source=self.source)
        detail = {
            "order_no": "2226688164543566229",
            # 录单金额取**实付**（业务方定；真实数据证明它与下单金额真的会不同）
            "pay_amount": 1234567,
            "total_amount": 9999999,
            "buyer_nick": "小明",
            "receiver_mobile": "13800138000",
            "goods": {"title": "代练-王者荣耀"},
        }
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail"
        ) as agiso, mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 0, body)
        # 分 → 元（两位小数字符串，前端一次换算都不做）。
        # ⚠️ 固定件里 `pay_amount` 与 `total_amount` **刻意取了不同的值**：这样"取错字段"
        #    会让这条红（同一份夹具两个值都写成一样，就等于没有判别力——本仓 E-0024 同族）。
        self.assertEqual(body["data"]["payment"], "12345.67")
        self.assertEqual(body["data"]["buyer_nick"], "小明")
        self.assertEqual(body["data"]["customer_phone"], "13800138000")
        self.assertEqual(body["data"]["remark"], "代练-王者荣耀")
        self.assertFalse(agiso.called, "没有阿奇索绑定时不得去调阿奇索")

    def test_agiso_wins_when_both_sides_are_usable(self):
        """spec §3-D6：一个来源两边都配 ⇒ **阿奇索赢**，闲管家那一行静默失效（已登记代价）。"""
        ShopBinding.objects.create(order_source=self.source, access_token="tok")
        self._xgj(order_source=self.source)
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            return_value={"payment": 500, "buyer_nick": "阿奇索的买家"},
        ), mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail"
        ) as xgj:
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["payment"], "5.00")
        self.assertEqual(body["data"]["buyer_nick"], "阿奇索的买家")
        self.assertFalse(xgj.called, "阿奇索可用时不得走闲管家")

    def test_xgj_is_used_when_the_agiso_row_is_not_usable(self):
        """判据是「阿奇索那边**可用**」而不是「有那一行」：

        一个建了行但**还没填授权码**的阿奇索绑定，不该把闲管家那条能用的配置一起挡掉。
        """
        ShopBinding.objects.create(order_source=self.source, access_token="")
        self._xgj(order_source=self.source)
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            return_value={"order_no": "1", "pay_amount": 100},
        ):
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["payment"], "1.00")

    def test_xgj_uses_the_first_row_with_complete_credentials(self):
        """同一来源有多行闲管家配置时，取**第一条凭据完整**的行，而不是「第一条行」。

        ⚠️ 这条是**修复轮新加行为**的测试（独立评审的 Minor：原来只 `.first()`）：
        第一行只填了 AppKey 时旧实现会回 `1015`，而后面明明有一条能用的——
        那正是"配了却点了没反应"的形状。没有这条测试，把循环改回 `.first()` 不会红。
        """
        incomplete = self._xgj(
            order_source=self.source, identity="ID-半配的",
            shop_name="半配的", app_key="AK-没密钥", app_secret="",
        )
        usable = self._xgj(
            order_source=self.source, identity="ID-能用的",
            shop_name="能用的", app_key="AK-完整的", app_secret=SECRET_A,
        )
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            return_value={"order_no": "1", "pay_amount": 100},
        ) as upstream:
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 0, body)
        # 前置哨兵：两行都真在（否则"只有一行"会让这条失去判别力），且第一行确实不完整
        self.assertEqual(XgjShop.objects.filter(order_source=self.source).count(), 2)
        self.assertEqual(incomplete.app_secret, "")
        self.assertEqual(usable.app_key, "AK-完整的")
        # 承重判据：用的是**第二行**的凭据
        self.assertEqual(upstream.call_args.kwargs["app_key"], "AK-完整的")
        self.assertEqual(upstream.call_args.kwargs["app_secret"], SECRET_A)

    def test_failure_log_redacts_the_app_secret(self):
        """上游失败文案进日志之前**必须**抹掉 AppSecret（spec §9-S1）。

        ⚠️ 断言的是**日志文本本身**。本项目 E-0009 的教训是"别把中间层当出口"——
        而这条链路上**日志就是唯一的出口**（上游原文既不弹给客服、也不落库）⇒ 断言它
        就是断言真实出口，不是拿中间层当替身。
        ⚠️ `assertLogs` 会自己把目标 logger 抬到 WARNING（同 E-0009 的注释），
        所以这里**不**用它证明"生产配了日志"，只用它读那条消息的**内容**。
        """
        self._xgj(order_source=self.source)
        # 模拟"上游把收到的内容回显进 msg"——我们**无法枚举**它不会，所以要防御
        leaky = XgjError(upstream_code=100001, upstream_msg=f"签名错误 app_secret={SECRET_A}")
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", side_effect=leaky
        ), self.assertLogs("apps.business.api.platform_order_views", level="WARNING") as logs:
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 1016)
        text = "\n".join(logs.output)
        self.assertNotIn(SECRET_A, text, "AppSecret 出现在日志里了（spec §9-S1 红线）")
        self.assertIn("***", text)
        # 上游原文的**其余部分**照旧保留（整条丢掉会让排障失明）
        self.assertIn("签名错误", text)

    def test_redact_secret_handles_the_empty_secret(self):
        """空 secret 必须**原样返回**。

        ⚠️ 没有这个守卫时 `msg.replace("", "***")` 会把消息**每个字符之间**都塞上 `***`
        （Python 的 `str.replace` 对空串就是这样）——日志会变成乱码。
        """
        from apps.business.api.platform_order_views import _redact_secret

        self.assertEqual(_redact_secret("boom", ""), "boom")
        self.assertEqual(_redact_secret("a SK b", "SK"), "a *** b")

    def test_neither_side_configured_is_1015(self):
        body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 1015)
        # msg 要把**两个**页面都点出来，否则填了闲管家的人会去错地方
        self.assertIn("其它平台店铺对接", body["msg"])
        self.assertIn("闲管家店铺对接", body["msg"])

    def test_a_given_source_with_no_config_does_not_fall_back_to_auto_detect(self):
        """手动给了来源但那个来源没有配置 ⇒ `1015`，**不回落自动判定**。

        ⚠️ 判据是「一个闲管家应用明明能读到这张单、却**没有**被调用」：
        租户明确选了某家店，系统却按别家店读回来，比"读不到"更糟（那是**静默读错**）。
        没有这条，把 `source_id is None` 的分支写成"查不到就自动判定"不会红。

        ⚠️ 第二个子用例是 `order_source = 0`：**它是一个"给了的值"**，只是查不到配置
        ⇒ 也必须回 `1015`。判据写成真值（`if not source_id`）时它会滑进自动判定
        ——那正是 E-0021「`0` 被当成没传」在服务端的同型。
        """
        self._xgj(order_source=None, identity="ID-别的店", shop_name="别的店")
        # 前置哨兵：那条"能读到的"应用确实配了凭据（否则下面可能只是"没人可试"）
        other = XgjShopBinding.objects.get()
        self.assertTrue(other.app_key and other.app_secret)
        for given in (self.source.id, 0):
            with self.subTest(order_source=given):
                with mock.patch(
                    "apps.business.api.platform_order_views.xgj_order_detail",
                    return_value={"order_no": "1", "pay_amount": 100, "seller_eid": "ID-别的店"},
                ) as upstream:
                    body = self._post(order_source=given).json()
                self.assertEqual(body["code"], 1015)
                self.assertFalse(upstream.called, "手动给了来源就不许再去试其它来源")

    def test_missing_price_stays_an_empty_string(self):
        """上游没给 `pay_amount` ⇒ 空串（**不是** `"0.00"`）：前端据此不覆盖已填的价格。

        两者混同会让「上游没给价格」变成「价格 0 元」而**静默记错账**
        （`order-addorder.ts` 的 `if (fields.payment !== '')` 是这条约定的消费方）。
        """
        self._xgj(order_source=self.source)
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            return_value={"order_no": "1", "buyer_nick": "无价单"},
        ):
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["payment"], "")
        # 0 分照实回 "0.00"（与上面那条区分开）
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            return_value={"order_no": "1", "pay_amount": 0},
        ):
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["data"]["payment"], "0.00")

    def test_success_payload_carries_source_and_platform(self):
        """成功载荷必须带 `order_source` / `order_source_name` / `platform`（per-source spec §5）。

        这三个是**自动判定回填下拉**的唯一依据（前端在没选来源时才知道后端判定成了哪家店）。
        """
        self._xgj(order_source=self.source)
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            return_value={"order_no": "1", "pay_amount": 100},
        ):
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["data"]["order_source"], self.source.pk)
        self.assertEqual(body["data"]["order_source_name"], "闲鱼A")
        self.assertEqual(body["data"]["platform"], "xgj")

        # 阿奇索那条路同样要带（platform 用于排障与前端分流）
        ShopBinding.objects.create(order_source=self.source, access_token="tok")
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            return_value={"payment": 500},
        ):
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["data"]["platform"], "agiso")
        self.assertEqual(body["data"]["order_source_name"], "闲鱼A")


#: 线 F（业务方）那条开关的 key（与注册表同源；这里另写字面值钉住它）。
UNPAID_GUARD_KEY = "reject_unpaid_xgj_order"
#: 上游「待付款」状态码（权威出处：`docs/XGJ-INTEGRATION.md` 的状态码表；⛔ 阿奇索的码不可比）。
UNPAID_STATUS = 11
#: 那一档拒绝的业务码（`apps/core/error_codes.py` 的 `PLATFORM_ORDER_STATUS_REJECTED`）。
UNPAID_REJECTED = 1028


class XgjUnpaidOrderGuardTests(TenantTestCase):
    """线 F：闲管家读单遇「待付款」直接拒绝（**系统开关**，默认开）。

    **六条判据各自能独立地红**（逐条见下面对应用例的 docstring）：
    ① 开关开 + 11 ⇒ 拒绝且**不返回半张表单**；② 开关开 + 12/21/22/23/24 ⇒ 照旧放行；
    ③ **开关关 + 11 ⇒ 行为与本批之前逐字相同**（承重判据）；④ 状态码缺失 / 未知 ⇒ 放行（fail-open）；
    ⑤ 阿奇索那条腿用同一个值（11）**不**被拦；⑥ 自动判定那条腿（录单页的默认路径）同样被拦/放行。

    ⚠️ 桩打在 `apps.business.api.platform_order_views`（**被查找的那个模块**，E-0029）。
    ⚠️ 阿奇索的平台级凭证必须显式给（同 `XgjReadPlatformOrderDispatchTests` 的第一条理由）。
    """

    def setUp(self):
        creds = override_settings(AGISO_APP_ID="app", AGISO_APP_SECRET="sec")
        creds.enable()
        self.addCleanup(creds.disable)
        self.client = make_logged_in_client(username="xgj-unpaid")
        self.source = OrderSource.objects.create(name="闲鱼待付款", shop_type=20)

    def _post(self, tid="2226688164543566229", **extra):
        body = {"tid": tid}
        body.update(extra)
        return self.client.post(READ_URL, json.dumps(body), content_type="application/json")

    def _xgj(self, *, order_source=None, identity="ID-A", shop_name=SHOP_A,
             app_key="AK-1", app_secret=SECRET_A):
        """建一条闲管家应用 + 它名下的一家店（与分流测试那份夹具同形）。"""
        binding = XgjShopBinding.objects.create(
            shop_name=f"应用-{shop_name}", app_key=app_key, app_secret=app_secret
        )
        XgjShop.objects.create(
            binding=binding, user_identity=identity, shop_name=shop_name,
            user_name=f"member-{identity}", order_source=order_source,
        )
        return binding

    def _detail(self, **over):
        """一份上游订单详情（映射表里**每一个**字段都给，好让「逐字相同」那条比得动）。"""
        detail = {
            "order_no": "2226688164543566229",
            "pay_amount": 1234567,
            "buyer_nick": "小明",
            "receiver_mobile": "13800138000",
            "goods": {"title": "代练-王者荣耀"},
            "order_status": UNPAID_STATUS,
            "pay_time": 1750000000,
        }
        detail.update(over)
        return detail

    def _read_xgj(self, detail):
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ) as upstream:
            body = self._post(order_source=self.source.id).json()
        self.assertTrue(upstream.called, "前置：这一条本该真去调闲管家读单")
        return body

    # ── ① 开关开 + 待付款 ⇒ 拒绝，且不返回半张表单 ──────────────────
    def test_1_unpaid_order_is_refused_with_a_business_code_and_no_form_fields(self):
        self._xgj(order_source=self.source)
        # 前置哨兵：**没有覆盖行** ⇒ 走的是注册表默认 `"Y"`（开关开）——否则这条只是"开关没开"。
        self.assertFalse(SystemSwitch.objects.exists())

        body = self._read_xgj(self._detail(order_status=UNPAID_STATUS))

        self.assertEqual(body["code"], UNPAID_REJECTED, body)   # 业务码，⛔ 不是 500
        self.assertIsNone(body["data"], "拒绝时不许回半张表单")
        # 「不返回半张表单」要打在**载荷文本**上：`data` 为 None 也能夹带别的东西。
        self.assertNotIn('"payment"', json.dumps(body))
        self.assertIn("待付款", body["msg"])                    # 一句人话
        # ⚠️ 出路必须是**读到这条 msg 的人做得到**的：这条 msg 会被录单页原样弹给
        #    录单客服，而他的门是 `order-new`——「闲管家店铺对接」页要 `system.pinduoduo_settings`、
        #    那个控件还要 `system.settings` ∧ `system.edit_settings`。
        #    ⇒ 主指引 = "买家付款后再读一次"；关开关写成**交给管理员**的条件句。
        self.assertIn("买家付款后再读一次", body["msg"])
        self.assertIn("请联系管理员", body["msg"])
        self.assertIn("闲管家店铺对接", body["msg"])            # 那一页只是**给管理员**看的落点

    # ── ② 开关开 + 其余状态 ⇒ 照旧放行 ──────────────────────────────
    def test_2_other_known_statuses_still_pass_with_the_switch_on(self):
        """12/21/22/23/24 一律照读（闸只认 11，**不**枚举"已知的好状态"）。"""
        self._xgj(order_source=self.source)
        self.assertFalse(SystemSwitch.objects.exists(), "前置：开关是默认的开")
        for status in (12, 21, 22, 23, 24):
            with self.subTest(order_status=status):
                body = self._read_xgj(self._detail(order_status=status))
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(body["data"]["payment"], "12345.67")
                self.assertEqual(body["data"]["order_status"], status)

    # ── ③ 开关关 + 待付款 ⇒ 与本批之前逐字相同（承重判据）────────────
    def test_3_switch_off_makes_an_unpaid_order_read_exactly_as_before(self):
        """关掉开关 ⇒ 待付款单**照旧**读出来，载荷逐字段与开关前一致（这条是「开关」的承重判据）。"""
        self._xgj(order_source=self.source)
        SystemSwitch.objects.create(key=UNPAID_GUARD_KEY, value="N")

        body = self._read_xgj(self._detail(order_status=UNPAID_STATUS))

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"],
            {
                "payment": "12345.67",            # 分 → 元（`pay_amount`）
                "buyer_nick": "小明",
                "buyer_eid": "",                   # 2026-11-10 客户线（夹具 detail 未给此键 ⇒ 空串）
                "customer_phone": "13800138000",   # `receiver_mobile`
                "remark": "代练-王者荣耀",         # `goods.title`
                "order_status": UNPAID_STATUS,     # 取整后透传（前端今天不消费）
                "pay_time": 1750000000,
                "order_source": self.source.pk,
                "order_source_name": "闲鱼待付款",
                "platform": "xgj",
            },
        )

    # ── ④ fail-open：缺状态码 / 未知值 ⇒ 放行 ────────────────────────
    def test_4_missing_or_unknown_status_falls_open(self):
        """只拦**明确**的待付款：上游少给一个字段（或给了没见过的值）⛔ 不许把正常单拦住。

        这条挡的是「写成『不在已知的 12/21/22/23/24 里就拦』」那种写法——那会让整个录单页
        对那些状态的单**全都读不出来**（比漏拦一张待付款单严重得多）。
        """
        self._xgj(order_source=self.source)
        self.assertFalse(SystemSwitch.objects.exists(), "前置：开关是默认的开（否则这条没有判别力）")
        cases = {
            "整个键都没给": {},
            "给了 null": {"order_status": None},
            "给了未知值": {"order_status": 99},
            "给了 0": {"order_status": 0},
        }
        for label, over in cases.items():
            with self.subTest(case=label):
                detail = self._detail()
                detail.pop("order_status", None)
                detail.update(over)
                body = self._read_xgj(detail)
                self.assertEqual(body["code"], 0, body)
                self.assertEqual(body["data"]["order_status"], over.get("order_status") or 0)

    # ── ⑤ 阿奇索那条腿一个字不动 ────────────────────────────────────
    def test_5_agiso_leg_is_not_touched_by_the_switch(self):
        """阿奇索的 `order_status` 用**同一个值**（11）也**不**被拦——两家的码语义不同、不可比。"""
        ShopBinding.objects.create(order_source=self.source, access_token="tok")
        # 闲管家那边也配着（否则"走了阿奇索"可能只是因为闲管家没配，判别力就没了）。
        self._xgj(order_source=self.source)
        self.assertFalse(SystemSwitch.objects.exists(), "前置：开关是默认的开")
        with mock.patch(
            "apps.business.api.platform_order_views.order_detail",
            # ⚠️ 阿奇索的映射只认这几个键（`_to_form_fields`）；`order_status=11` 是**同一个数字**。
            return_value={"payment": 500, "buyer_nick": "阿奇索的买家", "order_status": UNPAID_STATUS},
        ) as agiso, mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail"
        ) as xgj:
            body = self._post(order_source=self.source.id).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["platform"], "agiso")
        self.assertEqual(body["data"]["payment"], "5.00")
        self.assertEqual(body["data"]["order_status"], UNPAID_STATUS)
        self.assertTrue(agiso.called)
        self.assertFalse(xgj.called, "阿奇索可用时不得走闲管家")

    # ── ⑥ 自动判定那条腿（录单页默认路径）──────────────────────────
    def test_6_auto_detect_refuses_before_touching_the_source_dictionary(self):
        """没给来源（自动判定）时同样拦，且**在认店/建来源之前**就拦掉。

        ⚠️ 判据打在**副作用**上：订单里的卖家标识本地不认（会触发"按需同步 + 兜底建来源"那条路）
        ⇒ 断言 `sync_shops` **一次都没被调**、来源表没多行。把闸挪到 `source_for_order` **之后**，
        这条就红（被拒的一次读单不该在库里留下任何东西）。
        """
        self._xgj(order_source=self.source)
        self.assertFalse(SystemSwitch.objects.exists(), "前置：开关是默认的开")
        before_sources = OrderSource.objects.count()
        detail = self._detail(
            order_status=UNPAID_STATUS, seller_eid="ID-不在授权列表", seller_name="兜底店"
        )
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ), mock.patch("apps.business.xgj_shop.sync_shops") as sync:
            body = self._post().json()   # ⛔ 不带 order_source ⇒ 自动判定
        self.assertEqual(body["code"], UNPAID_REJECTED, body)
        self.assertIsNone(body["data"])
        self.assertFalse(sync.called, "被拒的读单不许再去按需同步店铺")
        self.assertEqual(OrderSource.objects.count(), before_sources, "被拒的读单不许建来源")
        self.assertFalse(XgjShop.objects.filter(user_identity="ID-不在授权列表").exists())

    def test_6b_auto_detect_lets_an_unpaid_order_through_when_the_switch_is_off(self):
        """自动判定那条腿也吃同一个开关：关掉 ⇒ 照读（与手动给来源那条逐字同判）。"""
        self._xgj(order_source=self.source)
        SystemSwitch.objects.create(key=UNPAID_GUARD_KEY, value="N")
        detail = self._detail(order_status=UNPAID_STATUS, seller_eid="ID-A", seller_name=SHOP_A)
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._post().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["order_status"], UNPAID_STATUS)
        self.assertEqual(body["data"]["payment"], "12345.67")


class XgjShopBindingIsolationTests(TenantTestCase):
    """跨租户隔离（`AGENTS.md` §4 的红线）。

    ⚠️ 第二个租户**必须**在 `setUpClass` 里建：在一个用例内部建 schema 会与外层事务相冲
    （Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）。

    ⚠️ **判据纪律**（`AGENTS.md` §5）：不用主键指认「另一个租户的那一行」——两个 schema 的主键
    各自从 1 开始 ⇒ 用**租户内唯一、且两租户取值不同**的业务值（`shop_name` / `app_secret`）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="闲管家隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        # ⚠️ 第一行：把连接显式钉回租户 A。跨域名请求不会在响应结束时还原 schema
        #    （见 `test_p3b_isolation.py` 的模块说明），所以上一条用例可能把连接留在 B 上。
        connection.set_tenant(self.tenant)

        self.client_a = make_logged_in_client(domain=DOMAIN_A, username="xgj-iso-a")
        self.a_row = XgjShopBinding.objects.create(
            shop_name=SHOP_A, app_key="AK-A", app_secret=SECRET_A
        )

        # B 侧：**故意不给凭据**——这样无论 `a_row.pk` 在 B 里是否命中 B 自己的行，
        # 结果都必须是「没有可用配置」（1015）。判据不是「B 找不到那一行」，
        # 而是**上游一次都没被调用**（= A 的密钥没有在 B 的请求里被用掉）。
        with tenant_context(self.tenant_b):
            XgjShopBinding.objects.create(shop_name=SHOP_B, app_key="", app_secret="")
            self.client_b = make_logged_in_client(domain=DOMAIN_B, username="xgj-iso-b")

    def test_a_row_is_invisible_in_b_and_b_row_is_visible(self):
        listed_a = self.client_a.get(URL).json()
        self.assertEqual(listed_a["code"], 0)
        self.assertEqual([i["shop_name"] for i in listed_a["data"]["items"]], [SHOP_A])

        try:
            res_b = self.client_b.get(URL, {}, SERVER_NAME=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

        body_b = res_b.json()
        self.assertEqual(body_b["code"], 0)
        names_b = [i["shop_name"] for i in body_b["data"]["items"]]
        # 非空哨兵：B 里看得到**自己**那一行（否则空列表能让下面的否定断言假通过）
        self.assertEqual(names_b, [SHOP_B])
        # 否定断言：A 的业务值与密钥都不出现
        raw = res_b.content.decode()
        self.assertNotIn(SHOP_A, raw)
        self.assertNotIn(SECRET_A, raw)

    def test_b_cannot_use_a_credentials_through_order_detail(self):
        """B 拿 A 那一行的主键去查订单 ⇒ `1015`，且**一次上游请求都没有发**。"""
        try:
            with mock.patch("apps.business.api.xgj_views.order_detail") as upstream:
                res_b = self.client_b.post(
                    ORDER_DETAIL_URL,
                    json.dumps({"binding": self.a_row.pk, "order_no": "123"}),
                    content_type="application/json",
                    SERVER_NAME=DOMAIN_B,
                )
        finally:
            connection.set_tenant(self.tenant)

        self.assertEqual(res_b.json()["code"], 1015)
        self.assertFalse(
            upstream.called,
            "B 的请求把 A 的 AppKey/AppSecret 用掉了——这是跨租户凭据泄漏（AGENTS.md §4）",
        )

    def test_b_cannot_use_a_credentials_through_test_connection(self):
        """「测试连接」同样按本租户的 queryset 取行（spec §9-S3 点名了**两个**端点）。

        这条端点比"查详情"更像一条**拿我们当跳板去打上游**的通道（它会把应用能操作的店铺列出来），
        所以缺了它最不该。判据与上一条同款：**上游一次都没被调用**。

        ⚠️ 不断言具体业务码：B 的表里**也有**主键 1（两个 schema 的序列各自从 1 开始，
        AGENTS.md §5 那条），所以 `a_row.pk` 在 B 里可能命中 **B 自己那一行**（那条没有凭据 ⇒ 1015），
        也可能什么都不命中（⇒ 404 的信封码）。**两种都不是泄漏**，承重判据只有下面那句
        `assertFalse(upstream.called)`。
        """
        try:
            with mock.patch("apps.business.api.xgj_views.shop_list") as upstream:
                res_b = self.client_b.post(
                    f"{URL}{self.a_row.pk}/test-connection/",
                    {},
                    SERVER_NAME=DOMAIN_B,
                )
        finally:
            connection.set_tenant(self.tenant)

        self.assertNotEqual(res_b.json()["code"], 0, "B 不该能对 A 的绑定做任何事")
        # ⚠️ 主判据是「**用到的密钥不是 A 的**」，不是"一次都没调"：
        # 两个 schema 的主键都从 1 开始（本文件 setUp 里 B 也有一行），所以 `a_row.pk` 在 B 里
        # 可能命中 B **自己**那一行。今天 B 那行**故意没有凭据**（setUp），于是"没调用"成立；
        # 哪天有人把 fixture 做得更真实（给 B 填上凭据），"没调用"就会**假红**。
        # 把判据写成"密钥不是 A 的"之后，两种情况都成立（附加断言仍然保留，它更强）。
        if upstream.called:
            self.assertNotEqual(
                upstream.call_args.kwargs["app_secret"],
                SECRET_A,
                "B 的请求把 A 的 AppSecret 用掉了——跨租户凭据泄漏（AGENTS.md §4）",
            )
        self.assertFalse(
            upstream.called,
            "B 的请求把 A 的 AppKey/AppSecret 用掉了——这是跨租户凭据泄漏（AGENTS.md §4）",
        )


class XgjShopBindingAuditTests(TenantTestCase):
    """⭐ Task L（2026-09-22 合并 `main`）：闲管家店铺对接三条写端点的**操作日志埋点**。

    这三条是 `main` 新上线的写端点，覆盖率网当场为它们变红
    （`test_every_enumerated_write_endpoint_is_classified`）⇒ 按「不许放宽判据」补埋点。
    本类判据打**数据库行**（E-0009），期望值写字面值（E-0024）。

    ⚠️ **凭据纪律是本类的重点**：`app_key` / `app_secret` 是**租户凭据**，
    一旦进日志列（`reason` / `to_value` / …）就等于把凭据抄进了可查询的明细表，
    且 `?actor=` 那类检索会把它带出来（与阿奇索 `ShopBinding` 的
    `test_no_credential_reaches_any_log_column` 同款）。
    """

    def setUp(self):
        self.client = make_logged_in_client(username="xgj-audit")

    def _rows(self):
        return list(OperationLog.objects.order_by("id"))

    def test_create_update_delete_each_write_one_registered_row(self):
        res = self.client.post(
            URL,
            json.dumps(
                {"shop_name": SHOP_A, "app_key": "AK-1", "app_secret": SECRET_A,
                 "remark": "备注甲"}
            ),
            content_type="application/json",
        )
        self.assertEqual(res.json()["code"], 0, res.content)
        pk = res.json()["data"]["id"]

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "system.xgj_binding_create")
        self.assertEqual(rows[0].log_type, 40)
        self.assertEqual(rows[0].target_kind, "xgj_binding")
        self.assertEqual(rows[0].target_id, str(pk))
        self.assertEqual(rows[0].target_name, SHOP_A)
        self.assertEqual(rows[0].reason, "备注甲")
        self.assertEqual(rows[0].actor, "xgj-audit")
        self.assertEqual(rows[0].summary, "【xgj-audit】新增闲管家店铺对接：A店-只应出现在A，备注：备注甲")

        res = self.client.patch(
            URL + f"{pk}/", json.dumps({"remark": "备注乙"}),
            content_type="application/json",
        )
        self.assertEqual(res.json()["code"], 0, res.content)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].action, "system.xgj_binding_update")
        self.assertEqual(rows[1].reason, "备注乙")

        res = self.client.delete(URL + f"{pk}/")
        # ⚠️ `EnvelopeModelViewSet.destroy` 回的是**信封**（200 + `code 0`），不是 204。
        self.assertEqual(res.status_code, 200, res.content[:200])
        self.assertEqual(res.json()["code"], 0, res.content[:200])
        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2].action, "system.xgj_binding_destroy")
        self.assertEqual(rows[2].target_name, SHOP_A)

    def test_sync_shops_writes_its_own_row_and_never_the_credentials(self):
        """⭐ per-source 线（2026-09-22）：「同步店铺」是**写端点**（落店铺行 + 自动来源），
        所以它有**自己的**动作，而不是借 `system.xgj_binding_update`（那会是一句假陈述：
        这一行本身没被改）。

        ⚠️ 判据打**数据库行**（E-0009），期望值写字面值（E-0024）；并逐列断言凭据不出现
        ——`reason` 是自由文本列，一旦有人把 `app_secret` 拼进那串计数里，这条会红。
        """
        res = self.client.post(
            URL,
            json.dumps({"shop_name": SHOP_A, "app_key": "AK-1", "app_secret": SECRET_A}),
            content_type="application/json",
        )
        pk = res.json()["data"]["id"]
        self.assertEqual(len(self._rows()), 1, "前置：建这一行本身会记一条 create")

        with mock.patch(
            "apps.business.xgj_shop.shop_list",
            return_value=[{"user_identity": "ID-A", "shop_name": "甲店", "user_name": "m"}],
        ):
            res = self.client.post(f"{URL}{pk}/sync-shops/")
        self.assertEqual(res.json()["code"], 0, res.content)

        rows = self._rows()
        self.assertEqual(len(rows), 2, "同步必须自己记一行")
        row = rows[1]
        self.assertEqual(row.action, "system.xgj_binding_sync_shops")
        self.assertEqual(row.log_type, 40)
        self.assertEqual(row.target_kind, "xgj_binding")
        self.assertEqual(row.target_id, str(pk))
        self.assertEqual(row.target_name, SHOP_A)
        self.assertEqual(row.actor, "xgj-audit")
        # 计数进 `reason`（句子只渲染 名 + 备注）
        self.assertEqual(row.reason, "新增店铺 1 家 / 更新 0 家 / 新建来源 1 个")
        self.assertEqual(row.summary, f"【xgj-audit】同步闲管家店铺：{SHOP_A}，备注：{row.reason}")
        # 凭据纪律：整行任何一列都不许出现 AppKey / AppSecret
        for field in ("reason", "summary", "target_name", "from_value", "to_value", "field_name"):
            self.assertNotIn(SECRET_A, getattr(row, field) or "", field)
            self.assertNotIn("AK-1", getattr(row, field) or "", field)

    def test_sync_shops_failure_writes_no_row(self):
        """同步**失败**（上游错误 / 凭据不全）时不留痕：什么都没发生就不该有一条「同步了」的记录。"""
        pk = XgjShopBinding.objects.create(shop_name=SHOP_A, app_key="AK-1").pk  # 凭据不全
        self.assertEqual(len(self._rows()), 0)
        res = self.client.post(f"{URL}{pk}/sync-shops/")
        self.assertEqual(res.json()["code"], 1015)
        self.assertEqual(self._rows(), [], "失败路径不许写「同步了」的假陈述")

    def test_credentials_never_reach_any_log_column(self):
        """`app_key` / `app_secret` **一个字符都不进**日志的任何文本列。"""
        res = self.client.post(
            URL,
            json.dumps(
                {"shop_name": SHOP_A, "app_key": "AK-1", "app_secret": SECRET_A,
                 "remark": "普通备注"}
            ),
            content_type="application/json",
        )
        pk = res.json()["data"]["id"]
        self.client.patch(URL + f"{pk}/", json.dumps({"remark": "改过"}),
                          content_type="application/json")

        blob = " | ".join(
            " ".join(str(getattr(row, field.name)) for field in row._meta.fields)
            for row in self._rows()
        )
        self.assertNotIn(SECRET_A, blob)
        self.assertNotIn("AK-1", blob)

    def test_the_two_read_only_endpoints_write_nothing(self):
        """`test-connection` 与 `xgj/order-detail` **只读上游、不落库**⇒ 不写日志行。

        它们登记在 `DELIBERATELY_NOT_LOGGED` 里（spec §5.7 第 5 行，与
        `read-platform-order/` 同类）。本条把「不写」也钉住——**豁免必须真的有豁免的形状**。
        """
        row = XgjShopBinding.objects.create(
            shop_name=SHOP_A, app_key="AK-1", app_secret=SECRET_A
        )
        with mock.patch(
            "apps.business.api.xgj_views.shop_list", return_value=[{"seller_id": "1"}]
        ):
            res = self.client.post(URL + f"{row.pk}/test-connection/")
        self.assertEqual(res.json()["code"], 0, res.content)
        with mock.patch(
            "apps.business.api.xgj_views.order_detail", return_value={"order_no": "1"}
        ):
            res = self.client.post(
                ORDER_DETAIL_URL,
                json.dumps({"binding": row.pk, "order_no": "2226688164543566229"}),
                content_type="application/json",
            )
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertEqual(self._rows(), [])
