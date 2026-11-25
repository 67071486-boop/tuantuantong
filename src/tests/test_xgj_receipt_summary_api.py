"""闲管家**收款明细读端点**的接口网
（spec `2026-10-22-xgj-receipt-detail-and-finance-merge-design.md` §6 / §9 / §12 的汇总接口网）。

逐条落点（spec → 本文件的用例）：

| 判据 | 用例 |
|---|---|
| 分组（按 `shop_identity` §12 F2-D：店名 / 笔数 / 实收总额 / 缺失笔数） | `SummaryApiTests` |
| 未识别桶（identity 为空的行单独成桶、桶内按 `seller_id` 分、旧三层兜底） | `SummaryApiTests.test_unidentified_rows_group_by_seller_in_their_own_bucket` |
| 排序（实收总额降序；同额 tie-break = 组键升序——顺序断言钉在兜底用例的同额夹具上） | `SummaryApiTests.test_display_name_falls_back_to_snapshot_then_identity` |
| 缺省日期（= 前一天..前一天，本地日） | `SummaryApiTests.test_default_window_is_yesterday` |
| 权限门（401 / 403，节点 `finance.xgj_receipt_detail`） | `PermissionGateTests` |
| **跨租户否定断言**（`AGENTS.md` §4；判据 = 租户内唯一、两租户取值不同的 `shop_identity`） | `CrossTenantTests` |
| 明细分页 / 单店筛选（`shop_identity`，空串 = 未识别桶）/ 金额缺失标记 | `DetailApiTests` |
| 参数校验（畸形日期 / 起止倒置 ⇒ 200 + `1000`，不 500） | `ParamValidationTests` |
| 统计口径（交易成功之后退款不撤销明细——§3 默认） | `SummaryApiTests.test_window_is_by_success_time_not_receipt_time` |

## 两条纪律

- 夹具**一律合成值**（E-0077）：`shop_identity`（详情 `seller_eid` 形状）/ `seller_id`
  （商家级 ID）/ 店名 / 金额全仿形状。
- 判据打在**响应里的值**与**数据库行**上（E-0009）；跨租户断言按 `AGENTS.md` §5-G
  的口径（⛔ 不用主键指认另一租户的行）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import connection
from django.utils import timezone

from apps.business.models import (
    OrderSource,
    OrderSourceGroup,
    XgjReceiptRecord,
    XgjShop,
    XgjShopBinding,
)
from apps.platform.provisioning import provision_tenant
from django_tenants.test.cases import TenantTestCase
from tests.p5_auth_helpers import make_logged_in_client

SUMMARY_URL = "/api/business/xgj-receipts/summary/"
LIST_URL = "/api/business/xgj-receipts/"
DOMAIN_A = "tenant.test.com"

#: 合成店铺标识（详情 `seller_eid` 形状——密文串；**统计分组键**，spec §12 F2-D。
#: 租户内唯一、两租户取值不同 ⇒ 跨租户否定断言的判据字段）。
IDENTITY_A1 = "EID-770000000001"
IDENTITY_B1 = "EID-770000000099"
#: 商家级 `seller_id`（推送原文；F-2 实锤它在 `XgjShop.user_identity` 里零命中，
#: 只留作行级事实、不再当分组键）。
SELLER_A1 = "770000000001"


def _local_day(offset_days: int = 0) -> datetime:
    """当前**本地日**的 `offset` 天偏移（0 点，带时区）——夹具写 `success_time` 用。"""
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    day = today + timedelta(days=offset_days)
    return datetime(day.year, day.month, day.day, tzinfo=tz)


class SummaryFixture(TenantTestCase):
    """登录用户 + 一批合成记账行的夹具。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.binding = XgjShopBinding.objects.create(
            shop_name="汇总测试应用", app_key="AK-SUM-TEST", app_secret="SK-SUM-TEST",
        )
        self.shop_one = XgjShop.objects.create(
            binding=self.binding, user_identity=IDENTITY_A1,
            shop_name="合成店铺·一号", user_name="合成会员一",
        )
        self.client = make_logged_in_client(
            # ⚠️ `nodes=` 只挂到**用户已有的 Group** 对应的 Role 上（`_grant_nodes` 的实现）：
            # 不给 `groups=` 的账号一个组都没有 ⇒ 节点无处可挂、照样 403。
            groups=("客服",), nodes=("finance.xgj_receipt_detail",), domain=DOMAIN_A,
        )

    #: 「没给 `success_time`」的哨兵：`add_row(success_time=NO_TIME)` ⇒ 存 `None`。
    NO_TIME = object()

    def add_row(self, *, seller_id: str, amount, days: int = -1, shop_snapshot: str = "",
                order_no: str = "33000000000000000001", success_time=NO_TIME,
                matched_count: int = 0, match_result=None,
                shop_identity: str | None = None) -> XgjReceiptRecord:
        return XgjReceiptRecord.objects.create(
            binding=self.binding,
            app_key="AK-SUM-TEST",
            order_no=order_no,
            seller_id=seller_id,
            user_name="合成买家",
            shop_name_snapshot=shop_snapshot,
            shop_identity=shop_identity,
            success_time=(
                _local_day(days) + timedelta(hours=12)
                if success_time is self.NO_TIME
                else success_time
            ),
            amount=amount,
            matched_count=matched_count,
            match_result=match_result or [],
        )


class SummaryApiTests(SummaryFixture):
    def test_groups_by_shop_identity_with_amount_desc_and_missing_counts(self):
        """分组 + 排序 + 缺失计数（F2-D）：钱多的店在前；缺失笔数与总笔数能对上（§4-D7）。"""
        # 店一（identity A1）：2 笔有金额（100.00 + 23.45）+ 1 笔缺失 ⇒ 总额 123.45、缺失 1。
        self.add_row(seller_id=SELLER_A1, amount=Decimal("100.00"),
                     shop_identity=IDENTITY_A1, order_no="33000000000000000011")
        self.add_row(seller_id=SELLER_A1, amount=Decimal("23.45"),
                     shop_identity=IDENTITY_A1, order_no="33000000000000000012")
        self.add_row(
            seller_id=SELLER_A1, amount=None, shop_identity=IDENTITY_A1,
            order_no="33000000000000000013", shop_snapshot="旧快照一号店",
        )
        # 店二（identity 不同 ⇒ 另一组，即便 seller_id 同为商家级ID 的同僚）：1 笔 9.90。
        self.add_row(
            seller_id=SELLER_A1, amount=Decimal("9.90"),
            shop_identity="EID-770000000002", order_no="33000000000000000014",
        )
        # 窗口外（今天）的行不进缺省窗口（缺省 = 前一天）。
        self.add_row(seller_id="770000000003", amount=Decimal("999.99"), days=0,
                     shop_identity="EID-770000000003",
                     order_no="33000000000000000015")

        response = self.client.get(SUMMARY_URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        items = body["data"]["items"]
        self.assertEqual(
            [row["shop_identity"] for row in items],
            [IDENTITY_A1, "EID-770000000002"],
        )
        first = items[0]
        self.assertEqual(first["shop_name"], "合成店铺·一号")  # 现解析命中（identity → XgjShop）
        self.assertEqual(first["count"], 3)
        self.assertEqual(first["amount"], "123.45")  # 字符串（⛔ 不是 float）
        self.assertEqual(first["missing_count"], 1)
        second = items[1]
        self.assertEqual(second["count"], 1)
        self.assertEqual(second["amount"], "9.90")
        self.assertEqual(second["missing_count"], 0)
        # 合计块：2 组 / 4 笔 / 133.35 / 缺失 1（**后端**算的，前端不做金额加法）。
        totals = body["data"]["totals"]
        self.assertEqual(totals, {
            "shop_count": 2, "count": 4, "amount": "133.35", "missing_count": 1,
        })

    def test_display_name_falls_back_to_snapshot_then_identity(self):
        """三层兜底（F2-D 后）：现解析不命中 ⇒ 组内快照（最新一行）⇒ 组键原文。"""
        self.add_row(
            seller_id=SELLER_A1, amount=Decimal("5.00"),
            shop_identity="EID-770000000004",
            order_no="33000000000000000021", shop_snapshot="快照兜底店",
        )
        self.add_row(
            seller_id=SELLER_A1, amount=Decimal("5.00"),
            shop_identity="EID-770000000005",
            order_no="33000000000000000022",
        )
        items = self.client.get(SUMMARY_URL).json()["data"]["items"]
        # ⭐ 同额并列的**顺序**断言（tie-break = 组键 `shop_identity` 升序，spec §6）：
        #    上面的 dict 取值对顺序不敏感 ⇒ 没有这一行，「同额 tie-break」就是没覆盖；
        #    把实现换成降序 / 删掉 tie-break，这条当场红（评审 L-1 的承重钉子）。
        self.assertEqual(
            [row["shop_identity"] for row in items],
            ["EID-770000000004", "EID-770000000005"],
        )
        by_identity = {row["shop_identity"]: row for row in items}
        self.assertEqual(by_identity["EID-770000000004"]["shop_name"], "快照兜底店")
        self.assertEqual(by_identity["EID-770000000005"]["shop_name"], "EID-770000000005")

    def test_unidentified_rows_group_by_seller_in_their_own_bucket(self):
        """未识别桶（F2-D）：identity 为空的行**单独成桶**、桶内按 `seller_id` 分，
        展示名走旧三层兜底（快照 → 原始 `seller_id`）；补数完成后该桶自然为空。"""
        # 同一 seller_id 的两行归并成桶里的一行；快照取**非空**的兜底。
        self.add_row(
            seller_id="770000000010", amount=Decimal("5.00"),
            order_no="33000000000000000031",
        )
        self.add_row(
            seller_id="770000000010", amount=Decimal("5.00"),
            order_no="33000000000000000032", shop_snapshot="旧快照一号店",
        )
        # 另一个 seller_id 各自成一行，快照也没有 ⇒ 兜底到原始 seller_id。
        self.add_row(
            seller_id="770000000011", amount=Decimal("9.00"),
            order_no="33000000000000000033",
        )
        body = self.client.get(SUMMARY_URL).json()
        items = body["data"]["items"]
        # 排序：金额降序（桶一 10.00 在桶二 9.00 前面）；未识别行的 `shop_identity` = 空串。
        self.assertEqual(
            [(row["shop_identity"], row["count"], row["shop_name"]) for row in items],
            [
                ("", 2, "旧快照一号店"),
                ("", 1, "770000000011"),
            ],
        )
        self.assertEqual(items[0]["amount"], "10.00")
        self.assertEqual(
            body["data"]["totals"],
            {"shop_count": 2, "count": 3, "amount": "19.00", "missing_count": 0},
        )

    def test_default_window_is_yesterday(self):
        """缺省窗口 = **前一天..前一天**（本地日；今天与更早的行都不进）。"""
        self.add_row(seller_id=SELLER_A1, amount=Decimal("1.00"),
                     shop_identity=IDENTITY_A1, order_no="33000000000000000041")
        self.add_row(seller_id=SELLER_A1, amount=Decimal("2.00"),
                     shop_identity=IDENTITY_A1, order_no="33000000000000000042", days=0)
        self.add_row(seller_id=SELLER_A1, amount=Decimal("4.00"),
                     shop_identity=IDENTITY_A1, order_no="33000000000000000043", days=-2)
        body = self.client.get(SUMMARY_URL).json()
        items = body["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 1)
        self.assertEqual(items[0]["amount"], "1.00")
        # 显式区间把三行都装进来。
        day_from = (timezone.localdate() - timedelta(days=2)).isoformat()
        day_to = timezone.localdate().isoformat()
        body = self.client.get(SUMMARY_URL, {"date_from": day_from, "date_to": day_to}).json()
        self.assertEqual(body["data"]["items"][0]["count"], 3)

    def test_window_is_by_success_time_not_receipt_time(self):
        """统计桶 = `success_time`（交易成功时间，R7）：交易成功在 3 天前 ⇒ 它只属于那一天。"""
        self.add_row(
            seller_id=SELLER_A1, amount=Decimal("7.00"),
            shop_identity=IDENTITY_A1,
            order_no="33000000000000000051",
            success_time=_local_day(-3) + timedelta(hours=9),
        )
        body = self.client.get(
            SUMMARY_URL,
            {"date_from": (timezone.localdate() - timedelta(days=3)).isoformat(),
             "date_to": (timezone.localdate() - timedelta(days=3)).isoformat()},
        ).json()
        self.assertEqual(body["data"]["items"][0]["count"], 1)
        # 缺省窗口（前一天）里没有它。
        body = self.client.get(SUMMARY_URL).json()
        self.assertEqual(body["data"]["items"], [])

    def test_rows_with_null_success_time_belong_to_no_window(self):
        """`success_time` 为空的行不进任何日期窗口（桶键缺失；行还在明细可查）。"""
        row = self.add_row(seller_id=SELLER_A1, amount=None,
                           order_no="33000000000000000061", success_time=None)
        self.assertIsNone(row.success_time)  # 自证：夹具真的造出了空桶键的行
        body = self.client.get(SUMMARY_URL).json()
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["totals"]["shop_count"], 0)


class DetailApiTests(SummaryFixture):
    def test_rows_are_sorted_desc_with_paginated_envelope(self):
        self.add_row(seller_id=SELLER_A1, amount=Decimal("1.00"),
                     shop_identity=IDENTITY_A1, order_no="33000000000000000071")
        later = self.add_row(seller_id=SELLER_A1, amount=Decimal("2.00"),
                             shop_identity=IDENTITY_A1,
                             order_no="33000000000000000072")
        response = self.client.get(LIST_URL, {"page_size": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["total"], 2)
        self.assertEqual(len(body["data"]["items"]), 1)
        # 排序 = 交易成功时间倒序（同一行创建次序无关）。
        self.assertEqual(body["data"]["items"][0]["order_no"], later.order_no)

    def test_shop_identity_filter_returns_only_that_shop(self):
        """点店铺看单店明细：`?shop_identity=` 精确筛（页面的次级视图，F2-D）。"""
        mine = self.add_row(seller_id=SELLER_A1, amount=Decimal("1.00"),
                            shop_identity=IDENTITY_A1,
                            order_no="33000000000000000081")
        self.add_row(seller_id="770000000002", amount=Decimal("2.00"),
                     shop_identity="EID-770000000002",
                     order_no="33000000000000000082")
        body = self.client.get(LIST_URL, {"shop_identity": IDENTITY_A1}).json()
        self.assertEqual(body["data"]["total"], 1)
        row = body["data"]["items"][0]
        self.assertEqual(row["order_no"], mine.order_no)
        # 行形状（F2-D）：分组键回显 + `seller_id` 照留（行级事实）。
        self.assertEqual(row["shop_identity"], IDENTITY_A1)
        self.assertEqual(row["seller_id"], SELLER_A1)

    def test_empty_shop_identity_filter_returns_only_unidentified_rows(self):
        """`?shop_identity=`（空串）= 只看**未识别桶**；参数缺席 = 不筛（整窗）。

        ⚠️ 空串与缺席必须可分辨（序列化器不设 `default`）：否则点桶会静默变成
        「整窗明细都算这一家的」，数字对不上。
        """
        identified = self.add_row(seller_id=SELLER_A1, amount=Decimal("1.00"),
                                  shop_identity=IDENTITY_A1,
                                  order_no="33000000000000000091")
        self.add_row(seller_id="770000000010", amount=Decimal("2.00"),
                     order_no="33000000000000000092")
        # 缺席 ⇒ 整窗两行。
        body = self.client.get(LIST_URL).json()
        self.assertEqual(body["data"]["total"], 2)
        # 空串 ⇒ 只有未识别的那一行（identified 的不在）。
        body = self.client.get(LIST_URL, {"shop_identity": ""}).json()
        self.assertEqual(body["data"]["total"], 1)
        row = body["data"]["items"][0]
        self.assertEqual(row["order_no"], "33000000000000000092")
        self.assertEqual(row["shop_identity"], "")
        self.assertNotEqual(row["order_no"], identified.order_no)

    def test_row_shape_amount_missing_and_match_summary(self):
        """行形状：金额字符串 / 缺失标记 / 匹配结果一句人话 / 展示名第一层（identity 真命中）。"""
        self.add_row(
            seller_id=SELLER_A1, amount=None, shop_identity=IDENTITY_A1,
            order_no="33000000000000000101",
            matched_count=1,
            match_result=[{"order_no": "8800001", "result": "refunded_skip"}],
        )
        body = self.client.get(LIST_URL).json()
        row = body["data"]["items"][0]
        self.assertEqual(row["amount"], "")
        self.assertIs(row["amount_missing"], True)
        self.assertEqual(row["matched_count"], 1)
        self.assertIn("已退单", row["match_summary"])
        self.assertIn("命中 1 张", row["match_summary"])
        # 展示名走了**现解析**这一层（setUp 建了 `user_identity == IDENTITY_A1` 的 XgjShop）。
        self.assertEqual(row["shop_name"], "合成店铺·一号")


class ParamValidationTests(SummaryFixture):
    def test_malformed_dates_are_1000_not_500(self):
        """请求来源的值过序列化器（E-0010）：畸形 / 倒置 ⇒ 200 + `1000`。"""
        for query in (
            {"date_from": "garbage"},
            {"date_to": "2026/10/01"},
            {"date_from": "2026-10-05", "date_to": "2026-10-01"},
        ):
            with self.subTest(query=query):
                response = self.client.get(SUMMARY_URL, query)
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(body["code"], 1000)
                self.assertNotEqual(body["msg"], "")
                response = self.client.get(LIST_URL, query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)


class PermissionGateTests(SummaryFixture):
    def test_anonymous_gets_401(self):
        from django.test import Client

        anonymous = Client(SERVER_NAME=DOMAIN_A)
        self.assertEqual(anonymous.get(SUMMARY_URL).status_code, 401)
        self.assertEqual(anonymous.get(LIST_URL).status_code, 401)

    def test_without_the_node_gets_403(self):
        """无节点 ⇒ 403（fail-closed；msg 点名权限）。

        ⚠️ 用**一个组都不给**的账号（照 `test_withdraw_review_api` 的 outsider 形状）：
        ⛔ 不能给 `groups=("客服",)`——本类 `setUp` 的夹具已把本节点挂到「客服」那个
        **共享的 Role** 上，同组的第二个账号会白捡到它（Role 挂组、不挂人）。
        """
        client = make_logged_in_client(username="receipt_no_node", domain=DOMAIN_A)
        for url in (SUMMARY_URL, LIST_URL):
            response = client.get(url)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["msg"], "你没有查看闲管家收款明细的权限")

    def test_with_the_node_gets_200(self):
        self.assertEqual(self.client.get(SUMMARY_URL).status_code, 200)
        self.assertEqual(self.client.get(LIST_URL).status_code, 200)


# ── 跨租户隔离（`AGENTS.md` §4 的红线）─────────────────────────────────────

DOMAIN_B = "xgj-receipt-iso-b.test.local"
SCHEMA_B = "xgj_receipt_iso_b"


class CrossTenantTests(TenantTestCase):
    """A 租户的汇总**看不到** B 租户的记账行（跨租户否定断言；判据 = `shop_identity`）。

    ⚠️ 判据字段**租户内唯一、且两租户取值不同**（`AGENTS.md` §5-G；⛔ 不用主键）：
    A 用 `IDENTITY_A1`、B 用 `IDENTITY_B1`。
    ⚠️ 第二个租户在 `setUpClass` 里建（在一个用例内部建 schema 会与外层事务相冲）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="收款明细隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        binding_a = XgjShopBinding.objects.create(
            shop_name="A应用", app_key="AK-ISO-A", app_secret="SK-ISO-A",
        )
        XgjReceiptRecord.objects.create(
            binding=binding_a, app_key="AK-ISO-A", order_no="33000000000000000111",
            seller_id=SELLER_A1, shop_identity=IDENTITY_A1,
            success_time=_local_day(-1) + timedelta(hours=12),
            amount=Decimal("55.00"),
        )
        from django_tenants.utils import tenant_context

        with tenant_context(self.tenant_b):
            binding_b = XgjShopBinding.objects.create(
                shop_name="B应用", app_key="AK-ISO-B", app_secret="SK-ISO-B",
            )
            XgjReceiptRecord.objects.create(
                binding=binding_b, app_key="AK-ISO-B", order_no="33000000000000000112",
                seller_id="770000000099", shop_identity=IDENTITY_B1,
                success_time=_local_day(-1) + timedelta(hours=12),
                amount=Decimal("66.00"),
            )
        self.client = make_logged_in_client(
            username="receipt_iso_a",
            # ⚠️ 同上：节点必须挂在已有的组上（`_grant_nodes` 的实现）。
            groups=("客服",), nodes=("finance.xgj_receipt_detail",), domain=DOMAIN_A,
        )

    def test_tenant_a_summary_does_not_see_tenant_b_rows(self):
        body = self.client.get(SUMMARY_URL).json()
        identities = [row["shop_identity"] for row in body["data"]["items"]]
        self.assertEqual(identities, [IDENTITY_A1])     # 只有自己的店
        self.assertNotIn(IDENTITY_B1, identities)       # B 的店不存在（否定断言按值）
        self.assertEqual(body["data"]["totals"]["amount"], "55.00")

    def test_tenant_a_detail_does_not_see_tenant_b_rows(self):
        body = self.client.get(LIST_URL, {"shop_identity": IDENTITY_B1}).json()
        # A 里没有 IDENTITY_B1 ⇒ 空集（⛔ 不是把 B 的行筛出来）。
        self.assertEqual(body["data"]["total"], 0)
        self.assertEqual(body["data"]["items"], [])
        body = self.client.get(LIST_URL, {"shop_identity": IDENTITY_A1}).json()
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["amount"], "55.00")
        # 连接钉回 A（跨 schema 的夹具写完即回，后续用例不受污染）。
        connection.set_tenant(self.tenant)


# ══════════════════════════════════════════════════════════════════════
# 公司店铺分组筛选（计划 `docs/plans/2026-10-26-order-source-groups.md` §4 #6-#7）
# ══════════════════════════════════════════════════════════════════════
class SourceGroupFilterTests(SummaryFixture):
    """`?source_group=`：只留「店铺**当前**所属来源在该分组」的行（summary 与 detail 同一套）。

    三半都断：**命中**、**排除**（别组 / **未识别店铺**）、**不传不筛**。
    """

    def setUp(self):
        super().setUp()
        # ⭐ 行业语义：分组挂在**订单来源**上，店铺（`XgjShop`）指向来源。
        self.group = OrderSourceGroup.objects.create(name="收款分组甲")
        self.source = OrderSource.objects.create(name="收款来源甲", group=self.group)
        self.shop_one.order_source = self.source
        self.shop_one.save(update_fields=["order_source"])

    def _seed_three(self):
        """三行：组内店 / 别家店 / **未识别店铺**（`shop_identity` 为空）。"""
        self.add_row(
            seller_id=SELLER_A1, amount=Decimal("10.00"), shop_identity=IDENTITY_A1,
            order_no="33000000000000000101",
        )
        self.add_row(
            seller_id="770000000101", amount=Decimal("20.00"),
            shop_identity="EID-770000000101", order_no="33000000000000000102",
        )
        self.add_row(
            seller_id="770000000102", amount=Decimal("30.00"), shop_identity=None,
            order_no="33000000000000000103",
        )

    def test_summary_filters_by_the_shops_current_source_group(self):
        self._seed_three()
        # 不传 = 不筛（三行都在：两个 identity 桶 + 未识别桶）。
        without = self.client.get(SUMMARY_URL).json()["data"]["totals"]
        self.assertEqual(without["count"], 3)

        body = self.client.get(SUMMARY_URL, {"source_group": self.group.pk}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual([row["shop_identity"] for row in body["data"]["items"]], [IDENTITY_A1])
        self.assertEqual(body["data"]["totals"]["count"], 1)
        self.assertEqual(body["data"]["totals"]["amount"], "10.00")

    def test_detail_filters_by_the_same_group(self):
        self._seed_three()
        self.assertEqual(self.client.get(LIST_URL).json()["data"]["total"], 3)
        body = self.client.get(LIST_URL, {"source_group": self.group.pk}).json()
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["order_no"], "33000000000000000101")

    def test_unidentified_shop_rows_are_excluded(self):
        """未识别店铺（`shop_identity` 为空）不属于任何分组 ⇒ 选分组时必须被排除。"""
        self._seed_three()
        body = self.client.get(LIST_URL, {"source_group": self.group.pk}).json()
        identities = [row["shop_identity"] for row in body["data"]["items"]]
        self.assertNotIn("", identities)

    def test_zero_and_non_integer_are_1000_on_both_endpoints(self):
        for url in (SUMMARY_URL, LIST_URL):
            for raw in ("0", "-1", "abc"):
                with self.subTest(url=url, raw=raw):
                    body = self.client.get(url, {"source_group": raw}).json()
                    self.assertEqual(body["code"], 1000, body)
                    self.assertIsNone(body["data"])

    def test_group_without_any_shop_returns_empty_totals(self):
        empty_group = OrderSourceGroup.objects.create(name="空分组")
        self._seed_three()
        body = self.client.get(SUMMARY_URL, {"source_group": empty_group.pk}).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["totals"]["count"], 0)
