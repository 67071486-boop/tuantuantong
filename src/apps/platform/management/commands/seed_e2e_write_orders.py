"""`seed_e2e_write_orders`：给一次性写租户补几笔确定性订单夹具（spec `docs/specs/2026-09-25-write-mode-audit-design.md` §1/§4/§7）。写模式的目的正是把「保存成功之后才出现」的界面纳入网内，而 `seed_e2e` 一笔订单都不造 ⇒ 写租户订单页只剩工具栏、行内控件（指派/改派/抛单/撤回/改状态/改价…）一个都渲染不出来。本命令只补订单夹具，⛔ 不碰配置与账号（那是 `seed_e2e` 的职责），必须排在它之后跑。
⛔ 两道闸门（与 `seed_e2e` 同形、fail-closed）：DEBUG 为假 ⇒ 拒绝；schema 必须在显式白名单 `WRITE_ORDER_SCHEMAS`（只有 `e2e_write`）里 ⇒ 拒绝——只读租户 `e2e` 的订单是读模式覆盖下界的实测基线，⛔ 在它上面造单 = 改掉只读模式的基线。幂等：订单号是合成常量（`7777` 开头 20 位数字，⛔ 不用带时间戳+随机数的 `generate_order_no()`），每笔按 `order_no` upsert；每笔形状照领域函数（`create_performance_for_order` 建订单+MAIN 行、`set_taker` 摆已指派、`reset_taker_performance` 清池内两态，D-0032 池内 ⇒ 无接单人），状态取 `OrderStatus` 成员。"""
from decimal import Decimal
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import tenant_context

from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    OrderType,
    Performance,
    Project,
    ProjectCategory,
)
from apps.business.performance import (
    create_customer_performance,
    create_performance_for_order,
    reset_taker_performance,
    set_taker,
)
from apps.platform.management.commands.seed_e2e import (
    E2E_CATEGORY,
    E2E_GAME,
    E2E_PROJECT,
)
from apps.platform.models import Tenant

#: 本命令**唯一**认的目标 schema。⚠️ 比 `seed_e2e.E2E_SCHEMAS` 更窄（见模块 docstring 的闸门 2）。
WRITE_ORDER_SCHEMA = "e2e_write"
#: E2E 专用 schema 的**显式白名单**（判据 = 集合成员，⛔ 不是前缀匹配）。
#: 形状照 `seed_e2e.E2E_SCHEMAS`；新增一个值必须同时改 `tests/test_seed_e2e_write_orders.py`
#: 里**独立写死**的那份期望（那份不是从本常量派生的）。
WRITE_ORDER_SCHEMAS = frozenset({"e2e_write"})

#: 夹具要用到的两个 E2E 账号（都由 `seed_e2e` 建；缺一个就响亮拒绝，⛔ 不自己造账号）。
TAKER_USERNAME = "taker001"
PUBLISHER_USERNAME = "cs001"
#: 这两个账号必须都在——`taker001` 是「已指派」那一笔的接手人，`cs001` 是发单人 / 销售。
REQUIRED_USERNAMES = (TAKER_USERNAME, PUBLISHER_USERNAME)

#: 夹具订单的唯一出处（订单号/状态/接手人/发单人/项目名）。订单号是合成常量：`7777` 开头、20 位纯数字，与真实生成器 `YYYYMMDD…` 形状刻意不同（一眼可辨夹具）、满足 `unique` 与详情路由 `\d{20}`；⛔ 不用 `generate_order_no()`（带时间戳+随机数 ⇒ 每次新单号，见模块 docstring「幂等」段）。覆盖口径（spec `2026-09-25-write-mode-audit-design.md` §7：
#: 至少两种状态、行内控件每一支都能渲染）：`UNASSIGNED`/`WAITING_GRAB` 落「发单价」两支（`changeThrowFeeEdit` 没发单人 / `changeThrowFee` 有发单人或已指派，判据 `order-orderlist.ts::TAKER_PERF_BRANCH`）；
#: `IN_PROGRESS` 出用户名+「清除接单人」；`COMPLETED` 给「只看已完成」列数据。⛔ 状态值一律取 `OrderStatus` 成员、「已指派」一律经 `set_taker`（理由见模块 docstring）。
ORDER_SPECS = (
    {
        "order_no": "77770000000000000001",
        "label": "未指派",
        "status": OrderStatus.UNASSIGNED,
        "taker": None,
        "publisher": None,
    },
    {
        "order_no": "77770000000000000002",
        "label": "待抢单",
        "status": OrderStatus.WAITING_GRAB,
        "taker": None,
        "publisher": PUBLISHER_USERNAME,
    },
    {
        "order_no": "77770000000000000003",
        "label": "代练中",
        "status": OrderStatus.IN_PROGRESS,
        "taker": TAKER_USERNAME,
        "publisher": PUBLISHER_USERNAME,
    },
    {
        "order_no": "77770000000000000004",
        "label": "已完成",
        "status": OrderStatus.COMPLETED,
        "taker": TAKER_USERNAME,
        "publisher": PUBLISHER_USERNAME,
    },
)

#: 夹具订单的金额：`total_price`（订单总价）与 `hall_price`（发单价 = 接单价，D3）。
#: ⚠️ `88.00` 与 `taker001` 的提成（`ORDER_PERCENT` × `E2E_COMMISSION_VALUE=10`）相乘得
#: `8.80`，与只读租户上那批订单同值（`set_taker` 会按这个数重算 `temp_amount`）。
FIXTURE_TOTAL_PRICE = Decimal("100.00")
FIXTURE_HALL_PRICE = Decimal("88.00")


def _assert_write_order_schema(schema: str) -> None:
    """闸门（纯函数）：目标 schema 必须在显式白名单 `WRITE_ORDER_SCHEMAS` 里，否则响亮拒绝。
    ⛔ 判据是集合成员、不是前缀（`e2e`、`e2e_write_extra` 一类都得在门外）；抽成纯函数为了让白名单能被直接钉住（`tests/test_seed_e2e_write_orders.py`）——被改成 `startswith("e2e")` 那类隐式豁免时白名单用例必须红。"""
    if schema not in WRITE_ORDER_SCHEMAS:
        raise CommandError(
            f"seed_e2e_write_orders 只认一次性写租户 schema，显式白名单 "
            f"{sorted(WRITE_ORDER_SCHEMAS)}，收到的是 {schema!r}"
            "（⛔ 拒绝对只读租户 e2e / 开发库造单——那会改掉读模式的基线数据）。"
        )


def _fixture_fields(spec, *, game, category, project, users):
    """把一条 spec 摊成一笔订单的全部夹具列。「创建」与「重跑时写回」共用这一份（两处各写一份必然漂移，E-0021 的形状）。
    `sales_user`/`created_by` 一律 `cs001`——本仓录单由客服发起，只读租户上那批订单也是这个形状，照抄。"""
    status = str(spec["status"])
    publisher = users[spec["publisher"]] if spec["publisher"] else None
    sales_user = users[PUBLISHER_USERNAME]
    completed = status == str(OrderStatus.COMPLETED)
    return {
        "game": game,
        "category": category,
        "project": project,
        # `project_text` 是**权威值**（D9），列表页渲染的就是它 ⇒ 每笔给一个可辨认的名字。
        "project_text": f"E2E 写夹具·{spec['label']}",
        "status": status,
        "order_type": str(OrderType.BOOST),
        "total_price": FIXTURE_TOTAL_PRICE,
        "hall_price": FIXTURE_HALL_PRICE,
        "content": f"E2E 写租户订单夹具（{spec['label']}）",
        "game_account": f"e2e-write-{spec['order_no'][-4:]}",
        "game_role_name": f"E2E 角色·{spec['label']}",
        "sales_user": sales_user,
        "created_by": sales_user.username,
        "hall_publisher": publisher,
        # 完单时间只该在「已完成」上有值（`apply_transition` 同一条口径：进已完成写、离开清）。
        "completed_at": timezone.now() if completed else None,
    }


def _ensure_performances(order, *, spec, users):
    """把这一笔的绩效行摆成 spec 要求的样子（幂等：已经是那个样子就不写——重复跑不该把 `claimed_at` 抖掉）。
    两条写入都用领域函数（`create_performance_for_order`/`set_taker`/`reset_taker_performance`），⛔ 不手写 `Performance.user` 列（理由见模块 docstring）。"""
    main = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
    if main is None:
        # 录单路径唯一的建行函数：MAIN 行 + 客服行一起建。
        main = create_performance_for_order(order)
    elif order.sales_user_id is not None and not Performance.objects.filter(
        order=order, emp_type=EmpType.SALES
    ).exists():
        # 补一条缺失的客服行。⚠️ `create_customer_performance` **自己不去重**
        # （它是 `create`，不是 `get_or_create`）⇒ 这里必须先判存在，否则重跑会撞
        # `uniq_performance_order_user`。
        create_customer_performance(order)

    if spec["taker"] is None:
        if main.user_id is not None:
            reset_taker_performance(main)
        return
    taker = users[spec["taker"]]
    if main.user_id != taker.id:
        set_taker(main, taker)


def _assert_fixture_orders_are_usable() -> None:
    """收尾校验（真的接在生产路径上）：夹具订单真的落库、状态对、接手人对。产物是下一次审计的数据基线，
    「订单在、但接单人没写上」在页面上表现为「接手人格显示『指派打手』」——审计只会读到少一支控件、不会红 ⇒ 这种静默少覆盖必须由本命令自己响亮拒绝。
    ⚠️ 判据全部重读数据库（⛔ 不信内存里刚构造的对象）：写入被谁吞掉时它才判得出来。"""
    problems = []
    statuses = set()
    for spec in ORDER_SPECS:
        order = Order.objects.filter(order_no=spec["order_no"]).first()
        if order is None:
            problems.append(f"{spec['order_no']}：订单没落库")
            continue
        statuses.add(str(order.status))
        if str(order.status) != str(spec["status"]):
            problems.append(
                f"{spec['order_no']}：状态是 {order.status}，期望 {spec['status']}"
            )
        main = Performance.objects.filter(order=order, emp_type=EmpType.MAIN).first()
        if main is None:
            problems.append(
                f"{spec['order_no']}：没有 MAIN 绩效行（行内「指派打手 / 改派」那一格点不开）"
            )
            continue
        actual = main.user.username if main.user_id is not None else None
        if actual != spec["taker"]:
            problems.append(
                f"{spec['order_no']}：接手人是 {actual!r}，期望 {spec['taker']!r}"
            )
    if len(statuses) < 2:
        problems.append(
            f"只造出 {sorted(statuses)} 这一种状态——订单页行内控件的两支渲染不出来"
        )
    if problems:
        raise CommandError(
            "seed_e2e_write_orders 种出来的订单夹具不可用（写模式的覆盖基线就建在它上面）："
            + "；".join(problems)
        )


def _upsert_fixture_orders(tenant):
    """在 `tenant` 的 schema 里 upsert 整套订单夹具，返回一份能打印的摘要。
    整段写入在一个事务里：中途任何一步抛错 ⇒ 整体回滚，不留「一半种好」的租户（与 `seed_e2e._seed_tenant_fixtures` 同一纪律）。"""
    summaries = []
    with tenant_context(tenant), transaction.atomic():
        game = Game.objects.filter(name=E2E_GAME).first()
        if game is None:
            raise CommandError(
                f"找不到 E2E 游戏「{E2E_GAME}」——本命令只补订单，不建配置 / 账号："
                "先跑 `seed_e2e --schema e2e_write`（`reset_e2e_write` 里已经排好了顺序）。"
            )
        category = ProjectCategory.objects.filter(game=game, name=E2E_CATEGORY).first()
        project = (
            Project.objects.filter(category=category, name=E2E_PROJECT).first()
            if category is not None
            else None
        )

        user_model = get_user_model()
        users = {
            user.username: user
            for user in user_model.objects.filter(username__in=REQUIRED_USERNAMES)
        }
        missing = sorted(set(REQUIRED_USERNAMES) - set(users))
        if missing:
            raise CommandError(
                f"E2E 账号 {missing!r} 不存在——本命令不建账号："
                "先跑 `seed_e2e --schema e2e_write`。"
            )

        for spec in ORDER_SPECS:
            fields = _fixture_fields(
                spec, game=game, category=category, project=project, users=users
            )
            order, created = Order.objects.get_or_create(
                order_no=spec["order_no"], defaults=fields
            )
            if not created:
                for key, value in fields.items():
                    setattr(order, key, value)
                # `updated_at` 是 `auto_now`，不列进 `update_fields` 就不会被写。
                order.save(update_fields=[*fields.keys(), "updated_at"])
            _ensure_performances(order, spec=spec, users=users)

        _assert_fixture_orders_are_usable()

        for spec in ORDER_SPECS:
            order = Order.objects.get(order_no=spec["order_no"])
            main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
            summaries.append(
                {
                    "order_no": order.order_no,
                    "status": str(order.status),
                    "taker": main.user.username if main.user_id is not None else None,
                }
            )
    return summaries


class Command(BaseCommand):
    help = (
        "给一次性写租户灌确定性的订单夹具（幂等；DEBUG=false 或 schema 不在 "
        f"{sorted(WRITE_ORDER_SCHEMAS)} 时拒绝）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema",
            default=WRITE_ORDER_SCHEMA,
            help=(
                f"目标租户 schema；本命令只接受白名单 {sorted(WRITE_ORDER_SCHEMAS)} 里的值"
                "（该参数只作闸门跳板，别的值一律拒绝）"
            ),
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_e2e_write_orders 只在 DEBUG=true 下运行——它会在租户库里造订单，"
                "绝不能碰非开发环境。"
            )
        schema = options["schema"]
        _assert_write_order_schema(schema)

        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(
                f"schema {schema!r} 不存在——写租户由 `reset_e2e_write` 建"
                "（它内部会先跑 seed_e2e 再调本命令）。"
            )

        summaries = _upsert_fixture_orders(tenant)

        self.stdout.write(
            f"✅ 写租户订单夹具已就绪（库={settings.DATABASES['default']['NAME']}，"
            f"schema={schema}）：{len(summaries)} 笔确定性订单"
        )
        for row in summaries:
            taker = row["taker"] if row["taker"] is not None else "（无）"
            self.stdout.write(f"  - {row['order_no']} | {row['status']} | 接手人={taker}")
