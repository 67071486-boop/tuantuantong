"""不重复算钱（spec §7 / §11-S7）残留面：一条结构性否定 + §15-T23 的现场核对。

**同一笔业务只允许一端产生绩效 / 结算。** 本文件把它拆成可重跑、能失败的判据，
每条都打**数据库行**（E-0009 / E-0024），⛔ 不打 logger、⛔ 不做文本搜索
（结构性那条例外，它要的就是结构）。

⛔⭐ 本文件原有三条「钱」的判据已随租户级鸡蛋铺线整条拆除**
（`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`；原判据原文按 `AGENTS.md` §4
只增不改、保留在下面这段历史里）：

① 镜像单转正允许我方算钱（`jidanpu_hall.claim_remote` / `promote_mirror`）；
② B-1 按「他自己的配置」算钱、并把接单人写成 `JidanpuBinding.local_user` 那位本地人员
   （`jidanpu_hall.discover_remote_taken`）；
③ B-1 只写那一位本地人员（同上）。

这三条的被测函数**已随模块整份删除** ⇒ 留下它们只会得到「对不存在的东西打桩」的 ERROR。
⚠️ 两个重算名字（`recompute_temp_amount` 是 `recompute_order_amounts` 的薄包装）**仍然存在**
（都在 `apps/business/performance.py`），但今天**没有**在生产代码里调用它们的租户级同步链路
——存活的两条个人绑定线自己不碰绩效金额 ⇒ 这一条判据原样保留会变成恒真的假保证（E-0024），
一并退出。

**留下来的两件事与租户级那条线无关**：
- `NoPersonnelWriterStructureTests`：人员同步残留的结构否定（扫的是 `src/apps/**` 全树）；
- `ZeroAmountOwnerlessSettlementTests`：无主 MAIN 绩效行**结不掉账**（判据在结算与人工路径上，
  人工路径今天照样造得出无主行 ⇒ 这条判据**仍然承重**）。

⚠️ 夹具一律**合成值**（E-0077）。
"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    Performance,
)
from apps.business.settlement import SettleError, settle_performances

#: 夹具值一律合成（E-0077）。
ORDER_NO = "20260925000000000999"
#: 旧 spec 的 `kind` 词表里有、本线**没有写入方**的取值（控制方裁定 5）。
DEAD_KIND = "user.sync"
#: 旧 spec §4.3 的人员映射类——**已整表删除**（业务方裁定「人员同步 不需要 删掉记录」；
#: 删表迁移 = `business/0042`）。⚠️ 判据已由「应用代码零引用」**升级**成
#: 「连**定义**都不许再出现」——理由见 `NoPersonnelWriterStructureTests` 里那条用例的 docstring。
DEAD_CLASS = "JidanpuUserLink"


def _app_root() -> Path:
    """应用代码的根（`settings.BASE_DIR` = `src/`）。"""
    return Path(settings.BASE_DIR) / "apps"


def _app_python_files() -> list[Path]:
    """`src/apps/**` 下参与结构性检查的 `.py`：排除迁移（那是表结构的历史）与字节码。"""
    root = _app_root()
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts and "migrations" not in path.parts
    )


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """树里**作为文档字符串**出现的字符串常量节点 id。

    ⚠️ 结构性否定要排除它们：一段解释「为什么本线不写某个取值」的文档字符串
    （正是本仓的注释风格）不该被当成写入方。
    """
    found: set[int] = set()
    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, nodes):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            found.add(id(first.value))
    return found


# ── §15-T23 现场的共同夹具（⚠️ 只剩「无主 MAIN 行」这一件事）────────────────


class _OwnerlessMainRowFixture(TenantTestCase):
    """§15-T23 现场的共同夹具：一张 `WAITING_GRAB` 单 + 一条非 0 的**无主** MAIN 行。

    ⚠️ **2026-10-08 改准**：本类原为 `_RemoteTakenFixture`，它另外还造了「B-1 现场」
    （`hall.launch` 台账行 + `JidanpuBinding`）并靠 `jidanpu_hall.discover_remote_taken`
    造状态。租户级鸡蛋铺线已整条拆除 ⇒ 那些**只能由已删模块产生**的夹具随之退场；
    本类只留**结算那三条判据真正需要**的行，⛔ 判据一个字没改（人工路径今天照样造得出
    无主行，那三条仍然承重）。

    ⚠️ `self.local_user` / `self.bystander` 仍保留：三条判据各要一个「写上去就能结账」的人
    （阳性对照）与一个「不许被写进去的人」（反向对照）。
    """

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="测试游戏丙")
        #: 现场里**另有**一个我方账号：人工路径把他写进绩效行时不许被链路顺手改写。
        self.bystander = get_user_model().objects.create_user(
            username="bystander-money", password="pw-b"
        )
        #: 阳性对照那位本地人员（提成配成「订单价 20%」——保留原夹具的档案形状）。
        self.local_user = get_user_model().objects.create_user(
            username="b1-local-money", password="pw-l"
        )
        StaffProfile.objects.create(
            user=self.local_user, commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal("20.00"),
        )
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB,
            hall_price="100.00",
        )
        self.main = Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount="42.00"
        )

# ── 结构性否定：本期不新增任何人员写入方（spec §13.3）──────────────────────


class NoPersonnelWriterStructureTests(SimpleTestCase):
    """「人员同步」残留清理后的结构否定：**既不许有人员写入方，也不许有人事映射表**。

    ① `JidanpuUserLink` **已整表删除**（业务方裁定）⇒ 判据从「零引用」
       **升级成「连定义都不许再出现」** + 「模型注册里没有它」（见下面第一条用例）；
    ② 死 kind `user.sync` 在应用代码里仍**一个写入方都没有**（字符串常量层面）。
    两条都是**结构否定**（AST / 字面量扫描 / 模型注册），⛔ 不用「用户表计数不变」这类行为否定代替。
    ⚠️ 结构性断言自己也要能失败（E-0065）：本类两条都做过一次「弄红」证明（见 E4 报告）。
    """

    def test_the_scan_covers_the_application_tree(self):
        """哨兵：扫描面非空、且**包含定义处**那个文件（否则「零命中」是扫了个寂寞）。"""
        root = _app_root()
        files = _app_python_files()
        self.assertTrue(files)
        relative = {path.relative_to(root).as_posix() for path in files}
        self.assertIn("business/models.py", relative)
        # 迁移文件被排除在外（删表的 `0042` 与建表的 `0036` 里本来就有这个类名。
        # ⚠️ 那是**历史**，`AGENTS.md` §4 要求保留 ⇒ 它们不该被这条判据当成「又加回来了」）。
        self.assertFalse(any("migrations" in path.parts for path in files))

    def test_jidanpu_user_link_is_gone_from_the_application_tree(self):
        """`JidanpuUserLink` **已整表删除**：模型注册里没有它，应用代码里连**定义**都不许再有。

        ⚠️ **为什么这条要从「零引用」升级**（清残留）：旧判据的三种命中形式
        （`import` / `ast.Name` / `ast.Attribute` / 字符串常量）**都不覆盖定义处**——
        `models.py` 里的 `class JidanpuUserLink` 是 `ast.ClassDef`，类名是字符串属性、
        不是常量节点 ⇒ 旧网在「类还在、只是没人引用」时**照样绿**。今天那个类已删，
        判据必须能挡住「有人把它加回来」，否则它是一张假网（E-0065 / E-0024）。
        四路源码命中 + 一路**模型注册**命中（后者是「表真的不在 Django 里」那一半）：
        ① `import` 别名；② `ast.Name`；③ `ast.Attribute`；④ 字符串常量含类名；
        ⑤ `ast.ClassDef` 的**定义**；⑥ `apps.get_app_config("business")` 的模型清单。
        失败方向：在 `models.py` 里加回 `class JidanpuUserLink` ⇒ 本用例在 ⑤ / ⑥ 上红
        （E4 报告有该红证）；在任何应用代码里加任一种引用 ⇒ 在 ①–④ 上红。
        """
        root = _app_root()
        offenders: list[str] = []
        for path in _app_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = _docstring_constant_ids(tree)
            where = path.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if any(alias.name == DEAD_CLASS for alias in node.names):
                        offenders.append(f"{where}: import")
                elif isinstance(node, ast.Name) and node.id == DEAD_CLASS:
                    offenders.append(f"{where}: use")
                elif isinstance(node, ast.Attribute) and node.attr == DEAD_CLASS:
                    offenders.append(f"{where}: attr")
                elif isinstance(node, ast.ClassDef) and node.name == DEAD_CLASS:
                    offenders.append(f"{where}: definition")
                elif (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and DEAD_CLASS in node.value
                    and id(node) not in docstrings
                ):
                    offenders.append(f"{where}: string")
        self.assertEqual(offenders, [])
        # 模型注册那一路：源码扫不到「动态注册」，但它扫得到这里。
        model_names = {model.__name__ for model in apps.get_app_config("business").get_models()}
        self.assertNotIn(DEAD_CLASS, model_names)
        # 哨兵：注册清单非空（否则「不含」是在空集合上成立的）。
        # ⚠️ 哨兵不用 `JidanpuBinding`（那张表已随租户级鸡蛋铺线拆除；其余断言一个字未改）
        # 它已随租户级鸡蛋铺线整条拆除删除 ⇒ 换成**存活**的 `JidanpuMemberBinding`。
        # 哨兵的作用（证明这个集合非空）与它守的那条 `assertNotIn(DEAD_CLASS, …)` 都未变。
        self.assertIn("JidanpuMemberBinding", model_names)

    def test_the_dead_user_sync_kind_has_no_writer_in_application_code(self):
        """`user.sync` 这个取值在应用代码里**一个写入方都没有**（字符串常量层面）。

        ⚠️ **排除文档字符串**：本仓的注释风格会写「⛔ 不写 `user.sync`」这类解释，
        那是说明而不是写入方。排除之后剩下的是真正的字面量使用（`kind=...` / 赋值 / 比较）。
        失败方向：在应用代码里加一个 `"user.sync"` 常量 ⇒ 本用例红。
        """
        root = _app_root()
        offenders: list[str] = []
        for path in _app_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstrings = _docstring_constant_ids(tree)
            where = path.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and node.value == DEAD_KIND
                    and id(node) not in docstrings
                ):
                    offenders.append(where)
        self.assertEqual(offenders, [])


# ── §15-T23 的现场核对 + ruling 7（代练中而 MAIN 无主）─────────────────────


class ZeroAmountOwnerlessSettlementTests(_OwnerlessMainRowFixture):
    """§15-T23：**无主**（`user` 为 NULL）的 MAIN 绩效行能不能进结算。

    **读码结论**（file:line 见 Task 4 报告，本文件不复述行号）：
    - **结账动作**（`settlement.py` 第 2 步的 `rows` 取数）要求「订单已完成」**且**
      「归属人非空」⇒ 无主的行（不管 0 元还是非 0 元）**结不了账**；
    - ⚠️ **结算列表**（`api/settlement_views.py` 的 `get_queryset`）**没有这两条判据**
      ⇒ 无主行**会出现在清单里**——这是 §15-T23 里「外观上像重复计了一笔」的
      业务决策点；本 Task **只登记、不改结算**（控制方裁定 6）。

    ⚠️ **2026-10-08 改准**：原文在这里还记着一条「B-1 现场那条行有主 ⇒ `COMPLETED` 之后
    **能**结账」的正面判据（业务方新裁定）。B-1 随租户级鸡蛋铺线整条拆除 ⇒
    那条正面判据的**生产者**没了（它靠 `jidanpu_hall.discover_remote_taken` 造状态），
    该用例一并退场。**「无主行结不掉」这条判据本身仍然承重**（人工路径仍能造出无主行），
    下面照旧钉它——这才是本节真正守的东西。

    ⚠️ **评审建议的登记，⛔ 不改任何判据）**：被删的 `MirrorPromotionMoneyTests` /
    `RemoteTakenMoneyTests` 里那条「**接单人 = 绑定 `local_user`**」身份判据**没有补**——
    因为实测 `local_user` 在 `src/apps/**` 里**已零引用**（该语义随 B-1 一起删除；
    今天这个名字只剩在「加那一列」的那一笔历史迁移 `business/0053` 里，而那张表已随本线删除），
    补了就是**守一个不存在的行为**（照 `E-0024`：那种断言只会变成恒真的假保证）。
    ⚠️ 本类里那个 `self.local_user` 是**测试自己的**合成夹具（「写上去就能结账」的阳性对照），
    ⛔ 它与上面那条被删的身份判据**不是一回事**，⛔ 不要拿它去把那条判据补回来。

    下面三条都打**数据库行**：无主行结不掉（fail-closed）、已完成但无主也结不掉
    （钉 `user` 那条判据）、同一条行**写上归属人**就能结（阳性对照 ⇒ 证明挡住它的是
    「无主」而不是别的）。
    """

    def setUp(self):
        super().setUp()
        self.viewer = get_user_model().objects.create_user(
            username="finance-money", password="pw-c"
        )
        profile = StaffProfile.objects.create(user=self.viewer)
        profile.games.add(self.game)

    def _settle(self):
        return settle_performances([self.main.pk], actor="财务甲", user=self.viewer)

    def _zero_the_amounts(self):
        """把这条 MAIN 行写成**真 0 元**（夹具给的是 `42.00`，是「确实改过」的起始哨兵）。"""
        Performance.objects.filter(pk=self.main.pk).update(
            temp_amount=Decimal("0.00"), bonus_amount=Decimal("0.00")
        )
        self.main.refresh_from_db()

    def _complete_the_order(self):
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.COMPLETED)
        self.main.refresh_from_db()

    def test_a_nonexistent_performance_id_is_refused_with_1006(self):
        """⭐ **M6**（2026-09-29 独立评审点名缺）：**不存在的绩效 id** ⇒ `SettleError(1006)`。

        这是结账第 3 步「fail-closed **逐 id** 判定」的另一半：`1006` 不只覆盖「行在、
        但不合规」，也覆盖「这个 id 压根查不到」——⛔ 不许静默跳过（跳过 = 请求方以为结掉了，
        钱却一分没动）。

        ⚠️ 与上面两条**刻意分家**：那两条钉的是「行存在、只是无主 / 订单未完成」这一族；
        本条钉的是「id 根本不存在」那一族（走的是同一个出口，但**前提不同**）。
        失败方向：把第 3 步那条 `if unqualified:` 去掉 ⇒ 本用例在「没抛错」上红
        （空批会被当成「结清了 0 行」静默成功）。
        """
        ghost_id = self.main.pk + 999999
        # 哨兵：这个 id 真的查不到（否则本用例测的是「那条行不合规」那一族）。
        self.assertFalse(Performance.objects.filter(pk=ghost_id).exists())
        with self.assertRaises(SettleError) as ctx:
            settle_performances([ghost_id], actor="财务甲", user=self.viewer)
        self.assertEqual(ctx.exception.code, 1006)

    def test_a_zero_amount_ownerless_row_is_refused_even_when_the_order_is_complete(self):
        """钉住「**归属人非空**」那条判据本身：订单改成已完成、金额写成 **0 元**，
        只留**无主**这一个不合规项 ⇒ 仍然结不掉。

        失败方向：把 `settlement.py` 第 2 步 `rows` 取数里的 `user__isnull=False` 删掉
        ⇒ 本用例红（那条 0 元无主行会被结掉，正是 T23 要防的「同一单两端都算钱」外观）。
        """
        self._zero_the_amounts()
        self._complete_the_order()
        # 前置哨兵：这一行确实是**0 元 + 无主 + 已完成**（否则本用例测的不是那条判据）。
        self.assertEqual(self.main.temp_amount, Decimal("0.00"))
        self.assertEqual(self.main.bonus_amount, Decimal("0.00"))
        self.assertIsNone(self.main.user_id)
        self.assertEqual(self.main.order.status, OrderStatus.COMPLETED)

        with self.assertRaises(SettleError) as ctx:
            self._settle()
        self.assertEqual(ctx.exception.code, 1006)

        self.main.refresh_from_db()
        self.assertEqual(self.main.settle_status, "未结账")
        self.assertIsNone(self.main.settled_amount)

    def test_the_same_zero_amount_row_settles_once_it_has_an_owner(self):
        """阳性对照：与上一条**同一形态**（已完成 + 0 元）的行，**只改归属人一个变量**就能结账。

        ⚠️ 这条是判别力的来源：两条用例在 `_settle()` 之前做的是**同一串**准备
        （`_zero_the_amounts()` + `_complete_the_order()`），唯一差别只有 `user`
        —— 没有它，「结不掉」可能只是被「订单未完成」或「游戏不可见」挡住。
        """
        self._zero_the_amounts()
        self._complete_the_order()
        # 唯一变化的变量：写上归属人（金额一个字不动）。
        Performance.objects.filter(pk=self.main.pk).update(user=self.bystander)
        self.main.refresh_from_db()

        self._settle()

        self.main.refresh_from_db()
        self.assertEqual(self.main.settle_status, "已结账")
        self.assertEqual(self.main.settled_amount, Decimal("0.00"))
