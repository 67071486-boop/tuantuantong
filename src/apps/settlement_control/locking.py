"""单机跨进程锁；运维核对时禁止新上游写调用。"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

from django.conf import settings


class LockUnavailable(Exception):
    pass


def trusted_executable(path):
    try:
        candidate = Path(path)
        if not candidate.is_absolute() or candidate.resolve(strict=True) != candidate:
            return False
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not os.access(path, os.X_OK)):
            return False
        for parent in candidate.parents:
            meta = parent.lstat()
            if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
                return False
        return True
    except OSError:
        return False


def _trusted_local_lock(path, fd):
    try:
        candidate = Path(path)
        if candidate.resolve(strict=True) != candidate:
            return False
        info = os.fstat(fd)
        actual = os.lstat(path)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
                or info.st_mode & stat.S_IWOTH):
            return False
        for parent in candidate.parents:
            meta = parent.lstat()
            if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
                return False
        return True
    except OSError:
        return False


@contextmanager
def operation_lock(*, exclusive=False):
    path = settings.SETTLEMENT_OPERATION_LOCK_FILE
    if not isinstance(path, str) or not os.path.isabs(path) or not hasattr(os, 'O_NOFOLLOW'):
        raise LockUnavailable
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise LockUnavailable
        if getattr(settings, 'ALIPAY_AUTH_LOCAL_ENABLED', False) and not _trusted_local_lock(path, fd):
            os.close(fd)
            raise LockUnavailable
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise LockUnavailable from None
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
