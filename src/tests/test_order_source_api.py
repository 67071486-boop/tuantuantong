"""`order-sources` 端点的契约测试（spec §8 #3/#4/#5/#6 + 计划 Part 4 的硬要求）。

覆盖：

- **#3** CRUD：新增 / 编辑 / 删除 / 列表分页；删除是**真删**。
- **#4** 校验：空名 / 超长名 / `order_by="abc"` / `order_by=-1` / `order_by=1000` /
  `shop_type=999` 一律 **200 + code `1000`**（不是 500）。**POST 与 PATCH 两条写路径都覆盖**
  （T2 评审 Minor 3），并另有一组**上界侧接受**用例（`999` / 10 字名 / 50 字描述；
  T2 评审 Minor 2：只测「越界被拒」时收紧上限不会红）。
- **#5** 分页契约：造 **21** 条 ⇒ 带 `?page_size=200` 拿全 21；不带会被默认 20 截断。
- **#6** 结构兜底：`?page=abc`（**实测 200 + code `1002`**）与 **detail 路由**的畸形参数。
- **Part 4 的硬要求**：`OrderSourceSerializer` 的字段集合**恰好**是字面值那 7 个
  （前端类型 vs 后端序列化器的缝隙——T4 的 12 条 mock 掉传输层，测不出来）。
- **只读时间戳的端到端判据**（T2 评审 Minor 4）：伪造 `created_at` 打真实出口，
  断言它没被写进库（`test_forged_created_at_is_not_writable_end_to_end`）。

⚠️ **两条断言纪律**（`AGENTS.md` §5）：

1. **期望值写字面值，不写自指导出常量**（E-0024）：错误码写 `1000`/`1002`/`0`，
   字段名写 7 个字面字符串，**不写** `ErrorCode.VALIDATION` / `set(serializer.fields)` 的自我循环。
2. `order_by=0` 是**合法默认值**（R5），所以专门有一条「传 0 要落库 0」的边界用例
   （E-0021 的族：真值判据会把「传了 0」当「没传」）。

⚠️ **为什么 detail 腿必须显式写**：`tests/test_no_route_500.py` 的两条腿
**都不覆盖 detail 路由**（registry 腿只枚举集合前缀；explicit 腿显式跳过带转换器的路由，
见 spec §12-S1）。实测不 500（DRF 把畸形主键归一成 `Http404`），但仍要防回归。
"""
from datetime import datetime, timezone
from importlib import import_module

from django_tenants.test.cases import TenantTestCase

from django.apps import apps
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.audit.models import OperationLog
from apps.business.api.serializers import OrderSourceSerializer
from apps.business.models import OrderSource, OrderSourceGroup
from tests.p5_auth_helpers import make_logged_in_client

BASE = "/api/business/order-sources/"

#: 契约的字面值（**不要**改成从序列化器派生——那与被断言的东西是同一个符号，E-0024）。
CONTRACT_FIELDS = [
    "id", "name", "remark", "order_by", "shop_type",
    "group", "group_name", "groups", "created_at", "updated_at",
]


class OrderSourceApiTests(TenantTestCase):
    def setUp(self):
        # P2 的集合端点不声明 `permission_classes` ⇒ 吃 fail-closed 默认权限类；
        # 只认证即可（`performances` 那种按职位放行的端点才需要 superuser）。
        #
        # `OrderSourceViewSet` 的写动作还要节点
        # （create / update / partial_update → `system.add_edit_source`；
        # destroy → `system.delete_source`，见 `ORDER_SOURCE_PERMISSIONS`）⇒ 夹具显式声明。
        # ⚠️ `nodes=` **必须配 `groups=`**（`_grant_nodes` 遍历 `user.groups` 挂节点）；
        # 组名 ≤5 字（`Role.name` 的 `max_length=5`）。
        self.client = make_logged_in_client(
            groups=("来源组",),
            nodes=("system.add_edit_source", "system.delete_source"),
        )

    def _post(self, payload):
        return self.client.post(BASE, payload, content_type="application/json")

    # ── #3 CRUD ───────────────────────────────────────────────────────────
    def test_create_returns_envelope_and_persists(self):
        r = self._post({"name": "闲鱼A", "remark": "描述", "order_by": 0, "shop_type": 20})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["msg"], "")
        self.assertEqual(body["data"]["name"], "闲鱼A")
        self.assertEqual(body["data"]["remark"], "描述")
        self.assertEqual(body["data"]["order_by"], 0)
        self.assertEqual(body["data"]["shop_type"], 20)
        row = OrderSource.objects.get(name="闲鱼A")
        self.assertEqual(row.shop_type, 20)
        self.assertEqual(row.remark, "描述")
        self.assertEqual(row.order_by, 0)

    def test_create_with_omitted_optionals_uses_model_defaults(self):
        """只传 `name` ⇒ 落库 `order_by=0` / `shop_type=0` / `remark=""`（R5 + spec §3）。"""
        r = self._post({"name": "只有名字"})
        self.assertEqual(r.json()["code"], 0)
        row = OrderSource.objects.get(name="只有名字")
        self.assertEqual(row.order_by, 0)
        self.assertEqual(row.shop_type, 0)
        self.assertEqual(row.remark, "")

    def test_list_is_ordered_by_backend(self):
        OrderSource.objects.create(name="甲")
        OrderSource.objects.create(name="乙", order_by=5)
        OrderSource.objects.create(name="丙")
        body = self.client.get(BASE).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual([i["name"] for i in body["data"]["items"]], ["乙", "甲", "丙"])
        self.assertEqual(body["data"]["total"], 3)

    def test_duplicate_name_is_1000_not_500(self):
        OrderSource.objects.create(name="甲")
        r = self._post({"name": "甲"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1000)
        self.assertEqual(OrderSource.objects.filter(name="甲").count(), 1)

    def test_patch_updates_name(self):
        row = OrderSource.objects.create(name="甲")
        r = self.client.patch(
            f"{BASE}{row.id}/", {"name": "乙"}, content_type="application/json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        self.assertEqual(r.json()["data"]["name"], "乙")
        row.refresh_from_db()
        self.assertEqual(row.name, "乙")

    def test_patch_order_by_to_zero_is_persisted(self):
        """⚠️ E-0021 的族：`0` 是有意义的值，不能被真值判据当成「没传」。"""
        row = OrderSource.objects.create(name="甲", order_by=7)
        r = self.client.patch(
            f"{BASE}{row.id}/", {"order_by": 0}, content_type="application/json"
        )
        self.assertEqual(r.json()["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.order_by, 0)

    def test_patch_duplicate_name_is_1000(self):
        OrderSource.objects.create(name="甲")
        other = OrderSource.objects.create(name="乙")
        r = self.client.patch(
            f"{BASE}{other.id}/", {"name": "甲"}, content_type="application/json"
        )
        self.assertEqual(r.json()["code"], 1000)
        other.refresh_from_db()
        self.assertEqual(other.name, "乙")

    def test_patch_keeping_own_name_is_ok(self):
        """DRF 的单字段唯一校验器在 PATCH 时要排除自身实例（否则「只改描述」都会 1000）。"""
        row = OrderSource.objects.create(name="甲")
        r = self.client.patch(
            f"{BASE}{row.id}/",
            {"name": "甲", "remark": "只改描述"},
            content_type="application/json",
        )
        self.assertEqual(r.json()["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.remark, "只改描述")

    def test_forged_created_at_is_not_writable_end_to_end(self):
        """伪造 `created_at` 打**真实出口**：POST 与 PATCH 都不许把它写进去。

        与 `OrderSourceSerializerContractTests.test_read_only_timestamps_are_read_only`
        （断的是序列化器属性 = 中间层）互补，这里判据落在响应值与**库里的值**上（E-0009）。

        ⚠️ **判别力如实登记：只有 PATCH 那条腿、且只有「库里的值」那两条断言能判别**。
        `created_at` 是 `auto_now_add=True` ⇒ Django `pre_save(add=True)` 在**新建**时无条件
        覆盖成 `now()`，POST 腿即使把序列化器的只读拿掉也照样绿（模型层兜底）；而**更新**时
        `add=False`、`auto_now` 也为假 ⇒ 那层保护不存在，序列化器只要接受该键就会被写库。
        另外响应里的 `created_at` 会按 `TIME_ZONE` 做时区换算，与传入的 `…Z` 字面量
        天然不等 ⇒ 单看响应字符串**没有判别力**（T2 修复的变异实测：可写时响应断言仍绿、
        `row.created_at` 断言红）。这也正是 E-0009 的口径：判据打在真实出口上。
        """
        forged = datetime(2000, 1, 1, tzinfo=timezone.utc)
        forged_iso = forged.isoformat().replace("+00:00", "Z")

        created = self._post({"name": "伪造时间", "created_at": forged_iso})
        self.assertEqual(created.json()["code"], 0)
        self.assertNotEqual(created.json()["data"]["created_at"], forged_iso)
        row = OrderSource.objects.get(name="伪造时间")
        self.assertNotEqual(row.created_at, forged)

        original = row.created_at
        patched = self.client.patch(
            f"{BASE}{row.id}/",
            {"created_at": forged_iso},
            content_type="application/json",
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["code"], 0)
        self.assertNotEqual(patched.json()["data"]["created_at"], forged_iso)
        row.refresh_from_db()
        self.assertEqual(row.created_at, original)
        self.assertNotEqual(row.created_at, forged)

    def test_delete_is_hard_delete(self):
        row = OrderSource.objects.create(name="甲")
        r = self.client.delete(f"{BASE}{row.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        self.assertFalse(OrderSource.objects.filter(id=row.id).exists())

    # ── #4 校验一律 1000（不是 500）────────────────────────────────────────
    def test_validation_cases_are_1000(self):
        """spec §8 #4 的**六**类输入（含 `-1` / `1000` 两个范围边界）。

        ⚠️ 「超长名」这一支不是锦上添花：`varchar(10)` 超长在库层是 `DataError`
        ⇒ 不校验就是 HTML 500（实测，spec §12）。
        """
        for payload in (
            {"name": ""},
            {"name": " "},
            {},
            {"name": "x" * 11},
            {"name": "a", "order_by": "abc"},
            {"name": "a", "order_by": -1},
            {"name": "a", "order_by": 1000},
            {"name": "a", "shop_type": 999},
            {"name": "a", "remark": "y" * 51},
        ):
            with self.subTest(payload=payload):
                r = self._post(payload)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["code"], 1000)
        self.assertEqual(OrderSource.objects.count(), 0)

    def test_validation_cases_are_1000_on_patch(self):
        """同一批判据必须在 **PATCH** 上也成立（T2 评审 Minor 3：原先只有 POST 版）。

        ⚠️ 写路径有两条视图方法（`create` / `partial_update`）——只测 POST 时，
        「PATCH 路径不再触发字段校验」**不会红**。这里钉 `order_by` / `shop_type` / 超长名，
        并断言原行**没有被写坏**（E-0009：判据打在库里的值上）。
        """
        row = OrderSource.objects.create(
            name="原样", remark="原描述", order_by=3, shop_type=10
        )
        for payload in (
            {"order_by": -1},
            {"order_by": 1000},
            {"shop_type": 999},
            {"name": "x" * 11},
        ):
            with self.subTest(payload=payload):
                r = self.client.patch(
                    f"{BASE}{row.id}/", payload, content_type="application/json"
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["code"], 1000)
        row.refresh_from_db()
        self.assertEqual(row.name, "原样")
        self.assertEqual(row.remark, "原描述")
        self.assertEqual(row.order_by, 3)
        self.assertEqual(row.shop_type, 10)

    def test_shop_type_boundary_values_are_accepted(self):
        """`shop_type` 的 9 个合法值一个都不许被误拒（判据是 choices，不是范围）。"""
        for value in (0, 10, 15, 20, 30, 40, 50, 60, 70):
            with self.subTest(shop_type=value):
                r = self._post({"name": f"来源{value}", "shop_type": value})
                self.assertEqual(r.json()["code"], 0)
                self.assertEqual(r.json()["data"]["shop_type"], value)

    def test_upper_bound_values_are_accepted(self):
        """上界侧的**接受**判据：`order_by=999` / `name` 10 字 / `remark` 50 字都落 0。

        ⚠️ 这是 `test_validation_cases_are_1000` 的**另一半**：只测「越界被拒」而不测
        「恰好在界内被接受」时，把序列化器的 `order_by.max_value` 从 999 改成 998、
        或把 `name` 的上限收紧到 9，整套测试**仍然是绿的**（T2 评审实测）。
        每条同时钉响应值与库里的值（E-0009：判据打在真实出口上）。
        """
        for payload, field, expected in (
            ({"name": "上界排序", "order_by": 999}, "order_by", 999),
            ({"name": "x" * 10}, "name", "x" * 10),
            ({"name": "上界描述", "remark": "y" * 50}, "remark", "y" * 50),
        ):
            with self.subTest(field=field):
                r = self._post(payload)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["code"], 0)
                self.assertEqual(r.json()["data"][field], expected)
                row = OrderSource.objects.get(name=payload["name"])
                self.assertEqual(getattr(row, field), expected)

    # ── #5 分页契约─────────────────────────────
    def test_page_size_200_returns_all_21(self):
        for i in range(21):
            OrderSource.objects.create(name=f"来源{i:02d}")
        with_param = self.client.get(BASE, {"page": 1, "page_size": 200}).json()
        self.assertEqual(with_param["code"], 0)
        self.assertEqual(len(with_param["data"]["items"]), 21)
        self.assertEqual(with_param["data"]["total"], 21)
        self.assertEqual(with_param["data"]["page"], 1)
        self.assertEqual(with_param["data"]["page_size"], 200)
        # 反向对照：不带 `page_size` 就是默认 20 —— 静默截断是真的会发生。
        without = self.client.get(BASE).json()
        self.assertEqual(len(without["data"]["items"]), 20)
        self.assertEqual(without["data"]["total"], 21)
        self.assertEqual(without["data"]["page_size"], 20)

    def test_page_size_300_is_honored_and_500_is_capped_to_300(self):
        """⭐ 每页上限 300（业务方 2026-11-07 拍板 200→300，E-0323 同日）。

        造 **301** 条 ⇒ `page_size=300` 必须真拿到 **300** 条（旧上限 200 下这条红：
        DRF `_positive_int` 会把 300 **静默截**成 200）；`page_size=500` 超上限
        ⇒ 拿到 300（截断而非报错， echoes 的 `page_size` 也是生效值 300）。
        """
        OrderSource.objects.bulk_create(
            [OrderSource(name=f"来源{i:03d}") for i in range(301)]
        )
        got_300 = self.client.get(BASE, {"page": 1, "page_size": 300}).json()
        self.assertEqual(got_300["code"], 0)
        self.assertEqual(len(got_300["data"]["items"]), 300)
        self.assertEqual(got_300["data"]["page_size"], 300)
        capped = self.client.get(BASE, {"page": 1, "page_size": 500}).json()
        self.assertEqual(capped["code"], 0)
        self.assertEqual(len(capped["data"]["items"]), 300)
        self.assertEqual(capped["data"]["page_size"], 300)

    # ── #6 结构兜底 ──────────────────────────────────────────────────────
    def test_malformed_collection_params_do_not_500(self):
        """`?page=abc` **实测 200 + code `1002`**（不是 `1000`，spec §12-S6）。"""
        r = self.client.get(BASE, {"page": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1002)
        # `page_size` 非法时 DRF 的 `_positive_int` 吞掉异常、回落到默认 20（实测）。
        fallback = self.client.get(BASE, {"page_size": "abc"})
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.json()["code"], 0)

    def test_malformed_detail_routes_do_not_500(self):
        """detail 路由的畸形参数（spec §12-S1：`test_no_route_500` 的两条腿都不覆盖它）。

        两条腿都断言 `200 + code 1002`（`NotFound` ⇒ 业务码 `1002`，`core/exceptions.py`）——
        `abc` 走不通 `<int:pk>` 转换器 ⇒ Django 路由 404 ⇒ 同一条业务分支。
        """
        for path in (f"{BASE}abc/", f"{BASE}999999/"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["code"], 1002)
        for method in ("patch", "delete"):
            with self.subTest(method=method):
                r = getattr(self.client, method)(
                    f"{BASE}999999/", {}, content_type="application/json"
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["code"], 1002)

    def test_unknown_query_params_are_ignored(self):
        """来源集合没有父级过滤 ⇒ 未知查询参数被忽略，不能 500（与 `GameViewSet` 同形）。"""
        OrderSource.objects.create(name="甲")
        r = self.client.get(BASE, {"game": "abc", "status": "BOGUS", "keyword": "x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        self.assertEqual(len(r.json()["data"]["items"]), 1)


class OrderSourceSerializerContractTests(TenantTestCase):
    """计划 Part 4 的硬要求：字段集合**恰好**是那 9 个**字面值**（2026-10-26 起含分组两列）。

    ⚠️ 这条测试的判别力来自「**不**从序列化器派生期望值」：写成
    `set(OrderSourceSerializer().fields)` 就是自指（改了也不红，E-0024）。
    写死字面列表后：加字段 / 删字段 / 改字段名（例如把 `order_by` 写成 `sort`）都会红。
    """

    def test_field_set_is_exactly_the_nine_literal_names(self):
        fields = OrderSourceSerializer().fields
        self.assertEqual(
            [name for name, field in fields.items() if not field.write_only],
            CONTRACT_FIELDS,
        )
        self.assertTrue(fields["group_ids"].write_only)

    def test_read_only_timestamps_are_read_only(self):
        """时间戳只读：传了也不能被客户端改写（`read_only_fields` 与契约同处）。"""
        fields = OrderSourceSerializer().fields
        for name in ("created_at", "updated_at"):
            with self.subTest(field=name):
                self.assertTrue(fields[name].read_only)
        for name in ("name", "remark", "order_by", "shop_type", "group"):
            with self.subTest(field=name):
                self.assertFalse(fields[name].read_only)


# ══════════════════════════════════════════════════════════════════════
# 公司店铺分组（计划 `docs/plans/2026-10-26-order-source-groups.md` §3.2）
# ══════════════════════════════════════════════════════════════════════
GROUPS = "/api/business/order-source-groups/"

#: 分组行的**恰好**键集（计划 §6 ⑦：多键少键都红；⛔ 不从序列化器派生，E-0024）。
GROUP_ROW_FIELDS = ["id", "name", "order_by", "source_count", "created_at", "updated_at"]


class OrderSourceGroupApiTests(TenantTestCase):
    """分组 CRUD / 归组 / 行形状 / 审计（计划 §6 批次 1a 的前半）。

    ⚠️ 权限两个方向**不在本类**：它们挂在 `test_rbac_endpoint_enforcement.py` 的
    `ConfigWriteEndpointNodeGateTests`（拒绝侧要挂**同域另一条**节点做对照，
    那是那个文件的既有形状）。本类的 client 同时持两条节点，只测行为与载荷。
    """

    def setUp(self):
        self.client = make_logged_in_client(
            username="grp-api",
            groups=("分组组",),
            nodes=("system.add_edit_source", "system.delete_source"),
        )

    def _post(self, payload):
        return self.client.post(GROUPS, payload, content_type="application/json")

    # ── CRUD ────────────────────────────────────────────────────────────
    def test_create_returns_the_exact_row_shape_and_persists(self):
        r = self._post({"name": "店铺一组", "order_by": 5})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["code"], 0)
        # ⚠️ 写响应**不带** `source_count`：它是**列表注解**（`annotate(Count("sources"))`），
        #    单行实例上没有这个属性 ⇒ DRF 的 read_only 字段直接 SkipField（不是 0、也不是 null）。
        #    页面在增删改后一律**重取列表**（计划 §3.3 第 5 条），所以这不影响消费方；
        #    `source_count` 的恰好键集判据打在**列表**那一跳（见下一条用例）。
        self.assertEqual(
            list(body["data"].keys()),
            ["id", "name", "order_by", "created_at", "updated_at"],
        )
        self.assertEqual(body["data"]["name"], "店铺一组")
        self.assertEqual(body["data"]["order_by"], 5)
        row = OrderSourceGroup.objects.get(name="店铺一组")
        self.assertEqual(row.order_by, 5)

    def test_list_is_ordered_by_backend_and_row_shape_is_exact(self):
        OrderSourceGroup.objects.create(name="甲组")
        OrderSourceGroup.objects.create(name="乙组", order_by=9)
        OrderSourceGroup.objects.create(name="丙组")
        body = self.client.get(GROUPS).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual([i["name"] for i in body["data"]["items"]], ["乙组", "甲组", "丙组"])
        for item in body["data"]["items"]:
            self.assertEqual(list(item.keys()), GROUP_ROW_FIELDS)

    def test_duplicate_name_is_1000_not_500(self):
        OrderSourceGroup.objects.create(name="同名组")
        r = self._post({"name": "同名组"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1000)
        self.assertEqual(OrderSourceGroup.objects.filter(name="同名组").count(), 1)

    def test_name_length_boundary(self):
        """20 字接受 / 21 字回 1000（`varchar(20)` 超长在库层是 `DataError`，E-0010 同族）。"""
        accepted = self._post({"name": "x" * 20})
        self.assertEqual(accepted.json()["code"], 0)
        self.assertEqual(accepted.json()["data"]["name"], "x" * 20)
        rejected = self._post({"name": "y" * 21})
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["code"], 1000)
        self.assertEqual(OrderSourceGroup.objects.count(), 1)

    def test_patch_renames_and_keeps_own_name_ok(self):
        group = OrderSourceGroup.objects.create(name="旧名")
        renamed = self.client.patch(
            f"{GROUPS}{group.id}/", {"name": "新名"}, content_type="application/json"
        )
        self.assertEqual(renamed.json()["code"], 0)
        self.assertEqual(renamed.json()["data"]["name"], "新名")
        # 只改排序、名字原样重发 ⇒ 不许被自己的唯一约束拦（DRF 排除自身）。
        same = self.client.patch(
            f"{GROUPS}{group.id}/", {"name": "新名", "order_by": 3},
            content_type="application/json",
        )
        self.assertEqual(same.json()["code"], 0)
        group.refresh_from_db()
        self.assertEqual(group.name, "新名")
        self.assertEqual(group.order_by, 3)

    def test_delete_turns_sources_ungrouped_but_keeps_them(self):
        """拍板②：删分组**不删来源**，只把它们变成未分组（`SET_NULL`）。"""
        group = OrderSourceGroup.objects.create(name="待删组")
        source = OrderSource.objects.create(name="来源甲", group=group)
        r = self.client.delete(f"{GROUPS}{group.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        self.assertFalse(OrderSourceGroup.objects.filter(id=group.id).exists())
        source.refresh_from_db()
        self.assertIsNone(source.group_id, "删分组不许带走来源（SET_NULL）")
        # 来源还在、还读得出来，且读面显示「未分组」（空串）。
        body = self.client.get(BASE).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual([i["name"] for i in body["data"]["items"]], ["来源甲"])
        self.assertEqual(body["data"]["items"][0]["group_name"], "")

    # ── source_count（删除确认文案的数据来源）───────────────────────────
    def test_source_count_is_annotated_once_per_group(self):
        """`annotate(Count("sources"))`：每个组**只数自己的**来源，且一次聚合给出。"""
        a = OrderSourceGroup.objects.create(name="甲组")
        b = OrderSourceGroup.objects.create(name="乙组")
        OrderSource.objects.create(name="A1", group=a)
        OrderSource.objects.create(name="A2", group=a)
        OrderSource.objects.create(name="B1", group=b)
        OrderSource.objects.create(name="未分组来源")
        body = self.client.get(GROUPS).json()
        counts = {i["name"]: i["source_count"] for i in body["data"]["items"]}
        self.assertEqual(counts, {"甲组": 2, "乙组": 1})

    # ── 归组（既有 PATCH /order-sources/<pk>/）──────────────────────────
    def test_assign_group_via_order_source_patch(self):
        group = OrderSourceGroup.objects.create(name="甲组")
        source = OrderSource.objects.create(name="来源甲")
        r = self.client.patch(
            f"{BASE}{source.id}/", {"group": group.id}, content_type="application/json"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        self.assertEqual(r.json()["data"]["group"], group.id)
        self.assertEqual(r.json()["data"]["group_name"], "甲组")
        source.refresh_from_db()
        self.assertEqual(source.group_id, group.id)

    def test_unassign_group_with_null(self):
        group = OrderSourceGroup.objects.create(name="甲组")
        source = OrderSource.objects.create(name="来源甲", group=group)
        r = self.client.patch(
            f"{BASE}{source.id}/", {"group": None}, content_type="application/json"
        )
        self.assertEqual(r.json()["code"], 0)
        self.assertIsNone(r.json()["data"]["group"])
        self.assertEqual(r.json()["data"]["group_name"], "")
        source.refresh_from_db()
        self.assertIsNone(source.group_id)

    def test_group_zero_and_unknown_id_are_1000(self):
        source = OrderSource.objects.create(name="来源甲")
        for value in (0, -1, 999999):
            with self.subTest(group=value):
                r = self.client.patch(
                    f"{BASE}{source.id}/", {"group": value}, content_type="application/json"
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["code"], 1000)
        source.refresh_from_db()
        self.assertIsNone(source.group_id)

    def test_new_group_adds_existing_source_without_replacing_existing_group(self):
        original = OrderSourceGroup.objects.create(name="原分组")
        source = OrderSource.objects.create(name="来源甲", group=original)
        response = self._post({"name": "新增组", "source_ids": [source.pk, source.pk]}).json()
        self.assertEqual(response["code"], 0)
        new_id = response["data"]["id"]
        source.refresh_from_db()
        self.assertEqual(source.group_id, original.pk)
        listed = self.client.get(BASE).json()["data"]["items"][0]
        self.assertEqual(listed["groups"], [
            {"id": original.pk, "name": "原分组"},
            {"id": new_id, "name": "新增组"},
        ])
        counts = self.client.get(GROUPS).json()["data"]["items"]
        self.assertEqual([row["source_count"] for row in counts], [1, 1])

    def test_create_group_rejects_missing_source_without_creating_group(self):
        source = OrderSource.objects.create(name="来源甲")
        body = self._post({"name": "新增组", "source_ids": [source.pk, source.pk + 1]}).json()
        self.assertEqual(body["code"], 1000)
        self.assertFalse(OrderSourceGroup.objects.filter(name="新增组").exists())
        self.assertFalse(source.groups.exists())
        self.assertFalse(OperationLog.objects.filter(action="system.order_source_group_create").exists())

    def test_editing_memberships_logs_only_changes_and_legacy_keeps_other_groups(self):
        first = OrderSourceGroup.objects.create(name="甲组")
        second = OrderSourceGroup.objects.create(name="乙组")
        third = OrderSourceGroup.objects.create(name="丙组")
        source = OrderSource.objects.create(name="来源甲", group=first)
        url = f"{BASE}{source.pk}/"

        def patch(payload):
            body = self.client.patch(url, payload, content_type="application/json").json()
            self.assertEqual(body["code"], 0, body)
            return body["data"]

        updated = patch({"group_ids": [first.pk, second.pk]})
        self.assertEqual([row["id"] for row in updated["groups"]], [first.pk, second.pk])
        patch({"group_ids": [second.pk, first.pk]})
        logs = OperationLog.objects.filter(action="system.order_source_update")
        self.assertEqual(logs.count(), 1)

        counts = self.client.get(GROUPS).json()["data"]["items"]
        self.assertEqual([row["source_count"] for row in counts], [1, 1, 0])

        legacy = patch({"group": third.pk})
        self.assertEqual(legacy["group"], third.pk)
        self.assertEqual([row["id"] for row in legacy["groups"]], [second.pk, third.pk])

        self.assertEqual(self.client.delete(f"{GROUPS}{third.pk}/").json()["code"], 0)
        listed = self.client.get(url).json()["data"]
        self.assertEqual([row["id"] for row in listed["groups"]], [second.pk])

        cleared = patch({"group_ids": []})
        self.assertIsNone(cleared["group"])
        self.assertEqual(cleared["groups"], [])

    def test_stale_legacy_form_does_not_remove_memberships_after_primary_group_is_deleted(self):
        first = OrderSourceGroup.objects.create(name="甲组")
        second = OrderSourceGroup.objects.create(name="乙组")
        third = OrderSourceGroup.objects.create(name="丙组")
        source = OrderSource.objects.create(name="来源甲", group=first)
        source.groups.add(second, third)
        url = f"{BASE}{source.pk}/"

        first.delete()
        row = self.client.get(url).json()["data"]
        self.assertEqual(row["group"], second.pk)
        self.assertEqual(row["group_name"], "乙组")

        saved = self.client.patch(
            url, {"group": None, "remark": "旧页面改备注"}, content_type="application/json"
        ).json()
        self.assertEqual(saved["code"], 0)
        self.assertEqual(saved["data"]["group"], second.pk)
        self.assertEqual(set(source.groups.values_list("pk", flat=True)), {second.pk, third.pk})

    def test_backfill_preserves_existing_group_and_skips_ungrouped_sources(self):
        group = OrderSourceGroup.objects.create(name="旧分组")
        grouped = OrderSource.objects.create(name="已有来源", group=group)
        ungrouped = OrderSource.objects.create(name="其他来源")
        migration = import_module("apps.business.migrations.0140_order_source_groups_memberships")

        with connection.schema_editor() as schema_editor:
            migration.copy_existing_memberships(apps, schema_editor)

        self.assertEqual(list(grouped.groups.values_list("pk", flat=True)), [group.pk])
        self.assertFalse(ungrouped.groups.exists())

    def test_list_does_not_n_plus_one_on_group(self):
        """`select_related("group")` 的判据：行数翻倍时查询条数**不变**。

        ⚠️ 只断「SQL 里有 join」是不够的（`values()` 也能拼出 join）；这里打真实列表出口，
        比较 1 行与 5 行两种规模下的查询条数。
        """
        for index in range(5):
            group = OrderSourceGroup.objects.create(name=f"组{index}")
            OrderSource.objects.create(name=f"来源{index}", group=group)
        # 预热：首个请求会多出「一次性的」元数据 / 权限查询，跨规模比较前先跑一次。
        self.client.get(BASE)
        with CaptureQueriesContext(connection) as few:
            self.client.get(BASE)
        for index in range(5, 10):
            group = OrderSourceGroup.objects.create(name=f"组{index}")
            OrderSource.objects.create(name=f"来源{index}", group=group)
        with CaptureQueriesContext(connection) as many:
            self.client.get(BASE)
        self.assertEqual(len(few), len(many), "来源列表对 group 有 N+1")

    # ── 审计（计划 §3.2 / §6 ⑧）────────────────────────────────────────
    def test_audit_target_kind_is_not_truncated_and_sentences_are_ours(self):
        created = self._post({"name": "甲组"})
        self.assertEqual(created.json()["code"], 0)
        group = OrderSourceGroup.objects.get(name="甲组")
        row = OperationLog.objects.get(action="system.order_source_group_create")
        self.assertEqual(row.target_kind, "source_group")
        self.assertLessEqual(len(row.target_kind), 16, "target_kind 超列宽会被静默截断")
        self.assertEqual(row.target_id, str(group.pk))
        self.assertEqual(row.target_name, "甲组")
        self.assertEqual(row.summary, "【grp-api】新增公司店铺分组：甲组")
        self.assertEqual(row.log_type, 40)
        # 删除句只渲染名字。
        deleted = self.client.delete(f"{GROUPS}{group.id}/")
        self.assertEqual(deleted.json()["code"], 0)
        destroy = OperationLog.objects.get(action="system.order_source_group_destroy")
        self.assertEqual(destroy.target_kind, "source_group")
        self.assertEqual(destroy.target_name, "甲组")
        self.assertEqual(destroy.summary, "【grp-api】删除公司店铺分组：甲组")

    def test_rename_audit_records_the_old_name_in_from_value(self):
        """计划 §6 ⑧：改名时 `OperationLog.from_value` = **旧名**（落库断言，E-0009）。"""
        group = OrderSourceGroup.objects.create(name="旧名")
        r = self.client.patch(
            f"{GROUPS}{group.id}/", {"name": "新名"}, content_type="application/json"
        )
        self.assertEqual(r.json()["code"], 0)
        row = OperationLog.objects.get(action="system.order_source_group_update")
        self.assertEqual(row.from_value, "旧名")
        self.assertEqual(row.target_name, "新名")
        self.assertEqual(row.summary, "【grp-api】编辑公司店铺分组：新名，原名称：旧名")
        self.assertEqual(row.log_type, 40)

    def test_noop_patch_writes_no_row(self):
        """纯 no-op 更新零行（`ConfigAuditMixin` 的既有判据，分组沿用）。"""
        group = OrderSourceGroup.objects.create(name="甲组", order_by=2)
        r = self.client.patch(
            f"{GROUPS}{group.id}/", {"name": "甲组", "order_by": 2},
            content_type="application/json",
        )
        self.assertEqual(r.json()["code"], 0)
        self.assertFalse(OperationLog.objects.filter(action="system.order_source_group_update").exists())

    # ── 结构兜底 ────────────────────────────────────────────────────────
    def test_malformed_params_do_not_500(self):
        OrderSourceGroup.objects.create(name="甲组")
        r = self.client.get(GROUPS, {"page": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1002)
        fallback = self.client.get(GROUPS, {"page_size": "abc"})
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.json()["code"], 0)
