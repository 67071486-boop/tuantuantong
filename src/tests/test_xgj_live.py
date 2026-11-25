"""闲管家**真实上游**联调（**opt-in、只读**）。

## 怎么跑

```
cd src && POSTGRES_DB=tuantuantong_xgj_live \\
  UV_CACHE_DIR=<repo>/.tools/cache \\
  XGJ_APP_KEY=<你的 AppKey> XGJ_APP_SECRET=<你的 AppSecret> \\
  <repo>/.tools/bin/uv run python manage.py test tests.test_xgj_live
```

## 四条纪律（改这个文件之前先读）

1. **只读**：只调三个**查询**接口——`user/authorize/list`、`order/list`、`order/detail`。
   ⛔ **绝不**调任何写接口（商品上下架 / 改价 / 发货 / 退款一律不碰）。
2. **凭据只从环境变量读、不落盘**：`XGJ_APP_KEY` / `XGJ_APP_SECRET`。
   ⚠️ 它们**只是这个测试的输入**——`config/settings.py` **不读**它们：
   闲管家的凭据是**按租户**存在 `XgjShopBinding` 表里的（spec §3-D1），
   没有任何平台级 AppKey/AppSecret。别把这两个名字当成部署配置写进 `.env`。
3. **基址用 `settings.XGJ_API_BASE`**（默认就是线上正式环境），本文件**不强改**它：
   本机若把它配成沙箱，联调打的就是沙箱——那是有意的配置，不是"意外打了生产"。
4. **默认跳过**（没设凭据时），且跳过理由是**逐字可见**的（`skipUnless` 的 reason）——
   这不是"静默跳过"：它在测试汇总里显示为 `skipped`，并且本文件的 docstring 说明了它要什么。
   ⚠️ 代价如实登记：**CI 里它永远跳过**，所以「真实上游能不能调通」这件事**不在**默认门禁里，
   要靠人手跑一次（spec §12-7）。

## 为什么值得有一条（值在哪）

本仓的单元测试全部把 HTTP 打桩掉，所以「**签名对不对**」这件事**默认门禁证明不了**——
签错的表现是生产上每一个请求都回 `100001 签名错误`，而本地全绿。
这条联调用**真实凭据**走一遍**我们自己的客户端**（不是另写一份签名），
2026-09-22 首次跑通即证明：签名实现正确、`order/detail` 的 `data` 形状已实测（spec §11-T1 关闭）。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business.models import XgjShopBinding
from apps.business.xgj import DEFAULT_BASE_URL, call
from tests.p5_auth_helpers import make_logged_in_client

#: 联调凭据（**只给这个测试用**；见文件头纪律 2）。
APP_KEY = os.environ.get("XGJ_APP_KEY", "")
APP_SECRET = os.environ.get("XGJ_APP_SECRET", "")
SKIP_REASON = (
    "未设置 XGJ_APP_KEY / XGJ_APP_SECRET ⇒ 跳过闲管家真实上游联调（只读）。"
    "怎么跑见本文件 docstring"
)

URL = "/api/business/xgj-bindings/"
ORDER_DETAIL_URL = "/api/business/xgj/order-detail/"


@skipUnless(APP_KEY and APP_SECRET, SKIP_REASON)
class XgjLiveReadOnlyTests(TenantTestCase):
    def setUp(self):
        self.client = make_logged_in_client(username="xgj-live")
        self.binding = XgjShopBinding.objects.create(
            shop_name="联调（只读）", app_key=APP_KEY, app_secret=APP_SECRET
        )

    def test_test_connection_lists_the_real_shops(self):
        """端到端：我方端点 → 我方客户端 → 真实上游。签名错的话这里会回 `1016`。"""
        body = self.client.post(f"{URL}{self.binding.pk}/test-connection/").json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        shops = body["data"]["shops"]
        # 不断言具体店名 / 数量（那是租户自己的数据，会变）；只断言「上游确实回了一批店铺」
        self.assertGreater(len(shops), 0, "上游回了 0 家店——是应用没授权店铺，还是调用被拒？")
        # 文档给了这个字段；拿它验证「返回的是店铺行」而不是别的东西
        self.assertIn("authorize_id", shops[0])

    def test_order_detail_reads_a_real_order(self):
        """用真实订单号走一遍端到端，并钉住录单映射要用的字段。

        ⚠️ 订单号是**现查**的（`order/list`，同样是只读），不硬编码：
        硬编码一个订单号会在它被清理/过期后变成一条"莫名失败"的测试。
        """
        now = int(time.time())
        data = call(
            "/api/open/order/list",
            {"page_no": 1, "page_size": 1, "update_time": [now - 30 * 86400, now]},
            app_key=APP_KEY,
            app_secret=APP_SECRET,
            # ⚠️ 与走端点那条腿用**同一个**基址来源（第二轮原来这里没传，
            #    于是本机把 XGJ_API_BASE 配成沙箱时只有另一条腿生效）。
            base_url=getattr(settings, "XGJ_API_BASE", "") or DEFAULT_BASE_URL,
        )
        rows = data.get("list") or []
        if not rows:
            self.skipTest("近 30 天这个账号下没有订单，无法验证 order/detail（不是失败）")
        order_no = str(rows[0]["order_no"])

        body = self.client.post(
            ORDER_DETAIL_URL,
            {"binding": self.binding.pk, "order_no": order_no},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body.get("msg"))
        detail = body["data"]["detail"]

        # ① **T1 的实测结果**：`data` 就是订单对象本身（不是 `{list:[…]}`）——所以端点能直接给出 `order_no`
        self.assertEqual(str(detail["order_no"]), order_no)
        # ② 录单映射要用的字段（spec §6 的映射表）。⚠️ 金额取的是 **pay_amount（实付）**
                #    （业务方定）——实测它与 `total_amount` 真的会不同，所以两个都断言存在
        self.assertIn("pay_amount", detail)
        self.assertIn("total_amount", detail)
        self.assertIn("buyer_nick", detail)
        self.assertIsInstance(detail["goods"], dict, "goods 实测是对象；变成数组会让 remark 静默为空")
        self.assertIn("title", detail["goods"])
        # ③ 上游会带**文档里没有**的字段（实测 `xyb_seller_amount`）⇒ 端点必须原样透传、不能裁剪
        self.assertTrue(
            set(detail) - {"order_no"},
            "detail 里除了 order_no 之外应当还有字段（否则原样透传这条实现错了）",
        )


class XgjLiveReadOnlyDeclarationTests(SimpleTestCase):
    """「这个联调文件是只读的」——**静态**自证，与真实上游无关，所以不需要建库。

    ⚠️ 它**不是**安全边界（没有它也不会有人去调写接口）：它的作用是把"只读"这条**声明**
    变成一个会红的判据，而不是留在注释里靠人记得。

    ⚠️ **判据的边界（别把它读强了）**：它匹配的是**枚举出来的 11 个写端点后缀**，
    而且要求前缀与后缀在源码里**连续出现**。所以：
    - 拼串（`"/api/open/" + "order/ship"`）调用的、或经别的模块间接调用的写接口，
      **抓不到**；
    - 闲管家将来新增的写端点，**也不在**这张枚举表里。
    换句话说：它防的是"有人在**本文件**里手写一个新写调用"这一种情况。
    """

    def test_no_write_endpoint_path_appears_in_this_file(self):
        source = Path(__file__).read_text(encoding="utf-8")
        # 闲管家的写接口端点（spec §2.2 的清单：product/* 的写动作、order 的 ship/cancel/改价、
        # trade/refund 的 operate/*）。**只写后缀**，前缀在这里拼——拼出来的完整路径
        # 不出现在源码里（**含注释**），扫描才有判别力。
        # ⚠️ 这里正是自指陷阱的现场：第一版把完整路径写进清单 ⇒ 扫描命中清单自己；
        #    改完之后仍然红，因为**讲解这条坑的注释里**又把完整路径抄了一遍
        #    （同族：E-0059 / E-0024）。
        write_suffixes = (
            "product/create", "product/batchCreate", "product/edit", "product/edit/stock",
            "product/publish", "product/downShelf", "product/delete",
            "order/ship", "order/cancel", "order/modify/price",
            "trade/refund/operate/",
        )
        write_paths = [f"/api/open/{suffix}" for suffix in write_suffixes]
        hits = [path for path in write_paths if path in source]
        self.assertEqual(hits, [], f"联调文件里出现了写接口路径：{hits}")
