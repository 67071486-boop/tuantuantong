"""账号和角色共用的管理员变更事务及租户锁。"""
import hashlib
from contextlib import contextmanager

from django.db import connection, transaction


def _tenant_lock_key() -> int:
    digest = hashlib.sha256(connection.schema_name.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_tenant_admin_mutations():
    """按当前租户加锁。事务结束时释放，须在管理员判定和账号行锁之前取得。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_tenant_lock_key()])


@contextmanager
def admin_mutation_transaction(*, lock_immediately=True):
    """角色变更可推迟取锁，让角色守卫在原有的读取之后按需取锁。"""
    with transaction.atomic():
        if lock_immediately:
            lock_tenant_admin_mutations()
        yield
