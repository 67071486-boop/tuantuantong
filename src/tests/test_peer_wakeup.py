from contextlib import closing
from unittest import mock

from django.db import connection, transaction
from django.test import TransactionTestCase

from apps.business import peer_wakeup


class PeerWakeupDatabaseTests(TransactionTestCase):
    def _notification(self, listener, *, timeout=0.05):
        with closing(listener.notifies(timeout=timeout, stop_after=1)) as notifications:
            return next(notifications, None)

    def test_outer_commit_releases_one_empty_signal_for_a_batch_and_rollback_releases_none(self):
        with closing(peer_wakeup.open_listener()) as listener:
            self.assertTrue(listener.autocommit)
            with transaction.atomic():
                for _ in range(32):
                    peer_wakeup.notify_hall_take()
                self.assertIsNone(self._notification(listener))
            notify = self._notification(listener, timeout=0.5)
            self.assertEqual((notify.channel, notify.payload), (peer_wakeup.CHANNEL, ""))
            self.assertIsNone(self._notification(listener))
            with transaction.atomic():
                peer_wakeup.notify_hall_take()
                transaction.set_rollback(True)
            self.assertIsNone(self._notification(listener))
        self.assertTrue(listener.closed)

    def test_notify_savepoint_keeps_the_outer_transaction_usable(self):
        def reject_notify(execute, statement, params, many, context):
            if "pg_notify" in statement:
                return execute("SELECT 1 / 0", None, False, context)
            return execute(statement, params, many, context)

        with transaction.atomic(), connection.execute_wrapper(reject_notify):
            with mock.patch.object(peer_wakeup, "_last_warning_at", float("-inf")):
                with self.assertLogs(peer_wakeup.logger, level="WARNING"):
                    peer_wakeup.notify_hall_take()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 7")
                self.assertEqual(cursor.fetchone(), (7,))
