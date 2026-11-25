"""鸡蛋铺**回调接收器**（`apps/callback/**`）的判据网；spec `docs/specs/2026-10-08-jidanpu-callback-receiver-design.md`
与 `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md`（§5.3/§5.4；判据 = §13.1 A1–A4/A6–A8）。

签名对拍两腿：**文档算例**（权威；夹具 = 文档原文，在**不进 git** 的 `.tools/recon/`、含示例密钥
⛔ 不抄进 `src/**`；worktree 里没有它 ⇒ 用 `CALLBACK_DOC_FIXTURE` 指过去）
+ **合成夹具字面期望值**（md5 用 `hashlib` 独立算出再抄入，⛔ 自指断言改了不会红，见 E-0024）。
新路径 404/401/503 三档分得开、两条路互不兜底；旧路径（正在收真实回调）状态码表与落盘键集合逐条冻结。
发版接线判据只有读码支撑（本机没有 nginx / systemd），⛔ 绿 ≠ 机器上验过。
接收器**不碰库**（连 Django 都不经过）⇒ 全 `SimpleTestCase`；WSGI 用例直接拿 `environ` 调 `application`。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from apps.callback import config, signature, store, wsgi

from .test_deploy_sync_inventory import rendered_sync_script

#: `src/tests/` 往上两级 = 仓库根。
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
DEPLOY = REPO_ROOT / "deploy"

#: 第三方文档的原始文本（**不进 git**，含示例密钥）。可用环境变量换一份。
DOC_FIXTURE = Path(
    os.environ.get("CALLBACK_DOC_FIXTURE")
    or (REPO_ROOT / ".tools" / "recon" / "jidanpu-openapi" / "raw-doc.txt")
)

#: 从文档里抓那条算例：`示例：<待签串>】以上参数进行加密后得到sign为<32 位小写 hex>`。
_DOC_EXAMPLE_RE = re.compile(
    r"示例：(?P<presign>.+?)】以上参数进行加密后得到sign为(?P<sign>[0-9a-f]{32})"
)

# ═══════════════════════════════════════════════════════════════════════════
# 合成夹具（⛔ 一律合成值，见 `AGENTS.md` §5-C / E-0077）
# 期望值是用 `hashlib` **独立**算出来再抄进来的（不是拿 `signature.compute_sign` 现算）。
# ═══════════════════════════════════════════════════════════════════════════

SECRET = "tt-callback-test-secret-0001"

#: F1：文档形状的基准报文（`note` 是**空串** ⇒ 按文档必须**不参与**签名）。
F1_PARAMS = {
    "orderNo": "TT202610080000000001",
    "sourceOrderNo": "SRC-TT-0001",
    "status": "代练中",
    "note": "",
    "action": "10",
    "timestamp": "1791500000",
}
F1_PRESIGN = (
    f"action=10&clientSecret={SECRET}&orderNo=TT202610080000000001"
    "&sourceOrderNo=SRC-TT-0001&status=代练中&timestamp=1791500000"
)
F1_SIGN = "8c91a50c0d70f6dc0494d3296fde4f91"

#: F1b：**反例** —— 把空 `note` 也拼进去的那个待签串（必须与 F1 不同、且不等于文档公布的 sign）。
F1B_PRESIGN = (
    f"action=10&clientSecret={SECRET}&note=&orderNo=TT202610080000000001"
    "&sourceOrderNo=SRC-TT-0001&status=代练中&timestamp=1791500000"
)
F1B_SIGN = "c95b56ea038e4379ef6280efa80e0696"

#: F2：**未知 action** + 非空 `note`（向前兼容那一支）。
F2_PARAMS = {
    "orderNo": "TT202610080000000001",
    "sourceOrderNo": "SRC-TT-0001",
    "status": "已撤单",
    "note": "有备注",
    "action": "99",
    "timestamp": "1791500123",
}
F2_PRESIGN = (
    f"action=99&clientSecret={SECRET}&note=有备注&orderNo=TT202610080000000001"
    "&sourceOrderNo=SRC-TT-0001&status=已撤单&timestamp=1791500123"
)
F2_SIGN = "53a97df043f564cfb7044739a540f722"

#: F3：`amount=0` —— **数值 0 必须参与签名**（真值判空会把"传了 0"当成"没传"，见 E-0021 家族）。
F3_PARAMS = {
    "orderNo": "TT202610080000000001",
    "sourceOrderNo": "SRC-TT-0001",
    "status": "Y",
    "action": "50",
    "timestamp": "1791500200",
    "amount": 0,
}
F3_PRESIGN = (
    f"action=50&amount=0&clientSecret={SECRET}&orderNo=TT202610080000000001"
    "&sourceOrderNo=SRC-TT-0001&status=Y&timestamp=1791500200"
)
F3_SIGN = "24fb4ff3258c71022b7677b98d1baa50"

# ═══════════════════════════════════════════════════════════════════════════
# 新路径（`/api/jidanpu/callback/<码>`）的夹具（spec §5.1 / §5.3）
# ⚠️ 一律**合成值**（`AGENTS.md` §5-C / E-0077）：码是 22 位合成串、密钥是合成哨兵。
# ═══════════════════════════════════════════════════════════════════════════

#: 文件里**有**的那串码（22 字符、只含 `[A-Za-z0-9_-]`）。
CODE_A = "Ab3_-callback_codes_XY"

#: 文件里**没有**的那串码（形状合法、22 位）。
CODE_ABSENT = "Dd5_-callback_codes_QQ"

#: `CODE_A` 那一行对应的密钥。⚠️ 与旧路径的 `SECRET` **不同**：
#: 「两条路互不兜底」那条判据就是靠这个差异立起来的。
SECRET_CODE_A = "tt-per-code-secret-0001"

#: `callback-keys.env` 的**首行注释**（spec §5.3 的固定文案；⛔ 实现里不许拿它做判据，
#: 这里只是照着 T3 要写出来的形状造夹具）。
KEYS_FILE_HEADER = "# 团团通 · 鸡蛋铺官方 API 回调密钥（0600；由「保存绑定」自动维护；⛔ 人工改要重启接收器）"

#: ⭐ 记录的**键集合**（**字面量**，⛔ 不从 `_new_record` 派生）：新旧记录形状统一的判据；
#: 也是「旧路径落盘形状一个字没变」的判据（往记录里加一个键 ⇒ 这几条红）。
RECORD_KEYS = frozenset({
    "record_version",
    "received_at",
    "method",
    "path",
    "query_string",
    "source_ip",
    "peer_addr",
    "headers",
    "sign_ok",
    "sign_reason",
    "sign_keys",
    "sign_ok_declared_fields_only",
    "json_ok",
    "body_len",
    "body_sha256",
    "body_stored",
    #: ⭐ H-1（record_version 4）：非空 = **原文已丢弃、只留元数据**。键集合仍统一
    #: （`_new_record` 给全所有键）⇒ 这一格本身**不能**用来分辨新老形状，只有值能。
    "body_dropped_reason",
    "body_b64",
    "body_text",
})

#: 记录格式版本（spec §5.4：2 → 3 → **4**，H-1 加 `body_dropped_reason`）。
#: **字面量**：⛔ 不写 `wsgi.RECORD_VERSION`（自指断言，E-0024）。
RECORD_VERSION_LITERAL = 4


def _callback_path(code: str) -> str:
    """新路径的地址（⛔ 只有这一处拼它，避免几条用例各自拼出一个不同的形状）。"""
    return f"{wsgi.CALLBACK_PATH_PREFIX}{code}"


def _sign(params: dict, secret: str = SECRET, *, declared_only: bool = False) -> str:
    """**独立**签名实现（照文档口径手写，⛔ 不调 `signature.compute_sign`；用它算期望值就是自指断言，E-0024）。

    ⭐ 默认 = **报文的全部顶层字段都参与**（文档口径）；`declared_only=True` 只算文档字段表，
    用来伪造"双方对参与字段口径不一致"那一支（真机首日 39 条 `action=30` 误拒即旧默认造成的）。
    """
    items = [
        (key, str(value))
        for key, value in params.items()
        if key != signature.SIGN_FIELD_NAME
        and value is not None
        and value != ""
        and (not declared_only or key in signature.SIGNED_FIELDS)
    ]
    items.append((signature.SECRET_FIELD_NAME, secret))
    items.sort(key=lambda item: item[0])
    return hashlib.md5("&".join(f"{k}={v}" for k, v in items).encode()).hexdigest()


def _signed_payload(params: dict, secret: str = SECRET) -> bytes:
    body = dict(params)
    body[signature.SIGN_FIELD_NAME] = _sign(params, secret)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class _Capture:
    """WSGI 的 `start_response` 替身：记下状态码与响应头。"""

    def __init__(self) -> None:
        self.status: str | None = None
        self.headers: dict[str, str] = {}

    def __call__(self, status, headers):
        self.status = status
        self.headers = {name.lower(): value for name, value in headers}


def _environ(
    body: bytes,
    *,
    method: str = "POST",
    path: str = wsgi.NOTIFY_PATH,
    remote_addr: str = "127.0.0.1",
    x_real_ip: str | None = None,
    content_length: int | None = None,
    content_type: str = "application/json",
    extra: dict | None = None,
) -> dict:
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "REMOTE_ADDR": remote_addr,
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(body) if content_length is None else content_length),
        "wsgi.input": BytesIO(body),
        "SERVER_PROTOCOL": "HTTP/1.1",
    }
    if x_real_ip is not None:
        environ["HTTP_X_REAL_IP"] = x_real_ip
    if extra:
        environ.update(extra)
    return environ


def _call(environ: dict) -> tuple[str, dict, _Capture]:
    """直接调 WSGI 应用，返回 `(状态码, 响应体 JSON, 响应头替身)`。"""
    capture = _Capture()
    payloads = wsgi.application(environ, capture)
    body = b"".join(payloads)
    parsed = json.loads(body.decode("utf-8")) if body else None
    return capture.status or "", parsed, capture


def _read_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _mask_secret(presign: str) -> str:
    """把待签串里 `clientSecret=` 之后那一段换成占位符（**断言消息里不许出现密钥**）。"""
    marker = f"{signature.SECRET_FIELD_NAME}="
    head, _, tail = presign.partition(marker)
    if not tail:
        return presign
    rest = tail.split("&", 1)
    return head + marker + "<密钥>" + ("&" + rest[1] if len(rest) > 1 else "")


class _ReceiverCase(SimpleTestCase):
    """把事件文件与密钥文件指到临时目录（两者都靠环境变量覆盖，⛔ 不写死路径）。"""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "shared" / "callback" / "events.jsonl"
        self.env_file = self.tmp / "shared" / "callback.env"
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        self.env_file.write_text(f"{config.SECRET_VAR_NAME}={SECRET}\n", encoding="utf-8")
        self.env_file.chmod(0o600)
        #: ⭐ 新路径的「码 → 密钥」文件（spec §5.3）：**另一份**文件，⛔ 不塞进 `callback.env`。
        self.keys_file = self.tmp / "shared" / "callback-keys.env"
        self.keys_file.write_text(
            f"{KEYS_FILE_HEADER}\n{CODE_A}\t{SECRET_CODE_A}\n", encoding="utf-8",
        )
        self.keys_file.chmod(0o600)
        patcher = mock.patch.dict(
            os.environ,
            {
                "CALLBACK_EVENTS_PATH": str(self.events),
                "CALLBACK_ENV_FILE": str(self.env_file),
                "CALLBACK_KEYS_FILE": str(self.keys_file),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # ⭐ H-1：限流状态是**进程级**的（`wsgi._RATE_WINDOWS`）⇒ 每条用例从干净窗口起步，
        # 否则整份套件在同一个 60 秒窗口里累计请求数、靠后的用例会撞上 429（假红）。
        wsgi._RATE_WINDOWS.clear()
        wsgi._REJECTED_SEEN.clear()
        self.addCleanup(wsgi._RATE_WINDOWS.clear)
        self.addCleanup(wsgi._REJECTED_SEEN.clear)

    def records(self) -> list[dict]:
        return _read_records(self.events)

    def last_record(self) -> dict:
        return self.records()[-1]

    def record_count(self) -> int:
        """已落盘记录条数（文件还不存在时是 0 —— ⛔ 不用 `exists()` 当"零条"的判据）。"""
        return len(self.records()) if self.events.exists() else 0

    def journal_text(self) -> str:
        return self.events.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# ① 权威对拍：第三方文档给的算例
# ═══════════════════════════════════════════════════════════════════════════


class DocWorkedExampleTests(SimpleTestCase):
    """⭐ 拿**文档公布的算例**验我们这套实现（这是唯一的权威对拍，不是自指）。"""

    def setUp(self) -> None:
        super().setUp()
        if not DOC_FIXTURE.is_file():
            self.skipTest(
                f"对拍夹具不在：{DOC_FIXTURE}（第三方文档原文，含示例密钥 ⇒ 按 AGENTS.md §5-C "
                "不进 git）⇒ 拿不到算例就没法对拍。有原文时用 CALLBACK_DOC_FIXTURE=… 指过去。"
            )
        match = _DOC_EXAMPLE_RE.search(DOC_FIXTURE.read_text(encoding="utf-8"))
        self.assertIsNotNone(
            match,
            f"{DOC_FIXTURE} 里没找到那条算例（正则：示例：<待签串>】…得到sign为<32 位 hex>）"
            " ⇒ 文档改版了，请重新取夹具与期望值",
        )
        presign = match.group("presign")
        self.doc_sign = match.group("sign")
        self.doc_presign = presign
        pairs = dict(item.split("=", 1) for item in presign.split("&"))
        self.doc_secret = pairs.pop(signature.SECRET_FIELD_NAME)
        # 自证夹具真的读到了东西（否则下面的断言会落在空字典上、空洞通过）。
        self.assertTrue(self.doc_secret, "文档算例里没解析出 clientSecret")
        self.assertIn("orderNo", pairs, f"文档算例里没解析出 orderNo：{sorted(pairs)}")
        self.doc_params = pairs

    def test_our_implementation_reproduces_the_documented_presign_string(self):
        """⭐ 我们拼出来的待签串必须与文档给的**逐字相同**（排序、分隔符、密钥的位置全都在里面）。

        ⚠️ 断言里**只打待签串的形状判断、不打密钥**：失败消息里把 `clientSecret=` 之后那一段
        换成占位符（本仓口径：密钥不进任何输出）。
        """
        composed = signature.build_presign_string(self.doc_params, self.doc_secret)
        self.assertEqual(
            _mask_secret(composed),
            _mask_secret(self.doc_presign),
            "待签串与文档算例不一致（排序 / 分隔符 / 空值处理 / 密钥的位置）",
        )

    def test_our_implementation_reproduces_the_documented_sign(self):
        """⭐ 文档算例：我们的实现必须算出文档公布的那个 `sign`。

        ⚠️ 密钥取自文档原文（不进 git），⛔ 断言里**只打签名、不打密钥**。
        """
        computed = signature.compute_sign(self.doc_params, self.doc_secret)
        self.assertEqual(
            computed,
            self.doc_sign,
            "签名算法与文档算例对不上：待签串的排序 / 空值处理 / 编码 / md5 里至少有一处错了"
            f"（本次算出 {computed}，文档公布 {self.doc_sign}）",
        )

    def test_empty_note_must_not_participate(self):
        """**反例**：把空 `note` 也算进去 ⇒ **得不到**文档那个 `sign`。

        这是"空值不参与签名"这条规则**唯一的实证**：文档同时给了待签串与结果，
        而待签串里没有 `note`（文档正文也写着「其中note为空，不参与签名」）。
        """
        pairs = dict(self.doc_params)
        pairs["note"] = ""
        items = sorted(
            [(key, str(value)) for key, value in pairs.items()]
            + [(signature.SECRET_FIELD_NAME, self.doc_secret)]
        )
        wrong = hashlib.md5("&".join(f"{k}={v}" for k, v in items).encode()).hexdigest()
        self.assertNotEqual(
            wrong,
            self.doc_sign,
            "把空 note 也算进去竟然也能得到文档公布的 sign ⇒ 说明这条算例对『空值不参与』没有判别力",
        )
        # 我们的实现给出的待签串里**不许**出现 note（先自证两条串确实不同）。
        composed = signature.build_presign_string(pairs, self.doc_secret)
        self.assertNotEqual(composed, "&".join(f"{k}={v}" for k, v in items), "自证失败：空值竟然参与了")
        self.assertNotIn("note=", composed)


# ═══════════════════════════════════════════════════════════════════════════
# ② 算法本身的回归钉（合成夹具 + 字面期望值）
# ═══════════════════════════════════════════════════════════════════════════


class SignatureAlgorithmTests(SimpleTestCase):
    def test_signed_fields_are_exactly_the_documented_ones(self):
        """参与签名的字段是**显式枚举**的（加/删一个字段 ⇒ 本用例红，逼人回来读文档）。"""
        self.assertEqual(
            signature.SIGNED_FIELDS,
            ("action", "amount", "note", "orderNo", "sourceOrderNo", "status", "timestamp"),
        )

    def test_presign_string_and_sign_for_the_baseline_fixture(self):
        """基准夹具：待签串**逐字**相等（排序 + 空 `note` 不参与 + 密钥位置）。"""
        self.assertEqual(signature.build_presign_string(F1_PARAMS, SECRET), F1_PRESIGN)
        self.assertEqual(signature.compute_sign(F1_PARAMS, SECRET), F1_SIGN)

    def test_empty_note_variant_differs_from_the_baseline(self):
        """反例的字面读数：空 `note` 参与后**既不等于**基准、也不等于基准的另一种拼法。

        ⚠️ 先自证两条串真的不同（否则下面的断言恒真）。
        """
        self.assertNotEqual(F1_PRESIGN, F1B_PRESIGN, "自证失败：两条待签串一样，本用例不产出结论")
        baseline = signature.compute_sign(F1_PARAMS, SECRET)
        wrong = hashlib.md5(F1B_PRESIGN.encode()).hexdigest()
        self.assertEqual(wrong, F1B_SIGN, "反例的期望值本身不对（自证失败，不产出结论）")
        self.assertNotEqual(baseline, wrong)
        # 我们的实现**不许**把空 note 拼进去（把 note 填成空串，结果必须与基准一致）。
        self.assertEqual(signature.compute_sign({**F1_PARAMS, "note": ""}, SECRET), F1_SIGN)
        self.assertEqual(signature.compute_sign({**F1_PARAMS, "note": None}, SECRET), F1_SIGN)

    def test_non_empty_note_and_unknown_action_are_signed(self):
        """非空 `note` 参与、**未知 action 不特殊对待**（照样是普通字段 ⇒ 不影响算法）。"""
        self.assertEqual(signature.build_presign_string(F2_PARAMS, SECRET), F2_PRESIGN)
        self.assertEqual(signature.compute_sign(F2_PARAMS, SECRET), F2_SIGN)

    def test_numeric_zero_participates(self):
        """`amount=0`（**数值 0**，不是字符串）必须参与签名 ⇒ 与 `"0"` 拼出同一个串。"""
        self.assertEqual(signature.build_presign_string(F3_PARAMS, SECRET), F3_PRESIGN)
        self.assertEqual(signature.compute_sign(F3_PARAMS, SECRET), F3_SIGN)
        self.assertEqual(
            signature.compute_sign({**F3_PARAMS, "amount": "0"}, SECRET),
            F3_SIGN,
            "数值 0 与字符串 '0' 拼出来的待签串应当一致（都参与签名）",
        )
        self.assertNotEqual(
            signature.compute_sign({key: value for key, value in F3_PARAMS.items() if key != "amount"}, SECRET),
            F3_SIGN,
            "把 amount 丢掉竟然得到同一个签名 ⇒ 说明 0 被当成空值丢掉了",
        )

    def test_sign_is_lowercase_hex_of_32_chars(self):
        computed = signature.compute_sign(F1_PARAMS, SECRET)
        self.assertEqual(len(computed), 32)
        self.assertEqual(computed, computed.lower())
        self.assertRegex(computed, r"^[0-9a-f]{32}$")

    def test_empty_secret_is_refused_instead_of_hashing_an_empty_key(self):
        """空密钥 ⇒ 抛，⛔ 不拼出一条"看起来正常"的待签串（那会让对比悄悄变成可预测的）。"""
        with self.assertRaises(signature.UnsignableValue):
            signature.build_presign_string(F1_PARAMS, "")

    def test_non_scalar_value_is_refused(self):
        """某字段是 dict / list ⇒ 拼不出待签串 ⇒ 抛（调用方 fail-closed，⛔ 不猜形状）。"""
        with self.assertRaises(signature.UnsignableValue):
            signature.build_presign_string({**F1_PARAMS, "note": {"a": 1}}, SECRET)

    def test_verify_reports_each_reason(self):
        """四条结论各自可辨（`ok` / 缺 sign / 对不上 / 读不到密钥）。"""
        body = _signed_payload(F1_PARAMS)
        params = json.loads(body)
        good = signature.verify_sign(params, params["sign"], SECRET)
        self.assertTrue(good.ok)
        self.assertEqual(good.reason, signature.REASON_OK)
        self.assertIn(signature.SECRET_FIELD_NAME, good.signed_keys)

        missing = signature.verify_sign(F1_PARAMS, None, SECRET)
        self.assertFalse(missing.ok)
        self.assertEqual(missing.reason, signature.REASON_SIGN_MISSING)

        mismatch = signature.verify_sign(params, "0" * 32, SECRET)
        self.assertFalse(mismatch.ok)
        self.assertEqual(mismatch.reason, signature.REASON_SIGN_MISMATCH)

        no_secret = signature.verify_sign(params, params["sign"], None)
        self.assertFalse(no_secret.ok)
        self.assertEqual(no_secret.reason, signature.REASON_SECRET_UNAVAILABLE)

        unsignable = signature.verify_sign({**params, "note": [1]}, params["sign"], SECRET)
        self.assertFalse(unsignable.ok)
        self.assertEqual(unsignable.reason, signature.REASON_UNSIGNED_VALUE)

    def test_sign_is_compared_in_constant_time(self):
        """⛔ 比较必须用 `hmac.compare_digest`（`==` 会按首个不同字符提前返回）。

        ⚠️ 弱判据（读源码文本不是行为）：只保证"那一行还在"，让删掉它必须是有意的。
        """
        source = (SRC_DIR / "apps" / "callback" / "signature.py").read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest", source)
        self.assertNotIn("== provided", source)
        self.assertNotIn("provided ==", source)

    def test_received_sign_tolerates_case_and_surrounding_whitespace_only(self):
        """收到的 `sign` 只容忍**大小写**与**首尾空白**（`_normalise_provided_sign`，有意登记过）。

        ⚠️ 容忍生效的**同时**必须证明改坏一个字符 / 长度不对 / 非 hex 仍**失败**
        （单侧归一化会让判据失去判别力，`AGENTS.md` §5-G / E-0083 家族）。
        """
        good = _sign(F1_PARAMS)
        self.assertEqual(good, F1_SIGN, "自证失败：夹具签名与字面期望值不一致，本用例不产出结论")
        for variant, label in (
            (good.upper(), "全大写 hex"),
            (f"  {good}  ", "首尾空白"),
            (f"\t{good.upper()}\n", "制表符 + 换行 + 大写"),
        ):
            with self.subTest(variant=label):
                self.assertTrue(
                    signature.verify_sign(F1_PARAMS, variant, SECRET).ok,
                    f"{label} 的签名被判失败 ⇒ 容忍没生效",
                )
        # ⛔ 边界一：**改坏一个字符**仍然必须失败（否则归一化把判据吃了）。
        broken = ("0" if good[0] != "0" else "1") + good[1:]
        broken_check = signature.verify_sign(F1_PARAMS, broken, SECRET)
        self.assertFalse(broken_check.ok, "改坏一个字符竟然通过了")
        self.assertEqual(broken_check.reason, signature.REASON_SIGN_MISMATCH)
        # ⛔ 边界二：长度不对 / 非 hex 也失败。
        self.assertFalse(signature.verify_sign(F1_PARAMS, good[:-1], SECRET).ok)
        self.assertFalse(signature.verify_sign(F1_PARAMS, "z" * 32, SECRET).ok)
        # 我们这一侧永远是规范形态（小写）⇒ 归一化只作用在"收到的值"上。
        self.assertEqual(signature.compute_sign(F1_PARAMS, SECRET), good)

    def test_a_field_outside_the_documented_table_is_signed_and_verifies(self):
        """⭐ **回归判据（真机首日抓到的缺陷）**：**文档字段表之外的字段照样参与签名**。

        真实形状 `action=30` 带 `imgUrl` / `name`（都不在文档字段表里，鸡蛋铺却算进了签名）；
        只按文档表算 ⇒ 真回调被误判 `sign_mismatch`（假阴性，不是安全事件）。
        失败方向（`AGENTS.md` §5-G「每条钉子都要弄红」）：① 主判改回只算 `SIGNED_FIELDS` ⇒ 红；
        ② 算全字段但漏掉新字段 ⇒ 红；③ `_present` 把空值也算进去 ⇒ 真实形状那条红。
        """
        # ① 真实形状：action=30 + imgUrl + name（都是文档字段表里没有的）
        real_shape = {
            "action": "30",
            "orderNo": "2025062409091188820",
            "sourceOrderNo": "54556512310200",
            "note": "",
            "imgUrl": "https://example.invalid/shot/1.png",
            "name": "打手甲",
            "timestamp": "1750859828",
        }
        self.assertNotIn("imgUrl", signature.SIGNED_FIELDS, "自证：imgUrl 确实不在文档字段表里")
        self.assertNotIn("name", signature.SIGNED_FIELDS, "自证：name 确实不在文档字段表里")
        check = signature.verify_sign(real_shape, _sign(real_shape), SECRET)
        self.assertTrue(check.ok, f"文档表外的字段必须参与签名（reason={check.reason}）")
        self.assertEqual(check.reason, signature.REASON_OK)
        # 实际进了待签串的 key 要**如实列出来**（⛔ 不是照着文档表报）
        self.assertIn("imgUrl", check.signed_keys)
        self.assertIn("name", check.signed_keys)

        # ② 拿"只算文档字段表"的签名来打 ⇒ 必须失败，且诊断字段 True（这正是真机 39 条的形状）
        declared_only = _sign(real_shape, declared_only=True)
        self.assertNotEqual(declared_only, _sign(real_shape), "自证：两种口径必须算出不同的签名")
        check2 = signature.verify_sign(real_shape, declared_only, SECRET)
        self.assertFalse(check2.ok)
        self.assertEqual(check2.reason, signature.REASON_SIGN_MISMATCH)
        self.assertIs(check2.ok_declared_fields_only, True, "诊断字段必须指出「文档那一组是对的」")

        # ③ 末尾那条判据：**改坏新字段的值**必须失败（否则"算全字段"就没意义了）
        tampered = {**real_shape, "imgUrl": "https://example.invalid/shot/EVIL.png"}
        self.assertFalse(signature.verify_sign(tampered, _sign(real_shape), SECRET).ok)

    def test_an_empty_optional_field_still_does_not_participate(self):
        """空值不参与签名这条**没有被上面那次改准动摇**（`note=""` 与"没有 note"必须等价）。"""
        params = {k: v for k, v in F1_PARAMS.items() if k != "note"}
        with_empty_note = {**params, "note": ""}
        self.assertEqual(signature.compute_sign(params, SECRET), signature.compute_sign(with_empty_note, SECRET))
        self.assertTrue(signature.verify_sign(with_empty_note, _sign(params), SECRET).ok)


# ═══════════════════════════════════════════════════════════════════════════
# ③ 端点行为
# ═══════════════════════════════════════════════════════════════════════════


class ReceiverEndpointTests(_ReceiverCase):
    def test_good_sign_returns_200_with_empty_object_and_records_it(self):
        status, payload, capture = _call(_environ(_signed_payload(F1_PARAMS)))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload, {})
        self.assertEqual(capture.headers["content-type"], "application/json; charset=utf-8")
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["sign_ok"])
        self.assertEqual(record["sign_reason"], signature.REASON_OK)
        self.assertEqual(record["record_version"], wsgi.RECORD_VERSION)
        self.assertEqual(record["path"], wsgi.NOTIFY_PATH)
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["body_len"], len(_signed_payload(F1_PARAMS)))
        self.assertEqual(
            record["body_sha256"], hashlib.sha256(_signed_payload(F1_PARAMS)).hexdigest()
        )
        self.assertEqual(record["sign_keys"], ["action", "clientSecret", "orderNo", "sourceOrderNo", "status", "timestamp"])

    def test_bad_sign_returns_401_and_still_records(self):
        """⛔ 验签失败**不许装成功**：回 401，但**照样记录**（留取证）+ `sign_ok=false`。"""
        body = _signed_payload(F1_PARAMS, secret="wrong-secret-000000000000000000")
        status, payload, _ = _call(_environ(body))
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload, {"error": "sign_invalid"})
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertFalse(records[0]["sign_ok"])
        self.assertEqual(records[0]["sign_reason"], signature.REASON_SIGN_MISMATCH)

    def test_missing_sign_returns_401_and_records_the_reason(self):
        body = json.dumps(F1_PARAMS, ensure_ascii=False).encode()
        status, _, _ = _call(_environ(body))
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(self.records()[0]["sign_reason"], signature.REASON_SIGN_MISSING)

    def test_unknown_action_is_accepted_and_recorded(self):
        """⭐ 未知 `action`：照样记录 + 回 200（向前兼容；⛔ 不做"白名单校验"）。"""
        payload = _signed_payload({**F1_PARAMS, "action": "9999"})
        status, body, _ = _call(_environ(payload))
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, {})
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]["sign_ok"])
        self.assertIn('"action": "9999"', records[0]["body_text"])

    def test_oversize_body_is_rejected_and_recorded_without_the_raw_body(self):
        """超限 ⇒ 413 + 记录（长度 / sha256 留着、**原文不落盘**）。"""
        big = b"x" * 5000
        with mock.patch.object(wsgi, "MAX_BODY_BYTES", 100):
            status, payload, _ = _call(_environ(big))
        self.assertEqual(status, "413 Payload Too Large")
        self.assertEqual(payload, {"error": "body_too_large"})
        record = self.records()[0]
        self.assertFalse(record["body_stored"])
        self.assertIsNone(record["body_b64"])
        self.assertIsNone(record["body_text"])
        self.assertEqual(record["body_len"], 5000)
        self.assertEqual(record["body_sha256"], hashlib.sha256(big).hexdigest())
        self.assertEqual(record["sign_reason"], "body_too_large")

    def test_declared_length_beyond_the_read_limit_is_not_read_at_all(self):
        """`Content-Length` 比读的上限还大 ⇒ **一个字节都不读**（别为了算指纹把 1 GB 拉进来）。"""
        status, _, _ = _call(
            _environ(b"", content_length=wsgi.HARD_READ_LIMIT + 1)
        )
        self.assertEqual(status, "413 Payload Too Large")
        record = self.records()[0]
        self.assertIsNone(record["body_sha256"])
        self.assertEqual(record["body_len"], wsgi.HARD_READ_LIMIT + 1)

    def test_non_json_body_is_rejected_with_400_and_recorded(self):
        status, payload, _ = _call(_environ(b"not json at all"))
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(payload, {"error": "body_not_json"})
        record = self.records()[0]
        self.assertFalse(record["json_ok"])
        self.assertEqual(record["sign_reason"], "body_not_json")
        # ⭐ H-1：拒收档**不落原文**——只留元数据（原文字段全空 + 原因非空）。
        self.assertEqual(record["body_dropped_reason"], "body_not_json")
        self.assertFalse(record["body_stored"])
        self.assertIsNone(record["body_text"])
        self.assertIsNone(record["body_b64"])

    def test_json_array_body_is_rejected_with_400(self):
        status, _, _ = _call(_environ(b"[1, 2, 3]"))
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(self.records()[0]["sign_reason"], "body_not_json")

    def test_raw_body_is_stored_byte_for_byte(self):
        """⭐ **逐字节**留档（含中文、含不确定的键顺序与空白）—— ⛔ 不是 `loads` 再 `dumps` 的那份。"""
        raw = (
            '{\n  "status": "已撤销",\n   "orderNo" : "TT202610080000000001",\n'
            '  "note": "第一行\\n第二行",\n  "brandNewField": "未登记",\n'
            '  "sourceOrderNo": "SRC-TT-0001", "action": "10",\n'
            '  "timestamp": "1791500000"\n}'
        ).encode("utf-8")
        params = json.loads(raw.decode())
        body = json.dumps({**params, "sign": _sign({k: v for k, v in params.items() if k != "sign"})},
                          ensure_ascii=False).encode("utf-8")
        status, _, _ = _call(_environ(body))
        self.assertEqual(status, "200 OK")
        record = self.records()[0]
        self.assertEqual(base64.b64decode(record["body_b64"]), body)
        self.assertEqual(record["body_text"], body.decode("utf-8"))
        # 自证：**原文**与"重新序列化一遍"确实不同（否则本用例证明不了"没被覆盖"）。
        reserialized = json.dumps(json.loads(body.decode()), ensure_ascii=False, indent=2)
        self.assertNotEqual(reserialized, body.decode("utf-8"))
        # 一条记录只占一行（JSON 会把字符串里的换行转义）⇒ 事件文件的行数就是事件条数。
        self.assertEqual(len(self.journal_text().splitlines()), 1)

    def test_a_json_string_with_newlines_still_stays_on_one_line(self):
        raw = json.dumps(
            {**F1_PARAMS, "note": "第一行\n第二行"}, ensure_ascii=False
        ).encode("utf-8")
        params = json.loads(raw.decode())
        body = json.dumps(
            {**params, "sign": _sign({k: v for k, v in params.items() if k != "sign"})},
            ensure_ascii=False,
        ).encode("utf-8")
        status, _, _ = _call(_environ(body))
        self.assertEqual(status, "200 OK")
        lines = self.journal_text().splitlines()
        self.assertEqual(len(lines), 1, "记录被换行切成了多行 ⇒ 事件文件不再是『一行一条』")
        self.assertEqual(json.loads(lines[0])["body_text"], body.decode("utf-8"))

    def test_source_ip_comes_from_x_real_ip_and_peer_addr_is_the_socket_peer(self):
        status, _, _ = _call(
            _environ(
                _signed_payload(F1_PARAMS),
                remote_addr="127.0.0.1",
                x_real_ip="203.0.113.9",
                extra={"HTTP_USER_AGENT": "jidanpu-callback/1.0"},
            )
        )
        self.assertEqual(status, "200 OK")
        record = self.records()[0]
        self.assertEqual(record["source_ip"], "203.0.113.9")
        self.assertEqual(record["peer_addr"], "127.0.0.1")
        headers = dict(record["headers"])
        self.assertEqual(headers["x-real-ip"], "203.0.113.9")
        self.assertEqual(headers["user-agent"], "jidanpu-callback/1.0")
        self.assertEqual(headers["content-type"], "application/json")

    def test_source_ip_falls_back_to_the_peer_when_the_header_is_absent(self):
        _call(_environ(_signed_payload(F1_PARAMS), remote_addr="198.51.100.7"))
        record = self.records()[0]
        self.assertEqual(record["source_ip"], "198.51.100.7")
        self.assertEqual(record["peer_addr"], "198.51.100.7")

    def test_secret_unavailable_returns_503_and_fails_closed(self):
        """读不到密钥 ⇒ **503**（不是 401：那是我们的配置问题，回 401 等于说假话）。"""
        os.environ["CALLBACK_ENV_FILE"] = str(self.tmp / "nope.env")
        status, payload, _ = _call(_environ(_signed_payload(F1_PARAMS)))
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload, {"error": "verifier_unavailable"})
        record = self.records()[0]
        self.assertFalse(record["sign_ok"])
        self.assertEqual(record["sign_reason"], signature.REASON_SECRET_UNAVAILABLE)

    def test_the_secret_never_lands_in_the_journal(self):
        """⛔ 密钥不许进事件文件（拿哨兵值直接搜文件）。"""
        _call(_environ(_signed_payload(F1_PARAMS)))
        _call(_environ(b'{"note": "boom"}'))  # 再来一条验签失败的
        self.assertNotIn(SECRET, self.journal_text())

    def test_the_secret_file_mode_warning_is_raised_but_does_not_block(self):
        """密钥文件对属主之外可读 ⇒ **响亮警告**，但照样服务（拒服务会让回调全变 5xx）。"""
        self.env_file.chmod(0o644)
        with self.assertLogs("apps.callback.config", level="WARNING") as logs:
            status, _, _ = _call(_environ(_signed_payload(F1_PARAMS)))
        self.assertEqual(status, "200 OK")
        self.assertTrue(any("chmod 600" in line for line in logs.output))

    def test_get_is_not_recorded(self):
        """⛔ 非 POST 不落盘（这个路径对公网可达，记每个 GET = 白送一个写盘放大口）。"""
        status, payload, _ = _call(_environ(b"", method="GET"))
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(payload, {"error": "method_not_allowed"})
        self.assertFalse(self.events.exists(), "非 POST 竟然落了盘")

    def test_other_paths_are_404_and_not_recorded(self):
        for path in ("/", "/jidanpu/v1/notify/", "/jidanpu/v1/notify/extra", "/api/business/orders/"):
            with self.subTest(path=path):
                status, payload, _ = _call(_environ(_signed_payload(F1_PARAMS), path=path))
                self.assertEqual(status, "404 Not Found")
                self.assertEqual(payload, {"error": "not_found"})
        self.assertFalse(self.events.exists(), "打到别的路径竟然落了盘")

    def test_query_string_is_kept_in_the_record(self):
        environ = _environ(_signed_payload(F1_PARAMS))
        environ["QUERY_STRING"] = "debug=1"
        _call(environ)
        self.assertEqual(self.records()[0]["query_string"], "debug=1")

    def test_all_records_share_the_same_key_set(self):
        """记录形状统一：不同结局的记录**键集合相同**（⛔ 不静默少键，否则 `jq` 筛子会漏）。"""
        _call(_environ(_signed_payload(F1_PARAMS)))
        _call(_environ(b"not json"))
        _call(_environ(b'{"a": 1}'))
        records = self.records()
        self.assertGreaterEqual(len(records), 3, "自证失败：样本不够，本用例不产出结论")
        key_sets = {frozenset(record) for record in records}
        self.assertEqual(len(key_sets), 1, f"记录键集合不一致：{key_sets}")


# ═══════════════════════════════════════════════════════════════════════════
# ④ 并发写：一行都不能坏
# ═══════════════════════════════════════════════════════════════════════════


class ConcurrentAppendTests(_ReceiverCase):
    def test_concurrent_writers_never_produce_a_broken_line(self):
        """多线程同时追加 ⇒ 每一行都必须是完整、可解析的一条 JSON（⛔ 不许出现半行）。

        能红的方向是真实的：改成带缓冲的 `open(path, "a")` 再 `write()`、或去掉 `flock`
        改成"先 `seek` 到末尾再写"，都会让两条记录交错、产出坏行。
        ⚠️ 载荷取 4 KB 级（远大于一条普通回调）就是为了让"短写 / 交错"有机会发生。
        """
        workers = 8
        per_worker = 25
        barrier = threading.Barrier(workers)
        padding = "。" * 1500

        def _worker(index: int) -> None:
            barrier.wait(timeout=10)
            for seq in range(per_worker):
                store.append_record(
                    {
                        "record_version": 1,
                        "worker": index,
                        "seq": seq,
                        "body_text": f"worker={index} seq={seq} {padding}",
                    },
                    path=self.events,
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_worker, index) for index in range(workers)]
            for future in futures:
                future.result(timeout=60)

        lines = self.journal_text().splitlines()
        expected = workers * per_worker
        self.assertEqual(len(lines), expected, f"行数应当是 {expected}（自证失败：样本不对）")
        seen = set()
        for line in lines:
            record = json.loads(line)  # 坏行会在这里抛
            seen.add((record["worker"], record["seq"]))
        self.assertEqual(len(seen), expected, "有记录被覆盖或重复 ⇒ 说明写入不是原子/串行的")

    def test_append_creates_the_directory_and_the_file_with_tight_modes(self):
        """事件目录与文件的权限：目录 0700、文件 0600（报文里可能有业务信息）。"""
        store.append_record({"record_version": 1}, path=self.events)
        self.assertEqual(self.events.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.events.parent.stat().st_mode & 0o777, 0o700)

    def test_0600_survives_a_hostile_umask(self):
        """⭐ `umask 0o777` 时 `os.open(..., 0o600)` 只会给出 `0o000` ⇒ 必须显式 `fchmod`。

        与密钥文件那条同形（`tests/test_jidanpu_callback_keys.py` 的
        `test_0600_is_forced_even_under_a_hostile_umask`）；事件文件留 PII（`source_ip` / 订单号原文），
        ⛔ 弄红它：去掉 `store.append_record` 里那句 `os.fchmod(fd, FILE_MODE)` ⇒ 读到 `0o000`。
        """
        #: ⚠️ 目录先建好（本用例钉的是**文件**那一格）：恶意 umask 下 `mkdir(0o700)` 会给出
        #: `0o000` ⇒ 连文件都建不出来，测的就成另一件事了。
        self.events.parent.mkdir(parents=True, exist_ok=True)
        previous = os.umask(0o777)
        try:
            store.append_record({"record_version": 1}, path=self.events)
            mode = self.events.stat().st_mode & 0o777
        finally:
            os.umask(previous)
        self.assertEqual(mode, 0o600, f"事件文件的权限是 {oct(mode)}")

    def test_an_existing_wider_mode_is_repaired(self):
        """已存在的 `0644` 文件 ⇒ 再写一条之后变成 `0600`（`os.open` 的 mode 只在新建那一次有效）。

        对已上线接收器安全：文件由接收器自己首次写入时建出（属主是它，`fchmod` 必然成功）。
        ⛔ 弄红它：同上（去掉那句 `fchmod`）⇒ 写完仍是 `0o644`。
        """
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("", encoding="utf-8")
        os.chmod(self.events, 0o644)

        store.append_record({"record_version": 1}, path=self.events)

        self.assertEqual(self.events.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(self.journal_text().splitlines()), 1, "这一条没写进去")


# ═══════════════════════════════════════════════════════════════════════════
# ④b H-1：拒收只落元数据 / 按来源 IP 限流 / jsonl 轮转往返
# ═══════════════════════════════════════════════════════════════════════════


class RejectedRequestMetadataTests(_ReceiverCase):
    """H-1 ①：**验签前不落原文**——拒收档只写元数据行（时刻/IP/长度/sha256/原因）。"""

    def test_a_bad_sign_stores_metadata_only(self):
        """验签失败 ⇒ 原文 / 请求头 / 查询串全部丢弃（`body_dropped_reason` 非空）。"""
        head = {"HTTP_USER_AGENT": "attacker/" + "x" * 200, "HTTP_X_EXTRA": "y" * 200}
        body = _signed_payload(F1_PARAMS, secret="wrong-secret-000000000000000000")
        environ = _environ(body, x_real_ip="203.0.113.77", extra=head)
        environ["QUERY_STRING"] = "q=" + "z" * 500
        status, _, _ = _call(environ)
        self.assertEqual(status, "401 Unauthorized")
        record = self.records()[0]
        self.assertFalse(record["sign_ok"])
        self.assertEqual(record["body_dropped_reason"], signature.REASON_SIGN_MISMATCH)
        self.assertFalse(record["body_stored"])
        self.assertIsNone(record["body_b64"])
        self.assertIsNone(record["body_text"])
        # ⭐ 放大面就在这里：请求头与查询串也是攻击者可控的字节 ⇒ 元数据行一个都不留。
        self.assertEqual(record["headers"], [])
        self.assertEqual(record["query_string"], "")
        # 留下的全是「排查要用的元数据」：长度 / sha256 / 来源 IP / 路径 / 时刻。
        self.assertEqual(record["body_len"], len(body))
        self.assertEqual(record["body_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(record["source_ip"], "203.0.113.77")
        self.assertEqual(record["path"], wsgi.NOTIFY_PATH)
        self.assertTrue(record["received_at"])
        # 自证：同一份报文用**对的**密钥 ⇒ 原文逐字节落盘（否则上面那批可能只是"从来不落原文"）。
        good = _signed_payload(F1_PARAMS)
        self.assertEqual(_call(_environ(good))[0], "200 OK")
        stored = self.records()[-1]
        self.assertTrue(stored["body_stored"])
        self.assertIsNone(stored["body_dropped_reason"])
        self.assertEqual(base64.b64decode(stored["body_b64"]), good)

    def test_identical_rejected_bodies_are_aggregated_within_the_window(self):
        """⭐ H-1 评审意见「聚合」：同 IP + 同 sha256 + 同原因，一个窗口内**只写一条**。

        ⚠️ 只聚合**拒收**档：验签通过的记录一条就是一条（下方阳性对照）。
        """
        body = _signed_payload(F1_PARAMS, secret="wrong-secret-000000000000000000")
        self.assertEqual(_call(_environ(body))[0], "401 Unauthorized")
        self.assertEqual(_call(_environ(body))[0], "401 Unauthorized")
        self.assertEqual(len(self.records()), 1, "同一份被拒报文在同一窗口里写了两行")
        # 换一份报文（不同 sha256）⇒ 是新的一行（聚合不吞掉不同的攻击样本）。
        self.assertEqual(
            _call(_environ(_signed_payload(F1_PARAMS, secret="another-wrong-secret-000")))[0],
            "401 Unauthorized",
        )
        self.assertEqual(len(self.records()), 2)
        # 阳性对照：**验签通过**的同一份报文来两次 ⇒ 两条完整记录（不聚合）。
        good = _signed_payload(F1_PARAMS)
        self.assertEqual(_call(_environ(good))[0], "200 OK")
        self.assertEqual(_call(_environ(good))[0], "200 OK")
        self.assertEqual(len(self.records()), 4)
        self.assertTrue(self.records()[-1]["body_stored"])

    def test_unknown_code_and_non_json_are_also_metadata_only(self):
        """码不认识 / 不是 JSON 对象 ⇒ 同样只落元数据（⛔ 不是「只挡验签失败」这一档）。"""
        _call(_environ(
            _signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_ABSENT)
        ))
        _call(_environ(b'{"broken": '))
        records = self.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["body_dropped_reason"], "unknown_callback_code")
        self.assertEqual(records[1]["body_dropped_reason"], "body_not_json")
        for record in records:
            self.assertIsNone(record["body_b64"])
            self.assertEqual(record["headers"], [])


class SourceIpRateLimitTests(_ReceiverCase):
    """H-1 ②：按来源 IP 的**进程内**固定窗限流（429 且不落盘）。"""

    def setUp(self) -> None:
        super().setUp()
        wsgi._RATE_WINDOWS.clear()
        self.addCleanup(wsgi._RATE_WINDOWS.clear)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(wsgi.ENV_RATE_LIMIT, None)

    def test_over_the_limit_is_429_and_writes_nothing(self):
        """窗口内超限 ⇒ 429 + `rate_limited`，**一条记录都不写**（限流不能再变成写盘口）。"""
        body = _signed_payload(F1_PARAMS)
        with mock.patch.object(wsgi, "RATE_LIMIT_MAX_REQUESTS", 2):
            self.assertEqual(_call(_environ(body))[0], "200 OK")
            self.assertEqual(_call(_environ(body))[0], "200 OK")
            before = self.record_count()
            status, payload, _ = _call(_environ(body))
        self.assertEqual(status, "429 Too Many Requests")
        self.assertEqual(payload, {"error": "rate_limited"})
        self.assertEqual(self.record_count(), before, "被限流的请求竟然落了盘")

    def test_the_limit_is_per_source_ip(self):
        """另一个来源 IP 不受影响（判据是 X-Real-IP 那个值，⛔ 不是全局开关）。"""
        body = _signed_payload(F1_PARAMS)
        with mock.patch.object(wsgi, "RATE_LIMIT_MAX_REQUESTS", 1):
            self.assertEqual(_call(_environ(body, x_real_ip="203.0.113.1"))[0], "200 OK")
            self.assertEqual(_call(_environ(body, x_real_ip="203.0.113.1"))[0], "429 Too Many Requests")
            self.assertEqual(_call(_environ(body, x_real_ip="203.0.113.2"))[0], "200 OK")

    def test_the_window_expires(self):
        """固定窗到期 ⇒ 计数归零（判据直接打在 `_rate_limited` 上，⛔ 不等真 60 秒）。"""
        with mock.patch.object(wsgi, "RATE_LIMIT_MAX_REQUESTS", 1):
            self.assertEqual(wsgi._rate_limited("198.51.100.9", now=1000.0), (False, False))
            self.assertEqual(wsgi._rate_limited("198.51.100.9", now=1001.0), (True, True))
            # 同一窗口里的后续拒收不再打日志（返回 False 的「首次」标记）。
            self.assertEqual(wsgi._rate_limited("198.51.100.9", now=1002.0), (True, False))
            # 窗口（60 秒）到期 ⇒ 重新放行。
            self.assertEqual(wsgi._rate_limited("198.51.100.9", now=1061.0), (False, False))

    def test_the_env_override_is_optional_and_invalid_values_fall_back(self):
        """`CALLBACK_RATE_LIMIT_PER_MINUTE` 可选：合法值生效；非法 / 0 ⇒ 回落默认 + warning。"""
        os.environ[wsgi.ENV_RATE_LIMIT] = "3"
        self.assertEqual(wsgi._rate_limit_max(), 3)
        for raw in ("abc", "0", "-5"):
            with self.subTest(raw=raw):
                os.environ[wsgi.ENV_RATE_LIMIT] = raw
                with self.assertLogs("apps.callback.wsgi", level="WARNING"):
                    self.assertEqual(
                        wsgi._rate_limit_max(), wsgi.RATE_LIMIT_MAX_REQUESTS
                    )

    def test_the_tracking_table_is_bounded(self):
        """伪造 IP 不能把进程内存撑爆：超过 `RATE_LIMIT_MAX_TRACKED` 会被清理。"""
        with mock.patch.object(wsgi, "RATE_LIMIT_MAX_TRACKED", 8):
            for index in range(50):
                wsgi._rate_limited(f"203.0.113.{index}", now=1000.0 + index)
        self.assertLessEqual(len(wsgi._RATE_WINDOWS), 8)


class JsonlRotationTests(_ReceiverCase):
    """H-1 ③：jsonl 按大小轮转 —— **承重**：已验签的行一个不丢、不崩、不重复消费。

    判据形态 = 消费侧真实读法：全局字节偏移 + `read_records_from` 往后再读，
    各轮读到的 `seq` 拼起来必须**恰好**等于写入顺序（不少一条、不多一条）。
    """

    ROW_PADDING = "。" * 40

    def _row(self, seq: int, *, worker: int = 0) -> dict:
        return {
            "record_version": 4,
            "worker": worker,
            "seq": seq,
            "body_text": f"worker={worker} seq={seq} {self.ROW_PADDING}",
        }

    def test_rotation_archives_the_file_and_keeps_global_offsets_continuous(self):
        with mock.patch.object(store, "ROTATE_BYTES", 400):
            for seq in range(12):
                store.append_record(self._row(seq), path=self.events)
        archives = store.segment_files(self.events)
        self.assertGreaterEqual(len(archives), 1, "阈值 400 字节、写 12 条 ⇒ 必须真的轮转过")
        self.assertEqual(archives[0][0], 0, "第一段的全局起始偏移必须是 0")
        for (base, entry), (next_base, _next) in zip(archives, archives[1:]):
            self.assertEqual(base + entry.stat().st_size, next_base, "段与段必须首尾相接")
        self.assertLess(self.events.stat().st_size, 400, "轮转后现役文件应当重新变小")

        whole = store.read_records_from(self.events, 0)
        self.assertEqual([row["seq"] for row in whole.records], list(range(12)))
        snap = store.snapshot(self.events)
        self.assertEqual(whole.new_offset, snap.total_bytes)
        # 偏移落在第一段中间 ⇒ 只读它之后那些（不重读、也不跳过）。
        cutoff = whole.line_ends[5]
        rest = store.read_records_from(self.events, cutoff)
        self.assertEqual([row["seq"] for row in rest.records], list(range(6, 12)))
        self.assertEqual(rest.new_offset, snap.total_bytes)
        # `iter_lines`（排查命令）也要能看到归档段里的行。
        self.assertEqual(len(list(store.iter_lines(self.events))), 12)

    def test_a_consumer_by_offset_never_loses_or_repeats_rows_across_rotations(self):
        """⭐ 轮转往返：消费侧每轮按偏移读 ⇒ 12 条恰好各读到一次（这是承重判据）。"""
        seen: list[int] = []
        offset = 0
        with mock.patch.object(store, "ROTATE_BYTES", 300):
            for seq in range(12):
                store.append_record(self._row(seq), path=self.events)
                chunk = store.read_records_from(self.events, offset)
                seen.extend(row["seq"] for row in chunk.records)
                offset = chunk.new_offset
        self.assertGreaterEqual(len(store.segment_files(self.events)), 2, "自证失败：没轮转过")
        self.assertEqual(seen, list(range(12)), f"消费侧读到 {seen}（丢行或重复）")
        # 追平之后：再读一次必须是**空**（不重复消费），且偏移不倒退。
        final = store.read_records_from(self.events, offset)
        self.assertEqual(final.records, ())
        self.assertGreaterEqual(final.new_offset, offset)
        self.assertEqual(offset, store.snapshot(self.events).total_bytes)

    def test_a_live_consumer_racing_rotation_never_loses_or_repeats_rows(self):
        """⭐ 读写竞态：消费者按偏移读的同时写者持续 append + 轮转 ⇒ 每条恰好读一次。

        判别力：去掉 `store._read_lock` 的共享锁 ⇒ 列段与读活文件之间可能跨过一次轮转、
        跳过还没读的行（本用例红）。这段竞态**真的会丢行**，不是理论洁癖。
        """
        total = 24
        def _writer() -> None:
            for seq in range(total):
                store.append_record(self._row(seq), path=self.events)
                time.sleep(0.001)

        seen: list[int] = []
        offset = 0
        with mock.patch.object(store, "ROTATE_BYTES", 260):
            writer = threading.Thread(target=_writer)
            writer.start()
            try:
                deadline = time.monotonic() + 30
                while len(seen) < total and time.monotonic() < deadline:
                    chunk = store.read_records_from(self.events, offset)
                    seen.extend(row["seq"] for row in chunk.records)
                    if chunk.new_offset != offset:
                        offset = chunk.new_offset
                    else:
                        time.sleep(0.001)
            finally:
                writer.join(timeout=30)
        self.assertEqual(seen, list(range(total)), f"读到的序列是 {seen}（丢行 / 重复）")
        self.assertGreaterEqual(len(store.segment_files(self.events)), 1, "自证失败：没轮转过")

    def test_rotation_under_concurrent_writers_never_produces_a_broken_line(self):
        """轮转 + 并发写：每一行都完整、可解析，且没有任何一行丢失 / 重复。"""
        workers, per_worker = 4, 15
        barrier = threading.Barrier(workers)

        def _worker(worker: int) -> None:
            barrier.wait(timeout=10)
            for seq in range(per_worker):
                store.append_record(self._row(seq, worker=worker), path=self.events)

        with mock.patch.object(store, "ROTATE_BYTES", 600):
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_worker, worker) for worker in range(workers)]
                for future in futures:
                    future.result(timeout=60)
        chunk = store.read_records_from(self.events, 0)
        expected = workers * per_worker
        self.assertEqual(len(chunk.records), expected, f"读到 {len(chunk.records)} / 应 {expected}")
        seen = {(row["worker"], row["seq"]) for row in chunk.records}
        self.assertEqual(len(seen), expected, "有记录被覆盖或重复")
        self.assertGreaterEqual(len(store.segment_files(self.events)), 1, "自证失败：没轮转过")


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ 排查命令
# ═══════════════════════════════════════════════════════════════════════════


class CallbackEventsCommandTests(_ReceiverCase):
    def _run(self, *args: str) -> str:
        out = StringIO()
        call_command("callback_events", *args, stdout=out, stderr=out)
        return out.getvalue()

    def _seed(self) -> None:
        _call(_environ(_signed_payload(F1_PARAMS)))
        # 一条**验签对不上**的（签名是 32 个 0；⚠️ 得用 `json.dumps` 造，⛔ 别在 bytes 字面量里写算式）。
        bad = json.dumps(
            {"note": "bad", "sign": "0" * 32, "action": "10", "orderNo": "TT-1"},
            ensure_ascii=False,
        ).encode("utf-8")
        _call(_environ(bad))

    def test_it_prints_a_summary_per_event_with_the_file_and_counts(self):
        self._seed()
        output = self._run("--limit", "10")
        self.assertIn("sign=ok", output)
        self.assertIn("sign=FAIL(sign_mismatch)", output)
        self.assertIn("action=10", output)
        self.assertIn("sourceOrderNo=SRC-TT-0001", output)
        self.assertIn(str(self.events), output)
        self.assertIn("共 2 行", output)
        self.assertIn("验签通过 1 / 未通过 1", output)

    def test_limit_keeps_the_newest_records(self):
        """`--limit N` 取的是**最新** N 条（排查问的永远是"刚刚那一条怎么了"）。

        ⚠️ 记号必须放在摘要**真会打印**的字段上（`body_text` 里的 `orderNo`；用 `--raw` 才有的键
        如 `seq` 会恒真、等于没判）⇒ `--limit` 被忽略或取了最旧几条时都红。
        """
        for index in range(5):
            body_text = json.dumps(
                {"orderNo": f"TT-LIMIT-{index:04d}", "sourceOrderNo": "SRC-1", "action": "10"},
                ensure_ascii=False,
            )
            store.append_record(
                {
                    "record_version": 1,
                    "received_at": f"2026-10-08T00:00:0{index}+00:00",
                    "source_ip": "203.0.113.9",
                    "sign_ok": True,
                    "sign_reason": "ok",
                    "json_ok": True,
                    "body_len": len(body_text),
                    "body_stored": True,
                    "body_text": body_text,
                },
                path=self.events,
            )
        output = self._run("--limit", "2")
        self.assertIn("本次显示 2 条", output)
        # 留下的是**最新两条**；被砍掉的是最旧那两条（这两条 `assertNotIn` 就是"能红"的那两条）。
        self.assertIn("orderNo=TT-LIMIT-0004", output)
        self.assertIn("orderNo=TT-LIMIT-0003", output)
        self.assertNotIn("orderNo=TT-LIMIT-0000", output, "--limit 2 竟然打印了最旧那条")
        self.assertNotIn("orderNo=TT-LIMIT-0001", output, "--limit 2 竟然打印了第 2 条")

    def test_summary_mode_prints_only_small_fields_while_raw_mode_prints_the_rest(self):
        """摘要模式⛔ 不打印大字段（逐字节 base64 / 请求头）；要看完整记录得给 `--raw`。

        ⚠️ 必须配对称自证（`--raw` 里必须看得到它们）：否则"摘要里没有"在字段压根不存在时
        照样成立（空洞通过）。任一侧改坏 ⇒ 红。
        """
        _call(_environ(_signed_payload(F1_PARAMS)))
        summary = self._run("--limit", "5")
        raw = self._run("--limit", "5", "--raw")
        for field in ("body_b64", "headers", "record_version", "body_sha256"):
            with self.subTest(field=field):
                # 自证：这些字段**真的在**（`--raw` 看得到）⇒ 下面那条断言才不是恒真。
                self.assertIn(field, raw, f"`--raw` 里看不到 {field} ⇒ 本用例的自证失败，不产出结论")
                self.assertNotIn(
                    field, summary, f"摘要模式打印了 {field}（排查时会把一行刷成几十行）"
                )
        # 摘要该有的东西必须在（否则"少打印"会退化成"什么都不打印"）。
        for expected in ("sign=ok", "action=10", "len=", "orderNo=", "sourceOrderNo="):
            with self.subTest(expected=expected):
                self.assertIn(expected, summary)

    def test_bad_sign_filter_shows_only_failures(self):
        self._seed()
        output = self._run("--bad-sign")
        self.assertNotIn("sign=ok", output)
        self.assertIn("sign=FAIL(sign_mismatch)", output)
        self.assertIn("已按 --bad-sign 过滤", output)

    def test_broken_lines_are_shown_and_flagged_loudly(self):
        """坏行必须**看得见**（并发写坏一行正是这个命令最该暴露的东西）。"""
        self._seed()
        with self.events.open("a", encoding="utf-8") as handle:
            handle.write('{"record_version": 1, "note": "被截断的一行"\n')
        output = self._run()
        self.assertIn("坏行 / 不是 JSON 对象", output)
        self.assertIn("坏行 1", output)
        self.assertIn("并发写坏了行", output)

    def test_raw_mode_redacts_any_field_whose_name_looks_like_a_secret(self):
        """⛔ 输出里不打印任何密钥：连"第三方在报文里回显了密钥"这种情形也要盖住。

        两条路都要盖：① 记录里**键名**含 `secret` 的字段；② `body_text` 那**一整段字符串**
        （按键名递归进不去字符串里）⇒ 命令会把能解成对象的 `body_text` 按打码后重写。
        """
        sentinel = "sentinel-must-not-be-printed-0001"
        encoded_body = base64.b64encode(json.dumps({"clientSecret": sentinel}).encode()).decode()
        _call(_environ(_signed_payload(F1_PARAMS)))
        store.append_record(
            {
                "record_version": 1,
                "sign_ok": True,
                "sign_reason": "ok",
                "body_text": json.dumps({"clientSecret": sentinel, "action": "10"}),
                "body_b64": encoded_body,
            },
            path=self.events,
        )
        raw = self._run("--raw")
        summary = self._run()
        for output in (raw, summary):
            self.assertNotIn(sentinel, output, "命令输出里出现了密钥值")
        self.assertIn("***", raw)
        # 逐字节那一段（base64）没法按键名打码 ⇒ `--raw` 里整个省略（要看原文直接读事件文件）。
        self.assertIn("已省略", raw)
        self.assertNotIn(encoded_body, raw, "body_b64 原样打出来了 ⇒ 回显在报文里的密钥会跟着泄漏")

    def test_missing_file_says_so_instead_of_printing_an_empty_list(self):
        """文件不在 ⇒ 明确说"一条都没有"，⛔ 不许静默给个空输出（静默空比报错更贵）。"""
        output = self._run("--path", str(self.tmp / "nope" / "events.jsonl"))
        self.assertIn("一条事件都没有", output)
        self.assertIn("nope", output)

    def test_path_argument_overrides_the_environment(self):
        other = self.tmp / "elsewhere" / "events.jsonl"
        store.append_record({"record_version": 1, "sign_ok": True, "marker": "only-here"}, path=other)
        output = self._run("--path", str(other), "--raw")
        self.assertIn("only-here", output)
        self.assertIn(str(other), output)


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ 独立于 Django（这是"接收器不碰库"的接线判据）
# ═══════════════════════════════════════════════════════════════════════════


class StandaloneFromDjangoTests(SimpleTestCase):
    def test_receiver_modules_do_not_import_django(self):
        """接收器的四个模块**不许** import django（"不走按 Host 认租户、不碰库"的实现判据）。

        ⚠️ 只查**模块级 import**（函数里偷偷 import 查不出）；`wsgi.py` 引入 `django.http`
        就会把 `settings` 拉进来 ⇒ 本用例红。
        """
        import ast

        for name in ("wsgi", "signature", "store", "config"):
            path = SRC_DIR / "apps" / "callback" / f"{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            with self.subTest(module=name):
                self.assertFalse(
                    [item for item in imported if item == "django" or item.startswith("django.")],
                    f"{path.name} 的模块级 import 里出现了 django：{imported}",
                )

    def test_the_receiver_starts_without_any_django_settings(self):
        """真起一遍：**不带** `DJANGO_SETTINGS_MODULE` 的子进程里 import 必须成功。

        ⚠️ 只证明"import 期不依赖 settings"；价值在它是**行为**判据（真跑新进程），不是读源码。
        """
        env = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import apps.callback.wsgi as w; print(w.NOTIFY_PATH)",
            ],
            cwd=str(SRC_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, f"子进程失败：{result.stderr}")
        self.assertEqual(result.stdout.strip(), wsgi.NOTIFY_PATH)


# ═══════════════════════════════════════════════════════════════════════════
# ⑦ 发版接线（⚠️ 只有读码支撑：本机没有 nginx、也没有 systemd）
# ═══════════════════════════════════════════════════════════════════════════


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _logical_lines(text: str) -> list[str]:
    """把 shell 的 `\\` 续行拼成逻辑行（否则「这一行里有 rsync 吗」会看错行）。"""
    joined: list[str] = []
    buffer = ""
    for line in text.splitlines():
        buffer += line
        if buffer.endswith("\\"):
            buffer = buffer[:-1] + " "
            continue
        joined.append(buffer)
        buffer = ""
    if buffer:
        joined.append(buffer)
    return joined


class CallbackUnitTests(SimpleTestCase):
    """`tuantuantong-callback.service` 自己的契约。"""

    _UNIT = DEPLOY / "systemd" / "tuantuantong-callback.service"

    def test_the_unit_exists_and_starts_the_receiver_on_8002(self):
        """`ExecStart` 逐段钉住：`current/src` 的 venv gunicorn + `apps.callback.wsgi` + `127.0.0.1:8002`。

        失败方向：绑成 `0.0.0.0`（把没有 nginx 兜底的端口暴露到公网）、抢 `8001`（那是主服务）、
        写成 Django 的 `config.wsgi`（那会走进按 Host 认租户的中间件）⇒ 本用例红。
        """
        import shlex

        text = _read(self._UNIT)
        self.assertIn("ExecStart=", text)
        exec_line = next(
            line for line in _logical_lines(text) if line.startswith("ExecStart=")
        ).split("=", 1)[1]
        tokens = shlex.split(exec_line)
        self.assertEqual(tokens[0], "/home/ubuntu/projects/tuantuantong/current/src/.venv/bin/gunicorn")
        self.assertEqual(tokens[1], "apps.callback.wsgi:application")
        self.assertEqual(tokens[tokens.index("--bind") + 1], "127.0.0.1:8002")
        self.assertEqual(tokens[tokens.index("--workers") + 1], "2")
        self.assertNotIn("8001", exec_line)

    def test_the_access_log_format_doubles_every_percent(self):
        """⛔ systemd 的 `ExecStart=` 会展开规格符 ⇒ 格式串里的 `%` 必须写成 `%%`。

        写成单 `%` ⇒ 单元**加载**时报 `Failed to resolve unit specifiers`（接收器起不来，
        日志里只有一句天书）；判据 = 把 `%%` 全部消掉之后**一个 `%` 都不许剩**。
        """
        exec_line = next(
            line for line in _logical_lines(_read(self._UNIT)) if line.startswith("ExecStart=")
        )
        self.assertIn("%%(h)s", exec_line, "格式串里没有双写的占位符 ⇒ 本用例的自证失败")
        self.assertNotIn("%", exec_line.replace("%%", ""), "格式串里有未双写的 `%`（systemd 会当规格符展开）")

    def test_working_directory_restart_and_no_timer(self):
        text = _read(self._UNIT)
        self.assertIn("WorkingDirectory=/home/ubuntu/projects/tuantuantong/current/src", text)
        self.assertIn("\nUser=ubuntu\n", text)
        self.assertIn("\nGroup=ubuntu\n", text)
        self.assertIn("\nRestart=always\n", text)
        self.assertIn("\nKillSignal=SIGTERM\n", text)
        self.assertIn("\nStartLimitIntervalSec=0\n", text)
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertNotIn("OnCalendar=", text)
        self.assertNotIn("WantedBy=timers.target", text)

    def test_the_unit_never_carries_the_secret_in_its_environment(self):
        """⛔ 单元里不许出现密钥变量（`Environment=` 会把值落进 `systemctl show` 与 `/proc/<pid>/environ`）。"""
        text = _read(self._UNIT)
        for line in text.splitlines():
            if line.startswith("Environment="):
                self.assertNotIn("SECRET", line.upper())

    def test_the_load_bearing_constraints_are_written_in_the_unit_comments(self):
        """承重约束必须写进单元注释（装机的人第一眼就看到）。

        ⚠️ **弱判据**（断的是文字）：只保证"这几句话还在"，不保证运维照做。
        """
        text = _read(self._UNIT)
        for phrase in (
            "CALLBACK_UNIT_ENABLE",
            "默认 **0 = 不启用**",
            "不要**把它塞进 `SYNC_SERVICES`",
            "8002",
            "不许抢 8001",
            "callback.env",
            "绝不进",
        ):
            self.assertIn(phrase, text, f"单元注释里缺少承重约束：{phrase}")


class DeployWiringTests(SimpleTestCase):
    """`deploy.sh` 两处 / `activate.sh` / `rollback.sh` 三份清单的接线。"""

    _UNIT_NAME = "tuantuantong-callback"

    def test_deploy_dot_sh_has_it_in_both_faces(self):
        """`deploy.sh` 的 **rsync 面**与 **chmod/md5sum 面**都要有它（漏一处 = 半新半旧）。"""
        lines = _logical_lines(_read(DEPLOY / "deploy.sh"))
        rsync_lines = [line for line in lines if "rsync" in line and "deploy/systemd/" in line]
        self.assertTrue(rsync_lines, "deploy.sh 里找不到带 deploy/systemd/ 的 rsync 行")
        self.assertIn(
            f"deploy/systemd/{self._UNIT_NAME}.service",
            "\n".join(rsync_lines),
            "deploy.sh 的 rsync 源文件清单里缺回调接收器单元",
        )
        chmod_lines = [
            line
            for line in lines
            if line.lstrip().startswith("ssh ") and "chmod 644" in line and "md5sum" in line
        ]
        self.assertEqual(len(chmod_lines), 1, "deploy.sh 里那条 chmod/md5sum 命令应当恰好一条")
        chmod_face, md5sum_face = chmod_lines[0].split("; md5sum ", 1)
        for face_name, face in (("chmod 644", chmod_face), ("md5sum", md5sum_face)):
            self.assertIn(
                f"$R/shared/{self._UNIT_NAME}.service",
                face,
                f"deploy.sh 的 {face_name} 面里缺回调接收器单元",
            )

    def test_the_unit_is_not_in_the_sync_services_list(self):
        """⛔ 它**不许**进 `SYNC_SERVICES`（那是常驻同步清单，混进去会被 `SYNC_UNIT_ENABLE=0` 的机器停掉）。"""
        for script in ("activate.sh", "rollback.sh"):
            assignments = [
                line
                for line in _logical_lines(rendered_sync_script(DEPLOY / script))
                if line.lstrip().startswith("SYNC_SERVICES=(")
            ]
            with self.subTest(script=script):
                self.assertEqual(len(assignments), 1)
                self.assertNotIn(self._UNIT_NAME, assignments[0])

    def test_both_scripts_read_the_callback_switch_from_the_same_file(self):
        """`activate.sh` 与 `rollback.sh` 都要认那个**逐台**开关，且读**同一个文件**。"""
        for script in ("activate.sh", "rollback.sh"):
            text = _read(DEPLOY / script)
            with self.subTest(script=script):
                self.assertIn("CALLBACK_UNIT_ENABLE", text)
                self.assertIn("$R/shared/callback-unit.env", text)
                self.assertIn(self._UNIT_NAME, text)

    def test_the_callback_switch_defaults_to_disabled(self):
        """默认值必须是 **0 = 不启用**（接收器是公网入口 + 要 0600 密钥 + 占端口）。

        失败方向：抄同步那个开关的默认值（`:-1`）⇒ 每台机器发版后都悄悄开出一个公网入口。
        """
        activate = _read(DEPLOY / "activate.sh")
        rollback = _read(DEPLOY / "rollback.sh")
        self.assertIn('CALLBACK_UNIT_ENABLE="${CALLBACK_UNIT_ENABLE:-0}"', activate)
        self.assertIn('CALLBACK_UNIT_ENABLE="${CALLBACK_UNIT_ENABLE:-0}"', rollback)

    def test_activate_resolves_the_switch_before_switching_current(self):
        """开关必须在切 `current` **之前**解析好（写错时软链不动，留出回滚余地）。

        判据打在**顺序**上：`CALLBACK_UNIT_ENABLE` 的解析行必须排在 `ln -sfn "$REL" "$R/current"` 之前。
        """
        lines = _read(DEPLOY / "activate.sh").splitlines()
        parse_index = next(
            index
            for index, line in enumerate(lines)
            if line.startswith('CALLBACK_UNIT_ENABLE="${CALLBACK_UNIT_ENABLE:-0}"')
        )
        switch_index = next(
            index for index, line in enumerate(lines) if line.strip() == 'ln -sfn "$REL" "$R/current"'
        )
        self.assertLess(parse_index, switch_index)

    def test_activate_restarts_the_unit_after_the_sync_section(self):
        """重启那一段必须排在同步单元那一段**之后**（⛔ 别让它失败时skip掉同步单元的重启）。"""
        text = _read(DEPLOY / "activate.sh")
        sync_loop = text.index('echo "✅ ${SYNC_SERVICE} 已安装并 active')
        callback_block = text.index('echo "✅ ${CALLBACK_UNIT} 已安装并 active')
        self.assertLess(sync_loop, callback_block)


class NginxTemplateTests(SimpleTestCase):
    """`deploy/nginx/callback.conf` 的接线（⚠️ **读码判据**：本机没有 nginx，⛔ 没在真机验过）。"""

    _CONF = DEPLOY / "nginx" / "callback.conf"

    def test_it_is_a_template_with_the_placeholder(self):
        text = _read(self._CONF)
        self.assertIn("__SERVER_NAME__", text)
        self.assertIn("listen 443 ssl http2;", text)  # nginx 1.24 只认这个老写法
        # ⛔ 独立指令 `http2 on;` 要 nginx ≥1.25.1 ⇒ 只许出现在**注释**里（讲为什么不用它）。
        directives = [
            line.strip() for line in text.splitlines() if not line.lstrip().startswith("#")
        ]
        self.assertNotIn("http2 on;", directives)

    def test_it_reuses_the_existing_wildcard_certificate(self):
        text = _read(self._CONF)
        self.assertIn("ssl_certificate     /etc/ssl/suqing/fullchain.pem;", text)
        self.assertIn("ssl_certificate_key /etc/ssl/suqing/privkey.pem;", text)

    def test_only_the_notify_path_is_proxied_and_everything_else_is_404(self):
        """⭐ 只有那**一个精确路径**转给接收器；其余一律 404（堵已存在的暴露面：装本块之前
        `callback.suqing.chat` 落到租户站点默认 server，可行工作室整套页面在该域名直接可达）。

        失败方向：写成前缀 `location /jidanpu/`（放通别的子路径）、兜底写成跳转 / `try_files`
        （把域名变成通往租户站点的路标）⇒ 本用例红。
        """
        text = _read(self._CONF)
        self.assertIn("location = /jidanpu/v1/notify {", text)
        self.assertIn("include proxy_params;", text)
        self.assertIn("proxy_pass http://127.0.0.1:8002;", text)
        # 代练通回调是**第二条精确路径**（→ 8003），必须与 8002 那条同一次装上。
        self.assertIn("location = /dailiantong/v1/notify {", text)
        self.assertIn("proxy_pass http://127.0.0.1:8003;", text)
        self.assertIn("location / { return 404; }", text)
        # ⛔ 不许出现租户站点那一套（静态根 / SPA 回退 / 转发到主服务）
        self.assertNotIn("try_files", text)
        self.assertNotIn("index.html", text)
        self.assertNotIn("proxy_pass http://127.0.0.1:8001", text)
        self.assertNotIn("location /api/", text)

    def test_the_body_limit_is_set_with_a_reason(self):
        """报文上限必须是**显式**给的，且理由写在旁边（不是拍脑袋、也不是 nginx 默认的 1m 巧合）。"""
        text = _read(self._CONF)
        self.assertIn("client_max_body_size 1m;", text)
        self.assertIn("取值理由", text)
        self.assertIn("MAX_BODY_BYTES", text)


class NginxTenantTemplateTests(SimpleTestCase):
    """⭐ 租户站点模板 `deploy/nginx/tuantuantong.conf` 上的**回调 location**（spec §5.1 / §5.2 表格第 1–2 行）。

    与 `callback.conf`（新装独立块，装错只影响 `callback.suqing.chat`）不同：本模板**每台租户机上
    正跑着**（`suqing.suqing.chat` / `kexing.suqing.chat`，见 `docs/RUNBOOK.md` §3.0.1）⇒ 最要紧的
    判据是**「既有 location 一条没被动」（顺序 + 正文逐字冻结）**，且必须与新 location **同一次**做完
    （`AGENTS.md` §5-A 同族：⛔ 别只加新的、把旧的碰松了）。
    ⚠️ 证据等级 = 读码：本机**没有 nginx** ⇒ 绿 **不许**读成"机器上验过了"；带引号的正则 /
    `{22}` 量词 / PCRE 差异只有真机能判（E-0200「存在但跑不了的网 = 假保证」同族），
    **第一次装到真机必须先跑 `nginx -t`**、绿了才 `systemctl reload nginx`（spec §13.2 B4 未验）。
    ⚠️ 判据打在**指令行**上、不打文件原文：本模板注释里本来就写了 8002/8001，原文判据会恒真/恒假。
    """

    _CONF = DEPLOY / "nginx" / "tuantuantong.conf"

    #: ⛔ **期望值是字面量**（不写 `wsgi.CALLBACK_CODE_LENGTH` / `wsgi.CALLBACK_PATH_PREFIX`，
    #: 那是自指断言，见 E-0024 / `AGENTS.md` §5-G）。⭐ 正则**整条加双引号**、那对引号**承重**：
    #: nginx 把没加引号的 `{` 当配置块开始符 ⇒ 真机报 `[emerg] unknown directive "22}$"`、
    #: 整份配置 load 不起来（站点全挂）。出处（官方文档）：
    #: https://nginx.org/en/docs/http/server_names.html “Regular expressions names”
    #: —— “A regular expression containing the characters “{” and “}” should be quoted:”。
    _LOCATION_LINE = 'location ~ "^/api/jidanpu/callback/[A-Za-z0-9_-]{22}$" {'

    #: ⭐ bind 口精确 location（独立常量：冻结表与覆盖面自证都要按它排除，与回调那条同款待遇）。
    _BIND_LOCATION_LINE = "location = /api/business/peer-sync/bind/ {"
    _WXPUSHER_LOGIN_LINE = "location = /api/auth/wxpusher/login {"

    #: 上面那行的**两侧**（把匹配串夹在中间）：⛔ 判据靠它们从**模板文本**里切出匹配串
    #: （⛔ 不是从 `_LOCATION_LINE` 自己切 —— 那是自指断言，去掉模板里的引号不会红，实测过）。
    #: ⚠️ `_PATTERN_HEAD` **带尾空格**：去掉尾空格会把正则的修饰符 `~` 也捕进匹配串。
    _PATTERN_HEAD = "location ~ "
    _PATTERN_TAIL = " {"

    #: 那份模板里**排好序**的 location 起始行（⛔ 改这里 = 改"既有那几条没被动"这条判据本身）。
    #: ⚠️ `location = /api`（404）排在 `/api/` **之后**、`location /`（SPA 回退）**最后**，与文件原样一致；
    #: `location /` 是**前缀**匹配（不参与正则那一轮）。
    _EXPECTED_LOCATION_LINES = [
        # ① 80 那个 server 块（只跳转 + ACME）
        "location /.well-known/acme-challenge/ { root /var/www/certbot; }",
        _WXPUSHER_LOGIN_LINE,
        "location / { return 301 https://$host$request_uri; }",
        # ② 443 这个 server 块（顺序 = 文件里的顺序）
        "location /static/ {",
        'location ~ "^/api/jidanpu/callback/[A-Za-z0-9_-]{22}$" {',  # 回调线新增（正则带引号）
        _BIND_LOCATION_LINE,  # bind 口 64k（免认证解析面）
        _WXPUSHER_LOGIN_LINE,
        "location /api/      { include proxy_params; proxy_pass http://127.0.0.1:8001; }",
        "location = /api { return 404; }",
        "location = /healthz { include proxy_params; proxy_pass http://127.0.0.1:8001; }",
        "location /m/ {",  # 打手手机端深链
        "location /          { try_files $uri $uri/ /index.html; }",
    ]

    #: 既有 7 条 location 的**正文逐字冻结**（⚠️ 注释不算：注释是解释、改注释不该红；
    #: 这些是**指令**，改了就是改行为）。键 = location 起始行（与上面那份顺序表同一套字面量）。
    #: ⚠️ 归一化只有「去掉空行 + 每行 strip」这一种，且**两侧同一套**（E-0083）。
    _FROZEN_EXISTING_BODIES = {
        "location /.well-known/acme-challenge/ { root /var/www/certbot; }":
            "location /.well-known/acme-challenge/ { root /var/www/certbot; }",
        "location / { return 301 https://$host$request_uri; }":
            "location / { return 301 https://$host$request_uri; }",
        "location /static/ {":
            "location /static/ {\n"
            "alias /home/ubuntu/projects/tuantuantong/current/src/staticfiles/;\n"
            "expires 7d;\n"
            "}",
        # ⛔ 这两条是**线上正在服务**的：`/api/` 转主服务、裸 `/api` 回 404（别让它掉进 SPA 回退）。
        "location /api/      { include proxy_params; proxy_pass http://127.0.0.1:8001; }":
            "location /api/      { include proxy_params; proxy_pass http://127.0.0.1:8001; }",
        "location = /api { return 404; }":
            "location = /api { return 404; }",
        "location = /healthz { include proxy_params; proxy_pass http://127.0.0.1:8001; }":
            "location = /healthz { include proxy_params; proxy_pass http://127.0.0.1:8001; }",
        # ⛔ 打手手机端深链：`/m/` 目录直出 + 深链回落手机端 index.html。
        "location /m/ {":
            "location /m/ {\n"
            "try_files $uri $uri/ /m/index.html;\n"
            "}",
        # SPA 回退：⛔ 别动（它同时是"裸 /api 之外的未知路径"的兜底）。
        "location /          { try_files $uri $uri/ /index.html; }":
            "location /          { try_files $uri $uri/ /index.html; }",
        # ⭐ bind 口 64k + 转主服务 8001（免认证口不许吃 20m 解析额度）。
        _BIND_LOCATION_LINE:
            "location = /api/business/peer-sync/bind/ {\n"
            "client_max_body_size 64k;\n"
            "include proxy_params;\n"
            "proxy_pass http://127.0.0.1:8001;\n"
            "}",
    }

    # ── 解析用具 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _location_start_line(line: str) -> str:
        """一条 location 的**起始行**（去掉行尾注释；`location` 一定写在单行开头）。

        ⚠️ 先按 `_logical_lines` 拼续行：否则"location 那行有没有正文"会被续行看错。
        """
        stripped = line.strip()
        return stripped.split("#", 1)[0].rstrip()

    def _location_blocks(self) -> list[tuple[str, str]]:
        """按**文件顺序**返回 `[(起始行, 正文归一化文本), …]`（正文用大括号配对切出来）。

        ⛔ 不按缩进猜块尾（`location /static/` 是多行块）⇒ **数大括号**，配对不上 `self.fail`；
        单行块正文 = 起始行本身、多行块正文含末尾 `}`，两种形状都如实返回
        （⛔ 不各自归一化，两侧同一套，E-0083）。归一化只有一种：**去掉注释、去掉空行、每行 strip**。
        """
        blocks: list[tuple[str, str]] = []
        pending: str | None = None
        depth = 0
        body: list[str] = []
        for raw in _logical_lines(_read(self._CONF)):
            code = raw.split("#", 1)[0].strip()  # ⛔ 注释不参与数大括号
            if pending is None:
                start = self._location_start_line(raw)
                if not start.startswith("location "):
                    continue
                brace = start.index("{")  # 起始行里必有（本模板的既有写法）
                tail = start[brace + 1:]
                pending, depth, body = start, 1 + tail.count("{") - tail.count("}"), [start]
                if depth == 0:  # 单行块：`location = /api { return 404; }`
                    blocks.append((pending, start))
                    pending = None
                continue
            depth += code.count("{") - code.count("}")
            body.append(code)
            if depth == 0:
                blocks.append((pending, "\n".join(part for part in body if part)))
                pending = None
        if pending is not None:
            self.fail(f"最后一条 location 的大括号没配对：{pending!r}")
        return blocks

    # ── ① 新那条 location 的形状 ─────────────────────────────────────────────

    def test_the_callback_location_regex_is_in_the_template(self):
        """⭐ **B1**：模板里有那条正则 location（定长 22、字符组逐字）。

        失败方向：删掉它（spec §13.2 B1 的"弄红它的办法"）；`{22}` 放宽成 `+`/`*`（= 前缀全放通，
        公网可达的端点白送一个免签名写盘口）；去掉 `$`；换成前缀 location ⇒ 都拼不出这一行
        （本断言钉**那一行本身**，含那对双引号）。
        ⚠️ "引号在不在"另有专门用例 `test_the_callback_regex_is_quoted`（本用例期望值就是那张
        字面量，⛔ 不是从模板解析）。
        """
        text = _read(self._CONF)
        self.assertIn(self._LOCATION_LINE, text)
        # ⛔ 反向：不许同时留一条"前缀式"的宽 location（两条并存时，正则那条会被前缀那条**悄悄架空**：
        #    `…/callback/<码>/x` 会走前缀那条）。⚠️ 打在**指令行**上：注释里就有 `location /api/` 这些字。
        self.assertEqual(
            [line for line in self._directives() if line.startswith("location ~ ")],
            [self._LOCATION_LINE],
            "正则 location 应当**恰好**这一条（多一条 = 有人又开了个口子）",
        )
        for line in self._directives():
            if not line.startswith("location "):
                continue
            # 这条 location 的**匹配串**不许是"不带定长约束的前缀 / 精确匹配"
            if line.split("{", 1)[0].rstrip().endswith(
                ("/api/jidanpu/callback/", "/api/jidanpu/callback", "/api/jidanpu/")
            ):
                self.fail(f"不许出现前缀式的宽 location（会架空那条定长正则）：{line!r}")

    def test_the_callback_regex_is_quoted(self):
        """⭐⭐ **B1b**：那条 location 的**正则整条被引号包起来**（真机实测缺陷；出处见 `_LOCATION_LINE` 注释）。

        真机现场：模板装到租户站点后 `sudo nginx -t` 报
        `[emerg] unknown directive "22}$" in /etc/nginx/sites-enabled/tuantuantong:57`
        —— 报的是残片，从报错文本反推不到"少了一对引号" ⇒ 判据必须钉在源码里。
        ⚠️ 本机没有 nginx ⇒ 绿只证明"形状对"，⛔ 不证明"nginx 收得下"（E-0200「存在但跑不了的网
        = 假保证」同族）：**第一次装到真机必须先跑 `nginx -t`**，绿了才 `systemctl reload nginx`。
        ⛔ 判据必须打在**模板那一行**上（从模板文本取行、取两个固定端点之间的整段），⛔ 不打在
        `_LOCATION_LINE` 字面量上 —— 截字面量看引号是自指断言（E-0024 同族）：模板里去掉引号它照样绿（实测）。

        失败方向：把模板那行改回不带引号的 `location ~ ^/api/jidanpu/callback/[A-Za-z0-9_-]{22}$ {` ⇒ 红。
        """
        text = _read(self._CONF)
        # ① 先自证"模板里确实有这条 location"，⛔ 否则下一条 `assertEqual` 会因为两边都空而**恒真**。
        self.assertIn(
            self._PATTERN_HEAD, text, f"模板里没有正则 location：{self._PATTERN_HEAD!r}"
        )
        lines = [line for line in self._directives() if line.startswith(self._PATTERN_HEAD)]
        self.assertEqual(
            lines, [self._LOCATION_LINE], "正则 location 应当**恰好**这一行（与 B1 同一张字面量）"
        )
        line = lines[0]
        self.assertTrue(
            line.endswith(self._PATTERN_TAIL),
            f"这条 location 的形状变了（结尾不再是我认得的端点）：{line!r}",
        )
        quoted = line[len(self._PATTERN_HEAD):-len(self._PATTERN_TAIL)]
        self.assertTrue(
            len(quoted) >= 2 and quoted[0] == quoted[-1] and quoted[0] in {'"', "'"},
            "那条 location 的正则**必须整条被引号包起来**"
            "（`{22}` 里的 `{` 不加引号会被 nginx 当成配置块开始符 ⇒ "
            f'真机报 unknown directive "22}}$"）：{quoted!r}',
        )
        self.assertIn("{", quoted, "这条判据的前提是正则里**确实**有 `{`（拿掉量词就该重议这条）")

    def test_the_template_still_has_the_placeholder_convention(self):
        """`__SERVER_NAME__` 占位符照旧 —— 打在 `server_name` **指令行**上（⛔ 不数全文字符串：
        注释里也写到它 ⇒ 那种判据恒真）。模板是**手工装**的（⛔ 不随 `deploy.sh` 上传）
        ⇒ 占位符是"每台机器替换成自己的域名"这条流程的抓手。
        """
        directives = [
            raw.split("#", 1)[0].strip() for raw in _logical_lines(_read(self._CONF))
        ]
        self.assertEqual(
            [line for line in directives if line.startswith("server_name")],
            ["server_name __SERVER_NAME__;", "server_name __SERVER_NAME__;"],  # 80 与 443 各一个
        )
        self.assertIn("listen 443 ssl http2;", _read(self._CONF))  # nginx 1.24 只认这个老写法

    def test_the_callback_block_has_exactly_three_directives(self):
        """那条 location 的**实质就三行**：正则 / `include proxy_params;` / `proxy_pass …8002;`。

        ⚠️ 逐行全等（`assertIn("8002", text)` 恒真——注释里也有 8002，删掉 `proxy_pass` 都不会红）；
        同时挡住"手写一份 `proxy_set_header`"的漂移（漏了覆盖写就变成可伪造）。
        """
        block = self._block_of(self._LOCATION_LINE)
        lines = [line for line in block.splitlines() if line.strip()]
        self.assertEqual(
            lines,
            [
                self._LOCATION_LINE,
                "include proxy_params;",
                "proxy_pass http://127.0.0.1:8002;",
                "}",
            ],
        )
        # ⛔ 正文里不能出现 `client_max_body_size`：本线**不动**报文上限（spec §5.2 第 3 行），
        #    要动必须同一次改 nginx 与 `apps/callback/wsgi.py` 的 `MAX_BODY_BYTES`。
        self.assertNotIn("client_max_body_size", block)
        # ⛔ 也**不许**手写 `proxy_set_header`（那会与 `proxy_params` 漂移；来源 IP 靠它的覆盖写）。
        self.assertNotIn("proxy_set_header", block)

    def _directives(self) -> list[str]:
        """模板里的**指令行**（按文件顺序）：整行注释与行尾注释都去掉、空行去掉。

        ⭐ 所有"内容"判据都打在这里、⛔ 不打文件原文：本模板注释里本来就写了
        `location /api/` / `8001` / `8002` ⇒ 原文上的 `assertNotIn` 恒假、`assertIn` 恒真
        （E-0063 同族：说假话的判据比没覆盖更贵）。
        """
        lines: list[str] = []
        for raw in _logical_lines(_read(self._CONF)):
            code = raw.split("#", 1)[0].strip()
            if code:
                lines.append(code)
        return lines

    def _block_of(self, start_line: str) -> str:
        for start, body in self._location_blocks():
            if start == start_line:
                return body
        self.fail(f"模板里没有这条 location：{start_line!r}")

    # ── ② 顺序（B2）与"既有那几条没被动"（最要紧的回归判据）────────────────

    def test_the_callback_location_is_listed_before_the_api_prefix(self):
        """⭐ **B2**：它排在 `location /api/` **之前**（spec §5.2 表格第 2 行明文要求）。

        ⚠️ 顺序不是分流正确性的承重件（nginx 按类型分轮：正则那条在不在前面都会被选中），
        但它是 spec 定的、也防"下一个人误以为长的优先" ⇒ 钉住；挪到后面就该红。
        """
        text = _read(self._CONF)
        self.assertLess(
            text.index(self._LOCATION_LINE),
            text.index("location /api/      {"),
            "回调那条 location 必须排在 `location /api/` 之前",
        )

    def test_existing_locations_are_untouched_in_order_and_content(self):
        """⭐⭐ **B2 的后半**：既有那些 location **一条都没被动**（顺序 + 正文逐字）。

        最要紧的回归判据：模板在 `suqing.suqing.chat` / `kexing.suqing.chat` 上**正跑着**，
        碰松一条（尤其 `/api/` 的 8001、裸 `/api` 的 404、SPA 回退那三条）就是线上事故。
        两半各自能红：**顺序**（`location /` 提到 `/api/` 前面）+ **正文**（8001 改成 8002、
        `location = /api` 的 404 改成 `return 301`）。⚠️ 期望值全是**字面量**（自指断言改了不会红）。
        """
        blocks = self._location_blocks()
        self.assertEqual(
            [start for start, _ in blocks],
            self._EXPECTED_LOCATION_LINES,
            "location 的顺序 / 条数变了（既有那几条**一条都不许动**）",
        )
        wrong: list[str] = []
        for start, body in blocks:
            frozen = self._FROZEN_EXISTING_BODIES.get(start)
            if frozen is None:
                continue  # 回调与 WxPusher 的入口正文各有专项用例
            if body != frozen:
                wrong.append(f"--- {start}\n实际：{body!r}\n期望：{frozen!r}")
        self.assertEqual(wrong, [], "既有 location 的**正文**被改了：\n" + "\n".join(wrong))

    def test_the_existing_body_freeze_covers_every_existing_location(self):
        """⭐ 上一条用例的**覆盖面自证**：冻结表**恰好**覆盖"顺序表里除新增那条以外"的全部。

        ⚠️ 没有这一条，上一条就会有一个静默缺口：将来有人加一条 location、又忘了往
        `_FROZEN_EXISTING_BODIES` 里登记 ⇒ 那条**没人守**，而读数还是绿的（E-0065 同族）。
        """
        self.assertEqual(
            sorted(self._FROZEN_EXISTING_BODIES),
            # 回调入口由本类专测；WxPusher 的两个同路径块由访问日志测试区分 HTTP/HTTPS。
            sorted(set(self._EXPECTED_LOCATION_LINES) - {self._LOCATION_LINE, self._WXPUSHER_LOGIN_LINE}),
        )
        self.assertEqual(len(self._EXPECTED_LOCATION_LINES), 12)

    # ── ③ 没有把别的东西接到接收器上 ───────────────────────────────────────────

    def test_nothing_else_was_rewired_to_the_receiver(self):
        """⭐ 只放行**那一个形状**：转发到 8002 的 `proxy_pass` 全模板**恰好 1 条**。

        ⚠️ 判据打在**行**上（"8002 出现过几次"恒真——注释里也写了 8002，改指 8002 都不会红）。
        ⚠️ `include proxy_params;` 期望条数 **5** 是**字面量**且**双向**成立：少一条 = 既有
        location 的 proxy_params 被删（来源 IP / X-Forwarded-* 全丢）；多一条 = 又开了新转发口。
        ⚠️ `/api/` 与 `= /healthz` 把 `include`/`proxy_pass` 写在**同一行** ⇒ 按行取子串，
        ⛔ 不是简单"以 proxy_pass 开头"。
        """
        directives = self._directives()
        self.assertEqual(
            sorted(
                line for line in directives if line.startswith("proxy_pass")
            ),
            sorted([
                "proxy_pass http://127.0.0.1:8002;",  # ⭐ 回调接收器，写在单独一行
                # ⭐ bind 口，同样写在单独一行 —— ⛔ 指主服务 8001、⛔ 不是 8002
                #    （断言意图 = "没有别的东西被接去接收器"）。
                "proxy_pass http://127.0.0.1:8001;",
                "proxy_pass http://127.0.0.1:8001;",  # WxPusher 签名入口仍转主服务。
            ]),
        )
        # 与既有那两条**同一行**的 `proxy_pass`（原样、指主服务 8001）：⛔ 别把任何一条改成 8002。
        self.assertEqual(
            sorted(
                line for line in directives
                if "proxy_pass" in line and not line.startswith("proxy_pass")
            ),
            sorted(
                [
                    "location /api/      { include proxy_params; proxy_pass http://127.0.0.1:8001; }",
                    "location = /healthz { include proxy_params; proxy_pass http://127.0.0.1:8001; }",
                ]
            ),
        )
        self.assertEqual(directives.count("include proxy_params;"), 3)  # 回调、bind 和 WxPusher 各独占一行
        self.assertEqual(sum("include proxy_params;" in line for line in directives), 5)
        # ⛔ 接收器**不许**出现在其它位置（尤其 `location /` 的 SPA 回退与 `/static/`）：
        #    转给 8002 的**行**恰好 1 条（⛔ 不是"8002 出现过几次"——注释里也有）。
        self.assertEqual(sum(line.endswith(":8002;") for line in directives), 1)


class ConfigPathTests(SimpleTestCase):
    """端口 / 路径的最终取值（本线交付口径，⛔ 改它必须同一次改文档）。"""

    def test_the_events_path_is_derived_from_the_root(self):
        with mock.patch.dict(os.environ, {"CALLBACK_ROOT": "/tmp/tt-root"}, clear=False):
            os.environ.pop("CALLBACK_EVENTS_PATH", None)
            os.environ.pop("CALLBACK_ENV_FILE", None)
            os.environ.pop("CALLBACK_KEYS_FILE", None)
            self.assertEqual(
                str(config.events_path()), "/tmp/tt-root/shared/callback/events.jsonl"
            )
            self.assertEqual(str(config.env_file_path()), "/tmp/tt-root/shared/callback.env")
            # ⭐ 新路径的「码 → 密钥」文件：**另一份**文件、同一个 `$R` 底下（spec §5.3）。
            self.assertEqual(
                str(config.callback_keys_path()), "/tmp/tt-root/shared/callback-keys.env"
            )

    def test_the_default_root_is_the_production_deployment_root(self):
        """默认值 = 生产形态（`deploy/*.sh` 里的 `R`）⇒ ⛔ 别改成别的路径。"""
        self.assertEqual(config.callback_root().as_posix(), config.DEFAULT_ROOT)
        self.assertEqual(
            config.DEFAULT_ROOT, "/home/ubuntu/projects/tuantuantong"
        )

    def test_the_secret_is_read_only_from_the_file(self):
        """⛔ 不从进程环境读密钥（`Environment=` 会把值落进 `systemctl show`）。

        判据 = `config.py` 里 `os.environ.get(...)` 找的**恰好是**那四个**路径**变量
        （期望值是**字面量**，⛔ 不写 `config.ENV_*`：自指断言改了常量名两边一起变、不会红）；
        多出第五个（尤其是指向密钥的）就该红。守的始终是同一条：**读密钥只认文件、不认环境**。
        """
        import ast

        path = SRC_DIR / "apps" / "callback" / "config.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        looked_up: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "environ"
                and node.args
            ):
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    looked_up.add(first.value)
                elif isinstance(first, ast.Name):
                    looked_up.add(str(getattr(config, first.id, first.id)))
        self.assertEqual(
            looked_up,
            {
                "CALLBACK_ROOT",
                "CALLBACK_EVENTS_PATH",
                "CALLBACK_ENV_FILE",
                "CALLBACK_KEYS_FILE",
            },
            f"config.py 里 os.environ.get 找的变量集合不对（多出来一个就可能是在读密钥）：{looked_up}",
        )
        # ⛔ 四个都是**路径**变量：名字里不许出现"密钥"那一族的词（防"顺手加个读密钥的开关"）。
        for name in looked_up:
            self.assertNotIn("SECRET", name.upper(), f"{name} 看起来是在从环境读密钥")
        self.assertNotIn(config.SECRET_VAR_NAME, looked_up)
        self.assertNotIn(f'os.environ["{config.SECRET_VAR_NAME}"]', source)
        self.assertNotIn(f"os.environ.get({config.SECRET_VAR_NAME}", source)


# ═══════════════════════════════════════════════════════════════════════════
# ⑧ 新路径：`/api/jidanpu/callback/<22 位码>` + 「码 → 密钥」文件
#    （spec `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §5.1/§5.3/§5.4；
#     判据 = §13.1 的 A1–A4 / A6–A8）
#    ⚠️ 铁律：**旧路径一个字都不许变**（今天正在收鸡蛋铺的真实回调）⇒ ⑧ 只加新行为、
#    ⑨ 把旧行为**逐条冻结**；两条一起看才说明"是增量"。
# ═══════════════════════════════════════════════════════════════════════════


class CallbackCodeFromPathTests(SimpleTestCase):
    """从路径里取码（`wsgi.callback_code_of_path`，spec §5.4 第 1 条）—— 纯函数那一半。"""

    def test_the_literal_shapes_are_pinned(self):
        """两条路的**字面量**形状（⛔ 改它们必须同一次改 spec / nginx 模板 / 消费者）。"""
        self.assertEqual(wsgi.NOTIFY_PATH, "/jidanpu/v1/notify")
        self.assertEqual(wsgi.CALLBACK_PATH_PREFIX, "/api/jidanpu/callback/")
        self.assertEqual(wsgi.CALLBACK_CODE_LENGTH, 22)

    def test_a_well_formed_path_yields_the_code(self):
        self.assertEqual(len(CODE_A), 22, f"自证失败：夹具码不是 22 位：{CODE_A!r}")
        self.assertEqual(wsgi.callback_code_of_path(_callback_path(CODE_A)), CODE_A)
        self.assertEqual(wsgi.callback_code_of_path(_callback_path(CODE_ABSENT)), CODE_ABSENT)

    def test_bad_shapes_yield_none(self):
        """**A2 的纯函数那一半**：21 / 23 位、带 `/`、带尾斜杠、带子路径 ⇒ `None`。

        弄红它：把定长 `{22}` 放宽成 `+` / `*`（或把 `fullmatch` 写成 `match`）⇒ 本用例红。
        """
        bad = {
            "21 位": _callback_path(CODE_A[:-1]),
            "23 位": _callback_path(CODE_A + "x"),
            "尾斜杠": _callback_path(CODE_A) + "/",
            "子路径": _callback_path(CODE_A) + "/extra",
            "22 位里混进 /": _callback_path(CODE_A[:10] + "/" + CODE_A[11:]),
            "22 位里混进 +": _callback_path(CODE_A[:5] + "+" + CODE_A[6:]),
            "22 位里混进 =": _callback_path(CODE_A[:21] + "="),
            "光一个前缀": wsgi.CALLBACK_PATH_PREFIX,
            "少一个斜杠的前缀": wsgi.CALLBACK_PATH_PREFIX.rstrip("/"),
            "旧路径": wsgi.NOTIFY_PATH,
            "别的路径": "/api/business/orders/",
        }
        for label, path in bad.items():
            with self.subTest(label=label):
                self.assertIsNone(wsgi.callback_code_of_path(path), path)


class NewPathPerCodeSecretTests(_ReceiverCase):
    """新路径的端点行为：**A1–A4**（spec §13.1）。"""

    def test_a1_valid_code_with_a_good_sign_returns_200_and_records_that_path(self):
        """**A1**：合法码 + 合法签名 ⇒ 200 + 落盘，`path` **逐字**是它（含那串码）。

        弄红它：把路径解析写错（少一个 `/`、前缀写歪）⇒ 这条会以 404 红。
        """
        body = _signed_payload(F1_PARAMS, SECRET_CODE_A)
        status, payload, _ = _call(_environ(body, path=_callback_path(CODE_A)))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload, {})
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(record["sign_ok"])
        self.assertEqual(record["sign_reason"], signature.REASON_OK)
        # ⚠️ 字面量（⛔ 不用 `wsgi.CALLBACK_PATH_PREFIX` 拼期望值：那是自指断言）
        self.assertEqual(record["path"], f"/api/jidanpu/callback/{CODE_A}")
        self.assertEqual(record["body_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(record["record_version"], RECORD_VERSION_LITERAL)

    def test_a2_bad_code_shapes_are_404_and_not_recorded(self):
        """**A2**：码形状不合法 ⇒ **404 且不落盘**（⛔ 不是 200、⛔ 不是 401、⛔ 不是 500）。

        弄红它：把定长 `{22}` 放宽成 `+` / `*` ⇒ 那批 404 变成 401/200（本用例红）。
        """
        body = _signed_payload(F1_PARAMS, SECRET_CODE_A)
        bad_paths = {
            "21 位": _callback_path(CODE_A[:-1]),
            "23 位": _callback_path(CODE_A + "x"),
            "带尾斜杠": _callback_path(CODE_A) + "/",
            "带子路径": _callback_path(CODE_A) + "/extra",
            "码里带斜杠": _callback_path(CODE_A[:10] + "/" + CODE_A[11:]),
            "光一个前缀": wsgi.CALLBACK_PATH_PREFIX,
        }
        for label, path in bad_paths.items():
            with self.subTest(label=label):
                status, payload, _ = _call(_environ(body, path=path))
                self.assertEqual(status, "404 Not Found")
                self.assertEqual(payload, {"error": "not_found"})
        self.assertEqual(self.record_count(), 0, "形状不对的码竟然落了盘")
        # ⭐ 自证：**同一个码**、形状对了 ⇒ 200（否则上面那批 404 可能只是"这个码根本没人认"）
        status, _, _ = _call(_environ(body, path=_callback_path(CODE_A)))
        self.assertEqual(status, "200 OK")
        self.assertEqual(self.record_count(), 1)

    def test_a3_unknown_code_is_401_and_records_the_no_binding_reason(self):
        """**A3**：码**不在密钥文件里** ⇒ **401** + 记录 + 「没有这个绑定」那一档 `sign_reason`
        （⛔ 不是 200 假成功、⛔ 不是 503"我们坏了"，两档必须分得开）。
        弄红它：把"找不到码"与"读不到文件"合成一个分支 ⇒ 本用例与 A4 那条至少红一条。
        """
        status, payload, _ = _call(
            _environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_ABSENT))
        )
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload, {"error": "sign_invalid"})
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertFalse(record["sign_ok"])
        self.assertEqual(record["sign_reason"], "unknown_callback_code")   # 字面量
        self.assertNotEqual(record["sign_reason"], signature.REASON_SECRET_UNAVAILABLE)
        self.assertEqual(record["path"], f"/api/jidanpu/callback/{CODE_ABSENT}")
        # ⭐ 自证：同一个报文打到**文件里有的**那个码上 ⇒ 200（否则这条 401 可能来自别的原因）
        status2, _, _ = _call(
            _environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_A))
        )
        self.assertEqual(status2, "200 OK")

    def test_a_code_line_without_a_secret_is_a_loud_failure_not_a_200(self):
        """⭐ 码**在文件里**、但那一行没有密钥 ⇒ 明确失败（⛔ 不是 200，⛔ 也不是"跳过验签"）。

        归 401 档的理由：从第三方看，这条回调归不到任何能验签的绑定上；503 留给文件本身读不到（A4）。
        """
        self.keys_file.write_text(f"{KEYS_FILE_HEADER}\n{CODE_A}\t\n", encoding="utf-8")
        status, _, _ = _call(
            _environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_A))
        )
        self.assertEqual(status, "401 Unauthorized")
        self.assertNotEqual(status, "503 Service Unavailable")
        record = self.last_record()
        self.assertFalse(record["sign_ok"])
        self.assertEqual(record["sign_reason"], "unknown_callback_code")

    def test_a4_unreadable_keys_file_is_503_and_distinguishable_from_an_unknown_code(self):
        """**A4**：**文件本身**读不到 ⇒ **503** + `secret_unavailable`；与 A3 的 401 分得开。

        弄红它：把两个异常合成一个（或让一个继承另一个）⇒ 两档的区别消失、本用例红。
        """
        os.environ["CALLBACK_KEYS_FILE"] = str(self.tmp / "shared" / "nope-keys.env")
        status, payload, _ = _call(
            _environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_A))
        )
        self.assertEqual(status, "503 Service Unavailable")
        self.assertEqual(payload, {"error": "verifier_unavailable"})
        missing_file_record = self.last_record()
        self.assertFalse(missing_file_record["sign_ok"])
        self.assertEqual(missing_file_record["sign_reason"], "secret_unavailable")

        # ⭐ 对照：文件放回去、换一个**文件里没有**的码 ⇒ 401（两档确实分得开）
        os.environ["CALLBACK_KEYS_FILE"] = str(self.keys_file)
        status2, _, _ = _call(
            _environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_ABSENT))
        )
        self.assertEqual(status2, "401 Unauthorized")
        self.assertNotEqual(
            missing_file_record["sign_reason"],
            self.last_record()["sign_reason"],
            "「文件读不到」与「码不认识」用了同一个 sign_reason ⇒ 两档被合并了",
        )

    def test_bad_sign_on_the_new_path_is_401_sign_mismatch(self):
        """新路径的**坏签名** ⇒ 401 `sign_mismatch`（⛔ 不许"码对了就放行"）。"""
        body = json.dumps({**F1_PARAMS, "sign": "0" * 32}, ensure_ascii=False).encode("utf-8")
        status, payload, _ = _call(_environ(body, path=_callback_path(CODE_A)))
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(payload, {"error": "sign_invalid"})
        record = self.last_record()
        self.assertFalse(record["sign_ok"])
        self.assertEqual(record["sign_reason"], signature.REASON_SIGN_MISMATCH)

    def test_missing_sign_on_the_new_path_is_401_sign_missing(self):
        body = json.dumps(F1_PARAMS, ensure_ascii=False).encode("utf-8")
        status, _, _ = _call(_environ(body, path=_callback_path(CODE_A)))
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(self.last_record()["sign_reason"], signature.REASON_SIGN_MISSING)

    def test_non_json_body_on_the_new_path_is_400_and_no_secret_is_needed(self):
        """新路径的报文不是 JSON 对象 ⇒ 400 + 记录（与旧路径同一口径、同一优先级）。"""
        status, payload, _ = _call(_environ(b"not json at all", path=_callback_path(CODE_ABSENT)))
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(payload, {"error": "body_not_json"})
        self.assertEqual(self.last_record()["sign_reason"], "body_not_json")

    def test_get_on_the_new_path_is_405_and_not_recorded(self):
        """⛔ 非 POST 不落盘（两条路同一口径：公网可达的端点不送写盘放大口）。"""
        status, payload, _ = _call(_environ(b"", method="GET", path=_callback_path(CODE_A)))
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(payload, {"error": "method_not_allowed"})
        self.assertEqual(self.record_count(), 0)

    def test_the_two_paths_never_fall_back_to_each_other(self):
        """⭐ 两条路**互不兜底**（`wsgi._secret_for` 是唯一分派点）。

        能红的两侧（`AGENTS.md` §5-G「每条钉子都要弄红」）：① 旧路径也去查「码 → 密钥」文件
        ⇒ 第一条断言变 200 ⇒ 红；② 新路径回落 `shared/callback.env` 那把 ⇒ 第二条变 200 ⇒ 红。
        """
        # ① 旧路径 + **每码密钥**签的报文 ⇒ 401（旧路径只认 `callback.env` 那把）
        status, _, _ = _call(_environ(_signed_payload(F1_PARAMS, SECRET_CODE_A)))
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(self.last_record()["sign_reason"], signature.REASON_SIGN_MISMATCH)
        # ② 新路径 + **旧密钥**签的报文 ⇒ 401（新路径只认码那一行）
        status2, _, _ = _call(
            _environ(_signed_payload(F1_PARAMS, SECRET), path=_callback_path(CODE_A))
        )
        self.assertEqual(status2, "401 Unauthorized")
        self.assertEqual(self.last_record()["sign_reason"], signature.REASON_SIGN_MISMATCH)
        # ③ 自证：各自用**自己那把**密钥 ⇒ 200（否则上面两条 401 可能只是"哪把都不认"）
        self.assertEqual(_call(_environ(_signed_payload(F1_PARAMS)))[0], "200 OK")
        self.assertEqual(
            _call(_environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_A)))[0],
            "200 OK",
        )


class RecordShapeAcrossPathsTests(_ReceiverCase):
    """**A7 / A8**：`record_version` 与记录形状（spec §13.1）。"""

    def test_a7_record_version_is_4_and_both_paths_share_one_key_set(self):
        """**A7**：`record_version = 4`（字面量），且新旧记录的**键集合相同**。

        弄红它：新记录少一个键 / 多一个键 / `RECORD_VERSION` 忘了升 ⇒ 本用例红。
        """
        _call(_environ(_signed_payload(F1_PARAMS)))                                     # 旧路径 200
        _call(_environ(b"not json", path=_callback_path(CODE_A)))                        # 新路径 400
        _call(_environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_A)))   # 新 200
        _call(_environ(_signed_payload(F1_PARAMS, SECRET_CODE_A), path=_callback_path(CODE_ABSENT)))  # 新 401
        records = self.records()
        self.assertEqual(len(records), 4, "自证失败：样本不够，本用例不产出结论")
        self.assertEqual({frozenset(record) for record in records}, {RECORD_KEYS})
        for record in records:
            self.assertEqual(record["record_version"], RECORD_VERSION_LITERAL)
        self.assertEqual(
            [record["path"] for record in records],
            [
                "/jidanpu/v1/notify",
                f"/api/jidanpu/callback/{CODE_A}",
                f"/api/jidanpu/callback/{CODE_A}",
                f"/api/jidanpu/callback/{CODE_ABSENT}",
            ],
            "「哪条路进来的」只能从 path 的取值域读出来（记录的键集合不许为此新增字段）",
        )
        # 分流判据是**全等**（spec §5.5）：前缀比对会把别人的码也收进来。
        new_path = f"/api/jidanpu/callback/{CODE_A}"
        self.assertTrue(new_path == f"{wsgi.CALLBACK_PATH_PREFIX}{CODE_A}")
        self.assertNotEqual(f"{wsgi.CALLBACK_PATH_PREFIX}{CODE_A[:21]}", new_path)

    def test_a8_the_per_code_secret_never_lands_in_the_journal_or_the_command_output(self):
        """**A8**：新文件里的密钥值**不进**事件文件、**不进**排查命令的输出（哨兵用例）。

        ⚠️ 自证那一步承重：哨兵**确实**是有效密钥（用它签的那条验签通过），否则本用例可能只是
        "哨兵压根没被用上"、什么都没证明。
        """
        sentinel = "sentinel-per-code-secret-0002"
        self.keys_file.write_text(
            f"{KEYS_FILE_HEADER}\n{CODE_A}\t{sentinel}\n", encoding="utf-8",
        )
        _call(_environ(_signed_payload(F1_PARAMS, sentinel), path=_callback_path(CODE_A)))
        _call(_environ(_signed_payload(F1_PARAMS, sentinel), path=_callback_path(CODE_ABSENT)))
        self.assertTrue(self.records()[0]["sign_ok"], "自证失败：哨兵没被当成有效密钥，本用例不产出结论")

        self.assertNotIn(sentinel, self.journal_text(), "密钥值进了事件文件")
        for extra in ((), ("--raw",)):
            output = StringIO()
            call_command("callback_events", *extra, stdout=output, stderr=output)
            self.assertNotIn(sentinel, output.getvalue(), f"密钥值进了命令输出（{extra}）")


class CallbackKeysFileTests(SimpleTestCase):
    """⭐「码 → 密钥」文件的**格式与查找方式**（T3 写那一侧必须对齐这一份；spec §5.3）。"""

    def _load(self, text: str, code: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "callback-keys.env"
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
            with mock.patch.dict(os.environ, {"CALLBACK_KEYS_FILE": str(path)}):
                return config.load_callback_secret(code)

    def test_the_format_is_code_tab_secret_with_comments_and_blank_lines(self):
        """一行一条 `<码><TAB><密钥>`；首行 `#` 注释与空行被忽略。"""
        text = f"{KEYS_FILE_HEADER}\n\n{CODE_A}\t{SECRET_CODE_A}\n{CODE_ABSENT}\tsecret-b\n"
        self.assertEqual(
            config.parse_callback_keys(text),
            {CODE_A: SECRET_CODE_A, CODE_ABSENT: "secret-b"},
        )
        self.assertEqual(self._load(text, CODE_A), SECRET_CODE_A)
        self.assertEqual(self._load(text, CODE_ABSENT), "secret-b")

    def test_a_space_separated_or_equals_separated_line_is_not_an_entry(self):
        """⛔ 分隔符只认 TAB：空格 / `=` 那些写法**不当成条目**（⛔ 不猜，否则形状会漂）。"""
        text = f"{CODE_A} {SECRET_CODE_A}\n{CODE_ABSENT}={SECRET_CODE_A}\n"
        self.assertEqual(config.parse_callback_keys(text), {})
        for code in (CODE_A, CODE_ABSENT):
            with self.assertRaises(config.CallbackCodeUnknown):
                self._load(text, code)

    def test_the_first_entry_wins_when_a_code_appears_twice(self):
        """同一个码出现多次 ⇒ **第一条胜出**（⛔ 不覆盖：不猜哪一行"更新"）。"""
        text = f"{CODE_A}\tfirst-secret\n{CODE_A}\tsecond-secret\n"
        self.assertEqual(config.parse_callback_keys(text), {CODE_A: "first-secret"})
        self.assertEqual(self._load(text, CODE_A), "first-secret")

    def test_a_carriage_return_at_the_end_of_the_line_is_tolerated(self):
        """Windows 上编辑过的文件（`\\r\\n`）照样能读（⚠️ 只容 `\\r`，⛔ 其余空白不归一化）。"""
        text = f"{KEYS_FILE_HEADER}\r\n{CODE_A}\t{SECRET_CODE_A}\r\n"
        self.assertEqual(config.parse_callback_keys(text), {CODE_A: SECRET_CODE_A})

    def test_a_secret_containing_more_tabs_keeps_the_rest_intact(self):
        """密钥里万一有 TAB ⇒ 原样保留（`partition` 而不是 `split`）。"""
        text = f"{CODE_A}\tsec\twith\ttabs\n"
        self.assertEqual(config.parse_callback_keys(text), {CODE_A: "sec\twith\ttabs"})

    def test_the_two_failure_buckets_are_different_exception_types(self):
        """⭐ 两个异常类**互不为子类**（spec §13.1 A3 对 A4 的"必须分得开"）。

        弄红它：让 `CallbackCodeUnknown` 继承 `SecretUnavailable`（或反过来）⇒ `except` 的先后
        顺序就变成承重逻辑，而调用方很容易把它们合成一个码。
        """
        self.assertFalse(issubclass(config.CallbackCodeUnknown, config.SecretUnavailable))
        self.assertFalse(issubclass(config.SecretUnavailable, config.CallbackCodeUnknown))

    def test_a_missing_file_is_secret_unavailable_not_an_unknown_code(self):
        """文件不在 ⇒ `SecretUnavailable`（503 那一档），⛔ 不是"码不认识"。"""
        with mock.patch.dict(
            os.environ, {"CALLBACK_KEYS_FILE": "/tmp/definitely-not-here-tt/callback-keys.env"}
        ):
            with self.assertRaises(config.SecretUnavailable):
                config.load_callback_secret(CODE_A)

    def test_a_code_missing_from_the_file_is_an_unknown_code(self):
        with self.assertRaises(config.CallbackCodeUnknown):
            self._load(f"{CODE_A}\t{SECRET_CODE_A}\n", CODE_ABSENT)

    def test_a_line_without_a_secret_is_an_unknown_code(self):
        """有这一行、但密钥是空的 ⇒ 同样"这个码取不到密钥"（⛔ 不许回一个空密钥去验签）。"""
        for text in (f"{CODE_A}\t\n", f"{CODE_A}\n"):
            with self.subTest(text=text):
                with self.assertRaises(config.CallbackCodeUnknown):
                    self._load(text, CODE_A)

    def test_a_world_readable_keys_file_warns_but_does_not_block(self):
        """文件对属主之外可读 ⇒ **响亮警告**、但照样能取到密钥（拒服务会让回调全变 5xx）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "callback-keys.env"
            path.write_text(f"{CODE_A}\t{SECRET_CODE_A}\n", encoding="utf-8")
            path.chmod(0o644)
            with mock.patch.dict(os.environ, {"CALLBACK_KEYS_FILE": str(path)}):
                with self.assertLogs("apps.callback.config", level="WARNING") as logs:
                    self.assertEqual(config.load_callback_secret(CODE_A), SECRET_CODE_A)
        self.assertTrue(any("chmod 600" in line for line in logs.output))


# ═══════════════════════════════════════════════════════════════════════════
# ⑨ 旧路径**逐条冻结**（**A6**）：状态码表 + 落盘键集合，钉成**字面量**（⛔ 不从实现里取常量）。
#    线上那条路（`callback.suqing.chat`）正在收鸡蛋铺真实回调 ⇒ 新增必须是**增量**
#    （既有用例继续守细枝末节，这一份钉两件大事）。
# ═══════════════════════════════════════════════════════════════════════════


class OldPathFrozenTests(_ReceiverCase):
    """旧路径 `/jidanpu/v1/notify` 的行为**一个字没变**。"""

    def test_a6_the_status_code_table_is_unchanged(self):
        """状态码表逐行钉死（每一行都是**字面量**期望值）。

        弄红它：让旧路径也去查「码 → 密钥」文件（`wsgi._secret_for` 的分派只认 `route`）⇒ 红。
        """

        def oversize() -> str:
            with mock.patch.object(wsgi, "MAX_BODY_BYTES", 100):
                return _call(_environ(b"x" * 5000))[0]

        def secret_gone() -> str:
            os.environ["CALLBACK_ENV_FILE"] = str(self.tmp / "nope.env")
            try:
                return _call(_environ(_signed_payload(F1_PARAMS)))[0]
            finally:
                os.environ["CALLBACK_ENV_FILE"] = str(self.env_file)

        table = (
            (
                "验签通过",
                lambda: _call(_environ(_signed_payload(F1_PARAMS)))[0],
                "200 OK",
                "ok",
            ),
            (
                "sign 缺失 / 空",
                lambda: _call(
                    _environ(json.dumps(F1_PARAMS, ensure_ascii=False).encode("utf-8"))
                )[0],
                "401 Unauthorized",
                "sign_missing",
            ),
            (
                "sign 对不上",
                lambda: _call(
                    _environ(_signed_payload(F1_PARAMS, secret="wrong-secret-000000000000000000"))
                )[0],
                "401 Unauthorized",
                "sign_mismatch",
            ),
            (
                "某字段不是标量",
                lambda: _call(
                    _environ(
                        json.dumps(
                            {**F1_PARAMS, "note": {"a": 1}, "sign": "0" * 32}, ensure_ascii=False
                        ).encode("utf-8")
                    )
                )[0],
                "401 Unauthorized",
                "unsigned_value",
            ),
            (
                "报文不是 JSON 对象",
                lambda: _call(_environ(b"not json"))[0],
                "400 Bad Request",
                "body_not_json",
            ),
            ("报文超过接收上限", oversize, "413 Payload Too Large", "body_too_large"),
            ("**我们**读不到密钥文件", secret_gone, "503 Service Unavailable", "secret_unavailable"),
        )
        for label, call, expected_status, expected_reason in table:
            with self.subTest(case=label):
                self.assertEqual(call(), expected_status)
                self.assertEqual(self.last_record()["sign_reason"], expected_reason)
        # 每一行的记录都必须是**老形状**（键集合 + 版本号一起钉）
        self.assertEqual(len(self.records()), len(table), "自证失败：样本数与表不符")
        for record in self.records():
            self.assertEqual(frozenset(record), RECORD_KEYS)
            self.assertEqual(record["record_version"], RECORD_VERSION_LITERAL)
        self.assertEqual([record["path"] for record in self.records()],
                         ["/jidanpu/v1/notify"] * len(table))

    def test_a6_non_post_and_other_paths_still_write_nothing(self):
        """⛔ 非 POST / 别的路径**一个字节都不写**（老口径，两条路同一优先级）。"""
        for method, path, expected in (
            ("GET", wsgi.NOTIFY_PATH, "405 Method Not Allowed"),
            ("PUT", wsgi.NOTIFY_PATH, "405 Method Not Allowed"),
            ("POST", "/jidanpu/v1/notify/", "404 Not Found"),
            ("POST", "/jidanpu/v1/notify/extra", "404 Not Found"),
            ("POST", "/api/business/orders/", "404 Not Found"),
        ):
            with self.subTest(method=method, path=path):
                status, _, _ = _call(
                    _environ(_signed_payload(F1_PARAMS), method=method, path=path)
                )
                self.assertEqual(status, expected)
        self.assertEqual(self.record_count(), 0, "不该受理的请求竟然落了盘")

    def test_a6_the_record_key_set_and_version_are_frozen(self):
        """⭐ 旧路径落盘的**键集合**是字面量冻结的（往 `_new_record` 加一个键 ⇒ 本用例红）。

        最容易犯的错：为"区分哪条路进来的"给记录加 `route` / `callback_code` 字段 —— 那会改掉
        旧路径的落盘形状（线上 700+ 条真实回调都是老形状）⇒ 「哪条路」只能由 `path` 取值域表达。
        """
        _call(_environ(_signed_payload(F1_PARAMS)))
        _call(_environ(b"not json"))
        records = self.records()
        self.assertEqual(len(records), 2, "自证失败：样本不够，本用例不产出结论")
        for record in records:
            self.assertEqual(frozenset(record), RECORD_KEYS)
            self.assertEqual(record["record_version"], RECORD_VERSION_LITERAL)
        self.assertEqual(records[0]["path"], "/jidanpu/v1/notify")
        self.assertTrue(records[0]["sign_ok"])
        self.assertFalse(records[1]["json_ok"])
        # 参数式的两件事仍在：`path` 是**逐字**的 PATH_INFO、查询串单独记
        environ = _environ(_signed_payload(F1_PARAMS))
        environ["QUERY_STRING"] = "debug=1"
        _call(environ)
        self.assertEqual(self.last_record()["query_string"], "debug=1")

    def test_a_broken_keys_file_does_not_affect_the_old_path(self):
        """⭐ 旧路径**根本不读**那份新文件：把它指到一个读不到的地方 ⇒ 旧路径照样 200。

        这条是"增量"最直接的判据：旧路径若被改成"先看看有没有按码的密钥"，
        它会因为文件读不到而回 503（或因为码空而回 401）⇒ 本用例红。
        """
        os.environ["CALLBACK_KEYS_FILE"] = str(self.tmp / "shared" / "no-such-keys.env")
        status, payload, _ = _call(_environ(_signed_payload(F1_PARAMS)))
        self.assertEqual(status, "200 OK")
        self.assertEqual(payload, {})
        self.assertTrue(self.last_record()["sign_ok"])
