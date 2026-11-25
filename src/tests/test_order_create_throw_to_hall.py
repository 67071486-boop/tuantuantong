"""录单即抛内部抢单大厅 —— 后端判据（spec §3.1 / §4.1）。

spec = `docs/specs/2026-10-08-hall-throw-and-autorefresh-design.md`（业务方裁定）。

本文件钉的八件事（用例名与判据逐条照 spec §4.1 的表）：

1. 一次 `POST /orders/`（带 `throw_to_hall=true` + 发单价）⇒ **建单与抛单两件事都成**，
   并且这张单**从大厅读口查得到**（判据打在终态上，⛔ 不看「脚本没报错」）；
2. 发单人 / 发单价 / 审计**恰好两行**（`order.create` + `order.throw_to_hall`）；
3. `throw_to_hall` 与 `assignee` **互斥** ⇒ 业务码 `1000` + 定稿文案，且**一张单都不建**；
4. 发单价**缺省 / `'0'` / `'0.00'` / `''`** 四档一律 `1000`、订单数不变
   —— 判据是**解析后的数值**，⛔ 不是真值（E-0021 族：`'0.00'` 是真值、却是非法价）；
5. **只持 `order.add_edit`** 的客服可以正常建单，直接提交内部抢单整次拒绝；
6. 同一账号**走不了**另外**三条**老路（`/transition/`、`/throw-to-hall/`、
   `/batch/transition/` 都 403、状态不变）—— 这是**反向承重判据**：边表放宽**没有**变成提权；
   ⭐ `/batch/transition/` 是**修复轮补的**（红队指出：枚举论证里那些「能到 `WAITING_GRAB`
   的写入口」必须**每条**都有反向判据，而它此前一条都没有）；
7. 抛单那一步失败 ⇒ 业务码 **`1023`** + 已建单号 + 原因，而**订单还在**（建单事务已提交）；
8. 同一条失败路径 ⇒ **没有** `member.launch` 台账行（对称于 E-0063）；
   ⛔ **2026-10-08 改准**：原文这里还钉 `hall.launch`（租户级鸡蛋铺线 `queue_launch` 写的）。
   那条线整条拆除 ⇒ 该 kind 的**生产者不存在**，那一半退场（⛔ 不是放宽：留下的那一半
   仍然**逐 kind**判，不写成「随便哪个 kind 都没有」）；
9. ⭐枚举论证里那句「`PATCH` 的 `status` 是只读的」有自己的判据：只持
   `order.add_edit` 的账号 PATCH 送 `status` ⇒ 状态不变，**同一次 PATCH 里的 `remark` 照常落库**
   （阳性对照）；
10. ⭐抛单之后那些钩子抛**未预期**异常 ⇒ **HTTP 500**（⛔ 不是 `1023`），
    而订单仍在、留在 `UNASSIGNED` —— 本用例钉的是**如实行为**，用户侧后果见它的 docstring。

三条纪律（本文件刻意都做到）：
- **夹具一律合成值**（E-0077：计划 / spec 里的示例值会被原样抄进 `src/**`）；
- **指认用单号，不用主键**（两个 schema 的主键序列各自从 1 开始 ⇒ 拿主键指认会恒真/恒假）；
- **恒真的判据不算判据**（E-0065 同族）：第 8 条先跑一次**成功**抛单、证明这套夹具**真的会**
  写那条 `member.launch` 台账，再去断言失败路径零行 —— 否则「零行」在没绑定 / 没链路的租户里
  **本来**就成立。
"""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import jidanpu_member, order_status
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
)
from apps.business.order_status import IllegalTransition
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

CREATE_URL = "/api/business/orders/"
HALL_URL = "/api/business/hall/"

CREATE_NODE = "order.add_edit"
HALL_PUBLISH_NODE = "order.send_to_grab_hall"
#: 读大厅自己的门（`ORDER_PERMISSIONS["hall"]`）。它**不是**抛单的门，只用来读结果。
HALL_READ_NODE = "grab_hall.take_order"

CS_GROUPS = ("客服",)


def _make_game(name="测试游戏甲"):
    return Game.objects.create(name=name)


class CreateWithThrowTests(TenantTestCase):
    """① ② ⑤：一次请求做完两件事；发单人 / 价 / 审计；最小权限账号也能用。"""

    def setUp(self):
        # ⚠️ 游戏必须在**建账号之前**建：`make_logged_in_client` 不传 `games` 时
        #    给的是「当前租户的全部游戏」（D-0136）⇒ 顺序反了会拿到零游戏、写侧被拒。
        self.game = _make_game()
        self.recorder = make_logged_in_client(
            username="测试客服甲",
            groups=CS_GROUPS,
            nodes=(CREATE_NODE, HALL_READ_NODE, HALL_PUBLISH_NODE),
        )
        self.recorder_user = User.objects.get(username="测试客服甲")

    def _post(self, client=None, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "测试项目文本甲",
            "hall_price": "88.50",
            "throw_to_hall": True,
        }
        payload.update(overrides)
        return (client or self.recorder).post(
            CREATE_URL, payload, content_type="application/json"
        )

    def test_create_with_throw_creates_and_throws_in_one_request(self):
        """① 一次请求两件事都成；**再用大厅读口查得到这张单**（判据在终态上）。"""
        response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 0, body)
        order_no = body["data"]["order_no"]

        # 终态一：库里这张单就是「待抢单」。
        order = Order.objects.get(order_no=order_no)
        self.assertEqual(order.status, OrderStatus.WAITING_GRAB)

        # 终态二：**大厅读口**（调用方真正能看到的那个载荷）里查得到它。
        # ⚠️ 指认用**单号**，⛔ 不用主键。
        hall = self.recorder.get(HALL_URL)
        self.assertEqual(hall.status_code, 200)
        hall_body = hall.json()
        self.assertEqual(hall_body["code"], 0, hall_body)
        self.assertIn(order_no, [row["order_no"] for row in hall_body["data"]["items"]])

    def test_hall_publisher_price_and_two_audit_rows(self):
        """② 发单人 = 录单人；价 = 请求里那个数；审计**恰好两行**（且顺序 = 建单在前）。"""
        body = self._post().json()
        self.assertEqual(body["code"], 0, body)
        order_no = body["data"]["order_no"]

        order = Order.objects.get(order_no=order_no)
        self.assertEqual(order.hall_publisher_id, self.recorder_user.pk)
        self.assertEqual(order.hall_price, Decimal("88.50"))

        rows = list(
            OperationLog.objects.filter(order_no=order_no).order_by("id")
        )
        self.assertEqual([row.action for row in rows], ["order.create", "order.throw_to_hall"])
        throw_row = rows[1]
        # `to_value` 是文本列 ⇒ 与「价」的字符串形式逐字比（⛔ 不拿 Decimal 比文本）
        self.assertEqual(throw_row.to_value, "88.50")
        # 留痕的操作人取**认证态**（R20）：不是请求体里可伪造的 `actor`
        self.assertEqual(throw_row.actor_id, self.recorder_user.pk)
        self.assertEqual(throw_row.actor, "测试客服甲")

    def test_create_only_can_record_but_cannot_publish_to_the_hall(self):
        strict = make_logged_in_client(
            username="测试客服乙", groups=("仅录单",), nodes=(CREATE_NODE,)
        )
        before = Order.objects.count()

        denied = self._post(client=strict)
        self.assertEqual(denied.status_code, 403, denied.content)
        self.assertEqual(Order.objects.count(), before)

        response = self._post(client=strict, throw_to_hall=False)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(order.hall_publisher_id)


class ThrowPayloadGateTests(TenantTestCase):
    """③ ④：两道闸都在**建单之前**，一行都不许写。"""

    def setUp(self):
        self.game = _make_game()
        self.recorder = make_logged_in_client(
            username="测试客服甲", groups=CS_GROUPS, nodes=(CREATE_NODE, HALL_READ_NODE, HALL_PUBLISH_NODE)
        )
        make_logged_in_client(username="测试打手甲", groups=("代练员",), nodes=("order.list",))
        self.taker_user = User.objects.get(username="测试打手甲")

    def _post(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "测试项目文本甲",
            "hall_price": "88.50",
            "throw_to_hall": True,
        }
        payload.update(overrides)
        return self.recorder.post(CREATE_URL, payload, content_type="application/json")

    def test_assignee_and_throw_are_mutually_exclusive(self):
        """③ 两个键同时给 ⇒ `1000` + 定稿文案，且**一张单都不建**。"""
        response = self._post(assignee=self.taker_user.pk)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], "内部抢单与指派接手人只能二选一")
        self.assertEqual(Order.objects.count(), 0)

    def test_zero_or_missing_price_is_rejected_before_creating(self):
        """④ 发单价四档（缺省 / `'0'` / `'0.00'` / `''`）一律 `1000`、订单数不变。

        ⚠️ **判据是解析后的数值**：`'0'` 与 `'0.00'` 都是**真值**、却都是非法价
        （E-0021 族）⇒ 写成真值判断会让这两档静默通过。空串那一档由 DRF 的字段
        校验先拒（同样 `1000`、同样一行不写）。
        """
        cases = {
            "缺省": {},
            "'0'": {"hall_price": "0"},
            "'0.00'": {"hall_price": "0.00"},
            "''": {"hall_price": ""},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                overrides = dict(overrides)
                if not overrides:
                    # 「缺省」= 载荷里根本没有这个键（⛔ 不是传 `None`：那会让本档变成另一种载荷）
                    body = self.recorder.post(
                        CREATE_URL,
                        {
                            "game": self.game.id,
                            "project_text": "测试项目文本甲",
                            "throw_to_hall": True,
                        },
                        content_type="application/json",
                    ).json()
                else:
                    body = self._post(**overrides).json()
                self.assertEqual(body["code"], 1000, (label, body))
                self.assertEqual(Order.objects.count(), 0, (label, body))


class AddEditOnlyCannotReachTheHallElsewhereTests(TenantTestCase):
    """⑥ ⑨ 反向承重判据：边表放宽**没有**提权 —— 同一账号走另外三条老路都必须被拒，
    而 `PATCH` 那条「除新建单外无路可走」的论证也有自己的判据。"""

    def setUp(self):
        self.game = _make_game()
        self.strict = make_logged_in_client(
            username="测试客服丙", groups=CS_GROUPS, nodes=(CREATE_NODE,)
        )

    def test_add_edit_alone_cannot_reach_waiting_grab_elsewhere(self):
        created = self.strict.post(
            CREATE_URL,
            {"game": self.game.id, "project_text": "测试项目文本甲"},
            content_type="application/json",
        ).json()
        self.assertEqual(created["code"], 0, created)
        order_no = created["data"]["order_no"]

        for label, url, payload in (
            (
                "/transition/",
                f"/api/business/orders/{order_no}/transition/",
                {"target": OrderStatus.WAITING_GRAB},
            ),
            ("/throw-to-hall/", f"/api/business/orders/{order_no}/throw-to-hall/", {}),
            # ⭐ **第三条老路（承重）**：枚举论证里「能到 `WAITING_GRAB` 的写入口」
            # 共四条，每一条都要有反向判据——`/batch/transition/` 此前**一条都没有**。
            # 它的类级门是 `ORDER_PERMISSIONS["batch_transition"]` = `order.change_status`
            # ⇒ 只持 `order.add_edit` 的账号同样拿不到（实测：传输层 403、业务码也是 403）。
            # ⚠️ `dry_run` **必须显式给 `False`**：缺了它这一发会先被序列化器判成 1000
            # （**一行不写**）⇒ 万一闸门被拆掉，「状态没变」也照样成立，判据会变成没判别力的
            # （E-0065 同族：恒真的判据不算判据）。
            (
                "/batch/transition/",
                "/api/business/orders/batch/transition/",
                {
                    "order_nos": [order_no],
                    "target": OrderStatus.WAITING_GRAB,
                    "dry_run": False,
                },
            ),
        ):
            with self.subTest(端点=label):
                response = self.strict.post(
                    url, payload, content_type="application/json"
                )
                # 类级节点门没过 ⇒ **传输层** 403（不是 200 + 业务码）
                self.assertEqual(response.status_code, 403, response.content)

        # 状态**没变**：三条老路都没把这张单推进大厅
        self.assertEqual(
            Order.objects.get(order_no=order_no).status, OrderStatus.UNASSIGNED
        )

    def test_patch_cannot_write_status(self):
        """⑨ 枚举论证里「`PATCH` 的 `status` 是**只读**的」这句话的判据（承重）。

        为什么需要它：那句「单持 `order.add_edit` 的人除新建单外**无路可走**」今天只剩
        `OrderUpdateSerializer.Meta.read_only_fields` 里那一行字面量守着 —— 把 `"status"`
        从那个列表里删掉，**没有一条用例会红**，而后果是提权（E-0028「角色收紧 ≠ 路径排他」同族）。

        ⚠️ **阳性对照**：同一次 `PATCH` 里的 `remark` **必须真的落库** —— 它证明这一次 PATCH
        真的执行过（而不是整包被拒 / 写路径被整体堵死），否则「状态没变」可以在写口全坏时
        照样成立。同一形状的先例见 `tests/test_order_column_fields.py` 的
        `test_patch_cannot_forge_the_completion_time_or_the_publisher`。
        """
        created = self.strict.post(
            CREATE_URL,
            {"game": self.game.id, "project_text": "测试项目文本甲"},
            content_type="application/json",
        ).json()
        self.assertEqual(created["code"], 0, created)
        order_no = created["data"]["order_no"]

        response = self.strict.patch(
            f"/api/business/orders/{order_no}/",
            {"status": OrderStatus.WAITING_GRAB, "remark": "测试备注甲"},
            content_type="application/json",
        )
        body = response.json()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(body["code"], 0, body)

        # 判据打在**数据库行**上（⛔ 不看响应体自报的 `status`）
        row = Order.objects.get(order_no=order_no)
        self.assertEqual(row.status, OrderStatus.UNASSIGNED)
        # 阳性对照：`remark` 是**同一次 PATCH** 里的普通可写字段 ⇒ 它落库了才说明
        # 这次 PATCH 真的执行过、且非只读字段照常写入。
        self.assertEqual(row.remark, "测试备注甲")


class ThrowStepFailureTests(TenantTestCase):
    """⑦ ⑧：抛单那一步失败 ⇒ 订单不回滚；且**没有**那一条台账行。

    ⚠️ 夹具里**必须有**一份启用中的下家（打手个人）绑定：否则 `queue_hall_launch`
    本来就**一行都不写**（未绑定 / 没人认领），第 8 条的「零行」会变成**恒真**的判据
    （E-0065 同族）。第 8 条里先用一次**成功**的抛单当控制，证明这套夹具真的会写那一行。

    ⛔ **2026-10-08 改准**：夹具里原还造一份**租户级** `JidanpuBinding`（为了让
    `jidanpu_hall.queue_launch` 写 `hall.launch`）。那条线整条拆除 ⇒ 这份夹具**不再需要**
    它（留着一个对已删代码无影响的绑定只会让下一个人以为它承重），同批删除。
    """

    def setUp(self):
        self.game = _make_game()
        self.recorder = make_logged_in_client(
            username="测试客服甲", groups=CS_GROUPS, nodes=(CREATE_NODE, HALL_READ_NODE, HALL_PUBLISH_NODE)
        )
        # 下游（打手个人）绑定：他游戏范围含这张单的游戏 + 单在池内且无人接
        # ⇒ `owners_of` 认领它 ⇒ `queue_hall_launch` 会写 `member.launch`。
        self.owner = make_logged_in_client(
            username="测试打手甲", groups=("代练员",), nodes=("order.list",)
        )
        JidanpuMemberBinding.objects.create(
            user=User.objects.get(username="测试打手甲"),
            jidanpu_base_url="https://jidanpu-downstream.test.invalid",
            account="测试下家账号甲",
            password="测试口令乙",
            enabled=True,
        )

    def _post(self):
        return self.recorder.post(
            CREATE_URL,
            {
                "game": self.game.id,
                "project_text": "测试项目文本甲",
                "hall_price": "88.50",
                "throw_to_hall": True,
            },
            content_type="application/json",
        )

    def _failing_throw(self):
        """把抛单那一步按**被查找的模块**打桩。

        桩只让 `apply_transition` 抛 `IllegalTransition`（可预期的域层拒绝）——
        这正是 `1023` 要接的那一档。

        录单动作在 `apps.business.order_intake`，桩必须打到那里的 `apply_transition`。
        打在 `order_views` 上是死桩：本用例会看到 `code=0` 当场红，而不是静默通过。
        """
        return mock.patch(
            "apps.business.order_intake.apply_transition",
            side_effect=IllegalTransition("测试：这张单现在不能进大厅"),
        )

    def test_throw_failure_keeps_the_order(self):
        """⑦ 业务码 `1023`（⛔ 不是 500、也不是笼统的 `1002`）+ 单号 + 原因；订单**还在**。"""
        with self._failing_throw():
            response = self._post()
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 1023, body)
        self.assertEqual(set(body["data"]), {"order_no", "reason"})
        self.assertTrue(body["data"]["order_no"])
        self.assertTrue(body["data"]["reason"])
        # msg 里带单号与原因（页面「原样显示后端 msg」那条路要能给出中间态）
        self.assertIn(body["data"]["order_no"], body["msg"])
        self.assertIn(body["data"]["reason"], body["msg"])

        # ⛔ 不回滚：单在库里、状态仍是未分配、价已经写上（与 1022 同一形状）
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.hall_price, Decimal("88.50"))
        # 建单那条留痕也在（订单是**完整**地落下来的，不是半截）
        self.assertTrue(
            OperationLog.objects.filter(
                order_no=order.order_no, action="order.create"
            ).exists()
        )
        # 抛单那条留痕**不该**有（动作没做成 ⇒ 不留成功日志）
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=order.order_no, action="order.throw_to_hall"
            ).exists()
        )

    def test_no_launch_event_when_throw_fails(self):
        """⑧ 失败路径 ⇒ **没有** `member.launch` 台账行。

        控制先跑：证明这套夹具在**成功**抛单时**真的会**写那一行 —— 否则本用例的
        「零行」是没判别力的（E-0065 同族：恒真的判据要能被打红才算判据）。

        ⛔ **2026-10-08 改准（原名 `…_no_hall_launch_event_…`）**：原判据同时钉
        `hall.launch` 与 `member.launch` **两种**台账；`hall.launch` 由租户级鸡蛋铺线
        （`jidanpu_hall.queue_launch`）产生，它已随那条线整条拆除 ⇒ 那一半退场。
        ⚠️ **留下的判据强度没降**：控制哨兵与「零行」断言都**照旧各自只对
        `member.launch` 一条 kind** 判（⛔ 没有放宽成「随便哪个 kind 都没有」）。
        """
        ok = self._post().json()
        self.assertEqual(ok["code"], 0, ok)
        ok_no = ok["data"]["order_no"]
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind=jidanpu_member.KIND_LAUNCH, target_ref=ok_no
            ).exists(),
            "控制失败：这套夹具在成功抛单时**应当**写一条 member.launch 台账行",
        )

        with self._failing_throw():
            body = self._post().json()
        self.assertEqual(body["code"], 1023, body)
        failed_no = body["data"]["order_no"]
        self.assertNotEqual(failed_no, ok_no)
        with self.subTest(台账=jidanpu_member.KIND_LAUNCH):
            self.assertFalse(
                JidanpuSyncEvent.objects.filter(
                    kind=jidanpu_member.KIND_LAUNCH, target_ref=failed_no
                ).exists()
            )


class UnexpectedHookFailureTests(TenantTestCase):
    """⑩（修复轮补）抛单之后那些钩子炸掉会怎样 —— 本类钉的是**如实行为**，⛔ 不是理想行为。"""

    def setUp(self):
        self.game = _make_game()
        self.recorder = make_logged_in_client(
            username="测试客服甲", groups=CS_GROUPS, nodes=(CREATE_NODE, HALL_READ_NODE, HALL_PUBLISH_NODE)
        )

    def _post(self):
        return self.recorder.post(
            CREATE_URL,
            {
                "game": self.game.id,
                "project_text": "测试项目文本甲",
                "hall_price": "88.50",
                "throw_to_hall": True,
            },
            content_type="application/json",
        )

    @staticmethod
    def _boom(order, from_status):
        """注入用的桩：回池钩子的替身，一被调用就抛**未预期**异常。"""
        raise RuntimeError("测试注入：回池钩子抛了未预期异常")

    def test_unexpected_hook_failure_is_a_500_and_keeps_the_order(self):
        """⑩ 钉**如实行为**：抛单之后那些钩子抛未预期异常时，今天会怎样。

        ## 钉的是什么（读码 + 实测，⛔ 不是「应该怎样」）

        `apply_transition` 在**状态已经写进库之后**才跑 `_run_target_hooks` 与尾部副作用
        （回池钩子 / `queue_status_out` / `upstream` 回调 / peer 通知 / `PeerOrderLink.update`）。
        本路径只 `except IllegalTransition`（可预期的域层拒绝 ⇒ 业务码 `1023`）
        ⇒ 钩子里冒出的**未预期**异常一路冒到顶：响应是 **HTTP 500**（⛔ 不是 `1023`、也不是任何业务码），
        第二个事务整体回滚 ⇒ **订单本身不丢**（留在 `UNASSIGNED`，建单那一笔的 `hall_price` 已落库）。
        E-0010 的口径正是如此：未预期错误必须响亮地 500，不许伪装成业务结果。

        ## 用户侧后果（本线的**已知残差**）

        前端把 500 归到「**结果未知**」那一档（它拿不到业务码与单号）；用户确认后**可能再建一张单**
        ⇒ 同一笔业务出现两张单。⚠️ **本用例不修它、只钉住它别悄悄变**：这条残差由总指挥
        登记在 spec / 决策记录里；要改成「中间态提示」得先动 `apply_transition` 的异常契约
        （那是本模块自己的设计，⛔ 不在本用例的范围里）。

        ## 判别力（E-0029：桩必须打在**被查找处**）

        先跑一次**不接桩**的对照（成功路径 ⇒ 业务码 0 + 状态 `WAITING_GRAB`），
        证明「接了桩才 500」。
        ⚠️ **桩打在哪**：钩子不是按模块属性现查的 —— `register_target_hook` 在注册时就把**函数对象**
        本身存进了 `order_status._TARGET_HOOKS`（`order_status.py` 里 `_run_target_hooks` 从这张表取）
        ⇒ 打在 `apps.business.performance._on_pool_entry` 这个名字上**不会生效**（表里那份引用不跟着变）。
        唯一被查找处是那张注册表，所以桩打在那里。
        ⚠️ 本路径（`apply_transition` → `WAITING_GRAB`）实测**只有**那一个钩子
        （`performance._on_pool_entry`，本文件跑测时打印 `_TARGET_HOOKS` 复核过）。
        """
        # ── 对照：不接桩 ⇒ 成功路径（业务码 0 + 状态 WAITING_GRAB）────────────────
        ok = self._post().json()
        self.assertEqual(ok["code"], 0, ok)
        ok_no = ok["data"]["order_no"]
        self.assertEqual(
            Order.objects.get(order_no=ok_no).status, OrderStatus.WAITING_GRAB
        )

        # ── 接桩：把回池钩子换成会炸的桩（打在**唯一被查找处** = 注册表）─────────
        # `raise_request_exception = False` ⇒ Django 测试客户端把未处理异常**当作 500 响应**
        # 交回来（默认它会直接把它抛进用例里，那样就看不到状态码了）。
        self.recorder.raise_request_exception = False
        with mock.patch.dict(
            order_status._TARGET_HOOKS,
            {str(OrderStatus.WAITING_GRAB): [self._boom]},
        ):
            response = self._post()

        # 判据一：**HTTP 状态码** = 500（⛔ 不是业务码、⛔ 不是 1023）
        self.assertEqual(response.status_code, 500, response.content)

        # 判据二：订单**还在**（第二个事务回滚带不走它），且留在未分配。
        # ⚠️ 用**单号**指认：失败那一发拿不到单号（500 响应里没有信封）⇒ 用「除了对照那张单
        #    之外恰好还有一张」指认它，并复核它**没有**被推进大厅。
        others = Order.objects.exclude(order_no=ok_no)
        self.assertEqual(others.count(), 1, list(others.values_list("order_no", "status")))
        failed = others.get()
        self.assertEqual(failed.status, OrderStatus.UNASSIGNED)
        # 建单那一笔是**完整**落下来的：价已写（与 1023 那条路同一形状）
        self.assertEqual(failed.hall_price, Decimal("88.50"))


class ThrowFailureToastShowsDisplayNoTests(ThrowStepFailureTests):
    """⭐ D-0301（2026-10-24）：`1023` 的**人读 msg** 用展示单号（来源号非空取它），
    `data.order_no` 是寻址值、照旧内置号。"""

    SRC = "SRC-TEST-TOAST"

    def _post_with_source(self):
        return self.recorder.post(
            CREATE_URL,
            {
                "game": self.game.id,
                "project_text": "测试项目文本甲",
                "hall_price": "88.50",
                "throw_to_hall": True,
                "source_order_no": self.SRC,
            },
            content_type="application/json",
        )

    def test_msg_shows_the_source_and_data_keeps_the_system_no(self):
        with self._failing_throw():
            response = self._post_with_source()
        body = response.json()
        self.assertEqual(body["code"], 1023, body)
        # data 是寻址值：20 位内置号（⛔ 不被换成来源号）。
        self.assertRegex(body["data"]["order_no"], r"^\d{20}$")
        # msg 是人读面：逐字带来源号、⛔ 不再出现内置号。
        self.assertIn(self.SRC, body["msg"])
        self.assertNotIn(body["data"]["order_no"], body["msg"])
        # 订单照旧在（1023 的语义不变）。
        self.assertTrue(
            Order.objects.filter(order_no=body["data"]["order_no"]).exists()
        )
