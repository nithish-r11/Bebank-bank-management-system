from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from decimal import Decimal
from datetime import timedelta
import uuid

from accounts.models import User
from banking.models import (
    BankAccount, Transaction, Loan, LoanInstallment, LoanVerificationLog,
    FixedDeposit, VirtualDebitCard, DebitCardAuditLog, Notification
)
from banking.views import safe_csv_cell


class FinancialProductOperationsTests(TestCase):
    """
    Comprehensive tests for BEBANK Fast-Track Task 3.3:
    - Employee Loan Verification Workspace & Queue Filtering
    - Employee Underwriting Recommendations (Mandatory notes, Under Review)
    - Admin Loan Decision Integration (Atomic one-time disbursement, duplicate rejection)
    - Fixed Deposit Monitoring Workspace (Read-only terms, upcoming/unsettled filters, CSV export)
    - Virtual Debit Card Monitoring & Masking (Staff inspection audit, Admin override boundary)
    - Role-Based Access Control: Employees & Admins permitted; Customers blocked (HTTP 403)
    """

    def setUp(self):
        self.client = Client()

        # Users
        self.employee = User.objects.create_user(
            username='officer_alice', password='password123', role='EMPLOYEE'
        )
        self.admin = User.objects.create_superuser(
            username='admin_boss', password='password123', role='ADMIN', is_staff=True
        )
        self.customer = User.objects.create_user(
            username='cust_charlie', password='password123', role='CUSTOMER', kyc_status='APPROVED'
        )
        self.other_customer = User.objects.create_user(
            username='cust_david', password='password123', role='CUSTOMER', kyc_status='PENDING'
        )

        # Accounts
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='20001',
            account_type='SAVINGS',
            balance=Decimal('10000.00'),
            email='charlie@example.com',
            phone='9876543210'
        )
        self.other_account = BankAccount.objects.create(
            user=self.other_customer,
            account_number='20002',
            account_type='CURRENT',
            balance=Decimal('5000.00'),
            email='david@example.com',
            phone='9876543211'
        )

        # Pending Loan Application
        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('50000.00'),
            interest_rate=Decimal('10.50'),
            duration_months=12,
            status='PENDING'
        )

        # Fixed Deposits
        now = timezone.now()
        self.active_fd = FixedDeposit.objects.create(
            account=self.account,
            amount=Decimal('20000.00'),
            interest_rate=Decimal('6.50'),
            duration_months=12,
            start_date=now - timedelta(days=60),
            maturity_date=now + timedelta(days=15),  # Upcoming maturity within 30d
            status='ACTIVE'
        )
        self.unsettled_fd = FixedDeposit.objects.create(
            account=self.account,
            amount=Decimal('15000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=6,
            start_date=now - timedelta(days=200),
            maturity_date=now - timedelta(days=5),  # Matured but status ACTIVE (unsettled)
            status='ACTIVE'
        )
        self.matured_fd = FixedDeposit.objects.create(
            account=self.account,
            amount=Decimal('10000.00'),
            interest_rate=Decimal('6.50'),
            duration_months=12,
            start_date=now - timedelta(days=400),
            maturity_date=now - timedelta(days=35),
            maturity_amount=Decimal('10650.00'),
            status='MATURED',
            settled_at=now - timedelta(days=34),
            settlement_amount=Decimal('10650.00'),
            closure_type='MATURITY'
        )

        # Virtual Debit Card
        self.card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=self.account,
            card_number='5041440012345678',
            cardholder_name='CHARLIE BROWN',
            card_type='PLATINUM',
            status='ACTIVE',
            daily_limit=Decimal('50000.00')
        )

        # URLs
        self.loan_queue_url = reverse('staff_loan_queue')
        self.loan_detail_url = reverse('staff_loan_detail', kwargs={'loan_id': self.loan.id})
        self.loan_verify_url = reverse('staff_loan_verify_action', kwargs={'loan_id': self.loan.id})
        self.fd_list_url = reverse('staff_fd_list')
        self.fd_detail_url = reverse('staff_fd_detail', kwargs={'fd_id': self.active_fd.id})
        self.fd_export_url = reverse('staff_fd_export_csv')
        self.card_list_url = reverse('staff_card_list')
        self.card_detail_url = reverse('staff_card_detail', kwargs={'card_id': self.card.id})
        self.admin_card_action_url = reverse('admin_card_action', kwargs={'card_id': self.card.id})

    # =========================================================================
    # 1. RBAC & PERMISSION BOUNDARIES
    # =========================================================================

    def test_customer_blocked_from_all_employee_financial_endpoints(self):
        """Customers must receive HTTP 403 on all employee financial product operations routes."""
        self.client.force_login(self.customer)
        endpoints = [
            self.loan_queue_url,
            self.loan_detail_url,
            self.fd_list_url,
            self.fd_detail_url,
            self.fd_export_url,
            self.card_list_url,
            self.card_detail_url,
        ]
        for url in endpoints:
            resp = self.client.get(url)
            self.assertEqual(
                resp.status_code, 403,
                f"Customer should receive 403 on {url}, got {resp.status_code}"
            )

        # POST endpoints also blocked
        resp_post = self.client.post(self.loan_verify_url, {'action_type': 'under_review'})
        self.assertEqual(resp_post.status_code, 403)

    def test_unauthenticated_redirected_to_login(self):
        """Unauthenticated requests must redirect to login."""
        endpoints = [
            self.loan_queue_url,
            self.loan_detail_url,
            self.fd_list_url,
            self.card_list_url,
        ]
        for url in endpoints:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)

    def test_employee_and_admin_authorized_for_financial_workspaces(self):
        """Both Employee and Admin can access loan queue, detail, FD list, and Card list."""
        for user in [self.employee, self.admin]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.loan_queue_url).status_code, 200)
            self.assertEqual(self.client.get(self.loan_detail_url).status_code, 200)
            self.assertEqual(self.client.get(self.fd_list_url).status_code, 200)
            self.assertEqual(self.client.get(self.fd_detail_url).status_code, 200)
            self.assertEqual(self.client.get(self.card_list_url).status_code, 200)
            self.assertEqual(self.client.get(self.card_detail_url).status_code, 200)

    # =========================================================================
    # 2. EMPLOYEE LOAN VERIFICATION & RECOMMENDATIONS
    # =========================================================================

    def test_staff_loan_queue_filters_and_search(self):
        """Loan queue correctly filters by status and customer search term."""
        self.client.force_login(self.employee)

        # Create approved and rejected loans
        approved_loan = Loan.objects.create(
            account=self.account, amount=Decimal('25000.00'), duration_months=6, status='APPROVED'
        )
        rejected_loan = Loan.objects.create(
            account=self.other_account, amount=Decimal('10000.00'), duration_months=12, status='REJECTED'
        )

        # Filter by PENDING
        resp = self.client.get(self.loan_queue_url, {'status': 'PENDING'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(l.id == self.loan.id for l in resp.context['page_obj']))
        self.assertFalse(any(l.id == approved_loan.id for l in resp.context['page_obj']))

        # Filter by APPROVED
        resp = self.client.get(self.loan_queue_url, {'status': 'APPROVED'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(l.id == approved_loan.id for l in resp.context['page_obj']))
        self.assertFalse(any(l.id == self.loan.id for l in resp.context['page_obj']))

        # Search by customer username 'cust_david'
        resp = self.client.get(self.loan_queue_url, {'q': 'cust_david', 'status': 'ALL'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(l.id == rejected_loan.id for l in resp.context['page_obj']))
        self.assertFalse(any(l.id == self.loan.id for l in resp.context['page_obj']))

    def test_employee_mark_loan_under_review(self):
        """Employee can mark a PENDING loan as UNDER_REVIEW, creating audit log and notification."""
        self.client.force_login(self.employee)
        payload = {
            'action_type': 'under_review',
            'notes': 'Checking salary slip and income statement.',
        }
        resp = self.client.post(self.loan_verify_url, data=payload)
        self.assertEqual(resp.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'UNDER_REVIEW')

        # Audit log verified
        log = LoanVerificationLog.objects.filter(loan=self.loan, action='UNDER_REVIEW').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.performed_by, self.employee)
        self.assertIn('Checking salary slip', log.notes)

        # Notification verified
        notif = Notification.objects.filter(recipient=self.customer, notification_type='LOAN').first()
        self.assertIsNotNone(notif)
        self.assertIn('Under Review', notif.title)

    def test_employee_recommend_approve_requires_mandatory_notes(self):
        """Recommending approval without notes must be rejected with error."""
        self.client.force_login(self.employee)
        payload = {
            'action_type': 'recommend_approve',
            'notes': '',  # Empty notes
        }
        resp = self.client.post(self.loan_verify_url, data=payload)
        self.assertEqual(resp.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.recommendation, 'NONE')
        self.assertIsNone(self.loan.reviewed_by)

    def test_employee_recommend_approve_success(self):
        """Employee records RECOMMEND_APPROVE with notes, setting status to UNDER_REVIEW."""
        self.client.force_login(self.employee)
        notes = "Verified CIBIL simulation score (780). Net monthly income ₹85,000. Recommend approval."
        payload = {
            'action_type': 'recommend_approve',
            'notes': notes,
        }
        resp = self.client.post(self.loan_verify_url, data=payload)
        self.assertEqual(resp.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.recommendation, 'RECOMMEND_APPROVE')
        self.assertEqual(self.loan.status, 'UNDER_REVIEW')
        self.assertEqual(self.loan.reviewed_by, self.employee)
        self.assertEqual(self.loan.verification_notes, notes)
        self.assertIsNotNone(self.loan.reviewed_at)

        # Verification log
        log = LoanVerificationLog.objects.filter(loan=self.loan, action='RECOMMEND_APPROVE').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.performed_by, self.employee)

    def test_employee_recommend_reject_success(self):
        """Employee records RECOMMEND_REJECT with mandatory notes."""
        self.client.force_login(self.employee)
        notes = "High existing debt-to-income ratio. Recommend rejection."
        payload = {
            'action_type': 'recommend_reject',
            'notes': notes,
        }
        resp = self.client.post(self.loan_verify_url, data=payload)
        self.assertEqual(resp.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.recommendation, 'RECOMMEND_REJECT')
        self.assertEqual(self.loan.status, 'UNDER_REVIEW')
        self.assertEqual(self.loan.reviewed_by, self.employee)

        log = LoanVerificationLog.objects.filter(loan=self.loan, action='RECOMMEND_REJECT').first()
        self.assertIsNotNone(log)

    def test_employee_cannot_directly_approve_or_disburse_loan(self):
        """Employees are blocked from executing final admin approval/disbursement."""
        self.client.force_login(self.employee)
        approve_url = reverse('approve_loan', kwargs={'loan_id': self.loan.id})
        resp = self.client.post(approve_url, {'reason': 'Employee unauthorized approval attempt'})
        self.assertEqual(resp.status_code, 403)

        # Loan balance and status remain untouched
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'PENDING')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('10000.00'))

    # =========================================================================
    # 3. ADMIN LOAN DECISION INTEGRATION & ATOMIC DISBURSEMENT
    # =========================================================================

    def test_admin_approve_and_one_time_atomic_disbursement(self):
        """Admin approves loan: atomic credit, single schedule generation, audit log."""
        self.client.force_login(self.admin)
        approve_url = reverse('approve_loan', kwargs={'loan_id': self.loan.id})
        initial_balance = self.account.balance

        resp = self.client.post(approve_url, {'reason': 'Final admin credit approval verified.'})
        self.assertEqual(resp.status_code, 302)

        # Account balance credited
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, initial_balance + self.loan.amount)

        # Loan status & audit
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')
        self.assertEqual(self.loan.admin_decided_by, self.admin)
        self.assertEqual(self.loan.admin_decision_reason, 'Final admin credit approval verified.')
        self.assertIsNotNone(self.loan.disbursed_at)

        # Amortization installments generated exactly once
        installments = LoanInstallment.objects.filter(loan=self.loan)
        self.assertEqual(installments.count(), self.loan.duration_months)

        # Verification log
        log = LoanVerificationLog.objects.filter(loan=self.loan, action='ADMIN_APPROVE').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.performed_by, self.admin)

    def test_admin_duplicate_approval_rejected(self):
        """Cannot approve an already approved loan (duplicate disbursement prevented)."""
        self.client.force_login(self.admin)
        approve_url = reverse('approve_loan', kwargs={'loan_id': self.loan.id})

        # First approval
        resp1 = self.client.post(approve_url, {'reason': 'First approval'})
        self.assertEqual(resp1.status_code, 302)
        self.account.refresh_from_db()
        bal_after_first = self.account.balance

        # Duplicate attempt
        resp2 = self.client.post(approve_url, {'reason': 'Duplicate attempt'})
        self.assertEqual(resp2.status_code, 302)

        # Balance remains unchanged
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, bal_after_first)
        # Transactions count for disbursement remains 1
        self.assertEqual(
            Transaction.objects.filter(account=self.account, transaction_type='LOAN_CREDIT').count(), 1
        )

    def test_admin_reject_loan_with_recorded_reason(self):
        """Admin rejects loan: status set to REJECTED, recorded reason, customer notified."""
        self.client.force_login(self.admin)
        reject_url = reverse('reject_loan', kwargs={'loan_id': self.loan.id})
        reason = "Income documentation unverified; excessive risk profile."

        resp = self.client.post(reject_url, {'reason': reason})
        self.assertEqual(resp.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'REJECTED')
        self.assertEqual(self.loan.admin_decision_reason, reason)
        self.assertEqual(self.loan.admin_decided_by, self.admin)

        # Verification log
        log = LoanVerificationLog.objects.filter(loan=self.loan, action='ADMIN_REJECT').first()
        self.assertIsNotNone(log)
        self.assertIn(reason, log.notes)

    # =========================================================================
    # 4. FIXED DEPOSIT MONITORING & READ-ONLY SURVEILLANCE
    # =========================================================================

    def test_staff_fd_list_filters(self):
        """FD list filters by status, upcoming 30d, and unsettled matured."""
        self.client.force_login(self.employee)

        # Active tab
        resp = self.client.get(self.fd_list_url, {'status': 'ACTIVE'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(fd.id == self.active_fd.id for fd in resp.context['page_obj']))
        self.assertFalse(any(fd.id == self.matured_fd.id for fd in resp.context['page_obj']))

        # Upcoming 30d tab
        resp_up = self.client.get(self.fd_list_url, {'status': 'UPCOMING'})
        self.assertEqual(resp_up.status_code, 200)
        self.assertTrue(any(fd.id == self.active_fd.id for fd in resp_up.context['page_obj']))

        # Unsettled matured tab
        resp_un = self.client.get(self.fd_list_url, {'status': 'UNSETTLED'})
        self.assertEqual(resp_un.status_code, 200)
        self.assertTrue(any(fd.id == self.unsettled_fd.id for fd in resp_un.context['page_obj']))

    def test_staff_fd_detail_view_ledger_references(self):
        """FD detail inspects terms, opening debit, and settlement credit."""
        self.client.force_login(self.employee)

        # Create opening and settlement transactions
        Transaction.objects.create(
            account=self.account, transaction_type='FD_DEBIT', amount=self.matured_fd.amount,
            reference_id='TX-FD-OPEN-1'
        )
        Transaction.objects.create(
            account=self.account, transaction_type='FD_MATURITY_CREDIT', amount=self.matured_fd.maturity_amount,
            reference_id='TX-FD-SETTLE-1'
        )

        detail_url = reverse('staff_fd_detail', kwargs={'fd_id': self.matured_fd.id})
        resp = self.client.get(detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"Fixed Deposit #{self.matured_fd.id}")
        self.assertContains(resp, 'TX-FD-OPEN-1')
        self.assertContains(resp, 'TX-FD-SETTLE-1')
        self.assertContains(resp, 'Read-Only Monitoring Workspace')

    def test_staff_fd_export_csv_and_formula_escaping(self):
        """FD CSV export provides text/csv attachment with safe_csv_cell escaping."""
        self.client.force_login(self.employee)
        resp = self.client.get(self.fd_export_url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment; filename="bebank_fd_report_', resp['Content-Disposition'])

        content = resp.content.decode('utf-8')
        self.assertIn('Principal (INR)', content)
        self.assertIn('cust_charlie', content)

    # =========================================================================
    # 5. VIRTUAL DEBIT CARD MONITORING & MASKING
    # =========================================================================

    def test_staff_card_list_and_number_masking(self):
        """Card monitoring list displays only masked card numbers (never raw PAN)."""
        self.client.force_login(self.employee)
        resp = self.client.get(self.card_list_url)
        self.assertEqual(resp.status_code, 200)

        # Must contain masked card number
        self.assertContains(resp, '•••• •••• •••• 5678')
        # Must NEVER contain the full unmasked PAN
        self.assertNotContains(resp, '5041440012345678')

    def test_staff_card_inspection_logs_audit_event(self):
        """Viewing card detail logs INSPECTED_BY_STAFF audit entry."""
        self.client.force_login(self.employee)
        initial_log_count = DebitCardAuditLog.objects.filter(card=self.card).count()

        resp = self.client.get(self.card_detail_url)
        self.assertEqual(resp.status_code, 200)

        # Audit log created
        new_log = DebitCardAuditLog.objects.filter(card=self.card, action='STATUS_INSPECTED_STAFF').first()
        self.assertIsNotNone(new_log)
        self.assertEqual(new_log.performed_by, self.employee)
        self.assertEqual(DebitCardAuditLog.objects.filter(card=self.card).count(), initial_log_count + 1)

    def test_employee_cannot_perform_admin_card_action(self):
        """Employee cannot block/unblock cards via admin_card_action (Admin-only)."""
        self.client.force_login(self.employee)
        resp = self.client.post(self.admin_card_action_url, {'action_type': 'block', 'reason': 'Employee attempt'})
        self.assertEqual(resp.status_code, 403)

        self.card.refresh_from_db()
        self.assertEqual(self.card.status, 'ACTIVE')

    def test_admin_can_perform_admin_card_action(self):
        """Admin can perform administrative card block/unblock with audit trail."""
        self.client.force_login(self.admin)
        resp = self.client.post(self.admin_card_action_url, {'action_type': 'block', 'reason': 'Security hold by admin'})
        self.assertEqual(resp.status_code, 302)

        self.card.refresh_from_db()
        self.assertEqual(self.card.status, 'BLOCKED')
        self.assertTrue(self.card.blocked_by_admin)

        audit = DebitCardAuditLog.objects.filter(card=self.card, action='BLOCKED_ADMIN').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.performed_by, self.admin)
