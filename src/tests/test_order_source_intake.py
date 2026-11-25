"""订单接入来源字典：**create 与 PATCH 两条路径**的写入语义 + 删除语义 + 跨租户（spec §8 #8/#9/#10）。

    本文件覆盖（spec §5.1 的四条规则、§8 #8/#9/#10）：

    | 请求里传了什么 | 期望 |
    |---|---|
    | 只传 `order_source`（非空） | 写 FK，且由**后端**把字典项 `name` 覆盖写进 `source` |
    | 两个都传 | **以 `order_source` 为准**（`source` 被覆盖） |
    | 只传 `source`（自由文本） | 写名字，并把 `order_source` 置为 `NULL`（不变式 D7） |
    | 传 `order_source=null` | FK 置空；`source` **不动**（快照语义） |
    | `order_source` 指向不存在的 id | 200 + code `1000`（**不是** 500） |

    ⚠️ **为什么 create 与 PATCH 必须各有一条**（评审 Critical 2）：两条路径走的是**两个序列化器**
    （`OrderSerializer` / `OrderUpdateSerializer`）与两个视图方法；只测 create 时
    「PATCH 忘了调 helper」不会红。**计划 Part 4 的 T3-M1 实测（本文件复核过）**：
    去掉 PATCH 的 helper 调用 ⇒ **3 条红**（`test_patch_order_source_updates_name` /
    `test_patch_freetext_source_nulls_fk` / `test_patch_to_another_source_follows_the_new_name`）；
    去掉 create 的 ⇒ **2 条红**（create 的 only_order_source / both 两条）。
    ⚠️ 本条原写「恰好 **2** 条红」——**被 T3 独立评审实测证伪**（实际 3 条），按 AGENTS.md §5 改成实测值。

    ⚠️ **判别力如实登记**：「只传自由文本 ⇒ FK 置 NULL」这条**只有 PATCH 能判别**——
    create 侧新建行的 FK 本来就是 `NULL`（默认值），所以 create 那条是**防回归**、不是判别点
    （计划 Part 4 的 T3-M2 实测：只有 PATCH 那条红）。**不要**把两边写成等价。

    ⚠️ **断言纪律**（`AGENTS.md` §5）：

    1. **端到端落库断言**：每条都从数据库重新取行（`Order.objects.get(...)` / `refresh_from_db()`），
    断言 `order_source_id` 与 `source` 两个值——**不**断言请求体或响应体里的 `order_source`。
    本线之前 `order_source` 只是请求体里的死键（DRF 忽略未知键），只看响应体会**全绿而库里什么都没有**。
    2. **期望值写字面值**（E-0024）：错误码写 `1000`/`0`，名字写字典里的字面值。
    3. **跨租户判据不用主键指认对方那一行**（`AGENTS.md` §5）：两个 schema 的主键序列各自从 1 开始，
    本文件用**两租户取值不同**的来源名与「B 侧该名字查无此行」作判据。
"""
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.models import Game, Order, OrderSource
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import P2_TEST_USERNAME, grant_games, make_logged_in_client

ORDERS = "/api/business/orders/"
SOURCES = "/api/business/order-sources/"

#: 租户 A = `TenantTestCase` 的默认租户（schema `test`）。
DOMAIN_A = "tenant.test.com"
#: 租户 B 的域名：刻意与 A 不同（`Domain` 上有唯一约束）。
DOMAIN_B = "order-src-intake-b.test.local"
SCHEMA_B = "order_src_intake_b"


class _OrderIntakeMixin:
    """两条路径共用的夹具与断言（`TenantTestCase` 的子类里混入）。"""

    def _make_order(self, *, order_no, **kwargs):
        """直接造一行订单（PATCH 用例的起点）。

    ⚠️ `project_text` 必填是**实测**踩到的：PATCH 会重跑 `validate_required`
    （`data.get("project_text", order.project_text)`），空值会让**与来源无关**的
    PATCH 也回 `1000`（spec §12 末段）。
"""
        return Order.objects.create(
            game=self.game,
            order_no=order_no,
            project_text="深渊满星",
            **kwargs,
        )

    def _reload(self, order_no):
        """从数据库重新取行——落库判据的唯一出口。"""
        return Order.objects.get(order_no=order_no)


class OrderIntakeCreateTests(_OrderIntakeMixin, TenantTestCase):
    """spec §8 #8：录单（create）接线。"""

    def setUp(self):
        # ⚠️ **本线的门**：录单（POST）与编辑（PATCH）都要节点
        # （`ORDER_PERMISSIONS["create"] = ("order.add_edit",)`；
        # `["partial_update"] = ("order.add_edit", "order.submit_edit")` —— 任一命中即放行）。
        # 夹具必须**显式声明**它需要的判据——这不是放宽断言，而是这批用例原先只靠
        # 「登录」就够、本线之后不够了。
        # 组名用「录入组」：它名下没有预置 Role、节点全靠 `nodes=` 显式声明 ⇒ 不会顺带
        # 改变 `allowed_transitions` 之类的行为（本文件也不断言那些）。
        # ⚠️ `Role.name` 是 `max_length=5` ⇒ 组名必须 ≤5 字。
        self.client = make_logged_in_client(
            groups=("录入组",), nodes=("order.add_edit", "order.submit_edit")
        )
        self.game = Game.objects.create(name="原神")
        self.source = OrderSource.objects.create(name="闲鱼A")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——录单写侧要 `assert_game_allowed(user, self.game)`。
        grant_games(User.objects.get(username=P2_TEST_USERNAME), self.game)

    def _post(self, payload):
        body = {"game": self.game.id, "project_text": "深渊满星", **payload}
        return self.client.post(ORDERS, body, content_type="application/json")

    def test_create_with_order_source_writes_fk_and_name(self):
        """前端形态 `{order_source: 20}`：只传 id，名字由后端回填。"""
        r = self._post({"order_source": self.source.id})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(r.json()["data"]["order_no"])
        self.assertEqual(order.order_source_id, self.source.id)
        self.assertEqual(order.source, "闲鱼A")

    def test_create_with_both_prefers_order_source(self):
        """两个都传 ⇒ 以 `order_source` 为准（`source` 被忽略）。"""
        r = self._post({"order_source": self.source.id, "source": "随手写的"})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(r.json()["data"]["order_no"])
        self.assertEqual(order.order_source_id, self.source.id)
        self.assertEqual(order.source, "闲鱼A")

    def test_create_with_freetext_source_keeps_fk_null(self):
        """只传自由文本 ⇒ 写名字、FK 为 NULL。

    ⚠️ 本条的判别力**只在防回归**：新建行的 FK 默认就是 NULL（PATCH 侧的
    `test_patch_freetext_source_nulls_fk` 才是判别点，计划 Part 4 的 T3-M2 实测）。
"""
        r = self._post({"source": "随手写的"})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(r.json()["data"]["order_no"])
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "随手写的")

    def test_create_with_nonexistent_order_source_is_1000(self):
        """不存在的 id ⇒ 200 + code `1000`（业务校验，不是 500），且**库里不多一行**。

    ⚠️ 末句是 T3 评审 Minor 1 补的（E-0009 的纪律：断言打在真实出口上——
    只断响应码的话，一个"先建行再回 1000"的坏实现照样绿）。
"""
        before = Order.objects.count()
        r = self._post({"order_source": 999999})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1000)
        self.assertEqual(Order.objects.count(), before)


class OrderIntakePatchTests(_OrderIntakeMixin, TenantTestCase):
    """spec §8 #9：编辑（PATCH，`OrderUpdateSerializer`）接线。"""

    def setUp(self):
        # ⚠️ **本线的门**：录单（POST）与编辑（PATCH）都要节点
        # （`ORDER_PERMISSIONS["create"] = ("order.add_edit",)`；
        # `["partial_update"] = ("order.add_edit", "order.submit_edit")` —— 任一命中即放行）。
        # 夹具必须**显式声明**它需要的判据——这不是放宽断言，而是这批用例原先只靠
        # 「登录」就够、本线之后不够了。
        # 组名用「录入组」：它名下没有预置 Role、节点全靠 `nodes=` 显式声明 ⇒ 不会顺带
        # 改变 `allowed_transitions` 之类的行为（本文件也不断言那些）。
        # ⚠️ `Role.name` 是 `max_length=5` ⇒ 组名必须 ≤5 字。
        self.client = make_logged_in_client(
            groups=("录入组",), nodes=("order.add_edit", "order.submit_edit")
        )
        self.game = Game.objects.create(name="原神")
        self.source = OrderSource.objects.create(name="闲鱼A")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——PATCH 既走读侧（`get_object()`）也走写侧。
        grant_games(User.objects.get(username=P2_TEST_USERNAME), self.game)

    def _patch(self, order_no, payload):
        return self.client.patch(
            f"{ORDERS}{order_no}/", payload, content_type="application/json"
        )

    def test_patch_order_source_updates_name(self):
        """前端形态 `{order_source: 20}`：FK 变、名字**随之**变成字典名。"""
        order = self._make_order(order_no="20260921000000000001", source="老名字")
        r = self._patch(order.order_no, {"order_source": self.source.id})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertEqual(order.order_source_id, self.source.id)
        self.assertEqual(order.source, "闲鱼A")

    def test_patch_freetext_source_nulls_fk(self):
        """只传自由文本 ⇒ 名字写进去、FK **置为 NULL**（本线的判别点，T3-M2 只此一条红）。"""
        order = self._make_order(
            order_no="20260921000000000002", source="老名字", order_source=self.source
        )
        self.assertEqual(order.order_source_id, self.source.id)
        r = self._patch(order.order_no, {"source": "自由文本"})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "自由文本")

    def test_patch_empty_string_source_nulls_fk(self):
        """`source=""` 是**E-0021 的边界**：空串 ≠ 没传 ⇒ 写空串、FK 置 NULL。

    T3 评审 I2：`apps/business/order_source.py` 的模块 docstring 自己点名「`source` 可以是空串，
    把它当真值判会分叉」，但此前**零覆盖**。判据落在**库里的值**上（字面量 `""`，不是 `None`）。
"""
        order = self._make_order(
            order_no="20260921000000000009", source="老名字", order_source=self.source
        )
        self.assertEqual(order.order_source_id, self.source.id)
        r = self._patch(order.order_no, {"source": ""})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "")

    def test_patch_null_order_source_keeps_name(self):
        """前端形态 `{order_source: null}` ⇒ FK 置空、`source` **一字不动**（快照语义）。"""
        order = self._make_order(
            order_no="20260921000000000003",
            source="字典时代的老名字",
            order_source=self.source,
        )
        r = self._patch(order.order_no, {"order_source": None})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "字典时代的老名字")

    def test_patch_nonexistent_order_source_is_1000_and_keeps_row(self):
        """PATCH 的非法 id 同样回 `1000`，且**不落库**（行保持原样）。"""
        order = self._make_order(
            order_no="20260921000000000004", source="老名字", order_source=self.source
        )
        r = self._patch(order.order_no, {"order_source": 999999})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1000)
        order = self._reload(order.order_no)
        self.assertEqual(order.order_source_id, self.source.id)
        self.assertEqual(order.source, "老名字")

    def test_patch_to_another_source_follows_the_new_name(self):
        """换绑到**另一个**字典项 ⇒ 名字跟着换成新字典项的名字（不是留在旧名字上）。"""
        other = OrderSource.objects.create(name="抖音B")
        order = self._make_order(
            order_no="20260921000000000005", source="闲鱼A", order_source=self.source
        )
        r = self._patch(order.order_no, {"order_source": other.id})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertEqual(order.order_source_id, other.id)
        self.assertEqual(order.source, "抖音B")

    def test_patch_same_order_source_after_rename_keeps_snapshot(self):
        """字典项**改名**后，再 PATCH（哪怕带上**同一个** `order_source`）**不重拍**名字快照。

    **整体评审 I-1**：前端编辑态在订单已有可解析来源时**总是**回传同一个 `order_source`
    （`order-addorder.ts` 的编辑回填），而旧实现只要收到非空 `order_source` 就**无条件**用字典
    当前名覆盖 `source` ⇒「只改个 `tag`」也会把历史快照重拍成改名后的名字，
    推翻 D4/R7 的快照语义（改名不重写历史）。

    判据落在**库里的两个值**上：FK 仍是同一个（*不*动），`source` 仍是改名**前**的名字。
"""
        order = self._make_order(
            order_no="20260921000000000011",
            source="改名前的名字",
            order_source=self.source,
        )
        self.source.name = "改名后的名字"
        self.source.save(update_fields=["name", "updated_at"])
        r = self._patch(order.order_no, {"tag": "紧急", "order_source": self.source.id})
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertEqual(order.order_source_id, self.source.id)  # FK 不变
        self.assertEqual(order.source, "改名前的名字")            # ★ 快照不被重拍
        self.assertEqual(order.tag, "紧急")                       # 别的字段照改


class OrderSourceDeleteTests(_OrderIntakeMixin, TenantTestCase):
    """spec §8 #10：删除字典项后，历史订单的名字**保留**、FK 变 `NULL`（D5 / R7）。"""
    def setUp(self):
        # ⚠️ `DELETE /order-sources/<id>/` 现在还要
        # `system.delete_source`（`OrderSourceViewSet.ORDER_SOURCE_PERMISSIONS["destroy"]`）。
        # ⚠️ `nodes=` **必须配 `groups=`**（`_grant_nodes` 遍历 `user.groups` 挂节点）。
        self.client = make_logged_in_client(
            groups=("来源组",), nodes=("system.delete_source",)
        )
        self.game = Game.objects.create(name="原神")
        self.source = OrderSource.objects.create(name="要删掉的来源")

    def test_delete_source_nulls_fk_and_keeps_name(self):
        order = self._make_order(
            order_no="20260921000000000006",
            source="要删掉的来源",
            order_source=self.source,
        )
        r = self.client.delete(f"{SOURCES}{self.source.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        order = self._reload(order.order_no)
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "要删掉的来源")

    def test_rename_source_does_not_rewrite_history(self):
        """**D4 的设计**：字典项**改名**后，历史订单的 `source` 快照**不跟随**（FK 也不动）。

    T3 评审 Minor 3：这条是刻意设计（`source` 降级为「录单当时的名字」），
    此前没有用例钉住 —— 没有它的话，一个「改名时批量回写历史订单」的改动**不会红**，
    而那恰好推翻 R7/D5（删除也要保留旧名）背后的同一条原则。
"""
        order = self._make_order(
            order_no="20260921000000000010",
            source="改名前的名字",
            order_source=self.source,
        )
        self.source.name = "改名后的名字"
        self.source.save(update_fields=["name", "updated_at"])
        order = self._reload(order.order_no)
        self.assertEqual(order.order_source_id, self.source.id)   # FK 不动
        self.assertEqual(order.source, "改名前的名字")             # 快照不跟随


class OrderSourceRemarkPayloadTests(TenantTestCase):
    """**I-4 的前提**：XSS payload 真的能落进 `remark`。

    整体评审探针实测 `stored_remark='<img src=x onerror=alert(1)>'`（`escaped=False`）。
    前端的转义测试若不同时钉住「这段文本能落库」，就会像在测一个**到不了**的形状——
    本用例把前提写在真实出口上（POST → 库里那行），而不是前端替身。
"""

    def setUp(self):
        # ⚠️ `POST /order-sources/` 现在还要
        # `system.add_edit_source`（`ORDER_SOURCE_PERMISSIONS["create"]`）。
        self.client = make_logged_in_client(
            groups=("来源组",), nodes=("system.add_edit_source",)
        )

    def test_payload_remark_fits_and_persists(self):
        """`remark` 上限 50，整段 payload（28 字）能原样落库。"""
        payload = '<img src=x onerror=alert(1)>'
        self.assertEqual(len(payload), 28)
        self.assertLessEqual(len(payload), 50)
        r = self.client.post(
            SOURCES,
            {"name": "XSS来源", "remark": payload},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 0)
        row = OrderSource.objects.get(name="XSS来源")
        self.assertEqual(row.remark, payload)


class OrderSourceIntakeIsolationTests(_OrderIntakeMixin, TenantTestCase):
    """跨租户（`AGENTS.md` §4 红线）：**A 的订单不能引用 B 的来源**。

    ⚠️ 判据**不用主键**指认对方那一行（两个 schema 的主键序列各自从 1 开始，
    `filter(id=…)` 会命中自己那一行）⇒ 用**两租户取值不同、租户内唯一**的来源名。
"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="订单来源接线隔离租户 B", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # 第一行：把连接显式钉回租户 A（schema 上下文会跨用例残留）。
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="原神")
        # ⚠️ 本线的门：PATCH 要 `order.add_edit` / `order.submit_edit`。
        # 组名「录入组」名下没有预置 Role，节点全靠 `nodes=` 显式声明。
        self.client = make_logged_in_client(
            domain=DOMAIN_A, username="src-intake-a",
            groups=("录入组",), nodes=("order.add_edit", "order.submit_edit"),
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——A 侧这张单要看得见/改得动，账号得有 A 的游戏权限。
        grant_games(User.objects.get(username="src-intake-a"), self.game)

    def test_a_order_cannot_reference_b_source(self):
        """A 的订单 PATCH 一个「只有 B 侧才存在」的 id ⇒ `1000`，且 A 的行不变。

    ⚠️ **构造的关键（本文件第一版在这里栽过，如实记录）**：两个 schema 的主键序列
    各自从 1 开始 ⇒ 若 A、B 各只有一行，**「B 的来源 id」在 A 里恰好也是 A 自己那一行**，
    于是「引用 B 的来源」在 A 里是**合法**的（实测：回 `code 0`，`FAIL: 0 != 1000`）——
    这不是代码的漏洞，而是**判据没有判别力**（`AGENTS.md` §5 点名的那种坑）。
    所以构造改成：A 侧 1 行、B 侧**足够多**行，取 B 的**最大 id**（在 A 的 id 值域之外，
    且断言它确实**不在** A 的 id 集合里）。名字仍作旁证（不单独当判据）。
"""
        with tenant_context(self.tenant_b):
            # B 侧造 3 行 ⇒ 最大 id ≥ 3，稳稳落在 A 侧（只有 1 行）的值域之外。
            for name in ("B 的来源一", "B 的来源二", "B 的来源三"):
                OrderSource.objects.create(name=name)
            b_max_id = OrderSource.objects.order_by("-id").values_list("id", flat=True)[0]
            self.assertEqual(OrderSource.objects.count(), 3)
        with tenant_context(self.tenant):
            OrderSource.objects.create(name="A 的唯一的来源")
            order = self._make_order(order_no="20260921000000000007", source="")
            a_ids = set(OrderSource.objects.values_list("id", flat=True))
            self.assertEqual(len(a_ids), 1)
            # 构造自证：这个 id 在 A 里**确实不存在**，所以下面的拒绝不可能来自
            # 「A 里恰好有同号行」。没有这一步，本用例会在 id 撞上时零判别力地变绿。
            self.assertNotIn(b_max_id, a_ids)

        connection.set_tenant(self.tenant)
        # 判据（名字，不按主键）：B 的来源名在 A 侧一行都没有，A 的来源名在 B 侧也没有。
        with tenant_context(self.tenant_b):
            self.assertFalse(OrderSource.objects.filter(name="A 的唯一的来源").exists())
        with tenant_context(self.tenant):
            self.assertFalse(OrderSource.objects.filter(name="B 的来源一").exists())

        # 红线：把「只有 B 才有的 id」当 `order_source` 发给 A 的租户 ⇒ 必须被拒（1000），
        # 且 A 的订单**不能**因此挂上任何字典项。
        r = self.client.patch(
            f"{ORDERS}{order.order_no}/",
            {"order_source": b_max_id},
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], 1000)
        connection.set_tenant(self.tenant)
        order = self._reload(order.order_no)
        self.assertIsNone(order.order_source_id)
        self.assertEqual(order.source, "")

    def test_a_cannot_read_b_order_source_by_name(self):
        """凭据层的同一条红线，打在**来源字典端点**上：A 的 client 查不到 B 的名字。"""
        with tenant_context(self.tenant):
            OrderSource.objects.create(name="A 的来源")
        with tenant_context(self.tenant_b):
            OrderSource.objects.create(name="B 的来源")

        connection.set_tenant(self.tenant)
        body = self.client.get(f"{SOURCES}?page_size=200").json()
        self.assertEqual(body["code"], 0)
        # 非空哨兵：A 自己那一行必须出现，否则下面的否定断言没有判别力。
        self.assertIn("A 的来源", [i["name"] for i in body["data"]["items"]])
        self.assertNotIn("B 的来源", [i["name"] for i in body["data"]["items"]])


class OrderListSourceColumnTests(_OrderIntakeMixin, TenantTestCase):
    """管理订单页「订单来源」列：**列表载荷必须带 `source`**（业务方 2026-09-21 反馈「没正常显示」）。

    修前的形状：模型有 `source`、详情有、**列表没有**（`OrderListSerializer.Meta.fields` 漏了它），
    而前端那一列还被 `templet: () => ''` 钉死 ⇒ 列恒空。
    判别力：把 `"source"` 从 `OrderListSerializer.Meta.fields` 删掉 ⇒ 本用例必须红。
"""

    def setUp(self):
        # ⚠️ 本线的门：列表读要 `order.list` 节点（`ORDER_PERMISSIONS["list"]`）。
        # 组名「录入组」名下没有预置 Role，节点全靠 `nodes=` 显式声明。
        self.client = make_logged_in_client(
            groups=("录入组",), nodes=("order.list",)
        )
        self.game = Game.objects.create(name="原神")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 该用例的期望值一个字没改——列表读侧按游戏权限收窄，没这条就恒空。
        grant_games(User.objects.get(username=P2_TEST_USERNAME), self.game)

    def test_list_payload_carries_source(self):
        """列表载荷带 `source`（**快照**键）。

    ⚠️ 2026-10-23 改名（原名 `test_list_payload_carries_source_name`）：那个名字会与
    本批新增的**派生键** `source_name`（字典当前名，见下面的
    `OrderListSourceDisplayNameTests`）撞车——本用例断的始终是 `source` 这个**快照**列，
    断言与期望值一个字没改。
"""
        with tenant_context(self.tenant):
            OrderSource.objects.create(name="闲鱼A")
        order = self._make_order(order_no="20260921000000000011", source="闲鱼A")
        body = self.client.get(ORDERS).json()
        self.assertEqual(body["code"], 0)
        rows = body["data"]["items"]
        # 非空哨兵：先证明这一行真的在列表里，再断言字段（否则空列表会让下面恒假/恒真）。
        self.assertIn(order.order_no, [r["order_no"] for r in rows])
        row = next(r for r in rows if r["order_no"] == order.order_no)
        self.assertEqual(row["source"], "闲鱼A")


class OrderListSourceDisplayNameTests(_OrderIntakeMixin, TenantTestCase):
    """管理订单页「订单来源」列的显示值：**字典当前名**优先，无字典回落**快照**（2026-10-23）。

    业务口径：那一列显示 `source_name`（派生键）；`source`（快照）继续原样回，数据库里
    **一个字都不改**（历史不重写，D4 / R7）。四条判据各一个用例：

    1. 字典项**改名**后 ⇒ 列表返回**新名**（`test_a_renamed_source_shows_the_current_name`）；
    2. 字典项**被删**（FK 置空）后 ⇒ 回落**快照**（`test_deleting_the_dictionary_falls_back_to_the_snapshot`）；
    3. **自由文本**单（从未挂字典）⇒ 仍是快照（`test_a_free_text_order_still_shows_the_snapshot`）；
    4. 列表 SQL 条数**不随行数增长**（挡住 N+1，`test_query_count_does_not_grow_with_row_count`）。

    ⚠️ 派生键只加在 `OrderSplitListSerializer`（订单列表这一条读面）与详情 `OrderSerializer`
    上，两处**同源**（模块级 `order_source_display_name`）⇒ 第 1 条顺手把详情那一路也钉住
    （只断列表的用例挡不住「详情另抄一份」）。
"""

    def setUp(self):
        # 本线的门：列表读 `order.list`、详情读 `order.view_detail`。
        # 组名「来源组」名下没有预置 Role，节点全靠 `nodes=` 显式声明。
        self.client = make_logged_in_client(
            groups=("来源组",), nodes=("order.list", "order.view_detail")
        )
        self.game = Game.objects.create(name="原神")
        # 列表读侧按游戏权限收窄：夹具补齐，不是放宽判据（没有它那几行看不见、用例恒空）。
        grant_games(User.objects.get(username=P2_TEST_USERNAME), self.game)

    def _items(self):
        """列表载荷的 `items`；先断业务码（端点失败时别让取值炸在别处）。"""
        body = self.client.get(ORDERS).json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]["items"]

    def _row(self, order_no):
        rows = {row["order_no"]: row for row in self._items()}
        self.assertIn(order_no, rows, rows)  # 非空哨兵
        return rows[order_no]

    def test_a_renamed_source_shows_the_current_name(self):
        """字典项改名 ⇒ 列表回**新名**；`source` 快照（键与值）与库里那一列一个字不动。"""
        source = OrderSource.objects.create(name="闲鱼A")
        order = self._make_order(
            order_no="20261023000000000001", source="闲鱼A", order_source=source
        )
        source.name = "改名后的闲鱼"
        source.save(update_fields=["name", "updated_at"])

        row = self._row(order.order_no)
        self.assertEqual(row["source_name"], "改名后的闲鱼")  # ★ 当前名
        self.assertEqual(row["source"], "闲鱼A")              # 快照键仍在、值不变
        # 判据打在**库里的行**上：读面派生，快照一个字都不重写（D4 / R7）。
        self.assertEqual(self._reload(order.order_no).source, "闲鱼A")

        # 详情与列表**同源**：同一个模块级函数（⛔ 抄两份的实现会在这里分叉）。
        detail = self.client.get(f"{ORDERS}{order.order_no}/").json()
        self.assertEqual(detail["code"], 0, detail)
        self.assertEqual(detail["data"]["source_name"], "改名后的闲鱼")
        self.assertEqual(detail["data"]["source"], "闲鱼A")

    def test_deleting_the_dictionary_falls_back_to_the_snapshot(self):
        """字典项被删（FK `SET_NULL` 置空）⇒ 回落 `source` 快照，历史名字不丢。"""
        source = OrderSource.objects.create(name="要删掉的来源")
        order = self._make_order(
            order_no="20261023000000000002", source="要删掉的来源", order_source=source
        )
        source.delete()

        reloaded = self._reload(order.order_no)  # 前置：`SET_NULL` 真的发生了
        self.assertIsNone(reloaded.order_source_id)
        self.assertEqual(reloaded.source, "要删掉的来源")

        row = self._row(order.order_no)
        self.assertEqual(row["source_name"], "要删掉的来源")
        self.assertEqual(row["source"], "要删掉的来源")

    def test_a_free_text_order_still_shows_the_snapshot(self):
        """从未挂过字典的自由文本单 ⇒ `source_name` 就是 `source`（不受本批影响）。"""
        order = self._make_order(order_no="20261023000000000003", source="随手写的来源")
        self.assertIsNone(order.order_source_id)

        row = self._row(order.order_no)
        self.assertEqual(row["source_name"], "随手写的来源")
        self.assertEqual(row["source"], "随手写的来源")

    def test_query_count_does_not_grow_with_row_count(self):
        """⛔ 不许 N+1：挂了字典的行从 1 涨到 6，列表 SQL 条数**不变**。

    判别力：去掉 `OrderViewSet.get_queryset()` 里的 `select_related("order_source")`
    ⇒ 6 行那次每行多一条字典查询 ⇒ 本用例红。
"""
        source = OrderSource.objects.create(name="查询数来源")
        self._make_order(
            order_no="20261023000000000011", source="查询数来源", order_source=source
        )
        with CaptureQueriesContext(connection) as captured:
            small = self._items()
        self.assertEqual(len(small), 1, small)  # 非空前置
        small_count = len(captured)

        for index in range(1, 6):
            self._make_order(
                order_no=f"2026102300000000002{index}",
                source="查询数来源",
                order_source=source,
            )
        with CaptureQueriesContext(connection) as captured:
            big = self._items()
        self.assertEqual(len(big), 6, big)  # 非空哨兵：第二次真的序列化了 6 行
        # 值也真出得来：否则「不查库」可以靠恒返回空串的桩骗过（E-0024 同族）。
        self.assertEqual({row["source_name"] for row in big}, {"查询数来源"})
        self.assertEqual(len(captured), small_count)
