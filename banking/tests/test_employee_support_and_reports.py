from decimal import Decimal
from datetime import timedelta
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from accounts.models import User, ProfileAuditLog
from banking.models import (
    BankAccount, Transaction, Loan, FixedDeposit, VirtualDebitCard,
    SupportTicket, SupportMessage, Notification,
    TellerOperationLog, LoanVerificationLog,
    EmployeeActivityLog, log_employee_activity
)
from banking.views import safe_csv_cell


class EmployeeSupportAndReportsTests(TestCase):
    """
    Focused tests for BEBANK Fast-Track Task 3.4:
    - Employee support helpdesk queue, search, filters, pagination.
    - Public replies, internal notes (hidden from customers), and notifications.
    - Assignment permissions: Employee self-claim/release vs Admin full assignment.
    - Audit log creation, immutability, IP attribution, and access boundaries.
    - Reports & analytics: database aggregations, date filters, role isolation.
    - CSV report exports with formula injection neutralization.
    - Employee dashboard personalized metrics and workload tracking.
    """

    def setUp(self):
        self.client = Client()

        # Users
        self.customer = User.objects.create_user(
            username='cust_alex', password='password123', role='CUSTOMER',
            email='alex@example.com', phone='9876543210', first_name='Alex', last_name='V'
        )
        self.other_customer = User.objects.create_user(
            username='cust_bella', password='password123', role='CUSTOMER',
            email='bella@example.com', phone='9876543211', first_name='Bella', last_name='K'
        )
        self.employee = User.objects.create_user(
            username='emp_sarah', password='password123', role='EMPLOYEE',
            email='sarah@bebank.com'
        )
        self.other_employee = User.objects.create_user(
            username='emp_john', password='password123', role='EMPLOYEE',
            email='john@bebank.com'
        )
        self.admin = User.objects.create_user(
            username='admin_boss', password='password123', role='ADMIN',
            email='admin@bebank.com'
        )

        # Accounts
        self.account = BankAccount.objects.create(
            user=self.customer, account_number='1000000001',
            account_type='SAVINGS', balance=Decimal('50000.00'),
            email=self.customer.email, phone=self.customer.phone
        )
        self.other_account = BankAccount.objects.create(
            user=self.other_customer, account_number='1000000002',
            account_type='CURRENT', balance=Decimal('25000.00'),
            email=self.other_customer.email, phone=self.other_customer.phone
        )

        # Support Tickets
        self.ticket1 = SupportTicket.objects.create(
            ticket_number='TKT-20261107-AAAA', customer=self.customer,
            category='TRANSACTION', subject='Transfer delayed to external vendor',
            description='My transfer has not reflected in vendor account.',
            priority='HIGH', status='OPEN', assigned_to=None
        )
        self.ticket2 = SupportTicket.objects.create(
            ticket_number='TKT-20261107-BBBB', customer=self.customer,
            category='CARD', subject='Virtual card blocked automatically',
            description='Unable to use virtual debit card online.',
            priority='URGENT', status='IN_PROGRESS', assigned_to=self.employee
        )
        self.ticket3 = SupportTicket.objects.create(
            ticket_number='TKT-20261107-CCCC', customer=self.other_customer,
            category='LOAN', subject='Loan EMI schedule query',
            description='Question about upcoming installment interest component.',
            priority='LOW', status='RESOLVED', assigned_to=self.other_employee
        )

        # URLs
        self.support_queue_url = reverse('staff_support_queue')
        self.reports_url = reverse('staff_reports')
        self.reports_export_url = reverse('staff_reports_export_csv')
        self.activity_log_url = reverse('staff_activity_log')
        self.activity_export_url = reverse('staff_activity_export_csv')
        self.dashboard_url = reverse('employee_dashboard')

    # =========================================================================
    # 1. SUPPORT QUEUE SEARCH & FILTERS
    # =========================================================================

    def test_support_queue_search_and_filters(self):
        """Staff support queue filters by status, category, priority, and searches contact info."""
        self.client.force_login(self.employee)

        # Filter by status OPEN
        resp = self.client.get(self.support_queue_url, {'status': 'OPEN'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(t.id == self.ticket1.id for t in resp.context['page_obj']))
        self.assertFalse(any(t.id == self.ticket2.id for t in resp.context['page_obj']))

        # Filter assigned to me
        resp_me = self.client.get(self.support_queue_url, {'assigned': 'me'})
        self.assertEqual(resp_me.status_code, 200)
        self.assertTrue(any(t.id == self.ticket2.id for t in resp_me.context['page_obj']))
        self.assertFalse(any(t.id == self.ticket1.id for t in resp_me.context['page_obj']))

        # Filter unassigned
        resp_un = self.client.get(self.support_queue_url, {'assigned': 'unassigned'})
        self.assertEqual(resp_un.status_code, 200)
        self.assertTrue(any(t.id == self.ticket1.id for t in resp_un.context['page_obj']))
        self.assertFalse(any(t.id == self.ticket2.id for t in resp_un.context['page_obj']))

        # Search by customer email
        resp_q = self.client.get(self.support_queue_url, {'q': 'alex@example.com'})
        self.assertEqual(resp_q.status_code, 200)
        self.assertTrue(any(t.id == self.ticket1.id for t in resp_q.context['page_obj']))
        self.assertFalse(any(t.id == self.ticket3.id for t in resp_q.context['page_obj']))

        # Search by customer phone
        resp_ph = self.client.get(self.support_queue_url, {'q': '9876543211'})
        self.assertEqual(resp_ph.status_code, 200)
        self.assertTrue(any(t.id == self.ticket3.id for t in resp_ph.context['page_obj']))
        self.assertFalse(any(t.id == self.ticket1.id for t in resp_ph.context['page_obj']))

    # =========================================================================
    # 2. SUPPORT REPLIES, INTERNAL NOTES & NOTIFICATION CORRECTNESS
    # =========================================================================

    def test_employee_public_reply_and_status_update(self):
        """Public reply creates message, logs activity, and notifies customer."""
        self.client.force_login(self.employee)
        reply_url = reverse('staff_support_reply', kwargs={'ticket_id': self.ticket1.id})
        payload = {
            'message': 'We have escalated this transfer with our settlement clearing house.',
            'is_internal_note': 'false',
            'status': 'IN_PROGRESS',
            'priority': 'HIGH',
        }
        resp = self.client.post(reply_url, data=payload)
        self.assertEqual(resp.status_code, 302)

        self.ticket1.refresh_from_db()
        self.assertEqual(self.ticket1.status, 'IN_PROGRESS')

        # Public message created
        msg = SupportMessage.objects.filter(ticket=self.ticket1, author=self.employee).first()
        self.assertIsNotNone(msg)
        self.assertFalse(msg.is_internal_note)

        # Customer notified for both reply and status change
        notif_reply = Notification.objects.filter(recipient=self.customer, title__startswith='Support Update').first()
        self.assertIsNotNone(notif_reply)
        notif_status = Notification.objects.filter(recipient=self.customer, title__startswith='Ticket Status Updated').first()
        self.assertIsNotNone(notif_status)

        # Activity log created
        act = EmployeeActivityLog.objects.filter(employee=self.employee, action_type='SUPPORT_REPLY').first()
        self.assertIsNotNone(act)
        self.assertEqual(act.customer, self.customer)
        self.assertEqual(act.target_id, self.ticket1.ticket_number)

    def test_employee_internal_note_hidden_from_customer(self):
        """Internal note is visible to staff but strictly hidden from customer view and sends no notification."""
        self.client.force_login(self.employee)
        reply_url = reverse('staff_support_reply', kwargs={'ticket_id': self.ticket1.id})
        secret_note = "CONFIDENTIAL: Customer account flagged for velocity review. Do not disclose."
        payload = {
            'message': secret_note,
            'is_internal_note': 'true',
        }
        initial_notif_count = Notification.objects.filter(recipient=self.customer).count()

        resp = self.client.post(reply_url, data=payload)
        self.assertEqual(resp.status_code, 302)

        # Internal message exists
        msg = SupportMessage.objects.filter(ticket=self.ticket1, is_internal_note=True).first()
        self.assertIsNotNone(msg)
        self.assertEqual(msg.message, secret_note)

        # No customer notification generated for internal note
        self.assertEqual(Notification.objects.filter(recipient=self.customer).count(), initial_notif_count)

        # Activity log created
        act = EmployeeActivityLog.objects.filter(employee=self.employee, action_type='SUPPORT_NOTE').first()
        self.assertIsNotNone(act)

        # Customer views ticket: internal note MUST NOT appear
        self.client.force_login(self.customer)
        cust_resp = self.client.get(reverse('support_ticket_detail', kwargs={'ticket_id': self.ticket1.id}))
        self.assertEqual(cust_resp.status_code, 200)
        self.assertNotContains(cust_resp, secret_note)
        self.assertFalse(any(m.is_internal_note for m in cust_resp.context['messages_list']))

        # Staff views ticket: internal note MUST appear
        self.client.force_login(self.employee)
        staff_resp = self.client.get(reverse('staff_support_detail', kwargs={'ticket_id': self.ticket1.id}))
        self.assertEqual(staff_resp.status_code, 200)
        self.assertContains(staff_resp, secret_note)
        self.assertContains(staff_resp, 'INTERNAL NOTE')

    # =========================================================================
    # 3. TICKET ASSIGNMENT PERMISSIONS & BOUNDARIES
    # =========================================================================

    def test_employee_can_claim_and_release_tickets(self):
        """Employee can self-claim unassigned tickets and release their own tickets."""
        self.client.force_login(self.employee)
        assign_url = reverse('staff_support_assign', kwargs={'ticket_id': self.ticket1.id})

        # Claim ticket1
        resp = self.client.post(assign_url, {'action': 'claim'})
        self.assertEqual(resp.status_code, 302)
        self.ticket1.refresh_from_db()
        self.assertEqual(self.ticket1.assigned_to, self.employee)

        # Release ticket1
        resp_rel = self.client.post(assign_url, {'action': 'release'})
        self.assertEqual(resp_rel.status_code, 302)
        self.ticket1.refresh_from_db()
        self.assertIsNone(self.ticket1.assigned_to)

    def test_employee_cannot_assign_to_arbitrary_staff_id(self):
        """Employee attempting to assign ticket to another staff ID receives 403 Forbidden."""
        self.client.force_login(self.employee)
        assign_url = reverse('staff_support_assign', kwargs={'ticket_id': self.ticket1.id})

        resp = self.client.post(assign_url, {'assigned_to': str(self.other_employee.id)})
        self.assertEqual(resp.status_code, 403)

        self.ticket1.refresh_from_db()
        self.assertIsNone(self.ticket1.assigned_to)

    def test_admin_can_assign_to_any_staff_and_clear(self):
        """Admin can assign tickets to any active staff member or clear assignment."""
        self.client.force_login(self.admin)
        assign_url = reverse('staff_support_assign', kwargs={'ticket_id': self.ticket1.id})

        # Assign to other_employee
        resp = self.client.post(assign_url, {'assigned_to': str(self.other_employee.id)})
        self.assertEqual(resp.status_code, 302)
        self.ticket1.refresh_from_db()
        self.assertEqual(self.ticket1.assigned_to, self.other_employee)

        # Notification sent to assigned staff
        notif = Notification.objects.filter(recipient=self.other_employee, notification_type='SUPPORT').first()
        self.assertIsNotNone(notif)
        self.assertIn('Ticket Assigned', notif.title)

        # Clear assignment
        resp_clr = self.client.post(assign_url, {'assigned_to': ''})
        self.assertEqual(resp_clr.status_code, 302)
        self.ticket1.refresh_from_db()
        self.assertIsNone(self.ticket1.assigned_to)

    # =========================================================================
    # 4. EMPLOYEE ACTIVITY LOG IMMUTABILITY & ACCESS BOUNDARIES
    # =========================================================================

    def test_employee_activity_log_immutability(self):
        """EmployeeActivityLog entries cannot be updated or deleted."""
        log = log_employee_activity(
            employee=self.employee, action_type='KYC_APPROVED',
            description="Approved customer KYC", customer=self.customer
        )
        self.assertIsNotNone(log)

        # Attempt to modify
        log.description = "Tampered description"
        with self.assertRaises(PermissionError):
            log.save()

        # Attempt to delete
        with self.assertRaises(PermissionError):
            log.delete()

    def test_customer_cannot_generate_employee_activity_log(self):
        """log_employee_activity strictly rejects generation by customer users."""
        res = log_employee_activity(
            employee=self.customer, action_type='KYC_APPROVED',
            description="Customer trying to log staff activity"
        )
        self.assertIsNone(res)

    def test_activity_log_view_role_boundaries(self):
        """Employees view only their own logs; Admin views all logs."""
        log_employee_activity(employee=self.employee, action_type='TELLER_DEPOSIT', description="Deposit 1")
        log_employee_activity(employee=self.other_employee, action_type='TELLER_WITHDRAWAL', description="Withdrawal 1")

        # Employee view
        self.client.force_login(self.employee)
        resp = self.client.get(self.activity_log_url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(any(l.employee == self.employee for l in resp.context['page_obj']))
        self.assertFalse(any(l.employee == self.other_employee for l in resp.context['page_obj']))

        # Customer view blocked
        self.client.force_login(self.customer)
        resp_cust = self.client.get(self.activity_log_url)
        self.assertIn(resp_cust.status_code, [302, 403])

        # Admin view sees all
        self.client.force_login(self.admin)
        resp_adm = self.client.get(self.activity_log_url)
        self.assertEqual(resp_adm.status_code, 200)
        self.assertTrue(any(l.employee == self.employee for l in resp_adm.context['page_obj']))
        self.assertTrue(any(l.employee == self.other_employee for l in resp_adm.context['page_obj']))

    # =========================================================================
    # 5. REPORTS & ANALYTICS DATABASE AGGREGATIONS & CSV EXPORT
    # =========================================================================

    def test_reports_database_aggregations_and_filters(self):
        """Reports console computes real database counter aggregations and honors date filters."""
        self.client.force_login(self.employee)

        today = timezone.localdate()
        Transaction.objects.create(
            account=self.account, transaction_type='DEPOSIT', amount=Decimal('15000.00'),
            reference_id='TX-RPT-DEP-1', created_at=timezone.now()
        )
        Transaction.objects.create(
            account=self.account, transaction_type='WITHDRAW', amount=Decimal('5000.00'),
            reference_id='TX-RPT-WDR-1', created_at=timezone.now()
        )

        resp = self.client.get(self.reports_url, {
            'report_type': 'overview',
            'date_from': today.strftime('%Y-%m-%d'),
            'date_to': today.strftime('%Y-%m-%d'),
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['deposit_volume'], Decimal('15000.00'))
        self.assertEqual(resp.context['withdrawal_volume'], Decimal('5000.00'))
        self.assertEqual(resp.context['net_cash_flow'], Decimal('10000.00'))
        self.assertContains(resp, '15000.00')

    def test_reports_csv_export_and_formula_escaping(self):
        """CSV export returns attachment with safe_csv_cell formula neutralization."""
        self.client.force_login(self.employee)

        resp = self.client.get(self.reports_export_url, {'report_type': 'transactions'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment; filename="bebank_report_transactions_', resp['Content-Disposition'])

        # Verification of safe_csv_cell
        self.assertEqual(safe_csv_cell('=SUM(A1:A10)'), "'=SUM(A1:A10)")
        self.assertEqual(safe_csv_cell('+12345'), "'+12345")
        self.assertEqual(safe_csv_cell('-500'), "'-500")
        self.assertEqual(safe_csv_cell('@exec'), "'@exec")
        self.assertEqual(safe_csv_cell('Normal text'), "Normal text")

        # Export generates activity log entry
        act = EmployeeActivityLog.objects.filter(employee=self.employee, action_type='REPORT_EXPORT').first()
        self.assertIsNotNone(act)

    def test_employee_performance_report_isolation(self):
        """Employee can only view their own performance metrics in reports."""
        self.client.force_login(self.employee)
        resp = self.client.get(self.reports_url, {'report_type': 'performance'})
        self.assertEqual(resp.status_code, 200)

        perf_rows = resp.context['staff_performance']
        self.assertEqual(len(perf_rows), 1)
        self.assertEqual(perf_rows[0]['user'], self.employee)

        # Admin can view all performance metrics
        self.client.force_login(self.admin)
        resp_adm = self.client.get(self.reports_url, {'report_type': 'performance'})
        self.assertEqual(resp_adm.status_code, 200)
        adm_perf = resp_adm.context['staff_performance']
        self.assertGreaterEqual(len(adm_perf), 2)

    # =========================================================================
    # 6. EMPLOYEE DASHBOARD PERSONALIZED WORKLOAD METRICS
    # =========================================================================

    def test_employee_dashboard_personalized_metrics(self):
        """Dashboard surfaces live personal workload counters and recent activity feed."""
        self.client.force_login(self.employee)

        # Create teller log and activity log for employee
        today = timezone.localdate()
        TellerOperationLog.objects.create(
            employee=self.employee, operation_type='DEPOSIT',
            account=self.account, customer=self.customer, amount=Decimal('7500.00'),
            reference_id='TLR-DASH-1', status='SUCCESS'
        )
        log_employee_activity(
            employee=self.employee, action_type='KYC_APPROVED',
            description="Verified demo driving license", customer=self.customer
        )

        resp = self.client.get(self.dashboard_url)
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(resp.context['my_pending_tickets'], 1)  # ticket2 is assigned to employee
        self.assertEqual(resp.context['my_teller_operations_today'], 1)
        self.assertEqual(resp.context['my_teller_volume_today'], Decimal('7500.00'))
        self.assertTrue(len(resp.context['my_recent_actions']) >= 1)

        # Assert visual presence
        self.assertContains(resp, 'My Operational Workload')
        self.assertContains(resp, 'Reports &amp; Analytics')
        self.assertContains(resp, 'Audit Logs')
