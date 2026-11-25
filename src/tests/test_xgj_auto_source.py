"""「一个来源 = 一家店」这一轮的承重测试（spec per-source §8）。

三组分开读：

- `XgjShopRemovalTests`：旧字段真被删掉（D3）+ 保存后自动同步（R5）+ 两个新端点（§5）；
- `XgjAutoDetectTests`：录单**没给来源**时的自动判定（R2/R7/D8/D9）；
- `XgjAutoDetectIsolationTests`：自动判定也不能跨租户借凭据（`AGENTS.md` §4 的红线）。

**桩打在哪里**（E-0029：打在「被查找的那个模块」上）：
- `apps.business.xgj_shop.shop_list`（`sync_shops` 里 `from ... import` 进来的名字）；
- `apps.business.api.platform_order_views.xgj_order_detail`（读单那条路的名字）；
- `apps.business.api.xgj_views.order_detail`（查详情那个动作的名字）。
"""
from __future__ import annotations

import json
import time
from unittest import mock

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import OrderSource, XgjShop, XgjShopBinding
from apps.business.xgj import XgjError
from apps.business.xgj_shop import _xgj_bool, _xgj_seconds, subscription_warning, sync_shops
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

URL = "/api/business/xgj-bindings/"
READ_URL = "/api/business/read-platform-order/"

DOMAIN_A = "tenant.test.com"
DOMAIN_B = "xgj-auto-iso-b.test.local"
SCHEMA_B = "xgj_auto_iso_b"

#: 判据用的业务值：**租户内唯一、且两租户取值不同**（`AGENTS.md` §5：不用主键指认跨租户的行）。
SHOP_A = "A店-只应出现在A"
SECRET_A = "SK-only-in-a"
SHOP_B = "B店-只应出现在B"
SECRET_B = "SK-only-in-b"


def _shops_payload():
    """一份正常的上游店铺列表（店铺名与会员名**刻意不同**：来源名该取店名，不是会员名）。"""
    return [
        {"user_identity": "ID-A", "shop_name": "甲店", "user_name": "member-a", "is_valid": True,
         "valid_end_time": int(time.time()) + 86400},
        {"user_identity": "ID-B", "shop_name": "乙店", "user_name": "member-b", "is_valid": False,
         "valid_end_time": 0},
    ]


class XgjShopRemovalTests(TenantTestCase):
    """旧字段拆除（D3）＋ 保存自动同步（R5）＋ 两个新端点（§5）。"""

    def setUp(self):
        self.client = make_logged_in_client(username="xgj-per-source")
        patcher = mock.patch("apps.business.xgj_shop.shop_list", return_value=_shops_payload())
        self.shop_list = patcher.start()
        self.addCleanup(patcher.stop)

    def _post_binding(self, **extra):
        payload = {"shop_name": "主应用", "app_key": "AK-1", "app_secret": SECRET_A}
        payload.update(extra)
        return self.client.post(URL, json.dumps(payload), content_type="application/json")

    # ── D3：字段真被删掉 ────────────────────────────────────────

    def test_order_source_field_is_gone_from_the_binding_model(self):
        """`XgjShopBinding.order_source` **真的**没了（不是"不再使用"）。

        ⚠️ 判据是模型的字段表本身：只删序列化器/视图字段、把列留在库里，
        下一个读者会以为"还能按来源反查应用"——而那正是本轮要拆掉的错模型。
        """
        with self.assertRaises(FieldDoesNotExist):
            XgjShopBinding._meta.get_field("order_source")
        # 反向也给一次：`OrderSource` 上不该再留着 `xgj_bindings` 这个反查名
        self.assertFalse(hasattr(OrderSource, "xgj_bindings"))
        # 正向哨兵：店铺那一侧的字段**还在**（拆过头会把整条链拆断）
        self.assertIsNotNone(XgjShop._meta.get_field("order_source"))

    # ── R5：保存后自动同步一次 ──────────────────────────────────

    def test_create_auto_syncs_shops_and_sources(self):
        """绑定应用后**自动同步一次**：店铺行与来源都建好，响应里带 `sync` 计数（R5）。"""
        body = self._post_binding().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"]["sync"],
            {"total": 2, "created_shops": 2, "updated_shops": 0, "created_sources": 2},
        )
        self.assertEqual(sorted(XgjShop.objects.values_list("shop_name", flat=True)), ["乙店", "甲店"])
        # 来源名 = **店铺名**（不是会员名）
        self.assertEqual(sorted(OrderSource.objects.values_list("name", flat=True)), ["乙店", "甲店"])

    def test_patch_auto_syncs_too(self):
        """「重存一次那一行」也要能重建「店 ↔ 来源」（升级步骤里写的就是这条）。"""
        self._post_binding()
        pk = XgjShopBinding.objects.get().pk
        XgjShop.objects.all().delete()
        OrderSource.objects.all().delete()
        with mock.patch("apps.business.xgj_shop.shop_list", return_value=_shops_payload()):
            body = self.client.patch(
                f"{URL}{pk}/", json.dumps({"remark": "重存"}), content_type="application/json"
            ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["sync"]["created_shops"], 2)
        self.assertEqual(XgjShop.objects.count(), 2)

    def test_save_without_credentials_skips_sync_without_calling_upstream(self):
        """凭据不全 ⇒ **不发上游请求**，只回一句说明（否则每次"只改备注"都白跑一次计费接口）。"""
        with mock.patch("apps.business.xgj_shop.shop_list") as upstream:
            res = self.client.post(
                URL,
                json.dumps({"shop_name": "半配的", "app_key": "AK-1"}),
                content_type="application/json",
            )
        # `validate_app_key` 只要求 AppKey 非空 ⇒ 这一行能存下来
        self.assertEqual(res.json()["code"], 0, res.content)
        body = res.json()
        self.assertIn("skipped", body["data"]["sync"])
        self.assertFalse(upstream.called, "凭据不全时不得发上游请求")

    def test_save_survives_a_failed_sync_and_reports_it(self):
        """同步失败**不回滚保存**：行照存，`sync` 里带错误码（前端提示「已保存，但同步失败」）。

        ⚠️ 判据里的 `code` 是**上游鉴权类**（1016）而不是 1015：说明走的是上游错误映射，
        不是"没配置"。
        """
        with mock.patch(
            "apps.business.xgj_shop.shop_list",
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            body = self._post_binding().json()
        self.assertEqual(body["code"], 0, body)
        self.assertTrue(XgjShopBinding.objects.exists(), "行必须存下来（凭据写错了要能改）")
        self.assertEqual(body["data"]["sync"]["code"], 1016)
        self.assertEqual(body["data"]["sync"]["error"], "店铺同步失败")

    def test_deleting_and_re_adding_a_binding_reuses_the_same_sources(self):
        """删掉对接再重新对接 ⇒ 同一家店**复用**原来的来源，不出现「甲店(2)」。

        ⚠️ 独立评审的 Important（实测复现）：`XgjShop.binding` 原先是 `CASCADE` ⇒ 删应用会
        连店行一起删掉、**把来源留成孤儿**，重新同步时同一家店拿到 `甲店(2)`
        ——历史单挂旧来源、新单挂新来源，「一个来源 = 一家店」当场断链。
        改成 `SET_NULL` 之后店行留着，同步按 `user_identity` 认回它并复用来源。
        """
        self._post_binding()
        self.assertEqual(sorted(OrderSource.objects.values_list("name", flat=True)), ["乙店", "甲店"])

        XgjShopBinding.objects.get().delete()
        # SET_NULL 的关键一半：**店行必须留着**（否则"认回同一家店"无从谈起）
        self.assertEqual(XgjShop.objects.count(), 2)
        self.assertIsNone(XgjShop.objects.order_by("id").first().binding_id)

        # 重新加一行：保存会自动同步（R5）⇒ 复用旧来源，不新建
        again = self._post_binding().json()
        self.assertEqual(again["code"], 0, again)
        self.assertEqual(
            sorted(OrderSource.objects.values_list("name", flat=True)), ["乙店", "甲店"],
            "重新对接不该多出来源（否则出现「甲店(2)」）",
        )
        self.assertEqual(again["data"]["sync"]["created_sources"], 0)
        self.assertEqual(again["data"]["sync"]["created_shops"], 0, "店行也复用")

    # ── §5：两个新端点 ──────────────────────────────────────────

    def test_sync_shops_endpoint_returns_counts(self):
        self._post_binding()
        pk = XgjShopBinding.objects.get().pk
        XgjShop.objects.all().delete()
        OrderSource.objects.all().delete()
        with mock.patch("apps.business.xgj_shop.shop_list", return_value=_shops_payload()):
            body = self.client.post(f"{URL}{pk}/sync-shops/").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["created_shops"], 2)
        self.assertEqual(body["data"]["created_sources"], 2)

    def test_sync_shops_endpoint_is_1015_without_credentials(self):
        pk = XgjShopBinding.objects.create(shop_name="半配的", app_key="AK-1").pk
        with mock.patch("apps.business.xgj_shop.shop_list") as upstream:
            body = self.client.post(f"{URL}{pk}/sync-shops/").json()
        self.assertEqual(body["code"], 1015)
        self.assertFalse(upstream.called)

    def test_sync_shops_endpoint_maps_upstream_failure(self):
        pk = XgjShopBinding.objects.create(
            shop_name="主应用", app_key="AK-1", app_secret=SECRET_A
        ).pk
        with mock.patch(
            "apps.business.xgj_shop.shop_list",
            side_effect=XgjError(upstream_code=100004, upstream_msg="参数错误"),
        ):
            body = self.client.post(f"{URL}{pk}/sync-shops/").json()
        self.assertEqual(body["code"], 1017)

    def test_shops_endpoint_lists_shops_with_source_and_binding(self):
        self._post_binding()
        pk = XgjShopBinding.objects.get().pk
        body = self.client.get(f"{URL}{pk}/shops/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual([i["shop_name"] for i in items], ["甲店", "乙店"])
        first = items[0]
        self.assertEqual(first["user_name"], "member-a")
        self.assertEqual(first["binding"], pk)
        self.assertEqual(first["binding_name"], "主应用")
        # 对应来源：**名字**要与店铺名一致（前端那一列显示的是名字，不是 id）
        self.assertEqual(first["order_source_name"], "甲店")
        self.assertEqual(first["order_source"], OrderSource.objects.get(name="甲店").pk)

    def test_shops_endpoint_flags_the_subscription_state(self):
        """D11 的标黄数据：`is_valid=false` / 订购过期 ⇒ `subscription_warning=True`。

        ⚠️ 这**不是**版本判定（上游不返回版本名），只是"订购状态看起来无效"。
        """
        self._post_binding()
        pk = XgjShopBinding.objects.get().pk
        got = {i["shop_name"]: i for i in self.client.get(f"{URL}{pk}/shops/").json()["data"]["items"]}
        # 甲店：is_valid=True 且有效期在未来 ⇒ 不标黄
        self.assertIs(got["甲店"]["is_valid"], True)
        self.assertIs(got["甲店"]["subscription_warning"], False)
        # 乙店：is_valid=False 且 valid_end_time=0 ⇒ 标黄
        self.assertIs(got["乙店"]["is_valid"], False)
        self.assertIs(got["乙店"]["subscription_warning"], True)


class XgjSubscriptionWarningTests(TenantTestCase):
    """标黄判据的**纯函数**（三态与边界，不然页面上的黄条会凭空出现或凭空消失）。"""

    def _shop(self, **kwargs):
        return XgjShop.objects.create(
            binding=XgjShopBinding.objects.create(shop_name="应用"), user_identity="ID-X",
            shop_name="某店", **kwargs,
        )

    def test_is_valid_false_warns_even_with_a_future_end_time(self):
        shop = self._shop(is_valid=False, valid_end_time=int(time.time()) + 86400)
        self.assertTrue(subscription_warning(shop))

    def test_is_valid_true_but_expired_warns(self):
        shop = self._shop(is_valid=True, valid_end_time=int(time.time()) - 1)
        self.assertTrue(subscription_warning(shop))

    def test_zero_end_time_warns(self):
        """文档示例里"无效订购"给的正是 `0`（`0 <= now` 自然成立）。"""
        shop = self._shop(is_valid=True, valid_end_time=0)
        self.assertTrue(subscription_warning(shop))

    def test_unknown_state_does_not_warn(self):
        """**没数据不等于异常**：`None`/`None` 不标黄（缺数据时标黄是关于钱的错提示）。"""
        shop = self._shop(is_valid=None, valid_end_time=None)
        self.assertFalse(subscription_warning(shop))

    def test_true_with_a_future_end_time_does_not_warn(self):
        shop = self._shop(is_valid=True, valid_end_time=int(time.time()) + 86400)
        self.assertFalse(subscription_warning(shop))

    def test_scalar_parsers_refuse_to_guess(self):
        """上游的布尔/秒级时间戳标量：认不出就回 `None`（**不猜**）。"""
        self.assertIs(_xgj_bool(True), True)
        self.assertIs(_xgj_bool(False), False)
        self.assertIs(_xgj_bool("true"), True)
        self.assertIs(_xgj_bool(0), False)
        self.assertIs(_xgj_bool("说不清"), None)
        self.assertIs(_xgj_bool(None), None)
        self.assertEqual(_xgj_seconds(1689782400), 1689782400)
        self.assertEqual(_xgj_seconds("1689782400"), 1689782400)
        self.assertEqual(_xgj_seconds(0), 0)
        self.assertIs(_xgj_seconds("x"), None)
        self.assertIs(_xgj_seconds(None), None)


class XgjAutoDetectTests(TenantTestCase):
    """录单**没给来源**时的自动判定（per-source spec D6/D7/R7/D8/D9）。"""

    def setUp(self):
        self.client = make_logged_in_client(username="xgj-auto")

    def _binding(self, *, shop_name, app_key, app_secret, identity=None, order_source=None):
        binding = XgjShopBinding.objects.create(
            shop_name=shop_name, app_key=app_key, app_secret=app_secret
        )
        if identity is not None:
            XgjShop.objects.create(
                binding=binding, user_identity=identity, shop_name=f"{shop_name}-店",
                order_source=order_source,
            )
        return binding

    def _read(self, tid="2226688164543566229", **extra):
        """读一次单。`extra` 用来在需要时**显式给来源**（测手动覆盖那条路）。"""
        payload = {"tid": tid}
        payload.update(extra)
        return self.client.post(READ_URL, json.dumps(payload), content_type="application/json")

    def test_hits_the_first_app_that_can_read_and_stops(self):
        """命中即停：第一个读得到的应用就是答案，**后面的一个都不试**（闲管家按次计费）。"""
        first = self._binding(shop_name="读不到", app_key="AK-读不到", app_secret="SK-1")
        second = self._binding(
            shop_name="能读到", app_key="AK-能读到", app_secret="SK-2", identity="ID-HIT"
        )
        third = self._binding(shop_name="不该被试", app_key="AK-不该被试", app_secret="SK-3")
        # 第二家店预先挂好来源 ⇒ 自动判定不该另建来源
        source = OrderSource.objects.create(name="既有来源", shop_type=20)
        XgjShop.objects.filter(user_identity="ID-HIT").update(order_source=source)

        detail = {"order_no": "222…", "pay_amount": 12345, "seller_eid": "ID-HIT",
                  "seller_name": "member-hit"}
        calls = [XgjError(upstream_code=100004, upstream_msg="订单不存在"), detail]
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", side_effect=calls
        ) as upstream:
            body = self._read().json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(upstream.call_count, 2, "命中之后不许再试第三个应用")
        self.assertEqual(upstream.call_args.kwargs["app_key"], "AK-能读到")
        # 用的是**第二个**应用的凭据，不是第一个的
        self.assertNotEqual(upstream.call_args.kwargs["app_secret"], "SK-1")
        self.assertEqual(body["data"]["platform"], "xgj")
        self.assertEqual(body["data"]["order_source"], source.pk)
        self.assertEqual(body["data"]["order_source_name"], "既有来源")
        self.assertEqual(body["data"]["payment"], "123.45")
        self.assertEqual(OrderSource.objects.count(), 1, "已经有来源了，不该再建一个")
        # 前置哨兵：第三个应用确实配了凭据（否则"没试它"可能只是它没凭据）
        self.assertIsNotNone(third.app_secret)
        self.assertIn(first.pk, [b.pk for b in XgjShopBinding.objects.all()])

    def test_auto_detect_skips_apps_without_credentials(self):
        """没填完凭据的应用**不发请求**（发出去只会拿到 100001，白花一次调用）。"""
        self._binding(shop_name="半配的", app_key="AK-1", app_secret="")
        hit = self._binding(
            shop_name="完整的", app_key="AK-2", app_secret="SK-2", identity="ID-HIT"
        )
        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-HIT"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ) as upstream:
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(upstream.call_args.kwargs["app_key"], "AK-2")
        self.assertEqual(XgjShop.objects.get(user_identity="ID-HIT").binding_id, hit.pk)

    def test_no_hit_is_1015_and_tells_the_user_to_pick_a_source(self):
        """一个都没命中 ⇒ `1015`，msg 必须提示**手动选择订单来源**（租户唯一的出路）。"""
        self._binding(shop_name="甲", app_key="AK-1", app_secret="SK-1")
        self._binding(shop_name="乙", app_key="AK-2", app_secret="SK-2")
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            side_effect=XgjError(upstream_code=100004, upstream_msg="订单不存在"),
        ) as upstream:
            body = self._read().json()
        self.assertEqual(body["code"], 1015)
        self.assertIn("请手动选择订单来源", body["msg"])
        self.assertEqual(upstream.call_count, 2, "两个应用都要试过")
        self.assertEqual(OrderSource.objects.count(), 0, "没命中就不该建来源")

    def test_auto_detect_does_not_walk_agiso(self):
        """D7：自动判定**只走闲管家**（业务方四问 ④：阿奇索按次收费，不盲目遍历）。

        ⚠️ 判据是「阿奇索客户端**一次都没被调用**」，而不是"结果对"——
        一个不知道自己在收费的遍历实现也会回 1015。
        """
        from apps.business.models import ShopBinding

        source = OrderSource.objects.create(name="阿奇索来源", shop_type=20)
        ShopBinding.objects.create(order_source=source, access_token="tok")
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail",
            side_effect=XgjError(upstream_code=100004, upstream_msg="订单不存在"),
        ), mock.patch("apps.business.api.platform_order_views.order_detail") as agiso:
            body = self._read().json()
        self.assertEqual(body["code"], 1015)
        self.assertFalse(agiso.called, "自动判定不许遍历阿奇索（它按次收费）")

    def test_unknown_shop_triggers_one_on_demand_sync(self):
        """R7：录单遇到未知店 ⇒ **按需同步一次**，用店铺名（不是会员名）建来源。"""
        self._binding(shop_name="应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-NEW",
                  "seller_name": "member-new"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ), mock.patch(
            "apps.business.xgj_shop.shop_list",
            return_value=[{"user_identity": "ID-NEW", "shop_name": "新店", "user_name": "member-new"}],
        ) as sync:
            body = self._read().json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(sync.call_count, 1, "按需同步只跑一次")
        shop = XgjShop.objects.get(user_identity="ID-NEW")
        self.assertEqual(shop.shop_name, "新店")
        self.assertEqual(shop.order_source.name, "新店", "来源名取店铺名（R3）")
        self.assertEqual(body["data"]["order_source"], shop.order_source_id)

    def test_sync_that_still_misses_falls_back_to_the_member_name(self):
        """D8：按需同步之后仍找不到 ⇒ 用订单里的 `seller_name` 兜底建来源，并**记住这家店**。

        ⚠️ 「记住」这半条是**必须**的：只建来源不建店行的话，同一家店的下一张单会再建一个来源，
        「一个来源 = 一家店」当场失效。这条用例的第二段就是在钉这件事。
        """
        self._binding(shop_name="应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-GONE",
                  "seller_name": "会员名兜底"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ), mock.patch(
            "apps.business.xgj_shop.shop_list", return_value=[]  # 授权列表里没有这家店
        ):
            first = self._read().json()
            second = self._read().json()

        self.assertEqual(first["code"], 0, first)
        self.assertEqual(OrderSource.objects.filter(name="会员名兜底").count(), 1)
        # 第二张单**复用**同一个来源（不是又建一个）
        self.assertEqual(second["data"]["order_source"], first["data"]["order_source"])
        self.assertEqual(OrderSource.objects.count(), 1, "兜底来源必须是幂等的")
        shop = XgjShop.objects.get(user_identity="ID-GONE")
        self.assertEqual(shop.shop_name, "", "订单里没有店铺名 ⇒ 快照留空，等下次同步补")
        self.assertEqual(shop.user_name, "会员名兜底")

    def test_no_identity_fallback_does_not_hijack_another_shops_source(self):
        """⚠️ 独立评审复核后补的**反向判据**：同名复用**不许劫持**已经挂在别家店上的来源。

        只按名字查（`filter(name=...)`）时，一张**没有标识**的单会拿走另一家已知店的来源
        ——那条路与 spec D5 直接冲突（"按标识认店，不按名字"；共用一个来源会把两家店的
        归属混在一起）。评审实测：`hijacked=True`。
        收紧之后这条兜底只认**自己建过、且还没挂店**的那种来源。
        """
        taken = OrderSource.objects.create(name="撞名了", shop_type=20)
        other = self._binding(
            shop_name="别家应用", app_key="AK-O", app_secret="SK-O",
            identity="ID-OTHER", order_source=taken,
        )
        self.assertEqual(XgjShop.objects.get(user_identity="ID-OTHER").order_source_id, taken.pk)
        self._binding(shop_name="本应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_name": "撞名了"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        # 承重判据：**没有**劫持那家已知店的来源
        self.assertNotEqual(body["data"]["order_source"], taken.pk)
        self.assertEqual(body["data"]["order_source_name"], "撞名了(2)")
        # 非空哨兵：那家店的来源一个字都没动
        self.assertEqual(XgjShop.objects.get(user_identity="ID-OTHER").order_source_id, taken.pk)
        self.assertGreater(other.pk, 0)

    def test_no_identity_fallback_does_not_take_a_manual_same_named_source(self):
        """租户**手工**建的同名来源也不占（只复用带自动标记的那种）。

        ⚠️ 判据是"新建了一个"与"没动那个手工来源"两条——只说"没劫持"的话，
        一个"什么都不做"的实现（比如直接 return 手工来源）也可能蒙混。
        """
        manual = OrderSource.objects.create(name="手工建的", shop_type=20, remark="我自己建的")
        self._binding(shop_name="应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_name": "手工建的"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        self.assertNotEqual(body["data"]["order_source"], manual.pk)
        manual.refresh_from_db()
        self.assertEqual(manual.remark, "我自己建的", "租户手工来源的备注不该被改")
        self.assertEqual(manual.xgj_shops.count(), 0, "手工来源不该被挂到店里")

    def test_an_orphan_shop_row_is_reattached_and_its_source_stays_usable(self):
        """⚠️ 独立评审复核后补：孤儿店行（删过对接、`binding IS NULL`）会被**接回**当前应用。

        不接回的两个后果（评审实测）：① 同一个来源"手动覆盖"回 `1015`、"自动判定"却能读到
        —— 一个来源两种结果；② 它只能等下一次「同步店铺」且上游仍有这家店才恢复，
        那份"短暂"可能是无限期。
        """
        source = OrderSource.objects.create(name="旧来源", shop_type=20)
        old_binding = self._binding(
            shop_name="旧应用", app_key="AK-OLD", app_secret="SK-OLD",
            identity="ID-A", order_source=source,
        )
        shop = XgjShop.objects.get(user_identity="ID-A")
        self.assertEqual(shop.order_source_id, source.pk, "前置：这家店本来挂着来源")
        old_binding.delete()
        shop.refresh_from_db()
        self.assertIsNone(shop.binding_id, "前置：SET_NULL 留下了孤儿店行")

        # 新应用 + 自动判定同一家店的单
        self._binding(shop_name="新应用", app_key="AK-NEW", app_secret="SK-NEW")
        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-A", "seller_name": "m"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        shop.refresh_from_db()
        self.assertIsNotNone(shop.binding_id, "孤儿店行必须被接回当前应用")
        self.assertEqual(body["data"]["order_source"], source.pk, "并且复用原来那个来源")

        # 接回之后，「手动覆盖选了这个来源」也要能读到（修复前这一支回 1015）
        new_binding = XgjShopBinding.objects.get(shop_name="新应用")
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ) as upstream:
            body = self._read(order_source=source.pk).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(upstream.call_args.kwargs["app_key"], "AK-NEW")
        self.assertGreater(new_binding.pk, 0)

    def test_a_failed_on_demand_sync_does_not_lose_the_order(self):
        """按需同步失败 ⇒ **订单照读**（已经拿到了），走兜底路径并记日志。"""
        self._binding(shop_name="应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-X",
                  "seller_name": "会员名兜底"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ), mock.patch(
            "apps.business.xgj_shop.shop_list",
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["payment"], "1.00")
        self.assertEqual(OrderSource.objects.get().name, "会员名兜底")

    def test_order_without_a_seller_eid_still_creates_a_source(self):
        """订单里连标识都没有 ⇒ 认不出店，只能按会员名建来源（并记下这条路径）。"""
        self._binding(shop_name="应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_name": "只有会员名"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(OrderSource.objects.get().name, "只有会员名")
        self.assertEqual(XgjShop.objects.count(), 0, "没有标识就认不出店，不该凭空造一行")

    def test_order_without_a_seller_eid_is_idempotent(self):
        """⚠️ 独立评审的 Important：**同一张没有标识的单读两次，不许建出两个来源**。

        这一支是唯一按**名字**兜底的地方（别处一律按 `user_identity`）。修之前实测
        `sources=['只有会员名', '只有会员名(2)']`——来源字典无界增长，而录单页还会自动
        选中新建的那个，正是本线要消灭的"同一家店散在多个来源"。
        """
        self._binding(shop_name="应用", app_key="AK-1", app_secret="SK-1")
        detail = {"order_no": "1", "pay_amount": 100, "seller_name": "只有会员名"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            first = self._read().json()
            second = self._read().json()
        self.assertEqual(first["code"], 0, first)
        self.assertEqual(
            sorted(OrderSource.objects.values_list("name", flat=True)), ["只有会员名"],
            "同名兜底来源必须复用（否则每读一次多一个来源）",
        )
        self.assertEqual(second["data"]["order_source"], first["data"]["order_source"])

    def test_auto_detect_failure_log_does_not_leak_the_secret(self):
        """自动判定那条路的日志里**不得**出现 AppSecret（spec §9-S1 的红线）。

        ⚠️ 这条是**独立评审找出的覆盖缺口**：作者原先只在**手动**那条路上有一条脱敏用例
        （`test_xgj_shop_binding.py::test_failure_log_redacts_the_app_secret`），
        而自动判定是**新加的一条日志路径**——评审把 `upstream_msg` 原样加进这条日志、
        **60 条用例全绿**。
        当前实现只记 `upstream_code`（连订单号与 binding 一起记，排障够用），所以天然安全；
        这条用例把它**钉住**：谁要把上游原文加进来，就必须先过脱敏器。
        """
        self._binding(shop_name="应用", app_key="AK-1", app_secret=SECRET_A)
        leaky = XgjError(upstream_code=100001, upstream_msg=f"签名错误 app_secret={SECRET_A}")
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", side_effect=leaky
        ), self.assertLogs("apps.business.api.platform_order_views", level="INFO") as logs:
            body = self._read().json()
        self.assertEqual(body["code"], 1015)
        text = "\n".join(logs.output)
        # 非空哨兵：确实记了一条（否则"没出现密钥"可能只是因为压根没日志）
        self.assertIn("自动判定", text)
        self.assertNotIn(SECRET_A, text, "AppSecret 出现在日志里了（spec §9-S1 红线）")

    def test_a_shop_whose_source_was_deleted_gets_it_rebuilt(self):
        """D1/D9：租户把来源删了 ⇒ 下次命中同一家店**重建**来源并回填（不把店忘掉）。"""
        old = OrderSource.objects.create(name="旧来源", shop_type=20)
        binding = self._binding(
            shop_name="应用", app_key="AK-1", app_secret="SK-1", identity="ID-A",
            order_source=old,
        )
        shop = XgjShop.objects.get(user_identity="ID-A")
        self.assertEqual(shop.order_source_id, old.pk, "前置：这一行本来挂着来源")
        shop.order_source.delete()
        shop.refresh_from_db()
        self.assertIsNone(shop.order_source_id)

        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-A", "seller_name": "m"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ):
            body = self._read().json()
        self.assertEqual(body["code"], 0, body)
        shop.refresh_from_db()
        self.assertIsNotNone(shop.order_source_id)
        self.assertEqual(shop.binding_id, binding.pk)
        self.assertEqual(body["data"]["order_source"], shop.order_source_id)

    def test_manual_override_still_wins_over_auto_detect(self):
        """手动覆盖仍生效：给了来源就按它走，**不**去做自动判定（D6 的另一半）。"""
        chosen_source = OrderSource.objects.create(name="手选的来源", shop_type=20)
        self._binding(
            shop_name="手选的应用", app_key="AK-CHOSEN", app_secret="SK-C",
            identity="ID-C", order_source=chosen_source,
        )
        # 另一家能读到的应用（id 更小）——自动判定会先试它；手动覆盖下**一次都不该试**
        other = self._binding(shop_name="别的应用", app_key="AK-OTHER", app_secret="SK-O")
        XgjShop.objects.create(
            binding=other, user_identity="ID-OTHER", shop_name="别的店",
            order_source=OrderSource.objects.create(name="别的来源", shop_type=20),
        )
        detail = {"order_no": "1", "pay_amount": 100, "seller_eid": "ID-C"}
        with mock.patch(
            "apps.business.api.platform_order_views.xgj_order_detail", return_value=detail
        ) as upstream:
            body = self.client.post(
                READ_URL,
                json.dumps({"order_source": chosen_source.pk, "tid": "2226688164543566229"}),
                content_type="application/json",
            ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(upstream.call_args.kwargs["app_key"], "AK-CHOSEN")
        self.assertEqual(body["data"]["order_source"], chosen_source.pk)
        self.assertEqual(body["data"]["order_source_name"], "手选的来源")


class XgjAutoDetectIsolationTests(TenantTestCase):
    """自动判定也不能跨租户借凭据（`AGENTS.md` §4）。

    ⚠️ 第二个租户**必须**在 `setUpClass` 里建（schema DDL 不能在用例事务里回滚）。
    ⚠️ **判据纪律**：不用主键指认「另一个租户的那一行」——两个 schema 的主键各自从 1 开始。
    这里的判据是「**用到的是 B 自己的凭据**」＋「A 的业务值与密钥都不出现」。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="自动判定隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.client_a = make_logged_in_client(domain=DOMAIN_A, username="xgj-auto-a")
        self.a_row = XgjShopBinding.objects.create(
            shop_name=SHOP_A, app_key="AK-A", app_secret=SECRET_A
        )
        # A 也真有一家店（`shops` 端点那条用例需要有东西**可泄漏**，否则"B 没看到 A 的行"
        # 可能只是因为 A 本来就没有行 —— 空集让否定断言恒真）
        XgjShop.objects.create(
            binding=self.a_row, user_identity="ID-A", shop_name=SHOP_A, user_name="member-a"
        )
        with tenant_context(self.tenant_b):
            b_binding = XgjShopBinding.objects.create(
                shop_name=SHOP_B, app_key="AK-B", app_secret=SECRET_B
            )
            XgjShop.objects.create(
                binding=b_binding, user_identity="ID-B", shop_name="B店", user_name="member-b"
            )
            self.client_b = make_logged_in_client(domain=DOMAIN_B, username="xgj-auto-b")

    def test_b_auto_detect_uses_b_credentials_and_never_a(self):
        """B 自动判定时用的必须是 **B 自己的** AppKey/AppSecret，且 A 的业务值不出现。

        ⚠️ 桩让上游失败（`XgjError`）以便看清"用了哪把钥匙"；若上游成功，B 会顺带建来源，
        反而看不清凭据这一层。
        """
        leaked = XgjError(upstream_code=100001, upstream_msg=f"签名错误 app_secret={SECRET_A}")
        try:
            with mock.patch(
                "apps.business.api.platform_order_views.xgj_order_detail", side_effect=leaked
            ) as upstream:
                res_b = self.client_b.post(
                    READ_URL,
                    json.dumps({"tid": "2226688164543566229"}),
                    content_type="application/json",
                    SERVER_NAME=DOMAIN_B,
                )
        finally:
            connection.set_tenant(self.tenant)

        body = res_b.json()
        self.assertEqual(body["code"], 1015)
        self.assertTrue(upstream.called, "B 自己配了完整凭据 ⇒ 应该去试它自己的应用")
        self.assertEqual(upstream.call_args.kwargs["app_key"], "AK-B")
        self.assertEqual(upstream.call_args.kwargs["app_secret"], SECRET_B)
        self.assertNotEqual(upstream.call_args.kwargs["app_secret"], SECRET_A)
        raw = res_b.content.decode()
        self.assertNotIn(SHOP_A, raw)
        self.assertNotIn(SECRET_A, raw)

    def test_b_does_not_see_a_shops_or_sources(self):
        """B 的店铺表里不该出现 A 的行（`shops` 端点）。

        ⚠️ 与 `test_a_row_is_invisible_in_b_and_b_row_is_visible` 同款判据（`AGENTS.md` §5）：
        两个 schema 的主键各自从 1 开始，所以 `a_row.pk` 在 B 里**可能命中 B 自己那一行**
        ⇒ 不断言"404"、也不断言"空"，只断言「**B 自己的**那家店在、A 的业务值与密钥不在」。
        """
        with tenant_context(self.tenant_b):
            b_pk = XgjShopBinding.objects.get().pk
        try:
            shops_b = self.client_b.get(f"{URL}{b_pk}/shops/", SERVER_NAME=DOMAIN_B).json()
        finally:
            connection.set_tenant(self.tenant)

        self.assertEqual(shops_b["code"], 0, shops_b)
        names = [i["shop_name"] for i in shops_b["data"]["items"]]
        # 非空哨兵：B 看得到**自己**那家店（否则空列表能让下面的否定断言假通过）
        self.assertEqual(names, ["B店"])
        raw = json.dumps(shops_b)
        self.assertNotIn(SHOP_A, raw)
        self.assertNotIn(SECRET_A, raw)

    def test_b_cannot_borrow_a_credentials_through_sync_shops(self):
        """B 用 A 那一行的主键去同步 ⇒ 用到的**绝不是 A 的凭据**（新端点也要有这条红线）。

        ⚠️ 承重判据是「**用到的密钥不是 A 的**」，不是"一次都没调"：`a_row.pk` 在 B 里可能命中
        B 自己那一行，而 B 那一行**是有凭据的**（setUp 有意给它配了，用来验"用的是自己那把钥匙"）
        ⇒ 断言"没调用"会假红。B 自己的表里还有一家店，所以同步成功也是允许的。
        """
        try:
            with mock.patch("apps.business.xgj_shop.shop_list", return_value=[]) as upstream:
                res_b = self.client_b.post(
                    f"{URL}{self.a_row.pk}/sync-shops/", SERVER_NAME=DOMAIN_B
                )
        finally:
            connection.set_tenant(self.tenant)

        if upstream.called:
            self.assertEqual(
                upstream.call_args.kwargs["app_secret"], SECRET_B,
                "B 的请求把 A 的 AppSecret 用掉了——跨租户凭据泄漏（AGENTS.md §4）",
            )
            self.assertNotEqual(upstream.call_args.kwargs["app_secret"], SECRET_A)
        else:
            # 没调用也必须是「B 里没有这个 id」这条正当理由，而不是"悄悄用了别人的"
            self.assertIn(res_b.json()["code"], (1015, 4004), res_b.content[:200])
        raw = res_b.content.decode()
        self.assertNotIn(SHOP_A, raw)
        self.assertNotIn(SECRET_A, raw)
