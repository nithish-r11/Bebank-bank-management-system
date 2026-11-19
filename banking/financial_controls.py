import csv
from decimal import Decimal
from collections import defaultdict
from django.utils import timezone
from django.db.models import Q, Count
from banking.models import Transaction, BankAccount, TellerOperationLog

# =============================================================================
# FINANCIAL MONITORING & RISK DETECTION THRESHOLDS (CONFIGURABLE-IN-CODE)
# =============================================================================
LARGE_TRANSACTION_THRESHOLD = Decimal('100000.00')  # ₹100,000.00
RAPID_VELOCITY_SECONDS = 300                        # 5 minutes window
RAPID_VELOCITY_THRESHOLD = 3                        # >= 3 txns in 5 minutes
CONCENTRATED_VOLUME_HOURS = 1                       # 1 hour window
CONCENTRATED_VOLUME_THRESHOLD = 5                   # >= 5 txns in 1 hour
TELLER_DAILY_SURGE_THRESHOLD = 20                   # >= 20 teller txns/day


def sanitize_csv_cell(value):
    """
    Guards against CSV Formula Injection (CWE-1236).
    If a cell starts with formula characters (=, +, -, @, \\t, \\r),
    prepends a single quote to neutralize formula evaluation in spreadsheet viewers.
    """
    if value is None:
        return ""
    val_str = str(value)
    if val_str and val_str[0] in ('=', '+', '-', '@', '\t', '\r'):
        return f"'{val_str}"
    return val_str


class ReconciliationIssue:
    """Read-only diagnostic representation of a detected ledger integrity issue."""
    def __init__(self, category, severity, reference_id, account_number, description, detected_at=None, transaction_id=None):
        self.category = category            # e.g., 'ASYMMETRIC_TRANSFER', 'SNAPSHOT_MISMATCH', etc.
        self.severity = severity            # 'CRITICAL', 'HIGH', 'MEDIUM', 'INFO'
        self.reference_id = reference_id or "N/A"
        self.account_number = account_number or "N/A"
        self.description = description
        self.detected_at = detected_at or timezone.now()
        self.transaction_id = transaction_id

    @property
    def masked_account_number(self):
        if not self.account_number or self.account_number == 'N/A':
            return 'N/A'
        acc = str(self.account_number)
        return f"••••{acc[-4:]}" if len(acc) >= 4 else acc


def run_ledger_reconciliation(date_from=None, date_to=None, max_records=2000, category_filter=None):
    """
    Performs read-only diagnostic reconciliation of the BEBANK financial ledger.
    Strictly read-only: does not alter account balances or transactions.
    Clearly distinguishes actual integrity violations from legacy records with NULL balance snapshots.
    """
    issues = []
    now = timezone.now()

    # Query scope for transactions
    tx_qs = Transaction.objects.select_related('account', 'account__user', 'related_account').all()
    if date_from:
        tx_qs = tx_qs.filter(created_at__date__gte=date_from)
    if date_to:
        tx_qs = tx_qs.filter(created_at__date__lte=date_to)

    tx_list = list(tx_qs.order_by('-created_at')[:max_records])

    total_scanned = len(tx_list)
    total_transfers = 0
    paired_transfers_count = 0
    asymmetric_transfers_count = 0
    snapshot_verified_count = 0
    snapshot_mismatch_count = 0
    legacy_null_count = 0

    # -------------------------------------------------------------------------
    # 1. TRANSFER LEG SYMMETRY & PAIRED CONSISTENCY
    # -------------------------------------------------------------------------
    transfer_groups = defaultdict(list)
    for tx in tx_list:
        if tx.transaction_type in ['TRANSFER_DEBIT', 'TRANSFER_CREDIT'] and tx.reference_id:
            transfer_groups[tx.reference_id].append(tx)

    for ref_id, legs in transfer_groups.items():
        total_transfers += 1
        debit_legs = [l for l in legs if l.transaction_type == 'TRANSFER_DEBIT']
        credit_legs = [l for l in legs if l.transaction_type == 'TRANSFER_CREDIT']

        # Check: Missing Leg (Asymmetric)
        if len(legs) == 1:
            asymmetric_transfers_count += 1
            missing_type = "Credit" if debit_legs else "Debit"
            present_leg = legs[0]
            issues.append(ReconciliationIssue(
                category='ASYMMETRIC_TRANSFER',
                severity='CRITICAL',
                reference_id=ref_id,
                account_number=present_leg.account.account_number if present_leg.account else 'N/A',
                description=f"Transfer reference {ref_id} is missing its corresponding {missing_type} leg. Only {present_leg.transaction_type} is posted.",
                detected_at=present_leg.created_at,
                transaction_id=present_leg.id
            ))
        elif len(debit_legs) > 1 or len(credit_legs) > 1:
            asymmetric_transfers_count += 1
            issues.append(ReconciliationIssue(
                category='DUPLICATE_TRANSFER_LEG',
                severity='CRITICAL',
                reference_id=ref_id,
                account_number=legs[0].account.account_number if legs[0].account else 'N/A',
                description=f"Transfer reference {ref_id} has duplicate legs: {len(debit_legs)} debit legs and {len(credit_legs)} credit legs posted.",
                detected_at=legs[0].created_at,
                transaction_id=legs[0].id
            ))
        elif len(debit_legs) == 1 and len(credit_legs) == 1:
            deb = debit_legs[0]
            crd = credit_legs[0]
            # Verify amounts match
            if deb.amount != crd.amount:
                issues.append(ReconciliationIssue(
                    category='TRANSFER_AMOUNT_MISMATCH',
                    severity='CRITICAL',
                    reference_id=ref_id,
                    account_number=deb.account.account_number if deb.account else 'N/A',
                    description=f"Transfer reference {ref_id} has unequal leg amounts: Debit is ₹{deb.amount:,.2f} but Credit is ₹{crd.amount:,.2f}.",
                    detected_at=deb.created_at,
                    transaction_id=deb.id
                ))
            else:
                paired_transfers_count += 1

            # Verify related_account cross-link
            if deb.related_account_id != crd.account_id or crd.related_account_id != deb.account_id:
                issues.append(ReconciliationIssue(
                    category='TRANSFER_CROSS_LINK_MISMATCH',
                    severity='HIGH',
                    reference_id=ref_id,
                    account_number=deb.account.account_number if deb.account else 'N/A',
                    description=f"Transfer reference {ref_id} has asymmetric related_account pointers between source and destination accounts.",
                    detected_at=deb.created_at,
                    transaction_id=deb.id
                ))

    # -------------------------------------------------------------------------
    # 2. BALANCE SNAPSHOT INTEGRITY & LEGACY NULL HANDLING
    # -------------------------------------------------------------------------
    for tx in tx_list:
        if tx.balance_before is None or tx.balance_after is None:
            legacy_null_count += 1
            # Clearly treated as legacy record, NOT an error
            continue

        if tx.amount <= Decimal('0.00'):
            issues.append(ReconciliationIssue(
                category='INVALID_AMOUNT',
                severity='HIGH',
                reference_id=tx.reference_id,
                account_number=tx.account.account_number if tx.account else 'N/A',
                description=f"Transaction #{tx.id} has invalid non-positive amount ₹{tx.amount}.",
                detected_at=tx.created_at,
                transaction_id=tx.id
            ))
            continue

        if tx.is_credit():
            expected = tx.balance_before + tx.amount
            if tx.balance_after != expected:
                snapshot_mismatch_count += 1
                issues.append(ReconciliationIssue(
                    category='SNAPSHOT_CREDIT_MISMATCH',
                    severity='HIGH',
                    reference_id=tx.reference_id,
                    account_number=tx.account.account_number if tx.account else 'N/A',
                    description=f"Credit snapshot discrepancy on Tx #{tx.id} ({tx.transaction_type}): Expected ₹{expected:,.2f} (₹{tx.balance_before:,.2f} + ₹{tx.amount:,.2f}), recorded ₹{tx.balance_after:,.2f}.",
                    detected_at=tx.created_at,
                    transaction_id=tx.id
                ))
            else:
                snapshot_verified_count += 1
        else:
            expected = tx.balance_before - tx.amount
            if tx.balance_after != expected:
                snapshot_mismatch_count += 1
                issues.append(ReconciliationIssue(
                    category='SNAPSHOT_DEBIT_MISMATCH',
                    severity='HIGH',
                    reference_id=tx.reference_id,
                    account_number=tx.account.account_number if tx.account else 'N/A',
                    description=f"Debit snapshot discrepancy on Tx #{tx.id} ({tx.transaction_type}): Expected ₹{expected:,.2f} (₹{tx.balance_before:,.2f} - ₹{tx.amount:,.2f}), recorded ₹{tx.balance_after:,.2f}.",
                    detected_at=tx.created_at,
                    transaction_id=tx.id
                ))
            else:
                snapshot_verified_count += 1

    # -------------------------------------------------------------------------
    # 3. TELLER OPERATION LOG RECONCILIATION
    # -------------------------------------------------------------------------
    teller_qs = TellerOperationLog.objects.filter(status='SUCCESS', operation_type__in=['DEPOSIT', 'WITHDRAWAL'])
    if date_from:
        teller_qs = teller_qs.filter(created_at__date__gte=date_from)
    if date_to:
        teller_qs = teller_qs.filter(created_at__date__lte=date_to)

    teller_logs = list(teller_qs.order_by('-created_at')[:500])
    teller_reconciled = 0
    scanned_ref_set = set(tx.reference_id for tx in tx_list if tx.reference_id)

    for tlog in teller_logs:
        if tlog.reference_id:
            # Check if matching transaction exists
            has_tx = (tlog.reference_id in scanned_ref_set) or Transaction.objects.filter(reference_id=tlog.reference_id).exists()
            if has_tx:
                teller_reconciled += 1
            else:
                issues.append(ReconciliationIssue(
                    category='TELLER_LOG_UNPOSTED_LEDGER',
                    severity='MEDIUM',
                    reference_id=tlog.reference_id,
                    account_number=tlog.account_number_snapshot or (tlog.account.account_number if tlog.account else 'N/A'),
                    description=f"Teller Operation #{tlog.id} ({tlog.operation_type} ₹{tlog.amount:,.2f}) marked SUCCESS but has no matching Transaction record with reference {tlog.reference_id}.",
                    detected_at=tlog.created_at
                ))

    # Apply category filter if requested
    if category_filter:
        issues = [iss for iss in issues if iss.category == category_filter]

    critical_count = sum(1 for iss in issues if iss.severity == 'CRITICAL')
    high_count = sum(1 for iss in issues if iss.severity == 'HIGH')

    stats = {
        'total_scanned': total_scanned,
        'total_transfers': total_transfers,
        'paired_transfers_count': paired_transfers_count,
        'asymmetric_transfers_count': asymmetric_transfers_count,
        'snapshot_verified_count': snapshot_verified_count,
        'snapshot_mismatch_count': snapshot_mismatch_count,
        'legacy_null_count': legacy_null_count,
        'teller_reconciled_count': teller_reconciled,
        'total_issues_count': len(issues),
        'critical_count': critical_count,
        'high_count': high_count,
        'reconciliation_timestamp': now,
    }

    return issues, stats


class RiskFlag:
    """Read-only representation of a risk indicator or anomalous transaction pattern."""
    def __init__(self, rule_id, rule_name, severity, transaction_id, reference_id, account_number, customer_username, amount, timestamp, reason):
        self.rule_id = rule_id                  # e.g., 'RULE_LARGE_TRANSACTION'
        self.rule_name = rule_name
        self.severity = severity                # 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'
        self.transaction_id = transaction_id
        self.reference_id = reference_id or "N/A"
        self.account_number = account_number or "N/A"
        self.customer_username = customer_username or "N/A"
        self.amount = amount
        self.timestamp = timestamp
        self.reason = reason

    @property
    def masked_account_number(self):
        if not self.account_number or self.account_number == 'N/A':
            return 'N/A'
        acc = str(self.account_number)
        return f"••••{acc[-4:]}" if len(acc) >= 4 else acc


def run_risk_monitoring_scan(date_from=None, date_to=None, max_records=1000, rule_filter=None, severity_filter=None):
    """
    Evaluates configurable simulation heuristics across recent financial activity.
    Flags are investigative indicators, not proof of fraud.
    Strictly read-only; does not automatically block accounts, reject transactions, or alter balances.
    """
    flags = []
    now = timezone.now()

    tx_qs = Transaction.objects.select_related('account', 'account__user').all()
    if date_from:
        tx_qs = tx_qs.filter(created_at__date__gte=date_from)
    if date_to:
        tx_qs = tx_qs.filter(created_at__date__lte=date_to)

    tx_list = list(tx_qs.order_by('-created_at')[:max_records])

    # -------------------------------------------------------------------------
    # RULE 1: LARGE VALUE TRANSACTIONS
    # -------------------------------------------------------------------------
    for tx in tx_list:
        if tx.amount >= LARGE_TRANSACTION_THRESHOLD:
            flags.append(RiskFlag(
                rule_id='RULE_LARGE_TRANSACTION',
                rule_name='Large Transaction Threshold Exceeded',
                severity='HIGH',
                transaction_id=tx.id,
                reference_id=tx.reference_id,
                account_number=tx.account.account_number if tx.account else 'N/A',
                customer_username=tx.account.user.username if (tx.account and tx.account.user) else 'N/A',
                amount=tx.amount,
                timestamp=tx.created_at,
                reason=f"Transaction amount ₹{tx.amount:,.2f} meets or exceeds the large-value monitoring threshold of ₹{LARGE_TRANSACTION_THRESHOLD:,.2f}."
            ))

    # -------------------------------------------------------------------------
    # RULE 2: RAPID VELOCITY (Multiple transactions within 5 minutes)
    # -------------------------------------------------------------------------
    account_tx_map = defaultdict(list)
    for tx in tx_list:
        if tx.account_id:
            account_tx_map[tx.account_id].append(tx)

    for acc_id, acc_txs in account_tx_map.items():
        # Sorted ascending for chronological window checks
        sorted_txs = sorted(acc_txs, key=lambda t: t.created_at)
        flagged_acc_tx_ids = set()

        for i, tx in enumerate(sorted_txs):
            window_end = tx.created_at + timezone.timedelta(seconds=RAPID_VELOCITY_SECONDS)
            # Find txs within this 5 minute window
            window_txs = [t for t in sorted_txs[i:] if t.created_at <= window_end]
            if len(window_txs) >= RAPID_VELOCITY_THRESHOLD:
                for wtx in window_txs:
                    if wtx.id not in flagged_acc_tx_ids:
                        flagged_acc_tx_ids.add(wtx.id)
                        flags.append(RiskFlag(
                            rule_id='RULE_RAPID_VELOCITY',
                            rule_name='Rapid Transaction Velocity',
                            severity='MEDIUM',
                            transaction_id=wtx.id,
                            reference_id=wtx.reference_id,
                            account_number=wtx.account.account_number if wtx.account else 'N/A',
                            customer_username=wtx.account.user.username if (wtx.account and wtx.account.user) else 'N/A',
                            amount=wtx.amount,
                            timestamp=wtx.created_at,
                            reason=f"Account recorded {len(window_txs)} transactions within a 5-minute velocity window."
                        ))

    # -------------------------------------------------------------------------
    # RULE 3: CONCENTRATED VOLUME (>= 5 transactions in 1 hour)
    # -------------------------------------------------------------------------
    for acc_id, acc_txs in account_tx_map.items():
        sorted_txs = sorted(acc_txs, key=lambda t: t.created_at)
        flagged_volume_tx_ids = set()

        for i, tx in enumerate(sorted_txs):
            window_end = tx.created_at + timezone.timedelta(hours=CONCENTRATED_VOLUME_HOURS)
            window_txs = [t for t in sorted_txs[i:] if t.created_at <= window_end]
            if len(window_txs) >= CONCENTRATED_VOLUME_THRESHOLD:
                for wtx in window_txs:
                    if wtx.id not in flagged_volume_tx_ids:
                        flagged_volume_tx_ids.add(wtx.id)
                        flags.append(RiskFlag(
                            rule_id='RULE_CONCENTRATED_VOLUME',
                            rule_name='High Concentrated Hourly Volume',
                            severity='MEDIUM',
                            transaction_id=wtx.id,
                            reference_id=wtx.reference_id,
                            account_number=wtx.account.account_number if wtx.account else 'N/A',
                            customer_username=wtx.account.user.username if (wtx.account and wtx.account.user) else 'N/A',
                            amount=wtx.amount,
                            timestamp=wtx.created_at,
                            reason=f"Account recorded {len(window_txs)} transactions within a 1-hour window."
                        ))

    # -------------------------------------------------------------------------
    # RULE 4: TRANSACTIONS INVOLVING FROZEN ACCOUNTS
    # -------------------------------------------------------------------------
    for tx in tx_list:
        if tx.account and getattr(tx.account, 'status', '') == 'FROZEN':
            flags.append(RiskFlag(
                rule_id='RULE_FROZEN_ACCOUNT_TRANSACTION',
                rule_name='Transaction on Frozen Bank Account',
                severity='CRITICAL',
                transaction_id=tx.id,
                reference_id=tx.reference_id,
                account_number=tx.account.account_number,
                customer_username=tx.account.user.username if tx.account.user else 'N/A',
                amount=tx.amount,
                timestamp=tx.created_at,
                reason=f"Transaction #{tx.id} ({tx.transaction_type}) recorded on account {tx.account.account_number} which is currently in FROZEN state."
            ))

    # -------------------------------------------------------------------------
    # RULE 5: UNUSUAL TELLER DAILY SURGE (>= 20 operations by one staff member)
    # -------------------------------------------------------------------------
    teller_ops = TellerOperationLog.objects.filter(
        created_at__gte=now - timezone.timedelta(days=1)
    ).values('employee__username').annotate(total_ops=Count('id'))

    for op in teller_ops:
        if op['total_ops'] >= TELLER_DAILY_SURGE_THRESHOLD:
            emp_uname = op['employee__username'] or 'System'
            flags.append(RiskFlag(
                rule_id='RULE_HIGH_VOLUME_TELLER',
                rule_name='Teller Operational Surge',
                severity='MEDIUM',
                transaction_id=None,
                reference_id='TELLER-SURGE',
                account_number='N/A',
                customer_username=emp_uname,
                amount=None,
                timestamp=now,
                reason=f"Employee @{emp_uname} conducted {op['total_ops']} counter operations within the past 24 hours, exceeding the surge threshold ({TELLER_DAILY_SURGE_THRESHOLD})."
            ))

    # Apply filters
    if rule_filter:
        flags = [f for f in flags if f.rule_id == rule_filter]
    if severity_filter:
        flags = [f for f in flags if f.severity == severity_filter]

    critical_count = sum(1 for f in flags if f.severity == 'CRITICAL')
    high_count = sum(1 for f in flags if f.severity == 'HIGH')
    medium_count = sum(1 for f in flags if f.severity == 'MEDIUM')
    low_count = sum(1 for f in flags if f.severity == 'LOW')

    stats = {
        'total_flags': len(flags),
        'critical_count': critical_count,
        'high_count': high_count,
        'medium_count': medium_count,
        'low_count': low_count,
        'scan_timestamp': now,
        'large_threshold': LARGE_TRANSACTION_THRESHOLD,
    }

    return flags, stats

