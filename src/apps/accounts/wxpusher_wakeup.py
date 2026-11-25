import logging
from contextlib import closing

import psycopg
from psycopg import sql
from django.db import Error, connection

CHANNEL = "tuantuantong_wxpusher_hall"
logger = logging.getLogger(__name__)


def notify_hall_wakeup():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_notify(%s, '')", [CHANNEL])
    except Error as exc:
        logger.warning("抢单提醒唤醒失败，待补捞（%s）", type(exc).__name__)


def open_listener():
    params = connection.get_connection_params()
    params.update(autocommit=True, connect_timeout=2)
    listener = psycopg.connect(**params)
    try:
        listener.execute(sql.SQL("LISTEN {}").format(sql.Identifier(CHANNEL)))
    except BaseException:
        listener.close()
        raise
    return listener


def wait_for_hall_wakeup(listener, timeout):
    with closing(listener.notifies(timeout=timeout, stop_after=1)) as notifications:
        return next(notifications, None) is not None
