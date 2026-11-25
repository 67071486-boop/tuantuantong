"""`deploy/app-release/publish.py` 与下载页之间的**契约**网（2026-10-21，轻量档）。

## 为什么要有这张网

下载页（`src/frontend/public/download/index.html`）**不在 `src/` 的构建产物里做数据**：
它用一个 `<script>` 去读桶里的 `releases.js`（`window.APP_RELEASE_FEED = {...}`），
再由发布脚本 `deploy/app-release/publish.py` 写这份文件。两侧是**字符串契约**：

- 脚本改了变量名 / 清单字段 → 页面**静默空白**（不报错、也不红任何网）；
- 页面改了指的变量名 → 同理。

所以这里钉三件事：① 清单能被自己**写出去再读回来**；② 「只保留最近 3 个」的**行为**；
③ 页面与脚本用的是**同一个变量名、同一套字段**。

⚠️ 如实边界：本网**不碰网络、不碰 COS**（桶有没有建、密钥有没有权限，跑 `publish.py --dry-run`
才知道）；也**不测**页面的渲染（那是浏览器行为，见 `docs/RUNBOOK.md`「打手 App 安装包分发」）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import SimpleTestCase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "deploy" / "app-release" / "publish.py"
_PAGE = _REPO_ROOT / "src" / "frontend" / "public" / "download" / "index.html"


def _load_publish():
    """把发布脚本当模块加载。⛔ 不用 `import deploy.*`——目录名带 `-`，不是包。"""
    spec = importlib.util.spec_from_file_location("app_release_publish", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HAS_DEPLOY = (Path(__file__).resolve().parents[2] / "deploy").is_dir()

@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class FeedContractTests(SimpleTestCase):
    def setUp(self):
        self.m = _load_publish()

    def _release(self, name: str, code: int, notes: str = "") -> dict:
        return {
            "file": f"daidaitong-{name}_{code}.apk",
            "name": name,
            "code": code,
            "size": 1024,
            "size_text": "1.0 KB",
            "sha256": "0" * 64,
            "uploaded_at": "2026-10-21T13:00:00+08:00",
            "notes": notes,
        }

    def test_feed_survives_write_then_read(self):
        """写出去的 `releases.js` 必须能被自己读回来（含中文更新说明）。"""
        feed = {
            "app": "代代通",
            "package": "chat.suqing.daidaitong",
            "base": "https://example.invalid/apk/",
            "updated_at": "2026-10-21T13:00:00+08:00",
            "releases": [self._release("0.1.0", 100, "首版：登录 / 大厅")],
        }
        text = self.m.render_feed(feed)
        self.assertIn("window.APP_RELEASE_FEED", text)
        self.assertEqual(self.m.parse_feed(text), feed)

    def test_empty_or_missing_feed_means_first_release(self):
        self.assertEqual(self.m.parse_feed(""), {})

    def test_unrecognised_feed_fails_loudly(self):
        with self.assertRaises(self.m.Die):
            self.m.parse_feed('{"releases": []}')

    def test_same_version_republish_replaces_instead_of_duplicating(self):
        old = [self._release("0.1.0", 100, "旧的说明"), self._release("0.0.9", 99)]
        merged = self.m.merge_release(old, self._release("0.1.0", 100, "新的说明"))
        self.assertEqual([r["name"] for r in merged], ["0.1.0", "0.0.9"])
        self.assertEqual(merged[0]["notes"], "新的说明")

    def test_keeps_the_three_newest_and_names_the_rest(self):
        """业务方口径：旧包只保留最近 3 个（总数 3，含最新）。"""
        releases = [self._release(f"0.{i}.0", i) for i in range(1, 7)]
        doomed = self.m.prune_candidates(releases)
        self.assertEqual(
            doomed,
            ["daidaitong-0.3.0_3.apk", "daidaitong-0.2.0_2.apk", "daidaitong-0.1.0_1.apk"],
        )

    def test_prune_is_by_version_code_not_by_list_order(self):
        """清单顺序乱着来也要按 versionCode 判新旧 —— ⛔ 别让「谁最后上传」决定删谁。"""
        releases = [self._release("0.1.0", 100), self._release("0.2.0", 200),
                    self._release("0.0.9", 90), self._release("0.1.5", 150)]
        self.assertEqual(self.m.prune_candidates(releases), ["daidaitong-0.0.9_90.apk"])

    def test_bucket_keys_stay_inside_apk_prefix(self):
        self.assertEqual(self.m.apk_key("daidaitong-0.1.0_100.apk"), "apk/daidaitong-0.1.0_100.apk")
        for bad in ("", "../releases.js", "apk/x.apk", "a/b.apk"):
            with self.subTest(bad=bad), self.assertRaises(self.m.Die):
                self.m.apk_key(bad)

    def test_publish_sets_and_preserves_the_minimum_supported_version(self):
        previous = {"releases": [self._release("0.1.10", 110)]}
        writes = []

        def read_feed(*_args):
            return self.m.parse_feed(writes[-1]) if writes else previous

        def write_feed(_client, _bucket, feed):
            writes.append(self.m.render_feed(feed))

        with TemporaryDirectory() as temp_dir:
            apk = Path(temp_dir) / "local-fixture.apk"
            apk.write_bytes(b"test package")
            with (
                patch.dict(os.environ, {"APP_COS_SECRET_ID": "test", "APP_COS_SECRET_KEY": "test"}),
                patch.object(self.m, "make_client"),
                patch.object(self.m, "remote_feed", side_effect=read_feed),
                patch.object(self.m, "put_feed", side_effect=write_feed),
                patch.object(self.m, "require_bucket"),
                patch.object(self.m, "upload_apk") as upload,
                patch.object(self.m, "head", return_value={
                    "Content-Length": apk.stat().st_size,
                    "Content-Type": "application/vnd.android.package-archive",
                }),
                patch.object(self.m, "list_apks", return_value=[]),
            ):
                args = ["--apk", str(apk), "--version-name", "0.1.11", "--version-code", "111"]
                with self.assertRaises(self.m.Die):
                    self.m.run(args + ["--min-supported-code", "112"])
                upload.assert_not_called()
                self.assertEqual(self.m.run(args + ["--min-supported-code", "111"]), 0)
                self.assertEqual(self.m.parse_feed(writes[-1])["min_supported_code"], 111)
                self.assertEqual(self.m.run(args), 0)
                self.assertEqual(self.m.parse_feed(writes[-1])["min_supported_code"], 111)
                self.assertEqual(self.m.run(args + ["--min-supported-code", "0"]), 0)
                self.assertNotIn("min_supported_code", self.m.parse_feed(writes[-1]))


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class PageContractTests(SimpleTestCase):
    """页面（静态 HTML）与脚本（写清单）必须指着**同一个**变量名与字段。"""

    def setUp(self):
        self.page = _PAGE.read_text(encoding="utf-8")

    def test_page_reads_the_variable_the_publisher_writes(self):
        self.assertIn("window.APP_RELEASE_FEED", _load_publish().render_feed({}))
        self.assertIn("window.APP_RELEASE_FEED", self.page)

    def test_page_uses_only_fields_the_feed_carries(self):
        """页面读的字段必须都在清单里 —— 少一个就是「页面上某个位置永远空白」。"""
        feed_keys = {"releases", "base", "app", "package", "updated_at"}
        used = set(re.findall(r"\b(?:feed|newest|r)\.([a-z0-9_]+)", self.page))
        unknown = {k for k in used if k and k not in feed_keys | {
            "name", "code", "file", "size", "size_text", "sha256", "uploaded_at", "notes",
        }}
        self.assertEqual(unknown, set(), f"页面读了清单里没有的字段：{sorted(unknown)}")

    def test_page_points_at_the_public_bucket_not_the_private_media_bucket(self):
        m = _load_publish()
        self.assertIn(f"https://{m.DEFAULT_BUCKET}.cos.{m.DEFAULT_REGION}.myqcloud.com", self.page)
        self.assertNotIn("tt-cos-media", self.page)


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class SizeFormatTests(SimpleTestCase):
    def test_human_size_is_rounded_to_one_decimal(self):
        m = _load_publish()
        self.assertEqual(m.human_size(0), "0.0 MB")
        self.assertEqual(m.human_size(int(88.34 * 1024 * 1024)), "88.3 MB")


@unittest.skipUnless(_HAS_DEPLOY, "本仓库只开源网页前后端源码，不含部署脚本 deploy/")
class EnvFileTests(SimpleTestCase):
    def test_reads_key_value_and_ignores_comments(self):
        import tempfile

        m = _load_publish()
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as fh:
            fh.write('# 注释\nAPP_COS_BUCKET="tt-app-1"\n\nAPP_COS_REGION=ap-shanghai\n')
            path = Path(fh.name)
        try:
            env = m.env_file(path)
        finally:
            path.unlink()
        self.assertEqual(json.dumps(env, sort_keys=True),
                         json.dumps({"APP_COS_BUCKET": "tt-app-1", "APP_COS_REGION": "ap-shanghai"},
                                    sort_keys=True))
