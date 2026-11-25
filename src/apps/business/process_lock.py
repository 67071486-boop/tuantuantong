"""基于 `fcntl.flock` 的**非阻塞**进程锁（同步 / 补数据类管理命令共用）。为什么要有它：`dailiantong_sync`（常驻）与手工补数据命令（`dailiantong_sync_progress`）都会对同一条推送行做「读上游 → 下载图 → 建本地行」；两个进程同时跑会重复下载、
重复写审计，严重时把一轮拖成分钟级（M7）。口径： - `acquire()` 非阻塞：拿不到就**立刻**返回 `False`，由调用方打印明确原因并退出（⛔ 不陪等——排障命令卡在那里比失败更难查）； - 锁文件用 `a+` 打开并 `flock(LOCK_EX | LOCK_NB)`；进程异常退出时内核自动释放，
不需要「删锁文件」这种会误删别人锁的动作； - 两台机共享同一个部署目录时也成立（`flock` 是文件级、随 NFS 语义，与既有 `dailiantong_sync` 的用法一致）。 ⚠️ 只做**互斥**，不做「谁持有」的诊断：`path` 写进错误文案，让运维知道去看哪个文件。"""

from __future__ import annotations

import fcntl
from pathlib import Path


class ProcessLock:
    """文件级非阻塞互斥锁；`with` 用法或手动 `acquire` / `release` 都可以。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> bool:
        """尝试加锁；成功 `True`，已被别人持有 `False`（⛔ 不阻塞）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        finally:
            self._stream.close()
            self._stream = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
