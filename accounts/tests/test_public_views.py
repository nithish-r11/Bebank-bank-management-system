from unittest.mock import patch
from django.db import DatabaseError
from django.test import TestCase, Client
from django.urls import reverse, NoReverseMatch
from accounts.models import User


class PublicViewsRoutingTest(TestCase):
    """Verify routing: / -> 302 redirect to /login/, /login/ -> 200, /terms/ -> 200, no welcome."""

    def setUp(self):
        self.client = Client()

    def test_root_url_redirects_to_login(self):
        """GET / must return HTTP 302 redirecting to /login/."""
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertRedirects(response, reverse('login'))

    def test_login_page_accessible(self):
        """reverse('login') must resolve to /login/ and return HTTP 200."""
        self.assertEqual(reverse('login'), '/login/')
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)

    def test_terms_page_accessible(self):
        """reverse('terms') must resolve to /terms/ and return HTTP 200."""
        self.assertEqual(reverse('terms'), '/terms/')
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)

    def test_no_separate_welcome_page(self):
        """There must be no separate welcome page or 'welcome' URL name."""
        with self.assertRaises(NoReverseMatch):
            reverse('welcome')


class LoginPageRenderingTests(TestCase):
    """Verify login page renders the approved left reference design."""

    def setUp(self):
        self.client = Client()

    def test_login_page_branding_and_titles(self):
        """Login page must render BEBANK branding, Bank Management System, and User Login card without subtitle."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertContains(response, "BEBANK")
        self.assertContains(response, "Bank Management System")
        self.assertContains(response, "User Login")
        # Subtitle must be removed completely per user instruction
        self.assertNotIn("Sign in to access the Bebank system", content)
        self.assertNotIn("login-panel-subtitle", content)

    def test_login_form_fields_and_toggle(self):
        """Login page must contain username, password with eye toggle, and role selector."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'placeholder="Enter username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'placeholder="Enter password"')
        self.assertContains(response, 'id="togglePassword"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, 'Customer')
        self.assertContains(response, 'Employee')
        self.assertContains(response, 'Admin')

    def test_login_submit_button_and_terms_link(self):
        """Login page must render navy Login button and Terms & Conditions link."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Login")
        self.assertContains(response, 'href="/terms/"')
        self.assertContains(response, "Terms &amp; Conditions")

    def test_header_cleanup_no_top_right_action_buttons(self):
        """Header must only have brand on the left; no top-right Login or Terms & Conditions buttons."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        # Header must not have navigation action links/buttons
        self.assertNotIn("nav-header-link", content)
        self.assertNotIn("btn-header-login", content)

    def test_decorative_bank_card_constraints(self):
        """Decorative bank card must display NITHISH KUMAR R, card number 1107 3006 0710 1007, and NO sample badges or payment logos."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        
        # Required sample card elements
        self.assertIn("NITHISH KUMAR R", content)
        self.assertIn("1107 3006 0710 1007", content)
        self.assertIn("tabular-nums", content)
        self.assertIn("card-number-display", content)
        
        # Must REMOVE sample badge completely per user instruction
        self.assertNotIn("SAMPLE — NOT VALID", content)
        self.assertNotIn("SAMPLE - NOT VALID", content)
        self.assertNotIn("card-sample-marking", content)
        
        # Decorative only: no CVV, no real payment networks, no real expiry, no balances
        self.assertNotIn("CVV", content)
        self.assertNotIn("Visa", content)
        self.assertNotIn("Mastercard", content)
        self.assertNotIn("RuPay", content)
        self.assertNotIn("v₹", content)
        self.assertNotIn("Balance", content)

    def test_header_logo_and_text_alignment(self):
        """Header branding must feature transparent B-monogram SVG logo, vertical divider, and stacked BEBANK / Bank Management System text."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # Logo on left
        self.assertIn("header-brand-logo-img", content)
        self.assertTrue("bebank_b_monogram" in content and ".svg" in content)

        # Vertical divider after logo
        self.assertIn("header-divider", content)

        # Stacked title and subtitle to the right of divider
        self.assertIn("header-brand-title", content)
        self.assertIn("header-brand-subtitle", content)
        self.assertIn("BEBANK", content)
        self.assertIn("Bank Management System", content)

        # Exact 46px vertical alignment specifications
        self.assertIn("46px", content)

    def test_typography_system_inter_font(self):
        """Pages must load Google Fonts Inter and avoid decorative serif fonts like Times New Roman."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # Inter font stylesheet loaded
        self.assertIn("fonts.googleapis.com/css2?family=Inter", content)
        self.assertIn("'Inter'", content)

        # No decorative serif font
        self.assertNotIn("Times New Roman", content)

    def test_b_monogram_svg_logo_integrated(self):
        """Pages must use the official scalable B-monogram SVG logo with transparent background."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertTrue("bebank_b_monogram" in content and ".svg" in content)

    def test_browser_favicon_link_tags(self):
        """Shared public head must include SVG favicon, PNG fallbacks, and Apple touch icon."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # SVG favicon link tag
        self.assertIn('rel="icon"', content)
        self.assertIn('type="image/svg+xml"', content)
        self.assertTrue('bebank_b_monogram' in content and '.svg' in content)

        # PNG fallback link tags
        self.assertIn('sizes="32x32"', content)
        self.assertIn('type="image/png"', content)
        self.assertTrue('favicon-32x32' in content and '.png' in content)

        # Apple touch icon
        self.assertIn('rel="apple-touch-icon"', content)
        self.assertTrue('apple-touch-icon' in content and '.png' in content)

    def test_favicon_static_assets_exist(self):
        """Favicon SVG, PNG fallbacks, and Apple touch icon files must exist on disk."""
        import os
        from django.conf import settings
        static_img_dir = os.path.join(settings.BASE_DIR, 'static', 'images')
        self.assertTrue(os.path.exists(os.path.join(static_img_dir, 'bebank_b_monogram.svg')))
        self.assertTrue(os.path.exists(os.path.join(static_img_dir, 'favicon-16x16.png')))
        self.assertTrue(os.path.exists(os.path.join(static_img_dir, 'favicon-32x32.png')))
        self.assertTrue(os.path.exists(os.path.join(static_img_dir, 'favicon-48x48.png')))
        self.assertTrue(os.path.exists(os.path.join(static_img_dir, 'apple-touch-icon.png')))

    def test_no_home_navigation_on_login(self):
        """Login page must have no Home button or Home navigation links."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertNotRegex(content, r'>\s*Home\s*<')

    def test_login_form_outline_icons(self):
        """Login form must use clean outline SVGs (Lucide-style) and avoid Bootstrap font icons or Unicode arrows."""
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')

        # Outline SVG presence for form elements
        self.assertIn("form-icon-user-round", content)
        self.assertIn("form-icon-lock", content)
        self.assertIn("form-icon-eye", content)
        self.assertIn("form-icon-eye-off", content)
        self.assertIn("form-icon-users-round", content)
        self.assertIn("form-icon-chevron-down", content)
        self.assertIn("form-icon-arrow-right", content)
        self.assertIn("form-icon-file-text", content)

        # Accessibility attributes for password toggle button
        self.assertIn('aria-pressed="false"', content)
        self.assertIn('aria-controls="id_password"', content)

        # Absence of legacy Bootstrap icons in the login form
        self.assertNotIn("bi-person", content)
        self.assertNotIn("bi-lock", content)
        self.assertNotIn("bi-people", content)
        self.assertNotIn("bi-file-earmark-text", content)

        # Absence of HTML entity / unicode arrow in login submit button
        self.assertNotIn("&rarr;", content)



class LoginPageAuthenticationTests(TestCase):
    """Verify login authentication, CSRF, and role-based redirects are preserved."""

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(
            username='auth_customer',
            password='CustomerPass123!',
            role='CUSTOMER'
        )
        self.employee = User.objects.create_user(
            username='auth_employee',
            password='EmployeePass123!',
            role='EMPLOYEE'
        )
        self.admin = User.objects.create_user(
            username='auth_admin',
            password='AdminPass123!',
            role='ADMIN'
        )

    def test_login_form_csrf_preserved(self):
        """Login page must include CSRF token."""
        response = self.client.get(reverse('login'))
        self.assertContains(response, 'csrfmiddlewaretoken')

    def test_customer_login_redirects_to_customer_dashboard(self):
        """Valid customer login redirects to customer dashboard."""
        response = self.client.post(reverse('login'), {
            'username': 'auth_customer',
            'password': 'CustomerPass123!',
            'role': 'CUSTOMER',
        })
        self.assertRedirects(response, reverse('customer_dashboard'))

    def test_employee_login_redirects_to_employee_dashboard(self):
        """Valid employee login redirects to employee dashboard."""
        response = self.client.post(reverse('login'), {
            'username': 'auth_employee',
            'password': 'EmployeePass123!',
            'role': 'EMPLOYEE',
        })
        self.assertRedirects(response, reverse('employee_dashboard'))

    def test_admin_login_redirects_to_admin_dashboard(self):
        """Valid admin login redirects to admin dashboard."""
        response = self.client.post(reverse('login'), {
            'username': 'auth_admin',
            'password': 'AdminPass123!',
            'role': 'ADMIN',
        })
        self.assertRedirects(response, reverse('admin_dashboard'))

    def test_invalid_credentials_shows_error(self):
        """Invalid credentials displays error message."""
        response = self.client.post(reverse('login'), {
            'username': 'auth_customer',
            'password': 'WrongPassword!',
            'role': 'CUSTOMER',
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username or password")


class TermsPageRenderingTests(TestCase):
    """Verify Terms & Conditions page renders the exact approved right reference design."""

    def setUp(self):
        self.client = Client()

    def test_terms_page_header_and_titles(self):
        """Terms page must render BEBANK header, Bank Management System, and Terms title."""
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BEBANK")
        self.assertContains(response, "Bank Management System")
        self.assertContains(response, "Terms &amp; Conditions")
        self.assertContains(response, "Please read the following terms carefully before using Bebank.")

    def test_terms_educational_draft_disclaimer(self):
        """Terms page must render the plain educational draft notice at the bottom."""
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This is a draft for educational use only.")

    def test_terms_numbered_sections_from_reference(self):
        """Terms page must render the 6 numbered policy sections matching the approved reference."""
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1. Project Purpose:")
        self.assertContains(response, "2. No Real Funds:")
        self.assertContains(response, "3. No UPI or Payment Integrations:")
        self.assertContains(response, "4. Mock KYC Only:")
        self.assertContains(response, "5. User Responsibilities:")
        self.assertContains(response, "6. Statements and PDFs:")

    def test_terms_clean_unadorned_styling(self):
        """Terms page must use a plain white background and no heavy cards or unnecessary icons."""
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        # Must not contain the previous heavy policy cards or draft banner
        self.assertNotIn("policy-card", content)
        self.assertNotIn("draft-banner", content)

    def test_no_home_navigation_on_terms(self):
        """Terms page must have no Home button or Home navigation links."""
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8')
        self.assertNotRegex(content, r'>\s*Home\s*<')


class PublicCrossLinkingAndResilienceTests(TestCase):
    """Verify bidirectional navigation, employee account creation KYC disclaimer, and DB resilience."""

    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='resil_employee',
            password='EmployeePass123!',
            role='EMPLOYEE',
            is_active=True,
            account_locked=False
        )

    def test_create_account_page_has_mock_kyc_and_terms_link(self):
        """Employee account creation page must feature mock KYC notice linking to Terms."""
        self.client.login(username='resil_employee', password='EmployeePass123!')
        response = self.client.get(reverse('create_account'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mock KYC")
        self.assertContains(response, 'href="/terms/"')

    @patch('django.contrib.auth.get_user')
    def test_login_resilient_to_database_error(self, mock_get_user):
        """Login page must return 200 even if session lookup raises DatabaseError."""
        mock_get_user.side_effect = DatabaseError("Session DB unavailable")
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "User Login")

    @patch('django.contrib.auth.get_user')
    def test_terms_resilient_to_database_error(self, mock_get_user):
        """Terms page must return 200 even if session lookup raises DatabaseError."""
        mock_get_user.side_effect = DatabaseError("Session DB unavailable")
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Terms &amp; Conditions")
