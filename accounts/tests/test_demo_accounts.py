import io
import os
from unittest.mock import patch
from decimal import Decimal
from django.test import TestCase, Client
from django.core.management import call_command, CommandError
from django.urls import reverse
from accounts.models import User
from banking.models import BankAccount


class ProvisionDemoAccountsTests(TestCase):
    """
    Unit & integration tests for Task 1.3:
    - Provisioning Customer (manojkumar), Employee (murugananthan), Admin (nithishkumar)
    - Idempotent repeated runs
    - Role conflict safety
    - Local SQLite database guard
    - Authentication and role-specific dashboard routing
    - Strict RBAC and object-level account isolation
    """

    DEMO_PASSWORDS = {
        'customer_password': 'test_manoj_pass_123',
        'employee_password': 'test_muruki_pass_123',
        'admin_password': 'test_nithis_pass_123',
    }

    def test_provision_demo_accounts_success(self):
        """Management command must create all 3 demo accounts and 1 linked BankAccount for customer."""
        out = io.StringIO()
        call_command('provision_demo_accounts', stdout=out, **self.DEMO_PASSWORDS)

        output = out.getvalue()
        self.assertIn("manojkumar", output)
        self.assertIn("murugananthan", output)
        self.assertIn("nithishkumar", output)
        # Ensure passwords are NEVER printed in terminal output
        for pwd in self.DEMO_PASSWORDS.values():
            self.assertNotIn(pwd, output)

        # 1. Customer checks
        cust = User.objects.get(username='manojkumar')
        self.assertEqual(cust.role, 'CUSTOMER')
        self.assertFalse(cust.is_staff)
        self.assertFalse(cust.is_superuser)
        self.assertTrue(cust.is_active)
        self.assertFalse(cust.account_locked)
        self.assertTrue(cust.check_password(self.DEMO_PASSWORDS['customer_password']))

        accounts = BankAccount.objects.filter(user=cust)
        self.assertEqual(accounts.count(), 1)
        account = accounts.first()
        self.assertEqual(account.account_type, 'SAVINGS')
        self.assertEqual(account.balance, Decimal('0.00'))

        # 2. Employee checks
        emp = User.objects.get(username='murugananthan')
        self.assertEqual(emp.role, 'EMPLOYEE')
        self.assertEqual(emp.first_name, 'Murugananthan')
        self.assertFalse(emp.is_staff)
        self.assertFalse(emp.is_superuser)
        self.assertTrue(emp.is_active)
        self.assertFalse(emp.account_locked)
        self.assertTrue(emp.check_password(self.DEMO_PASSWORDS['employee_password']))

        # 3. Admin checks
        adm = User.objects.get(username='nithishkumar')
        self.assertEqual(adm.role, 'ADMIN')
        self.assertTrue(adm.is_staff)
        self.assertTrue(adm.is_superuser)
        self.assertTrue(adm.is_active)
        self.assertFalse(adm.account_locked)
        self.assertTrue(adm.check_password(self.DEMO_PASSWORDS['admin_password']))

    def test_idempotent_repeated_provisioning(self):
        """Repeated runs must safely update existing accounts and never duplicate users or bank accounts."""
        call_command('provision_demo_accounts', **self.DEMO_PASSWORDS)
        self.assertEqual(User.objects.filter(username='manojkumar').count(), 1)
        self.assertEqual(BankAccount.objects.filter(user__username='manojkumar').count(), 1)

        # Run second time with new passwords
        updated_passwords = {
            'customer_password': 'updated_manoj_pass_456',
            'employee_password': 'updated_muruki_pass_456',
            'admin_password': 'updated_nithis_pass_456',
        }
        out2 = io.StringIO()
        call_command('provision_demo_accounts', stdout=out2, **updated_passwords)
        output2 = out2.getvalue()

        self.assertIn("Updated existing Customer user", output2)
        self.assertIn("skipping duplicate account creation", output2)
        self.assertEqual(User.objects.filter(username='manojkumar').count(), 1)
        self.assertEqual(User.objects.filter(username='murugananthan').count(), 1)
        self.assertEqual(User.objects.filter(username='nithishkumar').count(), 1)
        self.assertEqual(BankAccount.objects.filter(user__username='manojkumar').count(), 1)

        # Check updated passwords work
        cust = User.objects.get(username='manojkumar')
        self.assertTrue(cust.check_password('updated_manoj_pass_456'))

    def test_role_conflict_aborts_without_altering_privileges(self):
        """If a target username already exists with a different role, abort with CommandError."""
        User.objects.create_user(
            username='manojkumar',
            password='ExistingPassword123',
            role='EMPLOYEE'  # Conflicting role (expected CUSTOMER)
        )

        with self.assertRaises(CommandError) as ctx:
            call_command('provision_demo_accounts', **self.DEMO_PASSWORDS)

        self.assertIn("conflicting role", str(ctx.exception))
        # Verify role was not changed
        user = User.objects.get(username='manojkumar')
        self.assertEqual(user.role, 'EMPLOYEE')

    def test_remote_database_guard_rejects_non_local_db(self):
        """Reject execution if DATABASE_URL is set in environment or connection vendor is non-sqlite."""
        with patch.dict(os.environ, {'DATABASE_URL': 'postgres://user:pwd@neon.tech/db'}):
            with self.assertRaises(CommandError) as ctx:
                call_command('provision_demo_accounts', **self.DEMO_PASSWORDS)
            self.assertIn("DATABASE_URL environment variable is set", str(ctx.exception))

    def test_authentication_and_role_dashboards(self):
        """Verify client login authentication and role-specific dashboard navigation for all three demo accounts."""
        call_command('provision_demo_accounts', **self.DEMO_PASSWORDS)
        client = Client()

        # 1. Customer Login & Dashboard
        resp_cust = client.post(reverse('login'), {
            'username': 'manojkumar',
            'password': self.DEMO_PASSWORDS['customer_password'],
            'role': 'CUSTOMER',
        })
        self.assertEqual(resp_cust.status_code, 302)
        self.assertRedirects(resp_cust, reverse('customer_dashboard'))

        resp_dash_cust = client.get(reverse('customer_dashboard'))
        self.assertEqual(resp_dash_cust.status_code, 200)
        account = BankAccount.objects.get(user__username='manojkumar')
        self.assertContains(resp_dash_cust, account.account_number)

        # Customer cannot access employee or admin dashboards
        self.assertEqual(client.get(reverse('employee_dashboard')).status_code, 403)
        self.assertEqual(client.get(reverse('admin_dashboard')).status_code, 403)
        client.logout()

        # 2. Employee Login & Dashboard
        resp_emp = client.post(reverse('login'), {
            'username': 'murugananthan',
            'password': self.DEMO_PASSWORDS['employee_password'],
            'role': 'EMPLOYEE',
        })
        self.assertEqual(resp_emp.status_code, 302)
        self.assertRedirects(resp_emp, reverse('employee_dashboard'))

        resp_dash_emp = client.get(reverse('employee_dashboard'))
        self.assertEqual(resp_dash_emp.status_code, 200)

        # Employee cannot access admin dashboard or customer-only transaction endpoints
        self.assertEqual(client.get(reverse('admin_dashboard')).status_code, 403)
        self.assertEqual(client.get(reverse('withdraw')).status_code, 403)
        self.assertEqual(client.get(reverse('transfer')).status_code, 403)
        self.assertEqual(client.get('/admin/').status_code, 302)  # Staff required for Django admin
        client.logout()

        # 3. Admin Login & Dashboard
        resp_adm = client.post(reverse('login'), {
            'username': 'nithishkumar',
            'password': self.DEMO_PASSWORDS['admin_password'],
            'role': 'ADMIN',
        })
        self.assertEqual(resp_adm.status_code, 302)
        self.assertRedirects(resp_adm, reverse('admin_dashboard'))

        resp_dash_adm = client.get(reverse('admin_dashboard'))
        self.assertEqual(resp_dash_adm.status_code, 200)

        # Admin can access Django admin (/admin/)
        resp_django_admin = client.get('/admin/')
        self.assertEqual(resp_django_admin.status_code, 200)
        client.logout()

    def test_customer_isolation_cannot_see_other_accounts(self):
        """Customer 'manojkumar' must see only their own account and never another customer's data."""
        call_command('provision_demo_accounts', **self.DEMO_PASSWORDS)

        # Create a second customer
        other_user = User.objects.create_user(username='other_cust', password='pwd', role='CUSTOMER')
        other_account = BankAccount.objects.create(
            user=other_user,
            account_number='9999000001',
            account_type='SAVINGS',
            balance=Decimal('25000.00')
        )

        client = Client()
        client.login(username='manojkumar', password=self.DEMO_PASSWORDS['customer_password'])

        resp_dash = client.get(reverse('customer_dashboard'))
        self.assertContains(resp_dash, '10001')
        self.assertNotContains(resp_dash, '9999000001')
        self.assertNotContains(resp_dash, '25000.00')

        # Attempt to target other account via query parameter
        resp_idor = client.get(f"/customer/dashboard/?account_id={other_account.id}")
        self.assertEqual(resp_idor.status_code, 404)
