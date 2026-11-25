"""D-0251：实名核验**按人限流**的判据网。

口径出处：`docs/DECISIONS.md` 的 D-0251 行（⛔ 台账不改，这里只引用号）：

- ① 桶键只含**服务端来源**的值（租户 schema + user pk；M17 教训：⛔ 不用请求方可控值当桶键）；
- ② **本地格式校验失败不计数**（没出网）、**PASS 幂等短路不计数**（零出网零额度）；
- ③ **检查与递增都在厂商调用之前** ⇒ 被限流的那一次零出网、零扣减、零流水；
  不锁定——窗口滚过自动恢复；
- ④ 阈值 **3 / 窗口 86400s**（业务方：「每人每天最多 3 次」，取代 D-0251 原口径
  5 次/小时），settings 可配（`REALNAME_VERIFY_RATE_LIMIT` /
  `REALNAME_VERIFY_RATE_WINDOW_SECONDS`）；
- ⑤ 超额 ⇒ 业务码 `1029` +「尝试过于频繁，请稍后再试」。

厂商一律打桩（照 `tests/test_realname_verify_api.py` 的 `_VendorStub`，⛔ 绝不打真网，
`test_00` 是打桩自证）；cache = 默认 locmem，**每条用例 setUp/addCleanup 各清一次**
（locmem 跨用例存活，事务回滚回不去）。
"""
from __future__ import annotations

import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django_tenants.test.cases import TenantTestCase

from apps.platform.models import RealNameGrant, RealNamePackage
from apps.realname import vendor
from apps.realname.models import RealNameCertRecord
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_realname_verify_api import (
    FAKE_REQUEST_ID,
    ID_CARD_MALE,
    NAME_OK,
    TENANT_DOMAIN,
    _REAL_HTTPX_CLIENT,
    _VendorStub,
    enable_setting,
    make_grant,
    make_package,
    used_of,
)

User = get_user_model()

# ── 业务码与文案的**字面值**（E-0024：从被测模块 import 会把"码被改坏/文案被改坏"藏起来）──
OK = 0
VALIDATION = 1000
RATE_LIMITED = 1029

#: 业务口径阈值（业务方：每人每天最多 3 次）。夹具额度给 50 ⇒ 本网循环里的这一档
#: 只受限流桶约束，不会被 1026 余量不足抢先。
DEFAULT_LIMIT = 3

#: GB 11643 校验位（合成号生成用；权重/位表是公开国标，⛔ 不是拿被测实现当 oracle——
#: 下面用已知夹具 `110101199003070011` 交叉核对过）。
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CODES = "10X98765432"


def synth_id_card(seed: int) -> str:
    """拼一条**校验位合法**的合成身份证号（不指向任何真人，E-0077；同号不同 seed 各不同）。

    ⚠️ 为什么限流网要用**不同**的号打满桶：PASS 过的 (user, 号码) 再提交会命中**幂等短路**
    （③步，D-0251 ②：零出网也不计数）——同号打不满桶。「错误信息反复提交」那一支
    （失败不短路）另有 `test_02` 用同号钉死；本网的成功档用例必须每笔**真的出网**。
    """
    body = f"11010119900307{seed:03d}"
    return body + _ID_CHECK_CODES[sum(w * int(c) for w, c in zip(_ID_WEIGHTS, body)) % 11]


class _RateLimitCase(TenantTestCase):
    """公共夹具：登录用户 + 假厂商 + 启用的实名设置 + 足量额度 + **清过的 cache**。

    ⚠️ 额度给 50：本网的判据是「限流桶」，⛔ 让「租户余量不足（1026）」抢先开花
    把读数搅混——用例 01 明确断言 `used == 3`（扣减只来自前 3 次真实核验）。
    """

    def setUp(self):
        super().setUp()
        cache.clear()  # 桶必须每条用例从零起（⛔ 不依赖别的用例的残留）
        self.addCleanup(cache.clear)
        connection.set_tenant(self.tenant)  # 挡住上一条用例残留的 schema 上下文
        self.schema = self.tenant.schema_name
        enable_setting(self.schema)
        self.grant = make_grant(self.tenant, granted=50, used=0)
        make_package(total_quota=50, used_quota=0)
        self.client_user = make_logged_in_client(domain=TENANT_DOMAIN, username="rate-user-a")
        self.user = User.objects.get(username="rate-user-a")
        self.stub = _VendorStub()
        patcher = mock.patch.object(vendor.httpx, "Client", self.stub.factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 默认厂商回「一致」（result=1）；要别的档的用例自己改。
        self.stub.json_reply(
            {
                "msg": "查询成功",
                "requestid": FAKE_REQUEST_ID,
                "code": 200,
                "data": {"result": "1", "message": "一致"},
            }
        )

    # ── 便捷动作 ─────────────────────────────────────────────────────────────
    def post_verify(self, client=None, *, id_card_no=None):
        payload = {"name": NAME_OK, "id_card_no": id_card_no or ID_CARD_MALE}
        return (client or self.client_user).post(
            "/api/auth/realname/verify/", payload, content_type="application/json"
        )

    def records(self):
        return RealNameCertRecord.objects.all()


class RateLimitTests(_RateLimitCase):
    def test_00_the_vendor_stub_is_actually_wired(self):
        """⭐ 桩接没接上本身要有判据（E-0029）——没有它，"桩没接上"会以打真网的形式出现。"""
        self.assertIsNot(
            vendor.httpx.Client, _REAL_HTTPX_CLIENT, "桩没接上：被测代码会去打真网"
        )
        body = self.post_verify().json()
        self.assertEqual(body["code"], OK)
        self.assertEqual(self.stub.call_count, 1, "桩接上了但没被走到")

    def test_01_fourth_submission_is_rate_limited_before_touching_the_vendor(self):
        """前 3 次出网照常（成功档、**每笔不同号**）；第 **4** 次 ⇒ `1029` + 那句人话，
        且**零出网**、零扣减、零流水（业务方：每人每天最多 3 次）。

        ⚠️ 判据刻意打三样「没发生」上（与 `test_realname_verify_api` 同一口径）：
        出网计数（负向用例的唯一合法 `call_count` 断言）、`grant.used`、流水行数。
        ⚠️ 为什么每笔用不同的号：PASS 过的 (user, 号码) 再提交会命中幂等短路（不计数），
        同号打不满桶——见 `synth_id_card` 的 docstring。
        """
        for i in range(DEFAULT_LIMIT):
            body = self.post_verify(id_card_no=synth_id_card(i + 1)).json()
            self.assertEqual(body["code"], OK, f"第 {i + 1} 次不该被限流")
            self.assertEqual(body["data"]["result"], "pass")
        self.assertEqual(self.stub.call_count, DEFAULT_LIMIT, "前 3 次每笔都真的出了网")
        self.assertEqual(used_of(self.grant), DEFAULT_LIMIT)

        res = self.post_verify(id_card_no=synth_id_card(DEFAULT_LIMIT + 1))
        body = res.json()
        self.assertEqual(body["code"], RATE_LIMITED)
        self.assertIn("过于频繁", body["msg"])
        self.assertIn("稍后再试", body["msg"])
        self.assertEqual(self.stub.call_count, DEFAULT_LIMIT, "被限流的那一次不许出网")
        self.assertEqual(used_of(self.grant), DEFAULT_LIMIT, "被限流的那一次不许扣次数")
        self.assertEqual(self.records().count(), DEFAULT_LIMIT, "被限流的那一次不许写流水")

    def test_02_fail_results_exhaust_the_bucket_too(self):
        """前 3 次**失败**档（厂商「不一致」）同样耗尽桶——「用错误信息浪费接口」正是被拦形状。"""
        self.stub.json_reply(
            {
                "msg": "查询成功",
                "requestid": FAKE_REQUEST_ID,
                "code": 200,
                "data": {"result": "2", "message": "不一致"},
            }
        )
        for _ in range(DEFAULT_LIMIT):
            body = self.post_verify().json()
            self.assertEqual(body["data"]["result"], "fail")
        res = self.post_verify()
        self.assertEqual(res.json()["code"], RATE_LIMITED)
        self.assertEqual(self.stub.call_count, DEFAULT_LIMIT, "第 4 次不许再出网")

    def test_03_invalid_format_never_counts(self):
        """本地格式错（②步 `1000`）打满**也不计数**；随后一次合法提交照常出网成功。

        ⚠️ 判别力：若把计数挪到格式校验**之前**，这 10 次 `1000` 会把桶打满 ⇒
        最后那条合法提交回 `1029` ⇒ 本用例红。
        """
        for _ in range(2 * DEFAULT_LIMIT):
            res = self.post_verify(id_card_no="123")  # 长度/校验位都不对 ⇒ 本地就拦
            self.assertEqual(res.json()["code"], VALIDATION)
        self.assertEqual(self.stub.call_count, 0, "格式错根本不该出网")
        body = self.post_verify(id_card_no=synth_id_card(99)).json()
        self.assertEqual(body["code"], OK, "格式错被计了数 ⇒ 合法提交被误限流")

    def test_04_pass_short_circuit_does_not_count(self):
        """PASS 短路（③步）重验不计数：首验 + 10 次重验 ⇒ 一次都不限流、厂商只调 1 次。

        ⚠️ 判别力：若把计数挪到短路判定**之前**，10 次重验会把桶打满 ⇒ 第 4 次重验
        回 `1029` ⇒ 本用例红（E-0252 修后"重验同号"正是**日常**路径，误限流最伤）。
        """
        body = self.post_verify().json()
        self.assertEqual(body["data"]["result"], "pass")
        for i in range(10):
            res = self.post_verify()
            self.assertEqual(res.json()["code"], OK, f"第 {i + 1} 次短路重验被误限流")
        self.assertEqual(self.stub.call_count, 1, "短路重验不许出网")
        self.assertEqual(used_of(self.grant), 1, "短路重验不许扣次数")

    def test_05_buckets_are_per_user(self):
        """用户 A 打满 ⇒ A 被限；用户 B 的桶**独立**（键含 user pk，D-0251 ①）。

        ⚠️ 两人的提交各用**不同的号**（B 用 A 没提交过的号）：同号重验会命中幂等短路，
        那样「B 没被限」可能由短路凑出来而不是桶独立。
        """
        for i in range(DEFAULT_LIMIT):
            self.assertEqual(
                self.post_verify(id_card_no=synth_id_card(i + 1)).json()["code"], OK
            )
        self.assertEqual(
            self.post_verify(id_card_no=synth_id_card(DEFAULT_LIMIT + 1)).json()["code"],
            RATE_LIMITED,
        )

        client_b = make_logged_in_client(domain=TENANT_DOMAIN, username="rate-user-b")
        body = self.post_verify(client=client_b, id_card_no=synth_id_card(50)).json()
        self.assertEqual(body["code"], OK, "B 被 A 的计数误伤 ⇒ 桶键没按人分")

    @override_settings(REALNAME_VERIFY_RATE_LIMIT=1)
    def test_06_threshold_is_configurable_via_settings(self):
        """`REALNAME_VERIFY_RATE_LIMIT` 在**调用期**读（override 生效）：阈值 1 ⇒ 第 2 次被限。"""
        self.assertEqual(self.post_verify(id_card_no=synth_id_card(1)).json()["code"], OK)
        res = self.post_verify(id_card_no=synth_id_card(2))
        self.assertEqual(
            res.json()["code"], RATE_LIMITED, "settings 阈值没生效（import 期读死了？）"
        )

    def test_07_window_rollover_recovers_automatically(self):
        """「不锁定、窗口滚过自动恢复」：窗口 1 秒 ⇒ 打满 → 等 1.2 秒 → 恢复（= 跨天恢复的代理）。

        ⚠️ 默认窗口是 **86400 秒（一天）**，真睡一天不现实 ⇒ 把窗口 override 成 1 秒，
        读数等价于「跨过窗口就恢复」；默认值本身由 `test_08` 钉字面值。
        ⚠️ 代价如实登记：本用例**睡 ~1.2 秒**，买的是「TTL 真的会被设上」的端到端读数
        （locmem 的 TTL 没有可 fakable 的口子；`REALNAME_VERIFY_RATE_WINDOW_SECONDS`
        的可配性由本条顺带钉住——读不到这个 setting 时 TTL 会退回 86400s，恢复读数红）。
        """
        with override_settings(REALNAME_VERIFY_RATE_WINDOW_SECONDS=1):
            for i in range(DEFAULT_LIMIT):
                self.assertEqual(
                    self.post_verify(id_card_no=synth_id_card(i + 1)).json()["code"], OK
                )
            self.assertEqual(
                self.post_verify(id_card_no=synth_id_card(DEFAULT_LIMIT + 1)).json()["code"],
                RATE_LIMITED,
            )
            time.sleep(1.2)
            self.assertEqual(
                self.post_verify(id_card_no=synth_id_card(60)).json()["code"],
                OK,
                "窗口滚过没恢复 ⇒ TTL 没被设上",
            )

    def test_08_defaults_are_three_per_day(self):
        """默认口径 = **3 次 / 86400 秒**（业务方 2026-10-21 拍板，取代 D-0251 的 5 次/小时）。

        ⚠️ 为什么单独钉字面值：本网其余用例按测试文件里的 `DEFAULT_LIMIT = 3` 打循环，
        它们能证明「3 次放行、第 4 次拒」，但**证不了代码默认值是 3**（若实现退回 5，
        循环第 4 次仍放行 ⇒ 那些用例会红，但窗口默认值退回 3600 秒没有别的用例看）。
        这里直接读模块常量，红/绿一眼可读。
        """
        from apps.realname import rate_limit

        self.assertEqual(rate_limit.DEFAULT_LIMIT, 3, "业务方 2026-10-21：每人每天最多 3 次")
        self.assertEqual(rate_limit.DEFAULT_WINDOW_SECONDS, 86400, "窗口应为一天（86400 秒）")

    def test_09_rejected_attempts_also_count(self):
        """**被 1029 拒掉的那一次也 +1**（沿用 D-0251 的计数语义：count = 尝试数，不是放行数）。

        ⭐ 判别设计：同一窗口内先把阈值 override 成 2 打满（2 次放行、第 3 次被拒），
        再把阈值抬到 3 提交第 4 次——
        - 若被拒的那次**计了数** ⇒ 桶里是 3 + 1 = 4 > 3 ⇒ 仍然 `1029`；
        - 若被拒的**不计** ⇒ 桶里是 3，不 > 3 ⇒ 会放行（本用例红）。
        ⚠️ 只用一个 override 阈值看不出差别（3 > 2 恒成立），必须换阈值才问得出「记没记」。
        """
        with override_settings(REALNAME_VERIFY_RATE_LIMIT=2):
            self.assertEqual(self.post_verify(id_card_no=synth_id_card(1)).json()["code"], OK)
            self.assertEqual(self.post_verify(id_card_no=synth_id_card(2)).json()["code"], OK)
            self.assertEqual(
                self.post_verify(id_card_no=synth_id_card(3)).json()["code"],
                RATE_LIMITED,
                "起点自证失败：阈值 2 时第 3 次没被拒",
            )
        with override_settings(REALNAME_VERIFY_RATE_LIMIT=3):
            self.assertEqual(
                self.post_verify(id_card_no=synth_id_card(4)).json()["code"],
                RATE_LIMITED,
                "被拒的那次没计数 ⇒ 阈值放宽到 3 时第 4 次被放行了",
            )
        self.assertEqual(self.stub.call_count, 2, "两次被拒的提交都不许出网")
