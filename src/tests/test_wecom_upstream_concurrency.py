from datetime import timedelta
from queue import Queue
from threading import Barrier, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone
from django_tenants.utils import get_tenant_model, schema_context

from apps.business.models import EmpType, Game, Order, PeerDownstreamBinding, Performance
from apps.business import peer_inbound
from apps.business.peer_sync import hash_token, new_token
from apps.wecom.models import WecomAppConfig, WecomNotification


class UpstreamNotificationRaceTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model().objects.create(schema_name='wecom_notice_race')

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_concurrent_same_status_creates_one_notice(self):
        with schema_context(self.tenant.schema_name):
            account = get_user_model().objects.create_user(username='synthetic-race')
            binding = PeerDownstreamBinding.objects.create(account=account, peer_base_url='https://race.test.invalid', token_in_hash=hash_token(new_token()), token_out=new_token())
            game = Game.objects.create(name='合成竞态游戏')
            order = Order.objects.create(order_no='SYNTHETIC-RACE-1', game=game, status='IN_PROGRESS', sales_agent='小李')
            Performance.objects.create(order=order, user=account, emp_type=EmpType.MAIN)
            WecomAppConfig.objects.update_or_create(tenant_schema=self.tenant.schema_name, defaults={'enabled':True,'corp_id':'corp','agent_id':1,'secret':'secret','order_status_enabled':True})
            event = {'kind':'order.update','target_ref':order.order_no,'occurred_at':(timezone.now()+timedelta(seconds=5)).isoformat(),'payload':{'status':'ISSUE'}}
            barrier = Barrier(2)
            results = Queue()
            real_lookup = peer_inbound._local_order_for_peer_ref
            def lookup(ref):
                result = real_lookup(ref)
                barrier.wait(timeout=20)
                return result
            def worker():
                try:
                    with schema_context(self.tenant.schema_name):
                        results.put(peer_inbound.apply_upstream_events([event], binding=binding))
                except BaseException as exc:
                    results.put(exc)
                finally:
                    connections.close_all()
            with patch.object(peer_inbound, '_local_order_for_peer_ref', side_effect=lookup):
                threads = [Thread(target=worker) for _ in range(2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(30)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            outcomes = [results.get_nowait() for _ in threads]
            for outcome in outcomes:
                if isinstance(outcome, BaseException): raise outcome
            order.refresh_from_db()
            self.assertEqual(len(order.wecom_notice_journal), 1)
            from apps.wecom.notifications import _recover_order_journal
            _recover_order_journal()
            self.assertEqual(WecomNotification.objects.filter(order_no=order.order_no).count(), 1)

    def test_concurrent_downstream_same_event_creates_one_notice(self):
        from threading import local
        from django.db.models.query import QuerySet
        from apps.business.models import PeerOrderLink, PeerUpstreamBinding
        with schema_context(self.tenant.schema_name):
            account = get_user_model().objects.create_user(username='synthetic-downstream-race')
            binding = PeerUpstreamBinding.objects.create(peer_name='合成上家', peer_base_url='https://down-race.test.invalid', peer_account='test', token_in_hash=hash_token(new_token()), token_out=new_token(), local_base_url='https://local.test.invalid', bound_by=account)
            game = Game.objects.create(name='合成下行竞态游戏')
            order = Order.objects.create(order_no='SYNTHETIC-DOWN-1', game=game, status='IN_PROGRESS', sales_agent='小李')
            PeerOrderLink.objects.create(order=order, upstream_binding=binding, peer_order_no='SYNTHETIC-DOWN-REF', last_applied_at=timezone.now()-timedelta(hours=1))
            WecomAppConfig.objects.update_or_create(tenant_schema=self.tenant.schema_name, defaults={'enabled':True,'corp_id':'corp','agent_id':1,'secret':'secret','order_status_enabled':True})
            event = {'kind':'order.update','target_ref':'SYNTHETIC-DOWN-REF','occurred_at':timezone.now().isoformat(),'payload':{'status':'ISSUE','game_name':game.name}}
            barrier = Barrier(2)
            results = Queue()
            local_state = local()
            real_first = QuerySet.first
            def first(queryset):
                value = real_first(queryset)
                if queryset.model is PeerOrderLink and not getattr(local_state,'passed',False) and not queryset.query.select_for_update:
                    local_state.passed = True
                    barrier.wait(timeout=20)
                return value
            def worker():
                try:
                    with schema_context(self.tenant.schema_name):
                        results.put(peer_inbound.apply_events([event], binding=binding))
                except BaseException as exc:
                    results.put(exc)
                finally:
                    connections.close_all()
            with patch.object(QuerySet, 'first', first):
                threads = [Thread(target=worker) for _ in range(2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(30)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            outcomes = [results.get_nowait() for _ in threads]
            for outcome in outcomes:
                if isinstance(outcome, BaseException): raise outcome
            order.refresh_from_db()
            self.assertEqual(len(order.wecom_notice_journal), 1)
            self.assertEqual(sum(outcome.applied for outcome in outcomes), 1)

    def test_local_transition_and_downstream_event_preserve_local_action(self):
        from threading import Event, current_thread
        from django.db import transaction
        from django.db.models.query import QuerySet
        from apps.business.models import PeerOrderLink, PeerUpstreamBinding
        from apps.business.order_status import apply_transition
        with schema_context(self.tenant.schema_name):
            account = get_user_model().objects.create_user(username='synthetic-local-race')
            binding = PeerUpstreamBinding.objects.create(peer_name='合成本地竞争上家', peer_base_url='https://local-race.test.invalid', peer_account='test', token_in_hash=hash_token(new_token()), token_out=new_token(), local_base_url='https://local.test.invalid', bound_by=account)
            game = Game.objects.create(name='合成本地竞态游戏')
            order = Order.objects.create(order_no='SYNTHETIC-LOCAL-1', game=game, status='IN_PROGRESS', sales_agent='小李')
            PeerOrderLink.objects.create(order=order, upstream_binding=binding, peer_order_no='SYNTHETIC-LOCAL-REF', last_applied_at=timezone.now()-timedelta(hours=1))
            WecomAppConfig.objects.update_or_create(tenant_schema=self.tenant.schema_name, defaults={'enabled':True,'corp_id':'corp','agent_id':1,'secret':'secret','order_status_enabled':True})
            event = {'kind':'order.update','target_ref':'SYNTHETIC-LOCAL-REF','occurred_at':timezone.now().isoformat(),'payload':{'status':'PAUSED','game_name':game.name}}
            local_written, incoming_read, release = Event(), Event(), Event()
            results = Queue()
            real_first = QuerySet.first
            def first(queryset):
                value = real_first(queryset)
                if current_thread().name == 'inbound-race' and queryset.model is PeerOrderLink and not queryset.query.select_for_update:
                    incoming_read.set()
                return value
            def local_worker():
                try:
                    with schema_context(self.tenant.schema_name), transaction.atomic():
                        current=Order.objects.get(pk=order.pk)
                        apply_transition(current,'ISSUE',nodes=frozenset({'order.change_status'}))
                        local_written.set()
                        if not release.wait(20): raise TimeoutError('release')
                    results.put('local committed')
                except BaseException as exc: results.put(exc)
                finally: connections.close_all()
            def inbound_worker():
                try:
                    with schema_context(self.tenant.schema_name):
                        results.put(peer_inbound.apply_events([event],binding=binding))
                except BaseException as exc: results.put(exc)
                finally: connections.close_all()
            with patch.object(QuerySet, 'first', first):
                first_thread=Thread(target=local_worker);first_thread.start()
                self.assertTrue(local_written.wait(20))
                second_thread=Thread(target=inbound_worker,name='inbound-race');second_thread.start()
                self.assertTrue(incoming_read.wait(20))
                release.set()
                first_thread.join(30);second_thread.join(30)
            self.assertFalse(first_thread.is_alive() or second_thread.is_alive())
            outcomes=[results.get_nowait(),results.get_nowait()]
            for outcome in outcomes:
                if isinstance(outcome,BaseException): raise outcome
            order.refresh_from_db()
            self.assertEqual(order.status,'ISSUE')
            self.assertEqual(len(order.wecom_notice_journal),1)
            self.assertEqual(next(o for o in outcomes if o != 'local committed').skipped,1)
