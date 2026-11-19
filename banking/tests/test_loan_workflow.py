import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from django.test import TestCase, Client
from django.utils import timezone
from accounts.models import User
from banking.models import BankAccount, Transaction, Loan, LoanInstallment, LoanRepayment, add_months
from banking.views import get_simulation_loan_interest_rate, calculate_reducing_emi, generate_loan_schedule


class LoanWorkflowCalculationsTest(TestCase):
    """
    Test suite for reducing-balance EMI calculations, simulation rates, and amortization scheduling.
    """

    def test_simulation_rate_brackets(self):
        """Verify tier rates match the simulation schedule."""
        self.assertEqual(get_simulation_loan_interest_rate(6), Decimal('9.50'))
        self.assertEqual(get_simulation_loan_interest_rate(12), Decimal('9.50'))
        self.assertEqual(get_simulation_loan_interest_rate(13), Decimal('10.50'))
        self.assertEqual(get_simulation_loan_interest_rate(36), Decimal('10.50'))
        self.assertEqual(get_simulation_loan_interest_rate(37), Decimal('11.50'))
        self.assertEqual(get_simulation_loan_interest_rate(60), Decimal('11.50'))
        self.assertEqual(get_simulation_loan_interest_rate(61), Decimal('12.00'))
        self.assertEqual(get_simulation_loan_interest_rate(120), Decimal('12.00'))
        self.assertEqual(get_simulation_loan_interest_rate(0), Decimal('10.50'))
        self.assertEqual(get_simulation_loan_interest_rate("invalid"), Decimal('10.50'))

    def test_reducing_emi_formula_and_zero_interest(self):
        """Verify reducing-balance EMI calculation and zero-interest fallback."""
        # Principal 1,00,000, 10.50% p.a., 12 months
        emi = calculate_reducing_emi(Decimal('100000.00'), Decimal('10.50'), 12)
        self.assertEqual(emi, Decimal('8814.86'))

        # Zero interest: EMI = P / n
        zero_emi = calculate_reducing_emi(Decimal('60000.00'), Decimal('0.00'), 12)
        self.assertEqual(zero_emi, Decimal('5000.00'))

        # Zero tenure
        zero_tenure = calculate_reducing_emi(Decimal('10000.00'), Decimal('10.50'), 0)
        self.assertEqual(zero_tenure, Decimal('0.00'))

    def test_amortization_schedule_closure_and_rounding(self):
        """Verify that last installment absorbs rounding and sum of principal equals exact principal."""
        customer = User.objects.create_user(username='math_loan_user', password='password123', role='CUSTOMER')
        account = BankAccount.objects.create(user=customer, account_type='SAVINGS', balance=Decimal('5000.00'))

        loan = Loan.objects.create(
            account=account,
            amount=Decimal('100000.00'),
            duration_months=12,
            interest_rate=Decimal('10.50'),
            status='APPROVED',
            disbursed_at=timezone.now()
        )

        generate_loan_schedule(loan)
        loan.refresh_from_db()

        installments = list(loan.installments.all().order_by('installment_number'))
        self.assertEqual(len(installments), 12)

        # Principal parts must sum exactly to loan principal (no orphan pennies)
        total_principal_paid = sum(inst.principal_component for inst in installments)
        self.assertEqual(total_principal_paid, Decimal('100000.00'))

        # Total payable equals sum of installment emi_due
        total_due_sum = sum(inst.emi_due for inst in installments)
        self.assertEqual(loan.total_payable, total_due_sum)
        self.assertEqual(loan.outstanding_balance, total_due_sum)

        # Verify monthly calendar progression
        for i in range(1, 12):
            self.assertGreater(installments[i].due_date, installments[i - 1].due_date)


class LoanApplicationAndCustomerWorkspaceTest(TestCase):
    """
    Test suite for customer loan application, dashboard isolation, and input validation.
    """

    def setUp(self):
        self.client = Client()
        self.customer1 = User.objects.create_user(username='loan_alice', password='password123', role='CUSTOMER')
        self.customer2 = User.objects.create_user(username='loan_bob', password='password123', role='CUSTOMER')
        self.acc1 = BankAccount.objects.create(user=self.customer1, account_type='SAVINGS', balance=Decimal('1000.00'))
        self.acc2 = BankAccount.objects.create(user=self.customer2, account_type='SAVINGS', balance=Decimal('2000.00'))

    def test_customer_can_view_loan_page(self):
        """Customer can access the loan management and application workspace."""
        self.client.force_login(self.customer1)
        response = self.client.get('/bank/loan/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Customer Loans & EMI')
        self.assertContains(response, 'Apply for a Personal Loan')

    def test_successful_loan_application(self):
        """Valid loan application creates a PENDING loan record."""
        self.client.force_login(self.customer1)
        response = self.client.post('/bank/loan/', {
            'account_id': self.acc1.id,
            'amount': '50000.00',
            'months': '24'
        })
        self.assertEqual(response.status_code, 302)
        loan = Loan.objects.filter(account=self.acc1).first()
        self.assertIsNotNone(loan)
        self.assertEqual(loan.amount, Decimal('50000.00'))
        self.assertEqual(loan.duration_months, 24)
        self.assertEqual(loan.interest_rate, Decimal('10.50'))
        self.assertEqual(loan.status, 'PENDING')
        self.assertIsNotNone(loan.emi_amount)

    def test_application_with_invalid_amount_or_duration(self):
        """Reject non-positive amounts, amounts exceeding ₹10,00,000, and durations outside 6-120 months."""
        self.client.force_login(self.customer1)

        # Amount non-positive
        res_low = self.client.post('/bank/loan/', {'account_id': self.acc1.id, 'amount': '0.00', 'months': '12'})
        self.assertEqual(res_low.status_code, 302)
        self.assertFalse(Loan.objects.filter(amount=Decimal('0.00')).exists())

        # Amount too high
        res_high = self.client.post('/bank/loan/', {'account_id': self.acc1.id, 'amount': '1500000.00', 'months': '12'})
        self.assertEqual(res_high.status_code, 302)
        self.assertFalse(Loan.objects.filter(amount=Decimal('1500000.00')).exists())

        # Duration too short
        res_short = self.client.post('/bank/loan/', {'account_id': self.acc1.id, 'amount': '10000.00', 'months': '3'})
        self.assertEqual(res_short.status_code, 302)
        self.assertFalse(Loan.objects.filter(duration_months=3).exists())

        # Duration too long
        res_long = self.client.post('/bank/loan/', {'account_id': self.acc1.id, 'amount': '10000.00', 'months': '150'})
        self.assertEqual(res_long.status_code, 302)
        self.assertFalse(Loan.objects.filter(duration_months=150).exists())

    def test_customer_cannot_apply_using_another_customers_account(self):
        """IDOR protection: customer cannot apply using another user's account ID (HTTP 404)."""
        self.client.force_login(self.customer1)
        response = self.client.post('/bank/loan/', {
            'account_id': self.acc2.id,  # Owned by customer2
            'amount': '25000.00',
            'months': '12'
        })
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Loan.objects.filter(account=self.acc2).exists())

    def test_customer_isolation_on_loan_list(self):
        """Customer sees only their own loans and applications."""
        loan1 = Loan.objects.create(account=self.acc1, amount=Decimal('20000.00'), duration_months=12)
        loan2 = Loan.objects.create(account=self.acc2, amount=Decimal('40000.00'), duration_months=12)

        self.client.force_login(self.customer1)
        response = self.client.get('/bank/loan/')
        self.assertContains(response, f'#{loan1.id}')
        self.assertNotContains(response, f'#{loan2.id}')

    def test_customer_cannot_view_another_customers_loan_detail(self):
        """Customer cannot access detail view of another customer's loan (HTTP 404)."""
        loan2 = Loan.objects.create(account=self.acc2, amount=Decimal('30000.00'), duration_months=12)

        self.client.force_login(self.customer1)
        response = self.client.get(f'/bank/loan/{loan2.id}/')
        self.assertEqual(response.status_code, 404)


class LoanDisbursementAndApprovalTest(TestCase):
    """
    Test suite for administrative loan approval, disbursement, and ledger snapshot tracking.
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(username='loan_borrower', password='password123', role='CUSTOMER')
        self.admin = User.objects.create_superuser(username='loan_admin', password='password123', role='ADMIN')
        self.account = BankAccount.objects.create(user=self.customer, account_type='SAVINGS', balance=Decimal('500.00'))
        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('10000.00'),
            duration_months=12,
            interest_rate=Decimal('9.50'),
            status='PENDING'
        )

    def test_admin_approval_disburses_funds_and_creates_schedule(self):
        """Approving a loan credits account, creates ledger transaction with snapshots, and generates schedule."""
        self.client.force_login(self.admin)
        response = self.client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')
        self.assertIsNotNone(self.loan.disbursed_at)
        self.assertIsNotNone(self.loan.outstanding_balance)
        self.assertGreater(self.loan.outstanding_balance, Decimal('10000.00'))

        # Balance credit
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('10500.00'))

        # Transaction entry with balance snapshots
        tx = Transaction.objects.filter(
            account=self.account,
            transaction_type='LOAN_CREDIT',
            reference_id=f'LOAN-DISB-{self.loan.id}'
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.amount, Decimal('10000.00'))
        self.assertEqual(tx.balance_before, Decimal('500.00'))
        self.assertEqual(tx.balance_after, Decimal('10500.00'))

        # Amortization installments generated
        installments = self.loan.installments.all()
        self.assertEqual(installments.count(), 12)

    def test_cannot_approve_already_approved_loan(self):
        """Prevent duplicate disbursement by rejecting repeated approval attempts."""
        self.loan.status = 'APPROVED'
        self.loan.save(update_fields=['status'])

        self.client.force_login(self.admin)
        response = self.client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 302)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('500.00'))  # No double credit

    def test_customer_cannot_approve_loan(self):
        """RBAC: customer cannot approve loans (HTTP 403 Forbidden)."""
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 403)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'PENDING')


class LoanRepaymentWorkflowTest(TestCase):
    """
    Test suite for customer loan repayment, installment allocation, and loan closure.
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(username='repay_user', password='password123', role='CUSTOMER')
        self.other_customer = User.objects.create_user(username='other_user', password='password123', role='CUSTOMER')

        # Customer account with balance to pay installments
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_type='SAVINGS',
            balance=Decimal('20000.00')
        )
        self.other_account = BankAccount.objects.create(
            user=self.other_customer,
            account_type='SAVINGS',
            balance=Decimal('20000.00')
        )

        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('10000.00'),
            duration_months=6,
            interest_rate=Decimal('9.50'),
            status='APPROVED',
            disbursed_at=timezone.now()
        )
        generate_loan_schedule(self.loan)
        self.loan.refresh_from_db()

    def test_single_emi_repayment_success(self):
        """Paying one exact EMI debits customer account, records transaction, and marks first installment PAID."""
        first_inst = self.loan.installments.get(installment_number=1)
        emi_amount = first_inst.emi_due
        bal_before = self.account.balance
        key = str(uuid.uuid4())

        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(emi_amount),
            'idempotency_key': key,
        })
        self.assertEqual(response.status_code, 302)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, bal_before - emi_amount)

        # First installment marked PAID
        first_inst.refresh_from_db()
        self.assertEqual(first_inst.status, 'PAID')
        self.assertEqual(first_inst.amount_paid, emi_amount)
        self.assertIsNotNone(first_inst.paid_at)

        # Loan outstanding balance decreased
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.outstanding_balance, self.loan.total_payable - emi_amount)

        # Repayment record and ledger transaction exist
        repayment = LoanRepayment.objects.filter(loan=self.loan).first()
        self.assertIsNotNone(repayment)
        self.assertEqual(repayment.amount, emi_amount)

        tx = Transaction.objects.filter(
            account=self.account,
            transaction_type='LOAN_REPAYMENT_DEBIT',
            reference_id=repayment.reference_id
        ).first()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.balance_before, bal_before)
        self.assertEqual(tx.balance_after, bal_before - emi_amount)

    def test_partial_installment_repayment(self):
        """Paying less than the full installment marks installment PARTIALLY_PAID."""
        partial_amount = Decimal('100.00')
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(partial_amount),
            'idempotency_key': str(uuid.uuid4()),
        })
        self.assertEqual(response.status_code, 302)

        first_inst = self.loan.installments.get(installment_number=1)
        self.assertEqual(first_inst.status, 'PARTIALLY_PAID')
        self.assertEqual(first_inst.amount_paid, partial_amount)

    def test_overpayment_beyond_outstanding_balance_rejected(self):
        """Reject payment exceeding the loan's total outstanding balance."""
        excessive = self.loan.outstanding_balance + Decimal('500.00')
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(excessive),
            'idempotency_key': str(uuid.uuid4()),
        })
        self.assertEqual(response.status_code, 302)

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('20000.00'))  # No balance deducted

    def test_repayment_with_insufficient_funds_rejected(self):
        """Reject repayment if selected account does not have sufficient funds."""
        self.account.balance = Decimal('50.00')
        self.account.save(update_fields=['balance'])

        first_inst = self.loan.installments.get(installment_number=1)
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(first_inst.emi_due),
            'idempotency_key': str(uuid.uuid4()),
        })
        self.assertEqual(response.status_code, 302)

        first_inst.refresh_from_db()
        self.assertEqual(first_inst.status, 'PENDING')
        self.assertEqual(self.account.balance, Decimal('50.00'))

    def test_repayment_using_another_customers_account_rejected(self):
        """IDOR protection: customer cannot debit another customer's bank account for repayment (HTTP 404)."""
        first_inst = self.loan.installments.get(installment_number=1)
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.other_account.id,  # Belongs to other_customer
            'amount': str(first_inst.emi_due),
            'idempotency_key': str(uuid.uuid4()),
        })
        self.assertEqual(response.status_code, 404)

        self.other_account.refresh_from_db()
        self.assertEqual(self.other_account.balance, Decimal('20000.00'))  # Not debited

    def test_full_repayment_closes_loan(self):
        """Paying the entire outstanding balance settles all installments and marks loan CLOSED."""
        full_amount = self.loan.outstanding_balance
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(full_amount),
            'idempotency_key': str(uuid.uuid4()),
        })
        self.assertEqual(response.status_code, 302)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'CLOSED')
        self.assertEqual(self.loan.outstanding_balance, Decimal('0.00'))
        self.assertIsNotNone(self.loan.closed_at)

        # All installments are now PAID
        for inst in self.loan.installments.all():
            self.assertEqual(inst.status, 'PAID')
            self.assertEqual(inst.amount_paid, inst.emi_due)

    def test_repayment_requires_idempotency_key(self):
        """Reject repayment if idempotency_key is omitted."""
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': '100.00',
        })
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('20000.00'))

    def test_repayment_rejects_malformed_idempotency_key(self):
        """Reject repayment if idempotency_key is not a valid UUID."""
        self.client.force_login(self.customer)
        response = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': '100.00',
            'idempotency_key': 'not-a-valid-uuid',
        })
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('20000.00'))

    def test_duplicate_repayment_retry_idempotency(self):
        """Identical retry with same idempotency key does not debit twice."""
        key = str(uuid.uuid4())
        first_inst = self.loan.installments.get(installment_number=1)
        emi_amount = first_inst.emi_due
        bal_before = self.account.balance

        self.client.force_login(self.customer)
        # First request
        res1 = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(emi_amount),
            'idempotency_key': key,
        })
        self.assertEqual(res1.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, bal_before - emi_amount)

        # Second request with SAME key and details
        res2 = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(emi_amount),
            'idempotency_key': key,
        })
        self.assertEqual(res2.status_code, 302)
        self.account.refresh_from_db()
        # Balance must NOT be debited twice
        self.assertEqual(self.account.balance, bal_before - emi_amount)
        # Only one LoanRepayment record should exist for this key
        self.assertEqual(LoanRepayment.objects.filter(reference_id=f"LOAN-REPAY-{uuid.UUID(key).hex.upper()}").count(), 1)

    def test_reused_idempotency_key_with_altered_details_rejected(self):
        """Reusing an idempotency key with different payment details is rejected."""
        key = str(uuid.uuid4())
        first_inst = self.loan.installments.get(installment_number=1)
        emi_amount = first_inst.emi_due

        self.client.force_login(self.customer)
        # First request
        res1 = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': str(emi_amount),
            'idempotency_key': key,
        })
        self.assertEqual(res1.status_code, 302)

        # Second request with same key but ALTERED amount
        res2 = self.client.post(f'/bank/loan/{self.loan.id}/repay/', {
            'account_id': self.account.id,
            'amount': '50.00',
            'idempotency_key': key,
        })
        self.assertEqual(res2.status_code, 302)
        # Only 1 repayment in database
        self.assertEqual(LoanRepayment.objects.filter(loan=self.loan).count(), 1)
