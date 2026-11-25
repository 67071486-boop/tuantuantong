"""`local_base_url` 的判据（spec §6.1 / **裁决 R12** / spec §14-R13）。

守的是一条**生产后果**：生产由 nginx 终止 TLS、gunicorn 侧说 http，而本仓**没有**
`SECURE_PROXY_SSL_HEADER` ⇒ `request.scheme` 恒为 `"http"`。这个值在握手时交给上家、
成为它后来**推送**用的地址 ⇒ 若只按请求推导，交出去的是 `http://…`，推送会打到 nginx 的
80 → 301，而 `httpx` **默认不跟随重定向** ⇒ **下行推送永远失败**（整条功能不工作）。

判据分两层，缺一层就有假保证：

1. **配置形态**（照 `tests/test_p5_envelope_csrf.py` 的 `CSRF_TRUSTED_ORIGINS` 句式）：
   非 DEBUG 时 `PEER_SYNC_LOCAL_BASE_URL` 必须非空、`https://` 开头，**且真的过 URL 闸门**
   （评审 C4：只看前缀会让 `https://localhost` / `https://host/path` 蒙混过关，
   到第一次请求才炸）；生产漏配时**响亮地红**；
2. **取值行为**：配置非空时**赢过请求**且过同一道 URL 闸门（归一化）；留空时才按请求推导
   （本地开发形态，且有「换 scheme 结果跟着变」的对照，证明推导真的读了请求而不是常量）。
"""
from django.conf import settings
from django.test import RequestFactory, SimpleTestCase, override_settings

# 配置期的 DEBUG（`config.settings` 模块的全局），**不是** `django.conf.settings.DEBUG`：
# `manage.py test` 用 `setup_test_environment(debug=False)` 把后者**强制改成 False**
# （`django/test/runner.py` → `django/test/utils.py`），拿它做判据，下面「生产形态」那一支
# 在测试运行里会永远走同一边、变成死代码。同款处理见 `tests/test_p5_envelope_csrf.py` 里
# 「配置期的 DEBUG」那一段（它同样 `from config import settings as project_settings`）。
from config import settings as project_settings

from apps.business.api.peer_views import _local_base_url
from apps.business.peer_sync import normalize_base_url

#: 请求侧的主机（本地开发形态的租户域名），与配置值**必须不同**，
#: 这样「配置赢了」才不是靠两边恰好相等蒙对的。
REQUEST_HOST = "dev.test.local"


def _request(*, secure=False):
    """一条只带 scheme + host 的请求——`_local_base_url` 只读这两样。"""
    return RequestFactory(SERVER_NAME=REQUEST_HOST).get("/", secure=secure)


class PeerSyncLocalBaseUrlShapeTests(SimpleTestCase):
    """配置形态：非 DEBUG 必须是显式的 `https://` 基址，**且必须真的过 URL 闸门**。"""

    def _assert_production_shape(self, configured: str) -> None:
        """生产形态的判据（**唯一实现**，下面两条用例共用）。

        ⚠️ 评审 C4：旧版只看 `startswith("https://")` ⇒ `https://localhost`、
        `https://peer-a.test.invalid/api/`、`https://127.0.0.1` 全部放行，然后要到
        **第一次请求**才炸（`_local_base_url` 对配置值也跑 `normalize_base_url`，
        而它拒内网 / 拒路径）——一个"配置形态判据"却在请求期才失败。
        这里把配置值**真的送进那道闸门**：过不了就红在配置形态这一步。
        """
        normalized = normalize_base_url(configured)     # 非法 ⇒ 直接抛 ValueError
        self.assertTrue(
            normalized.startswith("https://"),
            f"{configured!r} 归一化后不是 https（生产必须显式 https，"
            f"否则握手交给上家的是 http://，下行推送会死在 nginx 的 80 → 301）："
            f"{normalized!r}",
        )

    def test_production_requires_an_explicit_https_local_base_url(self):
        configured = settings.PEER_SYNC_LOCAL_BASE_URL
        is_local_dev_shape = bool(project_settings.DEBUG)
        if not configured:
            self.assertTrue(
                is_local_dev_shape,
                "PEER_SYNC_LOCAL_BASE_URL 留空只对本地开发成立（配置期 DEBUG 为真）："
                "生产必须显式配 https://<租户域名>，否则握手交给上家的是 http://，"
                "下行推送会死在 nginx 的 80 → 301（httpx 默认不跟随重定向）。",
            )
            return
        self._assert_production_shape(configured)

    def test_the_shape_check_rejects_values_that_would_only_fail_at_request_time(self):
        """⭐ 这条让上面那条**能失败**（形态判据自己的"弄红"证明）。

        ⚠️ 本机是开发形态、配置留空 ⇒ 上面那条走 `return` 那一支，永远碰不到闸门
        ⇒ 没有这条，形态判据在开发机上就是**恒真**的（`AGENTS.md` §5-G）。
        """
        for bad in (
            "https://localhost",                              # 本机地址
            "https://127.0.0.1",                              # 内网地址
            "https://peer-a.test.invalid/api/",               # 带了路径（拼端点会拼错）
            "https://peer-a.test.invalid?x=1",                # 带了查询串
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError, msg=f"{bad} 不该过 URL 闸门"):
                    self._assert_production_shape(bad)
        # `http://` 过得了闸门（闸门允许 http），但**不是生产形态** ⇒ 由 https 那一条挡。
        with self.assertRaises(AssertionError):
            self._assert_production_shape("http://peer-a.test.invalid")


class LocalBaseUrlTests(SimpleTestCase):
    """取值行为：配置优先、请求兜底。"""

    @override_settings(PEER_SYNC_LOCAL_BASE_URL="")
    def test_empty_setting_falls_back_to_the_request(self):
        self.assertEqual(_local_base_url(_request()), f"http://{REQUEST_HOST}")

    @override_settings(PEER_SYNC_LOCAL_BASE_URL="")
    def test_the_fallback_follows_the_request_scheme(self):
        """对照：留空时推导**确实**读请求（否则上面那条可能是写死的常量）。"""
        self.assertEqual(_local_base_url(_request(secure=True)), f"https://{REQUEST_HOST}")

    @override_settings(PEER_SYNC_LOCAL_BASE_URL="HTTPS://Peer.Example.Test/")
    def test_configured_value_wins_and_is_normalised(self):
        """配置非空时**赢过请求**（host 不同），且过同一道 URL 闸门（大小写 / 尾斜杠归一）。"""
        self.assertEqual(
            _local_base_url(_request(secure=False)),
            "https://peer.example.test",
        )
