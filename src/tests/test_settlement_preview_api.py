"""结算预览端点（`GET /api/business/settlements/preview`）的判据网（计划 §3 T2.1）。

**它是什么**：「清算绩效」弹窗在点「确定结算或修改」**之前**拿它填那几格
（所选订单笔数 / 预计要结总额 / 实际需结总额 / 结算对象 / 微信收款码）。
逐字段口径与出处写在 `apps/business/settlement_preview.py` 的模块 docstring 里。

七条纪律（每条都有会红的用例）：

1. **只读 ⇒ 零副作用**：不写库、不写审计、不改 `updated_at`（`PreviewReadOnlyTests`）；
2. **命中行数**是 `count`：查不到的 id **不计**；全查不到 ⇒ `0`（不是报错）；
3. `bonus_total` 是 `Σ payable_net`（`bonus_amount − 扣点 + Σ**未结账**调整`），
   **不是** `Σ bonus_amount`——并有一条钉住「已结账的调整不再计入」；
   ⭐ **P1-2**：它还**只**累加「这一趟 `/settle` 真的会结掉」的行
   （`settlement._resolve_batch` 的 `settleable_ids`）——详见本文件
   `test_matched_rows_report_count_totals_and_counts` 与
   `test_an_ownerless_row_is_excluded_and_its_reason_is_reported` 的改准说明；
4. `temp_total` 是 `Σ temp_amount`；
5. `owners` 按人聚合、**用户名升序**；⭐ **P1-2 起它也只含可结的行**（与 `bonus_total`
   同口径——两格在弹窗上并排显示，金额之和必须对得上），无归属人 / 已结清 / 被冻结的行
   都不出现（原因进 `errors`）；
6. `payable_net` 抛 `AmountError` 的那一行**按 0.00 计入并在 `errors` 里如实列出**
   （⛔ 不静默当 0、也不整页 500）；⭐ **P1-2 起 `errors` 还写「这次结不掉」的原因**
   （逐 id，判据与结账侧同一份 `_reject_reasons`；前端不渲染它，只供接口层排查）；
7. **数据范围与结算列表同一套卡点**：越出自己游戏范围的 id 不进 `count`
   （跨租户另有一组，见 `CrossTenantPreviewTests`）。

⚠️ **期望值一律写字面值**（E-0024）：业务码写 `1000` / `403` / `401`，状态写
`"已结账"` / `"未结账"`、金额写 `"20.00"` ——不引被测模块导出的常量（自指断言不会红）。
⚠️ **留痕断言打数据库行**（E-0009）：本项目没有 `LOGGING` 配置，`logger` 断言会全绿而生产
一行都不写。本文件不写任何日志断言。
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    AuditLog,
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
    PerformanceAdjustment,
    ReviewStatus,
    SettleStatus,
)
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import grant_games, make_logged_in_client

#: 计划 §3 T2.1 的最终路径（**没有**尾斜杠，与 `/settlements/settle` 同形）。
PREVIEW_URL = "/api/business/settlements/preview"
#: 预览的闸门节点（**读**）：与结算列表 / 待归属 / 结算对象人员同一颗。
NODE_READ = "finance.payroll"

#: 本文件里「扣点模式」的字面值（⛔ 不从 `performance_amount` import：
#: 那是被测代码的常量，引它会让「模型/常量被改坏」也照样绿，E-0024）。
DEDUCT_FIXED = "1"


class _PreviewFixture(TenantTestCase):
    """建单 / 建绩效 / 建调整的小夹具（各用例只表达**自己的差异**）。"""

    def setUp(self):
        self.game_a = Game.objects.create(name="原神")
        self.game_b = Game.objects.create(name="崩铁")
        # 夹具默认给「当前租户全部游戏」+ 显式声明本端点要过的门。
        # ⚠️ `groups=` 是**必需的**：`make_logged_in_client(nodes=…)` 把节点挂在
        #    「该用户当前所在 Group」对应的 `Role` 上（`_grant_nodes`），**没有组就没地方挂**
        #    ⇒ 只写 `nodes=` 会静默变成「一个节点都没有」，症状是 403（看起来像判据写错了）。
        # ⚠️ 组名（= `Role` 名，`max_length=5`）**本文件内各账号刻意不同**：
        #    `Role` 是按**组名**共享的，同组会把先建的那个账号的节点**漏给**后一个
        #    （实测踩到：403 的用例拿到 200）⇒ 权限用例必须给各自独立的组。
        self.finance = make_logged_in_client(
            username="preview-fin", groups=("预览",), nodes=(NODE_READ,)
        )
        self.finance_user = User.objects.get(username="preview-fin")
        self.taker_a = User.objects.create_user(username="preview-taker-a")
        self.taker_b = User.objects.create_user(username="preview-taker-b")
        self._seq = 0

    def _order(self, *, game=None, status=OrderStatus.COMPLETED, total_price="100.00"):
        self._seq += 1
        return Order.objects.create(
            game=game or self.game_a,
            order_no=f"2026100300000{self._seq:07d}",
            status=status,
            total_price=Decimal(total_price),
        )

    def _perf(self, order, user, **overrides):
        fields = {
            "emp_type": EmpType.MAIN,
            "temp_amount": Decimal("20.00"),
            "bonus_amount": Decimal("20.00"),
            "commission_rate": Decimal("10.00"),
            "deduct_mode": "",
            "deduct_value": Decimal("0.00"),
            # ⚠️ `performance_review_needs_user` 这条 CHECK 约束要求「没有归属人 ⇒ 没有审核状态」
            #    （NULL 而不是空串）——默认值必须跟着 `user` 走，否则建行直接撞约束。
            "review_status": (
                ReviewStatus.APPROVED if user is not None else None
            ),
            "settle_status": SettleStatus.UNSETTLED,
        }
        fields.update(overrides)
        return Performance.objects.create(order=order, user=user, **fields)

    def _adj(self, performance, amount, *, settle=SettleStatus.UNSETTLED):
        return PerformanceAdjustment.objects.create(
            performance=performance,
            kind=PerformanceAdjustment.Kind.MANUAL,
            amount=Decimal(amount),
            reason="测试调整",
            operator="fin",
            settle_status=settle,
        )

    def _response(self, ids, *, client=None):
        return (client or self.finance).get(
            PREVIEW_URL,
            {"performance_ids": ",".join(str(one) for one in ids)},
        )

    def _data(self, ids, *, client=None):
        """发一次预览并断言「HTTP 200 + `code=0` + 信封有 `data`」——三条一起，失败即报全文。"""
        response = self._response(ids, client=client)
        self.assertEqual(response.status_code, 200, response.content[:400])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertIsNotNone(body["data"], body)
        return body["data"]


class PreviewPayloadTests(_PreviewFixture):
    """`count` / 两个总额 / 两个计数 / `owners` / `qr_codes` / `errors` 的口径。"""

    def test_matched_rows_report_count_totals_and_counts(self):
        """命中 2 行（一未结账、一已结账）⇒ `count=2`、两个计数各 1、`temp_total=50.00`。

        ⚠️ 「已结账」的行**照旧进预览的笔数 / 预计总额**（对标那张子页的「笔数」就是所选
        行数，不筛结账状态）——`settled_count` / `unsettled_count` 只是把它们分开数出来。

        ⭐ **P1-2改准**：`bonus_total` / `owners` 过去是 `50.00`（两行都算），
        但那条「已结账且名下没有未结账调整」的行在 `/settle` 里走的是**跳过**（Q3=A：
        成功、但一分钱不结）⇒ 预览把它算进「实际需结总额」就是多算。
        现在这两格与 `/settle` 同口径（`settlement._resolve_batch` 的 `settleable_ids`）
        ⇒ **只**剩未结账那一行的 `20.00`。⛔ 不是放宽，是收窄到「真的会结掉的钱」。
        """
        first = self._perf(
            self._order(), self.taker_a, temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
        )
        second = self._perf(
            self._order(), self.taker_a, temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"), settle_status=SettleStatus.SETTLED,
        )

        data = self._data([first.id, second.id])

        self.assertEqual(data["count"], 2)
        self.assertEqual(data["temp_total"], "50.00")
        self.assertEqual(data["bonus_total"], "20.00")
        self.assertEqual(data["settled_count"], 1)
        self.assertEqual(data["unsettled_count"], 1)
        self.assertEqual(
            data["owners"],
            [
                {
                    "user_id": self.taker_a.pk,
                    "username": "preview-taker-a",
                    # ⭐ 「显示人统一成昵称」线**追加**的显示名键；夹具这个账号
                    # 没有昵称 ⇒ 口径（昵称优先、空串回落登录账号）回落到登录账号。
                    "name": "preview-taker-a",
                    "amount": "20.00",
                }
            ],
        )
        self.assertEqual(data["errors"], [])

    def test_a_partial_hit_counts_only_the_rows_that_exist(self):
        """部分命中：真实那行照算，查不到的 id **不进** `count`（也不是报错）。"""
        perf = self._perf(self._order(), self.taker_a, temp_amount=Decimal("20.00"))

        data = self._data([perf.id, 987654321])

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["temp_total"], "20.00")
        self.assertEqual(len(data["owners"]), 1)

    def test_an_all_miss_reports_zero(self):
        """全不命中 ⇒ `count=0` 与三个 `0.00` / 空表（前端按对标的 `0笔 / 0元 / 空` 渲染）。"""
        data = self._data([987654320, 987654321])

        self.assertEqual(
            data,
            {
                "count": 0,
                "temp_total": "0.00",
                "bonus_total": "0.00",
                "settled_count": 0,
                "unsettled_count": 0,
                "owners": [],
                "qr_codes": [],
                "errors": [],
            },
        )

    def test_duplicate_ids_are_counted_once(self):
        """重复 id 只算一次（`count` 是**行数**，与结账第 1 步的去重同口径）。"""
        perf = self._perf(self._order(), self.taker_a)

        data = self._data([perf.id, perf.id, perf.id])

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["temp_total"], "20.00")

    def test_bonus_total_is_the_payable_net_not_the_bonus_amount(self):
        """`bonus_total` = `Σ payable_net` = `bonus − 扣点 + Σ**未结账**调整`。

        这一条是**有意偏离对标读数**的判据（对标那格是 `Σ bonusAmount`）：
        `bonus_amount=100.00`、固定扣点 `30.00`、一条 `+5.00` 的未结账调整
        ⇒ 应付净额 `75.00`。⛔ 写成 `100.00` 就是「一个与实际付款不一致的数」。
        ⚠️ 另加一条**已结账**的调整记录（`+100.00`）：它**不再计入**（`payable_net` 只算未结账的）
        ——没有这半条，把「未结账」筛选删掉也照样绿。
        """
        perf = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("100.00"),
            bonus_amount=Decimal("100.00"),
            deduct_mode=DEDUCT_FIXED,
            deduct_value=Decimal("30.00"),
        )
        self._adj(perf, "5.00")
        self._adj(perf, "100.00", settle=SettleStatus.SETTLED)

        data = self._data([perf.id])

        self.assertEqual(data["temp_total"], "100.00")
        self.assertEqual(data["bonus_total"], "75.00")
        self.assertEqual(data["owners"][0]["amount"], "75.00")
        self.assertEqual(data["errors"], [])

    def test_owners_are_aggregated_per_user_and_sorted_by_username(self):
        """按人聚合 + **用户名升序**（确定性顺序；对标那两个名字的固定顺序我们不照抄）。

        ⚠️ 聚合的是 **`payable_net`（生效绩效那一侧）**，不是 `temp_amount`——
        所以这里的字面值来自 `bonus_amount`（夹具里两者默认都是 `20.00`，
        第一行显式都写成 `30.00`，正是为了让「读的是哪一个」也能被这条判据区分）。
        """
        # taker-b 两行（30 + 20），taker-a 一行（20）⇒ 期望顺序是 a、b（不是插入序、不是金额序）。
        self._perf(
            self._order(),
            self.taker_b,
            temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
        )
        self._perf(self._order(), self.taker_b, temp_amount=Decimal("20.00"))
        self._perf(self._order(), self.taker_a, temp_amount=Decimal("20.00"))

        data = self._data(
            [
                row.id
                for row in Performance.objects.order_by("id")
            ]
        )

        self.assertEqual(
            [(owner["username"], owner["amount"]) for owner in data["owners"]],
            [("preview-taker-a", "20.00"), ("preview-taker-b", "50.00")],
        )

    def test_owner_name_is_the_nickname_while_username_is_kept(self):
        """⭐ 2026-10-04：「显示人统一成昵称」线——`owners[].name` = **昵称**，
        而老键 `username` **原样保留**（它是「账号」这个事实；前端按 `name ?? username` 回落）。

        **判别力**：夹具把 `first_name` 改成与登录账号**完全不同形**的合成昵称 ⇒
        「把 `username` 原样抄进 `name`」或「`name` 取错源」的实现都会让本条红。
        ⚠️ 排序口径**不动**（仍按 `username` 升序）——本条只有一项，排序由上面那条用例守。
        """
        self.taker_a.first_name = "测试昵称甲"
        self.taker_a.save(update_fields=["first_name"])
        self._perf(self._order(), self.taker_a)

        data = self._data([row.id for row in Performance.objects.order_by("id")])

        self.assertEqual(
            data["owners"],
            [
                {
                    "user_id": self.taker_a.pk,
                    "username": "preview-taker-a",
                    "name": "测试昵称甲",
                    "amount": "20.00",
                }
            ],
        )

    def test_an_ownerless_row_is_excluded_and_its_reason_is_reported(self):
        """⭐ **P1-2（2026-10-22）改准**：无归属人（`user` 为 NULL）的行**不进金额**。

        ⚠️ 原文（保留以存查）：「无归属人的行 ⇒ 单独一项 `{user_id: null, username: null}`、
        **排最后**……跳过它会让 `owners` 的金额之和与 `bonus_total` 对不上」。
        那条取舍的前提是「这行钱该算」——而结账侧对无归属人的行是 **`1006` 整批拒绝**
        （D-0069：没有归属人的绩效行必须结不了账）。既然 `/settle` 一分钱都不会结它，
        预览把它算进「实际需结总额」就是多算（P1-2 要消灭的形态）。
        ⛔ **不是「这行永远不能结」**：人工给它指定归属人之后，它就落进 `rows` 腿、
        预览与结账**同时**会算上它——本用例只钉「当前形态下两边都不算」。
        判据三半：不进 `bonus_total`、不出现在 `owners`、原因写进 `errors`（前端不渲染）。
        """
        with_owner = self._perf(self._order(), self.taker_a, temp_amount=Decimal("20.00"))
        # ⚠️ 两个金额都显式写：`bonus_total` / `owners` 读的是 `payable_net`（**生效**那一侧），
        #    只改 `temp_amount` 的话这条判据读到的仍是夹具默认的 `bonus_amount=20.00`。
        without_owner = self._perf(
            self._order(),
            None,
            temp_amount=Decimal("7.00"),
            bonus_amount=Decimal("7.00"),
        )

        data = self._data([with_owner.id, without_owner.id])

        self.assertEqual(data["count"], 2)          # 笔数仍是所选行（不改）
        self.assertEqual(data["bonus_total"], "20.00")
        self.assertEqual(
            data["owners"],
            [
                {
                    "user_id": self.taker_a.pk,
                    "username": "preview-taker-a",
                    # 同行数那份判据：没有昵称 ⇒ `name` 回落登录账号。
                    "name": "preview-taker-a",
                    "amount": "20.00",
                },
            ],
        )
        self.assertEqual(
            [entry["performance_id"] for entry in data["errors"]], [without_owner.id]
        )
        self.assertIn("还没有接单人", data["errors"][0]["message"])

    def test_qr_codes_only_include_people_with_a_stored_code(self):
        """`qr_codes` = **有收款码**的那些归属人；没码的人不出现（对标 `data: []` 就是这个形态）。

        ⚠️ 判据钉**值**（服务端签出来的 URL 含存储层给的路径）与**两行名字**
        （`username` 是登录账号、`name` 是显示名），⛔ 不钉「键存在吗」。
        """
        from apps.core.storage import get_storage

        # taker-a：设了显示名 + 上传过收款码。
        self.taker_a.first_name = "测试昵称甲"
        self.taker_a.save(update_fields=["first_name"])
        profile, _ = StaffProfile.objects.get_or_create(user=self.taker_a)
        key = f"test/users/{self.taker_a.pk}/wechat-qr.preview.png"
        get_storage().put_bytes(key, b"\x89PNG\r\n\x1a\n", content_type="image/png")
        profile.wechat_qr_key = key
        profile.save(update_fields=["wechat_qr_key"])

        # taker-b：有档案、**没有**码 ⇒ 不出现。
        StaffProfile.objects.get_or_create(user=self.taker_b)

        with_code = self._perf(self._order(), self.taker_a)
        without_code = self._perf(self._order(), self.taker_b)

        data = self._data([with_code.id, without_code.id])

        self.assertEqual(len(data["qr_codes"]), 1, data["qr_codes"])
        code = data["qr_codes"][0]
        self.assertEqual(code["user_id"], self.taker_a.pk)
        self.assertEqual(code["username"], "preview-taker-a")
        self.assertEqual(code["name"], "测试昵称甲")
        # 本机实现（`LocalStorage`）的签名 URL 指回我们自己的 blob 端点；生产是 COS 预签名，
        # 形状不同但同为短时效 —— 判据是「这个 key 真的进了 URL」，不是某种固定前缀。
        self.assertIn("/api/media/blob/", code["url"])
        self.assertIn(key, code["url"])

    def test_a_deduction_beyond_the_estimate_is_listed_in_errors_and_counted_as_zero(self):
        """扣点越界（`AmountError`）：该行按 `0.00` 计入，**并在 `errors` 里如实列出**。

        ⛔ 不许静默当 0（那是一笔不报错的错账，E-0010 同族），也⛔ 不许整页 500
        （财务诊断坏行的入口就是这张弹窗）。同批里合格的那一行照旧算。
        """
        broken = self._perf(
            self._order(),
            self.taker_a,
            temp_amount=Decimal("20.00"),
            bonus_amount=Decimal("20.00"),
            deduct_mode=DEDUCT_FIXED,
            deduct_value=Decimal("999.00"),
        )
        good = self._perf(
            self._order(), self.taker_b, temp_amount=Decimal("30.00"),
            bonus_amount=Decimal("30.00"),
        )

        data = self._data([broken.id, good.id])

        self.assertEqual(data["count"], 2)
        self.assertEqual(data["temp_total"], "50.00")
        # 坏行按 0.00 计入 ⇒ 总额只含合格那一行。
        self.assertEqual(data["bonus_total"], "30.00")
        self.assertEqual(
            [(owner["username"], owner["amount"]) for owner in data["owners"]],
            [("preview-taker-a", "0.00"), ("preview-taker-b", "30.00")],
        )
        self.assertEqual(len(data["errors"]), 1, data["errors"])
        error = data["errors"][0]
        self.assertEqual(error["performance_id"], broken.id)
        # 业务码取自异常本身（`deduct_amount` 用的是校验类码），文案原样透出。
        self.assertEqual(error["code"], 1000)
        self.assertIn("扣点金额不能超过预计绩效总计", error["message"])


class PreviewScopeTests(_PreviewFixture):
    """数据范围：越出自己游戏范围的 id **不进** `count`（与结算列表同一套卡点）。"""

    def _a_only(self):
        """只勾 `game_a` 的账号：`games=[]` 是**显式 opt-out**（夹具默认给全部游戏），
        权限的唯一来源是 `grant_games(..., game_a)`。"""
        client = make_logged_in_client(
            username="preview-a-only", groups=("预览甲",), nodes=(NODE_READ,), games=[]
        )
        grant_games(User.objects.get(username="preview-a-only"), self.game_a)
        return client

    def test_another_games_rows_are_not_previewed(self):
        """B 游戏的两行在 A 账号的预览里**一行都不出现**（金额也不参与两个总额）。

        ⚠️ 前置哨兵：那两行**真的在库里**（否则「看不见」与「压根没造出来」不可区分）；
        ⚠️ 同时断言管理员看得见它们（否则「把端点写成恒空」也会绿）。
        """
        client = self._a_only()
        mine = self._perf(
            self._order(game=self.game_a), self.taker_a, temp_amount=Decimal("20.00")
        )
        theirs = self._perf(
            self._order(game=self.game_b), self.taker_b, temp_amount=Decimal("30.00")
        )
        self.assertEqual(Performance.objects.filter(id__in=[mine.id, theirs.id]).count(), 2)

        data = self._data([mine.id, theirs.id], client=client)

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["temp_total"], "20.00")
        self.assertEqual(
            [owner["username"] for owner in data["owners"]], ["preview-taker-a"]
        )
        # 对照：超管（`scope_queryset` 短路）看得见两行 ⇒ 上面那 1 行真的是被收窄掉的。
        admin = make_logged_in_client(username="preview-admin", is_superuser=True)
        admin_data = self._data([mine.id, theirs.id], client=admin)
        self.assertEqual(admin_data["count"], 2)
        self.assertEqual(admin_data["temp_total"], "50.00")

    def test_a_profile_with_no_games_sees_nothing(self):
        """「有档案、零游戏」⇒ 空集（严格口径，业务方 2026-09-23 裁定）。"""
        client = make_logged_in_client(
            username="preview-empty", groups=("预览乙",), nodes=(NODE_READ,), games=[]
        )
        perf = self._perf(self._order(), self.taker_a)

        data = self._data([perf.id], client=client)

        self.assertEqual(data["count"], 0)
        self.assertEqual(data["bonus_total"], "0.00")


class PreviewReadOnlyTests(_PreviewFixture):
    """只读端点的**零副作用**：不写库、不写审计、不改 `updated_at`。"""

    def test_preview_changes_no_row_and_writes_no_audit(self):
        perf = self._perf(self._order(), self.taker_a)
        adjustment = self._adj(perf, "5.00")
        perf.refresh_from_db()
        updated_at_before = perf.updated_at
        audit_before = OperationLog.objects.count()
        legacy_before = AuditLog.objects.count()

        data = self._data([perf.id])

        self.assertEqual(data["count"], 1)          # 前置哨兵：端点真的干完了事
        perf.refresh_from_db()
        adjustment.refresh_from_db()
        self.assertEqual(perf.settle_status, "未结账")
        self.assertIsNone(perf.settled_at)
        self.assertIsNone(perf.settled_amount)
        # ⚠️ `updated_at` 是 `auto_now`：任何一次 `save()` 都会把它推走 ⇒ 这条能抓住
        #    「读路径顺手写了点什么」那种改动（不只看 `settle_status`）。
        self.assertEqual(perf.updated_at, updated_at_before)
        self.assertEqual(adjustment.settle_status, "未结账")
        self.assertIsNone(adjustment.settled_at)
        self.assertEqual(OperationLog.objects.count(), audit_before)
        self.assertEqual(AuditLog.objects.count(), legacy_before)   # 旧出口也一行不动


class PreviewPermissionTests(_PreviewFixture):
    """闸门 = `finance.payroll`（**读**节点；`settle` / `revoke` 的写节点一个字没动）。"""

    def test_a_holder_of_the_read_node_can_preview(self):
        client = make_logged_in_client(
            username="preview-reader", groups=("预览丙",), nodes=(NODE_READ,)
        )
        perf = self._perf(self._order(), self.taker_a)

        data = self._data([perf.id], client=client)

        self.assertEqual(data["count"], 1)

    def test_a_user_without_finance_payroll_is_denied(self):
        """**非管理员** + 持**别的**节点（不是没挂上节点）⇒ 403 且 `msg` 可辨。

        ⚠️ 显式给 `groups=` 才挂得上那个「别的节点」——否则这条会退化成
        「一个节点都没有也 403」那种**恒真**的判据（测不出「节点对不上」这一档）。
        """
        client = make_logged_in_client(
            username="preview-no-node",
            groups=("预览丁",),
            nodes=("finance.change_settlement_status",),
        )
        perf = self._perf(self._order(), self.taker_a)

        response = self._response([perf.id], client=client)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["msg"], "你没有查看结算预览的权限")

    def test_an_anonymous_request_is_401_not_403(self):
        """未认证 ⇒ **401**（分层：401 与 403 不能混）。

        它守的是 `SettlementPreviewView.permission_denied` 的**转调写法**：自己直接
        `raise PermissionDenied` 会把匿名请求也变成 403。
        """
        anonymous = Client(SERVER_NAME="tenant.test.com")

        response = anonymous.get(PREVIEW_URL, {"performance_ids": "1"})

        self.assertEqual(response.status_code, 401)


class PreviewInputValidationTests(_PreviewFixture):
    """请求来源的值必须过序列化器 ⇒ 200 + 业务码 `1000`，**不是** 500（E-0010）。"""

    def _assert_validation_failure(self, params):
        response = self.finance.get(PREVIEW_URL, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIsNone(body["data"], body)

    def test_missing_ids_key_is_a_validation_failure(self):
        self._assert_validation_failure({})

    def test_empty_ids_value_is_a_validation_failure(self):
        """`?performance_ids=`（空串）= 「没选」⇒ 由 required 报 `1000`（不是静默空结果）。"""
        self._assert_validation_failure({"performance_ids": ""})

    def test_non_integer_element_is_a_validation_failure(self):
        self._assert_validation_failure({"performance_ids": "abc"})

    def test_an_empty_element_between_commas_is_a_validation_failure(self):
        self._assert_validation_failure({"performance_ids": "1,,2"})

    def test_over_long_id_list_is_a_validation_failure(self):
        """超长列表**必须在入口拒绝**——否则它直进 `filter(id__in=…)`，
        撞 PostgreSQL 的绑定参数上限，变成 HTML 500（E-0010 的「超长列表」那一格）。"""
        self._assert_validation_failure(
            {"performance_ids": ",".join(str(one) for one in range(1, 1002))}
        )


#: 跨租户隔离的第二个租户（schema / 域名与其它测试模块**不重名**）。
DOMAIN_B = "sepop-preview-iso.test.local"
SCHEMA_B = "sepop_preview_iso"
ORDER_B_NO = "20261003000000009901"
USER_B = "preview-iso-user-b"


class CrossTenantPreviewTests(_PreviewFixture):
    """跨租户：B 的行在 A 的预览里**一行都不出现**（同一条请求在 B 里能看见，作对照）。

    ⚠️ **判据不用主键指认「另一个租户的那一行」**（AGENTS §5：两个 schema 的主键序列各自
    从起点开始）⇒ 这里用**构造保证**（把 B 那一行建成 A 最大值 + 1）+ 一条显式自证
    （`assertFalse(...exists())`），再用 `count` 与归属人作判据。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="预览隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)

        # A 侧：一行真实绩效（它同时是「A 的最大主键」的参照物）。
        self.perf_a = self._perf(self._order(), self.taker_a, temp_amount=Decimal("20.00"))
        max_a = (
            Performance.objects.order_by("-id").values_list("id", flat=True).first() or 0
        )

        try:
            with tenant_context(self.tenant_b):
                game_b = Game.objects.create(name="预览隔离游戏B")
                order_b = Order.objects.create(
                    game=game_b,
                    order_no=ORDER_B_NO,
                    status=OrderStatus.COMPLETED,
                    total_price=Decimal("88.00"),
                )
                user_b = User.objects.create_user(username=USER_B)
                # 显式主键 = A 的最大值 + 1 ⇒ 这一行在 A 的 schema 里**不存在**。
                self.perf_b = Performance.objects.create(
                    id=max_a + 1,
                    order=order_b,
                    user=user_b,
                    emp_type=EmpType.MAIN,
                    temp_amount=Decimal("30.00"),
                    bonus_amount=Decimal("30.00"),
                    review_status=ReviewStatus.APPROVED,
                    settle_status=SettleStatus.UNSETTLED,
                )
                self.admin_b = make_logged_in_client(
                    domain=DOMAIN_B, username="preview-iso-admin-b", is_superuser=True
                )
        finally:
            connection.set_tenant(self.tenant)

        # 夹具自证：B 的那一行在 A 的 schema 里确实不存在。
        self.assertFalse(
            Performance.objects.filter(id=self.perf_b.id).exists(),
            "构造失效：B 的绩效 id 在 A 里也存在，跨租户否定断言无意义",
        )

    def test_the_other_tenants_row_is_not_previewed(self):
        """A 的账号预览 B 的那一行 ⇒ `count=0`（钱一分都不算出来）。"""
        data = self._data([self.perf_b.id])          # A 侧客户端

        self.assertEqual(data["count"], 0)
        self.assertEqual(data["temp_total"], "0.00")
        self.assertEqual(data["owners"], [])

    def test_the_same_request_in_its_own_tenant_sees_the_row(self):
        """对照（两路独立证据的一半）：**同一条请求**在 B 里看得见那一行 ⇒
        上面那个 `0` 是租户边界造成的，不是「端点压根返回空」。"""
        response = self.admin_b.get(
            PREVIEW_URL, {"performance_ids": str(self.perf_b.id)}
        )
        body = response.json()

        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["count"], 1, body)
        self.assertEqual(body["data"]["temp_total"], "30.00")
        self.assertEqual(
            [owner["username"] for owner in body["data"]["owners"]], [USER_B]
        )

    def test_the_other_tenants_rows_do_not_leak_into_a_batch(self):
        """混选（A 的一行 + B 的一行）⇒ 只算 A 的那一行（不是报错，也不是把 B 的算进来）。"""
        data = self._data([self.perf_a.id, self.perf_b.id])

        self.assertEqual(data["count"], 1)
        self.assertEqual(data["temp_total"], "20.00")
        self.assertEqual(
            [owner["username"] for owner in data["owners"]], ["preview-taker-a"]
        )

