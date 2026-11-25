import json
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction
from django.test import Client, TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.business.models import EmpType, Game, Order, OrderStatus, PeerDownstreamBinding, PeerSyncEvent, PeerUpstreamBinding, Performance
from apps.business.peer_sync import hash_token, new_token
from apps.business.peer_worker import deliver_one
from tests._peer_stream_fakes import stream_response


class PeerBusyChainTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name='peer_busy_chain')
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain='peer-busy-chain.test.local')
        cls.domain.save()
        cls.sender_tenant = get_tenant_model()(schema_name='peer_busy_sender')
        cls.sender_tenant.save(verbosity=0)
        cls.sender_domain = get_tenant_domain_model()(tenant=cls.sender_tenant, domain='peer-busy-sender.test.local')
        cls.sender_domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.sender_domain.delete()
        cls.sender_tenant.delete(force_drop=True)
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_real_token_receiver_lock_busy_and_worker_retry(self):
        user = get_user_model().objects.create_user(username='peer-busy-user')
        game = Game.objects.create(name='合成并发游戏')
        token = new_token()
        PeerUpstreamBinding.objects.create(peer_name='合成上家', peer_base_url='https://peer.test.invalid', peer_account='synthetic', token_in_hash=hash_token(token), token_out=new_token(), local_base_url='https://peer-busy-chain.test.local', bound_by=user)
        with schema_context(self.sender_tenant.schema_name):
            sender_user = get_user_model().objects.create_user(username='peer-busy-sender')
            sender = PeerDownstreamBinding.objects.create(account=sender_user, peer_base_url='https://peer.test.invalid', token_in_hash=hash_token(new_token()), token_out=token)
        main = Order.objects.create(game=game, order_no='99990000000000000001', split_id='synthetic-group', split_kind='LEGACY', total_price=Decimal('100'), sales_user=user)
        client = Client(SERVER_NAME=self.domain.domain)
        url = '/api/business/peer-sync/inbound/events/'
        initial = {'idempotency_key': uuid.uuid4().hex, 'kind': 'order.assign', 'target_ref': 'synthetic-order', 'payload': {'status': 'IN_PROGRESS', 'game_name': game.name, 'hall_price': '100'}}
        response = client.post(url, {'events': [initial]}, content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + token)
        self.assertEqual(response.json()['code'], 0, response.content)
        denied = client.post(url, {'events': [initial]}, content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + new_token())
        self.assertEqual(denied.status_code, 401)
        child = Order.objects.get(source_order_no='synthetic-order')
        child.split_id = main.split_id; child.split_kind = 'LEGACY'; child.sales_user = user
        child.save(update_fields=['split_id', 'split_kind', 'sales_user'])
        Performance.objects.create(order=child, user=user, emp_type=EmpType.SALES)
        now = timezone.now()
        with schema_context(self.sender_tenant.schema_name):
            event = PeerSyncEvent.objects.create(binding_role='downstream', binding_id=str(sender.pk), direction='out', kind='order.update', target_type='order', target_ref='synthetic-order', payload={'status': 'IN_PROGRESS', 'game_name': game.name, 'hall_price': '80'}, meta={'occurred_at': (now + timedelta(seconds=2)).isoformat()}, idempotency_key=uuid.uuid4().hex)
        held, release = threading.Event(), threading.Event()
        errors = []
        def hold_main():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    Order.objects.select_for_update().get(pk=main.pk)
                    held.set()
                    if not release.wait(15):
                        raise AssertionError('锁未释放')
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        thread = threading.Thread(target=hold_main)
        thread.start()
        self.assertTrue(held.wait(10))
        received = []
        def transport(method, target, **kwargs):
            with schema_context(self.sender_tenant.schema_name):
                incoming = client.post(url, kwargs['json'], content_type='application/json', HTTP_AUTHORIZATION=kwargs['headers']['Authorization'])
            received.append((incoming.status_code, incoming.json()))
            return stream_response(incoming.json(), status_code=incoming.status_code)
        try:
            with schema_context(self.sender_tenant.schema_name), patch('apps.business.peer_client.httpx.stream', side_effect=transport):
                self.assertEqual(deliver_one(event.pk, now=now), 'retry')
                event.refresh_from_db()
            child.refresh_from_db()
            self.assertEqual(received[0][0], 409, received[0])
            self.assertEqual(received[0][1]['data']['retry_kind'], 'split_group_busy')
            self.assertEqual(child.total_price, Decimal('100'))
            self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
            self.assertEqual(event.attempts, 0)
        finally:
            release.set(); thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with schema_context(self.sender_tenant.schema_name), patch('apps.business.peer_client.httpx.stream', side_effect=transport):
            self.assertEqual(deliver_one(event.pk, now=now + timedelta(minutes=2)), 'done')
            event.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(child.total_price, Decimal('80'))
        with schema_context(self.sender_tenant.schema_name):
            self.assertFalse(Order.objects.filter(source_order_no='synthetic-order').exists())
