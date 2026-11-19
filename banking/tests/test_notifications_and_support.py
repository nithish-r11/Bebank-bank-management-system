import uuid
from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from accounts.models import User
from banking.models import (
    BankAccount, Transaction, FixedDeposit, Loan,
    VirtualDebitCard, Notification, SupportTicket, SupportMessage,
    send_in_app_notification, sanitize_internal_url, generate_ticket_number
)


class NotificationSystemTests(TestCase):
    """Unit and integration tests for Persistent In-App Notifications."""

    def setUp(self):
        self.client = Client()
        self.customer1 = User.objects.create_user(
            username='cust1', password='password123', role='CUSTOMER'
        )
        self.customer2 = User.objects.create_user(
            username='cust2', password='password123', role='CUSTOMER'
        )
        self.account1 = BankAccount.objects.create(
            user=self.customer1,
            account_number='ACC10001',
            account_type='SAVINGS',
            balance=Decimal('5000.00'),
            email='cust1@example.com',
            phone='9876543210',
            aadhaar='123456789012',
            pan='ABCDE1234F',
            address='123 Main St',
            pincode='600001'
        )

    def test_sanitize_internal_url(self):
        """Sanitizer only accepts valid relative internal paths starting with /."""
        self.assertEqual(sanitize_internal_url('/bank/history/'), '/bank/history/')
        self.assertEqual(sanitize_internal_url('/bank/support/1/'), '/bank/support/1/')
        self.assertEqual(sanitize_internal_url('http://evil.com'), '')
        self.assertEqual(sanitize_internal_url('https://evil.com'), '')
        self.assertEqual(sanitize_internal_url('//evil.com'), '')
        self.assertEqual(sanitize_internal_url('javascript:alert(1)'), '')
        self.assertEqual(sanitize_internal_url('/https://evil.com'), '')
        self.assertEqual(sanitize_internal_url(''), '')
        self.assertEqual(sanitize_internal_url(None), '')

    def test_send_in_app_notification_creation(self):
        """Helper correctly creates and stores durable notification."""
        notif = send_in_app_notification(
            recipient=self.customer1,
            notification_type='TRANSACTION',
            title='Deposit Confirmed',
            message='₹1000 has been deposited.',
            target_url='/bank/history/'
        )
        self.assertIsNotNone(notif)
        self.assertEqual(notif.recipient, self.customer1)
        self.assertEqual(notif.notification_type, 'TRANSACTION')
        self.assertEqual(notif.title, 'Deposit Confirmed')
        self.assertFalse(notif.is_read)
        self.assertEqual(notif.target_url, '/bank/history/')

    def test_notification_dedup_key_prevents_duplicate(self):
        """Duplicate notifications with identical dedup_key are gracefully prevented."""
        dedup_key = 'TEST-KEY-12345'
        notif1 = send_in_app_notification(
            recipient=self.customer1,
            notification_type='TRANSACTION',
            title='Transfer Sent',
            message='First attempt',
            dedup_key=dedup_key
        )
        notif2 = send_in_app_notification(
            recipient=self.customer1,
            notification_type='TRANSACTION',
            title='Transfer Sent Retry',
            message='Second attempt',
            dedup_key=dedup_key
        )
        self.assertEqual(notif1.id, notif2.id)
        self.assertEqual(Notification.objects.filter(recipient=self.customer1).count(), 1)

    def test_customer_can_view_own_notifications(self):
        """Customer sees their own notifications on the inbox page."""
        send_in_app_notification(self.customer1, 'TRANSACTION', 'Alert 1', 'Message 1')
        send_in_app_notification(self.customer2, 'SECURITY', 'Alert 2', 'Message 2')

        self.client.login(username='cust1', password='password123')
        response = self.client.get(reverse('notifications_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alert 1')
        self.assertNotContains(response, 'Alert 2')

    def test_mark_single_notification_read(self):
        """Customer can mark a notification as read via POST."""
        notif = send_in_app_notification(self.customer1, 'TRANSACTION', 'Alert 1', 'Message 1')
        self.assertFalse(notif.is_read)

        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('notification_mark_read', args=[notif.id]))
        self.assertEqual(response.status_code, 302)

        notif.refresh_from_db()
        self.assertTrue(notif.is_read)
        self.assertIsNotNone(notif.read_at)

    def test_mark_notification_read_idor_protection(self):
        """Customer cannot mark another customer's notification as read."""
        notif = send_in_app_notification(self.customer2, 'SECURITY', 'Cust2 Alert', 'Private')

        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('notification_mark_read', args=[notif.id]))
        self.assertEqual(response.status_code, 404)

        notif.refresh_from_db()
        self.assertFalse(notif.is_read)

    def test_mark_all_notifications_read(self):
        """Customer can mark all their unread notifications as read."""
        send_in_app_notification(self.customer1, 'TRANSACTION', 'Alert 1', 'Msg 1')
        send_in_app_notification(self.customer1, 'SECURITY', 'Alert 2', 'Msg 2')
        send_in_app_notification(self.customer2, 'SECURITY', 'Cust2 Alert', 'Msg 3')

        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('notification_mark_all_read'))
        self.assertEqual(response.status_code, 302)

        self.assertEqual(Notification.objects.filter(recipient=self.customer1, is_read=False).count(), 0)
        # Other customer unaffected
        self.assertEqual(Notification.objects.filter(recipient=self.customer2, is_read=False).count(), 1)


class BankingEventNotificationsTests(TestCase):
    """Verify that banking lifecycle operations emit expected notifications."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(username='admin', password='password123', role='ADMIN', is_superuser=True)
        self.customer1 = User.objects.create_user(username='cust1', password='password123', role='CUSTOMER')
        self.customer2 = User.objects.create_user(username='cust2', password='password123', role='CUSTOMER')

        self.account1 = BankAccount.objects.create(
            user=self.customer1, account_number='ACC10001', account_type='SAVINGS',
            balance=Decimal('20000.00'), email='cust1@example.com', phone='9876543210',
            aadhaar='123456789012', pan='ABCDE1234F', address='123 Main St', pincode='600001'
        )
        self.account2 = BankAccount.objects.create(
            user=self.customer2, account_number='ACC10002', account_type='SAVINGS',
            balance=Decimal('5000.00'), email='cust2@example.com', phone='9876543211',
            aadhaar='123456789013', pan='ABCDE1234G', address='456 Cross St', pincode='600002'
        )

    def test_withdrawal_emits_notification(self):
        """Successful withdrawal creates an in-app notification for the customer."""
        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('withdraw'), {
            'account_id': self.account1.id,
            'amount': '1500.00'
        })
        self.assertEqual(response.status_code, 302)

        notif = Notification.objects.filter(recipient=self.customer1, notification_type='TRANSACTION').first()
        self.assertIsNotNone(notif)
        self.assertIn('Withdrawal Successful', notif.title)
        self.assertIn('1,500.00', notif.message)

    def test_deposit_emits_notification(self):
        """Deposit creates an in-app notification for the recipient customer."""
        self.client.login(username='admin', password='password123')
        response = self.client.post(reverse('deposit'), {
            'account_number': self.account1.account_number,
            'amount': '2500.00'
        })
        self.assertEqual(response.status_code, 302)

        notif = Notification.objects.filter(recipient=self.customer1, notification_type='TRANSACTION').first()
        self.assertIsNotNone(notif)
        self.assertIn('Deposit Received', notif.title)
        self.assertIn('2,500.00', notif.message)

    def test_transfer_emits_notifications_for_both_parties(self):
        """Funds transfer emits notifications for both sender and recipient with dedup protection."""
        self.client.login(username='cust1', password='password123')
        idem_key = str(uuid.uuid4())
        response = self.client.post(reverse('transfer'), {
            'account_id': self.account1.id,
            'account_number': self.account2.account_number,
            'amount': '3000.00',
            'idempotency_key': idem_key
        })
        self.assertEqual(response.status_code, 302)

        sender_notif = Notification.objects.filter(recipient=self.customer1, notification_type='TRANSACTION').first()
        self.assertIsNotNone(sender_notif)
        self.assertIn('Transfer Sent', sender_notif.title)

        receiver_notif = Notification.objects.filter(recipient=self.customer2, notification_type='TRANSACTION').first()
        self.assertIsNotNone(receiver_notif)
        self.assertIn('Transfer Received', receiver_notif.title)

    def test_fd_booking_and_closure_emits_notification(self):
        """Booking and closing a Fixed Deposit generates persistent notifications."""
        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('fd'), {
            'account_id': self.account1.id,
            'amount': '5000.00',
            'months': '12'
        })
        self.assertEqual(response.status_code, 302)

        fd_notif = Notification.objects.filter(recipient=self.customer1, notification_type='FD').first()
        self.assertIsNotNone(fd_notif)
        self.assertIn('Fixed Deposit Created', fd_notif.title)

        fd = FixedDeposit.objects.filter(account=self.account1).first()
        response = self.client.post(reverse('fd_close', args=[fd.id]), {
            'closure_reason': 'Emergency fund need'
        })
        self.assertEqual(response.status_code, 302)

        close_notif = Notification.objects.filter(recipient=self.customer1, notification_type='FD', title__icontains='Closed').first()
        self.assertIsNotNone(close_notif)

    def test_loan_application_and_approval_emits_notifications(self):
        """Applying for a loan and loan approval both emit notifications."""
        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('loan'), {
            'account_id': self.account1.id,
            'amount': '50000.00',
            'months': '12'
        })
        self.assertEqual(response.status_code, 302)

        app_notif = Notification.objects.filter(recipient=self.customer1, notification_type='LOAN', title__icontains='Submitted').first()
        self.assertIsNotNone(app_notif)

        loan = Loan.objects.filter(account=self.account1).first()

        # Admin approves loan
        self.client.login(username='admin', password='password123')
        response = self.client.post(reverse('approve_loan', args=[loan.id]))
        self.assertEqual(response.status_code, 302)

        approval_notif = Notification.objects.filter(recipient=self.customer1, notification_type='LOAN', title__icontains='Approved').first()
        self.assertIsNotNone(approval_notif)

    def test_virtual_card_issuance_and_block_emits_notification(self):
        """Issuing and blocking a virtual debit card generates notifications."""
        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('card_issue'), {
            'account_id': self.account1.id,
            'cardholder_name': 'Manoj Kumar',
            'card_type': 'CLASSIC'
        })
        self.assertEqual(response.status_code, 302)

        card = VirtualDebitCard.objects.filter(user=self.customer1).first()
        self.assertIsNotNone(card)

        card_notif = Notification.objects.filter(recipient=self.customer1, notification_type='CARD', title__icontains='Issued').first()
        self.assertIsNotNone(card_notif)

        response = self.client.post(reverse('card_block', args=[card.id]))
        self.assertEqual(response.status_code, 302)

        block_notif = Notification.objects.filter(recipient=self.customer1, notification_type='CARD', title__icontains='Blocked').first()
        self.assertIsNotNone(block_notif)


class CustomerSupportManagementTests(TestCase):
    """Unit and integration tests for Customer Support / Complaints workflow."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(username='admin', password='password123', role='ADMIN', is_superuser=True)
        self.employee = User.objects.create_user(username='staff1', password='password123', role='EMPLOYEE')
        self.customer1 = User.objects.create_user(username='cust1', password='password123', role='CUSTOMER')
        self.customer2 = User.objects.create_user(username='cust2', password='password123', role='CUSTOMER')

    def test_ticket_number_generation(self):
        """Ticket numbers must follow human-readable unique format."""
        num = generate_ticket_number()
        self.assertTrue(num.startswith('TKT-'))
        self.assertEqual(len(num), 17)

    def test_customer_can_create_ticket(self):
        """Customer can open a new support ticket and receives a confirmation notification."""
        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('support_ticket_create'), {
            'category': 'TRANSACTION',
            'priority': 'HIGH',
            'subject': 'Debit not credited to beneficiary',
            'description': 'I performed a transfer of ₹5000 yesterday but beneficiary has not received it.'
        })
        self.assertEqual(response.status_code, 302)

        ticket = SupportTicket.objects.filter(customer=self.customer1).first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.category, 'TRANSACTION')
        self.assertEqual(ticket.priority, 'HIGH')
        self.assertEqual(ticket.status, 'OPEN')

        # Initial message
        msg = ticket.messages.first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.author, self.customer1)
        self.assertFalse(msg.is_internal_note)

        # Notification dispatched
        notif = Notification.objects.filter(recipient=self.customer1, notification_type='SUPPORT').first()
        self.assertIsNotNone(notif)
        self.assertIn(ticket.ticket_number, notif.title)

    def test_customer_cannot_view_another_customer_ticket(self):
        """Customer cannot access another customer's ticket (IDOR protected, 404)."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer2,
            category='CARD',
            subject='Card issue',
            description='Card issue details',
            status='OPEN'
        )

        self.client.login(username='cust1', password='password123')
        response = self.client.get(reverse('support_ticket_detail', args=[ticket.id]))
        self.assertEqual(response.status_code, 404)

    def test_customer_never_sees_internal_notes(self):
        """CRITICAL: Customers must never see employee internal notes."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer1,
            category='TRANSACTION',
            subject='Query',
            description='Initial query',
            status='IN_PROGRESS'
        )
        SupportMessage.objects.create(
            ticket=ticket, author=self.customer1, message='Customer message', is_internal_note=False
        )
        SupportMessage.objects.create(
            ticket=ticket, author=self.employee, message='Public staff reply', is_internal_note=False
        )
        SupportMessage.objects.create(
            ticket=ticket, author=self.employee, message='SUPER_CONFIDENTIAL_INTERNAL_NOTE_123', is_internal_note=True
        )

        self.client.login(username='cust1', password='password123')
        response = self.client.get(reverse('support_ticket_detail', args=[ticket.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Customer message')
        self.assertContains(response, 'Public staff reply')
        # Crucial assertion: confidential note MUST NOT be rendered
        self.assertNotContains(response, 'SUPER_CONFIDENTIAL_INTERNAL_NOTE_123')

    def test_customer_can_reply_to_open_ticket(self):
        """Customer can submit replies to their open ticket."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer1,
            category='OTHER',
            subject='Query',
            description='Initial query',
            status='WAITING_FOR_CUSTOMER'
        )

        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('support_ticket_reply', args=[ticket.id]), {
            'message': 'Here is the requested screenshot details.'
        })
        self.assertEqual(response.status_code, 302)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'IN_PROGRESS')
        self.assertEqual(ticket.messages.count(), 1)

    def test_customer_reopen_resolved_ticket(self):
        """Customer can reopen a resolved ticket, changing status back to OPEN."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer1,
            category='ACCOUNT',
            subject='Account query',
            description='Details',
            status='RESOLVED',
            resolved_at=timezone.now()
        )

        self.client.login(username='cust1', password='password123')
        response = self.client.post(reverse('support_ticket_reopen', args=[ticket.id]), {
            'reason': 'The issue still persists.'
        })
        self.assertEqual(response.status_code, 302)

        ticket.refresh_from_db()
        self.assertEqual(ticket.status, 'OPEN')
        self.assertIsNone(ticket.resolved_at)

    def test_customer_cannot_access_staff_support_queue(self):
        """Customers are rejected with 403 Forbidden when trying to access staff support queue."""
        self.client.login(username='cust1', password='password123')
        response = self.client.get(reverse('staff_support_queue'))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_view_support_queue_and_detail(self):
        """Staff members can view queue and full ticket conversation including internal notes."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer1,
            category='LOAN',
            subject='EMI delay',
            description='Customer description',
            status='OPEN'
        )
        SupportMessage.objects.create(
            ticket=ticket, author=self.employee, message='INTERNAL_NOTE_XYZ', is_internal_note=True
        )

        self.client.login(username='staff1', password='password123')
        response = self.client.get(reverse('staff_support_queue'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ticket.ticket_number)

        detail_response = self.client.get(reverse('staff_support_detail', args=[ticket.id]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, 'INTERNAL_NOTE_XYZ')

    def test_staff_public_reply_notifies_customer(self):
        """Public staff replies notify customer; internal notes do not."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer1,
            category='OTHER',
            subject='Inquiry',
            description='Details',
            status='OPEN'
        )

        self.client.login(username='staff1', password='password123')

        # 1. Internal Note without status change: no notification sent to customer
        self.client.post(reverse('staff_support_reply', args=[ticket.id]), {
            'message': 'Internal review note',
            'is_internal_note': 'true',
            'status': 'OPEN'
        })
        self.assertEqual(Notification.objects.filter(recipient=self.customer1, notification_type='SUPPORT').count(), 0)

        # 2. Public Reply notifies customer
        self.client.post(reverse('staff_support_reply', args=[ticket.id]), {
            'message': 'We have processed your request.',
            'status': 'RESOLVED'
        })
        cust_notif = Notification.objects.filter(recipient=self.customer1, notification_type='SUPPORT', title__icontains='Support Update').first()
        self.assertIsNotNone(cust_notif)
        self.assertIn(ticket.ticket_number, cust_notif.title)

    def test_admin_assignment_and_employee_restriction(self):
        """Only ADMIN can assign tickets to staff; employees get 403 Forbidden."""
        ticket = SupportTicket.objects.create(
            ticket_number=generate_ticket_number(),
            customer=self.customer1,
            category='OTHER',
            subject='Inquiry',
            description='Details',
            status='OPEN'
        )

        # Employee attempt -> 403
        self.client.login(username='staff1', password='password123')
        response = self.client.post(reverse('admin_support_assign', args=[ticket.id]), {
            'assigned_to': self.employee.id
        })
        self.assertEqual(response.status_code, 403)

        # Admin assignment -> 302 Success
        self.client.login(username='admin', password='password123')
        response = self.client.post(reverse('admin_support_assign', args=[ticket.id]), {
            'assigned_to': self.employee.id
        })
        self.assertEqual(response.status_code, 302)

        ticket.refresh_from_db()
        self.assertEqual(ticket.assigned_to, self.employee)

        # Assigned staff receives notification
        staff_notif = Notification.objects.filter(recipient=self.employee, notification_type='SUPPORT').first()
        self.assertIsNotNone(staff_notif)
        self.assertIn('Ticket Assigned', staff_notif.title)
