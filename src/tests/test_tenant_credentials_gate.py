"""凭据落点的**名字闸门**（M-11，T6 修复轮）：`write_tenant_credentials` 自己再挡一次非法名。

⚠️ **为什么单独一个文件、不挂在 `tests/test_platform_tenant_provision.py` 里**：那个模块有
`setUpModule()`（要连测试库清夹具行），而 `SimpleTestCase` 只跑自己时 Django **不建**测试库
⇒ 单跑那一条会以 `OperationalError: database "…" does not exist` 的 **setUpModule 错误**收场
（`AGENTS.md` §5-G：收集 / 导入 / 迁移错误一律记 BROKEN，不产出任何结论）。本文件**不碰数据库**，
放这里可以单独跑、也可以进任何网。
"""
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from apps.platform.identifiers import InvalidIdentifierError
from apps.platform.tenant_provisioning import write_tenant_credentials


class WriteTenantCredentialsGateTests(SimpleTestCase):
    """M-11（T6 修复轮）：`write_tenant_credentials` **自己**再过一次名字白名单。

    ⚠️ 为什么它必须自己挡：那个参数**直接决定文件落点**（`<仓库根>/secrets/tenant-<schema>.env`）
    ⇒ 它是路径注入面；「调用方一定已经过了闸门」只是**约定**，而本函数是公开的可复用层
    （控制台与将来的调用方都走它）。判别力：`../` 形状的名字在没有这道闸门时会写到 `secrets/`
    之外——本用例只断言「抛 `InvalidIdentifierError`」与「一个字节都没写出来」，
    ⛔ 不去真的制造目录穿越（那会把测试机上的文件写坏）。
    """

    def test_a_non_whitelisted_schema_name_is_refused_before_writing(self):
        with tempfile.TemporaryDirectory(prefix="pct6-m11-") as tmpdir:
            with override_settings(REPO_ROOT=tmpdir):
                with self.assertRaises(InvalidIdentifierError):
                    write_tenant_credentials(
                        schema_name="../pct6_escape",
                        database="tt_probe",
                        role="tt_probe",
                        password="pw-pct6-m11-literal",
                        host="127.0.0.1",
                        port="5432",
                    )

            # 终态：连 `secrets/` 目录都不该被建出来，更不该有任何 `.env` 落地。
            self.assertFalse(Path(tmpdir, "secrets").exists(), "名字不合法却建了凭据目录")
            self.assertEqual(list(Path(tmpdir).rglob("*.env")), [])
