from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User, ProfileAuditLog
from banking.models import (
    BankAccount, Transaction, Loan, FixedDeposit, VirtualDebitCard,
    SupportTicket, Notification
)


class EmployeeModuleComprehensiveTests(TestCase):
    """
    Comprehensive tests for Task 3.1:
    - Employee Dashboard & Live Metrics
    - Customer Search & 360° Management
    - Employee Customer Profile Update & Whitelist Protection
    - KYC Review Workspace, Authorization & Overwrite Prevention
    - Staff Account Operations & In-Branch Withdrawal
    - Navigation & RBAC Boundary Enforcement
    """

    def setUp(self):
        self.client = Client()

        # Admin user
        self.admin = User.objects.create_superuser(
            username="testadmin",
            password="AdminPassword123!",
            email="admin@bebank.test",
            role="ADMIN"
        )

        # Employee user
        self.employee = User.objects.create_user(
            username="testemployee",
            password="EmployeePassword123!",
            email="employee@bebank.test",
            role="EMPLOYEE",
            first_name="Jane",
            last_name="Banker"
        )

        # Customer 1 (Active, KYC Approved)
        self.customer1 = User.objects.create_user(
            username="customer_one",
            password="CustomerPassword123!",
            email="cust1@bebank.test",
            role="CUSTOMER",
            first_name="Manoj",
            last_name="Kumar",
            phone="9876543210",
            address="123 Bank St, Metro",
            pincode="600001",
            kyc_status="APPROVED",
            kyc_verified=True,
            simulated_id_type="SIMULATED_PASSPORT",
            simulated_id_number="PASS-98765432",
            kyc_submitted_at=timezone.now()
        )

        self.account1 = BankAccount.objects.create(
            user=self.customer1,
            account_number="ACC10000001",
            account_type="SAVINGS",
            balance=Decimal("15000.00"),
            email=self.customer1.email,
            phone=self.customer1.phone,
            aadhaar="123456789012",
            pan="ABCDE1234F",
            address=self.customer1.address,
            pincode=self.customer1.pincode
        )

        # Customer 2 (Pending KYC, Active)
        self.customer2 = User.objects.create_user(
            username="customer_two",
            password="CustomerPassword123!",
            email="cust2@bebank.test",
            role="CUSTOMER",
            first_name="Priya",
            last_name="Sharma",
            phone="9123456780",
            address="456 River View, City",
            pincode="560001",
            kyc_status="PENDING",
            kyc_verified=False,
            simulated_id_type="SIMULATED_DRIVING_LICENSE",
            simulated_id_number="DL-55443322",
            kyc_submitted_at=timezone.now()
        )

        self.account2 = BankAccount.objects.create(
            user=self.customer2,
            account_number="ACC10000002",
            account_type="CURRENT",
            balance=Decimal("5000.00"),
            email=self.customer2.email,
            phone=self.customer2.phone,
            address=self.customer2.address,
            pincode=self.customer2.pincode
        )

        # Customer 3 (Locked Account, Under Review)
        self.customer3 = User.objects.create_user(
            username="customer_three",
            password="CustomerPassword123!",
            email="cust3@bebank.test",
            role="CUSTOMER",
            first_name="Ramesh",
            last_name="Gupta",
            phone="9988776655",
            account_locked=True,
            kyc_status="UNDER_REVIEW",
            simulated_id_type="SIMULATED_NATIONAL_ID",
            simulated_id_number="NID-11223344",
            kyc_submitted_at=timezone.now()
        )

        self.account3 = BankAccount.objects.create(
            user=self.customer3,
            account_number="ACC10000003",
            account_type="SAVINGS",
            balance=Decimal("2000.00")
        )

        # Support ticket for Customer 2
        self.ticket = SupportTicket.objects.create(
            customer=self.customer2,
            subject="Assistance with debit card",
            description="Card not working for online transactions.",
            category="CARD",
            priority="HIGH",
            status="OPEN"
        )

        # Today's deposit transaction for account 1
        Transaction.objects.create(
            account=self.account1,
            transaction_type="DEPOSIT",
            amount=Decimal("2500.00"),
            balance_before=Decimal("12500.00"),
            balance_after=Decimal("15000.00")
        )

        # Today's withdraw transaction for account 1
        Transaction.objects.create(
            account=self.account1,
            transaction_type="WITHDRAW",
            amount=Decimal("500.00"),
            balance_before=Decimal("15500.00"),
            balance_after=Decimal("15000.00")
        )

    # =========================================================
    # 1. EMPLOYEE DASHBOARD & LIVE METRICS
    # =========================================================

    def test_employee_dashboard_renders_live_database_metrics(self):
        """Dashboard displays real database counts and aggregate values."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Context assertions
        self.assertEqual(resp.context['total_customers'], 3)
        self.assertEqual(resp.context['total_active_accounts'], 2)  # Account 3 is locked
        self.assertEqual(resp.context['pending_kyc'], 1)  # Customer 2 is PENDING
        self.assertEqual(resp.context['pending_tickets'], 1)  # 1 OPEN ticket
        self.assertEqual(resp.context['today_deposits'], Decimal("2500.00"))
        self.assertEqual(resp.context['today_withdrawals'], Decimal("500.00"))

        # HTML assertions
        self.assertContains(resp, "Employee Operations Dashboard")
        self.assertContains(resp, "ACC10000001")
        self.assertContains(resp, "Quick Action Center")

    def test_employee_dashboard_search_by_account_number(self):
        """Dashboard POST searches and returns account card."""
        self.client.force_login(self.employee)
        resp = self.client.post(reverse('employee_dashboard'), {
            'account_number': 'ACC10000001'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.context.get('account'))
        self.assertEqual(resp.context['account'].account_number, 'ACC10000001')
        self.assertContains(resp, "customer_one")

    def test_employee_dashboard_search_not_found(self):
        """Non-existent account search sets error message."""
        self.client.force_login(self.employee)
        resp = self.client.post(reverse('employee_dashboard'), {
            'account_number': 'NONEXISTENT999'
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context.get('account'))
        self.assertContains(resp, "No customer or account found")

    # =========================================================
    # 2. CUSTOMER DIRECTORY & MULTI-FIELD SEARCH
    # =========================================================

    def test_customer_list_search_by_username(self):
        """Search query matching username returns exact customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'q': 'customer_one'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_one")
        self.assertNotContains(resp, "customer_two")

    def test_customer_list_search_by_first_name(self):
        """Search query matching first_name returns customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'q': 'Priya'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_two")
        self.assertNotContains(resp, "customer_one")

    def test_customer_list_search_by_email(self):
        """Search query matching email returns customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'q': 'cust1@bebank.test'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_one")

    def test_customer_list_search_by_phone(self):
        """Search query matching phone returns customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'q': '9123456780'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_two")

    def test_customer_list_search_by_account_number(self):
        """Search query matching account_number returns customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'q': 'ACC10000001'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_one")

    def test_customer_list_filter_by_kyc_status(self):
        """Filtering by KYC status returns only matching customers."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'kyc': 'PENDING'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_two")
        self.assertNotContains(resp, "customer_one")

    def test_customer_list_filter_by_account_locked_status(self):
        """Filtering by account locked status returns only locked customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_list'), {'status': 'locked'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_three")
        self.assertNotContains(resp, "customer_one")

    # =========================================================
    # 3. CUSTOMER 360° PROFILE VIEW & MASKING
    # =========================================================

    def test_customer_detail_360_view(self):
        """Staff customer 360 view renders accounts, balance, transactions, and masked IDs."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_detail', args=[self.customer1.id]))
        self.assertEqual(resp.status_code, 200)

        # Context checks
        self.assertEqual(resp.context['target_user'], self.customer1)
        self.assertEqual(resp.context['total_balance'], Decimal("15000.00"))
        self.assertEqual(len(resp.context['accounts']), 1)
        self.assertEqual(len(resp.context['transactions']), 2)

        # Security Masking Verification
        self.assertContains(resp, "•••• •••• 9012")  # Masked Aadhaar
        self.assertContains(resp, "AB••••••4F")      # Masked PAN
        self.assertNotContains(resp, "123456789012") # Raw Aadhaar must NOT be exposed
        self.assertNotContains(resp, "ABCDE1234F")   # Raw PAN must NOT be exposed

    def test_customer_detail_cannot_view_non_customer(self):
        """Staff customer detail returns 404 if targeted user is not a customer."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_customer_detail', args=[self.admin.id]))
        self.assertEqual(resp.status_code, 404)

    # =========================================================
    # 4. EMPLOYEE CUSTOMER PROFILE UPDATE & AUDIT LOG
    # =========================================================

    def test_employee_can_update_whitelisted_profile_fields(self):
        """Staff can update first_name, last_name, email, phone, address, pincode."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_customer_profile_edit', args=[self.customer1.id]),
            data={
                'first_name': 'Manoj Updated',
                'last_name': 'Kumar Senior',
                'email': 'manoj.updated@bebank.test',
                'phone': '9876500000',
                'address': '789 Updated Blvd',
                'pincode': '600002',
            },
            follow=True
        )
        self.assertEqual(resp.status_code, 200)

        self.customer1.refresh_from_db()
        self.assertEqual(self.customer1.first_name, 'Manoj Updated')
        self.assertEqual(self.customer1.last_name, 'Kumar Senior')
        self.assertEqual(self.customer1.email, 'manoj.updated@bebank.test')
        self.assertEqual(self.customer1.phone, '9876500000')
        self.assertEqual(self.customer1.address, '789 Updated Blvd')
        self.assertEqual(self.customer1.pincode, '600002')

        # Linked bank account synchronized
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.email, 'manoj.updated@bebank.test')
        self.assertEqual(self.account1.phone, '9876500000')

        # ProfileAuditLog created
        audit = ProfileAuditLog.objects.filter(user=self.customer1).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.updated_by, self.employee)
        self.assertIn("Manoj Updated", audit.changes)

        # In-app notification sent to customer
        notif = Notification.objects.filter(recipient=self.customer1, notification_type='ACCOUNT').first()
        self.assertIsNotNone(notif)
        self.assertIn("updated by bank staff", notif.message)

    def test_employee_cannot_tamper_protected_fields_via_profile_update(self):
        """Staff cannot change username, role, is_superuser, account_locked, or balance."""
        self.client.force_login(self.employee)
        self.client.post(
            reverse('staff_customer_profile_edit', args=[self.customer1.id]),
            data={
                'first_name': 'Manoj',
                'last_name': 'Kumar',
                'email': self.customer1.email,
                'phone': self.customer1.phone,
                'address': self.customer1.address,
                'pincode': self.customer1.pincode,
                # Attempted malicious payload
                'username': 'hacked_username',
                'role': 'ADMIN',
                'is_superuser': True,
                'account_locked': True,
                'balance': '999999.00'
            }
        )

        self.customer1.refresh_from_db()
        self.assertEqual(self.customer1.username, 'customer_one')
        self.assertEqual(self.customer1.role, 'CUSTOMER')
        self.assertFalse(self.customer1.is_superuser)
        self.assertFalse(self.customer1.account_locked)

        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('15000.00'))

    # =========================================================
    # 5. KYC REVIEW WORKSPACE, ACTIONS & PROTECTIONS
    # =========================================================

    def test_kyc_queue_view_and_filtering(self):
        """KYC queue renders pending, under_review, approved, and rejected tabs."""
        self.client.force_login(self.employee)

        # Default is PENDING
        resp_pending = self.client.get(reverse('staff_kyc_queue'))
        self.assertEqual(resp_pending.status_code, 200)
        self.assertContains(resp_pending, "customer_two")
        self.assertNotContains(resp_pending, "customer_one")

        # APPROVED tab
        resp_approved = self.client.get(reverse('staff_kyc_queue'), {'status': 'APPROVED'})
        self.assertEqual(resp_approved.status_code, 200)
        self.assertContains(resp_approved, "customer_one")

        # UNDER_REVIEW tab
        resp_ur = self.client.get(reverse('staff_kyc_queue'), {'status': 'UNDER_REVIEW'})
        self.assertEqual(resp_ur.status_code, 200)
        self.assertContains(resp_ur, "customer_three")

    def test_staff_can_approve_kyc(self):
        """Staff approves pending KYC, records reviewer, timestamp, and sends notification."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_kyc_review', args=[self.customer2.id]),
            data={'action': 'approve'},
            follow=True
        )
        self.assertEqual(resp.status_code, 200)

        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.kyc_status, 'APPROVED')
        self.assertTrue(self.customer2.kyc_verified)
        self.assertEqual(self.customer2.kyc_reviewed_by, self.employee)
        self.assertIsNotNone(self.customer2.kyc_reviewed_at)

        notif = Notification.objects.filter(recipient=self.customer2, notification_type='KYC').first()
        self.assertIsNotNone(notif)
        self.assertIn("approved", notif.title.lower())

    def test_staff_can_mark_kyc_under_review(self):
        """Staff moves pending submission to under_review."""
        self.client.force_login(self.employee)
        self.client.post(
            reverse('staff_kyc_review', args=[self.customer2.id]),
            data={'action': 'under_review'}
        )
        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.kyc_status, 'UNDER_REVIEW')
        self.assertEqual(self.customer2.kyc_reviewed_by, self.employee)

    def test_staff_can_reject_kyc_with_reason(self):
        """Staff rejects KYC with a mandatory rejection reason."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_kyc_review', args=[self.customer2.id]),
            data={
                'action': 'reject',
                'rejection_reason': 'Simulated document number format does not match test pattern.'
            },
            follow=True
        )
        self.assertEqual(resp.status_code, 200)

        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.kyc_status, 'REJECTED')
        self.assertFalse(self.customer2.kyc_verified)
        self.assertEqual(self.customer2.kyc_rejection_reason, 'Simulated document number format does not match test pattern.')
        self.assertEqual(self.customer2.kyc_reviewed_by, self.employee)

        # Customer sees rejection reason
        resp_cust = self.client.get(reverse('customer_kyc'))
        self.client.force_login(self.customer2)
        resp_cust = self.client.get(reverse('customer_kyc'))
        self.assertContains(resp_cust, 'Simulated document number format does not match test pattern.')

    def test_reject_without_reason_is_rejected(self):
        """Rejection without specifying reason is disallowed."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_kyc_review', args=[self.customer2.id]),
            data={'action': 'reject', 'rejection_reason': ''},
            follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.kyc_status, 'PENDING')  # Unchanged
        self.assertContains(resp, "Please provide a specific rejection reason")

    def test_approved_kyc_cannot_be_silently_revoked(self):
        """Approved KYC cannot be rejected without explicit confirmation."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_kyc_review', args=[self.customer1.id]),
            data={'action': 'reject', 'rejection_reason': 'Accidental error'},
            follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.customer1.refresh_from_db()
        self.assertEqual(self.customer1.kyc_status, 'APPROVED')  # Protected from silent revoke
        self.assertContains(resp, "Explicit confirmation required")

    def test_customer_cannot_review_own_kyc(self):
        """Customer cannot access staff_kyc_review endpoint (HTTP 403)."""
        self.client.force_login(self.customer1)
        resp = self.client.post(
            reverse('staff_kyc_review', args=[self.customer1.id]),
            data={'action': 'approve'}
        )
        self.assertEqual(resp.status_code, 403)

    def test_staff_cannot_review_own_account(self):
        """Staff self-review guard explicitly prevents reviewer from approving their own user record."""
        from django.test import RequestFactory
        from django.contrib.messages.storage.fallback import FallbackStorage
        from accounts.views import staff_kyc_review_view

        factory = RequestFactory()
        req = factory.post(
            reverse('staff_kyc_review', args=[self.customer2.id]),
            data={'action': 'approve'}
        )
        # Assign customer2 user object but with EMPLOYEE role to trigger self-review guard
        req.user = self.customer2
        req.user.role = 'EMPLOYEE'
        req.session = {}
        req._messages = FallbackStorage(req)

        resp = staff_kyc_review_view(req, self.customer2.id)
        self.assertEqual(resp.status_code, 302)

        # Confirm KYC was NOT approved and remains PENDING
        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.kyc_status, 'PENDING')
        self.assertFalse(self.customer2.kyc_verified)

    # =========================================================
    # 6. STAFF OPERATIONS & IN-BRANCH WITHDRAWAL
    # =========================================================

    def test_staff_operations_hub_renders(self):
        """Staff operations hub renders overview metrics and links."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_operations'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Branch Account Operations Hub")
        self.assertContains(resp, "Cash Deposit")
        self.assertContains(resp, "Cash Withdrawal")

    def test_staff_withdraw_success(self):
        """Staff can disburse cash withdrawal with atomic row locking and notification."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_withdraw'),
            data={
                'account_number': 'ACC10000001',
                'amount': '1500.00'
            },
            follow=True
        )
        self.assertEqual(resp.status_code, 200)

        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('13500.00'))

        tx = Transaction.objects.filter(account=self.account1, transaction_type='WITHDRAW').order_by('-created_at').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal('1500.00'))
        self.assertEqual(tx.balance_after, Decimal('13500.00'))

        notif = Notification.objects.filter(recipient=self.customer1, notification_type='TRANSACTION').order_by('-created_at').first()
        self.assertIsNotNone(notif)
        self.assertIn("1,500.00", notif.message)

    def test_staff_withdraw_insufficient_funds(self):
        """Withdrawal exceeding account balance is rejected."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_withdraw'),
            data={
                'account_number': 'ACC10000001',
                'amount': '99999.00'
            },
            follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('15000.00'))
        self.assertContains(resp, "Insufficient funds")

    def test_staff_withdraw_from_locked_account_rejected(self):
        """Withdrawal from a locked customer account is rejected."""
        self.client.force_login(self.employee)
        resp = self.client.post(
            reverse('staff_withdraw'),
            data={
                'account_number': 'ACC10000003',
                'amount': '100.00'
            },
            follow=True
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Cannot withdraw from a locked or inactive customer account")

    def test_staff_profile_view_renders(self):
        """Staff profile view displays logged-in employee details and operational statistics."""
        self.client.force_login(self.employee)
        resp = self.client.get(reverse('staff_profile'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Staff Officer Profile")
        self.assertContains(resp, self.employee.username)
        self.assertContains(resp, "EMPLOYEE")

    # =========================================================
    # 7. NAVIGATION & ROLE ACCESS CONTROL (RBAC)
    # =========================================================

    def test_customer_cannot_access_any_staff_workspace(self):
        """Customer is strictly denied (HTTP 403) from all employee and staff endpoints."""
        self.client.force_login(self.customer1)
        staff_endpoints = [
            reverse('employee_dashboard'),
            reverse('staff_customer_list'),
            reverse('staff_customer_detail', args=[self.customer1.id]),
            reverse('staff_customer_profile_edit', args=[self.customer1.id]),
            reverse('staff_kyc_queue'),
            reverse('staff_operations'),
            reverse('staff_withdraw'),
            reverse('staff_profile'),
        ]
        for url in staff_endpoints:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 403)

    def test_admin_can_access_all_staff_workspaces(self):
        """Admin has full operational access across staff workspaces."""
        self.client.force_login(self.admin)
        staff_endpoints = [
            reverse('employee_dashboard'),
            reverse('staff_customer_list'),
            reverse('staff_customer_detail', args=[self.customer1.id]),
            reverse('staff_kyc_queue'),
            reverse('staff_operations'),
            reverse('staff_withdraw'),
            reverse('staff_profile'),
        ]
        for url in staff_endpoints:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)

