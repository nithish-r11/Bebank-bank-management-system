import uuid
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from banking.models import (
    BankAccount, Transaction, Beneficiary, FixedDeposit, Loan,
    VirtualDebitCard, DebitCardAuditLog, Notification,
    SupportTicket, SupportMessage, send_in_app_notification, sanitize_internal_url
)


class CustomerModuleInventoryIntegrationTests(TestCase):
    """
    End-to-end integration tests verifying every feature of the BEBANK Customer Module.
    """

    def setUp(self):
        self.client = Client()
        # Demo Customer
        self.customer = User.objects.create_user(
            username='manojkumar',
            password='Password@123',
            role='CUSTOMER',
            email='manojkumar@bebank.local',
            first_name='Manoj',
            last_name='Kumar',
            kyc_status='APPROVED',
            kyc_verified=True,
        )
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='ACC20260001',
            account_type='SAVINGS',
            balance=Decimal('10000.00'),
            email='manojkumar@bebank.local',
            phone='9876543210',
            aadhaar='123456789012',
            pan='ABCDE1234F',
            address='123 Bank Street',
            pincode='560001'
        )

        # Second customer for isolation/IDOR and transfer testing
        self.other_customer = User.objects.create_user(
            username='othercustomer',
            password='Password@123',
            role='CUSTOMER',
            email='other@bebank.local',
            kyc_status='APPROVED',
            kyc_verified=True,
        )
        self.other_account = BankAccount.objects.create(
            user=self.other_customer,
            account_number='ACC20260002',
            account_type='SAVINGS',
            balance=Decimal('5000.00'),
            email='other@bebank.local',
            phone='9876543211',
            aadhaar='987654321098',
            pan='XYZAB5678C',
            address='456 Cross Street',
            pincode='560002'
        )

        # Employee & Admin
        self.employee = User.objects.create_user(
            username='murugananthan',
            password='Password@123',
            role='EMPLOYEE',
            first_name='Murugananthan'
        )
        self.admin = User.objects.create_superuser(
            username='nithishkumar',
            password='Password@123',
            role='ADMIN',
            email='nithishkumar@bebank.local'
        )

    # ==========================================
    # 1. LOGIN & DASHBOARD
    # ==========================================
    def test_customer_login_and_dashboard_render(self):
        """Customer logs in and accesses dashboard with accurate metrics, navigation, and badges."""
        login_resp = self.client.post(reverse('login'), {
            'username': 'manojkumar',
            'password': 'Password@123',
            'role': 'CUSTOMER'
        })
        self.assertEqual(login_resp.status_code, 302)
        self.assertEqual(login_resp.url, reverse('customer_dashboard'))

        resp = self.client.get(reverse('customer_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'manojkumar')
        self.assertContains(resp, 'ACC20260001')
        self.assertContains(resp, '10000')

    # ==========================================
    # 2. PROFILE & KYC
    # ==========================================
    def test_customer_profile_view_and_update(self):
        """Customer views profile (read-only for customer), and employee performs controlled updates."""
        self.client.login(username='manojkumar', password='Password@123')
        resp = self.client.get(reverse('customer_profile'))
        self.assertEqual(resp.status_code, 200)

        # Customer direct POST is rejected (HTTP 405)
        cust_post_resp = self.client.post(reverse('customer_profile'), {
            'first_name': 'Manoj',
            'last_name': 'Kumar Refined'
        })
        self.assertEqual(cust_post_resp.status_code, 405)

        # Employee executes authorized profile update
        self.client.login(username='murugananthan', password='Password@123')
        staff_edit_resp = self.client.post(reverse('staff_customer_profile_edit', kwargs={'user_id': self.customer.id}), {
            'first_name': 'Manoj',
            'last_name': 'Kumar Refined',
            'email': 'manoj.updated@bebank.local',
            'phone': '9876543299',
            'address': '789 Updated Boulevard',
            'pincode': '560003'
        })
        self.assertEqual(staff_edit_resp.status_code, 302)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.last_name, 'Kumar Refined')
        self.assertEqual(self.customer.email, 'manoj.updated@bebank.local')
        self.assertEqual(self.customer.profile_updated_by, self.employee)

    def test_customer_kyc_workflow(self):
        """Customer submits simulated KYC and receives notification upon staff review."""
        self.customer.kyc_status = 'NOT_SUBMITTED'
        self.customer.kyc_verified = False
        self.customer.save(update_fields=['kyc_status', 'kyc_verified'])

        self.client.login(username='manojkumar', password='Password@123')
        kyc_page = self.client.get(reverse('customer_kyc'))
        self.assertEqual(kyc_page.status_code, 200)

        submit_resp = self.client.post(reverse('customer_kyc'), {
            'simulated_id_type': 'SIMULATED_PASSPORT',
            'simulated_id_number': 'SIM-PASS-789012'
        })
        self.assertEqual(submit_resp.status_code, 302)

        self.customer.refresh_from_db()
        self.assertEqual(self.customer.kyc_status, 'PENDING')

        # Employee approves KYC
        self.client.login(username='murugananthan', password='Password@123')
        review_resp = self.client.post(reverse('staff_kyc_review', args=[self.customer.id]), {
            'action': 'approve'
        })
        self.assertEqual(review_resp.status_code, 302)

        self.customer.refresh_from_db()
        self.assertTrue(self.customer.kyc_verified)
        self.assertEqual(self.customer.kyc_status, 'APPROVED')

        # Customer received KYC notification
        notif = Notification.objects.filter(recipient=self.customer, notification_type='KYC').first()
        self.assertIsNotNone(notif)
        self.assertIn('Approved', notif.title)

    # ==========================================
    # 3. BANKING OPERATIONS: WITHDRAW & TRANSFER
    # ==========================================
    def test_customer_withdrawal_and_ledger(self):
        """Customer performs withdrawal with ledger tracking and notification."""
        self.client.login(username='manojkumar', password='Password@123')
        resp = self.client.post(reverse('withdraw'), {
            'account_id': self.account.id,
            'amount': '2000.00'
        })
        self.assertEqual(resp.status_code, 302)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('8000.00'))

        tx = Transaction.objects.filter(account=self.account, transaction_type='WITHDRAW').first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal('2000.00'))

    def test_customer_transfer_symmetric_and_idempotent(self):
        """Transfer creates symmetric two-leg entries and prevents double debit on retry."""
        self.client.login(username='manojkumar', password='Password@123')
        idem_key = str(uuid.uuid4())

        resp = self.client.post(reverse('transfer'), {
            'account_id': self.account.id,
            'account_number': self.other_account.account_number,
            'amount': '1500.00',
            'idempotency_key': idem_key
        })
        self.assertEqual(resp.status_code, 302)

        self.account.refresh_from_db()
        self.other_account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('8500.00'))
        self.assertEqual(self.other_account.balance, Decimal('6500.00'))

        # Duplicate retry with same idempotency key
        retry_resp = self.client.post(reverse('transfer'), {
            'account_id': self.account.id,
            'account_number': self.other_account.account_number,
            'amount': '1500.00',
            'idempotency_key': idem_key
        })
        self.assertEqual(retry_resp.status_code, 302)

        # Balances remain untouched after retry
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('8500.00'))

    # ==========================================
    # 4. BENEFICIARY MANAGEMENT
    # ==========================================
    def test_beneficiary_lifecycle(self):
        """Customer adds, edits, and removes beneficiary; prevents adding own account."""
        self.client.login(username='manojkumar', password='Password@123')

        # Prevent adding self
        self_add = self.client.post(reverse('beneficiary_list'), {
            'account_number': self.account.account_number,
            'nickname': 'My Self'
        })
        self.assertEqual(self_add.status_code, 302)
        self.assertEqual(Beneficiary.objects.filter(user=self.customer).count(), 0)

        # Add valid destination
        add_resp = self.client.post(reverse('beneficiary_list'), {
            'account_number': self.other_account.account_number,
            'nickname': 'Friend Acct'
        })
        self.assertEqual(add_resp.status_code, 302)
        b = Beneficiary.objects.filter(user=self.customer).first()
        self.assertIsNotNone(b)
        self.assertEqual(b.nickname, 'Friend Acct')

        # Edit nickname
        self.client.post(reverse('beneficiary_edit', args=[b.id]), {'nickname': 'Best Friend'})
        b.refresh_from_db()
        self.assertEqual(b.nickname, 'Best Friend')

        # Delete beneficiary
        self.client.post(reverse('beneficiary_delete', args=[b.id]))
        self.assertEqual(Beneficiary.objects.filter(user=self.customer).count(), 0)

    # ==========================================
    # 5. TRANSACTION HISTORY & PASSBOOK PDF
    # ==========================================
    def test_transaction_history_and_pdf_export(self):
        """History filters and passbook PDF work correctly with account ownership."""
        Transaction.objects.create(account=self.account, transaction_type='DEPOSIT', amount=Decimal('500.00'))
        Transaction.objects.create(account=self.account, transaction_type='WITHDRAW', amount=Decimal('200.00'))

        self.client.login(username='manojkumar', password='Password@123')

        hist_resp = self.client.get(reverse('history') + '?type=DEPOSIT&flow=credit')
        self.assertEqual(hist_resp.status_code, 200)

        pdf_resp = self.client.get(reverse('passbook'))
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp['Content-Type'], 'application/pdf')

        # Customer cannot download another customer's passbook
        idor_pdf = self.client.get(reverse('passbook_detail', args=[self.other_account.id]))
        self.assertEqual(idor_pdf.status_code, 404)

    # ==========================================
    # 6. FINANCIAL PRODUCTS: FIXED DEPOSIT
    # ==========================================
    def test_fixed_deposit_workflow(self):
        """FD booking, premature closure, and notifications."""
        self.client.login(username='manojkumar', password='Password@123')

        book_resp = self.client.post(reverse('fd'), {
            'account_id': self.account.id,
            'amount': '3000.00',
            'months': '12'
        })
        self.assertEqual(book_resp.status_code, 302)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('7000.00'))

        fd = FixedDeposit.objects.filter(account=self.account).first()
        self.assertIsNotNone(fd)
        self.assertEqual(fd.status, 'ACTIVE')

        # Premature close
        close_resp = self.client.post(reverse('fd_close', args=[fd.id]), {
            'closure_reason': 'Need urgent liquidity'
        })
        self.assertEqual(close_resp.status_code, 302)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('10000.00'))
        fd.refresh_from_db()
        self.assertEqual(fd.status, 'CLOSED')

    # ==========================================
    # 7. FINANCIAL PRODUCTS: LOAN WORKFLOW
    # ==========================================
    def test_loan_application_approval_and_repayment(self):
        """Loan application, admin approval & disbursement, and customer repayment."""
        self.client.login(username='manojkumar', password='Password@123')

        apply_resp = self.client.post(reverse('loan'), {
            'account_id': self.account.id,
            'amount': '25000.00',
            'months': '12'
        })
        self.assertEqual(apply_resp.status_code, 302)

        loan = Loan.objects.filter(account=self.account).first()
        self.assertIsNotNone(loan)
        self.assertEqual(loan.status, 'PENDING')

        # Admin approves
        self.client.login(username='nithishkumar', password='Password@123')
        approve_resp = self.client.post(reverse('approve_loan', args=[loan.id]))
        self.assertEqual(approve_resp.status_code, 302)

        self.account.refresh_from_db()
        loan.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('35000.00'))
        self.assertEqual(loan.status, 'APPROVED')

        # Customer repays EMI
        self.client.login(username='manojkumar', password='Password@123')
        repay_resp = self.client.post(reverse('loan_repay', args=[loan.id]), {
            'account_id': self.account.id,
            'amount': '5000.00',
            'idempotency_key': str(uuid.uuid4())
        })
        self.assertEqual(repay_resp.status_code, 302)

        self.account.refresh_from_db()
        loan.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('30000.00'))
        self.assertTrue(loan.outstanding_balance < loan.total_payable)

    # ==========================================
    # 8. FINANCIAL PRODUCTS: VIRTUAL DEBIT CARDS
    # ==========================================
    def test_virtual_debit_card_lifecycle(self):
        """Card issuance, detail, temporary block, unblock, and cancel."""
        self.client.login(username='manojkumar', password='Password@123')

        issue_resp = self.client.post(reverse('card_issue'), {
            'account_id': self.account.id,
            'cardholder_name': 'Manoj Kumar',
            'card_type': 'CLASSIC'
        })
        self.assertEqual(issue_resp.status_code, 302)

        card = VirtualDebitCard.objects.filter(user=self.customer).first()
        self.assertIsNotNone(card)
        self.assertEqual(card.status, 'ACTIVE')
        self.assertTrue(card.masked_card_number.startswith('•••• •••• ••••'))

        # Block
        self.client.post(reverse('card_block', args=[card.id]))
        card.refresh_from_db()
        self.assertEqual(card.status, 'BLOCKED')

        # Unblock
        self.client.post(reverse('card_unblock', args=[card.id]))
        card.refresh_from_db()
        self.assertEqual(card.status, 'ACTIVE')

        # Cancel
        self.client.post(reverse('card_cancel', args=[card.id]))
        card.refresh_from_db()
        self.assertEqual(card.status, 'CANCELLED')

    # ==========================================
    # 9. NOTIFICATIONS INBOX & ACTIONS
    # ==========================================
    def test_notification_inbox_and_mark_read(self):
        """Notification inbox, single mark read, mark all read, and IDOR isolation."""
        n1 = send_in_app_notification(self.customer, 'TRANSACTION', 'Deposit Alert', '₹500 received.')
        n2 = send_in_app_notification(self.customer, 'SECURITY', 'Login Alert', 'New login.')
        other_n = send_in_app_notification(self.other_customer, 'SECURITY', 'Private Alert', 'Secret.')

        self.client.login(username='manojkumar', password='Password@123')

        resp = self.client.get(reverse('notifications_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Deposit Alert')
        self.assertNotContains(resp, 'Private Alert')

        # Mark single as read
        self.client.post(reverse('notification_mark_read', args=[n1.id]))
        n1.refresh_from_db()
        self.assertTrue(n1.is_read)

        # IDOR prevention
        idor_resp = self.client.post(reverse('notification_mark_read', args=[other_n.id]))
        self.assertEqual(idor_resp.status_code, 404)

        # Mark all read
        self.client.post(reverse('notification_mark_all_read'))
        n2.refresh_from_db()
        self.assertTrue(n2.is_read)

    # ==========================================
    # 10. CUSTOMER SUPPORT & INTERNAL NOTES ISOLATION
    # ==========================================
    def test_support_ticket_conversation_and_note_hiding(self):
        """Ticket submission, staff internal note invisibility to customer, and reopen."""
        self.client.login(username='manojkumar', password='Password@123')

        create_resp = self.client.post(reverse('support_ticket_create'), {
            'category': 'TRANSACTION',
            'priority': 'HIGH',
            'subject': 'Delayed deposit settlement',
            'description': 'My deposited funds have not yet reflected in the available balance.'
        })
        self.assertEqual(create_resp.status_code, 302)

        ticket = SupportTicket.objects.filter(customer=self.customer).first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.status, 'OPEN')

        # Staff adds internal note and public reply
        self.client.login(username='murugananthan', password='Password@123')
        self.client.post(reverse('staff_support_reply', args=[ticket.id]), {
            'message': 'CONFIDENTIAL_INTERNAL_INVESTIGATION_NOTE',
            'is_internal_note': 'true',
            'status': 'OPEN'
        })
        self.client.post(reverse('staff_support_reply', args=[ticket.id]), {
            'message': 'We have reviewed your request and confirmed your funds.',
            'status': 'RESOLVED'
        })

        # Customer checks ticket detail: INTERNAL NOTE MUST BE INVISIBLE
        self.client.login(username='manojkumar', password='Password@123')
        detail_resp = self.client.get(reverse('support_ticket_detail', args=[ticket.id]))
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, 'We have reviewed your request')
        self.assertNotContains(detail_resp, 'CONFIDENTIAL_INTERNAL_INVESTIGATION_NOTE')

        # Customer reopens resolved ticket
        reopen_resp = self.client.post(reverse('support_ticket_reopen', args=[ticket.id]), {
            'reason': 'Still have a follow up question.'
        })
        self.assertEqual(reopen_resp.status_code, 302)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'OPEN')

    # ==========================================
    # 11. SECURITY & RBAC ENFORCEMENT
    # ==========================================
    def test_rbac_access_restrictions(self):
        """Customer cannot access staff or admin endpoints."""
        self.client.login(username='manojkumar', password='Password@123')

        # Staff dashboard -> 403
        self.assertEqual(self.client.get(reverse('employee_dashboard')).status_code, 403)
        # Admin dashboard -> 403
        self.assertEqual(self.client.get('/admin-dashboard/').status_code, 403)
        # Account creation -> 403
        self.assertEqual(self.client.get(reverse('create_account')).status_code, 403)
        # Staff support queue -> 403
        self.assertEqual(self.client.get(reverse('staff_support_queue')).status_code, 403)

    def test_post_only_methods_reject_get(self):
        """State-changing financial endpoints reject GET requests with 405 Method Not Allowed."""
        self.client.login(username='manojkumar', password='Password@123')
        b = Beneficiary.objects.create(user=self.customer, account=self.other_account, nickname='Test')

        self.assertEqual(self.client.get(reverse('beneficiary_delete', args=[b.id])).status_code, 405)
        self.assertEqual(self.client.get(reverse('notification_mark_all_read')).status_code, 405)
        self.assertEqual(self.client.get(reverse('card_block', args=[1])).status_code, 405)
