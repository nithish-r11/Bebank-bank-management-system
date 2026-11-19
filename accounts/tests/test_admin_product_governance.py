from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from banking.models import (
    BankAccount, Transaction, Loan, LoanInstallment, LoanRepayment, LoanVerificationLog,
    FixedDeposit, VirtualDebitCard, DebitCardAuditLog, Notification,
    EmployeeActivityLog, generate_simulation_card_number
)


class AdminProductGovernanceTests(TestCase):
    """
    Comprehensive test suite for BEBANK Fast-Track Task 4.4:
    Admin Loan, Fixed Deposit, Debit Card & Notification Governance.
    """

    def setUp(self):
        self.client = Client(HTTP_HOST='127.0.0.1')

        # 1. Administrator User
        self.admin_user = User.objects.create_user(
            username='admin_governance',
            password='AdminPassword123!',
            email='admin_gov@bebank.local',
            role='ADMIN',
            is_staff=True,
            first_name='Admin',
            last_name='Director'
        )

        # 2. Staff / Employee User
        self.employee_user = User.objects.create_user(
            username='staff_tester',
            password='StaffPassword123!',
            email='staff@bebank.local',
            role='EMPLOYEE',
            first_name='Staff',
            last_name='Reviewer'
        )

        # 3. Customer User
        self.customer_user = User.objects.create_user(
            username='cust_governance',
            password='CustPassword123!',
            email='cust_gov@bebank.local',
            role='CUSTOMER',
            first_name='Priya',
            last_name='Sharma'
        )

        # Customer Account (Active)
        self.customer_account = BankAccount.objects.create(
            user=self.customer_user,
            account_number='BEB1002003001',
            account_type='SAVINGS',
            balance=Decimal('25000.00'),
            status='ACTIVE'
        )

        # Customer Account (Frozen)
        self.frozen_account = BankAccount.objects.create(
            user=self.customer_user,
            account_number='BEB1002003002',
            account_type='CURRENT',
            balance=Decimal('5000.00'),
            status='FROZEN',
            freeze_reason='Suspicious activity review'
        )

    # =========================================================================
    # 1. ADMIN LOAN GOVERNANCE TESTS
    # =========================================================================

    def test_admin_loan_list_access_and_rbac(self):
        """Verify Admin can access loan list while customer and employee are denied 403."""
        # Unauthenticated
        response = self.client.get(reverse('admin_loan_list'))
        self.assertEqual(response.status_code, 302)

        # Customer -> 403
        self.client.force_login(self.customer_user)
        response = self.client.get(reverse('admin_loan_list'))
        self.assertEqual(response.status_code, 403)

        # Employee -> 403
        self.client.force_login(self.employee_user)
        response = self.client.get(reverse('admin_loan_list'))
        self.assertEqual(response.status_code, 403)

        # Admin -> 200
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_loan_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_loan_list.html')

    def test_admin_loan_detail_and_search(self):
        """Verify search, filter, and detail view for loans."""
        loan = Loan.objects.create(
            account=self.customer_account,
            amount=Decimal('50000.00'),
            interest_rate=Decimal('10.50'),
            duration_months=12,
            status='PENDING'
        )

        self.client.force_login(self.admin_user)

        # Search by username
        response = self.client.get(reverse('admin_loan_list'), {'q': 'cust_governance'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'cust_governance')

        # Filter by status
        response = self.client.get(reverse('admin_loan_list'), {'status': 'PENDING'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '₹50000.00')

        # Detail view
        response = self.client.get(reverse('admin_loan_detail', kwargs={'loan_id': loan.id}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_loan_detail.html')
        self.assertContains(response, 'Admin Final Decision Authority')

    def test_admin_loan_approval_and_disbursement_idempotency(self):
        """Verify Admin approval atomically disburses principal, creates transaction, schedule, and is idempotent."""
        loan = Loan.objects.create(
            account=self.customer_account,
            amount=Decimal('100000.00'),
            interest_rate=Decimal('10.50'),
            duration_months=12,
            status='PENDING'
        )
        initial_balance = self.customer_account.balance

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin_loan_approve', kwargs={'loan_id': loan.id}),
            {'reason': 'Approved under annual credit policy.'}
        )
        self.assertEqual(response.status_code, 302)

        # Refresh loan & account
        loan.refresh_from_db()
        self.customer_account.refresh_from_db()

        self.assertEqual(loan.status, 'APPROVED')
        self.assertEqual(loan.admin_decided_by, self.admin_user)
        self.assertEqual(self.customer_account.balance, initial_balance + Decimal('100000.00'))

        # Verify transaction
        tx = Transaction.objects.filter(reference_id=f"LOAN-DISB-{loan.id}").first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'LOAN_CREDIT')
        self.assertEqual(tx.amount, Decimal('100000.00'))

        # Verify Amortization Installments generated
        installments = LoanInstallment.objects.filter(loan=loan)
        self.assertEqual(installments.count(), 12)

        # Verify Audit Log
        verif_log = LoanVerificationLog.objects.filter(loan=loan, action='ADMIN_APPROVE').first()
        self.assertIsNotNone(verif_log)
        act_log = EmployeeActivityLog.objects.filter(target_id=str(loan.id), action_type='LOAN_DECISION_APPROVE').first()
        self.assertIsNotNone(act_log)

        # IDEMPOTENCY: Repeated approval POST should not disburse funds again
        balance_after_first = self.customer_account.balance
        response = self.client.post(
            reverse('admin_loan_approve', kwargs={'loan_id': loan.id}),
            {'reason': 'Duplicate attempt'}
        )
        self.assertEqual(response.status_code, 302)
        self.customer_account.refresh_from_db()
        self.assertEqual(self.customer_account.balance, balance_after_first)

    def test_admin_loan_approval_to_frozen_account(self):
        """Verify Admin loan disbursement is permitted even if target account is frozen."""
        loan = Loan.objects.create(
            account=self.frozen_account,
            amount=Decimal('50000.00'),
            interest_rate=Decimal('10.50'),
            duration_months=6,
            status='PENDING'
        )
        init_balance = self.frozen_account.balance

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin_loan_approve', kwargs={'loan_id': loan.id}),
            {'reason': 'Approved funds for frozen account recovery.'}
        )
        self.assertEqual(response.status_code, 302)

        self.frozen_account.refresh_from_db()
        self.assertEqual(self.frozen_account.balance, init_balance + Decimal('50000.00'))
        self.assertTrue(self.frozen_account.is_frozen)  # Account remains frozen, credit accepted

    def test_admin_loan_rejection(self):
        """Verify Admin loan rejection updates status, records reason, and notifies customer."""
        loan = Loan.objects.create(
            account=self.customer_account,
            amount=Decimal('75000.00'),
            interest_rate=Decimal('10.50'),
            duration_months=12,
            status='PENDING'
        )

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin_loan_reject', kwargs={'loan_id': loan.id}),
            {'reason': 'Insufficient debt-service coverage ratio.'}
        )
        self.assertEqual(response.status_code, 302)

        loan.refresh_from_db()
        self.assertEqual(loan.status, 'REJECTED')
        self.assertEqual(loan.admin_decision_reason, 'Insufficient debt-service coverage ratio.')

        verif_log = LoanVerificationLog.objects.filter(loan=loan, action='ADMIN_REJECT').first()
        self.assertIsNotNone(verif_log)

    # =========================================================================
    # 2. ADMIN FIXED DEPOSIT GOVERNANCE TESTS
    # =========================================================================

    def test_admin_fd_list_access_and_rbac(self):
        """Verify Admin access and RBAC barriers for Fixed Deposit portfolio."""
        # Customer -> 403
        self.client.force_login(self.customer_user)
        response = self.client.get(reverse('admin_fd_list'))
        self.assertEqual(response.status_code, 403)

        # Employee -> 403
        self.client.force_login(self.employee_user)
        response = self.client.get(reverse('admin_fd_list'))
        self.assertEqual(response.status_code, 403)

        # Admin -> 200
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_fd_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_fd_list.html')

    def test_admin_fd_maturity_settlement_and_idempotency(self):
        """Verify Admin maturity settlement credits maturity value and is strictly idempotent."""
        past_date = timezone.now() - timezone.timedelta(days=365)
        fd = FixedDeposit.objects.create(
            account=self.customer_account,
            amount=Decimal('20000.00'),
            interest_rate=Decimal('6.50'),
            duration_months=12,
            start_date=past_date,
            status='ACTIVE'
        )
        payout = fd.maturity_amount
        initial_balance = self.customer_account.balance

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin_fd_settle', kwargs={'fd_id': fd.id}),
            {'notes': 'Administrative annual term settlement.'}
        )
        self.assertEqual(response.status_code, 302)

        fd.refresh_from_db()
        self.customer_account.refresh_from_db()

        self.assertEqual(fd.status, 'MATURED')
        self.assertEqual(self.customer_account.balance, initial_balance + payout)
        self.assertEqual(fd.closure_type, 'MATURITY')

        # Verify Transaction
        tx = Transaction.objects.filter(reference_id=f"FD-MAT-{fd.id}").first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'FD_MATURITY_CREDIT')
        self.assertEqual(tx.amount, payout)

        # Verify EmployeeActivityLog
        act = EmployeeActivityLog.objects.filter(target_id=str(fd.id), action_type='FD_SETTLEMENT').first()
        self.assertIsNotNone(act)

        # IDEMPOTENCY: Repeated settle call should not double credit
        balance_after_settle = self.customer_account.balance
        response = self.client.post(
            reverse('admin_fd_settle', kwargs={'fd_id': fd.id}),
            {'notes': 'Duplicate settlement attempt'}
        )
        self.assertEqual(response.status_code, 302)
        self.customer_account.refresh_from_db()
        self.assertEqual(self.customer_account.balance, balance_after_settle)

    def test_admin_fd_settlement_to_frozen_account(self):
        """Verify Admin FD payout is credited even if the target bank account is frozen."""
        fd = FixedDeposit.objects.create(
            account=self.frozen_account,
            amount=Decimal('10000.00'),
            interest_rate=Decimal('6.50'),
            duration_months=6,
            status='ACTIVE'
        )
        init_balance = self.frozen_account.balance

        self.client.force_login(self.admin_user)
        response = self.client.post(reverse('admin_fd_settle', kwargs={'fd_id': fd.id}))
        self.assertEqual(response.status_code, 302)

        self.frozen_account.refresh_from_db()
        self.assertEqual(self.frozen_account.balance, init_balance + fd.maturity_amount)
        self.assertTrue(self.frozen_account.is_frozen)

    def test_admin_fd_premature_closure(self):
        """Verify Admin premature closure refunds 100% principal and forfeits interest."""
        fd = FixedDeposit.objects.create(
            account=self.customer_account,
            amount=Decimal('15000.00'),
            interest_rate=Decimal('6.50'),
            duration_months=24,
            status='ACTIVE'
        )
        init_balance = self.customer_account.balance

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('admin_fd_close', kwargs={'fd_id': fd.id}),
            {'reason': 'Emergency customer medical withdrawal request.'}
        )
        self.assertEqual(response.status_code, 302)

        fd.refresh_from_db()
        self.customer_account.refresh_from_db()

        self.assertEqual(fd.status, 'CLOSED')
        self.assertEqual(fd.closure_type, 'PREMATURE')
        self.assertEqual(fd.settlement_amount, Decimal('15000.00'))  # Exactly 100% principal
        self.assertEqual(self.customer_account.balance, init_balance + Decimal('15000.00'))

        tx = Transaction.objects.filter(reference_id=f"FD-CLOSE-{fd.id}").first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.transaction_type, 'FD_CLOSURE_CREDIT')
        self.assertEqual(tx.amount, Decimal('15000.00'))

        act = EmployeeActivityLog.objects.filter(target_id=str(fd.id), action_type='FD_PREMATURE_CLOSE').first()
        self.assertIsNotNone(act)

    # =========================================================================
    # 3. ADMIN VIRTUAL DEBIT CARD GOVERNANCE TESTS
    # =========================================================================

    def test_admin_card_list_access_and_masking(self):
        """Verify Admin card list access and ensure full PAN is NEVER exposed in the HTML."""
        card_num = generate_simulation_card_number()
        card = VirtualDebitCard.objects.create(
            user=self.customer_user,
            account=self.customer_account,
            card_number=card_num,
            cardholder_name='Priya Sharma',
            card_type='CLASSIC',
            status='ACTIVE'
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_card_list'))
        self.assertEqual(response.status_code, 200)

        # Must display masked card number: •••• •••• •••• {last4}
        last4 = card_num[-4:]
        self.assertContains(response, f"•••• •••• •••• {last4}")

        # CRITICAL SECURITY INVARIANT: Raw 16-digit card number must NEVER appear in template
        self.assertNotContains(response, card_num)

    def test_admin_card_detail_and_actions(self):
        """Verify Admin can block, unblock, and cancel card with audit logs."""
        card_num = generate_simulation_card_number()
        card = VirtualDebitCard.objects.create(
            user=self.customer_user,
            account=self.customer_account,
            card_number=card_num,
            cardholder_name='Priya Sharma',
            card_type='PLATINUM',
            status='ACTIVE'
        )

        self.client.force_login(self.admin_user)

        # 1. Detail View
        response = self.client.get(reverse('admin_card_detail', kwargs={'card_id': card.id}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_card_detail.html')

        # 2. Admin Block Card
        response = self.client.post(
            reverse('admin_card_action', kwargs={'card_id': card.id}),
            {
                'action_type': 'block',
                'reason': 'Suspicious e-commerce velocity detected',
                'next': reverse('admin_card_detail', kwargs={'card_id': card.id})
            }
        )
        self.assertEqual(response.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'BLOCKED')
        self.assertTrue(card.blocked_by_admin)

        audit = DebitCardAuditLog.objects.filter(card=card, action='BLOCKED_ADMIN').first()
        self.assertIsNotNone(audit)
        act = EmployeeActivityLog.objects.filter(target_id=str(card.id), action_type='CARD_STATUS_CHANGE').first()
        self.assertIsNotNone(act)

        # 3. Admin Unblock Card
        response = self.client.post(
            reverse('admin_card_action', kwargs={'card_id': card.id}),
            {
                'action_type': 'unblock',
                'reason': 'Customer verified legitimate authorization',
                'next': reverse('admin_card_detail', kwargs={'card_id': card.id})
            }
        )
        self.assertEqual(response.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'ACTIVE')
        self.assertFalse(card.blocked_by_admin)

        # 4. Admin Cancel Card
        response = self.client.post(
            reverse('admin_card_action', kwargs={'card_id': card.id}),
            {
                'action_type': 'cancel',
                'reason': 'Permanent card compromise confirmed',
                'next': reverse('admin_card_detail', kwargs={'card_id': card.id})
            }
        )
        self.assertEqual(response.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'CANCELLED')

    # =========================================================================
    # 4. ADMIN NOTIFICATIONS & BROADCASTS TESTS
    # =========================================================================

    def test_admin_notification_list_and_detail(self):
        """Verify notification surveillance registry and detail inspection."""
        notif = Notification.objects.create(
            recipient=self.customer_user,
            notification_type='SECURITY',
            title='Security Alert: Login from New Device',
            message='A new session was initiated from IP 192.168.1.5.',
            target_url='/customer/profile/'
        )

        self.client.force_login(self.admin_user)

        # List
        response = self.client.get(reverse('admin_notification_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Security Alert: Login from New Device')

        # Filter by type
        response = self.client.get(reverse('admin_notification_list'), {'type': 'SECURITY'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Security Alert: Login from New Device')

        # Detail
        response = self.client.get(reverse('admin_notification_detail', kwargs={'notif_id': notif.id}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Delivered exclusively via the')

    def test_admin_notification_broadcast_and_url_sanitization(self):
        """Verify broadcast creation, audience targeting, open redirect prevention, and deduplication."""
        self.client.force_login(self.admin_user)

        # GET composer
        response = self.client.get(reverse('admin_notification_broadcast'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_notification_broadcast.html')

        # POST Broadcast to ALL_CUSTOMERS
        response = self.client.post(
            reverse('admin_notification_broadcast'),
            {
                'scope': 'ALL_CUSTOMERS',
                'notification_type': 'SYSTEM',
                'title': 'Scheduled Maintenance Window',
                'message': 'Core banking systems will undergo maintenance on Sunday from 02:00 to 04:00 AM.',
                'target_url': 'javascript:alert(1)',  # Malicious target URL to sanitize
                'idempotency_key': 'TEST-BCAST-001'
            }
        )
        self.assertEqual(response.status_code, 302)

        # Verify customer received notification
        notif = Notification.objects.filter(
            recipient=self.customer_user,
            title='Scheduled Maintenance Window'
        ).first()
        self.assertIsNotNone(notif)
        self.assertEqual(notif.safe_target_url, '')  # JavaScript URL was sanitized away!

        # Verify employee did NOT receive notification (scope was ALL_CUSTOMERS)
        emp_notif = Notification.objects.filter(
            recipient=self.employee_user,
            title='Scheduled Maintenance Window'
        ).first()
        self.assertIsNone(emp_notif)

        # Verify EmployeeActivityLog entry
        act = EmployeeActivityLog.objects.filter(
            action_type='SYSTEM_ANNOUNCEMENT'
        ).first()
        self.assertIsNotNone(act)
        self.assertIn('Scheduled Maintenance Window', act.description)

        # Test AJAX preview endpoint
        preview_resp = self.client.get(
            reverse('admin_notification_preview'),
            {'scope': 'ALL_CUSTOMERS'}
        )
        self.assertEqual(preview_resp.status_code, 200)
        self.assertIn('count', preview_resp.json())
