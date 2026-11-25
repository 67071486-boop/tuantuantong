"""**读路径登记网**：把「新增了一个 `Order` 读点却忘了登记」变红（spec §8-T4 第 26/27 条）。
判据三条（**都用 AST，不用正则**——正则会被注释 / docstring 骗过，而 `game_scope.py` 与 `settlement_amount.py` 的模块 docstring 里确实有 `Order.objects.…` 示例）：
1. `ORDER_READ_SITES`：**可执行**的 `Order.objects` 调用点（文件 + 作用域名 + **重数**）全集，硬编码字面量、**不从源码派生**（E-0024）；未登记 / 已不存在的落点都红。
2. `ORDER_JOIN_SITES`：**可执行**的 `order__` 字段路径全集，同上；⛔ 字符串字面量里的 `order__`（`Q(**{f"order__{x}": v})` 间接构造）本网看得见（收 `ast.Constant`），**看不见**的是「字段路径由变量拼出来、且拼出的段不在同一字符串常量里」（spec §10 第 1 条已登记的残留风险）。
3. `ALL_OBJECTS_SITES`：`Order.all_objects` / `*.all_objects` / `Count("all_objects", …)` 的使用点**恰好**等于白名单（多一处 / 少一处都红）。
口径与「计划」的偏差（执行期实测，逐条可复核）：**作用域名是限定的**（`Class.method` / 顶层 `func`）——同文件里 `SettlementViewSet.get_queryset` 与 `UnattributedView.get_queryset` 是两个落点（本仓真实存在这一对），裸方法名会把它们并成一个；**带重数**——同作用域第二个落点也算一次 ⇒ 删同函数第二处也红（计划「恰好 27 条」是 frozenset unique 口语，与落点数不是同一口径）；**`ALL_OBJECTS_SITES` 条数不要写死**（写死过一次就假一次），以本表字面量与本次运行报出的数字为准——`Game.all_objects` → `AttributeError`、`Count("all_objects")` → `FieldError`（反向关系只有 `orders`），`config_views.py` 的 `models.Count("orders", distinct=True)` 本来就是物理计数（SQL JOIN 不经过 manager、已软删行会被算进去），不进白名单；**A7 / C27**：`selectors.publisher_options()` / `selectors.taker_options()`（3 处）两条在被断言集合里。
本网自己的断言必须能失败（§5-G）：三个集合**各跑过一次**「把它弄红」的变异证明（原始输出见 T2 报告）——给 `claim_order` 加一处 `Order.objects`、给 `AdjustmentListView.create` 加一处 `order__status`、把 `GameSerializer` 的 `all_objects` 换回 `objects` ⇒ 三次都红在**本文件自己的断言消息**上。"""
import ast
import collections
from pathlib import Path

from django.test import SimpleTestCase

APPS_DIR = Path(__file__).resolve().parent.parent / "apps"

#: ① 可执行的 `Order.objects` 落点白名单（spec §2.2.2-A 组，**含写侧 `.update()`**）；键 = (相对 `apps/` 的路径, 作用域名)，值 = 该作用域里的**落点数**。
#: ⛔ 每一条都是**读该处**得到的、不是扫描器输出粘贴：`search_orders`（A1，`selectors.py:91`，读）/ `search_hall`（A2，`selectors.py:153`，读）/ `OrderViewSet.copy`（A3，`order_views.py` 的 `Order.objects.create`，**写**：新单 `deleted_at` 天然 NULL）/ `claim_order`（A4，`claim.py:165`，读）/ `apply_transition`（A5，`order_status.py:263`，读，加锁）/ `RefundView.refund`（A6，`refund_views.py:120`，读）/ `reverse_previous_round_refund`（A-w2，`refunds.py`，**写**）/ `UnattributedView.get_queryset`（`settlement_views.py:264`「待归属」清单的 `Order.objects.filter(status=COMPLETED)`——⚠️ **计划 A 组漏了这一处**：它把同文件的 `settlement_views.py:267` 记成 C 组的 join，实测它是 `Order.objects` **直读** ⇒ 由默认 manager 覆盖，只需就地注释）。
#: ⛔ **A-w1（`_write_partial_refund`）已整条移除（D-0312）**：累计退款额改**绝对值**后写入是「行锁内读改写」——`row = lock_order_row(order)`（该 `Order.objects.select_for_update` 落点由 `order_writes.lock_order_row` 那格登记）+ `row.save(update_fields=["refund_fee"])`，`save()` **不经过** `Order.objects` ⇒ 按本网口径这一条必须整条移除（多留一条会让「已不存在的读点」那一半断言红）。⛔ **A-w3（`on_order_refunded`）已删除（D-0181）**：原 `Order.objects.filter(pk=order.pk).update(refund_fee=gross)` 一句随「改退单不再自动填充退款额」裁定连同 `OrderRefund(kind=FULL)` 事件行整体删除，本函数**不再有任何 `Order.objects` 落点** ⇒ 登记必须整条移除（同上，多留即红）；⚠️ 退单时打手留痕 / 客服冲减链 / 鸡蛋铺撤下三件**都还在**（`ORDER_JOIN_SITES` 的 C25/C26 与 `ORDER_READ_SITES` 的 C23/C24 逐条未动）。
ORDER_READ_SITES: collections.Counter[tuple[str, str]] = collections.Counter({
    ("business/online_payroll.py", "create_batch"): 1,  # 库内锁住仍可见的待付款订单。
    ("wecom/external_pay.py", "match_payment"): 1,
    ("wecom/external_pay_views.py", "ExternalPayIssuesView.get"): 1,
    ("wecom/external_pay_views.py", "ExternalPayStatusView.get"): 2,
    ("wecom/notification_views.py", "WecomNotificationRecordsView.get"): 1,
    ("wecom/notifications.py", "queue_payment_mismatch"): 1,
    ("wecom/notifications.py", "retry_expired_payment_mismatch"): 1,
    # 退款提醒定时任务只在可行租户上下文读订单，四处都走排除软删的默认 manager。
    # 候选集合排除两类大厅影子单；判来源单号重号的子查询沿用订单聊天口径，让仍有效的同号影子单也能造成歧义。
    ("wecom/refund_notices.py", "_order_candidates"): 2,
    # 核验已选订单对每位候选收件人的详情权限节点与游戏范围。
    ("wecom/refund_notices.py", "_recipients"): 1,
    # 发送前按持久绑定主键加锁重读，重跑排除影子单的匹配才进入权限核验。
    ("wecom/refund_notices.py", "_send"): 1,
    # ⭐ D-0269（打手 App 钱包线）：解冻前查「这个人还有没有进行中的单」。只读、按 MAIN 绩效行的 user 收窄，⛔ 不接受请求参数里的租户线索。
    #    ⚠️ 落点登记在实际执行查询的 `_has_active_work`（本格 = A0；本网按**作用域**收，⛔ 不登记间接调用它的 `request_release`）。
    ("accounts/wallet.py", "_has_active_work"): 1,
    ("business/wxpusher_events.py", "queue_order_notice"): 1,  # 重读可见订单的通知展示字段。
    ("business/selectors.py", "search_orders"): 1,
    ("business/selectors.py", "search_hall"): 1,
    ("business/selectors.py", "split_is_balance_anchor_expression"): 1,
    ("business/split_group.py", "is_balance_anchor"): 1,
    ("business/order_writes.py", "change_price"): 1,
    ("business/order_writes.py", "_update_order_fields_with_price"): 1,
    ("business/api/order_views.py", "OrderViewSet.batch_price"): 1,
    ("business/api/order_views.py", "OrderViewSet.copy"): 1,
    # `claim_order` 共 **2** 处落点：① `Order.published_at`（发单时间）的条件更新（`filter(pk=…, published_at__isnull=True).update(...)`）——必须走 `Order.objects`（`WHERE` 里的 `published_at__isnull=True` 保证「只写第一次」，⛔ 不能换 `order.save()`：手上那份 `order` 是函数开头读的陈旧副本，无条件 `save` 会把并发写进去的值擦掉）；默认 manager 的软删语义对 `.update()` **没有约束力**——登记口径是「所有可执行 `Order.objects` 落点」，不是「读路径要加软删谓词」。
    #    ② 首次碰钱包之前的行锁（`select_for_update().filter(pk=order_id).first()`，统一「先锁 Order、再锁钱包」；位置在 `check_grab_limits` 之后——那是并发测试的栅栏同步点，挪到它前面会让抢单线程串行、把栅栏卡死）。它是**读点**（`SELECT … FOR UPDATE`）。判据：把任一句删掉，本表会以「已不存在的读点」报红。
    ("business/claim.py", "claim_order"): 2,
    # 第一次接手写发单时间在 `order_time_fields._mark`（条件更新，只写第一次）。
    # 和抢单那处写的是同一个事实，不能各写一遍。删掉这一格，本表会报「已不存在的读点」或漏登记。
    ("business/order_time_fields.py", "_mark"): 1,
    # ⭐ D-0247：客服接发单统计 / 经营报表的**唯一**订单基集入口。本模块所有统计都从 `_base_orders` 出发（1 处 `Order.objects`），`support_stats` / `business_report` 内不再各写一句（否则各算一个落点）。
    #    默认 manager 已排除软删（`OrderManager.get_queryset`），所以这里不再叠 `deleted_at__isnull=True`——本网按「所有可执行 `Order.objects` 落点」收，不是要求每个读点都手写软删谓词。
    ("business/report_stats.py", "_base_orders"): 1,
    # ⭐ 订单管理页「同步收款」（计划 `2026-10-26-receipt-sync-button.md` §5-M8）：两个落点，都是**任务取单面**（不是面向人/大屏的展示面）：
    #    ① `candidates_qs` = 唯一取单口径（端点 `count()` 与执行循环同源）——**必须**排除隐藏单，已登记到下面的 `HALL_SHADOW_EXCLUDED_SITES`；② `run_xgj_receipt_sync` 循环里每笔**重读**（`Order.objects.filter(pk=…).first()`）——判「扫单期间被软删（`None` ⇒ 跳过计数）/ 被推送线改成已结款 / 已退单」。⚠️ 默认 manager 已带软删排除 ⇒ 两处都不需要再手写 `deleted_at__isnull=True`。
    ("business/xgj_receipt_sync.py", "candidates_qs"): 1,
    ("business/xgj_receipt_sync.py", "run_xgj_receipt_sync"): 1,
    # ⭐ 上家收款同步（D-0359）：三个落点，都是**任务取单面 / 写前复核 / 计数**：① `_jidanpu_fallback_jobs` = ①式兜底候选（Order 直读，默认 manager 已带软删排除；⚠️ **刻意不排除隐藏单**——计划 §2.2：①式/peer 通道不按 hall FK 排除，peer 镜像转正单本身可能带 hall FK，是正主单，候选按「来源链接」收不按卡收）；
    #    ② `_fresh_order` = 两通道共用的写前重读（软删 ⇒ None ⇒ vanished 跳过）；③ `_bump_notfound` = 放弃计数的原子 UPDATE（**写**，条件更新那一族）。
    ("business/upstream_receipt_sync.py", "_jidanpu_fallback_jobs"): 1,
    ("business/upstream_receipt_sync.py", "_fresh_order"): 1,
    ("business/upstream_receipt_sync.py", "_bump_notfound"): 1,
    ("business/order_status.py", "apply_transition"): 1,
    ("business/api/refund_views.py", "RefundView.refund"): 1,
    # ⛔ A-w1（`_write_partial_refund`）已整条移除：绝对值口径下 `refund_fee` 是行锁内的读改写（`row.save(update_fields=…)`），不再有 `Order.objects.filter(pk=…).update(refund_fee=…)` 落点。
    #    ⚠️ 行锁本身仍是一个 `Order.objects` 落点，登记在 `("business/order_writes.py", "lock_order_row"): 1` 那一格。
    ("business/refunds.py", "reverse_previous_round_refund"): 1,
    # ⭐ `fix_missing_taker_performance` 保住原完单时间（生产事故修复）：`_repair` 在整单流程走完后按**单列**写回原完单时间（`Order.objects.filter(pk=…).update(completed_at=original_completed_at)`）。
    #    为什么必须走 `QuerySet.update()`：⛔ 不用 `order.save()`（整行覆盖，会把这一轮写下的 user / 金额 / 状态等列冲掉）；这一列本来就只该写一个值（与 A-w2 同形：写）。判据：删掉这一句，`tests.test_fix_missing_taker_performance` 的「原完单时间必须保住」那条当场变红。
    ("business/management/commands/fix_missing_taker_performance.py", "Command._repair"): 1,
    # ⛔ A-w3（`on_order_refunded`）已整条移除（D-0181）：见上面注释块。
    ("business/api/settlement_views.py", "UnattributedView.get_queryset"): 1,
    # A7（T8-b 落）：候选名单端点的**发单人**一半（`selectors.publisher_options()`）——底集 `Order.objects.filter(hall_publisher__isnull=False)` 走**默认 manager**
    # ⇒ 已软删订单天然不贡献候选人（spec §5.6 / 执行期数出来的 1 处）。
    ("business/selectors.py", "publisher_options"): 1,
    # T2b 落：一次性写租户的**订单夹具**（`seed_e2e_write_orders.py`）。⛔ 三处都**有意**走默认 manager（不需要 `all_objects`）：夹具只在**刚被 `reset_e2e_write` 重建**的 `e2e_write` 上跑，那时库里没有软删行；而"软删单不该被 upsert 命中、也不该算进收尾校验"正是默认 manager 的语义。
    #    ⚠️ 已知边界：若有人在**没重建**的租户上单独重跑本命令、而审计已把某笔夹具订单软删，`get_or_create` 会撞 `order_no` 唯一索引**响亮失败**（不是静默造重复单）——这是有意的。落点：`_upsert_fixture_orders` 2 处（`get_or_create(order_no=…)` 与回读摘要的 `get(order_no=…)`）；`_assert_fixture_orders_are_usable` 1 处（收尾校验按单号重读数据库，⛔ 不信内存对象）。
    ("platform/management/commands/seed_e2e_write_orders.py", "_upsert_fixture_orders"): 2,
    ("platform/management/commands/seed_e2e_write_orders.py", "_assert_fixture_orders_are_usable"): 1,
    # ⭐ D-0153：图片存储线 **20 张上限**的两处 `Order.objects` 落点——它们是 `select_for_update()` 的**行锁**（把「计数 + 落行」串起来，防并发超发），⛔ **不是列表读路径**：锁的是**单个订单行**、且这两处随后都还有各自的业务判据（租户前缀 / 状态 / 张数），所以**不涉** `order__deleted_at__isnull` 那条软删排除口径（本表其余读点才涉）。
    #    ⚠️ 踩坑记录：这两处是**整套**跑才红的（分模块跑看不见）——同族 `E-0091` 第 ③ 条。⚠️ 作用域名是模块级领域函数 `issue_screenshot_ticket` / `claim_screenshot`（签发 / 认领的判据与库写入抽成同模块领域函数；对外开放平台的取票 / 认领端点调的就是它们）。
    #    「签发侧」与「认领侧」两条锁各自承重（前者防超发、后者防群二维码淘汰竞态）、重数各 **1**。
    ("business/api/screenshot_views.py", "issue_screenshot_ticket"): 1,
    ("business/api/screenshot_views.py", "claim_screenshot"): 1,
    # A-w4：`apply_events` 的 `Order.objects.create(...)`——与 A3（`OrderViewSet.copy`）同形：**写**，新行的 `deleted_at` 天然为 NULL ⇒ 默认 manager 的软删过滤对 create 无意义（不构成读路径）。
    # ⚠️ 它**不是**「读点漏加 `deleted_at__isnull=True`」那一类，只是本网按「所有可执行 `Order.objects` 落点」的口径收它。⚠️ 更新那一支读的是 `link.order`（FK 反向属性），不是一句 `Order.objects`，故仍只有 1 处。
    ("business/peer_inbound.py", "apply_upstream_events"): 1,
    ("business/settlement.py", "settle_performances"): 1,
    ("business/peer_inbound.py", "apply_events"): 1,
    # ⭐ D-0303（团团通大厅镜像）：上行支的 `hall.claim`、截图支、状态支三处取单全部收进**唯一解析口** `_local_order_for_peer_ref`——判据是「内置号 OR 来源单号」（跨系统一律用来源单号做身份），三处若各写一句就是三份口径（E-0021 同族）。
    #    ⛔ 走**默认 manager**（已软删单取不到 ⇒ 与「不在大厅」同档拒绝，fail-closed；⛔ 不改成 `all_objects`：已删的单不能再被指派 / 接单）。⚠️ 同一个 helper 里还有一处 `Order.all_objects`（只给撤单复核的 `include_deleted=True` 分支用）⇒ 它在 ③ 的 `ALL_OBJECTS_SITES` 里单独登记。
    ("business/peer_inbound.py", "_local_order_for_peer_ref"): 1,
    # ⭐ D-0303 同族（指派收单）：收单锚第二半——按锚出的主键取本地正式单（`Order.objects.filter(pk=…).first()`）＝ **读**、走**默认 manager**：已软删 ⇒ 取不到即放弃该单（fail-closed，照接收口同一口径），⛔ 不改 `all_objects`（已删单不该被收单动作碰到）。
    ("business/peer_inbound.py", "_collect_assigned_hall_orders"): 1,
    # ⭐ D-0303（团团通大厅镜像）：铺底命令按「当前全部 WAITING_GRAB」取单（`Order.objects.filter(status=WAITING_GRAB)`，spec §2.3 逐字）——**读**，
    #    走默认 manager ⇒ 已软删单天然不在候选里（在厅的单被删了就不该再推给下家）。
    ("business/management/commands/push_peer_hall_snapshot.py", "Command._push_schema"): 1,
    # ⭐ 手工补数据的进度图命令按单号取那一行（`Order.objects.filter(order_no=…).first()`）：**读**、走默认 manager ⇒ 已软删的单取不到（手工补数据不该给回收站里的单补图，与其余读点同一口径）。
    #    ⚠️ 落点在本命令的 `Command._run`（动作体从 `handle` 抽出来加进程锁，作用域名随之变了——本网按作用域收，所以这一行必须同批改）。
    ("business/management/commands/dailiantong_sync_progress.py", "Command._run"): 1,
    # ⭐ D-0342（转正来源号改写的存量补写）：一次性命令两处 `Order.objects`（同一作用域）——`plan_rewrites`：查重闸（目标展示号被另一张未删单占用 ⇒ 跳过）；`Command._rewrite_schema`：条件更新落写（pk + 旧值都命中才改）。
    #    两处都走**默认 manager**（软删单天然不命中/不匹配）⇒ ⛔ 不再叠 `deleted_at__isnull=True`（没有用例能弄红的重复判据，§5-E / E-0021 口径）。
    ("business/management/commands/rewrite_promoted_source_no.py", "plan_rewrites"): 1,
    ("business/management/commands/rewrite_promoted_source_no.py", "Command._rewrite_schema"): 1,
    # ⭐ D-0369（指派也写发单人）：存量回填命令两处 `Order.objects`（同一作用域 `Command._run_for_tenant`）——① 候选枚举：`filter(hall_publisher__isnull=True).only("pk", "order_no")`——**读**，走默认 manager ⇒ 软删单天然不进候选（回填不碰已删单）；
    #    ② 条件更新落写：`filter(pk=…, hall_publisher__isnull=True).update(hall_publisher=…)`——**写**、只动一列（`QuerySet.update()` 不碰 `auto_now` ⇒ `updated_at` 天然不动；WHERE 再带 isnull ⇒ 并发下也不覆盖新值）。默认 manager 口径与上一条命令同源。
    ("business/management/commands/backfill_hall_publisher.py", "Command._run_for_tenant"): 2,
    # ⭐ 业务方拍板（评审 Important 5 改准）：镜像卡派生的隐藏单（`Order.hall_card`）的取 / 建 / 复活**改走 `Order.all_objects`**（两处）——默认 manager 看不见已软删的行 ⇒ `get_or_create` 会去新建一张，撞 0099 的 OneToOne 唯一索引（500）；复活的条件更新也必须用 `all_objects`（否则 0 行）。
    #    ⇒ 它的两处落点登记在**下面那张** `ALL_OBJECTS_SITES` 表里（⛔ 不留在本表：本表只收 `Order.objects`，多留一条会让「已不存在的读点」那一半断言红）。`hall_shadow.refund_for_card` 的 `filter(pk=…, status=WAITING_GRAB).update(status=REFUNDED)`：卡 `gone` ⇒ 隐藏单跟着退（**写**，与 A-w2 同形），走默认 manager + 显式 `deleted_at__isnull=True`：已软删的隐藏单不参与这次收口。
    ("business/hall_shadow.py", "refund_for_card"): 1,
    # ⭐ `sync_hidden_order_price` 按 `hall_card` + `WAITING_GRAB` 取那一张隐藏单（`qs = Order.objects.filter(...)` → `.first()` 与同一 `qs.update(...)` 共用）。走默认 manager ⇒ 已软删的隐藏单天然不在候选里（退掉的派生单不该被改价）。
    #    ⚠️ **有意**用「同一个 qs 变量」的形状（一次查询同时供 `.first()` 与 `.update()`），不内联 `Order.objects.filter(...).update(...)`——那会让本表变成 2 个落点。
    ("business/hall_shadow.py", "sync_hidden_order_price"): 1,
    # ⛔ 租户级鸡蛋铺镜像 / 回拉 / 对账一族的旧落点（`promote_mirror` / `consume_launch_event` / `discover_remote_taken` / `consume_withdraw_event` 与 `jidanpu_reconcile` 的 `resolve_order_nos` / `remote_order_keys` / `working_targets` / `_facts`）已随那条线整条拆除，登记条目随之删除——多留一条会让「已不存在的读点」那一半断言红，⛔ 不要加回。撤下事件必须能推到已软删的单 ⇒ 那一落点改走 `Order.all_objects`（在下面 ③ 的 `ALL_OBJECTS_SITES` 里登记）。
    # ⭐ 订单批量线的**两处**取行口——`resolve_orders` 按 `order_no__in` 解析这批单；`run_batch` 在 apply 分支用同一组单号 `select_for_update()` 锁行。两处都走**默认 manager**（`include_deleted` 为假时 ⇒ 软删单在批量里就该取不到），数据权限与列表同源（`scope_queryset` + `game_scope("game_id")`）。⚠️ `include_deleted=True` 那一半（A8 的 `batch_delete` 才用）是本文件 ③ 的登记对象。
    ("business/api/order_batch.py", "resolve_orders"): 1,
    ("business/api/order_batch.py", "run_batch"): 1,
    # 下家鸡蛋铺 `jidanpu_member` 的 `Order.objects` 读点——`orders_of`（按绑定账号列该下家的单，供补推候选）、三条事件消费支 `_consume_launch` / `_consume_assign` / `_consume_status_out`（各按 `order_no` 取一张单）、`apply_remote_completion`（写前复核：按 pk 复读一行新鲜状态）与 `_consume_unlist`（「从对方抢单大厅撤下」那一支）。
    #   ⚠️ **全走默认 manager 是有意的**：本线的语义是「**已软删单不再参与下游同步**」（与 `peer_inbound` / `order_batch` 同一口径）⇒ 取不到即跳过，⛔ 不改成 `all_objects`；软删的单在本线**不会**产生撤下（`_shape` 先给 `absent` ⇒ 排的是退单那一档），⛔ 不为了它绕过默认 manager（那一支的注释里写着同一条理由）。
    ("business/jidanpu_member.py", "orders_of"): 1,
    ("business/jidanpu_member.py", "_consume_launch"): 1,
    ("business/jidanpu_member.py", "_recover_relaunch_claim"): 1,
    ("business/jidanpu_member.py", "_consume_assign"): 1,
    # 罚款仅重验这一张未软删的已完成单，确认快照、实际审批和金额仍一致。
    ("business/jidanpu_member.py", "_penalty_order_still_current"): 1,
    ("business/jidanpu_member.py", "_consume_status_out"): 1,
    ("business/jidanpu_member.py", "_consume_locked"): 1,
    # 直派后台在当前租户内按订单号取未删除的单，未发送失败按原事件标记清理。
    ("business/jidanpu_direct_queue.py", "_run"): 1,
    ("business/jidanpu_direct_queue.py", "_fail_before_send"): 1,
    # ⭐ `apply_remote_completion`：写前复核（「判 → 写」整段抽在这里，②式回调 / ②式兜底对账 / 慢路回拉**三条路共用**那一段）。
    #   ⚠️ **默认 manager**：`fresh is None`（软删单取不到）按「挡住」处理。
    ("business/jidanpu_member.py", "_apply_remote_completion"): 1,
    # 上家 `_consume_unlist` 与下家三处上游写点互不重叠，两侧登记都留。
    ("business/jidanpu_member.py", "_consume_unlist"): 1,
    # ⭐ 下家②式**自愈**那一支：`_orders_by_source_order_no` 按**有效来源单号**反查我方订单（回调里的 `sourceOrderNo` ⇒ 补上链路缺的系统单号）。
    #   ⚠️ **只有这一个落点**：同一函数里的最终判等走的是既有函数 `jidanpu_member.effective_source_order_no`（本仓唯一那处口径），⛔ 不在这里另写一份。⚠️ 仍是**默认 manager**：已软删的单**不进自愈**（它按定义不在处理集合里，⛔ 不复活它）——与上面 `jidanpu_member` 那几处同一条口径。
    #   ⚠️ SQL 那一段（两个 `contains`）只是**粗筛**（故意取超集，把「来源单号首尾带空白」的行也捞进来），**判据**在 Python 那一行既有函数上 ⇒ 这里仍只算**一个**读点。
    ("business/jidanpu_member_openapi.py", "_orders_by_source_order_no"): 1,
    # ⭐ A-w10：`record_assigned_order` 的 `Order.objects.create(...)`——与 A3（`OrderViewSet.copy`）/ A-w4（`apply_events`）/ A-w5（`promote_mirror`）同形：**写**，新单的 `deleted_at` 天然为 NULL ⇒ 默认 manager 的软删过滤对 create 无意义（不构成读路径）。
    # ⚠️ **只有这一个落点**：同函数里建的链路 / 绩效分别在 `JidanpuUpstreamOrderLink` 与 `Performance` 上，⛔ 不在这里登记它们（多登一条会让「重数相等」那一半断言红）。⚠️ D-0324：建单语句从 `record_assigned_order` 搬进它的建单核心 `_create_pool_order`（复活重录与首次录池共用同一份投影）——判据 / 理由一字未改。
    ("business/jidanpu_upstream.py", "_create_pool_order"): 1,
    # ⭐ A-w11：`_pull_content` 的 `Order.objects.filter(pk=…).update(**updates)`——**内容回拉的收窄写**（只补我方为空的那几格，⛔ 不整行 `save()`、⛔ 不碰金额列）。与 A-w2 同形：**写**。
    # ⚠️ 默认 manager 的软删过滤在这里是**纵深**：调用方 `_is_pullable` 已经把「已软删 / 已退单 / 已停跟踪」的单挡在回拉扫描面之外 ⇒ 正常路径到不了这里；万一到了，`.update()` 会**静默 0 行**（不报错）——与那三处同一条已知副作用，⛔ 别改成 `all_objects`。
    # ⚠️ **只有这一个落点**：`_pull_row` 的其余写入走 `apply_transition`（已在 A5 登记）与链路 / 台账表，⛔ 不在这里登记它们。
    ("business/jidanpu_upstream.py", "_pull_content"): 1,
    # ⭐ A-w12：`promote_upstream_mirror` 的 `Order.objects.create(...)`——与 A-w10（`record_assigned_order`）/ A-w5（`promote_mirror`）同形：**写**，新单的 `deleted_at` 天然为 NULL ⇒ 默认 manager 的软删过滤对 create 无意义（不构成读路径）。
    # ⚠️ **只有这一个落点**：同函数里的链路 / 绩效分别落在 `JidanpuUpstreamOrderLink` 与 `Performance` 上，⛔ 不在这里登记它们（多登一条会让「重数相等」那一半断言红）。
    ("business/jidanpu_upstream.py", "promote_upstream_mirror"): 1,
    # ⭐ D-0322（回环镜像拦截）：`_loop_throw_keys` 的 `Order.objects.filter(jidanpu_member_links__isnull=False).filter(Q(source_order_no__in=…)|Q(order_no__in=…)).only(…)`——**读**（每轮一次的批量 JOIN：拉到的卡 `orderIdView` 对拍我方抛出键，判回环）。
    # ⚠️ 走**默认 manager** ⇒ 带软删过滤：软删单不参与判据是**设计意图**（被删单的回环卡不拦、照常建镜像，与既有行为一致），⛔ 别改成 `all_objects`。
    # ⚠️ **只有这一个落点**：同函数不写任何模型（拦截动作在 `sync_hall` 的镜像行上，不在 `Order` 上）。
    ("business/jidanpu_upstream.py", "_loop_throw_keys"): 1,
    # ⭐ 自动发货线（spec `2026-10-03-xgj-auto-ship-design.md` §3.3/§3.5）的**两处**：`auto_ship`（自动触发那一路）与 `retry_row`（页面「重试发货」那一路）各有一处 `Order.objects.filter(order_no=…).first()`。
    # 两处都是**读**，且都走**默认 manager** ⇒ 带软删过滤（`deleted_at` 非空的行读不到），落点形状与同族 `peer_inbound` / `jidanpu_upstream` 逐字同形（同样以 `order_no` 为键，不是主键）。
    ("business/xgj_ship.py", "auto_ship"): 1,
    ("business/xgj_ship.py", "retry_row"): 1,
    # ⭐ D-0357：发货闸 `schedule_auto_ship_gated` 的**两处**——①组互斥锁（`values_list("id", flat=True).first()` 取组内最小 id + `select_for_update()` 锁那一行，两步各一）；②闸下用 `split_group.active_members` 读全组成员（那一条登记在 `split_group` 自己名下）。
    # 两处都**读**、都走默认 manager ⇒ 已软删成员不进组闸（G9 删除时 `split_id` 已清，双保险同一口径）。
    ("business/xgj_ship.py", "schedule_auto_ship_gated"): 2,
    # ⭐ 2026-11-09 业务方拍板「上家来源单不发货」：发货闸顶的上家来源判据 `_order_from_upstream`
    # 一处——`Order.objects` 带 OR-join（`jidanpu_upstream_links` ∨ `peer_links.upstream_binding`）
    # 的 EXISTS，一条查询覆盖两条上家链路（jidanpu_upstream_triggers 的零绑定计数钉随之 1→2）。读、默认 manager。
    # ⭐ D-0357：`soft_delete_order` 的组上下文——G9/G10 在动第一笔写入前 `select_for_update().order_by("id").filter(split_id=…)` 锁**全组活跃成员行**（级联枚举 + prune 判数都要在锁下——行锁判据）。读、默认 manager。
    ("business/recycle.py", "soft_delete_order"): 1,
    # ⭐ D-0357：`split_group` 的两个行为侧判据（G2/G6/G11/G12 共用的唯一出处）——`active_members`（组内活跃成员，`id` 升序）与 `is_main_member`（「存在更小 id 的活跃成员吗」的 EXISTS 判断）。都读、都默认 manager。
    ("business/split_group.py", "active_members"): 1,
    ("business/split_group.py", "is_main_member"): 1,
    # ⭐ D-0357（G16）：订单列表新注解的两条**相关子查询**——`split_completed_members_expression`（组内已完成数，`Coalesce` 兜 0：带 GROUP BY 的聚合对空集不回一行）与 `split_is_main_expression`（本行是否组内 `id` 最小者，`NOT EXISTS` 形态）。
    # 与 `split_members_expression` 同一条口径：注解表达式、默认 manager ⇒ 已软删成员不算。
    ("business/selectors.py", "split_completed_members_expression"): 1,
    ("business/selectors.py", "split_is_main_expression"): 1,
    # ⭐ 闲管家推送接收端（spec `2026-10-04-xgj-push-inbound-design.md` §D3）：`_matching_orders` 按 `Order.source_order_no`（**BTRIM 之后**）取该推送对应的**全部**订单（`annotate(_push_key=…).filter(_push_key=order_no).order_by("id")`）——**读**，且走**默认 manager** ⇒ 已软删的单天然不进命中集合。这正是要的：软删单**本来就改不了状态**（唯一写入口 `apply_transition` 的加锁读同样走默认 manager，D-0144）⇒ 列进来只会白跑一趟；spec §10.3 把这条边界登记为 §D3 的补充口径。
    # ⚠️ 本落点**不是**「读点漏加 `deleted_at__isnull=True`」那一类（它就是默认 manager 的语义）；它进本表是因为本网按「**所有**可执行 `Order.objects` 落点」的口径收（同 A-w4 / A-w5 的形状）。⭐ 本落点**补排两族隐藏单**（`hall_card` / `upstream_hall_card` 非空），形状与理由见 `HALL_SHADOW_EXCLUDED_SITES` 的登记行——两句 `.exclude()` 是**同一条 queryset 链**，不新增 `Order.objects` 落点 ⇒ 重数仍 1。⚠️ **整套跑才红**：分模块跑看不见，只有合并树上的全量套件才报出来（`AGENTS.md` §5-F、同族踩坑：漏登记要等全量套件才红）。
    ("business/xgj_push.py", "_matching_orders"): 1,
    # ⭐ 交易订单操作记录线（spec `2026-10-04-trade-action-log-design.md` §D7）：`_order_backfill` 按该页**当前页那几行的 `order_no`** 批量取订单，用来把日志行上恒空的 `game_name` / `source_order_no` 回填成**订单当前值**（一次查询，无 N+1）——**读**，走**默认 manager** ⇒ 已软删的单不回填（查不到就保持空）。
    # 这正是要的：本页与全仓常规读面同一条口径（D-0144 之后软删单在常规读面上不存在），这条边界登记在 spec §10.9。⚠️ 本落点**不是**「读点漏加 `deleted_at__isnull=True`」那一类（它就是默认 manager 的语义）；它进本表是因为本网按「**所有**可执行 `Order.objects` 落点」的口径收（同 A-w4 / A-w5 的形状）。⚠️ **整套跑才红**：分模块跑看不见。**教训回流**：凡新增任何 `Order.objects` 读点的线，**受影响网清单必须包含本文件**（`tests.test_order_read_path_net`）。
    ("audit/api/views.py", "_order_backfill"): 1,
    # ⭐ D-0194（订单页统计值 + 首页概览 + 改价同步）：两个 `Order.objects` 读点，都走**默认 manager**（软删单在常规读面上不存在，D-0144）：`business/api/console_views.py::home_overview_data`——首页「数据概览」12 格的聚合底集（`filter(created_at__date__gte/__lte=…)`）⇒ 软删单**不进**这 12 格；`business/jidanpu_member.py::_consume_price`——`member.price` 消费者按单号取那张**还在**的单 ⇒ 软删单**不再往上游打**改价（那种形态由 `member.withdraw` 收干净，注释在该函数 docstring 里）。
    # ⚠️ **整套跑才红**的教训已回流进 `AGENTS.md` §5-G：新增任何 `Order.objects` 读点 / `order__` join 的线，受影响网清单**必须**含 `tests.test_order_read_path_net`。
    ("business/api/console_views.py", "home_overview_data"): 1,
    ("business/jidanpu_member.py", "_consume_price"): 1,
    # ⭐ 改「接手项目」⇒ 推下家那条线（`member.project`）：与上一条**同一形状**的第三个落点——消费者按单号取那张**还在**我方系统里的单（`filter(order_no=event.target_ref)`）⇒ 软删单**不再往上游打**改项目（那种形态由 `member.withdraw` 收干净，注释在 `_consume_project` 的 docstring 里）。
    # ⚠️ 走**默认 manager**（与 `_consume_price` / `_consume_launch` / `_consume_withdraw` 同口径）。⛔ 本行**不是**"多加一条注释就完事"：本网按「文件 + 作用域名 + 重数」收 ⇒ 漏了它当场红（`AGENTS.md` §5-G：新增 `Order.objects` 读点的线，受影响网**必须**含本文件）。
    ("business/jidanpu_member.py", "_consume_project"): 1,
    ("business/jidanpu_member.py", "_consume_source_order_no"): 1,
    # ⭐ 开放平台对外查单端点（spec §7.1 / §7.2）——**两个端点共用这一处落点**：`ApiOrderView` 的取数口是两条路由（按订单号 / 按来源单号）**唯一**的 `Order.objects` 底集（⛔ 视图里不许各写一份：那会多出落点）。
    # ⚠️ 走**默认 manager** ⇒ 已软删单（D-0144）在对外接口上**不存在**——这正是要的：软删单是租户自己的内部状态，第三方不该查到它（查不到的表现与"不存在"同形）。⚠️ 落点里**没有** `order__` join（用相关名 `game` / `project` / `server` / `category` 做 `select_related`、用 `performances` 做 `prefetch_related`），所以本网第二张表（`ORDER_JOIN_SITES`）**不需要**为 T4 加行——实测：只报这一条 read 落点。
    # ⚠️ 作用域名是 `ApiOrderView._orders_queryset`（v1 面的**游戏范围收窄**已删除：方法不接「允许集合」参数，回的是**本租户全部**订单）。⚠️ **落点数不变（仍是 1）**、走默认 manager 的语义不变。⚠️ "重命名会让本网红、逼人当场重数"正是本网的价值（按「文件 + 作用域名」收）。
    ("openapi/api/order_query_views.py", "ApiOrderView._orders_queryset"): 1,
    # ⭐ 开放平台 v2 的**只读扩展端点**（设计文档 §6.1）——三个带 `<order_no>` 的端点（`status/` / `images/`，以及 `ranks/` 之外的路径）共用**同一个**取单口。
    # ⚠️ 它是本模块**唯一**的 `Order.objects` 落点（`_live_order`）：各端点各写一份会让"对外怎么按单号取一张单"出现第二个判据（`AGENTS.md` §5-E）。
    # ⚠️ 走**默认 manager** ⇒ 已软删的单取不到（D-0144），与上面那条查单端点同口径（对外读面上不存在已软删的单，且与"不存在"**同形**）。⚠️ 它**不是**"读点漏加 `deleted_at__isnull=True`"那一类（默认 manager 的语义就是它）；进本表是因为本网按「**所有**可执行 `Order.objects` 落点」的口径收（同 A-w4 / A-w5 的形状）。
    ("openapi/api/order_extra_views.py", "ApiOrderExtraView._live_order"): 1,
    # ⭐ 开放平台 v2 的**写端点**（设计文档 §6.1 / §6.3）：改状态与改发单价两个端点共用**同一个**写侧取单口（模块级 `_live_order`；`_require_order` 是它唯一的上层入口）。
    # ⚠️ 它与上面那条读侧的落点**判据逐字相同**（走默认 manager ⇒ 已软删的单取不到，且"取不到"与"不存在"同形：都是业务码 `1002`），但**没有复用**读侧那个静态方法，理由两条：① 两条路的**取数面**不同：读侧那一份要 `prefetch_related("performances")`（装配对外查单载荷用），写侧这一份一个预取都不需要 ⇒ 复用会让每个写请求白付一次绩效行查询；② 本网按**每个 AST 落点**收（键 = 文件 + 作用域名）⇒ 复用会让写路径的取数判据挂在**另一个模块**的登记项上，而本表的价值恰恰是"哪条路的取单口在哪、一眼查到"。
    # ⚠️ 两个写端点共用它（`_require_order` 是唯一入口）：各写一份 `Order.objects` 会让"对外能不能改这张单"出现第二个判据（`AGENTS.md` §5-E）。
    ("openapi/api/order_write_views.py", "_live_order"): 1,
    # ⭐ `business/order_writes.py::change_sales_agent` 事务里的**行锁**（换归属 vs 结账的 TOCTOU 收窄）——`Order.objects.select_for_update().filter(pk=order.pk).first()`（把订单行锁住），后面那一次取 SALES 行的查询也跟着加了 `select_for_update`（那一条是 `Performance.objects`，**不进本表**；它的 `order__deleted_at__isnull=True` join 记在上面那张 `ORDER_JOIN_SITES` 的同一作用域里）。
    # ⚠️ 这个作用域只进本表这一格（它唯一的一处 `order__` join 记在 `ORDER_JOIN_SITES` 里）。真值 **1** 是**本网先报出来**的（先跑一次让网自己报），⛔ 不是按 +1 推算、也不是把扫描器输出粘进来。⚠️ **两张表各自计数**：一处新落点只进它该进的那张表。
    # ⚠️ 它**没有**软删过滤：锁的是**主表那一行本身**，而软删单在动作体里已被 `get_object()`（默认 manager）挡在外面 ⇒ 在这里补 `deleted_at__isnull=True` 是一条**没有任何用例能弄红**的防御性写法（E-0021 那一族），刻意不加。
    ("business/order_writes.py", "change_sales_agent"): 1,
    # ⭐ `order_writes.lock_order_row`——`/hall-price/`、`/performance/delete/`、`/sales-agent/`、`/adjust-amount/`（以及 `/adjust/`）统一锁序用的**唯一**订单行锁出口：`Order.objects.select_for_update(of=("self",)).order_by("id").filter(pk=…).first()`。
    # ⚠️ 它是一处**读点**（`SELECT … FOR UPDATE`），不是写点；登记口径仍是「所有可执行 `Order.objects` 落点」。真值 1 由本网先报出来（先跑一次让网自己报）。⚠️ 四处视图调的是**这个函数**（`lock_order_row(order)`），⛔ 不是各自写一句 `Order.objects…` ⇒ 本表只多这一格、不各占一行。
    ("business/order_writes.py", "lock_order_row"): 1,
    # 企微自动同步按组或单订单分别尝试非阻塞行锁。
    ("business/order_writes.py", "lock_order_and_sales_nowait"): 2,
    # ⭐ D-0223：「禁止录入相同的来源单号」的两道闸（各 1 处）——录单支 = `create_order` 建单事务之前；编辑支 = `OrderViewSet.partial_update` 进 `update_order_fields` 之前。各有一句 `filter(source_order_no=…, deleted_at__isnull=True)[.exclude(pk=…)].exists()`（编辑支多一个 `exclude(pk=…)`，录单支没有）。
    # ⚠️ 两条都**显式带 `deleted_at__isnull=True`**——不是防御性写法：业务口径第 4 条「回收站不占号」（D-0223）就是这条过滤的语义，有专门用例钉住（`tests/test_order_api.py::DuplicateSourceOrderNoGuardTests`）。
    # ⚠️ 各自真值 1 也是**本网先报出来**的（先跑一次让网自己报）。
    ("business/order_intake.py", "_duplicate_source_rejection"): 1,
    # ⭐ 新老客自动判定：同来源 + 同客户查「以前有没有下过单」；默认 manager 已排除软删单，
    #    与录单口径一致（已删历史不算下过单）。本行由本网先报出来后登记。
    ("business/order_intake.py", "_auto_customer_kind"): 1,
    # ⭐ 新老客存量回填：① 候选读取（有 customer + order_source 的未删除单）；
    #    ② 每批 bulk_update 的 queryset 也只碰未删除单；③ 收尾 flush 里同一类查询只出现一次。
    ("business/management/commands/backfill_customer_kind.py", "Command._run_for_tenant"): 1,
    ("business/management/commands/backfill_customer_kind.py", "Command._run_for_tenant.flush_batch"): 1,
    ("business/api/order_views.py", "OrderViewSet.partial_update"): 1,
    # ⭐ 大厅全量拉取（计划 `2026-10-25-peer-hall-pull` §1）：上家端点 `PeerHallListView.post` 的**原生在厅卡**底集（`WAITING_GRAB` + `performances__emp_type=MAIN` + `performances__user__isnull=True`）——与大厅读口 `selectors.search_hall` 同一条「真的可抢」判据（⛔ 不放宽成裸 `WAITING_GRAB`：抢不动的卡共享出去 = 下家点一次拒一次）。
    #    ⚠️ 走**默认 manager**（软删单不在厅，D-0144）；它带的是 `performances__` 反向 join，**不是** `order__` 字段路径 ⇒ 只进本表、不进 `ORDER_JOIN_SITES`。⚠️ 真值 1 由本网先报出来（先跑一次让网自己报）。
    ("business/api/peer_views.py", "PeerHallListView.post"): 1,
    # ⭐ 打手侧对外端点的**抢单**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 2 行）：按单号取那张单的**唯一**落点（模块级 `_live_order_in_scope`；`HallClaimView.post` 只调它）。⚠️ 走**默认 manager** ⇒ 已软删的单取不到（D-0144），与对外读面同口径：软删是租户自己的内部状态，"取不到"与"不存在"**同形**（都是业务码 `1002`）。它**不是**"读点漏加 `deleted_at__isnull=True`"那一类（默认 manager 的语义就是它）；进本表是因为本网按「**所有**可执行 `Order.objects` 落点」的口径收（同 A-w4 / A-w5 的形状）。
    # ⚠️ 它**额外**过了一道**游戏范围**收窄（`scope_queryset` + `game_scope("game_id")`），收窄的主体是**绑定账号**（`TakerApp.taker`）而不是 `request.user`——理由写在那个函数里。⚠️ 本行是**本网先报出来**的（先跑一次让网自己报），⛔ 不是按 +1 推算的。
    # ⭐ D-0338：大厅原生行的 `order_no` 键是「来源号优先展示号」⇒ 抢单取单口除了内置号那一路还有**来源号兜底**那一路 `Order.objects.filter(source_order_no=…)`（**恰好一张才认**，范围收窄与内置号那一路同一道 `scope_queryset`；零张/多张一律取不到 = `1002` 同形）。两句都在同一个作用域里 ⇒ 重数 2。判据：把兜底那一句删掉，本表会以「重数 1 != 2」报红。⚠️ 同批的**拉大厅**那条（`api/hall_views.py`）**不进本表**：它不写 `Order.objects`——取数走 `selectors.search_hall`（底集那一条落点早就登记在 A2 那一行）。
    ("takerapi/api/order_write_views.py", "_live_order_in_scope"): 2,
    # ⭐ 打手侧对外端点**第二批**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 3、4 行）：**「我的单」列表**的取数口 = `apps/takerapi/api/order_scope.py::claimed_order_qs`（它返回 `Order` queryset，是四条对外端点共用的那一份范围判据的**列表版**）。
    # ⚠️ 走**默认 manager** ⇒ 已软删的单取不到（D-0144），与内部「我接的单」同口径：软删是租户自己的内部状态，"取不到"与"不是你的单"**同形**（都是业务码 `1002`）。它**不是**"读点漏加过滤"那一类（默认 manager 的语义就是它）；进本表是因为本网按「**所有**可执行 `Order.objects` 落点」的口径收（同 A-w4 / A-w5 的形状）。⚠️ 它**刻意不做**游戏范围收窄（⛔ 不加 `scope_queryset` / `game_scope`）：授权判据是「**他是这张单的当前接手人**」而不是"游戏范围内"（spec §5.3-2 的红队改准），判据的逐条理由写在 `order_scope` 的模块 docstring 里。⚠️ 本行是**本网先报出来**的（先跑一次让网自己报），⛔ 不是按 +1 推算的。
    # ⚠️ **同批另两条 `Order.objects` 落点在哪里**（如实登记，免得后来者找不到）：① 查单 / 改状态 / 进度图三条走的 `claimed_order_of` 里**没有** `Order.objects`——它从 `Performance.objects` 起（spec §5.3-2 给的取数形状），所以那三条在本表里**不各占一行**；② `api/my_orders_views.py` 与 `api/order_images_views.py` 同样一处都没有（都是"先调 `order_scope`、再装配"）。
    ("takerapi/api/order_scope.py", "claimed_order_qs"): 1,
    # ⭐ 拆单**两处**（各自都是本网先报出来的）：① `OrderViewSet.split_local_order`——详情页那条拆分入口落子单时的 `Order.objects.create(...)`，与 A3（`OrderViewSet.copy`）/ A-w10（`record_assigned_order`）**逐字同形**：**写**，新单的 `deleted_at` 天然为 NULL ⇒ 默认 manager 的软删过滤对 create 无意义（不构成读路径）；它与 A3 一样进本表，是因为本网按「**所有**可执行 `Order.objects` 落点」的口径收（不区分读 / 写）。⚠️ **同一个方法里只有这一处**：当前单那一次改写走 `get_object()` 拿到的实例（`get_queryset()`），组 ID 复用 `order.split_id`，⛔ 没有第二句 `Order.objects`。
    # ② `selectors.split_members_expression`——列表载荷 `split_members` 那条**相关子查询**（`filter(split_id=OuterRef("split_id"))...`）：**读**，走默认 manager ⇒ 已软删的成员**不算**进「组内成员总数」（与 D-0144 同一条口径；理由写在那个函数自己的 docstring 里）。⚠️ 它是**注解表达式**、不是列表的底集（底集仍是 A1 的 `search_orders`）——进本表同样是因为「所有可执行 `Order.objects` 落点」这条口径。
    # ⚠️ 两条真值都是**本网先报出来**的（先跑一次让网自己报），⛔ 不是按「应该要登记」推的。
    ("business/api/order_views.py", "OrderViewSet.split_local_order"): 1,
    # 并列拆单锁住组成员（两处分支）或单笔源单，并创建子单。
    ("business/api/order_views.py", "OrderViewSet.split_priced_order"): 4,
    # SDK 抢单候选使用默认 manager，并按打手的游戏范围取单。
    ("openapi/api/integration_hall.py", "IntegrationHallClaimView.get_native_order"): 1,
    # SDK 订单写入锁在用订单，回收站动作另走 all_objects 分支。
    ("openapi/api/integration_orders.py", "_locked_order"): 1,
    ("business/selectors.py", "split_members_expression"): 1,
    # ⭐ D-0296（大厅离厅快照/优选/模拟单线）：`hall_simulated._top_up` 的 **3 处**——模拟单补充每轮从「该游戏的**历史订单**」里抽源复刻卡：① `values_list("game_id", flat=True).distinct()`——有历史订单的游戏全集（决定这一轮给哪些游戏补卡）；② `filter(game_id=…).exclude(pk__in=excluded).values_list("pk")`——该游戏的候选源单（排除集 = 当前在厅卡的源单 + 近 4 小时被抽过的源单）；③ `filter(pk=source_pk).first()`——抽中的源单按 pk 复读一行（取区服 / 项目文本 / 抛单价）。
    #    ⚠️ 三处都**有意走默认 manager**（与 `jidanpu_member` / `xgj_ship` 同一条口径）：已软删的单**不进**模拟单的源（①里不贡献 game、②里不进候选、③里取不到就跳过）——D-0144，模块 docstring 的「有历史订单（未软删）的游戏」写的正是它。它**不是**「读点漏加 `deleted_at__isnull=True`」那一类（默认 manager 的语义就是它）。
    #    ⚠️ 落点形状是**后台轮**（`jidanpu_sync` 进程内锚按 2 小时一拍调）：只在**已建立的租户上下文**里跑、⛔ 不接受任何租户标识参数（模块 docstring 的边界节）。
    ("business/hall_simulated.py", "_top_up"): 3,
    # ⭐ 交易平台抛单线：`_visible_order` 按订单号取**本租户 + 本账号游戏可见范围**内的那张单（`filter(order_no=…)`，再交给 `scope_queryset(..., game_scope("game_id"))`）——**读**，走默认 manager ⇒ 已软删的单天然取不到（与 `/refunds`、`/copy` 的取对象口同一条口径）。⚠️ 值 **1** 是本网先报出来的（先跑一次让网自己报），⛔ 不是按「应该要登记」推的。
    ("business/api/dailiantong_push_views.py", "_visible_order"): 1,    # ⭐ 余额不足预警只读一张「可抛候选单」清单
    #    （`未分配 / 待抢单` 且没有有效推送）来估算未来 24h 所需余额。⚠️ 这是**读**点（`Order.objects.filter(...)`，一处在 `balance_alert`），⛔ 不接受任何请求参数里的租户线索；不写库、不发单。
    ("business/dailiantong_push.py", "balance_alert"): 1,
    # ⭐ D-0325：**上家镜像卡派生隐藏单**的两个 `Order.objects` 读点（`upstream_hall_shadow`；取 / 建 / 复活那几处走 `all_objects`，登记在 `ALL_OBJECTS_SITES` 里）：`sync_price_for_card`——只取 `WAITING_GRAB` 那一张对价（转正后的单是生意价，⛔ 不许被上家改价改写）；默认 manager 已滤软删 ⇒ 软删行天然不进这一档（不复活）。`refund_for_card`——条件更新前的判读（`WAITING_GRAB` 且未软删才翻 `REFUNDED`）。
    ("business/upstream_hall_shadow.py", "sync_price_for_card"): 1,
    ("business/upstream_hall_shadow.py", "refund_for_card"): 1,
    ("business/upstream_hall_shadow.py", "refund_for_cards"): 2,  # 锁定未软删的待抢隐藏单，再写本批状态。

    # ⭐ D-0348（订单群码自动拉取）：daemon 线程里按 pk+单号取单（默认 manager，软删单取不到 ⇒ 与 D-0144 同口径；软删复检紧随其后 `deleted_at is not None`）。
    #   上下文：线程已用 `tenant_context(tenant)` 重建租户 schema（peer_sync 先例），这一条是复核闸的取数；读不到 = 单已被删/不存在 ⇒ 静默放弃。
    ("business/order_qr_fetch.py", "_run_fetch"): 1,
    # ⭐ D-0356（马上来查码队列）：两处落点各 1——`_suppress_fetching` 是**写**（按 pk 的条件 `.update()` 写持久退避列，仅对端 429 写、普通失败不写；默认 manager 的软删过滤对 `.update()` 无约束力，与本表「所有可执行 `Order.objects` 落点」的口径一致）；
    #   `queue_candidates`（管理命令）是**读**（查码队列的候选集：未软删 + 24h 窗 + 不在 429 退避期 + 无群码行，老单优先）。
    ("business/order_qr_fetch.py", "_suppress_fetching"): 1,
    ("business/management/commands/fetch_order_qr_queue.py", "queue_candidates"): 1,
    # ⓘ `mark_window_gaveups` 读点已随查码台账简化整段删除
    #   （两个关键时刻 kind 停写），登记行同步移除。
    # ⭐ V2 看板线（`/v2` Dashboard 的聚合只读端点）：三个 `Order.objects` 读点，都走**默认 manager**
    #    ⇒ 软删单不进这三个聚合（D-0144 同一条口径）。`trend_points`——窗口内逐日单量/金额，**另排除
    #    已退单**（趋势图标题口径「已排除已退单部分」）；`game_distribution`——窗口内按游戏名分组的
    #    单量，**不排除已退单、不排镜像隐藏单**（端点自己的口径，docstring 明写「别互相统一」）；
    #    `hall_stay_data`——大厅实时快照的在厅池与停留时长（池 = `WAITING_GRAB` 且无主；软删单经
    #    默认 manager 天然不进池），「今日被抢速度」样本从 `Performance` 起查、显式带
    #    `order__deleted_at__isnull=True`（登记在下面 `ORDER_JOIN_SITES` 的同名格）。
    ("business/api/dashboard_views.py", "trend_points"): 1,
    ("business/api/dashboard_views.py", "game_distribution"): 1,
    ("business/api/dashboard_views.py", "hall_stay_data"): 1,
    # ⭐ 2026-11-10 客户线：members 读面的取行口（行聚合 + 筛选下拉候选，全部默认 manager
    #    ⇒ 已删单不进会员行与候选，与列表口径同源）；回填命令的取单 / 连单 / 复查三处
    #    （候选只取在库 + customer IS NULL，默认 manager 天然排除已删单——「已删单不回填」）。
    ("business/api/customer_views.py", "CustomerMembersView.get"): 1,
    ("business/api/customer_views.py", "CustomerMemberFiltersView.get"): 1,
    # ⭐ 第七批（2026-11-11）：抽屉头部信息卡 `customer_info` 的合并统计与最近一单电话
    #    两处落点（同用例一个作用域；底集 = 全量 `Order.objects.all()`，与外层筛选无关，
    #    走默认 manager ⇒ 软删单不进读数）。
    ("business/api/customer_views.py", "CustomerMemberOrdersView.get"): 2,
    # ⭐ 2026-11-10 客户线第三批（analysis 聚合）：三处落点全走默认 manager（软删单不进
    #    客户分析，与 members 行集同口径）——`_period_orders` 是 KPI / 来源分布的共用基集；
    #    `analysis_consume_tiers` 全时段按客户 Sum；`analysis_growth` 全时段按客户 Min（首单）。
    ("business/customers.py", "_period_orders"): 1,
    ("business/customers.py", "analysis_consume_tiers"): 1,
    ("business/customers.py", "analysis_growth"): 1,
    # 限制档行集判据（客户侧 IN 子查询的基集，走默认 manager；第五批 2026-11-11）。
    ("business/customers.py", "restricted_member_customer_q"): 1,
    # ⭐ 来源单「有效口径」的在库系统单同号子查询（`source_order_no__in=Order.objects.…`）——
    #    走默认 manager 是承重语义：软删系统单不再代表其来源单 ⇒ 来源单恢复可见
    #    （docstring 明写「不双删」；同单两面只算一张，钉死不双删的用例在 test_customer_members）。
    ("business/customers.py", "valid_source_orders"): 1,
    ("business/management/commands/backfill_xgj_customers.py", "Command._candidates"): 1,
    ("business/management/commands/backfill_xgj_customers.py", "Command._backfill_one"): 1,
    ("business/management/commands/backfill_xgj_customers.py", "Command._report"): 1,
    # 只有在用订单才会和当前订单争用群聊的来源单号。
    ("wecom/archive_views.py", "_duplicate_legacy_number"): 1,
    ("wecom/archive_views.py", "_remember_order_group"): 1,
    # 仅用当前租户的未删订单号查找缺少展示号快照的推送记录。
    ("accounts/wxpusher_admin_views.py", "WxPusherAdminHistoryView.get"): 1,
})

#: ② 可执行的 `order__` 字段路径落点白名单：spec §2.2.2-C 组的落点 + **计划 C 清单之外**、但确实是 `order__` join 的既有落点（本表逐行用 `⚠️ 非 C` 标出）。
#: ⚠️ spec §2.2.2-**D 组的两类**（`order=order` 实例条件、`perf.order` 正向 FK 对象访问）**不在**本表里：它们不是 `order__` 字段路径，本网也扫不到；D 组的处理是「不加条件、就地注释」（spec §2.2.2 的 D1/D2）。
#: 键 = (相对 `apps/` 的路径, 作用域名)；值 = 该作用域里的**落点数**（含关键字参数名与字符串常量）。多值项逐处列出对应关系；`⚠️ 非 C` 的几条是**计划 C 清单之外**、但确实是 `order__` join 的既有落点（本网按 spec §8-T4-26「所有 `order__` 可执行落点」的口径收它们，理由写在各自行内）。
ORDER_JOIN_SITES: collections.Counter[tuple[str, str]] = collections.Counter({
    # 跟进资格只对上游订单列表交过来的可见订单号求交集。
    ("internal_chat/order_groups.py", "followable_order_numbers"): 2,
    # 同步检查的是当前群还关联哪个订单，以及哪条绑定可以处理事件。
    ("internal_chat/order_groups.py", "can_deliver"): 1,
    ("internal_chat/order_groups.py", "_materialize_message"): 1,
    ("internal_chat/order_groups.py", "_receive_detachment"): 1,
    ("internal_chat/order_groups.py", "receive"): 1,
    ("accounts/wxpusher_notifications.py", "worker_notice_targets"): 3,  # 只链接接单人当前拥有的未删除订单。
    ("business/online_payroll.py", "_scope"): 1,  # 按游戏权限控制财务历史访问，已删单仍保留流水。
    # ⭐ 图片存储线：三个可执行 join 落点，各 2 处（`order__order_no` + `order__deleted_at__isnull`，后者是 D-0144 的软删排除）。
    ("business/api/screenshot_views.py", "_pending_row_or_error"): 2,
    # 免密截图先排除回收站订单，再按单号核对锁的对象。
    ("business/api/screenshot_browser.py", "_browser_write_grant"): 2,
    ("business/api/screenshot_views.py", "ScreenshotCollectionView.list_items"): 2,
    ("business/api/screenshot_zip.py", "ScreenshotZipView.download"): 2,
    # C1：`select_related("order__game")` 1 处 + `order__deleted_at__isnull` 1 处。⭐ D-0365：那组条件抽成模块级 `my_orders_queryset`（列表与导出共用），作用域名随之从 `MyOrdersViewSet.get_queryset` 换成它。
    ("business/api/my_orders_views.py", "my_orders_queryset"): 2,
    # ⭐ D-0365：`MY_ORDER_DATE_FIELDS`（模块级字典）两档走 `order__` 关系打**订单**的列（`order__submitted_at` 交单 / `order__created_at` 录单；另两档 `claimed_at` / `settled_at` 是绩效本表列、不进本表）
    #   ⇒ `<module>` 作用域、重数 2（形状照下面 `PERFORMANCE_DATE_FIELDS` 那条的口径：字符串常量收在 `<module>` 名下）。
    ("business/api/my_orders_views.py", "<module>"): 2,
    # ⭐ D-0365：收入导出视图的 4 处镜像筛选（页面客户端筛选的服务端镜像）`order__game__name` / `order__status__in` / `order__order_no__icontains` / `order__project_text__icontains`。
    #   ⚠️ 软删口径**不在**这一格补：行集来自 `my_orders_queryset()`（上一行已带 `order__deleted_at__isnull`）⇒ 再加一道是没有用例能弄红的重复判据（E-0021 同族，与 `apply_income_filter` 那格同一口径）。
    ("business/api/my_orders_views.py", "MyOrdersExportView.get"): 4,
    # ⭐ D-0294（绩效出钱包）：`?income=` 这一档的 3 处 `order__status`（completed / unsettled / settled 三支各一条 filter）。
    #   ⚠️ 这 3 处**有意不带** `order__deleted_at__isnull`：入参 queryset 已由 `MyOrdersViewSet.get_queryset()` 排掉软删单（上一行那 1 处）⇒ 这里再加一道是没有用例能弄红的重复判据（§5-E / E-0021 同族）。
    ("business/api/my_orders_views.py", "apply_income_filter"): 3,
    # ⭐ D-0294（同上）：四数汇总的唯一取行口，**带着** D-0144 的软删排除
    #   （`order__deleted_at__isnull=True`）——软删的绩效行既不进四数、也不能申请提前结算。
    ("accounts/app_income_views.py", "my_performances"): 1,
    # 详情先经 Order 默认查询取到可见订单，再加载游戏和区服。
    ("business/api/my_orders_views.py", "get_own_order_detail_performance"): 2,
    # ⭐（2026-11-07 R3 起）基集抽出：三条基判据（`order__deleted_at__isnull=True` / 两个隐藏族 exclude）搬到 `_performance_base_queryset`（2026-11-08 撤强制展示腿后它仍是唯一构造处）。隐藏单排除的「必须排除」判据见 HALL_SHADOW_EXCLUDED_SITES 的同名登记。
    ("business/api/performance_views.py", "_performance_base_queryset"): 4,  # 另预取 order__game。
    # C2（绩效列表筛选段；导出走薄封装 `performance_queryset`、行集同源）：12 个订单维度筛选关键字（game_id / status / order_type / order_source_id / sales_user__username__in / created_by__in / order_no / order_no__icontains / game_role_name__icontains / source_order_no__icontains / project_text__icontains / customer_ww__icontains）+ `game_scope("order__game_id")` **1 处**（2026-11-08 撤挂负账强制展示腿，原「维度腿 + 强制腿各一次」回到 1）+ 顾客/username/keyword 各档（逐项以函数体为准；⛔ 条数不推算，合计以本行值与本网自报为准）；`filter(order__order_source__group_id=…)`——按来源所属分组筛；「收款状态」整支按 `order__payment_status` 判（业务方指认「收款状态 = 上家同步的那个」；`received_price` 全库零条有值 ⇒ 恒空），判据见 `tests.test_p4fu_performance_reads::NetReceiptsStateFilterTests`。
    # ⚠️ 隐藏单排除两条在 `_performance_base_queryset`（上一格）；除基集那条 `order__deleted_at__isnull=True` 外的各档**不再另补**软删过滤：它覆盖的是**同一个 JOIN 出来的全部行** ⇒ 再加一道是没有任何用例能弄红的重复判据（E-0021）。本网要的不是「每个 join 各配一条」，而是「落点数变了必须有人来看一眼」。判据见 `tests.test_performance_api` 的 `test_a_hall_shadow_order_is_not_listed`。
    ("business/api/performance_views.py", "performance_queryset_with_debt"): 16,
    # ⭐（2026-11-07 R3）挂负账快照粗筛（`performance_negative_debt.py`）：`order__deleted_at__isnull=True`——软删单的绩效行不进快照，与 `preview_rows` 同一条 D-0144 口径（跨关系 JOIN 不经过 `Order` 的默认 manager ⇒ 显式补）。B 项（净负窗口侧）吃的是 with_debt 已筛的 queryset ⇒ 不另占本表。
    ("business/performance_negative_debt.py", "_coarse_negative_rows"): 1,
    # ⭐⭐ `PERFORMANCE_DATE_FIELDS`（模块级字典）里三档改用 `order__` 关系打**订单**的列——`order__published_at`（发单时间）、`order__submitted_at`（交单时间）、`order__completed_at`（验收时间，取代原先那条打 `Performance.reviewed_at` 的 `reviewed` 档）。三个都是**字符串常量**，所以落在本表的 `<module>` 作用域名下、重数 = **3**（`_SiteScanner.visit_Constant` 收字符串常量）。
    #    ⚠️ **为什么不需要另加 `order__deleted_at__isnull`**：这三档只是把时间区间的**列名**换成订单上的列，而 `performance_queryset` 开头那条 `order__deleted_at__isnull=True` 覆盖的是**同一个 JOIN 出来的全部行**（与上面 C2 那条注释同一条理由）⇒ 再加一道是没有用例能弄红的重复判据（E-0021）。本网要的是「落点数变了必须有人来看一眼」，这一次看过之后的结论就是「不需要补」。
    #    ⚠️ 配套的**回流判据**（「接的列得有人写」）不在本网：见 `tests/test_performance_api.py::DateTypeColumnWriterContractTests`。
    ("business/api/performance_views.py", "<module>"): 3,
    # C3（计划行号 346 = detail 动作的基础集）：`game_scope("order__game_id")` 1 处 + 新增 1 处 = 2。
    # ⚠️ 计划的标签写「绩效导出」，但导出吃的是 `performance_queryset`（C2）⇒ 本处实际是 detail 动作。
    ("business/api/performance_views.py", "PerformanceViewSet._base_queryset"): 3,  # 另预取 order__game。
    # ⚠️ 非 C：绩效页 `#total` 六项合计，吃的是**已筛过的** queryset（`performance_queryset` 的产物）⇒ 不需要另加条件。⭐ D-0255：利润按单口径——`values("order_id", "order__total_price", "order__refund_fee").distinct()`（2 处字符串常量）+ `Sum("order__total_price")` / `Sum("order__refund_fee")`（2 处）= **4**。
    # ⚠️ `order_id` 不是 `order__` 字段路径，本网收不到（也不该收）。
    # ⭐ 性能线（2026-11-04）：旧 `performance_summary` 的取数口随 count 并聚合整段搬进 `performance_totals`
    # （行级一条 SQL 同时算 COUNT(DISTINCT id) + 两个 SUM；出参形状拆给纯函数 `summary_from_totals`、无 join）。
    # 选取结账汇总时，在同一条行级 SQL 上复用订单 JOIN，以 order__status 限定两个结账分类。
    ("business/api/performance_views.py", "performance_totals"): 5,
    # ⚠️ 非 C：绩效导出端点的 `select_related("order__game", "order__order_source")` 2 处；行集合来自 `performance_queryset`（已筛）⇒ 不需要另加条件。
    ("business/api/performance_write_views.py", "PerformanceExportView.get"): 2,
    # 结算列表与导出共用 settlement_rows 的软删及筛选，再由 scoped_settlement_rows 限定游戏范围。
    ("business/api/settlement_views.py", "settlement_rows"): 6,
    ("business/api/settlement_views.py", "scoped_settlement_rows"): 1,
    # C6（「结账对象人员」候选）：游戏过滤、权限和软删各一次。
    ("business/api/settlement_views.py", "SettlementPeopleView.get"): 3,
    # 日汇总和来源分解共用两个游戏范围分支。各自的查询仍排除已软删订单。
    ("business/api/settlement_views.py", "_settlement_scope"): 2,
    ("business/api/settlement_views.py", "SettlementDailySummaryView.get_queryset"): 1,
    # 来源分解包含 1 个软删条件、4 个来源分组与排序键、4 个载荷字段引用。
    ("business/api/settlement_views.py", "SettlementDailySourcesView._sources_data"): 9,
    # C7（锚点：`Performance.objects.all()` 的权限收窄）：`game_scope("order__game_id")` 1 处 + 软删过滤 1 处 = 2。
    ("business/api/adjustment_views.py", "AdjustmentListView.create"): 2,
    # C7（标签「未结账调整记录列表」实际落点）：`game_scope("performance__order__game_id")` 1 处 + 软删过滤 1 处 = 2。⚠️ 锚点行号与标签指向**两个不同的函数** ⇒ 两处都补。
    ("business/api/adjustment_views.py", "AdjustmentListView.get_queryset"): 2,
    # C8：`order__order_no` 关键字 1 处 + `game_scope("order__game_id")` 1 处 + 软删过滤 1 处 = 3。
    ("business/api/refund_views.py", "RefundView.get_queryset"): 3,
    # C9（接单统计）：游戏权限、软删及两种隐藏单过滤 4 处；只读取订单状态和单号 2 处。
    ("business/order_stats.py", "_window_rows"): 6,
    # C10（结账拒绝文案的取行，`_reject_reasons`）= **4**：`order__deleted_at__isnull`（D-0144 的软删排除）1 + `game_scope("order__game_id")` 1 + `exclude(order__hall_card__isnull=False)` 1——隐藏单的行与「不存在或不可见」同一个出口（与 `_resolve_batch` 同源：第 2 步排掉的行，拒绝文案这一处也必须排掉，否则会念出它的订单状态）+ `exclude(order__upstream_hall_card__isnull=False)` 1（第二个隐藏族）。
    # ⚠️ 「这行还有没有账没结」那条 `PerformanceAdjustment` 判据抽在模块级 `performances_with_unsettled_adjustments`（本表下面那一行）——同一个事实只写一份，两处各写一份就会漂移。
    ("business/settlement.py", "_reject_reasons"): 4,
    # 从 `_reject_message` 抽出的「这行还有没有账没结」判据（`PerformanceAdjustment` ⇒ `game_scope("performance__order__game_id")` 1 处）。
    # 两个消费方：拒绝文案的「已完全结清」那一支、以及「已是目标状态 ⇒ 跳过」的判据。
    ("business/settlement.py", "performances_with_unsettled_adjustments"): 1,
    # 结账侧「已是目标状态」的取行口——`order__deleted_at__isnull=True`（已软删单的行与「不存在或不可见」同一个出口，D-0144 同款）1 处 + `game_scope("order__game_id")` 1 处 = 2。
    ("business/settlement.py", "settled_ids_without_pending_adjustments"): 2,
    # C11 + C12（同一函数 `_resolve_batch`，预览与结账共用同一份解析）= **9**：`order__status`、`performance__order__status`、`performance__order__status__in`、两个 `game_scope`、已软删单与行排除（订单已完成 / 不在池内 / 归属人未冻结）——`exclude(order__hall_card__isnull=False)`——隐藏单上的绩效行不许结账（落在第 3 步 fail-closed ⇒ 整批 1006；判据见 `tests.test_settle_api`）；`exclude(order__upstream_hall_card__isnull=False)`（第二个隐藏族）。
    # ⚠️ `settle_performances` 自己一条 `order__` join 都没有（旧键会以「有已不存在的落点」报红）。
    ("business/settlement.py", "_resolve_batch"): 9,
    ("business/split_group.py", "unsettled_legacy_sales_waiting_ids"): 4,
    ("business/settlement.py", "settle_performances"): 1,
    # C13（撤销结账取行）：`game_scope("order__game_id")` + `performance__order__game_id` 各 1 处 + 软删过滤 1 处 = 3。
    ("business/performance_writes.py", "revoke_settlements"): 3,
    # 撤销侧「已是目标状态 ⇒ 跳过」的取行口——`order__deleted_at__isnull=True`（与 `_revoke_reject_message` 的软删口径同源）1 处 + `game_scope("order__game_id")` 1 处 = 2。
    ("business/performance_writes.py", "already_unsettled_ids"): 2,
    # C13 的**姊妹函数**（`settlement._reject_message` = C10 的同形物，计划漏列）：
    # `game_scope("order__game_id")` 1 处 + 软删过滤 1 处 = 2。
    ("business/performance_writes.py", "_revoke_reject_message"): 2,
    # C14（删除绩效取行）：`game_scope("order__game_id")` 2 处 + 软删过滤 1 处 = 3。
    ("business/performance_writes.py", "delete_performances"): 3,
    # C15：`order=order` 是实例条件、本处新增 `order__deleted_at__isnull=True` = 1。
    ("business/notifications.py", "_main_performance_taker_id"): 1,
    # C16：同 C15（pre_save 钩子的「之前的归属人」） = 1。
    ("business/notifications.py", "_remember_previous_taker"): 1,
    # C17（`Notification.order` 是 `SET_NULL` ⇒ 用 `.exclude(order__deleted_at__isnull=False)`）= 1。
    # ⚠️ 可见性过滤在**模块级** `_visible_notifications`（列表与未读计数端点共用、⛔ 不写第二份）。软删判据仍在（`.exclude(order__deleted_at__isnull=False)`，与 `.filter(...isnull=True)` 在 Django 5.2 下等价，依据见该 helper 的 docstring）——⛔ 不是漏了软删条件。
    ("business/api/notification_views.py", "_visible_notifications"): 1,
    # C18（抢单持有量闸）：`order__status__in` 关键字 1 处 + 新增 1 处 = 2。
    ("business/claim.py", "check_grab_limits"): 2,
    # C19（完成钩子的 MAIN 行） = 1。
    ("business/settlement_hooks.py", "_activate_taker_performance"): 1,
    # C20（客服提成那一半：SALES 行）= 1。⚠️ 客服提成重算抽在模块级 `recompute_customer_commission`（完成钩子与批量端点共用**同一份实现**，spec §D5）；利润减项那半在 `settlement_amount.taker_bonus_base`（D-0357 的组口径，它的三处 join 登记在上面本表的对应行）。
    #    ⛔ `on_order_completed` 自己已无任何 `order__` 落点（旧键会以「有已不存在的落点」报红）。
    ("business/settlement_hooks.py", "recompute_customer_commission"): 1,
    # 客服行**预计**绩效的唯一刷新点（`refresh_customer_estimate`；与上面 `recompute_customer_commission` 共用同一个算式 `customer_commission_amount`，⛔ 只写 `temp_amount`、不动 `bonus_amount`）。两条查询各带 `order__deleted_at__isnull=True`（D-0144 / C20 同款口径：跨关系 JOIN ⛔ 不经过 `Order` 的默认 manager ⇒ 必须显式补）：
    #      ① SALES 行——取要刷新的那一行（与 `recompute_customer_commission` 的 ① 同形，1 处）；② MAIN 行——算利润减项（容错读：查不到 / 无接单人按 `0.00` 算，1 处）。
    #    ⛔ 它是**估算值**的写入点：算不出就「不写、回 `None`、不抛」（计划 §2 T1.1 第 6 条）。
    ("business/settlement_hooks.py", "refresh_customer_estimate"): 2,
    # C21（`ensure_main_performance_of`，计划写「`main_performance_of` 一族」） = 1。
    ("business/performance.py", "ensure_main_performance_of"): 1,
    # C21b（D4a）：`_on_in_progress` 取该单 MAIN 行时带 `order__deleted_at__isnull=True`（照 `_activate_taker_performance` 的 C19 同款口径：跨关系 JOIN 不经过 `Order` 的默认 manager ⇒ 必须显式补，spec §4.3-C2）= 1 处。
    # ⛔ 本网先报出来的就是它（新读路径必须带软删过滤并登记）。
    ("business/performance.py", "_on_in_progress"): 1,
    # C22（退款联动的 SALES 行） = 1。
    # ⚠️ `order__deleted_at__isnull=True` 在模块级 `assert_sales_commissions_configured(order)`
    #    （唯一判据出处；两处调用同一份实现）。已软删单的客服行不参与这条判据。
    ("business/refunds.py", "assert_sales_commissions_configured"): 1,
    # C23（清零前的原接单人留痕，取 MAIN 行） = 1。
    ("business/refunds.py", "_record_taker_trace_before_clearing"): 1,
    # C24（上一轮冲减链的 SALES 行） = 1。
    ("business/refunds.py", "_reverse_sales_refund_deductions"): 1,
    # `on_order_refunded` = **4** 处 join（口径同一条：已软删单不参与，D-0144）：① MAIN 行 `.update()`；② 第 3 步的 SALES 行读取；③ 退款时取该单 MAIN 行的 `user_id` 去中断未打款的提现（`interrupt_open_withdrawals`）；④ 取该单 MAIN 绩效行去作废其 pending 提前结算申请（`settle_requests.cancel_pending_settle_requests`）。
    # 清 0 那一段抽在模块级 `_clear_unsettled_sales_amounts`（单独一行登记）。
    ("business/refunds.py", "on_order_refunded"): 4,
    # ⭐ C28：退单清 0 未结账客服行时那条 `Performance.objects.filter(order=order, emp_type=SALES, order__deleted_at__isnull=True)` = 1 处（抽在模块级 `refunds._clear_unsettled_sales_amounts`）。
    # ⚠️ 与 C22–C26 **同一个 join 口径**（已软删单的客服行不参与）。
    ("business/refunds.py", "_clear_unsettled_sales_amounts"): 1,
    # ⭐ C29：退单时把**自动入账**的打手工资从钱包扣回来。取 MAIN 行那一句带
    #    `order__deleted_at__isnull=True`（与 C23/C25 同一个 D-0144 join 口径：
    #    已软删单的 MAIN 行不参与回冲）。
    ("business/refunds.py", "_claw_back_auto_paid_taker"): 1,
    # ⭐ C27（T8-b 落）：候选名单端点的**打手**一半（`selectors.taker_options()`）。逐处数出来（执行期实测，⛔ 不是把扫描器输出粘进来）：① 基集 `order__deleted_at__isnull=True` 关键字 1 处；② `game_scope("order__game_id")` 字符串常量 1 处（数据权限卡点）；③ `?game=` 收窄的 `order__game_id=game_id` 关键字 1 处 ⇒ 3 处。
    ("business/selectors.py", "taker_options"): 3,
    # ⭐ D-0357（G13）：客服减项 helper `taker_bonus_base` 的**三处** `order__` 路径（非组支一条：`order=order, emp_type=MAIN, order__deleted_at__isnull=True`；组支两条：`order__in=组员子查询` + `order__deleted_at__isnull=True`）。
    # 与 `recompute_customer_commission` 原地那段查询同一条 D-0144 口径：跨关系 JOIN 不经过默认 manager ⇒ **显式补** `deleted_at__isnull=True`（已软删单的 MAIN 行不进减项）。判别力：删掉那条显式条件 ⇒ 本表红。
    ("business/settlement_amount.py", "taker_bonus_base"): 3,
    # 重算客服提成取 SALES 行时显式补软删过滤。已软删单的客服行不参与判据。
    # 漏登记或漏掉那条条件，本表会红。
    ("business/order_writes.py", "recompute_customer_commission_write"): 1,
    # ⭐ 换销售客服归属的两处——都是**同一个** `order__deleted_at__isnull=True` 口径（已软删单的 SALES 行不参与提成判据；跨关系 JOIN 不经过默认 manager，漏了会让「已软删的单」重新参与提成判据）：① `OrderViewSet.sales_agent`：动作体里那道「该单 SALES 行**已结账** ⇒ 拒绝换归属」的取行查询（与 `recompute_customer_commission_write` 的 ① 逐字同形）；② `order_writes.change_sales_agent`：换归属时取那一条 SALES 行（有则改 `user`、无则补建——与 `recompute_customer_commission_write` 的 ③ 同形）。
    #    ⛔ 这条登记是本网**先报出来**的（先跑一次让网自己报），不是照着扫描器输出粘的。⚠️ `change_sales_agent` 的行锁（`select_for_update()`）**没有** `order__` 字段路径 ⇒ 不占本表（登记在上面那张 `ORDER_READ_SITES` 表里）。
    # 本表管 `order__` join，读表管 `Order.objects`，两件事别混着改。
    ("business/api/order_views.py", "OrderViewSet.sales_agent"): 1,
    ("business/peer_direct_taker.py", "reveal_order_taker"): 1,
    ("business/peer_direct_taker.py", "_receive_assignment"): 2,
    ("business/order_writes.py", "change_sales_agent"): 1,
    # 结算预览的取行口（`settlement_preview.py`）——`order__deleted_at__isnull=True`（已软删单的绩效行不进预览，与结算列表同一口径）1 处 + `game_scope("order__game_id")` 1 处（数据权限卡点，与结算列表/绩效列表同源）= 2。
    # ⚠️ 它是**只读**端点：一行都不写（不写审计、不改 `updated_at`）。
    ("business/settlement_preview.py", "preview_rows"): 2,
    # ⭐ `pullable_links(binding)` 的处理集合判据（②式回调 / ②式兜底对账 / 慢路回拉**三条路共用同一条判据**：各写一份必然漂，漂了的后果就是「已退单的单被复活」），**2 处** `order__` join：
    #      ① `order__deleted_at__isnull=True`——已软删的单**不进处理集合**（跨关系 JOIN **不经过** `Order` 的默认 manager ⇒ 软删过滤必须**显式补**）；② `exclude(order__status__in=PULL_TERMINAL_STATUSES)`——把**已完成 / 已退单**挡在处理集合之外（「已退单不写」是**钱的承重判据**：把退掉的单写回「待验收」= 复活一张退单，而退款那条路是裸 `update()`、跑不到退款结清 ⇒ 钱会算错）。
    #    「去掉它就会红」的判据：`test_jidanpu_member_status.PullStatusesTests` 那两条 + `test_jidanpu_member_openapi_consume.SemanticsTests.test_a_refunded_order_is_not_even_queried`。
    ("business/jidanpu_member.py", "pullable_links"): 2,
    # 原订单的两个单号（含已软删原单）都保留为防碰撞依据，不能拿来录罚款。
    ("business/jidanpu_member.py", "_penalty_order_still_current"): 2,
    # ⭐ `reconcile` 的链路查询 = **2** 处 join：`exclude(order__hall_card__isnull=False)`——隐藏单建好就 `queue_hall_launch` 过、**天然有链路** ⇒ 不排它仍会从链路那一支加回对账集合，进 `to_launch` / `to_withdraw`（用例见 `tests.test_jidanpu_member_engine` 的 `test_a_linked_hall_shadow_order_is_not_reconciled`）；`exclude(order__upstream_hall_card__isnull=False)`（上家镜像隐藏单不进对账候选）。
    ("business/jidanpu_member.py", "reconcile"): 2,
    # ⭐ D-0326：`queue_percent_reprice` 按绑定列「在厅 + 待抢单」的链路。**2 处** `order__` join：① `order__status=OrderStatus.WAITING_GRAB`——只补还挂在对方大厅、本地待抢单的卡；② `order__deleted_at__isnull=True`——已软删的单不进下游补价，与本表 `pullable_links` 那条软删过滤同一条口径。
    ("business/jidanpu_member.py", "queue_percent_reprice"): 2,
    # 等待录单的改单只认当前订单的有效关联，软删单由消费者收口。
    ("business/jidanpu_member.py", "_remove_waiting_edits"): 2,
    # ⭐ `claim_net`（接单兜底网）的选取集与 `queue_percent_reprice` 同形状（在厅 + 待抢单），**2 处** `order__` join：① `order__status=OrderStatus.WAITING_GRAB`——只兜本地还在待抢单的单（已接手/终态的自有判据链，不进网）；② `order__deleted_at__isnull=True`——已软删的单不进兜底网（跨关系 JOIN 不经过默认 manager ⇒ 显式补，与 `pullable_links` 同一条口径）。
    ("business/jidanpu_member.py", "_claim_net"): 2,
    # ⭐ 开放平台 v2 的**进度图列表**（设计文档 §6.1 / §13.10）：对外 `images/` 端点的取行口与内部 `screenshot_views` 的进度图列表**同一口径**，逐处对应如下：① `order__order_no=order_no`——按单号取这张单的图（1 处）；② `order__deleted_at__isnull=True`——软删单的图不进对外读面（D-0144：跨关系 JOIN **不经过** `Order` 的默认 manager ⇒ 软删口径必须**显式补**，1 处）。
    # ⇒ 2 处。⚠️ 同一条口径在内部 `ScreenshotCollectionView.list_items` 里也有一份（本表上面那一行）——两处**都是**只读列表端点、口径逐字相同；对外这份由本行承担登记（⛔ 不许因为"内部已登记"就漏掉本行：本网按**每个落点**收）。
    ("openapi/api/order_extra_views.py", "OrderImagesView.get"): 2,
    # 查单和取图间可能发生软删除，取图时用 JOIN 再排除已删订单。
    ("openapi/api/integration_images.py", "IntegrationOrderImagesView.get"): 2,
    # ⭐ 打手侧对外端点**第二批**（spec `docs/specs/2026-10-08-taker-open-api-design.md` §5 端点表第 3、4、6 行）的**两处**落点。⚠️ **恰好这两处**是**本网自己报出来**的（做法：先留空跑一次让网自己报），⛔ 不是按某个数字推算的。逐处对应：`("takerapi/api/order_images_views.py", "TakerOrderImagesView.get"): 2`——与上家侧那份进度图列表**同一口径**的两处：`order__order_no=order_no` 1 处 + `order__deleted_at__isnull=True` 1 处（软删单的图不进对外读面，D-0144；跨关系 JOIN **不经过** `Order` 的默认 manager ⇒ 软删口径必须**显式补**）。
    #   · `("takerapi/api/order_scope.py", "claimed_perf_of"): 1`——`order__order_no=order_no`（spec §5.3-2 给的取数形状 `Performance.objects.filter(order=…)` 的等价写法：`order` 是 `Order` 实例、`order__order_no` 是它的单号）。⚠️⭐ **哪几处「看起来该在、实际不在」**（如实登记，免得后来者以为漏了）：`claimed_order_qs`（列表底集）**不在本表**：它的软删过滤写在**主表**上（`deleted_at__isnull=True`，主体就是 `Order`），而它那个相关子查询用的是 `order=OuterRef("pk")`（**实例条件**、不是 `order__` 字段路径）⇒ 本网一个字都收不到；它的 `Order.objects` 落点登记在**上面那张** `ORDER_READ_SITES` 表里（两张表**各自**计数，⛔ 别把一处新落点同时塞进两张表）。
    #   · `my_orders_views.py` **两处都没有**：`updated_since` 过滤的 `updated_at__gte` 不是 `order__` join、`select_related("game")` 也不是；两个端点都是"先调 `order_scope`、再装配"。· `MyOrderDetailView.get` 的 `claimed_order_with_price_qs(...).get(pk=…)` 同样不是 `order__` 路径（按主键取一行）。
    ("takerapi/api/order_images_views.py", "TakerOrderImagesView.get"): 2,
    # `claimed_perf_of` = **3** 处：`order__order_no` + `order__hall_card__isnull`（隐藏单不算他接的单）+ `order__upstream_hall_card__isnull`（上家镜像那一族同理）。
    ("takerapi/api/order_scope.py", "claimed_perf_of"): 3,
    # ⭐⭐ 兜底对账（计划 `docs/plans/2026-10-22-reconcile-stall-fix.md`，G1–G4）的 **3 处**。判据逐处：· `_candidates` **2 处**：候选从「Python 里逐行过滤」改成**落到 SQL**（G3 要 `.count()` + `LIMIT/OFFSET`，Python 侧再过滤会让偏移量与行数**静默错位**）⇒ `order__deleted_at__isnull=True`（软删排除，D-0144 那条既有判据的 SQL 版）+ `exclude(order__status__in=CLOSED_STATUSES)`（「在途」那一条的 SQL 版）。
    #    · `reconcile_openapi_statuses` **1 处**：**最新优先那 15 张**的判据 `order__status=OrderStatus.WAITING_GRAB`（「我方还停在待抢单」）。它是在 `_candidates` 那个**已带 `deleted_at__isnull=True`** 的 queryset 上追加的 ⇒ 同一个 JOIN 出来的行照样不含软删单，⛔ 不需要再补一道（E-0021：没有用例能弄红的重复判据）。
    #    · `Command._run_binding` **1 处**：`--only-waiting` 那一档的 `order__status=OrderStatus.WAITING_GRAB`；候选来自 `jidanpu_member.pullable_links`（它本身就带 `order__deleted_at__isnull=True`）。
    ("business/jidanpu_openapi_reconcile.py", "_candidates"): 2,
    ("business/jidanpu_openapi_reconcile.py", "reconcile_openapi_statuses"): 1,
    ("business/management/commands/reconcile_member_statuses.py", "Command._run_binding"): 1,
    # ⭐ `fix_missing_taker_performance`：候选集的 **2 处** join：`order__status=OrderStatus.COMPLETED`（只修已完成的单）+ `order__deleted_at__isnull=True`（D-0144：已软删的单不在这条修复线里）。
    #    ⚠️ 两处都在 `Performance.objects.filter(...)` 上，且 `user__isnull=True` 那一条不是 `order__` 字段路径、不进本表。⚠️ 本命令另有 1 处 `Order.objects` 写点（`Command._repair` 按单列写回原完单时间，⛔ 不是 `save()` 整行覆盖）⇒ 登记在 `ORDER_READ_SITES` 那一格，⛔ 不进本表（本表只收 `order__` 字段路径）。
    (
        "business/management/commands/fix_missing_taker_performance.py",
        "Command._run_for_tenant",
    ): 2,
    # ⭐ 打手 App 按需同步群二维码：消费手工拉图请求时按 `(绑定, 我方单号)` 取链路——`order__order_no` 1 处 join。
    #    ⚠️ 它是**消费**侧的取行口（`jidanpu_sync` 每一拍调），与自动那一轮的 `_link_for_by_remote`（按上家单号取、没有 join）不是同一件事。
    #    ⚠️ 软删口径不在这里补：取到链路之后由 `_is_pullable(link)` 判（停跟踪 / 已软删 / 已退单三档都在那一处，见它的 docstring）。
    ("business/jidanpu_upstream.py", "run_pull_pics_requests"): 1,
    # ⭐ D-0296：优选单**接单闸**的完成数判据（`hall_preferred.completed_count_for`）的 **2 处** join：① `order__status=COMPLETED`——只数「已完成」的订单；② `order__deleted_at__isnull=True`——⚠️ 这一处**就是** D-0144 的软删排除：跨关系 JOIN ⛔ 不经过 `Order` 的默认 manager ⇒ 必须显式补（已删单上的 MAIN 行不算「完成过」；「新读路径必须带软删过滤并登记」那一族）。
    #    ⚠️ 绩效行自己的软删过滤（`deleted_at__isnull=True`）打在**主表** `Performance` 上、不含 `order__` ⇒ 本网收不到、也不该收（两张表各自计数）。
    #    ⚠️ 消费方是接单闸（takerapi 原生抢单 + 镜像接单两条路都过它），**只读**。
    ("business/hall_preferred.py", "completed_count_for"): 2,
    # ⭐ 第三方平台订单管理页读面：列表的底集（同一平台 + 订单未软删）被**两条口径**（扁平 / 聚合）共用 ⇒ 收在模块级 `_push_rows_queryset`，逐处数出来：① `select_related("order__game")` 1 个字符串常量；② `select_related("order__server")` 1 个；③ `order__deleted_at__isnull=True`（D-0144：从推送行起查、不经过 `Order` 的默认 manager ⇒ 跨关系 JOIN 必须显式排除已软删单）1 处；④ `select_related("order__order_source")` 1 个。
    #    ⇒ 4 处。⚠️ 它**是两份读口径的唯一软删闸**：删掉它，扁平 / 聚合两侧会一起漏出已软删的单（本网的重数会当场报红）。⚠️ 本网按「字符串常量里含 `order__` 一并收」的口径收——`order__order_source` 也是本函数里的一个 `order__` 路径。真值由本网当场报出，⛔ 不是按「应该要」推的。
    ("business/api/platform_push_views.py", "_push_rows_queryset"): 4,
    # ⭐ 扁平分支从共用底集（`_push_rows_queryset`）之后**只剩**关键字这一处 join：
    #    `Q(order__order_no__icontains=…)` 1 处（`serial_no` 不是 `order__` 路径 ⇒ 不收）。
    #    ⚠️ 底集三处登记在上一格；本格多登会以「已不存在的落点」报红，⛔ 不是放宽。
    ("business/api/platform_push_views.py", "PlatformPushListView.get"): 1,
    # ⭐ 聚合分支（`_grouped_page`）的关键字比扁平多认**来源单号** ⇒ 2 处 join：① `Q(order__order_no__icontains=…)`；② `Q(order__source_order_no__icontains=…)`。底集那 3 处仍走 `_push_rows_queryset`（⛔ 不在本格重复登记）。
    ("business/api/platform_push_views.py", "PlatformPushListView._grouped_page"): 2,
    # ⭐ 页签批量计数：`PlatformPushCountsView.get` 同样从 `DailiantongPush` 起查、经 `order__` 读：① 基集 `order__deleted_at__isnull=True`（D-0144，与列表同口径）+ ② 关键字 `Q(order__order_no__icontains=…)` ⇒ 2 处。
    #    ⚠️ 本行由本网当场报出后登记，⛔ 不是按「应该要」推的。
    ("business/api/platform_push_views.py", "PlatformPushCountsView.get"): 2,
    # ⭐ D-0342（转正来源号改写的存量补写）：镜像行取候选那一条的 `promoted_order__isnull=False` 1 处（扫到的是 "promoted_order__" 前缀里的 `order__` 子串）。
    #    ⚠️ **有意不带** `order__deleted_at__isnull=True`：软删口径由**逐行闸**判（候选循环里 `order.deleted_at is not None ⇒ skip`，见命令 `plan_rewrites` 判据 2）——镜像行的 join 条件上判不了另一张表的列语义，放逐行闸是一处、可弄红（`test_rewrite_promoted_source_no.py::test_skips_soft_deleted_orders`）。
    ("business/management/commands/rewrite_promoted_source_no.py", "plan_rewrites"): 1,
    # ⭐ D-0359（上家收款同步）：两条候选 join 各 **9** 处（关键字 7 + 常量 2）：`order__status` / `order__payment_status` / `order__deleted_at__isnull`（join 侧没有默认 manager 的软删过滤，显式补的一道）+ 夜间支的 `order__completed_at__gte` / `order__receipt_sync_notfound_count__lt` 或手动支的 `order__created_at__gte` / `order__created_at__lt`（各 2 处，两支互斥各算各的）+ `order_by("order__completed_at", "order__id")` 的 2 个字符串常量。
    #    ⚠️ **有意不排除隐藏单**（无 `hall_card__isnull` / `upstream_hall_card__isnull`）：计划 §2.2——①式/peer 通道不按 hall FK 排除（peer 镜像转正单本身可能带 hall FK，是正主单），候选按「来源链接」收、不按卡收。
    ("business/upstream_receipt_sync.py", "_jidanpu_link_jobs"): 9,
    ("business/upstream_receipt_sync.py", "_peer_jobs"): 9,
    # ⭐ V2 看板线：`hall_stay_data` 的「今日被抢速度」样本（从 `Performance` 起查 ⇒ 跨关系 JOIN
    #    ⛔ 不经过 `Order` 的默认 manager）= **4** 处落点：① `order__deleted_at__isnull=True`——D-0144
    #    的软删排除（已删订单不进被抢速度样本；在厅池/停留时长走 `Order.objects` 默认 manager 天然
    #    排除、不占本表）；② ③ 两根隐藏族钉子 `order__hall_card__isnull=False` /
    #    `order__upstream_hall_card__isnull=False`（与 `order_stats._window_rows` 同口径，同时在上面
    #    `HALL_SHADOW_EXCLUDED_SITES` 登记形状）；④ `F("order__created_at")` 字符串常量（停留时长
    #    `claimed_at − 订单创建时刻` 的表达式，本网按字符串常量收）。
    ("business/api/dashboard_views.py", "hall_stay_data"): 4,
    # ⭐ D-0303 同族（指派收单，`peer_inbound._collect_assigned_hall_orders`）：链行锚的两道隐藏单
    #    排除 `order__hall_card` / `order__upstream_hall_card`（照 `_local_order_for_peer_ref` 同款）
    #    ＋ `promoted_order__isnull`（镜像行锚的 FK 判空——语义不是打 Order 列，被扫描器按
    #    `order__` 子串收进来）⇒ 重数 3。⚠️ 软删口径不在这格补：锚出的主键随即过默认 manager 的
    #    `Order.objects` 读点（上面 `ORDER_READ_SITES` 同名格），已删单在那里取不到即放弃。
    ("business/peer_inbound.py", "_collect_assigned_hall_orders"): 3,
    # 内部拉取只携带本地单号，让删除前已接收的事件入聊天存档；用户读权限仍按当前订单另查。
    ("internal_chat/peer_chat.py", "_pull"): 1,
})

#: `all_objects` 的恰好白名单。条数以本表为准，不要在注释里写死。`Game.all_objects` 与 `Count("all_objects")` 在本仓不存在（`AttributeError` / `FieldError`）；反向关系只有 `orders`，`Count("orders")` 走 SQL JOIN、数得到已软删的行。
#: 租户级 `jidanpu_hall.consume_withdraw_event` 已随那条线删掉，不要加回这一格。
ALL_OBJECTS_SITES: collections.Counter[tuple[str, str]] = collections.Counter({
    # 内部事实可看删除标记供聊天归档，公开授权仍由正常订单详情范围判定。
    ("internal_chat/order_authority.py", "order_facts"): 1,
    ("internal_chat/order_authority.py", "authorize"): 2,
    ("internal_chat/order_intents.py", "remember_chat_facts"): 1,
    # 补偿任务须保留已软删订单的未发送快照，管理员可看到待补偿数量。
    ('wecom/notifications.py', 'prepare_order_status'): 1,
    ('wecom/notifications.py', '_recover_order_journal'): 2,
    ('business/peer_inbound.py', 'apply_events'): 1,
    ('business/peer_inbound.py', '_apply_peer_order_fields'): 1,
    ('wecom/external_pay_views.py', 'ExternalPayStatusView.get'): 1,
    # T3 落：回收站列表的底集（`Order.all_objects.filter(deleted_at__isnull=False)`）。
    ("business/selectors.py", "search_recycle_bin"): 1,
    # T3 落：详情 / 动作的取对象口（已删单也要能取到，交给 1002 出口）。
    ("business/api/order_views.py", "OrderViewSet._order_including_deleted"): 1,
    # 回收站只列已删订单，删恢复要能锁已删行并复核外部关联。
    ("openapi/api/integration_orders.py", "IntegrationOrderRecycleView.get"): 1,
    ("openapi/api/integration_orders.py", "_locked_order"): 1,
    ("openapi/api/integration_orders.py", "require_local_write"): 1,
    # ⭐ 撤下事件的取行口——软删单也要能撤（「本地删除 ⇒ 去鸡蛋铺把对应那张单退掉」）。⛔ 改回默认 manager 会让这条路永远 `failed` + 告警（看起来像上游不配合，实际是我方取不到行）。
    # ⭐ 本线撤单事件的取行口——同上一条同因：我方先撤回 / 删除的那张单**只剩链路行**能指认它（订单已不在默认读路径里），⛔ 用默认 manager 会永远退不掉。读到后再按链路行取对方单号去退。
    ("business/jidanpu_member.py", "_consume_withdraw"): 1,
    ("business/jidanpu_member.py", "_consume"): 1,
    # 罚款要查旧绑定的退役链路，防止在新店重复录单；当前罚款单仍走默认 manager。
    ("business/jidanpu_member.py", "_consume_assign"): 1,
    # 结束已失效的改派时，仅清理软删单上仍属同一事件的等待标记，恢复订单后不会卡在旧指派。
    ("business/jidanpu_direct_taker.py", "_finish"): 1,
    # 旧地址缺少店铺信息，补号前读取当前订单的退役副本以识别换绑。
    ("business/jidanpu_member_openapi.py", "_legacy_can_fill_id"): 1,
    # 旧地址按单号匹配时也要纳入退役副本，防止晚到的回调错配到另一家店。
    ("business/jidanpu_member_openapi.py", "_link_of"): 1,
    # ⭐ 快路链路点查（perf/fastpath-link-index）：`_link_index` 来源臂的订单预筛——按本轮日志行实际出现的来源单号圈订单 id（`source_order_no` OR `order_no`），再按 `binding + order_id__in` 取链路。必须 `all_objects`：与旧全量 `select_related("order")` 的 JOIN 语义逐字对齐——来源号命中**软删单**时链路仍进候选 ⇒ `_handle_alarm` 的 `deleted_at` 分支写 ORDER_MISSING 台账；默认 manager 会把那一路静默吞掉（旧行为是写台账）。判据：`test_jidanpu_member_fastpath` 的软删单用例（台账在=旧语义保住）。
    ("business/jidanpu_member.py", "_link_index"): 1,
    # B1（`GameSerializer.order_count`）：`g.orders.count()` 走反向外键 manager（**会被过滤**）⇒ 改成显式 `Order.all_objects.filter(game=g).count()`（`PROTECT` 引用是**物理**的，D4）。
    ("business/api/serializers.py", "GameSerializer"): 1,
    # B2（`ProjectCategorySerializer.order_count`）同上。
    ("business/api/serializers.py", "ProjectCategorySerializer"): 1,
    # B3（`ProjectSerializer.order_count`）同上。
    ("business/api/serializers.py", "ProjectSerializer"): 1,
    # `GameViewSet.count_annotations` 的 `order_count` 走 `Order.all_objects.filter(...)` 子查询——口径与旧 `Count("orders")` JOIN 形状一致（**刻意算上已软删的订单**：它们仍物理引用着游戏，删除警告要报完整的历史账；JOIN 不经默认 manager 的老理由在那条形状下成立，子查询形状下由 `all_objects` 显式表达）。
    ("business/api/config_views.py", "GameViewSet"): 1,
    # ⭐ 订单批量线：批量脚手架那两处 `Order.all_objects`——`resolve_orders` / `run_batch` 的 `include_deleted` 分支。必要性：`/batch/delete/` 的「已软删单也算合格（幂等成功）」要求取行口看得到已删单，而默认 manager 会把它们全部滤掉（那样批量删除会对已删单回 1002，与单笔 `/delete/` 不同口径）。
    #   ⚠️ `batch_price`（`include_deleted` 恒为假）⇒ 这两处今天走的是 `else` 那一半（默认 manager），但**分支本身**是为 A8 的 `batch_delete` 留的，故现在就登记（多登记一条会让 ③ 的「恰好相等」断言红，所以不能等）。
    ("business/api/order_batch.py", "resolve_orders"): 1,
    ("business/api/order_batch.py", "run_batch"): 1,
    ("business/api/order_batch.py", "load_split_batch_members"): 1,
    ("business/api/order_batch.py", "lock_split_batch_scope"): 1,
    # 撤单投递前复核按单号取单，且**必须看到已软删**的行——「单已软删 ⇒ 照发撤单」正是 spec §16.2 的一条分支 ⇒ 这里**有意**绕过默认 manager（`Order.all_objects.filter(order_no=…)`，1 处）。
    # ⭐ 这一处收在 `peer_inbound._local_order_for_peer_ref` 的 `include_deleted=True` 分支（业务方裁定：同步身份 = 来源**或**内置单号 ⇒ 解析口只此一处）。⛔ 不是放宽：用 `all_objects` 的理由一字未变（已软删的单也要能撤回给下家）；调用方 `peer_worker._recheck_withdraw` 显式传 `include_deleted=True`。
    ("business/peer_inbound.py", "_local_order_for_peer_ref"): 1,
    # ⭐ 上家订单改价记录线（spec `docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md` §D3）：端点要判「这一行的订单现在还在不在」（`is_deleted`）⇒ 当前页的 `order_no` **一次批量**回查，且**必须看到已软删**的行（已删单要显示「删」徽标、不可点）。
    # ⚠️ **行为观测等价、⛔ 别把本网读成「不用 all_objects 会显示错」**：换成默认 manager 时已软删行会从映射里缺席，而序列化器的兜底恰好也是 `True`（缺行 = 已不可查看）⇒ 端点上看到的结果相同（`test_upstream_price_change_log.py` 已如实登记这条等价变异）。保留 `all_objects` 的理由是**显式读出 `deleted_at`**（与兜底两件事分开），由**本结构网**钉住这个读点的存在；⛔ 那条端点行为用例杀不掉这个变异。
    ("business/api/upstream_price_log_views.py", "_deleted_order_nos"): 1,
    # ⭐ D-0357（G10）：删主单级联后 `split_id` 的**整组清扫**——`Order.all_objects.filter(split_id=…).update(split_id="")`。必须 `all_objects`：级联成员**刚在本事务里被软删**（默认 manager 看不到它们）而 R7 要求「恢复任一成员各自为独立单」⇒ 已删成员的组标识也要一并清掉。
    # 同一锁内重读目标（含已删），保证请求拿到旧实例时不会重复返还金额。
    ("business/recycle.py", "soft_delete_order"): 2,
    # 发货用原主单（即使软删），并沿用历史组员已经成功的发货回执。
    ("business/split_group.py", "shipping_main_order"): 1,
    ("business/split_group.py", "shipping_lineage_order_nos"): 1,
    ("business/split_group.py", "child_source_numbers"): 2,  # 回收站里的来源号继续占用。
    ("business/xgj_ship.py", "_order_from_upstream"): 1,
    # ⭐ D-0303：改价记录页按**来源单号**搜索的那半——`Order.all_objects.filter(source_order_no__icontains=key).values_list(...)` 子查询。
    #    ⚠️ **必须看到已软删**（与展示侧 `_deleted_order_nos` 同一口径）：软删单的留痕行还在列表里、也必须还能按它的来源号搜到；查默认 manager 会出现「行在列表里、按来源号却搜不到」的静默空洞。判据见 `test_upstream_price_change_log.py` 的来源号搜索两向用例。
    ("business/api/upstream_price_log_views.py", "UpstreamPriceChangeLogListView.get_queryset"): 1,
    # ⭐ D-0296：模拟单取号的唯一性判据——`Order.all_objects.filter(order_no=candidate).exists()`。
    #    ⚠️ **必须看到已软删**：`order_no` 列上是全表唯一约束、软删单仍占号；查默认 manager 会把「撞了已删单」漏放成假唯一（真落单那一刻才响亮炸）⇒ 唯一性判据只能查 `all_objects`（理由逐字写在函数 docstring 里）。
    ("business/hall_simulated.py", "_unique_display_order_no"): 1,
    # ⭐ D-0296：离厅快照的**原生载荷**取行——`Order.all_objects.filter(pk=order.pk)…`（re-annotate 后喂 `HallListSerializer`）。
    #    ⚠️ **必须看到已软删**：软删那个写点（`recycle.soft_delete_order`）是在「刚把 `deleted_at` 写下去」**之后**调本函数的，默认 manager 会把这一行滤掉 ⇒ 快照**静默丢一张灰卡**（与 `peer_worker._recheck_withdraw` 同一条「软删单也要能取到」的理由；取不到时 `LookupError` 响亮失败，⛔ 不静默降级）。
    ("business/hall_snapshot.py", "native_payload"): 1,
    # ⭐ 镜像卡隐藏单的取 / 建 / 复活**必须看到已软删的行**——① `all_objects.get_or_create(hall_card=card, …)`：软删之后再落卡要查回**同一张**（新建会撞 0099 的 OneToOne 唯一索引 ⇒ 500）；② `all_objects.filter(pk=…).update(…, deleted_at=None)`：复活的条件更新（默认 manager 的软删过滤会让它匹配 0 行）。
    ("business/hall_shadow.py", "ensure_for_card"): 2,
    # ⭐ D-0325：**上家镜像卡**派生隐藏单的同一套理由（照 `hall_shadow` 逐字）：① `_shadow_of`（取卡那张隐藏单，含软删行——复活判据要读 `deleted_at`）；② `ensure_for_card` 的取 / 建（`get_or_create`，撞 OneToOne 唯一索引）+ 复活条件更新。
    ("business/upstream_hall_shadow.py", "_shadow_of"): 1,
    ("business/upstream_hall_shadow.py", "ensure_for_card"): 2,
    # ⭐ D-0325：转正**复用优先支**——取这张卡的隐藏单（含软删行，复活收编）+ 复活的
    #    条件更新（清 `deleted_at` / `upstream_hall_card`）。默认 manager 看不见软删行
    #    ⇒ 会把「该接着用的那一张」当不存在、走新建支造出第二张。
    ("business/jidanpu_upstream.py", "promote_upstream_mirror"): 2,
    # ⭐ D-0360（订单审计线）：时间线聚合的两个读点——① 双号归一（`order_no ‖ source_order_no` 两臂）：审计语义**必须看到软删单**（退单/撤单单恰是排查常态对象，计划 §1.2 钉死走 `all_objects`）；② 拆单组成员枚举：组账要完整，已删成员的号也要列出来（只列号不展开）。
    # 审计仍要看到被上家换绑隔离的历史关联。
    ("audit/timeline.py", "_mirror_items"): 1,
    ("audit/timeline.py", "resolve_order_by_key"): 1,
    ("audit/timeline.py", "list_split_group_members"): 1,
    # ⭐ D-0370（2026-11-03，迁链命令 `migrate_member_link_to_survivor`）：两处——
    #   软删 / 已退单的占链老单**正是**处置对象（默认 manager 会把它们藏掉）；
    #   「存活候选」同口径（⛔ 不许把活链行迁给一张已删的单）。
    ("business/management/commands/migrate_member_link_to_survivor.py",
     "survivor_candidates_for"): 1,
    ("business/management/commands/migrate_member_link_to_survivor.py", "plan_tenant"): 1,
})


class _SiteScanner(ast.NodeVisitor):
    """扫 `apps/**` 的 AST，收集**(相对 apps 的路径, 限定作用域名)** 三类落点。
    - 只走**可执行语句**：`ast.Expr(Constant str)`（模块 / 类 / 函数的 docstring 与裸字符串语句）整棵子树跳过；注释根本不进 AST。作用域名**限定到定义它的那层**（`Class.method` / `outer.inner` / 顶层 `func`），于是同名的两个方法不会被并成一个落点。
    - `order__` 同时收**关键字参数名**（真实的 `filter(order__x=…)` 写法）与**字符串常量**（`game_scope("order__game_id")` / `Count("order__total_price")` / `**{…}` 之类）；`all_objects` 同时收**属性**（`Order.all_objects` / `g.all_objects`）与**字符串常量**（`Count("all_objects", …)` 的写法；本仓实测没有，留着是为了将来不静默漏掉）。"""

    def __init__(self, rel_path):
        self.rel_path = rel_path
        self._scope = []
        self.read_sites = []
        self.join_sites = []
        self.all_objects_sites = []

    def _key(self):
        return (self.rel_path, ".".join(self._scope) or "<module>")

    def visit_Expr(self, node):
        # docstring / 裸字符串语句：整棵子树不算可执行落点。
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_Attribute(self, node):
        if node.attr == "objects" and isinstance(node.value, ast.Name) and node.value.id == "Order":
            self.read_sites.append(self._key())
        if node.attr == "all_objects":
            self.all_objects_sites.append(self._key())
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg and "order__" in node.arg:
            self.join_sites.append(self._key())
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            if "order__" in node.value:
                self.join_sites.append(self._key())
            if node.value == "all_objects":
                self.all_objects_sites.append(self._key())


def scan_sites():
    """返回 `(read_sites, join_sites, all_objects_sites)` 三个 `Counter`。"""
    read_sites, join_sites, all_objects_sites = (
        collections.Counter(),
        collections.Counter(),
        collections.Counter(),
    )
    for path in sorted(APPS_DIR.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        scanner = _SiteScanner(str(path.relative_to(APPS_DIR)))
        scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
        read_sites.update(scanner.read_sites)
        join_sites.update(scanner.join_sites)
        all_objects_sites.update(scanner.all_objects_sites)
    return read_sites, join_sites, all_objects_sites


class OrderReadPathNetTests(SimpleTestCase):
    """三个集合各断言**两个方向**：多一处（未登记）与少一处（登记表腐烂）都红。"""

    def test_every_executable_order_objects_site_is_registered(self):
        found, _, _ = scan_sites()
        self.assertEqual(
            found - ORDER_READ_SITES,
            collections.Counter(),
            f"有未登记的 `Order.objects` 读点（多出来的落点 ⇒ 请读该处并决定加注释还是登记）："
            f"{dict(found - ORDER_READ_SITES)}",
        )
        self.assertEqual(
            ORDER_READ_SITES - found,
            collections.Counter(),
            f"`ORDER_READ_SITES` 里有已不存在的读点（重构后忘了更新登记表）："
            f"{dict(ORDER_READ_SITES - found)}",
        )
        self.assertEqual(found, ORDER_READ_SITES, "`Order.objects` 落点与登记表的重数不相等")

    def test_every_executable_order_join_site_is_registered(self):
        _, found, _ = scan_sites()
        self.assertEqual(
            found - ORDER_JOIN_SITES,
            collections.Counter(),
            f"有未登记的 `order__` join 落点（新读路径 ⇒ 补 `order__deleted_at__isnull=True` "
            f"并登记）：{dict(found - ORDER_JOIN_SITES)}",
        )
        self.assertEqual(
            ORDER_JOIN_SITES - found,
            collections.Counter(),
            f"`ORDER_JOIN_SITES` 里有已不存在的落点（重构后忘了更新登记表）："
            f"{dict(ORDER_JOIN_SITES - found)}",
        )
        self.assertEqual(found, ORDER_JOIN_SITES, "`order__` 落点与登记表的重数不相等")

    def test_all_objects_usage_is_exactly_the_whitelist(self):
        _, _, found = scan_sites()
        self.assertEqual(
            found,
            ALL_OBJECTS_SITES,
            "`all_objects` 的使用点与白名单不相等（多一处 ⇒ 说明为什么必须绕过默认 manager；"
            "少一处 ⇒ 删掉登记表里的那一行）",
        )


#: 面向人的订单读面**必须显式排除**镜像卡派生的隐藏单（`Order.hall_card`，以及 D-0325 起的 `Order.upstream_hall_card`——**上家鸡蛋铺镜像卡**那条线的隐藏单）。键 = (相对 `apps/` 的路径, 限定作用域名)；值 = 该作用域里必须出现的**关键字参数名元组**（两个隐藏族各一根钉子，⛔ 缺一根就红：上家镜像单会从这条读面漏出去）。
#: ⚠️ 判据不是「这一处有 `Order.objects`」（上面三张表已经管了），而是「它**真的排除了**隐藏单」——三个读面共用同一份数据的时代（列表 / 统计 / 导出同源）已经过去，每处各写一句的写法必须被钉住，否则漏一处就静默多出一批单（E-0011 / §5-G）。⭐⭐ 命中还必须满足**两个形状条件**——① 调用方法是 **`exclude`**（⛔ 不是 `filter`）；② 比较值是 **`False`**（`ast.Constant(value=False)`）。理由：`filter(hall_card__isnull=True)` 与 `exclude(hall_card__isnull=False)` 语义等价，反过来写（`filter(...=False)` / `exclude(...=True)`）就是**不排除**；只看「关键字在不在」时四种写法**全部**绿 ⇒ 网抓不到「有人在改这一行时把布尔翻反了」。
#: ⇒ **全部登记点统一成 `exclude(<field>__hall_card__isnull=False)`**，本网同时校验方法与比较值。⚠️ 改回 `filter(isnull=True)` 即使语义相同也会红——那是有意的：口径只留一种形状，网才分得清「排除」与「翻反」。
HALL_SHADOW_EXCLUDED_SITES: dict[tuple[str, str], tuple[str, ...]] = {
    # 企微退款提醒只可绑定正常订单，不能落到两个大厅来源派生的订单投影上。
    ("wecom/refund_notices.py", "_order_candidates"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    # ── 订单域 / 配置域 ───────────────────────────────────────────
    ("business/selectors.py", "search_orders"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/selectors.py", "search_hall"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/selectors.py", "search_recycle_bin"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/api/order_views.py", "OrderViewSet._order_including_deleted"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/api/order_batch.py", "resolve_orders"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    # ── 待归属 / 大厅快照 / 拉取 ───────────────────────────────────
    ("business/api/settlement_views.py", "UnattributedView.get_queryset"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/api/peer_views.py", "PeerHallListView.post"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/api/console_views.py", "home_overview_data"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/report_stats.py", "_base_orders"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    # ⭐ 订单管理页「同步收款」的**唯一取单口径**：隐藏单是镜像卡派生的订单投影，
    #    ⛔ 不进任务取单面（否则会给它们发上游查询、还可能把「交易成功」写成已结款）。
    #    形状是 `exclude(hall_card__isnull=False)`（方法 `exclude` + 值 `False`），与其余登记点同款。
    ("business/xgj_receipt_sync.py", "candidates_qs"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    # ⭐ 闲管家**推送接收端**的取单口：`_matching_orders` 被 22 收款与 23/24 退款两个分支共用；隐藏单的 `source_order_no` 是另一个系统的内部单号（上家镜像卡 = 上家内部号），真撞号时退款推送会绕过隐藏单生命周期的唯一收口人把隐藏单翻成 `REFUNDED`（已抛给下家的副本不撤）⇒ 两族都排，形状照上一格（`xgj_receipt_sync.candidates_qs`）同款。
    ("business/xgj_push.py", "_matching_orders"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    # ── 绩效 / 结算 / 统计（跨关系 JOIN，不过 `Order` 的默认 manager）────
    ("business/api/performance_views.py", "_performance_base_queryset"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    ("business/settlement.py", "_resolve_batch"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    ("business/settlement.py", "_reject_reasons"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    ("business/order_stats.py", "_window_rows"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    # ⭐ V2 看板「今日被抢速度」样本：docstring 明写与 `_window_rows` 同口径（排除软删 / 软删绩效 /
    #    两族隐藏单）⇒ 两根钉子落在该样本的查询上（`exclude(...isnull=False)` 形状）。
    ("business/api/dashboard_views.py", "hall_stay_data"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    # ── 鸡蛋铺线：归属集合 / 对账（含**有链路**的隐藏单）────────────────
    ("business/jidanpu_member.py", "orders_of"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("business/jidanpu_member.py", "reconcile"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    # ── 打手 App / 对外 OpenAPI ─────────────────────────────────────
    ("takerapi/api/order_scope.py", "claimed_order_qs"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("takerapi/api/order_scope.py", "claimed_perf_of"): (
        "order__hall_card__isnull", "order__upstream_hall_card__isnull",
    ),
    ("openapi/api/order_query_views.py", "ApiOrderView._orders_queryset"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("openapi/api/order_extra_views.py", "ApiOrderExtraView._live_order"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    ("openapi/api/order_write_views.py", "_live_order"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
    # ── 运维命令 ──────────────────────────────────────────────────
    ("business/management/commands/push_peer_hall_snapshot.py", "Command._push_schema"): (
        "hall_card__isnull", "upstream_hall_card__isnull",
    ),
}

#: 面向人的订单读面**必须不排除**隐藏单（详情面）——「隐藏 ≠ 不存在」：客服排障要按单号看得到它是哪张镜像卡派生的、以及它的留痕。
#: 值 = 为什么这一处必须**不**排除（登记表的另一半：⛔ 只登记「必须排除」的话，有人为了让列表更干净，把排除加到 `get_queryset()` 上就会把详情面一起挡掉）。
#: ⚠️ 这两处**没有**直接写 `exclude(...)`；`get_queryset()` 的判据是「把 `include_hall_shadow` 显式传给 `search_orders`」，由下面 `HallShadowDetailActionWhitelistTests` 按 AST 钉住「放行集 = retrieve + 全部 detail GET 动作」。
HALL_SHADOW_MUST_NOT_EXCLUDE_SITES: dict[tuple[str, str], str] = {
    ("business/api/order_views.py", "OrderViewSet.get_queryset"):
        "列表与 `get_object()` 共用的构造点：在这里无条件排除会把详情面（retrieve / audit）"
        "一起挡掉——隐藏单的可见性由 `include_hall_shadow` 按动作分流，不在这里写死",
    ("business/api/order_views.py", "OrderViewSet.audit"):
        "详情页的留痕出口（只读）：隐藏单的 `/audit/` 必须 200。它走 `get_object()`，"
        "被挡掉时前端详情页会回 1002、反复弹「记录不存在」",
}


def _resolve_scope_node(tree, scope, where=""):
    """在 AST 里按 `Class.method` / `func` 逐级取作用域节点；取不到 ⇒ 响亮失败。"""
    node = tree
    for part in scope.split("."):
        found = None
        for child in getattr(node, "body", []):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and child.name == part:
                found = child
                break
        if found is None:
            raise AssertionError(f"{where} 里找不到作用域 {scope}")
        node = found
    return node


def _hall_shadow_calls(node, needle):
    """作用域里**全部**带 `needle` 关键字的调用：`[(方法名, 值节点, 行号)]`。
    ⚠️ 同时收 `filter` 与 `exclude`（以及任何别的方法名）：判据要回答的是
    「这一行到底排没排除」，而不是「这个关键字在不在」。"""
    calls = []
    for child in ast.walk(node):
        if not (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)):
            continue
        for keyword in child.keywords:
            if keyword.arg == needle:
                calls.append((child.func.attr, keyword.value, keyword.lineno))
    return calls


def _exclusion_violations(node, needle):
    """**不符合 `exclude(<needle>=False)`** 的命中描述；空列表 = 合格。
    两条判据：① 方法名必须是 `exclude`（`filter(isnull=True)` 语义相同但形状不同，统一到一种）；② 比较值必须是**真的 `False`**（直接读 `ast.Constant.value`；`True` / `not x` 都不算）。
    """
    violations = []
    for method, value, lineno in _hall_shadow_calls(node, needle):
        if method != "exclude":
            violations.append(
                f"第 {lineno} 行：调用方法是 {method!r}（必须是 'exclude'）"
            )
        elif not (isinstance(value, ast.Constant) and value.value is False):
            violations.append(
                f"第 {lineno} 行：比较值不是 False（实际 {ast.dump(value)}）"
            )
    return violations


class HallShadowExclusionNetTests(SimpleTestCase):
    """⭐ 读面登记**加「必须排除」判据**，⛔ 不只数 `Order.objects` 的条数。
    「隐藏单不进列表」这条口径在多个读面各写一份（列表 / 大厅 / 回收站 / 待归属 / 拉取 / 对账 / 统计 / 绩效 / 结算 / 首页 / 报表 / 我的单 / 对外查单 / 铺底命令）。只登记「这里有一个读点」挡不住「有人新写了一个读面、忘了排除」——本网按 AST 在**指定作用域**里找那个关键字参数，并校验**方法名与比较值**（见 `_exclusion_violations`），找不到或形状翻反都红（作用域名限定到类 / 函数，同名方法不会互相冒充）。⭐ 另一半：`HALL_SHADOW_MUST_NOT_EXCLUDE_SITES` 钉住详情面**必须不排除**。
    """

    @staticmethod
    def _resolve(rel_path, scope):
        tree = ast.parse((APPS_DIR / rel_path).read_text(encoding="utf-8"))
        return _resolve_scope_node(tree, scope, where=rel_path)

    def test_every_registered_read_surface_excludes_hall_shadow_orders(self):
        for (rel_path, scope), needles in HALL_SHADOW_EXCLUDED_SITES.items():
            node = self._resolve(rel_path, scope)
            for needle in needles:
                calls = _hall_shadow_calls(node, needle)
                self.assertTrue(
                    calls,
                    f"{rel_path}:{scope} 没有显式排除隐藏单（缺 `{needle}`）："
                    "隐藏单会从这条读面漏出去",
                )
                self.assertEqual(
                    _exclusion_violations(node, needle),
                    [],
                    f"{rel_path}:{scope} 的隐藏单排除形状不对——"
                    "口径是 `exclude(<field>__hall_card__isnull=False)`（方法 exclude、值 False）",
                )

    def test_both_hidden_families_are_pinned_at_every_site(self):
        """⭐ D-0325：每个登记点必须**同时**钉两个隐藏族。
        peer 镜像卡（`hall_card`）与上家鸡蛋铺镜像卡（`upstream_hall_card`）是并列的两族隐藏单；漏钉一族 = 那一族从全部读面静默漏出去。⚠️ 两个 needle 的 `order__` 前缀必须**同形**（都带或都不带）：登记点过的是关系 JOIN 时两族都要带前缀。
        """
        for (rel_path, scope), needles in HALL_SHADOW_EXCLUDED_SITES.items():
            self.assertEqual(
                len(needles),
                2,
                f"{rel_path}:{scope} 必须恰好钉两族隐藏单（peer / 上家镜像），实测 {needles}",
            )
            self.assertEqual(
                len({needle.startswith("order__") for needle in needles}),
                1,
                f"{rel_path}:{scope} 两根钉子的 `order__` 前缀不同形：{needles}",
            )

    def test_the_detail_read_surfaces_do_not_exclude_hall_shadow_orders(self):
        for (rel_path, scope), why in HALL_SHADOW_MUST_NOT_EXCLUDE_SITES.items():
            node = self._resolve(rel_path, scope)
            offending = [
                f"第 {lineno} 行：{method}({needle}=…)"
                for needle in (
                    "hall_card__isnull",
                    "order__hall_card__isnull",
                    # ⭐ D-0325：上家镜像那一族同样不许在详情面排除（隐藏 ≠ 不存在）。
                    "upstream_hall_card__isnull",
                    "order__upstream_hall_card__isnull",
                )
                for method, _, lineno in _hall_shadow_calls(node, needle)
            ]
            self.assertEqual(
                offending,
                [],
                f"{rel_path}:{scope} **不该**在这里排除隐藏单（{why}）",
            )


class HallShadowDetailActionWhitelistTests(SimpleTestCase):
    """⭐ 结构钉子：`audit` 必须与 `retrieve` 一起放行。
    `OrderViewSet.get_queryset()` 用 `include_hall_shadow=(self.action in HALL_SHADOW_DETAIL_READ_ACTIONS)` 决定详情面看不看得到隐藏单。白名单写错（漏了 `audit`）的后果**不是报错**、而是详情页轮询 `/audit/` 一直回 1002 并反复弹「记录不存在」——所以这里按 AST 把「放行集」钉成**恰好集合**：放行集 = `{"retrieve"} ∪ {本视图全部 `@action(detail=True, methods=["get"])`}`；新增只读 detail 动作 ⇒ 必须显式加进白名单（网红 = 强制人做决定）；**混合方法**的 detail 动作（`methods=["get", "post"]`）一律红：白名单按 `self.action` 判，不按 HTTP 方法判 ⇒ 放行一个带写方法的动作等于给隐藏单开写门。`get_queryset()` 必须**真的用**这个集合（关键字 `include_hall_shadow` 的值里出现它）。
    """

    ORDER_VIEWS = APPS_DIR / "business/api/order_views.py"

    def _detail_get_actions(self):
        tree = ast.parse(self.ORDER_VIEWS.read_text(encoding="utf-8"))
        cls = _resolve_scope_node(tree, "OrderViewSet", where="order_views.py")
        reads, mixed = {}, {}
        for child in cls.body:
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in child.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Name)
                    and decorator.func.id == "action"
                ):
                    continue
                kwargs = {kw.arg: kw.value for kw in decorator.keywords}
                if not (
                    isinstance(kwargs.get("detail"), ast.Constant)
                    and kwargs["detail"].value is True
                ):
                    continue
                methods = [
                    item.value
                    for item in getattr(kwargs.get("methods"), "elts", [])
                    if isinstance(item, ast.Constant)
                ]
                if "get" in methods:
                    reads[child.name] = methods
                    if set(methods) - {"get", "head", "options"}:
                        mixed[child.name] = methods
        return reads, mixed, cls

    def test_the_whitelist_is_exactly_retrieve_plus_detail_get_actions(self):
        from apps.business.api.order_views import HALL_SHADOW_DETAIL_READ_ACTIONS

        reads, mixed, cls = self._detail_get_actions()
        self.assertEqual(
            mixed,
            {},
            "detail 动作同时注册了写方法 ⇒ 白名单按动作名放行会把写门一起打开："
            f"{mixed}",
        )
        self.assertEqual(
            set(HALL_SHADOW_DETAIL_READ_ACTIONS),
            {"retrieve"} | set(reads),
            "隐藏单详情面放行集必须恰好 = `retrieve` + 本视图全部 "
            f"`@action(detail=True, methods=['get'])`；实测 detail GET = {sorted(reads)}",
        )

        # 白名单必须真的接在 `get_queryset()` 上（⛔ 隔着两行的常量不会被消费）。
        get_queryset = _resolve_scope_node(cls, "get_queryset", where="OrderViewSet")
        wired = [
            keyword.value
            for child in ast.walk(get_queryset)
            if isinstance(child, ast.Call)
            for keyword in child.keywords
            if keyword.arg == "include_hall_shadow"
        ]
        self.assertTrue(wired, "`get_queryset()` 没有把 `include_hall_shadow` 传给 `search_orders`")
        self.assertIn(
            "HALL_SHADOW_DETAIL_READ_ACTIONS",
            ast.dump(wired[0]),
            "`include_hall_shadow` 的值必须由 `HALL_SHADOW_DETAIL_READ_ACTIONS` 决定"
            "（⛔ 另写一份字面量黑名单：两份清单必然漂移）",
        )


class HallShadowExclusionNetSelfTests(SimpleTestCase):
    """网自己的断言必须能失败（§5-G）：**故意写反 ⇒ 必须被本网判红**。
    把一处 `exclude(...=False)` 翻成 `filter(...=True)` ⇒ 本网红（已做过变异证明）；这里把同一件事落成**永久**用例，覆盖全部四种形态——「只看关键字在不在」的老实现会在后三条上全部假绿。
    """

    SOURCE = """
class Probe:
    def correct(self):
        return QS.exclude(hall_card__isnull=False)

    def filter_true(self):
        return QS.filter(hall_card__isnull=True)

    def filter_false(self):
        return QS.filter(hall_card__isnull=False)

    def exclude_true(self):
        return QS.exclude(hall_card__isnull=True)

    def not_a_constant(self):
        return QS.exclude(hall_card__isnull=(True is False))
"""

    _WRONG = ("filter_true", "filter_false", "exclude_true", "not_a_constant")

    def _node(self, scope):
        return _resolve_scope_node(ast.parse(self.SOURCE), scope, where="<probe>")

    def test_the_correct_shape_passes(self):
        self.assertEqual(_exclusion_violations(self._node("Probe.correct"), "hall_card__isnull"), [])

    def test_every_wrong_shape_is_rejected(self):
        for scope in self._WRONG:
            with self.subTest(scope=scope):
                self.assertTrue(
                    _exclusion_violations(
                        self._node(f"Probe.{scope}"), "hall_card__isnull"
                    ),
                    f"`{scope}` 形状写反却通过了网 ⇒ Important 6 的加严没有判别力",
                )
