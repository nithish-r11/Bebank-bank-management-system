import http.server
import os
import re
import socketserver
import subprocess
import threading
import time
import urllib.parse
from decimal import Decimal
from django.conf import settings
from django.test import TestCase, Client
from accounts.models import User
from banking.models import BankAccount, Loan, FixedDeposit, Transaction as BankTx


class DuplicateSubmissionGuardTemplateIntegrationTests(TestCase):
    """
    Tests ensuring that all financial state-changing templates integrate the client-side
    duplicate submission safeguard, preserve CSRF tokens, required field validation,
    and retain standard form structure.
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(
            username='cust_guard', password='password123', role='CUSTOMER'
        )
        self.employee = User.objects.create_user(
            username='emp_guard', password='password123', role='EMPLOYEE'
        )
        self.admin = User.objects.create_superuser(
            username='adm_guard', password='password123', role='ADMIN'
        )

        self.account = BankAccount.objects.create(
            user=self.customer,
            account_type='SAVINGS',
            balance=Decimal('2000.00')
        )
        self.pending_loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('5000.00'),
            duration_months=12,
            status='PENDING'
        )

    def test_withdraw_template_includes_submit_guard(self):
        """GET /bank/withdraw/ renders with submit guard script and valid CSRF-protected form."""
        self.client.force_login(self.customer)
        response = self.client.get('/bank/withdraw/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertRegex(content, r'submit_guard(\.[a-f0-9]+)?\.js')
        self.assertIn('csrfmiddlewaretoken', content)
        self.assertIn('name="amount"', content)
        self.assertIn('required', content)

    def test_transfer_template_includes_submit_guard(self):
        """GET /bank/transfer/ renders with submit guard script and valid CSRF-protected form."""
        self.client.force_login(self.customer)
        response = self.client.get('/bank/transfer/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertRegex(content, r'submit_guard(\.[a-f0-9]+)?\.js')
        self.assertIn('csrfmiddlewaretoken', content)
        self.assertIn('name="account_number"', content)
        self.assertIn('name="amount"', content)

    def test_deposit_template_includes_submit_guard(self):
        """GET /bank/deposit/ renders with submit guard script for staff and valid CSRF-protected form."""
        self.client.force_login(self.employee)
        response = self.client.get('/bank/deposit/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertRegex(content, r'submit_guard(\.[a-f0-9]+)?\.js')
        self.assertIn('csrfmiddlewaretoken', content)
        self.assertIn('name="account_number"', content)
        self.assertIn('name="amount"', content)

    def test_fd_template_includes_submit_guard(self):
        """GET /bank/fd/ renders with submit guard script and valid CSRF-protected form."""
        self.client.force_login(self.customer)
        response = self.client.get('/bank/fd/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertRegex(content, r'submit_guard(\.[a-f0-9]+)?\.js')
        self.assertIn('csrfmiddlewaretoken', content)
        self.assertIn('name="amount"', content)
        self.assertIn('name="months"', content)

    def test_loan_template_includes_submit_guard(self):
        """GET /bank/loan/ renders with submit guard script and valid CSRF-protected form."""
        self.client.force_login(self.customer)
        response = self.client.get('/bank/loan/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertRegex(content, r'submit_guard(\.[a-f0-9]+)?\.js')
        self.assertIn('csrfmiddlewaretoken', content)
        self.assertIn('name="amount"', content)
        self.assertIn('name="months"', content)

    def test_admin_dashboard_template_includes_submit_guard(self):
        """GET /admin-dashboard/ renders with submit guard script covering loan action forms."""
        self.client.force_login(self.admin)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertRegex(content, r'submit_guard(\.[a-f0-9]+)?\.js')
        self.assertIn('csrfmiddlewaretoken', content)
        self.assertIn(f'/bank/loan/{self.pending_loan.id}/approve/', content)
        self.assertIn(f'/bank/loan/{self.pending_loan.id}/reject/', content)


class SubmitGuardScriptSpecificationTests(TestCase):
    """
    Tests ensuring static/js/submit_guard.js exists and implements all requirements:
    - POST only filtering
    - Browser constraint validation check (checkValidity)
    - Submitting state marking
    - Subsequent submit cancellation (preventDefault, stopImmediatePropagation)
    - Submit button disabling and 'Processing...' text
    - Preserves submit button name/value
    - bfcache restoration on pageshow
    """

    def test_submit_guard_file_exists(self):
        js_path = os.path.join(settings.BASE_DIR, 'static', 'js', 'submit_guard.js')
        self.assertTrue(os.path.isfile(js_path), f"File {js_path} must exist.")

    def test_submit_guard_script_content_specification(self):
        js_path = os.path.join(settings.BASE_DIR, 'static', 'js', 'submit_guard.js')
        if not os.path.isfile(js_path):
            self.fail(f"Missing {js_path}")

        with open(js_path, 'r', encoding='utf-8') as f:
            js_code = f.read()

        # Check for core design points
        self.assertIn('dataset.submitting', js_code)
        self.assertIn('preventDefault', js_code)
        self.assertIn('stopImmediatePropagation', js_code)
        self.assertIn('Processing...', js_code)
        self.assertIn('checkValidity', js_code)
        self.assertIn('disabled', js_code)
        self.assertIn('POST', js_code)
        self.assertIn('pageshow', js_code)
        # Ensure no duplicate-causing hidden submitter clones are created
        self.assertNotIn('data-submitter-clone', js_code, "Submitter hidden clone must not be created.")

    def test_submit_guard_javascript_runtime_in_node(self):
        """
        Execute static/js/submit_guard.js inside a Node.js micro DOM simulation to verify
        live event behavior:
        1. Form with POST and valid inputs:
           - First submit: marks form submitting, disables submit button, changes text to 'Processing...'.
           - Second submit: immediately cancelled (defaultPrevented = true).
        2. Form with invalid input (checkValidity() === false):
           - Submit is NOT guarded; submitting flag is not set, button remains enabled.
        3. Form with GET method:
           - Ignored; submitting flag is not set.
        4. pageshow event:
           - Resets submitting flag and restores button.
        """
        js_path = os.path.join(settings.BASE_DIR, 'static', 'js', 'submit_guard.js')
        if not os.path.isfile(js_path):
            self.fail(f"Missing {js_path}")

        test_runner_js = f"""
        const fs = require('fs');
        const scriptCode = fs.readFileSync({repr(js_path)}, 'utf-8');

        // Micro DOM mock
        class MockEvent {{
            constructor(type, init = {{}}) {{
                this.type = type;
                this.target = init.target || null;
                this.defaultPrevented = Boolean(init.defaultPrevented);
                this.propagationStopped = false;
                this.persisted = init.persisted || false;
            }}
            preventDefault() {{ this.defaultPrevented = true; }}
            stopImmediatePropagation() {{ this.propagationStopped = true; }}
        }}

        const listeners = {{}};
        global.document = {{
            readyState: 'complete',
            addEventListener: function(event, fn) {{
                listeners[event] = listeners[event] || [];
                listeners[event].push(fn);
            }}
        }};
        global.window = {{
            addEventListener: function(event, fn) {{
                listeners[event] = listeners[event] || [];
                listeners[event].push(fn);
            }}
        }};

        // Load the script
        eval(scriptCode);

        function trigger(event, target, extra = {{}}) {{
            const e = new MockEvent(event, {{ target, ...extra }});
            const list = listeners[event] || [];
            for (const fn of list) {{
                fn(e);
            }}
            return e;
        }}

        // Helper to construct mock form
        function createMockForm(method = 'POST', valid = true) {{
            const button = {{
                nodeName: 'BUTTON',
                type: 'submit',
                disabled: false,
                innerText: 'Submit Action',
                innerHTML: 'Submit Action',
                textContent: 'Submit Action',
                dataset: {{}},
                setAttribute: function(k, v) {{ this[k] = v; }},
                removeAttribute: function(k) {{ delete this[k]; }},
                closest: function(sel) {{ return this; }}
            }};
            const form = {{
                nodeName: 'FORM',
                getAttribute: (attr) => attr === 'method' ? method : null,
                dataset: {{}},
                checkValidity: () => valid,
                querySelectorAll: function(sel) {{
                    return [button];
                }},
                querySelector: function(sel) {{
                    return button;
                }},
                contains: function(node) {{ return node === button; }},
                appendChild: function(child) {{}}
            }};
            button.form = form;
            return {{ form, button }};
        }}

        // TEST 1: Valid POST form submission
        const {{ form: postForm, button: postBtn }} = createMockForm('POST', true);
        const submitEvt1 = trigger('submit', postForm);
        if (submitEvt1.defaultPrevented) throw new Error('First valid submit should not be prevented');
        if (postForm.dataset.submitting !== 'true') throw new Error('Form should be marked as submitting');

        // Wait a microtick for setTimeout(..., 0)
        setTimeout(() => {{
            if (!postBtn.disabled) throw new Error('Button should be disabled after submission');
            if (postBtn.textContent !== 'Processing...') throw new Error('Button text should be Processing...');

            // TEST 2: Second duplicate submit on same form
            const submitEvt2 = trigger('submit', postForm);
            if (!submitEvt2.defaultPrevented) throw new Error('Duplicate submit must be prevented');
            if (!submitEvt2.propagationStopped) throw new Error('Duplicate submit must stop propagation');

            // TEST 3: Invalid form
            const {{ form: invForm, button: invBtn }} = createMockForm('POST', false);
            const invEvt = trigger('submit', invForm);
            if (invForm.dataset.submitting === 'true') throw new Error('Invalid form should not be marked as submitting');
            if (invBtn.disabled) throw new Error('Invalid form button should not be disabled');

            // TEST 4: GET form
            const {{ form: getForm, button: getBtn }} = createMockForm('GET', true);
            const getEvt = trigger('submit', getForm);
            if (getForm.dataset.submitting === 'true') throw new Error('GET form should not be marked as submitting');

            // TEST 5: bfcache pageshow restoration
            global.document.querySelectorAll = function(sel) {{
                if (sel.includes('data-submitting="true"')) return [postForm];
                return [];
            }};
            trigger('pageshow', global.window, {{ persisted: true }});
            if (postForm.dataset.submitting === 'true') throw new Error('pageshow should reset submitting state');
            if (postBtn.disabled) throw new Error('pageshow should re-enable button');
            if (postBtn.textContent !== 'Submit Action') throw new Error('pageshow should restore button text');

            // TEST 6: Event already cancelled via defaultPrevented (e.g. custom confirmation/validator)
            const {{ form: defPrevForm, button: defPrevBtn }} = createMockForm('POST', true);
            const cancelEvt = trigger('submit', defPrevForm, {{ defaultPrevented: true }});
            if (defPrevForm.dataset.submitting === 'true') throw new Error('Cancelled event with defaultPrevented should not set submitting');
            if (defPrevBtn.disabled) throw new Error('Cancelled event should not disable submit button');

            // TEST 7: input[type="submit"] value manipulation and restoration
            const inputButton = {{
                nodeName: 'INPUT',
                type: 'submit',
                disabled: false,
                value: 'Save Data',
                dataset: {{}},
                setAttribute: function(k, v) {{ this[k] = v; }},
                removeAttribute: function(k) {{ delete this[k]; }},
                closest: function(sel) {{ return this; }}
            }};
            const inputForm = {{
                nodeName: 'FORM',
                getAttribute: (attr) => attr === 'method' ? 'POST' : null,
                dataset: {{}},
                checkValidity: () => true,
                querySelectorAll: function(sel) {{ return [inputButton]; }},
                querySelector: function(sel) {{ return inputButton; }},
                contains: function(node) {{ return node === inputButton; }},
                appendChild: function(child) {{}}
            }};
            inputButton.form = inputForm;

            trigger('submit', inputForm);
            if (inputForm.dataset.submitting !== 'true') throw new Error('Input form should be submitting');
            setTimeout(() => {{
                if (!inputButton.disabled) throw new Error('Input button should be disabled');
                if (inputButton.value !== 'Processing...') throw new Error('Input button value should be Processing...');

                global.document.querySelectorAll = function(sel) {{
                    if (sel.includes('data-submitting="true"')) return [inputForm];
                    return [];
                }};
                trigger('pageshow', global.window, {{ persisted: true }});
                if (inputButton.value !== 'Save Data') throw new Error('Input button value should be restored');

                console.log("ALL_NODE_TESTS_PASSED");
            }}, 10);
        }}, 10);
        """

        try:
            result = subprocess.run(
                ['node', '-e', test_runner_js],
                capture_output=True,
                text=True,
                cwd=str(settings.BASE_DIR)
            )
        except FileNotFoundError:
            self.skipTest("Node.js CLI is not available in test environment.")

        self.assertEqual(result.returncode, 0, f"Node test runner failed: {result.stderr}")
        self.assertIn("ALL_NODE_TESTS_PASSED", result.stdout)


class DuplicateSubmissionBackendIntegrityTests(TestCase):
    """
    Ensures that existing banking operations and loan actions continue to process
    normally upon valid POST requests (verifying no regressions in backend handlers).
    """

    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(username='c_bk', password='pwd', role='CUSTOMER')
        self.other_customer = User.objects.create_user(username='c_other', password='pwd', role='CUSTOMER')
        self.admin = User.objects.create_superuser(username='a_bk', password='pwd', role='ADMIN')

        self.account = BankAccount.objects.create(
            user=self.customer,
            account_type='SAVINGS',
            balance=Decimal('1000.00')
        )
        self.other_account = BankAccount.objects.create(
            user=self.other_customer,
            account_type='SAVINGS',
            balance=Decimal('500.00')
        )
        self.loan = Loan.objects.create(
            account=self.account,
            amount=Decimal('800.00'),
            duration_months=6,
            status='PENDING'
        )

    def test_withdraw_backend_preserves_functionality(self):
        self.client.force_login(self.customer)
        response = self.client.post('/bank/withdraw/', {'amount': '200'})
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('800.00'))

    def test_transfer_backend_preserves_functionality(self):
        self.client.force_login(self.customer)
        response = self.client.post('/bank/transfer/', {
            'account_number': self.other_account.account_number,
            'amount': '300'
        })
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.other_account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('700.00'))
        self.assertEqual(self.other_account.balance, Decimal('800.00'))

    def test_fd_backend_preserves_functionality(self):
        self.client.force_login(self.customer)
        response = self.client.post('/bank/fd/', {'amount': '400', 'months': '12'})
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, Decimal('600.00'))
        self.assertTrue(FixedDeposit.objects.filter(account=self.account, amount=Decimal('400.00')).exists())

    def test_loan_application_backend_preserves_functionality(self):
        self.client.force_login(self.customer)
        response = self.client.post('/bank/loan/', {'amount': '1500', 'months': '12'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Loan.objects.filter(account=self.account, amount=Decimal('1500.00')).exists())

    def test_loan_approval_backend_preserves_functionality(self):
        self.client.force_login(self.admin)
        response = self.client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 302)
        self.loan.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')
        self.assertEqual(self.account.balance, Decimal('1800.00'))


class RealBrowserChromiumSubmitGuardTests(TestCase):
    """
    Automated browser integration tests executing real Chromium (Microsoft Edge)
    against the actual static/js/submit_guard.js script.
    Verifies:
    1. Single submit button and multi-submit button forms.
    2. Submitter name/value preservation without duplication in POST body.
    3. Form locking, button disabling, and cancellation behavior.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.edge_binary = None
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        ]
        for c in candidates:
            if os.path.isfile(c):
                cls.edge_binary = c
                break

    def setUp(self):
        if not self.edge_binary:
            self.skipTest("Chromium/Edge browser binary not available in test environment.")

    def _run_browser_submission(self, button_to_click):
        received = []
        js_path = os.path.join(settings.BASE_DIR, 'static', 'js', 'submit_guard.js')
        with open(js_path, 'r', encoding='utf-8') as f:
            js_code = f.read()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                html = f"""<!DOCTYPE html>
<html>
<head><title>Browser Test</title></head>
<body>
<form id="testForm" method="POST" action="/submit">
    <input type="text" name="account_number" value="ACC1001">
    <button type="submit" id="btnApprove" name="action" value="approve">Approve</button>
    <button type="submit" id="btnReject" name="action" value="reject">Reject</button>
</form>
<script>
{js_code}
</script>
<script>
window.addEventListener('DOMContentLoaded', () => {{
    setTimeout(() => {{
        const btn = document.getElementById('{button_to_click}');
        btn.click();
    }}, 50);
}});
</script>
</body>
</html>"""
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.end_headers()
                self.wfile.write(html.encode('utf-8'))

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
                received.append({'body': body, 'parsed': parsed})
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"OK")

            def log_message(self, format, *args):
                pass

        server = socketserver.TCPServer(('127.0.0.1', 0), Handler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever)
        t.daemon = True
        t.start()

        proc = subprocess.Popen([
            self.edge_binary,
            '--headless=new',
            '--disable-gpu',
            f'http://127.0.0.1:{port}/'
        ])

        start = time.time()
        while not received and time.time() - start < 10:
            time.sleep(0.1)

        proc.terminate()
        server.shutdown()

        if not received:
            self.fail("Browser did not dispatch form submission within timeout.")
        return received[0]

    def test_approve_button_submits_value_exactly_once(self):
        post = self._run_browser_submission('btnApprove')
        self.assertEqual(post['body'].count('action='), 1, f"action parameter must appear exactly once in raw POST body: {post['body']}")
        self.assertEqual(post['parsed'].get('action'), ['approve'], "Parsed 'action' must contain exactly one ['approve'] entry.")

    def test_reject_button_submits_value_exactly_once(self):
        post = self._run_browser_submission('btnReject')
        self.assertEqual(post['body'].count('action='), 1, f"action parameter must appear exactly once in raw POST body: {post['body']}")
        self.assertEqual(post['parsed'].get('action'), ['reject'], "Parsed 'action' must contain exactly one ['reject'] entry.")
