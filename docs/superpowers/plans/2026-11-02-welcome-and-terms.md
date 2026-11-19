# Welcome and Terms & Conditions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a responsive, navy-and-white Welcome / Landing Page at root (`/`) and a public Terms & Conditions page at `/terms/`, featuring dynamic role-aware dashboard action buttons, strict locked/inactive guards, and prominent virtual banking simulation disclaimers.

**Architecture:** Django server-rendered views in `accounts/views.py` (`welcome_view`, `terms_view`) mapped cleanly in `accounts/urls.py` and `core/urls.py`. Shared template architecture via `templates/base_public.html` providing consistent header/footer, simulation banner, and Bootstrap 5 CDN styling. Dynamic CTA computed via Django URL reversal (`customer_dashboard`, `employee_dashboard`, `admin_dashboard`) with fallback to `login` for anonymous or locked accounts.

**Tech Stack:** Django 5+, Python 3.13, Bootstrap 5.3.2 CDN, Bootstrap Icons 1.11.3 CDN, Django TestCase / Client.

**Spec:** `welcome-and-terms-design.md` (Artifact: `C:\Users\nithi\.gemini\antigravity\brain\c4ccf6e4-86f4-4ad8-82e9-fcbc7d4e66c3\welcome-and-terms-design.md`)

## Global Constraints
- Root `/` must be accessible as the public Welcome page returning HTTP 200; do not automatically redirect authenticated visitors away from `/`.
- All dashboard links must use Django reverse (`customer_dashboard`, `employee_dashboard`, `admin_dashboard`) and never hardcode paths.
- Preserves existing authentication, RBAC, CSRF, and account-lock checks.
- Locked or inactive accounts (`account_locked=True` or `is_active=False`) must never receive an active dashboard shortcut.
- Prominent simulation banner: `SIMULATED BANKING ENVIRONMENT — NO REAL FUNDS` and `v₹` currency notation.
- Public Terms & Conditions page at `/terms/` explaining educational simulation, mock KYC (never real Aadhaar/PAN), and no real financial services.
- No database schema migrations or production database changes.
- Untracked `docs/` preserved.

## Review Focus
- Unauthenticated visitors accessing `/` see a Login CTA pointing to `/login/` without errors.
- Inactive or locked authenticated users (`is_active=False` or `account_locked=True`) accessing `/` see the Login CTA rather than an active dashboard link.
- Authenticated users with invalid or unassigned role choice do not crash `/` and fall back safely to login CTA.
- `/terms/` is fully public (accessible by anonymous visitors and all roles without authentication).
- Forms and existing navigation retain link to `/terms/` without breaking existing submit actions or CSRF.

---

### Task 1: Public Routes and View Stubs with TDD (RED &rarr; GREEN)

**Files:**
- Create: `accounts/tests/test_public_views.py`
- Modify: `core/urls.py:6-17`
- Modify: `accounts/urls.py:4-9`
- Modify: `accounts/views.py:1-20`

**Interfaces:**
- Consumes: Django URL resolver, `accounts.views.login_view`
- Produces: `welcome` route (`/`), `terms` route (`/terms/`), `login` route (`/login/`)

- [ ] **Step 1: Write the failing tests in `accounts/tests/test_public_views.py`**

```python
from django.test import TestCase, Client
from django.urls import reverse
from accounts.models import User

class PublicViewsRoutingTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_welcome_page_accessible(self):
        response = self.client.get(reverse('welcome'))
        self.assertEqual(response.status_code, 200)

    def test_root_url_resolves_to_welcome(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)

    def test_terms_page_accessible(self):
        response = self.client.get(reverse('terms'))
        self.assertEqual(response.status_code, 200)

    def test_login_page_remains_accessible(self):
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)
```

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: FAIL (`NoReverseMatch` for 'welcome' / 'terms').

- [ ] **Step 3: Implement route definitions and view stubs**

In `accounts/views.py`:
- Add `welcome_view(request)` stub rendering temporary response / initial template.
- Add `terms_view(request)` stub rendering temporary response / initial template.

In `accounts/urls.py`:
- Add `path('', views.welcome_view, name='welcome')`
- Add `path('terms/', views.terms_view, name='terms')`
- Ensure `path('login/', views.login_view, name='login')` remains active.

In `core/urls.py`:
- Remove `home_redirect` and `path('', home_redirect)`.
- Allow `path('', include('accounts.urls'))` to handle root `/`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: PASS (all 4 tests pass).

---

### Task 2: Dynamic Dashboard CTA and Security Guard Logic

**Files:**
- Modify: `accounts/views.py:welcome_view`
- Modify: `accounts/tests/test_public_views.py`

**Interfaces:**
- Consumes: `request.user`, `request.user.role`, `request.user.account_locked`, `request.user.is_active`, `accounts.decorators._is_locked_or_inactive`
- Produces: context dict for `welcome.html` containing `auth_action_url` and `auth_action_label`

- [ ] **Step 1: Write failing tests for dynamic CTA and locked guards**

In `accounts/tests/test_public_views.py`:
- `test_anonymous_user_cta`: Context `auth_action_label == 'Login'`, `auth_action_url == reverse('login')`.
- `test_customer_user_cta`: Customer user gets `auth_action_label == 'Go to Customer Dashboard'`, `auth_action_url == reverse('customer_dashboard')`.
- `test_employee_user_cta`: Employee user gets `auth_action_label == 'Go to Employee Dashboard'`, `auth_action_url == reverse('employee_dashboard')`.
- `test_admin_user_cta`: Admin user gets `auth_action_label == 'Go to Admin Dashboard'`, `auth_action_url == reverse('admin_dashboard')`.
- `test_locked_user_cta`: User with `account_locked=True` gets fallback `auth_action_label == 'Login'`, `auth_action_url == reverse('login')`.
- `test_inactive_user_cta`: User with `is_active=False` gets fallback `auth_action_label == 'Login'`, `auth_action_url == reverse('login')`.
- `test_unknown_role_cta`: User with unexpected role falls back safely to `auth_action_label == 'Login'`, `auth_action_url == reverse('login')`.

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: FAIL on context assertions.

- [ ] **Step 3: Implement `welcome_view` context logic**

In `accounts/views.py`:
```python
def welcome_view(request):
    auth_action_url = reverse('login')
    auth_action_label = "Login"

    user = request.user
    if user.is_authenticated and not (getattr(user, 'account_locked', False) or not getattr(user, 'is_active', True)):
        if user.role == 'CUSTOMER':
            auth_action_url = reverse('customer_dashboard')
            auth_action_label = "Go to Customer Dashboard"
        elif user.role == 'EMPLOYEE':
            auth_action_url = reverse('employee_dashboard')
            auth_action_label = "Go to Employee Dashboard"
        elif user.role == 'ADMIN':
            auth_action_url = reverse('admin_dashboard')
            auth_action_label = "Go to Admin Dashboard"

    return render(request, "welcome.html", {
        "auth_action_url": auth_action_url,
        "auth_action_label": auth_action_label,
    })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: PASS.

---

### Task 3: Shared Public Base Template (`templates/base_public.html`)

**Files:**
- Create: `templates/base_public.html`

**Interfaces:**
- Consumes: Bootstrap 5.3.2 CSS/JS, Bootstrap Icons 1.11.3, Django URL reversing (`welcome`, `terms`, `login`, `logout`)
- Produces: Base HTML layout for public pages with top warning banner, navbar, content block, and footer.

- [ ] **Step 1: Write template verification test in `accounts/tests/test_public_views.py`**

Test that public pages render the persistent simulation banner:
`self.assertContains(response, "SIMULATED BANKING ENVIRONMENT — NO REAL FUNDS")`
`self.assertContains(response, "BEBANK")`

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: FAIL (banner missing).

- [ ] **Step 2: Create `templates/base_public.html`**

Implement:
- Top sticky banner: `<div class="bg-warning text-dark py-1 text-center fw-bold small">⚠️ SIMULATED BANKING ENVIRONMENT — NO REAL FUNDS — EDUCATIONAL USE ONLY</div>`
- Navbar:
  - Brand: `🏦 BEBANK` linking to `{% url 'welcome' %}`
  - Links: `Home`, `Features`, `Roles`, `How It Works`, `Terms & Conditions`
  - Auth CTA Button: `<a href="{{ auth_action_url }}" class="btn btn-primary btn-sm px-3 rounded-pill">{{ auth_action_label }}</a>`
  - If authenticated: include a small "Logout" icon/button linking to `{% url 'logout' %}`
- Block content: `{% block content %}{% endblock %}`
- Footer:
  - Navy background (`#0f172a`), white text.
  - Bebank description: "Bebank Virtual Banking Management System is an educational banking simulation platform."
  - Legal disclaimer: "Project-use draft; requires legal review prior to commercial launch. No real financial services, UPI, or payment gateways are provided."
  - Quick links to Home, Terms & Conditions, and Login.
  - Copyright: `© 2026 Bebank System. Virtual simulation only.`

- [ ] **Step 3: Verify base layout renders correctly through view**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: PASS.

---

### Task 4: Welcome / Landing Page Content (`templates/welcome.html`)

**Files:**
- Create: `templates/welcome.html`
- Modify: `accounts/tests/test_public_views.py`

**Interfaces:**
- Extends: `base_public.html`
- Produces: Hero section, About section, Features grid, Role breakdown, Simulation workflow ("How Bebank Works")

- [ ] **Step 1: Write content assertion tests in `accounts/tests/test_public_views.py`**

Verify `welcome.html` contains:
- Title: "Virtual Banking Management System"
- Currency badge: `v₹`
- Features: "Accounts", "Virtual Deposits", "Virtual Withdrawals", "Instant Transfers", "Passbook", "Fixed Deposits", "Virtual Loans"
- Roles: "Customer", "Employee", "Admin"
- Workflow: "How Bebank Works"

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: FAIL.

- [ ] **Step 3: Implement `templates/welcome.html`**

Structure:
- **Hero Section**:
  - Heading: "Virtual Banking Management System"
  - Subheading: "Experience full-cycle modern banking operations in a safe, educational simulation powered by virtual currency (`v₹`)."
  - CTAs: Primary `<a href="{{ auth_action_url }}" class="btn btn-primary btn-lg rounded-pill px-4">{{ auth_action_label }}</a>` + Secondary `<a href="#features" class="btn btn-outline-light btn-lg rounded-pill px-4">Explore Features</a>`
- **About Bebank Section**:
  - Highlights Bebank's mission as an open, educational financial simulation environment designed to demonstrate robust banking workflows without financial risks.
- **Features Grid (`#features`)**:
  - 1. Customer Accounts (Savings & Current accounts)
  - 2. Virtual Deposits (Mock cash inflows in `v₹`)
  - 3. Virtual Withdrawals (Controlled withdrawals with balance checks)
  - 4. Instant Transfers (Atomic funds transfer between Bebank accounts)
  - 5. Real-Time Passbook (Detailed audit history of transactions)
  - 6. Fixed Deposits (Simulated interest-earning term deposits)
  - 7. Virtual Loans (Application, approval, and management)
- **Roles & Governance Section (`#roles`)**:
  - Customer Card: Account overview, transactions, loan requests, FDs.
  - Employee Card: Customer account creation, loan approvals, transaction management.
  - Admin Card: System oversight, user lock/unlock governance, audit trail.
- **How Bebank Works Section (`#how-it-works`)**:
  - Step 1: Employee onboards customer with mock KYC.
  - Step 2: Customer logs in to access virtual accounts and `v₹` balance.
  - Step 3: Perform secure simulated deposits, transfers, and investments.
  - Step 4: Strict RBAC and audit logging safeguard system integrity.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: PASS.

---

### Task 5: Terms & Conditions Page (`templates/terms.html`) & Cross-Linking

**Files:**
- Create: `templates/terms.html`
- Modify: `accounts/views.py:terms_view`
- Modify: `templates/login.html`
- Modify: `templates/create_account.html`
- Modify: `accounts/tests/test_public_views.py`

**Interfaces:**
- Extends: `base_public.html` for `terms.html`
- Produces: Fully populated `/terms/` page, link to Terms in `login.html`, and mock KYC notice in `create_account.html`

- [ ] **Step 1: Write tests for Terms & Conditions content and login links**

In `accounts/tests/test_public_views.py`:
- `test_terms_content`: Verify `/terms/` returns 200 and contains:
  - "Educational Virtual Banking Simulation Only"
  - "No Real Money or Real Financial Services"
  - "No UPI or Payment Gateway Integration"
  - "Virtual Balance (`v₹`)"
  - "Mock KYC Only: Never enter genuine Aadhaar or PAN details"
  - "Project-Use Draft Disclaimer"
- `test_login_page_has_terms_and_home_links`:
  - Verify `GET /login/` contains link to `/terms/` and link to `/` (Home).
- `test_create_account_page_has_mock_kyc_disclaimer`:
  - As authenticated employee, verify `GET /bank/create-account/` contains mock KYC disclaimer.

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: FAIL.

- [ ] **Step 3: Implement `templates/terms.html`**

Include:
- Section 1: Nature of the Service (Academic & Educational Simulation).
- Section 2: Virtual Currency (`v₹`) & No Real Funds.
- Section 3: Absence of Payment Gateways / UPI.
- Section 4: Strict Mock KYC Rule (Prohibition against real Aadhaar, PAN, banking numbers).
- Section 5: Permitted Usage & User Responsibilities.
- Section 6: Limitation of Liability & Simulation Disclaimer.
- Notice: "This document is a project-use draft and has not been vetted by legal counsel. It must undergo formal legal review before any commercial or public deployment."

- [ ] **Step 4: Update `templates/login.html` and `templates/create_account.html`**

In `templates/login.html`:
- Add a top header link `⬅ Back to Home` pointing to `{% url 'welcome' %}`.
- Below the login form, add a footer link: `<div class="text-center mt-3 small"><a href="{% url 'terms' %}" class="text-muted">Terms & Conditions</a></div>`.

In `templates/create_account.html`:
- Add a visible warning card above the form:
  `<div class="alert alert-warning small"><strong>Mock KYC Notice:</strong> This is a simulation. Never enter genuine Aadhaar, PAN, or personal sensitive data. See <a href="{% url 'terms' %}" target="_blank">Terms & Conditions</a>.</div>`

- [ ] **Step 5: Run tests to verify all pass**

Run: `.\.venv\Scripts\python.exe manage.py test accounts.tests.test_public_views -v 2`
Expected: PASS.

---

### Task 6: Full Regression Verification and Independent Code Review

**Files:**
- None (verification only)

- [ ] **Step 1: Run complete test suite**

Run: `.\.venv\Scripts\python.exe manage.py test -v 2`
Expected: All tests pass (0 failures, 0 errors, 3 skipped SQLite PostgreSQL concurrency tests).

- [ ] **Step 2: Run Django system check**

Run: `.\.venv\Scripts\python.exe manage.py check`
Expected: `System check identified no issues (0 silenced).`

- [ ] **Step 3: Run git diff whitespace and hygiene check**

Run: `git diff --check`
Expected: No output (clean diff).

- [ ] **Step 4: Verify git status**

Run: `git status --short`
Expected: Working tree contains only modified/untracked files as planned, untracked `docs/` preserved, no accidental changes.

- [ ] **Step 5: Request independent code review**

Invoke `superpowers:requesting-code-review` via fresh subagent.
Expected: Reviewer returns approved verdict.

