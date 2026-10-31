import os
from io import StringIO
from unittest.mock import patch
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase
from accounts.models import User

class BootstrapCommandTests(TestCase):
    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'secureadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_ADMIN_EMAIL': 'admin@simulated.bebank.local',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'secureemployee',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': 'BebankBranchPass#2026',
        'BOOTSTRAP_EMPLOYEE_EMAIL': 'emp@simulated.bebank.local',
    }, clear=True)
    def test_bootstrap_creates_admin_and_isolated_employee(self):
        call_command('bootstrap_system_users')

        admin = User.objects.get(username='secureadmin')
        self.assertEqual(admin.role, 'ADMIN')
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password('SuperSecureAdminPassword2026!'))

        # CRITICAL: Employee must NOT have Django Admin staff or superuser access
        emp = User.objects.get(username='secureemployee')
        self.assertEqual(emp.role, 'EMPLOYEE')
        self.assertFalse(emp.is_staff, "Employee must have is_staff=False by default.")
        self.assertFalse(emp.is_superuser, "Employee must have is_superuser=False.")
        self.assertTrue(emp.check_password('BebankBranchPass#2026'))

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'adminonly',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_ADMIN_EMAIL': 'adminonly@simulated.bebank.local',
    }, clear=True)
    def test_bootstrap_admin_only_when_employee_env_omitted(self):
        call_command('bootstrap_system_users')
        admin = User.objects.get(username='adminonly')
        self.assertEqual(admin.role, 'ADMIN')
        self.assertEqual(User.objects.count(), 1)

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'adminpartial',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'emppartial',
        # BOOTSTRAP_EMPLOYEE_PASSWORD missing!
    }, clear=True)
    def test_bootstrap_fails_if_employee_partially_supplied(self):
        with self.assertRaises(CommandError):
            call_command('bootstrap_system_users')
        self.assertFalse(User.objects.filter(username='adminpartial').exists())
        self.assertFalse(User.objects.filter(username='emppartial').exists())

    @patch.dict(os.environ, {}, clear=True)
    def test_bootstrap_fails_if_admin_env_missing(self):
        with self.assertRaises(CommandError):
            call_command('bootstrap_system_users')

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'weakadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': '123',  # Violates password validation
    }, clear=True)
    def test_bootstrap_validates_admin_password_strength(self):
        with self.assertRaises(ValidationError):
            call_command('bootstrap_system_users')
        self.assertFalse(User.objects.filter(username='weakadmin').exists())

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'existingadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
    }, clear=True)
    def test_bootstrap_rejects_existing_user_with_conflicting_role_or_privileges(self):
        # Create existing user with CUSTOMER role
        User.objects.create_user(username='existingadmin', password='pwd', role='CUSTOMER')
        with self.assertRaises(CommandError):
            call_command('bootstrap_system_users')
        # Ensure user was not silently elevated
        user = User.objects.get(username='existingadmin')
        self.assertEqual(user.role, 'CUSTOMER')
        self.assertFalse(user.is_staff)

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'idempotentadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'idempotentemp',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': 'EmployeeSecurePass2026!',
    }, clear=True)
    def test_bootstrap_is_idempotent_for_valid_existing_users(self):
        call_command('bootstrap_system_users')
        self.assertEqual(User.objects.count(), 2)

        # Second run should skip cleanly without error or modification
        call_command('bootstrap_system_users')
        self.assertEqual(User.objects.count(), 2)

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'atomicadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'atomicemp',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': '123',  # Weak employee password
    }, clear=True)
    def test_bootstrap_atomic_rollback_on_employee_validation_failure(self):
        with self.assertRaises(ValidationError):
            call_command('bootstrap_system_users')
        # Neither admin nor employee should exist due to atomic rollback / pre-validation
        self.assertFalse(User.objects.filter(username='atomicadmin').exists())
        self.assertFalse(User.objects.filter(username='atomicemp').exists())

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'rollbackadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'rollbackemp',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': 'BebankBranchPass#2026',
    }, clear=True)
    def test_bootstrap_database_rollback_on_employee_creation_integrity_error(self):
        original_create_user = User.objects.create_user
        out = StringIO()

        def fail_on_employee(*args, **kwargs):
            if kwargs.get('role') == 'EMPLOYEE':
                raise IntegrityError("Simulated database failure during employee creation")
            return original_create_user(*args, **kwargs)

        with patch.object(User.objects, 'create_user', side_effect=fail_on_employee):
            with self.assertRaises(IntegrityError):
                call_command('bootstrap_system_users', stdout=out)

        # Neither Admin nor Employee exists in DB after transaction rollback
        self.assertFalse(User.objects.filter(username='rollbackadmin').exists())
        self.assertFalse(User.objects.filter(username='rollbackemp').exists())
        # Success messages must be suppressed when transaction rolls back
        self.assertNotIn("Successfully created admin user", out.getvalue())

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'identicaluser',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'identicaluser',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': 'BebankBranchPass#2026',
    }, clear=True)
    def test_bootstrap_rejects_identical_admin_and_employee_usernames(self):
        with self.assertRaises(CommandError) as cm:
            call_command('bootstrap_system_users')
        self.assertIn("cannot be identical", str(cm.exception).lower())
        self.assertFalse(User.objects.filter(username='identicaluser').exists())

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'inactiveadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
    }, clear=True)
    def test_bootstrap_rejects_existing_inactive_admin_without_modification(self):
        User.objects.create_superuser(
            username='inactiveadmin',
            email='inactive@simulated.bebank.local',
            password='InitialPassword123!',
            role='ADMIN',
            is_active=False,
        )
        with self.assertRaises(CommandError) as cm:
            call_command('bootstrap_system_users')
        self.assertIn("inactive", str(cm.exception).lower())

        # Ensure user remains inactive and role is unchanged
        admin = User.objects.get(username='inactiveadmin')
        self.assertFalse(admin.is_active)
        self.assertEqual(admin.role, 'ADMIN')

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'activeadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecureAdminPassword2026!',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'inactiveemp',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': 'BebankBranchPass#2026',
    }, clear=True)
    def test_bootstrap_rejects_existing_inactive_employee_without_modification(self):
        User.objects.create_user(
            username='inactiveemp',
            email='inactiveemp@simulated.bebank.local',
            password='InitialPassword123!',
            role='EMPLOYEE',
            is_staff=False,
            is_superuser=False,
            is_active=False,
        )
        with self.assertRaises(CommandError) as cm:
            call_command('bootstrap_system_users')
        self.assertIn("inactive", str(cm.exception).lower())

        # Ensure employee remains inactive and admin was not created
        emp = User.objects.get(username='inactiveemp')
        self.assertFalse(emp.is_active)
        self.assertFalse(User.objects.filter(username='activeadmin').exists())
