import os
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.test import TestCase, Client
from django.conf import settings
from core.security_scanner import (
    SecurityScanner,
    SecurityFinding,
    GitAuditFinding,
    GitScanUnavailableError,
    scan_file_content,
    scan_runtime_source,
    scan_git_history,
    redact_line,
    SECRET_PATTERNS
)

class SecurityEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_create_admin_endpoint_is_disabled(self):
        """Ensure /create-admin/ does NOT exist as an unauthenticated HTTP route."""
        response = self.client.get('/create-admin/')
        self.assertEqual(response.status_code, 404)

    def test_create_employee_endpoint_is_disabled(self):
        """Ensure /create-employee/ does NOT exist as an unauthenticated HTTP route."""
        response = self.client.get('/create-employee/')
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_access_to_dashboards_redirects_to_login(self):
        """Ensure anonymous visitors cannot view any dashboard."""
        dashboards = [
            '/customer/dashboard/',
            '/employee/dashboard/',
            '/admin-dashboard/',
        ]
        for url in dashboards:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/login/', response.url)


class SecretScannerRegexTests(TestCase):
    """
    Unit tests proving every configured regular expression pattern compiles
    and reliably detects required credential assignment forms, including prefixes like ADMIN_SECRET.
    """

    def test_compiled_patterns_detect_all_standard_credential_forms(self):
        test_cases = [
            ("password = 'SYNTHETIC_PASS_123'", "ASSIGNED_CREDENTIAL"),
            ("SECRET_KEY = 'SYNTHETIC_KEY_456'", "ASSIGNED_CREDENTIAL"),
            ("ADMIN_SECRET = 'SYNTHETIC_ADMIN_SEC_999'", "ASSIGNED_CREDENTIAL"),
            ("APP_SECRET = 'SYNTHETIC_APP_SEC_888'", "ASSIGNED_CREDENTIAL"),
            ("api_key = 'SYNTHETIC_API_789'", "ASSIGNED_CREDENTIAL"),
            ("api-key = 'SYNTHETIC_API_789'", "ASSIGNED_CREDENTIAL"),
            ("access_token = 'SYNTHETIC_TOKEN_ABC'", "ASSIGNED_CREDENTIAL"),
            ("auth_token = 'SYNTHETIC_AUTH_DEF'", "ASSIGNED_CREDENTIAL"),
            ("private_key = 'SYNTHETIC_PRIV_GHI'", "ASSIGNED_CREDENTIAL"),
            ("user.set_password('SYNTHETIC_SETPASS_JKL')", "SET_PASSWORD_CREDENTIAL"),
            ("def create_admin(request):", "DISALLOWED_BOOTSTRAP_FUNCTION"),
            ("def create_employee(request):", "DISALLOWED_BOOTSTRAP_FUNCTION"),
        ]

        for code_snippet, expected_rule in test_cases:
            with self.subTest(code=code_snippet, rule=expected_rule):
                findings = scan_file_content(code_snippet, filename="sample.py")
                self.assertEqual(len(findings), 1, f"Failed to detect pattern in: {code_snippet}")
                self.assertEqual(findings[0].rule_id, expected_rule)

    def test_redact_line_replaces_all_credentials_on_same_line(self):
        """
        Verify redact_line replaces all secret literals on a line, including multiple secrets,
        without greedy syntax corruption.
        """
        raw_line = "SECRET_KEY = 'SYNTHETIC_VERY_SECRET_KEY' # config parameter"
        redacted = redact_line(raw_line)
        self.assertIn("[REDACTED_CREDENTIAL]", redacted)
        self.assertNotIn("SYNTHETIC_VERY_SECRET_KEY", redacted)
        self.assertEqual(redacted, "SECRET_KEY = '[REDACTED_CREDENTIAL]' # config parameter")

        multi_credential_line = "password = 'SYNTHETIC_FIRST_SECRET'; access_token = 'SYNTHETIC_SECOND_SECRET'"
        redacted_multi = redact_line(multi_credential_line)
        self.assertIn("password = '[REDACTED_CREDENTIAL]'", redacted_multi)
        self.assertIn("access_token = '[REDACTED_CREDENTIAL]'", redacted_multi)
        self.assertNotIn("SYNTHETIC_FIRST_SECRET", redacted_multi)
        self.assertNotIn("SYNTHETIC_SECOND_SECRET", redacted_multi)


class SecretScannerUnitTests(TestCase):
    """
    Unit tests proving the security scanner detects secrets, redacts them unconditionally,
    never leaks raw credential strings, handles allowlist pragmas, and handles Git failure modes.
    """

    def test_allowlist_pragma_produces_zero_findings_on_standard_password_assignment(self):
        """
        Verify that an explicit allowlist pragma directive on a standard credential assignment
        produces zero findings.
        """
        code = "password = 'SYNTHETIC_ALLOWLIST_SECRET' # pragma: allowlist secret"
        findings = scan_file_content(code, filename="allowlist_test.py")
        self.assertEqual(len(findings), 0, "Line with allowlist pragma must produce zero findings.")

    def test_scanner_detects_dummy_secret_and_redacts_without_exposing_value(self):
        raw_dummy_secret = "SYNTHETIC_DUMMY_SECRET_XYZ_998877"
        dummy_code = (
            f"ADMIN_SECRET = '{raw_dummy_secret}'\n"
            f"password = 'SYNTHETIC_DUMMY_PASSWORD_ABC'\n"
            f"# pragma: allowlist secret\n"
            f"EXEMPT_KEY = 'allowed_key'\n"
        )

        findings = scan_file_content(dummy_code, filename="test_dummy.py")
        self.assertGreaterEqual(len(findings), 2, "Scanner must detect both un-exempt secrets.")

        for finding in findings:
            self.assertIn("[REDACTED_CREDENTIAL]", finding.redacted_line)
            # CRITICAL: Assert raw secret values are nowhere in the finding representations
            self.assertNotIn(raw_dummy_secret, finding.redacted_line)
            self.assertNotIn(raw_dummy_secret, str(finding))
            self.assertNotIn(raw_dummy_secret, repr(finding))
            self.assertNotIn('SYNTHETIC_DUMMY_PASSWORD_ABC', finding.redacted_line)
            self.assertNotIn('SYNTHETIC_DUMMY_PASSWORD_ABC', str(finding))
            self.assertNotIn('SYNTHETIC_DUMMY_PASSWORD_ABC', repr(finding))

        allowed_findings = [f for f in findings if "EXEMPT_KEY" in f.redacted_line]
        self.assertEqual(len(allowed_findings), 0, "Line with allowlist pragma must not be flagged.")

    def test_python_discovery_uses_rglob_and_handles_missing_target_directory(self):
        """
        Verify rglob discovery on nested files and verify missing target dir raises FileNotFoundError.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_path = Path(tmp_dir)
            nested_pkg = base_path / 'nested' / 'subpkg'
            nested_pkg.mkdir(parents=True)
            nested_file = nested_pkg / 'deep_module.py'
            nested_file.write_text("password = 'SYNTHETIC_NESTED_PASS'", encoding='utf-8')

            findings = scan_runtime_source(base_dir=base_path, target_dirs=['nested'])
            self.assertEqual(len(findings), 1)
            self.assertIn("nested/subpkg/deep_module.py".replace("/", os.sep), findings[0].file_path)

            with self.assertRaises(FileNotFoundError):
                scan_runtime_source(base_dir=base_path, target_dirs=['non_existent_directory'])

    def test_git_scanner_handles_all_failure_modes_without_leaking_raw_stderr(self):
        """
        Verify Git timeout, missing binary, non-zero exits, and invalid repo raise GitScanUnavailableError,
        and verify raw stderr or sensitive details are never exposed in exception messages.
        """
        repo_path = Path(settings.BASE_DIR)

        with patch('subprocess.run', side_effect=FileNotFoundError("git binary not found")):
            with self.assertRaises(GitScanUnavailableError) as ctx:
                scan_git_history(repo_dir=repo_path)
            self.assertNotIn("git binary not found", str(ctx.exception))
            self.assertIn("not found in system PATH", str(ctx.exception))

        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['git', 'log'], 15)):
            with self.assertRaises(GitScanUnavailableError) as ctx:
                scan_git_history(repo_dir=repo_path)
            self.assertIn("timed out", str(ctx.exception))

        sensitive_fake_stderr = "fatal: failed to read /path/with/SECRET_TOKEN_IN_STDERR"
        with patch('subprocess.run', side_effect=subprocess.CalledProcessError(128, ['git', 'log'], stderr=sensitive_fake_stderr)):
            with self.assertRaises(GitScanUnavailableError) as ctx:
                scan_git_history(repo_dir=repo_path)
            # CRITICAL: Raw stderr must NOT be exposed in exception message
            self.assertNotIn(sensitive_fake_stderr, str(ctx.exception))
            self.assertNotIn("SECRET_TOKEN_IN_STDERR", str(ctx.exception))
            self.assertIn("exit status 128", str(ctx.exception))

    def test_isolated_temporary_git_repository_diff_scanning_with_root(self):
        """
        Create a temporary isolated Git repo, commit a synthetic dummy credential in the root commit,
        and prove that scan_git_history with --root detects it, sets rule_id, and redacts it.
        """
        with tempfile.TemporaryDirectory() as tmp_git_dir:
            repo_dir = Path(tmp_git_dir)
            try:
                subprocess.run(['git', 'init'], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(['git', 'config', 'user.name', 'Test Scanner'], cwd=repo_dir, check=True, capture_output=True)
                subprocess.run(['git', 'config', 'user.email', 'scanner@simulated.bebank.local'], cwd=repo_dir, check=True, capture_output=True)
            except (FileNotFoundError, subprocess.CalledProcessError) as err:
                self.skipTest(f"Git CLI unavailable in test environment for temporary repo creation: {err}")

            secret_file = repo_dir / "settings_mock.py"
            raw_dummy_secret = "SYNTHETIC_TEMP_GIT_SECRET_776655"
            secret_file.write_text(f"ADMIN_SECRET = '{raw_dummy_secret}'\n", encoding='utf-8')

            subprocess.run(['git', 'add', '.'], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(['git', 'commit', '-m', 'Initial root commit with mock settings'], cwd=repo_dir, check=True, capture_output=True)

            audit_findings = scan_git_history(repo_dir=repo_dir, max_commits=None)
            self.assertGreaterEqual(len(audit_findings), 1, "Must detect credential in root commit via --root flag")

            for finding in audit_findings:
                self.assertEqual(finding.rule_id, "ASSIGNED_CREDENTIAL")
                self.assertIn("[REDACTED_CREDENTIAL]", finding.redacted_diff_line)
                self.assertNotIn(raw_dummy_secret, finding.redacted_diff_line)
                self.assertNotIn(raw_dummy_secret, str(finding))
                self.assertNotIn(raw_dummy_secret, repr(finding))


class CodebaseSecretScanningTests(TestCase):
    """
    Project-wide integration tests scanning all runtime Python files and tracked Git history.
    """

    def test_runtime_codebase_contains_no_unredacted_credentials(self):
        """
        Comprehensive runtime source scan: recursively inspects all Python files
        in core/, accounts/, and banking/. Must fail on existing codebase until
        hardcoded credentials in accounts/views.py are removed.
        """
        target_dirs = ['core', 'accounts', 'banking']
        findings = scan_runtime_source(
            base_dir=Path(settings.BASE_DIR),
            target_dirs=target_dirs,
            exclude_tests=True
        )

        if findings:
            report_lines = [f"{f.file_path}:{f.line_number} -> {f.redacted_line}" for f in findings]
            failure_msg = (
                f"Security scan detected {len(findings)} unredacted credential violation(s) "
                f"in runtime Python code:\n" + "\n".join(report_lines)
            )
            self.fail(failure_msg)

    def test_tracked_git_history_audit_identifies_credentials_safely(self):
        """
        Inspects sample tracked Git history (most recent 30 commits).
        Verifies that findings include rule_id, credential lines are redacted,
        and disallowed function definitions are detected without requiring redaction markers.
        """
        try:
            audit_report = scan_git_history(repo_dir=Path(settings.BASE_DIR), max_commits=30)
            self.assertGreater(len(audit_report), 0, "Git history audit should find legacy commits in history.")
            for item in audit_report:
                self.assertIsInstance(item.rule_id, str)
                if item.rule_id in ['ASSIGNED_CREDENTIAL', 'SET_PASSWORD_CREDENTIAL']:
                    self.assertIn("[REDACTED_CREDENTIAL]", item.redacted_diff_line)
                elif item.rule_id == 'DISALLOWED_BOOTSTRAP_FUNCTION':
                    # Disallowed function definitions are not secrets and do not require redaction markers
                    self.assertTrue(
                        'create_admin' in item.redacted_diff_line or 'create_employee' in item.redacted_diff_line,
                        f"Disallowed function finding had unexpected content: {item.redacted_diff_line}"
                    )
        except GitScanUnavailableError as e:
            self.fail(f"Git history audit failed because Git environment is unavailable: {e}")
