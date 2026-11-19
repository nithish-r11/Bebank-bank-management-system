# Phase 0: Security Hardening & Zero-Downtime Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate all critical security vulnerabilities from the existing Bebank Django system by removing public administrative creation endpoints, enforcing strict Role-Based Access Control (RBAC) and object-level ownership authorization across all views, securing administrative mutations as POST-only with CSRF protection, introducing an atomic loan approval engine with row-level locks, barrier-synchronized PostgreSQL concurrency tests, enforcing Django Admin authorization requiring BOTH `is_staff=True` AND `role='ADMIN'` (strictly isolating employees and customers), creating a secure bootstrap CLI command with password validation, implementing a comprehensive safe secret scanner across all runtime Python source files and tracked Git history with guaranteed named-span redaction (`[REDACTED_CREDENTIAL]`) and fail-fast Git availability handling, and adding client-side duplicate submission safeguards.

**Architecture:** We use an incremental hardening approach preserving 100% of existing Django templates and views. We introduce modular RBAC decorators in `accounts/decorators.py` that check authentication, account lock status, and role hierarchy (`CUSTOMER`, `EMPLOYEE`, `ADMIN`), supplemented by object-level ownership checks ensuring customers access solely their own accounts, loans, and statements. Unauthenticated routes are removed from `accounts/urls.py` and replaced with an offline Django management command. Loan approval mutations are wrapped in `@transaction.atomic` using `select_for_update()` to prevent double-crediting, with automated rollback tests on the actual endpoint and barrier-synchronized PostgreSQL concurrency tests using `TransactionTestCase`. Django Admin is locked down using a custom `has_permission` check enforcing both `is_staff=True` and `role='ADMIN'`. A dedicated `core/security_scanner.py` utility handles runtime source scanning and tracked Git history audits with strict named-span redaction.

**Tech Stack:** Python 3.13, Django 6.0.3, Django TestCase / TransactionTestCase / Client, PostgreSQL (Neon / local) for concurrency tests, Bootstrap 5 templates.

**Spec:** [`bebank-2.0-implementation-design.md`](file:///C:/Users/nithi/.gemini/antigravity/brain/c4ccf6e4-86f4-4ad8-82e9-fcbc7d4e66c3/bebank-2.0-implementation-design.md)

## Global Constraints

- **Virtual Simulation Only**: Bebank is strictly a simulation. No real money, payment gateways, or real identity credentials.
- **Simulated KYC Only**: Never collect or validate real Aadhaar or PAN. Use mock simulated identifiers only (`SIM-0000-0000-0000`, `SIMPN0000X`).
- **No Regressions**: All 15 existing Django templates and customer/employee/admin web workflows must remain operational.
- **Strict TDD**: Write failing tests first (RED), verify failure, implement minimal code to pass (GREEN), then REFACTOR before committing.
- **Comprehensive Safe Secret Scanning**:
  - Runtime scanning must inspect **all** relevant Python source files across `core/`, `accounts/`, and `banking/` using `rglob("*.py")`. Missing target directories must raise an error, never producing a silent pass.
  - Tracked Git history scanning must inspect commit diffs to identify historical hardcoded credentials. A scan of $N$ commits must be explicitly documented as a sample/shallow scan, while full history scan must inspect all commits.
  - **Zero Secret Exposure**: All detected credentials must have their exact secret value replaced with `[REDACTED_CREDENTIAL]` using named-group replacement. Real secret values, passwords, or tokens must NEVER appear in terminal logs, test failures, exception messages, reports, or artifacts.
  - **Synthetic Test Fixtures Only**: Never put actual historical credentials or their fragments in tests or assertions; all test fixtures must use synthetic dummy strings (e.g., `SYNTHETIC_DUMMY_SECRET_XYZ`).
  - **Fail-Fast Git Verification**: The scanner must explicitly handle Git timeouts, missing executables, invalid repositories, and non-zero exit codes by raising `GitScanUnavailableError`; it must **never** report a successful or clean audit when scanning was skipped or failed.
  - **Limitations & False Positives**: Static regex-based scanning detects plaintext password assignments, known bootstrap patterns, and secret keys, but cannot detect runtime-constructed or encrypted credentials. Documented inline suppression (`# pragma: allowlist secret`) is supported for verified test fixtures and false positives.
- **Object-Level Ownership Authorization (IDOR Protection)**:
  - In addition to role checks, views must strictly verify that authenticated customers own the accessed bank account, loan, transaction, or statement. Cross-customer access attempts must receive HTTP 403 or 404.
- **Django Admin Dual-Key Authorization**:
  - Access to Django Admin (`/admin/`) strictly requires **BOTH** `is_staff=True` AND `role='ADMIN'`.
  - Authenticated `EMPLOYEE` users must have `is_staff=False` by default and must be denied access to `/admin/`. Even if `is_staff=True` is granted, an `EMPLOYEE` or `CUSTOMER` is rejected because `role != 'ADMIN'`.
  - Authenticated `CUSTOMER` users must be rejected from `/admin/`.
- **Loan Approval Atomicity & Barrier-Synchronized Concurrency**:
  - Wrap loan approval in `@transaction.atomic` with `select_for_update()` on both `Loan` and `BankAccount` rows.
  - Test the actual `approve_loan` endpoint with exception injection after balance update and transaction creation to verify that loan status, account balance, and transaction records all roll back completely.
  - Concurrency tests must use `TransactionTestCase` on PostgreSQL with independent database connections per worker thread and `threading.Barrier` synchronization to guarantee simultaneous request dispatch. **Do not claim SQLite verifies row locking** (SQLite uses database-level locking or no-ops for `select_for_update`).
- **Zero In-Flight Migrations in Phase 0**: Phase 0 uses existing database schema fields only; no schema migrations in Phase 0.
- **Git Safety Protocol**:
  - Always run `git status --short` first.
  - If uncommitted changes exist, display filenames only.
  - Never automatically stash, revert, or discard changes.
  - Rollback requires a clean working tree and explicit user approval before switching branches.
  - Never push to GitHub (`git push` is strictly forbidden).

## Review Focus

1. **Secret Scanner Precision & Zero Leakage**:
   - Verify scanner compiles all patterns with `re.compile()`, replaces exact matched secret values using named capture groups, and never leaks raw values in `str()`, `repr()`, or exception reports.
   - Verify file discovery uses `rglob("*.py")` and fails fast on missing target directories.
   - Verify isolated temporary Git repository test proves detection of synthetic dummy secrets.
   - Verify Git failure modes (timeouts, missing binary, non-zero exits) raise `GitScanUnavailableError`.
2. **Object-Level Ownership Authorization (IDOR Protection)**:
   - Verify Customer A cannot view or download Customer B's passbook, transactions, loan records, or withdraw from Customer B's account.
3. **Barrier-Synchronized Concurrency & Rollback**:
   - Verify `approve_loan` rolls back all changes upon exception.
   - Verify PostgreSQL concurrent threads synchronized via `threading.Barrier` result in exactly one approval and one credit.
4. **Django Admin Dual-Key Authorization**:
   - Verify both `is_staff=True` AND `role='ADMIN'` are required. Rejection of `EMPLOYEE` and `CUSTOMER` even if `is_staff=True`.
5. **CSRF Enforcement**:
   - Administrative mutations tested with `Client(enforce_csrf_checks=True)` for both rejection without token (403) and success with token (302).

---

## File Structure & Responsibilities

```
accounts/
├── admin.py                           # Custom AdminSite / has_permission hook (is_staff=True AND role='ADMIN')
├── management/
│   ├── __init__.py
│   └── commands/
│       ├── __init__.py
│       └── bootstrap_system_users.py   # Secure admin & employee seeding (employee is_staff=False)
├── tests/
│   ├── __init__.py
│   ├── test_security.py               # Comprehensive secret scanning, regex tests, public endpoint removal & auth redirect tests
│   ├── test_rbac.py                   # Role-based access control & Django admin dual-key isolation tests
│   └── test_bootstrap.py              # Management command validation & password policy tests
├── decorators.py                      # Reusable @login_required_with_lock_check and @role_required
├── views.py                           # Decorated views; removed public admin views
└── urls.py                            # Cleaned URLs without create-admin/create-employee

banking/
├── tests/
│   ├── __init__.py
│   ├── test_rbac.py                   # Banking views access control & object-level ownership (IDOR) tests
│   └── test_loan_actions.py           # Atomic loan approval, endpoint rollback injection, barrier PostgreSQL concurrency & CSRF tests
├── views.py                           # Decorated banking views; object ownership checks; atomic select_for_update loan actions
└── urls.py                            # Routed loan approval/rejection endpoints

core/
├── admin.py                           # Admin site configuration (enforces is_staff=True AND role='ADMIN')
└── security_scanner.py                # Reusable SecurityScanner class & functions with named-span redaction and fail-fast Git handling

templates/
├── admin_dashboard.html               # Converted approval/rejection links to POST forms with CSRF
└── (transfer/withdraw/deposit).html   # Form double-submission UX safeguard scripts
```

---

## Phased Tasks

### Task 0: Git Environment Verification & User Approval Gate (COMPLETED)

**Files:**
- Repository Root

**Status:**
- [x] Step 1: `git status --short` verified untracked plan file preserved.
- [x] Step 2: Read-only branch check confirmed `feature/phase-0-security-hardening` did not exist.
- [x] Step 3: Verified current branch was `main`.
- [x] Step 4: With explicit user authorization, checked out new feature branch: `feature/phase-0-security-hardening`.

---

### Task 1: Security Test Suite Foundation & Resilient Safe Secret Scanner (RED)

**Files:**
- Create: `accounts/tests/__init__.py`
- Create: `accounts/tests/test_security.py`
- Create: `core/security_scanner.py`

**Interfaces:**
- Consumes: Django Test Client, `django.urls.reverse`, `Path`, `subprocess`, `tempfile`
- Produces: Base security test coverage for public endpoints, auth redirects, comprehensive runtime secret scanner covering `core/`, `accounts/`, and `banking/` via `rglob("*.py")`, and Git history scanner with mandatory named-span redaction and fail-fast error handling

- [ ] **Step 1: Write comprehensive security & secret scan tests in `accounts/tests/test_security.py` [RED]**

```python
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
    and reliably detects required credential assignment forms.
    """

    def test_compiled_patterns_detect_all_standard_credential_forms(self):
        test_cases = [
            ("password = 'SYNTHETIC_PASS_123'", "ASSIGNED_CREDENTIAL"),
            ("SECRET_KEY = 'SYNTHETIC_KEY_456'", "ASSIGNED_CREDENTIAL"),
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

    def test_redact_line_replaces_exact_secret_value_without_greedy_corruption(self):
        """
        Verify redact_line replaces only the secret literal, preserving line syntax and other strings.
        """
        raw_line = "SECRET_KEY = 'SYNTHETIC_VERY_SECRET_KEY' # config parameter"
        redacted = redact_line(raw_line)
        self.assertIn("[REDACTED_CREDENTIAL]", redacted)
        self.assertNotIn("SYNTHETIC_VERY_SECRET_KEY", redacted)
        self.assertEqual(redacted, "SECRET_KEY = '[REDACTED_CREDENTIAL]' # config parameter")

        multi_string_line = "user = 'admin'; password = 'SYNTHETIC_SECRET_PASS'; role = 'ADMIN'"
        redacted_multi = redact_line(multi_string_line)
        self.assertIn("user = 'admin'", redacted_multi)
        self.assertIn("role = 'ADMIN'", redacted_multi)
        self.assertIn("[REDACTED_CREDENTIAL]", redacted_multi)
        self.assertNotIn("SYNTHETIC_SECRET_PASS", redacted_multi)


class SecretScannerUnitTests(TestCase):
    """
    Unit tests proving the security scanner detects secrets, redacts them unconditionally,
    never leaks raw credential strings, and handles Git failure modes explicitly.
    """

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

            # Successful discovery of nested file
            findings = scan_runtime_source(base_dir=base_path, target_dirs=['nested'])
            self.assertEqual(len(findings), 1)
            self.assertIn("nested/subpkg/deep_module.py".replace("/", os.sep), findings[0].file_path)

            # Missing target directory must raise FileNotFoundError, not silently pass
            with self.assertRaises(FileNotFoundError):
                scan_runtime_source(base_dir=base_path, target_dirs=['non_existent_directory'])

    def test_git_scanner_handles_all_failure_modes_explicitly(self):
        """
        Verify Git timeout, missing binary, non-zero exits, and invalid repo raise GitScanUnavailableError.
        """
        repo_path = Path(settings.BASE_DIR)

        with patch('subprocess.run', side_effect=FileNotFoundError("git binary not found")):
            with self.assertRaises(GitScanUnavailableError):
                scan_git_history(repo_dir=repo_path)

        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(['git', 'log'], 10)):
            with self.assertRaises(GitScanUnavailableError):
                scan_git_history(repo_dir=repo_path)

        with patch('subprocess.run', side_effect=subprocess.CalledProcessError(128, ['git', 'log'], stderr="fatal: not a git repository")):
            with self.assertRaises(GitScanUnavailableError):
                scan_git_history(repo_dir=repo_path)

    def test_isolated_temporary_git_repository_diff_scanning(self):
        """
        Create a temporary isolated Git repo, commit a synthetic dummy credential,
        and prove that scan_git_history detects it and redacts it without leaking raw value.
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
            secret_file.write_text(f"SECRET_KEY = '{raw_dummy_secret}'\n", encoding='utf-8')

            subprocess.run(['git', 'add', '.'], cwd=repo_dir, check=True, capture_output=True)
            subprocess.run(['git', 'commit', '-m', 'Add mock settings'], cwd=repo_dir, check=True, capture_output=True)

            audit_findings = scan_git_history(repo_dir=repo_dir, max_commits=None)
            self.assertGreaterEqual(len(audit_findings), 1)

            for finding in audit_findings:
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
        Verifies that any historical findings are cleanly redacted as [REDACTED_CREDENTIAL].
        """
        try:
            audit_report = scan_git_history(repo_dir=Path(settings.BASE_DIR), max_commits=30)
            for item in audit_report:
                self.assertIn("[REDACTED_CREDENTIAL]", item.redacted_diff_line)
        except GitScanUnavailableError as e:
            self.fail(f"Git history audit failed because Git environment is unavailable: {e}")
```

- [ ] **Step 2: Implement `core/security_scanner.py` with compiled regexes, named spans, and SecurityScanner class [GREEN Foundation]**

```python
import os
import re
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional

class GitScanUnavailableError(Exception):
    """Raised when Git CLI is unavailable or cannot inspect repository history."""
    pass

@dataclass
class SecurityFinding:
    file_path: str
    line_number: int
    rule_id: str
    redacted_line: str

    def __str__(self):
        return f"{self.file_path}:{self.line_number} [{self.rule_id}] {self.redacted_line}"

    def __repr__(self):
        return f"SecurityFinding(file_path={self.file_path!r}, line={self.line_number}, rule={self.rule_id!r}, redacted={self.redacted_line!r})"

@dataclass
class GitAuditFinding:
    commit_sha: str
    file_path: str
    redacted_diff_line: str

    def __str__(self):
        return f"commit {self.commit_sha} ({self.file_path}): {self.redacted_diff_line}"

    def __repr__(self):
        return f"GitAuditFinding(sha={self.commit_sha!r}, file={self.file_path!r}, redacted={self.redacted_diff_line!r})"


SECRET_PATTERNS = [
    # Plaintext assignments: password = '...', secret_key = '...', api_key = '...', etc.
    (
        re.compile(r'''(?i)\b(password|passwd|secret_key|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\s*[:=]\s*(['"])(?P<secret>[^\r\n'"]+)\2'''),
        'ASSIGNED_CREDENTIAL'
    ),
    # Method calls: set_password('...')
    (
        re.compile(r'''(?i)\bset_password\s*\(\s*(['"])(?P<secret>[^\r\n'"]+)\1\s*\)'''),
        'SET_PASSWORD_CREDENTIAL'
    ),
    # Disallowed bootstrap endpoint functions
    (
        re.compile(r'''\bdef\s+(?P<func>create_admin|create_employee)\b'''),
        'DISALLOWED_BOOTSTRAP_FUNCTION'
    ),
]

ALLOWLIST_PRAGMA = '# pragma: allowlist secret'

def redact_line(raw_line: str) -> str:
    """
    Replaces the exact matched secret value with [REDACTED_CREDENTIAL] using named capture spans.
    Preserves all surrounding syntax.
    """
    redacted = raw_line.rstrip('\r\n')
    for pattern, rule_id in SECRET_PATTERNS:
        if 'DISALLOWED' in rule_id:
            continue
        match = pattern.search(redacted)
        if match and 'secret' in match.groupdict():
            start, end = match.span('secret')
            redacted = redacted[:start] + "[REDACTED_CREDENTIAL]" + redacted[end:]
    return redacted

class SecurityScanner:
    """Security Scanner class encapsulating runtime code and Git history audit capabilities."""

    @staticmethod
    def scan_content(content: str, filename: str = "") -> List[SecurityFinding]:
        findings = []
        lines = content.splitlines()

        for idx, line in enumerate(lines, start=1):
            if ALLOWLIST_PRAGMA in line:
                continue
            for pattern, rule_id in SECRET_PATTERNS:
                if pattern.search(line):
                    masked = redact_line(line)
                    findings.append(SecurityFinding(
                        file_path=filename,
                        line_number=idx,
                        rule_id=rule_id,
                        redacted_line=masked
                    ))
                    break
        return findings

    @staticmethod
    def scan_runtime(base_dir: Path, target_dirs=('core', 'accounts', 'banking'), exclude_tests: bool = True) -> List[SecurityFinding]:
        all_findings = []
        for target in target_dirs:
            dir_path = base_dir / target
            if not dir_path.is_dir():
                raise FileNotFoundError(f"Target directory for scanning '{dir_path}' does not exist.")
            for py_file in dir_path.rglob("*.py"):
                if exclude_tests and ('tests' in py_file.parts or py_file.name.startswith('test_')):
                    continue
                if any(part in py_file.parts for part in ('.venv', 'venv', '__pycache__', 'migrations')):
                    continue
                content = py_file.read_text(encoding='utf-8', errors='replace')
                rel_path = str(py_file.relative_to(base_dir))
                file_findings = SecurityScanner.scan_content(content, filename=rel_path)
                all_findings.extend(file_findings)
        return all_findings

    @staticmethod
    def scan_git_history(repo_dir: Path, max_commits: Optional[int] = 50) -> List[GitAuditFinding]:
        cmd = ['git', 'log', '-p', '--no-color']
        if max_commits is not None:
            cmd.extend(['-n', str(max_commits)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(repo_dir),
                timeout=15,
                check=True
            )
        except subprocess.TimeoutExpired as err:
            raise GitScanUnavailableError(f"Git log command timed out after 15 seconds: {err}") from err
        except FileNotFoundError as err:
            raise GitScanUnavailableError(f"Git executable not found in PATH: {err}") from err
        except subprocess.CalledProcessError as err:
            raise GitScanUnavailableError(f"Git log failed with exit code {err.returncode}: {err.stderr}") from err

        audit_findings = []
        current_sha = "unknown"
        current_file = "unknown"

        for line in result.stdout.splitlines():
            if line.startswith("commit "):
                parts = line.split()
                if len(parts) > 1:
                    current_sha = parts[1][:8]
            elif line.startswith("+++ b/"):
                current_file = line[6:]
            elif line.startswith("+") and not line.startswith("+++"):
                added_content = line[1:].strip()
                if ALLOWLIST_PRAGMA in added_content:
                    continue
                for pattern, rule_id in SECRET_PATTERNS:
                    if pattern.search(added_content):
                        masked = redact_line(added_content)
                        audit_findings.append(GitAuditFinding(
                            commit_sha=current_sha,
                            file_path=current_file,
                            redacted_diff_line=masked
                        ))
                        break

        return audit_findings

# Standalone functional aliases
scan_file_content = SecurityScanner.scan_content
scan_runtime_source = SecurityScanner.scan_runtime
scan_git_history = SecurityScanner.scan_git_history
```

- [ ] **Step 3: Run tests to verify they fail on existing codebase [RED Verification]**

Run: `python manage.py test accounts.tests.test_security`  
Expected: **FAIL** (returns 200 on `/create-admin/`, and `test_runtime_codebase_contains_no_unredacted_credentials` detects hardcoded credentials in `accounts/views.py` with redacted error output).

- [ ] **Step 4: Commit initial failing test suite and scanner engine**

```bash
git add core/security_scanner.py accounts/tests/
git commit -m "test(security): add comprehensive secret scanner engine and security test suite"
```

---

### Task 2: Remove Public Admin & Employee HTTP Endpoints (GREEN)

**Files:**
- Modify: `accounts/urls.py`
- Modify: `accounts/views.py`
- Test: `accounts/tests/test_security.py`

**Interfaces:**
- Consumes: `accounts.urls`
- Produces: Cleaned routing table with no unauthenticated bootstrap endpoints and sanitized source across all project files

- [ ] **Step 1: Remove `create-admin/` and `create-employee/` routes in `accounts/urls.py`**

Remove lines 18–20:
```python
# DELETE THESE LINES:
# path('create-admin/', views.create_admin),
# path('create-employee/', views.create_employee),
```

- [ ] **Step 2: Remove insecure view functions in `accounts/views.py`**

Delete `create_admin(request)` (lines 150–160) and `create_employee(request)` (lines 162–172).

- [ ] **Step 3: Run security tests to verify passes [GREEN Verification]**

Run: `python manage.py test accounts.tests.test_security`  
Expected: **PASS** (HTTP 404 on both endpoints, all runtime source scans clean with zero violations).

- [ ] **Step 4: Refactor and clean unused imports in `accounts/views.py` [REFACTOR]**

Remove `HttpResponse` import if no longer required.

- [ ] **Step 5: Commit endpoint removal**

```bash
git add accounts/urls.py accounts/views.py
git commit -m "fix(security): remove unauthenticated admin and employee creation endpoints"
```

---

### Task 3: Secure Bootstrap Command with Password Validation & Employee Isolation (RED $\rightarrow$ GREEN $\rightarrow$ REFACTOR)

**Files:**
- Create: `accounts/management/__init__.py`
- Create: `accounts/management/commands/__init__.py`
- Create: `accounts/management/commands/bootstrap_system_users.py`
- Create: `accounts/tests/test_bootstrap.py`

**Interfaces:**
- Consumes: Environment variables (`BOOTSTRAP_ADMIN_USERNAME`, `BOOTSTRAP_ADMIN_PASSWORD`, `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_EMPLOYEE_USERNAME`, `BOOTSTRAP_EMPLOYEE_PASSWORD`), `django.contrib.auth.password_validation.validate_password`, `accounts.models.User`
- Produces: CLI management command `python manage.py bootstrap_system_users` ensuring Employee accounts have `is_staff=False`

- [ ] **Step 1: Write the failing tests in `accounts/tests/test_bootstrap.py` [RED]**

```python
import os
from unittest.mock import patch
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.test import TestCase
from accounts.models import User

class BootstrapCommandTests(TestCase):
    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'secureadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecurePassword2026!',
        'BOOTSTRAP_ADMIN_EMAIL': 'admin@simulated.bebank.local',
        'BOOTSTRAP_EMPLOYEE_USERNAME': 'secureemployee',
        'BOOTSTRAP_EMPLOYEE_PASSWORD': 'EmployeeSecurePass2026!',
        'BOOTSTRAP_EMPLOYEE_EMAIL': 'emp@simulated.bebank.local',
    })
    def test_bootstrap_creates_admin_and_isolated_employee(self):
        call_command('bootstrap_system_users')
        admin = User.objects.get(username='secureadmin')
        self.assertEqual(admin.role, 'ADMIN')
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password('SuperSecurePassword2026!'))

        # CRITICAL: Employee must NOT have Django Admin staff access
        emp = User.objects.get(username='secureemployee')
        self.assertEqual(emp.role, 'EMPLOYEE')
        self.assertFalse(emp.is_staff, "Employee must have is_staff=False by default.")
        self.assertFalse(emp.is_superuser)
        self.assertTrue(emp.check_password('EmployeeSecurePass2026!'))

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'weakadmin',
        'BOOTSTRAP_ADMIN_PASSWORD': '123',  # Violates minimum length
    })
    def test_bootstrap_validates_password_strength(self):
        with self.assertRaises(ValidationError):
            call_command('bootstrap_system_users')

    @patch.dict(os.environ, {
        'BOOTSTRAP_ADMIN_USERNAME': 'existinguser',
        'BOOTSTRAP_ADMIN_PASSWORD': 'SuperSecurePassword2026!',
    })
    def test_bootstrap_fails_safely_if_user_has_wrong_role(self):
        User.objects.create_user(username='existinguser', password='pwd', role='CUSTOMER')
        with self.assertRaises(ValueError):
            call_command('bootstrap_system_users')

    @patch.dict(os.environ, {}, clear=True)
    def test_bootstrap_fails_if_env_missing(self):
        with self.assertRaises(ValueError):
            call_command('bootstrap_system_users')
```

- [ ] **Step 2: Run test to verify it fails [RED Verification]**

Run: `python manage.py test accounts.tests.test_bootstrap`  
Expected: **FAIL** (`CommandError: Unknown command: 'bootstrap_system_users'`).

- [ ] **Step 3: Implement `bootstrap_system_users.py` with `is_staff=False` for employees [GREEN]**

```python
import os
from django.core.management.base import BaseCommand
from django.contrib.auth.password_validation import validate_password
from accounts.models import User

class Command(BaseCommand):
    help = "Securely bootstrap initial system administrator and employee with password validation."

    def handle(self, *args, **options):
        admin_user = os.environ.get('BOOTSTRAP_ADMIN_USERNAME')
        admin_pass = os.environ.get('BOOTSTRAP_ADMIN_PASSWORD')
        admin_email = os.environ.get('BOOTSTRAP_ADMIN_EMAIL', 'admin@simulated.bebank.local')

        emp_user = os.environ.get('BOOTSTRAP_EMPLOYEE_USERNAME')
        emp_pass = os.environ.get('BOOTSTRAP_EMPLOYEE_PASSWORD')
        emp_email = os.environ.get('BOOTSTRAP_EMPLOYEE_EMAIL', 'employee@simulated.bebank.local')

        if not admin_user or not admin_pass:
            raise ValueError("BOOTSTRAP_ADMIN_USERNAME and BOOTSTRAP_ADMIN_PASSWORD environment variables are required.")

        # 1. Validate Admin password policy
        validate_password(admin_pass)

        # 2. Check existing Admin user state
        existing_admin = User.objects.filter(username=admin_user).first()
        if existing_admin:
            if existing_admin.role != 'ADMIN' or not existing_admin.is_superuser or not existing_admin.is_staff:
                raise ValueError(
                    f"User '{admin_user}' already exists but has unexpected role ('{existing_admin.role}') "
                    f"or lacks staff/superuser privileges. Refusing to alter existing account."
                )
            self.stdout.write(self.style.WARNING(f"User '{admin_user}' already exists as valid ADMIN. Skipping."))
        else:
            User.objects.create_superuser(
                username=admin_user,
                email=admin_email,
                password=admin_pass,
                role='ADMIN'
            )
            self.stdout.write(self.style.SUCCESS(f"Successfully bootstrapped ADMIN user '{admin_user}'."))

        # 3. Optional Employee bootstrap with is_staff=False
        if emp_user and emp_pass:
            validate_password(emp_pass)
            existing_emp = User.objects.filter(username=emp_user).first()
            if existing_emp:
                if existing_emp.role != 'EMPLOYEE' or existing_emp.is_staff or existing_emp.is_superuser:
                    raise ValueError(
                        f"User '{emp_user}' already exists but has mismatched employee privileges."
                    )
                self.stdout.write(self.style.WARNING(f"Employee '{emp_user}' already exists. Skipping."))
            else:
                User.objects.create_user(
                    username=emp_user,
                    email=emp_email,
                    password=emp_pass,
                    role='EMPLOYEE',
                    is_staff=False,
                    is_superuser=False
                )
                self.stdout.write(self.style.SUCCESS(f"Successfully bootstrapped EMPLOYEE user '{emp_user}' (is_staff=False)."))
```

- [ ] **Step 4: Run test to verify it passes [GREEN Verification]**

Run: `python manage.py test accounts.tests.test_bootstrap`  
Expected: **PASS** (all 4 tests green).

- [ ] **Step 5: Refactor helper methods [REFACTOR]**

- [ ] **Step 6: Commit management command**

```bash
git add accounts/management/ accounts/tests/test_bootstrap.py
git commit -m "feat(security): implement secure bootstrap_system_users management command with employee admin isolation"
```

---

### Task 4: RBAC, Decorators & Django Admin Dual-Key Authorization (RED $\rightarrow$ GREEN $\rightarrow$ REFACTOR)

**Files:**
- Create: `accounts/decorators.py`
- Modify: `accounts/admin.py`
- Create: `accounts/tests/test_rbac.py`

**Interfaces:**
- Consumes: `django.contrib.auth.models.AnonymousUser`, `accounts.models.User`, `django.contrib.admin.site`, `request.user`
- Produces: `@login_required_with_lock_check`, `@role_required(*roles)`, and admin site hook requiring BOTH `is_staff=True` AND `role='ADMIN'`

- [ ] **Step 1: Write failing tests in `accounts/tests/test_rbac.py` [RED]**

```python
from django.test import TestCase, RequestFactory, Client
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.contrib.auth.models import AnonymousUser
from accounts.models import User
from accounts.decorators import role_required, login_required_with_lock_check

class RbacDecoratorTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.client = Client()
        self.customer = User.objects.create_user(username='cust', password='pwd', role='CUSTOMER', is_staff=False)
        self.customer_staff = User.objects.create_user(username='cust_staff', password='pwd', role='CUSTOMER', is_staff=True)
        self.employee = User.objects.create_user(username='emp', password='pwd', role='EMPLOYEE', is_staff=False)
        self.employee_staff = User.objects.create_user(username='emp_staff', password='pwd', role='EMPLOYEE', is_staff=True)
        self.admin = User.objects.create_superuser(username='adm', password='pwd', role='ADMIN', is_staff=True)
        self.admin_non_staff = User.objects.create_user(username='adm_non_staff', password='pwd', role='ADMIN', is_staff=False)
        self.locked_user = User.objects.create_user(username='locked', password='pwd', role='CUSTOMER', account_locked=True)

    def test_anonymous_redirected_to_login(self):
        @login_required_with_lock_check
        def dummy_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy/')
        req.user = AnonymousUser()  # Strict AnonymousUser() instance
        resp = dummy_view(req)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp.url)

    def test_locked_user_redirected_to_login(self):
        @login_required_with_lock_check
        def dummy_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy/')
        req.user = self.locked_user
        resp = dummy_view(req)
        self.assertEqual(resp.status_code, 302)

    def test_customer_accessing_admin_role_raises_403(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy-admin/')
        req.user = self.customer
        with self.assertRaises(PermissionDenied):
            admin_view(req)

    def test_admin_accessing_admin_role_succeeds(self):
        @role_required('ADMIN')
        def admin_view(request):
            return HttpResponse("OK")

        req = self.factory.get('/dummy-admin/')
        req.user = self.admin
        resp = admin_view(req)
        self.assertEqual(resp.status_code, 200)

    # Django Admin Dual-Key Authorization Tests: Both is_staff=True AND role='ADMIN' required!

    def test_anonymous_blocked_from_django_admin(self):
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])
        if response.status_code == 302:
            self.assertIn('/admin/login/', response.url)

    def test_customer_blocked_from_django_admin(self):
        self.client.force_login(self.customer)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_customer_with_is_staff_still_blocked_from_django_admin(self):
        """Even if is_staff=True, CUSTOMER role must be rejected from Django Admin."""
        self.client.force_login(self.customer_staff)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_employee_blocked_from_django_admin(self):
        """Standard employee (is_staff=False) blocked from Django Admin."""
        self.client.force_login(self.employee)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_employee_with_is_staff_still_blocked_from_django_admin(self):
        """Even if is_staff=True, EMPLOYEE role must be rejected from Django Admin."""
        self.client.force_login(self.employee_staff)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_admin_without_is_staff_blocked_from_django_admin(self):
        """User with role='ADMIN' but is_staff=False must be rejected."""
        self.client.force_login(self.admin_non_staff)
        response = self.client.get('/admin/')
        self.assertIn(response.status_code, [302, 403])

    def test_admin_with_both_staff_and_admin_role_can_access_django_admin(self):
        """Only users with BOTH is_staff=True AND role='ADMIN' can access Django Admin."""
        self.client.force_login(self.admin)
        response = self.client.get('/admin/')
        self.assertEqual(response.status_code, 200)
```

- [ ] **Step 2: Run test to verify it fails [RED Verification]**

Run: `python manage.py test accounts.tests.test_rbac`  
Expected: **FAIL** (`ModuleNotFoundError: No module named 'accounts.decorators'`, or `cust_staff` / `emp_staff` permitted into admin).

- [ ] **Step 3: Implement `accounts/decorators.py` [GREEN]**

```python
from functools import wraps
from django.shortcuts import redirect
from django.core.exceptions import PermissionDenied
from django.contrib import messages

def login_required_with_lock_check(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"/login/?next={request.path}")
        if getattr(request.user, 'account_locked', False):
            messages.error(request, "Account is blocked ❌")
            return redirect("login")
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def role_required(*allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f"/login/?next={request.path}")
            if getattr(request.user, 'account_locked', False):
                messages.error(request, "Account is blocked ❌")
                return redirect("login")
            if getattr(request.user, 'role', None) not in allowed_roles:
                raise PermissionDenied("You do not have permission to access this resource.")
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
```

- [ ] **Step 4: Implement Django Admin Dual-Key Authorization in `accounts/admin.py` [GREEN]**

Hook `admin.site.has_permission` to require BOTH `is_staff=True` AND `role == 'ADMIN'`:
```python
from django.contrib import admin
from .models import User

# Enforce dual-key authorization: is_staff=True AND role='ADMIN'
def dual_key_admin_has_permission(request):
    return (
        request.user.is_active
        and request.user.is_staff
        and getattr(request.user, 'role', None) == 'ADMIN'
    )

admin.site.has_permission = dual_key_admin_has_permission
```

- [ ] **Step 5: Run test to verify it passes [GREEN Verification]**

Run: `python manage.py test accounts.tests.test_rbac`  
Expected: **PASS** (all 11 tests green).

- [ ] **Step 6: Refactor docstrings and typing [REFACTOR]**

- [ ] **Step 7: Commit decorators & admin authorization**

```bash
git add accounts/decorators.py accounts/admin.py accounts/tests/test_rbac.py
git commit -m "feat(security): enforce RBAC decorators and Django admin dual-key authorization (is_staff and ADMIN role)"
```

---

### Task 5: Enforce Authentication & RBAC on Accounts Views (RED $\rightarrow$ GREEN $\rightarrow$ REFACTOR)

**Files:**
- Modify: `accounts/views.py`
- Test: `accounts/tests/test_rbac.py`

**Interfaces:**
- Consumes: `accounts.decorators.role_required`
- Produces: Hardened dashboard, block, and unblock views

- [ ] **Step 1: Write integration tests in `accounts/tests/test_rbac.py` for actual accounts views [RED]**

```python
    def test_customer_cannot_access_admin_dashboard(self):
        self.client.force_login(self.customer)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(response.status_code, 403)

    def test_customer_cannot_access_employee_dashboard(self):
        self.client.force_login(self.customer)
        response = self.client.get('/employee/dashboard/')
        self.assertEqual(response.status_code, 403)

    def test_employee_can_access_employee_dashboard(self):
        self.client.force_login(self.employee)
        response = self.client.get('/employee/dashboard/')
        self.assertEqual(response.status_code, 200)

    def test_admin_can_access_admin_dashboard(self):
        self.client.force_login(self.admin)
        response = self.client.get('/admin-dashboard/')
        self.assertEqual(response.status_code, 200)
```

- [ ] **Step 2: Run test to verify it fails [RED Verification]**

Run: `python manage.py test accounts.tests.test_rbac.RbacDecoratorTests.test_customer_cannot_access_admin_dashboard`  
Expected: **FAIL** (returns 200 instead of 403).

- [ ] **Step 3: Decorate views in `accounts/views.py` [GREEN]**

Import decorators:
```python
from .decorators import role_required, login_required_with_lock_check
```
Apply decorators:
- `customer_dashboard` $\rightarrow$ `@role_required('CUSTOMER')`
- `employee_dashboard` $\rightarrow$ `@role_required('EMPLOYEE', 'ADMIN')`
- `admin_dashboard` $\rightarrow$ `@role_required('ADMIN')`
- `block_user` $\rightarrow$ `@role_required('ADMIN')`
- `unblock_user` $\rightarrow$ `@role_required('ADMIN')`

- [ ] **Step 4: Run tests to verify they pass [GREEN Verification]**

Run: `python manage.py test accounts.tests.test_rbac`  
Expected: **PASS** (all tests green).

- [ ] **Step 5: Refactor view signatures and remove dead code [REFACTOR]**

- [ ] **Step 6: Commit changes**

```bash
git add accounts/views.py accounts/tests/test_rbac.py
git commit -m "fix(security): enforce RBAC on accounts views"
```

---

### Task 6: Enforce RBAC & Object-Level Ownership (IDOR Protection) Across Banking Views (RED $\rightarrow$ GREEN $\rightarrow$ REFACTOR)

**Files:**
- Modify: `banking/views.py`
- Create: `banking/tests/__init__.py`
- Create: `banking/tests/test_rbac.py`

**Interfaces:**
- Consumes: `accounts.decorators.role_required`, object ownership verification logic
- Produces: Protected banking endpoints with strict RBAC and object-level IDOR protection (customer ownership isolation)

- [ ] **Step 1: Write RBAC and object-level ownership tests in `banking/tests/test_rbac.py` [RED]**

```python
from django.test import TestCase, Client
from accounts.models import User
from banking.models import BankAccount, Loan, Transaction as BankTx

class BankingRbacTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.customer = User.objects.create_user(username='c1', password='pwd', role='CUSTOMER')
        self.other_customer = User.objects.create_user(username='c2', password='pwd', role='CUSTOMER')
        self.employee = User.objects.create_user(username='e1', password='pwd', role='EMPLOYEE', is_staff=False)

        self.account = BankAccount.objects.create(user=self.customer, account_type='SAVINGS', balance=1000)
        self.other_account = BankAccount.objects.create(user=self.other_customer, account_type='SAVINGS', balance=2000)

        self.loan = Loan.objects.create(account=self.account, amount=500, duration_months=6, status='PENDING')
        self.other_loan = Loan.objects.create(account=self.other_account, amount=800, duration_months=12, status='PENDING')

    def test_anonymous_cannot_access_withdraw(self):
        response = self.client.get('/bank/withdraw/')
        self.assertEqual(response.status_code, 302)

    def test_employee_cannot_access_withdraw(self):
        self.client.force_login(self.employee)
        response = self.client.get('/bank/withdraw/')
        self.assertEqual(response.status_code, 403)

    def test_customer_cannot_access_create_account(self):
        self.client.force_login(self.customer)
        response = self.client.get('/bank/create-account/')
        self.assertEqual(response.status_code, 403)

    def test_employee_can_access_create_account(self):
        self.client.force_login(self.employee)
        response = self.client.get('/bank/create-account/')
        self.assertEqual(response.status_code, 200)

    # Object-Level Ownership (IDOR Protection) Tests

    def test_customer_cannot_withdraw_from_another_customers_account(self):
        """Ensure customer cannot withdraw from an account owned by another customer."""
        self.client.force_login(self.customer)
        response = self.client.post('/bank/withdraw/', data={
            'account_id': self.other_account.id,
            'amount': 100
        })
        self.assertIn(response.status_code, [403, 404])
        self.other_account.refresh_from_db()
        self.assertEqual(self.other_account.balance, 2000, "Other customer's balance must be untouched.")

    def test_customer_cannot_view_or_download_another_customers_passbook(self):
        """Ensure customer cannot view or generate PDF for an account they do not own."""
        self.client.force_login(self.customer)
        response = self.client.get(f'/bank/passbook/{self.other_account.id}/')
        self.assertIn(response.status_code, [403, 404])

    def test_customer_cannot_view_another_customers_transaction_history(self):
        """Ensure customer sees only transactions belonging to their own accounts."""
        BankTx.objects.create(account=self.other_account, transaction_type='DEPOSIT', amount=500)
        self.client.force_login(self.customer)
        response = self.client.get('/bank/history/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(self.other_account.account_number), response.content.decode())
```

- [ ] **Step 2: Run test to verify it fails [RED Verification]**

Run: `python manage.py test banking.tests.test_rbac`  
Expected: **FAIL** (returns 200 on unauthorized endpoints, ownership not enforced).

- [ ] **Step 3: Decorate banking views and enforce object-level ownership [GREEN]**

In `banking/views.py`:
- Apply `@role_required('EMPLOYEE', 'ADMIN')` on staff actions (`create_account_view`, `deposit_view`, `analytics_view`).
- Apply `@role_required('CUSTOMER')` on customer actions (`withdraw_view`, `transfer_view`, `history_view`, `passbook_pdf_view`, `fd_view`, `loan_view`).
- In `withdraw_view`, `passbook_pdf_view`, `history_view`: ensure `account = get_object_or_404(BankAccount, id=account_id, user=request.user)`.

- [ ] **Step 4: Run test to verify it passes [GREEN Verification]**

Run: `python manage.py test banking.tests.test_rbac`  
Expected: **PASS** (all 7 RBAC and ownership tests green).

- [ ] **Step 5: Refactor view decorators and query filters [REFACTOR]**

- [ ] **Step 6: Commit changes**

```bash
git add banking/views.py banking/tests/
git commit -m "fix(security): apply RBAC and object-level ownership checks across all banking views"
```

---

### Task 7: Atomic Loan Approval Engine with Row Locking, Rollback Injection & Barrier PostgreSQL Concurrency (RED $\rightarrow$ GREEN $\rightarrow$ REFACTOR)

**Files:**
- Modify: `banking/urls.py`
- Modify: `banking/views.py` (`approve_loan`, `reject_loan`)
- Modify: `accounts/views.py` (`block_user`, `unblock_user`)
- Modify: `templates/admin_dashboard.html`
- Create: `banking/tests/test_loan_actions.py`

**Interfaces:**
- Consumes: Django `@require_POST`, `@transaction.atomic`, `select_for_update()`, `Client(enforce_csrf_checks=True)`, `TransactionTestCase`, `threading.Barrier`
- Produces: Atomic, concurrency-safe, CSRF-protected administrative loan approval endpoint with verified rollback on failure and barrier-synchronized PostgreSQL concurrency verification

- [ ] **Step 1: Write failing atomic loan approval, endpoint rollback injection, and barrier concurrency tests in `banking/tests/test_loan_actions.py` [RED]**

```python
import threading
from unittest.mock import patch
from django.test import TestCase, Client, TransactionTestCase
from django.db import connection
from accounts.models import User
from banking.models import BankAccount, Loan, Transaction as BankTx

class LoanActionSecurityTests(TestCase):
    def setUp(self):
        self.csrf_client = Client(enforce_csrf_checks=True)
        self.standard_client = Client()
        self.customer = User.objects.create_user(username='c1', password='pwd', role='CUSTOMER')
        self.admin = User.objects.create_superuser(username='adm', password='pwd', role='ADMIN')
        self.account = BankAccount.objects.create(user=self.customer, account_type='SAVINGS', balance=500)
        self.loan = Loan.objects.create(account=self.account, amount=1000, duration_months=12, status='PENDING')

    def test_get_to_approve_loan_fails_method_not_allowed(self):
        self.standard_client.force_login(self.admin)
        response = self.standard_client.get(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 405)  # Method Not Allowed

    def test_post_without_csrf_token_fails(self):
        """Ensure CSRF is strictly enforced against forged requests."""
        self.csrf_client.force_login(self.admin)
        response = self.csrf_client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 403)

    def test_post_with_genuine_csrf_token_succeeds(self):
        """Ensure administrative POST with valid CSRF token and session succeeds."""
        self.csrf_client.force_login(self.admin)
        get_res = self.csrf_client.get('/admin-dashboard/')
        csrf_token = get_res.cookies['csrftoken'].value

        response = self.csrf_client.post(
            f'/bank/loan/{self.loan.id}/approve/',
            data={'csrfmiddlewaretoken': csrf_token},
            HTTP_REFERER='/admin-dashboard/'
        )
        self.assertEqual(response.status_code, 302)
        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 1500)
        self.assertTrue(BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT", amount=1000).exists())

    def test_duplicate_approval_rejected_without_double_crediting(self):
        """Ensure attempting to approve an already approved loan is rejected and balance unchanged."""
        self.loan.status = 'APPROVED'
        self.loan.save()
        self.standard_client.force_login(self.admin)
        response = self.standard_client.post(f'/bank/loan/{self.loan.id}/approve/')
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 500)  # Unchanged balance

    def test_approve_loan_endpoint_rolls_back_completely_on_injected_exception(self):
        """
        Test the actual approve_loan HTTP endpoint with exception injection.
        Inject an exception inside approve_loan after balance update and transaction creation.
        Assert that loan status, account balance, and transaction records all roll back completely.
        """
        self.standard_client.force_login(self.admin)

        original_save = Loan.save
        def faulty_save(loan_instance, *args, **kwargs):
            if kwargs.get('update_fields') == ['status'] and loan_instance.status == 'APPROVED':
                raise RuntimeError("Simulated database failure after balance update and transaction creation!")
            return original_save(loan_instance, *args, **kwargs)

        with patch.object(Loan, 'save', side_effect=faulty_save, autospec=True):
            with self.assertRaises(RuntimeError):
                self.standard_client.post(f'/bank/loan/{self.loan.id}/approve/')

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'PENDING', "Loan status must remain PENDING after rollback.")

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 500, "Account balance must remain 500 after rollback.")

        credit_txs = BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT")
        self.assertFalse(credit_txs.exists(), "No LOAN_CREDIT transaction must exist after rollback.")


class ConcurrentLoanApprovalPostgresTests(TransactionTestCase):
    """
    PostgreSQL Row-Locking Concurrency Test Suite.
    CRITICAL: SQLite does NOT enforce row-level locking (select_for_update is a no-op or table lock in SQLite).
    This test runs against PostgreSQL (Neon or local) to verify row serialization under simultaneous threads.
    Threads synchronize via threading.Barrier and manage independent database connections.
    """
    def setUp(self):
        if connection.vendor != 'postgresql':
            self.skipTest(
                "Row-level locking concurrency verification requires PostgreSQL. "
                "SQLite does not support row-level locking (select_for_update)."
            )
        self.customer = User.objects.create_user(username='pg_cust', password='pwd', role='CUSTOMER')
        self.admin = User.objects.create_superuser(username='pg_admin', password='pwd', role='ADMIN')
        self.account = BankAccount.objects.create(user=self.customer, account_type='SAVINGS', balance=500)
        self.loan = Loan.objects.create(account=self.account, amount=1000, duration_months=12, status='PENDING')

    def test_concurrent_simultaneous_approvals_result_in_exactly_one_credit(self):
        """
        Synchronize simultaneous approval requests with a barrier and independent DB connections per thread.
        Assert exactly one approval, exactly one credit to balance, and no double-crediting.
        """
        thread_errors = []
        status_codes = []
        barrier = threading.Barrier(2)

        def execute_approval():
            from django.db import connection as thread_conn
            thread_conn.close()  # Ensure clean connection state for thread
            client = Client()
            client.force_login(self.admin)
            try:
                barrier.wait(timeout=5)  # Synchronize threads at barrier
                resp = client.post(f'/bank/loan/{self.loan.id}/approve/')
                status_codes.append(resp.status_code)
            except Exception as e:
                thread_errors.append(e)
            finally:
                thread_conn.close()

        t1 = threading.Thread(target=execute_approval)
        t2 = threading.Thread(target=execute_approval)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(len(thread_errors), 0, f"Unexpected thread errors: {thread_errors}")
        self.assertEqual(len(status_codes), 2)

        self.loan.refresh_from_db()
        self.assertEqual(self.loan.status, 'APPROVED')

        self.account.refresh_from_db()
        self.assertEqual(self.account.balance, 1500, "Account balance must be credited exactly once.")

        credit_tx_count = BankTx.objects.filter(account=self.account, transaction_type="LOAN_CREDIT").count()
        self.assertEqual(credit_tx_count, 1, "Exactly one LOAN_CREDIT record must be created.")
```

- [ ] **Step 2: Run test to verify it fails [RED Verification]**

Run: `python manage.py test banking.tests.test_loan_actions`  
Expected: **FAIL** (HTTP 404 URL does not exist).

- [ ] **Step 3: Route endpoints in `banking/urls.py` [GREEN]**

```python
path('loan/<int:loan_id>/approve/', views.approve_loan, name='approve_loan'),
path('loan/<int:loan_id>/reject/', views.reject_loan, name='reject_loan'),
```

- [ ] **Step 4: Implement atomic `approve_loan` and `reject_loan` with row locking [GREEN]**

In `banking/views.py`:
```python
from django.views.decorators.http import require_POST
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from accounts.decorators import role_required
from .models import Loan, BankAccount, Transaction

@require_POST
@role_required('ADMIN')
@transaction.atomic
def approve_loan(request, loan_id):
    # Lock loan row to prevent concurrent duplicate processing
    loan = get_object_or_404(Loan.objects.select_for_update(), id=loan_id)

    if loan.status != "PENDING":
        messages.error(request, f"Loan #{loan_id} has already been processed ({loan.status}). ❌")
        return redirect('/admin-dashboard/')

    # Lock account row to guarantee balance integrity
    account = BankAccount.objects.select_for_update().get(id=loan.account_id)
    account.balance += loan.amount
    account.save(update_fields=['balance'])

    Transaction.objects.create(
        account=account,
        transaction_type="LOAN_CREDIT",
        amount=loan.amount
    )

    loan.status = "APPROVED"
    loan.save(update_fields=['status'])

    messages.success(request, f"Loan #{loan_id} approved successfully ✅")
    return redirect('/admin-dashboard/')

@require_POST
@role_required('ADMIN')
@transaction.atomic
def reject_loan(request, loan_id):
    loan = get_object_or_404(Loan.objects.select_for_update(), id=loan_id)
    if loan.status != "PENDING":
        messages.error(request, f"Loan #{loan_id} has already been processed ({loan.status}). ❌")
        return redirect('/admin-dashboard/')

    loan.status = "REJECTED"
    loan.save(update_fields=['status'])
    messages.success(request, f"Loan #{loan_id} rejected. 🚫")
    return redirect('/admin-dashboard/')
```

In `accounts/views.py`:
```python
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from .decorators import role_required
from .models import User

@require_POST
@role_required('ADMIN')
def block_user(request, user_id):
    user = get_object_or_404(User, id=user_id)
    user.account_locked = True
    user.save(update_fields=['account_locked'])
    messages.success(request, "User blocked 🚫")
    return redirect('/admin-dashboard/')

@require_POST
@role_required('ADMIN')
def unblock_user(request, user_id):
    user = get_object_or_404(User, id=user_id)
    user.account_locked = False
    user.save(update_fields=['account_locked'])
    messages.success(request, "User unblocked ✅")
    return redirect('/admin-dashboard/')
```

- [ ] **Step 5: Update `templates/admin_dashboard.html` with POST forms and CSRF [GREEN]**

Replace lines 103–106:
```html
<td>
  <form method="POST" action="{% url 'approve_loan' l.id %}" style="display:inline;">
    {% csrf_token %}
    <button type="submit" class="btn btn-success btn-sm">Approve</button>
  </form>
  <form method="POST" action="{% url 'reject_loan' l.id %}" style="display:inline;">
    {% csrf_token %}
    <button type="submit" class="btn btn-danger btn-sm">Reject</button>
  </form>
</td>
```

- [ ] **Step 6: Run test to verify it passes [GREEN Verification]**

Run: `python manage.py test banking.tests.test_loan_actions`  
Expected: **PASS** (all tests green, atomic locking, endpoint rollback, and CSRF verified).

- [ ] **Step 7: Refactor clean exception handling and logging [REFACTOR]**

- [ ] **Step 8: Commit changes**

```bash
git add banking/urls.py banking/views.py accounts/views.py templates/admin_dashboard.html banking/tests/test_loan_actions.py
git commit -m "fix(security): implement atomic select_for_update loan approval, endpoint rollback test, and secure POST actions"
```

---

### Task 8: Client-Side Duplicate Submission Safeguards (UX Protection)

> [!NOTE]
> **ENGINEERING CLARIFICATION ON IDEMPOTENCY**:  
> Client-side button disabling is strictly a **UX safeguard** to prevent accidental double-clicks. It does **not** provide cryptographic or server-side idempotency protection against automated replays, script attacks, or network retries. True server-side database-backed idempotency (`IdempotencyRecord` table with unique constraints on `(user, idempotency_key)`) is scoped for implementation and PostgreSQL testing in Phase 1 and Phase 2.

**Files:**
- Modify: `templates/withdraw.html`
- Modify: `templates/transfer.html`
- Modify: `templates/deposit.html`
- Modify: `templates/fd.html`
- Modify: `templates/loan.html`

**Interfaces:**
- Consumes: DOM `form.addEventListener('submit')`
- Produces: Instant button disable + visual indicator on form submit

- [ ] **Step 1: Create reusable JavaScript submit guard snippet**

```html
<script>
document.querySelectorAll('form').forEach(function(form) {
    form.addEventListener('submit', function(e) {
        var btn = form.querySelector('button[type="submit"]');
        if (btn && !btn.disabled) {
            btn.disabled = true;
            btn.dataset.originalText = btn.innerText;
            btn.innerText = 'Processing...';
        }
    });
});
</script>
```

- [ ] **Step 2: Add snippet to `templates/transfer.html`, `templates/withdraw.html`, `templates/deposit.html`, `templates/fd.html`, and `templates/loan.html`**

- [ ] **Step 3: Verify manually with test browser or headless DOM check [GREEN Verification]**

Check that pages render cleanly without syntax errors.

- [ ] **Step 4: Commit changes**

```bash
git add templates/transfer.html templates/withdraw.html templates/deposit.html templates/fd.html templates/loan.html
git commit -m "fix(ux): add double-submission prevention guard on financial forms"
```

---

### Task 9: Phase 0 Full Verification, Runtime & Git History Secret Scan & Rollback Verification

**Files:**
- Review: All modified files in `accounts/`, `banking/`, `core/`, `templates/`

- [ ] **Step 1: Execute complete test suite**

Run: `python manage.py test accounts banking -v 2`  
Expected: All tests pass with zero failures and zero errors.

- [ ] **Step 2: Comprehensive Safe Secret Scan & Tracked Git History Review**

Verification Protocol:
- [ ] Safe Runtime Source Scan:
  - Recursively scans all `.py` files across `core/`, `accounts/`, and `banking/` using `scan_runtime_source`.
  - Confirms zero hardcoded plaintext passwords, keys, or disallowed functions remain in any runtime Python file.
  - Output must redact any detected secrets as `[REDACTED_CREDENTIAL]`. Never display real secret values.
- [ ] Tracked Git History Security Audit:
  - Executes `scan_git_history(repo_dir=Path(settings.BASE_DIR), max_commits=None)` over the complete tracked repository history (with `--root` flag) to inspect all historical commits that introduced hardcoded credentials.
  - Fail-Fast check: Asserts that if Git is unavailable or returns an error, `GitScanUnavailableError` is raised and caught, preventing false-positive pass claims.
  - Generate a redacted summary of commit SHAs requiring credential rotation.
  - Document mandatory credential rotation requirements for all legacy passwords prior to production deployment.
- [ ] Document Scanner Limitations and False-Positive Handling:
  - Document that the heuristic regex scanner covers standard credential patterns, `set_password` calls, and disallowed bootstrap functions.
  - False positives in fixtures or patterns are managed cleanly via the `# pragma: allowlist secret` directive.
- [ ] Object-Level Ownership Verification:
  - Confirm IDOR protection tests verify customers cannot view or modify other customers' accounts, loans, or transactions.
- [ ] Django Admin Dual-Key Authorization:
  - Verify that both `EMPLOYEE` and `CUSTOMER` users are denied access to `/admin/` (tested with `is_staff=False` and `is_staff=True`).
  - Verify that only users with **both** `is_staff=True` AND `role='ADMIN'` can access `/admin/`.
- [ ] Loan Atomicity & Endpoint Rollback:
  - Verify `approve_loan` endpoint rolls back loan status, account balance, and transaction records upon exception.
  - Barrier concurrency tests confirm PostgreSQL row lock serialization.
- [ ] All 15 existing Django templates remain loadable without template syntax errors.
- [ ] All tests use `AnonymousUser()`, never `User()`.
- [ ] CSRF tests execute with `Client(enforce_csrf_checks=True)` for both missing token failure (403) and valid token success (302).
- [ ] Working branch is strictly local (`feature/phase-0-security-hardening`), not pushed to GitHub.

- [ ] **Step 3: Verification of Rollback Procedure**

Strict Invariant Protocol:
1. Run `git status --short`.
2. Verify the working tree is completely clean (zero modified or uncommitted files). If any changes exist, report filenames only; **never** automatically stash, revert, or discard changes.
3. Prompt user and require explicit user approval before executing any checkout or branch switch.
4. Only upon explicit user confirmation, execute:
```bash
git checkout main  # switch back to main branch
```
Because no database migrations or schema alterations occurred during Phase 0, rollback is strictly instantaneous branch checkout upon approval.

---

> [!NOTE]
> **Plan complete and ready for execution approval.**  
> Under Superpowers methodology, no application source code has been modified yet, no tests executed, and no migrations run. Awaiting your approval to begin executing Phase 0 task-by-task.
