from django.test import TestCase, Client
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.db import IntegrityError, DatabaseError
from unittest.mock import patch
from decimal import Decimal
from accounts.models import User
from banking.models import BankAccount


class CreateAccountViewTests(TestCase):
    """
    Tests for Employee/Admin customer account creation:
    - Atomicity & transaction rollback (no orphan User records on BankAccount failure)
    - Opening balance validation (Decimal-based, reject negative, non-numeric, overflow, precision)
    - Zero opening balance acceptance
    - Authorization enforcement (EMPLOYEE/ADMIN only)
    """

    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp_creator', password='pwd', role='EMPLOYEE', is_staff=False
        )
        self.admin = User.objects.create_superuser(
            username='adm_creator', password='pwd', role='ADMIN', is_staff=True
        )
        self.customer = User.objects.create_user(
            username='cust_viewer', password='pwd', role='CUSTOMER', is_staff=False
        )
        self.url = reverse('create_account')
        self.valid_payload = {
            'username': 'newcustomer',
            'password': 'SecurePassword123!',
            'account_type': 'SAVINGS',
            'balance': '1000.00',
            'email': 'newcustomer@example.com',
            'phone': '9876543210',
            'aadhaar': '123456789012',
            'pan': 'ABCDE1234F',
            'address': '123 Test Street',
            'pincode': '560001',
        }

    # -------------------------------------------------------------
    # 1. Successful Account Creation & Zero Balance
    # -------------------------------------------------------------

    def test_valid_account_creation_creates_user_and_bank_account(self):
        """Employee can create customer user and bank account atomically with valid balance."""
        self.client.force_login(self.employee)
        response = self.client.post(self.url, data=self.valid_payload)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'account_details.html')

        user = User.objects.filter(username='newcustomer').first()
        self.assertIsNotNone(user)
        self.assertEqual(user.role, 'CUSTOMER')

        account = BankAccount.objects.filter(user=user).first()
        self.assertIsNotNone(account)
        self.assertEqual(account.balance, Decimal('1000.00'))
        self.assertEqual(account.account_type, 'SAVINGS')
        self.assertEqual(account.aadhaar, '123456789012')

    def test_zero_opening_balance_permitted(self):
        """Zero opening balance (₹0.00) is a valid initial balance for a savings account."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'zerobalancecust'
        payload['balance'] = '0'

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, 200)

        user = User.objects.filter(username='zerobalancecust').first()
        self.assertIsNotNone(user)
        account = BankAccount.objects.filter(user=user).first()
        self.assertIsNotNone(account)
        self.assertEqual(account.balance, Decimal('0.00'))

    # -------------------------------------------------------------
    # 2. Opening Balance Validation (Negative, Non-numeric, Precision, Overflow)
    # -------------------------------------------------------------

    def test_negative_opening_balance_rejected(self):
        """Negative opening balance must be rejected without creating User or BankAccount."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'negativecust'
        payload['balance'] = '-100.00'

        response = self.client.post(self.url, data=payload)
        self.assertRedirects(response, self.url)

        self.assertFalse(User.objects.filter(username='negativecust').exists())
        self.assertFalse(BankAccount.objects.filter(aadhaar='123456789012').exists())

    def test_non_numeric_opening_balance_rejected(self):
        """Non-numeric opening balance string must be rejected without creating User or BankAccount."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'nonnumcust'
        payload['balance'] = 'invalid_amount'

        response = self.client.post(self.url, data=payload)
        self.assertRedirects(response, self.url)

        self.assertFalse(User.objects.filter(username='nonnumcust').exists())
        self.assertFalse(BankAccount.objects.filter(aadhaar='123456789012').exists())

    def test_missing_or_empty_opening_balance_rejected(self):
        """Empty opening balance must be rejected without creating User or BankAccount."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'emptybalcust'
        payload['balance'] = ''

        response = self.client.post(self.url, data=payload)
        self.assertRedirects(response, self.url)

        self.assertFalse(User.objects.filter(username='emptybalcust').exists())
        self.assertFalse(BankAccount.objects.filter(aadhaar='123456789012').exists())

    def test_excessive_decimal_precision_rejected(self):
        """Balance with more than 2 decimal places must be rejected."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'precisioncust'
        payload['balance'] = '100.555'

        response = self.client.post(self.url, data=payload)
        self.assertRedirects(response, self.url)

        self.assertFalse(User.objects.filter(username='precisioncust').exists())
        self.assertFalse(BankAccount.objects.filter(aadhaar='123456789012').exists())

    def test_maximum_amount_overflow_rejected(self):
        """Balance exceeding maximum allowed 12-digit balance (9999999999.99) must be rejected."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'overflowcust'
        payload['balance'] = '10000000000.00'  # 11 integer digits + 2 decimals = 13 digits

        response = self.client.post(self.url, data=payload)
        self.assertRedirects(response, self.url)

        self.assertFalse(User.objects.filter(username='overflowcust').exists())
        self.assertFalse(BankAccount.objects.filter(aadhaar='123456789012').exists())

    def test_max_balance_derived_from_model_field_metadata(self):
        """Verify maximum balance boundary dynamically respects BankAccount DecimalField metadata."""
        field = BankAccount._meta.get_field('balance')
        max_int_digits = field.max_digits - field.decimal_places
        expected_max = Decimal('9' * max_int_digits + '.' + '9' * field.decimal_places)

        # Boundary value: exactly max allowed balance should succeed
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'maxbalcust'
        payload['balance'] = str(expected_max)

        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, 200)

        account = BankAccount.objects.filter(user__username='maxbalcust').first()
        self.assertIsNotNone(account)
        self.assertEqual(account.balance, expected_max)

        # Boundary + 0.01: exceeding max allowed balance should be rejected
        payload['username'] = 'exceedbalcust'
        payload['balance'] = str(expected_max + Decimal('0.01'))

        response_exceed = self.client.post(self.url, data=payload)
        self.assertRedirects(response_exceed, self.url)
        self.assertFalse(User.objects.filter(username='exceedbalcust').exists())

    # -------------------------------------------------------------
    # 3. Atomicity & Rollback (No Orphan User Records)
    # -------------------------------------------------------------

    def test_database_failure_during_bank_account_creation_rolls_back_user(self):
        """If BankAccount creation raises an error, User creation must be rolled back completely."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'rollbackcust'

        with patch.object(BankAccount.objects, 'create', side_effect=DatabaseError("Simulated DB error")):
            response = self.client.post(self.url, data=payload)

        # Must not crash with 500; must redirect gracefully back to form with error message
        self.assertRedirects(response, self.url)

        # Verify atomic rollback: no orphan user was committed
        self.assertFalse(
            User.objects.filter(username='rollbackcust').exists(),
            "Orphan User record was found! Transaction did not roll back user creation on BankAccount failure."
        )

    def test_validation_error_during_bank_account_creation_rolls_back_user(self):
        """If BankAccount creation raises ValidationError, User creation must be rolled back completely."""
        self.client.force_login(self.employee)
        payload = self.valid_payload.copy()
        payload['username'] = 'valerrorcust'

        with patch.object(BankAccount.objects, 'create', side_effect=ValidationError("Simulated validation error")):
            response = self.client.post(self.url, data=payload)

        # Must not crash with 500; must redirect gracefully back to form with error message
        self.assertRedirects(response, self.url)

        # Verify atomic rollback: no orphan user was committed
        self.assertFalse(
            User.objects.filter(username='valerrorcust').exists(),
            "Orphan User record was found! Transaction did not roll back user creation on ValidationError."
        )

    # -------------------------------------------------------------
    # 4. Authorization Enforcement
    # -------------------------------------------------------------

    def test_anonymous_user_redirected_to_login(self):
        """Unauthenticated requests are redirected to login."""
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response.url)

    def test_customer_user_forbidden(self):
        """CUSTOMER role is denied access (HTTP 403 Forbidden)."""
        self.client.force_login(self.customer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

        response_post = self.client.post(self.url, data=self.valid_payload)
        self.assertEqual(response_post.status_code, 403)
        self.assertFalse(User.objects.filter(username='newcustomer').exists())

    def test_admin_user_permitted(self):
        """ADMIN role can also create accounts."""
        self.client.force_login(self.admin)
        payload = self.valid_payload.copy()
        payload['username'] = 'admincreatedcust'
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(username='admincreatedcust').exists())
