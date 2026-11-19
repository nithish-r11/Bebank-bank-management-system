from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User
from banking.models import (
    BankAccount, Transaction, Loan, FixedDeposit, VirtualDebitCard,
    SupportTicket, EmployeeActivityLog
)


class AdminModuleTests(TestCase):
    """
    Comprehensive test suite for Task 4.1:
    - Admin Dashboard metrics and live database telemetry
    - Employee Management: search, filters, pagination, detail view
    - Secure Employee Creation: role='EMPLOYEE', non-superuser, password hashing, validator checks
    - Whitelisted profile updates & audit logs
    - Account status controls: activation/deactivation, lock/unlock
    - Guardrails: Self-lockout prevention, Last active admin protection
    - Admin permissions matrix, system settings, and backup archive
    - Cross-role RBAC barriers (Customer & Employee blocked with 403 Forbidden)
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

        # Second Admin user for last-active-admin tests
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
            username='emp_test',
            email='emp_test@bebank.local',
            password='EmployeePass123!',
            role='EMPLOYEE',
            first_name='Manoj',
            last_name='Staff',
            phone='9876543210',
            is_active=True,
            account_locked=False
        )

        # Customer user
        self.customer = User.objects.create_user(
            username='cust_test',
            email='cust_test@bebank.local',
            password='CustomerPass123!',
            role='CUSTOMER',
            is_active=True,
            account_locked=False
        )

        # Bank accounts
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='ACC-99001',
            account_type='SAVINGS',
            balance=Decimal('25000.00')
        )

    # =========================================================================
    # 1. ADMIN DASHBOARD & METRICS
    # =========================================================================

    def test_admin_dashboard_access_and_live_metrics(self):
        """Admin can access dashboard and receives real database aggregations."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_dashboard.html')

        # Context metrics verification
        self.assertEqual(response.context['total_customers'], 1)
        self.assertEqual(response.context['total_employees'], 1)
        self.assertEqual(response.context['total_admins'], 2)
        self.assertEqual(response.context['total_active_accounts'], 1)
        self.assertEqual(response.context['total_balance'], Decimal('25000.00'))

    def test_non_admin_cannot_access_admin_dashboard(self):
        """Customers and Employees receive 403 Forbidden on Admin Dashboard."""
        # Employee attempt
        self.client.force_login(self.employee)
        resp_emp = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp_emp.status_code, 403)

        # Customer attempt
        self.client.force_login(self.customer)
        resp_cust = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(resp_cust.status_code, 403)

    def test_unauthenticated_access_redirects_to_login(self):
        """Unauthenticated requests redirect to login."""
        response = self.client.get(reverse('admin_dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    # =========================================================================
    # 2. EMPLOYEE DIRECTORY: SEARCH, FILTER, PAGINATION
    # =========================================================================

    def test_admin_employee_list_view_and_search(self):
        """Admin can list and search employees by username or name."""
        # Create additional employees
        User.objects.create_user(
            username='ramesh_teller',
            email='ramesh@bebank.local',
            password='TestPass123!',
            role='EMPLOYEE',
            first_name='Ramesh',
            last_name='Kumar',
            phone='9123456780'
        )

        self.client.force_login(self.admin)
        url = reverse('admin_employee_list')

        # List all employees
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['page_obj']), 2)

        # Search by username
        resp_search = self.client.get(url, {'q': 'ramesh'})
        self.assertEqual(resp_search.status_code, 200)
        self.assertEqual(len(resp_search.context['page_obj']), 1)
        self.assertEqual(resp_search.context['page_obj'][0].username, 'ramesh_teller')

    def test_admin_employee_list_status_and_lock_filters(self):
        """Admin can filter employee directory by active/inactive and locked/unlocked status."""
        self.employee.is_active = False
        self.employee.account_locked = True
        self.employee.save()

        self.client.force_login(self.admin)
        url = reverse('admin_employee_list')

        # Filter active
        resp_active = self.client.get(url, {'status': 'active'})
        self.assertEqual(len(resp_active.context['page_obj']), 0)

        # Filter inactive
        resp_inactive = self.client.get(url, {'status': 'inactive'})
        self.assertEqual(len(resp_inactive.context['page_obj']), 1)
        self.assertEqual(resp_inactive.context['page_obj'][0].username, self.employee.username)

        # Filter locked
        resp_locked = self.client.get(url, {'lock': 'locked'})
        self.assertEqual(len(resp_locked.context['page_obj']), 1)

    # =========================================================================
    # 3. SECURE EMPLOYEE CREATION
    # =========================================================================

    def test_secure_employee_creation_success(self):
        """Admin creates employee: role='EMPLOYEE', is_superuser=False, password hashed, audit log created."""
        self.client.force_login(self.admin)
        url = reverse('admin_employee_create')

        post_data = {
            'username': 'newstaff_01',
            'email': 'newstaff@bebank.local',
            'password': 'SecureStaffPassword123!',
            'confirm_password': 'SecureStaffPassword123!',
            'first_name': 'Karthik',
            'last_name': 'Subbaraj',
            'phone': '9876501234',
            'address': 'BEBANK Chennai Main Branch',
            'pincode': '600001'
        }

        resp = self.client.post(url, post_data)
        self.assertEqual(resp.status_code, 302)

        # Verify created user in DB
        new_emp = User.objects.filter(username='newstaff_01').first()
        self.assertIsNotNone(new_emp)
        self.assertEqual(new_emp.role, 'EMPLOYEE')
        self.assertFalse(new_emp.is_superuser)
        self.assertFalse(new_emp.is_staff)
        self.assertTrue(new_emp.is_active)
        self.assertFalse(new_emp.account_locked)
        self.assertTrue(new_emp.check_password('SecureStaffPassword123!'))

        # Verify audit log
        audit = EmployeeActivityLog.objects.filter(
            employee=self.admin,
            action_type='EMPLOYEE_CREATE',
            target_id='newstaff_01'
        ).first()
        self.assertIsNotNone(audit)
        self.assertIn('Admin created employee', audit.description)

    def test_employee_creation_validations(self):
        """Password mismatch, duplicate username, and invalid emails are rejected."""
        self.client.force_login(self.admin)
        url = reverse('admin_employee_create')

        # 1. Password mismatch
        resp_mismatch = self.client.post(url, {
            'username': 'staff_mismatch',
            'email': 'mismatch@bebank.local',
            'password': 'Password123!',
            'confirm_password': 'DifferentPassword123!'
        })
        self.assertEqual(resp_mismatch.status_code, 200)
        self.assertFalse(User.objects.filter(username='staff_mismatch').exists())

        # 2. Duplicate username
        resp_dup = self.client.post(url, {
            'username': self.employee.username,
            'email': 'different_email@bebank.local',
            'password': 'Password123!',
            'confirm_password': 'Password123!'
        })
        self.assertEqual(resp_dup.status_code, 200)

        # 3. Invalid email
        resp_email = self.client.post(url, {
            'username': 'staff_invalid_email',
            'email': 'not-an-email',
            'password': 'Password123!',
            'confirm_password': 'Password123!'
        })
        self.assertEqual(resp_email.status_code, 200)
        self.assertFalse(User.objects.filter(username='staff_invalid_email').exists())

    # =========================================================================
    # 4. EMPLOYEE DETAIL & WHITELISTED PROFILE EDITING
    # =========================================================================

    def test_admin_employee_detail_view(self):
        """Admin can view employee detail with operational metrics."""
        self.client.force_login(self.admin)
        url = reverse('admin_employee_detail', kwargs={'user_id': self.employee.id})
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'admin_employee_detail.html')
        self.assertEqual(resp.context['employee'].id, self.employee.id)

    def test_admin_employee_edit_whitelisted_fields_only(self):
        """Admin can update permitted profile fields; uneditable fields cannot be modified."""
        self.client.force_login(self.admin)
        url = reverse('admin_employee_edit', kwargs={'user_id': self.employee.id})

        post_data = {
            'first_name': 'Manojkumar',
            'last_name': 'SeniorStaff',
            'email': 'manoj.updated@bebank.local',
            'phone': '9998887776',
            'address': 'New Branch Headquarters',
            'pincode': '600002',
            # Malicious parameter injections
            'role': 'ADMIN',
            'is_superuser': 'True',
            'account_locked': 'True'
        }

        resp = self.client.post(url, post_data)
        self.assertEqual(resp.status_code, 302)

        self.employee.refresh_from_db()
        self.assertEqual(self.employee.first_name, 'Manojkumar')
        self.assertEqual(self.employee.email, 'manoj.updated@bebank.local')
        self.assertEqual(self.employee.pincode, '600002')

        # Invariant checks: role and superuser must NOT change
        self.assertEqual(self.employee.role, 'EMPLOYEE')
        self.assertFalse(self.employee.is_superuser)
        self.assertFalse(self.employee.account_locked)

        # Audit log verified
        audit = EmployeeActivityLog.objects.filter(
            employee=self.admin,
            action_type='EMPLOYEE_PROFILE_UPDATE',
            target_id=self.employee.username
        ).first()
        self.assertIsNotNone(audit)

    # =========================================================================
    # 5. ACTIVATION / DEACTIVATION & LOCK / UNLOCK CONTROLS
    # =========================================================================

    def test_admin_can_toggle_employee_active_status(self):
        """Admin can deactivate and reactivate an employee safely."""
        self.client.force_login(self.admin)
        toggle_url = reverse('admin_employee_toggle_active', kwargs={'user_id': self.employee.id})

        # Deactivate
        resp = self.client.post(toggle_url)
        self.assertEqual(resp.status_code, 302)
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.is_active)

        # Reactivate
        resp2 = self.client.post(toggle_url)
        self.assertEqual(resp2.status_code, 302)
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active)

    def test_admin_can_toggle_employee_lock_status(self):
        """Admin can lock and unlock an employee account (resets failed logins)."""
        self.client.force_login(self.admin)
        lock_url = reverse('admin_employee_toggle_lock', kwargs={'user_id': self.employee.id})

        # Lock
        self.client.post(lock_url)
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.account_locked)

        # Unlock (resets failed attempts)
        self.employee.failed_login_attempts = 3
        self.employee.save()

        self.client.post(lock_url)
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.account_locked)
        self.assertEqual(self.employee.failed_login_attempts, 0)

    def test_self_lock_and_self_deactivation_prevention(self):
        """Administrators cannot deactivate or lock their own account."""
        self.client.force_login(self.admin)

        # Self deactivation
        resp_act = self.client.post(reverse('admin_employee_toggle_active', kwargs={'user_id': self.admin.id}))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active, "Admin must not be able to deactivate own account")

        # Self lock
        resp_lock = self.client.post(reverse('admin_employee_toggle_lock', kwargs={'user_id': self.admin.id}))
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.account_locked, "Admin must not be able to lock own account")

    def test_last_active_administrator_protection(self):
        """System prohibits deactivating or locking the only active administrator."""
        # Deactivate second admin first
        self.second_admin.is_active = False
        self.second_admin.save()

        self.client.force_login(self.admin)

        # Attempting to deactivate the only active admin via target ID
        resp = self.client.post(reverse('admin_employee_toggle_active', kwargs={'user_id': self.admin.id}))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    # =========================================================================
    # 6. PERMISSIONS, SETTINGS & BACKUP ARCHIVE
    # =========================================================================

    def test_admin_permissions_screen(self):
        """Admin permissions view renders 200 for Admin and 403 for other roles."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_permissions'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'admin_permissions.html')

        # Employee blocked
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse('admin_permissions')).status_code, 403)

    def test_admin_settings_screen(self):
        """Admin settings view renders 200 for Admin and 403 for other roles."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_settings'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'admin_settings.html')

        # Customer blocked
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse('admin_settings')).status_code, 403)

    def test_admin_backup_view_and_download(self):
        """Admin backup view displays storage health, and download returns file attachment."""
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_backup'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'admin_backup.html')

        # Test download
        resp_dl = self.client.get(reverse('admin_backup_download'))
        self.assertEqual(resp_dl.status_code, 200)
        self.assertTrue(resp_dl.has_header('Content-Disposition'))
        self.assertIn('attachment', resp_dl['Content-Disposition'])

        # Audit log verified
        audit = EmployeeActivityLog.objects.filter(
            employee=self.admin,
            action_type='ADMIN_BACKUP_ACTION'
        ).first()
        self.assertIsNotNone(audit)

    # =========================================================================
    # 7. CROSS-ROLE AUTHORIZATION BARRIERS
    # =========================================================================

    def test_employee_and_customer_blocked_from_all_admin_endpoints(self):
        """Employees and Customers receive 403 Forbidden across all Admin endpoints."""
        admin_urls = [
            reverse('admin_dashboard'),
            reverse('admin_employee_list'),
            reverse('admin_employee_create'),
            reverse('admin_employee_detail', kwargs={'user_id': self.employee.id}),
            reverse('admin_employee_edit', kwargs={'user_id': self.employee.id}),
            reverse('admin_permissions'),
            reverse('admin_settings'),
            reverse('admin_backup'),
            reverse('admin_backup_download'),
        ]

        # Test with Employee
        self.client.force_login(self.employee)
        for url in admin_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 403, f"Employee was not blocked from {url}")

        # Test with Customer
        self.client.force_login(self.customer)
        for url in admin_urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 403, f"Customer was not blocked from {url}")
