"""权限接线位点表的结构兜底（T1–T5 + 位点真实性）。

设计：`docs/specs/2026-09-21-rbac-wiring-map-design.md` §5。被测对象是
`apps/rbac/wiring.py`（单一出处）与它的生成物 `docs/RBAC-WIRING-MAP.md`。

**证明力边界（别把这套测试当行为证明）**：T2 只能证明「判据**装**在了那里」
（该 code 出现在那个方法/那条装配里），**不能**证明那条装配真的拦得住——
拦不拦由各接线线的行为测试负责。同族教训见 E-0011。
"""
import ast
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase

from apps.rbac import wiring
from apps.rbac.catalog import NODES
from apps.rbac.management.commands.render_rbac_wiring_map import DOC_REL, render_wiring_map

CODES = [n[0] for n in NODES]


def _anchor_parts(path: str):
    """把 `相对路径::锚点` 拆成 (文件, 锚点)。格式错就返回 `None`（由调用方报错）。"""
    if "::" not in path:
        return None
    file_part, anchor = path.split("::", 1)
    if not file_part or not anchor:
        return None
    return file_part, anchor


def _menu_permissions():
    """`config/menu.ts` 里 (路由名 → 声明的 code 集合)。

    逐行解析：实测 66 条 `permissions:` 声明**都在单行对象里**且同一行带 `routeName`。
    行内同时出现两者才算一条——格式将来变了这条会红，而不是静默漏掉。
    """
    text = (Path(settings.REPO_ROOT) / "src/frontend/src/config/menu.ts").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        if "permissions:" not in line or "routeName:" not in line:
            continue
        route = re.search(r"routeName:\s*'([^']+)'", line)
        block = re.search(r"permissions:\s*\[([^\]]*)\]", line)
        if not route or not block:
            continue
        rows[route.group(1)] = set(re.findall(r"'([a-z_0-9]+(?:\.[a-z_0-9]+)?)'", block.group(1)))
    return rows


#: 订单视图的**权限装配位点**（单笔与批量的闸都装在这一处）。T7 用它判断
#: 「某个节点的位点里有没有那一条批量动作」。
ORDER_VIEWS_REL = "src/apps/business/api/order_views.py"
ORDER_VIEWS_ANCHOR = f"{ORDER_VIEWS_REL}::OrderViewSet.get_permissions"


def _order_permissions_batch_actions():
    """从**源码 AST** 读 `OrderViewSet.ORDER_PERMISSIONS` 里的批量动作：`batch_*` → 节点 code 元组。

    ⚠️ 走 AST 而不是 import 视图类：本文件是 `SimpleTestCase`（不建租户、不碰数据库），
    而且 AST 正是本文件 T2 的既有取法（含「一层类体常量解引用」那条）。
    ⛔ **不许**改成读文本 / 裸 grep —— 那样注释或 docstring 里写一句 `batch_delete`
    就能满足 T7（T2 的「执行期修订 6」记过这类**假保证**的代价）。
    """
    module = ast.parse(
        (Path(settings.REPO_ROOT) / ORDER_VIEWS_REL).read_text(encoding="utf-8")
    )
    table = None
    for cls in ast.walk(module):
        if not (isinstance(cls, ast.ClassDef) and cls.name == "OrderViewSet"):
            continue
        for item in cls.body:
            if (
                isinstance(item, ast.Assign)
                and isinstance(item.targets[0], ast.Name)
                and item.targets[0].id == "ORDER_PERMISSIONS"
            ):
                table = item.value
    assert table is not None, (
        f"{ORDER_VIEWS_REL} 里找不到 OrderViewSet.ORDER_PERMISSIONS —— T7 的取值锚点失效了"
    )
    rows = {}
    for key, value in zip(table.keys, table.values):
        action = getattr(key, "value", None)
        if not isinstance(action, str) or not action.startswith("batch_"):
            continue
        rows[action] = tuple(
            sub.value for sub in ast.walk(value)
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
        )
    return rows


class WiringMapTests(SimpleTestCase):
    """不碰数据库：全部是静态事实（注册表 + 源码 + 生成物）。"""

    # ---- T1 覆盖：双向严格，且两态互斥 ----
    def test_t1_covers_every_code_exactly_once(self):
        """覆盖 + 互斥。⚠️ **2026-09-23 口径修订（Task 8，控制方裁定）**。

        原口径是「`SITES` 与 `PENDING` **完全互斥**」。那条口径会逼出一条坏选择：一个点若
        「菜单在 `menu.ts` 里**确实**声明了 permissions、但服务端没判据」，要登记拦截原因就
        只能把真实的 `EXPERIENCE` 位点**删掉**——地图读者会以为连菜单都没接。⇒ 改成：

        - **`INTERCEPT` 位点与 `PENDING` 互斥**（拦截层要么接了、要么待定，只能二选一）；
        - **`EXPERIENCE` 位点可与 `PENDING` 并存**（允许「有体验位点 + 在 `PENDING` 里写清
          拦截层为什么没有」）；
        - `SITES ∪ PENDING` 仍**恰好**是目录全集。

        松的只是「体验层不许与拦截待定并存」这条**会丢信息**的约束；拦截层的互斥没有松。
        """
        sites, pending = set(wiring.SITES), set(wiring.PENDING)
        self.assertEqual(sites - set(CODES), set(), "wiring 里有 catalog 不存在的 code")
        self.assertEqual(set(CODES) - (sites | pending), set(), "catalog 里有没登记的 code")
        intercept_codes = {
            code for code, entries in wiring.SITES.items()
            if any(s.layer == wiring.INTERCEPT for s in entries)
        }
        self.assertEqual(
            intercept_codes & pending, set(),
            "同一个 code 既有 INTERCEPT 位点、又在 PENDING 里写「拦截层待定」（只能二选一）",
        )
        # 目录条数本身（防"顺手改常量"；读数以断言为准，裁剪与自有节点的逐条登记见
        # `catalog.py` 的模块 docstring）。⭐ 第一条自有节点
        # `system.allow_taker_open_api`，spec §7.1）。真值由本条网自己报出（先跑一次它报
        # `AssertionError: 151 != 150`），⛔ 不按「150 + 1」推算。
        # ⭐ 系统开关线（D-0223）：151 → **152**（新增自有节点 `system.edit_settings`，
        #    `SITES` 同批登记；真值由本网自己报出，门禁阶段当场核对）。
        # ⭐ 首页二维码横幅线（D-0262）：152 → **153**（第三条自有节点
        #    `home.qr_manage`，`SITES` 同批登记两条拦截位点）。
        # ⭐ QQ 机器人线：153 → **154**（第四条自有节点
        #    `system.qqbot_settings`，`SITES` 同批登记；真值由本网自己报出）。
        # ⭐ 打手钱包 / 提现审核线：154 → **155**（第五条自有节点
        #    `finance.taker_withdraw_review`，`SITES` 同批登记；真值由本网自己报出）。
        #    （第六条 `finance.taker_wallet_adjust` 的 155 → 156 只改了字面量、未补注记。）
        # ⭐ 闲管家收款明细线：156 → **157**（第七条自有节点
        #    `finance.xgj_receipt_detail`，`SITES` 同批登记两个拦截位点 + 一个体验位点）。
        # ⭐ 交易平台涉钱写动作线（审计 M5）：158 → **159**（第九条自有节点
        #    `order.platform_write`，`SITES` 同批登记写端点位点；读端点仍在 order.list）。
        # ⭐ 频道文章作品线：157 → **158**（第八条自有节点
        #    `system.article_manage`，`SITES` 同批登记四个拦截位点 + 一个体验位点；
        #    体验位点指向 `config/menu.ts::app-mp-articles`，那条菜单在同一个提交里声明了
        #    同一个 code ⇒ T2b 的「位点指向的路由名真的声明了该 code」成立）。
        # ⭐ 客户维护组收敛（2026-11-11 业务方拍板）：159 → **151**
        #    （删 8 个无功能 / 已删功能点，迁移 `0015` 删库；PENDING 同批清行）。
        # ⭐ 客户线第五批：151 → **152**（自有限制节点 `customer.member_list_self_only`，
        #    迁移 `0016` 落行；SITES 同批登记 1 体验 + 4 拦截位点）。
        # ⭐ 企微自建应用线（D-0379）：152 → **153**（自有节点 `system.wecom_settings`，
        #    迁移 `0017` 落行；SITES 同批登记 1 体验 + 1 拦截位点）。
        self.assertEqual(len(CODES), 157)

    # ---- T5 层取值合法（拼错一个就红） ----
    def test_t5_layers_are_known(self):
        for code, entries in wiring.SITES.items():
            self.assertTrue(entries, f"{code} 的位点为空元组：已定就必须给出位点")
            for site in entries:
                self.assertIn(site.layer, wiring.LAYERS, f"{code} 的层取值非法：{site.layer}")

    # ---- 位点真实性（spec I4）：文件被 git 跟踪 + 锚点真的存在 ----
    def test_site_files_are_tracked(self):
        tracked = set(
            subprocess.run(
                ["git", "-C", str(settings.REPO_ROOT), "ls-files"],
                check=True, capture_output=True, text=True,
            ).stdout.split()
        )
        for code, entries in wiring.SITES.items():
            for site in entries:
                parts = _anchor_parts(site.path)
                self.assertIsNotNone(parts, f"{code} 的位点格式不是 <文件>::<锚点>：{site.path}")
                self.assertIn(parts[0], tracked, f"{code} 的位点文件未被 git 跟踪：{parts[0]}")

    # ---- T2 拦截层位点可命中（AST，不是裸 grep） ----
    def test_t2_intercept_sites_really_carry_the_code(self):
        """判据：锚点方法**真的是权限装配**（含 `has_node(` 调用或 `permission_classes`），
        且该 code 出现在它的取值里。

        允许**一层模块常量解引用**——`apps/rbac/views.py` 的写法是
        `node = ACTION_NODES.get(self.action, DEFAULT_NODE)`，字面量在模块常量里而不在方法体内。
        不做这层解析会把真实位点判成假；而放宽成「整个文件里出现过」又等于裸 grep，会失去判据力。

        锚点有两种形态——
        `类.方法`（授权装配写在方法里）**或** `类.类属性`（授权装配直接写在类体上，如
        `apps/audit/api/views.py::OperationLogListView.permission_classes = [has_node("system.logs")]`）。
        修订 **5** 只认类体里的 `Assign` 当**常量解引用**，锚点本身仍必须是 `FunctionDef` ⇒
        `system.logs` 这种「判据真在、只是写在类属性上」的真实位点会被判成**假**
        （与修订 5 同一类失败：把真位点判假，而它不是放宽判据力）。
        改动只有一处：`类.方法` 的候选里，`Assign` 且目标名 == 锚点成员名的**类体赋值**也算一个候选。
        判据力不变——仍然是「**那一个成员节点内**出现该 code 且它本身是授权装配」，
        不是「文件里出现过就行」（裸 grep）。
        """
        #: 判据的「授权出口」白名单。这是有意放宽：
        #: `has_node_now(` / `flag_enabled(` 是 `apps/rbac/permissions.py` 与
        #: `apps/business/order_flags.py` 的**授权判据本身**，只是不走「工厂 + permission_classes」
        #: 那条装配路径。`flag` 类点按 RBAC spec §4.2 **不进** `permission_classes`（它的失败形态是
        #: 过滤 / 脱敏，不是 403）⇒ 只认 `has_node(` 会把**真实的 flag 拦截位点判成假**。
        #: 放宽的边界：仍要求「**函数体里出现一次授权判据调用**」，不是「文件里出现过就行」（那是裸 grep）。
        auth_calls = {"has_node", "has_node_now", "flag_enabled"}
        for code, entries in wiring.SITES.items():
            intercepts = [s for s in entries if s.layer == wiring.INTERCEPT]
            if not intercepts:
                continue
            for site in intercepts:
                file_part, anchor = _anchor_parts(site.path)
                module = ast.parse((Path(settings.REPO_ROOT) / file_part).read_text(encoding="utf-8"))

                # 常量解引用（一层）：模块级 `Assign` ∪ **所在类体**的 `Assign`。
                # 类体那一支：逐 action 的节点表写成
                # `OrderViewSet.ORDER_PERMISSIONS` 这样的**类属性**、在 `get_permissions()` 里用
                # `self.ORDER_PERMISSIONS` 取；只扫模块体会把真实位点判成假。
                consts = {}
                for node in module.body:
                    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                        consts[node.targets[0].id] = {
                            sub.value for sub in ast.walk(node.value)
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        }
                    if isinstance(node, ast.ClassDef):
                        for sub_item in node.body:
                            if isinstance(sub_item, ast.Assign) and isinstance(
                                sub_item.targets[0], ast.Name
                            ):
                                consts[f"{node.name}.{sub_item.targets[0].id}"] = {
                                    sub.value for sub in ast.walk(sub_item.value)
                                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                                }

                def _scan(fn, cls_name=None):
                    """扫一个函数体：返回 `(是否含该 code, 是否含授权判据)`。

                    `cls_name` 给定时，`self.X` 这类属性访问会按 `类名.X` 去查类体常量；
                    否则按裸名查模块级常量。
                    """
                    literals, names, assembly = set(), set(), False
                    for sub in ast.walk(fn):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            literals.add(sub.value)
                        elif isinstance(sub, ast.Name):
                            names.add(sub.id)
                        elif isinstance(sub, ast.Attribute) and sub.attr == "permission_classes":
                            assembly = True
                        elif isinstance(sub, ast.Attribute):
                            # `self.ORDER_PERMISSIONS` / `Bare.CONST` 这类属性访问也算一次常量引用
                            names.add(sub.attr)
                            if cls_name:
                                names.add(f"{cls_name}.{sub.attr}")
                        elif isinstance(sub, ast.Call) and getattr(sub.func, "id", "") in auth_calls:
                            assembly = True
                    for name in names:
                        literals |= consts.get(name, set())
                    return code in literals, assembly

                # 锚点两种形态：`类.方法`（含类体常量一层解引用）或**模块级函数名**。
                # 后者同为修订 5：`order_flags.py` 的读取点是模块级函数
                # （`scope_by_creator` / `can_view_taker_price` …），没有类可挂。
                # ⚠️ `类.成员` 的成员既可以是方法，也可以是**类体上的权限装配**（修订 6，
                #    见 docstring：`permission_classes = [has_node("…")]` 直接写在类体上）。
                if "." in anchor:
                    class_name, member_name = anchor.split(".", 1)
                    members = [
                        item
                        for cls in ast.walk(module)
                        if isinstance(cls, ast.ClassDef) and cls.name == class_name
                        for item in cls.body
                        if (isinstance(item, ast.FunctionDef) and item.name == member_name)
                        or (
                            isinstance(item, ast.Assign)
                            and any(
                                isinstance(t, ast.Name) and t.id == member_name
                                for t in item.targets
                            )
                        )
                    ]
                    hits = [_scan(item, class_name) for item in members]
                else:
                    hits = [
                        _scan(node)
                        for node in module.body
                        if isinstance(node, ast.FunctionDef) and node.name == anchor
                    ]
                self.assertTrue(hits, f"{code} 的拦截位点锚点在该文件里找不到：{file_part}::{anchor}")
                self.assertTrue(
                    any(h[1] for h in hits),
                    f"{code} 的拦截位点不是授权判据（既无 {'/'.join(sorted(auth_calls))} 调用，"
                    f"也无 permission_classes）：{anchor}",
                )
                self.assertTrue(
                    any(h[0] for h in hits),
                    f"{code} 的拦截位点不成立：{anchor}（含一层模块/类体常量）里找不到该 code",
                )

    def test_t2b_experience_sites_match_menu_declaration(self):
        menu = _menu_permissions()
        seen = set()
        for code, entries in wiring.SITES.items():
            for site in entries:
                if site.layer != wiring.EXPERIENCE:
                    continue
                file_part, route = _anchor_parts(site.path)
                self.assertEqual(file_part, "src/frontend/src/config/menu.ts", f"{code} 的体验层位点不在菜单配置里")
                self.assertIn(route, menu, f"{code} 的体验层位点指向不存在的路由名：{route}")
                self.assertIn(code, menu[route], f"{code} 并未出现在路由 {route} 的 permissions 声明里")
                seen.add(code)
        self.assertTrue(seen, "一个体验层位点都没有？菜单接线是本文件的重要事实之一")

    # ---- T7（D-0160 批量线）：批量动作必须在它那几个节点的位点里**具名** ----
    def test_t7_every_batch_action_is_named_at_its_node_site(self):
        """`OrderViewSet.ORDER_PERMISSIONS` 的每个 `batch_*`：它的**每个**节点 code 都必须在
        `wiring.py` 的对应位点里被点名。

        判据三件（逐条都能单独失败）：

        ① **恰好 9 条**批量动作（字面量，独立于 spec §D6 的契约；删一条会红）；
        ② 每个节点 code 在 `wiring.py` 有 `SITES` 位点，**其中至少一条锚在
           `OrderViewSet.get_permissions`** —— 批量端点的闸就装在那里；🚨 **这是 T2 抓不到的那一格**：
           T2 只问「**已有位点**里那个 code 出现了没有」，它**不问**「新消费方有没有被登记」
           ⇒ 一条新的批量子动作接进 `ORDER_PERMISSIONS` 而地图上完全不提，T2 照样全绿
           （实测：`order.online_refund` 在 Task 10 之前只有 `refund_views` 那一处位点）。
        ③ 那条位点的 `note`（或 path）里**出现该动作名**（例：`"delete 动作"` →
           `"delete + batch_delete 两个动作"`）——位点对了、描述停在单笔动作上，
           读者读到的仍是**旧事实**（「半新半旧」的形状）。

        ## 证明力边界（⛔ 别读成「批量端点的权限真的拦得住」）

        本条只证明**接线表与动作表的登记一致**（谁消费谁、说明写没写准）。
        「拦不拦得住」由行为用例负责：`tests/test_order_batch_isolation.py` 的
        `NoNodeTests`（9 条各一次 403 + 零写入）。同族教训见 E-0011。

        ## 为什么锚点必须精确到 `OrderViewSet.get_permissions`

        锚点写松（例如只要求「某个位点的 note 里出现过这个动作名」）会让**别的文件**里
        一句说明满足它 ⇒ 判据变成「文字里出现过」。精确到锚点才等价于
        「**装配点**被登记了」，与本表其余部分的语义一致（T2 同口径）。
        """
        rows = _order_permissions_batch_actions()
        self.assertEqual(
            sorted(rows),
            [
                "batch_assign", "batch_delete", "batch_delete_v2", "batch_hall_price",
                "batch_payment_status", "batch_price", "batch_recompute_commission",
                "batch_refunds", "batch_transition", "batch_types",
            ],
            "批量动作集合变了——先核 spec §D6 的 9 条与 wiring.py 的位点，再改这个字面量",
        )
        for action, codes in sorted(rows.items()):
            for code in codes:
                entries = wiring.SITES.get(code, ())
                self.assertTrue(
                    entries, f"{action} 的节点 {code} 在 wiring.py 里没有任何位点"
                )
                hits = [site for site in entries if site.path == ORDER_VIEWS_ANCHOR]
                self.assertTrue(
                    hits,
                    f"{action} 的节点 {code} 在 {ORDER_VIEWS_ANCHOR} 没有位点——"
                    f"批量端点的闸就装在那里，地图上看不见它（T2 对「新消费方没登记」这一格是瞎的）",
                )
                self.assertTrue(
                    any(action in f"{site.note} {site.path}" for site in hits),
                    f"{code} 在 {ORDER_VIEWS_ANCHOR} 的位点说明里没有点名 {action}——"
                    f"描述文字停在单笔动作上（位点对了、读者读到的是旧事实）",
                )

    # ---- T3 available 是合取，且"已接未翻真"只允许在飞集合 ----
    def test_t3_available_requires_intercept_site(self):
        for code, _bid, _name, _note, _url, _kind, available in NODES:
            has_intercept = any(s.layer == wiring.INTERCEPT for s in wiring.SITES.get(code, ()))
            if available:
                self.assertTrue(has_intercept, f"{code} 标了 available=True，却没有拦截层位点")
            elif has_intercept:
                self.assertIn(
                    code, wiring.IN_FLIGHT,
                    f"{code} 已有拦截位点却没翻 available，且不在 IN_FLIGHT（静默失效的形状）",
                )

    def test_t3b_in_flight_only_exempts(self):
        # 豁免集合本身要能被解释：它必须全部是 catalog 里的 code，且当前一律未翻真
        self.assertTrue(set(wiring.IN_FLIGHT) <= set(CODES), "IN_FLIGHT 里有 catalog 不存在的 code")
        for code in wiring.IN_FLIGHT:
            node = next(n for n in NODES if n[0] == code)
            self.assertFalse(node[6], f"{code} 已翻真，应从 IN_FLIGHT 移除（该集合只登记未完成的）")

    # ---- T6 反向缺口：有菜单可见性、服务端却不确定有判据 ----
    def test_t6_no_experience_only_node_without_a_registered_reason(self):
        """**T3 只查单向**（`available=True` ⇒ 有 `INTERCEPT`），于是「**有菜单可见性、
        服务端却敞着**」这一类**完全静默**——`available=False` 算不上违反，图上（生成物）
        也看不出这是「刻意只有体验层」还是「漏了判据」（评审 I-1 牵出的同族形状）。

        判据：**只有体验层位点、又不在 `PENDING`、且 `available=False` 的 code 集合必须为空**。
        - 「只有体验层位点」= 在 `SITES` 里但没有任何 `INTERCEPT`；
        - 「不在 `PENDING`」= 连**拦截层**的阻塞原因都没登记（⚠️ 按 T1 的新口径，
          `EXPERIENCE` 位点**可以**与 `PENDING` 并存——「有体验位点 + 有拦截原因」是**正常**形态，
          不再需要二选一）；
        - `available=True` 却没有 `INTERCEPT` 归 **T3** 管，本判据不重复报。

        这一类今天必须是空集。豁免只能走下面的显式枚举，不能靠改宽条件把旧点藏起来。

        ⚠️ **豁免只能走 `EXPERIENCE_ONLY_ALLOWED` 的显式枚举**（今天为空集）。
        ⛔ **不要**改成「原因文本里含某个词」或「kind == PAGE」这类宽条件——
        那等于给一批点永久豁免，提醒会静默失效（E-0065 的同族）。
        """
        offenders = []
        for code, _bid, _name, _note, _url, _kind, available in NODES:
            entries = wiring.SITES.get(code, ())
            if not entries:
                continue  # 不在 SITES ⇒ 必在 PENDING（T1 的并集），已登记原因
            if any(s.layer == wiring.INTERCEPT for s in entries):
                continue
            if code in wiring.EXPERIENCE_ONLY_ALLOWED:
                continue
            if code in wiring.PENDING:
                continue
            if not available:
                offenders.append(code)
        self.assertEqual(
            offenders, [],
            "这些点只有体验层位点（菜单可见性）、没有拦截位点、又没登记阻塞原因 ⇒ "
            "「有菜单、服务端却敞着」的静默缺口。处置：补 `INTERCEPT` 位点（判据真的接上了）"
            f"或写进 `PENDING`（功能未实现/无独立位点）：{offenders}",
        )

    def test_t6b_experience_only_exemptions_are_explicit(self):
        """豁免集合本身要能被解释（与 T3b 对 `IN_FLIGHT` 的口径同形）。

        ⚠️ 它今天**是空集**——所以本判据今天**平凡通过**，它的价值只在于「将来真有人往
        `EXPERIENCE_ONLY_ALLOWED` 里塞条目时，塞错会被抓住」：条目必须在目录里，且确实是
        「只有体验层位点」的点（真接上 `INTERCEPT` 后就该从豁免集合删掉，否则豁免会一直挂着）。
        """
        self.assertTrue(
            set(wiring.EXPERIENCE_ONLY_ALLOWED) <= set(CODES),
            "EXPERIENCE_ONLY_ALLOWED 里有 catalog 不存在的 code",
        )
        for code in wiring.EXPERIENCE_ONLY_ALLOWED:
            entries = wiring.SITES.get(code, ())
            self.assertTrue(entries, f"{code} 在豁免集合里却没有任何位点（那不属于「只有体验层」）")
            self.assertFalse(
                any(s.layer == wiring.INTERCEPT for s in entries),
                f"{code} 已有拦截位点，应从 EXPERIENCE_ONLY_ALLOWED 删掉（它不再需要豁免）",
            )

    # ---- T4 生成物新鲜（逐字）+ 命令的 --check 出口可用 ----
    def test_t4_generated_doc_is_fresh(self):
        path = Path(settings.REPO_ROOT) / DOC_REL
        self.assertTrue(path.exists(), f"{DOC_REL} 不存在：跑 manage.py render_rbac_wiring_map")
        self.assertEqual(
            path.read_text(encoding="utf-8"), render_wiring_map(),
            f"{DOC_REL} 与 apps/rbac/wiring.py 不一致（过期或被手改）",
        )

    def test_t4b_check_command_passes(self):
        # 覆盖命令自身的出口（--check 不一致时要非零退出）
        call_command("render_rbac_wiring_map", "--check")
