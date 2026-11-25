"""生产数据库长连接的两条接线钉子（Task 8b）。

## 背景（真机实测）

应用机 → 腾讯云托管 PostgreSQL 走云联网：**ping 0.13 ms、TCP 建连约 2.7 ms**；
PostgreSQL 一条新连接还要多付 SCRAM 认证与若干次往返。而 `CONN_MAX_AGE` 未配时
Django 默认 **0** ⇒ **每条请求都重连一次**，一个页面通常并发若干 API 调用 ⇒ 每页白付几十毫秒。
实例 `max_connections` 实测 **2048**、当时占用 14 ⇒ 2 个 gunicorn worker 各持一条毫无压力。

## 本文件钉什么 / 不钉什么

- **钉**两件事：① 默认值是 **0**（本机开发用本地库、没有收益，且必须保持今天的行为）；
  ② `CONN_HEALTH_CHECKS` 这根**接线在场**且为 `True`（它与长连接**成对**：持连接会遇到
  "服务端把空闲连接掐了"，这个开关让 Django 在请求开始时先验一次，避免
  `server closed the connection unexpectedly` 这类**看起来像代码 bug** 的 500）。
- ⛔ **不钉**「环境变量能覆盖默认值」——那是 `django-environ` 的活，在这里测等于**自指**
  （断言自己刚搭的桩），没有任何诊断价值。生产上的值由机器 `.env` 的 `POSTGRES_CONN_MAX_AGE=60` 给，
  验收判据是「60 秒内两次请求复用同一条连接」（见计划 Task 8b Step 6）。

⚠️ 别把 `CONN_MAX_AGE == 0` 当成"生产没开"——**生产由 `.env` 覆盖成 60**；
这条断言守的是**没有 `.env` 覆盖时**（开发 / CI / 新检出）与今天完全一样的行为。
"""

from unittest import TestCase

from django.conf import settings


class DatabaseConnectionSettingsTests(TestCase):
    """长连接接线的默认值钉子。"""

    def test_conn_max_age_defaults_to_zero(self):
        """`CONN_MAX_AGE` 默认必须是 0（本机保持"每请求一条连接"的今天行为）。

        改掉这个默认值（例如图省事写成 60）会让本机开发的每条请求都持着一条连接，
        而本机连的是本地库 ⇒ **只多占连接、没有任何收益**，且悄悄换掉了既有行为 ⇒ 本用例应当变红。
        """
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], 0)

    def test_conn_health_checks_is_wired_and_on(self):
        """`CONN_HEALTH_CHECKS` 必须**接在 DATABASES["default"] 上**且默认 `True`。

        它在 `settings.py` 里出现、却没有传进 `DATABASES` 是**静默失效**的
        （Django 只读 `DATABASES` 里的键）⇒ 所以这里断言的是**在场**，不是"模块里有这个名字"。
        它必须与长连接同时具备：生产 `.env` 把 `CONN_MAX_AGE` 开到 60 后，
        托管实例可能掐掉空闲连接，缺了它就会冒出 500。
        """
        self.assertIn("CONN_HEALTH_CHECKS", settings.DATABASES["default"])
        self.assertIs(settings.DATABASES["default"]["CONN_HEALTH_CHECKS"], True)
