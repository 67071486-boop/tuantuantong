"""`egress_probe`：用**指定出口**探一次鸡蛋铺（登录 + 读一页大厅），给换出口的脚本当收尾判据。 python manage.py egress_probe --proxy 'http://u:p@1.2.3.4:8080' --tenant <schema> python manage.py egress_probe --proxy direct            # 直连（与出口池同一个词） printf '%s' "$URL" | python manage.py egress_probe --proxy - --tenant <schema>
# `-` = 从 **stdin** 读出口：`deploy/egress.sh` 经 ssh 跑自检时用它， # 代理账号 / 密码**不进远端命令行**（否则远端 `ps` 能看到）。 - 打印四行：`登录=OK/失败`、`大厅=OK/失败`、`耗时=…ms`、`出口=<host>`； **两条都通 ⇒ 退出码 0，任一失败 ⇒ 1**（抛 `CommandError`）。 - ⛔ 输出里**只有出口 host**：代理的账号 / 密码一个字都不进 stdout / stderr / 异常文案
（全局约束；异常只打类型名，⛔ 不打 `str(exc)`）。 - ⛔ **只读**：登录一次 + 读一页大厅，一个字节都不落库。 - 凭据只从 `--tenant` 那份租户库里的**启用中登录式上家绑定**取（`jidanpu.credentials_of` 是唯一取值口径，⛔ 本命令不另写一份）；省略 `--tenant` 时按 schema 序取第一份有可用绑定的租户（脚本总会显式给 schema）。 - 大厅的成功码**自己判** `code == 0`：复用 `jidanpu_upstream._require_read_success`。理由：SDK 的 `lenient` 把 `code > 0` 一律判成功 ⇒ 只看「没抛异常」会把失败信封**误绿** （E-0087 的读侧同族）。"""
from __future__ import annotations

import sys
import time
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.business import jidanpu, jidanpu_proxy, jidanpu_upstream
from apps.business.models import JidanpuUpstreamBinding
from apps.platform.models import Tenant

#: 大厅探针的逐字参数（brief：`HALL_PATH` + `gameName=原神&page=1&limit=10`）。
HALL_QUERY = {"gameName": "原神", "page": 1, "limit": 10}


def _proxy_host(proxy: str | None) -> str:
    """出口标签：只回 **host**（⛔ 不含账号 / 密码 / 端口 / 查询串 —— 硬约束）。"""
    if proxy is None:
        return "direct"
    return urlsplit(proxy).hostname or "?"


class Command(BaseCommand):
    help = (
        "用指定出口探一次鸡蛋铺（登录 + 读一页大厅）；两条都通退出 0，任一失败 1"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--proxy", required=True,
            help="完整代理 URL（如 http://u:p@1.2.3.4:8080）；写 direct = 直连；"
                 "写 - = 从 stdin 读（⛔ 密码不进命令行 / 远端 ps）。"
                 "⛔ 输出里只打 host，账号密码打码",
        )
        parser.add_argument(
            "--tenant", default=None,
            help="租户 schema 名（从该租户的启用中登录式上家绑定取凭据）；"
                 "省略时按 schema 序取第一份可用的",
        )

    def handle(self, *args, **options):
        token = (options["proxy"] or "").strip()
        if token == "-":
            # ⭐ 从 stdin 读（`deploy/egress.sh` 经 ssh 调它）：出口 URL 不进命令行，
            #    远端 `ps` 只看得到 `--proxy -`。
            token = (sys.stdin.readline() or "").strip()
        if not token:
            raise CommandError("--proxy 不能为空（要直连请写 direct；从 stdin 读用 -）")
        # 与出口池同一个词表：`direct` / `local` / `直连` / 空 = 直连。
        proxy = None if token.lower() in jidanpu_proxy.DIRECT_TOKENS else token
        schema = (options["tenant"] or "").strip() or None

        if schema is not None:
            tenants = Tenant.objects.exclude(
                schema_name=get_public_schema_name()
            ).order_by("schema_name")
            tenant = tenants.filter(schema_name=schema).first()
            if tenant is None:
                raise CommandError(f"没有这个租户 schema：{schema}")
            with tenant_context(tenant):
                binding = self._usable_binding()
                if binding is None:
                    raise CommandError(
                        f"租户 {schema} 里没有可用的登录式上家绑定"
                        "（需要「启用 + 地址 / 账号 / 密码齐全」）"
                    )
                self._probe(binding=binding, proxy=proxy)
            return

        # 省略 `--tenant`：按 schema 序找第一个有可用绑定的租户（只读，⛔ 不写任何库）。
        tenants = Tenant.objects.exclude(
            schema_name=get_public_schema_name()
        ).order_by("schema_name")
        for tenant in tenants:
            with tenant_context(tenant):
                binding = self._usable_binding()
                if binding is None:
                    continue
                self._probe(binding=binding, proxy=proxy)
                return
        raise CommandError(
            "没有任何租户有可用的登录式上家绑定；请用 --tenant 指一个配好凭据的租户"
        )

    # ── 取凭据（唯一口径：`jidanpu.credentials_of`）──────────────────────────

    @staticmethod
    def _usable_binding():
        """本租户里第一份**启用中、登录式、凭据齐全**的上家绑定；没有 ⇒ `None`。
        ⚠️ ②式（官方 API）绑定在界面上账号 / 密码就是空的 ⇒ 按 `mode` 先筛掉，
        ⛔ 不拿 `credentials_of` 的真值去猜「它是不是登录式」（两套凭据允许同时留着）。"""
        for binding in jidanpu_upstream.enabled_bindings():
            if binding.mode != JidanpuUpstreamBinding.Mode.PASSWORD:
                continue
            if jidanpu.credentials_of(binding) is None:
                continue
            return binding
        return None

    # ── 探一次 ──────────────────────────────────────────────────────────────

    def _probe(self, *, binding, proxy) -> None:
        started = time.monotonic()
        login_ok = False
        hall_ok = False
        try:
            # 显式建连（`proxy=` 直接交给 httpx；凭据从那一行绑定取）。
            with jidanpu.open_client(binding, proxy=proxy) as client:
                login_ok = self._login(client)
                if login_ok:
                    hall_ok = self._hall(client)
                else:
                    self.stdout.write("大厅=失败（登录未成功，未发起请求）")
        except Exception as exc:  # noqa: BLE001 —— 建连失败也算「这次出口不通」
            # ⛔ 只打类型名：`str(exc)` 可能带上游回包 / 凭据片段。
            if not login_ok:
                self.stdout.write(f"登录=失败（{type(exc).__name__}）")
                self.stdout.write("大厅=失败（登录未成功，未发起请求）")
            else:
                self.stdout.write(f"大厅=失败（{type(exc).__name__}）")
        elapsed_ms = int((time.monotonic() - started) * 1000)
        self.stdout.write(f"耗时={elapsed_ms}ms")
        self.stdout.write(f"出口={_proxy_host(proxy)}")
        if not (login_ok and hall_ok):
            raise CommandError("出口自检未通过（登录或大厅失败）")

    def _login(self, client) -> bool:
        try:
            client.ensure_login()
        except Exception as exc:  # noqa: BLE001 —— 登录失败是探针的结论，不是崩溃
            self.stdout.write(f"登录=失败（{type(exc).__name__}）")
            return False
        self.stdout.write("登录=OK")
        return True

    def _hall(self, client) -> bool:
        try:
            client.call("GET", jidanpu_upstream.HALL_PATH, query=dict(HALL_QUERY))
            # 读端点的成功码**自己判**（SDK 的 lenient 会把失败信封判成成功）。
            jidanpu_upstream._require_read_success(client, jidanpu_upstream.HALL_PATH)
        except Exception as exc:  # noqa: BLE001 —— 读不到就是「大厅=失败」
            self.stdout.write(f"大厅=失败（{type(exc).__name__}）")
            return False
        self.stdout.write("大厅=OK")
        return True
