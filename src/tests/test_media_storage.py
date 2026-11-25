"""图片存储线 Task 1：存储抽象（`LocalStorage` / `CosStorage`）与本机 blob 端点。

⚠️ 本文件的一切夹具值都是**合成值**（PII 守卫扫 `src/**`）——⛔ 不要写真实桶名 / 密钥 / 订单号。

## 三块各测什么

1. `LocalStorageTests`：本机实现的读写闭环（brief Step 1 的两条）+ 对象名规范
   + 目录穿越防护（含 Task 1 评审 Critical 的 `..` 绕过形状）+ 归一化租户判据；
2. `CosStorageSelectionTests`：`MEDIA_STORAGE_BACKEND=cos` 时 `get_storage()` 选到 `CosStorage`，
   且 SDK 构造**被 mock 掉**（⛔ 不联网、不需要云凭据）；
3. `MediaBlobEndpointTests`：**仅本机模式存在**的 `PUT/GET /api/media/blob/<path:key>`
   ——同一道「前缀必须是本租户」的闸（`AGENTS.md` §4 要求新增数据面补跨租户否定用例），
   含两条「`..` 绕出本租户命名空间」的回归钉子与「非法对象名 403 而非 500」的翻译钉子；
4. `BodySizeWallTests`：请求体墙（**E-0095**）——`DATA_UPLOAD_MAX_MEMORY_SIZE` 抬到 20 MiB 之后，
   `NonDirectUploadBodyLimitMiddleware` 必须把**非直传路径**恢复到 2.5 MiB 的旧姿态，
   ⛔ 但不能把本机直传（`/api/media/blob/`）一起拦掉。
"""
import mimetypes
import pathlib
import tempfile
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.db import connection
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django_tenants.test.cases import TenantTestCase
from rest_framework.test import force_authenticate

from apps.core.media_blob_views import MediaBlobView
from apps.core.storage import (
    ALLOWED_EXTS,
    CosStorage,
    LocalStorage,
    Presigned,
    THUMBNAIL_QUERY,
    append_game_logo_thumbnail_query,
    _reset_stable_get_urls,
    append_thumbnail_query,
    build_order_key,
    build_user_key,
    get_storage,
    key_belongs_to_tenant,
    key_media_kind,
)
from tests.p5_auth_helpers import make_logged_in_client

# ⭐ 结构网的源码锚点：按**模块自身位置**定位 `apps/` 包，不依赖跑测试时的 cwd。
import apps

APPS_ROOT = pathlib.Path(apps.__file__).resolve().parent

#: `TenantTestCase` 的租户域名（与 `make_logged_in_client` 的默认值同源）。
#: ⚠️ 请求必须显式打在这个 host 上：`self.client` 默认 `SERVER_NAME="testserver"`，
#: `TenantMainMiddleware` 在域名表里找不到它 ⇒ 进 urlconf 之前就 404（同型见
#: `tests/test_my_profile_api.py` 的 `TENANT_DOMAIN` 注释）。
TENANT_DOMAIN = "tenant.test.com"


class _TempMediaRootMixin:
    """把 `MEDIA_ROOT` 指到临时目录。

    ⚠️ **必须覆盖**：settings 的默认值是 `<仓库根>/.tools/media`，直接 `get_storage()`
    会往真实开发目录里写文件。`.tools/` 不进 git，但那是**本机数据**，测试不该动它
    （同族：`AGENTS.md` §5-G 的「跑测试要带自己的库后缀」——隔离是每个测试自己的责任）。
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)


class LocalStorageTests(_TempMediaRootMixin, TestCase):
    """本机实现的基本闭环（brief Step 1 的两条 + 形状与防穿越）。"""

    def test_put_head_delete_roundtrip(self):
        st = get_storage()
        self.assertIsInstance(st, LocalStorage)
        key = build_order_key("demo", "2026092500001", "jpg")
        self.assertTrue(key.startswith("demo/orders/2026092500001/"))

        # 直接走存储层写入（本机实现有内部写入口，供端点与测试用）
        st.put_bytes(key, b"hello", content_type="image/jpeg")
        self.assertEqual(st.head(key), (5, "image/jpeg"))

        with st.open_stream(key) as fh:
            self.assertEqual(fh.read(), b"hello")

        st.delete(key)
        self.assertIsNone(st.head(key))

    def test_head_missing_returns_none(self):
        self.assertIsNone(get_storage().head("demo/nope/does-not-exist.jpg"))

    def test_key_shapes_follow_spec_object_naming(self):
        """对象名形状逐条对照 spec §6.2（前缀 = 租户 schema 名）。"""
        self.assertEqual(
            build_order_key("demo", "2026092500001", "JPG", random_part="abc123"),
            "demo/orders/2026092500001/abc123.jpg",
        )
        # 群聊二维码用同一函数的 `kind="qr"`（spec §6.2 第三行）。
        self.assertEqual(
            build_order_key("demo", "2026092500001", "png", kind="qr", random_part="r1"),
            "demo/orders/2026092500001/qr.r1.png",
        )
        self.assertEqual(
            build_user_key("demo", 7, "avatar", "png", random_part="r1"),
            "demo/users/7/avatar.r1.png",
        )
        self.assertEqual(
            build_user_key("demo", 7, "wechat-qr", "jpeg", random_part="r2"),
            "demo/users/7/wechat-qr.r2.jpeg",
        )
        # ⭐ 问题反馈线（spec `2026-10-08-feedback-design.md` §1.3）：
        #    反馈截图**复用** `<租户>/users/<uid>/` 这条前缀，⛔ 不新造 `<租户>/feedback/…`
        #    ——新前缀会让 `key_media_kind` 判成 `"order"`（18 MiB），与认领侧的 10 MB 分叉。
        self.assertEqual(
            build_user_key("demo", 7, "feedback", "png", random_part="r3"),
            "demo/users/7/feedback.r3.png",
        )

    def test_key_builders_generate_a_fresh_random_part_by_default(self):
        """不传 `random_part` 时**每次不同**：对象名不能固定，否则浏览器缓存住旧图（spec §6.2）。"""
        first = build_order_key("demo", "2026092500001", "jpg")
        second = build_order_key("demo", "2026092500001", "jpg")
        self.assertNotEqual(first, second)
        # 形状钉住：`uuid.uuid4().hex[:12]` ⇒ 12 位小写十六进制
        for key in (first, second):
            self.assertRegex(key, r"^demo/orders/2026092500001/[0-9a-f]{12}\.jpg$")
        self.assertRegex(
            build_user_key("demo", 7, "avatar", "png"),
            r"^demo/users/7/avatar\.[0-9a-f]{12}\.png$",
        )
        # ⭐ 问题反馈线：第三种 kind 的随机段形状与另两种同源。
        self.assertRegex(
            build_user_key("demo", 7, "feedback", "jpg"),
            r"^demo/users/7/feedback\.[0-9a-f]{12}\.jpg$",
        )
        # ⭐ 首页二维码横幅线（D-0262）：第四种 kind 同一条前缀口径
        #    （⛔ 新造前缀会让 `key_media_kind` 判成 `"order"`、上限口径分叉）。
        self.assertEqual(
            build_user_key("demo", 7, "home-qr", "png", random_part="r4"),
            "demo/users/7/home-qr.r4.png",
        )
        self.assertRegex(
            build_user_key("demo", 7, "home-qr", "jpg"),
            r"^demo/users/7/home-qr\.[0-9a-f]{12}\.jpg$",
        )

    def test_head_infers_content_type_from_the_extension(self):
        """本机 `head` 从扩展名推内容类型（`mimetypes`）——`.jpg/.jpeg/.png` 是对标允许的三种。

        ⚠️ 这条是 Step 1 那条 `head(key) == (5, "image/jpeg")` 的**机制钉**：那条用例在
        `put_bytes` 传了 `content_type`，但本机实现**不存**它，靠的是扩展名。
        """
        st = get_storage()
        st.put_bytes("demo/users/7/avatar.r1.png", b"x")
        self.assertEqual(st.head("demo/users/7/avatar.r1.png"), (1, "image/png"))
        # 认不出的后缀回空串（`.qqq` 不在 `mimetypes` 的映射里；⚠️ 别拿 `.bin` 当未知——
        # 它在 `mimetypes.types_map` 里就是 `application/octet-stream`，断言会错）
        st.put_bytes("demo/orders/1/x.qqq", b"xx")
        self.assertEqual(st.head("demo/orders/1/x.qqq"), (2, ""))

    def test_allowed_exts_are_all_inferable_as_images(self):
        """`ALLOWED_EXTS` 的每一项都必须能被 `mimetypes` 推成 `image/*`。

        ⚠️ 这条**真的引用 `ALLOWED_EXTS`**（原先那条用例的注释声称「换掉 `ALLOWED_EXTS` 会先红」，
        但它只断言 `mimetypes.guess_type` 的结果、全文不引用该常量 ⇒ 那是个**假保证**）。
        判据钉的是**外部契约**（`mimetypes` 的内置映射），不是自指：
        把 `ALLOWED_EXTS` 里任一项换成 `mimetypes` 认不出图片的后缀（如 `txt`）⇒ 这条会红。

        ⚠️ 它**不**保证「新增一个 `mimetypes` 认得的图片后缀也会红」——那种改动是合法扩展，
        本条按设计放行；允许后缀的**准入**决策不在这个常量上。
        """
        for ext in ALLOWED_EXTS:
            with self.subTest(ext=ext):
                self.assertEqual(
                    mimetypes.guess_type(f"probe.{ext}")[0],
                    {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}[ext],
                )

    def test_key_belongs_to_tenant_normalizes_before_comparing(self):
        """归一化判据（Task 1 评审 Critical 的根因）：**任何 `..` 一律拒**，再要求首段 == tenant。"""
        self.assertTrue(key_belongs_to_tenant("demo/orders/1/x.jpg", "demo"))
        # ⛔ 下面这些形状，字符串 `key.startswith("demo/")` 或 `parts[0] == "demo"` 都会放行
        for bad in (
            "demo/../victim/orders/x.jpg",
            "demo/./../victim/x.jpg",
            "demo/orders/../../victim/x.jpg",
            "demo//../victim/x.jpg",
        ):
            self.assertFalse(key_belongs_to_tenant(bad, "demo"), bad)
        # 别的租户 / 空 / 只有点 / 前缀相近但不是本租户
        for bad in ("victim/orders/x.jpg", "", ".", "/", "demo-evil/x.jpg"):
            self.assertFalse(key_belongs_to_tenant(bad, "demo"), bad)

    def test_dotdot_escaped_prefix_is_refused_by_the_storage_layer(self):
        """⛔ 回归钉子（Task 1 评审 Critical）：`<租户>/../<别的租户>/x` 在**存储层**就必须拒。

        `resolve()` 之后它落在 `<root>/<别的租户>/x`——**仍在 root 之下**，所以只做
        「解析后仍在 root 之下」会放行它（这正是评审实测「两道闸同时过」的形状）。
        """
        st = get_storage()
        for bad in (
            "demo/../victim/orders/x.jpg",
            "demo/../../evil.jpg",
            "demo/orders/../../victim/x.jpg",
        ):
            with self.assertRaises(ValueError, msg=f"put_bytes 未拦住：{bad}"):
                st.put_bytes(bad, b"x", content_type="image/jpeg")
            with self.assertRaises(ValueError, msg=f"delete 未拦住：{bad}"):
                st.delete(bad)
            with self.assertRaises(ValueError, msg=f"head 未拦住：{bad}"):
                st.head(bad)


    def test_path_traversal_is_refused(self):
        """目录穿越必须在存储层被拒（端点之外也可能有人直接调）。"""
        st = get_storage()
        with self.assertRaises(ValueError):
            st.put_bytes("../escape.jpg", b"x", content_type="image/jpeg")
        with self.assertRaises(ValueError):
            st.head("/etc/hosts")
        with self.assertRaises(ValueError):
            st.delete("../../escape.jpg")

    def test_presign_points_at_the_local_blob_endpoint(self):
        """本机模式的 presign 换成本仓自己的端点（spec §6.1）。"""
        st = get_storage()
        key = build_order_key("demo", "2026092500001", "jpg")
        put = st.presign_put(key)
        self.assertEqual(put, Presigned(url=f"/api/media/blob/{key}", expires_in=600))
        # 读链接 TTL 更短（spec §6.1：短时效看图链接），且是**另一个**默认值
        self.assertEqual(st.presign_get(key), Presigned(url=f"/api/media/blob/{key}", expires_in=300))

    def test_default_backend_is_local(self):
        self.assertIsInstance(get_storage(), LocalStorage)


class CosStorageSelectionTests(SimpleTestCase):
    """后端开关的判据：`MEDIA_STORAGE_BACKEND=cos` ⇒ `CosStorage`，且**不联网**。

    ⚠️ 用 `mock.patch("qcloud_cos.CosConfig")` / `("qcloud_cos.CosS3Client")`：桩必须打在
    **被查找的那个模块**上（`cos-python-sdk-v5` 的顶层名字，见 `AGENTS.md` §5-E 的 E-0029）。
    `CosStorage.__init__` 里的 `from qcloud_cos import ...` 正是在那里查的。
    `assert_called_once_with(...)` 本身兼作「桩确实接上了」的判据（不接上时构造会真联网）。
    """

    COS_SETTINGS = dict(
        MEDIA_STORAGE_BACKEND="cos",
        COS_BUCKET="tt-cos-media-0000000000",   # 合成桶名（形状同真实：<名字>-<10 位 APPID>）
        COS_REGION="ap-test",
        COS_SECRET_ID="ak-placeholder",
        COS_SECRET_KEY="sk-placeholder",
    )

    @override_settings(**COS_SETTINGS)
    def test_get_storage_returns_cos_backend(self):
        with (
            mock.patch("qcloud_cos.CosConfig") as cfg_cls,
            mock.patch("qcloud_cos.CosS3Client") as client_cls,
        ):
            st = get_storage()

        self.assertIsInstance(st, CosStorage)
        self.assertEqual(st.bucket, "tt-cos-media-0000000000")
        cfg_cls.assert_called_once_with(
            Region="ap-test", SecretId="ak-placeholder", SecretKey="sk-placeholder"
        )
        client_cls.assert_called_once_with(cfg_cls.return_value)

    @override_settings(**COS_SETTINGS)
    def test_presign_put_delegates_signing_to_the_sdk(self):
        """签名一律交给 SDK（⛔ 不手搓）；上传签名照旧**短效直通**（时效语义本线未动）。

        ⚠️ 由既有用例 `test_presign_delegates_signing_to_the_sdk` 改名换向而来：它原钉
        GET `Expired=120` 直通——那是本线（URL 稳定化，业务方 2026-11-09）**有意废除**的
        短时效行为 ⇒ GET 一侧的判据改由 `CosStableGetUrlTests` 的对齐算式各条承接。
        """
        with (
            mock.patch("qcloud_cos.CosConfig"),
            mock.patch("qcloud_cos.CosS3Client") as client_cls,
        ):
            st = CosStorage()
        fake = client_cls.return_value
        fake.get_presigned_url.return_value = "https://example.invalid/signed"

        got = st.presign_put("demo/orders/2026092500001/abc.jpg", expires_in=120)

        self.assertEqual(
            got, Presigned(url="https://example.invalid/signed", expires_in=120)
        )
        fake.get_presigned_url.assert_called_once_with(
            Method="PUT",
            Bucket="tt-cos-media-0000000000",
            Key="demo/orders/2026092500001/abc.jpg",
            Expired=120,
        )


class CosStableGetUrlTests(SimpleTestCase):
    """读链接稳定化 + 上传签入缓存头（URL 稳定化线，业务方 2026-11-09 拍板）。

    三条承重契约：① 同一小时桶内同 key **逐字节同 URL**（同桶复用第一次签名——只对齐
    过期时刻不够：SDK 把签发时刻也算进签名）；② 过期时刻 = 本小时起点 + 7200（⇒ 有效期
    最短约 1 小时、最长 2 小时）；③ `cache_control=True` 时 `Cache-Control` 逐字面值签入。
    ⚠️ 桩照 `CosStorageSelectionTests` 的手法打在 `qcloud_cos` 顶层名字上（E-0029）；
    冻时间打在 `apps.core.storage.time.time`（cos_auth 的 `int(time.time())` 同源，一并定住）。
    """

    #: 一个「整点」的假纪元：`bucket=100` 的第 1800 秒（10:30:00 的形状）。
    HOUR = 3600
    T_HALF = 100 * HOUR + 1800

    def setUp(self):
        super().setUp()
        _reset_stable_get_urls()  # 模块级桶缓存在用例间必须隔离
        patcher = override_settings(**CosStorageSelectionTests.COS_SETTINGS)
        patcher.enable()
        self.addCleanup(patcher.disable)
        cfg_patcher = mock.patch("qcloud_cos.CosConfig")
        cfg_patcher.start()
        self.addCleanup(cfg_patcher.stop)
        client_patcher = mock.patch("qcloud_cos.CosS3Client")
        self.fake_cls = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.fake = self.fake_cls.return_value

    def _cos(self) -> CosStorage:
        return CosStorage()

    def _freeze(self, now: int) -> None:
        patcher = mock.patch("apps.core.storage.time.time", return_value=now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_key_within_the_same_hour_is_byte_identical_and_signs_once(self):
        """同桶两次 `presign_get` ⇒ **逐字节相等**，且 SDK 只被调**一次**（复用签名，非巧合相等）。"""
        self._freeze(self.T_HALF)
        # side_effect 每次调用都产出**不同**的串 ⇒ 若第二次重新签名，断言必红（排除假绿）。
        counter = {"n": 0}

        def _sign(**kw):
            counter["n"] += 1
            return f"https://cos.example.invalid/signed-{counter['n']}"

        self.fake.get_presigned_url.side_effect = _sign

        first = self._cos().presign_get("demo/orders/2026092500001/abc.jpg").url
        second = self._cos().presign_get("demo/orders/2026092500001/abc.jpg").url

        self.assertEqual(first, second)
        self.assertEqual(first, "https://cos.example.invalid/signed-1")
        self.assertEqual(self.fake.get_presigned_url.call_count, 1)

    def test_different_keys_sign_separately(self):
        self._freeze(self.T_HALF)
        self.fake.get_presigned_url.side_effect = (
            lambda **kw: f"https://cos.example.invalid/{kw['Key']}"
        )
        st = self._cos()
        a = st.presign_get("demo/orders/1/a.jpg").url
        b = st.presign_get("demo/orders/1/b.jpg").url
        self.assertNotEqual(a, b)
        self.assertEqual(self.fake.get_presigned_url.call_count, 2)

    def test_crossing_the_hour_boundary_signs_a_new_url(self):
        """跨整点 ⇒ 新桶清缓存重新签名：URL 变化，且新签的时效按**新桶**对齐。"""
        self.fake.get_presigned_url.side_effect = (
            lambda **kw: f"https://cos.example.invalid/s?Expired={kw['Expired']}"
        )
        st = self._cos()
        self._freeze(self.T_HALF)
        before = st.presign_get("demo/orders/2026092500001/abc.jpg").url
        self._freeze(101 * self.HOUR + 300)  # 下一小时的第 300 秒
        after = st.presign_get("demo/orders/2026092500001/abc.jpg").url
        self.assertNotEqual(before, after)
        self.assertEqual(self.fake.get_presigned_url.call_count, 2)
        # 旧桶：过期时刻 = (100+2)*3600 ⇒ Expired = 7200-1800；新桶：(101+2)*3600 ⇒ 7200-300
        self.assertIn("Expired=5400", before)
        self.assertIn("Expired=6900", after)

    def test_expiry_aligns_to_the_current_hour_plus_two(self):
        """对齐算式逐档钉住：桶内 :00 / :30 / :59:59 三档，Expired 都指向「本小时起点 + 7200」。"""
        self.fake.get_presigned_url.side_effect = (
            lambda **kw: f"https://cos.example.invalid/s?Expired={kw['Expired']}"
        )
        st = self._cos()
        for offset in (0, 1800, 3599):
            with self.subTest(offset=offset):
                _reset_stable_get_urls()
                self._freeze(100 * self.HOUR + offset)
                got = st.presign_get(f"demo/orders/2026092500001/off{offset}.jpg")
                # 过期时刻恒为本小时起点 + 7200 ⇒ Expired = 7200 - offset（3601 ≤ Expired ≤ 7200）
                self.assertEqual(
                    got.url, f"https://cos.example.invalid/s?Expired={7200 - offset}"
                )
                # `Presigned.expires_in` 如实报**剩余**有效期
                self.assertEqual(got.expires_in, 7200 - offset)

    def test_presign_put_with_cache_control_signs_the_header_verbatim(self):
        """`cache_control=True` ⇒ `Cache-Control` **逐字面值**签进签名（头值必须与直传方一致）。"""
        self._cos().presign_put("demo/orders/2026092500001/abc.jpg", cache_control=True)
        self.fake.get_presigned_url.assert_called_once_with(
            Method="PUT",
            Bucket="tt-cos-media-0000000000",
            Key="demo/orders/2026092500001/abc.jpg",
            Expired=600,
            Headers={"Cache-Control": "public, max-age=86400"},
        )

    def test_presign_put_without_the_flag_signs_no_headers(self):
        """默认（`cache_control=False`）**不带** Headers 形参：`assert_called_once_with` 的
        全量匹配兼作「没多签头」的判据——开放平台 / 打手 App 机器客户端的旧契约靠它保住。"""
        self._cos().presign_put("demo/orders/2026092500001/abc.jpg")
        self.fake.get_presigned_url.assert_called_once_with(
            Method="PUT",
            Bucket="tt-cos-media-0000000000",
            Key="demo/orders/2026092500001/abc.jpg",
            Expired=600,
        )

    def test_preview_signs_private_revalidation_headers(self):
        now = self.T_HALF + 123
        self._freeze(now)
        self.fake.get_presigned_url.return_value = "https://cos.example.invalid/preview"
        result = self._cos().presign_get("demo/games/1/logo.png", cache_preview=True)
        params = self.fake.get_presigned_url.call_args.kwargs["Params"]
        self.assertEqual(params, {"response-cache-control": "private, no-cache"})
        self.assertEqual(result.expires_in, 7200 - 1923)

    def test_preview_url_reuses_across_five_minutes_and_changes_at_hour(self):
        self.fake.get_presigned_url.side_effect = ["https://cos.invalid/first", "https://cos.invalid/second"]
        st = self._cos()
        self._freeze(self.T_HALF + 1)
        first = st.presign_get("demo/games/1/logo.png", cache_preview=True).url
        self._freeze(self.T_HALF + 299)
        self.assertEqual(st.presign_get("demo/games/1/logo.png", cache_preview=True).url, first)
        self._freeze(self.T_HALF + 600)
        self.assertEqual(st.presign_get("demo/games/1/logo.png", cache_preview=True).url, first)
        self._freeze(101 * self.HOUR + 1)
        self.assertEqual(st.presign_get("demo/games/1/logo.png", cache_preview=True).url, "https://cos.invalid/second")
        self.assertEqual(self.fake.get_presigned_url.call_count, 2)

    def test_preview_and_original_do_not_share_the_signature_cache(self):
        self._freeze(self.T_HALF + 1)
        self.fake.get_presigned_url.side_effect = ["https://cos.invalid/original", "https://cos.invalid/private"]
        st = self._cos()
        key = "demo/users/1/home-qr.synthetic.png"
        self.assertEqual(st.presign_get(key).url, "https://cos.invalid/original")
        self.assertEqual(st.presign_get(key, cache_preview=True).url, "https://cos.invalid/private")
        self.assertEqual(self.fake.get_presigned_url.call_count, 2)
        self.assertNotIn("Params", self.fake.get_presigned_url.call_args_list[0].kwargs)
        self.assertEqual(self.fake.get_presigned_url.call_args_list[1].kwargs["Params"]["response-cache-control"], "private, no-cache")


class AppendThumbnailQueryTests(SimpleTestCase):
    """列表/大厅缩图参数（数据万象 CI，业务方 2026-11-09）的拼接形状。

    ⛔ `format/jpg` 必带（CI 默认重编码会把小图反而变大，实测 4.7KB→24KB）——字面值钉死。
    """

    def test_cos_signed_url_joins_with_ampersand(self):
        url = append_thumbnail_query("https://cos.example.invalid/b/k.jpg?q-sign-algorithm=sha1")
        self.assertTrue(url.endswith("&imageMogr2/thumbnail/300x/format/jpg/rquality/80"))

    def test_local_blob_url_joins_with_question_mark(self):
        url = append_thumbnail_query("/api/media/blob/demo/orders/1/a.jpg")
        self.assertEqual(
            url, "/api/media/blob/demo/orders/1/a.jpg?imageMogr2/thumbnail/300x/format/jpg/rquality/80"
        )

    def test_empty_url_stays_empty(self):
        """「签不出回空串」的回落口径不被拼出一个指不动的参数串。"""
        self.assertEqual(append_thumbnail_query(""), "")

    def test_query_literals_are_pinned(self):
        """参数逐段钉死：缩到 300x、强制 jpg、质量 80——CI 行为由这三个值共同决定。"""
        self.assertEqual(
            THUMBNAIL_QUERY, "imageMogr2/thumbnail/300x/format/jpg/rquality/80"
        )

    def test_game_logo_thumbnail_preserves_format_and_has_failure_fallback(self):
        result = append_game_logo_thumbnail_query("https://cos.example.invalid/logo.png?sig=x")
        self.assertEqual(result, "https://cos.example.invalid/logo.png?sig=x&imageMogr2/thumbnail/300x300%3E/ignore-error/1")
        self.assertNotIn("format/jpg", result)
        self.assertEqual(append_game_logo_thumbnail_query(""), "")


class PreviewRealSdkSigningTests(SimpleTestCase):
    def test_private_revalidation_parameter_is_covered_by_the_real_sdk_signature(self):
        _reset_stable_get_urls()
        self.addCleanup(_reset_stable_get_urls)
        with override_settings(**CosStorageSelectionTests.COS_SETTINGS):
            with mock.patch("apps.core.storage.time.time", return_value=361923):
                url = CosStorage().presign_get("demo/games/1/logo.synthetic.png", cache_preview=True).url
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["response-cache-control"], ["private, no-cache"])
        self.assertIn("response-cache-control", query["q-url-param-list"][0])


class CacheControlWiringTests(SimpleTestCase):
    """「哪个取票端点给直传方签缓存头」的**结构性**判据（本机 LocalStorage 测不出 COS 签名）。

    承重：**已装 App 包改不了代码**——头像取票与订单截图取票被 `app/utils/upload.uts`
    （只带 `Content-Type`，COS 分支 ⛔ 不带我们的头）直传，签了必 403 ⇒ 这两处（连同
    开放平台 / 打手 App / 查码队列）**恒不签**；只给直传方是本仓**网页前端**的四个取票点开
    `cache_control=True`（`directUpload.ts` 统一带同值头）。App 发新包后再开 = 后续拍板项。
    """

    def _src(self, relpath: pathlib.Path) -> str:
        return (APPS_ROOT / relpath).read_text()

    def test_web_only_ticket_sites_opt_in(self):
        """只给**没有 App 消费方**的四个 web 取票点签缓存头。"""
        for relpath in (
            pathlib.Path("business/api") / "feedback_views.py",
            pathlib.Path("business/api") / "game_logo_views.py",
            pathlib.Path("business/api") / "article_views.py",
            pathlib.Path("business/api") / "qr_code_views.py",
        ):
            with self.subTest(site=str(relpath)):
                self.assertIn(
                    "presign_put(key, cache_control=True)", self._src(relpath)
                )

    def test_app_and_machine_client_sites_never_sign_cache_control(self):
        """头像与订单截图取票点被**已装 App 包**直传（`app/utils/upload.uts` 只发
        Content-Type）⇒ 恒不签；开放平台 / 打手 App 的机器客户端同口径。"""
        for relpath in (
            pathlib.Path("core") / "profile_views.py",
            pathlib.Path("business/api") / "screenshot_views.py",
            pathlib.Path("openapi/api") / "order_image_views.py",
            pathlib.Path("takerapi/api") / "order_images_views.py",
        ):
            with self.subTest(site=str(relpath)):
                self.assertNotIn("cache_control", self._src(relpath))


class MediaBlobEndpointTests(_TempMediaRootMixin, TenantTestCase):
    """本机 blob 端点：读写闭环 + **前缀必须是本租户**的同一道闸。

    ⚠️ 端点只在 `MEDIA_STORAGE_BACKEND=local` 时登记（`apps/core/urls.py`），
    本机/CI 的默认值就是 `local`（`.env`），而测试又跑不了云凭据 ⇒ 本类恒跑。
    """

    def setUp(self):
        super().setUp()
        self.client = make_logged_in_client(domain=TENANT_DOMAIN)
        # 本租户标识取自**真实租户上下文**（django-tenants 的路由结果），不硬编码 schema 名。
        self.schema = connection.schema_name

    def _url(self, key: str) -> str:
        return f"/api/media/blob/{key}"

    def _own_key(self, name: str = "abc123.jpg") -> str:
        return f"{self.schema}/orders/2026092500001/{name}"

    def test_put_then_get_roundtrip(self):
        key = self._own_key()
        response = self.client.put(self._url(key), data=b"hello", content_type="image/jpeg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_storage().head(key), (5, "image/jpeg"))

        response = self.client.get(self._url(key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"hello")

    def test_put_refuses_another_tenants_prefix_and_writes_nothing(self):
        key = "another-tenant/orders/2026092500001/abc123.jpg"
        response = self.client.put(self._url(key), data=b"hello", content_type="image/jpeg")
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(get_storage().head(key))

    def test_get_refuses_another_tenants_prefix(self):
        key = "another-tenant/orders/2026092500001/abc123.jpg"
        get_storage().put_bytes(key, b"secret", content_type="image/jpeg")
        response = self.client.get(self._url(key))
        self.assertEqual(response.status_code, 403)

    # ── ⛔ 回归钉子：`..` 绕出本租户命名空间 ──────────────
    def _dotdot_escaped_other_tenant_key(self) -> str:
        """`<本租户>/../<别的租户>/…`：首段**确实是**本租户 ⇒ 旧字符串判据会放行。"""
        return "/".join(
            [self.schema, "..", "another-tenant", "orders", "2026092500001", "abc123.jpg"]
        )

    def test_put_refuses_dotdot_escaped_other_tenant_prefix_and_writes_nothing(self):
        """`<本租户>/../<别的租户>/…` ⇒ **403**，且磁盘上**不留文件**。

        ⚠️ 这条在旧判据（`key.startswith(f"{schema}/")`）下会返回 200 并把文件写进
        `another-tenant/` 的命名空间（评审实测）⇒ 它是本 Critical 的写侧钉子。
        """
        escaped = self._dotdot_escaped_other_tenant_key()
        response = self.client.put(self._url(escaped), data=b"hello", content_type="image/jpeg")

        self.assertEqual(response.status_code, 403)
        # ① 别的租户的命名空间里不得出现这个对象
        self.assertIsNone(
            get_storage().head("another-tenant/orders/2026092500001/abc123.jpg")
        )
        # ② 连目录都不许被建出来（`put_bytes` 会 `mkdir(parents=True)`）
        self.assertFalse(
            (pathlib.Path(settings.MEDIA_ROOT).resolve() / "another-tenant").exists(),
            "目录穿越把文件写进了别的租户命名空间",
        )

    def test_get_refuses_dotdot_escaped_other_tenant_prefix(self):
        """读侧同型钉子：别的租户的文件**真的存在**，`..` 绕过的 GET 也不许把它读出来。

        ⚠️ 旧判据下这条会 200 + 返回 `secret` ⇒ 跨租户读泄漏。文件是**真放进去的**
        （走存储层、不经端点），所以这条不是「文件不存在所以 404」的假绿。
        """
        victim_key = "another-tenant/orders/2026092500001/abc123.jpg"
        get_storage().put_bytes(victim_key, b"secret", content_type="image/jpeg")
        self.assertIsNotNone(get_storage().head(victim_key))   # 自证受害文件确实在

        escaped = self._dotdot_escaped_other_tenant_key()
        response = self.client.get(self._url(escaped))

        self.assertEqual(response.status_code, 403)

    def test_dotdot_out_of_root_is_403_not_500(self):
        """越出 `MEDIA_ROOT` 的形状 ⇒ **403 而不是 500**（预期失败必须翻成业务响应）。

        ⚠️ 两处 `..` 在归一化判据里已被拒 ⇒ 走的是**前缀闸**那一支；
        真正由「存储层抛 `ValueError`」兜成 403 的那一支另见
        `test_absolute_path_key_is_refused_by_the_storage_layer_as_403`。
        """
        escaped = "/".join([self.schema, "..", "..", "..", "evil.jpg"])
        put = self.client.put(self._url(escaped), data=b"hello", content_type="image/jpeg")
        get = self.client.get(self._url(escaped))
        self.assertEqual(put.status_code, 403)
        self.assertEqual(get.status_code, 403)

    def test_absolute_path_key_is_refused_by_the_storage_layer_as_403(self):
        """`ValueError` → 403 的**翻译**必须真的被走到（⛔ 不许漏成 500）。

        `<path:key>` 允许 key 以 `/` 开头（`key_belongs_to_tenant` 会剔除空段，
        故首段仍是本租户、**过得了前缀闸**），而 `LocalStorage._path` 会把
        `root / "/…"` 解析成绝对路径 ⇒ 抛 `ValueError` ⇒ 由 `except ValueError` 兜成 403。
        没有这条用例的话，两处 `except ValueError` 就是**没人守的承重逻辑**。
        """
        key = f"/{self.schema}/orders/2026092500001/abc123.jpg"
        self.assertTrue(
            self._view_tenant_gate(key), "夹具没造对：这条 key 必须先过得了前缀闸"
        )
        put = self.client.put(self._url(key), data=b"hello", content_type="image/jpeg")
        get = self.client.get(self._url(key))
        self.assertEqual(put.status_code, 403)
        self.assertEqual(get.status_code, 403)

    @staticmethod
    def _view_tenant_gate(key: str) -> bool:
        return key_belongs_to_tenant(key, connection.schema_name)

    # ── key 指到的**路径形状**不是可写文件 ⇒ 403，⛔ 不是 500 ──────────
    def _put_ignoring_exceptions(self, key: str, body: bytes = b"hello"):
        """发一次 PUT，并让**响应码**（而不是冒出来的异常）承载结果。

        ⚠️ Django 测试客户端默认 `raise_request_exception=True`（未捕获异常直接 re-raise）
        ⇒ 断言会以 ERROR 收场、看不到 500。这里临时关掉它，让「不是 500」这条断言
        以 **FAIL** 的形式红（`AGENTS.md` §5-G：变异必须由目标用例自己的失败行判定）。
        """
        self.client.raise_request_exception = False
        try:
            return self.client.put(self._url(key), data=body, content_type="image/jpeg")
        finally:
            self.client.raise_request_exception = True

    def test_put_pointing_at_an_existing_directory_is_403_not_500(self):
        """key 正好指着**已存在的目录** ⇒ **403**（`IsADirectoryError` 被翻译掉）。

        ⚠️ 评审探针的形状：先正常写 `<schema>/orders/seed/x.jpg`（`put_bytes` 会
        `mkdir(parents=True)`，于是 `…/orders/seed` 是一个**目录**），再 PUT 那个目录本身。
        `Path.write_bytes` 抛 `IsADirectoryError`——它**不是** `ValueError`
        ⇒ 只捕 `ValueError` 时这里漏成 **500**（模块 docstring 原先承诺「非法对象名一律
        403、不漏 500」，那是**过度承诺**，本用例是那条承诺的判据）。
        """
        seed_key = f"{self.schema}/orders/seed/x.jpg"
        get_storage().put_bytes(seed_key, b"seed", content_type="image/jpeg")
        # 自证夹具造对了：`seed` 这个路径确实是一个**目录**（否则下面测的是别的东西）
        self.assertTrue(
            (pathlib.Path(settings.MEDIA_ROOT) / self.schema / "orders" / "seed").is_dir()
        )

        response = self._put_ignoring_exceptions(f"{self.schema}/orders/seed")

        self.assertEqual(response.status_code, 403)
        # 目录里那张图原样不动（403 不是「先删再炸」）
        self.assertEqual(get_storage().head(seed_key), (4, "image/jpeg"))

    def test_put_under_a_prefix_that_is_a_file_is_403_not_500(self):
        """key 的**某一段前缀是一个文件** ⇒ 403（`NotADirectoryError` 被翻译掉）。

        ⚠️ 评审探针的第二个形状：PUT `<schema>/users`（目录还不存在）会**建出一个叫
        `users` 的文件**并回 200（本任务**不改**这条既有行为）；之后再 PUT
        `<schema>/users/1/a.jpg`，`p.parent.mkdir(parents=True)` 就撞上那个文件
        ⇒ `NotADirectoryError` ⇒ 只捕 `ValueError` 时是 500。
        """
        prefix_key = f"{self.schema}/users"
        first = self.client.put(self._url(prefix_key), data=b"x", content_type="image/jpeg")
        self.assertEqual(first.status_code, 200)                    # 既有行为，本用例只钉住它
        # 自证夹具造对了：那一跳确实建出了一个**文件**（不是目录）
        self.assertTrue((pathlib.Path(settings.MEDIA_ROOT) / self.schema / "users").is_file())

        response = self._put_ignoring_exceptions(f"{self.schema}/users/1/a.jpg")

        self.assertEqual(response.status_code, 403)

    def test_an_unexpected_os_error_still_500s(self):
        """⛔ **不该吞的异常仍要响亮地 500**：磁盘满 / 权限被拒这类基础设施故障不是 403。

        ⚠️ 这条是上面两条翻译钉子的**边界**：允许翻成 403 的只有「路径形状」那三种
        （`ValueError` / `IsADirectoryError` / `NotADirectoryError`）。把 `OSError` 整族
        吞成 403 会让监控失明（`AGENTS.md` §5-E「未预期的错误仍应响亮」）。
        桩打在**被查找的那个模块**上（视图里是 `get_storage().put_bytes` ⇒ 类属性，E-0029）。
        """
        with mock.patch.object(
            LocalStorage, "put_bytes", side_effect=PermissionError("权限被拒")
        ):
            response = self._put_ignoring_exceptions(self._own_key("boom.jpg"))
        self.assertEqual(response.status_code, 500)

    def test_get_missing_object_is_404(self):
        response = self.client.get(self._url(self._own_key("nope.jpg")))
        self.assertEqual(response.status_code, 404)

    def test_put_over_the_size_limit_is_413(self):
        """超限走 `Content-Length` **前置**判据（⛔ 不先把整份 body 读进内存再判）。"""
        with mock.patch("apps.core.media_blob_views.MAX_IMAGE_BYTES", 4):
            response = self.client.put(
                self._url(self._own_key()), data=b"hello", content_type="image/jpeg"
            )
        self.assertEqual(response.status_code, 413)
        self.assertIsNone(get_storage().head(self._own_key()))

    # ── ⛔ 回归钉子（本机形态）：读 `request.body` 不该被 Django 的 2.5 MB 默认墙拦住 ──
    #: 3 MiB 的**字面值**：**明显大于** Django 默认 `DATA_UPLOAD_MAX_MEMORY_SIZE`（2.5 MB），
    #: 又远小于本仓 18 MiB 上限 ⇒ 用来钉住「上限之内的大体必须放行」。⛔ 不用 18 MiB（测试要快）。
    THREE_MIB = 3 * 1024 * 1024

    def test_put_three_mib_within_limit_is_accepted_and_lands_on_disk(self):
        """3 MiB（> Django 默认 2.5 MB 墙、< 本仓 18 MiB 上限）⇒ **200 且对象真的落盘**。

        ⚠️ 为什么需要这条判据：本机形态（`MEDIA_STORAGE_BACKEND=local`，开发 / CI 默认）下浏览器
        直传走这条路由，而它要读 `request.body`。`config/settings.py` 若**不显式**设置
        `DATA_UPLOAD_MAX_MEMORY_SIZE`，Django 默认 2.5 MB，`request.body` 对超出的体直接抛
        `RequestDataTooBig`（`django/http/request.py:419-425`）⇒ HTTP **400**，于是我们对外承诺的
        10 MB / 18 MiB 上限在本机形态下**根本走不到**。这条用例就是那道 setting 的钉子：
        把 `DATA_UPLOAD_MAX_MEMORY_SIZE` 那行删掉 ⇒ 本用例以 **400** 变红。

        ⚠️ 与超限侧的分工：真实超限仍是 **413**，由三条用例守着，各自钉一道判据——
        `test_put_over_the_size_limit_is_413`（第二道：`declared > limit`）、
        `test_put_without_content_length_is_refused_and_writes_nothing`（第一道：`declared <= 0`）、
        以及本文件里唯一一条钉**第三道**（读体后 `len(body) > limit` 兜底）的
        `test_put_understated_content_length_with_a_real_oversize_body_is_413`。
        """
        key = self._own_key("three-mib.jpg")
        body = b"\xff\xd8\xff" + b"0" * (self.THREE_MIB - 3)
        response = self.client.put(self._url(key), data=body, content_type="image/jpeg")
        self.assertEqual(response.status_code, 200)
        # 不只是 200：字节数真的落盘（`head` 返回 `(size, content_type)`）
        self.assertEqual(get_storage().head(key), (self.THREE_MIB, "image/jpeg"))

    def test_put_understated_content_length_with_a_real_oversize_body_is_413(self):
        """声明值**合法且不超限**、但读到的**真实体超限** ⇒ **413**（第三道：读体后按实际长度兜底）。

        ⚠️ **本波（复评 Minor）的现场**：本条原先号称测第三道，其实是靠
        「`RequestFactory` 造好请求后**删掉** `CONTENT_LENGTH`」——`_stream` 的 limit 是**造请求时**
        定下的（仍是 5 字节）⇒ 旧代码的 `declared <= 0` 前置拒收还不存在，于是它**意外地**打到了
        第三道。本波加了 `declared <= 0` 拒收后那条路径改打**第一道**（见下面
        `test_put_without_content_length_is_refused_and_writes_nothing`），第三道一度**零覆盖**。

        ⚠️ **形状 = `Content-Length` 少报**（声明 3 字节、真实体 10 字节）：这正是 Django
        `HttpRequest.body` 自己文档化的场景（`django/http/request.py` 的 `body` 属性：
        「Content-Length can be absent or **understated** (e.g. `Transfer-Encoding: chunked` on ASGI)」）
        ——ASGI 形态下 `_stream` 是**可 seek** 的临时文件，体长**不受** `Content-Length` 约束，
        视图读完整份体后再兜一次长度才拦得住。本仓生产是 **WSGI**（gunicorn，
        `WSGIRequest._stream` 是 `LimitedStream`）⇒ 真实流量里体长恒 ≤ 声明值，这一支生产上不可达；
        但视图是协议无关的（DRF `APIView`），所以这里用**与第一道用例同一个夹具手法**
        （`RequestFactory` 造请求后改 `META`）把这道**防御**钉住，⛔ 不是硬造一条假用例：
        我们只是把「声明值与真实体不一致」这个**被防御的违规形状**原样摆出来。

        ⚠️ **夹具自证**：先断言声明值与真实体一致、再把声明值**改小**并断言它确实变小 ⇒
        否则本用例会静默退化成「又测了一遍第二道」（`declared` 仍大于限额）。

        变异：删掉视图里 `if len(body) > limit:` 那两行 ⇒ 10 字节体被落盘并回 **200** ⇒
        本条以 `200 != 413` / `head() is not None` 变红（已实测）。
        """
        body = b"\xff\xd8\xff" + b"0" * 7                      # 10 字节
        request = RequestFactory().put(
            self._url(self._own_key()), data=body, content_type="image/jpeg"
        )
        # 自证夹具：先摆明「声明值 == 真实体」，再把声明值**少报**成 3（< 限额 4）
        self.assertEqual(request.META["CONTENT_LENGTH"], str(len(body)))
        self.assertEqual(len(body), 10)
        request.META["CONTENT_LENGTH"] = "3"
        # 自证：`declared` 既非缺失、也不超限 ⇒ 前两道判据都不会在这里拦
        self.assertEqual(request.META["CONTENT_LENGTH"], "3")
        force_authenticate(request, user=mock.Mock(is_authenticated=True))

        with mock.patch("apps.core.media_blob_views.MAX_IMAGE_BYTES", 4):
            response = MediaBlobView.as_view()(request, key=self._own_key())

        self.assertEqual(response.status_code, 413)
        self.assertIsNone(get_storage().head(self._own_key()))

    def test_put_without_content_length_is_refused_and_writes_nothing(self):
        """无 `Content-Length`（缺失 / 空串 / `0`）+ 有体 ⇒ **413，磁盘上不留对象**。

        ⚠️ **2026-09-25 复评 Important 的钉子**：WSGI（本仓生产形态，gunicorn）下没有声明值时
        `WSGIRequest` 的 `LimitedStream` limit=0（`django/core/handlers/wsgi.py`）⇒ `request.body`
        读出**空**、`RequestDataTooBig` **永远不会触发** ⇒ 修复前这里会 `put_bytes(key, b"")` 并回
        **200**，磁盘上真的落一个 **0 字节对象**（复评实测形状：`CONTENT_LENGTH=""` + 1000 字节体）。
        空图片永远不合法 ⇒ 现在回 **413**（与「超限」同码，本机路由既有口径）。

        ⚠️ **本波之后它专钉第一道判据**（`declared <= 0` 前置拒收）：本条与
        `test_put_understated_content_length_with_a_real_oversize_body_is_413`（第三道）分工不同，
        ⛔ 不再重复命中同一条（复评 Minor 订正前两条都打第一道，名称/分工名不副实）。
        这里额外把限额缩到 4 字节 ⇒ 证 `413` 与限额大小无关。

        ⚠️ **夹具自证 + 变异**：先在 `RequestFactory` 里带上声明值（`WSGIRequest` 的 `_stream` limit
        就按它建），再按三种缺失形状改 `META`；若删掉 `media_blob_views.put` 里那条
        `declared <= 0` 拒收，`request.body` 会读回那 1000 字节、落盘并回 **200** ⇒ 本条以
        `200 != 413` / `head() is not None` 变红（已实测，见任务报告）。
        """
        body = b"\xff\xd8\xff" + b"0" * (1000 - 3)
        for shape in ("missing", "empty-string", "zero"):
            with self.subTest(shape=shape):
                key = self._own_key(f"no-length-{shape}.jpg")
                request = RequestFactory().put(
                    self._url(key), data=body, content_type="image/jpeg"
                )
                # 自证夹具：声明值先真的存在（否则「缺失」不是我们造出来的）
                self.assertEqual(request.META["CONTENT_LENGTH"], str(len(body)))
                if shape == "missing":
                    del request.META["CONTENT_LENGTH"]
                elif shape == "empty-string":
                    request.META["CONTENT_LENGTH"] = ""
                else:
                    request.META["CONTENT_LENGTH"] = "0"
                force_authenticate(request, user=mock.Mock(is_authenticated=True))

                response = MediaBlobView.as_view()(request, key=key)

                self.assertEqual(response.status_code, 413)
                self.assertIsNone(get_storage().head(key))


    def test_anonymous_put_is_refused(self):
        key = self._own_key()
        response = Client(SERVER_NAME=TENANT_DOMAIN).put(
            self._url(key), data=b"hello", content_type="image/jpeg"
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertIsNone(get_storage().head(key))

    # ── 业务方裁定：同一道闸按 **key 的种类**取上限 ──────────────────
    #: 真实大小的**字面量**（⛔ 不引用 `MAX_USER_MEDIA_BYTES` / `MAX_IMAGE_BYTES`：
    #: 两侧同源的话，把常量改坏这些用例仍绿——本仓「自指断言」的教训）。
    TEN_MIB = 10 * 1024 * 1024

    def _user_key(self, name: str = "avatar.abc123.jpg") -> str:
        return f"{self.schema}/users/7/{name}"

    def test_put_refuses_user_media_one_byte_over_10mb(self):
        """真实的 10 MiB + 1 字节 ⇒ **413**（声明值就超限 ⇒ 连体都不读）。

        ⚠️ 「恰好 10 MiB 放行」那一侧**不在本文件**（这条 HTTP 路由只有 key、没有别的上下文，
        它是**非权威**的那道闸）⇒ 真实大小的放行判据放在两条**权威**路径上：
        `test_my_profile_media.ProfileMediaSizeLimitTests`（10 MiB 整）与
        `test_order_screenshots_api.OrderScreenshotLimitTests`（12 MiB 的订单截图）。
        ⚠️ 本机形态下这条 HTTP 路由**现在也读得动** 10 MiB 级的体（见
        `test_put_three_mib_within_limit_is_accepted_and_lands_on_disk` 与 `settings.py` 的
        `DATA_UPLOAD_MAX_MEMORY_SIZE`）；这里不重复一条 10 MiB 的用例是为了**测试要快**，
        不是因为它走不到。
        """
        key = self._user_key("wechat-qr.abc123.png")
        body = b"\xff\xd8\xff" + b"0" * (self.TEN_MIB - 2)          # 10 MiB + 1 字节
        response = self.client.put(self._url(key), data=body, content_type="image/jpeg")
        self.assertEqual(response.status_code, 413)
        self.assertIsNone(get_storage().head(key))

    def test_the_limit_is_chosen_by_key_kind(self):
        """两支上限**取错方向**必须能红：同一请求体，只有 key 的种类不同。

        ⚠️ 把两条上限缩到 4 / 8 字节来判「走的是哪一条」——判据用的是**字面量字节数**
        （5 字节的体），⛔ 不是常量对常量：把两支写成同一个常量、或把种类判反，
        下面四条断言里必有一条红。
        """
        user_key, order_key = self._user_key(), self._own_key()
        body = b"12345"                                             # 5 字节：> 4 且 < 8
        with (
            mock.patch("apps.core.media_blob_views.MAX_USER_MEDIA_BYTES", 4),
            mock.patch("apps.core.media_blob_views.MAX_IMAGE_BYTES", 8),
        ):
            user = self.client.put(self._url(user_key), data=body, content_type="image/jpeg")
            order = self.client.put(self._url(order_key), data=body, content_type="image/jpeg")
        self.assertEqual(user.status_code, 413, "用户媒体应当按 10 MB 那条（这里缩到 4）")
        self.assertEqual(order.status_code, 200, "订单截图应当按 18 MiB 那条（这里缩到 8）")
        self.assertIsNone(get_storage().head(user_key))
        self.assertEqual(get_storage().head(order_key)[0], 5)

        # 反向：把两条上限**对调**，判据必须跟着翻 —— 这一半排除「碰巧都拒 / 都放」
        other_order_key = self._own_key("second.jpg")
        with (
            mock.patch("apps.core.media_blob_views.MAX_USER_MEDIA_BYTES", 8),
            mock.patch("apps.core.media_blob_views.MAX_IMAGE_BYTES", 4),
        ):
            user2 = self.client.put(self._url(user_key), data=body, content_type="image/jpeg")
            order2 = self.client.put(self._url(other_order_key), data=body, content_type="image/jpeg")
        self.assertEqual(user2.status_code, 200)
        self.assertEqual(order2.status_code, 413)


class BodySizeWallTests(_TempMediaRootMixin, TenantTestCase):
    """请求体墙（**E-0095**）：全局抬到 20 MiB 之后，**非直传路径**必须回到 2.5 MiB 的旧姿态。

    背景（独立评审 Important I-3）：`DATA_UPLOAD_MAX_MEMORY_SIZE` **没有 per-view
    形式** ⇒ 为放行本机直传把它抬到 20 MiB 时，**未认证**的 `POST /api/auth/login` 等**每个**
    读体端点也被一起抬高了（生产同理，nginx 本来就是 `client_max_body_size 20m`）。
    `apps.core.middleware.NonDirectUploadBodyLimitMiddleware` 负责把非直传路径收回去。

    五条判据（缺一不可）：
    ① 3 MiB 体打**非直传**路径（`POST /api/auth/login`）⇒ **400**；
    ② **对照**：同一条请求、只把中间件从 `MIDDLEWARE` 里去掉 ⇒ **变成 401**（体真的进了登录
       视图并被 `_login_failed()` 拒掉）——把「那个 400 是中间件给的」与别的东西（CSRF /
       json 解析）区分开；
    ③ **回归面**：3 MiB 体打**本机直传路由** ⇒ **200 且真的落盘**（没把上传一起拦掉）；
    ④ 小的正常请求（一次真实登录 + `GET /api/auth/me`）不受影响；
    ⑤ **子路径**（`SCRIPT_NAME=/tt`）下打直传路由 ⇒ 仍 **200**（豁免判据用 `request.path_info`，
       ⛔ 不是 `request.path`；2026-09-25 复评 Minor-2）。

    ⚠️ 残余（如实，不在这张网里）：**无 `Content-Length`**（分块传输）的请求不被**本中间件**拦
    （它只读声明值）。⚠️ 机制订正（复评）：**WSGI（本仓生产形态）**下无声明值时体会被
    读成**空**、`RequestDataTooBig` **永不触发** ⇒ 残余形状是「**静默空体**」，不是「仍受 20 MiB
    约束」（「按真实长度再判」只在 **ASGI** 成立）。理由写在 `config/settings.py` ④ 与中间件的
    docstring 里；本机直传那条路由另有前置拒收（缺声明值 ⇒ 413）。
    """

    #: 中间件的 dotted path（去掉它做对照用；⛔ 不 import 类本身——`MIDDLEWARE` 存的是字符串）
    MIDDLEWARE_PATH = "apps.core.middleware.NonDirectUploadBodyLimitMiddleware"

    #: 3 MiB 的**字面值**：> Django 出厂默认 2.5 MB（`global_settings.py` 的 `2621440`）、
    #: < 本仓 18 MiB ⇒ 正好落在「旧姿态拒、新姿态放」的那条缝里。
    THREE_MIB = 3 * 1024 * 1024

    def setUp(self):
        super().setUp()
        self.schema = connection.schema_name

    def _three_mib_login_body(self) -> bytes:
        """**合法 JSON** 的 3 MiB 体。

        ⚠️ 有意做成合法 JSON：否则「不是 400」可能是 JSON 解析错误给的，而不是「中间件不在」
        给的——那样对照就说明不了任何事（`AGENTS.md` §5-G 的自洽变异同族）。
        """
        body = b'{"username":"nobody","password":"' + b"0" * self.THREE_MIB + b'"}'
        self.assertGreater(len(body), self.THREE_MIB)      # 自证体确实超过 2.5 MB 的墙
        return body

    def test_oversized_body_on_a_non_direct_upload_route_is_400(self):
        response = Client(SERVER_NAME=TENANT_DOMAIN).post(
            "/api/auth/login",
            data=self._three_mib_login_body(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        # 空体：中间件没有读体、也没走 DRF 渲染（与 Django 因 `RequestDataTooBig` 给的 400 同姿态）
        self.assertEqual(response.content, b"")

    def test_the_same_request_is_not_400_without_the_middleware(self):
        """对照：**只去掉中间件**，同一请求不再是 400。

        ⚠️ 必须**新建** `Client`：中间件链由 `ClientHandler` **首次 `__call__`** 时才
        `load_middleware()` 并缓存进 `_middleware_chain`（`django/test/client.py`），
        `Client.__init__` 只造 handler、这时还没读 `settings.MIDDLEWARE` ⇒ 复用已经发过请求的
        client，`override_settings(MIDDLEWARE=…)` 不会重装链（结论不变：必须新建）。
        """
        without = [
            item for item in settings.MIDDLEWARE if item != self.MIDDLEWARE_PATH
        ]
        self.assertNotEqual(without, settings.MIDDLEWARE, "对照没生效：中间件还在 MIDDLEWARE 里")
        with override_settings(MIDDLEWARE=without):
            response = Client(SERVER_NAME=TENANT_DOMAIN).post(
                "/api/auth/login",
                data=self._three_mib_login_body(),
                content_type="application/json",
            )
        self.assertNotEqual(
            response.status_code, 400, f"去掉中间件后仍是 400 ⇒ 那个 400 不是它给的: {response}"
        )
        # 更强的一层：**401** 是 `_login_failed()` 的信封（`apps/core/auth_views.py`）⇒ 证明这条
        # 3 MiB 的体真的被读进视图、走到了业务逻辑（中间件不在时 Django 的 20 MiB 墙容得下它）。
        # ⛔ 不写成「只看不是 400」：那样「因为别的原因回 401」也会过。
        self.assertEqual(
            response.status_code, 401, f"对照的响应形状变了（应当走到登录视图并登录失败）: {response}"
        )

    def test_the_direct_upload_route_still_takes_three_mib_and_lands_on_disk(self):
        """回归面：本机直传（`/api/media/blob/` 前缀）**必须继续**吃得下 3 MiB 并落盘。"""
        client = make_logged_in_client(username="body-wall-uploader", domain=TENANT_DOMAIN)
        key = f"{self.schema}/orders/2026092500001/three-mib.jpg"
        body = b"\xff\xd8\xff" + b"0" * (self.THREE_MIB - 3)
        response = client.put(
            f"/api/media/blob/{key}", data=body, content_type="image/jpeg"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_storage().head(key), (self.THREE_MIB, "image/jpeg"))

    def test_the_exemption_still_matches_when_a_script_name_is_set(self):
        """豁免前缀的判据必须是 `request.path_info`（2026-09-25 复评 Minor-2）。

        `request.path` = `SCRIPT_NAME + path_info`（`django/core/handlers/wsgi.py`）⇒ 传
        `SCRIPT_NAME="/tt"`（部署在子路径）时，旧判据（用 `request.path`）的
        `/tt/api/media/blob/…` 匹配不到豁免前缀 ⇒ 这条 3 MiB 的**合法直传**会被本墙拦成 **400**
        （回归）。改用 `request.path_info` ⇒ 仍 **200** 且真的落盘。

        ⚠️ 变异：把判据换回 `request.path` ⇒ 本条以 **400** 变红（已实测，见任务报告）。
        """
        client = make_logged_in_client(username="body-wall-subpath", domain=TENANT_DOMAIN)
        key = f"{self.schema}/orders/2026092500002/three-mib.jpg"
        body = b"\xff\xd8\xff" + b"0" * (self.THREE_MIB - 3)
        response = client.put(
            f"/api/media/blob/{key}",
            data=body,
            content_type="image/jpeg",
            SCRIPT_NAME="/tt",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_storage().head(key), (self.THREE_MIB, "image/jpeg"))

    def test_a_small_request_is_not_affected(self):
        """正常大小的一整条链路（登录 + 读自己）照旧可用。"""
        client = make_logged_in_client(username="body-wall-small", domain=TENANT_DOMAIN)
        me = client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["data"]["user"]["username"], "body-wall-small")


class KeyMediaKindTests(SimpleTestCase):
    """`key_media_kind` 的判据：**按 `/` 切段后的第 2 段**，⛔ 不是「含 `users` 子串」。

    读码依据（`apps/core/storage.py`）：`build_order_key` 恒产出 `<租户>/orders/…`、
    `build_user_key` 恒产出 `<租户>/users/…` ⇒ 两种 key 只有第 2 段不同。
    ⚠️ 判错方向的后果：把订单截图误判成用户媒体 ⇒ 18 MiB 的图被 10 MB 闸拒（回归）
    ⇒ 认不出的形状一律按**订单**（更宽的那条）处理。
    """

    def test_keys_from_the_two_builders(self):
        self.assertEqual(key_media_kind(build_user_key("demo", 7, "avatar", "jpg")), "user")
        self.assertEqual(key_media_kind(build_user_key("demo", 7, "wechat-qr", "png")), "user")
        # ⭐ 问题反馈线：反馈截图也走 `users` 段 ⇒ 本机直传的早反馈上限
        #    与认领侧同取 10 MB（spec §1.3 的**理由本身**就是这一条判据）。
        self.assertEqual(key_media_kind(build_user_key("demo", 7, "feedback", "png")), "user")
        # ⭐ 首页二维码横幅线（D-0262）：`home-qr` 走同一条 `users` 段
        #    ⇒ 横幅二维码（给人扫的）同取 10 MB 早反馈与认领上限。
        self.assertEqual(key_media_kind(build_user_key("demo", 7, "home-qr", "png")), "user")
        self.assertEqual(key_media_kind(build_order_key("demo", "2026", "jpg")), "order")
        self.assertEqual(
            key_media_kind(build_order_key("demo", "2026", "jpg", kind="qr")), "order"
        )

    def test_build_user_key_only_accepts_the_whitelisted_kinds(self):
        """`kind` 白名单是**断言**（它直接成为路径段）：白名单外一律响亮地抛。

        ⚠️ 判据取四种**实际在用**的 kind 之外的形状（含一个像新前缀的名字），
        免得有人把 `<租户>/feedback/...` 那样的新前缀接进用户媒体这张网。
        """
        for kind in ("passport", "feedback/../x", "Feedback", "orders", "homeqr", ""):
            with self.subTest(kind=kind):
                with self.assertRaises(AssertionError):
                    build_user_key("demo", 7, kind, "png")

    def test_users_must_be_the_second_segment_not_a_substring(self):
        for key in (
            "demo/orders/users/1/avatar.x.jpg",   # users 出现在第 3 段
            "demo/usersx/1/avatar.x.jpg",         # 第 2 段只是以 users 开头
            "demo/1/users/avatar.x.jpg",          # users 不是第 2 段
            "demo/Users/1/a.jpg",                 # 大小写不匹配（种类段是**小写**字面量 `users`）
            "demo/whatever/x.jpg",
            "",
            "users/1/a.jpg",
        ):
            self.assertEqual(key_media_kind(key), "order", key)

    def test_a_trailing_slash_does_not_change_the_segment(self):
        """`demo/users/1/`（尾斜杠）⇒ 仍认 **user**：空段被归一化剔除，第 2 段还是 `users`。

        ⚠️ 与 `key_belongs_to_tenant` 同一套归一化（剔除空段与 `.`）——两种畸形 key
        （`demo/Users/1/a.jpg` 大小写、`demo/users/1/` 尾斜杠）此前**零断言**
        （独立评审 Minor）。
        """
        self.assertEqual(key_media_kind("demo/users/1/"), "user")
        self.assertEqual(key_media_kind("demo/users/1"), "user")

    def test_a_leading_empty_segment_is_normalized_like_the_tenant_gate(self):
        """与 `key_belongs_to_tenant` 同一套归一化（剔除空段与 `.`），判据才不会两套。"""
        self.assertEqual(key_media_kind(f"/{self._tenant()}/users/7/avatar.x.jpg"), "user")

    @staticmethod
    def _tenant() -> str:
        return "demo"
