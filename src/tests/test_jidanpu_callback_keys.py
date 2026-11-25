"""回调地址拼装 +「码 → 密钥」文件的**写入侧**（本线 T3）的判据。

## 这份测试守什么（逐条对着 spec §13.3 的 C6/C10/C11、§13.1 的 A9 与任务书）

1. **C6（码的形状）**：落进文件的那一串恰好是 **22 个 `[A-Za-z0-9_-]`** —— 且它必须**过接收器
   那一关**（用读侧的路径解析当裁判，⛔ 不是我们自己再写一条正则）；
2. **C10（地址由后端算、算不出来回空串 + 原因）**：域名取**本租户的主域名**、形状逐字
   `https://<主域名>/api/jidanpu/callback/<码>`；①式 / 没有码 / 码形状不合 / 查不到主域名
   四种情形一律**空串 + 人话原因**（⛔ 不回落 `example.com`、⛔ 不拼半截地址）；
3. **C11（地址里不含 client_id / client_secret / 租户名 / schema 名）**：拿哨兵值直接搜地址
   （⚠️ 域名刻意**不含** `test` 之外的租户串，免得 `assertNotIn` 被域名自身的字符凑成假绿/假红）；
4. **A9（0600 文件的写入是原子的）**：打桩制造「写到一半抛异常」⇒ 原文件**内容一字不变**、
   ⛔ 不留临时文件（**不真去写坏一个文件**）；
5. **只增不减**：已有别的码的行 ⇒ 写新码之后那些行**逐字还在**（含 `#` 注释、含行尾 `\r` 的行）；
6. **同码更新**：同码写两次 ⇒ 文件里**只有一行**那个码、密钥是新值；
7. **权限 0600**：文件恰好 0600 —— 哪怕进程 `umask` 是 `0o777`（那正是「显式 `os.fchmod`」
   存在的理由）；父目录**不被我们动**（`shared/` 是发版建的，spec §5.3）；
8. ⛔ **密钥不泄漏**：哨兵密钥不许出现在日志 / 异常 / 异常回溯 / 返回值里 —— 而文件里**必须**有它
   （**正向哨兵**：少了这一句，上面那些否定断言在「什么都没发生」时也成立）；
9. **接线**（spec §5.3 的「谁写」）：保存绑定那一次 PUT 真的会写；①式 / 密钥为空**不写**；
   `enabled=False` **照写**；**写失败 ⇒ 整个保存回滚**（⛔ 不留「库里说配好了、文件里没有」的半态）。
   ⚠️ **2026-10-09 改准**：这几条今天住在**下家**那条 PUT 上（业务方裁定上家线不做②式官方API
   ⇒ 上家 PUT 里那一步已撤）⇒ 判据在 `tests/test_jidanpu_member_api.py` 的
   `JidanpuMemberOpenApiBindingTests`（`test_saving_an_openapi_binding_writes_the_0600_key_file` /
   `test_enabling_an_openapi_binding_also_writes_the_key_file` /
   `test_a_key_file_write_failure_rolls_the_whole_save_back` /
   `test_a_key_file_write_failure_leaves_an_existing_row_untouched`）；
   本文件只留**这个端点**自己的那三条（①式不写 / 空密钥不写 / 不回显密钥）。

## 三条纪律

- ⛔ **零真实网络**：本文件一个外部请求都不发（PUT 走 Django 测试 client；检测那一步打桩）；
- ⛔ **夹具一律合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `kefu-t3-*`、
  密钥用 `sentinel-…`（只允许出现在那个临时文件里）；
- ⭐ **契约由读侧裁判**：我们写出来的字节要用 **T4 的** `parse_callback_keys` 读回来对拍 ——
  「写入侧与读侧是不是同一份契约」不能由写入侧自己说了算。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_openapi
from apps.business.models import JidanpuUpstreamBinding
from apps.callback import config as callback_config
from apps.callback import wsgi as callback_wsgi
from apps.business.jidanpu_openapi import (
    CallbackKeysError,
    CALLBACK_URL_REASON_BAD_CODE,
    CALLBACK_URL_REASON_NO_CODE,
    CALLBACK_URL_REASON_NO_DOMAIN,
    CALLBACK_URL_REASON_NOT_OPENAPI,
)
from apps.platform.models import Domain
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具（⛔ 一个真值都没有）────────────────────────────────────────────────
#: 22 个字符、字母表 `A-Za-z0-9-_`（spec §5.1：`secrets.token_urlsafe(16)` 的形状）。
CODE_A = "AbCdEfGhIjKlMnOpQrStUv"
CODE_B = "ZzYyXxWwVvUuTtSsRrQqPp"
#: 21 位（**少一个字符**）：接收器的路径解析必须不认它 ⇒ 地址不给。
CODE_SHORT = "AbCdEfGhIjKlMnOpQrStU"

#: ⭐ 哨兵密钥：只允许出现在那份 0600 文件里（日志 / 异常 / 返回值里都不许有它）。
SENTINEL_SECRET = "sentinel-secret-must-not-leak-t3-0001"
#: 另一条绑定的密钥（用来验「别的行逐字未变」）。
OTHER_SECRET = "sentinel-secret-of-another-binding-0002"
#: 同一个码换一把密钥（用来验「同码更新、不新增重复行」）。
SECRET_B = "sentinel-secret-rotated-0003"
#: 不该出现在地址里的 client_id（C11 的哨兵）。
CLIENT_ID = "cid-synthetic-t3-0001"

#: 地址里**永远**不许出现的东西：那是**平台侧**那条老路（`callback.suqing.chat`），
#: 本线是「每租户自己域名下的地址」（spec §5.1）。
PLATFORM_CALLBACK_HOST = "callback.suqing.chat"

#: API 路径（⛔ 逐字，不从被测模块派生）。
BINDING_URL = "/api/business/member/upstream-jidanpu-binding/"

BASE_URL = "https://jidanpu-t3.test.invalid"
ACCOUNT = "kefu-t3-keys"
PASSWORD = "pw-canary-t3-keys"


def _binding(*, mode="openapi", code=CODE_A, secret=SENTINEL_SECRET, enabled=False):
    """一个**不用落库**的假绑定（写文件那几条用例只读这几个字段）。"""
    return SimpleNamespace(
        mode=mode, callback_code=code, client_secret=secret, enabled=enabled,
    )


def _temp_leftovers(directory: Path) -> list[str]:
    """目录里剩下的临时文件（⛔ 写失败之后不该有）。"""
    return sorted(
        p.name for p in directory.iterdir() if p.name.startswith(jidanpu_openapi._TMP_PREFIX)
    )


class _CaptureHandler(logging.Handler):
    """把本模块 logger 上真正发出来的记录收下来（⛔ 不用 `assertLogs`：它会把级别抬到 INFO，
    而且**一条都没发**时它直接判失败 —— 这两条都会让「日志里有没有泄漏」变得不可信）。"""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):  # pragma: no cover - 正常路径也走（本文件里它只做收集）
        try:
            self.messages.append(record.getMessage())
        except Exception:
            self.messages.append(str(record.msg))


class _LogCapture:
    """上下文管理器：挂上/摘掉捕获器（⛔ 不动 logger 的级别，免得串味到别的用例）。"""

    def __enter__(self):
        self.handler = _CaptureHandler()
        self.logger = logging.getLogger("apps.business.jidanpu_openapi")
        self.logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc_info):
        self.logger.removeHandler(self.handler)
        return False


# ══ 写文件：建 / 只增不减 / 同码更新 / 原子 / 权限 / 不泄漏 ═══════════════════════

class KeysFileWriteTests(TenantTestCase):
    """`ensure_callback_key` 的判据（不落库、不联网；用 `CALLBACK_KEYS_FILE` 指到临时目录）。

    ⚠️ 为什么用 `TenantTestCase` 而不是 `SimpleTestCase`：`get_tenant_domain_model()` /
    `connection` 那几个 Django 对象在**没有** Django 上下文时取不到，而本文件里
    「不落库」这件事靠的是**不碰 ORM**，不是靠测试基类。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tt-t3-keys-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        #: `shared/` 是**发版建的**（spec §5.3）⇒ 这里自己建好它，被测代码⛔ 不许新建目录。
        self.shared = self.root / "shared"
        self.shared.mkdir(mode=0o700)
        self.path = self.shared / "callback-keys.env"
        patcher = mock.patch.dict(os.environ, {"CALLBACK_KEYS_FILE": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    # ── 建文件 + 首行注释 ────────────────────────────────────────────────────

    def test_the_first_write_creates_the_file_with_the_header_line(self):
        """⭐ 文件不存在 ⇒ 第一次写把它建出来，**首行注释在**，第二行就是那一对（spec §5.3）。

        ⚠️ 为什么这条是**承重**的：文件不存在时接收器对新路径回 **503**（T4 的实测口径）
        ⇒ 漏建它 = 新路径的回调**全部 503**，而页面与库里一切正常（静默）。
        """
        self.assertFalse(self.path.exists())
        result = jidanpu_openapi.ensure_callback_key(_binding())
        self.assertIsNotNone(result, "②式 + 有码 + 有密钥 ⇒ 这一行必须写进去")
        self.assertEqual(result.path, self.path, "写的必须是 CALLBACK_KEYS_FILE 指的那一份")
        self.assertTrue(result.created, "这一次是「建」——文件此前不存在")
        self.assertTrue(result.changed, "新增了一行 ⇒ 内容确实变了")
        lines = self.path.read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER, "首行注释不在")
        self.assertEqual(lines[1], f"{CODE_A}\t{SENTINEL_SECRET}", "第二行必须是 `码<TAB>密钥`")
        # ⭐ 契约对拍：**读侧**（T4）解析回来必须正好是这一对。
        self.assertEqual(
            callback_config.parse_callback_keys(self.path.read_text(encoding="utf-8")),
            {CODE_A: SENTINEL_SECRET},
        )

    def test_the_header_line_is_verbatim_the_one_the_receiver_contract_declares(self):
        """首行注释与**读侧**（`apps/callback/config.py` 的 docstring，T4 冻结的那一份）逐字相同。

        ⚠️ 两处文本是同一句的两份拷贝（读侧只有 docstring、没有常量），⛔ 任一侧被改都要红
        —— 这条判据就是「逐字对齐」这件事的可执行形态。⛔ 不是自指：两边是**两个文件**。
        """
        source = Path(callback_config.__file__).read_text(encoding="utf-8")
        self.assertIn(jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER, source)

    # ── 只增不减 ─────────────────────────────────────────────────────────────

    def test_writing_a_new_code_leaves_every_other_line_byte_identical(self):
        """⭐ 已有别的码的行 / 注释 / 缺 TAB 的畸形行 ⇒ 写新码之后**逐字还在**（只增不减）。

        弄红它的办法：写成「重建整个文件」（只保留自己那一行）。⛔ 删行会让「别的绑定还在用」
        的情况断掉（spec §5.3 的撤销段）。
        """
        other_line = f"{CODE_B}\t{OTHER_SECRET}"
        original = (
            f"{jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER}\n"
            "# 人工加的一句注释\n"
            f"{other_line}\n"
            "\n"
            "这一行没有 TAB·读侧会忽略它\n"
            f"# {CODE_SHORT}\t这行是注释，不算条目\n"
        )
        self.path.write_text(original, encoding="utf-8")
        os.chmod(self.path, 0o600)

        jidanpu_openapi.ensure_callback_key(_binding())

        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(original), f"旧内容不再逐字是前缀了：\n{text!r}")
        self.assertIn(f"{CODE_A}\t{SENTINEL_SECRET}\n", text)
        # 读侧解析出来是**两条**：旧的那一条（逐字）＋新的那一条。
        self.assertEqual(
            callback_config.parse_callback_keys(text),
            {CODE_B: OTHER_SECRET, CODE_A: SENTINEL_SECRET},
        )
        self.assertEqual(text.count(f"\t{OTHER_SECRET}"), 1, "旧那一行的密钥被改动过")

    def test_a_last_line_without_a_newline_is_terminated_before_appending(self):
        """⚠️ 末行没有换行符 ⇒ 追加前必须补一个 `\\n`：否则新行会粘在旧行尾巴上、拼坏它那条密钥。"""
        original = f"{CODE_B}\t{OTHER_SECRET}"  # ⛔ 故意不带 \n
        self.path.write_text(original, encoding="utf-8")
        os.chmod(self.path, 0o600)

        jidanpu_openapi.ensure_callback_key(_binding())

        text = self.path.read_text(encoding="utf-8")
        self.assertIn(f"{CODE_B}\t{OTHER_SECRET}\n", text)
        self.assertEqual(
            callback_config.parse_callback_keys(text),
            {CODE_B: OTHER_SECRET, CODE_A: SENTINEL_SECRET},
        )

    # ── 同码更新（不新增重复行）──────────────────────────────────────────────

    def test_the_same_code_written_twice_stays_one_line_with_the_new_secret(self):
        """⭐ 同码写两次 ⇒ 文件里**只有一行**那个码、密钥是新值（⛔ 不是多出一行）。"""
        jidanpu_openapi.ensure_callback_key(_binding(secret=SENTINEL_SECRET))
        again = jidanpu_openapi.ensure_callback_key(_binding(secret=SECRET_B))
        self.assertTrue(again.changed, "换了密钥 ⇒ 内容确实变了")
        self.assertFalse(again.created, "文件早就在了")

        text = self.path.read_text(encoding="utf-8")
        entries = [line for line in text.split("\n")
                   if line and not line.lstrip().startswith("#")]
        self.assertEqual(entries, [f"{CODE_A}\t{SECRET_B}"], f"应当恰好一行：{entries!r}")
        self.assertNotIn(SENTINEL_SECRET, text, "旧密钥必须被换掉")
        self.assertEqual(callback_config.parse_callback_keys(text), {CODE_A: SECRET_B})

    def test_writing_the_identical_line_twice_is_an_idempotent_rewrite(self):
        """同样的码 + 同样的密钥 ⇒ `changed=False`（内容一字未变），文件仍然完整可读。"""
        jidanpu_openapi.ensure_callback_key(_binding())
        before = self.path.read_text(encoding="utf-8")
        again = jidanpu_openapi.ensure_callback_key(_binding())
        self.assertFalse(again.changed, "内容没变 ⇒ 不该报「变了」")
        self.assertFalse(again.created)
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    # ── 原子性（A9）─────────────────────────────────────────────────────────

    def test_a_write_that_fails_midway_leaves_the_old_file_byte_identical(self):
        """⭐⭐ A9：写盘**写到一半**抛 ⇒ 原文件**内容一字不变**、⛔ 不留临时文件、异常响亮。

        ⚠️ 打桩打在**写盘字节的唯一出口**（`jidanpu_openapi._write_all`）上，并且**先真的写了
        5 个字节**再抛 —— 模拟"磁盘满了 / 进程被杀"这一类真实形态。
        ⚠️ **桩接没接上本身有判据**（`calls` 非空，E-0029）：没接上时下面那两条否定断言会因为
        「压根没跑写」而**假绿**。
        弄红它的办法：把实现换成 `open(path, "w")` 截断重写 —— 那一刻原文件已经被清空 ⇒ 红。
        """
        original = f"{jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER}\n{CODE_B}\t{OTHER_SECRET}\n"
        self.path.write_text(original, encoding="utf-8")
        os.chmod(self.path, 0o600)
        calls: list[int] = []

        def half_then_fail(fd, data):
            os.write(fd, data[:5])
            calls.append(len(data))
            raise OSError("磁盘满了（打桩，不是真写坏）")

        with mock.patch.object(jidanpu_openapi, "_write_all", half_then_fail):
            with self.assertRaises(CallbackKeysError) as ctx:
                jidanpu_openapi.ensure_callback_key(_binding())

        self.assertTrue(calls, "桩没接上 ⇒ 下面两条断言没有判别力")
        self.assertEqual(self.path.read_text(encoding="utf-8"), original, "原文件被改动了")
        self.assertEqual(_temp_leftovers(self.shared), [], "临时文件没清掉")
        self.assertNotIn(SENTINEL_SECRET, str(ctx.exception))

    def test_the_target_file_is_never_truncated_while_writing(self):
        """⭐ 写盘过程中，**目标文件**的字节数从来不会变小（截断重写的直接后果是"瞬间空文件"）。

        ⚠️ 判据打在 `os.replace` 上：替换之前目标文件必须**一直在**、且**内容完整**。
        弄红它的办法：`open(path, "w")` 截断重写 —— 那么"替换"那一刻文件已经被清空了 ⇒ 红。
        """
        original = f"{jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER}\n{CODE_B}\t{OTHER_SECRET}\n"
        self.path.write_text(original, encoding="utf-8")
        os.chmod(self.path, 0o600)
        seen: list[str] = []
        real_replace = os.replace

        def spy_replace(src, dst, *args, **kwargs):
            seen.append(self.path.read_text(encoding="utf-8"))
            return real_replace(src, dst, *args, **kwargs)

        with mock.patch.object(jidanpu_openapi.os, "replace", spy_replace):
            jidanpu_openapi.ensure_callback_key(_binding())

        self.assertEqual(seen, [original], "替换发生的那一刻，原文件已经不是原来那份了")

    def test_a_missing_parent_directory_is_a_loud_error_and_is_not_created(self):
        """⛔ 父目录**不新建**（spec §5.3：`shared/` 由发版建）：目录不在就响亮报错，⛔ 不 `mkdir`。"""
        shutil.rmtree(self.shared)
        with self.assertRaises(CallbackKeysError) as ctx:
            jidanpu_openapi.ensure_callback_key(_binding())
        self.assertFalse(self.shared.exists(), "⛔ 不该悄悄造出一个目录")
        self.assertIn(str(self.shared), str(ctx.exception), "报错要点名那个目录（排查要用）")

    # ── 权限 0600 ───────────────────────────────────────────────────────────

    def test_the_file_is_exactly_0600(self):
        result = jidanpu_openapi.ensure_callback_key(_binding())
        self.assertEqual(stat.S_IMODE(os.stat(result.path).st_mode), 0o600)

    def test_0600_is_forced_even_under_a_hostile_umask(self):
        """⭐ 进程 `umask` 是 `0o777` 时 `os.open(..., 0o600)` 只会给出 `0000` ⇒ 必须显式 `fchmod`。

        ⚠️ 这条是**唯一**能让「显式 `os.fchmod(fd, 0o600)`」变成承重逻辑的判据（spec §5.3 第 3 条
        点名的正是二者之差）：去掉那一句 ⇒ 这里读到 `0o000` ⇒ 红。
        """
        previous = os.umask(0o777)
        try:
            result = jidanpu_openapi.ensure_callback_key(_binding())
            mode = stat.S_IMODE(os.stat(result.path).st_mode)
        finally:
            os.umask(previous)
        self.assertEqual(mode, 0o600, f"密钥文件的权限是 {oct(mode)}")

    def test_an_existing_wider_mode_is_repaired(self):
        """已存在的 0644 文件 ⇒ 写一次之后是 0600（同一个 `fchmod` 口径的另一半）。"""
        self.path.write_text(f"{CODE_A}\t{SENTINEL_SECRET}\n", encoding="utf-8")
        os.chmod(self.path, 0o644)
        jidanpu_openapi.ensure_callback_key(_binding())
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_the_parent_directory_is_left_as_it_was(self):
        """⛔ 我们不动 `shared/` 自己的权限（那是发版建的）：写完之后它的模式**一位都不变**。"""
        before = stat.S_IMODE(os.stat(self.shared).st_mode)
        jidanpu_openapi.ensure_callback_key(_binding())
        self.assertEqual(stat.S_IMODE(os.stat(self.shared).st_mode), before)
        self.assertEqual(before & 0o077, 0, "夹具自己建的就是 0700（组/其他人不可读）")

    # ── 什么时候不该写 ──────────────────────────────────────────────────────

    def test_a_password_mode_binding_writes_nothing(self):
        """①式绑定（`mode="password"`）⇒ **一个字节都不写**：它没有回调地址、也没有那把密钥。"""
        self.assertIsNone(jidanpu_openapi.ensure_callback_key(_binding(mode="password")))
        self.assertFalse(self.path.exists())

    def test_a_binding_without_a_code_writes_nothing(self):
        """没有回调码（`callback_code=""`，只能是 shell 直写库那种行）⇒ 不写。"""
        self.assertIsNone(jidanpu_openapi.ensure_callback_key(_binding(code="")))
        self.assertFalse(self.path.exists())

    def test_an_empty_secret_writes_nothing(self):
        """⭐ 密钥为空 ⇒ **绝不写空值**：空值会让那条回调必然 401，而页面与库里一切正常（静默）。"""
        self.assertIsNone(jidanpu_openapi.ensure_callback_key(_binding(secret="")))
        self.assertIsNone(jidanpu_openapi.ensure_callback_key(_binding(secret="   ")))
        self.assertFalse(self.path.exists())

    def test_a_disabled_binding_is_still_written(self):
        """`enabled=False` **照写**：地址要先给用户看，用户常常「先配好再启用」。"""
        result = jidanpu_openapi.ensure_callback_key(_binding(enabled=False))
        self.assertIsNotNone(result, "关了同步开关不等于「这个码没有密钥」")
        self.assertTrue(self.path.exists())

    def test_the_written_value_is_the_stripped_db_value(self):
        """密钥照 `openapi_credentials_of` 的口径 `strip()` 一次（复制粘贴带来的首尾空白很常见）。"""
        jidanpu_openapi.ensure_callback_key(_binding(secret=f"  {SENTINEL_SECRET}\n"))
        self.assertEqual(
            callback_config.parse_callback_keys(self.path.read_text(encoding="utf-8")),
            {CODE_A: SENTINEL_SECRET},
        )

    # ── ⛔ 密钥不泄漏 ────────────────────────────────────────────────────────

    def test_the_secret_never_reaches_logs_or_the_return_value(self):
        """⛔ 哨兵密钥不许出现在日志 / 返回值里；而**文件里必须有它**（正向哨兵）。"""
        with _LogCapture() as logs:
            result = jidanpu_openapi.ensure_callback_key(_binding())
        self.assertIn(SENTINEL_SECRET, self.path.read_text(encoding="utf-8"),
                      "正向哨兵：密钥没写进文件 ⇒ 下面的否定断言没有判别力")
        for label, text in {
            "返回值 repr": repr(result),
            "返回值字段": "".join(f"{f}" for f in result.__dataclass_fields__),
            "日志": "\n".join(logs.messages),
        }.items():
            with self.subTest(where=label):
                self.assertNotIn(SENTINEL_SECRET, text)

    def test_a_write_failure_reports_no_secret(self):
        """⛔ 写失败时：异常文本 / `repr` / **完整回溯** / 日志里都没有密钥。"""

        def always_fail(fd, data):
            raise OSError("磁盘满了（打桩；这一句里也没有密钥）")

        with _LogCapture() as logs:
            with mock.patch.object(jidanpu_openapi, "_write_all", always_fail):
                with self.assertRaises(CallbackKeysError) as ctx:
                    jidanpu_openapi.ensure_callback_key(_binding())

        texts = {
            "异常文本": str(ctx.exception),
            "异常 repr": repr(ctx.exception),
            "回溯": "".join(traceback.format_exception(ctx.exception)),
            "日志": "\n".join(logs.messages),
        }
        for label, text in texts.items():
            with self.subTest(where=label):
                self.assertNotIn(SENTINEL_SECRET, text)


# ══ 回调地址拼装（C6 / C10 / C11）═════════════════════════════════════════════════

#: 本类用的租户：⚠️ **刻意**让域名 / schema / 租户名三者互不为子串，
#: 否则 C11 的 `assertNotIn` 会被域名自身的字符凑成假绿或假红（E-0083 同族）。
PRIMARY_DOMAIN = "t3cb-primary.test.invalid"
ALT_DOMAIN = "t3cb-alt.test.invalid"
#: ⚠️ schema 名**刻意不与域名共享任何子串**（`t3cbkeys` ⊄ `t3cb-primary.test.invalid`）：
#: 否则 C11 的 `assertNotIn(schema, url)` 会因为域名自身含有它而**假红**（E-0083 同族）。
TENANT_SCHEMA = "t3cbkeys"
TENANT_NAME = "合成租户甲（T3）"


class CallbackAddressTests(TenantTestCase):
    """`callback_address` / `callback_url` 的判据（真租户、真 `platform.Domain` 行）。"""

    @classmethod
    def get_test_schema_name(cls):
        return TENANT_SCHEMA

    @classmethod
    def get_test_tenant_domain(cls):
        return PRIMARY_DOMAIN

    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = TENANT_NAME

    def _binding(self, **overrides):
        fields = {
            "mode": "openapi", "callback_code": CODE_A,
            "client_secret": SENTINEL_SECRET, "client_id": CLIENT_ID, "enabled": True,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    def test_the_address_has_the_shape_of_section_5_1(self):
        """⭐ 形状逐字：`https://<该租户主域名>/api/jidanpu/callback/<22 位码>`。"""
        address = jidanpu_openapi.callback_address(self._binding())
        self.assertEqual(address.reason, "", "算得出来时不该有原因")
        self.assertEqual(
            address.url, f"https://{PRIMARY_DOMAIN}/api/jidanpu/callback/{CODE_A}",
        )
        self.assertEqual(jidanpu_openapi.callback_url(self._binding()), address.url)
        self.assertNotIn(PLATFORM_CALLBACK_HOST, address.url,
                         "那是平台侧那条老路，⛔ 不是每租户地址")

    def test_the_code_in_the_address_is_the_one_the_receiver_accepts(self):
        """⭐ C6：地址里那串码必须**过接收器的路径解析**（形状 = 22 个 `[A-Za-z0-9_-]`）。"""
        url = jidanpu_openapi.callback_url(self._binding())
        path = url.split(PRIMARY_DOMAIN, 1)[1]
        self.assertRegex(path, rf"^/api/jidanpu/callback/[A-Za-z0-9_-]{{22}}$")
        self.assertEqual(
            callback_wsgi.callback_code_of_path(path), CODE_A,
            "接收器解析出来的码与绑定的码不一致 ⇒ 这条地址在真机上取不到密钥",
        )
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{22}", CODE_A))

    def test_the_primary_domain_wins_over_the_other_domains(self):
        """⭐ C10：域名取**主域名** —— 另加一个域名之后地址仍然是主域名那一份。"""
        Domain.objects.create(domain=ALT_DOMAIN, tenant=self.tenant, is_primary=False)
        self.assertEqual(
            jidanpu_openapi.callback_url(self._binding()),
            f"https://{PRIMARY_DOMAIN}/api/jidanpu/callback/{CODE_A}",
        )
        # 换成另一个主域名（换域名部署的形态）⇒ 地址跟着换。
        Domain.objects.filter(domain=PRIMARY_DOMAIN).update(is_primary=False)
        Domain.objects.filter(domain=ALT_DOMAIN).update(is_primary=True)
        self.assertEqual(
            jidanpu_openapi.callback_url(self._binding()),
            f"https://{ALT_DOMAIN}/api/jidanpu/callback/{CODE_A}",
        )

    def test_the_address_carries_no_identifier_or_secret(self):
        """⭐ C11：地址里⛔ 不含 client_id / client_secret / 租户名 / schema 名。"""
        url = jidanpu_openapi.callback_url(self._binding())
        self.assertTrue(url, "正向哨兵：地址先得拼得出来")
        for label, value in {
            "client_id": CLIENT_ID,
            "client_secret": SENTINEL_SECRET,
            "租户名": TENANT_NAME,
            "schema 名": TENANT_SCHEMA,
        }.items():
            with self.subTest(where=label):
                self.assertNotIn(value, url)

    def test_a_password_mode_binding_has_no_address(self):
        """①式绑定 ⇒ 空串 + 原因（⛔ 不拼一个不属于它的地址）。"""
        address = jidanpu_openapi.callback_address(self._binding(mode="password"))
        self.assertEqual((address.url, address.reason),
                         ("", CALLBACK_URL_REASON_NOT_OPENAPI))

    def test_a_binding_without_a_code_has_no_address(self):
        address = jidanpu_openapi.callback_address(self._binding(callback_code=""))
        self.assertEqual((address.url, address.reason), ("", CALLBACK_URL_REASON_NO_CODE))

    def test_a_code_the_receiver_would_not_accept_yields_no_address(self):
        """⭐ 码的形状接收器不认（21 位 / 带 `/`）⇒ **空串 + 原因**，⛔ 绝不拼半截地址。

        弄红它的办法：把形状判据去掉（直接拿码拼 URL）—— 那种地址在 nginx 那一层就 404，
        而用户会把它填进鸡蛋铺那侧（静默）。
        """
        for bad in (CODE_SHORT, "AbCdEfGhIjKlMnOpQrSt/U", CODE_A + "x"):
            with self.subTest(code=bad):
                address = jidanpu_openapi.callback_address(
                    self._binding(callback_code=bad)
                )
                self.assertEqual((address.url, address.reason),
                                 ("", CALLBACK_URL_REASON_BAD_CODE))

    def test_a_tenant_without_any_domain_row_has_no_address(self):
        """⭐ 查不到主域名 ⇒ 空串 + 原因（⛔ 不回落 `example.com`、⛔ 不拼半截地址）。"""
        Domain.objects.filter(tenant=self.tenant).delete()
        address = jidanpu_openapi.callback_address(self._binding())
        self.assertEqual((address.url, address.reason), ("", CALLBACK_URL_REASON_NO_DOMAIN))

    def test_the_address_and_the_reason_are_never_both_filled_or_both_empty(self):
        """⭐ **契约**（`CallbackAddress` 的不变式；2026-10-08 修复轮补的那一条）：

            **`url` 与 `reason` 恰好一个非空** —— 算得出来 ⇒（`url` 非空 **且** `reason` 空），
            算不出来 ⇒（`url` 空 **且** `reason` 非空）。**两向都要钉**。

        ⚠️ **为什么必须单独有一条**：上面那几条各钉**一个分支的取值**，而这一条钉的是
        **所有分支共有的那个形状**。少了它，「两个都填了」或「两个都空」这类改动可能从
        每一条各自的分支缝里滑过去（页面那一侧 `callbackCellState` 的判据就建在它上面：
        页面按「有地址就显示地址、没地址就显示原因」渲染 ⇒ 不变式一破，页面会把一句
        原因当成地址显示给用户，或者显示一个空格子）。

        ⚠️ **要能红**：把 `callback_address` 的成功那一支
        改成 `reason=<某句人话>` ⇒ 「两个都非空」红；改成 `url=""`（或把任一失败分支的原因
        写成空串）⇒ 「两个都空」红。
        """
        cases = [
            #: (标签, 绑定字段, 这一档**该**算得出地址吗)
            ("算得出来", {}, True),
            ("①式（没有回调地址这回事）", {"mode": "password"}, False),
            ("没有回调码", {"callback_code": ""}, False),
            ("码的形状接收器不认", {"callback_code": CODE_SHORT}, False),
        ]
        for label, overrides, computable in cases:
            with self.subTest(where=label):
                address = jidanpu_openapi.callback_address(self._binding(**overrides))
                #: ① **不变式本体**：恰好一个非空（⛔ 不是「至少一个非空」那种恒真判据）
                self.assertNotEqual(
                    address.url != "", address.reason != "",
                    f"`url` 与 `reason` 必须恰好一个非空：url={address.url!r} reason={address.reason!r}",
                )
                #: ② **方向**（两向各自承重：只断①的话「两向都反了」照样绿）
                if computable:
                    self.assertEqual(
                        address.url, f"https://{PRIMARY_DOMAIN}/api/jidanpu/callback/{CODE_A}",
                    )
                    self.assertEqual(address.reason, "")
                else:
                    self.assertEqual(address.url, "")
                    self.assertNotEqual(address.reason, "", "算不出来却不给原因（页面只能显示空白）")

        #: ③ 「查不到主域名」那一档也在同一张表里（要先把域名行删掉，故单独跑一遍）
        Domain.objects.filter(tenant=self.tenant).delete()
        address = jidanpu_openapi.callback_address(self._binding())
        self.assertNotEqual(address.url != "", address.reason != "")
        self.assertEqual(address.url, "")
        self.assertNotEqual(address.reason, "")


# ══ 接线：这个端点**自己不写**回调密钥文件（那一步在下家那条 PUT 上）════════════════


class CallbackKeyWiringTests(TenantTestCase):
    """`PUT /api/business/member/upstream-jidanpu-binding/` 与密钥文件的接线判据。

    ⚠️ 断言的落点：**文件内容**（写没写、写了什么）与**数据库行**（回滚没有），⛔ 不打 logger。

    ## ⭐ 「保存绑定 ⇒ 写那份 0600 文件」那三条判据**不在本类里了**（改准）

    本类原先还有三条**正向**接线判据（写成功 / `enabled=False` 照写 / 写失败整体回滚）。
    业务方裁定**上家线不需要②式官方API那一套**（`jidanpu_upstream_views.py`
    的 PUT 里那次 `ensure_callback_key(binding)` 连同 import 一起撤掉了）⇒ 那三条测的接线
    **在本文件这个端点上今天不存在**，它们**跟着接线搬到了下家**（`jidanpu_member_views.py`
    的 PUT，`apps/business/api/jidanpu_member_views.py:653`）。逐条的去处：

    | 本类原先那条 | 今天在哪 | 为什么 |
    |---|---|---|
    | 写成功（码 → 密钥 + 0600 + 调用一次） | `tests/test_jidanpu_member_api.py` 的 `JidanpuMemberOpenApiBindingTests::test_saving_an_openapi_binding_writes_the_0600_key_file` | 判据等价（文件字节 + 0600 + 调用参数），**下家侧已有一条**⇒ 这里是重复的 |
    | `enabled=False` 照写 | 同上（那条用例的 `_put` 默认就是 `enabled=False`）+ `…::test_enabling_an_openapi_binding_also_writes_the_key_file`（`enabled=True` 那一档） | 两档各一条才完整：错写成 `if binding.enabled:` / `if not binding.enabled:` 各红一条 |
    | 写失败 ⇒ 整个保存回滚 | `…::test_a_key_file_write_failure_rolls_the_whole_save_back` + `…::test_a_key_file_write_failure_leaves_an_existing_row_untouched` | 下家侧那两条更强（新建支「一行都不留」+ 更新支「一个字节都不改」） |

    ⛔ **不是把那三条判据弄没了**：三条今天都活在**下家那条 PUT** 上，且各自能红
    （把 `ensure_callback_key(binding)` 删掉 / 加 `if binding.enabled:` / 反过来加
    `if not binding.enabled:` / 吞掉异常 —— 四条变异的弄红读数见本线收口报告）。

    留在这里的三条是**这个端点**自己的判据：①式不写 / 空密钥不写 / 不回显密钥
    ——上家线撤掉那一步之后它们**依然成立**（它们钉的是「这个 PUT 绝不产生回调密钥行」）。
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tt-t3-wire-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.shared = self.root / "shared"
        self.shared.mkdir(mode=0o700)
        self.path = self.shared / "callback-keys.env"
        patcher = mock.patch.dict(os.environ, {"CALLBACK_KEYS_FILE": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = make_logged_in_client(username=ACCOUNT)
        self.user = User.objects.get(username=ACCOUNT)
        #: ②式绑定（码由模型层生成）。⚠️ 这正是 T10/T11 把 `mode` / `client_secret`
        #: 放进序列化器之后用户能造出来的那一行 —— 现在先用 ORM 造出来。
        #: ⭐ **T11 补了 `client_id`**（台账 §22.4-2 说的就是这个形状）：`mode="openapi"` 的必填面
        #: 是 **client_id + client_secret**（spec §4.3 的不变式）⇒ T11 之后
        #: 「②式但没有 client_id」的行**连启用都过不去**（`_credentials_missing` 挡住，
        #: 因为 `openapi_credentials_of` 缺了它根本取不到 token）。
        #: ⛔ 这不是放宽判据，而是把夹具改成**T11 之后的真实行形状**（本文件的判据一个字没动）。
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account=ACCOUNT,
            password=PASSWORD, mode="openapi", client_id=CLIENT_ID,
            client_secret=SENTINEL_SECRET, enabled=False,
        )

    def _put(self, **overrides):
        payload = {"jidanpu_base_url": BASE_URL, "account": ACCOUNT, "password": PASSWORD}
        payload.update(overrides)
        return self.client.put(
            BINDING_URL, json.dumps(payload), content_type="application/json",
        )

    def test_put_on_a_password_mode_binding_writes_nothing(self):
        """①式绑定（默认方式）⇒ ⛔ 那份文件一个字节都不写。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(mode="password")
        res = self._put()
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(self.path.exists(), "①式绑定不该产生任何回调密钥行")

    def test_put_with_an_empty_secret_writes_nothing(self):
        """②式但密钥是空的 ⇒ 不写（⛔ 绝不写空值：那会让回调必然 401 而页面正常）。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(client_secret="")
        res = self._put()
        self.assertEqual(res.json()["code"], 0, res.content)
        self.assertFalse(self.path.exists())

    def test_put_never_returns_the_secret(self):
        """⛔ 响应体里不许出现那把密钥（它只写不读，C3 同族）。"""
        res = self._put()
        self.assertNotIn(SENTINEL_SECRET, res.content.decode("utf-8"))
