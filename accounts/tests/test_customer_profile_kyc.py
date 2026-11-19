from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from accounts.models import User, ProfileAuditLog
from banking.models import BankAccount


class CustomerProfileAndKYCTests(TestCase):
    """
    Focused unit & integration tests for Task 2.2:
    - Customer Profile View & Edit
    - Input Validation (phone, email, pincode)
    - Object-Level Ownership & IDOR Prevention
    - Mass Assignment Defense (Protected Security & Administrative fields)
    - Profile Completeness Indicator
    - Simulated KYC Submission & Status Transitions
    - Rejected KYC Resubmission Handling
    - Approved KYC Record Immutability & Protection
    - Staff (Employee / Admin) Authorization on KYC Review
    - Customer Dashboard Integration
    """

    def setUp(self):
        # Customer 1
        self.cust1 = User.objects.create_user(
            username='alice',
            password='Password123!',
            email='alice@example.com',
            role='CUSTOMER',
            first_name='Alice',
            last_name='Smith',
            phone='9876543210',
            address='123 Elm Street',
            pincode='560001',
            kyc_status='NOT_SUBMITTED'
        )
        self.acc1 = BankAccount.objects.create(
            user=self.cust1,
            account_number='10001',
            account_type='SAVINGS',
            balance=Decimal('2500.00'),
            phone=self.cust1.phone,
            address=self.cust1.address,
            pincode=self.cust1.pincode,
            email=self.cust1.email
        )

        # Customer 2 (for IDOR / cross-user checks)
        self.cust2 = User.objects.create_user(
            username='bob',
            password='Password123!',
            email='bob@example.com',
            role='CUSTOMER',
            first_name='Bob',
            last_name='Brown',
            phone='9123456780',
            address='456 Pine Road',
            pincode='560002',
            kyc_status='NOT_SUBMITTED'
        )
        self.acc2 = BankAccount.objects.create(
            user=self.cust2,
            account_number='10002',
            account_type='CURRENT',
            balance=Decimal('5000.00')
        )

        # Employee
        self.employee = User.objects.create_user(
            username='emp_reviewer',
            password='Password123!',
            role='EMPLOYEE'
        )

        # Admin
        self.admin = User.objects.create_user(
            username='adm_reviewer',
            password='Password123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True
        )

        self.client_alice = Client()
        self.client_alice.login(username='alice', password='Password123!')

        self.client_bob = Client()
        self.client_bob.login(username='bob', password='Password123!')

        self.client_emp = Client()
        self.client_emp.login(username='emp_reviewer', password='Password123!')

        self.client_adm = Client()
        self.client_adm.login(username='adm_reviewer', password='Password123!')

    # =========================================================================
    # 1. PROFILE VIEW, EDIT & SYNC TESTS
    # =========================================================================

    def test_customer_can_view_own_profile(self):
        """Customer can access /customer/profile/ and see personal details and account summary."""
        resp = self.client_alice.get(reverse('customer_profile'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Alice Smith')
        self.assertContains(resp, 'alice@example.com')
        self.assertContains(resp, '10001')
        self.assertContains(resp, 'Profile Completeness')

    def test_customer_cannot_post_profile_update_directly(self):
        """Customer cannot directly POST profile updates; endpoint is read-only (HTTP 405)."""
        resp = self.client_alice.post(reverse('customer_profile'), {
            'first_name': 'Alicia',
            'last_name': 'Vance',
            'email': 'alicia.vance@example.com',
            'phone': '9988776655',
            'address': '789 Oak Boulevard',
            'pincode': '560099'
        })
        self.assertEqual(resp.status_code, 405)

        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.first_name, 'Alice')
        self.assertEqual(self.cust1.last_name, 'Smith')

    def test_employee_can_update_permitted_profile_fields(self):
        """Employee can update customer contact details via staff endpoint, and linked BankAccounts sync."""
        resp = self.client_emp.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}), {
            'first_name': 'Alicia',
            'last_name': 'Vance',
            'email': 'alicia.vance@example.com',
            'phone': '9988776655',
            'address': '789 Oak Boulevard',
            'pincode': '560099'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'updated successfully')

        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.first_name, 'Alicia')
        self.assertEqual(self.cust1.last_name, 'Vance')
        self.assertEqual(self.cust1.email, 'alicia.vance@example.com')
        self.assertEqual(self.cust1.phone, '9988776655')
        self.assertEqual(self.cust1.address, '789 Oak Boulevard')
        self.assertEqual(self.cust1.pincode, '560099')
        self.assertEqual(self.cust1.profile_updated_by, self.employee)
        self.assertIsNotNone(self.cust1.profile_updated_at)

        # Verify linked BankAccount sync
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.email, 'alicia.vance@example.com')
        self.assertEqual(self.acc1.phone, '9988776655')
        self.assertEqual(self.acc1.address, '789 Oak Boulevard')
        self.assertEqual(self.acc1.pincode, '560099')

        # Verify ProfileAuditLog entry exists
        audit_log = ProfileAuditLog.objects.filter(user=self.cust1).first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.updated_by, self.employee)
        self.assertIn('Alicia', audit_log.changes)

    def test_customer_cannot_access_staff_profile_edit_endpoint(self):
        """Regular customer cannot access the staff profile edit endpoint (HTTP 403 Forbidden)."""
        resp = self.client_alice.get(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}))
        self.assertEqual(resp.status_code, 403)

        post_resp = self.client_alice.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}), {
            'first_name': 'Attacker'
        })
        self.assertEqual(post_resp.status_code, 403)

    def test_profile_completeness_indicator(self):
        """Profile completeness calculates score correctly and reflects KYC verification."""
        # Alice starts with all basic fields filled, but KYC NOT_SUBMITTED (score: 75%)
        self.assertEqual(self.cust1.get_profile_completeness(), 75)

        # Submit simulated KYC -> PENDING (+15% = 90%)
        self.cust1.kyc_status = 'PENDING'
        self.cust1.save()
        self.assertEqual(self.cust1.get_profile_completeness(), 90)

        # Approved KYC -> (+25% = 100%)
        self.cust1.kyc_status = 'APPROVED'
        self.cust1.save()
        self.assertEqual(self.cust1.get_profile_completeness(), 100)

    # =========================================================================
    # 2. INPUT VALIDATION TESTS (STAFF ENDPOINT)
    # =========================================================================

    def test_invalid_email_rejected(self):
        """Staff profile edit with invalid email format is rejected."""
        resp = self.client_emp.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}), {
            'first_name': 'Alicia',
            'last_name': 'Smith',
            'email': 'invalid-email-no-at',
            'phone': '9876543210',
            'address': '123 Elm Street',
            'pincode': '560001'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Please enter a valid email address')
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.email, 'alice@example.com')

    def test_invalid_phone_rejected(self):
        """Phone numbers shorter than 10 or longer than 15 digits are rejected."""
        resp = self.client_emp.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}), {
            'phone': '12345',  # Too short
            'email': 'alice@example.com',
            'pincode': '560001'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Phone number must be between 10 and 15 digits')
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.phone, '9876543210')

    def test_invalid_pincode_rejected(self):
        """Postal codes shorter than 4 digits are rejected."""
        resp = self.client_emp.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}), {
            'pincode': '12',  # Too short
            'phone': '9876543210',
            'email': 'alice@example.com'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Please enter a valid postal code')
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.pincode, '560001')

    # =========================================================================
    # 3. MASS ASSIGNMENT & SECURITY DEFENSE
    # =========================================================================

    def test_mass_assignment_tampering_prevented(self):
        """Staff profile edit cannot alter username, role, staff flags, superuser flags, or account lock status."""
        resp = self.client_emp.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.cust1.id}), {
            'first_name': 'Alicia',
            'last_name': 'Smith',
            'email': 'alice@example.com',
            'phone': '9876543210',
            'address': '123 Elm Street',
            'pincode': '560001',
            # Malicious parameter injections
            'username': 'admin_alice',
            'role': 'ADMIN',
            'is_staff': True,
            'is_superuser': True,
            'account_locked': True,
            'failed_login_attempts': 99,
            'kyc_status': 'APPROVED',
            'kyc_verified': True,
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.username, 'alice')
        self.assertEqual(self.cust1.role, 'CUSTOMER')
        self.assertFalse(self.cust1.is_staff)
        self.assertFalse(self.cust1.is_superuser)
        self.assertFalse(self.cust1.account_locked)
        self.assertEqual(self.cust1.failed_login_attempts, 0)
        self.assertEqual(self.cust1.kyc_status, 'NOT_SUBMITTED')
        self.assertFalse(self.cust1.kyc_verified)
        self.assertEqual(self.cust1.failed_login_attempts, 0)
        self.assertEqual(self.cust1.kyc_status, 'NOT_SUBMITTED')
        self.assertFalse(self.cust1.kyc_verified)

    # =========================================================================
    # 4. SIMULATED KYC WORKFLOW & TRANSITIONS
    # =========================================================================

    def test_submit_simulated_kyc_success(self):
        """Customer can submit simulated KYC document details transitioning to PENDING."""
        resp = self.client_alice.post(reverse('customer_kyc'), {
            'simulated_id_type': 'SIMULATED_PASSPORT',
            'simulated_id_number': 'DEMO-PASS-9988'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Simulated KYC submitted successfully')
        self.assertContains(resp, 'Pending Verification')

        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.kyc_status, 'PENDING')
        self.assertEqual(self.cust1.simulated_id_type, 'SIMULATED_PASSPORT')
        self.assertEqual(self.cust1.simulated_id_number, 'DEMO-PASS-9988')
        self.assertIsNotNone(self.cust1.kyc_submitted_at)
        self.assertFalse(self.cust1.kyc_verified)

    def test_invalid_simulated_doc_type_rejected(self):
        """Selecting an invalid / unapproved document type is rejected."""
        resp = self.client_alice.post(reverse('customer_kyc'), {
            'simulated_id_type': 'REAL_PASSPORT',  # Invalid choice
            'simulated_id_number': 'DOC-123'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Please select a valid simulated document type')
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.kyc_status, 'NOT_SUBMITTED')

    def test_resubmission_blocked_when_pending_or_under_review(self):
        """Customer cannot overwrite or resubmit when KYC is already PENDING or UNDER_REVIEW."""
        self.cust1.kyc_status = 'PENDING'
        self.cust1.simulated_id_type = 'SIMULATED_PASSPORT'
        self.cust1.simulated_id_number = 'DEMO-PASS-1001'
        self.cust1.save()

        resp = self.client_alice.post(reverse('customer_kyc'), {
            'simulated_id_type': 'SIMULATED_NATIONAL_ID',
            'simulated_id_number': 'DEMO-NID-2002'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'currently under review')
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.simulated_id_number, 'DEMO-PASS-1001')

    def test_rejected_kyc_resubmission_clears_reason_and_resets_to_pending(self):
        """When KYC was REJECTED, customer can view reason and successfully resubmit."""
        self.cust1.kyc_status = 'REJECTED'
        self.cust1.kyc_rejection_reason = 'Simulated document number format is invalid.'
        self.cust1.save()

        # View KYC page displays rejection notice
        resp_get = self.client_alice.get(reverse('customer_kyc'))
        self.assertEqual(resp_get.status_code, 200)
        self.assertContains(resp_get, 'Verification Rejected')
        self.assertContains(resp_get, 'Simulated document number format is invalid.')

        # Customer resubmits corrected simulated information
        resp_post = self.client_alice.post(reverse('customer_kyc'), {
            'simulated_id_type': 'SIMULATED_DRIVING_LICENSE',
            'simulated_id_number': 'DEMO-DL-CORRECTED'
        }, follow=True)

        self.assertEqual(resp_post.status_code, 200)
        self.assertContains(resp_post, 'Simulated KYC submitted successfully')

        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.kyc_status, 'PENDING')
        self.assertEqual(self.cust1.simulated_id_type, 'SIMULATED_DRIVING_LICENSE')
        self.assertEqual(self.cust1.simulated_id_number, 'DEMO-DL-CORRECTED')
        self.assertEqual(self.cust1.kyc_rejection_reason, '')  # Prior reason cleared
        self.assertFalse(self.cust1.kyc_verified)

    def test_approved_kyc_record_is_immutable(self):
        """Once KYC is APPROVED, customer cannot modify or resubmit KYC details."""
        self.cust1.kyc_status = 'APPROVED'
        self.cust1.kyc_verified = True
        self.cust1.simulated_id_type = 'SIMULATED_PASSPORT'
        self.cust1.simulated_id_number = 'DEMO-PASS-OFFICIAL'
        self.cust1.save()

        resp = self.client_alice.post(reverse('customer_kyc'), {
            'simulated_id_type': 'SIMULATED_VOTER_CARD',
            'simulated_id_number': 'DEMO-TAMPER-999'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'already approved and verified')

        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.kyc_status, 'APPROVED')
        self.assertEqual(self.cust1.simulated_id_number, 'DEMO-PASS-OFFICIAL')
        self.assertTrue(self.cust1.kyc_verified)

    # =========================================================================
    # 5. STAFF REVIEW & RBAC TESTS
    # =========================================================================

    def test_employee_can_review_and_approve_kyc(self):
        """Employee can approve customer's simulated KYC via staff_kyc_review endpoint."""
        self.cust1.kyc_status = 'PENDING'
        self.cust1.simulated_id_type = 'SIMULATED_PASSPORT'
        self.cust1.simulated_id_number = 'DEMO-PASS-777'
        self.cust1.save()

        resp = self.client_emp.post(reverse('staff_kyc_review', args=[self.cust1.id]), {
            'action': 'approve'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.kyc_status, 'APPROVED')
        self.assertTrue(self.cust1.kyc_verified)
        self.assertEqual(self.cust1.kyc_reviewed_by, self.employee)
        self.assertIsNotNone(self.cust1.kyc_reviewed_at)

    def test_admin_can_review_and_reject_kyc_with_reason(self):
        """Admin can reject customer's simulated KYC with an explicit reason."""
        self.cust1.kyc_status = 'PENDING'
        self.cust1.simulated_id_type = 'SIMULATED_PASSPORT'
        self.cust1.simulated_id_number = 'DEMO-PASS-888'
        self.cust1.save()

        resp = self.client_adm.post(reverse('staff_kyc_review', args=[self.cust1.id]), {
            'action': 'reject',
            'rejection_reason': 'Fictional demo identifier does not meet simulation requirements.'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.cust1.refresh_from_db()
        self.assertEqual(self.cust1.kyc_status, 'REJECTED')
        self.assertFalse(self.cust1.kyc_verified)
        self.assertEqual(self.cust1.kyc_rejection_reason, 'Fictional demo identifier does not meet simulation requirements.')
        self.assertEqual(self.cust1.kyc_reviewed_by, self.admin)

    def test_customer_cannot_call_staff_kyc_review_endpoint(self):
        """Regular customer cannot execute staff KYC review (HTTP 403 Forbidden)."""
        resp = self.client_alice.post(reverse('staff_kyc_review', args=[self.cust2.id]), {
            'action': 'approve'
        })
        self.assertEqual(resp.status_code, 403)

    def test_staff_cannot_access_customer_profile_route(self):
        """Staff (Employee / Admin) receive 403 on customer-only profile routes."""
        resp_emp = self.client_emp.get(reverse('customer_profile'))
        self.assertEqual(resp_emp.status_code, 403)

        resp_adm = self.client_adm.get(reverse('customer_profile'))
        self.assertEqual(resp_adm.status_code, 403)

    # =========================================================================
    # 6. DASHBOARD REGRESSION & INTEGRATION
    # =========================================================================

    def test_customer_dashboard_displays_all_required_navigation(self):
        """Customer dashboard displays links to Profile, KYC, Accounts, Beneficiaries, History, Passbook."""
        resp = self.client_alice.get(reverse('customer_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse('customer_profile'))
        self.assertContains(resp, reverse('customer_kyc'))
        self.assertContains(resp, reverse('beneficiary_list'))
        self.assertContains(resp, reverse('history'))
        self.assertContains(resp, reverse('passbook'))

