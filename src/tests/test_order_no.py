import re
from datetime import datetime

from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.business.order_no import generate_order_no
from apps.platform.provisioning import provision_tenant

SHAPE = re.compile(r"^\d{20}$")


def _counter(no):
    """订单号的低 3 位是计数器。"""
    return int(no[17:20])


class OrderNoTests(TenantTestCase):
    def test_shape_is_20_digits(self):
        no = generate_order_no(now=datetime(2026, 9, 20, 9, 22, 41))
        self.assertRegex(no, SHAPE)
        self.assertEqual(len(no), 20)

    def test_timestamp_prefix_matches_given_time(self):
        no = generate_order_no(now=datetime(2026, 9, 20, 9, 22, 41))
        self.assertEqual(no[:14], "20260920092241")

    def test_suffix_low_three_is_a_counter(self):
        """低 3 位是计数器（不是随机）——连续生成时应当递增（模 1000）。"""
        lows = [int(generate_order_no()[17:20]) for _ in range(5)]
        for a, b in zip(lows, lows[1:]):
            self.assertEqual(b, (a + 1) % 1000)

    def test_two_hundred_consecutive_calls_never_collide(self):
        self.assertEqual(len({generate_order_no() for _ in range(200)}), 200)

    def test_same_second_still_unique_thanks_to_counter(self):
        """同一秒内生成 50 个也不重复——这正是计数器存在的理由。"""
        fixed = datetime(2026, 9, 20, 9, 22, 41)
        self.assertEqual(len({generate_order_no(now=fixed) for _ in range(50)}), 50)


class OrderNoCounterIsolationTests(TenantTestCase):
    """计数器住在**租户 schema 内**（D10 的实现前提），两个租户各自独立。

    这条原先只靠一个已删除的探针支撑。判据不用 `count()`：直接看计数器值——
    若两个租户共用一个序列，B 的第一个计数器就会紧接 A 的最后一个。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_a, _ = provision_tenant(
            schema_name="counter_a", name="计数器租户 A", domain="countera.test.local"
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name="counter_b", name="计数器租户 B", domain="counterb.test.local"
        )

    def test_counter_is_independent_per_tenant_schema(self):
        with tenant_context(self.tenant_a):
            a_lows = [_counter(generate_order_no()) for _ in range(3)]
        with tenant_context(self.tenant_b):
            # B 连发 5 个（与 A 的个数**不同**，两个方向才都能判）
            b_lows = [_counter(generate_order_no()) for _ in range(5)]
        with tenant_context(self.tenant_a):
            a_next = _counter(generate_order_no())

        # 先自证两批号真的读到了序列：各自都是连续递增的计数器
        self.assertEqual(a_lows, [(a_lows[0] + i) % 1000 for i in range(3)])
        self.assertEqual(b_lows, [(b_lows[0] + i) % 1000 for i in range(5)])

        # 核心：B 的第一值**不是** A 的下一个（共用序列时它会等于 A 的下一个）
        self.assertNotEqual(b_lows[0], (a_lows[-1] + 1) % 1000)
        # 反向：在 B 里连发 5 个之后，A 的下一个仍是 A 自己的下一个（没有被 B 推进）
        self.assertEqual(a_next, (a_lows[-1] + 1) % 1000)
