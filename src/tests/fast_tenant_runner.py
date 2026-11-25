"""全量套件用的 test runner：在 Django 默认 runner 之上挂「共享租户 schema」。

为什么要有它：`django_tenants` 的 `TenantTestCase` 每个测试类都建一次 schema + 跑一遍迁移，
本仓 749 个类 ⇒ 类级开销占全量墙钟的 74%。具体做法与代价见 `tests/support/fast_tenant.py`。

挂法（二选一，都不需要改任何测试文件）：

- `manage.py test --testrunner=tests.fast_tenant_runner.FastTenantRunner ...`
- 或 `settings.TEST_RUNNER` 指过来（`config/settings.py` 里只在 `manage.py test` 时设）。

分片脚本本来就用 `--testrunner=shard_timing_runner.TimingRunner` 计时；这里在
**能 import 到它时**继承它（分片脚本会把 `.tools/ops` 放进 `PYTHONPATH`），
所以计时台账与按类配平的行为一个字节都不变。

开关：`FAST_TENANT_SCHEMA=0` ⇒ 共享 schema 的补丁不装，行为与改动前一致
（runner 本身仍存在，只是退化成「原样跑」）。
"""

from __future__ import annotations

from django.test.runner import DiscoverRunner

from tests.support.fast_tenant import cleanup_cached_schemas, install_patch

try:  # 只有分片脚本（`.tools/ops` 在 PYTHONPATH 上）能拿到它
    from shard_timing_runner import TimingRunner as _BaseRunner
except Exception:  # noqa: BLE001 —— 普通 `manage.py test` 没有这个模块，属正常
    _BaseRunner = DiscoverRunner


class FastTenantRunner(_BaseRunner):
    """`DiscoverRunner`（分片时是 `TimingRunner`）+ 共享租户 schema 补丁。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # `install_patch()` 幂等；`FAST_TENANT_SCHEMA=0` 时它直接不做事。
        self.shared_tenant_enabled = install_patch()

    def teardown_databases(self, old_config, **kwargs):
        """拆测试库**之前**清掉缓存 schema（⛔ 不留脏库，`--keepdb` 下也要清干净）。

        ⚠️ 不能只靠 `atexit`：`teardown_databases` 会把连接 `NAME` 还原成开发库名，
        到 `atexit` 时已经连不回这个测试库了（实测 `--keepdb` 会留下隐藏 schema）。
        """
        try:
            cleanup_cached_schemas()
        except Exception:  # noqa: BLE001 —— 清理失败不能把测试结论判红
            pass
        return super().teardown_databases(old_config, **kwargs)
