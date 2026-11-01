from django.test import TestCase, RequestFactory, Client
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.contrib.auth.models import AnonymousUser
from django.contrib import admin
from django.contrib.messages.storage.fallback import FallbackStorage
from accounts.models import User
from banking.models import BankAccount, Transaction, FixedDeposit
from accounts.decorators import role_required, login_required_with_lock_check
from accounts.views import block_user, unblock_user
from core.admin_site import BebankAdminSite


class RbacDecoratorTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client()

        # Test users
        self.customer = User.objects.create_user(
            username='cust', password='pwd', role='CUSTOMER', is_staff=False
        )
        self.customer_staff = User.objects.create_user(
            username='cust_staff', password='pwd', role='CUSTOMER', is_staff=True
        )
        self.employee = User.objects.create_user(
            username='emp', password='pwd', role='EMPLOYEE', is_staff=False
        )
        self.employee_staff = User.objects.create_user(
            username='emp_staff', password='pwd', role='EMPLOYEE', is_staff=True
        )
        self.admin = User.objects.create_superuser(
            username='adm', password='pwd', role='ADMIN', is_staff=True
        )
        self.admin_non_staff = User.objects.create_user(
            username='adm_non_staff', password='pwd', role='ADMIN', is_staff=False
        )
        self.admin_inactive = User.objects.create_superuser(
            username='adm_inactive', password='pwd', role='ADMIN', is_staff=True, is_active=False
        )
        self.locked_user = User.objects.create_user(
            username='locked', password='pwd', role='CUSTOMER', account_locked=True
        )
        self.inactive_user = User.objects.create_user(
            username='inactive', password='pwd', role='CUSTOMER', is_active=False
        )
        self.locked_admin = User.objects.create_superuser(
            username='locked_adm', password='pwd', role='ADMIN', is_staff=True, account_locked=True
        )

    # ---------------------------------------------------------
    # 1. Anonymous user redirect and query parameter preservation
    # ---------------------------------------------------------

    def test_anonymous_redirected_to_login_with_next_param(self):
        @login_required_with_lock_check
        def dummy_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dashboard/view/?tab=statements')
        req.user = AnonymousUser()
        resp = dummy_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)
        self.assertIn('next=/dashboard/view/%3Ftab%3Dstatements', resp.url)

    def test_anonymous_blocked_from_role_view_with_next_param(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/admin-dashboard/?filter=all')
        req.user = AnonymousUser()
        resp = admin_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)
        self.assertIn('next=/admin-dashboard/%3Ffilter%3Dall', resp.url)

    # ---------------------------------------------------------
    # 2. Inactive / locked user access denial
    # ---------------------------------------------------------

    def test_locked_user_denied_access(self):
        @login_required_with_lock_check
        def dummy_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy/')
        req.user = self.locked_user
        resp = dummy_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_inactive_user_denied_access(self):
        @login_required_with_lock_check
        def dummy_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy/')
        req.user = self.inactive_user
        resp = dummy_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_locked_admin_denied_access_to_role_view(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/admin-dashboard/')
        req.user = self.locked_admin
        resp = admin_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_inactive_admin_denied_access_to_role_view(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/admin-dashboard/')
        req.user = self.admin_inactive
        resp = admin_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    # ---------------------------------------------------------
    # 3. Role-based authorization behavior (CUSTOMER, EMPLOYEE, ADMIN)
    # ---------------------------------------------------------

    def test_customer_accessing_admin_role_raises_403(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy-admin/')
        req.user = self.customer
        with self.assertRaises(PermissionDenied):
            admin_view(req)

    def test_employee_accessing_admin_role_raises_403(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy-admin/')
        req.user = self.employee
        with self.assertRaises(PermissionDenied):
            admin_view(req)

    def test_admin_accessing_admin_role_succeeds(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy-admin/')
        req.user = self.admin
        resp = admin_view(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), "OK")

    def test_multiple_allowed_roles_accepts_valid_and_rejects_unauthorized(self):
        @role_required('EMPLOYEE', 'ADMIN')
        def staff_view(request):
            return HttpResponse("STAFF_OK")

        # Employee permitted
        req_emp = self.factory.get('/staff-portal/')
        req_emp.user = self.employee
        resp_emp = staff_view(req_emp)
        self.assertEqual(resp_emp.status_code, 200)

        # Admin permitted
        req_adm = self.factory.get('/staff-portal/')
        req_adm.user = self.admin
        resp_adm = staff_view(req_adm)
        self.assertEqual(resp_adm.status_code, 200)

        # Customer forbidden
        req_cust = self.factory.get('/staff-portal/')
        req_cust.user = self.customer
        with self.assertRaises(PermissionDenied):
            staff_view(req_cust)

    def test_role_required_supports_iterable_of_roles(self):
        @role_required(['CUSTOMER', 'EMPLOYEE'])
        def customer_or_employee_view(request):
            return HttpResponse("OP_OK")

        req_cust = self.factory.get('/ops/')
        req_cust.user = self.customer
        self.assertEqual(customer_or_employee_view(req_cust).status_code, 200)

        req_adm = self.factory.get('/ops/')
        req_adm.user = self.admin
        with self.assertRaises(PermissionDenied):
            customer_or_employee_view(req_adm)

    # ---------------------------------------------------------
    # 4. Django Admin Dual-Key Authorization
    # ---------------------------------------------------------

    def test_anonymous_blocked_from_django_admin(self):
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])
        if response.status_code == 302:
            self.assertIn('/admin/login/', response.url)

    def test_customer_blocked_from_django_admin(self):
        self.client.force_login(self.customer)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_customer_with_is_staff_still_blocked_from_django_admin(self):
        """Even if is_staff=True, CUSTOMER role must be rejected from Django Admin."""
        self.client.force_login(self.customer_staff)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_employee_blocked_from_django_admin(self):
        """Standard employee (is_staff=False) blocked from Django Admin."""
        self.client.force_login(self.employee)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_employee_with_is_staff_still_blocked_from_django_admin(self):
        """Even if is_staff=True, EMPLOYEE role must be rejected from Django Admin."""
        self.client.force_login(self.employee_staff)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_admin_without_is_staff_blocked_from_django_admin(self):
        """User with role='ADMIN' but is_staff=False must be rejected."""
        self.client.force_login(self.admin_non_staff)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_admin_inactive_blocked_from_django_admin(self):
        """Inactive ADMIN account must be rejected."""
        self.client.force_login(self.admin_inactive)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_locked_admin_blocked_from_django_admin(self):
        """Locked ADMIN account must be rejected."""
        self.client.force_login(self.locked_admin)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_admin_with_all_three_conditions_can_access_django_admin(self):
        """Only users with is_active=True AND is_staff=True AND role='ADMIN' can access Django Admin."""
        self.client.force_login(self.admin)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)

    # ---------------------------------------------------------
    # 5. Existing Admin Registrations & Model Route Functionality
    # ---------------------------------------------------------

    def test_default_admin_site_is_bebank_admin_site(self):
        """Verify the global admin.site singleton is an instance of our custom BebankAdminSite."""
        self.assertIsInstance(admin.site, BebankAdminSite)

    def test_existing_admin_registrations_and_routes_remain_functional(self):
        """Verify all existing models are registered and their admin changelists are accessible."""
        registered_models = admin.site._registry
        self.assertIn(User, registered_models)
        self.assertIn(BankAccount, registered_models)
        self.assertIn(Transaction, registered_models)
        self.assertIn(FixedDeposit, registered_models)

        # Verify admin user can view model changelist page
        self.client.force_login(self.admin)
        response = self.client.get('/admin/accounts/user/')
        self.assertEqual(response.status_code, 200)


class AccountsViewsRbacTests(TestCase):
    """
    Integration tests verifying authentication, account-locking checks, and RBAC
    enforcement on accounts views and dashboards.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client()

        # Users across roles and states
        self.customer = User.objects.create_user(
            username='view_cust', password='pwd', role='CUSTOMER', is_staff=False
        )
        self.employee = User.objects.create_user(
            username='view_emp', password='pwd', role='EMPLOYEE', is_staff=False
        )
        self.admin = User.objects.create_superuser(
            username='view_adm', password='pwd', role='ADMIN', is_staff=True
        )
        self.locked_user = User.objects.create_user(
            username='view_locked', password='pwd', role='CUSTOMER', account_locked=True
        )
        self.inactive_user = User.objects.create_user(
            username='view_inactive', password='pwd', role='CUSTOMER', is_active=False
        )
        self.locked_admin = User.objects.create_superuser(
            username='view_locked_adm', password='pwd', role='ADMIN', is_staff=True, account_locked=True
        )
        self.inactive_admin = User.objects.create_superuser(
            username='view_inactive_adm', password='pwd', role='ADMIN', is_staff=True, is_active=False
        )
        self.target_user = User.objects.create_user(
            username='view_target', password='pwd', role='CUSTOMER'
        )

        # Customer account for dashboard rendering
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='ACC_RBAC_1001',
            account_type='SAVINGS',
            balance=1000
        )

    # ---------------------------------------------------------
    # 1. Unauthenticated (Anonymous) Access to Dashboards
    # ---------------------------------------------------------

    def test_anonymous_access_to_all_dashboards_redirects_to_login(self):
        """Ensure unauthenticated access to all 3 dashboards redirects to login with next param."""
        dashboards = [
            ('/customer/dashboard/', '/login/?next=/customer/dashboard/'),
            ('/employee/dashboard/', '/login/?next=/employee/dashboard/'),
            ('/admin-dashboard/', '/login/?next=/admin-dashboard/'),
        ]
        for url, expected_redirect in dashboards:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(expected_redirect, response.url)

    def test_anonymous_customer_dashboard_does_not_crash_orm(self):
        """Ensure customer dashboard does not crash with SimpleLazyObject TypeError when unauthenticated."""
        response = self.client.get('/customer/dashboard/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    # ---------------------------------------------------------
    # 2. CUSTOMER Dashboard Access & Boundary Enforcement
    # ---------------------------------------------------------

    def test_customer_can_access_customer_dashboard(self):
        """CUSTOMER role can successfully access customer dashboard and view rendered account."""
        self.client.force_login(self.customer)
        response = self.client.get('/customer/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'ACC_RBAC_1001')

    def test_customer_cannot_access_employee_dashboard(self):
        """CUSTOMER role is denied access (HTTP 403) from employee dashboard."""
        self.client.force_login(self.customer)
        response = self.client.get('/employee/dashboard/')
        self.assertEqual(response.status_code, 403)

    def test_customer_cannot_access_admin_dashboard(self):
        """CUSTOMER role is denied access (HTTP 403) from admin dashboard."""
        self.client.force_login(self.customer)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(response.status_code, 403)

    # ---------------------------------------------------------
    # 3. EMPLOYEE Dashboard Access & Boundary Enforcement
    # ---------------------------------------------------------

    def test_employee_can_access_employee_dashboard(self):
        """EMPLOYEE role can successfully access employee dashboard."""
        self.client.force_login(self.employee)
        response = self.client.get('/employee/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_employee_cannot_access_customer_dashboard(self):
        """EMPLOYEE role is denied access (HTTP 403) from customer dashboard."""
        self.client.force_login(self.employee)
        response = self.client.get('/customer/dashboard/')
        self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_admin_dashboard(self):
        """EMPLOYEE role is denied access (HTTP 403) from admin dashboard."""
        self.client.force_login(self.employee)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(response.status_code, 403)

    # ---------------------------------------------------------
    # 4. ADMIN Dashboard Access & Boundary Enforcement
    # ---------------------------------------------------------

    def test_admin_can_access_admin_dashboard(self):
        """ADMIN role can successfully access admin dashboard."""
        self.client.force_login(self.admin)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_admin_can_access_employee_dashboard(self):
        """ADMIN role can access employee dashboard (shared staff access)."""
        self.client.force_login(self.admin)
        response = self.client.get('/employee/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_admin_cannot_access_customer_dashboard(self):
        """ADMIN role is denied access (HTTP 403) from customer dashboard."""
        self.client.force_login(self.admin)
        response = self.client.get('/customer/dashboard/')
        self.assertEqual(response.status_code, 403)

    # ---------------------------------------------------------
    # 5. Admin User Block / Unblock Actions
    # ---------------------------------------------------------

    def test_customer_cannot_block_or_unblock_users(self):
        """CUSTOMER role cannot execute block_user or unblock_user view actions."""
        req_block = self.factory.get(f'/admin/block/{self.target_user.id}/')
        req_block.user = self.customer
        with self.assertRaises(PermissionDenied):
            block_user(req_block, self.target_user.id)

        req_unblock = self.factory.get(f'/admin/unblock/{self.target_user.id}/')
        req_unblock.user = self.customer
        with self.assertRaises(PermissionDenied):
            unblock_user(req_unblock, self.target_user.id)

    def test_employee_cannot_block_or_unblock_users(self):
        """EMPLOYEE role cannot execute block_user or unblock_user view actions."""
        req_block = self.factory.get(f'/admin/block/{self.target_user.id}/')
        req_block.user = self.employee
        with self.assertRaises(PermissionDenied):
            block_user(req_block, self.target_user.id)

        req_unblock = self.factory.get(f'/admin/unblock/{self.target_user.id}/')
        req_unblock.user = self.employee
        with self.assertRaises(PermissionDenied):
            unblock_user(req_unblock, self.target_user.id)

    def test_admin_can_block_and_unblock_users(self):
        """ADMIN role can execute block_user and unblock_user actions successfully."""
        # Test blocking
        req_block = self.factory.get(f'/admin/block/{self.target_user.id}/')
        req_block.user = self.admin
        setattr(req_block, 'session', {})
        setattr(req_block, '_messages', FallbackStorage(req_block))

        resp_block = block_user(req_block, self.target_user.id)
        self.assertEqual(resp_block.status_code, 302)
        self.target_user.refresh_from_db()
        self.assertTrue(self.target_user.account_locked)

        # Test unblocking
        req_unblock = self.factory.get(f'/admin/unblock/{self.target_user.id}/')
        req_unblock.user = self.admin
        setattr(req_unblock, 'session', {})
        setattr(req_unblock, '_messages', FallbackStorage(req_unblock))

        resp_unblock = unblock_user(req_unblock, self.target_user.id)
        self.assertEqual(resp_unblock.status_code, 302)
        self.target_user.refresh_from_db()
        self.assertFalse(self.target_user.account_locked)

    # ---------------------------------------------------------
    # 6. Locked and Inactive User Denial
    # ---------------------------------------------------------

    def test_locked_users_denied_from_dashboards_and_redirected_to_login(self):
        """Locked accounts cannot access dashboards and are redirected to login."""
        self.client.force_login(self.locked_user)
        resp_cust = self.client.get('/customer/dashboard/')
        self.assertEqual(resp_cust.status_code, 302)
        self.assertIn('/login/', resp_cust.url)

        self.client.force_login(self.locked_admin)
        resp_adm = self.client.get('/admin-dashboard/')
        self.assertEqual(resp_adm.status_code, 302)
        self.assertIn('/login/', resp_adm.url)

    def test_inactive_users_denied_from_dashboards_and_redirected_to_login(self):
        """Inactive accounts cannot access dashboards and are redirected to login."""
        self.client.force_login(self.inactive_user)
        resp_cust = self.client.get('/customer/dashboard/')
        self.assertEqual(resp_cust.status_code, 302)
        self.assertIn('/login/', resp_cust.url)

        self.client.force_login(self.inactive_admin)
        resp_adm = self.client.get('/admin-dashboard/')
        self.assertEqual(resp_adm.status_code, 302)
        self.assertIn('/login/', resp_adm.url)
