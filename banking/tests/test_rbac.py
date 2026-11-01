from decimal import Decimal
from django.test import TestCase, Client, RequestFactory
from django.core.exceptions import PermissionDenied
from accounts.models import User
from banking.models import BankAccount, Transaction, FixedDeposit, Loan
from banking.views import approve_loan, reject_loan


class BankingAnonymousAccessTests(TestCase):
    """
    Ensure anonymous users are intercepted by authentication decorators on all banking views
    and redirected to the login page with their target destination preserved.
    """

    def setUp(self):
        self.client = Client()

    def test_anonymous_cannot_access_banking_endpoints(self):
        endpoints = [
            ('/bank/withdraw/', '/login/?next=/bank/withdraw/'),
            ('/bank/transfer/', '/login/?next=/bank/transfer/'),
            ('/bank/history/', '/login/?next=/bank/history/'),
            ('/bank/passbook/', '/login/?next=/bank/passbook/'),
            ('/bank/history/pdf/', '/login/?next=/bank/history/pdf/'),
            ('/bank/fd/', '/login/?next=/bank/fd/'),
            ('/bank/loan/', '/login/?next=/bank/loan/'),
            ('/bank/create-account/', '/login/?next=/bank/create-account/'),
            ('/bank/deposit/', '/login/?next=/bank/deposit/'),
            ('/bank/analytics/', '/login/?next=/bank/analytics/'),
        ]
        for url, expected_redirect in endpoints:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(expected_redirect, response.url)


class BankingStaffVsCustomerRoleTests(TestCase):
    """
    Ensure strict RBAC boundaries between CUSTOMER roles and STAFF (EMPLOYEE, ADMIN) roles.
    """

    def setUp(self):
        self.client = Client()
        self.factory = RequestFactory()

        self.customer = User.objects.create_user(
            username='cust_role', password='pwd', role='CUSTOMER', is_staff=False
        )
        self.employee = User.objects.create_user(
            username='emp_role', password='pwd', role='EMPLOYEE', is_staff=False
        )
        self.admin = User.objects.create_superuser(
            username='adm_role', password='pwd', role='ADMIN', is_staff=True
        )

        self.account = BankAccount.objects.create(
            user=self.customer, account_number='11001', account_type='SAVINGS', balance=1000
        )
        self.loan = Loan.objects.create(
            account=self.account, amount=500, duration_months=6, status='PENDING'
        )

    def test_customer_cannot_access_staff_endpoints(self):
        """CUSTOMER role must be denied access (HTTP 403) from staff actions."""
        self.client.force_login(self.customer)
        for url in ['/bank/create-account/', '/bank/deposit/', '/bank/analytics/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_employee_cannot_access_customer_endpoints(self):
        """EMPLOYEE role must be denied access (HTTP 403) from customer-only actions."""
        self.client.force_login(self.employee)
        customer_urls = [
            '/bank/withdraw/',
            '/bank/transfer/',
            '/bank/history/',
            '/bank/passbook/',
            '/bank/fd/',
            '/bank/loan/',
        ]
        for url in customer_urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 403)

    def test_employee_can_access_staff_endpoints(self):
        """EMPLOYEE role can access staff operational endpoints."""
        self.client.force_login(self.employee)
        for url in ['/bank/create-account/', '/bank/deposit/', '/bank/analytics/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_admin_can_access_staff_endpoints(self):
        """ADMIN role can access staff operational endpoints."""
        self.client.force_login(self.admin)
        for url in ['/bank/create-account/', '/bank/deposit/', '/bank/analytics/']:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)

    def test_customer_and_employee_cannot_approve_or_reject_loans(self):
        """Non-admin roles must be blocked (HTTP 403) from approving or rejecting loans."""
        for user in [self.customer, self.employee]:
            req_approve = self.factory.get(f'/approve-loan/{self.loan.id}/')
            req_approve.user = user
            with self.assertRaises(PermissionDenied):
                approve_loan(req_approve, self.loan.id)

            req_reject = self.factory.get(f'/reject-loan/{self.loan.id}/')
            req_reject.user = user
            with self.assertRaises(PermissionDenied):
                reject_loan(req_reject, self.loan.id)


class ObjectLevelOwnershipIdorTests(TestCase):
    """
    Critical object-level authorization & IDOR tests: Customer A vs Customer B.
    Ensure no customer can access, manipulate, or expose another customer's resources.
    """

    def setUp(self):
        self.client = Client()

        # Customer A resources
        self.customer_a = User.objects.create_user(
            username='cust_a', password='pwd', role='CUSTOMER', is_staff=False
        )
        self.account_a = BankAccount.objects.create(
            user=self.customer_a, account_number='20001', account_type='SAVINGS', balance=Decimal('1000.00'),
            email='a@bebank.local', phone='9876543210', aadhaar='123412341234', pan='ABCDE1234F',
            address='123 Baker Street', pincode='560001'
        )
        self.loan_a = Loan.objects.create(
            account=self.account_a, amount=Decimal('500.00'), duration_months=12, status='PENDING'
        )
        self.fd_a = FixedDeposit.objects.create(
            account=self.account_a, amount=Decimal('300.00'), duration_months=6
        )
        self.tx_a = Transaction.objects.create(
            account=self.account_a, transaction_type='DEPOSIT', amount=Decimal('1000.00')
        )

        # Customer B resources (Target of IDOR attack)
        self.customer_b = User.objects.create_user(
            username='cust_b', password='pwd', role='CUSTOMER', is_staff=False
        )
        self.account_b = BankAccount.objects.create(
            user=self.customer_b, account_number='20002', account_type='CURRENT', balance=Decimal('2000.00'),
            email='b@bebank.local', phone='9876543211', aadhaar='567856785678', pan='VWXYZ5678K',
            address='456 Market Road', pincode='560002'
        )
        self.loan_b = Loan.objects.create(
            account=self.account_b, amount=Decimal('800.00'), duration_months=24, status='PENDING'
        )
        self.fd_b = FixedDeposit.objects.create(
            account=self.account_b, amount=Decimal('600.00'), duration_months=12
        )
        self.tx_b = Transaction.objects.create(
            account=self.account_b, transaction_type='DEPOSIT', amount=Decimal('2000.00')
        )

    def test_customer_cannot_view_another_customers_account_by_id(self):
        """Customer A cannot view Customer B's account details by changing account ID parameter."""
        self.client.force_login(self.customer_a)

        # Via URL path parameter
        resp_path = self.client.get(f'/customer/dashboard/{self.account_b.id}/')
        self.assertEqual(resp_path.status_code, 404)

        # Via query parameter
        resp_query = self.client.get(f'/customer/dashboard/?account_id={self.account_b.id}')
        self.assertEqual(resp_query.status_code, 404)

        # Authorized view shows only Customer A's data
        resp_own = self.client.get('/customer/dashboard/')
        self.assertEqual(resp_own.status_code, 200)
        self.assertContains(resp_own, '20001')
        self.assertNotContains(resp_own, '20002')
        self.assertNotContains(resp_own, 'b@bebank.local')

    def test_customer_cannot_see_another_customers_transactions_or_history(self):
        """Customer A cannot see Customer B's transactions or query Customer B's transaction history."""
        self.client.force_login(self.customer_a)

        # Default history must only contain Customer A's transactions
        resp_history = self.client.get('/bank/history/')
        self.assertEqual(resp_history.status_code, 200)
        self.assertNotContains(resp_history, '20002')
        self.assertNotContains(resp_history, '₹2000.00')

        # Attempt to target Customer B's account via path or query
        resp_path = self.client.get(f'/bank/history/{self.account_b.id}/')
        self.assertEqual(resp_path.status_code, 404)

        resp_query = self.client.get(f'/bank/history/?account_id={self.account_b.id}')
        self.assertEqual(resp_query.status_code, 404)

    def test_customer_cannot_download_another_customers_passbook_or_statement_pdf(self):
        """Customer A cannot generate or download statement PDF for Customer B's account."""
        self.client.force_login(self.customer_a)

        # Path parameter attack
        resp_pdf_path1 = self.client.get(f'/bank/passbook/{self.account_b.id}/')
        self.assertEqual(resp_pdf_path1.status_code, 404)

        resp_pdf_path2 = self.client.get(f'/bank/history/pdf/{self.account_b.id}/')
        self.assertEqual(resp_pdf_path2.status_code, 404)

        # Query parameter attack
        resp_pdf_query1 = self.client.get(f'/bank/passbook/?account_id={self.account_b.id}')
        self.assertEqual(resp_pdf_query1.status_code, 404)

        resp_pdf_query2 = self.client.get(f'/bank/history/pdf/?account_id={self.account_b.id}')
        self.assertEqual(resp_pdf_query2.status_code, 404)

        # Legitimate own passbook download succeeds
        resp_pdf_own = self.client.get('/bank/passbook/')
        self.assertEqual(resp_pdf_own.status_code, 200)
        self.assertEqual(resp_pdf_own['Content-Type'], 'application/pdf')

    def test_customer_cannot_withdraw_from_another_customers_account(self):
        """Customer A cannot withdraw funds from Customer B's account by passing account_id in POST."""
        self.client.force_login(self.customer_a)
        response = self.client.post('/bank/withdraw/', data={
            'account_id': self.account_b.id,
            'amount': '250.00'
        })
        self.assertEqual(response.status_code, 404)
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.balance, Decimal('2000.00'), "Customer B's balance must not be decremented.")

    def test_customer_cannot_transfer_from_another_customers_source_account(self):
        """Customer A cannot use Customer B's account as source in transfer operation."""
        self.client.force_login(self.customer_a)

        # Attack with 'account_id'
        resp1 = self.client.post('/bank/transfer/', data={
            'account_id': self.account_b.id,
            'account_number': self.account_a.account_number,
            'amount': '300.00'
        })
        self.assertEqual(resp1.status_code, 404)

        # Attack with 'source_account_id'
        resp2 = self.client.post('/bank/transfer/', data={
            'source_account_id': self.account_b.id,
            'account_number': self.account_a.account_number,
            'amount': '300.00'
        })
        self.assertEqual(resp2.status_code, 404)

        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.balance, Decimal('2000.00'), "Customer B's balance must remain untouched.")

    def test_customer_cannot_book_fd_on_another_customers_account(self):
        """Customer A cannot charge or bind a Fixed Deposit to Customer B's account."""
        self.client.force_login(self.customer_a)
        response = self.client.post('/bank/fd/', data={
            'account_id': self.account_b.id,
            'amount': '200.00',
            'months': '6'
        })
        self.assertEqual(response.status_code, 404)
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_b.balance, Decimal('2000.00'))
        self.assertEqual(FixedDeposit.objects.filter(account=self.account_b).count(), 1)

    def test_customer_cannot_apply_loan_on_another_customers_account(self):
        """Customer A cannot create a loan attached to Customer B's account."""
        self.client.force_login(self.customer_a)
        response = self.client.post('/bank/loan/', data={
            'account_id': self.account_b.id,
            'amount': '500.00',
            'months': '12'
        })
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Loan.objects.filter(account=self.account_b).count(), 1)

    def test_customer_cannot_access_another_customers_loan_details(self):
        """Customer A cannot query or view loan details belonging to Customer B."""
        self.client.force_login(self.customer_a)

        # Path parameter
        resp_path = self.client.get(f'/bank/loan/{self.loan_b.id}/')
        self.assertEqual(resp_path.status_code, 404)

        # Query parameter
        resp_query = self.client.get(f'/bank/loan/?loan_id={self.loan_b.id}')
        self.assertEqual(resp_query.status_code, 404)

        # Customer A's own loan page works
        resp_own = self.client.get('/bank/loan/')
        self.assertEqual(resp_own.status_code, 200)

    def test_customer_cannot_access_another_customers_fixed_deposit_details(self):
        """Customer A cannot query or view fixed deposit details belonging to Customer B."""
        self.client.force_login(self.customer_a)

        # Path parameter
        resp_path = self.client.get(f'/bank/fd/{self.fd_b.id}/')
        self.assertEqual(resp_path.status_code, 404)

        # Query parameter
        resp_query = self.client.get(f'/bank/fd/?fd_id={self.fd_b.id}')
        self.assertEqual(resp_query.status_code, 404)

        # Customer A's own FD page works
        resp_own = self.client.get('/bank/fd/')
        self.assertEqual(resp_own.status_code, 200)

    def test_customer_can_legitimately_transact_on_own_account(self):
        """Ensure Customer A can perform legitimate banking operations on their own account."""
        self.client.force_login(self.customer_a)

        # Legitimate withdrawal
        resp_w = self.client.post('/bank/withdraw/', data={'amount': '100.00'})
        self.assertEqual(resp_w.status_code, 302)
        self.account_a.refresh_from_db()
        self.assertEqual(self.account_a.balance, Decimal('900.00'))

        # Legitimate transfer to Customer B
        resp_t = self.client.post('/bank/transfer/', data={
            'account_number': self.account_b.account_number,
            'amount': '150.00'
        })
        self.assertEqual(resp_t.status_code, 302)
        self.account_a.refresh_from_db()
        self.account_b.refresh_from_db()
        self.assertEqual(self.account_a.balance, Decimal('750.00'))
        self.assertEqual(self.account_b.balance, Decimal('2150.00'))

        # Legitimate FD booking
        resp_fd = self.client.post('/bank/fd/', data={
            'amount': '200.00',
            'months': '6'
        })
        self.assertEqual(resp_fd.status_code, 302)
        self.account_a.refresh_from_db()
        self.assertEqual(self.account_a.balance, Decimal('550.00'))
        self.assertEqual(FixedDeposit.objects.filter(account=self.account_a).count(), 2)

        # Legitimate loan application
        resp_l = self.client.post('/bank/loan/', data={
            'amount': '300.00',
            'months': '12'
        })
        self.assertEqual(resp_l.status_code, 302)
        self.assertEqual(Loan.objects.filter(account=self.account_a).count(), 2)
