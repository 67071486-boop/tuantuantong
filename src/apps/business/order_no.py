"""订单号生成（D10）。形状：YYYYMMDDHHMMSS + 3 位随机 + 3 位计数器 = 20 位纯数字。 ⚠️ **唯一性保证级别——不要误以为更强**：
- **租户内**：由 `Order.order_no` 的唯一索引**确定保证** - **跨租户**：秒级时间戳 + 3 位随机 + 3 位计数器 => **概率唯一，不是构造保证**。
只有「同一秒 + 同一计数器值 + 同一随机数」同时发生才会跨租户撞号，概率极低但不为零，且**没有任何数据库约束能拦**。 **不要把这个号当作跨租户的稳定标识符使用**（平台层索引、跨租户对账）。"""
import random

from django.db import connection
from django.utils import timezone

COUNTER_SEQUENCE = "order_no_counter_seq"
COUNTER_MOD = 1000
RANDOM_MOD = 1000


def generate_order_no(now=None):
    """生成一个订单号。计数器取自租户 schema 内的序列，不依赖已有订单数据。"""
    moment = now or timezone.localtime()
    stamp = moment.strftime("%Y%m%d%H%M%S")
    rand = random.randrange(RANDOM_MOD)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT nextval('{COUNTER_SEQUENCE}')")
        counter = cursor.fetchone()[0] % COUNTER_MOD
    return f"{stamp}{rand:03d}{counter:03d}"
