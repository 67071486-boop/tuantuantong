"""T1 后端日志契约：`/orders/<单号>/audit/` 的 3 个新键 + 新写端点 `/orders/<单号>/notes/`。

订单详情页 1:1 复刻线，计划 `docs/plans/2026-09-22-order-detail-replica.md` §4 T1。

两件事：

1. `GET /api/business/orders/<单号>/audit/` 补 **3 个键**（`id` / `summary` / `log_type`），
   而**既有 6 键的名字与顺序一字不动**（有既有页面与用例在用）；
2. 新写端点 `POST /api/business/orders/<单号>/notes/`（订单详情页「添加订单日志」框）
   —— 新动作 `order.note`（**新拟**），内容进 `reason`、`summary` 由模板渲染
   （调用方**不得**直传 `summary`，spec §5.9）。

## 断言纪律（本仓硬规则，逐条对应）

- **断言打在真实出口**（E-0009）：`notes/` 一律断言 `OperationLog` 的**数据库行**，
  ⛔ 不拿响应体的 `data` 当「写进去了」的证据（那是中间层）。每个「零写入」断言
  前面都有一条**阳性对照**（先证明这条路径真的写得进去），否则空表能让它空洞通过。
- **期望值写字面值**（E-0024）：模板整句、业务码、列宽都写字面值，⛔ 不写
  `ACTION_TEMPLATES["order.note"]` 这类自指常量（那样模板被改坏时断言不会红）。
- **跨租户的否定断言不用主键指认另一租户的行**（AGENTS.md §5）：判据用
  **租户内唯一、且两租户取值不同的业务值**（订单号 / 自由文本内容）。
- **`is not None` 边界口径**（E-0021）：内容是否给出的判据不用真值
  （`0` / `""` 都是合法输入，只是这里另有非空要求）。
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.log import record
from apps.audit.models import OperationLog
from apps.business.models import Game, Order
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

# ── 租户与端点 ────────────────────────────────────────────────────────
DOMAIN_A = "tenant.test.com"

#: 判据用的业务值（租户内唯一、且两租户取值不同；**不是主键**）。
#: `SHARED_NO` 在两租户**各自存在**——订单号只有租户内唯一（spec D10），
#: 这正是「**同一个单号**也不许看到/写到别的租户的行」这条判据的来源。
SHARED_NO = "20260922000000000201"
ONLY_B_NO = "20260922000000000202"

NOTE_USER = "订单日志操作人"
NO_NODE_USER = "订单日志无节点用户"

#: 跨租户判据用的自由文本标记串（两租户取值不同、租户内唯一）。
MARKER_A = "A 租户的订单日志内容"
MARKER_B = "B 租户的订单日志内容"

#: `OperationLog.reason` / `summary` 的列宽（写字面值，E-0024：不引模型元数据自证）。
REASON_WIDTH = 255

#: `order.note` 的模板整句（写字面值，⛔ 不引 `ACTION_TEMPLATES`）
#: + ⭐ 审计日志线（spec §3.3）的人为前缀 `【显示名】`。夹具用户 `NOTE_USER` 没有
#: `first_name` ⇒ 显示名就是它的登录账号。
NOTE_SUMMARY = f"【{NOTE_USER}】添加订单 {SHARED_NO} 的日志：客户要求今晚 10 点前完成"
NOTE_CONTENT = "客户要求今晚 10 点前完成"

#: `order.change_taker_price` 的模板整句（同上，用于「summary 是服务端渲染的整句」）
#: + 同一个人为前缀。
PRICE_SUMMARY = f"【{NOTE_USER}】修改了订单接单价格（原订单价格：199.50元，修改后价格：88.00元）（订单 {SHARED_NO}）"

#: ⭐ 审计日志线（spec §3.6）的显示名判据：合成昵称（⛔ 不抄真实值，E-0077）。
#: `NOTE_USER` 夹具默认**没有** `first_name` ⇒ 显示名会退化成登录账号、判据失去判别力，
#: 所以人为那半的用例显式给它一个昵称，断言 `actor_display` **等于昵称、不等于账号**。
NICKNAME = "测试昵称甲"

#: 机器行（`actor_id` 为空）的 `actor`：形状照「上家带来的显示名」（spec §3.2 口径 2）。
PEER_DISPLAY_NAME = "测试上家昵称乙"

#: ⭐ **本仓机器来源**的机器名（合成值；用它断「机器正文里没有人名」）。
MACHINE_ACTOR = "系统同步"

#: 两个端点的门（I-1 / M-1）：
#: - 读端点 `audit` 与详情页 `retrieve` 是**同一个 any 元组**（`view_detail` ∪ `view_content`）；
#: - 写端点 `notes` 挂**对标专门节点** `order.add_log`（27），**不**借读端点的门。
#: ⚠️ 这里**不**把节点 code 写成常量再引用生产代码——它只是本文件要挂的那些 code。
READ_NODE = "order.view_detail"
READ_NODE_ALT = "order.view_content"
WRITE_NODE = "order.add_log"


class _TwoTenantLogCase(TenantTestCase):
    """两个租户 + 一个已登录租户 A 的 client 的公共夹具。

    ⚠️ 第二个租户**必须**在 `setUpClass` 里建：在用例内部建 schema 会与外层事务相冲
    （schema DDL 不能在 `TestCase` 的事务里回滚）。每个子类给自己的 schema 名
    （`SCHEMA_B` / `DOMAIN_B`）——同名会让第二个类在 `provision_tenant` 上撞已存在的 schema。
    """

    SCHEMA_B = "order_detail_log_b"
    DOMAIN_B = "order-detail-log-b.test.local"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a = cls.tenant
        cls.tenant_b, _ = provision_tenant(
            schema_name=cls.SCHEMA_B, name="订单日志契约租户 B", domain=cls.DOMAIN_B
        )

    def setUp(self):
        # 第一行：挡住上一条用例把连接留在 B 的 schema 上的残留（与
        # `tests/test_operation_log_isolation.py` 同款；跨域名请求不会自动还原 schema）。
        connection.set_tenant(self.tenant_a)
        self.client = make_logged_in_client(
            domain=DOMAIN_A,
            username=NOTE_USER,
            # ⚠️ `nodes` 只挂在**已存在的 Group** 对应的 Role 上（`_grant_nodes` 按
            # `user.groups` 遍历）⇒ 不传 `groups` 时节点一个都挂不上、请求全部 403。
            # ⚠️ 写端点 `notes` 自 I-1 起要 `order.add_log`（**不再**是 `order.view_detail`）
            # ⇒ 这个"操作人"夹具必须同时持有读写两个节点，否则本文件所有 `_post_note` 都会 403。
            groups=("客服",),
            nodes=(READ_NODE, WRITE_NODE, "order.submit_payout_price", "order.edit_payout_price"),
        )
        self.base = "/api/business/orders"
        self.game = Game.objects.create(name="原神")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        # 详情的 `retrieve` / `audit` / `notes` 三条都经 `get_object()`（被 `game_scope`
        # 收窄的 queryset）⇒ 这个"操作人"必须显式持有 `self.game`。
        # ⚠️ 本文件里凡断言「能读 / 能写」的账号都补；断言 403 的（`only_read` / `bare`）
        # 与匿名的**不补**。期望值一个字没改。
        grant_games(get_user_model().objects.get(username=NOTE_USER), self.game)

    # ── 造数据 ────────────────────────────────────────────────────────
    def _make_order(self, order_no, *, game=None, total_price="199.50"):
        """直接落一行订单（不经 API）——本文件测的是日志出口，不是录单。"""
        return Order.objects.create(
            order_no=order_no,
            game=game or self.game,
            project_text="满星",
            total_price=Decimal(total_price),
        )

    def _make_order_in_b(self, order_no):
        """在租户 B 里造一行订单（结束后连接回到 A）。

        ⚠️ `Game.name` 有租户内唯一约束（`uniq_game_name`）⇒ 同一用例里造两行订单时
        必须复用同一个游戏（`get_or_create`），不能每次 `create`。
        """
        with tenant_context(self.tenant_b):
            game_b, _ = Game.objects.get_or_create(name="B 租户的游戏")
            return Order.objects.create(
                order_no=order_no, game=game_b, project_text="B 租户的订单"
            )

    def _write_note_row_in_b(self, order_no, content):
        """在租户 B 里经**唯一写入出口** `record()` 写一行（结束后连接回到 A）。"""
        with tenant_context(self.tenant_b):
            return record(
                log_type=10,
                action="order.note",
                actor="B 租户操作人",
                # ⭐ 本行只是「B 库里有一行」的夹具（没有认证态用户）
                # ⇒ 显式声明机器来源（`record()` 对「没有操作人」fail-closed）。
                system=True,
                target_kind="order",
                target_id=order_no,
                target_name=order_no,
                order_no=order_no,
                reason=content,
            )

    def _post_note(self, order_no, payload, *, client=None):
        return (client or self.client).post(
            f"{self.base}/{order_no}/notes/",
            payload,
            content_type="application/json",
        )

    def _note_rows(self, order_no=None):
        rows = OperationLog.objects.filter(action="order.note")
        if order_no is not None:
            rows = rows.filter(order_no=order_no)
        return rows


class OrderAuditContractTests(_TwoTenantLogCase):
    """`GET /orders/<单号>/audit/`：既有 6 键 + 3 个新键（订单详情页 1:1 复刻线）。"""

    SCHEMA_B = "order_detail_audit_b"
    DOMAIN_B = "order-detail-audit-b.test.local"

    def test_rows_keep_the_first_nine_keys_and_append_actor_display(self):
        """键集与**顺序**：既有 9 键一字不动，第 10 键 `actor_display` 追加在末尾。

        ⚠️ 断的是**整张键表**（含顺序），不是 `assertIn`：顺序变了也是破坏既有契约。
        ⭐ 审计日志线（spec §3.6 / §4 判据 4）把 `actor_display` 加进**末尾**——
        前 6 键（`action`/`actor`/`reason`/`from`/`to`/`created_at`）与第 7–9 键
        （`id`/`summary`/`log_type`）**一个键都没动**、顺序也没动。
        """
        self._make_order(SHARED_NO)
        # 阳性对照 + 真实写路径：走 `/price/` 产生一行留痕（summary 由服务端渲染）。
        response = self.client.post(
            f"{self.base}/{SHARED_NO}/price/",
            {"total_price": "88.00", "reason": "客户砍价"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0)

        body = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(len(body["data"]), 1, body["data"])
        row = body["data"][0]

        self.assertEqual(
            list(row),
            [
                # ── 既有 6 键（名字与顺序一字不动）──
                "action", "actor", "reason", "from", "to", "created_at",
                # ── 第 7–9 键（订单详情线加的，本次同样一字不动）──
                "id", "summary", "log_type",
                # ── 第 10 键（本线新加，追加在**最末**）──
                "actor_display",
            ],
            row,
        )
        # 既有 6 键的**值**同样没变（前半段的形状逐字）
        self.assertEqual(row["action"], "order.change_taker_price")
        self.assertEqual(row["actor"], NOTE_USER)
        self.assertEqual(row["reason"], "客户砍价")
        self.assertEqual(row["from"], "199.50")
        self.assertEqual(row["to"], "88.00")

    def test_actor_display_is_the_nickname_for_a_human_row(self):
        """判据 5（人为那半）：走**真实端点**写的行 ⇒ `actor_display` = **昵称**。

        ⚠️ 夹具用户默认没有 `first_name`（显式取显示名会退化成账号名、判据失去判别力）
        ⇒ 这里先给它一个 `first_name`。断的是「昵称 ≠ 登录账号」——
        退回 `row.actor` 的实现会让这条**当场红**。
        ⭐ 2026-10-03 同一条整链判据里**也断正文**——
        `summary` 必须含昵称（「所有人为操作必须带上操作人」的读面出口），
        且**不含**登录账号（两个方向都钉，⛔ 只断一个方向会漏）。
        """
        self._make_order(SHARED_NO)
        user = get_user_model().objects.get(username=NOTE_USER)
        user.first_name = NICKNAME
        user.save(update_fields=["first_name"])

        response = self.client.post(
            f"{self.base}/{SHARED_NO}/price/",
            {"total_price": "88.00", "reason": "客户砍价"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())

        row = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()["data"][0]
        self.assertEqual(row["actor_display"], NICKNAME)
        # 操作人列**不再**回登录账号（两个方向都钉：值等于昵称、且不等于账号）
        self.assertEqual(row["actor"], NOTE_USER)
        self.assertNotEqual(row["actor_display"], row["actor"])
        # ⭐ 正文那一半：显示名在前缀里、且逐字等于该动作的渲染结果。
        self.assertEqual(
            row["summary"],
            f"【{NICKNAME}】修改了订单接单价格（原订单价格：199.50元，修改后价格：88.00元）（订单 {SHARED_NO}）",
        )
        self.assertIn(NICKNAME, row["summary"])
        self.assertNotIn(NOTE_USER, row["summary"])

    def test_actor_display_falls_back_to_actor_for_a_machine_row(self):
        """判据 5（回落那半）：`actor_id` 为空的行 ⇒ `actor_display` 回落 `actor`。

        ⚠️ 这是一条**跨列**判据：昵称映射里没有 `None` 这个键 ⇒ 若不回落，
        这一列会是 `None`（前端 `actor_display ?? actor` 会显示账号，但接口本身撒谎）。
        ⚠️ 机器行的 `actor` 用**上家带来的显示名**形状（spec §3.2 的口径 2）。
        ⭐ 2026-10-03 同一条整链判据里**也断正文**。
        机器行正文以 `【来源】` 开头（来源 = `actor` 列，不新造名称）。
        本用例钉「加了、且加的就是 `actor`」。
        ⚠️ 用一条模板里**没有** `{actor_name}` 的动作来断这一半（`order.peer_withdraw` 的模板
        **自带**上家带来的显示名，那是**模板正文**、不是 §3.3 的前缀规则 ⇒ 它在这条判据上不成立）。
        """
        self._make_order(SHARED_NO)
        record(
            log_type=10,
            action="order.peer_withdraw",
            actor=PEER_DISPLAY_NAME,
            actor_id=None,
            # ⭐ 机器来源必须**显式**声明（`record()` fail-closed）。
            system=True,
            target_kind="order",
            target_id=SHARED_NO,
            order_no=SHARED_NO,
        )
        # 第二条（**同一批**机器行）：模板里没有名字位的那条动作 ⇒ 来源由前缀给、只给一次。
        record(
            log_type=10,
            action="order.transition",
            actor=MACHINE_ACTOR,
            actor_id=None,
            system=True,
            target_kind="order",
            target_id=SHARED_NO,
            order_no=SHARED_NO,
            field_name="status",
            from_value="待验收",
            to_value="已完成",
        )
        rows = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()["data"]
        self.assertEqual(len(rows), 2, rows)
        by_action = {row["action"]: row for row in rows}
        peer = by_action["order.peer_withdraw"]
        self.assertEqual(peer["actor"], PEER_DISPLAY_NAME)
        self.assertEqual(peer["actor_display"], PEER_DISPLAY_NAME)
        machine = by_action["order.transition"]
        # 状态说明以机器来源名称开头。
        self.assertEqual(
            machine["summary"],
            f"{MACHINE_ACTOR}将订单 {SHARED_NO} 的状态从待验收改为已完成",
        )
        self.assertNotIn("【", machine["summary"])
        self.assertEqual(machine["summary"].count(MACHINE_ACTOR), 1, machine["summary"])
        # ⛔ 模板自带 `{actor_name}` 的那条（`order.peer_withdraw`）**不双前缀**：
        #    它的正文里 `【` 只有模板自己那一个。
        self.assertEqual(peer["summary"].count("【"), 1, peer["summary"])
        self.assertEqual(peer["summary"].count(PEER_DISPLAY_NAME), 1, peer["summary"])

    def test_new_keys_carry_literal_values(self):
        """3 个新键的**字面值**：`id` = 行主键、`summary` = 服务端渲染的整句、`log_type` = 10。"""
        self._make_order(SHARED_NO)
        self.client.post(
            f"{self.base}/{SHARED_NO}/price/",
            {"total_price": "88.00", "reason": "客户砍价"},
            content_type="application/json",
        )
        db_row = OperationLog.objects.get(
            order_no=SHARED_NO, action="order.change_taker_price"
        )
        row = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()["data"][0]

        # `id`：就是这一行的主键（前端做 key / tie-break）
        self.assertIsInstance(row["id"], int)
        self.assertEqual(row["id"], db_row.pk)
        # `summary`：**服务端渲染好的整句**（模板输出，不是 `reason` 的原文）
        self.assertEqual(row["summary"], PRICE_SUMMARY)
        self.assertNotEqual(row["summary"], row["reason"])
        # `log_type`：8 值分类 code 里的 `10 订单相关`
        self.assertEqual(row["log_type"], 10)

    def test_summary_is_the_whole_rendered_sentence_not_a_frontend_assembly(self):
        """`summary` 是**服务端渲染的整句**——前端不许自己拼句子（spec §5.9）。

        判据：它是模板「修改了订单接单价格（原订单价格：{旧}元，修改后价格：{新}元）」
        的**完整输出**（含括号与两个「元」），而不是把 `from`/`to`/`reason` 等列原样丢出来。
        把它当「前端用各列自己拼」的替代时，这条会红。
        """
        self._make_order(SHARED_NO)
        self.client.post(
            f"{self.base}/{SHARED_NO}/price/",
            {"total_price": "88.00", "reason": "客户砍价"},
            content_type="application/json",
        )
        row = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()["data"][0]
        self.assertEqual(row["summary"], PRICE_SUMMARY)
        # `summary` 与任何单个列都不同（它不是某一列的回显）
        for key in ("action", "actor", "reason", "from", "to"):
            self.assertNotEqual(row["summary"], row[key], key)

    def test_copied_order_log_distinguishes_orders_with_the_same_source_number(self):
        original = self._make_order(SHARED_NO)
        original.source_order_no = "SHOP-42"
        original.save(update_fields=["source_order_no"])
        clerk = make_logged_in_client(
            domain=DOMAIN_A, username="copy-log-clerk", groups=("客服",),
            nodes=(READ_NODE, "order.add_edit"),
        )
        grant_games(get_user_model().objects.get(username="copy-log-clerk"), self.game)
        copied = clerk.post(f"{self.base}/{SHARED_NO}/copy").json()
        self.assertEqual(copied["code"], 0, copied)
        new_no = copied["data"]["order_no"]
        self.assertNotEqual(new_no, SHARED_NO)
        self.assertEqual(copied["data"]["source_order_no"], "SHOP-42")
        new_row = OperationLog.objects.get(order_no=new_no, action="order.copy")

        legacy_summary = f"【旧复制人】复制订单 SHOP-42 生成新单：{new_no}"
        legacy_row = OperationLog.objects.create(
            log_type=10, action="order.copy", actor="旧复制人",
            order_no=new_no, reason="SHOP-42", summary=legacy_summary,
        )

        result = clerk.get(f"{self.base}/{new_no}/audit/").json()
        self.assertEqual(result["code"], 0, result)
        rows = {row["id"]: row for row in result["data"]}
        self.assertEqual(
            rows[new_row.pk]["summary"],
            f"【copy-log-clerk】复制订单 {SHARED_NO} 生成新单：{new_no}（新单来源单号：SHOP-42）",
        )
        self.assertEqual(rows[new_row.pk]["from"], SHARED_NO)
        self.assertEqual(rows[new_row.pk]["reason"], "SHOP-42")
        self.assertEqual(rows[legacy_row.pk]["summary"], legacy_summary)
        self.assertEqual(rows[legacy_row.pk]["reason"], "SHOP-42")
        legacy_row.refresh_from_db()
        self.assertEqual(legacy_row.summary, legacy_summary)

    def test_reading_an_order_log_preserves_source_numbers_and_free_text(self):
        order = self._make_order(SHARED_NO)
        order.source_order_no = f"SHOP-{SHARED_NO}-1"
        order.save(update_fields=["source_order_no"])
        reason = f"打手备注 {SHARED_NO}、长编号 {SHARED_NO}7、子单 {SHARED_NO}-2"
        context = f"对照子单 {SHARED_NO}-2 和长编号 {SHARED_NO}7。旧操作人将订单"
        source_summary = f"{context} {order.source_order_no} 改为待验收（原因：{reason}）"
        legacy_summary = f"{context} {SHARED_NO} 改为待验收（原因：{reason}）"
        legacy_rows = [
            OperationLog.objects.create(
                log_type=10, action="order.transition", actor="旧操作人",
                order_no=SHARED_NO, source_order_no=source, reason=reason, summary=summary,
            )
            for source, summary in ((order.source_order_no, source_summary), ("", legacy_summary))
        ]

        result = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()
        self.assertEqual(result["code"], 0, result)
        rows = {row["id"]: row for row in result["data"]}
        for log, persisted_summary, persisted_source in zip(
            legacy_rows, (source_summary, legacy_summary), (order.source_order_no, ""), strict=True,
        ):
            with self.subTest(log=log.pk):
                self.assertEqual(rows[log.pk]["summary"], source_summary)
                self.assertEqual(rows[log.pk]["reason"], reason)
                log.refresh_from_db()
                self.assertEqual(log.order_no, SHARED_NO)
                self.assertEqual(log.source_order_no, persisted_source)
                self.assertEqual(log.summary, persisted_summary)

    def test_audit_is_tenant_scoped_even_for_the_same_order_no(self):
        """跨租户不可见：**同一个单号**在两租户各自成立时，A 的凭据只读得到 A 的行。

        ⚠️ 判据是**租户内唯一的自由文本**（`MARKER_A` / `MARKER_B`），**不是主键**
        ——两个 schema 的主键序列各自从 1 开始（AGENTS.md §5 的硬规则）。
        """
        # ── A：同一单号 + 一行标记 ──
        self._make_order(SHARED_NO)
        record(
            log_type=10,
            action="order.note",
            actor="A 租户操作人",
            # ⭐ 夹具行（无认证态）⇒ 显式声明机器来源。
            system=True,
            target_kind="order",
            target_id=SHARED_NO,
            target_name=SHARED_NO,
            order_no=SHARED_NO,
            reason=MARKER_A,
        )
        # ── B：同一个单号 + 另一行标记；另有一个只存在于 B 的单号 ──
        self._make_order_in_b(SHARED_NO)
        self._make_order_in_b(ONLY_B_NO)
        self._write_note_row_in_b(SHARED_NO, MARKER_B)
        self._write_note_row_in_b(ONLY_B_NO, MARKER_B)

        # ── 阳性对照：A 自己的那一行必须看得到 ──
        body = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()
        self.assertEqual(body["code"], 0, body)
        reasons = [row["reason"] for row in body["data"]]
        self.assertEqual(reasons, [MARKER_A], body["data"])
        self.assertNotIn(MARKER_B, reasons)

        # ── 只存在于 B 的单号：A 的凭据拿不到（不是"看到了 B 的行"，而是这条单在 A 里不存在）
        missing = self.client.get(f"{self.base}/{ONLY_B_NO}/audit/")
        self.assertEqual(missing.status_code, 200, missing.content[:300])
        self.assertEqual(missing.json()["code"], 1002, missing.json())
        self.assertIsNone(missing.json()["data"])


class OrderNoteEndpointTests(_TwoTenantLogCase):
    """`POST /orders/<单号>/notes/`：写一行订单日志（订单详情页「添加」框）。"""

    SCHEMA_B = "order_detail_note_b"
    DOMAIN_B = "order-detail-note-b.test.local"

    # ── 正常写入：断言**数据库行**（E-0009）─────────────────────────────
    def test_a_note_is_written_as_exactly_one_database_row(self):
        self._make_order(SHARED_NO)
        response = self._post_note(SHARED_NO, {"content": NOTE_CONTENT})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())

        rows = list(self._note_rows(SHARED_NO))
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        # 逐列写字面值（E-0024）：action / 目标 / 内容 / 渲染出来的整句 / 操作人 / 分类
        self.assertEqual(row.action, "order.note")
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, SHARED_NO)
        self.assertEqual(row.target_name, SHARED_NO)
        self.assertEqual(row.order_no, SHARED_NO)
        self.assertEqual(row.reason, NOTE_CONTENT)
        self.assertEqual(row.summary, NOTE_SUMMARY)
        # ⚠️ 操作人取**认证态**（spec §5.9：从不取请求体的 `actor`），
        # `actor_id` 同步取认证态的主键。
        self.assertEqual(row.actor, NOTE_USER)
        self.assertEqual(
            row.actor_id, get_user_model().objects.get(username=NOTE_USER).pk
        )
        self.assertEqual(row.log_type, 10)

    def test_the_write_response_is_the_same_ten_key_row_as_audit(self):
        """M-7：`POST notes` 的**响应体形状** = `/audit/` 的同一行 10 键（不是某个子集）。

        ⚠️ 判据有两层，缺一层就会被"看起来对"骗过：
        ① **键表（含顺序）逐字**——照抄既有 6 键 + 第 7–9 键 + 第 10 键 `actor_display`
           那份契约（审计日志线 spec §3.6 追加）；
        ② 与**同一个读出口**（`GET …/audit/`）返回的同一行**逐键同名**——只写字面表的话，
        读出口将来变了、写出口没变，这条不会红。
        值也要钉：`id` = 该数据库行的主键、`summary` = 服务端渲染的**整句**（不是 `reason` 回显）、
        `log_type` = 10、`actor_display` = 昵称（夹具 `NOTE_USER` 无 `first_name` ⇒ 回落账号）。
        """
        self._make_order(SHARED_NO)
        response = self._post_note(SHARED_NO, {"content": NOTE_CONTENT})
        self.assertEqual(response.status_code, 200, response.content[:300])
        row = response.json()["data"]

        self.assertEqual(
            list(row),
            [
                # ── 既有 6 键（与 `/audit/` 一字不动）──
                "action", "actor", "reason", "from", "to", "created_at",
                # ── 第 7–9 键 ──
                "id", "summary", "log_type",
                # ── 第 10 键（本线追加在**最末**）──
                "actor_display",
            ],
            row,
        )
        # ② 与读出口的**同一个 10 键形状**（读出口变了这条会红）
        read_row = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()["data"][0]
        self.assertEqual(list(row), list(read_row), (row, read_row))

        db_row = self._note_rows(SHARED_NO).get()
        self.assertEqual(row["action"], "order.note")
        self.assertEqual(row["actor"], NOTE_USER)
        self.assertEqual(row["reason"], NOTE_CONTENT)
        self.assertEqual(row["id"], db_row.pk)
        self.assertEqual(row["summary"], NOTE_SUMMARY)
        self.assertNotEqual(row["summary"], row["reason"])
        self.assertEqual(row["log_type"], 10)
        # 单行出口也要解析显示名（判据 5 的 notes 侧）：夹具无 `first_name` ⇒ 回落账号名。
        self.assertEqual(row["actor_display"], NOTE_USER)

    def test_the_caller_cannot_supply_a_ready_made_summary(self):
        """⛔ 调用方**不能**直传 `summary`：它只由模板从行内列渲染（spec §5.9 的硬规矩）。

        传了 `summary` 也不生效——落库的必须是模板整句（`record()` 根本没有这个形参）。
        """
        self._make_order(SHARED_NO)
        forged = "调用方自己拼的句子"
        response = self._post_note(
            SHARED_NO, {"content": "正文内容", "summary": forged}
        )
        self.assertEqual(response.json()["code"], 0, response.json())

        row = self._note_rows(SHARED_NO).get()
        self.assertEqual(row.reason, "正文内容")
        self.assertEqual(row.summary, f"【{NOTE_USER}】添加订单 {SHARED_NO} 的日志：正文内容")
        self.assertNotEqual(row.summary, forged)

    def test_the_body_actor_is_not_what_lands_in_the_audit_row(self):
        """入参 `actor` 与既有写端点**同口径**（超长 ⇒ `1000`），但**不落任何列**。

        spec §5.9：操作人一律取认证态（`request.user.get_username()`），
        **从不取请求体**的 `actor`——请求体里的身份可伪造，而留痕的价值就是「谁做的」为真。
        与 `/transition/`、录单的同一规则（R20）。
        """
        self._make_order(SHARED_NO)
        response = self._post_note(
            SHARED_NO, {"content": NOTE_CONTENT, "actor": "假冒的操作人"}
        )
        self.assertEqual(response.json()["code"], 0, response.json())
        row = self._note_rows(SHARED_NO).get()
        self.assertEqual(row.actor, NOTE_USER)
        self.assertNotEqual(row.actor, "假冒的操作人")

        # 超长 `actor`（> `User.username` 的 150）⇒ 业务码 `1000`，不是 HTML 500（E-0010）
        over = self._post_note(
            SHARED_NO, {"content": "第二条", "actor": "长" * 200}
        )
        self.assertEqual(over.status_code, 200, over.content[:300])
        self.assertEqual(over.json()["code"], 1000, over.json())
        self.assertEqual(self._note_rows(SHARED_NO).count(), 1)

    # ── 空值：1000 + 零写入（每条都带阳性对照）─────────────────────────
    def test_empty_content_is_rejected_with_1000_and_writes_nothing(self):
        self._make_order(SHARED_NO)
        # 阳性对照：先证明这条路径真的写得进去（否则下面的「零写入」是空的）
        self.assertEqual(
            self._post_note(SHARED_NO, {"content": NOTE_CONTENT}).json()["code"], 0
        )
        before = self._note_rows(SHARED_NO).count()
        self.assertEqual(before, 1)

        response = self._post_note(SHARED_NO, {"content": ""})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000, response.json())
        self.assertEqual(response.json()["msg"], "订单日志内容不能为空")
        self.assertEqual(self._note_rows(SHARED_NO).count(), before)

    def test_whitespace_only_content_is_rejected_the_same_way(self):
        self._make_order(SHARED_NO)
        self.assertEqual(
            self._post_note(SHARED_NO, {"content": NOTE_CONTENT}).json()["code"], 0
        )
        before = self._note_rows(SHARED_NO).count()

        response = self._post_note(SHARED_NO, {"content": "   \t  "})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000, response.json())
        self.assertEqual(response.json()["msg"], "订单日志内容不能为空")
        self.assertEqual(self._note_rows(SHARED_NO).count(), before)

    def test_leading_and_trailing_whitespace_is_stored_verbatim(self):
        """M-5：非空内容的**首尾空白原样入库**（对标原样存 `remark`），不是被静默 strip。

        ⚠️ 这一条与上面「纯空白 ⇒ `1000`」是一对，两条必须同时绿：
        - 判空白用显式的 `validate_content`（`value.strip()` 为空才拒）；
        - 判完**返回原串**，所以 `"  正文  "` 落进 `reason` 就是 `"  正文  "`，
          正文也必须保留这几处空白。
        """
        self._make_order(SHARED_NO)
        raw = "  正文  前后都有空白  "
        response = self._post_note(SHARED_NO, {"content": raw})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0, response.json())

        row = self._note_rows(SHARED_NO).get()
        self.assertEqual(row.reason, raw)
        self.assertEqual(row.summary, f"【{NOTE_USER}】添加订单 {SHARED_NO} 的日志：{raw}")
        # 落库的**不是** strip 过的那一版（防"存的是 trimmed、断言恰好与 raw 相等"这类假绿）
        self.assertNotEqual(row.reason, raw.strip())

    def test_missing_content_is_rejected_the_same_way(self):
        self._make_order(SHARED_NO)
        self.assertEqual(
            self._post_note(SHARED_NO, {"content": NOTE_CONTENT}).json()["code"], 0
        )
        before = self._note_rows(SHARED_NO).count()

        response = self._post_note(SHARED_NO, {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000, response.json())
        self.assertEqual(response.json()["msg"], "订单日志内容不能为空")
        self.assertEqual(self._note_rows(SHARED_NO).count(), before)

    # ── 超长：业务码而非 500；列宽边界 ────────────────────────────────
    def test_overlong_content_returns_a_business_code_not_a_500(self):
        """超长内容在**入口**被拒（`1000`），不进那次插入 ⇒ 不会有 500（E-0010）。"""
        self._make_order(SHARED_NO)
        response = self._post_note(SHARED_NO, {"content": "长" * (REASON_WIDTH + 1)})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1000, response.json())
        self.assertEqual(self._note_rows(SHARED_NO).count(), 0)

    def test_content_exactly_at_the_column_width_is_accepted(self):
        """长备注在写入和查询时保留完整内容，正文仍标明来源单号。"""
        order = self._make_order(SHARED_NO)
        order.source_order_no = "SOURCE-NOTE-1"
        order.save(update_fields=["source_order_no"])
        content = "长" * REASON_WIDTH
        response = self._post_note(SHARED_NO, {"content": content})
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)

        row = self._note_rows(SHARED_NO).get()
        self.assertEqual(len(row.reason), REASON_WIDTH)
        self.assertEqual(row.reason, content)
        self.assertEqual(row.order_no, SHARED_NO)
        prefix = f"【{NOTE_USER}】添加订单 SOURCE-NOTE-1 的日志："
        self.assertTrue(row.summary.startswith(prefix))
        self.assertEqual(len(row.summary), REASON_WIDTH)
        self.assertEqual(body["data"]["summary"], prefix + content)

        audit = self.client.get(f"{self.base}/{SHARED_NO}/audit/").json()
        self.assertEqual(audit["code"], 0, audit)
        self.assertEqual(audit["data"][0]["summary"], prefix + content)

    # ── 跨租户：A 的凭据对 B 的单号 ⇒ 失败且零写入 ─────────────────────
    def test_a_note_to_another_tenants_order_fails_and_writes_nothing(self):
        """⚠️ 判据不用主键指认另一租户的行（AGENTS.md §5）：用**订单号 + 自由文本**。"""
        self._make_order(SHARED_NO)
        self._make_order_in_b(ONLY_B_NO)

        # 阳性对照：A 写自己的单必须成功（否则下面的「零写入」证明不了任何事）
        self.assertEqual(
            self._post_note(SHARED_NO, {"content": MARKER_A}).json()["code"], 0
        )
        before = self._note_rows().count()
        self.assertEqual(before, 1)

        # 负例：A 的凭据 + B 的单号 ⇒ 失败（这条单在 A 的 schema 里不存在）
        response = self._post_note(ONLY_B_NO, {"content": MARKER_B})
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 1002, response.json())
        self.assertIsNone(response.json()["data"])
        # A 侧：一条都没多，也没有落在 B 单号名下的行
        self.assertEqual(self._note_rows().count(), before)
        self.assertEqual(self._note_rows(ONLY_B_NO).count(), 0)

        # B 侧：那条内容一个字都没进 B 的 schema（跨租户写入必须失败，不是"写到了对面"）
        with tenant_context(self.tenant_b):
            self.assertEqual(
                self._note_rows(ONLY_B_NO).count(), 0,
                "A 的请求把行写进了 B 的 schema",
            )
            self.assertFalse(
                OperationLog.objects.filter(reason=MARKER_B).exists(),
                "跨租户请求在 B 里留下了行",
            )

    # ── 权限：写端点挂**对标专门节点** 27，读端点是详情页的 any 元组 ─────────
    def test_notes_gate_is_order_add_log_only(self):
        """I-1 的口径：`notes` 只认 `order.add_log`（27），**不**借 `order.view_detail`。

        ⚠️ 判据必须在**同一对**动作上给出两个方向（只测一条会把"借了读端点的门"放过去）：
        - 只持 `order.add_log` ⇒ 写 200（阳性对照）；
        - 只持 `order.view_detail` ⇒ 写 **403**（这就是 I-1 点名的形状：借门会让"只看详情"
          的自定义角色变成能写日志）。
        ⚠️ 两个用户各挂**自己的** Group（`WD1` / `VD1`）——`_grant_nodes` 把节点挂在
        `Group → Role` 上，复用同一个 Group 会在**同一用例内**累积两边的节点、把判据冲掉
        （③线 `_grant_nodes` 的 docstring 明写这个污染；评审的探针就是那么踩的）。
        """
        self._make_order(SHARED_NO)
        only_write = make_logged_in_client(
            domain=DOMAIN_A, username="日志只持写节点用户",
            groups=("WD1",), nodes=(WRITE_NODE,),
        )
        # （按游戏权限收窄）：**夹具补齐**。下面这条是阳性对照（必须写得进去），
        # 而 `notes` 经 `get_object()` 取单 ⇒ 只持写节点的账号同样要持有这单的游戏。
        grant_games(
            get_user_model().objects.get(username="日志只持写节点用户"), self.game
        )
        only_read = make_logged_in_client(
            domain=DOMAIN_A, username="日志只持读节点用户",
            groups=("VD1",), nodes=(READ_NODE,),
        )
        # 阳性对照：只持 27 的用户必须写得进去（否则下面的 403 证明不了任何事）
        accepted = self._post_note(SHARED_NO, {"content": NOTE_CONTENT}, client=only_write)
        self.assertEqual(accepted.status_code, 200, accepted.content[:300])
        self.assertEqual(accepted.json()["code"], 0, accepted.json())
        # 只持详情节点 ⇒ 403（且一行都不落）
        denied = self._post_note(SHARED_NO, {"content": "只有详情节点"}, client=only_read)
        self.assertEqual(denied.status_code, 403, denied.content[:300])
        self.assertEqual(self._note_rows(SHARED_NO).count(), 1)

    def test_audit_gate_matches_the_detail_gate_on_both_nodes(self):
        """M-1 的口径：`audit` 与 `retrieve` 是**同一个** any 元组（`view_detail` ∪ `view_content`）。

        ⚠️ 放宽的正是这一条判据：只持 `order.view_content`（15「显示内容项」）的账号
        **能开详情页**，就必须也能读**同一页**的日志（原先 `audit` 只判 `view_detail`
        ⇒ 开得了页、读不到日志）。反向的 `view_detail` 与"两个都没有 ⇒ 403"一起测，
        免得只测新方向时把"any 元组被写成了 all"放过去。
        """
        self._make_order(SHARED_NO)
        only_content = make_logged_in_client(
            domain=DOMAIN_A, username="日志只持内容项用户",
            groups=("VC1",), nodes=(READ_NODE_ALT,),
        )
        only_detail = make_logged_in_client(
            domain=DOMAIN_A, username="日志只持详情页用户",
            groups=("VD2",), nodes=(READ_NODE,),
        )
        # （按游戏权限收窄）：**夹具补齐**。这一条断言这两个账号都**能**开详情页
        # 与读日志（200 + code 0），两条读写路径都经 `get_object()` ⇒ 都要持有这单的游戏。
        # ⛔ 下面的 `bare`（断言 403）**不补**：补了会把「没节点 ⇒ 403」的判据冲掉。
        for _username in ("日志只持内容项用户", "日志只持详情页用户"):
            grant_games(get_user_model().objects.get(username=_username), self.game)
        for label, client in (("view_content", only_content), ("view_detail", only_detail)):
            detail = client.get(f"{self.base}/{SHARED_NO}/")
            self.assertEqual(detail.status_code, 200, (label, detail.content[:300]))
            self.assertEqual(detail.json()["code"], 0, (label, detail.json()))
            log = client.get(f"{self.base}/{SHARED_NO}/audit/")
            self.assertEqual(log.status_code, 200, (label, log.content[:300]))
            self.assertEqual(log.json()["code"], 0, (label, log.json()))
        # 两个节点都没有（仍已认证）⇒ 详情与日志都 403
        bare = make_logged_in_client(domain=DOMAIN_A, username=NO_NODE_USER)
        self.assertEqual(bare.get(f"{self.base}/{SHARED_NO}/").status_code, 403)
        self.assertEqual(bare.get(f"{self.base}/{SHARED_NO}/audit/").status_code, 403)

    def test_anonymous_request_is_rejected(self):
        """匿名 ⇒ 401（传输层语义，保留 HTTP 状态码），且不落行。"""
        from django.test import Client

        self._make_order(SHARED_NO)
        anonymous = Client(SERVER_NAME=DOMAIN_A)
        response = anonymous.post(
            f"{self.base}/{SHARED_NO}/notes/",
            {"content": NOTE_CONTENT},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401, response.content[:300])
        self.assertEqual(response.json()["code"], 401, response.json())
        self.assertEqual(self._note_rows(SHARED_NO).count(), 0)


class OrderNoteLedgerTests(TenantTestCase):
    """新写端点**已登记进台账**（spec §10.3；`tests/test_operation_log_ledger.py`）。

    ⚠️ 这里**不**重跑覆盖率网（它在 `tests/test_operation_log_coverage.py`，
    `test_every_enumerated_write_endpoint_is_classified` 已经覆盖全项目）——
    这里只钉「本端点登记在哪、指向哪个 action」，避免有人把它的那条登记删掉后
    只剩覆盖率网报一个宽泛的差集。
    """

    def test_the_notes_endpoint_is_registered_as_instrumented(self):
        from tests.test_operation_log_ledger import INSTRUMENTED

        key = ("post", "api/business/orders/<order_no>/notes/")
        self.assertEqual(
            INSTRUMENTED["apps/business/api/order_views.py"].get(key),
            "order.note",
            "订单日志写入端点没有按 HTTP 方法（POST）登记进 INSTRUMENTED",
        )
