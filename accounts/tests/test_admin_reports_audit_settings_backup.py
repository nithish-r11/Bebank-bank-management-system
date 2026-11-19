"""
Comprehensive Test Suite for BEBANK Task 4.5:
Admin Reports, Audit Logs, System Settings & Backup/Recovery
"""

import os
import shutil
import tempfile
import sqlite3
from decimal import Decimal
from io import StringIO
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
from django.core.management.base import CommandError
from django.conf import settings

from accounts.models import User, ProfileAuditLog
from banking.models import (
    BankAccount,
    Transaction,
    Loan,
    LoanRepayment,
    FixedDeposit,
    VirtualDebitCard,
    SupportTicket,
    EmployeeActivityLog,
    LoanVerificationLog,
    DebitCardAuditLog,
    TellerOperationLog,
)
from accounts.views import escape_csv_formula


class AdminReportsAuditSettingsBackupTests(TestCase):
    """
    Validation of Admin Reports, Multi-Source Audit Surveillance,
    System Diagnostics, Online SQLite Backup, and Disaster Recovery.
    """

    def setUp(self):
        self.client = Client()

        # Create Admin
        self.admin_user = User.objects.create_user(
            username='admin_reports_test',
            email='admin_reports@bebank.local',
            password='TestPassword123!',
            role='ADMIN',
            is_active=True,
            account_locked=False,
            is_staff=True,
            is_superuser=True
        )

        # Create Employee
        self.employee_user = User.objects.create_user(
            username='emp_reports_test',
            email='emp_reports@bebank.local',
            password='TestPassword123!',
            role='EMPLOYEE',
            is_active=True,
            account_locked=False,
            is_staff=False,
            is_superuser=False
        )

        # Create Customer
        self.customer_user = User.objects.create_user(
            username='cust_reports_test',
            email='cust_reports@bebank.local',
            password='TestPassword123!',
            role='CUSTOMER',
            is_active=True,
            account_locked=False,
            is_staff=False,
            is_superuser=False
        )

        # Create Customer 2
        self.customer_user2 = User.objects.create_user(
            username='cust_reports_test2',
            email='cust_reports2@bebank.local',
            password='TestPassword123!',
            role='CUSTOMER',
            is_active=True,
            account_locked=False,
            is_staff=False,
            is_superuser=False
        )

        # Accounts
        self.account1 = BankAccount.objects.create(
            user=self.customer_user,
            account_number='1000000001',
            account_type='SAVINGS',
            balance=Decimal('50000.00'),
            status='ACTIVE'
        )
        self.account2 = BankAccount.objects.create(
            user=self.customer_user2,
            account_number='1000000002',
            account_type='SAVINGS',
            balance=Decimal('25000.00'),
            status='ACTIVE'
        )

        # Create Transactions
        # Deposit
        self.txn_deposit = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('10000.00'),
            reference_id='DEP-TEST-001',
            balance_before=Decimal('40000.00'),
            balance_after=Decimal('50000.00')
        )
        # Withdrawal
        self.txn_withdraw = Transaction.objects.create(
            account=self.account1,
            transaction_type='WITHDRAW',
            amount=Decimal('5000.00'),
            reference_id='WTH-TEST-001',
            balance_before=Decimal('50000.00'),
            balance_after=Decimal('45000.00')
        )
        # Symmetric Transfer (TRANSFER_DEBIT + TRANSFER_CREDIT with same reference_id)
        self.txn_xfer_debit = Transaction.objects.create(
            account=self.account1,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('2000.00'),
            reference_id='XFER-REF-1001',
            balance_before=Decimal('45000.00'),
            balance_after=Decimal('43000.00')
        )
        self.txn_xfer_credit = Transaction.objects.create(
            account=self.account2,
            transaction_type='TRANSFER_CREDIT',
            amount=Decimal('2000.00'),
            reference_id='XFER-REF-1001',
            balance_before=Decimal('23000.00'),
            balance_after=Decimal('25000.00')
        )

        # Teller Log
        self.teller_log = TellerOperationLog.objects.create(
            employee=self.employee_user,
            customer=self.customer_user,
            account=self.account1,
            operation_type='DEPOSIT',
            amount=Decimal('10000.00'),
            reference_id='DEP-TEST-001',
            status='SUCCESS',
            description='Initial deposit by teller'
        )

        # Loan
        self.loan = Loan.objects.create(
            account=self.account1,
            amount=Decimal('100000.00'),
            interest_rate=Decimal('10.50'),
            duration_months=12,
            emi_amount=Decimal('8815.00'),
            total_payable=Decimal('105780.00'),
            outstanding_balance=Decimal('105780.00'),
            status='APPROVED',
            disbursed_at=timezone.now()
        )

        # Fixed Deposit
        self.fd = FixedDeposit.objects.create(
            account=self.account1,
            amount=Decimal('20000.00'),
            interest_rate=Decimal('7.00'),
            duration_months=12,
            maturity_amount=Decimal('21400.00'),
            status='ACTIVE',
            maturity_date=timezone.now() + timezone.timedelta(days=365)
        )

        # Debit Card
        self.card = VirtualDebitCard.objects.create(
            user=self.customer_user,
            account=self.account1,
            card_number='4532000011112222',
            cardholder_name='Cust Reports Test',
            status='ACTIVE',
            daily_limit=Decimal('50000.00')
        )

        # Support Ticket
        self.ticket = SupportTicket.objects.create(
            customer=self.customer_user,
            ticket_number='TKT-TEST-001',
            category='OTHER',
            subject='Inquiry about account statement',
            description='Need past statement',
            status='OPEN',
            priority='MEDIUM',
            assigned_to=self.employee_user
        )

        # Audit Logs
        self.emp_log = EmployeeActivityLog.objects.create(
            employee=self.employee_user,
            customer=self.customer_user,
            action_type='TELLER_DEPOSIT',
            target_id='DEP-TEST-001',
            description='Processed cash deposit of 10000.00',
            ip_address='192.168.1.100'
        )

        self.profile_log = ProfileAuditLog.objects.create(
            user=self.customer_user,
            updated_by=self.employee_user,
            changes="Phone: '9999999999' -> '8888888888'",
            ip_address='192.168.1.101'
        )

        self.loan_log = LoanVerificationLog.objects.create(
            loan=self.loan,
            performed_by=self.employee_user,
            action='VERIFICATION_NOTE',
            notes='All documents physically verified'
        )

        self.card_log = DebitCardAuditLog.objects.create(
            card=self.card,
            performed_by=self.admin_user,
            action='BLOCKED_ADMIN',
            reason='Security review routine check'
        )

        # Directory for test backups
        self.backup_dir = os.path.join(settings.BASE_DIR, 'backups')
        self.created_backups = []

    def tearDown(self):
        # Clean up any test backup files created
        for f in self.created_backups:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    # =========================================================================
    # 1. SECURITY & ACCESS CONTROL TESTS
    # =========================================================================

    def test_unauthenticated_user_redirected_to_login(self):
        """Unauthenticated requests to admin reports/audit/settings/backup redirect to login."""
        endpoints = [
            reverse('admin_reports'),
            reverse('admin_reports_export_csv'),
            reverse('admin_audit_log'),
            reverse('admin_audit_export_csv'),
            reverse('admin_settings'),
            reverse('admin_backup'),
            reverse('admin_backup_create'),
            reverse('admin_backup_download'),
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"Failed for {url}")
            self.assertIn('/login/', response.url)

    def test_customer_denied_access_to_all_admin_endpoints(self):
        """Customer role must receive 403 Forbidden for all admin endpoints."""
        self.client.force_login(self.customer_user)
        endpoints = [
            reverse('admin_reports'),
            reverse('admin_reports_export_csv'),
            reverse('admin_audit_log'),
            reverse('admin_audit_export_csv'),
            reverse('admin_settings'),
            reverse('admin_backup'),
            reverse('admin_backup_create'),
            reverse('admin_backup_download'),
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, f"Expected 403 for Customer on {url}")

    def test_employee_denied_access_to_admin_endpoints(self):
        """Employee role must receive 403 Forbidden for admin-only workspaces."""
        self.client.force_login(self.employee_user)
        endpoints = [
            reverse('admin_reports'),
            reverse('admin_reports_export_csv'),
            reverse('admin_audit_log'),
            reverse('admin_audit_export_csv'),
            reverse('admin_settings'),
            reverse('admin_backup'),
            reverse('admin_backup_create'),
            reverse('admin_backup_download'),
        ]
        for url in endpoints:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, f"Expected 403 for Employee on {url}")

    # =========================================================================
    # 2. ADMIN REPORTS WORKSPACE TESTS
    # =========================================================================

    def test_admin_reports_view_renders_correctly(self):
        """Admin reports view renders live metrics with single-leg transfer invariant."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_reports'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_reports.html')

        ctx = response.context
        # Deposits: 10000.00
        self.assertEqual(ctx['deposit_volume'], Decimal('10000.00'))
        self.assertEqual(ctx['deposit_count'], 1)

        # Withdrawals: 5000.00
        self.assertEqual(ctx['withdrawal_volume'], Decimal('5000.00'))
        self.assertEqual(ctx['withdrawal_count'], 1)

        # Single-leg transfer invariant: Exactly 2000.00 (not 4000.00)
        self.assertEqual(ctx['transfer_volume'], Decimal('2000.00'))
        self.assertEqual(ctx['transfer_count'], 1)

        # Loans & FDs
        self.assertEqual(ctx['active_loans_portfolio'], 1)
        self.assertEqual(ctx['active_fds_count'], 1)

        # Staff Workload row for employee
        staff_data = {item['employee'].username: item for item in ctx['staff_workload']}
        self.assertIn(self.employee_user.username, staff_data)
        emp_stats = staff_data[self.employee_user.username]
        self.assertEqual(emp_stats['teller_count'], 1)
        self.assertEqual(emp_stats['loan_verif_count'], 1)

    def test_admin_reports_date_filtering(self):
        """Date filtering in admin reports bounds transactions correctly."""
        self.client.force_login(self.admin_user)
        today = timezone.localdate().strftime('%Y-%m-%d')
        response = self.client.get(reverse('admin_reports'), {
            'date_from': today,
            'date_to': today
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['deposit_volume'], Decimal('10000.00'))

        # Future date range has zero volume
        future_date = (timezone.localdate() + timezone.timedelta(days=10)).strftime('%Y-%m-%d')
        response_future = self.client.get(reverse('admin_reports'), {
            'date_from': future_date,
            'date_to': future_date
        })
        self.assertEqual(response_future.status_code, 200)
        self.assertEqual(response_future.context['deposit_volume'], Decimal('0.00'))
        self.assertEqual(response_future.context['deposit_count'], 0)

    # =========================================================================
    # 3. CSV EXPORT & FORMULA INJECTION HARDENING TESTS
    # =========================================================================

    def test_escape_csv_formula_sanitization(self):
        """Formula injection payloads (=, +, -, @, tab, CR) are safely escaped."""
        dangerous_inputs = [
            ('=1+2', "'=1+2"),
            ('+cmd|/c calc', "'+cmd|/c calc"),
            ('-2+3', "'-2+3"),
            ('@SUM(A1:A10)', "'@SUM(A1:A10)"),
            ('\tmalicious', "'\tmalicious"),
            ('\rmalicious', "'\rmalicious"),
            ('Safe Standard String', 'Safe Standard String'),
            (12345, '12345'),
            (Decimal('50.00'), '50.00'),
            (None, ''),
        ]
        for raw, expected in dangerous_inputs:
            sanitized = escape_csv_formula(raw)
            self.assertEqual(sanitized, expected, f"Sanitization mismatch for {raw}")

    def test_admin_reports_export_csv_view(self):
        """Admin reports CSV export generates valid CSV and logs REPORT_EXPORT activity."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_reports_export_csv'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="bebank_admin_reports_', response['Content-Disposition'])

        content = response.content.decode('utf-8')
        self.assertIn("BEBANK Administrative & Operational Report", content)
        self.assertIn("Total Simulated Ledger Holdings", content)
        self.assertIn("Customer P2P Transfer Volume", content)

        # Verify audit activity logged
        audit = EmployeeActivityLog.objects.filter(
            employee=self.admin_user,
            action_type='REPORT_EXPORT'
        ).first()
        self.assertIsNotNone(audit, "Admin report export must create an EmployeeActivityLog entry.")

    # =========================================================================
    # 4. MULTI-SOURCE AUDIT SURVEILLANCE TESTS
    # =========================================================================

    def test_admin_audit_log_view_all_sources(self):
        """Admin audit workspace renders and filters across all 5 audit sources."""
        self.client.force_login(self.admin_user)

        sources = ['employee', 'profile', 'loan', 'card', 'teller']
        for src in sources:
            response = self.client.get(reverse('admin_audit_log'), {'source': src})
            self.assertEqual(response.status_code, 200, f"Audit log failed for source={src}")
            self.assertTemplateUsed(response, 'admin_audit_log.html')
            self.assertEqual(response.context['selected_source'], src)
            self.assertGreaterEqual(len(response.context['page_obj']), 1)

    def test_admin_audit_log_search_and_action_filtering(self):
        """Search query and action type filter correctly narrow audit entries."""
        self.client.force_login(self.admin_user)

        # Search for teller action
        response = self.client.get(reverse('admin_audit_log'), {
            'source': 'employee',
            'q': 'DEP-TEST-001'
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['page_obj']), 1)
        self.assertContains(response, 'DEP-TEST-001')

        # Action type filter
        response_action = self.client.get(reverse('admin_audit_log'), {
            'source': 'employee',
            'action_type': 'TELLER_DEPOSIT'
        })
        self.assertEqual(response_action.status_code, 200)
        self.assertEqual(len(response_action.context['page_obj']), 1)

    def test_admin_audit_export_csv_view(self):
        """Admin audit log CSV export generates valid CSV and logs AUDIT_EXPORT activity."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_audit_export_csv'), {'source': 'employee'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="bebank_audit_employee_', response['Content-Disposition'])

        content = response.content.decode('utf-8')
        self.assertIn("Timestamp,Actor Username,Actor Role,Action Type", content)
        self.assertIn("TELLER_DEPOSIT", content)

        # Verify audit activity logged
        audit = EmployeeActivityLog.objects.filter(
            employee=self.admin_user,
            action_type='REPORT_EXPORT'
        ).first()
        self.assertIsNotNone(audit, "Admin audit export must create an EmployeeActivityLog entry.")

    # =========================================================================
    # 5. SYSTEM SETTINGS & DIAGNOSTICS TESTS
    # =========================================================================

    def test_admin_settings_view_diagnostics(self):
        """Admin settings renders live diagnostics without exposing secrets."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_settings'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_settings.html')

        ctx = response.context
        self.assertIn('sqlite', ctx['db_engine'].lower())
        self.assertIsNotNone(ctx['db_latency_ms'])
        self.assertIn('Django', ctx['django_version'])
        self.assertIn('Python', ctx['python_version'])

        # Security: ensure secret key or db passwords are NOT rendered
        content = response.content.decode('utf-8')
        self.assertNotIn(settings.SECRET_KEY, content)
        self.assertContains(response, 'Symmetric two-leg balance snapshots')
        self.assertContains(response, 'Teller Idempotency Protection')

    # =========================================================================
    # 6. DATABASE BACKUP & RESTORE WORKSPACE TESTS
    # =========================================================================

    def test_admin_backup_view_renders_sqlite_health(self):
        """Admin backup view renders database health, row inventory, and snapshot list."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_backup'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_backup.html')

        ctx = response.context
        self.assertEqual(ctx['integrity_status'], 'ok')
        self.assertGreaterEqual(ctx['db_size_bytes'], 0)
        self.assertGreater(ctx['count_users'], 0)
        self.assertGreater(ctx['count_accounts'], 0)

    def test_admin_backup_create_view_via_post(self):
        """Admin backup create via POST takes an atomic SQLite snapshot and verifies it."""
        self.client.force_login(self.admin_user)

        # GET method should be rejected (405 Method Not Allowed)
        get_resp = self.client.get(reverse('admin_backup_create'))
        self.assertEqual(get_resp.status_code, 405)

        # POST creates backup
        post_resp = self.client.post(reverse('admin_backup_create'))
        self.assertEqual(post_resp.status_code, 302)
        self.assertRedirects(post_resp, reverse('admin_backup'))

        # Check backup file created in backups/
        backup_dir = os.path.join(settings.BASE_DIR, 'backups')
        self.assertTrue(os.path.exists(backup_dir))
        files = [f for f in os.listdir(backup_dir) if f.startswith('bebank_backup_') and f.endswith('.sqlite3')]
        self.assertGreater(len(files), 0, "Expected at least one snapshot created.")

        latest_snapshot = sorted(files)[-1]
        snapshot_path = os.path.join(backup_dir, latest_snapshot)
        self.created_backups.append(snapshot_path)

        # Verify snapshot integrity directly using sqlite3
        conn = sqlite3.connect(snapshot_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        result = cursor.fetchone()[0]
        conn.close()
        self.assertEqual(result, "ok")

        # Verify audit activity logged
        audit = EmployeeActivityLog.objects.filter(
            employee=self.admin_user,
            action_type='ADMIN_BACKUP_ACTION'
        ).first()
        self.assertIsNotNone(audit, "Admin backup snapshot creation must be logged.")

    def test_admin_backup_download_view_traversal_protection(self):
        """Admin backup download view prevents directory traversal attacks."""
        self.client.force_login(self.admin_user)

        # Traversal attempt with ../
        traversal_resp = self.client.get(reverse('admin_backup_download'), {'file': '../../../manage.py'})
        # Should return 400 Bad Request or 404 Not Found, never 200
        self.assertIn(traversal_resp.status_code, [400, 404])

        # Non-existent file
        missing_resp = self.client.get(reverse('admin_backup_download'), {'file': 'non_existent_snapshot.sqlite3'})
        self.assertEqual(missing_resp.status_code, 404)

    def test_admin_backup_download_view_valid_snapshot(self):
        """Admin backup download view serves verified snapshot with application/x-sqlite3."""
        self.client.force_login(self.admin_user)

        # Create a real snapshot first
        self.client.post(reverse('admin_backup_create'))
        backup_dir = os.path.join(settings.BASE_DIR, 'backups')
        files = [f for f in os.listdir(backup_dir) if f.startswith('bebank_backup_') and f.endswith('.sqlite3')]
        self.assertGreater(len(files), 0)
        snapshot_name = sorted(files)[-1]
        self.created_backups.append(os.path.join(backup_dir, snapshot_name))

        response = self.client.get(reverse('admin_backup_download'), {'file': snapshot_name})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/x-sqlite3')
        self.assertIn(snapshot_name, response['Content-Disposition'])

    # =========================================================================
    # 7. RESTORE MANAGEMENT COMMAND SAFEGUARDS
    # =========================================================================

    def test_restore_sqlite_backup_command_safeguards(self):
        """Management command restore_sqlite_backup requires --confirm and validates input."""
        # Test 1: Missing file raises CommandError
        out = StringIO()
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command('restore_sqlite_backup', 'non_existent_file.sqlite3', stdout=out, stderr=err)

        # Create temporary target db and source backup file on disk
        with tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False) as target_file, \
             tempfile.NamedTemporaryFile(suffix='.sqlite3', delete=False) as backup_file:
            target_path = target_file.name
            backup_path = backup_file.name

        self.created_backups.extend([target_path, backup_path])

        # Initialize both as valid SQLite databases
        for p in [target_path, backup_path]:
            conn = sqlite3.connect(p)
            conn.execute("CREATE TABLE test_tab (id INT PRIMARY KEY, val TEXT);")
            conn.execute("INSERT INTO test_tab VALUES (1, 'initial');")
            conn.commit()
            conn.close()

        with override_settings(DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': target_path}}):
            # Test 2: Calling without --confirm does NOT restore and outputs dry-run notice
            out_dry = StringIO()
            call_command('restore_sqlite_backup', backup_path, stdout=out_dry)
            val = out_dry.getvalue()
            self.assertIn("[DRY-RUN]", val)
            self.assertIn("Pass --confirm to execute", val)

            # Test 3: Calling with --confirm executes restoration successfully
            out_confirm = StringIO()
            call_command('restore_sqlite_backup', backup_path, confirm=True, stdout=out_confirm)
            val_confirm = out_confirm.getvalue()
            self.assertIn("DATABASE RESTORE COMPLETED SUCCESSFULLY", val_confirm)
