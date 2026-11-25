"""订单来源字典 ⇄ 订单 `source` 名字快照的写入语义（D4/D6/D7 的**唯一**出处）。 | 请求里传了什么 | 行为 | |---|---| | 只传 `order_source`（非空，且与当前 FK **不同**） | 写 FK，并把字典项的 `name` 覆盖写进 `source` | | 只传 `order_source`（非空，且与当前 FK **相同**） | 只动其他字段；FK 与 `source` 名字快照**都不动**（I-1） | | 两个都传 | **以 `order_source` 为准**（覆盖 `source`），`source` 的值被忽略 | | 只传 `source`（自由文本） | 写名字，并把 `order_source` 置为 `NULL`（维持 D7 的不变式） |
| 传 `order_source=null` | FK 置空；`source` 不动（快照语义） | ⚠️ **「同一个 FK ⇒ 不重拍名字」是既定裁定**：前端编辑态在订单已有可解析来源时**总是**回传同一个 `order_source`（`order-addorder.ts` 的编辑回填），于是旧实现「收到非空就覆盖」会把「只改个 `tag`」也变成一次快照重拍 —— 那与 D4/R7 的「改名不重写历史」直接矛盾。判别力由 `tests/test_order_source_intake.py` 的 `test_patch_same_order_source_after_rename_keeps_snapshot` 钉住。 ⚠️ **为什么是一个纯函数**：`create`（`OrderSerializer`）与 `PATCH`
（`OrderUpdateSerializer`）走的是**两个序列化器 + 两个视图方法**，规则若各写一遍，「改一处漏一处」正是 E-0010 的形状（spec §5.1 的「create 与 PATCH 同规则」）。两处都调用本函数，判别力由 `tests/test_order_source_intake.py` 的**两条**路径测试钉住。 ⚠️ 判据一律 `in validated_data`（「传没传」），**不是**真值（E-0021）： `source` 可以是空串（`include_blank`），把它当真值会让「传 `source=""`」被当成「没传」——于是 FK 与名字分叉，正是 D7 要防的。`order_source` 侧同理： `None` 与「没传」是**两种不同**的行为（前者保留名字、后者不动 FK）。 ⚠️ 不变式 D7：`order_source` 非空 ⟺ `source` 的名字来自字典。 `source` 只作文本快照，**不参与**来源归属判断（统计一律按 FK）。结论：本函数只负责「让两个字段不分叉」，**不**校验 `source` 的长度/空值（那是序列化器的既有判据，E-0010 禁止在这里另发明一套）。"""


def source_snapshot_overrides(validated_data, *, current_order_source_id=None):
    """把「序列化器校验后的入参」翻成 `serializer.save(**overrides)` 的覆盖字典。纯函数、无副作用。返回的键只可能是 `source` / `order_source` 两个之一： - 传了 `order_source` 且非空，**且 `current_order_source_id` 与它不同** ⇒ `{"source": <字典项名字>}`（FK 本身由序列化器写）；
    - 传了 `order_source` 且非空，但**与当前 FK 相同** ⇒ `{}` （I-1：前端编辑态总是回传同一个 id，此时**不重拍**旧的名字快照）； - 传了 `order_source=None` ⇒ `{}`（FK 由序列化器置空，`source` 名字快照不动）； - 只传了 `source` ⇒ `{"order_source": None}`（解除字典关联）；
    - 其余 ⇒ `{}`（两个字段都不动）。 `current_order_source_id` 是 **keyword-only、默认 `None`**：`create` 传默认值（新单的 FK 必然从 `None` 变成某个 id ⇒ 一定覆盖名字）；`partial_update` 必须传 `order.order_source_id`，否则「同一个 FK」这一支**静默失效**，退回旧的「编辑即重拍」（漏传不会抛错，所以两处调用点各有用例钉住）。"""
    if "order_source" in validated_data:
        order_source = validated_data["order_source"]
        if order_source is None:
            # FK 置空；`source` 名字快照**不动**（D5/R7 的快照语义）。
            return {}
        if "source" in validated_data:
            # 两个都传 ⇒ 以 FK 为准（spec §5.1；此时即使 FK 没变也覆盖名字，
            # 因为调用方**显式**给了自由文本，不能让它被静默忽略）。
            return {"source": order_source.name}
        if current_order_source_id == order_source.id:
            # I-1：同一个 FK ⇒ 保留旧快照名。前端编辑态总是回传同一个 id，
            # 这一支缺了就会把「改别的字段」变成一次改名重拍（D4/R7 的红线）。
            return {}
        return {"source": order_source.name}
    if "source" in validated_data:
        # 自由文本来源 ⇒ 解除字典关联，维持 D7 的不变式（避免「FK 指向 A、名字随手写」）。
        return {"order_source": None}
    return {}
