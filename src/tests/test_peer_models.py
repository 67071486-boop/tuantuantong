"""四张 Peer* 表的约束（spec §6）。

⚠️ 主键跨租户会撞，所以这里**不**用主键指认跨租户的行（`AGENTS.md` §5-G）。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里 ⇒ 普通 `TestCase`
的 search_path 里没有 `business_*` 表（照 `tests/test_jidanpu_models.py` 的先例）。
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.models import UniqueConstraint
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    Game,
    Order,
    PeerDownstreamBinding,
    PeerOrderLink,
    PeerSyncEvent,
    PeerUpstreamBinding,
)

#: ⚠️ 令牌用**合成字面量**，⛔ **不要** import `apps.business.peer_sync`——那个模块 其行先建。
#: 模型对令牌**格式不加校验**（只看长度），所以字面量足够；真哈希由既有用例覆盖。
PEER_TOKEN_IN_HASH_A = "hash-of-peer-token-a"
PEER_TOKEN_OUT_A = "peer-token-out-b"


def _upstream(**over):
    data = {
        "peer_name": "测试工作室甲",
        "peer_base_url": "https://peer-a.test.invalid",
        "peer_account": "peer-account-a",
        "local_base_url": "https://local-a.test.invalid",
        # `bound_by` 是**非空** `PROTECT` 外键（spec §6.1）⇒ 夹具必须给一个操作人。
        "bound_by": get_user_model().objects.create_user(
            username="peer-binder-" + uuid.uuid4().hex[:8], password="pw-binder-a"
        ),
        # `token_in_hash` 是**验证侧**那一列（裁决 R15：上家推单时带的 `token_a`）
        # ⇒ 我方只验证、不发送 ⇒ 入库就是哈希。夹具按形状给一个合成哈希串。
        "token_in_hash": PEER_TOKEN_IN_HASH_A,
        # `token_out` 是本侧**外发**那列（握手时我生成的 `token_b`）⇒ 明文（spec §9-S3）。
        "token_out": PEER_TOKEN_OUT_A,
    }
    data.update(over)
    return PeerUpstreamBinding.objects.create(**data)


def _downstream(**over):
    data = {
        # `account` 是 `OneToOne`（spec A9）⇒ 每行要一个自己的用户，否则撞唯一约束。
        "account": get_user_model().objects.create_user(
            username="peer-downstream-" + uuid.uuid4().hex[:8],
            password="pw-downstream-b",
        ),
        "peer_base_url": "https://peer-b.test.invalid",
        "token_in_hash": "hash-of-downstream-token-b",
        "token_out": "downstream-token-out-b",
    }
    data.update(over)
    return PeerDownstreamBinding.objects.create(**data)


class PeerOrderLinkConstraintTests(TenantTestCase):
    def setUp(self):
        # ⚠️ 两条订单必须**真的存在**：否则「没有绑定」那一支会因为外键先炸而红，
        # 判据就落不到 `CheckConstraint` 上（测试会因为**别的**原因通过）。
        game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(game=game, order_no="20260925000000000901")
        self.other_order = Order.objects.create(game=game, order_no="20260925000000000902")

    def test_a_link_must_point_at_exactly_one_binding(self):
        """`upstream_binding` / `downstream_binding` 恰有其一非空（spec §6.3）。"""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PeerOrderLink.objects.create(
                    order=self.order, peer_order_no="X1",
                    upstream_binding=None, downstream_binding=None,
                )

    def test_previous_order_link_model_can_insert_after_the_direct_assignment_migration(self):
        old_apps = MigrationLoader(connection).project_state(("business", "0150_backfill_penalty_approval_logs")).apps
        old_link = old_apps.get_model("business", "PeerOrderLink")
        binding = _upstream()

        for order, reference in ((self.order, "LEGACY-A"), (self.other_order, "LEGACY-B")):
            old_link.objects.create(order_id=order.pk, upstream_binding_id=binding.pk, peer_order_no=reference)

        self.assertEqual(
            list(PeerOrderLink.objects.filter(upstream_binding=binding).values_list("upstream_internal_order_no", flat=True)),
            ["", ""],
        )

    def test_same_peer_order_cannot_link_twice_within_one_binding(self):
        """同一绑定下同一个对端单号只能有一行（去重的地基，spec §6.3）。

        ⚠️ 评审 C4：旧版把**两笔插入**一起包在 `assertRaises` 里 ⇒ 第一笔若因别的原因抛，
        本用例照样"通过"。这里把第一笔挪出来并**断言它真的落库**，再单独钉第二笔。
        """
        binding = _upstream()
        with transaction.atomic():
            PeerOrderLink.objects.create(
                order=self.order, upstream_binding=binding, peer_order_no="DUP"
            )
        self.assertEqual(PeerOrderLink.objects.count(), 1)   # 第一笔真的成功了
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PeerOrderLink.objects.create(
                    order=self.other_order, upstream_binding=binding, peer_order_no="DUP"
                )

    def test_one_order_can_link_both_an_upstream_and_a_downstream(self):
        """A→B→C：B 的一条单可以同时链上家与下家（spec §14-R4）。

        ⚠️ 这两行能共存，靠的是 `order` 是 **FK 不是 OneToOne**；每行只有一半非空
        （另一侧的绑定列是 `NULL`）。⇒ 它钉的是「**同一条单可以有两个绑定**」。
        """
        upstream = _upstream()
        downstream = _downstream()
        PeerOrderLink.objects.create(
            order=self.order, upstream_binding=upstream, peer_order_no="A"
        )
        PeerOrderLink.objects.create(
            order=self.order, downstream_binding=downstream, peer_order_no="B"
        )
        self.assertEqual(PeerOrderLink.objects.filter(order=self.order).count(), 2)

    def test_the_null_side_does_not_collide_on_all_four_unique_constraints(self):
        """直接钉 spec §6.3 末段「有意依赖 PostgreSQL 的 `NULL` 差异性」——**四条约束各一对**。

        ⚠️ 评审 C4：旧版这一条的 docstring 声称「把任一条改成 `nulls_distinct=False` 即红」，
        **那是假的**——旧版两行的 `peer_order_no` 不同、非空列又都落在 `downstream_binding` 上
        ⇒ 四条里只有 `uniq_peer_link_up_order` 真的承重，其余三条**永远**不会触发。
        这里按四条约束各造一对行，让每一条都真的承重：

        - `uniq_peer_link_up_order`：两行 `upstream_binding=NULL`、**同一个 order**（A 组）；
        - `uniq_peer_link_up_peer`：两行 `upstream_binding=NULL`、**同一个对端单号**、order 不同（B 组）；
        - `uniq_peer_link_down_order`：两行 `downstream_binding=NULL`、**同一个 order**（C 组）；
        - `uniq_peer_link_down_peer`：两行 `downstream_binding=NULL`、**同一个对端单号**、order 不同（D 组）。

        任一条改成 `nulls_distinct=False` ⇒ 对应那一对行相撞、本用例在插入处
        `IntegrityError` 红。
        """
        up = [_upstream() for _ in range(4)]
        down = [
            _downstream(peer_base_url=f"https://peer-b{i}.test.invalid")
            for i in range(1, 5)
        ]
        rows = [
            # A：钉 `uniq_peer_link_up_order`（同一 order、upstream 全 NULL）
            dict(order=self.order, downstream_binding=down[0], peer_order_no="A1"),
            dict(order=self.order, downstream_binding=down[1], peer_order_no="A2"),
            # B：钉 `uniq_peer_link_up_peer`（同一对端单号、upstream 全 NULL、order 不同）
            dict(order=self.order, downstream_binding=down[2], peer_order_no="UP-X"),
            dict(order=self.other_order, downstream_binding=down[3], peer_order_no="UP-X"),
            # C：钉 `uniq_peer_link_down_order`（同一 order、downstream 全 NULL）
            dict(order=self.other_order, upstream_binding=up[0], peer_order_no="C1"),
            dict(order=self.other_order, upstream_binding=up[1], peer_order_no="C2"),
            # D：钉 `uniq_peer_link_down_peer`（同一对端单号、downstream 全 NULL、order 不同）
            dict(order=self.order, upstream_binding=up[2], peer_order_no="DOWN-Y"),
            dict(order=self.other_order, upstream_binding=up[3], peer_order_no="DOWN-Y"),
        ]
        for row in rows:
            PeerOrderLink.objects.create(**row)
        self.assertEqual(PeerOrderLink.objects.count(), len(rows))

    def test_the_five_unique_constraints_exist_with_their_exact_fields(self):
        """五条唯一约束的存在性与列组成。

        ⚠️ 期望值是**字面表**，⛔ 不从模型派生（派生的话删掉一条约束本用例照样绿，E-0024）。
        """
        actual = {
            c.name: list(c.fields)
            for c in PeerOrderLink._meta.constraints
            if isinstance(c, UniqueConstraint)
        }
        self.assertEqual(actual, {
            "uniq_peer_link_up_order": ["upstream_binding", "order"],
            "uniq_peer_link_up_peer": ["upstream_binding", "peer_order_no"],
            "uniq_peer_link_up_internal_no": ["upstream_binding", "upstream_internal_order_no"],
            "uniq_peer_link_down_order": ["downstream_binding", "order"],
            "uniq_peer_link_down_peer": ["downstream_binding", "peer_order_no"],
        })


class PeerUpstreamBindingDefaultTests(TenantTestCase):
    def test_enabled_defaults_to_true(self):
        """P1 **没有开关控件、绑定即生效** ⇒ 模型默认必须是 `True`（裁决 R8）。

        ⚠️ 夹具**不传** `enabled`：钉的是**默认值本身**；不重读数据库就看不出 DB 列默认被改。
        """
        binding = _upstream()
        self.assertIs(PeerUpstreamBinding.objects.get(pk=binding.pk).enabled, True)


class PeerDownstreamBindingAccountTests(TenantTestCase):
    """spec D10 / A9：一个上家账号**只能被一个下家绑**（业务方 2026-09-25 裁定）。

    ⚠️ 评审的 deferred-minor 分诊把这条列为**合并前必修**：`account` 是 `OneToOne`，
    但此前**没有一条用例**钉它 ⇒ 有人把它改成普通 FK 时全绿，而"一个账号被两个下家绑"
    会让两边都以为自己在收单（实际上家只认令牌最新那一套）。
    """

    def test_the_field_is_a_one_to_one(self):
        """结构判据：`account` 是 `OneToOneField`（业务裁定的落点）。"""
        self.assertIs(PeerDownstreamBinding._meta.get_field("account").one_to_one, True)

    def test_a_second_binding_on_the_same_account_is_rejected_by_the_database(self):
        """行为判据（**这才是"拦得住"的证据**）：第二次插入必须 `IntegrityError`。

        ⚠️ 只钉结构（`one_to_one is True`）证明不了数据库真的拦：约束若没进迁移/库，
        结构断言照样绿。判据打在**数据库**上。
        """
        account = get_user_model().objects.create_user(
            username="peer-onetoone-account", password="pw-onetoone"
        )
        _downstream(account=account, peer_base_url="https://peer-b1.test.invalid")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _downstream(account=account, peer_base_url="https://peer-b2.test.invalid")
        self.assertEqual(PeerDownstreamBinding.objects.filter(account=account).count(), 1)


class PeerSyncEventTests(TenantTestCase):
    def test_idempotency_key_is_not_unique_on_its_own(self):
        """本表**没有任何幂等唯一约束**；判重在**接收侧**。

        ⚠️ 这是**有意**的设计：键由对端生成，两个对端可能生成同一个键 ⇒
        `idempotency_key` 上只有一条**非唯一**索引（`peer_event_idem_idx`）。
        去重由接收侧自己按 `peer_order_no` / `idempotency_key` 做（Task 7）。
        ⛔ **不要**照着「唯一约束」去 `catch IntegrityError` —— 那个约束**不存在**（spec §6.4）。
        """
        PeerSyncEvent.objects.create(
            binding_role="upstream", binding_id=1, direction="out",
            kind="order.assign", target_type="order", target_ref="A1",
            idempotency_key="same-key",
        )
        PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=1, direction="in",
            kind="order.assign", target_type="order", target_ref="A2",
            idempotency_key="same-key",
        )
        self.assertEqual(PeerSyncEvent.objects.count(), 2)


class PeerP2ColumnDefaultsTests(TenantTestCase):
    """P2 新增三列的默认值与取活索引（spec §16.2 / §16.3 / §16.6）。

    ⚠️ 三列都是给 P2 **之后**的写路径用的；既有代码与夹具**都不传它们** ⇒
    判据钉的是「什么都不给也能建行，且默认值就是约定的那个」。
    """

    def setUp(self):
        game = Game.objects.create(name="测试游戏甲")
        self.order = Order.objects.create(game=game, order_no="20260926000000000901")
        # `PeerOrderLink` 要求上/下家绑定恰有其一（spec §6.3）；本用例走上家那一支。
        self.binding = _upstream()

    def test_p2_columns_have_safe_defaults(self):
        """三个新列都必须能"什么都不给"地建行（既有代码与夹具不传它们）。"""
        event = PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id="1", direction="out",
            kind="order.assign", target_type="order", target_ref="A-1",
            payload={}, idempotency_key="k1",
        )
        self.assertEqual(event.meta, {})
        self.assertIsNone(event.next_attempt_at)
        link = PeerOrderLink.objects.create(
            upstream_binding=self.binding, peer_order_no="A-1", order=self.order,
        )
        self.assertIsNone(link.last_applied_at)

    def test_the_three_event_indexes_exist_with_their_exact_columns(self):
        """事件索引的**名字 → 列**映射（P2 取活索引 + C10 证据索引；方法名沿旧名）。"""
        self.assertEqual(
            {index.name: list(index.fields) for index in PeerSyncEvent._meta.indexes},
            {
                "peer_event_status_idx": ["status", "created_at"],
                "peer_event_idem_idx": [
                    "binding_role", "binding_id", "idempotency_key",
                ],
                "peer_event_due_idx": ["status", "next_attempt_at"],
                # ⭐ C10（M-11）：抢单判据两个 `exists()`（`hall.throw`
                #    推送证据 / `hall.pull` 拉取证据）的打点索引——台账只增不减，
                #    没这把索引就是每轮全表扫。
                "peer_event_evidence_idx": [
                    "binding_role", "binding_id", "kind", "target_ref",
                ],
            },
        )

    def test_the_hall_pull_evidence_partial_unique_constraint_is_exact(self):
        """Z20 拉取证据的**部分唯一索引**：名字 → 列 + 条件都钉字面值。

        ⚠️ 计划 `2026-10-25-peer-hall-pull` §2：它与上面那些 `Meta.indexes` 是**两张表**——
        上面的用例仍是「恰好集合」（现在含 C10 的证据索引），
        ⛔ 不要把这条约束塞进 `indexes`（那会让那张网红）。
        ⚠️ 条件必须**只覆盖 `kind='hall.pull'`**：`hall.throw` 等台账行没有这个唯一性需求。
        """
        from django.db.models import Q, UniqueConstraint

        constraints = [
            c for c in PeerSyncEvent._meta.constraints
            if isinstance(c, UniqueConstraint)
        ]
        self.assertEqual(len(constraints), 1, constraints)
        constraint = constraints[0]
        self.assertEqual(constraint.name, "uniq_peer_event_hall_pull")
        self.assertEqual(
            list(constraint.fields),
            ["binding_role", "binding_id", "kind", "target_ref"],
        )
        self.assertEqual(constraint.condition, Q(kind="hall.pull"))




class PeerPasswordPlaintextPurgeTests(TenantTestCase):
    """⭐ C1（M-1，2026-10-25 审计批）：迁移 0095 的**数据体**——明文清空、布尔回填。

    ⚠️ 判的是迁移函数本身（用真 ORM 替身喂给它）：造出「有明文 / 没明文」两态的行 →
    跑 → 断言。**幂等可重放**（重放一次，结果不变、不报错）是计划验收明文。
    ⛔ 本类**不**证明「迁移在真库里被应用了」（那是 `migrate` 的职责）——它钉的是
    「函数做什么」；「模型有这一列 / 序列化器读这一列」由上面的索引网与绑定 API 网守。
    """

    #: 迁移模块名以数字开头 ⇒ 只能按字符串 import（⛔ 不写成 `from … import 0095_…`）。
    _MIGRATION_MODULE = "apps.business.migrations.0095_peerupstreambinding_password_set"

    @classmethod
    def _migration_module(cls):
        import importlib

        return importlib.import_module(cls._MIGRATION_MODULE)

    @classmethod
    def _run_migration_body(cls):
        class _Apps:
            """`RunPython` 收到的 `apps` 的最小替身：按真名取**真**模型。"""

            def get_model(self, app_label, model_name):
                assert (app_label, model_name) == ("business", "PeerUpstreamBinding")
                return PeerUpstreamBinding

        cls._migration_module().fill_password_set_and_clear_plaintext(_Apps(), None)

    def _make(self, *, password, password_set=False, account="peer-account-a"):
        row = _upstream(peer_account=account, password=password, password_set=password_set)
        # 把行摆回「迁移前」的形状：显式 update 绕开 create 的默认值路径。
        PeerUpstreamBinding.objects.filter(pk=row.pk).update(
            password=password, password_set=password_set
        )
        return row

    def test_plaintext_is_cleared_and_the_flag_is_backfilled(self):
        with_plaintext = self._make(password="legacy-plaintext-a")
        without = self._make(password="", account="peer-account-b")
        self._run_migration_body()
        with_plaintext.refresh_from_db()
        without.refresh_from_db()
        self.assertEqual(with_plaintext.password, "")
        self.assertTrue(with_plaintext.password_set)
        # 没明文的行一个字节都不动（布尔保持它自己的值）。
        self.assertEqual(without.password, "")
        self.assertFalse(without.password_set)

    def test_replaying_the_body_is_a_noop(self):
        row = self._make(password="legacy-plaintext-a")
        self._run_migration_body()
        self._run_migration_body()          # ⛔ 不许报错，也不许改出第二个状态
        row.refresh_from_db()
        self.assertEqual(row.password, "")
        self.assertTrue(row.password_set)

    def test_the_reverse_is_an_honest_noop(self):
        """反向 = **诚实的 no-op**：可回放，但**不恢复明文**（2026-10-25 全量门禁改准）。

        ⚠️ 首版「`reverse_code=None` 响亮报错」与四个迁移回放网相撞（它们要倒回
        全图 ⇒ 必须可回放，见迁移 0095 模块 docstring）。裁定的口径：
        反向 = `RunPython.noop`——unapply 只是不再动数据，**不**把明文变回来
        （写得出恢复逻辑就等于没删干净）。判据读 Django 自己的机制：
        `reversible` 为真且 `reverse_code` 恰好是 `RunPython.noop` 本尊。
        """
        from django.db.migrations import RunPython

        operations = self._migration_module().Migration.operations
        run_python = [op for op in operations if isinstance(op, RunPython)]
        self.assertEqual(len(run_python), 1)
        self.assertTrue(run_python[0].reversible)
        self.assertIs(run_python[0].reverse_code, RunPython.noop)
