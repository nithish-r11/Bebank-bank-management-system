from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal
import uuid

from accounts.models import User
from banking.models import BankAccount, Transaction, TellerOperationLog, Notification
from banking.views import safe_csv_cell, compute_teller_payload_hash


class TellerOperationsTests(TestCase):
    """
    Comprehensive tests for BEBANK Fast-Track Task 3.2:
    - RBAC: Employee/Admin permitted; Customer blocked (HTTP 403)
    - Account Opening: New customer creation & additional account for existing customer
    - Teller Deposit: Atomicity, row locking, balance snapshots, and UUID idempotency
    - Teller Withdrawal: Overdraft prevention, row locking, snapshots, and UUID idempotency
    - Audit Trail: TellerOperationLog records, receipt generation, history filters
    - CSV Export: Formula injection protection and content verification
    """

    def setUp(self):
        self.client = Client()

        # Users
        self.employee = User.objects.create_user(
            username='teller_alice', password='password123', role='EMPLOYEE'
        )
        self.admin = User.objects.create_superuser(
            username='admin_boss', password='password123', role='ADMIN', is_staff=True
        )
        self.customer = User.objects.create_user(
            username='cust_bob', password='password123', role='CUSTOMER'
        )
        self.locked_customer = User.objects.create_user(
            username='cust_locked', password='password123', role='CUSTOMER', account_locked=True
        )

        # Bank accounts
        self.customer_acc = BankAccount.objects.create(
            user=self.customer,
            account_number='10001',
            account_type='SAVINGS',
            balance=Decimal('5000.00'),
            email='bob@example.com',
            phone='9876543210'
        )
        self.locked_acc = BankAccount.objects.create(
            user=self.locked_customer,
            account_number='10002',
            account_type='SAVINGS',
            balance=Decimal('2000.00')
        )

        # URLs
        self.operations_url = reverse('staff_operations')
        self.deposit_url = reverse('staff_deposit')
        self.withdraw_url = reverse('staff_withdraw')
        self.history_url = reverse('staff_teller_history')
        self.export_url = reverse('staff_teller_export_csv')
        self.create_account_url = reverse('create_account')

    # =========================================================================
    # 1. RBAC & PERMISSION CHECKS
    # =========================================================================

    def test_customer_blocked_from_all_teller_endpoints(self):
        """Customers must receive HTTP 403 Forbidden on all teller operational endpoints."""
        self.client.force_login(self.customer)

        endpoints = [
            self.operations_url,
            self.deposit_url,
            self.withdraw_url,
            self.history_url,
            self.export_url,
            self.create_account_url,
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(
                response.status_code, 403,
                f"Customer should be blocked (403) from {url}, got {response.status_code}"
            )

    def test_unauthenticated_redirected_to_login(self):
        """Unauthenticated requests must be redirected to login."""
        endpoints = [
            self.operations_url,
            self.deposit_url,
            self.withdraw_url,
            self.history_url,
            self.export_url,
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"Unauthenticated request to {url} should redirect")

    def test_employee_and_admin_authorized(self):
        """Employees and Admins can access operations dashboard and teller forms."""
        for user in [self.employee, self.admin]:
            self.client.force_login(user)
            resp = self.client.get(self.operations_url)
            self.assertEqual(resp.status_code, 200)
            self.assertTemplateUsed(resp, 'staff_operations.html')

            resp = self.client.get(self.deposit_url)
            self.assertEqual(resp.status_code, 200)

            resp = self.client.get(self.withdraw_url)
            self.assertEqual(resp.status_code, 200)

    # =========================================================================
    # 2. EMPLOYEE ACCOUNT OPENING
    # =========================================================================

    def test_create_account_for_new_customer(self):
        """Employee can onboard a new customer with initial balance and log audit record."""
        self.client.force_login(self.employee)
        payload = {
            'customer_mode': 'new',
            'username': 'fresh_cust',
            'password': 'TempPassword123!',
            'account_type': 'SAVINGS',
            'balance': '1500.00',
            'email': 'fresh@example.com',
            'phone': '9876543211',
            'aadhaar': '111122223333',
            'pan': 'ABCDE1111F',
            'address': 'MG Road, Bangalore',
            'pincode': '560001',
        }
        response = self.client.post(self.create_account_url, data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'account_details.html')

        new_user = User.objects.filter(username='fresh_cust').first()
        self.assertIsNotNone(new_user)
        self.assertEqual(new_user.role, 'CUSTOMER')

        acc = BankAccount.objects.filter(user=new_user).first()
        self.assertIsNotNone(acc)
        self.assertEqual(acc.balance, Decimal('1500.00'))

        # Initial deposit transaction created
        init_tx = Transaction.objects.filter(account=acc, transaction_type='DEPOSIT').first()
        self.assertIsNotNone(init_tx)
        self.assertEqual(init_tx.amount, Decimal('1500.00'))

        # TellerOperationLog recorded
        op_log = TellerOperationLog.objects.filter(customer=new_user, operation_type='ACCOUNT_OPENED').first()
        self.assertIsNotNone(op_log)
        self.assertEqual(op_log.employee, self.employee)
        self.assertEqual(op_log.amount, Decimal('1500.00'))

        # Notification sent to customer
        notif = Notification.objects.filter(recipient=new_user, notification_type='ACCOUNT').first()
        self.assertIsNotNone(notif)
        self.assertIn('Bank Account Opened', notif.title)

    def test_create_additional_account_for_existing_customer(self):
        """Employee can open an additional account for an existing eligible customer."""
        self.client.force_login(self.employee)
        payload = {
            'customer_mode': 'existing',
            'existing_username': self.customer.username,
            'account_type': 'CURRENT',
            'balance': '500.00',
        }
        response = self.client.post(self.create_account_url, data=payload)
        self.assertEqual(response.status_code, 200)

        # Customer now has 2 accounts
        accounts = BankAccount.objects.filter(user=self.customer)
        self.assertEqual(accounts.count(), 2)

        new_acc = accounts.filter(account_type='CURRENT').first()
        self.assertIsNotNone(new_acc)
        self.assertEqual(new_acc.balance, Decimal('500.00'))

        # Teller log created
        op_log = TellerOperationLog.objects.filter(
            customer=self.customer, operation_type='ACCOUNT_OPENED', account=new_acc
        ).first()
        self.assertIsNotNone(op_log)

    def test_create_account_for_locked_customer_rejected(self):
        """Attempting to open an account for a locked customer must be rejected."""
        self.client.force_login(self.employee)
        payload = {
            'customer_mode': 'existing',
            'existing_username': self.locked_customer.username,
            'account_type': 'SAVINGS',
            'balance': '100.00',
        }
        response = self.client.post(self.create_account_url, data=payload)
        self.assertEqual(response.status_code, 302)
        # No new account created
        self.assertEqual(BankAccount.objects.filter(user=self.locked_customer).count(), 1)

    # =========================================================================
    # 3. TELLER DEPOSIT WORKFLOW & IDEMPOTENCY
    # =========================================================================

    def test_teller_deposit_success_and_ledger_snapshots(self):
        """Teller deposit increases balance, saves before/after snapshots, logs audit record, and notifies customer."""
        self.client.force_login(self.employee)
        token = uuid.uuid4()
        initial_balance = self.customer_acc.balance
        deposit_amount = Decimal('1250.50')

        payload = {
            'account_number': self.customer_acc.account_number,
            'amount': str(deposit_amount),
            'notes': 'Over-the-counter cash deposit',
            'idempotency_key': str(token),
        }
        response = self.client.post(self.deposit_url, data=payload)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/staff/teller/receipt/', response.url)

        # Refresh account
        self.customer_acc.refresh_from_db()
        expected_balance = initial_balance + deposit_amount
        self.assertEqual(self.customer_acc.balance, expected_balance)

        # Transaction verification
        tx = Transaction.objects.filter(account=self.customer_acc, transaction_type='DEPOSIT').order_by('-id').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, deposit_amount)
        self.assertEqual(tx.balance_before, initial_balance)
        self.assertEqual(tx.balance_after, expected_balance)

        # TellerOperationLog verification
        op = TellerOperationLog.objects.filter(idempotency_key=token).first()
        self.assertIsNotNone(op)
        self.assertEqual(op.employee, self.employee)
        self.assertEqual(op.customer, self.customer)
        self.assertEqual(op.operation_type, 'DEPOSIT')
        self.assertEqual(op.amount, deposit_amount)
        self.assertEqual(op.status, 'SUCCESS')
        self.assertIn('Over-the-counter', op.description)

        # Notification verification
        notif = Notification.objects.filter(recipient=self.customer, notification_type='TRANSACTION').first()
        self.assertIsNotNone(notif)
        self.assertIn('Deposit Received', notif.title)

    def test_teller_deposit_idempotency_safe_replay(self):
        """Replaying identical deposit request with same idempotency key returns same receipt without double-crediting."""
        self.client.force_login(self.employee)
        token = uuid.uuid4()
        payload = {
            'account_number': self.customer_acc.account_number,
            'amount': '1000.00',
            'idempotency_key': str(token),
        }

        # First request
        resp1 = self.client.post(self.deposit_url, data=payload)
        self.assertEqual(resp1.status_code, 302)
        receipt_url_1 = resp1.url

        self.customer_acc.refresh_from_db()
        bal_after_first = self.customer_acc.balance
        self.assertEqual(bal_after_first, Decimal('6000.00'))

        # Duplicate retry with same token & payload
        resp2 = self.client.post(self.deposit_url, data=payload)
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2.url, receipt_url_1)

        # Balance remains unchanged on replay
        self.customer_acc.refresh_from_db()
        self.assertEqual(self.customer_acc.balance, bal_after_first)
        # Only 1 teller operation log created
        self.assertEqual(TellerOperationLog.objects.filter(idempotency_key=token).count(), 1)

    def test_teller_deposit_idempotency_altered_payload_rejected(self):
        """Reusing an idempotency key with altered amount or account must return HTTP 400 rejection."""
        self.client.force_login(self.employee)
        token = uuid.uuid4()

        # Step 1: Execute original deposit of 500
        payload1 = {
            'account_number': self.customer_acc.account_number,
            'amount': '500.00',
            'idempotency_key': str(token),
        }
        resp1 = self.client.post(self.deposit_url, data=payload1)
        self.assertEqual(resp1.status_code, 302)

        # Step 2: Try reusing same token for 9000
        payload2 = {
            'account_number': self.customer_acc.account_number,
            'amount': '9000.00',
            'idempotency_key': str(token),
        }
        resp2 = self.client.post(self.deposit_url, data=payload2)
        self.assertEqual(resp2.status_code, 400)
        self.assertIn(b"Idempotency token reuse detected", resp2.content)

        # Balance remains at original 5500
        self.customer_acc.refresh_from_db()
        self.assertEqual(self.customer_acc.balance, Decimal('5500.00'))

    def test_teller_deposit_locked_account_rejected(self):
        """Cannot deposit into a locked customer account."""
        self.client.force_login(self.employee)
        payload = {
            'account_number': self.locked_acc.account_number,
            'amount': '200.00',
            'idempotency_key': str(uuid.uuid4()),
        }
        response = self.client.post(self.deposit_url, data=payload)
        self.assertEqual(response.status_code, 302)
        self.locked_acc.refresh_from_db()
        self.assertEqual(self.locked_acc.balance, Decimal('2000.00'))

    def test_teller_deposit_negative_or_invalid_amount_rejected(self):
        """Negative, zero, or non-numeric deposit amounts are rejected."""
        self.client.force_login(self.employee)
        for bad_amount in ['-100', '0', 'abc']:
            payload = {
                'account_number': self.customer_acc.account_number,
                'amount': bad_amount,
                'idempotency_key': str(uuid.uuid4()),
            }
            response = self.client.post(self.deposit_url, data=payload)
            self.assertEqual(response.status_code, 302)

        self.customer_acc.refresh_from_db()
        self.assertEqual(self.customer_acc.balance, Decimal('5000.00'))

    # =========================================================================
    # 4. TELLER WITHDRAWAL WORKFLOW & IDEMPOTENCY
    # =========================================================================

    def test_teller_withdrawal_success_and_ledger_snapshots(self):
        """Teller withdrawal debits balance, records snapshots, creates audit log, and notifies customer."""
        self.client.force_login(self.employee)
        token = uuid.uuid4()
        initial_balance = self.customer_acc.balance
        withdraw_amount = Decimal('1500.00')

        payload = {
            'account_number': self.customer_acc.account_number,
            'amount': str(withdraw_amount),
            'notes': 'Branch counter cash withdrawal',
            'idempotency_key': str(token),
        }
        response = self.client.post(self.withdraw_url, data=payload)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/staff/teller/receipt/', response.url)

        self.customer_acc.refresh_from_db()
        expected_balance = initial_balance - withdraw_amount
        self.assertEqual(self.customer_acc.balance, expected_balance)

        # Transaction verification
        tx = Transaction.objects.filter(account=self.customer_acc, transaction_type='WITHDRAW').order_by('-id').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, withdraw_amount)
        self.assertEqual(tx.balance_before, initial_balance)
        self.assertEqual(tx.balance_after, expected_balance)

        # TellerOperationLog verification
        op = TellerOperationLog.objects.filter(idempotency_key=token).first()
        self.assertIsNotNone(op)
        self.assertEqual(op.operation_type, 'WITHDRAWAL')
        self.assertEqual(op.amount, withdraw_amount)
        self.assertEqual(op.status, 'SUCCESS')

        # Notification verification
        notif = Notification.objects.filter(recipient=self.customer, notification_type='TRANSACTION').first()
        self.assertIsNotNone(notif)
        self.assertIn('Cash Withdrawal', notif.title)

    def test_teller_withdrawal_overdraft_prevention(self):
        """Withdrawal exceeding account balance is rejected without debiting balance."""
        self.client.force_login(self.employee)
        initial_balance = self.customer_acc.balance

        payload = {
            'account_number': self.customer_acc.account_number,
            'amount': str(initial_balance + Decimal('100.00')),
            'idempotency_key': str(uuid.uuid4()),
        }
        response = self.client.post(self.withdraw_url, data=payload)
        self.assertEqual(response.status_code, 302)

        self.customer_acc.refresh_from_db()
        self.assertEqual(self.customer_acc.balance, initial_balance)

    def test_teller_withdrawal_idempotency_safe_replay(self):
        """Replaying identical withdrawal request returns same receipt without double-debiting."""
        self.client.force_login(self.employee)
        token = uuid.uuid4()
        payload = {
            'account_number': self.customer_acc.account_number,
            'amount': '800.00',
            'idempotency_key': str(token),
        }

        # First request
        resp1 = self.client.post(self.withdraw_url, data=payload)
        self.assertEqual(resp1.status_code, 302)
        receipt_url_1 = resp1.url

        self.customer_acc.refresh_from_db()
        bal_after_first = self.customer_acc.balance
        self.assertEqual(bal_after_first, Decimal('4200.00'))

        # Retry with same token & payload
        resp2 = self.client.post(self.withdraw_url, data=payload)
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(resp2.url, receipt_url_1)

        # Balance untouched
        self.customer_acc.refresh_from_db()
        self.assertEqual(self.customer_acc.balance, bal_after_first)
        self.assertEqual(TellerOperationLog.objects.filter(idempotency_key=token).count(), 1)

    def test_teller_withdrawal_idempotency_altered_payload_rejected(self):
        """Reusing withdrawal token with altered amount returns HTTP 400."""
        self.client.force_login(self.employee)
        token = uuid.uuid4()

        payload1 = {
            'account_number': self.customer_acc.account_number,
            'amount': '500.00',
            'idempotency_key': str(token),
        }
        resp1 = self.client.post(self.withdraw_url, data=payload1)
        self.assertEqual(resp1.status_code, 302)

        # Alter amount with same token
        payload2 = {
            'account_number': self.customer_acc.account_number,
            'amount': '2500.00',
            'idempotency_key': str(token),
        }
        resp2 = self.client.post(self.withdraw_url, data=payload2)
        self.assertEqual(resp2.status_code, 400)

        self.customer_acc.refresh_from_db()
        self.assertEqual(self.customer_acc.balance, Decimal('4500.00'))

    def test_teller_withdrawal_locked_account_rejected(self):
        """Cannot withdraw from locked customer account."""
        self.client.force_login(self.employee)
        payload = {
            'account_number': self.locked_acc.account_number,
            'amount': '100.00',
            'idempotency_key': str(uuid.uuid4()),
        }
        response = self.client.post(self.withdraw_url, data=payload)
        self.assertEqual(response.status_code, 302)
        self.locked_acc.refresh_from_db()
        self.assertEqual(self.locked_acc.balance, Decimal('2000.00'))

    # =========================================================================
    # 5. DASHBOARD METRICS & AUDIT HISTORY
    # =========================================================================

    def test_staff_operations_dashboard_real_metrics(self):
        """Operations dashboard reflects live database metrics for today's teller activities."""
        self.client.force_login(self.employee)

        # Record a deposit and a withdrawal today
        Transaction.objects.create(
            account=self.customer_acc, transaction_type='DEPOSIT', amount=Decimal('3000.00')
        )
        Transaction.objects.create(
            account=self.customer_acc, transaction_type='WITHDRAW', amount=Decimal('1000.00')
        )
        TellerOperationLog.objects.create(
            employee=self.employee,
            operation_type='DEPOSIT',
            customer=self.customer,
            account=self.customer_acc,
            account_number_snapshot=self.customer_acc.account_number,
            amount=Decimal('3000.00'),
            reference_id='TLR-DEP-TEST01',
            status='SUCCESS'
        )

        response = self.client.get(self.operations_url)
        self.assertEqual(response.status_code, 200)
        ctx = response.context
        self.assertGreaterEqual(ctx['today_deposits'], Decimal('3000.00'))
        self.assertGreaterEqual(ctx['today_withdrawals'], Decimal('1000.00'))
        self.assertGreaterEqual(ctx['today_total_volume'], Decimal('4000.00'))
        self.assertTrue(len(ctx['recent_operations']) >= 1)

    def test_staff_teller_history_filters(self):
        """Teller history view filters by operation type, search term, and date range."""
        self.client.force_login(self.employee)

        log1 = TellerOperationLog.objects.create(
            employee=self.employee,
            operation_type='DEPOSIT',
            customer=self.customer,
            account=self.customer_acc,
            account_number_snapshot='10001',
            customer_username_snapshot='cust_bob',
            amount=Decimal('500.00'),
            reference_id='TLR-DEP-SEARCHABLE',
            status='SUCCESS',
            description='Test deposit filter'
        )
        log2 = TellerOperationLog.objects.create(
            employee=self.employee,
            operation_type='WITHDRAWAL',
            customer=self.customer,
            account=self.customer_acc,
            account_number_snapshot='10001',
            customer_username_snapshot='cust_bob',
            amount=Decimal('200.00'),
            reference_id='TLR-WTH-SEARCHABLE',
            status='SUCCESS',
            description='Test withdraw filter'
        )

        # Filter by op_type=DEPOSIT
        resp_dep = self.client.get(self.history_url, {'op_type': 'DEPOSIT'})
        self.assertEqual(resp_dep.status_code, 200)
        items_dep = list(resp_dep.context['page_obj'])
        self.assertTrue(any(item.reference_id == log1.reference_id for item in items_dep))
        self.assertFalse(any(item.reference_id == log2.reference_id for item in items_dep))

        # Filter by search query q='TLR-WTH'
        resp_q = self.client.get(self.history_url, {'q': 'TLR-WTH'})
        self.assertEqual(resp_q.status_code, 200)
        items_q = list(resp_q.context['page_obj'])
        self.assertTrue(any(item.reference_id == log2.reference_id for item in items_q))
        self.assertFalse(any(item.reference_id == log1.reference_id for item in items_q))

    def test_staff_teller_receipt_view(self):
        """Teller receipt displays correct operation details and 404 for invalid reference."""
        self.client.force_login(self.employee)
        op = TellerOperationLog.objects.create(
            employee=self.employee,
            operation_type='DEPOSIT',
            customer=self.customer,
            account=self.customer_acc,
            account_number_snapshot=self.customer_acc.account_number,
            customer_username_snapshot=self.customer.username,
            amount=Decimal('750.00'),
            reference_id='TLR-DEP-RECEIPTTEST',
            status='SUCCESS',
            description='Receipt test'
        )

        receipt_url = reverse('staff_teller_receipt', kwargs={'reference_id': op.reference_id})
        response = self.client.get(receipt_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'staff_teller_receipt.html')
        self.assertContains(response, 'TLR-DEP-RECEIPTTEST')
        self.assertContains(response, '750.00')

        # Invalid reference returns 404
        bad_url = reverse('staff_teller_receipt', kwargs={'reference_id': 'NONEXISTENT'})
        bad_resp = self.client.get(bad_url)
        self.assertEqual(bad_resp.status_code, 404)

    # =========================================================================
    # 6. CSV EXPORT & FORMULA INJECTION PREVENTION
    # =========================================================================

    def test_csv_export_content_and_headers(self):
        """CSV export returns text/csv content with correct attachment headers and data rows."""
        self.client.force_login(self.employee)
        TellerOperationLog.objects.create(
            employee=self.employee,
            operation_type='DEPOSIT',
            customer=self.customer,
            account=self.customer_acc,
            account_number_snapshot='10001',
            customer_username_snapshot='cust_bob',
            amount=Decimal('100.00'),
            reference_id='TLR-DEP-CSVTEST',
            status='SUCCESS',
            description='CSV test row'
        )

        response = self.client.get(self.export_url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment; filename="bebank_teller_report_', response['Content-Disposition'])

        content = response.content.decode('utf-8')
        self.assertIn('Reference ID', content)
        self.assertIn('TLR-DEP-CSVTEST', content)
        self.assertIn('cust_bob', content)
        self.assertIn('10001', content)

    def test_safe_csv_cell_prevents_formula_injection(self):
        """safe_csv_cell must prefix formula trigger characters with single quote."""
        malicious_inputs = [
            "=SUM(A1:A10)",
            "+cmd|' /C calc'!A0",
            "-2+3*4",
            "@evil_macro()",
            "\tTAB_COMMAND",
            "\rRETURN_COMMAND",
        ]
        for payload in malicious_inputs:
            escaped = safe_csv_cell(payload)
            self.assertTrue(
                escaped.startswith("'"),
                f"Payload '{payload}' was not escaped. Got '{escaped}'"
            )

        # Benign inputs should remain unescaped
        benign_inputs = ["Normal Text", "12345", "cust_bob", "TLR-DEP-1234"]
        for payload in benign_inputs:
            self.assertEqual(safe_csv_cell(payload), payload)

    def test_compute_teller_payload_hash(self):
        """compute_teller_payload_hash produces consistent SHA-256 digests and detects changes."""
        hash1 = compute_teller_payload_hash('DEPOSIT', '10001', Decimal('500.00'))
        hash2 = compute_teller_payload_hash('DEPOSIT', '10001', Decimal('500.00'))
        hash_diff_amt = compute_teller_payload_hash('DEPOSIT', '10001', Decimal('500.01'))
        hash_diff_acc = compute_teller_payload_hash('DEPOSIT', '10002', Decimal('500.00'))
        hash_diff_op = compute_teller_payload_hash('WITHDRAWAL', '10001', Decimal('500.00'))

        self.assertEqual(hash1, hash2)
        self.assertNotEqual(hash1, hash_diff_amt)
        self.assertNotEqual(hash1, hash_diff_acc)
        self.assertNotEqual(hash1, hash_diff_op)
