"""T5：打手侧**对外**端点之二 —— **抢单**（`POST /api/taker/v1/hall/<order_no>/claim/`）。

spec：`docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 2 行 · §5.3-3 · §5.4（失败档）·
§9 的 1（等价判据）/ 3 / 4 / 5 / 6 / 14 条。

## 本文件证什么（逐条都能失败）

| # | 判据 | spec |
|---|---|---|
| 1 | ⭐ **范围收窄（本轮等价判据）**：拿甲的令牌抢**他游戏范围之外**的厅单 ⇒ `1002`，且库里那张单**逐字段全列快照**一个字段没变（含 MAIN 绩效行的归属与 `claimed_at`、以及**没有**留痕行）；另配**对照**：抢他自己的单**成功**、那张"范围外"的单在**有权限的人**手里**抢得到** | §9-1 / §9-5 |
| 2 | ⭐ **抢单并发（键距写清）**：**不同幂等键**抢同一张单 ⇒ 恰好一个 `code=0`、另一个 **`1012`**；**同一个幂等键** ⇒ **`1001`**（`idempotency.py` 的 `IntegrityError` 支）；库里只有一条 MAIN 行带 `claimed_at` | §9-3 |
| 3 | ⭐ **幂等 × 抢单**：同键重发 ⇒ 回放**逐字相同**的 `code` / `msg` / `data`（⛔ 不断响应字节：快照是 JSONB、**不保键序**）；换键再抢 ⇒ `1012`；缺 / 非法键 ⇒ `1000` | §9-4 |
| 4 | ⭐⭐ **反例（必须有）**：构造一次"**条件更新成功但状态机拒绝**"的失败 ⇒ **用同一个键重试必须真抢到**（⛔ 不是回放那次失败）。判别力：handler 若调了 `mark_side_effect`，失败也会落快照 ⇒ 重试回放 `1002` ⇒ 本用例红 | §9-4 |
| 5 | ⭐ **节点保留**：把 `grab_hall.take_order` 从该账号撤掉 ⇒ 抢单**立刻不通**，且**没有半截写入**（状态没变、绩效行没人、没有留痕） | §9-6 |
| 6 | ⭐ **孤儿订单**：一条**没有 MAIN 绩效行**的待抢单 ⇒ **`1002`**，⛔ **不是 500**、也⛔ 不是 `1012` | §9-14 |
| 7 | **失败档逐条**：不在大厅（未分配 / 不存在，**同形**）⇒ `1002`；已被接走 ⇒ `1012`（文案写清"包含你自己刚才已经接过"）；没有接单价 / 禁自助接单 ⇒ `1013`（**领域层那句话说清是哪一支**） | §5.4 |
| 8 | **畸形路径段**：订单号里带 NUL ⇒ **404**（⛔ 不是 500）——路由转换器挡掉，这类路径段根本不进视图（带转换器的路由不在 `test_no_route_500` 那条腿的覆盖内，所以必须在这里补 HTTP 级判据） | `test_no_route_500` 的已知盲区 |

## ⚠️ 证明力边界（如实写清楚）

- 令牌**一律从 `/api/taker/oauth/token` 换**（真路由 + 真 `Client`，⛔ 不手搓）；
- 第 2 条的两半**不是两个真线程**：不同键那一半是**两次顺序调用**（并发真正的裁决者是
  `claim.py` 那条条件 `UPDATE` 的 `rowcount`，本用例钉的是"恰好一个赢 + 输家拿 `1012`"）；
  同键那一半用"**预先存在的同键占位行** + 把 `_find_existing` 打桩成没查到"**确定性地**
  走到 `IntegrityError` 那条支路（⛔ 不是把两次顺序调用硬说成并发）——
  这与 `tests/test_openapi_order_write.py` 里那条既有用例同一手法。
  ⛔ 本文件**不声称**覆盖真并发 / 跨进程（那是 `tests/test_claim_concurrency.py` 的活）。
- 夹具一律**合成值**（`AGENTS.md` §5-C）；来源 IP 取文档保留段（⛔ 不是任何真实客户端）。
"""
import hashlib
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.openapi import idempotency as openapi_idempotency
from apps.rbac.models import Role
from apps.takerapi.models import TakerApiIdempotency, TakerApp
from tests.p5_auth_helpers import grant_nodes

User = get_user_model()

TOKEN_PATH = "/api/taker/oauth/token"
CLAIM_TEMPLATE = "/api/taker/v1/hall/{order_no}/claim/"

GRANT_TYPE = "client_credentials"

# ── 合成凭据（⛔ 与任何真实租户的 appKey / secret 无关）────────────────────────
TAKER_A_KEY = "takerclaimkey00001"
TAKER_A_SECRET = "taker-claim-secret-00000000001"
TAKER_B_KEY = "takerclaimkey00002"
TAKER_B_SECRET = "taker-claim-secret-00000000002"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.51"

#: 订单号（20 位数字，与生成器同形）。
ORDER_NO_HIS = "20261008000000000101"
ORDER_NO_OTHER = "20261008000000000102"

#: ⭐ 该账号持的**唯一**节点（撤掉它就没有第二条路可走：`WAITING_GRAB → IN_PROGRESS`
#: 那条边的节点集是 `{order.change_status, grab_hall.take_order}`，而这里**只给**后者）。
#: ⚠️ 写字面量（`E-0024`）：从被测模块 import 会让"节点名被改坏"这件事在用例里看不见。
TAKE_ORDER_NODE = "grab_hall.take_order"

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字）────────────────────────────────
OK = 0
VALIDATION = 1000
CONFLICT = 1001
BUSINESS = 1002
ALREADY_TAKEN = 1012
GRAB_LIMIT = 1013
UNAUTHENTICATED = 401
NOT_FOUND = 404

#: ⭐ 留痕的字面值：`order.claim` 的 `log_type` 是 **10**（`apps/audit/actions.py` 的登记值）。
#: ⚠️ 写字面量而不是 `ACTION_LOG_TYPES["order.claim"]`：后者是自指（常量被改坏时本行不会红）。
CLAIM_LOG_TYPE = 10

#: 幂等键长度契约的两端（与 `apps/openapi/idempotency.py` 的对外契约同值）。
MIN_KEY = 8
MAX_KEY = 128

#: 那两句对外文案里**必须出现**的片段（spec §5.4 的两条要求）。
#: ⚠️ 判据打在**片段**上（不是整句相等）：整句相等会把文案的措辞当契约，
#: 而这两条要求的是**语义**（"包含你自己刚才已经接过" / "与不存在同形"）。
SELF_CLAIMED_FRAGMENT = "你自己刚才已经接过"


class _ClaimFixture(TenantTestCase):
    """两个打手（各持**不同**游戏权限）+ 各一张厅单 + 各一套凭据。

    ⚠️ 节点只给 `grab_hall.take_order`（见 `TAKE_ORDER_NODE` 的注释）：这样"撤掉节点 ⇒ 抢单不通"
    这条判据才有判别力（若同时给了 `order.change_status`，撤掉前者照样能抢）。
    """

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.game_his = Game.objects.create(name="抢单测试游戏甲")
        self.game_other = Game.objects.create(name="抢单测试游戏乙")

        self.taker_a = self._make_taker("claim-taker-a", games=[self.game_his])
        self.taker_b = self._make_taker(
            "claim-taker-b", games=[self.game_his, self.game_other]
        )
        self.app_a = self._make_app(
            "抢单测试应用甲", TAKER_A_KEY, TAKER_A_SECRET, self.taker_a
        )
        self.app_b = self._make_app(
            "抢单测试应用乙", TAKER_B_KEY, TAKER_B_SECRET, self.taker_b
        )
        self.token_a = self.issue_token(self.app_a)
        self.token_b = self.issue_token(self.app_b)

        #: 他自己游戏范围里的厅单（**对照**用：抢它必须成功）。
        self.order_his = self._order(self.game_his, ORDER_NO_HIS, price="88.00")
        #: 他**没有**权限那款游戏里的厅单（范围收窄用：抢它必须 `1002`）。
        self.order_other = self._order(self.game_other, ORDER_NO_OTHER, price="66.00")

    # ── 造物 ───────────────────────────────────────────────────────────
    def _make_taker(self, username, *, games):
        """普通账号 + 显式游戏权限 + **只挂那一个节点**。"""
        user = User.objects.create_user(username=username, password="pw-12345678")
        group, _ = Group.objects.get_or_create(name="代练员")
        user.groups.add(group)
        StaffProfile.objects.create(user=user).games.set(games)
        grant_nodes(user, TAKE_ORDER_NODE)
        return user

    def withdraw_take_order_node(self, user):
        """把 `grab_hall.take_order` 从该账号所在组的角色上撤掉（spec §9-6 的夹具动作）。

        ⚠️ 判据读的是 `user.groups → Role → permissions`，所以撤的是**角色**上那一行
        ——这正是"账号的节点集"在库里的载体。
        """
        from apps.rbac.models import PermissionNode

        node = PermissionNode.objects.get(code=TAKE_ORDER_NODE)
        for name in user.groups.values_list("name", flat=True):
            Role.objects.get(name=name).permissions.remove(node)

    def _make_app(self, name, app_key, app_secret, taker):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=taker,
            created_by=taker,
        )

    def _order(
        self, game, order_no, *, status=OrderStatus.WAITING_GRAB, price="88.00",
        with_main_performance=True, taker=None,
    ):
        """建一张订单（可选地建**那条 MAIN 绩效行**：孤儿订单那一条判据要的正是"没有它"）。"""
        order = Order.objects.create(
            game=game,
            order_no=order_no,
            status=status,
            hall_price=None if price is None else Decimal(price),
        )
        if with_main_performance:
            Performance.objects.create(
                order=order,
                user=taker,
                emp_type=EmpType.MAIN,
                temp_amount=Decimal("88.00") if price is None else Decimal(price),
            )
        return order

    # ── 令牌：**只从取令牌端点拿** ─────────────────────────────────────
    def issue_token(self, app) -> str:
        response = self.anonymous.post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE,
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = json.loads(response.content)
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        return body["access_token"]

    # ── 发请求 ─────────────────────────────────────────────────────────
    def claim(
        self, order_no, *, key, token=None, path=None, remote_addr=SOURCE_IP,
    ):
        """`POST …/claim/`（**空 body**：契约就是"无 body"）。

        ⚠️ 必须给**确定的字节**：Django 测试客户端在 `content_type` 缺省时用 multipart，
        而 multipart 的 boundary **每次随机** ⇒ 两次"同一个请求"的 `request.body` 不同
        ⇒ 幂等判据会以 `1000`（同键异体）**假红**。这里显式给 `application/json` + 空串。
        ⚠️ `key=None` ⇒ **不带** `Idempotency-Key` 头（"缺键 ⇒ `1000`"那一档要用）。
        """
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token_a}"}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        return self.anonymous.post(
            path or CLAIM_TEMPLATE.format(order_no=order_no),
            data="",
            content_type="application/json",
            REMOTE_ADDR=remote_addr,
            **headers,
        )

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def assert_business_failure(self, response, code):
        """业务失败：**HTTP 200** + 指定业务码（本仓口径：状态码只表达传输 / 认证层）。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        self.assertIsNone(payload["data"], payload)
        return payload

    def assert_claimed(self, response):
        """成功路径：HTTP 200 + `code=0` + `data` 恰好三个键。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        data = payload["data"]
        self.assertEqual(set(data), {"order_no", "status", "status_text"}, data)
        return data

    def main_of(self, order):
        return Performance.objects.get(order=order, emp_type=EmpType.MAIN)

    @staticmethod
    def order_snapshot(order):
        """一行订单的**全列快照**（`_meta.concrete_fields`，含 pk 与四个时间列）。

        ⚠️ 判"那张单一个字段都没变"必须用它，⛔ 不是挑几个字段比一比：挑字段的写法在
        "改了一列而我恰好没挑到"时**静默绿**（`E-0064` 同族）。
        ⚠️ 取 `attname`（外键取**主键值**），⛔ 不取对象本身（那是刷新前的缓存）。
        """
        return {
            field.name: getattr(order, field.attname)
            for field in Order._meta.concrete_fields
        }

    def assert_no_logs(self, order_no):
        self.assertEqual(
            OperationLog.objects.filter(order_no=order_no).count(),
            0,
            "失败路径留下了留痕行（「没抢到、日志却写了」）",
        )


class ClaimSuccessTests(_ClaimFixture):
    """成功路径（⭐ 也是范围判据那条的**正对照**：他自己的单同样的调用**成功**）。"""

    def test_he_can_claim_his_own_hall_order(self):
        """⭐ **正对照**：他自己游戏范围里那张厅单 ⇒ `code=0` + `data` 三个键 + 真的落库。

        缺这条，"抢别人的单回 `1002`"对"什么都拒"的实现也会绿。
        """
        data = self.assert_claimed(self.claim(ORDER_NO_HIS, key="claim-key-000001"))
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(data["order_no"], ORDER_NO_HIS)
        self.assertEqual(data["status"], OrderStatus.IN_PROGRESS)
        # `status_text` 取 `Order.get_status_display()`（⛔ 不是第三份 `dict(choices)`）。
        self.assertEqual(data["status_text"], "代练中")
        main = self.main_of(self.order_his)
        self.assertEqual(main.user_id, self.taker_a.pk)
        self.assertIsNotNone(main.claimed_at)

    def test_the_claim_writes_exactly_one_operation_log_row(self):
        """⭐ 抢单是**写**端点 ⇒ 必须有一次 `record(...)`（本仓留痕纪律）。"""
        self.assert_claimed(self.claim(ORDER_NO_HIS, key="claim-key-000002"))
        logs = OperationLog.objects.filter(order_no=ORDER_NO_HIS, action="order.claim")
        self.assertEqual(logs.count(), 1, list(logs.values()))
        row = logs.get()
        # ⚠️ 四条都写**字面量**（`E-0024`）：`log_type` 10 / 操作人是"哪个应用" / 渠道 / 接单人。
        self.assertEqual(row.log_type, CLAIM_LOG_TYPE)
        self.assertEqual(row.actor, "打手侧应用 抢单测试应用甲")
        self.assertIsNone(row.actor_id)          # 这条路上**没有"人"这个操作人**
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(self.taker_a.pk))
        self.assertEqual(row.target_name, self.taker_a.get_username())
        self.assertEqual(row.reason, "打手对接接口")

    def test_a_second_call_with_a_different_key_says_it_is_already_taken(self):
        """⭐ spec §9-3 的另一半 + §5.4 的文案要求：**不同幂等键**抢同一张单 ⇒ 输家 `1012`。

        ⚠️ 这一半**不需要真并发**：并发真正的裁决者是 `claim.py` 那条条件 `UPDATE`
        （`rowcount`），本用例钉的是"**恰好一个** `code=0`、另一个 `1012`"这个对外可观察结果。
        ⚠️ 文案必须写清"**包含你自己刚才已经接过**"（spec §5.4 逐字要求）：顺序双击 / 换键重发
        拿到的是**同一个码**，那句话是他唯一的解释。
        """
        first = self.assert_claimed(self.claim(ORDER_NO_HIS, key="claim-key-000003"))
        second = self.assert_business_failure(
            self.claim(ORDER_NO_HIS, key="claim-key-000004"), ALREADY_TAKEN
        )
        self.assertIn(SELF_CLAIMED_FRAGMENT, second["msg"])
        # ⭐ **库里只有一条 MAIN 行带 `claimed_at`**（"恰好一个赢家"在库里的读数）。
        rows = Performance.objects.filter(
            order=self.order_his, emp_type=EmpType.MAIN
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(
            rows.filter(claimed_at__isnull=False).count(), 1, "接单时刻不唯一"
        )
        main = self.main_of(self.order_his)
        self.assertEqual(main.user_id, self.taker_a.pk)
        # 赢家那一笔的响应仍是原来那一份（输的那一次没有改写任何东西）。
        self.assertEqual(first["status"], OrderStatus.IN_PROGRESS)
        # 只有**一个**留痕行（失败那一次不写）。
        self.assertEqual(
            OperationLog.objects.filter(order_no=ORDER_NO_HIS).count(), 1
        )


class ClaimScopeTests(_ClaimFixture):
    """⭐ spec §9-1（本轮的等价判据）与 §9-5：**范围收窄** + **两条对照**。

    ⚠️ 为什么"抢别人的单 ⇒ `1002`"这条**必须有对照**才成立：一个"什么都拒"的实现也能让它绿。
    本类配了两条对照——
    ① 抢**他自己的**单**成功**（见 `ClaimSuccessTests`）；
    ② 那张"范围外"的单在**有那款游戏权限的人**手里**抢得到**（本类第二条）。
    """

    def test_claiming_an_order_outside_his_game_scope_is_1002_and_changes_nothing(self):
        """⭐ 拿甲的令牌抢范围外的单 ⇒ `1002`，且那张单**一个字段都没变**（逐字段全列快照）。"""
        before = self.order_snapshot(self.order_other)
        payload = self.assert_business_failure(
            self.claim(ORDER_NO_OTHER, key="claim-key-000005"), BUSINESS
        )
        self.order_other.refresh_from_db()
        self.assertEqual(self.order_snapshot(self.order_other), before)
        # 绩效行也一个字没变（归属仍为空、没有接单时刻），且**没有**留痕。
        main = self.main_of(self.order_other)
        self.assertIsNone(main.user_id)
        self.assertIsNone(main.claimed_at)
        self.assert_no_logs(ORDER_NO_OTHER)
        # ⚠️ 文案与"不存在"**同形**（下面那条用例断同一句），⛔ 不泄漏"这个单号存不存在"。
        self.assertEqual(payload["msg"], self._msg_of_missing())

    def test_the_same_order_is_claimable_by_someone_who_has_that_game(self):
        """⭐ **对照**：同一张单、**有**那款游戏权限的打手 ⇒ **抢得到**。

        判别力：若那张单其实因为别的原因不可抢（状态不对 / 没有 MAIN 绩效行 / 已被接走），
        上面那条会以**与被测行为无关的原因**变绿；本用例把那些可能性排除掉。
        """
        data = self.assert_claimed(
            self.claim(ORDER_NO_OTHER, key="claim-key-000006", token=self.token_b)
        )
        self.assertEqual(data["order_no"], ORDER_NO_OTHER)
        self.order_other.refresh_from_db()
        self.assertEqual(self.main_of(self.order_other).user_id, self.taker_b.pk)
        # ⚠️ 而甲**仍然**拿到 `1002`（不是 `1012`）：那张单在他的游戏范围**之外**、
        #    取单那一步就取不到 ⇒ 与"不存在"**同形**——⛔ 不回一个能证明"这张单存在"的码。
        #    （这条同时是"反例不算泄漏"的判据：赢了的那个人换了，甲能看到的答复**一个字不变**。）
        payload = self.assert_business_failure(
            self.claim(ORDER_NO_OTHER, key="claim-key-000007"), BUSINESS
        )
        self.assertEqual(payload["msg"], self._msg_of_missing())

    def test_a_nonexistent_order_number_is_1002_with_the_same_message(self):
        """不存在的单号 ⇒ `1002`，且与"范围外"**同一句文案**（同形 ⇒ 不可探测存在性）。"""
        payload = self.assert_business_failure(
            self.claim("20261008000000000999", key="claim-key-000008"), BUSINESS
        )
        self.assertEqual(payload["msg"], self._msg_of_missing())

    def test_a_soft_deleted_order_is_1002(self):
        """已软删的单**取不到** ⇒ 与"不存在"同形（`D-0144`：默认 manager 的语义）。

        ⚠️ 软删态用 **ORM 直接造**（⛔ 不经过删除端点）：本用例要钉的是**取数口**的口径。
        """
        from django.utils import timezone

        before = self.order_snapshot(self.order_his)
        Order.objects.filter(pk=self.order_his.pk).update(deleted_at=timezone.now())
        payload = self.assert_business_failure(
            self.claim(ORDER_NO_HIS, key="claim-key-000009"), BUSINESS
        )
        self.assertEqual(payload["msg"], self._msg_of_missing())
        # 那两列是被夹具改的，其余字段逐字未动（本判据只关心"抢单没动它"）。
        self.order_his.refresh_from_db()
        current = self.order_snapshot(self.order_his)
        self.assertIsNotNone(current.pop("deleted_at"))
        before.pop("deleted_at")
        self.assertEqual(current, before)

    @staticmethod
    def _msg_of_missing():
        """`1002` 那一句的**字面量**（⛔ 不从被测模块 import：那是自指断言）。"""
        return "该订单不存在或不在抢单大厅"


class ClaimFailureCodeTests(_ClaimFixture):
    """spec §5.4 的失败档逐条（⛔ 不让它们退化成一个含糊的"失败"）。"""

    def test_an_order_not_in_the_hall_is_1002(self):
        """状态不是待抢单（且没人接）的单 ⇒ `1002`「不在大厅」（与"不存在"同形）。"""
        order = self._order(
            self.game_his, "20261008000000000201", status=OrderStatus.UNASSIGNED
        )
        payload = self.assert_business_failure(
            self.claim(order.order_no, key="claim-key-000010"), BUSINESS
        )
        self.assertEqual(payload["msg"], ClaimScopeTests._msg_of_missing())
        self.assert_no_logs(order.order_no)

    def test_an_order_taken_by_someone_else_is_1012(self):
        """别人的进行中单 ⇒ **`1012`**（已被接走），⛔ 不是 `1002`。

        ⚠️ 这一条与上一条的分工就是 `claim.py` 的分支：状态不是待抢单时，
        「MAIN 行有人」⇒ `1012`、「没人（也没行）」⇒ `1002`。
        """
        order = self._order(
            self.game_his, "20261008000000000202",
            status=OrderStatus.IN_PROGRESS, taker=self.taker_b,
        )
        payload = self.assert_business_failure(
            self.claim(order.order_no, key="claim-key-000011"), ALREADY_TAKEN
        )
        self.assertIn(SELF_CLAIMED_FRAGMENT, payload["msg"])
        self.assert_no_logs(order.order_no)

    def test_an_order_without_a_taker_price_is_1013(self):
        """没有接单价的厅单 ⇒ **`1013`**，且文案说清是"没有接单价"那一支（⛔ 不是 `1002`）。"""
        order = self._order(
            self.game_his, "20261008000000000203", price=None
        )
        payload = self.assert_business_failure(
            self.claim(order.order_no, key="claim-key-000012"), GRAB_LIMIT
        )
        self.assertIn("没有接单价", payload["msg"])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.main_of(order).user_id)

    def test_forbid_self_service_is_1013_with_its_own_message(self):
        """⭐ 同码的**另一支**：`禁自助接单` —— 文案必须能指出是这一支（四支共用一个码）。"""
        StaffProfile.objects.filter(user=self.taker_a).update(
            grab_forbid_self_service=True
        )
        payload = self.assert_business_failure(
            self.claim(ORDER_NO_HIS, key="claim-key-000013"), GRAB_LIMIT
        )
        self.assertIn("禁止自助接单", payload["msg"])
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.WAITING_GRAB)
        self.assert_no_logs(ORDER_NO_HIS)


class ClaimOrphanOrderTests(_ClaimFixture):
    """⭐ spec §9-14：**没有 MAIN 绩效行**的订单 ⇒ `1002`，⛔ **不是 500**。

    ## 这条为什么不是"顺手加的"

    `claim.py` **明文**把"一条 MAIN 行都没有"与"被别人抢走"做成**不可区分**
    （条件更新 `rowcount == 0` 时它刻意**不加** `exists()` 兜底：不变量被破坏时该响亮）。
    而对外契约要的是"不在大厅的单一律 `1002`"——孤儿订单连 `search_hall` 的入选条件都
     join 不上，它**本来就不在厅**。⇒ 对外侧必须在**那一条支路上**把成因分开，
    用的判据仍是领域层自己那一份（`claim._has_taker`）。

    ⚠️ 判别力：把那个分支去掉，本用例会拿到 `1012`（⛔ 不是 500 —— 所以"不是 500"这一半
    不能只靠 `assertNotEqual(500)`，必须**点名 `1002`**）。
    """

    def test_an_order_without_any_main_performance_is_1002(self):
        order = self._order(
            self.game_his, "20261008000000000301", with_main_performance=False
        )
        self.assertFalse(
            Performance.objects.filter(order=order, emp_type=EmpType.MAIN).exists()
        )
        payload = self.assert_business_failure(
            self.claim(order.order_no, key="claim-key-000014"), BUSINESS
        )
        self.assertEqual(payload["msg"], ClaimScopeTests._msg_of_missing())
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)
        self.assert_no_logs(order.order_no)

    def test_such_an_order_is_not_in_the_hall_either(self):
        """⭐ 与大厅那条判据**同源**的对照：孤儿订单**不进大厅**（所以抢它回 `1002` 是自洽的）。"""
        order = self._order(
            self.game_his, "20261008000000000302", with_main_performance=False
        )
        response = self.anonymous.get(
            "/api/taker/v1/hall/", HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        rows = self.body(response)["data"]["results"]
        nos = {row["order_no"] for row in rows}
        self.assertNotIn(order.order_no, nos)
        # 对照：同款游戏里那张**有** MAIN 行的单**在**（否则"不在"可能只是大厅空了）。
        self.assertIn(ORDER_NO_HIS, nos)


class ClaimIdempotencyTests(_ClaimFixture):
    """⭐ spec §9-4：幂等 × 抢单（四态 + **反例**）。"""

    def test_a_missing_key_is_1000_and_nothing_changes(self):
        payload = self.assert_business_failure(self.claim(ORDER_NO_HIS, key=None), VALIDATION)
        self.assertEqual(payload["msg"], "缺少幂等键")
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.main_of(self.order_his).user_id)
        self.assert_no_logs(ORDER_NO_HIS)
        # 台账里一行都不该有（判据打在**库里的行数**上，⛔ 不看日志）。
        self.assertEqual(TakerApiIdempotency.objects.count(), 0)

    def test_a_key_outside_the_length_contract_is_1000(self):
        for bad in ("x" * (MIN_KEY - 1), "y" * (MAX_KEY + 1)):
            with self.subTest(length=len(bad)):
                payload = self.assert_business_failure(
                    self.claim(ORDER_NO_HIS, key=bad), VALIDATION
                )
                self.assertIn("幂等键长度", payload["msg"])
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.WAITING_GRAB)

    def test_replaying_the_same_key_returns_the_same_envelope(self):
        """⭐ 同键重发 ⇒ 回放**逐字相同**的 `code` / `msg` / `data`，且**只抢了一次**。

        ⚠️ 断言打在**解析后的载荷**上，⛔ **不是** `response.content` 字节：
        `response_snapshot` 是 **JSONB** 列，PostgreSQL **不保键序**（`idempotency.py` 明文登记）
        ⇒ 字节比较会以"键序被库归一化"这种**与被测行为无关**的原因红。
        """
        key = "claim-key-000015"
        first = self.claim(ORDER_NO_HIS, key=key)
        first_payload = self.body(first)
        self.assertEqual(first_payload["code"], OK, first_payload)

        second = self.claim(ORDER_NO_HIS, key=key)
        second_payload = self.body(second)
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second_payload["code"], first_payload["code"])
        self.assertEqual(second_payload["msg"], first_payload["msg"])
        self.assertEqual(second_payload["data"], first_payload["data"])
        # ⭐ "只抢了一次"的库内读数：一条 MAIN 行、一个接单人、一条留痕。
        rows = Performance.objects.filter(
            order=self.order_his, emp_type=EmpType.MAIN, claimed_at__isnull=False
        )
        self.assertEqual(rows.count(), 1)
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO_HIS).count(), 1)

    def test_a_different_key_after_success_is_1012(self):
        self.assert_claimed(self.claim(ORDER_NO_HIS, key="claim-key-000016"))
        payload = self.assert_business_failure(
            self.claim(ORDER_NO_HIS, key="claim-key-000017"), ALREADY_TAKEN
        )
        self.assertIn(SELF_CLAIMED_FRAGMENT, payload["msg"])
        self.assertEqual(OperationLog.objects.filter(order_no=ORDER_NO_HIS).count(), 1)

    def test_the_same_key_with_a_different_endpoint_is_1000(self):
        """同一个键配**别的路径** ⇒ `1000`（唯一键是 `(app, key)`、**不含**端点）。

        ⚠️ 这条防的是"照字面只看 `request_hash`"那种简化：两个端点的请求体**都是空**
        ⇒ 摘要逐字相同，只看摘要就会把上一次的响应回放给这一次（= 静默给出错误答案）。
        """
        key = "claim-key-000018"
        self.assert_claimed(self.claim(ORDER_NO_HIS, key=key))
        # 同键、同体（空）、**同一个方法**，但路径指另一张单（那张单仍在厅）。
        other = self._order(self.game_his, "20261008000000000303")
        payload = self.assert_business_failure(
            self.claim(other.order_no, key=key), VALIDATION
        )
        self.assertEqual(payload["msg"], "同一个幂等键配了不同的请求体")
        other.refresh_from_db()
        self.assertEqual(other.status, OrderStatus.WAITING_GRAB)

    def test_a_failed_attempt_can_be_retried_with_the_same_key_and_really_claims(self):
        """⭐⭐ **反例（spec §9-4 点名必须有）**：条件更新成功但状态机拒绝 ⇒ 同键重试**真抢到**。

        ## 构造

        1. 先把该账号的 `grab_hall.take_order` 撤掉 ⇒ 走到"**绩效条件更新成功、状态机拒绝**"
           那一支（`IllegalTransition`）⇒ 响应 `1002`；
        2. 把节点**还回去**（模拟管理员当场补授权 / 或换一个持有节点的账号）；
        3. **用同一个键**重发**同一个请求** ⇒ 必须 `code=0` 并真的抢到。

        ## 判别力（这条是本文件最承重的一条）

        `idempotency._run` 的口径是"业务码 ≠ 0 ⇒ 连占位行一起回滚（这个键下次可重试）"，
        **除非** handler 声明过"副作用已落库"（`mark_side_effect`）。
        ⇒ 若抢单 handler 调了它（那是**错**的：半路失败的那半截写入已经被
        `transaction.set_rollback(True)` 回滚了、库里什么都没留下），第一发会落快照
        ⇒ 第二发**回放那次失败**（`1002`）⇒ 本用例红。
        ⚠️ 反过来也钉住了 `transaction.set_rollback(True)` 那两处：少了它，第一发会把
        "绩效行有人、订单还停在待抢单"的脏行**提交**掉 ⇒ 第二发拿到 `1012`/`1002` ⇒ 也红。
        """
        key = "claim-key-000019"
        self.withdraw_take_order_node(self.taker_a)
        first = self.assert_business_failure(self.claim(ORDER_NO_HIS, key=key), BUSINESS)
        # 第一发**一行都没留下**（半截写入被显式回滚）。
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(self.main_of(self.order_his).user_id)
        self.assert_no_logs(ORDER_NO_HIS)
        self.assertEqual(TakerApiIdempotency.objects.filter(key=key).count(), 0)

        # 把节点还回去，然后**同一个键**重发。
        grant_nodes(self.taker_a, TAKE_ORDER_NODE)
        data = self.assert_claimed(self.claim(ORDER_NO_HIS, key=key))
        self.assertEqual(data["order_no"], ORDER_NO_HIS)
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.main_of(self.order_his).user_id, self.taker_a.pk)
        # ⚠️ 第一发的失败**不是**"同一个键的正在进行中"（第四态）：那句话说明占位行被提交了。
        self.assertNotEqual(first["msg"], "同一个幂等键的请求正在处理中")

    def test_a_placeholder_row_for_the_same_key_is_1001(self):
        """第四态：同键、同体，但台账里的快照**是空的**（上一笔还在执行 / 崩了）⇒ `1001`。

        ⚠️ 这是**真实存在**的形态（占位行由 `_run` 在调业务处理器**之前**建），本用例
        直接把那一行摆出来（⛔ 不是伪造响应）。
        """
        key = "claim-key-000020"
        TakerApiIdempotency.objects.create(
            app=self.app_a,
            key=key,
            method="POST",
            path=CLAIM_TEMPLATE.format(order_no=ORDER_NO_HIS),
            # ⚠️ 摘要取**空 body** 的 sha256（本夹具的请求就是空 body）。
            request_hash=hashlib.sha256(b"").hexdigest(),
            response_snapshot={},
            status_code=200,
        )
        payload = self.assert_business_failure(self.claim(ORDER_NO_HIS, key=key), CONFLICT)
        self.assertEqual(payload["msg"], "同一个幂等键的请求正在处理中")
        # 一笔业务都没发生（占位行那一支在调 handler **之前**就返回了）。
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.WAITING_GRAB)
        self.assert_no_logs(ORDER_NO_HIS)

    def test_the_lost_race_on_the_unique_constraint_is_1001_not_500(self):
        """⭐ spec §9-3 的同键那一半：**并发**下第二笔撞唯一约束 ⇒ `1001`（⛔ 不是 500）。

        ## 这一条怎么"如实"走到那条支路

        真实并发里，两笔同键请求几乎同时过了"查现有行"（都没查到）⇒ 后到的 `INSERT` 撞
        `uniq_taker_idem_app_key`。要**确定性地**到达同一条支路，做法是本仓既有先例
        （`tests/test_openapi_order_write.py`）：**预先摆一行同键台账行** + 把
        `_find_existing` 打桩成"没查到" ⇒ 请求必然走到 `INSERT` 撞唯一约束。
        ⛔ 这不是"把两次顺序调用说成并发"：它证明的是**那条支路自己的行为**
        （内层 savepoint 拿住了 `IntegrityError`、外层事务没被弄坏、答复是 `1001`）。
        ⚠️ 打桩打在**被查找的那个模块**上（`apps.openapi.idempotency`，`E-0029`）。
        """
        key = "claim-key-000021"
        existing = TakerApiIdempotency.objects.create(
            app=self.app_a,
            key=key,
            method="POST",
            path=CLAIM_TEMPLATE.format(order_no=ORDER_NO_HIS),
            request_hash="0" * 64,
            response_snapshot={},
            status_code=200,
        )
        with mock.patch.object(
            openapi_idempotency, "_find_existing", return_value=None
        ):
            payload = self.assert_business_failure(
                self.claim(ORDER_NO_HIS, key=key), CONFLICT
            )
        self.assertEqual(payload["msg"], "同一个幂等键的请求正在处理中")
        # ⚠️ 两次"真的查库"：内层 savepoint 若没拿住 `IntegrityError`，这里会抛
        #    `TransactionManagementError`（事务已被弄坏）⇒ 本用例以 ERROR 红。
        existing.refresh_from_db()
        self.assertEqual(existing.request_hash, "0" * 64)   # 原来那一行没被改坏
        self.assertEqual(existing.response_snapshot, {})
        self.order_his.refresh_from_db()
        self.assertEqual(self.order_his.status, OrderStatus.WAITING_GRAB)


class ClaimNodeWithdrawalTests(_ClaimFixture):
    """⭐ spec §9-6：`nodes` 来自**账号**（⛔ 不是常量）——撤掉节点 ⇒ 抢单立刻不通。

    ## 为什么这一条能分辨"常量节点集"那种实现

    若 handler 传的是常量全集（例如上家侧那个 `OPEN_API_FULL_NODES`），状态机那道闸
    对本路径**恒真** ⇒ 撤掉节点后**照样抢得到** ⇒ 本用例红。
    ⚠️ 夹具只给 `grab_hall.take_order` 一个节点（那条边还有 `order.change_status` 可选）
    ⇒ 撤掉它之后交集为**空**。
    """

    def test_withdrawing_the_node_blocks_the_claim_and_leaves_no_half_write(self):
        # ① 对照组：**有**节点时抢得到（⛔ 缺它，撤掉节点后的失败可能只是因为别的原因）。
        self.assert_claimed(self.claim(ORDER_NO_HIS, key="claim-key-000022"))
        # ② 撤掉节点，抢另一张（同款游戏、仍在厅）⇒ 立刻不通。
        other = self._order(self.game_his, "20261008000000000401")
        self.withdraw_take_order_node(self.taker_a)
        payload = self.assert_business_failure(
            self.claim(other.order_no, key="claim-key-000023"), BUSINESS
        )
        # ⚠️ 状态机拒绝 ⇒ `1002`（spec §5.4），文案来自状态机（说明差的是哪一件事）。
        self.assertIn("权限", payload["msg"])
        # ③ ⭐ **没有半截写入**（`E-0022`：块内裸 return 会把绩效那半截提交掉）。
        other.refresh_from_db()
        self.assertEqual(other.status, OrderStatus.WAITING_GRAB)
        main = self.main_of(other)
        self.assertIsNone(main.user_id)
        self.assertIsNone(main.claimed_at)
        self.assert_no_logs(other.order_no)

    def test_granting_the_node_back_makes_it_work_again(self):
        """节点是**当场生效**的（判据读库、不缓存）——两条方向都在这里钉住。"""
        self.withdraw_take_order_node(self.taker_a)
        self.assert_business_failure(self.claim(ORDER_NO_HIS, key="claim-key-000024"), BUSINESS)
        grant_nodes(self.taker_a, TAKE_ORDER_NODE)
        self.assert_claimed(self.claim(ORDER_NO_HIS, key="claim-key-000025"))


class ClaimMalformedPathTests(_ClaimFixture):
    """畸形路径段（带转换器的路由**不在** `test_no_route_500` 那条腿的覆盖内 ⇒ 这里补 HTTP 级判据）。"""

    def test_a_nul_in_the_order_no_is_404_not_500(self):
        """`%00` 是 `unquote_to_bytes` 之后的**真 NUL**：共享转换器排除控制字符
        ⇒ 畸形路径段**不匹配路由** ⇒ **404**（⛔ 不是 500、也不进视图）。

        ⚠️ 判别力：把 `path()` 里的 `<order_no:order_no>` 换成裸 `<str:order_no>`，
        NUL 会进 `filter(order_no=…)` ⇒ PostgreSQL `DataError` ⇒ **HTML 500** ⇒ 本用例红。
        """
        response = self.anonymous.post(
            "/api/taker/v1/hall/20261008000000000101%00/claim/",
            data="",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            HTTP_IDEMPOTENCY_KEY="claim-key-000026",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, NOT_FOUND, response.content[:200])

    def test_a_very_long_order_no_does_not_500(self):
        """超长订单号（远超 `order_no` 列宽 20）：⛔ **不许 500**。

        ⚠️ 正确答复是 `1002`（取不到那张单）或 `1000`（幂等台账的 `path` 列装不下那条路径
        ——`idempotency._overlong_path_response` 那道闸）：**两者都是业务码**，
        本用例只钉"⛔ 不是 5xx"这一件事，⛔ 不把某一种当成契约。
        """
        response = self.claim("9" * 300, key="claim-key-000027")
        self.assertNotEqual(response.status_code, 500, response.content[:300])
        self.assertLess(response.status_code, 500, response.content[:300])

    def test_a_credential_less_call_to_the_claim_route_is_401(self):
        """⭐ 对照组：**没有凭据**打这条真路由 ⇒ `401`（证明上面那两条不是"路径不通"的巧合）。"""
        response = self.anonymous.post(
            CLAIM_TEMPLATE.format(order_no=ORDER_NO_HIS),
            data="",
            content_type="application/json",
            HTTP_IDEMPOTENCY_KEY="claim-key-000028",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(
            self.body(response)["code"], UNAUTHENTICATED, response.content[:200]
        )
