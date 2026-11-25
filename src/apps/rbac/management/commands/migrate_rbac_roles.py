"""存量租户的角色迁移：五个旧 Group → 新五个角色（spec §6.3 / §6.4）。

**默认 `--dry-run`**（D13）：必须显式 `--apply` 才写库。

⚠️ **显式后果**（spec §6.4，不许含糊）：迁移后每个账号的权限集按**新角色的默认值重算**，
不是把旧 Group 的权限平移 ⇒ 存量账号的可见范围**会变**。所以本命令产出
①「改了哪些账号」摘要，②「逐账号 old→new 可见范围」的核对清单，交业务方复核。

⚠️ **演练为什么不写库**：`transaction.atomic()` 只在**异常冒出块**时回滚，块内正常
`return`/`continue` 会把前面的写入**提交**掉（AGENTS.md §5 / E-0022）。所以演练路径
一律先 `transaction.set_rollback(True)`、再**正常离开** `atomic()` 块——不写 `return`。
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import tenant_context

from apps.platform.models import Tenant
from apps.rbac.models import PermissionNode, Role
from apps.rbac.permissions import user_node_codes
from apps.rbac.seed import seed_builtin_roles, seed_catalog

RENAME = {"租户管理员": "系统管理员", "打手": "代练员"}
#: 成员并入「客服」的旧职位（财务能力靠 [10] 财务运营的权限点给，与状态机无关）
MERGE_INTO_SERVICE = ("财务",)
#: 成员并入「客服主管」的旧职位。
#: ⚠️ **口径**：原理由写的是「P3a 的 `roles_of` 把『审核人』映射成状态机的
#: `REVIEWER`，并进客服会让 `PENDING_REVIEW` 两条边再无持有人」——该理由**随单轴化作废**
#: （`roles_of` 已删除，见 spec `2026-09-23-rbac-single-axis-design.md`）。
#: **合并不动**：把「审核人」并进「客服主管」是既成事实，改它等于搬动存量租户的用户；
#: 现理由 = 职位语义（审核属主管职责），且与对标 5 职位词表一致。
MERGE_INTO_SUPERVISOR = ("审核人",)


class Command(BaseCommand):
    help = "把存量租户的旧职位 Group 迁移为对标的内置角色（默认 dry-run）"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="真的写库（默认只演练）")
        parser.add_argument("--dry-run", action="store_true", help="显式演练（默认行为）")
        parser.add_argument("--schema", default=None, help="只处理某个租户 schema")

    def handle(self, *args, **options):
        apply_ = options["apply"] and not options["dry_run"]
        tenants = Tenant.objects.exclude(schema_name="public")
        if options["schema"]:
            tenants = tenants.filter(schema_name=options["schema"])
            # ⚠️ **`--schema` 拼错时必须响亮失败**（终局评审 D4）：过滤后为空时
            # 原实现直接跳过循环、**exit 0**，而本命令是部署顺序里的一步——
            # 运维会以为"迁移成功完成"，实际一个租户都没处理。零租户不是成功态，是输入错。
            if not tenants.exists():
                raise CommandError(f"没有匹配的租户 schema：{options['schema']}")
        for tenant in tenants:
            with tenant_context(tenant):
                # ⚠️ 整段（含快照与迁移）放在**同一个** `atomic()`：演练时连目录补灌一起回滚，
                # 于是「默认 dry-run」是字面意义上的"一行都不写"。
                with transaction.atomic():
                    # ⚠️ 目录必须先于 `before` 快照灌好（控制者裁决 #2）：否则在没有目录的
                    # 租户上 `before` 全是空数组，核对清单会把"目录本来就没有"误报成
                    # "权限全被收走"。
                    seed_catalog()
                    groups_before = self._group_snapshot()
                    nodes_before = self._node_snapshot()
                    self._migrate()
                    groups_after = self._group_snapshot()
                    nodes_after = self._node_snapshot()
                    if not apply_:
                        transaction.set_rollback(True)   # 先标记回滚，再正常离开块（E-0022）
            self.stdout.write(f"=== {tenant.schema_name} ===")
            self._print_group_diff(groups_before, groups_after)
            self._print_node_diff(nodes_before, nodes_after)
            if not apply_:
                self.stdout.write("（演练模式：以上变更**未写库**）")

    def _node_snapshot(self):
        """逐账号的可见范围快照——核对清单 ② 的来源（D13）。

        用 `apps.rbac.permissions.user_node_codes`：它已做 public 平面 fail-closed，
        与接口层的判据同源（不另写一套取值逻辑）。
        """
        return {
            user.username: sorted(user_node_codes(user))
            for user in get_user_model().objects.all()
        }

    def _group_snapshot(self):
        """逐账号的职位 Group 快照——核对清单 ① 的来源。

        必须与 ② 分开取：只改了职位、但默认权限集恰好不变的账号，在 ② 里是不可见的，
        而它同样是"改了哪些账号"的一部分。
        """
        return {
            user.username: sorted(user.groups.values_list("name", flat=True))
            for user in get_user_model().objects.all()
        }

    def _migrate(self):
        # 新五个角色与同名 Group 由这里建出来（幂等）：改名/合并的目标必须先存在，
        # 否则 `_rename` 的 `get_or_create` 会建出一个没有 `Role` 行的裸 Group。
        seed_builtin_roles()
        for old, new in RENAME.items():
            self._rename(old, new)
        for old in MERGE_INTO_SERVICE:
            self._merge_into(old, "客服")
        for old in MERGE_INTO_SUPERVISOR:
            self._merge_into(old, "客服主管")

    def _rename(self, old, new):
        source = Group.objects.filter(name=old).first()
        if source is None:
            return
        target, _ = Group.objects.get_or_create(name=new)
        for user in get_user_model().objects.filter(groups=source):
            user.groups.add(target)
        source.delete()

    def _merge_into(self, old, target_name):
        source = Group.objects.filter(name=old).first()
        if source is None:
            return
        target, _ = Group.objects.get_or_create(name=target_name)
        for user in get_user_model().objects.filter(groups=source):
            user.groups.add(target)
        source.delete()

    def _print_group_diff(self, before, after):
        """核对清单 ①：改了哪些账号（职位 old→new）。"""
        changed = 0
        for username in sorted(set(before) | set(after)):
            old, new = before.get(username, []), after.get(username, [])
            if old == new:
                continue
            changed += 1
            self.stdout.write(f"{username}: 职位 {old} → {new}")
        self.stdout.write(f"— 职位有变化的账号：{changed}")

    def _print_node_diff(self, before, after):
        """核对清单 ②：逐账号 old→new 可见范围（节点 code）diff。"""
        for username in sorted(set(before) | set(after)):
            old, new = before.get(username, []), after.get(username, [])
            added, removed = sorted(set(new) - set(old)), sorted(set(old) - set(new))
            if not added and not removed:
                continue
            self.stdout.write(f"{username}: +{len(added)} / -{len(removed)}")
            for code in added:
                self.stdout.write(f"    + {code}")
            for code in removed:
                self.stdout.write(f"    - {code}")
