"""Use the tenant database to wake its own outbox worker."""
import logging
import time

import psycopg
from psycopg import sql
from django.db import DatabaseError, connection, transaction

CHANNEL = "tuantuantong_peer_hall_take"
logger = logging.getLogger(__name__)
_last_warning_at = float("-inf")


def notify_hall_take() -> None:
    global _last_warning_at
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, '')", [CHANNEL])
    except DatabaseError as exc:
        if connection.closed_in_transaction or connection.needs_rollback:
            raise
        now = time.monotonic()
        if now - _last_warning_at >= 60:
            _last_warning_at = now
            logger.warning("大厅摘牌通知失败，定时投递继续（%s）", type(exc).__name__)


def open_listener():
    params = connection.get_connection_params()
    params["autocommit"] = True
    params["connect_timeout"] = 2
    listener = psycopg.connect(**params)
    try:
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(CHANNEL)))
    except BaseException:
        listener.close()
        raise
    return listener
