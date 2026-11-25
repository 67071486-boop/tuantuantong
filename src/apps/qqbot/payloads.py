"""大厅推送的文本拼装（纯函数，可单测）。

## 格式是**钉死的对外契约**（业务方拍板 D-0265）

```
【新单上架】{project_text，空回落 project.name（照 order_push._project_name 口径）}
游戏：{order.game.name}
系统：{system_name}
米 {hall_price：Decimal 去尾零规范化——330.00→"330"、330.50→"330.5"；None→"未设置"}
时间：{localtime 的 %Y-%m-%d %H:%M}
订单号：{order.order_no}
```

markdown 版 = 同样内容但**首行包 `**…**`**。

⚠️ 价格行**禁止** `¥` / `元` / 千分位——「米」是这条线业务方定的行话，数字原样。
⛔ 改这里的任何一行字都要先过业务方（`tests/test_qqbot_push_pipeline.py` 的钉值断言会红）。
"""
from __future__ import annotations

from decimal import Decimal

from django.utils import timezone


def _project_name(order) -> str:
    """`Order.project_text` 是权威值（D9）；为空才回落到 FK 的名字。

    ⚠️ 照抄 `apps/business/order_push._project_name` 的口径（含无项目时的「—」），
    ⛔ 不在那里改这里不改——两份口径必须同步。
    """
    text = (order.project_text or "").strip()
    if text:
        return text
    if order.project_id:
        return order.project.name
    return "—"


def price_text(value) -> str:
    """大厅价文案：去尾零的十进制（`330.00`→`"330"`、`330.50`→`"330.5"`）；缺价 ⇒「未设置」。

    ⚠️ `Decimal.normalize()` 会把 `330.00` 变成 `3.3E+2`（科学计数）——必须再
    `format(_, "f")` 回定点，否则「米 3.3E+2」会直接发进群。
    """
    if value is None:
        return "未设置"
    return format(Decimal(value).normalize(), "f")


def build_hall_texts(order, system_name: str, now=None) -> tuple[str, str]:
    """产出大厅推送的 `(纯文本, markdown)`；`now` 可注入（测试钉值用）。

    ⚠️ **格式**是 D-0265 钉的对外契约（六行、行首标签逐字）——⛔ 不得改动；
    **「订单号」的值**如今按 D-0301 换成回退口径：来源单号非空取它、
    空回退内置号（该值口径的裁定登记在 D-0301，D-0265 历史条目不改）。
    """
    moment = timezone.localtime(now or timezone.now())
    lines = [
        f"【新单上架】{_project_name(order)}",
        f"游戏：{order.game.name}",
        f"系统：{system_name}",
        f"米 {price_text(order.hall_price)}",
        f"时间：{moment.strftime('%Y-%m-%d %H:%M')}",
        f"订单号：{order.source_order_no or order.order_no}",
    ]
    text = "\n".join(lines)
    markdown = "\n".join([f"**{lines[0]}**", *lines[1:]])
    return text, markdown
