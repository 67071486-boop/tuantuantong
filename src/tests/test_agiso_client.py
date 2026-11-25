"""阿奇索客户端的纯函数与错误映射。**不联网**：HTTP 一律打桩。

- 业务路径的桩打在 `apps.business.agiso._post_form` 上（E-0029：打在**被查找的**那个模块上）；
- 网络异常那一组刻意打在 `urllib.request.urlopen` 上，好让 `_post_form` 自己的翻译层也在被测范围内。
"""
import hashlib
import http.client
import os
import time
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apps.business.agiso import DEFAULT_BASE_URL, AgisoError, order_detail, sign_params

# 官方文档给的向量：`foo=1, bar=2, foo_bar=3, foobar=4` 按参数名排序后拼成下面这串（逐字抄自文档）。
# 提成模块级常量，是为了让「拼串规则」本身有一条**不经过生产代码**的判据（见 SignParamsTests）。
_DOC_VECTOR_RAW = "bar2foo1foo_bar3foobar4"


class AgisoSettingsTests(SimpleTestCase):
    def test_keys_exist_with_safe_defaults(self):
        """本地没有凭证也要能起服务、能跑测试 ⇒ 有默认值，**不是**必填。

        ⚠️ 刻意**不**断言两个密钥等于 `""`：本机 `.env` 眼下确实没设这两个键
        （`settings.py` 的默认值也是 `""`，实测取到的就是空串），但只要谁往 `.env` 填了真凭证，
        那条断言立刻假红——这是**环境相关**的假红，不是代码错
        （同型教训见 `AGENTS.local.md` 里 `DJANGO_CSRF_TRUSTED_ORIGINS` 那段）。
        断「是 str」已足以证明「有默认值」：真若必填，缺变量会在 import settings 时
        就抛 `ImproperlyConfigured`，这一整个测试模块根本收集不起来。
        """
        self.assertTrue(hasattr(settings, "AGISO_APP_ID"))
        self.assertTrue(hasattr(settings, "AGISO_APP_SECRET"))
        self.assertIsInstance(settings.AGISO_APP_ID, str)
        self.assertIsInstance(settings.AGISO_APP_SECRET, str)
        self.assertIsInstance(settings.AGISO_API_BASE, str)

    def test_api_base_default_and_env_wiring(self):
        """基址：**环境感知的等值断言**——「settings 确实从 env 读」这一半恒成立；
        「默认值 = 实测基址」那一半**只在环境里没有 `AGISO_API_BASE` 这个键时**才在判据里（见下）。

        期望值一律取 `os.environ.get("AGISO_API_BASE", DEFAULT_BASE_URL)`（**带 default 参数**），
        这与 settings 自己的读取路径同源：`django-environ` 的 `read_env` 把 `.env` 灌进
        `os.environ`，而 `get_value` 是「`os.environ` 里**有**这个键就用它、哪怕值是空串，
        只有 `KeyError` 才回落到 default」（`environ/environ.py` 的 `get_value`）。
        ⚠️ **不要**写成 `os.environ.get("AGISO_API_BASE") or DEFAULT_BASE_URL`：
        空串是假值，`or` 会把「配置成空」误判成「没配置」，而 `AGISO_API_BASE=`（空值）是
        **合法配置**——Task 4 取用时是 `getattr(settings, "AGISO_API_BASE", "") or DEFAULT_BASE_URL`，
        空值照样兜到实测基址。实测：`.env` 里写 `AGISO_API_BASE=` 时 settings 取到 `''`，
        `or` 版本假红、本版本绿。
        ⚠️ `DEFAULT_BASE_URL` 从 `apps.business.agiso` import，**不在这里另抄一份字面量**。
        但要先看清这条断言的**前提**：`os.environ.get("AGISO_API_BASE", DEFAULT_BASE_URL)`
        只有「环境里**没有**这个键」时才落到 `DEFAULT_BASE_URL`，那时它才真的同时钉住
        「`settings.py` 的默认字面量」与「`agiso.DEFAULT_BASE_URL`」两份拷贝（哪一份漂移都会红）。
        而 `django-environ` 的 `read_env` 会把 `.env` 灌进 `os.environ`，**且 `.env.example:96`
        默认就写着 `AGISO_API_BASE=…`**（Task 5 加的那一行）⇒ 按 README 复制过 `.env` 的机器上，
        这条断言退化成「`settings.AGISO_API_BASE` == env 里的值」：**两份拷贝都不在比较里**，
        改漂移了它也不会红；此时它唯一钉住的是「settings 确实从 env 读」。
        （本机实测：`.env` 与 worktree 的 `.env` 今天都**没有** `AGISO_API_BASE=` 这个键
        ⇒ 本机确实走 default 那支，上面那两份拷贝在本机是被钉住的；换到按 README 配过的
        环境就不是了——**别把这条当"两份拷贝永远被钉住"的保证**。）
        ⚠️ 也不退化成 `startswith("http")`：那样 scheme/host/平台段写错都能过，
        连 spec §3.2 专门警告的 `alds` vs `aldsIdle`（写 `alds` 上游回 `Error_Code:5 未知的URL`）
        都拦不住。等值断言配 env 覆盖，两头都不丢。
        """
        self.assertEqual(
            settings.AGISO_API_BASE,
            os.environ.get("AGISO_API_BASE", DEFAULT_BASE_URL),
        )


class SignParamsTests(SimpleTestCase):
    def test_matches_documented_vector(self):
        """官方文档给的向量：foo=1, bar=2, foo_bar=3, foobar=4 → bar2foo1foo_bar3foobar4。

        ⚠️ **承重判据是「拼串」**：按算法（除 `sign` 外按参数名 ASCII 排序 → 拼「名+值」无分隔符）
        必须得到 `bar2foo1foo_bar3foobar4` —— 那是**官方文档原文给的向量**，也是本条用例真正
        在钉的东西。下面的 md5 字面量只是「该拼串 + 本用例传的 secret `S3cret`」代入
        `md5(secret + 拼串 + secret)` 的**独立算出值**（不是从实现反推的），即
        `md5("S3cret" + "bar2foo1foo_bar3foobar4" + "S3cret")`。
        ⚠️ 字面量与拼串二者冲突时**信拼串**：拼串与官方文档、spec §3.2 可三方对拍；
        字面量只对得上「拼串 + 某一个特定 secret」这一对组合，换 secret 就会变
        （实测：secret 换成 `MyAppSecret` 同一个拼串得 `de7f6c657f947ebb70183b3a7a35ea0c`）。
        """
        got = sign_params({"foo": 1, "bar": 2, "foo_bar": 3, "foobar": 4}, "S3cret")
        # 期望值是**独立算出来的字面量**（不是拿生产代码自己算自己）——见 AGENTS.md §5 的自指断言禁令
        self.assertEqual(got, "76467a96e85e32b2d92cd7e986c54a9f")

    def test_matches_documented_vector_with_second_secret(self):
        """同一个已知答案、**换一个 secret** —— 让「拼串」这条承重判据真正被断言上。

        ⚠️ 为什么需要第二条：单条用例里的 md5 是**单向**的，拼串本身从未被断言，
        它只活在 docstring 里。两个不同 secret 的字面量互为**独立向量**：
        任何规则变化（排序、分隔符、包裹方式、编码）都会**同时**弄红这两条，
        而 secret 若被误改只会弄红其中一条 —— 由此可分辨「算法错」还是「入参错」。
        ⚠️ `de7f6c657f947ebb70183b3a7a35ea0c` 就是最初实施计划里那个字面量的来源
        （它配的是 secret `MyAppSecret`，与上一条用例的 `S3cret` 不同）——
        现由官方文档算法派生，**不是**从实现反推的。
        """
        got = sign_params({"foo": 1, "bar": 2, "foo_bar": 3, "foobar": 4}, "MyAppSecret")
        self.assertEqual(got, "de7f6c657f947ebb70183b3a7a35ea0c")

    def test_concat_rule_matches_documented_vector(self):
        """承重的**拼串规则**本身必须被断言，不能只活在 docstring 里。

        官方文档给的向量：`foo=1, bar=2, foo_bar=3, foobar=4` ⇒ 按参数名 ASCII 排序后
        拼成 `bar2foo1foo_bar3foobar4`（无分隔符）。本条的期望 md5 由**测试自己**拿上面那个
        **字面拼串**算出来（`md5(secret + 拼串 + secret)`），**不经过** `sign_params`
        ⇒ 不是自指断言：排序写错、插了分隔符、漏掉某个参数，拼串就变了，本条必红。
        ⚠️ 它与上面两条 md5 字面量用例是**互补**的：那两条钉「这个具体结果」，
        本条钉「结果是从哪个串算出来的」。若有人把上面两条的字面量按坏实现重新生成过，
        那两条会转绿，本条**仍然红**——这正是它存在的意义。
        """
        self.assertEqual(
            sign_params({"foo": 1, "bar": 2, "foo_bar": 3, "foobar": 4}, "S3cret"),
            hashlib.md5(f"S3cret{_DOC_VECTOR_RAW}S3cret".encode("utf-8")).hexdigest(),
        )

    def test_sign_is_excluded(self):
        a = sign_params({"tid": "1", "sign": "x"}, "S")
        b = sign_params({"tid": "1"}, "S")
        self.assertEqual(a, b)

    def test_tolerates_extra_params(self):
        """验签要容忍新增参数（官方测试工具的「带多余参数」用例）：加一个键，结果必须变。"""
        self.assertNotEqual(
            sign_params({"tid": "1"}, "S"), sign_params({"tid": "1", "extra": "9"}, "S")
        )


class OrderDetailTests(SimpleTestCase):
    def _call(self, payload):
        with mock.patch("apps.business.agiso._post_form", return_value=payload) as patched:
            data = order_detail("1520087523676842500", "tok", app_id="app",
                                app_secret="sec", base_url="https://example.invalid/x")
        return data, patched

    def test_success_returns_data(self):
        data, patched = self._call({"IsSuccess": True, "Data": {"payment": 1234}, "Error_Code": 0})
        self.assertEqual(data, {"payment": 1234})
        sent = patched.call_args.kwargs["params"]
        self.assertEqual(sent["tid"], "1520087523676842500")
        self.assertIn("sign", sent)
        self.assertIn("timestamp", sent)

    def test_upstream_error_raises_with_code_and_request_id(self):
        with self.assertRaises(AgisoError) as ctx:
            self._call({"IsSuccess": False, "Error_Code": 1, "Error_Msg": "Invalid access token",
                        "RequestId": "R1"})
        self.assertEqual(ctx.exception.upstream_code, 1)
        self.assertEqual(ctx.exception.request_id, "R1")

    def test_token_goes_into_authorization_header(self):
        _, patched = self._call({"IsSuccess": True, "Data": {}, "Error_Code": 0})
        self.assertEqual(patched.call_args.kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(patched.call_args.kwargs["headers"]["ApiVersion"], "1")

    def test_posts_to_order_detail_under_the_configured_base(self):
        """URL 形状：Task 4 整段 stub 掉 `order_detail`，这条实测结论在此**别无覆盖**。"""
        _, patched = self._call({"IsSuccess": True, "Data": {}, "Error_Code": 0})
        self.assertEqual(
            patched.call_args.kwargs["url"], "https://example.invalid/x/Order/Detail"
        )

    def test_timestamp_is_seconds_not_milliseconds(self):
        """`timestamp` 是**秒级**（spec §3.2：服务端允许 ±10 分钟）。

        毫秒实现（`int(time.time() * 1000)`）会离 `time.time()` 差 3 个数量级 ⇒ 本条必红。
        容忍 ±60 秒，既覆盖这 3 个数量级的差异，也不与墙钟抖动/慢机器耦合。
        """
        _, patched = self._call({"IsSuccess": True, "Data": {}, "Error_Code": 0})
        sent = patched.call_args.kwargs["params"]
        self.assertLess(abs(int(time.time()) - sent["timestamp"]), 60)


class OrderDetailNetworkErrorTests(SimpleTestCase):
    """网络层失败必须**翻译**成 `AgisoError`：Task 4 只 `except AgisoError`（spec §8 ⇒ 1017，不得 500）。

    ⚠️ 这里刻意**不**把桩打在 `_post_form` 上、而是打在它内部的 `urlopen` 上：
    要覆盖的正是「`_post_form` 有没有把底层异常收干净」——把 `_post_form` 整个换掉等于
    把被测的翻译层一起换掉，测不出任何东西（同族教训：断言要打在真实出口上，E-0009）。
    """

    def _urlopen_returning(self, body: bytes):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = body
        return response

    def _expect_agiso_error(self):
        return self.assertRaises(AgisoError)

    def test_response_body_truncated_raises_agiso_error(self):
        """`IncompleteRead` 不是 `URLError`（它的 MRO 是 HTTPException → Exception，**不经 OSError**），
        声明 `Content-Length: 100` 只回 31 字节即触发 ⇒ 旧代码让它逃逸成 500。"""
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b'{"IsSuccess": true, "Data":'),
        ):
            with self._expect_agiso_error() as ctx:
                order_detail(
                    "1520087523676842500", "tok", app_id="app",
                    app_secret="sec", base_url="https://example.invalid/x",
                )
        self.assertEqual(ctx.exception.upstream_code, -1)

    def test_connection_reset_raises_agiso_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")):
            with self._expect_agiso_error() as ctx:
                order_detail(
                    "1520087523676842500", "tok", app_id="app",
                    app_secret="sec", base_url="https://example.invalid/x",
                )
        self.assertEqual(ctx.exception.upstream_code, -1)

    def test_non_utf8_body_raises_agiso_error(self):
        """响应体不是合法 UTF-8 ⇒ `decode()` 抛 `UnicodeDecodeError`，也不是 `URLError`。"""
        with mock.patch(
            "urllib.request.urlopen",
            return_value=self._urlopen_returning(b'{"IsSuccess": true, "x": "\xff\xfe"}'),
        ):
            with self._expect_agiso_error() as ctx:
                order_detail(
                    "1520087523676842500", "tok", app_id="app",
                    app_secret="sec", base_url="https://example.invalid/x",
                )
        self.assertEqual(ctx.exception.upstream_code, -1)


class OrderDetailUpstreamShapeTests(SimpleTestCase):
    """上游**形状**不对时也不得逃逸（同一个坑：Task 4 只捕 `AgisoError`）。"""

    def _call(self, payload):
        with mock.patch("apps.business.agiso._post_form", return_value=payload):
            return order_detail(
                "1520087523676842500", "tok", app_id="app",
                app_secret="sec", base_url="https://example.invalid/x",
            )

    def test_non_numeric_error_code_raises_agiso_error(self):
        """`Error_Code` 非数字：旧的 `int(... or 0)` 会抛 `ValueError` 逃逸成 500。"""
        with self.assertRaises(AgisoError) as ctx:
            self._call({"IsSuccess": False, "Error_Code": "abc", "Error_Msg": "boom",
                        "RequestId": "R9"})
        self.assertEqual(ctx.exception.upstream_code, -1)
        self.assertIn("abc", ctx.exception.upstream_msg)
        self.assertEqual(ctx.exception.request_id, "R9")

    def test_error_code_minus_one_is_not_reported_as_non_numeric(self):
        """`-1` 与 `"-1"` 都**能**转成整数 ⇒ msg 里不得出现「非数字」。

        ⚠️ 判据必须是「**转换失败**」本身，不能拿 `code == -1` 当「非数字」的判据：
        `-1` 是上游**真的可以回**的合法码，两者混为一谈时 msg 会写成
        「上游 Error_Code 非数字：-1」——与上游实回内容直接矛盾，
        而这段 msg 是 Task 4 warning 日志的文案来源（只污染日志，不影响控制流）。
        """
        for raw in (-1, "-1"):
            with self.subTest(raw=raw):
                with self.assertRaises(AgisoError) as ctx:
                    self._call({"IsSuccess": False, "Error_Code": raw, "Error_Msg": "boom",
                                "RequestId": "R7"})
                self.assertEqual(ctx.exception.upstream_code, -1)
                # 码能转成整数 ⇒ Error_Msg 原样传出，不得被追加任何注记
                self.assertEqual(ctx.exception.upstream_msg, "boom")
                self.assertNotIn("非数字", ctx.exception.upstream_msg)
                self.assertEqual(ctx.exception.request_id, "R7")

    def test_non_dict_json_body_raises_agiso_error(self):
        """上游回 `[]` 这类**非 dict 的 JSON**：旧的 `payload.get(...)` 会抛 `AttributeError` 逃逸。"""
        for payload in ([], "ok", 123):
            with self.subTest(payload=payload):
                with self.assertRaises(AgisoError) as ctx:
                    self._call(payload)
                self.assertEqual(ctx.exception.upstream_code, -1)

    def test_non_dict_data_raises_agiso_error(self):
        """`IsSuccess=true` 但 `Data` 不是 dict：契约「返回 `Data` 字典」必须**显式**守住。

        ⚠️ Task 4 的 `_to_form_fields` 会对返回值直接 `data.get("payment")`，而它只 `except AgisoError`
        ⇒ 这里若返回 list/str/数字，那边就抛 `AttributeError` → **500**，
        与本文件刚堵掉的「非 dict 响应体」是**同一类**逃逸。
        ⚠️ `Data` 缺失（`None`）也走这条：**不**静默换成 `{}` —— 「上游没给」与「上游给空」
        是两件事；静默替换会让录单页显示空价格而没人知道为什么。
        """
        for data in ([1, 2], "ok", 123, None):
            with self.subTest(data=data):
                with self.assertRaises(AgisoError) as ctx:
                    self._call({"IsSuccess": True, "Data": data, "Error_Code": 0})
                self.assertEqual(ctx.exception.upstream_code, -1)

    def test_bad_base_url_raises_agiso_error_not_value_error(self):
        """平台配置写坏（基址缺 scheme）⇒ `AgisoError`，**不是** `ValueError`（spec §8：不得 500）。

        `urllib.request.Request("no-scheme-here/Order/Detail", ...)` 抛
        `ValueError: unknown url type`（本机实测，Python 3.13），而 `ValueError` 既不是
        `URLError` 也不是 `OSError` ⇒ `Request(...)` 的构造若留在 `try` 之外，异常直接逃逸到
        Task 4 的视图（它只 `except AgisoError`）＝ **500**。
        ⚠️ 桩**不**打在 `_post_form` / `urlopen` 上：本条的判据正是「`_post_form` 内部的
        构造失败有没有被它自己的翻译层收干净」（同本文件网络异常那一组的理由）。
        ⚠️ 不联网：构造在 URL 打开之前就失败了。
        """
        with self.assertRaises(AgisoError) as ctx:
            order_detail(
                "1520087523676842500", "tok", app_id="app",
                app_secret="sec", base_url="no-scheme-here",
            )
        self.assertEqual(ctx.exception.upstream_code, -1)

    def test_empty_dict_data_is_legal_and_returned_verbatim(self):
        """`Data == {}` 是**合法**的（「给了但为空」），必须原样返回 `{}`、不得抛错。

        ⚠️ 这条与上一条合起来才是完整判据：要区分的是「**没给**」（非 dict / 缺失 ⇒ `AgisoError`）
        与「**给了一个空的**」（`{}` ⇒ 正常返回）。若把 `{}` 也当失败，录单页会把
        「上游确认这单没有可回填字段」误报成平台故障；若用 `or {}` 兜底，则反过来把
        「上游没给」伪装成「上游给空」——两个方向都是**静默**的，所以两头都要钉。
        """
        data = self._call({"IsSuccess": True, "Data": {}, "Error_Code": 0})
        self.assertEqual(data, {})
