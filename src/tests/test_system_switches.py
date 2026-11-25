"""系统设置开关（D-0223，2026-10-10）：模型口径 + 注册表 + 两个端点 + 闸的三条边界。

本文件证什么（每条都能独立地红）：

1. **开关 API**（照 `tests/test_recognition_settings_api.py` 的形状）：GET 形状
   （注册表默认 ∪ 覆盖行）、PATCH 改 Y/N、未知 key / 非对象体 / 非法值一律业务码 1000
   （⛔ 不是 500）、无节点账号 GET/PATCH 都 403（fail-closed）、PATCH 成功写**数据库审计行**
   （⛔ 不断言 logger，E-0009）、纯 no-op 不写审计行。
2. **数据迁移 `0007_add_system_edit_settings_node`**：冻结副本与 `catalog.NODES`
   那一行逐字同值；在"没有该节点"的租户上真把点建出来；只补不改 / 不碰角色授权；
   重跑安全（照 `TakerOpenApiNodeMigrationTests` 的口径）。
3. **闸的租户边界**（`AGENTS.md` §4 硬规则）：租户 A 开开关且已有号 X；租户 B（也开开关）
   建 X ⇒ **成功**；A 侧再建 X 仍被拦。判据用租户内唯一的**来源单号串**，⛔ 不用主键
   （两个 schema 的主键序列各自从 1 开始，`AGENTS.md` §5-G）。
4. **路径豁免**（业务方裁定「只拦手工」）：开关开着时——开放平台建单带重复
   `source_order_no` ⇒ 照建；开放平台改单把 `source_order_no` 改成已用号 ⇒ 照改；
   peer 入站建单带重复号 ⇒ 照建（HTTP 级，照 `tests/test_peer_binding_api.py` 的
   `E2E_INBOUND_URL` 先例走真 URL——连认证与中间件那层一起证）。

⚠️ 录单闸与编辑闸的**手工路径**用例在 `tests/test_order_api.py`
（`DuplicateSourceOrderNoGuardTests`，复用那份夹具）。

夹具纪律：来源单号 / 凭据 / 游戏名一律**合成值**（`AGENTS.md` §5-C，E-0077）；
跨域名请求结束后把连接钉回租户 A（`TenantMainMiddleware` 不还原 schema）。
"""
import importlib
import json

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import (
    Game,
    Order,
    PeerUpstreamBinding,
    SystemSwitch,
)
from apps.business.peer_sync import hash_token, new_token
from apps.business.system_switches import (
    SWITCHES,
    switch_enabled,
    switch_rows,
)
from apps.openapi import tokens
from apps.openapi.models import ApiApp
from apps.platform.provisioning import provision_tenant
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode, Role
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

SWITCHES_ENDPOINT = "/api/business/system-switches/"
#: 首条开关的 key（与注册表同源；断言里另写字面值钉住它）。
SWITCH_KEY = "forbid_duplicate_source_order_no"
#: ⭐ 线 F（业务方）新增的第二条开关：闲管家录单识别拒「待付款」，**默认开**。
UNPAID_SWITCH_KEY = "reject_unpaid_xgj_order"
#: 注册表顺序（= `switch_rows()` / 列表端点的出参顺序；新开关**追加在末尾**）。
#: ⭐ 第三条（`feat/wallet-single-payout`）：押金提现 T+7 自动代付，**默认开**。
AUTO_PAYOUT_SWITCH_KEY = "WITHDRAW_AUTO_PAYOUT"
#: ⭐ 第四条（D-0296）：接单大厅模拟单，**默认关**。
HALL_SIM_SWITCH_KEY = "hall_simulated_orders"
#: ⭐ 第五条（D-0328）：接单大厅优选订单，**默认开**（存量行为）。
PREFERRED_SWITCH_KEY = "hall_preferred_orders"
#: ⛔ 曾有第六条（D-0329）`hall_upstream_throw`——业务方拍板
#: 「同步订单是一整个功能」**整条撤除**（功能随发版自动生效，闸改常量水位、见
#: `upstream_hall_shadow.UPSTREAM_THROW_SINCE`）。注册表恰好集合回五条；这条 key
#: 现在是**未知 key**（`switch_enabled` / PATCH 它都该响亮报错）。
REMOVED_THROW_SWITCH_KEY = "hall_upstream_throw"
AUTO_REFUND_SWITCH_KEY = "auto_refund_platform_order"
EXPECTED_SWITCH_ORDER = [
    SWITCH_KEY, UNPAID_SWITCH_KEY, AUTO_PAYOUT_SWITCH_KEY, HALL_SIM_SWITCH_KEY,
    PREFERRED_SWITCH_KEY, AUTO_REFUND_SWITCH_KEY,
]

#: 租户 A = `TenantTestCase` 的 `test` schema（与既有隔离测试同口径）。
DOMAIN_A = "tenant.test.com"
SCHEMA_B = "sys_switch_b"
DOMAIN_B = "sys-switch-b.test.local"

#: 合成的重复来源单号（租户边界用例里 A / B 都用它——同号不同库，正是要证的边界）。
DUP_NO = "TB-合成-重复边界-0001"

#: 迁移 `0007` 的模块（冻结副本对拍用；照 `_CreateFixture` / `TakerOpenApiNodeMigrationTests` 的手法）。
MIGRATION_0007 = importlib.import_module(
    "apps.rbac.migrations.0007_add_system_edit_settings_node"
)


def switch_url(key=SWITCH_KEY):
    return f"{SWITCHES_ENDPOINT}{key}/"


def _grant_nodes_and_login(domain, username, codes=("system.settings", "system.edit_settings")):
    """在当前 schema 里建本线要的节点 + 角色，返回**真实登录**的 client。

    `get_or_create`（不是 `create`）：`TenantTestCase` 的 `test` schema 只跑迁移——
    `system.edit_settings` 已由数据迁移 `0007` 建好（按 `code` 命中）；而 `system.settings`
    （benchmark 91）没有种子 ⇒ 走 create 那一支。B 租户由 `provision_tenant` 建、
    `seed_tenant` 灌过完整目录 ⇒ 两个点都已存在（benchmark_id=91 那条按 id 命中）。
    """
    catalog = {
        "system.settings": (
            91, "系统设置", "系统设置", "/System/SystemSetting", NodeKind.PAGE
        ),
        "system.edit_settings": (
            None, "编辑系统设置", "编辑系统设置里的开关（保存设置）",
            "/SystemAjax/SystemOrderSetting", NodeKind.API,
        ),
    }
    nodes = []
    for code in codes:
        benchmark_id, name, note, url, kind = catalog[code]
        node, _ = PermissionNode.objects.get_or_create(
            code=code,
            defaults={
                "benchmark_id": benchmark_id, "name": name, "note": note, "url": url,
                "kind": kind, "level": 1,
            },
        )
        # 自证拿到的是**这一条**节点（benchmark_id 撞上别的 code 时 get_or_create 会静默
        # 返回错节点，症状会指向「权限没给对」而不是「拿错了节点」）。
        assert node.code == code, f"拿到的是错节点：{node.code!r} ≠ {code!r}"
        nodes.append(node)
    client = make_logged_in_client(domain=domain, username=username)
    role_name = "开关管理"  # ⚠️ `Role.name` 列宽 5，超长会 DataError。
    # `get_or_create`：同一条用例里本 helper 会被调多次（权限用例造第二、三个账号），
    # 硬 create 会撞 `auth_group` 的唯一约束。
    group, _ = Group.objects.get_or_create(name=role_name)
    role, _ = Role.objects.get_or_create(name=role_name, defaults={"group": group})
    role.permissions.set(nodes)
    User.objects.get(username=username).groups.add(role.group)
    return client


class SystemSwitchModelTests(TenantTestCase):
    """`SystemSwitch` 的字段口径（与 `RecognitionKeyword` 同形）。"""

    def test_key_is_unique(self):
        SystemSwitch.objects.create(key="a_switch", value="Y")
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SystemSwitch.objects.create(key="a_switch", value="N")

    def test_value_column_width_is_8(self):
        """列宽 8（只装 `"Y"` / `"N"`，宽度余量是给将来三态留的，不是让长串进来）。"""
        self.assertEqual(SystemSwitch._meta.get_field("value").max_length, 8)


class SwitchRegistryTests(TenantTestCase):
    """注册表与读取口（`switch_enabled` / `switch_rows`）。"""

    def test_registry_has_exactly_the_ruled_switches_with_their_values(self):
        """注册表里**恰好**是 `EXPECTED_SWITCH_ORDER` 那几条，五个字段逐字 = 各自的裁定。

        ⚠️ 「恰好几条」是**故意**写死的（⛔ 不是 `toContain`）：注册表被悄悄加/减一条时，
        上面那条列表端点用例与这条一起红。新开关必须**同一次**改 `EXPECTED_SWITCH_ORDER`
        并把它的裁定写进本用例——⛔ 不许把它放宽成「至少包含」。
        """
        self.assertEqual(set(SWITCHES), set(EXPECTED_SWITCH_ORDER))
        switch = SWITCHES[SWITCH_KEY]
        self.assertEqual(switch.key, SWITCH_KEY)
        self.assertEqual(switch.name, "录单时禁止录入相同的来源单号")
        self.assertEqual(switch.default, "N")
        self.assertEqual(switch.replica_sk, "SK32")
        # 说明里必须带「空号不拦 / 回收站不占号 / 只拦手工」三条口径（登记自查）。
        for phrase in ("空来源单号", "回收站", "开放平台"):
            self.assertIn(phrase, switch.description)

        # ── 线 F 的第二条（业务方）────────────────────────
        unpaid = SWITCHES[UNPAID_SWITCH_KEY]
        self.assertEqual(unpaid.key, UNPAID_SWITCH_KEY)
        self.assertEqual(unpaid.name, "录单识别闲管家订单时，待付款状态直接拒绝")
        # **默认开**：业务方原话是「应该拒绝」，开关只是给他关掉用的。
        self.assertEqual(unpaid.default, "Y")
        # 自有开关：**对标物里没有这一项** ⇒ 没有可填的 SK 编号（空串，⛔ 不是漏填）。
        self.assertEqual(unpaid.replica_sk, "")
        # 说明里必须带三条口径：只拦明确的待付款（fail-open）、上游没给就放行、阿奇索不受管。
        for phrase in ("待付款", "未知值", "阿奇索"):
            self.assertIn(phrase, unpaid.description)

        # ── 第三条（`feat/wallet-single-payout`）：自动代付，**默认开** ──────
        payout = SWITCHES[AUTO_PAYOUT_SWITCH_KEY]
        self.assertEqual(payout.key, AUTO_PAYOUT_SWITCH_KEY)
        # 业务方追加口径：「开关开就行了」（Jeepay 转账已实测通过）。
        # ⚠️ 这是**临时口径**（验收完可以关）——关掉时命令一条都不发，判据见
        # `tests/test_withdraw_payout_command.py::AutoPayoutCommandTests::test_the_switch_off_sends_nothing`。
        self.assertEqual(payout.default, "Y")
        # 自有开关：对标物没有对应项 ⇒ 空串（⛔ 不是漏填）。
        self.assertEqual(payout.replica_sk, "")
        for phrase in ("WITHDRAW_PAYOUT_DELAY_MINUTES", "一条都不发"):
            self.assertIn(phrase, payout.description)

        # ── 第四条（D-0296）：大厅模拟单，**默认关** ──────────────
        sim = SWITCHES[HALL_SIM_SWITCH_KEY]
        self.assertEqual(sim.key, HALL_SIM_SWITCH_KEY)
        # 生成是显式动作（模拟卡会改写大厅可见内容）⇒ 默认 N，不是默认行为。
        self.assertEqual(sim.default, "N")
        # 自有开关：对标物没有对应项 ⇒ 空串（⛔ 不是漏填）。
        self.assertEqual(sim.replica_sk, "")
        # 说明里必须带四条口径：节奏锚 / 每游戏张数 / 零客户信息 / 不建订单行。
        for phrase in ("2 小时", "2~3", "零客户信息", "不建订单行"):
            self.assertIn(phrase, sim.description)

        # ── 第五条（D-0328）：大厅优选订单，**默认开** ──────────────
        preferred = SWITCHES[PREFERRED_SWITCH_KEY]
        self.assertEqual(preferred.key, PREFERRED_SWITCH_KEY)
        # 业务方的指令是「把优选关掉」⇒ 开关是给他关用的；
        # 默认 Y = 存量行为一字不变（现有租户不登记任何行，优选照旧）。
        self.assertEqual(preferred.default, "Y")
        # 自有开关：对标物没有对应项 ⇒ 空串（⛔ 不是漏填）。
        self.assertEqual(preferred.replica_sk, "")
        # 说明里必须把**两向语义**写清：开 = 角标 + 完成数闸 + 优选强制押金；
        # 关 = 全部行不标优选、接单跳过这两道、高价单回归普通规则。
        for phrase in ("> 200", "完成过订单", "强制冻结押金", "不再标优选",
                       "跳过优选完成数闸与优选强制押金", "普通押金闸", "模拟单生成不受影响"):
            self.assertIn(phrase, preferred.description)

        # ── ⛔ 第六条已撤（改准 D-0329）：`hall_upstream_throw` 不在注册表 ──
        self.assertNotIn(REMOVED_THROW_SWITCH_KEY, SWITCHES)

        auto_refund = SWITCHES[AUTO_REFUND_SWITCH_KEY]
        self.assertEqual(auto_refund.name, "平台退款后自动退单")
        self.assertEqual(auto_refund.default, "Y")
        self.assertEqual(auto_refund.replica_sk, "")

    def test_removed_throw_switch_key_is_an_unknown_key_now(self):
        """撤除的 `hall_upstream_throw` 是**未知 key**：读取口响亮炸（`KeyError`）。

        ⛔ 不是「当关」——静默回 False 会把「key 打错」伪装成「开关永远关」的假行为
        （与 `switch_enabled` 对未知 key 的既定口径同一条）。
        """
        with self.assertRaises(KeyError):
            switch_enabled(REMOVED_THROW_SWITCH_KEY)

    def test_switch_rows_keeps_the_registry_order(self):
        """列表端点的顺序 = 注册表顺序（新开关追加在末尾，⛔ 不是插到中间）。"""
        self.assertEqual([row["key"] for row in switch_rows()], EXPECTED_SWITCH_ORDER)

    def test_switch_enabled_defaults_to_true_for_the_unpaid_guard_without_an_override_row(self):
        """无覆盖行 = 注册表默认 `Y` ⇒ **开**（线 F 的承重口径：默认就拒）。"""
        self.assertFalse(SystemSwitch.objects.exists())
        self.assertTrue(switch_enabled(UNPAID_SWITCH_KEY))

    def test_switch_enabled_reads_the_unpaid_guard_override_on_the_current_connection(self):
        """覆盖成 `N` ⇒ 关（业务方要的那个开关真的能关掉）。"""
        SystemSwitch.objects.create(key=UNPAID_SWITCH_KEY, value="N")
        self.assertFalse(switch_enabled(UNPAID_SWITCH_KEY))
        SystemSwitch.objects.filter(key=UNPAID_SWITCH_KEY).update(value="Y")
        self.assertTrue(switch_enabled(UNPAID_SWITCH_KEY))

    def test_switch_rows_fails_closed_on_a_dirty_value_of_a_default_y_switch(self):
        """脏值在列表端点也**当关**（`"N"`），与 `switch_enabled` 同判。

        ⚠️ 这是**防御性不变式、今天不可达**（脏值要直接改库才造得出来，API 只收 `"Y"`/`"N"`）；
        钉它是因为「列表端点与闸读同一个值」这条不变式一旦破了，页面会显示一个与闸不一致的勾选态。
        ⛔ 别把它读成"线上会出事"（评审 2026-10-15 的措辞更正）。
        """
        SystemSwitch.objects.create(key=UNPAID_SWITCH_KEY, value="也许")
        row = {r["key"]: r for r in switch_rows()}[UNPAID_SWITCH_KEY]
        self.assertEqual(row["value"], "N")
        self.assertEqual(row["default"], "Y")
        # 与闸同判：脏值 ⇒ 关。
        self.assertFalse(switch_enabled(UNPAID_SWITCH_KEY))

    def test_switch_enabled_defaults_to_false_without_an_override_row(self):
        """无覆盖行 = 注册表默认 `N` ⇒ 关（默认允许重复，业务方第 1 条）。"""
        self.assertFalse(SystemSwitch.objects.exists())
        self.assertFalse(switch_enabled(SWITCH_KEY))

    def test_switch_enabled_reads_the_override_on_the_current_connection(self):
        SystemSwitch.objects.create(key=SWITCH_KEY, value="Y")
        self.assertTrue(switch_enabled(SWITCH_KEY))
        # 覆盖回 N ⇒ 关。
        SystemSwitch.objects.filter(key=SWITCH_KEY).update(value="N")
        self.assertFalse(switch_enabled(SWITCH_KEY))

    def test_switch_enabled_fails_closed_on_a_dirty_value(self):
        """库里的脏值（理论上 API 已挡）一律当关——fail-closed，不是崩。"""
        SystemSwitch.objects.create(key=SWITCH_KEY, value="也许")
        self.assertFalse(switch_enabled(SWITCH_KEY))

    def test_switch_state_returns_default_judgment_and_none_watermark_without_a_row(self):
        """无覆盖行 ⇒ `(默认判, None)`——默认 N 的开关「开着且无行」不可达。

        ⚠️ D-0329 的水位消费方（upstream_hall_shadow 的开关闸）已于 2026-10-29 撤除；
        本用例保留钉的是「一条查询拿两样」这个形状本身（`switch_enabled` 委托它）。
        """
        from apps.business.system_switches import switch_state

        self.assertFalse(SystemSwitch.objects.exists())
        enabled, opened_at = switch_state(HALL_SIM_SWITCH_KEY)
        self.assertFalse(enabled)
        self.assertIsNone(opened_at)

    def test_switch_state_returns_the_row_value_and_its_updated_at(self):
        """有覆盖行 ⇒ 一条查询同时拿 `(值, 行的 updated_at)`；值翻向两个口一起翻。

        ⚠️ `updated_at` 是 `auto_now`，`create` 后它是「刚那一刻」；要钉「回的就是
        **那一列**」，用 `.update()` 绕过 auto_now 写一个已知时刻再读（判据钉值）。
        """
        from datetime import datetime
        from datetime import timezone as dt_timezone

        from apps.business.system_switches import switch_state

        SystemSwitch.objects.create(key=HALL_SIM_SWITCH_KEY, value="Y")
        known = datetime(2026, 10, 28, 8, 30, tzinfo=dt_timezone.utc)
        SystemSwitch.objects.filter(key=HALL_SIM_SWITCH_KEY).update(updated_at=known)
        enabled, opened_at = switch_state(HALL_SIM_SWITCH_KEY)
        self.assertTrue(enabled)
        self.assertEqual(opened_at, known)
        # 值判据与 `switch_enabled` 同源（委托同一行读）⇒ 关着时两个口一起翻。
        SystemSwitch.objects.filter(key=HALL_SIM_SWITCH_KEY).update(value="N")
        enabled, opened_at = switch_state(HALL_SIM_SWITCH_KEY)
        self.assertFalse(enabled)
        self.assertEqual(opened_at, known)
        self.assertFalse(switch_enabled(HALL_SIM_SWITCH_KEY))

    def test_switch_rows_unions_registry_and_override(self):
        body = {row["key"]: row for row in switch_rows()}
        self.assertEqual(
            body[SWITCH_KEY],
            {
                "key": SWITCH_KEY,
                "name": "录单时禁止录入相同的来源单号",
                "value": "N",
                "default": "N",
                "replica_sk": "SK32",
            },
        )
        SystemSwitch.objects.create(key=SWITCH_KEY, value="Y")
        body = {row["key"]: row for row in switch_rows()}
        self.assertEqual(body[SWITCH_KEY]["value"], "Y")
        self.assertEqual(body[SWITCH_KEY]["default"], "N")

        # ── 线 F 那条：无覆盖行 ⇒ 值 = 默认 `"Y"`（与首条相反，别照抄首条的期望值）──
        self.assertNotIn(
            UNPAID_SWITCH_KEY, SystemSwitch.objects.values_list("key", flat=True)
        )
        self.assertEqual(
            body[UNPAID_SWITCH_KEY],
            {
                "key": UNPAID_SWITCH_KEY,
                "name": "录单识别闲管家订单时，待付款状态直接拒绝",
                "value": "Y",
                "default": "Y",
                "replica_sk": "",
            },
        )
        # 覆盖成 `"N"` ⇒ 行里的值跟着变，`default` 不动（页面靠 value 渲染勾选态）。
        SystemSwitch.objects.create(key=UNPAID_SWITCH_KEY, value="N")
        row = {r["key"]: r for r in switch_rows()}[UNPAID_SWITCH_KEY]
        self.assertEqual(row["value"], "N")
        self.assertEqual(row["default"], "Y")


class SystemSwitchApiTests(TenantTestCase):
    """GET / PATCH 两端点：形状、校验、权限、留痕。

    夹具照 `test_recognition_settings_api.py`：只建本线要的节点（不 `seed_catalog()`），
    再给一个真实登录态挂上它们。
    """

    def setUp(self):
        self.client = _grant_nodes_and_login(DOMAIN_A, "switch-admin")

    def _get(self):
        response = self.client.get(SWITCHES_ENDPOINT)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def _patch(self, payload, key=SWITCH_KEY, raw=None):
        if raw is not None:
            return self.client.patch(switch_url(key), raw, content_type="application/json")
        return self.client.patch(
            switch_url(key), payload, content_type="application/json"
        )

    # ── GET：注册表默认 ∪ 覆盖行 ────────────────────────────────────
    def test_list_returns_the_registry_defaults(self):
        items = self._get()["data"]["items"]
        # 「恰好几条」是**故意**写死的（⛔ 不是 `toContain`）：注册表加/减一条时这条要红。
        self.assertEqual(len(items), len(EXPECTED_SWITCH_ORDER))
        self.assertEqual([i["key"] for i in items], EXPECTED_SWITCH_ORDER)
        self.assertEqual(items[0]["key"], SWITCH_KEY)
        self.assertEqual(items[0]["value"], "N")
        self.assertEqual(items[0]["replica_sk"], "SK32")
        # 线 F 那条：**默认值就是 `"Y"`**（列表端点出参的口径，与 `switch_enabled` 同判）。
        self.assertEqual(items[1]["key"], UNPAID_SWITCH_KEY)
        self.assertEqual(items[1]["name"], "录单识别闲管家订单时，待付款状态直接拒绝")
        self.assertEqual(items[1]["value"], "Y")
        self.assertEqual(items[1]["default"], "Y")
        self.assertEqual(items[1]["replica_sk"], "")
        # 第三条（自动代付）：列表端点默认值同样是 `"Y"`（与 `switch_enabled` 同判）。
        self.assertEqual(items[2]["key"], AUTO_PAYOUT_SWITCH_KEY)
        self.assertEqual(items[2]["value"], "Y")
        self.assertEqual(items[2]["default"], "Y")
        self.assertEqual(items[2]["replica_sk"], "")
        # 第五条（D-0328，优选订单）：列表端点默认 `"Y"`（存量行为）。
        self.assertEqual(items[4]["key"], PREFERRED_SWITCH_KEY)
        self.assertEqual(items[4]["value"], "Y")
        self.assertEqual(items[4]["default"], "Y")
        self.assertEqual(items[4]["replica_sk"], "")
        # ⛔ 第六条（D-0329 的 hall_upstream_throw）已撤（改准）：
        #    列表里不该再有它——恰好集合断言在上面那条 len / 顺序里钉。

    def test_patch_saves_the_unpaid_guard_off(self):
        """业务方要的那个开关**真的能关**：PATCH 成 `N` 后落库、读回、`switch_enabled` 变假。"""
        response = self._patch({"value": "N"}, key=UNPAID_SWITCH_KEY)
        self.assertEqual(response.json()["code"], 0, response.content[:300])
        self.assertEqual(SystemSwitch.objects.get(key=UNPAID_SWITCH_KEY).value, "N")
        self.assertFalse(switch_enabled(UNPAID_SWITCH_KEY))
        items = self._get()["data"]["items"]
        self.assertEqual(items[1]["value"], "N")

    def test_list_returns_the_override_after_a_patch(self):
        self._patch({"value": "Y"})
        items = self._get()["data"]["items"]
        self.assertEqual(items[0]["value"], "Y")
        self.assertEqual(SystemSwitch.objects.get(key=SWITCH_KEY).value, "Y")

    # ── PATCH：Y / N 都能落库；回保存后的行 ─────────────────────────
    def test_patch_saves_y_and_then_n(self):
        self.assertEqual(self._patch({"value": "Y"}).json()["code"], 0)
        self.assertEqual(SystemSwitch.objects.get(key=SWITCH_KEY).value, "Y")
        self.assertEqual(self._patch({"value": "N"}).json()["code"], 0)
        self.assertEqual(SystemSwitch.objects.get(key=SWITCH_KEY).value, "N")

    def test_patch_returns_the_saved_row(self):
        self.assertEqual(
            self._patch({"value": "Y"}).json()["data"],
            {"key": SWITCH_KEY, "name": "录单时禁止录入相同的来源单号", "value": "Y"},
        )

    # ── 校验：一律业务码 1000（不是 404、不是 500）──────────────────
    def test_unknown_key_is_a_business_error(self):
        response = self._patch({"value": "Y"}, key="not-a-switch")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_invalid_value_is_a_business_error(self):
        for value in ("X", "", "y", 1, None):
            with self.subTest(value=value):
                response = self._patch({"value": value})
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(response.json()["code"], 1000, response.content[:300])

    def test_missing_value_key_is_a_business_error(self):
        response = self._patch({})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_non_object_json_bodies_are_a_business_error_not_a_500(self):
        """请求体不是 JSON 对象（数组 / 字符串 / 数字）⇒ 200 + `code=1000`（E-0010 同族）。

        ⚠️ 本路由带 `<str:key>` 转换器 ⇒ `tests/test_no_route_500.py` 会跳过它，
        这条用例是它唯一的网（与 recognition 那条同一条理由）。
        """
        self.client.raise_request_exception = False
        for raw in ('[1, 2, 3]', '"just-a-string"', "123"):
            with self.subTest(body=raw):
                response = self._patch(None, raw=raw)
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(response.json()["code"], 1000, response.content[:300])

    # ── 权限：fail-closed ──────────────────────────────────────────
    def test_endpoints_require_the_two_nodes(self):
        """GET 判 `system.settings`、PATCH 判 `system.edit_settings`——各管各的。"""
        # 只有 GET 节点：GET 过、PATCH 403。
        read_only = _grant_nodes_and_login(
            DOMAIN_A, "switch-reader", codes=("system.settings",)
        )
        self.assertEqual(read_only.get(SWITCHES_ENDPOINT).status_code, 200)
        self.assertEqual(
            read_only.patch(
                switch_url(), {"value": "Y"}, content_type="application/json"
            ).status_code,
            403,
        )
        # 一个节点都没有的账号：两边都 403。
        outsider = make_logged_in_client(username="switch-outsider")
        self.assertEqual(outsider.get(SWITCHES_ENDPOINT).status_code, 403)
        self.assertEqual(
            outsider.patch(
                switch_url(), {"value": "Y"}, content_type="application/json"
            ).status_code,
            403,
        )

    def test_anonymous_cannot_reach_the_endpoints(self):
        client = Client(SERVER_NAME=DOMAIN_A)
        self.assertIn(client.get(SWITCHES_ENDPOINT).status_code, (401, 403))
        self.assertIn(
            client.patch(
                switch_url(), {"value": "Y"}, content_type="application/json"
            ).status_code,
            (401, 403),
        )

    # ── 留痕：恰好一行；no-op 与被拒请求零行 ────────────────────────
    def test_patch_writes_exactly_one_log_row_and_a_noop_writes_none(self):
        self._patch({"value": "Y"})
        rows = OperationLog.objects.filter(action="system.system_switch_update")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.log_type, 40)
        self.assertEqual(row.actor, "switch-admin")
        self.assertEqual(row.target_kind, "system_switch")
        self.assertEqual(row.target_id, SWITCH_KEY)
        self.assertEqual(row.target_name, "录单时禁止录入相同的来源单号")
        self.assertEqual(row.from_value, "N")
        self.assertEqual(row.to_value, "Y")
        self.assertEqual(
            row.summary,
            "【switch-admin】修改了系统设置开关「录单时禁止录入相同的来源单号」：N → Y",
        )
        # 纯 no-op：原样重发 ⇒ 零行（值没变不算一次修改）。
        before = rows.count()
        self.assertEqual(self._patch({"value": "Y"}).json()["code"], 0)
        self.assertEqual(rows.count(), before)
        # 被拒的请求（非法值）⇒ 零行。
        rejected = self._patch({"value": "X"})
        self.assertEqual(rejected.json()["code"], 1000)
        self.assertEqual(rows.count(), before)


class SystemEditSettingsNodeMigrationTests(TenantTestCase):
    """`0007_add_system_edit_settings_node`：存量租户补上第二条自有节点（照 `0006` 的口径）。"""

    def _make_module_row(self):
        return PermissionNode.objects.create(
            benchmark_id=11, code="system", name="系统维护", note="管理系统设置",
            url="/System/", kind="page", level=0, order_by=0,
        )

    def _run_0007(self):
        MIGRATION_0007.add_system_edit_settings_node(django_apps, None)

    def test_the_frozen_copy_matches_the_catalog_row(self):
        """⛔ 红线：迁移里的冻结副本与 `catalog.NODES` 那一行逐字同值（同 `0006` 的手法）。"""
        from apps.rbac.catalog import NODES

        rows = [n for n in NODES if n[0] == "system.edit_settings"]
        self.assertEqual(len(rows), 1, "catalog.NODES 里应当恰好一条 system.edit_settings")
        code, benchmark_id, name, note, url, kind, available = rows[0]

        self.assertEqual(MIGRATION_0007.SYSTEM_EDIT_SETTINGS_CODE, code)
        self.assertEqual(MIGRATION_0007.MODULE_CODE, "system")
        defaults = MIGRATION_0007.NODE_DEFAULTS
        self.assertIsNone(defaults["benchmark_id"])
        self.assertEqual(defaults["benchmark_id"], benchmark_id)
        self.assertEqual(defaults["name"], name)
        self.assertEqual(defaults["note"], note)
        self.assertEqual(defaults["url"], url)
        self.assertEqual(defaults["kind"], kind)
        self.assertEqual(defaults["level"], 1)
        self.assertEqual(defaults["available"], available)
        self.assertTrue(defaults["available"])

    def test_it_depends_on_the_previous_rbac_migration(self):
        self.assertEqual(
            [tuple(dep) for dep in MIGRATION_0007.Migration.dependencies],
            [("rbac", "0006_add_taker_open_api_node")],
        )

    def test_it_creates_the_node_on_a_tenant_that_lacks_it(self):
        PermissionNode.objects.all().delete()
        module_row = self._make_module_row()
        self._run_0007()
        node = PermissionNode.objects.get(code="system.edit_settings")
        self.assertEqual(node.benchmark_id, None)
        self.assertEqual(node.name, "编辑系统设置")
        self.assertEqual(node.url, "/SystemAjax/SystemOrderSetting")
        self.assertEqual(node.kind, "api")
        self.assertEqual(node.parent_id, module_row.pk, "挂在 system 栏目下")
        self.assertTrue(node.available)

    def test_rerunning_and_an_empty_catalog_are_both_safe(self):
        # 重跑：先建好（含被改过的显示名），再跑一次 ⇒ 一个字段都不动。
        PermissionNode.objects.all().delete()
        module_row = self._make_module_row()
        self._run_0007()
        PermissionNode.objects.filter(code="system.edit_settings").update(
            name="租户改过的名字"
        )
        self._run_0007()
        self.assertEqual(
            PermissionNode.objects.get(code="system.edit_settings").name, "租户改过的名字"
        )
        # 空目录（极端情形：栏目行还没灌）⇒ 建点、父级留空、不抛。
        PermissionNode.objects.all().delete()
        self._run_0007()
        node = PermissionNode.objects.get(code="system.edit_settings")
        self.assertIsNone(node.parent_id)


class DuplicateSourceGuardTenantBoundaryTests(TenantTestCase):
    """闸的租户边界：A 的开关状态与占号**不会**波及 B（`AGENTS.md` §4）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="开关边界租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant_a)
        # A 侧：开关开 + 已有一张占着 X 的单。
        SystemSwitch.objects.create(key=SWITCH_KEY, value="Y")
        self.game_a = Game.objects.create(name="边界游戏甲")
        Order.objects.create(
            order_no="20261010000000000001",
            game=self.game_a,
            project_text="边界占号甲",
            total_price=0,
            source_order_no=DUP_NO,
        )
        # 夹具默认给「当前租户全部游戏」（D-0136）⇒ 不用再声明游戏权限。
        self.client_a = make_logged_in_client(
            domain=DOMAIN_A,
            username="switch-boundary-a",
            groups=("客服",),
            nodes=("order.add_edit",),
        )
        try:
            with tenant_context(self.tenant_b):
                # B 侧：**同样**开着开关（两边都开才有判别力——B 放行 X 不能是因为开关没开）。
                SystemSwitch.objects.create(key=SWITCH_KEY, value="Y")
                self.game_b = Game.objects.create(name="边界游戏乙")
                Order.objects.create(
                    order_no="20261010000000000002",
                    game=self.game_b,
                    project_text="边界占号乙",
                    total_price=0,
                    source_order_no="TB-合成-仅B租户-0002",
                )
                # ⚠️ 必须在 B 的 schema 里建（连接此刻在 B 上）：用户 / 档案 / 节点都是租户表。
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B,
                    username="switch-boundary-b",
                    groups=("客服",),
                    nodes=("order.add_edit",),
                )
        finally:
            connection.set_tenant(self.tenant_a)

    def _create(self, client, domain, game_id, source_order_no):
        return client.post(
            "/api/business/orders/",
            {
                "game": game_id,
                "project_text": "边界新建",
                "total_price": "1.00",
                "source_order_no": source_order_no,
            },
            content_type="application/json",
        )

    def test_tenant_b_can_use_the_same_number_and_a_still_blocks_it(self):
        # ⚠️ 判据用**租户内唯一的来源单号串**（DUP_NO），⛔ 不用主键（§5-G）。
        response = self._create(self.client_b, DOMAIN_B, self.game_b.pk, DUP_NO)
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 0, body)
        # B 库里 DUP_NO 有了**自己的一行**（B 的占号行用的是「仅B租户」那个号）——
        # B 侧闸没拦：它看不见 A 的行，A 的开关状态也不该影响 B。
        with tenant_context(self.tenant_b):
            self.assertEqual(
                Order.objects.filter(source_order_no=DUP_NO).count(), 1
            )

        # A 侧同号仍被拦：开关与占号都是 A 自己的。
        response = self._create(self.client_a, DOMAIN_A, self.game_a.pk, DUP_NO)
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 1000, body)
        self.assertIn(DUP_NO, body["msg"])
        self.assertEqual(Order.objects.filter(source_order_no=DUP_NO).count(), 1)


class OpenApiPathExemptionTests(TenantTestCase):
    """路径豁免（业务方「只拦手工」）：开放平台建单 / 改单不受开关管。"""

    # 合成凭据（⛔ 不是任何真实租户的凭据）。
    APP_KEY = "probeswitchkey000001"
    APP_SECRET = "probe-switch-secret-00000001"
    SOURCE_IP = "203.0.113.92"

    def setUp(self):
        cache.clear()
        self.domain_a = self.domain.domain
        self.game = Game.objects.create(name="开关豁免游戏")
        creator, _ = User.objects.get_or_create(
            username="开关豁免建号人", defaults={"password": "unused-in-this-test"}
        )
        self.app = ApiApp.objects.create(
            name="开关豁免应用",
            app_key=self.APP_KEY,
            app_secret=self.APP_SECRET,
            created_by=creator,
        )
        # 开关开着 + 一张占着号的手工单。
        SystemSwitch.objects.create(key=SWITCH_KEY, value="Y")
        self.held_no = "TB-合成-开放平台豁免-0001"
        Order.objects.create(
            order_no="20261010000000000011",
            game=self.game,
            project_text="豁免占号",
            total_price=0,
            source_order_no=self.held_no,
        )

    def _post_create(self, body, key):
        token = tokens.issue(self.app.app_key, self.app.app_secret)
        try:
            return Client(SERVER_NAME=self.domain_a).generic(
                "POST",
                "/api/open/v1/orders/",
                data=json.dumps(body),
                content_type="application/json",
                REMOTE_ADDR=self.SOURCE_IP,
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY=key,
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_openapi_create_with_a_duplicate_source_order_no_still_creates(self):
        """对外建单带**已被手工单占用**的来源单号 ⇒ 照建（app 自己的去重桶不含手工单）。"""
        response = self._post_create(
            {
                "game": self.game.pk,
                "project_text": "豁免新建",
                "total_price": "66.00",
                "source_order_no": self.held_no,
            },
            key="probe-switch-key-0001",
        )
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 0, body)
        # 判据打在库里：同号现在有**两行**（占号的手工单 + 对外新建的那张）。
        self.assertEqual(Order.objects.filter(source_order_no=self.held_no).count(), 2)

    def test_openapi_update_to_a_used_source_order_no_still_lands(self):
        """对外改单把它自己那张单的来源单号改成**已用号** ⇒ 照改（闸在内部视图层）。"""
        own = Order.objects.create(
            order_no="20261010000000000012",
            game=self.game,
            project_text="豁免改单",
            total_price=0,
            source_order_no="TB-合成-开放平台豁免-0002",
        )
        token = tokens.issue(self.app.app_key, self.app.app_secret)
        try:
            response = Client(SERVER_NAME=self.domain_a).generic(
                "PATCH",
                f"/api/open/v1/orders/{own.order_no}/",
                data=json.dumps({"source_order_no": self.held_no}),
                content_type="application/json",
                REMOTE_ADDR=self.SOURCE_IP,
                HTTP_AUTHORIZATION=f"Bearer {token}",
                HTTP_IDEMPOTENCY_KEY="probe-switch-key-0002",
            )
        finally:
            connection.set_tenant(self.tenant)
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 0, body)
        own.refresh_from_db()
        self.assertEqual(own.source_order_no, self.held_no)


#: peer 入站端点（与 `tests/test_peer_binding_api.py::E2E_INBOUND_URL` 同一条路径）。
#: ⚠️ 照先例**走 URL、不打函数级直调**：HTTP 级才把 `PeerTokenAuthentication` 与
#: 端点那层整批 `transaction.atomic()` 一起证到。
PEER_INBOUND_EVENTS_URL = "/api/business/peer-sync/inbound/events/"


class PeerInboundPathExemptionTests(TenantTestCase):
    """路径豁免：peer 入站建单不经过 `create_order` ⇒ 天然不受开关管。"""

    def setUp(self):
        self.game = Game.objects.create(name="豁免peer游戏")
        self.kefu = User.objects.create_user(
            username="switch-exempt-kefu", password="pw-switch-exempt"
        )
        # ⚠️ 认证要的是**明文令牌**（库里只存 `hash_token` 的散列）⇒ 建绑定时留住明文。
        self.token_in = new_token()
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家乙",
            peer_base_url="https://peer-up2.test.invalid",
            peer_account="switch-exempt-account",
            token_in_hash=hash_token(self.token_in),
            token_out=new_token(),
            local_base_url="https://peer-local2.test.invalid",
            bound_by=self.kefu,
        )
        # 开关开着 + 一张占着号的手工单。
        SystemSwitch.objects.create(key=SWITCH_KEY, value="Y")
        self.held_no = "TB-合成-peer豁免-0001"
        Order.objects.create(
            order_no="20261010000000000021",
            game=self.game,
            project_text="peer豁免占号",
            total_price=0,
            source_order_no=self.held_no,
        )

    def test_peer_inbound_landing_with_a_duplicate_source_order_no_still_creates(self):
        """首单落库（本地还没有 `PeerOrderLink`）带**已用号** ⇒ 照建（自动通道豁免）。

        HTTP 级（评审 Minor-2，照 `PeerInboundHealthColumnTests.post_events` 的请求形状）：
        Bearer 打真端点——直接调 `receive_events` 会跳过认证与端点那层整批事务。
        """
        from datetime import timedelta

        event = {
            "kind": "order.update",
            "target_ref": self.held_no,
            "occurred_at": (
                Order.objects.get(source_order_no=self.held_no).updated_at
                + timedelta(seconds=5)
            ).isoformat(),
            "payload": {
                "status": "IN_PROGRESS",
                "hall_price": "33.00",
                "game_name": "豁免peer游戏",
            },
        }
        response = Client(SERVER_NAME=DOMAIN_A).post(
            PEER_INBOUND_EVENTS_URL,
            json.dumps({"events": [event]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_in}",
        )
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["applied"], 1, body)
        self.assertEqual(Order.objects.filter(source_order_no=self.held_no).count(), 2)
