"""订单时间线端点（D-0360）的契约与隔离网。

覆盖（计划 §四.3）：响应契约形状、双号归一（两臂歧义规则钉死）、
**查无 = 跨租户 = key 缺失同形同码**（防按响应差异探测他租单号）、
**软删单可见 + `is_deleted`**（审计语义）、403（无节点）、
**跨租户隔离**（双向 + 阳性对照，照 `test_operation_log_isolation.py` 的纪律）、
节上限截断、`bindings_health` 挂起判定（两级查询）。

## 判据纪律（照 `test_operation_log_isolation.py` 逐条）

1. **不用主键指认「另一个租户的那一行」**：判据一律用**租户内唯一、且两租户取值
   不同**的业务值（20 位订单号 / 来源号标记串）。
2. **每条否定断言之前先给阳性对照**（否则空表能让 `assertFalse` 空洞通过）。
3. **夹具一律合成值**（E-0077）：订单号 / 来源号 / 店名都是编的，⛔ 不用真实单号。
4. 期望值写字面值（E-0024）；断言打在响应体 / 数据库行上（E-0009）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.jidanpu_member import (
    LAUNCH_READBACK_FAILURE_REASONS,
    readback_breaker_health,
)
from apps.business.models import (
    DailiantongPush,
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    Performance,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
    XgjReceiptRecord,
)
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

# ── 端点与判据用的业务值（全部合成；E-0077）────────────────────────────────
ENDPOINT = "/api/audit/order-timeline/"
BREAKERS_ENDPOINT = "/api/audit/jidanpu-breakers/"
NODE_CODE = "order.platform_action_logs"

#: §1.6：查无 / 跨租户 / key 缺失**同形同码**的钉子（字面值，E-0024）。
NOT_FOUND_CODE = 1002
NOT_FOUND_MSG = "未找到匹配的订单"

#: 七个固定节名与顺序（§1.6；字面值钉住，改节名必须同时改视图与这里）。
SECTION_KEYS = [
    "operation_log", "jidanpu_sync", "peer_sync", "dailiantong", "mirror", "qr_fetch",
    "receipt",
]

#: 节上限（§1.6：operation_log 200、其余 100）。
OP_CAP = 200
SECTION_CAP = 100

NO_A = "20261101000000000001"
NO_A2 = "20261101000000000002"   # 归一歧义用例的第二张单（同来源号、更新）
NO_DEAD = "20261101000000000003"  # 软删单
SRC_A = "SRC-TT-0001"
SRC_A2 = "SRC-TT-0002"
SRC_DEAD = "SRC-TT-DEAD"

#: 隔离用：两租户**共用**的订单号（订单号只有租户内唯一）+ 各自独有的来源号标记。
SHARED_NO = "20261101000000000009"
SRC_MARK_A = "SRC-ONLY-A"
SRC_MARK_B = "SRC-ONLY-B"
NO_B_ONLY = "20261101000000000010"

GAME_A = "测试游戏甲"
STORE_A = "合成下家店铺甲"
PEER_NO_A = "PEER-NO-0001"
PEER_NO_OTHER = "PEER-NO-OTHER"

USERNAME_A = "timeline-p13-a"
USERNAME_B = "timeline-p13-b"


def _make_order(order_no, *, source_order_no="", deleted_at=None, created_at=None):
    """在当前 schema 造一张单（形状照 `test_order_soft_delete_read_paths` 的夹具）。

    ⚠️ `Order.created_at` 是 `auto_now_add` ⇒ 显式时刻要走 `update` 绕开（同刻 tie-break
    用例的确定性靠它）。
    """
    game = Game.objects.get_or_create(name=GAME_A)[0]
    order = Order.objects.create(
        game=game,
        order_no=order_no,
        source_order_no=source_order_no,
        status=OrderStatus.IN_PROGRESS,
        total_price=Decimal("100.00"),
        hall_price=Decimal("80.00"),
        deleted_at=deleted_at,
    )
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
    return order


def _make_log(order_no, *, source_order_no="", summary=""):
    """在当前 schema 造一条操作留痕（模型层写入口，照隔离测试的 `make_row`）。"""
    return OperationLog.objects.create(
        log_type=10,
        action="order.transition",
        actor="timeline-fixture-actor",
        order_no=order_no,
        source_order_no=source_order_no,
        summary=summary,
    )


def _grant_node_and_login(domain, username):
    """真实登录的 client，只持 `order.platform_action_logs` 这一个节点。

    ⚠️ **节点必须与 `Group → Role → permissions` 成对接**（`p5_auth_helpers` 的已知
    纪律：`make_logged_in_client` 只给 `nodes=` 不给组 ⇒ 一个节点都挂不上 ⇒ 全 403），
    照 `test_operation_log_isolation._grant_logs_node_and_login` 的形状手工接。
    ⚠️ 用 `get_or_create`：租户 A（`TenantTestCase` 的 `test` schema）只有迁移、没有
    目录种子，B 是 `provision_tenant` 建的、已灌完整目录——硬 `create` 会撞
    `benchmark_id` 唯一约束（先例同款实测）。
    """
    node, _ = PermissionNode.objects.get_or_create(
        benchmark_id=1267,
        defaults={
            "code": NODE_CODE,
            "name": "交易订单操作记录",
            "note": "勾选后代表此职位的人可以进入查看针对平台订单进行操作的记录列表",
            "url": "/Order/TradingOrderActionLogList",
            "kind": NodeKind.PAGE,
            "level": 1,
        },
    )
    assert node.code == NODE_CODE, f"benchmark_id=1267 不是本节点：{node.code!r}"
    client = make_logged_in_client(domain=domain, username=username)
    # ⚠️ `Role.name` 列宽只有 5 且 schema 内唯一（`apps/rbac/models.py:46`）——
    # 长名实测炸过（varchar(5) DataError）；group 名可以长，role 名不行。
    group = Group.objects.get_or_create(name=f"时间线审计员-{username}")[0]
    role, _ = Role.objects.get_or_create(name="审计员", defaults={"group": group})
    role.permissions.set([node])
    get_user_model().objects.get(username=username).groups.add(role.group)
    return client


def _timeline(client, key):
    """查一次时间线，断言 HTTP 200 + 信封成功，回 `(code, msg, data)`。"""
    response = client.get(ENDPOINT, {"key": key})
    assert response.status_code == 200, response.content[:300]
    body = response.json()
    return body["code"], body["msg"], body["data"]


# ═══════════════════════════════════════════════════════════════════════
# 契约与节形状


class TimelineContractTests(TenantTestCase):
    """响应契约（§1.6）：data 形状 / 节集合 / 同形同码 / 软删单 / 上限截断。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_a = _grant_node_and_login("tenant.test.com", USERNAME_A)

    def test_data_shape_and_fixed_section_keys(self):
        _make_order(NO_A, source_order_no=SRC_A)
        _make_log(NO_A, summary="【测试操作人】状态从待抢单改为代练中")
        code, _msg, data = _timeline(self.client_a, NO_A)
        self.assertEqual(code, 0)
        # data 恰好四键（§1.6 钉死；多键少键都红）
        self.assertEqual(
            sorted(data.keys()), ["bindings_health", "order", "related", "sections"]
        )
        # 节名固定顺序、空节照出（本夹具没有 peer / dailiantong 行）
        self.assertEqual([s["key"] for s in data["sections"]], SECTION_KEYS)
        for section in data["sections"]:
            self.assertEqual(
                sorted(section.keys()), ["items", "key", "label", "truncated"]
            )
            self.assertTrue(section["label"], "每节必须带人话标题")
        peer_section = data["sections"][2]
        self.assertEqual(peer_section["key"], "peer_sync")
        self.assertEqual(peer_section["items"], [])
        self.assertFalse(peer_section["truncated"])

    def test_operation_log_section_carries_unified_item_shape(self):
        _make_order(NO_A, source_order_no=SRC_A)
        _make_log(NO_A, summary="【测试操作人】改了状态")
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][0]["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        for key in ("at", "kind", "kind_label", "status", "summary", "actor"):
            self.assertIn(key, item, f"统一 item 形状缺 {key}")
        self.assertEqual(item["kind"], "order.transition")
        self.assertEqual(item["summary"], "【测试操作人】改了状态")
        self.assertEqual(item["actor"], "timeline-fixture-actor")

    def test_operation_log_source_arm_is_queried_only_with_non_empty_source(self):
        """source 臂：该单来源号非空时，来源号命中的日志也进节；空来源单不发这条查询。"""
        order = _make_order(NO_A, source_order_no=SRC_A)
        # 一条只有来源号的日志（`order_no` 留空——source 臂的典型行）
        _make_log("", source_order_no=SRC_A, summary="【测试操作人】发货回执")
        _code, _msg, data = _timeline(self.client_a, NO_A)
        summaries = [item["summary"] for item in data["sections"][0]["items"]]
        self.assertIn("【测试操作人】发货回执", summaries)
        # 来源号为空的单：日志里只有 order_no 臂命中的
        _make_log(order.order_no, summary="【测试操作人】无来源单的日志")
        _code, _msg, data = _timeline(self.client_a, NO_A)
        summaries = [item["summary"] for item in data["sections"][0]["items"]]
        self.assertIn("【测试操作人】无来源单的日志", summaries)

    def test_normalize_order_no_arm_beats_source_arm_and_latest_wins(self):
        """双号歧义（§1.2 钉死）：order_no 精确命中优先；仅 source 臂多条取 created_at 最新。"""
        older = _make_order(NO_A, source_order_no=SRC_A, created_at=timezone.now())
        Order.objects.filter(pk=older.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=1)
        )
        newer = _make_order(NO_A2, source_order_no=SRC_A)
        third = _make_order(NO_DEAD, source_order_no=SRC_DEAD)
        # ① order_no 精确命中优先：即使 NO_A 的来源号也能命中 newer，key=NO_A 仍归一到 older
        _code, _msg, data = _timeline(self.client_a, NO_A)
        self.assertEqual(data["order"]["order_no"], older.order_no)
        # ② 仅 source 臂命中两张 ⇒ 取 created_at 最新（newer）
        _code, _msg, data = _timeline(self.client_a, SRC_A)
        self.assertEqual(data["order"]["order_no"], newer.order_no)
        # ③ 阳性对照：另一张的来源号只命中它自己
        _code, _msg, data = _timeline(self.client_a, SRC_DEAD)
        self.assertEqual(data["order"]["order_no"], third.order_no)

    def test_soft_deleted_order_is_visible_with_is_deleted_flag(self):
        """软删单**不是查无**：返回完整时间线 + `is_deleted=true`（审计语义，§1.6）。"""
        _make_order(NO_DEAD, source_order_no=SRC_DEAD, deleted_at=timezone.now())
        _make_log(NO_DEAD, summary="【测试操作人】删单前留痕")
        code, _msg, data = _timeline(self.client_a, NO_DEAD)
        self.assertEqual(code, 0)
        self.assertTrue(data["order"]["is_deleted"])
        self.assertIsNotNone(data["order"]["deleted_at"])
        self.assertEqual(
            [i["summary"] for i in data["sections"][0]["items"]],
            ["【测试操作人】删单前留痕"],
        )

    def test_not_found_key_missing_and_unknown_are_the_same_shape(self):
        """查无 / key 缺失同形同码（跨租户那一半在隔离类里钉）。"""
        empty_key = self.client_a.get(ENDPOINT, {"key": ""})
        no_key = self.client_a.get(ENDPOINT)
        unknown = self.client_a.get(ENDPOINT, {"key": "99999999999999999999"})
        for response in (empty_key, no_key, unknown):
            self.assertEqual(response.status_code, 200, response.content[:300])
            body = response.json()
            self.assertEqual(body["code"], NOT_FOUND_CODE, body)
            self.assertEqual(body["msg"], NOT_FOUND_MSG, body)
            self.assertIsNone(body["data"], body)

    def test_section_caps_and_truncated_flags(self):
        _make_order(NO_A)
        for i in range(OP_CAP + 1):
            _make_log(NO_A, summary=f"【测试操作人】第 {i} 条")
        _code, _msg, data = _timeline(self.client_a, NO_A)
        op = data["sections"][0]
        self.assertEqual(len(op["items"]), OP_CAP)
        self.assertTrue(op["truncated"])
        # 其余节用 jdp 台账验 100（同一条钉子钉在另一节上，防 cap 常量写串）
        for i in range(SECTION_CAP + 1):
            JidanpuSyncEvent.objects.create(
                direction="out", kind="member.launch", status="done",
                target_type="order", target_ref=NO_A,
            )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        jdp = data["sections"][1]
        self.assertEqual(len(jdp["items"]), SECTION_CAP)
        self.assertTrue(jdp["truncated"])
        # 未触顶的节 truncated=false（阳性对照）
        self.assertFalse(data["sections"][2]["truncated"])

    def test_qr_fetch_section_declares_only_hit_rows_and_suppress_deadline(self):
        """⭐ 查码节空态提示语照实（只查到时留行）+ 退避截止。"""
        order = _make_order(NO_A)
        deadline = timezone.now()
        order.qr_fetch_suppress_until = deadline
        order.save(update_fields=["qr_fetch_suppress_until"])
        _code, _msg, data = _timeline(self.client_a, NO_A)
        qr = data["sections"][5]
        self.assertEqual(qr["key"], "qr_fetch")
        self.assertEqual(len(qr["items"]), 1)
        self.assertIn("查码只在查到时留行", qr["items"][0]["summary"])
        self.assertIsNotNone(qr["items"][0]["suppress_until"])

    def test_mirror_section_carries_upstream_card_and_split_group(self):
        order = _make_order(NO_A, source_order_no=SRC_A)
        order.split_id = "20261101000000000999"
        order.save(update_fields=["split_id"])
        _make_order(NO_A2, source_order_no=SRC_A2)
        Order.objects.filter(order_no=NO_A2).update(split_id=order.split_id)
        game = Game.objects.get(name=GAME_A)
        card = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url="https://mirror.example.test",
            upstream_order_id="UP-0001",
            game=game,
            status="promoted",
            first_seen_at=timezone.now(),
        )
        order.upstream_hall_card = card
        order.save(update_fields=["upstream_hall_card"])
        _code, _msg, data = _timeline(self.client_a, NO_A)
        mirror = data["sections"][4]
        kinds = [item["kind"] for item in mirror["items"]]
        self.assertIn("mirror.upstream_hall_card", kinds)
        self.assertIn("mirror.split_group", kinds)
        group = next(i for i in mirror["items"] if i["kind"] == "mirror.split_group")
        self.assertEqual(sorted(group["member_order_nos"]), sorted([NO_A, NO_A2]))
        # related 的拆组投影与节内一致
        self.assertEqual(
            data["related"]["split_group"]["member_order_nos"], group["member_order_nos"]
        )

    def test_jidanpu_section_uses_order_arm_plus_callback_compound_key(self):
        """jdp 节：`target_type="order"` 臂 + 逐绑定 callback 复合键；⛔ binding 维度行不进。"""

        order = _make_order(NO_A)
        user = get_user_model().objects.create_user(
            username="binding-user-a", password="合成密码-甲"
        )
        binding = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://shop.example.test",
            account="shop-account-a", password="合成密码-甲", enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id="REMOTE-1"
        )
        # ① order 臂（queue_member_event 的形状）
        JidanpuSyncEvent.objects.create(
            direction="out", kind="member.launch", status="failed",
            target_type="order", target_ref=order.order_no, reason="上游 500",
        )
        # ② callback 复合键——**第二段 = 对端（鸡蛋铺侧）单号**（F1 改准后的真实写形，
        #    实证 `jidanpu_member_openapi.py:426`：回调报文的 orderNo 经链路表按
        #    `jidanpu_order_id` 匹配，不是我们自己的 20 位单号）
        JidanpuSyncEvent.objects.create(
            direction="in", kind="openapi.status_in", status="done",
            target_type="callback", target_ref=f"{binding.pk}:REMOTE-1",
        )
        # ②-负：复合键第二段写成**我方单号**的行（曾经的错误形状）——
        # ⛔ 必须不进时间线（守的是「别再退回查我方号」这一条）
        JidanpuSyncEvent.objects.create(
            direction="in", kind="openapi.status_in", status="done",
            target_type="callback", target_ref=f"{binding.pk}:{order.order_no}",
        )
        # ③ binding 维度行（熔断台账的形状）——⛔ 必须不进时间线
        JidanpuSyncEvent.objects.create(
            direction="out", kind="member.launch_breaker_open", status="skipped",
            target_type="binding", target_ref=str(binding.pk),
            payload={"member_binding_id": binding.pk, "probe": False},
        )
        # ④ 同单号但 target_type 不对（裸 target_ref 禁区的形状）——⛔ 必须不进
        JidanpuSyncEvent.objects.create(
            direction="in", kind="openapi.status_in", status="done",
            target_type="hall", target_ref=order.order_no,
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        kinds = [item["kind"] for item in data["sections"][1]["items"]]
        self.assertIn("member.launch", kinds)
        # 阳性（F1 的核心判据）：**远端号键**的 callback 行查得到
        self.assertIn("openapi.status_in", kinds)
        # 负性：我方单号键的 callback 行查不到（它就在库里，不是写丢了）
        refs = [item["target_ref"] for item in data["sections"][1]["items"]]
        self.assertIn(f"{binding.pk}:REMOTE-1", refs)
        self.assertNotIn(f"{binding.pk}:{order.order_no}", refs)
        self.assertNotIn("member.launch_breaker_open", kinds)
        # ④ 的行存在但没进来（阳性对照在库层面做）
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                target_type="hall", target_ref=order.order_no
            ).exists()
        )

    def test_jidanpu_callback_arm_upstream_side_uses_upstream_order_id(self):
        """上家线 callback 臂（F1）：复合键第二段 = `upstream_order_id`（上家侧单号）。"""
        order = _make_order(NO_A)
        user = get_user_model().objects.create_user(
            username="upstream-cb-user", password="合成密码-戊"
        )
        upstream_binding = JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url="https://up.example.test",
            account="up-account-a", password="合成密码-戊", enabled=True,
        )
        JidanpuUpstreamOrderLink.objects.create(
            binding=upstream_binding, order=order, upstream_order_id="UP-REMOTE-9"
        )
        # 上家 openapi 消费引擎的真实写形（`jidanpu_openapi_consume.py:605-606`）
        JidanpuSyncEvent.objects.create(
            direction="in", kind="openapi.callback_unknown_order", status="failed",
            target_type="callback",
            target_ref=f"{upstream_binding.pk}:UP-REMOTE-9",
            reason="合成原因：这个回调码下的绑定里都没有这条链路",
            payload={"order_no": "UP-REMOTE-9", "source_order_no": ""},
        )
        # 无链路的 member 绑定名下的 callback 行（本单不指向它）⇒ 不得混进来。
        member_user = get_user_model().objects.create_user(
            username="member-cb-user", password="合成密码-己"
        )
        member_binding = JidanpuMemberBinding.objects.create(
            user=member_user, jidanpu_base_url="https://member-cb.example.test",
            account="member-account-a", password="合成密码-己", enabled=True,
        )
        JidanpuSyncEvent.objects.create(
            direction="in", kind="openapi.status_in", status="done",
            target_type="callback",
            target_ref=f"{member_binding.pk}:别人家的远端号",
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        refs = [item["target_ref"] for item in data["sections"][1]["items"]]
        self.assertEqual(refs, [f"{upstream_binding.pk}:UP-REMOTE-9"])

    def test_jidanpu_callback_arm_skips_links_without_remote_id(self):
        """member 链路行 `jidanpu_order_id` 为空（还没录成单）⇒ 不参与 callback 臂。"""
        order = _make_order(NO_A)
        user = get_user_model().objects.create_user(
            username="cb-empty-user", password="合成密码-庚"
        )
        binding = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://empty.example.test",
            account="empty-account-a", password="合成密码-庚", enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id=""
        )
        JidanpuSyncEvent.objects.create(
            direction="in", kind="openapi.status_in", status="done",
            target_type="callback", target_ref=f"{binding.pk}:REMOTE-X",
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        self.assertEqual(data["sections"][1]["items"], [])

    def test_operation_log_dual_arm_same_row_is_deduped(self):
        """S1：`source_order_no == order_no` 的病态行两臂都命中 ⇒ 按 pk 去重只出现一次。

        ⚠️ 顺序口径：合并后仍按各查询的 `Meta.ordering`（最新在前）——晚创建的那条在前，
        判据是「两臂都命中的同一条只出现一次」，不是次序。"""
        order = _make_order(NO_A, source_order_no=NO_A)  # 来源号与系统号同串（病态夹具）
        _make_log(order.order_no, source_order_no=order.order_no, summary="两臂都命中的同一条")
        _make_log(order.order_no, summary="只有 order_no 臂命中")
        _code, _msg, data = _timeline(self.client_a, NO_A)
        summaries = [item["summary"] for item in data["sections"][0]["items"]]
        self.assertEqual(
            sorted(summaries), sorted(["两臂都命中的同一条", "只有 order_no 臂命中"]),
            "同一条日志出现两次 = 双号臂没去重（S1）",
        )
        self.assertEqual(len(summaries), 2)

    def test_taker_display_contract_both_halves(self):
        """S2 接手人契约两半：有打单工资行 ⇒ 显示归属人；无行 ⇒ `taker_display` 为空。"""
        order_with_taker = _make_order(NO_A)
        taker = get_user_model().objects.create_user(
            username="taker-contract-a", password="合成密码-壬"
        )
        Performance.objects.create(
            order=order_with_taker, user=taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"), bonus_amount=Decimal("10.00"),
        )
        _make_order(NO_A2)  # 无任何 Performance 行的对照单
        _code, _msg, data = _timeline(self.client_a, NO_A)
        self.assertEqual(data["order"]["taker_display"], "taker-contract-a")
        _code, _msg, data = _timeline(self.client_a, NO_A2)
        self.assertIsNone(data["order"]["taker_display"])

    def test_peer_section_requires_link_and_peer_order_no(self):
        """peer 节：先经 link 取对端单号再收窄；同绑定不同单号的行不进来。"""
        order = _make_order(NO_A)

        user = get_user_model().objects.create_user(
            username="peer-binder-a", password="合成密码-乙"
        )
        binding = PeerUpstreamBinding.objects.create(
            peer_name="合成上家甲", peer_base_url="https://peer.example.test",
            peer_account="peer-account-a", token_in_hash="hash-in-a",
            token_out="token-out-a", local_base_url="tenant.test.com",
            bound_by=user,
        )
        PeerOrderLink.objects.create(
            order=order, upstream_binding=binding, peer_order_no=PEER_NO_A
        )
        PeerSyncEvent.objects.create(
            binding_role="upstream", binding_id=str(binding.pk), direction="in",
            kind="order.assign", target_type="order", target_ref=PEER_NO_A,
            idempotency_key="idem-a",
        )
        # 同绑定、但对端单号不同（⛔ 只按绑定不按单号收窄会把它拉进来）
        PeerSyncEvent.objects.create(
            binding_role="upstream", binding_id=str(binding.pk), direction="in",
            kind="order.assign", target_type="order", target_ref=PEER_NO_OTHER,
            idempotency_key="idem-b",
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        refs = [item["target_ref"] for item in data["sections"][2]["items"]]
        self.assertEqual(refs, [PEER_NO_A])
        self.assertEqual(data["related"]["peer_links"][0]["peer_order_no"], PEER_NO_A)

    def test_dailiantong_section_fk_direct(self):
        order = _make_order(NO_A)
        DailiantongPush.objects.create(
            order=order, price=Decimal("66.00"), state="sent", ex_order_no=order.order_no
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][3]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "dailiantong.push")
        self.assertEqual(items[0]["status"], "sent")

    def test_bindings_health_lists_only_bindings_of_this_order(self):

        order = _make_order(NO_A)
        user = get_user_model().objects.create_user(
            username="health-binder-a", password="合成密码-丙"
        )
        involved = JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://health.example.test",
            account=STORE_A, password="合成密码-丙", enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=involved, order=order, jidanpu_order_id="REMOTE-H1"
        )
        user2 = get_user_model().objects.create_user(
            username="health-binder-b", password="合成密码-丁"
        )
        JidanpuMemberBinding.objects.create(
            user=user2, jidanpu_base_url="https://health2.example.test",
            account="合成下家店铺乙", password="合成密码-丁", enabled=True,
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        stores = [entry["store"] for entry in data["bindings_health"]]
        self.assertEqual(stores, [STORE_A])
        self.assertFalse(data["bindings_health"][0]["suspended"])


# ═══════════════════════════════════════════════════════════════════════
# peer 节第二臂：派单方广播（轻量档增补，业务方真实单验证出的盲区）

SRC_DISPATCH = "SRC-DISPATCH-0001"
DOWN_URL_A = "https://down-a.example.test"
DOWN_URL_OFF = "https://down-off.example.test"


class PeerDispatchArmTests(TenantTestCase):
    """派单方广播臂：没有 `PeerOrderLink` 也能查到自己抛给下家的大厅台账。

    背景：链路行唯一写点在收单方（`peer_inbound`）⇒ 派单方（kexing 实测单
    hall.throw / hall.reprice / hall.pull 台账俱在、peer 节却为空）。第二臂复用
    `peer_dispatch._hall_targets`（「生效 + 开了大厅推送」两道闸）枚举下游绑定，
    逐绑定按 `(binding_role, binding_id, target_ref__in=双号)` 查；写形实证
    `peer_dispatch._enqueue_hall_row`（`binding_role="downstream"`、
    `target_ref=effective_peer_ref`——来源单号优先、无来源号=内置号）。
    夹具一律合成值（E-0077）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_a = _grant_node_and_login("tenant.test.com", USERNAME_A)

    def _make_downstream_binding(
        self, username, *, base_url, hall_push_enabled=True, state="active"
    ):
        """造一条下游绑定（模型层写入口；两道闸的两个开关都暴露成参数）。"""
        user = get_user_model().objects.create_user(
            username=username, password="合成密码-下游"
        )
        return PeerDownstreamBinding.objects.create(
            account=user,
            peer_base_url=base_url,
            token_in_hash=f"hash-in-{username}",
            token_out=f"token-out-{username}",
            state=state,
            hall_push_enabled=hall_push_enabled,
        )

    def _make_hall_row(self, binding, *, kind, target_ref, idem, status="done"):
        """照 `_enqueue_hall_row` 的真实写形造一行大厅台账（方向 out）。"""
        return PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=str(binding.pk), direction="out",
            kind=kind, target_type="order", target_ref=target_ref,
            status=status, idempotency_key=idem,
        )

    def test_dispatch_arm_finds_hall_rows_without_any_link(self):
        """派单方面：单 + 启用下游绑定 + `hall.throw` 台账行、零 `PeerOrderLink`
        ⇒ peer 节非空且含该行；它绑定 / 它单号的行不混进来。"""
        order = _make_order(NO_A, source_order_no=SRC_DISPATCH)
        binding = self._make_downstream_binding("dispatch-binder-a", base_url=DOWN_URL_A)
        self._make_hall_row(
            binding, kind="hall.throw", target_ref=SRC_DISPATCH, idem="idem-dispatch-a"
        )
        # 负①：同绑定、别的单号（收窄判据——只按绑定收会把它拉进来）
        self._make_hall_row(
            binding, kind="hall.throw", target_ref=PEER_NO_OTHER, idem="idem-dispatch-x"
        )
        # 负②：关了大厅推送的绑定（`_hall_targets` 第二道闸）——行在库里，但不得进时间线
        off = self._make_downstream_binding(
            "dispatch-binder-off", base_url=DOWN_URL_OFF, hall_push_enabled=False
        )
        self._make_hall_row(
            off, kind="hall.throw", target_ref=SRC_DISPATCH, idem="idem-dispatch-off"
        )
        # 派单方的现实：没有任何链路行（唯一写点在收单方）
        self.assertFalse(PeerOrderLink.objects.filter(order=order).exists())
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][2]["items"]
        refs = [item["target_ref"] for item in items]
        self.assertIn(SRC_DISPATCH, refs)
        self.assertEqual(
            [item["kind"] for item in items], ["hall.throw"], "混进了别的绑定/别的单号的行"
        )
        # 负②的行确实在库（不是写丢了，是闸挡住）
        self.assertTrue(
            PeerSyncEvent.objects.filter(idempotency_key="idem-dispatch-off").exists()
        )

    def test_dispatch_arm_falls_back_to_order_no_when_source_empty(self):
        """双键的后半：无来源号的单（`target_ref=内置号`，`effective_peer_ref` 回落）
        同样查得到。"""
        order = _make_order(NO_A)  # 来源号为空
        binding = self._make_downstream_binding("dispatch-binder-b", base_url=DOWN_URL_A)
        self._make_hall_row(
            binding, kind="hall.reprice", target_ref=order.order_no, idem="idem-dispatch-b"
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][2]["items"]
        self.assertEqual([item["kind"] for item in items], ["hall.reprice"])
        self.assertEqual(items[0]["target_ref"], order.order_no)

    def test_dispatch_arm_dual_key_same_string_yields_row_once(self):
        """病态 `source_order_no == order_no`：双键同串不得让同一条行出现两次。"""
        order = _make_order(NO_A, source_order_no=NO_A)  # 来源号与内置号同串（病态夹具）
        binding = self._make_downstream_binding("dispatch-binder-c", base_url=DOWN_URL_A)
        self._make_hall_row(
            binding, kind="hall.take", target_ref=NO_A, idem="idem-dispatch-c"
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][2]["items"]
        self.assertEqual(len(items), 1, "双号同串的行出现两次 = 双键没去重")
        self.assertEqual(items[0]["kind"], "hall.take")

    def test_dispatch_arm_skips_revoked_binding(self):
        """`_hall_targets` 第一道闸：已解除（`state=revoked`）的绑定不枚举。"""
        order = _make_order(NO_A, source_order_no=SRC_DISPATCH)
        binding = self._make_downstream_binding(
            "dispatch-binder-d", base_url=DOWN_URL_A, state="revoked"
        )
        self._make_hall_row(
            binding, kind="hall.pull", target_ref=SRC_DISPATCH, idem="idem-dispatch-d"
        )
        self.assertTrue(
            PeerSyncEvent.objects.filter(idempotency_key="idem-dispatch-d").exists()
        )
        _code, _msg, data = _timeline(self.client_a, NO_A)
        self.assertEqual(data["sections"][2]["items"], [])


# ═══════════════════════════════════════════════════════════════════════
# 收款记录节（轻量档提前落地，计划 §九 C：串单键 = 来源号口径）

RECEIPT_APP_KEY = "app-key-receipt-a"
SHOP_SNAPSHOT_A = "合成店铺甲"
SHOP_EID_B = "SELLER-EID-SYNTH-B"


class ReceiptSectionTests(TenantTestCase):
    """收款节：`XgjReceiptRecord.order_no`（= 推送的闲鱼单号、与我方来源号同源）
    双键接入；金额可空照实写「待回查」、店铺快照 / 标识取非空者、匹配结果人话走
    `xgj_receipt_stats.match_summary_text` 唯一出处；`raw_push` 不外泄。
    写形实证：`xgj_push` 的 22 推送记账段（update_or_create 按 `(app_key, order_no)`）。
    夹具一律合成值（E-0077）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client_a = _grant_node_and_login("tenant.test.com", USERNAME_A)

    def _make_receipt(self, order_no, *, amount=None, shop_name="", shop_identity="",
                      matched_count=0, match_result=None, success_time=None):
        """照推送记账行的落库形状造一行（模型层写入口；字段语义读自模型 docstring）。"""
        return XgjReceiptRecord.objects.create(
            app_key=RECEIPT_APP_KEY,
            order_no=order_no,
            shop_name_snapshot=shop_name,
            shop_identity=shop_identity,
            amount=amount,
            success_time=success_time,
            matched_count=matched_count,
            match_result=match_result if match_result is not None else [],
        )

    def test_receipt_section_matches_by_source_order_no(self):
        """①有来源号的单 + 匹配来源号的收款行 ⇒ receipt 节非空、summary 含金额与店铺；
        异单号的行不混进来；`raw_push` 不出泄露面。"""
        order = _make_order(NO_A, source_order_no=SRC_A)
        self._make_receipt(
            SRC_A,
            amount=Decimal("88.00"),
            shop_name=SHOP_SNAPSHOT_A,
            success_time=timezone.now(),
            matched_count=1,
            match_result=[{"order_no": NO_A, "result": "updated"}],
        )
        # 负：同 app_key、别的单号（双键收窄判据——只按 app_key 收会把它拉进来）
        self._make_receipt(PEER_NO_OTHER, amount=Decimal("1.00"))
        _code, _msg, data = _timeline(self.client_a, NO_A)
        receipt = data["sections"][6]
        self.assertEqual(receipt["key"], "receipt")
        self.assertEqual(receipt["label"], "收款记录")
        items = receipt["items"]
        self.assertEqual(len(items), 1, "异单号的收款行混进来了")
        item = items[0]
        self.assertEqual(item["kind"], "xgj.receipt")
        self.assertIn("88.00", item["summary"])
        self.assertIn(SHOP_SNAPSHOT_A, item["summary"])
        self.assertIn("已改已结款", item["summary"])
        self.assertIsNotNone(item["at"])
        self.assertEqual(item["matched_count"], 1)
        # 推送原文不外泄（只进 summary 归纳）
        self.assertNotIn("raw_push", item)

    def test_receipt_section_matches_by_order_no_and_falls_back_on_shop_and_amount(self):
        """②无来源号的单按内置号也查得到；金额待回查、店铺回落标识（取非空者）。"""
        order = _make_order(NO_A)  # 来源号为空
        self._make_receipt(order.order_no, shop_identity=SHOP_EID_B)
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][6]["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("待回查", items[0]["summary"])
        self.assertIn(SHOP_EID_B, items[0]["summary"])
        self.assertIn("未匹配到系统订单", items[0]["summary"])
        self.assertEqual(items[0]["amount"], "")

    def test_receipt_section_empty_without_matching_rows(self):
        """③无匹配 ⇒ 空节照出（items=[]、truncated=false）。"""
        _make_order(NO_A, source_order_no=SRC_A)
        # 别人的收款行在库，但不属于这张单
        self._make_receipt("完全无关的单号", amount=Decimal("9.99"))
        _code, _msg, data = _timeline(self.client_a, NO_A)
        receipt = data["sections"][6]
        self.assertEqual(receipt["key"], "receipt")
        self.assertEqual(receipt["items"], [])
        self.assertFalse(receipt["truncated"])
        # 阳性对照：那行确实在库里（不是写丢了）
        self.assertTrue(
            XgjReceiptRecord.objects.filter(order_no="完全无关的单号").exists()
        )

    def test_receipt_section_dual_key_same_string_yields_row_once(self):
        """④病态 `source_order_no == order_no`：两键同串不出重行。"""
        _make_order(NO_A, source_order_no=NO_A)  # 来源号与内置号同串（病态夹具）
        self._make_receipt(NO_A, amount=Decimal("66.00"))
        _code, _msg, data = _timeline(self.client_a, NO_A)
        items = data["sections"][6]["items"]
        self.assertEqual(len(items), 1, "双号同串的收款行出现两次 = 双键没去重")
        self.assertIn("66.00", items[0]["summary"])


# ═══════════════════════════════════════════════════════════════════════
# 权限门与熔断健康条判定


class TimelinePermissionTests(TenantTestCase):
    """无节点 403（传输层码，fail-closed）；有节点 200。"""

    def test_without_node_is_403_and_with_node_is_ok(self):
        _make_order(NO_A)
        denied = make_logged_in_client(username="timeline-no-node")
        response = denied.get(ENDPOINT, {"key": NO_A})
        self.assertEqual(response.status_code, 403, response.content[:300])
        allowed = _grant_node_and_login("tenant.test.com", USERNAME_A)
        code, _msg, data = _timeline(allowed, NO_A)
        self.assertEqual(code, 0)
        self.assertIsNotNone(data)


class BreakerHealthTests(TenantTestCase):
    """`readback_breaker_health` 的两级查询（§1.4）：挂起判据与 12h 窗。"""

    def _make_binding(self, username):

        user = get_user_model().objects.create_user(
            username=username, password="合成密码-熔断"
        )
        return JidanpuMemberBinding.objects.create(
            user=user, jidanpu_base_url="https://breaker.example.test",
            account=f"breaker-{username}", password="合成密码-熔断", enabled=True,
        )

    def _write_note(self, binding, *, created_at=None):
        """造一条熔断台账 note（模型层夹具；字面值照 `_write_launch_breaker_note` 的行形状）。"""
        row = JidanpuSyncEvent.objects.create(
            direction="out", kind="member.launch_breaker_open", status="skipped",
            target_type="binding", target_ref=str(binding.pk),
            reason="合成原因：回读失败已达 20 次",
            payload={"member_binding_id": binding.pk, "probe": False},
        )
        if created_at is not None:
            # `created_at` 是 auto_now_add ⇒ 显式时刻要走 update 绕开（标准手法）。
            JidanpuSyncEvent.objects.filter(pk=row.pk).update(created_at=created_at)
        return row

    def _write_failed_launches(self, binding, count):
        prefix = LAUNCH_READBACK_FAILURE_REASONS[0]
        for i in range(count):
            JidanpuSyncEvent.objects.create(
                direction="out", kind="member.launch", status="failed",
                target_type="order", target_ref="20261101000000000099",
                reason=f"{prefix}（合成第 {i} 发）",
                payload={"member_binding_id": binding.pk},
            )

    def test_no_note_means_not_suspended_even_with_twenty_failures(self):
        """两级查询的第一级：12h 窗内没有 note ⇒ 不算完整 state（健康店零第二跳）。"""
        binding = self._make_binding("breaker-user-1")
        self._write_failed_launches(binding, 20)
        suspended, _detail = readback_breaker_health(binding, now=timezone.now())
        self.assertFalse(suspended)

    def test_note_within_window_with_twenty_failures_is_suspended(self):
        binding = self._make_binding("breaker-user-2")
        self._write_failed_launches(binding, 20)
        self._write_note(binding, created_at=timezone.now())
        suspended, detail = readback_breaker_health(binding, now=timezone.now())
        self.assertTrue(suspended)
        self.assertIn("20", detail)

    def test_old_note_beyond_twelve_hours_is_not_in_view(self):
        """窗外的 note 对判定不可见（「隔了很久又坏」按全新一轮重新判定，§1.4 口径）。"""
        binding = self._make_binding("breaker-user-3")
        self._write_note(binding, created_at=timezone.now() - timezone.timedelta(hours=13))
        suspended, _detail = readback_breaker_health(binding, now=timezone.now())
        self.assertFalse(suspended)

    def test_breakers_endpoint_lists_only_enabled_bindings(self):
        enabled = self._make_binding("breaker-user-4")
        disabled = self._make_binding("breaker-user-5")
        JidanpuMemberBinding.objects.filter(pk=disabled.pk).update(enabled=False)
        self._write_note(enabled, created_at=timezone.now())
        self._write_note(disabled, created_at=timezone.now())
        client = _grant_node_and_login("tenant.test.com", USERNAME_A)
        response = client.get(BREAKERS_ENDPOINT)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        stores = [entry["store"] for entry in body["data"]]
        self.assertIn(enabled.account, stores)
        self.assertNotIn(disabled.account, stores)
        entry = next(e for e in body["data"] if e["store"] == enabled.account)
        self.assertIn("suspended", entry)


# ═══════════════════════════════════════════════════════════════════════
# 跨租户隔离（AGENTS.md §4 硬性；照 test_operation_log_isolation.py 的纪律）

SCHEMA_B = "p13_tl_b"
DOMAIN_B = "p13-tl-b.test.local"


class TimelineCrossTenantIsolationTests(TenantTestCase):
    """以 A 的凭据查 B 的单**必须查无**，且与「key 缺失」同形同码（双向 + 阳性对照）。

    判据用**租户内唯一、且两租户取值不同**的业务值：两租户共用同一个
    `order_no`（订单号只有租户内唯一），各自来源号标记不同 ⇒ 按「来源号标记 +
    订单号」指认行，⛔ 不用主键。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="时间线隔离租户 B", domain=DOMAIN_B
        )

    def test_tenant_a_cannot_read_tenant_b_order_and_shapes_match_not_found(self):
        # B 里造两张：它独有订单号的一张 + **与 A 同号**的一张（各带 B 的来源号标记）
        with tenant_context(self.tenant_b):
            _make_order(NO_B_ONLY, source_order_no=SRC_MARK_B)
            _make_log(NO_B_ONLY, summary="只有 B 可见的时间线")
            _make_order(SHARED_NO, source_order_no=SRC_MARK_B)
            _make_log(SHARED_NO, summary="B 的同号单时间线")
        # A 里造同号订单（A 的来源号标记）⇒ 阳性对照：A 自己的可查、标记是 A 的
        with tenant_context(self.tenant_a):
            _make_order(SHARED_NO, source_order_no=SRC_MARK_A)
            _make_log(SHARED_NO, summary="只有 A 可见的时间线")
            client_a = _grant_node_and_login("tenant.test.com", USERNAME_A)
            code, msg, data = _timeline(client_a, SHARED_NO)
            self.assertEqual(code, 0)
            self.assertEqual(data["order"]["source_order_no"], SRC_MARK_A)
            self.assertEqual(
                data["sections"][0]["items"][0]["summary"], "只有 A 可见的时间线"
            )
            # 判据本体：A 查 B 的独有单号 ⇒ 与「查无」同形同码（防探测）
            code, msg, data = _timeline(client_a, NO_B_ONLY)
            self.assertEqual(code, NOT_FOUND_CODE)
            self.assertEqual(msg, NOT_FOUND_MSG)
            self.assertIsNone(data)
        # 换租户验证（反向）：B 查同号单拿到的是 B 自己的标记、查 A 里独有的……
        # ⚠️ SHARED_NO 在 A、B 都存在（同号不同源）⇒ 各查各的；A 侧独有标记的单号
        # 用 SRC_MARK_A 反查：A 命中、B 查无（同形同码）。
        with tenant_context(self.tenant_b):
            client_b = _grant_node_and_login(DOMAIN_B, USERNAME_B)
            code, _msg, data = _timeline(client_b, NO_B_ONLY)
            self.assertEqual(code, 0)
            self.assertEqual(data["order"]["source_order_no"], SRC_MARK_B)
            code, _msg, data = _timeline(client_b, SHARED_NO)
            self.assertEqual(code, 0)
            self.assertEqual(data["order"]["source_order_no"], SRC_MARK_B)
            self.assertEqual(
                data["sections"][0]["items"][0]["summary"], "B 的同号单时间线"
            )
        # 数据层收口：A 的 schema 里查不到 B 的订单号（照隔离测试的两半口径）
        with tenant_context(self.tenant_a):
            self.assertFalse(Order.all_objects.filter(order_no=NO_B_ONLY).exists())
            self.assertTrue(Order.all_objects.filter(order_no=SHARED_NO).exists())

    def test_breakers_endpoint_is_tenant_scoped(self):
        """熔断健康条同样只在调用者租户里：B 的绑定不出现在 A 的列表里。"""

        with tenant_context(self.tenant_b):
            user = get_user_model().objects.create_user(
                username="b-breaker", password="合成密码-跨租户"
            )
            JidanpuMemberBinding.objects.create(
                user=user, jidanpu_base_url="https://b.example.test",
                account="仅 B 的合成店铺", password="合成密码-跨租户", enabled=True,
            )
        with tenant_context(self.tenant_a):
            user = get_user_model().objects.create_user(
                username="a-breaker", password="合成密码-跨租户"
            )
            JidanpuMemberBinding.objects.create(
                user=user, jidanpu_base_url="https://a.example.test",
                account="仅 A 的合成店铺", password="合成密码-跨租户", enabled=True,
            )
            client_a = _grant_node_and_login("tenant.test.com", USERNAME_A)
            response = client_a.get(BREAKERS_ENDPOINT)
            self.assertEqual(response.status_code, 200, response.content[:300])
            stores = [e["store"] for e in response.json()["data"]]
            self.assertIn("仅 A 的合成店铺", stores)
            self.assertNotIn("仅 B 的合成店铺", stores)
