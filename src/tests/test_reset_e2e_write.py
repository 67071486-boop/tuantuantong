"""`reset_e2e_write` 的闸门与常量判据（W1 + DEBUG 闸门 + 常量同源）。

**为什么这些必须是纯函数级用例**：本命令是**唯一**会 DROP 租户库的地方，而它一旦被指错库，
后果是不可逆的（`tt_e2e` 的基线数据当场没了）。所以判据要在**不连库**的前提下把闸门钉死：
`_assert_write_db` 是纯函数、`handle()` 的 DEBUG 闸门与库名闸门都排在**任何 DB 动作之前**
（用 `mock.patch` 顶掉 `drop_tenant_database` / `call_command` / `_run_seed_e2e`，断言它们**没被调用**）。

⚠️ 断言一律用 `assertRaisesMessage` 钉**关键片段**，⛔ 不用裸 `assertRaises`：
后者连"抛的是不是这个闸门"都分不出来（`E-0024` 同族）。
"""
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import SimpleTestCase, override_settings

from apps.platform.management.commands import reset_e2e_write
from apps.platform.management.commands.reset_e2e_write import (
    WRITE_DATABASE,
    WRITE_DOMAIN,
    WRITE_ENV_FILE,
    WRITE_SCHEMA,
    Command,
    _assert_write_db,
    _read_credentials,
)

#: 闸门文案里必须出现的关键片段（判据的判别内容；只写命令名会被消息里的命令名满足）。
GATE_MESSAGE = "只认一次性写租户库"
DEBUG_MESSAGE = "只在 DEBUG=true 下运行"


class ResetE2EWriteConstantsTests(SimpleTestCase):
    """常量与 spec §2 的表**逐字**对齐（⛔ 用字面量断言，不写成"读常量再比常量"的自指，E-0024）。

    这些常量的漂移后果不一样、都要能红：
    - `WRITE_DATABASE` 变了 ⇒ 闸门守的是另一个库（可能守成只读租户库）；
    - `WRITE_SCHEMA` 变了 ⇒ 派生库名与 `WRITE_DATABASE` 不一致，供给与 DROP 打的不是同一个库；
    - `WRITE_ENV_FILE` 变了 ⇒ 写模式的运行器去读另一个（不存在的）凭据文件。
    """

    def test_write_database_is_the_literal_tt_e2e_write(self):
        self.assertEqual(WRITE_DATABASE, "tt_e2e_write")

    def test_schema_and_domain_are_the_literals(self):
        self.assertEqual(WRITE_SCHEMA, "e2e_write")
        self.assertEqual(WRITE_DOMAIN, "e2e-write.test.local")

    def test_credentials_land_in_the_dedicated_0600_path(self):
        """凭据落点必须是 `.tools/e2e/tenant-write.env`（与只读租户的 `tenant.env` **分开**）。"""
        self.assertEqual(
            Path(WRITE_ENV_FILE).parts[-3:], (".tools", "e2e", "tenant-write.env")
        )
        # 落点必须在**本仓库根**下（worktree 的根是它自己 ⇒ 不能写死主检出的绝对路径）。
        self.assertEqual(Path(WRITE_ENV_FILE).parent.parent.parent, Path(settings.REPO_ROOT))

    def test_command_exposes_no_target_argument(self):
        """⛔ 命令**不接受任何"指向别的库"的参数**——闸门不能被参数松动。

        这条是结构性判据：把目标参数化（`--target-db` / `--schema`）必然让本用例红。
        Django 的全局选项（`--version` / `-v` / `--settings` …）由 `BaseCommand` 统一加，
        与"本命令自己声明了什么"无关 ⇒ 拿一个**没有 `add_arguments`** 的基类解析器做基线，
        只比"本命令额外多出来的选项"，基线必须为空。
        """
        base_options = {
            option
            for action in BaseCommand().create_parser("manage.py", "x")._actions
            for option in action.option_strings
        }
        own_options = [
            option
            for action in Command().create_parser("manage.py", "reset_e2e_write")._actions
            for option in action.option_strings
            if option not in base_options
        ]
        self.assertEqual(own_options, [])


class AssertWriteDbGateTests(SimpleTestCase):
    """W1：闸门对**非** `tt_e2e_write` 的库名一律拒绝，对 `tt_e2e_write` 放行。

    spec §6 W1 的原话含「对 `tt_e2e` 的一次真实调用」——在本命令里"真实调用"只能是这个
    纯函数（命令没有目标参数，⛔ 不允许为测试方便加一个，那正是把闸门变松）。
    """

    def test_accepts_the_write_database(self):
        _assert_write_db("tt_e2e_write")  # 不抛即通过

    def test_rejects_every_other_database(self):
        """反面：只读租户库、开发库、相近名字一律拒绝。

        ⛔ 相近名字（`tt_e2e_write_extra` / 大小写变体）刻意在内：判据必须是**恰好相等**，
        不是前缀、不是大小写不敏感。
        """
        for name in (
            "tt_e2e",
            "tt_dev",
            "tt_e2e_write_extra",
            "TT_E2E_WRITE",
            "",
            "tt_e2e_write ",
        ):
            with self.subTest(name=name):
                with self.assertRaisesMessage(CommandError, GATE_MESSAGE):
                    _assert_write_db(name)

    def test_message_names_both_the_expected_and_the_received_value(self):
        """排障信息要够：文案同时给出**该是哪个库**与**收到的是哪个**（否则只报"拒绝"）。

        ⚠️ 这条是**独立**于上面那两条的判据：把文案改成不点名收到的值（例如只留常量），
        上面两条照样绿，这条红。
        """
        with self.assertRaises(CommandError) as caught:
            _assert_write_db("tt_e2e")
        message = str(caught.exception)
        self.assertIn("tt_e2e_write", message)
        self.assertIn("'tt_e2e'", message)


class ResetE2EWriteGateWiringTests(SimpleTestCase):
    """闸门真的**接在** `handle()` 上，且排在**任何 DB 动作之前**。

    **为什么必须有**（同 `seed_e2e` 守卫那次的教训）：`_assert_write_db` 自己的用例全绿，
    只能证明"函数写得对"，证明不了"命令调了它"——把 `handle()` 里那两行删掉，上面全绿。
    这里用 `mock.patch` 顶掉三个真实出口（DROP 原语 / 供给子命令 / 灌夹具子进程），断言它们
    **没被调用**。⚠️ patch 打在**本命令模块**上（命令是 `from ... import` 进来的，
    `E-0029`：patch 要打在"被查找的那个模块"上）。
    """

    def _patch_command_io(self):
        """顶掉 `handle()` 的三个真实出口（DROP 原语 / 供给子命令 / 灌夹具子进程）。

        ⛔ 本类**不连库、不起进程**：即使闸门被改坏（变异），也既不会 DROP、也不会供给 / 灌数据。
        """
        drop_patcher = mock.patch.object(reset_e2e_write, "drop_tenant_database")
        drop = drop_patcher.start()
        self.addCleanup(drop_patcher.stop)
        call_patcher = mock.patch.object(reset_e2e_write, "call_command")
        call = call_patcher.start()
        self.addCleanup(call_patcher.stop)
        seed_patcher = mock.patch.object(reset_e2e_write.Command, "_run_seed_e2e")
        seed = seed_patcher.start()
        self.addCleanup(seed_patcher.stop)
        return drop, call, seed

    def _drift(self, attr, value):
        """让一个模块常量漂移，再顶掉三个出口。"""
        drift_patcher = mock.patch.object(reset_e2e_write, attr, value)
        drift_patcher.start()
        self.addCleanup(drift_patcher.stop)
        return self._patch_command_io()

    def test_refuses_when_debug_is_off(self):
        """DEBUG 闸门：关掉就拒，且**一个 DB 动作都不做**。"""
        drop, call, seed = self._patch_command_io()
        with override_settings(DEBUG=False):
            with self.assertRaisesMessage(CommandError, DEBUG_MESSAGE):
                Command().handle()
        drop.assert_not_called()
        call.assert_not_called()
        seed.assert_not_called()

    def test_refuses_before_any_drop_when_write_database_drifts(self):
        """常量漂到只读租户库 ⇒ 闸门必须**在 DROP 之前**拒绝（残余保险：DROP 一次不可逆）。"""
        drop, call, seed = self._drift("WRITE_DATABASE", "tt_e2e")
        with override_settings(DEBUG=True):
            with self.assertRaisesMessage(CommandError, GATE_MESSAGE):
                Command().handle()
        drop.assert_not_called()
        call.assert_not_called()
        seed.assert_not_called()

    def test_refuses_before_any_drop_when_write_schema_drifts(self):
        """schema 常量漂移 ⇒ 派生库名不再是 `tt_e2e_write` ⇒ 也在 DROP 之前拒绝。"""
        drop, call, seed = self._drift("WRITE_SCHEMA", "e2e")
        with override_settings(DEBUG=True):
            with self.assertRaisesMessage(CommandError, GATE_MESSAGE):
                Command().handle()
        drop.assert_not_called()
        call.assert_not_called()
        seed.assert_not_called()


class WriteCredentialsReaderTests(SimpleTestCase):
    """`_read_credentials`：缺文件 / 缺键必须**响亮**失败，不许静默回落到应用库凭据。

    静默回落是本命令最贵的失效形态：子进程会带着 `.env` 的应用库身份去灌夹具
    ⇒ 灌错库（`apps/platform/db_provisioning.py` 模块 docstring 同一条教训）。
    """

    def _write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_parses_the_credential_keys(self):
        path = self._write(
            "# 注释\n\nPOSTGRES_DB=tt_x\nPOSTGRES_USER=tt_x\nPOSTGRES_PASSWORD=p\n"
            "POSTGRES_HOST=127.0.0.1\nPOSTGRES_PORT=5432\n"
        )
        self.assertEqual(_read_credentials(path)["POSTGRES_DB"], "tt_x")

    def test_refuses_missing_file(self):
        with self.assertRaisesMessage(CommandError, "凭据文件不存在"):
            _read_credentials(Path("/nonexistent/tenant-write.env"))

    def test_refuses_missing_key(self):
        path = self._write("POSTGRES_DB=tt_x\nPOSTGRES_USER=tt_x\n")
        with self.assertRaisesMessage(CommandError, "缺少键"):
            _read_credentials(path)
