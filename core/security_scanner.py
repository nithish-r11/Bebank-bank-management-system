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
    rule_id: str
    redacted_diff_line: str

    def __str__(self):
        return f"commit {self.commit_sha} ({self.file_path}) [{self.rule_id}]: {self.redacted_diff_line}"

    def __repr__(self):
        return f"GitAuditFinding(sha={self.commit_sha!r}, file={self.file_path!r}, rule={self.rule_id!r}, redacted={self.redacted_diff_line!r})"


SECRET_PATTERNS = [
    # Plaintext assignments: password = '...', secret_key = '...', ADMIN_SECRET = '...', api_key = '...', etc. # pragma: allowlist secret
    (
        re.compile(r'''(?i)\b[a-zA-Z0-9_]*(password|passwd|secret_key|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\b\s*[:=]\s*(['"])(?P<secret>[^\r\n'"]+)\2'''),
        'ASSIGNED_CREDENTIAL'
    ),
    # Method calls: set_password('...') # pragma: allowlist secret
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
    Replaces all matched secret values on the line with [REDACTED_CREDENTIAL]
    using exact named capture spans. Preserves line syntax and non-credential strings.
    """
    redacted = raw_line.rstrip('\r\n')
    for pattern, rule_id in SECRET_PATTERNS:
        if 'DISALLOWED' in rule_id:
            continue
        matches = list(pattern.finditer(redacted))
        # Replace from right to left so earlier indices remain valid
        for match in reversed(matches):
            if 'secret' in match.groupdict():
                secret_val = match.group('secret')
                if secret_val != '[REDACTED_CREDENTIAL]':
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
        cmd = ['git', 'log', '-p', '--root', '--no-color']
        if max_commits is not None:
            cmd.extend(['-n', str(max_commits)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                cwd=str(repo_dir),
                timeout=15,
                check=True
            )
        except subprocess.TimeoutExpired:
            raise GitScanUnavailableError("Git history audit timed out. Inspection could not be completed.")
        except FileNotFoundError:
            raise GitScanUnavailableError("Git executable is not installed or not found in system PATH.")
        except subprocess.CalledProcessError as err:
            raise GitScanUnavailableError(f"Git history audit failed with exit status {err.returncode}. Repository may be missing, invalid, or corrupted.")
        except Exception:
            raise GitScanUnavailableError("Git history audit failed due to an unexpected execution error.")

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
                            rule_id=rule_id,
                            redacted_diff_line=masked
                        ))
                        break

        return audit_findings

# Standalone functional aliases
scan_file_content = SecurityScanner.scan_content
scan_runtime_source = SecurityScanner.scan_runtime
scan_git_history = SecurityScanner.scan_git_history
