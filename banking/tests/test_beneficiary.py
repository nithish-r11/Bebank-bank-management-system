from decimal import Decimal
import uuid
from django.test import TestCase, Client
from django.urls import reverse
from django.db import IntegrityError
from accounts.models import User
from banking.models import BankAccount, Transaction, Beneficiary


class BeneficiaryManagementTests(TestCase):
    """
    Unit & integration tests for BEBANK Fast-Track Task 2.1:
    - Beneficiary Model & Helper Methods (masking, availability, account holder name)
    - CRUD Operations (Add, Edit, Delete)
    - Constraint Enforcement (Self-beneficiary rejection, duplicate rejection)
    - Safe Error Handling & Inactive / Locked Account Rejection
    - Object-Level Isolation & IDOR Prevention
    - Staff RBAC Protection (Employee & Admin 403 Forbidden)
    - Transfer Integration (Beneficiary transfer, tampering rejection, inactive rejection, manual fallback, idempotency)
    - Financial Integrity & Deletion Isolation (Deleting beneficiary does NOT touch transactions or accounts)
    """

    def setUp(self):
        # Customer 1 (Sender / Owner)
        self.cust1 = User.objects.create_user(
            username='cust1',
            password='Password123!',
            role='CUSTOMER',
            first_name='Alice',
            last_name='Smith'
        )
        self.acc1 = BankAccount.objects.create(
            user=self.cust1,
            account_number='10001',
            account_type='SAVINGS',
            balance=Decimal('5000.00')
        )

        # Customer 2 (Destination / Beneficiary candidate)
        self.cust2 = User.objects.create_user(
            username='cust2',
            password='Password123!',
            role='CUSTOMER',
            first_name='Bob',
            last_name='Jones'
        )
        self.acc2 = BankAccount.objects.create(
            user=self.cust2,
            account_number='10002',
            account_type='SAVINGS',
            balance=Decimal('1000.00')
        )

        # Customer 3 (Third party for IDOR tests)
        self.cust3 = User.objects.create_user(
            username='cust3',
            password='Password123!',
            role='CUSTOMER'
        )
        self.acc3 = BankAccount.objects.create(
            user=self.cust3,
            account_number='10003',
            account_type='CURRENT',
            balance=Decimal('2000.00')
        )

        # Employee & Admin users
        self.employee = User.objects.create_user(
            username='emp_user',
            password='Password123!',
            role='EMPLOYEE'
        )
        self.admin = User.objects.create_user(
            username='adm_user',
            password='Password123!',
            role='ADMIN',
            is_staff=True,
            is_superuser=True
        )

        self.client1 = Client()
        self.client1.login(username='cust1', password='Password123!')

        self.client2 = Client()
        self.client2.login(username='cust2', password='Password123!')

    # =========================================================================
    # 1. MODEL & HELPER TESTS
    # =========================================================================

    def test_beneficiary_helpers_and_masking(self):
        """Test masked_account_number, account_holder_name, and is_available helpers."""
        b = Beneficiary.objects.create(
            user=self.cust1,
            account=self.acc2,
            nickname='Bob Personal'
        )

        # Masking: '10002' -> '•••• 0002'
        self.assertEqual(b.masked_account_number(), '•••• 0002')
        self.assertEqual(b.account_holder_name, 'Bob Jones')
        self.assertTrue(b.is_available())
        self.assertIn('Bob Personal', str(b))
        self.assertIn('•••• 0002', str(b))

        # Test short account number (< 4 digits)
        short_acc = BankAccount(account_number='789')
        b_short = Beneficiary(account=short_acc)
        self.assertEqual(b_short.masked_account_number(), '789')

        # Test availability when target user is locked
        self.cust2.account_locked = True
        self.cust2.save()
        self.assertFalse(b.is_available())

        # Test availability when target user is inactive
        self.cust2.account_locked = False
        self.cust2.is_active = False
        self.cust2.save()
        self.assertFalse(b.is_available())

    # =========================================================================
    # 2. ADD BENEFICIARY TESTS
    # =========================================================================

    def test_add_beneficiary_success(self):
        """Customer can successfully add another customer's active account."""
        resp = self.client1.post(reverse('beneficiary_list'), {
            'account_number': '10002',
            'nickname': 'Bob Checking'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Beneficiary for account 10002 added successfully")
        self.assertTrue(Beneficiary.objects.filter(user=self.cust1, account=self.acc2).exists())
        b = Beneficiary.objects.get(user=self.cust1, account=self.acc2)
        self.assertEqual(b.nickname, 'Bob Checking')

    def test_prevent_adding_own_account(self):
        """Customer cannot add their own account as a beneficiary."""
        resp = self.client1.post(reverse('beneficiary_list'), {
            'account_number': '10001',
            'nickname': 'My Own Account'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "You cannot add your own account as a beneficiary")
        self.assertFalse(Beneficiary.objects.filter(user=self.cust1, account=self.acc1).exists())

    def test_prevent_nonexistent_account(self):
        """Non-existent account yields safe error and does not create beneficiary."""
        resp = self.client1.post(reverse('beneficiary_list'), {
            'account_number': '9999999999',
            'nickname': 'Ghost Account'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Destination account not found or unavailable")
        self.assertEqual(Beneficiary.objects.filter(user=self.cust1).count(), 0)

    def test_prevent_inactive_or_locked_account(self):
        """Locked or inactive recipient accounts cannot be added."""
        self.cust2.account_locked = True
        self.cust2.save()

        resp = self.client1.post(reverse('beneficiary_list'), {
            'account_number': '10002',
            'nickname': 'Locked Bob'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Destination account not found or unavailable")
        self.assertFalse(Beneficiary.objects.filter(user=self.cust1, account=self.acc2).exists())

    def test_prevent_duplicate_beneficiary(self):
        """Adding the same beneficiary account twice is rejected."""
        Beneficiary.objects.create(user=self.cust1, account=self.acc2, nickname='First')

        resp = self.client1.post(reverse('beneficiary_list'), {
            'account_number': '10002',
            'nickname': 'Duplicate'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This account is already in your beneficiary list")
        self.assertEqual(Beneficiary.objects.filter(user=self.cust1, account=self.acc2).count(), 1)

    def test_database_unique_constraint(self):
        """Database constraint enforces uniqueness at the database level."""
        Beneficiary.objects.create(user=self.cust1, account=self.acc2)
        with self.assertRaises(IntegrityError):
            Beneficiary.objects.create(user=self.cust1, account=self.acc2)

    # =========================================================================
    # 3. EDIT & DELETE BENEFICIARY TESTS
    # =========================================================================

    def test_edit_beneficiary_nickname_success(self):
        """Owner can edit the nickname of their beneficiary."""
        b = Beneficiary.objects.create(user=self.cust1, account=self.acc2, nickname='Old Nick')
        resp = self.client1.post(reverse('beneficiary_edit', args=[b.id]), {
            'nickname': 'New Nick'
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Beneficiary nickname updated successfully")
        b.refresh_from_db()
        self.assertEqual(b.nickname, 'New Nick')

    def test_delete_beneficiary_success_and_preserves_financial_history(self):
        """Deleting a beneficiary removes only the beneficiary record, preserving accounts and transactions."""
        b = Beneficiary.objects.create(user=self.cust1, account=self.acc2, nickname='Bob')

        # Simulate existing transactions between acc1 and acc2
        tx_debit = Transaction.objects.create(
            account=self.acc1,
            related_account=self.acc2,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('100.00'),
            balance_before=Decimal('5000.00'),
            balance_after=Decimal('4900.00'),
            reference_id='TRF-TEST-REF-1'
        )
        tx_credit = Transaction.objects.create(
            account=self.acc2,
            related_account=self.acc1,
            transaction_type='TRANSFER_CREDIT',
            amount=Decimal('100.00'),
            balance_before=Decimal('1000.00'),
            balance_after=Decimal('1100.00'),
            reference_id='TRF-TEST-REF-1'
        )

        resp = self.client1.post(reverse('beneficiary_delete', args=[b.id]), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Beneficiary removed successfully")
        self.assertFalse(Beneficiary.objects.filter(id=b.id).exists())

        # Verify BankAccount records are completely unaffected
        self.assertTrue(BankAccount.objects.filter(id=self.acc1.id).exists())
        self.assertTrue(BankAccount.objects.filter(id=self.acc2.id).exists())

        # Verify Transaction history remains 100% intact
        self.assertTrue(Transaction.objects.filter(id=tx_debit.id).exists())
        self.assertTrue(Transaction.objects.filter(id=tx_credit.id).exists())

    def test_require_post_for_state_changing_operations(self):
        """GET requests to edit and delete endpoints must be rejected with 405 Method Not Allowed."""
        b = Beneficiary.objects.create(user=self.cust1, account=self.acc2)

        resp_edit_get = self.client1.get(reverse('beneficiary_edit', args=[b.id]))
        self.assertEqual(resp_edit_get.status_code, 405)

        resp_del_get = self.client1.get(reverse('beneficiary_delete', args=[b.id]))
        self.assertEqual(resp_del_get.status_code, 405)

    # =========================================================================
    # 4. OBJECT-LEVEL IDOR & RBAC TESTS
    # =========================================================================

    def test_cross_customer_idor_prevention_on_edit_and_delete(self):
        """Customer B cannot edit or delete Customer A's beneficiary (HTTP 404)."""
        b1 = Beneficiary.objects.create(user=self.cust1, account=self.acc2, nickname='Alice List')

        # Customer 2 attempts to edit Customer 1's beneficiary
        resp_edit = self.client2.post(reverse('beneficiary_edit', args=[b1.id]), {'nickname': 'Hacked'})
        self.assertEqual(resp_edit.status_code, 404)
        b1.refresh_from_db()
        self.assertEqual(b1.nickname, 'Alice List')

        # Customer 2 attempts to delete Customer 1's beneficiary
        resp_del = self.client2.post(reverse('beneficiary_delete', args=[b1.id]))
        self.assertEqual(resp_del.status_code, 404)
        self.assertTrue(Beneficiary.objects.filter(id=b1.id).exists())

    def test_staff_rbac_rejection(self):
        """Staff (Employee and Admin) must receive 403 Forbidden on customer beneficiary endpoints."""
        emp_client = Client()
        emp_client.login(username='emp_user', password='Password123!')

        adm_client = Client()
        adm_client.login(username='adm_user', password='Password123!')

        b1 = Beneficiary.objects.create(user=self.cust1, account=self.acc2)

        # Employee check
        self.assertEqual(emp_client.get(reverse('beneficiary_list')).status_code, 403)
        self.assertEqual(emp_client.post(reverse('beneficiary_edit', args=[b1.id]), {'nickname': 'X'}).status_code, 403)
        self.assertEqual(emp_client.post(reverse('beneficiary_delete', args=[b1.id])).status_code, 403)

        # Admin check
        self.assertEqual(adm_client.get(reverse('beneficiary_list')).status_code, 403)
        self.assertEqual(adm_client.post(reverse('beneficiary_edit', args=[b1.id]), {'nickname': 'X'}).status_code, 403)
        self.assertEqual(adm_client.post(reverse('beneficiary_delete', args=[b1.id])).status_code, 403)

    # =========================================================================
    # 5. TRANSFER INTEGRATION TESTS
    # =========================================================================

    def test_transfer_using_beneficiary_creates_two_leg_ledger(self):
        """Transfer via beneficiary executes full balanced two-leg ledger with UUID reference."""
        b = Beneficiary.objects.create(user=self.cust1, account=self.acc2, nickname='Bob')

        key = str(uuid.uuid4())
        resp = self.client1.post(reverse('transfer'), {
            'beneficiary_id': b.id,
            'amount': '500.00',
            'idempotency_key': key
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Transfer successful")

        self.acc1.refresh_from_db()
        self.acc2.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('4500.00'))
        self.assertEqual(self.acc2.balance, Decimal('1500.00'))

        expected_ref = f"TRF-{uuid.UUID(key).hex.upper()}"
        txs = Transaction.objects.filter(reference_id=expected_ref)
        self.assertEqual(txs.count(), 2)

        debit = txs.get(transaction_type='TRANSFER_DEBIT')
        self.assertEqual(debit.account, self.acc1)
        self.assertEqual(debit.related_account, self.acc2)
        self.assertEqual(debit.amount, Decimal('500.00'))
        self.assertEqual(debit.balance_before, Decimal('5000.00'))
        self.assertEqual(debit.balance_after, Decimal('4500.00'))

        credit = txs.get(transaction_type='TRANSFER_CREDIT')
        self.assertEqual(credit.account, self.acc2)
        self.assertEqual(credit.related_account, self.acc1)
        self.assertEqual(credit.amount, Decimal('500.00'))
        self.assertEqual(credit.balance_before, Decimal('1000.00'))
        self.assertEqual(credit.balance_after, Decimal('1500.00'))

    def test_transfer_with_tampered_beneficiary_id_rejected(self):
        """Submitting a beneficiary_id belonging to another customer is rejected (IDOR guard)."""
        # Beneficiary created by Customer 2
        b_other = Beneficiary.objects.create(user=self.cust2, account=self.acc3)

        key = str(uuid.uuid4())
        resp = self.client1.post(reverse('transfer'), {
            'beneficiary_id': b_other.id,
            'amount': '300.00',
            'idempotency_key': key
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Selected beneficiary not found or unauthorized")

        # Zero balance change
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('5000.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_transfer_to_inactive_beneficiary_rejected(self):
        """Transfer to a beneficiary whose destination account was locked/deactivated is rejected."""
        b = Beneficiary.objects.create(user=self.cust1, account=self.acc2)
        self.cust2.account_locked = True
        self.cust2.save()

        key = str(uuid.uuid4())
        resp = self.client1.post(reverse('transfer'), {
            'beneficiary_id': b.id,
            'amount': '200.00',
            'idempotency_key': key
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Selected beneficiary is currently unavailable or inactive")

        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('5000.00'))
        self.assertEqual(Transaction.objects.count(), 0)

    def test_manual_transfer_path_still_functional(self):
        """Standard manual account number transfer continues to function seamlessly without beneficiary."""
        key = str(uuid.uuid4())
        resp = self.client1.post(reverse('transfer'), {
            'account_number': '10002',
            'amount': '250.00',
            'idempotency_key': key
        }, follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Transfer successful")

        self.acc1.refresh_from_db()
        self.acc2.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('4750.00'))
        self.assertEqual(self.acc2.balance, Decimal('1250.00'))

    def test_beneficiary_transfer_idempotency_handling(self):
        """Beneficiary transfer preserves idempotency key retry handling and prevents double debits."""
        b = Beneficiary.objects.create(user=self.cust1, account=self.acc2)
        key = str(uuid.uuid4())

        # First submission
        resp1 = self.client1.post(reverse('transfer'), {
            'beneficiary_id': b.id,
            'amount': '400.00',
            'idempotency_key': key
        }, follow=True)
        self.assertContains(resp1, "Transfer successful")
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('4600.00'))

        # Identical duplicate submission
        resp2 = self.client1.post(reverse('transfer'), {
            'beneficiary_id': b.id,
            'amount': '400.00',
            'idempotency_key': key
        }, follow=True)
        self.assertContains(resp2, "Transfer already processed successfully")

        # Balance remains unchanged
        self.acc1.refresh_from_db()
        self.assertEqual(self.acc1.balance, Decimal('4600.00'))
        self.assertEqual(Transaction.objects.filter(amount=Decimal('400.00')).count(), 2)  # 1 debit + 1 credit

