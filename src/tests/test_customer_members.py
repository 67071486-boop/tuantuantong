"""客户（会员）线的判据（2026-11-10 客户线）。

覆盖（计划 §4）：
- 模型唯一约束（eid 租户内唯一）；
- W1 录单 upsert（同 eid 两单 → 1 行 / 昵称刷新 / 两单都连；空缺省不建；超宽拒且一行不写；
  PATCH 与对外 openapi 不是写入者）；
- 读接口 members / member-filters / member-orders（行聚合、筛选、排序两档、电话包含匹配、
  录单人显示名映射与回落、抽屉只读合并列表 / 分页 / eid 查无钉码、`total` 恒全量、分页、
  权限守卫、畸形入参不 500）；
- 分析报表 analysis（第三批：守卫三档、range 三档与畸形钉码、KPI 钉值含负数单、
  分层恰好落档、来源分布仅本期、未连客户的单不进任何聚合、30d/90d/12m 档界含首含尾、
  增长按**首单时间**钉住「不看建档时间」、空租户全零；第七批 2026-11-11：
  `source_customer_dist` 各店铺有订单客户数——店內去重 / 同名合并 / 跟随 range）；
- 抽屉头部信息卡 `customer_info` 恰好 7 键含 eid 掩码（第七批 2026-11-11）；
- 来源订单（第五批 2026-11-11；第六批同日合并）：`CustomerSourceOrder` 唯一约束 /
  `upsert_source_order` 幂等 upsert / 列表行集扩容为**全部建档客户**（来源单-only 行的
  钉值与筛选不可见性）/ 抽屉**两段并一张表**（同单两面按单号去重、无效单剔除、闲管家
  状态中文化逐码钉值、合并时间倒序、两侧各 2,000 条截断、系统单软删 ⇒ 来源单恢复
  不双删）/ 统计合并
  （订单数 / 累计消费 = 系统单 + 有效来源单；限制档保持系统单口径）/ 跨租户来源单不互串；
- **跨租户否定断言**（判据 = `eid` 这个租户内唯一且两租户取值不同的业务值，⛔ 不是主键，
  `AGENTS.md` §5-G）；
- 回填命令（mock 上游；桩打在被查找的模块 `backfill_xgj_customers.xgj_order_detail` 上，
  另附**桩接线自证**——`Command` 模块的全局名必须就是 `xgj.order_detail`，不接桩时走的
  就是真函数 ⇒ 桩没接上时命令必然真调上游而失败，E-0029）；
- 写入点契约网（`CustomerWritePathNetTests`：AST 扫描面**如实声明**只认三个文件）。

⚠️ 夹具一律合成值（E-0077）； eid / 昵称 / 单号都不是真实数据。
"""
import ast
import io
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.business import customers as customer_domain
from apps.business.api.serializers import OrderSerializer, OrderUpdateSerializer
from apps.business.models import (
    Customer,
    CustomerSourceOrder,
    Game,
    Order,
    OrderSource,
    OrderStatus,
    Project,
    ProjectCategory,
    SendResult,
    TaobaoOrderSendLog,
    XgjShop,
    XgjShopBinding,
)
from apps.platform.provisioning import provision_tenant
from apps.core.datetime_display import local_iso
from django.utils import timezone
from tests.p5_auth_helpers import P2_TEST_USERNAME, make_logged_in_client

# ── 合成判据值（租户内唯一；两租户取值不同）────────────────────────────────
EID_A = "cm-eid-tenant-a-0001"
EID_B = "cm-eid-tenant-b-0001"
NICK_A = "买家昵称甲"
NICK_B = "买家昵称乙"

URL_MEMBERS = "/api/business/customers/members/"
URL_FILTERS = "/api/business/customers/member-filters/"
URL_MEMBER_ORDERS = "/api/business/customers/member-orders/"
URL_ANALYSIS = "/api/business/customers/analysis/"
DOMAIN_A = "tenant.test.com"
DOMAIN_B = "cm-iso-b.test.local"
SCHEMA_B = "cm_iso_b"


def _aware(hour, minute=0):
    """ Asia/Shanghai（settings.TIME_ZONE）的合成时刻 → aware datetime。"""
    from django.utils import timezone

    return timezone.make_aware(datetime(2026, 6, 1, hour, minute))


def _ago(days):
    """「now − days 天」的 aware 时刻（分析夹具用；窗口按本地日期切）。"""
    from django.utils import timezone

    return timezone.now() - timedelta(days=days)


def _month_key(moment):
    """aware 时刻 → `YYYY-MM`（本地时区；与 analysis_growth 的分月同键）。"""
    from django.utils import timezone

    return timezone.localtime(moment).strftime("%Y-%m")


# ═══════════ 模型 ═══════════


class CustomerModelTests(TenantTestCase):
    def test_eid_is_unique_per_tenant(self):
        Customer.objects.create(eid=EID_A, buyer_nick=NICK_A)
        with self.assertRaises(IntegrityError):
            Customer.objects.create(eid=EID_A)

    def test_buyer_nick_defaults_to_empty(self):
        row = Customer.objects.create(eid=EID_A)
        self.assertEqual(row.buyer_nick, "")


class CustomerSourceOrderModelTests(TenantTestCase):
    """来源订单快照（第五批）：租户内唯一约束 + `upsert_source_order` 幂等语义。

    唯一写入方 = 收割运维脚本（不进仓）；这里经领域函数驱动（W3 的唯一入口）。
    """

    def test_source_order_no_is_unique_per_tenant(self):
        customer = Customer.objects.create(eid=EID_A)
        customer_domain.upsert_source_order(
            customer=customer, source_order_no="TB-SRC-合成-1", order_status=1,
            total_amount=Decimal("10.00"), updated_time=_aware(8), shop_name="店甲",
        )
        with self.assertRaises(IntegrityError):
            CustomerSourceOrder.objects.create(
                customer=customer, source_order_no="TB-SRC-合成-1",
            )

    def test_upsert_is_idempotent_and_refreshes_snapshot(self):
        """同号两写 = 一行；第二写整行刷新（金额/状态/店铺/时间），客户不变。"""
        customer = Customer.objects.create(eid=EID_A)
        row, created = customer_domain.upsert_source_order(
            customer=customer, source_order_no="TB-SRC-合成-2", order_status=0,
            total_amount="10.00", updated_time=_aware(8), shop_name="店甲",
        )
        self.assertTrue(created)
        row2, created2 = customer_domain.upsert_source_order(
            customer=customer, source_order_no="TB-SRC-合成-2", order_status=3,
            total_amount="99.50", updated_time=_aware(12, 30), shop_name="店乙",
        )
        self.assertFalse(created2)
        self.assertEqual(row2.pk, row.pk)
        self.assertEqual(CustomerSourceOrder.objects.count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.order_status, 3)
        self.assertEqual(row.total_amount, Decimal("99.50"))
        self.assertEqual(row.updated_time, _aware(12, 30))
        self.assertEqual(row.shop_name, "店乙")

    def test_upsert_defaults_amount_zero_and_null_time(self):
        """缺金额 = 0.00（E-0021：零是合法边界）；`updated_time` 允许空（列可空）。"""
        customer = Customer.objects.create(eid=EID_A)
        row, _ = customer_domain.upsert_source_order(
            customer=customer, source_order_no="TB-SRC-合成-3",
        )
        self.assertEqual(row.total_amount, Decimal("0.00"))
        self.assertIsNone(row.updated_time)
        self.assertEqual(row.order_status, 0)
        self.assertEqual(row.shop_name, "")

    def test_negative_amount_is_legal_boundary(self):
        """负金额照存（D-0015：退款冲减的合法边界，与系统订单同一口径）。"""
        customer = Customer.objects.create(eid=EID_A)
        row, _ = customer_domain.upsert_source_order(
            customer=customer, source_order_no="TB-SRC-合成-4", total_amount=Decimal("-5.00"),
        )
        self.assertEqual(row.total_amount, Decimal("-5.00"))


# ═══════════ W1：录单 upsert ═══════════


class IntakeBuyerEidTests(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(
            groups=("客服",),
            games=None,
            nodes=("order.add_edit",),
        )
        self.game = Game.objects.create(name="客户线游戏")
        self.cat = ProjectCategory.objects.create(game=self.game, name="客户线分类")
        self.project = Project.objects.create(category=self.cat, name="客户线项目")
        StaffProfile.objects.create(user=self._user()).games.set([self.game])
        self.base = "/api/business/orders"

    def _user(self):
        from django.contrib.auth.models import User

        return User.objects.get(username=P2_TEST_USERNAME)

    def _create(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "客户线项目文本",
            "total_price": "88.00",
        }
        payload.update(overrides)
        return self.client.post(self.base + "/", payload, content_type="application/json")

    def test_create_with_buyer_eid_creates_customer_and_links(self):
        body = self._create(buyer_eid=EID_A, buyer_nick=NICK_A).json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        customer = Customer.objects.get(eid=EID_A)
        self.assertEqual(order.customer_id, customer.pk)
        self.assertEqual(customer.buyer_nick, NICK_A)
        # 出参里回客户 id（前端详情可见；值为该行的主键，钉值不钉常量）。
        self.assertEqual(body["data"]["customer"], customer.pk)

    def test_same_eid_two_orders_share_one_customer_and_refresh_nick(self):
        first = self._create(buyer_eid=EID_A, buyer_nick=NICK_A).json()
        second = self._create(buyer_eid=EID_A, buyer_nick=NICK_B).json()
        self.assertEqual(Customer.objects.count(), 1)
        customer = Customer.objects.get(eid=EID_A)
        # 昵称 = **最新**快照（后写的那次）。
        self.assertEqual(customer.buyer_nick, NICK_B)
        for body in (first, second):
            order = Order.objects.get(order_no=body["data"]["order_no"])
            self.assertEqual(order.customer_id, customer.pk)

    def test_absent_or_blank_buyer_eid_writes_no_customer(self):
        body_absent = self._create().json()
        body_blank = self._create(buyer_eid="", buyer_nick=NICK_A).json()
        self.assertEqual(body_absent["code"], 0)
        self.assertEqual(body_blank["code"], 0)
        self.assertEqual(Customer.objects.count(), 0)
        for order in Order.objects.all():
            self.assertIsNone(order.customer)

    def test_overwide_eid_is_rejected_and_writes_nothing(self):
        body = self._create(buyer_eid="e" * 65).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(Customer.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_patch_cannot_write_customer(self):
        created = self._create(buyer_eid=EID_A).json()
        order = Order.objects.get(order_no=created["data"]["order_no"])
        other = Customer.objects.create(eid="cm-eid-other-row")
        res = self.client.patch(
            f"{self.base}/{order.order_no}/",
            {"customer": other.pk},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.customer_id, Customer.objects.get(eid=EID_A).pk)

    def test_split_group_links_every_member_to_same_customer(self):
        """拆单 × buyer_eid：组内各笔连**同一个**客户（同一次购买），昵称快照已写。

        评审探针实证后收编为正式用例（2026-11-10 修复轮）；判据打在库内组内每笔的
        `customer_id` 上，不是只看第 1 笔（只看主单会把「组内其余笔漏连」放过去）。
        """
        body = self._create(
            project_text="段一/段二/段三", split=True, buyer_eid=EID_A, buyer_nick=NICK_A,
        ).json()
        self.assertEqual(body["code"], 0, body)
        customer = Customer.objects.get(eid=EID_A)
        self.assertEqual(customer.buyer_nick, NICK_A)
        group = list(Order.objects.filter(split_id=body["data"]["split_id"]))
        self.assertEqual(len(group), 3)
        self.assertEqual({o.customer_id for o in group}, {customer.pk})

    def test_performance_failure_rolls_back_customer_row_too(self):
        """建单事务内绩效建行失败（`set_rollback` 路径）⇒ 客户行与订单**一起**回滚、零残留。

        钉住 `order_intake.py` 的 E-0022 语义：客户 upsert 与建单同一个 `atomic()`，
        「无单的客户」不得残留（评审探针实证后收编，2026-11-10 修复轮）。
        """
        from apps.business.commission import CommissionError

        with mock.patch(
            "apps.business.order_intake.create_performance_for_order",
            side_effect=CommissionError(message="绩效建行失败（合成）", code=1234),
        ):
            body = self._create(buyer_eid=EID_A, buyer_nick=NICK_A).json()
        self.assertNotEqual(body["code"], 0, body)
        self.assertEqual(Customer.objects.filter(eid=EID_A).count(), 0)
        self.assertEqual(Order.objects.count(), 0)


# ═══════════ 读接口 ═══════════


class MembersApiTestsBase(TenantTestCase):
    """两张表的公共夹具：1 个客户两笔单（跨来源/录单人）+ 另 1 个客户一笔单。"""

    def _make_order(self, *, order_no, customer=None, source=None, created_by="", phone="",
                    price="100.00", hour=8, minute=0):
        order = Order.objects.create(
            order_no=order_no,
            game=self.game,
            project_text="聚合项目",
            total_price=Decimal(price),
            customer_phone=phone,
            created_by=created_by,
            order_source=source,
            customer=customer,
        )
        Order.objects.filter(pk=order.pk).update(created_at=_aware(hour, minute))
        return order

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-admin")
        self.game = Game.objects.create(name="读面游戏")
        self.cat = ProjectCategory.objects.create(game=self.game, name="读面分类")
        Project.objects.create(category=self.cat, name="读面项目")
        # ⚠️ OrderSource 的 Meta.ordering = -order_by, id ⇒ 甲（9）排在乙（1）前。
        self.source_a = OrderSource.objects.create(name="店铺甲", order_by=9)
        self.source_b = OrderSource.objects.create(name="店铺乙", order_by=1)
        self.customer = Customer.objects.create(eid=EID_A, buyer_nick=NICK_A)
        self.other = Customer.objects.create(eid="cm-eid-row-2", buyer_nick="买家昵称丙")
        # 客户 A 两笔：08:00（店铺甲/客服甲/138...）与 12:30（店铺乙/客服乙/139...）→ 最近 = 12:30
        self._make_order(order_no="CMROW00000000000001", customer=self.customer,
                         source=self.source_a, created_by="客服甲", phone="13800000001", hour=8)
        self._make_order(order_no="CMROW00000000000002", customer=self.customer,
                         source=self.source_b, created_by="客服乙", phone="13900000002",
                         price="56.70", hour=12, minute=30)
        # 客户 B 一笔：10:00（店铺甲/客服甲）
        self._make_order(order_no="CMROW00000000000003", customer=self.other,
                         source=self.source_a, created_by="客服甲", phone="13700000003",
                         price="20.00", hour=10)
        # 一笔没连客户的手工单：不进任何会员行（"行 = 已连客户的单"）。
        self._make_order(order_no="CMROW00000000000004", created_by="客服甲", hour=9)


class CustomerMembersApiTests(MembersApiTestsBase):
    def test_recorder_counts_distinct_customers_with_current_filters(self):
        self._make_order(order_no="CMROW00000000000005", customer=self.customer,
                         source=self.source_a, created_by="客服甲")
        self._make_order(order_no="CMROW00000000000006", customer=self.other,
                         source=self.source_a, created_by="")
        body = self.client.get(URL_MEMBERS).json()["data"]
        self.assertEqual([(x["recorder"], x["count"]) for x in body["recorder_counts"]],
                         [("客服甲", 2), ("客服乙", 1)])
        first = self.client.get(URL_MEMBERS, {"page_size": 1, "page": 1}).json()["data"]
        second = self.client.get(URL_MEMBERS, {"page_size": 1, "page": 2}).json()["data"]
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(first["items"][0]["eid"], second["items"][0]["eid"])
        self.assertEqual(first["recorder_counts"], body["recorder_counts"])
        self.assertEqual(second["recorder_counts"], body["recorder_counts"])
        filtered = self.client.get(URL_MEMBERS, {"source": self.source_b.pk}).json()["data"]
        self.assertEqual([(x["recorder"], x["count"]) for x in filtered["recorder_counts"]],
                         [("客服乙", 1)])

    def test_rows_aggregate_and_latest_order_snapshot_wins(self):
        body = self.client.get(URL_MEMBERS).json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        # total 恒 = 客户表全量（不受筛选影响，业务方拍板）。
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["filtered_total"], 2)
        rows = data["items"]
        self.assertEqual([r["eid"] for r in rows], [EID_A, "cm-eid-row-2"])  # 最近下单在前
        top = rows[0]
        self.assertEqual(top["buyer_nick"], NICK_A)
        self.assertEqual(top["order_count"], 2)
        self.assertEqual(top["last_order_at"], "2026-06-01T12:30:00+08:00")
        self.assertEqual(top["total_amount"], "156.70")
        self.assertEqual(top["phone"], "13900000002")
        self.assertEqual(top["order_source"], self.source_b.pk)
        self.assertEqual(top["source_name"], "店铺乙")
        self.assertEqual(top["recorder"], "客服乙")

    def test_filter_by_source_changes_rows_and_totals(self):
        body = self.client.get(URL_MEMBERS, {"source": self.source_a.pk}).json()
        data = body["data"]
        self.assertEqual(data["total"], 2)                    # 全量数不变
        self.assertEqual(data["filtered_total"], 2)
        self.assertEqual(
            [(r["eid"], r["order_count"]) for r in data["items"]],
            [("cm-eid-row-2", 1), (EID_A, 1)],
        )
        # 行展示列取**筛选范围内**最近一单（客户 A 的店铺甲单）。
        row_a = next(r for r in data["items"] if r["eid"] == EID_A)
        self.assertEqual(row_a["phone"], "13800000001")
        self.assertEqual(row_a["recorder"], "客服甲")
        self.assertEqual(row_a["total_amount"], "100.00")

    def test_filter_by_recorder(self):
        body = self.client.get(URL_MEMBERS, {"recorder": "客服乙"}).json()
        data = body["data"]
        self.assertEqual(data["filtered_total"], 1)
        self.assertEqual([r["eid"] for r in data["items"]], [EID_A])
        self.assertEqual(data["items"][0]["recorder"], "客服乙")

    def test_filter_with_no_matching_orders_returns_empty_but_keeps_total(self):
        body = self.client.get(URL_MEMBERS, {"recorder": "不存在的客服"}).json()
        data = body["data"]
        self.assertEqual(data["items"], [])
        self.assertEqual(data["filtered_total"], 0)
        self.assertEqual(data["total"], 2)

    def test_pagination_defaults_and_second_page(self):
        body = self.client.get(URL_MEMBERS, {"page": 2, "page_size": 1}).json()
        data = body["data"]
        self.assertEqual(data["page_size"], 1)
        self.assertEqual(data["page"], 2)
        self.assertEqual([r["eid"] for r in data["items"]], ["cm-eid-row-2"])
        default = self.client.get(URL_MEMBERS).json()["data"]
        self.assertEqual(default["page_size"], 50)

    def test_malformed_query_is_1000_not_500(self):
        # 期望码**逐档钉死**（两档真实行为不同，不写模糊的 !=0）：
        # 序列化器档（source 非法）= 1000；分页档（page 非数字 → DRF NotFound）= 1002。
        for params, expected_code in (
            ({"source": "abc"}, 1000),
            ({"source": "0"}, 1000),
            ({"page": "abc"}, 1002),
        ):
            with self.subTest(params=params):
                res = self.client.get(URL_MEMBERS, params)
                self.assertEqual(res.status_code, 200, res.content)
                self.assertEqual(res.json()["code"], expected_code, res.json())

    def test_member_filters_lists_sources_and_recorders(self):
        body = self.client.get(URL_FILTERS).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            body["data"]["sources"],
            [{"id": self.source_a.pk, "name": "店铺甲"}, {"id": self.source_b.pk, "name": "店铺乙"}],
        )
        # 第二批起 recorders = [{value, label}]：本夹具两个录单人都没有 User 行 ⇒ label 回落原值。
        # 手工漏填（空串）不进候选；升序（钉值：恰好这两个录单人）。
        self.assertEqual(
            [r["value"] for r in body["data"]["recorders"]], ["客服乙", "客服甲"]
        )
        self.assertEqual(
            [r["label"] for r in body["data"]["recorders"]], ["客服乙", "客服甲"]
        )

    def test_rows_carry_recorder_display_fallback_without_user_row(self):
        # 夹具两个录单人都没有 User 行 ⇒ 显示名回落原账号（同值；键仍保留原值）。
        body = self.client.get(URL_MEMBERS).json()
        top = body["data"]["items"][0]
        self.assertEqual(top["recorder"], "客服乙")
        self.assertEqual(top["recorder_display"], "客服乙")


class CustomerMembersR2QueryTests(TenantTestCase):
    """第二批判据：order_by 两档 + 默认、phone 包含匹配（icase）、显示名映射与回落、
    filters 出参形状（value/label 有真实 User 行时可分）。夹具自建、不与基类共用
    （行数 / total 的钉值互不牵连）。"""

    def _make_order(self, *, order_no, customer=None, source=None, created_by="", phone="",
                    price="100.00", hour=8):
        order = Order.objects.create(
            order_no=order_no,
            game=self.game,
            project_text="第二批项目",
            total_price=Decimal(price),
            customer_phone=phone,
            created_by=created_by,
            order_source=source,
            customer=customer,
        )
        Order.objects.filter(pk=order.pk).update(created_at=_aware(hour))
        return order

    def setUp(self):
        from django.contrib.auth.models import User

        self.client = make_logged_in_client(is_superuser=True, username="cm-r2-admin")
        self.game = Game.objects.create(name="第二批游戏")
        self.source = OrderSource.objects.create(name="第二批店铺", order_by=1)
        # 在册账号（有显示名）+ 一个只在订单快照里出现的账号（User 已删形态的替身）。
        User.objects.create_user(username="cm_r2_user", first_name="九号客服")
        self.c_many = Customer.objects.create(eid="cm-r2-eid-many", buyer_nick="单多客")
        self.c_mid = Customer.objects.create(eid="cm-r2-eid-mid", buyer_nick="中间客")
        self.c_fresh = Customer.objects.create(eid="cm-r2-eid-fresh", buyer_nick="最近客")
        # 三客户的订单数 / 最近一单 / 合计金额两两不同序（防变异）：
        # 单多客 3×10=30 @08:00；中间客 2×30=60 @12:00；最近客 1×50=50 @20:00
        # ⇒ 数序 [many,mid,fresh]、时序 [fresh,mid,many]、额序 [mid,fresh,many] 互不相同。
        for i in range(1, 4):
            self._make_order(
                order_no=f"CMR2M0000000000000{i}",
                customer=self.c_many, source=self.source,
                created_by="cm_r2_user", phone=f"Ab1{i:02d}0001",
                price="10.00", hour=8,
            )
        for i in range(1, 3):
            self._make_order(
                order_no=f"CMR2X0000000000000{i}",
                customer=self.c_mid, source=self.source,
                created_by="cm_r2_user", phone=f"QQ7{i:02d}0001",
                price="30.00", hour=12,
            )
        self._make_order(
            order_no="CMR2F00000000000001",
            customer=self.c_fresh, source=self.source,
            created_by="cm_r2_gone", phone="ZZZ99001", price="50.00", hour=20,
        )

    def test_default_order_is_order_count_desc_not_latest_first(self):
        # 旧默认（最近下单在前）会把「最近客」排前；新默认（订单数降序）必须「单多客→中间客→最近客」。
        body = self.client.get(URL_MEMBERS).json()
        self.assertEqual([r["eid"] for r in body["data"]["items"]],
                         ["cm-r2-eid-many", "cm-r2-eid-mid", "cm-r2-eid-fresh"])

    def test_order_by_total_amount_desc(self):
        # 额序 [中间客 60 > 最近客 50 > 单多客 30] 与时序相反、也与数序不同
        # ⇒ 只有真按 Sum(total_price) 排才绿（夹具修前「按最近下单排」的变异 7/7 存活）。
        body = self.client.get(URL_MEMBERS, {"order_by": "total_amount"}).json()
        self.assertEqual([r["eid"] for r in body["data"]["items"]],
                         ["cm-r2-eid-mid", "cm-r2-eid-fresh", "cm-r2-eid-many"])
        self.assertEqual(body["data"]["items"][0]["total_amount"], "60.00")

    def test_order_by_malformed_is_1000(self):
        res = self.client.get(URL_MEMBERS, {"order_by": "nickname"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["code"], 1000, res.json())

    def test_phone_filter_rows_are_customers_with_a_matching_order(self):
        body = self.client.get(URL_MEMBERS, {"phone": "ab1"}).json()
        # icase：订单里的电话是小写/混合形态，筛选值小写也命中（行 = 有匹配单的客户）。
        self.assertEqual([r["eid"] for r in body["data"]["items"]], ["cm-r2-eid-many"])
        # 行内电话列仍 = 筛选范围内最近一单的电话（不是匹配那单的）。
        self.assertEqual(body["data"]["items"][0]["phone"], "Ab1030001")

    def test_phone_filter_miss_keeps_total(self):
        body = self.client.get(URL_MEMBERS, {"phone": "不存在的号"}).json()
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["filtered_total"], 0)
        self.assertEqual(body["data"]["total"], 3)

    def test_recorder_display_maps_via_user_first_name_and_falls_back(self):
        body = self.client.get(URL_MEMBERS).json()
        rows = {r["eid"]: r for r in body["data"]["items"]}
        self.assertEqual(rows["cm-r2-eid-many"]["recorder"], "cm_r2_user")
        self.assertEqual(rows["cm-r2-eid-many"]["recorder_display"], "九号客服")
        # User 行不存在（已删 / 改名）⇒ 回落原用户名，键不消失。
        self.assertEqual(rows["cm-r2-eid-fresh"]["recorder"], "cm_r2_gone")
        self.assertEqual(rows["cm-r2-eid-fresh"]["recorder_display"], "cm_r2_gone")

    def test_member_filters_recorders_are_value_label_pairs(self):
        body = self.client.get(URL_FILTERS).json()
        recorders = body["data"]["recorders"]
        self.assertEqual(
            [r["value"] for r in recorders], ["cm_r2_gone", "cm_r2_user"]
        )
        labels = {r["value"]: r["label"] for r in recorders}
        self.assertEqual(labels["cm_r2_user"], "九号客服")
        self.assertEqual(labels["cm_r2_gone"], "cm_r2_gone")


class CustomerMemberOrdersApiTests(TenantTestCase):
    """客户订单抽屉（第六批 §2.3 统一行形状）：单一合并列表、系统单行钉值
    （`no` = 系统单号、状态中文 = OrderStatus label、录单人 = 显示名回落）、
    最近下单在前、分页、eid 查无钉码（来源单合并与去重的专项判据在
    `MemberOrdersMergeTests`）。"""

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-mo-admin")
        self.game = Game.objects.create(name="抽屉游戏")
        self.source = OrderSource.objects.create(name="抽屉店铺", order_by=1)
        self.customer = Customer.objects.create(eid="cm-mo-eid-0001", buyer_nick="抽屉客")
        self.other = Customer.objects.create(eid="cm-mo-eid-0002", buyer_nick="别家客")
        # 该客户两笔（12:30 在前）+ 别家一笔 + 未连单（不属于任何客户，不进抽屉）。
        Order.objects.create(
            order_no="CMO00000000000000001", game=self.game, project_text="抽屉项目旧",
            total_price=Decimal("10.00"), customer=self.customer, created_by="客服甲",
            order_source=self.source, status=OrderStatus.COMPLETED,
        )
        Order.objects.filter(order_no="CMO00000000000000001").update(created_at=_aware(8))
        Order.objects.create(
            order_no="CMO00000000000000002", game=self.game, project_text="抽屉项目新",
            total_price=Decimal("88.00"), customer_phone="13811112222",
            customer=self.customer, created_by="客服乙", source_order_no="TB-合成-2",
            order_source=self.source, status=OrderStatus.IN_PROGRESS,
        )
        Order.objects.filter(order_no="CMO00000000000000002").update(created_at=_aware(12, 30))
        Order.objects.create(
            order_no="CMO00000000000000003", game=self.game, project_text="别家单",
            total_price=Decimal("20.00"), customer=self.other,
        )
        Order.objects.create(
            order_no="CMO00000000000000004", game=self.game, project_text="未连单",
            total_price=Decimal("30.00"),
        )

    def test_system_rows_pin_unified_shape_and_newest_first(self):
        body = self.client.get(URL_MEMBER_ORDERS, {"eid": "cm-mo-eid-0001"}).json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual([r["no"] for r in data["items"]],
                         ["CMO00000000000000002", "CMO00000000000000001"])
        top = data["items"][0]
        # 统一行形状恰好 10 键（修复轮加回 status 枚举键；多键少键都算形状漂移）。
        self.assertEqual(
            set(top),
            {"no", "is_system", "status", "game", "project", "amount", "time",
             "status_text", "shop", "recorder"},
        )
        self.assertTrue(top["is_system"])
        self.assertEqual(top["game"], "抽屉游戏")
        self.assertEqual(top["project"], "抽屉项目新")
        self.assertEqual(top["amount"], "88.00")
        self.assertEqual(top["time"], "2026-06-01T12:30:00+08:00")
        # `status` = OrderStatus 枚举值（v2 页显示词由前端 V2 词表映射）。
        self.assertEqual(top["status"], "IN_PROGRESS")
        # `status_text` = 共享 label（models 唯一出处；前端 v2 兜底用，⛔ 不为 v2 单独改）。
        self.assertEqual(top["status_text"], "代练中")
        self.assertEqual(top["shop"], "抽屉店铺")
        # 录单人 = 显示名（无 User 行 ⇒ 回落登录账号，与旧抽屉展示口径一致）。
        self.assertEqual(top["recorder"], "客服乙")
        second = data["items"][1]
        self.assertEqual(second["status_text"], "已完成")
        self.assertEqual(second["amount"], "10.00")

    def test_customer_info_card_pins_profile_and_merged_stats(self):
        """抽屉头部信息卡（第七批 §1.1）：恰好 7 键；eid 打掩码（完整值不出参）；
        统计 = 系统单 + 有效来源单**合并**口径（该客户 2 张系统单合计 98.00）、
        不随外层筛选变（本端点本就不收筛选参数）。"""
        body = self.client.get(URL_MEMBER_ORDERS, {"eid": "cm-mo-eid-0001"}).json()
        info = body["data"]["customer_info"]
        self.assertEqual(
            set(info),
            {"eid_masked", "buyer_nick", "phone", "total_amount", "order_count",
             "last_order_at", "created_at"},
        )
        self.assertEqual(info["eid_masked"], "cm-m***01")
        self.assertNotEqual(info["eid_masked"], "cm-mo-eid-0001")
        self.assertEqual(info["buyer_nick"], "抽屉客")
        self.assertEqual(info["phone"], "13811112222")
        self.assertEqual(info["order_count"], 2)
        self.assertEqual(info["total_amount"], "98.00")
        self.assertEqual(info["last_order_at"], "2026-06-01T12:30:00+08:00")
        # 建档时间 = Customer.created_at（auto_now_add）原值经 local_iso。
        customer = Customer.objects.get(eid="cm-mo-eid-0001")
        self.assertEqual(info["created_at"], local_iso(customer.created_at))

    def test_pagination(self):
        body = self.client.get(
            URL_MEMBER_ORDERS, {"eid": "cm-mo-eid-0001", "page": 2, "page_size": 1}
        ).json()
        self.assertEqual(body["data"]["page"], 2)
        self.assertEqual([r["no"] for r in body["data"]["items"]],
                         ["CMO00000000000000001"])
        default = self.client.get(
            URL_MEMBER_ORDERS, {"eid": "cm-mo-eid-0001"}
        ).json()["data"]
        self.assertEqual(default["page_size"], 50)

    def test_missing_or_malformed_eid_is_1000(self):
        for params in ({}, {"eid": ""}, {"eid": "x" * 65}):
            with self.subTest(params=params):
                res = self.client.get(URL_MEMBER_ORDERS, params)
                self.assertEqual(res.status_code, 200, res.content)
                self.assertEqual(res.json()["code"], 1000, res.json())

    def test_unknown_eid_is_1000_with_pinned_msg(self):
        res = self.client.get(URL_MEMBER_ORDERS, {"eid": "cm-mo-no-such-eid"})
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "客户不存在")


class CustomerMemberOrdersPermissionTests(TenantTestCase):
    def test_admin_passes_and_plain_user_403(self):
        admin = make_logged_in_client(is_superuser=True, username="cm-mo-perm-admin")
        self.assertEqual(admin.get(URL_MEMBER_ORDERS, {"eid": "x"}).status_code, 200)
        plain = make_logged_in_client(username="cm-mo-perm-plain")
        res = plain.get(URL_MEMBER_ORDERS, {"eid": "x"})
        self.assertEqual(res.status_code, 403, res.content)


class CustomerMembersPermissionTests(TenantTestCase):
    def test_admin_passes_node_user_passes_plain_user_403(self):
        admin = make_logged_in_client(is_superuser=True, username="cm-perm-admin")
        self.assertEqual(admin.get(URL_MEMBERS).status_code, 200)
        self.assertEqual(admin.get(URL_FILTERS).status_code, 200)
        # 抽屉端点同一节点闸（第二批）；eid 随便给——403 来自权限层、先于参数校验。
        self.assertEqual(
            admin.get(URL_MEMBER_ORDERS, {"eid": "cm-perm-eid"}).status_code, 200
        )

        holder = make_logged_in_client(
            username="cm-perm-holder", groups=("客服",), nodes=("customer.member_list",),
        )
        self.assertEqual(holder.get(URL_MEMBERS).status_code, 200)
        self.assertEqual(
            holder.get(URL_MEMBER_ORDERS, {"eid": "cm-perm-eid"}).status_code, 200
        )

        plain = make_logged_in_client(username="cm-perm-plain")
        for url in (URL_MEMBERS, URL_FILTERS):
            res = plain.get(url)
            self.assertEqual(res.status_code, 403, url)
        res = plain.get(URL_MEMBER_ORDERS, {"eid": "cm-perm-eid"})
        self.assertEqual(res.status_code, 403, res.content)


# ═══════════ 来源订单展示面（第五批 2026-11-11）═══════════════════════════


class MembersRowSetExpansionTests(TenantTestCase):
    """第五批行集扩容（全部建档客户）+ 第六批统计合并的钉值。

    判据：来源单-only 客户出现在列表，其统计**由来源单给出**（订单数 / 累计消费 /
    最近下单 = 有效来源单聚合）；展示列（电话/店铺/录单人）仍取**系统单**最近一单
    （来源单没有这些列 ⇒ 空串）；筛选仍只作用系统订单 ⇒ 来源单-only 行不被任何筛选
    命中、清空筛选即见；`total` 恒全量。
    """

    def _make_order(self, *, order_no, customer=None, created_by="", price="100.00", hour=8,
                    phone=""):
        order = Order.objects.create(
            order_no=order_no, game=self.game, project_text="扩容项目",
            total_price=Decimal(price), created_by=created_by, customer=customer,
            customer_phone=phone,
        )
        Order.objects.filter(pk=order.pk).update(created_at=_aware(hour))
        return order

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-exp-admin")
        self.game = Game.objects.create(name="扩容游戏")
        self.c_linked = Customer.objects.create(eid="cm-exp-eid-linked", buyer_nick="连单客")
        self.c_source = Customer.objects.create(eid="cm-exp-eid-source", buyer_nick="来源客")
        self.c_bare = Customer.objects.create(eid="cm-exp-eid-bare", buyer_nick="裸档客")
        self._make_order(order_no="CEXP0000000000000001", customer=self.c_linked,
                         created_by="客服甲")
        self._make_order(order_no="CEXP0000000000000002", customer=self.c_linked,
                         created_by="客服甲", price="40.00", hour=9, phone="13900000002")
        # 来源单-only：两笔来源单快照，无任何系统单（状态 2/0 都有效）。
        customer_domain.upsert_source_order(
            customer=self.c_source, source_order_no="CEXP-SRC-1", order_status=2,
            total_amount=Decimal("12.00"), updated_time=_aware(10), shop_name="来源店甲",
        )
        customer_domain.upsert_source_order(
            customer=self.c_source, source_order_no="CEXP-SRC-2", order_status=0,
            total_amount=Decimal("7.50"), updated_time=_aware(11), shop_name="来源店乙",
        )

    def test_source_only_customer_stats_come_from_source_orders(self):
        body = self.client.get(URL_MEMBERS).json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        # total = 客户表全量（3，含裸档客）；行集也 = 全部建档客户。
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["filtered_total"], 3)
        rows = {r["eid"]: r for r in data["items"]}
        self.assertEqual(len(rows), 3)
        # 来源单-only 客户：统计 = 两笔有效来源单的合并（12.00 + 7.50、最近 11:00）。
        src = rows["cm-exp-eid-source"]
        self.assertEqual(src["order_count"], 2)
        self.assertEqual(src["total_amount"], "19.50")
        self.assertEqual(src["last_order_at"], "2026-06-01T11:00:00+08:00")
        # 展示列仍取系统单最近一单：来源单-only 没有系统单 ⇒ 全空（第五批口径不变）。
        self.assertEqual(src["phone"], "")
        self.assertEqual(src["recorder"], "")
        self.assertEqual(src["order_source"], None)
        self.assertEqual(src["source_name"], "")
        linked = rows["cm-exp-eid-linked"]
        self.assertEqual(linked["order_count"], 2)
        self.assertEqual(linked["total_amount"], "140.00")
        # 裸档客（两侧都零单）：计数 0 / 金额 0.00 / 最近下单空。
        bare = rows["cm-exp-eid-bare"]
        self.assertEqual(bare["order_count"], 0)
        self.assertEqual(bare["total_amount"], "0.00")
        self.assertEqual(bare["last_order_at"], "")

    def test_order_count_sort_counts_both_sides_with_zero_rows_last(self):
        # 连单客与来源客同为 2（来源单计入订单数）⇒ tie-break -pk（来源客建档晚，排前）；
        # 裸档客 0 单垫底。
        body = self.client.get(URL_MEMBERS).json()
        self.assertEqual(
            [r["eid"] for r in body["data"]["items"]],
            ["cm-exp-eid-source", "cm-exp-eid-linked", "cm-exp-eid-bare"],
        )

    def test_total_amount_sort_uses_merged_totals_with_bare_row_last(self):
        """`order_by=total_amount`：合并合计降序——连单客 140.00 → 来源客 19.50 →
        裸档客 0.00（两侧都零单，Coalesce 后恒为数值、不再有 NULL 行）。"""
        body = self.client.get(URL_MEMBERS, {"order_by": "total_amount"}).json()
        rows = body["data"]["items"]
        self.assertEqual(
            [r["eid"] for r in rows],
            ["cm-exp-eid-linked", "cm-exp-eid-source", "cm-exp-eid-bare"],
        )
        self.assertEqual(rows[0]["total_amount"], "140.00")
        self.assertEqual(rows[1]["total_amount"], "19.50")
        self.assertEqual(rows[2]["total_amount"], "0.00")

    def test_source_only_customer_is_invisible_under_any_filter(self):
        """筛选只作用系统订单：来源单-only / 裸档客不被命中；清空筛选即见（非空哨兵 =
        无筛选时它们在列，见上一条）。筛选谁都不命中 ⇒ 行集为空、全量数不变。"""
        for params, expected in (
            ({"recorder": "客服甲"}, ["cm-exp-eid-linked"]),
            ({"phone": "139"}, ["cm-exp-eid-linked"]),
            ({"recorder": "不存在的客服"}, []),
        ):
            with self.subTest(params=params):
                body = self.client.get(URL_MEMBERS, params).json()
                self.assertEqual([r["eid"] for r in body["data"]["items"]], expected)
                self.assertEqual(body["data"]["filtered_total"], len(expected))
                self.assertEqual(body["data"]["total"], 3)  # 全量数不受筛选影响


class MembersAggregateInflationRegressionTests(TenantTestCase):
    """会员行聚合连乘回归钉（2026-11-11 hotfix）× 第六批统计合并：同客户 N 张系统单
    + M 张来源单的读数必须 = **两段之和**，不是它们的乘积。

    修前形态：订单聚合与来源单计数混进一条查询的两个跨关系 JOIN ⇒ 每张系统单 ×
    每张来源单一行（生产实测一行 260=10×26）。第六批合并后钉值改为合并口径：
    订单数 = N+M、累计消费 = 系统单合计 + 有效来源单合计——连乘形态会给出
    (N+M)×M 与 (Σsys+Σsrc)×M，钉值可分辨。金额造同值（10.00×2）让「连乘再求和」
    无处遁形；零系统单 + 有来源单的行 = 纯来源单侧读数（计数/金额/最近下单全来自来源单）。
    """

    def _make_order(self, order_no, *, customer, price, hour, phone="", created_by="客服甲"):
        order = Order.objects.create(
            order_no=order_no, game=self.game, project_text="回归钉项目",
            total_price=Decimal(price), customer=customer,
            customer_phone=phone, created_by=created_by,
        )
        Order.objects.filter(pk=order.pk).update(created_at=_aware(hour))
        return order

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-infl-admin")
        self.game = Game.objects.create(name="回归钉游戏")
        # 客户 A：3 张系统单（两张同为 10.00；最新 20:00）+ 2 张来源单（合计 3.00）。
        self.c_a = Customer.objects.create(eid="cm-infl-eid-a", buyer_nick="连乘客")
        self._make_order("CINF0000000000000001", customer=self.c_a,
                         price="10.00", hour=8, created_by="客服乙", phone="13800000001")
        self._make_order("CINF0000000000000002", customer=self.c_a,
                         price="10.00", hour=12, phone="13800000002")
        self._make_order("CINF0000000000000003", customer=self.c_a,
                         price="5.00", hour=20, phone="13900000009")
        customer_domain.upsert_source_order(
            customer=self.c_a, source_order_no="CINF-SRC-1",
            total_amount=Decimal("1.00"), updated_time=_aware(9),
        )
        customer_domain.upsert_source_order(
            customer=self.c_a, source_order_no="CINF-SRC-2",
            total_amount=Decimal("2.00"), updated_time=_aware(10),
        )
        # 客户 B：零系统单 + 2 张来源单（读数必须全部来自来源单侧）。
        self.c_b = Customer.objects.create(eid="cm-infl-eid-b", buyer_nick="零单客")
        for no, hour in (("CINF-SRC-B1", 9), ("CINF-SRC-B2", 10)):
            customer_domain.upsert_source_order(
                customer=self.c_b, source_order_no=no,
                total_amount=Decimal("9.00"), updated_time=_aware(hour),
            )

    def test_row_aggregates_are_the_sum_of_both_sides(self):
        data = self.client.get(URL_MEMBERS).json()["data"]
        rows = {r["eid"]: r for r in data["items"]}
        a = rows["cm-infl-eid-a"]
        self.assertEqual(a["order_count"], 5)          # 3 系统 + 2 来源；连乘形态 = 6
        self.assertEqual(a["total_amount"], "28.00")   # 25.00 + 3.00；连乘形态 = 50.00
        # 最近下单 = 两段 Max 取大者（系统 20:00 > 来源 10:00）。
        self.assertEqual(a["last_order_at"], "2026-06-01T20:00:00+08:00")
        # 展示列 = 筛选范围内最近一单（系统单 20:00 那张的电话）。
        self.assertEqual(a["phone"], "13900000009")
        b = rows["cm-infl-eid-b"]
        self.assertEqual(b["order_count"], 2)
        self.assertEqual(b["total_amount"], "18.00")
        self.assertEqual(b["last_order_at"], "2026-06-01T10:00:00+08:00")
        self.assertEqual(b["phone"], "")

    def test_scoped_aggregates_filter_system_side_and_keep_source_side_full(self):
        # 筛选只作用系统订单：命中 2 张 ⇒ 系统侧 2/15.00；来源单侧不随筛选变（2/3.00）。
        body = self.client.get(URL_MEMBERS, {"recorder": "客服甲"}).json()["data"]
        self.assertEqual(body["filtered_total"], 1)
        row = body["items"][0]
        self.assertEqual(row["eid"], "cm-infl-eid-a")
        self.assertEqual(row["order_count"], 4)
        self.assertEqual(row["total_amount"], "18.00")

    def test_total_amount_sort_uses_merged_totals(self):
        rows = self.client.get(URL_MEMBERS, {"order_by": "total_amount"}).json()["data"]["items"]
        self.assertEqual([r["eid"] for r in rows], ["cm-infl-eid-a", "cm-infl-eid-b"])
        self.assertEqual(rows[0]["total_amount"], "28.00")
        self.assertEqual(rows[1]["total_amount"], "18.00")


class MemberOrdersMergeTests(TenantTestCase):
    """抽屉两段并一张表（第六批 §2.5，业务方拍板 + 春雨现场形状）：同单两面按单号去重、
    无效单（23/24）剔除、闲管家状态中文逐码钉值、跨两类时间倒序、两侧各 2,000 截断、
    系统单软删 ⇒ 其来源单恢复可见（不双删）。

    夹具（合成值，形状照春雨：系统单的来源单号命中其来源单——同单两面）：
    客户 A = 3 张系统单（01 @20:00 带 MGS-SRC-DUP-1 / 02 @09:00 / 03 @07:00）+
    来源单 9 张：DUP-1（22，@18:00，与系统单 01 同号 ⇒ 剔）、INV-23（23，@22:00，
    金额 999 ⇒ 剔）、INV-24（24，@16:00 ⇒ 剔）、有效四码 11/12/21/22 各一张 +
    未知码 99 + 无更新时间一张。客户 B 的系统单带来源单号 MGS-SRC-CROSS、客户 A 名下
    有一张同号来源单（@23:00）⇒ 去重判据 = **任意在库系统订单**（租户内）。
    A 的合并列表期望 9 行（时间倒序见 test_merge_pinned_order）。
    """

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-mrg-admin")
        self.game = Game.objects.create(name="合并游戏")
        self.source = OrderSource.objects.create(name="合并店铺", order_by=1)
        self.customer = Customer.objects.create(eid="cm-mrg-eid-a", buyer_nick="合并客")
        self.other = Customer.objects.create(eid="cm-mrg-eid-b", buyer_nick="别家客")
        for order_no, hour, price, status, src_no, creator in (
            ("MGX00000000000000001", 20, "100.00", OrderStatus.COMPLETED, "MGS-SRC-DUP-1", "客服甲"),
            ("MGX00000000000000002", 9, "50.00", OrderStatus.IN_PROGRESS, "", "客服乙"),
            ("MGX00000000000000003", 7, "30.00", OrderStatus.UNASSIGNED, "", "客服甲"),
        ):
            order = Order.objects.create(
                order_no=order_no, game=self.game, project_text="合并项目",
                total_price=Decimal(price), customer=self.customer,
                created_by=creator, order_source=self.source, status=status,
                source_order_no=src_no,
            )
            Order.objects.filter(pk=order.pk).update(created_at=_aware(hour))
        order_b = Order.objects.create(
            order_no="MGX0000000000000000B", game=self.game, project_text="别家项目",
            total_price=Decimal("60.00"), customer=self.other,
            source_order_no="MGS-SRC-CROSS",
        )
        Order.objects.filter(pk=order_b.pk).update(created_at=_aware(23, 30))
        for no, status, amount, hour, minute, shop in (
            ("MGS-SRC-DUP-1", 22, "100.00", 18, 0, "来源店甲"),
            ("MGS-SRC-INV-23", 23, "999.00", 22, 0, "退款店"),
            ("MGS-SRC-INV-24", 24, "888.00", 16, 0, "关闭店"),
            ("MGS-SRC-11", 11, "9.90", 21, 0, "来源店甲"),
            ("MGS-SRC-12", 12, "12.00", 14, 0, "来源店乙"),
            ("MGS-SRC-21", 21, "21.00", 6, 0, "来源店甲"),
            ("MGS-SRC-22", 22, "22.00", 19, 30, "来源店乙"),
            ("MGS-SRC-99", 99, "2.00", 13, 0, "未知店"),
        ):
            customer_domain.upsert_source_order(
                customer=self.customer, source_order_no=no, order_status=status,
                total_amount=Decimal(amount),
                updated_time=_aware(hour, minute),
                shop_name=shop,
            )
        customer_domain.upsert_source_order(
            customer=self.customer, source_order_no="MGS-SRC-NULL", order_status=22,
            total_amount=Decimal("1.00"), shop_name="无时间店",
        )
        customer_domain.upsert_source_order(
            customer=self.customer, source_order_no="MGS-SRC-CROSS", order_status=22,
            total_amount=Decimal("500.00"), updated_time=_aware(23), shop_name="跨客店",
        )

    #: A 的合并列表期望顺序（时间倒序；NULL 时间垫底）。
    EXPECTED_NOS = [
        "MGS-SRC-11", "MGX00000000000000001", "MGS-SRC-22", "MGS-SRC-12",
        "MGS-SRC-99", "MGX00000000000000002", "MGX00000000000000003",
        "MGS-SRC-21", "MGS-SRC-NULL",
    ]

    def _drawer(self, **params):
        return self.client.get(URL_MEMBER_ORDERS, {"eid": "cm-mrg-eid-a", **params}).json()

    def test_merge_pinned_order_and_same_order_counts_once(self):
        body = self._drawer()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        # 3 系统单 + 6 有效来源单 = 9；无效 2 张与两面重复 2 张都不在。
        self.assertEqual(data["total"], 9)
        self.assertEqual([r["no"] for r in data["items"]], self.EXPECTED_NOS)
        rows = {r["no"]: r for r in data["items"]}
        # 同单两面：来源侧的同号单被剔、系统单只出现一次（is_system=True）。
        self.assertNotIn("MGS-SRC-DUP-1", rows)
        self.assertTrue(rows["MGX00000000000000001"]["is_system"])
        self.assertEqual(
            sum(1 for r in data["items"] if r["is_system"]), 3,
        )
        # 去重判据 = 任意在库系统订单（租户内）：命中**别家客户**系统单号的来源单同样被剔。
        self.assertNotIn("MGS-SRC-CROSS", rows)
        # 无效状态（23/24）不进列表——22:00/16:00 两张若混入会插在列表前中段。
        self.assertNotIn("MGS-SRC-INV-23", rows)
        self.assertNotIn("MGS-SRC-INV-24", rows)
        # 无更新时间的来源单垫底（nulls_last 同款先例）。
        self.assertEqual(rows["MGS-SRC-NULL"]["time"], "")
        self.assertEqual(data["items"][-1]["no"], "MGS-SRC-NULL")

    def test_source_row_shape_pinned(self):
        row = {r["no"]: r for r in self._drawer()["data"]["items"]}["MGS-SRC-11"]
        self.assertEqual(
            set(row),
            {"no", "is_system", "status", "game", "project", "amount", "time",
             "status_text", "shop", "recorder"},
        )
        self.assertFalse(row["is_system"])
        # `status` = 闲管家码原值（与系统单的 OrderStatus 枚举不可比，前端不拿它映射）。
        self.assertEqual(row["status"], 11)
        self.assertEqual(row["game"], "")
        self.assertEqual(row["project"], "")
        self.assertEqual(row["amount"], "9.90")
        self.assertEqual(row["time"], "2026-06-01T21:00:00+08:00")
        self.assertEqual(row["shop"], "来源店甲")
        self.assertEqual(row["recorder"], "")

    def test_status_text_pinned_per_code(self):
        rows = {r["no"]: r for r in self._drawer()["data"]["items"]}
        # 有效四码进列表、中文逐码钉死（docs/XGJ-INTEGRATION.md §5）。
        self.assertEqual(rows["MGS-SRC-11"]["status_text"], "待付款")
        self.assertEqual(rows["MGS-SRC-12"]["status_text"], "待发货")
        self.assertEqual(rows["MGS-SRC-21"]["status_text"], "已发货")
        self.assertEqual(rows["MGS-SRC-22"]["status_text"], "已完成")
        # 未知码兜底「未知」（99 不在码表也不在剔除集）。
        self.assertEqual(rows["MGS-SRC-99"]["status_text"], "未知")
        # 系统单状态中文 = OrderStatus label（models 唯一出处）。
        self.assertEqual(rows["MGX00000000000000001"]["status_text"], "已完成")
        self.assertEqual(rows["MGX00000000000000002"]["status_text"], "代练中")
        self.assertEqual(rows["MGX00000000000000003"]["status_text"], "未分配")
        # 23/24 被剔除、进不了列表 ⇒ 中文映射直接钉在码表常量上（防漏译）。
        from apps.business.api import customer_views

        self.assertEqual(customer_views.XGJ_SOURCE_STATUS_LABELS[23], "已退款")
        self.assertEqual(customer_views.XGJ_SOURCE_STATUS_LABELS[24], "已关闭")
        self.assertEqual(customer_views.XGJ_SOURCE_STATUS_UNKNOWN, "未知")

    def test_soft_deleted_system_order_hands_its_source_side_back(self):
        """系统单被软删 ⇒ 其来源单回到抽屉与统计（`valid_source_orders` 声称的承重语义
        「系统单代表被删 ⇒ 来源单恢复可见、不双删」，修复轮钉死）：默认 manager 不再让
        系统单代表它 ⇒ 同号来源单恢复可见；客户行数与金额不双算、也不塌掉。"""
        # 删除前：同号来源单被系统单代表，不在抽屉（合并去重判据，见上面的钉序用例）。
        self.assertNotIn(
            "MGS-SRC-DUP-1",
            [r["no"] for r in self._drawer()["data"]["items"]],
        )
        Order.all_objects.filter(order_no="MGX00000000000000001").update(
            deleted_at=timezone.now()
        )
        data = self._drawer()["data"]
        rows = {r["no"]: r for r in data["items"]}
        self.assertNotIn("MGX00000000000000001", rows)
        self.assertIn("MGS-SRC-DUP-1", rows)
        self.assertFalse(rows["MGS-SRC-DUP-1"]["is_system"])
        # 来源单行走闲管家中文（22 的码表值）；`status` = 闲管家码原值。
        self.assertEqual(rows["MGS-SRC-DUP-1"]["status_text"], "已完成")
        self.assertEqual(rows["MGS-SRC-DUP-1"]["status"], 22)
        # 抽屉行数 = 2 系统 + 7 有效来源 = 9（不双删：与删除前同值，同单两面只算一张）。
        self.assertEqual(data["total"], 9)
        # 统计侧：2 系统单（80）+ 7 来源单（167.90）= 9 张 / 247.90——与删除前逐值相同，
        # 已删系统单既不双算、也不让客户总额塌掉。
        row = {
            r["eid"]: r
            for r in self.client.get(URL_MEMBERS).json()["data"]["items"]
        }["cm-mrg-eid-a"]
        self.assertEqual(row["order_count"], 9)
        self.assertEqual(row["total_amount"], "247.90")

    def test_members_row_stats_are_merged_deduped_and_last_order_crosses_sides(self):
        """members 行统计合并（第六批）：A = 3 系统 + 6 有效来源 = 9、
        消费 180.00 + 67.90；最近下单取两段大者（来源 21:00 > 系统 20:00 ⇒ 21:00，
        GREATEST 跨段取大的实测钉）。同号重复与无效单不进统计。"""
        data = self.client.get(URL_MEMBERS).json()["data"]
        rows = {r["eid"]: r for r in data["items"]}
        a = rows["cm-mrg-eid-a"]
        self.assertEqual(a["order_count"], 9)
        self.assertEqual(a["total_amount"], "247.90")
        self.assertEqual(a["last_order_at"], "2026-06-01T21:00:00+08:00")
        b = rows["cm-mrg-eid-b"]
        self.assertEqual(b["order_count"], 1)
        self.assertEqual(b["total_amount"], "60.00")

    def test_drawer_pagination_slices_the_merged_list(self):
        body = self._drawer(page=2, page_size=4)
        data = body["data"]
        self.assertEqual(data["total"], 9)
        self.assertEqual(data["page"], 2)
        self.assertEqual([r["no"] for r in data["items"]], self.EXPECTED_NOS[4:8])

    def test_source_side_cap_is_2000_newest_kept(self):
        """来源单侧截断保护：有效来源单超过 2,000 ⇒ 取最近 2,000 条（计划 §2.3），
        `total` = 系统单 3 + 2000；若不截断会是 3 + 2007。"""
        CustomerSourceOrder.objects.bulk_create([
            CustomerSourceOrder(
                customer=self.customer, source_order_no=f"MGS-SRC-CAP-{i:04d}",
                order_status=22, total_amount=Decimal("1.00"),
                updated_time=_aware(1) + timedelta(hours=i), shop_name=f"店{i}",
            )
            for i in range(1, 2002)
        ])
        body = self._drawer()
        self.assertEqual(body["data"]["total"], 2003)
        self.assertEqual(body["data"]["items"][0]["no"], "MGS-SRC-CAP-2001")
        self.assertEqual(body["data"]["items"][0]["is_system"], False)

    def test_system_side_cap_is_2000_newest_kept(self):
        """系统单侧同款截断保护（修复轮补齐）：系统单超过 2,000 ⇒ 取最近 2,000 条
        （`Order.Meta.ordering` = -created_at, -id，切片即「最近优先」），
        `total` = 2000 + 有效来源单 6（不截断会是 2005 + 6 = 2011）。"""
        Order.objects.bulk_create([
            Order(
                order_no=f"MGXCAP{i:014d}", game=self.game, project_text="截断项目",
                total_price=Decimal("1.00"), customer=self.customer,
                created_by="客服甲", status=OrderStatus.COMPLETED,
            )
            for i in range(1, 2003)
        ])
        data = self._drawer()["data"]
        self.assertEqual(data["total"], 2006)
        # 批量行 created_at 同为当下（晚于夹具的 6 月时刻）⇒ id 最大（最后建）的最前。
        self.assertEqual(data["items"][0]["no"], "MGXCAP00000000002002")
        self.assertTrue(data["items"][0]["is_system"])


# ═══════════ 跨租户隔离 ═══════════


class CustomerMembersIsolationTests(TenantTestCase):
    """「以租户 A 的凭据访问 B 的客户行必须看不到」；判据 = `eid`（租户内唯一、两租户取值不同），
    ⛔ 主键（两个 schema 的 id 序列各自从 1 起，主键断言恒真）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="客户线隔离租户 B", domain=DOMAIN_B
        )

    @staticmethod
    def _seed_linked_order(game, customer, order_no):
        """租户内最小可聚合形状：一笔连了客户的在库单（行集非空的哨兵底座）。"""
        Order.objects.create(
            order_no=order_no,
            game=game,
            project_text="隔离项目",
            customer=customer,
        )

    def setUp(self):
        # 跨域名请求不会在响应结束时还原 schema（先例同款），每条用例开头钉回租户 A。
        connection.set_tenant(self.tenant)

    def test_recorder_counts_cannot_cross_tenant(self):
        with tenant_context(self.tenant):
            game = Game.objects.create(name="录单统计游戏甲")
            customer = Customer.objects.create(eid="recorder-iso-a", buyer_nick="甲")
            Order.objects.create(order_no="RECISO00000000000001", game=game, customer=customer,
                                 project_text="甲", created_by="recorder-only-a")
            client_a = make_logged_in_client(domain=DOMAIN_A, username="recorder-admin-a", is_superuser=True)
        with tenant_context(self.tenant_b):
            game = Game.objects.create(name="录单统计游戏乙")
            customer = Customer.objects.create(eid="recorder-iso-b", buyer_nick="乙")
            Order.objects.create(order_no="RECISO00000000000002", game=game, customer=customer,
                                 project_text="乙", created_by="recorder-only-b")
            client_b = make_logged_in_client(domain=DOMAIN_B, username="recorder-admin-b", is_superuser=True)
        try:
            a = client_a.get(URL_MEMBERS, SERVER_NAME=DOMAIN_A).json()["data"]["recorder_counts"]
            b = client_b.get(URL_MEMBERS, SERVER_NAME=DOMAIN_B).json()["data"]["recorder_counts"]
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual([(x["recorder"], x["count"]) for x in a], [("recorder-only-a", 1)])
        self.assertEqual([(x["recorder"], x["count"]) for x in b], [("recorder-only-b", 1)])

    def test_each_tenant_sees_only_its_own_customers(self):
        # ⚠️ 会员行 = 「已连客户且有单」⇒ 每个租户都要造**一笔连了客户**的单，
        # 否则行集为空、否定断言被空结果凑出来（E-0004：空结果 ≠ 没有数据）。
        with tenant_context(self.tenant):
            game = Game.objects.create(name="隔离游戏A")
            customer_a = Customer.objects.create(eid=EID_A, buyer_nick=NICK_A)
            self._seed_linked_order(game, customer_a, "CMISO00000000000001")
            a_client = make_logged_in_client(
                domain=DOMAIN_A, username="cm-iso-a", is_superuser=True,
            )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="隔离游戏B")
            customer_b = Customer.objects.create(eid=EID_B, buyer_nick=NICK_B)
            self._seed_linked_order(game_b, customer_b, "CMISO00000000000002")
            b_client = make_logged_in_client(
                domain=DOMAIN_B, username="cm-iso-b", is_superuser=True,
            )

        # ── A 侧：非空哨兵（看得到自己的）+ 否定断言（看不到 B 的）──────────
        a_body = a_client.get(URL_MEMBERS, SERVER_NAME=DOMAIN_A).json()
        self.assertEqual(a_body["code"], 0, a_body)
        self.assertEqual(a_body["data"]["total"], 1)             # 非空哨兵
        self.assertEqual([r["eid"] for r in a_body["data"]["items"]], [EID_A])
        self.assertNotIn(EID_B, [r["eid"] for r in a_body["data"]["items"]])

        # ── B 侧：同样两个方向（请求在 tenant_context 块外 + 显式 SERVER_NAME）──
        try:
            b_body = b_client.get(URL_MEMBERS, SERVER_NAME=DOMAIN_B).json()
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(b_body["code"], 0, b_body)
        self.assertEqual(b_body["data"]["total"], 1)
        self.assertEqual([r["eid"] for r in b_body["data"]["items"]], [EID_B])
        self.assertNotIn(EID_A, [r["eid"] for r in b_body["data"]["items"]])

    def test_source_orders_stay_inside_own_tenant(self):
        """来源单快照同样只在各自租户 schema（第五批）：判据 = 来源单号（租户内唯一、
        两租户取值不同，⛔ 主键）；每侧先造非空哨兵（自己的来源单在合并抽屉里可见，
        第六批：is_system=False 行）。"""
        with tenant_context(self.tenant):
            game = Game.objects.create(name="隔离游戏A")
            customer_a = Customer.objects.create(eid=EID_A, buyer_nick=NICK_A)
            self._seed_linked_order(game, customer_a, "CMISO00000000000001")
            customer_domain.upsert_source_order(
                customer=customer_a, source_order_no="CMISO-SRC-A",
                total_amount=Decimal("1.00"), updated_time=_aware(8),
            )
            a_client = make_logged_in_client(
                domain=DOMAIN_A, username="cm-iso-a", is_superuser=True,
            )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="隔离游戏B")
            customer_b = Customer.objects.create(eid=EID_B, buyer_nick=NICK_B)
            self._seed_linked_order(game_b, customer_b, "CMISO00000000000002")
            customer_domain.upsert_source_order(
                customer=customer_b, source_order_no="CMISO-SRC-B",
                total_amount=Decimal("2.00"), updated_time=_aware(9),
            )
            b_client = make_logged_in_client(
                domain=DOMAIN_B, username="cm-iso-b", is_superuser=True,
            )

        a_body = a_client.get(URL_MEMBER_ORDERS, {"eid": EID_A}, SERVER_NAME=DOMAIN_A).json()
        self.assertEqual(a_body["code"], 0, a_body)
        # 非空哨兵：自己的系统单 + 来源单都在合并列表里（来源侧 07:00 < 系统 08:00... 皆钉形状）。
        a_rows = {r["no"]: r["is_system"] for r in a_body["data"]["items"]}
        self.assertEqual(
            a_rows, {"CMISO00000000000001": True, "CMISO-SRC-A": False}
        )
        self.assertNotIn("CMISO-SRC-B", a_rows)              # 否定断言
        try:
            b_body = b_client.get(URL_MEMBER_ORDERS, {"eid": EID_B}, SERVER_NAME=DOMAIN_B).json()
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(b_body["code"], 0, b_body)
        b_rows = {r["no"]: r["is_system"] for r in b_body["data"]["items"]}
        self.assertEqual(
            b_rows, {"CMISO00000000000002": True, "CMISO-SRC-B": False}
        )
        self.assertNotIn("CMISO-SRC-A", b_rows)

    def test_member_orders_drawer_cross_tenant_eid_is_not_visible(self):
        """抽屉按 `eid` 取客户：A 的凭据查 B 的 eid 必须「客户不存在」（判据 = eid 租户内唯一
        且两租户取值不同，⛔ 主键）；查自己的 eid 照常 200（非空哨兵，E-0004）。"""
        with tenant_context(self.tenant):
            game = Game.objects.create(name="隔离游戏A")
            customer_a = Customer.objects.create(eid=EID_A, buyer_nick=NICK_A)
            self._seed_linked_order(game, customer_a, "CMISO00000000000001")
            a_client = make_logged_in_client(
                domain=DOMAIN_A, username="cm-iso-a", is_superuser=True,
            )
        with tenant_context(self.tenant_b):
            game_b = Game.objects.create(name="隔离游戏B")
            customer_b = Customer.objects.create(eid=EID_B, buyer_nick=NICK_B)
            self._seed_linked_order(game_b, customer_b, "CMISO00000000000002")
            b_client = make_logged_in_client(
                domain=DOMAIN_B, username="cm-iso-b", is_superuser=True,
            )

        a_own = a_client.get(URL_MEMBER_ORDERS, {"eid": EID_A}, SERVER_NAME=DOMAIN_A).json()
        self.assertEqual(a_own["code"], 0, a_own)
        self.assertEqual(a_own["data"]["total"], 1)  # 非空哨兵（本用例只有 1 张系统单）
        a_cross = a_client.get(URL_MEMBER_ORDERS, {"eid": EID_B}, SERVER_NAME=DOMAIN_A).json()
        self.assertEqual(a_cross["code"], 1000, a_cross)
        self.assertEqual(a_cross["msg"], "客户不存在")
        try:
            b_own = b_client.get(URL_MEMBER_ORDERS, {"eid": EID_B}, SERVER_NAME=DOMAIN_B).json()
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(b_own["code"], 0, b_own)
        self.assertEqual(b_own["data"]["total"], 1)


# ═══════════ 分析报表（第三批）═════════════════════════════════════════


class CustomerAnalysisApiTests(TenantTestCase):
    """analysis 端点行为判据（2026-11-10 第三批）。

    夹具 = 6 客户：甲（本期 3 单含负数单与未挂字典快照单）、乙（本期 1 单 + 40 天前
    1 单 ⇒ 30d/90d 两档读数可分）、丙/丁/戊（恰好 100/500/2000 落档探针）、
    己（零单 ⇒ 落 0-100 档且不进增长）。全部时刻相对 now 生成。
    另两支探针各自用例内自建：未连客户的 999 单（排除语义）、89/90 与 364/365 档界单。
    """

    def _order(self, order_no, *, customer, price, days_ago, source=None, source_text=""):
        order = Order.objects.create(
            order_no=order_no,
            game=self.game,
            project_text="分析项目",
            total_price=Decimal(price),
            customer=customer,
            order_source=source,
            source=source_text,
            created_by="分析客服",
        )
        Order.objects.filter(pk=order.pk).update(created_at=_ago(days_ago))
        return order

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-ana-admin")
        self.game = Game.objects.create(name="分析游戏")
        self.source_a = OrderSource.objects.create(name="分析店铺甲", order_by=9)
        self.source_b = OrderSource.objects.create(name="分析店铺乙", order_by=1)
        self.now = timezone.now()
        self.c_a = Customer.objects.create(eid="cm-ana-eid-a", buyer_nick="分析客甲")
        self.c_b = Customer.objects.create(eid="cm-ana-eid-b", buyer_nick="分析客乙")
        self.c_c = Customer.objects.create(eid="cm-ana-eid-c", buyer_nick="恰好一百")
        self.c_d = Customer.objects.create(eid="cm-ana-eid-d", buyer_nick="恰好五百")
        self.c_e = Customer.objects.create(eid="cm-ana-eid-e", buyer_nick="恰好两千")
        self.c_f = Customer.objects.create(eid="cm-ana-eid-f", buyer_nick="零单客")
        # 甲：3 单（含负数冲减与未挂字典的快照单），首单 = 2 天前。
        self._order("CANA0000000000001", customer=self.c_a, price="100.00", days_ago=1,
                    source=self.source_a)
        self._order("CANA0000000000002", customer=self.c_a, price="-30.00", days_ago=2,
                    source=self.source_b)
        self._order("CANA0000000000003", customer=self.c_a, price="10.00", days_ago=1,
                    source_text="闲鱼快照甲")
        # 乙：本期 1 单 + 40 天前 1 单（40 天必跨月界 ⇒ 首单月 ≠ 建档月）。
        self._order("CANB0000000000001", customer=self.c_b, price="200.00", days_ago=3,
                    source=self.source_a)
        self._order("CANB0000000000002", customer=self.c_b, price="50.00", days_ago=40,
                    source=self.source_b)
        # 丙/丁/戊：恰好落在档界 100/500/2000 上（各 1 单）。
        self._order("CANC0000000000001", customer=self.c_c, price="100.00", days_ago=4,
                    source=self.source_a)
        self._order("CAND0000000000001", customer=self.c_d, price="500.00", days_ago=5,
                    source=self.source_a)
        self._order("CANE0000000000001", customer=self.c_e, price="2000.00", days_ago=6,
                    source=self.source_a)

    def test_default_range_30d_kpi_pinned_with_negative_order(self):
        body = self.client.get(URL_ANALYSIS).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["range"], "30d")
        kpi = body["data"]["kpi"]
        self.assertEqual(kpi["customer_total"], 6)         # 恒全量（零单客也算）
        self.assertEqual(kpi["period_orders"], 7)          # 甲 3 + 乙 1 + 丙丁戊各 1
        # 100 − 30 + 10 + 200 + 100 + 500 + 2000 = 2880（负数照算 = 退款冲减）。
        self.assertEqual(kpi["period_amount"], "2880.00")
        self.assertEqual(kpi["avg_order"], "411.43")       # 2880 ÷ 7（HALF_UP）
        self.assertEqual(kpi["repeat_customers"], 1)       # 只有甲在本期 ≥2 单

    def test_source_dist_is_period_only_with_snapshot_and_unset_rows(self):
        data = self.client.get(URL_ANALYSIS).json()["data"]
        rows = data["source_dist"]
        # 金额降序：甲 2900（五单字典单）→ 快照 10 → 乙 −30（负行照实返回，扇形侧自滤）。
        self.assertEqual(
            [(r["order_source"], r["name"], r["amount"]) for r in rows],
            [
                (self.source_a.pk, "分析店铺甲", "2900.00"),
                (None, "闲鱼快照甲", "10.00"),
                (self.source_b.pk, "分析店铺乙", "-30.00"),
            ],
        )

    def test_consume_tiers_are_all_time_with_exact_bounds_rolling_up(self):
        data = self.client.get(URL_ANALYSIS).json()["data"]
        # 全时段累计：甲 80、乙 250（含 40 天前那单）、丙 100、丁 500、戊 2000、己零单。
        # 恰好落在档界上的归**高**一档；零单客按 0 落首档（不随 range 变，range=12m 同值）。
        expected = [
            {"label": "0-100", "count": 2},      # 甲 80 + 己 0
            {"label": "100-500", "count": 2},    # 乙 250 + 丙 100（恰好 100 落本档）
            {"label": "500-2000", "count": 1},   # 丁 500（恰好 500 落本档）
            {"label": "2000+", "count": 1},      # 戊 2000（恰好 2000 落本档）
        ]
        self.assertEqual(data["consume_tiers"], expected)
        data_12m = self.client.get(URL_ANALYSIS, {"range": "12m"}).json()["data"]
        self.assertEqual(data_12m["consume_tiers"], expected)

    def test_range_90d_pulls_the_40_day_order_into_period(self):
        data = self.client.get(URL_ANALYSIS, {"range": "90d"}).json()["data"]
        self.assertEqual(data["range"], "90d")
        kpi = data["kpi"]
        self.assertEqual(kpi["period_orders"], 8)          # 30d 的 7 单 + 乙的 40 天前单
        self.assertEqual(kpi["period_amount"], "2930.00")  # 2880 + 50
        self.assertEqual(kpi["avg_order"], "366.25")       # 2930 ÷ 8
        self.assertEqual(kpi["repeat_customers"], 2)       # 甲、乙都 ≥2 单
        rows = {r["name"]: r["amount"] for r in data["source_dist"]}
        self.assertEqual(rows["分析店铺乙"], "20.00")       # −30 + 50（来源分布随档位变）

    def test_range_12m_covers_the_same_year_of_orders(self):
        kpi = self.client.get(URL_ANALYSIS, {"range": "12m"}).json()["data"]["kpi"]
        self.assertEqual(kpi["period_orders"], 8)
        self.assertEqual(kpi["period_amount"], "2930.00")

    def test_window_boundary_is_date_inclusive_on_both_ends(self):
        # 窗首（today−29）落窗内、窗首前一天（today−30）落窗外（含首含尾）。
        c_in = Customer.objects.create(eid="cm-ana-edge-in")
        c_out = Customer.objects.create(eid="cm-ana-edge-out")
        self._order("CANE0000000000090", customer=c_in, price="10.00", days_ago=29)
        self._order("CANE0000000000091", customer=c_out, price="20.00", days_ago=30)
        kpi = self.client.get(URL_ANALYSIS).json()["data"]["kpi"]
        self.assertEqual(kpi["period_orders"], 8)          # 夹具 7 + 窗首 1
        self.assertEqual(kpi["period_amount"], "2890.00")  # 2880 + 10

    def test_window_boundaries_90d_and_12m_are_inclusive_on_both_ends(self):
        # 90d 窗首 = today−89（89 在、90 出）；12m 窗首 = today−364（364 在、365 出）。
        self._order("CANE0000000000890", customer=self.c_a, price="10.00", days_ago=89)
        self._order("CANE0000000000900", customer=self.c_b, price="20.00", days_ago=90)
        self._order("CANE0000000000364", customer=self.c_a, price="30.00", days_ago=364)
        self._order("CANE0000000000365", customer=self.c_b, price="40.00", days_ago=365)
        kpi_90 = self.client.get(URL_ANALYSIS, {"range": "90d"}).json()["data"]["kpi"]
        self.assertEqual(kpi_90["period_orders"], 9)          # 夹具 8（含乙 40 天单）+ 89 天那单；90 天出窗
        self.assertEqual(kpi_90["period_amount"], "2940.00")  # 2930 + 10（不是 +10+20）
        kpi_12m = self.client.get(URL_ANALYSIS, {"range": "12m"}).json()["data"]["kpi"]
        self.assertEqual(kpi_12m["period_orders"], 11)         # 夹具 8 + 89 + 90 + 364；只有 365 出窗
        self.assertEqual(kpi_12m["period_amount"], "2990.00")  # 2930 + 10 + 20 + 30（365 那单的 40 不进）

    def test_growth_counts_by_first_order_month_never_created_at(self):
        """增长分月按**首单时间**：全部客户建档于「今天」（回填形态），若按建档时间分月
        会塌成一根本月柱；本用例钉住乙的首单月（40 天前，必跨月界）。"""
        body = self.client.get(URL_ANALYSIS, {"range": "12m"}).json()
        growth = body["data"]["growth"]
        self.assertEqual(len(growth), 12)
        self.assertEqual(growth[-1]["month"], _month_key(self.now))
        expected: dict[str, int] = {}
        for days in (2, 40, 4, 5, 6):                      # 甲乙丙丁戊的首单时刻（己零单不计）
            key = _month_key(self.now - timedelta(days=days))
            expected[key] = expected.get(key, 0) + 1
        for point in growth:
            self.assertEqual(point["count"], expected.get(point["month"], 0), point)
        self.assertEqual(sum(p["count"] for p in growth), 5)
        # 夹具自证：首单月 ≠ 本月（否则「不看建档时间」这条判据被空集凑出来，E-0004）。
        self.assertNotEqual(_month_key(self.now - timedelta(days=40)), _month_key(self.now))
        self.assertIn(_month_key(self.now - timedelta(days=40)), expected)

    def test_malformed_range_is_1000_not_500(self):
        # 畸形档逐字钉 1000（E-0010）；空串按「没传」走缺省 30d（dashboard _params 同款先例）。
        for params, expected_code in (
            ({"range": "7d"}, 1000),
            ({"range": "30d "}, 1000),
            ({"range": ""}, 0),
        ):
            with self.subTest(params=params):
                res = self.client.get(URL_ANALYSIS, params)
                self.assertEqual(res.status_code, 200, res.content)
                self.assertEqual(res.json()["code"], expected_code, res.json())

    def test_period_orders_key_exposes_the_avg_order_denominator(self):
        # avg_order 的分母单独出键（读数可复核）；前端不展示它也不影响。
        kpi = self.client.get(URL_ANALYSIS).json()["data"]["kpi"]
        self.assertEqual(kpi["period_orders"], 7)

    def test_unlinked_order_is_excluded_from_every_period_aggregate(self):
        """未连客户的单不属于任何客户：period_orders / period_amount / source_dist 都不得含它。

        对照假值（`_period_orders` 少 `customer__isnull=False` 时）：单数 8、
        金额 3879.00、店铺甲行 3899.00 —— 三处按 999 显眼金额同时变红。
        """
        self._order("CANE0000000000999", customer=None, price="999.00", days_ago=1,
                    source=self.source_a)
        data = self.client.get(URL_ANALYSIS).json()["data"]
        self.assertEqual(data["kpi"]["period_orders"], 7)
        self.assertEqual(data["kpi"]["period_amount"], "2880.00")
        rows = {(r["order_source"], r["name"]): r["amount"] for r in data["source_dist"]}
        self.assertEqual(rows[(self.source_a.pk, "分析店铺甲")], "2900.00")
        self.assertEqual(
            [r["name"] for r in data["source_dist"]],
            ["分析店铺甲", "闲鱼快照甲", "分析店铺乙"],
        )


class CustomerSourceCustomerDistTests(TenantTestCase):
    """analysis `source_customer_dist`（第七批 §1.3）：各店铺「有订单的客户数」钉值。

    夹具（合成值）：c1 双店客（系统单店铺甲窗口内 + 窗口外各一）、c2 单店客（店铺甲 +
    快照单 + 未指定快照空 + 来源单若干坏形态）、c3 跨侧客（空快照系统单 + 来源单）。
    钉：店內客户去重、跨店各计一次、**同名合并**（来源单 shop_name = 字典名并进同一桶）、
    窗口（系统单按 created_at、来源单按 updated_time、NULL 不落窗）、来源单无效状态
    （23）与已在库系统单同号者不计。
    """

    def _order(self, order_no, *, customer, source=None, source_text="", days_ago=1,
               price="10.00"):
        order = Order.objects.create(
            order_no=order_no, game=self.game, project_text="客户数项目",
            total_price=Decimal(price), customer=customer,
            order_source=source, source=source_text, created_by="客户数客服",
        )
        Order.objects.filter(pk=order.pk).update(created_at=_ago(days_ago))
        return order

    def setUp(self):
        self.client = make_logged_in_client(is_superuser=True, username="cm-scd-admin")
        self.game = Game.objects.create(name="客户数游戏")
        self.source_a = OrderSource.objects.create(name="店铺甲", order_by=9)
        self.c1 = Customer.objects.create(eid="cm-scd-eid-1", buyer_nick="双店客")
        self.c2 = Customer.objects.create(eid="cm-scd-eid-2", buyer_nick="单店客")
        self.c3 = Customer.objects.create(eid="cm-scd-eid-3", buyer_nick="跨侧客")
        # 店铺甲（系统单）：c1、c2 窗口内各一笔（店內去重 = 2），c1 另一笔 40 天前（窗口外）。
        self._order("CSCDA0000000000001", customer=self.c1, source=self.source_a, days_ago=1)
        self._order("CSCDA0000000000002", customer=self.c2, source=self.source_a, days_ago=2)
        self._order("CSCDA0000000000003", customer=self.c1, source=self.source_a, days_ago=40)
        # 未挂字典：c2 快照文本单（独立桶「闲鱼快照乙」）、c3 空快照单（归「未指定」）。
        self._order("CSCDB0000000000001", customer=self.c2, source_text="闲鱼快照乙", days_ago=3)
        self._order("CSCDB0000000000002", customer=self.c3, source_text="", days_ago=4)
        # c2 的一张手工未连来源单号的系统单（快照空 ⇒ 也在「未指定」，与 c3 并桶去重 = 2）。
        self._order("CSCDB0000000000003", customer=self.c2, days_ago=10)
        Order.objects.filter(order_no="CSCDB0000000000003").update(
            source_order_no="CSCD-SRC-6"
        )
        # 来源单侧（shop_name 分桶）：有效 c3→店铺甲（与字典名同名 ⇒ 合并）、
        # c1/c3→来源店丙；c2 的窗口外 / 无效 23 / 同单两面 / NULL 时间四张全不计。
        for no, shop, cust, days, status in (
            ("CSCD-SRC-1", "店铺甲", self.c3, 5, 22),
            ("CSCD-SRC-2", "来源店丙", self.c1, 6, 22),
            ("CSCD-SRC-3", "来源店丙", self.c3, 7, 12),
            ("CSCD-SRC-4", "来源店丙", self.c2, 40, 22),
            ("CSCD-SRC-5", "来源店丙", self.c2, 8, 23),
            ("CSCD-SRC-6", "来源店丙", self.c2, 9, 22),
            ("CSCD-SRC-7", "来源店丙", self.c2, None, 22),
        ):
            customer_domain.upsert_source_order(
                customer=cust, source_order_no=no, order_status=status,
                total_amount=Decimal("1.00"),
                updated_time=None if days is None else _ago(days),
                shop_name=shop,
            )

    def test_customer_counts_dedupe_merge_names_and_follow_range(self):
        data = self.client.get(URL_ANALYSIS).json()["data"]
        # 店铺甲 = 系统 c1/c2 + 来源 c3（同名合并）= 3；未指定 = c3 空快照 + c2 手工单 = 2；
        # 来源店丙 = c1/c3（c2 的四张坏形态来源单全不计）= 2；闲鱼快照乙 = c2 = 1。
        # 同数按名字序（未 U+672A < 来 U+6765 < 闲 U+95F2）。
        self.assertEqual(
            data["source_customer_dist"],
            [
                {"name": "店铺甲", "count": 3},
                {"name": "未指定", "count": 2},
                {"name": "来源店丙", "count": 2},
                {"name": "闲鱼快照乙", "count": 1},
            ],
        )

    def test_range_90d_pulls_out_of_window_orders_back_in(self):
        data = self.client.get(URL_ANALYSIS, {"range": "90d"}).json()["data"]
        # 40 天的窗口外单随 90d 回窗：店铺甲不变（c1 同桶）、来源店丙 +c2 → 3。
        self.assertEqual(
            data["source_customer_dist"],
            [
                {"name": "店铺甲", "count": 3},
                {"name": "来源店丙", "count": 3},
                {"name": "未指定", "count": 2},
                {"name": "闲鱼快照乙", "count": 1},
            ],
        )


class CustomerAnalysisPermissionTests(TenantTestCase):
    """守卫（管理员短路 OR 三节点任一：member_list / member_analysis_report /
    member_list_self_only，plain 403）。

    ⚠️ 翻真同批（2026-11-11）：`member_analysis_report` 是 analysis 端点的**自家门**——
    「只勾分析报表」的职位页面可进、接口必须放行；但成员/筛选/订单抽屉**不认它**
    （任务书 §2：member-orders 可见性保持现状）。
    """

    def test_admin_passes_node_holder_passes_plain_user_403(self):
        admin = make_logged_in_client(is_superuser=True, username="cm-ana-perm-admin")
        self.assertEqual(admin.get(URL_ANALYSIS).status_code, 200)
        holder = make_logged_in_client(
            username="cm-ana-holder", groups=("客服",), nodes=("customer.member_list",),
        )
        self.assertEqual(holder.get(URL_ANALYSIS).status_code, 200)
        plain = make_logged_in_client(username="cm-ana-plain")
        res = plain.get(URL_ANALYSIS)
        self.assertEqual(res.status_code, 403, res.content)

    def test_analysis_report_holder_passes_analysis_but_not_members(self):
        """只持 `member_analysis_report`：analysis 200；成员/筛选/订单抽屉三个端点都 403（本点只当 analysis 的门）。"""
        holder = make_logged_in_client(
            username="cm-ana-report-holder", groups=("客服",),
            nodes=("customer.member_analysis_report",),
        )
        self.assertEqual(holder.get(URL_ANALYSIS).status_code, 200)
        self.assertEqual(holder.get(URL_MEMBERS).status_code, 403)
        self.assertEqual(holder.get(URL_FILTERS).status_code, 403)
        self.assertEqual(holder.get(URL_MEMBER_ORDERS).status_code, 403)

    def test_holder_without_any_customer_node_is_403(self):
        """三节点都不持（只持无关节点）：analysis 403（翻真后仍要过门，不放开）。"""
        outsider = make_logged_in_client(
            username="cm-ana-outsider", groups=("客服",), nodes=("order.list",),
        )
        res = outsider.get(URL_ANALYSIS)
        self.assertEqual(res.status_code, 403, res.content)


class CustomerAnalysisEmptyTests(TenantTestCase):
    """空租户：聚合全零、增长仍回 12 行 0（前端柱线不用补月），无单不 500（E-0021 口径）。"""

    def test_empty_tenant_is_all_zeros_with_twelve_growth_rows(self):
        client = make_logged_in_client(is_superuser=True, username="cm-ana-empty")
        body = client.get(URL_ANALYSIS).json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertEqual(data["range"], "30d")
        self.assertEqual(
            data["kpi"],
            {
                "customer_total": 0,
                "repeat_customers": 0,
                "period_amount": "0.00",
                "period_orders": 0,
                "avg_order": "0.00",
            },
        )
        self.assertEqual(data["source_dist"], [])
        self.assertEqual(data["source_customer_dist"], [])
        self.assertEqual([t["count"] for t in data["consume_tiers"]], [0, 0, 0, 0])
        self.assertEqual([p["count"] for p in data["growth"]], [0] * 12)


class CustomerAnalysisIsolationTests(TenantTestCase):
    """analysis 聚合的跨租户否定断言：A 的读数必须只含 A 的客户（判据 = `eid`
    租户内唯一且两租户取值不同，⛔ 主键；每侧都先造**非空哨兵**，E-0004）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="客户线隔离租户 B", domain=DOMAIN_B
        )

    @staticmethod
    def _seed_linked_orders(count, prefix):
        """租户内 N 个「1 客户 1 连单」的哨兵（eid/单号带租户侧前缀，两租户取值不同）。"""
        game = Game.objects.create(name=f"隔离游戏{prefix}")
        for i in range(1, count + 1):
            customer = Customer.objects.create(eid=f"{prefix}-eid-{i:02d}")
            Order.objects.create(
                order_no=f"{prefix}-NO-{i:02d}",
                game=game,
                project_text="隔离项目",
                customer=customer,
            )

    def setUp(self):
        connection.set_tenant(self.tenant)

    def test_analysis_aggregates_stay_inside_own_tenant(self):
        with tenant_context(self.tenant):
            self._seed_linked_orders(1, "CMAISOA")
            a_client = make_logged_in_client(
                domain=DOMAIN_A, username="cm-ana-iso-a", is_superuser=True,
            )
        with tenant_context(self.tenant_b):
            self._seed_linked_orders(3, "CMAISOB")
            b_client = make_logged_in_client(
                domain=DOMAIN_B, username="cm-ana-iso-b", is_superuser=True,
            )

        a_body = a_client.get(URL_ANALYSIS, SERVER_NAME=DOMAIN_A).json()
        self.assertEqual(a_body["code"], 0, a_body)
        # 非空哨兵（自己 1 个）+ 否定断言（B 的 3 个不进来）。
        self.assertEqual(a_body["data"]["kpi"]["customer_total"], 1)
        self.assertEqual(
            [t["count"] for t in a_body["data"]["consume_tiers"]], [1, 0, 0, 0]
        )
        self.assertEqual(sum(p["count"] for p in a_body["data"]["growth"]), 1)
        # 客户数扇形（第七批）同边界：种子单未挂来源 ⇒ 全落「未指定」桶。
        self.assertEqual(
            a_body["data"]["source_customer_dist"], [{"name": "未指定", "count": 1}]
        )
        try:
            b_body = b_client.get(URL_ANALYSIS, SERVER_NAME=DOMAIN_B).json()
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(b_body["code"], 0, b_body)
        self.assertEqual(b_body["data"]["kpi"]["customer_total"], 3)
        self.assertEqual(
            b_body["data"]["source_customer_dist"], [{"name": "未指定", "count": 3}]
        )


# ═══════════ 限制节点「只看自己录单的客户」（第五批 2026-11-11）════════════════


class MemberListSelfOnlyTests(TenantTestCase):
    """限制节点 `customer.member_list_self_only` 的语义（计划 §2.4，钉死）：

    - 持 `member_list` ⇒ 全量（现状不变）；
    - 只持 `member_list_self_only` ⇒ 四端点可进，行集 = 「录单人=自己的系统订单所连的客户」、
      `total` = 可见行数、来源单-only 不可见、录单人下拉只回自己、抽屉同范围；
    - 两点都无 ⇒ 403（现状）。
    夹具 = 3 客户：自己录的（**另有一张来源单**：限制档统计保持系统单口径的对照面）/
    别人录的 / 来源单-only（判据钉 eid，租户内唯一）。
    """

    URL = URL_MEMBERS

    def setUp(self):
        self.game = Game.objects.create(name="限制线游戏")
        # 录单人账号名 = 登录账号（`Order.created_by` 是用户名快照）。
        self.me = "cm_self_cs"
        self.c_own = Customer.objects.create(eid="cm-so-eid-own", buyer_nick="自己录的客")
        self.c_other = Customer.objects.create(eid="cm-so-eid-other", buyer_nick="别人录的客")
        self.c_source = Customer.objects.create(eid="cm-so-eid-source", buyer_nick="来源单客")
        for order_no, customer, creator, hour in (
            ("CMSO0000000000000001", self.c_own, self.me, 8),
            ("CMSO0000000000000002", self.c_other, "cm_other_cs", 9),
        ):
            order = Order.objects.create(
                order_no=order_no, game=self.game, project_text="限制线项目",
                total_price=Decimal("10.00"), customer=customer, created_by=creator,
            )
            Order.objects.filter(pk=order.pk).update(created_at=_aware(hour))
        customer_domain.upsert_source_order(
            customer=self.c_own, source_order_no="CMSO-SRC-OWN",
            total_amount=Decimal("3.00"), updated_time=_aware(10),
        )
        customer_domain.upsert_source_order(
            customer=self.c_source, source_order_no="CMSO-SRC-1",
            total_amount=Decimal("5.00"), updated_time=_aware(11),
        )

    def _restricted(self):
        return make_logged_in_client(
            username=self.me, groups=("客服",), nodes=("customer.member_list_self_only",),
        )

    def test_restricted_sees_only_own_recorded_customers(self):
        body = self._restricted().get(URL_MEMBERS).json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        # 行集 = 只有自己录单连的那个客户；total = 可见行数（1，不是全量 3）。
        self.assertEqual([r["eid"] for r in data["items"]], ["cm-so-eid-own"])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["filtered_total"], 1)
        self.assertEqual([(x["recorder"], x["count"]) for x in data["recorder_counts"]], [(self.me, 1)])
        # 限制档统计保持**系统单口径**（第六批 §2.2：来源单无录单人无法归属）：
        # 该客户 1 张自己的系统单 + 1 张来源单 ⇒ 订单数 1（全量档会看到 2，见下条）。
        self.assertEqual(data["items"][0]["order_count"], 1)
        self.assertEqual(data["items"][0]["total_amount"], "10.00")

    def test_restricted_scope_cannot_be_widened_by_client_recorder(self):
        """服务端收口：限制档传 recorder=别人 ⇒ 强制 AND 自己，看不到别人的客户。"""
        body = self._restricted().get(URL_MEMBERS, {"recorder": "cm_other_cs"}).json()
        self.assertEqual(body["data"]["items"], [])
        self.assertEqual(body["data"]["total"], 1)          # 可见行数不随筛选变

    def test_source_only_customer_is_invisible_to_restricted(self):
        """来源单没有录单人 ⇒ 来源单-only 客户对限制档不可见（清不清筛选都一样）。"""
        body = self._restricted().get(URL_MEMBERS).json()
        self.assertNotIn("cm-so-eid-source", [r["eid"] for r in body["data"]["items"]])

    def test_member_list_holder_still_sees_everything(self):
        """持 `member_list`（无限制点）⇒ 全量三客户 + 来源单-only 也在列（现状不变）；
        「自己录的客」的统计是合并口径（1 系统 + 1 来源 = 2），与限制档的 1 形成对照。"""
        holder = make_logged_in_client(
            username="cm_full_cs", groups=("客服",), nodes=("customer.member_list",),
        )
        body = holder.get(URL_MEMBERS).json()
        self.assertEqual(
            sorted(r["eid"] for r in body["data"]["items"]),
            ["cm-so-eid-other", "cm-so-eid-own", "cm-so-eid-source"],
        )
        self.assertEqual(body["data"]["total"], 3)
        rows = {r["eid"]: r for r in body["data"]["items"]}
        self.assertEqual(rows["cm-so-eid-own"]["order_count"], 2)
        self.assertEqual(rows["cm-so-eid-own"]["total_amount"], "13.00")

    def test_restricted_filters_recorders_only_self(self):
        body = self._restricted().get(URL_FILTERS).json()
        self.assertEqual(body["data"]["recorders"], [{"value": self.me, "label": self.me}])
        # 来源字典不受限制节点影响（店铺维度与录单人无关）。
        self.assertEqual(body["data"]["sources"], [])

    def test_restricted_drawer_is_scoped_same_as_list(self):
        client = self._restricted()
        own = client.get(URL_MEMBER_ORDERS, {"eid": "cm-so-eid-own"}).json()
        self.assertEqual(own["code"], 0, own)
        # 抽屉范围同列表（只看得到自己的客户）；内容 = 合并列表（来源单 10:00 在
        # 系统单 08:00 前），范围收口 ≠ 内容剔除来源单。
        self.assertEqual(own["data"]["total"], 2)
        self.assertEqual(
            [(r["no"], r["is_system"]) for r in own["data"]["items"]],
            [("CMSO-SRC-OWN", False), ("CMSO0000000000000001", True)],
        )
        # 别人的客户与来源单-only 客户：同一个「客户不存在」（不暴露存在性）。
        for eid in ("cm-so-eid-other", "cm-so-eid-source"):
            res = client.get(URL_MEMBER_ORDERS, {"eid": eid})
            body = res.json()
            self.assertEqual(body["code"], 1000, body)
            self.assertEqual(body["msg"], "客户不存在")

    def test_analysis_is_guarded_but_not_scoped_for_restricted(self):
        """analysis：限制档可进（守卫两节点任一），但聚合**不**按录单人收口（拍板口径：
        分析报表不受限制节点影响）⇒ customer_total 恒全量 3。"""
        body = self._restricted().get(URL_ANALYSIS).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["kpi"]["customer_total"], 3)

    def _extra_own_order(self, order_no, hour):
        """给「自己录的客」再补一张自己录的单（乘积 / 软删用例的追加夹具）。"""
        order = Order.objects.create(
            order_no=order_no, game=self.game, project_text="限制线项目",
            total_price=Decimal("10.00"), customer=self.c_own, created_by=self.me,
        )
        Order.objects.filter(pk=order.pk).update(created_at=_aware(hour))
        return order

    def test_restricted_total_counts_customers_not_orders(self):
        """同客户 2 张自己录的单 + 1 张别人的 ⇒ 行 1 行、total=1（评审 F1：乘积洞探针实证）。

        裸跨关系谓词（`orders__created_by` 编译成 JOIN）会把 total 乘成 2；客户侧
        IN 子查询天然去重。total 与 filtered_total 同判据、必须同值。
        """
        self._extra_own_order("CMSO0000000000000003", hour=11)
        data = self._restricted().get(URL_MEMBERS).json()["data"]
        self.assertEqual([r["eid"] for r in data["items"]], ["cm-so-eid-own"])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["filtered_total"], 1)

    def test_all_own_orders_soft_deleted_leaves_rows_and_drawer(self):
        """自己录的单**全部软删** ⇒ 该客户从行集与抽屉同时消失（评审 F2：JOIN 不走
        OrderManager ⇒ 软删单混入的越界口；子查询形态走默认 manager ⇒ 消失）。"""
        Order.all_objects.filter(order_no="CMSO0000000000000001").update(
            deleted_at=timezone.now()
        )
        client = self._restricted()
        data = client.get(URL_MEMBERS).json()["data"]
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["filtered_total"], 0)
        drawer = client.get(URL_MEMBER_ORDERS, {"eid": "cm-so-eid-own"}).json()
        self.assertEqual(drawer["code"], 1000, drawer)
        self.assertEqual(drawer["msg"], "客户不存在")

    def test_guard_three_tiers(self):
        """守卫：限制档四端点全 200；无节点的普通用户 403；eid 未过校验先于 403 与否不究。"""
        client = self._restricted()
        self.assertEqual(client.get(URL_MEMBERS).status_code, 200)
        self.assertEqual(client.get(URL_FILTERS).status_code, 200)
        self.assertEqual(
            client.get(URL_MEMBER_ORDERS, {"eid": "cm-so-eid-own"}).status_code, 200
        )
        self.assertEqual(client.get(URL_ANALYSIS).status_code, 200)
        # ⛔ 不给 plain 任何组：组会连带 Role，而「客服」组已被上面的限制档夹具挂了节点
        #（Group 是 get_or_create 的，同名组共享同一个 Role ⇒ 节点会漏过去）。
        plain = make_logged_in_client(username="cm_so_plain")
        for url in (URL_MEMBERS, URL_FILTERS, URL_ANALYSIS):
            self.assertEqual(plain.get(url).status_code, 403, url)
        res = plain.get(URL_MEMBER_ORDERS, {"eid": "cm-so-eid-own"})
        self.assertEqual(res.status_code, 403, res.content)


# ═══════════ 回填命令（W2，mock 上游）═══════════════════════════════════

COMMAND_PATH = "apps.business.management.commands.backfill_xgj_customers"


class BackfillStubWiringTests(SimpleTestCase):
    """桩接线自证（E-0029）：命令模块的全局 `xgj_order_detail` 必须就是 `xgj.order_detail`
    —— 桩打在命令模块才是「被查找的那个模块」；不接桩时走的就是真上游函数。"""

    def test_the_command_module_global_is_the_looked_up_name(self):
        from apps.business.management.commands.backfill_xgj_customers import (
            xgj_order_detail as command_order_detail,
        )
        from apps.business import xgj

        self.assertIs(command_order_detail, xgj.order_detail)


class BackfillCommandTests(TenantTestCase):
    @classmethod
    def _send_log(cls, *, order, tb_order_no, hour):
        return TaobaoOrderSendLog.objects.create(
            order_no=order.order_no,
            tb_order_no=tb_order_no,
            result=SendResult.SUCCESS,
            sent_at=_aware(hour),
            operator="系统",
        )

    def setUp(self):
        self.game = Game.objects.create(name="回填游戏")
        self.source = OrderSource.objects.create(name="回填店铺", order_by=1)
        self.binding = XgjShopBinding.objects.create(
            shop_name="回填应用", app_key="cm-app-key", app_secret="cm-app-secret"
        )
        XgjShop.objects.create(
            binding=self.binding, user_identity="cm-shop-identity",
            order_source=self.source,
        )

    def _make_order(self, order_no, *, source=None):
        order = Order.objects.create(
            order_no=order_no, game=self.game, project_text="回填项目",
            total_price=Decimal("10.00"), order_source=source,
        )
        return order

    @contextmanager
    def _stub_upstream(self, details):
        """桩打在**命令模块**（被查找的模块，E-0029）+ 限速 sleep 打桩成 no-op。
        `details` = 逐次调用的返回（side_effect 列表，本文件全部合成值）。"""
        with mock.patch(
            f"{COMMAND_PATH}.xgj_order_detail", side_effect=list(details)
        ) as upstream, mock.patch.object(
            type(self._command()), "_sleep", return_value=None
        ):
            yield upstream

    @staticmethod
    def _command():
        from apps.business.management.commands.backfill_xgj_customers import Command

        return Command()

    def _run(self, **options):
        out = io.StringIO()
        options.setdefault("schema", "test")
        call_command("backfill_xgj_customers", stdout=out, **options)
        return out.getvalue()

    def _detail(self, eid, nick):
        return {"order_no": "TB-合成", "buyer_eid": eid, "buyer_nick": nick}

    def test_backfill_creates_customer_and_links_orders(self):
        order_a = self._make_order("CMBK000000000000001", source=self.source)
        order_b = self._make_order("CMBK000000000000002", source=self.source)
        self._send_log(order=order_a, tb_order_no="TB-合成-1", hour=8)
        self._send_log(order=order_b, tb_order_no="TB-合成-2", hour=9)
        with self._stub_upstream([
            self._detail(EID_A, NICK_A),
            self._detail(EID_A, NICK_A),
        ]) as upstream:
            output = self._run()
        self.assertEqual(upstream.call_count, 2)
        self.assertEqual(Customer.objects.count(), 1)
        customer = Customer.objects.get(eid=EID_A)
        for order in (order_a, order_b):
            order.refresh_from_db()
            self.assertEqual(order.customer_id, customer.pk)
        self.assertIn("连单 2", output)
        self.assertIn("复查", output)
        self.assertIn("Customer 表 1 行", output)

    def test_backfill_rerun_is_a_no_op(self):
        order = self._make_order("CMBK000000000000003", source=self.source)
        self._send_log(order=order, tb_order_no="TB-合成-3", hour=8)
        with self._stub_upstream([self._detail(EID_A, NICK_A)]) as upstream:
            self._run()
            self.assertEqual(upstream.call_count, 1)
            # 续跑：候选为空（customer IS NULL 的单没了）⇒ 上游零调用。
            output = self._run()
            self.assertEqual(upstream.call_count, 1)
        self.assertIn("候选订单 0 个", output)
        order.refresh_from_db()
        self.assertIsNotNone(order.customer_id)

    def test_backfill_counts_upstream_failure_and_continues(self):
        order_a = self._make_order("CMBK000000000000004", source=self.source)
        order_b = self._make_order("CMBK000000000000005", source=self.source)
        # 候选按发货时间倒序 ⇒ 甲（9 点）先处理：第一次上游调用失败，第二次成功。
        self._send_log(order=order_a, tb_order_no="TB-合成-4", hour=9)
        self._send_log(order=order_b, tb_order_no="TB-合成-5", hour=8)
        from apps.business.xgj import XgjError

        with self._stub_upstream([
            XgjError(upstream_code=100001, upstream_msg="上游失败（合成）"),
            self._detail(EID_B, NICK_B),
        ]):
            output = self._run()
        self.assertEqual(Customer.objects.count(), 1)
        order_a.refresh_from_db()
        order_b.refresh_from_db()
        self.assertIsNone(order_a.customer_id)
        self.assertIsNotNone(order_b.customer_id)
        self.assertIn("上游失败 1", output)

    def test_backfill_skips_orders_without_usable_eid(self):
        order = self._make_order("CMBK000000000000006", source=self.source)
        self._send_log(order=order, tb_order_no="TB-合成-6", hour=8)
        with self._stub_upstream([{"order_no": "TB-合成-6", "buyer_nick": "无 eid 的单"}]):
            output = self._run()
        self.assertEqual(Customer.objects.count(), 0)
        order.refresh_from_db()
        self.assertIsNone(order.customer_id)
        self.assertIn("跳过（无 eid）1", output)
        # 身份键超列宽同样跳过（⛔ 不裁不兜，见 customers.upsert_customer 的注释）。
        # ⚠️ 未连单的 006 仍是候选 ⇒ 第二轮它再次被扫（幂等语义），共 2 个跳过。
        order2 = self._make_order("CMBK000000000000007", source=self.source)
        self._send_log(order=order2, tb_order_no="TB-合成-7", hour=9)
        with self._stub_upstream([
            {"order_no": "TB-合成-6", "buyer_nick": "无 eid 的单"},
            self._detail("e" * 65, NICK_A),
        ]):
            output2 = self._run()
        self.assertEqual(Customer.objects.count(), 0)
        self.assertIn("跳过（无 eid）2", output2)

    def test_backfill_counts_already_linked_when_another_writer_won(self):
        """条件 UPDATE 没写进（本轮扫描后别的写入先连了）⇒ 单列「已被连过」桶，不混进别的计数。

        模拟形态：`upsert_customer`（桩打在命令模块的查找点，E-0029）在返回**前**先把这单
        连上客户——等价于并发重跑时另一路先赢了条件 UPDATE。终态：库里恰好一行客户、
        订单连着它（幂等），报告把这一单计成「已被连过」而不是「连单」。
        """
        order = self._make_order("CMBK000000000000010", source=self.source)
        self._send_log(order=order, tb_order_no="TB-合成-10", hour=8)

        def link_first_then_return(eid, buyer_nick=""):
            customer = Customer.objects.get_or_create(eid=eid)[0]
            Order.objects.filter(pk=order.pk).update(customer=customer)
            return customer, False

        with self._stub_upstream([self._detail(EID_A, NICK_A)]):
            with mock.patch(
                f"{COMMAND_PATH}.upsert_customer", side_effect=link_first_then_return
            ):
                output = self._run()
        customer = Customer.objects.get(eid=EID_A)
        order.refresh_from_db()
        self.assertEqual(order.customer_id, customer.pk)
        self.assertIn("已被连过 1", output)
        self.assertIn("连单 0", output)

    def test_backfill_clips_overwide_nick_and_does_not_lose_the_link(self):
        order = self._make_order("CMBK000000000000008", source=self.source)
        self._send_log(order=order, tb_order_no="TB-合成-8", hour=8)
        with self._stub_upstream([self._detail(EID_A, "昵" * 200)]):
            self._run()
        customer = Customer.objects.get(eid=EID_A)
        self.assertEqual(len(customer.buyer_nick), 128)
        order.refresh_from_db()
        self.assertEqual(order.customer_id, customer.pk)

    def test_backfill_rejects_unknown_schema(self):
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            self._run(schema="不存在的租户")

    def test_no_secret_or_full_eid_in_output(self):
        order = self._make_order("CMBK000000000000009", source=self.source)
        self._send_log(order=order, tb_order_no="TB-合成-9", hour=8)
        with self._stub_upstream([self._detail(EID_A, NICK_A)]):
            output = self._run()
        self.assertNotIn("cm-app-secret", output)
        self.assertNotIn(EID_A, output)          # 完整 eid 不打印（掩码）
        self.assertIn("***", output)


# ═══════════ 写入点契约网 ═══════════


class CustomerWritePathNetTests(TenantTestCase):
    """Order.customer 链接、Customer 行与 CustomerSourceOrder 行的**写入点粒度**清单（照
    `test_order_time_fields.py::TimeFieldWritePathNetTests` 的形态）。

    ⚠️ **扫描面如实声明**（⛔ 不是「全仓」）：本网只扫下面三个文件——
    W1 的领域核心（order_intake.py）、共用 upsert（customers.py：W1/W2 的
    `upsert_customer` + 第五批 W3 的 `upsert_source_order`，**唯一写入方 = 收割运维脚本**，
    本体不进仓）、W2 命令（backfill_xgj_customers.py）。`api/customer_views.py` 是只读端点
    不在扫描面（它没有写语句；由上面的行为用例钉住只读）。PATCH / openapi / 同步面
    「不是写入者」由下面的**真闸**逐条断（read_only 白名单 + 对外白名单），不靠扫描。
    """

    SCAN_FILES = (
        "src/apps/business/order_intake.py",
        "src/apps/business/customers.py",
        "src/apps/business/management/commands/backfill_xgj_customers.py",
    )

    #: 登记表：一个写入点一行 = (文件, 作用域名)。多一处 / 少一处 / 挪作用域 ⇒ 红。
    REGISTERED_SITES = {
        ("src/apps/business/order_intake.py", "create_order"),
        ("src/apps/business/management/commands/backfill_xgj_customers.py", "Command::_backfill_one"),
    }

    #: Order.customer 链接与来源单 FK 的落库点（kwarg 形参名恰为 `customer` 的 save /
    #: update 调用），值 = 落库表达式文本（写法变了 ⇒ 红，与「值的形状被换掉」同一条判据）。
    #: 第三行 = W3 `upsert_source_order`（第五批）：CustomerSourceOrder 的唯一写入点。
    REGISTERED_LINK_SITES = {
        ("src/apps/business/order_intake.py", "create_order"): ["customer=customer"],
        ("src/apps/business/management/commands/backfill_xgj_customers.py", "Command::_backfill_one"): ["customer=customer"],
        ("src/apps/business/customers.py", "upsert_source_order"): ["customer=customer"],
    }

    def _measured(self):
        """AST 扫描：`upsert_customer(` 调用点 + 形参名恰为 `customer` 的写型 kwarg
        （save/create/update）+ `.customer =` 赋值。返回 {(文件, 作用域): [表达式文本]}。"""
        repo_root = Path(__file__).resolve().parents[2]
        measured: dict = {}
        for rel in self.SCAN_FILES:
            tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"))

            class Visitor(ast.NodeVisitor):
                def __init__(self):
                    self.stack = []

                def _scope(self):
                    return "::".join(self.stack[-2:]) if self.stack else "<module>"

                def _record(self, expr):
                    measured.setdefault((rel, self._scope()), []).append(expr)

                def visit_ClassDef(self, node):
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                def visit_FunctionDef(self, node):
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else ""
                    )
                    if name == "upsert_customer":
                        self._record("upsert_customer(...)")
                    if name in {"save", "create", "update", "get_or_create", "update_or_create"}:
                        for kw in node.keywords:
                            if kw.arg == "customer":
                                self._record(f"customer={ast.unparse(kw.value)}")
                    self.generic_visit(node)

                def visit_Assign(self, node):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "customer"
                        ):
                            self._record(f"<obj>.customer = {ast.unparse(node.value)}")
                    self.generic_visit(node)

            Visitor().visit(tree)
        return measured

    def test_upsert_call_sites_are_exactly_the_registered_two(self):
        measured = {
            key for key, exprs in self._measured().items()
            if any(expr.startswith("upsert_customer") for expr in exprs)
        }
        self.assertEqual(measured, self.REGISTERED_SITES)

    def test_order_customer_link_sites_are_exactly_the_registered_set(self):
        measured = {}
        for key, exprs in self._measured().items():
            hits = [
                expr for expr in exprs
                if expr.startswith("customer=") or expr.startswith("<obj>.customer")
            ]
            if hits:
                measured[key] = hits
        self.assertEqual(measured, self.REGISTERED_LINK_SITES)

    def test_patch_and_intake_payload_are_not_writers_of_the_fk(self):
        """真闸（⛔ 不是注释声称）：两个写序列化器的 `customer` 必须只读 ⇒
        请求体不是权威来源；W1 的入口键是 `buyer_eid`（表外键），不是 FK 本身。"""
        for serializer in (OrderSerializer, OrderUpdateSerializer):
            with self.subTest(serializer=serializer.__name__):
                fields = serializer().fields
                self.assertIn("customer", fields)
                self.assertTrue(fields["customer"].read_only)
                self.assertNotIn("buyer_eid", fields)
                self.assertNotIn("buyer_nick", fields)

    def test_openapi_whitelist_does_not_accept_buyer_keys(self):
        """对外录单不是写入者（一期如实不连）：`buyer_eid` 不在 CREATE_FIELDS ⇒
        表外键被 1000 拒（行为由 tests/test_openapi_order_create.py 的通用用例钉）。"""
        from apps.openapi.api.order_create_views import CREATE_FIELDS

        self.assertNotIn("buyer_eid", CREATE_FIELDS)
        self.assertNotIn("buyer_nick", CREATE_FIELDS)

    def test_the_fk_is_nullable_with_set_null(self):
        """列形状：可空 + SET_NULL（手工单不连；删档案不带走订单）。"""
        field = Order._meta.get_field("customer")
        self.assertTrue(field.null)
        from django.db.models import SET_NULL

        self.assertEqual(field.remote_field.on_delete, SET_NULL)
