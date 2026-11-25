import threading
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.db import DatabaseError, connection, connections, transaction
from django.test import TransactionTestCase
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business.models import EmpType, Game, Order, OrderRefund, OrderStatus, Performance, PerformanceAdjustment
from apps.business.order_status import apply_transition
from apps.business.order_writes import WriteContext, change_price, lock_order_and_sales_nowait, refund_total_write
from tests.p5_auth_helpers import grant_games
from rest_framework.test import APIRequestFactory, force_authenticate


class WecomRefundConcurrencyTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name='wecom_refund_concurrency')
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain='refund-concurrency.test.com')
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        shipping = patch('apps.business.xgj_ship.schedule_auto_ship')
        shipping.start()
        self.addCleanup(shipping.stop)

    def test_two_simultaneous_external_observations_write_one_delta(self):
        game = Game.objects.create(name='合成游戏')
        order = Order.objects.create(game=game, order_no='99990000000000000001',
                                     status=OrderStatus.IN_PROGRESS, total_price=Decimal('100'))
        barrier = threading.Barrier(2)
        results = []
        def work(index):
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    item = Order.objects.get(pk=order.pk)
                    barrier.wait(timeout=10)
                    result = refund_total_write(
                        item, refund_total=Decimal('10'), reason='企微退款',
                        request_id=f'synthetic-concurrent-{index}',
                        ctx=WriteContext(log_actor='企微同步'), dry_run=False,
                        allow_external_refund=True,
                    )
                    results.append(result.ok)
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()
        threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [True, True])
        order.refresh_from_db()
        self.assertEqual(order.refund_fee, Decimal('10'))
        self.assertEqual(list(OrderRefund.objects.filter(order=order).values_list('amount', flat=True)), [Decimal('10')])

    def test_last_child_completion_races_main_refund(self):
        from apps.business import settlement_hooks

        game = Game.objects.create(name='拆单并发游戏')
        sales = User.objects.create_user(username='synthetic-split-sales')
        StaffProfile.objects.create(user=sales, commission_mode=CommissionMode.PROFIT_PERCENT,
                                    commission_value=Decimal('20'))
        main = Order.objects.create(game=game, order_no='99990000000000000002',
                                    status=OrderStatus.COMPLETED, total_price=Decimal('100'),
                                    sales_user=sales, split_id='synthetic-split')
        child = Order.objects.create(game=game, order_no='99990000000000000003',
                                     status=OrderStatus.PENDING_REVIEW, total_price=Decimal('0'),
                                     sales_user=sales, split_id='synthetic-split')
        main_sales = Performance.objects.create(order=main, emp_type=EmpType.SALES, user=sales,
                                                commission_rate=Decimal('20'))
        Performance.objects.create(order=child, emp_type=EmpType.SALES, user=sales)
        entered, release, refund_started, refund_done = (threading.Event() for _ in range(4))
        results = []
        original = settlement_hooks.customer_commission_amount
        def delayed(order, *args):
            amount = original(order, *args)
            if threading.current_thread().name == 'complete-child' and order.pk == main.pk and not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise AssertionError('未释放并发屏障')
            return amount
        def complete():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    apply_transition(Order.objects.get(pk=child.pk), OrderStatus.COMPLETED,
                                     nodes={'order.change_status_completed'})
                results.append('completed')
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()
        def refund():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    item = Order.objects.get(pk=main.pk)
                    refund_started.set()
                    try:
                        with transaction.atomic():
                            item = lock_order_and_sales_nowait(item)
                            outcome = refund_total_write(item, refund_total=Decimal('10'), reason='企微退款',
                                                         request_id='synthetic-split-refund',
                                                         ctx=WriteContext(log_actor='企微同步'), dry_run=False,
                                                         allow_external_refund=True)
                            results.append(outcome.ok)
                    except DatabaseError:
                        results.append('busy')
            except Exception as exc:
                results.append(exc)
            finally:
                refund_done.set()
                connections.close_all()
        with patch('apps.business.settlement_hooks.customer_commission_amount', side_effect=delayed):
            thread = threading.Thread(target=complete, name='complete-child')
            thread.start()
            self.assertTrue(entered.wait(10), results)
            refund_thread = threading.Thread(target=refund, name='refund-main')
            refund_thread.start()
            try:
                self.assertTrue(refund_started.wait(10))
                self.assertTrue(refund_done.wait(2))
            finally:
                release.set()
                thread.join(15)
                refund_thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertFalse(refund_thread.is_alive())
        self.assertCountEqual(results, ['completed', 'busy'])
        with transaction.atomic():
            current = lock_order_and_sales_nowait(Order.objects.get(pk=main.pk))
            outcome = refund_total_write(
                current, refund_total=Decimal('10'), reason='企微退款', request_id='retry-after-child',
                ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
            )
            self.assertTrue(outcome.ok, outcome)
        main_sales.refresh_from_db()
        adjustments = sum(PerformanceAdjustment.objects.filter(performance=main_sales).values_list('amount', flat=True))
        self.assertEqual(main_sales.bonus_amount + adjustments, Decimal('18'))

    def test_refund_waits_for_finance_settlement_and_records_adjustment(self):
        from apps.business import settlement

        game = Game.objects.create(name='财务并发游戏')
        sales = User.objects.create_user(username='synthetic-settlement-sales')
        finance = User.objects.create_superuser(username='synthetic-finance', password='synthetic')
        grant_games(finance, game)
        order = Order.objects.create(game=game, order_no='99990000000000000004',
                                     status=OrderStatus.COMPLETED, total_price=Decimal('100'))
        customer = Performance.objects.create(order=order, emp_type=EmpType.SALES, user=sales,
                                              commission_rate=Decimal('20'), bonus_amount=Decimal('20'))
        entered, release, refund_started, refund_done = (threading.Event() for _ in range(4))
        results = []
        original = settlement._row_nets
        def delayed(*args):
            value = original(*args)
            entered.set()
            if not release.wait(10):
                raise AssertionError('财务结账屏障未释放')
            return value
        def settle():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    settlement.settle_performances([customer.pk], actor='财务', user=finance)
                    results.append('settled')
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()
        def refund():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    current = Order.objects.get(pk=order.pk)
                    refund_started.set()
                    try:
                        with transaction.atomic():
                            current = lock_order_and_sales_nowait(current)
                            outcome = refund_total_write(
                                current, refund_total=Decimal('10'), reason='企微退款',
                                request_id='synthetic-settlement-refund',
                                ctx=WriteContext(log_actor='企微同步'), dry_run=False,
                                allow_external_refund=True,
                            )
                            results.append(outcome.ok)
                    except DatabaseError:
                        results.append('busy')
            except Exception as exc:
                results.append(exc)
            finally:
                refund_done.set()
                connections.close_all()
        with patch('apps.business.settlement._row_nets', side_effect=delayed):
            settling = threading.Thread(target=settle)
            settling.start()
            self.assertTrue(entered.wait(10), results)
            refunding = threading.Thread(target=refund)
            refunding.start()
            try:
                self.assertTrue(refund_started.wait(10))
                self.assertTrue(refund_done.wait(2))
            finally:
                release.set()
                settling.join(15)
                refunding.join(15)
            self.assertFalse(settling.is_alive())
            self.assertFalse(refunding.is_alive())
        self.assertCountEqual(results, ['settled', 'busy'])
        with transaction.atomic():
            current = lock_order_and_sales_nowait(Order.objects.get(pk=order.pk))
            outcome = refund_total_write(
                current, refund_total=Decimal('10'), reason='企微退款', request_id='retry-after-settlement',
                ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
            )
            self.assertTrue(outcome.ok, outcome)
        customer.refresh_from_db()
        self.assertEqual(customer.settled_amount, Decimal('20'))
        self.assertEqual(PerformanceAdjustment.objects.get(performance=customer).amount, Decimal('-2'))

    def test_child_refund_and_group_price_change_do_not_deadlock(self):
        game = Game.objects.create(name='拆单改价并发游戏')
        admin = User.objects.create_superuser(username='synthetic-split-admin', password='synthetic')
        grant_games(admin, game)
        main = Order.objects.create(game=game, order_no='99990000000000000005',
                                    status=OrderStatus.COMPLETED, total_price=Decimal('60'),
                                    split_id='synthetic-lock-group', split_kind='LEGACY')
        child = Order.objects.create(game=game, order_no='99990000000000000006',
                                     status=OrderStatus.COMPLETED, total_price=Decimal('40'),
                                     split_id='synthetic-lock-group', split_kind='LEGACY')
        child_locked, main_locked = threading.Event(), threading.Event()
        results = []
        def refund():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    if not main_locked.wait(10):
                        raise AssertionError('改价没有锁住主单')
                    try:
                        with transaction.atomic():
                            current = lock_order_and_sales_nowait(Order.objects.get(pk=child.pk))
                            outcome = refund_total_write(
                                current, refund_total=Decimal('10'), reason='企微退款', request_id='split-race',
                                ctx=WriteContext(log_actor='企微同步'), dry_run=False, allow_external_refund=True,
                            )
                            results.append(outcome.ok)
                    except DatabaseError:
                        results.append('busy')
            except Exception as exc:
                results.append(exc)
            finally:
                child_locked.set()
                connections.close_all()
        def price():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    Order.objects.select_for_update().get(pk=main.pk)
                    main_locked.set()
                    if not child_locked.wait(10):
                        raise AssertionError('退款没有完成非阻塞尝试')
                    current = Order.objects.get(pk=child.pk)
                    outcome = change_price(current, total_price=Decimal('35'), reason='合成改价',
                                           ctx=WriteContext(user=admin, actor='财务', log_actor='财务'),
                                           dry_run=False)
                    results.append(outcome.ok)
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()
        threads = [threading.Thread(target=refund), threading.Thread(target=price)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ['busy', True])

    def test_child_refund_does_not_block_group_deletion(self):
        from apps.business.recycle import soft_delete_order

        game = Game.objects.create(name='拆单删除并发游戏')
        admin = User.objects.create_superuser(username='synthetic-delete-admin', password='synthetic')
        grant_games(admin, game)
        main = Order.objects.create(game=game, order_no='99990000000000000007',
                                    status=OrderStatus.COMPLETED, total_price=Decimal('60'),
                                    split_id='synthetic-delete-group', split_kind='LEGACY')
        child = Order.objects.create(game=game, order_no='99990000000000000008',
                                     status=OrderStatus.COMPLETED, total_price=Decimal('40'),
                                     split_id='synthetic-delete-group', split_kind='LEGACY')
        locked, attempted = threading.Event(), threading.Event()
        results = []
        def delete():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    Order.objects.select_for_update().get(pk=main.pk)
                    locked.set()
                    if not attempted.wait(10):
                        raise AssertionError('企微同步未完成非阻塞尝试')
                    soft_delete_order(order=Order.objects.get(pk=child.pk), actor=admin, is_del_emp='N')
                results.append('deleted')
            except Exception as exc:
                results.append(exc)
            finally:
                connections.close_all()
        def refund():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name):
                    if not locked.wait(10):
                        raise AssertionError('删除事务未锁住主单')
                    try:
                        with transaction.atomic():
                            lock_order_and_sales_nowait(Order.objects.get(pk=child.pk))
                    except DatabaseError:
                        results.append('busy')
            except Exception as exc:
                results.append(exc)
            finally:
                attempted.set()
                connections.close_all()
        threads = [threading.Thread(target=delete), threading.Thread(target=refund)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ['deleted', 'busy'])

    def test_manual_child_completion_conflict_returns_busy_and_rolls_back(self):
        game = Game.objects.create(name='拆单人工冲突游戏')
        admin = User.objects.create_superuser(username='synthetic-busy-admin', password='synthetic')
        from apps.business.roles import TENANT_ADMIN
        admin.groups.add(Group.objects.get_or_create(name=TENANT_ADMIN)[0])
        grant_games(admin, game)
        sales = User.objects.create_user(username='synthetic-busy-sales')
        StaffProfile.objects.create(user=sales, commission_mode=CommissionMode.PROFIT_PERCENT,
                                    commission_value=Decimal('20'))
        main = Order.objects.create(game=game, order_no='99990000000000000009',
                                    status=OrderStatus.COMPLETED, total_price=Decimal('100'),
                                    sales_user=sales, split_id='synthetic-busy-group', split_kind='LEGACY')
        child = Order.objects.create(game=game, order_no='99990000000000000010',
                                     status=OrderStatus.PENDING_REVIEW, total_price=Decimal('0'),
                                     sales_user=sales, split_id='synthetic-busy-group', split_kind='LEGACY')
        Performance.objects.create(order=main, emp_type=EmpType.SALES, user=sales, commission_rate=Decimal('20'))
        Performance.objects.create(order=child, emp_type=EmpType.SALES, user=sales)
        entered, release = threading.Event(), threading.Event()
        def hold_main():
            connections.close_all()
            try:
                with schema_context(self.tenant.schema_name), transaction.atomic():
                    Order.objects.select_for_update().get(pk=main.pk)
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('未释放主单锁')
            finally:
                connections.close_all()
        thread = threading.Thread(target=hold_main)
        thread.start()
        self.assertTrue(entered.wait(10))
        try:
            from apps.business.api.order_views import OrderViewSet
            request = APIRequestFactory().post(
                f'/api/business/orders/{child.order_no}/transition/', {'target': 'COMPLETED'}, format='json'
            )
            force_authenticate(request, user=admin)
            response = OrderViewSet.as_view({'post': 'transition'})(request, order_no=child.order_no)
            self.assertEqual(response.status_code, 409, response.data)
            self.assertIn('请稍后重试', response.data['msg'])
            from apps.business.peer_client import PeerBusy, push_events
            from tests._peer_stream_fakes import patch_peer_stream, stream_response
            with patch_peer_stream(stream_response(response.data, status_code=response.status_code)):
                with self.assertRaises(PeerBusy):
                    push_events('https://synthetic.test.invalid', 'synthetic-token', [])
        finally:
            release.set()
            thread.join(15)
            connection.set_tenant(self.tenant)
        self.assertFalse(thread.is_alive())
        child.refresh_from_db()
        self.assertEqual(child.status, OrderStatus.PENDING_REVIEW)
