"""B1-3：`/submit` 提交完单（spec §5 / §6.2；P4 的 B3）。

规则（逐条有测试）：
- **代练员**的动作；且必须是**这张单的接单人**（否则代练员 A 能替 B 交单）；
- 来源态**只接受 `IN_PROGRESS`**（⚠️ 不能只靠状态机：`ISSUE`/`PAUSED → PENDING_REVIEW` 是**客服**的边，
  不显式判来源态的话客服可以拿这个端点交单——与 `/withdraw`、`/assign` 的来源态闸门同形）；
- ⭐ **R2（编排方裁定）**：`/submit/` **不写任何金额**——既不写 `bonus_amount`，
  **也不写 `temp_amount`**（它只做：订单 → `PENDING_REVIEW` + `review_status = 待审核` + 既有留痕）。
  理由：「钱只在 `COMPLETED` 生成」（D3），「交单即生效、预计跟随生效」（D-0049 方案 A）的前提**已废**。
  代价如实登记：`temp_amount`（= 扣点基数）停在认领/指派时刻的预计值，中间改提成率**不再**刷新它。
- ⚠️ 因此**交单不再算提成、也不再因提成不可算而被拒**：金额那三条「预期失败」分支随
  `commission_amount(...)` 的删除一并消失（spec §4.2.1 明令删掉那句计算，⛔ 不许留「算完就丢」的死代码）。

⚠️ 金额断言一律钉**字面值**（`Decimal("0.00")` / `Decimal("88.00")`），**不得**引用被测模块导出的常量
或再算一遍——自指断言在常量被改坏时不红（E-0024 就是这条教训）。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client
from django_tenants.test.cases import TenantTestCase
from unittest import mock

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.claim import claim_order
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    ReviewStatus,
)
from apps.core.error_codes import ErrorCode

# RBAC 的 Role 必须改名导入。同名的状态机 Role 若被后一次 import 盖掉，
# Role.TAKER 会 AttributeError，而两个文件各自看都是绿的。
from apps.rbac.models import Role as RbacRole
from apps.rbac.seed import seed_rbac
from tests.p5_auth_helpers import grant_games, grant_nodes, make_logged_in_client

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
SUBMIT_ORDER_NODES = (
    "order.submit_work",   # `/submit/`
    "order.view_detail",
)

#: DRF 对**没有自定义 `message` 的权限类**给的默认 403 文案（本项目 zh-hans 下的实测值；
#: 同 `test_rbac_endpoint_enforcement` 里的同名常量）。用来把「节点门拒绝」与
#: 「CSRF 失败」等其它 403 来源区分开。⚠️ 期望值写字面量（E-0024）。
NODE_DENIED_MSG = "您没有执行该操作的权限"

PERCENT = CommissionMode.ORDER_PERCENT


class SubmitApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game, order_no="20260920000000000001",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"),
        )
        # ⚠️ 接单人**必须**用真实登录夹具建（`make_logged_in_client` 会当场建号+登录，
        #    它不接受「用户名已存在」）——P2 的 `force_login` 在本项目不可用（会话里没有
        #    `identity_plane`，每个请求都会 401，见 `tests/p5_auth_helpers.py`）。
        self.client = make_logged_in_client(
            username="taker1", groups=("代练员",), nodes=SUBMIT_ORDER_NODES,
            # ⚠️ 显式 opt-out（默认值翻面）：本类下面自己建档案（带提成率）
            # ⇒ 夹具这一份必须**不建**（`games=None`），否则撞唯一约束；游戏由下面的
            # `grant_games(self.taker, self.game)` 逐款补。
            games=None,
        )
        self.taker = User.objects.get(username="taker1")
        StaffProfile.objects.create(
            user=self.taker, commission_mode=PERCENT, commission_value=Decimal("20.00")
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。接单人 taker1 既是
        # 本文件每条用例的**发请求方**，也要经 `get_object()`（被 `game_scope` 收窄的
        # queryset）读到 `self.game` 名下这张单 ⇒ 缺游戏权限会拿 `1002`。
        # 该用例的期望值一个字没改。
        grant_games(self.taker, self.game)
        self.perf = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.taker,
            temp_amount=Decimal("88.00"),
        )
        self.url = f"/api/business/orders/{self.order.order_no}/submit/"

    def _login(self, *groups, username, nodes=SUBMIT_ORDER_NODES):
        """换一个账号登录（每个 username 只能用一次：夹具会建号）。

        ⚠️ ③线 Task 2：`/submit/` 要 `order.submit_work` 节点（提交完单；对标 105 已改造，
        见 spec §3.6）。2026-09-23 单轴化后**节点是唯一判据**，所以拒绝侧要显式少给那个节点
        （`nodes=` 可覆盖），而不是靠职位名。
        ⛔ `main` 那一侧同段还写「那条用例判的是 `roles_of` 的 **TAKER** 闸门」——
        `roles_of` 已被本线删除，那句随单轴化作废，不取。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, games=None, nodes=nodes
        )
        return User.objects.get(username=username)

    def _submit(self, payload=None):
        return self.client.post(self.url, payload or {}, content_type="application/json")

    # ── 正常路径 ────────────────────────────────────────────────
    def test_taker_submits_and_no_amount_is_written(self):
        """⭐ **R2**：交单后 `bonus_amount == Decimal("0.00")`，且 `temp_amount` **一个字不改**。

        `88.00` 是 `setUp` 里那条 MAIN 行的 `temp_amount`（认领/指派时刻的预计值）；
        旧口径下交单会按 20% 把它刷成 `17.60`（D-0049 方案 A），R2 之后**不再刷新**。
        """
        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        body = self._submit().json()
        self.assertEqual(body["code"], 0)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        self.assertIsInstance(self.perf.bonus_amount, Decimal)
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)

    def test_a_taker_without_a_profile_can_still_submit_and_no_amount_is_written(self):
        """没有提成档案 ⇒ 交单**照常成功**，因为 R2 之后交单**不算提成、不写金额**。

        ⚠️ 旧口径下本用例断「回落全额 ⇒ `bonus_amount == 88.00`」（提成读取层无档案回落 100%）；
        R2 把 `/submit/` 的提成计算整句删掉之后，那一档在**这条路径上**已不存在
        ⇒ 期望值改准为「不写任何金额」（判据没有放宽：`bonus` 与 `temp` 两个字面值都钉住）。

        ⚠️ **按游戏权限收窄）：本用例的登录账号必须同时持「系统管理员」职位**
        ——这是**让这个请求本身能发出去**的夹具补齐，不是放宽判据。

        为什么不能靠 `grant_games` 补：`grant_games` 内部会 `get_or_create(StaffProfile)`——
        补了游戏就等于补了档案，而本用例刻意要的是**没有 `StaffProfile`** 的那种账号形状。
        新契约下「没有档案的**非管理员**」读写订单一律拿 `1002` / `1000`
        （`tests/test_scope_orders.py::test_a_non_admin_without_a_profile_is_still_rejected_on_write`
        是这条的反面钉子）⇒ **没有档案却还能发这个请求**的账号形状，在本仓只有一种：
        `apps/platform/provisioning.py::seed_tenant` 供给出来的租户管理员
        （它**按构造没有 `StaffProfile`**，靠 `is_tenant_admin` 在 `scope_queryset` 里短路）。
        所以这里给它挂上那个职位——**没有给它建档案**，这个前提仍然成立。
        """
        fresh = self._login("代练员", "系统管理员", username="taker_no_profile")
        # 构造物：挂的是**职位**，不是档案——本用例的前提（没有档案）确实成立。
        # 没有这一条的话，「补了游戏权限」的改法会让本用例在**另一条分支**上碰巧变绿。
        self.assertFalse(StaffProfile.objects.filter(user=fresh).exists())
        self.perf.user = fresh
        self.perf.save(update_fields=["user"])
        self.assertEqual(self._submit().json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)

    def test_the_completed_bonus_excludes_the_deduction(self):
        """**B3**（判别点已移到**完成钩子**）：`bonus_amount` 只装佣金，扣点是独立一项——
        折进去会让 P4 **双重扣点**。

        ⭐ R2 之后「交单即写生效绩效」已废 ⇒ 本用例先在**交单**钉「一个金额都不写」，
        再到**完成**（`/transition/` → `COMPLETED`，钩子做 `bonus := temp`）钉「那 10 元扣点
        不折进生效绩效」：`temp_amount = 17.60`（= `88.00 × 20%` 的佣金）⇒ 完成时
        `bonus_amount == 17.60`（**折进扣点的话会是 `17.60 − 10.00 = 7.60`**）。

        ⭐ **修复轮 1 恢复判别力**：本条今天的判别对象是**完成钩子**
        （`settlement_hooks._activate_taker_performance` 的 `bonus_amount := temp_amount`），
        ⛔ 不是「钩子的全部行为」这种自指断言——它能被一条**具体的**坏实现杀掉：
        把那一行改成 `temp_amount − deduct_amount(mode=…, value=…, base=temp_amount)`
        （= 把扣点折进生效绩效）⇒ 断言拿到 `7.60` ⇒ **本用例红**（原始输出见
        `.superpowers/sdd/2026-09-28-order-rules/task-5-fix1-report.md` 的 F2 段）。
        ⚠️ 「折进扣点」是 B3 的**唯一**判别对象：扣点本身照旧由 P4 结算时另算
        （`settlement_amount.payable_net` 的减项），本用例不重复覆盖它。
        """
        self.perf.deduct_mode = "1"
        self.perf.deduct_value = Decimal("10.00")
        self.perf.temp_amount = Decimal("17.60")     # 88.00 × 20% 的佣金；扣点另算
        self.perf.save(update_fields=["deduct_mode", "deduct_value", "temp_amount"])
        # 前置哨兵：夹具必须**可判别**——坏实现会算出 `17.60 − 10.00 = 7.60`，与期望的
        # `17.60` 不同。若谁把这 10 元扣点改成 0，本用例会退化成恒真（那时哨兵先红）。
        self.assertEqual(
            self.perf.temp_amount - self.perf.deduct_value, Decimal("7.60")
        )
        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        self.assertEqual(self._submit().json()["code"], 0)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))     # R2：交单不写金额
        self.assertEqual(self.perf.deduct_value, Decimal("10.00"))    # 扣点原样留着给 P4

        # 完成态：`bonus := temp = 17.60`（佣金），那 10 元扣点仍是独立一项
        grant_nodes(self.taker, "order.change_status_completed")
        body = self.client.post(
            f"/api/business/orders/{self.order.order_no}/transition/",
            {"target": OrderStatus.COMPLETED},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.perf.refresh_from_db()
        self.assertEqual(self.perf.bonus_amount, Decimal("17.60"))    # 佣金，不含那 10 元
        self.assertEqual(self.perf.deduct_value, Decimal("10.00"))

    def test_temp_amount_does_not_follow_the_rate_change_after_claiming(self):
        """⭐ **R2**（反转 D-0049 方案 A）：交单**不再**把 `temp_amount` 刷成实际佣金。

        复刻 D-0049 的时序：录单（大厅价 `100.00`）→ 抛单 → 认领（此时率 20%
        ⇒ `temp_amount = 100.00 × 20 / 100 = 20.00`）→ **认领后、交单前**把该人的
        `StaffProfile.commission_value` 改成 50%（账号线的合法写入点）→ `/submit`。

        R2 之后交单**一个金额都不写**：`temp_amount` 仍是认领时刻的 `20.00`（⛔ 不是 `50.00`）、
        `bonus_amount == 0.00`。⚠️ 代价如实登记：`temp_amount` 同时是**扣点基数**
        （`settlement_amount.payable_net` 的 `base`）⇒ 「认领后、交单前改率」的静默口子
        **按 R2 重新打开**（方向是可能少扣钱），这是 R2 **明知并接受**的代价（spec §8）。

        ⚠️ 期望值一律写字面值（E-0024：不引用生产常量）。
        判别力：恢复 `/submit` 里的 `perf.temp_amount = bonus` ⇒ 下面那条断 `20.00` ⇒ 本用例红。
        """
        order = Order.objects.create(
            game=self.game,
            order_no="20260920000000000042",
            status=OrderStatus.WAITING_GRAB,      # 「抛单」后：池内、有待抢的 MAIN 行
            hall_price=Decimal("100.00"),
        )
        perf = Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("100.00")
        )
        # 认领：走真实抢单路径，按其当时的率（20%）重算预计绩效
        # claim_order 收的是节点集，不是已删除的 Role.TAKER。
        claim_order(
            user_id=self.taker.id, order_id=order.id, nodes={"grab_hall.take_order"}
        )
        perf.refresh_from_db()
        # 前置哨兵：认领时刻的预计值确实是 20.00（否则本用例测不到「交单不改它」）
        self.assertEqual(perf.temp_amount, Decimal("20.00"))
        # 认领之后、交单之前改率（合法写入点，D-0049 的触发条件）
        profile = StaffProfile.objects.get(user=self.taker)
        profile.commission_value = Decimal("50.00")
        profile.save(update_fields=["commission_value"])

        body = self.client.post(
            f"/api/business/orders/{order.order_no}/submit/",
            {}, content_type="application/json",
        ).json()
        self.assertEqual(body["code"], ErrorCode.OK)
        perf.refresh_from_db()
        # R2：交单不写任何金额 ⇒ 预计值停在认领时刻，生效值仍是 0
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertEqual(perf.temp_amount, Decimal("20.00"))    # 字面值，不是常量

    def test_submit_writes_a_performance_submit_row(self):
        """交单留痕：新表一行 `performance.submit`，记下接单人、单号与绩效额。

        ⚠️ 迁移自 `test_submit_writes_a_status_change_audit_row`：旧实现把状态位移
        （`IN_PROGRESS → PENDING_REVIEW`）写进 `from_value`/`to_value`，新动作的
        前后值是**绩效额**（模板 `{target_name}提交了订单 {order_no} 的完单，绩效金额：{to_value}`），
        状态位移已由「订单真的到了 `PENDING_REVIEW`」那条断言钉住（见上一条用例）。
        ⭐ **R2 之后 `to_value` 一律 `"0.00"`**：本次**未写任何金额**（spec §8-5 由实现者定的语义）
        ——⛔ 不再记那个已被删掉的 `bonus`。
        """
        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        self._submit()
        row = OperationLog.objects.get(
            order_no=self.order.order_no, action="performance.submit"
        )
        self.assertEqual(row.log_type, 20)
        self.assertEqual(row.target_name, "taker1")
        # ⭐ 模板首词是 `{actor_name}`（= 交单人本人）
        # ⇒ 名字**只出现一次**（修前是 `【taker1】taker1提交了订单…`，双人名）。
        # `target_name` 列**照旧填**（上一行仍断言 "taker1"），可追溯性不变。
        self.assertEqual(row.summary,
                         f"taker1提交了订单 {self.order.order_no} 的完单，绩效金额：0.00")
        self.assertEqual(row.to_value, "0.00")
        # 一个动作一行：旧的 `STATUS_CHANGE` 那一行由本行取代，不再单独写
        self.assertEqual(
            OperationLog.objects.filter(order_no=self.order.order_no).count(), 1
        )

    def test_the_seeded_worker_can_submit_without_an_explicit_node(self):
        """默认（**不显式给 `nodes=`**）的「代练员」也能交单 —— 端到端，判据在业务码上。

        ⚠️ 本用例与同文件其它用例的**唯一区别**就是 `make_logged_in_client` 不带 `nodes=`：
        那条路只吃**种子给的默认权限集**。所以它同时证明两件事——
        `/submit/` 挂的是 `order.submit_work`，且「代练员」默认持有它（本线 D5 + D6）。
        ⚠️ 必须换一个 username：`make_logged_in_client` 不接受已存在的用户名。
        """
        # （执行期偏差；`setUp` 那座桥与这行 `delete()` 的理由见
        #    `.agents/notes/implemented/architecture/2026-09-23-rbac-roles-and-catalog-consolidation.md`
        #    的「执行期偏离」）：本文件的 `setUp` 用
        #    `nodes=SUBMIT_ORDER_NODES` 把 `order.submit_work` **显式**挂到了同名
        #    `Role("代练员")` 上（夹具自己搭的桥）。不先拆掉这层桥，本用例**即使种子里
        #    没有这个点也会绿**（实测：改种子前它已经 ok）⇒ 对被测改动**零判别力**，
        #    变异验证（A4）杀不死它。删掉这个被污染的 Role，让 `seed_rbac()` 按
        #    `BUILTIN_ROLE_MODULES` **重建**它 —— 这才是真的"只吃种子默认集"。
        RbacRole.objects.filter(name="代练员").delete()
        seed_rbac()   # 幂等；保证内置角色的默认权限集已按新常量落库
        # ⚠️ `games=None`：本用例下面自己建带提成率的档案 ⇒ 夹具必须**不建**，否则撞唯一约束。
        self.client = make_logged_in_client(
            username="taker-default", groups=("代练员",), games=None
        )
        new_taker = User.objects.get(username="taker-default")
        # `/submit/` 有「必须是本单接单人」的闸门 ⇒ 把这单的接单人换成新账号
        self.perf.user = new_taker
        self.perf.save(update_fields=["user"])
        StaffProfile.objects.create(
            user=new_taker, commission_mode=PERCENT, commission_value=Decimal("20.00")
        )
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。本用例新换的接单人
        # 是**发请求方**，且 `/submit/` 经 `get_object()`（被 `game_scope` 收窄的 queryset）
        # 取单 ⇒ 不补本夹具的游戏会拿 **1002**，而本用例断的是「种子默认权限集 ⇒ 交单成功」。
        # ⛔ 这里补的是**游戏**、不是节点：`nodes=` 刻意不传，判别力（只吃种子默认集）不受影响。
        grant_games(new_taker, self.game)
        response = self._submit()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)
        # 只断 200 + 业务码 0 的话，
        #    **「放行但什么都没写成」的实现也能绿**（四个写入点一个都没落时本用例照样过）。
        #    这里把「放行」后面真正要发生的事按**字面值**钉住（E-0009/E-0024 同族：
        #    断言打在动作的落地结果上，不打在状态码上）：
        #    ⭐ R2 之后是「状态到待验收 + 审核状态待审核 + **一个金额都不写**」。
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))

    # ── 授权 ────────────────────────────────────────────────────
    def test_anonymous_is_rejected(self):
        response = Client(SERVER_NAME="tenant.test.com").post(
            self.url, {}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_a_user_without_the_submit_node_is_rejected(self):
        """没有 `/submit/` 的类级节点 `order.submit_work` ⇒ **HTTP 403**（传输层语义）。

        ⚠️ 2026-09-23 单轴化：本条替换了旧的 `test_cs_cannot_submit`（它断的是
        「客服没有 `代练员` 角色」与 msg 里的「代练员」——角色轴已删除）。判据换成
        **节点口径**：只给查看节点、不给交单节点 ⇒ 类级门 403（DRF 默认文案），
        状态一行不动。**「不是接单人」那道资源级判据**由下面两条用例单独钉住。
        """
        self._login("客服", username="cs1", nodes=("order.view_detail",))
        response = self._submit()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], 403)
        # ⚠️ 必须断**msg**——403 有多个来源
        # （CSRF 失败 / 权限类 / 节点门），只断 code 区分不出来源。判据取节点门的默认文案，
        # 并显式排除 CSRF（与 `tests/test_p5_rbac.py` 的 403 可分辨口径同一形状）。
        msg = response.json()["msg"]
        self.assertIn(NODE_DENIED_MSG, msg)
        self.assertNotIn("CSRF", msg)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)

    def test_another_taker_cannot_submit_someone_elses_order(self):
        """⚠️ 只判「是代练员」不够：这张单的接单人**不是**调用方 ⇒ 403。

        判别力：去掉那句 `perf.user_id != request.user.id` ⇒ 代练员 A 能替 B 交单，
        金额会记到 B 头上（本用例变红）。
        """
        other = self._login("代练员", username="taker_other")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。要断的是
        # `perf.user_id != request.user.id` 那道**接单人闸门**（403 + msg），而它排在
        # `get_object()` **之后** ⇒ 不补游戏权限的话，本用例先撞上读侧的 `1002`（HTTP 200），
        # 根本走不到那条闸门。补了也遮不住闸门（实测仍是 403 + "不是你接的"）。
        grant_games(other, self.game)
        response = self._submit()
        self.assertEqual(response.status_code, 403)
        self.assertIn("不是你接的", response.json()["msg"])
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))

    # ── 来源态：不能只靠状态机 ──────────────────────────────────
    def test_submit_requires_in_progress(self):
        self.order.status = OrderStatus.WAITING_GRAB
        self.order.save(update_fields=["status"])
        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        body = self._submit().json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS)
        self.assertIn("代练中", body["msg"])          # 可辨：说清只接受哪个来源态
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_cs_cannot_submit_an_issue_order_through_this_endpoint(self):
        """⛔ **这条是本端点最危险的形状**：`ISSUE → PENDING_REVIEW` 是持节点 16 的人能走的边。

        若只靠状态机判边，持 16 的账号打这个端点就能把一张**问题单**直接送进验收
        （跳过代练员交单）。入口的来源态闸门是唯一兜得住它的写法
        （与 `/withdraw` 拒绝「问题单/暂停单」同理）。
        """
        self.order.status = OrderStatus.ISSUE
        self.order.save(update_fields=["status"])
        cs_issue = self._login("客服", username="cs_issue")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。这一段要断的是
        # **资源级**闸门（403「不是你接的」），而 `get_object()` 排在它**之前** ⇒ 不补游戏
        # 权限就会先撞上读侧的 `1002`（HTTP 200），403 那句断言根本执行不到。
        # ⛔ 补游戏**不会**让它变成接单人：被判的仍是「这张单不是你接的」。
        grant_games(cs_issue, self.game)
        response = self._submit()
        # 先被**资源级**判据拦住（403「该单不是你接的」）：它持有交单节点（类级门放行），
        # 但这条绩效的接单人不是它。双角色那段（下面）走的才是**来源态**判据。
        self.assertEqual(response.status_code, 403)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)

        # 双角色账号（客服+代练员）：类级节点门放行、且它就是接单人 ⇒ 只能靠**来源态**拦住。
        # 这条才是「不能只靠状态机」的真正判别力所在（去掉来源态判据 ⇒ 本段变红）。
        dual = self._login("客服", "代练员", username="dual_issue")
        self.perf.user = dual
        self.perf.save(update_fields=["user"])
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。这一段要断的是
        # **来源态**闸门（业务码 + "代练中"），而它排在 `get_object()` **之后** ⇒ 不补游戏
        # 权限就会先撞上读侧的 `1002`，"代练中"那句断言根本执行不到。
        # 补了也遮不住来源态判据（实测仍是业务码 + "代练中"）。
        grant_games(dual, self.game)
        body = self._submit().json()
        self.assertEqual(body["code"], ErrorCode.BUSINESS)     # 业务码，不是 403
        self.assertIn("代练中", body["msg"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.ISSUE)

    # ── 金额类「预期失败」在 R2 之后**在这条路径上不再可达** ──────
    def test_an_uncomputable_commission_mode_no_longer_blocks_the_submit(self):
        """⭐ **R2 的连带**（spec §4.2.1 明令删掉 `commission_amount(...)` 那句计算）：
        `/submit/` 不再算提成 ⇒ 提成模式算不出（`FIXED`）**不再**是交单的失败原因。

        ⚠️ 旧口径下这里断 `1000 + "固定金额"` 且状态不动；那个分支随计算一起消失。
        判据**没有放宽**：仍按字面值钉住「放行 + 状态到位 + 一个金额都不写」。
        """
        profile = StaffProfile.objects.get(user=self.taker)
        profile.commission_mode = CommissionMode.FIXED
        profile.save(update_fields=["commission_mode"])
        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        body = self._submit().json()
        self.assertEqual(body["code"], ErrorCode.OK, body)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))

    def test_no_price_anywhere_no_longer_blocks_the_submit(self):
        """同上：没有有效基价（`hall_price=None`）**不再**拦交单——交单不算钱。

        判别力：任何把 `effective_taker_price(perf)` / `commission_amount(...)` 加回
        `/submit/` 的实现都会在这里以 `1000`（而不是 `0`）失败。
        """
        self.order.hall_price = None
        self.order.save(update_fields=["hall_price"])
        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        body = self._submit().json()
        self.assertEqual(body["code"], ErrorCode.OK, body)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self.perf.review_status, ReviewStatus.PENDING)
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))

    # ── 事务原子性 ──────────────────────────────────────────────
    def test_rolls_back_status_and_review_status_when_the_audit_write_fails(self):
        """打桩点放在块内**最后一笔**写入（`record`）——它之前的两笔都已落库。

        两处断言**都必须**有：只断状态会漏掉「审核状态被写了但状态回滚」的那种半成功。
        ⚠️ R2 之后块内不再写金额 ⇒ 第二笔哨兵从 `bonus_amount` 换成 `review_status`
        （它仍是块内真实落库的那一笔），判据强度不变。
        """
        seen = {}

        def _boom(**kwargs):
            seen["status"] = Order.objects.get(pk=self.order.pk).status
            seen["review_status"] = Performance.objects.get(
                pk=self.perf.pk
            ).review_status
            raise RuntimeError("boom")

        # setUp 已以接单人（taker1）身份真实登录；夹具每次调用都会建号，这里不重复登录
        with mock.patch(
            "apps.business.api.order_views.record", side_effect=_boom
        ):
            with self.assertRaises(RuntimeError):
                self._submit()
        # 哨兵：抛错前两笔**真的**落库了（否则「回滚」对「根本没写」不加区分）
        self.assertEqual(seen["status"], OrderStatus.PENDING_REVIEW)
        self.assertEqual(seen["review_status"], ReviewStatus.PENDING)
        self.order.refresh_from_db()
        self.perf.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)   # 状态回滚
        self.assertEqual(self.perf.bonus_amount, Decimal("0.00"))      # 金额一个字没写
        self.assertEqual(self.perf.temp_amount, Decimal("88.00"))
        self.assertIsNone(self.perf.review_status)                     # 审核状态也回滚
