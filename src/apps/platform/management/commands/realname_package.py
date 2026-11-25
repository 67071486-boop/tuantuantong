"""`realname_package`：平台侧实名认证密钥包录入/拨额度入口（子命令 `add`/`list`/`grant`/`quota`；基线 spec `docs/specs/2026-10-10-realname-verification-design.md` §2-B2/B3、§4.1）。spec §9.2 原写平台侧走 Django admin，但本仓装不了 admin（`django.contrib.admin` 不在 INSTALLED_APPS 且明写不许装回；平台控制台已独立 `../ttt-platform`）⇒ 录入面用管理命令，判据在 `tests/test_realname_package_command.py`；`add`/`list` 的校验与掩码抽在 `apps/realname/package_admin.py`（与租户侧端点 `realname-packages/` 同一出处，⛔ 两份实现；本命令行为与输出一字不变）。
⛔ `list`/`quota` 绝不打印 `key_secret`（本命令最贵的约束）：它只写不读、拿它就能核光买来的额度 ⇒ 输出只回 `key_id` 前 4 位 + `***`（`_mask_key_id`，与 `RealNamePackage.__str__` 同源）；判据钉「输出里不含库里那一行的密钥原文」，不是常量比常量。⚠️ 显式 `schema_context("public")`：django-tenants 会在每个租户 schema 建 SHARED_APPS 的同名影子表且 `search_path` 先命中当前租户 ⇒ 不显式切会静默写进影子表（「bug」与「静默数据分叉」是两种坏，这里消灭后一种）。
⚠️ `grant` 用行锁 + `F()`、⛔ 不是读-改-写：并发读-改-写会丢更新（照本仓钱那条先例 `refunds.py` 的 `F("refund_fee")` 与 `order_status.py` 的 `select_for_update()`）；判据 = 并发用例两个连接各 grant 一次 ⇒ `granted` 恰好 +2。"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.platform.models import RealNameGrant, RealNamePackage, Tenant, _mask_key_id
from apps.realname.package_admin import PackageAdminError, add_package, list_packages


def _display_local(moment) -> str:
    """把时间戳打成本地可读形状（`None` ⇒ 「不过期」）。"""
    if moment is None:
        return "不过期"
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M")


class Command(BaseCommand):
    help = "平台侧实名认证密钥包的录入 / 余量查看，以及给租户拨额度（子命令：add / list / grant / quota）"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", metavar="{add,list,grant,quota}")
        sub.required = True

        add = sub.add_parser("add", help="新增一个密钥包（key_id 已存在 ⇒ 报错，⛔ 不静默覆盖）")
        add.add_argument("--key-id", dest="key_id", required=True, help="厂商给的密钥 Id")
        add.add_argument("--key-secret", dest="key_secret", required=True, help="厂商给的密钥 Key")
        add.add_argument(
            "--total-quota", dest="total_quota", type=int, required=True, help="购入总次数（必须 ≥ 1）"
        )
        add.add_argument("--remark", default="", help="备注（选包 / 对账时看得懂就行）")
        add.add_argument(
            "--expires-at",
            dest="expires_at",
            default=None,
            help="套餐有效期（如 2027-10-10 或 '2027-10-10 12:30'）；不给 = 不过期",
        )

        sub.add_parser("list", help="列出全部密钥包与余量（⛔ 不打印 key_secret）")

        grant = sub.add_parser("grant", help="给某租户拨额度（累加；租户不存在 ⇒ 报错）")
        grant.add_argument("--tenant", required=True, help="租户 schema 名（如 demo）")
        grant.add_argument("--times", type=int, required=True, help="本次拨入次数（必须 ≥ 1）")

        quota = sub.add_parser("quota", help="看某租户还能核几次（⛔ 不打印 key_secret）")
        quota.add_argument("--tenant", required=True, help="租户 schema 名（如 demo）")

    def handle(self, *args, **options):
        action = options["action"]
        # 见模块 docstring：两张表以 public 那一份为唯一出处，⛔ 不跟着调用方的连接状态走。
        with schema_context(get_public_schema_name()):
            if action == "add":
                self._handle_add(options)
            elif action == "list":
                self._handle_list()
            elif action == "grant":
                self._handle_grant(options)
            elif action == "quota":
                self._handle_quota(options)
            else:  # pragma: no cover —— argparse 的 choices 已经挡住，兜一下防将来加子命令漏接
                raise CommandError(f"未知子命令：{action}")

    # ── add ──────────────────────────────────────────────────────────────
    def _handle_add(self, options):
        # ⭐ T3：校验与创建**全部**在 service（`apps/realname/package_admin.add_package`）
        # ——与租户侧端点同一出处；`PackageAdminError` 的文案就是命令的既有输出
        # （逐字，E-0024 口径），这里只负责翻译成 `CommandError`（manage.py 退出码 1 那条路）。
        try:
            pkg = add_package(
                key_id=options["key_id"],
                key_secret=options["key_secret"],
                total_quota=options["total_quota"],
                expires_at=options["expires_at"],
                remark=options["remark"],
            )
        except PackageAdminError as exc:
            raise CommandError(str(exc)) from exc
        # ⛔ 输出里不回 key_secret，连它的片段都不回（掩码只打在 key_id 上）。
        self.stdout.write(
            self.style.SUCCESS(
                f"已录入密钥包 #{pkg.pk}：密钥Id {_mask_key_id(pkg.key_id)}，"
                f"总次数 {pkg.total_quota}，有效期 {_display_local(pkg.expires_at)}，"
                f"备注 {pkg.remark or '（无备注）'}"
            )
        )

    # ── list ─────────────────────────────────────────────────────────────
    def _handle_list(self):
        # ⭐ T3：掩码行**同一出处**（`list_packages()`，行里没有 key_secret 这个键）。
        # 本方法只负责把 service 的行渲染成既有的逐行输出（一字不变）。
        packages = list_packages()
        if not packages:
            self.stdout.write("还没有录入任何密钥包（用 realname_package add 录入）。")
            return
        self.stdout.write(f"共 {len(packages)} 个密钥包：")
        for pkg in packages:
            self.stdout.write(
                "  #{id} 密钥Id {kid} 余量 {left}/{total} 已消耗 {used} "
                "有效期 {exp} 状态 {status} 备注 {remark}".format(
                    id=pkg["id"],
                    kid=pkg["key_id"],
                    left=pkg["total_quota"] - pkg["used_quota"],
                    total=pkg["total_quota"],
                    used=pkg["used_quota"],
                    exp=_display_local(pkg["expires_at"]),
                    status="启用" if pkg["is_active"] else "停用",
                    remark=pkg["remark"] or "（无备注）",
                )
            )
        self.stdout.write("（⛔ 密钥 Key 一律不回显；要换密钥用 add 录一个新包）")

    # ── grant ────────────────────────────────────────────────────────────
    def _handle_grant(self, options):
        schema_name = (options["tenant"] or "").strip()
        times = options["times"]
        # 同上：`> 0` 判，⛔ 不用真值。`--times 0` / 负数若静默成功，运维会以为拨过了。
        if times is None or times <= 0:
            raise CommandError(
                f"--times 必须 ≥ 1，给的是 {times!r}：0 或负数会让「拨了」与「没拨」看起来一样，"
                "而且 `granted` 是 PositiveIntegerField，负数会以数据库错误收场而不是人话。"
            )
        tenant = self._get_tenant(schema_name)

        with transaction.atomic():
            # ⭐ 行锁 + `F()`：见模块 docstring。`select_for_update()` 必须在事务里
            # （autocommit 下 Django 会抛 `TransactionManagementError`，响亮失败）。
            grant = (
                RealNameGrant.objects.select_for_update().filter(tenant=tenant).first()
            )
            if grant is None:
                # 「没拨过」与「拨到 0」是两件事 ⇒ 新建一行时**就把本次的次数写进去**，
                # ⛔ 不先建一个 0 额度的行再累加（那样中途失败会留下一个 0 额度的行，
                # 而扣减侧对「行存在但 granted=0」报的是「余量不足」，看起来像拨过了）。
                RealNameGrant.objects.create(tenant=tenant, granted=times)
            else:
                # ⛔ 不是 `grant.granted += times; grant.save()`：读-改-写在并发下丢更新。
                RealNameGrant.objects.filter(pk=grant.pk).update(
                    granted=F("granted") + times
                )

        after = RealNameGrant.objects.get(tenant=tenant)
        self.stdout.write(
            self.style.SUCCESS(
                f"已给租户 {schema_name} 拨 {times} 次：累计已拨 {after.granted}，"
                f"已用 {after.used}，剩余 {after.granted - after.used}"
            )
        )

    # ── quota ────────────────────────────────────────────────────────────
    def _handle_quota(self, options):
        schema_name = (options["tenant"] or "").strip()
        tenant = self._get_tenant(schema_name)
        grant = RealNameGrant.objects.filter(tenant=tenant).first()
        if grant is None:
            # ⛔ 这里**不建行**：`quota` 是只读子命令，读一下就改库是最难查的那类副作用。
            self.stdout.write(
                f"租户 {schema_name} 还没有拨过额度（剩余 0）："
                "用 realname_package grant --tenant <schema> --times <n> 拨。"
            )
            return
        self.stdout.write(
            "租户 {schema} 剩余 {left} 次（已拨 {granted}，已用 {used}）".format(
                schema=schema_name,
                left=grant.granted - grant.used,
                granted=grant.granted,
                used=grant.used,
            )
        )

    # ── 共用 ─────────────────────────────────────────────────────────────
    def _get_tenant(self, schema_name: str) -> Tenant:
        """取租户（不存在 ⇒ `CommandError`）。⚠️ 不替运维建租户：`Tenant.save()` 会真建 schema 并跑租户迁移（`auto_create_schema=True`）——
        「拨额度」顺手建出一个租户是最难查的那类副作用（拨错对象、还多出一套表）；要建租户走 `provision_tenant_db`。"""
        if not schema_name:
            raise CommandError("--tenant 不能是空白")
        tenant = Tenant.objects.filter(schema_name=schema_name).first()
        if tenant is None:
            raise CommandError(
                f"租户 {schema_name!r} 不存在（本命令 ⛔ 不替运维建租户："
                "Tenant.save() 会真建 schema 并跑租户面迁移）。"
                "要开租户请走 provision_tenant_db；先确认 schema 名拼对了。"
            )
        return tenant
