from decimal import Decimal
from datetime import date
from django.test import TestCase, Client
from django.utils import timezone
from accounts.models import User
from banking.models import BankAccount, VirtualDebitCard, DebitCardAuditLog, generate_simulation_card_number


class VirtualDebitCardModelAndSecurityTest(TestCase):
    """
    Test suite for model invariants, simulation masking, and zero CVV/PIN exposure.
    """

    def setUp(self):
        self.customer = User.objects.create_user(username='card_alice', password='password123', role='CUSTOMER')
        self.account = BankAccount.objects.create(user=self.customer, account_type='SAVINGS', balance=Decimal('5000.00'))

    def test_simulation_card_number_generator(self):
        """Simulation card numbers must be 16 digits starting with 5045."""
        num = generate_simulation_card_number()
        self.assertEqual(len(num), 16)
        self.assertTrue(num.startswith('5045'))
        self.assertTrue(num.isdigit())

    def test_masked_card_number_property(self):
        """Card number must strictly mask first 12 digits."""
        card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=self.account,
            card_number='5045123456789876',
            cardholder_name='ALICE SMITH',
            status='ACTIVE'
        )
        self.assertEqual(card.masked_card_number, '•••• •••• •••• 9876')
        self.assertEqual(card.expiry_display, f"{card.expiry_month:02d}/{str(card.expiry_year)[-2:]}")

    def test_is_simulation_flag_and_disclaimer(self):
        """Model must always be identified as a simulation."""
        card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=self.account,
            card_number='5045000011112222',
            cardholder_name='ALICE SMITH',
            status='ACTIVE'
        )
        self.assertTrue(card.is_simulation)

    def test_expiry_logic(self):
        """Expired card detection accurately compares year and month."""
        now = timezone.now().date()
        # Active future card
        active_card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=self.account,
            card_number='5045111122223333',
            cardholder_name='ALICE SMITH',
            expiry_month=12,
            expiry_year=now.year + 2,
            status='ACTIVE'
        )
        self.assertFalse(active_card.is_expired)
        self.assertTrue(active_card.is_usable)

        # Past expired card
        expired_card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=BankAccount.objects.create(user=self.customer, account_type='CURRENT', balance=Decimal('100.00')),
            card_number='5045999988887777',
            cardholder_name='ALICE SMITH',
            expiry_month=1,
            expiry_year=now.year - 1,
            status='ACTIVE'
        )
        self.assertTrue(expired_card.is_expired)
        self.assertFalse(expired_card.is_usable)


class CustomerDebitCardWorkflowTest(TestCase):
    """
    Test suite for customer issuance, blocking, unblocking, cancellation, and IDOR protection.
    """

    def setUp(self):
        self.client = Client()
        self.customer1 = User.objects.create_user(username='card_cust1', password='password123', role='CUSTOMER')
        self.customer2 = User.objects.create_user(username='card_cust2', password='password123', role='CUSTOMER')

        self.acc1 = BankAccount.objects.create(user=self.customer1, account_type='SAVINGS', balance=Decimal('10000.00'))
        self.acc2 = BankAccount.objects.create(user=self.customer2, account_type='SAVINGS', balance=Decimal('5000.00'))

    def test_customer_can_view_card_list_page(self):
        """Customer can access debit cards workspace."""
        self.client.force_login(self.customer1)
        response = self.client.get('/bank/cards/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Virtual Debit Cards')
        self.assertContains(response, 'Request New Virtual Debit Card')

    def test_successful_card_issuance(self):
        """Eligible account can issue a virtual debit card."""
        self.client.force_login(self.customer1)
        response = self.client.post('/bank/cards/issue/', {
            'account_id': self.acc1.id,
            'cardholder_name': 'JOHN DOE',
            'card_type': 'PLATINUM'
        })
        self.assertEqual(response.status_code, 302)

        card = VirtualDebitCard.objects.filter(account=self.acc1).first()
        self.assertIsNotNone(card)
        self.assertEqual(card.status, 'ACTIVE')
        self.assertEqual(card.cardholder_name, 'JOHN DOE')
        self.assertEqual(card.card_type, 'PLATINUM')
        self.assertEqual(card.daily_limit, Decimal('50000.00'))
        self.assertEqual(card.user, self.customer1)

        # Audit log created
        log = DebitCardAuditLog.objects.filter(card=card, action='ISSUED').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.performed_by, self.customer1)

    def test_duplicate_active_card_issuance_prevented(self):
        """Account with active card cannot receive a second active card."""
        VirtualDebitCard.objects.create(
            user=self.customer1,
            account=self.acc1,
            card_number=generate_simulation_card_number(),
            cardholder_name='JOHN DOE',
            status='ACTIVE'
        )

        self.client.force_login(self.customer1)
        response = self.client.post('/bank/cards/issue/', {
            'account_id': self.acc1.id,
            'cardholder_name': 'JOHN DOE'
        })
        self.assertEqual(response.status_code, 302)
        # Still only 1 card for acc1
        self.assertEqual(VirtualDebitCard.objects.filter(account=self.acc1).count(), 1)

    def test_can_issue_new_card_after_previous_card_cancelled(self):
        """If previous card is cancelled, a new card can be issued for that account."""
        VirtualDebitCard.objects.create(
            user=self.customer1,
            account=self.acc1,
            card_number=generate_simulation_card_number(),
            cardholder_name='JOHN DOE',
            status='CANCELLED'
        )

        self.client.force_login(self.customer1)
        response = self.client.post('/bank/cards/issue/', {
            'account_id': self.acc1.id,
            'cardholder_name': 'JOHN DOE'
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(VirtualDebitCard.objects.filter(account=self.acc1, status='ACTIVE').count(), 1)

    def test_idor_cannot_issue_card_on_another_customers_account(self):
        """Customer cannot request a card using another customer's bank account ID (HTTP 404)."""
        self.client.force_login(self.customer1)
        response = self.client.post('/bank/cards/issue/', {
            'account_id': self.acc2.id,  # Owned by customer2
            'cardholder_name': 'ATTACKER'
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(VirtualDebitCard.objects.filter(account=self.acc2).exists())

    def test_customer_card_blocking_and_unblocking(self):
        """Customer can block an active card and unblock a self-blocked card."""
        card = VirtualDebitCard.objects.create(
            user=self.customer1,
            account=self.acc1,
            card_number=generate_simulation_card_number(),
            cardholder_name='JOHN DOE',
            status='ACTIVE'
        )

        self.client.force_login(self.customer1)

        # Block
        res_block = self.client.post(f'/bank/cards/{card.id}/block/')
        self.assertEqual(res_block.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'BLOCKED')
        self.assertTrue(DebitCardAuditLog.objects.filter(card=card, action='BLOCKED_CUSTOMER').exists())

        # Unblock
        res_unblock = self.client.post(f'/bank/cards/{card.id}/unblock/')
        self.assertEqual(res_unblock.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'ACTIVE')
        self.assertTrue(DebitCardAuditLog.objects.filter(card=card, action='UNBLOCKED_CUSTOMER').exists())

    def test_customer_card_cancellation(self):
        """Customer can permanently cancel a card, preventing further activation."""
        card = VirtualDebitCard.objects.create(
            user=self.customer1,
            account=self.acc1,
            card_number=generate_simulation_card_number(),
            cardholder_name='JOHN DOE',
            status='ACTIVE'
        )

        self.client.force_login(self.customer1)
        response = self.client.post(f'/bank/cards/{card.id}/cancel/')
        self.assertEqual(response.status_code, 302)

        card.refresh_from_db()
        self.assertEqual(card.status, 'CANCELLED')
        self.assertTrue(DebitCardAuditLog.objects.filter(card=card, action='CANCELLED_CUSTOMER').exists())

        # Attempt to unblock cancelled card must fail
        res_unblock = self.client.post(f'/bank/cards/{card.id}/unblock/')
        self.assertEqual(res_unblock.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'CANCELLED')

    def test_customer_cannot_unblock_admin_suspended_card(self):
        """Customer cannot unblock a card that was suspended by administration."""
        card = VirtualDebitCard.objects.create(
            user=self.customer1,
            account=self.acc1,
            card_number=generate_simulation_card_number(),
            cardholder_name='JOHN DOE',
            status='BLOCKED',
            blocked_by_admin=True
        )

        self.client.force_login(self.customer1)
        response = self.client.post(f'/bank/cards/{card.id}/unblock/')
        self.assertEqual(response.status_code, 302)
        card.refresh_from_db()
        self.assertEqual(card.status, 'BLOCKED')  # Remains blocked

    def test_idor_customer_cannot_manage_another_customers_card(self):
        """Customer cannot view, block, unblock, or cancel another customer's card (HTTP 404)."""
        card2 = VirtualDebitCard.objects.create(
            user=self.customer2,
            account=self.acc2,
            card_number=generate_simulation_card_number(),
            cardholder_name='BOB ROE',
            status='ACTIVE'
        )

        self.client.force_login(self.customer1)

        # View detail
        res_view = self.client.get(f'/bank/cards/{card2.id}/')
        self.assertEqual(res_view.status_code, 404)

        # Block
        res_block = self.client.post(f'/bank/cards/{card2.id}/block/')
        self.assertEqual(res_block.status_code, 404)

        # Cancel
        res_cancel = self.client.post(f'/bank/cards/{card2.id}/cancel/')
        self.assertEqual(res_cancel.status_code, 404)

        card2.refresh_from_db()
        self.assertEqual(card2.status, 'ACTIVE')

    def test_post_only_method_enforcement(self):
        """State changing card endpoints must reject GET requests with HTTP 405 Method Not Allowed."""
        card = VirtualDebitCard.objects.create(
            user=self.customer1,
            account=self.acc1,
            card_number=generate_simulation_card_number(),
            cardholder_name='JOHN DOE',
            status='ACTIVE'
        )

        self.client.force_login(self.customer1)
        self.assertEqual(self.client.get('/bank/cards/issue/').status_code, 405)
        self.assertEqual(self.client.get(f'/bank/cards/{card.id}/block/').status_code, 405)
        self.assertEqual(self.client.get(f'/bank/cards/{card.id}/unblock/').status_code, 405)
        self.assertEqual(self.client.get(f'/bank/cards/{card.id}/cancel/').status_code, 405)


class StaffAndAdminDebitCardIntegrationTest(TestCase):
    """
    Test suite for Employee and Admin card inspection, action audit logs, and administrative overrides.
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(username='card_holder', password='password123', role='CUSTOMER')
        self.employee = User.objects.create_user(username='staff_emp', password='password123', role='EMPLOYEE')
        self.admin = User.objects.create_superuser(username='staff_admin', password='password123', role='ADMIN')

        self.account = BankAccount.objects.create(user=self.customer, account_type='SAVINGS', balance=Decimal('5000.00'))
        self.card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=self.account,
            card_number='5045123499998888',
            cardholder_name='CARD HOLDER',
            status='ACTIVE'
        )

    def test_staff_can_view_card_details_and_audit_log_recorded(self):
        """Employees and Admins can inspect card record; creates STATUS_INSPECTED_STAFF audit entry."""
        self.client.force_login(self.employee)
        response = self.client.get(f'/bank/staff/cards/{self.card.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Compliance & Audit View')
        self.assertContains(response, self.card.masked_card_number)

        # Audit entry exists
        self.assertTrue(DebitCardAuditLog.objects.filter(
            card=self.card,
            action='STATUS_INSPECTED_STAFF',
            performed_by=self.employee
        ).exists())

    def test_customer_cannot_access_staff_inspection(self):
        """Customers are rejected from staff card inspection (HTTP 403 Forbidden)."""
        self.client.force_login(self.customer)
        response = self.client.get(f'/bank/staff/cards/{self.card.id}/')
        self.assertEqual(response.status_code, 403)

    def test_admin_can_block_and_unblock_card(self):
        """Admin can administratively suspend and unblock card with audit trail."""
        self.client.force_login(self.admin)

        # Admin Block
        res_block = self.client.post(f'/bank/staff/cards/{self.card.id}/admin-action/', {
            'action_type': 'block',
            'reason': 'Fraud alert investigation'
        })
        self.assertEqual(res_block.status_code, 302)
        self.card.refresh_from_db()
        self.assertEqual(self.card.status, 'BLOCKED')
        self.assertTrue(self.card.blocked_by_admin)
        self.assertTrue(DebitCardAuditLog.objects.filter(card=self.card, action='BLOCKED_ADMIN').exists())

        # Admin Unblock
        res_unblock = self.client.post(f'/bank/staff/cards/{self.card.id}/admin-action/', {
            'action_type': 'unblock',
            'reason': 'Identity verified by risk officer'
        })
        self.assertEqual(res_unblock.status_code, 302)
        self.card.refresh_from_db()
        self.assertEqual(self.card.status, 'ACTIVE')
        self.assertFalse(self.card.blocked_by_admin)
        self.assertTrue(DebitCardAuditLog.objects.filter(card=self.card, action='UNBLOCKED_ADMIN').exists())

    def test_employee_cannot_execute_admin_card_actions(self):
        """Employee cannot trigger administrative overrides (HTTP 403 Forbidden)."""
        self.client.force_login(self.employee)
        response = self.client.post(f'/bank/staff/cards/{self.card.id}/admin-action/', {
            'action_type': 'block',
            'reason': 'Unauthorized employee attempt'
        })
        self.assertEqual(response.status_code, 403)
        self.card.refresh_from_db()
        self.assertEqual(self.card.status, 'ACTIVE')  # Card remains active

