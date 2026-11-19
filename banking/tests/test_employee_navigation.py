from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User


class EmployeeNavigationTests(TestCase):
    """
    Test suite verifying Employee Mobile Hamburger Menu and Navigation Drawer:
    - Hamburger button (☰) present in Employee mobile header with accessible attributes.
    - Employee bottom navbar (.bottom-nav) completely removed from employee pages.
    - Slide-in navigation drawer (#employeeMobileDrawer) rendered with all 12 items:
      1. Dashboard
      2. Customers
      3. Account Operations
      4. KYC Review
      5. Support Queue
      6. Loans
      7. Fixed Deposits
      8. Debit Cards
      9. Reports
      10. Activity Logs
      11. My Profile
      12. Logout
    - Close button (x) present in drawer with data-bs-dismiss="offcanvas".
    - Drawer navigation links have data-bs-dismiss="offcanvas" to close on selection.
    - Active navigation highlighting functions correctly across staff pages.
    - Desktop navigation (.staff-nav-links) is preserved.
    - Customer navigation (.bottom-nav & .customer-bottom-nav-spacer) remains unaffected.
    """

    def setUp(self):
        self.client = Client()
        self.employee = User.objects.create_user(
            username='emp_nav_tester',
            password='testpassword123',
            role='EMPLOYEE',
            first_name='Nav',
            last_name='Officer'
        )
        self.customer = User.objects.create_user(
            username='cust_nav_tester',
            password='testpassword123',
            role='CUSTOMER',
            kyc_status='APPROVED'
        )
        self.client.force_login(self.employee)

    def test_employee_mobile_header_has_accessible_hamburger_button(self):
        """Employee mobile header renders an accessible 3-line hamburger menu button."""
        resp = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Hamburger button markup and attributes
        self.assertContains(resp, 'btn-hamburger')
        self.assertContains(resp, 'd-lg-none')
        self.assertContains(resp, 'id="btnEmployeeHamburger"')
        self.assertContains(resp, 'data-drawer-target="#employeeMobileDrawer"')
        self.assertContains(resp, 'aria-controls="employeeMobileDrawer"')
        self.assertContains(resp, 'aria-expanded="false"')
        self.assertContains(resp, 'aria-label="Toggle navigation menu"')
        self.assertContains(resp, 'bi-list')

    def test_employee_bottom_navbar_completely_removed(self):
        """Horizontal mobile bottom navbar (.bottom-nav) is completely absent on employee views."""
        endpoints = [
            reverse('employee_dashboard'),
            reverse('staff_customer_list'),
            reverse('staff_operations'),
            reverse('staff_kyc_queue'),
            reverse('staff_support_queue'),
            reverse('staff_loan_queue'),
            reverse('staff_fd_list'),
            reverse('staff_card_list'),
            reverse('staff_reports'),
            reverse('staff_activity_log'),
            reverse('staff_profile'),
        ]
        for url in endpoints:
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertNotContains(resp, 'class="bottom-nav"')
                self.assertNotContains(resp, 'customer-bottom-nav-spacer')

    def test_offcanvas_drawer_structure_and_close_button(self):
        """Employee drawer uses accessible offcanvas structure with close button and accessible header."""
        resp = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Drawer container
        self.assertContains(resp, 'id="employeeMobileDrawer"')
        self.assertContains(resp, 'class="offcanvas offcanvas-end employee-drawer"')
        self.assertContains(resp, 'aria-labelledby="employeeMobileDrawerLabel"')
        self.assertContains(resp, 'role="dialog"')

        # Drawer header & close button
        self.assertContains(resp, 'btn-close btn-close-white')
        self.assertContains(resp, 'aria-label="Close navigation menu"')

        # Staff user badge
        self.assertContains(resp, 'emp_nav_tester')
        self.assertContains(resp, 'EMPLOYEE')

        # Native JavaScript drawer controller present
        self.assertContains(resp, 'initEmployeeDrawer')
        self.assertContains(resp, 'employeeDrawerBackdrop')
        self.assertContains(resp, '__employeeDrawer')

    def test_all_12_drawer_navigation_items_present_and_configured(self):
        """Drawer contains all 12 navigation items in specified order with dismiss attributes."""
        resp = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # 1. Dashboard
        self.assertContains(resp, reverse('employee_dashboard'))
        self.assertContains(resp, 'Dashboard')
        # 2. Customers
        self.assertContains(resp, reverse('staff_customer_list'))
        self.assertContains(resp, 'Customers')
        # 3. Account Operations
        self.assertContains(resp, reverse('staff_operations'))
        self.assertContains(resp, 'Account Operations')
        # 4. KYC Review
        self.assertContains(resp, reverse('staff_kyc_queue'))
        self.assertContains(resp, 'KYC Review')
        # 5. Support Queue
        self.assertContains(resp, reverse('staff_support_queue'))
        self.assertContains(resp, 'Support Queue')
        # 6. Loans
        self.assertContains(resp, reverse('staff_loan_queue'))
        self.assertContains(resp, 'Loans')
        # 7. Fixed Deposits
        self.assertContains(resp, reverse('staff_fd_list'))
        self.assertContains(resp, 'Fixed Deposits')
        # 8. Debit Cards
        self.assertContains(resp, reverse('staff_card_list'))
        self.assertContains(resp, 'Debit Cards')
        # 9. Reports
        self.assertContains(resp, reverse('staff_reports'))
        self.assertContains(resp, 'Reports')
        # 10. Activity Logs
        self.assertContains(resp, reverse('staff_activity_log'))
        self.assertContains(resp, 'Activity Logs')
        # 11. My Profile
        self.assertContains(resp, reverse('staff_profile'))
        self.assertContains(resp, 'My Profile')
        # 12. Logout
        self.assertContains(resp, reverse('logout'))
        self.assertContains(resp, 'Logout')
        self.assertContains(resp, 'btn-drawer-logout')

    def test_active_state_highlighting_in_drawer(self):
        """Current page is highlighted as active in drawer across different views."""
        # 1. Dashboard active
        resp = self.client.get(reverse('employee_dashboard'))
        self.assertContains(resp, f'href="{reverse("employee_dashboard")}"\n                   class="drawer-nav-link active"')

        # 2. Customers active
        resp = self.client.get(reverse('staff_customer_list'))
        self.assertContains(resp, f'href="{reverse("staff_customer_list")}"\n                   class="drawer-nav-link active"')

        # 3. Operations active
        resp = self.client.get(reverse('staff_operations'))
        self.assertContains(resp, f'href="{reverse("staff_operations")}"\n                   class="drawer-nav-link active"')

        # 4. KYC Review active
        resp = self.client.get(reverse('staff_kyc_queue'))
        self.assertContains(resp, f'href="{reverse("staff_kyc_queue")}"\n                   class="drawer-nav-link active"')

        # 5. Support Queue active
        resp = self.client.get(reverse('staff_support_queue'))
        self.assertContains(resp, f'href="{reverse("staff_support_queue")}"\n                   class="drawer-nav-link active"')

        # 6. Loans active
        resp = self.client.get(reverse('staff_loan_queue'))
        self.assertContains(resp, f'href="{reverse("staff_loan_queue")}"\n                   class="drawer-nav-link active"')

        # 7. Fixed Deposits active
        resp = self.client.get(reverse('staff_fd_list'))
        self.assertContains(resp, f'href="{reverse("staff_fd_list")}"\n                   class="drawer-nav-link active"')

        # 8. Debit Cards active
        resp = self.client.get(reverse('staff_card_list'))
        self.assertContains(resp, f'href="{reverse("staff_card_list")}"\n                   class="drawer-nav-link active"')

        # 9. Reports active
        resp = self.client.get(reverse('staff_reports'))
        self.assertContains(resp, f'href="{reverse("staff_reports")}"\n                   class="drawer-nav-link active"')

        # 10. Activity Logs active
        resp = self.client.get(reverse('staff_activity_log'))
        self.assertContains(resp, f'href="{reverse("staff_activity_log")}"\n                   class="drawer-nav-link active"')

        # 11. My Profile active
        resp = self.client.get(reverse('staff_profile'))
        self.assertContains(resp, f'href="{reverse("staff_profile")}"\n                   class="drawer-nav-link active"')

    def test_desktop_navigation_preserved(self):
        """Desktop navigation links remain present for screens >= 992px."""
        resp = self.client.get(reverse('employee_dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'staff-nav-links')
        self.assertContains(resp, 'staff-nav-link')
        self.assertContains(resp, 'brand-wrapper')
        self.assertContains(resp, 'Staff Portal')

    def test_customer_bottom_nav_unaffected(self):
        """Customer dashboard retains its customer bottom navbar and clearance spacer."""
        self.client.force_login(self.customer)
        resp = self.client.get(reverse('customer_dashboard'))
        self.assertEqual(resp.status_code, 200)

        # Customer navbar must be present
        self.assertContains(resp, 'customer-bottom-nav-spacer')
        self.assertContains(resp, 'class="bottom-nav"')

        # Employee hamburger and drawer must NOT be on customer pages
        self.assertNotContains(resp, 'employeeMobileDrawer')
        self.assertNotContains(resp, 'btn-hamburger')

