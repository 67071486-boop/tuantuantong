"""纯函数：令牌、URL 闸门、字段白名单、来源名截断（spec §9-S3/S7/S9、§10.1、A11）。

⚠️ 断言钉「**字面值**」，不钉被测常量（`AGENTS.md` §5-G / E-0024）：白名单那两条如果写成
`set(payload) == set(PEER_ORDER_FIELDS)`，那是**自指断言**——常量本身被改坏（例如往里加一个
`sales_user_id`）测试照样绿。所以下面另有一份逐字写死的 `EXPECTED_PEER_ORDER_FIELDS` 当外部契约值。
"""
import json
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

from apps.business.models import Order
from apps.business.peer_sync import (
    DERIVED_ORDER_FIELDS,
    PEER_ORDER_FIELDS,
    hash_token,
    new_token,
    normalize_base_url,
    order_payload,
    source_name_for,
    token_matches,
)

#: 外发字段的**外部契约值**（spec §10.1，游戏那一项按**裁决 R21** 改过）
#: ——逐字写死，⛔ 不要改成 `set(PEER_ORDER_FIELDS)`。
EXPECTED_PEER_ORDER_FIELDS = {
    "order_no",
    "status",
    # P3（业务方裁定）：发单价随状态同步一起外发。
    "hall_price",
    "remark",
    # ⭐ R21：传**名字**、⛔ 不传 `game_id`（跨租户 id 永远解析得到、却几乎总是另一款游戏）。
    "game_name",
    "server_id",
    "project_id",
    # 评审建议（R21 一并考虑）：自由文本的项目名随载荷过来。
    "project_text",
    "customer_phone",
    "customer_qq",
    "content",
    "source_order_no",
}


class TokenTests(SimpleTestCase):
    def test_hash_is_stable_and_compare_is_constant_time_helper(self):
        raw = new_token()
        self.assertNotEqual(raw, new_token())          # 每次都新
        self.assertEqual(hash_token(raw), hash_token(raw))
        self.assertTrue(token_matches(raw, hash_token(raw)))
        self.assertFalse(token_matches(raw + "x", hash_token(raw)))

    def test_hash_is_not_the_raw_value(self):
        raw = "abc123"
        self.assertNotIn(raw, hash_token(raw))


class BaseUrlTests(SimpleTestCase):
    def test_accepts_http_and_https_and_strips_trailing_slash(self):
        self.assertEqual(
            normalize_base_url("https://peer-a.test.invalid/"),
            "https://peer-a.test.invalid",
        )
        self.assertEqual(
            normalize_base_url("HTTP://Peer-A.test.invalid"),
            "http://peer-a.test.invalid",
        )

    def test_rejects_non_http_scheme(self):
        for bad in ("ftp://peer-a.test.invalid", "javascript:alert(1)", "peer-a.test.invalid"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_rejects_loopback_and_private_hosts(self):
        """⛔ SSRF 闸门（spec §9-S9）：这是两个外部可达端点上的**出站面**。"""
        for bad in (
            "http://127.0.0.1:8000",
            "http://localhost",
            "http://10.0.0.5",
            "http://192.168.1.1",
            "http://172.16.0.1",
            "http://169.254.169.254",   # 云元数据地址
            "http://[::1]",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_rejects_trailing_dot_forms_of_blocked_hosts(self):
        """⛔ 尾点写法不得绕过黑名单（E-0261 / R3-P1）。

        `localhost.` / `127.0.0.1.` 是合法 DNS 写法（FQDN 的根域显式写法），libc
        解析结果与不带尾点**完全相同**（都指向本机）——但 `urlsplit().hostname`
        原样带回尾点，黑名单的精确比较就漏了（实测旧行为：`http://localhost./` 放行）。
        """
        for bad in ("http://localhost./", "http://127.0.0.1./"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_trailing_dot_on_ordinary_domain_still_accepted(self):
        """对照（E-0261 的「不伤合法输入」面）：普通域名带尾点照旧**放行**，
        且归一化成不带尾点的同一基址（两种写法汇成同一个比较键）。"""
        self.assertEqual(
            normalize_base_url("https://peer-a.test.invalid./"),
            "https://peer-a.test.invalid",
        )

    def test_keeps_ipv6_literal_brackets(self):
        """IPv6 字面量重建时必须补回方括号，否则存进库的串不是合法 URL。"""
        self.assertEqual(
            normalize_base_url("http://[2606:4700:4700::1111]"),
            "http://[2606:4700:4700::1111]",
        )

    def test_rejects_numeric_encoded_hosts(self):
        """⛔ 数字编码形态（十进制 / 短写法 / 十六进制）不得绕过 SSRF 闸门。

        实测绕过：`2130706433` = 127.0.0.1、`127.1` = 127.0.0.1、`0x7f000001` = 127.0.0.1。
        同族可指向 `169.254.169.254`（云元数据）⇒ `peer_base_url` 由下家提供、上家据此出站，必须拒。
        """
        for bad in ("http://2130706433", "http://127.1", "http://0x7f000001"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_rejects_mixed_hex_label_hosts(self):
        """⛔ **混合十六进制标签**也属数字编码类别：`ipaddress` 不认、libc 认。

        实测（本机 Python 3.13 / macOS libc）：
        `socket.inet_aton("0x7f.0.0.1")` 成功、`socket.getaddrinfo("0x7f.0.0.1", 80)` 返回
        `['127.0.0.1']`，而 `ipaddress.ip_address("0x7f.0.0.1")` 抛 `ValueError`
        ⇒ 只挡「全十进制」与「单标签纯十六进制」两条正则时，这些串会当普通域名放行，
        出站 HTTP 会**真的打到回环地址**。
        """
        for bad in (
            "http://0x7f.0.0.1",          # = 127.0.0.1
            "http://127.0.0.0x1",         # = 127.0.0.1
            "http://0x7f.0x0.0x0.0x1",    # = 127.0.0.1
            "http://0x7f.1",              # = 127.0.0.1
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_rejects_digits_and_dots_only_hosts(self):
        """全数字 + 点的串永远不是合法域名（DNS 没有全数字顶层域名）⇒ 拒。

        ⚠️ 这两个 `inet_aton` 与 `getaddrinfo` 都不认（实测）——它们只由「纯数字/点」那条判据挡住；
        ⛔ 别把这条判据当成「libc 会解析」那一类的重复实现（那不是同一集合）。
        """
        for bad in ("http://256.1.1.1", "http://1.2.3.4.5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_rejects_cgnat_and_multicast_hosts(self):
        """spec §9-S9 的「内网地址」**包含** RFC 6598 共享段与组播段（控制方裁定 R9）。

        实测 Python 3.13：`100.64.0.1` 的 `is_private` / `is_reserved` / `is_loopback` /
        `is_link_local` / `is_multicast` **全为 False**（`is_global` 也为 False）——
        Tailscale 节点与运营商 NAT 内部地址都住在这里；`224.0.0.1` / `[ff02::1]` 只被
        `is_multicast` 认出来。两条都必须显式判，否则闸门放行。
        """
        for bad in (
            "http://100.64.0.1",          # RFC 6598 段首
            "http://100.127.255.254",     # RFC 6598 段尾
            "http://224.0.0.1",           # IPv4 组播
            "http://239.255.255.250",     # SSDP 组播
            "http://[ff02::1]",           # IPv6 链路本地全节点组播
            "http://[ff05::1:3]",         # IPv6 站点本地组播
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_rejects_path_query_fragment_and_userinfo(self):
        """只收「域名（含端口）」这一个基址：带路径 / 查询 / 锚点 / 用户密码一律**报错**。

        ⛔ 静默丢掉这些部分 = 「没报错、结果是错的」：Task 5/7 会往这个基址拼同步端点路径，
        而 `http://host/api/` 归一化成 `http://host` 会把请求指到根路径（实测旧行为如此）。
        """
        for bad in (
            "http://peer-a.test.invalid/api/",       # 旧行为：静默改写成根
            "http://peer-a.test.invalid?x=1",
            "http://peer-a.test.invalid#frag",
            "http://user:pw@peer-a.test.invalid",    # 旧行为：静默丢凭据
            "http://user@peer-a.test.invalid",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_base_url(bad)

    def test_still_accepts_bare_host_and_port(self):
        """收紧之后这三种写法必须照旧接受（裸域名 / 裸斜杠 / 指定端口）。"""
        self.assertEqual(
            normalize_base_url("https://peer-a.test.invalid"),
            "https://peer-a.test.invalid",
        )
        self.assertEqual(
            normalize_base_url("https://peer-a.test.invalid/"),
            "https://peer-a.test.invalid",
        )
        self.assertEqual(
            normalize_base_url("https://peer-a.test.invalid:8443"),
            "https://peer-a.test.invalid:8443",
        )

    def test_keeps_port_zero_and_normal_port(self):
        """端口判据用 `is not None`（`AGENTS.md` §5-E / E-0021）：`0` 不得被真值判断丢掉。"""
        self.assertEqual(
            normalize_base_url("http://peer-a.test.invalid:0"),
            "http://peer-a.test.invalid:0",
        )
        self.assertEqual(
            normalize_base_url("http://peer-a.test.invalid:8443"),
            "http://peer-a.test.invalid:8443",
        )

    def test_malformed_url_messages_are_our_own_chinese(self):
        """闸门抛的文案**必须自有中文**：⛔ `urlsplit()` / `.port` 的英文原文不得漏出去。

        实测（本机 Python 3.13）三条原文，评审 finding 1 点名：
        `http://[::1` ⇒ "Invalid IPv6 URL"、
        `http://example.com:abc` ⇒ "Port could not be cast to integer value as 'abc'"、
        `http://example.com:99999` ⇒ "Port out of range 0-65535"。

        ⚠️ 断言钉**字面值**、不钉 `peer_sync.BASE_URL_MALFORMED_MSG`（`AGENTS.md` §5-G / E-0024）：
        钉常量的话，把常量改成英文这句测试照样绿。
        """
        expected = "网址格式不对（域名或端口写法有误）"
        for bad in (
            "http://[::1",
            "http://example.com:abc",
            "http://example.com:99999",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    normalize_base_url(bad)
                message = str(ctx.exception)
                self.assertEqual(message, expected)
                self.assertTrue(
                    any("\u4e00" <= ch <= "\u9fff" for ch in message),
                    "闸门文案里没有汉字：%r" % (message,),
                )
                for stdlib_phrasing in ("Invalid IPv6 URL", "Port"):
                    self.assertNotIn(stdlib_phrasing, message)


class OrderPayloadTests(SimpleTestCase):
    def test_payload_only_contains_whitelisted_fields(self):
        order = SimpleNamespace(
            order_no="A1", status="IN_PROGRESS", total_price=100,
            remark="r", game=SimpleNamespace(name="测试游戏甲"),
            server_id=2, project_id=None, project_text="上家项目名",
            customer_phone="", customer_qq="", content="客户原始需求",
            source_order_no="S1", sales_user_id=9, created_by="someone",
        )
        payload = order_payload(order)
        # ① 常量本身必须等于外部契约值（这条能因「往白名单里加字段」而红）
        self.assertEqual(set(PEER_ORDER_FIELDS), EXPECTED_PEER_ORDER_FIELDS)
        # ② 载荷的键集合同样是那一个字面值（⛔ 不是 `set(PEER_ORDER_FIELDS)`：恒真）
        self.assertEqual(set(payload), EXPECTED_PEER_ORDER_FIELDS)
        # ③ 值确实取自订单（含 `None` 也照样带过去，不静默丢字段）
        self.assertEqual(payload["order_no"], "A1")
        self.assertIsNone(payload["project_id"])
        # ④ ⭐ R21：`game_name` 取的是 **FK 上的名字**（`order.game.name`），⛔ 不是任何 id。
        self.assertEqual(payload["game_name"], "测试游戏甲")
        self.assertNotIn("game_id", payload)
        self.assertEqual(payload["project_text"], "上家项目名")
        # ⛔ 租户内数据不外传（spec §10.1）
        for leaked in ("sales_user_id", "created_by"):
            self.assertNotIn(leaked, payload)

    def test_payload_is_json_safe_for_a_real_decimal_price(self):
        """`total_price` 是 `DecimalField` ⇒ 载荷必须**已经**可 JSON 化（Task 6 实测的 RED）。

        ⛔ 上一条用例喂的是 `int`（`SimpleNamespace(total_price=100)`）⇒ 抓不到这个形状；
        而真实 `Order.hall_price` 是 `Decimal`，原样放进 `PeerSyncEvent.payload`
        （`JSONField`）会让 psycopg 的内置 `json.dumps` 抛
        `TypeError: Object of type Decimal is not JSON serializable`；对端出站 HTTP 同理。
        """
        payload = order_payload(
            SimpleNamespace(
                order_no="A2", status="UNASSIGNED", total_price=Decimal("500.00"),
                hall_price=Decimal("100.00"),
                remark="", game=SimpleNamespace(name="测试游戏甲"),
                server_id=None, project_id=None, project_text="",
                customer_phone="", customer_qq="", content="", source_order_no="",
            )
        )
        # 整类可序列化（不是「这一次碰巧没有 Decimal」）
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        # 金额**值不变、只换 JSON 表示**（spec §10.3：不换算、不四舍五入）
        self.assertEqual(payload["hall_price"], "100.00")
        self.assertEqual(Decimal(payload["hall_price"]), Decimal("100.00"))
        # ⭐ 业务方裁定：客户价（= 上家接单价）**不再下发**。
        self.assertNotIn("total_price", payload)


class SourceNameTests(SimpleTestCase):
    def test_truncates_to_ten_chars(self):
        """A11：`OrderSource.name` 只有 10 字，绑定名可到 20 字。"""
        self.assertEqual(source_name_for("测试工作室甲"), "测试工作室甲")
        self.assertEqual(len(source_name_for("一二三四五六七八九十十一")), 10)
        self.assertEqual(source_name_for("一二三四五六七八九十十一"), "一二三四五六七八九十")


class PeerOrderFieldsModelGuardTests(TestCase):
    """白名单必须与 `Order` 的**真实列**对齐——唯一一条要碰模型的用例，故单列一个类。

    `order_payload` 用 `getattr(order, field, None)`：列一旦改名，白名单会**静默**发 `null`
    而不是报错，而本文件其它用例喂的是 `SimpleNamespace` ⇒ 抓不到这种漂移
    （`AGENTS.md` §5-G / E-0024 的同族形状：断言要钉外部契约，不能只钉自己造的对象）。
    ⚠️ 裁决 R21 之后白名单里多了一个**派生项** `game_name`（`Order` 上没有这一列）⇒
    下面三条夹具：① 排除派生项后再查"剩下的名字都在 `Order` 上"；② 用显式的
    `DERIVED_ORDER_FIELDS` 当外部契约，钉住"非列项**恰好**是这些"；③ 派生项的取值路径
    逐段走模型元数据（⛔ 不拿常量的字面值自比）。
    """

    def _accessible_names(self) -> set:
        accessible = set()
        for field in Order._meta.get_fields():
            accessible.add(field.name)
            # `server_id` / `project_id` 是外键的 `attname`（字段名是 `server` / `project`）
            attname = getattr(field, "attname", None)
            if attname:
                accessible.add(attname)
        return accessible

    def test_whitelist_names_all_exist_on_the_order_model(self):
        accessible = self._accessible_names()
        missing = sorted(set(PEER_ORDER_FIELDS) - set(DERIVED_ORDER_FIELDS) - accessible)
        self.assertEqual(
            missing,
            [],
            f"PEER_ORDER_FIELDS 里这些名字在 Order 上不存在（列被改名了？）：{missing}",
        )

    def test_non_column_whitelist_entries_are_exactly_the_declared_derived_ones(self):
        """派生项**恰好**是白名单里那些不是 `Order` 列的名字——多一个少一个都红。

        判别力：把 `game_name` 从 `DERIVED_ORDER_FIELDS` 里删掉（而不删白名单）⇒ 上面那条
        会红；反过来，白名单里加一个不存在的列也会被这条抓住（它既不是列、也不在派生表里）。
        """
        not_a_column = set(PEER_ORDER_FIELDS) - self._accessible_names()
        self.assertEqual(sorted(not_a_column), sorted(DERIVED_ORDER_FIELDS))

    def test_derived_paths_point_at_real_model_attributes(self):
        """派生项的取值路径（`order.game.name`）必须真的存在（⛔ 不是写一句注释）。

        判据走**模型元数据**：逐段 `_meta.get_field(...)`，不存在就直接
        `FieldDoesNotExist` 让用例红。
        """
        self.assertTrue(DERIVED_ORDER_FIELDS, "裁决 R21 之后 `game_name` 必须挂在派生表里")
        for name, path in DERIVED_ORDER_FIELDS.items():
            segments = path.split(".")
            self.assertEqual(segments[0], "order", f"{name} 的路径必须以 order. 开头")
            self.assertTrue(segments[1:], f"{name} 的路径没有取值段")
            model = Order
            for segment in segments[1:]:
                field = model._meta.get_field(segment)          # 不存在 ⇒ FieldDoesNotExist
                model = getattr(field, "related_model", None) or model


class InboundBatchCapTests(SimpleTestCase):
    """⭐ C4（L-2，2026-10-25 审计批）：入站 `events` 硬上限的**定值钉子**。

    上限 = `max(2 × 发送端单批, 100)`；发送端现状一次 HTTP 只投一条
    （`peer_worker.deliver_one` 的既有形状）⇒ 上限就是 **100**。
    ⚠️ 期望值是**字面量**（E-0024，同 `EXPECTED_PEER_ORDER_FIELDS` 的口径）：
    把上限改小（放宽了 DoS 面）或改大（放松了对端的分批要求）都要这条当场红。
    """

    def test_the_batch_cap_is_one_hundred(self):
        from apps.business.peer_sync import PEER_EVENTS_MAX_BATCH

        self.assertEqual(PEER_EVENTS_MAX_BATCH, 100)

    def test_the_sender_per_push_evidence_constant_is_one(self):
        """「发送端单批 = 1」的证据常量：发送端改批量形状时，这里逼着人回头核上限。"""
        from apps.business.peer_sync import PEER_EVENTS_MAX_PER_PUSH

        self.assertEqual(PEER_EVENTS_MAX_PER_PUSH, 1)
