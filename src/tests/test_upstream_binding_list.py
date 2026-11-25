"""「绑定上家系统设置」页的两条后端端点（计划
    `docs/plans/2026-10-04-upstream-binding-list-rework.md` §2.1 / §2.2 / §3.1 第 7 条）。

    被测对象 = `apps/business/api/upstream_binding_views.py`：

    | 端点 | 判据 |
    |---|---|
    | `GET /api/business/upstream-bindings/` | 行**恰好 13 个键**（+ 大厅镜像 P1 的两格）；两类行混排；**全租户**的鸡蛋铺行（含 `enabled=False`）都在；按 `bound_at` 降序；⛔ 不落库、⛔ 无凭据 |
    | `DELETE /api/business/upstream-bindings/jidanpu/<pk>/` | **真删**；级联口径由模型 `on_delete` 决定（链路 CASCADE / 镜像 SET_NULL / 台账保留）；操作日志**落数据库行** |

    ⚠️ 断言一律钉**字面值**（13 个键名 / `1002` / 那句人话 / `40`），⛔ 不从被测模块 import
    常量当期望值（`AGENTS.md` §5-G / E-0024：自指断言在被断言的常量被改坏时不会红）。

    ⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。

    ⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、人名 `测试客服甲`、
    单号全零段一眼是合成值；⛔ 不写真值。
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
    PeerUpstreamBinding,
)
from apps.business.peer_sync import hash_token
from tests.p5_auth_helpers import make_logged_in_client

LIST_URL = "/api/business/upstream-bindings/"
#: 明细路径的模板：`pk` 由用例显式填入（⛔ 不用反向解析——判据要钉住真实 URL 拼写）。
DELETE_URL = "/api/business/upstream-bindings/jidanpu/{pk}/"

#: 本页两条端点的节点门节点 code（计划 §2.1）。⚠️ 逐字写死、⛔ 不从视图 import
#: `UPSTREAM_BINDING_NODE`：那是自指断言（E-0024）。
PAGE_BINDING_NODE = "system.bind_upstream_system"

#: 行**恰好**的 13 个字面键名（计划 §2.1 的契约表 + 大厅镜像 P1 的两格）。
#: ⛔ 不从模块的 `ROW_KEYS` 派生。
EXPECTED_ROW_KEYS = [
    "id", "system_type", "system_name", "base_url", "account",
    "bound_by_name", "bound_at", "last_ok_at", "last_error", "last_error_at",
    "consecutive_failures",
    "hall_sync_enabled", "hall_price_percent",
]

#: 删除不存在那行时的人话（**外部契约值**，逐字；计划 §2.2）。
EXPECTED_MISSING_MSG = "这个绑定不存在"
#: 业务错误码字面值（`ErrorCode.BUSINESS`）。
EXPECTED_BUSINESS_CODE = 1002

PEER_BASE_URL = "https://peer-list.test.invalid"
PEER_PASSWORD = "pw-canary-peer-list"
PEER_TOKEN_OUT = "TOKEN-OUT-LIST"
PEER_LOCAL_BASE_URL = "https://local-list.test.invalid"
JIDANPU_BASE_URL = "https://jidanpu-list.test.invalid/"
JIDANPU_PASSWORD = "pw-canary-jidanpu-list"


class _FixtureMixin:
    """两类绑定的合成夹具（每个用例各自建行，`setUp` 只建账号与游戏）。"""

    def _new_user(self, username: str) -> User:
        return User.objects.create_user(username=username, password="pw-list-extra")

    def _peer(self, *, bound_by=None, bound_at=None, peer_name="测试工作室甲",
              base_url=PEER_BASE_URL, account="peer-acc-list", deleted_at=None,
              consecutive_failures=0, last_error="", last_error_at=None,
              hall_sync_enabled=False, hall_price_percent=100):
        row = PeerUpstreamBinding.objects.create(
            peer_name=peer_name, peer_base_url=base_url, peer_account=account,
            password=PEER_PASSWORD, token_in_hash=hash_token("TOKEN-IN-LIST"),
            token_out=PEER_TOKEN_OUT, local_base_url=PEER_LOCAL_BASE_URL,
            bound_by=bound_by or self.user, deleted_at=deleted_at,
            consecutive_failures=consecutive_failures, last_error=last_error,
            last_error_at=last_error_at,
            hall_sync_enabled=hall_sync_enabled,
            hall_price_percent=hall_price_percent,
        )
        if bound_at is not None:
            # `bound_at` 是 `auto_now_add` ⇒ 只能在落库后用 `update()` 钉一个确定值。
            PeerUpstreamBinding.objects.filter(pk=row.pk).update(bound_at=bound_at)
            row.refresh_from_db()
        return row

    def _jidanpu(self, *, user=None, created_at=None, account_name="", name="", enabled=True,
                 base_url=JIDANPU_BASE_URL, account="kefu-up-list",
                 last_ok_at=None, last_error="", last_error_at=None,
                 hall_price_percent=100):
        row = JidanpuUpstreamBinding.objects.create(
            user=user or self.user, jidanpu_base_url=base_url, account=account,
            account_name=account_name, name=name, password=JIDANPU_PASSWORD, enabled=enabled,
            last_ok_at=last_ok_at, last_error=last_error, last_error_at=last_error_at,
            hall_price_percent=hall_price_percent,
        )
        if created_at is not None:
            JidanpuUpstreamBinding.objects.filter(pk=row.pk).update(created_at=created_at)
            row.refresh_from_db()
        return row

    def _items(self):
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, resp.content[:300])
        return body["data"]


class UpstreamBindingListTests(_FixtureMixin, TenantTestCase):
    """`GET /api/business/upstream-bindings/`（计划 §2.1）。"""

    def setUp(self):
        self.client = make_logged_in_client(
            username="kefu-list", groups=("客服",), nodes=(PAGE_BINDING_NODE,),
        )
        self.user = User.objects.get(username="kefu-list")
        self.other = self._new_user("kefu-list-other")

    def test_row_key_set_is_exactly_the_thirteen_literal_names(self):
        self._peer()
        self._jidanpu(user=self.other)
        items = self._items()["items"]
        # 正向对照：两类行都在 ⇒ 下面两条键集断言都打在**真有行**的载荷上。
        self.assertEqual(len(items), 2, items)
        for item in items:
            self.assertEqual(sorted(item.keys()), sorted(EXPECTED_ROW_KEYS))

    def test_both_system_types_are_present_and_named_as_the_contract_says(self):
        peer = self._peer(peer_name="测试工作室甲")
        jidanpu = self._jidanpu(user=self.other, account_name="上家账号显示名甲")
        by_type = {item["system_type"]: item for item in self._items()["items"]}
        self.assertEqual(sorted(by_type), ["jidanpu", "tuantuantong"])
        self.assertEqual(by_type["tuantuantong"]["id"], peer.pk)
        self.assertEqual(by_type["jidanpu"]["id"], jidanpu.pk)

    def test_peer_row_is_mapped_field_by_field(self):
        self.user.first_name = "测试客服甲"
        self.user.save(update_fields=["first_name"])
        peer = self._peer(peer_name="测试工作室甲", account="peer-acc-list")
        item = self._items()["items"][0]
        self.assertEqual(item["id"], peer.pk)
        self.assertEqual(item["system_type"], "tuantuantong")
        self.assertEqual(item["system_name"], "测试工作室甲")
        self.assertEqual(item["base_url"], PEER_BASE_URL)
        self.assertEqual(item["account"], "peer-acc-list")
        self.assertEqual(item["bound_by_name"], "测试客服甲")

    def test_peer_row_carries_the_hall_switch_and_percent(self):
        """⭐ 大厅镜像 P1：团团通行的两格取**真值**（编辑弹窗不必再发一次明细 GET）。"""
        self._peer(hall_sync_enabled=True, hall_price_percent=90)
        item = self._items()["items"][0]
        self.assertIs(item["hall_sync_enabled"], True)
        self.assertEqual(item["hall_price_percent"], 90)

    def test_peer_row_defaults_are_off_and_hundred(self):
        """老行（两列上线前建的）读出来就是模型默认的 `False` / `100`，⛔ 不是 `null`。"""
        self._peer()
        item = self._items()["items"][0]
        self.assertIs(item["hall_sync_enabled"], False)
        self.assertEqual(item["hall_price_percent"], 100)

    def test_jidanpu_row_reports_no_hall_switch_but_its_own_percent(self):
        """鸡蛋铺行：`hall_sync_enabled = null`（那模型没有这个概念），百分比是它**自己**那列。

    ⚠️ 两个值一起钉：把鸡蛋铺行的开关写成 `False`（当作「有、且关着」）也会被这条抓出来
    ——`null` 与 `False` 在页面上是两句不同的话（「没有这个概念」/「有，但关着」）。
"""
        self._jidanpu(user=self.other, hall_price_percent=70)
        item = self._items()["items"][0]
        self.assertIsNone(item["hall_sync_enabled"])
        self.assertEqual(item["hall_price_percent"], 70)

    def test_jidanpu_row_is_mapped_field_by_field(self):
        self.other.first_name = "测试客服乙"
        self.other.save(update_fields=["first_name"])
        jidanpu = self._jidanpu(user=self.other, account_name="上家账号显示名甲",
                                account="kefu-up-list")
        item = self._items()["items"][0]
        self.assertEqual(item["id"], jidanpu.pk)
        self.assertEqual(item["system_type"], "jidanpu")
        self.assertEqual(item["system_name"], "上家账号显示名甲")
        self.assertEqual(item["base_url"], JIDANPU_BASE_URL)
        self.assertEqual(item["account"], "kefu-up-list")
        # `bound_by_name` 走 `user_display_name`（昵称优先）。
        self.assertEqual(item["bound_by_name"], "测试客服乙")

    def test_jidanpu_system_name_prefers_the_human_name_over_account_name(self):
        """⭐ 2026-10-29（业务方指令）：`system_name` **人填的 `name` 优先**——
    绑多家上家鸡蛋铺时靠它区分；两个值刻意都给，取错了来源立刻红。"""
        self._jidanpu(user=self.other, name="初音网游", account_name="上家账号显示名甲")
        item = self._items()["items"][0]
        self.assertEqual(item["system_name"], "初音网游")

    def test_jidanpu_system_name_falls_back_to_account_name_when_unnamed(self):
        """没起名（`name` 空串）⇒ 回落 `account_name`——**存量行**的显示与加列之前
    一字不差（⛔ 不让存量行变空白）。"""
        self._jidanpu(user=self.other, name="", account_name="上家账号显示名甲")
        item = self._items()["items"][0]
        self.assertEqual(item["system_name"], "上家账号显示名甲")

    def test_jidanpu_empty_account_name_stays_empty_and_is_not_faked_from_the_url(self):
        """`name` 与 `account_name` **都空** ⇒ `system_name` 回**空串**（计划 §2.1 的括注；
    ⭐ 2026-10-29 加 `name` 列后这条契约**原样保留**——页面显示 `—`、行与行靠地址列区分）。

    ⛔ 旧写法（用地址编一个名字）会让页面显示一个**看起来像对上家了**的名字。
"""
        self._jidanpu(user=self.other, name="", account_name="")
        item = self._items()["items"][0]
        self.assertEqual(item["system_name"], "")
        # 对照：地址仍在（证明这一行真有地址、只是名字为空）。
        self.assertEqual(item["base_url"], JIDANPU_BASE_URL)

    def test_jidanpu_consecutive_failures_is_zero_while_the_peer_value_is_the_real_one(self):
        """鸡蛋铺行的 `consecutive_failures` **恒 0**（模型没有这一列）；团团通行写真值。

    ⚠️ 正反两个值一起钉：只断言鸡蛋铺那一侧恒 0 的话，把两类行的该字段写死成 0
    也会绿。
"""
        self._jidanpu(user=self.other)
        self._peer(consecutive_failures=3)
        by_type = {item["system_type"]: item for item in self._items()["items"]}
        self.assertEqual(by_type["jidanpu"]["consecutive_failures"], 0)
        self.assertEqual(by_type["tuantuantong"]["consecutive_failures"], 3)

    def test_jidanpu_health_columns_are_exposed(self):
        """R20 / A19 两块提示靠这几个列取数（计划 §2.1 的 ⚠️ 那条）。"""
        now = timezone.now()
        self._jidanpu(user=self.other, last_ok_at=now, last_error="上家返回了失败",
                      last_error_at=now)
        item = self._items()["items"][0]
        self.assertEqual(item["last_error"], "上家返回了失败")
        self.assertIsNotNone(item["last_ok_at"])
        self.assertIsNotNone(item["last_error_at"])

    def test_both_system_types_render_timestamps_in_the_same_shape(self):
        """两类行的时刻串**同一套渲染**（前端一套 `formatDateTime` 就够）。

    判据：把两类的时刻钉在**同一瞬间**，两串必须逐字相等 —— 一侧走模型序列化器、
    另一侧走显式 `DateTimeField`，形状不一致时这条当场红。
"""
        moment = timezone.now().replace(microsecond=0)
        self._peer(bound_at=moment)
        self._jidanpu(user=self.other, created_at=moment)
        by_type = {item["system_type"]: item for item in self._items()["items"]}
        self.assertEqual(
            by_type["tuantuantong"]["bound_at"], by_type["jidanpu"]["bound_at"]
        )

    def test_rows_are_ordered_by_bound_at_descending_across_both_tables(self):
        base = timezone.now()
        old = self._peer(bound_at=base - timedelta(hours=3))
        mid = self._peer(bound_at=base - timedelta(hours=2), peer_name="测试工作室乙")
        new = self._jidanpu(user=self.other, created_at=base - timedelta(hours=1))
        items = self._items()["items"]
        self.assertEqual([item["id"] for item in items], [new.pk, mid.pk, old.pk])
        # 混排：三类来源的位置恰好说明两类行**同一张表**里排序，而不是各排各的。
        self.assertEqual([item["system_type"] for item in items],
                         ["jidanpu", "tuantuantong", "tuantuantong"])

    def test_same_bound_at_falls_back_to_id_descending(self):
        base = timezone.now()
        low = self._peer(bound_at=base, peer_name="测试工作室甲")
        high = self._peer(bound_at=base, peer_name="测试工作室乙")
        self.assertLess(low.pk, high.pk)
        self.assertEqual([item["id"] for item in self._items()["items"]],
                         [high.pk, low.pk])

    def test_soft_deleted_peer_rows_are_hidden(self):
        alive = self._peer()
        gone = self._peer(peer_name="已解绑的工作室", deleted_at=timezone.now())
        ids = [item["id"] for item in self._items()["items"]]
        self.assertEqual(ids, [alive.pk])
        self.assertNotIn(gone.pk, ids)

    def test_other_peoples_jidanpu_rows_are_listed_and_disabled_ones_too(self):
        """计划 §0 裁定 3：鸡蛋铺那类列**全租户所有人的行**；`enabled=False` 的行也在。"""
        mine = self._jidanpu(user=self.user, account="kefu-up-mine")
        theirs = self._jidanpu(user=self.other, account="kefu-up-theirs",
                               enabled=False)
        ids = sorted(item["id"] for item in self._items()["items"])
        self.assertEqual(ids, sorted([mine.pk, theirs.pk]))
        by_id = {item["id"]: item for item in self._items()["items"]}
        self.assertEqual(by_id[theirs.pk]["account"], "kefu-up-theirs")

    def test_total_matches_the_number_of_items(self):
        self._peer()
        self._jidanpu(user=self.other)
        data = self._items()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["items"]), 2)

    def test_no_credential_ever_appears_in_the_response(self):
        self._peer()
        self._jidanpu(user=self.other)
        body = self.client.get(LIST_URL).content.decode()
        # 正向对照：这一行**确实**回了载荷（否则下面几条 `assertNotIn` 在空列表上也成立）。
        self.assertIn("测试工作室甲", body)
        for secret in (PEER_PASSWORD, PEER_TOKEN_OUT, "token_in_hash", "token_out",
                       JIDANPU_PASSWORD, "password", "password_set"):
            self.assertNotIn(secret, body)

    def test_query_parameters_do_not_change_the_scope(self):
        """跨租户红线：范围只由认证态与 Host 决定，⛔ 不接受 `?user=` / `?tenant=`。"""
        self._peer()
        self._jidanpu(user=self.other)
        baseline = self._items()
        with_params = self.client.get(
            LIST_URL, {"user": str(self.other.pk), "tenant": "someone-else",
                       "account": "kefu-up-list"}
        ).json()["data"]
        self.assertEqual(with_params, baseline)
        self.assertEqual(baseline["total"], 2)

    def test_list_writes_nothing(self):
        """计划 §2.1：⛔ 一个字节都不写（不查上游、不落库）。"""
        self._peer()
        self._jidanpu(user=self.other)
        before = (OperationLog.objects.count(), PeerUpstreamBinding.objects.count(),
                  JidanpuUpstreamBinding.objects.count())
        self._items()
        after = (OperationLog.objects.count(), PeerUpstreamBinding.objects.count(),
                 JidanpuUpstreamBinding.objects.count())
        self.assertEqual(after, before)


class UpstreamBindingListRbacTests(_FixtureMixin, TenantTestCase):
    """`GET` 的节点门（fail-closed）：没有 `system.bind_upstream_system` 一律 403。"""

    def setUp(self):
        self.client = make_logged_in_client(
            username="kefu-list-nonode", groups=("客服",), nodes=(),
        )
        self.user = User.objects.get(username="kefu-list-nonode")

    def test_without_the_node_the_list_is_403_and_leaks_nothing(self):
        self._peer(peer_name="测试工作室甲")
        # 自证夹具真的登进来了（否则 403 可能由"压根没登录"凑出来）。
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, 403, resp.content[:300])
        self.assertEqual(resp.json()["code"], 403)
        self.assertNotIn("测试工作室甲", resp.content.decode())


class JidanpuUpstreamBindingDeleteTests(_FixtureMixin, TenantTestCase):
    """`DELETE /api/business/upstream-bindings/jidanpu/<pk>/`（计划 §2.2）。"""

    def setUp(self):
        self.client = make_logged_in_client(
            username="kefu-del", groups=("客服",), nodes=(PAGE_BINDING_NODE,),
        )
        self.user = User.objects.get(username="kefu-del")
        self.other = self._new_user("kefu-del-other")
        self.game = Game.objects.create(name="合成游戏甲")
        self.order = Order.objects.create(
            game=self.game, order_no="20261004000000000901",
            status=OrderStatus.IN_PROGRESS, hall_price="12.34",
            total_price="50.00", project_text="合成项目甲",
        )

    def _url(self, pk) -> str:
        return DELETE_URL.format(pk=pk)

    def test_delete_removes_the_binding_and_cascades_the_link(self):
        binding = self._jidanpu(user=self.user)
        link = JidanpuUpstreamOrderLink.objects.create(
            binding=binding, order=self.order, upstream_order_id="JP-9001",
        )
        resp = self.client.delete(self._url(binding.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], 0)
        self.assertIsNone(resp.json()["data"])
        self.assertFalse(JidanpuUpstreamBinding.objects.filter(pk=binding.pk).exists())
        self.assertFalse(JidanpuUpstreamOrderLink.objects.filter(pk=link.pk).exists())

    def test_delete_keeps_the_hall_mirror_row_and_clears_its_owner(self):
        binding = self._jidanpu(user=self.user)
        now = timezone.now()
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=JIDANPU_BASE_URL, upstream_order_id="JP-9002",
            owner_binding=binding, status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game, first_seen_at=now, last_seen_at=now,
        )
        self.client.delete(self._url(binding.pk))
        mirror.refresh_from_db()
        self.assertIsNone(mirror.owner_binding_id)

    def test_delete_keeps_the_sync_ledger_rows(self):
        """台账（`JidanpuSyncEvent`）无外键 ⇒ 删绑定**不碰它**（计划 §2.2）。"""
        binding = self._jidanpu(user=self.user)
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind="upstream.complete", target_type="order",
            target_ref=self.order.order_no,
            status=JidanpuSyncEvent.Status.PENDING,
            payload={"upstream_binding_id": binding.pk},
        )
        self.client.delete(self._url(binding.pk))
        self.assertTrue(JidanpuSyncEvent.objects.filter(pk=event.pk).exists())

    def test_delete_only_touches_the_named_binding(self):
        target = self._jidanpu(user=self.user)
        other_binding = self._jidanpu(user=self.other, account="kefu-up-other")
        other_link = JidanpuUpstreamOrderLink.objects.create(
            binding=other_binding, order=self.order, upstream_order_id="JP-9003",
        )
        self.client.delete(self._url(target.pk))
        self.assertTrue(JidanpuUpstreamOrderLink.objects.filter(pk=other_link.pk).exists())
        self.assertTrue(JidanpuUpstreamBinding.objects.filter(pk=other_binding.pk).exists())

    def test_delete_writes_an_operation_log_row(self):
        """留痕断言打在**数据库行**上（⛔ 不是 `assertLogs`，`AGENTS.md` §5-G）。

    ⚠️ 行里的值一律钉**字面值**：`log_type=40` / `target_kind` / `target_id` /
    `target_name`（站点地址去协议头 + 去尾斜杠）/ 渲染后的 `summary`。
"""
        binding = self._jidanpu(user=self.user)
        self.client.delete(self._url(binding.pk))
        rows = OperationLog.objects.filter(
            action="system.jidanpu_upstream_binding_delete"
        )
        self.assertEqual(rows.count(), 1)
        log = rows.get()
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.actor, "kefu-del")
        self.assertEqual(log.actor_id, self.user.pk)
        self.assertEqual(log.target_kind, "jidanpu_upstream")
        self.assertEqual(log.target_id, str(binding.pk))
        # 站点地址去协议头 + 去尾斜杠（`_upstream_binding_name` 的口径）。
        self.assertEqual(log.target_name, "jidanpu-list.test.invalid")
        # 人为操作的正文带 `【显示名】` 前缀。本夹具没有 `first_name`，显示名回落登录账号。
        # `actor` 列仍是登录账号。
        self.assertEqual(
            log.summary, "【kefu-del】删除上家鸡蛋铺绑定：jidanpu-list.test.invalid"
        )

    def test_the_row_is_removed_from_the_list_after_deletion(self):
        binding = self._jidanpu(user=self.user)
        self.assertEqual(self._items()["total"], 1)
        self.client.delete(self._url(binding.pk))
        self.assertEqual(self._items()["total"], 0)

    def test_unknown_pk_is_a_business_error_and_writes_no_log(self):
        resp = self.client.delete(self._url(99999999))
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(resp.json()["code"], EXPECTED_BUSINESS_CODE)
        self.assertEqual(resp.json()["msg"], EXPECTED_MISSING_MSG)
        self.assertEqual(
            OperationLog.objects.filter(
                action="system.jidanpu_upstream_binding_delete"
            ).count(),
            0,
        )

    def test_deleting_twice_reports_the_second_one_as_missing(self):
        binding = self._jidanpu(user=self.user)
        self.client.delete(self._url(binding.pk))
        resp = self.client.delete(self._url(binding.pk))
        self.assertEqual(resp.json()["code"], EXPECTED_BUSINESS_CODE)
        self.assertEqual(resp.json()["msg"], EXPECTED_MISSING_MSG)


class JidanpuUpstreamBindingDeleteRbacTests(_FixtureMixin, TenantTestCase):
    """`DELETE` 的节点门（fail-closed）：没有节点的人删不动。"""

    def setUp(self):
        self.client = make_logged_in_client(
            username="kefu-del-nonode", groups=("客服",), nodes=(),
        )
        self.user = User.objects.get(username="kefu-del-nonode")

    def test_without_the_node_the_delete_is_403_and_writes_nothing(self):
        binding = self._jidanpu(user=self.user)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        resp = self.client.delete(DELETE_URL.format(pk=binding.pk))
        self.assertEqual(resp.status_code, 403, resp.content[:300])
        self.assertEqual(resp.json()["code"], 403)
        # ⛔ 一行都没被删、也没留下操作日志（fail-closed 的**终态**判据）。
        self.assertTrue(JidanpuUpstreamBinding.objects.filter(pk=binding.pk).exists())
        self.assertEqual(
            OperationLog.objects.filter(
                action="system.jidanpu_upstream_binding_delete"
            ).count(),
            0,
        )
