"""T6：打手侧**对外**端点第二批之二 —— **改状态**（`POST /api/taker/v1/orders/<order_no>/status/`）。

spec：`docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 5 行 · §5.3-2（范围判据）·
§5.3-4（⭐ **目标态白名单**）· §5.4（失败档）· §9 的 1（四路对照的 ③）/ 7（P3 白名单）/ 14（孤儿订单）。

## 本文件证什么（逐条都能失败）

| # | 判据 | spec |
|---|---|---|
| 1 | ⭐⭐ **白名单判据打在白名单那一层**：**白名单外的合法枚举值**（`IN_PROGRESS` / `COMPLETED` / `REFUNDED` / `WAITING_GRAB` / `UNASSIGNED`）⇒ `1000` + **点名白名单** —— 而这些值**能过**序列化器的枚举校验 ⇒ 只有白名单拦得住它们 | §5.3-4 / §9-7 |
| 2 | ⭐ **三个白名单目标态各真的变**（`PENDING_REVIEW` / `ISSUE` / `PAUSED`）⇒ 库里 `status` 真变了，不是"回了 200 而已" | §9-7 |
| 3 | ⭐ **非枚举值 ⇒ `1000`**（中文标签「已完成」/ 未知串 / 缺字段）—— 这一档是**序列化器**拦的，与第 1 条**刻意分开** | §5.4 |
| 4 | ⭐⭐ **范围判据四路对照的 ③**：对**别人接手的单**改状态 ⇒ `1002` **且库里那张单逐字段一个都没变**（含 MAIN 绩效行、含**没有**留痕行）；⭐ 对照：改**自己的单** ⇒ 成功 | §9-1-③ |
| 5 | ⭐ **「他是当前接手人」不是「游戏范围内」**：造一张**在他游戏权限内、但被别人接手**的单 ⇒ 改它 `1002`（⛔ 只靠游戏范围实现就必红） | §5.3-2 |
| 6 | ⭐ **孤儿订单**（没有 MAIN 绩效行）⇒ `1002`，⛔ **不是 500** | §9-14 |
| 7 | ⭐ **节点门来自绑定账号**：撤掉 `order.change_status` ⇒ 改状态**立刻不通**，且库里**没有半截写入** | §5.3-4 |
| 8 | ⭐ **幂等 × 改状态**：同键重发 ⇒ 回放（判据打在解析后的 `code`/`msg`/`data` 上）；换键改同一个"已经在那个目标态"⇒ `1002`（状态机自环） | §9-4 同款 |
| 9 | ⭐ **留痕**：成功 ⇒ **恰好一行** `order.transition`（含 actor / 渠道可辨识 / x）+ **不含任何凭据值**；失败 ⇒ **0 行** | §6 硬规则 2 |
| 10 | ⭐ **装配**（§9-15）：`authentication_classes` / `permission_classes = []` / `throttle_classes` **恰好**那三件 | §9-15 |
| 11 | **鉴权面**：无令牌 / 上家侧令牌 / 会话 ⇒ `401`；⭐ 对照组 | §9-2 |
| 12 | **畸形入参**：非对象请求体 / `remark` 超 255 / NUL 路径段（⇒ 404）/ 超长单号（⇒ `1002`）⛔ 都不许 500 | `test_no_route_500` 的已知盲区 |

## ⚠️ 证明力边界（如实写清楚）

- 令牌**一律从 `/api/taker/oauth/token` 换**（走真路由 + 真 `Client`，⛔ 不手搓夹具）；
- 第 1 条的判别力来自"**白名单外的合法枚举值**"这一档：把 `TAKER_STATUS_WHITELIST` 换成
  别的集合、或者把白名单那段判据整条删掉 ⇒ 本条**必红**（读数见 T6 报告）；
  而"中文标签"那一档**只**证明枚举校验还在（它与第 1 条**各自**证明一件事）；
- 夹具一律**合成值**（`AGENTS.md` §5-C），来源 IP 取文档保留段（⛔ 不是任何真实客户端）。
"""
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile, WxPusherBinding, WxPusherNotice
from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.openapi.models import ApiApp
from apps.takerapi.models import TakerApiIdempotency, TakerApp
from tests.p5_auth_helpers import grant_nodes, make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/takerapi/urls.py` 逐字一致；⚠️ 带尾斜杠）─────────────────────
TOKEN_PATH = "/api/taker/oauth/token"
OPENAPI_TOKEN_PATH = "/api/open/oauth/token"
OPENAPI_ORDERS_PATH = "/api/open/v1/orders/"
STATUS_TEMPLATE = "/api/taker/v1/orders/{order_no}/status/"

GRANT_TYPE = "client_credentials"

# ── 合成凭据（⛔ 与任何真实租户的 appKey / secret 无关）────────────────────────
TAKER_A_KEY = "takerstatuskey00001"
TAKER_A_SECRET = "taker-status-secret-00000001"
TAKER_B_KEY = "takerstatuskey00002"
TAKER_B_SECRET = "taker-status-secret-00000002"
OPENAPI_KEY = "openstatuskey0000001"
OPENAPI_SECRET = "open-status-secret-00000001"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.81"

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字）──────────────────────────────────
OK = 0
VALIDATION = 1000
BUSINESS = 1002
UNAUTHENTICATED = 401
NOT_FOUND = 404

#: 该账号持的节点（**字面量**：从被测模块 import 会让"节点名被改坏"在用例里看不见）。
#: ⭐ 只给这一个：`{PENDING_REVIEW, ISSUE, PAUSED}` 三个目标态在状态机上的节点集都含它
#: （`order_status._TARGET_NODES`：前两个另含 `order.submit_work`，但**任一命中即放行**）
#: ⇒ "撤掉它 ⇒ 改状态立刻不通"这条判据才有判别力（若同时给了第二条，撤掉它照样能改）。
CHANGE_STATUS_NODE = "order.change_status"

#: ⭐⭐ **P3 白名单**（spec §5.3-4 / §9-7 / §1 的 P3 裁定）——**字面量**写在这里
#: （⛔ 不从 `order_status_views.TAKER_STATUS_WHITELIST` 派生：派生是自指恒真，`E-0024`）。
WHITELISTED = ("PENDING_REVIEW", "ISSUE", "PAUSED")

#: ⛔ **白名单外的合法枚举值**：五个都在 `OrderStatus` 里、都能过序列化器的枚举校验，
#: 而在状态机上**都有边**（D-0147 把状态机整体放开）⇒ 它们被拒**只能**是白名单干的。
#: ⚠️ 这一格是本文件**最重要**的判据的靶子（见 `WhitelistIsTheGateTests`）。
NON_WHITELISTED_ENUM_VALUES = (
    "IN_PROGRESS",
    "COMPLETED",
    "REFUNDED",
    "WAITING_GRAB",
    "UNASSIGNED",
)

#: 白名单那一句里**必须出现**的三个中文名（spec §5.3-4 要求"点名白名单"）。
#: ⚠️ 中文名取自 `OrderStatus.choices`（`PENDING_REVIEW` = 待验收 / `ISSUE` = 问题单 /
#: `PAUSED` = 已暂停），本文件**写死**这三个词：⛔ 不 import 那张表（否则文案改了也不会红）。
WHITELIST_LABELS = ("待验收", "问题单", "已暂停")

#: 留痕的字面值：`order.transition` 的 `log_type` = `LogType.ORDER` = **10**
#: （`apps/audit/actions.py:767` 的 `"order.transition": LogType.ORDER`）。
#: ⚠️ 写字面量而不是 `ACTION_LOG_TYPES` / `LogType.ORDER`：后者是自指（常量被改坏时不会红）。
TRANSITION_LOG_TYPE = 10

#: 「这一笔来自打手对接接口」在留痕里的**载体**（本端点写在 `actor` 上；`reason` 被
#: 调用方给的 `remark` 占了——见 `api/order_status_views.py` 的模块 docstring）。
TAKER_ACTOR_PREFIX = "打手侧应用"

#: 幂等键长度契约的两端（与 `apps/openapi/idempotency.py` 的对外契约同值）。
MIN_KEY = 8
MAX_KEY = 128


class _StatusFixture(TenantTestCase):
    """两个打手（各持**不同**游戏权限）+ 各一张进行中的单 + 两套凭据。

    ⚠️ 甲对**两款游戏都有权限**是有意的：第 5 条判据要造的那张"范围内但被别人接手"的单
    必须落在他有权限的游戏里——否则用例会以"范围外"这个**另一个原因**变绿（`E-0004` 形态）。
    """

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        self.game_a = Game.objects.create(name="改状态测试游戏甲")
        self.game_b = Game.objects.create(name="改状态测试游戏乙")

        self.taker_a = self._make_taker("status-taker-a", games=[self.game_a, self.game_b])
        self.taker_b = self._make_taker("status-taker-b", games=[self.game_a, self.game_b])
        self.app_a = self._make_app(
            "改状态测试应用甲", TAKER_A_KEY, TAKER_A_SECRET, self.taker_a
        )
        self.app_b = self._make_app(
            "改状态测试应用乙", TAKER_B_KEY, TAKER_B_SECRET, self.taker_b
        )
        self.token_a = self.issue_token(self.app_a)
        self.token_b = self.issue_token(self.app_b)

        #: ⭐ 对照：**他自己接手**的一张单（改它必须成功）。
        self.order_mine = self._order(
            self.game_a, "20261008000000000501", taker=self.taker_a, price="88.00"
        )
        #: ⭐⭐ 在**甲的**游戏权限范围内、但 MAIN 绩效行归**乙** ⇒ 只有"他是当前接手人"
        #: 那份判据拦得住它（"游戏范围内"会**放行**）。
        self.order_others = self._order(
            self.game_a, "20261008000000000502", taker=self.taker_b, price="66.00"
        )

    # ── 造物 ───────────────────────────────────────────────────────────
    def _make_taker(self, username, *, games):
        """普通账号 + 显式游戏权限 + **只挂** `order.change_status`（理由见那个常量的注释）。"""
        user = User.objects.create_user(username=username, password="pw-12345678")
        group, _ = Group.objects.get_or_create(name="代练员")
        user.groups.add(group)
        StaffProfile.objects.create(user=user).games.set(games)
        grant_nodes(user, CHANGE_STATUS_NODE)
        return user

    def withdraw_change_status_node(self, user):
        """把 `order.change_status` 从该账号所在组的角色上撤掉（spec §5.3-4 的判据动作）。"""
        from apps.rbac.models import PermissionNode, Role

        node = PermissionNode.objects.get(code=CHANGE_STATUS_NODE)
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
        self, game, order_no, *, status=OrderStatus.IN_PROGRESS, taker=None,
        price="88.00", with_main_performance=True,
    ):
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
                temp_amount=Decimal(price) if price is not None else Decimal("0.00"),
            )
        return order

    # ── 令牌：**只从取令牌端点拿**（spec §9-2 的要求）───────────────────
    def issue_token(self, app, *, path=TOKEN_PATH) -> str:
        response = self.anonymous.post(
            path,
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

    def make_api_app(self):
        return ApiApp.objects.create(
            name="改状态测试上家应用甲",
            app_key=OPENAPI_KEY,
            app_secret=OPENAPI_SECRET,
            created_by=self.taker_a,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def change_status(
        self, order_no, *, key, body=None, token=None, path=None,
        remote_addr=SOURCE_IP, raw=None,
    ):
        """`POST …/status/`。

        ⚠️ **必须给确定的字节**（`json.dumps` 的 `separators` 固定）：Django 测试客户端在
        `content_type` 缺省时用 multipart，而 multipart 的 boundary **每次随机** ⇒
        两次"同一个请求"的 `request.body` 不同 ⇒ 幂等判据会以 `1000`（同键异体）**假红**
        （T5 抢单那条已实测踩过）。
        ⚠️ `key=None` ⇒ **不带** `Idempotency-Key` 头（"缺键 ⇒ `1000`"那一档要用）。
        """
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token or self.token_a}"}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        payload = {"status": "PENDING_REVIEW"} if body is None else body
        if raw is not None:
            data, content_type = raw, "application/json"
        else:
            data, content_type = json.dumps(payload, separators=(",", ":")), "application/json"
        return self.anonymous.post(
            path or STATUS_TEMPLATE.format(order_no=order_no),
            data=data,
            content_type=content_type,
            REMOTE_ADDR=remote_addr,
            **headers,
        )

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def assert_business_failure(self, response, code):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        self.assertIsNone(payload["data"], payload)
        return payload

    def assert_changed(self, response, order_no, expected_status):
        """成功路径：HTTP 200 + `code=0` + `data` 恰好三个键 + **库里真变了**。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        data = payload["data"]
        self.assertEqual(set(data), {"order_no", "status", "status_text"}, data)
        self.assertEqual(data["order_no"], order_no)
        self.assertEqual(data["status"], expected_status)
        order = Order.objects.get(order_no=order_no)
        self.assertEqual(order.status, expected_status)
        self.assertEqual(data["status_text"], order.get_status_display())
        return data

    @staticmethod
    def order_snapshot(order_no):
        """一行订单的**全列快照**（`_meta.concrete_fields`，含 pk 与四个时间列）。

        ⚠️ 判"那张单一个字段都没变"必须用它，⛔ 不是挑几个字段比一比：挑字段的写法在
        "改了一列而我恰好没挑到"时**静默绿**（`E-0064` 同族；T5 抢单那条同款）。
        ⚠️ 取 `attname`（外键取**主键值**），⛔ 不取对象本身（那是刷新前的缓存）。
        """
        order = Order.objects.get(order_no=order_no)
        return {
            field.name: getattr(order, field.attname)
            for field in Order._meta.concrete_fields
        }

    @staticmethod
    def perf_snapshot(order_no):
        return {
            field.name: getattr(
                Performance.objects.get(order__order_no=order_no, emp_type=EmpType.MAIN),
                field.attname,
            )
            for field in Performance._meta.concrete_fields
        }

    def assert_no_logs(self, order_no):
        self.assertEqual(
            OperationLog.objects.filter(order_no=order_no).count(),
            0,
            "失败路径留下了留痕行（「没改成、日志却写了」）",
        )


class WhitelistIsTheGateTests(_StatusFixture):
    """⭐⭐ **本文件最重要的判据**：P3 白名单的判据**打在白名单那一层**（spec §5.3-4）。

    ## 判别力（这条用例**专门**钉那件事）

    靶子是**白名单外的合法枚举值**（`IN_PROGRESS` / `COMPLETED` / `REFUNDED` /
    `WAITING_GRAB` / `UNASSIGNED`）：它们**都是** `OrderStatus` 的成员 ⇒
    序列化器的 `ChoiceField` **放行**它们，而在状态机上（D-0147 整体放开）它们**都有边**
    ⇒ 它们被拒**只能**是下面的白名单判据干的。

    ⛔ 若实现把白名单写成了"序列化器只收那三个值"（退化成字段校验），
    这一类的用例要么**测不到白名单**（它连 `1000` 都收得到，但那条 `1000` 来自字段校验，
    换掉白名单也不会红）、要么整段判据**消失**。所以那三个白名单目标态的**正对照**
    （`WhitelistedTargetsReallyChangeTests`）与这里的**反例**必须一起在。
    """

    def test_a_valid_enum_outside_the_whitelist_is_rejected_and_names_the_whitelist(self):
        for value in NON_WHITELISTED_ENUM_VALUES:
            with self.subTest(value=value):
                payload = self.assert_business_failure(
                    self.change_status(
                        self.order_mine.order_no,
                        key=f"probe-wl-{value.lower()}",
                        body={"status": value},
                    ),
                    VALIDATION,
                )
                # ⭐ **点名白名单**（spec §5.3-4）：三个中文名都要在那一句里。
                for label in WHITELIST_LABELS:
                    self.assertIn(label, payload["msg"], payload)

    def test_a_rejected_out_of_whitelist_value_leaves_the_order_untouched(self):
        """⭐ 拒了之后**库里一个字段都没变**（⛔ 不是"回了 1000 但状态也改了"）。"""
        before = self.order_snapshot(self.order_mine.order_no)
        for value in NON_WHITELISTED_ENUM_VALUES:
            with self.subTest(value=value):
                self.change_status(
                    self.order_mine.order_no,
                    key=f"probe-wl2-{value.lower()}",
                    body={"status": value},
                )
        self.assertEqual(self.order_snapshot(self.order_mine.order_no), before)

    def test_the_whitelist_is_the_only_thing_that_can_reject_a_valid_enum(self):
        """⭐ **对照的正向表述**：被拒的那几个值都**真的在** `OrderStatus` 里。

        ⚠️ 这条不是同义反复：它把"靶子确实是合法枚举值"这件事**跑一遍**（上面的用例**假设**
        了它）——若哪天 `OrderStatus` 删掉某个成员，靶子就落到"非法枚举值"那一档，
        上面那条会以**字段校验**的方式变绿（假绿）。
        """
        for value in NON_WHITELISTED_ENUM_VALUES:
            with self.subTest(value=value):
                self.assertIn(value, OrderStatus.values)

    def test_the_three_whitelisted_values_are_valid_enums_too(self):
        """⭐ 反向：白名单那三个也是合法枚举值（⇒ 两边**都过**序列化器，差别只在白名单）。"""
        for value in WHITELISTED:
            with self.subTest(value=value):
                self.assertIn(value, OrderStatus.values)


class WhitelistedTargetsReallyChangeTests(_StatusFixture):
    """⭐ spec §9-7：`{PENDING_REVIEW, ISSUE, PAUSED}` **各配一条**，且**真变**。

    ⚠️ 只断"回了 `code=0`"是不够的（一个"什么都不写但回成功"的实现也能绿）⇒
    每条都**重读数据库**（`assert_changed` 里那句 `Order.objects.get(...)`）。
    ⚠️ 三个目标态**各建一张自己的单**：从前一个目标态往下走会让后一条依赖前一条的顺序
    （而 `ISSUE → PAUSED` 这类边今天都在，但那是**状态机**的事、与白名单无关）。
    """

    def test_pending_review_really_changes_the_status(self):
        self.assert_changed(
            self.change_status(
                self.order_mine.order_no,
                key="probe-target-pending",
                body={"status": "PENDING_REVIEW"},
            ),
            self.order_mine.order_no,
            "PENDING_REVIEW",
        )

    def test_issue_really_changes_the_status(self):
        order = self._order(self.game_a, "20261008000000000503", taker=self.taker_a)
        self.assert_changed(
            self.change_status(
                order.order_no, key="probe-target-issue",
                body={"status": "ISSUE", "remark": "  账号掉线  "},
            ),
            order.order_no,
            "ISSUE",
        )
        log = OperationLog.objects.get(order_no=order.order_no, action="order.transition")
        self.assertEqual(log.reason, "账号掉线")

    def test_worker_issue_requires_a_nonblank_remark(self):
        order_no = self.order_mine.order_no
        before = self.order_snapshot(order_no)
        for index, fields in enumerate(({}, {"remark": ""}, {"remark": " \t\n　"})):
            with self.subTest(fields=fields):
                payload = self.assert_business_failure(
                    self.change_status(
                        order_no, key=f"probe-blank-issue-{index}",
                        body={"status": "ISSUE", **fields},
                    ),
                    VALIDATION,
                )
                self.assertEqual(payload["msg"], "请输入问题原因")
                self.assertEqual(self.order_snapshot(order_no), before)

    def test_worker_app_report_does_not_push_back_to_the_same_worker(self):
        WxPusherBinding.objects.create(user=self.taker_a, app_id="932", uid="UID_synthetic_taker_api")
        with override_settings(
            WXPUSHER_SUBSCRIBE_TENANT_SCHEMA=self.tenant.schema_name,
            WXPUSHER_APP_ID="932", WXPUSHER_APP_TOKEN="AT_synthetic",
        ):
            order_no = self.order_mine.order_no
            self.assert_changed(
                self.change_status(
                    order_no, key="report-issue-from-worker-app",
                    body={"status": "ISSUE", "remark": "账号掉线"},
                ),
                order_no, "ISSUE",
            )
        self.assertFalse(WxPusherNotice.objects.filter(order_no=order_no).exists())

    def test_paused_really_changes_the_status(self):
        order = self._order(self.game_a, "20261008000000000504", taker=self.taker_a)
        self.assert_changed(
            self.change_status(order.order_no, key="probe-target-paused", body={"status": "PAUSED"}),
            order.order_no,
            "PAUSED",
        )

    def test_completed_is_never_reachable_through_this_endpoint(self):
        """⭐ P3 的核心（spec §1）：**「已完成」只能我方验收** ⇒ 打手永远改不到。

        ⚠️ 这条与上面那三条**互为正反**：上面证明"白名单内真能改"，这条证明"白名单外的
        `COMPLETED` 改不动"——两条一起才说明"白名单是有内容的、⛔ 不是'什么都放行'"。
        """
        order = self._order(self.game_a, "20261008000000000505", taker=self.taker_a)
        self.assert_business_failure(
            self.change_status(order.order_no, key="probe-completed", body={"status": "COMPLETED"}),
            VALIDATION,
        )
        self.assertEqual(Order.objects.get(order_no=order.order_no).status, OrderStatus.IN_PROGRESS)


class NonEnumValuesAreFieldValidationTests(_StatusFixture):
    """非枚举值 ⇒ `1000`（**这一档是序列化器拦的**，与白名单那一档刻意分开写）。

    ⚠️ 分开的理由：两条答复的**码相同**（都是 `1000`），但**成因不同**——
    本类证明"字段校验还在"，`WhitelistIsTheGateTests` 证明"白名单**另有**一道"。
    混成一条会让"白名单被删掉"这件事**看不出来**（中文标签照样被枚举字段拦住）。
    """

    def test_a_chinese_label_is_rejected(self):
        payload = self.assert_business_failure(
            self.change_status(
                self.order_mine.order_no, key="probe-label", body={"status": "已完成"}
            ),
            VALIDATION,
        )
        # ⚠️ 文案**不是**白名单那一句（那三个中文名一个都不该出现）：这是枚举校验的话。
        for label in WHITELIST_LABELS:
            self.assertNotIn(label, payload["msg"], payload)

    def test_an_unknown_string_is_rejected(self):
        self.assert_business_failure(
            self.change_status(
                self.order_mine.order_no, key="probe-unknown", body={"status": "NOT_A_STATUS"}
            ),
            VALIDATION,
        )

    def test_a_missing_status_is_rejected(self):
        self.assert_business_failure(
            self.change_status(self.order_mine.order_no, key="probe-missing", body={}),
            VALIDATION,
        )

    def test_an_over_long_remark_is_rejected(self):
        """`remark` ≤ 255（与它落地的 `OperationLog.reason` 列**同源**）⇒ 超长回 `1000`。"""
        self.assert_business_failure(
            self.change_status(
                self.order_mine.order_no,
                key="probe-long-remark",
                body={"status": "PENDING_REVIEW", "remark": "测" * 256},
            ),
            VALIDATION,
        )

    def test_a_non_object_body_does_not_500(self):
        """非对象请求体（JSON 数组 / 标量）⇒ `1000`，⛔ **不是 500**。"""
        for raw in ("[]", '"x"', "123"):
            with self.subTest(raw=raw):
                response = self.change_status(
                    self.order_mine.order_no, key=f"probe-body-{raw[0]}", raw=raw
                )
                self.assert_business_failure(response, VALIDATION)

    def test_a_missing_idempotency_key_is_1000(self):
        """⭐ 写端点**必带**幂等键（8–128；缺 / 非法 ⇒ `1000`）。"""
        for key in (None, "short", "k" * (MAX_KEY + 1)):
            with self.subTest(key=key):
                self.assert_business_failure(
                    self.change_status(self.order_mine.order_no, key=key), VALIDATION
                )
        # ⭐ 对照：长度落在区间里的键 ⇒ 成功（否则上面那三条对"什么都拒"的实现也绿）。
        self.assert_changed(
            self.change_status(self.order_mine.order_no, key="k" * MIN_KEY),
            self.order_mine.order_no,
            "PENDING_REVIEW",
        )


class ScopeTests(_StatusFixture):
    """⭐ spec §9-1 的 ③ 与 §5.3-2：改**别人的单** ⇒ `1002`，且**库里一个字段都没变**。

    ⚠️ 这是"改状态"这条路上**最贵**的一条判据：它一旦漏，外部系统能改**本租户任意一张单**
    的状态（含把别人的单改成「已退单」）。
    """

    def test_changing_someone_elses_order_is_1002_and_changes_nothing(self):
        order_before = self.order_snapshot(self.order_others.order_no)
        perf_before = self.perf_snapshot(self.order_others.order_no)
        payload = self.assert_business_failure(
            self.change_status(self.order_others.order_no, key="probe-others"),
            BUSINESS,
        )
        # ⭐ 逐字段全列快照（含 MAIN 绩效行）：⛔ 不是挑几个字段比一比。
        self.assertEqual(self.order_snapshot(self.order_others.order_no), order_before)
        self.assertEqual(self.perf_snapshot(self.order_others.order_no), perf_before)
        # ⭐ 与"不存在"**同一句文案**（spec §5.3-2 的"同形"）。
        missing = self.assert_business_failure(
            self.change_status("20261008000000005999", key="probe-missing-order"), BUSINESS
        )
        self.assertEqual(payload["msg"], missing["msg"])
        self.assert_no_logs(self.order_others.order_no)

    def test_changing_his_own_order_succeeds(self):
        """⭐ **对照**（缺它，"什么都拒"的实现也能让上面那条绿）。"""
        self.assert_changed(
            self.change_status(self.order_mine.order_no, key="probe-mine"),
            self.order_mine.order_no,
            "PENDING_REVIEW",
        )

    def test_an_order_in_his_game_scope_but_claimed_by_someone_else_is_1002(self):
        """⭐⭐ **「他是当前接手人」不是「游戏范围内」**（红队点名；本任务最重要的判据之一）。

        `order_others` **在**甲的游戏权限范围内（`game_a`，甲有权限）、且甲的令牌打它 ⇒
        只有"他是当前接手人"那份判据拦得住它。
        ⛔ 把 `order_scope` 换成 T5 那套 `scope_queryset` + `game_scope("game_id")`
        （游戏范围）⇒ 本用例**必红**（读数见 T6 报告）。
        """
        before = self.order_snapshot(self.order_others.order_no)
        self.assert_business_failure(
            self.change_status(self.order_others.order_no, key="probe-scope-trap"),
            BUSINESS,
        )
        self.assertEqual(self.order_snapshot(self.order_others.order_no), before)

    def test_the_actual_taker_can_change_that_order(self):
        """⭐ **第二个视角**：同一张单在**乙**（真的接手人）手里**改得动**。

        判别力：若那张单其实**没有 MAIN 绩效行**（夹具坏了），上面两条会以"与被测行为无关的
        原因"变绿；本用例把它排除掉。
        """
        self.assert_changed(
            self.change_status(
                self.order_others.order_no, key="probe-owner-b", token=self.token_b
            ),
            self.order_others.order_no,
            "PENDING_REVIEW",
        )

    def test_an_orphan_order_is_1002_not_500(self):
        """⭐ spec §9-14：**孤儿订单**（没有 MAIN 绩效行）⇒ `1002`，⛔ **不是 500**。"""
        orphan = self._order(
            self.game_a, "20261008000000000506", taker=None, with_main_performance=False
        )
        self.assert_business_failure(
            self.change_status(orphan.order_no, key="probe-orphan"), BUSINESS
        )
        self.assertEqual(Order.objects.get(order_no=orphan.order_no).status, OrderStatus.IN_PROGRESS)


class NodeGateTests(_StatusFixture):
    """⭐ spec §5.3-4：`nodes_of(taker)` 进状态机 —— **撤掉节点 ⇒ 改状态立刻不通**。

    判别力：若实现传的是常量节点集（`OPEN_API_FULL_NODES` 那种），撤掉节点**照样能改**
    ⇒ 本条红。⚠️ 判据同时要求"**没有半截写入**"（状态没变 + 没有留痕）。
    """

    def test_withdrawing_the_node_closes_the_endpoint(self):
        self.withdraw_change_status_node(self.taker_a)
        payload = self.assert_business_failure(
            self.change_status(self.order_mine.order_no, key="probe-no-node"), BUSINESS
        )
        # ⚠️ 领域层那句会点明是**权限**那一扇门（「（缺少所需权限节点）」）——
        #    本文件**不断言整句**（那是 `order_status` 的文案，不是本端点的契约），
        #    只钉"它被拒了、且库里没变"。
        self.assertTrue(payload["msg"], payload)
        self.assertEqual(
            Order.objects.get(order_no=self.order_mine.order_no).status, OrderStatus.IN_PROGRESS
        )
        self.assert_no_logs(self.order_mine.order_no)

    def test_the_control_group_with_the_node_it_works(self):
        """⭐ 对照：节点还在 ⇒ 同一条请求成功（否则上面那条对"永远拒绝"也绿）。"""
        self.assert_changed(
            self.change_status(self.order_mine.order_no, key="probe-has-node"),
            self.order_mine.order_no,
            "PENDING_REVIEW",
        )


class IdempotencyTests(_StatusFixture):
    """⭐ 幂等 × 改状态（spec §9-4 同款口径）。

    ⚠️ **断言打在解析后的 `code` / `msg` / `data` 上**（⛔ 不断 `response.content` 字节）：
    `response_snapshot` 是 JSONB、**不保键序**（`idempotency.py:362-365` 明文登记）。
    """

    def test_the_same_key_replays_the_same_envelope(self):
        key = "probe-idem-status-01"
        first = self.change_status(self.order_mine.order_no, key=key, body={"status": "PENDING_REVIEW"})
        second = self.change_status(self.order_mine.order_no, key=key, body={"status": "PENDING_REVIEW"})
        self.assertEqual(self.body(first)["code"], OK)
        self.assertEqual(
            {k: self.body(second)[k] for k in ("code", "msg", "data")},
            {k: self.body(first)[k] for k in ("code", "msg", "data")},
        )

    def test_the_replay_does_not_write_a_second_log_row(self):
        """⭐ 回放**不重新执行业务** ⇒ 留痕仍然**恰好一行**（判据打在库里的行数上）。"""
        key = "probe-idem-status-02"
        self.change_status(self.order_mine.order_no, key=key, body={"status": "PENDING_REVIEW"})
        self.change_status(self.order_mine.order_no, key=key, body={"status": "PENDING_REVIEW"})
        self.assertEqual(
            OperationLog.objects.filter(
                order_no=self.order_mine.order_no, log_type=TRANSITION_LOG_TYPE
            ).count(),
            1,
        )

    def test_a_new_key_against_the_same_target_state_is_1002(self):
        """⭐ 换键再改**同一个**目标态 ⇒ `1002`（状态机把它当**自环**拒掉）。

        口径写死（⛔ 不写成"两种都算过"）：本条钉的是**状态机**那一扇门
        （`order_status.self_transition_message`：已经「待验收」了、无需修改），
        与白名单无关（`PENDING_REVIEW` 在白名单里）。
        """
        self.change_status(self.order_mine.order_no, key="probe-self-1", body={"status": "PENDING_REVIEW"})
        self.assert_business_failure(
            self.change_status(
                self.order_mine.order_no, key="probe-self-2", body={"status": "PENDING_REVIEW"}
            ),
            BUSINESS,
        )

    def test_a_failure_does_not_burn_the_key(self):
        """⭐ 失败**不**把键永久占住：同一个键在一张**自己的**单上重试 ⇒ 成功。

        ⚠️ 这条证的是"`_run` 对业务码 ≠ 0 的默认处置 = 整笔回滚"（**没有任何东西**
        在失败时 `mark_side_effect`）——若哪天有人给本 handler 加了那句话，
        这条立刻红（同 T5 抢单那条反例的形状）。
        """
        key = "probe-retry-after-failure"
        self.assert_business_failure(
            self.change_status(self.order_others.order_no, key=key), BUSINESS
        )
        # 同一个键、换一张**自己的**单 —— ⚠️ 幂等台账的键是 (app, key)，与单号无关
        # ⇒ 上一次失败若把占位行提交了，这里会回 `1001`（正在处理中）而不是成功。
        self.assert_changed(
            self.change_status(self.order_mine.order_no, key=key),
            self.order_mine.order_no,
            "PENDING_REVIEW",
        )

    def test_the_idempotency_ledger_belongs_to_the_taker_side(self):
        """⭐ 台账落在**打手侧**那张表上（spec §4.1-1 的跨系统串台面）。"""
        self.change_status(self.order_mine.order_no, key="probe-ledger", body={"status": "PENDING_REVIEW"})
        rows = TakerApiIdempotency.objects.filter(app=self.app_a)
        self.assertEqual(rows.count(), 1, list(rows.values("key", "path")))
        self.assertEqual(rows.first().key, "probe-ledger")


class OperationLogTests(_StatusFixture):
    """⭐ 留痕：成功 ⇒ **恰好一行** `order.transition`；失败 ⇒ **0 行**（spec §6 硬规则 2）。

    ⚠️ 判据全部打在 `OperationLog` 的**数据库行**上（⛔ 不看日志输出：`E-0009` 实测
    `assertLogs` 会自己抬级别 ⇒ "生产一行都没写"的测试**全绿**）。
    ⚠️ 那一次 `record(...)` 不在本端点里，而在领域口
    （`apps/business/order_writes.py::transition_order`）⇒ 本类同时钉住
    "本端点**没有**补第二行"（行数**恰好 1**，⛔ 不是 `>= 1`）。
    """

    def test_a_successful_change_writes_exactly_one_row(self):
        self.change_status(self.order_mine.order_no, key="probe-log-1", body={"status": "PENDING_REVIEW"})
        rows = list(
            OperationLog.objects.filter(
                order_no=self.order_mine.order_no, log_type=TRANSITION_LOG_TYPE
            )
        )
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        # ⭐ 操作人 = 应用（这条路上没有"人"）＋ 机器行标记。
        self.assertIn(TAKER_ACTOR_PREFIX, row.actor)
        self.assertIn(self.app_a.name, row.actor)
        self.assertIsNone(row.actor_id)
        # ⭐ 前后值是**中文标签**（与全仓同源）。
        self.assertEqual(row.field_name, "status")
        self.assertEqual(row.from_value, "代练中")
        self.assertEqual(row.to_value, "待验收")

    def test_the_remark_lands_in_the_reason_column(self):
        """⭐ 入参 `remark` 落留痕的 `reason` 列（spec §5 端点表第 5 行给的入参）。"""
        remark = "测试备注：客户要求改期"
        self.change_status(
            self.order_mine.order_no,
            key="probe-log-remark",
            body={"status": "PENDING_REVIEW", "remark": remark},
        )
        row = OperationLog.objects.get(
            order_no=self.order_mine.order_no, log_type=TRANSITION_LOG_TYPE
        )
        self.assertEqual(row.reason, remark)

    def test_a_failed_change_writes_nothing(self):
        for key, body in (
            ("probe-log-f1", {"status": "COMPLETED"}),
            ("probe-log-f2", {"status": "已暂停"}),
        ):
            with self.subTest(body=body):
                self.change_status(self.order_mine.order_no, key=key, body=body)
        self.assert_no_logs(self.order_mine.order_no)

    def test_a_scope_failure_writes_nothing(self):
        self.change_status(self.order_others.order_no, key="probe-log-scope")
        self.assert_no_logs(self.order_others.order_no)

    def test_no_credential_value_leaks_into_the_log(self):
        """⭐ spec §9-11 的同款判据（凭据不进留痕）：`app_secret` / 令牌一个都不许出现。

        ⚠️ 判据扫的是**整张留痕表的所有文本列**（⛔ 不是只看 `reason`）：漏一列就等于漏一条
        泄漏面。⚠️ 用的是 `values()` 摊平（`E-0065`：豁免 / 拦截类判据要打在**全集**上）。
        """
        self.change_status(self.order_mine.order_no, key="probe-log-secret", body={"status": "PENDING_REVIEW"})
        token = self.token_a
        blob = json.dumps(
            [
                {
                    field.name: str(getattr(row, field.attname))
                    for field in OperationLog._meta.concrete_fields
                }
                for row in OperationLog.objects.all()
            ],
            ensure_ascii=False,
        )
        self.assertNotIn(self.app_a.app_secret, blob)
        self.assertNotIn(token, blob)


class AssemblyAndAuthTests(_StatusFixture):
    """⭐ 装配（spec §9-15）＋ 鉴权面（spec §9-2 的"三组不混"，在**真路由**上打）。"""

    def test_the_view_is_assembled_exactly(self):
        from apps.takerapi.api.order_status_views import TakerOrderStatusChangeView
        from apps.takerapi.authentication import TakerAppBearerAuthentication
        from apps.takerapi.throttling import TakerAppFallbackThrottle

        self.assertEqual(
            TakerOrderStatusChangeView.authentication_classes, [TakerAppBearerAuthentication]
        )
        self.assertEqual(TakerOrderStatusChangeView.permission_classes, [])
        self.assertEqual(
            TakerOrderStatusChangeView.throttle_classes, [TakerAppFallbackThrottle]
        )

    def test_no_credential_is_401(self):
        response = self.anonymous.post(
            STATUS_TEMPLATE.format(order_no=self.order_mine.order_no),
            data=json.dumps({"status": "PENDING_REVIEW"}),
            content_type="application/json",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_an_openapi_token_is_rejected(self):
        api_app = self.make_api_app()
        token = self.issue_token(api_app, path=OPENAPI_TOKEN_PATH)
        response = self.change_status(self.order_mine.order_no, key="probe-cross", token=token)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)
        # ⭐ 并且**库里没变**（认证失败不该有任何副作用）。
        self.assertEqual(
            Order.objects.get(order_no=self.order_mine.order_no).status, OrderStatus.IN_PROGRESS
        )

    def test_a_session_request_is_rejected(self):
        session = make_logged_in_client(
            domain=self.domain_name, username="status-session-user", games=None
        )
        response = session.post(
            STATUS_TEMPLATE.format(order_no=self.order_mine.order_no),
            data=json.dumps({"status": "PENDING_REVIEW"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_a_taker_token_is_rejected_by_the_openapi_route(self):
        response = self.anonymous.get(
            OPENAPI_ORDERS_PATH,
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])

    def test_the_control_group_a_valid_token_works(self):
        """⭐ 对照组（⛔ 缺了它，上面几条对"什么都拒"的实现也会绿）。"""
        self.assert_changed(
            self.change_status(self.order_mine.order_no, key="probe-auth-control"),
            self.order_mine.order_no,
            "PENDING_REVIEW",
        )


class MalformedPathTests(_StatusFixture):
    """㊙ **`test_no_route_500` 的已知盲区**：带路径转换器的路由不在那条腿的覆盖内。"""

    def test_a_nul_in_the_order_no_is_404_not_500(self):
        response = self.anonymous.post(
            "/api/taker/v1/orders/2026%00 1/status/",
            data=json.dumps({"status": "PENDING_REVIEW"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.token_a}",
            HTTP_IDEMPOTENCY_KEY="probe-nul-path",
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, NOT_FOUND, response.content[:200])

    def test_a_long_order_no_is_1002_not_500(self):
        """⭐ **天文数字**那一格：120 字符的单号 ⇒ `1002`（⛔ 不是 500、也⛔ 不是 404）。

        ⚠️ 理由同查单那条：`order_no` 列宽 20 而路径段没有长度上限 ⇒ 它会真的进
        `filter(order_no=…)`（PostgreSQL 对 `varchar` 比较**不报** `value too long`），
        答复是"查不到"那一档。
        ⚠️ 为什么是 **120** 而⛔ 不是 300：本端点是**写**端点、套着幂等入口，而那个入口有
        一条**整条路径**的列宽闸（台账的 `path` 列 = 191；本端点前缀
        `/api/taker/v1/orders/` + `/status/` 已占 29 个字符）⇒ 300 字符的单号
        **到不了**业务判据（下面那条单列钉住这个更靠前的闸）。
        两条合起来才是"这类请求不 500"的完整判据（`AGENTS.md` §5-E：判据是**这类请求**
        还会不会 500，不是"这个字段"）。
        """
        self.assert_business_failure(
            self.change_status("9" * 120, key="probe-long-order-no"), BUSINESS
        )

    def test_an_over_long_path_is_1000_not_500(self):
        """⭐ 更靠前的那道闸：路径超过幂等台账的列宽 ⇒ `1000` + 一句点名上限的话（⛔ 不是 500）。"""
        response = self.change_status("9" * 300, key="probe-overlong-path")
        payload = self.assert_business_failure(response, VALIDATION)
        self.assertIn("191", payload["msg"], payload)

    def test_the_path_requires_a_trailing_slash(self):
        response = self.change_status(
            self.order_mine.order_no,
            key="probe-no-slash",
            path=f"/api/taker/v1/orders/{self.order_mine.order_no}/status",
        )
        self.assertNotEqual(response.status_code, 200)
