"""上下游 P2 Task 5（业务裁定 A14 / A15）：游戏名对不上就**自动建同名游戏**并授权给绑定操作人。

spec §16.4 的裁决推翻了 P1/R21 时代的旧行为（名字解析不到 = 硬跳过 `bad_shape`）：
**名字对不上不能漏单** ⇒ 建一款同名的（或复活同名但已软删的那一款），把单录进去，
并把这款游戏授权给**建这条绑定的那个客服**（`PeerUpstreamBinding.bound_by`）。

判据分六层：

① **建**：名字解析不到 ⇒ 建 `Game`（`is_active=True`，⛔ 不带任何「同步来源」标记列）；
② **复活**：同名但 `deleted_at` 非空的游戏 ⇒ **复活它**（`deleted_at=None`），
   ⛔ 不许再建一行——`Game.name` 的 `uniq_game_name` **把软删行也算在内**，再建会撞唯一约束
   响亮 500（不是静默）；
③ **授权**：`bound_by.staff_profile` 存在 ⇒ `profile.games.add(game)`；
   没有档案（管理员按构造就没有）⇒ **跳过授权、⛔ 不建档案**（`apps/business/game_scope.py::games_of`
   对管理员已短路成全量）；
④ **真的用上**：`None` 只在名字缺失 / 空 / 非字符串时返回（建不出无名游戏 ⇒ 调用方仍按
   `bad_shape` 硬跳过）；其余情况解出来的那一行**必须**落在订单行上（`order.game`）；
⑤ **超长名字按列宽截断**（出题方裁定 1，依 R19 先例）：⛔ 不 500、⛔ 不跳过，代价是
   可能与对端不同名（可见）；
⑥ **建游戏与订单写入同生共死**（出题方裁定 2，E-0022 同族）：订单写入失败 ⇒ 游戏行回滚。

⚠️ 必须用 `TenantTestCase`：`apps.business` / `apps.accounts` 只在 `TENANT_APPS` 里
   （普通 `TestCase` 的 search_path 里没有 `business_*` / `accounts_*` 表）。
⚠️ 夹具一律**合成值**（`测试客服甲` / `peer-account-a` / `https://peer-a.test.invalid`）。
"""
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.api.peer_views import receive_events
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
)
from apps.business.peer_inbound import resolve_game
from apps.business.peer_sync import hash_token, new_token

#: 对端带来、本租户**没有**的游戏名（合成值）。
PEER_GAME_NAME = "对端新游戏甲"


class GameResolveTests(TenantTestCase):
    """`resolve_game` 的四处判据（A14/A15，spec §16.4）。"""

    def setUp(self):
        self.kefu = get_user_model().objects.create_user(
            username="peer-autocreate-kefu", password="pw-autocreate"
        )
        # ⚠️ 有档案这一支是**默认夹具**：A15 的授权落在 `staff_profile.games` 上。
        #    「没有档案」那一支由 `test_a_bound_by_without_a_profile_is_not_created` 单独构造。
        self.profile = StaffProfile.objects.create(user=self.kefu)
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _event(self, order_no="A-1", **payload_over):
        """造一条 `order.assign` 事件：只塞 `apply_events` 会消费的那些键。"""
        payload = {
            "status": OrderStatus.IN_PROGRESS,
            "total_price": "88.00",
            "remark": "上家备注",
            "content": "客户原始需求",
            "game_name": PEER_GAME_NAME,
        }
        payload.update(payload_over)
        return {
            "idempotency_key": uuid.uuid4().hex,
            "kind": "order.assign",
            "target_ref": order_no,
            "payload": payload,
        }

    # ── ① 未知名 ⇒ 建游戏 + 授权（A14/A15）─────────────────────────────────
    def test_an_unknown_game_name_creates_the_game_and_grants_it(self):
        """⭐ A14 + A15 的正例：建了同名游戏（启用），并授权给建绑定的那个客服。"""
        game = resolve_game(PEER_GAME_NAME, binding=self.binding)

        self.assertIsNotNone(game)
        self.assertEqual(game.name, PEER_GAME_NAME)
        self.assertTrue(game.is_active)
        self.assertIsNone(game.deleted_at)
        self.assertEqual(Game.objects.filter(name=PEER_GAME_NAME).count(), 1)
        # ⚠️ 授权断言打在**数据库出口**的重新查询上（⛔ 不读可能带缓存的 `profile.games`）。
        self.assertTrue(
            StaffProfile.objects.get(user=self.kefu).games.filter(pk=game.pk).exists()
        )

    def test_an_existing_active_game_is_reused_and_granted(self):
        """阴性对照：同名**在用**的游戏不新建第二行，但照样授权（⛔ 不是"只给新建的授权"）。"""
        existing = Game.objects.create(name=PEER_GAME_NAME)

        game = resolve_game(PEER_GAME_NAME, binding=self.binding)

        self.assertEqual(game.pk, existing.pk)
        self.assertEqual(Game.objects.filter(name=PEER_GAME_NAME).count(), 1)
        self.assertTrue(
            StaffProfile.objects.get(user=self.kefu).games.filter(pk=existing.pk).exists()
        )

    # ── ② 软删同名 ⇒ 复活不新建（`uniq_game_name` 算软删行）───────────────
    def test_a_soft_deleted_game_is_revived_not_duplicated(self):
        """`uniq_game_name` 把软删行也算在内 ⇒ 再建会 500；正确做法是**复活**。"""
        soft_deleted = Game.objects.create(
            name=PEER_GAME_NAME, deleted_at=timezone.now()
        )

        resolved = resolve_game(PEER_GAME_NAME, binding=self.binding)

        self.assertEqual(resolved.pk, soft_deleted.pk)
        self.assertIsNone(resolved.deleted_at)
        self.assertEqual(Game.objects.filter(name=PEER_GAME_NAME).count(), 1)
        # 复活那一支同样要授权（A15 管的是"解析出来的那一款"，与新建 / 复活无关）。
        self.assertTrue(
            StaffProfile.objects.get(user=self.kefu)
            .games.filter(pk=soft_deleted.pk)
            .exists()
        )

    # ── ③ 授权：没有档案不建档案、也不炸 ──────────────────────────────────
    def test_a_bound_by_without_a_profile_is_not_created(self):
        """管理员按构造没有档案 ⇒ 跳过授权，⛔ **不建档案**（`games_of` 对他已短路成全量）。

        判别力：把 `getattr(...)` 那一支写成 `StaffProfile.objects.get_or_create(...)`，
        下面的 `assertFalse(...exists())` 立刻红；写成 `binding.bound_by.staff_profile.games`
        则反向 OneToOne 抛 `RelatedObjectDoesNotExist` ⇒ 用例直接 ERROR。
        """
        admin = get_user_model().objects.create_user(
            username="peer-autocreate-admin", password="pw-admin"
        )
        binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室乙",
            peer_base_url="https://peer-c.test.invalid",
            peer_account="peer-account-c",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=admin,
        )

        game = resolve_game(PEER_GAME_NAME, binding=binding)

        # 游戏照建（不能因为操作人没档案就漏单），但**档案一行都不许凭空出现**。
        self.assertIsNotNone(game)
        self.assertEqual(game.name, PEER_GAME_NAME)
        self.assertIsNone(getattr(admin, "staff_profile", None))
        self.assertFalse(StaffProfile.objects.filter(user=admin).exists())

    # ── ④ `None` 只给"建不出名字"的那几类 ────────────────────────────────
    def test_a_missing_or_empty_name_is_none(self):
        """缺失（`None`）/ 空串 ⇒ `None`（调用方按 `bad_shape` 硬跳过：建不出无名游戏）。"""
        self.assertIsNone(resolve_game(None, binding=self.binding))
        self.assertIsNone(resolve_game("", binding=self.binding))
        self.assertFalse(Game.objects.exists())

    def test_a_non_string_name_is_none_not_a_500(self):
        """畸形名字（对端发了个对象 / 数字）⇒ `None`，⛔ 不是 500（E-0010：先归一形状）。"""
        self.assertIsNone(resolve_game({"nope": 1}, binding=self.binding))
        self.assertIsNone(resolve_game(7, binding=self.binding))
        self.assertFalse(Game.objects.exists())

    # ── ⑤ 端到端：解析出来的那一行**真的**落在订单行上 ───────────────────
    def test_the_resolved_game_is_the_one_pinned_on_the_order_row(self):
        """⭐ 端到端：未知名 ⇒ 落单成功，且 `order.game` **就是**新建的那一行（同 pk）。

        判别力：只建游戏但不把它写进订单行（例如解析完仍用 `payload["game_id"]` 或干脆不传
        `game`），`order.game_id == game.pk` 与 `order.game.name` 两条立刻红。
        """
        result = receive_events(self.binding, [self._event()])

        self.assertEqual(result["applied"], 1, result)
        self.assertEqual(result["skipped"], 0, result)
        self.assertEqual(result["skipped_reasons"], [])
        game = Game.objects.get(name=PEER_GAME_NAME)
        order = Order.objects.get()
        self.assertEqual(order.game_id, game.pk)
        self.assertEqual(order.game.name, PEER_GAME_NAME)
        self.assertEqual(order.source_order_no, "A-1")
        self.assertTrue(PeerOrderLink.objects.filter(order=order).exists())
        # A15 的授权与落单是同一次调用里发生的（不是只有单测直接调 `resolve_game` 才授）。
        self.assertTrue(
            StaffProfile.objects.get(user=self.kefu).games.filter(pk=game.pk).exists()
        )

    # ── ⑥ 裁定 1：超长名字按列宽截断（R19 先例 + A14）────────────────────
    def test_an_overlong_game_name_is_truncated_to_the_column_width(self):
        """⭐ 超长名字按 `Game.name` 的**列宽截断**：⛔ 不是 500、⛔ 不是跳过，单要落下来。

        依据（出题方裁定 1）：本仓 `project_text` 的 **R19** 先例——不让一个超长的对端值把
        整批落单一起回滚——加上 A14「不能漏单」。
        ⚠️ **代价是可见的**：截断后的名字可能与对端**不同名**（将来要精确同名得先单独裁定
        `Game.name` 列宽），所以这里断言的是**截断后**的那个名字，⛔ 不是原样。
        判别力：把截断去掉（原样 `create`）⇒ 本用例以
        `value too long for type character varying(64)` ERROR——正是它要防的那个 500。
        """
        long_name = "超长游戏名" * 20        # 100 字，远超列宽
        width = Game._meta.get_field("name").max_length
        self.assertGreater(len(long_name), width)      # 夹具自证：真的超了

        result = receive_events(self.binding, [self._event(game_name=long_name)])

        self.assertEqual(result["applied"], 1, result)
        self.assertEqual(result["skipped"], 0, result)
        game = Game.objects.get()
        self.assertEqual(game.name, long_name[:width])       # 列宽从模型元数据取，⛔ 不抄数字
        self.assertEqual(len(game.name), width)
        self.assertFalse(Game.objects.filter(name=long_name).exists())
        self.assertEqual(Order.objects.get().game_id, game.pk)

    # ── ⑦ 裁定 2：建游戏与订单写入同生共死（E-0022 同族）─────────────────
    def test_a_failed_order_write_rolls_the_auto_created_game_back(self):
        """⭐ 反向用例：`resolve_game` 是**写** ⇒ 必须与订单写入在**同一个** `atomic()` 里。

        判别力：把 `resolve_game` 挪回 `transaction.atomic()` **之外**，下面那条
        "没有新增 `Game` 行"立刻红——游戏行留下了、订单却没落成（正是裁定 2 要修的形态）。
        ⚠️ 异常本身**照旧冒出去**（未预期故障 ⇒ 端点翻 500，⛔ 不吞、⛔ 不当业务结果）。

        ⚠️ P4 挂账⑨ 起**不能**再用"超精度金额"制造写失败：那类对端值已被接收侧的形状闸挡在
        **任何写之前**（`bad_shape`，⛔ 不再抛异常）。改成让订单号生成器返回一个**已存在**的单号，
        逼一次真实的 `order_no` 唯一约束冲突——游戏行**真的写下去了**、随后与失败的订单写入
        一起回滚（`DataError` 换成 `IntegrityError`，证明力不变）。
        """
        taken = "SYN-P4-GATE-DUP"
        Order.objects.create(
            order_no=taken, game=Game.objects.create(name="已存在的游戏")
        )

        with mock.patch(
            "apps.business.order_no.generate_order_no", return_value=taken
        ):
            with self.assertRaises(IntegrityError) as raised:
                receive_events(self.binding, [self._event()])

        # ⚠️ `assertRaises(IntegrityError)` 只钉异常类型、**钉不到是哪条约束**：别的原因
        #    （外键 / NOT NULL / 另一个唯一列）也能让上面那条绿，于是「真有一笔 `order_no` 唯一冲突」
        #    这个前提只是**推断**。这里显式判出来（取**三层里最稳的一层**）：
        #    ① Django 把驱动异常包成 `IntegrityError` 时用 `raise … from` 保留了 `__cause__`
        #       （psycopg 的 `UniqueViolation`）⇒ 先断言它还在；
        #    ② SQLSTATE `23505`（unique_violation）**与约束名 / 数据库命名无关**，比匹配文案稳
        #       （E-0024：钉值不钉会漂的字符串）。⚠️ psycopg 3 的属性名是 `sqlstate`，
        #       psycopg 2 才是 `pgcode` ⇒ 两个都读，钉的仍是同一个值；
        #    ③ 再核约束名或消息里出现 `order_no`，把「是这一列」这一半也钉住。
        cause = raised.exception.__cause__
        self.assertIsNotNone(cause, "IntegrityError 没有保留底层驱动异常（__cause__ 为空）")
        sqlstate = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
        self.assertEqual(sqlstate, "23505")
        constraint = getattr(getattr(cause, "diag", None), "constraint_name", None) or ""
        self.assertIn("order_no", f"{constraint} {raised.exception}")

        # 两笔写同生共死：自动建的游戏行没留下、那笔同步单也没落成（只剩夹具那一张）。
        self.assertFalse(Game.objects.filter(name=PEER_GAME_NAME).exists())
        self.assertEqual(Order.objects.count(), 1)
        self.assertTrue(Order.objects.filter(order_no=taken).exists())
