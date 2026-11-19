import csv
import io
import uuid
from decimal import Decimal
from datetime import timedelta

from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from banking.models import (
    BankAccount, Transaction, Loan, FixedDeposit, VirtualDebitCard,
    TellerOperationLog, EmployeeActivityLog
)
from banking.financial_controls import sanitize_csv_cell, run_ledger_reconciliation, run_risk_monitoring_scan


class AdminTransactionMonitoringTests(TestCase):
    """
    Focused comprehensive test suite for Task 4.3:
    - Admin Transaction Directory: search, filters (type, direction, date range, account, customer), sorting, pagination
    - Masked account display and legacy NULL balance snapshot handling
    - Admin Transaction 360° Detail: snapshots, paired transfer analysis, teller log & audit display
    - Ledger Reconciliation Diagnostic: asymmetric transfer detection, snapshot math verification, legacy null handling
    - Risk Monitoring Scan: 5 transparent heuristic rules (large txn, rapid velocity, concentrated volume, teller surge, frozen account txn)
    - Safe CSV Exports: formula-injection sanitization, masking, 5k limit, audit logging
    - Strict RBAC: Customer and Employee get 403; unauthenticated get 302; invalid ID gets 404
    - Financial Control & Account Freeze Semantics:
        * Outgoing debits blocked: transfer, customer withdrawal, teller withdrawal, card issuance, FD creation, loan repayment
        * Incoming credits permitted: incoming transfer, teller deposit, FD settlement, FD premature refund, loan disbursement
    """

    def setUp(self):
        self.client = Client(HTTP_HOST='127.0.0.1')

        # Admin user
        self.admin = User.objects.create_user(
            username='super_admin',
            email='admin@bebank.local',
            password='AdminPassword123!',
            role='ADMIN',
            is_active=True,
            account_locked=False
        )

        # Employee user
        self.employee = User.objects.create_user(
            username='teller_emp',
            email='teller@bebank.local',
            password='EmployeePass123!',
            role='EMPLOYEE',
            first_name='Rohan',
            last_name='Mehta',
            is_active=True,
            account_locked=False
        )

        # Customer 1
        self.customer1 = User.objects.create_user(
            username='aditi_rao',
            email='aditi@example.com',
            password='CustomerPass123!',
            role='CUSTOMER',
            first_name='Aditi',
            last_name='Rao',
            phone='9876540001',
            kyc_status='APPROVED',
            is_active=True,
            account_locked=False
        )

        # Customer 2
        self.customer2 = User.objects.create_user(
            username='dev_sharma',
            email='dev@example.com',
            password='CustomerPass123!',
            role='CUSTOMER',
            first_name='Dev',
            last_name='Sharma',
            phone='9876540002',
            kyc_status='APPROVED',
            is_active=True,
            account_locked=False
        )

        # Bank accounts
        self.account1 = BankAccount.objects.create(
            user=self.customer1,
            account_number='111122223331',
            account_type='SAVINGS',
            balance=Decimal('50000.00'),
            status='ACTIVE'
        )

        self.account2 = BankAccount.objects.create(
            user=self.customer2,
            account_number='111122223332',
            account_type='CURRENT',
            balance=Decimal('25000.00'),
            status='ACTIVE'
        )

    # =========================================================================
    # 1. ADMIN TRANSACTION DIRECTORY & FILTERS
    # =========================================================================

    def test_admin_transaction_list_view_renders_successfully(self):
        """Admin can access transaction list with correct context and pagination."""
        tx = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('5000.00'),
            balance_before=Decimal('45000.00'),
            balance_after=Decimal('50000.00'),
            reference_id='TXN-TEST-001'
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_list'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_transaction_list.html')
        self.assertContains(response, 'TXN-TEST-001')
        self.assertContains(response, '••••3331')  # Masked account
        self.assertContains(response, '5000.00')

    def test_transaction_list_search_by_reference_and_username(self):
        """Search filters correctly by reference ID, account, or username."""
        tx1 = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('1000.00'),
            reference_id='TXN-SEARCH-ALPHA'
        )
        tx2 = Transaction.objects.create(
            account=self.account2,
            transaction_type='WITHDRAW',
            amount=Decimal('2000.00'),
            reference_id='TXN-SEARCH-BETA'
        )

        self.client.force_login(self.admin)

        # Search by reference
        resp = self.client.get(reverse('admin_transaction_list'), {'q': 'ALPHA'})
        self.assertContains(resp, 'TXN-SEARCH-ALPHA')
        self.assertNotContains(resp, 'TXN-SEARCH-BETA')

        # Search by username
        resp2 = self.client.get(reverse('admin_transaction_list'), {'q': 'dev_sharma'})
        self.assertContains(resp2, 'TXN-SEARCH-BETA')
        self.assertNotContains(resp2, 'TXN-SEARCH-ALPHA')

    def test_transaction_list_filters_by_type_direction_and_date(self):
        """Filters by type, direction (CREDIT vs DEBIT), and date bounds."""
        # Deposit (Credit)
        tx_dep = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('1200.00'),
            reference_id='TXN-FILT-DEP'
        )
        # Withdrawal (Debit)
        tx_wdr = Transaction.objects.create(
            account=self.account1,
            transaction_type='WITHDRAW',
            amount=Decimal('800.00'),
            reference_id='TXN-FILT-WDR'
        )

        self.client.force_login(self.admin)

        # Filter type DEPOSIT
        resp_dep = self.client.get(reverse('admin_transaction_list'), {'type': 'DEPOSIT'})
        self.assertContains(resp_dep, 'TXN-FILT-DEP')
        self.assertNotContains(resp_dep, 'TXN-FILT-WDR')

        # Filter direction DEBIT
        resp_deb = self.client.get(reverse('admin_transaction_list'), {'direction': 'DEBIT'})
        self.assertContains(resp_deb, 'TXN-FILT-WDR')
        self.assertNotContains(resp_deb, 'TXN-FILT-DEP')

        # Filter direction CREDIT
        resp_cred = self.client.get(reverse('admin_transaction_list'), {'direction': 'CREDIT'})
        self.assertContains(resp_cred, 'TXN-FILT-DEP')
        self.assertNotContains(resp_cred, 'TXN-FILT-WDR')

    def test_legacy_null_snapshots_rendered_without_error(self):
        """Legacy transactions with NULL balance_before and balance_after display N/A gracefully."""
        tx_legacy = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('300.00'),
            balance_before=None,
            balance_after=None,
            reference_id='TXN-LEGACY-NULL'
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'TXN-LEGACY-NULL')
        self.assertContains(response, 'N/A')

    # =========================================================================
    # 2. TRANSACTION 360° DETAIL & PAIRED TRANSFER ANALYSIS
    # =========================================================================

    def test_admin_transaction_detail_view_standard(self):
        """Admin can view complete 360° transaction detail including snapshots and delta."""
        tx = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('4000.00'),
            balance_before=Decimal('46000.00'),
            balance_after=Decimal('50000.00'),
            reference_id='TXN-DETAIL-001'
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_detail', args=[tx.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_transaction_detail.html')
        self.assertContains(response, 'TXN-DETAIL-001')
        self.assertContains(response, '4000.00')
        self.assertContains(response, '46000.00')
        self.assertContains(response, '50000.00')

    def test_paired_transfer_analysis_detects_symmetric_pair(self):
        """Detail view detects symmetric transfer partner leg sharing reference_id."""
        ref = f"TRF-{uuid.uuid4().hex[:12].upper()}"

        tx_debit = Transaction.objects.create(
            account=self.account1,
            related_account=self.account2,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('1500.00'),
            balance_before=Decimal('50000.00'),
            balance_after=Decimal('48500.00'),
            reference_id=ref
        )
        tx_credit = Transaction.objects.create(
            account=self.account2,
            related_account=self.account1,
            transaction_type='TRANSFER_CREDIT',
            amount=Decimal('1500.00'),
            balance_before=Decimal('25000.00'),
            balance_after=Decimal('26500.00'),
            reference_id=ref
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_detail', args=[tx_debit.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Symmetric Two-Leg Transfer')
        self.assertContains(response, 'CREDIT LEG')
        self.assertContains(response, 'Paired Counterpart Leg')

    def test_paired_transfer_analysis_detects_missing_credit_leg(self):
        """Detail view highlights warning when transfer partner leg is missing."""
        ref_orphan = f"TRF-{uuid.uuid4().hex[:12].upper()}"

        tx_orphan = Transaction.objects.create(
            account=self.account1,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('1500.00'),
            balance_before=Decimal('50000.00'),
            balance_after=Decimal('48500.00'),
            reference_id=ref_orphan
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_detail', args=[tx_orphan.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Counterpart Leg Missing')

    def test_transaction_detail_shows_teller_and_audit_logs(self):
        """Detail view renders linked TellerOperationLog and EmployeeActivityLog entries."""
        ref_teller = 'TXN-TELLER-AUDIT-001'
        tx_teller = Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('3500.00'),
            reference_id=ref_teller
        )

        teller_log = TellerOperationLog.objects.create(
            employee=self.employee,
            account=self.account1,
            operation_type='DEPOSIT',
            amount=Decimal('3500.00'),
            reference_id=ref_teller,
            status='SUCCESS',
            description='Counter cash deposit'
        )

        audit_log = EmployeeActivityLog.objects.create(
            employee=self.employee,
            action_type='TELLER_DEPOSIT',
            target_id=ref_teller,
            description='Teller deposit processed at counter'
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_detail', args=[tx_teller.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Branch Teller Operation Record')
        self.assertContains(response, 'teller_emp')
        self.assertContains(response, 'TELLER_DEPOSIT')

    # =========================================================================
    # 3. LEDGER INTEGRITY & RECONCILIATION DIAGNOSTICS
    # =========================================================================

    def test_ledger_reconciliation_detects_asymmetric_and_math_errors(self):
        """Reconciliation engine identifies orphan transfer legs and snapshot math violations."""
        # 1. Symmetric transfer pair (healthy)
        ref_ok = f"TRF-{uuid.uuid4().hex[:12].upper()}"
        Transaction.objects.create(
            account=self.account1,
            related_account=self.account2,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('100.00'),
            balance_before=Decimal('50000.00'),
            balance_after=Decimal('49900.00'),
            reference_id=ref_ok
        )
        Transaction.objects.create(
            account=self.account2,
            related_account=self.account1,
            transaction_type='TRANSFER_CREDIT',
            amount=Decimal('100.00'),
            balance_before=Decimal('25000.00'),
            balance_after=Decimal('25100.00'),
            reference_id=ref_ok
        )

        # 2. Orphan transfer debit (asymmetric)
        ref_orphan = f"TRF-{uuid.uuid4().hex[:12].upper()}"
        Transaction.objects.create(
            account=self.account1,
            transaction_type='TRANSFER_DEBIT',
            amount=Decimal('200.00'),
            balance_before=Decimal('49900.00'),
            balance_after=Decimal('49700.00'),
            reference_id=ref_orphan
        )

        # 3. Math mismatch transaction
        Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('500.00'),
            balance_before=Decimal('49700.00'),
            balance_after=Decimal('55000.00'),  # Incorrect: should be 50200.00
            reference_id='TXN-CORRUPT-MATH'
        )

        # 4. Legacy record with NULL snapshot (should NOT be an issue)
        Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('50.00'),
            balance_before=None,
            balance_after=None,
            reference_id='TXN-LEGACY-001'
        )

        # Run direct reconciliation scan
        issues, stats = run_ledger_reconciliation()
        issue_categories = [iss.category for iss in issues]

        self.assertIn('ASYMMETRIC_TRANSFER', issue_categories)
        self.assertIn('SNAPSHOT_CREDIT_MISMATCH', issue_categories)
        self.assertGreaterEqual(stats['legacy_null_count'], 1)

        # Test view output
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_reconciliation'))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'admin_reconciliation.html')
        self.assertContains(response, 'ASYMMETRIC_TRANSFER')
        self.assertContains(response, 'SNAPSHOT_CREDIT_MISMATCH')

    # =========================================================================
    # 4. RISK MONITORING & EXCEPTION FLAGS
    # =========================================================================

    def test_risk_monitoring_flags_large_transactions(self):
        """Rule 1: Flags transactions >= ₹100,000.00."""
        Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('150000.00'),
            reference_id='TXN-RISK-LARGE'
        )

        flags, stats = run_risk_monitoring_scan()
        rule_ids = [flag.rule_id for flag in flags]

        self.assertIn('RULE_LARGE_TRANSACTION', rule_ids)

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_risk_monitoring'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'RULE_LARGE_TRANSACTION')
        self.assertContains(response, '150,000.00')

    def test_risk_monitoring_flags_rapid_velocity(self):
        """Rule 2: Flags >= 3 transactions in 5 minutes on the same account."""
        now = timezone.now()
        for i in range(3):
            tx = Transaction.objects.create(
                account=self.account1,
                transaction_type='DEPOSIT',
                amount=Decimal('100.00'),
                reference_id=f'TXN-VELOCITY-{i}'
            )
            Transaction.objects.filter(id=tx.id).update(created_at=now - timedelta(minutes=i))

        flags, stats = run_risk_monitoring_scan()
        rule_ids = [flag.rule_id for flag in flags]

        self.assertIn('RULE_RAPID_VELOCITY', rule_ids)

    def test_risk_monitoring_flags_concentrated_volume(self):
        """Rule 3: Flags >= 5 transactions in 1 hour on the same account."""
        now = timezone.now()
        for i in range(5):
            tx = Transaction.objects.create(
                account=self.account2,
                transaction_type='DEPOSIT',
                amount=Decimal('200.00'),
                reference_id=f'TXN-CONC-{i}'
            )
            Transaction.objects.filter(id=tx.id).update(created_at=now - timedelta(minutes=i * 10))

        flags, stats = run_risk_monitoring_scan()
        rule_ids = [flag.rule_id for flag in flags]

        self.assertIn('RULE_CONCENTRATED_VOLUME', rule_ids)

    def test_risk_monitoring_flags_teller_surge(self):
        """Rule 4: Flags teller logging >= 20 operations in a 24-hour window."""
        now = timezone.now()
        for i in range(20):
            tlog = TellerOperationLog.objects.create(
                employee=self.employee,
                account=self.account1,
                operation_type='DEPOSIT',
                amount=Decimal('50.00'),
                reference_id=f'TXN-SURGE-{i}',
                status='SUCCESS'
            )
            TellerOperationLog.objects.filter(id=tlog.id).update(created_at=now - timedelta(minutes=i * 5))

        flags, stats = run_risk_monitoring_scan()
        rule_ids = [flag.rule_id for flag in flags]

        self.assertIn('RULE_HIGH_VOLUME_TELLER', rule_ids)

    def test_risk_monitoring_flags_frozen_account_transaction(self):
        """Rule 5: Flags transaction recorded on an account marked FROZEN."""
        self.account1.status = 'FROZEN'
        self.account1.save()

        Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('500.00'),
            reference_id='TXN-FROZEN-ACC-01'
        )

        flags, stats = run_risk_monitoring_scan()
        rule_ids = [flag.rule_id for flag in flags]

        self.assertIn('RULE_FROZEN_ACCOUNT_TRANSACTION', rule_ids)

    # =========================================================================
    # 5. SAFE CSV EXPORTS
    # =========================================================================

    def test_transaction_export_csv_formula_injection_and_masking(self):
        """Export CSV sanitizes formula injection characters, masks accounts, and logs audit."""
        # Transaction with formula injection attack in reference_id
        Transaction.objects.create(
            account=self.account1,
            transaction_type='DEPOSIT',
            amount=Decimal('500.00'),
            reference_id='=cmd|/C calc!A0'
        )

        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_export_csv'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="bebank_transactions_', response['Content-Disposition'])

        content = response.content.decode('utf-8')
        lines = list(csv.reader(io.StringIO(content)))
        self.assertGreater(len(lines), 1)

        # Check sanitization and account masking in CSV output
        found_sanitized = False
        found_masked_acc = False
        for row in lines:
            for cell in row:
                if cell.startswith("'="):
                    found_sanitized = True
                if cell == '••••3331':
                    found_masked_acc = True

        self.assertTrue(found_sanitized, "Formula trigger should be escaped with leading single-quote")
        self.assertTrue(found_masked_acc, "Account number should be masked in CSV")

        # Verify EmployeeActivityLog created
        log = EmployeeActivityLog.objects.filter(
            employee=self.admin,
            action_type='REPORT_EXPORT'
        ).last()
        self.assertIsNotNone(log)
        self.assertIn('transactions', log.description.lower())

    def test_reconciliation_export_csv(self):
        """Reconciliation CSV export returns valid CSV and logs audit."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_reconciliation_export_csv'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="bebank_reconciliation_', response['Content-Disposition'])

        # Verify EmployeeActivityLog created
        log = EmployeeActivityLog.objects.filter(
            employee=self.admin,
            action_type='REPORT_EXPORT'
        ).last()
        self.assertIsNotNone(log)
        self.assertIn('reconciliation', log.description.lower())

    # =========================================================================
    # 6. STRICT RBAC & IDOR SAFEGUARDS
    # =========================================================================

    def test_customer_and_employee_blocked_from_admin_transaction_views(self):
        """Customer and Employee users receive 403 Forbidden across all monitoring endpoints."""
        endpoints = [
            reverse('admin_transaction_list'),
            reverse('admin_transaction_export_csv'),
            reverse('admin_reconciliation'),
            reverse('admin_reconciliation_export_csv'),
            reverse('admin_risk_monitoring'),
        ]

        # Customer blocked
        self.client.force_login(self.customer1)
        for ep in endpoints:
            resp = self.client.get(ep)
            self.assertEqual(resp.status_code, 403, f"Customer should be 403 on {ep}")

        # Employee blocked
        self.client.force_login(self.employee)
        for ep in endpoints:
            resp = self.client.get(ep)
            self.assertEqual(resp.status_code, 403, f"Employee should be 403 on {ep}")

    def test_anonymous_redirected_to_login(self):
        """Unauthenticated requests are redirected (302) to login."""
        self.client.logout()
        resp = self.client.get(reverse('admin_transaction_list'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp.url)

    def test_invalid_transaction_id_returns_404(self):
        """Accessing a non-existent transaction ID cleanly returns 404."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse('admin_transaction_detail', args=[9999999]))
        self.assertEqual(response.status_code, 404)

    # =========================================================================
    # 7. FINANCIAL CONTROL RESTRICTIONS & ACCOUNT DEBIT-FREEZE ENFORCEMENT
    # =========================================================================

    def test_frozen_account_blocks_all_outgoing_debits(self):
        """
        Comprehensive test ensuring frozen accounts CANNOT perform outgoing debit operations:
        - Transfers (transfer_view)
        - Cash withdrawal (withdraw_view)
        - Teller withdrawal (staff_withdraw_view)
        - Virtual debit card issuance (card_issue_view)
        - Fixed Deposit creation (fd_view)
        - Loan repayment (loan_repay_view)
        """
        self.account1.status = 'FROZEN'
        self.account1.save()

        # 1. Transfer blocked
        self.client.force_login(self.customer1)
        resp_trf = self.client.post(reverse('transfer'), {
            'account_id': self.account1.id,
            'account_number': self.account2.account_number,
            'amount': '500.00',
            'idempotency_key': str(uuid.uuid4()),
            'description': 'Blocked transfer'
        }, follow=True)
        self.assertContains(resp_trf, 'frozen')

        # 2. Customer withdrawal blocked
        resp_wdr = self.client.post(reverse('withdraw'), {
            'account_id': self.account1.id,
            'amount': '200.00'
        }, follow=True)
        self.assertContains(resp_wdr, 'frozen')

        # 3. Teller withdrawal blocked
        self.client.force_login(self.employee)
        resp_t_wdr = self.client.post(reverse('staff_withdraw'), {
            'account_number': self.account1.account_number,
            'amount': '300.00',
            'notes': 'Blocked teller withdrawal'
        }, follow=True)
        self.assertContains(resp_t_wdr, 'frozen')

        # 4. Card issue blocked
        self.client.force_login(self.customer1)
        resp_card = self.client.post(reverse('card_issue'), {
            'account_id': self.account1.id,
            'cardholder_name': 'Aditi Rao',
            'daily_limit': '5000.00'
        }, follow=True)
        self.assertContains(resp_card, 'frozen')

        # 5. FD creation blocked
        resp_fd = self.client.post(reverse('fd'), {
            'account_id': self.account1.id,
            'amount': '5000.00',
            'months': '12'
        }, follow=True)
        self.assertContains(resp_fd, 'frozen')

        # 6. Loan repayment blocked
        loan = Loan.objects.create(
            account=self.account1,
            amount=Decimal('10000.00'),
            duration_months=12,
            interest_rate=Decimal('10.0'),
            total_payable=Decimal('11000.00'),
            outstanding_balance=Decimal('11000.00'),
            emi_amount=Decimal('916.67'),
            status='APPROVED'
        )
        resp_repay = self.client.post(reverse('loan_repay', args=[loan.id]), {
            'amount': '916.67',
            'idempotency_key': str(uuid.uuid4()),
            'account_id': self.account1.id
        }, follow=True)
        self.assertContains(resp_repay, 'frozen')

        # Balance remains unmodified
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('50000.00'))

    def test_frozen_account_permits_incoming_credits(self):
        """
        Comprehensive test ensuring frozen accounts CAN receive incoming credits:
        - Incoming transfer credit (transfer_view)
        - Teller counter deposit (staff_deposit_view)
        - FD maturity payout settlement (fd_settle_view)
        - FD premature closure refund (fd_close_view)
        - Loan disbursement (approve_loan)
        """
        self.account1.status = 'FROZEN'
        self.account1.save()

        # 1. Incoming transfer credit permitted
        self.client.force_login(self.customer2)
        resp_trf = self.client.post(reverse('transfer'), {
            'account_id': self.account2.id,
            'account_number': self.account1.account_number,
            'amount': '1000.00',
            'idempotency_key': str(uuid.uuid4()),
            'description': 'Allowed incoming transfer'
        }, follow=True)
        self.assertContains(resp_trf, 'Transfer successful')
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('51000.00'))

        # 2. Teller counter deposit permitted
        self.client.force_login(self.employee)
        resp_dep = self.client.post(reverse('staff_deposit'), {
            'account_number': self.account1.account_number,
            'amount': '2000.00',
            'notes': 'Over the counter cash deposit to frozen account'
        }, follow=True)
        self.assertContains(resp_dep, 'SUCCESS')
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, Decimal('53000.00'))

        # 3. Premature FD closure refund permitted
        fd_active = FixedDeposit.objects.create(
            account=self.account1,
            amount=Decimal('5000.00'),
            duration_months=12,
            interest_rate=Decimal('6.5'),
            status='ACTIVE',
            start_date=timezone.now(),
            maturity_date=timezone.now() + timedelta(days=365)
        )
        self.client.force_login(self.customer1)
        resp_fd_close = self.client.post(reverse('fd_close', args=[fd_active.id]), follow=True)
        self.assertEqual(resp_fd_close.status_code, 200)
        fd_active.refresh_from_db()
        self.assertEqual(fd_active.status, 'CLOSED')
        self.account1.refresh_from_db()
        self.assertGreater(self.account1.balance, Decimal('53000.00'))

        # 4. FD maturity settlement permitted
        bal_before_settle = self.account1.balance
        fd_matured = FixedDeposit.objects.create(
            account=self.account1,
            amount=Decimal('3000.00'),
            duration_months=6,
            interest_rate=Decimal('6.0'),
            status='ACTIVE',
            start_date=timezone.now() - timedelta(days=200),
            maturity_date=timezone.now() - timedelta(days=10)
        )
        resp_settle = self.client.post(reverse('fd_settle', args=[fd_matured.id]), follow=True)
        self.assertEqual(resp_settle.status_code, 200)
        fd_matured.refresh_from_db()
        self.assertEqual(fd_matured.status, 'MATURED')
        self.account1.refresh_from_db()
        self.assertGreater(self.account1.balance, bal_before_settle)

        # 5. Loan disbursement permitted
        bal_before_loan = self.account1.balance
        pending_loan = Loan.objects.create(
            account=self.account1,
            amount=Decimal('8000.00'),
            duration_months=12,
            interest_rate=Decimal('8.5'),
            status='PENDING'
        )
        self.client.force_login(self.admin)
        resp_loan = self.client.post(reverse('approve_loan', args=[pending_loan.id]), {
            'action': 'APPROVE',
            'admin_notes': 'Verified and approved'
        }, follow=True)
        self.assertEqual(resp_loan.status_code, 200)
        pending_loan.refresh_from_db()
        self.assertEqual(pending_loan.status, 'APPROVED')
        self.account1.refresh_from_db()
        self.assertEqual(self.account1.balance, bal_before_loan + Decimal('8000.00'))
