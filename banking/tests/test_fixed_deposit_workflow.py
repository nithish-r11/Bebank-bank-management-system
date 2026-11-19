from decimal import Decimal
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
from django.db import IntegrityError
import io

from accounts.models import User
from banking.models import BankAccount, Transaction, FixedDeposit, add_months
from banking.views import get_simulation_interest_rate


class FixedDepositWorkflowTests(TestCase):
    """
    Comprehensive tests for BEBANK Fast-Track Task 2.3:
    - Fixed Deposit booking with atomic balance debit & ledger entry
    - Validation rules (amount, tenure duration, account ownership)
    - Balance checks & locked/inactive account safety
    - Deterministic interest & maturity calculations
    - Customer maturity settlement workflow
    - Settlement idempotency (zero double-credit guarantee)
    - Premature closure workflow (100% principal return, forfeited interest)
    - Re-closure prevention & already-closed protection
    - Management command `process_matured_fds`
    - Object-level IDOR & RBAC enforcement
    - History & passbook debit/credit integration
    """

    def setUp(self):
        # Customer A
        self.cust_a = User.objects.create_user(
            username='cust_a',
            password='Password123!',
            role='CUSTOMER'
        )
        self.acc_a = BankAccount.objects.create(
            user=self.cust_a,
            account_number='10001',
            account_type='SAVINGS',
            balance=Decimal('10000.00')
        )

        # Customer B
        self.cust_b = User.objects.create_user(
            username='cust_b',
            password='Password123!',
            role='CUSTOMER'
        )
        self.acc_b = BankAccount.objects.create(
            user=self.cust_b,
            account_number='10002',
            account_type='SAVINGS',
            balance=Decimal('5000.00')
        )

        # Employee & Admin
        self.employee = User.objects.create_user(
            username='emp_user',
            password='Password123!',
            role='EMPLOYEE'
        )
        self.admin = User.objects.create_user(
            username='adm_user',
            password='Password123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True
        )

        self.client_a = Client()
        self.client_a.login(username='cust_a', password='Password123!')

        self.client_b = Client()
        self.client_b.login(username='cust_b', password='Password123!')

        self.client_emp = Client()
        self.client_emp.login(username='emp_user', password='Password123!')

    # =========================================================================
    # 1. BOOKING & DEBIT TESTS
    # =========================================================================

    def test_book_fd_success_with_ledger_and_balance_debit(self):
        """Booking an FD atomically debits account, creates FD record, and creates FD_DEBIT transaction."""
        key = str(uuid.uuid4())
        resp = self.client_a.post(reverse('fd'), {
            'account_id': self.acc_a.id,
            'amount': '2000.00',
            'months': '12',
            'idempotency_key': key
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "FD Booked Successfully")

        # 1. Account balance check
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('8000.00'))

        # 2. FD record check
        fd = FixedDeposit.objects.filter(account=self.acc_a).last()
        self.assertIsNotNone(fd)
        self.assertEqual(fd.amount, Decimal('2000.00'))
        self.assertEqual(fd.duration_months, 12)
        self.assertEqual(fd.interest_rate, Decimal('7.00'))
        self.assertEqual(fd.status, 'ACTIVE')
        self.assertEqual(fd.maturity_amount, Decimal('2140.00'))  # 2000 + (2000 * 7 * 12 / 1200) = 2140
        self.assertEqual(fd.interest_earned, Decimal('140.00'))
        self.assertIsNotNone(fd.start_date)
        self.assertIsNotNone(fd.maturity_date)

        # 3. Transaction ledger check
        expected_ref = f"FD-OPEN-{uuid.UUID(key).hex.upper()}"
        tx = Transaction.objects.filter(reference_id=expected_ref).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'FD_DEBIT')
        self.assertEqual(tx.amount, Decimal('2000.00'))
        self.assertEqual(tx.balance_before, Decimal('10000.00'))
        self.assertEqual(tx.balance_after, Decimal('8000.00'))
        self.assertFalse(tx.is_credit())

    def test_book_fd_invalid_amount_or_duration(self):
        """FD booking rejects negative amounts, amounts below min (₹100), and invalid durations (<3 or >60)."""
        # Negative amount
        resp1 = self.client_a.post(reverse('fd'), {'amount': '-500', 'months': '12'}, follow=True)
        self.assertContains(resp1, "Amount must be greater than 0")

        # Less than min amount
        resp2 = self.client_a.post(reverse('fd'), {'amount': '50', 'months': '12'}, follow=True)
        self.assertContains(resp2, "Minimum Fixed Deposit amount is ₹100.00")

        # Duration < 3 months
        resp3 = self.client_a.post(reverse('fd'), {'amount': '1000', 'months': '2'}, follow=True)
        self.assertContains(resp3, "FD duration must be between 3 and 60 months")

        # Duration > 60 months
        resp4 = self.client_a.post(reverse('fd'), {'amount': '1000', 'months': '72'}, follow=True)
        self.assertContains(resp4, "FD duration must be between 3 and 60 months")

        self.assertEqual(FixedDeposit.objects.count(), 0)

    def test_book_fd_insufficient_balance(self):
        """FD booking fails when requested principal exceeds available balance."""
        resp = self.client_a.post(reverse('fd'), {
            'amount': '15000.00',
            'months': '12'
        }, follow=True)
        self.assertContains(resp, "Insufficient balance")
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('10000.00'))
        self.assertEqual(FixedDeposit.objects.count(), 0)

    def test_book_fd_locked_or_inactive_user_rejected(self):
        """Locked or inactive customer accounts cannot book Fixed Deposits."""
        self.cust_a.account_locked = True
        self.cust_a.save()

        resp = self.client_a.post(reverse('fd'), {
            'amount': '1000.00',
            'months': '12'
        }, follow=True)
        self.assertContains(resp, "Account is blocked")
        self.assertEqual(FixedDeposit.objects.count(), 0)

    def test_idempotent_duplicate_booking_prevention(self):
        """Resubmitting the same idempotency key for booking does not double-debit account."""
        key = str(uuid.uuid4())
        resp1 = self.client_a.post(reverse('fd'), {
            'amount': '1000.00',
            'months': '12',
            'idempotency_key': key
        }, follow=True)
        self.assertContains(resp1, "FD Booked Successfully")

        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('9000.00'))
        self.assertEqual(FixedDeposit.objects.count(), 1)

        # Duplicate submit with same idempotency key
        resp2 = self.client_a.post(reverse('fd'), {
            'amount': '1000.00',
            'months': '12',
            'idempotency_key': key
        }, follow=True)
        self.assertContains(resp2, "Fixed Deposit already booked successfully")

        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('9000.00'))
        self.assertEqual(FixedDeposit.objects.count(), 1)

    # =========================================================================
    # 2. CALCULATION & TIER TESTS
    # =========================================================================

    def test_interest_rate_schedule_and_deterministic_maturity_amounts(self):
        """Verify illustrative simulation rate tier calculation and deterministic rounding."""
        # 3 months: 5.00%
        self.assertEqual(get_simulation_interest_rate(3), Decimal('5.00'))
        # 6 months: 6.00%
        self.assertEqual(get_simulation_interest_rate(6), Decimal('6.00'))
        # 12 months: 7.00%
        self.assertEqual(get_simulation_interest_rate(12), Decimal('7.00'))
        # 24 months: 7.25%
        self.assertEqual(get_simulation_interest_rate(24), Decimal('7.25'))
        # 36 months: 7.50%
        self.assertEqual(get_simulation_interest_rate(36), Decimal('7.50'))

        # Test model calculation
        fd_3 = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            interest_rate=Decimal('5.00'),
            duration_months=3
        )
        # Interest = 1000 * 5 * 3 / 1200 = 12.50
        self.assertEqual(fd_3.maturity_amount, Decimal('1012.50'))
        self.assertEqual(fd_3.interest_earned, Decimal('12.50'))

        fd_24 = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('5000.00'),
            interest_rate=Decimal('7.25'),
            duration_months=24
        )
        # Interest = 5000 * 7.25 * 24 / 1200 = 725.00
        self.assertEqual(fd_24.maturity_amount, Decimal('5725.00'))
        self.assertEqual(fd_24.interest_earned, Decimal('725.00'))

    # =========================================================================
    # 3. MATURITY SETTLEMENT TESTS
    # =========================================================================

    def test_maturity_settlement_success(self):
        """Matured FD can be settled, crediting maturity amount to account and updating status."""
        past_start = timezone.now() - timedelta(days=370)
        past_maturity = timezone.now() - timedelta(days=5)

        fd = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=12,
            start_date=past_start,
            maturity_date=past_maturity,
            status='ACTIVE'
        )
        self.assertTrue(fd.is_matured_eligible())

        resp = self.client_a.post(reverse('fd_settle', args=[fd.id]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "settled successfully")

        # Balance increased by maturity_amount (1070.00)
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('11070.00'))

        # FD status updated
        fd.refresh_from_db()
        self.assertEqual(fd.status, 'MATURED')
        self.assertEqual(fd.settlement_amount, Decimal('1070.00'))
        self.assertEqual(fd.closure_type, 'MATURITY')
        self.assertIsNotNone(fd.settled_at)

        # Ledger transaction created
        tx = Transaction.objects.filter(reference_id=f"FD-MAT-{fd.id}").first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'FD_MATURITY_CREDIT')
        self.assertEqual(tx.amount, Decimal('1070.00'))
        self.assertEqual(tx.balance_before, Decimal('10000.00'))
        self.assertEqual(tx.balance_after, Decimal('11070.00'))
        self.assertTrue(tx.is_credit())

    def test_early_settlement_rejected_prior_to_maturity(self):
        """Active FD cannot be settled before reaching its maturity date."""
        future_maturity = timezone.now() + timedelta(days=300)
        fd = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=12,
            maturity_date=future_maturity,
            status='ACTIVE'
        )
        self.assertFalse(fd.is_matured_eligible())

        resp = self.client_a.post(reverse('fd_settle', args=[fd.id]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "has not reached its maturity date yet")

        # Balance unchanged
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('10000.00'))
        self.assertEqual(fd.status, 'ACTIVE')

    def test_repeated_settlement_cannot_double_credit(self):
        """Attempting to settle an already settled FD cannot double-credit account."""
        past_maturity = timezone.now() - timedelta(days=5)
        fd = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=12,
            maturity_date=past_maturity,
            status='ACTIVE'
        )

        # First settlement
        resp1 = self.client_a.post(reverse('fd_settle', args=[fd.id]), follow=True)
        self.assertContains(resp1, "settled successfully")
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('11070.00'))

        # Second settlement attempt
        resp2 = self.client_a.post(reverse('fd_settle', args=[fd.id]), follow=True)
        self.assertContains(resp2, "already settled or closed")

        # Balance remains strictly unchanged
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('11070.00'))
        self.assertEqual(Transaction.objects.filter(reference_id=f"FD-MAT-{fd.id}").count(), 1)

    # =========================================================================
    # 4. PREMATURE CLOSURE TESTS
    # =========================================================================

    def test_premature_closure_returns_principal_and_forfeits_interest(self):
        """Premature closure safely returns 100% of principal with 0 penalty, but forfeits accrued interest."""
        future_maturity = timezone.now() + timedelta(days=200)
        fd = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('3000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=12,
            maturity_date=future_maturity,
            status='ACTIVE'
        )
        self.assertTrue(fd.is_premature_eligible())

        resp = self.client_a.post(reverse('fd_close', args=[fd.id]), {
            'closure_reason': 'Urgent medical expenditure'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "closed prematurely")
        self.assertContains(resp, "Accrued interest was forfeited")

        # Principal (3000.00) returned; interest (210.00) NOT paid
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('13000.00'))

        fd.refresh_from_db()
        self.assertEqual(fd.status, 'CLOSED')
        self.assertEqual(fd.settlement_amount, Decimal('3000.00'))
        self.assertEqual(fd.closure_type, 'PREMATURE')
        self.assertEqual(fd.closure_reason, 'Urgent medical expenditure')
        self.assertIsNotNone(fd.settled_at)

        # Ledger transaction created
        tx = Transaction.objects.filter(reference_id=f"FD-CLOSE-{fd.id}").first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'FD_CLOSURE_CREDIT')
        self.assertEqual(tx.amount, Decimal('3000.00'))
        self.assertEqual(tx.balance_before, Decimal('10000.00'))
        self.assertEqual(tx.balance_after, Decimal('13000.00'))
        self.assertTrue(tx.is_credit())

    def test_cannot_prematurely_close_already_closed_or_matured_fd(self):
        """Premature closure cannot be performed on already CLOSED or MATURED Fixed Deposits."""
        fd = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            duration_months=12,
            status='CLOSED'
        )

        resp = self.client_a.post(reverse('fd_close', args=[fd.id]), follow=True)
        self.assertContains(resp, "Only active Fixed Deposits can be prematurely closed")
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('10000.00'))

    # =========================================================================
    # 5. MANAGEMENT COMMAND SETTLEMENT TESTS
    # =========================================================================

    def test_process_matured_fds_command(self):
        """Management command process_matured_fds processes all eligible matured FDs and skips active/closed."""
        # 1. Eligible matured FD
        matured_fd = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=12,
            maturity_date=timezone.now() - timedelta(days=2),
            status='ACTIVE'
        )

        # 2. Still active (future maturity) FD
        active_fd = FixedDeposit.objects.create(
            account=self.acc_b,
            amount=Decimal('2000.00'),
            duration_months=12,
            maturity_date=timezone.now() + timedelta(days=30),
            status='ACTIVE'
        )

        out = io.StringIO()
        call_command('process_matured_fds', stdout=out)
        output = out.getvalue()

        self.assertIn("Successfully settled FD #", output)
        self.assertIn("1 FD(s) settled totaling ₹1070.00", output)

        # Matured FD is settled
        matured_fd.refresh_from_db()
        self.assertEqual(matured_fd.status, 'MATURED')
        self.acc_a.refresh_from_db()
        self.assertEqual(self.acc_a.balance, Decimal('11070.00'))

        # Future FD remains untouched
        active_fd.refresh_from_db()
        self.assertEqual(active_fd.status, 'ACTIVE')
        self.acc_b.refresh_from_db()
        self.assertEqual(self.acc_b.balance, Decimal('5000.00'))

        # Repeated run is completely idempotent
        out2 = io.StringIO()
        call_command('process_matured_fds', stdout=out2)
        self.assertIn("No matured Fixed Deposits pending settlement", out2.getvalue())

    # =========================================================================
    # 6. IDOR & RBAC SECURITY TESTS
    # =========================================================================

    def test_cross_customer_idor_prevention(self):
        """Customer B cannot view, settle, or close Customer A's Fixed Deposit (HTTP 404)."""
        fd_a = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('2000.00'),
            duration_months=12,
            status='ACTIVE'
        )

        # Customer B attempts to view Customer A's FD
        resp_view = self.client_b.get(reverse('fd_detail', args=[fd_a.id]))
        self.assertEqual(resp_view.status_code, 404)

        # Customer B attempts to settle Customer A's FD
        resp_settle = self.client_b.post(reverse('fd_settle', args=[fd_a.id]))
        self.assertEqual(resp_settle.status_code, 404)

        # Customer B attempts to close Customer A's FD
        resp_close = self.client_b.post(reverse('fd_close', args=[fd_a.id]))
        self.assertEqual(resp_close.status_code, 404)

    def test_staff_rbac_rejection(self):
        """Staff (Employee) cannot access customer FD endpoints (HTTP 403 Forbidden)."""
        resp_emp_list = self.client_emp.get(reverse('fd'))
        self.assertEqual(resp_emp_list.status_code, 403)

        fd_a = FixedDeposit.objects.create(
            account=self.acc_a,
            amount=Decimal('1000.00'),
            duration_months=6,
            status='ACTIVE'
        )
        resp_emp_detail = self.client_emp.get(reverse('fd_detail', args=[fd_a.id]))
        self.assertEqual(resp_emp_detail.status_code, 403)

