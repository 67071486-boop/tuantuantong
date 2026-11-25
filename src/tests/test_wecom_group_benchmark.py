"""可手动运行的客户群包含匹配规模验证；只生成合成数据。"""
import os
from time import perf_counter
from unittest import skipUnless

from django_tenants.test.cases import TenantTestCase

from apps.wecom.models import WecomGroupChat


@skipUnless(os.environ.get("WECOM_GROUP_BENCHMARK") == "1", "仅显式运行性能验证")
class WecomGroupBenchmark(TenantTestCase):
    def test_150k_group_contains(self):
        WecomGroupChat.objects.bulk_create((
            WecomGroupChat(chat_id=f"group-{i}", name=f"合成客户群订单{i:019d}", details={
                "owner": "合成员工", "member_list": [
                    {"userid": f"synthetic-{member}", "name": f"合成成员{member}", "type": 1}
                    for member in range(8)
                ],
            })
            for i in range(150000)
        ), batch_size=1000)
        qs = WecomGroupChat.objects.filter(dismissed=False, name__contains="0000000000000123456").order_by("name", "chat_id")
        plan = qs.explain(analyze=True, buffers=True)
        times = []
        for _ in range(5):
            started = perf_counter()
            rows = list(qs.all())
            times.append((perf_counter() - started) * 1000)
        self.assertEqual(len(rows), 1)
        print(f"WECOM_GROUP_BENCHMARK rows=150000 warm_ms={[round(t, 2) for t in times]}\n{plan}")
