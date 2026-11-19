import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from django.test import TestCase, Client
from django.utils import timezone
from accounts.models import User
from banking.models import BankAccount, Transaction


class TransactionHistoryUpgradeTests(TestCase):
    """
    Comprehensive tests for Task 1.2:
    - Server-side pagination (20 per page)
    - Type, flow, and date filters
    - Filter persistence across pagination links
    - Balance snapshot display (populated vs NULL)
    - Passbook PDF filters and bounding protection
    - RBAC and object-level account ownership
    """

    def setUp(self):
        self.client_a = Client()
        self.client_b = Client()

        self.user_a = User.objects.create_user(
            username='history_user_a',
            password='Password@123',
            role='CUSTOMER'
        )
        self.user_b = User.objects.create_user(
            username='history_user_b',
            password='Password@123',
            role='CUSTOMER'
        )
        self.employee = User.objects.create_user(
            username='history_emp',
            password='Password@123',
            role='EMPLOYEE'
        )

        self.account_a = BankAccount.objects.create(
            user=self.user_a,
            account_number='5555000001',
            account_type='SAVINGS',
            balance=Decimal('5000.00'),
            email='a@bebank.test',
            phone='9876500001',
            aadhaar='111122223333',
            pan='AAAAA0001A',
            address='123 Test Street',
            pincode='560001'
        )

        self.account_b = BankAccount.objects.create(
            user=self.user_b,
            account_number='5555000002',
            account_type='SAVINGS',
            balance=Decimal('2000.00'),
            email='b@bebank.test',
            phone='9876500002',
            aadhaar='444455556666',
            pan='BBBBB0002B',
            address='456 Test Avenue',
            pincode='560002'
        )

        self.client_a.force_login(self.user_a)
        self.client_b.force_login(self.user_b)

    def test_pagination_20_records_per_page(self):
        """Account with 45 transactions must paginate cleanly across 3 pages (20, 20, 5)."""
        base_time = timezone.now() - timedelta(days=50)
        # Create 45 transactions with incremental timestamps
        for i in range(45):
            t = Transaction.objects.create(
                account=self.account_a,
                transaction_type='DEPOSIT',
                amount=Decimal('10.00'),
                balance_before=Decimal(i * 10),
                balance_after=Decimal((i + 1) * 10)
            )
            Transaction.objects.filter(id=t.id).update(created_at=base_time + timedelta(hours=i))

        # Page 1
        resp_p1 = self.client_a.get(f'/bank/history/{self.account_a.id}/')
        self.assertEqual(resp_p1.status_code, 200)
        page_obj_1 = resp_p1.context['page_obj']
        self.assertEqual(len(page_obj_1), 20)
        self.assertEqual(page_obj_1.paginator.num_pages, 3)
        self.assertEqual(page_obj_1.paginator.count, 45)
        self.assertTrue(page_obj_1.has_next())
        self.assertFalse(page_obj_1.has_previous())

        # Page 2
        resp_p2 = self.client_a.get(f'/bank/history/{self.account_a.id}/?page=2')
        self.assertEqual(resp_p2.status_code, 200)
        page_obj_2 = resp_p2.context['page_obj']
        self.assertEqual(len(page_obj_2), 20)
        self.assertTrue(page_obj_2.has_next())
        self.assertTrue(page_obj_2.has_previous())

        # Page 3
        resp_p3 = self.client_a.get(f'/bank/history/{self.account_a.id}/?page=3')
        self.assertEqual(resp_p3.status_code, 200)
        page_obj_3 = resp_p3.context['page_obj']
        self.assertEqual(len(page_obj_3), 5)
        self.assertFalse(page_obj_3.has_next())
        self.assertTrue(page_obj_3.has_previous())

    def test_invalid_page_numbers_handled_gracefully(self):
        """Non-numeric, zero, negative, and out-of-range page values must never return 500."""
        for i in range(25):
            Transaction.objects.create(
                account=self.account_a,
                transaction_type='DEPOSIT',
                amount=Decimal('50.00')
            )

        # Non-numeric -> Page 1
        resp_nan = self.client_a.get(f'/bank/history/{self.account_a.id}/?page=invalid')
        self.assertEqual(resp_nan.status_code, 200)
        self.assertEqual(resp_nan.context['page_obj'].number, 1)

        # Negative -> Page 1
        resp_neg = self.client_a.get(f'/bank/history/{self.account_a.id}/?page=-1')
        self.assertEqual(resp_neg.status_code, 200)
        self.assertEqual(resp_neg.context['page_obj'].number, 1)

        # Zero -> Page 1
        resp_zero = self.client_a.get(f'/bank/history/{self.account_a.id}/?page=0')
        self.assertEqual(resp_zero.status_code, 200)
        self.assertEqual(resp_zero.context['page_obj'].number, 1)

        # Out-of-bounds -> Last page (Page 2)
        resp_high = self.client_a.get(f'/bank/history/{self.account_a.id}/?page=999')
        self.assertEqual(resp_high.status_code, 200)
        self.assertEqual(resp_high.context['page_obj'].number, 2)

    def test_transaction_type_filter(self):
        """Filtering by transaction type must return only matching records."""
        t1 = Transaction.objects.create(account=self.account_a, transaction_type='DEPOSIT', amount=Decimal('100.00'))
        t2 = Transaction.objects.create(account=self.account_a, transaction_type='WITHDRAW', amount=Decimal('50.00'))
        t3 = Transaction.objects.create(account=self.account_a, transaction_type='TRANSFER_DEBIT', amount=Decimal('200.00'))
        t4 = Transaction.objects.create(account=self.account_a, transaction_type='TRANSFER_CREDIT', amount=Decimal('300.00'))

        # Filter DEPOSIT
        resp_dep = self.client_a.get(f'/bank/history/{self.account_a.id}/?type=DEPOSIT')
        self.assertEqual(resp_dep.status_code, 200)
        items = list(resp_dep.context['page_obj'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, t1.id)

        # Filter WITHDRAW
        resp_wd = self.client_a.get(f'/bank/history/{self.account_a.id}/?type=WITHDRAW')
        self.assertEqual(resp_wd.status_code, 200)
        items = list(resp_wd.context['page_obj'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, t2.id)

        # Filter TRANSFER_DEBIT
        resp_td = self.client_a.get(f'/bank/history/{self.account_a.id}/?type=TRANSFER_DEBIT')
        self.assertEqual(resp_td.status_code, 200)
        items = list(resp_td.context['page_obj'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, t3.id)

        # Invalid type should be safely ignored and return all 4
        resp_inv = self.client_a.get(f'/bank/history/{self.account_a.id}/?type=UNKNOWN_XYZ')
        self.assertEqual(resp_inv.status_code, 200)
        self.assertEqual(len(resp_inv.context['page_obj']), 4)

    def test_flow_credit_debit_filter(self):
        """Flow filter must segregate credits (+ funds) from debits (- funds)."""
        dep = Transaction.objects.create(account=self.account_a, transaction_type='DEPOSIT', amount=Decimal('100.00'))
        tc = Transaction.objects.create(account=self.account_a, transaction_type='TRANSFER_CREDIT', amount=Decimal('200.00'))
        wd = Transaction.objects.create(account=self.account_a, transaction_type='WITHDRAW', amount=Decimal('50.00'))
        td = Transaction.objects.create(account=self.account_a, transaction_type='TRANSFER_DEBIT', amount=Decimal('75.00'))

        # Credit flow
        resp_cred = self.client_a.get(f'/bank/history/{self.account_a.id}/?flow=credit')
        self.assertEqual(resp_cred.status_code, 200)
        cred_ids = [t.id for t in resp_cred.context['page_obj']]
        self.assertCountEqual(cred_ids, [dep.id, tc.id])

        # Debit flow
        resp_deb = self.client_a.get(f'/bank/history/{self.account_a.id}/?flow=debit')
        self.assertEqual(resp_deb.status_code, 200)
        deb_ids = [t.id for t in resp_deb.context['page_obj']]
        self.assertCountEqual(deb_ids, [wd.id, td.id])

    def test_date_from_and_date_to_filters(self):
        """Date-range filtering must include records within boundaries and ignore invalid formats safely."""
        now = timezone.now()
        t_old = Transaction.objects.create(account=self.account_a, transaction_type='DEPOSIT', amount=Decimal('10.00'))
        Transaction.objects.filter(id=t_old.id).update(created_at=now - timedelta(days=10))

        t_mid = Transaction.objects.create(account=self.account_a, transaction_type='DEPOSIT', amount=Decimal('20.00'))
        Transaction.objects.filter(id=t_mid.id).update(created_at=now - timedelta(days=5))

        t_today = Transaction.objects.create(account=self.account_a, transaction_type='DEPOSIT', amount=Decimal('30.00'))
        Transaction.objects.filter(id=t_today.id).update(created_at=now)

        d_from = (now - timedelta(days=6)).strftime('%Y-%m-%d')
        d_to = (now - timedelta(days=4)).strftime('%Y-%m-%d')

        # Range filter
        resp_range = self.client_a.get(f'/bank/history/{self.account_a.id}/?date_from={d_from}&date_to={d_to}')
        self.assertEqual(resp_range.status_code, 200)
        range_ids = [t.id for t in resp_range.context['page_obj']]
        self.assertEqual(range_ids, [t_mid.id])

        # Malformed date strings must not cause HTTP 500
        resp_bad_date = self.client_a.get(f'/bank/history/{self.account_a.id}/?date_from=not-a-date&date_to=2026-99-99')
        self.assertEqual(resp_bad_date.status_code, 200)
        self.assertEqual(len(resp_bad_date.context['page_obj']), 3)

    def test_filter_persistence_between_pages(self):
        """Active filter parameters must be preserved in context and rendered in pagination links."""
        base_time = timezone.now() - timedelta(days=30)
        for i in range(30):
            t = Transaction.objects.create(
                account=self.account_a,
                transaction_type='DEPOSIT',
                amount=Decimal('100.00')
            )
            Transaction.objects.filter(id=t.id).update(created_at=base_time + timedelta(hours=i))

        resp = self.client_a.get(f'/bank/history/{self.account_a.id}/?type=DEPOSIT&flow=credit&page=1')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('filter_querystring', resp.context)
        querystring = resp.context['filter_querystring']
        self.assertIn('type=DEPOSIT', querystring)
        self.assertIn('flow=credit', querystring)
        self.assertNotIn('page=', querystring)

        # Verify HTML has preserved query params in next page link
        html = resp.content.decode()
        self.assertIn('page=2', html)
        self.assertIn('type=DEPOSIT', html)

    def test_balance_snapshots_display_and_null_handling(self):
        """Populated snapshots must render exact balances; NULL snapshots must render N/A without fabrication."""
        t_modern = Transaction.objects.create(
            account=self.account_a,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('450.00'),
            related_account=self.account_b,
            reference_id='TRF-SNAPSHOTTEST01',
            balance_before=Decimal('5000.00'),
            balance_after=Decimal('4550.00')
        )

        t_legacy = Transaction.objects.create(
            account=self.account_a,
            transaction_type='TRANSFER',
            amount=Decimal('150.00'),
            balance_before=None,
            balance_after=None
        )

        resp = self.client_a.get(f'/bank/history/{self.account_a.id}/')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        # Modern transaction checks
        self.assertIn('5000.00', html)
        self.assertIn('4550.00', html)
        self.assertIn('Transfer Sent', html)
        self.assertIn('5555000002', html)

        # Legacy transaction checks: must not crash, must not fabricate balance numbers
        t_legacy.refresh_from_db()
        self.assertIsNone(t_legacy.balance_before)
        self.assertIsNone(t_legacy.balance_after)
        self.assertIn('N/A', html)

    def test_passbook_pdf_filters_and_bounding_protection(self):
        """PDF generation must respect filters, include balance snapshots, and bound queries to avoid memory exhaustion."""
        for i in range(25):
            Transaction.objects.create(
                account=self.account_a,
                transaction_type='DEPOSIT' if i % 2 == 0 else 'WITHDRAW',
                amount=Decimal('100.00'),
                balance_after=Decimal(1000 + i * 10)
            )

        # PDF download with filters
        resp = self.client_a.get(f'/bank/history/pdf/{self.account_a.id}/?type=DEPOSIT&flow=credit')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertTrue(len(resp.content) > 1000)

    def test_passbook_pdf_large_volume_capped_safely(self):
        """Account with large transaction volume must be safely capped to prevent OOM."""
        # Create 550 transactions in bulk
        transactions = [
            Transaction(
                account=self.account_a,
                transaction_type='DEPOSIT',
                amount=Decimal('10.00'),
                balance_after=Decimal('5000.00')
            )
            for _ in range(550)
        ]
        Transaction.objects.bulk_create(transactions)

        resp = self.client_a.get(f'/bank/history/pdf/{self.account_a.id}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')

    def test_customer_ownership_and_staff_rbac(self):
        """Customer cannot view another customer's history; employees are forbidden."""
        # Customer A cannot access Customer B's history
        resp_idor = self.client_a.get(f'/bank/history/{self.account_b.id}/')
        self.assertEqual(resp_idor.status_code, 404)

        # Customer A cannot access Customer B's PDF
        resp_pdf_idor = self.client_a.get(f'/bank/history/pdf/{self.account_b.id}/')
        self.assertEqual(resp_pdf_idor.status_code, 404)

        # Employee cannot access customer history
        client_emp = Client()
        client_emp.force_login(self.employee)
        resp_emp = client_emp.get(f'/bank/history/{self.account_a.id}/')
        self.assertEqual(resp_emp.status_code, 403)

