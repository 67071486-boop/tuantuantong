from datetime import timedelta

from apps.business.models import PeerSyncEvent
from apps.business.peer_worker import claim_due_events, deliver_one
from tests._peer_stream_fakes import patch_peer_stream, stream_response
from tests.test_peer_worker import _WorkerFixture


class PeerLockBusyTests(_WorkerFixture):
    def test_old_busy_events_do_not_starve_later_normal_event(self):
        busy = [self._event(target_ref=f'busy-{i}', reason='对端订单正在处理，稍后自动重试',
                            next_attempt_at=self.now + timedelta(minutes=1)) for i in range(50)]
        PeerSyncEvent.objects.filter(pk__in=[row.pk for row in busy]).update(created_at=self.now - timedelta(days=1))
        normal = self._event(target_ref='normal')
        PeerSyncEvent.objects.filter(pk=normal.pk).update(created_at=self.now + timedelta(seconds=30))
        claimed = claim_due_events(now=self.now + timedelta(minutes=1))
        self.assertEqual(len(claimed), 50)
        self.assertIn(normal.pk, claimed)

    def test_busy_response_stays_pending_and_recovers_after_repeated_attempts(self):
        event = self._event(attempts=4)
        response = stream_response({'code': 409, 'msg': '订单正在处理，请稍后重试', 'data': {'retry_kind': 'split_group_busy'}}, status_code=409)
        for step in range(6):
            with patch_peer_stream(response):
                now = self.now + timedelta(minutes=step)
                self.assertEqual(deliver_one(event.pk, now=now), 'retry')
                event.refresh_from_db()
                self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
                self.assertEqual(event.attempts, 4)
                self.assertEqual(event.next_attempt_at, now + timedelta(seconds=60))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.consecutive_failures, 0)
        with patch_peer_stream(stream_response({'code': 0, 'data': {}})):
            self.assertEqual(deliver_one(event.pk, now=self.now + timedelta(minutes=7)), 'done')
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)

    def test_unrelated_409_still_uses_normal_failure_limit(self):
        event = self._event(attempts=4)
        with patch_peer_stream(stream_response({'code': 409, 'msg': '其他冲突', 'data': None}, status_code=409)):
            self.assertEqual(deliver_one(event.pk, now=self.now), 'failed')
        event.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.FAILED)
        self.assertEqual(event.attempts, 5)
