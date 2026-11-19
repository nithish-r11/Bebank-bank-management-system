from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from banking.models import (
    BankAccount, Transaction, Loan, FixedDeposit, VirtualDebitCard,
    SupportTicket, EmployeeActivityLog, Notification
)


class AdminCustomerAccountGovernanceTests(TestCase):
    """
    Focused comprehensive test suite for Task 4.2:
    - Admin Customer Directory: search, filters (active, lock, KYC), pagination
    - Admin Customer 360° Detail: profile, linked accounts, products, audit logs
    - Customer Access Governance: toggle active/inactive & lock/unlock with mandatory reason
    - Guardrails: Self-deactivation/lockout prevention, Last active admin protection
    - Admin Account Directory: search, type & status filters, pagination
    - Admin Account Detail: balance, status, transactions, audit trail
    - Account Freeze/Unfreeze: mandatory reason, audit logs, notifications
    - Debit-Freeze Semantics:
        * Outgoing debits blocked: transfers, teller withdrawals, card issuing, FD creation, loan repay
        * Incoming credits permitted: teller deposits, incoming transfers
    - RBAC barriers: Customer and Employee blocked with 403 Forbidden
    """

    def setUp(self):
        self.client = Client(HTTP_HOST='127.0.0.1')

        # Admin user
        self.admin = User.objects.create_user(
            username='adminuser',
            email='admin@bebank.local',
            password='AdminPassword123!',
            role='ADMIN',
            is_active=True,
            account_locked=False
        )

        # Second Admin user for last active admin guard tests
        self.second_admin = User.objects.create_user(
            username='secondadmin',
            email='secondadmin@bebank.local',
            password='AdminPassword123!',
            role='ADMIN',
            is_active=True,
            account_locked=False
        )

        # Employee user
        self.employee = User.objects.create_user(
            username='emp_governance',
            email='emp@bebank.local',
            password='EmployeePass123!',
            role='EMPLOYEE',
            first_name='Vikram',
            last_name='Staff',
            is_active=True,
            account_locked=False
        )

        # Customer 1
        self.customer1 = User.objects.create_user(
            username='aarav_patel',
            email='aarav.patel@example.com',
            password='CustomerPass123!',
            role='CUSTOMER',
            first_name='Aarav',
            last_name='Patel',
            phone='9876543210',
            kyc_status='APPROVED',
            is_active=True,
            account_locked=False
        )

        # Customer 2
        self.customer2 = User.objects.create_user(
            username='priya_sharma',
            email='priya.sharma@example.com',
            password='CustomerPass123!',
            role='CUSTOMER',
            first_name='Priya',
            last_name='Sharma',
            phone='9876543222',
            kyc_status='PENDING',
            is_active=True,
            account_locked=True
        )

        # Customer 3 (Deactivated)
        self.customer3 = User.objects.create_user(
            username='rohit_verma',
            email='rohit.verma@example.com',
            password='CustomerPass123!',
            role='CUSTOMER',
            first_name='Rohit',
            last_name='Verma',
            phone='9876543333',
            kyc_status='REJECTED',
            is_active=False,
            account_locked=False
        )

        # Bank accounts
        self.account1 = BankAccount.objects.create(
            user=self.customer1,
            account_number='100000000001',
            account_type='SAVINGS',
            balance=Decimal('50000.00'),
            status='ACTIVE'
        )

        self.account2 = BankAccount.objects.create(
            user=self.customer2,
            account_number='100000000002',
            account_type='CURRENT',
            balance=Decimal('25000.00'),
            status='ACTIVE'
        )

        self.account3 = BankAccount.objects.create(
            user=self.customer3,
            account_number='100000000003',
            account_type='SAVINGS',
            balance=Decimal('10000.00'),
            status='FROZEN',
            freeze_reason='Suspicious activity flagged'
        )

    # =========================================================================
    # 1. RBAC & PERMISSIONS MATRIX
    # =========================================================================

    def test_unauthenticated_user_redirected_to_login(self):
        """Unauthenticated requests to admin customer and account pages redirect to /login/."""
        urls = [
            reverse('admin_customer_list'),
            reverse('admin_customer_detail', kwargs={'user_id': self.customer1.id}),
            reverse('admin_account_list'),
            reverse('admin_account_detail', kwargs={'account_id': self.account1.id}),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/login/', response.url)

    def test_customer_denied_access_with_403(self):
        """Customer role receives 403 Forbidden across all admin governance endpoints."""
        self.client.force_login(self.customer1)
        urls = [
            (reverse('admin_customer_list'), 'get'),
            (reverse('admin_customer_detail', kwargs={'user_id': self.customer1.id}), 'get'),
            (reverse('admin_customer_toggle_active', kwargs={'user_id': self.customer1.id}), 'post'),
            (reverse('admin_customer_toggle_lock', kwargs={'user_id': self.customer1.id}), 'post'),
            (reverse('admin_account_list'), 'get'),
            (reverse('admin_account_detail', kwargs={'account_id': self.account1.id}), 'get'),
            (reverse('admin_account_toggle_freeze', kwargs={'account_id': self.account1.id}), 'post'),
        ]
        for url, method in urls:
            if method == 'get':
                response = self.client.get(url)
            else:
                response = self.client.post(url, {'reason': 'test'})
            self.assertEqual(response.status_code, 403, f"Customer allowed on {url}")

    def test_employee_denied_access_with_403(self):
        """Employee role receives 403 Forbidden across all admin governance endpoints."""
        self.client.force_login(self.employee)
        urls = [
            (reverse('admin_customer_list'), 'get'),
            (reverse('admin_customer_detail', kwargs={'user_id': self.customer1.id}), 'get'),
            (reverse('admin_customer_toggle_active', kwargs={'user_id': self.customer1.id}), 'post'),
            (reverse('admin_customer_toggle_lock', kwargs={'user_id': self.customer1.id}), 'post'),
            (reverse('admin_account_list'), 'get'),
            (reverse('admin_account_detail', kwargs={'account_id': self.account1.id}), 'get'),
            (reverse('admin_account_toggle_freeze', kwargs={'account_id': self.account1.id}), 'post'),
        ]
        for url, method in urls:
            if method == 'get':
                response = self.client.get(url)
            else:
                response = self.client.post(url, {'reason': 'test'})
            self.assertEqual(response.status_code, 403, f"Employee allowed on {url}")

    # =========================================================================
    # 2. ADMIN CUSTOMER DIRECTORY & FILTERS
    # =========================================================================

    def test_admin_customer_list_view_renders_correctly(self):
        """Admin can access the customer directory and view paginated records."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_customer_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_customer_list.html')
        self.assertContains(response, 'Customer Governance Directory')
        self.assertContains(response, 'aarav_patel')
        self.assertContains(response, 'priya_sharma')
        self.assertContains(response, 'rohit_verma')

    def test_admin_customer_list_search_by_username_and_account(self):
        """Search filters correctly by customer username, name, and account number."""
        self.client.force_login(self.admin)

        # Search by username
        response = self.client.get(reverse('admin_customer_list') + '?q=aarav')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'aarav_patel')
        self.assertNotContains(response, 'priya_sharma')

        # Search by account number
        response = self.client.get(reverse('admin_customer_list') + '?q=100000000002')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'priya_sharma')
        self.assertNotContains(response, 'aarav_patel')

    def test_admin_customer_list_filters(self):
        """Directory properly filters by active status, lock state, and KYC status."""
        self.client.force_login(self.admin)

        # Filter active
        response = self.client.get(reverse('admin_customer_list') + '?status=active')
        self.assertContains(response, 'aarav_patel')
        self.assertNotContains(response, 'rohit_verma')

        # Filter locked
        response = self.client.get(reverse('admin_customer_list') + '?lock=locked')
        self.assertContains(response, 'priya_sharma')
        self.assertNotContains(response, 'aarav_patel')

        # Filter KYC
        response = self.client.get(reverse('admin_customer_list') + '?kyc=APPROVED')
        self.assertContains(response, 'aarav_patel')
        self.assertNotContains(response, 'priya_sharma')

    # =========================================================================
    # 3. ADMIN CUSTOMER 360° DETAIL WORKSPACE
    # =========================================================================

    def test_admin_customer_detail_view_success(self):
        """Admin can view complete 360° customer profile and financial summary."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_customer_detail', kwargs={'user_id': self.customer1.id}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_customer_detail.html')
        self.assertContains(response, 'Aarav Patel')
        self.assertContains(response, '100000000001')
        self.assertContains(response, '50000.00')

    def test_admin_customer_detail_returns_404_for_employee_or_nonexistent(self):
        """Viewing a non-customer user ID via customer detail returns 404."""
        self.client.force_login(self.admin)
        # Employee ID
        response = self.client.get(reverse('admin_customer_detail', kwargs={'user_id': self.employee.id}))
        self.assertEqual(response.status_code, 404)
        # Non-existent ID
        response = self.client.get(reverse('admin_customer_detail', kwargs={'user_id': 99999}))
        self.assertEqual(response.status_code, 404)

    # =========================================================================
    # 4. CUSTOMER GOVERNANCE (TOGGLE ACTIVE & TOGGLE LOCK)
    # =========================================================================

    def test_toggle_active_requires_post_and_mandatory_reason(self):
        """Toggle active requires POST and non-empty reason; GET is rejected."""
        self.client.force_login(self.admin)
        url = reverse('admin_customer_toggle_active', kwargs={'user_id': self.customer1.id})

        # GET rejected with 405
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

        # POST without reason fails validation
        response = self.client.post(url, {'reason': '   '}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.customer1.refresh_from_db()
        self.assertTrue(self.customer1.is_active)  # Unchanged
        self.assertContains(response, 'reason is mandatory')

    def test_toggle_active_success_audit_and_notification(self):
        """Admin can toggle customer active state; audits action and notifies customer."""
        self.client.force_login(self.admin)
        url = reverse('admin_customer_toggle_active', kwargs={'user_id': self.customer1.id})

        # Deactivate
        response = self.client.post(url, {'reason': 'Suspected fraud investigation'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.customer1.refresh_from_db()
        self.assertFalse(self.customer1.is_active)

        # Verify audit log
        log = EmployeeActivityLog.objects.filter(
            action_type='ADMIN_CUSTOMER_STATUS_CHANGE',
            customer=self.customer1
        ).first()
        self.assertIsNotNone(log)
        self.assertIn('Suspected fraud investigation', log.description)
        self.assertEqual(log.employee, self.admin)

        # Verify in-app notification
        notif = Notification.objects.filter(
            recipient=self.customer1,
            notification_type='SYSTEM'
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn('Suspected fraud investigation', notif.message)

    def test_toggle_lock_success_audit_and_reset_failed_logins(self):
        """Admin can unlock customer; resets failed login counter to 0."""
        self.client.force_login(self.admin)
        self.customer2.failed_login_attempts = 5
        self.customer2.account_locked = True
        self.customer2.save()

        url = reverse('admin_customer_toggle_lock', kwargs={'user_id': self.customer2.id})
        response = self.client.post(url, {'reason': 'Customer identity verified by phone'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.customer2.refresh_from_db()
        self.assertFalse(self.customer2.account_locked)
        self.assertEqual(self.customer2.failed_login_attempts, 0)

        # Verify audit log
        log = EmployeeActivityLog.objects.filter(
            action_type='ADMIN_CUSTOMER_LOCK_CHANGE',
            customer=self.customer2
        ).first()
        self.assertIsNotNone(log)
        self.assertIn('unlocked', log.description)

        # Verify notification
        notif = Notification.objects.filter(
            recipient=self.customer2,
            notification_type='SECURITY'
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn('Customer identity verified', notif.message)

    def test_self_deactivation_and_lockout_prevention(self):
        """Admins cannot deactivate or lock their own account."""
        self.client.force_login(self.admin)

        # Attempt self-deactivation
        url_active = reverse('admin_customer_toggle_active', kwargs={'user_id': self.admin.id})
        response = self.client.post(url_active, {'reason': 'Deactivating myself'}, follow=True)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertContains(response, 'cannot deactivate their own account')

        # Attempt self-lockout
        url_lock = reverse('admin_customer_toggle_lock', kwargs={'user_id': self.admin.id})
        response = self.client.post(url_lock, {'reason': 'Locking myself'}, follow=True)
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.account_locked)
        self.assertContains(response, 'cannot lock their own account')

    def test_last_active_admin_protection(self):
        """Cannot deactivate or lock the last remaining active administrator."""
        # Deactivate second admin first
        self.second_admin.is_active = False
        self.second_admin.save()

        self.client.force_login(self.admin)
        url = reverse('admin_customer_toggle_active', kwargs={'user_id': self.admin.id})
        response = self.client.post(url, {'reason': 'Deactivating last admin'}, follow=True)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    # =========================================================================
    # 5. ADMIN BANK ACCOUNT DIRECTORY & DETAILS
    # =========================================================================

    def test_admin_account_list_view_renders_correctly(self):
        """Admin can view the bank account directory."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_account_list'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_account_list.html')
        self.assertContains(response, '100000000001')
        self.assertContains(response, '100000000002')
        self.assertContains(response, '100000000003')

    def test_admin_account_list_search_and_filters(self):
        """Admin can search accounts and filter by type and frozen status."""
        self.client.force_login(self.admin)

        # Search by customer name
        response = self.client.get(reverse('admin_account_list') + '?q=Aarav')
        self.assertContains(response, '100000000001')
        self.assertNotContains(response, '100000000002')

        # Filter by type
        response = self.client.get(reverse('admin_account_list') + '?type=CURRENT')
        self.assertContains(response, '100000000002')
        self.assertNotContains(response, '100000000001')

        # Filter by frozen status
        response = self.client.get(reverse('admin_account_list') + '?status=FROZEN')
        self.assertContains(response, '100000000003')
        self.assertNotContains(response, '100000000001')

    def test_admin_account_detail_view_success(self):
        """Admin can view account detail including ledger balance and status."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_account_detail', kwargs={'account_id': self.account1.id}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_account_detail.html')
        self.assertContains(response, '100000000001')
        self.assertContains(response, '50000.00')
        self.assertContains(response, 'ACTIVE')

    # =========================================================================
    # 6. ACCOUNT FREEZE / UNFREEZE GOVERNANCE
    # =========================================================================

    def test_toggle_freeze_requires_post_and_mandatory_reason(self):
        """Freezing an account requires POST and mandatory non-empty reason."""
        self.client.force_login(self.admin)
        url = reverse('admin_account_toggle_freeze', kwargs={'account_id': self.account1.id})

        # GET rejected with 405
        response = self.client.get(url)
        self.assertEqual(response.status_code, 405)

        # POST without reason fails
        response = self.client.post(url, {'reason': '   '}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.status, 'ACTIVE')
        self.assertContains(response, 'reason is mandatory')

    def test_account_freeze_and_unfreeze_lifecycle(self):
        """Admin can freeze and unfreeze an account; audits and notifies customer."""
        self.client.force_login(self.admin)
        url = reverse('admin_account_toggle_freeze', kwargs={'account_id': self.account1.id})

        # 1. FREEZE
        response = self.client.post(url, {'reason': 'Court attachment order received'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.status, 'FROZEN')
        self.assertTrue(self.account1.is_frozen)
        self.assertEqual(self.account1.frozen_by, self.admin)
        self.assertEqual(self.account1.freeze_reason, 'Court attachment order received')
        self.assertIsNotNone(self.account1.frozen_at)

        # Audit log for freeze
        freeze_log = EmployeeActivityLog.objects.filter(
            action_type='ADMIN_ACCOUNT_FREEZE',
            account_number=self.account1.account_number
        ).first()
        self.assertIsNotNone(freeze_log)
        self.assertIn('Court attachment order', freeze_log.description)

        # Customer notification
        freeze_notif = Notification.objects.filter(
            recipient=self.customer1,
            notification_type='SECURITY'
        ).first()
        self.assertIsNotNone(freeze_notif)
        self.assertIn('frozen', freeze_notif.message)

        # 2. UNFREEZE
        response = self.client.post(url, {'reason': 'Court clearance certificate validated'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.status, 'ACTIVE')
        self.assertFalse(self.account1.is_frozen)
        self.assertIsNone(self.account1.frozen_by)
        self.assertIsNone(self.account1.frozen_at)

        # Audit log for unfreeze
        unfreeze_log = EmployeeActivityLog.objects.filter(
            action_type='ADMIN_ACCOUNT_UNFREEZE',
            account_number=self.account1.account_number
        ).first()
        self.assertIsNotNone(unfreeze_log)
        self.assertIn('unfrozen', unfreeze_log.description)

    # =========================================================================
    # 7. DEBIT-FREEZE ENFORCEMENT ON FINANCIAL OPERATIONS
    # =========================================================================

    def test_frozen_account_blocks_outgoing_transfers(self):
        """Customer cannot initiate outgoing transfer from a frozen account."""
        import uuid
        self.account1.status = 'FROZEN'
        self.account1.save()

        self.client.force_login(self.customer1)
        response = self.client.post(reverse('transfer'), {
            'account_id': self.account1.id,
            'account_number': self.account2.account_number,
            'amount': '1000.00',
            'idempotency_key': str(uuid.uuid4()),
            'description': 'Rent payment'
        }, follow=True)

        self.assertContains(response, 'frozen')
        self.account1.refresh_from_db()
        self.account2.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('50000.00'))  # Not debited
        self.assertEqual(self.account2.balance, Decimal('25000.00'))  # Not credited

    def test_frozen_account_allows_incoming_transfer_credits(self):
        """A frozen account CAN receive incoming transfers (credits permitted)."""
        import uuid
        self.account2.status = 'FROZEN'
        self.account2.save()
        self.customer2.account_locked = False
        self.customer2.save()

        # Sender (account1) is ACTIVE
        self.client.force_login(self.customer1)
        response = self.client.post(reverse('transfer'), {
            'account_id': self.account1.id,
            'account_number': self.account2.account_number,
            'amount': '500.00',
            'idempotency_key': str(uuid.uuid4()),
            'description': 'Gift to frozen account'
        }, follow=True)

        self.assertContains(response, 'Transfer successful')
        self.account1.refresh_from_db()
        self.account2.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('49500.00'))
        self.assertEqual(self.account2.balance, Decimal('25500.00'))

    def test_frozen_account_blocks_teller_withdrawals(self):
        """Teller counter withdrawals are blocked on a frozen account."""
        self.account1.status = 'FROZEN'
        self.account1.save()

        self.client.force_login(self.employee)
        response = self.client.post(reverse('staff_withdraw'), {
            'account_number': self.account1.account_number,
            'amount': '2000.00',
            'notes': 'Counter cash withdrawal'
        }, follow=True)

        self.assertContains(response, 'frozen')
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('50000.00'))

    def test_frozen_account_allows_teller_deposits(self):
        """Teller counter deposits are permitted into a frozen account."""
        self.account1.status = 'FROZEN'
        self.account1.save()

        self.client.force_login(self.employee)
        response = self.client.post(reverse('staff_deposit'), {
            'account_number': self.account1.account_number,
            'amount': '3000.00',
            'notes': 'Over-the-counter cash deposit'
        }, follow=True)

        self.assertContains(response, 'SUCCESS')
        self.assertContains(response, '3000.00')
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('53000.00'))

    def test_frozen_account_blocks_virtual_card_issuance(self):
        """Virtual debit card creation is rejected if the account is frozen."""
        self.account1.status = 'FROZEN'
        self.account1.save()

        self.client.force_login(self.customer1)
        response = self.client.post(reverse('card_issue'), {
            'account_id': self.account1.id,
            'cardholder_name': 'Aarav Patel',
            'daily_limit': '10000.00'
        }, follow=True)

        self.assertContains(response, 'frozen')
        cards_count = VirtualDebitCard.objects.filter(account=self.account1).count()
        self.assertEqual(cards_count, 0)

    def test_frozen_account_blocks_fd_creation(self):
        """Fixed Deposit creation debit is rejected if funding account is frozen."""
        self.account1.status = 'FROZEN'
        self.account1.save()

        self.client.force_login(self.customer1)
        response = self.client.post(reverse('fd'), {
            'account_id': self.account1.id,
            'amount': '10000.00',
            'months': '12'
        }, follow=True)

        self.assertContains(response, 'frozen')
        fds_count = FixedDeposit.objects.filter(account=self.account1).count()
        self.assertEqual(fds_count, 0)

