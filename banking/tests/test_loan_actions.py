import threading
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, Client, TransactionTestCase
from django.db import connection
from accounts.models import User
from banking.models import BankAccount, Loan, Transaction as BankTx


class LoanActionSecurityTests(TestCase):
    """
    Test suite for administrative loan approval/rejection endpoints:
    - HTTP method restriction (POST only, GET rejected with 405)
    - CSRF protection enforcement
    - Authorization (ADMIN only)
    - State validation (PENDING loans only, no duplicate processing)
    - Atomic rollback on injected failure
    """

    def setUp(self):
        self.csrf_client = Client(enforce_csrf_checks=True)
        self.standard_client = Client()
        self.customer = User.objects.create_user(username='cust_loan', password='pwd', role='CUSTOMER')
        self.employee = User.objects.create_user(username='emp_loan', password='pwd', role='EMPLOYEE')
        self.admin = User.objects.create_superuser(username='adm_loan', password='pwd', role='ADMIN')

        self.account = BankAccount.objects.create(
            user=self.customer,
            account_type='SAVINGS',
            balance=Decimal('500.00')
        )
        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('1000.00'),
            duration_months=12,
            status='PENDING'
        )

    def test_get_to_approve_loan_fails_method_not_allowed(self):
        """GET request to approve_loan must be rejected with HTTP 405 Method Not Allowed."""
        self.standard_client.force_login(self.admin)
        response = self.standard_client.get(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 405)

    def test_get_to_reject_loan_fails_method_not_allowed(self):
        """GET request to reject_loan must be rejected with HTTP 405 Method Not Allowed."""
        self.standard_client.force_login(self.admin)
        response = self.standard_client.get(f'/bank/loan/{self.loan.id}/reject/')
        self.assertEqual(response.status_code, 405)

    def test_post_without_csrf_token_fails(self):
        """Ensure CSRF is strictly enforced against forged POST requests."""
        self.csrf_client.force_login(self.admin)
        response = self.csrf_client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 403)

    def test_post_reject_without_csrf_token_fails(self):
        """Ensure CSRF is strictly enforced against forged rejection POST requests."""
        self.csrf_client.force_login(self.admin)
        response = self.csrf_client.post(f'/bank/loan/{self.loan.id}/reject/')
        self.assertEqual(response.status_code, 403)

    def test_post_with_genuine_csrf_token_succeeds(self):
        """Administrative POST with valid CSRF token and session approves loan and credits account."""
        self.csrf_client.force_login(self.admin)
        get_res = self.csrf_client.get('/admin-dashboard/')
        csrf_token = get_res.cookies['csrftoken'].value

        response = self.csrf_client.post(
            f'/bank/loan/{self.loan.id}/approve/',
            data={'csrfmiddlewaretoken': csrf_token},
            HTTP_REFERER='/admin-dashboard/'
        )
        self.assertEqual(response.status_code, 302)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('1500.00'))
        self.assertTrue(
            BankTx.objects.filter(
                account=self.account,
                transaction_type="LOAN_CREDIT",
                amount=Decimal('1000.00')
            ).exists()
        )

    def test_post_reject_with_genuine_csrf_token_succeeds(self):
        """Administrative POST with valid CSRF token marks loan REJECTED and does not modify balance."""
        self.csrf_client.force_login(self.admin)
        get_res = self.csrf_client.get('/admin-dashboard/')
        csrf_token = get_res.cookies['csrftoken'].value

        response = self.csrf_client.post(
            f'/bank/loan/{self.loan.id}/reject/',
            data={'csrfmiddlewaretoken': csrf_token},
            HTTP_REFERER='/admin-dashboard/'
        )
        self.assertEqual(response.status_code, 302)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'REJECTED')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('500.00'))
        self.assertFalse(BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT").exists())

    def test_non_admin_cannot_approve_or_reject_loans(self):
        """Non-admin roles (CUSTOMER, EMPLOYEE) receive HTTP 403 on loan approval/rejection endpoints."""
        for user in [self.customer, self.employee]:
            self.standard_client.force_login(user)
            resp_app = self.standard_client.post(f'/bank/loan/{self.loan.id}/approve/')
            self.assertEqual(resp_app.status_code, 403)
            resp_rej = self.standard_client.post(f'/bank/loan/{self.loan.id}/reject/')
            self.assertEqual(resp_rej.status_code, 403)

    def test_duplicate_approval_rejected_without_double_crediting(self):
        """Attempting to approve an already approved loan is rejected and balance unchanged."""
        self.loan.status = 'APPROVED'
        self.loan.save()
        self.standard_client.force_login(self.admin)

        response = self.standard_client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('500.00'))
        self.assertFalse(BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT").exists())

    def test_already_rejected_loan_cannot_be_approved(self):
        """Attempting to approve an already rejected loan is rejected."""
        self.loan.status = 'REJECTED'
        self.loan.save()
        self.standard_client.force_login(self.admin)

        response = self.standard_client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('500.00'))
        self.assertFalse(BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT").exists())

    def test_already_processed_loan_cannot_be_rejected_again(self):
        """Attempting to reject an already approved loan is rejected without altering status."""
        self.loan.status = 'APPROVED'
        self.loan.save()
        self.standard_client.force_login(self.admin)

        response = self.standard_client.post(f'/bank/loan/{self.loan.id}/reject/')
        self.assertEqual(response.status_code, 302)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')

    def test_approve_loan_endpoint_rolls_back_completely_on_injected_exception(self):
        """
        Inject an exception inside approve_loan after balance update and transaction creation.
        Assert that loan status, account balance, and transaction records all roll back completely.
        """
        self.standard_client.force_login(self.admin)

        original_save = Loan.save

        def faulty_save(loan_instance, *args, **kwargs):
            if kwargs.get('update_fields') == ['status'] and loan_instance.status == 'APPROVED':
                raise RuntimeError("Simulated database failure during loan status commit!")
            return original_save(loan_instance, *args, **kwargs)

        with patch.object(Loan, 'save', side_effect=faulty_save, autospec=True):
            with self.assertRaises(RuntimeError):
                self.standard_client.post(f'/bank/loan/{self.loan.id}/approve/')

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'PENDING', "Loan status must remain PENDING after rollback.")

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('500.00'), "Account balance must remain 500.00 after rollback.")

        credit_txs = BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT")
        self.assertFalse(credit_txs.exists(), "No LOAN_CREDIT transaction must exist after rollback.")


class MoneyOperationsAtomicSafetyTests(TestCase):
    """
    Test suite verifying atomic transaction boundaries and rollback on withdrawal/transfer failures.
    """

    def setUp(self):
        self.client = Client()
        self.sender_user = User.objects.create_user(username='sender_u', password='pwd', role='CUSTOMER')
        self.receiver_user = User.objects.create_user(username='recv_u', password='pwd', role='CUSTOMER')

        self.sender_acc = BankAccount.objects.create(
            user=self.sender_user,
            account_number='20001',
            account_type='SAVINGS',
            balance=Decimal('1000.00')
        )
        self.receiver_acc = BankAccount.objects.create(
            user=self.receiver_user,
            account_number='20002',
            account_type='SAVINGS',
            balance=Decimal('200.00')
        )

    def test_withdrawal_atomic_rollback_on_transaction_creation_failure(self):
        """
        If transaction record creation fails during withdrawal, the account balance deduction
        must roll back atomically.
        """
        self.client.force_login(self.sender_user)

        with patch.object(BankTx.objects, 'create', side_effect=RuntimeError("Database failure creating tx")):
            with self.assertRaises(RuntimeError):
                self.client.post('/bank/withdraw/', data={
                    'account_id': self.sender_acc.id,
                    'amount': '300.00'
                })

        self.sender_acc.refresh_from_db()
        self.assertEqual(
            self.sender_acc.balance,
            Decimal('1000.00'),
            "Sender balance must roll back to 1000.00 if transaction record creation fails."
        )
        self.assertFalse(BankTx.objects.filter(account=self.sender_acc).exists())

    def test_transfer_atomic_rollback_on_transaction_creation_failure(self):
        """
        If transaction record creation fails during transfer, both sender deduction
        and receiver credit must roll back atomically.
        """
        self.client.force_login(self.sender_user)

        with patch.object(BankTx.objects, 'create', side_effect=RuntimeError("Database failure creating transfer tx")):
            with self.assertRaises(RuntimeError):
                self.client.post('/bank/transfer/', data={
                    'account_id': self.sender_acc.id,
                    'account_number': self.receiver_acc.account_number,
                    'amount': '400.00'
                })

        self.sender_acc.refresh_from_db()
        self.receiver_acc.refresh_from_db()
        self.assertEqual(
            self.sender_acc.balance,
            Decimal('1000.00'),
            "Sender balance must roll back to 1000.00 on transfer failure."
        )
        self.assertEqual(
            self.receiver_acc.balance,
            Decimal('200.00'),
            "Receiver balance must roll back to 200.00 on transfer failure."
        )
        self.assertFalse(BankTx.objects.filter(account=self.sender_acc).exists())

    def test_transfer_to_same_account_rejected(self):
        """Transferring to the exact same account must be rejected without balance alteration."""
        self.client.force_login(self.sender_user)
        response = self.client.post('/bank/transfer/', data={
            'account_id': self.sender_acc.id,
            'account_number': self.sender_acc.account_number,
            'amount': '100.00'
        })
        self.assertEqual(response.status_code, 302)
        self.sender_acc.refresh_from_db()
        self.assertEqual(self.sender_acc.balance, Decimal('1000.00'))
        self.assertFalse(BankTx.objects.filter(account=self.sender_acc).exists())

    def test_withdrawal_insufficient_balance_rejected(self):
        """Withdrawal exceeding account balance is rejected without balance deduction."""
        self.client.force_login(self.sender_user)
        response = self.client.post('/bank/withdraw/', data={
            'account_id': self.sender_acc.id,
            'amount': '2500.00'
        })
        self.assertEqual(response.status_code, 302)
        self.sender_acc.refresh_from_db()
        self.assertEqual(self.sender_acc.balance, Decimal('1000.00'))
        self.assertFalse(BankTx.objects.filter(account=self.sender_acc).exists())


class ConcurrentBankingPostgresTests(TransactionTestCase):
    """
    PostgreSQL Row-Locking Concurrency Test Suite.
    CRITICAL: SQLite does NOT enforce row-level locking (select_for_update is a no-op or table lock in SQLite).
    This test runs against PostgreSQL to verify row serialization under simultaneous threads.
    Threads synchronize via threading.Barrier and manage independent database connections.
    When running under SQLite (local test harness), tests are explicitly skipped with informative messages.
    """

    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest(
                "Row-level locking concurrency verification requires PostgreSQL. "
                "SQLite does not support row-level locking (select_for_update)."
            )
        self.customer = User.objects.create_user(username='pg_cust', password='pwd', role='CUSTOMER')
        self.admin = User.objects.create_superuser(username='pg_admin', password='pwd', role='ADMIN')
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='30001',
            account_type='SAVINGS',
            balance=Decimal('500.00')
        )
        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('1000.00'),
            duration_months=12,
            status='PENDING'
        )

    def test_concurrent_simultaneous_approvals_result_in_exactly_one_credit(self):
        """
        Synchronize simultaneous approval requests with a barrier and independent DB connections per thread.
        Assert exactly one approval, exactly one credit to balance, and no double-crediting.
        """
        thread_errors = []
        status_codes = []
        barrier = threading.Barrier(2)

        def execute_approval():
            from django.db import connection as thread_conn
            thread_conn.close()
            client = Client()
            client.force_login(self.admin)
            try:
                barrier.wait(timeout=5)
                resp = client.post(f'/bank/loan/{self.loan.id}/approve/')
                status_codes.append(resp.status_code)
            except Exception as e:
                thread_errors.append(e)
            finally:
                thread_conn.close()

        t1 = threading.Thread(target=execute_approval)
        t2 = threading.Thread(target=execute_approval)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(thread_errors), 0, f"Unexpected thread errors: {thread_errors}")
        self.assertEqual(len(status_codes), 2)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('1500.00'), "Account balance must be credited exactly once.")

        credit_tx_count = BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT").count()
        self.assertEqual(credit_tx_count, 1, "Exactly one LOAN_CREDIT record must be created.")

    def test_concurrent_simultaneous_withdrawals_prevent_overspend(self):
        """
        Two simultaneous withdrawal requests of â‚¹400 on an account with â‚¹500 balance.
        Under row locking, exactly one withdrawal must succeed and the other must be rejected.
        Account balance must be â‚¹100.00, not -â‚¹300.00.
        """
        thread_errors = []
        status_codes = []
        barrier = threading.Barrier(2)

        def execute_withdrawal():
            from django.db import connection as thread_conn
            thread_conn.close()
            client = Client()
            client.force_login(self.customer)
            try:
                barrier.wait(timeout=5)
                resp = client.post('/bank/withdraw/', data={
                    'account_id': self.account.id,
                    'amount': '400.00'
                })
                status_codes.append(resp.status_code)
            except Exception as e:
                thread_errors.append(e)
            finally:
                thread_conn.close()

        t1 = threading.Thread(target=execute_withdrawal)
        t2 = threading.Thread(target=execute_withdrawal)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(thread_errors), 0, f"Unexpected thread errors: {thread_errors}")
        self.assertEqual(len(status_codes), 2)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('100.00'), "Balance must be exactly 100.00 after 1 successful withdrawal.")
        withdraw_tx_count = BankTx.objects.filter(account=self.account, transaction_type="WITHDRAW").count()
        self.assertEqual(withdraw_tx_count, 1, "Exactly one WITHDRAW record must exist.")

    def test_concurrent_simultaneous_transfers_prevent_overspend(self):
        """
        Two simultaneous transfer requests of â‚¹400 on an account with â‚¹500 balance.
        Under row locking, exactly one transfer must succeed and the other must be rejected.
        Account balance must be â‚¹100.00, not -â‚¹300.00.
        """
        dest_user = User.objects.create_user(username='pg_dest', password='pwd', role='CUSTOMER')
        dest_acc = BankAccount.objects.create(
            user=dest_user,
            account_number='30002',
            account_type='SAVINGS',
            balance=Decimal('0.00')
        )

        thread_errors = []
        status_codes = []
        barrier = threading.Barrier(2)

        def execute_transfer():
            from django.db import connection as thread_conn
            thread_conn.close()
            client = Client()
            client.force_login(self.customer)
            try:
                barrier.wait(timeout=5)
                resp = client.post('/bank/transfer/', data={
                    'account_id': self.account.id,
                    'account_number': dest_acc.account_number,
                    'amount': '400.00'
                })
                status_codes.append(resp.status_code)
            except Exception as e:
                thread_errors.append(e)
            finally:
                thread_conn.close()

        t1 = threading.Thread(target=execute_transfer)
        t2 = threading.Thread(target=execute_transfer)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(thread_errors), 0, f"Unexpected thread errors: {thread_errors}")
        self.assertEqual(len(status_codes), 2)

        self.account.refresh_from_db()
        dest_acc.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('100.00'), "Sender balance must be 100.00 after 1 transfer.")
        self.assertEqual(dest_acc.balance, Decimal('400.00'), "Receiver balance must be 400.00 after 1 transfer.")
        transfer_tx_count = BankTx.objects.filter(account=self.account, transaction_type="TRANSFER").count()
        self.assertEqual(transfer_tx_count, 1, "Exactly one TRANSFER record must exist.")


class BankingFormViewGetPostRegressionTests(TestCase):
    """
    Regression test suite verifying that dual-method banking form views:
    - Return HTTP 200 on GET for authorized users.
    - Render the expected template without modifying database state, balance, or creating transactions.
    - Successfully process state changes on valid POST.
    - Do not allow GET requests to trigger financial mutations even if parameters are attached.
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(username='form_cust', password='pwd', role='CUSTOMER')
        self.employee = User.objects.create_user(username='form_emp', password='pwd', role='EMPLOYEE')
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='50001',
            account_type='SAVINGS',
            balance=Decimal('1000.00')
        )
        self.target_account = BankAccount.objects.create(
            user=self.employee,
            account_number='50002',
            account_type='SAVINGS',
            balance=Decimal('500.00')
        )

    def test_withdraw_view_get_renders_form_without_mutation(self):
        """GET /bank/withdraw/ returns 200 and does NOT alter balance or create transactions."""
        self.client.force_login(self.customer)
        response = self.client.get(f'/bank/withdraw/?account_id={self.account.id}&amount=200')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'withdraw.html')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('1000.00'))
        self.assertFalse(BankTx.objects.filter(account=self.account).exists())

    def test_transfer_view_get_renders_form_without_mutation(self):
        """GET /bank/transfer/ returns 200 and does NOT alter balance or create transactions."""
        self.client.force_login(self.customer)
        response = self.client.get(f'/bank/transfer/?account_id={self.account.id}&account_number=50002&amount=200')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'transfer.html')
        self.account.refresh_from_db()
        self.target_account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('1000.00'))
        self.assertEqual(self.target_account.balance, Decimal('500.00'))
        self.assertFalse(BankTx.objects.filter(account=self.account).exists())

    def test_deposit_view_get_renders_form_without_mutation(self):
        """GET /bank/deposit/ returns 200 for staff and does NOT alter balance or create transactions."""
        self.client.force_login(self.employee)
        response = self.client.get(f'/bank/deposit/?account_number=50001&amount=500')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'deposit.html')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('1000.00'))
        self.assertFalse(BankTx.objects.filter(account=self.account).exists())

    def test_fd_view_get_renders_form_without_mutation(self):
        """GET /bank/fd/ returns 200 and does NOT alter balance or create FixedDeposit records."""
        from banking.models import FixedDeposit
        self.client.force_login(self.customer)
        response = self.client.get(f'/bank/fd/?account_id={self.account.id}&amount=300&months=12')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'fd.html')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('1000.00'))
        self.assertFalse(FixedDeposit.objects.filter(account=self.account).exists())

    def test_loan_view_get_renders_form_without_mutation(self):
        """GET /bank/loan/ returns 200 and does NOT create Loan records."""
        self.client.force_login(self.customer)
        response = self.client.get(f'/bank/loan/?account_id={self.account.id}&amount=500&months=6')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'loan.html')
        self.assertFalse(Loan.objects.filter(account=self.account).exists())
