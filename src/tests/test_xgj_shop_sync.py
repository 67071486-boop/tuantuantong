"""闲管家店铺同步（`apps/business/xgj_shop.py`）的测试（spec §8）。

桩打在 `apps.business.xgj_shop.shop_list` —— **被查找的那个模块**（E-0029）。
"""
from __future__ import annotations

import time
from unittest import mock

from django_tenants.test.cases import TenantTestCase

from apps.business.models import OrderSource, XgjShop, XgjShopBinding
from apps.business.xgj import XgjError
from apps.business.xgj_shop import source_for_order, sync_shops, unique_source_name

SHOPS = [
    {"user_identity": "ID-A", "shop_name": "甲店", "user_name": "jia"},
    {"user_identity": "ID-B", "shop_name": "乙店", "user_name": "yi"},
]


class XgjShopSyncTests(TenantTestCase):
    def setUp(self):
        self.binding = XgjShopBinding.objects.create(
            shop_name="主应用", app_key="AK", app_secret="SK"
        )

    def _sync(self, rows=SHOPS):
        with mock.patch("apps.business.xgj_shop.shop_list", return_value=rows):
            return sync_shops(self.binding)

    def test_creates_one_source_per_shop_named_after_the_shop(self):
        result = self._sync()
        self.assertEqual(result["created_shops"], 2)
        self.assertEqual(result["created_sources"], 2)
        # 来源名 = 店铺名称（业务方 R3）
        self.assertEqual(
            sorted(OrderSource.objects.values_list("name", flat=True)), ["乙店", "甲店"]
        )
        self.assertEqual(
            sorted(XgjShop.objects.values_list("user_identity", flat=True)), ["ID-A", "ID-B"]
        )
        # 每个来源都被真正挂到店上（不是建完就断链）
        for shop in XgjShop.objects.all():
            self.assertIsNotNone(shop.order_source_id)
            self.assertEqual(shop.order_source.shop_type, 20)  # 闲鱼（字面值，不用常量）

    def test_is_idempotent(self):
        self._sync()
        second = self._sync()
        self.assertEqual(second["created_shops"], 0)
        self.assertEqual(second["created_sources"], 0)
        self.assertEqual(second["updated_shops"], 2)
        self.assertEqual(XgjShop.objects.count(), 2)
        self.assertEqual(OrderSource.objects.count(), 2)

    def test_renaming_a_shop_does_not_add_a_source(self):
        """R6：店名变了 ⇒ 还是同一家店、同一个来源，**来源名保持旧名**。"""
        self._sync()
        old_source_id = XgjShop.objects.get(user_identity="ID-A").order_source_id
        renamed = [
            {"user_identity": "ID-A", "shop_name": "甲店改名了", "user_name": "jia"},
            {"user_identity": "ID-B", "shop_name": "乙店", "user_name": "yi"},
        ]
        result = self._sync(renamed)
        self.assertEqual(result["created_sources"], 0)
        self.assertEqual(OrderSource.objects.count(), 2, "改名不该多出来源")
        shop = XgjShop.objects.get(user_identity="ID-A")
        self.assertEqual(shop.order_source_id, old_source_id, "还是原来那个来源")
        self.assertEqual(shop.order_source.name, "甲店", "来源名保持旧名（R6 要的就是这个）")
        self.assertEqual(shop.shop_name, "甲店改名了", "快照要跟上（只是来源名不动）")

    def test_a_new_shop_after_binding_gets_its_own_source(self):
        """R7：绑定之后新开的店，同步时自动建来源。"""
        self._sync()
        result = self._sync(SHOPS + [{"user_identity": "ID-C", "shop_name": "丙店"}])
        self.assertEqual(result["created_shops"], 1)
        self.assertEqual(result["created_sources"], 1)
        self.assertEqual(OrderSource.objects.count(), 3)

    def test_name_collision_gets_a_suffix_and_does_not_share_a_source(self):
        """D5：同名不共用（我们按标识认店）——第二家拿 `甲店(2)`。"""
        OrderSource.objects.create(name="甲店", shop_type=20)  # 手工建的同名来源
        self._sync(SHOPS)
        names = set(OrderSource.objects.values_list("name", flat=True))
        self.assertEqual(names, {"甲店", "甲店(2)", "乙店"})
        # 两家店各挂各的来源，没有共用
        self.assertEqual(len({s.order_source_id for s in XgjShop.objects.all()}), 2)

    def test_deleted_source_gets_rebuilt_on_next_sync(self):
        """D1/D9：来源被租户删掉后，下一次同步**重建**并回填（不把这家店忘掉）。"""
        self._sync()
        shop = XgjShop.objects.get(user_identity="ID-A")
        shop.order_source.delete()
        shop.refresh_from_db()
        self.assertIsNone(shop.order_source_id)
        self._sync()
        shop.refresh_from_db()
        self.assertIsNotNone(shop.order_source_id)
        self.assertEqual(shop.order_source.name, "甲店")

    def test_rows_without_identity_are_skipped_not_guessed(self):
        """没有 `user_identity` 就没法认店 ⇒ **跳过**，不拿名字去猜（否则改名即断链）。"""
        result = self._sync([{"shop_name": "没有标识的店"}])
        self.assertEqual(result["created_shops"], 0)
        self.assertEqual(XgjShop.objects.count(), 0)
        self.assertEqual(OrderSource.objects.count(), 0)

    def test_upstream_failure_is_not_swallowed(self):
        """同步失败必须抛出去——吞掉会让"同步失败"看起来像"成功但一家店都没有"。"""
        with mock.patch(
            "apps.business.xgj_shop.shop_list",
            side_effect=XgjError(upstream_code=100001, upstream_msg="签名错误"),
        ):
            with self.assertRaises(XgjError):
                sync_shops(self.binding)

    def test_unique_source_name_falls_back_when_exhausted(self):
        """同名把 `(2)`…`(N)` 用完之后仍要能建出来（不能死循环、不能重名）。"""
        OrderSource.objects.create(name="拥挤", shop_type=20)
        for n in range(2, 52):
            OrderSource.objects.create(name=f"拥挤({n})", shop_type=20)
        self.assertEqual(unique_source_name("拥挤"), "拥挤#1")

    # ── R10/D11：订购状态快照 ──────────────────────────────────

    def test_second_sync_refreshes_the_subscription_snapshot(self):
        """**已存在**的店铺行也要跟上订购状态（D11 的标黄靠它）。

        ⚠️ 这条是**变异验证找出来的缺口**：把 `sync_shops` 更新分支里那两行
        （`if is_valid is not None: shop.is_valid = …`）删掉时，**当时没有任何用例变红**
        ——因为既有用例走的都是"第一次创建"那条路（创建时字段是直接带上的）。
        而现实里订购到期 / 续费发生在**第二次同步**上，所以这条必须有。
        """
        self._sync([{"user_identity": "ID-A", "shop_name": "甲店", "is_valid": True,
                     "valid_end_time": int(time.time()) + 86400}])
        shop = XgjShop.objects.get(user_identity="ID-A")
        self.assertIs(shop.is_valid, True)

        self._sync([{"user_identity": "ID-A", "shop_name": "甲店", "is_valid": False,
                     "valid_end_time": 0}])
        shop.refresh_from_db()
        self.assertIs(shop.is_valid, False, "第二次同步必须把订购状态刷下来")
        self.assertEqual(shop.valid_end_time, 0)

    def test_a_sync_row_without_subscription_fields_keeps_the_old_snapshot(self):
        """上游这次**没给**这两个键 ⇒ 保留旧值，**不是**把它们清成"无数据"。

        ⚠️ 判据必须用 `is not None`（AGENTS.md §5 / E-0021）：写成真值的话，
        `is_valid=False`（"订购无效"）会被当成"没给"而**丢掉** ⇒ 标黄的那一行会自己变绿。
        """
        self._sync([{"user_identity": "ID-A", "shop_name": "甲店", "is_valid": False,
                     "valid_end_time": 0}])
        self._sync([{"user_identity": "ID-A", "shop_name": "甲店"}])
        shop = XgjShop.objects.get(user_identity="ID-A")
        self.assertIs(shop.is_valid, False, "上游没给这个键 ≠ 把已存的 False 丢掉")
        self.assertEqual(shop.valid_end_time, 0)

    # ── 来源名必须塞得进 `varchar(10)`（修：长店名把「同步店铺」打成 500）────

    def test_a_shop_name_longer_than_the_column_is_clipped_not_a_500(self):
        """店名超过来源名列宽（**10** 个字）⇒ **截断落库**，⛔ 不是 `DataError` 打成 500。

        ⚠️ 期望值写**字面量 10**、不拿 `OrderSource._meta` 现算：现算会让「截断被删掉」时
        **恒绿**（E-0024 族；先例 `tests/test_jidanpu_upstream_record.py` 的同一句注释）。

        线上原样（实测）：某租户的**第二个**闲管家应用里有家店名 **14 个字**，
        点「同步店铺」当场 500，而同一批里前几家店（名字短）**已经落库** ⇒ 半成品状态。
        """
        long_name = "测试店铺名字超过十个字了"  # 12 个字
        result = self._sync([{"user_identity": "ID-LONG", "shop_name": long_name}])
        self.assertEqual(result["created_shops"], 1)
        self.assertEqual(result["created_sources"], 1)
        shop = XgjShop.objects.get(user_identity="ID-LONG")
        self.assertIsNotNone(shop.order_source_id, "⛔ 不是只建了店、没挂来源")
        self.assertEqual(shop.order_source.name, "测试店铺名字超过十个", "前 10 个字")
        self.assertEqual(len(shop.order_source.name), 10)

    def test_two_long_names_sharing_a_prefix_stay_distinct(self):
        """两家店名前 10 个字相同 ⇒ 截断之后**仍然分得开**（唯一性靠后缀，⛔ 后缀不许被裁）。"""
        self._sync([
            {"user_identity": "ID-P1", "shop_name": "测试店铺名字超过十个甲"},
            {"user_identity": "ID-P2", "shop_name": "测试店铺名字超过十个乙"},
        ])
        names = sorted(OrderSource.objects.values_list("name", flat=True))
        self.assertEqual(len(names), 2, "两家店必须各有一个来源")
        self.assertEqual(len(set(names)), 2, "⛔ 截断之后不许撞名")
        self.assertEqual(len({s.order_source_id for s in XgjShop.objects.all()}), 2,
                         "也不许两家店共用一个来源")
        for name in names:
            self.assertLessEqual(len(name), 10)

    def test_long_seller_name_fallback_is_clipped_not_a_500(self):
        """**读单**那条兜底路径同样要裁：订单里的卖家会员名超过 10 个字 ⇒ 截断，⛔ 不 500。

        这条比「同步店铺」更影响日常——客服读一张「店铺不在授权列表里」的单子就走这里。
        """
        with mock.patch("apps.business.xgj_shop.shop_list", return_value=[]):
            source = source_for_order(
                self.binding,
                {"seller_eid": "ID-NEW", "seller_name": "测试会员名字超过十个字了"},  # 12 个字
            )
        self.assertEqual(source.name, "测试会员名字超过十个")
        self.assertEqual(len(source.name), 10)

    def test_long_seller_name_without_identity_reuses_one_source(self):
        """没有标识、只有长会员名时，**同一张单读两次也只建一个来源**。

        ⚠️ 这条钉的是「**匹配与创建必须用同一把尺子**」：只裁创建、不裁查重的话，第二次
        找不到第一次那条（名字对不上）⇒ 每读一次再建一个，来源字典**无界增长**，
        而症状只是「列表里多了一堆同名项」，**不会报错**。
        """
        detail = {"seller_name": "测试会员名字超过十个字了"}
        first = source_for_order(self.binding, detail)
        second = source_for_order(self.binding, detail)
        self.assertEqual(first.pk, second.pk, "同一张单读两次必须复用同一个来源")
        self.assertEqual(OrderSource.objects.count(), 1)
        self.assertEqual(len(first.name), 10)
