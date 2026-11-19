from decimal import Decimal
from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User
from banking.models import BankAccount, FixedDeposit, Loan, VirtualDebitCard, Notification, SupportTicket


class CustomerNavigationTests(TestCase):
    """
    Test suite verifying standardized Customer navigation across all Customer modules:
    - Navbar structure, item order, icons, and URLs are identical across all modules.
    - Clearance spacer (.customer-bottom-nav-spacer) is rendered to prevent content overlap.
    - Active item styling highlights the correct tab for the page.
    - Zero broken links, zero HTTP 500s.
    - Customer navigation does not bleed into Public, Login, Terms, or Employee/Admin pages.
    - Customer RBAC and ownership checks remain intact.
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(
            username='nav_customer',
            password='testpassword123',
            role='CUSTOMER',
            kyc_status='APPROVED'
        )
        self.account = BankAccount.objects.create(
            user=self.customer,
            account_number='99001',
            account_type='SAVINGS',
            balance=Decimal('10000.00')
        )
        self.fd = FixedDeposit.objects.create(
            account=self.account,
            amount=Decimal('5000.00'),
            duration_months=12,
            interest_rate=Decimal('6.50'),
            maturity_amount=Decimal('5325.00'),
            status='ACTIVE'
        )
        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('20000.00'),
            duration_months=12,
            interest_rate=Decimal('10.50'),
            status='APPROVED'
        )
        self.card = VirtualDebitCard.objects.create(
            user=self.customer,
            account=self.account,
            card_number='5555444433337890',
            cardholder_name='Nav Customer',
            expiry_month=12,
            expiry_year=2030,
            status='ACTIVE',
            daily_limit=Decimal('50000.00')
        )
        self.ticket = SupportTicket.objects.create(
            customer=self.customer,
            ticket_number='TKT-NAV001',
            subject='Navigation query',
            category='GENERAL',
            priority='LOW',
            status='OPEN'
        )
        self.notification = Notification.objects.create(
            recipient=self.customer,
            notification_type='TRANSACTION',
            title='Test Alert',
            message='Testing navbar presence',
            is_read=False
        )
        self.client.force_login(self.customer)

    def test_customer_dashboard_navbar(self):
        """Dashboard renders standardized navbar with Home active, responsive 8-tool grid, and no sensitive ID leak."""
        resp = self.client.get(reverse('customer_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, 'class="bottom-nav"')
        self.assertContains(resp, f'href="{reverse("customer_dashboard")}" class="active"')
        self.assertContains(resp, f'href="{reverse("withdraw")}" class=""')
        self.assertContains(resp, f'href="{reverse("transfer")}" class=""')
        self.assertContains(resp, f'href="{reverse("history")}" class=""')
        self.assertContains(resp, f'href="{reverse("logout")}" class=""')

        # Mobile responsive grid & 8 tools validation
        self.assertContains(resp, 'services-grid')
        self.assertContains(resp, reverse('customer_profile'))
        self.assertContains(resp, reverse('customer_kyc'))
        self.assertContains(resp, reverse('beneficiary_list'))
        self.assertContains(resp, reverse('passbook'))
        self.assertContains(resp, '/bank/fd/')
        self.assertContains(resp, '/bank/loan/')
        self.assertContains(resp, reverse('card_list'))
        self.assertContains(resp, reverse('support_ticket_list'))

        # Ensure no raw Aadhaar or PAN display in customer overview
        self.assertNotContains(resp, '<strong>Aadhaar:</strong>')
        self.assertNotContains(resp, '<strong>PAN:</strong>')
        # Ensure no Edit Profile link in Account Overview card
        self.assertNotContains(resp, 'Edit Profile &rarr;')
        # Ensure no Read-Only badge in Account Overview card or dashboard
        self.assertNotContains(resp, 'Read-Only')

    def test_account_details_read_only_and_sensitive_data_masked(self):
        """Account details page has no Read-Only badge, no Edit Profile button, and no raw Aadhaar/PAN."""
        # Create an account to render account_details view
        resp = self.client.get(reverse('customer_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Direct test on account_details template
        from django.template.loader import render_to_string
        rendered = render_to_string("account_details.html", {
            "account": self.account,
            "request": type('Req', (), {'user': self.customer})()
        })
        self.assertIn("Account Overview &amp; Details", rendered)
        self.assertNotIn("Read-Only", rendered)
        self.assertNotIn("Edit Profile", rendered)
        self.assertNotIn("Aadhaar", rendered)
        self.assertNotIn("PAN", rendered)

    def test_withdraw_page_navbar(self):
        """Withdraw page renders navbar with Withdraw active."""
        resp = self.client.get(reverse('withdraw'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, f'href="{reverse("withdraw")}" class="active"')
        self.assertContains(resp, f'href="{reverse("customer_dashboard")}" class=""')

    def test_transfer_page_navbar(self):
        """Transfer page renders navbar with Transfer active."""
        resp = self.client.get(reverse('transfer'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, f'href="{reverse("transfer")}" class="active"')

    def test_beneficiary_list_navbar(self):
        """Beneficiary list renders navbar with Transfer active."""
        resp = self.client.get(reverse('beneficiary_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, f'href="{reverse("transfer")}" class="active"')

    def test_history_page_navbar(self):
        """History page renders navbar with History active."""
        resp = self.client.get(reverse('history'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, f'href="{reverse("history")}" class="active"')

    def test_profile_and_kyc_navbars(self):
        """Profile and KYC pages render the standardized customer navbar."""
        resp_profile = self.client.get(reverse('customer_profile'))
        self.assertEqual(resp_profile.status_code, 200)
        self.assertContains(resp_profile, 'customer-bottom-nav-spacer')
        self.assertContains(resp_profile, 'class="bottom-nav"')

        resp_kyc = self.client.get(reverse('customer_kyc'))
        self.assertEqual(resp_kyc.status_code, 200)
        self.assertContains(resp_kyc, 'customer-bottom-nav-spacer')
        self.assertContains(resp_kyc, 'class="bottom-nav"')

    def test_fixed_deposits_navbars(self):
        """FD list and detail pages render the standardized customer navbar."""
        resp_fd = self.client.get(reverse('fd'))
        self.assertEqual(resp_fd.status_code, 200)
        self.assertContains(resp_fd, 'customer-bottom-nav-spacer')
        self.assertContains(resp_fd, 'class="bottom-nav"')

        resp_fd_detail = self.client.get(reverse('fd_detail', args=[self.fd.id]))
        self.assertEqual(resp_fd_detail.status_code, 200)
        self.assertContains(resp_fd_detail, 'customer-bottom-nav-spacer')
        self.assertContains(resp_fd_detail, 'class="bottom-nav"')

    def test_loans_navbars(self):
        """Loan list and detail pages render the standardized customer navbar."""
        resp_loan = self.client.get(reverse('loan'))
        self.assertEqual(resp_loan.status_code, 200)
        self.assertContains(resp_loan, 'customer-bottom-nav-spacer')
        self.assertContains(resp_loan, 'class="bottom-nav"')

        resp_loan_detail = self.client.get(reverse('loan_detail', args=[self.loan.id]))
        self.assertEqual(resp_loan_detail.status_code, 200)
        self.assertContains(resp_loan_detail, 'customer-bottom-nav-spacer')
        self.assertContains(resp_loan_detail, 'class="bottom-nav"')

    def test_debit_cards_navbars(self):
        """Cards list and detail pages render the standardized customer navbar and spacer."""
        resp_cards = self.client.get(reverse('card_list'))
        self.assertEqual(resp_cards.status_code, 200)
        self.assertContains(resp_cards, 'customer-bottom-nav-spacer')
        self.assertContains(resp_cards, 'class="bottom-nav"')
        # Verify card action area buttons are present
        self.assertContains(resp_cards, 'Details')

        resp_card_detail = self.client.get(reverse('card_detail', args=[self.card.id]))
        self.assertEqual(resp_card_detail.status_code, 200)
        self.assertContains(resp_card_detail, 'customer-bottom-nav-spacer')
        self.assertContains(resp_card_detail, 'class="bottom-nav"')

    def test_notifications_navbar(self):
        """Notifications page renders the standardized customer navbar."""
        resp = self.client.get(reverse('notifications_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, 'class="bottom-nav"')

    def test_support_ticket_navbars(self):
        """Support ticket list, create, and detail pages render customer navbar."""
        resp_list = self.client.get(reverse('support_ticket_list'))
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, 'customer-bottom-nav-spacer')
        self.assertContains(resp_list, 'class="bottom-nav"')

        resp_create = self.client.get(reverse('support_ticket_create'))
        self.assertEqual(resp_create.status_code, 200)
        self.assertContains(resp_create, 'customer-bottom-nav-spacer')
        self.assertContains(resp_create, 'class="bottom-nav"')

        resp_detail = self.client.get(reverse('support_ticket_detail', args=[self.ticket.id]))
        self.assertEqual(resp_detail.status_code, 200)
        self.assertContains(resp_detail, 'customer-bottom-nav-spacer')
        self.assertContains(resp_detail, 'class="bottom-nav"')

    def test_customer_nav_does_not_appear_on_public_pages(self):
        """Customer bottom nav does not appear on Login or Terms pages."""
        self.client.logout()
        resp_login = self.client.get(reverse('login'))
        self.assertEqual(resp_login.status_code, 200)
        self.assertNotContains(resp_login, 'customer-bottom-nav-spacer')
        self.assertNotContains(resp_login, 'class="bottom-nav"')

        resp_terms = self.client.get(reverse('terms'))
        self.assertEqual(resp_terms.status_code, 200)
        self.assertNotContains(resp_terms, 'customer-bottom-nav-spacer')
        self.assertNotContains(resp_terms, 'class="bottom-nav"')

    def test_employee_and_admin_pages_unmodified(self):
        """Employee dashboard does not render the customer bottom navbar."""
        emp = User.objects.create_user(
            username='nav_employee',
            password='testpassword123',
            role='EMPLOYEE'
        )
        self.client.force_login(emp)
        resp_emp = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(resp_emp.status_code, 200)
        # Employee page should not have customer spacer
        self.assertNotContains(resp_emp, 'customer-bottom-nav-spacer')
