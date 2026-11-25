"""软删 / 回收站 / 恢复三个端点的行为（spec §8-T1 第 1–5 条 / §8-T2 第 6–9 条；
D5/D6/D7 与 §5.1/§5.2/§5.3 的契约）。

⚠️ 留痕断言打**数据库行**（E-0009 / E-0024），不断言 logger。
⚠️ 跨租户的否定断言**用订单号指认、不用主键**（§5-G）。
⚠️ 夹具值一律合成（E-0077）。
"""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

ALIVE_NO = "20260924000000000201"
#: 第二笔已删单的单号（回收站排序用例）：删除时间**更早**，用来暴露排序方向。
OLDER_NO = "20260924000000000202"
#: 操作人的合成用户名（认证态里 `deleted_by` 的取值来源）。
ACTOR_USERNAME = "测试操作人甲"
#: ⭐ 订单日志人名线 spec §3-B2：操作人的**昵称**。
#: ⚠️ 必须与 `ACTOR_USERNAME` **不同形**（E-0077 + spec §5 的夹具纪律）：
#: 两者相同的话「`deleted_by_name` 走的是昵称还是账号」这条判据会**假绿**。
ACTOR_NICKNAME = "测试昵称操作人丙"


class _RecycleFixture(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(
            game=self.game, order_no=ALIVE_NO, total_price=Decimal("100.00")
        )
        self.taker = User.objects.create_user(
            username="测试打手甲", password="pw-测试打手甲"
        )
        self.perf_main = Performance.objects.create(
            order=self.order,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
            user=self.taker,
        )
        self.perf_sales = Performance.objects.create(
            order=self.order, emp_type=EmpType.SALES, temp_amount=Decimal("5.00")
        )
        self.client = make_logged_in_client(
            username=ACTOR_USERNAME,
            # ⚠️ `nodes=` 必须与 `groups=` 成对给：节点挂在 **Group → Role** 上
            # （`_grant_nodes` 遍历用户已有的组），没有组 ⇒ 一个节点都挂不上 ⇒ 全 403。
            groups=("回收组",),
            # `my_performance.export` 是绩效列表端点的准入门（`PerformanceViewSet` 的
            # `list` 节点门）——恢复用例要断言「绩效行**不**回到绩效列表」，没有它那条请求会 403。
            nodes=(
                "order.delete", "order.recycle", "order.list",
                "my_performance.export",
                # ⭐ N6：恢复后人工重抛走既有 `/throw-to-hall/`（D-0308 的 36 门 +
                # 带价那道 1162；`order.set_offer_price` 是既有的发单价写入节点）。
                "order.send_to_grab_hall", "order.submit_payout_price",
                "order.set_offer_price",
            ),
        )
        # `make_logged_in_client` 返回的是 Django 测试 client（没有 `.user` 属性）
        # ⇒ 操作人用户名按夹具**自己声明的那个**取，而不是回头问 client。
        self.actor = ACTOR_USERNAME
        # ⭐ spec §3-B2）：给操作人一个**与登录账号不同形**的昵称
        #    ⇒ 「删除人显示账号名称」那条判据才有判别力（两者相同时它会假绿）。
        User.objects.filter(username=ACTOR_USERNAME).update(first_name=ACTOR_NICKNAME)

    def _delete(self, is_del_emp, no=ALIVE_NO):
        return self.client.post(
            f"/api/business/orders/{no}/delete/",
            {"isDelEmp": is_del_emp},
            content_type="application/json",
        )


class SoftDeleteLandingTests(_RecycleFixture):
    """第 1–5 条。"""

    def test_the_delete_response_deleted_at_is_a_project_timezone_string(self):
        """D-0302（2026-10-24）：删除响应的 `deleted_at` 是**项目时区**（+08:00）的 ISO 串。

        管理端删除弹窗按串截显；旧写法裸 `.isoformat()` 出 UTC 串、与订单审计行
        （`order_views.py::_log_row` 已是 `local_iso`）同一页两个时钟。判据走**幂等分支**
        （可固定时刻）：第一趟真删（`deleted_at` = now），把列钉到 UTC 04:30，再删一趟
        ⇒ 响应回 `previous`（第一次的值）的北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        first = self._delete("N").json()
        self.assertEqual(first["code"], 0, first)
        # ⚠️ 已删行走 `all_objects`（默认 manager 排除软删行，D-0144——用它会静默 update 0 行）。
        Order.all_objects.filter(order_no=ALIVE_NO).update(
            deleted_at=dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc),
        )
        second = self._delete("N").json()
        self.assertEqual(second["code"], 0, second)
        self.assertEqual(second["data"]["deleted_at"], "2026-10-24T12:30:00+08:00")
        # ⛔ 不是 UTC 墙钟（防回退）。
        self.assertNotEqual(second["data"]["deleted_at"], "2026-10-24T04:30:00+00:00")

    def test_y_deletes_the_order_and_all_its_performance_rows(self):
        body = self._delete("Y").json()
        self.assertEqual(body["code"], 0)
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertIsNotNone(row.deleted_at)
        self.assertEqual(row.deleted_by, self.actor)
        for perf in Performance.objects.filter(order=row):
            self.assertIsNotNone(perf.deleted_at, f"绩效行 {perf.emp_type} 没被标记")
            self.assertEqual(perf.deleted_by, self.actor)
        self.assertEqual(body["data"]["isDelEmp"], "Y")

    def test_n_leaves_every_performance_row_untouched(self):
        self._delete("N")
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertIsNotNone(row.deleted_at)
        for perf in Performance.objects.filter(order=row):
            self.assertIsNone(perf.deleted_at)
            self.assertEqual(perf.deleted_by, "")

    def test_default_is_n_when_is_del_emp_is_omitted(self):
        self.client.post(
            f"/api/business/orders/{ALIVE_NO}/delete/", {}, content_type="application/json"
        )
        for perf in Performance.objects.filter(order_id=self.order.pk):
            self.assertIsNone(perf.deleted_at)

    def test_deleting_twice_is_idempotent_and_does_not_rewrite_or_relog(self):
        # D-0302：响应里的 `deleted_at` 是 `local_iso`（项目时区 +08:00）⇒
        # 与库行同源对拍也走同一条出口（旧写法裸 `.isoformat()` 的 UTC 形状不再成立）。
        from apps.core.datetime_display import local_iso

        first = self._delete("Y").json()["data"]["deleted_at"]
        second = self._delete("Y").json()
        self.assertEqual(second["code"], 0)
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertEqual(local_iso(row.deleted_at), first)   # **逐字相等**，不被改写
        logs = OperationLog.objects.filter(action="order.delete")
        self.assertEqual(logs.count(), 1)
        # 分类来自登记表 `ACTION_LOG_TYPES["order.delete"]`。这里钉**字面值 10**（E-0024）：
        # 把登记表那一项改成别的分类时，这条留痕用例自己就红（而不是只剩键集对拍会红）。
        self.assertEqual(logs.get().log_type, 10)

    def test_a_later_y_delete_marks_performance_on_an_already_deleted_order(self):
        """业务方 2026-09-24 裁定：已删状态下 `isDelEmp="Y"` 仍要真的删绩效行。

        先以 `N` 删（订单删了、绩效行不动），再以 `Y` 删 ⇒ 绩效行**必须被标掉**，
        而订单两列与审计行保持第一次的值（幂等不变）。
        """
        from apps.core.datetime_display import local_iso

        first = self._delete("N").json()["data"]["deleted_at"]
        for perf in Performance.objects.filter(order_id=self.order.pk):
            self.assertIsNone(perf.deleted_at)          # 夹具前提：第一趟是 N
        body = self._delete("Y").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["isDelEmp"], "Y")
        self.assertEqual(body["data"]["deleted_at"], first)   # 订单的删除时间逐字不变
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertEqual(local_iso(row.deleted_at), first)
        self.assertEqual(row.deleted_by, self.actor)
        for perf in Performance.objects.filter(order=row):
            self.assertIsNotNone(perf.deleted_at, f"绩效行 {perf.emp_type} 仍未被标记")
            self.assertEqual(perf.deleted_by, self.actor)
        self.assertEqual(
            OperationLog.objects.filter(action="order.delete").count(), 1
        )

    def test_the_idempotent_y_delete_does_not_refresh_an_already_marked_row(self):
        """已标记的绩效行**不被刷新** `deleted_at`（那是历史事实，不是本次时间）。"""
        self._delete("N")
        old = Order.all_objects.get(order_no=ALIVE_NO).deleted_at - timedelta(days=7)
        Performance.objects.filter(pk=self.perf_sales.pk).update(
            deleted_at=old, deleted_by="测试历史操作人"
        )
        self._delete("Y")
        self.perf_sales.refresh_from_db()
        self.assertEqual(self.perf_sales.deleted_at, old)           # 逐字不变
        self.assertEqual(self.perf_sales.deleted_by, "测试历史操作人")
        self.perf_main.refresh_from_db()
        self.assertIsNotNone(self.perf_main.deleted_at)             # 未标记的那行被标上
        self.assertEqual(self.perf_main.deleted_by, self.actor)

    def test_a_missing_order_number_returns_business_code_1002(self):
        body = self._delete("N", no="20260924000000009999").json()
        self.assertEqual(body["code"], 1002)

    def test_delete_is_post_only(self):
        """D5 的第三道闸：`DELETE /orders/<单号>/delete/` 仍是 405。"""
        resp = self.client.delete(f"/api/business/orders/{ALIVE_NO}/delete/")
        self.assertEqual(resp.status_code, 405)

    def test_invalid_is_del_emp_value_is_business_code_1000(self):
        body = self._delete("X").json()
        self.assertEqual(body["code"], 1000)
        self.assertIsNone(
            Order.all_objects.get(order_no=ALIVE_NO).deleted_at
        )   # 校验失败**什么都没写**


class RestoreSymmetryTests(_RecycleFixture):
    """第 6–9 条：恢复的**逐列对称**（§6.2）——对称面只到订单两列为止（绩效不随恢复，见
    `RestoreKeepsPerformanceRowsDeletedTests`）。"""

    def test_restore_clears_the_order_but_leaves_its_performance_rows_deleted(self):
        """⭐ 业务方 2026-11-05 裁定：恢复只恢复订单本身，绩效行**不**随订单恢复。

        Y 删把两行绩效随订单一起标记（既有行为，D-0144 不变）；恢复只清订单两列，
        绩效行的 `deleted_at` / `deleted_by` **原样保留**（要动请走「绩效管理」）。
        """
        self._delete("Y")
        marked_at = Order.all_objects.get(order_no=ALIVE_NO).deleted_at
        body = self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/").json()
        self.assertEqual(body["code"], 0)
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(row.deleted_by, "")
        # **第二条独立断言**：绩效行**仍处于已删**（⛔ 不随订单复活；删除时刻逐字不变）
        for perf in Performance.objects.filter(order=row):
            self.assertIsNotNone(perf.deleted_at, f"绩效行 {perf.emp_type} 被恢复了")
            self.assertEqual(perf.deleted_at, marked_at)
            self.assertEqual(perf.deleted_by, self.actor)
        # 恢复的留痕同样走登记表 `ACTION_LOG_TYPES["order.restore"]`（钉字面值 10）。
        self.assertEqual(
            OperationLog.objects.get(action="order.restore").log_type, 10
        )

    def test_restore_after_an_n_delete_is_a_noop_on_performance_rows(self):
        self._delete("N")
        self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/")
        for perf in Performance.objects.filter(order_id=self.order.pk):
            self.assertIsNone(perf.deleted_at)   # 从头到尾都是 None

    def test_restoring_a_live_order_writes_nothing_and_no_log(self):
        body = self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/").json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            OperationLog.objects.filter(action="order.restore").count(), 0
        )

    def test_the_restored_order_comes_back_to_the_list_but_not_to_performance(self):
        self._delete("Y")
        # 删除期间绩效列表也看不到它（T2 的绩效读路径排除：`order__deleted_at__isnull=True`）。
        gone = self.client.get("/api/business/performances/").json()
        self.assertNotIn(ALIVE_NO, [r["order_no"] for r in gone["data"]["items"]])
        self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/")
        body = self.client.get("/api/business/orders/").json()
        self.assertIn(ALIVE_NO, [r["order_no"] for r in body["data"]["items"]])
        perf = self.client.get("/api/business/performances/").json()
        nos = [r["order_no"] for r in perf["data"]["items"]]
        # ⭐ 业务方 2026-11-05 裁定：恢复不恢复绩效行 ⇒ 行自身的 `deleted_at` 仍在，
        # 绩效列表的读闸（`deleted_at__isnull=True`，performance_views `_base_queryset`）
        # 继续看不到它——要动请去「绩效管理」。
        self.assertNotIn(ALIVE_NO, nos)


class RestoreInHallOrderTests(_RecycleFixture):
    """⭐ N6（R3-M7）：恢复一张**删除时在厅**的单 ⇒ 回「未分配」、等人工重抛。

    ⛔ 不自动回厅：恢复侧不排 `hall.throw`、不排 `jidanpu_member.queue_hall_launch`；
    人工走既有「抛大厅」入口（D-0308 的 36 闸照走）。
    """

    def _downstream_binding(self):
        from apps.business.models import PeerDownstreamBinding
        from apps.business.peer_sync import hash_token, new_token

        return PeerDownstreamBinding.objects.create(
            account=User.objects.get(username=ACTOR_USERNAME),
            peer_base_url="https://syn-recycle-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=True,
        )

    def _restore(self, no=ALIVE_NO):
        return self.client.post(f"/api/business/orders/{no}/restore/").json()

    def test_restoring_an_in_hall_order_returns_it_to_unassigned(self):
        """① 删除时在厅 ⇒ 恢复后状态 = `UNASSIGNED`；恢复这一步**不新增**任何事件、
        `HallExitSnapshot` 仍是 1 条（删除时那条灰卡不删）。"""
        from apps.business.models import HallExitSnapshot, PeerSyncEvent

        self._downstream_binding()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.WAITING_GRAB)
        self._delete("Y")
        self.assertEqual(HallExitSnapshot.objects.count(), 1)
        events_before = PeerSyncEvent.objects.count()
        self.assertGreaterEqual(events_before, 1, "删除在厅单应排一条 hall.take")

        body = self._restore()
        self.assertEqual(body["code"], 0, body)
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertEqual(row.status, OrderStatus.UNASSIGNED)
        self.assertIsNone(row.deleted_at)
        self.assertEqual(
            PeerSyncEvent.objects.count(), events_before,
            "恢复侧凭空排了重抛事件（本批明令不排）",
        )
        self.assertEqual(HallExitSnapshot.objects.count(), 1, "恢复把灰卡删了")

    def test_the_restored_order_can_be_thrown_to_the_hall_by_hand(self):
        """② 恢复后人工 `/throw-to-hall/` 成功 ⇒ 状态回 `WAITING_GRAB`、新排一条
        `hall.throw`；删除时那条旧 `hall.take` 证据**原样还在**。"""
        from apps.business.models import PeerSyncEvent
        from apps.business.peer_sync import HALL_TAKE_KIND, HALL_THROW_KIND

        self._downstream_binding()
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.WAITING_GRAB)
        self._delete("Y")
        old_take = PeerSyncEvent.objects.get(kind=HALL_TAKE_KIND)
        old_take_pk = old_take.pk
        self._restore()
        # 夹具给 MAIN 行挂了接手人；抛大厅要求先清人（业务入口的既有闸，不是本批新增）。
        Performance.objects.filter(
            order=self.order, emp_type=EmpType.MAIN
        ).update(user=None)

        resp = self.client.post(
            f"/api/business/orders/{ALIVE_NO}/throw-to-hall/",
            {"price": "100.00"},
            content_type="application/json",
        )
        body = resp.json()
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(body["code"], 0, body)
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertEqual(row.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND).count(), 1
        )
        # 旧 take 证据不被改写 / 删除（`(created_at,id)` 投递序与作废靠它）。
        self.assertTrue(PeerSyncEvent.objects.filter(pk=old_take_pk).exists())

    def test_restoring_a_non_in_hall_order_leaves_its_status_alone(self):
        """③ 负例：恢复 `IN_PROGRESS` / `COMPLETED` 等非在厅单 ⇒ 状态原样、零事件。"""
        from apps.business.models import PeerSyncEvent

        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        self._delete("N")
        events_before = PeerSyncEvent.objects.count()
        body = self._restore()
        self.assertEqual(body["code"], 0, body)
        row = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertEqual(row.status, OrderStatus.IN_PROGRESS)
        self.assertEqual(PeerSyncEvent.objects.count(), events_before)

    def test_restoring_twice_writes_one_restore_log(self):
        """④ 幂等：恢复两次只处理一次（`deleted_at is None` 早返回），留痕一条。"""
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.WAITING_GRAB)
        self._delete("Y")
        self._restore()
        self._restore()
        self.assertEqual(
            OperationLog.objects.filter(action="order.restore").count(), 1
        )

    def test_a_hidden_order_is_never_forced_back_to_unassigned(self):
        """⑤ 隐藏单守卫：`hall_card_id` 非空的隐藏单恢复**不**被本批改状态。

        本批不碰隐藏单的删除 / 恢复语义——它的状态由卡的生命周期管。
        """
        from apps.business.models import PeerHallMirror, PeerUpstreamBinding
        from apps.business.peer_sync import hash_token, new_token

        binding = PeerUpstreamBinding.objects.create(
            peer_name="N6 隐藏单夹具上家",
            peer_base_url="https://syn-recycle-hidden-up.test.invalid",
            peer_account="syn-recycle-hidden",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://syn-recycle-hidden-local.test.invalid",
            bound_by=User.objects.get(username=ACTOR_USERNAME),
            hall_sync_enabled=True,
        )
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no="N6-HIDDEN-1",
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload={"game_name": self.game.name, "hall_price": "50.00"},
            remote_hall_price=Decimal("50.00"),
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )
        hidden = Order.objects.create(
            game=self.game,
            order_no="20260924000000000299",
            status=OrderStatus.WAITING_GRAB,
            hall_card=card,
            hall_price=Decimal("50.00"),
        )
        Order.all_objects.filter(pk=hidden.pk).update(
            deleted_at=timezone.now(), deleted_by=ACTOR_USERNAME
        )
        # ⚠️ 端点的 `_order_including_deleted` **有意排除隐藏单**（读面白名单，N3 的
        #    `test_order_read_path_net` 钉着）⇒ 这条守卫只能直接驱动领域函数。
        from apps.business.recycle import restore_order

        hidden.refresh_from_db()
        restore_order(
            order=hidden, actor=User.objects.get(username=ACTOR_USERNAME)
        )
        hidden = Order.all_objects.get(pk=hidden.pk)
        self.assertEqual(hidden.status, OrderStatus.WAITING_GRAB)
        self.assertIsNone(hidden.deleted_at)


class RestoreKeepsPerformanceRowsDeletedTests(_RecycleFixture):
    """⭐ 业务方 2026-11-05 裁定：恢复只恢复订单本身，绩效行**不**随订单恢复。

    此前是「`deleted_at >= 订单删除时刻` 的绩效行一并复活」（2026-09-25 的不对称恢复口径），
    该行为废止。三条支路（垃圾桶早删 / 同一次调用随单标记 / 晚一档的 Y 补删）**在同一个
    测试类里各跑一遍**：每一条恢复后都必须**仍处于已删**（`deleted_at` / `deleted_by` 不变）。
    """

    #: 垃圾桶（`POST /performances/<id>/delete/`）的门：1162 是软删的底门；
    #: 19 是「这条 MAIN 行有接单人 ⇒ 连带清接手人」那道门（本批改前仍要求）。
    BIN_NODES = ("order.submit_payout_price", "order.clear_taker")

    def _bin_delete(self, performance_id, username, group):
        # ⚠️ `group` 会变成 `Role.name`（`max_length=5`）⇒ 组名必须短。
        client = make_logged_in_client(
            username=username, groups=(group,), nodes=self.BIN_NODES
        )
        return client.post(
            f"/api/business/performances/{performance_id}/delete/"
        ).json()

    def test_a_row_deleted_by_the_bin_before_the_order_stays_deleted_on_restore(self):
        """支路 1：垃圾桶**早于**订单删除时刻单独删掉的行，恢复后**仍然是已删**。"""
        # 垃圾桶的「清接手人」那一步要走 `IN_PROGRESS → UNASSIGNED`；默认 `UNASSIGNED`
        # 是自环而状态机没有自环边 ⇒ 夹具先把它置成 `IN_PROGRESS`。
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.IN_PROGRESS)
        self.assertEqual(
            self._bin_delete(self.perf_main.id, "测试垃圾桶操作人甲", "桶组甲")["code"], 0
        )
        self.perf_main.refresh_from_db()
        bin_deleted_at = self.perf_main.deleted_at
        self.assertIsNotNone(bin_deleted_at)          # 前提：垃圾桶那一趟真的删成了

        # 再软删订单（Y ⇒ 仍未被标记的 SALES 行随订单一起标记）。
        self.assertEqual(self._delete("Y").json()["code"], 0)
        order = Order.all_objects.get(order_no=ALIVE_NO)
        self.perf_sales.refresh_from_db()
        self.assertIsNotNone(self.perf_sales.deleted_at)     # 前提：它随订单被标上
        # 前提哨兵：两条删除时刻**可分辨**（相等时分不清行是被哪条路标掉的）。
        self.assertLess(bin_deleted_at, order.deleted_at)

        self.assertEqual(
            self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/").json()["code"],
            0,
        )
        self.assertIsNone(Order.all_objects.get(order_no=ALIVE_NO).deleted_at)

        # 随订单一起标记的那行**也不复活**（新裁定）：删除时刻与标记人原样保留。
        self.perf_sales.refresh_from_db()
        self.assertIsNotNone(self.perf_sales.deleted_at)
        self.assertEqual(self.perf_sales.deleted_at, order.deleted_at)
        self.assertEqual(self.perf_sales.deleted_by, self.actor)
        # 垃圾桶单独删掉的那行原样（历史时刻不被改写）。
        self.perf_main.refresh_from_db()
        self.assertEqual(self.perf_main.deleted_at, bin_deleted_at)
        self.assertEqual(self.perf_main.deleted_by, "测试垃圾桶操作人甲")

    def test_rows_marked_by_the_same_delete_call_share_the_timestamp_and_stay_deleted(self):
        """支路 2（边界）：同一次操作标记的行与订单**逐字同一个时间戳**（soft_delete 口径
        不变），但恢复后**仍处于已删**——旧「`>=` 判据复活它们」的行为废止。"""
        self.assertEqual(self._delete("Y").json()["code"], 0)
        order = Order.all_objects.get(order_no=ALIVE_NO)
        self.perf_main.refresh_from_db()
        self.perf_sales.refresh_from_db()
        self.assertEqual(self.perf_main.deleted_at, order.deleted_at)
        self.assertEqual(self.perf_sales.deleted_at, order.deleted_at)

        self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/")
        self.assertIsNone(Order.all_objects.get(order_no=ALIVE_NO).deleted_at)
        self.perf_main.refresh_from_db()
        self.perf_sales.refresh_from_db()
        self.assertIsNotNone(self.perf_main.deleted_at)
        self.assertEqual(self.perf_main.deleted_at, order.deleted_at)
        self.assertIsNotNone(self.perf_sales.deleted_at)
        self.assertEqual(self.perf_sales.deleted_at, order.deleted_at)

    def test_rows_marked_by_a_later_y_delete_stay_deleted_on_restore_too(self):
        """支路 3（边界）：「先 N 删订单、再 Y 补删绩效」那一档同样**不**随恢复复活。

        ⚠️ 它不是「被垃圾桶单独删掉」：那条路要求订单还活着（订单已删时
        `get_object()` 就落 `1002`）⇒ 订单删除期间不能有绩效行被垃圾桶单独删。
        """
        self._delete("N")
        order_after_n = Order.all_objects.get(order_no=ALIVE_NO)
        self.assertEqual(self._delete("Y").json()["code"], 0)
        self.perf_main.refresh_from_db()
        self.assertIsNotNone(self.perf_main.deleted_at)
        self.assertLess(order_after_n.deleted_at, self.perf_main.deleted_at)

        self.client.post(f"/api/business/orders/{ALIVE_NO}/restore/")
        self.assertIsNone(Order.all_objects.get(order_no=ALIVE_NO).deleted_at)
        self.perf_main.refresh_from_db()
        self.perf_sales.refresh_from_db()
        self.assertIsNotNone(self.perf_main.deleted_at)
        self.assertIsNotNone(self.perf_sales.deleted_at)
        self.assertEqual(self.perf_main.deleted_by, self.actor)
        self.assertEqual(self.perf_sales.deleted_by, self.actor)


class RecycleBinListTests(_RecycleFixture):
    """§5.2 的列表契约（后端一半；前端一半在 T6）。"""

    def test_the_bin_returns_exactly_the_deleted_rows_newest_first(self):
        self._delete("N")
        newest = Order.all_objects.get(order_no=ALIVE_NO).deleted_at
        # 第二笔：删除时间**明确更早**。直接写 `deleted_at`（不走端点）是为了让两笔时间
        # 必然可分辨——连发两次请求的时间差小到可能相等，那样排序方向就不可辨了。
        older = Order.objects.create(
            game=self.game, order_no=OLDER_NO, total_price=Decimal("50.00")
        )
        Order.all_objects.filter(pk=older.pk).update(
            deleted_at=newest - timedelta(hours=1), deleted_by=self.actor
        )
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0)
        # 恰好是这两笔、且**最新在前**（升序排序会得到 [OLDER_NO, ALIVE_NO] ⇒ 必红）。
        self.assertEqual(
            [r["order_no"] for r in body["data"]["items"]], [ALIVE_NO, OLDER_NO]
        )
        self.assertEqual(body["data"]["total"], 2)

    def test_the_bin_row_carries_the_delete_time_and_deleter_verbatim(self):
        """§5.2 / §7.3 第 10、11 列：删除时间与删除人必须在行里、且逐字等于库里的值。

        ⚠️ DRF 的 `DateTimeField.to_representation`（`rest_framework/fields.py:1215-1230`
        已读过）会先把值转成**当前时区**再 `isoformat()`，所以期望串按同一规则算，
        而不是拿 UTC 的 `row.deleted_at.isoformat()` 去比（那会差 8 小时）。
        """
        self._delete("N")
        row = Order.all_objects.get(order_no=ALIVE_NO)
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1)
        expected_deleted_at = row.deleted_at.astimezone(
            timezone.get_current_timezone()
        ).isoformat()
        self.assertEqual(items[0]["deleted_at"], expected_deleted_at)
        self.assertEqual(items[0]["deleted_by"], self.actor)
        # ⭐ spec §3-B2 / 判据 7）：`deleted_by` 仍是**登录账号**（键名与语义都没变），
        # 新增的 `deleted_by_name` 才是**账号名称**（昵称）。夹具刻意让两者不同形 ⇒ 判别力成立。
        self.assertEqual(items[0]["deleted_by_name"], ACTOR_NICKNAME)
        self.assertNotEqual(items[0]["deleted_by_name"], self.actor)
        # 库里那一列**一个字都没被改写**（读面解析，不是落新值）。
        row.refresh_from_db()
        self.assertEqual(row.deleted_by, self.actor)

    #: 回收站行**恰好**承诺的键集合（**逐字列出**，⛔ 不从序列化器导出：自指断言在字段被改坏时
    #: 不会红）。它 = `OrderListSerializer.Meta.fields` 那 25 个 + 两个删除列 + 本修复补的
    #: `tag` / `game_account`（前端「订单号」徽标与「角色信息」列要读这两个键）
    #: + ⭐ 补的 `deleted_by_name`（删除人的**账号名称**，订单日志人名线 spec §3-B2）。
    #: ⚠️ `deleted_by`（登录账号快照）**仍在**：一个是显示值、一个是身份追溯，⛔ 不是替换。
    #: + ⭐ 补的 `taker_settled`（钱包发薪线给 `OrderListSerializer.Meta.fields`
    #: 加的「打手绩效是否已结账」；回收站的取数侧没有那个注解 ⇒ 值恒 `False`，但**键在**）。
    #: + ⭐ 补的 `platform_push`（T10a 平台推送读面给基类加的「这单已抛到平台」；
    #: 回收站的取数侧没有预取 ⇒ 值恒 `None`，但**键在**）。
    #:
    #: ⚠️ 判据是**恰好**（`==`），不是「缺键集合为空」——本仓 `tests/test_order_list_columns.py`
    #: 的 `TEN_KEYS` 那套只挡「少了」，而 T6 抓到的致命缺陷是**另一端**：前端行映射读了后端
    #: **不返回**的键 ⇒ `escapeHtml(undefined)` 在 `parseData` 里抛、整张表一行都渲染不出来。
    #: 「恰好」是唯一能让「前端多读了一个键」在后端红掉的形状（`assertIn` 永远挡不住它）。
    RECYCLE_ROW_KEYS = frozenset({
        "completed_at",
        "created_at",
        "created_by",
        "customer_phone",
        "customer_ww",
        "deleted_at",
        "deleted_by",
        "deleted_by_name",
        "game",
        "game_account",
        "game_role_name",
        "hall_price",
        "hall_publisher_name",
        "is_urgent",
        "net_received",
        "order_no",
        "payment_method",
        "payment_status",
        "platform_push",
        "project_text",
        "refund_fee",
        "remark",
        "sales_agent",
        "server",
        "source",
        "source_order_no",
        "status",
        "tag",
        "taker_name",
        "taker_settled",
        "time_requirement",
        "total_price",
    })

    def test_the_bin_row_key_set_is_exactly_the_promise(self):
        """行的键集合**恰好**等于 `RECYCLE_ROW_KEYS`（多一个、少一个都红）。

        判别力（两向）：删掉序列化器 `fields` 里的 `tag` ⇒ 少键、本用例红；
        给 `RecycleBinListSerializer` 多塞一个前端不认的键 ⇒ 多键、本用例同样红。
        """
        self._delete("N")
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)  # 非空哨兵：空表上「键集相等」是恒假的绿
        self.assertEqual(set(items[0]), set(self.RECYCLE_ROW_KEYS))

    def test_the_bin_row_carries_the_tag_and_game_account_values(self):
        """`tag` / `game_account` 是**真列**（`Order.tag` / `Order.game_account`），值逐字回出。

        ⚠️ 与上一条分工：上一条钉**键**（T6 的致命缺口），这一条钉**值真的来自库里的列**
        （只看键在不在，一个恒返回空串的桩也能骗过它）。
        """
        self._delete("N")
        Order.all_objects.filter(order_no=ALIVE_NO).update(
            tag="测试标记甲", game_account="测试账号甲"
        )
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)  # 非空哨兵
        self.assertEqual(items[0]["tag"], "测试标记甲")
        self.assertEqual(items[0]["game_account"], "测试账号甲")

    def test_deleted_by_name_falls_back_to_the_account_when_the_user_is_gone(self):
        """⭐ 2026-10-17（spec §3-B2 / 判据 7 的回落那半）：查不到人 ⇒ `deleted_by_name` 回落账号。

        场景：删除人是**已硬删 / 改名**的账号（`deleted_by` 只是文本快照）。
        ⚠️ 这是**显示**用的回落，不是「猜一个人」：它 ⛔ 不改 `deleted_by`、也 ⛔ 不回空串
        （空串会让前端那格变成空白，读者以为「没有删除人」）。
        """
        self._delete("N")
        # 合成账号：租户里**没有**这个用户（前置断言保证走的是回落支，不是真解析到的支）
        ghost = "已删账号乙"
        self.assertFalse(User.objects.filter(username=ghost).exists())
        Order.all_objects.filter(order_no=ALIVE_NO).update(deleted_by=ghost)
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 1, items)   # 非空哨兵
        self.assertEqual(items[0]["deleted_by"], ghost)
        self.assertEqual(items[0]["deleted_by_name"], ghost)

    def test_a_page_of_rows_costs_exactly_one_user_lookup(self):
        """⭐ 2026-10-17（spec §3-B2 / 判据 8）：**一页 N 行只发一次** `auth_user` 查询。

        判别力：把映射挪进 `SerializerMethodField` 里逐行 `User.objects.get(...)`
        ⇒ 行数变多时这条计数跟着涨、当场红（那是 N+1）。
        ⚠️ 只数**命中 `auth_user` 表**的 SELECT：其余查询（订单、游戏、权限…）与本次改动无关，
        把它们算进来会让这条判据变成「整页查询数」那种一碰就红、只能靠改数字维持的网。
        """
        # 三笔已删单（同一个删除人 ⇒ 去重后应当仍是**一次**查询）。
        extra_nos = (OLDER_NO, "20260924000000000203")
        for no in extra_nos:
            Order.objects.create(
                game=self.game, order_no=no, total_price=Decimal("1.00")
            )
        for no in (ALIVE_NO, *extra_nos):
            Order.all_objects.filter(order_no=no).update(
                deleted_at=timezone.now(), deleted_by=self.actor
            )
        with CaptureQueriesContext(connection) as ctx:
            body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0, body)
        items = body["data"]["items"]
        self.assertEqual(len(items), 3, items)   # 非空哨兵：三行都在（去重真的发生了）
        self.assertEqual({row["deleted_by_name"] for row in items}, {ACTOR_NICKNAME})
        # ⚠️ 判据只认**批量映射那一条**：`WHERE "auth_user"."username" IN (…)`。
        #    ⛔ 不数「所有带 auth_user 的查询」——会话 / 权限那条 `WHERE "auth_user"."id" = …`
        #    与订单 JOIN `hall_publisher_id` 也会带这个表名，数进来就是把噪声当判据。
        # ⚠️ 判别力仍在：实现若改成逐行 `User.objects.get(username=…)`，这条 `IN` 查询会**消失**
        #    （计数 0 ≠ 1 ⇒ 本用例红）。
        user_queries = [
            q["sql"] for q in ctx.captured_queries
            if '"auth_user"."username" IN' in q["sql"]
        ]
        self.assertEqual(
            len(user_queries), 1,
            f"一页 3 行只许一次 auth_user 查询，实际 {len(user_queries)} 次：{user_queries}",
        )
        # 反向：逐行 `filter(username=…)` 的形态**一条都不许有**（只数 IN 会漏掉这种实现）。
        per_row = [
            q["sql"] for q in ctx.captured_queries
            if '"auth_user"."username" =' in q["sql"]
        ]
        self.assertEqual(per_row, [], f"不许逐行按 username 查用户：{per_row}")

    def test_the_bin_excludes_live_orders(self):
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["data"]["total"], 0)

    def test_deleted_from_and_deleted_to_filter_on_the_delete_time_not_the_create_time(self):
        """D6-②：`deleted_from`/`deleted_to` 是**删除时间**，不是录单时间。"""
        self._delete("N")
        body = self.client.get(
            "/api/business/orders/recycle-bin/?deleted_from=1999-01-01T00:00:00%2B08:00"
            "&deleted_to=1999-01-02T00:00:00%2B08:00"
        ).json()
        self.assertEqual(body["data"]["total"], 0)

    def test_page_size_accepts_the_three_benchmark_values_and_rejects_others(self):
        self._delete("N")
        for size in (10, 20, 30):
            with self.subTest(page_size=size):
                body = self.client.get(
                    f"/api/business/orders/recycle-bin/?page=1&page_size={size}"
                ).json()
                self.assertEqual(body["code"], 0)
        body = self.client.get("/api/business/orders/recycle-bin/?page_size=7").json()
        self.assertEqual(body["code"], 1000)

    def test_without_order_recycle_the_bin_is_403_and_restore_is_403(self):
        # ⚠️ 组名必须与主夹具的 `回收组` **不同**：节点是挂在 Role 上的，同组会直接继承
        # 主夹具那三个节点 ⇒ 这条「无权限」用例会被静默变成恒真。
        anon = make_logged_in_client(
            username="测试无权限甲", groups=("甲组",), nodes=("order.delete",)
        )
        self.assertEqual(anon.get("/api/business/orders/recycle-bin/").status_code, 403)
        self.assertEqual(
            anon.post(f"/api/business/orders/{ALIVE_NO}/restore/").status_code, 403
        )

    def test_without_order_delete_the_delete_endpoint_is_403(self):
        no_perm = make_logged_in_client(
            username="测试无权限乙", groups=("乙组",), nodes=("order.list",)
        )
        resp = no_perm.post(
            f"/api/business/orders/{ALIVE_NO}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_restore_is_post_only(self):
        resp = self.client.delete(f"/api/business/orders/{ALIVE_NO}/restore/")
        self.assertEqual(resp.status_code, 405)


#: 跨租户用例的租户 B（域名刻意与 A 不同：`Domain` 上有唯一约束）。
DOMAIN_B = "odl-recycle-b.test.local"
B_SCHEMA = "odl_recycle_b"
#: 两个租户各自的单号（**指认用单号、不用主键**：两 schema 的主键序列各自从 1 起）。
CROSS_A_NO = "20260924000000000301"
CROSS_B_NO = "20260924000000000302"


class RecycleCrossTenantTests(TenantTestCase):
    """§8-T1 第 5 条：B 租户的单号在 A 里软删 / 恢复都拿不到，且 B 的行不被改动。

    ⚠️ 指认**用订单号**（`AGENTS.md` §5-G）：两个 schema 的主键序列各自从 1 起，
    `filter(id=…)` 在别的租户里会命中**它自己**的行 ⇒ 否定断言恒假或恒真。
    ⚠️ 跨域名请求**不会**在响应结束时还原连接上的 schema（`test_account_isolation.py`
    的模块说明，实测）⇒ 每条 B 侧操作都经 `_b_call()` 用 `try/finally` 钉回租户 A。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=B_SCHEMA, name="回收站隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        game_a = Game.objects.create(name="仅 A 的游戏")
        Order.objects.create(
            game=game_a, order_no=CROSS_A_NO, total_price=Decimal("100.00")
        )
        self.client_a = make_logged_in_client(
            username="测试回收甲",
            groups=("回收甲",),
            nodes=("order.delete", "order.recycle"),
        )
        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="仅 B 的游戏")
                Order.objects.create(
                    game=game_b, order_no=CROSS_B_NO, total_price=Decimal("200.00")
                )
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B,
                    username="测试回收乙",
                    groups=("回收乙",),
                    nodes=("order.delete", "order.recycle"),
                )
        finally:
            connection.set_tenant(self.tenant)

        # 夹具自证（不成立时下面的负断言无意义）：两个单号各只在一个 schema 里。
        self.assertTrue(Order.objects.filter(order_no=CROSS_A_NO).exists())
        self.assertFalse(Order.objects.filter(order_no=CROSS_B_NO).exists())
        self.assertTrue(self._b_call(
            lambda: Order.objects.filter(order_no=CROSS_B_NO).exists()
        ))
        self.assertFalse(self._b_call(
            lambda: Order.objects.filter(order_no=CROSS_A_NO).exists()
        ))

    def _b_call(self, function):
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def test_a_cannot_delete_or_restore_b_order_by_order_no(self):
        deleted = self.client_a.post(
            f"/api/business/orders/{CROSS_B_NO}/delete/",
            {"isDelEmp": "Y"},
            content_type="application/json",
        ).json()
        self.assertNotEqual(deleted["code"], 0, deleted)
        restored = self.client_a.post(
            f"/api/business/orders/{CROSS_B_NO}/restore/"
        ).json()
        self.assertNotEqual(restored["code"], 0, restored)
        # 断言打在**数据库行**上：B 的两列一个都没被碰。
        b_row = self._b_call(lambda: Order.all_objects.get(order_no=CROSS_B_NO))
        self.assertIsNone(b_row.deleted_at)
        self.assertEqual(b_row.deleted_by, "")

        # 正向对照：同一会话对**自己的**单做同一个动作必须真的生效——
        # 否则上面的「没改 B」可能由「这个端点对谁都不生效」凑出来。
        own = self.client_a.post(
            f"/api/business/orders/{CROSS_A_NO}/delete/",
            {"isDelEmp": "Y"},
            content_type="application/json",
        ).json()
        self.assertEqual(own["code"], 0, own)
        self.assertIsNotNone(
            Order.all_objects.get(order_no=CROSS_A_NO).deleted_at
        )
        # B 的会话对自己的单也必须能删（证明 B 侧夹具与路由都是活的）。
        b_own = self._b_call(lambda: self.client_b.post(
            f"/api/business/orders/{CROSS_B_NO}/delete/",
            {"isDelEmp": "N"},
            content_type="application/json",
        ).json())
        self.assertEqual(b_own["code"], 0, b_own)
        self.assertIsNotNone(
            self._b_call(
                lambda: Order.all_objects.get(order_no=CROSS_B_NO).deleted_at
            )
        )


class HiddenOrderRecycleBinTests(_RecycleFixture):
    """⭐ 评审 Important 6：回收站也**不得**看到隐藏单。

    `search_recycle_bin` 走 `Order.all_objects`（已软删的行）⇒ 默认 manager 的隐藏单排除
    在那里**不成立**，必须显式 `exclude(hall_card__isnull=False)`。否则一张被软删的隐藏单
    会以「一笔真退单」出现在回收站里（它没有真实客户 / 退款额）。
    """

    def test_a_deleted_hidden_order_is_not_in_the_recycle_bin(self):
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        card = make_mirror(game=self.game, bound_by=self.taker, no="SYN-SHADOW-BIN")
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261026000000000301"
        )
        Order.all_objects.filter(pk=hidden.pk).update(
            deleted_at=timezone.now(), deleted_by="syn-shadow"
        )
        self._delete("N")                                  # 一张**正常**单进回收站（正对照）
        body = self.client.get("/api/business/orders/recycle-bin/").json()
        self.assertEqual(body["code"], 0, body)
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertIn(ALIVE_NO, nos)                       # 先证明有数据
        self.assertNotIn(hidden.order_no, nos)
        self.assertEqual(body["data"]["total"], 1)
